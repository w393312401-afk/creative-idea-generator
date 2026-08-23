import unittest
from unittest.mock import patch

import prompt_pipeline as pp


class TestDLSPAndCascadeRules(unittest.TestCase):
    def test_local_prompt_has_dlsp_and_anti_cavernous(self):
        prompt = pp._local_beat_review_system_prompt()
        self.assertIn('ANTI-CAVERNOUS & STRICT METRIC ENVELOPE', prompt)
        self.assertIn('MATTE FINISH & NO WET GLOSSY REFLECTIONS', prompt)
        self.assertIn('FULL-FIELD DELTA CONSERVATION & MULTI-ZONE MAPPING', prompt)
        self.assertIn('TWO-SHOT DECOUPLED CROSSING', prompt)
        self.assertIn('24mm wide-angle', prompt)
        self.assertIn('1.3m chest-level eye height', prompt)

    def test_global_prompt_has_dlsp_and_asymmetry(self):
        prompt = pp._global_review_system_prompt()
        self.assertIn('ASYMMETRIC LANDMARKS & DLSP 5-LAYER DEPTH PERSISTENCE', prompt)
        self.assertIn('Layer 1 foreground', prompt)
        self.assertIn('Layer 2 midground', prompt)
        self.assertIn('Layer 3 longitudinal', prompt)
        self.assertIn('Layer 4 background', prompt)

    def test_fix_beat_sandwich_notes_contains_dlsp(self):
        with patch.object(pp, '_chat', return_value='{"video": "v", "image": "i"}') as mock_chat:
            pp.fix_beat_from_sequence_review(
                {}, "v_prompt", "i_prompt", ["透视拉伸"],
                preceding_image_prompt="prev_state",
                succeeding_image_prompt="succ_target"
            )
            mock_chat.assert_called_once()
            system_prompt = mock_chat.call_args[0][1]
            self.assertIn('DLSP 5-LAYER DEPTH STAGING & METRIC ENVELOPE', system_prompt)
            self.assertIn('24mm wide-angle', system_prompt)
            self.assertIn('1.3m chest level', system_prompt)
            self.assertIn('warm matte/semi-matte', system_prompt)


if __name__ == '__main__':
    unittest.main()
