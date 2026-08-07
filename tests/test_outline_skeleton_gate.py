# -*- coding: utf-8 -*-
"""拍数与施工推进骨架（docs/beat_count_skeleton_plan.md）的单元测试：

- P0-B 通用骨架门禁 outline_skeleton_violations：所有 pacing_skeleton 共用的确定性验收，
  重点是**不误判**——每一条规则误伤一次的代价是 150s 的重试白烧 + 掉进静态兜底列表
- P0-A 拍数区间化：compute_beats_floor 由卡片清单推出施工拍下界，
  _beat_count_is_valid 带上 floor 之后才真的挡得住「重型单塌成 6 拍」
- §4.5 recommended_beats 由 beat_outline 长度派生，两个字段不再各说各话
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp
from prompt_pipeline import (
    _MIN_ADAPTIVE_CONSTRUCTION_BEATS,
    _beat_count_is_valid,
    compute_beats_floor,
    outline_skeleton_violations,
    pacing_skeleton_outline_violations,
)


# 一份结构合格的单线清单：起手清理 → 推进 → 收尾 → reward 揭示，无过门。
LINEAR_OK = [
    '清空洞内碎冰与落石',
    '凿平起居区冰面地坪',
    '锚固钢制支撑框架',
    '铺设架空木龙骨地台',
    '封装内衬木饰面墙',
    '铺装成品木地板',
    '布置床铺与软装',
    '点亮灯带,人物入住',
]

# 一份结构合格的双完工清单：室外 mini-payoff → 过门 → 室内清理 → 分层重建 → reward。
DUAL_OK = [
    '清理洞口积雪与落石',
    '加固外部蓝冰拱口',
    '嵌装气密入口门框',
    '搭建洞口防风门廊',
    '挂装太阳能完成外观',
    '推镜过门进入原始冰洞内部',
    '清空洞内碎冰与积雪',
    '凿平并找平内部基底',
    '铺设龙骨与羊毛保温',
    '封装内衬木饰面墙',
    '布设电路并安装暖炉',
    '布置床铺与羊毛软装',
    '点亮暖灯,人物入住',
]


# 恰好卡在新下界上的双完工清单：外部 4 条 + 过门 1 条 + 过门后 6 条 = 11 条。
# 每一条都只承担一个变化——这正是拍数下界要保住的东西。
DUAL_MIN_OK = [
    '清理洞口积雪与落石',        # 0  室外起手
    '加固外部蓝冰拱口',          # 1  大结构
    '嵌装气密入口门框',          # 2  门扇
    '挂装太阳能完成外立面',      # 3  外部设备 + 小完工
    '推镜过门进入原始冰洞内部',  # 4  过门
    '清空洞内碎冰与积雪',        # 5  清运
    '凿平并找平内部基底',        # 6
    '铺设龙骨与羊毛保温',        # 7
    '封装内衬木饰面墙',          # 8
    '布置床铺与羊毛软装',        # 9
    '点亮暖灯,人物入住',         # 10 reward
]

# 旧门禁（清单 >=7 条、过门只需 idx>=2）能放行的最短双完工卡：外部 3 条、内部 2 条。
# 骨架 summary 点名的 17 个状态压进 6 个施工拍，每拍近 3 个变化——这正是新下界要拦的形态。
DUAL_OLD_MINIMUM = [
    '清理洞口积雪与落石',
    '嵌装气密入口门框',
    '点亮门廊灯完成外立面',
    '推镜过门进入原始冰洞内部',
    '清空洞内碎冰与积雪',
    '铺设龙骨保温与内衬板',
    '点亮暖灯,人物入住',
]

# 长度、过门位置、内部层族都合格，只缺「外部设备/平台」那一拍（无太阳能/通风/平台/护栏）。
DUAL_NO_UTILITY = [
    '清理洞口积雪与落石',
    '加固外部蓝冰拱口',
    '嵌装气密入口门框',
    '抹平洞口外立面接缝',
    '打磨蓝冰外壁完成外观',
    '推镜过门进入原始冰洞内部',
    '清空洞内碎冰与积雪',
    '凿平并找平内部基底',
    '铺设龙骨与羊毛保温',
    '封装内衬木饰面墙',
    '布设电路并安装暖炉',
    '布置床铺与羊毛软装',
    '点亮暖灯,人物入住',
]


def _idea(outline, **extra):
    idea = {'title': 'T', 'pacing_skeleton': 'linear_milestone', 'beat_outline': list(outline)}
    idea.update(extra)
    return idea


class TestOutlineSkeletonGateAcceptsGoodCards(unittest.TestCase):
    """防误判用例。新门禁最危险的失败模式不是漏判，而是把合格卡片整批打回。"""

    def test_valid_linear_outline_passes(self):
        self.assertEqual(outline_skeleton_violations(_idea(LINEAR_OK)), [])

    def test_valid_dual_outline_passes_both_gates(self):
        """新旧两个门禁必须同时放行，否则它们会互相打架、把合格卡片全否掉。"""
        idea = _idea(DUAL_OK, pacing_skeleton='dual_payoff')
        self.assertEqual(outline_skeleton_violations(idea), [])
        self.assertEqual(pacing_skeleton_outline_violations(idea), [])

    def test_shipped_fallback_outlines_all_pass(self):
        """静态兜底那三条选题就是本仓库自带的「合格样例」，一条都不许被新门禁否掉。

        这是防误判最有效的一条用例：词表改窄一点就会在这里炸出来。
        """
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, 'persist_trend_refs', return_value=[]), \
             patch.object(pp, 'load_trend_refs', return_value=[]), \
             patch.object(pp, '_chat', side_effect=RuntimeError('down')):
            result = pp.run_ideate({}, count=3)
        self.assertGreaterEqual(len(result['ideas']), 3)
        for idea in result['ideas']:
            self.assertEqual(outline_skeleton_violations(idea), [],
                             f"兜底选题「{idea['title']}」被通用骨架门禁误判")

    def test_minimum_legal_dual_outline_passes_both_gates(self):
        """恰好卡在新下界上的双完工卡必须放行，否则下界就等于「事实上禁用了这个骨架」。"""
        idea = _idea(DUAL_MIN_OK, pacing_skeleton='dual_payoff')
        self.assertEqual(outline_skeleton_violations(idea), [])
        self.assertEqual(pacing_skeleton_outline_violations(idea), [])

    def test_shipped_dual_fallback_outlines_pass_the_dual_gate(self):
        """三条 dual 兜底清单（13/14/15 条）是本仓库自带的双完工合格样例。

        收紧 dual 门禁最危险的失败模式就是把它们否掉——那意味着线上真实卡片会成批
        重烧 150s 然后掉进静态兜底，而静态兜底自己也不合格。这条是词表/下界的回归锁。
        """
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, 'persist_trend_refs', return_value=[]), \
             patch.object(pp, 'load_trend_refs', return_value=[]), \
             patch.object(pp, '_chat', side_effect=RuntimeError('down')):
            result = pp.run_ideate({}, count=3, pacing_skeleton_ids=['dual_payoff'])
        self.assertGreaterEqual(len(result['ideas']), 3)
        for idea in result['ideas']:
            self.assertEqual(idea['pacing_skeleton'], 'dual_payoff')
            self.assertEqual(pacing_skeleton_outline_violations(idea), [],
                             f"兜底选题「{idea['title']}」被收紧后的双完工门禁误判")
            self.assertEqual(outline_skeleton_violations(idea), [],
                             f"兜底选题「{idea['title']}」被通用骨架门禁误判")

    def test_standard_carrier_without_any_crossing_is_not_a_violation(self):
        """纯外立面/庭院/道路改造本来就没有内外过门：零处过门是合法的。

        这是相对 dual_payoff 门禁（要求恰好一处）的关键放宽。
        """
        errs = outline_skeleton_violations(_idea(LINEAR_OK))
        self.assertEqual(errs, [])

    def test_card_without_outline_is_not_a_structural_violation(self):
        """整条没有 outline 是另一个问题（run_ideate 的 with_outline 分支管），
        不该在结构门禁这里被当成违规打回。"""
        self.assertEqual(outline_skeleton_violations(_idea([])), [])
        self.assertEqual(outline_skeleton_violations({'title': 'T'}), [])
        self.assertEqual(outline_skeleton_violations(None), [])


class TestOutlineSkeletonGateRejectsBrokenCards(unittest.TestCase):

    def _errs(self, outline, **extra):
        return outline_skeleton_violations(_idea(outline, **extra))

    def test_too_few_entries(self):
        errs = self._errs(['清空洞内碎冰', '点亮灯带,人物入住'])
        self.assertTrue(any('at least four entries' in e for e in errs))

    def test_last_entry_must_be_the_reward_reveal(self):
        outline = LINEAR_OK[:-1] + ['继续打磨墙面收边']
        errs = self._errs(outline)
        self.assertTrue(any('must be the reward reveal' in e for e in errs))

    def test_two_crossings_are_rejected(self):
        outline = ['清理外墙藤蔓', '完成外部入口门面', '推镜过门进入原始仓内',
                   '清空仓内朽木', '再次过门进入原始阁楼', '铺设防潮基层',
                   '架设墙顶龙骨', '封装内衬面板', '布置床铺软装', '点亮灯光入住']
        errs = self._errs(outline)
        self.assertTrue(any('more than one doorway-crossing entry' in e for e in errs))

    def test_crossing_at_beat_one_is_now_allowed(self):
        """2026-08-07 起清单一比一还原：合成侧不再要求过门前留两个室外拍
        （_MIN_PRE_THRESHOLD_BEATS 只在没有清单的旧路径生效），过门折进清单里紧贴
        边界那一拍自己的运镜即可——这条曾经的硬性拒绝规则已移除，激发侧不该再打回
        清单第一条就是穿门/室内工作的合法清单（例如用户例子里"清运桥墩内积水与
        泥沙"直接作为第一条）。"""
        outline = ['推镜过门进入原始仓内', '清空仓内朽木', '铺设防潮基层',
                   '封装内衬面板', '点亮灯光入住']
        errs = self._errs(outline)
        self.assertFalse(any('exterior entries' in e for e in errs), errs)

    def test_post_crossing_beat_need_not_be_the_cleanout(self):
        """2026-08-07 起：过门拍自己交付一条真实清单工序，紧接着那一拍具体是什么
        完全由清单顺序决定，不再强制必须是清理——这条曾经的硬性拒绝规则已移除。"""
        outline = ['清理外墙藤蔓', '完成外部入口门面', '推镜过门进入原始仓内',
                   '刷涂墙面饰面涂料', '铺装成品木地板', '布置床铺软装',
                   '点亮灯光,人物入住']
        errs = self._errs(outline)
        self.assertFalse(any('interior cleanout' in e for e in errs), errs)

    def test_weak_milestone_wording_is_rejected(self):
        """schema 明文禁止的那两条：「开始施工」「继续完善」。"""
        for weak in ('开始施工外墙', '继续完善墙面'):
            outline = [weak] + LINEAR_OK[1:]
            errs = self._errs(outline)
            self.assertTrue(any('vague/partial-progress wording' in e for e in errs),
                            f'"{weak}" 未被判为弱里程碑')

    def test_weak_words_inside_a_sentence_are_not_flagged(self):
        """「开始/继续」出现在句中属于正常措辞，只查开头（宁可漏判不可误判）。"""
        outline = ['清空洞内碎冰与落石', '凿平地坪继续到墙脚', '锚固钢制支撑框架',
                   '封装内衬木饰面墙', '点亮灯带,人物入住']
        self.assertEqual(self._errs(outline), [])

    def test_repeated_milestone_is_rejected(self):
        outline = ['清空洞内碎冰与落石', '锚固钢制支撑框架', '锚固钢制支撑框架',
                   '封装内衬木饰面墙', '点亮灯带,人物入住']
        errs = self._errs(outline)
        self.assertTrue(any('repeats a milestone' in e for e in errs))


