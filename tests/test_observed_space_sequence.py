"""原片观察到的空间序列 → 复刻单里的过门次数。

盯的是一条会静默失效的不变量：**过门次数由原片决定，不由叙事骨架决定**。

失效时的样子（2026-08-14 用户复盘的地堡单）：原片先从山坡进隧道、再从隧道进主厅、
最后进睡眠壁龛，复刻出来的 13 张帧却只有一次过门——后面那两道门在画面深处，从头到尾
没被推开过。当时的链路里，过门次数是骨架常量：linear_milestone 写死一次、
nested_space_payoff 写死两次，而反推层压根不产出空间信息。

所以这里查三段接力，每一段断了都会让「第二次进门」悄悄消失：
  1. 反推层把逐拍 `space` 归一成序列，并数出过门位置（几次都算）；
  2. 清单条目把 space 透传给规划器，且系统提示词点名那几拍；
  3. 合成期的确定性收口把标记打在梯子上 —— 一路走到 [BRIDGE] 标签与机位族切分。
"""

import unittest

import prompt_pipeline as pp
from frame_continuity import family_map


def _beats(*spaces):
    return {'beats': [{'id': f'B{i:02d}', 'space': s} for i, s in enumerate(spaces, 1)]}


def _plan(*spaces):
    return [{'text': f'entry {i}', 'space': s} for i, s in enumerate(spaces, 1)]






class TestThePlannerIsToldWhereTheCrossingsAre(unittest.TestCase):
    """清单条目 → 规划器读到的那段文字。"""

    def test_every_boundary_is_named_and_the_single_crossing_wording_is_gone(self):
        plan, block = pp.build_outline_plan_block(
            _plan('slope', 'slope', 'tunnel', 'main room', 'main room', 'alcove'), 6)
        self.assertEqual(pp.outline_space_crossings(plan), [3, 4, 6])
        for spot in ('#3', '#4', '#6'):
            self.assertIn(spot, block)
        # 「挑一处边界」那套单过门口径不能同时出现——两套口径必有一套被违反。
        self.assertNotIn('usually the first entry whose work takes place inside', block)

    def test_a_card_without_space_labels_keeps_the_original_single_crossing_wording(self):
        _, block = pp.build_outline_plan_block(
            [{'text': 'entry 1'}, {'text': 'entry 2'}], 2)
        self.assertIn('usually the first entry whose work takes place inside', block)


class TestTheLadderIsSealedAgainstThePlanner(unittest.TestCase):
    """确定性收口：规划器标漏/标错，最终梯子仍以原片为准。"""

    def _ladder(self, n):
        return pp.finalize_beat_ladder_fields(
            [{'index': i, 'operation': 'flooring', 'description': f'b{i}'}
             for i in range(1, n + 1)])

    def test_a_planner_that_marked_only_one_crossing_gets_all_three_back(self):
        spaces = ['slope', 'slope', 'tunnel', 'tunnel', 'main room', 'alcove']
        brief = {'mode': 'Threshold', 'beat_outline': _plan(*spaces)}
        ladder = self._ladder(6)
        ladder[2]['bridge_stage'] = 1
        self.assertEqual(pp.apply_observed_space_sequence(ladder, brief), [3, 5, 6])
        self.assertEqual([b.get('bridge_stage') for b in ladder],
                         [None, None, 1, None, 2, 3])

    def test_a_crossing_the_planner_invented_is_cleared(self):
        brief = {'mode': 'Threshold', 'beat_outline': _plan('slope', 'tunnel', 'tunnel')}
        ladder = self._ladder(3)
        ladder[2]['bridge_stage'] = 2      # 同一个空间里凭空多标了一次
        pp.apply_observed_space_sequence(ladder, brief)
        self.assertEqual([b.get('bridge_stage') for b in ladder], [None, 1, None])

    def test_each_space_gets_its_own_camera_family_and_anchor_view(self):
        spaces = ['slope', 'tunnel', 'main room', 'alcove']
        brief = {'mode': 'Threshold', 'beat_outline': _plan(*spaces)}
        ladder = self._ladder(4)
        pp.apply_observed_space_sequence(ladder, brief)
        self.assertEqual([b['space_id'] for b in ladder],
                         ['site', 'primary', 'secondary', 'space_4'])
        # 机位族两两不同：相同就意味着 frame_continuity 会拿上一个空间的族锚
        # 去比对新空间的首帧。
        families = [b['camera_family'] for b in ladder]
        self.assertEqual(len(set(families)), 4)
        # 第 N 个室内空间取第 N 套锚点，不再封顶在 2。
        self.assertEqual([pp.beat_space_index(ladder, i) for i in range(1, 5)], [1, 1, 2, 3])

    def test_every_observed_space_is_registered_in_the_space_graph(self):
        """空间图是「不许凭空多出一个房间」的登记处；原片真有的房间必须在册。"""
        brief = {'mode': 'Threshold',
                 'beat_outline': _plan('slope', 'tunnel', 'main room', 'alcove')}
        pp.apply_observed_space_sequence(self._ladder(4), brief)
        graph = brief['space_graph']
        self.assertEqual([n['observed'] for n in graph['nodes']],
                         ['slope', 'tunnel', 'main room', 'alcove'])
        self.assertEqual(len(graph['edges']), 3)

    def test_a_ladder_whose_length_broke_the_one_to_one_contract_is_left_alone(self):
        """拍数与清单对不上时按下标硬贴，只会把过门贴到毫不相干的拍上。"""
        brief = {'mode': 'Threshold', 'beat_outline': _plan('slope', 'tunnel')}
        ladder = self._ladder(5)
        self.assertEqual(pp.apply_observed_space_sequence(ladder, brief), [])
        self.assertEqual([b.get('bridge_stage') for b in ladder], [None] * 5)

    def test_a_card_without_space_labels_changes_nothing(self):
        brief = {'mode': 'Threshold',
                 'beat_outline': [{'text': 'a'}, {'text': 'b'}, {'text': 'c'}]}
        ladder = self._ladder(3)
        ladder[1]['bridge_stage'] = 1
        self.assertEqual(pp.apply_observed_space_sequence(ladder, brief), [])
        self.assertEqual([b.get('bridge_stage') for b in ladder], [None, 1, None])


