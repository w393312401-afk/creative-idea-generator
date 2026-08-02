# -*- coding: utf-8 -*-
"""
🧪 S1/S2/C2/C3 回归：串行锁、探针互斥、队列调度与持久化
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
这一组测试对着几个具体的卡死/抢占场景，都是清单里确诊过的：

S1  裸锁路径持有 _FX_SERIAL_LOCK 时，已经拿到队列名额的任务不能无限期阻塞，
    也必须响应取消——否则整条队列停滞且控制台的取消对它无效，只能重启服务。
S2  积分探针必须经过同一条队列，不能抢正在生成的浏览器；而在**已持有名额**的
    上下文里嵌套取名额必须直接放行，否则选号时的 stale 探测会自锁。
C2  并发度、按 kind 的配额、排队超时、执行超时。
C3  队列落盘 + 重启后把上次的 active 回放成孤儿记录。
"""

import json
import threading
import time

import pytest

import server
from fx_control import (
    FxControlPlane, FxQueueCancelled, FxQueueTimeout, holds_fx_slot, current_fx_task_id,
)


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


@pytest.fixture(autouse=True)
def fresh_serial_lock(monkeypatch):
    """每个用例一把干净的兜底锁，避免用例之间互相污染。"""
    monkeypatch.setattr(server, '_FX_SERIAL_LOCK', threading.Lock())
    monkeypatch.setattr(server, '_FX_GUARD_LOCK_TIMEOUT_SECONDS', 1.0)


# ── S1 ───────────────────────────────────────────────────────────────────────

def test_guard_lock_times_out_instead_of_blocking_forever():
    """有人绕过队列占着兜底锁时，必须抛出可诊断的错误而不是永久挂住。"""
    server._FX_SERIAL_LOCK.acquire()
    try:
        started = time.time()
        with pytest.raises(RuntimeError) as excinfo:
            with server._fx_guard_lock('videos:t1', timeout=0.5):
                pytest.fail('不该拿到锁')
        assert time.time() - started < 5
        assert '绕过了队列' in str(excinfo.value)
    finally:
        server._FX_SERIAL_LOCK.release()


def test_guard_lock_is_cancellable_while_waiting():
    """等锁期间用户点了取消：必须立刻放弃，而不是等满超时。"""
    server._FX_SERIAL_LOCK.acquire()
    cancelled = threading.Event()
    errors = []

    def worker():
        try:
            with server._fx_guard_lock('videos:t1', cancel_check=cancelled.is_set,
                                       timeout=30):
                pass
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.1)
    cancelled.set()
    thread.join(3)
    try:
        assert not thread.is_alive(), '取消后必须马上退出，不能等满 30s'
        assert errors == ['GenerationCancelled']
    finally:
        server._FX_SERIAL_LOCK.release()


def test_guard_lock_releases_on_exception():
    with pytest.raises(ValueError):
        with server._fx_guard_lock('videos:t1'):
            raise ValueError('boom')
    assert server._FX_SERIAL_LOCK.acquire(timeout=0.5), '异常路径必须把锁还回去'
    server._FX_SERIAL_LOCK.release()


def test_no_task_id_path_still_goes_through_the_queue(monkeypatch, control):
    """S1 的根因：_fx_serial_lock_for 在无 task_id 时曾直接返回裸锁、绕过队列。"""
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    with server._fx_serial_lock_for({'imageBackend': 'google_fx'}, task_id=None):
        snapshot = control.snapshot()
        assert snapshot['active'] is not None, '无 task_id 的调用也必须占一个队列名额'
        assert snapshot['active']['task_id'].startswith('anon_')
    assert control.snapshot()['active'] is None


