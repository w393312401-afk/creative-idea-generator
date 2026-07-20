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
        beat = {'bridge_stage': 2}
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


if __name__ == '__main__':
    unittest.main()
