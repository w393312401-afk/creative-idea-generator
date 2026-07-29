# -*- coding: utf-8 -*-
"""
🎬 Google FX - Veo 视频生成服务 (首尾帧画布上传增强版)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
使用 Google Labs FX / Veo 3.1 生成视频（支持首尾帧图生视频）。
"""

import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

from ..config import (
    MAX_WAIT_SECONDS,
    OUTPUT_DIR,
    get_runtime_default_user_id,
    get_runtime_max_wait_seconds,
)
from ..models import VideoRequest
from ..utils.logger import log
from ..utils import account_binding
from ..utils.browser import (
    random_sleep,
    clean_path,
    get_ads_ws_url,
    find_or_create_page,
    ensure_flow_workspace,
    download_video_via_browser,
)
from ..ui_selectors import UI_SELECTORS, RATIO_MAP

# 导入共享的核心配置验证、连接与交互方法
# ⚠️ 这些名字曾住在 services/google_fx.py，L1/L2/L3 分层重构后已整体搬到
# google_fx_helpers (L2) / google_fx_dom (L1)。本文件是从旧分支合过来的版本，
# 长期还写着 `from .google_fx import ...`——而 google_fx.py 现在只剩
# 227 行的 L3 编排层且在文件末尾反向 import 本模块，于是 `import
# services.google_fx` 必然 ImportError（部分初始化 + 名字根本不存在），
# 整条 FX 链路（帧序列 / 视频）全都起不来。改为直接指向 L1/L2，
# 与 google_fx_image.py 的写法一致，循环 import 也随之消失。
# L1 DOM 原语
from .google_fx_dom import _safe_press_escape
# L2 FX 页面语义
from .google_fx_helpers import (
    _verify_and_fix_fx_config,
    _make_response_handler,
    _connect_fx_page,
    _find_fx_prompt_input,
    click_fx_send_button,
    _ensure_output_dir,
    _get_panel_uuids,
    _normalize_model_name,
    wait_out_manual_intervention,
    _ManualInterventionTimeoutError,
    _find_add2_btn,
    _mount_video_prompt_refs,
    _submit_video_to_canvas,
    _inspect_all_pending_tiles,
    _scan_canvas_tiles,
    _distinct_slices,
    _click_new_project_button,
    _dismiss_active_agent_mode,
    _dismiss_unexpected_overlays,
)

# 🚨 打包批量上传 (_upload_images_to_canvas_bulk) 依赖一个未经现场验证的假设：
# Flow 画布会严格按本地 FileList 顺序渲染新图的 <img> DOM 节点。图片解码/渲染完成
# 的先后顺序实际可能与上传顺序不一致（文件大小不同、网络抖动等），一旦错位，
# path→uuid 映射整批出错，会导致同一批次里多个视频被安上错误的首尾帧（而提示词
# 文本不受影响）——这是 2026 年多次"首尾帧/提示词错乱"投诉最可疑的根因。
# 逐张上传（_upload_image_to_canvas，每张图各自上传+网络响应比对）顺序天然正确，
# 已验证可靠。在打包上传的 DOM 顺序假设被现场验证之前，默认关闭打包上传、
# 强制走逐张上传（会更慢，但保证首尾帧不串）。验证方法见 spark-video-batch-pipeline
# 记忆文档；确认 Flow 严格保序后，可将此常量改回 True 以恢复打包上传的速度。
_ENABLE_BULK_CANVAS_UPLOAD = False


def _upload_image_to_canvas(page, local_path, timeout=45, extra_known_uuids=None):
    """
    通过 Canvas Create (add_2) 按钮上传本地图片到画布，并利用网络拦截获取其 UUID。
    这是最稳定的上传方式，完全避开了弹窗裁剪流程。
    """
    abs_path = os.path.abspath(local_path)

    add2_btn = _find_add2_btn(page)
    if not add2_btn:
        log("  ❌ Canvas 上传: 未找到 Create (add_2) 按钮", "GoogleFX")
        return None

    try:
        add2_btn.click()
        random_sleep(1.5, 2.0)
    except Exception as e:
        log(f"  ❌ Canvas 上传: 点击 Create 失败: {e}", "GoogleFX")
        return None

    known_uuids = _get_panel_uuids(page)
    if extra_known_uuids:
        known_uuids = known_uuids.union(extra_known_uuids)

    file_input = None
    for _fi_sel in ["input[type='file']", "input[accept*='image']"]:
        try:
            _fi = page.locator(_fi_sel).first
            if _fi.count() > 0:
                file_input = _fi
                break
        except Exception:
            pass

    if not file_input:
        upload_sels = [
            "button:has-text('Upload')", "button:has-text('上传')",
            "[role='button']:has-text('Upload')", "[role='button']:has-text('上传')",
            "div[class*='upload']", "label:has-text('Upload')",
            "button[aria-label*='Upload']", "button[aria-label*='上传']",
        ]
        for _up_sel in upload_sels:
            try:
                _matches = page.locator(_up_sel)
                for _idx in range(_matches.count()):
                    _el = _matches.nth(_idx)
                    if _el.is_visible(timeout=2000):
                        _el.click(force=True)
                        random_sleep(0.5, 1.0)
                        break
                else:
                    continue
                break
            except Exception:
                continue

        for _fi_sel in ["input[type='file']", "input[accept*='image']"]:
            try:
                _fi = page.locator(_fi_sel).first
                if _fi.count() > 0:
                    file_input = _fi
                    break
            except Exception:
                pass

    if not file_input:
        log("  ❌ Canvas 上传: 未找到 file input", "GoogleFX")
        _safe_press_escape(page, "Canvas 上传 file input 未找到")
        return None

    # 设置网络监听以精确捕获新上传的图片 UUID
    captured_data = []
    handle_response = _make_response_handler(captured_data, mode="image")
    page.on("response", handle_response)

    try:
        file_input.set_input_files(abs_path)
        log(f"  ✅ Canvas set_input_files: {os.path.basename(abs_path)}", "GoogleFX")

        log("  ⏳ 等待上传图片出现在画布...", "GoogleFX")
        new_uuid = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            # 1. 优先从网络拦截中寻找新 UUID
            dedup_candidate = None
            for timestamp, url in list(captured_data):
                m = re.search(r'name=([0-9a-f\-]{30,})', url)
                if m:
                    uuid_cand = m.group(1)
                    if uuid_cand not in known_uuids:
                        new_uuid = uuid_cand
                        log(f"  🎉 通过网络捕获到新上传图片 UUID={new_uuid[:16]}...", "GoogleFX")
                        break
                    else:
                        dedup_candidate = uuid_cand
            if new_uuid:
                break
            if dedup_candidate:
                new_uuid = dedup_candidate
                log(f"  🎉 通过网络捕获到去重上传图片 UUID={new_uuid[:16]}...", "GoogleFX")
                break

            # 2. 兜底通过 DOM 扫描对比 UUID 集合
            cur_uuids = _get_panel_uuids(page)
            new_uuids = cur_uuids - known_uuids
            if new_uuids:
                new_uuid = next(iter(new_uuids))
                log(f"  🎉 通过 DOM 扫描到上传图片: UUID={new_uuid[:16]}...", "GoogleFX")
                break
            time.sleep(1)
    finally:
        page.remove_listener("response", handle_response)

    _safe_press_escape(page, "Canvas 上传关闭对话框")
    return new_uuid


def _resolve_canvas_file_input(page):
    """点击 Create (add_2) 后，定位画布上传用的 <input type=file>。返回 locator 或 None。"""
    # 🔧 2026-07-03: 优先尝试触发上传按钮，因为即使 DOM 中已存在 input，如果不触发按钮也可能处于非激活状态。
    upload_sels = [
        "button:has-text('Upload')", "button:has-text('上传')",
        "[role='button']:has-text('Upload')", "[role='button']:has-text('上传')",
        "div[class*='upload']", "label:has-text('Upload')",
        "button[aria-label*='Upload']", "button[aria-label*='上传']",
        "button[role='menuitem']:has-text('上传')",
        "button[role='menuitem']:has-text('Upload')",
    ]
    for _up_sel in upload_sels:
        try:
            _matches = page.locator(_up_sel)
            for _idx in range(_matches.count()):
                _el = _matches.nth(_idx)
                if _el.is_visible(timeout=2000):
                    _el.click(force=True)
                    random_sleep(0.8, 1.5)
                    break
            else:
                continue
            break
        except Exception:
            continue

    file_input = None
    for _fi_sel in ["input[type='file']", "input[accept*='image']"]:
        try:
            _fi = page.locator(_fi_sel).first
            if _fi.count() > 0:
                file_input = _fi
                break
        except Exception:
            pass
    return file_input


