# -*- coding: utf-8 -*-
"""
🧪 账号池状态/选号逻辑单元测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯逻辑测试，不需要真实浏览器/AdsPower——状态文件指向 tmp_path，
refresh_credit 的真实探测部分用 monkeypatch 打桩。
"""



import pytest
from integrations.google_fx.utils import account_pool as ap


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    state_file = tmp_path / "account_pool.json"
    monkeypatch.setattr(ap, "_STATE_FILE", state_file)
    yield state_file


def test_add_list_remove_account():
    pool = ap.AccountPool()
    pool.add_account("user_a", "主账号")
    pool.add_account("user_b", "备用账号")

    accounts = pool.list_accounts()
    assert {a["user_id"] for a in accounts} == {"user_a", "user_b"}
    # 新增账号的积分是"未探测"（None），不是编造的乐观值——控制台据此显示
    # "未探测"而不是一个看起来有额度的数字。
    assert all(a["credit"] is ap.UNPROBED_CREDIT for a in accounts)
    assert all(a["credit_probed"] is False for a in accounts)
    assert all(a["disabled"] is False for a in accounts)

    pool.remove_account("user_a")
    assert {a["user_id"] for a in pool.list_accounts()} == {"user_b"}


def test_add_account_requires_user_id():
    pool = ap.AccountPool()
    with pytest.raises(ValueError):
        pool.add_account("   ")


def test_set_disabled_toggles_and_excludes_from_pick(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("user_a")
    pool.add_account("user_b")

    # 两个账号都设为"刚探测过且有额度"，跳过 pick_account 里的过期强制刷新分支
    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 10**9)
    for uid in ("user_a", "user_b"):
        state = ap._read_state()
        state[uid]["credit"] = 100
        state[uid]["last_checked_at"] = ap._now_iso()
        ap._write_state(state)

    pool.set_disabled("user_a", True)
    chosen = pool.pick_account(min_credit=1)
    assert chosen["user_id"] == "user_b"


def test_pick_account_returns_none_when_pool_empty():
    pool = ap.AccountPool()
    assert pool.pick_account(min_credit=1) is None


def test_pick_account_prefers_higher_cached_credit(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("low", "低额度")
    pool.add_account("high", "高额度")

    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 10**9)
    state = ap._read_state()
    state["low"]["credit"] = 50
    state["low"]["last_checked_at"] = ap._now_iso()
    state["high"]["credit"] = 900
    state["high"]["last_checked_at"] = ap._now_iso()
    ap._write_state(state)

    chosen = pool.pick_account(min_credit=1)
    assert chosen["user_id"] == "high"


def test_pick_account_skips_cooldown_accounts(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("cooling", "冷却中")
    pool.add_account("ready", "可用")

    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 10**9)
    state = ap._read_state()
    state["cooling"]["credit"] = 900
    state["cooling"]["last_checked_at"] = ap._now_iso()
    from datetime import timedelta
    state["cooling"]["cooldown_until"] = (ap._now() + timedelta(hours=1)).isoformat()
    state["ready"]["credit"] = 100
    state["ready"]["last_checked_at"] = ap._now_iso()
    ap._write_state(state)

    chosen = pool.pick_account(min_credit=1)
    assert chosen["user_id"] == "ready"


def test_mark_exhausted_sets_credit_zero_and_cooldown():
    pool = ap.AccountPool()
    pool.add_account("user_a")
    pool.mark_exhausted("user_a", cooldown_hours=1)

    accounts = {a["user_id"]: a for a in pool.list_accounts()}
    assert accounts["user_a"]["credit"] == 0
    assert accounts["user_a"]["cooldown_until"] is not None


def test_pick_account_falls_back_when_stale_leader_turns_out_exhausted(monkeypatch):
    """最优候选（缓存额度最高）过期，真实刷新后发现耗尽，应该自动跳到下一个候选。"""
    pool = ap.AccountPool()
    pool.add_account("stale_leader", "缓存额度高但已过期")
    pool.add_account("fresh_follower", "缓存额度较低但仍新鲜")

    state = ap._read_state()
    state["stale_leader"]["credit"] = 900
    state["stale_leader"]["last_checked_at"] = None  # 从未探测过 -> 视为过期
    state["fresh_follower"]["credit"] = 200
    state["fresh_follower"]["last_checked_at"] = ap._now_iso()
    ap._write_state(state)

    def fake_probe(user_id, port=None):
        assert user_id == "stale_leader"
        return 0  # 真实探测发现已耗尽

    from integrations.google_fx.services import google_fx_credit as credit_module
    monkeypatch.setattr(credit_module, "probe_flow_credit", fake_probe)

    chosen = pool.pick_account(min_credit=1)
    assert chosen["user_id"] == "fresh_follower"

    # stale_leader 的缓存应该已经被真实刷新结果（0）覆盖
    accounts = {a["user_id"]: a for a in pool.list_accounts()}
    assert accounts["stale_leader"]["credit"] == 0


