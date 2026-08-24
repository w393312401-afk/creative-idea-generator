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
        # 2026-08-25：拍摄角度两栏。俯仰与方位是两根独立的轴，两栏都要标。
        'camera_angle': 'low_angle',
        'camera_bearing': 'three_quarter',
        # 2026-08-25：焦段、构图、时间处理。
        'lens_feel': 'ultra_wide',
        'subject_placement': 'the open bay sits centred, filling about half of frame height',
        'time_treatment': 'timelapse',
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
        for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'camera_angle',
                    'camera_bearing', 'lens_feel', 'subject_placement', 'time_treatment',
                    'worker_count', 'light_state', 'material_flow'):
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
        self.assertEqual(entry['camera_angle'], 'low_angle')
        self.assertEqual(entry['camera_bearing'], 'three_quarter')
        self.assertEqual(entry['lens_feel'], 'ultra_wide')
        self.assertIn('centred', entry['placement'])
        self.assertEqual(entry['time_treatment'], 'timelapse')
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
        for key in ('tool', 'sfx', 'shot_scale', 'camera_move', 'camera_angle',
                    'camera_bearing', 'lens_feel', 'placement', 'time_treatment',
                    'crew', 'light', 'flow'):
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
        for tag in ('SHOT: medium / static', 'ANGLE: low_angle / three_quarter / ultra_wide',
                    'PLACEMENT: the open bay sits centred', 'TIME: timelapse',
                    'TOOL: cordless impact driver',
                    'SFX: impact driver clutch chatter', 'LIGHT: overcast midday',
                    'CREW: 1', 'MATERIAL FLOW: sheets drawn'):
            self.assertIn(tag, block, f'清单里没渲染出 {tag}')
        for rule in ('SHOT:', 'ANGLE:', 'PLACEMENT:', 'TIME:', 'TOOL:', 'SFX:', 'LIGHT:',
                     'CREW:', 'MATERIAL FLOW:'):
            self.assertIn(rule, block)
        self.assertIn('no music, no score', block)

    def test_a_card_without_craft_fields_gains_nothing(self):
        """老卡片/原创线凭空看见一条「照抄清单里的 SFX」，只会让规划器自己编几条。"""
        _plan, block = pp.build_outline_plan_block(
            [{'text': 'clear the floor', 'op': 'clear'}], 1)
        for tag in ('SHOT:', 'ANGLE:', 'PLACEMENT:', 'TIME:', 'TOOL:', 'SFX:', 'LIGHT:',
                    'CREW:', 'MATERIAL FLOW:'):
            self.assertNotIn(tag, block)


