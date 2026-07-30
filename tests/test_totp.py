# -*- coding: utf-8 -*-
"""TOTP 动态码生成（自动登录过两步验证靠它）。

核心断言是 RFC 6238 附录 B 的官方测试向量——自己实现 RFC 的唯一负责任的验证
方式就是拿标准里给的输入输出对着跑，而不是"看着像 6 位数就行"。
"""

import pytest

from integrations.google_fx.utils import totp

# RFC 6238 附录 B：密钥是 ASCII "12345678901234567890" 的 base32 形式，
# 表里给的是 8 位码，取后 6 位就是身份验证器 App 显示的那个。
_RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("timestamp,expected8", _RFC_VECTORS)
def test_matches_rfc6238_official_test_vectors(timestamp, expected8):
    assert totp.generate(_RFC_SECRET, at=timestamp, digits=8) == expected8
    # 生产里用的是 6 位：就是 8 位码的低 6 位。
    assert totp.generate(_RFC_SECRET, at=timestamp) == expected8[-6:]


def test_code_is_zero_padded_string_not_int():
    """码可能以 0 开头（约 1/10 概率）。返回 int 再格式化最容易漏掉补零，
    那会往 Google 的输入框里填 5 位数，表现为"2FA 莫名其妙总是失败"。"""
    code = totp.generate(_RFC_SECRET, at=1111111109)
    assert code == "081804"
    assert isinstance(code, str) and len(code) == 6


def test_same_30s_window_gives_same_code_and_next_window_differs():
    # 1111111110 和 1111111111 同属 counter=37037037 这个窗口；1111111109 落在
    # 上一个窗口——RFC 的向量表同时给出这两个相邻秒，正是为了钉住这个边界。
    assert totp.generate(_RFC_SECRET, at=1111111110) == totp.generate(_RFC_SECRET, at=1111111111)
    assert totp.generate(_RFC_SECRET, at=1111111109) != totp.generate(_RFC_SECRET, at=1111111110)


@pytest.mark.parametrize("raw", [
    "gezdgnbvgy3tqojqgezdgnbvgy3tqojq",              # 小写
    "GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ",       # Google 页面上的分组空格
    "GEZD-GNBV-GY3T-QOJQ-GEZD-GNBV-GY3T-QOJQ",       # 连字符
    "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ====",          # 带填充
])
def test_accepts_every_shape_users_actually_paste(raw):
    """用户复制密钥的形态五花八门，全都得能用——规整失败的话，用户会看到一个
    "密钥格式错误"，但他复制的确实就是 Google 给的那串。"""
    assert totp.normalize_secret(raw) == _RFC_SECRET
    assert totp.generate(raw, at=59) == "287082"


def test_rejects_non_base32_with_actionable_message():
    # 最常见的误粘：整条 otpauth:// URL。报错要点出到底哪些字符不合法。
    with pytest.raises(totp.TotpSecretError) as excinfo:
        totp.normalize_secret("otpauth://totp/Google:me@example.com?secret=ABC")
    assert "base32" in str(excinfo.value)


def test_rejects_too_short_secret():
    with pytest.raises(totp.TotpSecretError, match="短于"):
        totp.normalize_secret("ABCDEFGH")


def test_rejects_empty_secret():
    with pytest.raises(totp.TotpSecretError):
        totp.normalize_secret("   ")


def test_verify_secret_is_a_boolean_probe_not_an_exception():
    assert totp.verify_secret(_RFC_SECRET) is True
    assert totp.verify_secret("not-a-secret") is False


def test_seconds_remaining_tracks_the_window_boundary():
    assert totp.seconds_remaining(at=1111111100) == pytest.approx(10.0)
    assert totp.seconds_remaining(at=1111111109) == pytest.approx(1.0)


def test_generate_fresh_waits_out_a_window_about_to_expire(monkeypatch):
    """码只剩 1 秒时直接拿去填，提交到 Google 那边已经过期——白白消耗一次 2FA
    失败，而 Google 对连续 2FA 失败很敏感。必须先等到下一个窗口。"""
    slept = []
    monkeypatch.setattr(totp.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(totp, "seconds_remaining", lambda **kw: 1.0)

    totp.generate_fresh(_RFC_SECRET, min_validity=5.0)
    assert slept and slept[0] == pytest.approx(1.2)


def test_generate_fresh_does_not_wait_when_window_has_room(monkeypatch):
    slept = []
    monkeypatch.setattr(totp.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(totp, "seconds_remaining", lambda **kw: 25.0)

    totp.generate_fresh(_RFC_SECRET, min_validity=5.0)
    assert slept == []
