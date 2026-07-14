"""联网参考案例库(trend_refs.json)回归测试:
1. persist_trend_refs:按摘要文本指纹去重落库、已有条目只回填 id、库损坏时跳过
   写入但仍回填 id(沉淀失败不拖垮激发)。
2. load_trend_refs/delete_trend_refs:缺失→[]、损坏→None(调用方回 500)、按 id 删。
3. run_ideate(trend_ref_ids=...):选中案例=首要创意来源——不再自动联网搜索,
   选中文本进入 system prompt 且带强制借鉴指令;未选中时保持自动通道并沉淀入库。
4. refresh_trend_refs:绕过 6 小时缓存强制重搜并沉淀。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prompt_pipeline as pp


class _TrendRefsTmpDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._patches = [
            patch.object(pp, 'TREND_REFS_PATH',
                         os.path.join(self._tmp_dir, 'trend_refs.json')),
            patch.object(pp, 'SEARCH_SNIPPET_CACHE_PATH',
                         os.path.join(self._tmp_dir, 'search_snippet_cache.json')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestPersistAndLoad(_TrendRefsTmpDirMixin):
    def test_missing_file_loads_empty(self):
        self.assertEqual(pp.load_trend_refs(), [])

    def test_persist_assigns_id_and_stores(self):
        out = pp.persist_trend_refs([
            {'source': 'web_search', 'label': 'L1', 'query': 'q', 'text': '· 要点一'},
        ])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]['id'].startswith('tr_'))
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['id'], out[0]['id'])
        self.assertEqual(stored[0]['text'], '· 要点一')
        self.assertIn('created_at', stored[0])

    def test_persist_dedupes_by_text_fingerprint(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': '同一段要点'}])
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': '同一段要点'}])
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)          # 不重复入库
        self.assertEqual(out[0]['id'], stored[0]['id'])  # 但回填相同 id
        self.assertEqual(stored[0]['label'], 'A')        # 保留首次入库的条目

    def test_persist_skips_empty_text(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'L', 'text': '   '}])
        self.assertEqual(pp.load_trend_refs(), [])

    def test_corrupt_store_returns_none_and_persist_is_nonfatal(self):
        with open(pp.TREND_REFS_PATH, 'w', encoding='utf-8') as f:
            f.write('{broken json')
        self.assertIsNone(pp.load_trend_refs())
        # 损坏时 persist 跳过写入但仍回填 id,不抛异常
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'L', 'text': 'T'}])
        self.assertTrue(out[0]['id'].startswith('tr_'))
        self.assertIsNone(pp.load_trend_refs())  # 没有把损坏文件覆盖掉

    def test_delete_by_ids(self):
        out = pp.persist_trend_refs([
            {'source': 'web_search', 'label': 'A', 'text': 'aaa'},
            {'source': 'custom_urls', 'label': 'B', 'text': 'bbb'},
        ])
        result = pp.delete_trend_refs([out[0]['id']])
        self.assertEqual(result['deleted'], 1)
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['label'], 'B')

    def test_delete_missing_ids_is_noop(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'aaa'}])
        result = pp.delete_trend_refs(['tr_nope'])
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(len(pp.load_trend_refs()), 1)

    def test_write_rotates_bak(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'aaa'}])
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'bbb'}])
        self.assertTrue(os.path.exists(pp.TREND_REFS_PATH + '.bak'))
        with open(pp.TREND_REFS_PATH + '.bak', 'r', encoding='utf-8') as f:
            bak = json.load(f)
        self.assertEqual(len(bak), 1)  # .bak 是上一版(只有 A)


class TestRunIdeateWithSelectedRefs(_TrendRefsTmpDirMixin):
    def _seed(self):
        return pp.persist_trend_refs([
            {'source': 'web_search', 'label': '联网搜索 · 爆款', 'text': '· 集装箱爆改正在流行'},
            {'source': 'custom_urls', 'label': '自定义网址 · 1 个', 'text': '· 树屋前后对比很吃香'},
        ])

    def test_selected_refs_skip_auto_search_and_enter_prompt(self):
        seeded = self._seed()
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return '[{"title": "T", "trend_ref": "借鉴集装箱"}]'

        with patch.object(pp, 'fetch_trend_snippet') as mock_search, \
             patch.object(pp, 'fetch_custom_url_snippet') as mock_urls, \
             patch.object(pp, '_chat', side_effect=fake_chat):
            result = pp.run_ideate({}, count=2, trend_ref_ids=[seeded[0]['id']])

        mock_search.assert_not_called()  # 选中案例后不再自动联网搜索
        mock_urls.assert_not_called()
        self.assertIn('PRIMARY CREATIVE SOURCE', captured['system'])
        self.assertIn('集装箱爆改正在流行', captured['system'])
        self.assertNotIn('树屋前后对比', captured['system'])  # 未选中的不进 prompt
        self.assertEqual(len(result['trend_refs']), 1)
        self.assertEqual(result['trend_refs'][0]['id'], seeded[0]['id'])

    def test_unknown_ids_fall_back_to_auto_channel(self):
        with patch.object(pp, 'fetch_trend_snippet', return_value='· 自动要点') as mock_search, \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value='[{"title": "T"}]'):
            result = pp.run_ideate({}, count=1, trend_ref_ids=['tr_ghost'])
        mock_search.assert_called_once()
        self.assertEqual(result['trend_refs'][0]['source'], 'web_search')

    def test_auto_channel_persists_refs_into_store(self):
        with patch.object(pp, 'fetch_trend_snippet', return_value='· 自动要点'), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value='[{"title": "T"}]'):
            result = pp.run_ideate({}, count=1)
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['text'], '· 自动要点')
        # 返回给前端的 refs 已带库内 id
        self.assertEqual(result['trend_refs'][0]['id'], stored[0]['id'])


class TestRefreshTrendRefs(_TrendRefsTmpDirMixin):
    def test_refresh_bypasses_cache_and_persists(self):
        # 先塞一条"新鲜"缓存:普通调用会命中它,refresh(ttl=0)必须绕过强制重搜
        import time as _time
        pp._save_search_snippet_cache({
            'ideation_trend_snippet_v2': {'ts': _time.time(), 'text': '旧缓存要点'},
        })
        with patch.object(pp, '_chat', return_value='· 全新要点') as mock_chat:
            out = pp.refresh_trend_refs({'model': 'gemini-3-flash'})
        mock_chat.assert_called_once()  # 真发起了搜索而不是吃缓存
        self.assertEqual(out[0]['text'], '· 全新要点')
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['text'], '· 全新要点')


if __name__ == '__main__':
    unittest.main()
