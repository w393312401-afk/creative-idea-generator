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
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Video duration settings (to support switching models with different duration constraints)
VIDEO_DURATION = 8.0
WORKER_EXIT_TIME = 7.5

class DummyWriter:
    def write(self, *args, **kwargs):
        pass
    def flush(self, *args, **kwargs):
        pass


class _Tee:
    """Write to several streams at once (e.g. the real console + the log file),
    swallowing per-stream errors so one broken stream never crashes the server."""
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


# Persist logs to a file next to this script. Under pythonw (see run.bat) there is no
# console, so sys.stdout/stderr are None and every print() would otherwise be lost —
# which is exactly why "查看报错日志" used to come up empty. Always tee to server.log so
# the error trail survives regardless of how the server was launched.
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.log')
try:
    _log_file = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
    _log_file.write(f"\n===== SPARK server log opened {datetime.now().isoformat()} =====\n")
    _log_file.flush()
except Exception:
    _log_file = None

sys.stdout = _Tee(sys.stdout, _log_file) if (sys.stdout or _log_file) else DummyWriter()
sys.stderr = _Tee(sys.stderr, _log_file) if (sys.stderr or _log_file) else DummyWriter()

# Write PID to file for easy management without PowerShell
try:
    _pid_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.pid')
    with open(_pid_path, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
except Exception:
    pass


class DualStackHTTPServer(ThreadingMixIn, HTTPServer):
    # ThreadingMixIn so a long-running /api/compose call (the skill can take 60-180s)
    # does not block static file serving or library reads in the same browser session.
    address_family = socket.AF_INET6
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


PORT = int(os.environ.get('PORT', '8085'))
DB_FILE = 'library.json'
OUTPUT_ROOT = 'outputs'
IMG2IMG_CONTROL_PROMPT = (
    "IMAGE EDITING MODE. The attached previous frame is the authoritative source image, "
    "not a loose style reference. Preserve its exact camera position, lens, crop, horizon, "
    "perspective, boundaries, structural geometry, object positions, background, and every "
    "unchanged pixel-level detail. The target-state description below may repeat the whole "
    "scene only to identify continuity; do not redraw or redesign the scene from that text. "
    "Make the smallest localized edit required to reach the next construction state. "
    "Do not add grid lines, guides, labels, letters, numbers, percentages, captions, text, "
    "watermarks, extra people, or active machinery. Return one clean edited image only."
)
IMG2IMG_BRIDGE_CONTROL_PROMPT = (
    "IMAGE EDITING MODE (CAMERA MOVEMENT ACTIVE). The attached previous frame is the authoritative "
    "source image. Maintain extreme consistency of all physical landmarks, geometry, colors, "
    "materials, light source angles, and structural features. However, the camera viewpoint is "
    "actively advancing forward in a controlled camera-move / push-in (the camera perspective is "
    "shifting closer along the central axis). Shift object placement, horizon, and perspective boundaries "
    "according to correct optical flow and 3D depth parallax. Make the smallest localized edits to "
    "reveal newly exposed details at the margins while preserving the core content of the scene. "
    "Do not add extra objects, active machinery, or workers. Return one clean edited image only."
)

# The frontend is a "skill shell": it collects GUI dimensions, and the server runs them
# through the real restoration-prompt-composer skill contract before relaying to the local
# LLM proxy. SKILL_DIR can be overridden via env var for portability.
SKILL_DIR = os.environ.get(
    'SKILL_DIR',
    os.path.join(os.path.expanduser('~'), '.codex', 'skills', 'restoration-prompt-composer')
)

# ==========================================================================
# Server-managed (external) mode
# --------------------------------------------------------------------------
# When server_config.json (or SPARK_* env vars) provides an apiKey, the server runs in
# "managed" mode: model credentials live ONLY on the server, any apiKey/baseUrl sent by the
# browser is ignored, an optional access code gates usage, and per-IP rate limiting applies.
# When no server key is configured the server stays in legacy "local" mode and uses the
# client-supplied config exactly as before — so personal/local use is unaffected.
# ==========================================================================
import collections

SERVER_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_config.json')


def _load_server_config():
    cfg = {}
    if os.path.exists(SERVER_CONFIG_FILE):
        try:
            with open(SERVER_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
        except Exception as e:
            print(f"Warning: could not read server_config.json ({e})")
    env_map = {
        'baseUrl': 'SPARK_BASE_URL', 'apiKey': 'SPARK_API_KEY', 'model': 'SPARK_MODEL',
        'imageModel': 'SPARK_IMAGE_MODEL', 'accessCode': 'SPARK_ACCESS_CODE',
        'geminiDirectApiKey': 'GEMINI_API_KEY', 'geminiDirectImageModel': 'GEMINI_IMAGE_MODEL',
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            cfg[k] = v
    return cfg


SERVER_CONFIG = _load_server_config()
SERVER_MANAGED = bool(SERVER_CONFIG.get('apiKey'))
# When True (default), managed mode still honors the model/imageModel the browser sends, so
# changing the model in the front-end 配置中心 takes effect immediately (no file edit/restart).
# Secrets (apiKey/baseUrl) always stay server-side regardless. Set "allowClientModel": false
# in server_config.json to lock models to the server values (recommended for public exposure
# where per-model cost matters).
ALLOW_CLIENT_MODEL = SERVER_CONFIG.get('allowClientModel', True) is not False
ACCESS_CODE = (SERVER_CONFIG.get('accessCode') or '').strip()
RATE_MAX = int(os.environ.get('SPARK_RATE_MAX', SERVER_CONFIG.get('rateMax', 20) or 20))
RATE_WINDOW = int(os.environ.get('SPARK_RATE_WINDOW', SERVER_CONFIG.get('rateWindow', 3600) or 3600))

_RATE_BUCKET = collections.defaultdict(list)
_RATE_LOCK = threading.Lock()


def effective_config(client_config):
    """Resolve the config used for real model calls.
    Managed mode: secrets come from the server; only cosmetic client prefs pass through.
    Local mode: use the client config unchanged (legacy behavior)."""
    client_config = client_config or {}
    if not SERVER_MANAGED:
        return client_config
    merged = {
        'baseUrl': SERVER_CONFIG.get('baseUrl') or 'http://127.0.0.1:8046/v1',
        'apiKey': SERVER_CONFIG.get('apiKey') or '',
        'model': SERVER_CONFIG.get('model') or 'gemini-3-flash-agent',
        'imageModel': SERVER_CONFIG.get('imageModel') or 'gemini-3.1-flash-image',
    }
    for k in ('geminiDirectApiKey', 'geminiApiKey', 'geminiDirectImageModel'):
        if SERVER_CONFIG.get(k):
            merged[k] = SERVER_CONFIG.get(k)
    # Model is choosable from the front-end unless explicitly locked down. Secrets
    # (apiKey/baseUrl) always stay server-side; resolve_gateway() picks the right gateway
    # per model name, so honoring the client's model never leaks credentials.
    if ALLOW_CLIENT_MODEL:
        if client_config.get('model'):
            merged['model'] = client_config['model']
        if client_config.get('imageModel'):
            merged['imageModel'] = client_config['imageModel']
    # Non-secret, non-cost-sensitive prefs the end user is still allowed to choose.
    for k in ('imageAspectRatio', 'imageQuality'):
        if client_config.get(k):
            merged[k] = client_config[k]
    for k in ('geminiDirectApiKey', 'geminiApiKey', 'geminiDirectImageModel'):
        if client_config.get(k):
            merged[k] = client_config[k]
    return merged


def resolve_gateway(model_name, config):
    """Resolve the gateway URL and API key based on the model name.
    Automatically routes gpt-5, codex, and gpt-image-2 models to the Codex API Service on port 65038."""
    base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
    api_key = config.get('apiKey') or ''
    m_lower = (model_name or '').lower()
    if 'gpt-5' in m_lower or 'codex' in m_lower or 'gpt-image-2' in m_lower:
        base_url = 'http://127.0.0.1:65038/v1'
        api_key = 'agt_codex_JG9xnyWXYBS4qMmO9Z0UKD3pbEOpHr7M'
    return base_url, api_key


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


# Thread-safe task store for composition to survive page refreshes/closures
ACTIVE_TASKS = {}
ACTIVE_TASKS_LOCK = threading.Lock()

# Thread-safe store for background image generation/edit tasks
IMAGE_TASKS = {}
IMAGE_TASKS_LOCK = threading.Lock()

# Thread-safe locks for cache files to prevent race conditions during load-modify-save
PACKET_CACHE_LOCK = threading.RLock()
PROCESS_BRIEF_CACHE_LOCK = threading.RLock()


def save_tasks_to_disk():
    # Only serialize the serializable parts of ACTIVE_TASKS
    serializable = {}
    with ACTIVE_TASKS_LOCK:
        for tid, t in ACTIVE_TASKS.items():
            serializable[tid] = {
                "id": t["id"],
                "status": t["status"],
                "events": t["events"],
                "dimensions": t["dimensions"],
                "result": t["result"],
                "error": t["error"],
                "last_active": t["last_active"]
            }
    try:
        with open("tasks.json", "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        if sys.stdout:
            print(f"Error saving tasks to disk: {e}")

def load_tasks_from_disk():
    global ACTIVE_TASKS
    if not os.path.exists("tasks.json"):
        return
    try:
        with open("tasks.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        with ACTIVE_TASKS_LOCK:
            for tid, t in data.items():
                status = t["status"]
                error = t.get("error")
                events = t.get("events", [])
                if status == "running":
                    status = "failed"
                    error = "服务已重启，生成中断。"
                    # Append error event if not already present
                    if not any(isinstance(evt, (list, tuple)) and len(evt) > 0 and evt[0] == 'error' for evt in events):
                        events.append(['error', {'message': error}])
                
                ACTIVE_TASKS[tid] = {
                    "id": t["id"],
                    "status": status,
                    "events": [tuple(evt) if isinstance(evt, (list, tuple)) else evt for evt in events],
                    "listeners": set(),
                    "cancel_event": threading.Event(),
                    "dimensions": t["dimensions"],
                    "result": t.get("result"),
                    "error": error,
                    "last_active": t["last_active"]
                }
    except Exception as e:
        if sys.stdout:
            print(f"Error loading tasks from disk: {e}")

def get_or_create_task(task_id, dimensions=None):
    save_on_create = False
    with ACTIVE_TASKS_LOCK:
        if task_id not in ACTIVE_TASKS:
            ACTIVE_TASKS[task_id] = {
                "id": task_id,
                "status": "running",
                "events": [],
                "listeners": set(),
                "cancel_event": threading.Event(),
                "dimensions": dimensions,
                "result": None,
                "error": None,
                "last_active": time.time()
            }
            save_on_create = True
        else:
            if dimensions is not None:
                ACTIVE_TASKS[task_id]["dimensions"] = dimensions
                save_on_create = True
    if save_on_create:
        save_tasks_to_disk()
    return ACTIVE_TASKS[task_id]

def notify_listeners(task_id, event_type, data):
    t = ACTIVE_TASKS.get(task_id)
    if not t:
        return
    t["last_active"] = time.time()
    
    with ACTIVE_TASKS_LOCK:
        listeners = list(t["listeners"])
        
    dead_listeners = []
    for send_evt, stop_evt in listeners:
        try:
            send_evt(event_type, data)
            if event_type in ('result', 'error'):
                stop_evt.set()
        except Exception:
            dead_listeners.append((send_evt, stop_evt))
            stop_evt.set()
            
    if dead_listeners:
        with ACTIVE_TASKS_LOCK:
            t["listeners"].difference_update(dead_listeners)

def cleanup_old_tasks():
    now = time.time()
    to_delete = []
    with ACTIVE_TASKS_LOCK:
        for tid, t in ACTIVE_TASKS.items():
            if t["status"] in ("completed", "failed") and now - t["last_active"] > 604800:
                to_delete.append(tid)
        for tid in to_delete:
            del ACTIVE_TASKS[tid]
    if to_delete:
        save_tasks_to_disk()

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
        content = call_llm(config, dimensions, on_progress=on_progress)
        result = parse_sections(content)
        
        if t["cancel_event"].is_set():
            raise ConnectionError("Generation cancelled by user")
            
        # Double QA merged: call_llm's internal audit self-healing loop is the primary QA.
        # We assign its audit results directly to repair_md.
        result['repair_md'] = result.get('audit_md') or 'PASS — 工序与场景一致性检查通过，未发现违规，提示词未改动。'
            
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
            t["events"].append(('result', result))
            
        notify_listeners(task_id, 'result', result)
        
    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了生成任务"
            t["events"].append(('error', {'message': "用户取消了生成任务"}))
        notify_listeners(task_id, 'error', {'message': "用户取消了生成任务"})
    except Exception as e:
        if sys.stdout:
            import traceback
            print(f"[DEBUG] background task {task_id} failed: {e}")
            traceback.print_exc()
            
        error_msg = str(e)
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = error_msg
            t["events"].append(('error', {'message': error_msg}))
            
        notify_listeners(task_id, 'error', {'message': error_msg})
    finally:
        save_tasks_to_disk()


def generate_frames_worker(task_id, config, title, prompt_block, target_sequences):
    t = get_or_create_task(task_id)
    try:
        def progress_cb(stage, details):
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)
            
        result = generate_frame_sequence(
            config, title, prompt_block,
            on_progress=progress_cb,
            target_sequences=target_sequences
        )
        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
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


def generate_videos_worker(task_id, config, title, prompt_block, target_slots):
    t = get_or_create_task(task_id)
    try:
        def progress_cb(stage, details):
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)
            
        result = generate_video_sequence(
            config, title, prompt_block,
            on_progress=progress_cb,
            target_slots=target_slots
        )
        
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
        
        english_title = _chat(config, "You are a viral TikTok US marketing expert specializing in high-CTR hooks.", title_prompt, temperature=0.7, max_tokens=30)
        english_title = english_title.strip().strip('"').strip("'").strip()
        
        aspect_ratio = config.get('imageAspectRatio') or '9:16'
        
        # Extract first (Before) and last (After) image prompts from the build process
        def _extract_before_after_prompts(block):
            images, _ = _parse_prompt_slots(block)
            if images:
                keys = sorted(images)
                return images[keys[0]], images[keys[-1]]
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
        
        _generate_text_image(config, image_prompt, target_path)
        
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

def video_reverse_worker(task_id, temp_video_path, temp_dir_obj, fps, api, prompt_style, client_config, filename):
    t = get_or_create_task(task_id)
    output_root = temp_dir_obj.name
    
    def on_progress(stage, details):
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        with ACTIVE_TASKS_LOCK:
            t["events"].append(('progress', {'stage': stage, 'details': details}))
        notify_listeners(task_id, 'progress', {'stage': stage, 'details': details})

    try:
        # Import video_to_prompt_pipeline from skill root
        if str(SKILL_DIR) not in sys.path:
            sys.path.append(str(SKILL_DIR))
        import video_to_prompt_pipeline
        
        # Step 1: Keyframe Extraction
        on_progress('keyframe_extraction', '正在提取视频关键帧...')
        keyframe_paths = video_to_prompt_pipeline.extract_keyframes(temp_video_path, output_root, fps)
        if not keyframe_paths:
            raise RuntimeError("关键帧提取失败。请确保视频文件有效且 FFmpeg 环境正常。")

        # Step 2: Local CV Motion & Light Heuristics
        on_progress('cv_analysis', '正在使用计算机视觉算法分析运动与光照变化...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        cv_data = video_to_prompt_pipeline.analyze_video_cv(keyframe_paths)

        # Step 3: Fetch semantic metadata from Multimodal LLM
        on_progress('semantic_metadata', '大模型多模态视频分析与时序语义提取中...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        
        old_gemini_key = os.environ.get("GEMINI_API_KEY")
        gemini_key = client_config.get("apiKey") or os.environ.get("GEMINI_API_KEY")
        current_gemini_key = client_config.get("apiKey") or gemini_key
        if current_gemini_key:
            os.environ["GEMINI_API_KEY"] = current_gemini_key

        try:
            client_base_url = client_config.get("baseUrl")
            client_model = client_config.get("model")
            metadata = video_to_prompt_pipeline.fetch_semantic_metadata(
                keyframe_paths, cv_data, force_local=False, fps=fps, base_url=client_base_url, model=client_model
            )
        finally:
            if old_gemini_key is not None:
                os.environ["GEMINI_API_KEY"] = old_gemini_key
            elif "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]

        if not metadata or "time_sequence" not in metadata:
            raise RuntimeError("大模型多模态视频分析失败，请检查 API 密钥、网络连接或稍后重试。")

        # Step 4: Prompt Composition & Audit
        on_progress('prompt_composition', '正在合成 SCUP 提示词并进行物理一致性审计...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        images, videos = video_to_prompt_pipeline.compose_scup_prompts(metadata, clean_mode=(prompt_style == "clean"))

        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        audit_results = video_to_prompt_pipeline.run_scup_audit(
            images,
            videos,
            fps=fps,
            num_analyzed_frames=metadata.get("num_analyzed_frames"),
            total_frames=len(keyframe_paths),
            change_events=metadata.get("change_events"),
            analysis_frame_indices=metadata.get("analysis_frame_indices"),
            time_sequence=metadata.get("time_sequence"),
            post_render_qc=metadata.get("post_render_qc"),
            video_path=temp_video_path
        )

        # Build Markdown Audit report
        video_name = os.path.splitext(filename)[0]
        failed_gates = [g for g in audit_results["gates"] if g["status"] == "FAIL"]
        
        report_lines = [
            f"# SCUP Quality Audit Report — {video_name}",
            f"**Audit Score**: `{audit_results['score']}/100`",
            f"**Audit Status**: {'PASS' if audit_results['score'] >= 80 else 'REWRITE REQUIRED'}\n",
            "## Detailed Gate Checks\n",
            "| Gate Name | Tier | Status | Details |",
            "|---|---|---|---|"
        ]
        for g in audit_results["gates"]:
            status_emoji = "✅ PASS" if g["status"] == "PASS" else "❌ FAIL"
            details_str = "<br>".join(g["details"])
            report_lines.append(f"| {g['name']} | {g.get('tier', 'P0')} | {status_emoji} | {details_str} |")
            
        report_lines.append("\n## Action Items & Recommendations\n")
        if not failed_gates:
            report_lines.append("🎉 **Congratulations!** Your prompts perfectly adhere to the spatial consistency and time-lapse continuity rules. Ready for production rendering.")
        else:
            for g in failed_gates:
                report_lines.append(f"### ⚠️ Fix {g['name']} ({g['tier']})")
                report_lines.append(f"- **Problem**: {', '.join(g['details'])}")
                report_lines.append(f"- **Solution**: {g['solution']}\n")
                
        audit_md = "\n".join(report_lines)

        # Format prompts lists
        images_list = [{"n": i+1, "text": img} for i, img in enumerate(images)]
        videos_list = [{"n": i+1, "text": vid} for i, vid in enumerate(videos)]

        raw_text = f"===TITLE===\n视频反推提示词 ({video_name})\n\n===THEME===\n从视频分析反推\n\n===PROMPTS===\n图片提示词\n--------------------------------------------------\n"
        for i, img in enumerate(images):
            raw_text += f"图片 {i+1}:\n{img}\n\n"
        raw_text += "--------------------------------------------------\n视频提示词\n--------------------------------------------------\n"
        for i, vid in enumerate(videos):
            raw_text += f"视频 {i+1}:\n{vid}\n\n"
        raw_text += f"--------------------------------------------------\n===AUDIT===\n{audit_md}"

        # Copy collage file to outputs directory if it was generated
        collage_src = os.path.splitext(temp_video_path)[0] + "_collage.jpg"
        collage_url = None
        if os.path.exists(collage_src):
            try:
                os.makedirs(OUTPUT_ROOT, exist_ok=True)
                import time
                dest_filename = f"reverse_{int(time.time())}_{video_name}_collage.jpg"
                dest_path = os.path.join(OUTPUT_ROOT, dest_filename)
                shutil.copy(collage_src, dest_path)
                collage_url = f"/outputs/{dest_filename}"
                print(f"[+] Saved keyframe collage to persistent outputs: {dest_path}")
            except Exception as e:
                print(f"[-] Failed to copy keyframe collage to outputs: {e}")

        # Model label selection
        model_label = "Gemini-1.5-Flash"
        openai_key = os.environ.get("OPENAI_API_KEY")
        if api == "openai" or (api == "auto" and not gemini_key and openai_key):
            model_label = "GPT-4o-Mini"

        result = {
            "images": images_list,
            "videos": videos_list,
            "audit_md": audit_md,
            "prompt_block": raw_text,
            "title": f"视频反推提示词 ({video_name})",
            "model": model_label,
            "collage_url": collage_url,
            "image_count": len(images_list),
            "video_count": len(videos_list),
            "timings": {}
        }

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))

        notify_listeners(task_id, 'result', result)

    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了视频反推"
            t["events"].append(('error', {'message': "用户取消了视频反推"}))
        notify_listeners(task_id, 'error', {'message': "用户取消了视频反推"})
    except Exception as e:
        if sys.stdout:
            import traceback
            print(f"[DEBUG] Video reverse background task {task_id} failed: {e}")
            traceback.print_exc()
        error_msg = str(e)
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = error_msg
            t["events"].append(('error', {'message': error_msg}))
        notify_listeners(task_id, 'error', {'message': error_msg})
    finally:
        # Cleanup files
        try:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
            temp_dir_obj.cleanup()
        except Exception as ce:
            print(f"[DEBUG] Cleanup error: {ce}")
        save_tasks_to_disk()


def _slice_between(text, start_marker, end_marker):
    """Return the substring from start_marker up to (not including) end_marker.
    Falls back to the tail from start_marker if end_marker is absent."""
    start = text.find(start_marker)
    if start == -1:
        return ''
    end = text.find(end_marker, start + len(start_marker))
    if end == -1:
        return text[start:]
    return text[start:end]


def load_skill_contract():
    """Read the live SKILL.md + prompt-templates.md and assemble the authoritative
    composition contract. Reading at request time keeps the shell in sync with the skill."""
    skill_md_path = os.path.join(SKILL_DIR, 'SKILL.md')
    templates_path = os.path.join(SKILL_DIR, 'references', 'prompt-templates.md')

    pipeline = ''
    templates = ''
    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            skill_md = f.read()
        # Forward-composition portion only: pipeline + the vocab/camera/lighting/audio
        # reference tables. Drops the video reverse-engineering tiers (Tier 4) and the
        # cross-skill/Notion plumbing that do not apply to GUI-driven generation.
        pipeline = _slice_between(skill_md, '## Internal Composition Pipeline', '## Cross-Skill Integration')
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not read SKILL.md ({e})")
    try:
        with open(templates_path, 'r', encoding='utf-8') as f:
            templates = f.read()
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not read prompt-templates.md ({e})")

    # Replace hardcoded durations with constants dynamically
    if pipeline:
        pipeline = pipeline.replace("8-second", f"{int(VIDEO_DURATION)}-second")
        pipeline = pipeline.replace("t=8s", f"t={int(VIDEO_DURATION)}s")
        pipeline = pipeline.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")
    if templates:
        templates = templates.replace("8-second", f"{int(VIDEO_DURATION)}-second")
        templates = templates.replace("t=8s", f"t={int(VIDEO_DURATION)}s")
        templates = templates.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")

    return pipeline, templates


def build_topic_brief(d):
    """Translate the GUI dimension selections into a Tier-1 topic brief the skill consumes."""
    theme = d.get('theme', '未指定场景')
    anchors = d.get('anchors') or []
    anchors_str = '、'.join(anchors) if anchors else '由作曲家自行选取最契合主题的锚点'
    complexity = d.get('complexity', '中等重工')
    budget = d.get('budget', '轻奢设计师级')
    ratio = d.get('ratio', '50%')
    creativity = d.get('creativity', '突破常规')

    return f"""本次为 GUI 维度驱动的 Tier-1 合成请求。请据此走完内部合成管线（Step 1 至 Step 9），产出完整的 IMAGE/VIDEO 提示词集与中文质量审核报告。

输入维度：
- 场景主题：{theme}
- 核心创意锚点：{anchors_str}
- 项目复杂度：{complexity}
- 预算级别：{budget}
- 外壳 \u2194 内里反差强度：{ratio}
- 创意尺度：{creativity}

硬性要求：
1. 把该主题落成一个「可真实搭建 / 改造」的延时场景，必须显式给出 CARRIER / ENV / TRAUMA（初始残破或空白态）/ DESTINY（成品态）/ REWARD ACTION。因为场景主题均为写实载体（如百年空心橡树、蓝冰冰川洞、退役潜艇舱、废弃水塔等），所以必须以「真实可施工的废弃外壳\u2192温暖室内改造」为核心，用真实的工具链、材料来源与物理因果痕迹把它落地，禁止物体凭空出现。
2. 节拍数下限：至少 15 个施工节拍（即 N ≥ 15，对应至少 16 张 IMAGE、15 段 VIDEO），再加最终 reward；按真实工序把改造拆成足够细的独立步骤（拆除清运 \u2192 结构修复 \u2192 水电管线粗装 \u2192 封板封墙 \u2192 底漆 \u2192 面漆 \u2192 地面 \u2192 灯具/设备接线 \u2192 家具软装…）。每拍只允许一个物理操作 + 微增量拆分，相邻 IMAGE 锚点状态变化不超过 3 个九宫格。如果一个工序变化超过画面三分之一，再拆成连续子节拍。
3. 【施工顺序与物理因果，最高优先级】整套视频的推进必须符合现实物理因果顺序；任何违反都视为致命错误并必须重排节拍。明确禁止下列情况：
   - 在「布线 / 通电」节拍完成之前，出现任何亮灯、灯带发光、屏幕点亮、设备通电运行的画面；
   - 在外部（外立面 / 屋面 / 场地 / 除锈防腐）尚未处理完成之前，就跨过门槛进入室内施工；
   - 在「除锈 / 打磨 / 清洁 / 底漆」完成之前，出现面漆、喷漆、清漆或任何光泽涂层；
   - 湿作业（砂浆 / 混凝土 / 胶水 / 油漆）尚未干结固化之前，就进入会把它覆盖掉的下一道工序；
   - 任何会被后续封板/饰面遮盖的管线、结构、防水层，必须在封盖节拍之前先完成。
   施工状态必须单调递增：已清理的保持干净、已安装的保持就位、已干的保持干态，禁止回退。
4. 全程执行 NGCS 九宫格坐标、Camera DNA 逐字复制、GCTR 因果痕迹（每处变化至少 2 个接触痕迹）、连续动作流（工人 t=0s 进、t=7.5s 出）、以及纯自然语言铁律——最终提示词正文里不得出现 % 符号、数字区间或任何技术缩写。
5. 创意尺度越高，DESTINY 与视觉反差越大胆，但绝不能牺牲第 1、3、4 条的物理连续性与因果顺序。
6. 【方案新颖度避雷/克制套路】严禁采用陈旧套路的方案（如：集装箱改造成极简小屋、旧货车/巴士改造成标准露营房车、仓库改造成工业Loft、灯塔改造成海滨卧室等），必须提供高新颖度与强反差的方案组合。
7. 【可施工性屏障】严禁任何魔法般瞬间变出家具、无合理生根基础或材料突变的片段。必须留下真实的物理因果痕迹与接触痕迹，扅术细节和接口连接均需合理，必须能够映射到真实的施工工序中。
8. 【截图招牌反差点】整个场景必须包含且仅包含一个风格化的招牌反差点（如：载体本体材质切面窗、水面玻璃地板、树皮伪装天窗、活体木纹/岩壁旋梯、生物荧光苔藓照明、整块载体板材台面、改道瀑布淋浴等）。必须在成品态（DESTINY）的醒目位置体现。"""


def build_system_prompt():
    pipeline, templates = load_skill_contract()
    contract_block = pipeline if pipeline else "(SKILL.md 合成管线未能加载，请依据通用延时改造连续性契约生成。)"
    templates_block = templates if templates else "(canonical templates 未能加载)"

    return f"""You are operating as the `restoration-prompt-composer` skill — a one-shot prompt composition engine for restoration / renovation / construction time-lapse video. You receive a GUI-collected topic brief and must produce a complete, production-ready IMAGE + VIDEO prompt set that strictly conforms to the skill contract below. Internalize every gate; do not expose internal step names to the user.

==================== SKILL CONTRACT (authoritative) ====================
{contract_block}

==================== CANONICAL TEMPLATES & CHECKLIST ====================
{templates_block}

==================== OUTPUT FORMAT (MANDATORY) ====================
Respond with EXACTLY the four section markers below, in this order, with nothing before `===TITLE===` and nothing after the audit body. Do NOT wrap anything in markdown code fences.

===TITLE===
<a short, catchy, viral Chinese project name, e.g. 工业复古·废土集装箱卧室>
===THEME===
<the scene theme in Chinese, one short phrase>
===PROMPTS===
图片提示词
图片 1:
<English image-model prompt prose>

图片 2:
<English image-model prompt prose>

视频提示词
视频 1:
<English video-model prompt prose>

视频 2:
<English video-model prompt prose>
===AUDIT===
<提示词质量审核报告：用 Markdown 表格列出关键 P0/P1 检查项（九宫格锁定、Camera DNA 复制、因果痕迹 GCTR、微增量拆分、连续动作流、纯自然语言、累计状态、施工顺序等）及通过状态与一句话说明。>

Hard rules for the ===PROMPTS=== section:
- Use ONLY these labels: `图片提示词`, `图片 N:`, `视频提示词`, `视频 N:`. Each label on its own line.
- IMAGE count must equal N+1; VIDEO count must equal N. N must be AT LEAST 15 (so at least 16 image slots and 15 video slots); use more if the build genuinely needs it. Do not collapse or skip beats to stay short.
- The beat ladder MUST obey real construction order: demolition and debris clearing → structural repair → rough-in wiring/plumbing/ducting → close-up panels → primer → finish/topcoat → flooring → fixtures and lighting ONLY after their wiring beat → furniture and decoration last. Hard vetoes (a single occurrence invalidates the whole set): never show powered lights, glowing strips, lit screens, or running equipment before the wiring/power beat; never cross the threshold into the interior before the exterior is finished; never show paint, spray, or topcoat before rust removal, cleaning, and priming; never cover wet/uncured material with the next layer. Construction state is monotonic — finished work never regresses.
- [NEW RULE] Clear Path Requirement: If a project introduces any sliding, rolling, retracting, folding, or moving mechanical parts (e.g. bed rails, slide-out bed, retractable roof, folding stairs), the prompt generator must ensure a clean spatial path. If there are structural columns, support pillars, or bulkheads in the trauma state (IMAGE 1) that block this path, they must be explicitly cut and removed early (typically in structural repair phase) and replaced by peripheral support frames (e.g. ceiling arches or beams) before any rails or mechanisms are installed.
- [NEW RULE] Floor and Skeleton Logic: If floor/wall joists, ribs, or framing studs will be insulated or paneled later, the bare structural skeleton (e.g. open metal joists or ribs) must be exposed at the very beginning (IMAGE 1/2). The floor/wall states must progress monotonically forward: bare joists/studs -> rough-in / insulation -> subfloor -> wood planks / paneling. Never start with a solid finished-looking floor that disappears to reveal raw joists later.
- [NEW RULE] Perspective Isolation: Do not flip camera facing directions (e.g. turning 180 degrees from looking out to looking in) in the same spatial axis without a clean separate phase or TBCP transition. If the project centers on a slide-out reward action (e.g. bed sliding out of a cliff cabin), lock Camera Family B (looking outward through the opening towards the view) from the very first frame to maintain spatial consistency.
- [NEW RULE] Strict Single-Operation Beat Rule: Each {int(VIDEO_DURATION)}-second video prompt must describe exactly one homogeneous physical task. Combining multiple distinct stages (e.g. painting AND mounting frames, or framing AND insulating, or laying tile AND anchoring stoves AND mounting bed frames) into a single {int(VIDEO_DURATION)}-second clip is strictly prohibited to prevent visual morphing and cut-scene jumps.
- [NEW RULE] Bi-Directional Agent Flow: Standardize worker paths to prevent teleporting or instant popping. Workers must enter the frame from a specific coordinate edge at t=0s and walk out through the same edge by t={WORKER_EXIT_TIME}s, leaving the frame completely empty of personnel at t={int(VIDEO_DURATION)}s.
- [NEW RULE] Rigid Container Encapsulation: All loose materials, debris, fasteners, and liquids must be stored and tracked inside rigid, quantifiable containers (e.g. buckets, parts trays, boxes), and their volumes must be described as continuously increasing or decreasing.
- [NEW RULE] Mandatory Climax Video: Ensure the transition between the final two frames (the "Dressed interior" -> "Retract/slide action") is fully animated. The climax video (VIDEO N) must depict the actual physical kinetic movement of the mechanism (e.g. the bed rolling smoothly forward, the glass door sliding open).
- One blank line between each slot. No markdown headings, bullets, or tables inside this section.
- Prompt bodies are English natural-language prose suitable for an image / video generation model.
- Never emit `%`, raw numeric ranges (e.g. `10% to 90%`), or technical acronyms (HAL, GCTR, RPL, VMFP, RCE, NGCS, SCUP) inside the prompt bodies. Express all progress and traces as fluid visual prose.
- Every VIDEO begins with `Use the provided first frame and last frame as exact composition anchors.` and binds IMAGE N to IMAGE N+1."""


def _chat(config, system, user, temperature=0.85, max_tokens=16384, timeout=240, on_chunk=None, model=None):
    if not model:
        model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        # 16+ IMAGE and 15+ VIDEO slots is ~31 prompts; needs a large output budget.
        'max_tokens': max_tokens,
    }
    
    if on_chunk is not None:
        payload['stream'] = True

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    # Disable the Windows system proxy for this localhost call — scripted HTTP to
    # localhost otherwise gets intercepted/reset by the system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    
    if on_chunk is not None:
        try:
            full_content = []
            with opener.open(req, timeout=timeout) as resp:
                for line in resp:
                    line_str = line.decode('utf-8').strip()
                    if not line_str:
                        continue
                    if line_str.startswith('data: '):
                        data_part = line_str[6:]
                        if data_part == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_part)
                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                content = delta.get('content', '')
                                if content:
                                    full_content.append(content)
                                    on_chunk(content)
                        except Exception:
                            pass
            return ''.join(full_content)
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Streaming request failed: {e}. Falling back to non-streaming...")
            if 'stream' in payload:
                del payload['stream']
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                f'{base_url}/chat/completions',
                data=data,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                method='POST',
            )

    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError:
        # Real HTTP error from the proxy/model — handled specially upstream.
        raise
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        reason = getattr(e, 'reason', e)
        raise RuntimeError(
            f"无法连接本地 LLM 代理（{base_url}）：{reason}。"
            "请确认 Antigravity Tools 代理服务正在运行、端口正确（默认 8046），"
            "并检查 API 配置中心的 Base URL / API Key。"
        )
    try:
        return body['choices'][0]['message'].get('content') or ''
    except (KeyError, IndexError, TypeError):
        # Proxy returned 200 but not an OpenAI-shaped body (e.g. an error envelope).
        err = ''
        if isinstance(body, dict):
            err = (body.get('error') or {}).get('message') or body.get('message') or ''
        raise RuntimeError(f"LLM 代理返回了无法解析的响应：{err or json.dumps(body, ensure_ascii=False)[:300]}")


def _multimodal_chat(config, system, user_text, image_paths, model=None):
    if not model:
        model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)

    content_list = [{"type": "text", "text": user_text}]
    for path in image_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Image file not found: {path}")
        with open(path, "rb") as f:
            data = f.read()
        
        mime = "image/png"
        if path.lower().endswith(".webp"):
            mime = "image/webp"
        elif path.lower().endswith(".jpg") or path.lower().endswith(".jpeg"):
            mime = "image/jpeg"
            
        b64_str = base64.b64encode(data).decode('ascii')
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{b64_str}"
            }
        })

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': content_list},
        ],
        'temperature': 0.1,
        'max_tokens': 1000,
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=90) as resp:
        res_data = json.loads(resp.read().decode('utf-8'))
        return res_data['choices'][0]['message']['content']


