"""单拍制作字段（2026-08-22）的三条不变量。

这一组字段是为了堵住三个各自静默的洞：
  1. **去掉** —— 大环境栏被写进施工产物、细节栏复述已经写过的东西、痕迹栏混进原本
     就有的环境物。三样都不会报错，只是把有限的配额花在重复信息上；
  2. **加上** —— 工具、声音、景别、运镜、人数、光照、物料去向。每一条下游都已经有
     一条在跑的规则在等（动作-工具-音效三联、ASMR 原声、Material & Spoil Balance），
     此前没有字段承接，规则只能对着空气执行；
  3. **细化** —— 状态要写量、可见结果与结束状态分工、主导工序是工序词不是整句。

盯得最紧的一条：全部判 warn，一条 error 都不能出。它们是质量下限不是契约下限，
判成硬伤会让所有存量阶梯在合成门口集体判死，而那些阶梯并没有变坏。
"""

import json
import os
import unittest

import prompt_pipeline as pp
from prompt_pipeline import reverse


def _craft_beat(bid='B01', **kw):
    """制作字段齐全、内容也干净的一拍。体检器对它应当一条都不出。"""
    beat = {
        'id': bid, 'start': 0.0, 'end': 4.0, 'stage': 'structural',
        'space': 'main room',
        'operation': 'board ceiling',
        'package_operations': ['cut', 'fit', 'fasten'],
        'visual_subject': 'a ceiling under boarding',
        'visible_details': [
            'grey plasterboard sheets stacked against the left wall',
            'raw sawn pine joists overhead in the middle bay',
            'black rubber-handled impact driver on the trestle at frame right',
        ],
        'visible_action': 'a worker lifts a sheet and drives screws along the joist',
        'visible_result': 'the sheet snaps flat and the driver clutch stops',
        'state_before': 'two of five bays boarded, the remaining three open to the joists',
        'state_after': 'three of five bays boarded, roof line flush across the boarded run',
        'persistent_traces': ['screw dimples along the joist line', 'sawdust smear on the trestle top'],
        'tool': 'cordless impact driver',
        'sfx': ['impact driver clutch chatter', 'board edge knocking against the joist'],
        'shot_scale': 'medium',
        'camera_move': 'static',
        # 2026-08-23：画面里有人就必须写他们的身体语言，否则交付出来的人一动不动。
        'cast_action': 'the worker crouches under the open bay, head tilted to sight the joist line',
        'worker_count': 1,
        'light_state': 'overcast midday through the roof opening, no cast shadows',
        'material_flow': 'sheets drawn from the stack at the left wall, offcuts bundled by the door',
        'workers_present': True,
        'source_event_ids': ['E01'],
        'evidence_frames': ['review_002.png'],
    }
    beat.update(kw)
    return beat


def _codes(violations):
    return {v['code'] for v in violations}


