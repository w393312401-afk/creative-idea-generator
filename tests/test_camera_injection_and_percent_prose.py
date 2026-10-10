"""交付正文里的三处文本毛病（2026-08-30，第二批产物复盘）。

三条都不是机位解耦引入的，但都是被它**暴露**出来的——`camera_setups` 落地之后，写手开始
逐字照抄机位句，几条一直存在的边界就同时露头了。

  1. `fix_camera_dna` 的 required_markers 分支无条件前置，正文已经开着这句机位时会写两遍；
  2. 机位句里的 Grid 记号在交付前被翻译成散文，紧跟方位词时译重、把句子写碎；
  3. `100 percent` 后面没跟 `of` 时被译成名词短语 `all of it`，插在副词位上句子不通。
"""
import unittest

import prompt_pipeline as pp


class TestTheInteriorBranchDoesNotDoubleTheDeclaration(unittest.TestCase):
    """2026-08-30 实测图 12：整句机位声明在交付正文里出现两遍，中间没有句号，粘成一句
    长句——dedupe_camera_declaration 按句号切句，切不开就删不掉。"""

    DNA = ('static model eye-level diorama shot, fifty millimetre macro lens feel, camera '
           'placed level directly before entrance threshold')
    MARKERS = ('vanishing axis', 'pitch locked')

    def test_a_prompt_already_opening_with_the_dna_is_left_alone(self):
        prompt = f'{self.DNA}. The couple stand on the threshold.'
        out = pp.fix_camera_dna(prompt, self.DNA, required_markers=self.MARKERS)
        self.assertEqual(out.lower().count(self.DNA.lower()), 1)
        self.assertEqual(out, prompt)

    def test_a_prompt_with_a_stale_exterior_line_still_gets_the_interior_dna(self):
        """守卫不能把这条分支存在的理由抵消掉：室内帧带着过期的室外机位句时，
        仍然要注入室内那句。"""
        prompt = 'static wide exterior shot on a tripod. The corridor runs into depth.'
        out = pp.fix_camera_dna(prompt, self.DNA, required_markers=self.MARKERS)
        self.assertTrue(out.startswith(self.DNA))

    def test_the_marker_short_circuit_still_wins(self):
        prompt = 'Camera pitch locked level; the central vanishing axis stays centered.'
        self.assertEqual(
            pp.fix_camera_dna(prompt, self.DNA, required_markers=self.MARKERS), prompt)


class TestFullCoverageReadsAsAnAdverb(unittest.TestCase):
    """'all foundation footing trenches all of it excavated'（图 5/6/8）——
    百分比在副词位上，译成名词短语就把句子插断了。"""

    def test_a_bare_hundred_percent_becomes_fully(self):
        out = pp.scrub_spatial_notation('All foundation footing trenches 100 percent excavated.')
        self.assertNotIn('all of it', out)
        self.assertIn('fully excavated', out)

    def test_hundred_percent_of_something_still_becomes_the_entire(self):
        """带 'of' 的那一支是名词位，原行为不能动。"""
        out = pp.scrub_spatial_notation('100 percent of the floor is tiled.')
        self.assertIn('the entire floor', out)

    def test_partial_coverage_wording_is_untouched(self):
        out = pp.scrub_spatial_notation('The pad is 50 percent cleared.')
        self.assertNotIn('fully', out)


class TestCameraSentencesAreToldToAvoidGridTokens(unittest.TestCase):
    """机位句会被逐字发进每一帧，记号在交付前翻译成散文——紧跟方位词时会译重，
    交付正文里就成了 'pinned firmly across lower at the centre of the frame and
    across the lower centre of the frame'（图 7/9）。"""

    def test_the_spec_forbids_grid_cells_inside_a_camera_sentence(self):
        import inspect
        src = inspect.getsource(pp)
        self.assertIn('NEVER use grid cells such as "Grid B2"', src)

    def test_the_worked_example_no_longer_teaches_grid_notation(self):
        import inspect
        src = inspect.getsource(pp)
        self.assertNotIn('the optical center of Grid B2', src)

    def test_the_scrub_still_translates_a_grid_token_if_one_slips_through(self):
        """规格是劝导，不是硬门——记号真写进来时下游那道翻译仍然要在。"""
        out = pp.scrub_spatial_notation('Optical flow radiates from Grid B2.')
        self.assertNotIn('Grid B2', out)


if __name__ == '__main__':
    unittest.main()
