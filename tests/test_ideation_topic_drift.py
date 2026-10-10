"""选题漂移门禁回归测试（把创意拉回「动手改造」）。

2026-08-08 台账复盘暴露的三条漂移，每条都对应本文件的一组用例：

1. 归宿漂移 —— 「独居钟表与精密机械修缮室」这条工作室类选题从 SHELTER-ONLY 底下
   走了出去，因为那条硬约束当时只写在 system prompt 的文字里；
2. twist 漂移 —— 「自材质透光墙」四连、「引水铜管暖榻」三连，全串去重挡不住换后缀
   的同根变体；
3. 载体漂移 —— 最近 15 条里连着 7 条天然岩洞/化石壳体，而「同批载体互不重复」这条
   旧规则挡不住它（两块不同的石头本来就互不重复）。天然壳体身上没有可拆下来再
   利用的旧构件，工序塌成打磨抛光，这才是「脱离 DIY 选题」的机制。

用例全部用台账里真实出现过的字符串，不用编造的样例——门禁要能挡住的正是它们。
"""
import json
import unittest
from unittest.mock import patch

import prompt_pipeline as pp

LINEAR_OUTLINE = [
    {'op': 'clearing', 'text': '清空舱内废弃管线设备'},
    {'op': 'repair', 'text': '拆检并回装黄铜舷窗'},
    {'op': 'framing', 'text': '铺设舱底架空龙骨'},
    {'op': 'flooring', 'text': '铺装舱内软木地板'},
    {'op': 'reward', 'text': '通电亮灯,人物入住'},
]

# 只做打磨抛光的天然壳体清单——申报得出旧构件，工序里查无此事。
POLISH_ONLY_OUTLINE = [
    {'op': 'clearing', 'text': '清运洞内碎石积沙'},
    {'op': 'repair', 'text': '打磨抛光石壁表面'},
    {'op': 'framing', 'text': '架设洞内木龙骨'},
    {'op': 'flooring', 'text': '铺装橡木地板'},
    {'op': 'reward', 'text': '点亮暖灯,人物入住'},
]


def _idea(**kw):
    """一条最小可过关的卡片；用例只覆盖自己要测的字段。"""
    base = {
        'title': '退役潜艇舱改造成离网单人居所',
        'input_str': '做一个退役潜艇舱改造成离网单人居所',
        'carrier': 'retired submarine',
        'destiny': 'off-grid micro-home',
        'twist': 'porthole-lighting',
        'twist_zh': '保留黄铜舷窗作为背光搁板灯',
        'salvage_en': 'original brass portholes stripped out, de-rusted and reinstalled as backlit shelf lights',
        'salvage_zh': '原黄铜舷窗除锈回装成背光搁板灯',
        'dna': 'retired-submarine / micro-home / porthole-lighting',
        'score': 23,
        'beat_outline': [
            {'op': 'clearing', 'text': '清空舱内废弃管线设备'},
            {'op': 'repair', 'text': '拆检并回装黄铜舷窗'},
            {'op': 'reward', 'text': '通电亮灯,人物入住'},
        ],
    }
    base.update(kw)
    return base


class CarrierFamilyTest(unittest.TestCase):
    """载体家族分类：混合名里人造构筑物优先，天然词只是它所在的地貌。"""

    def test_real_ledger_carriers_classify_correctly(self):
        cases = {
            'granite boulder overhang': 'natural',
            'tufa limestone column grotto': 'natural',
            'giant fossil turtle shell': 'natural',
            'cliffside juniper trunk': 'natural',
            'alpine quartz cave': 'natural',
            'sinkhole cliff bowl': 'natural',
            'abandoned coastal stone winch house': 'man-made',
            'hollow bridge pylon': 'man-made',
            'abandoned quarry forge': 'man-made',
            'decommissioned grain silo': 'man-made',
            'artillery pillbox bunker': 'man-made',
            'abandoned flume gatehouse': 'man-made',
            'retired escape capsule': 'vehicle',
            'retired submarine': 'vehicle',
            'shipping container': 'vehicle',
        }
        for carrier, expected in cases.items():
            self.assertEqual(pp.carrier_family({'carrier': carrier}), expected, carrier)

    def test_carrier_missing_falls_back_to_dna_head_not_whole_dna(self):
        # 整条 DNA 丢进词表会被 destiny 里的 burrow 拽进 natural——只读第一段。
        probe = {'carrier': '', 'dna': 'missile-silo / burrow-dwelling / roof-hatch'}
        self.assertEqual(pp.carrier_family(probe), 'man-made')

    def test_unclassifiable_is_unknown_not_natural(self):
        # 宁可漏判不可误判：判不出来时不该被当成天然壳体去占配额。
        self.assertEqual(pp.carrier_family({'carrier': 'xyzzy thing'}), 'unknown')


