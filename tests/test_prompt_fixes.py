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
