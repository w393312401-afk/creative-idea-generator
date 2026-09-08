# -*- coding: utf-8 -*-
""""视频明明生成好了却识别不到"——先取证，再改代码。

2026-09-06 现场：提交之后 status 一直是 missing，任务干等到 300s 单任务超时，
而视频在 Flow 页面上早就生成完了。要修必须先看清新版结果卡片长什么样：
带不带稳定 id？带不带首尾帧的 img？显不显示提示词？完成后的视频是 <video src>
还是 blob/MSE？这些只能看真实 DOM，靠行为反推会一直猜。

这个文件钉死取证链路本身的三条硬要求：
  * 取证在**卡片跟丢的那一刻**自动发生，不用人守着（超时后再抓，画布可能已被清）；
  * 取证是**只读**的——绝不导航、绝不新建项目，否则那批已生成的视频就没了；
  * 取证**绝不能影响主流程**，任何一步挂了都只是少一份证据。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.google_fx.services import google_fx_diagnostics as D
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import forensics


# ── 取证产物：额外 collector ──────────────────────────────────────────────

class _Page:
    def __init__(self, evaluate_result=None, evaluate_raises=False):
        self.url = "https://flow.google.com/project/x"
        self.keyboard = self
        self._evaluate_result = evaluate_result
        self._evaluate_raises = evaluate_raises

    def evaluate(self, script, arg=None):
        if self._evaluate_raises:
            raise RuntimeError("evaluate boom")
        if isinstance(self._evaluate_result, dict):
            return self._evaluate_result
        if "cloneNode" in script:
            return "<html><body><flow-grid-tile-container/></body></html>"
        if "maxNodes" in script or "rows.join" in script:
            return "body\n  flow-grid-tile-container"
        return None

    def is_closed(self):
        return False

    def content(self):
        return "<html><body>raw</body></html>"

    def inner_text(self, selector):
        return "t"

    def screenshot(self, timeout=None, full_page=False):
        return b"\x89PNG\r\n\x1a\nfake"

    def bring_to_front(self):
        pass

    def locator(self, selector):
        raise RuntimeError("not needed")


class TestCaptureCollectors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _capture(self, page, collectors):
        with patch.object(forensics, "DEBUG_ROOT", self.tmp / "fx_debug"):
            return forensics.capture(page, "t", "why", bucket="b", collectors=collectors)

    def test_collector_output_lands_next_to_the_other_evidence(self):
        slot = self._capture(_Page(), {"canvas_tiles.json": lambda p: {"tiles": [1, 2]}})
        payload = json.load(open(os.path.join(slot, "canvas_tiles.json"), encoding="utf-8"))
        assert payload == {"tiles": [1, 2]}

    def test_a_broken_collector_does_not_cost_us_the_rest_of_the_evidence(self):
        def _boom(page):
            raise RuntimeError("collector boom")

        slot = self._capture(_Page(), {"canvas_tiles.json": _boom})
        files = set(os.listdir(slot))
        assert "screenshot.png" in files and "page.html" in files
        meta = json.load(open(os.path.join(slot, "meta.json"), encoding="utf-8"))
        assert any(k.startswith("collector_error:") for k in meta)


# ── 画布 dump 本身 ────────────────────────────────────────────────────────

class TestDumpCanvasTiles(unittest.TestCase):
    def test_passes_its_limits_into_the_page(self):
        seen = {}

        class _P:
            def evaluate(self, script, arg=None):
                seen["arg"] = arg
                seen["script"] = script
                return {"tiles": []}

        D.dump_canvas_tiles(_P(), max_tiles=7, text_limit=99)
        assert seen["arg"] == {"maxTiles": 7, "textLimit": 99}

    def test_a_dead_page_yields_an_error_dict_not_an_exception(self):
        """取证在失败路径上跑，自己再抛异常就把真正的错误盖掉了。"""
        out = D.dump_canvas_tiles(_Page(evaluate_raises=True))
        assert "error" in out

    def test_dump_is_read_only(self):
        """dump 的 JS 里不允许出现任何会改页面的动作。"""
        js = D._CANVAS_TILES_JS
        for forbidden in (".click(", ".goto(", "setAttribute", "remove()",
                          "dispatchEvent", "scrollIntoView"):
            assert forbidden not in js, f"画布 dump 不该做 {forbidden}"


# ── 手动取证入口：绝不能把画布弄没 ────────────────────────────────────────

class _CtxBrowser:
    def __init__(self, pages):
        self.contexts = [type("Ctx", (), {"pages": pages})()]


class TestCaptureCanvasSnapshotIsNonDestructive(unittest.TestCase):
    def _run(self, pages):
        import contextlib

        @contextlib.contextmanager
        def _slot(*a, **k):
            yield None

        fake_pw = type("PW", (), {
            "chromium": type("C", (), {
                "connect_over_cdp": staticmethod(lambda ws, timeout=None: _CtxBrowser(pages))
            })()
        })()

        @contextlib.contextmanager
        def _sync_playwright():
            yield fake_pw

        with patch.object(D, "browser_slot", _slot), \
             patch.dict("sys.modules", {}), \
             patch("playwright.sync_api.sync_playwright", _sync_playwright), \
             patch("integrations.google_fx.utils.browser.get_ads_ws_url",
                   lambda **k: "ws://x"), \
             patch.object(forensics, "capture", lambda *a, **k: "/slot"):
            return D.capture_canvas_snapshot()

    def test_uses_the_already_open_flow_tab_without_navigating(self):
        page = _Page(evaluate_result={"url": "https://flow.google.com/project/x",
                                      "counts": {"outermost-tiles": 6}})
        out = self._run([page])
        assert out["status"] == "ok"
        assert out["counts"]["outermost-tiles"] == 6

    def test_refuses_instead_of_navigating_when_no_flow_tab_is_open(self):
        """找不到画布就如实报错。绝不能自己导航去开一个——那会把现场冲掉。"""
        stray = _Page()
        stray.url = "https://www.example.com/"
        out = self._run([stray])
        assert out["status"] == "error"
        assert "Flow 标签页" in out["message"]

    def test_never_calls_the_navigating_helpers(self):
        """两个会动页面的助手都碰不得：
        find_or_create_page 会导航、还会关掉多余标签页；
        ensure_flow_workspace 在崩溃页上会去点「新建项目」。

        只看**真的被引用的名字**（注释/文档串里提到它们是正常的）。
        """
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(D.capture_canvas_snapshot)))
        referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        referenced |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                referenced |= {a.name for a in node.names}
        assert "find_or_create_page" not in referenced
        assert "ensure_flow_workspace" not in referenced


# ── 自动取证：卡片跟丢的那一刻 ────────────────────────────────────────────

class _Req:
    prompt = "p"
    model = "Veo 3.1"
    ratio = "9:16"
    image = ""
    end_image = ""


def _runner(n=2):
    return V._ChunkRunner(total_reqs=n, chunk_start=0, chunk=[_Req() for _ in range(n)],
                          all_slices={}, on_progress=None, cancel_check=None)


class TestTileLostCapture(unittest.TestCase):
    def test_captures_once_and_only_once_per_chunk(self):
        runner = _runner()
        calls = []
        with patch("integrations.google_fx.utils.forensics.capture",
                   lambda *a, **k: calls.append(k) or "/slot"):
            task = {"idx": 0, "tile_id": "spark_tile_1"}
            runner._capture_tile_lost(object(), task)
            runner._capture_tile_lost(object(), task)
        assert len(calls) == 1, "同一个 chunk 里反复截图只会拖慢轮询"
        assert "canvas_tiles.json" in calls[0]["collectors"]

    def test_capture_failure_never_breaks_the_polling_loop(self):
        runner = _runner()
        with patch("integrations.google_fx.utils.forensics.capture",
                   side_effect=RuntimeError("boom")):
            runner._capture_tile_lost(object(), {"idx": 0, "tile_id": "t"})
        # 没抛出来就算通过

    @staticmethod
    def _drive_await(state, captures):
        """用假时钟跑一遍 _await_generation：真时钟会让用例空转等满超时。"""
        runner = _runner(1)
        submitted = [{"sub_idx": 0, "idx": 0, "req": _Req(), "tile_id": "t1",
                      "click_time": 0.0, "status": "generating",
                      "video_url": None, "message": ""}]
        clock = {"t": 0.0}

        def _now():
            clock["t"] += 0.2      # 每次读表推进 0.2s，几十轮就能走完超时窗口
            return clock["t"]

        with patch.object(V, "_inspect_all_pending_tiles", lambda *a, **k: {"t1": state}), \
             patch.object(V, "detect_page_credit_exhaustion", lambda *a, **k: None), \
             patch.object(V, "_dismiss_unexpected_overlays", lambda *a, **k: False), \
             patch.object(V, "get_runtime_max_wait_seconds", lambda: 3), \
             patch.object(V.time, "sleep", lambda *_a: None), \
             patch.object(V.time, "time", _now), \
             patch.object(V._ChunkRunner, "_download_and_report_task",
                          lambda self, page, task: task.__setitem__("reported", True)), \
             patch.object(V._ChunkRunner, "_capture_tile_lost",
                          lambda self, page, task, st=None: captures.append(st)):
            runner._await_generation(object(), submitted)

    def test_fires_on_the_real_symptom_not_just_on_missing_tiles(self):
        """2026-09-06 实测：卡片其实**找得到**（status=generating），只是既不报进度
        也拿不到视频地址，任务就这么哑到 300s 超时——而页面上视频早就出来了。

        上一版只在 status=='missing' 时取证，这个条件在真实故障里根本不成立，
        于是重跑两次一份现场都没留下。触发条件必须按症状写：没进度 + 没视频地址。
        """
        captures = []
        self._drive_await({"status": "generating", "progress": None, "videoSrc": None,
                           "resolvedBy": "stamp",
                           "tileTag": "flow-grid-tile-container"}, captures)
        assert captures, "卡片找得到但一直哑着，也必须取证"
        assert captures[0]["resolvedBy"] == "stamp"

    def test_a_healthy_task_reporting_progress_is_not_captured(self):
        """正常爬进度的任务不该被当成故障截图，否则每批都在刷现场。"""
        captures = []
        self._drive_await({"status": "generating", "progress": 42, "videoSrc": None},
                          captures)
        assert captures == []

    def test_it_records_what_we_were_looking_for(self):
        """现场里必须带上"我们当时在找哪张卡"，否则事后没法比对。"""
        runner = _runner()
        runner._tile_slices["t1"] = "someslice"
        runner._refs_map["t1"] = ["uuid-a", "uuid-b"]
        captured = {}
        with patch("integrations.google_fx.utils.forensics.capture",
                   lambda *a, **k: captured.update(k) or "/slot"):
            runner._capture_tile_lost(object(), {"idx": 3, "tile_id": "t1"})
        assert captured["extra"]["tile_id"] == "t1"
        assert captured["extra"]["prompt_slice"] == "someslice"
        assert captured["extra"]["refs"] == ["uuid-a", "uuid-b"]


if __name__ == "__main__":
    unittest.main()
