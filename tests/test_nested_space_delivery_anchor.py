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


def _ladder_json(total=12, cut_at=6):
    """一条能过 Phase 1 确定性验收的 hard_cut 梯子（12 拍：11 施工 + reward）。"""
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
            'package_operations': ['repair'],
            'primary_progress': 'grows from absent to complete',
            'secondary_progress': 'the staged stock drains from full to empty',
            'persistent_traces': ['fastener marks', 'contact dust'],
            'preserve_state': 'all earlier permanent work remains unchanged',
        }
        if i == cut_at:
            beat.update(operation='threshold', hard_cut=True)
        elif i == cut_at + 1:
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
                return _ladder_json()
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
        self.assertEqual(state['image_1_prompt'], CLEAN_IMAGE_1)

    def test_image_1_is_regenerated_when_the_carrier_leaks_into_the_anchor(self):
        """直出模式对首帧只记录不重做——唯独这条例外：载体提前入镜会让 Beat 1
        无货可交，必须带着违规原文重来。"""
        state = self._run([LEAKED_IMAGE_1, CLEAN_IMAGE_1])
        self.assertEqual(state['image_1_prompt'], CLEAN_IMAGE_1)
        retry_users = [u for s, u in self.calls if 'the very first IMAGE prompt' in s]
        self.assertEqual(len(retry_users), 2)
        self.assertIn('describes the carrier itself', retry_users[1])

    def test_packet_anchors_are_forced_onto_site_features(self):
        self._run()
        packet_system = self._system_containing('spatial consistency supervisor')
        self.assertIn('DELIVERED-CARRIER ANCHOR RULE', packet_system)
        self.assertIn('NEVER register the carrier itself', packet_system)

    def test_beat_one_is_stamped_as_the_carrier_delivery(self):
        state = self._run()
        self.assertTrue(state['beat_ladder'][0].get('carrier_delivery'))
        self.assertFalse(any(b.get('carrier_delivery') for b in state['beat_ladder'][1:]))
        self.assertTrue(pp.carrier_arrives_on_camera(state['parsed_brief']))

    def test_other_skeletons_keep_the_carrier_in_the_anchor_frame(self):
        self.dimensions['pacing_skeleton'] = 'linear_milestone'
        state = self._run()
        img1_system = self._system_containing('the very first IMAGE prompt')
        self.assertNotIn('CARRIER NOT YET ON SITE', img1_system)
        self.assertIn('full extent of the carrier', img1_system)
        packet_system = self._system_containing('spatial consistency supervisor')
        self.assertNotIn('DELIVERED-CARRIER ANCHOR RULE', packet_system)
        self.assertFalse(state['beat_ladder'][0].get('carrier_delivery'))


class TestDeliveredCarrierAnchorGate(unittest.TestCase):
    """渲染后的 Anchor 验收门禁必须换口径：空场地是正确答案，载体入镜才是失败。"""

    def _gate_system(self, level, brief):
        captured = {}

        def _fake_multimodal(config, system, user, images):
            captured['system'] = system
            return 'PASS'

        with patch.object(pp, 'qa_gate_level', return_value=level), \
                patch.object(pp, '_multimodal_chat', side_effect=_fake_multimodal):
            pp.check_anchor_frame_compliance({}, 'img.webp', 'prompt', {'camera_dna': 'x'}, brief)
        return captured['system']

    def setUp(self):
        self.delivered = pp.apply_pacing_skeleton_to_brief(
            {'carrier': 'shipping container', 'env': 'quarry floor', 'trauma': 'rusted'},
            'nested_space_payoff')
        self.ordinary = pp.apply_pacing_skeleton_to_brief(
            {'carrier': 'glacier ice cave', 'env': 'alpine cliff', 'trauma': 'frost-cracked'},
            'linear_milestone')

    def test_strict_gate_fails_a_carrier_that_should_not_be_there_yet(self):
        system = self._gate_system('strict', self.delivered)
        self.assertIn('CARRIER MUST BE ABSENT', system)
        self.assertIn('EMPTY, untouched site', system)

    def test_lenient_gate_also_knows_the_carrier_is_not_due_yet(self):
        system = self._gate_system('lenient', self.delivered)
        self.assertIn('CARRIER ALREADY PRESENT', system)

    def test_ordinary_projects_keep_judging_the_carrier_itself(self):
        for level in ('strict', 'lenient'):
            system = self._gate_system(level, self.ordinary)
            self.assertNotIn('CARRIER MUST BE ABSENT', system)
            self.assertNotIn('CARRIER ALREADY PRESENT', system)
            self.assertIn('glacier ice cave', system)


if __name__ == '__main__':
    unittest.main()
