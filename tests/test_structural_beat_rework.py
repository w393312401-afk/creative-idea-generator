"""直出模式结构性硬伤单拍回炉（2026-07-15 盐湖贝壳单事故 P0 修复）：

- split_structural_video_errors：把 validate_beat_prompts 结果分成
  （结构性硬伤=VIDEO 无动作正文/桥接无运镜/幽灵施工, 风格瑕疵）两组。
- rework_structural_video_beat：只重写 VIDEO 的加法式回炉，最多一轮；
  重写稿必须保留插值锚定开场且通过 check_video_process_content 复验，
  否则保留原稿——保底不比不回炉更差。

2026-07-16 追加（实跑两条真实创意确诊的两个新根因，见
spark-2026-07-16-video-instability-live-diagnosis 记忆）：

- _strip_leading_label_line / 宽松锚点定位：LLM 经常把 user message 里的
  "Beat N ... prompt:" 标签复读在正文最前面，严格 startswith 检查会把内容
  完全合格的重写稿当废稿丢弃（实测 3/3 复现，回炉命中率被拖到 ~12%）。
- rework_similar_image_beat / split_image_similarity_errors：VIDEO 结构性
  硬伤有回炉，IMAGE 侧连续多拍近乎逐字复读（similarity 到 1.00）之前完全
  没有对应机制——只记不修，现补上同款保守回炉。
"""
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


ANCHOR = ("Use the provided first frame and last frame as exact composition anchors. "
          "Use IMAGE 6 as the actual first-frame image and IMAGE 7 as the actual last-frame image; "
          "every visible action must interpolate between those two frame images without inventing a third layout.")

# 2026-07-15 实案原文形态：锚定开场 + 音效行，无任何动作正文
HOLLOW_VIDEO = (ANCHOR + " Near-field sound carries wrench clicks and timber knocks over steady "
                "enclosed shell resonance. continuous construction time-lapse, not real-time footage.")

GOOD_REWRITE = (ANCHOR + " One lone worker in a solid pale shirt and dark cap is fastening "
                "tongue-and-groove panels wall by wall, tapping each board home with a rubber "
                "mallet, coverage sweeping steadily across every curved surface until the far "
                "wall is fully clad and the worker exits the frame. Near-field sound carries "
                "wrench clicks and timber knocks over steady enclosed shell resonance. "
                "continuous construction time-lapse, not real-time footage.")


def _milestone_beat(**overrides):
    beat = {
        'index': 1,
        'operation': 'framing',
        'description': 'install the complete radial roof frame',
        'bridge_stage': None,
        'stage_scope': 'large',
        'milestone_name': 'eight-rafter roof skeleton complete',
        'before_state': 'the stone wall has no roof framing',
        'after_state': 'all eight timber rafters meet at the central roof hub',
        'completion_extent': 'all eight rafters across the full roof circle',
        'changed_grid_cells': ['Grid A2', 'Grid B2'],
        # 一个普通施工拍申报 2~3 道紧密工序（_MIN/_MAX_PACKAGE_OPERATIONS）。
        # framing + insulation 正是 schema 第 13 条点名的参考组合。
        'package_operations': ['framing', 'insulation'],
        'primary_progress': 'the radial skeleton grows from zero to all eight rafters',
        'secondary_progress': 'the leaned timber bundle drains from eight pieces to none',
        'persistent_traces': ['sunk nail heads', 'pale sawdust bands'],
        'preserve_state': 'the five-course stone wall and doorway remain unchanged',
        'introduced_objects': [],
        'removed_objects': [],
    }
    beat.update(overrides)
    return beat


