import prompt_pipeline as pp


def _beat(index, operation, text, *, space_id='primary'):
    return {
        'index': index,
        'operation': operation,
        'description': text,
        'milestone_name': text,
        'before_state': 'the prior registered state',
        'after_state': text,
        'space_id': space_id,
        'package_operations': [operation],
    }


def test_project_origin_distinguishes_build_from_restoration_and_delivery():
    assert pp.project_origin_mode(
        {'carrier': 'mountain bunker', 'trauma': 'bare alpine ground'},
        'secret underground mountain bunker build') == 'ground_up_build'
    assert pp.project_origin_mode(
        {'carrier': 'mountain bunker', 'trauma': 'abandoned rusty bunker'},
        'abandoned bunker restoration') == 'existing_restoration'
    assert pp.project_origin_mode(
        {'carrier_delivered_on_camera': True, 'carrier': 'retired rail car'},
        'rail car shelter') == 'carrier_delivery_build'


def test_ground_up_underground_plan_requires_structure_and_engineering_systems():
    brief = pp.apply_project_contract({
        'theme': '地下山体掩体建造',
        'carrier': 'mountain bunker',
        'trauma': 'bare rocky hillside',
        'space_type': 'underground space',
        'mode': 'Threshold',
    })
    ladder = [
        _beat(1, 'clearing', 'clear loose stones from the ground', space_id='site'),
        _beat(2, 'flooring', 'install the complete oak floor'),
        _beat(3, 'furnishing', 'install the built-in bed'),
    ]
    errors = pp.spatial_planning_violations(ladder, brief)
    joined = ' '.join(errors).lower()
    assert 'excavation' in joined
    assert 'structural shell' in joined
    assert 'drainage' in joined
    assert 'waterproofing' in joined
    assert 'ventilation' in joined
    assert 'power' in joined


def test_complete_ground_up_underground_plan_passes_origin_and_system_gates():
    brief = pp.apply_project_contract({
        'theme': '地下山体掩体建造',
        'carrier': 'mountain bunker',
        'trauma': 'bare rocky hillside',
        'space_type': 'underground space',
        'mode': 'Standard',
    })
    ladder = [
        _beat(1, 'clearing', 'excavate the bunker trench and grow a spoil pile'),
        _beat(2, 'repair', 'assemble steel arch segments, portal frame and concrete end wall'),
        _beat(3, 'rough-in', 'install perimeter drainage channel, sump and waterproof membrane'),
        _beat(4, 'wiring', 'install ventilation intake duct, exhaust vent, electrical wiring and battery power source'),
        _beat(5, 'framing', 'install wall and ceiling framing'),
        _beat(6, 'drywall', 'close wall and ceiling panels'),
        _beat(7, 'flooring', 'install the complete oak floor'),
        _beat(8, 'lighting', 'install permanent ceiling fixtures'),
        _beat(9, 'furnishing', 'install the built-in bed'),
    ]
    assert pp.narrative_origin_violations(ladder, brief) == []
    assert pp.engineering_system_violations(ladder, brief) == []
    assert pp.construction_state_violations(ladder, brief) == []


def test_finished_floor_cannot_change_material_without_removal_beat():
    ladder = [
        _beat(1, 'flooring', 'complete the thermal oak floorboards across the room'),
        _beat(2, 'lighting', 'install permanent ceiling fixtures'),
        _beat(3, 'flooring', 'complete a cured concrete floor surface across the room'),
    ]
    errors = pp.construction_state_violations(ladder, {'mode': 'Standard'})
    assert any('floor from wood to concrete' in error for error in errors)


def test_secondary_room_has_its_own_monotonic_queue():
    ladder = [
        _beat(1, 'flooring', 'complete the oak floor in the primary room', space_id='primary'),
        _beat(2, 'furnishing', 'install the primary room cabinetry', space_id='primary'),
        _beat(3, 'clearing', 'clear debris from the raw rear compartment', space_id='secondary'),
        _beat(4, 'rough-in', 'install rear-compartment wiring and conduit', space_id='secondary'),
    ]
    assert pp.construction_state_violations(ladder, {'mode': 'Threshold'}) == []


def test_temporary_tripod_light_is_not_a_standalone_milestone():
    ladder = [_beat(1, 'lighting', 'set up a portable tripod LED work light and extension cable')]
    errors = pp.construction_state_violations(ladder, {'mode': 'Standard'})
    assert any('temporary work light' in error for error in errors)


def test_packet_inherits_origin_and_engineering_contracts():
    brief = pp.apply_project_contract({
        'theme': '地下山体掩体建造',
        'carrier': 'mountain bunker',
        'space_type': 'underground space',
    })
    packet = pp.merge_spatial_contract_into_packet({'camera_dna': 'locked'}, brief)
    assert packet['origin_contract']['mode'] == 'ground_up_build'
    assert set(packet['engineering_plan']['required_systems']) == {
        'drainage', 'waterproofing', 'ventilation', 'power'}