class TestCameraAngle(unittest.TestCase):
    """拍摄角度（2026-08-25）。俯仰与方位是两根独立的轴，两栏都要活着走完全程。"""

    def test_pass_a_reads_the_angle_off_each_frame(self):
        self.assertIn('camera_angle', reverse._PASS_A_SYSTEM)
        self.assertIn('camera_bearing', reverse._PASS_A_SYSTEM)
        # 猜一个角度比留空更糟：下游会把它照抄进每一帧
        self.assertIn('a guessed angle is worse than a blank one', reverse._PASS_A_SYSTEM)
        self.assertNotIn(reverse.PASS_A_PROMPT_VERSION, ('v3', 'v4'))

    def test_pass_a_parsing_coerces_free_text_to_the_closed_set(self):
        parsed = reverse._parse_facts_array(
            '[{"frame": "review_001.png", "camera_angle": "low angle shot",'
            ' "camera_bearing": "from the side"}]', ['review_001.png'])
        self.assertEqual(parsed['review_001.png']['camera_angle'], 'low_angle')
        self.assertEqual(parsed['review_001.png']['camera_bearing'], 'side')

    def test_an_unreadable_angle_is_dropped_not_guessed(self):
        parsed = reverse._parse_facts_array(
            '[{"frame": "review_001.png", "camera_angle": "cinematic vibes"}]',
            ['review_001.png'])
        self.assertEqual(parsed['review_001.png']['camera_angle'], '')

    def test_beat_level_free_text_is_coerced_too(self):
        doc = {'beats': [_craft_beat(camera_angle='WORMS EYE',
                                     camera_bearing='over the shoulder')]}
        reverse.normalize_beat_craft_fields(doc)
        self.assertEqual(doc['beats'][0]['camera_angle'], 'worm_eye')
        self.assertEqual(doc['beats'][0]['camera_bearing'], 'rear_three_quarter')

    def test_a_missing_angle_is_flagged_on_the_review_card(self):
        doc = {'beats': [_craft_beat(camera_angle='', camera_bearing='')]}
        violations = reverse._validate_beat_craft(doc['beats'])
        self.assertIn('missing_craft_fields', _codes(violations))
        self.assertIn('拍摄角度', violations[0]['message'])

    def test_the_observed_angle_reaches_the_packet_camera_sentence(self):
        """IMAGE 的开场句是**族级** camera_dna 的逐字复述，逐拍再写一句角度只会被顶回去
        （worker_scale_percent 当年就是这么空转的）。角度必须在写 camera_dna 那一刻进去。"""
        brief = {'beat_outline': [
            {'text': 'dig the trench', 'space': 'wooded slope outside',
             'camera_angle': 'low_angle', 'camera_bearing': 'side'},
            {'text': 'clear the floor', 'space': 'main room',
             'camera_angle': 'bird_eye', 'camera_bearing': 'front'},
        ]}
        rule = pp.observed_camera_angle_packet_rule(brief)
        self.assertIn('wooded slope outside', rule)
        self.assertIn('below the subject looking up', rule)
        self.assertIn('main room', rule)
        self.assertIn('directly overhead', rule)
        # 「绝不用航拍/高角度」那条默认规则不能压过实际观测
        self.assertIn('OVERRIDES', rule)

    def test_no_observed_angle_injects_nothing(self):
        """老任务/老断点/手输主题一律保持改动前的行为。"""
        self.assertEqual(pp.observed_camera_angle_packet_rule(
            {'beat_outline': [{'text': 'clear the floor'}]}), "")
        self.assertEqual(pp.observed_camera_angle_packet_rule({}), "")

    def test_the_dominant_pair_wins_per_space(self):
        brief = {'beat_outline': [
            {'text': 'a', 'space': 'outside', 'camera_angle': 'eye_level', 'camera_bearing': 'front'},
            {'text': 'b', 'space': 'outside', 'camera_angle': 'eye_level', 'camera_bearing': 'front'},
            {'text': 'c', 'space': 'outside', 'camera_angle': 'worm_eye', 'camera_bearing': 'side'},
        ]}
        self.assertEqual(pp.observed_camera_angles_by_space(brief),
                         {'outside': {'angle': 'eye_level', 'bearing': 'front',
                                      'lens': '', 'placement': ''}})

    def test_one_space_filmed_from_two_angles_is_surfaced_not_swallowed(self):
        """族级机位句一个空间只落得下一个角度。少数派那几拍的图会按多数派出——
        这不是 bug，但绝不能静默发生：卡片上写着鸟瞰、出来的图却是平视。"""
        beats = [
            _craft_beat('B01', space='main room', camera_angle='eye_level', camera_bearing='front'),
            _craft_beat('B02', space='main room', camera_angle='eye_level', camera_bearing='front'),
            _craft_beat('B03', space='main room', camera_angle='bird_eye', camera_bearing='front'),
        ]
        violations = reverse._validate_camera_angle_consistency(beats)
        self.assertEqual(_codes(violations), {'mixed_camera_angle'})
        msg = violations[0]['message']
        self.assertIn('main room', msg)
        self.assertIn('B03', msg)
        self.assertIn('鸟瞰', msg)
        self.assertNotIn('B01', msg)          # 多数派不点名
        self.assertEqual(violations[0]['level'], 'warn')   # 不拦，只摊开

    def test_one_angle_per_space_raises_nothing(self):
        beats = [
            _craft_beat('B01', space='outside', camera_angle='low_angle', camera_bearing='side'),
            _craft_beat('B02', space='outside', camera_angle='low_angle', camera_bearing='side'),
            _craft_beat('B03', space='main room', camera_angle='bird_eye', camera_bearing='front'),
        ]
        self.assertEqual(reverse._validate_camera_angle_consistency(beats), [])

    def test_unlabelled_angles_raise_nothing(self):
        """老任务一栏都没标，不该凭空多出一条告警。"""
        beats = [_craft_beat('B01', camera_angle='', camera_bearing=''),
                 _craft_beat('B02', camera_angle='', camera_bearing='')]
        self.assertEqual(reverse._validate_camera_angle_consistency(beats), [])

    def test_the_beat_contract_binds_the_angle_to_the_video(self):
        beat = {'observed_craft': {'camera_angle': 'worm_eye', 'camera_bearing': 'side'}}
        block = pp.observed_craft_directive(beat)
        self.assertIn('ANGLE (observed): worm_eye / side', block)
        self.assertIn('on or near the ground', block)
        self.assertIn('never drift to a different height', block)


