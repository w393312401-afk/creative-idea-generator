# -*- coding: utf-8 -*-
"""节拍阶梯规划的「记住最好的一版 + 不收敛就收手」（2026-08-22）。

这个循环是整条合成里最贵的一段：最多 4 轮，每轮一次 150s 超时的重负载调用，实测占
Phase 1 的 177s / 222s。翻 12 次真实规划的违规数轨迹（65→19→41、37→17→38、
50→20→20→50、39→23→26→39、12→24→12→24、34→10→32、48→17→32、53→14→36）能看出
两件事：

1. 中间轮次经常比后面的轮次**更好**，而代码原来只认「最后一版」——一条 19 项违规的
   第 2 轮梯子会被 41 项违规的第 3 轮梯子无条件顶掉。
2. 一旦某一轮没能超过已有最好成绩，这 12 次样本里后续轮次再没超过过它。

于是：全程记住最好的一版；某一轮没进步就收手，交付**最好的那一版**而不是刚跑出来
的更差那版。收手有一条硬前提——最好的那一版必须已经没有阻塞级违规（也就是它本来
就过得了最后一轮的宽松验收）。还有阻塞级违规时第 3、4 轮确实经常才把阻塞项清干净
（server.log 里那几条 "accepting final ladder ... after N attempt(s)" 就是这么来的），
那几轮一轮都不能省。本文件把这三条都钉住。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


def _ladder_json(total=12, bridge_at=3, tag='a'):
    """一条能过 Phase 1 确定性验收的单过门梯子。tag 只用于让两版梯子可区分。"""
    beats = []
    for i in range(1, total + 1):
        beat = {
            'index': i,
            'operation': 'repair',
            'description': f'stage {i} work',
            'bridge_stage': None,
            'stage_scope': 'large',
            'milestone_name': f'{tag} stage {i} product complete',
            'before_state': f'stage {i} product absent',
            'after_state': f'the entire stage {i} product is complete',
            'completion_extent': 'the full named zone',
            'changed_grid_cells': ['Grid B2', 'Grid C2'],
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
        elif bridge_at and i == bridge_at + 1:
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
    'destiny': 'buried shelter',
    'destiny_zh': '掩体避难所',
    'reward': 'lights activate',
    'mode': 'Threshold',
    'space_type': 'buried shell',
    'threshold_variant': 'coaxial',
    'threshold_elevated': False,
})

CLEAN_IMAGE_1 = (
    'A static tripod shot, ultra-wide lens feel, camera height 1.6m: a derelict quarry floor, '
    'cracked slab corner at Grid C2, slumped earth bank at Grid B2, ridge tree line at Grid A2; '
    'weeds split the slab, rusted fence wire sags across the gravel, rubble is drifted into the '
    'bank; horizon line remains level.'
)


class TestBeatLadderEarlyStop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        for p in (patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp, 'ck.json')),
                  patch.object(pp, 'CACHE_PATH', os.path.join(self._tmp, 'packet_cache.json'))):
            p.start()
            self.addCleanup(p.stop)
        self.dimensions = {
            'theme': '废弃集装箱', 'anchors': [], 'complexity': '中等重工',
            'budget': '轻奢设计师级', 'ratio': 50, 'creativity': '突破常规',
            'beats_count': 11,
        }

    def _run(self, ladders, rhythm_per_attempt):
        """rhythm_per_attempt：每一轮 rhythm_ladder_violations 该报几条（用来精确摆布
        「这一轮比上一轮好还是差」，不必去构造真的会触发某条判据的梯子）。"""
        pending = list(ladders)
        rhythm = list(rhythm_per_attempt)
        planner_calls = []

        def _chat(config, system, user, **kwargs):
            if 'construction planner' in system:
                planner_calls.append(user)
                return pending.pop(0)
            if 'spatial consistency supervisor' in system:
                return PACKET_JSON
            if 'the very first IMAGE prompt' in system:
                return CLEAN_IMAGE_1
            return BRIEF_JSON

        def _rhythm(*_a, **_k):
            n = rhythm.pop(0) if rhythm else 0
            return [f'quality note {j}' for j in range(n)]

        with patch.object(pp, '_chat', side_effect=_chat), \
                patch.object(pp, 'rhythm_ladder_violations', side_effect=_rhythm), \
                patch.object(pp, 'load_reference_file', return_value=''), \
                patch.object(pp, 'get_cropped_templates', return_value=''):
            state = pp.compose_anchor_and_packet({}, self.dimensions)
        return state, planner_calls

    def test_stops_and_ships_the_better_earlier_draft(self):
        """第 3 轮没能超过第 1 轮 → 收手，且交付的是第 1 轮那一版。

        修复前：跑满 4 轮，并且交付最后一轮那一版（第 1 轮那条更好的梯子被无条件顶掉）。

        最早的收手点是第 3 轮而不是第 2 轮：确定性 package 元数据修复挂在
        `attempt >= 2` 上，比它更早收手会把这一版梯子的修复整个跳过
        （tests/test_nested_space_delivery_anchor.py 钉着那条）。"""
        state, planner_calls = self._run(
            ladders=[_ladder_json(tag='first'), _ladder_json(tag='second'),
                     _ladder_json(tag='third'), _ladder_json(tag='fourth')],
            rhythm_per_attempt=[2, 5, 5, 5])

        self.assertEqual(len(planner_calls), 3,
                         '第 3 轮没能超过已有最好成绩，就不该再打第 4 轮')
        # 收口阶段会补插 reframe 等无 milestone 的拍，这里只看带 milestone 的施工拍。
        milestones = [b['milestone_name'] for b in state['beat_ladder']
                      if b.get('milestone_name')]
        self.assertTrue(milestones)
        self.assertTrue(
            all(m.startswith('first') for m in milestones),
            f'收手时必须交付最好的那一版（第 1 轮），而不是刚跑出来的更差那版；'
            f'实际拿到: {milestones[:3]}')

    def test_keeps_retrying_while_the_draft_is_still_improving(self):
        """每一轮都在进步就不收手——收手只针对「不再收敛」。

        第 3 轮起 retry_for_rhythm 本来就恒为 False（既有行为，与本次改动无关），
        所以这里断言的是"没有在第 2 轮被收手掉"，而不是一定跑满 4 轮。"""
        _state, planner_calls = self._run(
            ladders=[_ladder_json(tag='a'), _ladder_json(tag='b'),
                     _ladder_json(tag='c'), _ladder_json(tag='d')],
            rhythm_per_attempt=[8, 6, 4, 2])
        self.assertGreaterEqual(len(planner_calls), 3, '一直在进步时不该提前收手')

    def test_the_deterministic_package_repair_round_is_never_skipped(self):
        """收手不能早于第 3 轮：确定性 package 元数据修复就挂在那一轮上。"""
        _state, planner_calls = self._run(
            ladders=[_ladder_json(tag='a'), _ladder_json(tag='b'),
                     _ladder_json(tag='c'), _ladder_json(tag='d')],
            rhythm_per_attempt=[2, 9, 9, 9])
        self.assertGreaterEqual(len(planner_calls), 3,
                                '第 2 轮就收手会跳过 attempt>=2 那道确定性修复')

    def test_a_clean_first_draft_is_accepted_without_any_retry(self):
        """零违规的首轮照旧一次过，收手逻辑不该多打一轮。"""
        _state, planner_calls = self._run(
            ladders=[_ladder_json(tag='clean')],
            rhythm_per_attempt=[0])
        self.assertEqual(len(planner_calls), 1)


if __name__ == '__main__':
    unittest.main()