def test_refresh_credit_keeps_old_value_when_probe_fails(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("user_a")
    state = ap._read_state()
    state["user_a"]["credit"] = 777
    ap._write_state(state)

    from integrations.google_fx.services import google_fx_credit as credit_module
    monkeypatch.setattr(credit_module, "probe_flow_credit", lambda user_id, port=None: None)

    result = pool.refresh_credit("user_a", force=True)
    assert result["credit"] == 777
    assert result["last_probe_status"] == "failed"
    assert result["last_probe_error"]
    assert result["last_probe_at"]


def test_pick_account_never_uses_stale_cache_after_probe_failure(monkeypatch):
    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 30 * 60)
    pool = ap.AccountPool()
    pool.add_account("stale")
    state = ap._read_state()
    state["stale"]["credit"] = 777
    state["stale"]["last_checked_at"] = "2020-01-01T00:00:00+00:00"
    ap._write_state(state)

    from integrations.google_fx.services import google_fx_credit as credit_module
    monkeypatch.setattr(credit_module, "probe_flow_credit", lambda user_id, port=None: None)

    assert pool.pick_account(min_credit=1) is None
    account = pool.list_accounts()[0]
    assert account["credit"] == 777  # 保留仅供人工参考
    assert account["credit_trustworthy"] is False
    assert account["last_probe_status"] == "failed"


def test_refresh_credit_does_not_blame_account_when_browser_was_busy(monkeypatch):
    """浏览器忙 / 排队超时不是账号的问题，不能记成 last_probe_status='failed'。

    记成 failed 会让一排好账号在控制台挂上"⚠️ 最近探测失败"，还会被选号当成
    不可信账号跳过——一次队列拥堵就污染整个号池的健康状态。
    """
    pool = ap.AccountPool()
    pool.add_account("user_busy")
    state = ap._read_state()
    state["user_busy"]["credit"] = 640
    state["user_busy"]["last_probe_status"] = "ok"
    state["user_busy"]["last_checked_at"] = ap._now_iso()
    ap._write_state(state)

    from integrations.google_fx.services import google_fx_credit as credit_module

    def fake_probe(user_id, port=None):
        credit_module._set_probe_error(user_id, "浏览器被其它 FX 任务占用", kind='queue')
        return None

    monkeypatch.setattr(credit_module, "probe_flow_credit", fake_probe)

    result = pool.refresh_credit("user_busy", force=True)
    assert result["last_probe_status"] == "blocked"
    assert result["credit"] == 640
    assert "占用" in result["last_probe_error"]
    # 列表里也仍然是可信积分（credit_trustworthy 只把 'failed' 当不可信）
    listed = {a["user_id"]: a for a in pool.list_accounts()}["user_busy"]
    assert listed["credit_trustworthy"] is True


