"""Explicit unusual-activity retries require a verified new exit on the same profile."""

from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import proxy_rotator as R

pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def rig(monkeypatch):
    # These cases exercise same-account rotation, independent of local policy.
    monkeypatch.setattr(V, "_unusual_activity_account_switch_after", lambda: 0)
    reqs = [SimpleNamespace(prompt=f"clip {i}", image="", end_image="") for i in range(3)]
    events, rotations, bindings = [], [], []
    runner = V._ChunkRunner(3, 0, reqs, {}, lambda *args: events.append(args), None)
    runner._active_account_id = "profile-3"
    runner.project_url = runner.bound_project_url = "https://flow.google.com/project/original"
    runner.canvas_is_bound = True
    runner.path_to_uuid = {"frame.png": "frame-uuid"}
    runner.preexisting_tile_ids = {"historical-tile"}
    runner.completed = {0}
    runner.results[0] = {"status": "success", "video_url": "/saved.mp4",
                         "account_id": "profile-3", "project_url": runner.project_url}
    runner._submitted_tasks[0] = {"reported": True, "status": "success", "tile_id": "paid-tile"}
    runner._submitted_tiles[(runner.project_url, 0)] = "paid-tile"
    runner._confirmed_config = ("old-config",)
    env = SimpleNamespace(runner=runner, events=events, rotations=rotations, bindings=bindings,
                          result={"success": True, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2",
                                  "message": "verified"})

    def rotate(**kwargs):
        rotations.append(kwargs)
        assert kwargs["cancel_check"] == runner._check_cancel
        if isinstance(env.result, Exception):
            raise env.result
        return env.result

    monkeypatch.setattr(R, "ProxyRotator", lambda: SimpleNamespace(rotate_proxy_verified=rotate))
    monkeypatch.setattr(V, "get_runtime_default_port", lambda: "55000")
    monkeypatch.setattr(V.account_binding, "set_task_account", lambda account: bindings.append(account))
    monkeypatch.setattr(V, "log", lambda *a, **k: None)
    monkeypatch.setattr(V, "_check_cancelled", lambda: None)
    return env


def test_verified_ip_change_preserves_account_canvas_cache_and_paid_results(rig):
    runner = rig.runner
    result = dict(runner.results[0])
    tasks = dict(runner._submitted_tasks)
    tiles = dict(runner._submitted_tiles)

    assert runner._rotate_ip_immediately() is True
    assert rig.rotations[0]["user_id"] == "profile-3"
    assert rig.rotations[0]["port"] == "55000"
    assert rig.bindings == ["profile-3"]
    assert runner._current_account_id() == runner._ip_retry_account == "profile-3"
    assert runner.project_url == runner.bound_project_url == runner._ip_retry_project_url
    assert runner.path_to_uuid == {"frame.png": "frame-uuid"}
    assert runner.preexisting_tile_ids == {"historical-tile"}
    assert runner._submitted_tasks == tasks and runner._submitted_tiles == tiles
    assert runner.results[0] == result and runner.completed == {0}
    assert runner._confirmed_config is None
    assert runner.ip_retry == 1
    assert [stage for _, stage, _ in rig.events] == ["ip_rotating", "ip_rotated"]
    assert runner._blocked_exit_ips["profile-3"] == {"192.0.2.1"}
    assert rig.events[-1][2]["blocked_ip_count"] == 1


@pytest.mark.parametrize("rotation_result", [
    {"success": False, "message": "proxy update refused"},
    {"success": True, "old_ip": "192.0.2.1", "new_ip": "192.0.2.1"},
    {"success": True, "old_ip": "", "new_ip": "192.0.2.2"},
    {"success": True, "old_ip": "192.0.2.1", "new_ip": ""},
    RuntimeError("proxy service unavailable"),
])
def test_failed_rotation_stops_and_keeps_results(rig, monkeypatch, rotation_result):
    runner = rig.runner
    rig.result = rotation_result
    original = dict(runner.results[0])
    uncertain = {"status": "failed", "submission_pending": True, "account_id": "profile-3",
                 "project_url": runner.project_url, "message": "still pending"}
    runner.results[1] = dict(uncertain)
    runner._unresolved_identity_subs.add(1)
    rounds = []

    def blocked(remaining):
        rounds.append([idx for idx, _ in remaining])
        raise V._UnusualActivityError("unusual activity")

    monkeypatch.setattr(runner, "_run_round", blocked)
    result = runner.run()

    # The unconfirmed clip is offered for its one recover-or-resubmit retry.
    assert rounds == [[1, 2]]
    assert len(rig.rotations) == 1
    assert rig.bindings == []
    assert result[0] == original and result[1] == uncertain
    assert result[2]["status"] == "failed" and "Flow 平台异常活动限制" in result[2]["message"]
    assert [idx for idx, stage, _ in rig.events if stage == "video_error"] == [2]
    assert any(stage == "video_warning" and data.get("code") == "flow_unusual_activity"
               for _, stage, data in rig.events)
    assert runner.project_url.endswith("/original")


