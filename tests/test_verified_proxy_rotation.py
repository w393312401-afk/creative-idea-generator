"""Forced recovery must prove a different exit and fail closed on partial updates."""

from types import SimpleNamespace

import pytest

from integrations.google_fx.utils.proxy_rotator import ProxyRotator


@pytest.fixture
def setup_rotation(monkeypatch):
    rotator = ProxyRotator()
    rotator.auto_rotate = False
    rotator.rotate_mode = "list"
    old = {"proxy_type": "http", "proxy_host": "old", "proxy_port": "80"}
    same = {"proxy_type": "http", "proxy_host": "same-exit", "proxy_port": "80"}
    new = {"proxy_type": "http", "proxy_host": "new", "proxy_port": "80"}
    state = {"saved": old, "calls": []}
    monkeypatch.setattr(rotator, "_read_profile_proxy", lambda uid, port: state["saved"])
    monkeypatch.setattr(rotator, "_verified_rotation_candidates", lambda: iter([old, same, new]))
    monkeypatch.setattr(rotator, "_probe_proxy_ip", lambda cfg:
                        "203.0.113.2" if cfg["proxy_host"] == "new" else "203.0.113.1")
    monkeypatch.setattr(rotator, "_stop_browser_verified",
                        lambda uid, port: state["calls"].append(("stop", uid)))

    def update(uid, port, config):
        state["calls"].append(("update", uid, config["proxy_host"]))
        state["saved"] = config
        return True

    monkeypatch.setattr(rotator, "_update_profile_proxy", update)
    monkeypatch.setattr(rotator, "_read_counter_info", lambda: {"proxy_index": 4})
    monkeypatch.setattr(rotator, "_write_counter_info",
                        lambda *args: state["calls"].append(("counter", *args)))
    return SimpleNamespace(rotator=rotator, state=state, old=old, new=new, same=same)


def test_forced_rotation_ignores_normal_switch_and_skips_same_exit(setup_rotation):
    s = setup_rotation
    result = s.rotator.rotate_proxy_verified("profile-3", port="50325")
    assert result["success"]
    assert (result["old_ip"], result["new_ip"]) == ("203.0.113.1", "203.0.113.2")
    assert s.state["calls"] == [("stop", "profile-3"), ("update", "profile-3", "new"),
                                  ("counter", 0, 4)]


def test_no_different_exit_keeps_browser_and_proxy_untouched(setup_rotation, monkeypatch):
    s = setup_rotation
    monkeypatch.setattr(s.rotator, "_probe_proxy_ip", lambda cfg: "203.0.113.1")
    result = s.rotator.rotate_proxy_verified("profile-3")
    assert not result["success"]
    assert s.state["calls"] == []


def test_failed_original_probe_does_not_leak_credentials_or_change_profile(setup_rotation, monkeypatch):
    s = setup_rotation
    def fail(_config):
        raise ValueError("http://username:secret@proxy")
    monkeypatch.setattr(s.rotator, "_probe_proxy_ip", fail)
    result = s.rotator.rotate_proxy_verified("profile-3")
    assert not result["success"]
    assert "secret" not in str(result)
    assert s.state["calls"] == []


def test_candidate_probe_failure_can_try_another_route(setup_rotation, monkeypatch):
    s = setup_rotation
    def probe(cfg):
        if cfg["proxy_host"] == "same-exit":
            raise OSError("unreachable")
        return "203.0.113.2" if cfg["proxy_host"] == "new" else "203.0.113.1"
    monkeypatch.setattr(s.rotator, "_probe_proxy_ip", probe)
    assert s.rotator.rotate_proxy_verified("profile-3")["success"]


def test_failed_browser_stop_prevents_proxy_mutation(setup_rotation, monkeypatch):
    s = setup_rotation
    def fail(*_args):
        raise RuntimeError("browser remains active")
    monkeypatch.setattr(s.rotator, "_stop_browser_verified", fail)
    assert not s.rotator.rotate_proxy_verified("profile-3")["success"]
    assert s.state["calls"] == []