def test_refresh_credit_marks_login_required_on_login_page(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("user_login")
    state = ap._read_state()
    state["user_login"]["credit"] = 500
    ap._write_state(state)

    from integrations.google_fx.services import google_fx_credit as credit_module

    def fake_probe(user_id, port=None):
        credit_module._set_probe_error(user_id, "🔒 遇到 Google 登录页面，请人工打开 AdsPower 完成登录")
        return None

    monkeypatch.setattr(credit_module, "probe_flow_credit", fake_probe)

    result = pool.refresh_credit("user_login", force=True)
    assert result["last_probe_status"] == "failed"
    assert "登录页面" in result["last_probe_error"]
    assert result["cooldown_reason"] == "login_required"
    assert result["cooldown_until"] is not None



# ── AdsPower 本地 API 限频重试 ────────────────────────────────────────────────
# 背景（2026-07-26 server.log）：撞上 "Too many request per second" 时原代码直接
# break，把「列出全部环境」静默截断成前 N 个——一键导入会漏号。

import json


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload



def _profile_page(n, page_size=100):
    return {"code": 0, "data": {"list": [{"user_id": f"u{i}", "name": f"n{i}"} for i in range(n)]}}


_RATE_LIMITED = {"code": -1, "msg": "Too many request per second"}


def test_rate_limited_page_is_retried_not_truncated(monkeypatch):
    from integrations.google_fx.utils import account_pool as AP

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        # 第一次撞限频，重试一次后放行
        if len(calls) == 1:
            return _FakeResp(_RATE_LIMITED)
        return _FakeResp(_profile_page(3))

    monkeypatch.setattr(AP.requests, "get", fake_get)
    monkeypatch.setattr(AP.time, "sleep", lambda s: None)
    monkeypatch.setattr(AP, "log", lambda *a, **k: None)

    profiles = AP.AccountPool().list_adspower_profiles(port=50325)
    assert len(profiles) == 3, "限频只是瞬时状态，重试后必须拿到完整一页"
    assert len(calls) == 2


def test_rate_limit_gives_up_after_max_retries(monkeypatch):
    from integrations.google_fx.utils import account_pool as AP

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResp(_RATE_LIMITED)

    monkeypatch.setattr(AP.requests, "get", fake_get)
    monkeypatch.setattr(AP.time, "sleep", lambda s: None)
    monkeypatch.setattr(AP, "log", lambda *a, **k: None)

    profiles = AP.AccountPool().list_adspower_profiles(port=50325)
    assert profiles == []
    assert len(calls) == AP.AccountPool._RATE_LIMIT_RETRIES, "不该无限重试"


def test_non_rate_limit_error_is_not_retried(monkeypatch):
    from integrations.google_fx.utils import account_pool as AP

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResp({"code": -1, "msg": "some other failure"})

    monkeypatch.setattr(AP.requests, "get", fake_get)
    monkeypatch.setattr(AP.time, "sleep", lambda s: None)
    monkeypatch.setattr(AP, "log", lambda *a, **k: None)

    assert AP.AccountPool().list_adspower_profiles(port=50325) == []
    assert len(calls) == 1, "非限频错误重试没有意义"


def test_close_browser_success(monkeypatch):
    from integrations.google_fx.utils import account_pool as AP
    from integrations.google_fx.utils import browser as BR

    called_urls = []

    def fake_get(url, timeout=None):
        called_urls.append(url)
        return _FakeResp({"code": 0, "msg": "success"})

    monkeypatch.setattr(BR.requests, "get", fake_get)
    monkeypatch.setattr(BR, "get_runtime_default_port", lambda: 50325)

    pool = AP.AccountPool()
    ok, msg = pool.close_browser("user_123")
    assert ok is True
    assert "user_id=user_123" in called_urls[0]
    assert "50325" in called_urls[0]


def test_close_browser_requires_user_id():
    from integrations.google_fx.utils import account_pool as AP
    pool = AP.AccountPool()
    ok, msg = pool.close_browser("   ")
    assert ok is False
    assert "缺少 user_id" in msg


def test_account_pool_serial_number_support(monkeypatch):
    pool = ap.AccountPool()
    # 手动指定 serial_number
    pool.add_account("user_s1", "测试账号1", serial_number="101")
    accounts = {a["user_id"]: a for a in pool.list_accounts()}
    assert accounts["user_s1"]["serial_number"] == "101"

    # 未指定 serial_number 且 heal 时自动自愈补全
    state = ap._read_state()
    state["user_s2"] = {"name": "测试账号2", "credit": None}
    ap._write_state(state)

    fake_profiles = [
        {"user_id": "user_s2", "serial_number": 202, "name": "测试账号2", "group_name": "g1"}
    ]
    monkeypatch.setattr(pool, "list_adspower_profiles", lambda port=None: fake_profiles)

    healed_accounts = {a["user_id"]: a for a in pool.list_accounts(heal=True)}
    assert healed_accounts["user_s2"]["serial_number"] == "202"


def test_account_pool_record_task_count_and_sorting():
    pool = ap.AccountPool()
    pool.add_account("user_1", "Account 1", serial_number="10")
    pool.add_account("user_2", "Account 2", serial_number="5")
    pool.add_account("user_3", "Account 3", serial_number="20")

    # Record task counts
    pool.record_task_count("user_1", image_count=5, video_count=2)  # 7 total
    pool.record_task_count("user_2", image_count=1, video_count=10) # 11 total
    pool.record_task_count("user_3", image_count=8, video_count=0)  # 8 total

    # Test default/serial sorting
    accs_serial = pool.list_accounts(heal=False, sort_by="serial", sort_order="asc")
    assert [a["user_id"] for a in accs_serial] == ["user_2", "user_1", "user_3"]

    # Test total task sorting
    accs_tasks = pool.list_accounts(heal=False, sort_by="tasks", sort_order="desc")
    assert [a["user_id"] for a in accs_tasks] == ["user_2", "user_3", "user_1"]

    # Test image task sorting
    accs_img = pool.list_accounts(heal=False, sort_by="image_task", sort_order="desc")
    assert [a["user_id"] for a in accs_img] == ["user_3", "user_1", "user_2"]

    # Test video task sorting
    accs_vid = pool.list_accounts(heal=False, sort_by="video_task", sort_order="desc")
    assert [a["user_id"] for a in accs_vid] == ["user_2", "user_1", "user_3"]


def test_account_pool_credit_sorting(monkeypatch):
    pool = ap.AccountPool()
    pool.add_account("user_low", "Low", serial_number="1")
    pool.add_account("user_high", "High", serial_number="2")

    monkeypatch.setattr(ap, "STALE_AFTER_SECONDS", 10**9)
    state = ap._read_state()
    state["user_low"]["credit"] = 100
    state["user_high"]["credit"] = 800
    ap._write_state(state)

    accs_desc = pool.list_accounts(heal=False, sort_by="credit", sort_order="desc")
    assert [a["user_id"] for a in accs_desc] == ["user_high", "user_low"]

    accs_asc = pool.list_accounts(heal=False, sort_by="credit", sort_order="asc")
    assert [a["user_id"] for a in accs_asc] == ["user_low", "user_high"]



