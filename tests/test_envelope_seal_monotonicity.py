"""包络体跨视角状态单调性（一个构件、两张面）：外景把屋面/外壳/门窗封上之后，
之后任何一张室内帧看到的都是同一个构件的背面，只能是「已封闭、里面这层还没装修」，
不可能又变回破洞透天。

2026-07-28 实测倒退：IMAGE 4 塔楼顶部已完工并封闭黑钢金属屋顶，IMAGE 5 室内视角顶部
仍呈破损开裂并直接透出蓝天与山脊，一致性审查判「施工状态未单调递增（状态倒退）」。
根因在生成侧两条契约打架——首现帧「未被触碰的创伤状态」硬性要求三类衰败（最顺手的
落点就是头顶），而过门/硬切重置镜头时把施工进度也一起重置了。

覆盖：
1. prompt_pipeline.sealed_envelope_elements —— 从节拍梯读出「本拍之前已封哪些构件」。
2. prompt_pipeline.check_envelope_seal_regression —— 室内帧文本确定性兜底。
3. prompt_pipeline.rework_envelope_seal_regression_beat —— 定向回炉的采纳门。
4. 契约/审查正文里的条文（两个骨架、两个指令块、两个审查提示词都必须带上）。
"""
import ast
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import _beat_contract


def _ladder_with_roof_sealed_before_cut():
    """内外双重完工（dual_payoff）的典型形态：外景第 3 拍封顶，第 4 拍硬切进室内。
    实测倒退就发生在第 4 拍产出的 IMAGE 5 上。"""
    return [
        {'index': 1, 'operation': 'clearing', 'milestone_name': 'site cleared',
         'after_state': 'loose debris hauled out of the courtyard'},
        {'index': 2, 'operation': 'repair', 'milestone_name': 'stone walls repointed',
         'after_state': 'every open joint packed with fresh lime mortar'},
        {'index': 3, 'operation': 'repair', 'milestone_name': 'tower top black steel metal roof complete',
         'after_state': 'the tower top is fully sealed with black steel metal roofing panels, weathertight'},
        {'index': 4, 'operation': 'threshold', 'hard_cut': True,
         'milestone_name': 'interior revealed'},
        {'index': 5, 'operation': 'clearing', 'milestone_name': 'interior floor cleared'},
    ]


class TestSealedEnvelopeElements(unittest.TestCase):

    def test_reads_sealed_roof_off_the_ladder(self):
        ladder = _ladder_with_roof_sealed_before_cut()
        sealed = pp.sealed_envelope_elements(ladder, 5)
        self.assertIn('roof/ceiling', sealed)
        self.assertIn('black steel metal roof', sealed['roof/ceiling'])

    def test_only_counts_strictly_earlier_beats(self):
        """封顶那一拍自己不算——它的 IMAGE 正是把屋面从破到封的那一张。"""
        ladder = _ladder_with_roof_sealed_before_cut()
        self.assertEqual(pp.sealed_envelope_elements(ladder, 3), {})
        self.assertIn('roof/ceiling', pp.sealed_envelope_elements(ladder, 4))

    def test_element_mentioned_without_a_seal_marker_does_not_count(self):
        """只是顺口提了屋顶（清理拍把屋面落下的碎瓦扫走）不等于把它封上了。"""
        ladder = [
            {'index': 1, 'operation': 'clearing', 'milestone_name': 'courtyard cleared',
             'after_state': 'roof slates that fell into the courtyard hauled away'},
            {'index': 2, 'operation': 'clearing', 'milestone_name': 'interior floor cleared'},
        ]
        self.assertEqual(pp.sealed_envelope_elements(ladder, 2), {})

    def test_erecting_skeleton_is_not_sealing(self):
        """装完天花龙骨是「结构立起来了」，不是「封上了」——这时头顶依然合法地敞着，
        之后的室内帧写成透天完全正确，判成倒退会把合法的裸骨架帧判死。"""
        ladder = [
            {'index': 1, 'operation': 'framing', 'milestone_name': 'ceiling joists complete',
             'after_state': 'all twelve ceiling joists installed and bolted, open to the sky above'},
            {'index': 2, 'operation': 'clearing', 'milestone_name': 'interior floor cleared'},
        ]
        self.assertEqual(pp.sealed_envelope_elements(ladder, 2), {})

    def test_seal_marker_must_sit_near_the_element(self):
        """同一拍里另一件事的「封闭」措辞不能算到屋面头上。"""
        ladder = [
            {'index': 1, 'operation': 'repair',
             'milestone_name': 'below-grade drain trench sealed watertight',
             'after_state': ('the drain trench along the north side is sealed watertight with '
                             'bitumen membrane and backfilled, while overhead the roof is left '
                             'exactly as found for a later phase')},
            {'index': 2, 'operation': 'clearing', 'milestone_name': 'interior floor cleared'},
        ]
        self.assertEqual(pp.sealed_envelope_elements(ladder, 2), {})

    def test_empty_and_missing_ladder_are_safe(self):
        self.assertEqual(pp.sealed_envelope_elements(None, 3), {})
        self.assertEqual(pp.sealed_envelope_elements([], 3), {})
        self.assertEqual(pp.sealed_envelope_elements(_ladder_with_roof_sealed_before_cut(), None), {})


