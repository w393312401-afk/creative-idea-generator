# -*- coding: utf-8 -*-
"""「双空间重置兑现」的首帧契约：载体在 Beat 1 才被装备运到现场，所以 IMAGE 1 是
**还没有载体**的空场地。

这条契约横跨合成侧四个地方，任何一处漏掉都会把「第一帧就出现载体」放回来：
1. Phase 1 的 IMAGE 1 提示词（取景对象换成场地 + 明令载体不得入镜）；
2. Drift Lock 包的外部锚点（必须是场地自身的特征，否则首帧要么画出载体、要么漏锚点）；
3. beat ladder 上的 carrier_delivery 标记（Beat 1 的机械豁免与「起始帧没有载体」）；
4. 渲染后的 Anchor 验收门禁（否则它会拿载体去比对空场地，逼着一直重画到画出载体）。

这里用一份假的 _chat 跑通 Phase 1，断言四处都按同一个口径生效。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


def _ladder_json(total=12, bridge_at=3, cut_at=7):
    """一条能过 Phase 1 确定性验收的双空间梯子（12 拍：11 施工 + reward）。

    这个骨架有两处不连续，缺一不可：bridge_at 是进主空间的一镜过门（bridge_stage=1），
    cut_at 是主空间完工后重置到第二毛坯空间的硬切（hard_cut）。两处后面各跟一拍清运。
    cut_at=None 用来构造其它骨架（只有过门、没有重置）的梯子。
    """
    beats = []
    for i in range(1, total + 1):
        beat = {
            'index': i,
            'operation': 'repair',
            'description': f'stage {i} work',
            'bridge_stage': None,
            'stage_scope': 'large',
            'milestone_name': f'stage {i} product complete',
            'before_state': f'stage {i} product absent',
            'after_state': f'the entire stage {i} product is complete',
            'completion_extent': 'the full named zone',
            'changed_grid_cells': ['Grid B2', 'Grid C2'],
            # 普通施工拍要申报 2~3 道紧密工序（pp._MIN_PACKAGE_OPERATIONS）；
            # repair + placement 都不属于任何材料层族，不会触发跨层判据。
            'package_operations': ['repair', 'placement'],
            'primary_progress': 'grows from absent to complete',
            'secondary_progress': 'the staged stock drains from full to empty',
            'persistent_traces': ['fastener marks', 'contact dust'],
            'preserve_state': 'all earlier permanent work remains unchanged',
            'introduced_objects': [],
            'removed_objects': [],
        }
        if bridge_at and i == bridge_at:
            beat.update(operation='threshold', bridge_stage=1)
        elif cut_at and i == cut_at:
            # 切点必须点名它穿过的那道边界，否则是凭空跳到另一个舱段
            beat.update(operation='threshold', hard_cut=True,
                        description='hard cut through the bulkhead sliding door into the raw '
                                    'rear compartment',
                        milestone_name='second compartment established beyond the bulkhead')
        elif (bridge_at and i == bridge_at + 1) or (cut_at and i == cut_at + 1):
            beat.update(operation='clearing')
        elif i == total:
            beat.update(operation='reward')
        beats.append(beat)
    return json.dumps(beats)


PACKET_JSON = json.dumps({
    'camera_dna': 'static tripod shot, ultra-wide lens feel, camera height 1.6m; horizon line remains level',
    'geometry_lock': 'the quarry floor and its bank lines are fixed',
    'primary_landmarks': [
        {'name': 'cracked slab corner', 'grid': 'Grid C2', 'z_depth_scale': '20%'},
        {'name': 'slumped earth bank', 'grid': 'Grid B2', 'z_depth_scale': '45%'},
        {'name': 'ridge tree line', 'grid': 'Grid A2', 'z_depth_scale': '30%'},
    ],
    'frame_boundaries': {'left': 'B1', 'right': 'B3', 'top': 'A2', 'bottom': 'C2'},
    'object_ledger': [],
    'worker_choreography': 'one lone worker in a pale shirt',
    'worker_scale_percent': '18%',
    'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, 14)},
    'passive_environment': 'drifting cloud shadow',
    'interest_budget': {},
    'interior_camera_dna': 'static interior shot; camera pitch locked level; vanishing axis centered',
    'interior_primary_landmarks': [
        {'name': 'corrugated side wall ribs', 'grid': 'Grid B1', 'z_depth_scale': '55%'},
        {'name': 'end door frame ribs', 'grid': 'Grid B3', 'z_depth_scale': '50%'},
    ],
    'interior_light_source': 'a work light installed in an earlier beat',
})

BRIEF_JSON = json.dumps({
    'carrier': 'shipping container',
    'env': 'derelict quarry floor',
    'trauma': 'dented and rust-streaked',
    'destiny': 'buried two-room shelter',
    'destiny_zh': '双舱避难所',
    'reward': 'lights activate',
    'mode': 'Threshold',
    'space_type': 'buried shell',
    'threshold_variant': 'hard_cut',
    'threshold_elevated': False,
    # 两空间之间的边界：切点穿过的就是它（见 normalize_space_divider）
    'space_divider': 'the original steel end bulkhead with its sliding door',
    'secondary_space': 'the rear compartment behind that bulkhead',
    'space_divider_entry': 'existing_door',
})

CLEAN_IMAGE_1 = (
    'A static tripod shot, ultra-wide lens feel, camera height 1.6m: a derelict quarry floor, '
    'cracked slab corner at Grid C2, slumped earth bank at Grid B2, ridge tree line at Grid A2; '
    'weeds split the slab, rusted fence wire sags across the gravel, rubble is drifted into the '
    'bank; horizon line remains level.'
)
LEAKED_IMAGE_1 = (
    'A static tripod shot: a rust-streaked shipping container already sits on the derelict quarry '
    'floor; horizon line remains level.'
)


class TestDeliveredCarrierAnchor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp, 'ck.json')),
            patch.object(pp, 'CACHE_PATH', os.path.join(self._tmp, 'packet_cache.json')),
        ]
        for p in self._patches:
            p.start()
        self.dimensions = {
            'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工', 'budget': '轻奢设计师级',
            'ratio': 50, 'creativity': '突破常规', 'beats_count': 11,
            'pacing_skeleton': 'nested_space_payoff',
        }
        self.calls = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fake_chat(self, image_1_responses):
        pending = list(image_1_responses)

        def _chat(config, system, user, **kwargs):
            self.calls.append((system, user))
            if 'construction planner' in system:
                return getattr(self, '_ladder', None) or _ladder_json()
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return pending.pop(0)
            return BRIEF_JSON

        return _chat

    def _run(self, image_1_responses=(CLEAN_IMAGE_1,)):
        with patch.object(pp, '_chat', side_effect=self._fake_chat(image_1_responses)), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            return pp.compose_anchor_and_packet({}, self.dimensions)

    def _system_containing(self, needle):
        return next(s for s, _ in self.calls if needle in s)

    def test_image_1_prompt_demands_the_empty_receiving_site(self):
        state = self._run()
        img1_system = self._system_containing('the very first IMAGE prompt')
        self.assertIn('CARRIER NOT YET ON SITE', img1_system)
        self.assertIn('shipping container', img1_system)
        self.assertIn('EMPTY RECEIVING SITE', img1_system)
        # 取景对象整体换成场地，否则第 7 条还在要求「载体的全貌」
        self.assertIn('full extent of the receiving site', img1_system)
        self.assertNotIn('full extent of the carrier', img1_system)
        # 正文原样保留在开头；omni 档会在尾部补一句拍摄质感（见 test_image_1_carries_
        # the_profile_capture_style），所以这里比对前缀而不是全等。
        self.assertTrue(state['image_1_prompt'].startswith(CLEAN_IMAGE_1))

    def test_image_1_carries_the_profile_capture_style(self):
        """IMAGE 1 在 Phase 1 生成，而拍摄质感契约挂在 Phase 2 的 VIDEO OVERRIDE 段上，
        首帧因此曾是整条序列里唯一没有 UGC 手机质感的一张（2026-08-02 复盘）。
        omni 档下它必须和 2..N 帧同一套风格。"""
        with patch.object(pp, 'active_skill_profile', return_value='omni'):
            state = self._run()
        self.assertIn('smartphone', state['image_1_prompt'].lower())

    def test_image_1_keeps_base_profile_style_untouched(self):
        """base 档的拍摄质感由它自己的模板承载，不该被顺手塞一句手机质感。"""
        with patch.object(pp, 'active_skill_profile', return_value='base'):
            state = self._run()
        self.assertEqual(state['image_1_prompt'], CLEAN_IMAGE_1)

    def test_image_1_is_regenerated_when_the_carrier_leaks_into_the_anchor(self):
        """直出模式对首帧只记录不重做——唯独这条例外：载体提前入镜会让 Beat 1
        无货可交，必须带着违规原文重来。"""
        state = self._run([LEAKED_IMAGE_1, CLEAN_IMAGE_1])
        self.assertTrue(state['image_1_prompt'].startswith(CLEAN_IMAGE_1))
        retry_users = [u for s, u in self.calls if 'the very first IMAGE prompt' in s]
        self.assertEqual(len(retry_users), 2)
        self.assertIn('describes the carrier itself', retry_users[1])

    def test_packet_anchors_are_forced_onto_site_features(self):
        self._run()
        packet_system = self._system_containing('spatial consistency supervisor')
        self.assertIn('DELIVERED-CARRIER ANCHOR RULE', packet_system)
        self.assertIn('NEVER register the carrier itself', packet_system)
        self.assertIn('OPENING SUBJECT SCALE LOCK', packet_system)
        self.assertIn('never an ultra-wide, aerial, high-angle, or distant panorama', packet_system)
        self.assertIn('MOUNTAIN-AND-WATER SCENE LOCK', packet_system)
        self.assertIn('BOTH the registered mountain landform and the registered natural water body',
                      packet_system)
        self.assertIn('Keep the footprint safely above the waterline', packet_system)

    def test_empty_anchor_reserves_a_large_central_footprint(self):
        """首帧虽然还没有载体，也不能拍成大景观里的一小块空地；否则同机位落位后的
        载体必然还是远处小物体。"""
        self._run()
        img1_system = self._system_containing('the very first IMAGE prompt')
        self.assertIn('OPENING SCALE LOCK', img1_system)
        self.assertIn('footprint fills the central majority', img1_system)
        self.assertIn('roughly two-thirds of the frame', img1_system)
        self.assertIn('MOUNTAIN AND WATER LOCK', img1_system)
        self.assertIn('both a real mountain or steep mountain ridge', img1_system)
        self.assertIn('a real river, lake, stream, reservoir, fjord, or sheltered coastal inlet',
                      img1_system)
        self.assertIn('secondary framing layers around the large central footprint', img1_system)

    def test_beat_one_is_stamped_as_the_carrier_delivery(self):
        state = self._run()
        self.assertTrue(state['beat_ladder'][0].get('carrier_delivery'))
        self.assertFalse(any(b.get('carrier_delivery') for b in state['beat_ladder'][1:]))
        self.assertTrue(pp.carrier_arrives_on_camera(state['parsed_brief']))

    def test_fifteen_slot_budget_uses_the_container_creative_reference_form(self):
        """14 个施工节拍 + 1 个 reward 时，规划器要逐槽参考当前成功集装箱创意，
        不能只拿一句笼统的“双空间”让模型自由分配阶段。"""
        self.dimensions['beats_count'] = 14
        self._ladder = _ladder_json(total=15, bridge_at=3, cut_at=10)
        self._run()
        beat_system = self._system_containing('construction planner')
        self.assertIn('CANONICAL 15-SLOT REFERENCE FORM', beat_system)
        self.assertIn('Beat 1 carrier delivery/landing', beat_system)
        self.assertIn('Beat 9 primary core furniture and function-complete mini-payoff', beat_system)
        self.assertIn('Beat 10 declared reset into the untouched secondary space', beat_system)
        self.assertIn('Beat 15 worker exit and sole final reward', beat_system)

    def test_other_skeletons_keep_the_carrier_in_the_anchor_frame(self):
        self.dimensions['pacing_skeleton'] = 'linear_milestone'
        # 单线沿用 brief 判定的 hard_cut 变体：只有一处切入、没有第二空间重置
        self._ladder = _ladder_json(total=12, bridge_at=None, cut_at=4)
        state = self._run()
        img1_system = self._system_containing('the very first IMAGE prompt')
        self.assertNotIn('CARRIER NOT YET ON SITE', img1_system)
        self.assertIn('full extent of the carrier', img1_system)
        packet_system = self._system_containing('spatial consistency supervisor')
        self.assertNotIn('DELIVERED-CARRIER ANCHOR RULE', packet_system)
        self.assertFalse(state['beat_ladder'][0].get('carrier_delivery'))


class TestSecondSpaceArcFloor(unittest.TestCase):
    """硬切之后必须真的把第二空间盖起来，而不是切过去晃一眼就完。

    激发侧给第二幕记的账是 5 条（分层 4 + reward 1，见 _NESTED_MIN_SECONDARY_ENTRIES），
    而合成侧原来只查「硬切后至少有一拍」——ladder 于是可以合法地把硬切放到倒数第二拍，
    第二空间一拍带过。用户看到的就是「没进第二毛坯空间改造」。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp, 'ck.json')),
            patch.object(pp, 'CACHE_PATH', os.path.join(self._tmp, 'packet_cache.json')),
        ]
        for p in self._patches:
            p.start()
        self.dimensions = {
            'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工', 'budget': '轻奢设计师级',
            'ratio': 50, 'creativity': '突破常规', 'beats_count': 11,
            'pacing_skeleton': 'nested_space_payoff',
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, ladders):
        pending = list(ladders)
        beat_users = []

        def _chat(config, system, user, **kwargs):
            if 'construction planner' in system:
                beat_users.append(system + '\n' + user)
                return pending.pop(0)
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return CLEAN_IMAGE_1
            return BRIEF_JSON

        with patch.object(pp, '_chat', side_effect=_chat), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            state = pp.compose_anchor_and_packet({}, self.dimensions)
        return state, beat_users

    def test_a_one_beat_second_space_is_rejected_and_fed_back(self):
        # cut_at=10：硬切之后只剩「清理 + reward」，第二空间等于没盖
        state, beat_users = self._run([_ladder_json(total=12, cut_at=10),
                                       _ladder_json(total=12)])
        self.assertEqual(len(beat_users), 2)
        self.assertIn('PRIOR STRUCTURE VIOLATIONS', beat_users[1])
        self.assertIn('SECOND space needs at least', beat_users[1])
        establish_idx = next(i for i, b in enumerate(state['beat_ladder'])
                             if b.get('transition_stage') == 'secondary_establish')
        ordinary_after = [b for b in state['beat_ladder'][establish_idx + 1:-1]
                          if b.get('operation') not in ('threshold', 'reward')]
        self.assertGreaterEqual(len(ordinary_after), pp._NESTED_MIN_SECONDARY_ARC_BEATS)

    def test_a_full_second_arc_is_accepted_first_try(self):
        state, beat_users = self._run([_ladder_json(total=12)])
        self.assertEqual(len(beat_users), 1)
        # 两处概念标记均展开为可见过门，最终梯子不保留 hard_cut。
        self.assertEqual(state['beat_ladder'][2].get('bridge_stage'), 1)
        self.assertFalse(any(b.get('hard_cut') for b in state['beat_ladder']))
        self.assertTrue(any(b.get('transition_stage') == 'secondary_threshold'
                            for b in state['beat_ladder']))
        # 生成侧也要先说清楚，别让模型靠被打回来学：白烧一次 150s 规划调用
        self.assertIn('SECOND DISCONTINUITY', beat_users[0])
        self.assertIn('At least 4 ordinary construction beats must follow Beat R', beat_users[0])
        self.assertIn('FIXED SLOT BLUEPRINT', beat_users[0])
        self.assertIn('Beat 3 is the bridge_stage=1 crossing', beat_users[0])
        self.assertIn('Beat 7 is the hard_cut=true space reset', beat_users[0])
        self.assertIn('exactly 12 elements', beat_users[0])

    def test_final_retry_repairs_incompatible_package_metadata(self):
        ladder = json.loads(_ladder_json(total=12))
        ladder[5]['operation'] = 'furnishing'
        # wiring 与 furnishing 不相容，lighting 与 furnishing 同族：末轮修复删掉
        # 前者、留下后者，包仍然满足 2 道的下限。（若删完只剩一道，修复会整拍放过
        # 不动——把「相位冲突」换成「只剩一道工序」并没有修好任何东西。）
        ladder[5]['package_operations'] = ['furnishing', 'wiring', 'lighting']
        encoded = json.dumps(ladder)

        state, beat_users = self._run([encoded, encoded, encoded])

        # 2026-08-22：规划循环加了「不收敛就收手」（见 tests/test_beat_ladder_early_stop.py）。
        # 这里三轮喂的是**同一份**梯子，第 3 轮必然没能超过前两轮 —— 收手点恰好也是第 3
        # 轮，与原来的"末轮"重合，所以轮数仍是 3，末轮的确定性修复照常执行。
        self.assertEqual(len(beat_users), 3)
        repaired = next(b for b in state['beat_ladder']
                        if b.get('operation') == 'furnishing'
                        and b.get('transition_stage') == 'none')
        self.assertEqual(repaired['package_operations'], ['furnishing', 'lighting'])

    def test_a_missing_reset_cut_is_rejected(self):
        """只有过门、没有重置切点 —— 这正是 run_1785463152800 的形态：
        12 张图里 4~12 全是同一个空间，用户看到的就是「完全没有第二空间」。"""
        state, beat_users = self._run([_ladder_json(total=12, cut_at=None),
                                       _ladder_json(total=12)])
        self.assertEqual(len(beat_users), 2)
        self.assertIn('EXACTLY ONE space-reset beat', beat_users[1])
        self.assertTrue(any(b.get('transition_stage') == 'secondary_threshold'
                            for b in state['beat_ladder']))

    def test_a_reset_cut_that_skips_the_primary_payoff_is_rejected(self):
        """重置紧贴过门 = 主空间还没盖就切走，重置前那一拍不可能是小完工。"""
        state, beat_users = self._run([_ladder_json(total=12, bridge_at=3, cut_at=5),
                                       _ladder_json(total=12)])
        self.assertEqual(len(beat_users), 2)
        self.assertIn('PRIMARY space needs at least', beat_users[1])

    def test_other_skeletons_keep_the_single_interior_beat_floor(self):
        """这套下界是 nested 专属的。dual/linear 只有一处过门、过门后一拍即可，
        套上第二次硬切与 4 拍第二幕会把它们本来合法的梯子判成违规。"""
        self.dimensions['pacing_skeleton'] = 'dual_payoff'
        state, beat_users = self._run([_ladder_json(total=12, bridge_at=10, cut_at=None)])
        self.assertEqual(len(beat_users), 1)
        self.assertNotIn('SECOND DISCONTINUITY', beat_users[0])
        self.assertEqual(len(state['beat_ladder']), 14)


