# -*- coding: utf-8 -*-
"""
🧪 紧急换 IP 的触发条件 + 底部配置按钮自愈恢复
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯逻辑测试，页面用桩对象，不需要真实浏览器/AdsPower。

背景（2026-07-25 复盘）：一次"图片生成未找到底部配置按钮"在 server.log 里连打了
4 次强制换 IP，而整个过程一张图都没提交出去、一条生成报错都没有。两个独立的坑：
  1. 找不到配置按钮时只会干等 10 秒，救不了智能体模式/弹窗/没进项目页这三种
     "等不会好"的情况，也从不留现场；
  2. except 到任何异常都换 IP，UI 自动化自己的毛病也照换——换了没用，还会关掉
     浏览器重连，把 Flow 的登录 token 打失效。
"""



import pytest
from integrations.google_fx.services import google_fx_helpers as H


# ── 换 IP 判定 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "图片生成未找到底部配置按钮，无法确认配置，已停止生成",
    "无法找到输入框",
    "图片生成配置未选对，停止生成。当前状态: <空>",
    "图片生成配置未完成，停止生成。面板未确认项: model",
    "等待底部工具栏超时，未检测到可用输入框",
    "任务已取消",
    "请求超时：超出时间预算 (request budget exceeded)",
    "MANUAL_REQUIRED: 检测到需要人工处理",
    "AttributeError: 'NoneType' object has no attribute 'click'",
    "",
])
def test_ui_automation_failures_do_not_switch_account(message):
    """UI 自动化/脚本自身的失败：生成过程根本没报错，不该换 IP。"""
    should_switch, verdict = H._classify_failure_for_switch(message)
    assert should_switch is False, verdict


@pytest.mark.parametrize("message", [
    "所有 prompt 均未捕获到图片",
    "检测到异常活动 unusual activity",
    "net::ERR_TUNNEL_CONNECTION_FAILED",
    "429 Too Many Requests",
    "IP 被封",
    "代理连接失败",
])
def test_generation_side_failures_switch_account(message):
    """确实指向出口 IP / 账号风控的失败：照旧换 IP。"""
    should_switch, verdict = H._classify_failure_for_switch(message)
    assert should_switch is True, verdict


def test_unknown_error_defaults_to_no_switch(monkeypatch):
    monkeypatch.delenv("MIYA_ROTATE_ON_UNKNOWN_ERROR", raising=False)
    assert H._classify_failure_for_switch("某个从没见过的怪错误")[0] is False


def test_unknown_error_switches_when_opted_in(monkeypatch):
    monkeypatch.setenv("MIYA_ROTATE_ON_UNKNOWN_ERROR", "1")
    assert H._classify_failure_for_switch("某个从没见过的怪错误")[0] is True


def test_script_word_does_not_look_like_an_ip_error():
    """'script' 里含 'ip'——关键词表不能宽到把它当成 IP 类错误。"""
    assert H._classify_failure_for_switch("Playwright script raised somewhere")[0] is False


def test_switch_skipped_without_touching_pool(monkeypatch):
    """被判为非风控类时，连号池都不该碰（换号会关浏览器重连）。"""
    called = []
    monkeypatch.setattr(H, "log", lambda *a, **k: called.append("log"))
    assert H._switch_account_on_failure(reason="无法找到输入框") is None


def test_force_switch_bypasses_classification(monkeypatch):
    """调用方已确认是生成侧失败时，分类不该拦下来。"""
    seen = {}

    def fake_classify(reason):
        seen["classified"] = True
        return False, "不该被调用"

    monkeypatch.setattr(H, "_classify_failure_for_switch", fake_classify)
    monkeypatch.setattr(H, "log", lambda *a, **k: None)
    # 号池为空时换号会走到「没有可换的账号」分支返回 None，这里只关心没被分类拦下
    H._switch_account_on_failure(reason="批量生图部分失败 (1/3)", force_switch=True)
    assert "classified" not in seen


# ── 底部配置按钮自愈恢复 ──────────────────────────────────────────────────────

