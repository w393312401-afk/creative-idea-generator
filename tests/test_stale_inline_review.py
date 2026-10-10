"""测试帧变更后 inline_beat_review 与链上守卫标记的作废机制（Step 1）：

1. 当参与某一拍的任一帧图被重绘（hash 不匹配），drop_stale_review_verdicts 作废该拍的 inline_beat_review
2. 若该帧由守卫打上了 flag（flag_origin='chain_guard'），则 quality_gate 回落为 pending_manual_review，清空 vlm_qa_reason 并移除 flag_origin
3. 未变动的帧与其守卫记录完好保留
"""
import json
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

import server_common


class TestStaleInlineReview(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp_dir, 'outputs', 'test_proj')
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.orig_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = os.path.join(self.tmp_dir, 'outputs')

        # 创建 3 张测试图片
        self.paths = {}
        self.hashes = {}
        for s in (1, 2, 3):
            p = os.path.join(self.frames_dir, f'img_{s:03d}.webp')
            Image.new('RGB', (64, 64), color=(s * 50, 0, 0)).save(p, 'WEBP')
            self.paths[s] = p
            self.hashes[s] = server_common.frame_content_hash(p)

        self.manifest = {
            'title': 'test_proj',
            'frames': [
                {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'auto_approved'},
                {
                    'sequence': 2,
                    'file': 'frames/img_002.webp',
                    'quality_gate': 'sequence_review_flagged',
                    'vlm_qa_reason': '结构畸变',
                    'flag_origin': 'chain_guard',
                    'inline_beat_review': {
                        'beat': 1,
                        'verdict': 'flagged',
                        'issues': [{'beat': 1, 'text': '结构畸变', 'severity': 'chain'}],
                        'frames_sha256': {'1': self.hashes[1], '2': self.hashes[2]},
                    }
                },
                {
                    'sequence': 3,
                    'file': 'frames/img_003.webp',
                    'quality_gate': 'auto_approved',
                    'inline_beat_review': {
                        'beat': 2,
                        'verdict': 'pass',
                        'issues': [],
                        'frames_sha256': {'2': self.hashes[2], '3': self.hashes[3]},
                    }
                },
            ]
        }

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_unchanged_frames_retain_inline_review(self):
        changed = server_common.drop_stale_review_verdicts(self.manifest, self.project_dir)
        self.assertEqual(changed, [])
        self.assertIn('inline_beat_review', self.manifest['frames'][1])
        self.assertIn('inline_beat_review', self.manifest['frames'][2])

    def test_modifying_target_frame_drops_its_guard_flag_and_inline_review(self):
        # 修改 img_002.webp（重渲帧 2）
        Image.new('RGB', (64, 64), color='green').save(self.paths[2], 'WEBP')
        changed = server_common.drop_stale_review_verdicts(self.manifest, self.project_dir)
        # 帧 2 和 帧 3 的 inline 记录都依赖了帧 2，都应作废
        self.assertIn(2, changed)
        self.assertIn(3, changed)

        frame2 = self.manifest['frames'][1]
        self.assertNotIn('inline_beat_review', frame2)
        self.assertNotIn('flag_origin', frame2)
        self.assertEqual(frame2['quality_gate'], 'pending_manual_review')
        self.assertIsNone(frame2['vlm_qa_reason'])

        frame3 = self.manifest['frames'][2]
        self.assertNotIn('inline_beat_review', frame3)

    def test_modifying_preceding_frame_drops_arrival_inline_review(self):
        # 修改 img_001.webp（重渲帧 1）
        Image.new('RGB', (64, 64), color='green').save(self.paths[1], 'WEBP')
        changed = server_common.drop_stale_review_verdicts(self.manifest, self.project_dir)
        self.assertEqual(changed, [2])

        frame2 = self.manifest['frames'][1]
        self.assertNotIn('inline_beat_review', frame2)
        self.assertNotIn('flag_origin', frame2)
        self.assertEqual(frame2['quality_gate'], 'pending_manual_review')

        # 帧 3 的 inline 记录依赖 2 和 3，未受影响
        frame3 = self.manifest['frames'][2]
        self.assertIn('inline_beat_review', frame3)


if __name__ == '__main__':
    unittest.main()
