# -*- coding: utf-8 -*-
"""
🔢 TOTP 验证码生成（RFC 6238 / RFC 4226）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
自动登录要在 Google 的两步验证页面填 6 位动态码。码是本地按「共享密钥 + 当前
时间」算出来的，不需要联网、不需要手机——所以只要用户把绑定身份验证器 App 时
那串 base32 密钥存进号池凭据，掉登录就能真的无人值守恢复。

为什么自己实现而不是 pyotp：算法总共二十来行标准库代码（hmac + hashlib +
struct + base64 都在 stdlib 里），而这个项目的 requirements.txt 一贯克制，每条
依赖都写了「为什么非它不可」。为一个 20 行的 RFC 再拉一个包，还要求用户在离线
/内网机器上装得上，不划算。下面的实现按 RFC 6238 附录 B 的官方测试向量校验过
（见 tests/test_totp.py）。

⚠️ 时间敏感：动态码按本机时钟算。机器时钟偏差超过 ±30s 会生成 Google 不认的
码，表现为「密码填对了但 2FA 一直失败」。verify_secret() 不检查时钟——本地无从
判断谁对谁错——但 auto_login 连续失败时会在日志里点名提醒查时钟。
"""

import base64
import hashlib
import hmac
import re
import struct
import time

# base32 字母表（RFC 4648）。Google 给的密钥就是这个字母表，通常按 4 个字符
# 一组用空格隔开，也可能带 '-'；有些导出工具会补 '=' 填充。
_BASE32_ALPHABET = re.compile(r"^[A-Z2-7]+$")
_SEPARATORS = re.compile(r"[\s\-_]")

DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30


class TotpSecretError(ValueError):
    """TOTP 密钥格式不合法。跟「算码失败」分开，好让调用方回准确的报错文案。"""


def normalize_secret(secret: str) -> str:
    """把用户从 Google 页面复制来的密钥规整成纯 base32 大写串。

    用户复制到的形态五花八门：'abcd efgh ijkl mnop'（带空格分组）、小写、
    带 '=' 填充、带 '-'。全部规整成同一种，存盘和算码都用这一种，避免
    「看着一样但存进去多了个空格」这种查不出来的失败。
    """
    cleaned = _SEPARATORS.sub("", str(secret or "")).upper().rstrip("=")
    if not cleaned:
        raise TotpSecretError("TOTP 密钥为空")
    if not _BASE32_ALPHABET.match(cleaned):
        # 明确指出非法字符，不要只说「格式错误」——最常见的原因是用户把
        # 二维码下面那行 otpauth:// URL 整条粘了进来，或者把 1/0 认成了 I/O。
        bad = sorted({ch for ch in cleaned if not _BASE32_ALPHABET.match(ch)})
        raise TotpSecretError(
            f"TOTP 密钥含非 base32 字符 {''.join(bad)}；"
            "只能包含 A-Z 和 2-7（不含数字 0、1 和字母 O、I）"
        )
    if len(cleaned) < 16:
        # Google 发的密钥是 16 或 32 字符。短于 16 基本是复制漏了。
        raise TotpSecretError(f"TOTP 密钥只有 {len(cleaned)} 个字符，短于 Google 的最短长度 16，多半是复制漏了")
    return cleaned


def _decode_secret(secret: str) -> bytes:
    normalized = normalize_secret(secret)
    padding = "=" * (-len(normalized) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as e:  # normalize 已经挡掉字母表问题，这里只剩长度非法
        raise TotpSecretError(f"TOTP 密钥无法解码: {e}") from e


def verify_secret(secret: str) -> bool:
    """密钥能不能用来算码。保存凭据时先跑一遍，别等掉登录当场才发现填错了。"""
    try:
        _decode_secret(secret)
        return True
    except TotpSecretError:
        return False


def generate(secret: str, at: float = None, digits: int = DEFAULT_DIGITS,
             period: int = DEFAULT_PERIOD) -> str:
    """算出 at 时刻（默认现在）的动态码，返回补零到 digits 位的字符串。

    返回字符串而不是 int：码可能以 0 开头（约 10% 概率），转成 int 再格式化
    是一步没必要的、容易漏掉补零的中转。
    """
    key = _decode_secret(secret)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # RFC 4226 §5.4 动态截断
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def seconds_remaining(at: float = None, period: int = DEFAULT_PERIOD) -> float:
    """当前码还有多久过期。填表前用它决定要不要等下一个窗口。

    自动登录填码要花一两秒（定位输入框 + 逐字符输入 + 提交），如果拿到码时它
    只剩 1 秒有效期，提交到 Google 那边已经过期了——那是一次白白消耗的失败
    尝试，而 Google 对连续 2FA 失败很敏感。所以 auto_login 会在余量不足时
    先等到下一个窗口再算码。
    """
    now = time.time() if at is None else at
    return period - (now % period)


def generate_fresh(secret: str, min_validity: float = 5.0,
                   digits: int = DEFAULT_DIGITS, period: int = DEFAULT_PERIOD) -> str:
    """算一个至少还有 min_validity 秒有效期的码，必要时阻塞等到下一个窗口。

    最坏阻塞 min_validity 秒（默认 5s）——远比赔上一次 2FA 失败便宜。
    """
    remaining = seconds_remaining(period=period)
    if remaining < min_validity:
        time.sleep(remaining + 0.2)
    return generate(secret, digits=digits, period=period)
