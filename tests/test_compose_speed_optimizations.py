"""提示词合成迭代速度与确定性补齐的回归测试 (2026-09-05)。

验证：
1. Omni 镜头语法变体扩展：自然语言多镜头措辞（如 macro insert / cutting back to wide 等）被正确识别，不再误报缺镜头导致不必要的网络回炉。
2. 里程碑骨架确定性补齐：patch_milestone_video_prompt 与 patch_milestone_image_prompt 就地满足起首接触、进度线、物料流向与持久痕迹要求，0ms 消除假性硬门失败。
3. 批量生成稿保护机制：批量草稿在确定性补齐后顺利通过硬门，不被丢弃退回单拍逐拍重试循环。
"""
import unittest

import prompt_pipeline as pp
from prompt_pipeline.composers.base import BaseComposer
from prompt_pipeline.composers.omni import (
    OmniComposer, _missing_shot_rungs, _body_without_timeline
)


class TestComposeSpeedOptimizations(unittest.TestCase):

    def setUp(self):
        self.beat = {
            'index': 2,
            'operation': 'joist_installation',
            'description': 'installing structural floor joists',
            'milestone_name': 'structural joists aligned and fastened',
            'before_state': 'cleared subfloor with perimeter ledger board',
            'after_state': 'all floor joists fastened across framing bays',
            'primary_progress': 'lifting and aligning dimensional timber joists',
            'secondary_progress': 'driving structural screws from a cardboard box',
            'completion_extent': 'full subfloor footprint',
            'persistent_traces': ['pilot drill holes', 'zinc shavings', 'pencil layout marks'],
        }

    def test_omni_natural_shot_variants_are_recognized(self):
        composer = OmniComposer()
        composer.config = {'videoDuration': '10'}
        ladder = composer.ladder_for_kind(10, 'construction')

        prompt = (
            "The sequence opens with an opening wide working shot showing the active workspace. "
            "The worker enters and starts. "
            "Cutting in closer with a clean cut to a macro insert on the tool contact point, "
            "popping fasteners while holding progress. "
            "A second insert reveals an extreme close up insert on the persistent scrape lines. "
            "Cutting back to wide shot from the same camera setup as the opening wide working shot, "
            "the operation completes and worker steps out."
        )
        missing = _missing_shot_rungs(_body_without_timeline(prompt), ladder)
        self.assertEqual(missing, [], f'自然变体应被识别通过，但误报缺少: {missing}')

    def test_milestone_deterministic_patching(self):
        sparse_v = 'A wide working shot of worker installing joists. Clean cuts.'
        sparse_i = 'A wide static shot of the completed timber framing.'

        self.assertTrue(bool(pp.check_milestone_video_prompt(sparse_v, self.beat)))
        self.assertTrue(bool(pp.check_milestone_image_prompt(sparse_i, self.beat)))

        patched_v = BaseComposer.patch_milestone_video_prompt(sparse_v, self.beat)
        patched_i = BaseComposer.patch_milestone_image_prompt(sparse_i, self.beat)

        v_errs = pp.check_milestone_video_prompt(patched_v, self.beat)
        i_errs = pp.check_milestone_image_prompt(patched_i, self.beat)

        self.assertEqual(v_errs, [], f'VIDEO 补齐后不应再报里程碑缺失: {v_errs}')
        self.assertEqual(i_errs, [], f'IMAGE 补齐后不应再报里程碑缺失: {i_errs}')

    def test_batch_composed_draft_not_discarded_by_minor_milestone_gap(self):
        base_composer = BaseComposer()
        contract = {
            'beat': self.beat,
            'is_last': False,
            'is_threshold_or_reveal': False,
            'family': 'interior',
            'is_bridge': False,
            'is_cut': False,
            'is_pre_bridge': False,
            'is_post_reveal_cleanup': False,
            'stage_scope': 'large',
        }
        raw_v = (
            'Use the provided image as anchor. Continuous construction time-lapse. '
            'In the opening frame the visible state is cleared subfloor with perimeter ledger board. '
            'The lone worker enters and makes first effective tool contact immediately, working repeatedly cycle by cycle. '
            'The primary progression shows lifting and aligning dimensional timber joists. '
            'By the final moment all floor joists fastened across framing bays. '
            'Materials are drawn from a stock container.'
        )
        raw_i = (
            'A static ultra-wide 14mm tripod shot. The scene centers on the structural joists aligned and fastened. '
            'The completed visible state reveals all floor joists fastened across framing bays. '
            'Across the full area: full subfloor footprint.'
        )

        v_p, i_p, _, _, _, _, _ = base_composer.repair_beat_prompts(
            {}, 2, raw_v, raw_i, contract, {}, [], None, None, [], [], None, 'Test'
        )

        rem_v = pp.check_milestone_video_prompt(v_p, self.beat)
        rem_i = pp.check_milestone_image_prompt(i_p, self.beat)
        self.assertEqual(rem_v, [])
        self.assertEqual(rem_i, [])


if __name__ == '__main__':
    unittest.main()
