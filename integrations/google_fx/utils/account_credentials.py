# -*- coding: utf-8 -*-
"""
🔑 号池账号登录凭据
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
按 AdsPower user_id 存 Google 账号的邮箱 / 密码 / TOTP 密钥，供 auto_login.py
在掉登录时自动重新登进去。跟号池是同一套 key（user_id），语义上就是号池的一
个字段——但**必须是另一个文件**，原因只有一个：

    account_pool.list_accounts() 的返回值会被 /api/account-pool 整份 JSON 吐给
    浏览器。凭据只要跟号池状态同文件同 dict，任何一次「顺手把 info 拷进
    entry」的写法都会把明文密码送到前端，而这种泄漏在代码 review 时极难看出来
    （它长得就像一次普通的 dict(info)）。分文件之后，前端拿到的是
    public_entry() 这份白名单视图，明文根本没有路径流到 HTTP 响应里。

存储方式：明文 JSON，放 runtime/（已在 .gitignore），文件权限收到 0600。
**不假装加密**——本地自用工具，进程要能无人值守地自己读出密码来用，任何"加密"
的密钥都得跟密文放在同一台机器上，那只是混淆不是安全。跟项目里
server_config.json 明文存 apiKey 是同一套取舍，如实写在这里而不是用一个
base64 让人误以为存了密文。真正的保护来自：文件权限、runtime/ 不进 git、
接口不回明文。

⚠️ 拿到这个文件 = 拿到这些 Google 账号。别把 runtime/ 目录同步到网盘或打包发人。
"""

import json
import os
import stat
import threading
from datetime import datetime, timezone
from typing import Optional

from ..config import AI_DIR
from .logger import log
from . import totp

_STATE_FILE = AI_DIR / "runtime" / "account_credentials.json"
_LOCK = threading.Lock()


class CredentialStoreError(RuntimeError):
    """凭据写盘失败。写不进去而调用方以为存上了，下次掉登录会白等人工。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _restrict_permissions(path):
    """把凭据文件收到「仅属主可读写」。

    Windows 上 os.chmod 只能改只读位，POSIX 权限位无效——这不是可以静默忽略的
    降级，而是本模块的保护强度在 Windows 上确实更弱（靠 NTFS 用户目录 ACL）。
    如实记一行日志，不做假动作。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception as e:
        log(f"⚠️ 凭据文件权限未能收紧（{type(e).__name__}），"
            "该文件含明文密码，请确认所在目录不被其它用户或网盘同步访问", "凭据")


def _read_state() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # 不吞成空字典就完事：凭据读不出来会让所有账号的自动登录静默失效，
        # 表现成"功能没生效"而不是"文件坏了"，必须留痕。
        log(f"⚠️ 读取账号凭据失败，本次按「没有任何凭据」处理: {type(e).__name__}: {e}", "凭据")
        return {}


