# -*- coding: utf-8 -*-
"""代理号池：持久化、连通性检测、下发到 AdsPower、以及轮换取号。

对照 tests/test_google_fx_account_pool.py——两个池子是对称的运行时状态，
同样的三条硬要求：
  · 写盘失败必须抛出（禁用/检测结论丢了会让轮换重新选中坏代理）；
  · 从未检测过 ≠ 检测通过（不能把"不知道"渲染成"可用"）；
  · 密码不进对外视图（列表接口只报 has_password）。
"""

import json

import pytest

from integrations.google_fx.utils import proxy_pool as proxy_pool_module
from integrations.google_fx.utils.proxy_pool import (
    ProxyPool,
    ProxyPoolStateError,
    to_adspower_config,
)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_pool_module, '_STATE_FILE', tmp_path / 'proxy_pool.json')
    monkeypatch.setattr(proxy_pool_module, '_LEGACY_TXT_FILE', tmp_path / 'proxy_pool.txt')
    return ProxyPool()


class _Resp:
    def __init__(self, payload, code=0):
        self._payload = payload
        self.status_code = 200
        self._code = code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# ── 增删改查 ──────────────────────────────────────────────


def test_add_normalizes_and_rejects_bad_endpoints(pool):
    entry = pool.add_proxy(host=' proxy.example.com ', port=' 8080 ', proxy_type='HTTP')
    assert entry['host'] == 'proxy.example.com'
    assert entry['port'] == '8080'
    assert entry['proxy_type'] == 'http'
    assert entry['label'] == 'proxy.example.com:8080'  # 没给标签就用 host:port

    with pytest.raises(ValueError):
        pool.add_proxy(host='', port='8080')
    with pytest.raises(ValueError):
        pool.add_proxy(host='h', port='0')
    with pytest.raises(ValueError):
        pool.add_proxy(host='h', port='not-a-port')
    with pytest.raises(ValueError):
        pool.add_proxy(host='h', port='8080', proxy_type='socks4')


def test_password_never_leaves_in_public_views(pool):
    pool.add_proxy(host='h', port='1', user='u', password='s3cret')
    row = pool.list_proxies()[0]
    assert 'password' not in row
    assert row['has_password'] is True
    assert 's3cret' not in json.dumps(row)


def test_edit_keeps_password_when_not_resubmitted(pool):
    created = pool.add_proxy(host='h', port='1', user='u', password='s3cret')
    pool.add_proxy(proxy_id=created['proxy_id'], host='h', port='1', user='u',
                   note='只改备注', keep_password=True)
    assert pool.get(created['proxy_id'])['password'] == 's3cret'
    # keep_password=False（新增语义）时空密码就是清空，不要偷偷保留
    pool.add_proxy(proxy_id=created['proxy_id'], host='h', port='1', user='u',
                   keep_password=False)
    assert pool.get(created['proxy_id'])['password'] == ''


def test_changing_endpoint_clears_stale_check_result(pool):
    created = pool.add_proxy(host='h1', port='1')
    pid = created['proxy_id']
    state = proxy_pool_module._read_state()
    state['proxies'][pid].update({'last_check_status': 'ok', 'exit_ip': '1.2.3.4'})
    proxy_pool_module._write_state(state)

    # 换了出口地址：旧结论不再适用，必须清掉而不是留着让用户以为新地址验证过了
    pool.add_proxy(proxy_id=pid, host='h2', port='1', keep_password=True)
    row = pool.list_proxies()[0]
    assert row['last_check_status'] is None
    assert row['exit_ip'] == ''

    # 只改备注不动线路：检测结论应当保留
    state = proxy_pool_module._read_state()
    state['proxies'][pid].update({'last_check_status': 'ok', 'exit_ip': '5.6.7.8'})
    proxy_pool_module._write_state(state)
    pool.add_proxy(proxy_id=pid, host='h2', port='1', note='备注', keep_password=True)
    assert pool.list_proxies()[0]['exit_ip'] == '5.6.7.8'


def test_remove_and_toggle_report_missing_ids(pool):
    created = pool.add_proxy(host='h', port='1')
    assert pool.set_disabled(created['proxy_id'], True)['disabled'] is True
    assert pool.remove_proxy(created['proxy_id']) is True
    assert pool.remove_proxy(created['proxy_id']) is False
    assert pool.set_disabled('nope', True) is None


