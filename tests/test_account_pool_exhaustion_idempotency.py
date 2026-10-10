"""Repeated quota notifications must not overwrite the page's measured balance."""

from datetime import timedelta

import pytest

from integrations.google_fx.utils import account_pool as ap


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_STATE_FILE", tmp_path / "account_pool.json")
    monkeypatch.setattr(ap, "_get_min_credit_threshold", lambda: 15)
    monkeypatch.setattr(ap, "log", lambda *args: None)
    value = ap.AccountPool()
    value.add_account("test_account", name="Test", serial_number="10")
    return value


def account_state():
    return ap._read_state()["test_account"]


def test_duplicate_quota_notification_preserves_measured_one_credit(pool):
    pool.mark_exhausted("test_account", credit=1)
    pool.mark_exhausted("test_account")

    state = account_state()
    assert state["credit"] == 1
    assert state["credit_source"] == "measured"
    assert state["disabled"] is True
    assert state["disabled_reason"] == "zero_credit"
    assert state["cooldown_reason"] == "quota_exhausted"


@pytest.mark.parametrize("previous_credit", [None, 100])
def test_first_unknown_exhaustion_still_uses_zero(pool, previous_credit):
    if previous_credit is not None:
        pool.record_measured_credit("test_account", previous_credit)
    pool.mark_exhausted("test_account")

    state = account_state()
    assert state["credit"] == 0
    assert state["credit_source"] == "exhaustion_signal"


def test_new_exhaustion_after_clearing_cooldown_does_not_reuse_old_measurement(pool):
    pool.mark_exhausted("test_account", credit=1)
    pool.clear_cooldown("test_account")
    pool.mark_exhausted("test_account")

    assert account_state()["credit"] == 0
    assert account_state()["credit_source"] == "exhaustion_signal"


def test_expired_quota_cooldown_does_not_reuse_old_measurement(pool, monkeypatch):
    pool.mark_exhausted("test_account", credit=30)
    later = ap._now() + timedelta(hours=25)
    monkeypatch.setattr(ap, "_now", lambda: later)
    pool.mark_exhausted("test_account")

    assert account_state()["credit"] == 0
    assert account_state()["credit_source"] == "exhaustion_signal"


def test_new_measurement_can_replace_previous_exhaustion_balance(pool):
    pool.mark_exhausted("test_account", credit=1)
    pool.mark_exhausted("test_account", credit=0)

    assert account_state()["credit"] == 0
    assert account_state()["credit_source"] == "measured"


def test_image_daily_quota_keeps_its_independent_balance_behavior(pool):
    pool.record_measured_credit("test_account", 500)
    pool.mark_image_quota_exceeded("test_account")

    state = account_state()
    assert state["credit"] == 500
    assert state["credit_source"] == "measured"
    assert state["disabled"] is False
    assert state["cooldown_reason"] == "image_quota_exceeded"


def test_estimated_balance_is_not_preserved_as_a_measurement(pool):
    pool.mark_exhausted("test_account", credit=1)
    state = ap._read_state()
    state["test_account"]["credit_source"] = "estimated"
    ap._write_state(state)
    pool.mark_exhausted("test_account")

    assert account_state()["credit"] == 0
    assert account_state()["credit_source"] == "exhaustion_signal"
