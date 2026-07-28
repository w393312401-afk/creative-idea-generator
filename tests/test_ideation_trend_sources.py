"""激发趋势参考双通道回归测试:
1. _parse_trend_urls:配置中心「激发参考网址」原始输入(换行/逗号/中文分隔符/列表)
   → 去重、补协议、截前 5 个。
2. fetch_custom_url_snippet:抓取+aux 摘要通道的缓存命中、过期重抓、抓取全挂时回退
   旧缓存、无配置时零副作用。
3. _fetch_url_text 的 HTML 剥离(script/style 不能漏进正文)。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp

# 一份合格的「内外双重完工」工序清单，本文件多处复用。
# 2026-07-28 起 dual 门禁按骨架自己的账收紧（外部 >=4 条且落实 >=3 个外部族、其中必须
# 有一条外部设备/平台；过门后 >=6 条；整单 >=11 条），旧样例那种「外部 2 条 + 内部 2 条」
# 的 7~11 条清单不再合格——它把骨架点名的 17 个状态压进 6 个施工拍，每拍近 3 个变化，
# 而每拍的 IMAGE 是从上一帧续写的，一拍改多处画面必飘。见 _DUAL_MIN_OUTLINE_ENTRIES。
DUAL_OK_OUTLINE = [
    '立柱搭建外部框架',          # 0  大结构就位
    '封装外墙木饰面',            # 1  围护/外立面
    '安装双开谷仓门',            # 2  门扇
    '铺设门前碎石车道',          # 3  外部平台
    '挂装太阳能板与风管',        # 4  外部设备
    '完成外部入口门面',          # 5  外部小完工（mini-payoff）
    '推镜穿过门口进入原始室内',  # 6  过门
    '清空室内积渣',              # 7  清运
    '铺设防潮基层',              # 8
    '架设墙顶龙骨',              # 9
    '填充墙顶保温',              # 10
    '封装内衬面板',              # 11
    '点亮灯光,人物入住',         # 12 reward
]


class TestParseTrendUrls(unittest.TestCase):
    def test_empty_and_non_dict(self):
        self.assertEqual(pp._parse_trend_urls({}), [])
        self.assertEqual(pp._parse_trend_urls({'ideationTrendUrls': ''}), [])
        self.assertEqual(pp._parse_trend_urls(None), [])
        self.assertEqual(pp._parse_trend_urls({'ideationTrendUrls': 42}), [])

    def test_newline_and_cjk_separators(self):
        raw = "https://a.com/x\n b.com ，https://a.com/x；c.com/page、d.com"
        urls = pp._parse_trend_urls({'ideationTrendUrls': raw})
        self.assertEqual(urls, [
            'https://a.com/x',
            'https://b.com',
            'https://c.com/page',
            'https://d.com',
        ])

    def test_list_input_and_cap_at_five(self):
        raw = [f'https://site{i}.com' for i in range(8)]
        urls = pp._parse_trend_urls({'ideationTrendUrls': raw})
        self.assertEqual(len(urls), 5)
        self.assertEqual(urls[0], 'https://site0.com')
        self.assertEqual(urls[-1], 'https://site4.com')

    def test_http_scheme_preserved(self):
        urls = pp._parse_trend_urls({'ideationTrendUrls': 'http://legacy.com'})
        self.assertEqual(urls, ['http://legacy.com'])


class TestIdeationSearchParams(unittest.TestCase):
    def test_empty_or_missing_uses_default(self):
        for cfg in ({}, None, {'ideationSearchQuery': ''}, {'ideationSearchQuery': '   '}):
            p = pp._ideation_search_params(cfg)
            self.assertEqual(p['cache_key'], 'ideation_trend_snippet_v2')
            self.assertEqual(p['query'], pp.IDEATION_SEARCH_DEFAULT_QUERY)
            self.assertEqual(p['system_instruction'], pp.IDEATION_SEARCH_DEFAULT_INSTRUCTION)

    def test_custom_query_gets_fingerprinted_key_and_generic_instruction(self):
        p = pp._ideation_search_params({'ideationSearchQuery': ' 树屋改造 爆款 '})
        self.assertEqual(p['query'], '树屋改造 爆款')
        self.assertTrue(p['cache_key'].startswith('ideation_trend_search_'))
        self.assertEqual(p['system_instruction'], pp.IDEATION_SEARCH_CUSTOM_INSTRUCTION)
        # 同词稳定、异词不同键——改词立即生效且互不覆盖
        again = pp._ideation_search_params({'ideationSearchQuery': '树屋改造 爆款'})
        other = pp._ideation_search_params({'ideationSearchQuery': '洞穴改造'})
        self.assertEqual(p['cache_key'], again['cache_key'])
        self.assertNotEqual(p['cache_key'], other['cache_key'])


class TestFetchUrlTextStripping(unittest.TestCase):
    def test_strips_script_style_and_tags(self):
        html = ('<html><head><style>.x{color:red}</style>'
                '<script>alert("evil")</script></head>'
                '<body><h1>Tiny&nbsp;Cabin</h1><p>before &amp; after</p></body></html>')

        class FakeResp:
            def read(self, n):
                return html.encode('utf-8')

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(pp.urllib.request, 'urlopen', return_value=FakeResp()):
            text = pp._fetch_url_text('https://example.com')
        self.assertIn('Tiny', text)
        self.assertIn('before & after', text)
        self.assertNotIn('alert', text)
        self.assertNotIn('color:red', text)
        self.assertNotIn('<', text)


class TestFetchCustomUrlSnippet(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(
            pp, 'SEARCH_SNIPPET_CACHE_PATH',
            os.path.join(self._tmp_dir, 'search_snippet_cache.json'))
        self._path_patch.start()
        self.config = {'ideationTrendUrls': 'https://a.com\nhttps://b.com'}

    def tearDown(self):
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_no_urls_configured_returns_empty_without_fetch(self):
        with patch.object(pp, '_fetch_url_text') as mock_fetch:
            self.assertEqual(pp.fetch_custom_url_snippet({}), '')
        mock_fetch.assert_not_called()

    def test_fetch_summarize_and_cache(self):
        with patch.object(pp, '_fetch_url_text', return_value='page text') as mock_fetch, \
             patch.object(pp, '_chat', return_value='· 要点一\n· 要点二') as mock_chat:
            out1 = pp.fetch_custom_url_snippet(self.config)
            out2 = pp.fetch_custom_url_snippet(self.config)  # 第二次应命中缓存
        self.assertEqual(out1, '· 要点一\n· 要点二')
        self.assertEqual(out2, out1)
        self.assertEqual(mock_fetch.call_count, 2)  # 两个 URL 各抓一次,仅第一轮
        self.assertEqual(mock_chat.call_count, 1)

    def test_url_list_change_invalidates_cache(self):
        with patch.object(pp, '_fetch_url_text', return_value='page text'), \
             patch.object(pp, '_chat', return_value='summary-A'):
            pp.fetch_custom_url_snippet(self.config)
        with patch.object(pp, '_fetch_url_text', return_value='page text'), \
             patch.object(pp, '_chat', return_value='summary-B') as mock_chat:
            out = pp.fetch_custom_url_snippet({'ideationTrendUrls': 'https://c.com'})
        self.assertEqual(out, 'summary-B')
        self.assertEqual(mock_chat.call_count, 1)

    def test_all_fetches_fail_falls_back_to_stale_cache(self):
        with patch.object(pp, '_fetch_url_text', return_value='page text'), \
             patch.object(pp, '_chat', return_value='old summary'):
            pp.fetch_custom_url_snippet(self.config)
        # 让缓存过期,且这次抓取全挂:应回退旧值而不是空串/异常
        cache = pp._load_search_snippet_cache()
        key = next(iter(cache))
        cache[key]['ts'] = 0
        pp._save_search_snippet_cache(cache)
        with patch.object(pp, '_fetch_url_text', return_value=''), \
             patch.object(pp, '_chat') as mock_chat:
            out = pp.fetch_custom_url_snippet(self.config)
        self.assertEqual(out, 'old summary')
        mock_chat.assert_not_called()

    def test_summarize_failure_falls_back_silently(self):
        with patch.object(pp, '_fetch_url_text', return_value='page text'), \
             patch.object(pp, '_chat', side_effect=RuntimeError('gateway down')):
            self.assertEqual(pp.fetch_custom_url_snippet(self.config), '')


class TestRunIdeateReturnShape(unittest.TestCase):
    """run_ideate 返回 {'ideas', 'trend_refs'}:trend_refs 原样透传给前端展示,
    LLM 全挂时兜底列表也必须带上 trend_refs 与新字段(推荐拍数/趋势借鉴)。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._path_patch = patch.object(
            pp, 'SEARCH_SNIPPET_CACHE_PATH',
            os.path.join(self._tmp_dir, 'search_snippet_cache.json'))
        self._path_patch.start()
        # run_ideate 自动通道现在会把联网参考沉淀进 trend_refs.json,
        # 测试必须隔离到临时目录,不能写仓库根的真实案例库
        self._refs_patch = patch.object(
            pp, 'TREND_REFS_PATH',
            os.path.join(self._tmp_dir, 'trend_refs.json'))
        self._refs_patch.start()

    def tearDown(self):
        self._refs_patch.stop()
        self._path_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_success_returns_ideas_and_trend_refs(self):
        idea = {"title": "T", "recommended_beats": 11, "trend_ref": "借鉴A"}
        with patch.object(pp, 'fetch_trend_snippet', return_value='· 趋势要点'), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value='· 网址要点'), \
             patch.object(pp, '_chat', return_value='[' + json.dumps(idea, ensure_ascii=False) + ']'):
            result = pp.run_ideate({'ideationTrendUrls': 'https://a.com'}, count=1)
        self.assertEqual(result['ideas'][0]['title'], 'T')
        sources = [r['source'] for r in result['trend_refs']]
        self.assertEqual(sources, ['web_search', 'custom_urls'])
        self.assertEqual(result['trend_refs'][0]['text'], '· 趋势要点')

    def test_no_snippets_yields_empty_trend_refs(self):
        with patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value='[{"title": "T"}]'):
            result = pp.run_ideate({}, count=1)
        self.assertEqual(result['trend_refs'], [])

    def test_managed_ledger_is_included_in_next_ideation_prompt(self):
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return '[{"title": "新创意", "dna": "vehicle / refuge / brass"}]'

        ledger = [{
            'topic_dna': 'natural / refuge-den / self-material-window',
            'one_line': '蓝冰洞隐居卧室',
            'status': 'candidate',
        }]
        with patch.object(pp, 'read_ledger', return_value=ledger), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            pp.run_ideate({}, count=1)

        self.assertIn('MANAGED CREATIVE LEDGER', captured['system'])
        self.assertIn('natural / refuge-den / self-material-window', captured['system'])
        self.assertIn('蓝冰洞隐居卧室', captured['system'])
        self.assertIn('regardless of workflow status', captured['system'])

    def test_exact_ledger_matches_are_not_returned_even_if_model_repeats_them(self):
        ledger = [{
            'topic_dna': 'natural / refuge / window',
            'one_line': '旧创意',
            'status': 'published',
        }]
        response = json.dumps([
            {'title': '换标题但 DNA 重复', 'dna': 'NATURAL / REFUGE / WINDOW'},
            {'title': '旧创意', 'dna': 'vehicle / cabin / brass'},
            {'title': '真正的新创意', 'dna': 'man-made / loft / skylight'},
        ], ensure_ascii=False)
        with patch.object(pp, 'read_ledger', return_value=ledger), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value=response):
            result = pp.run_ideate({}, count=3)

        self.assertEqual([idea['title'] for idea in result['ideas']], ['真正的新创意'])

    def test_ledger_remix_uses_seed_and_skips_unrelated_trends(self):
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return json.dumps([{
                'title': '谷仓黄铜隐居屋·雨林版',
                'dna': 'grain-silo / rainforest-refuge / rain-chain-wall',
                # 通用骨架门禁（outline_skeleton_violations）对所有骨架生效，
                # 这里的清单必须是结构合法的（≥4 条、末条是 reward 揭示），
                # 否则这张卡会被当成硬失败丢掉，本用例要断言的 remix 行为就无从验证。
                'beat_outline': ['清空锈蚀内壁', '焊补穿孔钢板', '封装内衬木饰面',
                                 '铺装成品木地板', '点亮雨链墙,人物入住'],
            }], ensure_ascii=False)

        seed = {
            'topic_dna': 'grain-silo / refuge / brass',
            'one_line': '谷仓黄铜隐居屋',
            'creative_seed': {'carrier': 'grain silo', 'twist_zh': '黄铜机械夹层'},
        }
        with patch.object(pp, 'read_ledger', return_value=[seed]), \
             patch.object(pp, 'fetch_trend_snippet') as fetch_trend, \
             patch.object(pp, 'fetch_custom_url_snippet') as fetch_urls, \
             patch.object(pp, '_chat', side_effect=fake_chat):
            result = pp.run_ideate({}, count=1, remix_seed=seed)

        self.assertEqual(result['ideas'][0]['title'], '谷仓黄铜隐居屋·雨林版')
        self.assertEqual(result['trend_refs'], [])
        fetch_trend.assert_not_called()
        fetch_urls.assert_not_called()
        self.assertIn('REMIX SEED (PRIMARY CREATIVE SOURCE)', captured['system'])
        self.assertIn('谷仓黄铜隐居屋', captured['system'])
        self.assertIn('ONLY exception', captured['system'])

    def test_llm_failure_falls_back_with_new_fields(self):
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value='· 趋势要点'), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=RuntimeError('down')):
            result = pp.run_ideate({}, count=3)
        self.assertTrue(len(result['ideas']) >= 3)
        for idea in result['ideas']:
            self.assertIn('recommended_beats', idea)
            self.assertTrue(5 <= idea['recommended_beats'] <= 15)
            self.assertIn('trend_ref', idea)
            self.assertIn(idea['pacing_skeleton'], ('linear_milestone', 'dual_payoff'))
            # 兜底列表也要带逐拍工序简介(卡片上的「工序预览」),长度 = 推荐拍数 + 1
            # (末条是 reward 揭示拍),否则 LLM 全挂时卡片会退化成没有工序的空壳
            self.assertEqual(len(idea['beat_outline']), idea['recommended_beats'] + 1)
            self.assertTrue(all(isinstance(s, str) and s.strip() for s in idea['beat_outline']))
            self.assertTrue(all(len(s) <= 16 for s in idea['beat_outline']))
        # 兜底路径也要把已拿到的联网参考带回前端
        self.assertEqual(result['trend_refs'][0]['source'], 'web_search')

    def test_carrier_family_is_gone_and_batch_forbids_repeating_carriers(self):
        """载体家族(natural/man-made/vehicle/fantasy)已取消:prompt 里不再要求这个字段、
        不再按 4 桶轮换,批次多样性改由「同批载体互不重复」承担,DNA 第一槽换成具体载体。"""
        captured = {}

        import json
        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return json.dumps([{
                "title": "T",
                "carrier_slug": "pot",
                "destiny": "gold",
                "twist_family": "paint",
                "recommended_beats": 5,
                "beat_outline": ["1", "2", "3", "4", "5", "6"]
            }])

        with patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, 'load_reference_file', return_value=''), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            pp.run_ideate({}, count=4)

        system = captured['system']
        self.assertNotIn('carrier_family', system)
        self.assertNotIn('Fantasy-grounded', system)          # 旧的 4 桶轮换规则
        self.assertNotIn('carrier-family / destiny', system)   # 旧的 DNA 格式说明
        self.assertIn('BATCH DIVERSITY (no carrier may repeat)', system)
        self.assertIn('carrier-slug / destiny / twist-family', system)
        # 逐拍工序简介的产出契约
        self.assertIn('"beat_outline"', system)
        self.assertIn('recommended_beats + 1', system)

    def test_selected_pacing_skeletons_are_prompted_and_missing_ids_are_balanced(self):
        """GUI 默认同时启用新旧两套；模型漏写归属时后端也要轮询补齐，
        避免四张卡又全部退回原单线节拍。"""
        captured = {}
        payload = json.dumps([
            {'title': 'A', 'dna': 'a / refuge / x', 'beat_outline': [
                '立柱搭建外部框架', '封装外墙木饰面', '铺装成品木地板',
                '布置床铺与软装', '点亮灯光,人物入住',
            ]},
            {'title': 'B', 'dna': 'b / refuge / x', 'beat_outline': list(DUAL_OK_OUTLINE)},
        ], ensure_ascii=False)

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return payload

        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            result = pp.run_ideate({}, count=2,
                                   pacing_skeleton_ids=['linear_milestone', 'dual_payoff'])

        self.assertIn('PACING SKELETON REFERENCES', captured['system'])
        self.assertIn('linear_milestone', captured['system'])
        self.assertIn('dual_payoff', captured['system'])
        self.assertIn('visible, continuous doorway-crossing video', captured['system'])
        self.assertEqual([idea['pacing_skeleton'] for idea in result['ideas']],
                         ['linear_milestone', 'dual_payoff'])

    def test_single_selected_pacing_skeleton_rejects_unselected_model_value(self):
        payload = json.dumps([{
            'title': 'T', 'dna': 't / refuge / x',
            'pacing_skeleton': 'linear_milestone',
            'beat_outline': [
                *DUAL_OK_OUTLINE,
            ],
        }], ensure_ascii=False)
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value=payload):
            result = pp.run_ideate({}, count=1, pacing_skeleton_ids=['dual_payoff'])
        self.assertEqual(result['ideas'][0]['pacing_skeleton'], 'dual_payoff')

    def test_dual_payoff_deterministically_requires_a_visible_crossing_video(self):
        brief = {
            'mode': 'Threshold',
            'threshold_variant': 'coaxial',
            'threshold_elevated': True,
        }
        out = pp.apply_pacing_skeleton_to_brief(brief, 'dual_payoff')
        self.assertEqual(out['mode'], 'Threshold')
        self.assertEqual(out['threshold_variant'], 'coaxial')
        self.assertTrue(out['threshold_elevated'])
        self.assertTrue(out['require_visible_threshold_video'])

        accidental_cut = {'mode': 'Threshold', 'threshold_variant': 'hard_cut'}
        fixed = pp.apply_pacing_skeleton_to_brief(accidental_cut, 'dual_payoff')
        self.assertEqual(fixed['threshold_variant'], 'coaxial')
        self.assertTrue(fixed['require_visible_threshold_video'])

        old = {'mode': 'Standard', 'threshold_variant': 'coaxial'}
        self.assertEqual(pp.apply_pacing_skeleton_to_brief(old.copy(), 'linear_milestone'), old)

    def test_dual_payoff_label_cannot_pass_with_a_linear_outline(self):
        """长度、过门位置、外部族、内部层族全都合格，只是外部幕从来没有"完工"过——
        这样的清单挂 dual_payoff 的牌子仍然是在骗人，必须被 mini-payoff 那条拦下。"""
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                *DUAL_OK_OUTLINE[:5],
                '打磨外墙石缝接口',      # 本该是外部小完工,却没有任何完工语义
                *DUAL_OK_OUTLINE[6:],
            ],
        }
        errors = pp.pacing_skeleton_outline_violations(idea)
        self.assertTrue(errors)
        self.assertTrue(any('completed exterior mini-payoff' in err for err in errors))

    def test_dual_payoff_rejects_hard_cut_even_when_other_structure_is_valid(self):
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                *DUAL_OK_OUTLINE[:6], '硬切原始室内', *DUAL_OK_OUTLINE[7:],
            ],
        }
        errors = pp.pacing_skeleton_outline_violations(idea)
        self.assertTrue(any('forbids a hard cut' in err for err in errors))

    def test_dual_payoff_outline_passes_only_with_both_arcs_and_layered_rebuild(self):
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                *DUAL_OK_OUTLINE,
            ],
        }
        self.assertEqual(pp.pacing_skeleton_outline_violations(idea), [])

    def test_selected_theme_still_locks_one_carrier_for_the_whole_batch(self):
        """GUI 已选定基础主题时仍然锁死同一个载体——这条不受「同批载体互不重复」影响,
        两条规则是互斥分支,不能因为取消家族把主题锁一起弄丢。"""
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return '[{"title": "T"}]'

        with patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            pp.run_ideate({}, count=3, theme='wood_cabin', theme_label='木屋改建')

        self.assertIn('木屋改建', captured['system'])
        self.assertIn('exact same Axis-1 carrier', captured['system'])
        self.assertNotIn('BATCH DIVERSITY (no carrier may repeat)', captured['system'])