class TestDualPayoffGateRejectsCompressedCards(unittest.TestCase):
    """「一个节拍包含多重变化」的根因在这里：门禁若允许两幕摊在 6 个施工拍上，
    模型只能把多个工序塞进同一拍，而每拍的 IMAGE 是从上一帧续写的——一拍改多处
    时模型会重排整幅而不是叠加 delta，画面就飘了。"""

    def _errs(self, outline):
        return pacing_skeleton_outline_violations(
            _idea(outline, pacing_skeleton='dual_payoff'))

    def test_old_seven_entry_minimum_is_now_rejected(self):
        errs = self._errs(DUAL_OLD_MINIMUM)
        self.assertTrue(any('at least 11 outline entries' in e for e in errs), errs)

    def test_exterior_arc_without_a_utility_platform_beat_is_rejected(self):
        """太阳能板消失的根因：外部幕此前只检查最后一条的措辞，中间几条一律不查。"""
        errs = self._errs(DUAL_NO_UTILITY)
        self.assertTrue(any('exterior utility/platform beat' in e for e in errs), errs)
        # 只该命中这一条：长度/过门位置/mini-payoff/内部层族都是合格的
        self.assertEqual(len(errs), 1, errs)

    def test_crossing_too_early_leaves_no_room_for_the_exterior_act(self):
        outline = (DUAL_MIN_OK[:2] + DUAL_MIN_OK[4:10]
                   + ['铺装成品木地板', '布设电路并安装暖炉', DUAL_MIN_OK[10]])
        errs = self._errs(outline)
        self.assertTrue(any('at least 4 exterior entries' in e for e in errs), errs)

    def test_interior_rebuild_too_short_is_rejected(self):
        """外部幕合格但内部只剩 5 条：分层重建摊不开，必然一拍多族。"""
        outline = (DUAL_MIN_OK[:3] + ['搭建洞口防风门廊']
                   + DUAL_MIN_OK[3:9] + [DUAL_MIN_OK[10]])
        errs = self._errs(outline)
        self.assertTrue(any('at least 6 entries after the doorway crossing' in e for e in errs), errs)

    def test_non_dual_cards_are_untouched_by_the_dual_gate(self):
        """收紧的是 dual 专属门禁，单线骨架一个字符都不该受影响。"""
        self.assertEqual(pacing_skeleton_outline_violations(_idea(DUAL_OLD_MINIMUM)), [])
        self.assertEqual(pacing_skeleton_outline_violations(_idea(LINEAR_OK)), [])


