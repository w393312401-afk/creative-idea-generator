"""omni 弹性镜头梯 + 时间线切点的回归测试（2026-08-01）。

背景：此前 omni 的 VIDEO 契约把"六个镜头"写成常量，而 Omni Flash 的 Flow 面板提供
4/6/8/10 秒四档——六镜头塞进 4 秒等于每镜 0.67 秒，模型只能丢镜头或整体加速，两者都
表现为观感上的跳变。同时切点完全没有被声明，模型爱在哪儿切在哪儿切。

这里钉住六组行为：
  1. 时长 → 镜头梯：4/6/8/10 秒对应 3/4/5/6 镜，三镜不可裁（远景/中景/结果远景），
     回补优先级 近景 → 全景 → 特写；
  2. 切点分配：单调、无缝、末点等于时长，主工作镜配额最高；
  3. 时间线句：是本条片子**唯一**允许出现阿拉伯数字的地方，注入位置紧跟锚定开场句，
     模型自编的时间线会被确定性覆写；
  4. 镜头梯审计不能被时间线句自证——切点表本身就按顺序列出了每一级镜头名；
  5. 拍型分流：过门桥拍走过门梯、兑现拍走兑现梯，两者都免除节奏声明；
  6. base 的 even-rate 句（与镜头级进度锁对撞）在 omni 下被清掉，且它的校验错误
     被过滤，不会每拍都记一条假瑕疵。
"""
import re
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
import server_common
from prompt_pipeline import composers
from prompt_pipeline.composers import omni as omni_mod


ANCHOR = ("Use the provided first frame and last frame as exact composition anchors. Use "
          "IMAGE 3 as the actual first-frame image and IMAGE 4 as the actual last-frame "
          "image; every visible action must interpolate between those two frame images "
          "without inventing a third layout.")

BODY_SIX = (
    "The sequence opens with an establishing long shot of the stone shell in its unrepointed "
    "state. A clean cut moves to a full shot as one lone worker enters from the left courtyard "
    "path carrying a pointing trowel. A match cut moves into a medium shot where the worker "
    "presses mortar into the open joints. A close-up isolates the trowel edge as mortar "
    "squeezes out. An extreme close-up lingers on the tooled joint lines and the dust edge. A "
    "final clean cut returns to a wide outro shot where the worker exits through the left "
    "courtyard path and the repointed wall stands empty."
)


def _config(duration=None, model='Omni Flash'):
    cfg = {'videoModel': model}
    if duration is not None:
        cfg['videoDuration'] = duration
    return cfg


def _composer(duration=None, model='Omni Flash'):
    composer = composers.get_composer('omni')
    composer.begin_run(_config(duration, model), {'parsed_brief': {}})
    return composer


class _IsolatedServerConfig(unittest.TestCase):
    """开发机的 server_config.json 里可能配了 videoModel/videoDuration，而
    resolve_video_duration 在请求 config 缺项时会回退到它。测试一律显式声明。"""

    def setUp(self):
        for key in ('videoModel', 'videoDuration'):
            patcher = patch.dict(server_common.SERVER_CONFIG, {}, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)
            server_common.SERVER_CONFIG.pop(key, None)


class TestDurationResolution(_IsolatedServerConfig):
    def test_omni_defaults_to_ten_seconds(self):
        """默认 10 秒 = 排满六镜所需的长度。空值不再是合法态。"""
        self.assertEqual(server_common.resolve_video_duration(_config()), 10)
        self.assertEqual(server_common.resolve_video_duration(_config('')), 10)

    def test_declared_duration_wins(self):
        for declared, expected in (('4', 4), (6, 6), ('8', 8), ('10', 10)):
            self.assertEqual(server_common.resolve_video_duration(_config(declared)), expected)

    def test_illegal_duration_falls_back_instead_of_propagating(self):
        for bogus in ('7', 'abc', '0', None):
            self.assertEqual(server_common.resolve_video_duration(_config(bogus)), 10)

    def test_non_omni_models_are_fixed_at_eight(self):
        """Veo 面板没有时长 tab，残留的 omni 时长值不该泄漏到那边。"""
        self.assertEqual(
            server_common.resolve_video_duration(_config('4', model='Veo 3.1 - Lite')), 8)


