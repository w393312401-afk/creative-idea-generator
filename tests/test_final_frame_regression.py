"""末帧（reward 拍）不得倒退：继承句 + 镜面地收窄。

2026-08-22 复盘的两单实证（沼泽废弃地堡 / 海边地堡）：末帧是全序列唯一一张既没有继承句
也没有防倒退句的帧——它是唯一走 `Final IMAGE` 模板的帧，也是唯一被
frame_state.compile_delta_image_prompt 原样放行的施工帧。于是末帧把早已被松木板盖住的
混凝土锚点原样复述回来、又凭空长出一片镜面地，末条视频只能把这个差量演成「拆掉松木板
露出混凝土，再倒环氧做镜面」。
"""
import unittest

from prompt_pipeline import (
    apply_proactive_fixes,
    fix_final_inherited_state,
    fix_rhma_blur,
    ladder_gloss_floor_milestone,
    prior_finished_milestones,
    strip_unearned_gloss_floor,
)


# 沼泽废弃地堡那单的梯子形状：木作把混凝土壳整个包起来，全程没有任何一拍做过高反光面。
MATTE_LADDER = [
    {'index': 1, 'operation': 'clearing', 'milestone_name': 'bunker interior cleared of debris',
     'after_state': 'Bare cast concrete shell swept clean.', 'completion_extent': 'all debris removed',
     'preserve_state': 'Concrete shell untouched.'},
    {'index': 2, 'operation': 'repair', 'milestone_name': 'curved walls clad in knotty pine boards',
     'after_state': 'Vertical tongue-and-groove pine covers both curved walls.',
     'completion_extent': 'walls fully clad', 'preserve_state': 'Ceiling still bare concrete.'},
    {'index': 3, 'operation': 'repair', 'milestone_name': 'ceiling dome clad in knotty pine',
     'after_state': 'Pine cladding wraps the full ceiling dome.',
     'completion_extent': 'ceiling fully clad', 'preserve_state': 'Pine walls stay finished.'},
    {'index': 4, 'operation': 'surface', 'milestone_name': 'dark walnut hardwood flooring laid',
     'after_state': 'Semi-matte walnut boards cover the whole subfloor.',
     'completion_extent': 'floor fully laid', 'preserve_state': 'Pine walls and ceiling stay finished.'},
    {'index': 5, 'operation': 'reward', 'milestone_name': 'bedroom shelter complete and styled',
     'after_state': 'Finished timber bedroom, warmly lit.', 'completion_extent': 'fully furnished',
     'preserve_state': 'the pine-clad walls and ceiling and the walnut floor stay finished and unchanged'},
]

# 同一条梯子，但第 4 拍真的倒了环氧——这一单的末帧写镜面地是合法的。
GLOSS_LADDER = [dict(b) for b in MATTE_LADDER]
GLOSS_LADDER[3] = dict(
    GLOSS_LADDER[3],
    milestone_name='clear gloss epoxy poured over the workshop floor',
    after_state='A mirror-bright epoxy coat seals the whole floor.',
)

# 骨架梯子：管线里真实存在的形态（compose_checkpoints 里多数项目就长这样）。
SKELETAL_LADDER = [
    {'index': n, 'operation': 'repair', 'description': f'Renovation work step {n}'}
    for n in range(1, 5)
] + [{'index': 5, 'operation': 'reward', 'description': 'Renovation work step 5'}]


class TestLadderGlossEvidence(unittest.TestCase):
    """镜面地是否有工序背书，只由梯子说了算，且必须区分"没有"和"答不了"。"""

    def test_matte_ladder_answers_no_gloss(self):
        self.assertEqual(ladder_gloss_floor_milestone(MATTE_LADDER, 5), '')

    def test_gloss_ladder_names_the_milestone(self):
        self.assertIn('epoxy', ladder_gloss_floor_milestone(GLOSS_LADDER, 5))

    def test_skeletal_ladder_cannot_answer(self):
        # 'Renovation work step 4' 里没有信息量：判成"没交付过"就会误删合法的镜面地。
        self.assertIsNone(ladder_gloss_floor_milestone(SKELETAL_LADDER, 5))

    def test_missing_ladder_cannot_answer(self):
        self.assertIsNone(ladder_gloss_floor_milestone(None, 5))
        self.assertIsNone(ladder_gloss_floor_milestone([], 5))

    def test_only_prior_beats_count(self):
        # 末拍自己写了"镜面完工"不算背书——那正是要被判定的那一句。
        self_gloss = [dict(MATTE_LADDER[0]),
                      {'index': 2, 'operation': 'reward',
                       'milestone_name': 'high-gloss epoxy floor revealed',
                       'after_state': 'x', 'completion_extent': 'y'}]
        self.assertEqual(ladder_gloss_floor_milestone(self_gloss, 2), '')

    def test_gloss_and_floor_must_share_a_clause(self):
        far_apart = [{'index': 1, 'operation': 'repair',
                      'milestone_name': 'epoxy anchors set for the wall brackets',
                      'after_state': 'Brackets bolted; the floor was swept afterwards.',
                      'completion_extent': 'brackets mounted'}]
        self.assertEqual(ladder_gloss_floor_milestone(far_apart, 2), '')


