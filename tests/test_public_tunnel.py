import io
import json
import os
from pathlib import Path
import stat
import subprocess
import urllib.error

import pytest

from tools import public_tunnel as tunnel


REGION_ADDRESSES = {
    tunnel.REGIONS[0]: ['198.41.192.7', '198.41.192.27'],
    tunnel.REGIONS[1]: ['198.41.200.13', '198.41.200.43'],
}
ADDRESSES = [REGION_ADDRESSES[region][index] for index in range(2) for region in tunnel.REGIONS]


class FakeDoH:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return io.BytesIO(json.dumps(self.payload).encode())


def test_doh_filters_fake_private_ipv6_duplicates_and_non_a_answers():
    values = ['198.18.0.16', '127.0.0.1', '10.1.2.3', '192.168.1.2',
              '203.0.113.4', '224.0.0.1', '::1', '2606:4700::1111', 'not-an-ip',
              '198.41.192.7', '198.41.192.7', '198.41.192.27']
    opener = FakeDoH({'Status': 0, 'Answer': [
        *[{'type': 1, 'data': value} for value in values],
        {'type': 5, 'data': '198.41.192.37'},
    ]})
    assert tunnel.resolve_region_edges(tunnel.REGIONS[0], opener=opener) == REGION_ADDRESSES[tunnel.REGIONS[0]]
    request, timeout = opener.requests[0]
    assert request.full_url.startswith('https://cloudflare-dns.com/dns-query?')
    assert 'name=region1.v2.argotunnel.com&type=A' in request.full_url
    assert request.get_header('Accept') == 'application/dns-json'
    assert timeout == 10


def test_doh_does_not_inherit_environment_proxy(monkeypatch):
    handlers = []
    opener = FakeDoH({'Status': 0, 'Answer': [{'type': 1, 'data': '198.41.192.7'}]})

    def fake_build(handler):
        handlers.append(handler)
        return opener

    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setattr(tunnel.urllib.request, 'build_opener', fake_build)
    tunnel.resolve_region_edges(tunnel.REGIONS[0])
    assert handlers[0].proxies == {}


def test_resolution_uses_both_regions_and_requires_four_distinct_edges(monkeypatch):
    seen = []

    def resolve(region):
        seen.append(region)
        return REGION_ADDRESSES[region]

    monkeypatch.setattr(tunnel, 'resolve_region_edges', resolve)
    assert tunnel.resolve_edges() == (REGION_ADDRESSES, ADDRESSES)
    assert seen == list(tunnel.REGIONS)
    monkeypatch.setattr(tunnel, 'resolve_region_edges', lambda region: ['198.41.192.7'])
    with pytest.raises(tunnel.PublicTunnelError, match='不足 4'):
        tunnel.resolve_edges()


def test_four_edges_per_region_interleave_for_static_edge_parity(monkeypatch):
    regions = {
        tunnel.REGIONS[0]: ['198.41.192.7', '198.41.192.27', '198.41.192.37', '198.41.192.47'],
        tunnel.REGIONS[1]: ['198.41.200.13', '198.41.200.43', '198.41.200.53', '198.41.200.63'],
    }
    monkeypatch.setattr(tunnel, 'resolve_region_edges', lambda region: regions[region])
    selected, addresses = tunnel.resolve_edges()
    assert addresses[::2] == regions[tunnel.REGIONS[0]]
    assert addresses[1::2] == regions[tunnel.REGIONS[1]]
    assert selected == regions
    regions[tunnel.REGIONS[1]] = regions[tunnel.REGIONS[1]][:2]
    selected, addresses = tunnel.resolve_edges()
    assert len(addresses) == 4
    assert addresses[::2] == regions[tunnel.REGIONS[0]][:2]


@pytest.mark.parametrize('payload', [
    {'Status': 2}, {'Status': 0, 'Answer': [{'type': 1, 'data': '198.18.1.176'}]},
    {'Status': 0, 'Answer': None}, [],
])
def test_unusable_dns_fails_without_system_dns_fallback(payload):
    with pytest.raises(tunnel.PublicTunnelError):
        tunnel.resolve_region_edges(tunnel.REGIONS[0], opener=FakeDoH(payload))


def test_network_error_does_not_echo_exception_content():
    class FailedDoH:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError('sensitive upstream diagnostics')

    with pytest.raises(tunnel.PublicTunnelError) as error:
        tunnel.resolve_region_edges(tunnel.REGIONS[0], opener=FailedDoH())
    assert 'sensitive' not in str(error.value)


def test_cloudflared_help_verifies_edge_and_token_file_without_starting(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='--token-file value', stderr='')

    monkeypatch.setattr(tunnel.subprocess, 'run', fake_run)
    tunnel.validate_cloudflared(Path('/test/cloudflared'), {'PATH': '/bin'})
    command, kwargs = calls[0]
    assert command[-1] == '--help'
    assert '--edge' in command and '--protocol' in command
    assert kwargs['env'] == {'PATH': '/bin'}
    monkeypatch.setattr(tunnel.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout='old version', stderr=''))
    with pytest.raises(tunnel.PublicTunnelError, match='不支持'):
        tunnel.validate_cloudflared(Path('/test/cloudflared'), {})


