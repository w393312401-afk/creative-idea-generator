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
import os
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import _beat_contract
from prompt_pipeline.composers import base as pp_base_composer


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


def _ladder_with_floor_sealed():
    """楼板/甲板组用的节拍梯：第 3 拍铺好底板（封住脚下那一面），第 4 拍硬切进室内。

    2026-07-30 之前楼板刻意不进词表，理由是「楼板开洞常是合法的室内工序」——理由没错，
    代价是踩穿地面这一整类倒退全交给偏向 under-report 的 LLM 审查。现在收进来，另配
    一套窄判据（只认结构性破口）+ 声明式开洞豁免。"""
    return [
        {'index': 1, 'operation': 'clearing', 'milestone_name': 'site cleared'},
        {'index': 2, 'operation': 'structure', 'milestone_name': 'floor joists sistered',
         'after_state': 'new sistered floor joists span the full bay, still open between them'},
        {'index': 3, 'operation': 'flooring', 'milestone_name': 'subfloor deck complete',
         'after_state': 'new plywood subfloor decking installed over the floor joists, screwed down'},
        {'index': 4, 'operation': 'threshold', 'hard_cut': True,
         'milestone_name': 'interior revealed'},
        {'index': 5, 'operation': 'clearing', 'milestone_name': 'interior cleared'},
    ]


class TestFloorSlabGroup(unittest.TestCase):
    """楼板/甲板组（2026-07-30 新增）。"""

    LADDER = _ladder_with_floor_sealed()

    def _check(self, prompt, i=5, ladder=None):
        return pp.check_envelope_seal_regression(
            prompt, i, self.LADDER if ladder is None else ladder, family='interior')

    def test_decking_over_joists_counts_as_sealed(self):
        """铺板句里几乎必然带 'joist'。骨架否决不给例外，这一组等于白加——
        盖板一铺上，脚下那一面就实了。"""
        sealed = pp.sealed_envelope_elements(self.LADDER, 5)
        self.assertIn('floor/deck slab', sealed)
        self.assertIn('subfloor deck complete', sealed['floor/deck slab'])

    def test_bare_joists_alone_do_not_count_as_sealed(self):
        """只把龙骨对齐/加固不是封楼板——这时脚下依然合法地是空的。"""
        ladder = [
            {'index': 1, 'operation': 'structure', 'milestone_name': 'floor joists sistered',
             'after_state': 'new sistered floor joists installed across the bay'},
            {'index': 2, 'operation': 'clearing', 'milestone_name': 'cleared'},
        ]
        self.assertNotIn('floor/deck slab', pp.sealed_envelope_elements(ladder, 2))

    def test_flags_a_reopened_floor_deck(self):
        prompt = ('Camera pitch locked level. '
                  'Underfoot the new floor deck shows a gaping hole with boards missing, '
                  'dropping into the dark below.')
        errs = self._check(prompt)
        self.assertEqual(len(errs), 1)
        self.assertIn('floor/deck slab', errs[0])

    def test_declared_opening_is_not_a_regression(self):
        """楼梯井/检修口/吊装口是合法的室内工序，正是当初把楼板排除在外的原因。"""
        for prompt in (
            'A framed opening is cut through the new floor deck for the stair.',
            'The floor deck carries a hatch opening with a gap around its frame.',
            'An access opening in the floor decking exposes the riser below.',
        ):
            self.assertEqual(self._check(prompt), [], prompt)

    def test_sky_wording_does_not_fire_on_the_floor(self):
        """透天透雨那套词对楼板没有物理意义：透过屋顶的天光洒在地板上是完全正常的
        写法，用宽表会把它算到楼板头上。"""
        prompt = 'A shaft of daylight from the still-open roof falls across the new floor deck.'
        self.assertEqual(self._check(prompt), [])

    def test_correct_raw_floor_underside_is_not_a_violation(self):
        prompt = ('Underfoot the new subfloor decking is bare unpainted plywood with '
                  'rows of screw heads, unfinished.')
        self.assertEqual(self._check(prompt), [])


