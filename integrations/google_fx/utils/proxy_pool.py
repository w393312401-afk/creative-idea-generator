# -*- coding: utf-8 -*-
"""
🌐 Google FX 代理号池管理器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
维护一组可复用的出口代理（host/port/账密/协议），并把它们下发到 AdsPower 浏览器
环境（profile）的 userProxyConfig 上。定位与 account_pool.py 完全对称：那个管
"用哪个 Flow 账号"，这个管"这个账号从哪个出口走"。

为什么要有它（此前的状态）：代理只能靠两处配置
  1. runtime/google_fx.env 里的 MIYA_PROXY_* 单组环境变量——只能配一个出口，
     换一组要改文件重启；
  2. runtime/proxy_pool.txt——proxy_rotator 的 list 模式读的纯文本行，
     没有启用/禁用、没有连通性记录、没有"这条代理绑在哪个环境上"，
     控制台里完全看不见，坏了一条只能靠生成任务失败反推。

本模块把它们收进 runtime/proxy_pool.json（结构与 account_pool.json 同风格：
"读 JSON → 改 → 写"，写盘失败必须抛出），并提供：
  · 增删改 / 启用禁用（禁用的代理不参与轮换、不能下发）
  · 连通性检测：拿真实出口 IP，失败原因如实记录，不编造"可用"
  · 下发到 AdsPower 环境：调本地 API 的 user/update 写 user_proxy_config
  · 轮换取号：pick_proxy() 按游标轮转，供 proxy_rotator 使用

⚠️ 与"换 IP 已全局关停"的关系（见 server_common 的同名注释）：
自动换 IP 目前被 MIYA_ROTATE_THRESHOLD 写死的巨大阈值挡着，所以 ProxyRotator
不会自己来取号。本池子当前的主用法是**手动下发**（在控制台把某条代理应用到某个
AdsPower 环境，之后该环境一直走这个出口）。轮换接口照旧接好，是为了将来重新打开
自动换 IP 时不用再改一遍——但不会因为池子有内容就偷偷把换 IP 打开。
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests

from ..config import AI_DIR, get_runtime_default_port
from .logger import log

_STATE_FILE = AI_DIR / "runtime" / "proxy_pool.json"
_LEGACY_TXT_FILE = AI_DIR / "runtime" / "proxy_pool.txt"
_LOCK = threading.Lock()

# AdsPower 的 userProxyConfig 只认这几种；socks5 还要本机装了 PySocks 才能自检。
PROXY_TYPES = ("http", "https", "socks5")

# 连通性检测打这个地址读出口 IP。走代理直连，不开浏览器——检测一条代理不该
# 去排 FX 的浏览器串行锁。
_CHECK_URL = "https://ipinfo.io/json"
_CHECK_TIMEOUT = 12


class ProxyPoolStateError(RuntimeError):
    """代理池状态写盘失败。禁用标记丢了会让轮换重新选中坏代理，必须上报。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _blank_state() -> dict:
    return {"proxies": {}, "rotate_index": 0}


def _read_state() -> dict:
    if not _STATE_FILE.parent.exists():
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _STATE_FILE.exists():
        return _blank_state()
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"⚠️ 读取代理池状态失败，按空池处理: {e}", "代理池")
        return _blank_state()
    if not isinstance(data, dict):
        return _blank_state()
    proxies = data.get("proxies")
    if not isinstance(proxies, dict):
        proxies = {}
    state = {"proxies": {}, "rotate_index": 0}
    try:
        state["rotate_index"] = max(0, int(data.get("rotate_index", 0)))
    except (TypeError, ValueError):
        state["rotate_index"] = 0
    for proxy_id, info in proxies.items():
        if isinstance(info, dict):
            state["proxies"][str(proxy_id)] = info
    return state


def _write_state(state: dict):
    """写代理池状态。失败必须抛出——静默吞会让"已禁用/已下发"变成假象。"""
    if not _STATE_FILE.parent.exists():
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = _STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_file.replace(_STATE_FILE)
    except Exception as e:
        log(f"❌ 写入代理池状态失败（状态未落盘）: {type(e).__name__}: {e}", "代理池")
        raise ProxyPoolStateError(f"代理池状态写盘失败，本次修改未生效: {e}") from e