def test_readback_mismatch_rolls_back_and_does_not_retry(setup_rotation, monkeypatch):
    s = setup_rotation
    monkeypatch.setattr(s.rotator, "_read_profile_proxy", lambda *args: s.old)
    result = s.rotator.rotate_proxy_verified("profile-3")
    assert not result["success"]
    assert s.state["saved"] == s.old
    assert s.state["calls"][-1] == ("update", "profile-3", "old")
    assert not any(c[0] == "counter" for c in s.state["calls"])


def test_post_update_exit_regression_rolls_back(setup_rotation, monkeypatch):
    s = setup_rotation
    def probe(cfg):
        if cfg["proxy_host"] == "new" and not s.state["calls"]:
            return "203.0.113.2"
        return "203.0.113.1"
    monkeypatch.setattr(s.rotator, "_probe_proxy_ip", probe)
    assert not s.rotator.rotate_proxy_verified("profile-3")["success"]
    assert s.state["saved"] == s.old


def test_post_update_historical_blocked_exit_rolls_back(setup_rotation, monkeypatch):
    s = setup_rotation

    def probe(cfg):
        if cfg["proxy_host"] == "new":
            return "203.0.113.3" if s.state["calls"] else "203.0.113.2"
        return "203.0.113.1"

    monkeypatch.setattr(s.rotator, "_probe_proxy_ip", probe)
    result = s.rotator.rotate_proxy_verified("profile-3", excluded_ips={"203.0.113.3"})
    assert not result["success"]
    assert "已被本任务拦截" in result["message"]
    assert s.state["saved"] == s.old
    assert not any(c[0] == "counter" for c in s.state["calls"])


def test_cancel_after_update_restores_proxy_and_propagates(setup_rotation):
    s = setup_rotation
    def cancel():
        if s.state["saved"] == s.new:
            raise RuntimeError("任务已取消")
    with pytest.raises(RuntimeError, match="任务已取消"):
        s.rotator.rotate_proxy_verified("profile-3", cancel_check=cancel)
    assert s.state["saved"] == s.old
    assert not any(c[0] == "counter" for c in s.state["calls"])


def test_profile_readback_never_uses_another_account(monkeypatch):
    from integrations.google_fx.utils import proxy_rotator as module
    response = {"code": 0, "data": {"list": [
        {"user_id": "profile-2", "user_proxy_config": {"proxy_type": "http", "proxy_host": "wrong"}},
        {"user_id": "profile-3", "user_proxy_config": {"proxy_type": "http", "proxy_host": "right"}},
    ]}}
    monkeypatch.setattr(module.requests, "get", lambda *a, **kw: SimpleNamespace(json=lambda: response))
    assert ProxyRotator()._read_profile_proxy("profile-3", 50325)["proxy_host"] == "right"
    with pytest.raises(RuntimeError, match="未找到"):
        ProxyRotator()._read_profile_proxy("missing", 50325)


def test_list_rotation_advances_cursor_across_recoveries(tmp_path, monkeypatch):
    from integrations.google_fx.utils import proxy_rotator as module
    from integrations.google_fx.utils.proxy_pool import ProxyPool
    monkeypatch.setattr(module, "AI_DIR", tmp_path)
    monkeypatch.setattr(ProxyPool, "usable_proxies", lambda self: [])
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "proxy_pool.txt").write_text("a:80\nb:80\nc:80\n")
    rotator = ProxyRotator()
    rotator.rotate_mode = "list"
    saved = {"proxy_type": "http", "proxy_host": "a", "proxy_port": "80", "proxy_soft": "other"}
    monkeypatch.setattr(rotator, "_read_profile_proxy", lambda *args: dict(saved))
    monkeypatch.setattr(rotator, "_probe_proxy_ip", lambda config: {"a": "203.0.113.1", "b": "203.0.113.2", "c": "203.0.113.3"}[config["proxy_host"]])
    monkeypatch.setattr(rotator, "_stop_browser_verified", lambda *args: None)
    def update(uid, port, config):
        assert "_rotation_index" not in config
        saved.clear()
        saved.update(config)
        return True
    monkeypatch.setattr(rotator, "_update_profile_proxy", update)
    assert rotator.rotate_proxy_verified("profile-3")["new_ip"] == "203.0.113.2"
    assert rotator.rotate_proxy_verified("profile-3")["new_ip"] == "203.0.113.3"


