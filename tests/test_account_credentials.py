# -*- coding: utf-8 -*-
"""号池账号登录凭据的存储语义。

两件事必须钉死，因为出错都是静默的：
1. 明文密码/TOTP 密钥绝不能出现在任何会被发给浏览器的结构里；
2. "字段没传"和"字段传了空串"是两种不同的意思（不改 vs 清空）。
"""

import json

import pytest

from integrations.google_fx.utils import account_credentials as creds

_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """凭据文件路径是模块导入期算好的绝对路径，chdir 穿不透，必须直接改常量。
    不隔离的话测试会写进开发者真实的 runtime/account_credentials.json。"""
    monkeypatch.setattr(creds, "_STATE_FILE", tmp_path / "account_credentials.json")
    return tmp_path / "account_credentials.json"


def test_save_and_read_back_roundtrip():
    creds.save("u1", email="me@example.com", password="pw", totp_secret=_SECRET)
    stored = creds.get("u1")
    assert stored["email"] == "me@example.com"
    assert stored["password"] == "pw"
    assert stored["totp_secret"] == _SECRET


def test_public_entry_never_exposes_password_or_totp_secret():
    """这份视图会经 account_pool.list_accounts() → /api/account-pool 发到浏览器。
    任何一个明文字段漏进来都是把 Google 密码发给了前端。"""
    creds.save("u1", email="me@example.com", password="hunter2", totp_secret=_SECRET)

    entry = creds.public_entry("u1", creds.get("u1"))
    serialized = json.dumps(entry, ensure_ascii=False)

    assert "hunter2" not in serialized
    assert _SECRET not in serialized
    assert entry["has_password"] is True
    assert entry["has_totp"] is True
    assert entry["email"] == "me@example.com"  # 邮箱是有意回传的


def test_presence_map_never_exposes_secrets_either():
    creds.save("u1", email="a@b.com", password="hunter2", totp_secret=_SECRET)
    serialized = json.dumps(creds.presence_map(), ensure_ascii=False)
    assert "hunter2" not in serialized and _SECRET not in serialized


def test_omitted_field_keeps_previous_value():
    """前端的密码框天生是空的（列表接口不回明文）。改个邮箱就把密码抹掉的话，
    下一次掉登录会静默退回等人工，而用户以为凭据还在。"""
    creds.save("u1", email="old@example.com", password="pw", totp_secret=_SECRET)
    creds.save("u1", email="new@example.com")  # 只改邮箱

    stored = creds.get("u1")
    assert stored["email"] == "new@example.com"
    assert stored["password"] == "pw"
    assert stored["totp_secret"] == _SECRET


def test_explicit_empty_string_clears_the_field():
    creds.save("u1", email="a@b.com", password="pw", totp_secret=_SECRET)
    creds.save("u1", totp_secret="")  # 显式清空 2FA

    assert creds.get("u1")["totp_secret"] == ""
    assert creds.get("u1")["password"] == "pw"


def test_invalid_totp_secret_is_rejected_at_save_time():
    """存一个算不出码的密钥，要等到某天真的掉登录、走到 2FA 那步才会暴露，
    而那时人不在场。必须保存当场就拒。"""
    with pytest.raises(ValueError):
        creds.save("u1", email="a@b.com", password="pw", totp_secret="not base32!!")
    assert creds.get("u1") is None


def test_totp_secret_is_normalized_before_storage():
    creds.save("u1", email="a@b.com", password="pw",
               totp_secret="gezd gnbv gy3t qojq gezd gnbv gy3t qojq")
    assert creds.get("u1")["totp_secret"] == _SECRET


def test_has_credentials_requires_both_email_and_password():
    creds.save("u1", email="a@b.com")
    assert creds.has_credentials("u1") is False  # 只有邮箱会在密码页原地卡死

    creds.save("u1", password="pw")
    assert creds.has_credentials("u1") is True

    # TOTP 是可选的：有的号根本没开两步验证
    assert creds.get("u1").get("totp_secret") in (None, "")


def test_has_credentials_is_false_for_unknown_account():
    assert creds.has_credentials("never-seen") is False


# ── 熔断 ────────────────────────────────────────────────────────

