import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import shutil
import json
from unittest.mock import patch, MagicMock

import prompt_pipeline as pp
from prompt_pipeline import composers
import video_generator


class TestOmniVideoFrames(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_omni_fix_video_opening_dual_anchors(self):
        """验证在首尾帧模式 (single_frame=False) 下，Omni 生成双锚点开篇句。"""
        raw = "Craftsman builds the timber framework continuously."
        res = pp.fix_video_opening(1, raw, profile='omni', single_frame=False)
        self.assertTrue(res.startswith("Use the provided first frame and last frame as exact composition anchors."))
        self.assertIn("Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image", res)

    def test_omni_check_video_opening_accepts_dual_and_single(self):
        """验证 check_video_opening 对 Omni 兼容支持双锚点与单锚点。"""
        dual = "Use the provided first frame and last frame as exact composition anchors. Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. Action description."
        errors_dual = pp.check_video_opening(1, dual, profile='omni')
        self.assertEqual(errors_dual, [])

        single = "Use the provided image as the exact starting composition and environment anchor. Use IMAGE 1 as the actual first-frame image; begin from this initial state and naturally progress the work through the multi-shot sequence without inventing extraneous layouts. Action description."
        errors_single = pp.check_video_opening(1, single, profile='omni', single_frame=True)
        self.assertEqual(errors_single, [])

    def test_omni_plan_video_slots_includes_both_frames(self):
        """验证 plan_video_slots 在 Omni 视频提示词下正确规划 start_frame 和 end_frame，并改写为 IMAGE 1/2。"""
        frames_dir = os.path.join(self.tmp_dir, 'frames')
        videos_dir = os.path.join(self.tmp_dir, 'videos')
        os.makedirs(frames_dir, exist_ok=True)
        os.makedirs(videos_dir, exist_ok=True)

        f1 = os.path.join(frames_dir, 'img_001.webp')
        f2 = os.path.join(frames_dir, 'img_002.webp')
        with open(f1, 'wb') as f: f.write(b'f1')
        with open(f2, 'wb') as f: f.write(b'f2')

        video_slots = {
            1: {
                'body': "Use the provided first frame and last frame as exact composition anchors. Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. The worker hammers nails into the wooden beam.",
                'meta': 'demolition'
            }
        }
        slot_to_path = {1: f1, 2: f2}

        plans = video_generator.plan_video_slots(
            video_slots=video_slots,
            slot_to_path=slot_to_path,
            slot_to_quality={1: 'passed', 2: 'passed'},
            videos_dir=videos_dir,
            verify_fn=lambda *args: True
        )

        self.assertEqual(len(plans), 1)
        p = plans[0]
        self.assertEqual(p['start_frame'], f1)
        self.assertEqual(p['end_frame'], f2)
        self.assertEqual(p['start_anchor_slot'], 1)
        self.assertEqual(p['end_anchor_slot'], 2)
        self.assertIn("IMAGE 1", p['prompt'])
        self.assertIn("IMAGE 2", p['prompt'])


if __name__ == '__main__':
    unittest.main()