class TestVisibleMilestonePlanningGate(unittest.TestCase):
    def test_adaptive_accepts_shorter_ladder_but_fixed_does_not(self):
        self.assertTrue(pp._beat_count_is_valid(6, 16, 'adaptive'))
        self.assertFalse(pp._beat_count_is_valid(6, 16, 'fixed'))
        self.assertTrue(pp._beat_count_is_valid(16, 16, 'fixed'))

    def test_complete_countable_milestone_passes(self):
        self.assertEqual(pp.milestone_ladder_violations([_milestone_beat()]), [])

    def test_local_barely_visible_progress_is_rejected(self):
        beat = _milestone_beat(
            milestone_name='one small section begins to receive rafters',
            after_state='one corner has a local patch of framing',
            completion_extent='one small section',
            changed_grid_cells=['Grid A2'],
        )
        errors = pp.milestone_ladder_violations([beat])
        self.assertTrue(any('weak/local' in error for error in errors))

    def test_single_cell_terminal_component_milestone_passes(self):
        beat = _milestone_beat(
            operation='framing',
            milestone_name='bulkhead doorway framing complete',
            after_state='the steel bulkhead doorway is framed and sealed',
            completion_extent='doorway framing finished from sill to lintel',
            changed_grid_cells=['B2'],
            package_operations=['framing', 'insulation'],
        )
        self.assertEqual(pp.milestone_ladder_violations([beat]), [])

    def test_coherent_closeout_package_is_allowed(self):
        beat = _milestone_beat(
            operation='repair',
            milestone_name='weather-tight exterior shell complete',
            after_state='all six roof panels, the plank door, and the threshold path are complete',
            completion_extent='all six roof panels plus the full doorway and threshold approach',
            changed_grid_cells=['Grid A2', 'Grid B2', 'Grid C2'],
            package_operations=['roofing', 'door-install', 'threshold-closeout'],
        )
        self.assertEqual(pp.milestone_ladder_violations([beat]), [])

    def test_cross_phase_package_is_rejected(self):
        beat = _milestone_beat(package_operations=['demolition', 'painting'])
        errors = pp.milestone_ladder_violations([beat])
        self.assertTrue(any('incompatible construction phases' in error for error in errors))

    def test_missing_object_lifecycle_keys_are_a_hard_violation(self):
        """空数组是合法答案（这一拍没有新增/拆除任何可数物体），但**完全不声明**这个键
        不是——那等于场景状态表压根没有数据可校验（见 scene_state.py）。"""
        beat = _milestone_beat()
        del beat['introduced_objects']
        del beat['removed_objects']
        errors = pp.milestone_ladder_violations([beat])
        self.assertTrue(any('introduced_objects' in e and 'removed_objects' in e for e in errors))
        self.assertEqual(pp.hard_milestone_violations(errors), errors)

    def test_declared_empty_object_lifecycle_lists_are_not_a_violation(self):
        beat = _milestone_beat(introduced_objects=[], removed_objects=[])
        self.assertEqual(pp.milestone_ladder_violations([beat]), [])

    # ── 材料层跨阶段判据只看「这一拍干了什么」（2026-08-01 run 1785597123956 修复）
    # before_state / description 按 schema 必然点名被覆盖的那一层，扫进去等于把
    # 「起点层 -> 终点层」误判成「一拍跨两层」，覆盖拍全灭。
    def test_covering_beat_may_name_the_layer_it_conceals(self):
        """封板/饰面/家具覆盖拍在场景状态里点名既有下层不算跨阶段。"""
        for label, overrides in (
            ('board closure over rough-in', dict(
                operation='drywall',
                description='crews screw plasterboard over the exposed wiring and vapour barrier',
                milestone_name='full wall plasterboard closure complete',
                before_state='the vapour barrier and wiring runs sit exposed between the open studs',
                after_state='plasterboard fully closes the entire wall',
                package_operations=['drywall'])),
            ('finish over framing', dict(
                operation='painting',
                description='rollers lay two coats of finish paint across the boarded walls',
                milestone_name='all four walls painted',
                before_state='bare battens and furring strips are still visible at the ceiling line',
                after_state='painting is complete across all four walls',
                package_operations=['painting'])),
            ('furnishing onto a finished floor', dict(
                operation='furnishing',
                description='the galley cabinetry is carried onto the finished flooring and bolted down',
                milestone_name='all six cabinetry units installed',
                before_state='the tiling and flooring are complete and the room stands empty',
                after_state='all six cabinetry units stand on the already finished flooring',
                package_operations=['furnishing'])),
        ):
            with self.subTest(label):
                errors = pp.milestone_ladder_violations([_milestone_beat(**overrides)])
                self.assertEqual(
                    [e for e in errors if 'material-layer' in e], [],
                    f'{label} 是合格的单层覆盖拍，不该被跨阶段判据打回：{errors}')

    def test_genuine_multi_layer_bundle_still_rejected(self):
        """真·一拍打包隐蔽层+封板+饰面仍要拦下（package 明确申报，扫得到）。"""
        beat = _milestone_beat(
            operation='drywall',
            description='crews staple the vapour barrier, panel over it and roll on finish paint',
            milestone_name='wall membrane, panelling and painting complete',
            before_state='the bare shell stands open',
            after_state='the vapour barrier is stapled, the panelling closed and the paint rolled on',
            package_operations=['rough-in', 'drywall', 'painting'])
        errors = pp.milestone_ladder_violations([beat])
        self.assertTrue(any('material-layer' in error for error in errors), errors)

    # ── milestone_name 同理（2026-08-02）：它按 schema 第 10 条是「这一拍**终结在
    # 什么产物上**」，不是「干了什么」。产物名天然要带上它所依附/覆盖的基层，
    # 扫进去与扫 before_state 是同一个误判。实测这是压垮第 4 次重排的头号原因。
    def test_milestone_name_may_name_the_substrate_it_sits_on(self):
        for label, overrides in (
            ('finish floor over the framed cavity', dict(
                operation='flooring',
                milestone_name='plank flooring laid over the insulated joists',
                package_operations=['flooring'])),
            ('board closure over the services', dict(
                operation='drywall',
                milestone_name='wall lining screwed over the wiring runs',
                package_operations=['drywall'])),
            ('furniture anchored to the finished floor', dict(
                operation='furnishing',
                milestone_name='built-in bunk anchored to the finished flooring',
                package_operations=['furnishing'])),
            # nested_space_payoff 的 summary 自己点名要求的那一拍：'fixture' 在
            # 清运拍里是**被拆掉的对象**，不是软装相位。
            ('strip-out of seats and fixtures', dict(
                operation='clearing',
                milestone_name='seat and fixture strip-out complete',
                package_operations=['clearing'])),
        ):
            with self.subTest(label):
                errors = pp.milestone_ladder_violations([_milestone_beat(**overrides)])
                self.assertEqual(
                    [e for e in errors if 'material-layer' in e], [],
                    f'{label} 是合格的单层拍，不该被跨阶段判据打回：{errors}')


class TestMilestoneViolationSeverity(unittest.TestCase):
    """硬（合成侧依赖）/ 软（质量评判）分级。最后一次重排只有硬违规才让整单失败。"""

    def test_missing_fields_are_hard(self):
        beat = _milestone_beat()
        beat.pop('completion_extent')
        errors = pp.milestone_ladder_violations([beat])
        self.assertTrue(pp.hard_milestone_violations(errors), errors)

    def test_quality_only_violations_are_soft(self):
        for label, overrides in (
            ('cross-phase package', dict(package_operations=['demolition', 'painting'])),
            ('too many grid cells', dict(
                changed_grid_cells=['Grid A1', 'Grid B1', 'Grid C1', 'Grid D1'])),
            ('weak wording', dict(
                milestone_name='one small section begins to receive rafters',
                completion_extent='one small section')),
        ):
            with self.subTest(label):
                errors = pp.milestone_ladder_violations([_milestone_beat(**overrides)])
                self.assertTrue(errors, f'{label} 本身仍要报出来')
                self.assertEqual(
                    pp.hard_milestone_violations(errors), [],
                    f'{label} 只是质量问题，不该让整单硬失败：{errors}')

    def test_clean_ladder_has_neither(self):
        self.assertEqual(pp.milestone_ladder_violations([_milestone_beat()]), [])
        self.assertEqual(pp.hard_milestone_violations([]), [])


