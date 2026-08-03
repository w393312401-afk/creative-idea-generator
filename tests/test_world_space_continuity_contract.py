import copy

import prompt_pipeline as pp


def _marker_ladder(include_secondary=False):
    beats = [
        {'index': 1, 'operation': 'repair', 'description': 'carrier delivery',
         'persistent_traces': ['access-route tire ruts', 'carrier-proportional spoil ridge']},
        {'index': 2, 'operation': 'repair', 'description': 'entrance complete'},
        {'index': 3, 'operation': 'threshold', 'description': 'primary crossing', 'bridge_stage': 1},
        {'index': 4, 'operation': 'clearing', 'description': 'primary cleanout'},
    ]
    if include_secondary:
        beats += [
            {'index': 5, 'operation': 'repair', 'description': 'primary payoff'},
            {'index': 6, 'operation': 'threshold', 'description': 'secondary marker', 'hard_cut': True},
            {'index': 7, 'operation': 'clearing', 'description': 'secondary cleanout'},
        ]
    beats.append({'index': len(beats) + 1, 'operation': 'reward', 'description': 'reward'})
    return beats


def _brief(plane, nested=False):
    brief = {
        'carrier': 'retired rail car', 'mode': 'Threshold',
        'threshold_opening_plane': plane,
        'threshold_entry_motion': 'vertical_descent' if plane == 'horizontal_top' else 'level_push',
        'threshold_turn_degrees': 90 if plane != 'vertical_axial' else 0,
        'threshold_turn_direction': 'right' if plane != 'vertical_axial' else 'none',
        'pacing_skeleton': 'nested_space_payoff' if nested else 'linear_milestone',
    }
    if nested:
        brief.update({'secondary_space_transition': True,
                      'space_divider': 'the riveted end bulkhead with sliding door',
                      'secondary_space': 'the rear equipment compartment'})
    return brief


def test_world_and_topology_ledgers_are_backward_compatible_optional_dicts():
    brief = _brief('horizontal_top')
    pp.ensure_spatial_contract(brief)
    assert brief['world_lock']['status'] == 'provisional_until_image_1_acceptance'
    assert brief['entrance_topology']['opening_plane'] == 'horizontal_top'
    assert brief['carrier_envelope']['maximum_clear_interior']
    assert [node['id'] for node in brief['space_graph']['nodes']] == ['site', 'primary']


def test_topology_adaptive_primary_slots_do_not_remove_construction_beats():
    expected = {'vertical_axial': 3, 'vertical_side': 4, 'horizontal_top': 5}
    for plane, stage_count in expected.items():
        source = _marker_ladder()
        construction = [b['description'] for b in source if b['operation'] != 'threshold']
        expanded = pp.expand_spatial_transition_beats(copy.deepcopy(source), _brief(plane))
        stages = [b for b in expanded if b['transition_stage'] != 'none']
        assert len(stages) == stage_count
        assert [b['description'] for b in expanded if b['transition_stage'] == 'none'] == construction
        assert [b['index'] for b in expanded] == list(range(1, len(expanded) + 1))


def test_top_hatch_orders_hardware_shaft_landing_partial_then_establish():
    expanded = pp.expand_spatial_transition_beats(_marker_ladder(), _brief('horizontal_top'))
    stages = [b['transition_stage'] for b in expanded if b['transition_stage'] != 'none']
    assert stages == ['hatch_hardware_open', 'shaft_descent', 'landing_turn',
                      'partial_first_look', 'interior_establish']
    partial = next(b for b in expanded if b['transition_stage'] == 'partial_first_look')
    assert partial['reveal_scope'] == 'partial'
    assert 'portable lamp' in partial['light_source_state']


def test_nested_space_has_visible_divider_traversal_and_no_hard_cut():
    brief = _brief('vertical_side', nested=True)
    expanded = pp.expand_spatial_transition_beats(_marker_ladder(True), brief)
    assert not any(b.get('hard_cut') for b in expanded)
    secondary = [b for b in expanded if b.get('space_id') == 'secondary'
                 and b.get('transition_stage') != 'none']
    assert [b['transition_stage'] for b in secondary] == [
        'secondary_threshold', 'secondary_partial_first_look', 'secondary_establish']
    threshold = secondary[0]
    assert 'return light' in threshold['description']
    assert pp.beat_space_index(expanded, threshold['index']) == 2


def test_packet_inherits_all_spatial_ledgers():
    brief = _brief('vertical_axial')
    packet = pp.merge_spatial_contract_into_packet({'camera_dna': 'locked'}, brief)
    for key in ('world_lock', 'carrier_envelope', 'entrance_topology', 'space_graph',
                'camera_palette'):
        assert key in packet

