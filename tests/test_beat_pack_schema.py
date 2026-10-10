"""beat_pack.schema：模型产出的圣经与逐段行在进入拼装之前的确定性校验。"""
import copy

import pytest

import beat_pack_fixtures as fx
from beat_pack import schema


@pytest.fixture
def bible():
    return fx.example_bible()


def codes(issues):
    return {item['code'] for item in issues}


def merged_rows(bible):
    return [schema.merge_outline(bible, [row], i)[0] for i, row in enumerate(fx.full_rows(bible), 1)]


def test_the_shipped_example_and_the_fixture_rows_are_valid(bible):
    assert schema.validate_bible(bible, 28) == []
    assert schema.validate_theme(bible, merged_rows(bible), 28) == []


@pytest.mark.parametrize('mutate, code', [
    (lambda b: b['cameras'].pop('work'), 'camera_required'),
    (lambda b: b.update(geometry=b['geometry'] + ' 这里混进了中文。'), 'english_only'),
    (lambda b: b.update(geometry='A giant cone ' * 5), 'required_text'),
    (lambda b: b.update(geometry=b['geometry'].replace('door', 'hatch').replace('Door', 'Hatch')), 'geometry_door'),
    (lambda b: b['components'][0].update(text='A worker stands beside the giant husk with a long tool in hand.'), 'person_in_state_text'),
    (lambda b: b['cameras']['face'].update(text=b['cameras']['face']['text'] + ' A man stands for scale.'), 'person_in_state_text'),
    (lambda b: b['components'][1].update(key=b['components'][0]['key']), 'component_key_duplicate'),
    (lambda b: b['components'][1].update(scope='outside'), 'component_scope'),
    (lambda b: b['components'].__delitem__(slice(0, 12)), 'components_count'),
    (lambda b: b['outline'].pop(), 'outline_count'),
    (lambda b: b['outline'][4].update(kind='bridge'), 'outline_bridge'),
    (lambda b: b['outline'][-1].update(kind='construction'), 'outline_reveal'),
    (lambda b: b['outline'][5].update(cam='ghost'), 'outline_cam'),
    (lambda b: b['outline'][8].pop('imgcam'), 'outline_imgcam_required'),
    (lambda b: b['outline'][-1].pop('imgcam'), 'outline_imgcam_required'),
    (lambda b: b['outline'][-1].pop('cam_end'), 'outline_cam_end'),
    (lambda b: b['outline'][-1].update(cam_end='reveal'), 'outline_still_cam'),
    (lambda b: b['ledger']['stair'].update(rise_m=3.4), 'stair_arithmetic'),
    (lambda b: b['checks'][0].update(status='fail'), 'check_status'),
    (lambda b: b['checks'][0].update(scope='source'), 'check_scope'),
    (lambda b: b['payoff'].update(people_present=True), 'payoff_people'),
    (lambda b: b.update(name='一个特别特别特别特别长的名字'), 'name_length'),
    (lambda b: b.update(space_id='Bad Id'), 'space_id'),
    (lambda b: b['spaces'][0]['dimensions_m'].pop('height'), 'dimension_missing'),
    (lambda b: b['spaces'][0]['dimensions_m'].update(width=-1), 'dimension_value'),
    (lambda b: b.update(openings=[]), 'openings'),
    (lambda b: b.update(hook={'first_conflict': '只有一项'}), 'required_text'),
])
def test_bible_mutations_are_caught_with_a_specific_code(bible, mutate, code):
    broken = copy.deepcopy(bible)
    mutate(broken)
    assert code in codes(schema.validate_bible(broken, 28)), schema.validate_bible(broken, 28)


def test_interior_cameras_must_be_fixed(bible):
    broken = copy.deepcopy(bible)
    for key in ('int_s', 'int_k', 'int_u'):
        broken['cameras'][key]['travelling'] = True
    assert 'camera_interior' in codes(schema.validate_bible(broken, 28))


def test_explicit_travelling_flag_overrides_the_naming_convention(bible):
    cams = copy.deepcopy(bible['cameras'])
    assert schema.travelling_cameras(cams) == {'bridge', 'reveal'}
    cams['bridge']['travelling'] = False
    cams['work']['travelling'] = True
    assert schema.travelling_cameras(cams) == {'reveal', 'work'}


def test_a_negated_person_noun_is_allowed_but_a_bare_one_is_not():
    assert schema.person_nouns('Entirely unoccupied: no people, hands or silhouettes.') == []
    assert schema.person_nouns('No worker and without any person here.') == []
    assert schema.person_nouns('A worker and the Builder, a human figure.') == ['builder', 'figure', 'human', 'worker']
    assert schema.person_nouns("the builder's ladder") == ['builder']
    assert schema.person_nouns('A pre-built woodpile and a manhole cover.') == []        # 词边界：不误伤 built / manhole


def test_cjk_detection_covers_punctuation_and_ideographs():
    assert schema.has_cjk('door，window') and schema.has_cjk('门') and schema.has_cjk('a。b')
    assert not schema.has_cjk('A 0.90 m door, sill 2.0 m - fine.')