def _write_state(state: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = _STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        _restrict_permissions(tmp_file)  # 先收权限再改名，避免明文有一瞬间是 0644
        tmp_file.replace(_STATE_FILE)
        _restrict_permissions(_STATE_FILE)
    except Exception as e:
        log(f"❌ 写入账号凭据失败（未落盘）: {type(e).__name__}: {e}", "凭据")
        raise CredentialStoreError(f"凭据写盘失败，本次修改未生效: {e}") from e


# 连续多少次「凭据级失败」（密码错 / 2FA 码不对）之后停止自动重试。
#
# 这是本功能最重要的一条护栏。自动登录跑在无人值守的生成链路里，一旦密码填错，
# 没有熔断就会变成「每次掉登录都拿错密码撞一次」的死循环——Google 对连续失败的
# 登录尝试会先上验证码、再锁账号，那时用户损失的不是一次生成任务而是整个号。
# 取 2 而不是 3/5：密码要么对要么不对，第一次失败已经说明问题，第二次只是排除
# 「输入被页面吞了一个字符」这类偶发。
MAX_FAIL_STREAK = 2

# 只有这些原因才算进熔断计数。页面超时/浏览器被关/出验证码属于环境问题，
# 下次重试完全合理，把它们算进去会让熔断在账号根本没问题时误触发。
CREDENTIAL_FAILURES = ("wrong_password", "wrong_totp", "account_rejected")


def public_entry(user_id: str, info: dict) -> dict:
    """给 HTTP 接口/控制台用的视图：只报"有没有"，明文永不出这个模块。

    邮箱是唯一回传的原文——它本来就显示在号池的账号命名里（号池默认拿 AdsPower
    环境名当命名，而那正是邮箱），藏它没有意义，反而让用户没法确认自己填的是
    哪个号。密码和 TOTP 密钥只回布尔。
    """
    streak = int(info.get("auto_login_fail_streak") or 0)
    return {
        "user_id": user_id,
        "email": info.get("email") or "",
        "has_password": bool(info.get("password")),
        "has_totp": bool(info.get("totp_secret")),
        "updated_at": info.get("updated_at"),
        "auto_login_at": info.get("auto_login_at"),
        "auto_login_status": info.get("auto_login_status"),
        "auto_login_error": info.get("auto_login_error"),
        "auto_login_fail_streak": streak,
        "auto_login_blocked": streak >= MAX_FAIL_STREAK,
    }


def get(user_id: str) -> Optional[dict]:
    """取明文凭据。**只有 auto_login 该调它**，返回值不要放进任何日志或响应。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    with _LOCK:
        info = _read_state().get(user_id)
    return dict(info) if info else None


def has_credentials(user_id: str) -> bool:
    """够不够自动登录：邮箱和密码都在才算。TOTP 是可选的（有的号没开两步验证）。"""
    info = get(user_id)
    return bool(info and info.get("email") and info.get("password"))


def presence_map() -> dict:
    """user_id -> public_entry，供号池列表一次性带出「这个号配没配凭据」。

    号池每行都单独查一次文件太浪费（list_accounts 一次可能几十个号）。
    """
    with _LOCK:
        state = _read_state()
    return {uid: public_entry(uid, info) for uid, info in state.items()
            if isinstance(info, dict)}


def save(user_id: str, email: str = None, password: str = None,
         totp_secret: str = None) -> dict:
    """写入/更新凭据。传 None 表示「这一项不改」，传空字符串表示「清空这一项」。

    这个区分是必须的：列表接口从不回传密码明文，前端编辑时密码框默认是空的，
    如果把「空」一律当成「清空」，用户改一下邮箱就会把密码抹掉（proxy_pool 的
    keep_password 处理的是同一个坑，这里用 None/'' 的语义差别表达得更直接）。
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("缺少 user_id")

    # TOTP 密钥先验证再落盘：存一个算不出码的密钥，要等到某天真的掉登录、
    # 自动登录走到 2FA 那一步才会暴露，那时人不在场。
    normalized_totp = None
    if totp_secret is not None:
        stripped = str(totp_secret).strip()
        normalized_totp = totp.normalize_secret(stripped) if stripped else ""

    with _LOCK:
        state = _read_state()
        info = dict(state.get(user_id) or {})
        if email is not None:
            info["email"] = str(email).strip()
        if password is not None:
            info["password"] = str(password)
        if normalized_totp is not None:
            info["totp_secret"] = normalized_totp
        info["updated_at"] = _now_iso()
        # 改了凭据 = 用户已经在处理登录问题了，熔断计数清零，让下一次掉登录能
        # 用新凭据再试。不清零的话「密码错了 → 熔断 → 用户改对密码 → 仍然不
        # 自动登录」，用户完全看不出还要去哪儿解锁。
        if email is not None or password is not None or normalized_totp is not None:
            info["auto_login_fail_streak"] = 0
            info["auto_login_status"] = None
            info["auto_login_error"] = None
        state[user_id] = info
        _write_state(state)

    log(f"🔑 已保存账号 {user_id} 的登录凭据"
        f"（邮箱={'有' if info.get('email') else '无'}"
        f" 密码={'有' if info.get('password') else '无'}"
        f" 2FA={'有' if info.get('totp_secret') else '无'}）", "凭据")
    return public_entry(user_id, info)


def remove(user_id: str) -> bool:
    user_id = str(user_id or "").strip()
    with _LOCK:
        state = _read_state()
        if user_id not in state:
            return False
        del state[user_id]
        _write_state(state)
    log(f"🗑️ 已删除账号 {user_id} 的登录凭据", "凭据")
    return True


def is_blocked(user_id: str) -> bool:
    """熔断中：连续凭据级失败太多次，不许再自动登录，必须人工确认凭据。"""
    info = get(user_id)
    return bool(info) and int(info.get("auto_login_fail_streak") or 0) >= MAX_FAIL_STREAK


def record_attempt(user_id: str, status: str, reason: str = "",
                   error: str = "") -> Optional[dict]:
    """记一次自动登录的结果，并维护熔断计数。

    status='ok' 清零计数；凭据级失败（reason 在 CREDENTIAL_FAILURES 里）+1；
    其余失败原样记录但不累加——见 CREDENTIAL_FAILURES 的注释。
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    with _LOCK:
        state = _read_state()
        info = dict(state.get(user_id) or {})
        if not info:
            # 没配凭据的账号不该走到这里；真走到了也不要凭空建一条空凭据记录。
            return None
        if status == "ok":
            info["auto_login_fail_streak"] = 0
            info["auto_login_error"] = None
        else:
            if reason in CREDENTIAL_FAILURES:
                info["auto_login_fail_streak"] = int(info.get("auto_login_fail_streak") or 0) + 1
            info["auto_login_error"] = (error or reason or "未知原因")[:300]
        info["auto_login_status"] = status
        info["auto_login_at"] = _now_iso()
        state[user_id] = info
        try:
            _write_state(state)
        except CredentialStoreError:
            # 记录写不下去不该把一次成功的登录变成失败。但熔断计数丢了确实危险，
            # 所以留痕（_write_state 已经打过 ❌ 行）后继续，不静默。
            pass
    return public_entry(user_id, info)


def reset_breaker(user_id: str) -> Optional[dict]:
    """人工解除熔断（控制台按钮）。用户确认过凭据没问题时用。"""
    user_id = str(user_id or "").strip()
    with _LOCK:
        state = _read_state()
        if user_id not in state:
            return None
        state[user_id]["auto_login_fail_streak"] = 0
        state[user_id]["auto_login_error"] = None
        state[user_id]["auto_login_status"] = None
        _write_state(state)
        info = dict(state[user_id])
    log(f"♻️ 账号 {user_id} 自动登录熔断已人工解除", "凭据")
    return public_entry(user_id, info)


def current_totp(user_id: str) -> Optional[str]:
    """算这个账号此刻的 2FA 动态码；没配 TOTP 密钥返回 None。

    用 generate_fresh：码只剩两三秒时先等下一个窗口，别把一次 2FA 尝试
    浪费在一个提交到 Google 就已过期的码上。
    """
    info = get(user_id)
    secret = (info or {}).get("totp_secret")
    if not secret:
        return None
    try:
        return totp.generate_fresh(secret)
    except totp.TotpSecretError as e:
        log(f"⚠️ 账号 {user_id} 的 TOTP 密钥无法算码: {e}", "凭据")
        return None