def test_browser_stop_waits_for_delayed_adspower_exit(monkeypatch):
    from integrations.google_fx.utils import proxy_rotator as module
    polls = []
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    def get(url, **kwargs):
        if url.endswith('/stop'):
            return SimpleNamespace(json=lambda: {"code": 0})
        polls.append(url)
        return SimpleNamespace(json=lambda: {"code": 0, "data": {
            "status": "Inactive" if len(polls) >= 10 else "Active"}})
    monkeypatch.setattr(module.requests, "get", get)
    ProxyRotator()._stop_browser_verified("profile-3", 50325)
    assert len(polls) == 10


@pytest.fixture
def candidate_pool(tmp_path, monkeypatch):
    from integrations.google_fx.utils import proxy_pool as pool_module
    from integrations.google_fx.utils import proxy_rotator as rotator_module

    monkeypatch.setattr(pool_module, "_STATE_FILE", tmp_path / "proxy_pool.json")
    monkeypatch.setattr(rotator_module, "AI_DIR", tmp_path)

    def seed(rows, cursor=0):
        pool_module._write_state({
            "rotate_index": cursor,
            "proxies": {
                host: {
                    "host": host, "port": "80", "proxy_type": "http",
                    "source": "static", "created_at": f"2026-09-26T00:00:{index:02d}",
                    **extra,
                }
                for index, (host, extra) in enumerate(rows)
            },
        })
        return ProxyRotator()

    return seed


def test_candidates_exhaust_static_then_us_then_other_airport_despite_cursor(candidate_pool):
    rotator = candidate_pool([
        ("airport-other", {"source": "airport", "fallback_tier": 1}),
        ("airport-us-1", {"source": "airport", "fallback_tier": 0}),
        ("airport-us-2", {"source": "airport", "fallback_tier": 0}),
        ("static-1", {}), ("static-2", {}), ("static-3", {}),
    ], cursor=11)

    hosts = [entry["proxy_host"] for entry in rotator._verified_rotation_candidates()]

    assert set(hosts[:3]) == {"static-1", "static-2", "static-3"}
    assert set(hosts[3:5]) == {"airport-us-1", "airport-us-2"}
    assert hosts[5:] == ["airport-other"]
    assert len(hosts) == len(set(hosts))


@pytest.mark.parametrize("static_count", [7, 23])
def test_failed_static_and_us_exits_reach_other_airport(candidate_pool, monkeypatch, static_count):
    rotator = candidate_pool([
        ("airport-other", {"source": "airport", "fallback_tier": 1}),
        ("airport-us", {"source": "airport", "fallback_tier": 0}),
        *[(f"static-{index}", {}) for index in range(static_count)],
    ], cursor=static_count - 1)
    state = {"saved": {"proxy_type": "http", "proxy_host": "old", "proxy_port": "80"}}
    probed = []

    def probe(config):
        host = config["proxy_host"]
        probed.append(host)
        if host.startswith("static-") or host == "airport-us":
            raise OSError("unreachable")
        return "203.0.113.1" if host == "old" else "203.0.113.2"

    def update(_uid, _port, config):
        state["saved"] = dict(config)
        return True

    monkeypatch.setattr(rotator, "_read_profile_proxy", lambda *args: dict(state["saved"]))
    monkeypatch.setattr(rotator, "_probe_proxy_ip", probe)
    monkeypatch.setattr(rotator, "_stop_browser_verified", lambda *args: None)
    monkeypatch.setattr(rotator, "_update_profile_proxy", update)
    monkeypatch.setattr(rotator, "_read_counter_info", lambda: {"proxy_index": 0})
    monkeypatch.setattr(rotator, "_write_counter_info", lambda *args: None)

    result = rotator.rotate_proxy_verified("profile-3")

    assert result["success"]
    assert state["saved"]["proxy_host"] == "airport-other"
    assert probed[0] == "old"
    assert set(probed[1:static_count + 1]) == {f"static-{index}" for index in range(static_count)}
    assert probed[static_count + 1:] == ["airport-us", "airport-other", "airport-other"]