class TestCraftNormalization(unittest.TestCase):
    """值域归一：闭集收得住、人数认得出、认不出的一律丢弃。"""

    def test_shot_and_move_synonyms_collapse_into_the_closed_set(self):
        doc = {'beats': [_craft_beat(shot_scale='WIDE SHOT',
                                     camera_move='slow push in through the doorway')]}
        reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['shot_scale'], 'wide')
        self.assertEqual(doc['beats'][0]['camera_move'], 'push_in')

    def test_unrecognisable_enum_is_dropped_not_kept(self):
        """留一个歪值下去，等于让那一拍的机位由一句没人校对过的话决定。"""
        doc = {'beats': [_craft_beat(shot_scale='cinematic vibes', camera_move='????')]}
        reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['shot_scale'], '')
        self.assertEqual(doc['beats'][0]['camera_move'], '')

    def test_worker_count_reads_number_words(self):
        doc = {'beats': [_craft_beat(worker_count='one lone craftsman')]}
        reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['worker_count'], 1)
        self.assertIs(doc['beats'][0]['workers_present'], True)

    def test_zero_workers_rewrites_the_boolean(self):
        doc = {'beats': [_craft_beat(worker_count=0, workers_present=True)]}
        reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['worker_count'], 0)
        self.assertIs(doc['beats'][0]['workers_present'], False)

    def test_unparseable_count_is_dropped_never_defaulted_to_zero(self):
        """0 是「这一拍清场」这个真实断言，跟「没写」不是一回事。"""
        doc = {'beats': [_craft_beat(worker_count='several', workers_present=True)]}
        reverse.normalize_beat_keys(doc)
        self.assertNotIn('worker_count', doc['beats'][0])
        self.assertIs(doc['beats'][0]['workers_present'], True)

    def test_sfx_accepts_a_bare_string(self):
        doc = {'beats': [_craft_beat(sfx='hydraulic whine')]}
        reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['sfx'], ['hydraulic whine'])

    def test_dead_asmr_key_is_carried_into_the_live_one(self):
        """`audio_asmr_cues` 是变异线一直在写、全链路一处也没在读的键。"""
        beat = _craft_beat()
        beat.pop('sfx')
        beat['audio_asmr_cues'] = ['mallet taps', 'gravel crunch']
        doc = {'beats': [beat]}
        moved = reverse.normalize_beat_keys(doc)
        self.assertEqual(doc['beats'][0]['sfx'], ['mallet taps', 'gravel crunch'])
        self.assertIn(('audio_asmr_cues', 'sfx'),
                      [(m['from'], m['to']) for m in moved])


