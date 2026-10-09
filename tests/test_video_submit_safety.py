"""No paid calls: uncertain clicks stop selector fallback and pacing harvests safely."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.google_fx.services import google_fx_helpers as H


def test_strict_submit_does_not_try_another_button_after_uncertain_click(monkeypatch):
    from integrations.google_fx.services import google_fx_diagnostics as D
    monkeypatch.setattr(D, "dry_run_enabled", lambda: False)
    monkeypatch.setattr(H, "random_sleep", lambda *a: None)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    monkeypatch.setattr(H, "detect_page_credit_exhaustion", lambda page: None)
    page, editor = MagicMock(), MagicMock()
    button = page.locator.return_value.filter.return_value.last
    button.click.side_effect = RuntimeError("browser disconnected during click")
    with pytest.raises(RuntimeError) as caught:
        H.click_fx_send_button(page, editor, strict_submission=True)
    assert caught.value.submission_started is True
    button.click.assert_called_once()
    editor.press.assert_not_called()
    page.locator.assert_called_once_with("button")


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("warning_at", ["entry", "before_click", "before_enter"])
def test_credit_warning_stops_submission_without_accounting(monkeypatch, strict, warning_at):
    from integrations.google_fx.services import google_fx_diagnostics as D
    from integrations.google_fx.utils import proxy_rotator
    monkeypatch.setattr(D, "dry_run_enabled", lambda: False)
    monkeypatch.setattr(H, "random_sleep", lambda *a: None)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    readings = iter(["Insufficient credits warning"] if warning_at == "entry"
                    else [None, "Insufficient credits warning"])
    monkeypatch.setattr(H, "detect_page_credit_exhaustion", lambda page: next(readings))
    counter = MagicMock()
    monkeypatch.setattr(proxy_rotator, "ProxyRotator", counter)
    page, editor = MagicMock(), MagicMock()
    button = page.locator.return_value.filter.return_value.last
    if warning_at == "before_enter":
        button.is_visible.return_value = False
        page.locator.return_value.last.is_visible.return_value = False
        page.locator.return_value.count.return_value = 0
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS") as caught:
        H.click_fx_send_button(page, editor, strict_submission=strict)
    assert not getattr(caught.value, "submission_started", False)
    button.click.assert_not_called()
    editor.press.assert_not_called()
    counter.assert_not_called()


def test_pacing_harvest_does_not_shorten_minimum_gap(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(H, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(H, "_LAST_FX_SUBMIT_TS", 5.0)
    monkeypatch.setattr(H.random, "uniform", lambda *a: 20.0)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    monkeypatch.setattr(H, "_cancellable_sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    observations = []
    def harvest():
        observations.append(clock.now)
        clock.now += 1.0
    assert H.fx_pacing_wait(on_idle=harvest) == 15.0
    assert observations[0] == 10.0 and clock.now >= 25.0
    assert len(observations) > 1


def test_post_click_cancel_is_marked_as_uncertain_for_receipt(monkeypatch):
    from integrations.google_fx.models import VideoRequest
    page, editor = MagicMock(), MagicMock()
    for name in ("random_sleep", "_clear_prompt_reference_chips_video", "_check_cancelled", "fx_pacing_wait", "note_fx_submit", "_verify_video_prompt_before_send"):
        monkeypatch.setattr(H, name, lambda *a, **k: None)
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda *a, **k: editor)
    monkeypatch.setattr(H, "_fill_prompt_text", lambda *a, **k: True)
    monkeypatch.setattr(H, "click_fx_send_button", lambda *a, **k: True)
    monkeypatch.setattr(H, "_wait_for_new_tile_id", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("cancelled")))
    with pytest.raises(ConnectionError) as caught:
        H._submit_video_to_canvas(page, VideoRequest(prompt="p"), [])
    assert caught.value.submission_started is True
    assert caught.value.click_time > 0


@pytest.mark.parametrize('probe_error', [False, True])
def test_post_click_missing_tile_credit_check_always_keeps_submission_uncertain(monkeypatch, probe_error):
    from integrations.google_fx.models import VideoRequest
    page, editor = MagicMock(), MagicMock()
    for name in ('random_sleep', '_clear_prompt_reference_chips_video', '_check_cancelled',
                 'fx_pacing_wait', 'note_fx_submit', '_verify_video_prompt_before_send'):
        monkeypatch.setattr(H, name, lambda *a, **k: None)
    monkeypatch.setattr(H, '_find_fx_prompt_input', lambda *a, **k: editor)
    monkeypatch.setattr(H, '_fill_prompt_text', lambda *a, **k: True)
    monkeypatch.setattr(H, 'click_fx_send_button', lambda *a, **k: True)
    monkeypatch.setattr(H, '_wait_for_new_tile_id', lambda *a, **k: None)
    monkeypatch.setattr(H, 'last_credit_reading', lambda: (20, None))
    def credit(*a, **k):
        if probe_error:
            raise RuntimeError('read failed')
        return 'out of credits'
    monkeypatch.setattr(H, 'detect_page_credit_exhaustion', credit)
    with pytest.raises(RuntimeError) as caught:
        H._submit_video_to_canvas(page, VideoRequest(prompt='p'), [])
    assert caught.value.submission_started is True
    assert caught.value.click_time > 0


@pytest.mark.parametrize("fail_at", ["before_mount", "after_mount", "before_send"])
def test_existing_failure_stops_before_paid_click_even_without_pacing(monkeypatch, fail_at):
    from integrations.google_fx.models import VideoRequest
    from integrations.google_fx.services.google_fx_video import _UnusualActivityError
    page, editor = MagicMock(), MagicMock()
    phase = ["before_mount"]
    for name in ("random_sleep", "_clear_prompt_reference_chips_video", "_check_cancelled",
                 "note_fx_submit", "_verify_video_prompt_before_send"):
        monkeypatch.setattr(H, name, lambda *a, **k: None)
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda *a, **k: editor)
    monkeypatch.setattr(H, "_fill_prompt_text", lambda *a, **k: True)
    monkeypatch.setattr(H, "_video_refs_still_attached", lambda *a: True)

    def mount(*a, **k):
        phase[0] = "after_mount"
        return ["first", "last"]

    def pacing(*a, **k):
        # Simulates the already-old submission path, which never invokes idle.
        phase[0] = "before_send"
        return 0

    def observe():
        if phase[0] == fail_at:
            raise _UnusualActivityError("unusual activity")

    monkeypatch.setattr(H, "_mount_video_prompt_refs", mount)
    monkeypatch.setattr(H, "fx_pacing_wait", pacing)
    send = MagicMock(return_value=True)
    monkeypatch.setattr(H, "click_fx_send_button", send)
    req = VideoRequest(prompt="exact request", image="first", end_image="last")
    with pytest.raises(_UnusualActivityError) as caught:
        H._submit_video_to_canvas(page, req, [], on_idle=observe)
    assert not getattr(caught.value, "submission_started", False)
    send.assert_not_called()