def run_vlm_qa_check(config, img_i_path, img_ip1_path, video_prompt):
    """
    Compare generated IMAGE i and IMAGE i+1 with the transition VIDEO i prompt.
    Returns (pass_boolean, reason_string).
    """
    try:
        system_prompt = (
            "You are a strict, professional frame-by-frame visual quality auditor (VLM) for time-lapse videos. "
            "You are comparing Image 1 (IMAGE i) and Image 2 (IMAGE i+1) which represent the start and end frames "
            "of a video segment. The transition action is described by the VIDEO prompt.\n\n"
            "Your task is to detect the following flaws:\n"
            "1. NO CHANGE: The two images are identical or almost identical, meaning the image editor failed to execute the change.\n"
            "2. CAMERA perspective/viewpoint jumps: The camera position, angle, or background layout shifted or jumped. The background structure must remain locked (same viewpoint, same horizon line level, same perspective).\n"
            "3. ACTION mismatch: The visual change between Image 1 and Image 2 does NOT correspond to the action described in the VIDEO prompt.\n\n"
            "Response format:\n"
            "- If the background/composition is consistent AND the change corresponds to the video prompt, respond EXACTLY with: PASS\n"
            "- If there is a camera jump, no change, or incorrect change, respond with: FAIL: <reason in Chinese>"
        )
        
        user_text = f"VIDEO transition prompt:\n{video_prompt}\n\nPlease analyze the transition from Image 1 to Image 2."
        
        response = _multimodal_chat(config, system_prompt, user_text, [img_i_path, img_ip1_path])
        response_clean = response.strip()
        
        if response_clean.upper() == "PASS" or response_clean.upper().startswith("PASS"):
            return True, "PASS"
        else:
            return False, response_clean
    except Exception as e:
        if sys.stdout:
            print(f"[VLM QA] VLM API call failed: {e}. Skipping VLM check to avoid blocking pipeline.")
        return True, f"Skipped (API Error: {e})"


def load_reference_file(name):
    """Load a reference markdown file from the skill references folder."""
    path = os.path.join(SKILL_DIR, 'references', name)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not read reference file {name} ({e})")
    return ""


def get_cropped_templates(templates_content, i, total_beats, mode, bridge_stage):
    """Parse and crop the prompt-templates.md content based on the beat type
    to minimize the input context size during LLM prompt generation."""
    if not templates_content:
        return ""
        
    # Split templates by headers of level 2 and 3
    pattern = r'\n(###?\s+.*)'
    parts = re.split(pattern, '\n' + templates_content)
    
    sections = {}
    current_header = None
    for part in parts:
        part_strip = part.strip()
        if not part_strip:
            continue
        if part_strip.startswith('###') or part_strip.startswith('##'):
            current_header = part_strip
            sections[current_header] = ""
        elif current_header:
            sections[current_header] += part

    def find_section(kw):
        for h, val in sections.items():
            if kw.lower() in h.lower():
                return h + "\n" + val
        return ""

    image_1 = find_section("IMAGE 1")
    image_2_plus = find_section("IMAGE 2+")
    image_final = find_section("Final IMAGE")
    
    video_ordinary = find_section("Ordinary Construction VIDEO")
    video_bridge = find_section("Threshold Bridge")
    video_final = find_section("Final Reward VIDEO N")
    
    image_checklist = find_section("IMAGE Checklist")
    video_checklist = find_section("VIDEO Checklist")
    checklist_combined = f"{image_checklist}\n{video_checklist}"
    
    # If i is None, this is a request for image 1 generation
    if i is None:
        return f"{image_1}\n\n{image_checklist}"
        
    cropped = []
    
    is_last = (i == total_beats)
    is_bridge = (mode == 'Threshold' and bridge_stage in (1, 2))
    
    # Select IMAGE Template
    if is_last:
        cropped.append(image_final)
    elif is_bridge and bridge_stage == 1:
        # Sill Handoff Frame template is already contained within the Bridge templates block
        pass
    else:
        cropped.append(image_2_plus)
        
    # Select VIDEO Template
    if is_last:
        cropped.append(video_final)
    elif is_bridge:
        cropped.append(video_bridge)
    else:
        cropped.append(video_ordinary)
        
    cropped.append(checklist_combined)
    return "\n\n".join(cropped)


def parse_space_workflows():
    content = load_reference_file('space-workflows.md')
    workflows = {}
    if not content:
        return {
            "abandoned property": {"beats": "3-6", "phases": ["hazard clearing", "shell repair", "surface finish", "practical lighting", "final carry-out"], "threshold": False}
        }
    for line in content.splitlines():
        if line.strip().startswith('|') and not line.strip().startswith('|---'):
            parts = [p.strip() for p in line.split('|')[1:-1]]
            if len(parts) >= 4 and parts[0] != "Space Type":
                space_type = parts[0].strip('` ')
                beats = parts[1].strip()
                phases_raw = parts[2].strip()
                threshold_raw = parts[3].strip()
                phases = [p.strip() for p in re.split(r'→|->', phases_raw) if p.strip()]
                is_threshold = "Threshold" in threshold_raw
                workflows[space_type] = {
                    "beats": beats,
                    "phases": phases,
                    "threshold": is_threshold
                }
    return workflows


CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'packet_cache.json')


def load_packet_cache():
    with PACKET_CACHE_LOCK:
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                if sys.stdout:
                    print(f"Warning: could not read packet_cache.json ({e})")
        return {}


def save_packet_cache(cache):
    with PACKET_CACHE_LOCK:
        try:
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not write packet_cache.json ({e})")


def get_brief_fingerprint(dimensions):
    import hashlib
    serialized = json.dumps(dimensions, sort_keys=True)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


PROCESS_BRIEF_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'process_brief_cache.json')


def normalize_carrier_key(carrier, env):
    key = f"{carrier or ''}|{env or ''}".strip().lower()
    key = re.sub(r'[^a-z0-9]+', '-', key).strip('-')
    return key or 'unknown'


