# -*- coding: utf-8 -*-
"""
🎬 Google Flow 账号池管理器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
维护用户名下多个 AdsPower/Flow 账号的状态（命名、备注、已知积分、是否禁用/冷却），
并在每次批量视频生成前挑一个还有额度的账号，取代此前"只能手动指定单个
user_id"的用法（config.py 的 ADSPOWER_DEFAULT_USER_ID）。

积分数字只信 services.google_fx_credit.probe_flow_credit() 的真实探测结果
（打开 Flow 页面读 UI），本模块只做状态缓存/选号/持久化，不做"每条视频扣多少
积分"的估算记账——具体耗费因模型档位而异且未知（比如 "Lite [Lower Priority]"
干脆免积分），硬编码扣费只会引入假数据。

状态文件 runtime/account_pool.json，跟 proxy_rotator.py 的
runtime/generation_counter.json 同一目录、同样的"读 JSON → 改 → 写"风格。
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ..config import AI_DIR, get_runtime_default_port
from . import account_binding, account_credentials
from .logger import log

_STATE_FILE = AI_DIR / "runtime" / "account_pool.json"
_LOCK = threading.Lock()

# 新增账号的积分 = None（"未探测"），不是编造的乐观数字。
# 历史：这里曾经是 DEFAULT_CREDIT = 1000，和本模块开头声明的"积分数字只信真实探测"
# 直接矛盾——从未探测过的账号在控制台显示"积分 1000 / 可用"，用户以为有额度；
# pick_account 按积分降序排序时所有新号并列 1000，排序也失去意义。
UNPROBED_CREDIT = None
# 旧版 DEFAULT_CREDIT。只用于识别并清理历史状态文件里那个编造值（见 _read_state）。
_LEGACY_FABRICATED_CREDIT = 1000
STALE_AFTER_SECONDS = None  # 缓存不设固定超时期限，长期有效，直至进入不了 Flow 界面或显式刷新

# 选号排序时"未探测"账号的位置：排在已探测且有额度的账号之后、明确耗尽的之前。
# 它们会在 pick_account 里被强制真实探测一次，所以不需要乐观值来抢先。
_UNPROBED_SORT_KEY = -1


class AccountPoolStateError(RuntimeError):
    """号池状态写盘失败。禁用/冷却这类标记丢了会让选号重新选中坏账号，必须上报。"""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _read_state() -> dict:
    if not _STATE_FILE.parent.exists():
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _STATE_FILE.exists():
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"⚠️ 读取账号池状态失败，按空池处理: {e}", "账号池")
        return {}
    # 老数据迁移：曾经用一个 label 字段兼职"命名"和"备注"两种含义，
    # 现在拆成 name（账号命名，默认用邮箱）+ note（备注，选填）两个独立字段。
    for info in data.values():
        if not isinstance(info, dict):
            continue
        if "label" in info:
            info.setdefault("name", info.pop("label") or "")
        info.setdefault("note", "")
        # 老数据迁移（2026-07-26）：新增账号的积分默认值曾是编造的 1000
        # （DEFAULT_CREDIT），于是从未探测过的账号在控制台显示成"有 1000 额度、可用"。
        # 判据必须同时满足"值恰好是那个默认值"和"从来没探测成功过"——真实探测出
        # 1000 的账号一定带 last_checked_at，不会被误伤。
        if info.get("credit") == _LEGACY_FABRICATED_CREDIT and not info.get("last_checked_at"):
            info["credit"] = UNPROBED_CREDIT
        info.setdefault("image_task_count", 0)
        info.setdefault("video_task_count", 0)
    return data


def _write_state(state: dict):
    """写号池状态。失败必须抛出——不能静默吞。

    历史：这里 `except Exception` 只打一行日志。写盘失败时禁用/冷却标记回退到旧值，
    而调用方（set_disabled / mark_exhausted / mark_login_required）以为改成功了，
    下一次选号又会挑中刚被拉黑的账号，表现为"明明禁用了还在用"。
    """
    if not _STATE_FILE.parent.exists():
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = _STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_file.replace(_STATE_FILE)
    except Exception as e:
        log(f"❌ 写入账号池状态失败（状态未落盘）: {type(e).__name__}: {e}", "账号池")
        raise AccountPoolStateError(f"号池状态写盘失败，本次修改未生效: {e}") from e


class AccountPool:
    """Google Flow 账号池：状态持久化 + 选号 + 积分刷新。"""

    # ── 基础增删改查 ──────────────────────────────

    def list_accounts(self, heal: bool = True, sort_by: Optional[str] = None, sort_order: str = "desc") -> list:
        """列出号池账号。

        heal=False：跳过"用 AdsPower 邮箱/编号补命名与序号"的自愈。自愈要打 AdsPower 本地
        HTTP（含限频退避重试，最坏十几秒），只读类调用方——尤其是控制台每几秒
        轮询一次的状态快照——必须用 heal=False，否则 AdsPower 一卡，"只读状态"
        接口就跟着卡，控制台整体表现为假死。
        """
        with _LOCK:
            state = _read_state()

        # 老数据/新加账号的命名与环境编号自愈
        unnamed_or_noserial = [
            uid for uid, info in state.items()
            if not (info.get("name") or "").strip() or not (info.get("serial_number") or "").strip()
        ]
        if heal and unnamed_or_noserial:
            profile_infos = self._profile_info_map()
            healed = False
            for uid in unnamed_or_noserial:
                p_info = profile_infos.get(uid, {})
                if not (state[uid].get("name") or "").strip() and p_info.get("name"):
                    state[uid]["name"] = p_info["name"]
                    healed = True
                if not (state[uid].get("serial_number") or "").strip() and p_info.get("serial_number"):
                    state[uid]["serial_number"] = str(p_info["serial_number"])
                    healed = True
            if healed:
                try:
                    with _LOCK:
                        _write_state(state)
                except AccountPoolStateError:
                    # 命名自愈纯属显示优化，写不下去就下次再补，不要冒泡打断列表读取。
                    pass

        # 登录凭据存在另一个文件（runtime/account_credentials.json）里，这里只
        # 并入它的"有没有"视图。明文密码/TOTP 密钥永远不会进这个返回值——
        # 本方法的结果会被 /api/account-pool 整份 JSON 发给浏览器。
        # 一次性取全量而不是每行查一次：号池可能有几十个账号。
        try:
            credentials = account_credentials.presence_map()
        except Exception as e:
            log(f"⚠️ 读取账号凭据状态失败，号池列表按「都没配凭据」显示: {e}", "账号池")
            credentials = {}

        accounts = []
        for user_id, info in state.items():
            entry = dict(info)
            entry["user_id"] = user_id
            entry["serial_number"] = str(info.get("serial_number") or "")
            entry["image_task_count"] = int(info.get("image_task_count", 0))
            entry["video_task_count"] = int(info.get("video_task_count", 0))
            entry["task_count"] = entry["image_task_count"] + entry["video_task_count"]
            entry["credit_probed"] = entry.get("last_checked_at") is not None
            cred = credentials.get(user_id) or {}
            entry["login_email"] = cred.get("email") or ""
            entry["has_password"] = bool(cred.get("has_password"))
            entry["has_totp"] = bool(cred.get("has_totp"))
            # 能不能真的自动登录 = 邮箱和密码都齐了。TOTP 可选（有的号没开两步
            # 验证）；只填了邮箱不算，那会在密码页原地卡死。
            entry["auto_login_ready"] = bool(cred.get("email")) and bool(cred.get("has_password"))
            entry["auto_login_status"] = cred.get("auto_login_status")
            entry["auto_login_error"] = cred.get("auto_login_error")
            entry["auto_login_at"] = cred.get("auto_login_at")
            entry["auto_login_blocked"] = bool(cred.get("auto_login_blocked"))
            checked = _parse_iso(entry.get("last_checked_at"))
            if STALE_AFTER_SECONDS is not None and checked is not None:
                entry["credit_stale"] = (_now() - checked).total_seconds() >= STALE_AFTER_SECONDS
            else:
                entry["credit_stale"] = (checked is None)
            entry["credit_trustworthy"] = bool(
                entry.get("credit") is not None
                and not entry["credit_stale"]
                and entry.get("last_probe_status") != "failed"
            )
            accounts.append(entry)

        reverse = (str(sort_order or "").lower() == "desc")
        if sort_by == "credit":
            accounts.sort(key=lambda a: (a.get("credit") is not None, a.get("credit") or 0, a.get("name") or a["user_id"]), reverse=reverse)
        elif sort_by in ("tasks", "task_count"):
            accounts.sort(key=lambda a: (a.get("task_count", 0), a.get("name") or a["user_id"]), reverse=reverse)
        elif sort_by in ("image_task", "image_task_count"):
            accounts.sort(key=lambda a: (a.get("image_task_count", 0), a.get("name") or a["user_id"]), reverse=reverse)
        elif sort_by in ("video_task", "video_task_count"):
            accounts.sort(key=lambda a: (a.get("video_task_count", 0), a.get("name") or a["user_id"]), reverse=reverse)
        elif sort_by in ("serial", "serial_number"):
            def _sn_key(a):
                sn = str(a.get("serial_number") or "")
                return (int(sn) if sn.isdigit() else 999999, sn, a.get("name") or a["user_id"])
            accounts.sort(key=_sn_key, reverse=reverse)
        elif sort_by == "name":
            accounts.sort(key=lambda a: (a.get("name") or a["user_id"]), reverse=reverse)
        else:
            def _default_key(a):
                sn = str(a.get("serial_number") or "")
                return (int(sn) if sn.isdigit() else 999999, sn, a.get("name") or a["user_id"])
            accounts.sort(key=_default_key)
        return accounts

    def add_account(self, user_id: str, name: str = "", note: str = "", serial_number: str = "") -> dict:
        """新增或更新账号（按 user_id upsert，重命名/改备注复用同一入口）。

        name（账号命名）留空时：已有账号沿用旧命名，全新账号回退到 AdsPower
        环境的邮箱地址（查不到再退 user_id）。note（备注）是完全独立的可选
        字段，直接按传入值覆盖——留空即清空备注。
        serial_number（环境编号）留空时自动尝试从 AdsPower 环境列表获取。
        """
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id 不能为空")
        name = (name or "").strip()
        serial_number = str(serial_number or "").strip()

        with _LOCK:
            existing = dict(_read_state().get(user_id, {}))

        resolved_name = name or existing.get("name", "")
        resolved_serial = serial_number or str(existing.get("serial_number") or "")
        if not resolved_name or not resolved_serial:
            info_map = self._profile_info_map()
            p_info = info_map.get(user_id, {})
            if not resolved_name:
                resolved_name = p_info.get("name", "") or user_id
            if not resolved_serial:
                resolved_serial = str(p_info.get("serial_number") or "")

        with _LOCK:
            state = _read_state()
            state[user_id] = {
                "name": resolved_name,
                "note": (note or "").strip(),
                "serial_number": resolved_serial,
                "credit": existing.get("credit", UNPROBED_CREDIT),
                "image_task_count": int(existing.get("image_task_count", 0)),
                "video_task_count": int(existing.get("video_task_count", 0)),
                "last_checked_at": existing.get("last_checked_at"),
                "disabled": existing.get("disabled", False),
                "cooldown_until": existing.get("cooldown_until"),
                "last_probe_at": existing.get("last_probe_at"),
                "last_probe_status": existing.get("last_probe_status"),
                "last_probe_error": existing.get("last_probe_error"),
            }
            _write_state(state)
            entry = dict(state[user_id])
        entry["user_id"] = user_id
        log(f"➕ 账号池新增/更新账号: {user_id} 编号={resolved_serial} 命名={resolved_name}" +
            (f" 备注={note}" if note else ""), "账号池")
        return entry

    def record_task_count(self, user_id: str, image_count: int = 0, video_count: int = 0) -> Optional[dict]:
        """递增账号的图片与视频生成任务数量。"""
        user_id = (user_id or "").strip()
        if not user_id or (image_count <= 0 and video_count <= 0):
            return None
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return None
            info = state[user_id]
            info["image_task_count"] = max(0, int(info.get("image_task_count", 0)) + int(image_count))
            info["video_task_count"] = max(0, int(info.get("video_task_count", 0)) + int(video_count))
            _write_state(state)
            entry = dict(info)
        entry["user_id"] = user_id
        log(f"📊 账号 {user_id} 任务计数增加: 图片 +{image_count}, 视频 +{video_count} "
            f"(累计: 图片 {entry['image_task_count']}, 视频 {entry['video_task_count']})", "账号池")
        return entry

    def remove_account(self, user_id: str):
        with _LOCK:
            state = _read_state()
            if user_id in state:
                del state[user_id]
                _write_state(state)
        # 凭据存在另一个文件里，不跟着删就会在 runtime/ 下留一条谁也看不见、
        # 谁也不会再清理的明文密码——号池里已经没有这个账号，控制台自然也不会
        # 再显示"它还存着凭据"。
        try:
            account_credentials.remove(user_id)
        except Exception as e:
            log(f"⚠️ 移除账号 {user_id} 时未能清理其登录凭据（明文仍留在 "
                f"runtime/account_credentials.json）: {type(e).__name__}: {e}", "账号池")
        log(f"➖ 账号池移除账号: {user_id}", "账号池")

    def set_disabled(self, user_id: str, disabled: bool) -> Optional[dict]:
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return None
            state[user_id]["disabled"] = bool(disabled)
            _write_state(state)
            entry = dict(state[user_id])
        entry["user_id"] = user_id
        log(f"{'🚫' if disabled else '✅'} 账号池{'禁用' if disabled else '启用'}: {user_id}", "账号池")
        return entry

    def clear_cooldown(self, user_id: str) -> Optional[dict]:
        """解除登录失效/额度耗尽冷却，但不伪造积分或检查时间。"""
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return None
            state[user_id]["cooldown_until"] = None
            state[user_id].pop("cooldown_reason", None)
            _write_state(state)
            entry = dict(state[user_id])
        entry["user_id"] = user_id
        log(f"♻️ 账号 {user_id} 已解除冷却，等待下次真实积分探测", "账号池")
        return entry

    def close_browser(self, user_id: str, port=None) -> tuple[bool, str]:
        """关闭指定 user_id 的 AdsPower 浏览器实例。"""
        if not user_id or not user_id.strip():
            return False, "缺少 user_id"
        from .browser import close_ads_browser
        return close_ads_browser(user_id=user_id.strip(), port=port)

    # ── AdsPower 环境发现 ────────────────────────

    _PROFILE_PAGE_SIZE = 100
    _RATE_LIMIT_RETRIES = 4

    def list_adspower_profiles(self, port=None) -> list:
        """列出本机 AdsPower 里所有浏览器环境，供"添加账号"下拉框和一键导入用。

        翻页取全量（本地 API 单页上限 100）：只取第一页的老写法在环境超过 100 个
        时会静默漏掉后面的，一键导入全部就成了"只导入前 100 个"。AdsPower 本地
        API 有约 1 次/秒的限频，页间要停一下，否则第二页起会直接被拒。
        """
        port = port or get_runtime_default_port()
        profiles = []
        page = 1
        while True:
            resp = None
            # 限频是**瞬时**状态（别处刚好也在调本地 API，比如开/关浏览器），
            # 原来撞上就 break，等于把「列出全部环境」静默截断成前 N 个——
            # 一键导入会漏号，选号下拉框会少环境。退避重试几次再放弃。
            for retry in range(self._RATE_LIMIT_RETRIES):
                try:
                    resp = requests.get(
                        f"http://127.0.0.1:{port}/api/v1/user/list"
                        f"?page={page}&page_size={self._PROFILE_PAGE_SIZE}",
                        timeout=10,
                    ).json()
                except Exception as e:
                    log(f"⚠️ 查询 AdsPower 环境列表异常（第 {page} 页）: {type(e).__name__}: {e}", "账号池")
                    resp = None
                    break
                if resp.get("code") == 0:
                    break
                msg = str(resp.get("msg") or "")
                if "too many request" not in msg.lower():
                    break
                backoff = 1.2 * (retry + 1)
                log(f"⏳ AdsPower 本地 API 限频（第 {page} 页），{backoff:.1f}s 后重试 "
                    f"{retry + 1}/{self._RATE_LIMIT_RETRIES}", "账号池")
                time.sleep(backoff)
            if resp is None:
                break
            if resp.get("code") != 0:
                log(f"⚠️ 查询 AdsPower 环境列表失败（第 {page} 页）: {resp.get('msg')}", "账号池")
                break
            batch = resp.get("data", {}).get("list", []) or []
            profiles.extend(
                {
                    "user_id": p.get("user_id", ""),
                    "serial_number": str(p.get("serial_number", "") or "") if p.get("serial_number") is not None else "",
                    "name": p.get("name", ""),
                    "group_name": p.get("group_name", ""),
                }
                for p in batch
                if p.get("user_id")
            )
            if len(batch) < self._PROFILE_PAGE_SIZE:
                break
            page += 1
            time.sleep(1.1)  # AdsPower 本地 API 限频
        return profiles

    def import_adspower_profiles(self, port=None) -> dict:
        """把本机 AdsPower 里所有浏览器环境一次性并入号池。

        逐个环境挨着加太累，尤其环境有几十个时。已在池子里的环境原样跳过——
        不覆盖用户改过的命名/备注，也不重置积分缓存和禁用/冷却状态。新加的账号
        命名沿用 add_account() 的规则（AdsPower 环境的邮箱地址）。
        """
        profiles = self.list_adspower_profiles(port=port)
        if not profiles:
            log("⚠️ 一键导入：没拿到任何 AdsPower 环境（AdsPower 没开或本地 API 未启用？）", "账号池")
            return {"added": [], "skipped": [], "total": 0}

        with _LOCK:
            known = set(_read_state().keys())

        added, skipped = [], []
        for profile in profiles:
            user_id = profile["user_id"]
            if user_id in known:
                skipped.append(user_id)
                continue
            self.add_account(user_id, name=profile.get("name") or "", serial_number=profile.get("serial_number") or "")
            added.append(user_id)

        log(f"📥 一键导入 AdsPower 环境: 新增 {len(added)} 个，已存在跳过 {len(skipped)} 个", "账号池")
        return {"added": added, "skipped": skipped, "total": len(profiles)}

    def _profile_info_map(self) -> dict:
        """user_id -> AdsPower 环境的 info 字典（包含 name 和 serial_number）。"""
        try:
            return {
                p.get("user_id"): {
                    "name": p.get("name", ""),
                    "serial_number": p.get("serial_number", ""),
                }
                for p in self.list_adspower_profiles()
            }
        except Exception:
            return {}

    def _profile_name_map(self) -> dict:
        """user_id -> AdsPower 环境的 name 字段（Google 账号邮箱地址）。"""
        return {uid: info["name"] for uid, info in self._profile_info_map().items()}

    # ── 积分刷新 ──────────────────────────────────

    def refresh_credit(self, user_id: str, force: bool = False) -> Optional[dict]:
        """真实探测一次该账号的 Flow 积分并写回状态。探测失败保留旧值不覆盖。"""
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return None
            info = dict(state[user_id])

        last_checked = _parse_iso(info.get("last_checked_at"))
        if not force and last_checked is not None:
            if STALE_AFTER_SECONDS is None or (_now() - last_checked).total_seconds() < STALE_AFTER_SECONDS:
                entry = dict(info)
                entry["user_id"] = user_id
                return entry

        from ..services.google_fx_credit import (
            get_last_probe_error, get_last_probe_error_kind, probe_flow_credit,
        )
        credit = probe_flow_credit(user_id)
        attempted_at = _now_iso()
        # 探测压根没跑起来（浏览器被别人占着 / 排队超时 / 被取消）不是账号的问题，
        # 不能记成 last_probe_status='failed'——那会让一排好账号在控制台挂上
        # "⚠️ 最近探测失败"，还会让选号把它们当成不可信账号跳过。
        blocked = credit is None and get_last_probe_error_kind(user_id) == 'queue'

        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return None
            if credit is not None:
                state[user_id]["credit"] = credit
                state[user_id]["last_checked_at"] = attempted_at
                state[user_id]["last_probe_status"] = "ok"
                state[user_id]["last_probe_error"] = None
                # 探测成功说明登录是好的，所以「登录失效」那把冷却锁必须一起解开，
                # 不能只清 reason 留着 cooldown_until。自动登录让这个疏漏变得要命：
                # 探针撞上登录页 → 自动登进去 → 读到积分 → 账号明明已经可用，却因为
                # 冷却时间还没到，pick_account 继续跳过它整整两小时。
                # 只解 login_required 这一种：额度耗尽的 24h 冷却有它自己的语义，
                # 不该被一次积分探测顺手撤掉。
                if state[user_id].pop("cooldown_reason", None) == "login_required":
                    state[user_id]["cooldown_until"] = None
                log(f"🔎 账号 {user_id} 积分探测结果: {credit}", "账号池")
            elif blocked:
                err_msg = get_last_probe_error(user_id) or "浏览器忙，未能开始积分探测"
                state[user_id]["last_probe_status"] = "blocked"
                state[user_id]["last_probe_error"] = err_msg
                log(f"⏳ 账号 {user_id} 积分探测未开始: {err_msg}", "账号池")
            else:
                err_msg = get_last_probe_error(user_id) or "未能从账号菜单读取可信积分余额"
                state[user_id]["last_probe_status"] = "failed"
                state[user_id]["last_probe_error"] = err_msg
                log(f"⚠️ 账号 {user_id} 积分探测失败: {err_msg}，沿用旧值 {state[user_id].get('credit')}", "账号池")

                if any(kw in err_msg.lower() for kw in ("登录", "login", "sign in")):
                    cooldown_until = (_now() + timedelta(hours=2.0)).isoformat()
                    state[user_id]["cooldown_until"] = cooldown_until
                    state[user_id]["cooldown_reason"] = "login_required"
                    log(f"🔒 账号 {user_id} 积分探测遇到登录页面，自动标记为登录失效/人工处理冷却至 {cooldown_until}", "账号池")

            state[user_id]["last_probe_at"] = attempted_at
            _write_state(state)
            entry = dict(state[user_id])
        entry["user_id"] = user_id
        return entry

    def mark_exhausted(self, user_id: str, cooldown_hours: float = 24.0):
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return
            cooldown_until = (_now() + timedelta(hours=cooldown_hours)).isoformat()
            state[user_id]["credit"] = 0
            state[user_id]["cooldown_until"] = cooldown_until
            _write_state(state)
        log(f"🧊 账号 {user_id} 标记为额度耗尽，冷却至 {cooldown_until}", "账号池")

    def mark_login_required(self, user_id: str, cooldown_hours: float = 2.0):
        """账号登录失效/验证码/安全拦截等待人工处理超时：跟 mark_exhausted 一样
        进冷却（复用同一个 cooldown_until 字段，pick_account() 的过滤逻辑不用
        改），但不清零积分——登录问题跟额度无关，人工重新登录后额度还在，
        没必要按额度耗尽的 24 小时冷却。冷却时间短很多：给人工留出反应时间，
        避免选号立即又选回这个刚失效的账号，但也不至于把账号晾一整天。"""
        with _LOCK:
            state = _read_state()
            if user_id not in state:
                return
            cooldown_until = (_now() + timedelta(hours=cooldown_hours)).isoformat()
            state[user_id]["cooldown_until"] = cooldown_until
            state[user_id]["cooldown_reason"] = "login_required"
            _write_state(state)
        log(f"🔒 账号 {user_id} 登录失效等待人工处理超时，冷却至 {cooldown_until}", "账号池")

    # ── 选号 ──────────────────────────────────────

    def pick_account(self, min_credit: int = 1, exclude=None) -> Optional[dict]:
        """从池子里挑一个未禁用、不在冷却期、积分足够的账号。

        缓存过期（超过 STALE_AFTER_SECONDS）的候选会先真实刷新一次再判断，
        刷新后额度不够就跳到下一个候选，而不是直接放弃整个选号。

        exclude：本次要跳过的 user_id 集合，供「失败换号重试」排除当前账号和
        这一轮已经试过的账号用（见 switch_to_next_account）——这类排除只在
        一次生成里有效，不写进状态文件，不影响下一次生成的选号。
        """
        excluded = {str(u) for u in (exclude or ()) if u}
        with _LOCK:
            state = _read_state()
        now = _now()

        candidates = []
        for user_id, info in state.items():
            if user_id in excluded:
                continue
            if info.get("disabled"):
                continue
            cooldown_until = _parse_iso(info.get("cooldown_until"))
            if cooldown_until is not None and cooldown_until > now:
                continue
            candidates.append((user_id, dict(info)))

        # 未探测（credit=None）排在已探测有额度的账号之后：它们下一步会被强制真实
        # 探测一次，不该靠一个编造的乐观值抢到队首。
        def _sort_key(item):
            credit = item[1].get("credit")
            return _UNPROBED_SORT_KEY if credit is None else credit

        candidates.sort(key=_sort_key, reverse=True)

        for user_id, info in candidates:
            last_checked = _parse_iso(info.get("last_checked_at"))
            credit = info.get("credit")
            is_unprobed = (last_checked is None or credit is None)
            if STALE_AFTER_SECONDS is not None and last_checked is not None:
                is_stale = (_now() - last_checked).total_seconds() >= STALE_AFTER_SECONDS
            else:
                is_stale = is_unprobed

            if is_stale:
                refreshed = self.refresh_credit(user_id, force=True)
                if refreshed is not None:
                    info = refreshed
                if not refreshed or refreshed.get("last_probe_status") != "ok":
                    log(f"⏭️ 账号 {user_id} 初始积分探测未成功，跳过", "账号池")
                    continue

            credit = info.get("credit")
            if credit is None:
                # 探测失败且从来没探测成功过：不知道有没有额度，跳过而不是当成有。
                log(f"⏭️ 账号 {user_id} 积分仍未探测成功，跳过（不假设有额度）", "账号池")
                continue
            if credit >= min_credit:
                entry = dict(info)
                entry["user_id"] = user_id
                return entry

        return None


# ── 失败换号重试（2026-07-25）────────────────────────────────
# 各条生成链路（视频批量 / 图片批量 / FX 页面初始化）原先在失败重试前一律
# 强制换 IP。实测换 IP 只换出口地址，账号侧的风控评分不会跟着重置——被判
# "unusual activity" 的账号换个 IP 照样被判；反过来频繁换 IP 还会把 Flow 的
# 登录 token 打失效。所以统一改成换号：重试前切到号池里的下一个可用账号，
# IP 轮换交回 MIYA_ROTATE_THRESHOLD 配置的正常节奏（开浏览器前按请求数轮换）。
#
# 换号的落地方式（2026-07-26 改）：写 per-task 的 account_binding 上下文，而不是
# 改 ADSPOWER_DEFAULT_USER_ID 环境变量。utils/browser.get_ads_ws_url() 现在统一走
# account_binding.resolve_account()，所以下一轮重试重连浏览器时自然落到新账号上，
# 且**只影响当前执行上下文**。
#
# 旧写法（改 os.environ）是进程级副作用：换完不还原，后续所有任务——包括本来显式
# 指定了账号的——都跟着漂到新账号；两条链路同时换号还会互相覆盖；控制台"当前环境"
# 显示的值也会被悄悄改掉，用户看不出账号已经漂了。


def switch_to_next_account(exclude=(), min_credit: int = 1, stop_current: bool = True) -> Optional[dict]:
    """失败重试前换一个号池账号，返回新账号 dict；没号可换时返回 None。

    exclude：本轮已经试过的 user_id（当前账号会自动并入，不必重复传）。
    stop_current：换号前先把当前 AdsPower profile 的浏览器关掉——换号等于换
    profile，旧窗口不关会一直挂在那占内存，且下一轮连的是新 profile 的调试端口。

    返回 None 的两种情况都交由调用方原地重试（保持旧的"重试还是要跑"语义）：
    号池为空 / 没配账号，或池子里的号都被排除、禁用、冷却、额度不足。
    """
    try:
        from ..config import get_runtime_default_user_id, get_runtime_default_port, DEFAULT_USER_ID
    except Exception as e:  # config 理论上一定在，兜底避免换号异常炸穿重试循环
        log(f"⚠️ 换号失败（读运行时配置异常），本轮原地重试: {e}", "账号池")
        return None

    current = account_binding.resolve_account(
        fallback=get_runtime_default_user_id() or DEFAULT_USER_ID
    ).strip()
    skip = {str(u) for u in (exclude or ()) if u}
    if current:
        skip.add(current)

    try:
        chosen = AccountPool().pick_account(min_credit=min_credit, exclude=skip)
    except Exception as e:
        log(f"⚠️ 换号选号异常，本轮原地重试: {e}", "账号池")
        return None

    if chosen is None:
        log(f"⚠️ 号池里没有可换的账号（已排除 {len(skip)} 个），本轮沿用当前账号重试", "账号池")
        return None

    if stop_current and current:
        try:
            port = get_runtime_default_port()
            requests.get(
                f"http://127.0.0.1:{port}/api/v1/browser/stop?user_id={current}", timeout=10
            )
        except Exception as e:
            log(f"⚠️ 关闭旧账号浏览器失败（不阻塞换号）: {e}", "账号池")

    account_binding.set_task_account(chosen["user_id"])
    log(
        f"🔁 换号重试：{current or '(未指定)'} → {chosen['user_id']}"
        f"（{chosen.get('name') or '未命名'}，已知积分 "
        f"{chosen.get('credit') if chosen.get('credit') is not None else '未探测'}）"
        "［仅本任务上下文生效］",
        "账号池",
    )
    return chosen