class TestComputeBeatsFloor(unittest.TestCase):

    def test_heavy_threshold_card_keeps_most_of_its_density(self):
        """13 条清单（12 施工拍 + reward）→ ceil(13.0 * 0.7) = 10。

        2026-07-31：密度下界改由**按族跨度加权后的条数**派生，不再是裸条数
        （docs/pacing_rhythm_balance_plan.md §4.5）。这份清单里
        「铺设龙骨与羊毛保温」「封装内衬木饰面墙」各跨两个材料层族，各记 1.5 条，
        12 条因此加权成 13.0，下界从 9 抬到 10 —— 加权是有区分度的，不是全表 +1：
        只跨一族的条目仍然记 1 条。
        """
        self.assertEqual(compute_beats_floor(_idea(DUAL_OK)), 10)

    def test_light_card_falls_back_to_the_structural_minimum(self):
        """8 条清单（7 施工拍，其中一条跨两族）→ max(2, ceil(7.5*0.7)) = 6。"""
        self.assertEqual(compute_beats_floor(_idea(LINEAR_OK)), 6)

    def test_crossing_forces_four_structural_beats(self):
        """有过门时结构必备 4 拍（室外 x2 + 过门 + 过门后清理），即使清单本身很短。"""
        outline = ['清理外墙藤蔓', '完成外部入口门面', '推镜过门进入原始仓内',
                   '清空仓内朽木', '点亮灯光,人物入住']
        # 密度下界 ceil(4*0.7)=3，被结构必备的 4 顶上去
        self.assertEqual(compute_beats_floor(_idea(outline)), 4)

    def test_dual_payoff_card_gets_the_two_act_structural_floor(self):
        """双完工的结构必备拍是两幕的账（9），不是单线那个 4。

        11 条清单的密度下界只有 ceil(10*0.7)=7；沿用 4 的话 floor=7，ladder 可以
        合法地把两幕之一压没。同一份清单挂在单线骨架上仍是旧行为。
        """
        self.assertEqual(compute_beats_floor(_idea(DUAL_MIN_OK, pacing_skeleton='dual_payoff')), 9)
        # 挂在单线骨架上走密度下界：10 条施工条目里两条跨两族 → 加权 11.0 → ceil(7.7)=8
        self.assertEqual(compute_beats_floor(_idea(DUAL_MIN_OK)), 8)

    def test_dual_density_floor_still_wins_when_it_is_higher(self):
        """15 条的重型双完工单：密度下界 ceil(15.0*0.7)=11 高于结构必备 9，取大。"""
        heavy = DUAL_OK + ['铺装成品木地板', '安装舷窗背光灯具']
        self.assertEqual(compute_beats_floor(_idea(heavy, pacing_skeleton='dual_payoff')), 11)

    def test_missing_or_malformed_outline_falls_back_to_the_global_constant(self):
        self.assertEqual(compute_beats_floor(_idea([])), _MIN_ADAPTIVE_CONSTRUCTION_BEATS)
        self.assertEqual(compute_beats_floor(_idea(['只有一条'])), _MIN_ADAPTIVE_CONSTRUCTION_BEATS)
        self.assertEqual(compute_beats_floor({}), _MIN_ADAPTIVE_CONSTRUCTION_BEATS)
        self.assertEqual(compute_beats_floor(None), _MIN_ADAPTIVE_CONSTRUCTION_BEATS)


