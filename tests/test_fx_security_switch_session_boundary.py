"""Account failover must not probe credits inside an active sync Playwright session."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers
from integrations.google_fx.services import google_fx_image as images
from integrations.google_fx.services import google_fx_video as videos


def test_security_check_defers_account_selection_until_playwright_exits(monkeypatch):
    page = SimpleNamespace(url="https://flow.google.com/project/current",
                           bring_to_front=lambda: None)
    browser = SimpleNamespace(contexts=[object()])
    switches = []
    monkeypatch.setattr(helpers, "_check_cancelled", lambda: None)
    monkeypatch.setattr(helpers, "get_ads_ws_url", lambda **_kwargs: "ws://current")
    monkeypatch.setattr(helpers, "_connect_over_cdp_with_retry",
                        lambda *_args, **_kwargs: browser)
    monkeypatch.setattr(helpers, "find_or_create_page", lambda *_args, **_kwargs: page)
    monkeypatch.setattr(helpers, "_page_is_alive", lambda _page: True)
    monkeypatch.setattr(helpers, "is_flow_url", lambda _url: True)
    monkeypatch.setattr(helpers, "ensure_flow_workspace", lambda _page: True)
    monkeypatch.setattr(
        helpers, "_raise_if_manual_intervention_required",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("MANUAL_REQUIRED:security_check")),
    )
    monkeypatch.setattr(helpers, "_switch_account_on_failure",
                        lambda **_kwargs: switches.append("unsafe_nested_probe"))

    with pytest.raises(RuntimeError, match="SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED"):
        helpers._connect_fx_page(object())

    assert switches == []
    assert helpers._classify_failure_for_switch(
        "SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED: current account challenged")[0]


def test_image_outer_retry_switches_after_failed_session_has_returned(monkeypatch):
    attempts = []
    switches = []
    monkeypatch.setattr(images, "fx_pacing_wait", lambda *_args: None)
    monkeypatch.setattr(images, "fx_pacing_bounds", lambda: (0, 0))
    monkeypatch.setattr(images, "_check_cancelled", lambda: None)
    monkeypatch.setattr(images, "_record_current_generation_failure", lambda _reason: None)
    monkeypatch.setattr(images, "_cancellable_sleep", lambda _seconds: None)

    def single_attempt(_req):
        attempts.append(len(switches))
        if len(attempts) == 1:
            return {"status": "failed", "message":
                    "SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED: challenged"}
        return {"status": "success", "image_urls": []}

    monkeypatch.setattr(images, "_generate_images_batch_google_fx_single_attempt",
                        single_attempt)
    monkeypatch.setattr(images, "_switch_account_on_failure",
                        lambda **_kwargs: switches.append("next") or "next")
    request = SimpleNamespace(max_attempts=2, allow_account_switch=True,
                              project_url="https://flow.google.com/project/current",
                              require_fresh_canvas=False)

    result = images._generate_images_batch_google_fx_unlocked(request)

    assert result["status"] == "success"
    assert attempts == [0, 1]
    assert switches == ["next"]
    assert request.project_url is None
    assert request.require_fresh_canvas is True


def test_video_security_check_reaches_outer_rotation_after_session_exit(monkeypatch):
    session = {"active": False}

    @contextmanager
    def fake_playwright():
        session["active"] = True
        try:
            yield object()
        finally:
            session["active"] = False

    monkeypatch.setattr(videos, "sync_playwright", fake_playwright)
    monkeypatch.setattr(videos, "_connect_fx_page", lambda *_args, **_kwargs: (
        _ for _ in ()).throw(RuntimeError(
            "SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED: challenged")))
    runner = videos._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[object()],
                                 all_slices={}, on_progress=None, cancel_check=None)

    with pytest.raises(videos._IPBlockedError):
        runner._run_round([(0, object())])

    assert session["active"] is False


def test_image_quota_classifier_accepts_exception_objects():
    assert images._is_image_quota_failure(RuntimeError("unrelated UI error")) is False
