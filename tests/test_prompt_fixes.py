import re
import unittest
from prompt_pipeline import (
    fix_image_clean_frame_proactive,
    check_nlvtr_violations,
    fix_sound_design,
    fix_video_opening,
    _flatten_to_text,
    normalize_packet,
    normalize_beat_ladder,
    check_stylistic_repetition
)
from frame_generator import _extract_image_prompts


class TestSlotExtraction(unittest.TestCase):
    """Regression: _parse_prompt_slots returns {'body','meta'} dicts; _extract_image_prompts
    must unwrap them so 'prompt' is plain prose (a dict repr would reach the image model)
    and the BRIDGE meta flag sits at the top level where generate_frame_sequence reads it."""

    BLOCK = (
        "图片提示词\n"
        "图片 1:\n"
        "A static shot of the ruined cabin.\n"
        "\n"
        "图片 2 [BRIDGE]:\n"
        "CHANGE IN THIS FRAME: camera now at the sill.\n"
    )

    def test_prompt_is_plain_string(self):
        items = _extract_image_prompts(self.BLOCK)
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertIsInstance(it['prompt'], str)
            self.assertNotIn("{'body'", it['prompt'])

    def test_bridge_meta_surfaces_at_top_level(self):
        items = _extract_image_prompts(self.BLOCK)
        self.assertEqual(items[0]['meta'], '')
        self.assertEqual(items[1]['meta'], 'BRIDGE')
        self.assertIn('camera now at the sill', items[1]['prompt'])