class TestBeatCountIsValidHonoursFloor(unittest.TestCase):
    """回归锁：min_total_beats 只影响 prompt 文案与兜底路径，真正裁定 LLM 返回的
    ladder 长度合不合格的是 _beat_count_is_valid。floor 不传进来的话，一份 6 拍
    ladder 在 floor=9 的重型单里照样被判合格——本方案最容易漏掉的一处。"""

    def test_short_ladder_is_rejected_under_a_high_floor(self):
        self.assertFalse(_beat_count_is_valid(8, 13, 'adaptive', floor=9))

    def test_ladder_inside_the_range_is_accepted(self):
        self.assertTrue(_beat_count_is_valid(10, 13, 'adaptive', floor=9))
        self.assertTrue(_beat_count_is_valid(13, 13, 'adaptive', floor=9))

    def test_over_the_cap_is_still_rejected(self):
        self.assertFalse(_beat_count_is_valid(14, 13, 'adaptive', floor=9))

    def test_omitting_floor_reproduces_the_old_behaviour(self):
        """向后兼容锁：老任务/老断点/手动输入主题的路径不受影响。"""
        for candidate in range(1, 16):
            self.assertEqual(
                _beat_count_is_valid(candidate, 13, 'adaptive'),
                _MIN_ADAPTIVE_CONSTRUCTION_BEATS + 1 <= candidate <= 13)

    def test_fixed_mode_ignores_the_floor(self):
        """fixed 语义是「严格按所选拍数」，beats_floor 对它无意义。"""
        self.assertTrue(_beat_count_is_valid(13, 13, 'fixed', floor=9))
        self.assertFalse(_beat_count_is_valid(10, 13, 'fixed', floor=9))
        self.assertFalse(_beat_count_is_valid(10, 13, 'fixed', floor=None))

    def test_garbage_floor_falls_back_instead_of_crashing(self):
        self.assertTrue(_beat_count_is_valid(6, 13, 'adaptive', floor='x'))