class TestDeterministicBeatLadderFallback(unittest.TestCase):
    def test_standard_fallback_is_schema_complete(self):
        ladder = pp.deterministic_fallback_beat_ladder(
            {'mode': 'Standard'}, 8, 'coaxial', {'turn_degrees': 0})
        self.assertEqual([beat['index'] for beat in ladder], list(range(1, 9)))
        self.assertEqual(ladder[-1]['operation'], 'reward')
        self.assertEqual(pp.hard_milestone_violations(
            pp.milestone_ladder_violations(ladder)), [])

    def test_hard_cut_fallback_keeps_one_crossing_and_cleanout(self):
        ladder = pp.deterministic_fallback_beat_ladder(
            {'mode': 'Threshold'}, 9, 'hard_cut',
            {'turn_degrees': 90, 'turn_direction': 'left'})
        cuts = [i for i, beat in enumerate(ladder) if beat.get('hard_cut')]
        self.assertEqual(cuts, [2])
        self.assertEqual(ladder[3]['operation'], 'clearing')
        self.assertFalse(any(beat.get('bridge_stage') for beat in ladder))

    @patch('prompt_pipeline.space_reset_cut_required', return_value=True)
    def test_nested_fallback_keeps_bridge_reset_and_second_cleanout(self, _reset):
        ladder = pp.deterministic_fallback_beat_ladder(
            {'mode': 'Threshold'}, 14, 'coaxial',
            {'turn_degrees': 0, 'turn_direction': 'none'})
        bridge = [i for i, beat in enumerate(ladder) if beat.get('bridge_stage') == 1]
        reset = [i for i, beat in enumerate(ladder) if beat.get('hard_cut')]
        self.assertEqual(bridge, [2])
        self.assertEqual(len(reset), 1)
        self.assertEqual(ladder[bridge[0] + 1]['operation'], 'clearing')
        self.assertEqual(ladder[reset[0] + 1]['operation'], 'clearing')


class TestTemplateCroppingBeatBinding(unittest.TestCase):
    TEMPLATES = """
## IMAGE 2+
GENERIC IMAGE
## Interior IMAGE
INTERIOR IMAGE
## Threshold Bridge
BRIDGE VIDEO
## Ordinary Construction VIDEO
ORDINARY VIDEO
## Final IMAGE
FINAL IMAGE
## Final Reward VIDEO N
FINAL VIDEO
## IMAGE Checklist
IMAGE CHECKS
## VIDEO Checklist
VIDEO CHECKS
"""

    def test_threshold_beat_is_passed_into_crossing_detection(self):
        cropped = pp.get_cropped_templates(
            self.TEMPLATES, 3, 8, 'Threshold', 1,
            family='interior', beat={'operation': 'threshold', 'bridge_stage': 1})
        self.assertIn('BRIDGE VIDEO', cropped)
        self.assertIn('INTERIOR IMAGE', cropped)

    def test_non_crossing_threshold_mode_uses_ordinary_video(self):
        cropped = pp.get_cropped_templates(
            self.TEMPLATES, 4, 8, 'Threshold', None,
            family='interior', beat={'operation': 'clearing'})
        self.assertIn('ORDINARY VIDEO', cropped)
        self.assertNotIn('BRIDGE VIDEO', cropped)


class TestMilestonePromptSkeleton(unittest.TestCase):
    def test_image_requires_after_state_extent_and_two_traces(self):
        beat = _milestone_beat()
        good = (
            'The scene is the eight-rafter roof skeleton complete anchor. All eight timber rafters '
            'meet at the central roof hub across the full roof circle. The five-course stone wall '
            'and doorway remain unchanged. Sunk nail heads and pale sawdust bands remain visible.'
        )
        self.assertEqual(pp.check_milestone_image_prompt(good, beat), [])
        bad = 'One small section begins to show a timber change while everything else remains.'
        self.assertTrue(pp.check_milestone_image_prompt(bad, beat))

    def test_trace_requirement_never_exceeds_what_the_beat_declared(self):
        """门槛按本拍**实际声明了几条**痕迹取，不能无条件写死 2。

        2026-08-06：只声明 1 条（或 0 条）痕迹的拍，无论重写多少轮都凑不出 2 个命中，
        这道硬门就成了死门——实测一单卡在 Beat 9 上连"整拍重试"都用同一句报错原地
        打转，最后整单 BEAT_GENERATION_FAILED。
        """
        one_trace = _milestone_beat(persistent_traces=['sunk nail heads'])
        good = (
            'The scene is the eight-rafter roof skeleton complete anchor. All eight timber rafters '
            'meet at the central roof hub across the full roof circle. The five-course stone wall '
            'and doorway remain unchanged. Sunk nail heads remain visible.'
        )
        self.assertEqual(pp.check_milestone_image_prompt(good, one_trace), [])

        no_traces = _milestone_beat(persistent_traces=[])
        self.assertEqual(
            [e for e in pp.check_milestone_image_prompt(good, no_traces)
             if 'persistent contact traces' in e], [])

    def test_missing_trace_error_names_the_traces_that_are_absent(self):
        """回炉的模型必须知道**哪几条**痕迹没写进去；只说"至少两条"它无从下手。"""
        beat = _milestone_beat()
        partial = (
            'The scene is the eight-rafter roof skeleton complete anchor. All eight timber rafters '
            'meet at the central roof hub across the full roof circle. The five-course stone wall '
            'and doorway remain unchanged. Sunk nail heads remain visible.'
        )
        errors = [e for e in pp.check_milestone_image_prompt(partial, beat)
                  if 'persistent contact traces' in e]
        self.assertTrue(errors)
        self.assertIn('pale sawdust bands', errors[0])
        self.assertNotIn('sunk nail heads', errors[0])

    def test_video_requires_both_progress_lines_and_material_path(self):
        beat = _milestone_beat()
        good = (
            'At the very first moment the stone wall has no roof framing and the worker makes the '
            'first hammer contact, then repeatedly lifts rafters from a leaned timber bundle and '
            'carries them to the roof hub. The radial skeleton grows from zero to all eight rafters '
            'while the leaned timber bundle drains from eight pieces to none. At the end all eight '
            'timber rafters meet at the central roof hub.'
        )
        self.assertEqual(pp.check_milestone_video_prompt(good, beat), [])
        bad = 'A worker installs some wood and exits.'
        self.assertTrue(pp.check_milestone_video_prompt(bad, beat))


class TestSplitStructuralVideoErrors(unittest.TestCase):
    def test_structural_and_style_errors_are_separated(self):
        errs = [
            "VIDEO describes no visible action/process beyond the anchor opening and audio lines — write it out",
            "VIDEO phrasing/structure is too similar to previous beat (cleaned similarity: 0.77 > 0.65).",
            "Bridge VIDEO contains no camera-translation description — must be written out",
            "IMAGE prompt word count (190) exceeds limit of 170 words",
        ]
        structural, rest = pp.split_structural_video_errors(errs)
        self.assertEqual(len(structural), 2)
        self.assertTrue(any('no visible action' in e for e in structural))
        self.assertTrue(any('camera-translation' in e for e in structural))
        self.assertEqual(len(rest), 2)

    def test_empty_and_none_input(self):
        self.assertEqual(pp.split_structural_video_errors([]), ([], []))
        self.assertEqual(pp.split_structural_video_errors(None), ([], []))


