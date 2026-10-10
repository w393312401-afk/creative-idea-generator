from email.message import Message
import contextlib
import json
import threading
import time

import pytest

import server
import fx_console
from fx_control import FxControlPlane
from integrations.google_fx.utils import account_pool as account_pool_module
from integrations.google_fx.utils import selector_stats


def test_manual_endpoint_reads_reorganized_guide():
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/manual'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    handler.do_GET()
    status, payload = responses[0]
    assert status == 200
    assert payload['status'] == 'ok'
    assert payload['path'] == 'docs/guides/google_fx_console_manual.md'
    assert 'Google FX' in payload['markdown']


def test_fx_log_tail_reads_dedicated_logger_file_and_filters_task(tmp_path, monkeypatch):
    """实时日志接口必须读取 FX logger 真正写入的文件，而不是主服务日志。"""
    fx_log = tmp_path / 'fx.log'
    fx_log.write_text(
        '[12:00:00] │ ℹ️ │ 积分探针 │ [credit_probe_a] 开始探测\n'
        '[12:00:01] │ ℹ️ │ 积分探针 │ [credit_probe_b] 开始探测\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(server, '_fx_log_path', lambda: str(fx_log))

    assert server._fx_log_tail(task_id='credit_probe_a') == [
        '[12:00:00] │ ℹ️ │ 积分探针 │ [credit_probe_a] 开始探测'
    ]


class _Pool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.heal_calls = []

    def list_accounts(self, heal=True):
        self.heal_calls.append(heal)
        return list(self.accounts)


class _Flag:
    def active_count(self):
        return 2


class _Lock:
    def locked(self):
        return True


@contextlib.contextmanager
def _connected_socket(*args, **kwargs):
    yield object()


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """快照有 TTL 缓存，测试之间必须清掉，否则读到上一个用例的结果。"""
    server._FX_SNAPSHOT_CACHE['at'] = 0.0
    server._FX_SNAPSHOT_CACHE['value'] = None
    yield
    server._FX_SNAPSHOT_CACHE['at'] = 0.0
    server._FX_SNAPSHOT_CACHE['value'] = None


def test_status_snapshot_aggregates_runtime_accounts_tasks_and_selectors(monkeypatch):
    monkeypatch.setattr(server, 'effective_config', lambda _: {
        'adsPowerPort': '50325',
        'googleFxImageModel': 'Nano Banana 2',
        'videoModel': 'Veo Test',
        'googleFxIpRotateRequests': 4,
        'googleFxUserId': 'profile-a',
    })
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    pool = _Pool([
        {'user_id': 'a', 'credit': 100, 'disabled': False, 'cooldown_until': None},
        {'user_id': 'b', 'credit': 0, 'disabled': True, 'cooldown_until': None},
    ])
    monkeypatch.setattr(server, '_get_account_pool', lambda: pool)
    monkeypatch.setattr(server, 'get_fx_cancel_flag', lambda: _Flag())
    monkeypatch.setattr(server, '_FX_SERIAL_LOCK', _Lock())
    monkeypatch.setattr(selector_stats, 'summarize', lambda: [
        {'family': 'toolbar', 'primary_ratio': 0.5, 'miss': 1},
    ])

    with server.ACTIVE_TASKS_LOCK:
        old = dict(server.ACTIVE_TASKS)
        server.ACTIVE_TASKS.clear()
        server.ACTIVE_TASKS['videos_1'] = {
            'dimensions': {'type': 'videos', 'theme': 'demo', 'userId': 'a'},
            'status': 'running', 'events': [('progress', {'stage': 'submitting'})],
            'error': None, 'last_active': time.time(),
        }
        server.ACTIVE_TASKS['text_1'] = {
            'dimensions': {'type': 'compose'}, 'status': 'running', 'events': [],
            'error': None, 'last_active': time.time(),
        }
    try:
        snapshot = server._google_fx_status_snapshot()
    finally:
        with server.ACTIVE_TASKS_LOCK:
            server.ACTIVE_TASKS.clear()
            server.ACTIVE_TASKS.update(old)

    assert snapshot['runtime']['available'] is True
    assert snapshot['adspower']['online'] is True
    assert snapshot['execution'] == {
        'lock_busy': True, 'active_requests': 2, 'running_tasks': 1,
    }
    assert snapshot['accounts']['total'] == 2
    assert snapshot['accounts']['ready'] == 1
    assert snapshot['configuration']['selected_user_id'] == 'profile-a'
    assert snapshot['tasks'][0]['stage'] == 'submitting'
    assert snapshot['selectors']['warnings'][0]['family'] == 'toolbar'
    assert any(item['code'] == 'selector_drift' for item in snapshot['diagnostics'])
    assert 'apiKey' not in str(snapshot) and 'password' not in str(snapshot)


@pytest.mark.parametrize('mode, warns', [
    ('accepting', False),
    ('running', False),
    ('draining', True),
])
def test_status_diagnostic_accepts_current_and_legacy_ready_modes(monkeypatch, mode, warns):
    """当前 accepting 与旧 running 均可接收任务，不能误报为拒绝新任务。"""
    control = FxControlPlane()
    queue = control.snapshot()
    queue['mode'] = mode
    monkeypatch.setattr(control, 'snapshot', lambda: queue)
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server, 'effective_config', lambda _: {'adsPowerPort': '50325'})
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([]))
    monkeypatch.setattr(server, '_get_proxy_pool', lambda: type(
        'ProxyPool', (), {'summary': lambda self: {}})())
    monkeypatch.setattr(selector_stats, 'summarize', lambda: [])

    snapshot = server._google_fx_status_snapshot()

    warnings = [item for item in snapshot['diagnostics']
                if item['code'] == 'service_not_accepting']
    assert bool(warnings) is warns
    if warns:
        assert mode in warnings[0]['message']


