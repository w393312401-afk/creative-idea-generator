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
from prompt_pipeline import reverse
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

    def test_attach_scene_constants_humanizes_the_cast_column(self):
        doc = {'cast_identity': ['1:24 scale resin figurine couple: faded shirts']}
        constants = reverse.attach_scene_constants(doc, {})
        cast = constants.get('cast') or []
        self.assertTrue(cast)
        self.assertNotIn('figurine', cast[0].lower())
        self.assertIn('real living human', cast[0].lower())
        # 两处必须同步：合成器读的是 cast_identity，卡片读的是 scene_constants.cast
        self.assertNotIn('figurine', doc['cast_identity'][0].lower())

    def test_legacy_docs_are_humanized_on_the_way_into_the_prompt(self):
        """本次改动之前存下的任务、以及人在卡点上手改回去的，都得在送进提示词时兜住。"""
        lines = reverse.scene_constants_lines(
            {'cast': ['wax mannequin builder in blue overalls']})
        self.assertTrue(lines)
        joined = ' '.join(lines).lower()
        self.assertNotIn('mannequin', joined)
        self.assertIn('blue overalls', joined)


class TestScaleLockStillLocksSize(unittest.TestCase):
    """尺寸与材质是两件事：比例锁照旧锁尺寸，只是不再管人叫人偶。"""

    def test_clause_keeps_the_ratio_and_declares_real_people(self):
        from prompt_pipeline import observed_grounding as og
        clause = og.cast_scale_clause({'cast_scale': '1:24 scale — each standing person is about 7.1cm tall'})
        self.assertIn('1:24', clause)
        self.assertIn('7.1cm', clause)
        self.assertIn('real living people', clause.lower())
        self.assertIn('the same physical size in every frame', clause.lower())

    def test_the_old_figurine_clause_is_still_recognised_for_dedupe(self):
        """老任务正文里写的是改动之前那句 figurine 版。只认新记号的话，重跑一次会在
        同一段里叠出两句互相矛盾的比例声明。"""
        from prompt_pipeline import observed_grounding as og
        old = ('The site is cleared. Figurine scale lock: 1:24 scale — each standing figurine '
               'is about 7.1cm tall, the same physical size in every frame.')
        self.assertNotIn('scale lock', og._strip_cast_scale(old).lower())


class TestMiniatureRouting(unittest.TestCase):
    """走不走微缩沙盘那套系统提示词——「还有假人」的真正源头。

    2026-08-30 第二轮实测（replica_af8db0d7a95f）：措辞层已经全部真人化，交付正文里
    figurine 归零，人却还是模型——因为这一单**整条通道**走错了。旧判据
    `'craftsman' in cast_identity` 把「Caucasian male builder/craftsman in his 30s…」
    当成了"巨手工匠"的证据，于是一单真人实拍的半挂车改造拿到了微缩沙盘的系统提示词：
    39 处 miniature、33 处 giant hands、8 处 diorama，施工者成了拇指高的模型人。
    """

    def _doc(self, **kw):
        doc = {'carrier': '半挂车底盘', 'scene_signature': '', 'cast_identity': []}
        doc.update(kw)
        return doc

    def test_the_word_craftsman_no_longer_routes_a_real_build_into_miniature(self):
        doc = self._doc(cast_identity=[
            'Caucasian male builder/craftsman in his 30s with short trimmed beard, '
            'wearing a vibrant royal blue button-up work jacket'])
        is_mini, why = reverse.detect_miniature_scale(
            doc, title='平板半挂车底盘改造全能奢华移动挂车住宅')
        self.assertFalse(is_mini, f'职业词不是微缩证据，实际判据：{why}')

    def test_real_miniature_evidence_still_routes_to_the_miniature_lane(self):
        cases = [
            ('题材词在场景一句话里',
             self._doc(scene_signature='A miniature woodland clearing beside two figurines.')),
            ('识别项里的人偶',
             self._doc(cast_identity=['the male figurine: dark-skinned Black man, slim build'])),
            ('识别项里的比例记号',
             self._doc(cast_identity=['1:24 scale resident couple, roughly a thumb tall'])),
            ('载体名里的微缩',
             self._doc(carrier='微缩沙盘泥屋')),
        ]
        for label, doc in cases:
            with self.subTest(label):
                is_mini, why = reverse.detect_miniature_scale(doc, title='x')
                self.assertTrue(is_mini, f'{label} 应当判为微缩，实际：{why}')

    def test_an_explicit_profile_still_wins(self):
        is_mini, _ = reverse.detect_miniature_scale(
            self._doc(), title='x', config={'skillProfile': 'miniature'})
        self.assertTrue(is_mini)

    def test_the_verdict_is_frozen_on_the_doc_before_the_cast_is_humanized(self):
        """真人化会把 figurine 换成真人措辞，而 figurine 正是判微缩的证据之一。
        先归一再判就是把证据擦掉再断案——判定必须钉在 doc 上，只做一次。"""
        doc = {'cast_identity': ['the male figurine: dark-skinned Black man, slim build'],
               'carrier': '树桩', 'scene_signature': ''}
        reverse.attach_scene_constants(doc, {})
        self.assertEqual(doc.get('render_scale'), 'miniature')
        # 识别项确实被真人化了（figurine 没了），但档已经定死，不会跟着翻
        self.assertNotIn('figurine', doc['cast_identity'][0].lower())
        is_mini, why = reverse.detect_miniature_scale(doc)
        self.assertTrue(is_mini, why)

    def test_a_full_scale_job_is_frozen_as_full(self):
        doc = {'cast_identity': ['the lone builder/craftsman: Caucasian man'],
               'carrier': '半挂车底盘', 'scene_signature': ''}
        reverse.attach_scene_constants(doc, {})
        self.assertEqual(doc.get('render_scale'), 'full')