class TestGhostWorkAgentVocabulary(unittest.TestCase):
    """幽灵施工判据的词表必须认得合成器自己指定的施工主体称呼（2026-08-22）。

    合成器的着装规则（THEME-ADAPTIVE ATTIRE RULE）**指定**模型写 "one lone craftsman ..."，
    packet 的 worker_choreography 也照此存盘；可 _WORKER_AGENT_WORDS 里从来没有
    craftsman（\bman\b 匹配不到 crafts|man 的词内位置）。后果不是漏判而是纯误判：
    工人明明写了却被判成幽灵施工 → 每拍烧掉最多 2 次定向回炉 → 回炉稿复用同一句
    choreography 又再判一次不过 → 原稿原样保留。实测三条真实复刻单 51 拍里 19 拍中招，
    是回炉 71% 失败率的单一主因，也是提示词合成整体偏慢的大头之一。
    """

    def _clip(self, agent_phrase):
        return (ANCHOR + f" {agent_phrase} is fastening tongue-and-groove panels row by row, "
                "tapping each board home with a rubber mallet, coverage sweeping steadily "
                "across the wall until the far end is fully clad. Near-field sound carries "
                "mallet knocks over steady shell resonance. "
                "continuous construction time-lapse, not real-time footage.")

    def test_composer_mandated_craftsman_wording_is_a_visible_agent(self):
        for phrase in ('One lone craftsman in a solid olive-drab work t-shirt',
                       'One lone artisan in a dark cap',
                       'A single installer in dark cargo pants',
                       'One lone technician in a pale shirt',
                       'One lone tradesman in work boots'):
            with self.subTest(phrase=phrase):
                self.assertEqual(pp.check_video_process_content(self._clip(phrase)), [])

    def test_the_original_worker_wording_still_passes(self):
        self.assertEqual(
            pp.check_video_process_content(self._clip('One lone worker in a pale shirt')), [])

    def test_真的没有施工主体时仍然判幽灵施工(self):
        # 扩词只让检测器多认出一个主体，绝不能让它对真正无主体的正文放行
        ghost = (ANCHOR + " Tongue-and-groove panels are fastened row by row and each board is "
                 "tapped home with a rubber mallet until the far wall is fully clad. Near-field "
                 "sound carries mallet knocks. continuous construction time-lapse, not "
                 "real-time footage.")
        errs = pp.check_video_process_content(ghost)
        self.assertTrue(any('ghost work' in e for e in errs), errs)


class TestReworkStructuralVideoBeat(unittest.TestCase):
    ERRS = ["VIDEO describes no visible action/process beyond the anchor opening and audio lines"]

    def test_valid_rewrite_is_adopted(self):
        with patch.object(pp, '_chat', return_value=GOOD_REWRITE):
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertTrue(adopted)
        self.assertEqual(out, GOOD_REWRITE)

    def test_rewrite_missing_anchor_opening_is_rejected(self):
        # 重写稿吃掉了首尾帧锚定开场 → 拒绝，保留原稿
        bad = "One lone worker is fastening panels wall by wall with a mallet. " * 3
        with patch.object(pp, '_chat', return_value=bad):
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertFalse(adopted)
        self.assertEqual(out, HOLLOW_VIDEO)

    def test_rewrite_still_hollow_is_rejected(self):
        # 重写稿仍然只有锚定+音效（复验不过）→ 保留原稿
        with patch.object(pp, '_chat', return_value=HOLLOW_VIDEO):
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertFalse(adopted)
        self.assertEqual(out, HOLLOW_VIDEO)

    def test_llm_exception_keeps_original(self):
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertFalse(adopted)
        self.assertEqual(out, HOLLOW_VIDEO)

    def test_first_attempt_fails_second_attempt_succeeds(self):
        # 2026-07-17 实跑三单发现单发命中率不够——第一轮仍是空心，第二轮带着具体
        # 拒绝原因重试后写对了，应该被采纳（默认 max_attempts=2）。
        with patch.object(pp, '_chat', side_effect=[HOLLOW_VIDEO, GOOD_REWRITE]) as chat:
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertTrue(adopted)
        self.assertEqual(out, GOOD_REWRITE)
        self.assertEqual(chat.call_count, 2)
        # 第二轮的 user message 里必须带上第一轮具体的拒绝原因，而不是原样重复第一轮
        second_call_user = chat.call_args_list[1].args[2]
        self.assertIn('Your previous attempt was rejected', second_call_user)

    def test_gives_up_after_max_attempts_exhausted(self):
        with patch.object(pp, '_chat', return_value=HOLLOW_VIDEO) as chat:
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertFalse(adopted)
        self.assertEqual(out, HOLLOW_VIDEO)
        self.assertEqual(chat.call_count, 2)  # 默认 max_attempts=2，尝试完就放弃

    def test_max_attempts_is_configurable(self):
        with patch.object(pp, '_chat', return_value=HOLLOW_VIDEO) as chat:
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {}, max_attempts=3)
        self.assertFalse(adopted)
        self.assertEqual(chat.call_count, 3)

    def test_bridge_beat_requires_camera_translation_in_rewrite(self):
        errs = ["Bridge VIDEO contains no camera-translation description"]
        beat = {'bridge_stage': 1}
        # 桥接拍的重写稿写了工人动作但没有运镜 → 复验（is_bridge）不过 → 拒绝
        with patch.object(pp, '_chat', return_value=GOOD_REWRITE):
            out, adopted = pp.rework_structural_video_beat({}, 5, HOLLOW_VIDEO, errs, {}, beat=beat)
        self.assertFalse(adopted)
        # 带运镜描述的桥接重写稿被采纳
        bridge_fix = (ANCHOR + " The camera glides forward in a slow coaxial dolly push toward "
                      "the open threshold, crossing the sill as daylight rolls into the darker "
                      "interior exposure. Near-field sound carries salt crunches beneath steady lake wind.")
        with patch.object(pp, '_chat', return_value=bridge_fix):
            out, adopted = pp.rework_structural_video_beat({}, 5, HOLLOW_VIDEO, errs, {}, beat=beat)
        self.assertTrue(adopted)
        self.assertEqual(out, bridge_fix)


