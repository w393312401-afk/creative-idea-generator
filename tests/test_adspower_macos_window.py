# -*- coding: utf-8 -*-
"""macOS 窗口抑制：屏幕外坐标在 Mac 上无效，改用 app 级隐藏 + 归还焦点。

覆盖三件事：模式解析、隐藏/恢复的记账、以及人工接管时窗口会被翻出来再藏回去。
osascript / lsof 全部打桩，测试不碰真实窗口。
"""
import os
import pytest

from integrations.google_fx.config import get_runtime_adspower_macos_window_mode
from integrations.google_fx.utils import macos_window


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """每个用例都从干净的模块级状态开始（隐藏记账 / 降级开关是全局的）。"""
    monkeypatch.setattr(macos_window, "_HIDDEN_PIDS", set())
    monkeypatch.setattr(macos_window, "_HIDE_DEGRADED", False)
    monkeypatch.setattr(macos_window, "_ACCESSIBILITY_WARNED", False)
    yield


class _ScriptRecorder:
    """记录所有 osascript 调用，并可指定返回值。"""

    def __init__(self, ok=True, stderr=""):
        self.calls = []
        self.ok = ok
        self.stderr = stderr

    def __call__(self, script):
        self.calls.append(script)
        return self.ok, "", self.stderr

    def visibility_calls(self):
        return [s for s in self.calls if "set visible of" in s]


# ── 配置解析 ────────────────────────────────────────────────────────────

def test_mac_window_mode_defaults_to_hide(monkeypatch):
    monkeypatch.delenv("ADSPOWER_MACOS_WINDOW_MODE", raising=False)
    assert get_runtime_adspower_macos_window_mode() == "hide"


@pytest.mark.parametrize("value", ["hide", "focus", "off"])
def test_mac_window_mode_accepts_valid_modes(monkeypatch, value):
    monkeypatch.setenv("ADSPOWER_MACOS_WINDOW_MODE", value)
    assert get_runtime_adspower_macos_window_mode() == value


def test_mac_window_mode_rejects_garbage(monkeypatch):
    """非法值不能让浏览器裸奔——兜底回 hide，而不是当成 off。"""
    monkeypatch.setenv("ADSPOWER_MACOS_WINDOW_MODE", "minimise-please")
    assert get_runtime_adspower_macos_window_mode() == "hide"


# ── 隐藏 / 记账 ─────────────────────────────────────────────────────────

def test_hide_browser_hides_by_pid_and_restores_focus(monkeypatch):
    rec = _ScriptRecorder()
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "browser_pid_from_ws", lambda ws: 4242)

    assert macos_window.hide_browser("ws://127.0.0.1:9222/devtools", "Code") is True
    assert 4242 in macos_window._HIDDEN_PIDS

    hide_calls = rec.visibility_calls()
    assert len(hide_calls) == 1
    assert "unix id is 4242" in hide_calls[0]
    assert "to false" in hide_calls[0]
    # 焦点必须还回去，否则用户打字仍会被打断
    assert any('tell application "Code" to activate' in s for s in rec.calls)


def test_hide_browser_degrades_when_accessibility_denied(monkeypatch):
    """没有辅助功能权限时降级成"只归还焦点"，且不抛异常、不阻塞任务。"""
    rec = _ScriptRecorder(ok=False, stderr="execution error: ... (-1719)")
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "browser_pid_from_ws", lambda ws: 4242)

    assert macos_window.hide_browser("ws://127.0.0.1:9222/x", "Code") is False
    assert 4242 not in macos_window._HIDDEN_PIDS
    assert macos_window._HIDE_DEGRADED is True

    # 降级后不再重复尝试隐藏，只归还焦点
    rec.calls.clear()
    macos_window.hide_browser("ws://127.0.0.1:9222/x", "Code")
    assert rec.visibility_calls() == []


def test_hide_browser_without_pid_still_restores_focus(monkeypatch):
    """端口反查不到 PID（lsof 缺失等）时不能炸，焦点照样还回去。"""
    rec = _ScriptRecorder()
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "browser_pid_from_ws", lambda ws: None)

    assert macos_window.hide_browser("ws://127.0.0.1:9222/x", "Code") is False
    assert rec.visibility_calls() == []
    assert any("activate" in s for s in rec.calls)


# ── 人工接管：翻出来 / 藏回去 ──────────────────────────────────────────

def test_reveal_hidden_shows_and_fronts_live_processes(monkeypatch):
    rec = _ScriptRecorder()
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: True)
    macos_window._HIDDEN_PIDS.update({101, 102})

    assert macos_window.reveal_hidden() == 2
    shown = [s for s in rec.visibility_calls() if "to true" in s]
    assert len(shown) == 2
    # 显示之后还要切到最前，否则窗口可能仍压在别的应用底下
    assert len([s for s in rec.calls if "set frontmost of" in s]) == 2


def test_reveal_hidden_skips_dead_processes(monkeypatch):
    """浏览器已经关掉的 PID 不该再去 osascript 折腾。"""
    rec = _ScriptRecorder()
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: pid == 101)
    macos_window._HIDDEN_PIDS.update({101, 999})

    assert macos_window.reveal_hidden() == 1
    assert all("unix id is 999" not in s for s in rec.calls)


def test_rehide_revealed_hides_again_and_returns_focus(monkeypatch):
    rec = _ScriptRecorder()
    monkeypatch.setattr(macos_window, "_osascript", rec)
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: True)
    macos_window._HIDDEN_PIDS.add(101)

    assert macos_window.rehide_revealed("Code") == 1
    assert any("to false" in s for s in rec.visibility_calls())
    assert any('tell application "Code" to activate' in s for s in rec.calls)


def test_rehide_forgets_dead_pids(monkeypatch):
    monkeypatch.setattr(macos_window, "_osascript", _ScriptRecorder())
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: False)
    macos_window._HIDDEN_PIDS.add(999)

    assert macos_window.rehide_revealed() == 0
    assert 999 not in macos_window._HIDDEN_PIDS


# ── PID 反查 ────────────────────────────────────────────────────────────

def test_browser_pid_from_ws_parses_lsof(monkeypatch):
    class _Proc:
        stdout = "4242\n"

    monkeypatch.setattr(macos_window.shutil, "which", lambda name: "/usr/sbin/lsof")
    monkeypatch.setattr(macos_window.subprocess, "run", lambda *a, **k: _Proc())
    assert macos_window.browser_pid_from_ws("ws://127.0.0.1:9222/devtools/browser/x") == 4242


def test_browser_pid_from_ws_without_port_returns_none():
    assert macos_window.browser_pid_from_ws("not-a-url") is None
