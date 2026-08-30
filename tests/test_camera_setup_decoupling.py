"""机位与几何锁的解耦（2026-08-30）。

一句 camera_dna 原先同时背着两件事：跨帧必须逐字复述的**几何锁**（焦段/机高/消失轴/
地平线钉位），和**拍摄风格**（static tripod / locked eye-level）。只有前者需要逐字，
后者被一起锁死，于是全序列每一张图都是同一个平视三脚架。

这一组盯的是解耦之后四条通路各自的边界：
  · 景别进机位分组键（同角度推近 = 换了机位）；
  · camera_palette 通电，但只认完整机位句、不认短标签；
  · fix_camera_dna 的嗅探认得出非三脚架写法，不再重复前置；
  · dedupe_camera_declaration 的词表放宽后，不能开始误删有实质内容的句子。
"""
import unittest

import prompt_pipeline as pp


def _entry(**kw):
    # _outline_normalized_entries 按 'text' 过滤：没有正文的条目整条丢弃。
    idx = kw.pop('index', 1)
    base = {'text': f'beat {idx}', 'space': 'primary', 'camera_angle': 'eye_level',
            'camera_bearing': 'front', 'lens_feel': 'wide'}
    base.update(kw)
    return base


def _brief(entries):
    return {'beat_outline': entries}


class TestShotScaleOpensItsOwnSetup(unittest.TestCase):
    """同一个角度上推近了，在原片上就是换了一次机位。"""

    def test_same_angle_different_scale_splits_into_two_setups(self):
        setups = pp.observed_camera_setups(_brief([
            _entry(index=1, shot_scale='wide'),
            _entry(index=2, shot_scale='close'),
            _entry(index=3, shot_scale='wide'),
        ]))
        self.assertEqual([s['id'] for s in setups], ['SETUP_1', 'SETUP_2'])
        self.assertEqual(setups[0]['beats'], [1, 3])
        self.assertEqual(setups[1]['beats'], [2])
        self.assertEqual(setups[0]['scale'], 'wide')
        self.assertEqual(setups[1]['scale'], 'close')

    def test_the_scale_reaches_the_prose_the_packet_call_copies(self):
        setups = pp.observed_camera_setups(_brief([_entry(shot_scale='extreme_close')]))
        prose = pp.observed_camera_setup_prose(setups[0])
        self.assertIn(pp._SCALE_PROSE['extreme_close'], prose)

    def test_scale_outside_the_closed_set_opens_nothing(self):
        """写不进闭集的拼写变体一律当没读到——否则每个变体都凭空多一个机位。"""
        setups = pp.observed_camera_setups(_brief([
            _entry(index=1, shot_scale='wide'),
            _entry(index=2, shot_scale='WIDE SHOT'),
        ]))
        self.assertEqual(len(setups), 2, '闭集外的读数应归一为空串，与「没读到」同组')
        self.assertEqual(setups[1]['scale'], '')

    def test_scale_alone_never_opens_a_setup(self):
        """机位句写的是「机器站在哪」；只有景别没有机位读数，不算一台机器。"""
        setups = pp.observed_camera_setups(_brief([
            {'text': 'beat 1', 'space': 'primary', 'shot_scale': 'close'},
        ]))
        self.assertEqual(setups, [])


class TestCameraPaletteOnlyAcceptsRealSentences(unittest.TestCase):
    """palette 里装短标签时整段不生效——标签没有几何锁，顶替族级机位句比不换更坏。"""

    FULL = ('static low-angle shot, 20mm lens feel, camera height 0.4m looking up about '
            '20 degrees; horizon line pinned low at one-fifth frame height')

    def test_a_bare_family_label_is_refused(self):
        self.assertEqual(pp._palette_camera_sentence('entrance detail'), '')
        self.assertEqual(pp._palette_camera_sentence('24mm wide 1.3m chest-level'), '')

    def test_a_full_camera_sentence_is_accepted(self):
        self.assertEqual(pp._palette_camera_sentence(self.FULL), self.FULL)

    def test_label_palette_falls_back_to_the_family_dna(self):
        packet = {'camera_dna': 'FAMILY DNA',
                  'camera_palette': ['entrance detail', 'shaft axis', 'far-wall reverse']}
        self.assertEqual(
            pp.select_camera_dna({'index': 1}, 'FAMILY DNA', packet=packet, family='exterior'),
            'FAMILY DNA')

    def test_sentence_palette_is_used(self):
        packet = {'camera_dna': 'FAMILY DNA', 'camera_palette': {'default': self.FULL}}
        self.assertEqual(
            pp.select_camera_dna({'index': 1}, 'FAMILY DNA', packet=packet, family='exterior'),
            self.FULL)

    def test_the_observed_setup_still_outranks_the_palette(self):
        """复刻线量到的机位是硬读数，排在原创线的族轮转之前。"""
        packet = {'camera_dna': 'FAMILY DNA',
                  'camera_setups': {'SETUP_1': 'MEASURED SETUP SENTENCE'},
                  'camera_palette': {'default': self.FULL}}
        self.assertEqual(
            pp.select_camera_dna({'index': 1, 'camera_setup_id': 'SETUP_1'}, 'FAMILY DNA',
                                 packet=packet, family='exterior'),
            'MEASURED SETUP SENTENCE')