class TestPromptFixes(unittest.TestCase):
    
    def test_fix_image_clean_frame_proactive(self):
        # Test worker reference replacement
        prompt_with_worker = "A worker is installing the bed rails."
        cleaned = fix_image_clean_frame_proactive(prompt_with_worker)
        self.assertNotIn("worker", cleaned.lower())
        self.assertIn("equipment", cleaned.lower())
        self.assertIn("installation", cleaned.lower())

        # Test negative sentence with worker remains untouched
        prompt_negative = "No workers are present in this view."
        cleaned_negative = fix_image_clean_frame_proactive(prompt_negative)
        self.assertEqual(cleaned_negative, prompt_negative)

        # Test sweep replacement
        prompt_sweep = "The person is sweeping the floor."
        cleaned_sweep = fix_image_clean_frame_proactive(prompt_sweep)
        self.assertNotIn("sweeping", cleaned_sweep.lower())
        self.assertIn("swept dust", cleaned_sweep.lower())
        self.assertIn("object", cleaned_sweep.lower())  # person -> object

        # Test oil painting hangs on wall remains painting
        prompt_painting = "An oil painting hangs on the wall."
        cleaned_painting = fix_image_clean_frame_proactive(prompt_painting)
        self.assertIn("painting", cleaned_painting.lower())

    def test_check_nlvtr_violations(self):
        # Test % violation
        self.assertIn("Contains forbidden '%' symbol", check_nlvtr_violations("Progress is 50%"))
        
        # Test numeric range violation
        self.assertIn("Contains forbidden numeric range", check_nlvtr_violations("It measures 10 to 20 cm."))
        self.assertIn("Contains forbidden numeric range", check_nlvtr_violations("Range is 5-10 meters."))
        
        # Test acronym violation
        self.assertIn("Contains forbidden acronym 'GCTR'", check_nlvtr_violations("Using GCTR check."))
        self.assertIn("Contains forbidden acronym 'TSPA'", check_nlvtr_violations("Inside TSPA state."))

        # Test clean prompt
        self.assertEqual(len(check_nlvtr_violations("A clean description of the wooden table with tools on top.")), 0)

    def test_fix_sound_design(self):
        # Sound design missing -> should append sound clause
        prompt_no_sound = "The camera pans left."
        fixed = fix_sound_design(prompt_no_sound)
        self.assertIn("Sound effects include", fixed)
        self.assertIn("Ambient noise", fixed)

        # Sound design exists -> should remain unchanged
        prompt_with_sound = "Footsteps trigger a sound effect."
        fixed_existing = fix_sound_design(prompt_with_sound)
        self.assertEqual(fixed_existing, prompt_with_sound)

    def test_fix_video_opening(self):
        # Test empty or regular text
        prompt = "The camera slowly pans across the workspace."
        fixed = fix_video_opening(1, prompt)
        self.assertTrue(fixed.startswith("Use the provided first frame and last frame as exact composition anchors."))
        self.assertIn("IMAGE 1", fixed)
        self.assertIn("IMAGE 2", fixed)
        self.assertIn("The camera slowly pans", fixed)

        # Test already has partial opening
        prompt_with_partial = "Use the provided first frame. The camera zooms in."
        fixed_partial = fix_video_opening(2, prompt_with_partial)
        self.assertTrue(fixed_partial.startswith("Use the provided first frame and last frame as exact composition anchors."))
        self.assertIn("IMAGE 2", fixed_partial)
        self.assertIn("IMAGE 3", fixed_partial)

    def test_fix_primary_landmarks_no_replacement(self):
        from prompt_pipeline import fix_primary_landmarks
        packet = {
            'primary_landmarks': [
                {'name': 'sliding door frame sill', 'grid': 'Grid B2'},
                {'name': 'metal support beam', 'grid': 'Grid A1'}
            ]
        }
        
        # 'frame' and 'support beam' must NOT be replaced, and since they do not match the exact names,
        # both landmarks must be appended to the end of the prompt.
        prompt = "The horizon line remains level at 50-percent height of the frame. A worker inspects the support beam."
        fixed = fix_primary_landmarks(prompt, packet)
        
        # Original text remains untouched (no replacement)
        self.assertIn("height of the frame", fixed)
        self.assertIn("inspects the support beam", fixed)
        
        # Missing/imprecise landmarks are appended at the end
        self.assertIn("Locked anchors: sliding door frame sill at Grid B2, metal support beam at Grid A1", fixed)

    def test_threshold_bridge_stage_validation(self):
        # 1. Valid Threshold beat ladder
        valid_ladder = [
            {"index": 1, "operation": "clearing", "description": "Clear site", "bridge_stage": None},
            {"index": 2, "operation": "threshold", "description": "Approach doorway", "bridge_stage": 1},
            {"index": 3, "operation": "threshold", "description": "Cross doorway sill", "bridge_stage": 2},
            {"index": 4, "operation": "reward", "description": "Finished room", "bridge_stage": None}
        ]
        
        violations = []
        has_bridge_1 = False
        has_bridge_2 = False
        bridge_1_idx = -1
        bridge_2_idx = -1
        for idx, b in enumerate(valid_ladder):
            bs = b.get('bridge_stage')
            if bs == 1:
                has_bridge_1 = True
                bridge_1_idx = idx
            elif bs == 2:
                has_bridge_2 = True
                bridge_2_idx = idx
        if not (has_bridge_1 and has_bridge_2 and bridge_2_idx == bridge_1_idx + 1):
            violations.append("In Threshold mode, there must be exactly two consecutive beats with bridge_stage=1 and bridge_stage=2.")
        
        self.assertEqual(len(violations), 0)

        # 2. Invalid Threshold beat ladder (non-consecutive bridge stages)
        invalid_ladder = [
            {"index": 1, "operation": "clearing", "description": "Clear site", "bridge_stage": 1},
            {"index": 2, "operation": "threshold", "description": "Approach doorway", "bridge_stage": None},
            {"index": 3, "operation": "threshold", "description": "Cross doorway sill", "bridge_stage": 2},
            {"index": 4, "operation": "reward", "description": "Finished room", "bridge_stage": None}
        ]
        violations = []
        has_bridge_1 = False
        has_bridge_2 = False
        bridge_1_idx = -1
        bridge_2_idx = -1
        for idx, b in enumerate(invalid_ladder):
            bs = b.get('bridge_stage')
            if bs == 1:
                has_bridge_1 = True
                bridge_1_idx = idx
            elif bs == 2:
                has_bridge_2 = True
                bridge_2_idx = idx
        if not (has_bridge_1 and has_bridge_2 and bridge_2_idx == bridge_1_idx + 1):
            violations.append("In Threshold mode, there must be exactly two consecutive beats with bridge_stage=1 and bridge_stage=2.")
        
        self.assertEqual(len(violations), 1)

    def test_parse_and_format_prompt_slots_metadata(self):
        from prompt_pipeline import _parse_prompt_slots, _format_prompt_block
        block = """图片提示词
图片 1 [TRAUMA]:
Trauma state image prompt here.

图片 8 [BRIDGE]:
Bridge state image prompt here.

视频提示词
视频 1:
Video prompt 1 here.

视频 8 [BRIDGE]:
Video prompt 8 here.
"""
        images, videos = _parse_prompt_slots(block)
        
        self.assertIn(1, images)
        self.assertEqual(images[1]['body'], "Trauma state image prompt here.")
        self.assertEqual(images[1]['meta'], "TRAUMA")
        
        self.assertIn(8, images)
        self.assertEqual(images[8]['body'], "Bridge state image prompt here.")
        self.assertEqual(images[8]['meta'], "BRIDGE")
        
        self.assertIn(1, videos)
        self.assertEqual(videos[1]['body'], "Video prompt 1 here.")
        self.assertEqual(videos[1]['meta'], "")
        
        self.assertIn(8, videos)
        self.assertEqual(videos[8]['body'], "Video prompt 8 here.")
        self.assertEqual(videos[8]['meta'], "BRIDGE")
        
        # Format back and check that [BRIDGE] and [TRAUMA] are kept
        formatted = _format_prompt_block(images, videos)
        self.assertIn("图片 1 [TRAUMA]:", formatted)
        self.assertIn("图片 8 [BRIDGE]:", formatted)
        self.assertIn("视频 8 [BRIDGE]:", formatted)
        self.assertIn("视频 1:", formatted)

