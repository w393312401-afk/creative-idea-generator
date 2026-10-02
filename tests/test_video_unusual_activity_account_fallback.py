"""Opt-in profile fallback uses no real account probes or browser sessions."""

import json
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_credit as C
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import account_pool as P
from integrations.google_fx.utils import proxy_rotator as R

pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "log", lambda *a, **kw: None)
    monkeypatch.setattr(V, "_check_cancelled", lambda: None)
    monkeypatch.setattr(V, "_unusual_activity_account_switch_after", lambda: 2)
    monkeypatch.setattr(V, "_credit_cost_per_segment", lambda: 15)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 20)
    monkeypatch.setattr(P, "AccountPool", lambda: pytest.fail("real account probe forbidden"))
    monkeypatch.setattr(V, "_connect_fx_page", lambda *a, **kw: pytest.fail("browser forbidden"))
    monkeypatch.setattr(V.account_binding, "set_task_account", lambda uid: None)
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **kw: "old")
    events, rotations, selections = [], [], []
    reqs = [SimpleNamespace(prompt=f"clip {i}", image="", end_image="", output_path=str(tmp_path))
            for i in range(3)]
    runner = V._ChunkRunner(3, 0, reqs, {}, lambda *args: events.append(args), None)
    monkeypatch.setattr(runner, "_check_cancel", lambda: None)
    runner._active_account_id = "old"
    runner.project_url = runner.bound_project_url = "https://flow.google.com/project/old"
    runner.canvas_is_bound = True
    runner.path_to_uuid = {"frame.png": "old-uuid"}
    runner._confirmed_config = ("old",)
    runner.completed = {0}
    runner.results[0] = dict(status="success", video_url="/done.mp4", account_id="old")
    runner._submitted_tasks[1] = dict(sub_idx=1, idx=1, account_id="old",
                                      project_url=runner.project_url, tile_id="paid", reported=False)
    runner._submitted_tiles[(runner.project_url, 1)] = "paid"
    runner._unresolved_identity_subs.add(1)
    runner._ip_drain_pending_subs.add(1)
    runner.results[1] = dict(status="failed", submission_pending=True, account_id="old",
                             project_url=runner.project_url, tile_id="paid")
    state = SimpleNamespace(runner=runner, events=events, rotations=rotations,
                            selections=selections, chosen={"user_id": "new", "credit": 50})

    def rotate(**kwargs):
        rotations.append(kwargs)
        n = len(rotations)
        return dict(success=True, old_ip=f"1.2.3.{n}", new_ip=f"1.2.3.{n + 1}")

    def choose(**kwargs):
        selections.append(kwargs)
        return state.chosen

    monkeypatch.setattr(R, "ProxyRotator", lambda: SimpleNamespace(rotate_proxy_verified=rotate))
    monkeypatch.setattr(P, "switch_to_next_account", choose)
    return state


@pytest.mark.parametrize("value,expected", [(None, 0), (0, 0), (-2, 0), (2, 2), ("3", 3),
                                             ("bad", 0), (None, 0)])
def test_switch_threshold_is_explicit_and_invalid_values_are_disabled(monkeypatch, tmp_path, value, expected):
    monkeypatch.setattr(P, "AI_DIR", tmp_path)
    if value is not None:
        (tmp_path / "server_config.json").write_text(json.dumps({
            "videoUnusualActivityAccountSwitchAfter": value}), encoding="utf-8")
    assert V._unusual_activity_account_switch_after() == expected


def test_two_verified_exits_then_switch_resets_only_account_local_canvas(rig):
    r = rig.runner
    results = dict(r.results)
    receipts = dict(r._submitted_tasks)
    tiles = dict(r._submitted_tiles)
    assert r._recover_unusual_activity()
    assert r._recover_unusual_activity()
    assert not rig.selections
    assert r.project_url.endswith("/old")
    assert r._recover_unusual_activity()
    assert r.ip_retry == 3 and len(rig.rotations) == 2
    assert rig.selections == [{"exclude": {"old"}, "min_credit": 20}]
    assert r._current_account_id() == "new"
    assert r.project_url is r.bound_project_url is None
    assert r.path_to_uuid == {} and r._confirmed_config is None
    assert r._ip_retry_account is None
    assert r.completed == {0} and r.results == results
    assert r._submitted_tasks == receipts and r._submitted_tiles == tiles
    assert r._unresolved_identity_subs == r._ip_drain_pending_subs == {1}
    stages = [stage for _, stage, _ in rig.events]
    assert stages.count("ip_rotated") == 2
    assert stages[-2:] == ["account_switching", "account_switched"]
    assert rig.events[-1][2]["reason"] == "unusual_activity"


