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
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

import server_common
import frame_generator
import candidate_selection_pipeline as csp
import prompt_pipeline as pp
import pipeline_orchestrator as po
from frame_generator import QuotaExhaustedError


class TestSequenceReviewSystemPromptMilestones(unittest.TestCase):
    """回归防护：整套实图审查必须鼓励每拍显著完成，而不是恢复旧的一拍大变化配额。
    2026-07-23 改版后这些规则活在局部逐拍审查里（见 _local_beat_review_system_prompt），
    全局稀疏审查（_global_review_system_prompt）只保留跨帧规则，不应包含它们。"""

    def test_local_system_prompt_is_beat_independent_and_cacheable(self):
        """2026-07-25：拍号移出 system prompt（规则正文改用 IMAGE A / IMAGE B 别名），
        整单所有逐拍调用共用同一份前缀，prompt 缓存才可能命中。"""
        with patch.object(pp, '_multimodal_chat', return_value='[]') as chat:
            pp.check_beat_consistency({}, 'prompt block', 2, 9, 'a', 'b')
            pp.check_beat_consistency({}, 'prompt block', 7, 9, 'c', 'd')
            first, second = chat.call_args_list[0].args[1], chat.call_args_list[1].args[1]
        self.assertEqual(first, second)  # 逐拍之间 system prompt 必须逐字一致

        prompt = pp._local_beat_review_system_prompt()
        self.assertNotIn('beat 2 of', prompt)
        self.assertIn('IMAGE A', prompt)     # 本拍两张图改用稳定别名指代
        self.assertIn('IMAGE B', prompt)
        # 但必须要求模型在输出里用真实帧号，否则人在帧网格里对不上是哪一张
        self.assertIn('real names', prompt)

    def test_beat_number_and_final_beat_flag_live_in_the_user_turn(self):
        with patch.object(pp, '_multimodal_chat', return_value='[]') as chat:
            pp.check_beat_consistency({}, 'prompt block', 3, 10, 'a.webp', 'b.webp')
            user_text = chat.call_args.args[2]
        self.assertIn('beat 3 of 10', user_text)
        self.assertIn('IMAGE 3', user_text)
        self.assertIn('IMAGE 4', user_text)
        self.assertIn('is NOT the final beat', user_text)

        with patch.object(pp, '_multimodal_chat', return_value='[]') as chat:
            pp.check_beat_consistency({}, 'prompt block', 10, 10, 'a.webp', 'b.webp')
        self.assertIn('IS the final beat', chat.call_args.args[2])

    def test_visible_milestone_and_package_rules_present_in_local_prompt(self):
        prompt = pp._local_beat_review_system_prompt()
        self.assertIn('VISIBLE MILESTONE FIDELITY', prompt)
        self.assertIn('SINGLE MILESTONE PACKAGE RULE', prompt)
        self.assertIn('DUAL PROGRESS FIDELITY', prompt)
        self.assertIn('Multiple decisive completion jumps', prompt)
        self.assertNotIn('GLOBAL STAGE DELTA', prompt)
        self.assertNotIn('MORE THAN ONE beat', prompt)
        # 证据门槛：要求"仅凭这两张图"+不确定就别报，这是压假阳性的关键措辞
        self.assertIn('CONCRETE visible detail', prompt)
        self.assertIn('do NOT report it', prompt)

    def test_sealed_entry_rule_covers_every_crossing_variant(self):
        """2026-07-28 误杀回归：切点前那张外部帧的门本该封死，审查却按当年只对桥接变体
        写死的 peek 规则把「木门完全封闭、无法预览室内」报成违规。TBCP v7 把闭门起帧
        提级为全变体规则，两条规则因此合并成一条，不再按 [CUT] 标签分流——旧的 peek
        规则（连同 Monotonic Scale Lock）必须整条消失，否则它会继续要求桥接变体开门。"""
        prompt = pp._local_beat_review_system_prompt()
        self.assertNotIn('THRESHOLD PEEK ANCHOR QUALIFICATION', prompt)
        self.assertNotIn('scale must strictly INCREASE', prompt)

        rule = next(line for line in prompt.splitlines()
                    if line.startswith('- SEALED ENTRY BEFORE ANY CROSSING'))
        self.assertIn('[BRIDGE], [BRIDGE TURN] and [CUT] alike', rule)
        self.assertIn('REQUIRED state', rule)
        self.assertIn('never report it as a missing interior peek', rule)
        # 反向判据：外部帧已经开着门、室内可见，这才是本轮要抓的违规
        self.assertIn('standing open with the interior visible through it IS a violation', rule)
        # 切入只重置机位，不重置施工进度——封套密闭/施工顺序照查
        self.assertIn('resets the camera only', rule)
        # 该槽是真实生成的跨越片段，规则不得再说它「不是片段」
        self.assertIn('Judge the slot as a crossing clip', rule)
        self.assertNotIn('placeholder', rule.lower())

    def test_global_prompt_only_has_cross_frame_rules(self):
        prompt = pp._global_review_system_prompt()
        self.assertIn('NGCS coordinate lock', prompt)
        self.assertIn('Consistent Scene & Layout', prompt)
        self.assertIn('Material Continuity', prompt)
        self.assertIn('CARRIER IDENTITY', prompt)
        # 局部规则不该混进全局审查——规则数收窄正是这次改版要解决的稀释问题
        self.assertNotIn('VISIBLE MILESTONE FIDELITY', prompt)
        self.assertNotIn('UNEXPLAINED ANCHOR DELTA', prompt)
        self.assertNotIn('WORKER TEMPLATE CONSISTENCY', prompt)


class TestCheckBeatConsistency(unittest.TestCase):
    """逐拍局部审查的 JSON 解析契约：单条列表 = 该拍违规，[] = 干净，None = 没跑成。"""

    def test_clean_list_response_means_no_issues(self):
        with patch.object(pp, '_multimodal_chat', return_value='[]') as chat:
            issues = pp.check_beat_consistency({}, 'prompt block', 2, 5, 'a.webp', 'b.webp')
        self.assertEqual(issues, [])
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[3], ['a.webp', 'b.webp'])

    def test_violations_list_is_parsed(self):
        raw = json.dumps(['天花板未随墙面一起封板'])
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            issues = pp.check_beat_consistency({}, 'prompt block', 2, 5, 'a.webp', 'b.webp')
        self.assertEqual(issues, ['天花板未随墙面一起封板'])

    def test_dict_shaped_response_falls_back_gracefully(self):
        # 万一模型仍按旧的 {beat: [...]} 形状回复，容错取出对应 beat 的列表
        raw = json.dumps({'2': ['issue A']})
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            issues = pp.check_beat_consistency({}, 'prompt block', 2, 5, 'a.webp', 'b.webp')
        self.assertEqual(issues, ['issue A'])

    def test_malformed_json_returns_none_not_clean(self):
        with patch.object(pp, '_multimodal_chat', return_value='not json'):
            issues = pp.check_beat_consistency({}, 'prompt block', 2, 5, 'a.webp', 'b.webp')
        self.assertIsNone(issues)

    def test_exception_returns_none(self):
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            issues = pp.check_beat_consistency({}, 'prompt block', 2, 5, 'a.webp', 'b.webp')
        self.assertIsNone(issues)

    def test_truncated_response_is_reported_as_truncation_not_gateway_failure(self):
        """2026-07-25：撞 max_tokens 被截断的半截 JSON 此前和网关故障混为一谈，
        "违规多到写不下"会被当成基础设施异常、还触发整批降级重跑。"""
        payload = json.dumps({
            'choices': [{'finish_reason': 'length', 'message': {'content': '["半截'}}],
            'usage': {},
        }).encode()
        with patch.object(pp, '_execute_request_with_retry', return_value=payload), \
             patch.object(pp, 'resolve_gateway', return_value=('http://gw', 'k')):
            with self.assertRaises(pp.ResponseTruncated):
                pp._multimodal_chat({}, 'sys', 'user', [], max_tokens=10)


class TestCheckGlobalSequenceConsistency(unittest.TestCase):
    """全局稀疏审查沿用原 check_full_sequence_consistency 的单次多模态调用契约，
    只是规则子集变窄——这部分测试直接照搬原先针对单次调用的解析用例。"""

    def test_empty_prompt_block_or_no_frames_short_circuits(self):
        self.assertEqual(pp.check_global_sequence_consistency({}, '', {1: 'a.webp'}), {})
        self.assertEqual(pp.check_global_sequence_consistency({}, 'some prompt', {}), {})

    def test_clean_json_response_means_no_failures(self):
        with patch.object(pp, '_multimodal_chat', return_value='{}') as chat:
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a.webp', 2: 'b.webp'})
        self.assertEqual(failures, {})
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[3], ['a.webp', 'b.webp'])

    def test_violations_are_parsed_and_beat_indices_coerced(self):
        raw = json.dumps({'2': ['载体身份丢失'], '5': ['视角跳切']})
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b', 3: 'c'})
        # total_beats = 2 (3 images - 1); beat 5 is out of range and must be dropped
        self.assertEqual(failures, {2: ['载体身份丢失']})

    def test_out_of_range_and_non_list_entries_are_dropped(self):
        raw = json.dumps({'1': ['ok'], '99': ['out of range'], 'x': 'not a list'})
        with patch.object(pp, '_multimodal_chat', return_value=raw):
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertEqual(failures, {1: ['ok']})

    def test_malformed_json_returns_none_not_pass(self):
        with patch.object(pp, '_multimodal_chat', return_value='not json at all'):
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertIsNone(failures)

    def test_multimodal_chat_exception_returns_none_not_pass(self):
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'})
        self.assertIsNone(failures)

    def test_degraded_mode_extends_timeout_and_still_parses(self):
        with patch.object(pp, '_multimodal_chat', return_value='{}') as chat, \
             patch.object(pp, '_compress_frames_for_review', side_effect=lambda paths, **kw: paths) as comp:
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt block text', {1: 'a', 2: 'b'}, degraded=True)
        self.assertEqual(failures, {})
        comp.assert_called_once()
        self.assertEqual(chat.call_args.kwargs.get('timeout'), 180)


class TestVerifyReviewViolation(unittest.TestCase):
    def test_confirmed_true(self):
        with patch.object(pp, '_multimodal_chat', return_value='{"confirmed": true}'):
            self.assertTrue(pp._verify_review_violation({}, 'issue text', ['a.webp']))

    def test_confirmed_false(self):
        with patch.object(pp, '_multimodal_chat', return_value='{"confirmed": false}'):
            self.assertFalse(pp._verify_review_violation({}, 'issue text', ['a.webp']))

    def test_malformed_response_returns_none(self):
        with patch.object(pp, '_multimodal_chat', return_value='not json'):
            self.assertIsNone(pp._verify_review_violation({}, 'issue text', ['a.webp']))

    def test_exception_returns_none(self):
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('gateway down')):
            self.assertIsNone(pp._verify_review_violation({}, 'issue text', ['a.webp']))


