# -*- coding: utf-8 -*-
"""Flow 换域名 + 换前端（Angular Material）之后，视频链必须还认得出画布。

2026-09-05 现场：Google 把 Flow 从 labs.google/fx/tools/flow 搬到
flow.google.com，画布卡片也从 ``div[data-tile-id]`` 换成了
``<flow-grid-tile-container>`` 且不再带任何稳定 id。两处变化合起来的表现，
就是用户报的那一条——**反复上传参考图，却一段视频都不生成**：

  * 点了 Generate、积分照扣、视频照生成，但"等一个新 tile 出现"永远等不到，
    每个任务都被判成 ``Generate 后未检测到新 tile``；
  * 整批判失败后进重试轮，而 ``page.url`` 里没有 labs.google，
    ``_ChunkRunner`` 记不下 project_url，于是每轮都走"新建项目"分支、
    清空跨轮缓存，把整批参考图重传一遍。

这里钉死修复后的行为，防止哪天又有人把域名/选择器写死回去。
"""

import ast
import re
import unittest
from unittest.mock import patch

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_image as I
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import browser as B


NEW_PROJECT_URL = "https://flow.google.com/aa11bb22-cc33-4d44-8e55-ff6677889900"
OLD_PROJECT_URL = "https://labs.google/fx/tools/flow/project/old-canvas"


# ── 1. 域名判定 ────────────────────────────────────────────────────────────

class TestIsFlowUrl(unittest.TestCase):
    def test_accepts_both_the_old_and_the_new_host(self):
        assert H.is_flow_url(OLD_PROJECT_URL)
        assert H.is_flow_url(NEW_PROJECT_URL)

    def test_rejects_everything_else(self):
        assert not H.is_flow_url("https://accounts.google.com/signin")
        assert not H.is_flow_url("")
        assert not H.is_flow_url(None)


# ── 2. 画布卡片的定位与盖戳 ────────────────────────────────────────────────

class TestTileCompatLayer(unittest.TestCase):
    def test_tile_selector_covers_the_new_angular_container(self):
        assert "flow-grid-tile-container" in H.FLOW_TILE_SELECTOR
        assert "div[data-tile-id]" in H.FLOW_TILE_SELECTOR   # 旧版不能丢

    def test_every_tile_scan_goes_through_the_compat_prelude(self):
        """谁再直接 querySelectorAll('div[data-tile-id]')，新版画布上就是空手而归。"""
        with open(H.__file__, encoding="utf-8") as fh:
            src = fh.read()
        offenders = re.findall(r"querySelectorAll\('div\[data-tile-id\]'\)", src)
        assert not offenders, "仍有 %d 处只认旧版卡片的全局扫描" % len(offenders)

    def test_stamped_ids_are_part_of_the_prelude(self):
        """新版卡片没有 id，由我们盖 data-spark-tile-id；反查必须认这个戳。"""
        assert "data-spark-tile-id" in H._FLOW_TILE_JS
        assert "sparkTiles" in H._FLOW_TILE_JS
        assert "sparkFindTile" in H._FLOW_TILE_JS

    def test_locator_matches_stamped_ids_and_new_containers(self):
        seen = {}

        class _Page:
            def locator(self, sel):
                seen["sel"] = sel
                return self

            @property
            def first(self):
                return self

        H.flow_tile_locator(_Page(), tile_id="spark_tile_abc", uuid="u-1")
        assert "[data-spark-tile-id='spark_tile_abc']" in seen["sel"]
        assert "flow-grid-tile-container:has(img[src*='u-1'])" in seen["sel"]

    def test_locator_without_any_handle_returns_none(self):
        assert H.flow_tile_locator(object(), tile_id="", uuid="") is None

    def test_snapshot_stamps_and_returns_ids(self):
        class _Page:
            def evaluate(self, js, *a):
                assert "sparkTiles()" in js and "sparkTileId" in js
                return {"ids": ["spark_tile_1", "spark_tile_2"],
                        "layer": 0, "mediaCount": 4}

        assert H.snapshot_flow_tile_ids(_Page()) == ["spark_tile_1", "spark_tile_2"]

    def test_snapshot_survives_a_dead_page(self):
        class _Page:
            def evaluate(self, js, *a):
                raise RuntimeError("Target page closed")

        assert H.snapshot_flow_tile_ids(_Page()) == []


# ── 2b. 画布 DOM 漂移的运行期告警 ──────────────────────────────────────────