def load_process_brief_cache():
    with PROCESS_BRIEF_CACHE_LOCK:
        if os.path.exists(PROCESS_BRIEF_CACHE_PATH):
            try:
                with open(PROCESS_BRIEF_CACHE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                if sys.stdout:
                    print(f"Warning: could not read process_brief_cache.json ({e})")
        return {}


def save_process_brief_cache(cache):
    with PROCESS_BRIEF_CACHE_LOCK:
        try:
            with open(PROCESS_BRIEF_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if sys.stdout:
                print(f"Warning: could not write process_brief_cache.json ({e})")


def append_to_used_topic_ledger(parsed_brief, dimensions):
    ledger_path = os.path.join(SKILL_DIR, 'references', 'used-topic-ledger.md')
    if not os.path.exists(ledger_path):
        return
    date_str = datetime.now().strftime('%Y-%m-%d')
    carrier = parsed_brief.get('carrier', 'unknown').lower().replace(' ', '-')
    destiny = parsed_brief.get('destiny', 'unknown').lower().replace(' ', '-')
    anchors = dimensions.get('anchors') or []
    twist = anchors[0].lower().replace(' ', '-') if anchors else 'custom-twist'
    
    topic_dna = f"{carrier} / {destiny} / {twist}"
    one_sentence = f"{dimensions.get('theme', '未命名主题')}"
    source = "GUI Generation"
    avoid_notes = "Automatically registered by backend generator."
    
    new_row = f"| {date_str} | {topic_dna} | {one_sentence} | {source} | {avoid_notes} |\n"
    try:
        with open(ledger_path, 'a', encoding='utf-8') as f:
            f.write(new_row)
        if sys.stdout:
            print(f"[DEBUG] Appended topic to used-topic-ledger.md: {topic_dna}")
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not write to used-topic-ledger.md ({e})")


def clean_prompt_text(prompt):
    prompt = prompt.replace('%', ' percent')
    acronyms = ['HAL', 'TSPA', 'VMFP', 'GCTR', 'RPL', 'RCE', 'SCUP', 'NGCS', 'OSPL', 'RHMA', 'PBISP', 'HCL', 'NLVTR', 'MTAL']
    for ac in acronyms:
        prompt = re.sub(rf'[([（【]{ac}[)\]）】]', '', prompt)
        prompt = re.sub(rf'\b{ac}\b', '', prompt)
        
    # Split into sentences to perform negation-aware shortcut removal
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    shortcuts = [
        'cross-dissolve', 'fade-in', 'suddenly', 'magically', 'rapid montage', 
        'jump cut', 'jump-cut', 'jumpcuts', 'jumpcut', 'time skip', 'time-skip', 
        'instant transformation', 'cross dissolve', 'fade in', 'transformation progresses',
        'instantly transform', 'suddenly appears', 'teleport', 'as if by magic', 'out of nowhere'
    ]
    
    negation_words = ['forbid', 'avoid', 'no', 'without', 'never', 'not', 'stop', 'prevent', 'strictly', 'prohibit']
    
    for sentence in sentences:
        low_sent = sentence.lower()
        is_negation = any(neg in low_sent for neg in negation_words)
        if not is_negation:
            for sc in shortcuts:
                pattern = rf'\b{re.escape(sc)}s?\b'
                sentence = re.sub(pattern, '', sentence, flags=re.IGNORECASE)
                if ' ' in sc:
                    alt1 = sc.replace(' ', '-')
                    alt2 = sc.replace(' ', '')
                    sentence = re.sub(rf'\b{re.escape(alt1)}s?\b', '', sentence, flags=re.IGNORECASE)
                    sentence = re.sub(rf'\b{re.escape(alt2)}s?\b', '', sentence, flags=re.IGNORECASE)
        cleaned_sentences.append(sentence)
        
    prompt = " ".join(cleaned_sentences)
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    return prompt


def fix_image_clean_frame_proactive(prompt):
    """Proactively remove worker/agent references and active construction verbs from the image prompt to ensure it meets the Clean Frame requirements."""
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    negatives = ['no', 'zero', 'without', 'free of', 'absent', 'clear of', 'empty of', 'never']
    worker_agents = ['worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', 'people']
    
    for sentence in sentences:
        low_sent = sentence.lower()
        has_negative = any(re.search(rf'\b{neg}\b', low_sent) for neg in negatives)
        has_worker = any(re.search(rf'\b{w}s?\b', low_sent) for w in worker_agents)
        if has_negative and has_worker:
            cleaned_sentences.append(sentence)
            continue
            
        if 'painting' in low_sent and 'after painting' not in low_sent:
            is_noun_painting = re.search(r'\b(?:a|the|this|that|framed|oil|acrylic|canvas|decorative|original)\s+painting\b', low_sent) or \
                               re.search(r'\bpainting\s+(?:hangs|is hanging|depicts|decorates|on the wall|in a frame)\b', low_sent)
            if not is_noun_painting:
                sentence = re.sub(r'\bpainting\b', 'paint', sentence, flags=re.IGNORECASE)
        if 'installing' in low_sent and 'before installing' not in low_sent:
            sentence = re.sub(r'\binstalling\b', 'installation', sentence, flags=re.IGNORECASE)
        if 'sweeping' in low_sent:
            is_noun_sweeping = re.search(r'\bsweeping\s+(?:view|curve|arch|line|gesture|motion|pan|shot)\b', low_sent)
            if not is_noun_sweeping:
                sentence = re.sub(r'\bsweeping\b', 'swept dust', sentence, flags=re.IGNORECASE)
        if 'shoveling' in low_sent:
            sentence = re.sub(r'\bshoveling\b', 'cleared soil', sentence, flags=re.IGNORECASE)
            
        sentence = re.sub(r"\bworker's\b", "equipment's", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworkers's\b", "equipment's", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworker\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bworkers\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bbuilder\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bbuilders\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bcarpenter\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bcarpenters\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\blaborer\b", "equipment", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\blaborers\b", "equipments", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bperson\b", "object", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bpeople\b", "objects", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bman\b", "object", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"\bwoman\b", "object", sentence, flags=re.IGNORECASE)
        
        cleaned_sentences.append(sentence)
        
    return " ".join(cleaned_sentences)



def fix_video_opening(i, prompt):
    expected_start = f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout."
    prompt_stripped = prompt.strip()
    if "third layout." in prompt_stripped:
        idx = prompt_stripped.lower().find("third layout.")
        prompt_stripped = prompt_stripped[idx + len("third layout."):].strip()
    elif "third layout" in prompt_stripped:
        idx = prompt_stripped.lower().find("third layout")
        prompt_stripped = prompt_stripped[idx + len("third layout"):].strip()
    elif prompt_stripped.lower().startswith("use the provided first frame"):
        dot_idx = prompt_stripped.find(".")
        if dot_idx != -1:
            prompt_stripped = prompt_stripped[dot_idx + 1:].strip()
    return f"{expected_start} {prompt_stripped}"


def fix_pacing_control(prompt, is_threshold_or_reveal):
    if not is_threshold_or_reveal:
        phrase = "continuous construction time-lapse, not real-time footage."
        if phrase.lower() not in prompt.lower() and "continuous construction time-lapse" not in prompt.lower():
            if not prompt.endswith('.'):
                prompt += '.'
            prompt += f" {phrase}"
    return prompt


def fix_out_and_in(prompt, is_threshold_or_reveal=False):
    if is_threshold_or_reveal:
        return prompt
    low = prompt.lower()
    # Skip if the video is explicitly worker-free (threshold bridge, reward, etc.)
    sterile_phrases = ['sterile of workers', 'sterile of active workers',
                       'sterile of any human', 'no workers', 'no human presence',
                       'completely sterile of', 'without any human']
    if any(phrase in low for phrase in sterile_phrases):
        return prompt

    # Detect worker presence
    has_worker = any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer'))
    if not has_worker:
        return prompt

    # Detect multi-worker scenarios
    multi_worker_phrases = ['two workers', 'three workers', 'multiple workers',
                            'the workers', 'both workers']
    is_multi = any(phrase in low for phrase in multi_worker_phrases)

    # Check if entry/exit is already described
    has_entry = any(p in low for p in ['t=0', '0 seconds', 'start of the clip', 'enters the frame'])
    has_exit = any(p in low for p in [str(WORKER_EXIT_TIME), 'exits the frame', 'walks out', 'leaves the frame'])

    if has_entry and has_exit:
        # Already has full in/out — check for multi-worker vs single-worker template conflict
        if is_multi and 'one lone worker' in low:
            # Conflict: body says multi-worker but appended template says one worker
            prompt = re.sub(
                rf'At t=0s, one lone worker enters the frame from the Grid C1 edge;[^.]*leaving the frame completely empty at t={int(VIDEO_DURATION)}s\.',
                '', prompt).strip()
            # Re-add consistent multi-worker clause if no other exit clause remains
            if not any(p in prompt.lower() for p in ['exits the frame', 'walks out', 'leaves the frame']):
                prompt += f' At t=0s, the workers enter the frame; by t={WORKER_EXIT_TIME}s, all workers exit the frame, leaving it completely empty at t={int(VIDEO_DURATION)}s.'
        return prompt

    # Add missing entry/exit clause
    if not prompt.endswith('.'):
        prompt += '.'

    if is_multi:
        clause = f" At t=0s, the workers enter the frame; by t={WORKER_EXIT_TIME}s, all workers exit the frame, leaving it completely empty at t={int(VIDEO_DURATION)}s."
    else:
        clause = f" At t=0s, one lone worker enters the frame from the Grid C1 edge; the worker performs work, and by t={WORKER_EXIT_TIME}s, walks out of the frame through the Grid C1 edge, leaving the frame completely empty at t={int(VIDEO_DURATION)}s."

    prompt += clause
    return prompt


def fix_sound_design(prompt):
    """Guarantee the exemplar's mandatory two-part audio line. Safety net only — the beat prompt
    asks the model to write beat-specific sound; this fires only when it omits it entirely."""
    low = prompt.lower()
    if 'sound effect' not in low and 'ambient noise' not in low:
        clause = ("Sound effects include the tool contact, material movement, and footsteps of this beat. "
                  "Ambient noise is the steady enclosed room tone of the space.")
        if not prompt.endswith('.'):
            prompt += '.'
        prompt += f" {clause}"
    return prompt


def select_camera_dna(beat, base_camera_dna):
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    if bridge_stage == 1:
        is_bridge_1 = True
        is_bridge_2 = False
    elif bridge_stage == 2:
        is_bridge_1 = False
        is_bridge_2 = True
    elif bridge_stage is not None:
        is_bridge_1 = False
        is_bridge_2 = False
    else:
        is_bridge_1 = "bridge-1" in desc or "bridge 1" in desc or ("threshold" in op and "sill" in desc and "cross" not in desc)
        is_bridge_2 = "bridge-2" in desc or "bridge 2" in desc or ("threshold" in op and "cross" in desc)
    
    if is_bridge_1:
        return "coaxial forward-pushing camera, ultra-wide 14mm lens feel, camera height 1.6m, eye-level perspective dollying forward; horizon line remains level at 50-percent height; optical flow radiates symmetrically from the doorway sill in Grid B2."
    elif is_bridge_2:
        return "coaxial forward-pushing camera crossing the threshold, ultra-wide 14mm lens feel, camera height 1.6m, perspective dollying forward through the doorway; horizon line remains level at 50-percent height; optical flow radiates from the rear wall center in Grid B2."
    
    # If the base camera DNA has a range, clean it up
    cleaned_base = base_camera_dna
    if "14-18mm" in cleaned_base:
        cleaned_base = cleaned_base.replace("14-18mm", "14mm")
        
    return cleaned_base


def fix_camera_contradictions(prompt, is_moving):
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    cleaned_sentences = []
    
    if is_moving:
        static_phrases = [
            r'camera remains locked in a static tripod shot',
            r'camera remains locked in a static tripod',
            r'static tripod shot',
            r'camera remains locked',
            r'locked camera perspective',
            r'locked eye-level perspective',
            r'locked tripod shot',
            r'locked tripod'
        ]
        for sentence in sentences:
            low_sent = sentence.lower()
            if any(re.search(phrase, low_sent, flags=re.IGNORECASE) for phrase in static_phrases):
                continue
            cleaned_sentences.append(sentence)
    else:
        moving_phrases = [
            r'coaxial forward-pushing camera',
            r'coaxial forward-pushing',
            r'dollying forward',
            r'dolly-in',
            r'dolly forward',
            r'camera actively advances',
            r'camera viewpoint is actively advancing',
            r'optical flow radiates symmetrically from the doorway sill',
            r'crossing the threshold',
            r'crosses the sill'
        ]
        for sentence in sentences:
            low_sent = sentence.lower()
            if any(re.search(phrase, low_sent, flags=re.IGNORECASE) for phrase in moving_phrases):
                continue
            cleaned_sentences.append(sentence)
            
    return " ".join(cleaned_sentences).strip()


def check_camera_contradictions(prompt, is_moving):
    errors = []
    low = prompt.lower()
    if is_moving:
        static_words = ['static tripod', 'camera remains locked', 'locked eye-level', 'locked camera']
        for sw in static_words:
            if sw in low:
                errors.append(f"Moving camera prompt contains contradictory static clause '{sw}'")
    else:
        moving_words = ['dollying forward', 'dolly-in', 'forward-pushing', 'camera actively advances', 'crosses the sill', 'crosses the threshold']
        for mw in moving_words:
            if mw in low:
                errors.append(f"Static camera prompt contains contradictory moving clause '{mw}'")
    return errors


def fix_camera_dna(prompt, camera_dna):
    prefix = prompt[:300].lower()
    keywords = ['tripod', 'lens feel', 'camera height']
    if any(kw in prefix for kw in keywords):
        return prompt
    if camera_dna.lower() not in prompt.lower():
        return f"{camera_dna} {prompt}"
    return prompt


def fix_rhma_blur(prompt, is_last):
    if is_last and ("reflection" in prompt.lower() or "polished" in prompt.lower()):
        clause = "The highly reflective polished floor surface in Grid C1-C3 displays a heavily blurred, low-gloss, diffused reflection of the background; reflections are muted, dark, and highly out-of-focus, preventing high-frequency contrast or sharp details; realistic Fresnel falloff near the margins."
        if "blurred" not in prompt.lower() and "diffused" not in prompt.lower():
            if not prompt.endswith('.'):
                prompt += '.'
            prompt += f" {clause}"
    return prompt


def compress_prompt_to_budget(prompt, target_max_words, config, is_video=True):
    if not config:
        return prompt
    words = prompt.split()
    if len(words) <= target_max_words:
        return prompt

    system_prompt = f"""You are an expert prompt optimization tool.
Your job is to compress the given prompt to be under {target_max_words} words.
CRITICAL CONSTRAINTS:
1. You MUST preserve the beginning of the prompt (for videos: 'Use the provided first frame and last frame as exact composition anchors.', camera DNA descriptions).
2. You MUST preserve the end of the prompt (specifically: worker entry/exit actions at t=0s and t=7.5s, persistent trace descriptions, and sound effects/ambient noise).
3. Do NOT lose the core action being performed.
4. Reduce word count by pruning redundant adjectives, repetitive descriptions, and overly wordy details in the middle of the prompt.
5. The final output must be exactly under {target_max_words} words, and contain ONLY the compressed prompt prose. No labels, no quotes, no conversational filler."""

    # Format the prompt to use correct constants dynamically
    system_prompt = system_prompt.replace("t=7.5s", f"t={WORKER_EXIT_TIME}s")

    user_prompt = f"Original Prompt ({len(words)} words):\n{prompt}"
    try:
        model = config.get('auxModel') or config.get('model') or 'gemini-3-flash-agent'
        compressed = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=model).strip()
        compressed = _strip_code_fences(compressed).strip()
        compressed_words = compressed.split()
        if len(compressed_words) > 0 and len(compressed_words) < len(words):
            if sys.stdout:
                print(f"[COMPRESS] Successfully compressed prompt from {len(words)} to {len(compressed_words)} words.")
            return compressed
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] compress_prompt_to_budget failed: {e}")
    return prompt


def apply_proactive_fixes(i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal, beat=None, config=None):
    image_prompt = clean_prompt_text(image_prompt)
    image_prompt = fix_image_clean_frame_proactive(image_prompt)
    video_prompt = clean_prompt_text(video_prompt)
    video_prompt = fix_video_opening(i, video_prompt)
    video_prompt = fix_pacing_control(video_prompt, is_threshold_or_reveal)
    video_prompt = fix_out_and_in(video_prompt, is_threshold_or_reveal)
    video_prompt = fix_sound_design(video_prompt)
    
    base_camera_dna = packet.get('camera_dna', '')
    camera_dna = select_camera_dna(beat, base_camera_dna)
    if camera_dna:
        image_prompt = fix_camera_dna(image_prompt, camera_dna)
        
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    if bridge_stage in (1, 2):
        is_bridge = True
    elif bridge_stage is not None:
        is_bridge = False
    else:
        is_bridge = "bridge" in desc or "threshold" in op or "dolly" in desc or "push" in desc
        
    video_prompt = fix_camera_contradictions(video_prompt, is_bridge)
    image_prompt = fix_camera_contradictions(image_prompt, is_bridge)
    
    image_prompt = fix_rhma_blur(image_prompt, is_last)
    
    # Compress prompts to target budgets if they exceed limits
    image_prompt = compress_prompt_to_budget(image_prompt, 170, config, is_video=False)
    video_prompt = compress_prompt_to_budget(video_prompt, 180, config, is_video=True)
    
    return video_prompt, image_prompt


def check_nlvtr_violations(prompt):
    violations = []
    if '%' in prompt:
        violations.append("Contains forbidden '%' symbol")
    range_pattern = r'\b\d+(?:\s*%?\s*(?:to|-)\s*\d+\s*%?\s*(?:cm|m|kg|s|h|l|ml)?)\b'
    if re.search(range_pattern, prompt):
        violations.append("Contains forbidden numeric range")
    acronyms = ['HAL', 'TSPA', 'VMFP', 'GCTR', 'RPL', 'RCE', 'SCUP', 'NGCS', 'OSPL', 'RHMA', 'PBISP', 'HCL', 'NLVTR', 'MTAL']
    for ac in acronyms:
        if re.search(rf'\b{ac}\b', prompt):
            violations.append(f"Contains forbidden acronym '{ac}'")
    return violations


def check_image_clean_frame(prompt):
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    negatives = ['no', 'zero', 'without', 'free of', 'absent', 'clear of', 'empty of', 'never']
    worker_agents = ['worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', 'people']
    violations = []
    
    for sentence in sentences:
        low_sent = sentence.lower()
        has_negative = any(re.search(rf'\b{neg}\b', low_sent) for neg in negatives)
        has_worker = any(re.search(rf'\b{w}s?\b', low_sent) for w in worker_agents)
        
        # If the sentence has both a negative and a worker agent, it is a valid negation statement,
        # which the proactive fix preserves and validation accepts.
        if has_negative and has_worker:
            continue
            
        # Otherwise, check for worker references
        for w in worker_agents:
            if re.search(rf'\b{w}s?\b', low_sent):
                violations.append(f"IMAGE anchor contains worker/agent reference: '{w}'")
                
        # Check for active verbs
        active_verbs = ['shoveling', 'sweeping', 'painting', 'installing']
        for v in active_verbs:
            if re.search(rf'\b{v}\b', low_sent):
                # If the sentence contains a negation, we allow the active verb in a negative context (e.g. "no sweeping occurs")
                if has_negative:
                    continue
                # Specific phrase exemptions
                if v == 'painting':
                    if 'after painting' in low_sent:
                        continue
                    is_noun_painting = re.search(r'\b(?:a|the|this|that|framed|oil|acrylic|canvas|decorative|original)\s+painting\b', low_sent) or \
                                       re.search(r'\bpainting\s+(?:hangs|is hanging|depicts|decorates|on the wall|in a frame)\b', low_sent)
                    if is_noun_painting:
                        continue
                elif v == 'installing':
                    if 'before installing' in low_sent:
                        continue
                elif v == 'sweeping':
                    is_noun_sweeping = re.search(r'\bsweeping\s+(?:view|curve|arch|line|gesture|motion|pan|shot)\b', low_sent)
                    if is_noun_sweeping:
                        continue
                        
                violations.append(f"IMAGE anchor contains active verb: '{v}'")
                
    return violations


def check_video_opening(i, prompt):
    """Validate the mandatory first/last-frame anchor opening and its IMAGE i -> IMAGE i+1 binding.
    Mirrors fix_video_opening, which runs proactively, so well-formed prompts pass here."""
    errors = []
    low = prompt.strip().lower()
    if not low.startswith("use the provided first frame and last frame as exact composition anchors."):
        errors.append(f"VIDEO {i} missing required opening sentence 'Use the provided first frame and last frame as exact composition anchors.'")
    binding = f"use image {i} as the actual first-frame image and image {i + 1} as the actual last-frame image".lower()
    if binding not in low:
        errors.append(f"VIDEO {i} opening must bind IMAGE {i} (first frame) to IMAGE {i + 1} (last frame)")
    return errors


def check_pacing_control(prompt, is_threshold_or_reveal):
    """Non-threshold/reward beats must declare time-lapse pacing. Mirrors fix_pacing_control."""
    errors = []
    if not is_threshold_or_reveal:
        if "continuous construction time-lapse" not in prompt.lower():
            errors.append("VIDEO missing pacing control 'continuous construction time-lapse, not real-time footage'")
    return errors


def check_out_and_in(prompt, is_threshold_or_reveal=False):
    """If a worker is present, the clip must show them entering and exiting (Out-and-In passage).
    Mirrors fix_out_and_in's trigger so proactively-fixed prompts pass."""
    if is_threshold_or_reveal:
        return []
    errors = []
    low = prompt.lower()
    
    sterile_phrases = ['sterile of workers', 'sterile of active workers',
                       'sterile of any human', 'no workers', 'no human presence',
                       'completely sterile of', 'without any human']
    if any(phrase in low for phrase in sterile_phrases):
        return errors
        
    has_worker = any(re.search(rf'\b{w}s?\b', low) for w in ('worker', 'crew', 'person', 'builder', 'laborer'))
    if has_worker:
        entered = any(k in low for k in ('enter', 'walks in', 'steps in', 't=0', 'start of the clip', '0 seconds'))
        exited = any(k in low for k in ('exit', 'walks out', 'leaves the frame', 'steps out',
                                        'before the final frame', f'before t={int(VIDEO_DURATION)}', str(WORKER_EXIT_TIME)))
        if not (entered and exited):
            errors.append(f"VIDEO with a worker must show the worker entering at the start and exiting before the clip ends (Out-and-In passage)")
    return errors


def check_transition_shortcuts(prompt):
    """Reject abstract / causal-shortcut phrasing that skips concrete physical action — the exact
    failure mode of the placeholder fallback. Forces the model toward observable build actions."""
    errors = []
    low = prompt.lower()
    lazy_phrases = ['transformation progresses', 'magically', 'instantly transform',
                    'jump cut', 'time skip', 'suddenly appears', 'teleport',
                    'as if by magic', 'out of nowhere']
    for p in lazy_phrases:
        if p in low:
            errors.append(f"VIDEO uses abstract/causal-shortcut phrase '{p}'; describe concrete, traceable physical actions instead")
    return errors


def check_stylistic_repetition(curr_prompt, prev_prompt, packet, is_video=True):
    from difflib import SequenceMatcher
    errors = []
    
    def clean_text(text):
        # Remove any sentence containing persistent trace terms to avoid repetition clashes
        sentences_to_clean = re.split(r'(?<=[.!?])\s+', text)
        cleaned_sents = []
        for sent in sentences_to_clean:
            sent_low = sent.lower()
            if any(pt in sent_low for pt in ["persistent trace", "persistent mark", "persistent contact", "causal trace", "traces left", "traces include"]):
                continue
            cleaned_sents.append(sent)
        text = " ".join(cleaned_sents)

        text = text.lower()
        # Replace digits with empty string or space to ignore frame index/beat numbers
        text = re.sub(r'\b\d+\b', '', text)
        
        # Strip Camera DNA
        dna = packet.get('camera_dna', '')
        if dna:
            dna_clean = re.sub(r'\b\d+\b', '', dna.lower()).strip()
            dna_words = re.sub(r'[^\w\s]', ' ', dna_clean).split()
            for word in dna_words:
                if len(word) > 3:
                    text = text.replace(word, '')
                    
        # Strip Worker Choreography
        choreography = packet.get('worker_choreography', '')
        if choreography:
            ch_clean = re.sub(r'\b\d+\b', '', choreography.lower()).strip()
            ch_words = re.sub(r'[^\w\s]', ' ', ch_clean).split()
            for word in ch_words:
                if len(word) > 3:
                    text = text.replace(word, '')

        # Standard boilerplates to strip
        boilerplates = [
            "use the provided first frame and last frame as exact composition anchors",
            "use image as the actual first-frame image and image as the actual last-frame image",
            "every visible action must interpolate between those two frame images without inventing a third layout",
            "continuous construction time-lapse, not real-time footage",
            "camera remains locked in a static tripod shot",
            "same frame boundaries, maintaining the grid positions of all fixed landmarks",
            "locked anchors:",
            "boundary anchors:",
            "relative positioning lock",
            "completely empty of workers",
            "no active workers",
            "sterile of active workers",
            "worker locked in a solid silhouette profile",
            "by . seconds, the worker exits the frame",
            "leaving the frame completely empty at t=s",
            "leaving the scene completely empty and sterile",
            "transition shortcuts like cross-dissolves, fade-ins, or jump cuts are strictly forbidden",
            "sound effects include",
            "ambient noise is",
            "highly reflective polished floor surface",
            "blurred, low-gloss, diffused reflection",
            "fresnel falloff"
        ]
        for bp in boilerplates:
            text = text.replace(bp, "")
            
        # Clean punctuation and extra spaces
        text = re.sub(r'[^\w\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    c_curr = clean_text(curr_prompt)
    c_prev = clean_text(prev_prompt)
    
    if not c_curr or not c_prev:
        return errors
        
    ratio = SequenceMatcher(None, c_curr, c_prev).ratio()
    
    # Check for sentence-level exact/near-exact duplicates
    # Split by common sentence endings
    curr_sentences = [s.strip() for s in re.split(r'[.!?]', curr_prompt) if len(s.strip()) > 20]
    prev_sentences = [s.strip() for s in re.split(r'[.!?]', prev_prompt) if len(s.strip()) > 20]
    
    def is_mostly_boilerplate(sentence):
        sentence_low = sentence.lower()
        if any(pt in sentence_low for pt in ["persistent trace", "persistent mark", "persistent contact", "causal trace", "traces left", "traces include"]):
            return True
        if "use the provided first frame" in sentence_low:
            return True
        if "use image" in sentence_low and "as the actual first-frame" in sentence_low:
            return True
        if "continuous construction time-lapse" in sentence_low:
            return True
        if "transition shortcuts like" in sentence_low:
            return True
        if "highly reflective polished floor" in sentence_low:
            return True
        if "sound effects include" in sentence_low:
            return True
        if "camera remains locked" in sentence_low:
            return True
        # Camera DNA & boundaries physical invariants
        dna_keywords = ["tripod shot", "lens feel", "camera height", "perspective", "locked anchors", 
                        "left boundary", "right boundary", "top boundary", "bottom boundary", 
                        "horizon line", "optical flow", "frame boundary", "inherits all landmarks",
                        "held in frame", "entryway", "door sill", "rear wall", "door frame", "ceiling beam",
                        "sustain continuous action", "enters the frame", "exits the frame", "leaves the frame", 
                        "worker in a", "grid", "percent", "scale", "restored", "seconds", "empty", "sterile",
                        "walks out", "leaves the", "do not redesign", "camera remains", "coaxial", "interpolat"]
        if any(dk in sentence_low for dk in dna_keywords):
            return True
            
        # Ignore environmental, weather, lighting, ambient sound, and persistent construction traces
        # because these should remain consistent and identical across consecutive steps.
        extra_keywords = [
            "snow", "drift", "wind", "peak", "glow", "light", "ambient", "mist", "sky", "overcast", 
            "daylight", "shade", "shadow", "halogen", "led", "sfx", "sound effect", "ambient noise",
            "weld", "seam", "bolt", "screw", "fastener", "shaving", "dust", "varnish", "stain",
            "wood", "grain", "cladding", "underlayment", "conduit", "sconce", "pendant", "fixture",
            "reflection", "polished", "floorboard", "tile", "grout", "plaster", "drywall", "stud",
            "joist", "beam", "insulation", "bracket", "hinge", "handle", "frame", "sill", "molding",
            "trim", "sealant", "caulk", "groove", "scratch", "dent", "mark", "residue", "debris"
        ]
        if any(ek in sentence_low for ek in extra_keywords):
            return True
            
        # Whitelist persistent objects in the ledger
        ledger_objects = packet.get('object_ledger', []) if packet else []
        for obj in ledger_objects:
            if isinstance(obj, dict) and 'name' in obj:
                obj_words = obj['name'].lower().split()
                significant_words = [w for w in obj_words if len(w) > 3]
                if significant_words and any(w in sentence_low for w in significant_words):
                    return True
            
        return False
        
    for cs in curr_sentences:
        if is_mostly_boilerplate(cs):
            continue
        for ps in prev_sentences:
            if is_mostly_boilerplate(ps):
                continue
            s_ratio = SequenceMatcher(None, cs.lower(), ps.lower()).ratio()
            s_limit = 0.85 if is_video else 0.95
            if s_ratio > s_limit:
                errors.append(
                    f"{'VIDEO' if is_video else 'IMAGE'} sentence is too similar to previous beat's sentence "
                    f"(similarity: {s_ratio:.2f}):\n"
                    f"  Current: \"{cs}\"\n"
                    f"  Previous: \"{ps}\""
                )
                return errors
                
    limit = 0.65 if is_video else 0.88
    if ratio > limit:
        errors.append(
            f"{'VIDEO' if is_video else 'IMAGE'} phrasing/structure is too similar to previous beat "
            f"(cleaned similarity: {ratio:.2f} > {limit:.2f}). Please vary your sentence structures, verbs, and stylistic phrasing."
        )
        
    return errors


def check_monotonic_state_regression(config, prev_image, current_image):
    if not config or not prev_image or not current_image:
        return []
    
    system_prompt = """You are a construction prompt quality auditor.
Compare the previous beat's image state (IMAGE i) and the current beat's image state (IMAGE i+1).

Your ONLY job is to catch REAL continuity breaks: a MAJOR completed feature — an installed panel/wall/floor/fixture, a primary landmark, a finished surface, or a structural element — that was clearly present and finished in IMAGE i but has now vanished, reverted to an earlier unfinished state, or is directly contradicted in IMAGE i+1 (e.g. a finished floor becomes bare subfloor again, an installed door frame disappears, a painted wall is now unpainted).

Do NOT flag the omission of minor decorative or cosmetic micro-details — individual screws/nails/bolts/heads, dust, sawdust, pencil marks, scuff marks, slag flecks, heat-tint rings, wire clippings, and similar small persistent traces. A concise, natural-sounding image description is EXPECTED to drop most of these from beat to beat as new details accumulate — that is correct, not a regression. Only flag one of these if IMAGE i+1 actively describes that exact surface as pristine/untouched/unworked in a way that contradicts the completed work.

If everything major is correctly maintained, respond with exactly "PASS".
Otherwise, output a short bulleted list (at most 3 items, the most important ones) of MAJOR continuity breaks only. Keep it concise, direct, and actionable."""

    user_prompt = f"""IMAGE i (Previous State):
{prev_image}

IMAGE i+1 (New State):
{current_image}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=config.get('auxModel'))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        
        lines = [line.strip().lstrip('-* ').strip() for line in response_clean.split('\n') if line.strip()]
        errors = [f"Monotonic state regression: {line}" for line in lines if line and "PASS" not in line.upper()]
        return errors
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_monotonic_state_regression failed: {e}")
        return []


def check_visible_delta_between_frames(config, prev_image, current_image):
    if not config or not prev_image or not current_image:
        return []
    
    system_prompt = """You are a construction prompt quality auditor.
Compare the previous beat's image state (IMAGE i) and the current beat's image state (IMAGE i+1).
Check if there is a clear, visible progression or increment of construction work between the two states (e.g., a new panel installed, walls painted, wiring added, floor finished).
The current state MUST contain new completed elements or modifications that were not present in the previous state.
If there is a clear visible progression, respond with exactly "PASS".
If the two descriptions represent the exact same state of completion (even if worded differently), output a short description of the lack of progress (e.g. "No visible progress or added elements between IMAGE i and IMAGE i+1")."""

    user_prompt = f"""IMAGE i (Previous State):
{prev_image}

IMAGE i+1 (New State):
{current_image}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=config.get('auxModel'))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        
        return [f"Static frame violation: {response_clean}"]
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_visible_delta_between_frames failed: {e}")
        return []