def _short_error(exc: Exception, limit: int = 220) -> str:
    """requests 的连接异常会把整条 urllib3 重试链拼进消息（轻松四五百字符），
    原样存进状态文件既撑爆表格单元格，也撑爆 toast。留头部——真正说明问题的
    异常类型和根因在前面，尾部是重复的 URL 与堆叠包装。"""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clean_port(value) -> str:
    port = str(value or "").strip()
    if not port:
        raise ValueError("代理端口不能为空")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError("代理端口必须是 1~65535 的整数")
    return str(int(port))


def _clean_type(value) -> str:
    proxy_type = str(value or "http").strip().lower()
    if proxy_type not in PROXY_TYPES:
        raise ValueError(f"代理协议只支持 {', '.join(PROXY_TYPES)}")
    return proxy_type


def proxy_url(entry: dict) -> str:
    """拼 requests 用的代理 URL（含账密）。仅用于本地检测，不写进日志。"""
    scheme = entry.get("proxy_type") or "http"
    auth = ""
    user = str(entry.get("user") or "")
    password = str(entry.get("password") or "")
    if user:
        from urllib.parse import quote
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{entry.get('host')}:{entry.get('port')}"


def to_adspower_config(entry: dict) -> dict:
    """转成 AdsPower 本地 API 的 user_proxy_config 结构。"""
    config = {
        "proxy_soft": "other",
        "proxy_type": entry.get("proxy_type") or "http",
        "proxy_host": str(entry.get("host") or ""),
        "proxy_port": str(entry.get("port") or ""),
    }
    if entry.get("user"):
        config["proxy_user"] = str(entry["user"])
    if entry.get("password"):
        config["proxy_password"] = str(entry["password"])
    return config


def public_entry(proxy_id: str, info: dict) -> dict:
    """给 HTTP 接口/控制台用的视图：密码只报"有没有"，不回传明文。"""
    entry = {
        "proxy_id": proxy_id,
        "label": info.get("label") or "",
        "proxy_type": info.get("proxy_type") or "http",
        "host": info.get("host") or "",
        "port": str(info.get("port") or ""),
        "user": info.get("user") or "",
        "has_password": bool(info.get("password")),
        "note": info.get("note") or "",
        "disabled": bool(info.get("disabled")),
        "bound_user_id": info.get("bound_user_id") or "",
        "applied_at": info.get("applied_at"),
        "last_check_at": info.get("last_check_at"),
        "last_check_status": info.get("last_check_status"),
        "last_check_error": info.get("last_check_error"),
        "exit_ip": info.get("exit_ip") or "",
        "exit_location": info.get("exit_location") or "",
        "latency_ms": info.get("latency_ms"),
        "use_count": int(info.get("use_count") or 0),
    }
    entry["endpoint"] = f"{entry['host']}:{entry['port']}" if entry["host"] else ""
    return entry


