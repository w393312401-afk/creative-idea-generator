import os
import sys
import json
import socket
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
import threading
import time
import re

class DummyWriter:
    def write(self, *args, **kwargs):
        pass
    def flush(self, *args, **kwargs):
        pass

class RotatingFileStream:
    def __init__(self, filepath, max_bytes=2*1024*1024, backup_count=3, encoding='utf-8'):
        self.filepath = filepath
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.encoding = encoding
        self.lock = threading.Lock()
        self.file = None
        self._open()

    def _open(self):
        try:
            self.file = open(self.filepath, 'a', encoding=self.encoding, buffering=1)
        except Exception:
            self.file = None

    def write(self, data):
        with self.lock:
            if not self.file:
                return 0
            try:
                # Calculate if we need to rotate
                encoded_len = len(data.encode(self.encoding, errors='ignore'))
                if os.path.exists(self.filepath) and os.path.getsize(self.filepath) + encoded_len > self.max_bytes:
                    self._rotate()
                if self.file:
                    self.file.write(data)
                    self.file.flush()
            except Exception:
                pass
            return len(data)

    def flush(self):
        with self.lock:
            if self.file:
                try:
                    self.file.flush()
                except Exception:
                    pass

    def _rotate(self):
        if self.file:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None
        for i in range(self.backup_count - 1, 0, -1):
            sfn = f"{self.filepath}.{i}"
            dfn = f"{self.filepath}.{i+1}"
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    try:
                        os.remove(dfn)
                    except Exception:
                        pass
                try:
                    os.rename(sfn, dfn)
                except Exception:
                    pass
        dfn = f"{self.filepath}.1"
        if os.path.exists(self.filepath):
            if os.path.exists(dfn):
                try:
                    os.remove(dfn)
                except Exception:
                    pass
            try:
                os.rename(self.filepath, dfn)
            except Exception:
                pass
        self._open()

class _Tee:
    """Write to several streams at once (e.g. the real console + the log file),
    swallowing per-stream errors so one broken stream never crashes the server."""
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        # Global debug filter
        if "[DEBUG]" in data and not DEBUG_MODE:
            return len(data)
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

# Establish the log path and redirect stdout/stderr
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.log')
_rotating_log = RotatingFileStream(_LOG_PATH, max_bytes=2*1024*1024, backup_count=3, encoding='utf-8')
if _rotating_log.file:
    _rotating_log.file.write(f"\n===== SPARK server log opened {datetime.now().isoformat()} =====\n")
    _rotating_log.file.flush()

# Load config early so we know DEBUG_MODE
SERVER_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_config.json')

