"""/api/beat-pack/*：路由、门禁、JSON 约束、不开放跨站，以及与重启保护的联动。"""
import io
import json
import time
import types
from email.message import Message

import pytest

import beat_pack
import beat_pack_fixtures as fx
import server
import server_common
from beat_pack import jobs


def request(path, method='GET', body=None, headers=None, real_gate=False, raw=None):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.command = path, method
    handler.client_address = ('127.0.0.1', 1234)
    handler.server = types.SimpleNamespace(server_port=8085)
    handler.headers = Message()
    values = {'Host': '127.0.0.1:8085', 'Content-Type': 'application/json'}
    values.update(headers or {})
    encoded = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else b'')
    values.setdefault('Content-Length', str(len(encoded)))
    for key, value in values.items():
        handler.headers[key] = value
    handler.rfile = io.BytesIO(encoded)
    if not real_gate:
        handler._gate = lambda **kwargs: True
    replies = []
    handler._send_json = lambda payload, status=200: replies.append((status, payload))
    getattr(handler, 'do_' + method)()
    return replies[-1]


@pytest.fixture
def stub(monkeypatch):
    """替身 beat_pack 模块：只验证路由与参数传递，不起后台线程。"""
    calls = []

    class Error(Exception):
        def __init__(self, message, code='invalid_request', status=400):
            super().__init__(message)
            self.code, self.status = code, status

    def record(name, result):
        def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result(*args, **kwargs) if callable(result) else result
        return fn

    module = types.SimpleNamespace(
        BeatPackError=Error, set_concurrency=lambda value: calls.append(('set_concurrency', (value,), {})),
        capabilities=record('capabilities', {'available': True, 'default_model': 'claude-opus-5-5'}),
        list_jobs=record('list_jobs', [{'id': 'bp_1'}]),
        get_job=record('get_job', lambda job_id: {'id': job_id, 'status': 'running'}),
        prompt_text=record('prompt_text', lambda job_id, name='完整提示词.txt': f'TEXT of {name}'),
        start=record('start', lambda body, config, request_id='': {'id': 'bp_1', 'status': 'queued'}),
        cancel=record('cancel', lambda job_id: {'id': job_id, 'status': 'cancelled'}),
        resume=record('resume', lambda job_id, config: {'id': job_id, 'status': 'queued'}),
        import_job=record('import_job', lambda job_id, title=None: {'id': job_id, 'imported': title}),
        running_job_ids=lambda: [],
    )
    monkeypatch.setitem(server.sys.modules, 'beat_pack', module)
    monkeypatch.setattr(server, 'SERVER_CONFIG', {})
    monkeypatch.setattr(server, 'ACCESS_CODE', '')
    # effective_config 读的是 server_common 里的模式与服务端配置：固定成“非托管、无服务端配置”，不受开发机真实配置影响。
    monkeypatch.setattr(server_common, 'SERVER_CONFIG', {})
    monkeypatch.setattr(server_common, 'SERVER_MANAGED', False)
    module.calls = calls
    return module


def names(stub):
    return [c[0] for c in stub.calls if c[0] != 'set_concurrency']


def test_read_endpoints(stub):
    assert request('/api/beat-pack/capabilities') == (200, {'available': True, 'default_model': 'claude-opus-5-5'})
    assert request('/api/beat-pack/jobs') == (200, {'jobs': [{'id': 'bp_1'}]})
    assert request('/api/beat-pack/job?id=bp_9') == (200, {'job': {'id': 'bp_9', 'status': 'running'}})
    assert request('/api/beat-pack/prompt?id=bp_9') == (200, {'name': '完整提示词.txt', 'text': 'TEXT of 完整提示词.txt'})
    assert request('/api/beat-pack/prompt?id=bp_9&name=%E5%9B%BE%E7%89%87%E6%8F%90%E7%A4%BA%E8%AF%8D.txt')[1]['text'] == 'TEXT of 图片提示词.txt'


def test_start_passes_the_body_the_client_config_and_the_request_id(stub):
    body = {'theme': '巨型干松果改成雪林住所', 'segments': 28, 'config': {'claudeApiKey': 'k-from-browser', 'model': 'claude-sonnet-5-5'}, 'request_id': 'r1'}
    status, payload = request('/api/beat-pack/jobs', 'POST', body)
    assert (status, payload) == (200, {'job': {'id': 'bp_1', 'status': 'queued'}})
    name, args, kwargs = next(c for c in stub.calls if c[0] == 'start')
    assert args[0]['theme'] == body['theme'] and kwargs == {'request_id': 'r1'}
    assert args[1]['claudeApiKey'] == 'k-from-browser' and args[1]['model'] == 'claude-sonnet-5-5'      # 经 effective_config 合并


