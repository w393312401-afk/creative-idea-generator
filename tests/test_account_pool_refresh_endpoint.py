"""「刷新积分」接口 (/api/account-pool/refresh) 的准入行为契约。

2026-07-27 的故障：这个接口原本对**任何**活跃的 FX 任务都直接回 409，包括另一个
积分探针。而探针当时没有墙钟预算，一个进不去工作台的账号能占着浏览器好几分钟——
于是所有账号的「刷新积分」连着几十次 409，功能整体看上去就是坏的（server.log 里
连续 20+ 条 409，最后只能杀进程才恢复）。

现在的契约：
- 生成类任务占着浏览器 → 409，并说清是谁、跑了多久（探测确实拿不到浏览器）。
- 另一个探针占着浏览器 → 放行去排队（探针自带预算，等待是有界的）。
- 同一个账号的探测已在跑 → 409，别排重复的一轮。
- 队列处于 paused 模式 → 409，提示先恢复处理（排进去也永远不会被放行）。
- 探测没跑起来（浏览器忙）→ 409 而不是 422：账号本身没毛病，不该报成探测失败。
"""
from email.message import Message
import io
import json

import server


def _handler(user_id='u1'):
    # do_POST 会在分发前统一重读一次请求体，所以这里得给它真的 headers + rfile，
    # 光塞 _body_bytes 会被覆盖成空。
    payload = json.dumps({'user_id': user_id}).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/account-pool/refresh'
    h.headers = Message()
    h.headers['Content-Length'] = str(len(payload))
    h.rfile = io.BytesIO(payload)
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


class _Control:
    def __init__(self, active_list=None, processing_paused=False):
        self._snapshot = {
            'active_list': list(active_list or []),
            'active': (active_list or [None])[0],
            'processing_paused': processing_paused,
        }

    def snapshot(self):
        return self._snapshot


class _Pool:
    def __init__(self, entry):
        self.entry = entry
        self.calls = []

    def refresh_credit(self, user_id, force=False):
        self.calls.append((user_id, force))
        return self.entry


def _ok_entry(credit=1050):
    return {'user_id': 'u1', 'credit': credit, 'last_probe_status': 'ok'}


class TestRefreshAdmission:
    def test_generation_task_holding_the_browser_returns_409_with_context(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([
            {'task_id': 'compose_42', 'kind': 'auto', 'elapsed_seconds': 137.4},
        ]))
        pool = _Pool(_ok_entry())
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 409
        assert body['code'] == 'FX_BUSY'
        assert 'compose_42' in body['message'] and '137s' in body['message']
        assert pool.calls == []          # 没白跑一趟探测

    def test_another_probe_does_not_block_refresh(self, monkeypatch):
        """探针自带预算，排在它后面是有界等待——不能再一律 409。"""
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([
            {'task_id': 'credit_probe_other', 'kind': 'credit_probe', 'elapsed_seconds': 12.0},
        ]))
        pool = _Pool(_ok_entry())
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 200
        assert body['account']['credit'] == 1050
        assert pool.calls == [('u1', True)]

    def test_same_account_probe_already_running_is_not_queued_twice(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([
            {'task_id': 'credit_probe_u1', 'kind': 'credit_probe', 'elapsed_seconds': 3.0},
        ]))
        pool = _Pool(_ok_entry())
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 409
        assert body['code'] == 'FX_PROBE_RUNNING'
        assert pool.calls == []

    def test_paused_queue_is_reported_as_paused_not_as_a_busy_browser(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([], processing_paused=True))
        pool = _Pool(_ok_entry())
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 409
        assert body['code'] == 'FX_PAUSED'
        assert '暂停' in body['message']
        assert pool.calls == []

    def test_idle_browser_runs_the_probe(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([]))
        pool = _Pool(_ok_entry(880))
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert body['account']['credit'] == 880


class TestRefreshOutcome:
    def test_probe_blocked_by_a_busy_browser_is_409_not_a_probe_failure(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([]))
        pool = _Pool({'user_id': 'u1', 'credit': 640,
                      'last_probe_status': 'blocked',
                      'last_probe_error': '浏览器被其它 FX 任务占用，排队等待 100s 仍未拿到浏览器'})
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 409
        assert body['code'] == 'FX_BUSY'
        assert '排队等待' in body['message']

    def test_real_probe_failure_still_returns_422(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([]))
        pool = _Pool({'user_id': 'u1', 'credit': None,
                      'last_probe_status': 'failed',
                      'last_probe_error': 'Flow 仍停留在产品介绍页，未能进入工作台'})
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 422
        assert body['code'] == 'CREDIT_PROBE_FAILED'
        assert '产品介绍页' in body['message']

    def test_unknown_account_returns_404(self, monkeypatch):
        monkeypatch.setattr(server, 'FX_CONTROL', _Control([]))
        monkeypatch.setattr(server, '_get_account_pool', lambda: _Pool(None))

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 404
