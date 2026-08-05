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


def test_expanded_crossing_stages_own_their_state_and_carry_no_construction():
    """One planner marker fans out into several stages; each must own its camera-position
    state and inherit none of the marker's milestone/trace fields.  Cloning them shipped
    three byte-identical beats in the 2026-08-05 petrified-cypress run."""
    marker = {'index': 3, 'operation': 'threshold', 'description': 'primary crossing',
              'bridge_stage': 1, 'milestone_name': 'interior threshold crossing complete',
              'before_state': 'Camera is outside looking up at the arch.',
              'after_state': 'Camera is fully inside the cavity.',
              'completion_extent': 'the whole crossing',
              'changed_grid_cells': ['B2'], 'package_operations': ['threshold'],
              'persistent_traces': ['footprints on threshold entrance log']}
    ladder = [b for b in _marker_ladder() if b['operation'] != 'threshold']
    ladder.insert(2, marker)
    stages = [b for b in pp.expand_spatial_transition_beats(copy.deepcopy(ladder),
                                                            _brief('vertical_axial'))
              if b['transition_stage'] != 'none']

    assert len({b['before_state'] for b in stages}) == len(stages)
    assert len({b['after_state'] for b in stages}) == len(stages)
    for stage in stages:
        assert stage['milestone_name'] == ''
        assert stage['completion_extent'] == ''
        assert stage['persistent_traces'] == []
        assert stage['changed_grid_cells'] == []
        assert stage['package_operations'] == []
        assert marker['after_state'] not in (stage['before_state'], stage['after_state'])


def test_hardware_crossing_stage_never_claims_a_door_no_beat_installed():
    """entrance_topology.hardware is a topology ledger, not proof a leaf exists.  Asserting one
    on a found carrier made the renderer bolt hinges onto bare shell and made the paired VIDEO
    smuggle a full door installation into a crossing clip."""
    ladder = _marker_ladder()
    stage = next(b for b in pp.expand_spatial_transition_beats(copy.deepcopy(ladder),
                                                               _brief('vertical_axial'))
                 if b['transition_stage'] == 'door_hardware_open')
    assert 'no door leaf, hinge, latch or gasket exists yet' in stage['description']
    assert 'nothing is installed, delivered or mounted' in stage['description']

    installed = copy.deepcopy(ladder)
    installed[1]['description'] = 'cedar door leaf and hinges mounted in the archway'
    fitted = next(b for b in pp.expand_spatial_transition_beats(installed, _brief('vertical_axial'))
                  if b['transition_stage'] == 'door_hardware_open')
    assert 'a previous beat already installed' in fitted['description']


def test_entrance_hardware_detection_ignores_substring_lookalikes():
    for decoy in ('the outdoor slope is regraded', 'interlocking block paving is laid'):
        ladder = _marker_ladder()
        ladder[1]['description'] = decoy
        stage = next(b for b in pp.expand_spatial_transition_beats(ladder, _brief('vertical_axial'))
                     if b['transition_stage'] == 'door_hardware_open')
        assert 'no door leaf, hinge, latch or gasket exists yet' in stage['description']


def test_packet_inherits_all_spatial_ledgers():
    brief = _brief('vertical_axial')
    packet = pp.merge_spatial_contract_into_packet({'camera_dna': 'locked'}, brief)
    for key in ('world_lock', 'carrier_envelope', 'entrance_topology', 'space_graph',
                'camera_palette'):
        assert key in packet

