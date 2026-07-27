"""号池"一键添加全部 AdsPower 环境"接口 (/api/account-pool/import-all) 的行为契约。

覆盖点：
- 正常导入：委托给 AdsPower 侧 AccountPool.import_adspower_profiles()，回写新增/
  跳过/环境总数三个计数，并顺带把导入后的完整账号列表带回去（前端一次请求就能
  重渲染列表，不用再补一发 GET /api/account-pool）。
- 池子已包含全部环境时 added=0（前端据此提示"无需新增"而不是报成功）。
- AdsPower 没启动/本地 API 不可用时 total=0（前端提示去开 AdsPower），不是 500。
- 号池模块异常（比如内置运行时依赖缺失导致 import 失败）回 500 + 原因，
  跟其余 /api/account-pool/* 分支一致。
"""
from email.message import Message

import server


def _handler():
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/account-pool/import-all'
    h.headers = Message()
    h._body_bytes = b''
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


class _FakePool:
    def __init__(self, result, accounts):
        self._result = result
        self._accounts = accounts
        self.import_calls = 0

    def import_adspower_profiles(self):
        self.import_calls += 1
        return self._result

    def list_accounts(self):
        return self._accounts


class TestAccountPoolImportAll:
    def test_imports_all_profiles_and_returns_counts_with_fresh_list(self, monkeypatch):
        accounts = [
            {'user_id': 'u1', 'name': 'a@gmail.com', 'note': '', 'credit': 1000},
            {'user_id': 'u2', 'name': 'b@gmail.com', 'note': '', 'credit': 1000},
            {'user_id': 'u3', 'name': '我的主号', 'note': '别动我', 'credit': 12},
        ]
        pool = _FakePool({'added': ['u1', 'u2'], 'skipped': ['u3'], 'total': 3}, accounts)
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        assert pool.import_calls == 1
        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert (body['added'], body['skipped'], body['total']) == (2, 1, 3)
        assert body['accounts'] == accounts

    def test_nothing_new_reports_zero_added(self, monkeypatch):
        pool = _FakePool({'added': [], 'skipped': ['u1', 'u2'], 'total': 2}, [])
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 200
        assert (body['added'], body['skipped'], body['total']) == (0, 2, 2)

    def test_adspower_unavailable_is_ok_with_zero_total_not_500(self, monkeypatch):
        pool = _FakePool({'added': [], 'skipped': [], 'total': 0}, [])
        monkeypatch.setattr(server, '_get_account_pool', lambda: pool)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 200
        assert body['total'] == 0 and body['added'] == 0

    def test_pool_module_failure_returns_500_with_reason(self, monkeypatch):
        def boom():
            raise RuntimeError('Google FX 运行时依赖缺失')
        monkeypatch.setattr(server, '_get_account_pool', boom)

        h, sent = _handler()
        h.do_POST()

        body, status = sent[0]
        assert status == 500
        assert body['status'] == 'error'
        assert 'Google FX' in body['message']
