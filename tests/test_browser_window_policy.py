"""Background generation must not steal focus, including browser reuse.

All browser, network, and macOS window operations are test doubles.
"""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

from integrations.google_fx.utils import browser, cancel_flag, macos_window, proxy_rotator


@pytest.fixture(autouse=True)
def _offline_window_policy(monkeypatch):
    def unexpected_io(*args, **kwargs):
        pytest.fail("window policy test attempted real network or macOS window IO")

    monkeypatch.setattr(browser.requests.sessions.Session, "send", unexpected_io)
    monkeypatch.setattr(macos_window, "_osascript", unexpected_io)
    monkeypatch.setattr(type(cancel_flag), "is_cancelled", property(lambda self: False))


@pytest.mark.parametrize(
    "silent,headless,expected_front",
    [(True, False, False), (True, True, False), (False, True, False), (False, False, True)],
)
def test_page_focus_respects_background_settings(monkeypatch, silent, headless, expected_front):
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: silent)
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: headless)
    page = Mock()

    assert browser.bring_page_to_front_if_allowed(page) is expected_front
    assert page.bring_to_front.call_count == int(expected_front)


def test_page_focus_uses_current_setting_after_user_changes_mode(monkeypatch):
    settings = {"silent": True}
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: settings["silent"])
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: False)
    page = Mock()

    assert browser.bring_page_to_front_if_allowed(page) is False
    settings["silent"] = False
    assert browser.bring_page_to_front_if_allowed(page) is True
    settings["silent"] = True
    assert browser.bring_page_to_front_if_allowed(page) is False
    page.bring_to_front.assert_called_once_with()


@pytest.mark.parametrize("headless,expected_front", [(False, True), (True, False)])
def test_explicit_manual_takeover_can_reveal_silent_browser_but_not_headless(
    monkeypatch, headless, expected_front
):
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: True)
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: headless)
    page = Mock()

    assert browser.bring_page_to_front_if_allowed(page, force=True) is expected_front
    assert page.bring_to_front.call_count == int(expected_front)


def test_page_focus_preserves_browser_errors_when_foreground_is_enabled(monkeypatch):
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: False)
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: False)
    page = Mock()
    closed = RuntimeError("Target page has been closed")
    page.bring_to_front.side_effect = closed

    with pytest.raises(RuntimeError) as raised:
        browser.bring_page_to_front_if_allowed(page)

    assert raised.value is closed


@pytest.mark.parametrize(
    "is_mac,silent,headless,window_mode,should_hide",
    [
        (True, True, False, "hide", True),
        (True, False, False, "hide", False),
        (True, True, True, "hide", False),
        (True, True, False, "focus", False),
        (True, True, False, "off", False),
        (False, True, False, "hide", False),
    ],
)
def test_browser_reuse_applies_hide_mode_without_stealing_focus_or_restarting(
    monkeypatch, is_mac, silent, headless, window_mode, should_hide
):
    ws_url = "ws://127.0.0.1:9222/devtools/browser/existing"
    monkeypatch.setattr(browser, "IS_MAC", is_mac)
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: silent)
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: headless)
    monkeypatch.setattr(browser, "get_runtime_adspower_macos_window_mode", lambda: window_mode)
    lookup = Mock(return_value=ws_url)
    monkeypatch.setattr(browser, "get_running_ads_ws_url", lookup)
    # A missing accessibility permission can make suppression return False.
    # Reusing a healthy browser must still succeed in that case.
    suppress = Mock(return_value=False)
    monkeypatch.setattr(browser, "suppress_browser_window", suppress)

    def unexpected_restart_or_focus(*args, **kwargs):
        pytest.fail("reuse must not restart, rotate the proxy, or capture/restore focus")

    monkeypatch.setattr(browser, "build_adspower_launch_args", unexpected_restart_or_focus)
    monkeypatch.setattr(proxy_rotator, "ProxyRotator", unexpected_restart_or_focus)
    monkeypatch.setattr(browser, "_macos_frontmost_app", unexpected_restart_or_focus)
    monkeypatch.setattr(macos_window, "restore_focus", unexpected_restart_or_focus)

    result = browser._start_or_reuse_ads_browser(
        "existing-account", 50325, auto_rotate_proxy=True, max_start_attempts=3, start_timeout=45
    )

    assert result == ws_url
    lookup.assert_called_once_with("existing-account", port=50325)
    if should_hide:
        suppress.assert_called_once_with(ws_url, hide=True)
    else:
        suppress.assert_not_called()


def test_all_automatic_page_fronting_goes_through_window_policy():
    """A new service must not accidentally bypass the shared background policy."""
    package_root = Path(browser.__file__).resolve().parents[1]
    direct_calls = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        policy_calls = {
            id(node)
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and function.name == "bring_page_to_front_if_allowed"
            for node in ast.walk(function)
        } if path.resolve() == Path(browser.__file__).resolve() else set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "bring_to_front"
            ):
                direct_calls.append((path.relative_to(package_root), node.lineno))
                assert id(node) in policy_calls, (
                    f"{path.relative_to(package_root)}:{node.lineno} bypasses background window policy"
                )
    assert direct_calls, "foreground mode must retain an explicit page-fronting operation"