class TestCanvasTileDriftSignal(unittest.TestCase):
    """"画布上有媒体图、却一层卡片选择器都不命中" = Flow 改版的精确指纹。

    2026-09-05 那次就是这个状态，但当时没人看这一眼，表现只剩一句
    "Generate 后未检测到新 tile"，于是整批重试、重传参考图、继续烧积分。
    """

    def _capture_logs(self, scan):
        lines = []
        with patch.object(H, "log", lambda msg, tag="": lines.append(msg)), \
             patch.object(H.selector_stats, "record_hit", lambda *a, **k: None):
            H.record_canvas_tile_layer(scan)
        return lines

    def test_media_present_but_no_layer_matches_is_shouted_about(self):
        lines = self._capture_logs({"ids": [], "layer": -1, "mediaCount": 6})
        assert any("Flow 改版的典型指纹" in l for l in lines), lines

    def test_empty_canvas_is_not_reported_as_drift(self):
        """刚新建的空项目画布本来就没有卡片，不能报成选择器失效。"""
        lines = self._capture_logs({"ids": [], "layer": -1, "mediaCount": 0})
        assert not any("指纹" in l for l in lines), lines

    def test_falling_back_to_a_later_layer_is_warned(self):
        lines = self._capture_logs({"ids": ["a"], "layer": 1, "mediaCount": 3})
        assert any("兜底选择器命中" in l for l in lines), lines

    def test_primary_layer_is_quiet(self):
        lines = self._capture_logs({"ids": ["a"], "layer": 0, "mediaCount": 3})
        assert lines == []

    def test_layer_hit_is_recorded_into_selector_stats(self):
        seen = []
        with patch.object(H.selector_stats, "record_hit",
                          lambda *a, **k: seen.append((a, k))):
            H.record_canvas_tile_layer({"ids": ["a"], "layer": 0, "mediaCount": 1})
        assert seen and seen[0][0][0] == "canvas_tile"

    def test_tile_selectors_come_from_ui_selectors(self):
        """探针探的和生产代码用的必须是同一张表——否则探针报绿也说明不了什么。"""
        from integrations.google_fx.ui_selectors import UI_SELECTORS
        assert list(H.FLOW_TILE_LAYERS) == UI_SELECTORS["google_fx"]["canvas_tile"]


# ── 3. 新建项目的成功判定 ──────────────────────────────────────────────────

class _NewProjectPage:
    """点一下按钮就跳到新项目页的假页面。"""

    def __init__(self, before, after):
        self.url = before
        self._after = after
        self.keyboard = self

    def locator(self, sel):
        return self

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return True

    def click(self, timeout=0):
        self.url = self._after

    def press(self, _key):
        pass


class TestNewProjectConfirmation(unittest.TestCase):
    def test_new_host_project_url_without_project_segment_counts_as_success(self):
        page = _NewProjectPage("https://flow.google.com/", NEW_PROJECT_URL)
        with patch.object(H, "random_sleep", lambda *a, **k: None):
            assert H._click_new_project_button(page) is True

    def test_old_style_project_url_still_counts(self):
        page = _NewProjectPage("https://labs.google/fx/tools/flow",
                               "https://labs.google/fx/tools/flow/project/abc")
        with patch.object(H, "random_sleep", lambda *a, **k: None):
            assert H._click_new_project_button(page) is True

    def test_staying_put_is_still_a_failure(self):
        """点了没反应不能报成功——否则整批任务会堆在同一张画布上。"""
        page = _NewProjectPage("https://flow.google.com/", "https://flow.google.com/")
        with patch.object(H, "random_sleep", lambda *a, **k: None):
            assert H._click_new_project_button(page, confirm_timeout=0.05) is False


# ── 4. 已经在 Flow 上的标签页不许被导回首页 ────────────────────────────────

class _Tab:
    def __init__(self, url):
        self.url = url
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    def bring_to_front(self):
        pass

    def close(self):
        pass


class _Ctx:
    def __init__(self, pages):
        self.pages = pages

    def new_page(self):
        raise AssertionError("不该新建标签页：已经有一个 Flow 页了")


class TestFindOrCreatePageKeepsTheFlowTab(unittest.TestCase):
    def test_a_tab_on_the_new_host_is_reused_as_is(self):
        tab = _Tab(NEW_PROJECT_URL)
        with patch.object(B, "random_sleep", lambda *a, **k: None), \
             patch.object(B, "_is_manageable_user_page", lambda p: True):
            got = B.find_or_create_page(_Ctx([tab]), H.FLOW_HOST_HINTS,
                                        auto_login=False)
        assert got is tab
        assert tab.gotos == [], "已经在 Flow 项目页上，绝不能再导航一次把画布丢掉"

    def test_a_tab_somewhere_else_is_still_navigated_to_flow(self):
        tab = _Tab("https://www.example.com/")
        with patch.object(B, "random_sleep", lambda *a, **k: None), \
             patch.object(B, "_is_manageable_user_page", lambda p: True):
            B.find_or_create_page(_Ctx([tab]), H.FLOW_HOST_HINTS, auto_login=False)
        assert tab.gotos == [H.FLOW_HOME_URL]