class TestElasticLadder(_IsolatedServerConfig):
    def test_shot_count_tracks_duration(self):
        for duration, count in ((4, 3), (6, 4), (8, 5), (10, 6)):
            self.assertEqual(len(omni_mod.ladder_for(duration, 'construction')), count,
                             f'{duration}s')

    def test_three_core_shots_are_never_dropped(self):
        """远景（首帧锚）/ 中景（唯一携带推进量的镜头）/ 结果远景（尾帧锚）。"""
        for duration in (4, 6, 8, 10):
            keys = [r.key for r in omni_mod.ladder_for(duration, 'construction')]
            for core in ('establishing', 'medium', 'outro'):
                self.assertIn(core, keys, f'{duration}s 丢了不可裁的 {core}')

    def test_refill_priority_is_close_then_full_then_xclose(self):
        self.assertEqual([r.key for r in omni_mod.ladder_for(4, 'construction')],
                         ['establishing', 'medium', 'outro'])
        self.assertEqual([r.key for r in omni_mod.ladder_for(6, 'construction')],
                         ['establishing', 'medium', 'close', 'outro'])
        self.assertEqual([r.key for r in omni_mod.ladder_for(8, 'construction')],
                         ['establishing', 'full', 'medium', 'close', 'outro'])
        self.assertEqual([r.key for r in omni_mod.ladder_for(10, 'construction')],
                         ['establishing', 'full', 'medium', 'close', 'xclose', 'outro'])

    def test_dropped_rung_duties_move_into_the_neighbour(self):
        """裁镜头不能顺手把职责一起裁掉：没有独立全景时工人入画并进中景，
        没有独立特写时持久痕迹并进近景。"""
        roles_four = omni_mod.ladder_roles(omni_mod.ladder_for(4, 'construction'))
        self.assertIn('入画', roles_four)
        self.assertIn('持久痕迹', roles_four)
        roles_eight = omni_mod.ladder_roles(omni_mod.ladder_for(8, 'construction'))
        self.assertIn('持久痕迹', roles_eight)


class TestShotMarks(_IsolatedServerConfig):
    def test_marks_are_monotonic_gapless_and_exact(self):
        for duration in (4, 6, 8, 10):
            ladder = omni_mod.ladder_for(duration, 'construction')
            marks = omni_mod.shot_marks(duration, ladder)
            self.assertEqual(marks[0][0], 0.0, f'{duration}s 首点必须是 0')
            self.assertEqual(marks[-1][1], float(duration), f'{duration}s 末点必须等于时长')
            for (start, end, _), (next_start, _, _) in zip(marks, marks[1:]):
                self.assertGreater(end, start, f'{duration}s 出现零长/倒挂镜头')
                self.assertEqual(end, next_start, f'{duration}s 切点有缝隙或重叠')

    def test_every_shot_clears_the_readable_floor(self):
        """平均每镜 ≥1.3 秒是镜头梯表的立论——低于这个数读作闪帧而不是镜头。"""
        for duration in (4, 6, 8, 10):
            ladder = omni_mod.ladder_for(duration, 'construction')
            for start, end, rung in omni_mod.shot_marks(duration, ladder):
                self.assertGreaterEqual(round(end - start, 2), 1.0,
                                        f'{duration}s 的 {rung.key} 只有 {end - start:.1f} 秒')

    def test_the_work_shot_gets_the_largest_slice(self):
        """中景要装下"第一次动作完整可见 + 重复循环"，配额必须最高。"""
        for duration in (4, 6, 8, 10):
            ladder = omni_mod.ladder_for(duration, 'construction')
            spans = {r.key: round(e - s, 2) for s, e, r in omni_mod.shot_marks(duration, ladder)}
            self.assertEqual(max(spans, key=spans.get), 'medium', f'{duration}s: {spans}')
            self.assertGreaterEqual(spans['medium'], 1.5, f'{duration}s: {spans}')