def test_candidates_skip_disabled_and_failed_routes_in_all_tiers(candidate_pool):
    rotator = candidate_pool([
        ("static-disabled", {"disabled": True}),
        ("static-failed", {"last_check_status": "failed"}),
        ("airport-us-disabled", {"source": "airport", "fallback_tier": 0, "disabled": True}),
        ("airport-us-failed", {"source": "airport", "fallback_tier": 0, "last_check_status": "failed"}),
        ("airport-other-disabled", {"source": "airport", "fallback_tier": 1, "disabled": True}),
        ("airport-other-failed", {"source": "airport", "fallback_tier": 1, "last_check_status": "failed"}),
        ("airport-other", {"source": "airport", "fallback_tier": 1}),
        ("static", {}),
    ], cursor=5)

    assert [entry["proxy_host"] for entry in rotator._verified_rotation_candidates()] == ["static", "airport-other"]


def test_reachable_but_blocked_static_exits_fall_back_without_cycling(candidate_pool, monkeypatch):
    rotator = candidate_pool([
        ("static-1", {}), ("static-2", {}),
        ("airport-us", {"source": "airport", "fallback_tier": 0}),
        ("airport-other", {"source": "airport", "fallback_tier": 1}),
    ])
    state = {"saved": {"proxy_type": "http", "proxy_host": "static-1", "proxy_port": "80"}}
    exits = {"static-1": "203.0.113.1", "static-2": "203.0.113.2",
             "airport-us": "203.0.113.3", "airport-other": "203.0.113.4"}
    applied = []

    def update(uid, port, config):
        state["saved"] = dict(config)
        applied.append(config["proxy_host"])
        return True

    monkeypatch.setattr(rotator, "_read_profile_proxy", lambda *args: dict(state["saved"]))
    monkeypatch.setattr(rotator, "_probe_proxy_ip", lambda cfg: exits[cfg["proxy_host"]])
    monkeypatch.setattr(rotator, "_stop_browser_verified", lambda *args: None)
    monkeypatch.setattr(rotator, "_update_profile_proxy", update)
    monkeypatch.setattr(rotator, "_read_counter_info", lambda: {"proxy_index": 0})
    monkeypatch.setattr(rotator, "_write_counter_info", lambda *args: None)

    blocked = set()
    for expected_host in ("static-2", "airport-us", "airport-other"):
        snapshot = set(blocked)
        result = rotator.rotate_proxy_verified("profile-3", excluded_ips=blocked)
        assert result["success"]
        assert blocked == snapshot  # The rotator cannot mutate caller state.
        assert result["new_ip"] == exits[expected_host]
        blocked.add(result["old_ip"])

    result = rotator.rotate_proxy_verified("profile-3", excluded_ips=blocked)
    assert not result["success"] and "停止重试" in result["message"]
    assert applied == ["static-2", "airport-us", "airport-other"]
    assert state["saved"]["proxy_host"] == "airport-other"


def test_normal_config_prefers_static_then_uses_airport_when_static_unavailable(candidate_pool):
    from integrations.google_fx.utils import proxy_pool as pool_module

    rotator = candidate_pool([
        ("airport-other", {"source": "airport", "fallback_tier": 1}),
        ("airport-us", {"source": "airport", "fallback_tier": 0}),
        ("static-1", {}), ("static-2", {}),
    ], cursor=7)
    assert {rotator.get_proxy_config()["proxy_host"] for _ in range(4)} == {"static-1", "static-2"}

    state = pool_module._read_state()
    state["proxies"]["static-1"]["disabled"] = True
    state["proxies"]["static-2"]["last_check_status"] = "failed"
    pool_module._write_state(state)
    assert rotator.get_proxy_config()["proxy_host"] == "airport-us"

    state = pool_module._read_state()
    state["proxies"]["airport-us"]["last_check_status"] = "failed"
    pool_module._write_state(state)
    assert rotator.get_proxy_config()["proxy_host"] == "airport-other"
