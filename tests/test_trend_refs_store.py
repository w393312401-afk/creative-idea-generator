"""联网参考案例库(trend_refs.json)回归测试:
1. persist_trend_refs:按摘要文本指纹去重落库、已有条目只回填 id、库损坏时跳过
   写入但仍回填 id(沉淀失败不拖垮激发)。
2. load_trend_refs/delete_trend_refs:缺失→[]、损坏→None(调用方回 500)、按 id 删。
3. run_ideate(trend_ref_ids=...):选中案例=首要创意来源——不再自动联网搜索,
   选中文本进入 system prompt 且带强制借鉴指令;未选中时保持自动通道并沉淀入库。
4. refresh_trend_refs:绕过 6 小时缓存强制重搜并沉淀。
5. 软上限自动归档(2026-07-16):超过 TREND_REFS_CAP 时最老且未使用过的条目挪进
   trend_refs.archive.json;mark_trend_refs_used 回写使用统计让淘汰优先跳过
   验证过有用的条目;restore_trend_refs 可从归档挪回主库;delete_trend_refs
   支持 archive=True 对归档库操作。
6. 2026-07-23:mark_trend_refs_used 的计次点已从 run_ideate 挪到「一键合成」
   (/api/compose,见 tests/test_trend_ref_compose_usage.py)——run_ideate 只
   把候选案例 id 带在每条 idea 上(trend_ref_ids),本文件里 run_ideate 相关
   用例只验证 id 透传，不再验证 used_count 递增。
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
            patch.object(pp, 'TREND_REFS_ARCHIVE_PATH',
                         os.path.join(self._tmp_dir, 'trend_refs.archive.json')),
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


class TestSoftCapEviction(_TrendRefsTmpDirMixin):
    def test_no_eviction_below_cap(self):
        with patch.object(pp, 'TREND_REFS_CAP', 3):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'b'}])
        self.assertEqual(len(pp.load_trend_refs()), 2)
        self.assertEqual(pp.load_trend_refs_archive(), [])

    def test_eviction_prefers_unused_oldest_over_cap(self):
        with patch.object(pp, 'TREND_REFS_CAP', 2):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'oldest-unused', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'newer-unused', 'text': 'b'}])
            # 第三次入库触发超上限(2)淘汰:两条都未使用过,挪最老的那条(A)
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'newest-unused', 'text': 'c'}])
        stored = pp.load_trend_refs()
        archived = pp.load_trend_refs_archive()
        self.assertEqual(len(stored), 2)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]['label'], 'oldest-unused')
        self.assertEqual({e['label'] for e in stored}, {'newer-unused', 'newest-unused'})

    def test_eviction_protects_used_entries_over_unused(self):
        with patch.object(pp, 'TREND_REFS_CAP', 2):
            out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'used-old', 'text': 'a'}])
            pp.mark_trend_refs_used([out[0]['id']])  # 标记为"曾被勾选生成过灵感"
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'unused-new', 'text': 'b'}])
            # 触发淘汰:即使 used-old 更老,未使用过的 unused-new 优先被挪走
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'unused-newest', 'text': 'c'}])
        stored = pp.load_trend_refs()
        archived = pp.load_trend_refs_archive()
        self.assertIn('used-old', {e['label'] for e in stored})
        self.assertEqual(archived[0]['label'], 'unused-new')

    def test_eviction_skipped_when_archive_unwritable(self):
        # 归档文件本身损坏(读出 None)时,本轮不淘汰,主库宁可暂时超上限
        with open(pp.TREND_REFS_ARCHIVE_PATH, 'w', encoding='utf-8') as f:
            f.write('{broken')
        with patch.object(pp, 'TREND_REFS_CAP', 1):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'b'}])
        self.assertEqual(len(pp.load_trend_refs()), 2)  # 没被淘汰


class TestMarkTrendRefsUsed(_TrendRefsTmpDirMixin):
    def test_mark_used_increments_and_sets_timestamp(self):
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
        pp.mark_trend_refs_used([out[0]['id']])
        pp.mark_trend_refs_used([out[0]['id']])
        stored = pp.load_trend_refs()
        self.assertEqual(stored[0]['used_count'], 2)
        self.assertIsNotNone(stored[0]['last_used_at'])

    def test_mark_used_noop_for_unknown_ids(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
        pp.mark_trend_refs_used(['tr_ghost'])  # 不应抛异常
        self.assertEqual(pp.load_trend_refs()[0]['used_count'], 0)

    def test_run_ideate_no_longer_marks_used_only_tags_candidate_ids(self):
        # 2026-07-23:计次点挪到「一键合成」(server.py /api/compose)。run_ideate
        # 生成灵感卡片这一步本身不再计次,只把候选案例 id 原样带在每条 idea 上,
        # 供合成时按需回填(见 tests/test_trend_ref_compose_usage.py)。
        seeded = pp.persist_trend_refs([
            {'source': 'web_search', 'label': 'L1', 'text': '· 要点一'},
        ])
        with patch.object(pp, '_chat', return_value='[{"title": "T", "trend_ref": "借鉴"}]'):
            result = pp.run_ideate({}, count=1, trend_ref_ids=[seeded[0]['id']])
        stored = pp.load_trend_refs()
        self.assertEqual(stored[0]['used_count'], 0)
        self.assertEqual(result['ideas'][0]['trend_ref_ids'], [seeded[0]['id']])

    def test_mark_used_auto_archives_at_threshold(self):
        # 用满 TREND_REF_AUTO_ARCHIVE_AFTER(3)次后自动从主库挪进归档(软删可恢复)
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
        pp.mark_trend_refs_used([out[0]['id']])
        pp.mark_trend_refs_used([out[0]['id']])
        self.assertEqual(pp.load_trend_refs()[0]['used_count'], 2)
        self.assertEqual(pp.load_trend_refs_archive(), [])
        pp.mark_trend_refs_used([out[0]['id']])  # 第 3 次触发自动归档
        self.assertEqual(pp.load_trend_refs(), [])
        archived = pp.load_trend_refs_archive()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]['id'], out[0]['id'])
        self.assertEqual(archived[0]['used_count'], 3)

    def test_mark_used_archive_unwritable_skips_archive_but_keeps_count(self):
        # 归档库损坏时本轮跳过自动归档,但 used_count 递增仍要落盘,不能连计数都丢
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
        pp.mark_trend_refs_used([out[0]['id']])
        pp.mark_trend_refs_used([out[0]['id']])
        with open(pp.TREND_REFS_ARCHIVE_PATH, 'w', encoding='utf-8') as f:
            f.write('{broken')
        pp.mark_trend_refs_used([out[0]['id']])  # 第 3 次本该触发归档,但归档库读取失败
        stored = pp.load_trend_refs()
        self.assertEqual(len(stored), 1)  # 仍留在主库
        self.assertEqual(stored[0]['used_count'], 3)  # 计数仍然递增落盘


class TestRunIdeateTrendSourcePriority(_TrendRefsTmpDirMixin):
    """2026-07-25「联网主导」:每一批都走新鲜联网检索(fetch_trend_snippet 自带 6 小时
    缓存,所以"每批都搜"的真实搜索频率仍是每 6 小时一次),案例库加权随机抽取降级为
    联网整体失败时的兜底。旧行为(库非空就跳过联网)让绝大多数批次根本没联网、反复
    复用同几条历史摘要,信息茧房比不联网更严重。手动勾选仍然是最高优先级。"""

    def _seed(self):
        return pp.persist_trend_refs([
            {'source': 'web_search', 'label': 'A', 'text': '· 集装箱爆改正在流行'},
            {'source': 'custom_urls', 'label': 'B', 'text': '· 树屋前后对比很吃香'},
        ])

    def test_live_search_runs_even_when_library_is_not_empty(self):
        self._seed()
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return '[{"title": "T", "trend_ref": "借鉴"}]'

        with patch.object(pp, 'fetch_trend_snippet', return_value='· 谷仓改住宅本周爆火') as mock_search, \
             patch.object(pp, 'fetch_custom_url_snippet', return_value='') as mock_urls, \
             patch.object(pp.random, 'choices') as mock_choices, \
             patch.object(pp, '_chat', side_effect=fake_chat):
            result = pp.run_ideate({}, count=2)

        mock_search.assert_called_once()   # 库非空也照样联网
        mock_urls.assert_called_once()
        mock_choices.assert_not_called()   # 联网拿到东西就不再走库里的加权随机兜底
        self.assertIn('PRIMARY CREATIVE SOURCE', captured['system'])
        self.assertIn('谷仓改住宅本周爆火', captured['system'])
        self.assertNotIn('集装箱爆改正在流行', captured['system'])  # 旧条目不再顶替新鲜摘要
        # 新鲜摘要同时沉淀进库,供后续兜底/人工勾选复用
        self.assertIn('· 谷仓改住宅本周爆火', [e['text'] for e in pp.load_trend_refs()])
        self.assertEqual(result['ideas'][0]['trend_ref_ids'], [result['trend_refs'][0]['id']])

    def test_falls_back_to_library_pick_when_live_search_yields_nothing(self):
        seeded = self._seed()
        captured = {}

        def fake_chat(config, system_prompt, user_prompt, **kwargs):
            captured['system'] = system_prompt
            return '[{"title": "T", "trend_ref": "借鉴"}]'

        # 离网/超时/aux 模型不支持搜索时 fetch_trend_snippet 静默返回空串
        with patch.object(pp, 'fetch_trend_snippet', return_value=''), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp.random, 'choices', return_value=[seeded[0]]), \
             patch.object(pp, '_chat', side_effect=fake_chat):
            result = pp.run_ideate({}, count=2)

        self.assertIn('集装箱爆改正在流行', captured['system'])
        self.assertNotIn('树屋前后对比', captured['system'])  # 没被抽中的不进 prompt
        self.assertEqual(result['trend_refs'][0]['id'], seeded[0]['id'])
        # 2026-07-23:计次点挪到「一键合成」,灵感激发阶段本身不再计次;
        # 只把抽中的候选 id 带在每条 idea 上供合成时按需回填
        stored_by_id = {e['id']: e for e in pp.load_trend_refs()}
        self.assertEqual(stored_by_id[seeded[0]['id']]['used_count'], 0)
        self.assertEqual(stored_by_id[seeded[1]['id']]['used_count'], 0)
        self.assertEqual(result['ideas'][0]['trend_ref_ids'], [seeded[0]['id']])

    def test_manual_selection_overrides_live_search(self):
        seeded = self._seed()
        with patch.object(pp, 'fetch_trend_snippet') as mock_search, \
             patch.object(pp, 'fetch_custom_url_snippet'), \
             patch.object(pp.random, 'choices') as mock_choices, \
             patch.object(pp, '_chat', return_value='[{"title": "T", "trend_ref": "借鉴"}]'):
            result = pp.run_ideate({}, count=1, trend_ref_ids=[seeded[1]['id']])

        mock_search.assert_not_called()   # 显式勾选时连搜索都不发
        mock_choices.assert_not_called()  # 手动勾选命中时不走自动挑选
        self.assertEqual(result['trend_refs'][0]['id'], seeded[1]['id'])


class TestRestoreTrendRefs(_TrendRefsTmpDirMixin):
    def _archive_one(self):
        with patch.object(pp, 'TREND_REFS_CAP', 1):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'b'}])
        archived = pp.load_trend_refs_archive()
        self.assertEqual(len(archived), 1)  # sanity: A got archived
        return archived[0]['id']

    def test_restore_moves_entry_back_to_main(self):
        archived_id = self._archive_one()
        result = pp.restore_trend_refs([archived_id])
        self.assertEqual(result['restored'], 1)
        stored = pp.load_trend_refs()
        archive = pp.load_trend_refs_archive()
        self.assertIn(archived_id, {e['id'] for e in stored})
        self.assertEqual(archive, [])

    def test_restore_unknown_id_is_noop(self):
        result = pp.restore_trend_refs(['tr_ghost'])
        self.assertEqual(result['restored'], 0)

    def test_restore_empty_ids_is_noop(self):
        result = pp.restore_trend_refs([])
        self.assertEqual(result['restored'], 0)


class TestDeleteArchiveScope(_TrendRefsTmpDirMixin):
    def test_delete_with_archive_flag_targets_archive_store(self):
        with patch.object(pp, 'TREND_REFS_CAP', 1):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'b'}])
        archived_id = pp.load_trend_refs_archive()[0]['id']
        result = pp.delete_trend_refs([archived_id], archive=True)
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(pp.load_trend_refs_archive(), [])
        self.assertEqual(len(pp.load_trend_refs()), 1)  # 主库未受影响


class TestDeriveTrendRefLabel(unittest.TestCase):
    """2026-07-21 案例库可用性诊断:旧写法 label = 搜索词前 60 字,同一条(常年不变
    的默认)搜索词产出的所有历史条目 label 逐字相同,列表只能靠时间戳区分。改成
    从正文要点关键词提炼,同一批搜索结果里不同条目也应互相区分开。"""

    def test_extracts_bold_keyword_from_bullet(self):
        text = "* **百年废墟与荒野遗迹复活**：以老屋为载体...\n* **AI辅助渐变**：利用AI工具..."
        label = pp._derive_trend_ref_label(text, fallback='FALLBACK')
        self.assertIn('百年废墟与荒野遗迹复活', label)
        self.assertIn('AI辅助渐变', label)
        self.assertNotEqual(label, 'FALLBACK')

    def test_extracts_curly_quoted_keyword_from_bullet(self):
        text = "- 现在最爆的是“AI假延时改造”：废弃屋/地下掩体...\n- 题材从旧房转向“壳体猎奇”：埋地集装箱..."
        label = pp._derive_trend_ref_label(text, fallback='FALLBACK')
        self.assertIn('AI假延时改造', label)
        self.assertIn('壳体猎奇', label)

    def test_falls_back_to_colon_prefix_without_bold_or_quotes(self):
        text = "- 载体爆点：荒废农村老宅/小院\n- 异形空间升温：山洞、地下掩体"
        label = pp._derive_trend_ref_label(text, fallback='FALLBACK')
        self.assertIn('载体爆点', label)
        self.assertIn('异形空间升温', label)

    def test_two_different_batches_produce_different_labels(self):
        # 同一条默认搜索词、不同批次搜到的正文——旧写法两者 label 完全相同
        # (都是"联网搜索 · <搜索词前60字>"),新写法应各自反映自己的正文内容
        text_a = "* **AI假延时改造**：8-30秒完成废墟到豪宅"
        text_b = "* **独居女性硬核改造**：单身女性亲自搬砖铺砖"
        label_a = pp._derive_trend_ref_label(text_a, fallback='SAME')
        label_b = pp._derive_trend_ref_label(text_b, fallback='SAME')
        self.assertNotEqual(label_a, label_b)

    def test_empty_text_falls_back(self):
        self.assertEqual(pp._derive_trend_ref_label('', fallback='FALLBACK'), 'FALLBACK')
        self.assertEqual(pp._derive_trend_ref_label(None, fallback='FALLBACK'), 'FALLBACK')

    def test_label_topic_is_length_capped(self):
        text = "* **" + ("超" * 60) + "**：说明文字"
        label = pp._derive_trend_ref_label(text, fallback='FALLBACK')
        self.assertLessEqual(len(label), 20)  # 单条主题词截到 16 字,不会整段糊在一起

    def test_quote_marks_do_not_survive_mid_phrase(self):
        # 加粗内容自身还嵌套了引号（"老破小"与极小户型极限收纳）时,引号不应残留
        # 在提炼出的主题词中间
        text = '* **"老破小"与极小户型极限收纳**：聚焦小户型...'
        label = pp._derive_trend_ref_label(text, fallback='FALLBACK')
        self.assertNotIn('"', label)
        self.assertNotIn('“', label)
        self.assertNotIn('”', label)


class TestBuildLiveTrendRefsUsesDerivedLabel(_TrendRefsTmpDirMixin):
    def test_web_search_label_reflects_content_not_query(self):
        search = {'query': '固定默认搜索词', 'cache_key': 'k', 'system_instruction': 'sys'}
        refs = pp._build_live_trend_refs(
            {}, search,
            trend_snippet='* **集装箱爆改**：低成本移动城堡概念',
            custom_snippet='',
        )
        self.assertEqual(len(refs), 1)
        self.assertIn('集装箱爆改', refs[0]['label'])
        self.assertNotIn('固定默认搜索词', refs[0]['label'])


class TestPersistDedupesByTextEvenWithMismatchedId(_TrendRefsTmpDirMixin):
    """2026-07-21 实测库里真实出现过一对内容相同但 id 不同的重复条目——根因是
    id=md5(text) 这条不变式在某条目的正文被后续脚本改写(如 boilerplate 回填清洗)
    而未同步重算 id 时失效。persist 除了按 id 去重,还要按已入库条目的正文文本
    兜底去重,防止同样的情况让"新" id 绕过去重重复入库。"""

    def test_stale_id_on_existing_entry_does_not_defeat_text_dedup(self):
        stored = pp._load_trend_refs_unlocked() or []
        stored.append({
            'id': 'tr_stale_mismatched_id',  # 不等于 md5(text) 的"脏" id
            'created_at': '2026-07-15 22:07',
            'source': 'web_search',
            'label': 'OLD',
            'query': 'q',
            'text': '完全相同的正文内容',
            'used_count': 1,
            'last_used_at': None,
        })
        pp._write_trend_refs_unlocked(stored)

        out = pp.persist_trend_refs([
            {'source': 'web_search', 'label': 'NEW', 'text': '完全相同的正文内容'},
        ])
        self.assertEqual(out[0]['id'], 'tr_stale_mismatched_id')  # 回填的是旧条目的 id
        stored_after = pp.load_trend_refs()
        self.assertEqual(len(stored_after), 1)  # 没有重复入库
        self.assertEqual(stored_after[0]['label'], 'OLD')  # 保留原条目


class TestAvoidRepeatLabelsSuffix(unittest.TestCase):
    def test_empty_labels_yields_empty_suffix(self):
        self.assertEqual(pp._avoid_repeat_labels_suffix([]), '')
        self.assertEqual(pp._avoid_repeat_labels_suffix(None), '')

    def test_includes_sample_labels_capped_at_12(self):
        labels = [f'label{i}' for i in range(20)]
        suffix = pp._avoid_repeat_labels_suffix(labels)
        self.assertIn('label0', suffix)
        self.assertIn('label11', suffix)
        self.assertNotIn('label12', suffix)


class TestRefreshTrendRefsAvoidsRepeatAngles(_TrendRefsTmpDirMixin):
    def test_refresh_injects_existing_labels_into_system_instruction(self):
        pp.persist_trend_refs([
            {'source': 'web_search', 'label': '已收录角度A', 'text': '旧要点'},
        ])
        captured = {}

        def fake_chat(config, system, user, **kwargs):
            captured['system'] = system
            return '· 新要点'

        with patch.object(pp, '_chat', side_effect=fake_chat):
            pp.refresh_trend_refs({'model': 'gemini-3-flash'})
        self.assertIn('已收录角度A', captured['system'])


class TestRelabelTrendRef(_TrendRefsTmpDirMixin):
    def test_relabel_updates_main_store(self):
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'OLD', 'text': 'a'}])
        result = pp.relabel_trend_ref(out[0]['id'], '新名字')
        self.assertTrue(result['ok'])
        self.assertEqual(pp.load_trend_refs()[0]['label'], '新名字')

    def test_relabel_unknown_id_returns_not_ok(self):
        pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
        result = pp.relabel_trend_ref('tr_ghost', '新名字')
        self.assertFalse(result['ok'])

    def test_relabel_empty_label_is_rejected(self):
        out = pp.persist_trend_refs([{'source': 'web_search', 'label': 'OLD', 'text': 'a'}])
        result = pp.relabel_trend_ref(out[0]['id'], '   ')
        self.assertFalse(result['ok'])
        self.assertEqual(pp.load_trend_refs()[0]['label'], 'OLD')

    def test_relabel_archive_scope(self):
        with patch.object(pp, 'TREND_REFS_CAP', 1):
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'A', 'text': 'a'}])
            pp.persist_trend_refs([{'source': 'web_search', 'label': 'B', 'text': 'b'}])
        archived_id = pp.load_trend_refs_archive()[0]['id']
        result = pp.relabel_trend_ref(archived_id, '归档改名', archive=True)
        self.assertTrue(result['ok'])
        self.assertEqual(pp.load_trend_refs_archive()[0]['label'], '归档改名')
        self.assertEqual(pp.load_trend_refs()[0]['label'], 'B')  # 主库未受影响


class TestStripSearchBoilerplate(unittest.TestCase):
    def test_strips_search_echo_marker(self):
        text = "· 要点一\n· 要点二\n\n---\n**🔍 已为您搜索：** foo bar baz"
        self.assertEqual(pp._strip_search_boilerplate(text), "· 要点一\n· 要点二")

    def test_strips_citation_list_marker(self):
        text = ("· 要点一\n\n**🌐 来源引文：**\n[1] [youtube.com](https://youtube.com/x)\n"
                "[2] [douyin.com](https://douyin.com/y)")
        self.assertEqual(pp._strip_search_boilerplate(text), "· 要点一")

    def test_strips_both_markers_keeps_earliest_cut(self):
        text = "· 要点\n\n---\n**🔍 已为您搜索：** q\n\n**🌐 来源引文：**\n[1] a.com"
        self.assertEqual(pp._strip_search_boilerplate(text), "· 要点")

    def test_leaves_clean_text_unchanged(self):
        text = "· 要点一\n· 要点二\n· 要点三"
        self.assertEqual(pp._strip_search_boilerplate(text), text)

    def test_handles_empty_and_none(self):
        self.assertEqual(pp._strip_search_boilerplate(''), '')
        self.assertIsNone(pp._strip_search_boilerplate(None))


class TestBuildLiveTrendRefsCleansBoilerplate(_TrendRefsTmpDirMixin):
    def test_persisted_text_has_boilerplate_stripped(self):
        polluted = "· 集装箱爆改正在流行\n\n---\n**🔍 已为您搜索：** 集装箱 改造\n\n**🌐 来源引文：**\n[1] a.com"
        with patch.object(pp, 'fetch_trend_snippet', return_value=polluted), \
             patch.object(pp, 'fetch_custom_url_snippet', return_value=''), \
             patch.object(pp, '_chat', return_value='[{"title": "T"}]'):
            result = pp.run_ideate({}, count=1)
        stored = pp.load_trend_refs()
        self.assertEqual(stored[0]['text'], '· 集装箱爆改正在流行')
        self.assertNotIn('已为您搜索', result['trend_refs'][0]['text'])


if __name__ == '__main__':
    unittest.main()
