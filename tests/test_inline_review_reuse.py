"""测试收尾审查对链上守卫结论的复用机制（Step 3）：

1. 守卫记录（inline_beat_review）不进 _valid_verdict_sequences，确保跨帧层必跑（不变量 03）
2. _valid_inline_beats 提取哈希一致且 verdict in ('pass', 'flagged') 的拍
3. local_beats 准确排除已守卫的拍，避免逐拍层重复付费
4. local_beats=[] 时仍跑跨帧层，跨帧层跑成后所有干净帧均拿到 sequence_reviewed_pass
5. 守卫检出的 flagged 经 merge_review_results 正确进入最终 failures 并写 quality_gate='sequence_review_flagged'
6. full=True 全量审查时不复用 inline_beat_review
"""
import json
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

import server_common
import prompt_pipeline as pp
import pipeline_orchestrator as po


class TestInlineReviewReuse(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_proj')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')

        # 创建 3 张测试图片 (beat 1: 1->2, beat 2: 2->3)
        self.hashes = {}
        for s in (1, 2, 3):
            p = os.path.join(self.frames_dir, f'img_{s:03d}.webp')
            Image.new('RGB', (64, 64), color=(s * 50, 0, 0)).save(p, 'WEBP')
            self.hashes[s] = server_common.frame_content_hash(p)

        self.prompt_block = (
            "IMAGE 1: Empty site\n"
            "VIDEO 1: Digging\n"
            "IMAGE 2: Excavated trench\n"
            "VIDEO 2: Concrete\n"
            "IMAGE 3: Foundation slab\n"
        )

        self.manifest = {
            'title': 'test_proj',
            'frames': [
                {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'auto_approved'},
                {
                    'sequence': 2, 'file': 'frames/img_002.webp', 'quality_gate': 'auto_approved',
                    'inline_beat_review': {
                        'beat': 1,
                        'verdict': 'pass',
                        'issues': [],
                        'frames_sha256': {'1': self.hashes[1], '2': self.hashes[2]},
                    }
                },
                {
                    'sequence': 3, 'file': 'frames/img_003.webp', 'quality_gate': 'auto_approved',
                    'inline_beat_review': {
                        'beat': 2,
                        'verdict': 'pass',
                        'issues': [],
                        'frames_sha256': {'2': self.hashes[2], '3': self.hashes[3]},
                    }
                },
            ]
        }
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(self.manifest, f)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_guard_records_do_not_enter_valid_verdict_sequences(self):
        """不变量 03：守卫记录绝不能进 _valid_verdict_sequences，否则会把跨帧层洗掉。"""
        valid_seqs = po._valid_verdict_sequences(self.project_dir, [1, 2, 3])
        self.assertEqual(valid_seqs, set())

    def test_valid_inline_beats_extraction(self):
        inline_ok = po._valid_inline_beats(self.project_dir, [1, 2, 3])
        self.assertEqual(inline_ok, {1, 2})

    def test_local_beats_excludes_inline_ok_and_runs_only_global_review(self):
        """当所有拍均有有效守卫记录时，local_beats 为空，check_full_sequence_consistency 只跑跨帧层。"""
        # 模拟跨帧层返回干净 {}
        with patch('prompt_pipeline.check_global_sequence_consistency', return_value={}) as mock_global, \
             patch('prompt_pipeline.check_beat_consistency') as mock_local:
            po._sequence_consistency_review({}, 'test_proj', self.prompt_block, self.project_dir)
            # 逐拍层不应被调用（local_beats=[]）
            self.assertEqual(mock_local.call_count, 0)
            # 跨帧层必须被调用
            self.assertTrue(mock_global.called)

            # 最终所有帧均拿到 sequence_reviewed_pass 并且记录了 review_frames_sha256
            manifest = server_common.read_manifest(self.project_dir)
            for frame in manifest['frames']:
                self.assertEqual(frame['quality_gate'], 'sequence_reviewed_pass')
                self.assertIn('review_frames_sha256', frame)

    def test_flagged_inline_review_propagates_to_final_failures(self):
        """守卫标记为 flagged 的拍，其问题经 merge_review_results 进入 final_result 并标 sequence_review_flagged。"""
        # 修改 frame 2 的守卫结论为 flagged
        self.manifest['frames'][1]['inline_beat_review'] = {
            'beat': 1,
            'verdict': 'flagged',
            'issues': [{
                'beat': 1,
                'layer': 'local',
                'text': '门洞位置偏移',
                'frames': [1, 2],
                'verified': True,
                'severity': 'chain',
            }],
            'frames_sha256': {'1': self.hashes[1], '2': self.hashes[2]},
        }
        with open(os.path.join(self.project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(self.manifest, f)

        with patch('prompt_pipeline.check_global_sequence_consistency', return_value={}):
            po._sequence_consistency_review({}, 'test_proj', self.prompt_block, self.project_dir)
            manifest = server_common.read_manifest(self.project_dir)
            frame2 = manifest['frames'][1]
            self.assertEqual(frame2['quality_gate'], 'sequence_review_flagged')
            self.assertIn('门洞位置偏移', frame2['vlm_qa_reason'])
            # frame 3 (beat 2) 是干净的，应通过
            frame3 = manifest['frames'][2]
            self.assertEqual(frame3['quality_gate'], 'sequence_reviewed_pass')

    def test_full_review_ignores_inline_reviews(self):
        """full=True 时强制重审全部拍。"""
        with patch('prompt_pipeline.check_global_sequence_consistency', return_value={}), \
             patch('prompt_pipeline.check_beat_consistency', return_value=[]) as mock_local:
            po._sequence_consistency_review({}, 'test_proj', self.prompt_block, self.project_dir, full=True)
            # full=True 必须跑全部 2 拍的逐拍审查
            self.assertEqual(mock_local.call_count, 2)

    def _seed_outline(self):
        """给两拍的守卫记录挂上卡片工序判定，并在 manifest 里放一份交付总账。"""
        manifest = server_common.read_manifest(self.project_dir)
        manifest['outline_delivery_ledger'] = [
            {'index': 1, 'text': '挖基坑', 'beat': 1},
            {'index': 2, 'text': '浇混凝土', 'beat': 2},
        ]
        manifest['frames'][1]['inline_beat_review']['outline_frame_verdicts'] = {'1': 'visible'}
        manifest['frames'][2]['inline_beat_review']['outline_frame_verdicts'] = {'2': 'missing'}
        server_common.write_manifest(self.project_dir, manifest)

    def test_inline_result_carries_outline_verdicts(self):
        self._seed_outline()
        result = po._inline_result(self.project_dir, {1, 2})
        self.assertEqual(result['outline_frame_verdicts'], {'1': 'visible', '2': 'missing'})
        # 绝不伪造跨帧层
        self.assertFalse(result['global_reviewed'])
        self.assertFalse(result['global_attempted'])

    def test_outline_verdicts_survive_the_skipped_local_layer(self):
        """守卫过的拍在收尾那趟被跳过，逐拍层不会再产一次工序判定——
        它们必须经守卫记录进到交付总账，否则 frame_verdict 永远空着。"""
        self._seed_outline()
        with patch('prompt_pipeline.check_global_sequence_consistency', return_value={}), \
             patch('prompt_pipeline.check_beat_consistency') as mock_local:
            po._sequence_consistency_review({}, 'test_proj', self.prompt_block, self.project_dir)
            self.assertEqual(mock_local.call_count, 0)

        ledger = server_common.read_manifest(self.project_dir)['outline_delivery_ledger']
        by_index = {row['index']: row.get('frame_verdict') for row in ledger}
        self.assertEqual(by_index, {1: 'visible', 2: 'missing'})


if __name__ == '__main__':
    unittest.main()
