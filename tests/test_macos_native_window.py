"""AppKit window handling stays usable without System Events accessibility.

No test launches osascript or operates a real window.
"""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from integrations.google_fx.utils import macos_window


# Keep the wrapper available even when conftest disables native IO globally.
_REAL_JXA_WRAPPER = macos_window._jxa


@pytest.fixture(autouse=True)
def _isolated_native_windows(monkeypatch):
    def unexpected_io(*args, **kwargs):
        pytest.fail("native window unit test attempted unmocked subprocess or legacy UI scripting")

    monkeypatch.setattr(macos_window, "_HIDDEN_PIDS", set())
    monkeypatch.setattr(macos_window, "_HIDE_DEGRADED", False)
    monkeypatch.setattr(macos_window, "_ACCESSIBILITY_WARNED", False)
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(False, "", "unavailable")))
    monkeypatch.setattr(macos_window, "_osascript", Mock(side_effect=unexpected_io))
    monkeypatch.setattr(macos_window.subprocess, "run", unexpected_io)
    monkeypatch.setattr(macos_window, "browser_pid_from_ws", lambda ws: 4242)
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: True)


def test_native_hide_records_only_target_without_legacy_permissions(monkeypatch):
    native = Mock(return_value=(True, "true", ""))
    monkeypatch.setattr(macos_window, "_jxa", native)

    assert macos_window.hide_browser("ws://localhost:9222/devtools") is True
    assert macos_window._HIDDEN_PIDS == {4242}
    script = native.call_args.args[0]
    assert "runningApplicationWithProcessIdentifier(4242)" in script
    assert "app.hide" in script and "app.hide()" not in script
    macos_window._osascript.assert_not_called()


def test_native_false_result_falls_back_even_when_script_exits_successfully(monkeypatch):
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, "false", "")))
    legacy = Mock(return_value=(True, "", ""))
    monkeypatch.setattr(macos_window, "_osascript", legacy)

    assert macos_window.hide_browser("ws://localhost:9222/devtools") is True
    assert macos_window._HIDDEN_PIDS == {4242}
    legacy.assert_called_once()
    assert "unix id is 4242" in legacy.call_args.args[0]
    assert "to false" in legacy.call_args.args[0]


@pytest.mark.parametrize("output", ["", '"true"', "1", "null", "not json"])
def test_invalid_native_results_do_not_claim_hidden(monkeypatch, output):
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, output, "")))
    monkeypatch.setattr(macos_window, "_osascript", Mock(return_value=(False, "", "failed")))

    assert macos_window.hide_browser("ws://localhost:9222/devtools") is False
    assert macos_window._HIDDEN_PIDS == set()
    macos_window._osascript.assert_called_once()


def test_accessibility_denial_does_not_disable_later_native_success(monkeypatch):
    native = Mock(side_effect=[(True, "false", ""), (True, "true", "")])
    legacy = Mock(return_value=(False, "", "assistive access denied (-1719)"))
    monkeypatch.setattr(macos_window, "_jxa", native)
    monkeypatch.setattr(macos_window, "_osascript", legacy)

    assert macos_window.hide_browser("ws://localhost:9222/devtools") is False
    assert macos_window._HIDE_DEGRADED is True
    assert macos_window.hide_browser("ws://localhost:9222/devtools") is True
    assert macos_window._HIDDEN_PIDS == {4242}
    assert native.call_count == 2
    legacy.assert_called_once()


def test_degraded_legacy_path_still_tries_native_without_repeating_denied_ui(monkeypatch):
    monkeypatch.setattr(macos_window, "_HIDE_DEGRADED", True)

    assert macos_window.hide_browser("ws://localhost:9222/devtools") is False
    macos_window._jxa.assert_called_once()
    macos_window._osascript.assert_not_called()


