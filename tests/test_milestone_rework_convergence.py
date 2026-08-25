"""里程碑回炉与 profile 镜头语法回炉互相拆台的回归测试（2026-08-25）。

真实事故：19 拍的一单在 Beat 7 上整单判死（BEAT_GENERATION_FAILED）。日志里的循环是——
omni 的多镜头回炉把正文按切点表整段重排，顺手洗掉了里程碑骨架（"repeated cycles"、
两条进度线）；紧接着的里程碑成对回炉又是照**一镜到底**骨架写的，且采纳条件是「一条
不剩」，VIDEO 修好了但 IMAGE 还剩一条就整对丢弃 → 里程碑硬门不过 → 整拍重试 → 下一
轮同样两步再拆一次，重试用尽后整单挂在第 7 拍。

这里钉三件事：
  1. 里程碑成对回炉按「严格改善」采纳（错误集合真子集），不再全有全无；
  2. 它先过 profile 的归一钩子，且**引入**新的 profile 硬伤时判死（原稿本就有的不算）；
  3. omni 的多镜头回炉稿若洗掉了原稿有的里程碑骨架，一律保留原稿。
"""
import json
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline.composers import get_composer


MILESTONE_BEAT = {
    'index': 2, 'operation': 'framing', 'description': 'framing interior walls',
    'bridge_stage': None, 'stage_scope': 'large',
    'milestone_name': 'all interior framing complete',
    'before_state': 'the interior shell has no studs',
    'after_state': 'all declared wall and ceiling studs are installed',
    'completion_extent': 'all interior walls and the ceiling curve',
    'changed_grid_cells': ['Grid A2', 'Grid B2'],
    'package_operations': ['framing'],
    'primary_progress': 'stud count grows from zero to twelve',
    'secondary_progress': 'the staged timber bundle drains from twelve to zero',
    'persistent_traces': ['screw heads', 'sawdust bands'],
    'preserve_state': 'the cleared floor and original shell remain unchanged',
    'introduced_objects': [], 'removed_objects': [],
}

# 里程碑 VIDEO 骨架齐全的一稿（check_milestone_video_prompt 应为空）。
GOOD_VIDEO = (
    "Use the provided first frame and last frame as exact composition anchors. The interior "
    "shell has no studs at the first moment; the lone worker makes the first effective tool "
    "contact immediately and repeatedly performs the work cycle. The stud count grows from "
    "zero to twelve while the staged timber bundle drains from twelve to zero as pieces are "
    "carried in from the stack. By the final moment all declared wall and ceiling studs are "
    "installed across all interior walls and the ceiling curve."
)
# 只缺「重复工作循环」一条的稿子。
WEAK_VIDEO = GOOD_VIDEO.replace(" and repeatedly performs the work cycle", "")


