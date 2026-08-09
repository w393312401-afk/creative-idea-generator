"""2026-08-02 复盘落地的几道新契约的回归测试。

覆盖：
  · 锚点生命周期（alive → transformed_into / retired），取代"只改百分比"的滚动校准
  · 大纲 ↔ milestone 的 1:1 绑定契约（覆盖率 / diff / 人物交付物）
  · 回读校验（reworked=True 必须以"重跑审计通过"为准）
  · delta 可见性预算
  · 模板层系统性违规的三处误报修复（切点表数字、profile 字数硬顶、固定尾句白名单）
  · 相机锁定块的二次复述去重
"""

import json
import re
import unittest
import unittest.mock

import prompt_pipeline as pp
from prompt_pipeline.composers import omni


PACKET = {
    'primary_landmarks': [
        {'name': 'row of original oval passenger portholes', 'grid': 'Grid B1',
         'z_depth_scale': '30%'},
        {'name': 'exposed curved aluminum ceiling ribs', 'grid': 'Grid A2',
         'z_depth_scale': '40%'},
        {'name': 'tail section bulkhead', 'grid': 'Grid B3', 'z_depth_scale': '55%'},
    ],
}


def _ladder():
    return [
        {'index': 1, 'operation': 'clearing', 'milestone_name': 'cabin cleared',
         'after_state': 'the floor is swept back to bare aluminium'},
        {'index': 2, 'operation': 'drywall', 'milestone_name': 'birch ceiling panels complete',
         'after_state': ('birch boards clad over the exposed curved aluminum ceiling ribs '
                         'across the full ceiling')},
        {'index': 3, 'operation': 'framing',
         'milestone_name': 'continuous lateral glazing band complete',
         'after_state': ('the row of original oval passenger portholes converted into a '
                         'continuous lateral glazing band')},
        {'index': 4, 'operation': 'flooring', 'milestone_name': 'hardwood flooring complete',
         'after_state': 'hardwood planks cover the full cabin floor'},
    ]


class TestAnchorLifecycle(unittest.TestCase):
    """锚点是随工序演进的，不是 before 态常量。"""

    def test_covered_anchor_retires_after_the_beat_that_covers_it(self):
        state = pp.anchor_lifecycle(PACKET, _ladder(), 3, 'exterior')
        self.assertIn('exposed curved aluminum ceiling ribs', state['retired'])
        self.assertNotIn('exposed curved aluminum ceiling ribs',
                         [lm['name'] for lm in state['active']])

    def test_anchor_is_alive_before_the_beat_that_covers_it(self):
        state = pp.anchor_lifecycle(PACKET, _ladder(), 2, 'exterior')
        self.assertEqual(state['retired'], [])
        self.assertIn('exposed curved aluminum ceiling ribs',
                      [lm['name'] for lm in state['active']])

    def test_transformed_anchor_rebinds_to_its_successor_keeping_grid_and_scale(self):
        state = pp.anchor_lifecycle(PACKET, _ladder(), 4, 'exterior')
        self.assertEqual(state['transformed'],
                         [{'from': 'row of original oval passenger portholes',
                           'into': 'continuous lateral glazing band'}])
        heir = next(lm for lm in state['active'] if lm['name'] == 'continuous lateral glazing band')
        self.assertEqual(heir['grid'], 'Grid B1')
        self.assertEqual(heir['z_depth_scale'], '30%')

    def test_declared_transitions_win_over_the_keyword_heuristic(self):
        ladder = [{'index': 1, 'operation': 'repair', 'milestone_name': 'x', 'after_state': 'y',
                   'anchor_transitions': [{'anchor': 'tail section bulkhead',
                                           'action': 'retired'}]}]
        state = pp.anchor_lifecycle(PACKET, ladder, 2, 'exterior')
        self.assertEqual(state['retired'], ['tail section bulkhead'])

    def test_locked_anchor_sentence_follows_the_lifecycle(self):
        view = pp.packet_with_anchor_lifecycle(PACKET, _ladder(), 4, 'exterior')
        clause = pp._canonical_anchor_clause(view['primary_landmarks'])
        self.assertIn('continuous lateral glazing band', clause)
        self.assertNotIn('exposed curved aluminum ceiling ribs', clause)
        self.assertNotIn('original oval passenger portholes', clause)

    def test_never_strips_the_family_down_to_zero_anchors(self):
        """全员退役时保持原样：一族一个锚点都不剩比锚点略微过时更糟。"""
        packet = {'primary_landmarks': [dict(PACKET['primary_landmarks'][1])]}
        view = pp.packet_with_anchor_lifecycle(packet, _ladder(), 4, 'exterior')
        self.assertEqual(view['primary_landmarks'], packet['primary_landmarks'])


class TestOutlineMilestoneContract(unittest.TestCase):
    """大纲是给人看的，milestone 是给图用的，中间必须有 1:1 契约校验。"""

    OUTLINE = ['清空舱内碎屑', '切割舱门装配入口梯', '铺设隐蔽水管与地暖', '点亮全景，人物入住']

    def _ladder(self, refs):
        ops = ['clearing', 'repair', 'rough-in', 'reward']
        return [{'index': i + 1, 'operation': ops[i], 'milestone_name': f'm{i + 1}',
                 'description': 'work', 'outline_refs': r}
                for i, r in enumerate(refs)]

    def test_full_coverage_reports_no_violation(self):
        ladder = self._ladder([[1], [2], [3], [4]])
        ladder[-1]['description'] = 'the occupant moves in and switches the lights on'
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, ladder), [])

    def test_a_dropped_outline_entry_is_reported_and_diffed(self):
        ladder = self._ladder([[1], [], [3], [4]])
        contract = pp.outline_milestone_contract(self.OUTLINE, ladder)
        self.assertEqual(contract['uncovered'], [(2, '切割舱门装配入口梯')])
        kinds = {d['kind'] for d in contract['diff']}
        self.assertIn('dropped', kinds)
        self.assertIn('added', kinds)
        self.assertTrue(any('切割舱门装配入口梯' in v
                            for v in pp.outline_contract_violations(self.OUTLINE, ladder)))

    def test_merging_is_allowed_but_recorded(self):
        ladder = self._ladder([[1, 2], [3], [], [4]])
        ladder[-1]['description'] = 'the occupant moves in'
        contract = pp.outline_milestone_contract(self.OUTLINE, ladder)
        self.assertEqual(contract['coverage'], 1.0)
        merged = [d for d in contract['diff'] if d['kind'] == 'merged']
        self.assertEqual(merged[0]['outline_refs'], [1, 2])
        # 合并本身不违规——它只是必须被看见
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, ladder), [])

    def test_ladder_without_declared_refs_is_not_judged(self):
        ladder = [{'index': 1, 'operation': 'clearing', 'milestone_name': 'm1'}]
        self.assertFalse(pp.outline_milestone_contract(self.OUTLINE, ladder)['declared'])
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, ladder), [])

    def test_occupancy_is_a_hard_deliverable(self):
        ladder = self._ladder([[1], [2], [3], [4]])
        ladder[-1]['description'] = 'a clean empty finished cabin under warm light'
        violations = pp.outline_contract_violations(self.OUTLINE, ladder)
        self.assertTrue(any('OCCUPANT' in v for v in violations))


class TestStructuredOutlineEntriesAreNormalized(unittest.TestCase):
    """P1-C 之后 beat_outline 的条目是 {op, text}。契约层直接 str(entry) 会把
    "{'op': 'reward', 'text': '点亮…'}" 这串 repr 喂回给规划器重排（server.log
    35609/35610），模型对不上是哪条草案，自愈那一轮就白跑了。"""

    OUTLINE = [{'op': 'clearing', 'text': '清空舱内碎屑'},
               {'op': 'repair', 'text': '切割舱门装配入口梯'},
               {'op': 'rough-in', 'text': '铺设隐蔽水管与地暖'},
               {'op': 'reward', 'text': '点亮全景，人物入住'}]

    def _ladder(self, refs):
        ops = ['clearing', 'repair', 'rough-in', 'reward']
        return [{'index': i + 1, 'operation': ops[i], 'milestone_name': f'm{i + 1}',
                 'description': 'work', 'outline_refs': r}
                for i, r in enumerate(refs)]

    def test_diff_carries_clean_text_not_a_dict_repr(self):
        contract = pp.outline_milestone_contract(self.OUTLINE, self._ladder(
            [[1], [], [3], [4]]))
        self.assertEqual(contract['uncovered'], [(2, '切割舱门装配入口梯')])
        dropped = next(d for d in contract['diff'] if d['kind'] == 'dropped')
        self.assertEqual(dropped['outline_texts'], ['切割舱门装配入口梯'])

    def test_repair_feedback_names_the_entry_in_plain_chinese(self):
        violations = pp.outline_contract_violations(self.OUTLINE, self._ladder(
            [[1], [], [3], [4]]))
        self.assertTrue(any('("切割舱门装配入口梯")' in v for v in violations))
        self.assertFalse(any("'op':" in v for v in violations))

    def test_occupancy_still_detected_through_the_structured_form(self):
        self.assertTrue(pp.outline_requires_occupancy(self.OUTLINE))
        # 旧的纯字符串形态不能因为这次归一而失效
        self.assertTrue(pp.outline_requires_occupancy(['点亮全景，人物入住']))
        self.assertFalse(pp.outline_requires_occupancy(
            [{'op': 'clearing', 'text': '清空舱内碎屑'}]))