class TestRecommendedBeatsIsDerivedFromTheOutline(unittest.TestCase):
    """§1.3 的修法：拍数由清单派生而非独立申报——零重试成本，100% 消除不一致。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'SEARCH_SNIPPET_CACHE_PATH',
                         os.path.join(self._tmp_dir, 'search_snippet_cache.json')),
            patch.object(pp, 'TREND_REFS_PATH', os.path.join(self._tmp_dir, 'trend_refs.json')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _run(self, payload):
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=lambda *a, **k: payload):
            return pp.run_ideate({}, count=1)

    def test_model_declared_count_is_overwritten_by_the_outline_length(self):
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c',
            'recommended_beats': 12,          # 模型报 12
            'beat_outline': LINEAR_OK,        # 清单只有 8 条 = 7 施工拍
        }], ensure_ascii=False)
        idea = self._run(payload)['ideas'][0]
        self.assertEqual(idea['recommended_beats'], len(LINEAR_OK) - 1)

    def test_card_without_outline_keeps_its_declared_count(self):
        """清单为空时不改写，避免把没有 outline 的卡片写成 0 拍。"""
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c', 'recommended_beats': 9,
        }], ensure_ascii=False)
        idea = self._run(payload)['ideas'][0]
        self.assertEqual(idea['recommended_beats'], 9)

    def test_delivered_cards_carry_their_beats_floor(self):
        """前端只透传、不计算：下界必须由后端挂在每条 idea 上（见 §5.3）。"""
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c', 'beat_outline': DUAL_OK,
        }], ensure_ascii=False)
        idea = self._run(payload)['ideas'][0]
        self.assertEqual(idea['beats_floor'], compute_beats_floor(idea))
        self.assertEqual(idea['beats_floor'], 10)


class TestHardFailuresAreDroppedNotDowngraded(unittest.TestCase):
    """降级只能修「标签不诚实」，修不了「结构不成立」：末拍不是 reward 揭示的卡片，
    换成哪个骨架名字都还是在骗人，只能整张丢弃。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'SEARCH_SNIPPET_CACHE_PATH',
                         os.path.join(self._tmp_dir, 'search_snippet_cache.json')),
            patch.object(pp, 'TREND_REFS_PATH', os.path.join(self._tmp_dir, 'trend_refs.json')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_structurally_broken_card_is_dropped_while_the_rest_are_delivered(self):
        payload = json.dumps([
            {'title': '合格', 'dna': 'a / b / c', 'beat_outline': LINEAR_OK},
            {'title': '末拍不是揭示', 'dna': 'x / y / z',
             'beat_outline': LINEAR_OK[:-1] + ['继续打磨墙面收边']},
        ], ensure_ascii=False)
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=lambda *a, **k: payload):
            result = pp.run_ideate({}, count=2,
                                   pacing_skeleton_ids=['linear_milestone'])
        titles = [idea['title'] for idea in result['ideas']]
        self.assertIn('合格', titles)
        self.assertNotIn('末拍不是揭示', titles)


