import os
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageEnhance

import frame_continuity as fc


def _scene(path, *, shift=(0, 0), floor_fill=None, brightness=1.0):
    image = Image.new('RGB', (360, 540), '#b9c8d2')
    draw = ImageDraw.Draw(image)
    dx, dy = shift
    draw.rectangle((35 + dx, 95 + dy, 325 + dx, 460 + dy), fill='#786e61', outline='#202020', width=6)
    draw.rectangle((65 + dx, 135 + dy, 145 + dx, 245 + dy), fill='#86a8be', outline='#202020', width=4)
    draw.rectangle((215 + dx, 135 + dy, 295 + dx, 245 + dy), fill='#86a8be', outline='#202020', width=4)
    for x in range(50, 330, 35):
        draw.line((x + dx, 330 + dy, x + dx, 455 + dy), fill='#443d35', width=3)
    if floor_fill:
        draw.rectangle((125, 300, 245, 520), fill=floor_fill)
    if brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    image.save(path)


class FrameContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = os.path.join(self.tmp.name, 'ref.png')
        _scene(self.ref)

    def test_grid_mask_expands_declared_cell(self):
        mask = fc.grid_mask((300, 300), ['B2'])
        self.assertEqual(mask[150, 150], 255)
        self.assertEqual(mask[10, 10], 0)
        self.assertGreater(int((mask > 0).sum()), 10000)
        foreground = fc.grid_mask((300, 300), ['C2'])
        self.assertEqual(foreground[260, 150], 255)
        self.assertEqual(foreground[30, 250], 0)

    def test_local_delta_passes_without_camera_drift(self):
        candidate = os.path.join(self.tmp.name, 'delta.png')
        _scene(candidate, floor_fill='#9a6d3a')
        result = fc.analyze_frame(
            self.ref, candidate, beat={'changed_grid_cells': ['B2', 'C2']}, mode='balanced')
        self.assertNotEqual(result['status'], 'failed')
        self.assertGreater(result['previous']['change_region_difference'], 0.035)

    def test_large_camera_shift_is_rejected(self):
        candidate = os.path.join(self.tmp.name, 'shift.png')
        _scene(candidate, shift=(35, 20), floor_fill='#9a6d3a')
        result = fc.analyze_frame(
            self.ref, candidate, beat={'changed_grid_cells': ['C2']}, mode='balanced')
        self.assertEqual(result['status'], 'failed', result)

    def test_brightness_change_is_not_hard_failure(self):
        candidate = os.path.join(self.tmp.name, 'light.png')
        _scene(candidate, floor_fill='#9a6d3a', brightness=1.06)
        result = fc.analyze_frame(
            self.ref, candidate, beat={'changed_grid_cells': ['B2', 'C2']}, mode='balanced')
        self.assertNotEqual(result['status'], 'failed', result)

    def test_low_progress_recommends_retry_but_does_not_hard_fail(self):
        candidate = os.path.join(self.tmp.name, 'same.png')
        _scene(candidate)
        result = fc.analyze_frame(
            self.ref, candidate, beat={'changed_grid_cells': ['B2']}, mode='balanced')
        self.assertEqual(result['status'], 'warned')
        self.assertTrue(result['retry_recommended'])

    def test_family_map_starts_new_family_at_transition(self):
        videos = {1: {'meta': ''}, 2: {'meta': 'BRIDGE TURN'}, 3: {'meta': ''}}
        result = fc.family_map([1, 2, 3, 4], videos)
        self.assertEqual(result, {1: 'family-1', 2: 'family-1',
                                  3: 'family-2', 4: 'family-2'})

    def test_family_master_sidecar_updates_when_head_is_regenerated(self):
        first = fc.register_family_master(self.tmp.name, 'family-1', 1, self.ref)
        changed = os.path.join(self.tmp.name, 'changed.png')
        _scene(changed, shift=(4, 0))
        os.replace(changed, self.ref)
        second = fc.register_family_master(self.tmp.name, 'family-1', 1, self.ref)
        self.assertNotEqual(first['image_sha256'], second['image_sha256'])

    def test_low_texture_degrades_to_warning_not_hard_failure(self):
        # When an image pair has very few keypoints/matches, it should degrade to a warning
        # rather than falsely rejecting on noisy affine estimation.
        blank_ref = os.path.join(self.tmp.name, 'blank_ref.png')
        blank_cand = os.path.join(self.tmp.name, 'blank_cand.png')
        img1 = Image.new('RGB', (360, 540), '#b9c8d2')
        img2 = Image.new('RGB', (360, 540), '#b9c8d2')
        d1 = ImageDraw.Draw(img1)
        d2 = ImageDraw.Draw(img2)
        d1.rectangle((50, 100, 65, 115), fill='#202020')
        d2.rectangle((55, 105, 70, 120), fill='#202020')
        img1.save(blank_ref)
        img2.save(blank_cand)
        result = fc.analyze_frame(blank_ref, blank_cand, mode='balanced')
        self.assertNotEqual(result['status'], 'failed')
        self.assertEqual(result['status'], 'warned')
        self.assertTrue(any('low-texture' in r for r in result['reasons']))


if __name__ == '__main__':
    unittest.main()
