import threading
import time

import pytest

from fx_control import FxControlPlane, FxQueueCancelled, current_fx_task_id


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


def test_fifo_queue_and_task_context(control):
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
    second.start()
    deadline = time.time() + 2
    while control.snapshot()['waiting_count'] < 1 and time.time() < deadline:
        time.sleep(0.01)

    assert control.snapshot()['active']['task_id'] == 'a'
    assert control.snapshot()['waiting'][0]['task_id'] == 'b'
    release.set()
    first.join(2)
    second.join(2)
    assert entered == [('a', 'a'), ('b', 'b')]


def test_pause_rejects_new_work_and_holds_existing_queue(control):
    control.set_mode('pause')
    allowed, message = control.admission()
    assert allowed is False and 'paused' in message

    cancelled = threading.Event()
    errors = []

    def worker():
        try:
            with control.slot('queued', 'frames', cancel_check=cancelled.is_set):
                pytest.fail('paused queue must not dispatch')
        except FxQueueCancelled:
            errors.append('cancelled')

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    assert control.snapshot()['waiting_count'] == 1
    cancelled.set()
    thread.join(2)
    assert errors == ['cancelled']


def test_priority_reorders_waiting_tasks(control):
    control.set_mode('pause')
    stop = threading.Event()
    threads = []
    for task_id in ('low', 'high'):
        thread = threading.Thread(target=lambda tid=task_id: _wait(control, tid, stop))
        thread.start()
        threads.append(thread)
    deadline = time.time() + 2
    while control.snapshot()['waiting_count'] < 2 and time.time() < deadline:
        time.sleep(0.01)

    control.reprioritize('high', 10)
    assert [row['task_id'] for row in control.snapshot()['waiting']] == ['high', 'low']
    stop.set()
    for thread in threads:
        thread.join(2)


def _wait(control, task_id, stop):
    try:
        with control.slot(task_id, 'frames', cancel_check=stop.is_set):
            pass
    except FxQueueCancelled:
        pass
