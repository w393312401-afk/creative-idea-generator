import re
import unittest
from prompt_pipeline import (
    fix_image_clean_frame_proactive,
    check_nlvtr_violations,
    fix_sound_design,
    fix_video_opening
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

if __name__ == '__main__':
    unittest.main()
