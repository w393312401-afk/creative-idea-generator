# -*- coding: utf-8 -*-
"""
🎨 Google FX 服务 (Veo 视频生成 + Imagen/Nano Banana 图片批量生成) — L3 媒介编排层
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
职责: 取消检测、网络响应捕获、去重锁、以及视频/图片批量生成的三个顶层入口。
不含任何 DOM 操作或 FX 页面语义 —— 那些都在 google_fx_dom.py (L1) /
google_fx_helpers.py (L2) 里；本文件只编排，不碰页面。

🔒 LOCKED 2026-03-25 — find_fx_config_button / check_fx_config / fix_fx_config
   的定义已整体搬至 services/google_fx_helpers.py (函数体逐字未改动)。
   禁止在未获明确指示前修改这三个函数的任何逻辑；锁定约束随函数定义走。
"""

import os
import time
import threading
import copy
import hashlib
import json

from ..config import MAX_WAIT_SECONDS, OUTPUT_DIR
from ..models import VideoRequest, ImageBatchRequest
from ..utils.logger import log

# L2 (取消检测，本文件自身的去重锁代码需要用到；正常的 L3→L2 向下依赖，不构成循环)
from .google_fx_helpers import _check_cancelled


# ── 已知有效模型名 (2026-05-05) ──────────────────────────────────────────────

# ── 模型名简写 → 真实模型名 ────────────────────────────────────────────────


# ── aria-controls 结尾值 → 比例 Tab (Radix UI 固定业务值, 2026-05-05) ────────────
# ⚠️ 绝对不能用 id="radix-:rXX:" (每次刷新都变)，aria-controls 尾值由开发者定义不变

# ── 视频子模式 aria-controls 尾值 (2026-05-05 新增) ────────────────────────────
# 面板中 VIDEO 模式下的子 tab: 帧 (VIDEO_FRAMES) / 素材 (VIDEO_REFERENCES)
_ARIA_CONTROLS_VIDEO_SUBMODE_MAP = {
    "frames":          "VIDEO_FRAMES",
    "帧":              "VIDEO_FRAMES",
    "video_frames":    "VIDEO_FRAMES",
    "references":      "VIDEO_REFERENCES",
    "素材":            "VIDEO_REFERENCES",
    "video_references":"VIDEO_REFERENCES",
}

# ── 视频时长 aria-controls 尾值 (2026-05-05 新增，2026-07-19 补 10s) ─────────
# 面板中 VIDEO 模式下的时长选项: 4s / 6s / 8s / 10s（10s 仅 Omni Flash 模型提供，
# Veo 系列模型面板不显示该 tab，_click_video_duration_tab 找不到会跳过并留痕警告）
_VALID_VIDEO_DURATIONS = ["4", "6", "8", "10"]


# ==============================================================================
# 🔧 提取的模块级辅助函数 (原内部闭包，已提到模块级以支持独立测试/复用)
# ==============================================================================


# ==============================================================================
# 🔧 Google FX 底部工具栏配置工具函数 (适配 UI 大改版)
# ==============================================================================


# ==============================================================================
# 🖼️ 图生图：将已有 Flow 图片加为 Prompt 参考
# ==============================================================================


# ==============================================================================
# 🎬 Google FX (Veo 3.1) 视频生成
# ==============================================================================

def _generate_video_google_fx(req: VideoRequest):
    return _run_with_google_fx_dedupe("single_video", req, _generate_video_google_fx_unlocked)


# ==============================================================================
# 🎬 Google FX (Veo 3.1) 批量视频 — 多任务并行提交 & 统一监听
# ==============================================================================


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


_VIDEO_BATCH_FORCE_SERIAL = os.getenv("GOOGLE_FX_VIDEO_BATCH_FORCE_SERIAL", "1").strip().lower() not in ("0", "false", "no")
_GOOGLE_FX_RUN_LOCK_WAIT_SECONDS = _env_int("GOOGLE_FX_RUN_LOCK_WAIT_SECONDS", 1800, minimum=1)
_GOOGLE_FX_DEDUP_TTL_SECONDS = _env_int("GOOGLE_FX_DEDUP_TTL_SECONDS", 600, minimum=0)
_GOOGLE_FX_RUN_LOCK = threading.RLock()
_GOOGLE_FX_DEDUP_LOCK = threading.Lock()
_GOOGLE_FX_INFLIGHT_REQUESTS = {}


def _request_payload_for_dedupe(req):
    """Return a stable plain dict for request de-duplication."""
    if hasattr(req, "model_dump"):
        return req.model_dump()
    if hasattr(req, "dict"):
        return req.dict()
    return getattr(req, "__dict__", str(req))