class TestBeatOutlineDelivery(unittest.TestCase):
    """卡片上的「🔨 节拍简介」只认 idea.beat_outline:模型少写/写歪这个字段时,
    用户点开只会看到一句"没有节拍简介"。run_ideate 负责把它收干净,并在整批
    都缺失时重试一次,而不是把一批没有节拍简介的卡片直接推给前端。"""

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

    def _run(self, chat_side_effect):
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=chat_side_effect) as mock_chat:
            return pp.run_ideate({}, count=1), mock_chat

    def test_dirty_outline_types_are_normalized_to_string_list(self):
        """数组里混进 null/数字/空白项要被清掉,数字要转成字符串,顺序不能变。"""
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c',
            'beat_outline': ['  清运积渣  ', None, '', 5, '架设墙顶龙骨',
                             '铺装成品地板', '点亮灯带,人物入住'],
        }], ensure_ascii=False)
        result, _ = self._run(lambda *a, **k: payload)
        self.assertEqual(result['ideas'][0]['beat_outline'],
                         ['清运积渣', '5', '架设墙顶龙骨', '铺装成品地板', '点亮灯带,人物入住'])
        # 拍数一律由清单长度派生,模型申报的数字不再有话语权(见 §1.3)
        self.assertEqual(result['ideas'][0]['recommended_beats'], 4)

    def test_outline_returned_as_one_string_is_split_into_beats(self):
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c',
            'beat_outline': '清运积渣\n架设龙骨\n铺装地板\n点亮灯带,人物入住',
        }], ensure_ascii=False)
        result, _ = self._run(lambda *a, **k: payload)
        self.assertEqual(result['ideas'][0]['beat_outline'],
                         ['清运积渣', '架设龙骨', '铺装地板', '点亮灯带,人物入住'])

    def test_batch_with_no_outline_at_all_is_retried(self):
        """整批一条 beat_outline 都没有 = 模型整个忽略了这个字段,重试后用合规的那批。"""
        good = json.dumps([{
            'title': '带简介', 'dna': 'a / b / c',
            'beat_outline': ['清运积渣', '架设墙顶龙骨', '铺装成品地板', '点亮灯带,人物入住'],
        }], ensure_ascii=False)
        responses = ['[{"title": "无简介", "dna": "x / y / z"}]', good]
        result, mock_chat = self._run(lambda *a, **k: responses.pop(0))
        self.assertEqual(mock_chat.call_count, 2)
        self.assertEqual(result['ideas'][0]['title'], '带简介')

    def test_partial_outlines_are_kept_without_burning_a_retry(self):
        """只是个别条目没写:为一条重跑整批不划算,照收即可(前端对这类卡片退回「载入维度」)。"""
        payload = json.dumps([
            {'title': '有', 'dna': 'a / b / c',
             'beat_outline': ['清运积渣', '架设墙顶龙骨', '铺装成品地板', '点亮灯带,人物入住']},
            {'title': '无', 'dna': 'x / y / z'},
        ], ensure_ascii=False)
        result, mock_chat = self._run(lambda *a, **k: payload)
        self.assertEqual(mock_chat.call_count, 1)
        self.assertEqual([len(i['beat_outline']) for i in result['ideas']], [4, 0])

    def test_persistently_missing_outline_still_returns_ideas(self):
        """模型三次都不写:宁可给没有节拍简介的卡片,也不能把整批灵感丢掉(退回静态兜底)。"""
        payload = '[{"title": "无简介", "dna": "x / y / z"}]'
        result, mock_chat = self._run(lambda *a, **k: payload)
        self.assertEqual(mock_chat.call_count, 3)
        self.assertEqual(result['ideas'][0]['title'], '无简介')
        self.assertEqual(result['ideas'][0]['beat_outline'], [])


