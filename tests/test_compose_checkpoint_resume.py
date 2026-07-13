"""断点续传回归测试:提示词合成任务(compose_anchor_and_packet / compose_remaining_beats)
中断或失败后,重试必须从最后一个成功的拍(beat)续传,而不是把 Phase 1(brief/工序/
Drift Lock 包/IMAGE 1)和已经生成过的拍全部重新跑一遍。

覆盖三层:
1. 存档原语本身(load/save/clear_compose_checkpoint)的读写往返。
2. compose_anchor_and_packet 命中存档时整段跳过 Phase 1(不再调用 _chat)。
3. compose_remaining_beats 命中存档时只重新生成尚未成功的拍;中途崩溃(NameError 等
   代码级错误,现实中最容易触发"合成失败"的场景)后存档要精确停在最后一拍。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


class TestCheckpointStorage(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()

    def tearDown(self):
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_missing_checkpoint_returns_none(self):
        self.assertIsNone(pp.load_compose_checkpoint('does-not-exist'))

    def test_save_load_round_trip(self):
        pp.save_compose_checkpoint('fp-1', {'title': 'Hello', 'compiled_images': {'1': 'a'}})
        loaded = pp.load_compose_checkpoint('fp-1')
        self.assertEqual(loaded['title'], 'Hello')
        self.assertEqual(loaded['compiled_images'], {'1': 'a'})

    def test_clear_removes_only_that_fingerprint(self):
        pp.save_compose_checkpoint('fp-a', {'title': 'A'})
        pp.save_compose_checkpoint('fp-b', {'title': 'B'})
        pp.clear_compose_checkpoint('fp-a')
        self.assertIsNone(pp.load_compose_checkpoint('fp-a'))
        self.assertEqual(pp.load_compose_checkpoint('fp-b')['title'], 'B')

    def test_encode_decode_slots_round_trip(self):
        encoded = pp._checkpoint_encode_slots({1: 'x', 2: 'y'})
        self.assertEqual(encoded, {'1': 'x', '2': 'y'})
        self.assertEqual(pp._checkpoint_decode_slots(encoded), {1: 'x', 2: 'y'})
        self.assertEqual(pp._checkpoint_decode_slots(None), {})


class TestComposeAnchorResume(unittest.TestCase):
    """compose_anchor_and_packet 命中同一份 dimensions 的断点存档时应直接复用,
    一次 _chat 都不再调用(Phase 1 的 brief/beat ladder/packet/IMAGE 1 全部跳过)。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()
        self.dimensions = {
            'theme': '荒原空心铁镍陨石', 'anchors': [], 'complexity': '中等重工',
            'budget': '轻奢设计师级', 'ratio': 50, 'creativity': '脑洞大开', 'beats_count': 3,
        }
        self.fingerprint = pp.get_brief_fingerprint(self.dimensions)

    def tearDown(self):
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_resume_skips_phase1_entirely(self):
        checkpoint = {
            'theme': self.dimensions['theme'],
            'total_beats': 4,
            'parsed_brief': {'mode': 'Standard', 'theme': self.dimensions['theme']},
            'title': '断点续传测试标题',
            'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None} for i in range(1, 5)],
            'packet': {'camera_dna': 'static shot', 'object_ledger': [], 'lighting_phase_ladder': {}},
            'image_1_prompt': '已通过续传恢复的 IMAGE 1 提示词',
            'compiled_images': {'1': '已通过续传恢复的 IMAGE 1 提示词', '2': '已完成的 IMAGE 2'},
            'compiled_videos': {'1': '已完成的 VIDEO 1'},
        }
        pp.save_compose_checkpoint(self.fingerprint, checkpoint)

        with patch.object(pp, '_chat', side_effect=AssertionError('Phase 1 LLM call should have been skipped on resume')):
            state = pp.compose_anchor_and_packet({}, self.dimensions)

        self.assertEqual(state['title'], '断点续传测试标题')
        self.assertEqual(state['image_1_prompt'], '已通过续传恢复的 IMAGE 1 提示词')
        self.assertEqual(state['total_beats'], 4)
        self.assertEqual(len(state['beat_ladder']), 4)
        self.assertEqual(state['compiled_images'], {1: '已通过续传恢复的 IMAGE 1 提示词', 2: '已完成的 IMAGE 2'})
        self.assertEqual(state['compiled_videos'], {1: '已完成的 VIDEO 1'})
        self.assertEqual(state['brief_fingerprint'], self.fingerprint)

    def test_no_checkpoint_falls_through_to_normal_generation(self):
        # 没有存档时不应该在这个早期检查上抛异常或误跳过——正常往下走到 Phase 1(会真的
        # 调 _chat),这里只验证没有存档不会被误判为"可续传"。
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))


