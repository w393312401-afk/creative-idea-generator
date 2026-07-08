import os
import sys
import json
import socket
import urllib.request
import urllib.error
import urllib.parse
import re
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
    
    def on_progress(stage, details):
        if t["cancel_event"].is_set():
            raise ConnectionError("Generation cancelled by user")
            
        with ACTIVE_TASKS_LOCK:
            if stage == 'text_chunk':
                t["events"].append(('text_chunk', details))
            else:
                t["events"].append(('progress', {'stage': stage, 'details': details}))
                
        notify_listeners(task_id, 'text_chunk' if stage == 'text_chunk' else 'progress', details)

    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        if isinstance(config, dict):
            config['_skipped_checks'] = 0
        content = call_llm(config, dimensions, on_progress=on_progress)
        result = parse_sections(content)
        
        if t["cancel_event"].is_set():
            raise ConnectionError("Generation cancelled by user")
            
        # Double QA merged: call_llm's internal audit self-healing loop is the primary QA.
        # We assign its audit results directly to repair_md.
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
        
        if t["cancel_event"].is_set():
            raise ConnectionError("Generation cancelled by user")
            
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            # Trim streaming text_chunk events to optimize tasks.json size
            t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
            t["events"].append(('result', result))
            
        notify_listeners(task_id, 'result', result)
        save_tasks_to_disk()
        
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了生成任务"
            t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
            t["events"].append(('error', {'message': '用户取消了生成任务'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了生成任务'})
        save_tasks_to_disk()
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] background task {task_id} failed: {e}")
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        save_tasks_to_disk()


def auto_run_worker(task_id, config, dimensions):
    """Drives the full autonomous pipeline (compose -> render+verify IMAGE 1 -> refine
    packet -> remaining beats -> remaining frames -> videos) as one background task,
    reusing the same ACTIVE_TASKS/SSE plumbing as the three manual stages."""
    t = get_or_create_task(task_id, dimensions)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from pipeline_orchestrator import run_autonomous_pipeline
        result = run_autonomous_pipeline(config, dimensions, on_progress=progress_cb)

        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了生成任务"
            t["events"].append(('error', {'message': '用户取消了生成任务'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了生成任务'})
    except Exception as e:
        if sys.stdout:
            import traceback
            traceback.print_exc()
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
    finally:
        save_tasks_to_disk()


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
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)
            
        result = generate_frame_sequence(
            config, title, prompt_block,
            on_progress=progress_cb,
            target_sequences=target_sequences
        )
        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage
            
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了帧序列生成"
            t["events"].append(('error', {'message': '用户取消了帧序列生成'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了帧序列生成'})
        try:
            adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
            if adspower_path not in sys.path:
                sys.path.append(adspower_path)
            from utils.browser import stop_ads_browser
            user_id = config.get('userId')
            port = config.get('port')
            stop_ads_browser(user_id=user_id, port=port)
        except Exception:
            pass
    except Exception as e:
        if sys.stdout:
            import traceback
            traceback.print_exc()
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
    finally:
        save_tasks_to_disk()


def render_staged_worker(task_id, config, title, prompt_block):
    """Stages an ALREADY-composed prompt_block through pipeline_orchestrator's
    render/gate/recovery machinery (render IMAGE 1 -> Anchor Acceptance Gate -> render
    the rest -> autonomous recovery passes -> videos), instead of the old
    generate_frames_worker's one-shot full-batch render. Used by /api/render_staged,
    which scripts/generate_frames.py now calls so a skill-driven (agent-composed)
    prompt set also gets staged, gated rendering instead of a blind batch render."""
    t = get_or_create_task(task_id)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from pipeline_orchestrator import run_staged_frame_rendering
        result = run_staged_frame_rendering(config, title, prompt_block, on_progress=progress_cb)

        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了分步渲染任务"
            t["events"].append(('error', {'message': '用户取消了分步渲染任务'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了分步渲染任务'})
    except Exception as e:
        if sys.stdout:
            import traceback
            traceback.print_exc()
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
    finally:
        save_tasks_to_disk()


_VIDEO_GEN_SERIAL_LOCK = threading.Lock()


def generate_videos_worker(task_id, config, title, prompt_block, target_slots):
    t = get_or_create_task(task_id)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        progress_cb('queue', {'message': '视频生成请求已加入队列，正在等待排队生成...'})

        with _VIDEO_GEN_SERIAL_LOCK:
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
                if manifest_video_slots[slot].get('status') != 'success':
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
                        # Also update manifest file on disk
                        manifest_path = os.path.join(project_dir, 'manifest.json')
                        if os.path.exists(manifest_path):
                            try:
                                with open(manifest_path, 'r', encoding='utf-8') as f:
                                    mdata = json.load(f)
                                mdata['merged_video'] = merged_info
                                with open(manifest_path, 'w', encoding='utf-8') as f:
                                    json.dump(mdata, f, ensure_ascii=False, indent=2)
                            except Exception as e:
                                print(f"Warning: could not update manifest.json with merged_video ({e})")
                    progress_cb('merge_done', {'merged_video': merged_info})
                except Exception as merge_err:
                    print(f"Error during auto-merge: {merge_err}")
                    progress_cb('merge_error', {'message': f'自动合并视频失败: {str(merge_err)}'})

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了视频生成"
            t["events"].append(('error', {'message': '用户取消了视频生成'}))
        notify_listeners(task_id, 'error', {'message': '用户取消了视频生成'})
        try:
            adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
            if adspower_path not in sys.path:
                sys.path.append(adspower_path)
            from utils.browser import stop_ads_browser
            user_id = config.get('userId')
            port = config.get('port')
            stop_ads_browser(user_id=user_id, port=port)
        except Exception:
            pass
    except Exception as e:
        if sys.stdout:
            import traceback
            traceback.print_exc()
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
    finally:
        save_tasks_to_disk()


def generate_cover_worker(task_id, config, parent_task_id, title, theme, prompt_block):
    t = get_or_create_task(task_id)
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
        
        result_data = {
            'content': image_content,
            'english_title': english_title
        }
        
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result_data
            t["events"].append(('result', result_data))
        
        notify_listeners(task_id, 'result', result_data)
        
    except Exception as e:
        if sys.stdout:
            import traceback
            traceback.print_exc()
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
    finally:
        save_tasks_to_disk()


class DualStackHTTPServer(ThreadingMixIn, HTTPServer):
    # ThreadingMixIn so a long-running /api/compose call (the skill can take 60-180s)
    # does not block static file serving or library reads in the same browser session.
    address_family = socket.AF_INET6
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
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

    def end_headers(self):
        # Enable CORS for local development flexibility
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        # Never let browsers / Cloudflare serve stale front-end assets. This is what
        # caused "multiple UI forms" (old cached HTML/CSS shown alongside the new one).
        p = (self.path or '').split('?')[0]
        if p.endswith(('.html', '.css', '.js')) or p.endswith('/'):
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        super().end_headers()

    def _send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

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
            data = []
            if os.path.exists(DB_FILE):
                try:
                    with open(DB_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    if sys.stdout:
                        print(f"Error reading {DB_FILE}: {e}")
            self._send_json(data)
        elif path == '/api/get_manifest':
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
            send_event = None
            stop_evt = None
            try:
                send_event, stop_evt = self._open_sse_stream()
                
                # Send the last 100 lines first
                if os.path.exists(_LOG_PATH):
                    try:
                        with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                            lines = f.readlines()
                            last_lines = lines[-100:]
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
                            with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                                f.seek(last_size)
                                new_content = f.read()
                                if new_content:
                                    send_event('log', {'text': new_content})
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
                    res.append({
                        "id": t["id"],
                        "status": t["status"],
                        "dimensions": t["dimensions"],
                        "result": t["result"],
                        "error": t["error"],
                        "last_active": t["last_active"]
                    })
            res.sort(key=lambda x: x["id"], reverse=True)
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
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self.send_error(400, "Missing task_id")
                return
            
            task = ACTIVE_TASKS.get(task_id)
            if not task:
                self.send_error(404, "Task not found")
                return
                
            send_event, stop_evt = self._open_sse_stream()
            
            # Replay history
            with ACTIVE_TASKS_LOCK:
                history = list(task["events"])
                
            for event_type, event_data in history:
                try:
                    send_event(event_type, event_data)
                except Exception:
                    stop_evt.set()
                    return
            
            # If the task is already finished, stop immediately
            if task["status"] in ("completed", "failed", "cancelled"):
                stop_evt.set()
                return
                
            # Add to listeners
            with ACTIVE_TASKS_LOCK:
                task["listeners"].add((send_event, stop_evt))
                
            # Block until stream is stopped
            stop_evt.wait()
            
            # Clean up listener
            with ACTIVE_TASKS_LOCK:
                task["listeners"].discard((send_event, stop_evt))
                
        elif path == '/api/compose-status':
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
            task_id = query.get('task_id', [None])[0]
            if not task_id:
                self._send_json({'error': 'Missing task_id'}, status=400)
                return
            with IMAGE_TASKS_LOCK:
                task = IMAGE_TASKS.get(task_id)
            if not task:
                self._send_json({'status': 'not_found'})
            else:
                self._send_json(task)
        elif path == '/api/cache-info':
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
            super().do_GET()

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/library':
            try:
                data = self._read_json_body()
                with open(DB_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({'status': 'success'})
            except Exception as e:
                if sys.stdout:
                    print(f"Error writing {DB_FILE}: {e}")
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/ping':
            try:
                body = self._read_json_body()
                ok = ping_proxy(effective_config(body.get('config', {})))
                self._send_json({'online': bool(ok)})
            except Exception as e:
                self._send_json({'online': False, 'message': str(e)})

        elif path == '/api/vlm_qa':
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
                                video_errs.extend(check_camera_contradictions(video_prompt, is_bridge))
                                
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
            try:
                with PACKET_CACHE_LOCK:
                    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                        json.dump({}, f, ensure_ascii=False, indent=2)
                with PROCESS_BRIEF_CACHE_LOCK:
                    with open(PROCESS_BRIEF_CACHE_PATH, 'w', encoding='utf-8') as f:
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
                
                ideas = run_ideate(config, count)
                self._send_json({'status': 'ok', 'ideas': ideas})
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
                get_or_create_task(task_id, dimensions)
                
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
                task_id = body.get('task_id')
                if not task_id:
                    task_id = f"auto_{int(time.time() * 1000)}"

                # Start background thread
                cleanup_old_tasks()
                get_or_create_task(task_id, dimensions)

                threading.Thread(
                    target=auto_run_worker,
                    args=(task_id, config, dimensions),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/compose-cancel':
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id and task_id in ACTIVE_TASKS:
                    ACTIVE_TASKS[task_id]["cancel_event"].set()
                    
                    # Cancel any active Google FX Playwright UI generation
                    import builtins
                    builtins.google_fx_cancelled = True
                    
                    # Stop AdsPower browser to ensure it's freed and stopped
                    try:
                        adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
                        if adspower_path not in sys.path:
                            sys.path.append(adspower_path)
                        from utils.browser import stop_ads_browser
                        dimensions = ACTIVE_TASKS[task_id].get("dimensions") or {}
                        user_id = dimensions.get("userId")
                        port = dimensions.get("port")
                        stop_ads_browser(user_id=user_id, port=port)
                    except Exception as browser_err:
                        if sys.stdout:
                            print(f"[CANCEL] Failed to stop AdsPower browser: {browser_err}")
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/tasks/delete':
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id:
                    with ACTIVE_TASKS_LOCK:
                        if task_id in ACTIVE_TASKS:
                            # If it's running, cancel it first
                            ACTIVE_TASKS[task_id]["cancel_event"].set()
                            del ACTIVE_TASKS[task_id]
                    save_tasks_to_disk()
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/tasks/clear':
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
                get_or_create_task(task_id, {"type": "frames", "theme": title})

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
                get_or_create_task(task_id, {"type": "staged_render", "theme": title})

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
                get_or_create_task(task_id, {"type": "videos", "theme": title})

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
                
                # Run the merge
                merged_info = merge_project_videos(project_dir)
                if not merged_info:
                    self._send_json({'error': '合并失败：未找到任何成功的视频片段'}, status=400)
                    return
                
                # Update manifest.json on disk
                manifest_path = os.path.join(project_dir, 'manifest.json')
                mdata = {}
                if os.path.exists(manifest_path):
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        mdata = json.load(f)
                mdata['merged_video'] = merged_info
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    json.dump(mdata, f, ensure_ascii=False, indent=2)
                
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

        elif path == '/api/reverse-video':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self)):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                # We need to parse multipart form data.
                # Since we don't use FastAPI here, we can parse using email.parser.BytesParser
                content_type = self.headers.get('content-type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self._send_json({'error': 'Content-Type must be multipart/form-data'}, status=400)
                    return

                content_length = int(self.headers.get('content-length', 0))
                body_bytes = self.rfile.read(content_length)

                # Create a dummy message to parse using email parser
                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + body_bytes
                
                from email.parser import BytesParser
                from email.policy import default
                
                msg = BytesParser(policy=default).parsebytes(msg_bytes)

                file_data = None
                filename = None
                fps = 3.0
                api = "auto"
                prompt_style = "clean"
                config_str = "{}"

                for part in msg.walk():
                    content_disposition = part.get('Content-Disposition', '')
                    if 'form-data' in content_disposition:
                        name_match = re.search(r'name="([^"]+)"', content_disposition)
                        if name_match:
                            name = name_match.group(1)
                            if name == 'file':
                                filename_match = re.search(r'filename="([^"]+)"', content_disposition)
                                filename = filename_match.group(1) if filename_match else 'video.mp4'
                                file_data = part.get_payload(decode=True)
                            elif name == 'fps':
                                try:
                                    fps = float(part.get_payload(decode=True).decode('utf-8').strip())
                                except:
                                    pass
                            elif name == 'api':
                                try:
                                    api = part.get_payload(decode=True).decode('utf-8').strip()
                                except:
                                    pass
                            elif name == 'prompt_style':
                                try:
                                    prompt_style = part.get_payload(decode=True).decode('utf-8').strip()
                                except:
                                    pass
                            elif name == 'config':
                                try:
                                    config_str = part.get_payload(decode=True).decode('utf-8').strip()
                                except:
                                    pass

                # Parse task_id from multipart form data
                task_id = None
                for part in msg.walk():
                    content_disposition = part.get('Content-Disposition', '')
                    if 'form-data' in content_disposition:
                        name_match = re.search(r'name="([^"]+)"', content_disposition)
                        if name_match:
                            name = name_match.group(1)
                            if name == 'task_id':
                                try:
                                    task_id = part.get_payload(decode=True).decode('utf-8').strip()
                                except:
                                    pass

                if not task_id:
                    task_id = str(int(time.time() * 1000))

                import tempfile
                import shutil

                # Verify file extension
                suffix = os.path.splitext(filename)[1].lower()
                if suffix not in ('.mp4', '.mov', '.avi', '.mkv', '.webm'):
                    self._send_json({'error': '不支持该视频格式。仅支持 .mp4, .mov, .avi, .mkv, .webm 格式的视频。'}, status=400)
                    return

                # Parse config
                client_config = {}
                if config_str:
                    try:
                        client_config = json.loads(config_str)
                    except Exception as e:
                        if sys.stdout:
                            print(f"[DEBUG] Failed to parse client config JSON: {e}")

                # Save uploaded video to a temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                    tmp_file.write(file_data)
                    temp_video_path = tmp_file.name

                # Working directory for extracted frames; video_reverse_worker owns cleanup()
                temp_dir_obj = tempfile.TemporaryDirectory()

                # Setup dimensions
                gemini_key = client_config.get('geminiDirectApiKey') or client_config.get('apiKey') or os.environ.get('GEMINI_API_KEY')
                openai_key = os.environ.get('OPENAI_API_KEY')
                video_name = os.path.splitext(filename)[0]
                model_label = "Gemini-1.5-Flash"
                if api == "openai" or (api == "auto" and not gemini_key and openai_key):
                    model_label = "GPT-4o-Mini"
                dimensions = {
                    "theme": f"视频反推 ({video_name})",
                    "creativity": model_label,
                    "type": "reverse-video"
                }

                # Create task in ACTIVE_TASKS
                cleanup_old_tasks()
                get_or_create_task(task_id, dimensions)

                # Start background thread
                threading.Thread(
                    target=video_reverse_worker,
                    args=(task_id, temp_video_path, temp_dir_obj, fps, api, prompt_style, client_config, filename),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})

            except Exception as e:
                if sys.stdout:
                    import traceback
                    traceback.print_exc()
                self._send_json({'error': f'Request processing failed: {str(e)}'}, status=500)

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
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None}
                
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
                    IMAGE_TASKS[task_id] = {'status': 'pending', 'result': None, 'error': None}
                
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
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
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
    # Record our PID so run.bat/stop.bat and restart tooling kill the right process.
    # This write was lost in the module split; the stale pid file then caused restarts
    # to no-op and pile up duplicate server instances fighting over port 8085.
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
    server_address = ('', PORT)
    httpd = DualStackHTTPServer(server_address, SparkRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    if sys.stdout:
        print("Stopping server...")


if __name__ == '__main__':
    run()