@pytest.mark.parametrize('reply, ok', [
    ('{"a": 1}', True),
    ('```json\n{"a": {"b": [1, 2]}}\n```', True),
    ('Here you go:\n{"a": "brace } inside a string"}\nHope that helps.', True),
    ('{"a": "quote \\" and } inside"} trailing {not json}', True),
    ('', False),
    ('no json at all', False),
    ('{"a": 1', False),
    ('[1, 2, 3]', False),
])
def test_parse_json_reply(reply, ok):
    if ok:
        assert isinstance(schema.parse_json_reply(reply), dict)
    else:
        with pytest.raises(ValueError):
            schema.parse_json_reply(reply)


def test_merge_outline_fixes_structure_and_drops_model_overrides(bible):
    content = fx.content_row(bible, 9)
    content.update(cam='open', kind='construction', stage='乱写的阶段', actor=False)
    row = schema.merge_outline(bible, [content], 9)[0]
    entry = bible['outline'][8]
    assert (row['cam'], row['kind'], row['stage'], row['imgcam']) == (entry['cam'], 'bridge', entry['stage'], entry['imgcam'])
    assert 'actor' not in row and 'index' not in row
    reveal = schema.merge_outline(bible, [fx.content_row(bible, 28)], 28)[0]
    assert reveal['actor'] is False and reveal['cam_end'] == 'int_u' and reveal['imgcam'] == 'int_s'


def test_merge_outline_keeps_a_valid_label_and_fills_a_missing_one(bible):
    content = fx.content_row(bible, 3)
    content['label'] = '改写后的工序名'
    assert schema.merge_outline(bible, [content], 3)[0]['label'] == '改写后的工序名'
    content.pop('label')
    assert schema.merge_outline(bible, [content], 3)[0]['label'] == bible['outline'][2]['label']


@pytest.mark.parametrize('mutate, code', [
    (lambda r: r.update(act=r['act'] + ' 工人'), 'english_only'),
    (lambda r: r.update(act='The builder works.'), 'required_text'),
    (lambda r: r.update(act=r['act'].replace('builder', 'carpenter')), 'act_actor'),
    (lambda r: r['upd'].update(ghost='Some perfectly fine English state text here.'), 'upd_key'),
    (lambda r: r.update(upd={}), 'row_upd'),
    (lambda r: r.update(show=['ghost']), 'show_key'),
    (lambda r: r.update(sound='嘎吱声'), 'english_only'),
    (lambda r: r.update(wear='穿着外套'), 'english_only'),
    (lambda r: r.update(vis=''), 'required_text'),
    (lambda r: r['upd'].update(shell='A man leans on the freshly finished shell wall with a hammer.'), 'person_in_state_text'),
])
def test_row_mutations_are_caught(bible, mutate, code):
    row = schema.merge_outline(bible, [fx.content_row(bible, 5)], 5)[0]
    mutate(row)
    assert code in codes(schema.validate_row(bible, row, 5)), schema.validate_row(bible, row, 5)


def test_a_change_the_camera_cannot_see_is_rejected(bible):
    # 第 4 段用外景机位 face，却只更新室内组件 walls：镜头里看不到任何变化。
    row = schema.merge_outline(bible, [fx.content_row(bible, 4)], 4)[0]
    assert bible['cameras'][row['cam']]['scope'] == 'ext'
    row['upd'] = {'walls': 'The wall is now freshly planed and oiled to a warm honey colour.'}
    assert 'no_visible_change' in codes(schema.validate_row(bible, row, 4))
    row['upd'] = {'walls': row['upd']['walls'], 'door': 'The door leaf is hung and latched, and the frame is sealed all round.'}
    assert 'no_visible_change' not in codes(schema.validate_row(bible, row, 4))


def test_the_first_clip_may_establish_without_updating_but_must_say_what_to_show(bible):
    row = schema.merge_outline(bible, [fx.content_row(bible, 1)], 1)[0]
    row['upd'] = {}
    assert 'row_show' not in codes(schema.validate_row(bible, row, 1))        # show 里已有组件
    row.pop('show')
    assert 'row_show' in codes(schema.validate_row(bible, row, 1))
    assert 'row_upd' not in codes(schema.validate_row(bible, row, 1))


def test_the_reveal_must_be_unoccupied_in_its_own_text(bible):
    row = schema.merge_outline(bible, [fx.content_row(bible, 28)], 28)[0]
    assert schema.validate_row(bible, row, 28) == []
    row['act'] += ' A woman opens the curtain.'
    assert 'person_in_reveal' in codes(schema.validate_row(bible, row, 28))
    row['actor'] = True
    assert 'reveal_actor' in codes(schema.validate_row(bible, row, 28))


def test_validate_theme_reports_missing_rows_and_stops_early_on_a_broken_outline(bible):
    rows = merged_rows(bible)
    assert 'rows_count' in codes(schema.validate_theme(bible, rows[:-1], 28))
    broken = copy.deepcopy(bible)
    broken['outline'].pop()
    issues = schema.validate_theme(broken, rows, 28)
    assert 'outline_count' in codes(issues) and 'rows_count' not in codes(issues)


def test_state_helpers_accumulate_updates(bible):
    state = schema.initial_state(bible)
    assert state['door'].startswith('No entrance exists')
    state = schema.apply_row(state, {'upd': {'door': 'new door state'}})
    assert state['door'] == 'new door state' and state['shell'] == schema.initial_state(bible)['shell']


def test_issues_carry_the_clip_number(bible):
    row = schema.merge_outline(bible, [fx.content_row(bible, 6)], 6)[0]
    row['act'] = 'short'
    issues = schema.validate_rows(bible, [row], 6)
    assert issues and all(item['row'] == 6 for item in issues)
