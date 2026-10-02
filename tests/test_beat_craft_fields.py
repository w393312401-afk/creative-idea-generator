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
from prompt_pipeline import reference_context


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






class TestCraftTransmission(unittest.TestCase):
    """字段真正的出口只有一个：beats_to_dimensions → 清单条目 → 规划提示词。"""






    def test_a_card_without_craft_fields_gains_nothing(self):
        """老卡片/原创线凭空看见一条「照抄清单里的 SFX」，只会让规划器自己编几条。"""
        _plan, block = pp.build_outline_plan_block(
            [{'text': 'clear the floor', 'op': 'clear'}], 1)
        for tag in ('SHOT:', 'ANGLE:', 'PLACEMENT:', 'TIME:', 'TOOL:', 'SFX:', 'LIGHT:',
                    'CREW:', 'MATERIAL FLOW:'):
            self.assertNotIn(tag, block)


class TestCameraAngle(unittest.TestCase):
    """拍摄角度（2026-08-25）。俯仰与方位是两根独立的轴，两栏都要活着走完全程。"""






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
        # 观测到 bird_eye 就得写出"陡俯"，不能被稀释成 high_angle（reverse.py 把
        # top_down/overhead/aerial 全归一到 bird_eye，它是"原片确实是俯拍"的唯一出口）
        self.assertIn('steeply overhead looking down at the ground plane', rule)
        # 但同时要否掉正交地图化——这才是 45° 菱形/顶视平面图的病根
        self.assertIn('rather than a flat orthographic plan view', rule)
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




    def test_the_beat_contract_binds_the_angle_to_the_video(self):
        beat = {'observed_craft': {'camera_angle': 'worm_eye', 'camera_bearing': 'side'}}
        block = pp.observed_craft_directive(beat)
        self.assertIn('ANGLE (observed): worm_eye / side', block)
        self.assertIn('on or near the ground', block)
        self.assertIn('never drift to a different height', block)