def test_disabled_policy_preserves_same_account_behavior(rig, monkeypatch):
    monkeypatch.setattr(V, "_unusual_activity_account_switch_after", lambda: 0)
    for _ in range(4):
        assert rig.runner._recover_unusual_activity()
    assert len(rig.rotations) == 4 and not rig.selections
    assert rig.runner._current_account_id() == "old"


def test_failed_exit_rotation_can_switch_without_counting_as_verified_exit(rig, monkeypatch):
    monkeypatch.setattr(R, "ProxyRotator", lambda: SimpleNamespace(
        rotate_proxy_verified=lambda **kw: dict(success=False, message="exits exhausted")))
    assert rig.runner._recover_unusual_activity()
    assert rig.runner.ip_retry == 1
    assert rig.runner._current_account_id() == "new"
    assert not any(stage == "ip_rotated" for _, stage, _ in rig.events)
    assert rig.runner._ip_rotation_failure is None


def test_exhausted_rotation_and_fallback_stop_and_keep_receipts(rig, monkeypatch):
    r = rig.runner
    rig.chosen = None
    attempted = []

    def blocked(remaining):
        attempted.append((r._current_account_id(), [i for i, _ in remaining]))
        raise V._UnusualActivityError("unusual activity")

    monkeypatch.setattr(r, "_run_round", blocked)
    results = r.run()
    assert attempted == [("old", [1, 2])] * 3
    assert len(rig.rotations) == 2
    assert rig.selections == [{"exclude": {"old"}, "min_credit": 20}]
    assert results[0]["status"] == "success"
    assert results[1]["submission_pending"] is True and results[1]["tile_id"] == "paid"
    assert "Flow 平台异常活动限制" in results[2]["message"]
    assert "号池没有其他" in results[2]["message"]
    assert rig.events[-2][1] == "video_warning"
    assert rig.events[-2][2]["code"] == "flow_unusual_activity"


def test_rejected_profiles_stay_excluded_across_later_fallback(rig):
    r = rig.runner
    for _ in range(3):
        assert r._recover_unusual_activity()
    rig.chosen = None
    for _ in range(2):
        assert r._recover_unusual_activity()
    assert not r._recover_unusual_activity()
    assert rig.selections[-1]["exclude"] == {"old", "new"}
    assert r._current_account_id() == "new"


def test_unusual_activity_rotates_then_switches_within_retry_budget(rig, monkeypatch):
    r = rig.runner
    monkeypatch.setattr(V, "MAX_IP_RETRIES", 3)
    attempted = []

    def blocked(remaining):
        attempted.append(r._current_account_id())
        raise V._UnusualActivityError("unusual activity")

    monkeypatch.setattr(r, "_run_round", blocked)
    results = r.run()
    assert attempted == ["old", "old", "old", "new"]
    assert r.ip_retry == 3 and len(rig.rotations) == 2 and len(rig.selections) == 1
    assert "Flow 平台异常活动限制" in results[2]["message"]
    assert "上限 3 次" in results[2]["message"]


def test_cancel_before_account_selection_does_not_probe(rig, monkeypatch):
    r = rig.runner
    r._unusual_activity_ip_retries["old"] = 2

    def cancelled():
        raise ConnectionError("cancelled")

    monkeypatch.setattr(r, "_check_cancel", cancelled)
    with pytest.raises(ConnectionError, match="cancelled"):
        r._recover_unusual_activity()
    assert not rig.selections and not rig.rotations


@pytest.mark.parametrize("draining,expected", [(False, 0), (True, 2)])
def test_accepted_video_resets_streak_except_when_draining_current_block(rig, monkeypatch, draining, expected):
    r = rig.runner
    r._unusual_activity_ip_retries["old"] = 2
    if draining:
        r._deferred_stop = V._UnusualActivityError("unusual activity")
    monkeypatch.setattr(V, "download_video_via_browser", lambda *a: "/done2.mp4")
    task = dict(sub_idx=2, idx=2, req=r.chunk[2], status="success",
                video_url="https://example.com/video.mp4", account_id="old")
    r._download_and_report_task(object(), task)
    assert r._unusual_activity_ip_retries["old"] == expected