def _load_server_config():
    cfg = {}
    if os.path.exists(SERVER_CONFIG_FILE):
        try:
            with open(SERVER_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
        except Exception as e:
            # sys.stdout may not be Tee-ed yet, print to standard stderr
            sys.stderr.write(f"Warning: could not read server_config.json ({e})\n")
    env_map = {
        'baseUrl': 'SPARK_BASE_URL', 'apiKey': 'SPARK_API_KEY', 'model': 'SPARK_MODEL',
        'imageModel': 'SPARK_IMAGE_MODEL', 'accessCode': 'SPARK_ACCESS_CODE',
        'geminiDirectApiKey': 'GEMINI_API_KEY', 'geminiDirectImageModel': 'GEMINI_IMAGE_MODEL',
        'codexApiKey': 'CODEX_API_KEY',
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            cfg[k] = v
    return cfg

SERVER_CONFIG = _load_server_config()
DEBUG_MODE = bool(SERVER_CONFIG.get('debug') or os.environ.get('SPARK_DEBUG'))

sys.stdout = _Tee(sys.stdout, _rotating_log) if (sys.stdout or _rotating_log.file) else DummyWriter()
sys.stderr = _Tee(sys.stderr, _rotating_log) if (sys.stderr or _rotating_log.file) else DummyWriter()

SERVER_MANAGED = bool(SERVER_CONFIG.get('apiKey'))
ALLOW_CLIENT_MODEL = SERVER_CONFIG.get('allowClientModel', True) is not False
ACCESS_CODE = (SERVER_CONFIG.get('accessCode') or '').strip()
RATE_MAX = int(os.environ.get('SPARK_RATE_MAX', SERVER_CONFIG.get('rateMax', 20) or 20))
RATE_WINDOW = int(os.environ.get('SPARK_RATE_WINDOW', SERVER_CONFIG.get('rateWindow', 3600) or 3600))

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
SKILL_DIR = os.environ.get(
    'SKILL_DIR',
    os.path.join(os.path.expanduser('~'), '.codex', 'skills', 'restoration-prompt-composer')
)

def effective_config(client_config):
    client_config = client_config or {}
    if not SERVER_MANAGED:
        return client_config
    merged = {
        'baseUrl': SERVER_CONFIG.get('baseUrl') or 'http://127.0.0.1:8046/v1',
        'apiKey': SERVER_CONFIG.get('apiKey') or '',
        'model': SERVER_CONFIG.get('model') or 'gemini-3-flash-agent',
        'imageModel': SERVER_CONFIG.get('imageModel') or 'gemini-3.1-flash-image',
    }
    if SERVER_CONFIG.get('imageEditFallbackModel'):
        merged['imageEditFallbackModel'] = SERVER_CONFIG.get('imageEditFallbackModel')
    for k in ('geminiDirectApiKey', 'geminiApiKey', 'geminiDirectImageModel'):
        if SERVER_CONFIG.get(k):
            merged[k] = SERVER_CONFIG.get(k)
    if ALLOW_CLIENT_MODEL:
        if client_config.get('model'):
            merged['model'] = client_config['model']
        if client_config.get('imageModel'):
            merged['imageModel'] = client_config['imageModel']
        if client_config.get('imageEditFallbackModel'):
            merged['imageEditFallbackModel'] = client_config['imageEditFallbackModel']
    for k in ('imageAspectRatio', 'imageQuality', 'imageBackend', 'googleFxImageModel', 'videoModel', 'googleFxIpRotateRequests'):
        if client_config.get(k):
            merged[k] = client_config[k]
    for k in ('geminiDirectApiKey', 'geminiApiKey', 'geminiDirectImageModel'):
        if client_config.get(k):
            merged[k] = client_config[k]
    return merged

def resolve_gateway(model_name, config):
    base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
    api_key = config.get('apiKey') or ''
    m_lower = (model_name or '').lower()
    if 'gpt-5' in m_lower or 'codex' in m_lower or 'gpt-image-2' in m_lower:
        base_url = 'http://127.0.0.1:65038/v1'
        api_key = config.get('codexApiKey') or SERVER_CONFIG.get('codexApiKey') or ''
        if not api_key and sys.stdout:
            print("Warning: codex-routed model requested but no codexApiKey configured in server_config.json")
    return base_url, api_key


def _safe_project_name(title):
    raw = (title or 'spark_frames').strip()
    import hashlib
    import unicodedata
    normalized = unicodedata.normalize('NFKC', raw)
    # 保留中文主题作为本地项目目录名，同时过滤 Windows 禁用字符和 URL 高风险符号。
    sanitized = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', normalized)
    sanitized = re.sub(r'[#?%&+=;,@!`~^()\[\]{}]+', '_', sanitized)
    sanitized = re.sub(r'\s+', '_', sanitized)
    sanitized = re.sub(r'_+', '_', sanitized).strip(' ._-')
    title_hash = hashlib.md5(raw.encode('utf-8', errors='ignore')).hexdigest()

    if not sanitized:
        return title_hash[:12]
    return sanitized[:60].rstrip(' ._-') or title_hash[:12]


def _legacy_ascii_project_name(title):
    raw = (title or 'spark_frames').strip()
    import hashlib
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', raw)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_-')
    title_hash = hashlib.md5(raw.encode('utf-8', errors='ignore')).hexdigest()
    if not sanitized:
        return title_hash[:12]
    return f"{sanitized[:40]}_{title_hash[:6]}"


def _get_project_dir(title):
    # 1. Try new Chinese-preserving naming scheme
    new_name = _safe_project_name(title)
    new_dir = os.path.join(OUTPUT_ROOT, new_name)
    if os.path.exists(new_dir):
        return new_dir

    # 2. Try legacy ASCII+hash naming scheme used before Chinese folder names
    legacy_dir = os.path.join(OUTPUT_ROOT, _legacy_ascii_project_name(title))
    if os.path.exists(legacy_dir):
        return legacy_dir

    # 3. Try old raw-title naming scheme
    old_raw = (title or 'spark_frames').strip()
    old_raw = re.sub(r'[\\/:*?"<>|]+', '_', old_raw)
    old_raw = re.sub(r'\s+', '_', old_raw)
    old_name = old_raw
    old_dir = os.path.join(OUTPUT_ROOT, old_name)
    if os.path.exists(old_dir):
        return old_dir

    # 4. Default to new naming scheme path if neither exists (for creation)
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


# --- Task Management State and Helpers ---
PACKET_CACHE_LOCK = threading.RLock()
PROCESS_BRIEF_CACHE_LOCK = threading.RLock()
ACTIVE_TASKS = {}
ACTIVE_TASKS_LOCK = threading.RLock()

# Async image-station generation/edit tasks (polled via the /api/image-station endpoints)
IMAGE_TASKS = {}
IMAGE_TASKS_LOCK = threading.Lock()

def save_tasks_to_disk():
    # Save individual tasks to tasks/ folder
    os.makedirs("tasks", exist_ok=True)
    with ACTIVE_TASKS_LOCK:
        active_ids = set(ACTIVE_TASKS.keys())
        for tid, t in ACTIVE_TASKS.items():
            task_data = {
                "id": t["id"],
                "status": t["status"],
                "events": t["events"],
                "dimensions": t["dimensions"],
                "result": t.get("result"),
                "error": t.get("error"),
                "last_active": t["last_active"]
            }
            try:
                filepath = os.path.join("tasks", f"{tid}.json")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(task_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                if sys.stdout:
                    print(f"Error saving task {tid} to disk: {e}")
                    
    # Delete task files on disk for tasks no longer present in memory
    try:
        if os.path.exists("tasks"):
            for filename in os.listdir("tasks"):
                if filename.endswith(".json"):
                    tid = filename[:-5]
                    if tid not in active_ids:
                        try:
                            os.remove(os.path.join("tasks", filename))
                        except OSError:
                            pass
    except Exception as e:
        if sys.stdout:
            print(f"Error cleaning up task files: {e}")


def load_tasks_from_disk():
    global ACTIVE_TASKS
    # Backward compatibility: migrate monolithic tasks.json if present
    if os.path.exists("tasks.json") and not os.path.exists("tasks"):
        try:
            with open("tasks.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            os.makedirs("tasks", exist_ok=True)
            for tid, t in data.items():
                filepath = os.path.join("tasks", f"{tid}.json")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(t, f, ensure_ascii=False, indent=2)
            os.remove("tasks.json")
        except Exception as e:
            if sys.stdout:
                print(f"Error migrating tasks.json to tasks/ folder: {e}")

    if not os.path.exists("tasks"):
        return

    try:
        with ACTIVE_TASKS_LOCK:
            for filename in os.listdir("tasks"):
                if filename.endswith(".json"):
                    tid = filename[:-5]
                    filepath = os.path.join("tasks", filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            t = json.load(f)
                        
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
                            print(f"Error loading task file {filename}: {e}")
    except Exception as e:
        if sys.stdout:
            print(f"Error reading tasks directory: {e}")


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
            if t["status"] in ("completed", "failed", "cancelled") and now - t["last_active"] > 604800:
                to_delete.append(tid)
        for tid in to_delete:
            del ACTIVE_TASKS[tid]
    if to_delete:
        save_tasks_to_disk()


def ping_proxy(config):
    model = config.get('model') or 'gemini-3-flash-agent'
    base_url, api_key = resolve_gateway(model, config)
    req = urllib.request.Request(
        f'{base_url}/models',
        headers={'Authorization': f'Bearer {api_key}'},
        method='GET',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        # Some OpenAI-compatible proxies do not expose /models, but any HTTP
        # response still proves the local gateway is reachable.
        return e.code in (404, 405)
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return False
    except Exception:
        return False


def ping_model_completion(config):
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
    with opener.open(req, timeout=8) as resp:
        return resp.status == 200

