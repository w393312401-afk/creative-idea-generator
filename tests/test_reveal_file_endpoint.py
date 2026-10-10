"""/api/reveal_file（在本机文件管理器里定位成片/媒体文件）的端点契约。

覆盖点：
- 本机来源 + 合法媒体路径 → 调 reveal_media_in_file_manager，回带绝对路径；
- 非本机来源 403：这个动作打开的是**跑服务端那台机器**的 Finder/资源管理器，
  远程访问时点按钮不该在别人的桌面上弹窗；
- 越界/非媒体/不存在的路径 400（真正的边界判定在 resolve_gallery_media_path，
  见 test_gallery_endpoints.py），且不会走到打开文件管理器那一步。
"""
import io
import json
from email.message import Message

import pytest

import server
import server_common


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def revealed(monkeypatch):
    """记录 reveal 调用，避免测试真的弹出 Finder 窗口。"""
    calls = []

    def _fake(raw, base_dir=None):
        calls.append(raw)
        if 'bad' in raw:
            raise ValueError('路径不在 outputs/ 内')
        return '/abs/' + raw.lstrip('/')

    monkeypatch.setattr(server, 'reveal_media_in_file_manager', _fake, raising=False)
    return calls


def _post(payload, peer='127.0.0.1'):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/reveal_file'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    h.client_address = (peer, 51234)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    server.SparkRequestHandler.do_POST(h)
    assert len(sent) == 1
    return sent[0]


def test_reveals_local_media_for_localhost(revealed):
    body, status = _post({'path': 'outputs/树屋项目/树屋_2x.mp4'})

    assert status == 200, body
    assert body == {'status': 'ok', 'path': '/abs/outputs/树屋项目/树屋_2x.mp4'}
    assert revealed == ['outputs/树屋项目/树屋_2x.mp4']


@pytest.mark.parametrize('peer', [
    '127.0.0.1',
    '::1',
    # 服务端监听的是 IPv6 双栈套接字：浏览器从 http://127.0.0.1 连过来，
    # 对端地址就是这个 IPv4-mapped 形式。按字面量比 '127.0.0.1' 会把本机自己挡在门外。
    '::ffff:127.0.0.1',
    '127.0.0.53',
])
def test_loopback_forms_all_count_as_local(revealed, peer):
    body, status = _post({'path': 'outputs/树屋项目/树屋_2x.mp4'}, peer=peer)

    assert status == 200, body


def test_accepts_url_field_as_alias(revealed):
    body, status = _post({'url': '/outputs/树屋项目/videos/vid_001.mp4?v=17'})

    assert status == 200, body
    assert revealed == ['/outputs/树屋项目/videos/vid_001.mp4?v=17']


def test_remote_client_is_refused(revealed, monkeypatch):
    # 本机网卡地址集合固定成一个已知值：否则跑测试的机器万一真的用着
    # 192.168.1.42 这个地址，这条用例会随环境变绿变红
    monkeypatch.setattr(server, '_own_host_ips', lambda: {'10.0.0.9'})
    body, status = _post({'path': 'outputs/树屋项目/树屋_2x.mp4'}, peer='192.168.1.42')

    assert status == 403
    assert body['status'] == 'error'
    # 拒绝必须发生在打开文件管理器之前
    assert revealed == []


def test_unsafe_path_is_rejected_with_reason(revealed):
    body, status = _post({'path': '../bad.mp4'})

    assert status == 400
    assert body['message'] == '路径不在 outputs/ 内'


def test_missing_target_is_rejected(revealed):
    body, status = _post({})

    assert status == 400
    assert body['status'] == 'error'