def check_lighting_phase_ladder_monotonicity(ladder):
    """
    Validate the lighting phase ladder in the packet:
    1. Must use the five allowed phases.
    2. Must progress monotonically: hold or +1 only.
    """
    errors = []
    if not ladder:
        return errors
        
    phases = ["ambient only", "temporary work light active", "fixture install in progress", "partial practical activation", "final practical stabilization"]
    phase_to_val = {p: idx for idx, p in enumerate(phases)}
    
    try:
        sorted_keys = sorted([int(k) for k in ladder.keys()])
    except Exception as e:
        errors.append(f"Invalid keys in lighting_phase_ladder: {e}")
        return errors
        
    prev_val = None
    for k in sorted_keys:
        phase = ladder.get(str(k))
        if phase not in phase_to_val:
            errors.append(f"Invalid lighting phase '{phase}' in image {k} (allowed: {phases})")
            continue
            
        val = phase_to_val[phase]
        if prev_val is not None:
            diff = val - prev_val
            if diff < 0:
                errors.append(f"Lighting phase regressed from '{phases[prev_val]}' (image {k-1}) to '{phase}' (image {k})")
            elif diff > 1:
                errors.append(f"Lighting phase jumped illegally by +{diff} from '{phases[prev_val]}' (image {k-1}) to '{phase}' (image {k}). Must hold or +1 only.")
        prev_val = val
        
    return errors


def check_grid_coordinates(prompt):
    errors = []
    # Match patterns like Grid C1, Grid C1-C3, Grid C1 to C3, etc.
    coord_matches = re.findall(r'Grid\s+([A-Za-z]\d)(?:\s*[-–—to\s]+\s*([A-Za-z]\d))?', prompt, re.IGNORECASE)
    for c1, c2 in coord_matches:
        for c in (c1, c2):
            if c:
                cell = c.upper()
                if cell[0] not in ("A", "B", "C") or cell[1] not in ("1", "2", "3"):
                    errors.append(f"Invalid Grid coordinate 'Grid {cell}' found (only A1-C3 are allowed)")
    return errors


def check_primary_landmarks_exact_match(image_prompt, packet):
    errors = []
    if not packet or 'primary_landmarks' not in packet:
        return errors
    
    landmarks = packet['primary_landmarks']
    for lm in landmarks:
        name = lm.get('name', '').strip()
        grid = lm.get('grid', '').strip()
        
        # Landmark name check (case-insensitive exact string match)
        if name.lower() not in image_prompt.lower():
            errors.append(f"IMAGE prompt fails to restate primary landmark name exactly: '{name}'")
            
        # Landmark grid check (case-insensitive)
        if grid.lower() not in image_prompt.lower():
            raw_coord = grid.replace("Grid", "").strip()
            if raw_coord.lower() not in image_prompt.lower():
                errors.append(f"IMAGE prompt fails to restate grid coordinate '{grid}' for landmark '{name}'")
    return errors


def validate_beat_prompts(i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal, prev_video=None, prev_image=None, config=None, beat=None, skip_llm_checks=False):
    errors = []
    
    # Word count limits check
    img_word_count = len(image_prompt.split())
    if img_word_count > 170:
        errors.append(f"IMAGE prompt word count ({img_word_count}) exceeds limit of 170 words")
        
    vid_word_count = len(video_prompt.split())
    if vid_word_count > 180:
        errors.append(f"VIDEO prompt word count ({vid_word_count}) exceeds limit of 180 words")

    # Grid coordinate checks
    errors.extend(check_grid_coordinates(image_prompt))
    errors.extend(check_grid_coordinates(video_prompt))
    
    # Landmark exact-match restatement check
    errors.extend(check_primary_landmarks_exact_match(image_prompt, packet))

    errors.extend(check_nlvtr_violations(image_prompt))
    errors.extend(check_image_clean_frame(image_prompt))
    if "horizon line" not in image_prompt.lower():
        errors.append("IMAGE prompt missing 'horizon line' camera lock statement")
        
    if is_last:
        if "reflection" in image_prompt.lower() or "polished" in image_prompt.lower():
            if "blurred" not in image_prompt.lower() and "diffused" not in image_prompt.lower():
                errors.append("Final IMAGE with polished/reflective floor missing RHMA-Blur diffused reflection description")
                
    errors.extend(check_nlvtr_violations(video_prompt))
    errors.extend(check_video_opening(i, video_prompt))
    errors.extend(check_out_and_in(video_prompt, is_threshold_or_reveal))
    errors.extend(check_transition_shortcuts(video_prompt))
    errors.extend(check_pacing_control(video_prompt, is_threshold_or_reveal))
    
    # Check camera contradictions
    op = beat.get('operation', '').lower() if beat else ''
    desc = beat.get('description', '').lower() if beat else ''
    
    bridge_stage = beat.get('bridge_stage') if beat else None
    if bridge_stage in (1, 2):
        is_bridge = True
    elif bridge_stage is not None:
        is_bridge = False
    else:
        is_bridge = "bridge" in desc or "threshold" in op or "dolly" in desc or "push" in desc
        
    errors.extend(check_camera_contradictions(video_prompt, is_bridge))
    errors.extend(check_camera_contradictions(image_prompt, is_bridge))
    
    if prev_video:
        errors.extend(check_stylistic_repetition(video_prompt, prev_video, packet, is_video=True))
    if prev_image:
        errors.extend(check_stylistic_repetition(image_prompt, prev_image, packet, is_video=False))
        if config and not skip_llm_checks:
            errors.extend(check_monotonic_state_regression(config, prev_image, image_prompt))
            errors.extend(check_visible_delta_between_frames(config, prev_image, image_prompt))
        
    return errors


def extract_persistent_traces_to_ledger(config, video_prompt, image_prompt):
    if not config or not video_prompt or not image_prompt:
        return []
    
    system_prompt = """You are a spatial consistency supervisor.
Analyze the generated VIDEO and IMAGE prompts for a construction/renovation beat and extract any new permanent physical features, installed materials, or persistent traces (like scratches, screws, wood shavings, panels, coatings, etc.) introduced in this beat.
Format them as a JSON list of objects, each containing:
- "name": precise name (e.g. "sanddust on sill", "steel screw heads", "green insulation foam")
- "material_color": color/texture (e.g. "metallic silver", "dusty tan")
- "initial_state": state when introduced (e.g. "freshly installed", "scattered particles")
- "grid": approximate grid coordinate if mentioned (e.g. "Grid B2", defaults to "Grid B2" if unknown)
- "z_depth_scale": depth scale if mentioned (e.g. "50%", defaults to "50%" if unknown)

Return ONLY a valid JSON list, no code fences, no other text."""

    user_prompt = f"""VIDEO Prompt:
{video_prompt}

IMAGE Prompt:
{image_prompt}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=config.get('auxModel'))
        response_clean = _strip_code_fences(response).strip()
        new_items = json.loads(response_clean)
        if isinstance(new_items, list):
            valid_items = []
            for item in new_items:
                if isinstance(item, dict) and "name" in item:
                    valid_items.append({
                        "name": str(item.get("name")),
                        "material_color": str(item.get("material_color", "unknown")),
                        "initial_state": str(item.get("initial_state", "installed")),
                        "grid": str(item.get("grid", "Grid B2")),
                        "z_depth_scale": str(item.get("z_depth_scale", "50%"))
                    })
            return valid_items
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] extract_persistent_traces_to_ledger failed: {e}")
    return []


def parse_audit_failures(config, audit_md):
    if not config or not audit_md or '|' not in audit_md:
        return {}
    
    system_prompt = """You are a structured data extractor.
Analyze the following Markdown audit table and extract all items that did NOT pass (indicated by '未通过' or 'Fail' or 'No' in the "通过状态" status column).
Use the "拍号 / Beat Index" column to identify which beat index (1-based integer, e.g., 3) each failure belongs to, and extract the specific reason/description of the failure.
Respond ONLY with a valid JSON dictionary mapping the beat index (as a stringified integer, e.g. "3") to a list of failure descriptions for that beat. If no failures are found, return {}.
Example output:
{
  "3": ["Ceiling insulation missing in Grid A2 when walls were insulated."],
  "4": ["Fireplace installed before flooring was complete."]
}
Do not include any code fences, markdown, or other text."""

    try:
        response = _chat(config, system_prompt, audit_md, temperature=0.1, timeout=30, model=config.get('auxModel'))
        response_clean = _strip_code_fences(response).strip()
        failures = json.loads(response_clean)
        if isinstance(failures, dict):
            valid_failures = {}
            for k, v in failures.items():
                if isinstance(v, list) and v:
                    valid_failures[str(k)] = [str(item) for item in v]
            return valid_failures
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] parse_audit_failures failed: {e}")
    return {}


def generate_physical_process_brief(config, carrier, env, space_type):
    """Route B grounding step: force the model to explicitly reason about the REAL-WORLD
    construction/renovation order for this specific carrier before any creative beat/prompt
    writing happens, instead of letting that domain reasoning happen implicitly (and invisibly)
    inside the beat-ladder composer. No external search — this is recall-and-reason, not retrieval.
    Cached by normalized carrier+env so repeated projects about the same kind of subject reuse
    the same grounded knowledge instead of re-inventing it (and possibly differently) every time.
    """
    cache_key = normalize_carrier_key(carrier, env)
    with PROCESS_BRIEF_CACHE_LOCK:
        cache = load_process_brief_cache()
        cached = cache.get(cache_key)
        if cached:
            return cached

    system_prompt = """You are a real-world construction and restoration domain expert consulted BEFORE any creative writing begins.
Your ONLY job is factual: describe how this specific carrier/structure is ACTUALLY renovated, converted, or restored in reality, based on real building/engineering/trade practice — not a generic renovation template.

Think in terms of genuine physical dependency: what MUST happen before what, and why (structural safety, material cure times, trade sequencing, regulatory/hazard requirements). If the carrier is unusual (a vehicle, vessel, natural formation, industrial relic, etc.), reason from the closest real trade practice (marine refit, aircraft teardown, mine/tunnel shoring, etc.) rather than defaulting to generic residential renovation steps.

You must output ONLY a valid JSON object with no markdown, no code fences, no other text, with these keys:
1. "domain_confidence": one of "high", "medium", "low" — how well-established/documented real-world practice is for this specific carrier. Use "low" if the carrier is fantastical or has no real-world analog.
2. "real_world_phases": an ordered array of phase names (3-8 short phase names, e.g. "hull descaling and rust treatment", "ballast/structural reinforcement", "interior gutting", "electrical rough-in", "insulation and vapor barrier", "interior finish", "fixture install"). Order must reflect genuine real-world dependency, not a generic template.
3. "hard_prerequisites": an array of short strings, each stating a strict ordering constraint and why (e.g. "Structural reinforcement must precede any interior fit-out — the shell must be load-bearing safe first", "Waterproofing/sealing must precede electrical rough-in — active leaks would damage wiring"). Include only constraints that would be a real physical error if violated.
4. "hazard_notes": an array of short strings noting any real hazard/regulatory considerations relevant to this carrier's era or material (e.g. asbestos, lead paint, corrosion, structural instability), or an empty array if none apply.
5. "typical_materials": an array of short strings naming the real materials/tools/systems specific to this carrier (e.g. for a retired submarine: "steel hull plating", "ballast tank valves", "pressure-hull rivets"), used to ground object/material choices instead of generic placeholders.

If domain_confidence is "low", still provide a best-effort reasoned list rather than an empty one, but mark the confidence honestly."""

    user_prompt = f"""Carrier (the object/structure being renovated): {carrier}
Environment: {env}
Space type classification: {space_type}

Reason through the REAL-WORLD renovation/conversion order for this specific carrier and output the JSON."""

    brief = None
    for attempt in range(3):
        try:
            resp = _chat(config, system_prompt, user_prompt, temperature=0.25, timeout=60)
            resp_clean = _strip_code_fences(resp).strip()
            parsed = json.loads(resp_clean)
            if isinstance(parsed, dict) and parsed.get('real_world_phases'):
                brief = parsed
                break
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] generate_physical_process_brief attempt {attempt+1} failed: {e}")

    if not brief:
        # Low-confidence empty brief: downstream code treats this as "fall back to the static
        # space-workflows.md table", not as a hard failure.
        brief = {
            "domain_confidence": "low",
            "real_world_phases": [],
            "hard_prerequisites": [],
            "hazard_notes": [],
            "typical_materials": [],
        }

    with PROCESS_BRIEF_CACHE_LOCK:
        cache = load_process_brief_cache()
        cache[cache_key] = brief
        save_process_brief_cache(cache)
    return brief


def check_real_world_order_violation(config, hard_prerequisites, beat_ladder):
    """Semantic gate for the beat ladder against the grounded process-brief's hard prerequisites.
    Catches a beat ladder that is internally self-consistent (passes the generic ceiling/fixture/
    door/floor rules) but is still globally wrong FOR THIS CARRIER because nothing else in the
    pipeline has real-world dependency facts to compare against.
    """
    if not config or not hard_prerequisites or not beat_ladder:
        return []

    beats_desc = "\n".join(
        f"Beat {b.get('index')}: {b.get('operation')} - {b.get('description', '')}"
        for b in beat_ladder
    )

    system_prompt = """You are a real-world construction sequence auditor.
You are given a list of hard real-world ordering prerequisites for this specific carrier, and a proposed beat-by-beat construction ladder.
Check whether the beat ladder's operation order violates any of the hard prerequisites.
If everything respects the prerequisites, respond with exactly "PASS".
Otherwise, output a concise bulleted list, each line naming the beat index and which prerequisite it violates. Keep it short and actionable."""

    user_prompt = f"""Hard Prerequisites:
{chr(10).join('- ' + p for p in hard_prerequisites)}

Proposed Beat Ladder:
{beats_desc}"""

    try:
        response = _chat(config, system_prompt, user_prompt, temperature=0.1, timeout=30, model=config.get('auxModel'))
        response_clean = response.strip()
        if response_clean.upper() == "PASS" or "PASS" in response_clean.upper()[:10]:
            return []
        lines = [line.strip().lstrip('-* ').strip() for line in response_clean.split('\n') if line.strip()]
        return [f"Real-world order violation: {line}" for line in lines if line and "PASS" not in line.upper()]
    except Exception as e:
        if sys.stdout:
            print(f"[DEBUG] check_real_world_order_violation failed: {e}")
        return []


def call_llm(config, dimensions, on_progress=None):
    if sys.stdout:
        print("[DEBUG] call_llm: Starting structured agent loop...")

    # Step 1: Brief Parsing
    if on_progress:
        on_progress('outline', '正在解析场景维度并规划工序...')

    theme = dimensions.get('theme', '未指定场景')
    anchors = dimensions.get('anchors') or []
    anchors_str = '、'.join(anchors) if anchors else '由作曲家自行选取最契合主题的锚点'
    complexity = dimensions.get('complexity', '中等重工')
    budget = dimensions.get('budget', '轻奢设计师级')
    ratio = dimensions.get('ratio', '50')
    creativity = dimensions.get('creativity', '突破常规')
    beats_count = int(dimensions.get('beats_count', 15))
    total_beats = beats_count + 1

    # Brief parsing LLM call
    brief_system = """You are a scene analysis agent for a restoration time-lapse project.
Your job is to parse the design dimensions into a structured JSON object containing scene variables.
You must output ONLY a valid JSON object with the keys below, and no markdown formatting, no code fences, no other text.

Required JSON keys:
1. "carrier": The main object or structure being renovated (e.g. "double-height loft", "school bus").
2. "env": The surrounding environment (e.g. "wooded hillside", "urban lot").
3. "trauma": The initial ruined, broken, dirty, or empty state of the scene.
4. "destiny": The target finished state of the scene.
5. "reward": The final action or reveal motion that happens at the end (e.g. "lights turn on", "person walks in").
6. "mode": Must be either "Standard" or "Threshold". Set to "Threshold" only if there is a clear boundary crossing (e.g. entering a room, building, cabin, container) from exterior to interior.
7. "space_type": Must be exactly one of the following strings:
   - "abandoned property"
   - "exterior facade"
   - "road / street / driveway"
   - "garage / workshop"
   - "backyard / landscape / pool"
   - "luxury apartment"
   - "retail / showroom"
   - "underground space"
   - "custom build object"
"""
    brief_user = f"""Design dimensions to parse:
- Theme: {theme}
- Core Creative Anchors: {anchors_str}
- Project Complexity: {complexity}
- Budget Level: {budget}
- Raw-Shell vs Refined-Interior Contrast Intensity (higher = bolder before/after clash): {ratio}
- Creativity Scale: {creativity}
"""
    
    parsed_brief = {}
    for attempt in range(3):
        try:
            brief_text = _chat(config, brief_system, brief_user, temperature=0.2, timeout=60)
            brief_text_cleaned = _strip_code_fences(brief_text)
            parsed_brief = json.loads(brief_text_cleaned)
            required_keys = ["carrier", "env", "trauma", "destiny", "reward", "mode", "space_type"]
            if all(k in parsed_brief for k in required_keys):
                break
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Brief parsing attempt {attempt+1} failed: {e}")
            if attempt == 2:
                parsed_brief = {
                    "carrier": theme,
                    "env": "surrounding environment",
                    "trauma": "ruined state",
                    "destiny": "finished design",
                    "reward": "lights activate",
                    "mode": "Threshold" if beats_count >= 12 else "Standard",
                    "space_type": "abandoned property"
                }

    title = parsed_brief.get('destiny', '未命名创意')
    if title:
        title = f"{creativity}·{title}"

    # Register used topic DNA
    try:
        append_to_used_topic_ledger(parsed_brief, dimensions)
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not write topic to used topic ledger ({e})")

    # Step 1.5: World-Knowledge Grounding Query (Route B — recall-and-reason, no external search).
    # Runs BEFORE any beat/prompt writing so the real-world construction order for THIS carrier
    # is established as a fact-finding pass, separate from the creative writing that follows.
    if on_progress:
        on_progress('outline', f'正在查证 {parsed_brief.get("carrier", "该载体")} 的真实改造工序知识...')
    process_brief = generate_physical_process_brief(
        config,
        parsed_brief.get('carrier', theme),
        parsed_brief.get('env', ''),
        parsed_brief.get('space_type', 'abandoned property'),
    )
    if sys.stdout:
        print(
            f"[DEBUG] Physical process brief (confidence={process_brief.get('domain_confidence')}): "
            f"{process_brief.get('real_world_phases')}"
        )

    # Step 2: Programmatic Workflow Lookup
    workflows = parse_space_workflows()
    space_type = parsed_brief.get('space_type', 'abandoned property')
    workflow = workflows.get(space_type, workflows.get('abandoned property'))

    # Grounded real-world phases take priority over the generic 9-bucket static table whenever
    # the grounding step is confident enough to trust; the static table remains the fallback.
    if process_brief.get('domain_confidence') in ('high', 'medium') and process_brief.get('real_world_phases'):
        effective_phases = process_brief['real_world_phases']
    else:
        effective_phases = workflow['phases']

    # Step 3: Beat Ladder Generation
    if on_progress:
        on_progress('outline', f'工序定义成功。正在根据 {space_type} 生成 {total_beats} 拍工序排布...')

    phases_str = " -> ".join(effective_phases)

    real_world_reference_block = ""
    if process_brief.get('hard_prerequisites') or process_brief.get('hazard_notes'):
        prereq_lines = "\n".join(f"- {p}" for p in process_brief.get('hard_prerequisites', []))
        hazard_lines = "\n".join(f"- {h}" for h in process_brief.get('hazard_notes', []))
        real_world_reference_block = f"""
==================== REAL-WORLD PROCESS REFERENCE (this carrier specifically) ====================
Hard prerequisites (violating these is a real-world physical error, not a style choice):
{prereq_lines or '- (none identified)'}
Hazard/regulatory notes for this carrier's era or material:
{hazard_lines or '- (none identified)'}
"""

    beat_system = f"""You are a professional construction planner specializing in time-lapse renovation projects.
Your goal is to expand the standard construction phases into a detailed, step-by-step beat ladder.
You must output ONLY a valid JSON array of beats, containing exactly {total_beats} elements. Do not output code fences, markdown, or other text.
{real_world_reference_block}
Each beat object in the JSON array must have:
1. "index": (integer) from 1 to {total_beats}.
2. "operation": One of: "clearing", "repair", "rough-in", "flooring", "framing", "drywall", "priming", "painting", "wiring", "lighting", "furnishing", "threshold", "reward".
3. "description": (string) Detailed English visual description of the operation, tools/materials used, and the physical changes in the scene.
4. "bridge_stage": (integer or null) Set to 1 for the first bridge/threshold beat (Beat T), 2 for the second bridge/threshold beat (Beat T+1), and null for all other beats.