# ── 5. 重试轮不再重传整批参考图 ────────────────────────────────────────────

class _PreparePage:
    def __init__(self, url):
        self.url = url
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    def reload(self, **kw):
        pass


def _runner():
    return V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[object()],
                          all_slices={}, on_progress=None, cancel_check=None)


class TestPreparePageKeepsUploadedReferences(unittest.TestCase):
    def _prepare(self, runner, page, clicked_new=True, lands_on=None):
        def _new_project(_page):
            if clicked_new and lands_on:
                _page.url = lands_on      # 新建成功 = 跳到新项目页
            return clicked_new

        with patch.object(V, "ensure_flow_workspace", lambda p: None), \
             patch.object(V, "_click_new_project_button", _new_project), \
             patch.object(V, "_dismiss_unexpected_overlays", lambda *a, **k: False), \
             patch.object(V, "random_sleep", lambda *a, **k: None), \
             patch.object(V._ChunkRunner, "_wait_toolbar_ready", lambda *a, **k: True), \
             patch.object(V._ChunkRunner, "_snapshot_preexisting_tiles", lambda *a: None):
            runner._prepare_page(page)

    def test_project_url_is_recorded_on_the_new_host(self):
        """记不下 project_url，下一轮就会重走新建项目分支、把参考图重传一遍。"""
        runner = _runner()
        page = _PreparePage("https://flow.google.com/")
        self._prepare(runner, page, lands_on=NEW_PROJECT_URL)
        assert runner.project_url == NEW_PROJECT_URL

    def test_failed_new_project_does_not_discard_the_upload_cache(self):
        """新建项目失败 = 还站在同一张画布上，图都还在，缓存不能清。

        缓存本来就要过 _verify_cached_uploads 的画布 DOM 校验才敢复用，
        留着它最坏也只是多校验一次；清掉它则是每轮重传整批参考图。
        """
        runner = _runner()
        runner.path_to_uuid = {"/p/img_001.webp": "uuid-1"}
        page = _PreparePage("https://flow.google.com/")
        self._prepare(runner, page, clicked_new=False)
        assert runner.path_to_uuid == {"/p/img_001.webp": "uuid-1"}


    def test_the_workspace_home_is_never_recorded_as_the_batch_canvas(self):
        """2026-09-06 现场：新版 UI 上"新建项目"点了不生效是常态（真实日志里
        每一次真实跑批都有 `新建项目点击未生效`），于是流程"在当前页面继续"——
        而当前页面往往就是工作台首页。首页同样是 Flow 地址，旧判据 is_flow_url
        照单全收，把它记成了本批次的项目页。下一个分批 goto 回首页，Flow 在那里
        给的是另一张画布：参考图重传、上一轮提交的卡片认不回来，看到的现象就是
        "同一组任务还在开新画布"。
        """
        runner = _runner()
        page = _PreparePage("https://flow.google.com/")
        self._prepare(runner, page, clicked_new=False)
        assert runner.project_url is None

    def test_a_route_less_project_url_is_still_recorded(self):
        """新版项目页可能没有 /project/ 这一段，不能因此判成"没有画布"。"""
        runner = _runner()
        page = _PreparePage("https://flow.google.com/")
        self._prepare(runner, page, lands_on=NEW_PROJECT_URL)
        assert runner.project_url == NEW_PROJECT_URL

    def test_old_host_project_url_is_still_recorded(self):
        runner = _runner()
        page = _PreparePage("https://labs.google/fx/tools/flow")
        self._prepare(runner, page, lands_on=OLD_PROJECT_URL)
        assert runner.project_url == OLD_PROJECT_URL


# ── 5b. "这是不是一张画布" 的唯一判据 ──────────────────────────────────────

