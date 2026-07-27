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
                'beat_outline': ['清空锈蚀内壁', '点亮雨链墙'],
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
            {'title': 'A', 'dna': 'a / refuge / x', 'beat_outline': ['立柱', '亮灯']},
            {'title': 'B', 'dna': 'b / refuge / x', 'beat_outline': [
                '立柱搭建外部框架', '完成外部入口门面', '推镜穿过门口进入原始室内',
                '清空室内积渣', '铺设防潮基层', '架设墙顶龙骨',
                '填充墙顶保温', '封装内衬面板', '铺装成品地板',
                '布置床铺软装', '点亮灯光入住',
            ]},
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
                '立柱搭建外部框架', '完成外部入口门面', '推镜穿过门口进入原始室内',
                '清空室内积渣', '铺设防潮基层', '架设墙顶龙骨',
                '填充墙顶保温', '封装内衬面板', '铺装成品地板',
                '布置床铺软装', '点亮灯光入住',
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
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                '清理外部', '修复外壳', '过门进入室内', '清理室内',
                '铺装地板', '安装灯具', '布置家具', '点亮入住',
            ],
        }
        errors = pp.pacing_skeleton_outline_violations(idea)
        self.assertTrue(errors)
        self.assertTrue(any('completed exterior mini-payoff' in err for err in errors))

    def test_dual_payoff_rejects_hard_cut_even_when_other_structure_is_valid(self):
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                '立柱搭建外部框架', '完成外部入口门面', '硬切原始室内',
                '清空室内积渣', '铺设防潮基层', '架设墙顶龙骨',
                '填充墙顶保温', '封装内衬面板', '铺装成品地板',
                '布置床铺软装', '点亮灯光入住',
            ],
        }
        errors = pp.pacing_skeleton_outline_violations(idea)
        self.assertTrue(any('forbids a hard cut' in err for err in errors))

    def test_dual_payoff_outline_passes_only_with_both_arcs_and_layered_rebuild(self):
        idea = {
            'pacing_skeleton': 'dual_payoff',
            'beat_outline': [
                '立柱搭建外部框架', '完成外部入口门面', '推镜穿过门口进入原始室内',
                '清空室内积渣', '铺设防潮基层', '架设墙顶龙骨',
                '填充墙顶保温', '封装内衬面板', '铺装成品地板',
                '布置床铺软装', '点亮灯光入住',
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
            'beat_outline': ['  清运积渣  ', None, '', 5, '点亮灯带,人物入住'],
        }], ensure_ascii=False)
        result, _ = self._run(lambda *a, **k: payload)
        self.assertEqual(result['ideas'][0]['beat_outline'],
                         ['清运积渣', '5', '点亮灯带,人物入住'])

    def test_outline_returned_as_one_string_is_split_into_beats(self):
        payload = json.dumps([{
            'title': 'T', 'dna': 'a / b / c',
            'beat_outline': '清运积渣\n架设龙骨\n点亮灯带,人物入住',
        }], ensure_ascii=False)
        result, _ = self._run(lambda *a, **k: payload)
        self.assertEqual(result['ideas'][0]['beat_outline'],
                         ['清运积渣', '架设龙骨', '点亮灯带,人物入住'])

    def test_batch_with_no_outline_at_all_is_retried(self):
        """整批一条 beat_outline 都没有 = 模型整个忽略了这个字段,重试后用合规的那批。"""
        good = json.dumps([{
            'title': '带简介', 'dna': 'a / b / c',
            'beat_outline': ['清运积渣', '点亮灯带,人物入住'],
        }], ensure_ascii=False)
        responses = ['[{"title": "无简介", "dna": "x / y / z"}]', good]
        result, mock_chat = self._run(lambda *a, **k: responses.pop(0))
        self.assertEqual(mock_chat.call_count, 2)
        self.assertEqual(result['ideas'][0]['title'], '带简介')

    def test_partial_outlines_are_kept_without_burning_a_retry(self):
        """只是个别条目没写:为一条重跑整批不划算,照收即可(前端对这类卡片退回「载入维度」)。"""
        payload = json.dumps([
            {'title': '有', 'dna': 'a / b / c', 'beat_outline': ['清运积渣', '点亮灯带']},
            {'title': '无', 'dna': 'x / y / z'},
        ], ensure_ascii=False)
        result, mock_chat = self._run(lambda *a, **k: payload)
        self.assertEqual(mock_chat.call_count, 1)
        self.assertEqual([len(i['beat_outline']) for i in result['ideas']], [2, 0])

    def test_persistently_missing_outline_still_returns_ideas(self):
        """模型三次都不写:宁可给没有节拍简介的卡片,也不能把整批灵感丢掉(退回静态兜底)。"""
        payload = '[{"title": "无简介", "dna": "x / y / z"}]'
        result, mock_chat = self._run(lambda *a, **k: payload)
        self.assertEqual(mock_chat.call_count, 3)
        self.assertEqual(result['ideas'][0]['title'], '无简介')
        self.assertEqual(result['ideas'][0]['beat_outline'], [])


if __name__ == '__main__':
    unittest.main()