class TestStripUnearnedGlossFloor(unittest.TestCase):
    def test_reflection_sentence_is_dropped_whole(self):
        text = ("The room is finished and warmly lit. The highly reflective polished floor surface "
                "across the lower third displays a heavily blurred, diffused reflection of the "
                "background with realistic Fresnel falloff. A wool throw lies on the bed.")
        out = strip_unearned_gloss_floor(text)
        self.assertNotIn('reflect', out.lower())
        self.assertNotIn('Fresnel', out)
        self.assertIn('warmly lit', out)
        self.assertIn('wool throw', out)

    def test_gloss_adjective_is_downgraded_not_deleted(self):
        text = "A polished walnut floor runs under a woven jute rug and a reading chair."
        out = strip_unearned_gloss_floor(text)
        self.assertNotIn('polished', out.lower())
        self.assertIn('satin-matte', out)
        self.assertIn('woven jute rug', out)

    def test_non_floor_gloss_is_left_alone(self):
        text = "A polished brass pendant hangs from the ceiling above the bed."
        self.assertEqual(strip_unearned_gloss_floor(text), text)


class TestFixRhmaBlur(unittest.TestCase):
    MIRROR = ("The bedroom is complete. The highly reflective polished floor displays a mirror-bright "
              "reflection of the lantern above it.")

    def test_unearned_mirror_floor_is_removed(self):
        out = fix_rhma_blur(self.MIRROR, True, beat_ladder=MATTE_LADDER, beat_index=5)
        self.assertNotIn('reflect', out.lower())
        self.assertIn('The bedroom is complete.', out)

    def test_earned_mirror_floor_keeps_the_blur_clause(self):
        out = fix_rhma_blur(self.MIRROR, True, beat_ladder=GLOSS_LADDER, beat_index=5)
        self.assertIn('heavily blurred', out.lower())
        self.assertIn('diffused', out.lower())

    def test_unknown_ladder_preserves_legacy_behaviour(self):
        legacy = fix_rhma_blur(self.MIRROR, True)
        skeletal = fix_rhma_blur(self.MIRROR, True, beat_ladder=SKELETAL_LADDER, beat_index=5)
        self.assertEqual(legacy, skeletal)
        self.assertIn('heavily blurred', legacy.lower())

    def test_non_final_frames_are_untouched(self):
        self.assertEqual(fix_rhma_blur(self.MIRROR, False, beat_ladder=MATTE_LADDER, beat_index=3),
                         self.MIRROR)