class TestTimelineSentence(_IsolatedServerConfig):
    def test_timeline_names_every_shot_in_order(self):
        ladder = omni_mod.ladder_for(10, 'construction')
        sentence = omni_mod.timeline_sentence(10, ladder)
        self.assertTrue(sentence.startswith('Cut this ten-second clip on these marks'))
        self.assertTrue(sentence.endswith('seconds.'))
        self.assertEqual(omni_mod._missing_shot_rungs(sentence, ladder), [])

    def test_timeline_is_the_only_place_digits_may_appear(self):
        composer = _composer()
        fixed = composer.fix_omni_video(
            3, f"{ANCHOR} {BODY_SIX} The worker sets 3 boards and 12 fasteners.",
            {}, False, beat={'operation': 'repair'}, config=_config())
        self.assertIn('three boards', fixed)
        self.assertIn('twelve fasteners', fixed)
        self.assertEqual(omni_mod._stray_digits(fixed), [], fixed)
        # 时间码本身与 IMAGE 编号照旧保留
        self.assertIn('IMAGE 3', fixed)
        self.assertRegex(fixed, r'from 0\.0 to \d\.\d')

    def test_injection_sits_right_after_the_anchor_sentence(self):
        composer = _composer()
        fixed = composer.fix_omni_video(3, f"{ANCHOR} {BODY_SIX}", {}, False,
                                        beat={'operation': 'repair'}, config=_config())
        head, _, rest = fixed.partition('third layout.')
        self.assertTrue(rest.strip().startswith('Cut this ten-second clip'), rest[:120])

    def test_model_authored_timeline_is_overwritten_not_appended(self):
        """切点表是确定性契约，不是模型的创作空间。"""
        composer = _composer(duration='6')
        bogus = ("Cut this ten-second clip on these marks and hold no other cuts — an "
                 "establishing long shot from 0.0 to 5.0, and a wide outro shot from 5.0 to "
                 "10.0 seconds.")
        fixed = composer.fix_omni_video(3, f"{ANCHOR} {bogus} {BODY_SIX}", {}, False,
                                        beat={'operation': 'repair'}, config=_config('6'))
        self.assertEqual(len(re.findall(r'Cut this', fixed)), 1, fixed)
        self.assertIn('Cut this six-second clip', fixed)
        self.assertNotIn('to 10.0 seconds', fixed)

    def test_reinjection_is_idempotent(self):
        composer = _composer()
        once = composer.fix_omni_video(3, f"{ANCHOR} {BODY_SIX}", {}, False,
                                       beat={'operation': 'repair'}, config=_config())
        twice = composer.fix_omni_video(3, once, {}, False,
                                        beat={'operation': 'repair'}, config=_config())
        self.assertEqual(len(re.findall(r'Cut this', twice)), 1)
        self.assertEqual(len(re.findall(re.escape(omni_mod.OMNI_PACING_PHRASE), twice)), 1)
        self.assertEqual(len(re.findall(re.escape(omni_mod.OMNI_INSHOT_PHRASE), twice)), 1)


class TestTimelineAudit(_IsolatedServerConfig):
    def test_missing_timeline_is_a_structural_error(self):
        composer = _composer()
        ladder = omni_mod.ladder_for(10, 'construction')
        errs = omni_mod.omni_video_violations(f"{ANCHOR} {BODY_SIX}", ladder=ladder, duration=10)
        self.assertTrue(any('缺少时间线句' in e for e in errs), errs)
        structural, _style = composer.split_structural_video_errors(errs)
        self.assertTrue(any('缺少时间线句' in e for e in structural))

    def test_timeline_that_disagrees_with_the_duration_is_rejected(self):
        ladder = omni_mod.ladder_for(6, 'construction')
        text = f"{ANCHOR} {omni_mod.timeline_sentence(10, ladder)} {BODY_SIX}"
        errs = omni_mod.omni_video_violations(text, ladder=ladder, duration=6)
        self.assertTrue(any('时长/镜头梯不符' in e for e in errs), errs)

    def test_the_ladder_audit_cannot_be_satisfied_by_the_timeline_alone(self):
        """回归：切点表本身按顺序列出了每一级镜头名。拿它去过镜头轮换检查等于自证——
        正文一个镜头都没写也能通过。"""
        ladder = omni_mod.ladder_for(10, 'construction')
        naked = (f"{ANCHOR} {omni_mod.timeline_sentence(10, ladder)} The worker repoints the "
                 f"wall while dust settles on the sill.")
        errs = omni_mod.omni_video_violations(naked, ladder=ladder, duration=10)
        self.assertTrue(any('not an edited multi-shot sequence' in e for e in errs), errs)

    def test_stray_digits_only_leave_a_trace_and_never_force_a_rework(self):
        """记号瑕疵回炉一轮也未必修得掉，还要多烧一次调用——留痕即可。"""
        composer = _composer()
        ladder = omni_mod.ladder_for(10, 'construction')
        text = f"{ANCHOR} {omni_mod.timeline_sentence(10, ladder)} {BODY_SIX} It has 3 beams."
        errs = omni_mod.omni_video_violations(text, ladder=ladder, duration=10)
        digit_errs = [e for e in errs if e.startswith(omni_mod.OMNI_VIDEO_STYLE_PREFIX)]
        self.assertEqual(len(digit_errs), 1, errs)
        structural, style = composer.split_structural_video_errors(errs)
        self.assertEqual(structural, [])
        self.assertEqual(style, digit_errs)


