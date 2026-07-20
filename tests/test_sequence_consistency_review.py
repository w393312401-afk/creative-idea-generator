"""渲染后整套序列一致性审查（替代已去掉的逐帧质检门 + 合成阶段文本审核循环）：

1. prompt_pipeline.check_full_sequence_consistency —— 多模态调用返回的 JSON 解析。
2. prompt_pipeline.fix_beat_from_sequence_review —— 单拍定向重写。
3. pipeline_orchestrator._sequence_consistency_review —— 审查→改→重渲的编排循环，
   最多 2 轮，轮次耗尽仍有问题的帧标 'sequence_review_flagged'。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server_common
import prompt_pipeline as pp
import pipeline_orchestrator as po


class TestSequenceReviewSystemPromptStageScope(unittest.TestCase):
    """回归防护：GLOBAL STAGE DELTA veto 已被 STAGE SCOPE FIDELITY / DRIFT 两条规则取代
    （见 stage_scope 三档配额改动），确保这两条规则文本不会被后续改动误删。"""

    def test_stage_scope_rules_present_and_old_veto_gone(self):
        prompt = pp._sequence_review_system_prompt(10)
        self.assertIn('STAGE SCOPE FIDELITY', prompt)
        self.assertIn('STAGE SCOPE DRIFT & STALLED PROGRESSION', prompt)
        self.assertIn('MORE THAN ONE beat', prompt)
        self.assertNotIn('GLOBAL STAGE DELTA', prompt)


class TestCheckFullSequenceConsistency(unittest.TestCase):
    def test_empty_prompt_block_or_no_frames_short_circuits(self):
        self.assertEqual(pp.check_full_sequence_consistency({}, '', {1: 'a.webp'}), {})
        self.assertEqual(pp.check_full_sequence_consistency({}, 'some prompt', {}), {})

    def test_clean_json_response_means_no_failures(self):
        with patch.object(pp, '_multimodal_chat', return_value='{}') as chat:
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a.webp', 2: 'b.webp'})
        self.assertEqual(failures, {})
        chat.assert_called_once()
        # 图片路径按 sequence 升序传入，且系统提示词按 total_beats 生成
        self.assertEqual(chat.call_args.args[3], ['a.webp', 'b.webp'])

    def test_violations_are_parsed_and_beat_indices_coerced(self):
        raw = json.dumps({'2': ['天花板未随墙面一起封板'], '5': ['视角跳切']})
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b', 3: 'c'})
        # total_beats = 2 (3 images - 1); beat 5 is out of range and must be dropped
        self.assertEqual(failures, {2: ['天花板未随墙面一起封板']})

    def test_out_of_range_and_non_list_entries_are_dropped(self):
        raw = json.dumps({'1': ['ok'], '99': ['out of range'], 'x': 'not a list'})
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertEqual(failures, {1: ['ok']})

    def test_malformed_json_returns_none_not_pass(self):
        # 响应不可解析 = 审查没跑成，必须返回 None 哨兵；返回 {} 会被当"审查通过"
        # 盖 sequence_reviewed_pass 假章（2026-07-15 盐湖贝壳单事故根源）
        with patch.object(pp, '_multimodal_chat', return_value='not json at all'):
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertIsNone(failures)

    def test_multimodal_chat_exception_returns_none_not_pass(self):
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertIsNone(failures)

    def test_degraded_mode_extends_timeout_and_still_parses(self):
        with patch.object(pp, '_multimodal_chat', return_value='{}') as chat, \
             patch.object(pp, '_compress_frames_for_review', side_effect=lambda paths, **kw: paths) as comp:
            failures = pp.check_full_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'}, degraded=True)
        self.assertEqual(failures, {})
        comp.assert_called_once()
        self.assertEqual(chat.call_args.kwargs.get('timeout'), 180)


class TestFixBeatFromSequenceReview(unittest.TestCase):
    def test_no_issues_returns_inputs_unchanged_without_calling_llm(self):
        with patch.object(pp, '_chat', side_effect=AssertionError('should not be called')):
            v, i = pp.fix_beat_from_sequence_review({}, 'video body', 'image body', [])
        self.assertEqual((v, i), ('video body', 'image body'))

    def test_successful_rewrite_applies_deterministic_cleanup(self):
        raw = json.dumps({'video': 'new video body', 'image': 'new image body'})
        with patch.object(pp, '_chat', return_value=raw), \
             patch.object(pp, 'clean_prompt_text', side_effect=lambda s: s), \
             patch.object(pp, 'fix_image_clean_frame_proactive', side_effect=lambda s: s):
            v, i = pp.fix_beat_from_sequence_review(
                {}, 'old video', 'old image', ['天花板未封板'])
        self.assertEqual(v, 'new video body')
        self.assertEqual(i, 'new image body')

    def test_malformed_response_returns_inputs_unchanged(self):
        with patch.object(pp, '_chat', return_value='not json'):
            v, i = pp.fix_beat_from_sequence_review({}, 'old video', 'old image', ['issue'])
        self.assertEqual((v, i), ('old video', 'old image'))

    def test_llm_exception_returns_inputs_unchanged(self):
        with patch.object(pp, '_chat', side_effect=RuntimeError('boom')):
            v, i = pp.fix_beat_from_sequence_review({}, 'old video', 'old image', ['issue'])
        self.assertEqual((v, i), ('old video', 'old image'))


class _TmpProjectCase(unittest.TestCase):
    TITLE = 'sequence_review_test_project'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_output_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = server_common._get_project_dir(self.TITLE)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_output_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_frame(self, seq):
        with open(po._frame_path(self.TITLE, seq), 'wb') as f:
            f.write(b'fake webp bytes')

    def _read_manifest(self):
        return server_common.read_manifest(self.project_dir) or {}

    PROMPT_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame\n\n图片 2:\nsecond frame\n\n图片 3:\nthird frame\n\n"
        "视频提示词\n视频 1:\nvideo one\n\n视频 2:\nvideo two\n"
    )


class TestSequenceConsistencyReview(_TmpProjectCase):
    def test_zero_or_one_beat_prompt_block_is_a_noop(self):
        result = po._sequence_consistency_review({}, self.TITLE, '', self.project_dir)
        self.assertEqual(result, '')
        self.assertEqual(self._read_manifest(), {})

    def test_missing_frames_skips_review_without_touching_manifest(self):
        # 只渲了第 1 帧，第 2/3 帧还没落盘——不该在没审过的情况下把它们标"已通过"
        self._touch_frame(1)
        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=AssertionError('should not run review with missing frames')):
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        self.assertEqual(result, self.PROMPT_BLOCK)
        self.assertEqual(self._read_manifest(), {})

    def test_clean_review_marks_all_frames_reviewed_pass(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency', return_value={}) as mock_check, \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        mock_check.assert_called_once()
        mock_render.assert_not_called()  # 没有问题，不该触发任何重渲
        self.assertEqual(result, self.PROMPT_BLOCK)
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass', 3: 'sequence_reviewed_pass'})

    def test_failure_triggers_fix_and_targeted_rerender_then_converges(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        # Round 1: beat 1 flagged. Round 2: clean.
        check_results = [{1: ['天花板未封板']}, {}]

        def fake_check(config, prompt_block, frame_paths):
            return check_results.pop(0)

        render_calls = []

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None):
            render_calls.append(target_sequences)

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check), \
             patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')) as mock_fix, \
             patch.object(po, 'generate_frame_sequence', side_effect=fake_render):
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        mock_fix.assert_called_once_with({}, 'video one', 'second frame', ['天花板未封板'])
        self.assertEqual(render_calls, [[2]])  # beat 1 -> IMAGE 2 only, no full re-render
        self.assertIn('fixed video 1', result)
        self.assertIn('fixed image 2', result)
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass', 3: 'sequence_reviewed_pass'})

    def test_exhausting_rounds_flags_remaining_beats_with_reason(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        # Every round reports the same violation on beat 2, and the fixer keeps producing
        # a genuinely new (round-numbered) rewrite each time so the loop never converges
        # on its own — it must be the max_rounds cap that stops it, not the no-change exit.
        fix_calls = {'n': 0}

        def fake_fix(config, video_prompt, image_prompt, issues):
            fix_calls['n'] += 1
            return f'changed video r{fix_calls["n"]}', f'changed image r{fix_calls["n"]}'

        with patch.object(po, 'check_full_sequence_consistency',
                          return_value={2: ['视角跳切，中间没有回拉镜头过渡']}), \
             patch.object(po, 'fix_beat_from_sequence_review', side_effect=fake_fix), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review(
                {}, self.TITLE, self.PROMPT_BLOCK, self.project_dir, max_rounds=2)

        self.assertEqual(mock_render.call_count, 2)  # one rerender per round, capped at max_rounds
        gates_by_seq = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(gates_by_seq[3]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('视角跳切', gates_by_seq[3]['vlm_qa_reason'])
        self.assertEqual(gates_by_seq[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(gates_by_seq[2]['quality_gate'], 'sequence_reviewed_pass')

    def test_final_rework_is_verified_before_stamping_pass(self):
        """末轮回炉重渲出来的帧必须再审一次才盖章：此前末轮"回炉重渲→不复核→直接按
        重渲前的失败理由标 flagged"——重渲结果从未被验证（2026-07-17 拱渡槽单实锤）。
        本例最后一次重渲真的修好了，复核轮应把全部帧盖 sequence_reviewed_pass。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        check_results = [{1: ['问题A']}, {1: ['问题A仍在']}, {}]
        fix_calls = {'n': 0}

        def fake_fix(config, video_prompt, image_prompt, issues):
            fix_calls['n'] += 1
            return f'changed video r{fix_calls["n"]}', f'changed image r{fix_calls["n"]}'

        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=lambda *a, **kw: check_results.pop(0)) as mock_check, \
             patch.object(po, 'fix_beat_from_sequence_review', side_effect=fake_fix), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review(
                {}, self.TITLE, self.PROMPT_BLOCK, self.project_dir, max_rounds=2)

        self.assertEqual(mock_check.call_count, 3)   # 2 轮回炉 + 1 轮末位复核
        self.assertEqual(mock_render.call_count, 2)  # 回炉重渲仍以 max_rounds 为上限
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass',
                                 3: 'sequence_reviewed_pass'})

    def test_flag_reason_comes_from_final_verification_not_stale_round(self):
        """复核轮仍失败时，flagged 理由必须描述重渲后的真实画面（末轮复核给出的），
        而不是重渲前旧帧的过期理由。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        check_results = [{1: ['round1 旧理由']}, {1: ['round2 旧理由']}, {1: ['复核后仍存在的问题']}]
        fix_calls = {'n': 0}

        def fake_fix(config, video_prompt, image_prompt, issues):
            fix_calls['n'] += 1
            return f'changed video r{fix_calls["n"]}', f'changed image r{fix_calls["n"]}'

        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=lambda *a, **kw: check_results.pop(0)), \
             patch.object(po, 'fix_beat_from_sequence_review', side_effect=fake_fix), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review(
                {}, self.TITLE, self.PROMPT_BLOCK, self.project_dir, max_rounds=2)

        self.assertEqual(mock_render.call_count, 2)  # 复核轮绝不再触发新的回炉重渲
        gates_by_seq = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(gates_by_seq[2]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('复核后仍存在的问题', gates_by_seq[2]['vlm_qa_reason'])
        self.assertNotIn('旧理由', gates_by_seq[2]['vlm_qa_reason'])

    def test_exhausting_time_budget_flags_remaining_beats_with_reason(self):
        """2026-07-16：max_rounds 限的是轮数，不是耗时——网关限流下每轮重渲的退避
        可能比轮数本身慢得多（实测真实链跑了超过 1 小时未收敛）。time_budget_seconds
        应该在总耗时超预算时提前熔断，即使 max_rounds 还没用完；round 1 必须真的
        跑完（不是被总时长熔断在第一轮就拦住），round 2 才因为超预算被跳过。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        fix_calls = {'n': 0}

        def fake_fix(config, video_prompt, image_prompt, issues):
            fix_calls['n'] += 1
            return f'changed video r{fix_calls["n"]}', f'changed image r{fix_calls["n"]}'

        # started_at=0；round1 起始检查 10s（<100 预算，正常跑）；round2 起始检查 200s
        # （>100 预算，熔断，round2 连 check_full_sequence_consistency 都不该调用）。
        with patch.object(po.time, 'time', side_effect=[0, 10, 200]), \
             patch.object(po, 'check_full_sequence_consistency',
                          return_value={2: ['视角跳切，中间没有回拉镜头过渡']}) as mock_check, \
             patch.object(po, 'fix_beat_from_sequence_review', side_effect=fake_fix), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review(
                {}, self.TITLE, self.PROMPT_BLOCK, self.project_dir,
                max_rounds=5, time_budget_seconds=100)

        mock_check.assert_called_once()  # round 1 really ran the review
        self.assertEqual(mock_render.call_count, 1)  # round 2's rerender never started
        gates_by_seq = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(gates_by_seq[3]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('视角跳切', gates_by_seq[3]['vlm_qa_reason'])
        self.assertEqual(gates_by_seq[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(gates_by_seq[2]['quality_gate'], 'sequence_reviewed_pass')

    def test_review_unavailable_marks_frames_skipped_not_pass(self):
        """审查两次（常规+降级）都没跑成：帧必须标 sequence_review_skipped，绝不能
        盖 sequence_reviewed_pass 假章；且第二次调用必须带 degraded=True。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        calls = []

        def fake_check(config, prompt_block, frame_paths, degraded=False):
            calls.append(degraded)
            return None

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        self.assertEqual(calls, [False, True])  # 常规一次 + 降级重试一次，然后放弃
        mock_render.assert_not_called()
        self.assertEqual(result, self.PROMPT_BLOCK)
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_review_skipped', 2: 'sequence_review_skipped',
                                 3: 'sequence_review_skipped'})

    def test_degraded_retry_success_after_first_timeout_still_reviews(self):
        """第一次调用失败、降级重试成功且无违规：正常盖 sequence_reviewed_pass。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        results = [None, {}]

        def fake_check(config, prompt_block, frame_paths, degraded=False):
            return results.pop(0)

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        mock_render.assert_not_called()
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass',
                                 3: 'sequence_reviewed_pass'})

    def test_no_actual_change_from_fix_stops_the_loop_early(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value={1: ['issue the LLM cannot actually fix']}), \
             patch.object(po, 'fix_beat_from_sequence_review',
                          side_effect=lambda config, v, i, issues: (v, i)) as mock_fix, \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir, max_rounds=2)

        mock_fix.assert_called_once()
        mock_render.assert_not_called()  # no rerender since nothing actually changed
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates.get(2), 'sequence_review_flagged')


if __name__ == '__main__':
    unittest.main()