# ── P1-C：结构化 beat_outline {op, text} 的新增测试 ─────────────────────────


# 新形态的结构化清单（与 LINEAR_OK 同构，只是每条变成 {op, text}）
LINEAR_OK_STRUCTURED = [
    {'op': 'clearing',   'text': '清空洞内碎冰与落石'},
    {'op': 'repair',     'text': '凿平起居区冰面地坪'},
    {'op': 'framing',    'text': '锚固钢制支撑框架'},
    {'op': 'framing',    'text': '铺设架空木龙骨地台'},
    {'op': 'drywall',    'text': '封装内衬木饰面墙'},
    {'op': 'flooring',   'text': '铺装成品木地板'},
    {'op': 'furnishing', 'text': '布置床铺与软装'},
    {'op': 'reward',     'text': '点亮灯带,人物入住'},
]

DUAL_OK_STRUCTURED = [
    {'op': 'clearing',   'text': '清理洞口积雪与落石'},
    {'op': 'framing',    'text': '加固外部蓝冰拱口'},
    {'op': 'framing',    'text': '嵌装气密入口门框'},
    {'op': 'furnishing', 'text': '搭建洞口防风门廊'},
    {'op': 'furnishing', 'text': '挂装太阳能完成外观'},
    {'op': 'threshold',  'text': '推镜过门进入原始冰洞内部'},
    {'op': 'clearing',   'text': '清空洞内碎冰与积雪'},
    {'op': 'repair',     'text': '凿平并找平内部基底'},
    {'op': 'framing',    'text': '铺设龙骨与羊毛保温'},
    {'op': 'drywall',    'text': '封装内衬木饰面墙'},
    {'op': 'wiring',     'text': '布设电路并安装暖炉'},
    {'op': 'furnishing', 'text': '布置床铺与羊毛软装'},
    {'op': 'reward',     'text': '点亮暖灯,人物入住'},
]