class TestCheckFullSequenceConsistency(unittest.TestCase):
    """三层编排（局部逐拍 + 全局稀疏 + 二次复核）——mock 三个子函数而非
    _multimodal_chat，隔离编排逻辑本身的测试和三个子函数各自的解析契约。

    2026-07-25 起返回 {'failures':..., 'unreviewed_beats':..., 'global_reviewed':...}：
    "审过且干净"与"压根没审成"必须能被调用方区分开，见 test_partial_local_failure_*。"""

    def test_empty_prompt_block_or_no_frames_short_circuits(self):
        for args in (('', {1: 'a.webp'}), ('some prompt', {})):
            result = pp.check_full_sequence_consistency({}, *args)
            self.assertEqual(result['failures'], {})
            self.assertEqual(result['unreviewed_beats'], [])
            self.assertTrue(result['global_reviewed'])

    def test_all_clean_means_no_failures_and_nothing_unreviewed(self):
        with patch.object(pp, 'check_beat_consistency', return_value=[]) as local, \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}) as glob:
            result = pp.check_full_sequence_consistency(
                {}, 'prompt block', {1: 'a', 2: 'b', 3: 'c'})
        self.assertEqual(result['failures'], {})
        self.assertEqual(result['unreviewed_beats'], [])
        self.assertTrue(result['global_reviewed'])
        self.assertEqual(local.call_count, 2)  # total_beats = 2
        glob.assert_called_once()

    def test_local_violation_confirmed_by_verifier_is_kept(self):
        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            return ['issue at beat 1'] if beat == 1 else []
        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, '_verify_review_violation', return_value=True) as verify:
            result = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b', 3: 'c'})
        self.assertEqual(result['failures'], {1: ['issue at beat 1']})
        verify.assert_called_once()

    def test_local_violation_rejected_by_verifier_is_dropped(self):
        # 这是压"硬塞问题"假阳性的关键路径：初审标了，复核否了，最终不计入结果
        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            return ['spurious issue'] if beat == 1 else []
        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, '_verify_review_violation', return_value=False):
            result = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b', 3: 'c'})
        self.assertEqual(result['failures'], {})

    def test_verifier_infra_failure_keeps_candidate_conservatively(self):
        # verify 返回 None（复核调用本身没跑成）不能悄悄抹掉初审抓到的问题，
        # 否则又滑回"找不到问题"——保守起见按"保留"处理
        with patch.object(pp, 'check_beat_consistency', return_value=['issue']), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, '_verify_review_violation', return_value=None):
            result = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'})
        self.assertEqual(result['failures'], {1: ['issue']})

    def test_global_violation_confirmed_is_attributed_to_its_beat(self):
        with patch.object(pp, 'check_beat_consistency', return_value=[]), \
             patch.object(pp, 'check_global_sequence_consistency',
                          return_value={2: ['载体身份丢失']}), \
             patch.object(pp, '_verify_review_violation', return_value=True):
            result = pp.check_full_sequence_consistency(
                {}, 'prompt block', {1: 'a', 2: 'b', 3: 'c'})
        self.assertEqual(result['failures'], {2: ['载体身份丢失']})

    def test_local_layer_totally_failing_still_uses_global_result(self):
        # 单拍/全部局部审查抖动不该拖累整批——只要全局层跑成，仍按拿到的信号处理，
        # 但没审成的拍必须如实出现在 unreviewed_beats 里
        with patch.object(pp, 'check_beat_consistency', return_value=None), \
             patch.object(pp, 'check_global_sequence_consistency',
                          return_value={1: ['全局问题']}), \
             patch.object(pp, '_verify_review_violation', return_value=True):
            result = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'})
        self.assertEqual(result['failures'], {1: ['全局问题']})
        self.assertEqual(result['unreviewed_beats'], [1])

    def test_global_layer_totally_failing_still_uses_local_result(self):
        with patch.object(pp, 'check_beat_consistency', return_value=['本地问题']), \
             patch.object(pp, 'check_global_sequence_consistency', return_value=None), \
             patch.object(pp, '_verify_review_violation', return_value=True):
            result = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'})
        self.assertEqual(result['failures'], {1: ['本地问题']})
        self.assertFalse(result['global_reviewed'])

    def test_partial_local_failure_is_reported_not_swallowed(self):
        """回归防护（2026-07-25）：11 拍里第 2 拍超时、其余干净、跨帧层跑成时，旧实现
        直接返回空 failures，第 2 拍涉及的帧于是被盖 sequence_reviewed_pass 假章——
        与 2026-07-15 盐湖贝壳单同款 fail-open，只是粒度缩到单拍。"""
        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            return None if beat == 2 else []
        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}):
            result = pp.check_full_sequence_consistency(
                {}, 'prompt block', {1: 'a', 2: 'b', 3: 'c', 4: 'd'})
        self.assertEqual(result['failures'], {})
        self.assertEqual(result['unreviewed_beats'], [2])
        self.assertTrue(result['global_reviewed'])

    def test_cancellation_propagates_instead_of_looking_like_infra_failure(self):
        """回归防护（2026-07-25）：GenerationCancelled 继承 ConnectionError → Exception，
        此前被逐拍的 except Exception 吞成"本拍没跑成"，审查会跑完全部拍次、再整批降级
        重跑，最后把每一帧都标成未审查——用户点一次取消就清零了全部真实结论。"""
        with patch.object(pp, 'check_beat_consistency',
                          side_effect=server_common.GenerationCancelled('cancelled')), \
             patch.object(pp, 'check_global_sequence_consistency',
                          side_effect=AssertionError('取消后不该继续跑跨帧层')):
            with self.assertRaises(server_common.GenerationCancelled):
                pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'})

    def test_both_layers_failing_returns_none_not_pass(self):
        # 两层都没跑成才如实报告"没审成"——绝不能 fail-open 成"无违规"
        # （2026-07-15 盐湖贝壳单事故根源）
        with patch.object(pp, 'check_beat_consistency', return_value=None), \
             patch.object(pp, 'check_global_sequence_consistency', return_value=None):
            failures = pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'})
        self.assertIsNone(failures)

    def test_degraded_mode_compresses_local_pair_and_passes_through(self):
        with patch.object(pp, 'check_beat_consistency', return_value=[]) as local, \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, '_compress_frames_for_review',
                          side_effect=lambda paths, **kw: [f'compressed-{p}' for p in paths]):
            pp.check_full_sequence_consistency({}, 'prompt block', {1: 'a', 2: 'b'}, degraded=True)
        self.assertEqual(local.call_args.args[4:6], ('compressed-a', 'compressed-b'))
        self.assertEqual(local.call_args.kwargs.get('timeout'), 120)