def _google_fx_request_fingerprint(label: str, req) -> str:
    payload = _request_payload_for_dedupe(req)
    raw = json.dumps({"label": label, "payload": payload}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _purge_google_fx_dedupe_cache(now: float):
    if _GOOGLE_FX_DEDUP_TTL_SECONDS <= 0:
        return
    expired = [
        key for key, entry in _GOOGLE_FX_INFLIGHT_REQUESTS.items()
        if entry.get("done_at") and now - entry["done_at"] > _GOOGLE_FX_DEDUP_TTL_SECONDS
    ]
    for key in expired:
        _GOOGLE_FX_INFLIGHT_REQUESTS.pop(key, None)


def _run_with_google_fx_lock(label: str, fn, *args, **kwargs):
    """Serialize Google FX page automation; the shared Flow canvas is not concurrency-safe."""
    log(f"🔐 等待 Google FX 运行锁: {label}", "GoogleFX")
    _check_cancelled()
    acquired = _GOOGLE_FX_RUN_LOCK.acquire(timeout=_GOOGLE_FX_RUN_LOCK_WAIT_SECONDS)
    if not acquired:
        raise RuntimeError(f"Google FX run lock timeout after {_GOOGLE_FX_RUN_LOCK_WAIT_SECONDS}s: {label}")
    try:
        _check_cancelled()
        log(f"🔐 已获得 Google FX 运行锁: {label}", "GoogleFX")
        return fn(*args, **kwargs)
    finally:
        try:
            _GOOGLE_FX_RUN_LOCK.release()
            log(f"🔓 已释放 Google FX 运行锁: {label}", "GoogleFX")
        except RuntimeError:
            pass


def _mark_deduped(result, label, key, cached_age=None):
    """给"复用了别人的结果"的返回值打标。

    去重命中此前是完全静默的：调用方拿到一份 deepcopy 的旧结果，业务层看不出
    "这次没真跑"。用户重跑同一个分镜时会以为文件是新生成的。现在结果里带
    `deduped` 元数据，SPARK 侧据此在任务事件与控制台上标注。
    """
    if isinstance(result, dict):
        result["deduped"] = {
            "label": label,
            "fingerprint": key,
            "cached_age_seconds": cached_age,
            "note": "本次请求与近期请求指纹相同，直接复用了上一次的结果，未重新生成",
        }
    return result


def _run_with_google_fx_dedupe(label: str, req, fn):
    """
    Coalesce duplicate Google FX requests.

    n8n or an HTTP client may retry the same payload while the first UI run is
    still active. Without this guard, the same prompt can be submitted twice to
    Flow. Duplicates wait for the first run and reuse its result.

    ⚠️ 命中缓存的返回值会被 _mark_deduped 打上 `deduped` 元数据。想彻底关掉这层
    缓存把 GOOGLE_FX_DEDUP_TTL_SECONDS 设为 0（控制台的运行配置里可以热调）。
    """
    if _GOOGLE_FX_DEDUP_TTL_SECONDS <= 0:
        return _run_with_google_fx_lock(label, fn, req)

    key = _google_fx_request_fingerprint(label, req)
    short_key = key[:12]
    now = time.time()
    owner = False

    with _GOOGLE_FX_DEDUP_LOCK:
        _purge_google_fx_dedupe_cache(now)
        entry = _GOOGLE_FX_INFLIGHT_REQUESTS.get(key)
        if entry is None:
            entry = {
                "event": threading.Event(),
                "started_at": now,
                "done_at": None,
                "result": None,
                "error": None,
            }
            _GOOGLE_FX_INFLIGHT_REQUESTS[key] = entry
            owner = True
        elif entry.get("done_at"):
            age = round(now - entry["done_at"], 1)
            log(f"♻️ 命中重复 Google FX 请求缓存: {label} key={short_key}"
                f"（{age}s 前完成，TTL {_GOOGLE_FX_DEDUP_TTL_SECONDS}s）"
                "——本次**没有真的生成**，直接复用上次结果", "GoogleFX")
            if entry.get("error"):
                raise RuntimeError(str(entry["error"]))
            return _mark_deduped(copy.deepcopy(entry.get("result")),
                                 label=label, key=short_key, cached_age=age)
        else:
            log(f"♻️ 检测到重复 Google FX 请求，等待首个任务完成: {label} key={short_key}", "GoogleFX")

    if not owner:
        wait_timeout = _GOOGLE_FX_RUN_LOCK_WAIT_SECONDS + MAX_WAIT_SECONDS + 60
        if not entry["event"].wait(timeout=wait_timeout):
            raise RuntimeError(f"Duplicate Google FX request wait timeout: {label} key={short_key}")
        if entry.get("error"):
            raise RuntimeError(str(entry["error"]))
        return _mark_deduped(copy.deepcopy(entry.get("result")),
                             label=label, key=short_key, cached_age=None)

    try:
        result = _run_with_google_fx_lock(label, fn, req)
        with _GOOGLE_FX_DEDUP_LOCK:
            if isinstance(result, dict) and result.get("status") == "failed":
                _GOOGLE_FX_INFLIGHT_REQUESTS.pop(key, None)
            else:
                entry["result"] = copy.deepcopy(result)
                entry["done_at"] = time.time()
        return result
    except Exception as e:
        with _GOOGLE_FX_DEDUP_LOCK:
            _GOOGLE_FX_INFLIGHT_REQUESTS.pop(key, None)
        raise
    finally:
        entry["event"].set()


def _generate_videos_batch_google_fx(req):
    return _run_with_google_fx_dedupe("video_batch", req, _generate_videos_batch_google_fx_unlocked)


# ==============================================================================
# 🖼️ Google FX (Nano Banana 系列) 图片批量生成
# ==============================================================================

def _generate_images_batch_google_fx(req: ImageBatchRequest):
    return _run_with_google_fx_dedupe("image_batch", req, _generate_images_batch_google_fx_unlocked)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 📦 子模块入口向下兼容路由 (Route and Re-export)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from .google_fx_video import (
    _generate_video_google_fx_unlocked,
    _generate_videos_batch_google_fx_unlocked,
)

from .google_fx_image import (
    _generate_images_batch_google_fx_unlocked,
)