class TestLeadingLabelStrip(unittest.TestCase):
    def test_short_colon_line_is_stripped(self):
        self.assertEqual(pp._strip_leading_label_line('Beat 5 video prompt:\nActual content here.'),
                          'Actual content here.')

    def test_no_label_line_is_unchanged(self):
        text = 'Use the provided first frame and last frame as exact composition anchors.'
        self.assertEqual(pp._strip_leading_label_line(text), text)

    def test_long_first_line_not_treated_as_label(self):
        # 首行本身就是正文一句话（长、不是短标签）——不该被当标签丢掉
        text = ('Use the provided first frame and last frame as exact composition anchors, and '
                'nothing else about the framing changes here:\nSecond line continues the prompt.')
        self.assertEqual(pp._strip_leading_label_line(text), text)


class TestReworkStructuralVideoBeatEchoedLabel(unittest.TestCase):
    """2026-07-16 实测根因：LLM 把 user message 的 "Beat N video prompt:" 标签复读在
    正文最前面，导致内容完全合格的重写稿被严格 startswith 检查当废稿丢弃（3/3 复现）。
    改成宽松定位锚点开场起点后，同样的带标签回复应该被剥掉标签、正常采纳。"""
    ERRS = ["VIDEO describes no visible action/process beyond the anchor opening and audio lines"]

    def test_echoed_label_prefix_is_recovered_not_rejected(self):
        echoed = "Beat 6 video prompt:\n" + GOOD_REWRITE
        with patch.object(pp, '_chat', return_value=echoed):
            out, adopted = pp.rework_structural_video_beat({}, 6, HOLLOW_VIDEO, self.ERRS, {})
        self.assertTrue(adopted)
        self.assertEqual(out, GOOD_REWRITE)


# --- IMAGE 侧相似度回炉（2026-07-16 新增，此前完全没有对应机制） ---

# 2026-07-16 实跑「盐湖坠落货机尾段」单的真实合成结果：图片 9→10 命中
# similarity 1.00（除相机/几何锁定句外几乎逐字复读，只有 traces 那句在变）。
PREV_IMAGE = ("Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same "
              "camera height as the exterior shots, camera pitch locked level; the central vanishing "
              "axis stays centered on the rear interior wall in Grid B2. New traces include screw "
              "rows, clean board seams, and pale gypsum dust.")

CURR_IMAGE_TOO_SIMILAR = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same camera "
    "height as the exterior shots, camera pitch locked level; the central vanishing axis stays "
    "centered on the rear interior wall in Grid B2. Physical traces include faint roller lap bands, "
    "blue masking tape borders, and fine primer speckles on floor paper.")

GOOD_IMAGE_REWRITE = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same camera "
    "height as the exterior shots, camera pitch locked level; the central vanishing axis stays "
    "centered on the rear interior wall in Grid B2. A thin band of roller texture has appeared "
    "along the lower wall, with strips of blue tape marking clean edges and a faint dusting of "
    "primer speckling the protective floor paper near the baseboards.")


class TestSplitImageSimilarityErrors(unittest.TestCase):
    def test_image_and_video_similarity_errors_are_separated(self):
        errs = [
            "IMAGE phrasing/structure is too similar to previous beat (cleaned similarity: 1.00 > 0.88).",
            "VIDEO phrasing/structure is too similar to previous beat (cleaned similarity: 0.70 > 0.65).",
            "IMAGE prompt word count (190) exceeds limit of 170 words",
        ]
        similar, rest = pp.split_image_similarity_errors(errs)
        self.assertEqual(len(similar), 1)
        self.assertIn('IMAGE phrasing', similar[0])
        self.assertEqual(len(rest), 2)

    def test_empty_and_none_input(self):
        self.assertEqual(pp.split_image_similarity_errors([]), ([], []))
        self.assertEqual(pp.split_image_similarity_errors(None), ([], []))


class TestReworkSimilarImageBeat(unittest.TestCase):
    ERRS = ["IMAGE phrasing/structure is too similar to previous beat (cleaned similarity: 1.00 > 0.88)."]

    def test_valid_rewrite_is_adopted(self):
        with patch.object(pp, '_chat', return_value=GOOD_IMAGE_REWRITE):
            out, adopted = pp.rework_similar_image_beat({}, 10, CURR_IMAGE_TOO_SIMILAR, self.ERRS, {},
                                                          prev_image=PREV_IMAGE)
        self.assertTrue(adopted)
        self.assertEqual(out, GOOD_IMAGE_REWRITE)

    def test_still_similar_rewrite_is_rejected(self):
        # 重写稿换了几个词但整体结构还是跟上一拍高度重合 → 复验不过 → 保留原稿
        with patch.object(pp, '_chat', return_value=CURR_IMAGE_TOO_SIMILAR):
            out, adopted = pp.rework_similar_image_beat({}, 10, CURR_IMAGE_TOO_SIMILAR, self.ERRS, {},
                                                          prev_image=PREV_IMAGE)
        self.assertFalse(adopted)
        self.assertEqual(out, CURR_IMAGE_TOO_SIMILAR)

    def test_llm_exception_keeps_original(self):
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted = pp.rework_similar_image_beat({}, 10, CURR_IMAGE_TOO_SIMILAR, self.ERRS, {},
                                                          prev_image=PREV_IMAGE)
        self.assertFalse(adopted)
        self.assertEqual(out, CURR_IMAGE_TOO_SIMILAR)

    def test_echoed_label_prefix_is_recovered_not_rejected(self):
        echoed = "Beat 10 image prompt:\n" + GOOD_IMAGE_REWRITE
        with patch.object(pp, '_chat', return_value=echoed):
            out, adopted = pp.rework_similar_image_beat({}, 10, CURR_IMAGE_TOO_SIMILAR, self.ERRS, {},
                                                          prev_image=PREV_IMAGE)
        self.assertTrue(adopted)
        self.assertEqual(out, GOOD_IMAGE_REWRITE)


# --- STAGE SCOPE 措辞合规回炉（2026-07-17 实跑发现：LLM 自由生成正文不可靠复述分档
# 关键词——default 拍甚至写出了 large 拍的 "entire floor area"，因此在配额机制之上
# 加一道确定性事后校验+回炉，同款 rework_similar_image_beat 的保守契约） ---