class TestEnvelopeAdjacentSentencePair(unittest.TestCase):
    """相邻句对作用域（2026-07-30 新增）：把同一个陈述拆成两句、第二句用回指词接着
    说，是绕过逐句判定最顺手的写法。"""

    LADDER = _ladder_with_roof_sealed_before_cut()

    def _check(self, prompt):
        return pp.check_envelope_seal_regression(prompt, 5, self.LADDER, family='interior')

    def test_backref_split_is_caught(self):
        """逐句看两句都干净，合起来就是把已封的屋面写成了透天。"""
        prompt = ('Overhead the new black steel roof is complete and weathertight. '
                  'Beyond it, blue sky and the distant mountain ridge fill the frame above.')
        errs = self._check(prompt)
        self.assertEqual(len(errs), 1)
        self.assertIn('roof/ceiling', errs[0])

    def test_pronoun_backref_is_caught(self):
        prompt = ('The tower ceiling was closed with steel panels in an earlier beat. '
                  'It is torn open again right above the hearth.')
        self.assertEqual(len(self._check(prompt)), 1)

    def test_pair_scope_allows_the_group_naming_itself_in_the_second_sentence(self):
        """句对作用域按组分别判：下一句点到**本组**不否决（补语落在下一句正是这层
        判定要抓的形态），点到**别的**组才否决。"""
        prompt = ('A gaping hole is torn open overhead. '
                  'It is in the ceiling right above the workbench.')
        errs = self._check(prompt)
        self.assertEqual(len(errs), 1)
        self.assertIn('roof/ceiling', errs[0])

    def test_other_group_in_the_second_sentence_vetoes_conservatively(self):
        """下一句点到别的构件组一律否决，哪怕那个词只是顺带提到（"…show above the
        floor"）。这是明知会漏判的取舍：分不清敞开措辞到底属于哪个构件时，漏一条
        远好过把违规挂到错的构件上——回炉会照着错的判定去改对的句子。"""
        prompt = ('Overhead the new black steel roof is complete and weathertight. '
                  'Beyond it, blue sky and the distant ridge show above the floor.')
        self.assertEqual(self._check(prompt), [])

    def test_no_backref_opener_means_no_pair(self):
        """下一句不是回指式开头，就是在说另一件事——顶部已封写成一句、地面碎裂写成
        另一句仍然必须是合法写法（这条是原始口径，不能被相邻句对吃掉）。"""
        prompt = ('Overhead the new roof decking is closed and unpainted. '
                  'Rubble lies across the room where a collapsed shelf fell, '
                  'and a gaping hole opens in the old hearth surround.')
        self.assertEqual(self._check(prompt), [])

    def test_next_sentence_naming_another_element_is_not_paired(self):
        """下一句自己点了包络构件 → 它在说那件事，逐句判定已经覆盖，不跨句拼。"""
        prompt = ('Overhead the new roof is complete and weathertight. '
                  'Below it, the exterior wall stands unfinished with gaps between the stones.')
        # 外墙这一组本单没封过，所以正确结果是"无违规"；关键是不能把屋面判成违规
        self.assertNotIn('roof/ceiling', ' '.join(self._check(prompt)))

    def test_one_error_per_group_even_when_written_open_twice(self):
        """同一个构件在一稿里被写敞开两次，回炉要修的是同一件事，报两条只会让
        采纳门与日志噪声翻倍。"""
        prompt = ('Overhead the ceiling is torn open with a gaping hole. '
                  'Dirt lies in the corners. '
                  'The ceiling still shows blue sky through a missing section.')
        errs = self._check(prompt)
        self.assertEqual(len(errs), 1)


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
        self.assertIn('CONTINUITY, NOT A RESET', contract['anchor_rule'])
        self.assertIn('reset the CAMERA, not the', contract['anchor_rule'])
        # 2026-08-24：门后必须是废墟的那一半已删，剩下的只有防倒退。
        self.assertNotIn('at least three', contract['anchor_rule'].lower())
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
        self.assertNotIn('CONTINUITY, NOT A RESET', contract['anchor_rule'])

    def test_batch_system_prompt_carries_the_shared_rule(self):
        prompt = pp._batch_shared_system_prompt(self.PACKET, '', '')
        self.assertIn('SHARED-BOUNDARY (ENVELOPE) CROSS-VIEW MONOTONICITY', prompt)
        self.assertIn('ONE physical element with TWO', prompt)
        # 必须是插值进去的，不是把花括号原样印出来。
        self.assertNotIn('{ENVELOPE_CROSS_VIEW_RULE}', prompt)

    def test_both_instruction_blocks_interpolate_the_same_constant(self):
        """批量直出和单拍兜底是两份手抄的指令块——这条规则只加到其中一份就会漂移，
        而单拍兜底恰恰是批量那拍失败后走的路径，最需要它。

        2026-08-01 起两份指令块分居两个文件：批量那份仍在 prompt_pipeline/__init__.py
        （_batch_shared_system_prompt），单拍兜底那份随 Phase 2 一起搬进了
        prompt_pipeline/composers/base.py，在那边写作 pp.ENVELOPE_CROSS_VIEW_RULE。
        两处合计仍必须恰好两次——两份手抄件的漂移风险一点没变。"""
        sources = [pp.__file__, pp_base_composer.__file__]
        sites = []
        for path in sources:
            tree = ast.parse(open(path, encoding='utf-8').read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                for v in node.values:
                    if not isinstance(v, ast.FormattedValue):
                        continue
                    target = v.value
                    named = (isinstance(target, ast.Name) and target.id == 'ENVELOPE_CROSS_VIEW_RULE') or \
                            (isinstance(target, ast.Attribute) and target.attr == 'ENVELOPE_CROSS_VIEW_RULE')
                    if named:
                        sites.append((os.path.basename(path), node.lineno))
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
        prompt = pp._global_review_system_prompt()
        self.assertIn('ENVELOPE SEAL PERSISTENCE', prompt)
        self.assertIn('after a threshold crossing or a hard cut', prompt)

    def test_global_review_stays_cross_frame_only(self):
        """全局审查刻意只留跨帧规则（规则数量会稀释判断力）——新增这条不能把逐拍
        审查的工序/因果规则也带进来。"""
        prompt = pp._global_review_system_prompt()
        for local_only in ('SINGLE MILESTONE PACKAGE RULE', 'FLOOR-BEFORE-HEAVY-OBJECTS',
                           'DOOR COMPLETENESS'):
            self.assertNotIn(local_only, prompt)


if __name__ == '__main__':
    unittest.main()
