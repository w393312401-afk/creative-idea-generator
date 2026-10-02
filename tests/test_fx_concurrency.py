"""并发方案 P2：多租约调度 + 准入条件（容量 / 账号 / 出口 / 项目）+ 原子占号。

N=1（默认）必须与旧串行行为一致；N>1 时每个任务独占一个环境，且四类冲突都能被挡住。
"""
import threading
import time
from datetime import datetime, timezone

import pytest

import fx_console
import server
from fx_control import FxControlPlane, FxQueueCancelled
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import account_credentials as creds
from integrations.google_fx.utils import browser, browser_gate, egress, lease_registry


@pytest.fixture(autouse=True)
def _clean_hooks():
    yield
    lease_registry.install(None)
    browser_gate.install(None)


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


@pytest.fixture
def concurrent(monkeypatch):
    def set_n(n):
        monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', str(n))
    return set_n


class Task:
    """在线程里持有一份租约，直到 release()。"""

    def __init__(self, control, task_id, **kwargs):
        self.control, self.task_id, self.kwargs = control, task_id, kwargs
        self.entered, self._release, self.error = threading.Event(), threading.Event(), None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with self.control.slot(self.task_id, **self.kwargs):
                self.entered.set()
                self._release.wait(5)
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def release(self):
        self._release.set()
        self.thread.join(3)


def waiting(control, task_id):
    return any(row['task_id'] == task_id for row in control.snapshot()['waiting'])


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def blockers(control, task_id):
    row = next(r for r in control.board()['waiting'] if r['task_id'] == task_id)
    return {item['type'] for item in row['blocked_by']}


# ── 容量 ──────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [('2', 2), ('99', 4), ('0', 1), ('-3', 1), ('abc', 1), ('', 1)])
def test_concurrency_limit_is_clamped(monkeypatch, control, raw, expected):
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', raw)
    assert control.limits()['max_concurrent'] == expected
    assert control.max_concurrent() == expected


def test_default_is_serial_and_unset_env_means_one(monkeypatch, control):
    monkeypatch.delenv('SPARK_FX_MAX_CONCURRENT', raising=False)
    a = Task(control, 'a', kind='videos')
    assert a.entered.wait(2)
    b = Task(control, 'b', kind='videos')
    assert wait_until(lambda: waiting(control, 'b'))
    assert not b.entered.is_set()
    assert blockers(control, 'b') == {'capacity'}
    a.release()
    assert b.entered.wait(2)
    b.release()


def test_two_tasks_run_concurrently_and_third_waits(concurrent, control):
    concurrent(2)
    a, b = Task(control, 'a', kind='videos'), Task(control, 'b', kind='videos')
    assert a.entered.wait(2) and b.entered.wait(2), 'N=2 时两个任务应同时进入'
    assert control.snapshot()['active_count'] == 2
    assert control.board()['capacity'] == {'max': 2, 'used': 2, 'per_project_max': 1, 'egress_policy': 'hard'}

    c = Task(control, 'c', kind='videos')
    assert wait_until(lambda: waiting(control, 'c'))
    assert not c.entered.is_set() and blockers(control, 'c') == {'capacity'}
    a.release()
    assert c.entered.wait(2)
    b.release()
    c.release()


def test_waiters_are_released_by_priority_then_arrival(control):
    holder = Task(control, 'holder', kind='frames')
    assert holder.entered.wait(2)
    order = []

    def waiter(name, priority):
        with control.slot(name, 'frames', priority=priority):
            order.append(name)

    threads = []
    for name, priority in (('low_first', 0), ('low_second', 0), ('urgent', 50)):
        thread = threading.Thread(target=waiter, args=(name, priority), daemon=True)
        thread.start()
        threads.append(thread)
        assert wait_until(lambda n=name: waiting(control, n))
    assert [row['task_id'] for row in control.snapshot()['waiting']] == ['urgent', 'low_first', 'low_second']
    holder.release()
    for thread in threads:
        thread.join(3)
    assert order == ['urgent', 'low_first', 'low_second']


def test_blocked_waiter_does_not_hold_up_later_admissible_ones(concurrent, control):
    concurrent(3)
    owner = Task(control, 'owner', kind='videos', want_account='X')
    assert owner.entered.wait(2)
    blocked = Task(control, 'blocked', kind='videos', want_account='X')
    assert wait_until(lambda: waiting(control, 'blocked'))
    free = Task(control, 'free', kind='videos', want_account='Y')
    assert free.entered.wait(2), '前面的被账号挡住，后面的不该被拖住'
    assert not blocked.entered.is_set()
    owner.release()
    assert blocked.entered.wait(2)
    free.release()
    blocked.release()


