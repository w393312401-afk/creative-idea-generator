"""Shared reference and prompt-context regression coverage."""
import unittest
from prompt_pipeline import reference_context


class TestReferenceContext(unittest.TestCase):
    def test_banned_hits_are_case_insensitive(self):
        hits = reference_context.banned_element_hits(
            'A worker lifts a WELDING TORCH into frame.', ['welding torch', 'excavator'])
        self.assertEqual(hits, ['welding torch'])


    def test_no_banned_elements_means_no_hits(self):
        self.assertEqual(reference_context.banned_element_hits('anything at all', []), [])


    def test_banned_hits_word_boundary_avoids_false_positives(self):
        """防止 oven 误杀 woven、bed 误杀 embedded、car 误杀 carpet 等假阳性。"""
        text = 'Worker arranging woven storage baskets, placing embedded LED fixtures on carpet, stripping tree bark.'
        banned = ['oven', 'bed', 'car', 'bar', 'pot', 'fan']
        self.assertEqual(reference_context.banned_element_hits(text, banned), [])

        # 真实出现完整独立词时必须正常拦截
        real_violating_text = 'Worker installs an oven on the counter and platform bed.'
        self.assertEqual(sorted(reference_context.banned_element_hits(real_violating_text, banned)), ['bed', 'oven'])


    def test_banned_hits_distinguishes_rustic_earthen_oven_from_residential_oven(self):
        """庇护所土工泥炉（domed oven / cob oven）属于手工建造工序，不应被针对现代厨房电器的 oven 误拦。"""
        banned = ['kitchen cabinetry', 'oven', 'refrigerator']
        cob_text = 'craftsman moulds the cohesive dried cob clay structure into a domed oven and ignites firewood'
        self.assertEqual(reference_context.banned_element_hits(cob_text, banned), [])
        appliance_text = 'craftsman installs a stainless steel oven under the counter'
        self.assertEqual(reference_context.banned_element_hits(appliance_text, banned), ['oven'])


    def test_banned_hits_cjk_support(self):
        """中文禁用词支持自然匹配。"""
        text = '在厨房吧台角落安装了微波炉和电磁炉'
        self.assertEqual(reference_context.banned_element_hits(text, ['微波炉', '洗碗机']), ['微波炉'])


    def test_cast_reaches_the_prompt_lines_and_says_never_re_cast(self):
        lines = reference_context.scene_constants_lines({'cast': ['the lone builder: …red tee…']}, '')
        self.assertTrue(any('never re-cast' in x for x in lines))


    def test_the_prompt_lines_carry_the_signature_first(self):
        lines = reference_context.scene_constants_lines(
            {'materials': ['mossy concrete wall'], 'fixtures_in_shot': ['tripod work light']},
            'A mossy concrete bunker in autumn woodland.')
        self.assertTrue(lines[0].startswith('the place itself:'))
        self.assertTrue(any('tripod work light' in x for x in lines))
        self.assertEqual(reference_context.scene_constants_lines({}, ''), [])