class TestReviewParallelism(unittest.TestCase):
    """逐拍审查并发化（2026-07-25）：11 拍串行、每拍 60s 超时的审查此前要跑几分钟，
    期间没有任何进度、也拦不住取消。并发化必须同时守住三样线程局部的东西：取消 sink、
    上游广播 sink、token 记账——它们都是 threading.local，裸线程池会静默弄丢。"""

    def _paths(self, n):
        return {i: f'f{i}' for i in range(1, n + 1)}

    def test_beats_run_concurrently(self):
        seen_threads = set()
        barrier_lock = threading.Lock()

        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            with barrier_lock:
                seen_threads.add(threading.current_thread().name)
            time.sleep(0.02)
            return []

        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}):
            pp.check_full_sequence_consistency({}, 'prompt block', self._paths(6))
        self.assertGreater(len(seen_threads), 1)

    def test_concurrency_one_falls_back_to_serial(self):
        with patch.object(pp, 'check_beat_consistency', return_value=[]) as local, \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}):
            pp.check_full_sequence_consistency({'reviewConcurrency': 1}, 'prompt block',
                                               self._paths(4))
        self.assertEqual(local.call_count, 3)

    def test_worker_threads_inherit_cancel_and_upstream_sinks(self):
        """子线程里 _execute_request_with_retry 必须还能看到取消回调——看不到的话
        取消按钮在整段审查期间又变成"点了没用"。"""
        seen = []

        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            seen.append(frame_generator.current_thread_sinks())
            return []

        frame_generator.set_cancel_check_sink(lambda: False)
        frame_generator.set_upstream_event_sink(lambda ev: None)
        try:
            with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
                 patch.object(pp, 'check_global_sequence_consistency', return_value={}):
                pp.check_full_sequence_consistency({}, 'prompt block', self._paths(4))
        finally:
            frame_generator.set_cancel_check_sink(None)
            frame_generator.set_upstream_event_sink(None)

        self.assertEqual(len(seen), 3)
        for upstream, cancel in seen:
            self.assertIsNotNone(cancel)
            self.assertIsNotNone(upstream)

    def test_token_usage_from_worker_threads_is_merged_back(self):
        """_usage_tracker 也是 threading.local：不并回父线程的话，整段并发审查的
        token 消耗在任务结算里会凭空消失。"""
        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            pp._record_tokens({'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11})
            return []

        pp.start_accounting()
        try:
            with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
                 patch.object(pp, 'check_global_sequence_consistency', return_value={}):
                pp.check_full_sequence_consistency({}, 'prompt block', self._paths(4))
            stats = pp.stop_and_get_accounting()
        finally:
            pp._usage_tracker.active = False
        self.assertEqual(stats['api_calls'], 3)
        self.assertEqual(stats['total_tokens'], 33)

    def test_cancellation_inside_a_worker_aborts_the_whole_review(self):
        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            if beat == 2:
                raise server_common.GenerationCancelled('cancelled')
            return []

        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency',
                          side_effect=AssertionError('取消后不该继续跑跨帧层')):
            with self.assertRaises(server_common.GenerationCancelled):
                pp.check_full_sequence_consistency({}, 'prompt block', self._paths(5))

    def test_per_beat_progress_events_are_emitted(self):
        events = []

        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            return ['问题'] if beat == 2 else []

        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency', return_value={}), \
             patch.object(pp, '_verify_review_violation', return_value=True):
            pp.check_full_sequence_consistency(
                {}, 'prompt block', self._paths(4),
                on_progress=lambda stage, data: events.append((stage, data)))

        beats = [d for s, d in events if s == 'sequence_review_beat']
        self.assertEqual(sorted(d['beat'] for d in beats), [1, 2, 3])
        flagged = [d for d in beats if d['beat'] == 2][0]
        self.assertTrue(flagged['reviewed'])
        self.assertEqual(flagged['issues'], ['问题'])

    def test_on_progress_raising_cancel_stops_remaining_beats(self):
        def on_progress(stage, data):
            raise server_common.GenerationCancelled('用户取消')

        with patch.object(pp, 'check_beat_consistency', return_value=[]), \
             patch.object(pp, 'check_global_sequence_consistency',
                          side_effect=AssertionError('取消后不该继续跑跨帧层')):
            with self.assertRaises(server_common.GenerationCancelled):
                pp.check_full_sequence_consistency({}, 'prompt block', self._paths(6),
                                                   on_progress=on_progress)

    def test_only_beats_reviews_just_those_and_skips_global(self):
        called = []

        def fake_local(config, prompt_block, beat, total, before, after, timeout=60):
            called.append(beat)
            return []

        with patch.object(pp, 'check_beat_consistency', side_effect=fake_local), \
             patch.object(pp, 'check_global_sequence_consistency',
                          side_effect=AssertionError('skip_global 时不该跑跨帧层')):
            result = pp.check_full_sequence_consistency(
                {}, 'prompt block', self._paths(6), only_beats=[2, 4], skip_global=True)
        self.assertEqual(sorted(called), [2, 4])
        self.assertEqual(result['unreviewed_beats'], [])
        self.assertFalse(result['global_reviewed'])

    def test_global_violation_verified_against_its_own_beat_not_all_frames(self):
        """回归防护（2026-07-25）：跨帧违规的二次复核此前把全套帧图又喂了一遍——
        复核的全部意义就是窄口径只验这一条，喂全套既贵又正好触发要避免的注意力稀释。
        现在只带这一拍的两张 + IMAGE 1（跨帧规则的身份基准）。"""
        verify_calls = []

        with patch.object(pp, 'check_beat_consistency', return_value=[]), \
             patch.object(pp, 'check_global_sequence_consistency',
                          return_value={3: ['载体身份丢失']}), \
             patch.object(pp, '_verify_review_violation',
                          side_effect=lambda cfg, issue, imgs: verify_calls.append(imgs) or True):
            pp.check_full_sequence_consistency({}, 'prompt block', self._paths(6))

        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(verify_calls[0], ['f1', 'f3', 'f4'])

    def test_degraded_compresses_once_and_cleans_up_temp_dir(self):
        """回归防护（2026-07-25）：降级档此前逐拍各压一次、跨帧层再压一次、复核前又
        压一次，一单几十张图、十几个 mkdtemp 目录且从不清理。"""
        made_dirs = []
        real_compress = pp._compress_frames_for_review

        def spy(paths, **kw):
            out = real_compress(paths, **kw)
            made_dirs.append(sorted({os.path.dirname(p) for p in out}))
            return out

        tmp = tempfile.mkdtemp()
        try:
            frames = {}
            for i in (1, 2, 3):
                path = os.path.join(tmp, f'img_{i}.png')
                Image.new('RGB', (32, 32), (i * 20, 0, 0)).save(path)
                frames[i] = path
            with patch.object(pp, '_compress_frames_for_review', side_effect=spy) as comp, \
                 patch.object(pp, 'check_beat_consistency', return_value=[]), \
                 patch.object(pp, 'check_global_sequence_consistency', return_value={}):
                pp.check_full_sequence_consistency({}, 'prompt block', frames, degraded=True)
            self.assertEqual(comp.call_count, 1)
            for dirs in made_dirs:
                for d in dirs:
                    self.assertFalse(os.path.exists(d), f'临时目录未清理: {d}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMergeReviewResults(unittest.TestCase):
    def test_retry_result_fills_in_the_unreviewed_beats(self):
        first = _review({1: ['旧问题']}, unreviewed_beats=[2, 3], global_reviewed=True)
        second = _review({2: ['补审出的问题']}, unreviewed_beats=[3], global_reviewed=False)
        merged = pp.merge_review_results(first, second)
        self.assertEqual(merged['failures'], {1: ['旧问题'], 2: ['补审出的问题']})
        self.assertEqual(merged['unreviewed_beats'], [3])   # 重跑后仍没审成的才算漏审
        self.assertTrue(merged['global_reviewed'])          # 上一轮已跑成，不因本轮跳过而作废

    def test_none_operands_pass_through(self):
        first = _review({1: ['问题']})
        self.assertEqual(pp.merge_review_results(first, None), first)
        self.assertEqual(pp.merge_review_results(None, first), first)


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

    def test_cut_slot_video_body_rewritable_from_placeholder(self):
        """占位声明正文在复审中支持重写为真实的跨越镜头运镜描述。"""
        raw = json.dumps({'video': 'a sweeping dolly through the doorway', 'image': 'new image body'})
        with patch.object(pp, '_chat', return_value=raw), \
             patch.object(pp, 'clean_prompt_text', side_effect=lambda s: s), \
             patch.object(pp, 'fix_image_clean_frame_proactive', side_effect=lambda s: s):
            v, i = pp.fix_beat_from_sequence_review(
                {}, pp.HARD_CUT_VIDEO_PLACEHOLDER, 'old image', ['木门封闭'], video_meta='CUT')
        self.assertEqual(v, 'a sweeping dolly through the doorway')
        self.assertEqual(i, 'new image body')

    def test_new_cut_slot_video_body_stays_rewritable(self):
        """2026-07-30：[CUT] 槽的正文现在是真实跨越片段的普通镜头描述，与 BRIDGE 同权，
        照常可改写——只有旧单的占位声明才冻结。"""
        raw = json.dumps({'video': 'new crossing body', 'image': 'new image body'})
        with patch.object(pp, '_chat', return_value=raw), \
             patch.object(pp, 'clean_prompt_text', side_effect=lambda s: s), \
             patch.object(pp, 'fix_image_clean_frame_proactive', side_effect=lambda s: s):
            v, _ = pp.fix_beat_from_sequence_review(
                {}, 'the camera pushes through the opened hatch', 'old image', ['issue'],
                video_meta='CUT')
        self.assertEqual(v, 'new crossing body')

    def test_crossing_slot_keeps_its_camera_motion_sentence(self):
        """收尾的确定性修复必须按运动镜头跑：默认的静止机位口径会把跨越片段唯一的
        动作句（镜头推进穿过门）当矛盾句删掉，槽位就又变成"没有镜头"了。"""
        motion = ('The camera pushes forward through the opened hatch and settles inside. '
                  'Dust hangs in the light.')
        raw = json.dumps({'video': motion, 'image': 'new image body'})
        with patch.object(pp, '_chat', return_value=raw), \
             patch.object(pp, 'clean_prompt_text', side_effect=lambda s: s), \
             patch.object(pp, 'fix_image_clean_frame_proactive', side_effect=lambda s: s), \
             patch.object(pp, 'fix_horizon_line', side_effect=lambda s, **kw: s):
            v, _ = pp.fix_beat_from_sequence_review(
                {}, 'old video', 'old image', ['issue'], family='interior', video_meta='CUT')
        self.assertIn('pushes forward through the opened hatch', v)

    def test_bridge_slot_video_body_stays_rewritable(self):
        """单一过门拍的 VIDEO 是真实可见片段，不受硬切豁免影响，照常可改写。"""
        raw = json.dumps({'video': 'new video body', 'image': 'new image body'})
        with patch.object(pp, '_chat', return_value=raw), \
             patch.object(pp, 'clean_prompt_text', side_effect=lambda s: s), \
             patch.object(pp, 'fix_image_clean_frame_proactive', side_effect=lambda s: s):
            v, _ = pp.fix_beat_from_sequence_review(
                {}, 'old video', 'old image', ['issue'], video_meta='BRIDGE TURN')
        self.assertEqual(v, 'new video body')

    def test_malformed_response_returns_inputs_unchanged(self):
        with patch.object(pp, '_chat', return_value='not json'):
            v, i = pp.fix_beat_from_sequence_review({}, 'old video', 'old image', ['issue'])
        self.assertEqual((v, i), ('old video', 'old image'))

    def test_llm_exception_returns_inputs_unchanged(self):
        with patch.object(pp, '_chat', side_effect=RuntimeError('boom')):
            v, i = pp.fix_beat_from_sequence_review({}, 'old video', 'old image', ['issue'])
        self.assertEqual((v, i), ('old video', 'old image'))


def _review(failures=None, unreviewed_beats=None, global_reviewed=True,
            global_unreviewed_beats=None, global_attempted=True):
    """构造 check_full_sequence_consistency 的新形状返回值（见其 docstring）。"""
    return {'failures': failures or {},
            'unreviewed_beats': list(unreviewed_beats or []),
            'global_unreviewed_beats': list(global_unreviewed_beats or []),
            'global_reviewed': global_reviewed,
            'global_attempted': global_attempted}


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

    def test_single_rendered_frame_skips_review_without_touching_manifest(self):
        # 只渲了第 1 帧：连一拍都凑不齐，没有可比对的画面对，如实早退且不碰 manifest
        self._touch_frame(1)
        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=AssertionError('should not run review with <2 frames')):
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        self.assertEqual(result, self.PROMPT_BLOCK)
        self.assertEqual(self._read_manifest(), {})

    def test_partially_rendered_sequence_reviews_the_rendered_prefix(self):
        """2026-07-25：此前缺任何一帧就整体放弃、一拍都不审——逐帧手动生成到一半想
        中途查一下完全办不到。现在审"从 IMAGE 1 起连续渲出来的那一段"，未渲染的帧
        一律不碰（既没审过，就不能标任何结论）。"""
        self._touch_frame(1)
        self._touch_frame(2)          # 第 3 帧还没渲
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        seen = {}
        events = []

        def fake_check(config, prompt_block, frame_paths, **kw):
            seen['paths'] = sorted(frame_paths)
            return _review({})

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir,
                                            on_progress=lambda s, d: events.append((s, d)))

        self.assertEqual(seen['paths'], [1, 2])   # 只把渲出来的前缀送审
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(frames[2]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(frames[3]['quality_gate'], 'pending_manual_review')  # 没渲的不碰
        result_ev = [d for s, d in events if s == 'sequence_review_result'][0]
        self.assertTrue(result_ev['passed'])
        self.assertTrue(result_ev['partial'])
        self.assertIn('前 2', result_ev['message'])   # 必须说清只覆盖了前缀

    def test_gap_in_the_middle_stops_the_reviewed_prefix(self):
        """中间断开时不把后面的帧混进来：它们接的是另一条 i2i 链，跨帧规则拿它们
        和链头比只会制造假阳性。"""
        self._touch_frame(1)
        self._touch_frame(3)          # 第 2 帧缺失
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=AssertionError('前缀不足两帧时不该开审')):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(set(gates.values()), {'pending_manual_review'})

    def test_clean_review_marks_all_frames_reviewed_pass(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency', return_value=_review({})) as mock_check, \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        mock_check.assert_called_once()
        mock_render.assert_not_called()  # 没有问题，不该触发任何重渲
        self.assertEqual(result, self.PROMPT_BLOCK)
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass', 3: 'sequence_reviewed_pass'})

    def test_failure_flags_without_fixing_or_rerendering(self):
        # 2026-07-23 行为变更：发现问题不再自动改写提示词+重渲——只标记+报告，
        # 等人工点「修复此帧问题」才会真正触发 fix_beat_from_sequence_review。
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({1: ['天花板未封板']})) as mock_check, \
             patch.object(po, 'fix_beat_from_sequence_review',
                          side_effect=AssertionError('不该自动修复')) as mock_fix, \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            result = po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        mock_check.assert_called_once()
        mock_fix.assert_not_called()
        mock_render.assert_not_called()
        self.assertEqual(result, self.PROMPT_BLOCK)  # 提示词原样返回，没有被改写
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_review_flagged',
                                 3: 'sequence_reviewed_pass'})
        reason = self._read_manifest()['frames'][1]['vlm_qa_reason']
        self.assertIn('天花板未封板', reason)

    def test_multiple_failing_beats_all_flagged_independently(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({1: ['问题A'], 2: ['问题B', '问题C']})), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        mock_render.assert_not_called()
        gates_by_seq = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(gates_by_seq[2]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('问题A', gates_by_seq[2]['vlm_qa_reason'])
        self.assertEqual(gates_by_seq[3]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('问题B', gates_by_seq[3]['vlm_qa_reason'])
        self.assertIn('问题C', gates_by_seq[3]['vlm_qa_reason'])
        self.assertEqual(gates_by_seq[1]['quality_gate'], 'sequence_reviewed_pass')

    def test_failure_progress_message_names_flagged_frames(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        events = []
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({1: ['天花板未封板']})), \
             patch.object(po, 'generate_frame_sequence'):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir,
                                            on_progress=lambda stage, data: events.append((stage, data)))

        result_events = [d for s, d in events if s == 'sequence_review_result']
        self.assertEqual(len(result_events), 1)
        self.assertFalse(result_events[0]['passed'])
        self.assertIn('IMG 002', result_events[0]['message'])
        self.assertIn('天花板未封板', result_events[0]['message'])
        self.assertIn('修复此帧问题', result_events[0]['message'])

    def test_review_unavailable_marks_frames_skipped_not_pass(self):
        """审查两次（常规+降级）都没跑成：帧必须标 sequence_review_skipped，绝不能
        盖 sequence_reviewed_pass 假章；且第二次调用必须带 degraded=True。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        calls = []

        def fake_check(config, prompt_block, frame_paths, degraded=False, **kw):
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
        results = [None, _review({})]

        def fake_check(config, prompt_block, frame_paths, degraded=False, **kw):
            return results.pop(0)

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check), \
             patch.object(po, 'generate_frame_sequence') as mock_render:
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        mock_render.assert_not_called()
        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass',
                                 3: 'sequence_reviewed_pass'})


    def test_unreviewed_beat_frames_are_marked_skipped_not_pass(self):
        """回归防护（2026-07-25）：第 1 拍没审成时，它涉及的 IMG 001/002 必须标
        sequence_review_skipped；旧实现会把它们连同真正审过的 IMG 003 一起盖
        sequence_reviewed_pass。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        events = []
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({}, unreviewed_beats=[1])):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir,
                                            on_progress=lambda s, d: events.append((s, d)))

        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(gates, {1: 'sequence_review_skipped', 2: 'sequence_review_skipped',
                                 3: 'sequence_reviewed_pass'})
        result_ev = [d for s, d in events if s == 'sequence_review_result'][0]
        self.assertFalse(result_ev['passed'])
        self.assertEqual(result_ev['unreviewed_sequences'], [1, 2])
        self.assertIn('未审完', result_ev['message'])

    def test_global_layer_not_reviewed_means_no_frame_gets_a_pass(self):
        """跨帧层没跑成时，跨帧规则对所有帧都没查过——一帧都不能盖"通过"。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({}, global_reviewed=False)):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        gates = {f['sequence']: f['quality_gate'] for f in self._read_manifest()['frames']}
        self.assertEqual(set(gates.values()), {'sequence_review_skipped'})

    def test_flagged_beat_wins_over_unreviewed_neighbour_beat(self):
        """帧同时"参与的另一拍没审成"和"本拍被检出问题"时，检出的问题优先——
        漏审不得把已经抓到的违规洗成一句"未审查"。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({1: ['天花板未封板']}, unreviewed_beats=[2])):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames[2]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('天花板未封板', frames[2]['vlm_qa_reason'])
        self.assertEqual(frames[3]['quality_gate'], 'sequence_review_skipped')

    def test_totally_failed_review_keeps_previous_real_verdicts(self):
        """回归防护（2026-07-25）：审查整轮没跑起来（网关抖动/用户取消）时，上一轮
        真实的 reviewed_pass / review_flagged 必须原样保留——帧文件这期间没被动过，
        一次失败不该把整单的审查结论清零。只有本来没有真实结论的帧才标未审查。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_reviewed_pass', 'vlm_qa_reason': None},
            {'sequence': 2, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': '天花板未封板'},
            {'sequence': 3, 'quality_gate': 'pending_manual_review', 'vlm_qa_reason': None},
        ]})
        with patch.object(po, 'check_full_sequence_consistency', return_value=None):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(frames[2]['quality_gate'], 'sequence_review_flagged')
        self.assertEqual(frames[2]['vlm_qa_reason'], '天花板未封板')  # 问题描述也不能丢
        self.assertEqual(frames[3]['quality_gate'], 'sequence_review_skipped')


    def test_partial_failure_retries_only_the_unreviewed_beats(self):
        """回归防护（2026-07-25）：第 1 拍没审成时，降级重试只重跑第 1 拍，不再把
        已经审干净的整批再烧一遍全部调用；跨帧层上一轮跑成了也不重跑。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        calls = []

        def fake_check(config, prompt_block, frame_paths, degraded=False,
                       only_beats=None, skip_global=False, on_progress=None,
                       global_only_beats=None):
            calls.append({'degraded': degraded, 'only_beats': only_beats,
                          'skip_global': skip_global, 'global_only_beats': global_only_beats})
            if not degraded:
                return _review({}, unreviewed_beats=[1], global_reviewed=True)
            return _review({1: ['补审出的问题']}, unreviewed_beats=[], global_reviewed=False,
                           global_attempted=False)

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], {'degraded': True, 'only_beats': [1], 'skip_global': True,
                                    'global_only_beats': None})
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        # 补审出的问题落到 IMG 002；其余帧因两轮合计已审完，正常盖通过
        self.assertEqual(frames[2]['quality_gate'], 'sequence_review_flagged')
        self.assertIn('补审出的问题', frames[2]['vlm_qa_reason'])
        self.assertEqual(frames[1]['quality_gate'], 'sequence_reviewed_pass')
        self.assertEqual(frames[3]['quality_gate'], 'sequence_reviewed_pass')


class TestFrameReviewStatus(unittest.TestCase):
    """审查结果 → 每帧 quality_gate 的翻译规则（覆盖判定的单元测试）。"""

    def test_frame_needs_all_of_its_beats_and_the_global_layer(self):
        # 4 帧 3 拍：IMG 2 参与 beat 1/2，IMG 3 参与 beat 2/3
        status = pp.frame_review_status([1, 2, 3, 4], _review({}, unreviewed_beats=[2]))
        self.assertEqual(status[1][0], 'reviewed')       # 只参与 beat 1
        self.assertEqual(status[2][0], 'unreviewed')     # 参与 beat 2
        self.assertEqual(status[3][0], 'unreviewed')     # 参与 beat 2
        self.assertEqual(status[4][0], 'reviewed')       # 只参与 beat 3

    def test_reason_text_names_which_layer_missed(self):
        status = pp.frame_review_status([1, 2], _review({}, unreviewed_beats=[1]))
        self.assertIn('逐拍审查未跑成', status[2][1])
        status = pp.frame_review_status([1, 2], _review({}, global_reviewed=False))
        self.assertIn('跨帧一致性审查未跑成', status[2][1])

    def test_clean_full_review_marks_every_frame_reviewed(self):
        status = pp.frame_review_status([1, 2, 3], _review({}))
        self.assertEqual({s: v[0] for s, v in status.items()},
                         {1: 'reviewed', 2: 'reviewed', 3: 'reviewed'})