class TestFixCameraDnaAcceptsNonTripodWritings(unittest.TestCase):
    """嗅探原先只认 tripod / lens feel / camera height，非三脚架写法一律被判「没写过」
    再前置一次族级样板句——锁写了、注入不进去、校验器还报绿的老形状。"""

    DNA = ('static steeply overhead shot, 28mm lens feel, camera 6m above the ground plane '
           'looking down about 70 degrees')

    def test_an_overhead_declaration_already_present_is_left_alone(self):
        prompt = ('A steeply overhead camera looks down about 70 degrees onto the footprint; '
                  'the ground plane fills the frame. Workers set the first course.')
        self.assertEqual(pp.fix_camera_dna(prompt, self.DNA), prompt)

    def test_a_prompt_with_no_camera_line_still_gets_the_dna(self):
        prompt = 'Workers set the first course of blocks along the marked line.'
        self.assertTrue(pp.fix_camera_dna(prompt, self.DNA).startswith(self.DNA))


class TestDedupeStillSparesSubstantiveSentences(unittest.TestCase):
    """_CAMERA_RESTATEMENT_TOKENS 从 17 词扩到 39 词、正则从 {4,} 放宽到 {3,} 之后，
    dedupe 判「这句是相机复述、整句删掉」的口子变大了。含实质内容的句子必须活下来——
    删掉一句工序描述，产物上看不出是被谁吃的。"""

    DNA = ('static low-angle shot, 20mm lens feel, camera height 0.4m; horizon line pinned '
           'low at one-fifth frame height')

    def test_a_work_sentence_carrying_camera_words_survives(self):
        prompt = (self.DNA + '. The worker sets a low ground rail against the wall and shims '
                  'it level, then checks the height of the second course by eye.')
        kept = pp.dedupe_camera_declaration(prompt, self.DNA)
        self.assertIn('shims it level', kept)
        self.assertIn('second course', kept)

    def test_an_actual_restatement_is_still_removed(self):
        prompt = (self.DNA + '. The camera frames the site in a static low-angle shot, twenty '
                  'millimeter lens feel, camera height zero point four meters, horizon pinned '
                  'low. The worker sets the first course.')
        kept = pp.dedupe_camera_declaration(prompt, self.DNA)
        self.assertEqual(kept.count('camera height'), 1)
        self.assertIn('The worker sets the first course', kept)


class TestBeatContractNoLongerHardcodesStatic(unittest.TestCase):
    """契约句里的 exact 管的是几何锁，不是「所有图都得是同一个平视三脚架」。"""

    LADDER = [{'index': i, 'operation': 'work', 'description': 'd'} for i in range(1, 4)]
    PACKET = {
        'camera_dna': ('static low-angle shot, 20mm lens feel, camera height 0.4m looking up; '
                       'horizon line pinned low at one-fifth frame height'),
        'primary_landmarks': [{'name': 'a', 'grid': 'A1', 'z_depth_scale': '10%'}],
        'frame_boundaries': {'left': 'A1', 'right': 'A3', 'top': 'A2', 'bottom': 'C2'},
        'geometry_lock': 'x',
    }

    def _contract(self):
        return pp._beat_contract(1, 3, self.LADDER, 'Theme', self.PACKET, '')['family_contract']

    def test_the_static_adjective_is_gone_but_the_verbatim_lock_stays(self):
        text = self._contract()
        self.assertNotIn('exact static camera declaration', text)
        self.assertIn('must OPEN with this camera declaration, word for word', text)
        self.assertIn(self.PACKET['camera_dna'], text)

    def test_it_forbids_normalising_back_to_a_house_default(self):
        text = self._contract()
        self.assertIn("THIS beat's own camera setup", text)
        self.assertIn('never normalise it back to a level', text)

    def test_it_still_bans_camera_move_language_in_a_still(self):
        """静帧写运镜词会被 fix_camera_contradictions 整句删掉，删完一句机位都不剩。"""
        self.assertIn('add no camera-move language', self._contract())


if __name__ == '__main__':
    unittest.main()