class TestCraftValidation(unittest.TestCase):
    """单拍体检。全是 warn，且同一种毛病只出一条。"""

    def test_a_clean_beat_raises_nothing(self):
        self.assertEqual(reverse._validate_beat_craft([_craft_beat()]), [])

    def test_every_finding_is_a_warning(self):
        """存量阶梯不能因为这组判据在合成门口集体判死。"""
        bare = _craft_beat()
        for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'worker_count',
                    'light_state', 'material_flow'):
            bare.pop(key, None)
        bare.update(operation='lowering the bus into the excavation pit',
                    macro_environment=['rectangular pit excavated in dark clay'],
                    state_before='the room looks unfinished',
                    persistent_traces=['fallen autumn leaves', 'moss on the sill'])
        violations = reverse._validate_beat_craft([bare])
        self.assertTrue(violations)
        self.assertEqual({v['level'] for v in violations}, {'warn'})

    def test_work_product_in_macro_environment(self):
        beat = _craft_beat(macro_environment=[
            'dense autumn forest canopy under clear sky',
            'rectangular deep pit excavated in layered dark soil'])
        self.assertIn('macro_env_work_product', _codes(reverse._validate_beat_craft([beat])))

    def test_macro_environment_that_only_describes_the_place_is_fine(self):
        beat = _craft_beat(macro_environment=['dense autumn forest canopy under clear sky'])
        self.assertNotIn('macro_env_work_product', _codes(reverse._validate_beat_craft([beat])))

    def test_detail_that_restates_the_macro_environment(self):
        """换了措辞的同一件东西——实词交集抓不到，环境物概念抓得到。"""
        beat = _craft_beat(
            macro_environment=['dense autumn deciduous forest canopy with golden yellow foliage'],
            visible_details=['gloss yellow enamel bus bodywork with black rub rails at frame left',
                             'golden orange beech and oak leaf litter blanketing the ground',
                             'twin-leg steel wire sling on the roof bracket overhead'])
        self.assertIn('detail_repeats_context', _codes(reverse._validate_beat_craft([beat])))

    def test_trace_that_is_really_a_pre_existing_environment_feature(self):
        beat = _craft_beat(persistent_traces=['screw dimples along the joist line',
                                              'scattered leaves at the pit rim'])
        self.assertIn('trace_without_mark', _codes(reverse._validate_beat_craft([beat])))

    def test_state_without_a_quantity(self):
        beat = _craft_beat(state_before='the bus hangs above the open trench')
        self.assertIn('state_without_quantity', _codes(reverse._validate_beat_craft([beat])))

    def test_state_quantified_in_words_counts(self):
        """「悬空一个车身高」是量，跟「1.5 米」一样算数。"""
        beat = _craft_beat(state_before='trench open, bus hanging one body-height above the rim')
        self.assertNotIn('state_without_quantity', _codes(reverse._validate_beat_craft([beat])))

    def test_result_that_is_the_after_state_written_twice(self):
        beat = _craft_beat(
            visible_result='the bus body settles fully into the trench cavity below grade',
            state_after='the bus body is fully seated in the trench cavity below grade')
        self.assertIn('result_echoes_state', _codes(reverse._validate_beat_craft([beat])))

    def test_operation_written_as_a_clause(self):
        beat = _craft_beat(operation='lowering the yellow bus into the excavation pit')
        self.assertIn('operation_not_a_token', _codes(reverse._validate_beat_craft([beat])))

    def test_transition_beat_is_exempt_from_operation_and_material_checks(self):
        """过门拍不干活，工具与物料两栏本来就该空。"""
        beat = _craft_beat(stage='transition', operation='threshold',
                           package_operations=['threshold'])
        beat.pop('tool')
        beat.pop('material_flow')
        codes = _codes(reverse._validate_beat_craft([beat]))
        self.assertNotIn('missing_craft_fields', codes)
        self.assertNotIn('operation_not_a_token', codes)

    def test_missing_craft_fields_collapse_into_one_warning(self):
        """七种各报一条、每条再列十四个拍号，卡点就成了一面没人读的红墙。"""
        beats = []
        for i in range(1, 5):
            beat = _craft_beat(f'B0{i}')
            for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'worker_count',
                        'light_state', 'material_flow'):
                beat.pop(key, None)
            beats.append(beat)
        gaps = [v for v in reverse._validate_beat_craft(beats)
                if v['code'] == 'missing_craft_fields']
        self.assertEqual(len(gaps), 1)
        for label in ('主导工具', '本拍声音', '景别', '运镜', '工人数', '光照时段', '物料去向'):
            self.assertIn(label, gaps[0]['message'])
        self.assertIn('B04', gaps[0]['message'])

    def test_craft_check_is_wired_into_validate_beats(self):
        beat = _craft_beat(operation='lowering the yellow bus into the excavation pit')
        doc = {'video_duration_sec': 10.0, 'beats': [beat], 'banned_elements': []}
        overview = {'media_metadata': {'duration_sec': 10.0},
                    'change_events': [{'event_id': 'E01', 'start': 0.0, 'peak': 2.0, 'end': 4.0,
                                       'evidence_frames': ['review_002.png']}],
                    'review_sampling': {'frames': [
                        {'index': 2, 'timestamp': 2.0,
                         'frame_path': '/job/review_frames/review_002.png'}]},
                    'scenes': []}
        self.assertIn('operation_not_a_token', _codes(reverse.validate_beats(doc, overview)))