class TestFixFrameIssue(_TmpProjectCase):
    """人工确认后触发的定向修复：读取该帧记录的问题原因，优化提示词后重渲——
    非首帧走一致性审查的定向重写，首帧走单提示词反馈重写；重渲一律图生图。"""

    def setUp(self):
        super().setUp()
        # 修复收尾会对着新画面复核每条问题（_reverify_frame_issues）；这些用例关心的
        # 是修复本身，统一桩成"复核没跑成"（保守按未解决处理，不改变既有断言）。
        patcher = patch.object(po, '_verify_review_violation', return_value=None)
        self.reverify = patcher.start()
        self.addCleanup(patcher.stop)

    def _write_gate(self, seq_gates):
        frames = [{'sequence': s, 'quality_gate': g, 'vlm_qa_reason': r}
                  for s, (g, r) in seq_gates.items()]
        server_common.write_manifest(self.project_dir, {'frames': frames})

    def test_no_recorded_reason_raises(self):
        self._touch_frame(2)
        self._write_gate({1: ('sequence_reviewed_pass', None), 2: ('sequence_reviewed_pass', None),
                          3: ('sequence_reviewed_pass', None)})
        with self.assertRaises(RuntimeError):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

    def test_non_first_frame_uses_beat_fix_and_i2i_rerender(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        self._write_gate({1: ('sequence_reviewed_pass', None),
                          2: ('sequence_review_flagged', '天花板未随墙面一起封板'),
                          3: ('sequence_reviewed_pass', None)})
        render_calls = []

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4):
            render_calls.append(target_sequences)

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')) as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence', side_effect=fake_render):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        mock_fix.assert_called_once_with({}, 'video one', 'second frame', ['天花板未随墙面一起封板'],
                                         video_meta='', preceding_image_prompt='first frame',
                                         succeeding_image_prompt='third frame', succeeding_video_prompt='video two')
        self.assertEqual(render_calls, [[2]])  # 只重渲第 2 帧（4选1候选优选）
        self.assertIn('fixed image 2', result['prompt_block'])
        self.assertIn('fixed video 1', result['prompt_block'])
        self.assertEqual(result['reason'], '天花板未随墙面一起封板')

    def test_first_frame_uses_prompt_feedback_and_image_edit_never_t2i(self):
        # 首帧没有前置视频过渡（beat 0 不存在），走单提示词反馈重写，
        # 并以 4选1 候选优选模式重渲
        self._touch_frame(1)
        self._write_gate({1: ('sequence_review_flagged', 'IMAGE 1 不够原始'),
                          2: ('sequence_reviewed_pass', None), 3: ('sequence_reviewed_pass', None)})
        render_calls = []

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4):
            render_calls.append(target_sequences)

        with patch.object(po, 'fix_image_prompt_with_vlm_feedback',
                          return_value='fixed first frame') as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence', side_effect=fake_render):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 1)

        mock_fix.assert_called_once_with({}, 'first frame', 'IMAGE 1 不够原始', succeeding_image_prompt='second frame')
        self.assertEqual(render_calls, [[1]])
        self.assertIn('fixed first frame', result['prompt_block'])



class TestReviewVerdictInvalidation(_TmpProjectCase):
    """审查结论与帧内容绑定（2026-07-25）：审查时把看过的帧内容哈希记进
    review_frames_sha256，之后任何一帧被重渲都会让相关结论自动作废。此前完全没有这套
    机制——修完 IMG 005 后 IMG 004/006 仍挂着 sequence_reviewed_pass，前端显示的
    "全部审查通过"从那一刻起就是假的。"""

    def _write_frame(self, seq, color):
        Image.new('RGB', (16, 16), color).save(po._frame_path(self.TITLE, seq), format='WEBP')

    def _reviewed_manifest(self):
        for seq, color in ((1, (10, 0, 0)), (2, (20, 0, 0)), (3, (30, 0, 0))):
            self._write_frame(seq, color)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'sequence_reviewed_pass', 'vlm_qa_reason': None}
            for s in (1, 2, 3)
        ]})
        po._record_review_fingerprints(self.project_dir, self.TITLE, [1, 2, 3])

    def test_fingerprints_cover_the_neighbouring_frames(self):
        self._reviewed_manifest()
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        # IMG 002 的结论同时依赖 001/002/003 三张图（它参与 beat 1 与 beat 2）
        self.assertEqual(set(frames[2]['review_frames_sha256']), {'1', '2', '3'})
        self.assertEqual(set(frames[1]['review_frames_sha256']), {'1', '2'})
        self.assertIn('reviewed_at', frames[1])

    def test_rerendering_one_frame_invalidates_its_neighbours_verdicts(self):
        self._reviewed_manifest()
        self._write_frame(2, (99, 99, 99))   # 模拟 IMG 002 被重渲

        changed = po.invalidate_stale_review_verdicts(self.project_dir)

        self.assertEqual(sorted(changed), [1, 2, 3])  # 三帧的结论都依赖 IMG 002
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        for seq in (1, 2, 3):
            self.assertEqual(frames[seq]['quality_gate'], 'pending_manual_review')
            self.assertNotIn('review_frames_sha256', frames[seq])

    def test_untouched_frames_keep_their_verdicts(self):
        for seq, color in ((1, (10, 0, 0)), (2, (20, 0, 0)), (3, (30, 0, 0)),
                           (4, (40, 0, 0)), (5, (50, 0, 0))):
            self._write_frame(seq, color)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'sequence_reviewed_pass'} for s in range(1, 6)
        ]})
        po._record_review_fingerprints(self.project_dir, self.TITLE, [1, 2, 3, 4, 5])
        self._write_frame(1, (99, 0, 0))     # 只重渲了 IMG 001

        changed = po.invalidate_stale_review_verdicts(self.project_dir)

        self.assertEqual(sorted(changed), [1, 2])    # 只有依赖 IMG 001 的两帧作废
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        for seq in (3, 4, 5):
            self.assertEqual(frames[seq]['quality_gate'], 'sequence_reviewed_pass')

    def test_invalidation_also_drops_structured_issues(self):
        self._reviewed_manifest()
        with server_common.manifest_lock(self.project_dir):
            m = server_common.read_manifest(self.project_dir)
            for f in m['frames']:
                f['review_issues'] = [{'text': '旧问题', 'layer': 'local', 'beat': 1,
                                       'frames': [1, 2]}]
            server_common.write_manifest(self.project_dir, m)
        self._write_frame(3, (99, 99, 99))

        po.invalidate_stale_review_verdicts(self.project_dir)

        manifest = self._read_manifest()
        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertNotIn('review_issues', frames[3])

    def test_review_records_fingerprints_after_a_successful_run(self):
        for seq, color in ((1, (10, 0, 0)), (2, (20, 0, 0)), (3, (30, 0, 0))):
            self._write_frame(seq, color)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency', return_value=_review({})):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertTrue(all('review_frames_sha256' in frames[s] for s in (1, 2, 3)))

    def test_unreviewed_frames_do_not_get_fingerprints(self):
        """没审成的帧不该留指纹——留了就等于宣称"这个结论对这几张图成立"。"""
        for seq, color in ((1, (10, 0, 0)), (2, (20, 0, 0)), (3, (30, 0, 0))):
            self._write_frame(seq, color)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        with patch.object(po, 'check_full_sequence_consistency',
                          return_value=_review({}, unreviewed_beats=[1])):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertNotIn('review_frames_sha256', frames[1])
        self.assertNotIn('review_frames_sha256', frames[2])
        self.assertIn('review_frames_sha256', frames[3])


class TestReviewSeverityAndStructuredResult(_TmpProjectCase):
    """审查结论的可读化（2026-08-25）：

    1. 影响分级（chain / cosmetic）此前只有生成期链上守卫在用——它需要它来决定停不
       停链。手动整套审查这条路从没调过分级器，于是"会一路传染进后面每一帧的结构
       问题"与"只是光影差一点"在前端长得一模一样。现在补上，落进 review_issues。
    2. 结果播报此前是把「几帧有问题 + 每帧原因 + 未审完 + 只覆盖前缀 + 复用了几拍」
       拼成一根字符串扔进日志流。现在同一份内容另出一份结构化的 lines/flagged_frames，
       message 原样保留（老前端与服务端日志不受影响）。
    """

    def _three_flagged(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        return _review({1: ['塔吊消失']}) | {
            'issues': [{'text': '塔吊消失', 'layer': 'local', 'beat': 1,
                        'frames': [1, 2], 'verified': True}],
        }

    def _run(self, review_result, classify=None, events=None):
        cm = patch.object(po, 'check_full_sequence_consistency', return_value=review_result)
        import chain_guard
        cls = patch.object(chain_guard, 'classify_chain_impact',
                           **({'side_effect': classify} if callable(classify)
                              else {'return_value': classify if classify is not None else []}))
        with cm, cls as spy:
            po._sequence_consistency_review(
                {}, self.TITLE, self.PROMPT_BLOCK, self.project_dir,
                on_progress=(lambda s, d: events.append((s, d))) if events is not None else None)
        return spy

    def _issues_of(self, seq):
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        return frames[seq].get('review_issues') or []

    def test_severity_is_classified_and_persisted(self):
        spy = self._run(self._three_flagged(), classify=['chain'])
        self.assertEqual([i.get('severity') for i in self._issues_of(2)], ['chain'])
        # 只送文案，不送整个 dict
        self.assertEqual(spy.call_args[0][1], ['塔吊消失'])

    def test_classifier_failure_leaves_issues_unclassified_not_alarming(self):
        """分级器挂了就不分级。这里的分级只用于展示、不决定任何动作，把整单标成
        "会传染下游"是在编造判定（停链那条路才需要 fail-safe 到 chain）。"""
        spy = self._run(self._three_flagged(), classify=[])   # on_error=None 的失败形状
        self.assertNotIn('severity', self._issues_of(2)[0])
        self.assertIn('on_error', spy.call_args.kwargs,
                      '展示用的分级必须显式传 on_error，不能沿用停链的 chain 兜底')
        self.assertIsNone(spy.call_args.kwargs['on_error'])

    def test_partial_classification_is_discarded_whole(self):
        # 分级器只回了一半：宁可整轮不分级，也不半套落盘
        result = self._three_flagged()
        result['issues'].append({'text': '地面材质突变', 'layer': 'local', 'beat': 1,
                                 'frames': [1, 2], 'verified': True})
        self._run(result, classify=['chain'])
        self.assertTrue(all('severity' not in i for i in self._issues_of(2)))

    def test_existing_severity_is_not_reclassified(self):
        """链上守卫审过的拍自带判定（见 _inline_result），不重分也不重复计费。"""
        result = self._three_flagged()
        result['issues'][0]['severity'] = 'cosmetic'
        spy = self._run(result, classify=['chain'])
        self.assertEqual(self._issues_of(2)[0]['severity'], 'cosmetic')
        spy.assert_not_called()

    def test_same_text_across_frames_is_classified_once(self):
        """跨帧层的一条违规会同时落在它涉及的每一帧上，分级只该送一次。"""
        result = self._three_flagged()
        result['issues'] = [
            {'text': '施工顺序倒置', 'layer': 'global', 'beat': b,
             'frames': [1, 2, 3], 'verified': True} for b in (1, 2)
        ]
        spy = self._run(result, classify=['chain'])
        self.assertEqual(spy.call_args[0][1], ['施工顺序倒置'])   # 去重后只有一条

    def test_rejected_issues_are_neither_classified_nor_persisted(self):
        result = self._three_flagged()
        result['issues'].append({'text': '被推翻的指控', 'layer': 'local', 'beat': 1,
                                 'frames': [1, 2], 'verified': False})
        spy = self._run(result, classify=['chain'])
        self.assertEqual(spy.call_args[0][1], ['塔吊消失'])
        self.assertEqual([i['text'] for i in self._issues_of(2)], ['塔吊消失'])

    # ── 结构化播报 ──────────────────────────────────────────────────
    def test_result_event_carries_structured_lines_and_frames(self):
        events = []
        self._run(self._three_flagged(), classify=['chain'], events=events)
        ev = [d for s, d in events if s == 'sequence_review_result'][-1]

        self.assertFalse(ev['passed'])
        self.assertEqual([f['sequence'] for f in ev['flagged_frames']], [2])
        self.assertIn('塔吊消失', ev['flagged_frames'][0]['reason'])
        # 每帧原因各占一行，不再和"几帧有问题/未审完/复用"挤在同一句里
        texts = [l['text'] for l in ev['lines']]
        self.assertTrue(any('发现 1 帧存在问题' in t for t in texts))
        self.assertTrue(any('IMG 002' in t and '塔吊消失' in t for t in texts))
        self.assertTrue(all(isinstance(l.get('cls', ''), str) for l in ev['lines']))
        # message 原样保留：老前端与服务端日志靠它
        self.assertIn('一致性审查', ev['message'])

    def test_passing_run_also_carries_lines(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        events = []
        self._run(_review({}), events=events)
        ev = [d for s, d in events if s == 'sequence_review_result'][-1]
        self.assertTrue(ev['passed'])
        self.assertEqual(ev['flagged_frames'], [])
        self.assertTrue(any('审查通过' in l['text'] for l in ev['lines']))


class TestUndoFrameFix(_TmpProjectCase):
    """修复快照与撤销（2026-08-02）：定向修复是**覆盖写同一个帧文件**，旧图此前不留档
    ——修坏了只能盲重渲碰运气，也没法拿前后两张对比。删除整拍早有 .deleted_slots 快照，
    修复没有。现在每次修复动手前存一份（帧图 + manifest 条目 + 两段提示词正文），
    可一键退回。"""

    FIXED_BLOCK = (
        "图片提示词\n图片 1:\nfirst frame\n\n图片 2:\nsecond frame 改过\n\n图片 3:\nthird frame\n\n"
        "视频提示词\n视频 1:\nvideo one 改过\n\n视频 2:\nvideo two\n"
    )

    def _entry(self, **over):
        base = {'sequence': 2, 'quality_gate': 'sequence_review_flagged',
                'vlm_qa_reason': '塔吊消失', 'prompt': 'second frame',
                'url': '/outputs/x/frames/img_002.webp',
                'review_issues': [{'text': '塔吊消失', 'layer': 'local', 'beat': 1,
                                   'frames': [1, 2]}]}
        base.update(over)
        return base

    def _snapshot(self, **over):
        """按 fix_frame_issue 的调用方式存一份修复前快照。"""
        images, videos = po._parse_prompt_slots(self.PROMPT_BLOCK)
        return po.save_fix_snapshot(self.project_dir, self.TITLE, 2, self._entry(**over),
                                    images[2], 1, videos[1])

    def test_snapshot_keeps_the_frame_image_entry_and_both_prompt_bodies(self):
        self._touch_frame(2)
        meta = self._snapshot()
        snap_dir = po._fix_snapshot_dir(self.project_dir, 2)
        self.assertTrue(os.path.exists(os.path.join(snap_dir, 'img_002.webp')))
        self.assertEqual(meta['frame']['vlm_qa_reason'], '塔吊消失')
        self.assertIn('second frame', meta['image']['body'])
        self.assertIn('video one', meta['video']['body'])
        self.assertEqual(meta['video_beat'], 1)
        self.assertNotIn('url', meta['frame'])   # url 由渲染路径现算，与快照无关

    def test_undo_restores_image_prompt_bodies_and_the_recorded_problem(self):
        self._touch_frame(2)
        self._snapshot()
        # 修复发生了：帧图被覆盖、问题被清掉、两段提示词被改写
        with open(po._frame_path(self.TITLE, 2), 'wb') as f:
            f.write(b'fixed frame bytes')
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1}, {'sequence': 2, 'quality_gate': 'pending_manual_review',
                              'vlm_qa_reason': None, 'prompt': 'second frame 改过',
                              'url': '/outputs/x/frames/img_002.webp',
                              'fix_backup': {'at': 'T', 'reason': '塔吊消失'}},
            {'sequence': 3},
        ]})

        result = po.undo_frame_fix(self.TITLE, 2, self.FIXED_BLOCK)

        with open(po._frame_path(self.TITLE, 2), 'rb') as f:
            self.assertEqual(f.read(), b'fake webp bytes')      # 旧图回来了
        self.assertIn('second frame\n', result['prompt_block'])  # 图片正文回到修复前
        self.assertIn('video one\n', result['prompt_block'])     # 那条视频过渡也回来了
        frame = {f['sequence']: f for f in self._read_manifest()['frames']}[2]
        self.assertEqual(frame['quality_gate'], 'sequence_review_flagged')
        self.assertEqual(frame['vlm_qa_reason'], '塔吊消失')     # 问题回来了，可以重修
        self.assertEqual(frame['url'], '/outputs/x/frames/img_002.webp')
        self.assertNotIn('fix_backup', frame)                    # 快照已用掉

    def test_undo_only_rolls_back_its_own_slots(self):
        """修完 002 又修了 003 之后撤销 002：整体还原 prompt_block 会把 003 的修复
        一起吞掉，所以只换回这一帧涉及的那两个槽位。"""
        self._touch_frame(2)
        self._snapshot()
        server_common.write_manifest(self.project_dir, {'frames': [{'sequence': 2}]})
        later = self.FIXED_BLOCK.replace('third frame', 'third frame 后来也改过')

        result = po.undo_frame_fix(self.TITLE, 2, later)

        self.assertIn('third frame 后来也改过', result['prompt_block'])
        self.assertIn('second frame\n', result['prompt_block'])

    def test_undo_runs_the_shared_finalize(self):
        """撤销也是一次"这一帧的画面变了"：下游血统、相邻审查结论、成片都要作废。"""
        for s in (1, 2, 3):
            self._touch_frame(s)
        self._snapshot()
        with open(po._frame_path(self.TITLE, 2), 'wb') as f:
            f.write(b'fixed frame bytes')
        server_common.write_manifest(self.project_dir, {
            'frames': [{'sequence': 1}, self._entry(), {'sequence': 3}],
            'merged_video': {'file': 'merged.mp4'},
            'videos': [{'slot': 1}],
        })

        po.undo_frame_fix(self.TITLE, 2, self.FIXED_BLOCK)

        manifest = self._read_manifest()
        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertTrue(frames[3]['stale_lineage'])
        self.assertNotIn('stale_lineage', frames[1])
        self.assertNotIn('merged_video', manifest)
        self.assertEqual(manifest['videos'], [])

    def test_undo_without_a_snapshot_raises_instead_of_pretending(self):
        self._touch_frame(2)
        with self.assertRaises(RuntimeError):
            po.undo_frame_fix(self.TITLE, 2, self.PROMPT_BLOCK)

    def test_a_second_fix_replaces_the_snapshot_and_does_not_claim_a_stale_one(self):
        """只留最近一次：连修两轮之后人想回到的是"上一版"。快照里存的条目不能带着
        上一轮的 fix_backup——那枚记号指向的正是这份刚被覆盖掉的快照。"""
        self._touch_frame(2)
        self._snapshot()
        meta = self._snapshot(fix_backup={'at': '旧', 'reason': '旧问题'})
        self.assertNotIn('fix_backup', meta['frame'])
        server_common.write_manifest(self.project_dir, {'frames': [{'sequence': 2}]})
        restored = po.undo_frame_fix(self.TITLE, 2, self.FIXED_BLOCK)['frame']
        self.assertNotIn('fix_backup', restored)

    def test_snapshots_can_be_dropped_when_slot_numbers_move(self):
        """快照按帧号存（.frame_fixes/005/）。删除整拍会把其后的帧整体前移，
        fix_backup 记号跟着条目挪到 004，而 .frame_fixes/004 里躺的是另一帧的旧图
        ——不清掉就会"撤销"出一张张冠李戴的画面。"""
        self._touch_frame(2)
        self._snapshot()
        manifest = {'frames': [{'sequence': 2, 'fix_backup': {'at': 'T', 'reason': 'x'}},
                               {'sequence': 3, 'fix_backup': {'at': 'T', 'reason': 'y'}}]}

        po.drop_fix_snapshots(self.project_dir, manifest)

        self.assertFalse(os.path.exists(po._fix_snapshot_dir(self.project_dir, 2)))
        self.assertTrue(all('fix_backup' not in f for f in manifest['frames']))

    def test_dropping_one_slots_snapshot_leaves_the_others_alone(self):
        """手动上传只换掉这一格的图，别的格子的可撤销性不受影响。"""
        self._touch_frame(2)
        self._snapshot()
        manifest = {'frames': [{'sequence': 2, 'fix_backup': {'at': 'T', 'reason': 'x'}},
                               {'sequence': 3, 'fix_backup': {'at': 'T', 'reason': 'y'}}]}

        po.drop_fix_snapshots(self.project_dir, manifest, [2])

        self.assertFalse(os.path.exists(po._fix_snapshot_dir(self.project_dir, 2)))
        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertNotIn('fix_backup', frames[2])
        self.assertIn('fix_backup', frames[3])

    def test_fix_marks_the_frame_as_undoable_only_after_the_rerender(self):
        """记号必须在重渲之后盖：重渲会整体改写这条 manifest 条目，写在前面会被冲掉；
        重渲抛错时也不该留记号——那次修复没落地，没有新版本需要退回。"""
        self._touch_frame(1)
        self._touch_frame(2)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1}, self._entry(), {'sequence': 3}]})

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('video one 改过', 'second frame 改过')), \
             patch.object(csp, 'run_candidate_selection_frame_sequence',
                          side_effect=RuntimeError('上游炸了')), \
             patch.object(po, '_reverify_frame_issues', return_value=None):
            with self.assertRaises(RuntimeError):
                po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)
        frame = {f['sequence']: f for f in self._read_manifest()['frames']}[2]
        self.assertNotIn('fix_backup', frame)

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('video one 改过', 'second frame 改过')), \
             patch.object(csp, 'run_candidate_selection_frame_sequence', return_value=None), \
             patch.object(po, '_reverify_frame_issues', return_value=None):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        self.assertTrue(result['undoable'])
        frame = {f['sequence']: f for f in self._read_manifest()['frames']}[2]
        self.assertEqual(frame['fix_backup']['reason'], '塔吊消失')
        # 快照里存的是修复前的那份正文
        snap = po._read_fix_snapshot(self.project_dir, 2)
        self.assertIn('second frame', snap['image']['body'])