class BuiltInCarrierBankTest(unittest.TestCase):
    """内置形态矩阵 Axis-1 的载体银行必须能被分类器 100% 认出来。

    认不出来 = unknown = 不占天然配额 = 悄悄放行。词表和矩阵是两份分别维护的清单，
    谁改了另一份不会红——这条用例就是它们之间唯一的锁。首次跑出来抓到 4 处漏判：
    复数 "shipping containers"、"cargo plane tail section"、"oversized seashell"、
    "basalt column cluster"。
    """

    def _axis1_banks(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'skills', 'gemini-veo-restoration-composer', 'references',
                            'idea-engine.md')
        src = io.open(path, encoding='utf-8').read()
        banks = {}
        for tag in ('A. Living / natural', 'B. Abandoned man-made',
                    'C. Vehicles / vessels', 'D. Fantasy-grounded'):
            body = src[src.index('**' + tag):].split('\n')[1]
            banks[tag] = [t.strip() for t in body.split('·') if t.strip()]
        return banks

    def test_every_carrier_in_the_matrix_is_classifiable(self):
        unknown = [(tag, item) for tag, items in self._axis1_banks().items()
                   for item in items if pp.carrier_family({'carrier': item}) == 'unknown']
        self.assertEqual(unknown, [], f'分类器认不出这些内置载体（会静默逃过配额）: {unknown}')

    def test_the_natural_and_fantasy_banks_both_classify_as_natural(self):
        # D 组（晶洞/树桩/蘑菇柄/陨石/琥珀/海螺壳/冰块）在配额上等同天然：
        # 它们同样交不出可拆下来再利用的旧构件。
        banks = self._axis1_banks()
        for tag in ('A. Living / natural', 'D. Fantasy-grounded'):
            for item in banks[tag]:
                self.assertEqual(pp.carrier_family({'carrier': item}), 'natural',
                                 f'{tag} / {item}')


class TwistRootTest(unittest.TestCase):
    def test_suffix_variants_share_one_root(self):
        for slug in ('glass-floor-gears', 'glass-floor-cliff', 'glass-floor-tides',
                     'glass-floor-veins'):
            self.assertEqual(pp.topic_twist_root(slug), 'glass-floor', slug)

    def test_burned_roots_counts_and_skips_placeholders(self):
        ledger = [
            {'topic_dna': 'a / b / self-material-window'},
            {'topic_dna': 'c / d / self-material-glow'},
            {'topic_dna': 'e / f / custom-twist'},
            {'creative_seed': {'twist': 'quartz-backlit-wall'}},
        ]
        roots = pp.burned_twist_roots(ledger)
        self.assertEqual(roots.get('self-material'), 2)
        self.assertEqual(roots.get('quartz-backlit'), 1)
        self.assertNotIn('custom-twist', roots)

    def test_burned_root_is_rejected_even_with_a_brand_new_shell(self):
        burned = {'self-material': 4}
        errs = pp.ideation_twist_root_violations(
            _idea(twist='self-material-diffuser'), burned)
        self.assertTrue(errs)
        self.assertIn('self-material', errs[0])

    def test_fresh_root_passes(self):
        self.assertEqual(
            pp.ideation_twist_root_violations(_idea(), {'self-material': 4}), [])

    def test_placeholder_twist_is_rejected(self):
        self.assertTrue(pp.ideation_twist_root_violations(_idea(twist='custom-twist'), {}))