class TestOutlineContractIsVisibleOnTheResultPage(unittest.TestCase):
    """映射 diff 此前只进 server.log 的 [OUTLINE] 行——用户照着卡片挑的选题，
    结果页上却看不出那条工序去哪了。"""

    OUTLINE = ['清空舱内碎屑', '切割舱门装配入口梯', '铺设隐蔽水管与地暖', '点亮全景，人物入住']

    def _contract(self, refs):
        ops = ['clearing', 'repair', 'rough-in', 'reward']
        ladder = [{'index': i + 1, 'operation': ops[i], 'milestone_name': f'm{i + 1}',
                   'outline_refs': r} for i, r in enumerate(refs)]
        return pp.outline_milestone_contract(self.OUTLINE, ladder)

    def test_undeclared_contract_renders_nothing(self):
        self.assertEqual(pp.render_outline_contract_md({'declared': False}), ("", ""))
        self.assertEqual(pp.render_outline_contract_md(None), ("", ""))

    def test_clean_one_to_one_mapping_says_so(self):
        md, note = pp.render_outline_contract_md(self._contract([[1], [2], [3], [4]]))
        self.assertIn('覆盖率 100%', md)
        self.assertIn('每条工序都由一拍单独交付', md)
        self.assertEqual(note, "")

    def test_merge_and_drop_are_both_spelled_out(self):
        md, note = pp.render_outline_contract_md(self._contract([[1, 2], [], [3], [4]]))
        self.assertIn('**合并**', md)
        self.assertIn('切割舱门装配入口梯', md)
        md2, note2 = pp.render_outline_contract_md(self._contract([[1], [], [3], [4]]))
        self.assertIn('⚠️ **未交付**', md2)
        self.assertIn('切割舱门装配入口梯', note2)

    def test_uncovered_note_survives_a_json_round_trip(self):
        """落盘再读回来时 (idx, text) 元组会变成两元素列表。"""
        contract = json.loads(json.dumps(self._contract([[1], [], [3], [4]])))
        _, note = pp.render_outline_contract_md(contract)
        self.assertIn('切割舱门装配入口梯', note)
        self.assertNotIn('[', note)


class TestDraftPlanIsNeverTruncated(unittest.TestCase):
    """卡片推荐 13 拍、滑块拨到 8 时，旧行为把中间那几条草案直接切掉——规划器不知道
    它们存在，覆盖率契约管不到，用户在弹窗里却还看得见完整清单。"""

    OUTLINE = [{'op': 'clearing', 'text': f'工序{i}'} for i in range(1, 14)] + [
        {'op': 'reward', 'text': '点亮全景，人物入住'}]

    def test_every_entry_reaches_the_planner_even_at_a_tight_cap(self):
        plan, block = pp.build_outline_plan_block(self.OUTLINE, 8)
        self.assertEqual(len(plan), len(self.OUTLINE))
        for entry in self.OUTLINE:
            self.assertIn(entry['text'], block)
        self.assertIn('14. 点亮全景，人物入住 [reward]', block)

    def test_over_budget_cap_no_longer_triggers_compression(self):
        """2026-08-07 起清单一比一还原：调用方（compose_anchor_and_packet 的
        _outline_strict 分支）保证 max_total_beats 恒等于 len(plan)，这个 cap 参数
        不再被用来压缩清单——不管传进来多小，块里要求的都是清单原长度，不再出现
        "BUDGET COMPRESSION" 或按预算合并的措辞。"""
        _, block = pp.build_outline_plan_block(self.OUTLINE, 8)
        self.assertNotIn('BUDGET COMPRESSION', block)
        self.assertNotIn('MERGE WIDTH LIMIT', block)
        self.assertIn(f'EXACTLY {len(self.OUTLINE)} elements', block)

    def test_a_draft_that_fits_carries_no_compression_note(self):
        _, block = pp.build_outline_plan_block(self.OUTLINE, 15)
        self.assertNotIn('BUDGET COMPRESSION', block)

    def test_one_to_one_contract_forbids_merging_regardless_of_cap(self):
        """一比一契约不再按拍数预算算"允许合并宽度"——不管 cap 多大/多小，块里都
        明说禁止合并/拆分/新增一拍；_max_merge_width 只服务没有清单的旧路径
        （outline_contract_violations 的非严格分支），不再被这个函数调用。"""
        for cap in range(4, 16):
            _, block = pp.build_outline_plan_block(self.OUTLINE, cap)
            self.assertIn('Never merge two entries onto one beat', block)
            self.assertIn('never split one entry across two beats', block)

    def test_no_draft_means_no_block_and_no_refs_demand(self):
        self.assertEqual(pp.build_outline_plan_block([], 10), ([], ""))
        self.assertEqual(pp.build_outline_plan_block(None, 10), ([], ""))

    def test_legacy_plain_string_entries_still_render(self):
        plan, block = pp.build_outline_plan_block(['清空舱内碎屑', '点亮入住'], 10)
        self.assertEqual(plan, [{'op': None, 'text': '清空舱内碎屑'},
                                {'op': None, 'text': '点亮入住'}])
        self.assertIn('1. 清空舱内碎屑\n', block)
        self.assertNotIn('[None]', block)

    def test_rich_fact_source_reaches_the_planner_verbatim(self):
        entry = {'op': 'flooring', 'text': '铺好毛毡与松木地板',
                 'en': 'grey wool felt underlay laid edge to edge, oiled pine planks nailed over it',
                 'mat': ['wool felt underlay', 'oiled pine planks']}
        plan, block = pp.build_outline_plan_block([entry], 1)
        self.assertEqual(plan[0]['en'], entry['en'])
        self.assertEqual(plan[0]['mat'], entry['mat'])
        self.assertIn(f'EN: {entry["en"]}', block)
        self.assertIn('MATERIALS: wool felt underlay, oiled pine planks', block)
        self.assertIn('copy that EN wording verbatim', block)


class TestMergeWidthGate(unittest.TestCase):
    """覆盖率 100% 不等于"卡片上那条工序看得见"：三条草案压进一拍照样是满分
    （server.log 33623）。合并仍然合法，但宽度有上界。"""

    OUTLINE = [f'工序{i}' for i in range(1, 9)]

    def _ladder(self, refs):
        return [{'index': i + 1,
                 'operation': 'threshold' if r == [] else 'repair',
                 'milestone_name': f'milestone {i + 1}', 'outline_refs': r}
                for i, r in enumerate(refs)]

    def test_two_way_merge_stays_legal(self):
        ladder = self._ladder([[1, 2], [3], [4], [5], [6], [7], [8]])
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, ladder), [])

    def test_three_way_merge_is_rejected_despite_full_coverage(self):
        ladder = self._ladder([[1, 2, 3], [4], [5], [6], [7], [8]])
        self.assertEqual(pp.outline_milestone_contract(self.OUTLINE, ladder)['coverage'], 1.0)
        violations = pp.outline_contract_violations(self.OUTLINE, ladder)
        self.assertEqual(len(violations), 1)
        self.assertIn('工序1、工序2、工序3', violations[0])
        self.assertIn('at most 2', violations[0])

    def test_a_tight_beat_budget_widens_the_allowance_instead_of_deadlocking(self):
        """拍数被压得很紧时闸门必须自动放宽——否则每一种排法都违规，
        三轮重排全废，最后掉进兜底 ladder，比合并更糟。"""
        ladder = self._ladder([[1, 2, 3], [4, 5, 6], [7, 8]])
        self.assertEqual(pp._max_merge_width(len(self.OUTLINE), ladder), 3)
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, ladder), [])

    def test_threshold_beats_do_not_count_as_carriers(self):
        """过门拍按契约允许留空，不该被算进"能认领的拍数"里去收紧闸门。"""
        with_threshold = self._ladder([[1, 2], [], [3], [4], [5], [6], [7], [8]])
        self.assertEqual(pp._max_merge_width(len(self.OUTLINE), with_threshold), 2)
        self.assertEqual(pp.outline_contract_violations(self.OUTLINE, with_threshold), [])