class TestIncrementalReview(_TmpProjectCase):
    """增量审查（2026-08-02）：只重审"结论已经失效"的那几拍，仍然成立的结论原样保留。

    此前每次都是全量——修完三帧再审一遍要把已经审干净的十来拍连同跨帧窗口整批重烧，
    几分钟起步，于是"修完就重审"这个本该最顺手的动作反而没人愿意做。判定材料
    （review_frames_sha256）与送审收窄的开关（only_beats / global_only_beats）早就都有。"""

    PROMPT_BLOCK_7 = (
        "图片提示词\n" + "".join(f"图片 {i}:\nframe {i}\n\n" for i in range(1, 8))
        + "视频提示词\n" + "".join(f"视频 {i}:\nvideo {i}\n\n" for i in range(1, 7))
    )

    def _write_frame(self, seq, color):
        Image.new('RGB', (16, 16), color).save(po._frame_path(self.TITLE, seq), format='WEBP')

    def _all_reviewed(self, n=7):
        for s in range(1, n + 1):
            self._write_frame(s, (s * 10, 0, 0))
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'sequence_reviewed_pass'} for s in range(1, n + 1)
        ]})
        po._record_review_fingerprints(self.project_dir, self.TITLE, list(range(1, n + 1)))

    def _run(self, result=None, full=False):
        """跑一轮审查，回报 check_full_sequence_consistency 收到的送审范围。"""
        calls = []

        def fake_check(config, prompt_block, frame_paths, **kw):
            calls.append(kw)
            return result if result is not None else _review({})

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK_7,
                                            self.project_dir, full=full)
        return calls

    def test_nothing_changed_means_no_model_calls_at_all(self):
        self._all_reviewed()
        events = []
        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=AssertionError('没有帧变过就不该再烧一次审查')):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK_7, self.project_dir,
                                            on_progress=lambda s, d: events.append((s, d)))
        result = [d for s, d in events if s == 'sequence_review_result'][0]
        self.assertTrue(result['passed'])
        self.assertEqual(result['reused_beats'], 6)
        self.assertIn('仍然成立', result['message'])

    def test_only_the_beats_around_a_changed_frame_are_resubmitted(self):
        self._all_reviewed()
        self._write_frame(4, (200, 200, 200))     # 只有 IMG 004 被重渲/修复过

        calls = self._run()

        # IMG 004 变了 → 003/004/005 的结论作废 → 覆盖它们的 beat 2..5 重审，
        # beat 1（001→002）与 beat 6（006→007）两头没被碰过，直接沿用
        self.assertEqual(calls[0]['only_beats'], [2, 3, 4, 5])
        self.assertEqual(calls[0]['global_only_beats'], [2, 3, 4, 5])

    def test_untouched_frames_keep_their_verdicts_and_timestamps(self):
        self._all_reviewed()
        before = {f['sequence']: f.get('reviewed_at')
                  for f in self._read_manifest()['frames']}
        self._write_frame(4, (200, 200, 200))

        self._run()

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        for seq in (1, 2, 6, 7):
            self.assertEqual(frames[seq]['quality_gate'], 'sequence_reviewed_pass')
            self.assertEqual(frames[seq].get('reviewed_at'), before[seq],
                             '没重审的帧不该被刷新成"刚审过"')
        for seq in (3, 4, 5):
            self.assertEqual(frames[seq]['quality_gate'], 'sequence_reviewed_pass')

    def test_an_unfixed_problem_from_an_earlier_round_is_not_washed_away(self):
        """一拍被选中重审，可能只是因为它**另一头**的帧变了；这一头上一轮判出来、
        还没修的问题不能被这一轮的"通过"洗掉——那条问题这轮压根没人复查过。

        场景：IMG 004 上一轮被 beat 3 判出问题、还没修；随后 IMG 006 被重渲。
        beat 4（004→005）因此要重审，但 beat 3 不用——IMG 004 的结论仍然成立。"""
        self._all_reviewed()
        with server_common.manifest_lock(self.project_dir):
            m = server_common.read_manifest(self.project_dir)
            for f in m['frames']:
                if f['sequence'] == 4:
                    f['quality_gate'] = 'sequence_review_flagged'
                    f['vlm_qa_reason'] = '层数对不上'
            server_common.write_manifest(self.project_dir, m)
        before = {f['sequence']: f.get('reviewed_at') for f in self._read_manifest()['frames']}
        self._write_frame(6, (200, 200, 200))

        calls = self._run()

        self.assertEqual(calls[0]['only_beats'], [4, 5, 6])   # IMG 004 参与的 beat 4 在内
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames[4]['quality_gate'], 'sequence_review_flagged')
        self.assertEqual(frames[4]['vlm_qa_reason'], '层数对不上')
        self.assertEqual(frames[4].get('reviewed_at'), before[4])

    def test_full_scope_reviews_everything_again(self):
        self._all_reviewed()
        calls = self._run(full=True)
        self.assertIsNone(calls[0]['only_beats'])
        self.assertIsNone(calls[0]['global_only_beats'])

    def test_result_reports_remaining_problems_even_when_nothing_was_rereviewed(self):
        """这一趟没发现新问题 ≠ 这套序列干净：上几轮标出来、还没修的问题必须照报。"""
        self._all_reviewed()
        with server_common.manifest_lock(self.project_dir):
            m = server_common.read_manifest(self.project_dir)
            m['frames'][2]['quality_gate'] = 'sequence_review_flagged'
            m['frames'][2]['vlm_qa_reason'] = '层数对不上'
            server_common.write_manifest(self.project_dir, m)
        events = []
        with patch.object(po, 'check_full_sequence_consistency',
                          side_effect=AssertionError('没有帧变过就不该再烧一次审查')):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK_7, self.project_dir,
                                            on_progress=lambda s, d: events.append((s, d)))
        result = [d for s, d in events if s == 'sequence_review_result'][0]
        self.assertFalse(result['passed'])
        self.assertIn('IMG 003', result['message'])

    def test_newly_rendered_frames_pull_in_only_the_beats_they_touch(self):
        """续渲：前 5 帧早就审过，新渲出 006/007 时只需审接上去的那两拍。"""
        self._all_reviewed(n=5)
        for s in (6, 7):
            self._write_frame(s, (s * 10, 0, 0))

        calls = self._run()

        self.assertEqual(calls[0]['only_beats'], [5, 6])

    def test_fingerprints_of_boundary_frames_cover_their_unreviewed_neighbour(self):
        """增量下边界帧的邻居这轮没被重审，但结论依然依赖那张图——指纹漏记的话，
        邻居之后被重渲时这条结论不会作废，增量会一直认为它有效、永远不再复查。"""
        self._all_reviewed()
        self._write_frame(4, (200, 200, 200))
        self._run()

        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        # IMG 003 这轮被重审，它的结论同时依赖 002/003/004
        self.assertEqual(set(frames[3]['review_frames_sha256']), {'2', '3', '4'})
        # 于是之后 IMG 002 被重渲，IMG 003 的结论会跟着作废
        self._write_frame(2, (7, 7, 7))
        self.assertIn(3, po.invalidate_stale_review_verdicts(self.project_dir))