class TestComposeRemainingBeatsResume(unittest.TestCase):
    """compose_remaining_beats 命中断点存档时,只应重新生成 pass_beats_done 里没有记录
    的拍;真正生成成功过的拍不再重复调用 LLM。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()
        self.fingerprint = 'fp-remaining-beats-test'

        # 把整段函数体依赖的重活全部换成便宜的桩实现,只保留"每拍一次 _chat"这条真实路径,
        # 让测试聚焦于续传的 skip 逻辑本身,不被具体校验规则的通过/失败细节干扰。
        patches = [
            patch.object(pp, 'load_reference_file', return_value=''),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'apply_proactive_fixes', side_effect=lambda i, v, im, *a, **k: (v, im)),
            patch.object(pp, 'validate_beat_prompts', return_value=[]),
            patch.object(pp, 'extract_persistent_traces_to_ledger', return_value=[]),
            patch.object(pp, 'validate_and_repair', side_effect=lambda config, dims, block, **k: (block, 'PASS')),
            patch.object(pp, 'parse_audit_failures', return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_state(self, total_beats=4, compiled_images=None, compiled_videos=None):
        return {
            'theme': 'test theme',
            'total_beats': total_beats,
            'parsed_brief': {'mode': 'Standard', 'theme': 'test theme'},
            'title': 'Test Title',
            'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
                            for i in range(1, total_beats + 1)],
            'packet': {'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, total_beats + 2)}, 'object_ledger': []},
            'brief_fingerprint': self.fingerprint,
            'image_1_prompt': 'IMAGE 1 body',
            'compiled_images': compiled_images if compiled_images is not None else {1: 'IMAGE 1 body'},
            'compiled_videos': compiled_videos if compiled_videos is not None else {},
        }

    @staticmethod
    def _beat_number_from_user(user_text):
        # beat_user starts with "Generate prompts for Beat {i}: ..."
        marker = 'Generate prompts for Beat '
        idx = user_text.index(marker) + len(marker)
        return int(user_text[idx:].split(':', 1)[0])

    def _fake_chat_factory(self, calls, crash_on_beat=None):
        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None):
            if user.startswith('Here is the complete generated prompt set:'):
                return '(dummy audit report, no table)'
            beat_i = self._beat_number_from_user(user)
            calls.append(beat_i)
            if crash_on_beat is not None and beat_i == crash_on_beat:
                raise NameError(f"simulated code bug hitting beat {beat_i}")
            return f"===VIDEO===\nVideo prompt for beat {beat_i}\n===IMAGE===\nImage prompt for beat {beat_i + 1}\n===TRACES===\n[]"
        return fake_chat

    def test_fresh_run_generates_every_beat(self):
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)
        self.assertEqual(sorted(calls), [1, 2, 3])
        self.assertIn('Image prompt for beat 4', output)
        # 整单成功交付后存档应该被清空
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))

    def test_crash_mid_run_checkpoints_completed_beats_only(self):
        state = self._make_state(total_beats=4)
        calls = []
        with patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls, crash_on_beat=3)):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        self.assertEqual(sorted(calls), [1, 2, 3])  # beat 4 never attempted; beat 3 crashed
        checkpoint = pp.load_compose_checkpoint(self.fingerprint)
        self.assertIsNotNone(checkpoint, "a crash mid-loop must still leave a checkpoint behind")
        self.assertEqual(sorted(checkpoint['pass_beats_done']), [1, 2])
        self.assertEqual(checkpoint['audit_passes'], 0)

    def test_resume_after_crash_only_regenerates_remaining_beats(self):
        # 复现步骤 1:第一次跑,在 beat 3 崩溃(模拟真实中断/失败),beat 1/2 已经成功。
        state = self._make_state(total_beats=4)
        with patch.object(pp, '_chat', side_effect=self._fake_chat_factory([], crash_on_beat=3)):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        # 复现步骤 2:重试——调用方(call_llm/run_autonomous_pipeline)会用
        # compose_anchor_and_packet 从存档恢复出的、已经包含 beat 1/2 产出的同一份
        # compiled_images/compiled_videos(这里直接复用同一个 state 对象,因为
        # compose_remaining_beats 是原地修改它的,等价于真实续传时恢复出的状态),
        # 再次调用 compose_remaining_beats。
        calls = []
        with patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)

        # 关键断言:beat 1 和 2 已经成功过,续传时绝不能再重新调用 LLM 生成它们
        self.assertEqual(sorted(calls), [3, 4], "resume must only regenerate the beats that never succeeded")
        self.assertIn('Image prompt for beat 4', output)
        self.assertIn('Image prompt for beat 5', output)
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))

    def test_placeholder_fallback_beat_is_retried_not_skipped(self):
        # 一个拍如果 4 次尝试全部验证失败、落到占位符兜底,不能被记为"已完成"——续传时
        # 必须重新真实生成它,否则一次 LLM 抖动会把某一拍永久锁死成占位符文本。
        # 场景:beat 2 每次校验都不通过(兜底为占位符),beat 3 紧接着崩溃中断整个任务；
        # 重试时 beat 1(真正成功)应被跳过,beat 2(占位符兜底)和 beat 3(从未跑到)都
        # 必须重新真实生成。
        beat_2_should_fail = {'value': True}

        def validation_fails_only_for_beat_2(i, *args, **kwargs):
            if i == 2 and beat_2_should_fail['value']:
                return ['forced validation failure for test']
            return []

        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, 'validate_beat_prompts', side_effect=validation_fails_only_for_beat_2), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls, crash_on_beat=3)):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        # beat 2 was attempted (repeatedly, via its 4-attempt retry loop) but never counted
        # as done because every attempt failed validation and fell back to a placeholder.
        self.assertIn(2, calls)
        checkpoint = pp.load_compose_checkpoint(self.fingerprint)
        self.assertEqual(sorted(checkpoint['pass_beats_done']), [1], "the fallback beat must not be marked done")
        self.assertEqual(checkpoint['fallback_count'], 1)
        self.assertIn('static ultra-wide 14mm tripod shot', state['compiled_images'][3], "beat 2 should have shipped its placeholder text for now")

        # Resume: validation now succeeds for everyone, and the beat-3 crash is gone.
        beat_2_should_fail['value'] = False
        calls = []
        with patch.object(pp, 'validate_beat_prompts', side_effect=validation_fails_only_for_beat_2), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)

        self.assertEqual(sorted(calls), [2, 3], "resume must retry the fallback beat, not skip it like a completed one")
        self.assertIn('Image prompt for beat 3', output)  # real beat-2 output replaced the placeholder
        self.assertIn('Image prompt for beat 4', output)
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))


if __name__ == '__main__':
    unittest.main()