def test_only_exact_gui_cloudflared_token_key_is_read(tmp_path):
    config = tmp_path / 'gui_config.json'
    config.write_text(json.dumps({'token': 'wrong-key', 'cloudflared': {'token': 'correct-key'}}))
    assert tunnel.read_tunnel_token(config) == 'correct-key'
    config.write_text(json.dumps({'token': 'wrong-key'}))
    with pytest.raises(tunnel.PublicTunnelError, match='缺少 cloudflared.token'):
        tunnel.read_tunnel_token(config)


def test_external_token_file_and_mutually_exclusive_credentials(tmp_path, monkeypatch):
    source = tmp_path / 'external.token'
    source.write_text('independent-tunnel-token\n')
    assert tunnel.read_token_file(source) == 'independent-tunnel-token'
    source.write_text('invalid\nembedded-newline')
    with pytest.raises(tunnel.PublicTunnelError, match='格式无效'):
        tunnel.read_token_file(source)
    with pytest.raises(SystemExit):
        tunnel.main(['--gui-config', str(tmp_path / 'gui.json'), '--token-file', str(source)])


@pytest.mark.parametrize('credential_source', ['gui', 'file'])
def test_launch_exec_keeps_pid_private_token_and_numeric_edges(
        tmp_path, monkeypatch, capsys, caplog, credential_source):
    config = tmp_path / 'gui_config.json'
    secret = 'a-test-token-that-must-never-be-logged'
    config.write_text(json.dumps({'cloudflared': {'token': secret}}))
    source_token_file = tmp_path / 'external.token'
    source_token_file.write_text(secret + '\n')
    credentials = (['--gui-config', str(config)] if credential_source == 'gui'
                   else ['--token-file', str(source_token_file)])
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    token_file = runtime / 'tunnel.token'
    token_file.write_text('old-token')
    token_file.chmod(0o644)
    monkeypatch.setattr(tunnel, 'validate_cloudflared', lambda *args: None)
    monkeypatch.setattr(tunnel, 'resolve_edges', lambda: (REGION_ADDRESSES, ADDRESSES))
    for key in ('HTTP_PROXY', 'https_proxy', 'ALL_PROXY', 'NO_PROXY'):
        monkeypatch.setenv(key, 'a-proxy')
    monkeypatch.setenv('TUNNEL_TOKEN', 'environment-token-must-not-override-file')
    monkeypatch.setenv('KEEP_THIS_VARIABLE', 'preserved')
    launched = []
    monkeypatch.setattr(tunnel.os, 'execve', lambda *args: launched.append(args))
    assert tunnel.main([*credentials, '--runtime-dir', str(runtime),
                        '--cloudflared', '/test/cloudflared']) == 0
    binary, command, environment = launched[0]
    assert binary == '/test/cloudflared'
    assert command.count('--edge') == 4
    assert [command[i + 1] for i, value in enumerate(command) if value == '--edge'] == [f'{ip}:7844' for ip in ADDRESSES]
    assert command[-2:] == ['--token-file', str(token_file)]
    assert secret not in command
    assert not any(key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'} for key in environment)
    assert 'TUNNEL_TOKEN' not in environment
    assert environment['KEEP_THIS_VARIABLE'] == 'preserved'
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert token_file.read_text() == secret + '\n'
    record = json.loads((runtime / 'edges.json').read_text())
    assert record['pid'] == os.getpid()
    assert record['addresses'] == ADDRESSES
    assert int((runtime / 'tunnel.pid').read_text()) == os.getpid()
    assert secret not in (runtime / 'edges.json').read_text()
    captured = capsys.readouterr()
    assert secret not in caplog.text + captured.out + captured.err
    assert sorted(path.name for path in runtime.iterdir()) == ['edges.json', 'tunnel.pid', 'tunnel.token']


def test_check_does_not_read_or_write_credentials_or_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tunnel, 'validate_cloudflared', lambda *args: None)
    monkeypatch.setattr(tunnel, 'resolve_edges', lambda: (REGION_ADDRESSES, ADDRESSES))
    monkeypatch.setattr(tunnel, 'read_tunnel_token', lambda *args: pytest.fail('check read credentials'))
    monkeypatch.setattr(tunnel.os, 'execve', lambda *args: pytest.fail('check started a connector'))
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(tunnel, 'read_token_file', lambda *args: pytest.fail('check read credentials'))
    assert tunnel.main(['--runtime-dir', str(runtime), '--check']) == 0
    assert json.loads(capsys.readouterr().out)['addresses'] == ADDRESSES
    assert not runtime.exists()


def test_dns_failure_exits_without_replacing_token_or_starting(tmp_path, monkeypatch, caplog):
    config = tmp_path / 'gui_config.json'
    secret = 'test-secret-do-not-print'
    config.write_text(json.dumps({'cloudflared': {'token': secret}}))
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    token_file = runtime / 'tunnel.token'
    token_file.write_text('previous-token')
    monkeypatch.setattr(tunnel, 'validate_cloudflared', lambda *args: None)

    def failed_dns():
        raise tunnel.PublicTunnelError('Cloudflare DoH 解析失败。')

    monkeypatch.setattr(tunnel, 'resolve_edges', failed_dns)
    monkeypatch.setattr(tunnel.os, 'execve', lambda *args: pytest.fail('DNS failure started connector'))
    assert tunnel.main(['--gui-config', str(config), '--runtime-dir', str(runtime)]) == 1
    assert token_file.read_text() == 'previous-token'
    assert 'DoH 解析失败' in caplog.text
    assert secret not in caplog.text