class TestTheCrossingsSurviveToTheDeliveredSlots(unittest.TestCase):
    """梯子上的标记 → [BRIDGE] 标签 → 帧渲染的机位族切分。"""

    def test_three_observed_crossings_become_three_bridges_and_four_families(self):
        spaces = ['slope', 'slope', 'tunnel', 'tunnel', 'main room', 'main room', 'alcove']
        brief = {'mode': 'Threshold', 'beat_outline': _plan(*spaces)}
        ladder = pp.finalize_beat_ladder_fields(
            [{'index': i, 'operation': 'flooring', 'description': f'b{i}'}
             for i in range(1, 8)])
        pp.apply_observed_space_sequence(ladder, brief)
        _, videos, _ = pp._build_partial_prompt_block(
            {i: f'image {i}' for i in range(1, 9)},
            {i: f'video {i}' for i in range(1, 8)},
            ladder, 'linear_milestone')
        bridges = sorted(i for i, v in videos.items() if 'BRIDGE' in v['meta'])
        self.assertEqual(bridges, [3, 5, 7])
        families = family_map(list(range(1, 9)), {i: videos[i] for i in videos})
        self.assertEqual(len(set(families.values())), 4)
        # 每次过门后的首帧都是新族的族头——比对基线不会跨过一次合法的空间切换。
        self.assertEqual(families[4], 'family-2')
        self.assertEqual(families[6], 'family-3')
        self.assertEqual(families[8], 'family-4')


class TestPacketAnchorsPerSpace(unittest.TestCase):
    """第 N 个空间取第 N 套锚点。取不到自己那套时宁可退回主空间，也不能盖上别人的。"""

    def _packet(self):
        return {
            'interior_camera_dna': 'primary dna',
            'interior_primary_landmarks': [{'name': 'primary wall'}],
            'interior_space_families': [
                {'space': 2, 'camera_dna': 'second dna',
                 'primary_landmarks': [{'name': 'second bulkhead'}]},
                {'space': 3, 'camera_dna': 'third dna',
                 'primary_landmarks': [{'name': 'third alcove end'}]},
            ],
        }

    def test_each_space_reads_its_own_family(self):
        packet = self._packet()
        self.assertEqual(pp.packet_for_space(packet, 1)['interior_camera_dna'], 'primary dna')
        self.assertEqual(pp.packet_for_space(packet, 2)['interior_camera_dna'], 'second dna')
        third = pp.packet_for_space(packet, 3)
        self.assertEqual(third['interior_camera_dna'], 'third dna')
        self.assertEqual(third['interior_primary_landmarks'], [{'name': 'third alcove end'}])

    def test_a_two_space_legacy_packet_still_works(self):
        packet = {'interior_camera_dna': 'primary dna',
                  'interior_primary_landmarks': [{'name': 'primary wall'}],
                  'secondary_interior_camera_dna': 'second dna',
                  'secondary_interior_primary_landmarks': [{'name': 'second bulkhead'}]}
        self.assertEqual(pp.packet_for_space(packet, 2)['interior_camera_dna'], 'second dna')
        # 第三个空间在这份老包里没有登记：退回主空间视图，绝不拿第二空间的地标顶上。
        self.assertEqual(pp.packet_for_space(packet, 3)['interior_camera_dna'], 'primary dna')


if __name__ == '__main__':
    unittest.main()