class TestCheckEnvelopeSealRegression(unittest.TestCase):
    LADDER = _ladder_with_roof_sealed_before_cut()

    # 实测那一版 IMAGE 5 的文本形态：封好的屋面又被写成破损透天。
    REGRESSED = (
        'Camera pitch locked level; the central vanishing axis stays centered. '
        'Overhead, the tower ceiling is still torn open with a gaping hole, blue sky and the '
        'distant mountain ridge showing through. '
        'Dirt drifts lie in the corners.'
    )
    # 正确写法：外面封好、里面毛坯。
    SEALED_RAW_UNDERSIDE = (
        'Camera pitch locked level; the central vanishing axis stays centered. '
        'Overhead, the new black steel roof reads from beneath as bare decking on exposed '
        'rafters with rows of fastener heads, unpainted and unfinished. '
        'Rust streaks run down the walls and rubble lies where it fell across the floor.'
    )

    def _check(self, prompt, i=5, ladder=None, family='interior'):
        return pp.check_envelope_seal_regression(
            prompt, i, self.LADDER if ladder is None else ladder, family=family)

    def test_flags_the_real_regression(self):
        errs = self._check(self.REGRESSED)
        self.assertEqual(len(errs), 1)
        self.assertIn('roof/ceiling', errs[0])
        self.assertIn('black steel metal roof', errs[0])

    def test_sealed_outside_raw_inside_is_not_a_violation(self):
        """裸屋面板/外露椽条/螺钉排是「已封闭、内表面未装修」的正确读法，不能判违规——
        否则回炉会把它推回破洞透天，正好制造这条规则要防的倒退。"""
        self.assertEqual(self._check(self.SEALED_RAW_UNDERSIDE), [])

    def test_negated_clarification_clause_is_not_a_violation(self):
        """契约本身鼓励写澄清句，把它判成违规会把最合规的稿子判死。"""
        prompt = ('Overhead the sealed roof shows no holes and no sky through it; '
                  'the underside is bare decking.')
        self.assertEqual(self._check(prompt), [])

    def test_requires_same_sentence_co_occurrence(self):
        """顶部已封写成一句、地面碎裂写成另一句是完全合法的——跨句共现不算命中。"""
        prompt = ('Overhead the new roof decking is closed and unpainted. '
                  'Across the floor, rubble and a collapsed shelf lie where they fell, '
                  'with a gap between the floorboards.')
        self.assertEqual(self._check(prompt), [])

    def test_exterior_frames_are_out_of_scope(self):
        """外景帧看到的是同一构件的外面那层，另有里程碑校验管，这里不重复拦。"""
        self.assertEqual(self._check(self.REGRESSED, family='exterior'), [])

    def test_before_the_seal_beat_open_roof_is_correct(self):
        """封顶之前屋面本来就是破的——这时判违规就是把创伤状态给禁了。"""
        self.assertEqual(self._check(self.REGRESSED, i=3), [])

    def test_missing_ladder_or_prompt_is_safe(self):
        self.assertEqual(
            pp.check_envelope_seal_regression(self.REGRESSED, 5, None, family='interior'), [])
        self.assertEqual(
            pp.check_envelope_seal_regression('', 5, self.LADDER, family='interior'), [])


