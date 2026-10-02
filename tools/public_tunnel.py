#!/usr/bin/env python3
"""启动公网隧道，并避开本机 TUN 的 fake-IP edge DNS 结果。

每次通过 Cloudflare DoH 获取两个 edge 区域的真实 IPv4。没有可用解析时退出，
由 launchd KeepAlive 重试；成功后 exec cloudflared，让 launchd 直接监督它。
不以公网 HTTP 探测结果杀进程，也不修改 Clash 或本地 SPARK 服务。

--edge 是 cloudflared 官方源码中的隐藏测试参数，不是稳定的公开接口：
https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/cmd.go
升级 cloudflared 后，应先运行本脚本 --check 验证参数与解析。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME = Path.home() / 'Library' / 'Application Support' / 'SPARK' / 'public-access'
REGIONS = ('region1.v2.argotunnel.com', 'region2.v2.argotunnel.com')
DOH_URL = 'https://cloudflare-dns.com/dns-query'
MAX_EDGES_PER_REGION = 4
MIN_TOTAL_EDGES = 4
MAX_DNS_RESPONSE_BYTES = 256 * 1024
LOG = logging.getLogger('public_tunnel')


class PublicTunnelError(RuntimeError):
    """可直接显示的短错误；不包含配置内容或凭据。"""


def clean_environment(environment=None):
    env = dict(os.environ if environment is None else environment)
    for key in list(env):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}:
            env.pop(key)
    # --token / TUNNEL_TOKEN 优先于 --token-file，不能让旧环境凭据覆盖指定文件。
    env.pop('TUNNEL_TOKEN', None)
    return env


def validate_cloudflared(binary, environment):
    """--help 只解析参数，不建立隧道；同时确认隐藏 edge 与 token-file 支持。"""
    command = [str(binary), 'tunnel', '--edge', '198.41.192.7:7844',
               '--protocol', 'http2', '--no-autoupdate', 'run', '--help']
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=10, env=environment, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PublicTunnelError('cloudflared 参数自检失败，请检查可执行文件。') from None
    if result.returncode or '--token-file' not in result.stdout:
        raise PublicTunnelError('cloudflared 不支持所需的 edge / token-file 参数。')


def resolve_region_edges(region, *, opener=None, timeout=10):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = DOH_URL + '?' + urllib.parse.urlencode({'name': region, 'type': 'A'})
    request = urllib.request.Request(url, headers={'Accept': 'application/dns-json'})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_DNS_RESPONSE_BYTES + 1)
        if len(raw) > MAX_DNS_RESPONSE_BYTES:
            raise ValueError('oversized DNS response')
        data = json.loads(raw)
    except (OSError, urllib.error.URLError, ValueError):
        raise PublicTunnelError(f'{region} 的 Cloudflare DoH 解析失败。') from None
    if not isinstance(data, dict) or data.get('Status') != 0:
        raise PublicTunnelError(f'{region} 的 Cloudflare DoH 返回 DNS 错误。')
    answers = data.get('Answer')
    if not isinstance(answers, list):
        answers = []
    addresses = []
    for answer in answers:
        if not isinstance(answer, dict) or answer.get('type') != 1:
            continue
        try:
            address = ipaddress.ip_address(answer.get('data', ''))
        except (ValueError, TypeError):
            continue
        # is_global 同时排除 fake-IP 198.18/15、私网、回环、文档与保留地址。
        if (not isinstance(address, ipaddress.IPv4Address) or not address.is_global
                or address.is_multicast):
            continue
        value = str(address)
        if value not in addresses:
            addresses.append(value)
        if len(addresses) == MAX_EDGES_PER_REGION:
            break
    if not addresses:
        raise PublicTunnelError(f'{region} 未返回可用的公网 IPv4，拒绝使用 fake-IP。')
    return addresses


def resolve_edges():
    regions = {region: resolve_region_edges(region) for region in REGIONS}
    # cloudflared 的 StaticEdge 按输入奇偶分配区域。传 region1[0], region2[0],
    # region1[1], region2[1]...，且保持两组数量相同，避免混合区域失去冗余。
    paired_count = min(len(regions[region]) for region in REGIONS)
    regions = {region: rows[:paired_count] for region, rows in regions.items()}
    addresses = [regions[region][index] for index in range(paired_count) for region in REGIONS]
    if len(set(addresses)) < MIN_TOTAL_EDGES:
        raise PublicTunnelError('两个 edge 区域不足 4 个可用公网 IPv4，等待重新解析。')
    if len(set(addresses)) != len(addresses):
        raise PublicTunnelError('两个 edge 区域解析地址重叠，等待重新解析。')
    return regions, addresses


def read_tunnel_token(gui_config):
    try:
        config = json.loads(Path(gui_config).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise PublicTunnelError('无法读取指定的 gui_config.json。') from None
    cloudflared = config.get('cloudflared') if isinstance(config, dict) else None
    token = cloudflared.get('token') if isinstance(cloudflared, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise PublicTunnelError('gui_config.json 缺少 cloudflared.token。')
    return validate_token(token)


def validate_token(token):
    token = token.strip()
    if not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise PublicTunnelError('cloudflared.token 格式无效。')
    return token


def read_token_file(token_file):
    try:
        token = Path(token_file).read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise PublicTunnelError('无法读取指定的隧道 token 文件。') from None
    return validate_token(token)


def atomic_write(path, text):
    """在同目录以 0600 原子替换，避免跟随旧 token 文件的符号链接或保留宽权限。"""
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_runtime(runtime_dir, token, regions, addresses, metrics):
    runtime_dir = Path(runtime_dir)
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_dir.chmod(0o700)
        token_file = runtime_dir / 'tunnel.token'
        atomic_write(token_file, token + '\n')
        atomic_write(runtime_dir / 'tunnel.pid', str(os.getpid()) + '\n')
        record = {'resolved_at': datetime.now(timezone.utc).isoformat(),
                  'pid': os.getpid(), 'regions': regions, 'addresses': addresses,
                  'protocol': 'http2', 'metrics': metrics}
        atomic_write(runtime_dir / 'edges.json', json.dumps(record, indent=2) + '\n')
    except OSError:
        raise PublicTunnelError('无法写入公网隧道运行目录。') from None
    return token_file


def cloudflared_command(binary, addresses, token_file, metrics):
    command = [str(binary), 'tunnel']
    for address in addresses:
        command.extend(['--edge', f'{address}:7844'])
    command.extend(['--protocol', 'http2', '--no-autoupdate', '--metrics', metrics,
                    '--loglevel', 'info', 'run', '--token-file', str(token_file)])
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument('--gui-config', type=Path, help='读取 cloudflared.token')
    credentials.add_argument('--token-file', type=Path, help='读取独立隧道的本地 token 文件')
    parser.add_argument('--runtime-dir', type=Path, default=DEFAULT_RUNTIME,
                        help='私有运行目录；不要放在网页静态服务目录内')
    parser.add_argument('--cloudflared', type=Path,
                        default=Path.home() / '.antigravity_tools' / 'bin' / 'cloudflared')
    parser.add_argument('--metrics', default='127.0.0.1:20244')
    parser.add_argument('--check', action='store_true',
                        help='验证参数和真实 edge 解析，不读取或写入 token、不启动隧道')
    args = parser.parse_args(argv)
    if not args.check and not (args.gui_config or args.token_file):
        parser.error('启动隧道需指定 --gui-config 或 --token-file；--check 无需凭据。')
    environment = clean_environment()
    try:
        validate_cloudflared(args.cloudflared, environment)
        token = None
        if not args.check:
            token = read_token_file(args.token_file) if args.token_file else read_tunnel_token(args.gui_config)
        regions, addresses = resolve_edges()
        if args.check:
            print(json.dumps({'regions': regions, 'addresses': addresses}, ensure_ascii=False))
            return 0
        token_file = prepare_runtime(args.runtime_dir, token, regions, addresses, args.metrics)
        command = cloudflared_command(args.cloudflared, addresses, token_file, args.metrics)
        LOG.info('使用两个区域的 %d 个真实 IPv4 启动公网隧道（http2）。', len(addresses))
        # 替换当前进程，PID 不变；退出/失败由 launchd KeepAlive 处理。
        try:
            os.execve(str(args.cloudflared), command, environment)
        except OSError:
            raise PublicTunnelError('无法执行 cloudflared，等待服务管理器重试。') from None
        return 0
    except PublicTunnelError as error:
        LOG.error('%s', error)
        return 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[public-tunnel] %(levelname)s %(message)s')
    sys.exit(main())