class TestDualPayoffCrossingDetection(unittest.TestCase):
    """过门拍的识别口径。原来只要一拍里同时出现「进入」和「室内」就算一次过门，
    于是室内工序段里正常的「搬入家具进入室内布置」被算成第二次过门，整批合格的
    卡片一起被否掉——server.log 里刷屏的 "exactly one" 多数是这么来的。"""

    def _errs(self, outline):
        return pp.pacing_skeleton_outline_violations(
            {'pacing_skeleton': 'dual_payoff', 'beat_outline': outline})

    def test_ordinary_interior_beat_is_not_counted_as_a_second_crossing(self):
        outline = [
            '清理谷仓外墙藤蔓', '加固石砌墙体与梁架', '安装双开谷仓门',
            '铺装门前碎石车道', '点亮外墙壁灯完成门面', '推镜穿过谷仓门进入原始仓内',
            '清空仓内朽木与粪土', '浇筑并找平室内地坪', '铺设防潮层与电路管线',
            '架设木龙骨与保温棉', '封装内衬板与饰面', '搬入家具进入室内布置',
            '点亮吊灯,人物入住',
        ]
        self.assertEqual(self._errs(outline), [])

    def test_crossing_without_door_or_camera_cue_still_counts_when_raw_state_named(self):
        outline = [
            '清理石屋周边灌木与碎石', '修补外墙石缝与拱券', '安装实木入户门与五金',
            '铺设入口石板平台', '点亮门廊灯完成外立面', '进入未修的屋内查看',
            '清运屋内塌落瓦砾', '找平夯实室内地基', '铺设防潮膜与管线',
            '架设木龙骨隔墙', '封装松木内衬板', '布置床铺与软装',
            '炉火点亮,人物入住',
        ]
        self.assertEqual(self._errs(outline), [])

    def test_missing_and_duplicated_crossings_are_reported_distinguishably(self):
        # 两份样例都要够长，否则先撞上长度下界、看不到过门计数的判定（见 DUAL_OK_OUTLINE）
        linear = ['清空洞内碎冰与积雪', '凿平起居区冰面地坪', '锚固钢制支撑框架',
                  '喷涂洞壁隔热封闭层', '铺设防潮膜与电路管线', '铺设架空木龙骨地台',
                  '填充羊毛保温层', '封装内衬松木板', '铺装成品木地板',
                  '布置床铺与软装', '点亮灯带,人物入住']
        self.assertIn('found 0', self._errs(linear)[0])

        twice = ['清理外墙藤蔓', '加固砖砌山墙', '安装谷仓木门框',
                 '铺设门前碎石车道', '完成外部入口门面', '推镜过门进入原始仓内',
                 '清空仓内朽木', '再次过门进入原始阁楼', '铺设防潮基层',
                 '架设墙顶龙骨', '封装内衬面板', '布置床铺软装', '点亮灯光入住']
        self.assertIn('found 2', self._errs(twice)[0])

    def test_interior_vocabulary_is_shared_by_detection_and_verification(self):
        """用 仓内 认出过门拍，就不能反过来判它"没落进室内"——两处词表必须是同一份。"""
        # 清单要够长才走得到落点判定这一步，否则先被长度下界挡回、这条断言会变成空转
        outline = ['清理外墙藤蔓', '加固砖砌山墙', '安装谷仓木门框',
                   '铺设门前碎石车道', '点亮壁灯完成外立面', '推镜过门进入原始仓内',
                   '清空仓内朽木', '铺设防潮基层', '架设墙顶龙骨',
                   '填充墙顶保温', '封装内衬面板', '布置床铺软装', '点亮灯光入住']
        self.assertEqual(self._errs(outline), [])


