"""Gemini Omni 提示词合成链路（prompt_pipeline.composers）的回归测试（2026-08-01）。

背景：技能包按 profile 拆开之后，omni 包（gemini-omni-restoration-composer）的契约
与 base 包最大的分歧在 VIDEO——Omni 的视频提示词是一段剪辑过的六镜头序列
（远景/全景/中景/近景/特写/结果远景），默认 UGC 手机拍摄质感，且**没有例外地**禁止
一镜到底措辞。IMAGE 段、Phase 1、槽位格式则是全 profile 共享的下游契约。

这里钉住五组行为：
  1. 分派：get_composer 按 profile 给出实现，未知 profile 回落 base；
  2. 同一份 dimensions 在两个 profile 下 VIDEO 文本不同，且 IMAGE 段逐字一致；
  3. omni 的 VIDEO 带六镜头轮换、不含一镜到底措辞（含兜底稿与确定性归一）；
  4. 审计：六镜头缺失/一镜到底措辞算结构性硬伤，触发定向回炉；
  5. 断点续传指纹带 profile —— base 下合成到一半切 omni 必须重排而不是续传
     （否则会交付半 base 半 omni 的混合提示词集）。
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import prompt_pipeline as pp
from prompt_pipeline import composers
from prompt_pipeline.composers import omni as omni_mod


BASE_CONFIG = {'videoModel': 'Veo 3.1'}
# videoDuration 必须一起钉死：不给它，clip_duration() 会回落到开发机 server_config.json
# 里的那一档（用户在设置页随时改得动），镜头梯就跟着从四镜变三镜，而下面按默认四镜梯
# 断言的用例会当场变红。test_omni_timeline.py 早就为同一件事留过一条注释。
OMNI_CONFIG = {'videoModel': 'Omni Flash', 'videoDuration': 10}

# 两个 profile 共用的 IMAGE 稿——IMAGE 段不随 profile 变化，测试要能证明这一点。
IMAGE_DRAFT = (
    "A static ultra-wide 14mm tripod shot at 1.6m height; horizon line remains level. The "
    "stone shell stands with its repointed courses complete across the full south wall, fresh "
    "mortar joints visible between every block. Scrape grooves and dust edges remain embedded "
    "in the sills. The frame contains only static surfaces, materials, and causal traces."
)

# base 契约下模型该交出的东西：一条连续的施工延时。
BASE_VIDEO_DRAFT = (
    "One lone worker in a solid pale shirt, dark pants, and dark cap is repointing the south "
    "wall with a pointing trowel, scooping mortar from a rigid bucket and pressing it into "
    "each joint along the full course. Scrape grooves and mortar crumbs accumulate on the "
    "sill below."
)

# omni 契约下模型该交出的东西：一条主工作镜，被两个特写插入切开，再切回同一机位收尾。
OMNI_VIDEO_DRAFT = (
    "The clip opens on a wide working shot captured like casual smartphone footage, slightly "
    "off-center with mild wide-angle edge distortion and phone auto-exposure settling on the "
    "stone shell in its unrepointed state, with one lone worker in a solid pale shirt, dark "
    "pants, and dark cap already at the south wall pressing mortar into the open joints with a "
    "pointing trowel at zero seconds, scooping from a rigid mortar bucket and working joint "
    "after joint as the repointed run grows steadily and fine dust settles nearby. A clean cut "
    "at the three-second mark drops into a close-up insert on the trowel edge with minor "
    "handheld motion blur and small blown highlights, capturing mortar squeezing out under "
    "pressure. A second insert at the five-second mark pushes to an extreme close-up insert on "
    "the tooled joint lines and the dust edge engraved into the porous stone, with low-light "
    "noise and mild compression proving the physical causality. A final clean cut at the "
    "seven-second mark returns to a returning wide shot from the same camera setup as the "
    "opening wide working shot, matching the phone-recorded exposure of the last frame, where "
    "— after the remaining joints are filled the same way — the worker keeps pointing through "
    "the last instant and the repointed wall carries the scrape grooves still visible."
)


def _body(slot):
    """_parse_prompt_slots 的槽位是 {'body','meta'}；测试只关心正文。"""
    return slot['body'] if isinstance(slot, dict) else slot


def _make_state(fingerprint, total_beats=3):
    return {
        'theme': '石屋改造成隐藏工作室',
        'total_beats': total_beats,
        'parsed_brief': {'mode': 'Standard', 'theme': '石屋改造成隐藏工作室'},
        'title': 'Omni Composer Test',
        'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}',
                         'bridge_stage': None}
                        for i in range(1, total_beats + 1)],
        'packet': {'camera_dna': 'static ultra-wide 14mm tripod shot at 1.6m height',
                   'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, total_beats + 2)},
                   'object_ledger': []},
        'brief_fingerprint': fingerprint,
        'image_1_prompt': 'IMAGE 1 body',
        'compiled_images': {1: 'IMAGE 1 body'},
        'compiled_videos': {},
    }


def _fake_chat_by_profile(calls=None):
    """按 system prompt 里有没有 OMNI VIDEO OVERRIDE 段决定回什么样的 VIDEO 稿——
    模拟一个"照契约办事"的模型。这同时验证了 omni 的指令确实送到了模型手上：
    指令没送到，这个桩就永远只会回 base 稿。"""
    def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240,
                  on_chunk=None, model=None, enable_search=False):
        is_omni = 'OMNI VIDEO OVERRIDE' in system
        if calls is not None:
            calls.append('omni' if is_omni else 'base')
        video = OMNI_VIDEO_DRAFT if is_omni else BASE_VIDEO_DRAFT
        beats = [int(n) for n in re.findall(
            r'====================\s*BEAT\s+(\d+)\s*====================', user)]
        if beats:
            return "\n".join(
                f"===BEAT {b} VIDEO===\n{video}\n===BEAT {b} IMAGE===\n{IMAGE_DRAFT}\n"
                f"===BEAT {b} TRACES===\n[]"
                for b in beats)
        marker = 'Generate prompts for Beat '
        idx = user.index(marker) + len(marker)
        int(user[idx:].split(':', 1)[0])
        return f"===VIDEO===\n{video}\n===IMAGE===\n{IMAGE_DRAFT}\n===TRACES===\n[]"
    return fake_chat


class _ComposeHarness(unittest.TestCase):
    """把 Phase 2 里与本测试无关的重活换成便宜的桩，只留 composer 自己的通路。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)
        patches = [
            patch.object(pp, 'COMPOSE_CHECKPOINT_PATH',
                         os.path.join(self._tmp_dir, 'compose_checkpoints.json')),
            patch.object(pp, 'load_reference_file', return_value=''),
            patch.object(pp, 'get_cropped_templates', return_value=''),
            patch.object(pp, 'validate_beat_prompts', return_value=[]),
            patch.object(pp, 'check_milestone_video_prompt', return_value=[]),
            patch.object(pp, 'check_milestone_image_prompt', return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _compose(self, config, fingerprint, total_beats=3, calls=None):
        state = _make_state(fingerprint, total_beats=total_beats)
        with patch.object(pp, '_chat', side_effect=_fake_chat_by_profile(calls)):
            return pp.compose_remaining_beats(dict(config), state)


class TestComposerDispatch(unittest.TestCase):
    def test_profile_picks_the_implementation(self):
        self.assertIsInstance(composers.get_composer('base'), composers.BaseComposer)
        self.assertIsInstance(composers.get_composer('omni'), composers.OmniComposer)

    def test_unknown_profile_falls_back_to_base(self):
        """profile 有一部分来自前端配置，拼错一个字母不该让整条合成链路 500。"""
        for bogus in ('', None, 'nope', 'OMNI-flash'):
            composer = composers.get_composer(bogus)
            self.assertIs(type(composer), composers.BaseComposer, bogus)

    def test_video_model_mapping_is_not_duplicated_here(self):
        """视频模型名 → profile 的判定只能有一处真相源（server_common）。composers
        里再出现一份 'omni' in videoModel 的判断，两处就会各自漂移。"""
        import prompt_pipeline.composers.base as base_mod
        for path in (composers.__file__, base_mod.__file__, omni_mod.__file__):
            src = open(path, encoding='utf-8').read()
            for forbidden in ("get('videoModel'", 'get("videoModel"',
                              'profile_for_video_model('):
                self.assertNotIn(forbidden, src, f'{path} 不该自己解析视频模型名')

    def test_compose_remaining_beats_dispatches_on_the_active_profile(self):
        seen = {}

        class _Spy(composers.BaseComposer):
            def compose_remaining_beats(self, config, state, on_progress=None):
                seen['profile'] = self.profile
                return 'spy-output'

        with patch.dict(composers.COMPOSERS, {'omni': _Spy}, clear=False):
            out = pp.compose_remaining_beats(dict(OMNI_CONFIG), _make_state('fp'), None)
        self.assertEqual(out, 'spy-output')
        self.assertEqual(seen['profile'], 'base')  # _Spy 继承 base 的 profile 属性


class TestOmniVideoDiffersFromBase(_ComposeHarness):
    def test_same_dimensions_produce_different_video_text(self):
        base_out = self._compose(BASE_CONFIG, 'fp-base-diff')
        omni_out = self._compose(OMNI_CONFIG, 'fp-omni-diff')

        base_images, base_videos = pp._parse_prompt_slots(base_out)
        omni_images, omni_videos = pp._parse_prompt_slots(omni_out)

        self.assertEqual(sorted(base_videos), sorted(omni_videos), '槽位编号不该随 profile 变')
        for seq in base_videos:
            self.assertNotEqual(_body(base_videos[seq]), _body(omni_videos[seq]),
                                f'VIDEO {seq} 在两个 profile 下必须不同')

    def test_image_slots_are_identical_across_profiles(self):
        """IMAGE 段是下游帧渲染/创意库共享的契约，omni 一律委托 base，不许有分叉。"""
        base_images, _ = pp._parse_prompt_slots(self._compose(BASE_CONFIG, 'fp-base-img'))
        omni_images, _ = pp._parse_prompt_slots(self._compose(OMNI_CONFIG, 'fp-omni-img'))
        self.assertEqual(base_images, omni_images)

    def test_slot_format_survives_the_omni_path(self):
        """约束 B：多镜头只是 VIDEO 槽里的文本变长，不是新结构——_parse_prompt_slots /
        _format_prompt_block 必须照旧解析得动。"""
        out = self._compose(OMNI_CONFIG, 'fp-omni-slots', total_beats=3)
        images, videos = pp._parse_prompt_slots(out)
        self.assertEqual(sorted(images), [1, 2, 3, 4])
        self.assertEqual(sorted(videos), [1, 2, 3])
        missing_images, missing_videos = pp._missing_prompt_slots(images, videos, (1, 4), (1, 3))
        self.assertEqual((missing_images, missing_videos), ([], []))

    def test_omni_video_carries_the_six_shot_rotation(self):
        out = self._compose(OMNI_CONFIG, 'fp-omni-six')
        _, videos = pp._parse_prompt_slots(out)
        for seq, slot in videos.items():
            text = _body(slot)
            self.assertEqual(omni_mod._missing_shot_rungs(text), [],
                             f'VIDEO {seq} 六镜头轮换不完整')
            self.assertEqual(omni_mod._one_take_hits(text), [],
                             f'VIDEO {seq} 出现一镜到底措辞')
            self.assertIn(omni_mod.OMNI_PACING_MARKER, text.lower(),
                          f'VIDEO {seq} 缺少 omni 的节奏声明')

    def test_base_video_is_untouched_by_the_omni_contract(self):
        """base 必须零变化：既不该长出六镜头，也不该丢掉自己的节奏声明。"""
        out = self._compose(BASE_CONFIG, 'fp-base-untouched')
        _, videos = pp._parse_prompt_slots(out)
        for slot in videos.values():
            text = _body(slot)
            self.assertNotEqual(omni_mod._missing_shot_rungs(text), [])
            self.assertIn('continuous construction time-lapse', text.lower())
            self.assertNotIn(omni_mod.OMNI_PACING_MARKER, text.lower())


class TestOmniReferenceLoading(_ComposeHarness):
    def test_required_references_are_read_through_the_profile(self):
        """SKILL.md §Required Reference Loading 声明的 7 个必读契约，必须经
        load_reference_file(name, 'omni') 取——不硬编码路径，omni 包缺的文件才能回落。"""
        with patch.object(pp, 'load_reference_file', return_value='') as loader:
            self._compose(OMNI_CONFIG, 'fp-omni-refs')
        asked = {(args[0], args[1]) for args, _ in
                 ((c.args, c.kwargs) for c in loader.call_args_list) if len(args) > 1}
        for name in omni_mod.OMNI_ALWAYS_LOAD_REFERENCES:
            self.assertIn((name, 'omni'), asked, f'{name} 没有按 omni profile 读取')

    def test_threshold_reference_is_conditional(self):
        """过门契约是按需加载的（SKILL.md 的 Load conditionally），普通拍不该读它。"""
        with patch.object(pp, 'load_reference_file', return_value='') as loader:
            self._compose(OMNI_CONFIG, 'fp-omni-nothreshold')
        asked = [args[0] for args, _ in
                 ((c.args, c.kwargs) for c in loader.call_args_list) if args]
        self.assertNotIn(omni_mod.OMNI_THRESHOLD_REFERENCE, asked)


class TestVendoredOmniPackage(unittest.TestCase):
    def test_required_references_really_exist_in_the_repo(self):
        """上一组用桩证明了"按 omni 读"，这条证明那些文件真的在仓库内置的包里——
        两边都过才等于链路通（这里不打桩，走真实的 load_reference_file）。"""
        composer = composers.get_composer('omni')
        for name in omni_mod.OMNI_ALWAYS_LOAD_REFERENCES:
            self.assertTrue(composer.reference(name).strip(), f'{name} 读空')


class TestOmniAudit(_ComposeHarness):
    """镜头结构缺失/一镜到底措辞 = 结构性硬伤 → 定向回炉一轮，回炉不成也要留痕。"""

    NON_COMPLIANT = (
        "The camera holds a single continuous take as one lone worker in a solid pale shirt "
        "repoints the south wall with a pointing trowel from the first moment to the last."
    )

    def test_violations_are_classified_as_structural(self):
        composer = composers.get_composer('omni')
        errs = omni_mod.omni_video_violations(self.NON_COMPLIANT)
        self.assertEqual(len(errs), 2, errs)
        structural, style = composer.split_structural_video_errors(errs)
        self.assertEqual(sorted(structural), sorted(errs))
        self.assertEqual(style, [])

    def test_compliant_video_raises_no_violation(self):
        self.assertEqual(omni_mod.omni_video_violations(OMNI_VIDEO_DRAFT), [])

    def test_base_only_pacing_error_is_dropped(self):
        """base 的"缺 continuous construction time-lapse"在 omni 下不成立——omni 有
        自己的节奏声明。不过滤掉，每一拍都会被记一条假瑕疵。"""
        composer = composers.get_composer('omni')
        base_err = ("VIDEO missing pacing control 'continuous construction time-lapse, "
                    "not real-time footage'")
        with patch.object(pp, 'validate_beat_prompts', return_value=[base_err, '别的瑕疵']):
            errs = composer.validate_beat_prompts(
                1, OMNI_VIDEO_DRAFT, IMAGE_DRAFT, {}, 'Standard', False, False)
        self.assertNotIn(base_err, errs)
        self.assertIn('别的瑕疵', errs)

    def test_non_compliant_draft_triggers_a_rework_round(self):
        calls = []

        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240,
                      on_chunk=None, model=None, enable_search=False):
            if 'multi-shot contract' in system:
                calls.append('rework')
                return OMNI_VIDEO_DRAFT
            calls.append('generate')
            beats = [int(n) for n in re.findall(
                r'====================\s*BEAT\s+(\d+)\s*====================', user)]
            return "\n".join(
                f"===BEAT {b} VIDEO===\n{self.NON_COMPLIANT}\n===BEAT {b} IMAGE===\n"
                f"{IMAGE_DRAFT}\n===BEAT {b} TRACES===\n[]" for b in beats)

        config = dict(OMNI_CONFIG)
        state = _make_state('fp-omni-rework', total_beats=2)
        with patch.object(pp, '_chat', side_effect=fake_chat):
            out = pp.compose_remaining_beats(config, state)

        self.assertEqual(calls.count('rework'), 2, '每个违规拍都该回炉一轮')
        _, videos = pp._parse_prompt_slots(out)
        for seq, slot in videos.items():
            text = _body(slot)
            self.assertEqual(omni_mod._missing_shot_rungs(text), [], f'VIDEO {seq} 回炉后仍不合规')
            self.assertEqual(omni_mod._one_take_hits(text), [])
        audit = config.get('_beat_audit') or []
        self.assertTrue(audit, '回炉必须留痕')
        self.assertTrue(any(any(omni_mod.OMNI_VIDEO_ERROR_PREFIX in e for e in row['structural'])
                            for row in audit))

    def test_failed_rework_keeps_the_draft_and_still_records_it(self):
        """回炉稿复验不过就保留原稿——保底不比不回炉更差，但审核面板上必须看得见。"""
        config = dict(OMNI_CONFIG)
        state = _make_state('fp-omni-rework-fail', total_beats=1)

        def fake_chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240,
                      on_chunk=None, model=None, enable_search=False):
            if 'multi-shot contract' in system:
                return "still a single continuous take with no shot ladder at all."
            beats = [int(n) for n in re.findall(
                r'====================\s*BEAT\s+(\d+)\s*====================', user)]
            return "\n".join(
                f"===BEAT {b} VIDEO===\n{self.NON_COMPLIANT}\n===BEAT {b} IMAGE===\n"
                f"{IMAGE_DRAFT}\n===BEAT {b} TRACES===\n[]" for b in beats)

        with patch.object(pp, '_chat', side_effect=fake_chat):
            pp.compose_remaining_beats(config, state)

        audit = config.get('_beat_audit') or []
        self.assertTrue(any(row['reworked'] is False for row in audit))