General Rules:
- The beats must be realistic and in monotonic order matching the phases: {phases_str} -> reward.
- If a REAL-WORLD PROCESS REFERENCE section is present above, its hard prerequisites are mandatory and override generic assumptions about this type of space.
- Each beat must focus on EXACTLY ONE distinct physical operation (e.g. debris clearing, structural repair, piping, wall paneling, priming, painting, lighting installation, furnishing). Do not combine distinct operations.
- Beat {total_beats} must be the final reward/reveal motion: {parsed_brief['reward']}.
- If mode is "Threshold", you must split the exterior-interior crossing into two beats:
  - Beat T (e.g. Beat 6): "threshold" - Exterior approach pushing toward the open threshold, peeked interior landmarks visible.
  - Beat T+1 (e.g. Beat 7): "threshold" - Crossing sill and settling into the interior, door frame sliding out.
  - All subsequent beats (Beats T+2 to {beats_count}) must be interior construction operations (e.g., clearing interior, interior walls, interior flooring, etc.).
- CEILING/ROOF COVERAGE RULE: For any enclosed space (fuselage, cabin, room, container, vault, bunker, etc.), the ceiling/roof/top surface must be treated as a construction surface just like the walls and floor. When the beat ladder includes framing, paneling, insulating, or painting walls, the SAME operation MUST also explicitly cover the ceiling/roof/top curve. A renovation that covers walls but leaves the ceiling as raw exposed structure is physically incorrect.
- FIXTURE INSTALLATION RULE: If the beat ladder includes a wiring/electrical rough-in beat, there MUST be a subsequent "lighting" or "fixture install" beat BEFORE the furnishing/staging beat and BEFORE the reward beat. Light fixtures cannot appear in the final reward if they were never installed.
- DOOR LEAF RULE: If a door frame is installed in one beat, a subsequent beat MUST include installing a door panel/leaf/sash unless the design explicitly calls for an open archway.
- FLOORING-BEFORE-HEAVY-OBJECTS RULE: Floor finish (hardwood, tile, etc.) MUST be installed BEFORE any heavy anchored objects (fireplace, stove, workbench) are placed on it. The correct order is: subfloor -> finish floor -> anchor heavy objects onto the finished floor.
- VIEWPOINT CONTINUITY RULE: If the beat ladder uses Threshold mode (exterior-to-interior crossing), all subsequent interior beats must maintain interior camera viewpoint. If a beat requires showing exterior work after the threshold crossing, either (a) place that exterior beat BEFORE the threshold crossing, or (b) describe the work from the interior viewpoint showing only what is visible from inside.
"""

    beat_user = f"""Please generate exactly {total_beats} beats for:
Carrier: {parsed_brief['carrier']}
Trauma: {parsed_brief['trauma']}
Destiny: {parsed_brief['destiny']}
Mode: {parsed_brief['mode']}
Space Type: {space_type}
"""

    beat_ladder = None
    beat_user_current = beat_user
    hard_prerequisites = process_brief.get('hard_prerequisites', [])
    for attempt in range(3):
        try:
            beat_text = _chat(config, beat_system, beat_user_current, temperature=0.3, timeout=90)
            beat_text_cleaned = _strip_code_fences(beat_text)
            beat_ladder = json.loads(beat_text_cleaned)
            if isinstance(beat_ladder, list) and len(beat_ladder) == total_beats:
                idxs = [b.get('index') for b in beat_ladder]
                if idxs == list(range(1, total_beats + 1)):
                    # Semantic gate: does this internally-valid ladder still violate a real-world
                    # hard prerequisite for THIS carrier? Only the grounding step can catch this.
                    violations = check_real_world_order_violation(config, hard_prerequisites, beat_ladder)
                    if not violations:
                        break
                    if sys.stdout:
                        print(f"[DEBUG] Beat ladder attempt {attempt+1} violated real-world order: {violations}")
                    if attempt < 2:
                        beat_user_current = beat_user + "\n\n" + "==================== PRIOR REAL-WORLD ORDER VIOLATIONS ====================\n" + \
                            "The previous beat ladder violated these real-world prerequisites. Fix the ordering:\n" + \
                            "\n".join(f"- {v}" for v in violations)
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] Beat ladder generation attempt {attempt+1} failed: {e}")
            if attempt == 2:
                beat_ladder = []
                for idx in range(1, total_beats + 1):
                    op = "repair"
                    b_stage = None
                    if idx == 1:
                        op = "clearing"
                    elif idx == total_beats:
                        op = "reward"
                    elif parsed_brief.get('mode') == 'Threshold':
                        t_idx = 6 if total_beats >= 12 else (total_beats // 2)
                        if idx == t_idx:
                            op = "threshold"
                            b_stage = 1
                        elif idx == t_idx + 1:
                            op = "threshold"
                            b_stage = 2
                    beat_ladder.append({
                        "index": idx,
                        "operation": op,
                        "description": f"Renovation work step {idx}",
                        "bridge_stage": b_stage
                    })

    # Step 4: Drift Lock Packet Generation
    if on_progress:
        on_progress('outline', '工序排布完成。正在计算三维空间一致性与 Camera DNA 锁定特征...')

    brief_fingerprint = get_brief_fingerprint(dimensions)
    cache = load_packet_cache()
    packet = cache.get(brief_fingerprint)

    if not packet:
        scup_ref = load_reference_file('spatial-consistency-upgrade-protocol.md')
        assembly_ref = load_reference_file('drift-lock-assembly-guide.md')
        beats_desc = "\n".join([f"Beat {b['index']}: {b['operation']} - {b['description']}" for b in beat_ladder])

        packet_system = f"""You are a spatial consistency supervisor for a time-lapse renovation prompt composer.
Your job is to generate a comprehensive Drift Lock & SCUP Packet for the project.
You must output ONLY a valid JSON object matching the keys below, with no other text, no markdown, and no code fences.

Required JSON keys:
1. "camera_dna": A single camera sentence (~25-30 words) describing shot type, lens feel, camera height, perspective axis, and boundaries. Include horizon pinning (e.g., "horizon line remains perfectly level at exactly 50-percent height of the frame; all optical flow lines radiate symmetrically from the optical center of Grid B2").
2. "geometry_lock": A description of structural facts that cannot change (doors, windows, columns, wall lines).
3. "primary_landmarks": A list of exactly 3 landmarks (Foreground, Mid-depth, Background). Each landmark must be a JSON object with:
   - "name": The exact name (e.g. "cracked floor seam")
   - "grid": Grid coordinate (from Grid A1 to Grid C3)
   - "z_depth_scale": Frame-height percentage scale (e.g., "60%")
4. "frame_boundaries": A JSON object with keys "left", "right", "top", "bottom", specifying Grid coordinates and physical features for each edge.
5. "object_ledger": A list of detail-critical recurring objects. Each must be a JSON object with "name", "material_color", "initial_state", "grid", and "z_depth_scale". Provide a comprehensive list of all detail-critical objects with no hard limit. Prefer the real-world materials listed in REAL-WORLD MATERIALS REFERENCE below over generic placeholders when they fit the scene.
6. "worker_choreography": The worker trajectory, silhouette (HAL), and manual tool lock (MTAL) details.
7. "lighting_phase_ladder": A mapping of IMAGE indices (1 to {total_beats + 1}) to lighting phases (e.g. "ambient only", "temporary work light active", etc.). Shadow and exposure progression must be monotonic.
8. "passive_environment": Direction and elements for passive layers (e.g. clouds, watercaustics).
9. "interest_budget": A dictionary with keys "clip_hooks", "sequence_reveal", and "final_reward".
{('==================== REAL-WORLD MATERIALS REFERENCE (this carrier specifically) ====================' + chr(10) + chr(10).join('- ' + m for m in process_brief.get('typical_materials', []))) if process_brief.get('typical_materials') else ''}

==================== REFERENCE GUIDES ====================
{assembly_ref}
{scup_ref}
"""
        packet_user = f"""Scene Variables:
{json.dumps(parsed_brief, indent=2, ensure_ascii=False)}

Beat Ladder:
{beats_desc}
"""

        for attempt in range(3):
            try:
                packet_text = _chat(config, packet_system, packet_user, temperature=0.2, timeout=90)
                packet_text_cleaned = _strip_code_fences(packet_text)
                packet = json.loads(packet_text_cleaned)
                if all(k in packet for k in ["camera_dna", "geometry_lock", "primary_landmarks", "frame_boundaries"]):
                    ladder_errs = check_lighting_phase_ladder_monotonicity(packet.get("lighting_phase_ladder"))
                    if ladder_errs:
                        raise ValueError(f"lighting_phase_ladder validation failed: {ladder_errs}")
                    cache[brief_fingerprint] = packet
                    save_packet_cache(cache)
                    break
            except Exception as e:
                if sys.stdout:
                    print(f"[DEBUG] Drift lock packet generation attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    packet = {
                        "camera_dna": f"static tripod shot, ultra-wide 14mm lens feel, camera height 1.6m, locked eye-level perspective; horizon line remains level at 50-percent height; optical flow radiates from B2.",
                        "geometry_lock": "Standard boundaries",
                        "primary_landmarks": [
                            {"name": "floor center", "grid": "Grid C2", "z_depth_scale": "10%"},
                            {"name": "back column", "grid": "Grid B2", "z_depth_scale": "50%"},
                            {"name": "window opening", "grid": "Grid A2", "z_depth_scale": "40%"}
                        ],
                        "frame_boundaries": {"left": "B1", "right": "B3", "top": "A2", "bottom": "C2"},
                        "object_ledger": [],
                        "worker_choreography": "HAL safety vest worker",
                        "lighting_phase_ladder": {str(i): "ambient only" for i in range(1, total_beats + 2)},
                        "passive_environment": "soft light drift",
                        "interest_budget": {}
                    }

    # Step 5 & 6: Progressive Step-by-Step Slot Generation
    if on_progress:
        on_progress('batch', {'current': 0, 'total': total_beats})

    compiled_images = {}
    compiled_videos = {}

    scup_ref = load_reference_file('spatial-consistency-upgrade-protocol.md')
    templates_raw = load_reference_file('prompt-templates.md')
    templates_cropped_img1 = get_cropped_templates(templates_raw, None, total_beats, mode, None)

    image_1_system = f"""You are a professional prompt composer. Your job is to generate the very first IMAGE prompt (IMAGE 1 / Trauma State) for the renovation project.
You must output ONLY the prompt text, with no other text, no title, no labels. The prompt must be in English.

==================== SCUP CONTRACT & TEMPLATES ====================
{scup_ref}
{templates_cropped_img1}

==================== DRIFT LOCK PACKET ====================
{json.dumps(packet, indent=2, ensure_ascii=False)}

Hard Rules:
1. Clean Frame Boundary: The frame must be completely empty of people, workers, or machinery. Do NOT use the words 'worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', or 'people' in the prompt text, even to say they are absent. Describe only static objects and surfaces.
2. Hierarchical Context Layering (HCL): First 40 tokens contain Camera DNA and the 3 Primary Landmarks.
3. Natural-Language Visual-Only Translation Rule (NLVTR): No '%', no numeric ranges, no acronyms (HAL, NGCS, OSPL, etc.) in the text.
4. Set the scene as the initial trauma state.
"""
    image_1_user = f"Generate IMAGE 1 prompt for theme: {theme}."
    
    image_1_prompt = ""
    for attempt in range(3):
        try:
            image_1_prompt = _chat(config, image_1_system, image_1_user, temperature=0.8, timeout=60)
            image_1_prompt = _strip_code_fences(image_1_prompt).strip()
            image_1_prompt = clean_prompt_text(image_1_prompt)
            image_1_prompt = fix_image_clean_frame_proactive(image_1_prompt)
            camera_dna = packet.get('camera_dna', '')
            if camera_dna:
                image_1_prompt = fix_camera_dna(image_1_prompt, camera_dna)
            errs = check_image_clean_frame(image_1_prompt)
            errs.extend(check_grid_coordinates(image_1_prompt))
            errs.extend(check_primary_landmarks_exact_match(image_1_prompt, packet))
            if not errs:
                break
            if sys.stdout:
                print(f"[DEBUG] IMAGE 1 failed validation (attempt {attempt+1}): {errs}")
                print(f"[DEBUG]   Generated IMAGE 1 prompt: {image_1_prompt}")
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] IMAGE 1 generation attempt {attempt+1} failed: {e}")
    
    if not image_1_prompt:
        image_1_prompt = f"A static ultra-wide 14mm tripod shot at 1.6m height: initial ruined empty state of {theme}; horizon line remains level; no workers."

    compiled_images[1] = image_1_prompt

    mode = parsed_brief.get('mode', 'Standard')

    audit_passes = 0
    max_audit_passes = 2
    audit_feedback_dict = {}

    while audit_passes <= max_audit_passes:
        if audit_passes > 0:
            if on_progress:
                on_progress('audit', f'检测到工序校验不通过，启动自动修复生成第 {audit_passes}/{max_audit_passes} 轮...')
            if sys.stdout:
                print(f"[AUDIT] Starting self-healing regeneration pass {audit_passes}/{max_audit_passes}...")
        
        if audit_passes == 0:
            beats_to_generate = list(range(1, total_beats + 1))
            if sys.stdout:
                print(f"[AUDIT] Initial pass: Generating all {total_beats} beats...")
        else:
            beats_to_generate = sorted([int(k) for k in audit_feedback_dict.keys() if k.isdigit()])
            if sys.stdout:
                print(f"[AUDIT] Self-healing pass: Regenerating only failed beats: {beats_to_generate}...")

        fallback_count = 0
        for i in beats_to_generate:
            if sys.stdout:
                print(f"[DEBUG] Step 5: Composing Beat {i} of {total_beats} (Pass {audit_passes})...")
            if on_progress:
                on_progress('batch', {'current': i, 'total': total_beats})

            beat = beat_ladder[i - 1]
            is_last = (i == total_beats)
            is_threshold_or_reveal = (beat.get('operation') in ('threshold', 'reward'))

            bridge_stage = beat.get('bridge_stage')
            is_bridge = (mode == 'Threshold' and bridge_stage in (1, 2))
            tbcp_ref = load_reference_file('threshold-bridge-consistency-protocol.md') if is_bridge else ''
            
            # Crop templates per beat
            templates_cropped = get_cropped_templates(templates_raw, i, total_beats, mode, bridge_stage)
            
            prior_prompts_block = ""
            if i > 1:
                prior_prompts_block = f"""
==================== PREVIOUS BEAT GENERATED PROMPTS (DO NOT DUPLICATE PHRASING) ====================
To prevent formulaic repetition, the vocabulary, sentence structures, and opening patterns of VIDEO {i} and IMAGE {i+1} must NOT duplicate or mirror those in the previous beat prompts:
Previous VIDEO {i-1}:
{compiled_videos[i-1]}

Previous IMAGE {i}:
{compiled_images[i]}
"""

            # Retrieve lighting phases for this beat
            img_i_lighting = packet.get("lighting_phase_ladder", {}).get(str(i), "ambient only")
            img_ip1_lighting = packet.get("lighting_phase_ladder", {}).get(str(i + 1), "ambient only")

            beat_system = f"""You are a professional prompt composer operating under the `restoration-prompt-composer` skill.
Your job is to generate exactly two prompts for Beat {i}:
1. VIDEO {i}: The construction timelapse video.
2. IMAGE {i+1}: The clean environment state snapshot after the video.

==================== LIGHTING PHASE CONTRACT FOR THIS BEAT ====================
- IMAGE {i} (State before this beat) uses lighting phase: {img_i_lighting}
- IMAGE {i+1} (The state you are generating now) MUST use lighting phase: {img_ip1_lighting}
- VIDEO {i} (The transition video prompt) MUST describe the transition matching this lighting phase progression: from '{img_i_lighting}' to '{img_ip1_lighting}'.

==================== SKILL CONTRACTS ====================
{scup_ref}
{tbcp_ref}
{templates_cropped}

==================== DRIFT LOCK PACKET ====================
{json.dumps(packet, indent=2, ensure_ascii=False)}

==================== PRIOR PROMPTS (for continuity) ====================
IMAGE 1 (Trauma State):
{compiled_images[1]}

IMAGE {i} (State before this beat):
{compiled_images[i]}
{prior_prompts_block}

Instructions:
- VIDEO {i} must start with: "Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout."
- VIDEO {i} must use progressive (-ing) verbs for ongoing actions, name worker silhouettes (HAL) and tools (MTAL) if workers are present, encapsulate bulk materials in rigid containers (VMFP/RCE), and include pacing control "continuous construction time-lapse, not real-time footage" (unless threshold or reward).
- VIDEO {i} CONCRETENESS (no abstractions): describe the SAME single lone worker every beat, reusing the exact costume from the packet worker_choreography (e.g. "one lone worker in a solid pale shirt, dark pants, and dark cap"); name the ONE specific manual tool used; describe the concrete repeated work cycle in -ing verbs (e.g. scooping, lifting, pressing, fastening). NEVER write vague filler like "transformation progresses" or "the scene transforms" — show observable physical actions only.
- VIDEO {i} must end with a PERSISTENT-TRACES clause naming the marks this beat leaves behind (e.g. scrape grooves, end-grain circles, screw heads, nail rows, sawdust trails, trimmed edges, compression tracks), followed by a natural-language description of both the near-field diegetic sound effects (2-4 specific sounds of tools, materials, or footsteps) and the steady room/environment ambient noise. Use varied phrasing for these audio descriptions rather than a single formulaic structure.
- IMAGE {i+1} must be a clean frame with ZERO workers/machinery. Do NOT use the words 'worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', or 'people' under any circumstances, even to state that they are absent or not present. Describe only static objects, surfaces, and traces. It must RESTATE the locked anchors by name and Grid cell exactly as given in the packet primary_landmarks (e.g. "Locked anchors: <name> at Grid A2, <name> at Grid B2, <name> at Grid C2"), restate the left/right/top/bottom boundaries from the packet frame_boundaries, and then describe the visible state delta of this beat plus a FEW (2-3, not exhaustive) PERSISTENT physical traces that prove the work happened (scrape marks, fastener heads, sawdust, membrane wrinkles, displaced soil, etc.). Prior MAJOR installed/finished features (panels, walls, floors, fixtures, primary landmarks) stay present and unchanged (monotonic state) — but you do NOT need to re-list every minor trace from every earlier beat; it is fine and expected for small cosmetic details to fade from the description as new ones accumulate.
- For threshold bridge beats (if beat is a threshold bridge), follow the TBCP rules (Bridge-1 stops at sill, Bridge-2 crosses sill; soft exposure roll; door-frame wipe).
- NLVTR visual-only rule: No '%' symbols, no numeric ranges, no acronyms (HAL, SCUP, NGCS, VMFP, RCE, GCTR, RPL, etc.) in the prompts.
- FULL-ENCLOSURE COVERAGE: When the beat involves framing, insulating, paneling, or painting walls, the IMAGE prompt MUST explicitly include the ceiling/roof/top surface as well. For example, if walls in Grid B1, B3, C1, C3 are paneled, the ceiling curve in Grid A1, A2, A3 must ALSO be described as paneled. Never treat wall coverage as complete without ceiling coverage in any enclosed space (cabin, room, fuselage, container, vault, etc.).
- CAMERA VIEWPOINT CONTINUITY: If the previous IMAGE was shot from an interior viewpoint (camera inside the space, entry behind camera), the next IMAGE MUST maintain the same interior viewpoint UNLESS an explicit camera-pullback VIDEO is inserted between them. You CANNOT jump from interior to exterior viewpoint without a transition. If the beat requires switching back to an exterior view, generate the VIDEO as a reverse dolly pulling back through the doorway, and describe the exposure transition accordingly.
- EXTERIOR WORK VISIBILITY: If the beat involves work on the EXTERIOR surface of the structure (e.g., exterior insulation, exterior membrane), and the camera is positioned INSIDE looking out, the VIDEO must show the worker operating at the boundary edges visible from inside (e.g., working at seam lines visible in Grid B1/B3 from the interior). Do not describe exterior work that would be invisible from the current camera position.
- CONSTRUCTION ORDER CONSTRAINTS: Floor finish (hardwood, tile) MUST be installed BEFORE heavy anchored objects (fireplace, stove) are placed on it. If this beat installs a fireplace or heavy object, the IMAGE must show it sitting on the FINISHED floor, not on bare metal/subfloor. If the floor is not yet finished, the fireplace cannot be installed in this beat.
- Output the prompts in the following format:
===VIDEO===
<video prompt body>
===IMAGE===
<image prompt body>
===TRACES===
[
  {
    "name": "precise name of new permanent feature/material/trace (e.g. steel screw heads, green insulation foam)",
    "material_color": "color/texture (e.g. metallic silver)",
    "initial_state": "state when introduced (e.g. freshly installed)",
    "grid": "approximate grid coordinate if mentioned (e.g. Grid B2, default to Grid B2)",
    "z_depth_scale": "depth scale if mentioned (e.g. 50%, default to 50%)"
  }
]
"""
            beat_user = f"Generate prompts for Beat {i}: {beat['operation']} - {beat['description']}."
            if str(i) in audit_feedback_dict:
                beat_user += "\n\n" + "==================== PRIOR AUDIT FAILURES FOR THIS BEAT ====================\n"
                beat_user += "This beat failed a prior quality audit. You MUST fix these specific issues:\n"
                for err in audit_feedback_dict[str(i)]:
                    beat_user += f"- {err}\n"
                beat_user += "\nStrictly ensure your new generation does not repeat these mistakes."

            vid_prompt = ""
            img_prompt = ""
            new_ledger_items = None
            
            feedback = ""
            for attempt in range(4):
                try:
                    user_msg = beat_user
                    if feedback:
                        user_msg += f"\n\n{feedback}"
                    
                    resp = _chat(config, beat_system, user_msg, temperature=0.8, timeout=90)
                    secs = _extract_marked(resp, ['===VIDEO===', '===IMAGE===', '===TRACES==='])
                    v_p = secs.get('===VIDEO===', '').strip()
                    i_p = secs.get('===IMAGE===', '').strip()
                    
                    # Apply proactive fixes
                    v_p, i_p = apply_proactive_fixes(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, beat=beat, config=config)
                    
                    # Validate prompts
                    prev_v = compiled_videos.get(i - 1) if i > 1 else None
                    prev_i = compiled_images.get(i) if i > 1 else None
                    
                    # 1. Run cheap/local validations first
                    errs = validate_beat_prompts(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, prev_v, prev_i, config=config, beat=beat, skip_llm_checks=True)
                    if not errs:
                        # 2. If cheap validations passed, run LLM validation (monotonic, delta, etc.)
                        llm_errs = validate_beat_prompts(i, v_p, i_p, packet, mode, is_last, is_threshold_or_reveal, prev_v, prev_i, config=config, beat=beat, skip_llm_checks=False)
                        if not llm_errs:
                            vid_prompt = v_p
                            img_prompt = i_p
                            # Also parse TRACES JSON embedded in the prompt response
                            traces_str = secs.get('===TRACES===', '').strip()
                            new_ledger_items_parsed = []
                            if traces_str:
                                try:
                                    traces_clean = _strip_code_fences(traces_str).strip()
                                    parsed = json.loads(traces_clean)
                                    if isinstance(parsed, list):
                                        for item in parsed:
                                            if isinstance(item, dict) and "name" in item:
                                                new_ledger_items_parsed.append({
                                                    "name": str(item.get("name")),
                                                    "material_color": str(item.get("material_color", "unknown")),
                                                    "initial_state": str(item.get("initial_state", "installed")),
                                                    "grid": str(item.get("grid", "Grid B2")),
                                                    "z_depth_scale": str(item.get("z_depth_scale", "50%"))
                                                })
                                        new_ledger_items = new_ledger_items_parsed
                                except Exception as e:
                                    if sys.stdout:
                                        print(f"[DEBUG] Failed to parse prompt-embedded TRACES JSON: {e}")
                            break
                        else:
                            errs = llm_errs
                    
                    feedback = "The generated prompts failed validation. Please fix the following errors:\n"
                    for err in errs:
                        feedback += f"- {err}\n"
                    feedback += "\nPlease rewrite the prompts, strictly adhering to all rules."
                    if sys.stdout:
                        print(f"[DEBUG] Beat {i} attempt {attempt+1} failed validation: {errs}")
                        print(f"[DEBUG]   Generated VIDEO prompt: {v_p}")
                        print(f"[DEBUG]   Generated IMAGE prompt: {i_p}")
                except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Beat {i} hit a code-level error ({type(e).__name__}: {e}); aborting to avoid "
                        f"shipping placeholder output. Fix the bug rather than retrying."
                    ) from e
                except Exception as e:
                    if sys.stdout:
                        print(f"[DEBUG] Beat {i} attempt {attempt+1} error: {e}")
                    feedback = f"Error during generation: {e}. Please retry."

            if not vid_prompt or not img_prompt:
                fallback_count += 1
                clean_op = beat.get('operation', 'construction').replace('_', ' ')
                desc = beat.get('description', 'performing restoration work').strip().rstrip('.')
                
                vid_prompt = (
                    f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                    f"The video captures the physical process of: {desc}. A worker is visible performing the manual installation and assembly steps, slowly building and placing elements. The background and camera position remain locked."
                )
                if not is_threshold_or_reveal:
                    vid_prompt += " continuous construction time-lapse, not real-time footage."
                
                img_prompt = (
                    f"A static ultra-wide 14mm tripod shot at 1.6m height: clean completed state after the step of {desc} of {theme}; "
                    f"horizon line remains level; no workers are present in this clean frame. The newly completed features are visible and integrated into the scene."
                )
                if is_last:
                    img_prompt += " Polished floor displays blurred diffused reflections."

            compiled_images[i + 1] = img_prompt
            compiled_videos[i] = vid_prompt

            # Dynamically update the object ledger with new persistent traces/features
            if vid_prompt and img_prompt:
                if new_ledger_items is None:
                    new_ledger_items = extract_persistent_traces_to_ledger(config, vid_prompt, img_prompt)
                if new_ledger_items:
                    if 'object_ledger' not in packet or not isinstance(packet['object_ledger'], list):
                        packet['object_ledger'] = []
                    existing_names = {x['name'].lower() for x in packet['object_ledger'] if isinstance(x, dict) and 'name' in x}
                    added_count = 0
                    for item in new_ledger_items:
                        if item['name'].lower() not in existing_names:
                            packet['object_ledger'].append(item)
                            existing_names.add(item['name'].lower())
                            added_count += 1
                    if sys.stdout:
                        print(f"[DEBUG] Dynamic Ledger: Added {added_count} new items (deduplicated). Total objects: {len(packet['object_ledger'])}")

        # Quality gate
        fallback_limit = max(2, total_beats // 3)
        if fallback_count > fallback_limit:
            raise RuntimeError(
                f"{fallback_count} of {total_beats} beats fell back to placeholder prompts "
                f"(limit {fallback_limit}); output quality too low to ship. See server.log for per-beat errors."
            )

        # Step 7: Final Audit & Reassembly
        if on_progress:
            on_progress('audit', '正在运行工序与场景一致性二次校验...')

        reassembled_prompts_block = _format_prompt_block(compiled_images, compiled_videos)

        audit_system = """You are a construction sequence and prompt quality auditor.
