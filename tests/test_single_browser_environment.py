"""One AdsPower profile at a time; all API traffic is an in-memory fake."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from integrations.google_fx.utils import browser as B
from integrations.google_fx.utils import cancel_flag


class FakeAdsPower:
    def __init__(self):
        self.opened = set()
        self.events = []
        self.stopping = {}
        self.polls_before_close = 0
        self.stop_failure = False
        self.never_closes = False
        self.inventory_result = None
        self.now = 0.0
        self.max_opened = 0
        self.start_entered = None
        self.release_start = None
        self.lock = Lock()

    @staticmethod
    def response(data):
        return SimpleNamespace(status_code=200, text=str(data), json=lambda: data,
                               raise_for_status=lambda: None)

    def sleep(self, seconds):
        self.now += seconds

    def get(self, url, params=None, **kwargs):
        parsed = urlparse(url)
        assert parsed.hostname == "127.0.0.1" and parsed.port == 50325
        query = parse_qs(parsed.query)
        uid = str((params or {}).get("user_id") or query.get("user_id", [""])[0])
        endpoint = parsed.path.rsplit("/", 1)[-1]
        if endpoint == "start" and self.start_entered:
            self.start_entered.set()
            assert self.release_start.wait(3), "test failed to release fake start"
        with self.lock:
            if endpoint == "local-active":
                self.events.append(("inventory", tuple(sorted(self.opened))))
                if isinstance(self.inventory_result, Exception):
                    raise self.inventory_result
                if self.inventory_result is not None:
                    return self.response(self.inventory_result)
                return self.response({"code": 0, "data": {"list": [
                    {"user_id": user_id} for user_id in sorted(self.opened)]}})
            if endpoint == "stop":
                self.events.append(("stop", uid))
                if self.stop_failure:
                    return self.response({"code": 1, "msg": "stop refused"})
                self.stopping[uid] = self.polls_before_close
                return self.response({"code": 0})
            if endpoint == "active":
                if uid in self.stopping and not self.never_closes:
                    if self.stopping[uid] <= 0:
                        self.opened.discard(uid)
                        del self.stopping[uid]
                    else:
                        self.stopping[uid] -= 1
                status = "Active" if uid in self.opened else "Inactive"
                self.events.append(("active", uid, status))
                return self.response({"code": 0, "data": {
                    "status": status, "ws": {"puppeteer": f"ws://fake/{uid}"}}})
            if endpoint == "start":
                assert not self.opened - {uid}, "started while another profile was still open"
                self.events.append(("start", uid))
                self.opened.add(uid)
                self.max_opened = max(self.max_opened, len(self.opened))
                return self.response({"code": 0, "data": {"ws": {"puppeteer": f"ws://fake/{uid}"}}})
            pytest.fail(f"unexpected AdsPower endpoint in test: {endpoint}")


@pytest.fixture
def ads(monkeypatch, ads_inventory_reader):
    fake = FakeAdsPower()
    monkeypatch.setattr(B.requests, "get", fake.get)
    monkeypatch.setattr(requests.sessions.Session, "send", lambda *a, **kw: pytest.fail("real network forbidden"))
    monkeypatch.setattr(B, "_is_ws_port_open", lambda *a, **kw: True)
    monkeypatch.setattr(B, "_find_running_browser_ws", lambda *a, **kw: pytest.fail("real cache inspection forbidden"))
    monkeypatch.setattr(B, "_try_revive_adspower", lambda *a, **kw: pytest.fail("real app launch forbidden"))
    monkeypatch.setattr(B, "get_runtime_adspower_silent_mode", lambda: False)
    monkeypatch.setattr(B, "get_runtime_adspower_headless", lambda: False)
    monkeypatch.setattr(B, "get_runtime_adspower_macos_window_mode", lambda: "off")
    monkeypatch.setattr(B, "build_adspower_launch_args", lambda **kw: ("", []))
    monkeypatch.setattr(B, "time", SimpleNamespace(
        sleep=fake.sleep, monotonic=lambda: fake.now, time=lambda: fake.now))
    monkeypatch.setattr(B, "log", lambda *a, **kw: None)
    state = cancel_flag.init_context("single-browser-unit-test")
    fake.cancel_state = state
    try:
        yield fake
    finally:
        cancel_flag.clear_context()


def connect(uid):
    return B.get_ads_ws_url(user_id=uid, port=50325, auto_rotate_proxy=False)


def test_old_environment_must_confirm_inactive_before_starting_replacement(ads):
    ads.opened = {"old"}
    ads.polls_before_close = 2
    assert connect("new") == "ws://fake/new"
    inactive = ads.events.index(("active", "old", "Inactive"))
    assert ads.events.index(("stop", "old")) < inactive < ads.events.index(("start", "new"))
    assert ads.events.count(("active", "old", "Active")) == 2
    assert ads.opened == {"new"} and ads.max_opened == 1


def test_existing_target_is_reused_only_after_other_environment_closes(ads):
    ads.opened = {"old", "target"}
    assert connect("target") == "ws://fake/target"
    assert ("stop", "old") in ads.events
    assert ("active", "old", "Inactive") in ads.events
    assert ("stop", "target") not in ads.events
    assert not any(event[0] == "start" for event in ads.events)
    assert ads.opened == {"target"}


def test_all_previous_environments_close_before_any_new_start(ads):
    ads.opened = {"old-a", "old-b", "old-c"}
    assert connect("new") == "ws://fake/new"
    start_index = ads.events.index(("start", "new"))
    for uid in ("old-a", "old-b", "old-c"):
        assert ads.events.index(("stop", uid)) < start_index
        assert ads.events.index(("active", uid, "Inactive")) < start_index
    assert ads.opened == {"new"}


@pytest.mark.parametrize("failure", ["refused", "still_active"])
def test_failed_or_unconfirmed_close_prevents_replacement_start(ads, failure):
    ads.opened = {"old"}
    ads.stop_failure = failure == "refused"
    ads.never_closes = failure == "still_active"
    with pytest.raises(B.BrowserEnvironmentError):
        connect("new")
    assert not any(event[0] == "start" for event in ads.events)
    assert ads.opened == {"old"}


@pytest.mark.parametrize("result", [
    {"code": 1, "msg": "inventory unavailable"},
    {"code": 0, "data": {"list": "invalid inventory"}},
    requests.exceptions.ConnectionError("offline fake API"),
])
def test_inventory_failure_never_means_no_open_environments(ads, result):
    ads.inventory_result = result
    with pytest.raises(B.BrowserEnvironmentError):
        connect("new")
    assert not any(event[0] in ("stop", "start") for event in ads.events)


def test_cancelled_request_does_not_close_or_start_environments(ads):
    ads.opened = {"old"}
    ads.cancel_state.val = True
    with pytest.raises(RuntimeError, match="取消"):
        connect("new")
    assert not any(event[0] in ("stop", "start") for event in ads.events)


def test_cancel_while_waiting_for_old_environment_blocks_new_start(ads, monkeypatch):
    ads.opened = {"old"}
    ads.polls_before_close = 99

    def cancel_during_wait(seconds):
        ads.sleep(seconds)
        ads.cancel_state.val = True

    monkeypatch.setattr(B.time, "sleep", cancel_during_wait)
    with pytest.raises(RuntimeError, match="取消"):
        connect("new")
    assert ("stop", "old") in ads.events
    assert not any(event[0] == "start" for event in ads.events)


def test_repeated_same_target_reuses_single_existing_environment(ads):
    assert connect("same") == "ws://fake/same"
    assert connect("same") == "ws://fake/same"
    assert ads.events.count(("start", "same")) == 1
    assert not any(event[0] == "stop" for event in ads.events)
    assert ads.opened == {"same"}


@pytest.mark.parametrize("second_uid,expected_starts", [("first", 1), ("second", 2)])
def test_concurrent_startup_is_serialized_and_same_target_is_not_started_twice(ads, second_uid, expected_starts):
    ads.start_entered = Event()
    ads.release_start = Event()
    second_entered = Event()

    def second_connect():
        second_entered.set()
        return connect(second_uid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(connect, "first")
        assert ads.start_entered.wait(2)
        second = pool.submit(second_connect)
        assert second_entered.wait(2)
        # A competing caller must not pass the inventory boundary while the
        # first start is pending; it would otherwise see an empty stale list.
        assert not second.done()
        assert sum(event[0] == "inventory" for event in ads.events) == 1
        ads.release_start.set()
        assert first.result(timeout=3) == "ws://fake/first"
        assert second.result(timeout=3) == f"ws://fake/{second_uid}"
    assert sum(event[0] == "start" for event in ads.events) == expected_starts
    assert ads.opened == {second_uid} and ads.max_opened == 1


@pytest.mark.parametrize("failure", ["inventory", "closure"])
def test_environment_failure_escapes_fx_connection_without_retry_or_stopping_target(ads, monkeypatch, failure):
    from integrations.google_fx.services import google_fx_helpers as H

    ads.opened = {"old", "target"}
    if failure == "inventory":
        ads.inventory_result = {"code": 1}
    else:
        ads.stop_failure = True
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    monkeypatch.setattr(H, "get_ads_ws_url", lambda: connect("target"))
    monkeypatch.setattr(H, "_connect_over_cdp_with_retry", lambda *a, **kw: pytest.fail("CDP must not connect"))

    with pytest.raises(B.BrowserEnvironmentError):
        H._connect_fx_page(object())
    assert sum(event[0] == "inventory" for event in ads.events) == 1
    assert ("stop", "target") not in ads.events
    assert not any(event[0] == "start" for event in ads.events)
