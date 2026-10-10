# -*- coding: utf-8 -*-
"""
🌐 账号出口身份（并发方案 P2 / R5）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
同一时刻在线的几个 AdsPower 环境必须走不同的出口，否则 Google 会把它们关联起来。
这里只回答一个问题：某个环境现在走哪个出口？

出口身份 = 环境代理配置的（类型, 主机, 端口, 用户名）——**不含密码**，也不在任何日志里出现。
没有代理（直连）的环境都归为同一个身份 `local-direct`：它们共用本机 IP，同时最多 1 个。
查不到（AdsPower 不可达、环境不存在）返回 None，调用方按"未知"处理，不当成冲突。

只在并发上限 > 1 时才会被调用；带 10 分钟缓存，避免选号时每个候选都打一次 AdsPower。
"""
import threading
import time

import requests

from ..config import get_runtime_default_port
from .logger import log

DIRECT = "local-direct"
_TTL_SECONDS = 600
_CACHE = {}
_LOCK = threading.Lock()


def identity_from_config(config):
    """代理配置 -> 出口身份字符串；无代理 -> local-direct。"""
    if not isinstance(config, dict):
        return DIRECT
    proxy_type = str(config.get("proxy_type") or "").strip().lower()
    soft = str(config.get("proxy_soft") or "").strip().lower()
    host = str(config.get("proxy_host") or "").strip()
    if soft == "no_proxy" or proxy_type in ("", "noproxy", "no_proxy") or not host:
        return DIRECT
    return "{}://{}:{}@{}".format(proxy_type, host, str(config.get("proxy_port") or "").strip(),
                                  str(config.get("proxy_user") or "").strip())


def egress_id_for(user_id, port=None, force=False):
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(user_id)
        if cached and not force and now - cached[0] < _TTL_SECONDS:
            return cached[1]
    port = port or get_runtime_default_port() or 50325
    try:
        data = requests.get(
            f"http://127.0.0.1:{port}/api/v1/user/list",
            params={"user_id": user_id, "page_size": 100}, timeout=4,
        ).json()
        if data.get("code") != 0:
            return None
        for profile in (data.get("data") or {}).get("list", []):
            if str(profile.get("user_id")) == user_id:
                value = identity_from_config(profile.get("user_proxy_config"))
                with _LOCK:
                    _CACHE[user_id] = (now, value)
                return value
    except Exception as exc:
        log(f"⚠️ 读取环境 {user_id} 的出口配置失败（按未知处理）: {type(exc).__name__}", "账号池")
    return None


def invalidate(user_id=None):
    """代理被改过之后丢掉缓存（换 IP / 下发代理时调用）。"""
    with _LOCK:
        if user_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(user_id), None)