def test_non_fx_backend_does_not_take_a_slot(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    with server._fx_serial_lock_for({'imageBackend': 'api'}, task_id='frames_1'):
        assert control.snapshot()['active'] is None, '纯 API 渲染不该占用 FX 浏览器名额'


# ── S2 ───────────────────────────────────────────────────────────────────────

def test_nested_slot_is_reentrant(control):
    """选号时的 stale 积分探测跑在已持有名额的线程里，嵌套取名额必须直接放行。"""
    with control.slot('videos_1', 'videos'):
        assert holds_fx_slot() is True
        with control.slot('credit_probe_x', 'credit_probe'):
            # 没有自锁，也没有第二个 active
            assert control.snapshot()['active_count'] == 1
            assert current_fx_task_id() == 'videos_1'
    assert holds_fx_slot() is False


def test_nested_server_browser_slot_reuses_non_reentrant_guard(monkeypatch, control):
    """回归：生成任务内的 stale 积分探测不能二次获取同一把 threading.Lock。"""
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    with server._fx_browser_slot('videos_1', 'videos'):
        assert server._FX_SERIAL_LOCK.locked()
        started = time.time()
        with server._fx_browser_slot('credit_probe_x', 'credit_probe'):
            assert control.snapshot()['active']['task_id'] == 'videos_1'
            assert server._FX_SERIAL_LOCK.locked()
        assert time.time() - started < 0.5
    assert not server._FX_SERIAL_LOCK.locked()


def test_probe_waits_instead_of_stealing_the_browser(control):
    """探针不在任务上下文里时必须排队，不能和生成任务同时进浏览器。"""
    holding = threading.Event()
    release = threading.Event()
    probe_entered = threading.Event()

    def generation():
        with control.slot('videos_1', 'videos'):
            holding.set()
            release.wait(3)

    def probe():
        with control.slot('credit_probe_a', 'credit_probe', priority=40):
            probe_entered.set()

    gen = threading.Thread(target=generation)
    gen.start()
    assert holding.wait(2)
    pro = threading.Thread(target=probe)
    pro.start()
    time.sleep(0.3)
    assert not probe_entered.is_set(), '生成任务占着浏览器时探针必须等着'
    release.set()
    gen.join(3)
    assert probe_entered.wait(3)
    pro.join(3)


def test_browser_gate_routes_package_side_probes_through_the_queue(monkeypatch, control):
    from integrations.google_fx.utils import browser_gate
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    browser_gate.install(server._fx_browser_gate)
    try:
        with browser_gate.browser_slot('credit_probe', priority=40):
            assert control.snapshot()['active']['kind'] == 'credit_probe'
    finally:
        browser_gate.install(None)


def test_browser_gate_is_noop_when_not_installed():
    """独立跑脚本（没有 SPARK 宿主）时行为不变。"""
    from integrations.google_fx.utils import browser_gate
    browser_gate.install(None)
    assert browser_gate.is_installed() is False
    with browser_gate.browser_slot('credit_probe'):
        pass  # 不抛异常即可


# ── C2 ───────────────────────────────────────────────────────────────────────

def test_max_concurrent_is_clamped_to_real_global_browser_capacity(control):
    control.set_limits({'max_concurrent': 2})
    assert control.limits()['max_concurrent'] == 1
    entered = threading.Event()
    second = threading.Event()
    release = threading.Event()

    def first():
        with control.slot('a', 'videos'):
            entered.set()
            release.wait(3)

    def other():
        with control.slot('b', 'frames'):
            second.set()
            release.wait(3)

    threads = [threading.Thread(target=first), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(2)
        time.sleep(0.2)
        assert not second.is_set(), '全局浏览器锁存在时第二个任务不得被伪放行为 active'
        assert control.snapshot()['active_count'] == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(3)


def test_kind_limit_cannot_bypass_global_single_browser_capacity(control):
    control.set_limits({'max_concurrent': 1, 'kind_limits': {'videos': 1}})
    release = threading.Event()
    marks = {}

    def run(task_id, kind, event):
        with control.slot(task_id, kind):
            event.set()
            release.wait(3)

    v1_in, v2_in, f1_in = threading.Event(), threading.Event(), threading.Event()
    threads = [
        threading.Thread(target=run, args=('v1', 'videos', v1_in)),
        threading.Thread(target=run, args=('v2', 'videos', v2_in)),
        threading.Thread(target=run, args=('f1', 'frames', f1_in)),
    ]
    threads[0].start()
    assert v1_in.wait(2)
    threads[1].start()
    time.sleep(0.2)
    threads[2].start()
    try:
        assert not f1_in.wait(0.3), '全局单浏览器容量满时 frames 也必须等待'
        assert not v2_in.is_set(), '第二个 videos 必须被配额挡住'
    finally:
        release.set()
        for thread in threads:
            thread.join(3)


def test_queue_wait_timeout_gives_up(control):
    control.set_limits({'queue_wait_timeout_seconds': 1})
    release = threading.Event()
    holding = threading.Event()
    errors = []

    def holder():
        with control.slot('long', 'videos'):
            holding.set()
            release.wait(5)

    def waiter():
        try:
            with control.slot('impatient', 'videos'):
                pass
        except FxQueueTimeout as exc:
            errors.append(str(exc))

    first = threading.Thread(target=holder)
    first.start()
    assert holding.wait(2)
    second = threading.Thread(target=waiter)
    second.start()
    second.join(5)
    try:
        assert errors and '超过 1s' in errors[0]
        assert control.snapshot()['waiting_count'] == 0, '超时的任务要从队列里摘掉'
    finally:
        release.set()
        first.join(3)


def test_overdue_active_reports_execution_timeout(control):
    control.set_limits({'task_timeout_seconds': 1})
    release = threading.Event()
    holding = threading.Event()

    def holder():
        with control.slot('slow', 'videos'):
            holding.set()
            release.wait(4)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(2)
    try:
        assert control.overdue_active() == []
        time.sleep(1.2)
        overdue = control.overdue_active()
        assert [row['task_id'] for row in overdue] == ['slow']
        assert control.snapshot()['active']['overdue'] is True
    finally:
        release.set()
        thread.join(3)


def test_limits_are_validated(control):
    with pytest.raises(ValueError):
        control.set_limits({'max_concurrent': 'many'})
    with pytest.raises(ValueError):
        control.set_limits({'nonsense': 1})
    with pytest.raises(ValueError):
        control.set_limits({'kind_limits': {'videos': 0}})


# ── C3 / C4 ──────────────────────────────────────────────────────────────────

def test_queue_state_and_limits_survive_restart(tmp_path):
    state, audit = tmp_path / 'state.json', tmp_path / 'audit.jsonl'
    first = FxControlPlane(state, audit)
    first.set_limits({'max_concurrent': 3, 'task_timeout_seconds': 42})
    first.set_mode('pause')

    second = FxControlPlane(state, audit)
    assert second.limits()['max_concurrent'] == 1
    assert second.limits()['task_timeout_seconds'] == 42
    assert second.snapshot()['mode'] == 'paused'


def test_active_task_at_crash_is_replayed_as_orphan(tmp_path):
    """进程被强杀时 active 会留在状态文件里，重启后要能解释"上次为什么中断"。"""
    state, audit = tmp_path / 'state.json', tmp_path / 'audit.jsonl'
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        'accepting': True, 'processing_paused': False,
        'active': {'videos_9': {'task_id': 'videos_9', 'kind': 'videos',
                                'started_at': time.time(), 'priority': 0,
                                'sequence': 1, 'enqueued_at': time.time()}},
        'waiting': [], 'updated_at': '2026-07-26T20:00:00+08:00',
    }), encoding='utf-8')

    control = FxControlPlane(state, audit)
    orphaned = control.snapshot()['orphaned']
    assert [row['task_id'] for row in orphaned] == ['videos_9']
    assert orphaned[0]['was'] == 'active'
    assert control.snapshot()['active'] is None, '孤儿记录不能占用真实名额'
    assert control.clear_orphaned() == 1
    assert control.snapshot()['orphaned'] == []


