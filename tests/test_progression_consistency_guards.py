from unittest.mock import patch

import pipeline_orchestrator as po
import prompt_pipeline as pp


def test_horizontal_top_hatch_forces_descent_and_quarter_turn():
    brief = {
        'threshold_variant': 'coaxial',
        'threshold_opening_plane': 'horizontal_top',
        'threshold_entry_motion': 'level_push',
        'threshold_turn_degrees': 0,
        'threshold_turn_direction': 'none',
    }
    pp.normalize_threshold_topology(brief)
    assert brief['threshold_entry_motion'] == 'vertical_descent'
    assert brief['threshold_turn_degrees'] == 90
    assert brief['threshold_turn_direction'] in ('left', 'right')


def test_vertical_axial_entry_keeps_zero_turn():
    brief = {'threshold_opening_plane': 'vertical_axial'}
    pp.normalize_threshold_topology(brief)
    assert brief['threshold_entry_motion'] == 'level_push'
    assert brief['threshold_turn_degrees'] == 0
    assert brief['threshold_turn_direction'] == 'none'


def test_local_review_prioritizes_declared_action_delta():
    prompt = pp._local_beat_review_system_prompt()
    assert 'DECLARED ACTION -> ANCHOR DELTA' in prompt
    assert 'clearing/demolition beat' in prompt
    assert 'may not also repair, prime, coat, clad, floor, furnish or illuminate' in prompt


def test_chain_drift_reviews_three_frame_exterior_family():
    images = {1: {'body': 'a'}, 2: {'body': 'b'}, 3: {'body': 'c'}}
    with patch.object(po, '_parse_prompt_slots', return_value=(images, {})), \
         patch.object(po, 'resolve_family_anchor', return_value=1), \
         patch.object(po, '_frame_path', side_effect=lambda title, seq: f'/tmp/img_{seq}.webp'), \
         patch.object(po.os.path, 'exists', return_value=True), \
         patch.object(po, 'run_chain_tail_drift_check', return_value=(True, 'PASS')) as review, \
         patch.object(po, 'read_manifest', return_value={}):
        po._chain_drift_lookback({'qaGateLevel': 'standard'}, 'demo', 'block', '/tmp/demo')
    review.assert_called_once()
    assert review.call_args.kwargs['anchor_seq'] == 1
    assert review.call_args.kwargs['mid_seq'] == 2
    assert review.call_args.kwargs['tail_seq'] == 3


def test_chain_drift_reviews_two_frame_family_by_reusing_tail_as_mid():
    images = {1: {'body': 'a'}, 2: {'body': 'b'}}
    with patch.object(po, '_parse_prompt_slots', return_value=(images, {})), \
         patch.object(po, 'resolve_family_anchor', return_value=1), \
         patch.object(po, '_frame_path', side_effect=lambda title, seq: f'/tmp/img_{seq}.webp'), \
         patch.object(po.os.path, 'exists', return_value=True), \
         patch.object(po, 'run_chain_tail_drift_check', return_value=(True, 'PASS')) as review, \
         patch.object(po, 'read_manifest', return_value={}):
        po._chain_drift_lookback({'qaGateLevel': 'standard'}, 'demo', 'block', '/tmp/demo')
    args = review.call_args.args
    assert args[2] == args[3] == '/tmp/img_2.webp'