def test_verified_rotation_retries_only_unfinished_on_original_canvas(rig, monkeypatch):
    runner = rig.runner
    rounds = []

    def round_(remaining):
        rounds.append(([idx for idx, _ in remaining], runner._current_account_id(), runner.project_url))
        index = 1 if len(rounds) == 1 else 2
        runner.completed.add(index)
        runner.results[index] = {"status": "success", "video_url": f"/{index}.mp4"}
        if len(rounds) == 1:
            raise V._UnusualActivityError("unusual activity")
        return True

    monkeypatch.setattr(runner, "_run_round", round_)
    result = runner.run()
    assert [indices for indices, _, _ in rounds] == [[1, 2], [2]]
    assert all(account == "profile-3" and project.endswith("/original") for _, account, project in rounds)
    assert len(rig.rotations) == 1
    assert [stage for _, stage, _ in rig.events].count("ip_rotated") == 1
    assert all(result[i]["status"] == "success" for i in range(3))
    assert not any(stage == "video_error" for _, stage, _ in rig.events)


def test_platform_card_wording_is_preserved_without_duplicate_error(rig, monkeypatch):
    card_error = ("We noticed some unusual activity. Please visit the Help Center. "
                  "You have not been charged for this generation.")
    rig.result = {"success": False, "message": "proxy update refused"}
    rig.runner.results[1] = {"status": "failed", "video_url": None,
                             "message": card_error, "account_id": "profile-3"}
    monkeypatch.setattr(rig.runner, "_run_round", lambda remaining: (_ for _ in ()).throw(
        V._UnusualActivityError("unusual activity")))

    result = rig.runner.run()

    assert result[1]["message"] == card_error
    assert result[2]["status"] == "failed"
    assert [idx for idx, stage, _ in rig.events if stage == "video_error"] == [2]
    assert len(rig.rotations) == 1


@pytest.mark.parametrize("error,handler", [
    (V._IPBlockedError("SECURITY_CHECK_ACCOUNT_SWITCH_REQUIRED: security_check"), "_cooldown_and_switch_account"),
    (V._IPBlockedError("429 rate limited"), "_cooldown_and_switch_account"),
    (V._CreditExhaustedError("out of credits"), "_handle_credit_exhausted"),
])
def test_other_security_rate_and_credit_signals_keep_existing_paths(rig, monkeypatch, error, handler):
    rounds, handlers = [], []

    def round_(remaining):
        rounds.append(True)
        if len(rounds) == 1:
            raise error
        return True

    monkeypatch.setattr(rig.runner, "_run_round", round_)
    monkeypatch.setattr(rig.runner, handler, lambda *args: handlers.append(True) or True)
    rig.runner.run()
    assert len(handlers) == 1 and len(rounds) == 2
    assert rig.rotations == []


def test_unusual_activity_stops_at_retry_limit_without_rotating(rig, monkeypatch):
    monkeypatch.setattr(V, "MAX_IP_RETRIES", 2)
    rig.runner.ip_retry = 2
    monkeypatch.setattr(rig.runner, "_run_round", lambda *a: (_ for _ in ()).throw(
        V._UnusualActivityError("unusual activity")))
    result = rig.runner.run()
    assert rig.rotations == []
    assert result[0]["status"] == "success"
    assert all("Flow 平台异常活动限制" in item["message"] for item in result[1:])
    assert [idx for idx, stage, _ in rig.events if stage == "video_error"] == [1, 2]
    assert any(stage == "video_warning" and data.get("code") == "flow_unusual_activity"
               for _, stage, data in rig.events)


def test_repeated_blocks_exclude_prior_exits_but_keep_new_exit_until_rejected(rig):
    runner = rig.runner
    assert runner._rotate_ip_immediately()
    assert rig.rotations[0]["excluded_ips"] == set()
    assert runner._blocked_exit_ips["profile-3"] == {"192.0.2.1"}

    rig.result = {"success": True, "old_ip": "192.0.2.2", "new_ip": "192.0.2.3"}
    assert runner._rotate_ip_immediately()
    assert rig.rotations[1]["excluded_ips"] == {"192.0.2.1", "192.0.2.2"}
    assert runner._blocked_exit_ips["profile-3"] == {"192.0.2.1", "192.0.2.2"}
    # Each call receives a snapshot; later failures must not alter earlier calls.
    assert rig.rotations[0]["excluded_ips"] == set()

    rig.result = {"success": True, "old_ip": "192.0.2.3", "new_ip": "192.0.2.1"}
    assert not runner._rotate_ip_immediately()
    assert rig.rotations[2]["excluded_ips"] == {"192.0.2.1", "192.0.2.2", "192.0.2.3"}
    assert len(rig.bindings) == 2


