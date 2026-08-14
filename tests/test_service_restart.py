"""测试后端服务重启接口（POST /api/restart）及自动重载逻辑。"""
import io
import json
import time
from email.message import Message

import pytest

import server
import server_common as sc


def _post(path, payload, headers_dict=None):
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.headers = Message()
    h.headers['Content-Length'] = str(len(raw))
    if headers_dict:
        for k, v in headers_dict.items():
            h.headers[k] = str(v)
    h.rfile = io.BytesIO(raw)
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def test_restart_endpoint_calls_restart_process(monkeypatch):
    restarted = []
    monkeypatch.setattr(server, 'restart_server_process', lambda: restarted.append(True))
    monkeypatch.setattr(server, 'access_ok', lambda h: True)

    h, sent = _post('/api/restart', {})
    server.SparkRequestHandler.do_POST(h)

    assert len(sent) == 1
    assert sent[0][1] == 200
    assert sent[0][0].get('status') == 'ok'
    # Wait briefly for thread to fire
    time.sleep(0.1)
    assert len(restarted) == 1


def test_restart_endpoint_enforces_access_code(monkeypatch):
    monkeypatch.setattr(server, 'access_ok', lambda h: False)

    h, sent = _post('/api/restart', {})
    server.SparkRequestHandler.do_POST(h)

    assert len(sent) == 1
    assert sent[0][1] == 401
    assert '访问码' in sent[0][0].get('error', '')