class TestOccupantSurvivesTheCleanFrameRule(unittest.TestCase):
    """通用的"无人场景"规则不能清掉用户点名要的人物交付物。"""

    BEAT = {'operation': 'reward', 'requires_occupant': True}

    def test_proactive_clean_frame_no_longer_rewrites_the_occupant(self):
        prompt = 'A person sits reading by the glazing band.'
        self.assertEqual(
            pp.fix_image_clean_frame_proactive(prompt, allow_occupant=True), prompt)
        self.assertIn('object', pp.fix_image_clean_frame_proactive(prompt))

    def test_clean_frame_check_allows_the_occupant_on_that_beat(self):
        prompt = 'A person sits reading by the glazing band.'
        self.assertEqual(pp.check_image_clean_frame(prompt, allow_occupant=True), [])
        self.assertTrue(pp.check_image_clean_frame(prompt))

    def test_missing_occupant_is_reported(self):
        errs = pp.check_occupant_delivered(
            'A finished cabin under warm light.', 'The camera drifts across the finished cabin.',
            self.BEAT)
        self.assertEqual(len(errs), 2)

    def test_sterile_declaration_collides_with_the_deliverable(self):
        errs = pp.check_occupant_delivered(
            'The occupant reads by the window.',
            'The occupant walks in; the frame is completely sterile of active workers.',
            self.BEAT)
        self.assertTrue(any('sterile' in e for e in errs))

    def test_beats_without_the_flag_are_untouched(self):
        self.assertEqual(pp.check_occupant_delivered('x', 'y', {'operation': 'reward'}), [])


class TestRepairWriteThenVerify(unittest.TestCase):
    """修复状态不能自报，必须以回读校验为准。"""

    def test_rework_with_residual_is_recorded_as_failed(self):
        config = {}
        pp.record_beat_audit(config, 16, [], ['x'], reworked=True, image_reworked=True,
                             milestone_name='final reveal',
                             residual=['Envelope regression: ...'])
        entry = config['_beat_audit'][0]
        self.assertEqual(entry['milestone_status'], 'rework_failed')
        self.assertIs(entry['repair_verified'], False)
        self.assertEqual(entry['residual'], ['Envelope regression: ...'])

    def test_clean_reverify_keeps_the_reworked_status(self):
        config = {}
        pp.record_beat_audit(config, 5, [], ['x'], reworked=True, milestone_name='floor',
                             residual=[])
        entry = config['_beat_audit'][0]
        self.assertEqual(entry['milestone_status'], 'reworked')
        self.assertIs(entry['repair_verified'], True)

    def test_payoff_regressions_block_only_the_final_beat(self):
        residual = ["Envelope regression: an earlier beat already closed the roof",
                    "Reward IMAGE must literally show 'converted row of portholes'"]
        self.assertEqual(len(pp.payoff_blocking_residual(residual, True)), 2)
        self.assertEqual(pp.payoff_blocking_residual(residual, False), [])

    def test_ordinary_residual_never_blocks(self):
        self.assertEqual(
            pp.payoff_blocking_residual(['IMAGE prompt word count (260) exceeds limit'], True), [])


class TestDeltaVisibilityBudget(unittest.TestCase):
    """变化面积打不过保持面积的拍，是注定的无效帧。"""

    def _beat(self, cells, **extra):
        beat = {'index': 6, 'operation': 'framing', 'milestone_name': 'wall top rails complete',
                'changed_grid_cells': cells}
        beat.update(extra)
        return beat

    def test_corner_only_delta_is_rejected(self):
        errs = pp.delta_visibility_violations([self._beat(['Grid A1', 'Grid A3'])])
        self.assertEqual(len(errs), 1)
        self.assertIn('camera_setup', errs[0])

    def test_centre_cross_delta_passes(self):
        self.assertEqual(pp.delta_visibility_violations([self._beat(['Grid A2'])]), [])

    def test_declaring_a_camera_setup_satisfies_the_budget(self):
        beat = self._beat(['Grid A1'], camera_setup='low upward angle onto the ceiling ribs')
        self.assertEqual(pp.delta_visibility_violations([beat]), [])

    def test_threshold_and_reward_beats_are_exempt(self):
        for op in ('threshold', 'reward'):
            beat = self._beat(['Grid A1'])
            beat['operation'] = op
            self.assertEqual(pp.delta_visibility_violations([beat]), [])


class TestTemplateLevelFalsePositives(unittest.TestCase):
    """模板层的系统性"违规"其实是默认产出本身，审计报的是契约不是错误。"""

    def test_shot_timeline_is_exempt_from_the_numeric_range_ban(self):
        ladder = omni.ladder_for(8, 'construction')
        sentence = omni.timeline_sentence(8, ladder)
        self.assertEqual(pp.check_nlvtr_violations(sentence), [])

    def test_a_real_numeric_range_still_fails(self):
        self.assertIn('Contains forbidden numeric range',
                      pp.check_nlvtr_violations('the gap widens 3 to 5 cm along the seam'))

    def test_video_word_ceiling_follows_the_profile(self):
        body = 'word ' * 450
        base_errs = pp.validate_beat_prompts(
            1, body, 'img', {}, 'Standard', False, False)
        self.assertTrue(any('VIDEO prompt word count' in e for e in base_errs))
        omni_errs = pp.validate_beat_prompts(
            1, body, 'img', {}, 'Standard', False, False, video_word_limit=510)
        self.assertFalse(any('VIDEO prompt word count' in e for e in omni_errs))

    def test_fixed_tail_sentences_are_whitelisted(self):
        text_policy = ('No captions, subtitles, floating labels, UI text, or rendered prompt '
                       'words appear anywhere in the image.')
        boundaries = ('Left wall curve, right wall curve, ceiling ribs at top, floor pan at '
                      'bottom.')
        for tail in (text_policy, boundaries):
            errs = pp.check_stylistic_repetition(
                tail + ' Pink batts now fill every floor bay.',
                tail + ' Birch boards now close the ceiling.',
                {}, is_video=False)
            self.assertEqual(errs, [], tail)


class TestOmniLadderOutAndIn(unittest.TestCase):
    """omni 跳过了 base 的时间戳版 out-and-in，得有自己的镜头梯版兜底。"""

    LADDER = omni.ladder_for(8, 'construction')

    def test_missing_entry_and_exit_are_filled_in_ladder_terms(self):
        fixed = omni.ensure_ladder_out_and_in(
            'A medium shot shows one lone worker fastening the birch boards.',
            self.LADDER, packet={})
        self.assertEqual(pp.check_out_and_in(fixed), [])
        self.assertEqual(pp.check_nlvtr_violations(fixed), [])
        self.assertNotIn('Grid', fixed)

    def test_it_is_idempotent(self):
        once = omni.ensure_ladder_out_and_in(
            'A medium shot shows one lone worker fastening boards.', self.LADDER, packet={})
        self.assertEqual(omni.ensure_ladder_out_and_in(once, self.LADDER, packet={}), once)

    def test_worker_free_clips_are_left_alone(self):
        sterile = 'The clip is completely sterile of workers throughout.'
        self.assertEqual(omni.ensure_ladder_out_and_in(sterile, self.LADDER), sterile)

    def test_threshold_and_reward_ladders_are_left_alone(self):
        body = 'A wide approach shot with one lone worker visible at the opening.'
        self.assertEqual(
            omni.ensure_ladder_out_and_in(body, omni.ladder_for(8, 'traversal'), packet={}), body)


class TestCameraBlockDedupe(unittest.TestCase):
    """一条 230–310 词的提示词里，相机锁定块不该占两遍。"""

    DNA = ('Static tripod shot, 24mm lens feel, camera height 1.6m, '
           'locked eye-level perspective.')

    def test_prose_restatement_is_dropped(self):
        prompt = (self.DNA + ' The camera frames the site in a static tripod shot, twenty-four '
                  'millimeter lens feel, camera height one point six meters, locked eye-level '
                  'perspective. Pink insulation batts fill the floor bays.')
        out = pp.dedupe_camera_declaration(prompt, self.DNA)
        self.assertTrue(out.startswith(self.DNA))
        self.assertNotIn('twenty-four millimeter', out)
        self.assertIn('Pink insulation batts', out)

    def test_content_sentences_that_mention_a_shot_survive(self):
        prompt = self.DNA + ' A wide shot of birch panels covering the ceiling.'
        self.assertEqual(pp.dedupe_camera_declaration(prompt, self.DNA), prompt)

    def test_no_dna_is_a_no_op(self):
        self.assertEqual(pp.dedupe_camera_declaration('anything', ''), 'anything')


class TestSlotCountsAreExplicit(unittest.TestCase):
    """before 帧是隐式插入的、不进 beats_count —— 这个约定必须显式化。"""

    def test_counts_are_derived_from_the_delivered_ladder(self):
        counts = pp.frame_slot_counts(16)
        self.assertEqual(counts['video_slots'], 16)
        self.assertEqual(counts['image_slots'], 17)
        self.assertEqual(counts['construction_beats'], 15)
        self.assertTrue(counts['before_frame_included'])

    def test_declared_mismatch_is_reported(self):
        self.assertIs(pp.frame_slot_counts(16, 15)['declared_matches_delivered'], True)
        self.assertIs(pp.frame_slot_counts(16, 16)['declared_matches_delivered'], False)

    def test_empty_ladder_is_all_zeroes(self):
        counts = pp.frame_slot_counts(0)
        self.assertEqual(counts['image_slots'], 0)
        self.assertFalse(counts['before_frame_included'])