class ShelterOnlyTest(unittest.TestCase):
    def test_the_workshop_that_actually_shipped_is_now_blocked(self):
        leaked = _idea(
            destiny='horology and precision repair room',
            title='荒野巨型鳞木化石干改造成独居钟表与精密机械修缮室')
        errs = pp.ideation_shelter_violations(leaked)
        self.assertTrue(errs)
        self.assertTrue(any('SHELTER-ONLY' in e for e in errs))

    def test_chinese_title_workshop_is_blocked_even_with_a_clean_english_destiny(self):
        errs = pp.ideation_shelter_violations(_idea(
            destiny='snug winter refuge den', title='废弃粮仓改造成独居陶艺工作室'))
        self.assertTrue(errs)

    def test_destiny_without_a_dwelling_word_fails_the_litmus(self):
        self.assertTrue(pp.ideation_shelter_violations(_idea(destiny='quiet green space')))

    def test_every_real_shelter_destiny_in_the_matrix_passes(self):
        for destiny in ('snug winter refuge den', 'off-grid micro-home',
                        'solitary reading-and-sleeping nook', 'hermit woodland hideout',
                        'weatherproof base-camp shelter', 'subterranean burrow dwelling',
                        'cozy off-grid sleeping pod', 'stormproof survival shelter',
                        'one-room sleeping cabin', 'cliffside sleeping loft'):
            self.assertEqual(pp.ideation_shelter_violations(_idea(destiny=destiny)), [],
                             destiny)


class SalvageGateTest(unittest.TestCase):
    """旧物再生门 —— 这次拉回的核心。"""

    def test_declared_and_performed_passes(self):
        self.assertEqual(pp.ideation_salvage_violations(_idea()), [])

    def test_missing_declaration_is_rejected(self):
        errs = pp.ideation_salvage_violations(_idea(salvage_en='', salvage_zh=''))
        self.assertTrue(errs)
        self.assertIn('salvage_en', errs[0])

    def test_placeholder_declaration_is_rejected(self):
        self.assertTrue(pp.ideation_salvage_violations(
            _idea(salvage_en='n/a', salvage_zh='无')))

    def test_declared_but_never_built_is_rejected(self):
        # 「石灰华洞穴」那一档的典型形态：申报了一句，清单里全是打磨抛光。
        polished_only = _idea(
            carrier='tufa limestone column grotto',
            salvage_zh='洞口旧铁栅栏改成床架',
            salvage_en='old iron grille at the mouth becomes a bed frame',
            beat_outline=[
                {'op': 'clearing', 'text': '清运洞内碎石'},
                {'op': 'repair', 'text': '打磨抛光石壁表面'},
                {'op': 'reward', 'text': '点亮暖灯,人物入住'},
            ])
        errs = pp.ideation_salvage_violations(polished_only)
        self.assertTrue(errs)
        self.assertIn('never happens in beat_outline', errs[0])

    def test_english_salvage_wording_in_en_field_also_counts(self):
        idea = _idea(beat_outline=[
            {'op': 'clearing', 'text': '清空舱内设备'},
            {'op': 'repair', 'text': '安装舷窗灯具',
             'en': 'original brass portholes salvaged and remounted as shelf lights'},
            {'op': 'reward', 'text': '通电亮灯,人物入住'},
        ])
        self.assertEqual(pp.ideation_salvage_violations(idea), [])

    def test_no_outline_only_checks_the_declaration(self):
        self.assertEqual(pp.ideation_salvage_violations(_idea(beat_outline=[])), [])