# ── R1 账号 / R2 项目 ───────────────────────────────────

def test_same_account_is_exclusive_even_with_free_capacity(concurrent, control):
    concurrent(3)
    a = Task(control, 'a', kind='videos', want_account='X')
    assert a.entered.wait(2)
    assert control.leased_user_ids() == {'X'}, '想用的账号在授予租约的一刻就对别人可见'
    b = Task(control, 'b', kind='videos', want_account='X')
    assert wait_until(lambda: waiting(control, 'b'))
    assert blockers(control, 'b') == {'account'}
    a.release()
    assert b.entered.wait(2)
    b.release()


def test_one_project_never_runs_two_leases_but_other_projects_do(concurrent, control):
    concurrent(3)
    frames = Task(control, 'frames_1', kind='frames', project_key='p', stage='frames')
    assert frames.entered.wait(2)
    same_project = Task(control, 'videos_1', kind='videos', project_key='p', stage='videos')
    assert wait_until(lambda: waiting(control, 'videos_1'))
    assert blockers(control, 'videos_1') == {'project'}
    other = Task(control, 'videos_2', kind='videos', project_key='q', stage='videos')
    assert other.entered.wait(2), '不同项目可以并发'
    frames.release()
    assert same_project.entered.wait(2)
    same_project.release()
    other.release()


# ── R5 出口 ──────────────────────────────────────────

@pytest.fixture
def egress_map(control):
    mapping = {'a': 'http://p1:80@u', 'b': 'http://p1:80@u', 'c': 'http://p2:80@u', 'd': egress.DIRECT, 'e': egress.DIRECT}
    control.egress_resolver = lambda uid: mapping.get(uid)
    return mapping


def test_same_egress_waits_under_hard_policy(concurrent, control, egress_map):
    concurrent(3)
    a = Task(control, 'a_task', kind='videos', want_account='a')
    assert a.entered.wait(2)
    b = Task(control, 'b_task', kind='videos', want_account='b')
    assert wait_until(lambda: waiting(control, 'b_task'))
    assert blockers(control, 'b_task') == {'ip'}
    c = Task(control, 'c_task', kind='videos', want_account='c')
    assert c.entered.wait(2), '不同出口可以并发'
    a.release()
    assert b.entered.wait(2)
    b.release()
    c.release()


def test_warn_policy_lets_same_egress_through(monkeypatch, concurrent, control, egress_map):
    concurrent(3)
    monkeypatch.setenv('SPARK_FX_EGRESS_POLICY', 'warn')
    a = Task(control, 'a_task', kind='videos', want_account='a')
    assert a.entered.wait(2)
    b = Task(control, 'b_task', kind='videos', want_account='b')
    assert b.entered.wait(2)
    a.release()
    b.release()


def test_direct_connections_share_one_identity(concurrent, control, egress_map):
    concurrent(3)
    d = Task(control, 'd_task', kind='videos', want_account='d')
    assert d.entered.wait(2)
    e = Task(control, 'e_task', kind='videos', want_account='e')
    assert wait_until(lambda: waiting(control, 'e_task'))
    assert blockers(control, 'e_task') == {'ip'}
    d.release()
    assert e.entered.wait(2)
    e.release()


def test_egress_is_never_resolved_when_serial(monkeypatch, control):
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    calls = []
    control.egress_resolver = lambda uid: calls.append(uid)
    with control.slot('t', 'videos', want_account='a'):
        assert control.claim_account('b') is True
    assert calls == [], 'N=1 不得为出口判定去打 AdsPower'


def test_unknown_egress_is_allowed_and_audited(concurrent, control):
    concurrent(2)
    control.egress_resolver = lambda uid: None
    a = Task(control, 'a_task', kind='videos', want_account='a')
    assert a.entered.wait(2)
    done = []

    def claimer():
        with control.slot('b_task', 'videos'):
            done.append(control.claim_account('b'))

    thread = threading.Thread(target=claimer)
    thread.start()
    thread.join(3)
    assert done == [True]
    assert control.recent_audit(10, action_prefix='egress.unknown')
    a.release()


