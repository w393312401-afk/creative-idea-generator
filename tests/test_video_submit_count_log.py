"""Submission summaries distinguish new paid requests from local failures."""

from types import SimpleNamespace

import pytest

from integrations.google_fx.models import VideoRequest
from integrations.google_fx.services import google_fx_video as V


pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def summary_rig(monkeypatch):
    messages, receipts = [], []
    reqs = [VideoRequest(prompt=f"clip {index}") for index in range(3)]
    runner = V._ChunkRunner(3, 0, reqs, {}, None, None)
    runner._active_account_id = "test-account"
    monkeypatch.setattr(V, "log", lambda message, *a: messages.append(message))
    monkeypatch.setattr(V, "random_sleep", lambda *a: None)
    monkeypatch.setattr(V, "_check_cancelled", lambda: None)
    monkeypatch.setattr(V, "snapshot_flow_tile_ids", lambda page: [])
    monkeypatch.setattr(V, "detect_page_credit_exhaustion", lambda *a, **k: None)
    monkeypatch.setattr(V, "_inspect_all_pending_tiles", lambda *a, **k: {})
    for name in ("_check_cancel", "_ensure_video_config", "_credit_checkpoint",
                 "_observe_during_pacing", "_wake_ready_tiles", "_deliver_ready_tasks"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_submission", receipts.append)
    monkeypatch.setattr(runner, "_upload_references", lambda *a: {})

    def submit(page, req, *a, **k):
        return {"tile_id": req.prompt, "click_time": 1}

    monkeypatch.setattr(V, "_submit_video_to_canvas", submit)
    return SimpleNamespace(runner=runner, reqs=reqs, messages=messages, receipts=receipts,
                           submit=submit)


@pytest.mark.parametrize("confirmed", [0, 1])
def test_preparation_failures_are_not_logged_as_submitted(summary_rig, monkeypatch, confirmed):
    rig = summary_rig

    def upload(page, remaining):
        if remaining[0][0] == confirmed:
            raise RuntimeError("upload menu unavailable")
        return {}

    monkeypatch.setattr(rig.runner, "_upload_references", upload)
    tasks = rig.runner._submit_tasks(object(), list(enumerate(rig.reqs)), [], {},
                                    prepare_references=True)
    assert len(tasks) == 3  # Failed preparation records remain for reporting.
    assert len(rig.receipts) == confirmed
    assert rig.messages[-1] == (f"📡 本轮已确认提交 {confirmed} 个视频任务，"
                                f"当前 {confirmed} 个在途待收取")


@pytest.mark.parametrize("uncertain", [False, True])
def test_failed_or_uncertain_click_is_not_logged_as_confirmed(summary_rig, monkeypatch, uncertain):
    rig = summary_rig

    def fail(*a, **k):
        error = RuntimeError("submit failed")
        error.submission_started = uncertain
        raise error

    monkeypatch.setattr(V, "_submit_video_to_canvas", fail)
    rig.runner._submit_tasks(object(), [(0, rig.reqs[0])], [], {}, prepare_references=True)
    assert not rig.receipts
    assert rig.messages[-1] == "📡 本轮已确认提交 0 个视频任务，当前 0 个在途待收取"
    assert bool(rig.runner._unresolved_identity_subs) is uncertain


def test_resumed_task_is_inflight_but_not_a_new_submission(summary_rig):
    rig = summary_rig
    resumed = {"tile_id": "existing", "req": rig.reqs[0], "sub_idx": 0,
               "status": "generating"}
    rig.runner._submit_tasks(object(), [(1, rig.reqs[1])], [resumed], {},
                             prepare_references=True)
    assert len(rig.receipts) == 1
    assert rig.messages[-1] == "📡 本轮已确认提交 1 个视频任务，当前 2 个在途待收取"