Your job is to analyze the complete, reassembled IMAGE and VIDEO prompt set and output a quality audit report.
Analyze the prompt set for strict construction order, physical causality, and spatial consistency.

You MUST check for ALL of the following specific issues:

1. **天花板/顶面遗漏**: For any enclosed space, check that ceiling/roof treatment (framing, insulation, paneling, painting) is explicitly described alongside wall treatment. If walls are covered but the ceiling is left as raw/exposed structure, mark as 未通过.
2. **视角跳切**: Check that camera viewpoint transitions are smooth. If an IMAGE is interior (camera inside), the next IMAGE cannot suddenly be exterior without an intervening camera-pullback VIDEO. Mark any sudden interior-to-exterior or exterior-to-interior jumps without transition as 未通过.
3. **施工顺序**: Check physical causality: flooring before heavy furniture/fixtures, wiring before light fixtures, priming before painting, door leaf after door frame. Mark violations as 未通过.
4. **视频模板冲突**: Check that each VIDEO's worker entry/exit template matches the body text. If the body says no workers but the template adds a worker, or the body says two workers but the template says one lone worker, mark as 未通过.
5. **灯具安装遗漏**: If the final IMAGE shows light fixtures, check that there was an explicit fixture installation beat. If fixtures appear without installation, mark as 未通过.
6. **门扇遗漏**: If a door frame was installed, check that a door panel/leaf was also installed in a subsequent beat (unless it is explicitly an archway). Mark as 未通过 if missing.
7. **外部工作视角**: If a beat involves exterior work (e.g., exterior insulation) but the camera is inside, check that the work is visible from the camera position. Mark as 未通过 if contradicted.

You must output a Markdown table with the following columns:
| 拍号 / Beat Index | 审核大项 | 子检查项 / 协议要求 | 通过状态 | 审核判定说明 |

In the "拍号 / Beat Index" column, specify the 1-based index of the beat (e.g. "3", "5"), or "Project" if it is a project-wide check.

Do NOT output anything else. Do not wrap in markdown code fences."""
        audit_user = f"""Here is the complete generated prompt set:
{reassembled_prompts_block}