class TestFinalInheritedState(unittest.TestCase):
    REWARD_BEAT = MATTE_LADDER[-1]

    def test_inheritance_and_no_regression_are_added(self):
        prompt = "Locked anchors: the cast concrete back wall facet at the centre of the frame."
        out = fix_final_inherited_state(prompt, self.REWARD_BEAT, MATTE_LADDER, 5)
        self.assertIn('Inherited state remains unchanged:', out)
        self.assertIn('never re-exposed as the original found', out)
        self.assertIn('no regression of any previously completed feature', out)

    def test_prior_milestones_are_named(self):
        out = fix_final_inherited_state("Locked anchors: the concrete dome.",
                                        self.REWARD_BEAT, MATTE_LADDER, 5)
        self.assertIn('ceiling dome clad in knotty pine', out)
        self.assertIn('dark walnut hardwood flooring laid', out)

    def test_existing_inheritance_clause_is_not_duplicated(self):
        prompt = ("Inherited state remains unchanged: the pine-clad walls stay finished. "
                  "Show no regression of any previously completed feature. "
                  "Nothing is ever re-exposed as the original found fabric.")
        self.assertEqual(fix_final_inherited_state(prompt, self.REWARD_BEAT, MATTE_LADDER, 5),
                         prompt)

    def test_milestones_with_digits_are_skipped(self):
        # completion_extent 式的 '100%' 措辞复述进正文会当场撞上 NLVTR 记号门禁。
        ladder = [{'index': 1, 'operation': 'repair', 'milestone_name': '80% of roof decking laid'},
                  dict(self.REWARD_BEAT, index=2)]
        self.assertEqual(prior_finished_milestones(ladder, 2), [])
        out = fix_final_inherited_state("Locked anchors: the ridge beam.", self.REWARD_BEAT, ladder, 2)
        self.assertNotIn('%', out)

    def test_skeletal_milestones_are_skipped(self):
        self.assertEqual(prior_finished_milestones(SKELETAL_LADDER, 5), [])

    def test_transition_beats_are_not_deliveries(self):
        ladder = [{'index': 1, 'operation': 'threshold', 'milestone_name': 'camera crosses the sill'},
                  {'index': 2, 'operation': 'repair', 'milestone_name': 'walls clad in pine'},
                  dict(self.REWARD_BEAT, index=3)]
        self.assertEqual(prior_finished_milestones(ladder, 3), ['walls clad in pine'])


class TestSwampBunkerRegression(unittest.TestCase):
    """沼泽废弃地堡那单的原始失败形态，整条 apply_proactive_fixes 跑一遍必须消失。"""

    PACKET = {
        'camera_dna': 'static wide interior shot, wide 20mm lens feel, camera height 1.3m',
        'interior_primary_landmarks': [
            {'name': 'the pine-clad ceiling dome', 'grid': 'Grid B1', 'z_depth_scale': '25%'},
            {'name': 'the pine-clad curved back wall', 'grid': 'Grid B2', 'z_depth_scale': '50%'},
        ],
    }
    # 实际出事的那张 IMAGE 13：混凝土锚点复辟 + 凭空镜面地。
    BAD_FINAL_IMAGE = (
        "static wide interior shot, wide 20mm lens feel, camera height 1.3m, camera pitch locked "
        "level; central vanishing axis centered. The cozy, fully renovated timber bedroom shelter "
        "stands peacefully complete in glowing warm light with a plush bed and a suspended hurricane "
        "lantern. The highly reflective polished floor surface across the lower third of the frame "
        "displays a heavily blurred, low-gloss, diffused reflection of the background, with realistic "
        "Fresnel falloff near the margins. Locked anchors: board-formed concrete ceiling dome across "
        "the upper centre of the frame; curved monolithic cast concrete back wall facet at the centre "
        "of the frame."
    )
    # 末条视频替那个倒退编出来的工序。
    BAD_FINAL_VIDEO = (
        "Use the provided first frame and last frame as exact composition anchors. Continuous "
        "construction time-lapse, not real-time footage. A lone worker pours and spreads a clear "
        "liquid gloss epoxy coat across the foreground wood floor using an aluminum squeegee, "
        "creating a mirror-like reflective surface on the floor. SFX: squeegee drag."
    )

    def _fixed(self):
        return apply_proactive_fixes(
            5, self.BAD_FINAL_VIDEO, self.BAD_FINAL_IMAGE, self.PACKET, 'Threshold',
            True, True, beat=MATTE_LADDER[-1], config=None, family='interior',
            beat_ladder=MATTE_LADDER)

    def test_invented_mirror_floor_is_gone_from_both_sides(self):
        video, image = self._fixed()
        self.assertNotIn('fresnel', image.lower())
        self.assertNotIn('reflective', image.lower())
        self.assertNotIn('mirror-like', video.lower())
        self.assertNotIn('gloss epoxy', video.lower())

    def test_final_image_gains_the_inheritance_and_no_regression_stanza(self):
        _video, image = self._fixed()
        low = image.lower()
        self.assertIn('inherited state remains unchanged', low)
        self.assertIn('never re-exposed as the original found', low)
        self.assertIn('no regression of any previously completed feature', low)

    def test_no_banned_notation_leaks_from_the_added_stanza(self):
        from prompt_pipeline import check_nlvtr_violations
        video, image = self._fixed()
        self.assertEqual(check_nlvtr_violations(image), [])
        self.assertEqual(check_nlvtr_violations(video), [])


if __name__ == '__main__':
    unittest.main()