class TestLensPlacementAndTime(unittest.TestCase):
    """2026-08-25 的三条逐拍新栏：焦段感、主体构图、时间处理。"""

    def test_pass_a_reads_lens_and_composition_off_each_frame(self):
        spec = reverse._PASS_A_SYSTEM
        self.assertIn('lens_feel', spec)
        self.assertIn('subject_placement', spec)
        # 焦段 ≠ 景别：广角站远和长焦站近都能拍到「整面墙」
        self.assertIn('both give you "the whole wall"', spec)
        # 分数写单词：数字和百分号会被图像模型当文字画进画面
        self.assertIn('never digits or percent signs', spec)
        self.assertNotIn(reverse.PASS_A_PROMPT_VERSION, ('v3', 'v4', 'v5'))

    def test_time_treatment_is_a_pass_b_reading_not_a_frame_reading(self):
        """单帧看不出时间快慢，它只能由帧序列读——所以只在 Pass B 里问。"""
        self.assertNotIn('time_treatment', reverse._PASS_A_SYSTEM)
        self.assertIn('time_treatment', reverse._PASS_B_SYSTEM)
        self.assertIn('almost always real_time', reverse._PASS_B_SYSTEM)

    def test_free_text_is_coerced_into_the_closed_sets(self):
        parsed = reverse._parse_facts_array(
            '[{"frame": "review_001.png", "lens_feel": "14mm ultra-wide"}]', ['review_001.png'])
        self.assertEqual(parsed['review_001.png']['lens_feel'], 'ultra_wide')
        doc = {'beats': [_craft_beat(lens_feel='85mm telephoto', time_treatment='sped up')]}
        reverse.normalize_beat_craft_fields(doc)
        self.assertEqual(doc['beats'][0]['lens_feel'], 'tele')
        self.assertEqual(doc['beats'][0]['time_treatment'], 'timelapse')

    def test_an_unreadable_lens_is_dropped_not_guessed(self):
        doc = {'beats': [_craft_beat(lens_feel='cinematic glass')]}
        reverse.normalize_beat_craft_fields(doc)
        self.assertEqual(doc['beats'][0]['lens_feel'], '')

    def test_composition_reaches_the_packet_where_the_anchor_numbers_are_invented(self):
        """z_depth_scale 与地平线钉位此前从没在原片上量过——这是它们唯一的来源。"""
        rule = pp.observed_camera_angle_packet_rule({'beat_outline': [
            {'text': 'dig', 'space': 'outside', 'camera_angle': 'low_angle',
             'lens_feel': 'ultra_wide',
             'placement': 'the shell sits centred, filling about three fifths of frame height'},
        ]})
        self.assertIn('very wide lens', rule)
        self.assertIn('three fifths of frame height', rule)
        self.assertIn('z_depth_scale', rule)

    def test_real_time_beat_is_told_it_is_not_a_time_lapse(self):
        """所有拍默认被写成 continuous construction time-lapse，成品巡览拍因此被交付成快放。"""
        block = pp.observed_craft_directive({'observed_craft': {'time_treatment': 'real_time'}})
        self.assertIn('TIME (observed): real_time', block)
        self.assertIn('NOT a construction time-lapse', block)

    def test_composition_binds_the_still_frame(self):
        block = pp.observed_craft_directive(
            {'observed_craft': {'placement': 'the trench runs along the lower left'}})
        self.assertIn('COMPOSITION (measured off the film)', block)
        self.assertIn('lower left', block)


