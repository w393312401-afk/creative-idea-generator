"""No paid/browser actions: deterministic scheduling, ownership and cancellation."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import account_pool as P

pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_unusual_activity_account_switch_after", lambda: 0)
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(V, "time", SimpleNamespace(time=lambda: clock.now,
                                                  monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(V, "random_sleep", lambda *a: None)
    monkeypatch.setattr(V, "log", lambda *a, **k: None)
    monkeypatch.setattr(V, "get_runtime_max_wait_seconds", lambda: 100)
    monkeypatch.setattr(V, "snapshot_flow_tile_ids", lambda page: [])
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k: None)
    monkeypatch.setattr(V, "_dismiss_unexpected_overlays", lambda *a, **k: None)
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **k: "actual-account")
    monkeypatch.setattr(V, "download_video_via_browser", lambda *a, **k: str(tmp_path / "out.mp4"))
    reqs = [VideoRequest(prompt=f"clip {i}", image=str(tmp_path / f"{i}.png"),
                         end_image=str(tmp_path / f"{i + 1}.png"), output_path=str(tmp_path))
            for i in range(8)]
    events, uploads, sends, receipts, marks = [], [], [], [], []
    monkeypatch.setattr(P, "AccountPool", lambda: SimpleNamespace(
        mark_exhausted=lambda uid, **kwargs: marks.append((uid, kwargs))))
    runner = V._ChunkRunner(8, 0, reqs, {}, lambda *args: events.append(args), None)
    runner.project_url = "https://flow.google.com/project/current"
    runner._active_account_id = "actual-account"
    monkeypatch.setattr(runner, "_check_cancel", lambda: None)
    monkeypatch.setattr(runner, "_ensure_video_config", lambda *a: None)
    monkeypatch.setattr(runner, "_credit_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_wake_ready_tiles", lambda *a: None)
    monkeypatch.setattr(runner, "_record_submission", lambda data: receipts.append(dict(data)))
    ready_at = {}

    def upload(page, remaining):
        uploads.append([i for i, _ in remaining])
        return {p: f"uuid-{p}" for _, req in remaining for p in (req.image, req.end_image)}

    monkeypatch.setattr(runner, "_upload_references", upload)
    def submit(page, req, baseline, **kwargs):
        # A terminal task must actually free its slot before the next submission.
        assert len(runner._pending_tasks(list(runner._submitted_tasks.values()))) < 5
        index = int(req.prompt.split()[-1])
        clock.now += 1
        sends.append((index, clock.now))
        ready_at[f"tile-{index}"] = clock.now + (30 if index == 0 else 4)
        return {"tile_id": f"tile-{index}", "click_time": clock.now}
    monkeypatch.setattr(V, "_submit_video_to_canvas", submit)

    def inspect(page, ids, *args, **kwargs):
        return {tid: ({"status": "done", "videoSrc": f"https://flow.google/video/{tid}"}
                      if clock.now >= ready_at[tid] else {"status": "generating", "progress": 50})
                for tid in ids}
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    return SimpleNamespace(runner=runner, reqs=reqs, clock=clock, events=events, uploads=uploads,
                           sends=sends, receipts=receipts, inspect=inspect, marks=marks)


def test_window_replenishes_without_waiting_for_slowest_clip(rig):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert [i for i, _ in rig.sends] == list(range(8))
    assert rig.sends[5][1] < 31  # Clip 0 is still in flight; fixed batches waited for it.
    assert rig.uploads == [[i] for i in range(8)]
    rig.runner._await_generation(object(), tasks)
    assert rig.runner.completed == set(range(8))
    assert len(rig.receipts) == 8
    submitted = [data for _, stage, data in rig.events if stage == "request_submitted"]
    assert all(data["account_id"] == "actual-account" and data["confirmed"] for data in submitted)
    assert all(data["start_uuid"] != data["end_uuid"] and len(data["prompt_hash"]) == 64 for data in submitted)


def test_fixed_window_waits_for_each_result_and_uses_stable_receipts(rig):
    with V.account_binding.bound_fixed_task_account('actual-account'):
        assert rig.runner.max_inflight == 1
        tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
        rig.runner._await_generation(object(), tasks)
    assert rig.sends[1][1] >= 31
    submitted = [data for _, stage, data in rig.events if stage == 'request_submitted']
    completed = [data for _, stage, data in rig.events if stage == 'video_done']
    assert len(submitted) == len(completed) == 8
    assert len({d['submission_id'] for d in submitted}) == 8
    assert all(d['fixed_video_account'] and d['submission_pending'] for d in submitted)
    assert [d['submission_id'] for d in completed] == [d['submission_id'] for d in submitted]
    assert all(d['submission_pending'] is False for d in completed)
    assert rig.runner.max_inflight == V.VIDEO_CHUNK_SIZE == 5


def test_fixed_cancel_after_confirmation_preserves_initial_pending_event(rig):
    captured = []
    def cancel(idx, stage, data):
        if stage == 'request_submitted':
            captured.append(data)
            raise ConnectionError('cancelled')
    rig.runner.on_progress = cancel
    with V.account_binding.bound_fixed_task_account('actual-account'):
        with pytest.raises(ConnectionError):
            rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    assert len(rig.sends) == 1
    assert captured[0]['submission_pending'] is True
    assert captured[0]['fixed_video_account'] is True
    assert captured[0]['submission_id'] == rig.runner._submitted_tasks[0]['submission_id']


def test_fixed_started_probe_error_is_journaled_without_a_second_deep_probe(rig, monkeypatch):
    from integrations.google_fx.utils.browser import BrowserSessionClosedError
    attempts, probes = [], []

    def submit(*args, **kwargs):
        attempts.append(True)
        error = BrowserSessionClosedError('browser closed after Generate during credit probe')
        error.submission_started = True
        error.click_time = 12.5
        raise error

    def credit(page, deep=False):
        if deep:
            probes.append(True)
            raise BrowserSessionClosedError('second probe has no browser')
        return None

    monkeypatch.setattr(V, '_submit_video_to_canvas', submit)
    monkeypatch.setattr(V, 'detect_page_credit_exhaustion', credit)
    with V.account_binding.bound_fixed_task_account('actual-account'):
        rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert attempts == [True]
    assert probes == []
    assert rig.uploads == [[0]]
    receipt = rig.runner.results[0]
    assert receipt['submission_pending'] is True
    assert receipt['confirmed'] is False
    assert receipt['fixed_video_account'] is True
    assert receipt['submitted_at'] == 12.5
    assert receipt['submission_id'].startswith('fx-')
    assert rig.runner._unresolved_identity_subs == {0}
    assert isinstance(rig.runner._deferred_stop, V.account_binding.FixedAccountStopError)
    events = [d for _, event, d in rig.events if event == 'request_submitted']
    assert len(events) == 1
    assert events[0]['submission_id'] == receipt['submission_id']
    assert events[0]['confirmed'] is False and events[0]['submission_pending'] is True


def test_normal_run_uses_one_browser_session_for_more_than_five_clips(rig, monkeypatch):
    connections, closes = [], []
    page = SimpleNamespace(bring_to_front=lambda: None)
    monkeypatch.setattr(V, "sync_playwright", lambda: nullcontext(object()))
    def connect(*args, **kwargs):
        connections.append(True)
        return SimpleNamespace(close=lambda: closes.append(True)), page
    monkeypatch.setattr(V, "_connect_fx_page", connect)
    monkeypatch.setattr(rig.runner, "_prepare_page", lambda page: None)
    monkeypatch.setattr(rig.runner, "_adopt_completed_tiles", lambda page, rest: ([], rest))
    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.ProjectVideoRecovery",
                        lambda page: nullcontext(object()))
    result = rig.runner.run()
    assert len(connections) == len(closes) == 1
    assert len(result) == 8
    assert all(v["status"] == "success" and v["account_id"] == "actual-account" for v in result)


def test_cancel_after_confirmation_preserves_accounting_and_identity(rig):
    def cancel(idx, stage, data):
        if stage == "request_submitted":
            raise ConnectionError("cancelled")
    rig.runner.on_progress = cancel
    with pytest.raises(ConnectionError, match="cancelled"):
        rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert len(rig.sends) == len(rig.receipts) == 1
    assert rig.runner._submitted_tasks[0]["account_id"] == "actual-account"
    assert rig.runner._submitted_tasks[0]["project_url"] == rig.runner.project_url


def test_uncertain_submission_is_not_retried_or_counted_as_confirmed(rig, monkeypatch):
    def uncertain(*args, **kwargs):
        exc = RuntimeError("new tile missing")
        exc.submission_started = True
        raise exc
    monkeypatch.setattr(V, "_submit_video_to_canvas", uncertain)
    rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert rig.runner._unresolved_identity_subs == {0}
    assert rig.runner.results[0]["submission_pending"] is True
    assert len(rig.receipts) == 0
    assert len(rig.uploads) == 1
    pending = [d for _, event, d in rig.events if event == "request_submitted"]
    assert pending[0]["confirmed"] is False


def test_cancellation_while_waiting_for_submitted_tile_keeps_uncertain_receipt(rig, monkeypatch):
    def cancel_after_click(*args, **kwargs):
        exc = ConnectionError("cancelled after Generate")
        exc.submission_started = True
        exc.click_time = 7
        raise exc
    monkeypatch.setattr(V, "_submit_video_to_canvas", cancel_after_click)
    with pytest.raises(ConnectionError, match="cancelled after"):
        rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    assert rig.runner.results[0]["submission_pending"] is True
    pending = [d for _, event, d in rig.events if event == "request_submitted"]
    assert pending[0]["confirmed"] is False and pending[0]["submitted_at"] == 7


def test_cancel_before_click_does_not_create_uncertain_submission(rig, monkeypatch):
    monkeypatch.setattr(V, "_submit_video_to_canvas", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("cancelled")))
    with pytest.raises(ConnectionError):
        rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    assert not rig.runner._unresolved_identity_subs
    assert not [event for _, event, _ in rig.events if event == "request_submitted"]


def test_ready_results_keep_original_account_and_project_after_rotation(rig):
    tasks = rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    rig.runner._active_account_id = "another-account"
    rig.runner.project_url = "another-project"
    rig.clock.now = 32
    rig.runner._await_generation(object(), tasks)
    result = rig.runner.results[0]
    assert result["account_id"] == "actual-account"
    assert result["project_url"] == "https://flow.google.com/project/current"


def test_browser_reconnect_resumes_submitted_task_without_second_send(rig, monkeypatch):
    class TargetClosedError(RuntimeError):
        pass
    calls = [0]
    def inspect(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise TargetClosedError("Target page disappeared")
        return rig.inspect(*args, **kwargs)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    monkeypatch.setattr(V, "sync_playwright", lambda: nullcontext(object()))
    page = SimpleNamespace(bring_to_front=lambda: None)
    monkeypatch.setattr(V, "_connect_fx_page", lambda *a, **k: (SimpleNamespace(close=lambda: None), page))
    monkeypatch.setattr(rig.runner, "_prepare_page", lambda page: None)
    monkeypatch.setattr(rig.runner, "_adopt_completed_tiles", lambda page, rest: ([], rest))
    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.ProjectVideoRecovery",
                        lambda page: nullcontext(object()))
    assert all(r["status"] == "success" for r in rig.runner.run())
    assert [idx for idx, _ in rig.sends].count(0) == 1
    assert len(rig.receipts) == 8


def _offline_round(monkeypatch, runner):
    connections = []
    page = SimpleNamespace(bring_to_front=lambda: None)
    monkeypatch.setattr(V, "sync_playwright", lambda: nullcontext(object()))

    def connect(*args, **kwargs):
        connections.append(page)
        return SimpleNamespace(close=lambda: None), page

    monkeypatch.setattr(V, "_connect_fx_page", connect)
    monkeypatch.setattr(runner, "_prepare_page", lambda page: None)
    monkeypatch.setattr(runner, "_adopt_completed_tiles", lambda page, rest: ([], rest))
    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.ProjectVideoRecovery",
                        lambda page: nullcontext(object()))
    return connections


@pytest.mark.parametrize("phase", ["upload", "config"])
@pytest.mark.parametrize("fail_index", [0, 3])
def test_preparation_failure_drains_paid_tasks_then_stops_retrying(rig, monkeypatch, phase, fail_index):
    connections = _offline_round(monkeypatch, rig.runner)
    calls = []
    original_upload = rig.runner._upload_references

    def prepare(page, request):
        index = request[0][0] if phase == "upload" else int(request.prompt.split()[-1])
        calls.append(index)
        if index == fail_index:
            raise RuntimeError("upload menu unavailable" if phase == "upload" else "video config unconfirmed")
        return original_upload(page, request) if phase == "upload" else None

    monkeypatch.setattr(rig.runner, "_upload_references" if phase == "upload" else "_ensure_video_config", prepare)
    result = rig.runner.run()

    assert len(connections) == 1
    assert calls == list(range(fail_index + 1))
    assert [idx for idx, _ in rig.sends] == list(range(fail_index))
    assert len(rig.receipts) == fail_index
    assert rig.runner.gen_retry_used == 0
    assert all(item["status"] == "success" for item in result[:fail_index])
    assert all(item["status"] == "failed" and "尚未提交" in item["message"]
               and "停止本批次自动重试" in item["message"] for item in result[fail_index:])
    terminal = [(idx, event) for idx, event, _ in rig.events if event in ("video_done", "video_error")]
    assert all(event == "video_done" for _, event in terminal[:fail_index])
    assert terminal[fail_index:] == [(idx, "video_error") for idx in range(fail_index, 8)]


def test_reconnect_during_preparation_failure_drain_never_reuploads_or_resubmits(rig, monkeypatch):
    class TargetClosedError(RuntimeError):
        pass

    connections = _offline_round(monkeypatch, rig.runner)
    original_upload = rig.runner._upload_references
    preparations = []

    def upload(page, remaining):
        index = remaining[0][0]
        preparations.append(index)
        if index == 3:
            raise RuntimeError("upload menu unavailable")
        return original_upload(page, remaining)

    disconnected = []

    def inspect(*args, **kwargs):
        if rig.runner._preparation_failures and not disconnected:
            disconnected.append(True)
            raise TargetClosedError("Target page disappeared while draining")
        return rig.inspect(*args, **kwargs)

    monkeypatch.setattr(rig.runner, "_upload_references", upload)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    result = rig.runner.run()

    assert len(connections) == 2
    assert preparations == [0, 1, 2, 3]
    assert [idx for idx, _ in rig.sends] == [0, 1, 2]
    assert len(rig.receipts) == 3
    assert [item["status"] for item in result] == ["success"] * 3 + ["failed"] * 5
    assert len([event for _, event, _ in rig.events if event == "video_error"]) == 5


@pytest.mark.parametrize("phase", ["upload", "config"])
@pytest.mark.parametrize("error", [
    ConnectionError("cancelled"),
    V._ManualInterventionTimeoutError("login timeout"),
    RuntimeError("MANUAL_REQUIRED:captcha_required:verification needed"),
    type("TargetClosedError", (RuntimeError,), {})("browser closed"),
    type("BrowserSessionClosedError", (RuntimeError,), {})("browser session closed"),
])
def test_preparation_does_not_swallow_cancellation_or_browser_and_manual_signals(rig, monkeypatch, phase, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(rig.runner, "_upload_references" if phase == "upload" else "_ensure_video_config", fail)
    with pytest.raises(type(error)) as raised:
        rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert raised.value is error
    assert rig.runner._preparation_failures == {}
    assert not rig.sends


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.parametrize("error,expected", [
    (V._CreditExhaustedError("out of credits"), V._CreditExhaustedError),
    (RuntimeError("INSUFFICIENT_CREDITS: 0 Google Flow credits"), V._CreditExhaustedError),
    (V._IPBlockedError("unusual activity"), V._IPBlockedError),
    (RuntimeError("SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED: challenged"), V._IPBlockedError),
])
def test_preparation_keeps_credit_and_security_paths(rig, monkeypatch, inflight, error, expected):
    original_upload = rig.runner._upload_references

    def upload(page, remaining):
        if remaining[0][0] == int(inflight):
            raise error
        return original_upload(page, remaining)

    monkeypatch.setattr(rig.runner, "_upload_references", upload)
    if inflight:
        tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
        assert isinstance(rig.runner._deferred_stop, expected)
        rig.runner._await_generation(object(), tasks)
        # The paid in-flight request is collected before any account switch.
        assert rig.runner.completed == {0}
        assert not rig.runner._credit_pending_subs
        assert len(rig.receipts) == 1
    else:
        with pytest.raises(expected):
            rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert rig.runner._preparation_failures == {}


def test_actual_page_credit_error_is_not_misreported_as_local_preparation(rig, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("config tab unavailable")

    monkeypatch.setattr(rig.runner, "_ensure_video_config", fail)
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k: "out of credits")
    with pytest.raises(V._CreditExhaustedError, match="out of credits"):
        rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert rig.runner._preparation_failures == {}


def test_helper_runtime_cancellation_remains_cancellation(rig, monkeypatch):
    from integrations.google_fx.utils import cancel_flag

    def cancel(*args, **kwargs):
        cancel_flag.is_cancelled = True
        V._check_cancelled()

    monkeypatch.setattr(rig.runner, "_upload_references", cancel)
    cancel_flag.init_context("test-preparation-cancel")
    try:
        with pytest.raises(RuntimeError, match="任务已取消"):
            rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    finally:
        cancel_flag.clear_context()
    assert rig.runner._preparation_failures == {}


def test_single_video_upload_does_not_infer_image_mode_from_missing_add_button(rig, monkeypatch, tmp_path):
    frame = tmp_path / "start.png"
    frame.write_bytes(b"frame")
    page = SimpleNamespace(
        url=rig.runner.project_url,
        bring_to_front=lambda: None,
        locator=lambda selector: SimpleNamespace(first=SimpleNamespace(is_visible=lambda: True)),
    )
    monkeypatch.setattr(V, "sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(V, "_connect_fx_page", lambda *a, **k: (SimpleNamespace(close=lambda: None), page))
    monkeypatch.setattr(V, "ensure_flow_workspace", lambda page: None)
    monkeypatch.setattr(V, "_click_new_project_button", lambda page: True)
    monkeypatch.setattr(V, "_find_add2_btn", lambda page: None)
    monkeypatch.setattr(V, "_get_panel_uuids", lambda page: {"start-uuid"})
    uploads = []
    monkeypatch.setattr(V, "_upload_image_to_canvas", lambda page, path, **k: uploads.append(path) or "start-uuid")
    configurations = []

    def configure(page, **kwargs):
        configurations.append(kwargs["want_video"])
        raise RuntimeError("stop before generation")

    monkeypatch.setattr(V, "_verify_and_fix_fx_config", configure)
    result = V._generate_video_google_fx_unlocked(VideoRequest(prompt="clip", image=str(frame)))
    assert uploads == [str(frame)]
    assert configurations == [True]
    assert result["message"] == "stop before generation"


def test_unusual_activity_drains_then_rotates_ip_on_same_profile(rig, monkeypatch):
    from contextlib import contextmanager
    from integrations.google_fx.utils import proxy_rotator

    connections = _offline_round(monkeypatch, rig.runner)
    rotations = []
    session = {"active": False}

    @contextmanager
    def playwright():
        session["active"] = True
        try:
            yield object()
        finally:
            session["active"] = False

    def inspect(page, ids, *args, **kwargs):
        if rotations:
            return rig.inspect(page, ids, *args, **kwargs)
        return {tid: ({"status": "done", "videoSrc": "https://flow.google/video/saved"}
                      if tid == "tile-0" else {
                          "status": "failed", "failedText": "We detected unusual activity",
                          "isIpBlocked": True}) for tid in ids}

    def rotate(**kwargs):
        assert not session["active"]
        assert rig.runner.completed == {0}
        assert rig.runner._submitted_tasks[1]["reported"] is True
        assert not rig.runner._pending_tasks(list(rig.runner._submitted_tasks.values()))
        assert kwargs["user_id"] == "actual-account"
        rotations.append(kwargs)
        return {"success": True, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2"}

    monkeypatch.setattr(V, "sync_playwright", playwright)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    monkeypatch.setattr(V, "_IP_BACKOFF_STEP_SECS", 0)
    monkeypatch.setattr(V.account_binding, "set_task_account", lambda account: None)
    monkeypatch.setattr(proxy_rotator, "ProxyRotator", lambda: SimpleNamespace(rotate_proxy_verified=rotate))
    result = rig.runner.run()
    assert len(rotations) == 1 and len(connections) == 2
    # The accepted clip is kept; only the blocked clip is sent again after rotation.
    assert [idx for idx, _ in rig.sends].count(0) == 1
    assert [idx for idx, _ in rig.sends].count(1) == 2
    assert all(item["status"] == "success" for item in result)
    assert all(item["account_id"] == "actual-account"
               and item["project_url"] == "https://flow.google.com/project/current" for item in result)


@pytest.mark.parametrize("checkpoint", ["before_upload", "after_upload", "before_generate"])
def test_unusual_activity_between_submissions_stops_at_next_checkpoint(rig, monkeypatch, checkpoint):
    observed = []
    error_visible = [False]
    original_upload = rig.runner._upload_references
    original_submit = V._submit_video_to_canvas

    def inspect(page, ids, *args, **kwargs):
        observed.append(list(ids))
        if checkpoint == "before_upload" and len(observed) == 2:
            error_visible[0] = True
        if error_visible[0]:
            return {tid: {"status": "failed", "failedText": "We detected unusual activity",
                          "isIpBlocked": True} for tid in ids}
        return rig.inspect(page, ids, *args, **kwargs)

    def upload(page, remaining):
        result = original_upload(page, remaining)
        if checkpoint == "after_upload" and remaining[0][0] == 1:
            error_visible[0] = True
        return result

    def submit(page, req, *args, **kwargs):
        if checkpoint == "before_generate" and req.prompt == "clip 1":
            error_visible[0] = True
            kwargs["on_idle"]()
        return original_submit(page, req, *args, **kwargs)

    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    monkeypatch.setattr(rig.runner, "_upload_references", upload)
    monkeypatch.setattr(V, "_submit_video_to_canvas", submit)
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)

    assert [idx for idx, _ in rig.sends] == [0]
    assert len(rig.receipts) == 1
    assert rig.uploads == ([[0]] if checkpoint == "before_upload" else [[0], [1]])
    assert isinstance(rig.runner._deferred_stop, V._UnusualActivityError)
    assert tasks[0]["status"] == "failed" and tasks[0]["reported"] is True
    assert [(idx, event) for idx, event, _ in rig.events if event == "video_error"] == [(0, "video_error")]
    assert not rig.runner._unresolved_identity_subs


def test_failed_state_is_reported_before_any_slow_wake_or_download(rig, monkeypatch):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:2])), [], {}, prepare_references=True)
    states = {
        "tile-0": {"status": "done", "videoSrc": "https://flow.google/video/ready"},
        "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True},
    }
    order = []
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: dict(states))

    def wake(*args):
        assert rig.runner.results[1]["status"] == "failed"
        assert isinstance(rig.runner._deferred_stop, V._UnusualActivityError)
        assert any(idx == 1 and event == "video_error" for idx, event, _ in rig.events)
        order.append("wake")

    def download(*args, **kwargs):
        assert rig.runner.results[1]["status"] == "failed"
        order.append("download")
        return "/saved.mp4"

    monkeypatch.setattr(rig.runner, "_wake_ready_tiles", wake)
    monkeypatch.setattr(V, "download_video_via_browser", download)
    rig.runner._await_generation(object(), tasks)
    assert order == ["wake", "download"]
    assert rig.runner.completed == {0}
    assert not rig.runner._unresolved_identity_subs


def test_pre_generate_observation_does_not_download_or_touch_prepared_prompt(rig, monkeypatch):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:2])), [], {}, prepare_references=True)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {
        "tile-0": {"status": "done", "videoSrc": "https://flow.google/video/ready"},
        "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True},
    })
    monkeypatch.setattr(rig.runner, "_wake_ready_tiles", lambda *a: pytest.fail("prompt must stay untouched"))
    monkeypatch.setattr(V, "download_video_via_browser", lambda *a, **k: pytest.fail("download must wait for drain"))
    with pytest.raises(V._UnusualActivityError):
        rig.runner._observe_during_pacing(object(), tasks)
    assert tasks[1]["status"] == "failed" and tasks[1]["reported"] is True
    assert tasks[0]["status"] == "generating" and not tasks[0].get("reported")
    assert not rig.runner._unresolved_identity_subs


def test_reported_failed_and_successful_tiles_are_excluded_from_new_scans(rig, monkeypatch):
    scans = []
    old = dict(sub_idx=0, idx=0, req=rig.reqs[0], tile_id="old-failure", status="failed", reported=True)
    done = dict(sub_idx=7, idx=7, req=rig.reqs[7], tile_id="old-success", status="success", reported=True)

    def inspect(page, ids, *args, **kwargs):
        scans.append(list(ids))
        states = rig.inspect(page, ids, *args, **kwargs)
        # 即便底层误夹带旧卡片，调度器也只能采信当前未终结任务。
        states["old-failure"] = {"status": "failed", "isIpBlocked": True}
        states["old-success"] = {"status": "failed", "isCreditExhausted": True}
        return states

    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    rig.runner._submit_tasks(object(), [(1, rig.reqs[1])], [old, done], {}, prepare_references=True)
    assert [idx for idx, _ in rig.sends] == [1]
    assert all("old-failure" not in ids and "old-success" not in ids for ids in scans)
    assert rig.runner._deferred_stop is None
    assert not [event for _, event, _ in rig.events if event == "video_error"]


def test_missing_card_without_verified_failure_is_not_assigned_an_old_warning(rig, monkeypatch):
    tasks = rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {
        "tile-0": {"status": "missing"},
        "unrelated-old-warning": {"status": "failed", "isIpBlocked": True},
    })
    rig.runner._await_generation(object(), tasks, until_slot_available=True)
    assert rig.runner._deferred_stop is None
    assert not tasks[0].get("reported") and tasks[0]["status"] == "generating"


@pytest.mark.parametrize("entry", ["submit", "await"])
def test_resumed_success_download_waits_for_failure_scan(rig, monkeypatch, entry):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:2])), [], {}, prepare_references=True)
    tasks[0].update(status="success", video_url="https://flow.google/video/already-ready")
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {
        "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True},
    })
    downloads = []

    def download(*args, **kwargs):
        assert rig.runner.results[1]["status"] == "failed"
        assert isinstance(rig.runner._deferred_stop, V._UnusualActivityError)
        downloads.append(True)
        return "/saved.mp4"

    monkeypatch.setattr(V, "download_video_via_browser", download)
    if entry == "submit":
        tasks = rig.runner._submit_tasks(object(), [], tasks, {}, prepare_references=True)
    rig.runner._await_generation(object(), tasks)
    assert downloads == [True]
    assert rig.runner.completed == {0}


@pytest.mark.parametrize("found_on_canvas", [True, False])
def test_missing_paid_video_has_bounded_drain_then_rotates_and_recovers_or_resubmits_once(
        rig, monkeypatch, found_on_canvas):
    from contextlib import contextmanager
    from integrations.google_fx.utils import proxy_rotator

    connections = _offline_round(monkeypatch, rig.runner)
    recoveries = []

    def recover(page, requests, cancel, **kwargs):
        recoveries.append(requests)
        return {r["tile_id"]: {"status": "done", "videoSrc": "https://flow.google/video/original",
                               "mediaId": "original-media"}
                for r in requests if r["tile_id"] == "uncertain-0" and found_on_canvas}

    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.recover_project_videos", recover)
    rotations = []
    blocked_at = []
    session = {"active": False}
    monkeypatch.setattr(rig.runner, "_capture_tile_lost", lambda *a: None)
    # A generous normal generation budget must not extend security-error drain.
    monkeypatch.setattr(V, "get_runtime_max_wait_seconds", lambda: 3600)

    @contextmanager
    def playwright():
        session["active"] = True
        try:
            yield object()
        finally:
            session["active"] = False

    def inspect(page, ids, *args, **kwargs):
        if rotations:
            return rig.inspect(page, ids, *args, **kwargs)
        if "tile-2" in ids and not blocked_at:
            blocked_at.append(rig.clock.now)
        if not blocked_at:
            return {tid: {"status": "generating", "progress": 20} for tid in ids}
        states = {}
        for tid in ids:
            if tid == "tile-0":
                states[tid] = {"status": "missing"}
            elif tid == "tile-2":
                states[tid] = {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True}
            elif rig.clock.now >= blocked_at[0] + 6:
                states[tid] = {"status": "done", "videoSrc": "https://flow.google/video/saved"}
            else:
                states[tid] = {"status": "generating", "progress": 50}
        return states

    def rotate(**kwargs):
        assert not session["active"]
        elapsed = rig.clock.now - blocked_at[0]
        assert V._UNUSUAL_ACTIVITY_DRAIN_SECONDS <= elapsed <= V._UNUSUAL_ACTIVITY_DRAIN_SECONDS + 2
        assert rig.runner.completed == {1}
        assert rig.runner._unresolved_identity_subs == {0}
        assert rig.runner._ip_drain_pending_subs == {0}
        pending = rig.runner._submitted_tasks[0]
        assert pending["status"] == "failed" and pending["submission_pending"] is True
        assert pending["reported"] is True
        assert pending["submitted_at"] == rig.receipts[0]["submitted_at"]
        assert pending["refs"] == rig.receipts[0]["refs"]
        assert kwargs["user_id"] == "actual-account"
        rotations.append(kwargs)
        return {"success": True, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2"}

    monkeypatch.setattr(V, "sync_playwright", playwright)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    monkeypatch.setattr(V.account_binding, "set_task_account", lambda account: None)
    monkeypatch.setattr(proxy_rotator, "ProxyRotator", lambda: SimpleNamespace(rotate_proxy_verified=rotate))
    result = rig.runner.run()

    assert len(rotations) == 1 and len(connections) == 2
    sends = [idx for idx, _ in rig.sends]
    assert sends[:3] == [0, 1, 2] and sends.count(2) == 2
    # Back on the same canvas, the paid clip is looked up by its full original request.
    lookup = next(r for batch in recoveries for r in batch if r["tile_id"] == "uncertain-0")
    receipt = rig.receipts[0]
    assert lookup["prompt"] == rig.reqs[0].prompt
    assert lookup["refs"] == [u.lower() for u in receipt["refs"]]
    assert lookup["click_time"] == receipt["submitted_at"]
    if found_on_canvas:
        assert sends.count(0) == 1  # Recovered: no second charge.
        for key in ("account_id", "project_url", "tile_id", "prompt_hash", "start_uuid", "end_uuid"):
            assert result[0][key] == receipt[key]
    else:
        assert sends.count(0) == 1  # Unknown result keeps its original paid receipt.
        assert result[0]['status'] == 'failed' and result[0]['submission_pending'] is True
    assert all(item["status"] == "success" for item in result[1:])
    assert not any(item.get("submission_pending") for item in result[1:])
    assert len([event for idx, event, _ in rig.events if idx == 0 and event == "video_error"]) == 1


def test_later_unusual_activity_does_not_restart_pending_drain_deadline(rig, monkeypatch):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:3])), [], {}, prepare_references=True)
    started_at = rig.clock.now
    monkeypatch.setattr(rig.runner, "_capture_tile_lost", lambda *a: None)
    monkeypatch.setattr(V, "get_runtime_max_wait_seconds", lambda: 3600)

    def inspect(page, ids, *args, **kwargs):
        states = {"tile-0": {"status": "missing"},
                  "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True}}
        states["tile-2"] = ({"status": "failed", "failedText": "unusual activity", "isIpBlocked": True}
                            if rig.clock.now >= started_at + 12
                            else {"status": "generating", "progress": 50})
        return states

    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    rig.runner._await_generation(object(), tasks)

    assert V._UNUSUAL_ACTIVITY_DRAIN_SECONDS <= rig.clock.now - started_at <= V._UNUSUAL_ACTIVITY_DRAIN_SECONDS + 2
    assert rig.runner._unresolved_identity_subs == rig.runner._ip_drain_pending_subs == {0}
    assert tasks[0]["submission_pending"] is True
    assert all(task["reported"] is True for task in tasks)
    assert not tasks[1].get("submission_pending") and not tasks[2].get("submission_pending")
    assert len([event for _, event, _ in rig.events if event == "video_error"]) == 3


def test_cancellation_during_security_drain_keeps_paid_receipt_without_resubmitting(rig, monkeypatch):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:2])), [], {}, prepare_references=True)
    started_at = rig.clock.now
    receipt = dict(rig.receipts[0])
    monkeypatch.setattr(rig.runner, "_capture_tile_lost", lambda *a: None)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {
        "tile-0": {"status": "missing"},
        "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True},
    })

    def cancel():
        if rig.clock.now - started_at >= 8:
            raise ConnectionError("cancelled during drain")

    monkeypatch.setattr(rig.runner, "_check_cancel", cancel)
    monkeypatch.setattr(rig.runner, "_rotate_ip_immediately", lambda: pytest.fail("cancelled work must not rotate"))
    with pytest.raises(ConnectionError, match="cancelled during drain"):
        rig.runner._await_generation(object(), tasks)

    assert rig.clock.now - started_at < V._UNUSUAL_ACTIVITY_DRAIN_SECONDS
    assert [idx for idx, _ in rig.sends] == [0, 1]
    assert len(rig.receipts) == 2 and rig.receipts[0] == receipt
    assert not tasks[0].get("reported") and tasks[0]["status"] == "generating"
    assert rig.runner._submitted_tasks[0]["tile_id"] == receipt["tile_id"]
    assert not rig.runner._ip_drain_pending_subs
    assert not rig.runner._unresolved_identity_subs


def test_one_missing_card_after_generate_is_not_repeated_without_terminal_evidence(rig, monkeypatch):
    _offline_round(monkeypatch, rig.runner)
    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.recover_project_videos",
                        lambda *a, **k: {})
    original_submit = V._submit_video_to_canvas
    calls = []

    def flaky(page, req, baseline, **kwargs):
        index = int(req.prompt.split()[-1])
        calls.append(index)
        if index == 2 and calls.count(2) == 1:
            exc = RuntimeError("Generate 后未检测到新 tile")
            exc.submission_started = True
            raise exc
        return original_submit(page, req, baseline, **kwargs)

    monkeypatch.setattr(V, "_submit_video_to_canvas", flaky)
    result = rig.runner.run()

    assert calls == [0, 1, 2]
    assert all(item["status"] == "success" for item in result[:2])
    assert result[2]['submission_pending'] is True
    assert rig.runner._unresolved_identity_subs == {2}
    assert rig.runner.gen_retry_used == 0  # Unstarted clips did not spend the retry budget.


def test_uncertain_submissions_only_recover_originals_then_stop(rig, monkeypatch):
    _offline_round(monkeypatch, rig.runner)
    tasks = rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    tasks[0].update(status="failed", submission_pending=True, message="awaiting original paid request")
    rig.runner._download_and_report_task(object(), tasks[0])
    rig.runner._unresolved_identity_subs.add(0)
    rig.runner._ip_drain_pending_subs.add(0)
    attempted, lookups = [], []

    def recover(page, requests, cancel, **kwargs):
        lookups.extend(r["tile_id"] for r in requests)
        # Clip 0's original finished on the canvas; clip 1 never produced a card.
        return {r["tile_id"]: {"status": "done", "videoSrc": "https://flow.google/video/clip-0"}
                for r in requests if r["tile_id"] == "uncertain-0"}

    def uncertain(page, req, *args, **kwargs):
        attempted.append(req.prompt)
        exc = RuntimeError("new tile missing")
        exc.submission_started = True
        raise exc

    monkeypatch.setattr("integrations.google_fx.services.flow_video_identity.recover_project_videos", recover)
    monkeypatch.setattr(V, "_submit_video_to_canvas", uncertain)
    monkeypatch.setattr(rig.runner, "_rotate_ip_immediately", lambda: pytest.fail("ordinary ambiguity must not rotate"))
    result = rig.runner.run()

    assert lookups == ["uncertain-0", "uncertain-1"]
    assert attempted == ["clip 1"]
    assert len(rig.receipts) == 1
    assert result[0]["status"] == "success"
    assert result[1]["submission_pending"] is True
    assert rig.runner._unresolved_identity_subs == {1} and rig.runner._uncertain_retried == {0, 1}
    assert all(item["status"] == "failed" and "结果仍未确认" in item["message"] for item in result[2:])
    assert rig.runner.ip_retry == 0 and rig.runner.gen_retry_used == 0


@pytest.mark.parametrize('retries', [0, 2, 5])
@pytest.mark.parametrize('fixed_account', [False, True])
def test_confirmed_native_failure_retries_per_slot_with_configured_budget(
        rig, monkeypatch, retries, fixed_account):
    _offline_round(monkeypatch, rig.runner)
    rig.runner.retry_limits = {index: retries for index in range(8)}
    original_inspect = rig.inspect
    def inspect(page, ids, *args, **kwargs):
        states = original_inspect(page, ids, *args, **kwargs)
        for tile in ids:
            if tile == 'tile-7':
                states[tile] = {'status': 'failed', 'failedText': 'video generation failed'}
        return states
    monkeypatch.setattr(V, '_inspect_all_pending_tiles', inspect)
    with (V.account_binding.bound_fixed_task_account('actual-account')
          if fixed_account else nullcontext()):
        result = rig.runner.run()
    assert sum(index == 7 for index, _ in rig.sends) == retries + 1
    assert all(item['status'] == 'success' for item in result[:7])
    assert result[7]['status'] == 'failed' and not result[7].get('submission_pending')
    warnings = [data for index, stage, data in rig.events
                if index == 7 and stage == 'video_warning' and data.get('code') == 'video_auto_retry']
    assert [warning['retry'] for warning in warnings] == list(range(1, retries + 1))


def test_cancellation_at_native_retry_warning_prevents_next_generate(rig, monkeypatch):
    rig.runner.submission_attempts[0] = 1
    def cancel(index, stage, details):
        if stage == 'video_warning':
            raise ConnectionError('cancel retry')
    rig.runner.on_progress = cancel
    with pytest.raises(ConnectionError, match='cancel retry'):
        rig.runner._prepare_paid_submission(0)
    assert rig.runner.submission_attempts[0] == 1
    assert rig.sends == []


@pytest.mark.parametrize("timeout", ["per_task", "batch"])
def test_ordinary_timeout_during_unusual_activity_still_isolates_paid_request(rig, monkeypatch, timeout):
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs[:2])), [], {}, prepare_references=True)
    monkeypatch.setattr(rig.runner, "_capture_tile_lost", lambda *a: None)
    if timeout == "per_task":
        rig.clock.now = 1000
    else:
        monkeypatch.setattr(V, "get_runtime_max_wait_seconds", lambda: 2)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {
        "tile-0": {"status": "missing"},
        "tile-1": {"status": "failed", "failedText": "unusual activity", "isIpBlocked": True},
    })
    started = rig.clock.now
    rig.runner._await_generation(object(), tasks)
    assert rig.clock.now - started < V._UNUSUAL_ACTIVITY_DRAIN_SECONDS
    assert rig.runner._unresolved_identity_subs == rig.runner._ip_drain_pending_subs == {0}
    assert rig.runner.results[0]["submission_pending"] is True
    assert rig.runner.results[0]["tile_id"] == rig.receipts[0]["tile_id"]
    assert len(rig.receipts) == 2


def test_credit_warning_before_upload_marks_real_balance_without_a_paid_submission(rig, monkeypatch):
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k:
                        "账号积分不足：实测 1 < 15 [credit=1]")
    with pytest.raises(V._CreditExhaustedError):
        rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    assert rig.marks == [("actual-account", {"credit": 1})]
    assert not rig.sends and not rig.uploads and not rig.receipts
    assert not rig.runner._unresolved_identity_subs


@pytest.mark.parametrize("signal", ["exception", "page"])
def test_credit_signal_does_not_disappear_inside_ambiguous_submit_path(rig, monkeypatch, signal):
    reason = "INSUFFICIENT_CREDITS: 实测余额不足 [credit=1]"
    submitted = []

    def uncertain(*args, **kwargs):
        submitted.append(True)
        exc = RuntimeError(reason if signal == "exception" else "new tile missing")
        exc.submission_started = True
        exc.click_time = 7
        raise exc

    monkeypatch.setattr(V, "_submit_video_to_canvas", uncertain)
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k:
                        reason if submitted and signal == "page" else None)
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {}, prepare_references=True)
    assert tasks == []
    assert isinstance(rig.runner._deferred_stop, V._CreditExhaustedError)
    assert rig.marks == [("actual-account", {"credit": 1})]
    assert rig.runner._unresolved_identity_subs == rig.runner._credit_pending_subs == {0}
    assert not rig.runner._blocking_uncertain_subs()
    assert rig.runner.results[0]["submission_pending"] is True
    assert rig.runner.results[0]["submitted_at"] == 7
    assert not rig.receipts and len(submitted) == 1


def test_credit_stop_collects_inflight_clips_then_switches_and_retries_failures(rig, monkeypatch):
    connections = _offline_round(monkeypatch, rig.runner)
    current = ["actual-account"]
    switches = []
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **k: current[0])
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k:
                        "Insufficient credits warning [credit=1]"
                        if current[0] == "actual-account" and len(rig.sends) == 3 else None)
    exhausted_at = []

    def inspect(page, ids, *args, **kwargs):
        if current[0] != "actual-account" or len(rig.sends) < 3:
            return rig.inspect(page, ids, *args, **kwargs)
        if not exhausted_at:
            exhausted_at.append(rig.clock.now)
        elapsed = rig.clock.now - exhausted_at[0]
        states = {}
        for tid in ids:
            if tid == "tile-0":
                states[tid] = {"status": "done", "videoSrc": "https://flow.google/video/ready"}
            elif tid == "tile-1":
                # Paid clip keeps generating after the send button turns into
                # "Insufficient credits"; it must be collected, not abandoned.
                states[tid] = ({"status": "done", "videoSrc": "https://flow.google/video/late"}
                               if elapsed >= 60 else {"status": "generating", "progress": 40})
            else:
                states[tid] = ({"status": "failed", "failedText": "Failed"}
                               if elapsed >= 20 else {"status": "generating", "progress": 10})
        return states

    def choose(**kwargs):
        assert rig.clock.now - exhausted_at[0] >= 60
        assert rig.marks == [("actual-account", {"credit": 1})]
        assert rig.runner.completed == {0, 1}
        assert not rig.runner._credit_pending_subs and not rig.runner._unresolved_identity_subs
        assert "stop_current" not in kwargs  # Nothing left to recover on the old profile.
        switches.append(kwargs)
        current[0] = "replacement-account"
        return {"user_id": current[0], "name": "next"}

    monkeypatch.setattr(V, "_inspect_all_pending_tiles", inspect)
    monkeypatch.setattr(P, "switch_to_next_account", choose)
    result = rig.runner.run()

    assert len(switches) == 1 and len(connections) == 2
    # Only the clip that really failed is sent again, on the replacement account.
    assert [idx for idx, _ in rig.sends] == [0, 1, 2, 2, 3, 4, 5, 6, 7]
    assert rig.marks == [("actual-account", {"credit": 1})]
    assert all(item["status"] == "success" for item in result)
    assert result[0]["account_id"] == result[1]["account_id"] == "actual-account"
    assert all(item["account_id"] == "replacement-account" for item in result[2:])
    assert not any(item.get("submission_pending") for item in result)


def test_prior_credit_pending_does_not_hide_new_unrelated_submission_ambiguity(rig, monkeypatch):
    rig.runner._unresolved_identity_subs.update({0, 1})
    rig.runner._credit_pending_subs.add(0)
    assert rig.runner._blocking_uncertain_subs() == {1}


def test_credit_stop_without_tile_preserves_pending_without_a_false_timeout(rig):
    task = dict(sub_idx=0, idx=0, req=rig.reqs[0], status="generating", tile_id=None,
                account_id="actual-account", project_url=rig.runner.project_url)
    rig.runner._set_deferred_stop(V._CreditExhaustedError("out of credits [credit=1]"))
    rig.runner._await_generation(object(), [task])
    assert rig.runner.results[0]["submission_pending"] is True
    assert "超时" not in rig.runner.results[0]["message"]
    assert rig.runner._credit_pending_subs == {0}