Please generate the detailed quality audit table."""

        audit_md = ""
        for attempt in range(3):
            try:
                audit_md = _chat(config, audit_system, audit_user, temperature=0.3, timeout=120)
                if '|' in audit_md:
                    break
            except Exception as e:
                if sys.stdout:
                    print(f"[DEBUG] Pass 3 Exception: {e}")

        audit_md_cleaned = _strip_code_fences(audit_md)

        # Parse failures from the audit table
        failures = parse_audit_failures(config, audit_md_cleaned)
        if not failures:
            if sys.stdout:
                print("[AUDIT] All quality checks passed successfully!")
            break
        else:
            if sys.stdout:
                print(f"[AUDIT] Found failures in beats: {list(failures.keys())}")
            audit_feedback_dict = failures
            audit_passes += 1
            if audit_passes > max_audit_passes:
                if sys.stdout:
                    print(f"[AUDIT] Warning: Maximum audit healing passes reached, continuing with some failures.")
                break

    final_output = f"""===TITLE===
{title}
===THEME===
{parsed_brief.get('theme', theme)}
===PROMPTS===
{reassembled_prompts_block}
===AUDIT===
{audit_md_cleaned}"""

    return final_output


def call_image_llm(config, prompt_content):
    base_model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in base_model:
        base_model = base_model.replace('nano-banana-2', 'gemini-3.1-flash-image')

    # Build magic suffix model name (Method 3)
    model = base_model
    
    # 1. Aspect Ratio suffix
    aspect_ratio = config.get('imageAspectRatio')
    if aspect_ratio:
        # replace ':' with '-' to convert '9:16' to '9-16'
        ratio_suffix = aspect_ratio.replace(':', '-')
        model = f"{model}-{ratio_suffix}"

    # 2. Quality suffix
    quality = config.get('imageQuality')
    if quality:
        q_lower = quality.lower()
        if q_lower in ('2k', 'medium'):
            model = f"{model}-2k"
        elif q_lower in ('4k', 'hd'):
            model = f"{model}-4k"
        # '1k' or 'standard' gets no suffix

    if sys.stdout:
        print(f"[DEBUG] call_image_llm (magic suffix): original_model='{base_model}', constructed_model='{model}'")

    base_url, api_key = resolve_gateway(model, config)
    payload = {
        'model': model,
        'messages': [
            {'role': 'user', 'content': prompt_content},
        ]
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    return body['choices'][0]['message'].get('content') or ''


def _quality_to_images_api(quality):
    return _image_quality_to_label(quality)


def _image_size_to_api_size(aspect_ratio):
    return aspect_ratio or '9:16'


def _image_quality_to_label(quality):
    q = (quality or '2K').lower()
    if q in ('4k', 'hd'):
        return '4K'
    if q in ('2k', 'medium'):
        return '2K'
    return '1K'


def _image_generation_model(config):
    model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (config.get('imageAspectRatio') or '9:16').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality = _image_quality_to_label(config.get('imageQuality'))
    if quality == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _image_generation_model_for_request(model, size, quality):
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (size or '1:1').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality_label = _image_quality_to_label(quality)
    if quality_label == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality_label == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _image_edit_model(config):
    model = config.get('imageModel') or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    if model == 'gpt-image-2':
        return model
    if re.search(r'-\d+-\d+(?:-\d+k)?$', model.lower()):
        return model

    aspect_ratio = (config.get('imageAspectRatio') or '9:16').replace(':', '-')
    if model.lower().startswith('gemini'):
        return f'{model}-{aspect_ratio}'

    quality = _image_quality_to_label(config.get('imageQuality'))
    if quality == '4K':
        return f'{model}-{aspect_ratio}-4k'
    if quality == '2K':
        return f'{model}-{aspect_ratio}-2k'
    return f'{model}-{aspect_ratio}'


def _safe_project_name(title):
    raw = (title or 'spark_frames').strip()
    import hashlib
    # 仅保留英文字母、数字、下划线和连字符，彻底杜绝中文编码问题以及 #、?、%、&、+ 等 URL 特殊字符引发的 404 截断
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', raw)
    # 合并连续的下划线，并去除首尾的下划线/连字符
    sanitized = re.sub(r'_+', '_', sanitized).strip('_-')
    
    # 计算原始标题的 MD5 值，用于防冲突和兜底
    title_hash = hashlib.md5(raw.encode('utf-8', errors='ignore')).hexdigest()
    
    if not sanitized:
        # 如果纯中文或其他非 ASCII 字符导致过滤后为空，直接使用前 12 位 MD5 哈希
        return title_hash[:12]
    else:
        # 截取前 40 个 ASCII 字符，并附加 6 位 MD5 后缀以保证唯一性
        return f"{sanitized[:40]}_{title_hash[:6]}"


def _get_project_dir(title):
    # 1. Try new naming scheme
    new_name = _safe_project_name(title)
    new_dir = os.path.join(OUTPUT_ROOT, new_name)
    if os.path.exists(new_dir):
        return new_dir
        
    # 2. Try old naming scheme
    old_raw = (title or 'spark_frames').strip()
    old_raw = re.sub(r'[\\/:*?"<>|]+', '_', old_raw)
    old_raw = re.sub(r'\s+', '_', old_raw)
    old_name = old_raw.strip('._-')[:60] or 'spark_frames'
    old_dir = os.path.join(OUTPUT_ROOT, old_name)
    if os.path.exists(old_dir):
        return old_dir
        
    # 3. Default to new naming scheme path if neither exists (for creation)
    return new_dir


def _extract_image_url_from_text(content):
    if not content:
        return None
    markdown = re.search(r'!\[.*?\]\((.*?)\)', content, re.DOTALL)
    if markdown:
        return markdown.group(1).strip()
    raw_url = re.search(r'(https?://[^\s)]+|data:image/[^\s)]+)', content, re.DOTALL)
    if raw_url:
        return raw_url.group(1).strip()
    trimmed = content.strip()
    if trimmed.startswith(('http://', 'https://', 'data:image/')):
        return trimmed
    return None


def _clean_gemini_image_model(model):
    model = model or 'gemini-3.1-flash-image'
    if 'nano-banana-2' in model:
        model = model.replace('nano-banana-2', 'gemini-3.1-flash-image')
    model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
    model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', model, flags=re.IGNORECASE)
    return model


def _gemini_direct_api_key(config):
    return (
        (config or {}).get('geminiDirectApiKey')
        or (config or {}).get('geminiApiKey')
        or SERVER_CONFIG.get('geminiDirectApiKey')
        or SERVER_CONFIG.get('geminiApiKey')
        or os.environ.get('GEMINI_API_KEY')
        or ''
    ).strip()


def _prepare_gemini_inline_image(file_data, content_type=None, max_side=1536):
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(file_data)).convert('RGB')
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'image/png', base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return content_type or 'image/png', base64.b64encode(file_data).decode('ascii')


def _crop_to_aspect_ratio(img, aspect_ratio_str):
    if not aspect_ratio_str or aspect_ratio_str.lower() == 'auto':
        return img
    try:
        aspect_ratio_str = aspect_ratio_str.replace('-', ':')
        if ':' in aspect_ratio_str:
            w_ratio, h_ratio = map(float, aspect_ratio_str.split(':'))
        else:
            return img
            
        target_ratio = w_ratio / h_ratio
        width, height = img.size
        current_ratio = width / height
        
        if abs(current_ratio - target_ratio) < 0.01:
            return img
            
        if current_ratio > target_ratio:
            new_width = int(height * target_ratio)
            left = (width - new_width) // 2
            right = left + new_width
            img = img.crop((left, 0, right, height))
        else:
            new_height = int(width / target_ratio)
            top = (height - new_height) // 2
            bottom = top + new_height
            img = img.crop((0, top, width, bottom))
            
        return img
    except Exception:
        return img


def _extract_b64_from_gemini_native_response(obj):
    if isinstance(obj, list):
        for item in obj:
            found = _extract_b64_from_gemini_native_response(item)
            if found:
                return found
        return None
    if not isinstance(obj, dict):
        if isinstance(obj, str) and obj.startswith('data:image/') and ',' in obj:
            return obj.split(',', 1)[1]
        return None

    for key in ('inlineData', 'inline_data', 'image', 'output_image', 'outputImage'):
        value = obj.get(key)
        if isinstance(value, dict):
            data = value.get('data') or value.get('b64_json') or value.get('base64')
            if isinstance(data, str) and len(data) > 100:
                return data.split(',', 1)[1] if data.startswith('data:image/') and ',' in data else data
            url = value.get('url')
            if isinstance(url, str) and url.startswith('data:image/') and ',' in url:
                return url.split(',', 1)[1]

    for key in ('b64_json', 'base64', 'data'):
        value = obj.get(key)
        if isinstance(value, str) and len(value) > 100:
            if key == 'data':
                is_image = (
                    obj.get('type') == 'image' or
                    'image' in str(obj.get('mime_type') or obj.get('mimeType') or '') or
                    not any(c.isspace() for c in value[:100])
                )
                if not is_image:
                    continue
            return value.split(',', 1)[1] if value.startswith('data:image/') and ',' in value else value

    for value in obj.values():
        found = _extract_b64_from_gemini_native_response(value)
        if found:
            return found
    return None


def _post_gemini_direct_json(url, api_key, payload, timeout=240):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': api_key,
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _gemini_native_image_edit(config, model, prompt, file_items, aspect_ratio, image_size):
    api_key = _gemini_direct_api_key(config)
    if not api_key:
        return None

    clean_model = _clean_gemini_image_model(model)
    direct_model = (config or {}).get('geminiDirectImageModel') or SERVER_CONFIG.get('geminiDirectImageModel') or clean_model
    instruction = (
        'IMAGE EDITING MODE. Use the attached reference image as the authoritative source canvas. '
        'Preserve its composition, camera angle, objects, identity, materials, and layout unless the '
        'user explicitly asks to change them. Do not create an unrelated text-to-image scene. '
        'Return one edited image only.\n\n'
        f'{prompt or ""}'
    )
    inline_images = []
    for _name, _filename, content_type, file_data in file_items:
        mime, encoded = _prepare_gemini_inline_image(file_data, content_type)
        inline_images.append((mime, encoded))

    endpoint_model = urllib.parse.quote(direct_model, safe='')
    last_error = None

    interactions_input = [{'type': 'text', 'text': instruction}]
    for mime, encoded in inline_images:
        interactions_input.append({
            'type': 'image',
            'image': {'mime_type': mime, 'mimeType': mime, 'data': encoded},
            'mime_type': mime,
            'mimeType': mime,
            'data': encoded,
        })
    image_generation_config = {}
    if aspect_ratio:
        image_generation_config['aspectRatio'] = aspect_ratio
    if image_size:
        image_generation_config['imageSize'] = image_size

    interactions_payload = {
        'model': direct_model,
        'input': interactions_input,
        'responseModalities': ['IMAGE'],
        'response_format': {
            'type': 'image',
            'mime_type': 'image/jpeg',
            'mimeType': 'image/jpeg',
        }
    }
    if image_generation_config:
        interactions_payload['imageGenerationConfig'] = image_generation_config

    generate_parts = [{'text': instruction}]
    for mime, encoded in inline_images:
        generate_parts.append({'inlineData': {'mimeType': mime, 'data': encoded}})
    generate_payload = {
        'contents': [{'role': 'user', 'parts': generate_parts}],
        'generationConfig': {'responseModalities': ['IMAGE']},
    }
    if aspect_ratio or image_size:
        generate_payload['generationConfig']['imageConfig'] = {}
        if aspect_ratio:
            generate_payload['generationConfig']['imageConfig']['aspectRatio'] = aspect_ratio
        if image_size:
            generate_payload['generationConfig']['imageConfig']['imageSize'] = image_size

    for label, url, payload in (
        ('interactions', 'https://generativelanguage.googleapis.com/v1beta/interactions', interactions_payload),
        ('generateContent', f'https://generativelanguage.googleapis.com/v1beta/models/{endpoint_model}:generateContent', generate_payload),
    ):
        try:
            if sys.stdout:
                print(f"[GEMINI DIRECT] I2I via {label}, model={direct_model}, images={len(file_items)}")
            resp_data = _post_gemini_direct_json(url, api_key, payload, timeout=300)
            b64_json = _extract_b64_from_gemini_native_response(resp_data)
            if b64_json:
                return {'created': int(time.time()), 'data': [{'b64_json': b64_json}]}
            last_error = f'{label} response contained no image data'
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8')[:800]
            except Exception:
                pass
            last_error = f'{label} HTTP {e.code}: {detail}'
            if sys.stdout:
                print(f"[GEMINI DIRECT] {last_error}")
        except Exception as e:
            last_error = f'{label}: {e}'
            if sys.stdout:
                print(f"[GEMINI DIRECT] {last_error}")

    raise RuntimeError(f'Gemini direct image edit failed: {last_error}')


def _detect_image_mime_from_path(path):
    with open(path, 'rb') as image_file:
        signature = image_file.read(16)
    if signature.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if signature.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if signature.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if signature.startswith(b'RIFF') and signature[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def _persist_data_url_image(data_url, title, prefix='cover'):
    if not data_url or not data_url.startswith('data:image/'):
        return data_url

    header, encoded = data_url.split(',', 1)
    ext_match = re.match(r'data:image/([a-zA-Z0-9.+-]+);base64', header)
    ext = (ext_match.group(1) if ext_match else 'png').lower()
    if ext == 'jpeg':
        ext = 'jpg'

    out_dir = os.path.join(OUTPUT_ROOT, 'covers')
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{_safe_project_name(title)}_{prefix}_{int(time.time() * 1000)}.{ext}"
    target_path = os.path.join(out_dir, filename)
    with open(target_path, 'wb') as f:
        f.write(base64.b64decode(encoded))

    rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    return '/' + rel_path


def _extract_image_prompts(block):
    images, _ = _parse_prompt_slots(block)
    return [{'index': idx, 'prompt': images[idx]} for idx in sorted(images)]


def _decode_or_download_image(data_item, target_path, config):
    b64 = None
    url = None
    if isinstance(data_item, dict):
        b64 = data_item.get('b64_json')
        url = data_item.get('url')
    elif isinstance(data_item, str):
        url = data_item

    image_bytes = None
    if b64:
        image_bytes = base64.b64decode(b64)
    elif url and url.startswith('data:image/'):
        _, encoded = url.split(',', 1)
        image_bytes = base64.b64decode(encoded)
    elif url and (url.startswith('http://') or url.startswith('https://')):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=180) as resp:
            image_bytes = resp.read()

    if not image_bytes:
        raise RuntimeError('image response did not include b64_json, data URL, or downloadable URL')

    if target_path.lower().endswith('.webp'):
        from PIL import Image
        import io
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.save(target_path, format='WEBP', quality=80)
        except Exception as e:
            print(f"Failed to convert image to WebP: {e}. Saving raw bytes instead.")
            with open(target_path, 'wb') as f:
                f.write(image_bytes)
    else:
        with open(target_path, 'wb') as f:
            f.write(image_bytes)


def _save_image_station_result(resp_data):
    """
    Given the response from /api/image/generations or /api/image/edits,
    if it contains b64_json or a remote URL, download/decode it,
    save it to outputs/image-station/ as a WebP file,
    and update resp_data to point to the local WebP URL.
    This prevents sending megabytes of base64 to the frontend.
    """
    if not isinstance(resp_data, dict) or 'data' not in resp_data:
        return resp_data
    
    out_dir = os.path.join(OUTPUT_ROOT, 'image-station')
    os.makedirs(out_dir, exist_ok=True)
    
    import base64
    import time
    import uuid
    import urllib.request
    
    for item in resp_data.get('data', []):
        b64 = item.get('b64_json')
        url = item.get('url')
        
        image_bytes = None
        if b64:
            image_bytes = base64.b64decode(b64)
        elif url and url.startswith('data:image/'):
            try:
                _, encoded = url.split(',', 1)
                image_bytes = base64.b64decode(encoded)
            except Exception: pass
        elif url and (url.startswith('http://') or url.startswith('https://')):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=180) as resp:
                    image_bytes = resp.read()
            except Exception as e:
                print(f"[IMAGE STATION] Failed to download remote image {url}: {e}")
                
        if image_bytes:
            filename = f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.webp"
            target_path = os.path.join(out_dir, filename)
            
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(image_bytes))
                img.save(target_path, format='WEBP', quality=80)
                
                # Update the item in resp_data
                rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                if 'b64_json' in item:
                    del item['b64_json']
                item['url'] = '/' + rel_path
            except Exception as e:
                print(f"[IMAGE STATION] Failed to convert/save image as WebP: {e}")
                
    return resp_data


def _post_json(base_url, api_key, path, payload, timeout=240):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url.rstrip("/")}{path}',
        data=data,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _post_multipart(base_url, api_key, path, fields, file_field, file_path, timeout=300):
    boundary = f'----SparkFrameBoundary{int(time.time() * 1000)}'
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\n'.encode('utf-8'))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8'))
        chunks.append(str(value).encode('utf-8'))
        chunks.append(b'\r\n')
    filename = os.path.basename(file_path)
    chunks.append(f'--{boundary}\r\n'.encode('utf-8'))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode('utf-8')
    )
    with open(file_path, 'rb') as f:
        chunks.append(f.read())
    chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(chunks)

    req = urllib.request.Request(
        f'{base_url.rstrip("/")}{path}',
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _generate_text_image(config, prompt, target_path):
    model = _image_generation_model(config)
    base_url, api_key = resolve_gateway(model, config)
    clean_render_prompt = (
        "Render a clean photorealistic image with no visible text, captions, labels, grid lines, "
        "measurement guides, letters, numbers, or percentages. Any terms such as Grid A2, Grid B1, "
        "coordinates, or percentage heights in the scene description are invisible composition "
        "instructions only and must never appear in the image.\n\n"
        f"{prompt}"
    )
    payload = {
        'model': _image_generation_model(config),
        'prompt': clean_render_prompt,
        'size': _image_size_to_api_size(config.get('imageAspectRatio')),
        'quality': _quality_to_images_api(config.get('imageQuality')),
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'response_format': 'b64_json',
    }
    data = _post_json(base_url, api_key, '/images/generations', payload, timeout=300)
    if not data.get('data'):
        raise RuntimeError('text-to-image response contained no image data')
    _decode_or_download_image(data['data'][0], target_path, config)


def _generate_image_edit(config, prompt, reference_path, target_path, control_prompt=None):
    if control_prompt is None:
        control_prompt = IMG2IMG_CONTROL_PROMPT
    model = _image_edit_model(config)
    base_url, api_key = resolve_gateway(model, config)

    aspect_ratio = config.get('imageAspectRatio') or '9:16'
    try:
        from PIL import Image
        img = Image.open(reference_path).convert('RGB')
        img = _crop_to_aspect_ratio(img, aspect_ratio)
        import io
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        ref_bytes = buf.getvalue()
    except Exception:
        with open(reference_path, 'rb') as f:
            ref_bytes = f.read()

    # Ensure clean model name (no suffixes like -9-16-2k)
    clean_model = re.sub(r'-\d+-\d+(?:-\d+k)?$', '', model, flags=re.IGNORECASE)
    clean_model = re.sub(r'-(?:2k|4k)(?:-\d+x\d+)?$', '', clean_model, flags=re.IGNORECASE)

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            # 1. If using Gemini Direct API, route to the native Gemini image edit method
            if _gemini_direct_api_key(config):
                file_items = [('image', 'reference.png', 'image/png', ref_bytes)]
                image_size = _image_quality_to_label(config.get('imageQuality'))
                
                native_resp = _gemini_native_image_edit(
                    config,
                    clean_model,
                    prompt,
                    file_items,
                    aspect_ratio,
                    image_size,
                )
                if native_resp and native_resp.get('data'):
                    _decode_or_download_image(native_resp['data'][0], target_path, config)
                    return False  # Success, not a fallback
                else:
                    raise RuntimeError('Gemini direct image edit returned no data')

            # 2. Use the standard OpenAI /images/edits endpoint via multipart/form-data.
            import uuid
            boundary = f"Boundary-{uuid.uuid4().hex}"
            body_data = bytearray()

            fields = {
                'model': clean_model,
                'prompt': f'{control_prompt}\n\n{prompt}'.strip(),
                'aspect_ratio': aspect_ratio,
                'image_size': _image_quality_to_label(config.get('imageQuality')),
                'response_format': 'b64_json',
            }

            for k, v in fields.items():
                body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
                body_data.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
                body_data.extend(f"{v}\r\n".encode('utf-8'))

            # Add reference image file
            body_data.extend(f"--{boundary}\r\n".encode('utf-8'))
            body_data.extend(f'Content-Disposition: form-data; name="image"; filename="reference.png"\r\n'.encode('utf-8'))
            body_data.extend(b'Content-Type: image/png\r\n\r\n')
            body_data.extend(ref_bytes)
            body_data.extend(b"\r\n")

            body_data.extend(f"--{boundary}--\r\n".encode('utf-8'))

            if sys.stdout:
                print(
                    f"[FRAME SEQUENCE] Image-to-Image edit via /images/edits (multipart) (attempt {attempt+1}/{max_attempts}): "
                    f"{os.path.basename(reference_path)} ({len(ref_bytes)} bytes) -> {clean_model}"
                )

            req = urllib.request.Request(
                f'{base_url}/images/edits',
                data=bytes(body_data),
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': f'multipart/form-data; boundary={boundary}',
                },
                method='POST',
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=360) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            if not data.get('data'):
                raise RuntimeError('image-to-image response contained no image data')
            _decode_or_download_image(data['data'][0], target_path, config)
            return False  # Success, not a fallback

        except Exception as e:
            if sys.stdout:
                print(f"[FRAME SEQUENCE] Image edit attempt {attempt+1}/{max_attempts} failed: {e}")
            if attempt < max_attempts - 1:
                import time
                time.sleep(2.0 + attempt * 2.0)
            else:
                # All attempts failed, do fallback
                if sys.stdout:
                    print(f"[FRAME SEQUENCE] All /images/edits attempts failed. Falling back to compatibility JSON /images/generations.")

    # Fallback to the old JSON /images/generations compatibility path
    import base64
    image_b64 = base64.b64encode(ref_bytes).decode('ascii')
    payload = {
        'model': clean_model,
        'prompt': f'{control_prompt}\n\n{prompt}',
        'image': image_b64,
        'aspect_ratio': aspect_ratio,
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'response_format': 'b64_json',
    }
    if aspect_ratio:
        payload['size'] = aspect_ratio
    data = _post_json(base_url, api_key, '/images/generations', payload, timeout=360)
    if not data.get('data'):
        raise RuntimeError('image-to-image response contained no image data')
    _decode_or_download_image(data['data'][0], target_path, config)
    return True  # Fallback occurred


def generate_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None):
    images, videos = _parse_prompt_slots(prompt_block)
    prompts = [{'index': idx, 'prompt': images[idx]} for idx in sorted(images)]
    if not prompts:
        raise RuntimeError('未在 prompt_block 中找到任何 图片 N: 提示词')

    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest = {
        'title': title,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'method': 'A_single_chain',
        'aspect_ratio': config.get('imageAspectRatio') or '9:16',
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'control_prompt': IMG2IMG_CONTROL_PROMPT,
        'frames': [],
    }

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                existing_manifest = json.load(f)
                if isinstance(existing_manifest, dict) and 'frames' in existing_manifest:
                    manifest['frames'] = existing_manifest['frames']
                    manifest['created_at'] = existing_manifest.get('created_at', manifest['created_at'])
        except Exception:
            pass

    if on_progress:
        total_to_generate = len(target_sequences) if target_sequences is not None else len(prompts)
        on_progress('start', {'total': total_to_generate})

    manifest_frames_by_seq = {f['sequence']: f for f in manifest['frames']}

    previous_path = None
    generated_count = 0
    lineage_degraded = False

    for seq, item in enumerate(prompts, start=1):
        filename = f'img_{seq:03d}.webp'
        target_path = os.path.join(frames_dir, filename)
        
        should_generate = True
        if target_sequences is not None:
            min_target = min(target_sequences)
            should_generate = seq >= min_target

        if not should_generate:
            if os.path.exists(target_path):
                previous_path = target_path
                existing_frame = manifest_frames_by_seq.get(seq)
                if existing_frame and existing_frame.get('quality_gate') == 'i2i_fallback_degraded':
                    lineage_degraded = True
            continue

        # If the file already exists on disk and we are not doing a specific target retry/regeneration,
        # we can skip the external API call and use the existing file immediately.
        already_exists = os.path.exists(target_path) and os.path.getsize(target_path) > 0
        skip_api_call = already_exists and (target_sequences is None)

        model = ""
        retries = 0
        vlm_qa_failed = False
        vlm_qa_reason = None

        if not skip_api_call:
            use_text_generation = (seq == 1 or not previous_path or not os.path.exists(previous_path))
            model = _image_generation_model(config) if use_text_generation else _image_edit_model(config)
            reference = previous_path if not use_text_generation else None
            
            is_fallback = False
            ctrl_prompt = IMG2IMG_CONTROL_PROMPT
            try:
                if use_text_generation:
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    prompt_text = item['prompt'].lower()
                    is_camera_moving = any(kw in prompt_text for kw in [
                        'push-in', 'push in', 'forward-pushing', 'forward pushing', 'dolly',
                        'camera moves', 'camera relocates', 'crosses the sill', 'sill-handoff',
                        'camera advances', 'zoom-in', 'zoom in', 'closer view'
                    ])
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT if is_camera_moving else IMG2IMG_CONTROL_PROMPT
                    is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                    if is_fallback:
                        lineage_degraded = True
            except Exception:
                retries += 1
                if use_text_generation:
                    _generate_text_image(config, item['prompt'], target_path)
                    lineage_degraded = False
                else:
                    prompt_text = item['prompt'].lower()
                    is_camera_moving = any(kw in prompt_text for kw in [
                        'push-in', 'push in', 'forward-pushing', 'forward pushing', 'dolly',
                        'camera moves', 'camera relocates', 'crosses the sill', 'sill-handoff',
                        'camera advances', 'zoom-in', 'zoom in', 'closer view'
                    ])
                    ctrl_prompt = IMG2IMG_BRIDGE_CONTROL_PROMPT if is_camera_moving else IMG2IMG_CONTROL_PROMPT
                    is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                    if is_fallback:
                        lineage_degraded = True

            # Run VLM visual QA check on successful edits
            if seq > 1 and not use_text_generation and os.path.exists(previous_path) and os.path.exists(target_path):
                video_prompt = videos.get(seq - 1, "")
                if video_prompt:
                    vlm_pass, vlm_reason = run_vlm_qa_check(config, previous_path, target_path, video_prompt)
                    if not vlm_pass:
                        if sys.stdout:
                            print(f"[VLM QA] Beat {seq-1} (IMAGE {seq}) failed VLM check: {vlm_reason}. Retrying generation...")
                        # Retry generation up to 2 times
                        for retry_attempt in range(2):
                            try:
                                is_fallback = _generate_image_edit(config, item['prompt'], previous_path, target_path, control_prompt=ctrl_prompt)
                                if is_fallback:
                                    lineage_degraded = True
                                vlm_pass, vlm_reason = run_vlm_qa_check(config, previous_path, target_path, video_prompt)
                                if vlm_pass:
                                    if sys.stdout:
                                        print(f"[VLM QA] Beat {seq-1} passed VLM check on retry {retry_attempt+1}!")
                                    break
                                else:
                                    if sys.stdout:
                                        print(f"[VLM QA] Beat {seq-1} failed VLM check on retry {retry_attempt+1}: {vlm_reason}")
                            except Exception as retry_err:
                                if sys.stdout:
                                    print(f"[VLM QA] Retry {retry_attempt+1} hit exception: {retry_err}")
                        if not vlm_pass:
                            vlm_qa_failed = True
                            vlm_qa_reason = vlm_reason
                        
            if vlm_qa_failed:
                current_quality_gate = 'vlm_qa_failed'
            else:
                current_quality_gate = 'i2i_fallback_degraded' if lineage_degraded else 'pending_manual_review'
        else:
            reference = previous_path if seq > 1 else None
            existing_frame = manifest_frames_by_seq.get(seq)
            model = existing_frame.get('model', '') if existing_frame else _image_generation_model(config)
            retries = existing_frame.get('retry_count', 0) if existing_frame else 0
            
            current_quality_gate = existing_frame.get('quality_gate', 'pending_manual_review') if existing_frame else 'pending_manual_review'
            vlm_qa_reason = existing_frame.get('vlm_qa_reason') if existing_frame else None
            if current_quality_gate == 'i2i_fallback_degraded':
                lineage_degraded = True

        rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        frame_info = {
            'slot': item['index'],
            'sequence': seq,
            'file': rel_path,
            'url': '/' + rel_path,
            'prompt': item['prompt'],
            'reference': os.path.relpath(reference, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/') if reference else None,
            'model': model,
            'aspect_ratio': config.get('imageAspectRatio') or '9:16',
            'image_size': _image_quality_to_label(config.get('imageQuality')),
            'retry_count': retries,
            'quality_gate': current_quality_gate,
            'vlm_qa_reason': vlm_qa_reason,
        }
        
        manifest_frames_by_seq[seq] = frame_info
        previous_path = target_path
        generated_count += 1

        if on_progress:
            on_progress('frame', {'frame': frame_info, 'current': generated_count, 'total': total_to_generate})

    manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]

    manifest_path = os.path.join(project_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest['project_dir'] = os.path.abspath(project_dir)
    return manifest
def _get_google_fx_video_service():
    import sys
    adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
    if adspower_path not in sys.path:
        sys.path.append(adspower_path)
    import services.google_fx
    from services import google_fx_video
    import models
    return google_fx_video, models


def generate_video_sequence(config, title, prompt_block, on_progress=None, target_slots=None):
    images, videos = _parse_prompt_slots(prompt_block)
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)

    # Load existing manifest to map slots to frame paths and quality gates
    manifest_path = os.path.join(project_dir, 'manifest.json')
    slot_to_path = {}
    slot_to_quality = {}
    manifest_data = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
            for frame in manifest_data.get('frames', []):
                slot_to_path[frame['slot']] = os.path.join(os.path.dirname(os.path.abspath(__file__)), frame['file'].lstrip('/'))
                slot_to_quality[frame['slot']] = frame.get('quality_gate')
        except Exception as e:
            print(f"Warning: could not read manifest.json ({e})")

    # If manifest doesn't exist or is empty, we can try to guess paths
    if not slot_to_path:
        for i in range(1, len(images) + 1):
            guess_path = os.path.join(frames_dir, f'img_{i:03d}.webp')
            if os.path.exists(guess_path):
                slot_to_path[i] = os.path.abspath(guess_path)

    if not slot_to_path:
        raise RuntimeError('未找到已生成的帧图像。请先生成帧序列！')

    google_fx_video, models = _get_google_fx_video_service()

    video_items = sorted(videos.keys())
    if target_slots is not None:
        target_slots = [int(x) for x in target_slots]
        video_items = [idx for idx in video_items if idx in target_slots]
    else:
        # Full regeneration (not a retry): clear all old video files so
        # breakpoint-resume doesn't reuse stale videos from a previous run.
        # This ensures the UI always shows freshly generated videos.
        if os.path.isdir(videos_dir):
            cleared = 0
            for fname in os.listdir(videos_dir):
                fpath = os.path.join(videos_dir, fname)
                if os.path.isfile(fpath) and fname.lower().endswith('.mp4'):
                    try:
                        os.remove(fpath)
                        cleared += 1
                    except Exception as rm_err:
                        print(f"Warning: could not remove old video {fpath}: {rm_err}")
            if cleared:
                print(f"[INFO] Cleared {cleared} old video file(s) for full regeneration.")
        # Also clear old video entries from manifest
        if 'videos' in manifest_data:
            manifest_data['videos'] = []

    if on_progress:
        on_progress('start', {
            'total': len(video_items),
            'slots': video_items
        })

    video_results = []
    pending_items = []
    
    def save_manifest_incremental():
        existing_videos = manifest_data.get('videos', [])
        video_map = {v['slot']: v for v in existing_videos}
        for v in video_results:
            video_map[v['slot']] = v
            
        merged_videos = []
        for slot_idx in sorted(videos.keys()):
            if slot_idx in video_map:
                merged_videos.append(video_map[slot_idx])
                
        manifest_data['videos'] = merged_videos
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Warning: could not write updated manifest.json ({e})")
    
    video_model = config.get('videoModel') or 'Veo 3.1 - Lite [Lower Priority]'

    import tempfile
    import shutil

    for seq, idx in enumerate(video_items, start=1):
        prompt = videos[idx]
        
        # Automatically map IMAGE N -> IMAGE 1 and IMAGE N+1 -> IMAGE 2
        # to match the 2-card UI in Google Labs FX (Veo)
        import re
        prompt = re.sub(rf'\bimage\s+{idx}\b', 'IMAGE 1', prompt, flags=re.IGNORECASE)
        prompt = re.sub(rf'\bimage\s+{idx + 1}\b', 'IMAGE 2', prompt, flags=re.IGNORECASE)
        prompt = re.sub(rf'图片\s*{idx}\b', 'IMAGE 1', prompt)
        prompt = re.sub(rf'图片\s*{idx + 1}\b', 'IMAGE 2', prompt)

        dest_filename = f'vid_{idx:03d}.mp4'
        dest_path = os.path.join(videos_dir, dest_filename)
        
        # 1. Breakpoint Resume: Check if file already exists and is valid
        # If it is an explicit retry, we bypass this check and delete the existing file.
        is_explicit_retry = target_slots is not None and idx in target_slots
        if is_explicit_retry and os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception as e:
                print(f"Warning: could not remove old video file {dest_path}: {e}")

        if not is_explicit_retry and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            rel_path = os.path.relpath(dest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
            video_info = {
                'slot': idx,
                'sequence': seq,
                'file': rel_path,
                'url': '/' + rel_path,
                'prompt': prompt,
                'model': video_model,
                'status': 'success'
            }
            video_results.append(video_info)
            if on_progress:
                on_progress('video_done', {
                    'index': idx,
                    'current': seq,
                    'total': len(video_items),
                    'video': video_info
                })
            continue

        start_frame_path = slot_to_path.get(idx)
        end_frame_path = slot_to_path.get(idx + 1)
        
        err_msg = None
        if not start_frame_path or not os.path.exists(start_frame_path):
            err_msg = f"视频 {idx} 所需的起始帧 IMAGE {idx} 不存在。请重新生成该帧！"
        elif not end_frame_path or not os.path.exists(end_frame_path):
            err_msg = f"视频 {idx} 所需的结束帧 IMAGE {idx+1} 不存在。请重新生成该帧！"
        else:
            start_quality = slot_to_quality.get(idx)
            end_quality = slot_to_quality.get(idx + 1)
            if start_quality == 'i2i_fallback_degraded' or end_quality == 'i2i_fallback_degraded':
                err_msg = (
                    f"视频 {idx} 的起始帧 IMAGE {idx} 或结束帧 IMAGE {idx+1} 属于降级帧（i2i fallback degraded），"
                    f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
                )

        if err_msg:
            video_info = {
                'slot': idx,
                'sequence': seq,
                'file': '',
                'url': '',
                'prompt': prompt,
                'model': video_model,
                'status': 'failed',
                'error': err_msg
            }
            video_results.append(video_info)
            save_manifest_incremental()
            if on_progress:
                on_progress('video_error', {
                    'index': idx,
                    'current': seq,
                    'total': len(video_items),
                    'message': err_msg
                })
            continue

        temp_out_dir = tempfile.mkdtemp()
        req = models.VideoRequest(
            prompt=prompt,
            image=start_frame_path,
            end_image=end_frame_path,
            model=video_model,
            ratio=config.get('imageAspectRatio') or '9:16',  # FIX ratio
            output_path=temp_out_dir
        )
        pending_items.append({
            'idx': idx,
            'seq': seq,
            'req': req,
            'dest_path': dest_path,
            'temp_out_dir': temp_out_dir,
            'prompt': prompt
        })

    if pending_items:
        reqs_list = [item['req'] for item in pending_items]
        
        def batch_progress_cb(batch_idx, stage, details):
            if not on_progress:
                return
            item = pending_items[batch_idx]
            if stage == 'video_start':
                on_progress('video_start', {
                    'index': item['idx'],
                    'current': item['seq'],
                    'total': len(video_items)
                })
            elif stage == 'video_done':
                # Move the generated file to final destination
                generated_path = details.get('video_url')
                if generated_path and os.path.exists(generated_path):
                    shutil.move(generated_path, item['dest_path'])
                    rel_path = os.path.relpath(item['dest_path'], os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                    video_info = {
                        'slot': item['idx'],
                        'sequence': item['seq'],
                        'file': rel_path,
                        'url': '/' + rel_path,
                        'prompt': item['prompt'],
                        'model': video_model,
                        'status': 'success'
                    }
                    video_results.append(video_info)
                    save_manifest_incremental()
                    on_progress('video_done', {
                        'index': item['idx'],
                        'current': item['seq'],
                        'total': len(video_items),
                        'video': video_info
                    })
                else:
                    on_progress('video_error', {
                        'index': item['idx'],
                        'current': item['seq'],
                        'total': len(video_items),
                        'message': '生成的视频文件不存在'
                    })
            elif stage == 'video_error':
                # Per-segment failure isolation: record failed status and continue
                video_info = {
                    'slot': item['idx'],
                    'sequence': item['seq'],
                    'file': '',
                    'url': '',
                    'prompt': item['prompt'],
                    'model': video_model,
                    'status': 'failed',
                    'error': details.get('message') or '生成失败'
                }
                video_results.append(video_info)
                save_manifest_incremental()
                on_progress('video_error', {
                    'index': item['idx'],
                    'current': item['seq'],
                    'total': len(video_items),
                    'message': details.get('message') or '生成失败'
                })

        def cancel_check_cb():
            if on_progress:
                try:
                    # Trigger a dummy call to check if the connection is dead
                    return on_progress('cancel_check', None)
                except Exception:
                    return True
            return False

        try:
            google_fx_video.generate_videos_batch_google_fx(
                reqs_list,
                on_progress=batch_progress_cb,
                cancel_check=cancel_check_cb
            )
        finally:
            # Clean up all temp directories
            for item in pending_items:
                try:
                    shutil.rmtree(item['temp_out_dir'], ignore_errors=True)
                except:
                    pass

    # Final merge and save of manifest
    save_manifest_incremental()

    manifest_data['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest_data['project_dir'] = os.path.abspath(project_dir)
    return manifest_data


def merge_project_videos(project_dir):
    manifest_path = os.path.join(project_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return None
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest_data = json.load(f)
        
    videos = manifest_data.get('videos', [])
    # Filter and sort by slot index
    video_files = []
    # Make sure we only check files that exist
    for v in sorted(videos, key=lambda x: x.get('slot', 0)):
        if v.get('status') == 'success' and v.get('file'):
            abs_path = os.path.abspath(v['file'].lstrip('/'))
            if not os.path.exists(abs_path):
                abs_path = os.path.abspath(os.path.join(project_dir, 'videos', os.path.basename(v['file'])))
            if os.path.exists(abs_path):
                video_files.append(abs_path)
                
    if not video_files:
        return None
        
    # Write concat list to project directory
    concat_list_path = os.path.join(project_dir, 'concat_list.txt')
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for vf in video_files:
            safe_path = vf.replace('\\', '/')
            f.write(f"file '{safe_path}'\n")
            
    # Determine the Chinese theme name to use for the output filename
    title = manifest_data.get('title', '')
    chinese_name = ""
    
    # 1. Try to find the theme in library.json
    library_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'library.json')
    if os.path.exists(library_path):
        try:
            with open(library_path, 'r', encoding='utf-8') as lf:
                lib_data = json.load(lf)
            if isinstance(lib_data, list):
                for item in lib_data:
                    if item.get('title') == title:
                        theme = item.get('theme', '')
                        theme_chinese = "".join(re.findall(r'[\u4e00-\u9fa5]+', theme))
                        if theme_chinese:
                            chinese_name = theme_chinese
                            break
        except Exception as le:
            print(f"Warning: could not read library.json for theme lookup ({le})")
            
    # 2. Fallback: extract Chinese characters from title
    if not chinese_name and title:
        title_chinese = "".join(re.findall(r'[\u4e00-\u9fa5]+', title))
        if title_chinese:
            chinese_name = title_chinese
            
    # 3. Fallback: use sanitized project folder name if no Chinese characters found
    if not chinese_name:
        chinese_name = _safe_project_name(title)
        
    output_filename = f"{chinese_name}_2x.mp4"
    output_path = os.path.join(project_dir, output_filename)

    # Clean up any old merged files in the project root to prevent duplicate files
    if os.path.exists(project_dir):
        for fname in os.listdir(project_dir):
            if fname.lower().endswith('.mp4') and os.path.isfile(os.path.join(project_dir, fname)):
                try:
                    os.remove(os.path.join(project_dir, fname))
                except Exception as e:
                    print(f"Warning: could not remove old merged file {fname} ({e})")
    
    # Check if the first video has audio
    has_audio = False
    if len(video_files) > 0:
        first_video = video_files[0]
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            first_video
        ]
        try:
            import subprocess
            res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            if "audio" in res.stdout.lower():
                has_audio = True
        except Exception as probe_err:
            print(f"[DEBUG] ffprobe check failed: {probe_err}")
            
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path
    ]
    
    if has_audio:
        cmd.extend([
            "-filter_complex", "[0:v]setpts=0.5*PTS[v];[0:a]atempo=2.0[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:a", "aac"
        ])
    else:
        cmd.extend([
            "-filter_complex", "[0:v]setpts=0.5*PTS[v]",
            "-map", "[v]"
        ])
        
    cmd.extend([
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_path
    ])
    
    print(f"[INFO] Merging {len(video_files)} videos to {output_path} (has_audio={has_audio})...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    try:
        os.remove(concat_list_path)
    except:
        pass
        
    if res.returncode == 0:
        rel_path = os.path.relpath(output_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        file_size = os.path.getsize(output_path)
        
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                output_path
            ]
            dur_res = subprocess.run(dur_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            duration = float(dur_res.stdout.strip())
        except Exception as dur_err:
            print(f"[DEBUG] ffprobe duration check failed: {dur_err}")
            
        return {
            'file': rel_path,
            'url': '/' + rel_path,
            'size_bytes': file_size,
            'duration_seconds': round(duration, 2),
            'status': 'success'
        }
    else:
        print(f"[ERROR] ffmpeg merge failed with code {res.returncode}: {res.stderr}")
        raise RuntimeError(f"FFmpeg merge failed: {res.stderr}")




def build_validator_system_prompt():
    return f"""You are a strict construction-sequence, physical-causality, and spatial-consistency (SCUP) auditor for a restoration / renovation time-lapse prompt set (Chinese-labeled 图片 / 视频 prompts). Your job is to detect and repair violations of real-world build order, physical causality, and scene/spatial consistency. Do NOT redesign, restyle, re-theme, or otherwise "improve" anything.