class TestStructuredOutlinePassesGates(unittest.TestCase):
    """P1-C：{op, text} 结构化清单必须通过所有现有门禁。"""

    def test_structured_linear_passes(self):
        self.assertEqual(outline_skeleton_violations(_idea(LINEAR_OK_STRUCTURED)), [])

    def test_structured_dual_passes_both_gates(self):
        idea = _idea(DUAL_OK_STRUCTURED, pacing_skeleton='dual_payoff')
        self.assertEqual(outline_skeleton_violations(idea), [])
        self.assertEqual(pacing_skeleton_outline_violations(idea), [])

    def test_op_precise_reward_detection(self):
        """op='reward' 直接命中规则 2，不依赖正则。"""
        # 末拍 text 不含任何 reward 正则关键词，但 op='reward' → 放行
        outline = [
            {'op': 'clearing',   'text': '清空洞内碎冰与落石'},
            {'op': 'repair',     'text': '凿平起居区冰面地坪'},
            {'op': 'framing',    'text': '锚固钢制支撑框架'},
            {'op': 'reward',     'text': '最终大收官'},  # text 不含 reward 关键词
        ]
        errs = outline_skeleton_violations(_idea(outline))
        self.assertFalse(any('reward' in e.lower() for e in errs))

    def test_op_precise_threshold_detection(self):
        """op='threshold' 直接命中规则 3，不依赖过门正则。"""
        outline = [
            {'op': 'clearing',   'text': '清理外部积雪'},
            {'op': 'repair',     'text': '加固外壁结构'},
            {'op': 'threshold',  'text': '走进下一个空间'},  # text 不含过门正则
            {'op': 'clearing',   'text': '清空内部碎屑'},
            {'op': 'framing',    'text': '铺设龙骨'},
            {'op': 'furnishing', 'text': '布置家具'},
            {'op': 'reward',     'text': '点亮灯带,人物入住'},
        ]
        errs = outline_skeleton_violations(_idea(outline))
        # 不应报「多于一处过门」或「没有过门」
        self.assertFalse(any('doorway-crossing' in e for e in errs))

    def test_op_precise_clearing_detection(self):
        """op='clearing' 直接命中规则 5（过门后清理），不依赖清理正则。"""
        outline = [
            {'op': 'clearing',   'text': '清理外部积雪'},
            {'op': 'repair',     'text': '加固外壁结构'},
            {'op': 'threshold',  'text': '推镜过门进入原始内部'},
            {'op': 'clearing',   'text': '搬走屋里的杂物'},  # text 不含强清理关键词
            {'op': 'framing',    'text': '铺设龙骨'},
            {'op': 'furnishing', 'text': '布置家具'},
            {'op': 'reward',     'text': '点亮灯带,人物入住'},
        ]
        errs = outline_skeleton_violations(_idea(outline))
        self.assertFalse(any('cleanout' in e for e in errs))

    def test_wrong_reward_op_is_rejected(self):
        """末拍 op 不是 'reward' → 规则 2 报错。"""
        outline = [
            {'op': 'clearing',   'text': '清空碎冰'},
            {'op': 'repair',     'text': '修补地坪'},
            {'op': 'framing',    'text': '锚固框架'},
            {'op': 'furnishing', 'text': '点亮灯带,人物入住'},  # op 错了
        ]
        errs = outline_skeleton_violations(_idea(outline))
        self.assertTrue(any('reward' in e.lower() for e in errs))