class TestLensPlacementAndTime(unittest.TestCase):
    """2026-08-25 的三条逐拍新栏：焦段感、主体构图、时间处理。"""





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



    def test_they_reach_the_prompt_lines_with_their_own_verbs(self):
        lines = reference_context.scene_constants_lines(
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




if __name__ == '__main__':
    unittest.main()




class TestEverythingAliveIsCovered(unittest.TestCase):
    """「活物」不等于「人」。

    2026-08-23 用户追问：不止人偶吧，只要有活物都会分析吗。当时的答案是「否」——
    cast_action 只写了 people or figurines，动物没覆盖；而画面里另一半会动的东西
    （溪水、烟、火苗、风吹树冠）连字段都没有，它们不产生任何一拍的 delta，因此在
    以「变化」为骨架的整条反推链路里没有任何落脚点，交付出来的背景就是静止贴图。
    """





    def test_motion_reaches_the_prompt_with_its_own_verb(self):
        lines = reference_context.scene_constants_lines(
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


class ObservedCameraSetupsTest(unittest.TestCase):
    """原片量到几个机位就发几句机位（2026-08-25）。

    改动前：机位句按**空间**发一句，同一个空间里换过的机位被多数派投票投掉，少数派那几拍
    的图按多数派的角度出——用户看着卡片上写的「B07 鸟瞰」，出来的图却是平视，整条链路一声
    不吭。现在分组键是 (空间, 角度, 方位, 焦段)，几何锁从一把变成几把，每一把仍然逐字复述。
    """

    def _brief(self):
        return {'beat_outline': [
            {'text': 'dig', 'space': 'outside', 'camera_angle': 'high_angle',
             'camera_bearing': 'three_quarter', 'lens_feel': 'wide',
             'placement': 'the trench runs along the lower left'},
            {'text': 'tamp', 'space': 'outside', 'camera_angle': 'high_angle',
             'camera_bearing': 'three_quarter', 'lens_feel': 'wide',
             'placement': 'the trench runs along the lower left'},
            {'text': 'nail', 'space': 'outside', 'camera_angle': 'low_angle',
             'camera_bearing': 'side', 'lens_feel': 'normal',
             'placement': 'the wall fills three fifths of frame height'},
        ]}

    def test_the_same_space_filmed_twice_yields_two_setups(self):
        setups = pp.observed_camera_setups(self._brief())
        self.assertEqual([s['id'] for s in setups], ['SETUP_1', 'SETUP_2'])
        self.assertEqual(setups[0]['beats'], [1, 2])
        self.assertEqual(setups[1]['beats'], [3])
        self.assertEqual(setups[1]['angle'], 'low_angle')

    def test_every_setup_gets_its_own_sentence_requested_from_the_packet(self):
        rule = pp.observed_camera_angle_packet_rule(self._brief())
        self.assertIn('"SETUP_1"', rule)
        self.assertIn('"SETUP_2"', rule)
        self.assertIn('"camera_setups"', rule)
        # 同空间的两个机位不能被写成两个房间
        self.assertIn('never let them describe two different rooms', rule)

    def test_the_setup_id_is_pinned_to_the_beat_and_picks_the_sentence(self):
        brief = self._brief()
        ladder = [{'index': i} for i in range(3)]
        self.assertEqual(pp.apply_observed_camera_setups(ladder, brief), 3)
        self.assertEqual([b['camera_setup_id'] for b in ladder],
                         ['SETUP_1', 'SETUP_1', 'SETUP_2'])
        packet = {'camera_dna': 'FAMILY.',
                  'camera_setups': {'SETUP_1': 'OVERHEAD SENTENCE.',
                                    'SETUP_2': 'LOW ANGLE SENTENCE.'}}
        picked = [pp.select_camera_dna(b, packet['camera_dna'], packet=packet, family='exterior')
                  for b in ladder]
        self.assertEqual(picked, ['OVERHEAD SENTENCE.', 'OVERHEAD SENTENCE.',
                                  'LOW ANGLE SENTENCE.'])

    def test_a_length_mismatch_disables_the_whole_thing(self):
        """规划四轮全灭退回兜底梯子时，按下标硬贴只会把 A 拍的机位贴到 B 拍上。"""
        ladder = [{'index': 0}, {'index': 1}]
        self.assertEqual(pp.apply_observed_camera_setups(ladder, self._brief()), 0)
        self.assertNotIn('camera_setup_id', ladder[0])

    def test_no_reading_and_no_packet_key_fall_back_verbatim(self):
        """原创单、老 job、老断点的 packet 里没有 camera_setups —— 逐字回落到族级机位句。"""
        self.assertEqual(pp.observed_camera_setups({}), [])
        self.assertEqual(pp.apply_observed_camera_setups([{'index': 0}], {}), 0)
        self.assertEqual(
            pp.select_camera_dna({'camera_setup_id': 'SETUP_1'}, 'FAMILY.',
                                 packet={'camera_dna': 'FAMILY.'}, family='exterior'),
            'FAMILY.')
        self.assertEqual(
            pp.select_camera_dna({}, 'FAMILY.',
                                 packet={'camera_setups': {'SETUP_1': 'X.'}}, family='exterior'),
            'FAMILY.')

    def test_a_placement_alone_does_not_open_a_new_setup(self):
        """机位句写的是「机器站在哪」。拿一句构图开一个新机位 = 凭空多出一台机器。"""
        self.assertEqual(pp.observed_camera_setups({'beat_outline': [
            {'text': 'a', 'space': 'outside', 'placement': 'centred, three fifths high'}]}), [])

    def test_a_dict_shaped_sentence_is_flattened_not_shipped(self):
        """dict 形状的值会在 fix_camera_dna 的 .lower() 上当场中断整单。"""
        packet = pp.normalize_packet(
            {'camera_setups': {'SETUP_1': {'text': 'sentence'}, 'SETUP_2': ''}})
        self.assertIsInstance(packet['camera_setups']['SETUP_1'], str)
        self.assertNotIn('SETUP_2', packet['camera_setups'])


class ObservedShotScaleLadderTest(unittest.TestCase):
    """镜头梯吃原片景别（2026-08-25）。

    改动前施工梯的主镜/切回镜写死是远景，于是原片整拍拍在特写上时，切点表、逐镜职责、
    镜头名审计、定向回炉四处一致地要求写 wide working shot——观测到的 shot_scale 只以
    一句劝导文字下发，软的必然输给硬的。
    """

    def setUp(self):
        from prompt_pipeline.composers import omni
        self.omni = omni
        self.four = omni._CONSTRUCTION_LADDERS[4]

    def _phrases(self, ladder):
        return [r.phrase for r in ladder]

    def test_an_unknown_or_wide_scale_changes_nothing(self):
        for scale in (None, '', 'wide', 'bogus'):
            self.assertEqual(self.omni.apply_observed_scale(self.four, scale), self.four)

    def test_a_close_beat_is_not_pulled_out_to_a_wide_master(self):
        ladder = self.omni.apply_observed_scale(self.four, 'close')
        self.assertEqual(self._phrases(ladder)[0], 'a close working shot')
        self.assertEqual(self._phrases(ladder)[-1], 'a returning close shot')
        # 主镜已经很紧时插入镜必须更紧，否则两镜写成同一个画面、四镜梯塌成两镜
        self.assertEqual(self._phrases(ladder)[1], 'a macro detail insert')

    def test_the_rung_keys_survive_the_rewrite(self):
        """切点表、职责文案、缺镜头审计、越界景别审计、兜底稿全部按 rung.key 取值。"""
        for scale in ('medium', 'close', 'extreme_close', 'extreme_wide'):
            ladder = self.omni.apply_observed_scale(self.four, scale)
            self.assertEqual([r.key for r in ladder], [r.key for r in self.four])

    def test_the_timeline_and_the_audit_read_the_same_ladder(self):
        """两边取梯口径不一致 = 每拍必判违规、每拍烧一轮回炉，报的还是「缺镜头」。"""
        ladder = self.omni.apply_observed_scale(self.four, 'medium')
        timeline = self.omni.timeline_sentence(8, ladder)
        self.assertIn('a medium working shot', timeline)
        self.assertEqual(self.omni._missing_shot_rungs(timeline, ladder), [])

    def test_the_traversal_and_reward_ladders_are_untouched(self):
        """过门梯/兑现梯的三个工位由职责定，景别是那份职责的一部分。"""
        for ladder in (self.omni._TRAVERSAL_LADDER, self.omni._REWARD_LADDER):
            self.assertEqual(self.omni.apply_observed_scale(ladder, 'close'), ladder)

    def test_the_scale_reaches_the_ladder_through_the_beat(self):
        self.assertEqual(pp.observed_shot_scale_of(
            {'observed_craft': {'shot_scale': 'extreme_close'}}), 'extreme_close')
        self.assertIsNone(pp.observed_shot_scale_of({}))
        self.assertIsNone(pp.observed_shot_scale_of(
            {'observed_craft': {'shot_scale': 'cinematic'}}))


class ObservedShotScaleSequenceTest(unittest.TestCase):
    """逐镜景别序列（2026-08-25）：原片一拍里远/全/中/近/特怎么切，交付就怎么切。

    改动前景别只有**逐拍**一个读数，镜头梯拿它去排三到四镜，中间那两个插入镜的景别是
    写死的——「原片是远景切特写再切中景」这件事整条链路一个字都接不住。
    """

    def setUp(self):
        from prompt_pipeline.composers import omni
        self.omni = omni
        self.four = omni._CONSTRUCTION_LADDERS[4]

    def _phrases(self, ladder):
        return [r.phrase for r in ladder]

    def test_the_middle_inserts_follow_the_reference_films_own_cuts(self):
        ladder = self.omni.apply_observed_scale(
            self.four, 'wide', shot_scales=['wide', 'extreme_close', 'medium', 'wide'])
        self.assertEqual(self._phrases(ladder)[1], 'an extreme close insert')
        self.assertEqual(self._phrases(ladder)[2], 'a medium insert')

    def test_the_first_and_last_shot_return_to_the_beats_main_scale(self):
        """两拍之间那张 IMAGE 同时属于两拍，景别只能有一个——首尾镜回到主景别，它才有唯一解。"""
        ladder = self.omni.apply_observed_scale(
            self.four, 'medium', shot_scales=['medium', 'extreme_wide', 'close', 'medium'])
        self.assertEqual(self._phrases(ladder)[0], 'a medium working shot')
        self.assertEqual(self._phrases(ladder)[-1], 'a returning medium shot')

    def test_an_insert_at_the_main_scale_keeps_the_tighter_default(self):
        """插入镜与主镜同框 = 两镜写成同一个画面，四镜梯当场塌成两镜。"""
        ladder = self.omni.apply_observed_scale(
            self.four, 'wide', shot_scales=['wide', 'wide', 'wide', 'wide'])
        self.assertEqual(self._phrases(ladder), self._phrases(self.four))

    def test_an_unreadable_middle_shot_keeps_its_default(self):
        ladder = self.omni.apply_observed_scale(
            self.four, 'wide', shot_scales=['wide', '', 'close', 'wide'])
        self.assertEqual(self._phrases(ladder)[1], 'a close-up insert')
        self.assertEqual(self._phrases(ladder)[2], 'a close insert')

    def test_a_sequence_shorter_than_the_ladder_changes_only_what_it_covers(self):
        """宁可少改一级，也不能把第三镜的景别贴到第二镜上。"""
        ladder = self.omni.apply_observed_scale(self.four, 'wide', shot_scales=['wide', 'medium'])
        self.assertEqual(self._phrases(ladder), self._phrases(self.four))

    def test_the_sequence_survives_the_trip_through_the_outline(self):
        self.assertEqual(
            pp.observed_shot_scale_sequence_of({'observed_craft': {'shot_scales': 'wide/?/close'}}),
            ['wide', '', 'close'])
        self.assertEqual(
            pp.observed_shot_scale_sequence_of({'observed_craft': {'shot_scales': '?/?'}}), [])
        self.assertEqual(pp.observed_shot_scale_sequence_of({}), [])




class SubjectPlacementBindsTheImageTest(unittest.TestCase):
    """构图逐拍绑 IMAGE（2026-08-25）。

    此前构图只有两个落点：packet 生成时按机位投一次票（于是它是整段常量，逐拍变不了），
    和逐拍契约里那句劝导文字（写手可写可不写，漏了没有任何东西会响）。
    """

    IMG = ("Static tripod shot, 24mm lens feel, camera height 1.6m, horizon locked at half "
           "frame height. A worker screws plasterboard to the joists.")
    READING = ('the shell sits centred, filling about three fifths of frame height, '
               'horizon across the upper third')

    def test_the_reading_is_injected_after_the_camera_sentence(self):
        out = pp.fix_subject_placement(self.IMG, self.READING)
        self.assertIn('three fifths of frame height', out)
        self.assertLess(out.index('Compose the frame'), out.index('A worker screws'))
        self.assertGreater(out.index('Compose the frame'), out.index('Static tripod'))

    def test_it_is_idempotent(self):
        once = pp.fix_subject_placement(self.IMG, self.READING)
        self.assertEqual(pp.fix_subject_placement(once, self.READING), once)

    def test_the_camera_sentences_own_horizon_wording_does_not_count_as_composition(self):
        """机位句里本来就写着 horizon / frame height。拿通用构图词表去判，注入器整个空转。"""
        self.assertIn('Compose the frame', pp.fix_subject_placement(self.IMG, self.READING))

    def test_a_paraphrase_already_in_the_body_is_not_doubled(self):
        body = ("Static tripod. The shell is centred, occupying three fifths of the frame "
                "height, the upper third carries the horizon.")
        self.assertEqual(pp.fix_subject_placement(body, self.READING), body)

    def test_no_reading_injects_nothing(self):
        self.assertEqual(pp.fix_subject_placement(self.IMG, ''), self.IMG)
        self.assertEqual(pp.fix_subject_placement('', self.READING), '')



class MicroTracesOnlyReachTheMiniatureChannelTest(unittest.TestCase):
    """微观痕迹只喂 mini 通道（2026-08-30，用户反馈）。

    micro_traces 是 Pass A 逐帧量的微距级痕迹（锯末、粉笔弹线、飞溅）。miniature 通道
    整条片子就是微距镜头贴着模型拍的，这一栏是它的主要画面内容；base / omni 交付全尺寸
    实景，同样的痕迹在它们的景别下一个像素都渲染不出来，进提示词只会挤掉真看得见的
    画面依据。

    盯的是三个注入点必须**同时**关：少关一处，那一处就静默恢复投喂，而这种恢复不会
    报任何错——和这一族字段此前每个洞的形状一模一样。同族的 MAT SPECS / FASTENING
    是工程规格，不受门控影响，非 mini 通道照样要。
    """

    ENTRY = {
        'text': '铺屋面瓦',
        'mat_specs': ['9mm OSB sheathing, raw matte face'],
        'fasteners': ['countersunk black drywall screws'],
        'micro': ['fine sawdust along the pencil cut-line'],
        'tool': 'impact driver',
    }
    FACT = {'subject': 'roof deck', 'materials': ['cedar shingle'],
            'micro_traces': ['chalk snap line on the subfloor']}

    def _plan_block(self, micro_traces):
        _plan, block = pp.build_outline_plan_block(
            [dict(self.ENTRY)], 1, multishot=True, micro_traces=micro_traces)
        return block

    def test_the_planning_prompt_drops_the_line_and_the_rule_together(self):
        """清单行与 FORENSIC DETAIL 里说 micro 的那半句是一对。只关行不关规则，
        规划器会照着一条「抄清单里的 MICRO TRACES」自己编几条填进去。"""
        mini = self._plan_block(True)
        self.assertIn('MICRO TRACES:', mini)
        self.assertIn('"MICRO TRACES"', mini)

        base = self._plan_block(False)
        self.assertNotIn('MICRO TRACES', base)
        self.assertIn('FORENSIC DETAIL', base)
        self.assertIn('MAT SPECS:', base)
        self.assertIn('FASTENING:', base)

    def test_the_craft_dict_never_carries_micro_off_channel(self):
        """在数据层丢而不是在渲染层跳过：observed_craft 一旦挂上就会随断点落盘，
        任何一个新读取点都会重新拿到它。"""
        mini = pp._craft_from_outline_entry(self.ENTRY, include_micro=True)
        self.assertIn('micro', mini)

        base = pp._craft_from_outline_entry(self.ENTRY, include_micro=False)
        self.assertNotIn('micro', base)
        self.assertIn('mat_specs', base)
        self.assertIn('fasteners', base)

    def test_the_compose_prompt_bullet_follows_the_data(self):
        base = pp._craft_from_outline_entry(self.ENTRY, include_micro=False)
        directive = pp.observed_craft_directive({'observed_craft': base})
        self.assertNotIn('MICRO TRACES', directive)
        self.assertIn('MATERIAL SPECS', directive)

    def test_apply_observed_craft_fields_honours_the_gate(self):
        ladder = [{'operation': 'board'}]
        pp.apply_observed_craft_fields(
            ladder, {'beat_outline': [dict(self.ENTRY)]}, include_micro=False)
        self.assertNotIn('micro', pp.observed_craft_of(ladder[0]))

        ladder = [{'operation': 'board'}]
        pp.apply_observed_craft_fields(
            ladder, {'beat_outline': [dict(self.ENTRY)]}, include_micro=True)
        self.assertIn('micro', pp.observed_craft_of(ladder[0]))


    def test_the_gate_reads_the_profile_and_nothing_else(self):
        """通道就是 profile。按题材关键词再猜一遍就是第二个真相源。"""
        self.assertTrue(pp.micro_traces_channel_enabled({'skillProfile': 'miniature'}))
        for profile in ('base', 'omni'):
            self.assertFalse(pp.micro_traces_channel_enabled({'skillProfile': profile}))
        # 题材写着 miniature 但通道是 base：仍然不发。
        self.assertFalse(pp.micro_traces_channel_enabled(
            {'skillProfile': 'base', 'videoModel': 'Veo 3.1', 'title': 'miniature shack'}))