def _upload_images_to_canvas_bulk(page, local_paths, timeout=120):
    """
    🎯 打包一次性上传：单次 set_input_files 把该批次全部参考图一起传到画布，
    而不是一张接一张地点击 Create→上传。

    返回 {original_path: uuid} 映射；其 key 与传入的 local_paths 一一对应（保持顺序）。
    任一环节失败（画布不支持多选 / 数量不匹配 / DOM 顺序无法确定）时返回 None，
    调用方据此回退到逐张上传，保证不发生回退即出错的情况。

    映射依据：Flow 画布保持插入顺序（与 _mount_video_prompt_refs 的既有假设一致），
    因此按文档顺序读取「本次新增的」图片 UUID，与文件列表顺序逐一对应。
    """
    valid = [(p, os.path.abspath(p)) for p in local_paths if p and os.path.exists(p)]
    if not valid:
        return None
    abs_paths = [ap for _, ap in valid]

    add2_btn = _find_add2_btn(page)
    if not add2_btn:
        log("  ❌ 打包上传: 未找到 Create (add_2) 按钮", "GoogleFX")
        return None
    try:
        add2_btn.click()
        random_sleep(1.5, 2.0)
    except Exception as e:
        log(f"  ❌ 打包上传: 点击 Create 失败: {e}", "GoogleFX")
        return None

    known_uuids = _get_panel_uuids(page)

    file_input = _resolve_canvas_file_input(page)
    if not file_input:
        log("  ❌ 打包上传: 未找到 file input", "GoogleFX")
        _safe_press_escape(page, "打包上传 file input 未找到")
        return None

    captured_data = []
    handle_response = _make_response_handler(captured_data, mode="image")
    page.on("response", handle_response)
    new_uuids = set()
    try:
        try:
            # ⭐ 核心：一次性把全部文件交给同一个 input
            file_input.set_input_files(abs_paths)
        except Exception as e:
            log(f"  ⚠️ 打包 set_input_files 失败（画布可能不支持多选）: {e}", "GoogleFX")
            return None

        names = ", ".join(os.path.basename(ap) for ap in abs_paths)
        log(f"  ✅ 打包 set_input_files: 一次性提交 {len(abs_paths)} 张 ({names})", "GoogleFX")
        log(f"  ⏳ 等待 {len(abs_paths)} 张图片全部出现在画布...", "GoogleFX")

        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = _get_panel_uuids(page)
            new_uuids = cur - known_uuids
            if len(new_uuids) >= len(abs_paths):
                log(f"  🎉 已检测到 {len(new_uuids)} 张新图全部就绪", "GoogleFX")
                break
            time.sleep(1)
    finally:
        page.remove_listener("response", handle_response)

    _safe_press_escape(page, "打包上传关闭对话框")

    if len(new_uuids) < len(abs_paths):
        log(f"  ⚠️ 打包上传: 仅检测到 {len(new_uuids)}/{len(abs_paths)} 张新图，回退逐张上传", "GoogleFX")
        return None

    # 读取本次新增图片在 DOM 中的文档顺序（== 文件列表顺序）
    try:
        ordered = page.evaluate(r"""() => {
            const seen = [];
            const imgs = Array.from(document.querySelectorAll('img[src*="getMediaUrlRedirect"]'));
            for (const img of imgs) {
                const src = img.getAttribute('src') || '';
                const m = src.match(/name=([0-9a-f\-]{30,})/);
                if (m && !seen.includes(m[1])) seen.push(m[1]);
            }
            return seen;
        }""")
    except Exception as e:
        log(f"  ⚠️ 打包上传: 读取 DOM 顺序失败 ({e})，回退逐张上传", "GoogleFX")
        return None

    ordered_new = [u for u in ordered if u in new_uuids]
    if len(ordered_new) < len(abs_paths):
        log(f"  ⚠️ 打包上传: DOM 顺序仅解析出 {len(ordered_new)}/{len(abs_paths)}，回退逐张上传", "GoogleFX")
        return None

    mapping = {}
    for (orig_path, _abs), uuid in zip(valid, ordered_new):
        mapping[orig_path] = uuid
        log(f"  🔗 打包映射: {os.path.basename(orig_path)} → {uuid[:16]}...", "GoogleFX")
    log(f"  ✅ 打包上传完成：{len(mapping)} 张图片一次性上传并映射就绪", "GoogleFX")
    return mapping