class FamilyQuotaTest(unittest.TestCase):
    def test_the_seven_stone_run_gets_capped(self):
        # 台账里真实出现过的连续天然壳体，一次交付 6 条。
        batch = [_idea(carrier=c, score=20 + i) for i, c in enumerate([
            'giant fossil turtle shell', 'giant fossilized conch shell',
            'cliffside juniper trunk', 'alpine quartz cave',
            'slot-canyon cliff niche', 'sinkhole cliff bowl'])]
        failures = pp.ideation_family_quota_violations(batch, count=6)
        # 6 条的配额是 ceil(6/3)=2，其余 4 条打回；留下的是分数最高的两条。
        self.assertEqual(len(failures), 4)
        kept = [i for i in range(6) if i not in failures]
        self.assertEqual(sorted(kept), [4, 5])

    def test_a_balanced_batch_passes_untouched(self):
        batch = [_idea(carrier=c) for c in (
            'retired submarine', 'decommissioned grain silo',
            'artillery pillbox bunker', 'alpine quartz cave')]
        self.assertEqual(pp.ideation_family_quota_violations(batch, count=4), {})

    def test_already_delivered_naturals_count_against_the_quota(self):
        # 第一轮已经收了 2 条天然，补批再来 1 条就超额——否则「分批凑石头」照样成立。
        already = [_idea(carrier='alpine quartz cave'), _idea(carrier='sinkhole cliff bowl')]
        batch = [_idea(carrier='cliffside juniper trunk')]
        failures = pp.ideation_family_quota_violations(batch, count=6, already=already)
        self.assertEqual(list(failures), [0])

    def test_unknown_carriers_do_not_consume_the_natural_quota(self):
        batch = [_idea(carrier='xyzzy thing') for _ in range(4)]
        self.assertEqual(pp.ideation_family_quota_violations(batch, count=4), {})


class CarrierFamilyPressureTest(unittest.TestCase):
    def test_pressure_window_reports_the_recent_skew(self):
        rows = [{'creative_seed': {'carrier': 'alpine quartz cave'}} for _ in range(7)]
        rows.append({'creative_seed': {'carrier': 'retired submarine'}})
        counts = pp.carrier_family_pressure(rows)
        self.assertEqual(counts['natural'], 7)
        self.assertEqual(counts['vehicle'], 1)


class CombinedGateTest(unittest.TestCase):
    def test_a_clean_candidate_passes_every_single_card_gate(self):
        self.assertEqual(pp.ideation_topic_violations(_idea(), {'glass-floor': 12}), [])

    def test_the_drifted_card_fails_on_all_three_axes_at_once(self):
        drifted = _idea(
            carrier='monumental petrified lepidodendron trunk',
            destiny='horology and precision repair room',
            title='荒野巨型鳞木化石干改造成独居钟表与精密机械修缮室',
            twist='self-material-diffuser',
            salvage_en='', salvage_zh='')
        errs = pp.ideation_topic_violations(drifted, {'self-material': 4})
        self.assertGreaterEqual(len(errs), 3)


class FallbackIdeasTest(unittest.TestCase):
    def test_shipped_fallbacks_declare_salvage_where_the_shell_allows_it(self):
        # 兜底卡不过门禁（它们是三次失败后的最后一口饭），但人造/载具那两条必须
        # 自己立得住，否则默认交付的就是反面教材。
        for idea in (
            {'salvage_zh': '原黄铜舷窗除锈回装成背光搁板灯',
             'salvage_en': 'original brass portholes stripped out, de-rusted and reinstalled as backlit shelf lights',
             'beat_outline': [{'op': 'repair', 'text': '拆检并回装黄铜舷窗'},
                              {'op': 'reward', 'text': '通电亮灯,人物入住'}]},
            {'salvage_zh': '原发射井滑动舱门机构翻新复用作屋顶天窗',
             'salvage_en': "the silo's original sliding blast-hatch mechanism refurbished and reused as the roof light hatch",
             'beat_outline': [{'op': 'repair', 'text': '翻新屋顶滑动舱门机构'},
                              {'op': 'reward', 'text': '舱门滑开,天光落入'}]},
        ):
            self.assertEqual(pp.ideation_salvage_violations(idea), [])