class TestPacingGateDoesNotDiscardPassingCards(unittest.TestCase):
    """节拍验收从「整批连坐」改成「按张处理」。

    旧行为：四张里一张没过 → 整批丢掉重来，三次 150s 调用烧完还是掉进静态兜底，
    而静态兜底又要过台账去重，用久了只剩一两条甚至零条，用户看到的就是「换一批
    灵感」转几分钟然后一句「暂无灵感推荐」。
    """

    GOOD_OUTLINE = list(DUAL_OK_OUTLINE)
    LINEAR_OUTLINE = [
        '清空洞内碎冰与积雪', '凿平起居区冰面地坪', '锚固钢制支撑框架',
        '喷涂洞壁隔热封闭层', '铺设架空木龙骨地台', '填充羊毛保温层',
        '铺装成品木地板', '布置床铺与软装', '点亮灯带,人物入住',
    ]

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

    def _payload(self):
        return json.dumps([
            {'title': '合格卡', 'dna': 'a / refuge / x',
             'pacing_skeleton': 'dual_payoff', 'beat_outline': self.GOOD_OUTLINE},
            {'title': '写成单线的卡', 'dna': 'b / refuge / y',
             'pacing_skeleton': 'dual_payoff', 'beat_outline': self.LINEAR_OUTLINE},
        ], ensure_ascii=False)

    def _run(self, pacing_ids):
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value=self._payload()) as mock_chat:
            return pp.run_ideate({}, count=2, pacing_skeleton_ids=pacing_ids), mock_chat

    def test_failing_card_is_downgraded_not_dropped_when_linear_is_selected(self):
        result, mock_chat = self._run(['linear_milestone', 'dual_payoff'])
        self.assertEqual([i['title'] for i in result['ideas']], ['合格卡', '写成单线的卡'])
        # 标签必须诚实：内容是单线清单，就不能继续挂 dual_payoff 的牌子
        self.assertEqual([i['pacing_skeleton'] for i in result['ideas']],
                         ['dual_payoff', 'linear_milestone'])
        # 已经有卡片过关时最多再补一次，不再把三次机会全烧完
        self.assertEqual(mock_chat.call_count, 2)

    def test_dual_only_selection_drops_the_failing_card_but_keeps_the_rest(self):
        result, mock_chat = self._run(['dual_payoff'])
        self.assertEqual([i['title'] for i in result['ideas']], ['合格卡'])
        self.assertEqual(result['ideas'][0]['pacing_skeleton'], 'dual_payoff')
        self.assertEqual(mock_chat.call_count, 2)

    def test_whole_batch_failing_uses_all_three_attempts_then_still_delivers(self):
        payload = json.dumps([{
            'title': '写成单线的卡', 'dna': 'b / refuge / y',
            'pacing_skeleton': 'dual_payoff', 'beat_outline': self.LINEAR_OUTLINE,
        }], ensure_ascii=False)
        with patch.object(pp, 'read_ledger', return_value=[]), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value=payload) as mock_chat:
            result = pp.run_ideate({}, count=1,
                                   pacing_skeleton_ids=['linear_milestone', 'dual_payoff'])
        self.assertEqual(mock_chat.call_count, 3)
        self.assertEqual([i['title'] for i in result['ideas']], ['写成单线的卡'])
        self.assertEqual(result['ideas'][0]['pacing_skeleton'], 'linear_milestone')

    def test_exhausted_fallback_raises_instead_of_returning_an_empty_batch(self):
        """兜底选题被台账全部认领时，以前静静返回空数组，前端只显示「暂无灵感推荐」,
        分不清是模型挂了还是兜底用完了。现在必须报出可执行的原因。"""
        burned = [
            {'topic_dna': 'glacier-ice-cave / refuge-den / self-material-window'},
            {'topic_dna': 'retired-submarine / micro-home / porthole-lighting'},
            {'topic_dna': 'missile-silo / burrow-dwelling / roof-hatch'},
        ]
        with patch.object(pp, 'read_ledger', return_value=burned), \
             patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', side_effect=RuntimeError('proxy down')):
            with self.assertRaises(RuntimeError) as ctx:
                pp.run_ideate({}, count=3)
        self.assertIn('没有产出任何新卡片', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
