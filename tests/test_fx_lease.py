"""并发方案 P1：租约内核（N=1 下零行为变化）的单测。

P1 的规则（R3/R4/R7/R9）在只有一个租约时不会触发，所以这里用"他人租约"的替身 provider
把它们真正跑一遍；同时钉住"没装登记簿 = 旧行为"这条回退路径。
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

import server
from fx_control import FxControlPlane, LEASE_STALL_SECONDS
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import account_credentials as creds
from integrations.google_fx.utils import browser, lease_registry
from integrations.google_fx.utils import logger as fx_logger


@pytest.fixture(autouse=True)
def _uninstall_registry():
    yield
    lease_registry.install(None)


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


class _Provider:
    def __init__(self, leased):
        self.leased = set(leased)
        self.touched = 0

    def leased_user_ids(self, exclude_current=True):
        return set(self.leased)

    def touch(self):
        self.touched += 1


# ── 登记簿 ───────────────────────────────────────────

def test_registry_defaults_to_no_leases_and_swallows_errors():
    assert lease_registry.leased_by_others() == set()
    lease_registry.touch()  # 未安装：空操作

    class Broken:
        def leased_user_ids(self, exclude_current=True):
            raise RuntimeError('boom')

        def touch(self):
            raise RuntimeError('boom')

    lease_registry.install(Broken())
    assert lease_registry.leased_by_others() == set()
    lease_registry.touch()


def test_cleanup_registry_preserves_legacy_providers_and_protects_unknown_results():
    assert lease_registry.protected_from_cleanup('neighbor') is False
    lease_registry.install(_Provider([]))
    assert lease_registry.protected_from_cleanup('neighbor') is False

    class IntegratedLegacy(_Provider):
        account_admission_factory = object()

    lease_registry.install(IntegratedLegacy([]))
    assert lease_registry.protected_from_cleanup('neighbor') is True

    class Unknown(_Provider):
        def protected_from_cleanup(self, profile):
            return None

    lease_registry.install(Unknown([]))
    assert lease_registry.protected_from_cleanup('neighbor') is True

    class Broken(_Provider):
        def protected_from_cleanup(self, profile):
            raise RuntimeError('binding unavailable')

    lease_registry.install(Broken([]))
    assert lease_registry.protected_from_cleanup('neighbor') is True


def test_log_line_is_a_heartbeat():
    provider = _Provider([])
    lease_registry.install(provider)
    fx_logger.log('心跳测试', 'System')
    assert provider.touched == 1


def test_control_plane_provider_excludes_own_lease(control):
    holding = threading.Event()
    release = threading.Event()

    def other():
        with control.slot('other', 'videos', account_pin='acct-other'):
            holding.set()
            release.wait(2)

    thread = threading.Thread(target=other)
    thread.start()
    assert holding.wait(2)

    # 从另一个上下文（没有自己的租约）看：other 的账号被租着
    assert control.leased_user_ids() == {'acct-other'}
    release.set()
    thread.join(2)
    assert control.leased_user_ids() == set()

    with control.slot('me', 'videos', account_pin='acct-me'):
        assert control.leased_user_ids(exclude_current=True) == set()
        assert control.leased_user_ids(exclude_current=False) == {'acct-me'}


# ── 心跳与卡死标记 ───────────────────────────────────

def test_touch_refreshes_heartbeat_and_stalled_is_flag_only(control):
    with control.slot('t', 'videos', project_key='p', stage='videos'):
        start = control.board()['leases'][0]
        assert start['state'] == 'running'
        assert (start['project_key'], start['stage']) == ('p', 'videos')

        late = time.time() + LEASE_STALL_SECONDS + 5
        stalled = control.board(now=late)['leases'][0]
        assert stalled['state'] == 'stalled'
        assert stalled['heartbeat_age_seconds'] > LEASE_STALL_SECONDS

        control.touch()
        fresh = control.board(now=time.time() + 1)['leases'][0]
        assert fresh['state'] == 'running'
        # 只是标记：槽位仍被占用，没有任何自动释放
        assert control.snapshot()['active']['task_id'] == 't'

    rows = control.recent_audit(20, action_prefix='lease.stalled')
    assert len(rows) == 1 and rows[0]['task_id'] == 't'


def test_stalled_reported_once_per_lease(control):
    with control.slot('t', 'videos'):
        late = time.time() + LEASE_STALL_SECONDS + 5
        control.board(now=late)
        control.board(now=late + 1)
    assert len(control.recent_audit(20, action_prefix='lease.stalled')) == 1


# ── 持久化与崩溃恢复 ─────────────────────────────────

def test_construction_has_no_write_side_effect(tmp_path):
    state = tmp_path / 'state.json'
    FxControlPlane(state, tmp_path / 'audit.jsonl')
    assert not state.exists(), '导入/构造控制面不得写盘（测试里会 import server）'


def test_lease_is_persisted_while_held_and_cleared_on_release(tmp_path):
    state = tmp_path / 'state.json'
    control = FxControlPlane(state, tmp_path / 'audit.jsonl')
    with control.slot('t', 'videos', project_key='p', stage='videos'):
        data = json.loads(state.read_text(encoding='utf-8'))
        assert data['pid'] == os.getpid()
        assert data['lease']['task_id'] == 't' and data['lease']['project_key'] == 'p'
    assert json.loads(state.read_text(encoding='utf-8'))['lease'] is None


def test_interrupted_lease_is_recovered_by_next_process(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({
        'schema': 1, 'pid': os.getpid() + 12345, 'updated_at': '2026-10-02T10:00:00+08:00',
        'lease': {'lease_id': 'L_9', 'task_id': 'videos_dead', 'kind': 'videos', 'user_id': 'acct-1',
                  'project_key': 'p', 'stage': 'videos', 'started_at': '2026-10-02T09:58:00+08:00'},
    }), encoding='utf-8')

    control = FxControlPlane(state, tmp_path / 'audit.jsonl')

    assert control.recovered['task_id'] == 'videos_dead'
    assert control.board()['recovered']['user_id'] == 'acct-1'
    assert control.recent_audit(5, action_prefix='lease.recovered')[0]['task_id'] == 'videos_dead'
    # 构造期不写盘；宿主启动时显式确认后，才把文件改写成当前进程的空租约。
    assert json.loads(state.read_text(encoding='utf-8'))['lease']['task_id'] == 'videos_dead'
    control.acknowledge_recovery()
    assert json.loads(state.read_text(encoding='utf-8'))['lease'] is None
    assert FxControlPlane(state, tmp_path / 'audit.jsonl').recovered is None


def test_same_process_state_and_foreign_schema_are_not_recovered(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'schema': 1, 'pid': os.getpid(), 'lease': {'task_id': 'x'}}), encoding='utf-8')
    assert FxControlPlane(state, tmp_path / 'a.jsonl').recovered is None
    state.write_text(json.dumps({'mode': 'accepting', 'lease': {'task_id': 'x'}}), encoding='utf-8')
    assert FxControlPlane(state, tmp_path / 'a.jsonl').recovered is None
    state.write_text('not json', encoding='utf-8')
    assert FxControlPlane(state, tmp_path / 'a.jsonl').recovered is None


def test_force_release_is_recorded_in_history_and_persisted(tmp_path):
    state = tmp_path / 'state.json'
    control = FxControlPlane(state, tmp_path / 'audit.jsonl')
    release = threading.Event()

    def hold():
        with control.slot('stuck', 'videos'):
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    deadline = time.time() + 2
    while control.snapshot()['active'] is None and time.time() < deadline:
        time.sleep(0.01)

    assert control.force_release_active('stuck') == 1
    assert json.loads(state.read_text(encoding='utf-8'))['lease'] is None
    assert [row['outcome'] for row in control.board()['history']] == ['force_released']
    release.set()
    thread.join(2)
    # 线程收尾不会再追加一条重复历史
    assert len(control.board()['history']) == 1


# ── R4：只关无主浏览器 ──────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_ensure_profile_exclusive_never_stops_a_leased_neighbor(monkeypatch):
    stopped = []

    def fake_get(url, params=None, timeout=None):
        if 'browser/stop' in url:
            stopped.append((params or {}).get('user_id'))
            return _Resp({'code': 0})
        return _Resp({'code': 0, 'data': {'status': 'Inactive'}})

    monkeypatch.setattr(browser.requests, 'get', fake_get)
    monkeypatch.setattr(browser, 'list_running_ads_browsers',
                        lambda port=None, strict=False: [{'user_id': 'me'}, {'user_id': 'leased'}, {'user_id': 'stray'}])
    lease_registry.install(_Provider(['leased']))

    closed = browser.ensure_profile_exclusive('me', port=1)

    assert stopped == ['stray']
    assert closed == ['stray']


def test_without_registry_every_other_profile_is_stopped_as_before(monkeypatch):
    stopped = []

    def fake_get(url, params=None, timeout=None):
        if 'browser/stop' in url:
            stopped.append((params or {}).get('user_id'))
        return _Resp({'code': 0, 'data': {'status': 'Inactive'}})

    monkeypatch.setattr(browser.requests, 'get', fake_get)
    monkeypatch.setattr(browser, 'list_running_ads_browsers',
                        lambda port=None, strict=False: [{'user_id': 'me'}, {'user_id': 'b'}, {'user_id': 'c'}])

    browser.ensure_profile_exclusive('me', port=1)

    assert sorted(stopped) == ['b', 'c']
    assert browser.ensure_single_ads_browser is browser.ensure_profile_exclusive  # 旧名仍可用


@pytest.mark.parametrize('binding_state', ['bound', 'busy', 'unknown', 'invalid_guard'])
def test_neighbor_cleanup_keeps_flow_bound_or_unknown_profiles_and_prunes_unbound(monkeypatch, binding_state):
    stopped, probed, released = [], [], []

    class ProbeGuard:
        def close(self):
            released.append('managed')

    def factory(profile):
        probed.append(profile)
        if profile == 'unbound':
            return None
        if binding_state in {'busy', 'unknown'}:
            raise RuntimeError(binding_state)
        if binding_state == 'invalid_guard':
            return object()
        return ProbeGuard()

    def fake_get(url, params=None, timeout=None):
        profile = (params or {}).get('user_id')
        assert profile == 'unbound', 'managed neighbor must never receive stop/active cleanup requests'
        if 'browser/stop' in url:
            stopped.append(profile)
        return _Resp({'code': 0, 'data': {'status': 'Inactive'}})

    monkeypatch.setattr(browser.requests, 'get', fake_get)
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [
        {'user_id': 'me'}, {'user_id': 'managed'}, {'user_id': 'unbound'}])
    lease_registry.install(FxControlPlane(account_admission_factory=factory))

    assert browser.ensure_profile_exclusive('me', port=1) == ['unbound']
    assert stopped == ['unbound']
    assert probed == ['managed', 'unbound']
    assert released == (['managed'] if binding_state == 'bound' else [])


def test_cleanup_with_control_provider_without_flow_integration_keeps_previous_behavior(monkeypatch):
    stopped = []

    def fake_get(url, params=None, timeout=None):
        if 'browser/stop' in url:
            stopped.append((params or {}).get('user_id'))
        return _Resp({'code': 0, 'data': {'status': 'Inactive'}})

    monkeypatch.setattr(browser.requests, 'get', fake_get)
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [
        {'user_id': 'me'}, {'user_id': 'neighbor'}])
    lease_registry.install(FxControlPlane())

    assert browser.ensure_profile_exclusive('me', port=1) == ['neighbor']
    assert stopped == ['neighbor']


# ── R3/R7：选号绕开他人租约 ────────────────────────────

@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'account_pool.json')
    monkeypatch.setattr(creds, '_STATE_FILE', tmp_path / 'account_credentials.json')
    pool = ap.AccountPool()
    pool.add_account('best', name='best', serial_number='1')
    pool.add_account('other', name='other', serial_number='2')
    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state['best'].update(credit=900, last_checked_at=now_iso)
        state['other'].update(credit=100, last_checked_at=now_iso)
        ap._write_state(state)
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [])
    return pool


def test_pick_account_skips_accounts_leased_by_others(pool):
    assert pool.pick_account(min_credit=10)['user_id'] == 'best'
    lease_registry.install(_Provider(['best']))
    assert pool.pick_account(min_credit=10)['user_id'] == 'other'
    lease_registry.install(_Provider(['best', 'other']))
    assert pool.pick_account(min_credit=10) is None


def test_pick_open_account_does_not_grab_or_close_a_leased_window(pool, monkeypatch):
    stopped = []
    monkeypatch.setattr(browser, 'list_running_ads_browsers',
                        lambda port=None, strict=False: [{'user_id': 'best'}])
    monkeypatch.setattr(browser, 'stop_ads_browser', lambda user_id=None, port=None: stopped.append(user_id) or True)
    assert pool.pick_open_account(min_credit=10)['user_id'] == 'best'  # 无人租用：照旧复用

    lease_registry.install(_Provider(['best']))
    assert pool.pick_open_account(min_credit=10) is None
    assert stopped == []

    # 即便额度已耗尽，也不能替别的任务把它关掉
    with ap._LOCK:
        state = ap._read_state()
        state['best']['credit'] = 0
        ap._write_state(state)
    assert pool.pick_open_account(min_credit=10) is None
    assert stopped == []


# ── 宿主侧：对账、无主浏览器、R2 观察 ─────────────────────

def _set_open(ids, first_seen_ago=600.0):
    now = time.time()
    with server._FX_OPEN_LOCK:
        server._FX_OPEN_STATE.update(at=now, ids=list(ids), error=None,
                                     first_seen={uid: now - first_seen_ago for uid in ids})


@pytest.fixture
def host(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})

    class Pool:
        def list_accounts(self, heal=True):
            return [{'user_id': uid, 'serial_number': str(i)} for i, uid in
                    enumerate(['leased', 'warm', 'orphan', 'fresh'], start=1)]

    monkeypatch.setattr(server, '_get_account_pool', lambda: Pool())
    yield control
    with server._FX_OPEN_LOCK:
        server._FX_OPEN_STATE.update(at=0.0, ids=[], first_seen={}, error=None)


def test_open_browsers_are_classified_leased_warm_orphan_and_grace():
    now = time.time()
    board = {
        'leases': [{'user_id': 'leased'}],
        'history': [{'user_id': 'warm', 'ended_ts': now - 60}],
        'accounts': [{'user_id': 'orphan', 'label': '#3'}],
    }
    with server._FX_OPEN_LOCK:
        server._FX_OPEN_STATE.update(
            at=now, ids=['leased', 'warm', 'orphan', 'fresh'], error=None,
            first_seen={'leased': now - 900, 'warm': now - 900, 'orphan': now - 900, 'fresh': now - 10})
    try:
        rows = {row['user_id']: row for row in server._fx_open_browser_rows(board, now=now)}
    finally:
        with server._FX_OPEN_LOCK:
            server._FX_OPEN_STATE.update(at=0.0, ids=[], first_seen={}, error=None)

    assert rows['leased']['leased'] and not rows['leased']['orphan']
    assert rows['warm']['warm'] and not rows['warm']['orphan']
    assert rows['orphan']['orphan'] and rows['orphan']['label'] == '#3'
    assert not rows['fresh']['orphan'], '刚被观察到的浏览器要先过宽限期，不能立刻判无主'


def test_warm_window_expires_after_thirty_minutes():
    now = time.time()
    board = {'leases': [], 'history': [{'user_id': 'a', 'ended_ts': now - 31 * 60}], 'accounts': []}
    with server._FX_OPEN_LOCK:
        server._FX_OPEN_STATE.update(at=now, ids=['a'], error=None, first_seen={'a': now - 900})
    try:
        assert server._fx_open_browser_rows(board, now=now)[0]['orphan'] is True
    finally:
        with server._FX_OPEN_LOCK:
            server._FX_OPEN_STATE.update(at=0.0, ids=[], first_seen={}, error=None)


def test_orphan_close_endpoint_only_closes_true_orphans(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server, '_fx_refresh_open_browsers', lambda now=None: None)
    monkeypatch.setattr(server, '_get_account_pool', lambda: type(
        'Pool', (), {'list_accounts': lambda self, heal=True: []})())
    closed = []
    monkeypatch.setattr(browser, 'close_ads_browser',
                        lambda user_id=None, port=None: closed.append(user_id) or (True, 'ok'))

    with control.slot('earlier', 'frames', account_pin='warm'):
        pass  # 刚用过 → 保温
    holding, release = threading.Event(), threading.Event()

    def hold():
        with control.slot('videos_1', 'videos', account_pin='leased'):
            holding.set()
            release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(2)
    now = time.time()
    with server._FX_OPEN_LOCK:
        server._FX_OPEN_STATE.update(at=now, ids=['leased', 'warm', 'orphan'], error=None,
                                     first_seen={uid: now - 900 for uid in ('leased', 'warm', 'orphan')})

    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/orphans/close'
    handler._gate = lambda: True
    handler._read_json_body = lambda: {}
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    try:
        handler.do_POST()
    finally:
        release.set()
        thread.join(2)
        with server._FX_OPEN_LOCK:
            server._FX_OPEN_STATE.update(at=0.0, ids=[], first_seen={}, error=None)

    status, payload = responses[0]
    assert status == 200 and payload['closed'] == 1
    assert closed == ['orphan']
    skipped = {row['user_id']: row.get('skipped') for row in payload['results'] if not row['closed']}
    assert set(skipped) == {'leased', 'warm'}
    assert control.recent_audit(5, action_prefix='orphan.close')[0]['details']['user_id'] == 'orphan'


def test_project_conflict_is_observed_not_enforced():
    board = {
        'leases': [{'task_id': 'frames_1', 'project_key': 'p', 'stage': 'frames'}],
        'waiting': [
            {'task_id': 'videos_1', 'project_key': 'p', 'stage': 'videos', 'blocked_by': []},
            {'task_id': 'videos_2', 'project_key': 'other', 'stage': 'videos', 'blocked_by': []},
        ],
    }
    server._FX_R2_REPORTED.clear()
    server._annotate_project_conflicts(board)

    blocked = board['waiting'][0]['blocked_by']
    assert blocked == [{'type': 'project', 'holder_task': 'frames_1', 'rule': 'R2', 'enforced': False}]
    assert board['waiting'][1]['blocked_by'] == []
    # 同阶段（同一项目的第二个 frames）不属于 R2，不应误报
    again = {'leases': [{'task_id': 'f1', 'project_key': 'p', 'stage': 'frames'}],
             'waiting': [{'task_id': 'f2', 'project_key': 'p', 'stage': 'frames', 'blocked_by': []}]}
    server._annotate_project_conflicts(again)
    assert again['waiting'][0]['blocked_by'] == []


def test_server_slot_passes_project_meta_into_the_lease(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {'videos_1': {
        'dimensions': {'type': 'videos', 'theme': '校车', 'project_key': 'run_1__校车'},
        'status': 'running', 'events': []}})
    with server._fx_browser_slot('videos_1', 'videos'):
        lease = control.board()['leases'][0]
        persisted_view = control.snapshot()['active']
    assert (lease['project_key'], lease['stage']) == ('run_1__校车', 'videos')
    assert persisted_view['task_id'] == 'videos_1'
    # 对旧调用方可见的快照字段不变：不得出现 active_list（见 P0 记录的 409 陷阱）
    assert 'active_list' not in control.snapshot()


def test_bootstrap_installs_registry_and_acknowledges_recovery(monkeypatch, tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'schema': 1, 'pid': os.getpid() + 999, 'lease': {'task_id': 'dead'}}), encoding='utf-8')
    control = FxControlPlane(state, tmp_path / 'audit.jsonl')
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'apply_google_fx_runtime_overrides', lambda *_: None)
    monkeypatch.setattr(server, 'apply_direct_env', lambda *_: None)
    monkeypatch.setattr(server.FX_CONFIG, 'migrate_deprecated_values', lambda: None)
    monkeypatch.setattr(server, '_FX_WATCHDOG_STARTED', type('E', (), {'is_set': lambda s: True, 'set': lambda s: None})())
    monkeypatch.setattr(server, '_FX_SELECTOR_DRIFT_STARTED', type('E', (), {'is_set': lambda s: True, 'set': lambda s: None})())

    from integrations.google_fx.utils import account_binding, browser_gate
    try:
        server.bootstrap_fx_runtime()
        assert lease_registry.is_installed()
        assert json.loads(state.read_text(encoding='utf-8'))['lease'] is None
    finally:
        # bootstrap 装的是进程级钩子：测试结束必须卸掉，免得影响后面的用例。
        browser_gate.install(None)
        account_binding.install_pin_resolver(None)
        account_binding.install_account_observer(None)
