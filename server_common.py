import os
import sys
import json
import socket
import contextlib
import shutil
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
import collections
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
        # print(x) 对同一条语句会拆成两次独立的 write() 调用（消息一次，末尾的
        # "\n" 再一次；多参数还会更多次）——每次 write() 各自加锁，但两次之间
        # 没有互斥。服务端多线程（每个任务一个 worker、每个 HTTP 请求也各自
        # 一个线程）并发打印时，一个线程的消息体和它自己的换行符之间，另一个
        # 线程的完整输出可能插进来，落到磁盘上变成两条日志绞在一起的一整段
        # 乱码——这正是"报错是一串乱字符"的根因，不是编码问题。
        # 修法：按线程 id 缓冲，凑够一个完整行（遇到 "\n"）才真正落盘，保证
        # "调用方的一条 print() = 磁盘上一段连续、不被别的线程插队的字节"。
        self._line_buffers = {}
        self._line_buffers_lock = threading.Lock()
        self._open()

    def _open(self):
        try:
            self.file = open(self.filepath, 'a', encoding=self.encoding, buffering=1)
        except Exception:
            self.file = None

    def write(self, data):
        if not isinstance(data, str) or not data:
            return 0
        ident = threading.get_ident()
        with self._line_buffers_lock:
            buf = self._line_buffers.get(ident, '') + data
            if '\n' not in buf:
                self._line_buffers[ident] = buf
                return len(data)
            *complete_lines, remainder = buf.split('\n')
            if remainder:
                self._line_buffers[ident] = remainder
            else:
                self._line_buffers.pop(ident, None)
        for line in complete_lines:
            self._write_line(line + '\n')
        return len(data)

    def _write_line(self, line):
        with self.lock:
            if not self.file:
                return
            try:
                # Calculate if we need to rotate
                encoded_len = len(line.encode(self.encoding, errors='ignore'))
                if os.path.exists(self.filepath) and os.path.getsize(self.filepath) + encoded_len > self.max_bytes:
                    self._rotate()
                if self.file:
                    self.file.write(line)
                    self.file.flush()
            except Exception:
                pass

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
# server.log 是全服务唯一的日志落点：所有 print/log()、所有任务的进度事件
# （见 notify_listeners 的镜像）、以及后台启动时进程本身的 fd1/fd2（run.sh 直接
# 重定向到这里）都汇到这一个文件，前端日志面板 tail 的也是它。
# SPARK_LOG_FILE 可以显式改落点（外部部署想把日志接到别处时用）。
_LOG_PATH = os.environ.get('SPARK_LOG_FILE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'server.log')

# 测试进程不碰生产日志。pytest 把 server_common 当普通模块导入，光是这一次
# import 就会往 server.log 追加一条启动横幅——跑一天测试能攒几十条（本次改造前
# 实测文件里有 63 条，绝大多数是测试留下的），把真正的服务日志冲得七零八落。
# 既然目标是"所有日志集中到一个文件"，这个文件就不该混进测试噪音。
_UNDER_PYTEST = 'pytest' in sys.modules

# 容量：任务进度事件并进来之后单位时间行数明显变多，档位从 2MB×3 提到 8MB×2——
# 总量翻倍的同时把轮转兄弟文件从 3 个减到 2 个，符合"日志尽量集中在一处"。
if _UNDER_PYTEST:
    _rotating_log = None
else:
    _rotating_log = RotatingFileStream(_LOG_PATH, max_bytes=8*1024*1024, backup_count=2, encoding='utf-8')
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
        'codexApiKey': 'CODEX_API_KEY', 'codexBaseUrl': 'CODEX_BASE_URL',
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            cfg[k] = v
    return cfg

SERVER_CONFIG = _load_server_config()
DEBUG_MODE = bool(SERVER_CONFIG.get('debug') or os.environ.get('SPARK_DEBUG'))

def _console_stream(stream):
    """只有真正连着终端时才把日志同时打到控制台。
    后台启动时进程的 fd1/fd2 已经被 shell 重定向到日志文件了，这里再 tee 一份
    等于同一条日志落两个文件——旧版 server.log 与 server_nohup.log 内容几乎完全
    重复就是这么来的（run.sh 的 `nohup ... > server_nohup.log 2>&1`）。
    现在 run.sh 直接把 fd1/fd2 指向 server.log，本函数保证 Python 这一侧不再
    重复写一遍，两路合起来正好是一份完整、不重样的日志。"""
    try:
        return stream if (stream and stream.isatty()) else None
    except Exception:
        return None


if _rotating_log and _rotating_log.file:
    sys.stdout = _Tee(_console_stream(sys.stdout), _rotating_log)
    sys.stderr = _Tee(_console_stream(sys.stderr), _rotating_log)
else:
    # 轮转日志没能打开（磁盘满/权限）：保持进程原有的 stdout/stderr，让输出
    # 至少还能落进 shell 的重定向，别丢进黑洞。pythonw 下它们本来就是 None，
    # 必须垫一个空写入器，否则第一次 print() 直接炸。
    if sys.stdout is None:
        sys.stdout = DummyWriter()
    if sys.stderr is None:
        sys.stderr = DummyWriter()


# --- Unified structured logging --------------------------------------------
# 排查问题此前得在 server.log（裸 print，无时间戳，所有任务的输出混在一起）
# 和某个任务自己的 SSE 事件流之间来回对照才能拼出时间线。log() 给每行补上
# 一致的 时间戳/级别/子系统标签/任务 id，前端日志面板据此可以按级别/任务过滤
# （见 initLocalServiceLogs）。仍然只是 print()，照常经过上面的 _Tee 落盘到
# server.log + 控制台，不引入新的输出通道；没迁移到这里的旧 print() 不受影响，
# 可以逐步迁移，不必一次性改完。
_LOG_CONTEXT = threading.local()

LOG_LEVELS = ('DEBUG', 'INFO', 'WARN', 'ERROR')


def set_log_context(task_id):
    """注册（传 None=清除）当前线程正在处理的任务 id。log() 据此自动打
    [task=xxx] 标签——worker 线程入口调一次（与 set_upstream_event_sink /
    set_cancel_check_sink 同一注册点），内部所有 log() 调用不用逐层传参。"""
    _LOG_CONTEXT.task_id = task_id


def log(level, tag, msg, task_id=None, **fields):
    """结构化输出一行日志：HH:MM:SS.mmm [LEVEL] [tag] [task=xxx] message k=v ...
    DEBUG 级别在非 DEBUG_MODE 下直接跳过（不落盘也不进控制台，与旧版
    _Tee 对裸 "[DEBUG]" 前缀的过滤是同一个开关）。

    task_id 通常不用传——worker 线程用 set_log_context 注册一次，这里自动
    读线程局部值；只有跨线程记一次性事件（例如 HTTP 请求线程收到取消请求，
    要记的是"哪个任务"而不是"当前线程属于哪个任务"）才需要显式传它，
    这样仍能落进同一个 [task=xxx] 标签格式，前端按任务过滤时不用认两种写法。"""
    level = level.upper()
    if level == 'DEBUG' and not DEBUG_MODE:
        return
    ts = time.strftime('%H:%M:%S') + f".{int((time.time() % 1) * 1000):03d}"
    tid = task_id or getattr(_LOG_CONTEXT, 'task_id', None)
    head = f"{ts} [{level:<5}] [{tag}]"
    if tid:
        head += f" [task={tid}]"
    extra = (' ' + ' '.join(f'{k}={v}' for k, v in fields.items())) if fields else ''
    print(f"{head} {msg}{extra}")


def log_exception(tag, prefix='', task_id=None):
    """记录当前异常的完整堆栈——但只在 DEBUG_MODE 下才真正落盘/进控制台。
    调用点通常已经在 except 块里打了一条 ERROR 级别的一行摘要（"XX 任务失败: {e}"），
    这里补的是"要深挖根因时才需要"的完整 traceback：平时日志只看到那一行摘要，
    不会被裸 traceback.print_exc() 糊得满屏都是看不出主线；开 DEBUG_MODE 后
    这行会带上和其它日志一致的 时间戳/级别/task 标签，而不是一段无标签的裸文本。"""
    import traceback
    tb = traceback.format_exc()
    log('DEBUG', tag, f"{prefix}\n{tb}".strip(), task_id=task_id)


SERVER_MANAGED = bool(SERVER_CONFIG.get('apiKey'))
ALLOW_CLIENT_MODEL = SERVER_CONFIG.get('allowClientModel', True) is not False
ACCESS_CODE = (SERVER_CONFIG.get('accessCode') or '').strip()


def strict_gates_enabled(config=None):
    """视觉门禁 fail-closed 开关（server_config.json 的 strictGates / 环境变量
    SPARK_STRICT_GATES）。默认关闭：判定服务异常时放行但在 manifest 留痕
    （auto_approved_degraded）；开启后判定服务异常按判定失败处理。"""
    if isinstance(config, dict) and config.get('strictGates') is not None:
        return bool(config.get('strictGates'))
    return bool(SERVER_CONFIG.get('strictGates') or os.environ.get('SPARK_STRICT_GATES'))


QA_GATE_LEVELS = ('standard', 'lenient', 'off')


def qa_gate_level(config=None):
    """帧质检门档位（请求 config 的 qaGateLevel > server_config.json > 环境变量
    SPARK_QA_GATE_LEVEL）。standard=现有全量质检；lenient=只拦硬伤（无变化/换场景/
    出现人物机械/文字水印），构图视角漂移等降级为警告放行，且停用跨帧地标漂移复查；
    off=视觉门全部跳过（manifest 记 auto_approved_degraded 留痕）。非法值回退 standard。"""
    raw = None
    if isinstance(config, dict) and config.get('qaGateLevel'):
        raw = config.get('qaGateLevel')
    else:
        raw = SERVER_CONFIG.get('qaGateLevel') or os.environ.get('SPARK_QA_GATE_LEVEL')
    level = str(raw).strip().lower() if raw else 'standard'
    return level if level in QA_GATE_LEVELS else 'standard'


def _int_setting(env_key, cfg_key, default):
    """数值配置的容错解析：非法值给出中文告警并回退默认，而不是 import 时崩掉整个服务。"""
    raw = os.environ.get(env_key, SERVER_CONFIG.get(cfg_key, default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        if sys.stdout:
            print(f"[WARN] 配置项 {cfg_key}/{env_key} 的值 {raw!r} 不是有效整数，已回退默认值 {default}")
        return default


# 2026-07-24: 旧默认 20/3600 是所有端点共用的单一预算，配合 rate_ok() 现按 action
# 分桶（见 server.py），每个端点各自的 60/小时对单用户经隧道/手机远程访问的正常
# 使用节奏足够宽松，同时仍能挡住误泄露访问链接后的高频滥用。
RATE_MAX = _int_setting('SPARK_RATE_MAX', 'rateMax', 60)
RATE_WINDOW = _int_setting('SPARK_RATE_WINDOW', 'rateWindow', 3600)
# 个人自用场景整套频控都是多余的（没有别人会用这个链接）；rateLimitEnabled=false
# 让 rate_ok() 直接放行，不必删掉机制本身——分桶/配额都还在，以后要对外开放时
# 把这个改回 true（或删掉这一项，默认就是 true）即可原样启用。
RATE_LIMIT_ENABLED = SERVER_CONFIG.get('rateLimitEnabled', True) is not False

PORT = _int_setting('PORT', 'port', 8085)
DB_FILE = 'library.json'
LEDGER_FILE = 'topic_ledger.json'
OUTPUT_ROOT = 'outputs'
# 2026-07-12: 旧版要求 "smallest localized edit"，与"每拍必须有全画幅阶段变化"的产品方向
# 直接对抗——提示词写了大变换、控制指令又叫模型最小化改动，结果就是整单"挤牙膏"式微小
# 变化。现改为：相机/几何/锚点绝对锁定，但阶段变换本身必须完整执行、覆盖其全部可见范围。
IMG2IMG_CONTROL_PROMPT = (
    "IMAGE EDITING MODE. The attached previous frame is the authoritative source image, "
    "not a loose style reference. Keep the camera ABSOLUTELY locked: exact camera position, "
    "lens, crop, horizon, perspective, frame boundaries, structural geometry, and the "
    "positions/scales of all locked anchor landmarks must match the source frame precisely. "
    "Also match the source frame's exact photographic rendering style, grain, and material "
    "weathering/grime/wear/finish level — do not clean up, smooth, brighten, or re-stylize any "
    "surface into a crisper or more polished look than the source frame already has. "
    "Within that locked framing, EXECUTE THE FULL STAGE TRANSFORMATION the description "
    "specifies: apply the described construction change across its entire visible extent — "
    "every surface and region the description says has changed must visibly change. Do NOT "
    "minimize, shrink, or token-patch the edit; the result must read as a completed "
    "construction stage clearly different from the source frame, while everything the "
    "description does not change stays pixel-faithful. Do not add grid lines, guides, labels, "
    "letters, numbers, percentages, captions, text, watermarks, extra people, or active "
    "machinery. Return one clean edited image only."
)
# Frame 1 uses the cover only as an image reference. The actual edit instruction that follows
# this control text is always the parsed `图片 1` prompt; no cover-generation prompt enters the
# frame-sequence request.
IMG2IMG_COVER_REFERENCE_CONTROL_PROMPT = (
    "IMAGE EDITING MODE. The attached image is only a visual identity reference for the "
    "project's subject and environment; it is not the text instruction and not a previous "
    "sequence frame. Render the scene required by the IMAGE 1 prompt below. Follow IMAGE 1 for "
    "the scene state, camera, composition, contents, and all exclusions. Preserve only useful "
    "subject identity, materials, terrain, and lighting continuity from the reference. Do not "
    "copy cover titles, captions, logos, poster layouts, split screens, borders, or finished-state "
    "details that conflict with IMAGE 1. Return one clean image only."
)
IMG2IMG_BRIDGE_CONTROL_PROMPT = (
    "IMAGE EDITING MODE (CAMERA MOVEMENT ACTIVE). The attached previous frame is the authoritative "
    "source image. Maintain extreme consistency of all physical landmarks, geometry, colors, "
    "materials, light source angles, and structural features. However, the camera viewpoint is "
    "actively advancing forward in a controlled camera-move / push-in (the camera perspective is "
    "shifting closer along the central axis). Shift object placement, horizon, and perspective boundaries "
    "according to correct optical flow and 3D depth parallax, and render the advanced viewpoint "
    "fully and decisively — inherited landmarks scale up naturally, newly exposed margins are "
    "filled with coherent detail; do not shrink the camera advance into a timid crop. "
    "Do not add extra objects, active machinery, or workers. Return one clean edited image only."
)
IMG2IMG_BRIDGE_TURN_CONTROL_PROMPT = (
    "IMAGE EDITING MODE (CAMERA MOVEMENT + TURN ACTIVE). The attached previous frame is the "
    "authoritative source image. Maintain extreme consistency of all physical landmarks, geometry, "
    "colors, materials, light source angles, and structural features. This single edit covers the "
    "WHOLE merged crossing: the camera first advances forward through the threshold in a controlled "
    "push-in, then ROTATES horizontally (one smooth pan) to face the interior's long axis — do not "
    "render only one half of this move. Inherited landmarks scale up naturally as the camera "
    "advances, then slide toward and past the frame edge as the camera turns; the newly revealed "
    "side of the space enters from the opposite edge and must be rendered fully and decisively, "
    "coherent with the established materials, lighting direction, weathering, and decay state — do "
    "not shrink either the advance or the turn into a timid crop. Do not add extra objects, active "
    "machinery, or workers. Return one clean edited image only."
)
# 过门帧"回退到未被触碰状态"的定向编辑指令（2026-07-26 用户实测："过门帧有人工痕迹，
# 不够原始"）。与门框清除的加推指令不同：这里镜头一动不动，只改画面里"有人来过"的
# 内容——搬走布景式的整洁、把衰败补回到与室外同源的程度。
IMG2IMG_RAW_STATE_CONTROL_PROMPT = (
    "IMAGE EDITING MODE (STATE CORRECTION, CAMERA LOCKED). The attached image is this project's "
    "first interior frame right after the camera crossed the threshold. Keep the camera, lens, "
    "crop, perspective, geometry, structural layout, and every landmark's position and scale "
    "EXACTLY as they are — this is not a re-frame and not a new viewpoint. Change only the STATE "
    "of what is in frame: this space must read as an untouched, long-abandoned ruin that nobody "
    "has entered or tidied yet. Remove every trace of human intervention — tools, toolboxes, "
    "ladders, scaffolding, paint cans, buckets, tarps, drop cloths, work lights, safety cones, "
    "and any fresh or neatly stacked material — and undo any patch that looks newly repaired, "
    "re-clad, or freshly painted, returning it to the surrounding weathered original material. "
    "Break up anything that looks arranged: debris must lie scattered unevenly where it fell, "
    "dirt drifted into corners, never gathered into neat piles or swept aside. Deepen the decay "
    "to match the exterior frames of the same building — cracks and sagging in the structure, "
    "rust/water stains/peeling paint on the surfaces, moss or roots following the damp cracks, "
    "and fallen wreckage across the floor. Do not add people, machinery, furniture, or any new "
    "built object. Return one clean edited image only."
)
# 提示词合成真正会去读的技能契约文件全集（相对 SKILL_DIR）。缺任何一个都不会报错：
# load_reference_file 返回空串、run_ideate 拿到空的形态矩阵/台账，合成照样跑完，只是
# 质量悄悄劣化——所以这份清单存在的意义就是让"悄悄"变成"吵闹"（启动日志 + /api/mode
# 上报给前端）。此前启动检查只覆盖其中 2 个，另外 6 个缺失时全程无声。
SKILL_CONTRACT_FILES = (
    'SKILL.md',
    'references/prompt-templates.md',
    'references/idea-engine.md',
    'references/used-topic-ledger.md',
    'references/space-workflows.md',
    'references/spatial-consistency-upgrade-protocol.md',
    'references/drift-lock-assembly-guide.md',
    'references/threshold-bridge-consistency-protocol.md',
)

# ── 技能包（skill）本地路径的解析 ──
# 取值优先级：环境变量 SKILL_DIR > server_config.json 的 skillDir > 内置默认路径 >
# 常见技能根目录下的自动探测。skillDir 这个配置项是 2026-07-26 补的：缺失告警一直
# 写着"用环境变量 SKILL_DIR / server_config.json 指向技能所在位置"，但代码只读环境
# 变量，照着提示往 server_config.json 里写是没有任何效果的。
SKILL_PACKAGE_NAME = 'restoration-prompt-composer'
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SKILL_DIR = os.path.join(
    os.path.expanduser('~'), '.codex', 'skills', SKILL_PACKAGE_NAME)
# 自动探测只在"没有任何显式配置、默认路径也没有契约文件"时兜底，扫这几个技能根目录
# 的一级子目录（不递归）。技能包被改名（例如装成了 gemini-omni-restoration-composer）
# 或装到了另一个 agent 的技能目录时，这一层能把它捡回来，而不是整条链路静默降级。
_SKILL_ROOT_CANDIDATES = (
    os.path.join(os.path.expanduser('~'), '.codex', 'skills'),
    os.path.join(os.path.expanduser('~'), '.claude', 'skills'),
    os.path.join(os.path.expanduser('~'), '.agents', 'skills'),
    os.path.join(_PROJECT_ROOT, 'skills'),
)
# 自动探测采纳门槛：至少命中这么多个契约文件才认。1 个太松——随便一个只有 SKILL.md
# 的技能包都会被误认成本项目的技能包。
_SKILL_AUTODETECT_MIN_HITS = 2


def _skill_contract_hits(directory):
    """directory 下命中了几个契约文件（用于挑"最像本项目技能包"的那个目录）。"""
    if not directory or not os.path.isdir(directory):
        return 0
    return sum(1 for rel in SKILL_CONTRACT_FILES
               if os.path.exists(os.path.join(directory, *rel.split('/'))))


def _expand_local_path(raw):
    """把配置里写的路径归一成绝对路径：支持 ~、$VAR，相对路径按本项目根目录解析。"""
    p = os.path.expanduser(os.path.expandvars(str(raw or '').strip()))
    if not p:
        return ''
    if not os.path.isabs(p):
        p = os.path.join(_PROJECT_ROOT, p)
    return os.path.normpath(p)


def _autodetect_skill_dir():
    """在常见技能根目录里挑契约覆盖最全的那个包；都不够格时返回空串。"""
    best, best_hits = '', 0
    for root in _SKILL_ROOT_CANDIDATES:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            candidate = os.path.join(root, name)
            hits = _skill_contract_hits(candidate)
            if hits > best_hits:
                best, best_hits = candidate, hits
    return best if best_hits >= _SKILL_AUTODETECT_MIN_HITS else ''


def _resolve_skill_dir():
    """返回 (路径, 来源)，来源取值 env / config / default / autodetect。

    显式指定（环境变量或 skillDir）时绝不二次猜测：路径写错了就该在启动日志和前端
    横幅上看见"这个目录缺 8 个文件"，而不是被自动探测悄悄换成另一个包。"""
    env_raw = os.environ.get('SKILL_DIR')
    if env_raw and env_raw.strip():
        return _expand_local_path(env_raw), 'env'
    cfg_raw = SERVER_CONFIG.get('skillDir')
    if cfg_raw and str(cfg_raw).strip():
        return _expand_local_path(cfg_raw), 'config'
    if _skill_contract_hits(_DEFAULT_SKILL_DIR):
        return _DEFAULT_SKILL_DIR, 'default'
    found = _autodetect_skill_dir()
    if found:
        return found, 'autodetect'
    return _DEFAULT_SKILL_DIR, 'default'


def _skill_config_mtime():
    try:
        return os.path.getmtime(SERVER_CONFIG_FILE)
    except OSError:
        return None


SKILL_DIR, SKILL_DIR_SOURCE = _resolve_skill_dir()
_SKILL_CONFIG_MTIME = _skill_config_mtime()


def skill_dir():
    """技能文件的实际读取路径——激发/合成每次要读契约文件时都走这里。

    顺带做热更新：server_config.json 的 mtime 变了就重算一次 skillDir，所以改完配置
    下一次「激发创意」立即用新路径，不必重启服务。除 skillDir 外的键不在这里回灌，
    免得把启动时就固化成模块常量的那些配置（端口、频控…）搞成半新半旧。"""
    global SKILL_DIR, SKILL_DIR_SOURCE, _SKILL_CONFIG_MTIME
    mtime = _skill_config_mtime()
    if mtime != _SKILL_CONFIG_MTIME:
        _SKILL_CONFIG_MTIME = mtime
        try:
            fresh = _load_server_config()
        except Exception:
            fresh = SERVER_CONFIG
        if fresh.get('skillDir'):
            SERVER_CONFIG['skillDir'] = fresh.get('skillDir')
        else:
            SERVER_CONFIG.pop('skillDir', None)
        SKILL_DIR, SKILL_DIR_SOURCE = _resolve_skill_dir()
    return SKILL_DIR


def skill_reference_path(name):
    """技能包 references/ 下某个文件的绝对路径（每次都按当前 skill_dir() 拼）。"""
    return os.path.join(skill_dir(), 'references', name)


def missing_skill_contract_files():
    """SKILL_CONTRACT_FILES 里当前不存在的那些（返回相对路径列表，保持声明顺序）。"""
    base = skill_dir()
    return [rel for rel in SKILL_CONTRACT_FILES
            if not os.path.exists(os.path.join(base, *rel.split('/')))]


def skill_contract_report():
    """技能契约现状的单一事实来源：{'dir', 'source', 'missing', 'total'}。

    调用方（启动检查、/api/mode）一律走这个函数而不是自己拼 SKILL_DIR——server.py 是
    `from server_common import *` 进来的，那份 SKILL_DIR 是导入时的**副本**，之后改
    server_common.SKILL_DIR 它不会跟着变（测试里改路径、配置热切换时都会踩到）。
    dir 在本函数内取值，才保证和 missing 的判定基于同一个路径。"""
    return {
        'dir': skill_dir(),
        'source': SKILL_DIR_SOURCE,
        'missing': missing_skill_contract_files(),
        'total': len(SKILL_CONTRACT_FILES),
    }

def get_fx_cancel_flag():
    from integrations.google_fx.utils import cancel_flag
    return cancel_flag


@contextlib.contextmanager
def fx_cancel_context(cancel_fn, deadline=None, poll_interval=0.5):
    """让内置 Google FX 运行时能感知 SPARK 的取消信号。

    SPARK 这侧唯一的跨进程取消信号一直是 builtins.google_fx_cancelled，而 AdsPower 侧
    2026-07 的重构已经把进程级全局旗标彻底删掉（一个请求的取消会污染其它并发请求），
    换成 per-request 的 CancelState + contextvar：utils.cancel_flag.is_cancelled 只认
    「当前上下文里存在 CancelState」，没有上下文时读永远是 False、写还会被忽略并打一条
    Warning。SPARK 在自己的 worker 线程里同步调用 FX 运行时，
    从来没建过这个上下文——于是脚本里那一整排 _check_cancelled() 全是空转：用户点「取消」
    后 SPARK 把 AdsPower 浏览器关掉，脚本却还在按 2 秒一轮扫 DOM 等图片 URL，把满屏
    TargetClosedError 刷进日志，直到每张图各自的 MAX_WAIT_SECONDS 走完才收摊（一批 5 张
    就是分钟级的"取消后日志还在疯狂检测"）。

    contextvar 只对建它的那个线程可见，而 FX 运行时正是在调用线程里同步跑的，所以这里
    在**调用线程**建 CancelState，再起一个守卫线程按 poll_interval 轮询 SPARK 自己的
    cancel_fn。CancelState 是普通对象、跨线程按引用改，置位后脚本下一次 _check_cancelled()
    立刻抛「任务已取消」。cancel_fn 会被别的线程调用，必须是纯谓词（各 worker 的
    progress_cb('cancel_check') / cancel_event.is_set 都满足）。

    deadline：绝对时刻（time.time() 秒），FX 运行时的 deadline_exceeded() 兜底用；None 不限时。
    FX 依赖不可用（纯 API 后端、依赖未安装）时安静降级成 no-op。
    """
    if cancel_fn is None:
        yield None
        return
    try:
        cancel_flag = get_fx_cancel_flag()
    except Exception as e:
        print(f"Warning: Google FX 取消上下文不可用，取消只在批次边界生效 ({e})")
        yield None
        return

    try:
        from fx_control import current_fx_task_id
        request_id = current_fx_task_id()
    except Exception:
        request_id = None
    request_id = request_id or f"spark-{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
    state = cancel_flag.init_context(request_id, deadline)
    cancel_flag.register(request_id, state)
    stop = threading.Event()

    # 进门先探一次：已经取消了就别让脚本白开一次浏览器等 poll_interval 才反应
    try:
        state.val = bool(cancel_fn())
    except Exception:
        state.val = True

    def _watch():
        while not stop.wait(poll_interval):
            try:
                if cancel_fn():
                    state.val = True
                    return
            except Exception:
                # 谓词自己炸了（连接已断等）按取消处理：宁可早停也不要空转到超时
                state.val = True
                return

    if not state.val:
        watcher = threading.Thread(target=_watch, name='fx-cancel-watch', daemon=True)
        watcher.start()
    try:
        yield state
    finally:
        stop.set()
        cancel_flag.unregister(request_id)
        # 不 reset contextvar：worker 线程是复用的，下一批会 init_context 覆盖成新状态；
        # 这里留着已置位的旧状态反而能让收尾路上的 _check_cancelled() 继续短路。


# ── 换 IP 已全局关停（2026-07-26）──
# 历史：AdsPower 侧原本每 googleFxIpRotateRequests（默认 5）个请求换一次 IP；后来改成
# 「先在同一个 IP 上把号池轮一圈，轮满 googleFxAccountsPerIp 个号才放行一次换 IP」来压
# 低频率。两版都还是会换 IP，而换 IP 本身就是故障源：实测会把 Flow 侧登录 token 打失效
# （批次中途弹登录、「等待人工处理」超时，整批剩余槽位跟着废掉），换 IP 重试后画布 tile
# 追踪还会绑到旧卡片导致下载串片。
# 现行为：一次都不换。apply_google_fx_runtime_overrides 每次都把 MIYA_ROTATE_THRESHOLD
# 写成单进程绝对够不到的阈值——AdsPower 的 utils/browser.get_ads_ws_url() 每次连浏览器
# 前会调 ProxyRotator.rotate_proxy()，而 ProxyRotator 每次实例化都现读 os.environ
# （config.runtime_env_or_default），所以这个阈值一写死，换 IP 分支就永远进不去。
# 与之配套的 googleFxAccountsPerIp 配置项已删除。
#
# 请求指纹的轮换全部改由「换号」承担：帧/视频序列照旧按 _account_switch_interval
# （沿用 googleFxIpRotateRequests 这个节拍值，语义现在只剩「每几个请求换一个号」）切成
# 若干「腿」，每腿绑号池里的下一个账号单独调一次批量脚本，只是每条腿都跑在同一个 IP 上。
# 号池可用账号 ≤1 个（含手动指定账号/空号池）时完全不切腿——没号可换，切腿只会白白多开
# 几次浏览器。切腿见 video_generator.plan_generation_legs 与
# frame_generator.plan_frame_chunk_accounts（帧那边的腿必须对齐既有的链式批次边界）。
# 下面这些是两条链共用的号池取数口径，所以住在 server_common。
_IP_ROTATE_DISABLED = 100000


def apply_google_fx_runtime_overrides(config):
    """把 SPARK 配置里的 Google FX / AdsPower 运行时可调项映射为环境变量覆盖。

    AdsPower 侧（config.py 的 runtime_env_or_default）每次调用都重新读取
    os.environ，所以这里设置后同进程内后续的 FX 调用立即生效，无需重启。
    浏览器编号（AdsPower profile 的 user_id）留空时不覆盖，沿用 AdsPower
    侧 .env 里的 ADSPOWER_DEFAULT_USER_ID 默认值。

    MIYA_ROTATE_THRESHOLD 恒定写成 _IP_ROTATE_DISABLED = 永不换 IP，不再受任何
    配置项影响（原因见上方「换 IP 已全局关停」）。
    """
    os.environ['MIYA_ROTATE_THRESHOLD'] = str(_IP_ROTATE_DISABLED)
    browser_port = str(config.get('adsPowerPort') or '').strip()
    if browser_port:
        os.environ['ADSPOWER_PORT'] = browser_port
    browser_user_id = str(config.get('googleFxUserId') or '').strip()
    if browser_user_id:
        os.environ['ADSPOWER_DEFAULT_USER_ID'] = browser_user_id
    from integrations.google_fx.model_catalog import normalize_google_fx_image_model
    image_model = normalize_google_fx_image_model(config.get('googleFxImageModel'))
    if image_model:
        os.environ['GOOGLE_FX_IMAGE_MODEL'] = image_model
    video_model = str(config.get('videoModel') or '').strip()
    if video_model:
        os.environ['GOOGLE_FX_VIDEO_MODEL'] = video_model


def _get_account_pool_service():
    """返回内置 Google FX 账号池服务。"""
    from integrations.google_fx.utils import account_pool
    return account_pool.AccountPool()


def _select_pool_account(config, pool):
    """池子非空且未手动指定账号时，自动选一个还有额度的账号写回 config；
    手动填了 googleFxUserId 单字段 = 这一次的临时覆盖，跳过自动挑选；
    池子为空（还没添加任何账号）= 完全不介入，行为与手动单选时代一致。
    返回被自动选中的 user_id（未触发自动选号则返回 None）。"""
    manual_override = str(config.get('googleFxUserId') or '').strip()
    if manual_override:
        return None
    if not pool.list_accounts():
        return None
    min_credit = config.get('videoAccountPoolMinCredit', 1)
    chosen = pool.pick_account(min_credit=min_credit)
    if chosen is None:
        raise RuntimeError('号池所有账号积分不足或被禁用，请在「号池管理」里检查/刷新后重试')
    config['googleFxUserId'] = chosen['user_id']
    return chosen['user_id']


def _account_switch_interval(config):
    """每多少个请求换一次账号（默认 5）。

    配置键沿用历史名 googleFxIpRotateRequests——换 IP 关停后它只剩「换号节拍」这一个
    语义，键名保留是为了不动用户已存的配置。"""
    try:
        n = int(config.get('googleFxIpRotateRequests') or 5)
    except (TypeError, ValueError):
        n = 5
    return max(1, n)


def _account_in_cooldown(account):
    raw = account.get('cooldown_until')
    if not raw:
        return False
    try:
        from datetime import datetime
        until = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return False  # 解析不了就不当冷却——宁可多轮一个号，也不要把可用池判空
    now = datetime.now(until.tzinfo) if until.tzinfo else datetime.now()
    return until > now


def _account_has_credit(account, min_credit):
    try:
        return float(account.get('credit')) >= float(min_credit)
    except (TypeError, ValueError):
        return True  # 积分未知（还没探测过）不排除，交给生成时的真实反馈去冷却


def _account_rotation_ring(config, pool, first_user_id):
    """号池里当前可用的账号排成轮转顺序，本次已选中的账号排最前。

    可用 = 未禁用 + 不在冷却 + 积分未知或 ≥ min_credit（与 pick_account 同一口径）。"""
    min_credit = config.get('videoAccountPoolMinCredit', 1)
    try:
        accounts = pool.list_accounts() or []
    except Exception as e:
        print(f"Warning: 读取号池轮转顺序失败，退回单账号模式 ({e})")
        return [first_user_id] if first_user_id else []
    ring = [
        acc['user_id'] for acc in accounts
        if acc.get('user_id') and not acc.get('disabled')
        and not _account_in_cooldown(acc) and _account_has_credit(acc, min_credit)
    ]
    if first_user_id:
        ring = [first_user_id] + [uid for uid in ring if uid != first_user_id]
    return ring


def _next_unused_account(config, pool, ring, exclude):
    """挑一个本次还没用过的号：先看轮转环，环里没有了再回头问一次号池
    （刷新过积分/冷却到期的账号可能这会儿又可用了）。都没有则返回 None。"""
    for uid in ring:
        if uid not in exclude:
            return uid
    try:
        chosen = pool.pick_account(min_credit=config.get('videoAccountPoolMinCredit', 1))
    except Exception:
        return None
    if chosen and chosen.get('user_id') and chosen['user_id'] not in exclude:
        return chosen['user_id']
    return None


def effective_config(client_config):
    client_config = client_config or {}
    from integrations.google_fx.model_catalog import normalize_google_fx_image_model
    if not SERVER_MANAGED:
        merged = dict(client_config)
        if 'googleFxImageModel' in merged:
            merged['googleFxImageModel'] = normalize_google_fx_image_model(
                merged.get('googleFxImageModel'))
        return merged
    merged = {
        'baseUrl': SERVER_CONFIG.get('baseUrl') or 'http://127.0.0.1:8046/v1',
        'apiKey': SERVER_CONFIG.get('apiKey') or '',
        'model': SERVER_CONFIG.get('model') or 'gemini-3.6-flash-high',
        'imageModel': SERVER_CONFIG.get('imageModel') or 'gemini-3.1-flash-image',
    }
    # cheapModel/auxModel 此前在托管模式下被静默丢弃（example 配置里承诺了
    # cheapModel）；realityCheckpointInterval（帧链现实同步检查点间隔）同批透传，
    # 防复刻同类静默失效。
    # 已删除的两组键（不要再加回来）：
    #   · geminiDirectApiKey / geminiApiKey / geminiDirectImageModel —— 直连
    #     Google AI Studio 的暗路入口，整条路径已随之删除；
    #   · imageEditFallbackModel —— 配额耗尽自动降级到 gpt-image-2 的开关，
    #     降级机制已取消，配额耗尽一律显式报错。
    # imageEditTransport 同批补进来：它此前只写在 server_config.json 里，托管模式
    # （配了 apiKey 就是）下被这份白名单整个丢掉，于是配 'chat' 的机器每个进程
    # 照样先打一枪必挂的 /images/edits——「配置了但从未生效」的静默失效，和
    # qaGateLevel 当年丢的是同一个口子（见 tests/test_qa_gate_levels.py）。
    for k in ('cheapModel', 'auxModel', 'realityCheckpointInterval', 'imageEditTransport'):
        if SERVER_CONFIG.get(k):
            merged[k] = SERVER_CONFIG.get(k)
    if ALLOW_CLIENT_MODEL:
        if client_config.get('model'):
            merged['model'] = client_config['model']
        if client_config.get('imageModel'):
            merged['imageModel'] = client_config['imageModel']
        for k in ('cheapModel', 'auxModel'):
            if client_config.get(k):
                merged[k] = client_config[k]
    for k in ('imageAspectRatio', 'imageQuality', 'imageBackend', 'googleFxImageModel', 'videoModel', 'videoDuration', 'googleFxIpRotateRequests', 'googleFxUserId', 'adsPowerPort', 'videoAccountPoolMinCredit', 'qaGateLevel', 'realityCheckpointInterval', 'ideationTrendUrls', 'ideationSearchQuery', 'coverReferencePath'):
        if k in client_config:
            merged[k] = client_config[k]
        elif k in SERVER_CONFIG:
            merged[k] = SERVER_CONFIG[k]
    if 'googleFxImageModel' in merged:
        merged['googleFxImageModel'] = normalize_google_fx_image_model(
            merged.get('googleFxImageModel'))
    return merged

def gpt_image_pixel_size(aspect_ratio):
    """gpt-image-2 走独立的 codex 网关(65038)，不认 Gemini 网关(8046)那套
    'w:h' 比例字符串——它是真正的 OpenAI images API，只认 1024x1024 /
    1024x1536 / 1536x1024 / auto 这几个像素档位，传 '9:16' 会被忽略掉回默认方形。
    按宽高比归到最接近的三档之一。"""
    ratio = (aspect_ratio or '1:1').strip()
    try:
        w_str, h_str = ratio.split(':', 1)
        w, h = float(w_str), float(h_str)
    except (ValueError, AttributeError):
        return '1024x1024'
    if w <= 0 or h <= 0:
        return '1024x1024'
    if abs(w - h) / max(w, h) < 0.05:
        return '1024x1024'
    return '1024x1536' if h > w else '1536x1024'


_CODEX_BASE_URL_DEFAULT = 'http://127.0.0.1:65038/v1'


def resolve_gateway(model_name, config):
    base_url = (config.get('baseUrl') or 'http://127.0.0.1:8046/v1').rstrip('/')
    api_key = config.get('apiKey') or ''
    m_lower = (model_name or '').lower()
    if 'gpt-5' in m_lower or 'codex' in m_lower or 'gpt-image-2' in m_lower:
        base_url = (config.get('codexBaseUrl')
                    or SERVER_CONFIG.get('codexBaseUrl')
                    or _CODEX_BASE_URL_DEFAULT).rstrip('/')
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


def make_idea_project_key(task_id, title):
    """Return a stable, per-compose media namespace.

    The run id intentionally leads the key: _safe_project_name truncates long names at
    60 characters, so a suffix could disappear for long Chinese titles and collide again.
    """
    safe_id = re.sub(r'[^a-zA-Z0-9_-]+', '_', str(task_id or '')).strip('_') or 'unknown'
    return f"run_{safe_id}__{title or '未命名创意'}"


def _legacy_ascii_project_name(title):
    raw = (title or 'spark_frames').strip()
    import hashlib
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', raw)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_-')
    title_hash = hashlib.md5(raw.encode('utf-8', errors='ignore')).hexdigest()
    if not sanitized:
        return title_hash[:12]
    return f"{sanitized[:40]}_{title_hash[:6]}"


_PROJECT_KEY_CONTEXT = threading.local()


def set_project_key_context(project_key=None):
    """Override _get_project_dir's key for the current media worker thread only."""
    if project_key:
        _PROJECT_KEY_CONTEXT.value = str(project_key)
    elif hasattr(_PROJECT_KEY_CONTEXT, 'value'):
        delattr(_PROJECT_KEY_CONTEXT, 'value')


def _get_project_dir(title):
    # Media workers keep the human title for prompts/logs while their thread-local
    # project key selects an isolated on-disk namespace for this compose run.
    title = getattr(_PROJECT_KEY_CONTEXT, 'value', None) or title
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


def resolve_cover_reference(config, title):
    """Resolve the cover used only as frame 1's image reference.

    A client-selected cover wins; headless callers fall back to this project's newest cover.
    Request paths are restricted to outputs/covers so arbitrary local files cannot be uploaded.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    covers_dir = os.path.abspath(os.path.join(root, OUTPUT_ROOT, 'covers'))
    named = (config or {}).get('coverReferencePath') if isinstance(config, dict) else None
    if isinstance(named, str) and named.strip():
        raw = named.strip().split('?', 1)[0]
        candidate = raw if os.path.isabs(raw) else os.path.join(root, raw.lstrip('/\\'))
        candidate = os.path.abspath(candidate)
        try:
            inside = os.path.commonpath([candidate, covers_dir]) == covers_dir
        except ValueError:
            inside = False
        if inside and os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate

    if not os.path.isdir(covers_dir):
        return None
    prefix = f"{_safe_project_name(title)}_cover_"
    found = [os.path.join(covers_dir, name) for name in os.listdir(covers_dir)
             if name.startswith(prefix)]
    found = [path for path in found if os.path.isfile(path) and os.path.getsize(path) > 0]
    return max(found, key=os.path.getmtime) if found else None

    # 2. Try old naming scheme (unreachable legacy tail, kept as-is)
    old_raw = (title or 'spark_frames').strip()
    old_raw = re.sub(r'[\\/:*?"<>|]+', '_', old_raw)
    old_raw = re.sub(r'\s+', '_', old_raw)
    old_name = old_raw.strip('._-')[:60] or 'spark_frames'
    old_dir = os.path.join(OUTPUT_ROOT, old_name)
    if os.path.exists(old_dir):
        return old_dir

    # 3. Default to new naming scheme path if neither exists (for creation)
    return new_dir


def delete_idea_output_files(title, covers=None):
    """Best-effort purge of everything a generated idea left on disk: its whole
    project directory (frames/videos/manifest under outputs/<project>/) plus any
    standalone cover images (outputs/covers/*.webp) referenced by a saved idea.
    Deleting the task/library record alone leaves these behind as orphan files,
    so this must be called from both the task-delete and library-delete endpoints.
    """
    deleted = {"project_dir": None, "covers": []}

    if title:
        try:
            project_dir = _get_project_dir(title)
            if project_dir and os.path.isdir(project_dir):
                shutil.rmtree(project_dir, ignore_errors=True)
                deleted["project_dir"] = project_dir
        except Exception as e:
            if sys.stdout:
                print(f"[DELETE] Failed to remove project dir for '{title}': {e}")

    output_root_abs = os.path.abspath(OUTPUT_ROOT)
    for cover_url in (covers or []):
        if not isinstance(cover_url, str):
            continue
        rel = cover_url.lstrip('/')
        # Only ever touch files SPARK itself served out of outputs/ — never an
        # external/data URI, and never anything that normalizes outside outputs/.
        if not (rel == OUTPUT_ROOT or rel.startswith(OUTPUT_ROOT + '/')):
            continue
        cover_path = os.path.abspath(rel)
        if not (cover_path == output_root_abs or cover_path.startswith(output_root_abs + os.sep)):
            continue
        try:
            if os.path.isfile(cover_path):
                os.remove(cover_path)
                deleted["covers"].append(cover_path)
        except Exception as e:
            if sys.stdout:
                print(f"[DELETE] Failed to remove cover '{cover_path}': {e}")

    return deleted


# --- Gallery (画廊) helpers ---
# 画廊只认这两类扩展名：既是扫描的准入名单，也是删除接口的白名单——
# manifest.json / 检查点等非媒体文件永远不会被画廊接口碰到。
GALLERY_IMAGE_EXTS = ('.webp', '.png', '.jpg', '.jpeg', '.gif', '.bmp')
GALLERY_VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.m4v')

# outputs/ 下这两个一级目录不是"项目"：covers 是全局封面池，image-station 是
# 图像工坊的出图历史。它们没有 manifest，删除后也不做项目级清理/同步。
GALLERY_SPECIAL_DIRS = ('covers', 'image-station')


def _gallery_media_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in GALLERY_IMAGE_EXTS:
        return 'image'
    if ext in GALLERY_VIDEO_EXTS:
        return 'video'
    return None


def _gallery_item(abs_path, base_dir, kind):
    st = os.stat(abs_path)
    rel = os.path.relpath(abs_path, base_dir).replace('\\', '/')
    return {
        'name': os.path.basename(abs_path),
        'path': rel,
        'url': '/' + rel,
        'type': _gallery_media_type(abs_path),
        'kind': kind,
        'size': st.st_size,
        'mtime': int(st.st_mtime),
    }


# 项目目录最近 24 小时内有产出时不标孤儿：正在生成中的项目（compose 已挂但
# staged 帧渲染还在跑等竞态）引用关系可能还没落到 library/tasks 里
GALLERY_ORPHAN_GRACE_SECONDS = 24 * 3600


def _gallery_title_names(title):
    """一个标题在历代目录命名方案下的全部候选目录名（与 _get_project_dir 的
    三种方案一一对应，外加曾经存在过的截断变体）。用于引用判定，从宽收集。"""
    if not isinstance(title, str) or not title.strip():
        return set()
    names = {_safe_project_name(title), _legacy_ascii_project_name(title)}
    old_raw = title.strip()
    old_raw = re.sub(r'[\\/:*?"<>|]+', '_', old_raw)
    old_raw = re.sub(r'\s+', '_', old_raw)
    if old_raw:
        names.add(old_raw)
        trimmed = old_raw.strip('._-')[:60]
        if trimmed:
            names.add(trimmed)
    return names


def gallery_collect_references(library_items=None, tasks=None):
    """从 library.json 与任务持久层收集"被引用"的封面文件与项目目录名。

    判定刻意从宽：title/english_title/frameRun 标题与文件路径/staged 任务的
    theme、所有命名方案变体都算引用——宁可漏标孤儿，绝不能把在用资产标成孤儿
    （孤儿标记是画廊批量清理的入口，误标的代价是用户删掉活资产）。
    library_items/tasks 传 None 时从真实数据源读取；测试传显式列表。
    """
    cover_paths = set()
    project_names = set()

    def add_cover(u):
        if not isinstance(u, str):
            return
        rel = u.replace('\\', '/').lstrip('/')
        if rel.startswith(OUTPUT_ROOT + '/'):
            cover_paths.add(rel)

    def add_title(t):
        project_names.update(_gallery_title_names(t))

    def add_path_project(p):
        if not isinstance(p, str):
            return
        parts = p.replace('\\', '/').lstrip('/').split('/')
        if len(parts) >= 2 and parts[0] == OUTPUT_ROOT and parts[1] not in GALLERY_SPECIAL_DIRS:
            project_names.add(parts[1])

    def eat_record(rec):
        if not isinstance(rec, dict):
            return
        for u in (rec.get('covers') or []):
            add_cover(u)
        add_cover(rec.get('collage_url'))
        add_title(rec.get('title'))
        add_title(rec.get('english_title'))
        fr = rec.get('frameRun')
        if isinstance(fr, dict):
            add_title(fr.get('title'))
            for coll in ('frames', 'videos'):
                for e in (fr.get(coll) or []):
                    if isinstance(e, dict):
                        add_path_project(e.get('url'))
                        add_path_project(e.get('file'))

    if library_items is None:
        library_items = []
        with LIBRARY_LOCK:
            if os.path.exists(DB_FILE):
                try:
                    with open(DB_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        library_items = data
                except Exception:
                    pass
    for item in (library_items or []):
        eat_record(item)

    if tasks is None:
        with ACTIVE_TASKS_LOCK:
            tasks = [
                {'dimensions': t.get('dimensions'), 'result': t.get('result')}
                for t in ACTIVE_TASKS.values()
            ]
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        eat_record(t.get('result'))
        dims = t.get('dimensions')
        if isinstance(dims, dict):
            # staged_render 任务的 theme 就是项目标题；compose 任务的 theme 是
            # 场景主题，多收一个引用无害（从宽原则）
            add_title(dims.get('theme'))

    return {'cover_paths': cover_paths, 'project_names': project_names}


def scan_gallery(base_dir=None, refs=None):
    """扫描 outputs/ 下全部历史媒体资产，按来源分组返回（画廊页数据源）。

    分组：covers（封面池）、image-station（图像工坊出图）、以及每个项目目录一组
    （frames/ 帧序列 + videos/ 分段视频 + 项目根的合成视频）。项目根下的插帧中间
    产物目录（*_frames，成百上千张 jpg）不属于用户资产，不进画廊。

    refs 传 gallery_collect_references() 的返回值时做引用标注：封面 item 加
    in_use（被点子库/任务引用），项目组加 orphan（无任何引用且超过活跃宽限期）。
    refs=None 时不加任何标注字段（前端按无标注降级展示）。
    """
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base_dir, OUTPUT_ROOT)
    groups = []
    totals = {'images': 0, 'videos': 0, 'bytes': 0}

    def collect_dir(dpath, kind, only_type=None):
        items = []
        if not os.path.isdir(dpath):
            return items
        for fname in sorted(os.listdir(dpath)):
            fpath = os.path.join(dpath, fname)
            if not os.path.isfile(fpath):
                continue
            mtype = _gallery_media_type(fname)
            if mtype is None or (only_type and mtype != only_type):
                continue
            try:
                items.append(_gallery_item(fpath, base_dir, kind))
            except OSError:
                continue
        return items

    def add_group(key, title, gkind, items):
        if not items:
            return
        items.sort(key=lambda it: it['mtime'], reverse=True)
        gbytes = sum(it['size'] for it in items)
        for it in items:
            totals['images' if it['type'] == 'image' else 'videos'] += 1
        totals['bytes'] += gbytes
        groups.append({
            'key': key,
            'title': title,
            'kind': gkind,
            'items': items,
            'bytes': gbytes,
            'latest_mtime': items[0]['mtime'],
        })

    if not os.path.isdir(out_dir):
        return {'groups': [], 'totals': totals}

    cover_items = collect_dir(os.path.join(out_dir, 'covers'), 'cover', only_type='image')
    if refs is not None:
        for it in cover_items:
            it['in_use'] = it['path'] in refs['cover_paths']
    add_group('covers', '封面图片', 'covers', cover_items)
    add_group('image-station', '图像工坊', 'studio',
              collect_dir(os.path.join(out_dir, 'image-station'), 'studio', only_type='image'))

    now = time.time()
    for name in sorted(os.listdir(out_dir)):
        if name in GALLERY_SPECIAL_DIRS:
            continue
        pdir = os.path.join(out_dir, name)
        if not os.path.isdir(pdir):
            continue
        items = collect_dir(os.path.join(pdir, 'frames'), 'frame')
        items += collect_dir(os.path.join(pdir, 'videos'), 'video', only_type='video')
        # 项目根：合成视频（含 _2x/_配音字幕 等成品）与零散图片
        items += collect_dir(pdir, 'merged', only_type='video')
        items += collect_dir(pdir, 'other', only_type='image')
        add_group(name, name, 'project', items)
        if refs is not None and items:
            g = groups[-1]
            g['orphan'] = (name not in refs['project_names']
                           and g['latest_mtime'] < now - GALLERY_ORPHAN_GRACE_SECONDS)

    # 最近有动静的组排最前（covers/image-station 也参与排序）
    groups.sort(key=lambda g: g['latest_mtime'], reverse=True)
    return {'groups': groups, 'totals': totals}


def _project_dir_has_gallery_media(pdir):
    """项目目录里是否还剩"画廊可见"的媒体：frames/ 任意媒体、videos/ 视频、
    项目根的图片/视频。与 scan_gallery 的收集范围一一对应——插帧中间产物
    （*_2x_frames 等隐藏产物）刻意不算数，否则"删除本组"后文件夹永远删不掉，
    留下带残渣的空壳。"""
    frames_dir = os.path.join(pdir, 'frames')
    if os.path.isdir(frames_dir):
        for f in os.listdir(frames_dir):
            if _gallery_media_type(f) and os.path.isfile(os.path.join(frames_dir, f)):
                return True
    videos_dir = os.path.join(pdir, 'videos')
    if os.path.isdir(videos_dir):
        for f in os.listdir(videos_dir):
            if _gallery_media_type(f) == 'video' and os.path.isfile(os.path.join(videos_dir, f)):
                return True
    for f in os.listdir(pdir):
        if _gallery_media_type(f) and os.path.isfile(os.path.join(pdir, f)):
            return True
    return False


def gallery_delete_files(paths, base_dir=None):
    """删除 outputs/ 内指定的媒体文件（画廊删除接口的后端）。

    安全边界：只接受规范化后仍落在 outputs/ 内、且扩展名在媒体白名单内的路径；
    目录、manifest、越界路径一律进 failed 而不是抛异常。项目目录删到不再有
    画廊可见媒体时，整个项目文件夹（含 manifest、插帧中间产物等隐藏残留）一并
    rmtree——"删除本组"的预期就是文件夹也消失。
    返回 affected_project_dirs（仍存活、需要重同步 manifest 的项目目录绝对路径），
    manifest 同步函数在 server.py 里，由调用方负责执行。
    """
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    out_root_abs = os.path.abspath(os.path.join(base_dir, OUTPUT_ROOT))
    result = {'deleted': [], 'failed': [], 'affected_project_dirs': [], 'removed_project_dirs': []}
    touched = set()

    for raw in (paths or []):
        if not isinstance(raw, str) or not raw.strip():
            result['failed'].append({'path': str(raw), 'error': '无效路径'})
            continue
        rel = raw.replace('\\', '/').lstrip('/')
        abs_p = os.path.abspath(os.path.join(base_dir, rel))
        if not abs_p.startswith(out_root_abs + os.sep):
            result['failed'].append({'path': raw, 'error': '路径不在 outputs/ 内'})
            continue
        if _gallery_media_type(abs_p) is None:
            result['failed'].append({'path': raw, 'error': '仅允许删除图片/视频文件'})
            continue
        if not os.path.isfile(abs_p):
            result['failed'].append({'path': raw, 'error': '文件不存在'})
            continue
        try:
            os.remove(abs_p)
            result['deleted'].append(rel)
            top = os.path.relpath(abs_p, out_root_abs).replace('\\', '/').split('/')[0]
            if top not in GALLERY_SPECIAL_DIRS:
                touched.add(os.path.join(out_root_abs, top))
        except Exception as e:
            result['failed'].append({'path': raw, 'error': str(e)})

    for pdir in sorted(touched):
        if not os.path.isdir(pdir):
            continue
        if _project_dir_has_gallery_media(pdir):
            result['affected_project_dirs'].append(pdir)
        else:
            shutil.rmtree(pdir, ignore_errors=True)
            result['removed_project_dirs'].append(pdir)
    return result


# --- Task Management State and Helpers ---
class GenerationCancelled(ConnectionError):
    """用户取消生成任务的专用信号。继承 ConnectionError 以兼容既有的
    `except ConnectionError → cancelled` worker 收尾逻辑，但可以被
    _chat 的流式回调、各重试循环精确识别后直接放行（不吞掉、不重试、
    不降级成一次全新的非流式请求）。"""


PACKET_CACHE_LOCK = threading.RLock()
# 提示词合成断点续传(compose_checkpoints.json)专用锁,见 prompt_pipeline 的
# save_compose_checkpoint/load_compose_checkpoint
COMPOSE_CHECKPOINT_LOCK = threading.RLock()
ACTIVE_TASKS = {}
ACTIVE_TASKS_LOCK = threading.RLock()

# library.json 读写共用一把锁：Windows 上 os.replace 会因并发打开的读句柄抛
# PermissionError，读路径也必须串行化，否则存在整库清零风险
LIBRARY_LOCK = threading.Lock()

# topic_ledger.json（创意台账管理库）读写锁，同一套整库清零教训 → 同一套防护
LEDGER_LOCK = threading.Lock()

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


LEDGER_STATUSES = {'candidate', 'used', 'published', 'discarded'}


def read_ledger(path=None):
    """读取 topic_ledger.json。缺失返回 []；损坏返回 None（调用方应回 500，
    不能静默降级为 [] —— 那正是 library.json 整库清零事故的触发路径）。"""
    path = path or LEDGER_FILE
    with LEDGER_LOCK:
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            if sys.stdout:
                print(f"[WARN] {path} 读取失败: {e}")
            return None
        return data if isinstance(data, list) else None


def _write_ledger_file(entries, path):
    """无防护的落盘：写前若现有文件非空则轮换 .bak，然后原子写入。是否允许这次
    覆盖由调用方（write_ledger 的空列表拒绝 / delete_ledger_entries 的按 id 删除）
    各自判断，这里只负责"写就写安全"。调用方必须已持有 LEDGER_LOCK。"""
    existing_count = 0
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                _old = json.load(f)
            existing_count = len(_old) if isinstance(_old, list) else 1
        except Exception:
            existing_count = 1  # 读不出来按"非空"保守处理
    if existing_count > 0:
        try:
            shutil.copyfile(path, path + '.bak')
        except Exception as _bak_err:
            if sys.stdout:
                print(f"[LEDGER GUARD] 备份 {path}.bak 失败（继续写入）: {_bak_err}")
    write_json_atomic(path, entries)


def write_ledger(entries, path=None):
    """写 topic_ledger.json，套用 library.json 的三层防护：空列表拒绝覆盖非空、
    写前 .bak 轮换、原子替换。返回 (ok, message)；ok=False 时调用方应回 409。

    这条防护只堵"客户端以为台账是空的"这类状态错乱场景（整表回写、编辑状态/
    打分/备注时用）。真要批量/全部删除，走 delete_ledger_entries——按 id 删除
    天然带着"确有意图"的证据，不吃这道防护。"""
    path = path or LEDGER_FILE
    if not isinstance(entries, list):
        return False, '台账数据必须是数组'
    with LEDGER_LOCK:
        existing_count = 0
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    _old = json.load(f)
                existing_count = len(_old) if isinstance(_old, list) else 1
            except Exception:
                existing_count = 1  # 读不出来按"非空"保守处理
        incoming_count = len(entries)
        if incoming_count == 0 and existing_count > 0:
            if sys.stdout:
                print(f"[LEDGER GUARD] 拒绝空列表覆盖非空创意台账（现有 {existing_count} 条）")
            return False, (
                f'拒绝将非空创意台账（{existing_count} 条）覆盖为空：客户端状态疑似错乱，'
                f'请刷新页面重新加载后再操作；如确要清空请用批量删除。'
            )
        _write_ledger_file(entries, path)
        return True, None


def register_ledger_candidates(ideas, path=None, source='Ideation Pool'):
    """Atomically add newly generated ideas to the creative ledger.

    Unlike the browser's historical GET + whole-table POST flow, this operation keeps
    the read/dedupe/write sequence under ``LEDGER_LOCK`` so two simultaneous ideation
    requests cannot overwrite each other's candidates. Topic DNA is the primary
    dedupe key; title is a fallback for malformed/legacy ideas without DNA.

    Returns ``{'added': int, 'duplicates': int, 'entries': list}``. A corrupt ledger
    raises instead of silently starting a new empty ledger.
    """
    import uuid as _uuid

    path = path or LEDGER_FILE
    if not isinstance(ideas, list):
        raise ValueError('激发结果必须是数组')

    def _key(value):
        return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()

    with LEDGER_LOCK:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
            except Exception as e:
                raise RuntimeError(f'创意台账读取失败，已停止自动入账: {e}') from e
            if not isinstance(entries, list):
                raise RuntimeError('创意台账格式错误，已停止自动入账')
        else:
            entries = []

        dna_keys = {
            _key(row.get('topic_dna')) for row in entries
            if isinstance(row, dict) and _key(row.get('topic_dna'))
        }
        title_keys = {
            _key(row.get('one_line')) for row in entries
            if isinstance(row, dict) and _key(row.get('one_line'))
        }
        added = 0
        duplicates = 0
        today = datetime.now().strftime('%Y-%m-%d')

        for idea in ideas:
            if not isinstance(idea, dict):
                continue
            dna = str(idea.get('dna') or idea.get('topic_dna') or '').strip()
            title = str(idea.get('title') or idea.get('one_line') or '').strip()
            dna_key = _key(dna)
            title_key = _key(title)
            if (dna_key and dna_key in dna_keys) or (not dna_key and title_key and title_key in title_keys):
                duplicates += 1
                continue
            if not dna_key and not title_key:
                continue

            score = idea.get('score', idea.get('llm_score'))
            entries.append({
                'id': str(_uuid.uuid4()),
                'date': today,
                'topic_dna': dna,
                'one_line': title,
                'source': source,
                'avoid_notes': '',
                'status': 'candidate',
                'llm_score': score if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                'user_score': None,
                'performance_note': '',
            })
            if dna_key:
                dna_keys.add(dna_key)
            if title_key:
                title_keys.add(title_key)
            added += 1

        if added:
            _write_ledger_file(entries, path)
        return {'added': added, 'duplicates': duplicates, 'entries': entries}


def delete_ledger_entries(ids, path=None):
    """按 id 集合批量删除台账条目（批量删除所选 / 全选删除都走这条）。不经过
    write_ledger 的空列表覆盖防护——那道防护针对的是"客户端以为库是空的"这类
    状态错乱场景，而显式 id 列表天然不会被这种错乱触发（错乱状态产不出真实 id）。

    返回 dict：
      - deleted: 实际删除条数
      - remaining: 删除后的剩余条目列表；读取/解析失败时为 None（调用方应回 500）
    """
    path = path or LEDGER_FILE
    id_set = set(ids or [])
    if not id_set:
        return {'deleted': 0, 'remaining': []}
    with LEDGER_LOCK:
        if not os.path.exists(path):
            return {'deleted': 0, 'remaining': []}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            if sys.stdout:
                print(f"[WARN] {path} 读取失败: {e}")
            return {'deleted': 0, 'remaining': None}
        if not isinstance(data, list):
            return {'deleted': 0, 'remaining': None}
        remaining = [e for e in data if not (isinstance(e, dict) and e.get('id') in id_set)]
        deleted = len(data) - len(remaining)
        if deleted > 0:
            _write_ledger_file(remaining, path)
        return {'deleted': deleted, 'remaining': remaining}


def parse_legacy_ledger_md(content):
    """把 used-topic-ledger.md 的原始文本解析成结构化条目列表，供一次性迁移使用。

    表格在文件中途会被一段 "## Avoid List" 标题打断、随后又继续出现数据行——
    因此按"是否是 | 开头的数据行"逐行扫描全文，而不是假设单一连续表格。"""
    import uuid as _uuid
    entries = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        cells = [c.strip() for c in stripped.strip('|').split('|')]
        if len(cells) < 5:
            continue
        date_str, topic_dna, one_line, source, avoid_notes = cells[:5]
        # 跳过表头行（"Date"/"日期"）与分隔行（"---"）
        if not date_str or date_str.lower().startswith('date') or date_str.startswith('日期'):
            continue
        if set(date_str) <= {'-'}:
            continue
        entries.append({
            'id': str(_uuid.uuid4()),
            'date': date_str,
            'topic_dna': topic_dna,
            'one_line': one_line,
            'source': source or 'Migrated',
            'avoid_notes': avoid_notes,
            'status': 'used',
            'llm_score': None,
            'user_score': None,
            'performance_note': '',
        })
    return entries


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


# manifest_lock 只护单次文件 I/O；帧渲染 worker 的一整轮生成（分钟级）在内存里
# 长期持有一份 manifest['frames'] 快照，逐帧整体覆写落盘。如果同一项目目录被
# 两个 worker 同时跑（取消还没真正生效又点了重渲、多标签页/刷新后重连早于
# 点击、/api/generate_frames 与 /api/render_staged 撞车），后写入的那个会用
# 自己过期的快照整体覆盖对方刚写完的帧——manifest 层面的帧图片"无故被覆盖或
# 丢失"。这里按项目目录做互斥占位：同一时刻只允许一个 worker 持有。
_ACTIVE_FRAME_RUNS = {}
_ACTIVE_FRAME_RUNS_LOCK = threading.Lock()


def claim_frame_run(project_dir, task_id):
    """尝试为 project_dir 声明一次帧渲染运行权。

    成功返回 None；已有其他运行中 worker 占用该项目时返回占用者的 task_id
    （调用方应把它当 already_running 转告前端重新挂流，不得再起第二个
    worker）。占用者若已终态（异常路径漏调 release_frame_run），视为陈旧
    占位自动收回，避免把项目永久锁死。
    """
    key = os.path.normcase(os.path.abspath(project_dir))
    with _ACTIVE_FRAME_RUNS_LOCK:
        holder = _ACTIVE_FRAME_RUNS.get(key)
        if holder and holder != task_id:
            holder_task = ACTIVE_TASKS.get(holder)
            if holder_task and holder_task.get('status') == 'running':
                return holder
        _ACTIVE_FRAME_RUNS[key] = task_id
        return None


def release_frame_run(project_dir, task_id):
    """释放 claim_frame_run 的占位；只释放自己持有的那份，防止迟到的旧
    worker 收尾时误删新 worker 刚声明的占位。"""
    key = os.path.normcase(os.path.abspath(project_dir))
    with _ACTIVE_FRAME_RUNS_LOCK:
        if _ACTIVE_FRAME_RUNS.get(key) == task_id:
            del _ACTIVE_FRAME_RUNS[key]


# 一致性审查真正跑出来的两种结论（区别于"没审成"/"还没轮到审"）。帧内容变了要作废的
# 就是它们，见 drop_stale_review_verdicts。
REAL_REVIEW_VERDICTS = ('sequence_reviewed_pass', 'sequence_review_flagged')


def frame_content_hash(path):
    """帧图片文件内容的 sha256（读不到返回 None）。"""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def drop_stale_review_verdicts(manifest, project_dir):
    """就地作废"所看帧图已经变过"的一致性审查结论，返回被作废的帧序号列表。

    审查结论覆盖的是"当时那几张图"：任何一帧被重渲（单帧重试/定向修复/整单重渲），
    它自己以及相邻拍的判定都不再成立。此前没有任何机制清理，manifest 上于是留着一片
    过期的 sequence_reviewed_pass——修完 IMG 005 后 IMG 004/006 仍显示"审查通过"，
    前端据此显示的"全部通过"从那一刻起就是假的。锚点门早有 anchor_prompt_sha256 这套
    指纹机制，一致性审查此前完全没有对应物。

    判定依据是审查时记下的 review_frames_sha256（{帧序号: 内容哈希}，见
    pipeline_orchestrator._record_review_fingerprints）。作废＝结论回落
    pending_manual_review、清掉 vlm_qa_reason 与结构化 review_issues；有任何一帧作废时
    manifest['chain_drift'] 一并丢弃（链尾回望比的是同一批帧图）。
    调用方负责写回 manifest。"""
    frames = (manifest or {}).get('frames') or []
    if not frames:
        return []
    frames_dir = os.path.join(project_dir, 'frames')
    live = {}

    def _live_hash(seq):
        if seq not in live:
            live[seq] = frame_content_hash(os.path.join(frames_dir, f'img_{seq:03d}.webp'))
        return live[seq]

    changed = []
    for frame in frames:
        recorded = frame.get('review_frames_sha256')
        if not isinstance(recorded, dict) or not recorded:
            continue
        stale = False
        for seq_str, recorded_hash in recorded.items():
            try:
                seq = int(seq_str)
            except (TypeError, ValueError):
                continue
            if _live_hash(seq) != recorded_hash:
                stale = True
                break
        if not stale:
            continue
        changed.append(frame.get('sequence'))
        frame.pop('review_frames_sha256', None)
        frame.pop('reviewed_at', None)
        frame.pop('review_issues', None)
        if frame.get('quality_gate') in REAL_REVIEW_VERDICTS:
            frame['quality_gate'] = 'pending_manual_review'
            frame['vlm_qa_reason'] = None
    if changed:
        manifest.pop('chain_drift', None)
    return [s for s in changed if isinstance(s, int)]


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
        # 2026-07-12 防呆：内存任务表为空时一律跳过清理——空内存 + 磁盘有任务文件
        # 意味着本实例没有(或还没)加载历史任务（启动竞态/加载失败/幽灵实例），此时
        # “孤儿清理”会把全部任务历史当垃圾删光（实际发生过一次，tasks/ 被整目录清空）。
        if not active_ids:
            return
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


def prepare_task_for_run(task_id, dimensions=None):
    """为一次生成运行准备任务记录，返回 (task, already_running)。

    与 get_or_create_task 的区别：重试复用旧 task_id 时，终态记录
    （failed/cancelled/completed）被原地重置后复用——重试覆盖被重试的
    那条任务记录，而不是留下失败记录再另开一条新记录。

    - 记录不存在 → 新建（等价 get_or_create_task）
    - 记录存在且 running → 原样返回 already_running=True：调用方不得再起
      第二个 worker（连点重试只是重新挂回同一条流）
    - 记录存在且终态 → 清空 events/result/error、换新 cancel_event 后复用；
      listeners 保留，仍挂着的旁观流会直接收到新一轮运行的事件
    """
    with ACTIVE_TASKS_LOCK:
        t = ACTIVE_TASKS.get(task_id)
        if t is not None and t["status"] == "running":
            return t, True
        if t is None:
            t = {
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
            ACTIVE_TASKS[task_id] = t
        else:
            t["status"] = "running"
            t["events"] = []
            t["cancel_event"] = threading.Event()
            t["result"] = None
            t["error"] = None
            t["last_active"] = time.time()
            if dimensions is not None:
                t["dimensions"] = dimensions
    save_tasks_to_disk()
    return t, False


# 任务进度事件 → 日志级别。没列出的 stage 一律 INFO。
_TASK_STAGE_LEVELS = {
    # 上游失败/降级/需要人工介入——这些正是"出问题了"的信号，要能被
    # 日志面板的「只看问题」和胶囊未读徽标抓到
    'upstream_retry': 'WARN',
    'transport_fallback': 'WARN',
    'video_warning': 'WARN',
    'video_retry_autonomous': 'WARN',
    'needs_human_review': 'WARN',
    'anchor_retry': 'WARN',
    'video_error': 'ERROR',
    # 高频、单条信息量低的过程量
    'batch': 'DEBUG',
    'batch_generating': 'DEBUG',
}

# 事件里可能挂着整份 manifest / 帧条目 / prompt block，直接 str() 会把一整屏
# JSON 灌进日志。这些字段只留可辨识的摘要。
_TASK_EVENT_BRIEF_KEYS = ('sequence', 'seq', 'current', 'total', 'status', 'reason', 'path')


def _fmt_task_event(data, limit=220):
    """把事件 payload 压成一行。日志面板要的是"第几帧、成没成、为什么失败"，
    不是整份结构体。"""
    if data is None:
        return ''
    if not isinstance(data, dict):
        s = str(data)
        return s if len(s) <= limit else s[:limit] + '…'
    parts = []
    for k, v in data.items():
        if isinstance(v, dict):
            keep = {kk: v[kk] for kk in _TASK_EVENT_BRIEF_KEYS if v.get(kk) is not None}
            v = keep or f'<{len(v)}字段>'
        elif isinstance(v, (list, tuple, set)):
            v = f'<{len(v)}项>'
        else:
            s = str(v)
            if len(s) > 80:
                v = s[:80] + '…'
        parts.append(f'{k}={v}')
    out = ' '.join(parts)
    return out if len(out) <= limit else out[:limit] + '…'


def _mirror_task_event_to_log(task_id, event_type, data):
    """把任务进度事件顺手记进主日志。

    这条通道此前只喂给订阅了 /api/tasks/<id>/stream 的进度 UI，从不落 server.log：
    结果是帧序列跑到一半出问题时，日志面板里一条帧级线索都没有，只能去盯进度条，
    而进度条又只显示"当前在干嘛"、不留历史。并进主日志之后，日志面板（tail
    server.log）自动就有了这些行，级别过滤 / 按任务 id 过滤 / 重复行折叠也一并
    适用，前端不必再开第二条 SSE 订阅。"""
    try:
        if event_type == 'text_chunk':
            # 逐 token 的流式文本，一次生成几千条——进度 UI 要用，日志不要
            return
        if event_type == 'progress' and isinstance(data, dict):
            stage = data.get('stage') or 'progress'
            payload = data.get('details')
        else:
            stage = event_type
            payload = data
        level = 'ERROR' if event_type == 'error' else _TASK_STAGE_LEVELS.get(stage, 'INFO')
        body = _fmt_task_event(payload)
        log(level, 'TASK', f"{stage} {body}".strip(), task_id=task_id)
    except Exception:
        # 记日志绝不能把任务本身搞挂
        pass


# ── FX 阶段时间线（2026-07-26）────────────────────────────────────────────────
# 任务事件本身不带时间戳，于是"卡在哪一步、卡了多久"只能去日志里翻。这里在事件
# 广播的必经之路上顺手记一条带时间戳的阶段轨迹，控制台据此把超时阶段标红——
# 典型的"等工具栏空转 180s"就能一眼看到，而不是靠读 3MB 日志推断。
#
# 只保留每个任务最近 _STAGE_TIMELINE_LIMIT 个阶段、最多 _STAGE_TIMELINE_TASKS 个
# 任务（按 LRU 淘汰），纯内存、不落盘：它服务的是"现在卡在哪"，不是历史审计。
_STAGE_TIMELINE_LIMIT = 40
_STAGE_TIMELINE_TASKS = 60
_STAGE_TIMELINE = collections.OrderedDict()
_STAGE_TIMELINE_LOCK = threading.Lock()

# 这些事件是数据流噪音，不构成"阶段"
_STAGE_TIMELINE_SKIP = {'text_chunk', 'heartbeat'}


def record_task_stage(task_id, event_type, data):
    """记录一次阶段进入（同一阶段连续出现只累加计数，不新建行）。"""
    if event_type in _STAGE_TIMELINE_SKIP:
        return
    data = data if isinstance(data, dict) else {}
    stage = str(data.get('stage') or event_type)
    now = time.time()
    with _STAGE_TIMELINE_LOCK:
        rows = _STAGE_TIMELINE.get(task_id)
        if rows is None:
            rows = []
            _STAGE_TIMELINE[task_id] = rows
            while len(_STAGE_TIMELINE) > _STAGE_TIMELINE_TASKS:
                _STAGE_TIMELINE.popitem(last=False)
        _STAGE_TIMELINE.move_to_end(task_id)
        if rows and rows[-1]['stage'] == stage:
            rows[-1]['last_at'] = now
            rows[-1]['count'] += 1
        else:
            rows.append({
                'stage': stage,
                'at': now,
                'last_at': now,
                'count': 1,
                'message': str(data.get('message') or data.get('reason') or '')[:160],
                'event_type': str(event_type),
                'code': str(data.get('code') or ''),
                'max_wait_seconds': data.get('max_wait_secs') or data.get('max_wait_seconds'),
            })
            del rows[:-_STAGE_TIMELINE_LIMIT]


def task_stage_timeline(task_id):
    with _STAGE_TIMELINE_LOCK:
        return [dict(row) for row in _STAGE_TIMELINE.get(task_id, [])]


def notify_listeners(task_id, event_type, data):
    t = ACTIVE_TASKS.get(task_id)
    if not t:
        return
    t["last_active"] = time.time()
    try:
        record_task_stage(task_id, event_type, data)
    except Exception:
        pass  # 时间线纯诊断用，绝不能影响事件广播
    _mirror_task_event_to_log(task_id, event_type, data)
    
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
    model = config.get('model') or 'gemini-3.6-flash-high'
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
    model = config.get('model') or 'gemini-3.6-flash-high'
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