class TestSpaceDividerLogic(unittest.TestCase):
    """第二空间必须「进得去」，而不是切一下就换了房间。

    2026-07-31 用户复盘：切点落地时观众既不知道第二空间存在，也不知道它在哪、怎么过去的。
    补的是边界的制作逻辑——边界是个具体的东西、在主空间里一直看得见且关着、本来没有门
    时必须在片中开出来。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp, 'ck.json')),
            patch.object(pp, 'CACHE_PATH', os.path.join(self._tmp, 'pc.json')),
        ]
        for p in self._patches:
            p.start()
        self.dimensions = {
            'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工', 'budget': '轻奢设计师级',
            'ratio': 50, 'creativity': '突破常规', 'beats_count': 11,
            'pacing_skeleton': 'nested_space_payoff',
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, ladders, brief=None):
        pending = list(ladders)
        beat_users = []

        def _chat(config, system, user, **kwargs):
            if 'construction planner' in system:
                beat_users.append(system + '\n' + user)
                return pending.pop(0)
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return CLEAN_IMAGE_1
            return brief or BRIEF_JSON

        with patch.object(pp, '_chat', side_effect=_chat), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            return pp.compose_anchor_and_packet({}, self.dimensions), beat_users

    def test_brief_normalization_fills_a_usable_divider(self):
        """brief LLM 漏字段时不能让下游拿空串去拼契约，那会写出「push through the 」。
        默认 built_opening：最坏是多演一拍开门，反过来默认「本来就有门」会让实心
        舱壁被直接穿过去。"""
        brief = pp.apply_pacing_skeleton_to_brief({'carrier': 'boxcar'}, 'nested_space_payoff')
        pp.normalize_space_divider(brief)
        self.assertTrue(brief['space_divider'])
        self.assertEqual(brief['space_divider_entry'], 'built_opening')
        self.assertTrue(brief['secondary_space'])
        # 其它骨架不该被塞这些键
        other = pp.apply_pacing_skeleton_to_brief({'carrier': 'boxcar'}, 'dual_payoff')
        pp.normalize_space_divider(other)
        self.assertNotIn('space_divider', other)

    def test_divider_matching_falls_back_when_the_name_is_all_stopwords(self):
        """「the partition wall between the two compartments」全是通用词，用项目自己的
        实词一个都匹配不上——这类完全合理的命名不能因此判成没点名边界。"""
        generic = {'space_divider': 'the partition wall between the two compartments'}
        self.assertEqual(pp.space_divider_terms(generic), ['partition', 'wall'])
        self.assertTrue(pp.mentions_space_divider('cut a doorway through the partition', generic))
        self.assertFalse(pp.mentions_space_divider('lay the oak floor planks', generic))

        # 实词一个不剩的命名：退回通用隔断词表，而不是判成「没点名边界」
        all_stopwords = {'space_divider': 'the original steel section between both compartments'}
        self.assertEqual(pp.space_divider_terms(all_stopwords), [])
        self.assertTrue(pp.mentions_space_divider('push open the hatch', all_stopwords))
        self.assertFalse(pp.mentions_space_divider('lay the oak floor planks', all_stopwords))

        named = {'space_divider': 'the original steel end bulkhead with its sliding door'}
        self.assertTrue(pp.mentions_space_divider('push open the bulkhead door', named))
        self.assertFalse(pp.mentions_space_divider('install the ceiling battens', named))

    def test_a_cut_that_names_no_boundary_is_rejected(self):
        bad = json.loads(_ladder_json(total=12))
        bad[6]['description'] = 'jump to the second space'
        bad[6]['milestone_name'] = 'second space established'
        _, beat_users = self._run([json.dumps(bad), _ladder_json(total=12)])
        self.assertEqual(len(beat_users), 2)
        self.assertIn('must name the physical boundary it passes through', beat_users[1])

    def test_a_solid_divider_demands_an_on_camera_doorway_beat(self):
        """载体的隔断本来没有门时，主空间那一幕必须有一拍把门洞开出来，
        否则切点等于让镜头穿墙。"""
        solid = json.loads(BRIEF_JSON)
        solid['space_divider'] = 'the solid riveted mid-shell bulkhead'
        solid['space_divider_entry'] = 'built_opening'
        solid = json.dumps(solid)

        no_opening = json.loads(_ladder_json(total=12))
        no_opening[6]['description'] = 'hard cut through the mid-shell bulkhead into the raw rear half'

        with_opening = json.loads(json.dumps(no_opening))
        with_opening[4].update(
            description='cut and frame a new doorway through the riveted bulkhead',
            milestone_name='bulkhead doorway framed and fitted with a shut panel',
            after_state='the bulkhead now carries one finished framed doorway, still closed off')

        _, beat_users = self._run([json.dumps(no_opening), json.dumps(with_opening)], brief=solid)
        self.assertEqual(len(beat_users), 2)
        self.assertIn('must CUT AND FRAME that doorway on camera', beat_users[1])

    def test_an_existing_door_needs_no_doorway_beat(self):
        _, beat_users = self._run([_ladder_json(total=12)])
        self.assertEqual(len(beat_users), 1)
        self.assertIn('already has a usable door/hatch', beat_users[0])
        self.assertNotIn('must CUT AND FRAME that doorway', beat_users[0])

    def test_primary_beats_carry_the_shut_divider_contract(self):
        ladder = json.loads(_ladder_json(total=12))
        brief = pp.normalize_space_divider(
            pp.apply_pacing_skeleton_to_brief(json.loads(BRIEF_JSON), 'nested_space_payoff'))
        packet = json.loads(PACKET_JSON)

        primary = pp._beat_contract(5, 12, ladder, 'Threshold', packet, '', parsed_brief=brief)
        self.assertIn('stays visible in frame and SHUT', primary['family_contract'])
        self.assertIn('bulkhead', primary['family_contract'])

        # 过门拍本身是首现帧，不背这条（它要说的是「室内第一次露面且没人碰过」）
        bridge = pp._beat_contract(3, 12, ladder, 'Threshold', packet, '', parsed_brief=brief)
        self.assertNotIn('stays visible in frame and SHUT', bridge['family_contract'])

        # 切点拍要说清穿过的就是它
        cut = pp._beat_contract(7, 12, ladder, 'Threshold', packet, '', parsed_brief=brief)
        self.assertIn('this cut passes through', cut['family_contract'])

        # 第二空间已经在门的另一侧，不该再要求那道门关着
        secondary = pp._beat_contract(9, 12, ladder, 'Threshold', packet, '', parsed_brief=brief)
        self.assertNotIn('stays visible in frame and SHUT', secondary['family_contract'])

    def test_other_skeletons_get_no_divider_contract(self):
        ladder = json.loads(_ladder_json(total=12, bridge_at=3, cut_at=None))
        brief = pp.apply_pacing_skeleton_to_brief(json.loads(BRIEF_JSON), 'dual_payoff')
        pp.normalize_space_divider(brief)
        contract = pp._beat_contract(5, 12, ladder, 'Threshold', json.loads(PACKET_JSON), '',
                                     parsed_brief=brief)
        self.assertNotIn('stays visible in frame and SHUT', contract['family_contract'])


class TestSecondSpaceHasItsOwnAnchors(unittest.TestCase):
    """重置切点之后的帧不能再复述主空间的地标和室内 camera DNA。

    2026-07-31 实机复盘：两个空间共用同一套 interior 锚点 + 同一句 camera DNA，出来的
    画面是「刚装好的车厢原地变回废墟」——观众读到的不是换了舱段，而是同一个房间被推倒
    重来。族仍然是 'interior'（门框出画/禁地平线/包络封闭这些室内规则一条都不能少），
    换的只是锚点集与室内机位。
    """

    LADDER = json.loads(_ladder_json(total=12))
    PACKET_TWO_SPACES = dict(
        json.loads(PACKET_JSON),
        secondary_interior_camera_dna=(
            'static interior shot from the far end looking back along the short axis; '
            'camera pitch locked level'),
        secondary_interior_primary_landmarks=[
            {'name': 'rear bulkhead hatch ring', 'grid': 'Grid B2', 'z_depth_scale': '50%'},
            {'name': 'stacked bunk rail brackets', 'grid': 'Grid C1', 'z_depth_scale': '30%'},
        ],
    )

    def test_space_index_flips_only_at_the_reset_cut(self):
        # 过门在第 3 拍、重置切点在第 7 拍
        for i in (1, 2, 3, 4, 5, 6):
            self.assertEqual(pp.beat_space_index(self.LADDER, i), 1, f'beat {i}')
        for i in (7, 8, 9, 10, 11, 12):
            self.assertEqual(pp.beat_space_index(self.LADDER, i), 2, f'beat {i}')

    def test_single_crossing_projects_stay_in_one_space(self):
        """只有一处不连续的项目（dual/linear）整片只有一个室内，恒为第 1 空间。"""
        bridge_only = json.loads(_ladder_json(total=12, bridge_at=3, cut_at=None))
        cut_only = json.loads(_ladder_json(total=12, bridge_at=None, cut_at=4))
        for ladder in (bridge_only, cut_only):
            for i in range(1, 13):
                self.assertEqual(pp.beat_space_index(ladder, i), 1)

    def test_packet_view_swaps_anchors_and_camera_dna(self):
        view = pp.packet_for_space(self.PACKET_TWO_SPACES, 2)
        names = [lm['name'] for lm in view['interior_primary_landmarks']]
        self.assertEqual(names, ['rear bulkhead hatch ring', 'stacked bunk rail brackets'])
        self.assertIn('looking back along the short axis', view['interior_camera_dna'])
        # 主空间那一套一个字都不该漏进来
        primary_names = [lm['name'] for lm in self.PACKET_TWO_SPACES['interior_primary_landmarks']]
        self.assertFalse(set(names) & set(primary_names))
        # 原包不被就地修改
        self.assertEqual(
            [lm['name'] for lm in self.PACKET_TWO_SPACES['interior_primary_landmarks']],
            primary_names)

    def test_packet_view_is_a_no_op_without_a_second_set(self):
        """老包没有第二套锚点：原样返回，保持既有行为而不是把锚点清空。"""
        legacy = json.loads(PACKET_JSON)
        self.assertIs(pp.packet_for_space(legacy, 2), legacy)
        self.assertIs(pp.packet_for_space(self.PACKET_TWO_SPACES, 1), self.PACKET_TWO_SPACES)

    def test_beat_contract_hands_the_second_space_its_own_anchors(self):
        primary = pp._beat_contract(5, 12, self.LADDER, 'Threshold', self.PACKET_TWO_SPACES, '')
        secondary = pp._beat_contract(9, 12, self.LADDER, 'Threshold', self.PACKET_TWO_SPACES, '')
        self.assertEqual(primary['space'], 1)
        self.assertEqual(secondary['space'], 2)
        # 两拍的族都还是 interior：室内规则不能因为换空间就失效
        self.assertEqual(primary['family'], 'interior')
        self.assertEqual(secondary['family'], 'interior')
        self.assertIn('Door clearance (mandatory)', secondary['family_contract'])

        # 室内 camera DNA 也必须换成第二空间那一句，否则两个舱段连机位都一模一样
        self.assertIn('vanishing axis centered', primary['family_contract'])
        self.assertNotIn('looking back along the short axis', primary['family_contract'])
        self.assertIn('looking back along the short axis', secondary['family_contract'])
        self.assertIn('SECOND SPACE (mandatory)', secondary['family_contract'])
        self.assertNotIn('SECOND SPACE (mandatory)', primary['family_contract'])
        # 校验/修复也必须拿到换过视图的包，否则主空间的地标又被盖回第二空间的帧
        self.assertEqual(
            [lm['name'] for lm in secondary['packet']['interior_primary_landmarks']],
            ['rear bulkhead hatch ring', 'stacked bunk rail brackets'])

    def test_packet_prompt_asks_the_second_space_for_its_own_anchor_set(self):
        systems = []
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        def _chat(config, system, user, **kwargs):
            systems.append(system)
            if 'construction planner' in system:
                return _ladder_json(total=12)
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return CLEAN_IMAGE_1
            return BRIEF_JSON

        dims = {'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工',
                'budget': '轻奢设计师级', 'ratio': 50, 'creativity': '突破常规',
                'beats_count': 11, 'pacing_skeleton': 'nested_space_payoff'}
        with patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(tmp, 'ck.json')), \
                patch.object(pp, 'CACHE_PATH', os.path.join(tmp, 'pc.json')), \
                patch.object(pp, '_chat', side_effect=_chat), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            pp.compose_anchor_and_packet({}, dims)

        packet_system = next(s for s in systems if 'spatial consistency supervisor' in s)
        self.assertIn('secondary_interior_camera_dna', packet_system)
        self.assertIn('secondary_interior_primary_landmarks', packet_system)
        self.assertIn('none of them may be an object already registered', packet_system)

    def test_single_space_projects_are_not_asked_for_a_second_set(self):
        """dual/linear 只有一个室内：多问一套锚点等于让 packet LLM 凭空编一个房间。"""
        systems = []
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        def _chat(config, system, user, **kwargs):
            systems.append(system)
            if 'construction planner' in system:
                return _ladder_json(total=12, bridge_at=3, cut_at=None)
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return CLEAN_IMAGE_1
            return BRIEF_JSON

        dims = {'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工',
                'budget': '轻奢设计师级', 'ratio': 50, 'creativity': '突破常规',
                'beats_count': 11, 'pacing_skeleton': 'dual_payoff'}
        with patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(tmp, 'ck.json')), \
                patch.object(pp, 'CACHE_PATH', os.path.join(tmp, 'pc.json')), \
                patch.object(pp, '_chat', side_effect=_chat), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            pp.compose_anchor_and_packet({}, dims)

        packet_system = next(s for s in systems if 'spatial consistency supervisor' in s)
        self.assertNotIn('secondary_interior_primary_landmarks', packet_system)


if __name__ == '__main__':
    unittest.main()
