# -*- coding: utf-8 -*-
"""
🖼️ Google FX Image Service (Imagen Image Generation - Pure Execution)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import re
import time
import base64
import hashlib
import json
from collections import deque
import requests
from playwright.sync_api import sync_playwright

from ..config import (
    OUTPUT_DIR,
    get_runtime_default_user_id,
    get_runtime_max_wait_seconds,
)
from ..models import ImageBatchRequest
from ..utils.logger import log
from ..utils.browser import (
    random_sleep, clean_path, ensure_flow_workspace, _flow_project_crashed,
)
from ..utils.ui_helpers import inject_batch_image_observer
from ..utils import account_binding, cancel_flag, media_ledger

# L1 DOM 原语
from .google_fx_dom import _click_first_visible, _find_first_visible, _safe_press_escape
# L2 FX 页面语义 (含媒体捕获 / 输出目录 / 取消检测 / 代理轮换)
from .google_fx_helpers import (
    _check_cancelled,
    _cancellable_sleep,
    _switch_account_on_failure,
    _classify_failure_for_switch,
    _CAPTURED_DATA_MAXLEN,
    _normalize_ratio_value,
    _normalize_model_name,
    _verify_and_fix_fx_config,
    _connect_fx_page,
    _raise_if_manual_intervention_required,
    _prepare_fx_canvas,
    _get_panel_uuids,
    _count_error_cards,
    _make_response_handler,
    _ensure_output_dir,
    _clear_prompt_reference_chips_image,
    _add_flow_image_to_prompt,
    _upload_image_to_canvas_and_mount,
    _wait_for_flow_reference_ready,
    _find_fx_prompt_input,
    _mount_uuid_as_ref,
    click_fx_send_button,
    _get_prompt_reference_uuids,
    read_prompt_bar_state,
    fx_pacing_wait,
    fx_pacing_bounds,
    note_fx_submit,
    _click_new_project_button,
)


_MEDIA_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
_MIN_GENERATED_RESULT_AGE_SECONDS = 8.0
# How long a network candidate may stay un-corroborated before it is accepted on
# its own evidence.  Flow's canvas is virtualized: a result tile that lands
# outside the current viewport is never mounted, so waiting for DOM confirmation
# forever means waiting until the timeout.
_UNCONFIRMED_CANDIDATE_GRACE_SECONDS = 30.0
# 提交后的「画布水化窗口」：Flow 画布是虚拟化的，点下发送后它会把先前卸载掉的历史
# tile 重新挂载，这些 tile 的签名 URL 也会被重新拉一次；上一次提交刚落地、还没被
# 谁认领的结果也在其中。「捕获时刻离提交太近」是历史媒体唯一不会被画布重排伪装的
# 证据——tile 是否新建、DOM 是否新增这两条佐证恰恰都被重排伪装成「本次新结果」
# （2026-08-22 事故：IMG 012 在提交后 1 秒就"抓"回了 IMG 013 的候选图并落进槽位）。
#
# 两条线的窗口不同，取值来自全量日志里「提交 → 捕获」的实测分布：
#   · 单图 1x（660 次）：0/1/2s 各有若干（全是本类事故），3–6s 一次没有，
#     真结果集中在 10–14s；取 6s。
#   · 候选 x4（85 批）：真结果最快 15s，主群落在 20–44s；异常批则是 0/1/4/7/9s
#     的整齐爆发；取 12s。
_CANVAS_HYDRATION_WINDOW_SECONDS = 6.0
_CANDIDATE_HYDRATION_WINDOW_SECONDS = 12.0


class ReferenceMountError(RuntimeError):
    """A requested i2i reference could not be proven mounted in Flow.

    Frame-sequence callers must never silently fall back to text-to-image: one
    unreferenced submission breaks the lineage for every later frame in the
    chunk.  Keep a stable machine-readable prefix so the orchestration layer
    can choose a page-reset retry without mistaking this for account quota or
    risk control.
    """


def _reference_mount_error(message):
    return ReferenceMountError(f"REFERENCE_MOUNT_FAILED: {message}")


from .google_fx_credit import (
    is_credit_exhausted_message,
    is_image_quota_message,
    detect_page_credit_exhaustion,
)


def _is_login_required_failure(reason):
    text = str(reason or "").lower()
    return any(token in text for token in (
        "manual_required:login_required",
        "google 登录页面",
        "google login page",
        "sign in to google",
        "account_login_required",
    ))


def _cooldown_current_login_account(reason):
    """Persist account-specific login failure before switching away from it."""
    user_id = account_binding.resolve_account(fallback=get_runtime_default_user_id())
    if not user_id:
        return None
    try:
        from ..utils.account_pool import AccountPool
        AccountPool().mark_login_required(user_id)
        log(f"🔒 账号 {user_id} 登录失效，已进入冷却并准备换号: {reason}", "GoogleFX")
        return user_id
    except Exception as exc:
        log(f"⚠️ 账号 {user_id} 登录冷却写入失败: {exc}", "GoogleFX")
        return None


def _is_quota_failure(reason):
    text = str(reason or "").lower()
    return is_credit_exhausted_message(text) or any(token in text for token in (
        "quota_exhausted", "quota exhausted", "quota exceeded", "quota_exceeded",
        "resource_exhausted", "resource exhausted", "insufficient_credits",
        "insufficient credits", "not enough credits", "credits exhausted",
        "额度耗尽", "配额耗尽", "配额不足", "额度不足",
    ))


def _is_image_quota_failure(reason):
    """图片单日配额（≠ 账号积分耗尽）。判据收在 google_fx_credit 里统一维护——
    这里、server_common.failover_and_select_next_account、helpers
    ._classify_failure_for_switch 三处此前各抄一份 token 清单，改一处漏两处。"""
    return is_image_quota_message(reason)


def _record_current_generation_failure(reason):
    user_id = account_binding.resolve_account(fallback=get_runtime_default_user_id())
    if not user_id:
        return None
    try:
        from ..utils.account_pool import AccountPool
        pool = AccountPool()
        if _is_image_quota_failure(reason):
            pool.mark_image_quota_exceeded(user_id, error_detail=str(reason)[:200] or "图片余额超限")
        elif _is_quota_failure(reason):
            pool.mark_exhausted(user_id, reason="quota_exhausted", error_detail=str(reason)[:200])
        else:
            pool.record_generation_failure(user_id, reason)
        return user_id
    except Exception as exc:
        log(f"⚠️ 账号 {user_id} 生成失败状态写入失败: {exc}", "GoogleFX")
        return None


def _flow_project_id(url):
    """Return the ``/project/<id>`` segment of a Flow URL, or "" when route-less."""
    match = re.search(r"/project/([^/?#]+)", str(url or ""))
    return match.group(1).strip().lower() if match else ""


def _canvas_media_tile_count(page):
    """How many media tiles the canvas currently shows; -1 when unknown."""
    try:
        return int(page.evaluate("""() => {
            let count = 0;
            for (const tile of document.querySelectorAll('div[data-tile-id]')) {
                if (tile.querySelector('img, video')) count += 1;
            }
            return count;
        }"""))
    except Exception:
        return -1


def _create_fresh_flow_canvas(page, stale_url=""):
    """Open a brand-new canvas for a task that has not bound one yet.

    2026-08-05 事故的第一现场。AdsPower 的浏览器跨任务不关，新任务连上来时页面
    通常还停在**上一个任务**的 ``/fx/tools/flow/project/<id>`` 上。调用方拿不到
    project_url（这是新任务的正常状态）时若沿用当前页面，就等于把新任务直接跑进
    旧任务的画布：旧图在画布历史里，而抓图那边的全部基线都是实时 DOM 采样，看不见
    视口外没挂载的历史 tile，于是历史图会被当成本次结果落盘。

    所以这里只有一条路：回项目列表 → 新建项目 → **证明**拿到的确实不是原来那块。
    证不出来就报错中止，绝不"先跑着看"——串进去的帧要靠人眼才看得出来。
    """
    stale_project = _flow_project_id(stale_url)
    try:
        page.goto(
            "https://labs.google/fx/tools/flow",
            timeout=60000,
            wait_until="domcontentloaded",
        )
        random_sleep(1, 2)
    except Exception as nav_err:
        raise RuntimeError(f"Cannot open the Flow workspace: {nav_err}") from nav_err

    if not ensure_flow_workspace(page, timeout_seconds=30):
        raise RuntimeError(
            "FLOW_CANVAS_UNAVAILABLE: 未能从当前页面进入 Google Flow 工作台"
        )

    created = _click_new_project_button(page)
    if not created and _find_fx_prompt_input(page, announce=False) is None:
        raise RuntimeError(
            "FLOW_CANVAS_UNAVAILABLE: 无法为新任务创建可用的 Google Flow 画布"
        )

    # 等待新建项目后 URL 稳定变为 /project/<id> 或工具栏输入框出现
    for _ in range(8):
        current_url = str(getattr(page, "url", "") or "")
        fresh_project = _flow_project_id(current_url)
        if fresh_project:
            break
        random_sleep(0.2, 0.4)

    current_url = str(getattr(page, "url", "") or "")
    fresh_project = _flow_project_id(current_url)
    if stale_project and fresh_project == stale_project:
        raise RuntimeError(
            "FLOW_CANVAS_UNAVAILABLE: 新建画布后仍停在上一个任务的项目 "
            f"({stale_project[:8]}...)，已中止以免把历史帧混入本次任务"
        )

    if fresh_project:
        log(f"🆕 已为本次任务新建 Flow 画布: {fresh_project[:8]}...", "GoogleFX")
        return current_url

    # 路由里没有 /project/ 的 Flow 变体无法用 URL 自证。改用画布本身作证：
    # 全新项目必然是空的，还挂着媒体卡片就说明我们仍在历史画布上。
    for _ in range(3):
        tile_count = _canvas_media_tile_count(page)
        if tile_count <= 0:
            log("🆕 已为本次任务新建 Flow 画布（无项目路由，画布为空已确认）", "GoogleFX")
            return None
        random_sleep(1, 2)
    raise RuntimeError(
        "FLOW_CANVAS_UNAVAILABLE: 新建画布后画布上仍有历史媒体卡片，"
        "无法确认已离开上一个任务的画布，已中止以免把历史帧混入本次任务"
    )


def _enter_bound_project(page, requested_project_url):
    """Navigate back into the task's own canvas and **prove** we landed in it.

    若打开绑定画布失败、撞上崩溃页（Something went wrong）或未能进入已绑定项目
    （如被弹回工作台列表），不再重试，直接返回 None 由上层立即新建替代画布。
    """
    requested_pid = _flow_project_id(requested_project_url)

    def _landed():
        current = str(getattr(page, "url", "") or "")
        if requested_pid and _flow_project_id(current) != requested_pid:
            return None
        if _find_fx_prompt_input(page, announce=False) is None:
            return None
        return current

    try:
        page.goto(requested_project_url, timeout=60000, wait_until="domcontentloaded")
        random_sleep(1, 2)
    except Exception as nav_err:
        log(f"⚠️ 打开已绑定 Flow 画布失败: {nav_err}，不重试，直接新建画布", "GoogleFX")
        return None

    # 崩溃页直接放弃重试，由上层立即新建画布
    if _flow_project_crashed(page):
        log("⚠️ 已绑定 Flow 画布撞上崩溃页 (Something went wrong)，不重试，直接新建画布", "GoogleFX")
        return None

    # ensure_flow_workspace 返回 False = 这个项目本身进不了工作台（被删/失效）。
    # 返回 True 也不代表进对了地方：崩溃恢复"退回项目列表"同样算 ready，
    # 所以下面还要用 /project/<id> 核对落点。
    ready = ensure_flow_workspace(page, timeout_seconds=30)
    landed = _landed() if ready else None
    if landed is not None:
        return landed

    landed_pid = _flow_project_id(str(getattr(page, "url", "") or ""))
    log(
        f"⚠️ 未能进入已绑定画布（当前落点项目={landed_pid[:8] or '工作台列表'}），不重试，直接新建画布...",
        "GoogleFX",
    )
    return None


def _open_image_flow_canvas(page, requested_project_url=None, require_fresh_canvas=False):
    """Open a usable Flow image workspace and return its bindable project URL.

    Current Flow accounts can expose the generation workspace directly at
    ``/tools/flow`` without changing the address to a ``/project/...`` route.
    The prompt workspace is the source of truth in that case.  Requiring a
    project-shaped URL made L2 click the landing-page ``New project`` control
    even though a fully usable workspace was already open, then reject the
    successful route-less workspace as a failed click.

    ``require_fresh_canvas`` is what a task sets on its **first** batch: it has
    no canvas of its own yet, so whatever the shared browser is showing must be
    treated as another task's property, never adopted.  Later chunks of the same
    task leave it False and keep reusing the canvas they established.
    """
    requested_project_url = str(requested_project_url or "").strip()
    target_url = requested_project_url or "https://labs.google/fx/tools/flow"
    current_url = str(getattr(page, "url", "") or "")

    if require_fresh_canvas and not requested_project_url:
        return _create_fresh_flow_canvas(page, stale_url=current_url)

    # AdsPower keeps the browser/page alive between chunks.  Reconnecting CDP
    # does not require navigating again when the requested workspace is already
    # open and its prompt editor is usable; avoiding goto preserves canvas state
    # and removes a repeated page-load timeout from every five-frame chunk.
    requested_pid = _flow_project_id(requested_project_url)
    current_pid = _flow_project_id(current_url)
    same_project = bool(
        requested_project_url
        and (current_url == requested_project_url or (requested_pid and requested_pid == current_pid))
    )
    # ⚠️ "/fx/tools/flow" is a prefix of every project route, so this substring
    # test used to accept the previous task's `/fx/tools/flow/project/<id>` as a
    # "restored workspace" and bind it to the new task (2026-08-05 事故).  Only a
    # route-less workspace may be adopted without an explicit request.
    restored_workspace = bool(
        not requested_project_url
        and "/fx/tools/flow" in current_url
        and "/project/" not in current_url
    )
    if same_project or restored_workspace:
        # 刚跑完上一帧的画布常常还在收尾（结果 tile 挂载、编辑器重挂），此刻
        # _find_fx_prompt_input 会短暂返回 None。原来一旦没找到就直接落到下面的
        # page.goto——同一块画布被整页重载，i2i 的画布上下文与已挂载的参考图全部
        # 丢掉，每帧还要多付一次页面加载。这里先给它几秒钟稳定下来。
        editor = _find_fx_prompt_input(page, announce=False)
        if editor is None and same_project:
            # 只在"确知这块画布就是我们要的那块"时才等；route-less 的 restored_workspace
            # 没有编辑器时多半停在产品落地页，那条路要立刻去点 New project，不能白等。
            for _ in range(10):
                random_sleep(0.4, 0.6)
                editor = _find_fx_prompt_input(page, announce=False)
                if editor is not None:
                    break
        if editor is not None:
            current_url = str(getattr(page, "url", "") or "")
            log("♻️ 复用当前标签页里已打开的 Flow 画布（不刷新、不新建）", "GoogleFX")
            return current_url if "/project/" in current_url else None

    # _connect_fx_page() has already entered the Flow workspace.  The normal
    # workspace home has a visible "New project" button but no prompt editor.
    # Reloading the same /tools/flow URL here used to tear down that ready DOM;
    # one or two seconds later the button had not hydrated yet, so all three
    # attempts failed in ~4s with "Cannot create or open a usable Flow canvas".
    # Reuse the loaded page first and create the canvas from its current state.
    if restored_workspace:
        created = _click_new_project_button(page)
        if created or _find_fx_prompt_input(page, announce=False) is not None:
            current_url = str(getattr(page, "url", "") or "")
            return current_url if "/project/" in current_url else None

        # The page may still be a product landing/onboarding screen.  Let the
        # shared workspace entry routine finish that transition, then retry the
        # project action once without a destructive reload.
        if ensure_flow_workspace(page, timeout_seconds=30):
            if _find_fx_prompt_input(page, announce=False) is not None:
                current_url = str(getattr(page, "url", "") or "")
                return current_url if "/project/" in current_url else None
            created = _click_new_project_button(page)
            if created or _find_fx_prompt_input(page, announce=False) is not None:
                current_url = str(getattr(page, "url", "") or "")
                return current_url if "/project/" in current_url else None
        raise RuntimeError(
            "FLOW_CANVAS_UNAVAILABLE: Flow 工作台已打开，但无法进入或新建可用画布"
        )
    if requested_project_url:
        # 已绑定画布：进门要么成功、要么证明进不去，绝不把"被弹回工作台列表"
        # 当成进门成功（否则下面那句 _click_new_project_button 会把整条帧链
        # 换到一块空白新画布上）。
        landed = _enter_bound_project(page, requested_project_url)
        if landed is not None:
            return landed if "/project/" in landed else None
        # A persisted project can be deleted, expire, or become inaccessible to
        # the same account.  The local reference files are durable and will be
        # uploaded into a replacement canvas below, so a dead project URL must
        # not permanently brick the whole frame sequence.
        log(
            "⚠️ 已绑定 Flow 画布进入失败（崩溃页/被弹回列表/项目失效），"
            "直接新建替代画布：本帧起 i2i 续链断开，参考图将改用上传方式重新挂载",
            "GoogleFX",
        )
        # 绑定的项目打不开时，页面上剩下的那块画布不是"替代品"而是别人的东西
        # （多半就是上一个任务的）。只能新建，且要能证明确实换了一块。
        return _create_fresh_flow_canvas(page, stale_url=requested_project_url)

    try:
        page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        random_sleep(1, 2)
    except Exception as nav_err:
        raise RuntimeError(f"Cannot open the Flow workspace: {nav_err}") from nav_err

    workspace_ready = ensure_flow_workspace(page, timeout_seconds=30)
    if not workspace_ready:
        raise RuntimeError(
            "FLOW_CANVAS_UNAVAILABLE: 未能从当前页面进入 Google Flow 工作台"
        )

    # A visible prompt editor means Flow has already restored a generation
    # workspace.  This is also how the L1 diagnostics identify the current UI.
    if _find_fx_prompt_input(page, announce=False) is None:
        created = _click_new_project_button(page)
        # Some Flow variants create the workspace in-place and retain the same
        # URL.  Re-check the actual UI before treating the URL-only confirmation
        # failure as a real creation failure.
        if not created and _find_fx_prompt_input(page, announce=False) is None:
            raise RuntimeError(
                "FLOW_CANVAS_UNAVAILABLE: 无法创建或打开可用的 Google Flow 画布"
            )

    current_url = str(getattr(page, "url", "") or "")
    return current_url if "/project/" in current_url else None


def _extract_media_uuid(value):
    """Return the Flow media UUID embedded in a redirect URL or local filename."""
    match = _MEDIA_UUID_RE.search(str(value or ""))
    return match.group(1).lower() if match else ""


def _is_blocked_media_candidate(url, blocked_uuids):
    """Reject media that existed before submit or is mounted as a prompt reference."""
    media_uuid = _extract_media_uuid(url)
    blocked = {str(value).lower() for value in (blocked_uuids or ()) if value}
    return bool(media_uuid and media_uuid in blocked)


def _absolute_media_url(src):
    """把 tile 扫描拿到的 img src 转成服务端可下载的绝对 URL；取不到就返回 ""。

    只有两种 src 能下载：已经是绝对的 http(s)，以及「/」开头的 labs.google 站内相对
    路径。`blob:` / `data:image/` 是页面内存对象，_download_image 那边的 requests /
    browser fetch 都取不到——而 tile 扫描的 looksLikeMedia 现在恰恰会放它们进来。
    """
    src = str(src or "").strip()
    if not src:
        return ""
    lowered = src.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return src
    if src.startswith("/"):
        return f"https://labs.google{src}"
    return ""


def _is_generated_candidate_stable(submit_ts, *, now=None, confirmed_new_tile=False):
    """Whether a candidate has enough evidence to belong to this submission.

    A media URL inside a tile that did not exist before Send is already tied to
    the current submission.  Applying the generic hydration grace period to
    that URL used to permanently blacklist genuinely fast results: the image
    was visible in Flow, but the worker kept logging ``等待图片 URL`` until its
    timeout.  The age guard remains useful only for unassociated global/network
    candidates.

    ⚠️ This is a *deferral* signal, never a rejection: Nano Banana Lite returns
    in 6–10s, i.e. right across this threshold, so "arrived early" says nothing
    about whether the media is history.  Only the pre-submit baselines
    (panel UUIDs / prompt references / caller exclusions) may block a candidate.
    """
    if confirmed_new_tile:
        return True
    current_time = time.time() if now is None else now
    return current_time >= submit_ts + _MIN_GENERATED_RESULT_AGE_SECONDS


def _canvas_hydration_window_seconds(candidate_mode=False):
    """水化窗口秒数。SPARK_FX_HYDRATION_WINDOW 可覆盖两条线，置 0 关掉该判据。"""
    default = (_CANDIDATE_HYDRATION_WINDOW_SECONDS if candidate_mode
               else _CANVAS_HYDRATION_WINDOW_SECONDS)
    raw = os.environ.get("SPARK_FX_HYDRATION_WINDOW")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _is_canvas_hydration_capture(capture_ts, submit_ts, *, window=None,
                                 candidate_mode=False):
    """这条观测是不是「提交后画布重排」顺带拉回来的历史媒体。

    与 _is_generated_candidate_stable 的关键差别在于判据落在**观测时刻**而不是
    **判定时刻**：后者只要多等几轮就自动放行，于是提交后 0 秒抓到的历史图熬过 8s
    最小年龄闸照样会被收下——2026-08-22 事故正是这么发生的。观测时刻不会随等待改变。
    """
    if window is None:
        window = _canvas_hydration_window_seconds(candidate_mode)
    if window <= 0 or capture_ts is None or not submit_ts:
        return False
    return capture_ts < submit_ts + window


def _unconfirmed_candidate_is_acceptable(first_seen_ts, *, now=None,
                                         grace=_UNCONFIRMED_CANDIDATE_GRACE_SECONDS):
    """Whether a never-corroborated network candidate may be accepted anyway.

    Requiring DOM corroboration for every candidate assumes the generated tile
    always reaches the DOM.  It does not: Flow virtualizes the canvas, so a
    result dropped outside the viewport stays unmounted and the worker times
    out on an image that Flow finished minutes ago.  A candidate that survived
    every pre-submit baseline, first appeared after Send and still stands after
    this grace period is the best evidence available — and the near-duplicate
    guard downstream still rejects an accidentally captured reference.
    """
    current_time = time.time() if now is None else now
    return current_time - first_seen_ts >= grace


def _images_are_near_duplicates(candidate_path, reference_paths, threshold=3.0):
    """Last-resort guard against accepting a recompressed prompt reference as output."""
    try:
        from PIL import Image, ImageChops, ImageStat

        with Image.open(candidate_path) as candidate_image:
            candidate = candidate_image.convert("RGB")
            for reference_path in reference_paths or ():
                if not reference_path or not os.path.exists(reference_path):
                    continue
                with Image.open(reference_path) as reference_image:
                    reference = reference_image.convert("RGB")
                    if reference.size != candidate.size:
                        reference = reference.resize(candidate.size)
                    channel_means = ImageStat.Stat(
                        ImageChops.difference(candidate, reference)
                    ).mean
                    mean_abs_difference = sum(channel_means) / max(len(channel_means), 1)
                    if mean_abs_difference <= threshold:
                        return True, mean_abs_difference, reference_path
    except Exception as exc:
        log(f"  ⚠️ 参考图近重复校验失败: {type(exc).__name__}: {exc}", "GoogleFX")
    return False, None, None

# ── _generate_images_batch_google_fx_unlocked ──
def _generate_images_batch_google_fx_single_attempt(req: ImageBatchRequest):
    _check_cancelled()
    browser = None
    captured_data = deque(maxlen=_CAPTURED_DATA_MAXLEN)
    result = {"status": "failed", "image_urls": [], "message": "", "project_url": None}

    # Check max limitation
    prompts = req.prompts[:5]
    if not prompts:
        result["message"] = "No prompts provided"
        return result

    # 候选 x4 与单图 1x 的真实生成延迟差一倍，水化窗口按模式取（见窗口常量处的实测分布）
    is_candidate_request = bool(getattr(req, "is_candidate_mode", False))

    log(f"🚀 Google FX 批量生图请求: {len(prompts)} 个, 首个: {prompts[0][:30]}...", "GoogleFX")
    # 模型名归一化：将任意别名/错误名称映射为真实模型
    req.model = _normalize_model_name(req.model, is_video=False)
    log(f"  ℹ️ 实际使用模型: {req.model}", "GoogleFX")

    try:
        with sync_playwright() as p:
            browser, page = _connect_fx_page(
                p,
                allow_account_switch=bool(
                    getattr(req, "allow_account_switch", True)
                ),
            )

            require_fresh_canvas = bool(
                getattr(req, "require_fresh_canvas", False)
                and not getattr(req, "project_url", None)
            )
            project_url = _open_image_flow_canvas(
                page, getattr(req, "project_url", None),
                require_fresh_canvas=require_fresh_canvas,
            )

            # 🛠️ 0. 新建/清理画布
            _has_ref_images = bool([r for r in (req.images or []) if r and os.path.exists(clean_path(str(r)))])
            # 画布刚为本次任务新建出来时，绝不允许 _prepare_fx_canvas 的"优先打开最新
            # 历史项目"兜底把我们送回上一个任务的画布。
            _prepare_fx_canvas(
                page, has_refs=_has_ref_images,
                require_fresh_canvas=require_fresh_canvas,
            )
            if project_url:
                result["project_url"] = project_url
                req.project_url = project_url
            elif require_fresh_canvas:
                # 无 /project/ 路由的 Flow 变体：画布已经新建好了，但没有 URL 可以带回去。
                # 就地清掉这面旗，本请求后续的重试才会复用它，而不是每次重试都重开画布。
                req.require_fresh_canvas = False


            # 🛠️ 2. 验证配置 (Image 模式)
            target_count_cfg = getattr(req, "generation_count", None) or ("4x" if getattr(req, "is_candidate_mode", False) else "1x")
            selected_ratio = _verify_and_fix_fx_config(
                page, model=req.model, ratio=req.ratio,
                want_video=False, context_label="图片生成",
                count=target_count_cfg,
            )
            # 2026-08-01 清理：此处原有一个 36 行的 _wait_for_new_panel_image() 闭包
            # （轮询 add_2 面板等新 UUID、带 cancel chip 早退哨兵）。它定义了但从未被
            # 调用过——参考图就绪判定实际走的是 _wait_for_flow_reference_ready()。

            # 🛠️ 2.4 准备并彻底清空编辑器，删除所有历史文本与历史参考图
            input_el = _find_fx_prompt_input(page, announce=True)
            if not input_el:
                raise Exception("无法找到输入框")
            log("🧹 正在彻底清空输入框及历史参考图...", "GoogleFX")
            # 先清掉挂在编辑器之外「素材槽」里的历史参考图 chip
            # （Ctrl+A/Backspace 只能清空编辑器内部，够不到这些）
            _clear_prompt_reference_chips_image(page)
            # 上面的 Clear prompt 已经把文字和参考图一起清了；只有它没生效时才动键盘。
            # ⚠️ 判空必须走 read_prompt_bar_state（看 Slate 有没有渲染 placeholder），
            # 不能用 input_el.inner_text()：空编辑器里 placeholder
            # 「What do you want to create?」本身就有 29 个字符，用 innerText 判空
            # 会永远得到「还剩 29 字」，把每一轮都拖进无谓的兜底清空。
            try:
                if not read_prompt_bar_state(page).get("editor_empty"):
                    input_el.click()
                    random_sleep(0.3, 0.5)
                    page.keyboard.press("ControlOrMeta+a")
                    random_sleep(0.1, 0.2)
                    page.keyboard.press("Backspace")
                    random_sleep(0.3, 0.5)
                    if not read_prompt_bar_state(page).get("editor_empty"):
                        log("  ⚠️ 键盘清空未生效，改用 fill('') 兜底", "GoogleFX")
                        input_el.fill("")
                        random_sleep(0.2, 0.3)
            except Exception as e:
                log(f"  ⚠️ 清空输入框异常: {e}", "GoogleFX")

            ref_image_refs = [clean_path(str(r)) for r in (req.images or []) if r]
            # ⚠️ 去重：防止 n8n 传入重复路径导致同一图片多次处理
            valid_refs = list(dict.fromkeys(p for p in ref_image_refs if os.path.exists(p)))
            if valid_refs:
                log(f"🖼️ 检测到 {len(valid_refs)} 张参考图，从文件名提取 UUID 并在画布挂载...", "GoogleFX")
                mounted_count = 0
                for local_path in valid_refs:
                    _uuid_m = re.search(
                        r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                        os.path.basename(local_path)
                    )
                    _ref_uuid = _uuid_m.group(1) if _uuid_m else ""
                    _ok = False
                    if _ref_uuid:
                        log(f"  📎 参考图 UUID: {_ref_uuid[:16]}... ({os.path.basename(local_path)})", "GoogleFX")
                        _ok = _add_flow_image_to_prompt(page, _ref_uuid)
                    else:
                        log(f"  ⚠️ 文件名无法提取 UUID，直接走上传挂载: {os.path.basename(local_path)}", "GoogleFX")
                    if not _ok:
                        # ── 上传回退（2026-07-25）──
                        # UUID 挂载是「在当前画布上找那张 tile」，因此天然绑定当前登录的
                        # Google 账号：换了 AdsPower 环境（=换账号=换 Flow 画布）、页面刷新
                        # 清空画布、历史项目被换掉，这张 tile 都不在了，原来会直接降级成
                        # 「以无参考模式生成」——调用方拿到的是一张断链的图，且毫无察觉。
                        # 上传不绑账号：把调用方留在本地的这张原图传进当前画布再挂，链式
                        # 参考就能跨账号接上（视频侧一直是纯上传，所以从来没有这个问题）。
                        log(f"  ↻ 画布上挂不到该参考图，改用上传方式: {os.path.basename(local_path)}", "GoogleFX")
                        _ok = _upload_image_to_canvas_and_mount(page, local_path)
                        # 上传成功会把新 tile 的 UUID 交回来。带回给调用方留档，
                        # 下一次引用同一张参考图就能直接挂画布，不必再传一遍。
                        if isinstance(_ok, str) and _ok:
                            result.setdefault('uploaded_reference_uuids', {})[local_path] = _ok
                    if _ok:
                        mounted_count += 1
                        _rr, _rs = _wait_for_flow_reference_ready(page, timeout_seconds=15, settle_range=(0.5, 1.0))
                        if _rr:
                            log(f"  ✅ 参考图已挂入编辑器 (sel={_rs!r})", "GoogleFX")
                        else:
                            log(f"  ⚠️ 挂载后未检测到 cancel chip，继续", "GoogleFX")
                    else:
                        log(f"  ❌ 参考图挂载失败，UUID 与上传回退均未成功: "
                            f"{os.path.basename(local_path)}", "GoogleFX")

                if mounted_count != len(valid_refs):
                    raise _reference_mount_error(
                        f"请求挂载 {len(valid_refs)} 张参考图，实际仅确认 {mounted_count} 张；"
                        "已禁止降级为无参考生成"
                    )
                log(f"✅ 参考图处理完毕 ({mounted_count}/{len(valid_refs)} 张成功)", "GoogleFX")

            elif ref_image_refs:
                raise _reference_mount_error(
                    f"参考图本地文件不存在: {ref_image_refs}"
                )

            # 🛠️ 3. 准备 Slate.js 编辑器
            # input_el 是 Locator（惰性解析），上面 2.4 节已经取过一次，中间的清 chip /
            # 挂参考图都不会让它失效，不必再全选择器扫一遍 DOM。
            existing_imgs = set()
            try:
                existing_imgs = set(page.evaluate("""() => Array.from(document.images || [])
                    .map(img => img.currentSrc || img.src || '')
                    .filter(Boolean)"""))
            except Exception as e:
                log(f"  ⚠️ 获取现有图片列表失败: {type(e).__name__}", "GoogleFX")

            inject_batch_image_observer(page, existing_imgs)
            log("🔍 准备生成并监控网络...", "GoogleFX")

            # ━━━ 网络拦截: 监听图片生成相关的网络响应 ━━━
            handle_response = _make_response_handler(captured_data, mode="image")
            page.on("response", handle_response)
            # （这里原有一个 submit_ts = time.time()，赋值后从未被读取——每轮真正用的是
            #   循环里各自的 submit_ts_this。已删。）

            # 🛠️ 4+5. 串行提交 + 等待 + 参考图链式挂载
            # 流程: 提交 prompt[0] → 等第0张图出现在 add_2 面板 → 选中它 → 提交 prompt[1] → ...
            # 这样保证每张图生成完毕才触发下一张，实现真正的图生图链式生成。


            # ── 辅助: 只填写文字（不发送，供后续挂参考后再 submit）──
            def _fill_text_only(prompt_text, idx, has_ref=False):
                """
                把 prompt 写入编辑器，返回 True/False，不触发发送。
                has_ref=True: 编辑器里已经挂了参考图 chip，必须禁止 fill() / Meta+a
                             （两者都会清空整个编辑器内容，把 chip 一起删掉），
                             改为 click→End→keyboard.type() 追加文字。
                has_ref=False: 正常流程，允许 fill() 先清空再写入。
                """
                prompt_filled = False

                # ── 有参考图：只用追加方式，不能清空编辑器 ──
                if has_ref:
                    # 方法A: click → End → 直接 keyboard.insert_text() 追加
                    # ⚠️ 注意：绝对不能用 fill() / Meta+a / selectAll / Backspace
                    #   以上任何清空操作都会把编辑器里的参考图 chip 一起删掉
                    try:
                        input_el.click(); random_sleep(0.3, 0.5)
                        page.keyboard.press("End"); random_sleep(0.1, 0.2)
                        # 使用 insert_text 瞬间粘贴内容，避免逐字敲打的缓慢
                        page.keyboard.insert_text(prompt_text); random_sleep(0.3, 0.5)

                        # 特殊保护：因为前面是输入文字，可能没触发 react 状态更新，按个空格触发一下
                        page.keyboard.press("Space")

                        if prompt_text[:15].lower() in input_el.inner_text().strip().lower():
                            prompt_filled = True
                            log(f"✅ keyboard.insert_text() 追加写入 (有参考图): 第 {idx+1} 个", "GoogleFX")
                    except Exception as e:
                        log(f"⚠️ keyboard.insert_text() 追加失败: {e}", "GoogleFX")

                    # 方法B: execCommand insertText 追加（fill 的 no-clear 替代）
                    if not prompt_filled:
                        try:
                            input_el.click(); random_sleep(0.2, 0.3)
                            page.evaluate("""(text) => {
                                const editor = document.querySelector('[data-slate-editor=true]');
                                if (editor) {
                                    editor.focus();
                                    // 只选文字节点，不选 chip 内联元素
                                    const sel = window.getSelection();
                                    if (sel && sel.rangeCount) {
                                        const r = sel.getRangeAt(0);
                                        r.collapse(false);  // 移到末尾
                                    }
                                }
                                document.execCommand('insertText', false, text);
                            }""", prompt_text)
                            random_sleep(0.3, 0.5)
                            if prompt_text[:15].lower() in input_el.inner_text().strip().lower():
                                prompt_filled = True
                                log(f"✅ execCommand insertText (有参考图): 第 {idx+1} 个", "GoogleFX")
                        except Exception as e:
                            log(f"⚠️ execCommand (有参考图) 失败: {e}", "GoogleFX")

                    if not prompt_filled:
                        log(f"❌ 第 {idx+1} 个 prompt 所有追加方法均失败 (有参考图模式)", "Error")
                    return prompt_filled

                # ── 无参考图：原始三方法，fill() 优先 ──
                # 方法1: fill()
                try:
                    input_el.click(); random_sleep(0.3, 0.5)
                    input_el.fill(prompt_text); random_sleep(0.2, 0.4)
                    if prompt_text[:15].lower() in input_el.inner_text().strip().lower():
                        prompt_filled = True
                        log(f"✅ fill() 写入: 第 {idx+1} 个", "GoogleFX")
                except Exception as e:
                    log(f"⚠️ fill() 失败: {e}", "GoogleFX")
                # 方法2: keyboard.type()
                if not prompt_filled:
                    try:
                        input_el.click(); random_sleep(0.2, 0.3)
                        page.keyboard.press("ControlOrMeta+a"); page.keyboard.press("Backspace")
                        random_sleep(0.2, 0.3)
                        page.keyboard.type(prompt_text, delay=15); random_sleep(0.3, 0.5)
                        if prompt_text[:15].lower() in input_el.inner_text().strip().lower():
                            prompt_filled = True
                            log(f"✅ keyboard.type() 写入: 第 {idx+1} 个", "GoogleFX")
                    except Exception as e:
                        log(f"⚠️ keyboard.type() 失败: {e}", "GoogleFX")

                # 方法3: execCommand()
                if not prompt_filled:
                    try:
                        input_el.click(); random_sleep(0.2, 0.3)
                        page.evaluate("""(text) => {
                            document.execCommand('selectAll', false, null);
                            document.execCommand('insertText', false, text);
                        }""", prompt_text)
                        random_sleep(0.3, 0.5)
                        if prompt_text[:15].lower() in input_el.inner_text().strip().lower():
                            prompt_filled = True
                            log(f"✅ execCommand 写入: 第 {idx+1} 个", "GoogleFX")
                    except Exception as e:
                        log(f"⚠️ execCommand 失败: {e}", "GoogleFX")

                if not prompt_filled:
                    log(f"❌ 第 {idx+1} 个 prompt 所有输入方法均失败", "Error")
                return prompt_filled

            # 🔄 串行执行: 提交 → 等网络捕获 URL → 下载到本地 → 挂参考/提交下一张
            existing_error_count = _count_error_cards(page)
            log(f"  📋 提交前已有错误卡片: {existing_error_count} 个", "GoogleFX")

            output_dir = _ensure_output_dir(req, "images")

            local_paths = []       # 最终保存的本地路径列表
            all_result_urls = []   # 对应的网络 URL（用于面板 UUID 提取）
            last_failed_detail = [None]  # 记录卡片/页面级真实报错信息（避免被超时通配文本覆盖）

            def _download_image(img_url, save_idx):
                """下载单张图到本地，返回 local_path 或 None"""
                m_uuid = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', img_url)
                if m_uuid:
                    local_path = os.path.join(output_dir, f"fx_batch_{int(time.time())}_{save_idx}_{m_uuid.group(1)}.jpg")
                else:
                    local_path = os.path.join(output_dir, f"fx_batch_{int(time.time())}_{save_idx}.jpg")

                # ── data URL (canvas.toDataURL) ──
                if img_url.startswith("data:image/"):
                    try:
                        b64_part = img_url.split(",", 1)[1] if "," in img_url else ""
                        img_bytes = base64.b64decode(b64_part)
                        with open(local_path, "wb") as f:
                            f.write(img_bytes)
                        log(f"  ✅ Canvas数据保存: {len(img_bytes)} bytes", "GoogleFX")
                        return local_path
                    except Exception as e:
                        log(f"  ⚠️ Canvas解码失败: {e}", "GoogleFX")
                        return None

                # ── labs.google URL (getMediaUrlRedirect) 需要 cookie → 用 browser fetch ──
                # ── 其他非 http URL → 也用 browser fetch ──
                use_browser_fetch = (
                    "labs.google" in img_url
                    or not img_url.startswith("http")
                )

                if not use_browser_fetch:
                    # ── GCS / 其他公开直链 → requests.get ──
                    try:
                        r = requests.get(img_url, stream=True, timeout=15)
                        r.raise_for_status()
                        with open(local_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        log(f"  ✅ 直链下载完成: {local_path}", "GoogleFX")
                        return local_path
                    except Exception as e:
                        log(f"  ⚠️ 直链下载失败，降级 browser fetch: {e}", "GoogleFX")
                        use_browser_fetch = True

                # ── browser fetch (带 cookie，适用于 labs.google 及直链失败的兜底) ──
                if use_browser_fetch:
                    try:
                        b64_data = page.evaluate("""async (url) => {
                            const response = await fetch(url);
                            const blob = await response.blob();
                            return new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                reader.onerror = reject;
                                reader.readAsDataURL(blob);
                            });
                        }""", img_url)
                        img_bytes = base64.b64decode(b64_data)
                        with open(local_path, "wb") as f:
                            f.write(img_bytes)
                        log(f"  ✅ Browser fetch 下载完成: {local_path}", "GoogleFX")
                        return local_path
                    except Exception as e:
                        log(f"  ⚠️ Browser fetch 下载失败: {e}", "GoogleFX")
                        return None

            def _scan_new_result_tiles(known_tile_ids=None):
                """一次 evaluate 同时取回「新 tile 里的媒体」「全页媒体 src」「新 tile 是否已报错」。"""
                try:
                    result = page.evaluate("""(beforeIds) => {
                        const before = new Set(beforeIds || []);
                        const rows = [];
                        const mediaSrcs = [];
                        let failed = null;

                        const isMediaSrc = (lower) => lower.includes('getmediaurlredirect')
                            || lower.includes('flow-content.google')
                            || lower.includes('storage.googleapis.com')
                            || lower.includes('googleusercontent.com')
                            || lower.includes('ggpht.com');

                        for (const img of document.querySelectorAll('img')) {
                            const src = img.currentSrc || img.getAttribute('src') || '';
                            if (src && isMediaSrc(src.toLowerCase())) mediaSrcs.push(src);
                        }

                        const isVisible = (el) => {
                            let cur = el;
                            while (cur) {
                                const style = window.getComputedStyle(cur);
                                if (style.display === 'none' || style.visibility === 'hidden'
                                    || parseFloat(style.opacity) === 0) {
                                    return false;
                                }
                                cur = cur.parentElement;
                            }
                            return true;
                        };

                        for (const tile of document.querySelectorAll('div[data-tile-id]')) {
                            const tileId = tile.getAttribute('data-tile-id') || '';
                            if (!tileId || before.has(tileId)) continue;

                            for (const img of tile.querySelectorAll('img')) {
                                const src = img.currentSrc || img.getAttribute('src') || '';
                                if (!src) continue;
                                const lower = src.toLowerCase();
                                const looksLikeMedia = isMediaSrc(lower)
                                    || lower.startsWith('blob:')
                                    || lower.startsWith('data:image/');
                                const largeEnough = img.naturalWidth >= 128 && img.naturalHeight >= 128;
                                if (looksLikeMedia && largeEnough) rows.push({tileId, src});
                            }

                            if (failed) continue;
                            const t = (tile.innerText || '').toLowerCase();
                            const hasCreditExhaustedText = (
                                /\\b(out of credits?|insufficient credits?|not enough credits?|credits? exhausted|credits? depleted|no credits? left|resource_exhausted|quota_exhausted|quota exceeded|daily limit|reached (?:the )?daily limit|daily generation limit)\\b/i.test(t)
                                || /(?<!\\d)0\\s*(?:(?:google\\s+)?flow\\s+)?credits?\\b/i.test(t)
                                || /(?:credits?|credit\\s+balance|积分|点数|额度|配额|余额)[:：=为是]\\s*0(?!\\d)/i.test(t)
                                || /(积分不足|没有足够的积分|积分已用完|积分已耗尽|积分耗尽|点数不足|点数已用完|点数已耗尽|额度不足|额度已用完|额度耗尽|配额不足|配额已用完|配额耗尽|无可用积分|无可用点数|单日上限|单日配额已用完|今日生成次数已达上限|已达单日上限)/.test(t)
                                || /(?<!\\d)0\\s*(?:积分|点数)(?!\\d)/.test(t)
                            );
                            const hasFailText = t.includes('failed') || t.includes('something went wrong')
                                             || t.includes('unusual activity') || t.includes('help center')
                                             || t.includes('出错了') || t.includes('生成失败')
                                             || t.includes('失败') || t.includes('使用人数过多')
                                             || hasCreditExhaustedText;
                            if (!hasFailText) continue;
                            const hasWarningIcon = Array.from(tile.querySelectorAll('i')).some((i) => {
                                const txt = (i.innerText || i.textContent || '').trim().toLowerCase();
                                const isWarning = txt === 'warning' || txt === 'error' || txt === 'error_outline';
                                return isWarning && isVisible(i);
                            });
                            if (hasWarningIcon || hasCreditExhaustedText) failed = {text: tile.innerText, isCreditExhausted: hasCreditExhaustedText};
                        }
                        return {rows, mediaSrcs, failed};
                    }""", list(known_tile_ids or []))
                except Exception as e:
                    log(f"  ⚠️ 新结果 tile 扫描失败: {type(e).__name__}", "GoogleFX")
                    return {"rows": [], "mediaSrcs": [], "failed": None}
                return result or {"rows": [], "mediaSrcs": [], "failed": None}

            def _wait_for_net_url(submit_ts_this, timeout=None, known_tile_ids_before_submit=None,
                                   known_net_urls_before_submit=None,
                                   known_dom_srcs_before_submit=None,
                                   blocked_media_uuids=None):
                """
                双路并行检测新图片 URL:
                  路径A — 网络拦截 captured_data (被动等待)
                  路径B — DOM 扫描页面上新出现的 img[src*='getMediaUrlRedirect'] (主动探测)
                哪路先出结果就返回，不需要打开面板。
                timeout=None 时现读运行时配置（控制台可热调等待上限）。

                2026-08-01：删掉了 _current_prompt_text / _current_prompt_idx /
                _has_ref_for_this_prompt 三个参数。调用方一直认真传值，函数体一次都没读过
                （归属判定实际靠 known_tile_ids_before_submit 的 tile 基线 +
                blocked_media_uuids 黑名单）。留着会让人以为本函数会按 prompt 内容做校验。
                """
                timeout = timeout or get_runtime_max_wait_seconds()
                deadline = time.time() + timeout
                # These baselines must be frozen before clicking Send.  Capturing them here
                # used to race Flow's lazy-loaded reference tile: chain_ref_NNN could gain a
                # redirect src immediately after submit and be mistaken for the generated image.
                known_net = set(known_net_urls_before_submit or ())
                if known_net_urls_before_submit is None:
                    known_net = set(url for ts, url in captured_data if ts < submit_ts_this)

                # 记录提交前页面上已有的媒体 img srcs（Canvas + 其他区域）
                known_dom_srcs = set(known_dom_srcs_before_submit or ())
                if known_dom_srcs_before_submit is None:
                    try:
                        known_dom_srcs = set(page.evaluate("""() => {
                            const isMediaSrc = (lower) => lower.includes('getmediaurlredirect')
                                || lower.includes('flow-content.google')
                                || lower.includes('storage.googleapis.com')
                                || lower.includes('googleusercontent.com')
                                || lower.includes('ggpht.com');
                            return Array.from(document.querySelectorAll('img'))
                                .map(img => img.currentSrc || img.getAttribute('src') || '')
                                .filter(src => src && isMediaSrc(src.toLowerCase()));
                        }"""))
                    except Exception as e:
                        log(f"  ⚠️ 初始化 known_dom_srcs 失败: {type(e).__name__}", "GoogleFX")
                        known_dom_srcs = set()

                blocked_media_uuids = {
                    str(value).lower() for value in (blocked_media_uuids or ()) if value
                }
                known_tile_ids_before_submit = set(known_tile_ids_before_submit or ())
                ignored_candidates = set()
                pending_unconfirmed_candidates = set()
                early_candidate_logs = set()

                def _acceptable_candidate(url, source, *, confirmed_new_tile=False,
                                          capture_ts=None):
                    if not url or url in ignored_candidates:
                        return False
                    # 提交后头几秒的观测是画布重排把历史 tile 又拉了一遍，不是本次结果。
                    # 网络侧有确切的捕获时刻，可以就地判死并把这个 UUID 拉黑（生成不可能
                    # 在 4 秒内完成，所以这不是"证据不足"而是"证据相反"）；DOM 侧没有捕获
                    # 时刻，只能按"现在还太早"延后一轮，绝不拉黑。
                    if capture_ts is not None:
                        if _is_canvas_hydration_capture(
                                capture_ts, submit_ts_this,
                                candidate_mode=is_candidate_request):
                            ignored_candidates.add(url)
                            media_uuid = _extract_media_uuid(url)
                            if media_uuid:
                                blocked_media_uuids.add(media_uuid)
                            log(
                                f"  🚫 {source}提交后 {max(0.0, capture_ts - submit_ts_this):.1f}s 即被捕获，"
                                f"判为画布重排的历史媒体: {media_uuid[:16] if media_uuid else 'no-uuid'}...",
                                "GoogleFX",
                            )
                            return False
                    elif _is_canvas_hydration_capture(
                            time.time(), submit_ts_this,
                            candidate_mode=is_candidate_request):
                        return False
                    # Hard evidence first.  Only media that already existed before Send
                    # (canvas history, mounted prompt references, caller exclusions) is
                    # provably not this result; that check owns every permanent rejection.
                    if _is_blocked_media_candidate(url, blocked_media_uuids):
                        ignored_candidates.add(url)
                        media_uuid = _extract_media_uuid(url)
                        log(
                            f"  🚫 {source}忽略参考/历史媒体 UUID: {media_uuid[:16]}...",
                            "GoogleFX",
                        )
                        return False
                    if not _is_generated_candidate_stable(
                        submit_ts_this,
                        confirmed_new_tile=confirmed_new_tile,
                    ):
                        # 2026-08-02 事故：这里原本会把「出现过早」的 UUID 永久写进
                        # blocked_media_uuids。Nano Banana 2 Lite 实测 6–10s 就出图，
                        # 正好横跨这条 8s 线，于是三次生成的成品（d5b9144b / 6dd6aa35 /
                        # f07d50ca）刚被网络捕获就被自己拉黑，之后连「新 tile 内确认」
                        # 这种最强证据都救不回来——Flow 画布上明明有图，worker 却一路
                        # 等到 120s 超时，还被上层判成风控去换号。
                        # 早到不是历史媒体的证据，只是证据还不够：这里只延后判定，
                        # 下一轮拿到 tile/DOM 佐证或过了最小年龄后仍可重新参评。
                        if url not in early_candidate_logs:
                            early_candidate_logs.add(url)
                            media_uuid = _extract_media_uuid(url)
                            log(
                                f"  ⏳ {source}候选出现过早，暂缓判定（等待新 tile/DOM 佐证）: "
                                f"{media_uuid[:16] if media_uuid else 'no-uuid'}...",
                                "GoogleFX",
                            )
                        return False
                    return True

                def _scan_new_result_tiles(known_tile_ids=None):
                    """一次 evaluate 同时取回「新 tile 里的媒体」「全页媒体 src」「新 tile 是否已报错」。

                    2026-08-01 合并：路径B 和路径C 原本是两次独立的 page.evaluate，各自
                    完整遍历一遍 div[data-tile-id]，2s 一轮各跑一次。除了白花一次 CDP 往返
                    和一遍全表遍历，两次扫描之间 DOM 还可能变（tile 在两次扫描的间隙拿到
                    媒体），判定依据于是来自两个不同快照。合并后两路共用同一快照。

                    返回 {"rows": [...], "mediaSrcs": [...], "failed": {text} | None}。
                    mediaSrcs 是全页（不限 tile、不限尺寸）的媒体 img src：新结果并不总能
                    落进一个新 data-tile-id 里——画布是虚拟化的，视口外的结果 tile 根本不
                    挂载。它用来给路径A 的候选做「提交后才出现的媒体」佐证。
                    """
                    before_ids = known_tile_ids if known_tile_ids is not None else known_tile_ids_before_submit
                    try:
                        result = page.evaluate("""(beforeIds) => {
                            const before = new Set(beforeIds || []);
                            const rows = [];
                            const mediaSrcs = [];
                            let failed = null;

                            const isMediaSrc = (lower) => lower.includes('getmediaurlredirect')
                                || lower.includes('flow-content.google')
                                || lower.includes('storage.googleapis.com')
                                || lower.includes('googleusercontent.com')
                                || lower.includes('ggpht.com');

                            for (const img of document.querySelectorAll('img')) {
                                const src = img.currentSrc || img.getAttribute('src') || '';
                                if (src && isMediaSrc(src.toLowerCase())) mediaSrcs.push(src);
                            }

                            const isVisible = (el) => {
                                let cur = el;
                                while (cur) {
                                    const style = window.getComputedStyle(cur);
                                    if (style.display === 'none' || style.visibility === 'hidden'
                                        || parseFloat(style.opacity) === 0) {
                                        return false;
                                    }
                                    cur = cur.parentElement;
                                }
                                return true;
                            };

                            for (const tile of document.querySelectorAll('div[data-tile-id]')) {
                                const tileId = tile.getAttribute('data-tile-id') || '';
                                if (!tileId || before.has(tileId)) continue;

                                for (const img of tile.querySelectorAll('img')) {
                                    const src = img.currentSrc || img.getAttribute('src') || '';
                                    if (!src) continue;
                                    const lower = src.toLowerCase();
                                    const looksLikeMedia = isMediaSrc(lower)
                                        || lower.startsWith('blob:')
                                        || lower.startsWith('data:image/');
                                    // Exclude avatars/icons while still accepting current Flow
                                    // variants that render a direct GCS URL instead of the old
                                    // getMediaUrlRedirect URL.
                                    const largeEnough = img.naturalWidth >= 128 && img.naturalHeight >= 128;
                                    if (looksLikeMedia && largeEnough) rows.push({tileId, src});
                                }

                                if (failed) continue;
                                const t = (tile.innerText || '').toLowerCase();
                                const hasCreditExhaustedText = (
                                    /\\b(out of credits?|insufficient credits?|not enough credits?|credits? exhausted|credits? depleted|no credits? left|resource_exhausted|quota_exhausted|quota exceeded|daily limit|reached (?:the )?daily limit|daily generation limit)\\b/i.test(t)
                                    || /(?<!\\d)0\\s*(?:(?:google\\s+)?flow\\s+)?credits?\\b/i.test(t)
                                    || /(?:credits?|credit\\s+balance|积分|点数|额度|配额|余额)[:：=为是]\\s*0(?!\\d)/i.test(t)
                                    || /(积分不足|没有足够的积分|积分已用完|积分已耗尽|积分耗尽|点数不足|点数已用完|点数已耗尽|额度不足|额度已用完|额度耗尽|配额不足|配额已用完|配额耗尽|无可用积分|无可用点数|单日上限|单日配额已用完|今日生成次数已达上限|已达单日上限)/.test(t)
                                    || /(?<!\\d)0\\s*(?:积分|点数)(?!\\d)/.test(t)
                                );
                                const hasFailText = t.includes('failed') || t.includes('something went wrong')
                                                 || t.includes('unusual activity') || t.includes('help center')
                                                 || t.includes('出错了') || t.includes('生成失败')
                                                 || t.includes('失败') || t.includes('使用人数过多')
                                                 || hasCreditExhaustedText;
                                if (!hasFailText) continue;
                                const hasWarningIcon = Array.from(tile.querySelectorAll('i')).some((i) => {
                                    const txt = (i.innerText || i.textContent || '').trim().toLowerCase();
                                    const isWarning = txt === 'warning' || txt === 'error' || txt === 'error_outline';
                                    return isWarning && isVisible(i);
                                });
                                if (hasWarningIcon || hasCreditExhaustedText) failed = {text: tile.innerText, isCreditExhausted: hasCreditExhaustedText};
                            }
                            return {rows, mediaSrcs, failed};
                        }""", list(before_ids or []))
                    except Exception as e:
                        log(f"  ⚠️ 新结果 tile 扫描失败: {type(e).__name__}", "GoogleFX")
                        return {"rows": [], "mediaSrcs": [], "failed": None}
                    return result or {"rows": [], "mediaSrcs": [], "failed": None}

                def _scan_captured(confirmed_new_tile_uuids, post_submit_dom_uuids=()):
                    """路径A：纯内存 deque 扫描，不碰浏览器，可以随便高频调用。

                    佐证分两档：新 tile 内的媒体（最强，可越过最小年龄）与「提交后才在
                    页面上出现的媒体」（次强）。两档都拿不到时，候选不会被丢弃，而是挂起
                    计时——熬过宽限期仍然只有它一个干净候选，就按它落盘。
                    """
                    fallback = None
                    for ts, url in reversed(captured_data):
                        media_uuid = _extract_media_uuid(url)
                        in_new_tile = bool(
                            media_uuid and media_uuid in confirmed_new_tile_uuids
                        )
                        confirmed = in_new_tile or bool(
                            media_uuid and media_uuid in post_submit_dom_uuids
                        )
                        if (ts >= submit_ts_this - 1 and url not in known_net
                                and _acceptable_candidate(
                                    url,
                                    "路径A",
                                    confirmed_new_tile=in_new_tile,
                                    capture_ts=ts,
                                )):
                            if confirmed:
                                return url
                            if fallback is None:
                                fallback = (url, ts, media_uuid)
                            if url not in pending_unconfirmed_candidates:
                                pending_unconfirmed_candidates.add(url)
                                log(
                                    f"  ⏳ 路径A候选尚未关联本次新结果 tile，继续等待: "
                                    f"{media_uuid[:16] if media_uuid else 'no-uuid'}...",
                                    "GoogleFX",
                                )
                    if fallback and _unconfirmed_candidate_is_acceptable(fallback[1]):
                        url, captured_ts, media_uuid = fallback
                        log(
                            f"  ⚠️ 路径A候选已挂起 {int(time.time() - captured_ts)}s 仍无 tile/DOM 佐证"
                            f"（画布虚拟化未挂载该结果），按提交后新增媒体接受: "
                            f"{media_uuid[:16] if media_uuid else 'no-uuid'}...",
                            "GoogleFX",
                        )
                        return url
                    return None

                poll_count = 0
                while time.time() < deadline:
                    _check_cancelled()
                    poll_count += 1

                    # Media inside a tile created after this submit is the strongest
                    # association evidence.  Flow also lazy-loads historical canvas tiles
                    # after Send, so page-wide media is only ever used to corroborate a
                    # network candidate that already survived the pre-submit baselines.
                    tile_scan = _scan_new_result_tiles(known_tile_ids_before_submit)
                    new_tile_rows = tile_scan.get("rows") or []
                    new_failed_tile = tile_scan.get("failed")
                    confirmed_new_tile_uuids = {
                        _extract_media_uuid(row.get("src"))
                        for row in new_tile_rows
                        if _extract_media_uuid(row.get("src"))
                    }
                    post_submit_dom_uuids = {
                        _extract_media_uuid(src)
                        for src in (tile_scan.get("mediaSrcs") or [])
                        if src and src not in known_dom_srcs and _extract_media_uuid(src)
                    }

                    # ── 路径A: 网络拦截 ──
                    hit = _scan_captured(confirmed_new_tile_uuids, post_submit_dom_uuids)
                    if hit:
                        log(f"  📡 路径A 网络捕获: {hit[:80]}...", "GoogleFX")
                        return hit

                    # ── 路径B: 仅扫描本次提交新增 tile 内的图片 ──
                    acceptable_rows = sorted(
                        (
                            row for row in new_tile_rows
                            if row.get("src")
                            and row.get("src") not in known_dom_srcs
                            and _acceptable_candidate(
                                row.get("src"),
                                "路径B",
                                confirmed_new_tile=True,
                            )
                        ),
                        key=lambda row: (row.get("tileId", ""), row.get("src", "")),
                    )
                    # 转为绝对 URL 后才算数。
                    # ⚠️ 只有「/」开头的站内相对路径才能拼前缀。tile 扫描的 looksLikeMedia
                    # 现在也认 blob: / data:image/，这两种既非 http 开头、也不是相对路径，
                    # 原来那句 `else f"https://labs.google{new_src}"` 会拼出
                    # https://labs.googleblob:https://... 这种垃圾地址，_download_image 必然
                    # 失败，进而把整批按「已生成但下载落盘失败」截断。blob:/data: 是页面内存
                    # 对象、服务端取不到，只能跳过；同一张图稍后通常会以真正的 http(s) src
                    # 再出现一次，所以这里不 return、继续往下走本轮的路径C 和等待。
                    for row in acceptable_rows:
                        new_src = row["src"]
                        full_url = _absolute_media_url(new_src)
                        if not full_url:
                            if new_src not in ignored_candidates:
                                ignored_candidates.add(new_src)
                                log(
                                    f"  ⏭️ 路径B 跳过不可下载的 src ({new_src[:32]}...)，"
                                    f"等待同一结果的 http(s) 地址",
                                    "GoogleFX",
                                )
                            continue
                        log(
                            f"  🖼️  路径B新结果tile捕获: "
                            f"tile={row.get('tileId', '')[:16]}... {new_src[:80]}...",
                            "GoogleFX",
                        )
                        return full_url

                    # ── 路径C: 新提交卡片已报错失败（与路径B 同一次扫描的结果）──
                    # 顺序仍是先 B 后 C：同一轮里既拿到媒体又检测到报错时，媒体优先，
                    # 与合并前的行为一致。
                    if new_failed_tile:
                        failed_text = str(new_failed_tile.get('text') or '').strip()
                        log(f"  ❌ 检测到新提交卡片已报错失败: "
                            f"{failed_text[:100]}", "GoogleFX")
                        last_failed_detail[0] = failed_text or "卡片报错生成失败"
                        return None

                    elapsed = int(deadline - time.time())
                    if poll_count % 5 == 0 and elapsed > 0:
                        log(f"  ⏳ 等待图片 URL... 剩余 {elapsed}s (网络={len(captured_data)}, DOM扫描#{poll_count})", "GoogleFX")

                    # 昂贵的 DOM 扫描仍保持 2s 一轮，但这 2s 拆成 0.25s 的小步，
                    # 每步后摸一次内存里的网络捕获——图实际就绪时几乎总是路径A 先
                    # 出结果，原来固定睡满 2s 等于白白多压最多 2s/张的尾延迟。
                    # ⚠️ 必须用 page.wait_for_timeout 而不是 time.sleep：sync_playwright
                    # 只在调用 Playwright API 时才把 response 事件派发给 handler，
                    # 纯 time.sleep 期间 captured_data 根本不会有新内容，
                    # 高频扫描就成了空转。
                    slice_deadline = min(time.time() + 2, deadline)
                    while time.time() < slice_deadline:
                        try:
                            page.wait_for_timeout(250)
                        except Exception:
                            time.sleep(0.25)   # 页面已关等：交给下一轮的路径B/C 报错
                            break
                        hit = _scan_captured(confirmed_new_tile_uuids, post_submit_dom_uuids)
                        if hit:
                            log(f"  📡 路径A 网络捕获: {hit[:80]}...", "GoogleFX")
                            return hit
                        _check_cancelled()

                # 超时前主动扫描一次页面是否存在积分/配额耗尽提示
                # （deep：这是"等不到结果"的收尾诊断，值得花几秒读真实余额）
                page_credit_err = detect_page_credit_exhaustion(page, deep=True)
                if page_credit_err:
                    last_failed_detail[0] = page_credit_err

                return None

            def _halt_batch_keeping_prefix(exc, idx):
                """本批到此为止：已成功的前缀能留就留，留不下就把原异常抛出去。

                返回 True 表示调用方应当 break（前缀已记进 result）。

                批内每张图都是前一张的 i2i 续链，所以任何一张失败都必须**停在这一张**，
                不能跳过它继续跑——那会把 prompt[idx+1] 接到 prompt[idx-1] 的图上，
                整批 prompt↔帧 的对应关系全体错位。
                但"停下"不等于"作废"：前 idx 张已经真实生成、下载、扣过积分，且它们的
                血统仍然精确。超时（_wait_for_net_url 返回空）和下载落盘失败两条路早就
                是 break 保前缀，而参考图挂载失败 / 近重复判定这两条却是 raise ——
                异常会跳过下面那句 result["image_urls"] = local_paths，让 except 返回
                image_urls=[]，把这几张图连同积分一起丢掉，上层重试再从头生成一遍。
                统一成同一套语义（2026-08-02）。
                """
                if not local_paths:
                    # 一张都没成，没有前缀可保；交给原异常，保留它的分类前缀
                    # （REFERENCE_MOUNT_FAILED / 近重复文案）供上层判定。
                    raise exc
                result["failed_index"] = idx
                result["message"] = str(exc)
                log(f"⚠️ {result['message']}，停止本批并保留前 {len(local_paths)} 张成功结果",
                    "GoogleFX")
                return True

            # 跨任务台账：本账号历史上已被下载消费掉的 media UUID 一律不再是"新结果"。
            # 提交前那几条基线（panel/DOM/tile）全都是实时 DOM 采样，Flow 画布虚拟化
            # 之后看不见视口外的历史 tile；本集合不碰 DOM，是唯一不受虚拟化影响的证据。
            ledger_account_id = account_binding.resolve_account(
                fallback=get_runtime_default_user_id()
            )
            consumed_media_uuids = media_ledger.consumed_uuids(ledger_account_id)
            if consumed_media_uuids:
                log(f"  📒 已加载账号 {ledger_account_id} 的历史媒体台账: "
                    f"{len(consumed_media_uuids)} 条", "GoogleFX")

            def _wait_for_candidate_urls(submit_ts_this, target_count=4, timeout=None,
                                         known_tile_ids_before_submit=None,
                                         known_net_urls_before_submit=None,
                                         known_dom_srcs_before_submit=None,
                                         blocked_media_uuids=None):
                """4选1候选图模式：单次提交 x4 后统一等待并捕获所有候选图 URL。"""
                timeout = timeout or get_runtime_max_wait_seconds()
                deadline = time.time() + timeout
                known_net = set(known_net_urls_before_submit or ())
                if known_net_urls_before_submit is None:
                    known_net = set(url for ts, url in captured_data if ts < submit_ts_this)

                known_dom_srcs = set(known_dom_srcs_before_submit or ())
                blocked_media_uuids = {
                    str(value).lower() for value in (blocked_media_uuids or ()) if value
                }
                collected = []
                seen_uuids = set()
                hydration_logged = set()

                poll_count = 0
                while time.time() < deadline:
                    _check_cancelled()
                    poll_count += 1
                    # 提交刚落下的这几秒里，画布重排会把历史 tile 连同它们的媒体一起
                    # 重新挂上来。这段时间内 DOM 侧的一切"新增"都不可信，直接不采。
                    in_hydration_window = _is_canvas_hydration_capture(
                        time.time(), submit_ts_this, candidate_mode=True)

                    tile_scan = _scan_new_result_tiles(known_tile_ids_before_submit)
                    new_tile_rows = tile_scan.get("rows") or []
                    confirmed_new_tile_uuids = {
                        _extract_media_uuid(row.get("src"))
                        for row in new_tile_rows
                        if _extract_media_uuid(row.get("src"))
                    }
                    post_submit_dom_uuids = {
                        _extract_media_uuid(src)
                        for src in (tile_scan.get("mediaSrcs") or [])
                        if src and src not in known_dom_srcs and _extract_media_uuid(src)
                    }

                    # 1. 扫描网络捕获 captured_data
                    for ts, url in list(captured_data):
                        if ts >= submit_ts_this - 1 and url not in known_net:
                            m_uuid = _extract_media_uuid(url)
                            if not m_uuid or m_uuid in seen_uuids:
                                continue
                            if _is_blocked_media_candidate(url, blocked_media_uuids):
                                continue
                            if _is_canvas_hydration_capture(
                                    ts, submit_ts_this, candidate_mode=True):
                                # 提交后十来秒内就出现 = 画布重排捞回来的历史媒体
                                # （x4 真结果 15s 起，见窗口常量处的实测分布）。
                                # 这里必须就地拉黑 UUID：只"跳过本轮"的话，它熬过
                                # _MIN_GENERATED_RESULT_AGE_SECONDS 之后照样会被
                                # 下一轮收下——2026-08-22 的 IMG 012 就是这么被
                                # IMG 013 的候选图顶掉的。
                                blocked_media_uuids.add(m_uuid)
                                if m_uuid not in hydration_logged:
                                    hydration_logged.add(m_uuid)
                                    log(
                                        f"  🚫 提交后 {max(0.0, ts - submit_ts_this):.1f}s 即被捕获，"
                                        f"判为画布重排的历史媒体，不作候选: {m_uuid[:16]}...",
                                        "GoogleFX",
                                    )
                                continue
                            in_new_tile = m_uuid in confirmed_new_tile_uuids
                            confirmed = in_new_tile or (m_uuid in post_submit_dom_uuids)
                            if confirmed or _is_generated_candidate_stable(submit_ts_this, confirmed_new_tile=in_new_tile):
                                seen_uuids.add(m_uuid)
                                collected.append(url)
                                log(f"  🎯 捕获候选图 ({len(collected)}/{target_count}): {url[:70]}...", "GoogleFX")
                                if len(collected) >= target_count:
                                    return collected

                    # 2. 扫描 DOM 新 tile（水化窗口内一律不采：此刻的"新 tile"多半是旧图重挂）
                    for row in (() if in_hydration_window else new_tile_rows):
                        src = row.get("src")
                        if src and src not in known_dom_srcs:
                            m_uuid = _extract_media_uuid(src)
                            if m_uuid and m_uuid not in seen_uuids and not _is_blocked_media_candidate(src, blocked_media_uuids):
                                full_url = _absolute_media_url(src)
                                if full_url:
                                    seen_uuids.add(m_uuid)
                                    collected.append(full_url)
                                    log(f"  🎯 从新 tile 捕获候选图 ({len(collected)}/{target_count}): {full_url[:70]}...", "GoogleFX")
                                    if len(collected) >= target_count:
                                        return collected

                    if len(collected) >= target_count:
                        return collected

                    if len(collected) >= 1 and (time.time() - submit_ts_this) > 45:
                        return collected

                    page.wait_for_timeout(400)

                return collected

            if getattr(req, "is_candidate_mode", False):
                prompt_text = prompts[0]
                has_ref_for_this_prompt = bool(valid_refs)
                if has_ref_for_this_prompt:
                    refs_before_prompt = _get_prompt_reference_uuids(page, limit=8)
                    if refs_before_prompt:
                        filled = _fill_text_only(prompt_text, 0, has_ref=True)
                    else:
                        _ref_ready, _ = _wait_for_flow_reference_ready(page, timeout_seconds=45)
                        filled = _fill_text_only(prompt_text, 0, has_ref=_ref_ready)
                else:
                    filled = _fill_text_only(prompt_text, 0, has_ref=False)

                if not filled:
                    raise RuntimeError("候选图提示词输入失败")

                try:
                    input_el.click(); random_sleep(0.1, 0.2)
                    page.keyboard.press("End")
                    page.keyboard.type(" "); random_sleep(0.15, 0.25)
                    if not has_ref_for_this_prompt:
                        page.keyboard.press("Backspace"); random_sleep(0.1, 0.2)
                except Exception as e:
                    log(f"  ⚠️ React state 触发: {e}", "GoogleFX")

                random_sleep(1.0, 2.0)
                try:
                    pre_submit_tile_ids = set(page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('div[data-tile-id]'))
                            .map(card => card.getAttribute('data-tile-id') || '')
                            .filter(Boolean);
                    }"""))
                except Exception:
                    pre_submit_tile_ids = set()

                pre_submit_known_net_urls = {url for _ts, url in captured_data}
                try:
                    pre_submit_dom_srcs = set(page.evaluate("""() => {
                        const isMediaSrc = (lower) => lower.includes('getmediaurlredirect')
                            || lower.includes('flow-content.google')
                            || lower.includes('storage.googleapis.com')
                            || lower.includes('googleusercontent.com')
                            || lower.includes('ggpht.com');
                        return Array.from(document.querySelectorAll('img'))
                            .map(img => img.currentSrc || img.getAttribute('src') || '')
                            .filter(src => src && isMediaSrc(src.toLowerCase()));
                    }"""))
                except Exception:
                    pre_submit_dom_srcs = set()

                prompt_reference_uuids = set(_get_prompt_reference_uuids(page, limit=8))
                pre_submit_media_uuids = (
                    set(_get_panel_uuids(page))
                    | prompt_reference_uuids
                    | consumed_media_uuids
                    | {
                        str(value).lower()
                        for value in (getattr(req, "excluded_media_uuids", None) or [])
                        if value
                    }
                )

                click_fx_send_button(page, input_el)
                note_fx_submit()
                submit_ts_this = time.time()
                log(f"🚀 [4选1 智能候选] 已单次提交 prompt (x4 模式)，等待 4 张候选图同步生成...", "GoogleFX")

                candidate_urls = _wait_for_candidate_urls(
                    submit_ts_this,
                    target_count=4,
                    timeout=get_runtime_max_wait_seconds(),
                    known_tile_ids_before_submit=pre_submit_tile_ids,
                    known_net_urls_before_submit=pre_submit_known_net_urls,
                    known_dom_srcs_before_submit=pre_submit_dom_srcs,
                    blocked_media_uuids=pre_submit_media_uuids,
                )

                # 候选线此前既不做近重复校验、也不记台账：整条 4选1 通路上，
                # 「这张图是不是本次生成的」全靠 tile/DOM 佐证，而那两条恰恰会被
                # 画布重排骗过（2026-08-22）。这里补上单图线早就有的两道：
                #   · 像素级近重复 —— 抓回来的旧图往往就是参考图或某个已有槽位；
                #   · 媒体台账 —— 落盘即记账，下一次提交起这个 UUID 永远不再是"新结果"。
                candidate_reference_paths = list(valid_refs)
                candidate_reference_paths.extend(
                    path
                    for path in (getattr(req, "excluded_image_paths", None) or [])
                    if path not in candidate_reference_paths
                )
                for c_idx, c_url in enumerate(candidate_urls):
                    log(f"⬇️  下载候选图 #{c_idx+1}/{len(candidate_urls)}...", "GoogleFX")
                    lp = _download_image(c_url, c_idx)
                    if not (lp and os.path.exists(lp) and os.path.getsize(lp) > 0):
                        continue
                    duplicate, duplicate_mad, duplicate_ref = _images_are_near_duplicates(
                        lp, candidate_reference_paths,
                    )
                    if duplicate:
                        # 丢这一张即可，别的候选照常参评：候选之间本就互相独立，
                        # 与单图线"整批停在这一张"的语义不同。
                        try:
                            os.remove(lp)
                        except OSError:
                            pass
                        log(
                            f"  🚫 候选 #{c_idx+1} 与已有画面近乎相同 "
                            f"(MAD={duplicate_mad:.2f}, ref={os.path.basename(duplicate_ref or '')})，"
                            f"判为画布上的旧图，丢弃",
                            "GoogleFX",
                        )
                        continue
                    consumed_uuid = _extract_media_uuid(c_url) or _extract_media_uuid(lp)
                    if consumed_uuid:
                        consumed_media_uuids.add(consumed_uuid)
                        media_ledger.record_consumed(
                            ledger_account_id, consumed_uuid,
                            context=os.path.basename(lp),
                        )
                    local_paths.append(lp)

                if local_paths:
                    result["status"] = "success"
                    result["image_urls"] = local_paths
                    result["message"] = f"成功生成 {len(local_paths)} 张候选图"
                    try:
                        final_url = str(getattr(page, "url", "") or "")
                        if "/project/" in final_url:
                            result["project_url"] = final_url
                            req.project_url = final_url
                    except Exception:
                        pass
                    log(f"🎉 [4选1 智能候选] 4x 批量生成完成，共获得 {len(local_paths)} 张候选图 (project_url: {result.get('project_url')})", "GoogleFX")
                    return result
                else:
                    raise RuntimeError("4选1 候选图生成未捕获到有效图片")

            for idx, prompt_text in enumerate(prompts):
                _check_cancelled()
                log(f"\n{'─'*40}", "GoogleFX")
                log(f"🖼️  Step {idx+1}/{len(prompts)}: '{prompt_text[:50]}...'", "GoogleFX")
                if idx > 0:
                    # ── 链式续图必须先摘掉上一拍的参考图（2026-07-26）──
                    # 素材槽里的 chip 活在编辑器之外，下面 _fill_text_only(has_ref=False)
                    # 的 fill() 只清得掉编辑器里的文字，碰不到它们；而 Step B 的
                    # _mount_uuid_as_ref 只管挂新的、从不摘旧的。结果整批下来 chip 只增不减：
                    # 第 N 张带着第 1..N-1 张 + 循环外挂的封面/上一批留档，一路累积
                    # （日志里每批开头稳定「清除 6 个」= 1 次循环外挂载 + 5 次循环内挂载）。
                    # 多参考图下模型锚定的是最早那张，于是每一帧都被拽回第一帧的构图，
                    # 表现成「帧序列一直在做第一帧」。这里先清空，让每次提交严格只带 1 张参考图。
                    _clear_prompt_reference_chips_image(page)

                # ── Step A: 图生图模式（第一张）→ 参考图已在框里，用追加模式写入文字 ──
                # 注意：valid_refs 挂载是在进入循环前的 2.5 节完成的，
                #       所以第一张图时参考图 chip 已在编辑器中，必须用 has_ref=True 追加，
                #       不能用 fill() 否则 chip 会被清掉。
                has_ref_for_this_prompt = False
                if valid_refs and idx == 0:
                    refs_before_prompt = _get_prompt_reference_uuids(page, limit=8)
                    if refs_before_prompt:
                        has_ref_for_this_prompt = True
                        log(f"  ✅ 检测到参考图已就绪（预检查）", "GoogleFX")
                        filled = _fill_text_only(prompt_text, idx, has_ref=True)
                    else:
                        log(f"  ⏳ 等待参考图就绪信号...", "GoogleFX")
                        _ref_ready, ready_sel = _wait_for_flow_reference_ready(
                            page,
                            timeout_seconds=45,
                            settle_range=(1.0, 2.0),
                        )
                        if _ref_ready:
                            log(f"  ✅ 参考图已就绪 (sel={ready_sel!r})，开始追加输入提示词", "GoogleFX")
                            has_ref_for_this_prompt = True
                            # 有参考图 chip 在框里 → 追加模式，不清空编辑器
                            filled = _fill_text_only(prompt_text, idx, has_ref=True)
                        else:
                            log(f"  ⚠️ 等待45s 参考图仍未就绪，改用无参考模式输入该 prompt", "Error")
                            filled = _fill_text_only(prompt_text, idx, has_ref=False)
                else:
                    # ── 无参考图 / 后续图：正常 fill 写入 ──
                    filled = _fill_text_only(prompt_text, idx, has_ref=False)

                if not filled:
                    log(f"❌ 第 {idx+1} 个 prompt 输入失败，跳过", "Error")
                    continue

                # ── Step B: 非第一张时，在文字填好之后再挂参考图 chip ──
                # 顺序关键：fill 之后再 mount，chip 不会被 fill 清掉
                if idx > 0 and all_result_urls:
                    prev_url = all_result_urls[-1]
                    # UUID 有两处可取：网络 URL 的 name= 参数，以及上一张已经落盘的
                    # 本地文件名（_download_image 按 ..._<uuid>.jpg 命名）。只认前者的话，
                    # 路径B/DOM 扫描回来的那种不带 name= 的 URL 就会直接断链——日志里
                    # 「⚠️ 无法从 URL 提取 UUID，跳过参考图挂载」说的就是这一下。
                    m_uuid = (re.search(r'name=([0-9a-f\-]{30,})', prev_url)
                              or re.search(
                                  r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                                  prev_url))
                    prev_local = local_paths[-1] if local_paths else None
                    if not m_uuid and prev_local:
                        m_uuid = re.search(
                            r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                            os.path.basename(prev_local))

                    mounted = False
                    if m_uuid:
                        ref_uuid = m_uuid.group(1)
                        log(f"🔗 挂载参考图 chip (UUID: {ref_uuid[:16]}...)", "GoogleFX")
                        mounted = _mount_uuid_as_ref(page, ref_uuid)  # 模块级函数
                    else:
                        log(f"  ⚠️ URL 与本地文件名都取不到 UUID，直接走上传挂载", "GoogleFX")

                    if not mounted and prev_local and os.path.exists(prev_local):
                        # 与循环外 2.5 节同一套上传回退：画布上找不到那张 tile（换号/
                        # 刷新/画布被清）时，把上一帧已经下载到本地的原图传回画布再挂。
                        # 这里断链的代价比首帧更大——本帧会退化成纯 t2i，与整条序列脱节。
                        log(f"  ↻ UUID 挂载失败，改用上传方式续链: {os.path.basename(prev_local)}", "GoogleFX")
                        mounted = _upload_image_to_canvas_and_mount(page, prev_local)

                    if mounted:
                        has_ref_for_this_prompt = True
                    elif _halt_batch_keeping_prefix(
                        _reference_mount_error(
                            f"第 {idx+1} 张未能挂载上一张生成结果，已在提交前终止以保护 i2i 血统"
                        ),
                        idx,
                    ):
                        break

                # ── Step C: 触发 React state 同步，然后发送 ──
                # ⚠️ 有参考图时禁止 Backspace（光标可能停在 chip 上，会把 chip 删掉）
                try:
                    input_el.click(); random_sleep(0.1, 0.2)
                    page.keyboard.press("End")
                    if has_ref_for_this_prompt:
                        # 有参考图首张：只追加空格触发 state 同步，不删除
                        # （Backspace 有概率删掉 chip，不安全）
                        page.keyboard.type(" "); random_sleep(0.15, 0.25)
                    else:
                        # 无参考图 / 后续图：Space+Backspace 触发 state 同步
                        page.keyboard.type(" "); random_sleep(0.1, 0.15)
                        page.keyboard.press("Backspace"); random_sleep(0.2, 0.3)
                except Exception as e:
                    log(f"  ⚠️ React state 触发: {type(e).__name__}", "GoogleFX")
                    if cancel_flag.is_cancelled:
                        raise
                # ✅ Patch: 提交前模拟人工确认停顿（1.2–2.8s），降低提交节奏被识别风险
                random_sleep(1.2, 2.8)

                try:
                    pre_submit_tile_ids = set(page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('div[data-tile-id]'))
                            .map(card => card.getAttribute('data-tile-id') || '')
                            .filter(Boolean);
                    }"""))
                except Exception as e:
                    log(f"  ⚠️ 记录提交前 tile 基线失败: {type(e).__name__}", "GoogleFX")
                    pre_submit_tile_ids = set()
                    if cancel_flag.is_cancelled:
                        raise

                # Freeze every relevant baseline before Send.  Prompt references are blocked
                # explicitly because their redirect src may be assigned lazily after this point.
                pre_submit_known_net_urls = {url for _ts, url in captured_data}
                try:
                    # Must use the same predicate/accessor as the in-wait scan, otherwise
                    # pre-existing media reads as "appeared after Send" and corroborates
                    # the wrong candidate.
                    pre_submit_dom_srcs = set(page.evaluate("""() => {
                        const isMediaSrc = (lower) => lower.includes('getmediaurlredirect')
                            || lower.includes('flow-content.google')
                            || lower.includes('storage.googleapis.com')
                            || lower.includes('googleusercontent.com')
                            || lower.includes('ggpht.com');
                        return Array.from(document.querySelectorAll('img'))
                            .map(img => img.currentSrc || img.getAttribute('src') || '')
                            .filter(src => src && isMediaSrc(src.toLowerCase()));
                    }"""))
                except Exception as e:
                    log(f"  ⚠️ 记录提交前 DOM 媒体基线失败: {type(e).__name__}", "GoogleFX")
                    pre_submit_dom_srcs = set()
                prompt_reference_uuids = set(_get_prompt_reference_uuids(page, limit=8))
                pre_submit_media_uuids = (
                    set(_get_panel_uuids(page))
                    | prompt_reference_uuids
                    | consumed_media_uuids
                    | {
                        str(value).lower()
                        for value in (getattr(req, "excluded_media_uuids", None) or [])
                        if value
                    }
                )

                click_fx_send_button(page, input_el)
                note_fx_submit()   # 提交节奏闸门的参照点
                log(f"✅ 第 {idx+1}/{len(prompts)} 个 prompt 已提交", "GoogleFX")

                submit_ts_this = time.time()

                # ── 等网络捕获到 URL ──
                log(f"⏳ 等待第 {idx+1} 张图片网络 URL...", "GoogleFX")
                img_url = _wait_for_net_url(
                    submit_ts_this,
                    timeout=get_runtime_max_wait_seconds(),
                    known_tile_ids_before_submit=pre_submit_tile_ids,
                    known_net_urls_before_submit=pre_submit_known_net_urls,
                    known_dom_srcs_before_submit=pre_submit_dom_srcs,
                    blocked_media_uuids=pre_submit_media_uuids,
                )

                if not img_url:
                    # Batch prompts form one strict i2i lineage.  Continuing with
                    # prompt idx+1 after idx timed out would bind it to the last
                    # successful image and shift every remaining prompt↔frame
                    # mapping.  Stop here and return the durable successful prefix;
                    # the SPARK layer resumes from this exact index.
                    result["failed_index"] = idx
                    fail_detail = last_failed_detail[0]
                    if not fail_detail:
                        fail_detail = detect_page_credit_exhaustion(page, deep=True)
                    if fail_detail:
                        result["message"] = f"第 {idx+1} 张图生成失败: {fail_detail}"
                    else:
                        result["message"] = f"第 {idx+1} 张图超时未捕获到 URL"
                    log(f"⚠️ {result['message']}，停止本批并保留前 {len(local_paths)} 张成功结果", "GoogleFX")
                    break

                # ── 立即下载到本地 (下载完成 = 图已就绪) ──
                log(f"⬇️  开始下载第 {idx+1} 张图...", "GoogleFX")
                local_path = _download_image(img_url, idx)

                if local_path:
                    active_reference_paths = []
                    if has_ref_for_this_prompt:
                        if idx == 0:
                            active_reference_paths = list(valid_refs)
                        elif local_paths:
                            active_reference_paths = [local_paths[-1]]
                    active_reference_paths.extend(
                        path
                        for path in (getattr(req, "excluded_image_paths", None) or [])
                        if path not in active_reference_paths
                    )
                    duplicate, duplicate_mad, duplicate_ref = _images_are_near_duplicates(
                        local_path,
                        active_reference_paths,
                    )
                    if duplicate:
                        try:
                            os.remove(local_path)
                        except OSError:
                            pass
                        if _halt_batch_keeping_prefix(
                            RuntimeError(
                                "所有 prompt 均未捕获到图片："
                                f"候选结果与参考图近乎相同 (MAD={duplicate_mad:.2f}, "
                                f"ref={os.path.basename(duplicate_ref or '')})"
                            ),
                            idx,
                        ):
                            break
                    # 落盘且通过近重复校验之后才记账：只被捕获、没能落盘的 UUID 不算
                    # 被消费，提前记进去会把同一次生成的重试结果误伤成"历史媒体"。
                    consumed_uuid = _extract_media_uuid(img_url)
                    if consumed_uuid:
                        consumed_media_uuids.add(consumed_uuid)
                        media_ledger.record_consumed(
                            ledger_account_id, consumed_uuid,
                            context=os.path.basename(local_path),
                        )
                    local_paths.append(local_path)
                    all_result_urls.append(img_url)
                    log(f"✅ 第 {idx+1} 张图下载完成，准备处理下一张", "GoogleFX")
                else:
                    result["failed_index"] = idx
                    result["message"] = f"第 {idx+1} 张图已生成但下载落盘失败"
                    log(f"⚠️ {result['message']}，停止本批并保留成功前缀", "GoogleFX")
                    break

            if not local_paths:
                raise Exception("所有 prompt 均未捕获到图片")

            if len(local_paths) < len(prompts):
                # Keep the account binding stable while returning the successful
                # prefix: its project URL/UUID ownership belongs to this account.
                # The remainder consumes the shared retry budget and switches only
                # if a later full failed attempt is classified as account-side.
                log(f"⚠️ 批量生图未完全成功 ({len(local_paths)}/{len(prompts)} 成功)，返回成功前缀", "GoogleFX")
                result["status"] = "partial"
                result.setdefault("failed_index", len(local_paths))
                result.setdefault(
                    "message",
                    f"批量生图部分成功 ({len(local_paths)}/{len(prompts)})",
                )
            else:
                result["status"] = "success"
            result["image_urls"] = local_paths

    except Exception as e:
        if cancel_flag.is_cancelled or "任务已取消" in str(e):
            log("🛑 任务已取消，跳过换号", "GoogleFX")
            result["message"] = "任务已取消"
        else:
            log(f"❌ generate_images_batch 内部错误: {e}", "Error")
            result["message"] = str(e)
            # 换号与重试只由外层统一处理。这里过去也判一次，导致每次失败打印两份
            # 判定日志；最后一次则只留下内层的“未知错误”，真正原因很容易被埋掉。
    finally:
        # 清理 response 事件监听器，防止复用 page 时 handler 堆积泄漏
        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass
        # 保持浏览器开启，避免画布被清空。后续由 n8n 在工作流末尾统一关闭。
        # if browser:
        #     try:
        #         browser.close()
        #     except Exception:
        #         pass  # CDP 连接模式下 close() 会抛出异常，这是预期行为

    try:
        final_url = str(getattr(page, "url", "") or "")
        if "/project/" in final_url:
            result["project_url"] = final_url
            req.project_url = final_url
    except Exception:
        pass

    return result


def _generate_images_batch_google_fx_unlocked(req: ImageBatchRequest):
    # 只补齐距上次真实提交还差的那段间隔，而不是无条件干睡 15~25s
    # （理由与实测数据见 helpers.fx_pacing_wait）。
    fx_pacing_wait(*fx_pacing_bounds())
    try:
        max_retries = max(1, min(6, int(getattr(req, "max_attempts", 3) or 3)))
    except (TypeError, ValueError):
        max_retries = 3
    last_err = None
    tried_accounts = set()   # 本次已经试过的号池账号，换号时排除，免得换回刚失败的号
    for attempt in range(1, max_retries + 1):
        _check_cancelled()
        log(f"🔄 开始第 {attempt}/{max_retries} 次图片生成尝试...", "GoogleFX")
        try:
            result = _generate_images_batch_google_fx_single_attempt(req)
            if result.get("status") in ("success", "partial", "ok"):
                result["attempts_used"] = attempt
                images = result.get("image_urls") or []
                if images:
                    try:
                        current_uid = account_binding.resolve_account(
                            fallback=get_runtime_default_user_id()
                        )
                        if not current_uid:
                            log("⚠️ 图片已生成，但无法解析实际 AdsPower 账号，任务数未记录", "GoogleFX")
                        else:
                            from ..utils.account_pool import AccountPool
                            pool_inst = AccountPool()
                            entry = pool_inst.record_task_count(
                                current_uid, image_count=len(images)
                            )
                            pool_inst.optimistic_deduct_credit(current_uid, amount=len(images))
                            if entry is None:
                                log(
                                    f"⚠️ 图片已生成，但账号 {current_uid} 不在账号池中，任务数未记录",
                                    "GoogleFX",
                                )
                    except Exception as _e:
                        log(f"⚠️ 记录账号图片任务数失败: {_e}", "GoogleFX")
                return result
            err_msg = result.get("message", "")
            if _is_login_required_failure(err_msg):
                _cooldown_current_login_account(err_msg)
                raise RuntimeError(f"ACCOUNT_LOGIN_REQUIRED: {err_msg}")
            if not err_msg:
                err_msg = "Google FX 图片生成失败（未返回原因）"
            # Failed attempts are retried only here.  The frame orchestration
            # layer no longer adds another whole-batch retry loop.
            raise RuntimeError(err_msg)
        except Exception as e:
            last_err = e
            if cancel_flag.is_cancelled or "任务已取消" in str(e):
                # 取消不是"这次尝试失败了"：接着冷却重试等于用户点完取消之后又开
                # 三轮浏览器。直接按取消收摊，与 single_attempt 内部同形。
                log("🛑 任务已取消，停止重试", "GoogleFX")
                return {"status": "failed", "image_urls": [], "message": "任务已取消"}
            should_switch, verdict = _classify_failure_for_switch(e)
            _record_current_generation_failure(e)
            if attempt == max_retries:
                log(
                    f"⚠️ 第 {attempt} 次图片生成失败（重试已用尽）: {e}；分类：{verdict}",
                    "GoogleFX",
                )
                break
            # 换不换号由错误类型决定（2026-07-25）：此前每次尝试失败都无条件强制
            # 干预一次，一个"未找到底部配置按钮"就能连打 4 次，而整个过程一张图都
            # 没提交过。UI 自动化类失败只冷却后原地重试即可——换号救不了，反而
            # 每次都要关浏览器重连、把 Flow 登录 token 越打越松。
            log(f"⚠️ 第 {attempt} 次图片生成失败: {e}，"
                f"{'准备换号重试' if should_switch else '原地冷却重试（不换号）'}：{verdict}", "GoogleFX")
            cooldown_time = 0 if (_is_login_required_failure(e) or _is_quota_failure(e)) else 15 * attempt
            if cooldown_time:
                log(f"🕐 冷却等待 {cooldown_time} 秒...", "GoogleFX")
                _cancellable_sleep(cooldown_time)
            if should_switch and not bool(getattr(req, "allow_account_switch", True)):
                log(
                    "📌 本次为原画布迭代，账号已锁定；不换号，避免脱离原 Flow 项目",
                    "GoogleFX",
                )
            elif should_switch:
                switched = _switch_account_on_failure(force_switch=True, exclude=tried_accounts)
                if switched:
                    tried_accounts.add(switched)
                    # 项目 URL 是账号私有的：换号之后那条绑定必然打不开，留着只会让
                    # 下一次尝试走"绑定项目失效"的兜底，而兜底若沿用页面上现成的画布
                    # 就又回到了跨任务串图的老路。直接要求新账号开一块干净画布。
                    req.project_url = None
                    req.require_fresh_canvas = True

    log(f"❌ 图片生成在 {max_retries} 次尝试后全部失败。", "GoogleFX")
    return {
        "status": "failed",
        "image_urls": [],
        "message": f"All attempts failed. Last error: {last_err}",
        "attempts_used": max_retries,
    }
