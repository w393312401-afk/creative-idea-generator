"""Local and configured public editing dispatch rejects unrelated browser sites."""
import io
import json
import types
from email.message import Message

import pytest

import server


@pytest.fixture
def editor(monkeypatch):
    calls = []

    class EditError(Exception):
        def __init__(self, message, status=400, code='invalid'):
            super().__init__(message)
            self.status, self.code = status, code

    def start(**kwargs):
        calls.append(kwargs)
        return {'id': 'edit-1', 'status': 'queued'}

    module = types.SimpleNamespace(
        capabilities=lambda: {'available': True, 'message': '可以开始精剪'},
        start=start, list_jobs=lambda source: [{'id': 'edit-1', 'source': source}],
        cancel=lambda job_id: {'id': job_id, 'status': 'cancelled'},
        VideoEditError=EditError,
    )
    monkeypatch.setitem(server.sys.modules, 'codex_video_editor', module)
    monkeypatch.setattr(server, '_own_host_ips', lambda: {'192.168.1.5'})
    monkeypatch.setattr(server, 'SERVER_CONFIG', {})
    return module, calls


def request(path, method='GET', body=None, headers=None, peer='::ffff:127.0.0.1', real_gate=False):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.command = path, method
    handler.client_address = (peer, 1234)
    handler.server = types.SimpleNamespace(server_port=8085)
    handler.headers = Message()
    values = {'Host': '127.0.0.1:8085', 'Content-Type': 'application/json'}
    values.update(headers or {})
    encoded = json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
    values.setdefault('Content-Length', str(len(encoded)))
    for key, value in values.items():
        handler.headers[key] = value
    handler.rfile = io.BytesIO(encoded)
    if not real_gate:
        handler._gate = lambda: True
    replies = []
    handler._send_json = lambda payload, status=200: replies.append((status, payload))
    getattr(handler, 'do_' + method)()
    return handler, replies[-1]


def test_available_and_source_history(editor):
    assert request('/api/codex-video-editor/capabilities')[1] == (
        200, {'available': True, 'message': '可以开始精剪'})
    _, (status, payload) = request('/api/codex-video-editor/jobs?source=%2Foutputs%2Fdemo%2Fmerged.mp4')
    assert status == 200
    assert payload['jobs'][0]['source'] == '/outputs/demo/merged.mp4'


def test_same_origin_dispatch_and_cancel(editor):
    module, calls = editor
    body = {
        'source': '/outputs/海边/成片.mp4', 'mode': 'trim_speed',
        'notes': '保留展示', 'request_id': '123',
        'model': 'gpt-6.1-sol', 'reasoning_effort': 'high',
    }
    _, (status, payload) = request('/api/codex-video-editor/jobs', 'POST', body,
                                   {'Origin': 'http://127.0.0.1:8085', 'Sec-Fetch-Site': 'same-origin'})
    assert status == 200 and payload['job']['status'] == 'queued'
    assert calls == [body]
    assert request('/api/codex-video-editor/cancel', 'POST', {'id': 'edit-1'})[1][1]['job']['status'] == 'cancelled'


@pytest.fixture
def public_editor(editor, monkeypatch):
    monkeypatch.setattr(server, 'SERVER_CONFIG', {
        'codexEditorAllowedOrigins': ['https://ps.wushi-api.com'],
    })
    return editor


def public_headers(**overrides):
    values = {'Host': 'ps.wushi-api.com', 'Origin': 'https://ps.wushi-api.com',
              'Sec-Fetch-Site': 'same-origin', 'X-SPARK-Codex-Editor': '1'}
    values.update(overrides)
    return values


@pytest.mark.parametrize('host,origin', [
    ('ps.wushi-api.com', 'https://ps.wushi-api.com'),
    ('ps.wushi-api.com:443', 'https://ps.wushi-api.com'),
    ('ps.wushi-api.com', 'https://ps.wushi-api.com:443'),
    ('PS.WUSHI-API.COM', 'https://ps.wushi-api.com'),
])
def test_configured_public_origin_dispatches_and_cancels(public_editor, host, origin):
    headers = public_headers(Host=host, Origin=origin)
    body = {'source': '/outputs/demo/merged.mp4', 'request_id': 'public-request'}
    _, (status, payload) = request('/api/codex-video-editor/jobs', 'POST', body, headers)
    assert status == 200 and payload['job']['status'] == 'queued'
    assert public_editor[1][0]['source'] == body['source']
    assert request('/api/codex-video-editor/cancel', 'POST', {'id': 'edit-1'}, headers)[1][0] == 200


def test_public_capabilities_and_history_allow_fetch_without_origin(public_editor):
    headers = public_headers()
    headers.pop('Origin')
    assert request('/api/codex-video-editor/capabilities', headers=headers)[1][0] == 200
    response = request('/api/codex-video-editor/jobs?source=%2Foutputs%2Fdemo%2Fmerged.mp4', headers=headers)[1]
    assert response[0] == 200 and response[1]['jobs'][0]['source'] == '/outputs/demo/merged.mp4'
    assert public_editor[1] == []