LARGE_IMAGE_MISSING_COVERAGE = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same "
    "camera height as the exterior shots, camera pitch locked level; the central vanishing "
    "axis stays centered on the rear interior wall in Grid B2. Traces include screw rows, "
    "taped seams, and gypsum dust."
)

LARGE_IMAGE_GOOD_REWRITE = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same "
    "camera height as the exterior shots, camera pitch locked level; the central vanishing "
    "axis stays centered on the rear interior wall in Grid B2. All interior walls and the "
    "ceiling curve are now fully paneled, with the entire floor area finished underfoot. "
    "Traces include screw rows, taped seams, and gypsum dust."
)

DEFAULT_IMAGE_OVERCLAIMING = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same "
    "camera height as the exterior shots, camera pitch locked level; the central vanishing "
    "axis stays centered on the rear interior wall in Grid B2. The entire floor area is now "
    "finished with fresh hardwood boards."
)

DEFAULT_IMAGE_GOOD_REWRITE = (
    "Static tripod shot inside the enclosed interior, same ultra-wide lens feel and same "
    "camera height as the exterior shots, camera pitch locked level; the central vanishing "
    "axis stays centered on the rear interior wall in Grid B2. One section of fresh hardwood "
    "boards now covers the near half of the floor, with bare subfloor still visible beyond it."
)


class TestCheckStageScopeWording(unittest.TestCase):
    def test_large_without_coverage_claim_is_flagged(self):
        errs = pp.check_stage_scope_wording(LARGE_IMAGE_MISSING_COVERAGE, 'large')
        self.assertTrue(errs)
        self.assertIn('large', errs[0])

    def test_large_with_coverage_claim_passes(self):
        self.assertEqual(pp.check_stage_scope_wording(LARGE_IMAGE_GOOD_REWRITE, 'large'), [])

    def test_default_overclaiming_full_coverage_is_flagged(self):
        errs = pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, 'default')
        self.assertTrue(errs)
        self.assertIn('entire', errs[0])

    def test_default_without_overclaim_passes(self):
        self.assertEqual(pp.check_stage_scope_wording(DEFAULT_IMAGE_GOOD_REWRITE, 'default'), [])

    def test_small_overclaiming_full_coverage_is_flagged(self):
        errs = pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, 'small')
        self.assertTrue(errs)

    def test_none_scope_and_empty_prompt_are_noop(self):
        self.assertEqual(pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, None), [])
        self.assertEqual(pp.check_stage_scope_wording('', 'large'), [])


class TestReworkStageScopeWordingBeat(unittest.TestCase):
    def test_large_rewrite_adding_coverage_is_adopted(self):
        errs = pp.check_stage_scope_wording(LARGE_IMAGE_MISSING_COVERAGE, 'large')
        with patch.object(pp, '_chat', return_value=LARGE_IMAGE_GOOD_REWRITE):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 11, LARGE_IMAGE_MISSING_COVERAGE, errs, 'large')
        self.assertTrue(adopted)
        self.assertEqual(out, LARGE_IMAGE_GOOD_REWRITE)

    def test_large_rewrite_still_missing_coverage_is_rejected(self):
        errs = pp.check_stage_scope_wording(LARGE_IMAGE_MISSING_COVERAGE, 'large')
        with patch.object(pp, '_chat', return_value=LARGE_IMAGE_MISSING_COVERAGE):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 11, LARGE_IMAGE_MISSING_COVERAGE, errs, 'large')
        self.assertFalse(adopted)
        self.assertEqual(out, LARGE_IMAGE_MISSING_COVERAGE)

    def test_default_rewrite_removing_overclaim_is_adopted(self):
        errs = pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, 'default')
        with patch.object(pp, '_chat', return_value=DEFAULT_IMAGE_GOOD_REWRITE):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 7, DEFAULT_IMAGE_OVERCLAIMING, errs, 'default')
        self.assertTrue(adopted)
        self.assertEqual(out, DEFAULT_IMAGE_GOOD_REWRITE)

    def test_default_rewrite_still_overclaiming_is_rejected(self):
        errs = pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, 'default')
        with patch.object(pp, '_chat', return_value=DEFAULT_IMAGE_OVERCLAIMING):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 7, DEFAULT_IMAGE_OVERCLAIMING, errs, 'default')
        self.assertFalse(adopted)
        self.assertEqual(out, DEFAULT_IMAGE_OVERCLAIMING)

    def test_llm_exception_keeps_original(self):
        errs = pp.check_stage_scope_wording(DEFAULT_IMAGE_OVERCLAIMING, 'default')
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 7, DEFAULT_IMAGE_OVERCLAIMING, errs, 'default')
        self.assertFalse(adopted)
        self.assertEqual(out, DEFAULT_IMAGE_OVERCLAIMING)

    def test_echoed_label_prefix_is_recovered_not_rejected(self):
        errs = pp.check_stage_scope_wording(LARGE_IMAGE_MISSING_COVERAGE, 'large')
        echoed = "Beat 11 image prompt:\n" + LARGE_IMAGE_GOOD_REWRITE
        with patch.object(pp, '_chat', return_value=echoed):
            out, adopted = pp.rework_stage_scope_wording_beat(
                {}, 11, LARGE_IMAGE_MISSING_COVERAGE, errs, 'large')
        self.assertTrue(adopted)
        self.assertEqual(out, LARGE_IMAGE_GOOD_REWRITE)


# --- SIGNATURE ANCHOR RULE（2026-07-20 根因修复：dimensions.anchors / 一键合成灵感
# 卡片的 twist_zh 此前只被当成"参考文字"喂给 Step 1 brief-parsing LLM 调用，从未落到
# 任何结构化字段，往后整条合成管线（beat ladder / 逐拍撰写 / 确定性复核）都读不到它，
# 导致招牌反差点从未出现在渲染出的提示词里。现在 beat ladder 生成要求在 reward 拍
# 声明 anchor_keywords（字面短语），这里是"声明了就必须字面出现"的确定性复核+回炉，
# 同款 rework_stage_scope_wording_beat 的保守契约。) ---