# ── 原子占号（R7）────────────────────────────────────

def test_claim_account_conflicts(concurrent, control, egress_map):
    concurrent(2)
    results = {}
    a_in, b_done, a_release = threading.Event(), threading.Event(), threading.Event()

    def a_task():
        with control.slot('a_task', 'videos'):
            results['a'] = control.claim_account('a')
            a_in.set()
            b_done.wait(3)
            a_release.set()

    def b_task():
        a_in.wait(3)
        with control.slot('b_task', 'videos'):
            results['same_account'] = control.claim_account('a')
            results['same_egress'] = control.claim_account('b')
            results['other_egress'] = control.claim_account('c')
            results['claim_after'] = control.current_claim()
            control.restore_claim(None)
            results['restored'] = control.current_claim()
        b_done.set()

    threads = [threading.Thread(target=a_task), threading.Thread(target=b_task)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results == {'a': True, 'same_account': False, 'same_egress': False, 'other_egress': True,
                       'claim_after': 'c', 'restored': None}
    denied = [row['details']['reason'] for row in control.recent_audit(10, action_prefix='lease.claim_denied')]
    assert sorted(denied) == ['account', 'ip']


def test_concurrent_claims_never_hand_out_the_same_account(concurrent, control):
    concurrent(4)
    candidates = ['x1', 'x2', 'x3', 'x4']
    chosen, lock, ready = [], threading.Lock(), threading.Barrier(4)

    def task(index):
        with control.slot(f't{index}', 'videos'):
            ready.wait(3)  # 四个任务同时进入，再同时抢号
            for user in candidates:
                if control.claim_account(user):
                    with lock:
                        chosen.append(user)
                    break
            time.sleep(0.05)

    threads = [threading.Thread(target=task, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(chosen) == candidates


def test_claim_outside_a_lease_or_when_serial_never_blocks(control):
    assert control.claim_account('whatever') is True  # 不在租约里
    with control.slot('only', 'videos'):
        assert control.claim_account('a') is True
        assert control.claim_account('b') is True  # 只有自己，换号不受限


# ── 强制释放 / 嵌套 / 取消 ───────────────────────────

def test_force_released_lease_keeps_its_capacity_until_the_thread_exits(control):
    stuck = Task(control, 'stuck', kind='videos')
    assert stuck.entered.wait(2)
    assert control.force_release_active('stuck') == 1
    assert control.snapshot()['active'] is None  # 旧语义：控制台看到"已释放"
    assert control.board()['leases'][0]['state'] == 'force_released'

    nxt = Task(control, 'next', kind='videos')
    assert wait_until(lambda: waiting(control, 'next'))
    time.sleep(0.2)
    assert not nxt.entered.is_set(), '底层线程还在跑，放行下一个会造成并发污染'
    assert blockers(control, 'next') == {'capacity'}

    stuck.release()
    assert nxt.entered.wait(2)
    nxt.release()
    assert [r['outcome'] for r in control.board()['history'] if r['task_id'] == 'stuck'] == ['force_released']


def test_nested_probe_reuses_the_outer_lease(concurrent, control):
    concurrent(2)
    with control.slot('outer', 'videos'):
        with control.slot('credit_probe_x', 'credit_probe', want_account='x'):
            assert control.snapshot()['active_count'] == 1
        assert control.snapshot()['active_count'] == 1


def test_cancelled_waiter_leaves_queue_clean_at_n2(concurrent, control):
    concurrent(1)
    holder = Task(control, 'holder', kind='videos')
    assert holder.entered.wait(2)
    cancelled, errors = threading.Event(), []

    def waiter():
        try:
            with control.slot('w', 'videos', cancel_check=cancelled.is_set):
                pass
        except FxQueueCancelled:
            errors.append(True)

    thread = threading.Thread(target=waiter)
    thread.start()
    assert wait_until(lambda: waiting(control, 'w'))
    cancelled.set()
    thread.join(3)
    assert errors == [True] and control.snapshot()['waiting'] == []
    holder.release()


def test_multiple_leases_are_persisted_and_recovered(tmp_path, concurrent):
    import json
    import os
    concurrent(2)
    state = tmp_path / 'state.json'
    control = FxControlPlane(state, tmp_path / 'audit.jsonl')
    a = Task(control, 'a', kind='videos', want_account='x', project_key='p')
    b = Task(control, 'b', kind='videos', want_account='y', project_key='q')
    assert a.entered.wait(2) and b.entered.wait(2)
    data = json.loads(state.read_text(encoding='utf-8'))
    assert {row['task_id'] for row in data['leases']} == {'a', 'b'}
    assert data['lease']['task_id'] == 'a'  # 旧字段保持兼容

    data['pid'] = os.getpid() + 4242
    state.write_text(json.dumps(data), encoding='utf-8')
    revived = FxControlPlane(state, tmp_path / 'audit2.jsonl')
    assert revived.recovered['task_id'] == 'a' and revived.recovered['others'] == 1
    assert len(revived.recent_audit(10, action_prefix='lease.recovered')) == 2
    assert 'all' not in revived.board()['recovered']
    a.release()
    b.release()


# ── 宿主：兜底串行锁让路 / 旁路探针带账号 ───────────────

def _host(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})


def test_serial_guard_lock_is_skipped_when_concurrent(monkeypatch, concurrent, control):
    _host(monkeypatch, control)
    concurrent(2)
    inside, release, errors = [], threading.Event(), []

    def run(name):
        try:
            with server._fx_browser_slot(name, 'videos'):
                inside.append(name)
                release.wait(3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in ('t1', 't2')]
    for thread in threads:
        thread.start()
    assert wait_until(lambda: len(inside) == 2), errors
    assert not server._FX_SERIAL_LOCK.locked(), 'N>1 时兜底锁不得被占，否则第二个任务会 10s 超时'
    release.set()
    for thread in threads:
        thread.join(3)
    assert errors == []


def test_serial_guard_lock_still_taken_when_serial(monkeypatch, control):
    _host(monkeypatch, control)
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    with server._fx_browser_slot('t1', 'videos'):
        assert server._FX_SERIAL_LOCK.locked()
    assert not server._FX_SERIAL_LOCK.locked()


def test_user_pinned_account_in_task_record_becomes_want_account(monkeypatch, concurrent, control):
    _host(monkeypatch, control)
    concurrent(2)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {
        'videos_1': {'dimensions': {'type': 'videos', 'userId': 'acct-1'}, 'status': 'running', 'events': []},
        'videos_2': {'dimensions': {'type': 'videos', 'googleFxUserId': 'acct-1'}, 'status': 'running', 'events': []}})
    entered, release = threading.Event(), threading.Event()

    def first():
        with server._fx_browser_slot('videos_1', 'videos'):
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=first, daemon=True)
    thread.start()
    assert entered.wait(2)
    done = threading.Event()

    def queued():
        with server._fx_browser_slot('videos_2', 'videos'):
            done.set()

    waiter = threading.Thread(target=queued, daemon=True)
    waiter.start()
    assert wait_until(lambda: waiting(control, 'videos_2'))
    assert 'account' in blockers(control, 'videos_2')
    release.set()
    thread.join(3)
    assert done.wait(3)
    waiter.join(3)


