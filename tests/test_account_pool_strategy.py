import pytest
from datetime import datetime, timedelta, timezone
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import account_credentials as creds
import server_common


@pytest.fixture(autouse=True)
def isolate_pool_and_creds(tmp_path, monkeypatch):
    pool_file = tmp_path / "account_pool.json"
    creds_file = tmp_path / "account_credentials.json"
    monkeypatch.setattr(ap, "_STATE_FILE", pool_file)
    monkeypatch.setattr(creds, "_STATE_FILE", creds_file)


def test_pick_account_strategy_credit_desc():
    pool = ap.AccountPool()
    pool.add_account("user_low", name="Low Credit", serial_number="1")
    pool.add_account("user_high", name="High Credit", serial_number="2")
    pool.add_account("user_mid", name="Mid Credit", serial_number="3")

    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state["user_low"]["credit"] = 20
        state["user_low"]["last_checked_at"] = now_iso
        state["user_high"]["credit"] = 800
        state["user_high"]["last_checked_at"] = now_iso
        state["user_mid"]["credit"] = 150
        state["user_mid"]["last_checked_at"] = now_iso
        ap._write_state(state)

    chosen = pool.pick_account(min_credit=10, strategy="credit_desc")
    assert chosen is not None
    assert chosen["user_id"] == "user_high"


def test_pick_account_strategy_expiration_asc():
    pool = ap.AccountPool()
    pool.add_account("user_late", name="Late Exp", serial_number="1", expires_at="2026-12-31")
    pool.add_account("user_early", name="Early Exp", serial_number="2", expires_at="2026-09-01")
    pool.add_account("user_none", name="No Exp", serial_number="3")

    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state["user_late"]["credit"] = 500
        state["user_late"]["last_checked_at"] = now_iso
        state["user_early"]["credit"] = 100
        state["user_early"]["last_checked_at"] = now_iso
        state["user_none"]["credit"] = 999
        state["user_none"]["last_checked_at"] = now_iso
        ap._write_state(state)

    # When strategy is expiration_asc, user_early (2026-09-01) comes before user_late (2026-12-31),
    # and accounts without expiration date come after
    chosen = pool.pick_account(min_credit=10, strategy="expiration_asc")
    assert chosen is not None
    assert chosen["user_id"] == "user_early"


def test_pick_account_with_priority_user_ids():
    pool = ap.AccountPool()
    pool.add_account("u1", name="U1", serial_number="1")
    pool.add_account("u2", name="U2", serial_number="2")
    pool.add_account("u3", name="U3", serial_number="3")

    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state["u1"]["credit"] = 1000  # Highest credit in pool, but not in priority list
        state["u1"]["last_checked_at"] = now_iso
        state["u2"]["credit"] = 50    # In priority list
        state["u2"]["last_checked_at"] = now_iso
        state["u3"]["credit"] = 200   # In priority list
        state["u3"]["last_checked_at"] = now_iso
        ap._write_state(state)

    # Priority list is ['u2', 'u3'], u3 has higher credit (200 > 50)
    chosen = pool.pick_account(min_credit=10, priority_user_ids=["u2", "u3"], strategy="credit_desc")
    assert chosen is not None
    assert chosen["user_id"] == "u3"

    # If priority accounts are in cooldown, falls back to u1
    with ap._LOCK:
        state = ap._read_state()
        cool_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state["u2"]["cooldown_until"] = cool_time
        state["u3"]["cooldown_until"] = cool_time
        ap._write_state(state)

    chosen_fallback = pool.pick_account(min_credit=10, priority_user_ids=["u2", "u3"], strategy="credit_desc")
    assert chosen_fallback is not None
    assert chosen_fallback["user_id"] == "u1"


def test_set_expires_at():
    pool = ap.AccountPool()
    pool.add_account("u1", name="U1", serial_number="1")

    res = pool.set_expires_at("u1", "2026-10-15")
    assert res["expires_at"] == "2026-10-15"

    multiple = pool.set_expires_at_multiple(["u1"], "2026-11-20")
    assert len(multiple) == 1
    assert multiple[0]["expires_at"] == "2026-11-20"


