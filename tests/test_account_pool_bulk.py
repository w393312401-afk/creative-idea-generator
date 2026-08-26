# -*- coding: utf-8 -*-
"""
🧪 账号池多选/全选批量操作单元与接口测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
测试批量删除、批量禁用/启用、批量清除冷却、批量关闭浏览器，
以及对应 HTTP POST 接口对 user_ids 列表参数的支持。
"""

import io
import json
from email.message import Message
import pytest

import server
from integrations.google_fx.utils import account_pool as ap
from integrations.google_fx.utils import account_credentials as creds


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    pool_file = tmp_path / "account_pool.json"
    cred_file = tmp_path / "account_credentials.json"
    monkeypatch.setattr(ap, "_STATE_FILE", pool_file)
    monkeypatch.setattr(creds, "_STATE_FILE", cred_file)
    yield


def _handler(path, payload):
    body = json.dumps(payload).encode("utf-8")
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    h.rfile = io.BytesIO(body)
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _post(path, payload):
    h, sent = _handler(path, payload)
    h.do_POST()
    return sent[0]


class TestAccountPoolBulkMethods:
    def test_remove_accounts_bulk(self):
        pool = ap.AccountPool()
        pool.add_account("acc1", "账号1")
        pool.add_account("acc2", "账号2")
        pool.add_account("acc3", "账号3")

        creds.save("acc1", email="a1@test.com", password="pwd")
        creds.save("acc2", email="a2@test.com", password="pwd")

        assert len(pool.list_accounts()) == 3

        # 批量删除 acc1 与 acc2
        removed = pool.remove_accounts(["acc1", "acc2", "acc_nonexistent"])
        assert removed == 2
        remaining = pool.list_accounts()
        assert len(remaining) == 1
        assert remaining[0]["user_id"] == "acc3"

        # 检查凭据也已被清理
        assert creds.get("acc1") is None
        assert creds.get("acc2") is None

    def test_set_disabled_multiple(self):
        pool = ap.AccountPool()
        pool.add_account("acc1")
        pool.add_account("acc2")
        pool.add_account("acc3")

        # 批量禁用 acc1 和 acc3
        res = pool.set_disabled_multiple(["acc1", "acc3"], True)
        assert len(res) == 2
        accounts = {a["user_id"]: a for a in pool.list_accounts()}
        assert accounts["acc1"]["disabled"] is True
        assert accounts["acc2"]["disabled"] is False
        assert accounts["acc3"]["disabled"] is True

        # 批量重新启用 acc1
        res2 = pool.set_disabled_multiple(["acc1", "acc2"], False)
        assert len(res2) == 2
        accounts2 = {a["user_id"]: a for a in pool.list_accounts()}
        assert accounts2["acc1"]["disabled"] is False
        assert accounts2["acc2"]["disabled"] is False
        assert accounts2["acc3"]["disabled"] is True

    def test_clear_cooldown_multiple(self):
        pool = ap.AccountPool()
        pool.add_account("acc1")
        pool.add_account("acc2")

        pool.mark_exhausted("acc1", cooldown_hours=10)
        pool.mark_login_required("acc2", cooldown_hours=2)

        accs = {a["user_id"]: a for a in pool.list_accounts()}
        assert accs["acc1"]["cooldown_until"] is not None
        assert accs["acc2"]["cooldown_until"] is not None

        res = pool.clear_cooldown_multiple(["acc1", "acc2"])
        assert len(res) == 2

        accs2 = {a["user_id"]: a for a in pool.list_accounts()}
        assert accs2["acc1"]["cooldown_until"] is None
        assert accs2["acc2"]["cooldown_until"] is None


class TestAccountPoolBulkEndpoints:
    def test_endpoint_delete_bulk(self):
        pool = ap.AccountPool()
        pool.add_account("u1")
        pool.add_account("u2")
        pool.add_account("u3")

        body, status = _post("/api/account-pool/delete", {"user_ids": ["u1", "u2"]})
        assert status == 200
        assert body["status"] == "ok"
        assert body["removed_count"] == 2

        remaining = [a["user_id"] for a in pool.list_accounts()]
        assert remaining == ["u3"]

    def test_endpoint_toggle_bulk(self):
        pool = ap.AccountPool()
        pool.add_account("u1")
        pool.add_account("u2")

        body, status = _post("/api/account-pool/toggle", {"user_ids": ["u1", "u2"], "disabled": True})
        assert status == 200
        assert body["status"] == "ok"
        assert body["count"] == 2

        for a in pool.list_accounts():
            assert a["disabled"] is True

    def test_endpoint_clear_cooldown_bulk(self):
        pool = ap.AccountPool()
        pool.add_account("u1")
        pool.mark_exhausted("u1", cooldown_hours=5)

        body, status = _post("/api/account-pool/clear-cooldown", {"user_ids": ["u1"]})
        assert status == 200
        assert body["status"] == "ok"
        assert body["count"] == 1
        assert pool.list_accounts()[0]["cooldown_until"] is None

    def test_endpoint_close_browser_bulk(self, monkeypatch):
        pool = ap.AccountPool()
        pool.add_account("u1")
        pool.add_account("u2")

        monkeypatch.setattr(ap.AccountPool, "close_browser", lambda self, uid, port=None: (True, f"closed {uid}"))

        body, status = _post("/api/account-pool/close-browser", {"user_ids": ["u1", "u2"]})
        assert status == 200
        assert body["status"] == "ok"
        assert body["count"] == 2
        assert body["results"]["u1"]["success"] is True

    def test_endpoint_credentials_bulk(self):
        pool = ap.AccountPool()
        pool.add_account("u1", "u1@test.com")
        pool.add_account("u2", "u2@test.com")

        body, status = _post("/api/account-pool/credentials", {
            "user_ids": ["u1", "u2"],
            "password": "Sharpal2025"
        })
        assert status == 200
        assert body["status"] == "ok"
        assert body["count"] == 2

        c1 = creds.get("u1")
        c2 = creds.get("u2")
        assert c1["password"] == "Sharpal2025"
        assert c2["password"] == "Sharpal2025"
        assert c1["email"] == "u1@test.com"
        assert c2["email"] == "u2@test.com"
