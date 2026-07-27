"""联网参考案例库使用计次触发点回归测试(2026-07-23)：

之前 mark_trend_refs_used() 在 run_ideate 生成灵感卡片时就立即计次——只要
案例被拿去当创意来源展示过就算"用过一次"，哪怕用户从没点开、更没合成过。
现在计次点挪到「一键合成」(/api/compose)：只有真正被合成的 idea、且该 idea
确实借鉴过参考(dimensions.trend_ref 非空，由 run_ideate 里 LLM 的 trend_ref
字段透传而来)才算一次真实使用；只是被激发展示/浏览的候选案例不算数。
run_ideate 本身只把候选案例 id 原样带在每条 idea 上(idea['trend_ref_ids'])，
供合成时按需回填。
"""
import os
from unittest.mock import patch

import pytest

import server
import server_common
import prompt_pipeline as pp


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("tasks", exist_ok=True)
    server_common.ACTIVE_TASKS.clear()
    monkeypatch.setattr(pp, "TREND_REFS_PATH", os.path.join(str(tmp_path), "trend_refs.json"))
    monkeypatch.setattr(pp, "TREND_REFS_ARCHIVE_PATH", os.path.join(str(tmp_path), "trend_refs.archive.json"))
    # /api/compose 前置的访问门禁/限流/客户端 IP 探测与本测试无关,直接放行
    monkeypatch.setattr(server, "access_ok", lambda handler: True)
    monkeypatch.setattr(server, "rate_ok", lambda ip, action='default': True)
    monkeypatch.setattr(server, "_client_ip", lambda handler: "127.0.0.1")
    monkeypatch.setattr(server, "effective_config", lambda c: c or {})
    # 真正的合成 worker 不需要跑,只关心计次副作用是否发生
    monkeypatch.setattr(server, "background_worker", lambda *a, **k: None)
    yield
    server_common.ACTIVE_TASKS.clear()


def _compose_handler(body):
    h = object.__new__(server.SparkRequestHandler)
    h.path = "/api/compose"
    h._read_json_body = lambda: body
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


class TestComposeMarksTrendRefUsage:
    def test_borrowed_idea_marks_used_on_compose(self):
        seeded = pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "a"}])
        body = {
            "dimensions": {
                "theme": "x",
                "trend_ref": "借鉴了集装箱爆改",
                "trend_ref_ids": [seeded[0]["id"]],
            },
            "task_id": "compose_borrowed",
        }
        h, sent = _compose_handler(body)
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][0]["status"] == "ok"
        stored = pp.load_trend_refs()
        assert stored[0]["used_count"] == 1
        assert stored[0]["last_used_at"] is not None

    def test_non_borrowed_idea_does_not_mark_used(self):
        """trend_ref 为空串(LLM 判定没借鉴)：即使 trend_ref_ids 带过来了也不计次。"""
        seeded = pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "a"}])
        body = {
            "dimensions": {
                "theme": "x",
                "trend_ref": "",
                "trend_ref_ids": [seeded[0]["id"]],
            },
            "task_id": "compose_not_borrowed",
        }
        h, sent = _compose_handler(body)
        server.SparkRequestHandler.do_POST(h)

        assert pp.load_trend_refs()[0]["used_count"] == 0

    def test_missing_trend_ref_fields_is_noop(self):
        """完全不带趋势参考字段的普通合成(非灵感卡片路径)不应报错也不计次。"""
        pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "a"}])
        body = {"dimensions": {"theme": "x"}, "task_id": "compose_plain"}
        h, sent = _compose_handler(body)
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][0]["status"] == "ok"
        assert pp.load_trend_refs()[0]["used_count"] == 0

    def test_already_running_duplicate_submit_does_not_double_count(self):
        """同一 task_id 已在运行中重复提交(already_running)不应重复计次。"""
        seeded = pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "a"}])
        body = {
            "dimensions": {
                "theme": "x",
                "trend_ref": "借鉴了集装箱爆改",
                "trend_ref_ids": [seeded[0]["id"]],
            },
            "task_id": "compose_dup",
        }
        h1, sent1 = _compose_handler(body)
        server.SparkRequestHandler.do_POST(h1)
        assert pp.load_trend_refs()[0]["used_count"] == 1

        h2, sent2 = _compose_handler(body)
        server.SparkRequestHandler.do_POST(h2)
        assert sent2[0][0].get("already_running") is True
        assert pp.load_trend_refs()[0]["used_count"] == 1  # 没有变成 2


class TestRunIdeateDoesNotMarkUsageAnymore:
    def test_ideation_alone_leaves_used_count_at_zero(self):
        seeded = pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "· 要点"}])
        with patch.object(pp, "_chat", return_value='[{"title": "T", "trend_ref": "借鉴"}]'):
            result = pp.run_ideate({}, count=1, trend_ref_ids=[seeded[0]["id"]])

        assert pp.load_trend_refs()[0]["used_count"] == 0
        assert result["ideas"][0]["trend_ref_ids"] == [seeded[0]["id"]]

    def test_auto_picked_ideation_also_leaves_used_count_at_zero(self):
        seeded = pp.persist_trend_refs([{"source": "web_search", "label": "A", "text": "· 要点"}])
        # 2026-07-25 起激发默认每批都走新鲜联网检索,案例库加权随机抽取只在联网整体
        # 拿不到东西时兜底——这条测试关心的是兜底路径上的计次行为,所以显式把两条联网
        # 通道摁成空。(不显式摁不行:SEARCH_SNIPPET_CACHE_PATH 是仓库根的绝对路径,
        # 会穿透 fixture 的 chdir 隔离,把真实的 6 小时缓存喂进来。)
        with patch.object(pp, "fetch_trend_snippet", return_value=""), \
             patch.object(pp, "fetch_custom_url_snippet", return_value=""), \
             patch.object(pp.random, "choices", return_value=[seeded[0]]), \
             patch.object(pp, "_chat", return_value='[{"title": "T", "trend_ref": "借鉴"}]'):
            result = pp.run_ideate({}, count=1)

        assert pp.load_trend_refs()[0]["used_count"] == 0
        assert result["ideas"][0]["trend_ref_ids"] == [seeded[0]["id"]]