class ProxyPool:
    """Google FX 代理号池：状态持久化 + 连通性检测 + 下发到 AdsPower 环境。"""

    # ── 基础增删改查 ──────────────────────────────

    def list_proxies(self) -> list:
        with _LOCK:
            state = _read_state()
        rows = [public_entry(pid, info) for pid, info in state["proxies"].items()]
        # 默认排序：可用的在前，其次未检测，失败/禁用沉底；同档按标签稳定排序。
        def _rank(row):
            if row["disabled"]:
                return 3
            if row["last_check_status"] == "failed":
                return 2
            if row["last_check_status"] != "ok":
                return 1
            return 0
        rows.sort(key=lambda r: (_rank(r), r["label"] or r["endpoint"]))
        return rows

    def get(self, proxy_id: str) -> Optional[dict]:
        with _LOCK:
            info = _read_state()["proxies"].get(str(proxy_id))
        return dict(info) if info else None

    def add_proxy(self, host: str, port, proxy_type: str = "http", user: str = "",
                  password: str = "", label: str = "", note: str = "",
                  proxy_id: str = "", bound_user_id: str = "",
                  keep_password: bool = False) -> dict:
        """新增或更新一条代理（带 proxy_id 即更新）。

        keep_password=True：编辑时前端没回传密码就沿用原密码（列表接口本来就
        不回传明文，不这样处理的话一次改备注就会把密码清空）。
        """
        host = str(host or "").strip()
        if not host:
            raise ValueError("代理地址不能为空")
        port = _clean_port(port)
        proxy_type = _clean_type(proxy_type)
        proxy_id = str(proxy_id or "").strip()

        with _LOCK:
            state = _read_state()
            existing = dict(state["proxies"].get(proxy_id, {})) if proxy_id else {}
            if proxy_id and not existing:
                raise KeyError("代理不存在")
            if not proxy_id:
                proxy_id = uuid.uuid4().hex[:12]
            resolved_password = str(password or "")
            if keep_password and not resolved_password:
                resolved_password = str(existing.get("password") or "")
            # 改了出口地址/账密 = 换了一条线路，旧的检测结论不再适用，清掉而不是留着
            # 让用户以为新地址已经验证过。
            endpoint_changed = (
                existing.get("host") != host
                or str(existing.get("port") or "") != port
                or existing.get("proxy_type") != proxy_type
                or str(existing.get("user") or "") != str(user or "").strip()
                or str(existing.get("password") or "") != resolved_password
            )
            entry = {
                "label": str(label or "").strip() or existing.get("label") or f"{host}:{port}",
                "proxy_type": proxy_type,
                "host": host,
                "port": port,
                "user": str(user or "").strip(),
                "password": resolved_password,
                "note": str(note or "").strip(),
                "disabled": bool(existing.get("disabled", False)),
                "bound_user_id": str(bound_user_id or existing.get("bound_user_id") or "").strip(),
                "applied_at": existing.get("applied_at"),
                "use_count": int(existing.get("use_count") or 0),
                "created_at": existing.get("created_at") or _now_iso(),
            }
            if endpoint_changed:
                entry.update({"last_check_at": None, "last_check_status": None,
                              "last_check_error": None, "exit_ip": "",
                              "exit_location": "", "latency_ms": None})
            else:
                entry.update({
                    "last_check_at": existing.get("last_check_at"),
                    "last_check_status": existing.get("last_check_status"),
                    "last_check_error": existing.get("last_check_error"),
                    "exit_ip": existing.get("exit_ip") or "",
                    "exit_location": existing.get("exit_location") or "",
                    "latency_ms": existing.get("latency_ms"),
                })
            state["proxies"][proxy_id] = entry
            _write_state(state)
        log(f"➕ 代理池新增/更新: {entry['label']} ({host}:{port}/{proxy_type})", "代理池")
        return public_entry(proxy_id, entry)

    def remove_proxy(self, proxy_id: str) -> bool:
        proxy_id = str(proxy_id or "").strip()
        with _LOCK:
            state = _read_state()
            if proxy_id not in state["proxies"]:
                return False
            label = state["proxies"][proxy_id].get("label") or proxy_id
            del state["proxies"][proxy_id]
            _write_state(state)
        log(f"➖ 代理池移除: {label}", "代理池")
        return True

    def set_disabled(self, proxy_id: str, disabled: bool) -> Optional[dict]:
        proxy_id = str(proxy_id or "").strip()
        with _LOCK:
            state = _read_state()
            if proxy_id not in state["proxies"]:
                return None
            state["proxies"][proxy_id]["disabled"] = bool(disabled)
            _write_state(state)
            info = dict(state["proxies"][proxy_id])
        log(f"{'🚫' if disabled else '✅'} 代理池{'禁用' if disabled else '启用'}: "
            f"{info.get('label') or proxy_id}", "代理池")
        return public_entry(proxy_id, info)

    # ── 连通性检测 ────────────────────────────────

    def check_proxy(self, proxy_id: str) -> Optional[dict]:
        """走这条代理请求一次出口 IP。成功记 ok + 真实 IP，失败如实记错误原因。

        不开浏览器、不排 FX 队列：一条代理通不通是网络层的事，没必要跟生成任务
        抢那把浏览器串行锁（账号积分探针那种非要读 Flow UI 的才必须排队）。
        """
        proxy_id = str(proxy_id or "").strip()
        with _LOCK:
            state = _read_state()
            if proxy_id not in state["proxies"]:
                return None
            entry = dict(state["proxies"][proxy_id])

        url = proxy_url(entry)
        proxies = {"http": url, "https": url}
        started = datetime.now(timezone.utc)
        exit_ip = location = ""
        status = "failed"
        error = None
        latency_ms = None
        try:
            resp = requests.get(_CHECK_URL, proxies=proxies, timeout=_CHECK_TIMEOUT)
            latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            resp.raise_for_status()
            data = resp.json()
            exit_ip = str(data.get("ip") or "").strip()
            location = " ".join(x for x in (data.get("city"), data.get("country")) if x)
            if exit_ip:
                status = "ok"
            else:
                error = "代理连通但没返回出口 IP，无法确认线路可用"
        except ImportError as e:
            # socks5 需要 PySocks；缺依赖是环境问题，别报成"代理坏了"。
            error = f"缺少 SOCKS 依赖（pip install requests[socks]）: {e}"
        except Exception as e:
            error = _short_error(e)

        checked_at = _now_iso()
        with _LOCK:
            state = _read_state()
            if proxy_id not in state["proxies"]:
                return None
            state["proxies"][proxy_id].update({
                "last_check_at": checked_at,
                "last_check_status": status,
                "last_check_error": error,
                "exit_ip": exit_ip,
                "exit_location": location,
                "latency_ms": latency_ms,
            })
            _write_state(state)
            info = dict(state["proxies"][proxy_id])
        if status == "ok":
            log(f"🌐 代理 {info.get('label')} 连通，出口 IP {exit_ip}"
                f"（{location or '未知位置'}，{latency_ms}ms）", "代理池")
        else:
            log(f"⚠️ 代理 {info.get('label')} 检测失败: {error}", "代理池")
        return public_entry(proxy_id, info)

    # ── 下发到 AdsPower 环境 ──────────────────────

    def apply_to_profile(self, proxy_id: str, user_id: str, port=None) -> dict:
        """把这条代理写进指定 AdsPower 环境的 userProxyConfig。

        AdsPower 的代理配置在浏览器**启动时**读取，所以已经开着的窗口不会换出口——
        调用方（控制台）会在提示里说明这一点，而不是假装立刻生效。
        """
        proxy_id = str(proxy_id or "").strip()
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("缺少 user_id（要下发到哪个 AdsPower 环境）")
        with _LOCK:
            state = _read_state()
            if proxy_id not in state["proxies"]:
                raise KeyError("代理不存在")
            entry = dict(state["proxies"][proxy_id])
        if entry.get("disabled"):
            raise ValueError("这条代理已被禁用，请先启用再下发")

        ads_port = port or get_runtime_default_port()
        resp = requests.post(
            f"http://127.0.0.1:{ads_port}/api/v1/user/update",
            json={"user_id": user_id, "user_proxy_config": to_adspower_config(entry)},
            timeout=15,
        ).json()
        if resp.get("code") != 0:
            raise RuntimeError(f"AdsPower 拒绝了代理下发: {resp.get('msg') or resp}")

        applied_at = _now_iso()
        with _LOCK:
            state = _read_state()
            if proxy_id in state["proxies"]:
                state["proxies"][proxy_id]["bound_user_id"] = user_id
                state["proxies"][proxy_id]["applied_at"] = applied_at
                state["proxies"][proxy_id]["use_count"] = int(
                    state["proxies"][proxy_id].get("use_count") or 0) + 1
                _write_state(state)
                entry = dict(state["proxies"][proxy_id])
        log(f"📡 代理 {entry.get('label')} 已下发到 AdsPower 环境 {user_id}"
            "（浏览器下次启动时生效）", "代理池")
        return public_entry(proxy_id, entry)

    # ── 轮换取号 ──────────────────────────────────

    def usable_proxies(self) -> list:
        """可参与轮换的代理：未禁用，且最近一次检测不是失败（没检测过也算可用，
        跟号池对"未探测账号"的处理一致——不知道不等于坏，但会如实标出来）。"""
        return [row for row in self.list_proxies()
                if not row["disabled"] and row["last_check_status"] != "failed"]

    def pick_proxy(self) -> Optional[dict]:
        """按游标轮转取一条可用代理（含密码，供 proxy_rotator 直接下发）。"""
        with _LOCK:
            state = _read_state()
            usable = [(pid, info) for pid, info in state["proxies"].items()
                      if not info.get("disabled") and info.get("last_check_status") != "failed"]
            if not usable:
                return None
            usable.sort(key=lambda item: item[1].get("created_at") or "")
            index = state.get("rotate_index", 0) % len(usable)
            proxy_id, info = usable[index]
            state["rotate_index"] = (index + 1) % len(usable)
            state["proxies"][proxy_id]["use_count"] = int(info.get("use_count") or 0) + 1
            try:
                _write_state(state)
            except ProxyPoolStateError:
                # 轮换游标写不下去只影响下一次从哪条开始，不该阻断本次取号。
                pass
            entry = dict(state["proxies"][proxy_id])
        entry["proxy_id"] = proxy_id
        return entry

    # ── 摘要与导入 ────────────────────────────────

    def summary(self) -> dict:
        rows = self.list_proxies()
        return {
            "total": len(rows),
            "enabled": sum(1 for r in rows if not r["disabled"]),
            "disabled": sum(1 for r in rows if r["disabled"]),
            "ok": sum(1 for r in rows if not r["disabled"] and r["last_check_status"] == "ok"),
            "failed": sum(1 for r in rows if not r["disabled"] and r["last_check_status"] == "failed"),
            "unchecked": sum(1 for r in rows
                             if not r["disabled"] and not r["last_check_status"]),
            "bound": sum(1 for r in rows if r["bound_user_id"]),
        }

    def import_legacy_txt(self) -> dict:
        """把 proxy_rotator list 模式那份 runtime/proxy_pool.txt 并进来。

        行格式 host:port 或 host:port:user:password（# 开头是注释）。已经在池子里的
        同 host:port 原样跳过，不覆盖用户改过的标签/备注与检测记录。
        """
        if not _LEGACY_TXT_FILE.exists():
            return {"added": 0, "skipped": 0, "total": 0,
                    "message": "runtime/proxy_pool.txt 不存在"}
        lines = []
        try:
            with open(_LEGACY_TXT_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f
                         if line.strip() and not line.strip().startswith("#")]
        except Exception as e:
            raise RuntimeError(f"读取 runtime/proxy_pool.txt 失败: {e}") from e

        with _LOCK:
            known = {f"{info.get('host')}:{info.get('port')}"
                     for info in _read_state()["proxies"].values()}

        added = skipped = 0
        for line in lines:
            parts = line.split(":")
            if len(parts) < 2:
                skipped += 1
                continue
            host, port = parts[0].strip(), parts[1].strip()
            if f"{host}:{port}" in known:
                skipped += 1
                continue
            try:
                self.add_proxy(
                    host=host, port=port,
                    user=parts[2].strip() if len(parts) >= 4 else "",
                    password=parts[3].strip() if len(parts) >= 4 else "",
                    note="从 runtime/proxy_pool.txt 导入",
                )
            except ValueError:
                skipped += 1
                continue
            known.add(f"{host}:{port}")
            added += 1
        log(f"📥 从 proxy_pool.txt 导入代理: 新增 {added} 条，跳过 {skipped} 条", "代理池")
        return {"added": added, "skipped": skipped, "total": len(lines)}