class _FakePage:
    """按剧本演进的假页面：某个恢复动作做完后配置按钮才出现。"""

    def __init__(self, url, appear_after=None):
        self._url = url
        self.appear_after = appear_after  # 'agent' | 'overlay' | 'project' | 'reload' | None
        self.has_btn = False
        self.events = []

    @property
    def url(self):
        return self._url

    def reload(self, timeout=None):
        self.events.append("reload")
        if self.appear_after == "reload":
            self.has_btn = True

    def goto(self, url, timeout=None):
        self.events.append("goto")
        self._url = url

    def evaluate(self, js, *args):
        if "projectLink" in js:
            self.events.append("click_project")
            self._url = "https://labs.google/fx/tools/flow/project/abc"
            if self.appear_after == "project":
                self.has_btn = True
            return True
        if "offsetParent" in js:  # _dump_visible_button_texts
            return ["Nano Banana 2", "Send"]
        return None

    def screenshot(self, path=None):
        self.events.append("screenshot")

    def content(self):
        return "<html></html>"


@pytest.fixture
def stub_recovery(monkeypatch):
    """把恢复流程里的等待/点击全部打桩，只保留控制流。"""
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    monkeypatch.setattr(H, "random_sleep", lambda a, b: None)
    monkeypatch.setattr(H, "find_fx_config_button",
                        lambda p: (("BTN", "Nano Banana 2 1x") if p.has_btn else (None, "")))
    monkeypatch.setattr(H, "_click_new_project_button",
                        lambda p: (p.events.append("new_project"), True)[1])

    def make(kind):
        def _fn(p, tag="GoogleFX"):
            p.events.append(f"dismiss_{kind}")
            if p.appear_after == kind:
                p.has_btn = True
                return True
            return False
        return _fn

    monkeypatch.setattr(H, "_dismiss_active_agent_mode", make("agent"))
    monkeypatch.setattr(H, "_dismiss_unexpected_overlays", make("overlay"))
    return monkeypatch


def test_recovers_from_agent_mode(stub_recovery):
    """智能体模式下底部工具栏被聊天框替换 —— 退出智能体即可恢复。"""
    page = _FakePage("https://labs.google/fx/tools/flow/project/x", appear_after="agent")
    btn, txt = H._recover_missing_fx_config_button(page, "图片生成")
    assert btn == "BTN" and txt == "Nano Banana 2 1x"
    assert "dismiss_agent" in page.events
    assert "reload" not in page.events  # 别为已经解决的问题白刷新一次


def test_recovers_from_blocking_overlay(stub_recovery):
    page = _FakePage("https://labs.google/fx/tools/flow/project/x", appear_after="overlay")
    btn, _ = H._recover_missing_fx_config_button(page, "图片生成")
    assert btn == "BTN"
    assert "dismiss_overlay" in page.events


def test_recovers_when_never_entered_project_page(stub_recovery):
    """工具栏"就绪"是被宽选择器骗的（首页的输入框），得重新进项目。"""
    page = _FakePage("https://labs.google/fx/tools/flow", appear_after="project")
    btn, _ = H._recover_missing_fx_config_button(page, "图片生成")
    assert btn == "BTN"
    assert "click_project" in page.events


def test_recovers_after_reload(stub_recovery):
    page = _FakePage("https://labs.google/fx/tools/flow/project/x", appear_after="reload")
    btn, _ = H._recover_missing_fx_config_button(page, "图片生成")
    assert btn == "BTN"
    assert "reload" in page.events


def test_unrecoverable_saves_crime_scene(stub_recovery):
    """全都救不回来时必须留档，否则下次照样无从查起。"""
    page = _FakePage("https://labs.google/fx/tools/flow/project/x", appear_after=None)
    btn, txt = H._recover_missing_fx_config_button(page, "图片生成")
    assert (btn, txt) == (None, "")
    assert page.events.count("reload") == 1
    assert "screenshot" in page.events


def test_recovery_stays_cancellable(stub_recovery, monkeypatch):
    """恢复流程最长要跑几十秒，中途取消必须能立刻中断。"""
    def boom():
        raise RuntimeError("任务已取消")

    monkeypatch.setattr(H, "_check_cancelled", boom)
    page = _FakePage("https://labs.google/fx/tools/flow/project/x")
    with pytest.raises(RuntimeError, match="任务已取消"):
        H._recover_missing_fx_config_button(page, "图片生成")