def test_managed_mode_uses_the_servers_claude_gateway_and_ignores_the_browsers(stub, monkeypatch):
    monkeypatch.setattr(server_common, 'SERVER_MANAGED', True)
    monkeypatch.setattr(server_common, 'SERVER_CONFIG', {'apiKey': 'main', 'claudeBaseUrl': 'http://srv.test/v1', 'claudeApiKey': 'srv-claude'})
    request('/api/beat-pack/jobs', 'POST', {'theme': '巨型干松果改成雪林住所',
                                            'config': {'claudeBaseUrl': 'http://evil.test/v1', 'claudeApiKey': 'stolen'}})
    config = next(c for c in stub.calls if c[0] == 'start')[1][1]
    assert server_common.resolve_gateway('claude-opus-5-5', config) == ('http://srv.test/v1', 'srv-claude')


def test_capabilities_can_be_asked_with_the_browsers_config(stub):
    request('/api/beat-pack/capabilities', 'POST', {'config': {'claudeBaseUrl': 'http://x/v1'}})
    name, args, _ = next(c for c in stub.calls if c[0] == 'capabilities')
    assert args[0]['claudeBaseUrl'] == 'http://x/v1'


def test_cancel_resume_and_import(stub):
    assert request('/api/beat-pack/cancel', 'POST', {'id': 'bp_1'})[1]['job']['status'] == 'cancelled'
    assert request('/api/beat-pack/resume', 'POST', {'id': 'bp_1', 'config': {'apiKey': 'k'}})[1]['job']['status'] == 'queued'
    resume = next(c for c in stub.calls if c[0] == 'resume')
    assert resume[1][0] == 'bp_1' and resume[1][1]['apiKey'] == 'k'
    assert request('/api/beat-pack/import', 'POST', {'id': 'bp_1', 'title': '新标题'})[1]['job'] == {'id': 'bp_1', 'imported': '新标题'}


def test_concurrency_setting_comes_from_the_server_config(stub, monkeypatch):
    monkeypatch.setattr(server, 'SERVER_CONFIG', {'beatPackConcurrency': 2})
    request('/api/beat-pack/jobs')
    assert ('set_concurrency', (2,), {}) in stub.calls


@pytest.mark.parametrize('method, path', [('GET', '/api/beat-pack/nope'), ('POST', '/api/beat-pack/nope'),
                                          ('POST', '/api/beat-pack/job'), ('GET', '/api/beat-pack/cancel')])
def test_unknown_routes_and_wrong_methods_are_404(stub, method, path):
    status, payload = request(path, method, {} if method == 'POST' else None)
    assert status == 404 and names(stub) == []


def test_writes_must_be_json(stub):
    assert request('/api/beat-pack/jobs', 'POST', {'theme': 'x'}, {'Content-Type': 'text/plain'})[0] == 415
    assert request('/api/beat-pack/jobs', 'POST', {'theme': 'x'}, {'Content-Type': 'application/x-www-form-urlencoded'})[0] == 415
    assert request('/api/beat-pack/jobs', 'POST', {'theme': 'x'}, {'Content-Type': 'application/json; charset=utf-8'})[0] == 200
    assert names(stub) == ['start']


def test_malformed_bodies_are_400(stub):
    assert request('/api/beat-pack/jobs', 'POST', raw=b'[1, 2]')[0] == 400
    assert request('/api/beat-pack/jobs', 'POST', raw=b'{not json')[0] == 400
    assert names(stub) == []


def test_oversized_bodies_are_413_before_anything_is_read(stub):
    status, payload = request('/api/beat-pack/jobs', 'POST', {'theme': 'x'}, {'Content-Length': '300000'})
    assert status == 413 and '过长' in payload['message'] and names(stub) == []


def test_errors_map_to_their_status_and_code_without_leaking_internals(stub):
    def refuse(body, config, request_id=''):
        raise stub.BeatPackError('请填写创意主题', 'invalid_request', 400)

    stub.start = refuse
    assert request('/api/beat-pack/jobs', 'POST', {}) == (400, {'message': '请填写创意主题', 'code': 'invalid_request'})
    stub.cancel = lambda job_id: (_ for _ in ()).throw(stub.BeatPackError('找不到这个任务', 'not_found', 404))
    assert request('/api/beat-pack/cancel', 'POST', {'id': 'bp_1'}) == (404, {'message': '找不到这个任务', 'code': 'not_found'})
    stub.get_job = lambda job_id: (_ for _ in ()).throw(RuntimeError('/Users/fly/secret/path exploded'))
    status, payload = request('/api/beat-pack/job?id=x')
    assert status == 500 and 'secret' not in json.dumps(payload) and 'exploded' not in json.dumps(payload)
    stub.get_job = lambda job_id: (_ for _ in ()).throw(ValueError('bad'))
    assert request('/api/beat-pack/job?id=x')[0] == 400