class TestReverifyAfterFix(_TmpProjectCase):
    """修复闭环（2026-07-25）：重渲之后对着新画面把刚才那几条问题逐条再验一遍，
    直接回答"到底修好没有"。此前修复是开环的——重渲完把 gate 设回 pending_manual_review
    就走人，那条问题是否解决没有任何人回答。"""

    ISSUES = [
        {'text': '天花板未封板', 'layer': 'local', 'beat': 1, 'frames': [1, 2]},
        {'text': '载体身份丢失', 'layer': 'global', 'beat': 1, 'frames': [1, 2]},
    ]

    def _setup_flagged_frame(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_review_flagged',
             'vlm_qa_reason': '天花板未封板；载体身份丢失',
             'review_issues': [dict(i) for i in self.ISSUES]},
        ]})

    def _run_fix(self, verify_side_effect):
        with patch.object(po, 'fix_beat_from_sequence_review', return_value=('v', 'i')), \
             patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_verify_review_violation', side_effect=verify_side_effect):
            return po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

    def test_all_issues_gone_clears_the_flag(self):
        self._setup_flagged_frame()
        result = self._run_fix(lambda cfg, text, imgs: False)  # 复核：这条问题不存在了

        self.assertEqual(result['reverify']['remaining'], [])
        self.assertEqual(sorted(result['reverify']['resolved']),
                         ['天花板未封板', '载体身份丢失'])
        frame = self._read_manifest()['frames'][0]
        self.assertEqual(frame['quality_gate'], 'pending_manual_review')
        self.assertNotIn('review_issues', frame)

    def test_still_present_issue_keeps_the_frame_flagged_with_only_that_issue(self):
        self._setup_flagged_frame()
        result = self._run_fix(lambda cfg, text, imgs: text == '载体身份丢失')

        self.assertEqual(result['reverify']['resolved'], ['天花板未封板'])
        self.assertEqual(result['reverify']['remaining'], ['载体身份丢失'])
        frame = self._read_manifest()['frames'][0]
        self.assertEqual(frame['quality_gate'], 'sequence_review_flagged')
        self.assertEqual(frame['vlm_qa_reason'], '载体身份丢失')   # 已解决的那条不再挂着
        self.assertEqual([i['text'] for i in frame['review_issues']], ['载体身份丢失'])

    def test_verifier_infra_failure_never_reports_a_false_success(self):
        self._setup_flagged_frame()
        result = self._run_fix(lambda cfg, text, imgs: None)   # 复核本身没跑成

        self.assertEqual(result['reverify']['resolved'], [])
        self.assertEqual(len(result['reverify']['remaining']), 2)
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'],
                         'sequence_review_flagged')

    def test_reverify_uses_the_frames_recorded_on_each_issue(self):
        self._setup_flagged_frame()
        calls = []

        def spy(cfg, text, imgs):
            calls.append(imgs)
            return False

        self._run_fix(spy)
        for imgs in calls:
            self.assertEqual([os.path.basename(p) for p in imgs],
                             ['img_001.webp', 'img_002.webp'])

    def test_manual_description_without_structured_issues_still_gets_reverified(self):
        """旧 manifest / 人工描述没有结构化记录时，退化成"只看这一帧"的一条问题。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '塔吊凭空消失了')
        calls = []

        with patch.object(po, 'fix_beat_from_sequence_review', return_value=('v', 'i')), \
             patch.object(csp, 'run_candidate_selection_frame_sequence'), \
             patch.object(po, '_verify_review_violation',
                          side_effect=lambda cfg, text, imgs: calls.append(text) or False):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        self.assertEqual(calls, ['塔吊凭空消失了'])
        self.assertEqual(result['reverify']['resolved'], ['塔吊凭空消失了'])


class TestFixFrameViaImageEdit(_TmpProjectCase):
    def test_missing_frame_raises(self):
        with self.assertRaises(RuntimeError):
            po._fix_frame_via_image_edit({}, self.TITLE, 1, 'new prompt')

    def test_self_edit_updates_manifest_and_uses_own_file_as_reference(self):
        self._touch_frame(1)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'bad',
             'retry_count': 0},
        ]})
        edit_calls = []

        def fake_edit(config, prompt, reference_path, target_path, control_prompt=None):
            edit_calls.append((reference_path, target_path))

        with patch.object(po, '_generate_image_edit', side_effect=fake_edit), \
             patch.object(po, '_image_edit_model', return_value='edit-model'):
            result = po._fix_frame_via_image_edit({}, self.TITLE, 1, 'new prompt')

        target_path = po._frame_path(self.TITLE, 1)
        self.assertEqual(edit_calls, [(target_path, target_path)])  # 自己当参考、原地编辑
        self.assertEqual(result['sequence'], 1)
        frame = self._read_manifest()['frames'][0]
        self.assertEqual(frame['quality_gate'], 'pending_manual_review')
        self.assertIsNone(frame['vlm_qa_reason'])
        self.assertEqual(frame['prompt'], 'new prompt')
        self.assertEqual(frame['retry_count'], 1)

    def test_self_edit_runs_the_shared_finalize(self):
        """首帧被改画之后必须走与其它渲染路径同一个收尾：其后各帧仍派生自旧图
        → stale_lineage；看过这张图的审查结论作废；已合并成片与视频清单作废。

        此前这条通道自己开锁写 manifest、绕过了整个收尾——「重试首帧」会标记下游、
        「修复首帧」不会，同一件事两种结果；成片也会原样留在清单里，看着像还对得上。"""
        for s in (1, 2, 3):
            self._touch_frame(s)
        hashes = {s: server_common.frame_content_hash(po._frame_path(self.TITLE, s))
                  for s in (1, 2, 3)}
        server_common.write_manifest(self.project_dir, {
            'frames': [
                {'sequence': 1, 'quality_gate': 'sequence_reviewed_pass',
                 'review_frames_sha256': {'1': hashes[1], '2': hashes[2]}},
                {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass',
                 'review_frames_sha256': {str(s): hashes[s] for s in (1, 2, 3)}},
                # 第 3 帧看的是 2/3，与首帧无关：它的结论不该被这次修复牵连
                {'sequence': 3, 'quality_gate': 'sequence_reviewed_pass',
                 'review_frames_sha256': {'2': hashes[2], '3': hashes[3]}},
            ],
            'merged_video': {'file': 'merged.mp4'},
            'videos': [{'slot': 1, 'file': 'vid_001.mp4'}],
        })

        def fake_edit(config, prompt, reference_path, target_path, control_prompt=None):
            with open(target_path, 'wb') as f:
                f.write(b'edited webp bytes')   # 画面真的变了，哈希才会对不上

        with patch.object(po, '_generate_image_edit', side_effect=fake_edit), \
             patch.object(po, '_image_edit_model', return_value='edit-model'):
            po._fix_frame_via_image_edit({}, self.TITLE, 1, 'new prompt')

        manifest = self._read_manifest()
        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertNotIn('stale_lineage', frames[1])          # 本轮重生的那帧不标
        self.assertTrue(frames[2]['stale_lineage'])           # 其后各帧仍派生自旧首帧
        self.assertTrue(frames[3]['stale_lineage'])
        self.assertEqual(frames[2]['quality_gate'], 'pending_manual_review')
        self.assertNotIn('review_frames_sha256', frames[2])
        self.assertEqual(frames[3]['quality_gate'], 'sequence_reviewed_pass',
                         '没看过首帧的结论不该被这次修复牵连')
        self.assertNotIn('merged_video', manifest)
        self.assertEqual(manifest['videos'], [])

    def test_quota_exhausted_never_switches_models(self):
        """定向修复撞上配额耗尽：原样上抛，配了 imageEditFallbackModel 也不换模型。
        修复本就是"改这一处、其余不动"，换模型重画整张会把已确认的构图一起改掉。"""
        self._touch_frame(1)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'bad'},
        ]})
        calls = []

        def fake_edit(config, prompt, reference_path, target_path, control_prompt=None):
            calls.append(config.get('imageModel'))
            raise QuotaExhaustedError('quota')

        with patch.object(po, '_generate_image_edit', side_effect=fake_edit), \
             patch.object(po, '_image_edit_model', side_effect=lambda c: c.get('imageModel', 'primary')):
            with self.assertRaises(QuotaExhaustedError):
                po._fix_frame_via_image_edit(
                    {'imageEditFallbackModel': 'fallback-model'}, self.TITLE, 1, 'new prompt')

        self.assertEqual(len(calls), 1)

    def test_quota_exhausted_reraises(self):
        self._touch_frame(1)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'bad'},
        ]})
        with patch.object(po, '_generate_image_edit', side_effect=QuotaExhaustedError('quota')):
            with self.assertRaises(QuotaExhaustedError):
                po._fix_frame_via_image_edit({}, self.TITLE, 1, 'new prompt')

    def test_chat_transport_is_recorded_and_cleared_on_edits_rerender(self):
        """定向修复落进「同模型换传输通道」的兜底时（网关 /images/edits 号池墙，见
        frame_generator.CHAT_TRANSPORT）：模型没换所以构图不会被重画，但那条通道固定
        出 1K 档——请求 2K 就是降档，manifest 必须如实标注；之后走 /images/edits 重渲过，
        过期的留痕必须清掉。"""
        from PIL import Image
        Image.new('RGB', (768, 1376), (10, 20, 30)).save(po._frame_path(self.TITLE, 1), format='WEBP')
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'bad'},
        ]})
        events = []

        with patch.object(po, '_generate_image_edit', return_value=po.CHAT_TRANSPORT), \
             patch.object(po, '_image_edit_model', return_value='gemini-3.1-flash-image'):
            po._fix_frame_via_image_edit({'imageQuality': '2K'}, self.TITLE, 1, 'new prompt',
                                         on_progress=lambda s, d: events.append((s, d)))

        frame = self._read_manifest()['frames'][0]
        self.assertEqual(frame['transport'], po.CHAT_TRANSPORT)
        self.assertIn('分辨率降档', frame['degraded_reason'])
        self.assertEqual(frame['actual_pixels'], '768x1376')
        announced = [d for s, d in events if s == 'transport_fallback']
        self.assertTrue(announced and announced[0]['degraded'])

        # 本单请求就是 1K 时同一条通道并没有损失画质，不许扣"降档"帽子
        with patch.object(po, '_generate_image_edit', return_value=po.CHAT_TRANSPORT), \
             patch.object(po, '_image_edit_model', return_value='gemini-3.1-flash-image'):
            po._fix_frame_via_image_edit({'imageQuality': '1K'}, self.TITLE, 1, 'new prompt')
        frame = self._read_manifest()['frames'][0]
        self.assertEqual(frame['transport'], po.CHAT_TRANSPORT)
        self.assertNotIn('degraded_reason', frame)

        # 额度恢复后再修一次：走 /images/edits，通道留痕不许留在原地误导人
        with patch.object(po, '_generate_image_edit', return_value=None), \
             patch.object(po, '_image_edit_model', return_value='gemini-3.1-flash-image'):
            po._fix_frame_via_image_edit({}, self.TITLE, 1, 'newer prompt')

        frame = self._read_manifest()['frames'][0]
        for key in ('transport', 'degraded_reason', 'actual_pixels'):
            self.assertNotIn(key, frame)

    def test_google_fx_backend_rejected_with_clear_error(self):
        self._touch_frame(1)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'bad'},
        ]})
        with patch.object(po, '_generate_image_edit',
                          side_effect=AssertionError('不该打到直连网关')):
            with self.assertRaises(RuntimeError):
                po._fix_frame_via_image_edit({'imageBackend': 'google_fx'}, self.TITLE, 1, 'new prompt')


class TestManualFrameIssue(_TmpProjectCase):
    """人工主动描述某一帧的问题：描述记进 manifest 的 manual_issue（quality_gate 标
    manual_flagged），与机器判定的 vlm_qa_reason 并存，随后由 fix_frame_issue 一起
    交给提示词改写。"""

    def setUp(self):
        super().setUp()
        patcher = patch.object(po, '_verify_review_violation', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flag_records_description_and_remembers_previous_gate(self):
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass', 'vlm_qa_reason': None},
        ]})
        frame = po.set_manual_frame_issue(self.TITLE, 2, '  塔吊凭空消失了  ')

        self.assertEqual(frame['manual_issue'], '塔吊凭空消失了')  # 两端空白已剥掉
        stored = self._read_manifest()['frames'][0]
        self.assertEqual(stored['manual_issue'], '塔吊凭空消失了')
        self.assertEqual(stored['quality_gate'], 'manual_flagged')
        self.assertEqual(stored['manual_flag_prev_gate'], 'sequence_reviewed_pass')

    def test_empty_description_clears_flag_and_restores_previous_gate(self):
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '塔吊凭空消失了')
        po.set_manual_frame_issue(self.TITLE, 2, '   ')

        stored = self._read_manifest()['frames'][0]
        self.assertNotIn('manual_issue', stored)
        self.assertNotIn('manual_flag_prev_gate', stored)
        self.assertEqual(stored['quality_gate'], 'sequence_reviewed_pass')

    def test_reflagging_does_not_overwrite_remembered_previous_gate(self):
        # 改描述（manual_flagged -> manual_flagged）时若把 prev_gate 覆盖成
        # manual_flagged，撤销后 gate 就永远回不去原值了
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '第一版描述')
        po.set_manual_frame_issue(self.TITLE, 2, '第二版描述')
        po.set_manual_frame_issue(self.TITLE, 2, '')

        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'sequence_reviewed_pass')

    def test_clear_without_remembered_gate_falls_back_by_vlm_reason(self):
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'manual_flagged', 'manual_issue': '旧描述',
             'vlm_qa_reason': '天花板未随墙面一起封板'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '')
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'sequence_review_flagged')

        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'manual_flagged', 'manual_issue': '旧描述'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '')
        self.assertEqual(self._read_manifest()['frames'][0]['quality_gate'], 'pending_manual_review')

    def test_missing_manifest_or_frame_raises(self):
        with self.assertRaises(RuntimeError):
            po.set_manual_frame_issue(self.TITLE, 2, '描述')
        server_common.write_manifest(self.project_dir, {'frames': [{'sequence': 1}]})
        with self.assertRaises(RuntimeError):
            po.set_manual_frame_issue(self.TITLE, 2, '描述')

    def test_fix_uses_manual_issue_when_review_never_flagged_it(self):
        """一致性审查没标记过的帧，只要人工描述过问题就能修——这正是本功能要解决的
        缺口（此前只有被审查标记的帧才有修复入口）。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'sequence_reviewed_pass', 'vlm_qa_reason': None}
            for s in (1, 2, 3)
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '塔吊凭空消失了')

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')) as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence'):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        mock_fix.assert_called_once_with({}, 'video one', 'second frame', ['塔吊凭空消失了'],
                                         video_meta='', preceding_image_prompt='first frame',
                                         succeeding_image_prompt='third frame', succeeding_video_prompt='video two')
        self.assertEqual(result['reason'], '塔吊凭空消失了')
        # 修完清掉描述，否则帧网格会一直显示「人工标记」看着像没修
        stored = [f for f in self._read_manifest()['frames'] if f['sequence'] == 2][0]
        self.assertNotIn('manual_issue', stored)
        self.assertNotEqual(stored['quality_gate'], 'manual_flagged')

    def test_fix_merges_manual_description_with_machine_verdict(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_review_flagged',
             'vlm_qa_reason': '天花板未随墙面一起封板'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '塔吊凭空消失了')

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')) as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence'):
            result = po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        # 人工描述排在机器判定前面，两份都交给改写
        mock_fix.assert_called_once_with({}, 'video one', 'second frame',
                                         ['塔吊凭空消失了', '天花板未随墙面一起封板'],
                                         video_meta='', preceding_image_prompt='first frame',
                                         succeeding_image_prompt='third frame', succeeding_video_prompt='video two')
        self.assertEqual(result['reason'], '塔吊凭空消失了；天花板未随墙面一起封板')

    def test_fix_accepts_manual_reason_argument_and_persists_it_first(self):
        """在修复对话框里现场写的描述：先落盘再修，中途失败描述也不会丢。"""
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_reviewed_pass'},
        ]})
        seen = {}

        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4):
            seen['gate'] = self._read_manifest()['frames'][0]['quality_gate']
            seen['issue'] = self._read_manifest()['frames'][0].get('manual_issue')
            raise RuntimeError('上游炸了')

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('fixed video 1', 'fixed image 2')), \
             patch.object(csp, 'run_candidate_selection_frame_sequence', side_effect=fake_render):
            with self.assertRaises(RuntimeError):
                po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2,
                                   manual_reason='塔吊凭空消失了')

        self.assertEqual(seen['gate'], 'manual_flagged')
        self.assertEqual(seen['issue'], '塔吊凭空消失了')
        # 重渲抛错后描述仍留在 manifest 上，人不用重写一遍
        stored = self._read_manifest()['frames'][0]
        self.assertEqual(stored['manual_issue'], '塔吊凭空消失了')

    def test_duplicate_manual_and_machine_text_is_not_sent_twice(self):
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 2, 'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': '同一句话'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '同一句话')

        with patch.object(po, 'fix_beat_from_sequence_review',
                          return_value=('v', 'i')) as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence'):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 2)

        self.assertEqual(mock_fix.call_args.args[3], ['同一句话'])

    def test_first_frame_manual_issue_goes_through_image_edit(self):
        self._touch_frame(1)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'sequence_reviewed_pass'},
        ]})
        po.set_manual_frame_issue(self.TITLE, 1, '首帧地面太干净，不像废墟')

        render_calls = []
        def fake_render(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4):
            render_calls.append(target_sequences)

        with patch.object(po, 'fix_image_prompt_with_vlm_feedback',
                          return_value='fixed first frame') as mock_fix, \
             patch.object(csp, 'run_candidate_selection_frame_sequence', side_effect=fake_render):
            po.fix_frame_issue({}, self.TITLE, self.PROMPT_BLOCK, 1)

        mock_fix.assert_called_once_with({}, 'first frame', '首帧地面太干净，不像废墟', succeeding_image_prompt='second frame')
        self.assertEqual(render_calls, [[1]])