REWARD_IMAGE_MISSING_ANCHOR = (
    "Static tripod shot inside the finished circular interior, same lens feel and camera "
    "height as prior beats, camera pitch locked level; the central vanishing axis stays "
    "centered on the rear wall in Grid B2. A plain black metal stove sits in the corner, "
    "warm light spilling across the finished timber floor."
)

REWARD_IMAGE_GOOD_REWRITE = (
    "Static tripod shot inside the finished circular interior, same lens feel and camera "
    "height as prior beats, camera pitch locked level; the central vanishing axis stays "
    "centered on the rear wall in Grid B2. The cast-iron valve stove hangs suspended above "
    "the hearth, warm light spilling across the finished timber floor."
)


class TestCheckSignatureAnchorRealized(unittest.TestCase):
    def test_no_anchor_keywords_declared_is_noop(self):
        self.assertEqual(
            pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, {'operation': 'reward'}), [])
        self.assertEqual(pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, None), [])

    def test_missing_keyword_is_flagged(self):
        beat = {'operation': 'reward', 'anchor_keywords': ['cast-iron valve stove']}
        errs = pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, beat)
        self.assertTrue(errs)
        self.assertIn('cast-iron valve stove', errs[0])

    def test_present_keyword_passes(self):
        beat = {'operation': 'reward', 'anchor_keywords': ['cast-iron valve stove']}
        self.assertEqual(pp.check_signature_anchor_realized(REWARD_IMAGE_GOOD_REWRITE, beat), [])

    def test_keyword_match_is_case_insensitive(self):
        beat = {'operation': 'reward', 'anchor_keywords': ['Cast-Iron Valve Stove']}
        self.assertEqual(pp.check_signature_anchor_realized(REWARD_IMAGE_GOOD_REWRITE, beat), [])

    def test_multiple_keywords_each_checked_independently(self):
        beat = {'operation': 'reward', 'anchor_keywords': ['cast-iron valve stove', 'suspended above the hearth']}
        errs = pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, beat)
        self.assertEqual(len(errs), 2)


class TestReworkMissingAnchorBeat(unittest.TestCase):
    BEAT = {'operation': 'reward', 'anchor_keywords': ['cast-iron valve stove']}

    def test_valid_rewrite_is_adopted(self):
        errs = pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, self.BEAT)
        with patch.object(pp, '_chat', return_value=REWARD_IMAGE_GOOD_REWRITE):
            out, adopted = pp.rework_missing_anchor_beat({}, 12, REWARD_IMAGE_MISSING_ANCHOR, errs, beat=self.BEAT)
        self.assertTrue(adopted)
        self.assertEqual(out, REWARD_IMAGE_GOOD_REWRITE)

    def test_rewrite_still_missing_anchor_is_rejected(self):
        errs = pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, self.BEAT)
        with patch.object(pp, '_chat', return_value=REWARD_IMAGE_MISSING_ANCHOR):
            out, adopted = pp.rework_missing_anchor_beat({}, 12, REWARD_IMAGE_MISSING_ANCHOR, errs, beat=self.BEAT)
        self.assertFalse(adopted)
        self.assertEqual(out, REWARD_IMAGE_MISSING_ANCHOR)

    def test_llm_exception_keeps_original(self):
        errs = pp.check_signature_anchor_realized(REWARD_IMAGE_MISSING_ANCHOR, self.BEAT)
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted = pp.rework_missing_anchor_beat({}, 12, REWARD_IMAGE_MISSING_ANCHOR, errs, beat=self.BEAT)
        self.assertFalse(adopted)
        self.assertEqual(out, REWARD_IMAGE_MISSING_ANCHOR)


# --- IMAGE 侧 sterile 占位句禁令（2026-07-21 水磨坊实测确诊：'sterile'/'sterile of
# objects' 是 VIDEO 专用声明词汇被合成 LLM 挪用到 IMAGE 当"这拍没新内容"的万能占位
# 句，4/12 张 IMAGE 命中，直接打穿破损/原始感要求；同款保守回炉契约） ---

STERILE_IMAGE = (
    "Static wide 18mm interior tripod shot, camera height 1.6m, locked eye-level "
    "perspective down the central loft axis; camera pitch locked level; central "
    "vanishing axis centered. Completely sterile of objects. Locked anchors: historic "
    "cast-iron drive gear hub at Grid B2 holding 45 percent of frame height."
)

STERILE_IMAGE_GOOD_REWRITE = (
    "Static wide 18mm interior tripod shot, camera height 1.6m, locked eye-level "
    "perspective down the central loft axis; camera pitch locked level; central "
    "vanishing axis centered. Fine pale grey scraper score lines streak the granite "
    "wall beside a bright yellow pressure-washer hose coiled on the damp floor timbers. "
    "Locked anchors: historic cast-iron drive gear hub at Grid B2 holding 45 percent of "
    "frame height."
)


class TestCheckImageDecayPlaceholder(unittest.TestCase):
    def test_completely_sterile_is_flagged(self):
        errs = pp.check_image_decay_placeholder("Completely sterile.")
        self.assertTrue(errs)
        self.assertIn('sterile', errs[0])

    def test_sterile_of_objects_is_flagged(self):
        errs = pp.check_image_decay_placeholder(STERILE_IMAGE)
        self.assertTrue(errs)

    def test_prompt_without_sterile_passes(self):
        self.assertEqual(pp.check_image_decay_placeholder(STERILE_IMAGE_GOOD_REWRITE), [])

    def test_empty_prompt_is_noop(self):
        self.assertEqual(pp.check_image_decay_placeholder(''), [])
        self.assertEqual(pp.check_image_decay_placeholder(None), [])


class TestReworkDecayPlaceholderBeat(unittest.TestCase):
    ERRS = ["IMAGE prompt uses the word 'sterile' — that is VIDEO-only vocabulary"]

    def test_valid_rewrite_is_adopted(self):
        with patch.object(pp, '_chat', return_value=STERILE_IMAGE_GOOD_REWRITE):
            out, adopted = pp.rework_decay_placeholder_beat({}, 4, STERILE_IMAGE, self.ERRS)
        self.assertTrue(adopted)
        self.assertEqual(out, STERILE_IMAGE_GOOD_REWRITE)

    def test_rewrite_still_using_sterile_is_rejected(self):
        with patch.object(pp, '_chat', return_value=STERILE_IMAGE):
            out, adopted = pp.rework_decay_placeholder_beat({}, 4, STERILE_IMAGE, self.ERRS)
        self.assertFalse(adopted)
        self.assertEqual(out, STERILE_IMAGE)

    def test_llm_exception_keeps_original(self):
        with patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            out, adopted = pp.rework_decay_placeholder_beat({}, 4, STERILE_IMAGE, self.ERRS)
        self.assertFalse(adopted)
        self.assertEqual(out, STERILE_IMAGE)