class TestReworkEnvelopeSealRegression(unittest.TestCase):
    LADDER = _ladder_with_roof_sealed_before_cut()

    def _errs(self, prompt):
        return pp.check_envelope_seal_regression(prompt, 5, self.LADDER, family='interior')

    def test_adopts_a_rewrite_that_re_passes(self):
        bad = TestCheckEnvelopeSealRegression.REGRESSED
        good = TestCheckEnvelopeSealRegression.SEALED_RAW_UNDERSIDE
        with patch.object(pp, '_chat', return_value=good):
            fixed, adopted = pp.rework_envelope_seal_regression_beat(
                {}, 5, bad, self._errs(bad), beat_ladder=self.LADDER)
        self.assertTrue(adopted)
        self.assertEqual(fixed, good)

    def test_rejects_a_rewrite_that_still_regresses(self):
        """回炉稿仍然透天就不采纳，保留原稿仅留痕——和其余 IMAGE 回炉同一套保守契约，
        永远不会比不回炉更糟。"""
        bad = TestCheckEnvelopeSealRegression.REGRESSED
        still_bad = ('Camera pitch locked level. Overhead the ceiling is torn open, '
                     'blue sky pouring through the hole.')
        with patch.object(pp, '_chat', return_value=still_bad):
            fixed, adopted = pp.rework_envelope_seal_regression_beat(
                {}, 5, bad, self._errs(bad), beat_ladder=self.LADDER)
        self.assertFalse(adopted)
        self.assertEqual(fixed, bad)

    def test_upstream_failure_keeps_the_original(self):
        bad = TestCheckEnvelopeSealRegression.REGRESSED
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            fixed, adopted = pp.rework_envelope_seal_regression_beat(
                {}, 5, bad, self._errs(bad), beat_ladder=self.LADDER)
        self.assertFalse(adopted)
        self.assertEqual(fixed, bad)