def test_server_common_select_pool_account_and_ring():
    pool = ap.AccountPool()
    pool.add_account("acc1", name="Acc 1", serial_number="1", expires_at="2026-12-01")
    pool.add_account("acc2", name="Acc 2", serial_number="2", expires_at="2026-09-01")
    pool.add_account("acc3", name="Acc 3", serial_number="3", expires_at="2026-10-01")

    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state["acc1"]["credit"] = 100
        state["acc1"]["last_checked_at"] = now_iso
        state["acc2"]["credit"] = 200
        state["acc2"]["last_checked_at"] = now_iso
        state["acc3"]["credit"] = 300
        state["acc3"]["last_checked_at"] = now_iso
        ap._write_state(state)

    # Test selection with expiration_asc strategy and priority accounts
    config = {
        "videoAccountPoolMinCredit": 10,
        "googleFxPriorityUserIds": ["acc1", "acc3"],
        "googleFxAccountStrategy": "expiration_asc",
    }

    # In priority list ['acc1', 'acc3'], acc3 expires 2026-10-01 (earlier than acc1 2026-12-01)
    selected = server_common._select_pool_account(config, pool)
    assert selected == "acc3"
    assert config["googleFxUserId"] == "acc3"

    # Test rotation ring organization
    ring = server_common._account_rotation_ring(config, pool, first_user_id="acc3")
    # acc3 first (first_user_id), then acc1 (priority), then acc2 (non-priority)
    assert ring == ["acc3", "acc1", "acc2"]


def test_effective_config_carries_pool_scheduling_keys():
    """号池调度字段必须被 effective_config 从 SERVER_CONFIG 搬进生成配置。

    回归用：这两个键曾经存进了 server_config.json 却没登记进 effective_config 的搬运表，
    于是 _select_pool_account 永远看不到它们——选号策略与优先级实例整条链路是死的。
    """
    saved = {k: server_common.SERVER_CONFIG.get(k)
             for k in ('googleFxAccountStrategy', 'googleFxPriorityUserIds')}
    try:
        server_common.SERVER_CONFIG['googleFxAccountStrategy'] = 'expiration_asc'
        server_common.SERVER_CONFIG['googleFxPriorityUserIds'] = ['accX', 'accY']
        merged = server_common.effective_config({})
        assert merged.get('googleFxAccountStrategy') == 'expiration_asc'
        assert merged.get('googleFxPriorityUserIds') == ['accX', 'accY']

        # 浏览器 localStorage 里的旧值不得覆盖服务端配置
        merged = server_common.effective_config({
            'googleFxAccountStrategy': 'credit_desc',
            'googleFxPriorityUserIds': [],
        })
        assert merged.get('googleFxAccountStrategy') == 'expiration_asc'
        assert merged.get('googleFxPriorityUserIds') == ['accX', 'accY']
    finally:
        for k, v in saved.items():
            if v is None:
                server_common.SERVER_CONFIG.pop(k, None)
            else:
                server_common.SERVER_CONFIG[k] = v


def test_rotation_strategy_balances_by_task_count():
    """'均衡轮换' 必须真的按累计任务数摊平，而不是悄悄退化成 credit_desc。"""
    pool = ap.AccountPool()
    pool.add_account("busy", name="Busy", serial_number="1")
    pool.add_account("idle", name="Idle", serial_number="2")

    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        # 积分更高但已经干了一堆活；credit_desc 会选它，rotation 不该选它
        state["busy"]["credit"] = 500
        state["busy"]["last_checked_at"] = now_iso
        state["busy"]["image_task_count"] = 40
        state["idle"]["credit"] = 100
        state["idle"]["last_checked_at"] = now_iso
        state["idle"]["image_task_count"] = 0
        ap._write_state(state)

    assert pool.pick_account(min_credit=10, strategy="credit_desc")["user_id"] == "busy"
    assert pool.pick_account(min_credit=10, strategy="rotation")["user_id"] == "idle"

    config = {"videoAccountPoolMinCredit": 10, "googleFxAccountStrategy": "rotation"}
    ring = server_common._account_rotation_ring(config, pool, first_user_id=None)
    assert ring == ["idle", "busy"]
