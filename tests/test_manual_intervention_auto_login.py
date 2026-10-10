# -*- coding: utf-8 -*-
"""生成链路掉登录时的自愈行为（wait_out_manual_intervention 的自动登录前置）。

这是本功能真正要解决的场景：夜里跑批，账号掉登录，原本会停下来发横幅、等人工
最多 20 分钟，等不到就把整批任务判失败——"一掉登录就废一批"。

契约（顺序即优先级）：
1. 页面本来正常 → 什么都不做（既有行为，不能因为加了自动登录就多跑一趟探测）。
2. login_required + 自动登录成功 → 直接返回 True，**且一个事件都不发**。
3. 自动登录失败 → 原样退回既有的"发 detected → 轮询等人工"路径。
4. 验证码/安全检查等非登录拦截 → 压根不调自动登录（那些自动化处理不了）。
"""

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers


class _Page:
    """只实现 _probe_manual_intervention 用到的那点接口。

    blocked_states 是一个队列：每次探测弹出一个，模拟页面状态随时间变化。
    """

    def __init__(self, states):
        self.states = list(states)
        self.probes = 0

    def evaluate(self, script):
        self.probes += 1
        state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        return state


def _blocked(code="login_required", reason="accounts.google.com login page"):
    return {"code": code, "reason": reason, "sample": "sign in"}


@pytest.fixture
def events():
    captured = []

    def on_event(phase, code, reason, max_wait):
        captured.append((phase, code))

    on_event.captured = captured
    return on_event


# 等人工的轮询循环是 `while time.time() < deadline`，退出条件挂在**墙钟**上。
# 把 time.sleep 打成 no-op 只会让它空转满 max_wait_secs，所以这里保留真实
# sleep、只把节拍和预算一起调小（下面各用例传 max_wait_secs=0.4）。
_FAST_MAX_WAIT = 0.4


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    monkeypatch.setattr(helpers, "_MANUAL_INTERVENTION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(helpers, "_page_is_gone", lambda page: False)
    monkeypatch.setattr(helpers, "_check_cancelled", lambda: None)


def _patch_auto_login(monkeypatch, outcome):
    """替换 utils.browser.attempt_auto_login —— helpers 是在函数体里 import 它的，
    所以要打在源模块上。返回一个记录调用次数的列表。"""
    from integrations.google_fx.utils import browser as browser_utils
    calls = []

    def fake(page, user_id=None, context_label="", cancel_check=None):
        calls.append(context_label)
        return outcome

    monkeypatch.setattr(browser_utils, "attempt_auto_login", fake)
    return calls


def test_healthy_page_never_reaches_the_auto_login_path(monkeypatch, events):
    calls = _patch_auto_login(monkeypatch, True)
    page = _Page([None])

    assert helpers.wait_out_manual_intervention(page, on_event=events) is True
    assert calls == []
    assert events.captured == []


def test_successful_auto_login_resumes_without_bothering_the_user(monkeypatch, events):
    """自动恢复了却弹一个又立刻撤掉的横幅，只会让用户以为出了事故。
    整段自愈只留日志，不发任何前端事件。"""
    calls = _patch_auto_login(monkeypatch, True)
    # 第一次探测：被登录页拦住；自动登录之后的复检：干净了。
    page = _Page([_blocked(), None])

    assert helpers.wait_out_manual_intervention(page, on_event=events) is True
    assert len(calls) == 1
    assert events.captured == [], "自动恢复不该发 detected/cleared 事件"


def test_failed_auto_login_falls_back_to_the_existing_human_wait(monkeypatch, events):
    """自动登录是加在前面的一层机会，不是替换。失败必须原样退回等人工。"""
    calls = _patch_auto_login(monkeypatch, False)
    page = _Page([_blocked()])  # 一直被拦

    assert helpers.wait_out_manual_intervention(
        page, on_event=events, max_wait_secs=_FAST_MAX_WAIT) is False
    assert len(calls) == 1
    assert events.captured[0] == ("detected", "login_required")
    assert events.captured[-1] == ("timeout", "login_required")


def test_human_can_still_resolve_it_after_a_failed_auto_login(monkeypatch, events):
    calls = _patch_auto_login(monkeypatch, False)
    # 拦住 → 自动登录失败 → 发 detected → 人工在窗口里登好了 → 复检干净
    page = _Page([_blocked(), _blocked(), None])

    assert helpers.wait_out_manual_intervention(
        page, on_event=events, max_wait_secs=_FAST_MAX_WAIT) is True
    assert len(calls) == 1, "等人工期间不该反复重试自动登录（会绕开只提交一次密码的护栏）"
    assert ("cleared", "login_required") in events.captured


@pytest.mark.parametrize("code", ["captcha_required", "security_check",
                                  "verification_required", "onboarding_required"])
def test_non_login_blocks_do_not_invoke_auto_login(monkeypatch, events, code):
    """验证码/安全检查/设备验证自动化处理不了。在这些情况上调自动登录纯属浪费
    时间，还会在日志里留一行没有意义的"自动登录未成功"。"""
    calls = _patch_auto_login(monkeypatch, True)
    page = _Page([_blocked(code=code, reason=code)])

    helpers.wait_out_manual_intervention(page, on_event=events, max_wait_secs=_FAST_MAX_WAIT)

    assert calls == []
    assert events.captured[0] == ("detected", code)


def test_captcha_appearing_right_after_login_is_reported_under_its_own_code(monkeypatch, events):
    """登进去了但紧接着撞上验证码：横幅要说"验证码"，不能沿用旧的
    login_required——那会让用户跑去 AdsPower 里找一个并不存在的登录页。"""
    _patch_auto_login(monkeypatch, True)
    page = _Page([_blocked(), _blocked(code="captcha_required", reason="recaptcha")])

    helpers.wait_out_manual_intervention(page, on_event=events, max_wait_secs=_FAST_MAX_WAIT)

    assert events.captured[0] == ("detected", "captcha_required")