class TestCraftTransmission(unittest.TestCase):
    """字段真正的出口只有一个：beats_to_dimensions → 清单条目 → 规划提示词。"""

    def test_beats_to_dimensions_carries_every_craft_field(self):
        doc = {'beats': [_craft_beat()], 'banned_elements': []}
        entry = reverse.beats_to_dimensions(doc)['beat_outline'][0]
        self.assertEqual(entry['tool'], 'cordless impact driver')
        self.assertEqual(entry['sfx'][0], 'impact driver clutch chatter')
        self.assertEqual(entry['shot_scale'], 'medium')
        self.assertEqual(entry['camera_move'], 'static')
        self.assertEqual(entry['crew'], '1')
        self.assertIn('overcast midday', entry['light'])
        self.assertIn('offcuts bundled', entry['flow'])

    def test_zero_crew_survives_the_trip(self):
        """写成字符串就是为了这一条：0 传成整数会被 `if value` 判成空。"""
        doc = {'beats': [_craft_beat(worker_count=0, workers_present=False)],
               'banned_elements': []}
        entry = reverse.beats_to_dimensions(doc)['beat_outline'][0]
        self.assertEqual(entry['crew'], '0')

    def test_normalized_entries_keep_the_craft_keys(self):
        doc = {'beats': [_craft_beat()], 'banned_elements': []}
        outline = reverse.beats_to_dimensions(doc)['beat_outline']
        normalized = pp._outline_normalized_entries(outline)[0]
        for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'crew', 'light', 'flow'):
            self.assertIn(key, normalized, f'{key} 在归一化时被丢掉了')

    def test_crossing_beat_macro_environment_reaches_the_planner(self):
        """首拍那份走文档级 initial_macro_environment，只有过门拍是哑的——不容易看出来。"""
        first = _craft_beat('B01', space='outside')
        crossing = _craft_beat('B02', space='inner room', stage='transition',
                               operation='threshold', package_operations=['threshold'],
                               macro_environment=['low vaulted rock chamber lit by one opening'])
        doc = {'beats': [first, crossing], 'banned_elements': []}
        outline = reverse.beats_to_dimensions(doc)['beat_outline']
        normalized = pp._outline_normalized_entries(outline)
        self.assertEqual(normalized[1]['macro_environment'],
                         ['low vaulted rock chamber lit by one opening'])

    def test_plan_block_renders_and_binds_the_craft_fields(self):
        doc = {'beats': [_craft_beat()], 'banned_elements': []}
        outline = reverse.beats_to_dimensions(doc)['beat_outline']
        _plan, block = pp.build_outline_plan_block(outline, 1)
        for tag in ('SHOT: medium / static', 'TOOL: cordless impact driver',
                    'SFX: impact driver clutch chatter', 'LIGHT: overcast midday',
                    'CREW: 1', 'MATERIAL FLOW: sheets drawn'):
            self.assertIn(tag, block, f'清单里没渲染出 {tag}')
        for rule in ('SHOT:', 'TOOL:', 'SFX:', 'LIGHT:', 'CREW:', 'MATERIAL FLOW:'):
            self.assertIn(rule, block)
        self.assertIn('no music, no score', block)

    def test_a_card_without_craft_fields_gains_nothing(self):
        """老卡片/原创线凭空看见一条「照抄清单里的 SFX」，只会让规划器自己编几条。"""
        _plan, block = pp.build_outline_plan_block(
            [{'text': 'clear the floor', 'op': 'clear'}], 1)
        for tag in ('SHOT:', 'TOOL:', 'SFX:', 'LIGHT:', 'CREW:', 'MATERIAL FLOW:'):
            self.assertNotIn(tag, block)


class TestCraftContract(unittest.TestCase):
    """契约文件与变异线的口径。"""

    def _schema(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'skills', 'gemini-omni-restoration-composer', 'references',
                            'timelapse-beats.schema.json')
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def test_new_fields_are_declared_but_never_required(self):
        beat_schema = self._schema()['definitions']['beat']
        for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'worker_count',
                    'light_state', 'material_flow'):
            self.assertIn(key, beat_schema['properties'], f'{key} 没写进契约')
            self.assertNotIn(key, beat_schema['required'],
                             f'{key} 一旦必填，所有存量阶梯就在合成门口集体判死')

    def test_schema_enums_match_the_code(self):
        props = self._schema()['definitions']['beat']['properties']
        self.assertEqual(tuple(props['shot_scale']['enum']), reverse.SHOT_SCALES)
        self.assertEqual(tuple(props['camera_move']['enum']), reverse.CAMERA_MOVES)

    def test_variant_rewrites_content_fields_and_inherits_the_camera(self):
        """工具/音效/物料随载体变；景别、运镜、人数属于节奏骨架，原样继承。"""
        rewrite_clause = reverse._MUTATE_SYSTEM.split('WHAT YOU REWRITE')[1]
        self.assertIn('tool, sfx, material_flow', rewrite_clause)
        self.assertIn('shot_scale, camera_move and worker_count', rewrite_clause)


if __name__ == '__main__':
    unittest.main()


