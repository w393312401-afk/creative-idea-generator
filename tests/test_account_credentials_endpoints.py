# -*- coding: utf-8 -*-
"""自动登录凭据的 HTTP 接口契约。

重点在两条容易静默出错的边界：
- 字段缺省 vs 传空串（不改 vs 清空）。搞混了，用户改个邮箱就把密码抹了，而且
  完全无声——下一次掉登录直接退回等人工。
- 「浏览器忙」要回 409 而不是 422：账号本身没毛病，报成失败会让用户跑去改凭据。
"""
from email.message import Message
import io
import json

import pytest

import server
from integrations.google_fx.utils import account_credentials as creds
from integrations.google_fx.utils import auto_login

_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "_STATE_FILE", tmp_path / "account_credentials.json")


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


class _Control:
    def __init__(self, active_list=None, processing_paused=False):
        self._snapshot = {"active_list": list(active_list or []),
                          "processing_paused": processing_paused}

    def snapshot(self):
        return self._snapshot


class TestSaveCredentials:
    def test_saves_and_reports_presence_without_echoing_secrets(self):
        body, status = _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "me@example.com",
            "password": "hunter2", "totp_secret": _SECRET})

        assert status == 200
        assert body["credentials"]["has_password"] is True
        assert body["credentials"]["has_totp"] is True
        serialized = json.dumps(body, ensure_ascii=False)
        assert "hunter2" not in serialized and _SECRET not in serialized

    def test_omitting_password_keeps_the_stored_one(self):
        _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "old@example.com", "password": "hunter2"})

        body, status = _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "new@example.com"})

        assert status == 200
        assert creds.get("u1")["password"] == "hunter2"
        assert creds.get("u1")["email"] == "new@example.com"

    def test_explicit_empty_totp_clears_it(self):
        _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "a@b.com", "password": "pw", "totp_secret": _SECRET})

        _post("/api/account-pool/credentials", {"user_id": "u1", "totp_secret": ""})

        assert creds.get("u1")["totp_secret"] == ""
        assert creds.get("u1")["password"] == "pw"

    def test_bad_totp_secret_is_a_400_with_a_fixable_message(self):
        """是用户填错了，不是服务故障。500 会让人以为要重启服务。"""
        body, status = _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "a@b.com", "password": "pw",
            "totp_secret": "otpauth://totp/whatever"})

        assert status == 400
        assert "base32" in body["message"]

    def test_missing_user_id_is_400(self):
        body, status = _post("/api/account-pool/credentials", {"email": "a@b.com"})
        assert status == 400


class TestDeleteAndBreaker:
    def test_delete_removes_the_plaintext(self):
        _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "a@b.com", "password": "hunter2"})

        body, status = _post("/api/account-pool/credentials/delete", {"user_id": "u1"})

        assert status == 200 and body["removed"] is True
        assert creds.get("u1") is None

    def test_delete_of_unknown_account_is_not_an_error(self):
        body, status = _post("/api/account-pool/credentials/delete", {"user_id": "nope"})
        assert status == 200 and body["removed"] is False

    def test_reset_breaker_reopens_auto_login(self):
        _post("/api/account-pool/credentials", {
            "user_id": "u1", "email": "a@b.com", "password": "pw"})
        creds.record_attempt("u1", "failed", "wrong_password", "x")
        creds.record_attempt("u1", "failed", "wrong_password", "x")

        body, status = _post("/api/account-pool/reset-breaker", {"user_id": "u1"})

        assert status == 200
        assert body["credentials"]["auto_login_blocked"] is False
        assert creds.is_blocked("u1") is False

    def test_reset_breaker_on_unknown_account_is_404(self):
        body, status = _post("/api/account-pool/reset-breaker", {"user_id": "nope"})
        assert status == 404


class TestTestLoginEndpoint:
    def test_generation_task_holding_the_browser_returns_409(self, monkeypatch):
        monkeypatch.setattr(server, "FX_CONTROL", _Control([
            {"task_id": "compose_42", "kind": "auto", "elapsed_seconds": 90.0}]))
        called = []
        monkeypatch.setattr(auto_login, "run_standalone_login",
                            lambda uid, **kw: called.append(uid))

        body, status = _post("/api/account-pool/login", {"user_id": "u1"})

        assert status == 409 and body["code"] == "FX_BUSY"
        assert called == [], "浏览器忙的时候不该白开一次登录"

    def test_credential_failure_is_422_so_the_user_goes_and_fixes_it(self, monkeypatch):
        monkeypatch.setattr(server, "FX_CONTROL", _Control([]))
        monkeypatch.setattr(server, "_get_account_pool", lambda: type(
            "P", (), {"list_accounts": lambda self, heal=True: []})())
        monkeypatch.setattr(auto_login, "run_standalone_login", lambda uid, **kw:
                            auto_login.AutoLoginResult(False, "wrong_password", "密码不正确"))

        body, status = _post("/api/account-pool/login", {"user_id": "u1"})

        assert status == 422
        assert body["code"] == "WRONG_PASSWORD"

    def test_busy_browser_from_the_slot_is_409_not_a_credential_problem(self, monkeypatch):
        """排不进浏览器闸门跟凭据对不对无关。报成 422 会让用户跑去改一个
        本来没问题的密码。"""
        monkeypatch.setattr(server, "FX_CONTROL", _Control([]))
        monkeypatch.setattr(server, "_get_account_pool", lambda: type(
            "P", (), {"list_accounts": lambda self, heal=True: []})())
        monkeypatch.setattr(auto_login, "run_standalone_login", lambda uid, **kw:
                            auto_login.AutoLoginResult(False, "browser_busy", "浏览器忙"))

        body, status = _post("/api/account-pool/login", {"user_id": "u1"})

        assert status == 409

    def test_success_returns_the_refreshed_account_row(self, monkeypatch):
        monkeypatch.setattr(server, "FX_CONTROL", _Control([]))
        monkeypatch.setattr(server, "_get_account_pool", lambda: type("P", (), {
            "list_accounts": lambda self, heal=True: [
                {"user_id": "u1", "auto_login_ready": True}]})())
        monkeypatch.setattr(auto_login, "run_standalone_login", lambda uid, **kw:
                            auto_login.AutoLoginResult(True, "logged_in", "已重新登录"))

        body, status = _post("/api/account-pool/login", {"user_id": "u1"})

        assert status == 200 and body["status"] == "ok"
        assert body["account"]["auto_login_ready"] is True
