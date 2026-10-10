"""Profile rotation preserves receipts and never reuses another account's canvas."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import account_pool as P
from integrations.google_fx.utils import browser as B

pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def rig(monkeypatch):
    marks, stops, selections, notifications = [], [], [], []
    reqs = [SimpleNamespace(prompt=f"clip {i}", image="", end_image="") for i in range(3)]
    runner = V._ChunkRunner(3, 0, reqs, {}, None, None)
    runner._active_account_id = "old-account"
    runner.project_url = runner.bound_project_url = "https://flow.google.com/project/old"
    runner.canvas_is_bound = True
    runner.canvas_is_dirty = True
    runner.path_to_uuid = {"frame.png": "old-frame"}
    runner._reference_scope = ("old-account", runner.project_url)
    runner._reference_scope_initialized = True
    runner._reference_keys = {"frame.png": ("old-key",)}
    runner._confirmed_config = ("old-config",)
    runner.preexisting_tile_ids = {"old-tile"}
    runner._preexisting_scanned = True
    runner.completed = {0}
    runner.results[0] = {"status": "success", "video_url": "/done.mp4",
                         "account_id": "old-account", "project_url": runner.project_url}
    runner._submitted_tasks[1] = {
        "sub_idx": 1, "idx": 1, "req": reqs[1], "tile_id": "paid-tile",
        "reported": False, "account_id": "old-account", "project_url": runner.project_url,
    }
    runner._submitted_tiles[(runner.project_url, 1)] = "paid-tile"
    monkeypatch.setattr(V, "log", lambda *a, **k: None)
    monkeypatch.setattr(V, "_IP_BACKOFF_STEP_SECS", 0)
    monkeypatch.setattr(runner, "_check_cancel", lambda: None)
    monkeypatch.setattr(P, "AccountPool", lambda: SimpleNamespace(
        mark_exhausted=lambda uid, **kw: marks.append((uid, kw))))
    monkeypatch.setattr(B, "stop_ads_browser", lambda **kw: stops.append(kw))

    def choose(**kwargs):
        selections.append(kwargs)
        return {"user_id": "new-account", "name": "next"}

    monkeypatch.setattr(P, "switch_to_next_account", choose)
    runner.on_progress = lambda idx, event, data: notifications.append(
        (event, data, runner._current_account_id(), runner.project_url))
    return SimpleNamespace(runner=runner, marks=marks, stops=stops,
                           selections=selections, notifications=notifications)


@pytest.mark.parametrize("reason", ["credit", "security"])
def test_successful_switch_resets_canvas_before_notification_and_keeps_receipts(rig, reason):
    runner = rig.runner
    completed_result = dict(runner.results[0])
    pending_receipt = dict(runner._submitted_tasks[1])
    submitted_tiles = dict(runner._submitted_tiles)
    if reason == "credit":
        runner._cooldown_and_switch_credit_exhausted("0 Google Flow credits")
    else:
        runner._cooldown_and_switch_account()

    assert runner._current_account_id() == "new-account"
    assert runner.project_url is runner.bound_project_url is None
    assert not runner.canvas_is_bound and not runner.canvas_is_dirty
    assert runner.path_to_uuid == runner._reference_keys == {}
    assert runner._reference_scope is None and not runner._reference_scope_initialized
    assert not runner.preexisting_tile_ids and not runner._preexisting_scanned
    assert runner._confirmed_config is None
    assert runner.completed == {0} and runner.results[0] == completed_result
    assert runner._submitted_tasks[1] == pending_receipt
    assert runner._submitted_tiles == submitted_tiles
    assert rig.notifications[-1][2:] == ("new-account", None)
    assert rig.notifications[-1][1]["previous"] == "old-account"


def _mock_round(monkeypatch, runner, account="new-account"):
    page = SimpleNamespace(bring_to_front=lambda: None)
    monkeypatch.setattr(V, "sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(V, "_connect_fx_page", lambda *a, **k: (SimpleNamespace(close=lambda: None), page))
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **kw: account)
    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.ProjectVideoRecovery",
                        lambda page: nullcontext(object()))
    monkeypatch.setattr(runner, "_prepare_page", lambda page: setattr(
        runner, "project_url", f"https://flow.google.com/project/{account}"))
    monkeypatch.setattr(runner, "_await_generation", lambda *a: None)
    monkeypatch.setattr(runner, "_download_and_report", lambda *a: None)


def test_switched_round_retries_old_account_pending_request_once_on_new_account(rig, monkeypatch):
    runner = rig.runner
    runner._cooldown_and_switch_credit_exhausted("0 Google Flow credits")
    _mock_round(monkeypatch, runner)
    new_work = []
    monkeypatch.setattr(runner, "_adopt_completed_tiles", lambda p, rest: ([], rest))
    monkeypatch.setattr(runner, "_submit_tasks", lambda p, rest, *a, **k: new_work.append(
        [idx for idx, _ in rest]) or [])

    # The old profile's receipt is kept, and one more round is scheduled for it.
    assert runner._run_round([(1, runner.chunk[1]), (2, runner.chunk[2])]) is False
    assert new_work == [[2]]
    assert runner._unresolved_identity_subs == {1}
    assert runner.results[1]["submission_pending"] is True
    assert runner.results[1]["account_id"] == "old-account"
    assert runner.results[1]["project_url"].endswith("/old")

    # Its canvas belongs to another profile, so it is resubmitted here exactly once.
    runner.completed.add(2)
    runner._run_round([(1, runner.chunk[1])])
    assert new_work == [[2], [1]]
    assert not runner._unresolved_identity_subs and runner._uncertain_retried == {1}


def test_connector_initiated_switch_also_resets_bound_canvas(rig, monkeypatch):
    runner = rig.runner
    _mock_round(monkeypatch, runner)
    entry = []
    monkeypatch.setattr(runner, "_prepare_page", lambda p: entry.append(
        (runner._active_account_id, runner.project_url, runner.bound_project_url, dict(runner.path_to_uuid))))
    monkeypatch.setattr(runner, "_adopt_completed_tiles", lambda p, rest: ([], rest))
    monkeypatch.setattr(runner, "_submit_tasks", lambda *a, **k: [])

    runner._run_round([(1, runner.chunk[1])])
    assert entry == [("new-account", None, None, {})]


def test_last_completed_clips_retire_empty_account_without_opening_replacement(rig, monkeypatch):
    runner = rig.runner
    runner._submitted_tasks.clear()
    _mock_round(monkeypatch, runner, account="old-account")
    monkeypatch.setattr(runner, "_adopt_completed_tiles", lambda p, rest: ([], rest))
    monkeypatch.setattr(runner, "_submit_tasks", lambda *a, **k: [])

    def complete(*args):
        runner.completed.update({1, 2})
        for idx in (1, 2):
            runner.results[idx] = {"status": "success", "video_url": f"/{idx}.mp4",
                                   "account_id": "old-account", "project_url": runner.project_url}
        runner._deferred_stop = V._CreditExhaustedError("0 Google Flow credits")

    monkeypatch.setattr(runner, "_await_generation", complete)
    result = runner.run()

    assert all(item["status"] == "success" for item in result)
    assert rig.marks == [("old-account", {"credit": None})]
    assert rig.stops == [{"user_id": "old-account"}]
    assert rig.selections == []


def test_deferred_exhaustion_switches_account_and_keeps_unresolved_receipt_for_retry(rig, monkeypatch):
    runner = rig.runner
    _mock_round(monkeypatch, runner, account="old-account")
    monkeypatch.setattr(runner, "_adopt_completed_tiles", lambda p, rest: ([], rest))
    monkeypatch.setattr(runner, "_submit_tasks", lambda *a, **k: [])

    def unresolved(*args):
        runner._unresolved_identity_subs.add(1)
        runner.results[1] = {"status": "failed", "submission_pending": True,
                             "account_id": "old-account", "project_url": "original"}
        runner._deferred_stop = V._CreditExhaustedError("0 Google Flow credits")

    monkeypatch.setattr(runner, "_await_generation", unresolved)
    # An unconfirmed request no longer pins the batch to an exhausted account.
    with pytest.raises(V._CreditExhaustedError) as stop:
        runner._run_round([(1, runner.chunk[1])])
    assert runner._handle_credit_exhausted(str(stop.value)) is True
    assert rig.marks == [("old-account", {"credit": None})]
    assert len(rig.selections) == 1 and runner._current_account_id() == "new-account"
    assert runner.results[1]["submission_pending"] is True
    assert runner.results[1]["project_url"] == "original"
    assert runner._unresolved_identity_subs == {1} and not runner._uncertain_retried


def test_no_replacement_does_not_overwrite_pending_identity(rig, monkeypatch):
    runner = rig.runner
    pending = {"status": "failed", "submission_pending": True,
               "account_id": "old-account", "project_url": "original", "tile_id": "paid-tile"}
    runner._unresolved_identity_subs.add(1)
    runner.results[1] = dict(pending)
    monkeypatch.setattr(P, "switch_to_next_account", lambda **kw: None)

    assert runner._handle_credit_exhausted("0 Google Flow credits") is False
    assert runner.results[1] == pending
    assert runner.results[2]["status"] == "failed"
    assert runner.project_url.endswith("/old")
    assert runner.path_to_uuid == {"frame.png": "old-frame"}