class TestOmniDeterministicNormalisation(unittest.TestCase):
    """确定性归一：不指望模型每次都守规矩，能用规则清掉的措辞就地清掉。"""

    def test_one_take_wording_is_stripped(self):
        for banned in ("shot as one continuous take", "a oner from start to finish",
                       "written as a single take", "one unbroken take at a steady speed",
                       "filmed one-shot", "in one continuous shot"):
            text = f"The worker repoints the wall. It is {banned}. Dust settles on the sill."
            cleaned = omni_mod.strip_one_take_language(text)
            self.assertEqual(omni_mod._one_take_hits(cleaned), [], f'{banned} 没清干净: {cleaned}')
            self.assertIn('The worker repoints the wall.', cleaned, '正文不该被误删')

    def test_base_fallback_sentence_is_removed_whole(self):
        """base 兜底稿那一句同时宣告一镜到底又禁止剪辑点，改词改不干净，只能整句删。"""
        text = ("The camera pushes forward through the open threshold. One unbroken take at a "
                "steady speed: no cut, no fade, no dissolve, no speed ramp.")
        cleaned = omni_mod.strip_one_take_language(text)
        self.assertEqual(omni_mod._one_take_hits(cleaned), [])
        self.assertNotIn('no cut', cleaned)
        self.assertIn('The camera pushes forward', cleaned)

    def test_ordinary_prose_is_not_mangled(self):
        text = ("A wide working shot holds while the worker presses mortar into one joint "
                "after another, and the returning wide shot lands on the finished course.")
        self.assertEqual(omni_mod.strip_one_take_language(text), text)

    def test_shot_rungs_must_appear_in_order(self):
        shuffled = ("a returning wide shot, then an extreme close-up insert, a close-up "
                    "insert, and finally a wide working shot")
        self.assertTrue(omni_mod._missing_shot_rungs(shuffled),
                        '乱序的四个词不是组接，不能算通过')

    def test_hyphen_and_spacing_variants_count_as_the_same_rung(self):
        for variant in ('close-up', 'close up', 'closeup'):
            text = (f"a wide working shot, a {variant} insert, an extreme {variant} insert, "
                    f"a returning wide shot")
            self.assertEqual(omni_mod._missing_shot_rungs(text), [], variant)

    def test_fallback_placeholder_gets_the_shot_ladder_clause(self):
        """占位符兜底稿也不许违反镜头语法（它照旧计入 fallback_count 门禁）。

        这里用的是 base 的**过门桥**兜底文案，所以补的必须是过门梯（逼近/门槛/落定），
        不是施工梯——一段穿门镜头写不出"工具接触点"和"重复作业循环"。"""
        composer = composers.get_composer('omni')
        composer.begin_run(dict(OMNI_CONFIG), _make_state('fp'))
        base_fallback = (
            "Use the provided first frame and last frame as exact composition anchors. "
            "The camera pushes forward in one continuous coaxial move through the open "
            "threshold, and settles fully inside by the last frame. One unbroken take at a "
            "steady speed: no cut, no fade, no dissolve, no speed ramp.")
        out = composer.finalize_fallback_video(
            base_fallback, {'is_threshold_or_reveal': True, 'is_bridge': True, 'beat': None})
        self.assertEqual(omni_mod._one_take_hits(out), [])
        body = omni_mod._body_without_timeline(out)
        self.assertEqual(omni_mod._missing_shot_rungs(body, omni_mod._TRAVERSAL_LADDER), [])
        # 过门拍免除节奏声明——补梯不该顺手把它塞进来
        self.assertNotIn(omni_mod.OMNI_PACING_MARKER, out.lower())

    def test_ugc_capture_is_the_default_and_cinematic_is_opt_in(self):
        composer = composers.get_composer('omni')
        composer.begin_run({}, _make_state('fp'))
        self.assertFalse(composer.wants_cinematic())
        self.assertIn('casual UGC phone footage', composer.capture_style_rule())

        state = _make_state('fp')
        state['parsed_brief']['theme'] = '院线感的石屋改造'
        composer.begin_run({}, state)
        self.assertTrue(composer.wants_cinematic())
        rule = composer.capture_style_rule()
        self.assertIn('cinematic', rule)
        # 院线感也不解除一镜到底禁令。
        self.assertIn('one-take ban', rule.lower())