def test_write_failure_raises_instead_of_silently_dropping_state(pool, monkeypatch):
    created = pool.add_proxy(host='h', port='1')

    def _boom(*_args, **_kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(proxy_pool_module.json, 'dump', _boom)
    with pytest.raises(ProxyPoolStateError):
        pool.set_disabled(created['proxy_id'], True)


# ── 连通性检测 ────────────────────────────────────────────


def test_check_records_real_exit_ip(pool, monkeypatch):
    created = pool.add_proxy(host='h', port='1', user='u', password='p')
    seen = {}

    def _get(url, proxies=None, timeout=None):
        seen['url'] = url
        seen['proxies'] = proxies
        return _Resp({'ip': '203.0.113.9', 'city': 'Osaka', 'country': 'JP'})

    monkeypatch.setattr(proxy_pool_module.requests, 'get', _get)
    row = pool.check_proxy(created['proxy_id'])
    assert row['last_check_status'] == 'ok'
    assert row['exit_ip'] == '203.0.113.9'
    assert row['exit_location'] == 'Osaka JP'
    assert seen['proxies']['https'].startswith('http://u:p@h:1')


def test_check_failure_is_recorded_not_swallowed(pool, monkeypatch):
    created = pool.add_proxy(host='h', port='1')

    def _get(*_args, **_kwargs):
        raise OSError('connection refused')

    monkeypatch.setattr(proxy_pool_module.requests, 'get', _get)
    row = pool.check_proxy(created['proxy_id'])
    assert row['last_check_status'] == 'failed'
    assert 'connection refused' in row['last_check_error']
    assert row['exit_ip'] == ''
    assert pool.check_proxy('missing') is None


def test_check_without_exit_ip_is_not_reported_ok(pool, monkeypatch):
    """代理回了 200 但没给 IP：不能算验证通过。"""
    created = pool.add_proxy(host='h', port='1')
    monkeypatch.setattr(proxy_pool_module.requests, 'get',
                        lambda *a, **k: _Resp({'city': 'nowhere'}))
    row = pool.check_proxy(created['proxy_id'])
    assert row['last_check_status'] == 'failed'


# ── 下发到 AdsPower ───────────────────────────────────────


def test_apply_writes_user_proxy_config_and_binds(pool, monkeypatch):
    created = pool.add_proxy(host='h', port='1', user='u', password='p', proxy_type='socks5')
    sent = {}

    def _post(url, json=None, timeout=None):
        sent['url'] = url
        sent['body'] = json
        return _Resp({'code': 0})

    monkeypatch.setattr(proxy_pool_module.requests, 'post', _post)
    monkeypatch.setattr(proxy_pool_module, 'get_runtime_default_port', lambda: '50325')
    row = pool.apply_to_profile(created['proxy_id'], 'profile-a')

    assert sent['url'] == 'http://127.0.0.1:50325/api/v1/user/update'
    assert sent['body']['user_id'] == 'profile-a'
    assert sent['body']['user_proxy_config'] == {
        'proxy_soft': 'other', 'proxy_type': 'socks5', 'proxy_host': 'h',
        'proxy_port': '1', 'proxy_user': 'u', 'proxy_password': 'p',
    }
    assert row['bound_user_id'] == 'profile-a'
    assert row['applied_at']


def test_apply_surfaces_adspower_rejection_and_guards_inputs(pool, monkeypatch):
    created = pool.add_proxy(host='h', port='1')
    monkeypatch.setattr(proxy_pool_module.requests, 'post',
                        lambda *a, **k: _Resp({'code': -1, 'msg': 'user not found'}))
    monkeypatch.setattr(proxy_pool_module, 'get_runtime_default_port', lambda: '50325')
    with pytest.raises(RuntimeError, match='user not found'):
        pool.apply_to_profile(created['proxy_id'], 'ghost')
    with pytest.raises(ValueError):
        pool.apply_to_profile(created['proxy_id'], '')
    with pytest.raises(KeyError):
        pool.apply_to_profile('nope', 'profile-a')

    pool.set_disabled(created['proxy_id'], True)
    with pytest.raises(ValueError, match='禁用'):
        pool.apply_to_profile(created['proxy_id'], 'profile-a')


# ── 轮换与摘要 ────────────────────────────────────────────


def test_pick_rotates_and_skips_unusable(pool, monkeypatch):
    first = pool.add_proxy(host='h1', port='1', label='一号')
    second = pool.add_proxy(host='h2', port='1', label='二号')
    third = pool.add_proxy(host='h3', port='1', label='三号')
    pool.set_disabled(third['proxy_id'], True)
    monkeypatch.setattr(proxy_pool_module.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('down')))
    pool.check_proxy(second['proxy_id'])  # 标记为 failed，不参与轮换

    picked = [pool.pick_proxy()['proxy_id'] for _ in range(3)]
    assert set(picked) == {first['proxy_id']}  # 只剩一号可用，轮换不该选中坏的

    fourth = pool.add_proxy(host='h4', port='1')
    assert {pool.pick_proxy()['proxy_id'] for _ in range(4)} == {
        first['proxy_id'], fourth['proxy_id']}


def test_empty_pool_picks_nothing(pool):
    assert pool.pick_proxy() is None
    assert pool.usable_proxies() == []
    assert pool.summary() == {'total': 0, 'enabled': 0, 'disabled': 0, 'ok': 0,
                              'failed': 0, 'unchecked': 0, 'bound': 0}


def test_summary_counts_unchecked_separately_from_ok(pool, monkeypatch):
    pool.add_proxy(host='h1', port='1')
    checked = pool.add_proxy(host='h2', port='1')
    monkeypatch.setattr(proxy_pool_module.requests, 'get',
                        lambda *a, **k: _Resp({'ip': '1.1.1.1'}))
    pool.check_proxy(checked['proxy_id'])

    summary = pool.summary()
    assert summary['total'] == 2
    assert summary['ok'] == 1
    assert summary['unchecked'] == 1  # 未检测的没被算进 ok


def test_import_legacy_txt_skips_duplicates_and_comments(pool, tmp_path):
    proxy_pool_module._LEGACY_TXT_FILE.write_text(
        '# 注释行\n'
        '1.2.3.4:8080\n'
        '5.6.7.8:9090:user:pass\n'
        '1.2.3.4:8080\n'
        'garbage\n',
        encoding='utf-8',
    )
    result = pool.import_legacy_txt()
    assert result['added'] == 2
    assert result['skipped'] == 2
    rows = {r['endpoint'] for r in pool.list_proxies()}
    assert rows == {'1.2.3.4:8080', '5.6.7.8:9090'}
    # 再导一次不应重复
    assert pool.import_legacy_txt()['added'] == 0


def test_import_legacy_reports_missing_file(pool):
    result = pool.import_legacy_txt()
    assert result['added'] == 0
    assert 'proxy_pool.txt' in result['message']


def test_adspower_config_omits_empty_credentials():
    assert to_adspower_config({'host': 'h', 'port': '1'}) == {
        'proxy_soft': 'other', 'proxy_type': 'http', 'proxy_host': 'h', 'proxy_port': '1',
    }
