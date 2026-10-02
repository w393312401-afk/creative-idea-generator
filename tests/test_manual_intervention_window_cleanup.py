"""Manual intervention may foreground the browser, but must always release it.

The browser page, macOS window operations, clock, and network are all isolated.
The real foreground policy is exercised through a fake page so silent mode and
headless mode retain their distinct behavior during an explicit human handoff.
"""

from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers
from integrations.google_fx.utils import browser, forensics, macos_window


class _Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def handoff(monkeypatch, offline_fx_video_io):
    state = SimpleNamespace(
        calls=[],
        shown=1,
        headless=False,
        previous_app="Code",
        probes=[("captcha_required", "verification needed"), None],
    )
    state.page = SimpleNamespace(
        bring_to_front=lambda: state.calls.append(("front",)),
    )

    def probe(page, context_label):
        assert page is state.page
        return state.probes.pop(0) if len(state.probes) > 1 else state.probes[0]

    def reveal():
        state.calls.append(("reveal", state.shown))
        return state.shown

    def rehide(previous_app):
        state.calls.append(("rehide", previous_app))
        return state.shown

    def event(phase, code, reason, max_wait_secs):
        state.calls.append(("event", phase))

    def forbidden_ui(*args, **kwargs):
        pytest.fail("manual intervention test attempted real macOS UI access")

    monkeypatch.setattr(helpers, "time", _Clock())
    monkeypatch.setattr(helpers, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(helpers, "_MANUAL_INTERVENTION_POLL_SECONDS", 1)
    monkeypatch.setattr(helpers, "_probe_manual_intervention", probe)
    monkeypatch.setattr(helpers, "_page_is_gone", lambda page: False)
    monkeypatch.setattr(helpers, "_check_cancelled", lambda: None)
    monkeypatch.setattr(browser, "get_runtime_adspower_silent_mode", lambda: True)
    monkeypatch.setattr(browser, "get_runtime_adspower_headless", lambda: state.headless)
    monkeypatch.setattr(browser, "reveal_hidden_browser_windows", reveal)
    monkeypatch.setattr(browser, "rehide_browser_windows", rehide)
    monkeypatch.setattr(macos_window, "frontmost_app", lambda: state.previous_app)
    monkeypatch.setattr(
        macos_window, "restore_focus",
        lambda previous_app: state.calls.append(("restore_focus", previous_app)) or True,
    )
    monkeypatch.setattr(macos_window, "_osascript", forbidden_ui)
    monkeypatch.setattr(forensics, "capture", lambda *args, **kwargs: state.calls.append(("capture",)))

    state.run = lambda **kwargs: helpers.wait_out_manual_intervention(
        state.page, on_event=event, max_wait_secs=3, **kwargs,
    )
    return state


def _assert_rehidden_once(state):
    assert state.calls.count(("reveal", 1)) == 1
    assert state.calls.count(("rehide", "Code")) == 1
    assert state.calls.index(("reveal", 1)) < state.calls.index(("rehide", "Code"))


def test_silent_manual_handoff_reveals_then_rehides_after_recovery(handoff):
    assert handoff.run() is True

    _assert_rehidden_once(handoff)
    assert ("event", "detected") in handoff.calls
    assert ("event", "cleared") in handoff.calls
    assert ("front",) not in handoff.calls, "reveal already foregrounded the hidden browser"
    assert not any(call[0] == "restore_focus" for call in handoff.calls)


def test_silent_manual_handoff_foregrounds_page_when_no_window_was_hidden(handoff):
    handoff.shown = 0

    assert handoff.run() is True

    assert handoff.calls.count(("front",)) == 1
    assert handoff.calls.index(("reveal", 0)) < handoff.calls.index(("front",))
    assert handoff.calls.count(("restore_focus", "Code")) == 1
    assert handoff.calls.index(("front",)) < handoff.calls.index(("restore_focus", "Code"))
    assert not any(call[0] == "rehide" for call in handoff.calls)


def test_headless_manual_handoff_does_not_try_to_foreground_page(handoff):
    handoff.shown = 0
    handoff.headless = True

    assert handoff.run() is True

    assert ("front",) not in handoff.calls
    assert not any(call[0] == "restore_focus" for call in handoff.calls)


@pytest.mark.parametrize("cancel_source", ["global", "predicate_true", "predicate_raises"])
def test_forced_foreground_is_released_after_cancellation(handoff, monkeypatch, cancel_source):
    handoff.shown = 0

    def cancelled():
        raise RuntimeError("cancelled")

    kwargs = {}
    expected_error = RuntimeError
    if cancel_source == "global":
        monkeypatch.setattr(helpers, "_check_cancelled", cancelled)
    elif cancel_source == "predicate_true":
        kwargs["cancel_check"] = lambda: True
        expected_error = ConnectionError
    else:
        kwargs["cancel_check"] = cancelled

    with pytest.raises(expected_error):
        handoff.run(**kwargs)

    assert handoff.calls.count(("front",)) == 1
    assert handoff.calls.count(("restore_focus", "Code")) == 1
    assert handoff.calls.index(("front",)) < handoff.calls.index(("restore_focus", "Code"))
    assert not any(call[0] == "rehide" for call in handoff.calls)


def test_forced_foreground_without_previous_app_does_not_guess_focus_target(handoff):
    handoff.shown = 0
    handoff.previous_app = ""

    assert handoff.run() is True

    assert handoff.calls.count(("front",)) == 1
    assert not any(call[0] == "restore_focus" for call in handoff.calls)


@pytest.mark.parametrize("message", ["任务已取消", "请求超时：超出时间预算"])
def test_global_cancel_or_request_timeout_rehides_before_propagating(
    handoff, monkeypatch, message,
):
    error = RuntimeError(message)

    def cancelled():
        raise error

    monkeypatch.setattr(helpers, "_check_cancelled", cancelled)

    with pytest.raises(RuntimeError) as caught:
        handoff.run()

    assert caught.value is error
    _assert_rehidden_once(handoff)
    assert ("event", "cleared") not in handoff.calls
    assert ("event", "timeout") not in handoff.calls


def test_cancel_predicate_returning_true_rehides_before_propagating(handoff):
    with pytest.raises(ConnectionError, match="用户已取消"):
        handoff.run(cancel_check=lambda: True)

    _assert_rehidden_once(handoff)


@pytest.mark.parametrize("error_type", [ConnectionError, RuntimeError])
def test_cancel_predicate_raising_rehides_and_preserves_error(handoff, error_type):
    error = error_type("cancel callback failed")

    def cancelled():
        raise error

    with pytest.raises(error_type) as caught:
        handoff.run(cancel_check=cancelled)

    assert caught.value is error
    _assert_rehidden_once(handoff)


def test_manual_wait_timeout_rehides_once_without_real_sleep_or_capture(handoff):
    handoff.probes = [("captcha_required", "verification needed")]

    assert handoff.run() is False

    _assert_rehidden_once(handoff)
    assert ("event", "timeout") in handoff.calls
    assert ("capture",) in handoff.calls


def test_closed_page_rehides_before_returning(handoff, monkeypatch):
    monkeypatch.setattr(helpers, "_page_is_gone", lambda page: True)

    assert handoff.run() is False

    _assert_rehidden_once(handoff)
    assert ("event", "timeout") in handoff.calls


def test_healthy_page_does_not_touch_windows(handoff):
    handoff.probes = [None]

    assert handoff.run() is True
    assert handoff.calls == []
