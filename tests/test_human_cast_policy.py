"""活物一律真人（Human Cast Policy，2026-08-30）。

起因：整条链路的词汇表是从微缩沙盘那条线长出来的，管画面里的活人一律叫 figurine。
实测 replica_af8db0d7a95f——原片拍的是真人施工，scene_constants.cast 也老老实实写着
"the lone builder: light-skinned Caucasian man…"，交付的 IMAGE 正文却成了 "The lone
equipment figurine in a royal blue work jacket"，生图模型照着这个词渲，人就成了蜡像。

用户定的口径：**所有通道一律真人，微缩线也不例外**。这一组钉住四件事：
  ① 改写本身：假人措辞 → 真人措辞，尺寸记号（1:24 / 拇指高）原样保留；
  ② 否定句不许被改反（"never reads as a plastic doll" 是对的话）；
  ③ 交付正文的唯一出口（_format_prompt_block → _delivery_scrub）真的在改；
  ④ 场景恒常：识别出来是假人，落地即自动优化成真人形式。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_pipeline as pp
from prompt_pipeline import reference_context
from prompt_pipeline.human_cast import (
    humanize_cast_entry,
    humanize_cast_list,
    humanize_cast_text,
)

_DOLL_WORDS = ('figurine', 'doll', 'mannequin', 'wax figure', 'resin')


class TestHumanizeText(unittest.TestCase):
    def test_the_reported_line_stops_calling_a_worker_a_figurine(self):
        got = humanize_cast_text(
            'The lone equipment figurine in a royal blue work jacket stands beside the chassis.')
        self.assertNotIn('figurine', got.lower())
        self.assertIn('person', got.lower())
        # 衣着不能在改写里丢掉——那是全片复述的外形锁
        self.assertIn('royal blue work jacket', got)

    def test_material_words_are_dropped_and_scale_is_kept(self):
        """尺寸归比例锁，材质归这里。两件事分开：1:24 要留着，cast-resin 要没。"""
        got = humanize_cast_text('Two 1:24 scale cast-resin miniature figurines watch the work.')
        self.assertIn('1:24', got)
        for w in ('resin', 'figurine'):
            self.assertNotIn(w, got.lower())
        self.assertIn('people', got.lower())

    def test_material_adjective_on_a_real_person_noun_is_dropped(self):
        self.assertEqual(humanize_cast_text('the resin man kneels'), 'the man kneels')

    def test_negated_clauses_are_left_alone(self):
        """把 "never reads as a plastic doll" 改成 "never reads as a person"，
        意思正好翻过来——这一条比漏改危险得多。"""
        for text in ('the render never reads as a plastic doll',
                     'no mannequin stiffness in the pose',
                     'skin, not a wax figure'):
            self.assertEqual(humanize_cast_text(text), text)

    def test_ordinary_words_are_not_collateral_damage(self):
        """figure / model 单用满地都是（"a figure in the doorway"、"scale model of
        the house"），碰它们必然误伤场景描述。"""
        text = 'a figure in the doorway beside a scale model of the house'
        self.assertEqual(humanize_cast_text(text), text)
        self.assertEqual(humanize_cast_text('plastic-like sheen on the wall'),
                         'plastic-like sheen on the wall')

    def test_it_is_idempotent(self):
        once = humanize_cast_text('resident miniature figurines migrate across the set')
        self.assertEqual(humanize_cast_text(once), once)

    def test_non_strings_pass_through(self):
        self.assertIsNone(humanize_cast_text(None))
        self.assertEqual(humanize_cast_text(''), '')
        self.assertEqual(humanize_cast_list('not a list'), 'not a list')

    def test_double_spaces_in_untouched_text_are_preserved(self):
        """这个函数挂在交付正文的唯一出口上。没改到活物的正文必须字节不变，
        否则它就顺手兼职了一个没人要求的排版清洗器。"""
        text = 'The pad is poured.  The joists are set.'
        self.assertEqual(humanize_cast_text(text), text)


class TestCastEntries(unittest.TestCase):
    def test_a_fake_person_entry_is_auto_upgraded_to_a_real_human(self):
        got = humanize_cast_entry('1:24 scale cast-resin figurine: blue shirt, brown trousers')
        self.assertIn('real living human', got.lower())
        self.assertIn('1:24', got)
        self.assertNotIn('figurine', got.lower())

    def test_an_entry_that_is_already_a_real_person_is_untouched(self):
        text = ('the lone builder: light-skinned Caucasian man, medium athletic build, '
                'short dark brown hair, trimmed full beard')
        self.assertEqual(humanize_cast_entry(text), text)


class TestDeliveryBoundary(unittest.TestCase):
    """所有正文出去的唯一一道门（_format_prompt_block → _delivery_scrub）。"""

    def test_prompt_block_assembly_humanizes_every_slot(self):
        block = pp._format_prompt_block(
            {1: 'Two cast-resin miniature figurines stand beside the chassis.'},
            {1: 'The couple figurines tilt their heads as the hand withdraws.'},
        )
        low = block.lower()
        for w in _DOLL_WORDS:
            self.assertNotIn(w, low, f'交付正文里不该再出现「{w}」')
        self.assertIn('people', low)

    def test_the_boundary_still_strips_planning_annotations(self):
        """真人化是**加**在这道门上的第三条，不能把原来那两条挤掉。"""
        block = pp._format_prompt_block({1: 'The pad is poured（工序：浇筑）.'}, {})
        self.assertNotIn('（工序：', block)


class TestSceneConstants(unittest.TestCase):
    """场景恒常：识别出来是假人，也要自动优化成真人的形式。"""


    def test_legacy_docs_are_humanized_on_the_way_into_the_prompt(self):
        """本次改动之前存下的任务、以及人在卡点上手改回去的，都得在送进提示词时兜住。"""
        lines = reference_context.scene_constants_lines(
            {'cast': ['wax mannequin builder in blue overalls']})
        self.assertTrue(lines)
        joined = ' '.join(lines).lower()
        self.assertNotIn('mannequin', joined)
        self.assertIn('blue overalls', joined)










if __name__ == '__main__':
    unittest.main()
