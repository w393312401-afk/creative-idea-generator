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


def _int_setting(env_key, cfg_key, default):
    """数值配置的容错解析：非法值给出中文告警并回退默认，而不是 import 时崩掉整个服务。"""
    raw = os.environ.get(env_key, SERVER_CONFIG.get(cfg_key, default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        if sys.stdout:
            print(f"[WARN] 配置项 {cfg_key}/{env_key} 的值 {raw!r} 不是有效整数，已回退默认值 {default}")
        return default


RATE_MAX = _int_setting('SPARK_RATE_MAX', 'rateMax', 20)
RATE_WINDOW = _int_setting('SPARK_RATE_WINDOW', 'rateWindow', 3600)

PORT = _int_setting('PORT', 'port', 8085)
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

# AdsPower 自动化脚本目录：可用 server_config.json 的 adspowerPath 覆盖。
# 旧代码把这个绝对路径连同 3 行 sys.path 注入复制粘贴在 5 个调用点。
_ADSPOWER_DEFAULT_PATH = 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'


def ensure_adspower_on_path():
    """把 AdsPower 脚本目录加入 sys.path（幂等），返回实际使用的路径。"""
    p = SERVER_CONFIG.get('adspowerPath') or _ADSPOWER_DEFAULT_PATH
    if p not in sys.path:
        sys.path.append(p)
    return p


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
    # cheapModel/auxModel 此前在托管模式下被静默丢弃（example 配置里承诺了
    # cheapModel）——与已修复过的 imageEditFallbackModel 漏传属同一类 bug
    for k in ('geminiDirectApiKey', 'geminiApiKey', 'geminiDirectImageModel', 'cheapModel', 'auxModel'):
        if SERVER_CONFIG.get(k):
            merged[k] = SERVER_CONFIG.get(k)
    if ALLOW_CLIENT_MODEL:
        if client_config.get('model'):
            merged['model'] = client_config['model']
        if client_config.get('imageModel'):
            merged['imageModel'] = client_config['imageModel']
        if client_config.get('imageEditFallbackModel'):
            merged['imageEditFallbackModel'] = client_config['imageEditFallbackModel']
        for k in ('cheapModel', 'auxModel'):
            if client_config.get(k):
                merged[k] = client_config[k]
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

# library.json 读写共用一把锁：Windows 上 os.replace 会因并发打开的读句柄抛
# PermissionError，读路径也必须串行化，否则存在整库清零风险
LIBRARY_LOCK = threading.Lock()

# Async image-station generation/edit tasks (polled via the /api/image-station endpoints)
IMAGE_TASKS = {}
IMAGE_TASKS_LOCK = threading.Lock()


def write_json_atomic(path, data, indent=2):
    """写 JSON 的原子替换版本：先写同目录 .tmp 再 os.replace。

    进程中途崩溃/断电不会留下半截 JSON。Windows 上目标文件被并发打开时
    os.replace 可能瞬时抛 PermissionError，重试一次后仍失败则记录告警并
    抛出，由调用方决定是否容忍。
    """
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    try:
        os.replace(tmp_path, path)
    except (OSError, PermissionError):
        time.sleep(0.15)
        try:
            os.replace(tmp_path, path)
        except (OSError, PermissionError) as e:
            if sys.stdout:
                print(f"[WARN] 原子写入 {path} 失败（文件可能被占用）: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


# manifest.json 的每项目写锁：三个代码路径（同步器/帧生成/视频生成）此前
# 各自整读整写同一份 manifest，无锁竞争会互相覆盖对方的字段
_MANIFEST_LOCKS = {}
_MANIFEST_LOCKS_GUARD = threading.Lock()


def manifest_lock(project_dir):
    key = os.path.normcase(os.path.abspath(project_dir))
    with _MANIFEST_LOCKS_GUARD:
        lock = _MANIFEST_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MANIFEST_LOCKS[key] = lock
        return lock


def read_manifest(project_dir):
    """读取项目 manifest.json；损坏/缺失时返回 None（调用方自行决定降级策略）。"""
    manifest_path = os.path.join(project_dir, 'manifest.json')
    with manifest_lock(project_dir):
        if not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            if sys.stdout:
                print(f"[WARN] manifest.json 损坏或不可读 ({manifest_path}): {e}")
            return None


def write_manifest(project_dir, data):
    manifest_path = os.path.join(project_dir, 'manifest.json')
    with manifest_lock(project_dir):
        try:
            write_json_atomic(manifest_path, data)
        except (OSError, PermissionError):
            # 已在 write_json_atomic 里告警；manifest 写失败不应炸掉生成任务，
            # 下一次写入（每帧后都会写）会自然补上
            pass

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
                # 原子替换：任务文件写一半崩溃会留下半截 JSON，重启加载时整个任务丢失
                write_json_atomic(os.path.join("tasks", f"{tid}.json"), task_data)
            except Exception as e:
                if sys.stdout:
                    print(f"Error saving task {tid} to disk: {e}")

        # 清理必须留在同一把锁内：旧写法在锁外用过期快照删文件，
        # 期间其他线程新建的任务文件会被当成孤儿误删
        try:
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