def test_status_snapshot_never_triggers_adspower_name_healing(monkeypatch):
    """S3 回归：快照必须用 heal=False。

    命名自愈会打 AdsPower 本地 HTTP（含限频退避重试，最坏十几秒）。控制台按秒级
    轮询这个"只读"接口，一旦它会打 AdsPower，AdsPower 卡住就会把控制台一起拖死。
    """
    monkeypatch.setattr(server, 'effective_config', lambda _: {'adsPowerPort': '50325'})
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    pool = _Pool([{'user_id': 'a', 'credit': 5, 'disabled': False, 'cooldown_until': None}])
    monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

    server._google_fx_status_snapshot()
    assert pool.heal_calls == [False], '状态快照绝不能触发命名自愈'


def test_status_snapshot_uses_ttl_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(server, '_google_fx_status_snapshot',
                        lambda: calls.append(1) or {'status': 'ok'})
    server.google_fx_status_snapshot()
    server.google_fx_status_snapshot()
    assert len(calls) == 1, 'TTL 内的重复轮询应命中缓存'
    server.google_fx_status_snapshot(force=True)
    assert len(calls) == 2, 'force=1 必须绕过缓存'


def test_status_snapshot_reports_unprobed_and_login_required(monkeypatch):
    """B2/B4 回归：'未探测'不能被算成可用，登录失效要单独报出来。"""
    monkeypatch.setattr(server, 'effective_config', lambda _: {'adsPowerPort': '50325'})
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([
        {'user_id': 'fresh', 'credit': None, 'disabled': False, 'cooldown_until': None},
        {'user_id': 'locked', 'credit': 50, 'disabled': False,
         'cooldown_until': '2099-01-01T00:00:00+08:00', 'cooldown_reason': 'login_required'},
    ]))

    snapshot = server._google_fx_status_snapshot()
    assert snapshot['accounts']['unprobed'] == 1
    assert snapshot['accounts']['ready'] == 0, '未探测的账号不算可用'
    assert snapshot['accounts']['login_required'] == 1
    codes = {item['code'] for item in snapshot['diagnostics']}
    assert 'accounts_unprobed' in codes and 'accounts_login_required' in codes


def test_locked_default_account_marks_switch_interval_inert(monkeypatch):
    """旧换号节拍保留配置值，但锁定环境也按额度用尽后换号。"""
    monkeypatch.setattr(server, 'effective_config', lambda _: {
        'adsPowerPort': '50325',
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': 'pinned',
        'googleFxSequenceUserLock': True,
    })
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([
        {'user_id': 'pinned', 'credit': 100, 'disabled': False, 'cooldown_until': None},
        {'user_id': 'other', 'credit': 100, 'disabled': False, 'cooldown_until': None},
    ]))

    snapshot = server._google_fx_status_snapshot()
    assert snapshot['configuration']['account_switch_effective'] is False
    assert 'switch_interval_inert' in {item['code'] for item in snapshot['diagnostics']}


def test_switch_interval_remains_inert_when_default_account_is_unlocked(monkeypatch):
    """不锁定默认环境也优先复用浏览器，旧请求计数不再触发换号。"""
    monkeypatch.setattr(server, 'effective_config', lambda _: {
        'adsPowerPort': '50325',
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': 'pinned',
        'googleFxSequenceUserLock': False,
    })
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([
        {'user_id': 'pinned', 'credit': 100, 'disabled': False, 'cooldown_until': None},
    ]))

    snapshot = server._google_fx_status_snapshot()
    assert snapshot['configuration']['account_switch_effective'] is False
    note = next(item for item in snapshot['diagnostics'] if item['code'] == 'switch_interval_inert')
    assert note['level'] == 'info'
    assert '复用' in note['message']


