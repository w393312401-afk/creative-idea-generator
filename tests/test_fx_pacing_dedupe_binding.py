# -*- coding: utf-8 -*-
"""
🧪 S4/S5/S6/S7/B7/B8 回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S4  换号只影响当前执行上下文，不再改进程级 ADSPOWER_DEFAULT_USER_ID。
S5  两条链共用提交节奏闸门（此前只有图片链会等，视频链无间隔连发）。
S6  号池状态写盘失败必须上报，不能静默吞掉（否则禁用/冷却标记会悄悄回退）。
S7  日志任务标签按上下文隔离，不会串到并发的其它任务上。
B7  去重命中要在返回值里留痕（此前静默复用旧结果，业务层看不出没真跑）。
B8  选择器统计可以清空，且外部改动后会重新加载。
"""

import os
import threading

import pytest

from integrations.google_fx.services import google_fx_helpers as helpers
from integrations.google_fx.services import google_fx as fx_service
from integrations.google_fx.utils import account_binding
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import logger as fx_logger
from integrations.google_fx.utils import selector_stats


# ── S5 提交节奏闸门 ──────────────────────────────────────────────────────────

def test_video_chain_waits_for_the_pacing_gate(monkeypatch):
    """S5 回归：视频提交前必须过闸门。

    _submit_video_to_canvas 里 note_fx_submit() 一直都有，但 fx_pacing_wait() 此前
    只在图片链被调用，视频批量的连续提交完全没有最小间隔约束。
    """
    source = helpers.__dict__['_submit_video_to_canvas'].__code__.co_names
    assert 'fx_pacing_wait' in source, '视频提交路径必须调用 fx_pacing_wait'
    assert 'note_fx_submit' in source


def test_pacing_wait_only_sleeps_the_missing_remainder(monkeypatch):
    slept = []
    monkeypatch.setattr(helpers, '_cancellable_sleep', lambda s: slept.append(s))
    monkeypatch.setattr(helpers.random, 'uniform', lambda a, b: 20.0)

    # 本进程还没提交过任何东西：不需要拉开间隔
    monkeypatch.setattr(helpers, '_LAST_FX_SUBMIT_TS', 0.0)
    assert helpers.fx_pacing_wait(15, 25) == 0.0
    assert slept == []

    # 刚提交过：补足差额
    monkeypatch.setattr(helpers, '_LAST_FX_SUBMIT_TS', helpers.time.time() - 5)
    waited = helpers.fx_pacing_wait(15, 25)
    assert 14 < waited < 16 and len(slept) == 1

    # 距上次提交已经很久：不睡
    monkeypatch.setattr(helpers, '_LAST_FX_SUBMIT_TS', helpers.time.time() - 300)
    assert helpers.fx_pacing_wait(15, 25) == 0.0


def test_pacing_bounds_are_hot_configurable(monkeypatch):
    monkeypatch.setenv('GOOGLE_FX_PACING_MIN_SECONDS', '3')
    monkeypatch.setenv('GOOGLE_FX_PACING_MAX_SECONDS', '9')
    assert helpers.fx_pacing_bounds() == (3.0, 9.0)


def test_pacing_bounds_never_return_an_inverted_range(monkeypatch):
    """min > max 会让 random.uniform 拿到反向区间，必须退化成固定间隔。"""
    monkeypatch.setenv('GOOGLE_FX_PACING_MIN_SECONDS', '30')
    monkeypatch.setenv('GOOGLE_FX_PACING_MAX_SECONDS', '5')
    low, high = helpers.fx_pacing_bounds()
    assert low == 30.0 and high == 30.0


def test_pacing_bounds_tolerate_garbage_env(monkeypatch):
    monkeypatch.setenv('GOOGLE_FX_PACING_MIN_SECONDS', 'abc')
    monkeypatch.delenv('GOOGLE_FX_PACING_MAX_SECONDS', raising=False)
    assert helpers.fx_pacing_bounds() == (15.0, 25.0)


# ── S4 账号绑定 ──────────────────────────────────────────────────────────────

def test_switch_account_does_not_touch_process_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(ap.AccountPool, '_profile_name_map', lambda self: {})
    monkeypatch.setenv('ADSPOWER_DEFAULT_USER_ID', 'original')

    pool = ap.AccountPool()
    pool.add_account('next_one')
    state = ap._read_state()
    state['next_one']['credit'] = 500
    state['next_one']['last_checked_at'] = ap._now_iso()
    ap._write_state(state)
    monkeypatch.setattr(ap, 'STALE_AFTER_SECONDS', 10 ** 9)
    monkeypatch.setattr(ap.requests, 'get', lambda *a, **k: None)

    token = account_binding.set_task_account(None)
    try:
        chosen = ap.switch_to_next_account()
        assert chosen['user_id'] == 'next_one'
        assert account_binding.current_task_account() == 'next_one'
        assert os.environ['ADSPOWER_DEFAULT_USER_ID'] == 'original', \
            '换号绝不能改进程级环境变量（否则后续所有任务都会跟着漂）'
    finally:
        account_binding.reset_task_account(token)