def test_account_pin_applies_inside_the_slot(control):
    from integrations.google_fx.utils import account_binding
    stop = threading.Event()
    seen = {}

    def waiter():
        try:
            with control.slot('videos_pin', 'videos', cancel_check=stop.is_set):
                seen['pin'] = account_binding.pinned_account()
        except FxQueueCancelled:
            pass

    control.set_mode('pause')  # 先让它停在等待队列里，才能钉账号
    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.time() + 2
    while control.snapshot()['waiting_count'] < 1 and time.time() < deadline:
        time.sleep(0.01)

    control.pin_account('videos_pin', 'profile-42')
    assert control.snapshot()['waiting'][0]['account_pin'] == 'profile-42'

    account_binding.install_pin_resolver(lambda: __import__(
        'fx_control').current_account_pin())
    try:
        control.set_mode('resume')
        thread.join(3)
        assert seen.get('pin') == 'profile-42', '进临界区后账号绑定必须生效'
    finally:
        account_binding.install_pin_resolver(None)


def test_pin_account_rejects_tasks_that_already_started(control):
    with control.slot('running_task', 'videos'):
        with pytest.raises(KeyError):
            control.pin_account('running_task', 'profile-1')


# ── B6 ───────────────────────────────────────────────────────────────────────

def test_audit_tail_read_filters_by_action_and_task(control):
    for index in range(50):
        control.audit('queue.started', f't{index}', {'i': index})
    control.audit('control.pause', None, {})
    rows = control.recent_audit(5)
    assert len(rows) == 5
    assert rows[0]['action'] == 'control.pause', '最新的在前'

    filtered = control.recent_audit(10, action_prefix='queue.')
    assert filtered and all(r['action'].startswith('queue.') for r in filtered)

    by_task = control.recent_audit(10, task_id='t7')
    assert by_task and all(r['task_id'] == 't7' for r in by_task)


def test_audit_rotates_and_still_reads(tmp_path, monkeypatch):
    import fx_control
    monkeypatch.setattr(fx_control, 'AUDIT_MAX_BYTES', 2048)
    monkeypatch.setattr(fx_control, 'AUDIT_BACKUP_COUNT', 2)
    control = fx_control.FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')
    for index in range(400):
        control.audit('queue.started', f't{index}', {'payload': 'x' * 50})

    assert (tmp_path / 'audit.jsonl.1').exists(), '超过上限必须轮转，不能无限增长'
    assert (tmp_path / 'audit.jsonl').stat().st_size < 8192
    rows = control.recent_audit(3)
    assert rows and rows[0]['task_id'] == 't399'


def test_recent_audit_is_empty_when_file_missing(tmp_path):
    control = FxControlPlane(tmp_path / 'state.json', tmp_path / 'nope' / 'audit.jsonl')
    assert control.recent_audit(10) == []