class TestOutlineBindingIsFaithful(unittest.TestCase):
    """覆盖率只查**编号**：认领了第 3 条却交付完全不相干的工作，在
    outline_contract_violations 眼里是满分——用户在卡片上挑中的工序照样消失，而且
    这回连 diff 都看不出来（编号是对的）。outline_binding_violations 补的就是
    "认领是否名副其实"这三层：顺序、工序类型、英文复述。"""

    OUTLINE = [{'op': 'clearing', 'text': '清空舱内碎屑'},
               {'op': 'rough-in', 'text': '铺设隐蔽水管与地暖'},
               {'op': 'drywall', 'text': '封装内衬面板'},
               {'op': 'reward', 'text': '点亮全景，人物入住'}]

    def _beat(self, i, op, refs, delivery=None, package=None):
        return {'index': i, 'operation': op, 'milestone_name': f'm{i}',
                'description': 'work', 'package_operations': package or [op],
                'outline_refs': refs,
                'outline_delivery': (delivery if delivery is not None
                                     else [f'install the {op} layer across the whole bay'
                                           for _ in refs])}

    def _ladder(self, *specs):
        return [self._beat(i, *spec) for i, spec in enumerate(specs, 1)]

    def test_a_faithful_ladder_reports_nothing(self):
        ladder = self._ladder(('clearing', [1]), ('rough-in', [2]),
                              ('drywall', [3]), ('reward', [4]))
        self.assertEqual(pp.outline_binding_violations(self.OUTLINE, ladder), [])

    def test_claim_order_may_not_run_backwards(self):
        """卡片清单本身就是施工序，认领序倒退 = 悄悄重排了用户看到的工序表。"""
        ladder = self._ladder(('drywall', [3]), ('rough-in', [2]),
                              ('clearing', [1]), ('reward', [4]))
        violations = pp.outline_binding_violations(self.OUTLINE, ladder)
        self.assertTrue(any('never run backwards' in v for v in violations))
        self.assertTrue(any('铺设隐蔽水管与地暖' in v for v in violations))

    def test_merging_ahead_and_splitting_are_not_order_violations(self):
        """合并跳号、拆分重号都合法——只有真的倒退才算。"""
        merged = self._ladder(('clearing', [1, 2],
                               ['clear the debris out', 'lay the hidden pipe runs']),
                              ('drywall', [3]), ('reward', [4]))
        self.assertEqual([v for v in pp.outline_binding_violations(self.OUTLINE, merged)
                          if 'backwards' in v], [])
        split = self._ladder(('clearing', [1]), ('rough-in', [2]), ('drywall', [3]),
                             ('drywall', [3]), ('reward', [4]))
        self.assertEqual([v for v in pp.outline_binding_violations(self.OUTLINE, split)
                          if 'backwards' in v], [])

    def test_a_beat_must_actually_do_the_operation_it_claims(self):
        """跨语言内容校验的唯一确定性抓手：清单是中文、ladder 是英文，但工序类型
        枚举完全同源。认领 rough-in 的拍却在刷漆，这里就能抓住。"""
        ladder = self._ladder(('clearing', [1]), ('painting', [2]),
                              ('drywall', [3]), ('reward', [4]))
        violations = pp.outline_binding_violations(self.OUTLINE, ladder)
        self.assertTrue(any('rough-in' in v and '铺设隐蔽水管与地暖' in v
                            for v in violations))

    def test_the_claimed_operation_may_live_in_package_operations(self):
        """合并拍的主 operation 只能有一个，被吞并那条的工序类型落在包里就算数。"""
        ladder = self._ladder(('drywall', [2, 3],
                               ['run the hidden pipes', 'close the lining boards'],
                               ['rough-in', 'drywall']),
                              ('reward', [4]))
        ladder.insert(0, self._beat(0, 'clearing', [1]))
        self.assertEqual([v for v in pp.outline_binding_violations(self.OUTLINE, ladder)
                          if 'neither carries' in v], [])

    def test_threshold_and_reward_ops_are_exempt_from_the_operation_match(self):
        """过门/揭示拍的 operation 由过门拆分规则与 reward 规则各自钉死，
        认领它们的拍未必用同名 operation（过门常由 bridge_stage/hard_cut 表达）。"""
        ladder = self._ladder(('clearing', [1]), ('rough-in', [2]),
                              ('drywall', [3]), ('furnishing', [4]))
        self.assertEqual([v for v in pp.outline_binding_violations(self.OUTLINE, ladder)
                          if 'neither carries' in v], [])

    def test_every_claim_needs_an_english_restatement(self):
        ladder = self._ladder(('clearing', [1]), ('rough-in', [2], []),
                              ('drywall', [3]), ('reward', [4]))
        violations = pp.outline_binding_violations(self.OUTLINE, ladder)
        self.assertTrue(any('no matching "outline_delivery"' in v for v in violations))

    def test_placeholder_and_chinese_restatements_are_rejected(self):
        """复述是清单(中文)与提示词(英文)之间唯一的跨语言抓手：写成占位符或中文，
        下游 check_outline_delivery_realized 拿它去 IMAGE 正文里永远匹配不上。"""
        for bad in ('entry 2', '铺设隐蔽水管与地暖', 'work'):
            ladder = self._ladder(('clearing', [1]), ('rough-in', [2], [bad]),
                                  ('drywall', [3]), ('reward', [4]))
            violations = pp.outline_binding_violations(self.OUTLINE, ladder)
            self.assertTrue(any('no usable English content words' in v for v in violations),
                            f'{bad!r} 应该被判为不可用的复述')

    def test_rich_entry_material_names_may_not_be_replaced(self):
        outline = [
            {'op': 'flooring', 'text': '铺好毛毡与松木地板',
             'en': 'grey wool felt underlay laid edge to edge, oiled pine planks nailed over it',
             'mat': ['wool felt underlay', 'oiled pine planks']},
        ]
        ladder = self._ladder(('flooring', [1],
                               ['install moisture membrane beneath hardwood boards']))
        violations = pp.outline_binding_violations(outline, ladder)
        self.assertTrue(any('FIDELITY' in v and 'wool felt underlay' in v
                            and 'oiled pine planks' in v for v in violations))

    def test_mismatched_lengths_do_not_pair_a_restatement_with_the_wrong_entry(self):
        """outline_delivery 少一项时，剩下的必须按位对齐、缺的那条判缺失——
        错位地把第 3 条的复述配给第 2 条，等于把内容校验指向错误的工序。"""
        beat = self._beat(1, 'drywall', [2, 3], ['run the hidden pipe circuits'])
        paired = pp._beat_outline_delivery(beat, [2, 3])
        self.assertEqual(paired, {2: 'run the hidden pipe circuits'})

    def test_legacy_plain_string_outlines_are_not_judged_on_operations(self):
        """老形态条目没有 op，无从比对——跳过而不是硬判。"""
        outline = ['清空舱内碎屑', '铺设隐蔽水管', '点亮入住']
        ladder = self._ladder(('painting', [1]), ('lighting', [2]), ('reward', [3]))
        self.assertEqual([v for v in pp.outline_binding_violations(outline, ladder)
                          if 'neither carries' in v], [])

    def test_a_ladder_that_never_declared_refs_is_not_judged(self):
        ladder = [{'index': 1, 'operation': 'clearing', 'milestone_name': 'm1'}]
        self.assertEqual(pp.outline_binding_violations(self.OUTLINE, ladder), [])

    def test_an_unresolved_contract_is_called_out_in_the_audit_panel(self):
        """重排耗尽仍未满足是唯一"工序没落实、单子照发"的情形,面板上必须看得见,
        否则它和正常单长得一模一样。"""
        contract = {'declared': True, 'coverage': 1.0, 'uncovered': [], 'diff': [],
                    'unresolved': ['beat 2 claims card entry 3 but does no drywall work']}
        md, _ = pp.render_outline_contract_md(contract)
        self.assertIn('工序契约未满足', md)
        self.assertIn('beat 2 claims card entry 3', md)
        # 契约满足时这一段不出现
        clean, _ = pp.render_outline_contract_md(
            {'declared': True, 'coverage': 1.0, 'uncovered': [], 'diff': []})
        self.assertNotIn('工序契约未满足', clean)