class TestGlobalReviewWindows(unittest.TestCase):
    """跨帧稀疏审查的分批口径（2026-07-30）。

    规则数早在 2026-07-23 就从 30 余条收窄到 6 条，图片数却一直没收窄：一单 13 帧仍是
    14 张图一次性喂进一次调用——正是当初拆出逐拍审查要避开的注意力稀释，只是这次来自
    图片而不是规则。现在切成重叠窗口，每窗最多 6 张（含恒定作基准的链头帧）。"""

    def test_short_sequence_stays_one_window(self):
        """帧数不超过窗口容量时必须与分批前完全一致，短单不该被改动波及。"""
        self.assertEqual(pp.global_review_windows([1, 2, 3]), [[1, 2, 3]])
        self.assertEqual(pp.global_review_windows([1, 2, 3, 4, 5, 6]), [[1, 2, 3, 4, 5, 6]])

    def test_long_sequence_is_split_with_head_in_every_window(self):
        windows = pp.global_review_windows(list(range(1, 15)))
        self.assertEqual(windows, [
            [1, 2, 3, 4, 5, 6],
            [1, 6, 7, 8, 9, 10],
            [1, 10, 11, 12, 13, 14],
        ])
        # 链头帧进每一个窗口：跨帧规则问的是"还是不是同一个空间/载体"，没有未被触碰
        # 的原始状态作基准就无从判断
        for win in windows:
            self.assertEqual(win[0], 1)
            self.assertLessEqual(len(win), 6)

    def test_every_beat_is_covered_by_some_window(self):
        """窗口接缝处不能漏拍：拍 N 需要 IMAGE N 与 N+1 同在一个窗口里，重叠 1 帧
        就是为此存在。任一拍没人管＝那一段的跨帧漂移永远查不出来。"""
        for total_frames in range(2, 40):
            seqs = list(range(1, total_frames + 1))
            total_beats = total_frames - 1
            covered = set()
            for win in pp.global_review_windows(seqs):
                covered.update(pp._reportable_beats(win, total_beats))
            self.assertEqual(covered, set(range(1, total_beats + 1)), total_frames)

    def test_non_contiguous_sequences_are_handled(self):
        """帧号不连续（某几拍被删过）时也不能崩，且不会凭空补出不存在的拍。"""
        windows = pp.global_review_windows([1, 2, 5, 6, 9, 10, 11, 12])
        for win in windows:
            self.assertEqual(win[0], 1)
        self.assertEqual(pp._reportable_beats([1, 2, 5, 6], 11), [1, 5])

    def test_empty_input(self):
        self.assertEqual(pp.global_review_windows([]), [])


class TestGlobalReviewBatchedCalls(unittest.TestCase):
    """分批之后 check_global_sequence_consistency 的调用与合并契约。"""

    FRAMES = {s: f'img_{s:03d}.webp' for s in range(1, 15)}

    def test_each_call_gets_only_its_window_images(self):
        calls = []

        def fake_chat(config, system, user, paths, **kw):
            calls.append((paths, user))
            return '{}'

        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat):
            failures = pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES)

        self.assertEqual(failures, {})
        self.assertEqual(len(calls), 3)
        for paths, user in calls:
            self.assertLessEqual(len(paths), 6)
            self.assertEqual(paths[0], 'img_001.webp')
            # user turn 必须点明真实 IMAGE 号，否则模型会把附件从 1 重新编号
            self.assertIn('IMAGE 1', user)

    def test_system_prompt_is_constant_across_windows(self):
        """系统提示词不再内插拍数：跨窗口/跨单共用同一前缀才吃得到 prompt 缓存，
        否则分批之后浪费翻倍（同 _local_beat_review_system_prompt 的 2026-07-25 改造）。"""
        systems = []
        with patch.object(pp, '_multimodal_chat',
                          side_effect=lambda c, s, u, p, **kw: systems.append(s) or '{}'):
            pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES)
        self.assertEqual(len(set(systems)), 1)

    def test_violations_from_all_windows_are_merged(self):
        def fake_chat(config, system, user, paths, **kw):
            if 'IMAGE 2' in user:
                return json.dumps({'3': ['材质突变']})
            if 'IMAGE 12' in user:
                return json.dumps({'11': ['载体身份丢失']})
            return '{}'

        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat):
            failures = pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES)
        self.assertEqual(failures, {3: ['材质突变'], 11: ['载体身份丢失']})

    def test_beats_outside_the_window_are_dropped(self):
        """窗口只看得见自己那几帧，报窗口外的拍号只能是模型按附件重新编号编出来的
        ——收下就是把违规挂到无关的帧上。"""
        def fake_chat(config, system, user, paths, **kw):
            # 第一窗（IMAGE 1..6）谎报第 12 拍
            if 'IMAGE 2' in user:
                return json.dumps({'12': ['窗口外的拍'], '2': ['窗口内的拍']})
            return '{}'

        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat):
            failures = pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES)
        self.assertEqual(failures, {2: ['窗口内的拍']})

    def test_partial_window_failure_keeps_findings_and_reports_unreviewed(self):
        """一个窗口没跑成时不再整层判失败（那会把已查出的违规一起扔掉），而是把
        该窗覆盖的拍号带回给调用方——那些帧因此拿不到"已审查通过"的章。"""
        def fake_chat(config, system, user, paths, **kw):
            if 'IMAGE 12' in user:
                raise RuntimeError('gateway down')
            if 'IMAGE 2' in user:
                return json.dumps({'2': ['材质突变']})
            return '{}'

        unreviewed = []
        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat):
            failures = pp.check_global_sequence_consistency(
                {}, 'prompt', self.FRAMES, unreviewed_beats_out=unreviewed)
        self.assertEqual(failures, {2: ['材质突变']})
        # 第三窗 [1,10,11,12,13,14] → 拍 10..13
        self.assertEqual(unreviewed, [10, 11, 12, 13])

    def test_failed_window_does_not_taint_its_neighbours(self):
        """漏审只记在失败那一窗自己覆盖的拍上。

        默认 overlap=1 下每一拍恰好归属一个窗口（重叠的是帧不是拍：IMAGE 6 同时进
        第一、第二窗，好让拍 5 = 5→6 与拍 6 = 6→7 各自完整落在一窗内）。所以第二窗
        失败时，第一窗审干净的拍 1..5 必须原样保持"已审"。"""
        def fake_chat(config, system, user, paths, **kw):
            if 'IMAGE 7' in user:      # 第二窗失败
                raise RuntimeError('gateway down')
            return '{}'

        unreviewed = []
        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat):
            pp.check_global_sequence_consistency(
                {}, 'prompt', self.FRAMES, unreviewed_beats_out=unreviewed)
        self.assertEqual(unreviewed, [6, 7, 8, 9])

    def test_wider_overlap_lets_a_neighbour_rescue_a_beat(self):
        """overlap 调大时同一拍会被多窗覆盖，任一窗审成就不算漏审——这条是
        unreviewed 计算里"减去别窗已审过的拍"那一步的契约（默认口径下用不到，
        但把 overlap 调宽是排查漂移时的常规手段，那时它必须成立）。"""
        windows = pp.global_review_windows(list(range(1, 15)), window=6, overlap=3)
        beat_windows = [w for w in windows if 6 in pp._reportable_beats(w, 13)]
        self.assertGreaterEqual(len(beat_windows), 2, windows)

        def fake_chat(config, system, user, paths, **kw):
            # 只让其中一个覆盖拍 6 的窗口失败
            if user.count('IMAGE 6') and 'IMAGE 4' in user:
                raise RuntimeError('gateway down')
            return '{}'

        unreviewed = []
        with patch.object(pp, '_multimodal_chat', side_effect=fake_chat), \
             patch.object(pp, 'global_review_windows', return_value=windows):
            pp.check_global_sequence_consistency(
                {}, 'prompt', self.FRAMES, unreviewed_beats_out=unreviewed)
        self.assertNotIn(6, unreviewed)

    def test_all_windows_failing_returns_none(self):
        """整层都没跑成仍必须返回 None：调用方绝不能把它当"通过"。"""
        with patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('down')):
            self.assertIsNone(pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES))

    def test_full_review_merges_global_unreviewed_beats(self):
        """窗口漏审必须并进 check_full_sequence_consistency 的 unreviewed_beats，
        否则 frame_review_status 会给那些帧盖上"已审查通过"（2026-07-15 fail-open
        事故的同款）。"""
        def fake_global(config, prompt_block, frames, **kw):
            out = kw.get('unreviewed_beats_out')
            if out is not None:
                out.extend([10, 11])
            return {}

        with patch.object(pp, 'check_beat_consistency', return_value=[]), \
             patch.object(pp, 'check_global_sequence_consistency', side_effect=fake_global):
            result = pp.check_full_sequence_consistency({}, 'prompt', self.FRAMES)
        self.assertTrue(result['global_reviewed'])
        self.assertEqual(result['unreviewed_beats'], [10, 11])