# --- FIRST INTERIOR REVEAL 强制衰败措辞校验：已于 2026-08-24 整条删除 ---
#
# 原本这里有三组用例（衰败类目计数、人工痕迹判定、定向回炉），守的是「过门后室内首现
# 帧必须是没人碰过的废墟」那条硬规则。这条线现在跑的全是爆款复刻——门后是什么样由原片
# 说了算，模板不再有权规定——check_first_interior_reveal_decay /
# rework_first_interior_reveal_decay_beat 与 'decay' 那路缺陷回炉一并删除，用例随之退役。
# 仍然生效、并且仍有用例覆盖的是防倒退那一半（见 test_envelope_seal_monotonicity.py 的
# test_first_interior_reveal_scopes_untouched_to_unworked_surfaces）。


# --- 相似度免检清单不再豁免 'sterile'（2026-07-21）：之前 'sterile' 在
# is_mostly_boilerplate 的 dna_keywords 里，含这个词的句子整句被当结构性样板忽略，
# 两拍写出几乎一样的 "sterile" 占位句也测不出"太像上一拍"。摘除后应恢复可测。 ---

class TestStylisticRepetitionNoLongerIgnoresSterile(unittest.TestCase):
    def test_duplicate_sterile_sentence_is_now_caught(self):
        prev = (
            "Static wide 18mm interior tripod shot, camera height 1.6m, locked "
            "eye-level perspective down the central loft axis; camera pitch locked "
            "level; central vanishing axis centered. Completely sterile of objects. "
            "Historic cast-iron drive gear hub sits centered in frame."
        )
        curr = (
            "Static wide 18mm interior tripod shot, camera height 1.6m, locked "
            "eye-level perspective down the central loft axis; camera pitch locked "
            "level; central vanishing axis centered. Completely sterile of objects. "
            "Solid oak floor joist framework spans the visible floor plane."
        )
        errs = pp.check_stylistic_repetition(curr, prev, {}, is_video=False)
        self.assertTrue(errs)
        self.assertTrue(any('too similar' in e for e in errs))


# --- 拍级"图文内容脱节"校验（2026-07-22 喀斯特溶洞实测确诊：VIDEO 明确写了搬入
# 躺椅/石桌/盆栽，配套 IMAGE 正文却完全没提这些家具，只写了一句借旧词拼出的空洞
# 完工声明；check_stage_scope_wording 只查"有没有完工关键词"，查不出这层更深的
# 内容缺失）---

DAYBED_TRACE_ITEM = {
    "name": "beige linen daybed",
    "material_color": "beige linen",
    "initial_state": "installed",
    "grid": "Grid B2",
    "z_depth_scale": "40%",
}

IMAGE_MISSING_DAYBED = (
    "Static wide 14mm interior tripod shot, camera height 1.6m, camera pitch locked "
    "level; central vanishing axis centered. Final lighting is now stable and complete "
    "across every interior wall, vaulted ceiling, and floor surface. Locked anchors: "
    "carved limestone archway at Grid B2 holding 45 percent of frame height."
)

IMAGE_WITH_DAYBED_DIFFERENT_PHRASING = (
    "Static wide 14mm interior tripod shot, camera height 1.6m, camera pitch locked "
    "level; central vanishing axis centered. A custom low-profile linen lounge daybed "
    "with cream beige textiles now sits against the far wall. Locked anchors: carved "
    "limestone archway at Grid B2 holding 45 percent of frame height."
)

IMAGE_WITH_DAYBED_GOOD_REWRITE = (
    "Static wide 14mm interior tripod shot, camera height 1.6m, camera pitch locked "
    "level; central vanishing axis centered. A beige linen daybed now sits freshly "
    "installed against the far wall at Grid B2. Locked anchors: carved limestone "
    "archway at Grid B2 holding 45 percent of frame height."
)


class TestCheckImageRealizesTraces(unittest.TestCase):
    def test_check_image_realizes_traces_flags_missing_feature(self):
        errs = pp.check_image_realizes_traces(IMAGE_MISSING_DAYBED, [DAYBED_TRACE_ITEM])
        self.assertTrue(errs)
        self.assertIn('beige linen daybed', errs[0])

    def test_check_image_realizes_traces_lenient_on_partial_phrasing(self):
        errs = pp.check_image_realizes_traces(IMAGE_WITH_DAYBED_DIFFERENT_PHRASING, [DAYBED_TRACE_ITEM])
        self.assertEqual(errs, [])

    def test_check_image_realizes_traces_empty_when_no_traces(self):
        self.assertEqual(pp.check_image_realizes_traces(IMAGE_MISSING_DAYBED, []), [])
        self.assertEqual(pp.check_image_realizes_traces(IMAGE_MISSING_DAYBED, None), [])


class TestReworkMissingContentImageBeat(unittest.TestCase):
    def test_rework_missing_content_image_beat_adopts_good_rewrite(self):
        with patch.object(pp, '_chat', return_value=IMAGE_WITH_DAYBED_GOOD_REWRITE):
            out, adopted = pp.rework_missing_content_image_beat(
                {}, 10, IMAGE_MISSING_DAYBED, [DAYBED_TRACE_ITEM])
        self.assertTrue(adopted)
        self.assertEqual(out, IMAGE_WITH_DAYBED_GOOD_REWRITE)
        self.assertEqual(pp.check_image_realizes_traces(out, [DAYBED_TRACE_ITEM]), [])

    def test_rework_missing_content_image_beat_rejects_bad_rewrite(self):
        with patch.object(pp, '_chat', return_value=IMAGE_MISSING_DAYBED):
            out, adopted = pp.rework_missing_content_image_beat(
                {}, 10, IMAGE_MISSING_DAYBED, [DAYBED_TRACE_ITEM])
        self.assertFalse(adopted)
        self.assertEqual(out, IMAGE_MISSING_DAYBED)


if __name__ == '__main__':
    unittest.main()