class TestOutlineReachesThePromptComposer(unittest.TestCase):
    """节拍简介此前只影响 ladder 规划，到了逐拍提示词合成就完全消失——
    _milestone_beat_directive 只读 ladder 自己的字段。硬规则要成立，卡片工序原文
    必须一路带到合成阶段，并在合成之后被校验。"""

    OUTLINE = [{'op': 'clearing', 'text': '清空洞内碎冰与积雪'},
               {'op': 'reward', 'text': '点亮全景，人物入住'}]

    def _bound_ladder(self):
        ladder = [
            {'index': 1, 'operation': 'clearing', 'milestone_name': 'cavern floor cleared',
             'description': 'clear it out', 'stage_scope': 'large',
             'before_state': 'ice covers the floor', 'after_state': 'the entire floor is bare',
             'completion_extent': 'the whole cavern floor', 'changed_grid_cells': ['Grid B2'],
             'package_operations': ['clearing'], 'primary_progress': 'bare floor spreads',
             'secondary_progress': 'the spoil crate fills', 'preserve_state': 'walls untouched',
             'persistent_traces': ['shovel scrapes', 'melt puddles'],
             'outline_refs': [1],
             'outline_delivery': ['shovel out the broken cave ice and packed snow']},
            {'index': 2, 'operation': 'reward', 'milestone_name': 'reveal',
             'description': 'lights up', 'outline_refs': [2],
             'outline_delivery': ['warm lamps come up and the occupant settles in']},
        ]
        pp.bind_outline_to_ladder({}, self.OUTLINE, ladder)
        return ladder

    def test_binding_pins_the_card_text_onto_each_beat(self):
        ladder = self._bound_ladder()
        self.assertEqual(ladder[0]['outline_items'], [
            {'index': 1, 'text': '清空洞内碎冰与积雪',
             'delivery': 'shovel out the broken cave ice and packed snow'}])
        # 人物类交付物同时在 reward 拍上打标，让通用"无人干净帧"规则让路
        self.assertTrue(ladder[-1]['requires_occupant'])

    def test_beats_without_claims_carry_no_items(self):
        ladder = [{'index': 1, 'operation': 'threshold', 'outline_refs': []}]
        pp.bind_outline_to_ladder({}, self.OUTLINE, ladder)
        self.assertNotIn('outline_items', ladder[0])
        self.assertEqual(pp.beat_outline_items(ladder[0]), [])

    def test_the_directive_carries_both_the_chinese_source_and_the_english_gloss(self):
        ladder = self._bound_ladder()
        directive = pp.outline_delivery_directive(ladder[0])
        self.assertIn('CARD WORK ITEM(S) THIS BEAT DELIVERS (hard requirement', directive)
        self.assertIn('清空洞内碎冰与积雪', directive)
        self.assertIn('shovel out the broken cave ice and packed snow', directive)
        # 整块里程碑契约里也必须出现，且排在最前面
        block = pp._milestone_beat_directive(ladder[0])
        self.assertIn('清空洞内碎冰与积雪', block)
        self.assertLess(block.index('CARD WORK ITEM(S)'), block.index('Terminal stage product'))

    def test_no_items_leaves_the_directive_exactly_as_before(self):
        """老断点/手动填维度直出的梯子没有这个字段，措辞必须一字不变。"""
        bare = {'index': 1, 'operation': 'clearing', 'milestone_name': 'm',
                'before_state': 'a', 'after_state': 'b', 'completion_extent': 'c',
                'changed_grid_cells': [], 'package_operations': [], 'primary_progress': 'd',
                'secondary_progress': 'e', 'persistent_traces': [], 'preserve_state': 'f',
                'stage_scope': 'large'}
        self.assertEqual(pp.outline_delivery_directive(bare), "")
        self.assertTrue(pp._milestone_beat_directive(bare).startswith(
            'VISIBLE MILESTONE CONTRACT FOR THIS BEAT (mandatory):\n- Terminal stage product'))

    def test_an_image_that_never_delivers_the_claimed_work_is_flagged(self):
        ladder = self._bound_ladder()
        image = ('A static wide shot of the chamber. The rough limestone walls remain '
                 'untouched and the vaulted ceiling is unchanged.')
        errors = pp.check_outline_delivery_realized(image, ladder[0])
        self.assertEqual(len(errors), 1)
        self.assertIn('清空洞内碎冰与积雪', errors[0])

    def test_differently_worded_delivery_is_not_a_false_positive(self):
        """复述是规划阶段写的，合成阶段换个说法很正常——命中一个实义词就不算缺失，
        只抓"一个字都没提"那种（正是原始事故的形态）。"""
        ladder = self._bound_ladder()
        image = ('A static wide shot of the cavern floor, now scraped bare of ice, '
                 'with shovel marks fanning across the stone.')
        self.assertEqual(pp.check_outline_delivery_realized(image, ladder[0]), [])

    def test_rich_card_fact_source_outranks_planner_restatement(self):
        outline = [
            {'op': 'flooring', 'text': '铺好毛毡与松木地板',
             'en': 'grey wool felt underlay laid edge to edge, oiled pine planks nailed over it',
             'mat': ['wool felt underlay', 'oiled pine planks']},
        ]
        ladder = [{'index': 1, 'operation': 'flooring', 'milestone_name': 'replacement floor',
                   'outline_refs': [1],
                   'outline_delivery': ['install moisture membrane beneath hardwood boards']}]
        pp.bind_outline_to_ladder({}, outline, ladder)
        errors = pp.check_outline_delivery_realized(
            'The moisture membrane and hardwood boards cover the whole floor.', ladder[0])
        self.assertEqual(len(errors), 1)
        self.assertIn('铺好毛毡与松木地板', errors[0])

    def test_rich_fields_are_bound_without_changing_mixed_outline_indices(self):
        outline = [
            '清空碎屑',
            {'op': 'repair', 'text': '焊补钢板'},
            {'op': 'flooring', 'text': '铺好毛毡与松木地板',
             'en': 'grey wool felt underlay laid edge to edge, oiled pine planks nailed over it',
             'mat': ['wool felt underlay', 'oiled pine planks'],
             'zone': 'floor', 'scope': 'large', 'trace': 'pine seams'},
        ]
        ladder = [
            {'operation': 'clearing', 'outline_refs': [1], 'outline_delivery': ['clear debris']},
            {'operation': 'repair', 'outline_refs': [2], 'outline_delivery': ['weld steel plate']},
            {'operation': 'flooring', 'outline_refs': [3],
             'outline_delivery': [outline[2]['en']]},
        ]
        pp.bind_outline_to_ladder({}, outline, ladder)
        self.assertEqual([b['outline_items'][0]['index'] for b in ladder], [1, 2, 3])
        self.assertEqual(ladder[2]['outline_items'][0]['card_en'], outline[2]['en'])
        self.assertEqual(ladder[2]['outline_items'][0]['mat'], outline[2]['mat'])
        self.assertEqual(ladder[2]['outline_items'][0]['zone'], 'floor')
        self.assertEqual(ladder[2]['outline_items'][0]['scope'], 'large')
        self.assertEqual(ladder[2]['outline_items'][0]['trace'], 'pine seams')

    def test_item_less_crossing_beats_are_skipped(self):
        """老形态的纯过渡拍（一条工序都没认领）仍然豁免。"""
        beat = {'operation': 'threshold', 'bridge_stage': 1, 'outline_items': []}
        self.assertEqual(pp.check_outline_delivery_realized('nothing relevant here', beat), [])
        self.assertTrue(pp.outline_delivery_exempt(beat))

    def test_a_crossing_beat_that_claims_an_entry_is_still_checked(self):
        """一比一契约下过门拍自己也在交付一条真实工序（它的视频以穿越开场、紧接着
        做那件活），豁免掉它等于让整张卡上唯一一条工序没人查正文。"""
        beat = {'operation': 'threshold', 'bridge_stage': 1,
                'outline_items': [{'index': 1, 'text': '推开舱门铺入口踏板',
                                   'card_en': 'oak entry treads bolted across the hatch sill',
                                   'mat': ['oak entry treads'],
                                   'delivery': 'oak entry treads bolted across the hatch sill'}]}
        self.assertFalse(pp.outline_delivery_exempt(beat))
        errors = pp.check_outline_delivery_realized(
            'The camera pushes through the open hatch into the bare cabin.', beat)
        self.assertEqual(len(errors), 1)
        self.assertIn('推开舱门铺入口踏板', errors[0])
        self.assertEqual(pp.check_outline_delivery_realized(
            'Oak entry treads are bolted across the hatch sill.', beat), [])

    def test_every_declared_material_must_land_not_just_one(self):
        """卡片承诺两层材料，成片只画一层不算交付——每个 mat 独立判定。"""
        beat = {'operation': 'flooring',
                'outline_items': [{'index': 1, 'text': '铺好毛毡与松木地板',
                                   'card_en': 'grey wool felt underlay laid edge to edge, '
                                              'oiled pine planks nailed over it',
                                   'mat': ['wool felt underlay', 'oiled pine planks']}]}
        only_one = 'Oiled pine planks now cover the whole floor.'
        errors = pp.check_outline_delivery_realized(only_one, beat)
        self.assertEqual(len(errors), 1)
        self.assertEqual(pp.check_outline_delivery_realized(
            'A wool felt underlay runs edge to edge with oiled pine planks nailed over it.',
            beat), [])

    def test_a_ladder_without_binding_is_never_flagged(self):
        self.assertEqual(pp.check_outline_delivery_realized('anything', {'operation': 'repair'}), [])


