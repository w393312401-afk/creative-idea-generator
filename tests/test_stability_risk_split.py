import unittest

import prompt_pipeline as pp
from prompt_pipeline.frame_state import build_frame_state_contract, validate_frame_state_contract


def _beat():
    return {
        'index': 1,
        'operation': 'installation',
        'package_operations': ['set framing', 'fasten panels', 'seal joints'],
        'milestone_name': 'wall system complete',
        'before_state': 'bare wall structure',
        'after_state': 'wall system fully installed and sealed',
        'preserve_state': 'floor and windows unchanged',
        'completion_extent': 'the full wall is complete',
        'changed_grid_cells': ['A1', 'B1', 'C1'],
        'persistent_traces': ['fastener heads', 'sealed joints', 'alignment marks'],
        'introduced_objects': ['studs', 'panels', 'sealant'],
        'description': 'framing covers the old wall and hides the rough substrate',
    }


class StabilityRiskSplitTests(unittest.TestCase):
    def test_risk_counts_spatial_and_occlusion_complexity(self):
        self.assertGreaterEqual(pp.beat_stability_risk(_beat()), 3.4)

    def test_high_risk_three_operation_beat_becomes_two_valid_states(self):
        result = pp.split_high_risk_beats([_beat()], threshold=3.4)
        self.assertEqual(len(result), 2)
        self.assertEqual([b['index'] for b in result], [1, 2])
        self.assertNotEqual(result[0]['after_state'], result[1]['after_state'])
        self.assertEqual(result[1]['before_state'], result[0]['after_state'])
        errors = validate_frame_state_contract(build_frame_state_contract(result))
        self.assertEqual(errors, [])

    def test_low_risk_beat_is_not_split(self):
        beat = _beat()
        beat['changed_grid_cells'] = ['B2']
        beat['introduced_objects'] = []
        beat['description'] = 'small aligned installation'
        self.assertEqual(len(pp.split_high_risk_beats([beat], threshold=99)), 1)


if __name__ == '__main__':
    unittest.main()