def test_blocked_exits_are_scoped_to_account_and_batch(rig):
    runner = rig.runner
    assert runner._rotate_ip_immediately()
    runner._reset_account_canvas("profile-4")
    assert runner._rotate_ip_immediately()
    assert rig.rotations[-1]["excluded_ips"] == set()
    assert runner._blocked_exit_ips["profile-3"] == {"192.0.2.1"}
    assert runner._blocked_exit_ips["profile-4"] == {"192.0.2.1"}
    runner._reset_account_canvas("profile-3")
    rig.result = {"success": True, "old_ip": "192.0.2.2", "new_ip": "192.0.2.3"}
    assert runner._rotate_ip_immediately()
    assert rig.rotations[-1]["excluded_ips"] == {"192.0.2.1", "192.0.2.2"}
    fresh = V._ChunkRunner(0, 0, [], {}, None, None)
    assert fresh._blocked_exit_ips == {}


def test_retry_limit_preserves_newly_completed_and_uncertain_results(rig, monkeypatch):
    runner = rig.runner
    monkeypatch.setattr(V, "MAX_IP_RETRIES", 1)
    runner.ip_retry = 1

    def blocked(remaining):
        runner.completed.add(1)
        runner.results[1] = {"status": "success", "video_url": "/newly-saved.mp4"}
        runner._unresolved_identity_subs.add(2)
        runner.results[2] = {"status": "failed", "submission_pending": True,
                             "message": "still pending"}
        raise V._UnusualActivityError("unusual activity")

    monkeypatch.setattr(runner, "_run_round", blocked)
    result = runner.run()
    assert result[1]["video_url"] == "/newly-saved.mp4"
    assert result[2]["submission_pending"] is True
    assert not any(stage == "video_error" for _, stage, _ in rig.events)


@pytest.mark.parametrize("previous_rotations", [0, 1, 4])
def test_unusual_activity_rotates_without_any_cooldown(rig, monkeypatch, previous_rotations):
    rig.runner.ip_retry = previous_rotations
    monkeypatch.setattr(V.time, "sleep", lambda *args: pytest.fail("must rotate without cooldown"))
    assert rig.runner._rotate_ip_immediately() is True
    assert len(rig.rotations) == 1
    assert rig.runner.ip_retry == previous_rotations + 1


def test_cancellation_before_rotation_never_changes_proxy(rig, monkeypatch):
    def check():
        raise ConnectionError("cancelled")

    monkeypatch.setattr(rig.runner, "_check_cancel", check)
    with pytest.raises(ConnectionError, match="cancelled"):
        rig.runner._rotate_ip_immediately()
    assert rig.rotations == []


def test_cancellation_from_proxy_verification_is_not_reported_as_failure(rig):
    rig.result = ConnectionError("cancelled while verifying")
    with pytest.raises(ConnectionError, match="cancelled while verifying"):
        rig.runner._rotate_ip_immediately()
    assert rig.runner._ip_rotation_failure is None
    assert rig.bindings == []


@pytest.mark.parametrize("failure", ["navigation", "redirect", "toolbar"])
def test_rotated_retry_cannot_fall_back_to_a_new_canvas(rig, monkeypatch, failure):
    runner = rig.runner
    assert runner._rotate_ip_immediately()
    page = SimpleNamespace(url="https://flow.google.com/project/other")

    def goto(url, **kwargs):
        if failure == "navigation":
            raise RuntimeError("navigation failed")
        if failure != "redirect":
            page.url = url

    page.goto = goto
    monkeypatch.setattr(V, "_dismiss_unexpected_overlays", lambda *a: None)
    monkeypatch.setattr(runner, "_wait_toolbar_ready", lambda *a: False)
    monkeypatch.setattr(V, "_click_new_project_button", lambda *a: pytest.fail("must not create a canvas"))
    with pytest.raises(V._IPRetryRecoveryError):
        runner._prepare_page(page)
    assert runner.project_url == runner._ip_retry_project_url
    assert runner.results[0]["status"] == "success"


def test_detection_warning_does_not_signal_terminal_stop(rig):
    # video_generator skips later account legs only on the terminal code.
    rig.runner._begin_unusual_activity_drain()
    warnings = [data for _, stage, data in rig.events if stage == "video_warning"]
    assert [w["code"] for w in warnings] == ["flow_unusual_activity_detected"]