class TestCardDeclaredBeatProperties(unittest.TestCase):
    """卡片自报的 scope / zone / trace 必须落到拍上。

    这三个下游字段（stage_scope / changed_grid_cells / persistent_traces）此前全部由
    规划器凭空发挥：猜错 scope 直接决定 IMAGE 用不用 "the entire" 措辞，猜错 zone 让
    相邻拍在完全不同区域反复横跳，漏掉 trace 就是「做完又消失」。"""

    OUTLINE = [
        {'op': 'clearing', 'text': '清空舱内碎屑',
         'en': 'loose debris and broken panels shoveled out of the steel cabin floor',
         'mat': ['broken panels'], 'zone': 'floor', 'scope': 'large'},
        {'op': 'flooring', 'text': '铺好毛毡与松木地板',
         'en': 'grey wool felt underlay laid edge to edge, oiled pine planks nailed over it',
         'mat': ['wool felt underlay', 'oiled pine planks'], 'zone': 'floor', 'scope': 'large',
         'trace': 'pine plank seams running lengthwise'},
    ]

    def _ladder(self, **overrides):
        ladder = [
            {'index': 1, 'operation': 'clearing', 'milestone_name': 'floor cleared',
             'stage_scope': 'large', 'changed_grid_cells': ['Grid B2', 'Grid C2'],
             'persistent_traces': ['shovel scrapes'], 'preserve_state': 'walls untouched',
             'outline_refs': [1],
             'outline_delivery': ['broken panels shoveled out of the cabin floor']},
            {'index': 2, 'operation': 'flooring', 'milestone_name': 'pine floor complete',
             'stage_scope': 'large', 'changed_grid_cells': ['Grid B2'],
             'persistent_traces': ['pine plank seams running lengthwise', 'nail heads'],
             'preserve_state': 'walls untouched', 'outline_refs': [2],
             'outline_delivery': ['grey wool felt underlay laid edge to edge, oiled pine '
                                  'planks nailed over it']},
        ]
        for pos, patch in overrides.items():
            ladder[int(pos)].update(patch)
        return ladder

    def test_a_conforming_ladder_raises_nothing(self):
        self.assertEqual(pp.outline_binding_violations(self.OUTLINE, self._ladder()), [])

    def test_stage_scope_must_equal_the_card_declared_coverage(self):
        errors = pp.outline_binding_violations(
            self.OUTLINE, self._ladder(**{'1': {'stage_scope': 'small'}}))
        self.assertEqual(len(errors), 1)
        # 错误文案要同时点名两个值，喂回重排时才自愈得了
        self.assertIn('scope="large"', errors[0])
        self.assertIn('stage_scope="small"', errors[0])

    def test_same_zone_beats_must_share_a_grid_cell(self):
        errors = pp.outline_binding_violations(
            self.OUTLINE, self._ladder(**{'1': {'changed_grid_cells': ['Grid A1']}}))
        self.assertEqual(len(errors), 1)
        self.assertIn('zone "floor"', errors[0])

    def test_the_card_declared_trace_must_land_on_its_own_beat(self):
        errors = pp.outline_binding_violations(
            self.OUTLINE, self._ladder(**{'1': {'persistent_traces': ['nail heads']}}))
        self.assertEqual(len(errors), 1)
        self.assertIn('pine plank seams running lengthwise', errors[0])

    def test_the_next_beat_must_not_drop_the_trace(self):
        outline = [dict(self.OUTLINE[0], trace='bare steel floor pan showing shovel scrapes'),
                   self.OUTLINE[1]]
        ladder = self._ladder()
        ladder[0]['persistent_traces'] = ['bare steel floor pan showing shovel scrapes']
        ladder[1]['preserve_state'] = 'walls untouched'
        errors = pp.outline_binding_violations(outline, ladder)
        self.assertEqual(len(errors), 1)
        self.assertIn('drops the residue', errors[0])
        # 下一拍把它写进 preserve_state 就算接住了
        ladder[1]['preserve_state'] = 'the bare steel floor pan stays visible at the edges'
        self.assertEqual(pp.outline_binding_violations(outline, ladder), [])

    def test_legacy_cards_without_these_fields_are_untouched(self):
        legacy = [{'op': 'clearing', 'text': '清空舱内碎屑'},
                  {'op': 'flooring', 'text': '铺好松木地板'}]
        ladder = self._ladder(**{'0': {'stage_scope': 'small', 'changed_grid_cells': ['Grid A1'],
                                       'persistent_traces': []},
                                 '1': {'outline_delivery': ['pine planks nailed down']}})
        ladder[0]['outline_delivery'] = ['debris shoveled out']
        self.assertEqual(pp.outline_binding_violations(legacy, ladder), [])

    def test_the_plan_block_hands_the_planner_these_properties(self):
        _, block = pp.build_outline_plan_block(self.OUTLINE, len(self.OUTLINE))
        self.assertIn('zone: floor', block)
        self.assertIn('scope: large', block)
        self.assertIn('LEAVES: pine plank seams running lengthwise', block)
        self.assertIn('COVERAGE:', block)
        self.assertIn('ZONE:', block)
        self.assertIn('LEAVES:', block)

    def test_a_legacy_plan_block_never_mentions_them(self):
        """老卡凭空看见一段"把 stage_scope 设成清单里的 scope"只会让规划器编值。"""
        _, block = pp.build_outline_plan_block(
            [{'op': 'clearing', 'text': '清空舱内碎屑'}], 1)
        self.assertNotIn('CARD-DECLARED BEAT PROPERTIES', block)

    def test_the_frame_review_names_the_materials_and_the_trace(self):
        ladder = self._ladder()
        pp.bind_outline_to_ladder({}, self.OUTLINE, ladder)
        block = pp.outline_frame_review_block(ladder[1]['outline_items'])
        self.assertIn('"oiled pine planks"', block)
        self.assertIn('in the floor zone', block)
        self.assertIn('pine plank seams running lengthwise', block)