def test_probe_gate_passes_target_account_and_waits_if_it_is_leased(monkeypatch, concurrent, control):
    _host(monkeypatch, control)
    concurrent(2)
    holder = Task(control, 'videos_1', kind='videos', want_account='acct-1')
    assert holder.entered.wait(2)
    browser_gate.install(server._fx_browser_gate)
    entered = threading.Event()

    def probe():
        with browser_gate.browser_slot('credit_probe', task_id='credit_probe_acct-1', user_id='acct-1'):
            entered.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    assert wait_until(lambda: waiting(control, 'credit_probe_acct-1'))
    assert blockers(control, 'credit_probe_acct-1') == {'account'}, '不能去探正被别的任务用着的账号'
    holder.release()
    assert entered.wait(3)
    thread.join(3)


# ── 选号：两个任务同时选号不撞号 ─────────────────────────

def test_two_tasks_picking_at_once_get_different_accounts(monkeypatch, tmp_path, concurrent, control):
    concurrent(2)
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'account_pool.json')
    monkeypatch.setattr(creds, '_STATE_FILE', tmp_path / 'account_credentials.json')
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [])
    pool = ap.AccountPool()
    pool.add_account('best', name='best', serial_number='1')
    pool.add_account('next', name='next', serial_number='2')
    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state['best'].update(credit=900, last_checked_at=now_iso)
        state['next'].update(credit=500, last_checked_at=now_iso)
        ap._write_state(state)
    lease_registry.install(control)

    picked, barrier = {}, threading.Barrier(2)

    def task(name):
        with control.slot(name, 'videos'):
            barrier.wait(3)
            picked[name] = pool.pick_account(min_credit=10)['user_id']
            time.sleep(0.1)

    threads = [threading.Thread(target=task, args=(n,)) for n in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(picked.values()) == ['best', 'next']


def test_failed_pick_restores_the_previous_claim(monkeypatch, tmp_path, concurrent, control):
    concurrent(2)
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'account_pool.json')
    monkeypatch.setattr(creds, '_STATE_FILE', tmp_path / 'account_credentials.json')
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [])
    pool = ap.AccountPool()
    pool.add_account('only', name='only', serial_number='1')
    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state['only'].update(credit=1, last_checked_at=now_iso)  # 低于阈值：会被挑中后又被否决
        ap._write_state(state)
    lease_registry.install(control)

    with control.slot('t', 'videos', want_account='mine'):
        assert pool.pick_account(min_credit=500) is None
        assert control.current_claim() == 'mine'


