import json
import unittest
from unittest.mock import patch

from prompt_pipeline import (
    check_anchor_frame_compliance,
    refine_packet_from_accepted_anchor,
    call_llm,
)


class TestAnchorAcceptanceGate(unittest.TestCase):
    """check_anchor_frame_compliance / refine_packet_from_accepted_anchor: the new
    autonomous QA that IMAGE 1 never got before (frame_generator._run_vlm_qa always
    skipped it since there's no prior frame to compare motion against)."""

    PACKET = {
        "camera_dna": "static tripod shot, ultra-wide 14mm, height 1.6m",
        "primary_landmarks": [
            {"name": "cracked floor seam", "grid": "Grid C2", "z_depth_scale": "20%"},
        ],
    }
    PARSED_BRIEF = {"carrier": "a beached container ship", "env": "a tidal mudflat", "trauma": "hull breached and rusted"}

    def test_pass_response_returns_true(self):
        with patch('prompt_pipeline._multimodal_chat', return_value='PASS'):
            passed, reason = check_anchor_frame_compliance(
                {}, '/fake/img_001.webp', 'a static shot of the ruined hull', self.PACKET, self.PARSED_BRIEF,
            )
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')

    def test_fail_response_returns_false_with_reason(self):
        with patch('prompt_pipeline._multimodal_chat', return_value='FAIL: 画面里出现了一名工人'):
            passed, reason = check_anchor_frame_compliance(
                {}, '/fake/img_001.webp', 'a static shot of the ruined hull', self.PACKET, self.PARSED_BRIEF,
            )
        self.assertFalse(passed)
        self.assertIn('工人', reason)

    def test_api_error_fails_open(self):
        # Matches run_vlm_qa_check's convention: an infra failure must not block the
        # pipeline, so it counts as a (skipped) pass rather than a hard failure.
        with patch('prompt_pipeline._multimodal_chat', side_effect=RuntimeError('gateway down')):
            passed, reason = check_anchor_frame_compliance(
                {}, '/fake/img_001.webp', 'a static shot of the ruined hull', self.PACKET, self.PARSED_BRIEF,
            )
        self.assertTrue(passed)
        self.assertIn('Skipped', reason)

    def test_refine_packet_merges_valid_json(self):
        refined_json = json.dumps({
            "camera_dna": "static tripod shot, ultra-wide 14mm, height 1.6m",
            "geometry_lock": "single hull opening, no interior walls yet",
            "primary_landmarks": [
                {"name": "rusted hull breach", "grid": "Grid B2", "z_depth_scale": "50%"},
            ],
            "frame_boundaries": {"left": "B1", "right": "B3", "top": "A2", "bottom": "C2"},
        })
        with patch('prompt_pipeline._multimodal_chat', return_value=refined_json):
            refined = refine_packet_from_accepted_anchor({}, '/fake/img_001.webp', self.PACKET)
        self.assertEqual(refined['primary_landmarks'][0]['name'], 'rusted hull breach')
        self.assertEqual(refined['geometry_lock'], 'single hull opening, no interior walls yet')

    def test_refine_packet_falls_back_to_original_on_bad_json(self):
        with patch('prompt_pipeline._multimodal_chat', return_value='not valid json at all'):
            refined = refine_packet_from_accepted_anchor({}, '/fake/img_001.webp', self.PACKET)
        self.assertEqual(refined, self.PACKET)

    def test_refine_packet_falls_back_on_api_error(self):
        with patch('prompt_pipeline._multimodal_chat', side_effect=RuntimeError('gateway down')):
            refined = refine_packet_from_accepted_anchor({}, '/fake/img_001.webp', self.PACKET)
        self.assertEqual(refined, self.PACKET)


class TestCallLlmIsThinWrapper(unittest.TestCase):
    """call_llm must stay a pure two-phase composition: compose_anchor_and_packet's
    output is handed unchanged to compose_remaining_beats. This is what lets the
    autonomous pipeline call phase 1 alone, gate on the rendered IMAGE 1, mutate
    state['packet'], and only then call phase 2 — regression-guards the split."""

    def test_call_llm_composes_both_phases_in_order(self):
        fake_state = {'image_1_prompt': 'a static shot', 'packet': {'camera_dna': 'x'}}
        config = {}
        dimensions = {'theme': 'a beached container ship'}
        on_progress = lambda stage, details: None

        with patch('prompt_pipeline.compose_anchor_and_packet', return_value=fake_state) as phase1, \
             patch('prompt_pipeline.compose_remaining_beats', return_value='FINAL PROMPT BLOCK') as phase2:
            result = call_llm(config, dimensions, on_progress=on_progress)

        phase1.assert_called_once_with(config, dimensions, on_progress=on_progress)
        phase2.assert_called_once_with(config, fake_state, on_progress=on_progress)
        self.assertEqual(result, 'FINAL PROMPT BLOCK')


if __name__ == '__main__':
    unittest.main()