class TestOutlineDeliveryLedger(unittest.TestCase):
    """卡片工序交付总账：一条工序一行，贯穿规划 / 合成 / 渲帧。

    此前三个阶段各自留痕，但数据散在 _outline_contract（按工序号，只到规划期）、
    _beat_audit（按拍，条目是回炉流水）、manifest.quality_gate（按帧，自由文本）三处，
    谁也回答不了用户最该看见的那一句：「我照着挑的这几条工序，最后落实了几条」。"""

    OUTLINE = [{'op': 'clearing', 'text': '清空洞内碎冰与积雪'},
               {'op': 'repair', 'text': '修补岩壁裂缝并除锈'},
               {'op': 'rough-in', 'text': '铺设隐蔽水管与地暖'},
               {'op': 'drywall', 'text': '封装内衬面板'},
               {'op': 'framing', 'text': '切割舱门装配入口梯'},
               {'op': 'reward', 'text': '点亮全景，人物入住'}]

    DELIVERY = {
        1: 'shovel out the broken cave ice and packed snow',
        2: 'patch the rock wall fissures and strip the rust',
        3: 'run the hidden pipe circuits and underfloor heating loops',
        4: 'close the birch lining boards over the studs',
        5: 'cut the hatch opening and bolt the entry ladder',
        6: 'warm lamps come up and the occupant settles in',
    }

    def _beat(self, pos, op, refs, package=None):
        return {'index': pos, 'operation': op, 'milestone_name': f'm{pos}',
                'description': 'work', 'package_operations': package or [op],
                'outline_refs': list(refs),
                'outline_delivery': [self.DELIVERY[n] for n in refs]}

    def _bound(self, *specs):
        """specs = (operation, refs[, package_operations])，返回 (config, ladder)。"""
        ladder = [self._beat(pos, *spec) for pos, spec in enumerate(specs, 1)]
        config = {}
        pp.bind_outline_to_ladder(config, self.OUTLINE, ladder)
        return config, ladder

    def _ledger(self, config, ladder, prompt_audit=None, frame_verdicts=None):
        return pp.build_outline_delivery_ledger(
            ladder, config.get('_outline_contract'),
            prompt_audit=prompt_audit if prompt_audit is not None
            else config.get('_outline_prompt_audit'),
            frame_verdicts=frame_verdicts)

    def test_ledger_indexes_every_card_entry_once(self):
        """合并/拆分都不许让总账多一行或少一行——这张表是按**卡片工序**索引的，
        一条工序在卡片上只有一行，落到几拍是它自己那一行里的信息。"""
        config, ladder = self._bound(
            ('clearing', [1, 2]),                      # 合并
            ('rough-in', [3]),
            ('drywall', [4]),
            ('framing', [5]), ('framing', [5]),        # 拆分
            ('reward', [6]))
        ledger = self._ledger(config, ladder)
        self.assertEqual([r['index'] for r in ledger], [1, 2, 3, 4, 5, 6])
        self.assertEqual([r['text'] for r in ledger],
                         [e['text'] for e in self.OUTLINE])
        merged = next(r for r in ledger if r['index'] == 2)
        self.assertEqual(merged['plan_verdict'], 'merged')
        self.assertEqual(merged['claimed_beats'], [1])
        split = next(r for r in ledger if r['index'] == 5)
        self.assertEqual(split['plan_verdict'], 'split')
        self.assertEqual(split['claimed_beats'], [4, 5])
        # 落点是**到达帧**（beat + 1），用户在帧网格上照着这个号找图
        self.assertEqual(split['frame_seqs'], [5, 6])

    def test_dropped_entry_shows_as_unclaimed_with_no_downstream_verdict(self):
        """没落点就无从谈交付：把它报成 prompt/frame missing 是误导——那会读成
        "写了没做到"，实际是"压根没人认领"。"""
        config, ladder = self._bound(
            ('clearing', [1]), ('repair', [2]), ('rough-in', [3]),
            ('drywall', [4]), ('reward', [6]))
        row = next(r for r in self._ledger(config, ladder) if r['index'] == 5)
        self.assertEqual(row['plan_verdict'], 'dropped')
        self.assertEqual(row['claimed_beats'], [])
        self.assertEqual(row['prompt_verdict'], '')
        self.assertEqual(row['frame_verdict'], '')
        self.assertIn('没有任何一拍认领', row['note'])
        # 面板上这一行两列都是破折号，不是"未交付"
        md = pp.render_outline_delivery_md(self._ledger(config, ladder))
        self.assertIn('⚠️ 无人认领', md)
        self.assertIn('| 5 | 切割舱门装配入口梯 | ⚠️ 无人认领 | — | — |', md)

    def test_reworked_prompt_is_distinguishable_from_first_pass(self):
        """回炉后通过 ≠ 一次过。合成期这两种结局在 _beat_audit 里是同一个
        reworked 布尔，按工序索引之后必须分得开。"""
        config, ladder = self._bound(('clearing', [1]), ('repair', [2]),
                                     ('rough-in', [3]), ('drywall', [4]),
                                     ('framing', [5]), ('reward', [6]))
        first_pass = ('the cave floor is shovelled bare of broken ice and packed snow, '
                      'meltwater pooling in the scrapes')
        # 第 1 拍一次过；第 2 拍首轮缺失、回炉后写进去了；第 3 拍回炉后仍然没写
        pp.record_outline_delivery(config, 1, first_pass, ladder[0])
        pp.record_outline_delivery(config, 2, 'the rock wall fissures are patched and derusted',
                                   ladder[1], missing_before=[2])
        pp.record_outline_delivery(config, 3, 'nothing relevant to that work at all', ladder[2])
        verdicts = {r['index']: r['prompt_verdict'] for r in self._ledger(config, ladder)}
        self.assertEqual(verdicts[1], 'delivered')
        self.assertEqual(verdicts[2], 'reworked')
        self.assertEqual(verdicts[3], 'missing')
        # 一拍都没记过的工序留空（未知），不是"没交付"
        self.assertEqual(verdicts[6], '')
        md = pp.render_outline_delivery_md(self._ledger(config, ladder))
        self.assertIn('♻️ 回炉后通过', md)

    def test_hidden_layer_entry_is_marked_not_applicable(self):
        """「铺设隐蔽水管与地暖」按施工顺序必然被下一拍封板盖住：到达帧判"看不见"
        是对的观察、错的结论。这一档必须确定性置位，不能交给 VLM 去判。"""
        config, ladder = self._bound(
            ('clearing', [1]), ('repair', [2]), ('rough-in', [3]),
            ('drywall', [4]), ('framing', [5]), ('reward', [6]))
        rows = {r['index']: r for r in self._ledger(config, ladder)}
        self.assertEqual(rows[3]['frame_verdict'], 'not_applicable')
        self.assertIn('封盖后不可见', rows[3]['note'])
        # 同为隐蔽层族，但下一拍不封板 → 照常送审
        self.assertEqual(rows[5]['frame_verdict'], 'unreviewed')
        # 已经审过的判定照常落位
        seen = self._ledger(config, ladder, frame_verdicts={'1': 'visible', '2': 'missing'})
        self.assertEqual([r['frame_verdict'] for r in seen][:2], ['visible', 'missing'])

    def test_a_floor_pour_seals_hidden_work_just_like_a_wall_board(self):
        """封盖不只有封板一种。2026-08-06 实测一张 7 拍岗亭卡时暴露的缺口：
        「铺设防潮膜与隐蔽地暖管」→「浇筑微水泥自流平地板」，地暖管被自流平彻底埋掉，
        与被封板盖住是同一件事，却因为封盖表里只有 drywall 而被送去画面层判"没交付"。"""
        outline = [{'op': 'clearing', 'text': '清空岗亭内积砂'},
                   {'op': 'rough-in', 'text': '铺设防潮膜与隐蔽地暖管'},
                   {'op': 'flooring', 'text': '浇筑微水泥自流平地板'},
                   {'op': 'reward', 'text': '点亮黄铜壁灯揭示暖阁'}]
        delivery = ['sweep the drifted sand out of the booth',
                    'lay the damp-proof membrane and buried heating loops',
                    'pour the polished microcement levelling floor',
                    'warm brass lamps come up on the finished refuge']
        ladder = [{'index': i, 'operation': e['op'], 'milestone_name': f'm{i}',
                   'description': 'work', 'package_operations': [e['op']],
                   'outline_refs': [i], 'outline_delivery': [delivery[i - 1]]}
                  for i, e in enumerate(outline, 1)]
        config = {}
        pp.bind_outline_to_ladder(config, outline, ladder)
        rows = {r['index']: r for r in pp.build_outline_delivery_ledger(
            ladder, config.get('_outline_contract'))}
        self.assertEqual(rows[2]['frame_verdict'], 'not_applicable')
        self.assertIn('封盖后不可见', rows[2]['note'])
        # 地板自己看得见，照常送审
        self.assertEqual(rows[3]['frame_verdict'], 'unreviewed')

    def test_only_burying_operations_count_as_sealing(self):
        """面漆盖的是饰面，不埋任何隐蔽层——它进封盖表就会把一整批可判工序误标成
        「本来就看不见」，比漏判更糟。"""
        def _next_op_seals(op):
            ladder = [{'index': 1, 'operation': 'rough-in', 'package_operations': ['rough-in']},
                      {'index': 2, 'operation': op, 'package_operations': [op]}]
            return pp._outline_hidden_layer_beat(ladder, 1)

        for sealing in ('drywall', 'flooring', 'priming'):
            self.assertTrue(_next_op_seals(sealing), f'{sealing} 应当算封盖')
        for open_op in ('painting', 'lighting', 'furnishing', 'clearing'):
            self.assertFalse(_next_op_seals(open_op), f'{open_op} 不该算封盖')
        # 链尾的隐蔽层没有"下一拍"，一律照常送审
        self.assertFalse(pp._outline_hidden_layer_beat(
            [{'index': 1, 'operation': 'rough-in', 'package_operations': ['rough-in']}], 1))

    def test_ledger_survives_a_ladder_without_outline_items(self):
        """老断点 / 手填维度直出 / 根本没有卡片清单的老单：那条链路整个没跑过，
        返回空账——凭空拼一张"全部未交付"的表是误导，不是留痕。"""
        bare = [{'index': 1, 'operation': 'clearing', 'milestone_name': 'm1'},
                {'index': 2, 'operation': 'reward', 'milestone_name': 'm2'}]
        self.assertEqual(pp.build_outline_delivery_ledger(bare, {}), [])
        self.assertEqual(pp.build_outline_delivery_ledger(bare, None), [])
        self.assertEqual(pp.build_outline_delivery_ledger(None, None), [])
        self.assertEqual(pp.build_outline_delivery_ledger(bare, {}, outline=[]), [])
        self.assertEqual(pp.render_outline_delivery_md([]), "")
        self.assertEqual(pp.outline_delivery_log_line([]), "")
        self.assertEqual(pp.outline_delivery_alert([]), "")
        self.assertEqual(pp.stash_outline_delivery_ledger({}, bare), [])

    def test_a_card_whose_list_was_never_claimed_is_the_loudest_row_not_an_empty_table(self):
        """2026-08-06 实测：规划四轮全败 → deterministic_fallback_beat_ladder 上一个
        outline_refs 都没有 → 总账为空 → 体检表/日志/落盘全部静默。可这恰恰是**整张卡
        的工序被通用施工序整体换掉**、最该报警的一单，绝不能和"这单本来就没有清单"
        同样处理。"""
        fallback_ladder = [{'index': i, 'operation': op, 'milestone_name': f'stage {i}'}
                           for i, op in enumerate(['clearing', 'repair', 'reward'], 1)]
        ledger = pp.build_outline_delivery_ledger(fallback_ladder, {}, outline=self.OUTLINE)
        self.assertEqual(len(ledger), len(self.OUTLINE))
        self.assertTrue(all(r['plan_verdict'] == 'dropped' for r in ledger))
        self.assertTrue(all(r['claimed_beats'] == [] for r in ledger))
        # 下游两列一律留空：没落点就无从谈交付（同 dropped 的既有口径）
        self.assertTrue(all(r['prompt_verdict'] == '' and r['frame_verdict'] == ''
                            for r in ledger))
        # 面板表头必须点明这单退回了通用施工序，而不是只列一堆"无人认领"
        md = pp.render_outline_delivery_md(ledger)
        self.assertIn('规划未采纳这份清单', md)
        self.assertIn('有拍认领 **0/6**', md)
        # 并且顶到 repair_md 上（非 PASS 开头 → 前端审核面板自动展开高亮）
        alert = pp.outline_delivery_alert(ledger)
        self.assertIn('规划未采纳灵感卡片的工序清单', alert)
        self.assertIn('6 条工序', alert)
        self.assertFalse(alert.upper().startswith('PASS'))

    def test_a_partially_delivered_card_does_not_raise_the_fallback_alarm(self):
        """部分未交付由 render_outline_contract_md 的 uncovered_note 报过一次，
        这条警报只管"一条都没认领"，否则两句互相稀释。"""
        config, ladder = self._bound(
            ('clearing', [1]), ('repair', [2]), ('rough-in', [3]),
            ('drywall', [4]), ('reward', [6]))          # 第 5 条无人认领
        ledger = self._ledger(config, ladder)
        self.assertEqual(pp.outline_delivery_alert(ledger), "")
        self.assertNotIn('规划未采纳这份清单', pp.render_outline_delivery_md(ledger))

    def test_audit_table_renders_all_four_verdict_symbols(self):
        rows = [{'index': i, 'text': f'工序{i}', 'delivery': '', 'claimed_beats': [i],
                 'frame_seqs': [i + 1], 'plan_verdict': 'claimed',
                 'prompt_verdict': 'delivered', 'frame_verdict': v, 'note': ''}
                for i, v in enumerate(['visible', 'missing', 'unreviewed', 'not_applicable'], 1)]
        md = pp.render_outline_delivery_md(rows)
        self.assertIn('卡片工序交付体检（4 条）', md)
        for symbol in ('✅', '⚠️ 画面里看不到', '— 未审查', '➖ 隐蔽工序，封盖后不可见'):
            self.assertIn(symbol, md)
        self.assertIn('第 1 拍 → 帧 2', md)

    def test_the_audit_log_line_is_the_regression_yardstick(self):
        """硬规则上线后没有回归观测：这一行就是可累积的口径。"""
        config, ladder = self._bound(
            ('clearing', [1]), ('repair', [2]), ('rough-in', [3]),
            ('drywall', [4]), ('reward', [6]))
        pp.record_outline_delivery(
            config, 1, 'the cave floor is shovelled bare of broken ice and packed snow', ladder[0])
        line = pp.outline_delivery_log_line(self._ledger(config, ladder), 'nested_space_payoff')
        self.assertIn('[OUTLINE-AUDIT] skeleton=nested_space_payoff entries=6', line)
        self.assertIn('plan=5/6', line)     # 第 5 条无人认领
        self.assertIn('prompt=1/6', line)
        self.assertIn('frame=0/6', line)    # 灰度期画面一列还没数据
        self.assertIn('na=1', line)         # 隐蔽工序占 1 条


