"""2026-08-02 复盘落地的几道新契约的回归测试。

覆盖：
  · 锚点生命周期（alive → transformed_into / retired），取代"只改百分比"的滚动校准
  · 大纲 ↔ milestone 的 1:1 绑定契约（覆盖率 / diff / 人物交付物）
  · 回读校验（reworked=True 必须以"重跑审计通过"为准）
  · delta 可见性预算
  · 模板层系统性违规的三处误报修复（切点表数字、profile 字数硬顶、固定尾句白名单）
  · 相机锁定块的二次复述去重
"""

import unittest

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


if __name__ == '__main__':
    unittest.main()
