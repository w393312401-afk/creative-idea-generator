"""Airport import keeps credentials private and preserves pool routing identity."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.google_fx.utils import airport_bridge as bridge
from integrations.google_fx.utils import proxy_pool
from tools import import_airport_proxies as importer


@pytest.fixture
def nodes():
    return [
        {'name': 'US Los Angeles', 'type': 'ss', 'server': 'us.example',
         'port': 443, 'cipher': 'aes-128-gcm', 'password': 'private-us-secret'},
        {'name': '🇯🇵 Tokyo', 'type': 'trojan', 'server': 'jp.example',
         'port': 443, 'password': 'private-jp-secret', 'sni': 'tls.example'},
    ]


def test_listeners_are_independent_loopback_routes_with_no_global_proxy(nodes, tmp_path):
    state = {'proxies': {}, 'rotate_index': 3}
    original_nodes, original_state = copy.deepcopy(nodes), copy.deepcopy(state)
    _, config, entries = bridge.build_bridge(nodes, tmp_path / 'subscription.yaml', state)

    assert config['allow-lan'] is False
    assert config['tun']['enable'] is False
    assert config['dns']['enable'] is False
    assert config['rules'] == ['MATCH,REJECT']
    assert not ({'port', 'socks-port', 'mixed-port', 'external-controller', 'proxy-groups'} & config.keys())
    assert len({row['port'] for row in config['listeners']}) == len(nodes)
    for listener, node in zip(config['listeners'], nodes):
        assert listener['listen'] == '127.0.0.1'
        assert listener['type'] == 'http'
        assert listener['proxy'] == node['name']
        entry = entries[listener['name']]
        assert (entry['host'], int(entry['port'])) == ('127.0.0.1', listener['port'])
        assert entry['node_name'] == node['name']
    assert state == original_state
    assert nodes == original_nodes
    config['proxies'][0]['password'] = 'edited'
    assert nodes == original_nodes


@pytest.mark.parametrize('name', ['美国 洛杉矶', '美國 西雅图', '🇺🇸 Dallas', 'US-01', 'USA Chicago', 'United States'])
def test_us_names_receive_preferred_airport_tier(name, nodes, tmp_path):
    preferred = {**nodes[0], 'name': name}
    _, _, entries = bridge.build_bridge([preferred, nodes[1]], tmp_path / 'profile', {'proxies': {}})
    by_name = {entry['node_name']: entry for entry in entries.values()}
    assert by_name[name]['fallback_tier'] == 0
    assert by_name[nodes[1]['name']]['fallback_tier'] == 1


def test_reordering_keeps_ids_ports_manual_edits_and_check_results(nodes, tmp_path):
    profile = tmp_path / 'subscription.yaml'
    bridge_id, _, first = bridge.build_bridge(nodes, profile, {'proxies': {}})
    pid = next(iter(first))
    first[pid].update(disabled=True, label='My label', note='My note',
                      bound_user_id='profile-7', use_count=4, last_check_status='ok',
                      exit_ip='203.0.113.9', latency_ms=123)
    state = {'proxies': first, 'rotate_index': 7}
    reordered_id, _, reordered = bridge.build_bridge(list(reversed(nodes)), profile, state)
    assert reordered_id == bridge_id
    assert reordered == first
    merged = bridge.merge_entries(state, bridge_id, reordered)
    assert merged == state


def test_updated_node_keeps_endpoint_and_user_metadata_but_resets_check(nodes, tmp_path):
    profile = tmp_path / 'subscription.yaml'
    _, _, entries = bridge.build_bridge(nodes, profile, {'proxies': {}})
    pid = next(iter(entries))
    entries[pid].update(disabled=True, bound_user_id='profile-7', last_check_status='ok',
                        exit_ip='203.0.113.9', latency_ms=123)
    old_entry = copy.deepcopy(entries[pid])
    changed = copy.deepcopy(nodes)
    changed[0].update(server='new-us.example', password='new-private-secret')
    _, config, updated = bridge.build_bridge(changed, profile, {'proxies': entries})

    assert updated[pid]['port'] == old_entry['port']
    assert updated[pid]['node_fingerprint'] != old_entry['node_fingerprint']
    assert updated[pid]['disabled'] is True
    assert updated[pid]['bound_user_id'] == 'profile-7'
    assert updated[pid]['last_check_status'] is None
    assert updated[pid]['exit_ip'] == ''
    assert updated[pid]['latency_ms'] is None
    assert config['proxies'][0]['password'] == 'new-private-secret'
    assert 'new-private-secret' not in json.dumps(updated)


def test_removal_retires_endpoint_without_reusing_port_or_modifying_static(nodes, tmp_path):
    profile = tmp_path / 'subscription.yaml'
    state = {'proxies': {
        'static': {'host': 'static.example', 'port': '8080', 'password': 'static-private',
                   'disabled': False, 'last_check_status': 'ok'},
        'other-local': {'host': '127.0.0.1', 'port': '17901', 'disabled': True},
    }, 'rotate_index': 4}
    bridge_id, _, entries = bridge.build_bridge(nodes, profile, state)
    assert min(int(entry['port']) for entry in entries.values()) == 17902
    initial = bridge.merge_entries(state, bridge_id, entries)
    retired_id = next(pid for pid, entry in entries.items() if entry['node_name'] == nodes[0]['name'])
    fresh = {**nodes[0], 'name': '🇺🇸 New node'}
    _, config, replacement = bridge.build_bridge([nodes[1], fresh], profile, initial)
    merged = bridge.merge_entries(initial, bridge_id, replacement)

    assert merged['proxies']['static'] == state['proxies']['static']
    assert merged['proxies']['other-local'] == state['proxies']['other-local']
    assert merged['rotate_index'] == 4
    assert merged['proxies'][retired_id]['disabled'] is True
    assert merged['proxies'][retired_id]['last_check_status'] == 'failed'
    assert retired_id not in {listener['name'] for listener in config['listeners']}
    assert merged['proxies'][retired_id]['port'] not in {entry['port'] for entry in replacement.values()}
    assert initial['proxies'][retired_id]['disabled'] is False


def test_merge_keeps_manual_changes_during_changed_node_startup(nodes, tmp_path):
    profile = tmp_path / 'subscription.yaml'
    bridge_id, _, old_entries = bridge.build_bridge(nodes, profile, {'proxies': {}})
    state = {'proxies': old_entries}
    changed = copy.deepcopy(nodes)
    changed[0]['password'] = 'updated-private-secret'
    _, _, replacement = bridge.build_bridge(changed, profile, state)
    pid = next(iter(old_entries))
    latest = copy.deepcopy(state)
    latest['proxies'][pid].update(disabled=True, bound_user_id='bound-during-startup',
                                   label='edited during startup', use_count=19,
                                   last_check_status='ok', exit_ip='203.0.113.8')
    merged = bridge.merge_entries(latest, bridge_id, replacement)
    entry = merged['proxies'][pid]
    assert entry['disabled'] is True
    assert entry['bound_user_id'] == 'bound-during-startup'
    assert entry['label'] == 'edited during startup'
    assert entry['use_count'] == 19
    assert entry['last_check_status'] is None
    assert entry['exit_ip'] == ''


def test_pool_never_contains_outbound_credentials(nodes, tmp_path):
    bridge_id, config, entries = bridge.build_bridge(nodes, tmp_path / 'profile', {'proxies': {}})
    serialized = json.dumps(bridge.merge_entries({'proxies': {}}, bridge_id, entries))
    for node in nodes:
        assert node['password'] not in serialized
        assert node['server'] not in serialized
    assert all(entry['user'] == entry['password'] == '' for entry in entries.values())
    assert config['proxies'] == nodes


@pytest.fixture
def import_environment(nodes, tmp_path, monkeypatch):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir()
    home.mkdir()
    core = tmp_path / 'mihomo'
    core.write_text('test stub')
    state_path = root / 'proxy_pool.json'
    initial = {'proxies': {'static': {'host': 'static.example', 'port': '8080'}}, 'rotate_index': 2}
    state_path.write_text(json.dumps(initial))
    monkeypatch.setattr(proxy_pool, '_STATE_FILE', state_path)
    monkeypatch.setattr(proxy_pool, 'log', lambda *args, **kwargs: None)
    monkeypatch.setattr(importer, 'ROOT', root)
    monkeypatch.setattr(importer.sys, 'platform', 'darwin')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(importer, 'read_profile', lambda path: copy.deepcopy(nodes))
    profile = tmp_path / 'subscription.yaml'
    bridge_id, _, _ = bridge.build_bridge(nodes, profile, initial)
    directory = home / 'Library' / 'Application Support' / 'SPARK' / 'airport_bridge' / bridge_id
    directory.mkdir(parents=True)
    old_config = directory / 'config.json'
    old_config.write_text('original config')
    calls = []
    return SimpleNamespace(profile=profile, core=core, initial=initial, state_path=state_path,
                           directory=directory, old_config=old_config, calls=calls)


def test_validation_failure_leaves_pool_and_active_config_untouched(import_environment, monkeypatch):
    env = import_environment
    def run(args, **kwargs):
        env.calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'config invalid', stderr=b'')
    monkeypatch.setattr(importer.subprocess, 'run', run)
    monkeypatch.setattr(proxy_pool, '_write_state', lambda _: pytest.fail('pool was written'))

    with pytest.raises(RuntimeError, match='未通过内核校验'):
        importer.import_profile(env.profile, env.core)
    assert json.loads(env.state_path.read_text()) == env.initial
    assert env.old_config.read_text() == 'original config'
    assert all(args[0] != 'launchctl' for args in env.calls)
    assert (env.directory / 'candidate.json').stat().st_mode & 0o777 == 0o600


def test_startup_failure_restores_config_without_writing_pool(import_environment, monkeypatch):
    env = import_environment
    def run(args, **kwargs):
        env.calls.append(args)
        return SimpleNamespace(returncode=1 if args[:2] == ['launchctl', 'print'] else 0,
                               stdout=b'', stderr=b'')
    monkeypatch.setattr(importer.subprocess, 'run', run)
    monkeypatch.setattr(importer, 'listening', lambda port: False)
    clock = iter([0, 16])
    monkeypatch.setattr(importer.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(proxy_pool, '_write_state', lambda _: pytest.fail('pool was written'))

    with pytest.raises(RuntimeError, match='未能启动全部本地入口'):
        importer.import_profile(env.profile, env.core)
    assert env.old_config.read_text() == 'original config'
    assert json.loads(env.state_path.read_text()) == env.initial
    assert not (env.directory / 'candidate.json').exists()
    assert any(args[:2] == ['launchctl', 'bootout'] for args in env.calls)


def test_successful_import_commits_only_after_listeners_and_keeps_private_files(import_environment, monkeypatch):
    env = import_environment
    started = False
    def run(args, **kwargs):
        nonlocal started
        env.calls.append(args)
        if args[:2] == ['launchctl', 'bootstrap']:
            started = True
        return SimpleNamespace(returncode=1 if args[:2] == ['launchctl', 'print'] else 0,
                               stdout=b'', stderr=b'')
    monkeypatch.setattr(importer.subprocess, 'run', run)
    monkeypatch.setattr(importer, 'listening', lambda port: started)
    original_write = proxy_pool._write_state
    def write(state):
        assert started
        original_write(state)
    monkeypatch.setattr(proxy_pool, '_write_state', write)

    result = importer.import_profile(env.profile, env.core)
    stored = json.loads(env.state_path.read_text())
    assert result['imported'] == 2
    assert result['us_nodes'] == 1
    assert stored['proxies']['static'] == env.initial['proxies']['static']
    assert 'private-us-secret' not in env.state_path.read_text()
    assert env.directory.stat().st_mode & 0o777 == 0o700
    for path in [env.old_config, env.state_path, Path(result['launch_agent'])]:
        assert path.stat().st_mode & 0o777 == 0o600