def test_breaker_opens_after_consecutive_credential_failures():
    """没有熔断，密码填错就会变成"每次掉登录都拿错密码撞一次"的死循环，
    Google 先上验证码、再锁号——损失的不是一次任务而是整个账号。"""
    creds.save("u1", email="a@b.com", password="wrong")

    creds.record_attempt("u1", "failed", "wrong_password", "密码不对")
    assert creds.is_blocked("u1") is False, "第一次失败还不该熔断（可能是输入被吞字符）"

    creds.record_attempt("u1", "failed", "wrong_password", "密码不对")
    assert creds.is_blocked("u1") is True


def test_environmental_failures_do_not_trip_the_breaker():
    """页面超时/浏览器被关/出验证码是环境问题，账号本身没毛病。把它们算进熔断
    会让功能在账号完全正常的情况下自己关掉。"""
    creds.save("u1", email="a@b.com", password="pw")
    for _ in range(5):
        creds.record_attempt("u1", "failed", "captcha_required", "出验证码了")
    assert creds.is_blocked("u1") is False


def test_success_resets_the_failure_streak():
    creds.save("u1", email="a@b.com", password="pw")
    creds.record_attempt("u1", "failed", "wrong_password", "密码不对")
    creds.record_attempt("u1", "ok", "logged_in", "")

    assert creds.public_entry("u1", creds.get("u1"))["auto_login_fail_streak"] == 0
    assert creds.get("u1")["auto_login_error"] is None


def test_saving_new_credentials_clears_the_breaker():
    """不清零的话就是「密码错 → 熔断 → 用户改对密码 → 仍然不自动登录」，
    而用户完全看不出还要去哪儿解锁。"""
    creds.save("u1", email="a@b.com", password="wrong")
    creds.record_attempt("u1", "failed", "wrong_password", "密码不对")
    creds.record_attempt("u1", "failed", "wrong_password", "密码不对")
    assert creds.is_blocked("u1") is True

    creds.save("u1", password="correct")
    assert creds.is_blocked("u1") is False


def test_reset_breaker_is_a_manual_escape_hatch():
    creds.save("u1", email="a@b.com", password="pw")
    creds.record_attempt("u1", "failed", "wrong_password", "x")
    creds.record_attempt("u1", "failed", "wrong_password", "x")

    entry = creds.reset_breaker("u1")
    assert entry["auto_login_blocked"] is False
    assert creds.is_blocked("u1") is False


def test_reset_breaker_returns_none_for_unknown_account():
    assert creds.reset_breaker("never-seen") is None


def test_record_attempt_does_not_fabricate_a_credential_record():
    """没配凭据的账号不该因为记了一笔"登录失败"就凭空出现在凭据文件里——
    那会让号池显示它配过凭据。"""
    assert creds.record_attempt("never-seen", "failed", "wrong_password", "x") is None
    assert creds.get("never-seen") is None


def test_remove_deletes_the_plaintext(_isolated_store):
    creds.save("u1", email="a@b.com", password="hunter2")
    assert creds.remove("u1") is True
    assert creds.get("u1") is None
    assert "hunter2" not in _isolated_store.read_text(encoding="utf-8")


def test_remove_is_false_for_unknown_account():
    assert creds.remove("never-seen") is False


def test_current_totp_returns_none_when_not_configured():
    creds.save("u1", email="a@b.com", password="pw")
    assert creds.current_totp("u1") is None


def test_current_totp_generates_a_six_digit_code(monkeypatch):
    from integrations.google_fx.utils import totp
    monkeypatch.setattr(totp, "seconds_remaining", lambda **kw: 30.0)  # 别在测试里真等窗口

    creds.save("u1", email="a@b.com", password="pw", totp_secret=_SECRET)
    code = creds.current_totp("u1")
    assert code is not None and len(code) == 6 and code.isdigit()


def test_corrupt_store_degrades_to_empty_instead_of_crashing(_isolated_store):
    """凭据文件坏了不能把号池列表整个炸掉——那会让用户以为号池坏了。"""
    _isolated_store.parent.mkdir(parents=True, exist_ok=True)
    _isolated_store.write_text("{ not json", encoding="utf-8")
    assert creds.presence_map() == {}
    assert creds.get("u1") is None