@pytest.mark.parametrize('preferred,locked', [('pinned', True), ('', True), ('pinned', False), ('', False)])
def test_inert_config_notes_cover_legacy_switch_interval(preferred, locked):
    """保存回执始终说明额度策略，避免旧配置被误认为仍控制轮转。"""
    notes = server._inert_config_notes({
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': preferred,
        'googleFxSequenceUserLock': locked,
    })
    assert notes
    assert '余额' in '；'.join(notes)
    assert '24' in '；'.join(notes)


def test_status_endpoint_is_gated_and_returns_snapshot(monkeypatch):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/google-fx/status'
    handler.headers = Message()
    handler._gate = lambda *a, **k: True
    sent = []
    handler._send_json = lambda obj, status=200: sent.append((obj, status))
    monkeypatch.setattr(server, 'google_fx_status_snapshot',
                        lambda force=False: {'status': 'ok', 'runtime': {}})

    handler.do_GET()
    assert sent == [({'status': 'ok', 'runtime': {}}, 200)]


def test_clear_cooldown_preserves_credit_and_check_time(tmp_path, monkeypatch):
    monkeypatch.setattr(account_pool_module, '_STATE_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(account_pool_module.AccountPool, '_profile_name_map', lambda self: {})
    pool = account_pool_module.AccountPool()
    pool.add_account('a', 'A')
    state = account_pool_module._read_state()
    state['a']['credit'] = 17
    state['a']['last_checked_at'] = '2026-07-26T10:00:00+08:00'
    state['a']['cooldown_until'] = '2099-01-01T00:00:00+08:00'
    state['a']['cooldown_reason'] = 'login_required'
    account_pool_module._write_state(state)

    result = pool.clear_cooldown('a')
    assert result['cooldown_until'] is None
    assert 'cooldown_reason' not in result
    assert result['credit'] == 17
    assert result['last_checked_at'] == '2026-07-26T10:00:00+08:00'


# ── 配置白名单与版本栈（B3 / C1）───────────────────────────────────────────────

@pytest.fixture
def config_store(tmp_path):
    config_file = tmp_path / 'server_config.json'
    config = {'apiKey': 'secret', 'adsPowerPort': 50325}
    config_file.write_text(json.dumps(config), encoding='utf-8')
    control = FxControlPlane(tmp_path / 'control.json', tmp_path / 'audit.jsonl')
    store = fx_console.FxConfigStore(
        config=config,
        config_file=str(config_file),
        versions_file=str(tmp_path / 'versions.jsonl'),
        apply_overrides=lambda _cfg: None,
        audit=control.audit,
    )
    return store, config, control


def test_fx_config_update_is_whitelisted_and_audited(config_store):
    store, config, control = config_store

    with pytest.raises(ValueError):
        store.save({'apiKey': 'leak'})

    outcome = store.save({'adsPowerPort': 50326, 'googleFxIpRotateRequests': 7})
    assert outcome['config']['adsPowerPort'] == 50326
    assert config['apiKey'] == 'secret', '白名单外的字段必须原样保留'
    assert control.recent_audit()[0]['details']['before']['adsPowerPort'] == 50325


def test_fx_config_noop_save_returns_empty_changed(config_store):
    store, _config, _control = config_store
    store.save({'adsPowerPort': 50326})
    outcome = store.save({'adsPowerPort': 50326})
    assert outcome['changed'] == {}
    assert outcome['version'] is None


def test_pacing_bounds_must_not_be_inverted(config_store):
    store, _config, _control = config_store
    with pytest.raises(ValueError):
        store.save({'googleFxPacingMinSeconds': 40, 'googleFxPacingMaxSeconds': 10})


def test_bool_config_round_trips_to_env(config_store, monkeypatch):
    store, _config, _control = config_store
    monkeypatch.delenv('FX_DRY_RUN', raising=False)
    store.save({'googleFxDryRun': True})
    assert store.current()['googleFxDryRun'] is True
    fx_console.apply_direct_env(store.current())
    import os
    assert os.environ['FX_DRY_RUN'] == '1'


def test_schema_marks_restart_required_fields():
    schema = fx_console.FX_CONFIG_SPEC
    # 这些读取方是 import 期求值的模块级常量，必须如实标成"需重启"，
    # 不能在 UI 上谎称热生效。
    assert schema['googleFxDedupTtlSeconds']['hot'] is False
    assert schema['googleFxRunLockWaitSeconds']['hot'] is False
    # 这些的读取方每次调用现读，能热生效
    assert schema['googleFxPacingMinSeconds']['hot'] is True
    assert schema['googleFxMaxWaitSeconds']['hot'] is True
    # 旧数值仍可通过 API 保存/导入，但界面不再提供一个会误导调度行为的编辑框。
    assert schema['googleFxIpRotateRequests']['inactive'] is True
    assert fx_console.validate_patch({'googleFxIpRotateRequests': 20}) == {'googleFxIpRotateRequests': 20}

@pytest.mark.parametrize('task,expected', [
    ({'status': 'completed', 'dimensions': {'type': 'frames'},
      'result': {'frames': [{'sequence': 1, 'url': '/frame.webp'}]}}, 'completed'),
    ({'status': 'completed', 'dimensions': {'type': 'frames'},
      'result': {'frames': [{'sequence': 1, 'status': 'failed'}]}}, 'partial_failed'),
    ({'status': 'completed', 'dimensions': {'type': 'frames'},
      'result': {'frames': [], 'halted_at_sequence': 3}}, 'partial_failed'),
    ({'status': 'completed', 'dimensions': {'type': 'frames'},
      'result': {'frames': [{'sequence': 1}], 'videos': [{'status': 'failed'}]}}, 'completed'),
    ({'status': 'completed', 'result': {'completion_state': 'partial_failed'}}, 'partial_failed'),
    ({'status': 'completed', 'result': {'merge_error': 'missing clip'}}, 'partial_failed'),
    ({'status': 'completed', 'result': {'completion_state': 'completed_with_warnings'}}, 'completed_with_warnings'),
    ({'status': 'running', 'outcome': 'partial_failed', 'result': {'has_failures': True}}, 'running'),
])
def test_task_outcome_uses_real_results_without_stale_retry_or_unrelated_video_errors(task, expected):
    assert server._task_outcome(task) == expected


def test_task_list_exposes_small_business_outcome_summary(monkeypatch):
    tasks = {
        'videos_partial': {'id': 'videos_partial', 'status': 'completed',
            'dimensions': {'type': 'videos'}, 'events': [], 'last_active': 20,
            'error': None, 'outcome': 'partial_failed',
            'result': {'completion_state': 'partial_failed', 'videos': [{'slot': 44, 'status': 'failed'}]}},
        'frames_ok': {'id': 'frames_ok', 'status': 'completed',
            'dimensions': {'type': 'frames'}, 'events': [], 'last_active': 10,
            'error': None, 'result': {'frames': [{'sequence': 1, 'url': '/ok.webp'}]}},
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/tasks'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda payload, status=200: responses.append(payload)
    handler.do_GET()
    rows = {task['id']: task for task in responses[0]['tasks']}
    assert rows['videos_partial']['outcome'] == 'partial_failed'
    assert rows['videos_partial']['result']['has_failures'] is True
    assert rows['frames_ok']['result']['completion_state'] == 'completed'
    assert 'frames' not in rows['frames_ok']['result']  # Polling stays lightweight.


def test_fx_status_keeps_historical_failures_separate_from_current_intervention(monkeypatch):
    monkeypatch.setattr(server, 'effective_config', lambda _: {'adsPowerPort': '50325'})
    monkeypatch.setattr(server.socket, 'create_connection', _connected_socket)
    monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool([]))
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {
        'videos_partial': {'status': 'completed', 'outcome': 'partial_failed',
            'dimensions': {'type': 'videos'}, 'events': [], 'last_active': 20},
        'frames_running': {'status': 'running', 'outcome': 'partial_failed',
            'dimensions': {'type': 'frames'}, 'events': [], 'last_active': 30},
    })
    monkeypatch.setattr(server, '_fx_task_timeline', lambda *_: [
        {'stage': 'old', 'duration_seconds': 900, 'slow': True},
        {'stage': 'submitting', 'duration_seconds': 5, 'slow': False},
    ])
    monkeypatch.setattr(server, '_fx_manual_intervention', lambda *_: {'code': 'login_required'})
    snapshot = server._google_fx_status_snapshot()
    rows = {task['id']: task for task in snapshot['tasks']}
    assert rows['videos_partial']['outcome'] == 'partial_failed'
    assert rows['videos_partial']['manual_intervention'] is None
    assert rows['videos_partial']['stuck_stage'] is None
    assert rows['frames_running']['outcome'] == 'running'
    assert rows['frames_running']['manual_intervention']['code'] == 'login_required'
    assert rows['frames_running']['stuck_stage'] is None, 'A slow past stage is not a current stall'
    recent = next(row for row in snapshot['diagnostics'] if row['code'] == 'recent_failures')
    assert recent['level'] == 'info'
    assert recent['message'].startswith('历史记录')