class TestOmniSystemPrompt(unittest.TestCase):
    def test_override_block_is_appended_not_replacing_the_image_rules(self):
        """omni 不复制一份 IMAGE 契约——base 那份必须原样还在，override 只追加在后面。"""
        packet = {'camera_dna': 'static shot', 'object_ledger': []}
        # 片长要钉住：镜头梯（进而下面那几个镜头名）随它变，不钉就会跟着开发机的
        # server_config.json 漂——六秒档只有三镜，extreme close-up 那一行根本不会出现。
        omni_composer = composers.get_composer('omni')
        omni_composer.begin_run(dict(OMNI_CONFIG), {})
        with patch.object(pp, 'load_reference_file', return_value=''):
            base_prompt = composers.get_composer('base').batch_system_prompt({}, packet, '', '')
            omni_prompt = omni_composer.batch_system_prompt(dict(OMNI_CONFIG), packet, '', '')
        self.assertTrue(omni_prompt.startswith(base_prompt))
        self.assertIn('OMNI VIDEO OVERRIDE', omni_prompt)
        for rung in ('establishing long shot', 'full shot', 'medium shot', 'close-up',
                     'extreme close-up', 'wide outro shot'):
            self.assertIn(rung, omni_prompt)
        self.assertIn(omni_mod.OMNI_PACING_PHRASE, omni_prompt)
        # 槽位标记不能被改掉（约束 B：下游全靠这个格式）。
        self.assertIn('===BEAT N VIDEO===', omni_prompt)
        self.assertIn('===BEAT N IMAGE===', omni_prompt)