@pytest.mark.parametrize("activate", [True, False])
def test_native_manual_reveal_respects_activation_choice(monkeypatch, activate):
    native = Mock(return_value=(True, "true", ""))
    monkeypatch.setattr(macos_window, "_jxa", native)
    macos_window._HIDDEN_PIDS.add(4242)

    assert macos_window.reveal_hidden(activate=activate) == 1
    scripts = [call.args[0] for call in native.call_args_list]
    assert "app.unhide" in scripts[0] and "app.unhide()" not in scripts[0]
    assert len(scripts) == (2 if activate else 1)
    if activate:
        assert "app.activateWithOptions(0)" in scripts[1]
    assert macos_window._HIDDEN_PIDS == {4242}
    macos_window._osascript.assert_not_called()


def test_native_rehide_recovers_despite_legacy_denial_and_forgets_dead_processes(monkeypatch):
    monkeypatch.setattr(macos_window, "_HIDE_DEGRADED", True)
    monkeypatch.setattr(macos_window, "_process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, "true", "")))
    macos_window._HIDDEN_PIDS.update({4242, 9999})

    assert macos_window.rehide_revealed() == 1
    assert macos_window._HIDDEN_PIDS == {4242}
    macos_window._jxa.assert_called_once()
    macos_window._osascript.assert_not_called()


def test_native_show_can_succeed_when_system_rejects_activation(monkeypatch):
    native = Mock(side_effect=[(True, "true", ""), (True, "false", "")])
    monkeypatch.setattr(macos_window, "_jxa", native)
    monkeypatch.setattr(macos_window, "_osascript", Mock(return_value=(False, "", "denied")))

    # The interface reports visibility, not a guarantee of foreground activation.
    assert macos_window.show_browser("ws://localhost:9222/devtools") is True
    assert "set frontmost of" in macos_window._osascript.call_args.args[0]


def test_native_frontmost_name_avoids_system_events(monkeypatch):
    name = 'My "Editor" \\ Work'
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, json.dumps(name), "")))

    assert macos_window.frontmost_app() == name
    macos_window._osascript.assert_not_called()


def test_frontmost_lookup_falls_back_when_native_data_is_invalid(monkeypatch):
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, "true", "")))
    monkeypatch.setattr(macos_window, "_osascript", Mock(return_value=(True, "Code", "")))

    assert macos_window.frontmost_app() == "Code"
    macos_window._osascript.assert_called_once()


def test_native_focus_restore_uses_exact_escaped_name_and_public_activation(monkeypatch):
    name = 'My "Editor" \\ Work'
    native = Mock(return_value=(True, "true", ""))
    monkeypatch.setattr(macos_window, "_jxa", native)

    assert macos_window.restore_focus(name) is True
    script = native.call_args.args[0]
    assert f"=== {json.dumps(name)}" in script
    assert "activateWithOptions(0)" in script
    assert "IgnoringOtherApps" not in script
    macos_window._osascript.assert_not_called()


def test_native_focus_refusal_uses_escaped_legacy_fallback(monkeypatch):
    monkeypatch.setattr(macos_window, "_jxa", Mock(return_value=(True, "false", "")))
    legacy = Mock(return_value=(True, "", ""))
    monkeypatch.setattr(macos_window, "_osascript", legacy)

    assert macos_window.restore_focus('My "Editor"') is True
    legacy.assert_called_once_with('tell application "My \\"Editor\\"" to activate')


def test_jxa_wrapper_sets_language_and_timeout_without_shell(monkeypatch):
    monkeypatch.setattr(macos_window.shutil, "which", lambda name: "/usr/bin/osascript")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=" false\n", stderr=""))
    monkeypatch.setattr(macos_window.subprocess, "run", run)

    assert _REAL_JXA_WRAPPER("script body") == (True, "false", "")
    run.assert_called_once_with(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", "script body"],
        capture_output=True, text=True, timeout=macos_window._OSASCRIPT_TIMEOUT,
    )


def test_jxa_timeout_returns_failure_for_legacy_fallback(monkeypatch):
    monkeypatch.setattr(macos_window.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(macos_window.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("osascript", 5)))

    ok, out, error = _REAL_JXA_WRAPPER("script body")
    assert ok is False
    assert out == ""
    assert "TimeoutExpired" in error
