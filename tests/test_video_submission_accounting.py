"""Offline accounting: confirmed receipts, measurements, cancellation and ownership."""
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import account_pool as A


@pytest.fixture
def pool(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_STATE_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(A, "_get_min_credit_threshold", lambda: 15)
    monkeypatch.setattr(A.AccountPool, "_profile_info_map", lambda self: {})
    monkeypatch.setattr(A, "log", lambda *a, **k: None)
    p = A.AccountPool()
    for account in ("bound-account", "default-account"):
        p.add_account(account, name=account, serial_number="1")
        p.record_measured_credit(account, 100)
    return p


def test_measured_balance_replaces_estimates_and_duplicate_receipt_does_not_deduct(pool):
    assert pool.record_video_submission("bound-account", "tile-1", 15)["credit"] == 85
    pool.record_measured_credit("bound-account", 70)
    duplicate = pool.record_video_submission("bound-account", "tile-1", 15)
    assert duplicate["credit"] == 70
    assert duplicate["video_task_count"] == 1
    assert duplicate["credit_source"] == "measured"
    next_submission = pool.record_video_submission("bound-account", "tile-2", 15)
    assert next_submission["credit"] == 55
    assert next_submission["video_task_count"] == 2
    assert next_submission["estimated_spent_since_measurement"] == 15
    assert A._read_state()["default-account"]["credit"] == 100


def test_estimated_zero_requires_measurement_instead_of_disabling(pool):
    pool.record_video_submission("bound-account", "expensive", 200)
    state = A._read_state()["bound-account"]
    assert state["credit"] == 0 and not state["disabled"]
    assert A._credit_is_stale(state)
    pool.record_measured_credit("bound-account", 0)
    assert A._read_state()["bound-account"]["disabled"] is True
    pool.record_measured_credit("bound-account", 60)
    assert A._read_state()["bound-account"]["disabled"] is False


def test_editing_account_preserves_receipts_and_estimate_source(pool):
    pool.record_video_submission("bound-account", "tile-1", 100)
    pool.add_account("bound-account", name="Renamed", serial_number="1")
    receipt = pool.record_video_submission("bound-account", "tile-1", 100)
    assert receipt["credit_source"] == "estimated"
    assert receipt["video_task_count"] == 1 and not receipt["disabled"]


def test_runner_uses_configured_cost_and_actual_account(pool, monkeypatch):
    monkeypatch.setattr(V, "_credit_cost_per_segment", lambda: 21)
    monkeypatch.setattr(V, "get_runtime_default_user_id", lambda: "default-account")
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **k: "bound-account")
    runner = V._ChunkRunner(1, 0, [SimpleNamespace(prompt="p")], {}, None, None)
    runner.project_url = "canvas"
    metadata = runner._submission_metadata(runner.chunk[0], {"tile_id": "tile", "click_time": 1})
    runner._record_submission(metadata)
    runner._record_submission(metadata)
    assert A._read_state()["bound-account"]["credit"] == 79
    assert A._read_state()["default-account"]["credit"] == 100


def test_credit_checkpoint_updates_bound_account_only(pool, monkeypatch):
    from integrations.google_fx.services import google_fx_credit as C
    monkeypatch.setattr(V, "_CREDIT_RECHECK_EVERY_SUBMITS", 1)
    monkeypatch.setattr(V, "get_runtime_default_user_id", lambda: "default-account")
    monkeypatch.setattr(V.account_binding, "resolve_account", lambda **k: "bound-account")
    monkeypatch.setattr(C, "read_page_credit_via_menu", lambda page: 40)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 15)
    runner = V._ChunkRunner(1, 0, [SimpleNamespace(prompt="p")], {}, None, None)
    assert runner._credit_checkpoint(object(), remaining_after=1) is None
    assert A._read_state()["bound-account"]["credit"] == 40
    assert A._read_state()["default-account"]["credit"] == 100
