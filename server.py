import os
import sys
import json
import shutil
import socket
import urllib.request
import urllib.error
import urllib.parse
import base64
import time
import threading
import mimetypes
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Register MIME types to ensure correct browser content-type headers
mimetypes.add_type('image/webp', '.webp')
mimetypes.add_type('video/mp4', '.mp4')

# Import everything from sub-modules to preserve namespace compatibility
from server_common import *
from frame_generator import *
from video_generator import *
from prompt_pipeline import *

# Explicitly import private functions that are not imported by wildcard '*'
from server_common import _get_project_dir, _safe_project_name, _LOG_PATH
from prompt_pipeline import _parse_prompt_slots, _chat, _aux_model
from frame_generator import (
    _image_generation_model_for_request,
    _image_quality_to_label,
    _generate_text_image,
    _run_async_image_generation,
    _run_async_image_edit
)

# Per-IP sliding-window rate-limit state used by rate_ok() (managed mode only)
import collections
_RATE_BUCKET = collections.defaultdict(list)
_RATE_LOCK = threading.Lock()



def background_worker(task_id, config, dimensions):
    t = get_or_create_task(task_id, dimensions)
    start_time = time.time()
    # 本轮运行的身份令牌：重试复用 task_id 时 prepare_task_for_run 会换新
    # cancel_event。取消已被 /api/compose-cancel 立即终态化，旧线程可能还卡在
    # 几十秒的 LLM 请求里没死透——它凭令牌发现自己已被接管后必须静默退出，
    # 绝不能把过期事件/结果写进新一轮运行的记录。
    run_token = t["cancel_event"]

    def run_cancelled():
        return run_token.is_set() or t.get("cancel_event") is not run_token

    def on_progress(stage, details):
        with ACTIVE_TASKS_LOCK:
            if run_cancelled():
                raise GenerationCancelled("Generation cancelled by user")
            if stage == 'cancel_check':
                # 纯取消探针（重试循环的 attempt 边界调用），不产生事件
                return False
            if stage == 'text_chunk':
                t["events"].append(('text_chunk', details))
            else:
                t["events"].append(('progress', {'stage': stage, 'details': details}))

        # 实时推送必须和历史重放（上面 events 里存的）同形：旧写法实时只发
        # 裸 details（没有 stage 字段），前端 updateProgressUI 靠 stage 分派，
        # 导致实时阶段文本/步骤条从不更新、只有断线重连重放时才对
        if stage == 'text_chunk':
            notify_listeners(task_id, 'text_chunk', details)
        else:
            notify_listeners(task_id, 'progress', {'stage': stage, 'details': details})

    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        if isinstance(config, dict):
            config['_skipped_checks'] = 0
        content = call_llm(config, dimensions, on_progress=on_progress)
        result = parse_sections(content)

        if run_cancelled():
            raise GenerationCancelled("Generation cancelled by user")
            
        # skill 直出模式：文本阶段无审查，audit_md 只是直出模式的说明文案；
        # 一致性审查在帧渲染后对真实画面进行（pipeline_orchestrator）。
        result['repair_md'] = result.get('audit_md') or 'PASS — 工序与场景一致性检查通过，未发现违规，提示词未改动。'
        result['skipped_checks_count'] = config.get('_skipped_checks', 0) if isinstance(config, dict) else 0
        
        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage
            
        duration = time.time() - start_time
        images, videos = _parse_prompt_slots(result['prompt_block'])
        result['timings'] = {
            'total_duration_seconds': round(duration, 2)
        }
        result['image_count'] = len(images)
        result['video_count'] = len(videos)
        # 结构化槽位契约：前端优先消费，避免前后端双实现解析漂移（帧配对错位事故前提）
        from prompt_pipeline import prompt_slots_list
        result['prompt_slots'] = prompt_slots_list(result['prompt_block'])

        # 发布用双语标题行（TikTok 英文标题+tags / 国内社媒中文标题+话题），失败不阻塞出单
        from prompt_pipeline import generate_social_titles
        theme = dimensions.get('theme', '') if isinstance(dimensions, dict) else ''
        social = generate_social_titles(config, result.get('title') or '', theme)
        if social.get('tiktok'):
            result['social_title_en'] = social['tiktok']
        if social.get('cn'):
            result['social_title_cn'] = social['cn']

        with ACTIVE_TASKS_LOCK:
            # 终态化必须在锁内复核取消/换代：/api/compose-cancel 可能刚把记录切
            # 到 cancelled，completed 不得把它翻回来
            if run_cancelled():
                raise GenerationCancelled("Generation cancelled by user")
            t["status"] = "completed"
            t["result"] = result
            # Trim streaming text_chunk events to optimize tasks.json size
            t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
            t["events"].append(('result', result))

        notify_listeners(task_id, 'result', result)
        save_tasks_to_disk()

    except ConnectionError:
        # GenerationCancelled 走这里（其子类）。/api/compose-cancel 通常已抢先把
        # 记录终态化并广播过（status 不再是 running），重试也可能已换代接管
        # （cancel_event 被换新）——两种情况都不再重复写事件/广播。
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t.get("cancel_event") is run_token and t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了生成任务"
                t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
                t["events"].append(('error', {'message': '用户取消了生成任务'}))
                finalize = True
        if finalize:
            notify_listeners(task_id, 'error', {'message': '用户取消了生成任务'})
            save_tasks_to_disk()
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] background task {task_id} failed: {e}")
        error_msg = None
        with ACTIVE_TASKS_LOCK:
            if t.get("cancel_event") is run_token and t["status"] == "running":
                if run_token.is_set():
                    # 取消引发的连带异常（连接被掐等）应记为取消，而不是失败
                    t["status"] = "cancelled"
                    error_msg = "用户取消了生成任务"
                else:
                    t["status"] = "failed"
                    error_msg = str(e)
                t["error"] = error_msg
                t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
                t["events"].append(('error', {'message': error_msg}))
        if error_msg is not None:
            notify_listeners(task_id, 'error', {'message': error_msg})
            save_tasks_to_disk()


