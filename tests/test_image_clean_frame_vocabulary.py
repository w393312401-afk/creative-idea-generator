# -*- coding: utf-8 -*-
"""净帧人称词表的单一真源与定语豁免（2026-08-22）。

这个仓库里同一份人称词表曾经抄了三份：`_WORKER_AGENT_WORDS`（VIDEO 侧判"有没有施工
主体"）、`fix_image_clean_frame_proactive` 里一份字面量、`check_image_clean_frame` 里
又一份。合成器的着装规则**指定**模型写 "one lone craftsman ..."，三份词表却都没有
craftsman —— VIDEO 侧因此把写了工人的正文误判成幽灵施工（每拍白烧两次回炉），IMAGE
侧则反过来漏判（真出现人也拦不住）。补词只补了一处，另外两处照旧漏，正是"抄三份"
的必然结果。现在三处读同一份，本文件钉住这条不变量。

IMAGE 侧比 VIDEO 侧多一层顾虑：VIDEO 侧多认一个词只会**少报**一条违规，IMAGE 侧多认
一个词会**多报**一条、还会被确定性改写踩坏正文（"Craftsman-style windows" → "equipment-
style windows"）。所以定语用法要豁免，这也一并钉住。
"""
import re
import unittest

import prompt_pipeline as pp


class TestSingleSourceOfTruth(unittest.TestCase):
    def test_every_agent_word_has_a_deterministic_replacement(self):
        """词表里加了词却没配替身 → 那个词判得出违规、却改写不掉，净帧照旧漏人。"""
        missing = [w for w in pp._WORKER_AGENT_WORDS
                   if w not in pp._IMAGE_AGENT_REPLACEMENTS]
        self.assertEqual(missing, [], f'这些词没有净帧替身: {missing}')

    def test_the_composer_mandated_word_is_in_the_table(self):
        """着装规则写死的 craftsman 必须在表里——它是实际产出里出现频率最高的那个词
        （实测 72 条 VIDEO 里 48 条用它），漏了它整张表等于形同虚设。"""
        self.assertIn('craftsman', pp._WORKER_AGENT_WORDS)
        self.assertIn('craftsmen', pp._WORKER_AGENT_WORDS)

    def test_both_image_side_checks_read_the_shared_table(self):
        """随便挑一个只存在于共享表里的词，两道 IMAGE 侧关卡都必须认得。"""
        for word in ('craftsman', 'artisan', 'installer', 'technician', 'tradesman'):
            with self.subTest(word=word):
                sentence = f"One lone {word} kneels beside the finished hearth."
                self.assertTrue(pp.check_image_clean_frame(sentence),
                                f'check_image_clean_frame 没认出 {word}')
                self.assertNotIn(word, pp.fix_image_clean_frame_proactive(sentence).lower(),
                                 f'fix_image_clean_frame_proactive 没改写 {word}')


class TestAttributiveExemption(unittest.TestCase):
    """定语用法不是"画面里有人"。这几种说法在成品室内描述里完全正常。"""

    CASES = (
        'Craftsman-style windows line the south wall.',
        'Artisan-crafted oak shelving spans the alcove.',
        'The crew quarters bunk is freshly made.',
        'Fine craftsmanship shows in the mitred corners.',
        'Artisan plaster covers the chimney breast.',
    )

    def test_attributive_uses_are_not_flagged(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(pp.check_image_clean_frame(text), [])

    def test_attributive_uses_survive_the_proactive_rewrite(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(pp.fix_image_clean_frame_proactive(text), text)


class TestCleanFrameStillWorks(unittest.TestCase):
    def test_negation_sentences_are_still_scrubbed_whole(self):
        for word in ('workers', 'craftsmen'):
            with self.subTest(word=word):
                text = f"The room is clean with no {word} present."
                self.assertEqual(pp.check_image_clean_frame(text), [])
                self.assertEqual(pp.fix_image_clean_frame_proactive(text), 'The room is clean.')

    def test_possessive_form_is_rewritten_without_orphan_apostrophe(self):
        out = pp.fix_image_clean_frame_proactive("A technician's toolbag rests on the bench.")
        self.assertNotIn('technician', out.lower())
        self.assertIn("equipment's", out)

    def test_a_clean_sentence_is_untouched(self):
        text = 'Warm light falls across the finished oak floor.'
        self.assertEqual(pp.check_image_clean_frame(text), [])
        self.assertEqual(pp.fix_image_clean_frame_proactive(text), text)

    def test_allow_occupant_still_disables_the_whole_check(self):
        """人物入住类的最终兑现帧，交付物就是那个人——补词不能把它一起拦掉。"""
        text = 'One person sits reading in the finished window seat.'
        self.assertEqual(pp.check_image_clean_frame(text, allow_occupant=True), [])
        self.assertEqual(pp.fix_image_clean_frame_proactive(text, allow_occupant=True), text)


class TestVideoSideUnaffectedByAttributiveMask(unittest.TestCase):
    """定语豁免只加在 IMAGE 侧。VIDEO 侧的幽灵施工判定必须照旧认 craftsman 为施工主体
    ——那正是这次修复的起点。"""

    ANCHOR = ("Use the provided first frame and last frame as exact composition anchors. "
              "Use IMAGE 6 as the actual first-frame image and IMAGE 7 as the actual "
              "last-frame image; every visible action must interpolate between those two "
              "frame images without inventing a third layout.")

    def test_craftsman_counts_as_a_visible_agent_in_video(self):
        clip = (self.ANCHOR + " One lone craftsman in an olive-drab work t-shirt is fastening "
                "tongue-and-groove panels row by row, tapping each board home with a rubber "
                "mallet until the far wall is fully clad. Near-field sound carries mallet "
                "knocks. continuous construction time-lapse, not real-time footage.")
        self.assertEqual(pp.check_video_process_content(clip), [])


if __name__ == '__main__':
    unittest.main()