class TrendBlockRestatesEveryFilterTest(unittest.TestCase):
    """联网参考块拼在 system prompt 的最末尾（最强位置），且自称「首要创意来源」。

    它自己列了一份「以下过滤器仍然严格适用」的清单——那份清单漏掉哪条，模型就会
    合理地以为「跟着热点走时那条不算数」。2026-08-08 新增两条硬约束时它就漏了，
    这条用例把两份清单钉在一起，防止再次分叉。
    """

    def _block(self):
        _refs, block = pp._format_primary_trend_block([
            {'id': 'r1', 'source': 'search', 'label': '爆款载体题材',
             'text': '• 极限载体：废弃集装箱、旧巴士、破败阁楼'}])
        return block

    def test_the_filter_roll_call_names_every_hard_policy(self):
        block = self._block()
        for policy in ('SHELTER-ONLY', 'REALISM-ONLY', 'SALVAGE-AND-REBUILD',
                       'twist ROOTS', 'CARRIER-FAMILY QUOTA', 'ledger dedupe',
                       'cliché blocklist', 'buildability'):
            self.assertIn(policy, block, policy)

    def test_it_says_which_axis_to_borrow_on_when_the_trend_is_a_cave(self):
        # 热点本身是天然壳体时，模型要知道该借它的 twist/材质而不是它的载体，
        # 否则「跟着热点走」和家族配额会正面顶上，白烧一次 150s 重试。
        block = self._block()
        self.assertIn('WHICH AXIS TO BORROW ON', block)
        self.assertIn('family quota', block)

    def test_an_empty_reference_list_still_produces_no_block(self):
        self.assertEqual(pp._format_primary_trend_block([]), ([], ''))


class SourceHierarchyIsStatedToTheModelTest(unittest.TestCase):
    """「联网主导、skill 只做过滤器」这条定位以前只活在代码注释里。

    模型看到的是两个都自称第一的块（idea-engine 说 authoritative、趋势块说 PRIMARY
    CREATIVE SOURCE），没有 tie-break；而 Axis-1 载体银行近 40% 是天然壳体、趋势库
    几乎为零，"照矩阵组合"于是成了一条合规的漂移路径。这两条用例锁住那句 tie-break，
    并且分别锁住有参考 / 没参考两种情形——后者不能连矩阵一起降级，否则断网那批会
    失去唯一的载体来源。
    """

    def _system_prompt(self, *, with_refs):
        clean = json.dumps([dict(_idea(beat_outline=LINEAR_OUTLINE))], ensure_ascii=False)
        refs = [{'id': 'r1', 'source': 'search', 'label': '爆款载体题材',
                 'text': '• 极限载体：废弃集装箱、旧巴士'}] if with_refs else []
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, 'persist_trend_refs', return_value=list(refs)), \
             patch.object(pp, 'load_trend_refs', return_value=list(refs)), \
             patch.object(pp, '_chat', return_value=clean) as chat:
            pp.run_ideate({}, count=1)
        return chat.call_args_list[0][0][1]

    def test_with_references_the_carrier_bank_is_demoted_to_a_fallback_vocabulary(self):
        system = self._system_prompt(with_refs=True)
        self.assertIn('SOURCE HIERARCHY', system)
        self.assertIn('NOT a competing creative source', system)
        self.assertIn('fallback vocabulary, not this batch', system)
        self.assertIn('outranks it on the carrier axis', system)
        # 规约身份仍在——降级的只是载体银行，不是整份 idea-engine。
        self.assertIn('RULEBOOK', system)
        # Axes 2-5 不受影响，否则整份形态矩阵等于作废
        self.assertIn('Axes 2-5', system)

    def test_without_references_the_matrix_is_restored_as_the_primary_source(self):
        system = self._system_prompt(with_refs=False)
        self.assertIn('no trend reference is available this batch', system)
        self.assertIn('primary creative\nsource this time', system)
        # 但两条硬约束不跟着放松
        self.assertIn('CARRIER-FAMILY QUOTA still caps', system)
        self.assertIn('SALVAGE-AND-REBUILD still requires', system)

    def test_exactly_one_hierarchy_block_is_ever_present(self):
        # 只数分隔线标题，不数正文里那句「Read the SOURCE HIERARCHY right after it」引用。
        for with_refs in (True, False):
            system = self._system_prompt(with_refs=with_refs)
            self.assertEqual(system.count('==================== SOURCE HIERARCHY'), 1, with_refs)


