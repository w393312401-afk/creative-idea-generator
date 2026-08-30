"""交付边界的收口（2026-08-30）。

规划期标注（「（工序：…）」「→」「；本拍认领…」）原先只在 apply_proactive_fixes 末尾剥，
那是**一条合成路径**的末尾；`_format_prompt_block` 才是所有正文出去的唯一一道门。
2026-08-30 实测：一单交付正文里留下 17 处「（工序：」和 18 处「→」，而同一段文本单独跑
scrub_planning_annotations 完全有效——不是正则漏了，是那条路没走到收口。

这一组钉住两件事：门上那道收口真的在剥；以及它**不能**顺手把换行也收掉
（scrub_spatial_notation 的 `\\s{2,}` 会，所以它有意没有一起挪过来）。
"""
import unittest

import prompt_pipeline as pp


class TestPlanningAnnotationsDieAtTheDoor(unittest.TestCase):

    def test_package_annotation_is_stripped_from_an_image_body(self):
        block = pp._format_prompt_block(
            {1: 'The hut is cleared away.（工序：dismantle hut、sweep ground） Locked anchors: x.'},
            {})
        self.assertNotIn('（工序：', block)
        self.assertIn('The hut is cleared away.', block)
        self.assertIn('Locked anchors: x.', block)

    def test_arrow_becomes_prose(self):
        block = pp._format_prompt_block(
            {1: 'The hand sweeps the soil level. → The old hut is cleared away.'}, {})
        self.assertNotIn('→', block)
        self.assertIn('resulting in', block)

    def test_video_bodies_go_through_the_same_door(self):
        block = pp._format_prompt_block({}, {1: 'Work continues.（工序：lay turf、rake lawn）'})
        self.assertNotIn('（工序：', block)

    def test_the_chinese_summary_is_scrubbed_too(self):
        block = pp._format_prompt_block(
            {1: {'body': 'A frame.', 'summary': '清理场地（工序：dismantle hut）'}}, {})
        self.assertIn('图片 1（清理场地）', block)
        self.assertNotIn('（工序：', block)

    def test_a_real_delivered_fragment_that_used_to_leak(self):
        """2026-08-30 那一单交付正文里的原文片段，逐字取回。"""
        leaked = ('Preserve unchanged: the completed prior work — The hand lowers and unfolds '
                  'the printed technical drawing flat against the dirt ground. → The full-colour '
                  'two-story villa architectural facade rendering lies flat on the ground.'
                  '（工序：unfold drawing、stage blueprint） — remains fully unchanged in the frame.')
        block = pp._format_prompt_block({1: leaked}, {})
        self.assertNotIn('（工序：', block)
        self.assertNotIn('→', block)
        self.assertIn('remains fully unchanged in the frame.', block)


class TestTheDoorDoesNotReflowTheBody(unittest.TestCase):
    """scrub_spatial_notation 有意没有一起挪到门上：它末尾的 `\\s{2,}` 含换行，会把锚点
    正文里的分行结构压成一行。门上这道用的是 `[ \\t]`，换行必须活着。"""

    MULTILINE = ('A vertical macro photograph of the diorama.\n'
                 '\n'
                 'Spatial layout:\n'
                 '- Left zone: two figurines stand on stony ground.\n'
                 '- Center zone: the ruined hut facade.\n')

    def test_line_structure_survives(self):
        block = pp._format_prompt_block({1: self.MULTILINE}, {})
        self.assertIn('Spatial layout:\n- Left zone:', block)
        self.assertIn('- Center zone: the ruined hut facade.', block)

    def test_grid_and_percent_are_left_to_the_composing_path(self):
        """门上不做记号翻译——那一道仍在 apply_proactive_fixes 里，对走那条路的正文生效。
        在门上再做一次会连换行一起收掉，代价大于收益。"""
        block = pp._format_prompt_block({1: 'The pad spans Grid B2 at 60% of frame height.'}, {})
        self.assertIn('Grid B2', block)


if __name__ == '__main__':
    unittest.main()