class TestInvalidOpIsRejected(unittest.TestCase):
    """P1-C 规则 8：不合法的 op 值触发报错。"""

    def test_invalid_op_is_rejected(self):
        outline = [
            {'op': 'clearing',   'text': '清空碎冰'},
            {'op': 'foobar',     'text': '修补地坪'},
            {'op': 'framing',    'text': '锚固框架'},
            {'op': 'reward',     'text': '点亮灯带,人物入住'},
        ]
        errs = outline_skeleton_violations(_idea(outline))
        self.assertTrue(any('invalid' in e.lower() for e in errs))


class TestOldStringFormatStillPasses(unittest.TestCase):
    """P1-C 向后兼容：旧字符串格式的 beat_outline 仍通过所有门禁。"""

    def test_old_linear_string_format_still_passes(self):
        """旧格式 LINEAR_OK（纯字符串列表）在 _outline_texts fallback 路径上仍然通过。"""
        self.assertEqual(outline_skeleton_violations(_idea(LINEAR_OK)), [])

    def test_old_dual_string_format_still_passes(self):
        idea = _idea(DUAL_OK, pacing_skeleton='dual_payoff')
        self.assertEqual(outline_skeleton_violations(idea), [])
        self.assertEqual(pacing_skeleton_outline_violations(idea), [])

    def test_old_format_compute_beats_floor(self):
        """compute_beats_floor 在旧格式上结果不变。"""
        idea_old = _idea(LINEAR_OK)
        idea_new = _idea(LINEAR_OK_STRUCTURED)
        self.assertEqual(compute_beats_floor(idea_old), compute_beats_floor(idea_new))


class TestMixedFormatIsNormalized(unittest.TestCase):
    """P1-C：混合格式（部分 string、部分 dict）能被正确解析。"""

    def test_mixed_format_outline_texts(self):
        from prompt_pipeline import _outline_texts
        idea = {'beat_outline': [
            '清空碎冰',  # 旧字符串
            {'op': 'repair', 'text': '修补地坪'},  # 新对象
            42,  # 数字（旧形态边界情况）
        ]}
        texts = _outline_texts(idea)
        self.assertEqual(texts, ['清空碎冰', '修补地坪', '42'])

    def test_mixed_format_outline_ops(self):
        from prompt_pipeline import _outline_ops
        idea = {'beat_outline': [
            '清空碎冰',
            {'op': 'repair', 'text': '修补地坪'},
            42,
        ]}
        ops = _outline_ops(idea)
        self.assertEqual(ops, [None, 'repair', None])


class TestFallbackCardsInNewFormatPass(unittest.TestCase):
    """P1-C 回归锁：6 条兜底卡片在新 {op,text} 格式下全部通过门禁。"""

    def test_shipped_fallback_outlines_all_pass_with_op(self):
        """所有 fallback_ideas 的 beat_outline 都通过 outline_skeleton_violations。"""
        import prompt_pipeline as pp
        # 这些卡片在模块内直接定义，用到时才激活——这里直接从它们的位置拿（是 run_ideate
        # 的 fallback 路径里的最终兜底，不在嵌套闭包里，而是模块级 fallback_ideas 列表）。
        # 由于 fallback_ideas 在 run_ideate 内部定义，我们用 _NESTED_TRANSPORT_FALLBACK_IDEAS。
        for card in pp._NESTED_TRANSPORT_FALLBACK_IDEAS:
            idea = {'title': card['title'], 'pacing_skeleton': 'nested_space_payoff',
                    'beat_outline': card['beat_outline']}
            errs = outline_skeleton_violations(idea)
            self.assertEqual(errs, [], f"Fallback card {card['title']} failed: {errs}")


if __name__ == '__main__':
    unittest.main()