class TestAmbientSoundAndGrade(unittest.TestCase):
    """2026-08-25 的两条全片新栏：环境底噪与影调。形状与 motion / cast 一致。"""

    def test_pass_b_asks_for_both_once_per_film(self):
        spec = reverse._PASS_B_SYSTEM
        self.assertIn('ambient_sound', spec)
        self.assertIn('color_grade', spec)
        self.assertIn('audio counterpart of ambient_motion', spec)
        # 情绪词不是影调
        self.assertIn('Never write mood or genre words', spec)

    def test_both_merge_into_the_scene_constants_container(self):
        doc = {'ambient_sound': ['wind through the canopy', 'a stream below the slope'],
               'color_grade': 'cool overcast neutral grade, gentle contrast, lifted blacks'}
        reverse.attach_scene_constants(doc, [])
        sc = doc['scene_constants']
        self.assertEqual(sc['ambient_sound'],
                         ['wind through the canopy', 'a stream below the slope'])
        # 影调是一句话，但仍装进列表——这个容器全链路按列表读
        self.assertEqual(sc['grade'], ['cool overcast neutral grade, gentle contrast, lifted blacks'])

    def test_they_reach_the_prompt_lines_with_their_own_verbs(self):
        lines = reverse.scene_constants_lines(
            {'ambient_sound': ['wind through the canopy'], 'grade': ['cool neutral grade']})
        joined = '\n'.join(lines)
        self.assertIn('audible under every shot', joined)
        self.assertIn('identical in every frame', joined)

    def test_the_composer_pins_the_ambient_bed_and_the_grade(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {'scene_constants': {
            'ambient_sound': ['wind through the canopy'],
            'grade': ['cool overcast neutral grade'],
        }}})
        block = composer.scene_constants_block()
        self.assertIn('ambient bed', block)
        self.assertIn('sits UNDER that beat', block)   # 底噪不替换本拍的 sfx
        self.assertIn('EVERY IMAGE and EVERY VIDEO in this job, identically', block)
        self.assertIn('award-winning', block)          # 明确点名要禁的词

    def test_without_them_the_block_says_nothing(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'materials': ['mossy concrete']}}})
        block = composer.scene_constants_block()
        self.assertNotIn('ambient bed', block)
        self.assertNotIn('identically', block)

    def test_a_variant_keeps_the_grade_and_the_cast_but_not_the_venue(self):
        """四条变异轴没有一条动到出镜的人，也没有一条动到创作者的拍法；
        而材质、痕迹、环境底噪都是**这个场地**的属性，换了场地就不成立。"""
        from prompt_pipeline.mutate import generate_orthogonal_variant
        baseline = {
            'pipeline_id': 'job_base', 'video_duration_sec': 30.0,
            'scene_constants': {
                'materials': ['mossy concrete wall'],
                'ambient_sound': ['wind through the canopy'],
                'cast': ['the lone builder: red tee'],
                'grade': ['cool overcast neutral grade'],
            },
            'beats': [_craft_beat('B01')],
        }
        variant = generate_orthogonal_variant(baseline, preset='polar')
        self.assertEqual(variant['scene_constants'],
                         {'cast': ['the lone builder: red tee'],
                          'grade': ['cool overcast neutral grade']})


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
        self.assertEqual(tuple(props['camera_angle']['enum']), reverse.CAMERA_ANGLES)
        self.assertEqual(tuple(props['camera_bearing']['enum']), reverse.CAMERA_BEARINGS)
        self.assertEqual(tuple(props['lens_feel']['enum']), reverse.LENS_FEELS)
        self.assertEqual(tuple(props['time_treatment']['enum']), reverse.TIME_TREATMENTS)

    def test_variant_rewrites_content_fields_and_inherits_the_camera(self):
        """工具/音效/物料随载体变；景别、运镜、人数属于节奏骨架，原样继承。"""
        rewrite_clause = reverse._MUTATE_SYSTEM.split('WHAT YOU REWRITE')[1]
        self.assertIn('tool, sfx, material_flow', rewrite_clause)
        self.assertIn('camera_angle, camera_bearing, lens_feel, subject_placement, '
                      'camera_move, time_treatment and worker_count', rewrite_clause)


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

    # ── 全局人物识别项（2026-08-24）───────────────────────────────────────

    def test_pass_a_reads_who_is_in_frame_and_what_they_look_like(self):
        spec = reverse._PASS_A_SYSTEM
        self.assertIn('cast_appearance', spec)
        self.assertIn('ethnicity', spec)
        # 版本号必须跟着 Pass A 提示词一起走，否则旧缓存会被当成新结果复用
        self.assertNotEqual(reverse.PASS_A_PROMPT_VERSION, 'v3')

    def test_pass_b_asks_for_cast_identity_once_per_film(self):
        spec = reverse._PASS_B_SYSTEM
        self.assertIn('cast_identity', spec)
        self.assertIn('for the WHOLE film (not per beat)', spec)
        # 身份 ≠ 动作：动作是每一拍自己的 cast_action，别把两者写成一件事
        self.assertIn('IDENTITY, never ACTION', spec)

    def test_pass_a_cast_reading_survives_parsing(self):
        parsed = reverse._parse_facts_array(
            '[{"frame": "review_001.png", "cast_appearance": ["light-brown-skinned man, red tee"],'
            ' "workers_present": true}]', ['review_001.png'])
        self.assertEqual(parsed['review_001.png']['cast_appearance'],
                         ['light-brown-skinned man, red tee'])

    def test_the_composer_demands_the_cast_is_restated_every_frame(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'cast': ['the lone builder: light-brown-skinned man, red tee']},
        }})
        block = composer.scene_constants_block()
        self.assertIn('light-brown-skinned man, red tee', block)
        self.assertIn('FIXED IDENTITY', block)
        self.assertIn('EVERY IMAGE and EVERY VIDEO', block)
        self.assertIn('never re-cast', block)

    def test_without_a_cast_the_block_says_nothing_about_people(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'materials': ['mossy concrete']},
        }})
        self.assertNotIn('FIXED IDENTITY', composer.scene_constants_block())

    def test_without_motion_the_block_is_unchanged(self):
        from prompt_pipeline.composers import get_composer
        composer = get_composer('base')
        composer.begin_run({}, {'parsed_brief': {
            'scene_constants': {'materials': ['mossy concrete']},
        }})
        block = composer.scene_constants_block()
        self.assertIn('mossy concrete', block)
        self.assertNotIn('keep moving', block)
