# -*- coding: utf-8 -*-
"""2026-08-05 串图事故的回归覆盖：新任务绝不能跑进上一个任务的 Flow 画布。

事故经过：任务 1785929405589（悬崖石屋）与一小时前的任务 1785925223144（榕树树洞）
写着同一个 project 43a554bb。AdsPower 的浏览器跨任务不关，新任务连上来时页面还停在
旧任务的 /fx/tools/flow/project/43a554bb 上，而 _open_image_flow_canvas 判断"是不是
可复用的工作台"用的是 `"/fx/tools/flow" in current_url` —— 这个子串是**每一条**项目
路由的前缀，于是旧项目被当成工作台首页原地采纳。随后旧画布上的历史图 6b857588 在提交
后 1 秒被"网络捕获"，落盘成了新任务的 IMG 001。
"""

import inspect

import pytest

from integrations.google_fx.services import google_fx_image as image_service


PREV_TASK_PROJECT = (
    "https://labs.google/fx/tools/flow/project/43a554bb-4972-4eec-8ec9-a9f6f3d5ef0b"
)
FRESH_PROJECT = (
    "https://labs.google/fx/tools/flow/project/11111111-2222-4333-8444-555555555555"
)
WORKSPACE = "https://labs.google/fx/tools/flow"


class _StubPage:
    """够 _open_image_flow_canvas 用的最小 Page：一个 URL、一次 goto、一个画布计数。"""

    def __init__(self, url, media_tiles=0):
        self.url = url
        self.media_tiles = media_tiles
        self.goto_calls = []

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append(url)
        self.url = url

    def evaluate(self, script, arg=None):
        return self.media_tiles


def _patch_canvas_env(monkeypatch, *, new_project, prompt_input=True,
                      workspace_ready=True):
    monkeypatch.setattr(image_service, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        image_service, "ensure_flow_workspace",
        lambda *a, **k: workspace_ready,
    )
    monkeypatch.setattr(image_service, "_click_new_project_button", new_project)
    monkeypatch.setattr(
        image_service, "_find_fx_prompt_input",
        lambda *a, **k: object() if prompt_input else None,
    )


def test_new_task_never_adopts_the_previous_task_canvas(monkeypatch):
    """事故主线：页面停在旧任务项目上，新任务必须新建画布而不是就地采纳。"""
    page = _StubPage(PREV_TASK_PROJECT)

    def _new_project(target, confirm_timeout=10.0):
        target.url = FRESH_PROJECT
        return True

    _patch_canvas_env(monkeypatch, new_project=_new_project)

    assert image_service._open_image_flow_canvas(
        page, None, require_fresh_canvas=True
    ) == FRESH_PROJECT
    # 必须先回项目列表，不能在旧项目里就地开工
    assert page.goto_calls == [WORKSPACE]


def test_fresh_canvas_refuses_to_land_back_on_the_previous_project(monkeypatch):
    """Flow 把我们送回上一个项目时必须报错中止，绝不"先跑着看"。"""
    page = _StubPage(PREV_TASK_PROJECT)

    def _restores_previous_project(target, confirm_timeout=10.0):
        target.url = PREV_TASK_PROJECT
        return True

    _patch_canvas_env(monkeypatch, new_project=_restores_previous_project)

    with pytest.raises(RuntimeError, match="FLOW_CANVAS_UNAVAILABLE"):
        image_service._open_image_flow_canvas(page, None, require_fresh_canvas=True)


def test_route_less_fresh_canvas_must_be_empty(monkeypatch):
    """没有 /project/ 路由的 Flow 变体用不了 URL 自证，改由"画布必须是空的"把关。"""
    page = _StubPage(WORKSPACE, media_tiles=4)
    _patch_canvas_env(monkeypatch, new_project=lambda *a, **k: True)

    with pytest.raises(RuntimeError, match="历史媒体卡片"):
        image_service._open_image_flow_canvas(page, None, require_fresh_canvas=True)

    page = _StubPage(WORKSPACE, media_tiles=0)
    assert image_service._open_image_flow_canvas(
        page, None, require_fresh_canvas=True
    ) is None


def test_project_route_is_not_a_restored_workspace(monkeypatch):
    """子串判断的原始缺陷：项目路由不是"工作台首页"，不能被无请求地原地采纳。"""
    page = _StubPage(PREV_TASK_PROJECT)
    _patch_canvas_env(monkeypatch, new_project=lambda *a, **k: True)

    image_service._open_image_flow_canvas(page, None, require_fresh_canvas=False)

    # 早退分支若仍然命中就一次导航都不会发生，旧项目会被直接绑给调用方。
    assert page.goto_calls == [WORKSPACE]


def test_bound_project_is_still_reused_without_navigation(monkeypatch):
    """同一任务的后续 chunk 不受影响：请求的就是当前项目时依旧零导航复用。"""
    page = _StubPage(PREV_TASK_PROJECT)
    _patch_canvas_env(
        monkeypatch,
        new_project=lambda *a, **k: pytest.fail("已绑定画布不该重新新建"),
    )

    assert image_service._open_image_flow_canvas(
        page, PREV_TASK_PROJECT
    ) == PREV_TASK_PROJECT
    assert page.goto_calls == []