def test_the_access_code_gates_every_endpoint(stub, monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', 'letmein')
    for method, path in (('GET', '/api/beat-pack/capabilities'), ('GET', '/api/beat-pack/jobs'), ('POST', '/api/beat-pack/jobs'),
                         ('POST', '/api/beat-pack/cancel'), ('POST', '/api/beat-pack/import')):
        assert request(path, method, {} if method == 'POST' else None, real_gate=True)[0] == 401, path
    assert names(stub) == []
    assert request('/api/beat-pack/jobs', headers={'X-Access-Code': 'letmein'}, real_gate=True)[0] == 200


def test_starting_and_resuming_are_rate_limited_but_cancelling_is_not(stub, monkeypatch):
    seen = []
    monkeypatch.setattr(server, 'rate_ok', lambda ip, action='default': seen.append(action) or False)
    monkeypatch.setattr(server, 'RATE_LIMIT_ENABLED', True)
    assert request('/api/beat-pack/jobs', 'POST', {}, real_gate=True)[0] == 429
    assert request('/api/beat-pack/resume', 'POST', {'id': 'bp_1'}, real_gate=True)[0] == 429
    assert request('/api/beat-pack/cancel', 'POST', {'id': 'bp_1'}, real_gate=True)[0] == 200
    assert request('/api/beat-pack/jobs', real_gate=True)[0] == 200
    assert set(seen) == {'beat_pack'} and names(stub) == ['cancel', 'list_jobs']


def test_the_api_omits_the_cors_wildcard_like_the_other_paid_local_api(stub, monkeypatch):
    for path, expected in (('/api/beat-pack/jobs', False), ('/api/codex-video-editor/jobs', False), ('/api/tasks', True)):
        handler = object.__new__(server.SparkRequestHandler)
        handler.path = path
        headers = {}
        handler.send_header = lambda name, value, headers=headers: headers.update({name: value})
        monkeypatch.setattr(server.SimpleHTTPRequestHandler, 'end_headers', lambda self: None)
        handler.end_headers()
        assert ('Access-Control-Allow-Origin' in headers) is expected, path


def test_running_generation_blocks_a_restart_until_forced(stub, monkeypatch):
    restarted = []
    monkeypatch.setattr(server, 'restart_server_process', lambda: restarted.append(True))
    stub.running_job_ids = lambda: ['bp_20261009_010203_abcdef']
    status, payload = request('/api/restart', 'POST', {})
    assert status == 409 and payload['failure_code'] == 'TASKS_RUNNING'
    assert payload['running_tasks'] == ['beat_pack:bp_20261009_010203_abcdef'] and restarted == []
    status, payload = request('/api/restart', 'POST', {'force': True})
    assert status == 200 and payload['status'] == 'ok'
    time.sleep(0.2)
    assert restarted == [True]


def test_an_idle_beat_pack_does_not_block_a_restart(stub, monkeypatch):
    monkeypatch.setattr(server, 'restart_server_process', lambda: None)
    assert request('/api/restart', 'POST', {})[0] == 200


def test_the_busy_probe_never_blocks_on_a_broken_module(monkeypatch):
    broken = types.SimpleNamespace(running_job_ids=lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setitem(server.sys.modules, 'beat_pack', broken)
    assert server._beat_pack_busy() == []
    monkeypatch.setitem(server.sys.modules, 'beat_pack', types.SimpleNamespace(running_job_ids=lambda: ['bp_a', 'bp_b']))
    assert server._beat_pack_busy() == ['beat_pack:bp_a', 'beat_pack:bp_b']


def test_end_to_end_through_the_http_handlers(tmp_path, monkeypatch):
    """真模块 + 假模型：创建 → 轮询 → 取提示词 → 已导入项目工作台。"""
    monkeypatch.setattr(jobs, 'BASE_DIR', tmp_path / 'beat_packs')
    monkeypatch.setattr(jobs, '_RECONCILED', False)
    monkeypatch.setattr(jobs, '_JOBS', {})
    model = fx.FakeModel()
    monkeypatch.setattr(jobs, '_make_chat', lambda live, name, on_receive: model)
    monkeypatch.setattr(server, 'SERVER_CONFIG', {})
    monkeypatch.setattr(server, 'ACCESS_CODE', '')
    monkeypatch.setattr(server_common, 'SERVER_CONFIG', {})
    monkeypatch.setattr(server_common, 'SERVER_MANAGED', False)
    status, payload = request('/api/beat-pack/jobs', 'POST', {'theme': '巨型干松果改成雪林里的完整住所', 'segments': 28,
                                                              'config': {'model': 'claude-sonnet-5-5'}})
    assert status == 200 and payload['job']['model'] == 'claude-sonnet-5-5'
    job_id = payload['job']['id']
    deadline = time.time() + 30
    while time.time() < deadline:
        status, payload = request(f'/api/beat-pack/job?id={job_id}')
        if payload['job']['status'] in ('completed', 'failed'):
            break
        time.sleep(0.02)
    job = payload['job']
    assert job['status'] == 'completed' and job['result']['import']['status'] == 'imported'
    status, payload = request(f'/api/beat-pack/prompt?id={job_id}')
    assert status == 200 and payload['text'].count('\n图片 ') >= 28 and '[BRIDGE]' in payload['text']
    assert request(f'/api/beat-pack/prompt?id={job_id}&name=beat_package.json')[0] == 400
    status, payload = request('/api/beat-pack/jobs')
    assert [j['id'] for j in payload['jobs']] == [job_id]
    status, payload = request('/api/beat-pack/import', 'POST', {'id': job_id})
    assert status == 409 and payload['code'] == 'duplicate_title'
    status, payload = request('/api/beat-pack/job?id=../../etc/passwd')
    assert status == 400
