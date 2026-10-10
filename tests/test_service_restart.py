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


def test_restart_endpoint_refuses_while_tasks_run_unless_forced(monkeypatch):
    restarted = []
    monkeypatch.setattr(server, 'restart_server_process', lambda: restarted.append(True))
    monkeypatch.setattr(server, 'access_ok', lambda h: True)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {'videos_x': {'status': 'running'},
                                                 'videos_done': {'status': 'completed'}})

    h, sent = _post('/api/restart', {})
    server.SparkRequestHandler.do_POST(h)
    assert sent[0][1] == 409
    assert sent[0][0]['running_tasks'] == ['videos_x']

    h, sent = _post('/api/restart', {'force': True})
    server.SparkRequestHandler.do_POST(h)
    assert sent[0][1] == 200
    time.sleep(0.1)
    assert restarted == [True]


def test_restart_endpoint_enforces_access_code(monkeypatch):
    monkeypatch.setattr(server, 'access_ok', lambda h: False)

    h, sent = _post('/api/restart', {})
    server.SparkRequestHandler.do_POST(h)

    assert len(sent) == 1
    assert sent[0][1] == 401
    assert '访问码' in sent[0][0].get('error', '')


def test_auto_reload_reads_current_config_and_restarts_for_stale_code(monkeypatch, tmp_path):
    config_path = tmp_path / 'server_config.json'
    config_path.write_text('{"autoReload": false}', encoding='utf-8')
    monkeypatch.setattr(sc, 'SERVER_CONFIG_FILE', str(config_path))
    monkeypatch.setattr(server, '_AUTO_RELOAD_TRIGGERED', False)
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server, 'code_staleness_report',
                        lambda: {'stale': True, 'stale_files': ['integrations/google_fx/ui_selectors.py']})

    sleep_calls = []
    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # The first poll sees autoReload=false; change the file before the next.
        if sleep_calls.count(1.5) == 2:
            config_path.write_text('{"autoReload": true}', encoding='utf-8')

    monkeypatch.setattr(server.time, 'sleep', fake_sleep)
    restarted = []
    monkeypatch.setattr(server, 'restart_server_process', lambda: restarted.append(True))

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, 'Thread', InlineThread)
    server._start_auto_reload_watcher()

    assert sleep_calls[:3] == [3.0, 1.5, 1.5]
    assert restarted == [True]
    assert server._AUTO_RELOAD_TRIGGERED is True
