# -*- coding: utf-8 -*-
"""
📒 Flow 媒体消费台账（跨任务防串图）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
记录每个 Flow 账号「已经被某次生成消费掉」的 media UUID，供下一次抓图时永久拉黑。

为什么需要它（2026-08-05 事故）：
任务 1785929405589（悬崖石屋）在提交后 1 秒就"网络捕获"到 UUID 6b857588 并落盘成
IMG 001——那张图其实是一小时前任务 1785925223144（榕树树洞）在**同一块画布**上生成
的历史图。当时四道防线全部失守：

  1. 新任务没有绑定 project，却复用了浏览器里还停着的旧任务画布（见
     google_fx_image._open_image_flow_canvas 的 require_fresh_canvas）；
  2. Flow 画布是虚拟化的，一小时前的 tile 早已卸载，提交前的 DOM 基线
     （pre_submit_tile_ids / pre_submit_dom_srcs / _get_panel_uuids）全都看不见它；
  3. 提交后画布重排，历史 tile 重新挂载，它的 tileId 不在基线里，于是被当成
     "本次新结果 tile"，直接绕过 _MIN_GENERATED_RESULT_AGE_SECONDS 的最小年龄闸；
  4. excluded_media_uuids 只装本任务 manifest 里的 UUID，近重复校验也只比本任务的帧。

前三道都依赖实时 DOM，都会被画布虚拟化绕过。本台账不碰 DOM：一个 UUID 一旦被任何
任务下载落盘，就再也不可能是"另一次生成的新结果"。这条判据与视口、滚动位置、
tile 是否挂载全部无关，是唯一不受虚拟化影响的证据。

状态文件 runtime/fx_media_ledger.json，与 account_pool.json 同目录、同「读 JSON →
改 → 原子替换」风格。
"""

import json
import os
import threading
from datetime import datetime, timezone

from ..config import AI_DIR
from .logger import log

_STATE_FILE = AI_DIR / "runtime" / "fx_media_ledger.json"
_LOCK = threading.Lock()

# 每账号保留多少条消费记录。一条约 120 字节，2000 条 ≈ 240KB/账号，足够覆盖
# 远超「上一个任务」的回溯窗口，又不会让文件无限长。超出后按写入顺序丢最旧的。
_MAX_UUIDS_PER_ACCOUNT = 2000

_SCHEMA_VERSION = 1


def is_enabled() -> bool:
    """台账可以整体关掉（排障用），默认开。"""
    raw = str(os.environ.get("SPARK_FX_MEDIA_LEDGER", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _normalize(value) -> str:
    return str(value or "").strip().lower()


def _read_state() -> dict:
    if not _STATE_FILE.exists():
        return {"version": _SCHEMA_VERSION, "accounts": {}}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # 台账读不出来只能退回"无历史"，不能把生成任务带崩：主防线（画布隔离）仍在。
        log(f"⚠️ 读取 Flow 媒体台账失败，本次按空台账处理: {e}", "GoogleFX")
        return {"version": _SCHEMA_VERSION, "accounts": {}}
    if not isinstance(data, dict):
        return {"version": _SCHEMA_VERSION, "accounts": {}}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    return {"version": _SCHEMA_VERSION, "accounts": accounts}


def _write_state(state: dict) -> bool:
    """写台账。失败只告警不抛：一张已经生成、下载、扣过积分的图不该因为记账失败作废。

    代价是这条记录丢了以后不再拦得住它——但画布隔离那条主防线不依赖本文件，
    所以这里的降级是"少一层兜底"，不是"回到事故状态"。
    """
    try:
        if not _STATE_FILE.parent.exists():
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = _STATE_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_file.replace(_STATE_FILE)
        return True
    except Exception as e:
        log(f"⚠️ Flow 媒体台账写盘失败（本次消费未记账，跨任务拦截会少一层）: {e}", "GoogleFX")
        return False


def consumed_uuids(account_id) -> set:
    """该账号历史上已被下载消费掉的全部 media UUID。"""
    account_id = _normalize(account_id)
    if not account_id or not is_enabled():
        return set()
    with _LOCK:
        state = _read_state()
    entries = (state["accounts"].get(account_id) or {}).get("consumed") or []
    return {
        _normalize(entry.get("uuid") if isinstance(entry, dict) else entry)
        for entry in entries
        if (entry.get("uuid") if isinstance(entry, dict) else entry)
    }


def record_consumed(account_id, media_uuid, context="") -> bool:
    """把一个刚落盘的 media UUID 记进台账。返回是否真的写成功。

    只在「下载成功 + 通过近重复校验」之后调用：捕获到但没落盘的 UUID 不算被消费，
    记进去反而会把同一次生成的重试结果误伤掉。
    """
    account_id = _normalize(account_id)
    media_uuid = _normalize(media_uuid)
    if not account_id or not media_uuid or not is_enabled():
        return False
    with _LOCK:
        state = _read_state()
        account = state["accounts"].setdefault(account_id, {})
        entries = account.get("consumed")
        if not isinstance(entries, list):
            entries = []
        if any(_normalize(e.get("uuid") if isinstance(e, dict) else e) == media_uuid
               for e in entries):
            return True
        entries.append({
            "uuid": media_uuid,
            "at": _now_iso(),
            "context": str(context or "")[:200],
        })
        if len(entries) > _MAX_UUIDS_PER_ACCOUNT:
            entries = entries[-_MAX_UUIDS_PER_ACCOUNT:]
        account["consumed"] = entries
        return _write_state(state)
