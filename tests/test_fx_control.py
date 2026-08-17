import threading
import time

import pytest

from fx_control import (
    FxControlPlane,
    FxQueueCancelled,
    FxQueueTimeout,
    current_fx_task_id,
    holds_fx_slot,
)


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


def test_mutex_lock_and_task_context(control):
    entered = []
    release = threading.Event()

    def worker(task_id):
        with control.slot(task_id, 'videos'):
            entered.append((task_id, current_fx_task_id()))
            if task_id == 'a':
                release.wait(2)

    first = threading.Thread(target=worker, args=('a',))
    second = threading.Thread(target=worker, args=('b',))
    first.start()
    deadline = time.time() + 2
    while control.snapshot()['active'] is None and time.time() < deadline:
        time.sleep(0.01)

    assert control.snapshot()['active']['task_id'] == 'a'
    assert control.snapshot()['busy'] is True

    second.start()
    time.sleep(0.05)
    # While first is running, second is blocked on the mutex lock
    assert entered == [('a', 'a')]

    release.set()
    first.join(2)
    second.join(2)
    assert entered == [('a', 'a'), ('b', 'b')]
    assert control.snapshot()['active'] is None
    assert control.snapshot()['busy'] is False


def test_reentrancy_in_same_context(control):
    """同一上下文内嵌套调用 slot() 必须安全放行，不发生自锁。"""
    with control.slot('outer_task', 'frames'):
        assert holds_fx_slot() is True
        assert current_fx_task_id() == 'outer_task'
        # 嵌套获取
        with control.slot('inner_probe', 'credit_probe'):
            assert holds_fx_slot() is True
            assert current_fx_task_id() == 'outer_task'

    assert holds_fx_slot() is False


def test_cancel_waiting_task(control):
    first_holding = threading.Event()
    first_release = threading.Event()
    cancelled = threading.Event()
    errors = []

    def first_worker():
        with control.slot('first', 'frames'):
            first_holding.set()
            first_release.wait(2)

    def waiting_worker():
        try:
            with control.slot('second', 'frames', cancel_check=cancelled.is_set):
                pytest.fail('cancelled task should not acquire lock')
        except FxQueueCancelled:
            errors.append('cancelled')

    t1 = threading.Thread(target=first_worker)
    t2 = threading.Thread(target=waiting_worker)
    t1.start()
    first_holding.wait(2)

    t2.start()
    time.sleep(0.05)
    cancelled.set()
    t2.join(2)
    first_release.set()
    t1.join(2)

    assert errors == ['cancelled']


def test_wait_timeout(control):
    first_holding = threading.Event()
    first_release = threading.Event()
    errors = []

    def first_worker():
        with control.slot('first', 'frames'):
            first_holding.set()
            first_release.wait(2)

    def waiting_worker():
        try:
            with control.slot('second', 'frames', wait_timeout=0.1):
                pytest.fail('timed out task should not acquire lock')
        except FxQueueTimeout:
            errors.append('timeout')

    t1 = threading.Thread(target=first_worker)
    t2 = threading.Thread(target=waiting_worker)
    t1.start()
    first_holding.wait(2)

    t2.start()
    t2.join(2)
    first_release.set()
    t1.join(2)

    assert errors == ['timeout']


def test_force_release_active(control):
    first_holding = threading.Event()
    first_release = threading.Event()

    def first_worker():
        with control.slot('task_stuck', 'frames'):
            first_holding.set()
            first_release.wait(2)

    t = threading.Thread(target=first_worker)
    t.start()
    first_holding.wait(2)

    assert control.snapshot()['active']['task_id'] == 'task_stuck'
    count = control.force_release_active(task_id='task_stuck')
    assert count == 1
    assert control.snapshot()['active'] is None

    first_release.set()
    t.join(2)
