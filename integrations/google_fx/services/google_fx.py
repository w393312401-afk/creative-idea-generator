# -*- coding: utf-8 -*-
"""
🎨 Google FX 服务 (Veo 视频生成 + Imagen/Nano Banana 图片批量生成) — L3 媒介编排层
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
职责: 取消检测、网络响应捕获、去重锁、以及图片批量生成的顶层入口。
不含任何 DOM 操作或 FX 页面语义 —— 那些都在 google_fx_dom.py (L1) /
google_fx_helpers.py (L2) 里；本文件只编排，不碰页面。

📌 串行化归属（2026-08-01 厘清）—— 本文件的 _GOOGLE_FX_RUN_LOCK **不是**全局唯一
   的串行点，别再照着"三个顶层入口都要过这把锁"去改：

   - 有 SPARK 宿主时：真正的串行点是宿主的 FX 控制面队列
     （server._fx_browser_slot / fx_control.FxControlPlane，max_concurrent 被
     硬钳在 1，见 fx_control.LIMIT_SPEC）。frames 走 server.py 的
     _fx_serial_lock_for、videos 走 _fx_browser_slot(task_id, 'videos')，
     两条链因此已经互斥。队列层比这把裸 RLock 强的地方：可取消、有排队/执行
     超时、状态落盘、控制台可见。
   - 独立跑（无宿主、n8n 直连）时：browser_gate 未安装、队列不存在，
     _GOOGLE_FX_RUN_LOCK 才是唯一兜底。

   所以视频批量（google_fx_video.generate_videos_batch_google_fx，它自己的
   docstring 写明是「SPARK 图生视频的唯一入口」）不经过本文件的锁是**正确**的，
   不是漏网：它只在有宿主的场景下被调用，队列已经管住了。

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

from ..config import get_runtime_max_wait_seconds
from ..models import ImageBatchRequest
from ..utils.logger import log

# L2 (取消检测，本文件自身的去重锁代码需要用到；正常的 L3→L2 向下依赖，不构成循环)
from .google_fx_helpers import _check_cancelled

# 2026-08-01 清理：这里原有 _ARIA_CONTROLS_VIDEO_SUBMODE_MAP / _VALID_VIDEO_DURATIONS
# 两个常量和 4 段空的注释标题（函数体搬去 helpers.py 后留下的壳）。两个常量在全 repo
# 无引用——真正在用的同名口径在 google_fx_helpers.py 里，改这份不会有任何效果，
# 留着只会让人改错地方。


# ==============================================================================
# 🎬 Google FX (Veo 3.1) 视频生成
# ==============================================================================
# 这里曾有两个加锁/去重包装：_generate_video_google_fx（单条）与
# _generate_videos_batch_google_fx（批量）。两个都已删除（2026-08-01），因为：
#   1. 全 repo 零调用者。视频的真实入口是 SPARK →
#      google_fx_video.generate_videos_batch_google_fx(reqs, on_progress, cancel_check)。
#   2. 批量那个的签名已经和被包装函数对不上：_run_with_google_fx_dedupe 只往下传
#      单个 req，on_progress / cancel_check 会被丢掉——真接上去反而会让进度上报
#      和取消一起失效。它是 n8n VideoBatchRequest 端点时代的残留，而
#      VideoBatchRequest 本身也已无人使用。
#   3. 串行化本来就不归它管，见本文件顶部「串行化归属」。
# 视频生成的实现仍在 google_fx_video.py，未改动。


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

    entry["error"] 的约定（2026-08-01 补）：首个任务失败时**必须**写它。此前两条
    失败路径（抛异常 / 返回 status=failed）都只 pop key 不写 error，而 error 字段
    在全文件从未被赋值过——等待方于是醒来后 error/result 双 None，把 None 当结果
    返回。上层 isinstance 守卫会把它降级成"未知错误"，真实失败原因丢失，
    _classify_failure_for_switch 也因此拿到空串判成"无错误信息"、永不换号。
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
        # 现读等待上限：控制台调大「单张最长等待」后，重复请求的等待窗口要跟着放大，
        # 否则先到的那个还在正常生成、后到的却先超时报错。
        wait_timeout = _GOOGLE_FX_RUN_LOCK_WAIT_SECONDS + get_runtime_max_wait_seconds() + 60
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
                # 失败结果不进缓存（同指纹的下一次请求必须真的重跑），但**必须**把失败
                # 原因写进 entry：等待方持有的是同一个 entry 对象，只 pop 不写 error 的话
                # 它醒来时 error/result 双 None，于是把 None 当成结果返给上层。
                entry["error"] = result.get("message") or f"Google FX 请求失败: {label}"
                _GOOGLE_FX_INFLIGHT_REQUESTS.pop(key, None)
            else:
                entry["result"] = copy.deepcopy(result)
                entry["done_at"] = time.time()
        return result
    except Exception as e:
        with _GOOGLE_FX_DEDUP_LOCK:
            entry["error"] = str(e) or type(e).__name__
            _GOOGLE_FX_INFLIGHT_REQUESTS.pop(key, None)
        raise
    finally:
        entry["event"].set()


# ==============================================================================
# 🖼️ Google FX (Nano Banana 系列) 图片批量生成
# ==============================================================================
# 本文件仅剩这一个真实入口（frame_generator._fx_generate_batch 调用它）。

def _generate_images_batch_google_fx(req: ImageBatchRequest):
    return _run_with_google_fx_dedupe("image_batch", req, _generate_images_batch_google_fx_unlocked)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 📦 子模块入口向下兼容路由 (Route and Re-export)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# google_fx_video 的两个 _unlocked 别名不再从这里 re-export：它们此前只被上面那两个
# 已删除的包装用到，视频链的调用方一直是直接 import google_fx_video。

from .google_fx_image import (
    _generate_images_batch_google_fx_unlocked,
)