class TestFingerprintCarriesTheProfile(unittest.TestCase):
    """约束 A：断点续传指纹必须含 profile。一单在 base 下合成到一半、切成 Omni Flash
    再点合成，命中旧断点就会续出半 base 半 omni 的混合提示词集。"""

    DIMENSIONS = {
        'theme': '荒原空心铁镍陨石', 'anchors': [], 'complexity': '中等重工',
        'budget': '轻奢设计师级', 'ratio': 50, 'creativity': '脑洞大开', 'beats_count': 3,
    }

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)
        p = patch.object(pp, 'COMPOSE_CHECKPOINT_PATH',
                         os.path.join(self._tmp_dir, 'compose_checkpoints.json'))
        p.start()
        self.addCleanup(p.stop)

    def test_fingerprint_differs_by_profile(self):
        self.assertNotEqual(pp.get_brief_fingerprint(self.DIMENSIONS, 'base'),
                            pp.get_brief_fingerprint(self.DIMENSIONS, 'omni'))

    def test_packet_cache_key_inherits_the_split(self):
        """packet_cache_key 从 brief_fingerprint 派生，因此自动跟着分家——同一份
        dimensions 换 profile 不能复用上一次的 Drift Lock 包。"""
        src = open(pp.__file__, encoding='utf-8').read()
        self.assertIn('packet_cache_key = f"{brief_fingerprint}:', src)

    def _save_base_checkpoint(self):
        checkpoint = {
            'theme': self.DIMENSIONS['theme'],
            'total_beats': 4,
            'parsed_brief': {'mode': 'Standard', 'theme': self.DIMENSIONS['theme']},
            'title': 'base 下合成到一半的存档',
            'beat_ladder': [{'index': i, 'operation': 'repair', 'description': f'step {i}',
                             'bridge_stage': None} for i in range(1, 5)],
            'packet': {'camera_dna': 'static shot', 'object_ledger': [],
                       'lighting_phase_ladder': {}},
            'image_1_prompt': 'base 语法的 IMAGE 1',
            'compiled_images': {'1': 'base 语法的 IMAGE 1', '2': 'base 语法的 IMAGE 2'},
            'compiled_videos': {'1': 'base 语法的一镜到底 VIDEO 1'},
            'pass_beats_done': [1],
        }
        fingerprint = pp.get_brief_fingerprint(self.DIMENSIONS, 'base')
        pp.save_compose_checkpoint(fingerprint, checkpoint)
        return fingerprint

    def test_same_profile_still_resumes(self):
        """先证明续传本身没被这次改动打坏：同一个 profile 照旧命中存档、跳过 Phase 1。"""
        self._save_base_checkpoint()
        with patch.object(pp, '_chat', side_effect=AssertionError('续传时不该再调 Phase 1')):
            state = pp.compose_anchor_and_packet(dict(BASE_CONFIG), self.DIMENSIONS)
        self.assertEqual(state['image_1_prompt'], 'base 语法的 IMAGE 1')
        self.assertEqual(state['compiled_videos'], {1: 'base 语法的一镜到底 VIDEO 1'})

    def test_switching_to_omni_replans_instead_of_resuming(self):
        base_fingerprint = self._save_base_checkpoint()

        # 命中存档就一次 _chat 都不会有（见 test_same_profile_still_resumes）；
        # 这里 _chat 必须真的被调到，才说明 omni 走的是重排而不是续传。规划层现在有
        # 确定性降级路径，所以代理持续报错不再是本测试应期待的终态异常。
        # DIMENSIONS 没有卡片 beat_outline，生产模式下"规划全败+无大纲"现在是硬失败
        # （见 compile_outline_fallback_ladder 的 allow_generic 门禁）——这个测试关心
        # 的是指纹/续传行为而不是这道新门禁，所以显式开诊断口子，保留旧的降级期望。
        omni_config = {**OMNI_CONFIG, 'diagnosticMode': True, 'allowPlaceholderPrompts': True}
        chat = MagicMock(side_effect=RuntimeError('Phase 1 从头跑了'))
        with patch.object(pp, '_chat', chat):
            state = pp.compose_anchor_and_packet(omni_config, self.DIMENSIONS)
        self.assertTrue(chat.called, 'omni 必须重新跑 Phase 1，而不是命中 base 的存档')
        self.assertNotEqual(state['image_1_prompt'], 'base 语法的 IMAGE 1')

        # 旧存档还在（base 那一单没被动过），只是 omni 这次根本不看它。
        self.assertIsNotNone(pp.load_compose_checkpoint(base_fingerprint))
        self.assertIsNone(pp.load_compose_checkpoint(
            pp.get_brief_fingerprint(self.DIMENSIONS, 'omni')))


if __name__ == '__main__':
    unittest.main()