class TestPacketShapeNormalization(unittest.TestCase):
    """Regression tests for the Beat-2 abort: the packet LLM returned worker_choreography
    as a nested dict, and check_stylistic_repetition crashed on dict.lower()."""

    # Shape taken verbatim from the real poisoned cache entry (rooftop tram project)
    DICT_CHOREOGRAPHY = {
        "trajectory": "Workers enter from Grid C1 and exit via Grid C1 before the final frame.",
        "silhouette": "one lone worker in a solid bright-neon-yellow safety vest",
        "manual_tool_lock": "tools are locked to the worker's hands with no morphing",
    }

    def test_flatten_to_text(self):
        self.assertEqual(_flatten_to_text("already text"), "already text")
        self.assertEqual(_flatten_to_text(None), "")
        self.assertEqual(_flatten_to_text(["a", "b"]), "a b")
        flat = _flatten_to_text(self.DICT_CHOREOGRAPHY)
        self.assertIsInstance(flat, str)
        self.assertIn("trajectory:", flat)
        self.assertIn("safety vest", flat)

    def test_normalize_packet_flattens_prose_fields(self):
        packet = {
            "camera_dna": {"lens": "14mm", "height": "1.6m"},
            "worker_choreography": dict(self.DICT_CHOREOGRAPHY),
            "passive_environment": {"direction": "left-to-right", "elements": "clouds"},
            "primary_landmarks": [{"name": "column", "grid": "Grid B2", "z_depth_scale": 50}],
            "frame_boundaries": {"left": {"grid": "B1", "feature": "wall"}},
            "lighting_phase_ladder": {"1": "ambient only", "2": ["temporary", "work light"]},
            "object_ledger": [{"name": "bucket", "z_depth_scale": 10}],
        }
        normalize_packet(packet)
        self.assertIsInstance(packet["camera_dna"], str)
        self.assertIsInstance(packet["worker_choreography"], str)
        self.assertIn("safety vest", packet["worker_choreography"])
        self.assertIsInstance(packet["passive_environment"], str)
        self.assertIsInstance(packet["primary_landmarks"][0]["z_depth_scale"], str)
        self.assertIsInstance(packet["frame_boundaries"]["left"], str)
        self.assertIsInstance(packet["lighting_phase_ladder"]["2"], str)
        self.assertIsInstance(packet["object_ledger"][0]["z_depth_scale"], str)

    def test_normalize_packet_keeps_clean_packet_unchanged(self):
        packet = {"camera_dna": "static tripod shot", "worker_choreography": "one lone worker"}
        normalize_packet(packet)
        self.assertEqual(packet["camera_dna"], "static tripod shot")
        self.assertEqual(packet["worker_choreography"], "one lone worker")

    def test_check_stylistic_repetition_survives_dict_packet(self):
        # Even an UN-normalized packet must not crash the validator (defense in depth)
        packet = {"camera_dna": {"lens": "14mm"}, "worker_choreography": dict(self.DICT_CHOREOGRAPHY)}
        curr = "The worker lays grey stone tiles across the floor in repeated pressing cycles."
        prev = "The worker hoists timber studs into place and drives nails along the frame."
        errors = check_stylistic_repetition(curr, prev, packet, is_video=True)
        self.assertIsInstance(errors, list)

    def test_normalize_beat_ladder(self):
        ladder = [
            {"index": "1", "operation": "clearing", "description": {"text": "remove debris"}, "bridge_stage": None},
            {"index": 2, "operation": ["threshold", "approach"], "description": "push to sill", "bridge_stage": "1"},
        ]
        normalize_beat_ladder(ladder)
        self.assertEqual(ladder[0]["index"], 1)
        self.assertIsInstance(ladder[0]["description"], str)
        self.assertIn("remove debris", ladder[0]["description"])
        self.assertIsInstance(ladder[1]["operation"], str)
        self.assertEqual(ladder[1]["bridge_stage"], 1)


if __name__ == '__main__':
    unittest.main()