class TestCastActionValidation(unittest.TestCase):
    """画面里有人却没写身体语言 —— 2026-08-23 用户实测：成片里人物完全静止。"""

    def test_a_beat_with_people_but_no_body_language_is_flagged(self):
        beat = _craft_beat()
        beat.pop('cast_action')
        findings = reverse._validate_beat_craft([beat])
        codes = [f['code'] for f in findings]
        self.assertIn('missing_cast_action', codes)
        self.assertEqual([f['level'] for f in findings], ['warn'] * len(findings))

    def test_a_sterile_beat_is_not_nagged(self):
        """清场帧本来就没人可写；对它报缺失只会把卡点变成一面红墙。"""
        beat = _craft_beat()
        beat.pop('cast_action')
        beat.update(workers_present=False, worker_count=0)
        codes = [f['code'] for f in reverse._validate_beat_craft([beat])]
        self.assertNotIn('missing_cast_action', codes)

    def test_the_finding_aggregates_beat_ids(self):
        beats = []
        for bid in ('B01', 'B02', 'B03'):
            beat = _craft_beat(bid)
            beat.pop('cast_action')
            beats.append(beat)
        findings = [f for f in reverse._validate_beat_craft(beats)
                    if f['code'] == 'missing_cast_action']
        self.assertEqual(len(findings), 1, '同一种毛病只出一条，拍号列进正文')
        for bid in ('B01', 'B02', 'B03'):
            self.assertIn(bid, findings[0]['message'])


class TestEverythingAliveIsCovered(unittest.TestCase):
    """「活物」不等于「人」。

    2026-08-23 用户追问：不止人偶吧，只要有活物都会分析吗。当时的答案是「否」——
    cast_action 只写了 people or figurines，动物没覆盖；而画面里另一半会动的东西
    （溪水、烟、火苗、风吹树冠）连字段都没有，它们不产生任何一拍的 delta，因此在
    以「变化」为骨架的整条反推链路里没有任何落脚点，交付出来的背景就是静止贴图。
    """

    def test_the_cast_field_asks_for_animals_too(self):
        spec = reverse._PASS_B_SYSTEM
        self.assertIn('an animal', spec)
        self.assertIn('EVERY living thing in frame', spec)

    def test_ambient_motion_is_asked_for_once_per_film_not_per_beat(self):
        """按拍问会把它变成十几行重复内容，还挤占 Pass B 最容易被截断的那段输出。"""
        spec = reverse._PASS_B_SYSTEM
        self.assertIn('ambient_motion', spec)
        self.assertIn('for the WHOLE film (not per beat)', spec)

    def test_pass_b_motion_is_merged_into_scene_constants(self):
        doc = {'ambient_motion': ['the stream runs past the stump',
                                  'smoke drifts from the tin chimney']}
        reverse.attach_scene_constants(doc, [])
        self.assertEqual(doc['scene_constants']['motion'],
                         ['the stream runs past the stump', 'smoke drifts from the tin chimney'])

    def test_a_user_edited_constants_block_is_not_overwritten(self):
        """卡点上删空过的那份必须留住——判键在不在，不判值真不真。"""
        doc = {'scene_constants': {'motion': []}, 'ambient_motion': ['smoke drifts']}
        reverse.attach_scene_constants(doc, [])
        self.assertEqual(doc['scene_constants'], {'motion': []})

    def test_motion_reaches_the_prompt_with_its_own_verb(self):
        lines = reverse.scene_constants_lines(
            {'materials': ['mossy concrete'], 'motion': ['the stream runs past the stump']})
        joined = '\n'.join(lines)
        self.assertIn('always-present materials', joined)
        # 「一直在」和「一直在动」不能共用一个措辞，否则运动项会被写成静物
        self.assertIn('never stops moving', joined)

    def test_the_composer_demands_the_motion_keeps_moving(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'motion': ['the stream runs past the stump']},
        }})
        block = composer.scene_constants_block()
        self.assertIn('the stream runs past the stump', block)
        self.assertIn('keep moving', block)
        self.assertIn('EVERY video clip', block)

    def test_without_motion_the_block_is_unchanged(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'materials': ['mossy concrete']},
        }})
        block = composer.scene_constants_block()
        self.assertIn('mossy concrete', block)
        self.assertNotIn('keep moving', block)
