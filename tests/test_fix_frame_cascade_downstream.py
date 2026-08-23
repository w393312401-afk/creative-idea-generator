import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import pipeline_orchestrator as po
import candidate_selection_pipeline as csp


class TestFixFrameCascadeDownstream(unittest.TestCase):
    TITLE = 'test_fix_cascade_proj'
    PROMPT_BLOCK = (
        "图片 1: a raw unfinished concrete room\n"
        "视频 1: install timber studs\n"
        "图片 2: timber framing studs installed on the left wall\n"
        "视频 2: attach drywall boards\n"
        "图片 3: white drywall boards fully enclosing the room\n"
        "视频 3: install wooden floor\n"
        "图片 4: wooden floor installed in the room\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = os.path.join(self.tmp, self.TITLE)
        self.frames_dir = os.path.join(self.project_dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)

        for seq in (1, 2, 3, 4):
            with open(os.path.join(self.frames_dir, f'img_{seq:03d}.webp'), 'wb') as f:
                f.write(b'fake_frame_bytes')

        frames = [
            {'sequence': 1, 'file': 'frames/img_001.webp', 'quality_gate': 'sequence_reviewed_pass'},
            {'sequence': 2, 'file': 'frames/img_002.webp', 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': '左墙龙骨缺失'},
            {'sequence': 3, 'file': 'frames/img_003.webp', 'quality_gate': 'sequence_reviewed_pass'},
            {'sequence': 4, 'file': 'frames/img_004.webp', 'quality_gate': 'sequence_reviewed_pass'},
        ]
        server_common.write_manifest(self.project_dir, {'title': self.TITLE, 'frames': frames})

    def tearDown(self):
        server_common.OUTPUT_ROOT = self._orig_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fix_frame_with_cascade_downstream_true(self):
        """cascade_downstream=True 时，target_sequences 包含当前帧及其所有下游帧 [2, 3, 4]。"""
        with patch.object(csp, 'run_candidate_selection_frame_sequence') as mock_run_cand, \
             patch.object(po, '_reverify_frame_issues', return_value={'resolved': ['左墙龙骨缺失'], 'remaining': []}):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2, cascade_downstream=True)
            mock_run_cand.assert_called_once()
            _, kwargs = mock_run_cand.call_args
            self.assertEqual(kwargs.get('target_sequences'), [2, 3, 4])

    def test_fix_frame_with_cascade_downstream_false(self):
        """cascade_downstream=False 时，target_sequences 仅为 [2]。"""
        with patch.object(csp, 'run_candidate_selection_frame_sequence') as mock_run_cand, \
             patch.object(po, '_reverify_frame_issues', return_value={'resolved': ['左墙龙骨缺失'], 'remaining': []}):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2, cascade_downstream=False)
            mock_run_cand.assert_called_once()
            _, kwargs = mock_run_cand.call_args
            self.assertEqual(kwargs.get('target_sequences'), [2])


if __name__ == '__main__':
    unittest.main()