def _generate_video_google_fx(req: VideoRequest):
    """
    Veo 视频生成主流程。
    """
    browser = None
    captured_data = []
    result = {"status": "failed", "video_url": None, "message": ""}
    # 规整传入的模型名，防止如 "veo 3.1 lite" 导致不匹配
    req.model = _normalize_model_name(req.model, is_video=True)
    log(f"🚀 Veo 3.1 视频生成请求: {req.prompt[:30]}... [模型: {req.model}]", "GoogleFX-Video")

    try:
        with sync_playwright() as p:
            browser, page = _connect_fx_page(p)
            if not page:
                raise RuntimeError("无法连接到 Google FX 浏览器页面")

            page.bring_to_front()

            if "labs.google" not in page.url:
                page.goto("https://labs.google/fx/tools/flow", timeout=60000)
                random_sleep(1, 2)
            ensure_flow_workspace(page)

            # 🛠️ 0. 新建项目（中英文 UI 共用，并验证确实进入新的项目 URL）
            if not _click_new_project_button(page):
                log("⚠️ 未能确认新项目已创建，将在当前页面继续", "GoogleFX-Video")

            # 🛠️ 1. 等待底部工具栏
            log("📍 等待底部工具栏...", "GoogleFX-Video")
            for i in range(30):
                has_input = False
                try: has_input = page.locator("textarea").first.is_visible()
                except: pass
                if not has_input:
                    try: has_input = page.locator("[contenteditable='true']").first.is_visible()
                    except: pass
                if has_input:
                    log("✅ 底部工具栏已加载", "GoogleFX-Video")
                    break
                time.sleep(1)

            # 🛠️ 1.2 如果有本地参考图，先上传到画布并获取 UUID
            img_path = clean_path(req.image)
            end_img_path = clean_path(req.end_image)
            has_start = bool(img_path)
            has_end = bool(end_img_path)

            start_uuid = None
            end_uuid = None
            if has_start or has_end:
                # 检查 Create 按钮是否可见。如果不可见，说明当前处于不支持画布上传的模式，需临时切换回 Image 模式
                if not _find_add2_btn(page):
                    log("⚙️ Create 按钮未找到，临时切换到 Image 模式以进行图片上传...", "GoogleFX-Video")
                    _verify_and_fix_fx_config(page, model="Nano Banana 2", ratio=req.ratio, want_video=False, context_label="临时切换Image")

                if has_start and img_path and os.path.exists(img_path):
                    start_uuid = _upload_image_to_canvas(page, img_path)
                    if not start_uuid:
                        raise RuntimeError("首帧图片上传到画布失败")
                    # 等待首帧在画布中就绪，确保其 UUID 已经被 DOM 记录
                    log(f"  ⏳ 等待首帧 {start_uuid[:16]}... 在画布中就绪", "GoogleFX-Video")
                    for _ in range(10):
                        if start_uuid in _get_panel_uuids(page):
                            break
                        time.sleep(1)
                if has_end and end_img_path and os.path.exists(end_img_path):
                    extra_known = {start_uuid} if start_uuid else None
                    end_uuid = _upload_image_to_canvas(page, end_img_path, extra_known_uuids=extra_known)
                    if not end_uuid:
                        raise RuntimeError("尾帧图片上传到画布失败")

            # 🛠️ 2. 验证并切换配置到 Video 模式 + 指定模型 + VIDEO_FRAMES 视频子模式
            log("⚙️ 切换配置到 Video / 帧 模式...", "GoogleFX-Video")
            _verify_and_fix_fx_config(
                page,
                model=req.model,
                ratio=req.ratio,
                want_video=True,
                context_label="切换Video",
                video_submode="VIDEO_FRAMES"
            )

            # 🛠️ 3. 挂载参考图到提示词框（Start -> End 顺序）
            prompt_has_refs = False
            if has_start or has_end:
                start_ref = start_uuid or ""
                end_ref = end_uuid or ""
                mounted_prompt_refs = _mount_video_prompt_refs(
                    page,
                    start_ref=start_ref,
                    end_ref=end_ref,
                )
                expected_prompt_refs = min(len([ref for ref in [start_ref, end_ref] if str(ref or "").strip()]), 2)
                prompt_has_refs = expected_prompt_refs > 0 and len(mounted_prompt_refs) >= expected_prompt_refs
                if not prompt_has_refs:
                    raise RuntimeError(f"参考图挂载失败 ({len(mounted_prompt_refs)}/{expected_prompt_refs})")
                log(f"✅ 视频参考卡片已挂载完成 ({len(mounted_prompt_refs)}/{expected_prompt_refs})", "GoogleFX-Video")

            # 🛠️ 4. 输入提示词 (Slate.js 富文本编辑器)
            slate_sel = "[data-slate-editor='true']"
            input_el = None
            try:
                sl = page.locator(slate_sel).first
                if sl.is_visible():
                    input_el = sl
                    log("📝 检测到 Slate.js 编辑器", "GoogleFX-Video")
            except: pass

            if not input_el:
                for sel in ["textarea", "[contenteditable='true']"]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible():
                            input_el = el
                            break
                    except: pass

            if not input_el:
                raise RuntimeError("无法找到视频提示词输入框")

            # 填充提示词
            input_el.click()
            random_sleep(0.3, 0.5)
            page.keyboard.press("ControlOrMeta+a")
            random_sleep(0.1, 0.2)
            page.keyboard.press("Backspace")
            random_sleep(0.2, 0.3)

            page.evaluate("""(text) => {
                const editor = document.querySelector('[data-slate-editor="true"]');
                if (editor) {
                    editor.focus();
                    const event = new InputEvent('beforeinput', {
                        inputType: 'insertText',
                        data: text,
                        bubbles: true,
                        cancelable: true,
                        composed: true
                    });
                    editor.dispatchEvent(event);
                }
            }""", req.prompt)
            random_sleep(0.5, 0.8)

            log("🔍 准备生成并扫描资源...", "GoogleFX-Video")
            existing_urls_set = set(page.evaluate("""() => {
                return Array.from(document.querySelectorAll('video'))
                    .map(v => v.src || v.currentSrc)
                    .filter(Boolean);
            }"""))

            # 注册网络监听器以捕获视频生成结果
            handle_response = _make_response_handler(captured_data, mode="video")
            page.on("response", handle_response)
            click_time = time.time()

            # 🛠️ 5. 点击发送
            click_fx_send_button(page, input_el)

            log(f"📡 正在等待视频生成 (最大 {get_runtime_max_wait_seconds() * 5}s)...", "GoogleFX-Video")
            wait_start = time.time()

            while time.time() - wait_start < get_runtime_max_wait_seconds() * 5:
                if captured_data:
                    for ts, url in reversed(captured_data):
                        if url not in existing_urls_set and ts > click_time:
                            log(f"✅ 网络响应捕获到视频: {url[:80]}...", "GoogleFX-Video")
                            result.update({"status": "success", "video_url": url})
                            break
                if result["status"] == "success": break

                dom_src = page.evaluate("""(existingUrls) => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    for (const v of videos) {
                        const src = v.src || v.currentSrc;
                        if (src && !existingUrls.includes(src)) return src;
                    }
                    return null;
                }""", list(existing_urls_set))

                if dom_src and dom_src not in existing_urls_set:
                    log(f"✅ DOM扫描发现新视频", "GoogleFX-Video")
                    result.update({"status": "success", "video_url": dom_src})
                    break

                time.sleep(1)

            if result["status"] != "success":
                raise Exception("超时未检测到视频文件")

            # 🎬 下载视频到本地
            vid_url = result["video_url"]
            if vid_url:
                output_dir = req.output_path if (hasattr(req, "output_path") and req.output_path) else os.path.join(OUTPUT_DIR, "videos")
                if not os.path.exists(output_dir): os.makedirs(output_dir)
                # 优先寻找直链进行高速下载
                direct_url = None
                for ts, curl in reversed(captured_data):
                    if "storage.googleapis.com" in curl and ts > click_time:
                        direct_url = curl
                        break

                local_path = os.path.join(output_dir, f"veo3_{int(time.time())}.mp4")
                downloaded = False
                if direct_url:
                    try:
                        log(f"⚡ 直链下载: {direct_url[:80]}...", "GoogleFX-Video")
                        r = requests.get(direct_url, stream=True, timeout=30)
                        r.raise_for_status()
                        with open(local_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        downloaded = True
                        log(f"✅ 直链下载完成: {local_path}", "GoogleFX-Video")
                    except Exception as e:
                        log(f"⚠️ 直链下载失败: {e}, 回退浏览器下载", "GoogleFX-Video")

                if not downloaded:
                    local_path = download_video_via_browser(page, vid_url, output_dir, "veo3")

                result.update({"video_url": local_path})

    except Exception as e:
        log(f"❌ generate_video_veo 内部错误: {e}", "Error")
        result["message"] = str(e)
    finally:
        if browser:
            try: browser.close()
            except: pass

    return result


_generate_video_google_fx_unlocked = _generate_video_google_fx


class _IPBlockedError(Exception):
    """Google 安全检测拦截（异常活动/unusual activity），冷却换号后重试。"""
    pass


# 2026-07-25：把"被判异常活动就强制换 IP"统一改成换号重试。换 IP 只换出口地址，
# 账号侧的风控评分不会跟着重置，实测换完照样被判；而频繁换 IP 反而会把 Flow 的
# 登录 token 打失效（见 SPARK server_common 的「换号不换 IP 阶梯」）。现在递增冷却
# 后切到号池的下一个可用账号重试，IP 轮换完全交回 MIYA_ROTATE_THRESHOLD 配置的
# 正常节奏（开浏览器前按请求数轮换）。换号实现见 utils.account_pool.switch_to_next_account。


# ── 批量流程参数 ──
VIDEO_CHUNK_SIZE = 5             # 每批提交的视频任务数
UPLOAD_GROUP_SIZE = 8            # 参考图分组上传：每组最多张数
MAX_GEN_RETRIES_PER_CHUNK = 1    # 非 IP 原因失败（生成失败/超时/校验拒收）的有限重试轮数
_IP_BACKOFF_STEP_SECS = 20       # IP 封禁重试的递增冷却步长
_IP_BACKOFF_CAP_SECS = 90        # 冷却封顶

# 首尾帧复用同一张图（如 HERO 展示视频：起止锚点都是帧序列最后一张完工图）在
# path_to_uuid 缓存里的"第二份"专用键后缀。2026-07-23 实测确诊：把同一 UUID 同时
# 作为 start/end 两个 chip 挂载时，Flow 挂载逻辑（_mount_flow_images_to_prompt）
# 按 UUID 去重，第二个引用被折叠掉，只能挂上 1/2；随后走"直接上传到 Start/End
# 槽位"兜底，但该兜底在重试轮里有时也找不到槽位容器（0/2），
# CANVAS_MOUNT_FAILED 未被单任务捕获，直接捅穿 _ChunkRunner.run()，
# 让整批任务（含已成功的其他视频）一起失败。修复：把同一张本地图上传两次，拿到
# 两个独立 UUID，让挂载走和"两张不同图"完全一样、已验证稳定的路径，从根上绕开
# 这个折叠 bug；第二份 UUID 用 path + 本后缀 作为 path_to_uuid 的键存取。
_DUP_END_REF_KEY_SUFFIX = "\x00__dup_end_ref__"


def _real_upload_path(key):
    """把 _upload_references 里可能带 _DUP_END_REF_KEY_SUFFIX 的内部 key 还原为真实本地路径。"""
    if key.endswith(_DUP_END_REF_KEY_SUFFIX):
        return key[:-len(_DUP_END_REF_KEY_SUFFIX)]
    return key

# IP 封禁重试上限：连续被封达此次数后停止重试、将剩余任务标记为失败。
# 旧行为是不设上限（while True 无限重试），单个 IP/账号被持续风控时会
# 无限循环、长时间占用全局运行锁并拖垮整个服务。默认 10 次（每次递增冷却最多
# 90s，约合最多 ~15 分钟）。设为 0 或负数表示不限制（保留旧行为，谨慎使用）。
try:
    MAX_IP_RETRIES = int(os.getenv("GOOGLE_FX_MAX_IP_RETRIES", "10"))
except (TypeError, ValueError):
    MAX_IP_RETRIES = 10


class _UuidRefRequest:
    """提交画布时用的请求视图：image/end_image 换成画布 UUID，保留原始路径备查。"""

    def __init__(self, original_req, start_uuid, end_uuid):
        self.prompt = original_req.prompt
        self.image = start_uuid or ""
        self.end_image = end_uuid or ""
        self.model = original_req.model
        self.ratio = original_req.ratio
        self.output_path = getattr(original_req, 'output_path', None)
        self.original_image = original_req.image
        self.original_end_image = original_req.end_image


class _ChunkRunner:
    """单个 chunk（≤VIDEO_CHUNK_SIZE 个视频任务）的执行器（2026-07-04 重构，
    行为与旧的单体函数一致，按相位拆分便于维护）。

    每轮 (_run_round)：连浏览器 → 备页 → 认领历史卡片 → 上传参考图 → 提交任务
    → 并行等待 → 下载上报。IP 被封（unusual activity）时递增冷却 + 换号后重跑，最多
    MAX_IP_RETRIES 次（超限则将剩余任务标记为失败，避免无限循环拖垮服务）；
    非 IP 失败（生成失败/超时/校验拒收）允许 MAX_GEN_RETRIES_PER_CHUNK
    轮有限重试，重试轮优先认领画布上已完成的卡片，避免重复提交烧配额。

    不再在每批开始时无条件强制换 IP（2026-07-04 复盘：上一批能跑完说明当前 IP
    是"活的好 IP"，强制轮换等于主动放弃它去抽一个未知 IP，实测造成连环封禁）。
    """

    def __init__(self, total_reqs, chunk_start, chunk, all_slices, on_progress, cancel_check):
        self.total_reqs = total_reqs
        self.chunk_start = chunk_start
        self.chunk = chunk
        self.on_progress = on_progress
        self.cancel_check = cancel_check
        # 本 chunk 的提示词区分性切片（tile 归属核对 / 认领用）。切片必须基于
        # 全批次提示词计算（剔除公共 boilerplate 前缀），否则没有区分度。
        self.chunk_slices = {s: all_slices.get(chunk_start + s, '') for s in range(len(chunk))}
        self.completed = set()       # 已成功下载并通过校验的 sub_idx
        self.results = {}            # sub_idx -> item_result dict
        self.project_url = None      # 整批任务共享的 Flow 项目页 URL（由主流程在 chunk 间传递）
        # 跨轮缓存：本地路径 → 画布 UUID。参考图是项目级资产，重试轮回到同一
        # 项目页后仍在画布上，无需重复上传（之前每次 IP/失败重试都整批重传，
        # 一轮白耗 ~1 分钟且推高风控）。使用前会对照画布 DOM 逐个验证，
        # 画布上找不到的自动重新上传，验证不过不会错挂。
        self.path_to_uuid = {}
        self.gen_retry_used = 0
        self.ip_retry = 0
        # 失败换号重试：本 chunk 已经试过（并被判过封）的号池账号，换号时排除，
        # 免得在同一批里换回刚被判异常活动的那个号。
        self.tried_accounts = set()
        # 本轮浏览器会话内已确认过的配置 (model, ratio)；换会话/刷新页面即失效
        self._confirmed_config = None
        # 本轮提交产生的 tile 映射（等待/早期封禁扫描用）
        self._prompts_map = {}
        self._tile_slices = {}

    # ── 小工具 ──

    def _check_cancel(self):
        if self.cancel_check and self.cancel_check():
            raise ConnectionError("用户已取消视频生成")
        # 预算耗尽也应中止：让 IP 重试冷却循环 / 各等待轮次在超时后干净放弃，
        # 而不是把整个批次拖到很久。
        from ..utils import cancel_flag
        if cancel_flag.deadline_exceeded():
            raise ConnectionError("请求超时：超出时间预算 (request budget exceeded)")

    def _notify(self, idx, stage, payload):
        if not self.on_progress:
            return None
        try:
            return self.on_progress(idx, stage, payload)
        except ConnectionError as ce:
            log("检测到客户端已断开，中止批量生成", "GoogleFX-Video")
            raise ce

    def _manual_intervention_event(self, phase, code, reason, max_wait_secs):
        """wait_out_manual_intervention 的 on_event 回调：把登录失效/验证码等
        暂停状态转成 SPARK 进度事件，驱动前端常驻横幅——不然只写日志很容易被
        忽略，人工不知道要去 AdsPower 窗口处理。用本 chunk 第一个任务的全局
        槽位号作为事件锚点（这类状态是连接/页面级的，不属于单个任务）。"""
        self._notify(self.chunk_start, f"manual_intervention_{phase}", {
            "code": code, "reason": reason, "max_wait_secs": max_wait_secs,
        })

    # ── 驱动循环 ──

    def run(self):
        """跑完本 chunk（IP 封禁最多重试 MAX_IP_RETRIES 次；非 IP 失败有限重试）。
        返回按 sub_idx 顺序的结果列表。"""
        while True:
            remaining = [
                (sub_idx, self.chunk[sub_idx])
                for sub_idx in range(len(self.chunk))
                if sub_idx not in self.completed
            ]
            if not remaining:
                log("✅ 本批次所有任务已完成，无需重试", "GoogleFX-Video")
                break

            if self.ip_retry > 0:
                log(
                    f"🔄 封禁重试第 {self.ip_retry} 次（已换号），"
                    f"剩余 {len(remaining)} 个未完成任务...",
                    "GoogleFX-Video",
                )

            try:
                if self._run_round(remaining):
                    break
            except _IPBlockedError:
                if MAX_IP_RETRIES > 0 and self.ip_retry >= MAX_IP_RETRIES:
                    log(
                        f"⛔ IP 连续被封已达上限 {MAX_IP_RETRIES} 次，停止重试，"
                        f"剩余 {len(remaining)} 个任务标记为失败（疑似账号风控 / security check）",
                        "Error",
                    )
                    self._fail_remaining(
                        remaining,
                        f"IP 连续被封达上限（{MAX_IP_RETRIES} 次），疑似账号风控 "
                        f"(unusual activity / security check)，需人工处理",
                    )
                    break
                self._cooldown_and_switch_account()
                continue
            except _ManualInterventionTimeoutError as e:
                # 登录失效/验证码/安全拦截，人工处理等待超时：只判本 chunk 剩余
                # 任务失败并留下明确原因，不炸穿整个批次——已完成的任务保留成果，
                # 人工处理完成后可针对失败任务单独重试。
                log(f"⛔ 人工处理等待超时，剩余 {len(remaining)} 个任务标记为失败: {e}", "Error")
                self._fail_remaining(remaining, f"需要人工登录/验证但等待超时: {e}")
                break
            except Exception as e:
                # TargetClosedError: 浏览器页面在多任务并发提交时意外关闭，
                # 重建浏览器连接后即可继续。不计入 IP 重试，直接重跑一轮。
                if "TargetClosedError" in type(e).__name__ or "Target page" in str(e):
                    log(f"⚠️ 浏览器页面意外关闭 (TargetClosedError)，重新连接后重试...", "GoogleFX-Video")
                    continue
                log(f"❌ 批量生成过程发生致命错误: {e}", "Error")
                raise

        return [
            self.results.get(sub_idx, {
                "status": "failed",
                "video_url": None,
                "message": "未知错误：任务未被处理",
            })
            for sub_idx in range(len(self.chunk))
        ]

    def _fail_remaining(self, remaining, message):
        """将本 chunk 尚未完成的槽位标记为失败（用于放弃重试的场景，如 IP 重试上限耗尽）。"""
        for sub_idx, _ in remaining:
            if sub_idx not in self.completed:
                self.results[sub_idx] = {
                    "status": "failed",
                    "video_url": None,
                    "message": message,
                }

    def _run_round(self, remaining):
        """一轮完整尝试。返回 True 表示本 chunk 处理完毕（可能含最终失败的槽位），
        False 表示需要再跑一轮（非 IP 失败的有限重试）。
        _IPBlockedError / ConnectionError / 其他异常原样抛给 run()；
        浏览器在 finally 中关闭（冷却重试前必须先关，下一轮会重新连浏览器）。"""
        browser = None
        try:
            with sync_playwright() as p:
                browser, page = _connect_fx_page(p, cancel_check=self.cancel_check,
                                                  on_event=self._manual_intervention_event)
                if not page:
                    raise RuntimeError("无法连接到 Google FX 浏览器页面")
                page.bring_to_front()

                self._confirmed_config = None
                self._prompts_map = {}
                self._tile_slices = {}

                self._prepare_page(page)
                adopted, remaining = self._adopt_completed_tiles(page, remaining)
                path_to_uuid = self._upload_references(page, remaining)
                submitted = self._submit_tasks(page, remaining, adopted, path_to_uuid)
                self._await_generation(page, submitted)
                self._download_and_report(page, submitted)
        finally:
            if browser:
                try: browser.close()
                except: pass

        # 🔁 非 IP 原因的失败（生成失败/等待超时/下载失败/校验拒收）允许有限重试：
        # 重试轮会先尝试"认领"画布上可能已经完成的卡片，认领不到才重新提交。
        # 复盘中 slot 6/7 就是等待超时后被直接放弃、成片缺段。
        failed_subs = [s for s in range(len(self.chunk)) if s not in self.completed]
        if failed_subs and self.gen_retry_used < MAX_GEN_RETRIES_PER_CHUNK:
            self.gen_retry_used += 1
            log(
                f"🔁 本批次仍有 {len(failed_subs)} 个任务未完成"
                f"（槽位 {[self.chunk_start + s + 1 for s in failed_subs]}），"
                f"进行第 {self.gen_retry_used} 次失败重试...",
                "GoogleFX-Video",
            )
            return False
        return True

    # ── 相位 1: 项目导航 + 页面就绪 ──

    def _prepare_page(self, page):
        """项目导航：
        - 首轮：强制回 Flow 首页并新建项目，保证画布干净。旧逻辑只在 URL 不含
          labs.google 时才导航，而项目页 URL 同样含 labs.google —— 静默沿用旧项目
          导致画布堆满历史/其他任务的卡片，是"下载到无关视频"（跨任务串片）的
          直接温床（2026-07-04 复盘确诊）。
        - 重试轮：回到本 chunk 固定的项目页，保留此前提交的卡片供认领复用。
        然后等待底部工具栏就绪；超时则主动刷新一次页面再等（换 IP 后页面可能
        卡在空白状态，光等不会自愈——2026-07-01 实测这样丢过整批任务）。"""
        if self.project_url:
            try:
                if page.url != self.project_url:
                    page.goto(self.project_url, timeout=60000)
                    random_sleep(2, 3)
                log("📌 已回到本批次项目页（重试轮，保留历史卡片）", "GoogleFX-Video")
            except Exception as nav_err:
                log(f"⚠️ 回到项目页失败: {nav_err}，改为新建项目", "GoogleFX-Video")
                self.project_url = None

        if not self.project_url:
            # 新建项目 = 全新画布，上一个项目里传的参考图不在这里，缓存全部失效
            self.path_to_uuid = {}
            try:
                page.goto("https://labs.google/fx/tools/flow", timeout=60000)
                random_sleep(1, 2)
            except Exception as nav_err:
                log(f"⚠️ 导航到 Flow 首页失败: {nav_err}", "GoogleFX-Video")
            ensure_flow_workspace(page)
            clicked_new = _click_new_project_button(page)
            if not clicked_new:
                log("⚠️ 未能新建项目，将在当前页面继续（画布可能残留历史卡片）", "GoogleFX-Video")

        # 🧹 导航刚完成、任何 FX 功能面板都不应处于打开状态的安全时机——
        # 顺手清理一遍可能残留的未知弹窗（Google 产品公告/条款更新提示等），
        # 避免它们在后续步骤里悄悄挡住点击。
        _dismiss_unexpected_overlays(page, "GoogleFX-Video")

        log("📍 等待底部工具栏...", "GoogleFX-Video")
        if self._wait_toolbar_ready(page, 30):
            log("✅ 底部工具栏已加载", "GoogleFX-Video")
        else:
            log("⚠️ 底部工具栏等待超时（30s），尝试刷新页面后重新等待...", "GoogleFX-Video")
            try:
                page.reload(timeout=60000)
                random_sleep(2, 3)
            except Exception as e:
                log(f"⚠️ 页面刷新失败: {type(e).__name__}: {e}", "GoogleFX-Video")
            self._confirmed_config = None
            if self._wait_toolbar_ready(page, 20):
                log("✅ 刷新后底部工具栏已加载", "GoogleFX-Video")
            else:
                log("⚠️ 刷新后仍未检测到底部工具栏，继续尝试后续步骤...", "GoogleFX-Video")

        # 记录本 chunk 的项目页 URL（新建项目后 URL 会带项目 id）
        if not self.project_url:
            try:
                if "labs.google" in page.url:
                    self.project_url = page.url
            except Exception:
                pass

    def _wait_toolbar_ready(self, page, max_wait_secs):
        agent_dismiss_tried = False
        manual_check_tried = False
        for i in range(max_wait_secs):
            self._check_cancel()
            has_input = False
            try: has_input = page.locator("textarea").first.is_visible()
            except Exception: pass
            if not has_input:
                try: has_input = page.locator("[contenteditable='true']").first.is_visible()
                except Exception: pass
            if has_input:
                return True
            # 🔁 等到还剩一半时间仍未出现时，尝试一次"取消智能体模式"——换 IP 重连后
            # 页面有时会停在智能体对话模式，标准输入框被替换成聊天框，光等不会自愈
            # (2026-07-23 server.log 实测: 30s 超时+刷新后再 20s 仍等不到工具栏)。
            if not agent_dismiss_tried and i >= max_wait_secs // 2:
                agent_dismiss_tried = True
                if _dismiss_active_agent_mode(page, "GoogleFX-Video"):
                    continue
            # 🧹 每 3s 顺手扫一遍未知弹窗——工具栏等不到的另一个常见原因就是
            # 有个没见过的弹窗挡在上面（新功能公告/条款更新等）。
            if i > 0 and i % 3 == 0:
                if _dismiss_unexpected_overlays(page, "GoogleFX-Video"):
                    continue
            # 🛑 2026-07-24: 此前这里对"工具栏迟迟等不到"完全没有区分原因——如果
            # 页面其实停在登录失效/验证码/安全拦截页，会白等满 30s+20s 后仍然
            # 硬着头皮往下走（上传/提交阶段），最终在更下游报出让人摸不着头脑
            # 的错误。这里在等到 1/3 时长时查一次，命中则暂停轮询等人工处理，
            # 而不是继续假装"可能只是慢"。
            if not manual_check_tried and i >= max(3, max_wait_secs // 3):
                manual_check_tried = True
                if not wait_out_manual_intervention(page, context_label="等待视频工具栏中",
                                                     cancel_check=self.cancel_check,
                                                     on_event=self._manual_intervention_event):
                    raise _ManualInterventionTimeoutError(
                        "等待视频工具栏时检测到需要人工处理，等待超时，已放弃本批次"
                    )
                agent_dismiss_tried = False
                continue
            time.sleep(1)
        return False

    # ── 相位 2: 认领画布上已完成的历史任务（仅重试轮） ──

    def _adopt_completed_tiles(self, page, remaining):
        """换 IP 中止时，此前已提交的生成任务仍在 Flow 后端继续跑；旧逻辑一律
        从头重新提交（复盘实测 slot 1/2 各被提交了 14 次，烧配额且把账号风控
        越推越高）。重试轮先扫描画布：内容匹配（提示词切片）且带 videoSrc 的
        完成卡片直接认领复用。返回 (adopted_tasks, still_remaining)。"""
        if not ((self.ip_retry > 0 or self.gen_retry_used > 0) and remaining):
            return [], remaining
        canvas_tiles = _scan_canvas_tiles(page)
        if not canvas_tiles:
            return [], remaining

        adopted = []
        still_remaining = []
        used_tile_ids = set()
        for sub_idx, req in remaining:
            slice_ = self.chunk_slices.get(sub_idx) or ''
            match = None
            if slice_:
                for t in canvas_tiles:
                    if not t.get('tileId') or t['tileId'] in used_tile_ids:
                        continue
                    if t.get('videoSrc') and not t.get('failed') \
                            and slice_ in (t.get('textClean') or ''):
                        match = t  # 保留文档序最后一个匹配（最新）
            if match:
                used_tile_ids.add(match['tileId'])
                idx = self.chunk_start + sub_idx
                log(f"♻️ 任务 {idx + 1} 认领画布上已完成的历史卡片，跳过重复提交", "GoogleFX-Video")
                adopted.append({
                    "sub_idx": sub_idx,
                    "idx": idx,
                    "req": req,
                    "tile_id": match['tileId'],
                    "click_time": time.time(),
                    "status": "success",
                    "video_url": match['videoSrc'],
                    "message": ""
                })
                self._notify(idx, 'video_start', {'prompt': req.prompt})
            else:
                still_remaining.append((sub_idx, req))
        if adopted:
            log(f"♻️ 本轮认领 {len(adopted)} 个已完成任务，仍需提交 {len(still_remaining)} 个", "GoogleFX-Video")
        return adopted, still_remaining

    # ── 相位 3: 参考图分组上传 ──

    def _verify_cached_uploads(self, page, wanted_paths, max_wait_secs=8):
        """对照画布 DOM 验证跨轮缓存：返回 {路径: UUID}，只含缓存里有且画布上
        确实存在的条目。重试轮刚回到项目页时图片卡片可能还在异步渲染，
        对期望存在的 UUID 做短暂轮询等待，超时未出现的按缺失处理（重新上传）。"""
        wanted = {p: self.path_to_uuid[p] for p in wanted_paths if p in self.path_to_uuid}
        if not wanted:
            return {}
        want_uuids = set(wanted.values())
        deadline = time.time() + max_wait_secs
        found = set()
        while time.time() < deadline:
            self._check_cancel()
            found = _get_panel_uuids(page) & want_uuids
            if found == want_uuids:
                break
            time.sleep(1)
        return {p: u for p, u in wanted.items() if u in found}

    def _upload_references(self, page, remaining):
        """收集本轮任务的全部参考图（首/尾帧去重），分组上传到画布。
        返回 {本地路径（或 _DUP_END_REF_KEY_SUFFIX 标记的第二份）: 画布 UUID}。

        参考图只上传一次：重试轮（IP 被封/生成失败）回到同一项目页时，上一轮
        传过的图仍是项目画布资产，经 DOM 验证存在后直接复用 UUID，只上传缺失
        的（旧行为是每轮整批重传，6 张图一轮白耗 ~1 分钟且加重风控）。

        起止帧复用同一张图的任务（如 HERO 展示视频）额外上传一次，拿到独立的
        第二个 UUID（见 _DUP_END_REF_KEY_SUFFIX 常量注释）——同一 UUID 挂载两次
        会被 Flow 端去重折叠，最终只能挂上 1/2 参考卡片。"""
        unique_images = []
        for _sub_idx, req in remaining:
            start_p = clean_path(req.image)
            end_p = clean_path(req.end_image)
            if start_p and os.path.exists(start_p) and start_p not in unique_images:
                unique_images.append(start_p)
            if start_p and end_p and start_p == end_p:
                dup_key = start_p + _DUP_END_REF_KEY_SUFFIX
                if dup_key not in unique_images:
                    unique_images.append(dup_key)
            elif end_p and os.path.exists(end_p) and end_p not in unique_images:
                unique_images.append(end_p)

        path_to_uuid = {}
        if not unique_images:
            return path_to_uuid

        # ── 跨轮复用：先认领画布上已有的，再决定还需要传哪些 ──
        reused = self._verify_cached_uploads(page, unique_images)
        if reused:
            path_to_uuid.update(reused)
            unique_images = [p for p in unique_images if p not in reused]
            log(f"♻️ 画布上已有 {len(reused)} 张参考图（跨轮复用，跳过重复上传），"
                f"仍需上传 {len(unique_images)} 张", "GoogleFX-Video")
        if not unique_images:
            log("✅ 本轮参考图全部复用画布已有资产，无需上传", "GoogleFX-Video")
            return path_to_uuid

        total_images = len(unique_images)
        total_groups = (total_images + UPLOAD_GROUP_SIZE - 1) // UPLOAD_GROUP_SIZE
        log(f"📤 开始分组上传该批次共 {total_images} 张参考图（每组最多 {UPLOAD_GROUP_SIZE} 张，共 {total_groups} 组）...", "GoogleFX-Video")

        if not _find_add2_btn(page):
            log("⚙️ Create 按钮未找到，临时切换到 Image 模式以进行图片上传...", "GoogleFX-Video")
            first_ratio = self.chunk[0].ratio if hasattr(self.chunk[0], 'ratio') else '9:16'
            _verify_and_fix_fx_config(page, model="Nano Banana 2", ratio=first_ratio,
                                      want_video=False, context_label="临时切换Image")

        self._check_cancel()

        for group_start in range(0, total_images, UPLOAD_GROUP_SIZE):
            group = unique_images[group_start : group_start + UPLOAD_GROUP_SIZE]
            group_num = group_start // UPLOAD_GROUP_SIZE + 1
            log(f"📤 上传第 {group_num}/{total_groups} 组图片（{len(group)} 张）...", "GoogleFX-Video")
            self._check_cancel()

            # 首选：单次 set_input_files 打包上传该组图片（仅在 _ENABLE_BULK_CANVAS_UPLOAD
            # 为 True 时尝试——见文件头注释，该路径的顺序假设未经现场验证，默认关闭）。
            # 组内含"同图第二份"标记 key 时跳过打包（打包按本地路径去重上传，无法
            # 表达"同一文件传两次拿两个 UUID"的诉求），回退逐张上传。
            has_dup_marker = any(k.endswith(_DUP_END_REF_KEY_SUFFIX) for k in group)
            bulk_map = _upload_images_to_canvas_bulk(page, group) \
                if (_ENABLE_BULK_CANVAS_UPLOAD and not has_dup_marker) else None
            if bulk_map and len(bulk_map) == len(group):
                path_to_uuid.update(bulk_map)
                self.path_to_uuid.update(bulk_map)  # 即时写回跨轮缓存
                # 等待该组新图在画布中就绪
                for uuid in bulk_map.values():
                    for _ in range(10):
                        self._check_cancel()
                        if uuid in _get_panel_uuids(page):
                            break
                        time.sleep(1)
                log(f"✅ 第 {group_num} 组打包上传成功", "GoogleFX-Video")
            else:
                # 逐张上传（打包上传关闭/失败时的默认路径，顺序天然正确）
                if _ENABLE_BULK_CANVAS_UPLOAD:
                    log(f"⚠️ 第 {group_num} 组打包上传未成功，回退到逐张上传模式...", "GoogleFX-Video")
                for img_path in group:
                    self._check_cancel()
                    real_path = _real_upload_path(img_path)
                    extra_known = set(path_to_uuid.values()) if path_to_uuid else None
                    uuid = _upload_image_to_canvas(page, real_path, extra_known_uuids=extra_known)
                    if not uuid:
                        raise RuntimeError(f"图片 {os.path.basename(real_path)} 上传到画布失败")
                    path_to_uuid[img_path] = uuid
                    # 即时写回跨轮缓存：即使本轮中途被封中止，已传的图下一轮也能复用
                    self.path_to_uuid[img_path] = uuid
                    log(f"  ⏳ 等待图片 {uuid[:16]}... 在画布中就绪", "GoogleFX-Video")
                    for _ in range(10):
                        self._check_cancel()
                        if uuid in _get_panel_uuids(page):
                            break
                        time.sleep(1)

            # 组间短暂暂停，避免触发画布限流
            if group_start + UPLOAD_GROUP_SIZE < total_images:
                random_sleep(1.0, 2.0)

        log("✅ 当前批次所有参考图已成功上传并就绪", "GoogleFX-Video")
        return path_to_uuid

    # ── 相位 4: 依次提交任务（不等待生成完成） ──

    def _ensure_video_config(self, page, req):
        """确认 Flow 处于 Video/帧 模式 + 指定模型/比例。
        同一浏览器会话内首个任务做完整校验（打开配置面板确认，~5s）；后续任务
        若 (model, ratio) 未变则跳过——提交动作本身不会改变模式/模型，重复打开
        面板纯属浪费（2026-07-01 实测 16 段任务约浪费 80s）。会话重建/页面刷新
        时 _confirmed_config 被重置，自动恢复完整校验。"""
        wanted = (req.model, req.ratio)
        if self._confirmed_config == wanted:
            log("⚙️ 本会话已确认过相同配置（模型/比例未变），跳过重复校验", "GoogleFX-Video")
            return
        _verify_and_fix_fx_config(
            page,
            model=req.model,
            ratio=req.ratio,
            want_video=True,
            context_label="切换Video",
            video_submode="VIDEO_FRAMES"
        )
        self._confirmed_config = wanted

    def _submit_tasks(self, page, remaining, adopted, path_to_uuid):
        """依次提交本轮任务到画布（不等待生成完成，仅等待新 tile 出现并用提示词
        切片核对归属）。认领的历史任务直接并入结果（状态已是 success）。
        每提交一个任务立即扫一次全部已提交 tile：账号在提交中途被封时立刻中止，
        不再浪费时间提交注定失败的剩余任务（每个提交要十几秒）。

        单任务提交异常（CANVAS_MOUNT_FAILED / 找不到输入框 / Generate 后无新 tile）
        就地标记该任务失败并继续提交其余任务，不让异常捅穿到 run()——那样会把
        本该只失败一段的情况变成整批（含已成功的其他视频）全部失败。IP 封禁 /
        连接取消 / 浏览器页面意外关闭仍按原样向上抛出，交由 run() 的既有恢复逻辑
        处理。"""
        submitted = []
        for _ad in adopted:
            submitted.append(_ad)
            self._prompts_map[_ad["tile_id"]] = _ad["req"].prompt
            self._tile_slices[_ad["tile_id"]] = self.chunk_slices.get(_ad["sub_idx"], '')

        # 当前已有的 tile_ids 作为基线，用于识别提交后新出现的 tile
        before_tile_ids = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('div[data-tile-id]'))
                        .map(el => el.getAttribute('data-original-tile-id') || el.getAttribute('data-tile-id'));
        }""")

        for sub_idx, req in remaining:
            idx = self.chunk_start + sub_idx
            log(f"🎬 正在提交第 {idx + 1}/{self.total_reqs} 段视频任务: {req.prompt[:30]}...", "GoogleFX-Video")
            self._check_cancel()
            self._notify(idx, 'video_start', {'prompt': req.prompt})

            self._ensure_video_config(page, req)

            _start_path = clean_path(req.image)
            _end_path = clean_path(req.end_image)
            _start_uuid = path_to_uuid.get(_start_path)
            if _start_path and _end_path and _start_path == _end_path:
                # 首尾帧复用同一张图（如 HERO 展示视频）：尾帧用 _upload_references
                # 里额外上传的"第二份"独立 UUID，避免同一 UUID 挂载两次被折叠成 1/2
                # （见 _DUP_END_REF_KEY_SUFFIX 注释）。缺失时退回主 UUID，至少不比
                # 修复前更差。
                _end_uuid = path_to_uuid.get(_start_path + _DUP_END_REF_KEY_SUFFIX) or _start_uuid
            else:
                _end_uuid = path_to_uuid.get(_end_path)
            log(
                f"🧭 任务 {idx + 1} 帧映射核对: "
                f"首帧={os.path.basename(_start_path) if _start_path else '无'}→{(_start_uuid or '未匹配')[:12]} | "
                f"尾帧={os.path.basename(_end_path) if _end_path else '无'}→{(_end_uuid or '未匹配')[:12]}",
                "GoogleFX-Video",
            )

            try:
                task_info = _submit_video_to_canvas(
                    page, _UuidRefRequest(req, _start_uuid, _end_uuid), before_tile_ids,
                    expect_slice=self.chunk_slices.get(sub_idx)
                )
            except (_IPBlockedError, ConnectionError):
                raise
            except Exception as submit_err:
                if "TargetClosedError" in type(submit_err).__name__ or "Target page" in str(submit_err):
                    # 浏览器页面意外关闭：不是这一个任务的问题，交给 run() 外层的
                    # TargetClosedError 恢复逻辑重建连接、整轮重跑。
                    raise
                # 🚨 单任务提交失败（参考图挂载失败/找不到输入框/Generate 后无新 tile
                # 等）不应拖垮整批——2026-07-23 实测：HERO 展示视频（首尾复用同一张
                # 图）在重试轮挂载失败后，CANVAS_MOUNT_FAILED 一路捅穿 run()，
                # 把本该只失败这一段的情况变成整批任务（含已成功的其他视频）全灭。
                # 这里就地记为失败，交给已有的 gen-retry 轮次机制重新尝试，其余任务
                # 继续正常提交。
                log(f"❌ 任务 {idx + 1} 提交失败，标记失败等待重试: {submit_err}", "GoogleFX-Video")
                submitted.append({
                    "sub_idx": sub_idx,
                    "idx": idx,
                    "req": req,
                    "tile_id": None,
                    "click_time": time.time(),
                    "status": "failed",
                    "video_url": None,
                    "message": f"提交失败: {submit_err}",
                })
                self._notify(idx, 'video_error', {'message': f"提交失败: {submit_err}"})
                continue

            tile_id = task_info["tile_id"]
            submitted.append({
                "sub_idx": sub_idx,
                "idx": idx,
                "req": req,
                "tile_id": tile_id,
                "click_time": task_info["click_time"],
                "status": "generating",
                "video_url": None,
                "message": ""
            })
            self._prompts_map[tile_id] = req.prompt
            self._tile_slices[tile_id] = self.chunk_slices.get(sub_idx, '')
            before_tile_ids.append(tile_id)

            # 🚨 提前检测 IP 被封：每提交完一个任务就扫一次已提交 tile，
            # 命中立即中止本轮提交（而不是等整批提交完才发现）
            early_states = _inspect_all_pending_tiles(
                page, [t["tile_id"] for t in submitted], self._prompts_map,
                slices_map=self._tile_slices
            )
            if any(s.get("isIpBlocked") for s in early_states.values()):
                log(
                    "🚨 提交过程中检测到 IP 被封（异常活动/unusual activity），"
                    "立即停止继续提交本批剩余任务，冷却换号后重跑...",
                    "GoogleFX-Video",
                )
                raise _IPBlockedError("IP blocked: unusual activity detected during submit")

            random_sleep(1.0, 1.5)

        log(f"📡 已成功提交 {len(submitted)} 个视频任务，开始并行等待生成...", "GoogleFX-Video")
        return submitted

    # ── 相位 5: 并行等待生成完成 ──

    def _await_generation(self, page, submitted):
        """并行轮询本轮全部已提交 tile 直到完成/失败/超时；
        任一 tile 报 unusual activity 立即抛 _IPBlockedError。"""
        pending_tile_ids = [
            t["tile_id"] for t in submitted
            if t["status"] not in ("success", "failed")
        ]
        wait_start = time.time()
        timeout_limit = get_runtime_max_wait_seconds() * 5
        poll_count = 0

        while pending_tile_ids and (time.time() - wait_start < timeout_limit):
            self._check_cancel()
            poll_count += 1
            # 🧹 生成动辄要等几分钟，是最容易被"没人盯着的时候冒出来的弹窗"
            # 悄悄挡住画布的时段——每 30s(每 5s 一轮 x6) 顺手清一遍。
            if poll_count % 6 == 0:
                _dismiss_unexpected_overlays(page, "GoogleFX-Video")
            states = _inspect_all_pending_tiles(page, pending_tile_ids, self._prompts_map,
                                                slices_map=self._tile_slices)

            if any(state.get("isIpBlocked") for state in states.values()):
                log(
                    "🚨 检测到 IP 被封（异常活动/unusual activity），"
                    "立即中止当前批次，冷却换号后重跑...",
                    "GoogleFX-Video",
                )
                raise _IPBlockedError("IP blocked: unusual activity detected")

            still_pending = []
            for task in submitted:
                if task["status"] in ("success", "failed"):
                    continue
                state = states.get(task["tile_id"], {})
                status = state.get("status", "generating")
                if status == "done":
                    video_src = state.get("videoSrc")
                    log(f"✅ 任务 {task['idx'] + 1} 生成成功! URL: {video_src[:80]}...", "GoogleFX-Video")
                    task["status"] = "success"
                    task["video_url"] = video_src
                elif status == "failed":
                    err_msg = state.get("failedText") or "生成失败"
                    log(f"❌ 任务 {task['idx'] + 1} 生成失败: {err_msg}", "GoogleFX-Video")
                    task["status"] = "failed"
                    task["message"] = err_msg
                else:
                    still_pending.append(task["tile_id"])
                    progress = state.get("progress")
                    if progress is not None:
                        log(f"⏳ 任务 {task['idx'] + 1} 正在生成: {progress}%", "GoogleFX-Video")

            pending_tile_ids = still_pending
            if pending_tile_ids:
                time.sleep(5)

    # ── 相位 6: 下载 + 上报（SPARK 侧锚点校验可拒收） ──

    def _download_and_report(self, page, submitted):
        """依次下载生成成功的视频并逐个上报。SPARK 侧回调对下载内容做锚点校验，
        返回 'rejected'（首尾帧与锚点图不符=串片）时撤销完成标记，
        让该槽位进入失败重试轮重新生成。"""
        for task in submitted:
            sub_idx = task["sub_idx"]
            idx = task["idx"]
            req = task["req"]
            item_result = {"status": "failed", "video_url": None, "message": ""}

            if task["status"] == "success" and task["video_url"]:
                try:
                    output_dir = req.output_path if (hasattr(req, "output_path") and req.output_path) \
                        else os.path.join(OUTPUT_DIR, "videos")
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    local_path = download_video_via_browser(page, task["video_url"], output_dir, f"veo3_{idx}")
                    item_result.update({"status": "success", "video_url": local_path})
                    self.completed.add(sub_idx)
                except Exception as download_err:
                    log(f"⚠️ 下载任务 {idx + 1} 视频失败: {download_err}", "GoogleFX-Video")
                    item_result.update({"status": "failed", "message": f"下载失败: {download_err}"})
            else:
                item_result.update({
                    "status": "failed",
                    "message": task["message"] or "超时未检测到视频文件或生成失败"
                })

            self.results[sub_idx] = item_result

            if item_result["status"] == "success":
                cb_ret = self._notify(idx, 'video_done', item_result)
                if cb_ret == 'rejected':
                    log(f"🚫 任务 {idx + 1} 下载内容未通过锚点校验，标记失败待重试", "GoogleFX-Video")
                    self.completed.discard(sub_idx)
                    item_result = {
                        "status": "failed",
                        "video_url": None,
                        "message": "下载内容与锚点帧不符，已被拒收"
                    }
                    self.results[sub_idx] = item_result
            else:
                self._notify(idx, 'video_error', item_result)

    # ── IP 封禁处理 ──

    def _cooldown_and_switch_account(self):
        """被判异常活动：递增冷却 + 换号后重试（不再强制换 IP，理由见
        _IPBlockedError 上方注释）。

        冷却保留：立刻按相同节奏重放大概率再次触发（2026-07-01 实测同一 chunk
        连续被封 2 次），所以按重试次数递增等待（20s/40s/60s... 封顶 90s），给
        风控评分留出冷却时间，也减少重复上传参考图的频率。
        换号：切到号池里的下一个可用账号，下一轮 _run_round 重连浏览器时自然落到
        新 profile 上。没号可换（池子为空/都被排除）就只冷却后原地重试。"""
        self.ip_retry += 1
        log(f"🔄 被判异常活动，第 {self.ip_retry} 次冷却换号重试...", "GoogleFX-Video")
        backoff_secs = min(_IP_BACKOFF_STEP_SECS * self.ip_retry, _IP_BACKOFF_CAP_SECS)
        log(f"🕐 冷却等待 {backoff_secs}s 后再重试，避免立即被再次判定异常...", "GoogleFX-Video")
        for _ in range(backoff_secs):
            self._check_cancel()
            time.sleep(1)

        from ..config import get_runtime_default_user_id
        from ..utils.account_pool import switch_to_next_account
        current = (get_runtime_default_user_id() or "").strip()
        if current:
            self.tried_accounts.add(current)
        chosen = switch_to_next_account(exclude=self.tried_accounts)
        if chosen:
            self.tried_accounts.add(chosen["user_id"])
            # 透出给 SPARK：换号后本批次的积分记账/后续换号要跟着换目标账号，
            # 前端也需要知道"这一批中途换号了"。
            self._notify(self.chunk_start, "account_switched", {
                "previous": current or None,
                "user_id": chosen["user_id"],
                "name": chosen.get("name") or "",
                "reason": "ip_blocked",
                "retry": self.ip_retry,
            })


def generate_videos_batch_google_fx(reqs: list, on_progress=None, cancel_check=None):
    """
    批量视频生成主流程（SPARK 图生视频的唯一入口）。

    将请求按 VIDEO_CHUNK_SIZE 个一组分批，每批由一个 _ChunkRunner 执行；同一次
    调用里的所有 chunk 固定复用首批进入的 Flow 项目，不能在任务中途重复新建项目：
    项目导航 → （重试轮）认领历史卡片 → 分组上传参考图 → 依次提交（不等待）
    → 并行等待 → 下载并逐个上报。

    on_progress(idx, stage, details): stage ∈ video_start/video_done/video_error；
    video_done 的回调可返回 'rejected'（SPARK 侧锚点校验拒收）触发该槽位重试。
    cancel_check(): 返回 True 时抛 ConnectionError 中止全部任务。

    IP 被封（unusual activity）时递增冷却 + 换号后只重跑未完成任务，最多 MAX_IP_RETRIES
    次；非 IP 失败每 chunk 允许 MAX_GEN_RETRIES_PER_CHUNK 轮有限重试。
    """
    results = []

    # ── 兼容：n8n 端点将整个 VideoBatchRequest 对象传进来 ──
    # generate_videos_batch_google_fx 原本期望 reqs 是 VideoRequest 的 list，
    # 但 _run_with_google_fx_lock 会把 req (VideoBatchRequest) 原样传进来。
    # 这里做一次解包，提取真正的 items 列表。
    if hasattr(reqs, "items") and not isinstance(reqs, list):
        reqs = reqs.items or []

    # 规范化批量生成中每个子任务的模型名
    for r in reqs:
        if hasattr(r, "model"):
            r.model = _normalize_model_name(r.model, is_video=True)

    log(f"🚀 开始批量生成视频，共 {len(reqs)} 段...", "GoogleFX-Video")

    if not reqs:
        return results


    # 基于全批次提示词计算区分性切片（剔除所有段共有的 boilerplate 前缀），
    # 供 tile 归属核对与断点认领使用。单任务 chunk 也能拿到有区分度的切片。
    all_slices = _distinct_slices({i: r.prompt for i, r in enumerate(reqs)})

    # _ChunkRunner 过去把 project_url 存在实例里，但每 5 个视频都会新建一个 runner，
    # 导致 14 段任务在第 1/6/11 段各点一次“新建项目”。换号节拍恰好设成 15 时，
    # 这尤其像是换号触发了重建。项目 URL 必须属于整次批量调用，而不是单个 chunk。
    batch_project_url = None
    for chunk_start in range(0, len(reqs), VIDEO_CHUNK_SIZE):
        chunk = reqs[chunk_start : chunk_start + VIDEO_CHUNK_SIZE]
        log(f"📦 开始处理第 {chunk_start // VIDEO_CHUNK_SIZE + 1} 批视频请求 ({len(chunk)} 个)...", "GoogleFX-Video")
        runner = _ChunkRunner(
            total_reqs=len(reqs),
            chunk_start=chunk_start,
            chunk=chunk,
            all_slices=all_slices,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )
        runner.project_url = batch_project_url
        results.extend(runner.run())
        if runner.project_url:
            batch_project_url = runner.project_url

    successful = [r for r in results if r and isinstance(r, dict) and r.get("status") == "success" and r.get("video_url")]
    if successful:
        try:
            current_uid = account_binding.resolve_account(
                fallback=get_runtime_default_user_id()
            )
            if not current_uid:
                log("⚠️ 视频已生成，但无法解析实际 AdsPower 账号，任务数未记录", "GoogleFX-Video")
            else:
                from ..utils.account_pool import AccountPool
                entry = AccountPool().record_task_count(
                    current_uid, video_count=len(successful)
                )
                if entry is None:
                    log(
                        f"⚠️ 视频已生成，但账号 {current_uid} 不在账号池中，任务数未记录",
                        "GoogleFX-Video",
                    )
        except Exception as _e:
            log(f"⚠️ 记录账号视频任务数失败: {_e}", "GoogleFX-Video")

    return results


def _generate_videos_batch_google_fx_unlocked(*args, **kwargs):
    return generate_videos_batch_google_fx(*args, **kwargs)
