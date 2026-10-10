import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import server_common
import pipeline_orchestrator as po
import candidate_selection_pipeline as csp


class TestFixFrameCandidateSelection(unittest.TestCase):
    TITLE = 'test_fix_4_candidates_proj'
    PROMPT_BLOCK = (
        "图片 1: a raw unfinished concrete room\n"
        "视频 1: install timber studs\n"
        "图片 2: timber framing studs installed on the left wall\n"
        "视频 2: attach drywall boards\n"
        "图片 3: white drywall boards fully enclosing the room\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = os.path.join(self.tmp, self.TITLE)
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)

        for seq in (1, 2, 3):
            with open(os.path.join(self.frames_dir, f'img_{seq:03d}.webp'), 'wb') as f:
                f.write(b'fake_frame_bytes')

        frames = [
            {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'sequence_reviewed_pass'},
            {'sequence': 2, 'file': 'frames/img_002.webp', 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': '左墙龙骨缺失'},
            {'sequence': 3, 'file': 'frames/img_003.webp', 'quality_gate': 'sequence_reviewed_pass'},
        ]
        server_common.write_manifest(self.project_dir, {'title': self.TITLE, 'frames': frames})

    def tearDown(self):
        server_common.OUTPUT_ROOT = self._orig_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fix_frame_generates_4_candidates_and_selects_best(self):
        """修复第 2 帧：生成 4 张候选图，AI 鉴别优选 #2，写入主帧并更新 manifest。"""
        # Mock candidate generation to produce 4 fake webp files
        def fake_generate_candidates(config, title, item, reference_path, seq, candidate_count=4, **kwargs):
            cands_dir = os.path.join(self.frames_dir, 'candidates', f'frame_{seq:03d}')
            os.makedirs(cands_dir, exist_ok=True)
            paths = []
            for i in range(1, candidate_count + 1):
                p = os.path.join(cands_dir, f'candidate_{i}.webp')
                with open(p, 'wb') as f:
                    f.write(f'candidate_content_{i}'.encode('utf-8'))
                paths.append(p)
            return paths

        fake_eval_result = {
            "candidates": [
                {"index": 1, "score": 80, "strengths": "构图准确", "defects": "光影稍暗"},
                {"index": 2, "score": 95, "strengths": "完美体现左墙木龙骨安装，细节极佳", "defects": "无"},
                {"index": 3, "score": 75, "strengths": "清晰", "defects": "龙骨位置有偏差"},
                {"index": 4, "score": 85, "strengths": "质感良好", "defects": "轻微透视漂移"},
            ],
            "best_index": 2,
            "selection_reason": "候选 #2 完美契合提示词要求，左墙木龙骨结构严谨，透视连续性极佳",
        }

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')), \
             patch.object(csp, 'generate_frame_candidates', side_effect=fake_generate_candidates) as mock_gen, \
             patch.object(csp, 'evaluate_and_select_best_candidate', return_value=fake_eval_result) as mock_eval, \
             patch.object(po, '_verify_review_violation', return_value=False):
            result = po.fix_frame_issue({'candidateSelectionMode': True}, self.TITLE, self.PROMPT_BLOCK, 2)

        self.assertTrue(result['undoable'])
        self.assertIn('fixed image 2', result['prompt_block'])
        self.assertEqual(result['reverify']['remaining'], [])

        # Verify manifest entry for frame 2
        manifest = server_common.read_manifest(self.project_dir)
        frame2 = next(f for f in manifest['frames'] if f['sequence'] == 2)

        self.assertEqual(frame2['chosen_candidate_index'], 2)
        self.assertEqual(frame2['selection_mode'], 'candidate_selection')
        self.assertEqual(len(frame2['candidates']), 4)
        self.assertTrue(frame2['candidates'][1]['is_chosen'])
        self.assertEqual(frame2['candidates'][1]['score'], 95)

        # Verify winning candidate content copied to img_002.webp
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            content = f.read()
        self.assertEqual(content, b'candidate_content_2')

        # Verify candidate switching works
        csp.switch_frame_candidate(self.TITLE, 2, 4)
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            new_content = f.read()
        self.assertEqual(new_content, b'candidate_content_4')

        # Verify undo restores previous state
        undo_res = po.undo_frame_fix(self.TITLE, 2, result['prompt_block'])
        with open(os.path.join(self.frames_dir, 'img_002.webp'), 'rb') as f:
            restored_content = f.read()
        self.assertEqual(restored_content, b'fake_frame_bytes')
        self.assertIn('timber framing studs installed on the left wall', undo_res['prompt_block'])

    def test_fix_frame_with_candidate_selection_disabled_generates_single_frame(self):
        """当 4选1 模式开关关闭（candidateSelectionMode: False）时，修复单帧走标准单帧重渲而非 4选1。"""
        fake_gen_seq = MagicMock()
        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')), \
             patch('frame_generator.generate_frame_sequence', fake_gen_seq), \
             patch.object(csp, 'run_candidate_selection_frame_sequence') as mock_cand_seq, \
             patch.object(po, '_verify_review_violation', return_value=False):
            result = po.fix_frame_issue({'candidateSelectionMode': False, 'generation_mode': 'standard'},
                                        self.TITLE, self.PROMPT_BLOCK, 2)

        self.assertTrue(result['undoable'])
        self.assertIn('fixed image 2', result['prompt_block'])
        # 确认未调用 4选1 流程，而是调用了标准 generate_frame_sequence
        mock_cand_seq.assert_not_called()
        fake_gen_seq.assert_called_once()
        self.assertEqual(fake_gen_seq.call_args[1]['target_sequences'], [2])

    def test_fix_frame_1_generates_4_candidates_and_selects_best(self):
        """修复第 1 帧（首帧）：生成 4 张候选图，AI 鉴别优选 #3，写入主帧并更新 manifest。"""
        def fake_generate_candidates(config, title, item, reference_path, seq, candidate_count=4, **kwargs):
            cands_dir = os.path.join(self.frames_dir, 'candidates', f'frame_{seq:03d}')
            os.makedirs(cands_dir, exist_ok=True)
            paths = []
            for i in range(1, candidate_count + 1):
                p = os.path.join(cands_dir, f'candidate_{i}.webp')
                with open(p, 'wb') as f:
                    f.write(f'candidate_frame1_content_{i}'.encode('utf-8'))
                paths.append(p)
            return paths

        fake_eval_result = {
            "candidates": [
                {"index": 1, "score": 82, "strengths": "原始毛坯质感", "defects": "稍亮"},
                {"index": 2, "score": 78, "strengths": "清晰", "defects": "有现代家具杂物"},
                {"index": 3, "score": 96, "strengths": "完美的原始毛坯混凝土房间，零施工污染", "defects": "无"},
                {"index": 4, "score": 88, "strengths": "质感好", "defects": "地面略平整"},
            ],
            "best_index": 3,
            "selection_reason": "候选 #3 完全符合首帧原始废墟毛坯要求",
        }

        # 标一下第 1 帧待修
        po.set_manual_frame_issue(self.TITLE, 1, '首帧画面不够原始毛坯')

        with patch.object(po, 'fix_image_prompt_with_vlm_feedback',
                          return_value='fixed raw concrete prompt'), \
             patch.object(csp, 'generate_frame_candidates', side_effect=fake_generate_candidates) as mock_gen, \
             patch.object(csp, 'evaluate_and_select_best_candidate', return_value=fake_eval_result) as mock_eval, \
             patch.object(po, '_verify_review_violation', return_value=False):
            result = po.fix_frame_issue({'candidateSelection': True}, self.TITLE, self.PROMPT_BLOCK, 1)

        self.assertTrue(result['undoable'])
        self.assertIn('fixed raw concrete prompt', result['prompt_block'])

        # Verify manifest entry for frame 1
        manifest = server_common.read_manifest(self.project_dir)
        frame1 = next(f for f in manifest['frames'] if f['sequence'] == 1)

        self.assertEqual(frame1['chosen_candidate_index'], 3)
        self.assertEqual(frame1['selection_mode'], 'candidate_selection')
        self.assertEqual(len(frame1['candidates']), 4)
        self.assertTrue(frame1['candidates'][2]['is_chosen'])
        self.assertEqual(frame1['candidates'][2]['score'], 96)

        # Verify winning candidate content copied to img_001.webp
        with open(os.path.join(self.frames_dir, 'img_001.webp'), 'rb') as f:
            content = f.read()
        self.assertEqual(content, b'candidate_frame1_content_3')
