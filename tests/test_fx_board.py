"""并发作战板 P0：控制面只读观测 + /api/google-fx/board 数据契约。

P0 的硬约束是"零行为变化"：放行仍然是一把锁，snapshot() 不能多出 active_list
（server.py 的积分刷新/测试登录接口一旦读到非空 active_list 就会对生成任务回 409）。
"""
import threading
import time

import pytest

import server
from fx_control import FxControlPlane, FxQueueCancelled
from integrations.google_fx.utils import account_binding


@pytest.fixture
def control(tmp_path):
    return FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_waiting_is_visible_while_blocked_and_cleared_after(control):
    release = threading.Event()
    done = threading.Event()

    def holder():
        with control.slot('first', 'videos'):
            release.wait(2)

    def waiter():
        with control.slot('second', 'frames', priority=3):
            pass
        done.set()

    t1 = threading.Thread(target=holder)
    t1.start()
    assert _wait_for(lambda: control.snapshot()['active'] is not None)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert _wait_for(lambda: control.snapshot()['waiting_count'] == 1)

    snap = control.snapshot()
    assert snap['waiting'][0]['task_id'] == 'second'
    assert snap['waiting'][0]['priority'] == 3
    board = control.board()
    assert board['capacity'] == {'max': 1, 'used': 1, 'per_project_max': 1, 'egress_policy': 'hard'}
    assert board['leases'][0]['task_id'] == 'first'
    assert board['waiting'][0]['position'] == 1
    assert board['waiting'][0]['blocked_by'] == [{'type': 'capacity', 'holder_task': 'first'}]

    release.set()
    t1.join(2)
    t2.join(2)
    assert done.is_set()
    assert control.snapshot()['waiting'] == []


def test_snapshot_never_exposes_active_list(control):
    """active_list 非空会让 server.py 的探针接口改成对生成任务回 409：P0 不许引入。"""
    with control.slot('t', 'videos'):
        assert 'active_list' not in control.snapshot()
        assert control.snapshot()['active']['task_id'] == 't'


def test_cancelled_waiter_leaves_no_waiting_row(control):
    release = threading.Event()
    cancelled = threading.Event()
    errors = []

    def holder():
        with control.slot('first', 'frames'):
            release.wait(2)

    def waiter():
        try:
            with control.slot('second', 'frames', cancel_check=cancelled.is_set):
                pass
        except FxQueueCancelled:
            errors.append('cancelled')

    t1 = threading.Thread(target=holder)
    t1.start()
    assert _wait_for(lambda: control.snapshot()['active'] is not None)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert _wait_for(lambda: control.snapshot()['waiting_count'] == 1)
    cancelled.set()
    t2.join(2)
    release.set()
    t1.join(2)

    assert errors == ['cancelled']
    assert control.snapshot()['waiting'] == []


def test_history_records_segments_with_outcome_and_account(control):
    with control.slot('ok_task', 'videos'):
        control.note_account('acct-1')
    with pytest.raises(RuntimeError):
        with control.slot('bad_task', 'frames'):
            raise RuntimeError('boom')

    class GenerationCancelled(ConnectionError):
        pass

    with pytest.raises(GenerationCancelled):
        with control.slot('cancel_task', 'videos'):
            raise GenerationCancelled('stop')

    history = {row['task_id']: row for row in control.board()['history']}
    assert history['ok_task']['outcome'] == 'ok'
    assert history['ok_task']['user_id'] == 'acct-1'
    assert history['bad_task']['outcome'] == 'error'
    assert history['cancel_task']['outcome'] == 'cancelled'
    assert all(row['ended_ts'] >= row['started_ts'] for row in history.values())


def test_history_window_drops_old_segments(control):
    with control.slot('old', 'videos'):
        pass
    future = time.time() + 31 * 60
    assert control.board(now=future)['history'] == []
    assert [row['task_id'] for row in control.board()['history']] == ['old']


def test_note_account_only_updates_own_context(control):
    """嵌套探针探别的号不能把外层任务的账号改掉：observer 只在 resolve 没有显式账号时触发。"""
    account_binding.install_account_observer(control.note_account)
    try:
        with control.slot('outer', 'videos'):
            account_binding.resolve_account(explicit='probe-target', fallback='default-acct')
            assert control.board()['leases'][0]['user_id'] is None
            account_binding.resolve_account(fallback='default-acct')
            assert control.board()['leases'][0]['user_id'] == 'default-acct'
            with account_binding.bound_task_account('bound-acct'):
                assert control.board()['leases'][0]['user_id'] == 'bound-acct'
    finally:
        account_binding.install_account_observer(None)