class TestGlobalWindowFailureIsNotWashedAwayByRetry(_TmpProjectCase):
    """回归防护（2026-07-30，跨帧分批改造自带的坑）：跨帧层按窗口分批之后，个别窗口
    没跑成时 global_reviewed 仍然是 True（其余窗口有判定）。降级重试若只看这个布尔值
    就会 skip_global=True，于是那几个失败的窗口根本没被补跑；而重试的**本地层**对那
    几拍是成功的，merge 又以重试结果为准——那几帧最后拿到了 sequence_reviewed_pass。

    净效果比不重试还糟：一次网关抖动把"跨帧规则没查过"洗成了"查过且通过"。这正是
    2026-07-15 盐湖贝壳单 fail-open 的同款形态，只是粒度从整批缩到了窗口。"""

    def setUp(self):
        super().setUp()
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})

    def _run(self, retry_result):
        calls = []

        def fake_check(config, prompt_block, frame_paths, degraded=False,
                       only_beats=None, skip_global=False, on_progress=None,
                       global_only_beats=None):
            calls.append({'degraded': degraded, 'only_beats': only_beats,
                          'skip_global': skip_global, 'global_only_beats': global_only_beats})
            if not degraded:
                # 第一轮：本地层全成，但覆盖第 2 拍的跨帧窗口没跑成
                return _review({}, unreviewed_beats=[2], global_reviewed=True,
                               global_unreviewed_beats=[2])
            return retry_result

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        return calls, {f['sequence']: f for f in self._read_manifest()['frames']}

    def test_retry_reruns_the_failed_window_instead_of_skipping_global(self):
        calls, _ = self._run(_review({}, unreviewed_beats=[], global_unreviewed_beats=[]))
        self.assertEqual(len(calls), 2)
        # 有窗口漏审 → 必须补跑跨帧层，且只补跑覆盖第 2 拍的那个窗口
        self.assertFalse(calls[1]['skip_global'])
        self.assertEqual(calls[1]['global_only_beats'], [2])

    def test_beats_pass_only_after_the_window_actually_reran(self):
        _, frames = self._run(_review({}, unreviewed_beats=[], global_unreviewed_beats=[]))
        for seq in (1, 2, 3):
            self.assertEqual(frames[seq]['quality_gate'], 'sequence_reviewed_pass', seq)

    def test_window_failing_again_keeps_the_frames_unreviewed(self):
        """补跑仍然失败 → 那几帧必须保持"未经审查"，绝不能盖通过章。"""
        _, frames = self._run(_review({}, unreviewed_beats=[2], global_unreviewed_beats=[2],
                                       global_reviewed=False))
        # 第 2 拍覆盖 IMG 002 与 IMG 003
        self.assertNotEqual(frames[2]['quality_gate'], 'sequence_reviewed_pass')
        self.assertNotEqual(frames[3]['quality_gate'], 'sequence_reviewed_pass')

    def test_merge_carries_window_gaps_when_retry_skipped_global(self):
        """merge 的契约：second 没跑跨帧层时，上一轮的窗口漏审必须原样留着。
        把它抹掉就是凭空发通过章——那几帧的跨帧规则至今没人查过。"""
        first = _review({}, unreviewed_beats=[2], global_reviewed=True,
                        global_unreviewed_beats=[2])
        second = _review({}, unreviewed_beats=[], global_reviewed=False,
                         global_unreviewed_beats=[], global_attempted=False)
        merged = pp.merge_review_results(first, second)
        self.assertEqual(merged['unreviewed_beats'], [2])
        self.assertEqual(merged['global_unreviewed_beats'], [2])

    def test_merge_clears_window_gaps_when_retry_did_rerun_global(self):
        first = _review({}, unreviewed_beats=[2], global_reviewed=True,
                        global_unreviewed_beats=[2])
        second = _review({}, unreviewed_beats=[], global_reviewed=True,
                         global_unreviewed_beats=[], global_attempted=True)
        merged = pp.merge_review_results(first, second)
        self.assertEqual(merged['unreviewed_beats'], [])
        self.assertEqual(merged['global_unreviewed_beats'], [])


class TestGlobalWindowRestriction(unittest.TestCase):
    """check_global_sequence_consistency 的 only_beats：补跑时只重跑覆盖这些拍的窗口，
    不把已经审干净的窗口整批再烧一遍。"""

    FRAMES = {s: f'img_{s:03d}.webp' for s in range(1, 15)}

    def test_only_beats_restricts_which_windows_run(self):
        users = []
        with patch.object(pp, '_multimodal_chat',
                          side_effect=lambda c, s, u, p, **kw: users.append(u) or '{}'):
            pp.check_global_sequence_consistency(
                {}, 'prompt', self.FRAMES, only_beats=[11])
        # 14 帧共 3 个窗口，只有第三窗 [1,10..14] 覆盖第 11 拍
        self.assertEqual(len(users), 1)
        self.assertIn('IMAGE 11', users[0])

    def test_only_beats_none_runs_every_window(self):
        users = []
        with patch.object(pp, '_multimodal_chat',
                          side_effect=lambda c, s, u, p, **kw: users.append(u) or '{}'):
            pp.check_global_sequence_consistency({}, 'prompt', self.FRAMES, only_beats=None)
        self.assertEqual(len(users), 3)


class TestManualFlagSurvivesReview(_TmpProjectCase):
    """回归防护（2026-07-30）：一致性审查不得把人工标记洗掉。

    实测链路：用户在帧网格描述了 IMG 002 的问题（「门开反了」）→ quality_gate 变成
    manual_flagged，视频门禁据此硬拦；随后跑一次一致性审查，机器没看出这个问题 →
    审查主循环无条件覆盖 quality_gate 为 sequence_reviewed_pass → 那道硬拦消失，
    用户明确说有问题的帧会被拿去烧视频额度。

    最坏的地方在于 manual_issue 字段还留着：帧网格照旧显示「人工标记」徽标，界面说
    标了、门禁说没标。set_manual_frame_issue 的注释早写明两者应当并存（机器判定进
    vlm_qa_reason，人的描述进 manual_issue），只是审查的写入端没有遵守。"""

    def setUp(self):
        super().setUp()
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)
        ]})
        po.set_manual_frame_issue(self.TITLE, 2, '门开反了')

    def _review(self, result):
        with patch.object(po, 'check_full_sequence_consistency', return_value=result):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        return {f['sequence']: f for f in self._read_manifest()['frames']}

    def test_clean_review_does_not_clear_the_manual_flag(self):
        frames = self._review(_review())
        self.assertEqual(frames[2]['quality_gate'], 'manual_flagged')
        self.assertEqual(frames[2]['manual_issue'], '门开反了')
        # 未被标记的帧照常拿到审查结论
        self.assertEqual(frames[1]['quality_gate'], 'sequence_reviewed_pass')

    def test_video_gate_still_blocks_the_flagged_frame(self):
        """这条才是要害：门禁看的是 quality_gate。"""
        import video_generator as vg
        frames = self._review(_review())
        self.assertIn(frames[2]['quality_gate'], vg._FLAGGED_QUALITY_GATES)

    def test_machine_verdict_is_kept_for_undo(self):
        """人工标记压着机器判定，但那份判定不能丢：撤销标记时要回落到**最新**的机器
        结论，而不是被标记之前那个过期的。"""
        frames = self._review(_review())
        self.assertEqual(frames[2]['manual_flag_prev_gate'], 'sequence_reviewed_pass')
        self.assertIn('未发现', frames[2]['vlm_qa_reason'] or '未发现')

        restored = po.set_manual_frame_issue(self.TITLE, 2, '')
        self.assertEqual(restored['quality_gate'], 'sequence_reviewed_pass')

    def test_review_finding_its_own_problem_still_records_it(self):
        """机器也检出问题时，人工标记继续压在上面（两者都是"有问题"，门禁照拦），
        但审查结论必须照常落进 vlm_qa_reason/review_issues，不能因为压着就不记。"""
        frames = self._review(_review(
            {1: ['材质突变']},
            ))
        self.assertEqual(frames[2]['quality_gate'], 'manual_flagged')
        self.assertIn('材质突变', frames[2]['vlm_qa_reason'])
        self.assertEqual(frames[2]['manual_flag_prev_gate'], 'sequence_review_flagged')

    def test_review_service_down_does_not_clear_the_manual_flag_either(self):
        """审查服务不可用那条路径（标 sequence_review_skipped）同样不能覆盖人工标记。"""
        with patch.object(po, 'check_full_sequence_consistency', return_value=None):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        frames = {f['sequence']: f for f in self._read_manifest()['frames']}
        self.assertEqual(frames[2]['quality_gate'], 'manual_flagged')
        self.assertEqual(frames[1]['quality_gate'], 'sequence_review_skipped')

    def test_stashed_verdict_is_invalidated_when_the_frame_changes(self):
        """帧图变了，被压在下面的机器判定同样不再成立——留着的话，之后撤销人工标记
        会回落到一个针对旧画面的"审查通过"。"""
        self._review(_review())
        manifest = self._read_manifest()
        target = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertEqual(target['manual_flag_prev_gate'], 'sequence_reviewed_pass')
        # 伪造"这一帧此后被重渲过"：记下的指纹与磁盘上的内容对不上
        target['review_frames_sha256'] = {'2': 'deadbeef'}
        server_common.write_manifest(self.project_dir, manifest)

        server_common.drop_stale_review_verdicts(manifest, self.project_dir)
        target = next(f for f in manifest['frames'] if f['sequence'] == 2)
        self.assertEqual(target['manual_flag_prev_gate'], 'pending_manual_review')
        self.assertEqual(target['quality_gate'], 'manual_flagged')   # 人工标记本身还在


class TestOutlineFrameAuditIsGrayOnly(_TmpProjectCase):
    """卡片工序的**画面层**交付审查（2026-08-05 P2）。

    工序原文靠 manifest 里的交付总账过河（审查是用户手动触发的独立入口，跟合成那次
    运行不在同一个进程生命周期里）。灰度期这一层的判定只回写总账、只打日志：
    failures / quality_gate 一个字都不许变——VLM 判"这条施工工序算不算完成"的尺度
    还是未知量，误判率没摸清之前不能让它拦单。"""

    LEDGER = [
        {'index': 1, 'text': '清空洞内碎冰与积雪', 'delivery': 'shovel out the cave ice',
         'claimed_beats': [1], 'frame_seqs': [2], 'plan_verdict': 'claimed',
         'prompt_verdict': 'delivered', 'frame_verdict': 'unreviewed', 'note': ''},
        {'index': 2, 'text': '铺设隐蔽水管与地暖', 'delivery': 'run the hidden pipe circuits',
         'claimed_beats': [2], 'frame_seqs': [3], 'plan_verdict': 'claimed',
         'prompt_verdict': 'delivered', 'frame_verdict': 'not_applicable',
         'note': '隐蔽工序，封盖后不可见'},
    ]

    def setUp(self):
        super().setUp()
        for seq in (1, 2, 3):
            self._touch_frame(seq)
        server_common.write_manifest(self.project_dir, {
            'frames': [{'sequence': s, 'quality_gate': 'pending_manual_review'}
                       for s in (1, 2, 3)],
            'outline_delivery_ledger': [dict(row) for row in self.LEDGER],
        })

    def _run(self, result):
        seen = {}

        def fake_check(config, prompt_block, frame_paths, **kw):
            seen['outline_items'] = kw.get('outline_items')
            return result

        with patch.object(po, 'check_full_sequence_consistency', side_effect=fake_check):
            po._sequence_consistency_review({}, self.TITLE, self.PROMPT_BLOCK, self.project_dir)
        manifest = self._read_manifest()
        return seen, manifest

    def test_manifest_carries_outline_items_for_the_review_stage(self):
        seen, _ = self._run(_review())
        self.assertEqual(sorted(seen['outline_items']), ['1', '2'])
        self.assertEqual([i['text'] for i in seen['outline_items']['1']],
                         ['清空洞内碎冰与积雪'])

    def test_a_project_without_a_ledger_passes_no_items(self):
        """老单：连这个入参都不传，整条审查链路的调用形状与改造前逐字相同。"""
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': 'pending_manual_review'} for s in (1, 2, 3)]})
        seen, _ = self._run(_review())
        self.assertIsNone(seen['outline_items'])

    def test_frame_verdict_is_observed_but_never_flags_while_gray(self):
        result = dict(_review(), outline_frame_verdicts={'1': 'missing', '2': 'missing'})
        _, manifest = self._run(result)
        rows = {r['index']: r for r in manifest['outline_delivery_ledger']}
        self.assertEqual(rows[1]['frame_verdict'], 'missing')
        # 隐蔽工序的确定性结论不被 VLM 的"看不见"覆盖
        self.assertEqual(rows[2]['frame_verdict'], 'not_applicable')
        # quality_gate 与没有总账时逐字相同：这一层判定绝不外溢
        frames = {f['sequence']: f for f in manifest['frames']}
        self.assertEqual([frames[s]['quality_gate'] for s in (1, 2, 3)],
                         ['sequence_reviewed_pass'] * 3)
        self.assertTrue(all(not f.get('review_issues') for f in manifest['frames']))

    def test_persisting_the_ledger_creates_the_project_manifest_if_needed(self):
        """合成收尾时项目目录往往还没建（封面/首帧才建），落盘必须自己兜住。"""
        fresh = os.path.join(self.tmp, 'brand_new_project')
        self.assertTrue(po.persist_outline_delivery_ledger(fresh, self.LEDGER, title='t'))
        self.assertEqual(po._outline_items_for_review(fresh).keys(), {'1', '2'})
        # 空账不落盘，也不建目录
        empty = os.path.join(self.tmp, 'never_created')
        self.assertFalse(po.persist_outline_delivery_ledger(empty, []))
        self.assertFalse(os.path.exists(empty))


if __name__ == '__main__':
    unittest.main()
