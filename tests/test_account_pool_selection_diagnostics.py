"""选号失败要解释真实阻塞原因，诊断不得重探、改余额或泄露异常原文。"""

from datetime import datetime, timedelta, timezone

import pytest

import server_common
from integrations.google_fx.utils import account_pool as ap


@pytest.fixture
def pool_state(tmp_path, monkeypatch):
    now = datetime(2026, 9, 16, 22, 54, tzinfo=timezone.utc)
    path = tmp_path / "account_pool.json"
    monkeypatch.setattr(ap, "_STATE_FILE", path)
    monkeypatch.setattr(ap, "_now", lambda: now)
    monkeypatch.setattr(ap, "_get_min_credit_threshold", lambda: 15)
    monkeypatch.setattr(ap, "PROBE_RETRY_AFTER_SECONDS", 600)
    monkeypatch.setattr(ap, "PROBE_BLOCKED_RETRY_AFTER_SECONDS", 120)
    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 21600)
    return ap.AccountPool(), path, now


def test_failed_adspower_probe_keeps_backoff_and_reports_real_cause(pool_state, monkeypatch):
    pool, path, now = pool_state
    ap._write_state({
        "cached": {
            "credit": 800,
            "last_checked_at": (now - timedelta(days=1)).isoformat(),
            "last_probe_status": "failed",
            "last_probe_at": (now - timedelta(minutes=7)).isoformat(),
            "last_probe_error": "AdsPower 本地 API 服务无法连接；secret-marker",
        },
        "unprobed": {
            "credit": None,
            "last_probe_status": "failed",
            "last_probe_at": (now - timedelta(minutes=6)).isoformat(),
            "last_probe_error": "AdsPower 本地 API 服务无法连接",
        },
        "manual": {"credit": 500, "disabled": True},
    })
    before = path.read_bytes()
    from integrations.google_fx.services import google_fx_credit

    def no_probe(*args, **kwargs):
        pytest.fail("失败退避和诊断均不能重新打开浏览器")

    monkeypatch.setattr(google_fx_credit, "probe_flow_credit", no_probe)
    # 避免显示用的命名自愈/凭据合并；选号与诊断仍走真实池实现。
    monkeypatch.setattr(pool, "list_accounts", lambda: [{"user_id": "cached"}])
    with pytest.raises(RuntimeError) as exc:
        server_common._select_pool_account({"videoAccountPoolMinCredit": 15}, pool)

    message = str(exc.value)
    assert "2 个账号积分探测失败" in message
    assert "1 个账号已禁用" in message
    assert "最近一次积分探测因 AdsPower 本地 API 无法连接而失败" in message
    assert "2 个账号处于探测重试等待期" in message
    assert "180 秒" in message
    assert "积分不足" not in message
    assert "secret-marker" not in message
    assert path.read_bytes() == before


def test_diagnostics_distinguish_cooldown_credit_and_busy_browser(pool_state):
    pool, path, now = pool_state
    ap._write_state({
        "low": {"credit": 10, "last_checked_at": now.isoformat()},
        "login": {
            "credit": 500,
            "cooldown_until": (now + timedelta(hours=1)).isoformat(),
            "cooldown_reason": "login_required",
        },
        "quota": {
            "credit": 500,
            "cooldown_until": (now + timedelta(hours=1)).isoformat(),
            "cooldown_reason": "image_quota_exceeded",
        },
        "busy": {
            "credit": None,
            "last_probe_status": "blocked",
            "last_probe_at": (now - timedelta(seconds=30)).isoformat(),
        },
        "unknown": {"credit": None},
    })
    before = path.read_bytes()
    message = pool.selection_unavailable_message(min_credit=15)
    assert "1 个账号积分低于最低要求" in message
    assert "1 个账号登录失效，处于冷却期" in message
    assert "1 个账号处于冷却期" in message
    assert "1 个账号浏览器忙，积分探测尚未完成" in message
    assert "1 个账号积分尚未确认" in message
    assert "90 秒" in message
    assert path.read_bytes() == before


def test_expired_backoff_does_not_report_waiting(pool_state):
    pool, _, now = pool_state
    ap._write_state({"failed": {
        "credit": None,
        "last_probe_status": "failed",
        "last_probe_at": (now - timedelta(minutes=20)).isoformat(),
    }})
    message = pool.selection_unavailable_message()
    assert "积分探测失败" in message
    assert "等待期" not in message


@pytest.mark.parametrize("broken_diagnostics", [False, True])
def test_selection_fallback_does_not_blame_all_accounts(broken_diagnostics):
    class Pool:
        def list_accounts(self):
            return [{"user_id": "a"}]

        def pick_account(self, **kwargs):
            return None

    pool = Pool()
    if broken_diagnostics:
        def unavailable(**kwargs):
            raise RuntimeError("diagnostic failure")
        pool.selection_unavailable_message = unavailable

    with pytest.raises(RuntimeError, match="号池暂时没有可用账号") as exc:
        server_common._select_pool_account({}, pool)
    assert "所有账号积分不足" not in str(exc.value)


def test_selection_passes_configured_threshold_to_diagnostics():
    class Pool:
        def list_accounts(self):
            return [{"user_id": "a"}]

        def pick_account(self, **kwargs):
            return None

        def selection_unavailable_message(self, min_credit):
            assert min_credit == 50
            return "号池：1 个账号积分低于最低要求"

    with pytest.raises(RuntimeError, match="1 个账号积分低于最低要求"):
        server_common._select_pool_account({"videoAccountPoolMinCredit": 50}, Pool())