class TestOutlineReachesTheFrameReview(unittest.TestCase):
    """P2：让工序原文抵达帧审查层。四道关里前三道判的都是提示词文本，帧渲染之后的
    VLM 审查完全不知道卡片工序的存在——「IMAGE 正文写了躺椅、渲出来的图里没有躺椅」
    这一类，文本层判通过、画面层压根没在查。"""

    LEDGER = [
        {'index': 1, 'text': '清空洞内碎冰与积雪',
         'delivery': 'shovel out the broken cave ice and packed snow',
         'claimed_beats': [1], 'frame_seqs': [2], 'plan_verdict': 'claimed',
         'prompt_verdict': 'delivered', 'frame_verdict': 'unreviewed', 'note': ''},
        {'index': 2, 'text': '修补岩壁裂缝并除锈',
         'delivery': 'patch the rock wall fissures and strip the rust',
         'claimed_beats': [1], 'frame_seqs': [2], 'plan_verdict': 'merged',
         'prompt_verdict': 'delivered', 'frame_verdict': 'unreviewed', 'note': ''},
    ]

    PROMPT_BLOCK = ('图片 1: an untouched ice cave\n'
                    '图片 2: the cave floor scraped bare\n'
                    '视频 1: the ice is shovelled out\n')

    def test_manifest_carries_outline_items_for_the_review_stage(self):
        """审查是用户手动触发的独立入口，跟合成那次运行不在同一个进程生命周期里：
        工序原文必须落盘，且要经得起 JSON 往返（int 键会变字符串）。"""
        by_beat = pp.outline_items_by_beat(self.LEDGER)
        self.assertEqual(sorted(by_beat), ['1'])
        self.assertEqual([i['text'] for i in by_beat['1']],
                         ['清空洞内碎冰与积雪', '修补岩壁裂缝并除锈'])
        round_tripped = json.loads(json.dumps(by_beat, ensure_ascii=False))
        self.assertEqual(len(pp._outline_items_for_beat(round_tripped, 1)), 2)
        self.assertEqual(pp._outline_items_for_beat(round_tripped, 9), [])
        self.assertEqual(pp.outline_items_by_beat([]), {})

    def _run_beat_review(self, response, outline_out=None):
        captured = {}

        def _fake_multimodal(config, system, user, images, **kwargs):
            captured['system'], captured['user'] = system, user
            return response

        with unittest.mock.patch.object(pp, '_multimodal_chat', _fake_multimodal), \
                unittest.mock.patch.object(pp, '_cached_blind_spot_block', lambda force=False: ''):
            issues = pp.check_beat_consistency(
                {}, self.PROMPT_BLOCK, 1, 1, 'a.webp', 'b.webp',
                outline_items=pp.outline_items_by_beat(self.LEDGER)['1'],
                outline_out=outline_out)
        return issues, captured

    def test_beat_review_prompt_carries_the_card_work_items(self):
        _, captured = self._run_beat_review('[]')
        self.assertIn('CARD WORK ITEM(S) THIS BEAT MUST DELIVER', captured['user'])
        self.assertIn('清空洞内碎冰与积雪', captured['user'])
        self.assertIn('shovel out the broken cave ice and packed snow', captured['user'])
        self.assertIn('CARD WORK NOT DELIVERED', captured['user'])
        # 老单（没有总账）时这一段整个不出现，措辞与改造前逐字相同
        with unittest.mock.patch.object(pp, '_multimodal_chat',
                                        lambda *a, **k: '[]'), \
                unittest.mock.patch.object(pp, '_cached_blind_spot_block', lambda force=False: ''):
            pp.check_beat_consistency({}, self.PROMPT_BLOCK, 1, 1, 'a.webp', 'b.webp')
        self.assertEqual(pp.outline_frame_review_block([]), "")

    def test_frame_verdict_is_observed_but_never_flags_while_gray(self):
        """灰度期（_OUTLINE_FRAME_GATE_ENFORCING=False）：判定只进总账出参，
        不进 failures / quality_gate 那条通路，一个字都不影响既有判定。"""
        self.assertFalse(pp._OUTLINE_FRAME_GATE_ENFORCING)
        response = json.dumps([
            '构件在两帧之间凭空更换了材质',
            'CARD WORK NOT DELIVERED: 修补岩壁裂缝并除锈 — patch the rock wall fissures '
            'and strip the rust',
        ], ensure_ascii=False)
        out = {}
        issues, _ = self._run_beat_review(response, outline_out=out)
        # 普通违规照常返回；工序判定被摘出来，不进复核/failures
        self.assertEqual(issues, ['构件在两帧之间凭空更换了材质'])
        self.assertEqual(out, {'1': 'visible', '2': 'missing'})

    def test_the_gate_reports_normally_once_enforcing(self):
        """开关翻成 True 时它就是一条普通违规，走既有的复核 → failures 通路。"""
        response = json.dumps(['CARD WORK NOT DELIVERED: 清空洞内碎冰与积雪'],
                              ensure_ascii=False)
        with unittest.mock.patch.object(pp, '_OUTLINE_FRAME_GATE_ENFORCING', True):
            issues, _ = self._run_beat_review(response)
        self.assertEqual(len(issues), 1)
        self.assertIn('CARD WORK NOT DELIVERED', issues[0])

    def test_an_unattributable_report_is_dropped_rather_than_misfiled(self):
        """认不出说的是哪条工序时宁可这条记不上账——把 A 的"没交付"记到 B 头上，
        比漏记一条更糟。"""
        items = pp.outline_items_by_beat(self.LEDGER)['1']
        verdicts = pp.outline_frame_verdicts(items, ['CARD WORK NOT DELIVERED: 说不清是哪一条'])
        self.assertEqual(verdicts, {'1': 'visible', '2': 'visible'})
        # 复述换了说法也认得出来（实义词最佳匹配）
        loose = pp.outline_frame_verdicts(
            items, ['CARD WORK NOT DELIVERED: the rock fissures were never patched'])
        self.assertEqual(loose['2'], 'missing')

    def test_review_results_merge_keeps_the_worst_frame_verdict(self):
        first = {'failures': {}, 'issues': [], 'unreviewed_beats': [2],
                 'global_unreviewed_beats': [], 'global_reviewed': True,
                 'global_attempted': True, 'outline_frame_verdicts': {'1': 'missing'}}
        second = {'failures': {}, 'issues': [], 'unreviewed_beats': [],
                  'global_unreviewed_beats': [], 'global_reviewed': True,
                  'global_attempted': True, 'outline_frame_verdicts': {'1': 'visible',
                                                                       '2': 'visible'}}
        merged = pp.merge_review_results(first, second)
        self.assertEqual(merged['outline_frame_verdicts'], {'1': 'missing', '2': 'visible'})


if __name__ == '__main__':
    unittest.main()