class MilestonePairAdoptionTest(unittest.TestCase):
    """里程碑成对回炉：严格改善即采纳。"""

    def setUp(self):
        self.assertEqual(pp.check_milestone_video_prompt(GOOD_VIDEO, MILESTONE_BEAT), [])
        self.assertEqual(
            pp.check_milestone_video_prompt(WEAK_VIDEO, MILESTONE_BEAT),
            ['MILESTONE VIDEO must show repeated work cycles across the full clip.'])

    def _rework(self, reply, **kwargs):
        with patch.object(pp, '_chat', return_value=reply):
            return pp.rework_milestone_prompt_pair(
                {}, 2, WEAK_VIDEO, 'A static ultra-wide 14mm tripod shot at 1.6m height.',
                MILESTONE_BEAT,
                video_errors=pp.check_milestone_video_prompt(WEAK_VIDEO, MILESTONE_BEAT),
                image_errors=pp.check_milestone_image_prompt(
                    'A static ultra-wide 14mm tripod shot at 1.6m height.', MILESTONE_BEAT),
                **kwargs)

    def test_strict_improvement_is_adopted_even_with_residual_image_errors(self):
        """VIDEO 修好了、IMAGE 仍有残留时也要采纳——旧的全有全无写法正是死循环的一半。"""
        reply = json.dumps({'video': GOOD_VIDEO, 'image': 'A static ultra-wide 14mm tripod shot at 1.6m height.'})
        video, image, reworked = self._rework(reply)
        self.assertTrue(reworked)
        self.assertEqual(pp.check_milestone_video_prompt(video, MILESTONE_BEAT), [])
        # IMAGE 侧仍有残留，但这一对整体严格改善，交给下游 IMAGE 合并回炉继续修。
        self.assertTrue(pp.check_milestone_image_prompt(image, MILESTONE_BEAT))

    def test_non_improving_rewrite_is_rejected(self):
        """没修好任何一条（或换了一批新错）的重写稿仍旧保留原稿。"""
        reply = json.dumps({'video': WEAK_VIDEO, 'image': 'A static ultra-wide 14mm tripod shot at 1.6m height.'})
        video, _image, reworked = self._rework(reply)
        self.assertFalse(reworked)
        self.assertEqual(video, WEAK_VIDEO)

    def test_normalize_hook_runs_on_the_candidate(self):
        reply = json.dumps({'video': GOOD_VIDEO, 'image': 'A static ultra-wide 14mm tripod shot at 1.6m height.'})
        video, _image, reworked = self._rework(
            reply, normalize_video=lambda t: t + " NORMALIZED.")
        self.assertTrue(reworked)
        self.assertIn("NORMALIZED.", video)

    def test_rewrite_introducing_profile_violation_is_rejected(self):
        """profile 侧硬伤只在**新引入**时判死；原稿本来就有的那条不算。"""
        reply = json.dumps({'video': GOOD_VIDEO, 'image': 'A static ultra-wide 14mm tripod shot at 1.6m height.'})
        video, _image, reworked = self._rework(
            reply, profile_video_check=lambda t: ['OMNI: 镜头梯缺失'] if t is not WEAK_VIDEO else [])
        self.assertFalse(reworked)
        self.assertEqual(video, WEAK_VIDEO)

        video, _image, reworked = self._rework(
            reply, profile_video_check=lambda t: ['OMNI: 镜头梯缺失'])
        self.assertTrue(reworked, "原稿本来就有的 profile 硬伤不该把里程碑修复判死")


class OmniHookTest(unittest.TestCase):
    """omni profile 的两个钩子与多镜头回炉的里程碑守卫。"""

    def setUp(self):
        self.composer = get_composer('omni')

    def test_normalize_reworked_video_strips_one_take_language(self):
        text = self.composer.normalize_reworked_video(
            GOOD_VIDEO + " One unbroken take at a steady speed.", beat=MILESTONE_BEAT)
        self.assertNotIn('unbroken take', text.lower())

    def test_video_profile_violations_flags_missing_shot_ladder(self):
        self.assertTrue(self.composer.video_profile_violations(GOOD_VIDEO, beat=MILESTONE_BEAT))

    def test_multishot_rework_keeps_draft_when_milestone_skeleton_is_washed_out(self):
        """镜头语法过了、但把里程碑骨架洗掉的回炉稿必须被拒——它正是硬门死循环的另一半。"""
        washed = (
            "Use the provided first frame and last frame as exact composition anchors. "
            "The worker keeps building. Nothing else is described."
        )
        with patch.object(pp, '_chat', return_value=washed), \
                patch.object(type(self.composer), 'video_profile_violations', lambda *a, **k: []), \
                patch('prompt_pipeline.composers.omni.omni_video_violations', return_value=[]):
            video, reworked = self.composer.rework_omni_multishot(
                {}, 2, GOOD_VIDEO, {}, beat=MILESTONE_BEAT)
        self.assertFalse(reworked)
        self.assertEqual(video, GOOD_VIDEO)

    def test_multishot_rework_adopts_candidate_that_keeps_the_skeleton(self):
        keeper = GOOD_VIDEO + " A clean cut at the three-second mark."
        with patch.object(pp, '_chat', return_value=keeper), \
                patch('prompt_pipeline.composers.omni.omni_video_violations', return_value=[]):
            video, reworked = self.composer.rework_omni_multishot(
                {}, 2, GOOD_VIDEO, {}, beat=MILESTONE_BEAT)
        self.assertTrue(reworked)
        self.assertIn('three-second mark', video)


if __name__ == '__main__':
    unittest.main()