class TestBeatKindRouting(_IsolatedServerConfig):
    def test_bridge_and_reward_beats_get_their_own_ladders(self):
        composer = _composer()
        self.assertEqual(
            composer.ladder_for_beat({'operation': 'repair'}, False),
            omni_mod.ladder_for(10, 'construction'))
        self.assertEqual(
            composer.ladder_for_beat({'operation': 'threshold', 'bridge_stage': 1}, True),
            omni_mod._TRAVERSAL_LADDER)
        self.assertEqual(
            composer.ladder_for_beat({'operation': 'reward'}, True),
            omni_mod._REWARD_LADDER)

    def test_bridge_and_reward_are_exempt_from_the_pacing_declaration(self):
        composer = _composer()
        for beat in ({'operation': 'threshold', 'bridge_stage': 1}, {'operation': 'reward'}):
            fixed = composer.fix_omni_video(3, f"{ANCHOR} The camera moves through.", {}, True,
                                            beat=beat, config=_config())
            self.assertNotIn(omni_mod.OMNI_PACING_MARKER, fixed.lower(), beat)
            self.assertIn('Cut this ten-second clip', fixed, beat)

    def test_unknown_beat_kind_injects_nothing(self):
        """回炉通路只拿到一段文本时拍型不明——猜错等于给穿门镜头硬塞一张施工切点表。"""
        composer = _composer()
        out = composer.normalize_omni_video("The camera holds a single continuous take.")
        self.assertEqual(omni_mod._one_take_hits(out), [])
        self.assertNotIn('Cut this', out)
        self.assertNotIn(omni_mod.OMNI_PACING_MARKER, out.lower())


class TestEvenRateConflict(_IsolatedServerConfig):
    def test_base_even_rate_sentence_is_stripped(self):
        """base 那句要求"每一刻都在推进"，与远景/全景零推进、特写不产生推进量、
        结果远景在剪辑点做 same-way 压缩的镜头级进度锁直接对撞。"""
        composer = _composer()
        text = f"{ANCHOR} {BODY_SIX} {pp._EVEN_RATE_PHRASE}"
        fixed = composer.fix_omni_video(3, text, {}, False, beat={'operation': 'repair'},
                                        config=_config())
        self.assertNotIn(pp._EVEN_RATE_MARKER, fixed.lower())
        self.assertIn(omni_mod.OMNI_INSHOT_MARKER, fixed.lower())

    def test_base_even_rate_error_is_filtered_out_of_the_audit(self):
        composer = _composer()
        base_err = ("VIDEO missing the even-rate clause: the clip must state that the "
                    "transformation advances continuously and at an even rate")
        with patch.object(pp, 'validate_beat_prompts', return_value=[base_err]):
            errs = composer.validate_beat_prompts(
                3, f"{ANCHOR} {omni_mod.timeline_sentence(10, omni_mod.ladder_for(10))} "
                   f"{BODY_SIX} {omni_mod.OMNI_PACING_PHRASE}",
                'image', {}, 'Standard', False, False, beat={'operation': 'repair'})
        self.assertNotIn(base_err, errs)