def test_dead_bound_project_falls_back_to_a_fresh_canvas(monkeypatch):
    """绑定项目打不开时，页面上剩下的画布是别人的东西，只能新建。"""
    page = _StubPage(PREV_TASK_PROJECT)

    def _new_project(target, confirm_timeout=10.0):
        target.url = FRESH_PROJECT
        return True

    dead_project = "https://labs.google/fx/tools/flow/project/dead-0000"
    _patch_canvas_env(monkeypatch, new_project=_new_project)
    # 死项目本身进不了工作台，回到项目列表之后才行——这正是这条兜底存在的原因。
    monkeypatch.setattr(
        image_service, "ensure_flow_workspace",
        lambda target, **k: target.url != dead_project,
    )

    assert image_service._open_image_flow_canvas(page, dead_project) == FRESH_PROJECT
    # 换新画布之前必须先重试进入绑定画布（Flow 的崩溃页多半是瞬时的）。
    assert page.goto_calls == [dead_project] * 3 + [WORKSPACE]


def test_flow_project_id_extraction():
    assert image_service._flow_project_id(PREV_TASK_PROJECT) == \
        "43a554bb-4972-4eec-8ec9-a9f6f3d5ef0b"
    assert image_service._flow_project_id(WORKSPACE) == ""
    assert image_service._flow_project_id(None) == ""


def test_first_batch_of_a_task_requests_a_fresh_canvas():
    """SPARK 侧必须在任务的第一批置位 require_fresh_canvas，否则修的只是半条链路。"""
    source = inspect.getsource(__import__("frame_generator")._fx_generate_batch)
    assert "require_fresh_canvas=require_fresh_canvas" in source
    # 判据是"本任务在这个账号上还没开过画布"，不是"这次没传 project_url"——
    # 无 /project/ 路由的变体永远拿不到 URL，只按 URL 判会让每个 chunk 都重开画布。
    assert "opened_accounts" in source


def test_account_switch_drops_the_previous_accounts_canvas_binding():
    source = inspect.getsource(image_service._generate_images_batch_google_fx_unlocked)
    switch_branch = source[source.index("_switch_account_on_failure("):]
    assert "req.project_url = None" in switch_branch
    assert "req.require_fresh_canvas = True" in switch_branch


def test_prepare_canvas_never_reopens_history_for_a_fresh_task():
    from integrations.google_fx.services import google_fx_helpers as helpers
    source = inspect.getsource(helpers._prepare_fx_canvas)
    fresh_branch = source[source.index("if not toolbar_exists and require_fresh_canvas"):]
    fresh_branch = fresh_branch[:fresh_branch.index("elif not toolbar_exists")]
    # "优先打开最新历史项目"那条兜底在这条分支里必须完全不存在——
    # 最新历史项目正是上一个任务的画布。
    assert "/project/" not in fresh_branch
    assert "_click_new_project_button" in fresh_branch


# ── 2026-08-17 现场：绑定画布撞崩溃页，整条帧链被换到空白新画布上 ──
# AdsPower 每个请求都会把 profile 重新拉起，绑定的项目页于是常在冷加载时撞上
# Flow 的 "Something went wrong"。ensure_flow_workspace 里的崩溃恢复会把页面弹回
# **工作台列表**然后返回 True，而这里原来只看 workspace_ready——"回到列表"被当成
# "已进入绑定画布"，找不到编辑器就去点 New project。日志里的表现是每帧一个新
# project_url，加上一串「画布上挂不到该参考图，改用上传方式」。

class _CrashOncePage(_StubPage):
    """第一次进项目给崩溃页，reload 之后才正常——真机上最常见的形态。"""

    def __init__(self, url, crash_times=1):
        super().__init__(url)
        self.crash_left = crash_times
        self.reload_calls = 0
        self.crashed = False

    def goto(self, url, timeout=None, wait_until=None):
        super().goto(url, timeout=timeout, wait_until=wait_until)
        self.crashed = self.crash_left > 0

    def reload(self, timeout=None, wait_until=None):
        self.reload_calls += 1
        if self.crash_left > 0:
            self.crash_left -= 1
        self.crashed = self.crash_left > 0


def test_bound_canvas_survives_a_transient_crash_page(monkeypatch):
    page = _CrashOncePage(WORKSPACE)  # 崩溃恢复已经把我们弹回了工作台列表
    _patch_canvas_env(
        monkeypatch,
        new_project=lambda *a, **k: pytest.fail("崩溃页是瞬时的，不该换新画布"),
    )
    monkeypatch.setattr(image_service, "_flow_project_crashed", lambda p: p.crashed)

    assert image_service._open_image_flow_canvas(page, PREV_TASK_PROJECT) == PREV_TASK_PROJECT
    assert page.reload_calls == 1
    assert page.goto_calls == [PREV_TASK_PROJECT]


def test_bounced_back_to_workspace_list_is_not_a_successful_entry(monkeypatch):
    """崩溃恢复把我们留在工作台列表时，绝不能当作"已进入绑定画布"。"""
    page = _StubPage(WORKSPACE)
    created = []

    def _new_project(target, confirm_timeout=10.0):
        created.append(target.url)
        target.url = FRESH_PROJECT
        return True

    _patch_canvas_env(monkeypatch, new_project=_new_project)
    monkeypatch.setattr(image_service, "_flow_project_crashed", lambda p: False)
    # goto 之后 URL 仍停在工作台列表 = Flow 把我们弹了回来
    monkeypatch.setattr(_StubPage, "goto", lambda self, url, **k: self.goto_calls.append(url))

    result = image_service._open_image_flow_canvas(page, PREV_TASK_PROJECT)

    # 重试用尽后才允许新建，且新建必须走"回项目列表"的正规路径
    assert page.goto_calls == [PREV_TASK_PROJECT] * 3 + [WORKSPACE]
    assert result == FRESH_PROJECT