class RunIdeateEndToEndTest(unittest.TestCase):
    """整条激发链路：漂移卡被打回、返工意见回喂给模型、干净卡照常交付。"""

    def _run(self, chat_side_effect, count=1, ledger=None, **kw):
        with patch.object(pp, 'read_ledger', return_value=list(ledger or [])), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, 'persist_trend_refs', return_value=[]), \
             patch.object(pp, 'load_trend_refs', return_value=[]), \
             patch.object(pp, '_chat', side_effect=chat_side_effect) as chat:
            return pp.run_ideate({}, count=count, **kw), chat

    def test_a_polish_only_natural_card_is_rejected_then_the_clean_retry_is_delivered(self):
        drifted = json.dumps([dict(
            _idea(carrier='tufa limestone column grotto',
                  title='石灰华凝灰岩洞穴改造成湖畔地热暖阁',
                  dna='tufa-limestone-grotto / refuge-den / geothermal-water',
                  twist='geothermal-water-bench',
                  salvage_zh='洞口旧铁栅栏改成床架',
                  salvage_en='old iron grille becomes a bed frame',
                  beat_outline=POLISH_ONLY_OUTLINE))], ensure_ascii=False)
        clean = json.dumps([dict(_idea(beat_outline=LINEAR_OUTLINE))], ensure_ascii=False)
        result, chat = self._run([drifted, clean])

        titles = [i['title'] for i in result['ideas']]
        self.assertEqual(titles, ['退役潜艇舱改造成离网单人居所'])
        # 返工意见必须把「那道工序根本没发生」原样告诉模型，否则它改不动。
        self.assertGreaterEqual(chat.call_count, 2)
        self.assertIn('never happens in beat_outline', chat.call_args_list[1][0][2])

    def test_the_prompt_carries_the_burned_roots_and_the_family_quota(self):
        ledger = [{'topic_dna': f'x{i} / refuge-den / self-material-window',
                   'creative_seed': {'carrier': 'alpine quartz cave'}} for i in range(7)]
        clean = json.dumps([dict(_idea(beat_outline=LINEAR_OUTLINE))], ensure_ascii=False)
        _result, chat = self._run([clean], count=6, ledger=ledger)
        system = chat.call_args_list[0][0][1]
        self.assertIn('BURNED TWIST ROOTS', system)
        self.assertIn('self-material(7x)', system)
        self.assertIn('CARRIER-FAMILY QUOTA', system)
        self.assertIn('at most 2 of the 6 candidates', system)
        self.assertIn('natural 7', system)   # 最近台账的家族偏斜如实告知
        self.assertIn('SALVAGE-AND-REBUILD POLICY', system)
        self.assertIn('"salvage_zh"', system)

    def test_a_pinned_carrier_suspends_the_family_quota(self):
        # 用户在 GUI 里钉死了天然载体，就不该再拿配额去否决他自己的选择。
        cards = [dict(_idea(carrier='alpine quartz cave',
                            title=f'高山石英晶脉石穴改造成暖阁{i}',
                            dna=f'alpine-quartz-cave{i} / refuge-den / quartz-backlit',
                            twist=f'quartz-backlit-wall-{i}',
                            salvage_zh='旧探矿铁轨截段改装成壁炉支架',
                            salvage_en='old prospecting rail cut down and refitted as a hearth bracket',
                            beat_outline=[
                                {'op': 'clearing', 'text': '清运洞内碎石'},
                                {'op': 'repair', 'text': '旧探矿铁轨除锈改装'},
                                {'op': 'flooring', 'text': '铺装橡木地板'},
                                {'op': 'reward', 'text': '点亮暖灯,人物入住'},
                            ]))
                 for i in range(3)]
        # 钉死单线骨架，免得三张卡被轮询分配到 dual/nested 后栽在它们各自的清单长度门禁上
        # ——这条用例要验的是配额，不是节拍骨架。
        result, _chat = self._run([json.dumps(cards, ensure_ascii=False)],
                                  count=3, theme_label='高山石英晶脉石穴',
                                  pacing_skeleton_ids=['linear_milestone'])
        self.assertEqual(len(result['ideas']), 3)


if __name__ == '__main__':
    unittest.main()