def auto_run_worker(task_id, config, dimensions):
    """Drives the full autonomous pipeline (compose -> render+verify IMAGE 1 -> refine
    packet -> remaining beats -> remaining frames -> videos) as one background task,
    reusing the same ACTIVE_TASKS/SSE plumbing as the three manual stages."""
    t = get_or_create_task(task_id, dimensions)
    set_log_context(task_id)
    log('INFO', 'AUTORUN', "开始自治管线任务")
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if stage == 'review_poll':
                # 监修决策带内通道（与 cancel_check 同款探测，不入事件史）：
                # /api/frame-review 把决策写进任务记录，这里取走并消费掉
                with ACTIVE_TASKS_LOCK:
                    decisions = t.get('review_decisions') or {}
                    return decisions.pop(str((details or {}).get('sequence')), None)
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from pipeline_orchestrator import run_autonomous_pipeline
        # 自治管线的渲染/视频阶段驱动共享 FX 浏览器：全程持有 FX 串行锁
        # （_FX_SERIAL_LOCK 定义在本模块靠后位置，调用时才解析，安全）
        with _FX_SERIAL_LOCK:
            result = run_autonomous_pipeline(config, dimensions, on_progress=progress_cb)

        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'AUTORUN', "自治管线任务完成")
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了生成任务"
            t["events"].append(('error', {'message': '用户取消了生成任务'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了生成任务'})
        log('WARN', 'AUTORUN', "自治管线任务已被用户取消")
    except Exception as e:
        log_exception('AUTORUN')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'AUTORUN', f"自治管线任务失败: {e}")
    finally:
        save_tasks_to_disk()
        set_log_context(None)


def _client_ip(handler):
    for h in ('CF-Connecting-IP', 'X-Forwarded-For'):
        v = handler.headers.get(h)
        if v:
            return v.split(',')[0].strip()
    try:
        return handler.client_address[0]
    except Exception:
        return 'unknown'


def access_ok(handler):
    """True if the request is allowed past the access gate (always true when no code set)."""
    if not ACCESS_CODE:
        return True
    
    # Check X-Access-Code
    x_code = (handler.headers.get('X-Access-Code') or '').strip()
    if x_code == ACCESS_CODE:
        return True
        
    # Check Authorization Bearer
    auth = (handler.headers.get('Authorization') or '').strip()
    if auth.startswith('Bearer '):
        if auth[7:].strip() == ACCESS_CODE:
            return True

    # EventSource 无法携带自定义请求头：只读流式端点（日志流）允许用
    # query 参数携带访问码
    try:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(handler.path).query)
        if (q.get('access_code', [''])[0] or '').strip() == ACCESS_CODE:
            return True
    except Exception:
        pass

    return False


def rate_ok(ip):
    """Per-IP sliding-window rate limit; only enforced in managed mode."""
    if not SERVER_MANAGED:
        return True
    if ip in ('127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1'):
        return True
    now = time.time()
    with _RATE_LOCK:
        bucket = [t for t in _RATE_BUCKET[ip] if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_MAX:
            _RATE_BUCKET[ip] = bucket
            return False
        bucket.append(now)
        _RATE_BUCKET[ip] = bucket
        return True


def generate_frames_worker(task_id, config, title, prompt_block, target_sequences):
    t = get_or_create_task(task_id)
    # 之后本线程里所有 log() 调用自动带上 [task=task_id]，日志面板可以按任务过滤
    # （见 server_common.log 的说明），不用在每一处调用手动传 task_id。
    set_log_context(task_id)
    log('INFO', 'FRAMES', f"开始帧序列任务 {'（整单）' if target_sequences is None else f'（子集 {target_sequences}）'}", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if stage == 'review_poll':
                # 监修决策带内通道（与 cancel_check 同款探测，不入事件史）：
                # /api/frame-review 把决策写进任务记录，这里取走并消费掉
                with ACTIVE_TASKS_LOCK:
                    decisions = t.get('review_decisions') or {}
                    return decisions.pop(str((details or {}).get('sequence')), None)
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        # 上游失败即时广播：每次尝试失败立刻推 SSE 'upstream_retry'，
        # 前端不再在后台重试退避链（最长分钟级）里干等着看"生成中"
        from frame_generator import set_upstream_event_sink, set_cancel_check_sink
        set_upstream_event_sink(lambda ev: progress_cb('upstream_retry', ev))
        # 取消检测线程局部兜底：generate_frame_sequence 此前只在"每帧开始前"调用
        # 一次 cancel_check（progress_cb('cancel_check', ...)），一帧内部自己的
        # HTTP 重试退避链（单次编辑最多 3 外层 × 5 内层 = 15 次尝试，遇上游限流
        # 单是内层退避就可能烧到分钟级）完全不受它约束——用户点「取消」，这一帧
        # 卡着不动，任务不会真正停。注册后 _execute_request_with_retry 的每次
        # （重）试前都会探测这个回调，取消能在下一次尝试前就生效。
        set_cancel_check_sink(lambda: t["cancel_event"].is_set())
        try:
            # google_fx 后端共享同一个 AdsPower 浏览器和 builtins.google_fx_cancelled
            # 全局旗标：必须与视频生成互斥，否则新任务开跑时重置旗标/抢浏览器，
            # 会把另一个在跑的 FX 任务搅乱（跨任务串片事故的同族问题）
            with _fx_serial_lock_for(config):
                if target_sequences is None:
                    # 整单渲染走编排层：分段渲染+检查点现实校准+链尾回望+监修暂停。
                    # 此前一次性端点直调 generate_frame_sequence，这些机制对主界面的
                    # 帧序列按钮完全不生效。单帧/子集重试仍走原直调路径。
                    from pipeline_orchestrator import render_frames_for_task
                    result = render_frames_for_task(
                        config, title, prompt_block, on_progress=progress_cb,
                    )
                else:
                    result = generate_frame_sequence(
                        config, title, prompt_block,
                        on_progress=progress_cb,
                        target_sequences=target_sequences
                    )
        finally:
            set_upstream_event_sink(None)
            set_cancel_check_sink(None)
        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage
            
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'FRAMES', "帧序列任务完成", title=title)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了帧序列生成"
            t["events"].append(('error', {'message': '用户取消了帧序列生成'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了帧序列生成'})
        log('WARN', 'FRAMES', "帧序列任务已被用户取消", title=title)
        try:
            ensure_adspower_on_path()
            from utils.browser import stop_ads_browser
            user_id = config.get('googleFxUserId') or None
            stop_ads_browser(user_id=user_id)
        except Exception:
            pass
    except Exception as e:
        log_exception('FRAMES')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'FRAMES', f"帧序列任务失败: {e}", title=title)
    finally:
        save_tasks_to_disk()
        set_log_context(None)


def render_staged_worker(task_id, config, title, prompt_block):
    """Stages an ALREADY-composed prompt_block through pipeline_orchestrator's
    render/gate/recovery machinery (render IMAGE 1 -> Anchor Acceptance Gate -> render
    the rest -> autonomous recovery passes -> videos), instead of the old
    generate_frames_worker's one-shot full-batch render. Used by /api/render_staged,
    which scripts/generate_frames.py now calls so a skill-driven (agent-composed)
    prompt set also gets staged, gated rendering instead of a blind batch render."""
    t = get_or_create_task(task_id)
    set_log_context(task_id)
    log('INFO', 'STAGED', "开始分步渲染任务", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if stage == 'review_poll':
                # 监修决策带内通道（与 cancel_check 同款探测，不入事件史）：
                # /api/frame-review 把决策写进任务记录，这里取走并消费掉
                with ACTIVE_TASKS_LOCK:
                    decisions = t.get('review_decisions') or {}
                    return decisions.pop(str((details or {}).get('sequence')), None)
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from pipeline_orchestrator import run_staged_frame_rendering
        from frame_generator import set_cancel_check_sink
        # 见 generate_frames_worker 里的同款说明：不注册这个，取消按钮点了也拦不住
        # 一帧内部正卡着的 HTTP 重试退避链。
        set_cancel_check_sink(lambda: t["cancel_event"].is_set())
        try:
            # 分步渲染尾段总会驱动 FX 视频生成：全程持有 FX 串行锁
            with _FX_SERIAL_LOCK:
                result = run_staged_frame_rendering(config, title, prompt_block, on_progress=progress_cb)
        finally:
            set_cancel_check_sink(None)

        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'STAGED', "分步渲染任务完成", title=title)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了分步渲染任务"
            t["events"].append(('error', {'message': '用户取消了分步渲染任务'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了分步渲染任务'})
        log('WARN', 'STAGED', "分步渲染任务已被用户取消", title=title)
    except Exception as e:
        log_exception('STAGED')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'STAGED', f"分步渲染任务失败: {e}", title=title)
    finally:
        save_tasks_to_disk()
        set_log_context(None)


# FX 浏览器串行锁：所有驱动 AdsPower/google_fx 浏览器的任务（帧序列 FX 路径、
# 视频生成、分步渲染、自治管线）共用一把，代替旧的仅视频任务持有的
# _VIDEO_GEN_SERIAL_LOCK。共享的还有 builtins.google_fx_cancelled 旗标——
# 只有互斥后，任务开始时的旗标重置才是安全的。
_FX_SERIAL_LOCK = threading.Lock()


def _fx_serial_lock_for(config):
    """帧序列任务仅在走 google_fx 后端时才需要 FX 串行锁；纯 API 渲染不串行。"""
    import contextlib
    uses_fx = isinstance(config, dict) and config.get('imageBackend') == 'google_fx'
    return _FX_SERIAL_LOCK if uses_fx else contextlib.nullcontext()


def generate_videos_worker(task_id, config, title, prompt_block, target_slots):
    t = get_or_create_task(task_id)
    set_log_context(task_id)
    log('INFO', 'VIDEOS', f"开始视频任务{f'（子集 {target_slots}）' if target_slots else '（整单）'}", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if stage == 'review_poll':
                # 监修决策带内通道（与 cancel_check 同款探测，不入事件史）：
                # /api/frame-review 把决策写进任务记录，这里取走并消费掉
                with ACTIVE_TASKS_LOCK:
                    decisions = t.get('review_decisions') or {}
                    return decisions.pop(str((details or {}).get('sequence')), None)
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        progress_cb('queue', {'message': '视频生成请求已加入队列，正在等待排队生成...'})

        with _FX_SERIAL_LOCK:
            result = generate_video_sequence(
                config, title, prompt_block,
                on_progress=progress_cb,
                target_slots=target_slots
            )
            
            usage = stop_and_get_accounting()
            if usage:
                result['token_usage'] = usage
            
            # Check if all expected videos are successfully generated
            _, expected_videos = _parse_prompt_slots(prompt_block)
            expected_slots = list(expected_videos.keys())
            manifest_videos = result.get('videos', [])
            manifest_video_slots = {v['slot']: v for v in manifest_videos}
            
            has_failures = False
            for slot in expected_slots:
                if slot not in manifest_video_slots:
                    has_failures = True
                    break
                # 'skipped_cut'（声明式硬切槽位）是预期缺失，不算失败——合并门禁
                # （merge_project_videos）对它同样按预期缺失处理
                if manifest_video_slots[slot].get('status') not in ('success', 'skipped_cut'):
                    has_failures = True
                    break

            if has_failures:
                progress_cb('merge_skip', {'message': '检测到存在生成失败或缺失的视频片段，已跳过自动合并视频。'})
            else:
                # Try to automatically merge videos
                try:
                    progress_cb('merge_start', {'message': '正在自动合并并加速视频...'})
                    project_dir = _get_project_dir(title)
                    merged_info = merge_project_videos(project_dir)
                    if merged_info:
                        result['merged_video'] = merged_info
                        # Also update manifest file on disk (locked read-modify-write)
                        try:
                            with manifest_lock(project_dir):
                                mdata = read_manifest(project_dir)
                                if mdata is not None:
                                    mdata['merged_video'] = merged_info
                                    write_manifest(project_dir, mdata)
                        except Exception as e:
                            log('WARN', 'VIDEOS', f"更新 manifest.json 的 merged_video 字段失败: {e}", title=title)
                    progress_cb('merge_done', {'merged_video': merged_info})
                except Exception as merge_err:
                    log('ERROR', 'VIDEOS', f"自动合并视频失败: {merge_err}", title=title)
                    progress_cb('merge_error', {'message': f'自动合并视频失败: {str(merge_err)}'})

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'VIDEOS', "视频任务完成", title=title)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了视频生成"
            t["events"].append(('error', {'message': '用户取消了视频生成'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了视频生成'})
        log('WARN', 'VIDEOS', "视频任务已被用户取消", title=title)
        try:
            ensure_adspower_on_path()
            from utils.browser import stop_ads_browser
            user_id = config.get('googleFxUserId') or None
            stop_ads_browser(user_id=user_id)
        except Exception:
            pass
    except Exception as e:
        log_exception('VIDEOS')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'VIDEOS', f"视频任务失败: {e}", title=title)
    finally:
        save_tasks_to_disk()
        set_log_context(None)


def generate_cover_worker(task_id, config, parent_task_id, title, theme, prompt_block):
    t = get_or_create_task(task_id)
    set_log_context(task_id)
    log('INFO', 'COVER', "开始封面任务", title=title)
    try:
        # Generate a catchy, viral English title/hook for TikTok US cover
        title_prompt = (
            f"You are a TikTok US marketing expert. Convert this Chinese project title '{title}' (Theme: '{theme}') "
            f"into a catchy, viral, high-CTR English title or hook suitable for a TikTok US video cover. "
            f"Requirements:\n"
            f"1. Must be extremely short (3 to 6 words).\n"
            f"2. Use bold, dramatic action-oriented or curiosity-inducing keywords (e.g., 'I BUILT A SECRET BUNKER!', 'EPIC TREEHOUSE BUILD', 'MODERN CABIN REBUILD', 'WEIRD FLOATING GLASS ROOM').\n"
            f"3. Only output the English title itself. Do not include quotes, explanation, translation labels, or any prefix.\n\n"
            f"Chinese Title: {title}"
        )
        
        english_title = _chat(config, "You are a viral TikTok US marketing expert specializing in high-CTR hooks.", title_prompt, temperature=0.7, max_tokens=30, model=_aux_model(config))
        english_title = english_title.strip().strip('"').strip("'").strip()

        # 发布用双语标题行：新激发已在 compose 阶段生成，这里只给缺字段的旧创意补齐
        social = {}
        try:
            has_social = False
            if parent_task_id:
                with ACTIVE_TASKS_LOCK:
                    parent = ACTIVE_TASKS.get(str(parent_task_id)) or ACTIVE_TASKS.get(parent_task_id)
                    parent_result = (parent or {}).get("result") or {}
                    has_social = bool(parent_result.get("social_title_en") and parent_result.get("social_title_cn"))
            if not has_social:
                from prompt_pipeline import generate_social_titles
                social = generate_social_titles(config, title, theme)
        except Exception:
            social = {}

        aspect_ratio = config.get('imageAspectRatio') or '9:16'
        
        # Extract first (Before) and last (After) image prompts from the build process
        def _extract_before_after_prompts(block):
            images, _ = _parse_prompt_slots(block)
            if images:
                keys = sorted(images)
                before_item = images[keys[0]]
                after_item = images[keys[-1]]
                before_body = before_item['body'] if isinstance(before_item, dict) else before_item
                after_body = after_item['body'] if isinstance(after_item, dict) else after_item
                return before_body, after_body
            return None, None
        
        before_prompt, after_prompt = _extract_before_after_prompts(prompt_block)
        if before_prompt and after_prompt:
            visual_context = (
                f"- BEFORE STATE (Left half of image): A highly realistic, detailed view showing: {before_prompt}\n"
                f"- AFTER STATE (Right half of image): A breathtaking, gorgeous, high-end finished view showing: {after_prompt}"
            )
        else:
            visual_context = (
                f"- BEFORE STATE (Left half of image): The messy, ruined, dusty, or empty initial construction phase of the {theme}.\n"
                f"- AFTER STATE (Right half of image): The spectacular, beautiful, fully completed, and pristine finished phase of the {theme}."
            )
        
        image_prompt = (
            f"A highly professional, viral TikTok video cover, aspect ratio {aspect_ratio}, ultra-high resolution, stunning aesthetic, optimized for TikTok US feed to maximize CTR.\n\n"
            f"Video Theme: {theme}\n"
            f"English Hook: {english_title}\n\n"
            f"COMPOSITION STYLE: Side-by-side before-and-after comparison with a soft, blended transition in the middle.\n"
            f"- The image must feature a soft, seamless gradient transition in the center blending the left and right halves together, without any harsh dividing line, split line, or frame border.\n"
            f"- Left side shows the BEFORE state. It must look messy, realistic, under construction, or in ruins.\n"
            f"- Right side shows the AFTER state. It must look completed, pristine, high-end, and visually stunning.\n"
            f"- Ensure the camera angle, perspective, and architectural lines are perfectly aligned between the left and right halves to create a single, continuous, and satisfyingly blended transformation.\n\n"
            f"VISUAL CONTEXT:\n{visual_context}\n\n"
            f"VISUAL STYLE & QUALITY:\n"
            f"- The visual style must be highly photorealistic, with crisp textures, natural cinematic lighting, and realistic shadows. The image must look like a real professional photograph, NOT a cartoon, illustration, 3D CGI render, or painting.\n"
            f"- Create a dramatic contrast in lighting: the left half (Before) can have colder, dimmer, or more rugged lighting, while the right half (After) must feature gorgeous, warm, inviting, and premium glow and highlights to create a 'wow' factor.\n\n"
            f"TEXT OVERLAY:\n"
            f"- Overlay the bold English hook text: '{english_title}' across the upper or middle part of the image.\n"
            f"- The text MUST use a clean, solid, ultra-bold, high-contrast sans-serif font (similar to Montserrat Bold or Impact) in solid white or solid bright yellow with a subtle, clean black outline or drop shadow to ensure extreme legibility and professional graphic design look.\n"
            f"- Keep the font style clean, simple, and standard. Avoid messy, irregular, hand-drawn, graffiti, or low-contrast text styles. The layout must be perfectly centered and balanced."
        )
        
        out_dir = os.path.join(OUTPUT_ROOT, 'covers')
        os.makedirs(out_dir, exist_ok=True)
        filename = f"{_safe_project_name(title)}_cover_{int(time.time() * 1000)}.webp"
        target_path = os.path.join(out_dir, filename)
        
        if sys.stdout:
            print(f"[DEBUG] Generating cover image via _generate_text_image to {target_path}...")

        try:
            _generate_text_image(config, image_prompt, target_path)
        except QuotaExhaustedError:
            fallback_model = config.get('imageEditFallbackModel') or config.get('fallbackImageModel')
            if not fallback_model:
                raise
            if sys.stdout:
                print(f"[COVER] Quota exhausted on primary model. Retrying cover with fallback model: {fallback_model}")
            fallback_config = dict(config)
            fallback_config['imageModel'] = fallback_model
            _generate_text_image(fallback_config, image_prompt, target_path)
        
        rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        image_content = '/' + rel_path
        
        # Persist cover details in parent task result
        if parent_task_id:
            with ACTIVE_TASKS_LOCK:
                task = ACTIVE_TASKS.get(str(parent_task_id)) or ACTIVE_TASKS.get(parent_task_id)
                if task and task.get("result"):
                    if "covers" not in task["result"] or not isinstance(task["result"]["covers"], list):
                        task["result"]["covers"] = []
                    cover_rel_url = image_content
                    if cover_rel_url not in task["result"]["covers"]:
                        task["result"]["covers"].append(cover_rel_url)
                    task["result"]["english_title"] = english_title
                    if social.get('tiktok') and not task["result"].get("social_title_en"):
                        task["result"]["social_title_en"] = social['tiktok']
                    if social.get('cn') and not task["result"].get("social_title_cn"):
                        task["result"]["social_title_cn"] = social['cn']

        result_data = {
            'content': image_content,
            'english_title': english_title
        }
        if social.get('tiktok'):
            result_data['social_title_en'] = social['tiktok']
        if social.get('cn'):
            result_data['social_title_cn'] = social['cn']
        
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result_data
            t["events"].append(('result', result_data))

        notify_listeners(task_id, 'result', result_data)
        log('INFO', 'COVER', "封面任务完成", title=title)

    except Exception as e:
        log_exception('COVER')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'COVER', f"封面任务失败: {e}", title=title)
    finally:
        save_tasks_to_disk()
        set_log_context(None)


class DualStackHTTPServer(ThreadingMixIn, HTTPServer):
    # ThreadingMixIn so a long-running /api/compose call (the skill can take 60-180s)
    # does not block static file serving or library reads in the same browser session.
    address_family = socket.AF_INET6
    # Windows 上 SO_REUSEADDR 允许第二个实例静默绑定同一端口——这正是文档记录过的
    # “重复实例越积越多”的根源；显式 False + SO_EXCLUSIVEADDRUSE 让第二次启动立刻报错。
    allow_reuse_address = (sys.platform != 'win32')
    daemon_threads = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class IPv4HTTPServer(ThreadingMixIn, HTTPServer):
    """IPv4 回退：本机禁用 IPv6 时 DualStackHTTPServer 无法启动。"""
    address_family = socket.AF_INET
    allow_reuse_address = (sys.platform != 'win32')
    daemon_threads = True

    def server_bind(self):
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def check_video_prompt_semantically(config, video_prompt, vlm_reason):
    """
    Use LLM to determine if the video transition prompt has semantic or logical issues
    and is responsible for the VLM QA failure.
    """
    system_prompt = (
        "You are a video transition auditor. Your job is to check if the VIDEO transition prompt "
        "has any logical errors, contradictions, or is responsible for a VLM QA failure.\n"
        "You will be given the VIDEO prompt and the VLM QA failure reason (in Chinese).\n\n"
        "Determine if the video prompt itself has issues (e.g., describes impossible actions, has camera pan/zoom/tilt when it must be static, or is contradictory).\n"
        "Response format:\n"
        "- If the video prompt has issues, respond EXACTLY in this format: FAIL: <reason in Chinese>\n"
        "- If the video prompt is completely fine and doesn't need to be corrected, respond EXACTLY with: PASS"
    )
    user_prompt = f"VIDEO prompt:\n{video_prompt}\n\nVLM Failure Reason:\n{vlm_reason}"
    from prompt_pipeline import _chat, _aux_model
    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.2, timeout=45, model=_aux_model(config))
        return response.strip()
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_video_prompt_semantically failed: {e}")
        return "PASS"


def fix_video_prompt_with_vlm_feedback(config, original_video_prompt, vlm_reason, validation_errors=None):
    """
    Use LLM (auxModel) to generate a corrected video prompt based on the VLM QA audit failure reason and any validation errors.
    """
    system_prompt = (
        "You are an expert prompt engineering assistant. Your job is to modify a video transition prompt "
        "to fix specific errors detected by a visual quality auditor (VLM) or rule validation errors.\n"
        "You will be given the original video transition prompt, the VLM failure reason (in Chinese), "
        "and optionally any rule validation errors.\n\n"
        "Please provide a corrected video transition prompt that:\n"
        "- Corrects the action or pacing so it matches the image changes or fixes the VLM failure.\n"
        "- Ensures there are no forbidden camera movement instructions (like zoom, pan, tilt) if the camera must remain static.\n"
        "- Fixes any coordinates, pacing, or rule validation errors listed.\n"
        "- Keeps the original action intent, style, and duration constraints intact.\n"
        "- Do NOT output any explanations, markdown code fences, or headers. Output ONLY the raw corrected prompt text in English."
    )
    user_prompt = (
        f"Original VIDEO prompt:\n{original_video_prompt}\n\n"
        f"VLM Audit Failure Reason:\n{vlm_reason}\n\n"
    )
    if validation_errors:
        user_prompt += f"Rule Validation Errors:\n" + "\n".join(f"- {e}" for e in validation_errors) + "\n\n"
    user_prompt += "Please output the corrected VIDEO prompt in English."

    from prompt_pipeline import _chat, _aux_model, _strip_markdown_fences_only
    try:
        response = _chat(
            config, system_prompt, user_prompt,
            temperature=0.3, timeout=60, model=_aux_model(config)
        )
        return _strip_markdown_fences_only(response).strip()
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] fix_video_prompt_with_vlm_feedback failed: {e}")
        return original_video_prompt


class SparkRequestHandler(SimpleHTTPRequestHandler):
    timeout = 60

    def log_message(self, format, *args):
        if sys.stdout:
            try:
                sys.stdout.write(f"LOG: {format % args}\n")
            except Exception:
                pass

    def send_response(self, code, message=None):
        # 记住本次响应状态码，供 end_headers 判断是否要压制 404 的隐式缓存
        self._spark_status_code = code
        super().send_response(code, message)

    def end_headers(self):
        # Enable CORS for local development flexibility
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        # Never let browsers / Cloudflare serve stale front-end assets. This is what
        # caused "multiple UI forms" (old cached HTML/CSS shown alongside the new one).
        # no-store（而非 no-cache）：no-cache 只是要求每次都带条件请求去服务端"问一下"，
        # 但那次协商行为本身是在响应第一次被缓存时就已经和浏览器约定好的——如果某个
        # URL 是在这条 no-cache 规则生效之前就被缓存下来的（更早的部署/没有这个头的
        # 历史版本），浏览器压根不会再发请求去验证，永远沿用旧缓存，导致"必须开无痕
        # 才能看到最新改动"。no-store 直接禁止缓存这个响应本身，不存在"沿用旧协商结果"
        # 这个坑，每次加载都是从磁盘整份重新拉取。
        p = (self.path or '').split('?')[0]
        if p.endswith(('.html', '.css', '.js')) or p.endswith('/'):
            self.send_header('Cache-Control', 'no-store')
        # 帧/视频文件名是确定性的（img_001.webp 等），生成前请求会先拿到 404——
        # 404 在 RFC 7231 里是浏览器默认可启发式缓存的状态码，Safari 对此尤其
        # 激进。不加这个头，浏览器可能把"还没生成"的 404 缓存下来，等真正生成
        # 完成后同一个 URL 仍被浏览器判定为命中缓存直接返回旧的 404，画面上
        # 表现为"后端明明已经生成成功，图片却一直显示裂图"（macOS/Safari 上
        # 尤其容易复现）。只压制错误响应的缓存，不影响正常图片的缓存效率。
        if getattr(self, '_spark_status_code', 200) >= 400:
            self.send_header('Cache-Control', 'no-store')
        elif p.startswith('/outputs/'):
            # 反过来：200 成功响应也会被浏览器启发式缓存（无 Cache-Control 时
            # 按 Last-Modified 估算新鲜期，期间根本不会再发请求）。但 outputs/
            # 下的帧/视频文件名是确定性且可被覆写的——重试单帧、"不用的帧"被
            # 画廊删除后又在同一序号位置重新生成，都是原地覆盖同一个 URL。
            # 浏览器缓存的旧字节和新生成的内容"打架"：manifest 已经指向新帧，
            # 图片却仍在显示删除前/重试前的旧画面。no-cache（而非 no-store）
            # 让浏览器每次都带 If-Modified-Since 去问一声——文件没变就是一个
            # 廉价的 304（不传输正文，不重新下载），真的被覆盖了才会传新内容，
            # 两头都不吃亏：既不会显示旧帧，也不会退化回"每次全量重下"。
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _gate(self, with_rate=False):
        """访问码（及可选频控）门禁。未设置访问码时恒放行。返回 False 时已回写响应。"""
        if not access_ok(self):
            self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
            return False
        if with_rate and not rate_ok(_client_ip(self)):
            self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
            return False
        return True

    def _read_json_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length else b'{}'
        # Browsers always send UTF-8; fall back to the Windows codepage for resilience
        # against non-UTF-8 clients (e.g. curl launched from a GBK console).
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('gbk', errors='replace')
        return json.loads(text)

    def _open_sse_stream(self):
        """Start a Server-Sent-Events response and return (send_event, stop).

        A background heartbeat writes an SSE comment (": keepalive") whenever the
        stream has been silent for >10s. The LLM pipeline can block for up to ~240s
        with no output during the non-streaming fallback in _chat(); without a
        heartbeat the browser drops that idle connection and reports it as a
        `TypeError: network error` (shown to the user as "合成失败：network error").
        The heartbeat keeps the connection warm across those gaps.

        send_event(type, data) writes one event; on a broken pipe it raises
        ConnectionError("Client disconnected"). Call stop() in a finally block.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        # Tell any intermediary (Cloudflare tunnel / nginx) not to buffer the stream.
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        lock = threading.Lock()
        last_send = [time.time()]
        stop_evt = threading.Event()

        def _raw_write(text):
            # Serialize writes: the worker thread emits events while the heartbeat
            # thread emits pings — both touch the same wfile.
            with lock:
                self.wfile.write(text.encode('utf-8'))
                self.wfile.flush()
                last_send[0] = time.time()

        def send_event(event_type, data):
            try:
                _raw_write(f"event: {event_type}\ndata: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False)}\n\n")
            except Exception as e:
                stop_evt.set()
                if sys.stdout:
                    print(f"[DEBUG] SSE client disconnected ({self.path}): {e}")
                raise ConnectionError("Client disconnected")

        def _heartbeat():
            while not stop_evt.wait(5):
                if time.time() - last_send[0] >= 10:
                    try:
                        _raw_write(": keepalive\n\n")
                    except Exception:
                        # 客户端已断开：必须 set stop_evt 释放阻塞在
                        # stop_evt.wait() 的处理线程，否则它会一直滞留
                        stop_evt.set()
                        break

        threading.Thread(target=_heartbeat, daemon=True).start()
        return send_event, stop_evt

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/api/library':
            if not self._gate():
                return
            # 读失败必须报 500 而不是静默返回 []：前端拿到空列表后一次保存
            # 就会把整个创意库覆盖成空——这是真实存在过的整库清零路径
            with LIBRARY_LOCK:
                if not os.path.exists(DB_FILE):
                    self._send_json([])
                    return
                try:
                    with open(DB_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    if sys.stdout:
                        print(f"Error reading {DB_FILE}: {e}")
                    self._send_json({'error': f'创意库文件读取失败: {e}'}, status=500)
                    return
            self._send_json(data)
        elif path == '/api/get_manifest':
            if not self._gate():
                return
            title = query.get('title', [''])[0]
            if not title:
                self._send_json({'error': 'Missing title'}, status=400)
                return
            project_dir = _get_project_dir(title)
            try:
                sync_project_manifest_with_disk(project_dir)
            except Exception as e:
                if sys.stdout:
                    print(f"Error syncing manifest for {title}: {e}")
            manifest_path = os.path.join(project_dir, 'manifest.json')
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._send_json(data)
                except Exception as e:
                    self._send_json({'error': str(e)}, status=500)
            else:
                self._send_json({'error': 'Not found'}, status=404)
        elif path == '/api/logs/stream':
            # 服务日志包含内部路径/任务详情，必须过门禁（EventSource 用 ?access_code= 传码）
            if not self._gate():
                return
            send_event = None
            stop_evt = None
            try:
                send_event, stop_evt = self._open_sse_stream()
                
                # Send the last 100 lines first.
                # 二进制模式读取整份文件再统一 decode，而不是文本模式 seek()——
                # 文本模式的 seek() 只应该用 tell() 拿到的"不透明游标"，喂一个由
                # os.path.getsize() 算出来的裸字节偏移量是未定义行为：偏移量一旦
                # 落在某个多字节 UTF-8 字符中间，errors='replace' 会悄悄垫上
                # "�" 之类的替换符，前端看到的就是一串乱字符（这不是 print() 端
                # 输出了乱码，是这里读的时候自己读岔了）。
                if os.path.exists(_LOG_PATH):
                    try:
                        with open(_LOG_PATH, 'rb') as f:
                            raw = f.read()
                        last_lines = raw.decode('utf-8', errors='replace').splitlines(keepends=True)[-100:]
                        send_event('history', {'lines': last_lines})
                    except Exception as e:
                        print(f"Error reading log history: {e}")

                # Start tailing
                last_size = os.path.getsize(_LOG_PATH) if os.path.exists(_LOG_PATH) else 0

                while not stop_evt.is_set():
                    time.sleep(0.5)
                    if not os.path.exists(_LOG_PATH):
                        continue
                    curr_size = os.path.getsize(_LOG_PATH)
                    if curr_size > last_size:
                        try:
                            with open(_LOG_PATH, 'rb') as f:
                                f.seek(last_size)
                                new_bytes = f.read()
                            if new_bytes:
                                send_event('log', {'text': new_bytes.decode('utf-8', errors='replace')})
                        except Exception as e:
                            print(f"Error reading new logs: {e}")
                        last_size = curr_size
                    elif curr_size < last_size:
                        # Log file was rotated or cleared
                        last_size = curr_size
            except ConnectionError:
                pass
            except Exception as e:
                if send_event:
                    try:
                        send_event('error', {'message': str(e)})
                    except Exception:
                        pass
            finally:
                if stop_evt:
                    stop_evt.set()
        elif path == '/api/tasks':
            if not self._gate():
                return
            cleanup_old_tasks()
            limit = 100
            if 'limit' in query:
                try:
                    limit = int(query['limit'][0])
                except ValueError:
                    pass
            with ACTIVE_TASKS_LOCK:
                res = []
                for tid, t in ACTIVE_TASKS.items():
                    # 列表接口只回瘦身摘要：完整 result（提示词全文/封面/帧清单）
                    # 会被 2.5s 一次的角标轮询反复整包下载
                    full_result = t.get("result")
                    result_summary = None
                    if isinstance(full_result, dict):
                        result_summary = {}
                        if full_result.get("timings"):
                            result_summary["timings"] = full_result["timings"]
                        if full_result.get("token_usage"):
                            result_summary["token_usage"] = full_result["token_usage"]
                    # 运行中任务附带精简事件流（去掉 text_chunk 大块文本），
                    # 任务抽屉靠它渲染阶段与进度——之前根本没回传，进度永远 0%
                    events_summary = None
                    if t["status"] == "running":
                        non_chunks = [evt for evt in t["events"] if evt[0] != 'text_chunk']
                        events_summary = non_chunks[-50:]
                        if len(non_chunks) != len(t["events"]):
                            events_summary = events_summary + [('text_chunk', '')]
                    entry = {
                        "id": t["id"],
                        "status": t["status"],
                        "dimensions": t["dimensions"],
                        "result": result_summary,
                        "error": t["error"],
                        "last_active": t["last_active"]
                    }
                    if events_summary is not None:
                        entry["events"] = events_summary
                    res.append(entry)
            # 按活跃时间排序：任务 ID 是「毫秒时间戳」和「frames_<uuid>」混排，
            # 旧的字符串排序会把两类 ID 搅在一起
            res.sort(key=lambda x: x.get("last_active") or 0, reverse=True)
            total_count = len(res)
            if limit and limit > 0:
                sliced = res[:limit]
            else:
                sliced = res
            self._send_json({
                "tasks": sliced,
                "total_count": total_count
            })
        elif path == '/api/compose-stream':
            if not self._gate():
                return
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self.send_error(400, "Missing task_id")
                return
            
            task = ACTIVE_TASKS.get(task_id)
            if not task:
                self.send_error(404, "Task not found")
                return
                
            send_event, stop_evt = self._open_sse_stream()

            # 追赶式重放 + 原子注册：事件追加和监听器注册都在 ACTIVE_TASKS_LOCK
            # 下进行，所以「锁内确认无未发事件后立即注册」能确保零丢失。
            # 旧写法在快照历史和注册监听器之间有窗口——任务恰好在这期间完成
            # 的话，终态事件永远送不到，前端只能对着心跳干等。
            sent = 0
            is_terminal = False
            while True:
                with ACTIVE_TASKS_LOCK:
                    pending = list(task["events"][sent:])
                    if not pending:
                        is_terminal = task["status"] in ("completed", "failed", "cancelled")
                        if not is_terminal:
                            task["listeners"].add((send_event, stop_evt))
                        break
                for event_type, event_data in pending:
                    try:
                        send_event(event_type, event_data)
                    except Exception:
                        stop_evt.set()
                        return
                    sent += 1

            # Task already finished — history fully replayed, close the stream
            if is_terminal:
                stop_evt.set()
                return

            # Block until stream is stopped
            stop_evt.wait()

            # Clean up listener
            with ACTIVE_TASKS_LOCK:
                task["listeners"].discard((send_event, stop_evt))
                
        elif path == '/api/compose-status':
            if not self._gate():
                return
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self.send_error(400, "Missing task_id")
                return
            
            task = ACTIVE_TASKS.get(task_id)
            if not task:
                self._send_json({"status": "not_found"})
                return
                
            with ACTIVE_TASKS_LOCK:
                task["last_active"] = time.time()
                res = {
                    "id": task["id"],
                    "status": task["status"],
                    "result": task["result"],
                    "error": task["error"],
                    "dimensions": task["dimensions"]
                }
            self._send_json(res)
        elif path == '/api/mode':
            # Public: tells the frontend whether to hide the API settings and prompt for an access code.
            self._send_json({
                'server_managed': SERVER_MANAGED,
                'needs_access_code': bool(ACCESS_CODE),
            })
        elif path == '/api/image/task/status':
            if not self._gate():
                return
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self._send_json({'error': 'Missing task_id'}, status=400)
                return

            # 长轮询（毫秒级同步）：带 wait+since 时挂起，直到任务指纹（状态|阶段）
            # 变化或超时才返回——上游报错被 worker 写进 stage 的瞬间（~50ms 采样）
            # 就能推到前端。不带 wait 则保持旧的即查即回行为。ThreadingHTTPServer
            # 每请求一线程，本地单用户挂几条长轮询无压力。
            try:
                wait_s = float(query.get('wait', ['0'])[0] or 0)
            except ValueError:
                wait_s = 0.0
            wait_s = max(0.0, min(wait_s, 25.0))
            since = query.get('since', [''])[0]

            def _snapshot():
                with IMAGE_TASKS_LOCK:
                    t = IMAGE_TASKS.get(task_id)
                    return dict(t) if t else None

            def _fp(t):
                return f"{t.get('status')}|{t.get('stage', '')}|{t.get('stage_at', '')}"

            task = _snapshot()
            if wait_s > 0 and task and since and task.get('status') == 'pending' and _fp(task) == since:
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    time.sleep(0.05)
                    task = _snapshot()
                    if not task or task.get('status') != 'pending' or _fp(task) != since:
                        break
            if not task:
                self._send_json({'status': 'not_found'})
            else:
                task['fingerprint'] = _fp(task)
                self._send_json(task)
        elif path == '/api/cache-info':
            if not self._gate():
                return
            try:
                packet_cache_size = 0
                cache_keys_count = 0
                with PACKET_CACHE_LOCK:
                    if os.path.exists(CACHE_PATH):
                        packet_cache_size = os.path.getsize(CACHE_PATH)
                        try:
                            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                                cache_data = json.load(f)
                                if isinstance(cache_data, dict):
                                    cache_keys_count = len(cache_data)
                        except Exception:
                            pass
                
                self._send_json({
                    'packet_cache_size': packet_cache_size,
                    'packet_cache_keys': cache_keys_count
                })
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)
        elif path == '/api/gallery':
            # 画廊：扫描 outputs/ 下全部历史媒体（封面/帧序列/视频/图像工坊），
            # 并标注引用关系（封面 in_use / 项目组 orphan）。引用收集失败时降级
            # 为无标注扫描，画廊本体不受影响。
            if not self._gate():
                return
            try:
                try:
                    refs = gallery_collect_references()
                except Exception as e:
                    if sys.stdout:
                        print(f"[GALLERY] reference collection failed: {e}")
                    refs = None
                self._send_json(scan_gallery(refs=refs))
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)
        elif path == '/api/ledger':
            # 创意台账：topic_ledger.json 全量返回，读失败必须报 500 而不是静默
            # 降级为 []（同 /api/library 的整库清零教训，见 read_ledger 注释）
            if not self._gate():
                return
            data = read_ledger()
            if data is None:
                self._send_json({'error': '创意台账文件读取失败'}, status=500)
                return
            self._send_json(data)
        elif path == '/api/trend-refs':
            # 联网参考案例库(trend_refs.json)全量返回,新→旧;读失败必须报 500
            # 而不是静默降级为 [](同 /api/ledger 的整库清零教训)
            if not self._gate():
                return
            refs = load_trend_refs()
            if refs is None:
                self._send_json({'error': '联网参考案例库文件读取失败'}, status=500)
                return
            self._send_json({'status': 'ok', 'refs': list(reversed(refs))})
        elif path.startswith('/api/contents/generations/tasks/'):
            try:
                task_id = path.split('/')[-1]
                auth_header = self.headers.get('Authorization') or ''
                target_url = f'https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}'
                req = urllib.request.Request(
                    target_url,
                    headers={
                        'Authorization': auth_header,
                        'Content-Type': 'application/json',
                    },
                    method='GET',
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=60) as resp:
                    resp_data = json.loads(resp.read().decode('utf-8'))
                self._send_json(resp_data)
            except urllib.error.HTTPError as e:
                detail = ''
                try:
                    detail = e.read().decode('utf-8')[:500]
                except Exception: pass
                self._send_json({'error': f'Volcengine API HTTP {e.code}', 'detail': detail}, status=e.code)
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)
        else:
            if not self._static_path_allowed(path):
                self.send_error(404, "Not found")
                return
            super().do_GET()

    # ── 静态兜底路由封锁 ─────────────────────────────────────────────
    # 服务绑定在所有网卡且 CORS 为 *：兜底静态路由绝不允许吐出密钥配置、
    # 日志、任务数据或服务端源码。登录页所需的 html/css/js 保持公开。
    _BLOCKED_BASENAMES = (
        'server_config', 'server.log', 'server.pid', 'library.json',
        'packet_cache.json', 'process_brief_cache.json', 'tasks.json',
        'requirements.txt',
    )
    _BLOCKED_PREFIXES = (
        '/tasks/', '/.git', '/.claude/', '/.gemini/', '/scratch/',
        '/tests/', '/.pytest_cache/', '/.agents/',
        '/prompt_pipeline/',  # Python 源码包目录:.py 内容已被后缀拦,此项兜住目录列表
    )
    _BLOCKED_SUFFIXES = ('.py', '.pyc', '.bat', '.pid', '.log')

    def _static_path_allowed(self, path):
        low = path.split('?', 1)[0].lower()
        if any(low.endswith(sfx) for sfx in self._BLOCKED_SUFFIXES):
            return False
        if any(low.startswith(pfx) for pfx in self._BLOCKED_PREFIXES):
            return False
        base = low.rsplit('/', 1)[-1]
        if any(base.startswith(b) for b in self._BLOCKED_BASENAMES):
            return False
        return True

    def do_HEAD(self):
        # 图像工坊用 HEAD 探测 /outputs 资源存活；封锁规则必须与 GET 一致
        from urllib.parse import urlparse
        if not self._static_path_allowed(urlparse(self.path).path):
            self.send_error(404, "Not found")
            return
        super().do_HEAD()

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/library':
            if not self._gate():
                return
            try:
                data = self._read_json_body()
                # 加锁 + 原子替换：直接 open('w') 在写一半崩溃时会留下半截 JSON
                with LIBRARY_LOCK:
                    # 2026-07-12 整库清零事故（library.json 被某个 savedIdeas 为空的客户端
                    # 会话全量覆盖成 []）后的双保险：
                    # 1) 空列表覆盖非空库一律拒绝——删除单条走 /api/library/delete_item 后
                    #    的全量回写永远至少剩 0..N-1 条，只有状态错乱的客户端才会 POST []；
                    # 2) 任何成功覆盖前把现有非空库轮换到 library.json.bak，误写可回滚一版。
                    existing_count = 0
                    if os.path.exists(DB_FILE):
                        try:
                            with open(DB_FILE, 'r', encoding='utf-8') as f:
                                _old = json.load(f)
                            existing_count = len(_old) if isinstance(_old, list) else 1
                        except Exception:
                            existing_count = 1  # 读不出来按“非空”保守处理
                    incoming_count = len(data) if isinstance(data, list) else 0
                    if incoming_count == 0 and existing_count > 0:
                        if sys.stdout:
                            print(f"[LIBRARY GUARD] 拒绝空列表覆盖非空创意库（现有 {existing_count} 条）")
                        self._send_json({
                            'status': 'rejected',
                            'message': f'拒绝将非空创意库（{existing_count} 条）覆盖为空：客户端库状态疑似错乱，'
                                       f'请刷新页面重新加载库后再操作；如确要清空请逐条删除。'
                        }, status=409)
                        return
                    if existing_count > 0:
                        try:
                            shutil.copyfile(DB_FILE, DB_FILE + '.bak')
                        except Exception as _bak_err:
                            if sys.stdout:
                                print(f"[LIBRARY GUARD] 备份 {DB_FILE}.bak 失败（继续写入）: {_bak_err}")
                    write_json_atomic(DB_FILE, data)
                self._send_json({'status': 'success'})
            except Exception as e:
                if sys.stdout:
                    print(f"Error writing {DB_FILE}: {e}")
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/ledger':
            # 创意台账全量保存：状态/表现打分/备注编辑与候选入库都走"整表回写"，
            # 与 /api/library 同一套约定（客户端始终持有完整数组）
            if not self._gate():
                return
            try:
                data = self._read_json_body()
                ok, message = write_ledger(data)
                if not ok:
                    self._send_json({'status': 'rejected', 'message': message}, status=409)
                    return
                self._send_json({'status': 'success'})
            except Exception as e:
                if sys.stdout:
                    print(f"Error writing {LEDGER_FILE}: {e}")
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/ledger/delete':
            # 台账批量删除：按 id 列表删（全选删空也走这条），不经过 /api/ledger
            # 整表回写的空列表防护——显式 id 列表本身就是确认过的意图
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                ids = body.get('ids')
                if not isinstance(ids, list) or not ids:
                    self._send_json({'status': 'error', 'message': '缺少要删除的 ids'}, status=400)
                    return
                result = delete_ledger_entries(ids)
                if result['remaining'] is None:
                    self._send_json({'status': 'error', 'message': '创意台账文件读取失败'}, status=500)
                    return
                self._send_json({'status': 'ok', 'deleted': result['deleted']})
            except Exception as e:
                if sys.stdout:
                    print(f"Error deleting from {LEDGER_FILE}: {e}")
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/trend-refs/search':
            # 「搜一批新参考」：绕过 6 小时缓存强制联网重搜+自定义网址重摘要,
            # 沉淀进案例库后全量返回(新→旧)。同步调用,最长约 1~2 分钟。
            if not self._gate(with_rate=True):
                return
            try:
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                new_refs = refresh_trend_refs(config)
                refs = load_trend_refs()
                if refs is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库文件读取失败'}, status=500)
                    return
                self._send_json({
                    'status': 'ok',
                    # 本批搜到的条目 id(文本与旧条目相同则 id 相同,前端据此算真正新增数)
                    'added': [r['id'] for r in new_refs],
                    'refs': list(reversed(refs)),
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/trend-refs/delete':
            # 案例库按 id 批量删除(显式 id 列表即确认过的意图,同 /api/ledger/delete)
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                ids = body.get('ids')
                if not isinstance(ids, list) or not ids:
                    self._send_json({'status': 'error', 'message': '缺少要删除的 ids'}, status=400)
                    return
                result = delete_trend_refs(ids)
                if result['remaining'] is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库文件读取失败'}, status=500)
                    return
                self._send_json({'status': 'ok', 'deleted': result['deleted']})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/ping':
            try:
                body = self._read_json_body()
                ok = ping_proxy(effective_config(body.get('config', {})))
                self._send_json({'online': bool(ok)})
            except Exception as e:
                self._send_json({'online': False, 'message': str(e)})

        elif path == '/api/vlm_qa':
            if not self._gate(with_rate=True):
                return
            try:
                body = self._read_json_body()
                config_req = effective_config(body.get('config', {}))
                img_i_path = body.get('img_i_path')
                img_ip1_path = body.get('img_ip1_path')
                video_prompt = body.get('video_prompt')
                is_bridge = bool(body.get('is_bridge', False))

                # If img_i_path is empty, it means we are checking the first frame (no previous frame).
                # In this case, we skip the check and directly return success.
                if not img_i_path:
                    self._send_json({
                        'status': 'ok',
                        'vlm_pass': True,
                        'vlm_reason': 'First frame skipped'
                    })
                    return

                if not img_ip1_path or not video_prompt:
                    self._send_json({'status': 'error', 'message': 'Missing parameters: img_ip1_path and video_prompt are required'}, status=400)
                    return

                if not os.path.exists(img_i_path):
                    self._send_json({'status': 'error', 'message': f'img_i_path does not exist: {img_i_path}'}, status=400)
                    return

                if not os.path.exists(img_ip1_path):
                    self._send_json({'status': 'error', 'message': f'img_ip1_path does not exist: {img_ip1_path}'}, status=400)
                    return

                from prompt_pipeline import run_vlm_qa_check
                vlm_pass, vlm_reason = run_vlm_qa_check(config_req, img_i_path, img_ip1_path, video_prompt, is_bridge=is_bridge)
                self._send_json({
                    'status': 'ok',
                    'vlm_pass': bool(vlm_pass),
                    'vlm_reason': vlm_reason
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/generate_image_with_vlm':
            if not self._gate(with_rate=True):
                return
            try:
                import requests
                body = self._read_json_body()
                config_req = effective_config(body.get('config', {}))
                prompt = body.get('prompt')
                reference_image = body.get('reference_image')
                ratio = body.get('ratio', '9:16')
                model = body.get('model', 'Nano Banana 2')
                output_path = body.get('output_path')
                video_prompt = body.get('video_prompt')
                chain_index = int(body.get('chain_index', 1))
                is_bridge = bool(body.get('is_bridge', False))
                base_url = body.get('base_url', 'http://127.0.0.1:8000')

                if reference_image and not os.path.exists(reference_image):
                    self._send_json({'status': 'error', 'message': f'reference_image does not exist: {reference_image}'}, status=400)
                    return

                if not prompt or not output_path:
                    self._send_json({'status': 'error', 'message': 'Missing parameters: prompt and output_path are required'}, status=400)
                    return

                current_prompt = prompt
                last_generated_path = None
                vlm_pass = True
                vlm_reason = None
                video_prompt_corrected = False

                for attempt in range(3): # 1 initial + 2 retries = 3 total attempts
                    # Prepare call to AdsPower AI service
                    payload = {
                        "prompts": [current_prompt],
                        "ratio": ratio,
                        "model": model,
                        "output_path": output_path
                    }
                    if reference_image:
                        payload["images"] = [reference_image]

                    log_msg = f"[VLM QA Retry Flow] Generating image frame {chain_index} (attempt {attempt + 1}/3)..."
                    if sys.stdout:
                        print(log_msg)

                    gen_url = f"{base_url.rstrip('/')}/generate_images_batch"
                    headers = {"Content-Type": "application/json"}
                    
                    response = requests.post(gen_url, json=payload, headers=headers, timeout=600)
                    
                    if not response.ok:
                        raise RuntimeError(f"AdsPower generation failed: HTTP {response.status_code} - {response.text}")
                    
                    res_data = response.json()
                    img_path = None
                    for k in ['generated_image_path', 'generated_image_url', 'image_url', 'output_path', 'local_path', 'path', 'url']:
                        if isinstance(res_data.get(k), str) and res_data[k].strip():
                            img_path = res_data[k].strip()
                            break
                    if not img_path and isinstance(res_data.get('image_urls'), list) and res_data['image_urls']:
                        img_path = res_data['image_urls'][0]

                    if not img_path or not os.path.exists(img_path):
                        raise RuntimeError(f"Generated image path not found or does not exist: {img_path}")

                    last_generated_path = img_path

                    # Perform VLM Check if it's not the first frame and we have a reference image and video prompt
                    if chain_index > 1 and reference_image and video_prompt:
                        from prompt_pipeline import run_vlm_qa_check
                        vlm_pass, vlm_reason = run_vlm_qa_check(config_req, reference_image, img_path, video_prompt, is_bridge=is_bridge)
                        if vlm_pass:
                            if sys.stdout:
                                print(f"[VLM QA Retry Flow] Frame {chain_index} passed VLM check on attempt {attempt + 1}!")
                            break
                        else:
                            if attempt < 2:
                                if sys.stdout:
                                    print(f"[VLM QA Retry Flow] Frame {chain_index} failed VLM check on attempt {attempt + 1}: {vlm_reason}.")
                                
                                # Run Quality Check on Video Prompt
                                from prompt_pipeline import (
                                    check_grid_coordinates,
                                    check_nlvtr_violations,
                                    check_video_opening,
                                    check_out_and_in,
                                    check_transition_shortcuts,
                                    check_pacing_control,
                                    check_camera_contradictions
                                )
                                video_errs = []
                                video_errs.extend(check_grid_coordinates(video_prompt))
                                video_errs.extend(check_nlvtr_violations(video_prompt))
                                video_errs.extend(check_video_opening(chain_index - 1, video_prompt))
                                video_errs.extend(check_out_and_in(video_prompt, is_bridge))
                                video_errs.extend(check_transition_shortcuts(video_prompt))
                                video_errs.extend(check_pacing_control(video_prompt, is_bridge))
                                # 桥接段 TBCP 无条件禁 pan/tilt（reward 拍不会是桥接，无误伤面）
                                video_errs.extend(check_camera_contradictions(video_prompt, is_bridge, ban_pan_tilt=is_bridge))
                                
                                vid_word_count = len(video_prompt.split())
                                if vid_word_count > 180:
                                    video_errs.append(f"VIDEO prompt word count ({vid_word_count}) exceeds limit of 180 words")
                                
                                semantic_res = check_video_prompt_semantically(config_req, video_prompt, vlm_reason)
                                video_prompt_failed = bool(video_errs) or semantic_res.startswith("FAIL")
                                
                                if video_prompt_failed:
                                    combined_errs = list(video_errs)
                                    if semantic_res.startswith("FAIL"):
                                        combined_errs.append(semantic_res)
                                    if sys.stdout:
                                        print(f"[VLM QA Retry Flow] Frame {chain_index} video prompt failed quality check. Errors: {combined_errs}")
                                    
                                    new_video_prompt = fix_video_prompt_with_vlm_feedback(config_req, video_prompt, vlm_reason, combined_errs)
                                    if sys.stdout:
                                        print(f"[VLM QA Retry Flow] Rewritten video prompt: {new_video_prompt}")
                                    video_prompt = new_video_prompt
                                    video_prompt_corrected = True
                                else:
                                    if sys.stdout:
                                        print(f"[VLM QA Retry Flow] Frame {chain_index} video prompt passed quality check.")

                                # Correct the image prompt as before
                                from prompt_pipeline import fix_image_prompt_with_vlm_feedback
                                current_prompt = fix_image_prompt_with_vlm_feedback(config_req, current_prompt, vlm_reason)
                                if sys.stdout:
                                    print(f"[VLM QA Retry Flow] Rewritten image prompt: {current_prompt}")
                            else:
                                if sys.stdout:
                                    print(f"[VLM QA Retry Flow] Frame {chain_index} failed VLM check and retries exhausted.")
                    else:
                        vlm_pass = True
                        break

                res_payload = {
                    'generated_image_path': last_generated_path,
                    'vlm_pass': bool(vlm_pass)
                }
                if video_prompt_corrected:
                    res_payload['corrected_video_prompt'] = video_prompt

                if vlm_pass:
                    res_payload['status'] = 'ok'
                    self._send_json(res_payload)
                else:
                    res_payload['status'] = 'failed'
                    res_payload['message'] = f"VLM QA failed: {vlm_reason}"
                    self._send_json(res_payload)

            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/clear-cache':
            if not self._gate():
                return
            try:
                with PACKET_CACHE_LOCK:
                    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                        json.dump({}, f, ensure_ascii=False, indent=2)
                self._send_json({'status': 'success', 'message': '系统缓存清理成功'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/ideate':
            try:
                if not access_ok(self):
                    self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'status': 'error', 'message': '请求频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                count = body.get('count', 8)
                # 基础场景主题选择器已移除；theme/theme_label 仅为旧客户端兼容保留。
                # trend_ref_ids=用户在联网参考案例库勾选的条目 id：非空时本批灵感
                # 以选中案例为首要创意来源(不再自动联网搜索)
                theme = body.get('theme')
                theme_label = body.get('theme_label')
                trend_ref_ids = body.get('trend_ref_ids') or []

                result = run_ideate(config, count, theme=theme, theme_label=theme_label,
                                    trend_ref_ids=trend_ref_ids)
                self._send_json({
                    'status': 'ok',
                    'ideas': result['ideas'],
                    # 本批注入过灵感 prompt 的联网参考(搜索词摘要/自定义网址摘要),
                    # 前端展示成可折叠面板,让用户能看到"搜到了什么"
                    'trend_refs': result.get('trend_refs') or [],
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/compose':
            try:
                if not access_ok(self):
                    self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                dimensions = body.get('dimensions', {})
                config = effective_config(body.get('config'))
                task_id = body.get('task_id')
                if not task_id:
                    task_id = str(int(time.time() * 1000))

                # Start background thread
                cleanup_old_tasks()
                # 重试沿用旧 task_id：终态记录被原地重置复用（覆盖被重试的记录），
                # 已在运行则不再起第二个 worker，前端拿到 ok 后重新挂流即可
                _, already_running = prepare_task_for_run(task_id, dimensions)
                if already_running:
                    self._send_json({'status': 'ok', 'task_id': task_id, 'already_running': True})
                    return

                threading.Thread(
                    target=background_worker,
                    args=(task_id, config, dimensions),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/auto_run':
            try:
                if not access_ok(self):
                    self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                dimensions = body.get('dimensions', {})
                config = effective_config(body.get('config'))
                # 自治管线全程驱动 FX 浏览器：把浏览器编号带进 dimensions，
                # /api/compose-cancel 取消时据此关闭正确的 AdsPower 窗口
                dimensions['userId'] = config.get('googleFxUserId') or None
                task_id = body.get('task_id')
                if not task_id:
                    task_id = f"auto_{int(time.time() * 1000)}"

                # Start background thread
                cleanup_old_tasks()
                # 同 /api/compose：重试复用 task_id 时原地覆盖旧记录，运行中防重复提交
                _, already_running = prepare_task_for_run(task_id, dimensions)
                if already_running:
                    self._send_json({'status': 'ok', 'task_id': task_id, 'already_running': True})
                    return

                threading.Thread(
                    target=auto_run_worker,
                    args=(task_id, config, dimensions),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/frame-review':
            # 监修模式决策回传：{task_id, sequence, decision: 'adopt'|'rerender'}。
            # 决策写进任务记录，worker 的 progress_cb 对 'review_poll' 探测取走。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                decision = body.get('decision')
                if decision not in ('adopt', 'rerender'):
                    self._send_json({'error': "decision 必须是 'adopt' 或 'rerender'"}, status=400)
                    return
                with ACTIVE_TASKS_LOCK:
                    t = ACTIVE_TASKS.get(task_id)
                    if not t:
                        self._send_json({'error': '任务不存在或已结束'}, status=404)
                        return
                    t.setdefault('review_decisions', {})[str(body.get('sequence'))] = decision
                self._send_json({'ok': True})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)
            return

        elif path == '/api/compose-cancel':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id and task_id in ACTIVE_TASKS:
                    log('INFO', 'CANCEL', "收到取消请求", task_id=task_id)
                    ACTIVE_TASKS[task_id]["cancel_event"].set()

                    # 只有可能驱动 FX 浏览器的任务才触发全局 FX 取消与浏览器停止：
                    # 旧写法无条件设 builtins.google_fx_cancelled，取消一个纯 LLM
                    # 合成任务会把另一个在跑的帧/视频 FX 任务一并打断
                    dimensions = ACTIVE_TASKS[task_id].get("dimensions") or {}
                    id_prefix = str(task_id).split('_', 1)[0]
                    is_fx_capable = (
                        id_prefix in ('frames', 'videos', 'staged', 'auto')
                        or dimensions.get('type') in ('frames', 'videos', 'staged', 'auto_run')
                    )
                    if not is_fx_capable:
                        # 纯 LLM 合成任务立即终态化：worker 可能正卡在一个 90-240 秒
                        # 的 LLM 请求里，等它自己感知取消会让前端"正在取消"挂几分钟。
                        # 这里直接把记录切到 cancelled 并广播，后台线程随后凭 run
                        # 令牌发现记录已被终态化/接管，静默退出（见 background_worker）。
                        finalized = False
                        with ACTIVE_TASKS_LOCK:
                            t = ACTIVE_TASKS.get(task_id)
                            if t and t["status"] == "running":
                                t["status"] = "cancelled"
                                t["error"] = "用户取消了生成任务"
                                t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
                                t["events"].append(('error', {'message': '用户取消了生成任务'}))
                                finalized = True
                        if finalized:
                            notify_listeners(task_id, 'error', {'message': '用户取消了生成任务'})
                            save_tasks_to_disk()
                    if is_fx_capable:
                        import builtins
                        builtins.google_fx_cancelled = True

                        # Stop AdsPower browser to ensure it's freed and stopped
                        try:
                            ensure_adspower_on_path()
                            from utils.browser import stop_ads_browser
                            user_id = dimensions.get("userId")
                            stop_ads_browser(user_id=user_id)
                        except Exception as browser_err:
                            log('WARN', 'CANCEL', f"关闭 AdsPower 浏览器失败: {browser_err}", task_id=task_id)
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/tasks/delete':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id:
                    title = None
                    with ACTIVE_TASKS_LOCK:
                        task = ACTIVE_TASKS.get(task_id)
                        if task:
                            # If it's running, cancel it first
                            task["cancel_event"].set()
                            title = (task.get("result") or {}).get("title")
                            del ACTIVE_TASKS[task_id]
                    if title:
                        delete_idea_output_files(title)
                    save_tasks_to_disk()
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/library/delete_item':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                title = body.get('title', '')
                covers = body.get('covers') or []
                deleted = delete_idea_output_files(title, covers) if title else {"project_dir": None, "covers": []}
                self._send_json({'status': 'ok', 'deleted': deleted})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/gallery/delete':
            # 画廊删除：按路径删 outputs/ 内媒体文件（白名单校验在 gallery_delete_files 里），
            # 项目内文件删完后重同步该项目 manifest，保证帧/视频列表与磁盘一致
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                paths = body.get('paths')
                if not isinstance(paths, list) or not paths:
                    self._send_json({'status': 'error', 'message': '缺少要删除的文件列表 paths'}, status=400)
                    return
                result = gallery_delete_files(paths)
                for pdir in result.pop('affected_project_dirs', []):
                    try:
                        sync_project_manifest_with_disk(pdir)
                    except Exception as e:
                        if sys.stdout:
                            print(f"[GALLERY] Manifest resync failed for {pdir}: {e}")
                self._send_json({'status': 'ok', **result})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/image/task/cancel':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if not task_id:
                    self._send_json({'status': 'error', 'message': 'Missing task_id'}, status=400)
                    return
                with IMAGE_TASKS_LOCK:
                    t = IMAGE_TASKS.get(task_id)
                    if t and t.get('status') == 'pending':
                        IMAGE_TASKS[task_id] = {'status': 'cancelled', 'result': None, 'error': '用户取消了任务'}
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/tasks/clear':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                status_group = body.get('status_group')
                with ACTIVE_TASKS_LOCK:
                    to_delete = []
                    for tid, t in ACTIVE_TASKS.items():
                        if status_group == "completed" and t["status"] == "completed":
                            to_delete.append(tid)
                        elif status_group == "failed_cancelled" and t["status"] in ("failed", "cancelled"):
                            to_delete.append(tid)
                    for tid in to_delete:
                        ACTIVE_TASKS[tid]["cancel_event"].set()
                        del ACTIVE_TASKS[tid]
                save_tasks_to_disk()
                self._send_json({'status': 'ok', 'count': len(to_delete)})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/generate_frames':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                title = body.get('title', '')
                prompt_block = body.get('prompt_block', '')
                target_sequences = body.get('target_sequences')

                import uuid
                task_id = f"frames_{uuid.uuid4().hex}"

                # Register in ACTIVE_TASKS
                cleanup_old_tasks()
                # userId：本次使用的 AdsPower 浏览器编号，供 /api/compose-cancel 取消时
                # 关闭正确的窗口（仅 google_fx 后端相关，api 后端留 None 无影响）
                get_or_create_task(task_id, {"type": "frames", "theme": title, "userId": config.get('googleFxUserId') or None})

                threading.Thread(
                    target=generate_frames_worker,
                    args=(task_id, config, title, prompt_block, target_sequences),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/render_staged':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                title = body.get('title', '')
                prompt_block = body.get('prompt_block', '')

                import uuid
                task_id = f"staged_{uuid.uuid4().hex}"

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "staged_render", "theme": title, "userId": config.get('googleFxUserId') or None})

                threading.Thread(
                    target=render_staged_worker,
                    args=(task_id, config, title, prompt_block),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/render_anchor':
            try:
                if not access_ok(self):
                    self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                title = body.get('title', '')
                prompt = body.get('prompt', '')
                sequence = int(body.get('sequence', 1))
                meta = body.get('meta', '')
                if not title or not prompt:
                    self._send_json({'status': 'error', 'message': 'title 和 prompt 均为必填'}, status=400)
                    return

                # Synchronous by design: a conversational agent calls this mid-turn and
                # needs the verdict back directly, not a task_id to poll. The server is
                # ThreadingMixIn, so blocking here does not stall other requests.
                from prompt_pipeline import start_accounting, stop_and_get_accounting
                from pipeline_orchestrator import render_and_gate_single_frame
                start_accounting()
                gate = render_and_gate_single_frame(config, title, sequence, prompt, meta=meta)
                usage = stop_and_get_accounting()

                response = {
                    'status': 'ok',
                    'gate_status': gate['status'],
                    'reason': gate['reason'],
                    'prompt': gate['prompt'],
                    'image_url': '/' + os.path.relpath(gate['image_path'], os.path.dirname(os.path.abspath(__file__))).replace('\\', '/'),
                    'project_dir': gate['project_dir'],
                }
                if usage:
                    response['token_usage'] = usage
                self._send_json(response)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/generate_videos':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                title = body.get('title', '')
                prompt_block = body.get('prompt_block', '')
                target_slots = body.get('target_slots')

                import uuid
                task_id = f"videos_{uuid.uuid4().hex}"

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "videos", "theme": title, "userId": config.get('googleFxUserId') or None})

                threading.Thread(
                    target=generate_videos_worker,
                    args=(task_id, config, title, prompt_block, target_slots),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/merge_videos':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                if not title:
                    self._send_json({'error': '缺少项目标题 (title)'}, status=400)
                    return
                
                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return
                
                # Run the merge. force/allow_partial=True 时用占位帧填充缺口（强制合并）。
                force = bool(body.get('force') or body.get('allow_partial'))
                try:
                    merged_info = merge_project_videos(project_dir, allow_partial=force)
                except PartialMergeBlocked as blocked:
                    self._send_json({
                        'status': 'blocked',
                        'missing': blocked.missing,
                        'mismatched': blocked.mismatched,
                        'message': str(blocked),
                    }, status=409)
                    return
                if not merged_info:
                    self._send_json({'error': '合并失败：未找到任何成功的视频片段'}, status=400)
                    return
                
                # Update manifest.json on disk (locked read-modify-write)
                with manifest_lock(project_dir):
                    mdata = read_manifest(project_dir) or {}
                    mdata['merged_video'] = merged_info
                    write_manifest(project_dir, mdata)

                self._send_json({'status': 'ok', 'merged_video': merged_info})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/generate_cover':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                title = body.get('title', '')
                theme = body.get('theme', '')
                prompt_block = body.get('prompt_block', '')
                parent_task_id = body.get('id') or body.get('task_id')

                import uuid
                task_id = f"cover_{uuid.uuid4().hex}"

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "cover", "theme": theme})

                threading.Thread(
                    target=generate_cover_worker,
                    args=(task_id, config, parent_task_id, title, theme, prompt_block),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)

        elif path == '/api/image/generations':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                model = body.get('model') or config.get('imageModel') or 'gemini-3.1-flash-image'
                if 'nano-banana-2' in model:
                    model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
                
                base_url, api_key = resolve_gateway(model, config)
                prompt = body.get('prompt', '')
                size = body.get('size') or body.get('aspect_ratio') or '1:1'
                quality = _image_quality_to_label(body.get('image_size') or body.get('quality') or '2K')
                response_format = body.get('response_format') or 'b64_json'

                final_model = _image_generation_model_for_request(model, size, quality)

                payload = {
                    'model': final_model,
                    'prompt': prompt,
                    'size': size,
                    'quality': quality,
                    'response_format': response_format
                }

                import uuid
                task_id = f"img_task_{uuid.uuid4().hex}"

                with IMAGE_TASKS_LOCK:
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None,
                                            'stage': '任务已受理，排队中', 'created_at': time.time()}

                threading.Thread(
                    target=_run_async_image_generation,
                    args=(task_id, base_url, api_key, payload),
                    daemon=True
                ).start()
                
                self._send_json({'task_id': task_id, 'status': 'pending'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)

        elif path == '/api/image/edits':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return

                content_type = self.headers.get('content-type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self._send_json({'error': 'Content-Type must be multipart/form-data'}, status=400)
                    return

                content_length = int(self.headers.get('content-length', 0))
                body_bytes = self.rfile.read(content_length)

                # Parse the multipart form data
                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + body_bytes
                from email.parser import BytesParser
                from email.policy import default
                msg = BytesParser(policy=default).parsebytes(msg_bytes)

                fields = {}
                files = []
                client_config_str = "{}"

                for part in msg.walk():
                    if part.get_content_disposition() != 'form-data':
                        continue
                    name = part.get_param('name', header='content-disposition')
                    if not name:
                        continue
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b''
                    if filename is not None:
                        if payload:
                            files.append((name, filename, part.get_content_type(), payload))
                    else:
                        value = payload.decode('utf-8', errors='replace').strip()
                        if name == 'config':
                            client_config_str = value
                        else:
                            fields[name] = value

                # Resolve config
                client_config = {}
                if client_config_str:
                    try:
                        client_config = json.loads(client_config_str)
                    except: pass
                config = effective_config(client_config)
                model = fields.get('model') or config.get('imageModel') or 'gemini-3.1-flash-image'
                if 'nano-banana-2' in model:
                    model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
                base_url, api_key = resolve_gateway(model, config)

                if not files:
                    self._send_json({'error': 'At least one image file is required'}, status=400)
                    return

                # Standard OpenAI edits proxy logic (works for Gemini and GPT alike)
                import uuid
                boundary = f"Boundary-{uuid.uuid4().hex}"
                body_data = bytearray()

                # Add text fields
                for k, v in fields.items():
                    if k == 'model':
                        if 'nano-banana-2' in v:
                            v = v.replace('nano-banana-2', 'gemini-3.1-flash-image')
                        if not v.lower().startswith('gemini'):
                            v = _image_generation_model_for_request(
                                v,
                                fields.get('aspect_ratio') or fields.get('size'),
                                fields.get('image_size') or fields.get('quality')
                            )
                    elif k == 'image_size':
                        v_lower = v.lower()
                        if v_lower in ('hd', '4k'):
                            v = '4K'
                        elif v_lower in ('medium', '2k'):
                            v = '2K'
                        else:
                            v = '1K'
                    elif k == 'aspect_ratio' and v.lower() == 'auto':
                        continue
                    body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
                    body_data.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
                    body_data.extend(f"{v}\r\n".encode('utf-8'))

                # Add default model if not provided
                if 'model' not in fields:
                    model = config.get('imageModel') or 'gemini-3.1-flash-image'
                    if 'nano-banana-2' in model:
                        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
                    if model.lower().startswith('gemini'):
                        model_with_suffix = model
                    else:
                        model_with_suffix = _image_generation_model_for_request(
                            model,
                            fields.get('aspect_ratio') or fields.get('size'),
                            fields.get('image_size') or fields.get('quality')
                        )
                    body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
                    body_data.extend(f'Content-Disposition: form-data; name="model"\r\n\r\n'.encode('utf-8'))
                    body_data.extend(f"{model_with_suffix}\r\n".encode('utf-8'))

                # Add files
                for index, (name, filename, p_content_type, file_data) in enumerate(files):
                    upstream_name = 'image' if index == 0 else 'image[]'
                    body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
                    body_data.extend(f'Content-Disposition: form-data; name="{upstream_name}"; filename="{filename}"\r\n'.encode('utf-8'))
                    body_data.extend(f'Content-Type: {p_content_type}\r\n\r\n'.encode('utf-8'))
                    body_data.extend(file_data)
                    body_data.extend(b"\r\n")

                body_data.extend(f"--{boundary}--\r\n".encode('utf-8'))

                task_id = f"img_task_{uuid.uuid4().hex}"

                with IMAGE_TASKS_LOCK:
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None,
                                            'stage': '任务已受理，排队中', 'created_at': time.time()}

                threading.Thread(
                    target=_run_async_image_edit,
                    args=(task_id, base_url, api_key, bytes(body_data), boundary),
                    daemon=True
                ).start()
                
                self._send_json({'task_id': task_id, 'status': 'pending'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=500)

        elif path == '/v1/chat/completions':
            try:
                if not access_ok(self):
                    self._send_json({'error': 'Unauthorized access'}, status=401)
                    return
                body = self._read_json_body()
                model_name = body.get('model') or ''
                if model_name.startswith('gemini-3.5-flash') and not model_name.endswith('-low'):
                    body['model'] = 'gemini-3.5-flash-low'
                config = effective_config(body.get('config'))
                if 'config' in body:
                    del body['config']
                
                base_url, api_key = resolve_gateway(body.get('model'), config)
                if sys.stdout:
                    print(f"[CHAT PROXY] Proxying request to model {body.get('model')} on {base_url}/chat/completions")
                data = json.dumps(body).encode('utf-8')
                headers = {
                    'Content-Type': 'application/json',
                }
                if api_key:
                    headers['Authorization'] = f'Bearer {api_key}'
                req = urllib.request.Request(
                    f'{base_url}/chat/completions',
                    data=data,
                    headers=headers,
                    method='POST'
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=180) as resp:
                    resp_data = json.loads(resp.read().decode('utf-8'))
                self._send_json(resp_data)
            except urllib.error.HTTPError as e:
                detail = ''
                try:
                    detail = e.read().decode('utf-8')[:500]
                except Exception: pass
                if sys.stdout:
                    print(f"[CHAT PROXY] HTTP Error {e.code}: {detail}")
                self._send_json({'error': f'Gateway API HTTP {e.code}', 'detail': detail}, status=e.code)
            except Exception as e:
                if sys.stdout:
                    print(f"[CHAT PROXY] General Error: {e}")
                self._send_json({'error': str(e)}, status=500)

        elif path == '/api/contents/generations/tasks':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                auth_header = self.headers.get('Authorization') or ''
                if not auth_header and config.get('apiKey'):
                    auth_header = f"Bearer {config.get('apiKey')}"
                
                # Strip config to avoid sending it upstream
                if 'config' in body:
                    del body['config']
                
                target_url = 'https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks'
                data = json.dumps(body).encode('utf-8')
                req = urllib.request.Request(
                    target_url,
                    data=data,
                    headers={
                        'Authorization': auth_header,
                        'Content-Type': 'application/json',
                    },
                    method='POST',
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=60) as resp:
                    resp_data = json.loads(resp.read().decode('utf-8'))
                self._send_json(resp_data)
            except urllib.error.HTTPError as e:
                detail = ''
                try:
                    detail = e.read().decode('utf-8')[:500]
                except Exception: pass
                if sys.stdout:
                    print(f"[ARK PROXY] POST tasks HTTP error {e.code}: {detail}")
                self._send_json({'error': f'Volcengine API HTTP {e.code}', 'detail': detail}, status=e.code)
            except Exception as e:
                if sys.stdout:
                    print(f"[ARK PROXY] POST tasks error: {e}")
                self._send_json({'error': str(e)}, status=500)

        else:
            self.send_response(404)
            self.end_headers()


def sync_project_manifest_with_disk(project_dir):
    """
    Scans the frames/ and videos/ directories in project_dir.
    1. Converts any *.png in frames/ to *.webp, and deletes the *.png.
    2. Ensures every img_*.webp file in frames/ has a corresponding entry in manifest['frames'].
    3. Ensures every video_*.mp4 file in videos/ (or similar) has a corresponding entry in manifest['videos'].
    4. Removes any entries in manifest['frames'] or manifest['videos'] whose files do not exist on disk.
    """
    manifest_path = os.path.join(project_dir, 'manifest.json')
    from datetime import datetime
    if not os.path.exists(manifest_path):
        title = os.path.basename(project_dir)
        manifest = {
            "title": title,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "frames": [],
            "videos": []
        }
    else:
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception:
            return
            
    if not isinstance(manifest, dict):
        return
        
    if 'frames' not in manifest:
        manifest['frames'] = []
    if 'videos' not in manifest:
        manifest['videos'] = []
        
    frames_dir = os.path.join(project_dir, 'frames')
    modified = False
    
    from PIL import Image
    import io
    
    # 1. Scan frames_dir and convert PNG to WebP
    if os.path.exists(frames_dir):
        for fname in os.listdir(frames_dir):
            if fname.lower().endswith('.png') and fname.lower().startswith('img_'):
                png_path = os.path.join(frames_dir, fname)
                webp_name = os.path.splitext(fname)[0] + '.webp'
                webp_path = os.path.join(frames_dir, webp_name)
                try:
                    img = Image.open(png_path)
                    img.save(webp_path, format='WEBP', quality=80)
                    os.remove(png_path)
                    modified = True
                    print(f"[SYNC] Converted orphaned {fname} -> WebP")
                except Exception as e:
                    print(f"[SYNC] Failed to convert orphaned {png_path} to WebP: {e}")
                    
    # 2. Build map of existing webp files on disk
    existing_webp_frames = {}
    if os.path.exists(frames_dir):
        for fname in os.listdir(frames_dir):
            if fname.lower().endswith('.webp') and fname.lower().startswith('img_'):
                try:
                    seq_str = fname.split('_')[1].split('.')[0]
                    seq = int(seq_str)
                    existing_webp_frames[seq] = fname
                except Exception:
                    pass
                    
    def _rel_url_for(path):
        rel = os.path.relpath(path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        return rel, '/' + rel

    def _normalize_media_entry(entry, path):
        rel, url = _rel_url_for(path)
        changed = False
        if entry.get('file') != rel:
            entry['file'] = rel
            changed = True
        if entry.get('url') != url:
            entry['url'] = url
            changed = True
        return changed

    # 3. Synchronize manifest['frames']
    manifest_frames = manifest['frames']
    new_frames = []
    seen_sequences = set()
    
    for frame in manifest_frames:
        seq = frame.get('sequence') or frame.get('slot')
        if seq:
            file_path = frame.get('file')
            if not file_path:
                modified = True
                continue
            if file_path and file_path.lower().endswith('.png'):
                file_path = os.path.splitext(file_path)[0] + '.webp'
                frame['file'] = file_path
                frame['url'] = '/' + file_path.lstrip('/')
                modified = True
                
            full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_path.lstrip('/'))
            if os.path.exists(full_path):
                new_frames.append(frame)
                seen_sequences.add(seq)
            else:
                modified = True
                
    for seq, fname in existing_webp_frames.items():
        if seq not in seen_sequences:
            rel_file_path = os.path.relpath(os.path.join(frames_dir, fname), os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
            new_frame = {
                "slot": seq,
                "sequence": seq,
                "file": rel_file_path,
                "url": '/' + rel_file_path,
                "prompt": "已从磁盘恢复的帧图像",
                "reference": None,
                "model": "gemini-3.1-flash-image",
                "aspect_ratio": manifest.get('aspect_ratio') or "9:16",
                "image_size": manifest.get('image_size') or "2K",
                "retry_count": 0,
                "quality_gate": "pending_manual_review"
            }
            new_frames.append(new_frame)
            modified = True
            print(f"[SYNC] Restored missing frame {seq} to manifest")
            
    manifest_frames_by_seq = {}
    for frame in manifest['frames']:
        seq = frame.get('sequence') or frame.get('slot')
        if seq:
            try:
                manifest_frames_by_seq[int(seq)] = frame
            except Exception:
                pass

    rebuilt_frames = []
    for seq, fname in sorted(existing_webp_frames.items()):
        frame_path = os.path.join(frames_dir, fname)
        frame = manifest_frames_by_seq.get(seq, {}).copy()
        frame['slot'] = frame.get('slot') or seq
        frame['sequence'] = frame.get('sequence') or seq
        if _normalize_media_entry(frame, frame_path):
            modified = True
        frame.setdefault("prompt", "Recovered frame image from disk")
        frame.setdefault("reference", None)
        frame.setdefault("model", "gemini-3.1-flash-image")
        frame.setdefault("aspect_ratio", manifest.get('aspect_ratio') or "9:16")
        frame.setdefault("image_size", manifest.get('image_size') or "2K")
        frame.setdefault("retry_count", 0)
        frame.setdefault("quality_gate", "pending_manual_review")
        rebuilt_frames.append(frame)
    if rebuilt_frames:
        new_frames = rebuilt_frames

    new_frames.sort(key=lambda x: x.get('sequence', 0))
    if len(manifest['frames']) != len(new_frames) or modified:
        manifest['frames'] = new_frames
        modified = True
        
    videos_dir = os.path.join(project_dir, 'videos')
    existing_videos = {}
    if os.path.exists(videos_dir):
        for fname in os.listdir(videos_dir):
            if fname.lower().endswith('.mp4') and fname.lower().startswith('vid_'):
                try:
                    slot = int(fname.split('_')[1].split('.')[0])
                    existing_videos[slot] = fname
                except Exception:
                    pass

    # Load library to lookup the prompt block for this project if needed
    project_title = os.path.basename(project_dir)
    parsed_videos = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                lib_data = json.load(f)
            for item in lib_data:
                if item.get('title') == project_title:
                    p_block = item.get('prompt_block')
                    if p_block:
                        _, parsed_videos = _parse_prompt_slots(p_block)
                    break
        except Exception as e:
            print(f"[SYNC] Error loading prompt block from {DB_FILE}: {e}")

    manifest_videos = manifest['videos']
    new_videos = []
    seen_slots = set()
    for video in manifest_videos:
        slot = video.get('slot') or video.get('sequence')
        try:
            slot = int(slot)
        except Exception:
            slot = None
        if slot in existing_videos:
            video_path = os.path.join(videos_dir, existing_videos[slot])
            if _normalize_media_entry(video, video_path):
                modified = True
            if not video.get('prompt') or video.get('prompt') == "Recovered video from disk":
                if slot in parsed_videos:
                    video['prompt'] = parsed_videos[slot].get('body') or "Recovered video from disk"
                    modified = True
                    print(f"[SYNC] Updated existing video slot {slot} prompt from library")
            new_videos.append(video)
            if slot:
                seen_slots.add(slot)
        else:
            vpath = video.get('file')
            if not vpath:
                continue
            full_vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), vpath.lstrip('/'))
            if os.path.exists(full_vpath):
                new_videos.append(video)
                if slot:
                    seen_slots.add(slot)
            else:
                modified = True

    # Restore missing videos from disk
    for slot, fname in sorted(existing_videos.items()):
        if slot not in seen_slots:
            video_path = os.path.join(videos_dir, fname)
            rel_file_path = os.path.relpath(video_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
            prompt = "Recovered video from disk"
            if slot in parsed_videos:
                prompt = parsed_videos[slot].get('body') or "Recovered video from disk"
            new_video = {
                "slot": slot,
                "sequence": len(new_videos) + 1,
                "file": rel_file_path,
                "url": '/' + rel_file_path,
                "prompt": prompt,
                "model": "Veo 3.1 - Lite [Lower Priority]",
                "status": "success"
            }
            new_videos.append(new_video)
            seen_slots.add(slot)
            modified = True
            print(f"[SYNC] Restored missing video slot {slot} to manifest: {prompt[:30]}...")

    new_videos.sort(key=lambda x: x.get('slot', 0))
    for idx, video in enumerate(new_videos):
        video['sequence'] = idx + 1

    if len(manifest['videos']) != len(new_videos) or modified:
        manifest['videos'] = new_videos
        modified = True
        
    # Sync merged_video
    if 'merged_video' in manifest:
        merged_info = manifest['merged_video']
        if merged_info and merged_info.get('file'):
            full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), merged_info['file'].lstrip('/'))
            if not os.path.exists(full_path):
                del manifest['merged_video']
                modified = True
        else:
            del manifest['merged_video']
            modified = True

    if modified:
        write_manifest(project_dir, manifest)
        print(f"[SYNC] Saved synchronized manifest for {project_dir}")


def run_migrations():
    """
    1. Synchronizes all project manifests in outputs/ with files on disk (converting PNG to WebP and restoring missing frames).
    2. Converts base64 image in restored_history.json to local WebP and updates references.
    """
    if sys.stdout:
        print("[MIGRATION] Checking for assets to migrate to WebP...")
    
    outputs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
    if os.path.exists(outputs_dir):
        for name in os.listdir(outputs_dir):
            project_dir = os.path.join(outputs_dir, name)
            if os.path.isdir(project_dir):
                try:
                    sync_project_manifest_with_disk(project_dir)
                except Exception as e:
                    print(f"[MIGRATION] Failed to sync project {name}: {e}")

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # Record our PID as a diagnostic breadcrumb. 注意：run.bat/stop.bat 实际是按
    # 端口号（Get-NetTCPConnection）找进程杀的，并不读取这个文件——真正防止
    # 重复实例的是 DualStackHTTPServer 的 SO_EXCLUSIVEADDRUSE 独占绑定。
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.pid'), 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    run_migrations()
    load_tasks_from_disk()
    # sys.stdout is None under pythonw (no console); guard prints so the server
    # also runs cleanly headless, while still logging when launched with python.exe.
    if sys.stdout:
        print(f"Starting SPARK server on port {PORT}...")
        print(f"Persisted library file will be saved at: {os.path.abspath(DB_FILE)}")
        print(f"Skill contract source: {SKILL_DIR}")
        # 技能契约文件缺失时旧行为是静默降级为空契约，生成质量悄悄劣化——
        # 启动时显式列出缺失文件，便于第一时间发现
        _skill_expected = [
            os.path.join(SKILL_DIR, 'SKILL.md'),
            os.path.join(SKILL_DIR, 'references', 'prompt-templates.md'),
        ]
        _missing = [p for p in _skill_expected if not os.path.exists(p)]
        if _missing:
            print("[WARN] 技能契约文件缺失，提示词合成将降级运行：")
            for p in _missing:
                print(f"[WARN]   缺失: {p}")
    server_address = ('', PORT)
    try:
        httpd = DualStackHTTPServer(server_address, SparkRequestHandler)
    except OSError as e:
        # 本机未启用 IPv6（或双栈绑定失败）时回退到纯 IPv4，而不是直接启动失败
        if sys.stdout:
            print(f"IPv6 双栈监听失败（{e}），回退到 IPv4 监听...")
        httpd = IPv4HTTPServer(server_address, SparkRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    if sys.stdout:
        print("Stopping server...")


if __name__ == '__main__':
    run()
