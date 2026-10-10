"""第四种本体模式：先拆后建（2026-08-30）。

2026-08-30 复盘（拆茅屋、原地建两层别墅那一单）：这类片子起点确实站着一个既存物，但它是
**被清掉的对象**，不是要被改造的载体。旧判据写的是「有既存物线索就不算新建」，而这类片子
必然同时命中两组线索，于是 ground_up 分支被一票否决，落到兜底的 existing_restoration。

分类一错，整条链跟着错：carrier_envelope 取了那间待拆茅屋的尺寸，envelope_signature 取了
它的外壳并被要求在**每一张室内帧里逐字复述**（实测把两层别墅的走廊锁成「巴掌大单层破泥屋」），
盲端腔体锁跟着套上（门后既是死胡同黑暗、又是采光走廊）。

这一组钉住两件事：认得出先拆后建；以及**不把修复线抢走**——后者才是这条判据的主要风险，
「拆除锈蚀座椅」「移除腐朽结构件」配上一句「浇筑」在修复线里遍地都是。
"""
import unittest

import prompt_pipeline as pp


def _brief(theme='', carrier='', trauma='', steps=()):
    return {'theme': theme, 'carrier': carrier, 'trauma': trauma,
            'beat_outline': [{'text': t} for t in steps]}


class TestDemolishAndRebuildIsRecognised(unittest.TestCase):

    def test_chinese_demolish_then_rebuild(self):
        brief = _brief('老屋翻新', '旧泥房', '墙体开裂',
                       ('拆除旧屋主体，清理场地', '放线开挖基槽，浇筑基础', '砌筑一层墙体'))
        self.assertEqual(pp.project_origin_mode(brief), 'demolish_and_rebuild')

    def test_english_demolish_then_rebuild(self):
        brief = _brief('rebuild', 'old shack', 'collapsed',
                       ('demolish the old shack and clear the ground',
                        'excavate footing trenches and pour concrete'))
        self.assertEqual(pp.project_origin_mode(brief), 'demolish_and_rebuild')

    def test_the_signal_can_live_only_in_the_beat_outline(self):
        """复刻线的主题是从原片标题来的，「Giant Hand Builds a Dream Luxury Villa」里
        没有「拆」字——拆除与打地基是**工序**，只写在清单条目里。所以判这条模式时要多看
        一眼清单正文，否则整类片子一条都认不出来。"""
        brief = _brief('Giant Hand Builds a Dream Luxury Villa', 'miniature mud hut',
                       'fractured front wall',
                       ('The old hut structure is cleared away, leaving a smooth earth pad.',
                        'The hand finishes trench excavation and aligns the tied rebar mesh.'))
        self.assertEqual(pp.project_origin_mode(brief), 'demolish_and_rebuild')


class TestRestorationIsNotStolen(unittest.TestCase):
    """判据的主要风险不是漏判，是误判——修复线里局部拆除加浇筑太常见。"""

    def test_partial_teardown_in_a_bus_conversion_stays_restoration(self):
        brief = _brief('废弃校车改造成温馨露营车', '废弃校车', '车厢锈蚀、座椅腐烂',
                       ('拆除锈蚀座椅与旧地板', '浇筑找平层', '搭建内部床架'))
        self.assertEqual(pp.project_origin_mode(brief), 'existing_restoration')

    def test_a_bunker_repair_that_pours_concrete_stays_restoration(self):
        brief = _brief('废弃地堡修复', '混凝土地堡', '渗水锈蚀',
                       ('清除积水与残骸', '开挖排水沟并浇筑', '安装门'))
        self.assertEqual(pp.project_origin_mode(brief), 'existing_restoration')

    def test_a_generic_structure_noun_alone_is_a_part_not_the_whole(self):
        """「结构」「structure」单独出现多半指零件。要算「主体被拆」必须带
        旧/老/整个/entire 这类限定词。"""
        zh = _brief('废弃谷仓修复', '木结构谷仓', '木结构腐朽',
                    ('移除腐朽结构件', '浇筑基础加固', '重铺屋面'))
        en = _brief('barn restoration', 'timber barn', 'rot',
                    ('remove the rotten structure members', 'pour a new footing'))
        self.assertEqual(pp.project_origin_mode(zh), 'existing_restoration')
        self.assertEqual(pp.project_origin_mode(en), 'existing_restoration')

    def test_a_pure_ground_up_build_is_untouched(self):
        brief = _brief('从零搭建森林小屋', '林间空地', '', ('开挖基础并浇筑', '立柱上梁'))
        self.assertEqual(pp.project_origin_mode(brief), 'ground_up_build')

    def test_an_explicit_declaration_still_wins(self):
        """老任务、老断点里已经写死的 project_origin 不该被新判据改写。"""
        brief = _brief('x', '', '', ('拆除旧屋主体', '开挖地基浇筑'))
        brief['project_origin'] = 'existing_restoration'
        self.assertEqual(pp.project_origin_mode(brief), 'existing_restoration')

    def test_the_new_mode_is_accepted_when_declared(self):
        brief = _brief('x')
        brief['project_origin'] = 'demolish_and_rebuild'
        self.assertEqual(pp.project_origin_mode(brief), 'demolish_and_rebuild')


class TestTheContractFollowsTheMode(unittest.TestCase):

    def _contract(self, steps):
        brief = _brief('老屋翻新', '旧泥房', '墙体开裂', steps)
        pp.ensure_spatial_contract(brief)
        return brief.get('origin_contract') or {}

    DEMOLISH = ('拆除旧屋主体，清理场地', '放线开挖基槽，浇筑基础')
    RESTORE = ('修补裂缝', '重刷外墙')

    def test_starting_reality_says_the_old_structure_goes_away(self):
        c = self._contract(self.DEMOLISH)
        self.assertEqual(c.get('mode'), 'demolish_and_rebuild')
        self.assertIn('demolished and cleared on camera', c.get('starting_reality', ''))

    def test_the_no_switch_rule_is_replaced_by_a_one_switch_rule(self):
        """通用那句「不许中途换前提」对这条线是错的：它的前提本来就换一次，而且换在
        明面上。照抄会要求把拆掉的旧壳一路守到末帧。"""
        c = self._contract(self.DEMOLISH)
        self.assertIn('switches exactly once', c.get('rule', ''))
        self.assertIn('never restate the demolished structure', c.get('rule', '').lower())

    def test_other_modes_keep_the_original_no_switch_rule(self):
        c = self._contract(self.RESTORE)
        self.assertNotEqual(c.get('mode'), 'demolish_and_rebuild')
        self.assertIn('never switch between', c.get('rule', ''))


if __name__ == '__main__':
    unittest.main()
