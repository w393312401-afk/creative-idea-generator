# -*- coding: utf-8 -*-
"""
🧪 提交节奏闸门 + 死页面快速失败
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯逻辑测试，页面用桩对象，不需要真实浏览器/AdsPower。

背景（2026-07-26 对 server.log 做的耗时归因）：
  1. 每次进批量生图前无条件 sleep(15~25s)，21 次共睡掉 376s——而风控看的是两次
     **提交**之间的间隔，不是脚本启动前干等了多久，一条腿跑完往往已过去几分钟。
  2. 浏览器被关掉之后 _wait_for_fx_toolbar 拿着死 page 一秒一轮空转到超时，
     177 次「人工接管状态检测失败: TargetClosedError」＝ 180s 纯空转。
"""

import time


import pytest
from integrations.google_fx.services import google_fx_helpers as H


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(H, "log", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_submit_ts(monkeypatch):
    monkeypatch.setattr(H, "_LAST_FX_SUBMIT_TS", 0.0)


# ── 提交节奏闸门 ──────────────────────────────────────────────────────────────

def test_no_wait_before_the_first_submit_of_the_process():
    """本进程还没提交过任何东西，没有需要拉开的间隔——不该睡。"""
    slept = []
    assert H.fx_pacing_wait(15, 25) == 0.0
    assert not slept


def test_no_wait_when_previous_submit_is_already_old_enough(monkeypatch):
    """上一条腿跑了几分钟，间隔早就够了——一秒都不该补。"""
    monkeypatch.setattr(H, "_LAST_FX_SUBMIT_TS", time.time() - 300)
    monkeypatch.setattr(H.time, "sleep", lambda s: pytest.fail(f"不该睡 {s}s"))
    assert H.fx_pacing_wait(15, 25) == 0.0


def test_only_the_remaining_gap_is_slept(monkeypatch):
    """刚提交完就再来一发：只补差额，不是重新睡满一整轮。"""
    slept = []
    monkeypatch.setattr(H, "_LAST_FX_SUBMIT_TS", time.time() - 10)
    monkeypatch.setattr(H.time, "sleep", slept.append)
    # 目标间隔固定成 20s，已过 10s → 只补 ~10s
    monkeypatch.setattr(H.random, "uniform", lambda a, b: 20.0)
    waited = H.fx_pacing_wait(15, 25)
    assert 9.0 <= waited <= 10.5, waited
    # 可取消 sleep 会切成短片轮询取消标志；总等待量仍应只补剩余差额。
    assert slept and 9.0 <= sum(slept) <= 10.5


def test_note_fx_submit_arms_the_gate(monkeypatch):
    """记录一次提交后，紧接着的下一次调用必须重新拉开间隔。"""
    slept = []
    monkeypatch.setattr(H.time, "sleep", slept.append)
    monkeypatch.setattr(H.random, "uniform", lambda a, b: 20.0)
    H.note_fx_submit()
    waited = H.fx_pacing_wait(15, 25)
    assert waited > 19.0, waited
    assert slept


# ── 死页面快速失败 ────────────────────────────────────────────────────────────

class _ClosedPage:
    def is_closed(self):
        return True


class _DeadPage:
    """连 is_closed() 都抛（CDP 连接整个没了）——同样必须当作已消失。"""
    def is_closed(self):
        raise RuntimeError("Target page, context or browser has been closed")


class _LivePage:
    def is_closed(self):
        return False


@pytest.mark.parametrize("page", [_ClosedPage(), _DeadPage()])
def test_page_is_gone_detects_closed_and_dead(page):
    assert H._page_is_gone(page) is True


def test_page_is_gone_false_for_live_page():
    assert H._page_is_gone(_LivePage()) is False


def test_toolbar_wait_fails_fast_instead_of_spinning(monkeypatch):
    """页面已关：立刻报错，不该等满 timeout（原来是一秒一轮空转到底）。"""
    monkeypatch.setattr(H, "_dismiss_unexpected_overlays", lambda *a, **k: None)
    monkeypatch.setattr(H, "_dismiss_active_agent_mode", lambda *a, **k: None)
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda *a, **k: None)
    monkeypatch.setattr(
        H, "_raise_if_manual_intervention_required",
        lambda *a, **k: pytest.fail("页面已关就不该再去探人工接管状态"),
    )
    slept = []
    monkeypatch.setattr(H.time, "sleep", slept.append)

    with pytest.raises(RuntimeError, match="浏览器/标签页已关闭"):
        H._wait_for_fx_toolbar(_ClosedPage(), timeout=60)
    assert not slept, "不该进入 1s 一轮的空转"


def test_dead_page_failure_does_not_trigger_account_switch():
    """这是 UI 自动化侧的失败，换号既救不了还会把登录 token 打松。"""
    should_switch, verdict = H._classify_failure_for_switch(
        "等待底部工具栏时浏览器/标签页已关闭，停止等待"
    )
    assert should_switch is False, verdict