class TestFlowProjectUrlPredicate(unittest.TestCase):
    """三条链各判各的必然分叉，所以判据只有一处：utils.browser。"""

    def test_both_project_url_shapes_count_as_a_canvas(self):
        assert B.is_flow_project_url(NEW_PROJECT_URL)
        assert B.is_flow_project_url(
            "https://flow.google.com/project/1c9c7b51-9a72-4bac-a717-729fc0776ce1")
        assert B.is_flow_project_url(OLD_PROJECT_URL)

    def test_the_workspace_home_is_not_a_canvas(self):
        assert not B.is_flow_project_url("https://flow.google.com/")
        assert not B.is_flow_project_url("https://labs.google/fx/tools/flow")

    def test_a_blob_url_is_not_a_canvas(self):
        """blob:https://flow.google.com/<uuid> 和新版项目页长得一模一样。"""
        assert not B.is_flow_project_url(
            "blob:https://flow.google.com/e835e846-b54f-4d06-8b28-5bd563aaa2fa")

    def test_a_uuid_somewhere_else_is_not_a_canvas(self):
        assert not B.is_flow_project_url(
            "https://example.com/e835e846-b54f-4d06-8b28-5bd563aaa2fa")

    def test_the_id_identifies_the_canvas_across_both_shapes(self):
        pid = "aa11bb22-cc33-4d44-8e55-ff6677889900"
        assert B.flow_project_id(NEW_PROJECT_URL) == pid
        assert B.flow_project_id("https://flow.google.com/project/" + pid) == pid

    def test_every_chain_asks_this_one_function(self):
        """谁再用 `"/project/" in url` 自己判一遍，三条链就会重新分叉。"""
        for mod in (I, V):
            with self.subTest(module=mod.__name__):
                with open(mod.__file__, encoding="utf-8") as fh:
                    src = fh.read()
                assert '"/project/" in ' not in src, (
                    "%s 仍在用子串自判项目页" % mod.__name__)


# ── 6. 图片 / 帧链 ────────────────────────────────────────────────────────
#
# 视频链 2026-09-05 迁完了，图片链（google_fx_image）当天没动，于是 2026-09-06
# 复查时它整个还停在旧 UI 上：卡片扫描只认 div[data-tile-id]、画布复用只认
# "/fx/tools/flow" 这段旧路径、站内相对路径写死拼 https://labs.google。
# 上面第 2 节那道"不许直接扫 div[data-tile-id]"的守卫当时只读了 helpers 一个文件，
# 图片链的三处漏在外面——所以这一节的第一条就是把守卫的读取范围补齐。

_MIGRATED_MODULES = (H, I)