# ── R11 窗口 / 出口模块 / 配置 ────────────────────────

def test_manual_reveal_is_scoped_to_the_current_task_when_concurrent(monkeypatch, control):
    import integrations.google_fx.utils.macos_window as mac
    monkeypatch.setattr(browser, 'IS_MAC', True)
    called = []
    monkeypatch.setattr(mac, 'reveal_hidden', lambda activate=True: called.append('all') or 3)
    monkeypatch.setattr(browser, 'reveal_current_task_window', lambda: called.append('own') or 1)

    lease_registry.install(None)
    assert browser.reveal_hidden_browser_windows() == 3 and called == ['all']

    class Two:
        def max_concurrent(self):
            return 2
    called.clear()
    lease_registry.install(Two())
    assert browser.reveal_hidden_browser_windows() == 1 and called == ['own']


def test_egress_identity_hides_passwords_and_groups_direct_connections():
    proxied = {'proxy_type': 'http', 'proxy_host': '1.2.3.4', 'proxy_port': '8080',
               'proxy_user': 'u', 'proxy_password': 'TOPSECRET'}
    assert 'TOPSECRET' not in egress.identity_from_config(proxied)
    assert egress.identity_from_config(proxied) == 'http://1.2.3.4:8080@u'
    for direct in (None, {}, {'proxy_soft': 'no_proxy'}, {'proxy_type': 'noproxy'}, {'proxy_type': 'http', 'proxy_host': ''}):
        assert egress.identity_from_config(direct) == egress.DIRECT


def test_egress_lookup_is_cached_and_invalidated(monkeypatch):
    calls = []

    class Resp:
        def json(self):
            return {'code': 0, 'data': {'list': [{'user_id': 'u1', 'user_proxy_config': {
                'proxy_type': 'socks5', 'proxy_host': 'h', 'proxy_port': '1', 'proxy_user': 'x'}}]}}

    monkeypatch.setattr(egress.requests, 'get', lambda *a, **k: calls.append(1) or Resp())
    egress.invalidate()
    assert egress.egress_id_for('u1', port=1) == 'socks5://h:1@x'
    assert egress.egress_id_for('u1', port=1) == 'socks5://h:1@x'
    assert len(calls) == 1
    egress.invalidate('u1')
    egress.egress_id_for('u1', port=1)
    assert len(calls) == 2
    monkeypatch.setattr(egress.requests, 'get', lambda *a, **k: (_ for _ in ()).throw(OSError('down')))
    egress.invalidate()
    assert egress.egress_id_for('u1', port=1) is None, 'AdsPower 不可达：按未知处理，不当成冲突'


def test_console_exposes_concurrency_settings_with_safe_defaults(monkeypatch):
    spec = fx_console.FX_CONFIG_SPEC
    assert spec['fxMaxConcurrent']['default'] == 1 and spec['fxMaxConcurrent']['max'] == 4
    assert spec['fxEgressPolicy']['default'] == 'hard'
    assert fx_console.validate_patch({'fxMaxConcurrent': 2}) == {'fxMaxConcurrent': 2}
    with pytest.raises(ValueError):
        fx_console.validate_patch({'fxMaxConcurrent': 9})
    # apply_direct_env 直接写 os.environ；先用 monkeypatch.setenv 占位，teardown 才会还原成"未设置"。
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    monkeypatch.setenv('SPARK_FX_EGRESS_POLICY', 'hard')
    fx_console.apply_direct_env({'fxMaxConcurrent': 2, 'fxEgressPolicy': 'warn'})
    assert FxControlPlane().max_concurrent() == 2
    assert FxControlPlane()._egress_policy() == 'warn'


