"""单图段（只上首帧）的正文改写。

2026-08-22：单帧分支此前只把首帧编号换成 IMAGE 1，尾帧声明与插值指令原样留在正文里，
于是模型收到一个裸的 `IMAGE 13`（一张没上传的参考图）和一句"在两张帧之间插值"。更早一步
还有个更硬的坑：一条普通两锚点正文挂到单图槽位上时，正文里声明的尾帧编号会把改写拽回
两卡位口径，产出「IMAGE 1 首帧 / IMAGE 2 尾帧」——而 IMAGE 2 根本没上传。
"""
import os
import re
import unittest

from video_generator import (
    plan_video_slots,
    rewrite_prompt_for_single_frame,
    rewrite_prompt_for_two_card_ui,
)


# 实际末条视频的正文形态（沼泽废弃地堡那单的 vid_012）。
TWO_ANCHOR_BODY = (
    "Use the provided first frame and last frame as exact composition anchors. Use IMAGE 12 as the "
    "actual first-frame image and IMAGE 13 as the actual last-frame image; every visible action must "
    "interpolate between those two frame images without inventing a third layout. Continuous "
    "construction time-lapse, not real-time footage. Locked camera on a 24mm wide-angle lens at 1.3m "
    "chest level. The clip transitions from the pine-clad state of IMAGE 12 to the styled bedroom of "
    "IMAGE 13 through the reward reveal motion of warm lantern light rising. SFX: soft footsteps."
)


class TestSingleFrameRewrite(unittest.TestCase):
    def setUp(self):
        self.out = rewrite_prompt_for_single_frame(TWO_ANCHOR_BODY, 12)

    def test_no_bare_reference_to_an_unuploaded_image(self):
        # 唯一允许出现的编号是 IMAGE 1（那张真的上传了的首帧）。
        self.assertEqual(set(re.findall(r'IMAGE\s*(\d+)', self.out, re.IGNORECASE)), {'1'})

    def test_two_anchor_declaration_is_gone(self):
        low = self.out.lower()
        self.assertNotIn('last-frame image', low)
        self.assertNotIn('interpolate between those two frame images', low)

    def test_single_anchor_opening_is_declared(self):
        low = self.out.lower()
        self.assertIn('sole starting-frame anchor', low)
        self.assertIn('no last-frame reference image', low)

    def test_it_forbids_inventing_a_new_space(self):
        # 没有尾锚 = 模型可以自由发挥，这句是唯一的缰绳。
        self.assertIn('invent no new layout', self.out.lower())

    def test_action_body_survives(self):
        self.assertIn('24mm wide-angle lens', self.out)
        self.assertIn('warm lantern light rising', self.out)
        self.assertIn('SFX: soft footsteps', self.out)

    def test_empty_prompt_still_yields_a_usable_declaration(self):
        self.assertIn('sole starting-frame anchor', rewrite_prompt_for_single_frame('', 12).lower())


class TestTwoCardBranchUnaffected(unittest.TestCase):
    def test_ordinary_two_anchor_slot_still_maps_to_image_1_and_2(self):
        out = rewrite_prompt_for_two_card_ui(TWO_ANCHOR_BODY, 12, start_slot=12, end_slot=13)
        self.assertIn('IMAGE 1 as the actual first-frame image', out)
        self.assertIn('IMAGE 2 as the actual last-frame image', out)
        self.assertNotIn('sole starting-frame anchor', out.lower())


class TestHeroSlotPlanning(unittest.TestCase):
    """[HERO] 槽位：契约说只上首帧，正文里声明的尾帧编号不得改写口径。"""

    def _plan(self, tmpdir, meta):
        frames = {}
        for slot in (11, 12):
            path = os.path.join(tmpdir, f'img_{slot:03d}.webp')
            with open(path, 'wb') as fh:
                fh.write(b'x')
            frames[slot] = path
        videos = {12: {'body': TWO_ANCHOR_BODY, 'meta': meta}}
        return plan_video_slots(videos, frames, {}, tmpdir, verify_fn=lambda *a, **k: (True, ''))[0]

    def test_hero_slot_uploads_one_frame_and_rewrites_the_body(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, 'HERO')
            self.assertIsNone(plan['end_anchor_slot'])
            self.assertIsNone(plan['end_frame'])
            self.assertEqual(set(re.findall(r'IMAGE\s*(\d+)', plan['prompt'], re.IGNORECASE)), {'1'})
            self.assertIn('no last-frame reference image', plan['prompt'].lower())

    def test_hero_slot_does_not_block_and_generates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, 'HERO')
            self.assertEqual(plan['action'], 'generate')
            self.assertIsNone(plan['end_frame'])
            self.assertIsNone(plan['end_anchor_slot'])

    def test_ordinary_slot_keeps_both_anchors(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(tmp, 'PACE 1.24')
            self.assertEqual(plan['end_anchor_slot'], 13)
            self.assertNotIn('sole starting-frame anchor', plan['prompt'].lower())


if __name__ == '__main__':
    unittest.main()