def test_task_account_binding_is_isolated_per_thread():
    seen = {}

    def worker(name):
        account_binding.set_task_account(name)
        # 让另一个线程有机会插进来覆盖（如果绑定是全局的就会串）
        threading.Event().wait(0.05)
        seen[name] = account_binding.current_task_account()

    threads = [threading.Thread(target=worker, args=(f'acct{i}',)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert seen == {f'acct{i}': f'acct{i}' for i in range(4)}


def test_resolve_account_priority_order(monkeypatch):
    token = account_binding.set_task_account('from_switch')
    account_binding.install_pin_resolver(lambda: 'from_pin')
    try:
        assert account_binding.resolve_account(explicit='explicit',
                                               fallback='env') == 'explicit'
        assert account_binding.resolve_account(fallback='env') == 'from_switch'
        account_binding.reset_task_account(token)
        assert account_binding.resolve_account(fallback='env') == 'from_pin'
        account_binding.install_pin_resolver(None)
        assert account_binding.resolve_account(fallback='env') == 'env'
        assert account_binding.resolve_account() == ''
    finally:
        account_binding.install_pin_resolver(None)


# ── S6 写盘失败上报 ──────────────────────────────────────────────────────────

def test_state_write_failure_is_raised_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(ap.AccountPool, '_profile_name_map', lambda self: {})
    pool = ap.AccountPool()
    pool.add_account('a')

    def boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(ap.json, 'dump', boom)
    with pytest.raises(ap.AccountPoolStateError):
        pool.set_disabled('a', True)


def test_list_accounts_tolerates_heal_write_failure(tmp_path, monkeypatch):
    """命名自愈只是显示优化，写不下去不该打断列表读取。"""
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(ap.AccountPool, '_profile_name_map',
                        lambda self: {'a': 'a@example.com'})
    pool = ap.AccountPool()
    pool.add_account('a', name='')
    state = ap._read_state()
    state['a']['name'] = ''
    ap._write_state(state)

    monkeypatch.setattr(ap, '_write_state',
                        lambda _s: (_ for _ in ()).throw(ap.AccountPoolStateError('x')))
    accounts = pool.list_accounts(heal=True)
    assert [a['user_id'] for a in accounts] == ['a']


# ── S7 日志标签隔离 ──────────────────────────────────────────────────────────

def test_task_label_does_not_leak_across_threads():
    labels = {}

    def worker(name):
        fx_logger.set_task_label(name)
        threading.Event().wait(0.05)
        labels[name] = fx_logger.current_task_label()

    threads = [threading.Thread(target=worker, args=(f'task_{i}',)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert labels == {f'task_{i}': f'task_{i}' for i in range(4)}


def test_task_label_reset_restores_previous():
    token = fx_logger.set_task_label('outer')
    inner = fx_logger.set_task_label('inner')
    assert fx_logger.current_task_label() == 'inner'
    fx_logger.reset_task_label(inner)
    assert fx_logger.current_task_label() == 'outer'
    fx_logger.reset_task_label(token)


# ── B7 去重可见化 ────────────────────────────────────────────────────────────

def test_dedupe_cache_hit_is_marked_in_the_result(monkeypatch):
    monkeypatch.setattr(fx_service, '_GOOGLE_FX_DEDUP_TTL_SECONDS', 600)
    fx_service._GOOGLE_FX_INFLIGHT_REQUESTS.clear()

    class _Req:
        def __init__(self):
            self.prompts = ['same prompt']

        def model_dump(self):
            return {'prompts': self.prompts}

    calls = []

    def fake_run(req):
        calls.append(req)
        return {'status': 'ok', 'image_urls': ['a.png']}

    monkeypatch.setattr(fx_service, '_run_with_google_fx_lock',
                        lambda label, fn, *a, **k: fn(*a, **k))

    first = fx_service._run_with_google_fx_dedupe('image_batch', _Req(), fake_run)
    assert first.get('deduped') is None, '首次真跑不该被打标'

    second = fx_service._run_with_google_fx_dedupe('image_batch', _Req(), fake_run)
    assert len(calls) == 1, '相同指纹应命中缓存'
    assert second['deduped']['fingerprint']
    assert '未重新生成' in second['deduped']['note']
    fx_service._GOOGLE_FX_INFLIGHT_REQUESTS.clear()


def test_dedupe_ttl_zero_disables_the_cache(monkeypatch):
    monkeypatch.setattr(fx_service, '_GOOGLE_FX_DEDUP_TTL_SECONDS', 0)
    fx_service._GOOGLE_FX_INFLIGHT_REQUESTS.clear()
    calls = []
    monkeypatch.setattr(fx_service, '_run_with_google_fx_lock',
                        lambda label, fn, *a, **k: calls.append(1) or {'status': 'ok'})

    class _Req:
        def model_dump(self):
            return {'p': 1}

    fx_service._run_with_google_fx_dedupe('image_batch', _Req(), lambda r: None)
    fx_service._run_with_google_fx_dedupe('image_batch', _Req(), lambda r: None)
    assert len(calls) == 2, 'TTL=0 时每次都必须真跑'


# ── B8 选择器统计 ────────────────────────────────────────────────────────────

def test_selector_stats_reset_clears_drift_warnings(tmp_path, monkeypatch):
    monkeypatch.setenv('ADSPWR_SELECTOR_STATS_FILE', str(tmp_path / 'stats.json'))
    monkeypatch.setattr(selector_stats, '_STATS', None)
    monkeypatch.setattr(selector_stats, '_STATS_MTIME', None)
    monkeypatch.setattr(selector_stats, '_STATS_PATH', None)

    selector_stats.record_hit('toolbar', 2, 'button.fallback', total=3)
    selector_stats.record_hit('toolbar', -1)
    rows = selector_stats.summarize()
    assert rows and rows[0]['miss'] == 1

    assert selector_stats.reset('toolbar') == 1
    assert selector_stats.summarize() == [], '清空后漂移警告不该还挂着'


def test_selector_stats_reload_after_external_change(tmp_path, monkeypatch):
    """B8 回归：_load 曾经一旦加载就永不失效，外部改动读不到。"""
    stats_file = tmp_path / 'stats.json'
    monkeypatch.setenv('ADSPWR_SELECTOR_STATS_FILE', str(stats_file))
    monkeypatch.setattr(selector_stats, '_STATS', None)
    monkeypatch.setattr(selector_stats, '_STATS_MTIME', None)
    monkeypatch.setattr(selector_stats, '_STATS_PATH', None)

    selector_stats.record_hit('toolbar', 0, 'button.primary', total=2)
    assert len(selector_stats.summarize()) == 1

    # 模拟"另一个进程/人工把文件清空了"
    import json
    import time as time_module
    time_module.sleep(0.01)
    stats_file.write_text(json.dumps({}), encoding='utf-8')
    os.utime(stats_file, (time_module.time() + 1, time_module.time() + 1))
    assert selector_stats.summarize() == [], '文件 mtime 变了就应该重新加载'


def test_reset_all_families(tmp_path, monkeypatch):
    monkeypatch.setenv('ADSPWR_SELECTOR_STATS_FILE', str(tmp_path / 'stats.json'))
    monkeypatch.setattr(selector_stats, '_STATS', None)
    monkeypatch.setattr(selector_stats, '_STATS_MTIME', None)
    monkeypatch.setattr(selector_stats, '_STATS_PATH', None)
    selector_stats.record_hit('a', 0)
    selector_stats.record_hit('b', 1)
    assert selector_stats.reset() == 2
    assert selector_stats.summarize() == []


def test_legacy_fabricated_credit_is_migrated_to_unprobed(tmp_path, monkeypatch):
    """B2 回归（老数据）：状态文件里那个编造的 1000 必须被清成"未探测"。

    只改 credit 恰好等于旧默认值 **且** 从来没探测成功过的账号——真实探测出 1000 的
    账号一定带 last_checked_at，不能被误伤。
    """
    import json
    state_file = tmp_path / 'accounts.json'
    state_file.write_text(json.dumps({
        'never_probed': {'name': 'A', 'credit': 1000, 'last_checked_at': None},
        'really_1000': {'name': 'B', 'credit': 1000, 'last_checked_at': '2026-07-26T10:00:00+08:00'},
        'normal': {'name': 'C', 'credit': 88, 'last_checked_at': '2026-07-26T10:00:00+08:00'},
    }), encoding='utf-8')
    monkeypatch.setattr(ap, '_STATE_FILE', state_file)

    state = ap._read_state()
    assert state['never_probed']['credit'] is None
    assert state['really_1000']['credit'] == 1000, '真实探测出 1000 的账号不能被误清'
    assert state['normal']['credit'] == 88


def test_migrated_account_is_not_counted_as_ready(tmp_path, monkeypatch):
    import json
    state_file = tmp_path / 'accounts.json'
    state_file.write_text(json.dumps({
        'legacy': {'name': 'A', 'credit': 1000, 'last_checked_at': None,
                   'disabled': False, 'cooldown_until': None},
    }), encoding='utf-8')
    monkeypatch.setattr(ap, '_STATE_FILE', state_file)
    monkeypatch.setattr(ap.AccountPool, '_profile_name_map', lambda self: {})

    rows = ap.AccountPool().list_accounts(heal=False)
    assert rows[0]['credit'] is None
    assert rows[0]['credit_probed'] is False