class TestMiniatureLeakSentinel(unittest.TestCase):
    """路由修好了，还要有个哨兵：判据万一再漏，不能又是静默的。"""

    def _run(self, block, beats):
        import replica_pipeline as rp
        state = {'prompt_block': block}
        msgs = []
        rp._warn_if_miniature_wording_leaked(
            state, beats, on_progress=lambda evt, data: msgs.append(data.get('message') or ''))
        return state, msgs

    def test_it_shouts_when_a_full_scale_job_carries_miniature_wording(self):
        block = ('图片提示词\n图片 1:\nA vertical macro diorama photograph. '
                 'The giant hand lowers a beam while the miniature builder watches.')
        state, msgs = self._run(block, {'render_scale': 'full'})
        self.assertTrue(msgs, '走错道必须出声——这次误判烧掉一整单就是因为它全程没有声音')
        self.assertIn('miniature', (state.get('miniature_wording_leak') or {}).get('hits', {}))

    def test_it_stays_quiet_on_a_genuine_miniature_job(self):
        block = 'A macro diorama photograph; the giant hand places a micro-tool.'
        _state, msgs = self._run(block, {'render_scale': 'miniature'})
        self.assertEqual(msgs, [], '微缩单里这些词本来就该有')


class TestExplicitProfileBeatsTheFrozenVerdict(unittest.TestCase):
    """定档是为了让**自动**判定稳定，不是为了压住用户的明确指定。

    freeze_render_scale 把题材判定钉在任务上（识别项被改写、被人编辑都不该让同一单
    换通道）。但显式 skillProfile=miniature 是当次的明确指定——它要是被一个早先自动
    定下的 'full' 压过去，就又是一次「设置无效」。
    """

    def test_an_explicit_miniature_choice_overrides_a_frozen_full_verdict(self):
        doc = {'render_scale': 'full', 'cast_identity': ['a lone builder'], 'carrier': '半挂车'}
        is_mini, why = reverse.detect_miniature_scale(doc, config={'skillProfile': 'miniature'})
        self.assertTrue(is_mini, why)
        self.assertIn('显式', why)

    def test_without_an_explicit_choice_the_frozen_verdict_still_rules(self):
        doc = {'render_scale': 'full', 'cast_identity': ['1:24 scale figurine couple']}
        is_mini, why = reverse.detect_miniature_scale(doc, config={'skillProfile': 'auto'})
        self.assertFalse(is_mini, f'已定档就不该被识别项措辞翻案：{why}')

    def test_the_sentinel_keeps_quiet_when_the_user_asked_for_miniature(self):
        """用户自己把链路钉成微缩，正文里的微缩措辞是他要的，不是走错了道。"""
        import replica_pipeline as rp
        state = {'prompt_block': 'A macro diorama shot; the giant hand places a micro-tool.'}
        msgs = []
        rp._warn_if_miniature_wording_leaked(
            state, {'render_scale': 'full'}, config={'skillProfile': 'miniature'},
            on_progress=lambda evt, data: msgs.append(data.get('message') or ''))
        self.assertEqual(msgs, [])


class TestFastLaneAnnouncesWhatItCannotDo(unittest.TestCase):
    """极速直通通道做不到的设置，必须当场说出口。

    「提示词链路」选 Omni 多镜头组接，而极速通道只有两份系统提示词（微缩 / 真实尺度），
    两份写的都是单镜头正文——多镜头切点语法只有深度通道按 active_skill_profile 分派
    OmniComposer 才有。不说的话，用户看到的是"我明明选了多镜头"，而交付的每一条 VIDEO
    都是一镜到底，且没有任何地方能看出为什么。
    """

    def _notes(self, config, is_miniature=False):
        from prompt_pipeline.fast_composer import announce_fast_lane_limits
        msgs = []
        announce_fast_lane_limits(
            config, is_miniature,
            on_progress=lambda evt, data: msgs.append(data.get('message') or ''))
        return msgs

    def test_choosing_omni_on_the_fast_lane_is_announced(self):
        for cfg in ({'skillProfile': 'omni'},
                    {'skillProfile': 'auto', 'videoModel': 'Omni Flash'}):
            with self.subTest(str(cfg)):
                notes = self._notes(cfg)
                self.assertTrue(notes, '选了 omni 却走极速通道必须出声')
                joined = ' '.join(notes)
                self.assertIn('单镜头', joined)
                self.assertIn('深度合成', joined, '必须说清楚怎么才能拿到多镜头')

    def test_a_veo_job_on_the_fast_lane_needs_no_warning(self):
        """base 档要的就是单镜延时，极速通道产的正是它——没有落差就别制造噪音。"""
        self.assertEqual(self._notes({'skillProfile': 'auto', 'videoModel': 'Veo 3.1'}), [])

    def test_an_explicit_choice_gets_told_how_topic_and_grammar_differ(self):
        """题材由原片证据定，链路只管视频语法。显式钉了链路却拿到微缩口径时，
        要说清这两件事不是一回事，而不是让人以为设置被无视了。"""
        notes = self._notes({'skillProfile': 'base', 'videoModel': 'Veo 3.1'}, is_miniature=True)
        self.assertTrue(notes)
        self.assertIn('微缩', ' '.join(notes))

    def test_auto_never_produces_the_topic_note(self):
        """没显式表过态就不该收到"你选的和实际不同"这类话——他没选过。"""
        notes = self._notes({'skillProfile': 'auto', 'videoModel': 'Veo 3.1'}, is_miniature=True)
        self.assertEqual(notes, [])


if __name__ == '__main__':
    unittest.main()