class TestEnvelopeContractText(unittest.TestCase):
    """文字契约侧：确定性兜底只逮住漏网的一稿，真正让模型别写错的是这些条文。"""

    PACKET = {'camera_dna': '', 'primary_landmarks': [], 'interior_primary_landmarks': [],
              'frame_boundaries': {}}

    def _contract(self, ladder, i):
        return _beat_contract(i, len(ladder), ladder, 'Threshold', self.PACKET, '')

    def test_first_interior_reveal_scopes_untouched_to_unworked_surfaces(self):
        """过门桥接（单线里程碑推进）的首现帧：「未被触碰」不能连已封构件一起重置。"""
        ladder = [
            {'index': 1, 'operation': 'clearing', 'milestone_name': 'site cleared'},
            {'index': 2, 'operation': 'repair', 'milestone_name': 'roof sealed weathertight',
             'after_state': 'the roof is fully sealed'},
            {'index': 3, 'operation': 'threshold', 'bridge_stage': 1,
             'milestone_name': 'camera settles inside'},
            {'index': 4, 'operation': 'clearing', 'milestone_name': 'interior floor cleared'},
        ]
        contract = self._contract(ladder, 3)
        self.assertTrue(contract['is_first_interior_reveal'])
        text = contract['anchor_rule'] + contract['family_contract']
        self.assertIn('SCOPE OF', contract['anchor_rule'])
        self.assertIn('reset the CAMERA, not the', contract['anchor_rule'])
        # 已封构件从里面看必须仍是封的，但内表面允许毛坯。
        self.assertIn('already CLOSED', contract['anchor_rule'])
        self.assertIn('unfinished never means still open', text.lower())

    def test_hard_cut_first_frame_carries_the_same_carve_out(self):
        """内外双重完工（dual_payoff）的硬切首帧走另一条 anchor_rule 分支，同样要带。"""
        ladder = _ladder_with_roof_sealed_before_cut()
        contract = self._contract(ladder, 4)
        self.assertTrue(contract['is_cut'])
        self.assertIn('resets the CAMERA ONLY', contract['anchor_rule'])
        self.assertIn('already closed', contract['anchor_rule'])

    def test_ordinary_interior_beats_do_not_get_the_first_reveal_clause(self):
        """这条只挂在首现帧上；普通室内拍走 STAGE SCOPE，不该被灌这段。"""
        ladder = _ladder_with_roof_sealed_before_cut()
        contract = self._contract(ladder, 5)
        self.assertFalse(contract['is_first_interior_reveal'])
        self.assertNotIn('SCOPE OF', contract['anchor_rule'])

    def test_batch_system_prompt_carries_the_shared_rule(self):
        prompt = pp._batch_shared_system_prompt(self.PACKET, '', '')
        self.assertIn('SHARED-BOUNDARY (ENVELOPE) CROSS-VIEW MONOTONICITY', prompt)
        self.assertIn('ONE physical element with TWO', prompt)
        # 必须是插值进去的，不是把花括号原样印出来。
        self.assertNotIn('{ENVELOPE_CROSS_VIEW_RULE}', prompt)

    def test_both_instruction_blocks_interpolate_the_same_constant(self):
        """批量直出和单拍兜底是两份手抄的指令块——这条规则只加到其中一份就会漂移，
        而单拍兜底恰恰是批量那拍失败后走的路径，最需要它。"""
        src = open(pp.__file__, encoding='utf-8').read()
        sites = [node.lineno for node in ast.walk(ast.parse(src))
                 if isinstance(node, ast.JoinedStr)
                 for v in node.values
                 if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)
                 and v.value.id == 'ENVELOPE_CROSS_VIEW_RULE']
        self.assertEqual(len(set(sites)), 2, f'期望两个指令块各引一次，实际 {sites}')


class TestEnvelopeReviewRules(unittest.TestCase):
    """审查侧：逐拍审查只看相邻两帧，封顶拍和倒退帧隔得远时只有全局稀疏审查能抓到。"""

    def test_local_beat_review_rejects_viewpoint_as_an_excuse(self):
        prompt = pp._local_beat_review_system_prompt()
        self.assertIn('A change of viewpoint is NEVER an excuse for a regression', prompt)
        self.assertIn('ONE element with two faces', prompt)
        # 毛坯内表面不能被判成违规，否则修复方向正好反了。
        self.assertIn('NOT a violation', prompt)

    def test_global_review_covers_the_non_adjacent_case(self):
        prompt = pp._global_review_system_prompt(10)
        self.assertIn('ENVELOPE SEAL PERSISTENCE', prompt)
        self.assertIn('after a threshold crossing or a hard cut', prompt)

    def test_global_review_stays_cross_frame_only(self):
        """全局审查刻意只留跨帧规则（规则数量会稀释判断力）——新增这条不能把逐拍
        审查的工序/因果规则也带进来。"""
        prompt = pp._global_review_system_prompt(10)
        for local_only in ('SINGLE MILESTONE PACKAGE RULE', 'FLOOR-BEFORE-HEAVY-OBJECTS',
                           'DOOR COMPLETENESS'):
            self.assertNotIn(local_only, prompt)


if __name__ == '__main__':
    unittest.main()