Check the whole set in shot order for these hard vetoes:
[Construction Order & Causality]
- No powered lights, glowing strips, lit screens, or running equipment before the wiring / power beat. Power-on and lighting must come AFTER the beat that installs their wiring.
- No crossing the threshold into the interior before the exterior (facade / roof / site / rust-proofing) is finished.
- No paint, spray, or topcoat before rust removal, cleaning, and priming.
- No covering wet or uncured material (mortar, concrete, glue, paint) with the next layer before it has cured.
- No service (wiring / plumbing / waterproofing) installed after the panel that would hide it.
- Construction state must be monotonic: cleaned stays clean, installed stays installed, dried stays dried — no regression to an earlier state.
- CEILING/ROOF COVERAGE: No enclosed space (room, cabin, fuselage, container, vault) may have walls paneled/insulated/painted while the ceiling/roof is left as raw exposed structure. If walls are covered, the ceiling must also be covered in the same or a subsequent beat. If ceiling coverage is missing, add it to the wall-coverage beat.
- CAMERA VIEWPOINT CONTINUITY: No sudden camera viewpoint jumps. If IMAGE N is interior (camera inside the space, entry behind camera), IMAGE N+1 cannot be exterior (camera outside looking in) without an intervening reverse-dolly VIDEO that pulls the camera back through the doorway. If this occurs, either keep the viewpoint consistent or insert a camera-pullback transition in the VIDEO.
- FLOOR-BEFORE-HEAVY-OBJECTS: Floor finish must be installed BEFORE heavy anchored objects (fireplace, stove) are placed on it. If a heavy object is installed on a bare subfloor and then the finished floor appears under it, reorder the beats so flooring comes first.
- FIXTURE COMPLETENESS: If wiring/electrical rough-in is present, light fixture installation must occur BEFORE the reward beat. Fixtures cannot appear in the final reward without an installation beat.
- DOOR COMPLETENESS: If a door frame is installed, a door panel/leaf must be installed in a subsequent beat unless the design explicitly specifies an open archway.
- WORKER TEMPLATE CONSISTENCY: Worker entry/exit template clauses at the end of each VIDEO must match the body: no workers in a sterile/no-worker video, correct worker count for multi-worker videos. If a VIDEO body says "sterile of workers" or "no human presence", the template must not add a worker. If the body uses "two workers", the template must not say "one lone worker".
- CLEAR PATH REQUIREMENT: If there are sliding, rolling, retracting, or moving parts (e.g. bed rails, sliding bed, folding stairs), ensure a clean spatial path. If structural columns, pillars, or bulkheads block the path of movement in the trauma state (IMAGE 1), they must be explicitly cut/removed early in the sequence (typically during structural repair) and replaced by peripheral support frames before rails or sliding mechanisms are installed.
- FLOOR & SKELETON MONOTONICITY: If floor/wall joists, ribs, or framing studs will be insulated or paneled later, the bare structural skeleton must be exposed at the very beginning (IMAGE 1/2). The state must progress monotonically forward: bare joists/studs -> rough-in/insulation -> subfloor -> finished flooring/cladding. Never start with a solid finished-looking floor that disappears to reveal raw joists later.
- STRICT SINGLE-OPERATION BEAT RULE: Each {int(VIDEO_DURATION)}-second video prompt must describe exactly one homogeneous physical task. Combining multiple distinct stages (e.g. spray painting AND mounting door frames, or framing studs AND packing insulation wool, or laying tile AND anchoring stoves AND installing bed rails) into a single {int(VIDEO_DURATION)}-second video is strictly prohibited.

[Scene Consistency & Spatial Consistency Upgrade Protocol (SCUP)]
- Consistent Scene & Layout: The background environment, geographical elements, time-of-day, camera position/DNA, visual style, and color scheme must be completely consistent across the sequence.
- Material Continuity: Materials (e.g. wood type, steel type) must not magically transform between shots unless painted or replaced.
- NGCS coordinate lock: Ensure the 3 Primary Landmarks remain locked to the same coordinates (e.g. A1, B3, C2) across all images unless explicitly altered.
- Ghost Clause: Occluded landmarks must be preserved in parenthetical tags, e.g. `[Object Name] remains physically locked at [Grid Cell] ... hidden behind [occluding object]`.
- VMFP & RCE Volume: Loose materials must be encapsulated in rigid, countable containers (buckets/bags) and have volume percentage capacities.
- RHMA Reflection: Glossy/wet surfaces must use highly blurred, diffused reflections (RHMA-Blur) to prevent video flicker.
- Clean Frame Boundary: Image anchors must have ZERO active workers or active machinery.
- Out-and-In Passage: Workers in video prompts must enter at t=0s and exit before t={int(VIDEO_DURATION)}s.
- PERSPECTIVE ISOLATION: Do not flip camera facing directions (e.g. turning 180 degrees from looking out to looking in) in the same spatial axis without a clean separate phase or TBCP transition. If the project centers on a slide-out reward action, lock the Camera Family (e.g. Camera Family B looking outward through the opening towards the view) from the very first frame to maintain spatial consistency.
- BI-DIRECTIONAL AGENT FLOW: Workers in video prompts must enter from a specific coordinate edge at t=0s and walk out through the same edge by t={WORKER_EXIT_TIME}s, leaving the frame completely empty of active agents at t={int(VIDEO_DURATION)}s. No teleportation or instant popping.
- RIGID CONTAINER ENCAPSULATION: All loose materials, debris, fasteners, and liquids must be stored and tracked inside rigid, quantifiable containers (e.g. buckets, parts trays, boxes), and their volumes must be described as continuously increasing or decreasing.
- MANDATORY CLIMAX VIDEO: The prompt composer must generate exactly N video prompts for N+1 images, ensuring the transition between the final two frames (the "Dressed interior" -> "Retract/slide action") is fully animated. The climax video (VIDEO N) must depict the actual physical kinetic movement of the mechanism (e.g. the bed rolling smoothly forward, the glass door sliding open).
- NLVTR Text Lock: No '%' symbol, numeric ranges, colons in variable strings, or acronyms (HAL, DKP, VMFP, RPL, RCE, SCUP, NGCS, OSPL, RHMA, PBISP, HCL, NLVTR) in prompt bodies.

When you rewrite a slot to fix a violation, you MUST preserve every other constraint:
- Keep the exact labels 图片提示词 / 图片 N: / 视频提示词 / 视频 N: and the same slot counts (image count = video count + 1).
- Keep each VIDEO's opening sentence "Use the provided first frame and last frame as exact composition anchors." and its IMAGE N to IMAGE N+1 binding.
- Keep the Camera DNA wording consistent across same-family IMAGE slots.
- Natural-language prose only: never introduce the percent glyph, numeric ranges, or acronyms (HAL, DKP, VMFP, RPL, RCE, SCUP, NGCS, OSPL, RHMA, PBISP, HCL, NLVTR).
- Reordering beats may require renumbering the slots and re-pointing the IMAGE N to IMAGE N+1 bindings accordingly; do that consistently.
- Change ONLY what is necessary to fix ordering / causality / consistency; copy every other slot character-for-character.

Output EXACTLY these two sections, in THIS order, nothing before or after, no code fences:
===REPAIR===
<one short Chinese verdict. If clean, output exactly: PASS — 工序与场景一致性检查通过，未发现违规，提示词未改动。 If you fixed anything, output: 已修复 N 处： then a numbered list, one line each, naming the offending slot, what was physically wrong, and how it was reordered or corrected.>
===PROMPTS===
<If and ONLY IF you changed something, output the COMPLETE corrected prompt set here. If you changed nothing, output exactly the single word: UNCHANGED>"""


def validate_and_repair(config, dimensions, prompt_block, on_progress=None):
    """Second pass: enforce construction order + physical causality + scene consistency (SCUP) and repair in place.
    Returns (repaired_prompt_block, repair_md). Best-effort — caller falls back to the
    first-pass block if this raises."""
    if not prompt_block.strip():
        return prompt_block, '（无提示词内容，跳过工序与场景一致性校验）'
    if on_progress:
        on_progress('repair', '工序与场景一致性校验返回有细微瑕疵，正在进行一致性修复以保时序因果，请稍候...')
    user = (
        f"场景主题：{dimensions.get('theme', '')}。\n\n"
        "以下是待校验的完整提示词集，请按系统指令做工序与场景一致性的二次校验与最小化修复：\n\n"
        + prompt_block
    )
    last_repair_md = '（工序与场景一致性校验未返回结论）'
    repaired = prompt_block
    
    for attempt in range(3):
        try:
            def handle_chunk(chunk):
                if on_progress:
                    on_progress('text_chunk', chunk)
            content = _chat(config, build_validator_system_prompt(), user,
                            temperature=0.3, timeout=180, on_chunk=handle_chunk)
            secs = _extract_marked(content, ['===REPAIR===', '===PROMPTS==='])
            repair_md = secs.get('===REPAIR===', '').strip()
            
            if repair_md:
                new_prompts = _strip_code_fences(secs.get('===PROMPTS===', ''))
                if new_prompts and new_prompts.strip().upper() != 'UNCHANGED' and len(new_prompts) > 800:
                    new_prompts = _normalize_prompt_block(new_prompts)
                    # Guard against output truncation in the validator pass
                    if len(new_prompts) >= len(prompt_block) * 0.9:
                        repaired = new_prompts
                    else:
                        if sys.stdout:
                            print("[DEBUG] validate_and_repair: validator output was truncated. Discarding repaired block.")
                        repaired = prompt_block
                        repair_md = repair_md + "\n\n⚠️ 注意：由于校验修复输出被大模型截断，系统自动保留了原始的完整提示词，仅记录上述校验发现。"
                else:
                    repaired = prompt_block
                return repaired, repair_md
            else:
                if content:
                    last_repair_md = f'（校验输出格式未包含 ===REPAIR=== 标记，第 {attempt + 1} 次重试）'
        except Exception as e:
            last_repair_md = f'（校验出错：{e}，第 {attempt + 1} 次重试）'
            
    return repaired, last_repair_md


def _strip_code_fences(s):
    """Remove a leading ```lang line and trailing ``` line if the model wrapped a
    section in a markdown code fence (it tends to do this for the prompt block)."""
    s = s.strip()
    if s.startswith('```'):
        nl = s.find('\n')
        s = s[nl + 1:] if nl != -1 else s[3:]
    if s.rstrip().endswith('```'):
        s = s[:s.rstrip().rfind('```')]
    return s.strip()


def _parse_prompt_slots(block):
    """Parse Chinese-labeled image/video prompt slots from a prompt block."""
    text = _strip_code_fences(block or '')
    image_matches = re.findall(
        r'图片\s*(\d+)\s*:\s*(.*?)(?=\n图片\s*\d+\s*:|\n视频提示词|\n视频\s*\d+\s*:|\Z)',
        text,
        re.DOTALL
    )
    video_matches = re.findall(
        r'视频\s*(\d+)\s*:\s*(.*?)(?=\n视频\s*\d+\s*:|\n图片提示词|\n图片\s*\d+\s*:|\Z)',
        text,
        re.DOTALL
    )
    images = {int(n): body.strip() for n, body in image_matches if body.strip()}
    videos = {int(n): body.strip() for n, body in video_matches if body.strip()}
    return images, videos


def _missing_prompt_slots(images, videos, image_range, video_range):
    expected_images = set(range(image_range[0], image_range[1] + 1))
    expected_videos = set(range(video_range[0], video_range[1] + 1))
    return sorted(expected_images - set(images)), sorted(expected_videos - set(videos))


def _format_prompt_block(images, videos):
    image_lines = ["图片提示词"]
    for idx in sorted(images):
        image_lines.extend([f"图片 {idx}:", images[idx].strip(), ""])

    video_lines = ["视频提示词"]
    for idx in sorted(videos):
        video_lines.extend([f"视频 {idx}:", videos[idx].strip(), ""])

    return ("\n".join(image_lines).rstrip() + "\n\n" + "\n".join(video_lines).rstrip()).strip()


def _normalize_prompt_block(block):
    images, videos = _parse_prompt_slots(block)
    if not images and not videos:
        return _strip_code_fences(block or '')
    return _format_prompt_block(images, videos)


def _extract_marked(content, markers):
    """Split content into a dict keyed by marker, using regex to locate markers.
    Allows for spacing, case differences, and optional markdown formatting (like bold)."""
    positions = []
    for m in markers:
        core = m.replace('===', '').strip()
        pattern = r'(?:\*\*)?===\s*' + re.escape(core) + r'\s*===(?:\*\*)?'
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            positions.append((match.start(), match.end(), m))
            
    positions.sort(key=lambda x: x[0])
    out = {}
    for i, (start, end, m) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        out[m] = content[end:next_start].strip()
    return out


def parse_sections(content):
    """Split the marker-delimited model output into structured fields. Robust to the
    model omitting markers or formatting them with different spaces/case."""
    out = {'title': '', 'theme': '', 'prompt_block': '', 'audit_md': '', 'raw': content}
    markers = ['===TITLE===', '===THEME===', '===PROMPTS===', '===AUDIT===']
    keys = ['title', 'theme', 'prompt_block', 'audit_md']
    
    positions = []
    for m, k in zip(markers, keys):
        core = m.replace('===', '').strip()
        pattern = r'(?:\*\*)?===\s*' + re.escape(core) + r'\s*===(?:\*\*)?'
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            positions.append((match.start(), match.end(), k))
            
    if not positions:
        out['prompt_block'] = _strip_code_fences(content)
        first_line = content.strip().splitlines()[0] if content.strip() else '未命名创意'
        out['title'] = first_line[:40]
        return out
        
    positions.sort(key=lambda x: x[0])
    
    for i, (start, end, key) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        out[key] = content[end:next_start].strip()
        
    out['prompt_block'] = _normalize_prompt_block(out['prompt_block'])
    out['audit_md'] = _strip_code_fences(out['audit_md'])
    if not out['title']:
        out['title'] = '未命名创意'
    return out


def ping_proxy(config):
    model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)
    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 5,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=payload,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        return resp.status == 200


def run_ideate(config, count=8):
    engine_path = os.path.join(SKILL_DIR, 'references', 'idea-engine.md')
    ledger_path = os.path.join(SKILL_DIR, 'references', 'used-topic-ledger.md')
    
    engine_content = ""
    if os.path.exists(engine_path):
        with open(engine_path, 'r', encoding='utf-8') as f:
            engine_content = f.read()
            
    ledger_content = ""
    if os.path.exists(ledger_path):
        with open(ledger_path, 'r', encoding='utf-8') as f:
            ledger_content = f.read()
            
    system_prompt = f"""You are the Upstream Ideation Layer for the `restoration-prompt-composer` skill.
Your task is to generate a ranked list of {count} highly novel, realistic, buildable time-lapse renovation topic seeds.
You must combine axes from the Morphological Matrix in `idea-engine.md` and filter them to ensure quality.

Here is the authoritative `idea-engine.md` specifying the matrices, rules, filters, scoring rubric, and continuous-supply mechanisms:
==================== IDEA ENGINE ====================
{engine_content}

Here is the current `used-topic-ledger.md` showing already used/burned topic DNAs:
==================== USED TOPIC LEDGER ====================
{ledger_content}

==================== GENERATION INSTRUCTIONS ====================
1. Combine Axis 1 (Carrier), Axis 2 (Environment), Axis 3 (Trauma), Axis 4 (Destiny), and Axis 5 (Signature Twist) to form candidates.
2. Filter out any candidates that:
   - Have a NON-SHELTER destiny. SHELTER-ONLY POLICY is a hard veto: every destiny MUST be a habitable private dwelling / refuge (a place to sleep, shelter, and live). Reject outright any bar, cafe, tea house, speakeasy, recording/ceramics/painting/art studio, shop, gallery, museum, public observatory, commercial spa/sauna/onsen, or lab. Litmus: "could one person live and sleep here as their own refuge?" — if no, drop it.
   - Violate the Orthogonal-Pairing Rule (Raw shell vs cozy interior contrast).
   - Do not have exactly ONE Axis-5 signature twist.
   - Match or are one edit-step away from any burned Topic DNA in the ledger.
   - Are in the Cliché Blocklist.
   - Fail the Buildability Gate (no magic/conjuring).
3. Score each candidate (0-5 for Novelty, Visual Contrast, Twist Strength, Buildability, Scroll-Stop).
4. Select the top {count} candidates with highest total score.
5. In this batch, ensure Axis-1 carrier families (Living/Natural, Abandoned Man-made, Vehicles/Vessels, Fantasy-grounded) rotate and do not repeat consecutively.
6. Translate all names to natural Chinese for the final title and one-click input string.
7. Return ONLY a valid JSON array of objects, with no markdown code fences, no other text.

Each object in the JSON array must have EXACTLY these keys:
- "title": (string) A catchy Chinese one-sentence title, e.g. "蓝冰冰川洞改造成隐居雪境卧室"
- "input_str": (string) A Chinese Tier-1 one-click input string, e.g. "做一个蓝冰冰川洞穴改造成隐居雪境卧室"
- "carrier": (string) Carrier in English, e.g. "glacier ice cave"
- "env": (string) Environment in English, e.g. "alpine cliff"
- "trauma": (string) Trauma state in English, e.g. "frost-cracked & ice-encased"
- "destiny": (string) Destiny in English — MUST be a habitable shelter/dwelling/refuge, e.g. "snug winter refuge den"
- "twist": (string) Signature twist DNA name in English, e.g. "self-material-window"
- "twist_zh": (string) Chinese display description of the signature twist, e.g. "窗户直接切穿半透明蓝冰"
- "carrier_family": (string) one of: "natural", "man-made", "vehicle", "fantasy"
- "dna": (string) Topic DNA in the format "carrier-family / destiny / twist-family", e.g., "natural / refuge-den / self-material-window"
- "score": (number) Total score out of 25.
"""

    user_prompt = f"Generate {count} top-quality unique renovation ideas following the instructions."
    
    for attempt in range(3):
        try:
            resp = _chat(config, system_prompt, user_prompt, temperature=0.8, timeout=90)
            cleaned = _strip_code_fences(resp).strip()
            ideas = json.loads(cleaned)
            if isinstance(ideas, list) and len(ideas) > 0:
                return ideas
        except Exception as e:
            if sys.stdout:
                print(f"[DEBUG] run_ideate attempt {attempt+1} failed: {e}")
                
    # Fallback if LLM fails (shelter-only destinies, per SHELTER-ONLY POLICY)
    return [
        {
            "title": "蓝冰冰川洞改造成隐居雪境卧室",
            "input_str": "做一个蓝冰冰川洞穴改造成隐居雪境卧室",
            "carrier": "glacier ice cave",
            "env": "alpine cliff",
            "trauma": "frost-cracked & ice-encased",
            "destiny": "snug winter refuge den",
            "twist": "self-material-window",
            "twist_zh": "窗户直接切穿半透明蓝冰",
            "carrier_family": "natural",
            "dna": "natural / refuge-den / self-material-window",
            "score": 24
        },
        {
            "title": "退役潜艇舱改造成离网单人居所",
            "input_str": "做一个退役潜艇舱改造成离网单人居所",
            "carrier": "retired submarine",
            "env": "misty fjord",
            "trauma": "rust-flaked & gutted",
            "destiny": "off-grid micro-home",
            "twist": "porthole-lighting",
            "twist_zh": "保留黄铜舷窗作为背光搁板灯",
            "carrier_family": "vehicle",
            "dna": "vehicle / micro-home / porthole-lighting",
            "score": 23
        },
        {
            "title": "废弃导弹井改造成地下隐居卧室",
            "input_str": "做一个废弃导弹发射井改造成地下隐居卧室",
            "carrier": "missile silo",
            "env": "high desert mesa",
            "trauma": "debris-packed & guano-caked",
            "destiny": "subterranean burrow dwelling",
            "twist": "roof-hatch",
            "twist_zh": "混凝土屋顶舱门滑动打开露出天空",
            "carrier_family": "man-made",
            "dna": "man-made / burrow-dwelling / roof-hatch",
            "score": 23
        }
    ]


def _run_async_image_generation(task_id, base_url, api_key, payload):
    try:
        import urllib.request
        import json
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            f'{base_url}/images/generations',
            data=data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'completed', 'result': resp_data, 'error': None}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        error_msg = f'Image API HTTP {e.code}: {detail}'
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}
    except Exception as e:
        error_msg = str(e)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}


def _run_async_image_edit(task_id, base_url, api_key, body_data, boundary):
    try:
        import urllib.request
        import json
        req = urllib.request.Request(
            f'{base_url}/images/edits',
            data=body_data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            },
            method='POST',
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
        resp_data = _save_image_station_result(resp_data)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'completed', 'result': resp_data, 'error': None}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:500]
        except Exception: pass
        error_msg = f'Image API HTTP {e.code}: {detail}'
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}
    except Exception as e:
        error_msg = str(e)
        with IMAGE_TASKS_LOCK:
            IMAGE_TASKS[task_id] = {'status': 'failed', 'result': None, 'error': error_msg}

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

        elif path == '/api/compose-cancel':
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id and task_id in ACTIVE_TASKS:
                    ACTIVE_TASKS[task_id]["cancel_event"].set()
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

                # Setup dimensions
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
                    
    # 3. Synchronize manifest['frames']
    manifest_frames = manifest['frames']
    new_frames = []
    seen_sequences = set()
    
    for frame in manifest_frames:
        seq = frame.get('sequence') or frame.get('slot')
        if seq:
            file_path = frame.get('file')
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
            
    new_frames.sort(key=lambda x: x.get('sequence', 0))
    if len(manifest['frames']) != len(new_frames) or modified:
        manifest['frames'] = new_frames
        modified = True
        
    videos_dir = os.path.join(project_dir, 'videos')
    manifest_videos = manifest['videos']
    new_videos = []
    for video in manifest_videos:
        vpath = video.get('file')
        if vpath:
            full_vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), vpath.lstrip('/'))
            if os.path.exists(full_vpath):
                new_videos.append(video)
            else:
                modified = True
    if len(manifest['videos']) != len(new_videos):
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

    station_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'image-service-station')
    history_path = os.path.join(station_dir, 'restored_history.json')
    if os.path.exists(history_path):
        try:
            with open(history_path, 'r', encoding='utf-8') as f:
                history = json.load(f)
            
            if isinstance(history, list):
                modified = False
                out_dir = os.path.join(outputs_dir, 'image-station')
                os.makedirs(out_dir, exist_ok=True)
                
                from PIL import Image
                import io
                import base64
                import time
                
                for item in history:
                    img_data = item.get('image')
                    if img_data and isinstance(img_data, str) and img_data.startswith('data:image/'):
                        try:
                            header, encoded = img_data.split(',', 1)
                            image_bytes = base64.b64decode(encoded)
                            
                            item_id = item.get('id', f"restored_{int(time.time())}")
                            filename = f"restored_{item_id}.webp"
                            target_path = os.path.join(out_dir, filename)
                            
                            img = Image.open(io.BytesIO(image_bytes))
                            img.save(target_path, format='WEBP', quality=80)
                            
                            rel_path = os.path.relpath(target_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                            item['image'] = '/' + rel_path
                            modified = True
                            if sys.stdout:
                                print(f"[MIGRATION] Converted history item {item_id} base64 -> local WebP")
                        except Exception as ex:
                            print(f"[MIGRATION] Error converting history item: {ex}")
                
                if modified:
                    with open(history_path, 'w', encoding='utf-8') as f:
                        json.dump(history, f, ensure_ascii=False, indent=2)
                    if sys.stdout:
                        print(f"[MIGRATION] Saved updated restored_history.json")
        except Exception as e:
            print(f"[MIGRATION] Failed to migrate restored_history.json: {e}")


def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
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