@pytest.mark.parametrize('overrides', [
    {'Host': 'evil.example'}, {'Host': 'ps.wushi-api.com.evil.example'},
    {'Host': 'ps.wushi-api.com:8085'}, {'Host': 'ps.wushi-api.com:0'},
    {'Host': 'ps.wushi-api.com:'}, {'Host': 'ps.wushi-api.com?'},
    {'Host': 'user@ps.wushi-api.com'}, {'Host': 'ps.wushi-api.com/path'},
    {'Origin': 'https://evil.example'}, {'Origin': 'null'},
    {'Origin': 'http://ps.wushi-api.com'}, {'Origin': 'https://ps.wushi-api.com:8443'},
    {'Origin': 'https://ps.wushi-api.com/'}, {'Origin': 'https://ps.wushi-api.com?'},
    {'Origin': 'https://user@ps.wushi-api.com'},
    {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'},
    {'X-SPARK-Codex-Editor': ''}, {'X-SPARK-Codex-Editor': 'other'},
])
def test_public_dispatch_rejects_unrelated_or_malformed_requests(public_editor, overrides):
    assert request('/api/codex-video-editor/jobs', 'POST', {}, public_headers(**overrides))[1][0] == 403
    assert public_editor[1] == []


def test_public_mutation_requires_origin_and_local_proxy(public_editor):
    headers = public_headers()
    headers.pop('Origin')
    assert request('/api/codex-video-editor/jobs', 'POST', {}, headers)[1][0] == 403
    assert request('/api/codex-video-editor/jobs', 'POST', {}, public_headers(), peer='192.168.1.99')[1][0] == 403
    assert public_editor[1] == []


@pytest.mark.parametrize('configured', [
    [], 'https://ps.wushi-api.com', ['http://ps.wushi-api.com'],
    ['https://*.wushi-api.com'], ['https://ps.wushi-api.com/path'],
    ['https://ps.wushi-api.com:bad'], ['https://ps.wushi-api.com:0'],
    [None, {'origin': 'https://ps.wushi-api.com'}],
])
def test_unconfigured_or_invalid_public_origins_remain_denied(editor, monkeypatch, configured):
    monkeypatch.setattr(server, 'SERVER_CONFIG', {'codexEditorAllowedOrigins': configured})
    assert request('/api/codex-video-editor/capabilities', headers=public_headers())[1][0] == 403
    assert editor[1] == []


def test_public_requests_preserve_access_code_gate(public_editor, monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', 'test-editor-access')
    assert request('/api/codex-video-editor/jobs', 'POST', {}, public_headers(), real_gate=True)[1][0] == 401
    assert public_editor[1] == []
    headers = public_headers(**{'X-Access-Code': 'test-editor-access'})
    assert request('/api/codex-video-editor/jobs', 'POST', {}, headers, real_gate=True)[1][0] == 200


@pytest.mark.parametrize('headers,peer', [
    ({'Origin': 'https://evil.example'}, '127.0.0.1'),
    ({'Origin': 'null'}, '127.0.0.1'),
    ({'Host': 'evil.example:8085'}, '127.0.0.1'),
    ({'Host': 'user@localhost:8085'}, '127.0.0.1'),
    ({'Origin': 'http://localhost:9999'}, '127.0.0.1'),
    ({'Host': 'localhost:9999', 'Origin': 'http://localhost:9999'}, '127.0.0.1'),
    ({'Origin': 'https://127.0.0.1:8085'}, '127.0.0.1'),
    ({'Host': 'localhost:bad'}, '127.0.0.1'),
    ({'Sec-Fetch-Site': 'cross-site'}, '127.0.0.1'),
    ({'Sec-Fetch-Site': 'same-site'}, '127.0.0.1'),
    ({}, '192.168.1.99'),
])
def test_reject_foreign_origins_and_clients(editor, headers, peer):
    assert request('/api/codex-video-editor/jobs', 'POST', {}, headers, peer)[1][0] == 403
    assert editor[1] == []


def test_json_required_and_invalid_body(editor):
    assert request('/api/codex-video-editor/jobs', 'POST', {}, {'Content-Type': 'text/plain'})[1][0] == 415
    assert request('/api/codex-video-editor/jobs', 'POST', ['wrong'])[1][0] == 400
    assert editor[1] == []


def test_oversized_request_closes_connection_without_dispatch(editor):
    handler, response = request('/api/codex-video-editor/jobs', 'POST', {}, {'Content-Length': '20000'})
    assert response[0] == 413 and handler.close_connection
    assert editor[1] == []


@pytest.mark.parametrize('headers', [
    {'Content-Length': '-1'}, {'Content-Length': 'nope'},
    {'Content-Length': '+2'}, {'Transfer-Encoding': 'chunked'},
])
def test_invalid_length_is_rejected_before_reading(editor, headers):
    handler, response = request('/api/codex-video-editor/jobs', 'POST', {}, headers)
    assert response[0] == 400 and handler.close_connection
    assert editor[1] == []


def test_domain_error_preserves_actionable_status(editor):
    module, _ = editor

    def busy(**kwargs):
        raise module.VideoEditError('已有视频正在精剪', status=409, code='busy')

    module.start = busy
    assert request('/api/codex-video-editor/jobs', 'POST', {})[1] == (
        409, {'message': '已有视频正在精剪', 'code': 'busy'})


def test_editor_api_omits_cors_wildcard(editor, monkeypatch):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/codex-video-editor/capabilities'
    headers = {}
    handler.send_header = lambda name, value: headers.update({name: value})
    monkeypatch.setattr(server.SimpleHTTPRequestHandler, 'end_headers', lambda self: None)
    handler.end_headers()
    assert 'Access-Control-Allow-Origin' not in headers


def test_public_dispatch_preserves_existing_no_access_code_preference(public_editor, monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '')
    body = {'source': '/outputs/demo/merged.mp4', 'request_id': 'public-without-access-code'}
    _, (status, payload) = request('/api/codex-video-editor/jobs', 'POST', body,
                                   public_headers(), real_gate=True)
    assert status == 200 and payload['job']['status'] == 'queued'
    assert public_editor[1] == [{**body, 'mode': 'trim', 'notes': '',
                                 'model': None, 'reasoning_effort': None}]
