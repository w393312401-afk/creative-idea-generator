"""Unit tests for Dynamic Spatial Migration & Anti-Position-Lock protocol (Rule 13, 15 & 17).

Verifies that characters/figurines are never permanently locked into corners (e.g. lower-left),
and that spatial migration is enforced from reverse to compose and linter/fixer.
"""

import unittest
import prompt_pipeline as pp
from prompt_pipeline.fast_reverse import _default_cast_action_for_stage
from prompt_pipeline.fast_composer import build_fast_composer_system_prompt
from prompt_pipeline.composers.miniature import MiniatureComposer


class DynamicSpatialMigrationTests(unittest.TestCase):
    """测试人物/人偶全序列空间迁移与防站桩死锁体系。"""

    def test_repetitive_cast_positioning_detector(self):
        """连续帧放置在同一角落必须被检测出来。"""
        images = {
            1: "Macro photo. Impoverished figurines stand at the lower-left watching the ruined hut.",
            2: "Macro photo. The resident figurines stand at the lower-left observing the cleared mudflat.",
            3: "Macro photo. Figurines stand at the diorama edge watching the ash survey lines.",
        }
        errors = pp.check_repetitive_cast_positioning(images)
        self.assertTrue(len(errors) >= 1)
        self.assertTrue(any('repeats the same cast position' in e for e in errors))

    def test_non_repetitive_cast_positioning_passes(self):
        """动态迁移位置的提示词不应报错。"""
        images = {
            1: "Macro photo. Figurines crouch near the site boundary studying the blueprint.",
            2: "Macro photo. Figurines walk along the chalk grid lines inspecting boundary stakes.",
            3: "Macro photo. Figurines step beside the front column touching the brass shoe.",
            4: "Macro photo. Figurines stand on the elevated timber veranda enjoying the view.",
        }
        errors = pp.check_repetitive_cast_positioning(images)
        self.assertEqual(errors, [])

    def test_fix_repetitive_cast_positioning(self):
        """重复的站桩描述必须被自动重置为随工序迁移的动态交互位置。"""
        images = {
            1: "A vertical 9:16 macro photograph. Two miniature figurines stand on the ground at the lower left gazing up.",
            2: "A vertical 9:16 macro photograph. Two miniature figurines stand on the ground at the lower left gazing up.",
            3: "A vertical 9:16 macro photograph. Two miniature figurines stand on the ground at the lower left gazing up.",
        }
        beats = [
            {'stage': 'demolition'},
            {'stage': 'structural'},
            {'stage': 'roof'},
        ]
        fixed = pp.fix_repetitive_cast_positioning(images, beats)
        # Frame 2 和 Frame 3 必须不再含有 lower left
        self.assertNotIn("at the lower left", fixed[2])
        self.assertNotIn("at the lower left", fixed[3])
        # 修复后的提示词不能触发重复报警
        errors = pp.check_repetitive_cast_positioning(fixed)
        self.assertEqual(errors, [])

    def test_fast_reverse_stage_based_cast_defaults(self):
        """反推 fallback 必须按工序阶段提供具象化位置与动作。"""
        demo = _default_cast_action_for_stage('demolition', 1, 17)
        self.assertIn("blueprint", demo.lower())

        struct = _default_cast_action_for_stage('structural', 5, 17)
        self.assertIn("structural framework", struct.lower())

        trans = _default_cast_action_for_stage('transition', 11, 17)
        self.assertIn("threshold", trans.lower())

        reveal = _default_cast_action_for_stage('reveal', 17, 17)
        self.assertIn("celebrating", reveal.lower())

    def test_fast_composer_system_prompt_mandates_spatial_migration(self):
        """直通合成系统提示词必须显式包含全序列空间迁移律。"""
        sys_prompt = build_fast_composer_system_prompt(is_miniature=True)
        self.assertIn("DYNAMIC SPATIAL MIGRATION & ANTI-POSITION-LOCK PROTOCOL", sys_prompt)
        self.assertIn("STRICTLY BAN REPETITIVE LOWER-LEFT STANDING", sys_prompt)
        self.assertNotIn("Describe their precise physical location", sys_prompt)

    def test_ensure_living_cast_reaction_dynamic(self):
        """MiniatureComposer 注入动作必须根据当前 beat 动态变化。"""
        mini = MiniatureComposer()
        v_prompt = "A vertical macro time-lapse video. Giant hand hammers wooden pegs."
        
        # Structural beat
        res_struct = mini.ensure_living_cast_reaction(v_prompt, beat={'stage': 'structural'})
        self.assertIn("columns", res_struct.lower())
        self.assertNotIn("down by the diorama edge", res_struct.lower())

        # Transition beat
        res_trans = mini.ensure_living_cast_reaction(v_prompt, beat={'stage': 'transition'})
        self.assertIn("threshold", res_trans.lower())

    def test_fix_miniature_video_scrubs_corner_standing(self):
        """fix_miniature_video 必须清洗 corner/edge 站桩词汇。"""
        mini = MiniatureComposer()
        raw = "A vertical macro time-lapse video. The miniature couple figurines stand attentively at the lower-left observing the giant hand."
        fixed = mini.fix_miniature_video(raw)
        self.assertNotIn("stand attentively at the lower-left observing", fixed)
        self.assertIn("dynamically shift stance", fixed)


if __name__ == '__main__':
    unittest.main()
