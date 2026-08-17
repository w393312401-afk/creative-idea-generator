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
    """「锁定默认环境」会把轮转环压成单元素，换号节拍那个数字一次都用不上。

    回归的是一个纯粹的"配了但没生效"：控制台状态条照旧显示「换号节拍 每 15 次请求」、
    保存时还回一句"已保存并热生效"，而 _account_rotation_ring 早就退化成 [默认账号]，
    两条链的切腿逻辑都走单腿分支，整条序列一个号跑到底。
    """
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
    assert snapshot['configuration']['account_switch_requests'] == 15
    assert snapshot['configuration']['account_switch_effective'] is False
    assert 'switch_interval_inert' in {item['code'] for item in snapshot['diagnostics']}


def test_switch_interval_not_flagged_when_default_account_is_unlocked(monkeypatch):
    """只是钉了默认环境、没勾锁定时，后续仍按节拍轮转——不该误报成不生效。"""
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
    assert snapshot['configuration']['account_switch_effective'] is True
    assert 'switch_interval_inert' not in {item['code'] for item in snapshot['diagnostics']}


def test_inert_config_notes_cover_locked_switch_interval():
    """保存回执：热生效不等于跑得起来，被压住的字段要如实说出来。"""
    assert server._inert_config_notes({
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': 'pinned',
        'googleFxSequenceUserLock': True,
    }), '锁定 + 指定环境时换号节拍必须被标为不生效'
    # 勾了锁定却没指定环境是被 validate_patch 拦下的空承诺，行为仍是自动选号轮转
    assert server._inert_config_notes({
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': '',
        'googleFxSequenceUserLock': True,
    }) == []
    assert server._inert_config_notes({
        'googleFxIpRotateRequests': 15,
        'googleFxSequenceUserId': 'pinned',
        'googleFxSequenceUserLock': False,
    }) == []


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