def test_observer_exception_never_breaks_account_resolution():
    account_binding.install_account_observer(lambda _uid: 1 / 0)
    try:
        assert account_binding.resolve_account(fallback='x') == 'x'
        with account_binding.bound_task_account('y') as current:
            assert current == 'y'
    finally:
        account_binding.install_account_observer(None)


class _Pool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.heal_calls = []

    def list_accounts(self, heal=True):
        self.heal_calls.append(heal)
        return list(self.accounts)


def test_build_fx_board_enriches_projects_accounts_and_never_heals(monkeypatch, control):
    pool = _Pool([
        {'user_id': 'a', 'serial_number': '70', 'name': 'x@example.com', 'credit': 900},
        {'user_id': 'b', 'serial_number': '66', 'credit': 5, 'min_credit': 15},
        {'user_id': 'c', 'serial_number': '', 'name': 'off', 'credit': 100, 'disabled': True},
    ])
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, '_get_account_pool', lambda: pool)
    tasks = {
        'videos_1': {'dimensions': {'type': 'videos', 'theme': '校车', 'project_key': 'run_1__校车'},
                     'status': 'running', 'events': [], 'last_active': time.time()},
        'frames_2': {'dimensions': {'type': 'frames', 'theme': '荒岛', 'project_key': 'run_2__荒岛'},
                     'status': 'running', 'events': [], 'last_active': time.time()},
        'text_3': {'dimensions': {'type': 'compose', 'theme': 'x'}, 'status': 'running',
                   'events': [], 'last_active': time.time()},
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)

    release = threading.Event()

    def holder():
        with control.slot('videos_1', 'videos'):
            control.note_account('a')
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert _wait_for(lambda: control.board()['leases'] and control.board()['leases'][0]['user_id'] == 'a')

    def wait_then_run():
        with control.slot('frames_2', 'frames'):
            pass
    waiter = threading.Thread(target=wait_then_run)
    waiter.start()
    assert _wait_for(lambda: control.snapshot()['waiting_count'] == 1)

    board = server._build_fx_board()

    lease = board['leases'][0]
    assert (lease['project_key'], lease['stage'], lease['account_label']) == ('run_1__校车', 'videos', '#70')
    queued = board['waiting'][0]
    assert (queued['project_label'], queued['stage']) == ('荒岛', 'frames')
    stages = {row['project_key']: row['stages'] for row in board['projects']}
    assert stages['run_1__校车']['videos']['state'] == 'running'
    assert stages['run_2__荒岛']['frames']['state'] == 'waiting'
    assert all('compose' not in row['stages'] for row in board['projects'])  # 非浏览器任务不进矩阵
    states = {row['user_id']: row['state'] for row in board['accounts']}
    assert states == {'a': 'ready', 'b': 'low_credit', 'c': 'disabled'}
    assert pool.heal_calls == [False]

    release.set()
    thread.join(2)
    waiter.join(2)


def test_project_rows_get_fx_queue_marks(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    release = threading.Event()

    def holder():
        with control.slot('videos_a', 'videos'):
            release.wait(2)

    def waiter():
        with control.slot('frames_b', 'frames'):
            pass

    t1 = threading.Thread(target=holder)
    t1.start()
    assert _wait_for(lambda: control.snapshot()['active'] is not None)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert _wait_for(lambda: control.snapshot()['waiting_count'] == 1)

    rows = [
        {'task': {'id': 'frames_b'}, 'sub_jobs': []},
        {'task': {}, 'sub_jobs': [{'id': 'videos_a'}]},
        {'task': {'id': 'other'}, 'sub_jobs': [{'id': 'zzz'}]},
    ]
    server._attach_fx_queue_marks(rows)

    assert rows[0]['fx_queue']['state'] == 'waiting'
    assert rows[0]['fx_queue']['holder_task'] == 'videos_a'
    assert rows[1]['fx_queue']['state'] == 'active'
    assert rows[2]['fx_queue'] is None

    release.set()
    t1.join(2)
    t2.join(2)


def test_board_endpoint_returns_contract(monkeypatch, control):
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([]))
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/board'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))

    handler.do_GET()

    status, payload = responses[0]
    assert status == 200 and payload['status'] == 'ok'
    board = payload['board']
    for key in ('capacity', 'leases', 'waiting', 'history', 'projects', 'accounts', 'server_ts'):
        assert key in board
