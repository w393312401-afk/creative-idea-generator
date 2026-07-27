"""断点续传回归测试:提示词合成任务(compose_anchor_and_packet / compose_remaining_beats)
中断或失败后,重试必须从最后一个成功的拍(beat)续传,而不是把 Phase 1(brief/工序/
Drift Lock 包/IMAGE 1)和已经生成过的拍全部重新跑一遍。

覆盖三层:
1. 存档原语本身(load/save/clear_compose_checkpoint)的读写往返。
2. compose_anchor_and_packet 命中存档时整段跳过 Phase 1(不再调用 _chat)。
3. compose_remaining_beats 命中存档时只重新生成尚未成功的拍;中途崩溃(NameError 等
   代码级错误,现实中最容易触发"合成失败"的场景)后存档要精确停在最后一拍。
"""
import json
import os
import re
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


class TestSignatureAnchorFlowsIntoParsedBrief(unittest.TestCase):
    """根因回归(2026-07-20)：dimensions.anchors(如一键合成灵感卡片的 idea.twist_zh)
    此前只被当"参考文字"塞进 Step 1 brief-parsing 的 user message，该调用要求 LLM 输出
    的 JSON schema 里根本没有承载它的字段——往后整条合成管线（beat ladder / 逐拍撰写 /
    确定性复核）都读不到它，导致招牌反差点从未出现在渲染出的提示词里。

    修复：parsed_brief['signature_anchor'] 由 Python 侧直接赋值（不经过 LLM 转录），
    这里用一个「Step 1 LLM 返回的 JSON 完全不含任何锚点相关字段」的桩响应证明它仍然
    正确落到 parsed_brief 里，并且会被写进 Step 3 beat ladder 生成的 system prompt。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)
        for p in (patch.object(pp, 'load_reference_file', return_value=''),
                  patch.object(pp, 'get_cropped_templates', return_value='')):
            p.start()
            self.addCleanup(p.stop)

        self.dimensions = {
            'theme': '高山废弃铁路蒸汽机车注水塔改造成独居御寒暖阁',
            'anchors': ['利用蒸汽机车原装铸铁注水阀门旧物Upcycling改装成悬空柴暖壁炉'],
            'complexity': '硬核重工', 'budget': '轻奢设计师级', 'ratio': '50%',
            'creativity': '脑洞大开', 'beats_count': 2, 'beat_count_mode': 'fixed',
        }
        self.captured_beat_system = {}
        self.captured_beat_user = {}

        # Step 1 brief-parsing 桩响应：故意不含任何锚点相关字段，证明后面能看到锚点
        # 靠的是 Python 侧直接赋值，不是指望这次 LLM 调用把它转录出来。
        brief_json = json.dumps({
            'carrier': 'abandoned steam-locomotive water tower', 'env': 'alpine mountainside',
            'trauma': 'rusted and frost-cracked', 'destiny': 'snug winter refuge den',
            'destiny_zh': '独居御寒暖阁', 'reward': 'firelight fills the finished room',
            'mode': 'Standard', 'space_type': 'abandoned property',
            'carrier_slug': 'steam-locomotive-water-tower',
        })
        beat_ladder_json = json.dumps([
            {'index': 1, 'operation': 'clearing', 'description': 'clearing debris', 'bridge_stage': None, 'stage_scope': 'large',
             'milestone_name': 'approach fully cleared', 'before_state': 'debris covers the approach',
             'after_state': 'the entire approach is cleared to stable ground',
             'completion_extent': 'the entire visible approach', 'changed_grid_cells': ['Grid B2', 'Grid C2'],
             'package_operations': ['clearing'], 'primary_progress': 'clear ground expands across the full approach',
             'secondary_progress': 'two debris crates fill from empty to full',
             'persistent_traces': ['rake grooves', 'crate drag marks'],
             'preserve_state': 'the tower shell remains rusted and untouched'},
            {'index': 2, 'operation': 'framing', 'description': 'framing interior walls', 'bridge_stage': None, 'stage_scope': 'large',
             'milestone_name': 'all interior framing complete', 'before_state': 'the interior shell has no studs',
             'after_state': 'all declared wall and ceiling studs are installed',
             'completion_extent': 'all interior walls and the ceiling curve',
             'changed_grid_cells': ['Grid A2', 'Grid B2', 'Grid C2'],
             'package_operations': ['framing'], 'primary_progress': 'stud count grows from zero to twelve',
             'secondary_progress': 'the staged timber bundle drains from twelve to zero',
             'persistent_traces': ['screw heads', 'sawdust bands'],
             'preserve_state': 'the cleared floor and original shell remain unchanged'},
            {'index': 3, 'operation': 'reward', 'bridge_stage': None,
             'description': 'The cast-iron valve stove hangs suspended above the hearth as warm light fills the finished room.',
             'anchor_keywords': ['cast-iron valve stove']},
        ])
        packet_json = json.dumps({
            'camera_dna': 'static tripod shot, ultra-wide lens, horizon line remains level at 50-percent height',
            'geometry_lock': 'fixed boundaries', 'primary_landmarks': [
                {'name': 'tower doorway', 'grid': 'Grid B2', 'z_depth_scale': '40%'},
            ],
            'frame_boundaries': {'left': 'B1', 'right': 'B3', 'top': 'A2', 'bottom': 'C2'},
            'object_ledger': [], 'worker_choreography': 'one lone worker',
            'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, 5)},
            'passive_environment': 'still alpine air', 'interest_budget': {},
        })

        def fake_chat(config, system, user, **kwargs):
            if 'scene analysis agent' in system:
                return brief_json
            if 'construction planner' in system:
                self.captured_beat_system['text'] = system
                self.captured_beat_user['text'] = user
                return beat_ladder_json
            if 'spatial consistency supervisor' in system:
                return packet_json
            if 'generate the very first IMAGE prompt' in system:
                return 'A static wide shot of the derelict water tower, rusted and untouched.'
            raise AssertionError(f'Unexpected _chat call, system prompt head: {system[:80]!r}')

        self._chat_patch = patch.object(pp, '_chat', side_effect=fake_chat)
        self._chat_patch.start()
        self.addCleanup(self._chat_patch.stop)

    def test_signature_anchor_survives_into_parsed_brief_and_beat_ladder_prompt(self):
        state = pp.compose_anchor_and_packet({}, self.dimensions)

        self.assertEqual(
            state['parsed_brief']['signature_anchor'],
            '利用蒸汽机车原装铸铁注水阀门旧物Upcycling改装成悬空柴暖壁炉',
        )
        # Fix 2: the beat-ladder generation call must have been told about the anchor
        # and required to realize it on the reward beat.
        self.assertIn('利用蒸汽机车原装铸铁注水阀门旧物Upcycling改装成悬空柴暖壁炉',
                       self.captured_beat_system['text'])
        self.assertIn('SIGNATURE ANCHOR RULE', self.captured_beat_system['text'])
        # Fix 3 groundwork: the reward beat's anchor_keywords normalized through cleanly.
        reward_beat = state['beat_ladder'][-1]
        self.assertEqual(reward_beat['operation'], 'reward')
        self.assertEqual(reward_beat['anchor_keywords'], ['cast-iron valve stove'])

    def test_no_declared_anchor_leaves_signature_anchor_empty(self):
        self.dimensions['anchors'] = []
        state = pp.compose_anchor_and_packet({}, self.dimensions)
        self.assertEqual(state['parsed_brief']['signature_anchor'], '')
        self.assertNotIn('SIGNATURE ANCHOR RULE', self.captured_beat_system['text'])

    def test_card_beat_outline_reaches_the_ladder_call_as_a_soft_plan(self):
        """灵感卡片上展示的工序预览(idea.beat_outline)随 dimensions 传进来后,必须出现在
        节拍阶梯生成调用里,否则用户挑卡时看到的工序和成片对不上;同时必须明确标成软参考,
        不能让它盖过真实施工顺序等硬规则。"""
        self.dimensions['beat_outline'] = ['清运塔内积渣与锈屑', '架设内墙木龙骨', '点亮壁炉,人物入住']
        pp.compose_anchor_and_packet({}, self.dimensions)

        beat_user = self.captured_beat_user['text']
        self.assertIn('清运塔内积渣与锈屑', beat_user)
        self.assertIn('架设内墙木龙骨', beat_user)
        self.assertIn('SOFT reference', beat_user)
        self.assertIn('every mandatory rule in the system prompt outranks it', beat_user)
        # 编号顺序透传,便于模型对齐用户看到的那一版工序
        self.assertIn('1. 清运塔内积渣与锈屑', beat_user)
        self.assertIn('2. 架设内墙木龙骨', beat_user)

    def test_outline_is_capped_at_the_beat_budget_and_absent_when_not_provided(self):
        """草案条数不该反过来把拍数预算顶穿:beats_count=2(fixed)时最多 3 条(含 reward)。
        没传 beat_outline 时整块不出现,手工填维度直出的老路径行为不变。"""
        pp.compose_anchor_and_packet({}, self.dimensions)
        self.assertNotIn('Draft plan', self.captured_beat_user['text'])

        self.dimensions['beat_outline'] = ['A清渣', 'B龙骨', 'C封板', 'D刷漆', 'E入住']
        pp.compose_anchor_and_packet({}, self.dimensions)
        beat_user = self.captured_beat_user['text']
        self.assertIn('Draft plan', beat_user)
        self.assertIn('3. C封板', beat_user)
        self.assertNotIn('D刷漆', beat_user)   # 超出 beats_count + 1 的草案条目被截掉
        self.assertNotIn('E入住', beat_user)


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
            patch.object(pp, 'check_milestone_video_prompt', return_value=[]),
            patch.object(pp, 'check_milestone_image_prompt', return_value=[]),
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
        # Individual-retry beat_user starts with "Generate prompts for Beat {i}: ..."
        marker = 'Generate prompts for Beat '
        idx = user_text.index(marker) + len(marker)
        return int(user_text[idx:].split(':', 1)[0])

    @staticmethod
    def _beat_numbers_from_batch_user(user_text):
        # Batched user message has one "==================== BEAT {i} ====================" per beat.
        return [int(n) for n in re.findall(r'====================\s*BEAT\s+(\d+)\s*====================', user_text)]

    def _fake_chat_factory(self, calls):
        """Fake `_chat` that answers BOTH request shapes compose_remaining_beats can now
        make: the batched multi-beat call (first call, listing every pending beat in
        this pass) and the individual-retry fallback call (one beat, only made when the
        batch didn't produce a valid result for that beat). `calls` collects every beat
        number actually generated (from either path) in the order _chat was asked for
        them, same contract the pre-batching tests already relied on. Always returns
        well-formed content — tests that need a mid-run failure inject it via
        validate_beat_prompts (see _validation_crashes_on), since batching means there's
        no longer a separate `_chat` "turn" per beat to fail in isolation."""
        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None):
            batch_beats = self._beat_numbers_from_batch_user(user)
            if batch_beats:
                calls.extend(batch_beats)
                return "\n".join(
                    f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n===BEAT {b} TRACES===\n[]"
                    for b in batch_beats
                )
            beat_i = self._beat_number_from_user(user)
            calls.append(beat_i)
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

    @staticmethod
    def _validation_crashes_on(beat_num, flag):
        """validate_beat_prompts side_effect: raises a code-level error for `beat_num`
        while `flag['value']` is True (simulates a real bug hit while processing that
        beat's batched result), passes everyone else. Beats are now generated together
        in one batched _chat call, so a crash can no longer be injected by making _chat
        itself raise only for one beat's turn (there IS no separate turn) — the
        equivalent, realistic failure point is a code bug in the per-beat processing
        that runs after the batch response comes back, which is exactly what the merged
        parse+commit loop's own NameError-class handling exists to catch."""
        def side_effect(i, *args, **kwargs):
            if i == beat_num and flag['value']:
                raise NameError(f"simulated code bug hitting beat {beat_num}")
            return []
        return side_effect

    def test_crash_mid_run_checkpoints_completed_beats_only(self):
        crash_flag = {'value': True}
        state = self._make_state(total_beats=4)
        calls = []
        with patch.object(pp, 'validate_beat_prompts', side_effect=self._validation_crashes_on(3, crash_flag)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        # The one batched _chat call already asked for all of beats 1-4 at once (that's
        # the whole point of batching) — the crash happens afterward, in per-beat
        # validation of the batch's own result, when processing reaches beat 3.
        self.assertEqual(sorted(set(calls)), [1, 2, 3, 4])
        checkpoint = pp.load_compose_checkpoint(self.fingerprint)
        self.assertIsNotNone(checkpoint, "a crash mid-loop must still leave a checkpoint behind")
        self.assertEqual(sorted(checkpoint['pass_beats_done']), [1, 2],
                          "beats 1/2 were committed before the crash on beat 3; the merged "
                          "parse+commit loop must not lose them")

    def test_resume_after_crash_only_regenerates_remaining_beats(self):
        # 复现步骤 1:第一次跑,处理 beat 3 时崩溃(模拟真实代码 bug),beat 1/2 已经成功。
        crash_flag = {'value': True}
        state = self._make_state(total_beats=4)
        with patch.object(pp, 'validate_beat_prompts', side_effect=self._validation_crashes_on(3, crash_flag)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory([])):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        # 复现步骤 2:重试——调用方(call_llm/run_autonomous_pipeline)会用
        # compose_anchor_and_packet 从存档恢复出的、已经包含 beat 1/2 产出的同一份
        # compiled_images/compiled_videos(这里直接复用同一个 state 对象,因为
        # compose_remaining_beats 是原地修改它的,等价于真实续传时恢复出的状态),
        # 再次调用 compose_remaining_beats——这次 bug 已修复(crash_flag 关闭)。
        crash_flag['value'] = False
        calls = []
        with patch.object(pp, 'validate_beat_prompts', side_effect=self._validation_crashes_on(3, crash_flag)), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)

        # 关键断言:beat 1 和 2 已经成功过,续传时绝不能再重新调用 LLM 生成它们——只应
        # 有一次批量调用,覆盖 [3, 4]。
        self.assertEqual(sorted(set(calls)), [3, 4], "resume must only regenerate the beats that never succeeded")
        self.assertIn('Image prompt for beat 4', output)
        self.assertIn('Image prompt for beat 5', output)
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))

    def test_direct_mode_accepts_batch_result_despite_validation_errors(self):
        """skill 直出模式核心契约:批量直出的结果即使校验有**风格**瑕疵也直接采纳
        (只记录日志),不触发单拍重写——每拍恰好只被生成一次。
        (2026-07-15 起例外:结构性硬伤会触发单拍定向回炉,见 split_structural_video_errors
        与 test_structural_beat_rework;本测试的瑕疵字符串刻意不命中结构性标记。)"""
        state = self._make_state(total_beats=3)
        calls = []
        with patch.object(pp, 'validate_beat_prompts', return_value=['soft defect: whatever']), \
             patch.object(pp, '_chat', side_effect=self._fake_chat_factory(calls)):
            output = pp.compose_remaining_beats({}, state)

        self.assertEqual(calls, [1, 2, 3], "one batched call, no per-beat rewrite calls")
        self.assertIn('Image prompt for beat 4', output)
        # 全部真实采纳,无占位符,整单交付后存档清空
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))

    def test_placeholder_fallback_beat_is_retried_not_skipped(self):
        # skill 直出模式下,占位符兜底只剩一种触发方式:批量响应缺了某拍的段落,且单拍
        # 兜底生成的每次尝试也都没拿到 VIDEO/IMAGE 两段(传输/截断类故障)。这种拍不能被
        # 记为"已完成"——续传时必须重新真实生成它,否则一次 LLM 抖动会把某一拍永久锁死
        # 成占位符文本。
        # 场景:beat 2 的段落始终缺失(兜底为占位符),beat 3 紧接着(处理批量结果时)
        # 崩溃中断整个任务；重试时 beat 1(真正成功)应被跳过,beat 2(占位符兜底)和
        # beat 3(从未解析成功)都必须重新真实生成。
        beat_2_sections_missing = {'value': True}
        beat_3_should_crash = {'value': True}

        def validation_side_effect(i, *args, **kwargs):
            if i == 3 and beat_3_should_crash['value']:
                raise NameError("simulated code bug hitting beat 3")
            return []

        calls = []

        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None):
            batch_beats = self._beat_numbers_from_batch_user(user)
            if batch_beats:
                calls.extend(batch_beats)
                return "\n".join(
                    f"===BEAT {b} VIDEO===\nVideo prompt for beat {b}\n===BEAT {b} IMAGE===\nImage prompt for beat {b + 1}\n===BEAT {b} TRACES===\n[]"
                    for b in batch_beats
                    if not (b == 2 and beat_2_sections_missing['value'])
                )
            beat_i = self._beat_number_from_user(user)
            calls.append(beat_i)
            if beat_i == 2 and beat_2_sections_missing['value']:
                return "malformed response with no section markers"
            return f"===VIDEO===\nVideo prompt for beat {beat_i}\n===IMAGE===\nImage prompt for beat {beat_i + 1}\n===TRACES===\n[]"

        state = self._make_state(total_beats=3)
        with patch.object(pp, 'validate_beat_prompts', side_effect=validation_side_effect), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            with self.assertRaises(RuntimeError):
                pp.compose_remaining_beats({}, state)

        # beat 2 was attempted (via the individual direct-generation fallback, since the
        # batch response was missing its sections) but never counted as done because no
        # attempt ever produced both sections, so it fell back to a placeholder.
        self.assertIn(2, calls)
        checkpoint = pp.load_compose_checkpoint(self.fingerprint)
        self.assertEqual(sorted(checkpoint['pass_beats_done']), [1], "the fallback beat must not be marked done")
        self.assertEqual(checkpoint['fallback_count'], 1)
        self.assertIn('static ultra-wide 14mm tripod shot', state['compiled_images'][3], "beat 2 should have shipped its placeholder text for now")

        # Resume: beat 2's sections now come back, and the beat-3 crash is gone.
        beat_2_sections_missing['value'] = False
        beat_3_should_crash['value'] = False
        calls.clear()
        with patch.object(pp, 'validate_beat_prompts', side_effect=validation_side_effect), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            output = pp.compose_remaining_beats({}, state)

        self.assertEqual(sorted(set(calls)), [2, 3], "resume must retry the fallback beat, not skip it like a completed one")
        self.assertIn('Image prompt for beat 3', output)  # real beat-2 output replaced the placeholder
        self.assertIn('Image prompt for beat 4', output)
        self.assertIsNone(pp.load_compose_checkpoint(self.fingerprint))


if __name__ == '__main__':
    unittest.main()