class TestExtraShots(_IsolatedServerConfig):
    """短片长下的头号失败模式：照着满六镜的习惯写，把六个镜头塞进四秒。
    缺镜头一直有人查，多镜头此前完全没人查——而多出来的镜头恰恰把每镜压到一秒以下。"""

    def test_six_shots_in_a_four_second_clip_is_a_structural_error(self):
        composer = _composer(duration='4')
        ladder = omni_mod.ladder_for(4, 'construction')
        text = f"{ANCHOR} {omni_mod.timeline_sentence(4, ladder)} {BODY_SIX}"
        errs = omni_mod.omni_video_violations(text, ladder=ladder, duration=4)
        extra = [e for e in errs if '额外景别' in e]
        self.assertEqual(len(extra), 1, errs)
        for dropped in ('全景 full shot', '近景 close-up', '特写 extreme close-up'):
            self.assertIn(dropped, extra[0])
        structural, _style = composer.split_structural_video_errors(errs)
        self.assertEqual(structural, extra)

    def test_the_matching_ladder_raises_nothing(self):
        ladder = omni_mod.ladder_for(10, 'construction')
        text = f"{ANCHOR} {omni_mod.timeline_sentence(10, ladder)} {BODY_SIX}"
        self.assertEqual(omni_mod.omni_video_violations(text, ladder=ladder, duration=10), [])

    def test_construction_scales_are_extra_inside_a_crossing(self):
        """一段穿门镜头写不出"工具接触点"和"重复作业循环"。"""
        text = (f"{ANCHOR} {omni_mod.timeline_sentence(10, omni_mod._TRAVERSAL_LADDER)} A wide "
                f"approach shot holds outside. A threshold shot crosses the sill. A medium shot "
                f"shows the worker. An interior wide shot settles inside.")
        errs = omni_mod.omni_video_violations(
            text, ladder=omni_mod._TRAVERSAL_LADDER, duration=10)
        self.assertTrue(any('额外景别' in e and '中景' in e for e in errs), errs)


class TestWordBudget(_IsolatedServerConfig):
    def test_budget_scales_with_shot_count(self):
        previous = (0, 0)
        for count in (3, 4, 5, 6):
            target, ceiling = omni_mod.video_word_targets(count)
            self.assertGreater(target, previous[0])
            self.assertGreater(ceiling, target)
            previous = (target, ceiling)

    def test_draft_budget_never_trims_a_compliant_draft(self):
        """回归：预压缩预算一度低于目标字数，于是一份**完全合规**的初稿照样被裁，
        而 _local_trim_to_budget 丢的是中间整句 —— 恰好是唯一携带推进量的那几镜。"""
        for count in (3, 4, 5, 6):
            target, ceiling = omni_mod.video_word_targets(count)
            draft = omni_mod.video_draft_budget(count)
            self.assertGreaterEqual(
                draft, target - omni_mod._STRUCTURAL_INJECTION_WORDS,
                f'{count} 镜：预算 {draft} 装不下合规初稿')
            self.assertGreaterEqual(
                ceiling, draft + omni_mod._STRUCTURAL_INJECTION_WORDS,
                f'{count} 镜：硬顶 {ceiling} 装不下预算 {draft} + 结构句')

    def test_injections_do_not_push_the_prompt_over_its_ceiling(self):
        """旧账：此前的 460 是硬顶，却被当成预压缩预算用，压完再追加进出句/音效句/
        节奏句，结果必然超顶且不再复裁。"""
        composer = _composer(duration='4')
        ladder = omni_mod.ladder_for(4, 'construction')
        _target, ceiling = omni_mod.video_word_targets(len(ladder))
        bloated = f"{ANCHOR} " + (BODY_SIX + ' ') * 4
        fixed = composer.fix_omni_video(3, bloated, {}, False, beat={'operation': 'repair'},
                                        config=_config('4'))
        self.assertLessEqual(len(fixed.split()), ceiling, len(fixed.split()))

    def test_short_clips_really_produce_short_ladders_end_to_end(self):
        composer = _composer(duration='4')
        fixed = composer.fix_omni_video(3, f"{ANCHOR} {BODY_SIX}", {}, False,
                                        beat={'operation': 'repair'}, config=_config('4'))
        self.assertIn('Cut this four-second clip', fixed)
        marks = re.findall(r'from (\d\.\d) to (\d\.\d)', fixed)
        self.assertEqual(len(marks), 3, marks)
        self.assertEqual(marks[-1][1], '4.0')


if __name__ == '__main__':
    unittest.main()