# ── 绕过 pick_account 的选号路径也必须遵守"别人占着的号不用" ──────────

import server_common  # noqa: E402


@pytest.fixture
def two_account_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'account_pool.json')
    monkeypatch.setattr(creds, '_STATE_FILE', tmp_path / 'account_credentials.json')
    monkeypatch.setattr(browser, 'list_running_ads_browsers', lambda port=None, strict=False: [])
    pool = ap.AccountPool()
    pool.add_account('pref', name='pref', serial_number='1')
    pool.add_account('other', name='other', serial_number='2')
    now_iso = datetime.now(timezone.utc).isoformat()
    with ap._LOCK:
        state = ap._read_state()
        state['pref'].update(credit=900, last_checked_at=now_iso)
        state['other'].update(credit=500, last_checked_at=now_iso)
        ap._write_state(state)
    return pool


def _run_in_lease(control, name, fn, **slot_kwargs):
    out = {}

    def target():
        with control.slot(name, 'videos', **slot_kwargs):
            out['value'] = fn()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(5)
    return out.get('value')


def test_sequence_default_account_is_yielded_when_another_task_holds_it(concurrent, control, two_account_pool):
    concurrent(2)
    lease_registry.install(control)
    config = {'googleFxSequenceUserId': 'pref', 'videoAccountPoolMinCredit': 1}
    holder = Task(control, 'holder', kind='videos', want_account='pref')
    assert holder.entered.wait(2)
    try:
        picked = _run_in_lease(control, 'second', lambda: server_common._select_pool_account(config, two_account_pool))
        assert picked == 'other', '首选账号被别的任务占着：必须让出，改走自动选号'
        assert config['googleFxUserId'] == 'other'
    finally:
        holder.release()


def test_sequence_default_account_is_used_when_nobody_else_has_it(concurrent, control, two_account_pool):
    concurrent(2)
    lease_registry.install(control)
    config = {'googleFxSequenceUserId': 'pref', 'videoAccountPoolMinCredit': 1}
    picked = _run_in_lease(control, 'solo', lambda: server_common._select_pool_account(config, two_account_pool))
    assert picked == 'pref'


def test_leg_revalidation_and_rotation_skip_accounts_held_elsewhere(concurrent, control, two_account_pool):
    concurrent(2)
    lease_registry.install(control)
    config = {'videoAccountPoolMinCredit': 1}
    holder = Task(control, 'holder', kind='videos', want_account='pref')
    assert holder.entered.wait(2)
    try:
        leg = _run_in_lease(control, 'second', lambda: server_common.revalidate_leg_account(
            config, two_account_pool, 'pref', ['pref', 'other'], set()))
        assert leg == 'other'
        nxt = _run_in_lease(control, 'third', lambda: server_common._next_unused_account(
            config, two_account_pool, ['pref', 'other'], set()))
        assert nxt == 'other'
    finally:
        holder.release()


def test_serial_mode_is_unchanged_by_the_new_account_guards(monkeypatch, control, two_account_pool):
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    lease_registry.install(control)
    config = {'videoAccountPoolMinCredit': 1}
    assert _run_in_lease(control, 't', lambda: server_common.revalidate_leg_account(
        config, two_account_pool, 'pref', ['pref', 'other'], set())) == 'pref'
    assert _run_in_lease(control, 't2', lambda: server_common._next_unused_account(
        config, two_account_pool, ['pref', 'other'], {'pref'})) == 'other'


def test_state_file_stays_valid_json_under_concurrent_grants_and_releases(tmp_path, concurrent):
    """回归：N>1 时多个任务同时授予/释放租约，状态文件曾被共用的临时文件名写坏。"""
    import json
    concurrent(4)
    state = tmp_path / 'state.json'
    control = FxControlPlane(state, tmp_path / 'audit.jsonl')
    errors = []

    def churn(index):
        try:
            for round_no in range(15):
                with control.slot(f't{index}_{round_no}', 'videos', want_account=f'acct{index}'):
                    json.loads(state.read_text(encoding='utf-8'))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert errors == []
    assert json.loads(state.read_text(encoding='utf-8'))['leases'] == []