class TestImageChainUsesTheCompatLayer(unittest.TestCase):
    def test_no_module_scans_the_old_container_directly(self):
        """守卫要覆盖每个扫画布的模块，只盯 helpers 等于给图片链免检。"""
        for mod in _MIGRATED_MODULES:
            with self.subTest(module=mod.__name__):
                with open(mod.__file__, encoding="utf-8") as fh:
                    src = fh.read()
                offenders = re.findall(
                    r"querySelectorAll\('div\[data-tile-id\]'\)", src)
                assert not offenders, "%s 仍有 %d 处只认旧版卡片的全局扫描" % (
                    mod.__name__, len(offenders))

    def test_no_module_hardcodes_the_old_flow_address(self):
        """写死旧地址最轻是白饶一跳 301，最重是拼出跨站 URL 带错 cookie。

        注释和文档串里提旧域名是可以的（那是故障记录），所以这里走 AST，
        只看真正会被执行到的字符串字面量。
        """
        for mod in _MIGRATED_MODULES:
            with self.subTest(module=mod.__name__):
                with open(mod.__file__, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                docstrings = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.FunctionDef,
                                         ast.AsyncFunctionDef, ast.ClassDef)):
                        if ast.get_docstring(node, clean=False) is not None:
                            docstrings.add(id(node.body[0].value))
                offenders = [
                    node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "labs.google" in node.value
                    and id(node) not in docstrings
                ]
                assert not offenders, "%s 第 %s 行仍写死旧站地址" % (
                    mod.__name__, offenders)

    def test_image_tile_scans_carry_the_stamping_prelude(self):
        """新版卡片没有自带 id，不盖戳就没有 tile 基线可比，等于认不出新结果。"""
        with open(I.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert "sparkTiles()" in src
        assert "sparkTileId" in src
        assert "_FLOW_TILE_JS" in src


class _CanvasPage:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []

    def goto(self, url, timeout=None, **kwargs):
        self.goto_calls.append(url)
        self.url = url


class TestImageCanvasReuseOnTheNewHost(unittest.TestCase):
    """"在同一张画布上继续做任务"在新域名下必须照样成立。

    复用判定原来写的是 `"/fx/tools/flow" in current_url`。新域名的工作台是
    https://flow.google.com/，这一条恒假 —— 每个 chunk 都会整页重载去重开画布，
    i2i 续链的画布上下文和已挂载的参考图一起丢，参考图每批重传一遍。
    """

    def _patched(self, page, editor=True):
        return (
            patch.object(I, "random_sleep", lambda *a, **k: None),
            patch.object(I, "_find_fx_prompt_input",
                         lambda *a, **k: object() if editor else None),
            patch.object(I, "_click_new_project_button",
                         lambda _p: self.fail("不该重开画布")),
        )

    def test_route_less_new_host_workspace_is_reused_in_place(self):
        page = _CanvasPage("https://flow.google.com/")
        p1, p2, p3 = self._patched(page)
        with p1, p2, p3:
            assert I._open_image_flow_canvas(page) is None
        assert page.goto_calls == []

    def test_bound_new_host_project_is_reused_without_navigation(self):
        page = _CanvasPage(NEW_PROJECT_URL + "/project/canvas-1")
        p1, p2, p3 = self._patched(page)
        with p1, p2, p3:
            landed = I._open_image_flow_canvas(page, page.url)
        assert landed == page.url
        assert page.goto_calls == []

    def test_a_fresh_canvas_is_still_required_on_the_new_host(self):
        """首批任务不许认领浏览器里停着的上一个任务的画布（2026-08-05 事故）。"""
        page = _CanvasPage("https://flow.google.com/project/previous-task")
        created = {}

        def _new_project(p):
            created["clicked"] = True
            p.url = "https://flow.google.com/project/brand-new"
            return True

        with patch.object(I, "random_sleep", lambda *a, **k: None),              patch.object(I, "ensure_flow_workspace", lambda *a, **k: True),              patch.object(I, "_find_fx_prompt_input", lambda *a, **k: object()),              patch.object(I, "_click_new_project_button", _new_project):
            landed = I._open_image_flow_canvas(page, require_fresh_canvas=True)
        assert created.get("clicked")
        assert landed.endswith("/brand-new")
        assert page.goto_calls == [B.FLOW_HOME_URL]


class TestImageMediaUrlResolution(unittest.TestCase):
    def test_site_relative_src_resolves_against_the_current_host(self):
        assert I._absolute_media_url(
            "/media/x.png", "https://flow.google.com/project/a"
        ) == "https://flow.google.com/media/x.png"
        assert I._absolute_media_url(
            "/media/x.png", "https://labs.google/fx/tools/flow"
        ) == "https://labs.google/media/x.png"

    def test_unknown_host_falls_back_to_the_new_home(self):
        assert I._absolute_media_url("/media/x.png", "about:blank").startswith(
            "https://flow.google.com")

    def test_absolute_and_in_memory_srcs_are_unchanged(self):
        assert I._absolute_media_url("https://x/y.png", "") == "https://x/y.png"
        assert I._absolute_media_url("blob:https://flow.google.com/abc", "") == ""
        assert I._absolute_media_url("data:image/png;base64,AA", "") == ""

    def test_new_host_media_needs_cookies_so_it_goes_through_the_browser(self):
        """flow.google.com 的 getMediaUrlRedirect 交给无 cookie 的 requests 必然失败。"""
        for url in ("https://flow.google.com/getMediaUrlRedirect?id=1",
                    "https://labs.google/getMediaUrlRedirect?id=1"):
            with self.subTest(url=url):
                assert B.is_flow_url(url)


class TestImageChainReportsItsCanvasBack(unittest.TestCase):
    """开出画布却回传 None = 调用方记不下 project_url = 下一批重开一张。

    frame_generator 把 `_open_image_flow_canvas` 的返回值存进 canvas_session，
    后续 chunk 靠它复用同一张画布跑 i2i 续链。回传 None 时，同一组任务的每个
    分批都会重新走"新建画布"，续链每批断一次、参考图每批重传。
    """

    def _reuse(self, url):
        page = _CanvasPage(url)
        with patch.object(I, "random_sleep", lambda *a, **k: None),              patch.object(I, "_find_fx_prompt_input", lambda *a, **k: object()),              patch.object(I, "_click_new_project_button",
                          lambda _p: self.fail("不该重开画布")):
            return I._open_image_flow_canvas(page, url)

    def test_route_less_new_host_canvas_is_reported_back(self):
        assert self._reuse(NEW_PROJECT_URL) == NEW_PROJECT_URL

    def test_project_route_canvas_is_reported_back(self):
        url = "https://flow.google.com/project/1c9c7b51-9a72-4bac-a717-729fc0776ce1"
        assert self._reuse(url) == url

    def test_old_host_canvas_is_reported_back(self):
        assert self._reuse(OLD_PROJECT_URL) == OLD_PROJECT_URL


if __name__ == "__main__":
    unittest.main()
