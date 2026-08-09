import os
import sys
import json
import hashlib
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

# fx_console 用 ANSI 转义给控制台上色/画框。那些字节原样落进 server.log 之后，
# 前端日志面板（纯文本渲染，没有终端解释器）把它们当普通字符显示出来——面板里
# 那些 "[0m" 和半截框线就是这么来的。落盘这一路统一剥掉，控制台那一路不动，
# 终端里的彩色输出照旧。
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def strip_ansi(text):
    return _ANSI_RE.sub('', text) if isinstance(text, str) else text

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
        # 只在落盘这一路剥 ANSI：这里拿到的一定是完整一行（write() 已按 "\n" 切好），
        # 转义序列不会横跨两次调用被切断。
        line = strip_ansi(line)
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


def http_access_logging():
    """是否把正常的 HTTP 往返也记进日志：2xx/3xx，以及 /outputs 下"文件还没生成"
    的 404 探测（帧文件名是确定性的，前端会在生成完成前就来取，那个 404 是预期
    行为不是故障）。4xx/5xx 里真正的异常不受这个开关影响，永远留痕。

    默认关闭。实测这类行占 server.log 的 81%（43,284 行里 35,249 行），把真正
    有用的行冲得找不到。

    刻意不挂在 DEBUG_MODE 上——"我要看异常堆栈"和"我要看每一个 304"是两个不同
    的需求，绑在一起正是日志一开 debug 就彻底没法读的原因。要看访问日志时单独
    打开 logHttpAccess / SPARK_LOG_HTTP_ACCESS。"""
    return bool(SERVER_CONFIG.get('logHttpAccess') or os.environ.get('SPARK_LOG_HTTP_ACCESS'))


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

def _path_setting(env_key, cfg_key, default):
    """可被环境变量/配置覆盖的数据文件路径；不设置就是仓库根目录下那一份。

    留这个口子是因为自动化测试必须能把真实创意库隔离开：测试驱动真实页面时，
    页面的 saveLibrary() 会把它当时的 savedIdeas 整份 POST 回 /api/library，
    指向真库就是一次整库覆盖（2026-07-27 实际发生过一次，靠 .recovery 快照
    与 tools/recover_ice_cave_all.py 的确定性重建捞回）。
    """
    raw = os.environ.get(env_key) or SERVER_CONFIG.get(cfg_key) or default
    return str(raw)


PORT = _int_setting('PORT', 'port', 8085)
DB_FILE = _path_setting('SPARK_DB_FILE', 'dbFile', 'library.json')
LEDGER_FILE = _path_setting('SPARK_LEDGER_FILE', 'ledgerFile', 'topic_ledger.json')
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
# ── 技能 profile：目标视频模型 → 技能包 的注册表 ──
# 一个 profile = 一个技能包 + 它自己的契约清单。清单必须按包分开声明：两个包的
# references/ 文件名完全不重叠（base 是 prompt-templates/spatial-consistency…，
# omni 是 omni-*.md），拿 base 的清单去查 omni 包会报"缺 8 个文件"——包明明是全的。
#
# 每份清单存在的意义都一样：契约文件缺失不会报错，load_reference_file 返回空串、
# run_ideate 拿到空的形态矩阵/台账，合成照样跑完，只是质量悄悄劣化。清单就是把
# "悄悄"变成"吵闹"（启动日志 + /api/mode 上报给前端）。
_BASE_CONTRACT_FILES = (
    'SKILL.md',
    'references/prompt-templates.md',
    'references/idea-engine.md',
    'references/used-topic-ledger.md',
    'references/space-workflows.md',
    'references/spatial-consistency-upgrade-protocol.md',
    'references/drift-lock-assembly-guide.md',
    'references/threshold-bridge-consistency-protocol.md',
)
# omni 包自己的 SKILL.md §Required Reference Loading 声明了 7 个"每次必读" + 4 个
# "按需读"，全都算契约：按需的那几个（过门、样例梯、Tier 0 的形态矩阵与台账）缺失
# 同样是无声降级，只是触发条件更窄。
_OMNI_CONTRACT_FILES = (
    'SKILL.md',
    'references/omni-scene-skeleton.md',
    'references/omni-multishot-language.md',
    'references/omni-restoration-continuity.md',
    'references/omni-beat-skeleton.md',
    'references/omni-damage-vocabulary.md',
    'references/omni-lighting-environment-audio.md',
    'references/omni-output-templates.md',
    'references/omni-threshold-bridge.md',
    'references/omni-worked-ladders.md',
    'references/idea-engine.md',
    'references/used-topic-ledger.md',
)

# ── 契约注册表 ──
# 上面那份 _*_CONTRACT_FILES 只回答"文件在不在"，回答不了"SKILL.md 里写的契约有没有
# 人执行"。运行时从不读 SKILL.md（它只做存在性证明），全部执行都是 Python 门禁手写的
# 一份平行实现——散文改了、门禁没跟，两边就会静默分叉，而这正是最难发现的一类劣化。
# contract-registry.json 把每条契约钉到一个真实存在的执行者上，由
# tests/test_skill_contract_registry.py 逐条 import 校验。
#
# 它**不进** _*_CONTRACT_FILES：那份清单参与 vendored 完整性判定与自动探测，加一项会
# 让所有还没带注册表的历史技能包一夜之间"不完整"，把灾备入口也一起堵死。注册表缺失
# 是需要单独上报的一种状态，不是"这个包坏了"。
SKILL_REGISTRY_REL = 'references/contract-registry.json'
# 主版本相同即视为兼容（次版本用于增补契约条目）。技能包声明的主版本与这里不一致，
# 说明包与运行时脱节——照跑会按错误的契约集合审计，所以要一路上报到前端。
SUPPORTED_CONTRACT_VERSION = '1.0'

DEFAULT_SKILL_PROFILE = 'base'
SKILL_PROFILES = {
    'base': {
        'package': 'gemini-veo-restoration-composer',
        'label': '修复延时合成器（Veo / 通用）',
        'contracts': _BASE_CONTRACT_FILES,
        # 环境变量与配置键保持历史名字：这一层在多 profile 之前就存在，改名会让
        # 已经配好的机器在升级后静默回到默认路径。
        'env': 'SKILL_DIR',
        'config_key': 'skillDir',
    },
    'omni': {
        'package': 'gemini-omni-restoration-composer',
        'label': 'Gemini Omni 多镜头合成器',
        'contracts': _OMNI_CONTRACT_FILES,
        'env': 'SKILL_DIR_OMNI',
        'config_key': None,  # 只认 skillProfiles.omni，不再给每个包发一个顶层键
    },
}
# 旧名保留：外部（测试、frame_generator）按这个名字引用 base 的契约清单。
SKILL_CONTRACT_FILES = _BASE_CONTRACT_FILES
SKILL_PACKAGE_NAME = SKILL_PROFILES[DEFAULT_SKILL_PROFILE]['package']

# 「做哪个模型的提示词 → 读哪个技能包」。匹配 videoModel（配置或前端请求里带的那个）
# 的小写子串，命中即用对应 profile；都不命中回到 base。用子串而不是全等，是因为
# 视频模型名带档位后缀（'Omni Flash'、'Veo 3.1 - Lite [Lower Priority]'）。
SKILL_PROFILE_VIDEO_MODEL_RULES = (
    ('omni', 'omni'),
)

# ── 技能包（skill）本地路径的解析 ──
# 取值优先级：完整的仓库内置 skills/<包名> > 环境变量 >
# server_config.json > 旧的 ~/.codex 默认路径 > 自动探测。仓库内包是与运行时
# 代码同步版本化的契约；只要它完整，机器本地的下载目录或 ~/.codex 就不得
# 覆盖它。显式路径仅在仓库内包缺失/契约不完整时才是灾备入口。
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_VENDORED_SKILL_ROOT = os.path.join(_PROJECT_ROOT, 'skills')
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
_IGNORED_SKILL_OVERRIDE_WARNED = set()


def _normalize_skill_profile(profile):
    """把 None / 未知名字归一到已注册的 profile；未知名字回落到 base 而不是抛错。

    这条路径上的输入有一部分来自前端配置（skillProfile、videoModel），拼错一个字母
    不该让激发/合成整条链路 500——回落到 base 再把这件事喊出来即可。"""
    name = str(profile or '').strip().lower() or DEFAULT_SKILL_PROFILE
    return name if name in SKILL_PROFILES else DEFAULT_SKILL_PROFILE


def skill_contract_files(profile=None):
    """某个 profile 的契约清单（相对该包根目录）。"""
    return SKILL_PROFILES[_normalize_skill_profile(profile)]['contracts']


def _skill_contract_hits(directory, profile=None):
    """directory 下命中了几个契约文件（用于挑"最像这个 profile 的技能包"的目录）。"""
    if not directory or not os.path.isdir(directory):
        return 0
    return sum(1 for rel in skill_contract_files(profile)
               if os.path.exists(os.path.join(directory, *rel.split('/'))))


def _expand_local_path(raw):
    """把配置里写的路径归一成绝对路径：支持 ~、$VAR，相对路径按本项目根目录解析。"""
    p = os.path.expanduser(os.path.expandvars(str(raw or '').strip()))
    if not p:
        return ''
    if not os.path.isabs(p):
        p = os.path.join(_PROJECT_ROOT, p)
    return os.path.normpath(p)


def _autodetect_skill_dir(profile=None):
    """在常见技能根目录里挑该 profile 契约覆盖最全的那个包；都不够格时返回空串。"""
    best, best_hits = '', 0
    for root in _SKILL_ROOT_CANDIDATES:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            candidate = os.path.join(root, name)
            hits = _skill_contract_hits(candidate, profile)
            if hits > best_hits:
                best, best_hits = candidate, hits
    return best if best_hits >= _SKILL_AUTODETECT_MIN_HITS else ''


def _configured_skill_dir(profile):
    """server_config.json 里为该 profile 显式写的路径（未写返回空串）。

    两个来源：通用的 skillProfiles.<profile>，以及 base 保留的历史顶层键 skillDir。
    前者优先——写了更明确的那个就该赢。"""
    profiles_cfg = SERVER_CONFIG.get('skillProfiles')
    if isinstance(profiles_cfg, dict):
        raw = profiles_cfg.get(profile)
        if raw and str(raw).strip():
            return str(raw)
    legacy_key = SKILL_PROFILES[profile].get('config_key')
    if legacy_key:
        raw = SERVER_CONFIG.get(legacy_key)
        if raw and str(raw).strip():
            return str(raw)
    return ''


def _resolve_skill_dir(profile=None):
    """返回 (路径, 来源)，来源取值 vendored / env / config / default /
    autodetect。完整的 vendored 包始终是权威来源；显式路径是缺包时的灾备，
    不是覆盖仓库契约的插件机制。"""
    profile = _normalize_skill_profile(profile)
    spec = SKILL_PROFILES[profile]

    vendored = os.path.join(_VENDORED_SKILL_ROOT, spec['package'])
    vendored_hits = _skill_contract_hits(vendored, profile)
    vendored_complete = vendored_hits == len(skill_contract_files(profile))

    env_raw = os.environ.get(spec['env'])
    cfg_raw = _configured_skill_dir(profile)
    if vendored_complete:
        ignored = env_raw or cfg_raw
        if ignored and profile not in _IGNORED_SKILL_OVERRIDE_WARNED:
            _IGNORED_SKILL_OVERRIDE_WARNED.add(profile)
            if sys.stdout:
                print(
                    f"[WARN] {profile} skill 的外部覆盖路径 {ignored!r} 已忽略："
                    f"仓库内置契约包完整，必须优先使用 {vendored}"
                )
        return vendored, 'vendored'

    if env_raw and env_raw.strip():
        return _expand_local_path(env_raw), 'env'
    if cfg_raw:
        return _expand_local_path(cfg_raw), 'config'
    if _skill_contract_hits(vendored, profile):
        return vendored, 'vendored'
    legacy_default = os.path.join(
        os.path.dirname(_DEFAULT_SKILL_DIR), spec['package'])
    if _skill_contract_hits(legacy_default, profile):
        return legacy_default, 'default'
    found = _autodetect_skill_dir(profile)
    if found:
        return found, 'autodetect'
    # 一个都没找到：把 base 指回历史默认路径（既有告警文案与测试都按这个来），其余
    # profile 指向仓库内该包应该在的位置——缺失清单指着一个"本该由 git 管起来"的
    # 目录，比指着某台机器的 ~/.codex 更好修。
    if profile == DEFAULT_SKILL_PROFILE:
        return _DEFAULT_SKILL_DIR, 'default'
    return vendored, 'vendored'


def _skill_config_mtime():
    try:
        return os.path.getmtime(SERVER_CONFIG_FILE)
    except OSError:
        return None


# base 的路径同时暴露成模块全局（历史接口：frame_generator / video_generator 直接
# import SKILL_DIR，测试也直接 patch 它）；其余 profile 只存在这张表里。
_SKILL_DIRS = {name: _resolve_skill_dir(name) for name in SKILL_PROFILES}
SKILL_DIR, SKILL_DIR_SOURCE = _SKILL_DIRS[DEFAULT_SKILL_PROFILE]
_SKILL_CONFIG_MTIME = _skill_config_mtime()


def _refresh_skill_dirs_if_config_changed():
    """server_config.json 的 mtime 变了就把所有 profile 的路径重算一遍。

    所以改完配置下一次「激发创意」立即用新路径，不必重启服务。除技能路径相关的键
    外一律不回灌，免得把启动时就固化成模块常量的那些配置（端口、频控…）搞成半新半旧。"""
    global SKILL_DIR, SKILL_DIR_SOURCE, _SKILL_CONFIG_MTIME
    mtime = _skill_config_mtime()
    if mtime == _SKILL_CONFIG_MTIME:
        return
    _SKILL_CONFIG_MTIME = mtime
    try:
        fresh = _load_server_config()
    except Exception:
        fresh = SERVER_CONFIG
    for key in ('skillDir', 'skillProfiles', 'skillProfile', 'videoModel'):
        if fresh.get(key):
            SERVER_CONFIG[key] = fresh.get(key)
        else:
            SERVER_CONFIG.pop(key, None)
    for name in SKILL_PROFILES:
        _SKILL_DIRS[name] = _resolve_skill_dir(name)
    SKILL_DIR, SKILL_DIR_SOURCE = _SKILL_DIRS[DEFAULT_SKILL_PROFILE]


def skill_dir(profile=None):
    """技能文件的实际读取路径——激发/合成每次要读契约文件时都走这里。"""
    profile = _normalize_skill_profile(profile)
    _refresh_skill_dirs_if_config_changed()
    # base 一律读模块全局：测试与旧代码会直接 patch server_common.SKILL_DIR，
    # 从表里取会把那种 patch 静默吃掉。
    if profile == DEFAULT_SKILL_PROFILE:
        return SKILL_DIR
    return _SKILL_DIRS[profile][0]


def skill_dir_source(profile=None):
    profile = _normalize_skill_profile(profile)
    _refresh_skill_dirs_if_config_changed()
    if profile == DEFAULT_SKILL_PROFILE:
        return SKILL_DIR_SOURCE
    return _SKILL_DIRS[profile][1]


def skill_reference_path(name, profile=None):
    """技能包 references/ 下某个文件的绝对路径（每次都按当前 skill_dir() 拼）。"""
    return os.path.join(skill_dir(profile), 'references', name)


def missing_skill_contract_files(profile=None):
    """该 profile 契约清单里当前不存在的那些（相对路径，保持声明顺序）。"""
    base = skill_dir(profile)
    return [rel for rel in skill_contract_files(profile)
            if not os.path.exists(os.path.join(base, *rel.split('/')))]


# 注册表按 (路径, mtime) 缓存：合成逐拍调用报告，每次重读磁盘既慢又会把一个损坏的
# JSON 刷成满屏告警。mtime 变了才重读，所以改完注册表不用重启。
_SKILL_REGISTRY_CACHE = {}


def _major(version):
    return str(version or '').strip().split('.')[0]


def skill_contract_registry(profile=None):
    """该 profile 技能包里的契约注册表，返回 (数据, 状态)。

    状态取值：ok / missing / unreadable / version_mismatch。数据在 missing 与
    unreadable 时为 None；version_mismatch 时仍返回解析结果——调用方要拿它里面的
    contract_version 报给用户看"包声明的是几"。"""
    path = os.path.join(skill_dir(profile), *SKILL_REGISTRY_REL.split('/'))
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None, 'missing'

    cached = _SKILL_REGISTRY_CACHE.get(path)
    if not cached or cached[0] != stamp:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError('registry root is not an object')
        except (OSError, ValueError) as e:
            _SKILL_REGISTRY_CACHE[path] = (stamp, None, str(e))
        else:
            _SKILL_REGISTRY_CACHE[path] = (stamp, data, None)
        cached = _SKILL_REGISTRY_CACHE[path]

    _stamp, data, err = cached
    if data is None:
        return None, 'unreadable'
    if _major(data.get('contract_version')) != _major(SUPPORTED_CONTRACT_VERSION):
        return data, 'version_mismatch'
    return data, 'ok'


def skill_contract_strict():
    """严格模式：技能契约缺失时把"静默降级"升级成"直接失败"。

    默认关（历史行为：缺文件只打 WARN 照跑）。想让一次配错的部署当场炸出来、而不是
    连续产出几十条劣化提示词的人，把它打开。环境变量优先于配置文件，便于 CI 单次开启。"""
    raw = os.environ.get('SKILL_CONTRACT_STRICT')
    if raw is None or not str(raw).strip():
        raw = SERVER_CONFIG.get('strictSkillContract')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def skill_contract_report(profile=None):
    """技能契约现状的单一事实来源：{'profile','label','package','dir','source',
    'missing','total','contract_version','registry_status','registry_expected',
    'unenforced'}。

    调用方（启动检查、/api/mode）一律走这个函数而不是自己拼 SKILL_DIR——server.py 是
    `from server_common import *` 进来的，那份 SKILL_DIR 是导入时的**副本**，之后改
    server_common.SKILL_DIR 它不会跟着变（测试里改路径、配置热切换时都会踩到）。
    dir 在本函数内取值，才保证和 missing 的判定基于同一个路径。"""
    profile = _normalize_skill_profile(profile)
    spec = SKILL_PROFILES[profile]
    registry, registry_status = skill_contract_registry(profile)
    entries = (registry or {}).get('contracts')
    entries = entries if isinstance(entries, list) else []
    return {
        'profile': profile,
        'label': spec['label'],
        'package': spec['package'],
        'dir': skill_dir(profile),
        'source': skill_dir_source(profile),
        'missing': missing_skill_contract_files(profile),
        'total': len(spec['contracts']),
        # 注册表侧：契约总数、包声明的版本、以及登记在案的无执行者缺口。前端据此
        # 区分"文件缺了"（missing）和"文件在但契约与代码脱节"（registry_status）。
        'contract_version': (registry or {}).get('contract_version'),
        'registry_expected': SUPPORTED_CONTRACT_VERSION,
        'registry_status': registry_status,
        'contract_count': len(entries),
        'unenforced': [c.get('id') for c in entries
                       if isinstance(c, dict) and not c.get('enforcer')],
    }


def skill_contract_reports():
    """全部 profile 的契约现状，按注册顺序。启动日志与 /api/mode 用它一次报全：
    只报当前激活的那个，等于把"另一个包没装好"留到用户切模型的那一刻才炸。"""
    return [skill_contract_report(name) for name in SKILL_PROFILES]


def profile_for_video_model(video_model):
    """「做哪个模型的提示词」→「读哪个技能包」。不认识的模型名一律回 base。"""
    haystack = str(video_model or '').lower()
    for needle, profile in SKILL_PROFILE_VIDEO_MODEL_RULES:
        if needle in haystack:
            return profile
    return DEFAULT_SKILL_PROFILE


_SKILL_PROFILE_OVERRIDE_WARNED = set()


def active_skill_profile(config=None):
    """本次请求该用哪个 profile。

    优先级：环境变量 SKILL_PROFILE > 配置 skillProfile > 按 videoModel 推断。
    显式覆盖存在的意义：换提示词风格不该只能靠改视频模型下拉框——反过来，只想换
    渲染档位的人也不该被顺手改掉提示词语法。两个值都取 'auto' 时才走推断。

    config 是本次请求带上来的前端配置（run_ideate/合成都会收到），没带就退回
    服务端 server_config.json。"""
    _refresh_skill_dirs_if_config_changed()
    cfg = config if isinstance(config, dict) else SERVER_CONFIG
    raw = os.environ.get('SKILL_PROFILE') or cfg.get('skillProfile') \
        or SERVER_CONFIG.get('skillProfile') or 'auto'
    raw = str(raw).strip().lower()
    if raw and raw != 'auto':
        if raw in SKILL_PROFILES:
            return raw
        if raw not in _SKILL_PROFILE_OVERRIDE_WARNED:
            _SKILL_PROFILE_OVERRIDE_WARNED.add(raw)
            if sys.stdout:
                print(f"[WARN] 未知的 skillProfile '{raw}'，本次按 videoModel 推断；"
                      f"可选值：auto/{'/'.join(SKILL_PROFILES)}")
    return profile_for_video_model(cfg.get('videoModel') or SERVER_CONFIG.get('videoModel'))


# ── 单段视频时长 ──
# omni 的时间线提示词把镜头切点钉在秒上，所以**合成端与生成端必须对同一个数达成一致**。
# 这就是 videoDuration 不能再留空的原因：空值原本表示"沿用 Flow 面板当前时长"，那是个
# 不可知态——提示词侧不知道该按几秒排镜头，生成端也不知道面板上残留的是什么值。
# 只有 Omni Flash 的 Flow 面板提供 4/6/8/10s 时长 tab；其余模型时长固定 8 秒
# （video_generator._CLIP_BASE_SECONDS 同款口径）。
FIXED_VIDEO_DURATION = 8
OMNI_VIDEO_DURATIONS = (4, 6, 8, 10)
OMNI_DEFAULT_VIDEO_DURATION = 10  # 满六镜所需的时长，见 omni composer 的镜头梯表


def resolve_video_duration(config=None):
    """本次生成的单段视频时长（秒，int）。永远返回一个确定的数，不返回 None。

    非 Omni 系列模型一律 8 秒（面板固定，不可调）；Omni 系列模型取 videoDuration/video_duration，
    缺失/非法值回落到 10 秒。"""
    cfg = config if isinstance(config, dict) else SERVER_CONFIG
    model = str(cfg.get('videoModel') or cfg.get('video_model') or SERVER_CONFIG.get('videoModel') or '').strip().lower()
    if 'omni' not in model:
        return FIXED_VIDEO_DURATION
    raw = cfg.get('videoDuration') if 'videoDuration' in cfg else cfg.get('video_duration')
    if raw in (None, ''):
        raw = SERVER_CONFIG.get('videoDuration')
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return OMNI_DEFAULT_VIDEO_DURATION
    return value if value in OMNI_VIDEO_DURATIONS else OMNI_DEFAULT_VIDEO_DURATION



# ── 历史选题台账（used-topic-ledger.md）的可写位置 ──
# 这份文件是**运行时被追加写**的（每选中一个选题就落一行），却长在技能包的
# references/ 下。技能包进了 git 之后，它会变成"每合成一次就脏一次"的跟踪文件；
# 而且它按 profile 分裂就等于把去重记忆劈成两半——同一个选题在 Veo 侧用过、在
# Omni 侧还能再被激发出来，那是同一条视频换个分镜语法，不是新选题。
# 所以：写入统一去 runtime/（已整目录 gitignore），包内那份降级为只读种子，
# 首次使用时整份拷过去，历史记录不丢。
USED_TOPIC_LEDGER_FILE = _path_setting(
    'SPARK_USED_TOPIC_LEDGER_FILE', 'usedTopicLedgerFile',
    os.path.join('runtime', 'used-topic-ledger.md'))
USED_TOPIC_LEDGER_SEED = 'used-topic-ledger.md'


def used_topic_ledger_path():
    """可写台账的绝对路径（相对路径按项目根解析）。"""
    p = USED_TOPIC_LEDGER_FILE
    if not os.path.isabs(p):
        p = os.path.join(_PROJECT_ROOT, p)
    return os.path.normpath(p)


def ensure_used_topic_ledger(profile=None):
    """返回可写台账路径；首次调用时从技能包里的种子整份拷贝。

    种子按 profile 取，取不到再退到 base——两个包的台账是同一份语料（omni 那份是
    从 base 继承来的，只多一段 provenance 说明），所以谁先播种都不影响去重。"""
    path = used_topic_ledger_path()
    if os.path.exists(path):
        return path
    seeds = [skill_reference_path(USED_TOPIC_LEDGER_SEED, profile)]
    if _normalize_skill_profile(profile) != DEFAULT_SKILL_PROFILE:
        seeds.append(skill_reference_path(USED_TOPIC_LEDGER_SEED, DEFAULT_SKILL_PROFILE))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for seed in seeds:
            if os.path.exists(seed):
                shutil.copyfile(seed, path)
                if sys.stdout:
                    print(f"[LEDGER] 已从技能包种子初始化可写台账: {seed} → {path}")
                return path
        # 种子也缺：建一个空文件，让后续追加有落点（去重能力这次确实是降级的，
        # 契约缺失清单里会把 references/used-topic-ledger.md 报出来）。
        with open(path, 'w', encoding='utf-8') as f:
            f.write('# Used Topic Ledger\n\n')
    except Exception as e:
        if sys.stdout:
            print(f"Warning: could not initialize used-topic-ledger at {path} ({e})")
    return path

# ============================================================================
# 运行时能力探针（Runtime Capability Report）
# ----------------------------------------------------------------------------
# 这一层存在的唯一理由：把「悄悄劣化」变成「清单上写着」。
#
# 两处已知的静默失效：
#   · numpy 缺失 → 本地视觉探针（防串片的首尾帧锚点比对、i2v 帧对契约、冻结片段
#     检测、换族惯性检测）全部走 except 分支静默返回 'skipped'/False。设计如此
#     （探针不该拖垮主流程），后果是整套内容级校验消失而日志上看不出异常。
#   · 技能包契约文件缺失 → load_reference_file 返回空串，合成按空契约跑完，创意维度
#     变窄、一致性约束消失。启动日志与 /api/mode 会喊，但那是**服务级**信号，翻不到
#     具体某一单上：三天后看着一单成片，无从知道它当初是不是在缺契约的状态下合成的。
#
# 所以除了服务级告警，还要把当时的能力状态写进每一单的 manifest（见
# frame_generator.stamp_manifest_capabilities）。"这单的内容级校验根本没跑" 必须是
# 清单上的一行，而不是要靠人回忆环境。
def _module_available(name):
    """只探"能不能 import"，不真正持有模块引用——探针本身不该把 numpy 常驻进来。"""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def runtime_capability_report(config=None):
    """{'degraded': [...], 'numpy': bool, 'pillow': bool, 'ffmpeg': bool,
        'skill_profile': str, 'skill_contract_missing': [...]}。

    degraded 是给人看的中文短句列表，空列表 = 能力齐全。判定全部即时进行，不做缓存：
    这几样都可能在服务运行期间被装上/删掉（换 venv、改 skillDir），缓存只会让清单
    记录当年那一刻的假象。

    契约按**本单实际用的那个 profile**查（config 是本次请求的配置）：这一层存在的
    全部理由就是"三天后看着一单成片，能知道它当初是在哪个包、缺不缺契约的状态下
    合成的"——固定查 base 的话，一单 omni 成片的记录就是错的。"""
    numpy_ok = _module_available('numpy')
    pillow_ok = _module_available('PIL')
    ffmpeg_ok = bool(shutil.which('ffmpeg'))
    profile = active_skill_profile(config)
    missing_skill = missing_skill_contract_files(profile)
    degraded = []
    if not numpy_ok:
        degraded.append(
            'numpy 缺失：本地视觉探针（首尾帧防串片、i2v 帧对契约、冻结检测、'
            '换族惯性检测）全部静默跳过，本单未做任何内容级校验')
    if not pillow_ok:
        degraded.append('Pillow 缺失：帧转档/封面合成/审查前压图不可用')
    if not ffmpeg_ok:
        degraded.append('ffmpeg 不在 PATH：视频抽帧类校验（防串片、冻结检测）无法进行')
    if missing_skill:
        degraded.append(
            f'技能契约缺 {len(missing_skill)}/{len(skill_contract_files(profile))} 个文件'
            f'（{profile} 包：{", ".join(missing_skill)}）：提示词合成按空契约降级，'
            f'创意维度变窄、一致性约束消失')
    return {
        'degraded': degraded,
        'numpy': numpy_ok,
        'pillow': pillow_ok,
        'ffmpeg': ffmpeg_ok,
        'skill_profile': profile,
        'skill_contract_missing': missing_skill,
    }


def stamp_manifest_capabilities(manifest, stage):
    """把当时的运行时能力状态盖进 manifest（就地修改，不落盘——调用方紧接着会写）。

    形态：manifest['capability_degraded'] = {stage: {'at': 时间戳, 'issues': [中文短句]}}。
    某阶段能力齐全时删掉它自己那一条（环境补好后重渲，旗标必须能消失，否则清单会
    永久挂着一条早已修好的告警）；整个字典空了就把键删掉。

    stage: 'frames' | 'videos'。分阶段记是因为两个阶段可能跨越环境变化（装上 numpy、
    改了 skillDir），而且劣化后果不同：帧阶段丢的是换族惯性检测，视频阶段丢的是
    防串片与冻结检测。"""
    if not isinstance(manifest, dict):
        return
    issues = runtime_capability_report()['degraded']
    stamps = manifest.get('capability_degraded')
    if not isinstance(stamps, dict):
        stamps = {}
    if issues:
        stamps[stage] = {
            'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'issues': issues,
        }
        if sys.stdout:
            print(f"[CAPABILITY] {stage} 阶段在能力劣化状态下运行，已写入 manifest: "
                  f"{'; '.join(issues)}")
    else:
        stamps.pop(stage, None)
    if stamps:
        manifest['capability_degraded'] = stamps
    else:
        manifest.pop('capability_degraded', None)


# ============================================================================
# 运行时版本指纹（Runtime Version Fingerprint）
# ----------------------------------------------------------------------------
# 2026-08-06 复盘：一次失败任务跑在 9:40 启动的旧进程上，而修复它的代码 14:28 才
# 落盘——旧进程仍在内存里跑着没重启，用户看到的"失败"其实是"还没生效的修复"。
# 这层只做一件事：把"这个进程现在跑的是不是磁盘上最新的代码"变成一个可读字段，
# 而不是要靠人去猜"我是不是忘记重启了"。与上面 runtime_capability_report() 同一个
# "悄悄劣化 → 清单上写着"的思路，只是这里劣化的是代码本身，不是依赖/契约。
SERVICE_START_TIME = time.time()

_CORE_SOURCE_GLOBS = (
    'server.py', 'server_common.py', 'frame_generator.py', 'frame_continuity.py',
    'pipeline_orchestrator.py', 'video_generator.py',
    os.path.join('prompt_pipeline', '*.py'),
    os.path.join('prompt_pipeline', 'composers', '*.py'),
)


def _git_head_info():
    """服务启动那一刻的 git 提交与工作区脏状态，只在 import 时算一次——它回答的是
    "这个进程当初从哪个代码状态启动"，不是"磁盘现在是什么状态"（那是下面
    code_staleness_report() 按 mtime 比对的事）。非 git 环境/git 不在 PATH 时静默
    返回全 None，不影响服务启动。"""
    import subprocess
    info = {'commit': None, 'commit_short': None, 'dirty': None}
    try:
        commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=_PROJECT_ROOT, capture_output=True,
            text=True, timeout=5, check=False).stdout.strip()
        if commit:
            info['commit'] = commit
            info['commit_short'] = commit[:12]
    except Exception:
        pass
    try:
        status = subprocess.run(
            ['git', 'status', '--porcelain'], cwd=_PROJECT_ROOT, capture_output=True,
            text=True, timeout=5, check=False).stdout
        info['dirty'] = bool(status.strip())
    except Exception:
        pass
    return info


_GIT_HEAD_INFO = _git_head_info()


def _core_source_files():
    import glob
    files = []
    for pattern in _CORE_SOURCE_GLOBS:
        files.extend(glob.glob(os.path.join(_PROJECT_ROOT, pattern)))
    return files


def code_staleness_report():
    """本次调用时刻，磁盘上是否存在比"服务启动时间"更晚修改过的核心源文件。

    命中说明这个运行中的进程仍在跑旧代码——修复已经落盘，但没有生效，必须重启
    才能带上它。每次调用都现读 mtime（不缓存）：这几个文件随时可能被编辑，缓存
    只会让这个信号本身过期。"""
    stale = []
    for path in _core_source_files():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > SERVICE_START_TIME:
            stale.append(os.path.relpath(path, _PROJECT_ROOT).replace(os.sep, '/'))
    return {'stale': bool(stale), 'stale_files': sorted(stale)}


def runtime_version_report(config=None):
    """任务记录 / `/api/mode` 共用的运行时指纹：这个进程是从哪个 git 状态、什么
    时候启动的，以及磁盘上现在是否已经有比它更新的核心代码。"""
    report = {
        'git_commit': _GIT_HEAD_INFO.get('commit'),
        'git_commit_short': _GIT_HEAD_INFO.get('commit_short'),
        'git_dirty': _GIT_HEAD_INFO.get('dirty'),
        'service_start_time': SERVICE_START_TIME,
        'skill_profile': active_skill_profile(config),
    }
    report.update(code_staleness_report())
    return report


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
    调用方应传 fx_request_deadline()——2026-08-01 之前所有生产调用点都漏传了它，
    于是 CancelState.deadline 恒为 None、deadline_exceeded() 恒为 False，
    GOOGLE_FX_REQUEST_BUDGET_SECONDS 这个控制台旋钮从头到尾没接线（详见该函数）。
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


def fx_request_deadline(budget_seconds=None):
    """本次 FX 请求的绝对超时时刻（time.time() 秒），喂给 fx_cancel_context(deadline=...)。

    FX 侧一直备好了整套预算机制——config.GOOGLE_FX_REQUEST_BUDGET_SECONDS、
    get_runtime_request_budget_seconds()、helpers._check_cancelled() 与
    _ChunkRunner._check_cancel() 里的 deadline_exceeded() 检查、以及 fx_console
    里那个「单请求总时间预算」输入框——唯独没人把 deadline 传进 CancelState。
    结果 remaining_seconds() 恒返 None、deadline_exceeded() 恒返 False，用户在控制台
    改这个值不会有任何效果。本函数就是那根缺失的接线。

    走 get_runtime_request_budget_seconds() 而不是模块常量：控制台热调后立刻生效。
    返回 None 表示不限时（读不到配置时的安全降级——宁可不限时，也不要凭空给正在
    跑的批次安一个假预算把它掐掉）。
    """
    if budget_seconds is None:
        try:
            from integrations.google_fx.config import get_runtime_request_budget_seconds
            budget_seconds = get_runtime_request_budget_seconds()
        except Exception as e:
            print(f"Warning: 读取 Google FX 请求预算失败，本次不限时 ({e})")
            return None
    try:
        budget_seconds = float(budget_seconds)
    except (TypeError, ValueError):
        return None
    if budget_seconds <= 0:
        return None
    return time.time() + budget_seconds


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
    video_ref_mode = str(config.get('videoRefMode') or '').strip()
    if video_ref_mode:
        os.environ['GOOGLE_FX_VIDEO_REF_MODE'] = video_ref_mode


def _get_account_pool_service():
    """返回内置 Google FX 账号池服务。"""
    from integrations.google_fx.utils import account_pool
    return account_pool.AccountPool()


def sequence_default_account(config):
    """控制台配的「序列生成默认浏览器环境」（googleFxSequenceUserId）。留空返回 ''。"""
    return str(config.get('googleFxSequenceUserId') or '').strip()


def sequence_account_locked(config):
    """默认环境是否被锁定（锁定 = 整条序列不按节拍换号）。"""
    return bool(config.get('googleFxSequenceUserLock')) and bool(sequence_default_account(config))


def _select_pool_account(config, pool):
    """池子非空且未手动指定账号时，自动选一个还有额度的账号写回 config；
    手动填了 googleFxUserId 单字段 = 这一次的临时覆盖，跳过自动挑选；
    池子为空（还没添加任何账号）= 完全不介入，行为与手动单选时代一致。
    返回被自动选中的 user_id（未触发自动选号则返回 None）。

    优先级：手动 googleFxUserId > 序列生成默认环境（googleFxSequenceUserId，
    仅当它此刻确实可用）> 号池自动选号。默认环境不可用时如实降级到自动选号并
    打一行原因——静默换号会让用户以为序列一直跑在他钉的那个环境上。"""
    manual_override = str(config.get('googleFxUserId') or '').strip()
    if manual_override:
        return None
    accounts = pool.list_accounts()
    if not accounts:
        return None
    min_credit = config.get('videoAccountPoolMinCredit', 1)

    preferred = sequence_default_account(config)
    if preferred:
        match = next((a for a in accounts if str(a.get('user_id')) == preferred), None)
        if match is None:
            print(f"Warning: 序列生成默认环境 {preferred} 不在号池里，改为自动选号")
        elif match.get('disabled'):
            print(f"Warning: 序列生成默认环境 {preferred} 已禁用，改为自动选号")
        elif _account_in_cooldown(match):
            print(f"Warning: 序列生成默认环境 {preferred} 处于冷却期，改为自动选号")
        elif not _account_has_credit(match, min_credit):
            print(f"Warning: 序列生成默认环境 {preferred} 积分不足 {min_credit}，改为自动选号")
        else:
            config['googleFxUserId'] = preferred
            return preferred

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

    可用 = 未禁用 + 不在冷却 + 积分未知或 ≥ min_credit（与 pick_account 同一口径）。

    「锁定默认环境」且本次确实选中了那个环境时返回单元素环：plan_frame_chunk_accounts /
    plan_generation_legs 见到 len(ring)<=1 就不切腿，整条序列留在同一个环境上。
    默认环境没被选中（不可用而降级了）时不锁——那样锁住的是降级后的替补账号，
    不是用户钉的那个。"""
    min_credit = config.get('videoAccountPoolMinCredit', 1)
    if sequence_account_locked(config) and first_user_id == sequence_default_account(config):
        return [first_user_id]
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


# FX 模型设置（视频模型 / 图片模型 / 视频时长 / 视频参考模式）由 FX 服务管理中心
# 统一管理，server_config.json 是唯一权威源。浏览器 localStorage 可能缓着旧模型——
# 让它覆盖服务端就会出现"在控制台改了模型但不生效"的静默失效。
# 配置中心（index.html）改这些项时会同步 POST /api/google-fx/config 写服务端，
# 所以"服务端优先"不会把用户刚在配置中心选的值顶回去。
_SERVER_AUTHORITATIVE_KEYS = frozenset({
    'videoModel', 'googleFxImageModel', 'videoDuration', 'videoRefMode',
})


def effective_config(client_config):
    client_config = dict(client_config or {})
    # 默认环境与换号节拍只有一个权威来源：Google FX 服务管理中心写入的服务端配置。
    # 即使浏览器还缓存着旧前端，也不能再让历史字段覆盖统一配置。
    client_config.pop('googleFxUserId', None)
    client_config.pop('googleFxIpRotateRequests', None)
    from integrations.google_fx.model_catalog import normalize_google_fx_image_model
    if not SERVER_MANAGED:
        merged = dict(client_config)
        for key in ('googleFxIpRotateRequests', 'googleFxSequenceUserId',
                    'googleFxSequenceUserLock'):
            if key in SERVER_CONFIG:
                merged[key] = SERVER_CONFIG[key]
        # FX 模型设置：服务端配置优先，防止浏览器旧缓存覆盖控制台的改动
        for key in _SERVER_AUTHORITATIVE_KEYS:
            if key in SERVER_CONFIG:
                merged[key] = SERVER_CONFIG[key]
        for key in ('frameContinuityMode', 'frameContinuityMaxRetries',
                    'frameContinuityLocalEdit', 'autoSplitHighRiskBeats'):
            if key not in merged and key in SERVER_CONFIG:
                merged[key] = SERVER_CONFIG[key]
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
    # cheapModel），补进白名单防复刻同类静默失效。
    # 已删除的三组键（不要再加回来）：
    #   · geminiDirectApiKey / geminiApiKey / geminiDirectImageModel —— 直连
    #     Google AI Studio 的暗路入口，整条路径已随之删除；
    #   · imageEditFallbackModel —— 配额耗尽自动降级到 gpt-image-2 的开关，
    #     降级机制已取消，配额耗尽一律显式报错；
    #   · realityCheckpointInterval / frameChainGate / frameChainGateRetries /
    #     anchorHardGate / chainDriftRegen —— 生成期一致性审查的开关，整套机制
    #     已于 2026-08-05 移除。
    # imageEditTransport 同批补进来：它此前只写在 server_config.json 里，托管模式
    # （配了 apiKey 就是）下被这份白名单整个丢掉，于是配 'chat' 的机器每个进程
    # 照样先打一枪必挂的 /images/edits——「配置了但从未生效」的静默失效，和
    # qaGateLevel 当年丢的是同一个口子（见 tests/test_qa_gate_levels.py）。
    for k in ('cheapModel', 'auxModel', 'imageEditTransport'):
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
    # skillProfile 同批（2026-08-01）：激发页脚的「提示词链路」选择器就是靠它把
    # base/omni 送到服务端的（active_skill_profile 读的正是 config['skillProfile']）。
    # 不进这份白名单，托管模式下前端选了哪条链路会被整个丢掉——用户以为切了，
    # 实际永远按 videoModel 推断，和 qaGateLevel / imageEditTransport 当年是同一个口子。
    for k in ('imageAspectRatio', 'imageQuality', 'imageBackend', 'googleFxImageModel', 'videoModel', 'videoDuration', 'videoRefMode', 'adsPowerPort', 'videoAccountPoolMinCredit', 'qaGateLevel', 'strictFrameStateContract', 'frameContinuityMode', 'frameContinuityMaxRetries', 'frameContinuityLocalEdit', 'autoSplitHighRiskBeats', 'ideationTrendUrls', 'ideationSearchQuery', 'coverReferencePath', 'skillProfile'):
        if k in _SERVER_AUTHORITATIVE_KEYS:
            # FX 模型设置：服务端配置优先，防止浏览器旧缓存覆盖控制台的改动
            if k in SERVER_CONFIG:
                merged[k] = SERVER_CONFIG[k]
            elif k in client_config:
                merged[k] = client_config[k]
        elif k in client_config:
            merged[k] = client_config[k]
        elif k in SERVER_CONFIG:
            merged[k] = SERVER_CONFIG[k]
    for k in ('googleFxIpRotateRequests', 'googleFxSequenceUserId',
              'googleFxSequenceUserLock'):
        if k in SERVER_CONFIG:
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


# 封面图与帧/视频/成片一样住在项目目录里：outputs/<项目目录>/cover_<毫秒时间戳>.webp。
# 它以前落在全局池 outputs/covers/ 下，文件名靠 <安全标题>_cover_ 前缀反查归属——
# 于是删项目删不掉封面（要靠 delete_idea_output_files 拿着 URL 单独再删一遍）、
# 标题一改就认不回来、画廊里还得单开一个「封面图片」组。现在它跟项目打包在一起。
COVER_FILENAME_PREFIX = 'cover_'
# 迁移前的历史封面仍留在这个全局池里（见 tools/migrate_covers.py），只读不写。
LEGACY_COVERS_DIRNAME = 'covers'


def _is_cover_filename(name):
    """项目目录里的这张图是不是封面（与 project_cover_path 的命名一一对应）。"""
    return (isinstance(name, str)
            and name.startswith(COVER_FILENAME_PREFIX)
            and _gallery_media_type(name) == 'image')


def project_cover_path(project_key, ext='webp'):
    """新封面图的落盘路径，并确保项目目录已存在。

    封面通常是一个项目最先产出的文件（第一帧必须以它图生图），所以这里往往就是
    项目目录被创建出来的那一刻。
    """
    pdir = _get_project_dir(project_key)
    os.makedirs(pdir, exist_ok=True)
    return os.path.join(pdir, f"{COVER_FILENAME_PREFIX}{int(time.time() * 1000)}.{ext}")


def resolve_cover_reference(config, title, project_key=None):
    """Resolve the cover used only as frame 1's image reference.

    A client-selected cover wins; headless callers fall back to this project's newest cover.
    Request paths are restricted to outputs/ so arbitrary local files cannot be uploaded.

    回落查找顺序：项目目录里的 cover_*（新布局）→ 全局封面池里以 <安全标题>_cover_
    开头的那批（迁移前的历史封面）。两处都按 mtime 取最新的一张。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    outputs_dir = os.path.abspath(os.path.join(root, OUTPUT_ROOT))
    named = (config or {}).get('coverReferencePath') if isinstance(config, dict) else None
    if isinstance(named, str) and named.strip():
        raw = named.strip().split('?', 1)[0]
        candidate = raw if os.path.isabs(raw) else os.path.join(root, raw.lstrip('/\\'))
        candidate = os.path.abspath(candidate)
        try:
            # 放宽到整个 outputs/：封面现在住在项目目录里，不再只有 covers/ 一处。
            # 边界仍然是 outputs/——外部本地文件依旧不可能被当成参考图送进模型。
            inside = os.path.commonpath([candidate, outputs_dir]) == outputs_dir
        except ValueError:
            inside = False
        if inside and os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate

    found = []

    project_dir = _get_project_dir(project_key or title)
    project_dir = (project_dir if os.path.isabs(project_dir)
                   else os.path.join(root, project_dir))
    if os.path.isdir(project_dir):
        found += [os.path.join(project_dir, name) for name in os.listdir(project_dir)
                  if _is_cover_filename(name)]

    legacy_dir = os.path.abspath(os.path.join(outputs_dir, LEGACY_COVERS_DIRNAME))
    if os.path.isdir(legacy_dir):
        prefix = f"{_safe_project_name(title)}_cover_"
        found += [os.path.join(legacy_dir, name) for name in os.listdir(legacy_dir)
                  if name.startswith(prefix)]

    found = [path for path in found if os.path.isfile(path) and os.path.getsize(path) > 0]
    return max(found, key=os.path.getmtime) if found else None


def delete_idea_output_files(title, covers=None):
    """Best-effort purge of everything a generated idea left on disk: its whole
    project directory (frames/videos/manifest/cover under outputs/<project>/) plus any
    standalone cover images (outputs/covers/*.webp) referenced by a saved idea.
    Deleting the task/library record alone leaves these behind as orphan files,
    so this must be called from both the task-delete and library-delete endpoints.

    新布局下封面就在项目目录里，第一步的 rmtree 已经把它带走；covers 参数只对
    迁移前留在全局封面池里的历史封面还有用（清不掉的 URL 会被静默跳过）。
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

# outputs/ 下这两个一级目录不是"项目"：covers 是**历史**全局封面池（新封面已改为
# 落进项目目录，见 project_cover_path），image-station 是图像工坊的出图历史。
# 它们没有 manifest，删除后也不做项目级清理/同步。
GALLERY_SPECIAL_DIRS = (LEGACY_COVERS_DIRNAME, 'image-station')


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

    返回值里的 project_owners 是"项目目录名 → 点子库条目（id/标题）"的反查表，
    给画廊每个项目组挂上"回到激发项目"的直达入口。它只认点子库条目——运行中的
    任务还没落库，前端也没有可载入的记录。
    """
    cover_paths = set()
    project_names = set()
    project_owners = {}

    def add_cover(u):
        if not isinstance(u, str):
            return
        rel = u.replace('\\', '/').lstrip('/')
        if rel.startswith(OUTPUT_ROOT + '/'):
            cover_paths.add(rel)

    def add_title(t, sink=None):
        names = _gallery_title_names(t)
        project_names.update(names)
        if sink is not None:
            sink.update(names)

    def add_path_project(p, sink=None):
        if not isinstance(p, str):
            return
        parts = p.replace('\\', '/').lstrip('/').split('/')
        if len(parts) >= 2 and parts[0] == OUTPUT_ROOT and parts[1] not in GALLERY_SPECIAL_DIRS:
            project_names.add(parts[1])
            if sink is not None:
                sink.add(parts[1])

    def eat_record(rec, sink=None):
        if not isinstance(rec, dict):
            return
        for u in (rec.get('covers') or []):
            add_cover(u)
        add_cover(rec.get('collage_url'))
        add_title(rec.get('title'), sink)
        add_title(rec.get('english_title'), sink)
        # 每次合成独占的媒体命名空间（run_<task_id>__<标题>）才是真正的目录名来源，
        # 标题派生的那几个变体只对早期没有 project_key 的记录管用
        add_title(rec.get('project_key'), sink)
        fr = rec.get('frameRun')
        if isinstance(fr, dict):
            add_title(fr.get('title'), sink)
            for coll in ('frames', 'videos'):
                for e in (fr.get(coll) or []):
                    if isinstance(e, dict):
                        add_path_project(e.get('url'), sink)
                        add_path_project(e.get('file'), sink)

    if library_items is None:
        # 必须走 read_library()：创意库现在存在拆分库 library/ 里，直接读
        # library.json 拿到的是迁移前的旧快照（收藏后新增的项目会被判成孤儿）
        library_items = read_library() or []

    # 归属反查分两趟。第一趟只认 project_key 派生出的目录名——它是每次合成独占的
    # 命名空间（run_<task_id>__<标题>），一个目录只可能属于一条创意，是硬证据。
    # 第二趟才用标题变体补齐没被认领的目录：标题会重名、会被截断，拿它当第一优先
    # 级会让后合成的项目被先前那条同名创意抢走归属。
    library_items = [i for i in (library_items or []) if isinstance(i, dict)]
    for pass_no in (1, 2):
        for item in library_items:
            owned = set()
            eat_record(item, owned)
            idea_id = str(item.get('id') or '').strip()
            if not idea_id:
                continue
            owner = {'idea_id': idea_id, 'idea_title': str(item.get('title') or '')}
            if pass_no == 1:
                key = str(item.get('project_key') or '').strip()
                names = {_safe_project_name(key)} if key else set()
            else:
                names = owned
            for name in names:
                # 同名目录归最靠前的那条（点子库按新→旧排列，重复合成时优先认新记录）
                project_owners.setdefault(name, owner)

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
            # 帧/视频/封面子作业现在也带着母项目的 project_key（P3），它是这些
            # 任务产出目录的精确来源，比 theme 那条从宽匹配可靠得多
            add_title(dims.get('project_key'))
            # staged_render 任务的 theme 就是项目标题；compose 任务的 theme 是
            # 场景主题，多收一个引用无害（从宽原则）
            add_title(dims.get('theme'))

    return {
        'cover_paths': cover_paths,
        'project_names': project_names,
        'project_owners': project_owners,
    }


def scan_gallery(base_dir=None, refs=None):
    """扫描 outputs/ 下全部历史媒体资产，按来源分组返回（画廊页数据源）。

    分组：image-station（图像工坊出图）、每个项目目录一组（frames/ 帧序列 +
    videos/ 分段视频 + 项目根的合成视频与封面），以及 covers（历史全局封面池，
    迁移干净后自然消失——新封面已经跟着项目走了）。项目根下的插帧中间产物目录
    （*_frames，成百上千张 jpg）不属于用户资产，不进画廊。

    refs 传 gallery_collect_references() 的返回值时做引用标注：封面 item 加
    in_use（被点子库/任务引用），项目组加 orphan（无任何引用且超过活跃宽限期），
    能反查到点子库归属的项目组再加 idea_id/idea_title（画廊上"回到激发项目"的
    直达入口）。refs=None 时不加任何标注字段（前端按无标注降级展示）。
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

    cover_items = collect_dir(os.path.join(out_dir, LEGACY_COVERS_DIRNAME),
                              'cover', only_type='image')
    if refs is not None:
        for it in cover_items:
            it['in_use'] = it['path'] in refs['cover_paths']
    add_group(LEGACY_COVERS_DIRNAME, '封面图片', 'covers', cover_items)
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
        # 项目根的图片里，cover_* 是这个项目的封面——单独标出 kind='cover'，
        # 画廊的「封面」筛选与「在用」角标才能照常认得它（前端按 item.kind 分类）
        root_images = collect_dir(pdir, 'other', only_type='image')
        for it in root_images:
            if _is_cover_filename(it['name']):
                it['kind'] = 'cover'
                if refs is not None:
                    it['in_use'] = it['path'] in refs['cover_paths']
        items += root_images
        add_group(name, name, 'project', items)
        if refs is not None and items:
            g = groups[-1]
            g['orphan'] = (name not in refs['project_names']
                           and g['latest_mtime'] < now - GALLERY_ORPHAN_GRACE_SECONDS)
            owner = (refs.get('project_owners') or {}).get(name)
            if owner:
                g['idea_id'] = owner['idea_id']
                g['idea_title'] = owner['idea_title']

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
# True 仅当本进程成功跑完一次 load_tasks_from_disk——即 ACTIVE_TASKS 确实代表磁盘上的
# 全部任务。save_tasks_to_disk 的孤儿清理以此为授权前提，见那里的 2026-07-31 注释。
TASKS_LOADED_FROM_DISK = False

# library.json 读写共用一把锁：Windows 上 os.replace 会因并发打开的读句柄抛
# PermissionError，读路径也必须串行化，否则存在整库清零风险
#
# 2026-07-31 改成 RLock：拆分存储把创意库拆成"索引 + 逐条正文"后，写入路径天然是
# 嵌套的（写一条要先 _ensure_library_split 惰性迁移、再读索引、再落盘，每一层都想
# 自己拿锁），普通 Lock 在同线程二次获取就是死锁。RLock 对**跨线程**的互斥性完全
# 一致，只是允许同一线程重入——正是这里需要的。
LIBRARY_LOCK = threading.RLock()

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


# ============================================================================
# 创意库「缩量覆盖」闸门（Library Shrink Guard）
# ----------------------------------------------------------------------------
# /api/library 的契约是"客户端始终持有完整数组、整份 POST 回来覆盖"，所以任何一次
# 状态错乱的写入都是一次静默的数据丢失。此前只有两道防护：
#   1) 空列表覆盖非空库 → 拒绝（2026-07-12 整库清零事故）；
#   2) 同一条创意内部 prompt_slots 与 frameRun 数量不自洽 → 拒绝。
# 中间那一大片"非空 → 非空，但少了一条创意 / 某条少了几帧"完全没人管——2026-07-27
# 自动化测试驱动真实页面把真库整份覆盖，就是从这个缺口掉下去的（靠 .recovery 快照
# 与 tools/recover_ice_cave_all.py 捞回）。
#
# 补法不是"一律拒绝缩量"：删除单条创意（deleteFromLibrary）与删除某一拍
# （/api/delete_slot 之后的 frameRun 回写）都是合法缩量，它们本来就该少。区别在于
# 合法缩量知道自己在删什么，于是这里要求**声明意图**：客户端把 removed_ids /
# frame_shrink_ids 一并 POST 上来，声明与实际差异一致才放行。没声明的缩量一律 409，
# 页面刷新后重来——刷新的代价是几秒钟，静默丢失的代价是那两次事故。
_LIBRARY_INTENT_KEYS = ('removed_ids', 'frame_shrink_ids')


def _library_index(ideas):
    """{id: idea} 映射。无 id 的记录（历史遗留数据里确实存在）与重复 id 的记录都无法
    按身份比对，它们由 library_shrink_verdict 里的条数兜底负责，这里直接跳过/折叠。"""
    index = {}
    for idea in ideas:
        if not isinstance(idea, dict):
            continue
        ident = idea.get('id')
        if ident is None or ident == '':
            continue
        index[str(ident)] = idea
    return index


def _idea_frame_count(idea):
    """一条创意当前挂着多少帧记录（frameRun.frames）。没有 frameRun 记 0——
    "从没生成过帧"与"帧被清空"在这里必须是同一个数，否则 media_renderer 清理幽灵
    frameRun（项目目录已被删）时会被误判成缩量。那条路径照样要声明意图，见下。"""
    if not isinstance(idea, dict):
        return 0
    run = idea.get('frameRun')
    if not isinstance(run, dict):
        return 0
    frames = run.get('frames')
    return len(frames) if isinstance(frames, list) else 0


def library_shrink_verdict(existing, incoming, intent=None, label='创意库',
                           delete_hint='界面上的删除入口（它会带上删除声明）'):
    """这次整表覆盖是不是「未声明的缩量」。返回 (ok, message, detail)。

    existing: 磁盘上现有的表（非 list 时视为无法比对，直接放行——上游已按"非空"
      保守处理过空列表那道防护）
    incoming: 即将写入的表
    intent: {'removed_ids': [...], 'frame_shrink_ids': [...]}，客户端声明"我知道
      这次会少掉这些"。声明多了不算错（用户可能连点两次删除，第二次那条已经不在
      库里了），声明少了才拦——判定只看"实际少了但没声明"这个方向。
    label / delete_hint: 报文用词。同一套判定给两张整表回写的表共用——创意库
      （/api/library）与创意台账（/api/ledger），它们是同一个契约、同一类事故。

    detail 里带上具体差异，前端据此提示用户刷新；调用方回 409。
    """
    if not isinstance(existing, list) or not isinstance(incoming, list):
        return True, None, {}
    intent = intent if isinstance(intent, dict) else {}
    declared_removed = {str(x) for x in (intent.get('removed_ids') or [])}
    declared_shrink = {str(x) for x in (intent.get('frame_shrink_ids') or [])}

    old_index = _library_index(existing)
    new_index = _library_index(incoming)

    removed = [i for i in old_index if i not in new_index and i not in declared_removed]
    frame_shrunk = []
    for ident, old_idea in old_index.items():
        if ident not in new_index or ident in declared_shrink:
            continue
        before = _idea_frame_count(old_idea)
        after = _idea_frame_count(new_index[ident])
        if after < before:
            frame_shrunk.append({
                'id': ident,
                'title': str(old_idea.get('title') or ''),
                'before': before,
                'after': after,
            })
    # 条数兜底：按身份比对会漏掉两类真实缩量——无 id 的历史记录（没有身份可比），
    # 以及重复 id（两条同 id 的记录在索引里折叠成一条，删掉其中一条看不出来）。
    # 所以除了身份差异，总条数也要对得上：少掉的条数超过"已声明且确实消失的 id 数"
    # 就算未声明缩量。
    explained = len([i for i in declared_removed if i in old_index and i not in new_index])
    count_lost = max(0, len(existing) - len(incoming) - explained)
    # removed 里的每一条本来就会体现在条数上，别把同一件事报两遍
    count_lost = max(0, count_lost - len(removed))

    if not removed and not frame_shrunk and not count_lost:
        return True, None, {}

    parts = []
    if removed:
        titles = ', '.join(
            f'「{old_index[i].get("title") or old_index[i].get("one_line") or i}」'
            for i in removed[:5])
        parts.append(f'{len(removed)} 条会消失（{titles}{"…" if len(removed) > 5 else ""}）')
    if frame_shrunk:
        detail_txt = ', '.join(
            f'「{f["title"] or f["id"]}」{f["before"]}→{f["after"]} 帧' for f in frame_shrunk[:5])
        parts.append(f'{len(frame_shrunk)} 条创意的帧记录会减少（{detail_txt}'
                     f'{"…" if len(frame_shrunk) > 5 else ""}）')
    if count_lost:
        parts.append(f'另有 {count_lost} 条记录按条数比对整体消失'
                     f'（无 id 的历史记录，或 id 重复的记录）')

    message = (
        f'已阻止这次{label}覆盖：客户端提交的数据比服务器上的少，且没有声明是哪次删除造成的——'
        + '；'.join(parts)
        + f'。这通常意味着页面状态已过期（另一个标签页/另一台设备改过{label}，'
          f'或本页开着的时间里发生过删除/新增）。'
          f'请刷新页面重新加载后再操作；确实要删除请用{delete_hint}。'
    )
    return False, message, {
        'removed_ids': removed,
        'frame_shrunk': frame_shrunk,
        'count_lost': count_lost,
    }


def _read_library_file(path=None):
    """直接读单文件形态的 library.json（调用方须已持有 LIBRARY_LOCK）。"""
    path = path or DB_FILE
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


def read_library(path=None):
    """读取整个创意库（数组）。缺失返回 []；损坏返回 None（与 read_ledger 同一套约定）。

    调用方**不能**把 None 静默降级成 []：读不出来时回空库，客户端拿到空库后的
    任何一次写入都可能把它当成"库本来就是空的"——那正是 2026-07-12 整库清零事故
    的触发路径。只读用途（如 build_projects_index）可以按"少一列信息"处理，
    写路径必须让请求失败。

    path 显式传入时读那个单文件（测试与迁移用）；不传则走当前生效的存储形态：
    library/ 拆分库已建立就从拆分库重组，否则读老的 library.json。
    """
    if path is not None:
        with LIBRARY_LOCK:
            return _read_library_file(path)
    with LIBRARY_LOCK:
        if _library_store_ready():
            return _read_library_split()
        return _read_library_file(DB_FILE)


# ============================================================================
# 点子库拆分存储（Library Split Store）
# ----------------------------------------------------------------------------
# 老形态是单个 library.json + "客户端始终持有完整数组、整份 POST 回来覆盖"的契约。
# 代价：实测 2 条创意就 208KB（单条 164KB——prompt_block / prompt_slots / audit_md
# / repair_md / frameRun 正文全塞在条目里），改一个字段要上传并重写全库；而"整表
# 覆盖"这个动作本身引发过两次数据事故，逼出了三道防线（空库拒写、缩量闸门 409、
# .bak 轮换），用户日常看到的就是"保存失败，请刷新页面后重试"。
#
# 新形态把一条创意拆成两半：
#   library/index.json        —— 轻量索引数组，列表渲染/搜索/排序只需要它（几 KB）
#   library/items/<id>.json   —— 正文，点开某条才读
# 于是"改一条"就只写一个文件，不再有任何一次写入能碰到别的记录——那三道防线堵的
# 洞在结构上消失了（它们仍保留在整表兼容层上，见 server.py 的 POST /api/library）。
#
# 迁移是惰性的：第一次访问时若只有 library.json，就地拆开并把原文件备份成
# library.json.pre-split，此后拆分库为唯一真相源。
# ============================================================================

LIBRARY_DIR = _path_setting('SPARK_LIBRARY_DIR', 'libraryDir', 'library')

# 索引里保留的字段：列表卡片要显示的 + 合流索引要用的。正文字段
# （prompt_block / prompt_slots / audit_md / repair_md / frameRun / covers）
# 一律不进索引——它们正是让单条膨胀到 164KB 的东西。
LIBRARY_INDEX_FIELDS = (
    'id', 'project_key', 'title', 'theme', 'english_title',
    'social_title_cn', 'social_title_en', 'timestamp', 'creativity',
    'image_count', 'video_count', 'activeCoverUrl', 'collage_url',
    'status', 'tags', 'note', 'updated_at',
)


def _library_paths(library_dir=None):
    root = library_dir or LIBRARY_DIR
    return root, os.path.join(root, 'index.json'), os.path.join(root, 'items')


def _library_item_path(item_id, library_dir=None):
    """条目文件路径。id 直接进文件名，必须先消毒——历史 id 有 '1784...' 这种
    时间戳，也有 importLibrary 生成的 '1784684845170.123' 带小数点的，还可能是
    别处导入的任意字符串。越界字符一律折成下划线，空 id 拒绝。"""
    _, _, items_dir = _library_paths(library_dir)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(item_id or '')).strip('._-')
    if not safe:
        raise ValueError('创意条目缺少可用的 id')
    return os.path.join(items_dir, f'{safe}.json')


def library_index_entry(item):
    """完整条目 → 索引条目。frameRun 的帧数折成一个计数，正文全部丢弃。"""
    entry = {k: item.get(k) for k in LIBRARY_INDEX_FIELDS if k in item}
    entry['id'] = item.get('id')
    covers = item.get('covers')
    entry['cover'] = (item.get('activeCoverUrl')
                      or (covers[0] if isinstance(covers, list) and covers else None))
    entry['cover_count'] = len(covers) if isinstance(covers, list) else 0
    frame_run = item.get('frameRun')
    frames = frame_run.get('frames') if isinstance(frame_run, dict) else None
    entry['frame_count'] = len(frames) if isinstance(frames, list) else 0
    return entry


def _library_store_ready(library_dir=None):
    _, index_path, _ = _library_paths(library_dir)
    return os.path.exists(index_path)


def _read_library_index(library_dir=None):
    """索引数组。缺失返回 []，损坏返回 None（同 read_library 的约定）。"""
    _, index_path, _ = _library_paths(library_dir)
    if not os.path.exists(index_path):
        return []
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        if sys.stdout:
            print(f"[WARN] {index_path} 读取失败: {e}")
        return None
    return data if isinstance(data, list) else None


def _read_library_split(library_dir=None):
    """拆分库 → 完整数组（整表兼容层用）。

    索引在、正文文件丢了的条目**不跳过**：回退成"只有索引字段的残条"。跳过等于
    在整表回写路径上凭空缩量一条，会被缩量闸门当成客户端状态错乱拦下来，用户看到
    的是一句莫名其妙的 409；残条至少让那条创意还在库里、标题还看得见。
    """
    index = _read_library_index(library_dir)
    if index is None:
        return None
    items = []
    for entry in index:
        if not isinstance(entry, dict):
            continue
        try:
            path = _library_item_path(entry.get('id'), library_dir)
        except ValueError:
            items.append(dict(entry))
            continue
        if not os.path.exists(path):
            if sys.stdout:
                print(f"[WARN] 创意正文缺失，按索引残条返回: {entry.get('id')}")
            items.append(dict(entry))
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            if sys.stdout:
                print(f"[WARN] {path} 读取失败，按索引残条返回: {e}")
            items.append(dict(entry))
            continue
        items.append(data if isinstance(data, dict) else dict(entry))
    return items


def _write_library_index(index, library_dir=None):
    root, index_path, _ = _library_paths(library_dir)
    os.makedirs(root, exist_ok=True)
    write_json_atomic(index_path, index)


def read_library_index(library_dir=None):
    """列表渲染用的轻量索引。拆分库还没建立时就地迁移一次。"""
    with LIBRARY_LOCK:
        _ensure_library_split(library_dir)
        return _read_library_index(library_dir)


def read_library_item(item_id, library_dir=None):
    """单条正文。不存在返回 None。"""
    with LIBRARY_LOCK:
        _ensure_library_split(library_dir)
        try:
            path = _library_item_path(item_id, library_dir)
        except ValueError:
            return None
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            if sys.stdout:
                print(f"[WARN] {path} 读取失败: {e}")
            return None
        return data if isinstance(data, dict) else None


def write_library_item(item, library_dir=None):
    """写入/覆盖单条创意，并同步索引。返回写进索引的那条轻量记录。

    这是新契约的核心：一次保存只碰**一个**正文文件加索引，绝不重写别的记录，
    因此不存在"整表覆盖"能引发的整库清零/未声明缩量，也就不需要在这条路径上
    挂那三道防线。已存在的 id 视为更新（原地覆盖并保持它在索引里的位置）。
    """
    if not isinstance(item, dict):
        raise ValueError('创意条目必须是对象')
    with LIBRARY_LOCK:
        _ensure_library_split(library_dir)
        root, _, items_dir = _library_paths(library_dir)
        path = _library_item_path(item.get('id'), library_dir)
        os.makedirs(items_dir, exist_ok=True)

        item = dict(item)
        item['updated_at'] = time.time()
        write_json_atomic(path, item)

        index = _read_library_index(library_dir)
        if index is None:
            raise RuntimeError('创意库索引损坏，已停止写入（请从 library.json.pre-split 恢复）')
        entry = library_index_entry(item)
        ident = str(item.get('id'))
        for i, row in enumerate(index):
            if isinstance(row, dict) and str(row.get('id')) == ident:
                index[i] = entry
                break
        else:
            index.insert(0, entry)      # 与前端 savedIdeas.unshift 同序：新的在最前
        _write_library_index(index, library_dir)
        return entry


def delete_library_item(item_id, library_dir=None):
    """按 id 删除单条（正文文件 + 索引行）。返回是否真的删掉了东西。

    按 id 删天然带着"确有意图"的证据，不吃缩量闸门——与创意台账的
    delete_ledger_entries 同一个道理（见 write_ledger 的说明）。
    """
    with LIBRARY_LOCK:
        _ensure_library_split(library_dir)
        index = _read_library_index(library_dir)
        if index is None:
            raise RuntimeError('创意库索引损坏，已停止删除（请从 library.json.pre-split 恢复）')
        ident = str(item_id)
        remaining = [r for r in index if not (isinstance(r, dict) and str(r.get('id')) == ident)]
        removed = len(remaining) != len(index)
        if removed:
            _write_library_index(remaining, library_dir)
        try:
            path = _library_item_path(item_id, library_dir)
        except ValueError:
            return removed
        if os.path.exists(path):
            try:
                os.remove(path)
                removed = True
            except OSError as e:
                if sys.stdout:
                    print(f"[WARN] 删除创意正文 {path} 失败: {e}")
        return removed


def _ensure_library_split(library_dir=None, db_file=None):
    """惰性迁移：只有 library.json、还没有 library/index.json 时就地拆开。
    调用方须已持有 LIBRARY_LOCK。返回迁移报告，未发生迁移时返回 None。"""
    if _library_store_ready(library_dir):
        return None
    db_file = db_file or DB_FILE
    root, _, items_dir = _library_paths(library_dir)
    legacy = _read_library_file(db_file)
    if legacy is None:
        # 老库损坏：绝不能"当成空库"建一个空的拆分库——那会把损坏固化成清零
        raise RuntimeError(f'{db_file} 读取失败，已停止迁移到拆分库（请人工修复或从 .bak 恢复）')

    os.makedirs(items_dir, exist_ok=True)
    index = []
    migrated = 0
    for item in legacy:
        if not isinstance(item, dict):
            continue
        if item.get('id') is None or item.get('id') == '':
            # 历史遗留的无 id 记录：补一个稳定 id，否则没法落成文件
            item = dict(item)
            item['id'] = f"legacy-{hashlib.md5(json.dumps(item, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:12]}"
        write_json_atomic(_library_item_path(item.get('id'), library_dir), item)
        index.append(library_index_entry(item))
        migrated += 1
    _write_library_index(index, library_dir)

    # 备份原文件而不是删除：出问题时这是唯一一份完整的老数据
    backup = None
    if os.path.exists(db_file):
        backup = db_file + '.pre-split'
        try:
            shutil.copyfile(db_file, backup)
        except Exception as e:
            if sys.stdout:
                print(f"[LIBRARY] 备份 {backup} 失败（迁移继续）: {e}")
            backup = None
    if sys.stdout:
        print(f"[LIBRARY] 已迁移 {migrated} 条创意到拆分库 {root}/（原文件备份：{backup}）")
    return {'migrated': migrated, 'backup': backup, 'dir': root}


def migrate_library_to_split(library_dir=None, db_file=None, force=False):
    """显式迁移入口（tools/migrate_library.py 用）。force=True 时即使拆分库
    已存在也重新从 library.json 拆一遍——只有确知拆分库有问题时才该这么做。"""
    with LIBRARY_LOCK:
        if force:
            _, index_path, _ = _library_paths(library_dir)
            if os.path.exists(index_path):
                os.remove(index_path)
        return _ensure_library_split(library_dir, db_file)


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
        existing_data = None
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    _old = json.load(f)
                existing_data = _old
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
        # 2026-07-30：补上与 /api/ledger 同源的第二道——非空 → 非空的缩量。台账的整表
        # 回写只用于编辑（状态/评分/备注），删除一律走 delete_ledger_entries 的按 id 删，
        # 所以这条路径上的任何缩量都是状态错乱，无需 intent 声明。
        #
        # 这不是假想：合成流程会用 register_ledger_candidates 在服务端**追加**候选，
        # 一个在那之前打开的页面手里是短一截的表，此后任何一次改评分都会把新登记的
        # 候选整批抹掉——和 library.json 那两次事故是同一个机制。
        ok, message, detail = library_shrink_verdict(
            existing_data if isinstance(existing_data, list) else None, entries, {},
            label='创意台账', delete_hint='台账列表里的删除/批量删除（按 id 删，不吃这道防护）')
        if not ok:
            if sys.stdout:
                print(f"[LEDGER GUARD] 拒绝未声明的缩量覆盖: {detail}")
            return False, message
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
            entry = {
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
            }
            # 这条选题激发出来的项目主键。台账行以前只有 DNA 与一句话选题，
            # 要回到它合成出来的项目只能靠标题模糊匹配（app.js openSparkProject
            # 那一串 sparkNormKey 比对）。/api/compose 登记候选时把 project_key
            # 一并带上，此后台账 ↔ 项目就是一条硬链接。
            project_key = str(idea.get('project_key') or '').strip()
            if project_key:
                entry['project_key'] = project_key
            # 新入账创意保留二创所需的原始要素；旧台账没有该字段时，前端仍可用
            # one_line + topic_dna 作为精简母题。只收白名单并限制长度，避免把整份
            # 客户端 dimensions 或异常大 payload 永久塞进台账。
            raw_seed = idea.get('creative_seed')
            if isinstance(raw_seed, dict):
                seed = {}
                for field in ('input_str', 'carrier', 'env', 'trauma', 'destiny', 'twist',
                              'twist_zh', 'salvage', 'salvage_zh'):
                    value = raw_seed.get(field)
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        seed[field] = str(value).strip()[:500]
                if seed:
                    entry['creative_seed'] = seed
            entries.append(entry)
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


# ── 审查盲区台账（operator blind spots）─────────────────────────────────────
# 「机器判过、人判废」的那些帧就是 rubric 的缺口：判定档位已经拉满（qaGateLevel 默认
# standard 全量严检）还是漏，说明漏掉的是**维度**而不是**严格度**——再调严一档也看不见
# 它本来就没在查的东西。
#
# 数据早就在盘上，只是从没被回读过：set_manual_frame_issue 把人的描述写进 manual_issue，
# 同时把被覆盖的机器判定存进 manual_flag_prev_gate（正是为了"事后无从对照谁看漏了什么"
# 这句注释里的目的）。这里把它读出来，喂回逐拍审查的系统提示词。
#
# 只收 manual_flag_prev_gate == 'sequence_reviewed_pass' 的：机器已经报过问题的那些
# 不是盲区，把它们混进来只会让提示词越滚越长而信息量不增。
_BLIND_SPOT_SOURCE_GATE = 'sequence_reviewed_pass'


def collect_operator_blind_spots(limit=12, max_chars=200, output_root=None):
    """扫描所有项目 manifest，收「机器放行、人判废」的样本。

    返回按新近度排序的 [{'text','title','sequence','at'}, ...]，最多 limit 条。
    任何一个项目读不出来都跳过——这是增强信号，不是门禁，永远不该让一次目录异常
    影响审查本身。"""
    root = output_root or OUTPUT_ROOT
    rows = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            manifest = read_manifest(entry.path)
        except Exception:
            continue
        if not manifest:
            continue
        for frame in manifest.get('frames') or []:
            if not isinstance(frame, dict):
                continue
            issue = str(frame.get('manual_issue') or '').strip()
            if not issue or frame.get('manual_flag_prev_gate') != _BLIND_SPOT_SOURCE_GATE:
                continue
            rows.append({
                'text': issue[:max_chars],
                'title': manifest.get('title') or entry.name,
                'sequence': frame.get('sequence'),
                'at': frame.get('reviewed_at') or '',
            })
    # 去重：同一条描述反复出现（同一类毛病被标了很多次）只保留一条，但它出现的次数
    # 本身是权重信号，所以按出现次数降序排在前面。
    tally = {}
    for row in rows:
        key = re.sub(r'\s+', ' ', row['text']).strip().casefold()
        if key not in tally:
            tally[key] = dict(row, count=0)
        tally[key]['count'] += 1
    ranked = sorted(tally.values(), key=lambda r: (-r['count'], r['at']), reverse=False)
    ranked.sort(key=lambda r: -r['count'])
    return ranked[:limit]


def operator_blind_spot_block(limit=12, output_root=None):
    """把盲区样本渲染成一段可直接追加进审查系统提示词的英文文本；无样本时返回 ''。

    **必须追加在系统提示词的末尾**：那份提示词是常量，为的是让整单（乃至跨单）所有
    逐拍调用共用同一份缓存前缀（见 _local_beat_review_system_prompt 的 2026-07-25 说明）。
    追加在尾部不动前缀，缓存照常命中。"""
    spots = collect_operator_blind_spots(limit=limit, output_root=output_root)
    if not spots:
        return ''
    lines = []
    for spot in spots:
        repeat = f" (reported {spot['count']} times)" if spot.get('count', 1) > 1 else ''
        lines.append(f"- {spot['text']}{repeat}")
    return (
        "\n\n[Operator-Reported Blind Spots]\n"
        "The following defects were found by the human operator on frames THIS AUDIT HAD ALREADY "
        "PASSED. They are the rubric's known gaps — check for each of them explicitly, in addition "
        "to the rules above. Report one only when it is concretely visible in these images; the "
        "same confidence bar and second-reviewer check apply.\n"
        + "\n".join(lines)
    )


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
    前端据此显示的"全部通过"从那一刻起就是假的。

    判定依据是审查时记下的 review_frames_sha256（{帧序号: 内容哈希}，见
    pipeline_orchestrator._record_review_fingerprints）。作废＝结论回落
    pending_manual_review、清掉 vlm_qa_reason 与结构化 review_issues。
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
        # 人工标记压着机器判定时，那份判定被暂存在 manual_flag_prev_gate（见
        # pipeline_orchestrator._set_manifest_quality_gate 的 respect_manual_flag）。
        # 帧图变了它同样不再成立——留着的话，用户之后撤销人工标记会回落到一个针对
        # 旧画面的"审查通过"。
        if frame.get('manual_flag_prev_gate') in REAL_REVIEW_VERDICTS:
            frame['manual_flag_prev_gate'] = 'pending_manual_review'
        if frame.get('quality_gate') in REAL_REVIEW_VERDICTS:
            frame['quality_gate'] = 'pending_manual_review'
            frame['vlm_qa_reason'] = None
    return [s for s in changed if isinstance(s, int)]


def manifest_fingerprint(manifest):
    """一份 manifest 的内容指纹，用于判断"这单在某次操作之后有没有被继续改动过"。

    只取真正代表"这单当前有哪些内容"的字段：帧号 + 帧文件 + 质检结论、视频槽位 +
    文件 + 状态、以及有没有成片。刻意不含时间戳/耗时/重试次数这类每次写 manifest
    都会变、却不代表内容变化的字段——否则撤销删除永远会被判成"已被改动过"。

    见 /api/delete_slot 写快照与 /api/restore_slot 的分歧检查。
    """
    m = manifest or {}
    frames = sorted(
        (
            f.get('sequence') or f.get('slot'),
            f.get('file') or f.get('url') or '',
            f.get('quality_gate') or '',
        )
        for f in (m.get('frames') or []) if isinstance(f, dict)
    )
    videos = sorted(
        (
            v.get('slot'),
            v.get('file') or v.get('url') or '',
            v.get('status') or '',
        )
        for v in (m.get('videos') or []) if isinstance(v, dict)
    )
    payload = json.dumps(
        {'frames': frames, 'videos': videos,
         'merged': bool((m.get('merged_video') or {}).get('url'))},
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


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


# ============================================================================
# 任务持久层（Task Store）
# ----------------------------------------------------------------------------
# 老形态：单个 tasks/<id>.json 装下 meta + events + result 全部内容，而
# save_tasks_to_disk() 每次调用都把**内存里所有任务整份重写一遍**，再扫一遍目录
# 删孤儿。实测单个任务文件 523 KB（events 375 KB + result 132 KB），server.py 里
# 有 15 处调用（建任务、每个终态落点、结果回填…），于是同时挂着 3 个任务时，
# 一次状态变更 ≈ 1.5 MB 的同步磁盘写。
#
# 更要命的是"整目录重写 + 孤儿清理"这个动作本身：它引发过两次真实事故（tasks/
# 被整目录清空；一个只建了 1 条内存任务的测试把 5 个真实任务记录删光），逼出了
# 两段防呆补丁。
#
# 新形态把一条任务拆成三份，并且**只写这一条**：
#   tasks/<id>.json          —— meta（id/status/dimensions/error/last_active）
#   tasks/events/<id>.jsonl  —— 事件流，追加写（运行中只 append 新增的那几条）
#   tasks/results/<id>.json  —— 结果全文，只在有结果时写
# 删除是显式的 delete_task_files()，不再靠"谁不在内存里就删谁"的目录扫描。
#
# 迁移是惰性的：load_tasks_from_disk 读到老格式（meta 里内联着 events/result）
# 就照读，并就地重写成新格式。
# ============================================================================

# 与 DB_FILE / LEDGER_FILE / LIBRARY_DIR 同一套可覆盖机制。留这个口子的理由和那几个
# 一样，而且是被真事故教出来的：想起第二个服务实例做验证时，光靠切工作目录隔离不掉
# ——run() 第一行就 os.chdir 到仓库目录，于是那个"隔离"实例直接操作了真实的 tasks/，
# 把 4 个正在跑的任务记录当孤儿删了（见 prune_orphan_task_files 的第三段防呆）。
TASKS_DIR = _path_setting('SPARK_TASKS_DIR', 'tasksDir', 'tasks')
_TASK_TERMINAL_STATUSES = ('completed', 'failed', 'cancelled')

# tid -> 已经落盘的事件条数。运行中每次保存只把新增的那几条 append 到 .jsonl，
# 不重新序列化整条事件流（compose 的 text_chunk 能堆到 375 KB）。
_TASK_FLUSHED_EVENTS = {}
# 文件写入串行化。不用 ACTIVE_TASKS_LOCK 是为了不在持锁期间做磁盘 I/O——
# 那会让所有 worker 线程卡在事件广播上。
_TASK_IO_LOCK = threading.RLock()


def _task_file_id(task_id):
    """task_id → 安全文件名。

    task_id 是客户端传上来的（/api/compose 等接口的请求体里就有），直接拼进路径
    等于把路径穿越开给外部输入。真实 id 的形态是时间戳与 frames_/videos_/cover_/
    seqreview_ + hex，全在白名单内，所以这层消毒对现有文件是恒等的，不会把已有
    任务文件孤立掉。
    """
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(task_id or '')).strip('._-')
    if not safe:
        raise ValueError('任务缺少可用的 id')
    return safe


def _task_paths(task_id, tasks_dir=None):
    root = tasks_dir or TASKS_DIR
    fid = _task_file_id(task_id)
    return (os.path.join(root, f'{fid}.json'),
            os.path.join(root, 'events', f'{fid}.jsonl'),
            os.path.join(root, 'results', f'{fid}.json'))


def _write_task_events(events_path, events, start_index):
    """把 events[start_index:] 追加到 .jsonl；start_index=0 表示整份重写。"""
    os.makedirs(os.path.dirname(events_path), exist_ok=True)
    mode = 'w' if start_index == 0 else 'a'
    with open(events_path, mode, encoding='utf-8') as f:
        for evt in events[start_index:]:
            # 事件是 (type, payload) 元组；JSON 里存成两元数组，读回来再转元组
            f.write(json.dumps(list(evt) if isinstance(evt, (list, tuple)) else [evt, None],
                               ensure_ascii=False, default=str) + '\n')


def save_task_to_disk(task_id, tasks_dir=None):
    """落盘**单个**任务。返回是否真的写了东西。

    这是替代 save_tasks_to_disk() 的日常写入路径：一次只碰一个任务的文件，
    绝不触及其他任务，也绝不删除任何东西。
    """
    with ACTIVE_TASKS_LOCK:
        t = ACTIVE_TASKS.get(task_id)
        if t is None:
            return False
        # 在锁内取快照，锁外做 I/O
        meta = {
            'id': t['id'],
            'status': t['status'],
            'dimensions': t['dimensions'],
            'error': t.get('error'),
            'last_active': t['last_active'],
            'last_client_poll_at': t.get('last_client_poll_at'),
            'last_worker_progress_at': t.get('last_worker_progress_at'),
            'failure_code': t.get('failure_code'),
            'timings': t.get('timings') or {},
            'runtime_version': t.get('runtime_version'),
            'format': 2,
        }
        events = list(t['events'])
        result = t.get('result')

    try:
        meta_path, events_path, result_path = _task_paths(task_id, tasks_dir)
    except ValueError as e:
        if sys.stdout:
            print(f"Error saving task to disk: {e}")
        return False

    with _TASK_IO_LOCK:
        try:
            os.makedirs(os.path.dirname(meta_path) or '.', exist_ok=True)
            # 原子替换：写一半崩溃会留下半截 JSON，重启加载时整个任务丢失
            write_json_atomic(meta_path, meta)

            flushed = _TASK_FLUSHED_EVENTS.get(task_id, 0)
            # 整份重写的两种情形：
            #   · 事件流变短了 —— prepare_task_for_run 重跑时清空，或终态化时
            #     滤掉 text_chunk，此时磁盘上那份已经对不上了；
            #   · 终态 —— 只发生一次，且此时 text_chunk 多半已被滤掉，代价很小，
            #     换来"落盘内容与内存严格一致"。
            full_rewrite = len(events) < flushed or meta['status'] in _TASK_TERMINAL_STATUSES
            if full_rewrite:
                _write_task_events(events_path, events, 0)
            elif len(events) > flushed:
                _write_task_events(events_path, events, flushed)
            _TASK_FLUSHED_EVENTS[task_id] = len(events)

            if result is not None:
                os.makedirs(os.path.dirname(result_path), exist_ok=True)
                write_json_atomic(result_path, result)
        except Exception as e:
            if sys.stdout:
                print(f"Error saving task {task_id} to disk: {e}")
            return False
    return True


def delete_task_files(task_id, tasks_dir=None):
    """显式删除一个任务的三份文件。

    老实现没有这个函数——删除任务是"把它从 ACTIVE_TASKS 里摘掉，然后靠
    save_tasks_to_disk 的孤儿扫描顺手把文件删了"。那个隐式耦合正是两次
    误删事故的机制本身。
    """
    try:
        paths = _task_paths(task_id, tasks_dir)
    except ValueError:
        return False
    removed = False
    with _TASK_IO_LOCK:
        for path in paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                    removed = True
                except OSError as e:
                    if sys.stdout:
                        print(f"Error deleting task file {path}: {e}")
        _TASK_FLUSHED_EVENTS.pop(task_id, None)
    return removed


def save_tasks_to_disk(tasks_dir=None):
    """把内存里所有任务刷一遍盘。

    保留它是为了退出前 flush、迁移、以及少数确实要全量落盘的场合。**日常写入
    请用 save_task_to_disk(tid)**——这个函数是 O(任务数 × 文件大小)。

    与老版本的关键差异：不再做孤儿清理。删文件现在是 delete_task_files() 的
    显式动作，目录扫描式删除请用 prune_orphan_task_files()。
    """
    with ACTIVE_TASKS_LOCK:
        ids = list(ACTIVE_TASKS.keys())
    for tid in ids:
        save_task_to_disk(tid, tasks_dir)


# 孤儿任务文件的"新鲜度"宽限期。刚写下来的任务文件一律不动，理由见
# prune_orphan_task_files 的第三段防呆。与画廊的 GALLERY_ORPHAN_GRACE_SECONDS 同值、
# 同理由（"正在生成中的项目，引用关系可能还没落到 library/tasks 里"）。
TASK_ORPHAN_GRACE_SECONDS = 24 * 3600


def prune_orphan_task_files(tasks_dir=None, grace_seconds=None):
    """删除内存里已经没有对应记录的任务文件。只该在启动加载完成后跑一次。

    这个函数是整个持久层里唯一还会做"目录扫描式删除"的地方，也是历史上出事最多的
    地方，所以三段防呆缺一不可：

      1) 内存任务表为空时一律跳过：空内存 + 磁盘有任务文件意味着本实例没有（或
         还没）加载历史任务（启动竞态/加载失败/幽灵实例），此时"孤儿清理"会把
         全部任务历史当垃圾删光（实际发生过，tasks/ 被整目录清空）。
      2) 本进程没成功跑完一次 load_tasks_from_disk 时同样跳过：内存里那几条不是
         "全部任务"，而是某个调用方刚塞进来的子集（实际发生过：一个直接打
         /api/compose 处理函数的测试建了 1 条内存任务，落盘时把 5 个真实任务删了）。
      3) 2026-07-31 新增——**只删够旧的文件**。前两段防呆都假设"本进程的内存 =
         磁盘应有的全部"，但只要有第二个写者，这个前提就不成立：另一个服务实例
         （或本次启动加载之后、prune 之前刚落盘的新任务）写下的文件，在本进程眼里
         就是凭空冒出来的孤儿。实际发生过：一个指向同一 tasks/ 的第二实例启动，
         把 4 个正在跑的真实任务记录当孤儿删了（内存里还在，一重启就没了）。
         宽限期让"刚写下来的"永远安全，代价只是过期文件晚一天被清掉。

    返回真正删掉的任务条数。
    """
    root = tasks_dir or TASKS_DIR
    grace = TASK_ORPHAN_GRACE_SECONDS if grace_seconds is None else grace_seconds
    with ACTIVE_TASKS_LOCK:
        active_ids = {_task_file_id(tid) for tid in ACTIVE_TASKS}
    if not active_ids or not TASKS_LOADED_FROM_DISK:
        return 0
    now = time.time()
    removed = 0
    with _TASK_IO_LOCK:
        try:
            for filename in os.listdir(root):
                if not filename.endswith('.json'):
                    continue
                if filename[:-5] in active_ids:
                    continue
                meta_path = os.path.join(root, filename)
                try:
                    if now - os.path.getmtime(meta_path) < grace:
                        continue      # 太新，可能是别的写者刚落的盘
                except OSError:
                    continue
                for path in (meta_path,
                             os.path.join(root, 'events', filename[:-5] + '.jsonl'),
                             os.path.join(root, 'results', filename)):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                removed += 1
                if sys.stdout:
                    print(f"[TASKS] 清理孤儿任务文件: {filename[:-5]}")
        except Exception as e:
            if sys.stdout:
                print(f"Error cleaning up task files: {e}")
    return removed


def _read_task_events(events_path):
    """读回 .jsonl 事件流。半截行（写到一半崩溃）跳过而不是让整个任务加载失败——
    事件流是诊断数据，丢最后一行远好过丢掉整条任务记录。"""
    if not os.path.exists(events_path):
        return []
    events = []
    try:
        with open(events_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                events.append(tuple(evt) if isinstance(evt, list) else evt)
    except Exception as e:
        if sys.stdout:
            print(f"[WARN] 任务事件流 {events_path} 读取失败（按空事件流继续）: {e}")
    return events


def load_tasks_from_disk(tasks_dir=None):
    global ACTIVE_TASKS
    root = tasks_dir or TASKS_DIR
    # Backward compatibility: migrate monolithic tasks.json if present
    if os.path.exists("tasks.json") and not os.path.exists(root):
        try:
            with open("tasks.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            os.makedirs(root, exist_ok=True)
            for tid, t in data.items():
                filepath = os.path.join(root, f"{tid}.json")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(t, f, ensure_ascii=False, indent=2)
            os.remove("tasks.json")
        except Exception as e:
            if sys.stdout:
                print(f"Error migrating tasks.json to tasks/ folder: {e}")

    if not os.path.exists(root):
        return

    legacy_ids = []
    try:
        with ACTIVE_TASKS_LOCK:
            for filename in os.listdir(root):
                if filename.endswith(".json"):
                    tid = filename[:-5]
                    filepath = os.path.join(root, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            t = json.load(f)

                        # format 2 = 拆分形态：events 在 .jsonl、result 在 results/。
                        # 老文件把两者内联在 meta 里，这里照读，读完记下来重写成新格式。
                        _, events_path, result_path = _task_paths(tid, root)
                        is_legacy = 'events' in t or 'result' in t
                        if is_legacy:
                            legacy_ids.append(tid)
                            events = t.get("events", [])
                            result = t.get("result")
                        else:
                            events = _read_task_events(events_path)
                            result = None
                            if os.path.exists(result_path):
                                try:
                                    with open(result_path, "r", encoding="utf-8") as f:
                                        result = json.load(f)
                                except Exception as e:
                                    if sys.stdout:
                                        print(f"[WARN] 任务结果 {result_path} 读取失败: {e}")

                        # 磁盘上现有的事件条数（老格式一律记 0：它们的事件还内联在
                        # meta 里，.jsonl 根本不存在，下一次保存必须整份重写）
                        on_disk_events = 0 if is_legacy else len(events)

                        status = t["status"]
                        error = t.get("error")
                        if status == "running":
                            status = "failed"
                            error = "服务已重启，生成中断。"
                            # Append error event if not already present
                            if not any(isinstance(evt, (list, tuple)) and len(evt) > 0 and evt[0] == 'error' for evt in events):
                                events.append(['error', {'message': error}])
                                # 这条是刚补的，磁盘上没有——保持 on_disk_events 不变，
                                # 下次保存会把它 append 上去

                        ACTIVE_TASKS[tid] = {
                            "id": t["id"],
                            "status": status,
                            "events": [tuple(evt) if isinstance(evt, (list, tuple)) else evt for evt in events],
                            "listeners": set(),
                            "cancel_event": threading.Event(),
                            "dimensions": t["dimensions"],
                            "result": result,
                            "error": error,
                            "last_active": t["last_active"],
                            "last_client_poll_at": t.get("last_client_poll_at"),
                            "last_worker_progress_at": t.get("last_worker_progress_at", t["last_active"]),
                            "failure_code": t.get("failure_code"),
                            "timings": t.get("timings") or {"batch_durations": []},
                            # 老记录没有这个字段——留 None 而不是伪造一份"当前进程"的
                            # 指纹，那会让一条真正的旧任务看起来像是刚才这次启动生成的。
                            "runtime_version": t.get("runtime_version"),
                        }
                        _TASK_FLUSHED_EVENTS[tid] = on_disk_events
                    except Exception as e:
                        if sys.stdout:
                            print(f"Error loading task file {filename}: {e}")
        global TASKS_LOADED_FROM_DISK
        TASKS_LOADED_FROM_DISK = True
    except Exception as e:
        if sys.stdout:
            print(f"Error reading tasks directory: {e}")
        return

    # 惰性迁移：把老格式的任务就地重写成拆分形态。放在加载完成之后做，这样即使
    # 中途出错，内存里也已经是一份完整的任务表（TASKS_LOADED_FROM_DISK 已置位）。
    if legacy_ids:
        for tid in legacy_ids:
            _TASK_FLUSHED_EVENTS[tid] = 0      # 强制整份重写事件流
            save_task_to_disk(tid, root)
        if sys.stdout:
            print(f"[TASKS] 已把 {len(legacy_ids)} 个任务记录迁移到拆分形态"
                  f"（meta / events.jsonl / results）")


def ensure_task_project_key(task_id, dimensions):
    """在 dimensions 里就位 project_key，并返回它。

    project_key 是「同一条创意」在 任务 / 点子库 / 台账 / 画廊 四处的硬主键
    （见 build_projects_index）。它以前要等到**结果阶段**才生成，于是运行中的任务
    没有主键可用，四个界面只能靠标题模糊匹配互相反查。这里把它提前到任务创建那一刻。

    标题取 task_label（灵感卡片的选题名）优先、theme 兜底。刻意**不用**结果里那个
    LLM 生成的 title：那要等合成跑完才有，而且自治管线（auto_run_worker）本来就是
    用 task_label 建键的——两条路径此前建出的键不一样，现在统一。
    """
    if not isinstance(dimensions, dict):
        return None
    existing = dimensions.get('project_key')
    if existing:
        return existing
    label = dimensions.get('task_label') or dimensions.get('theme')
    if not label:
        return None
    key = make_idea_project_key(task_id, label)
    dimensions['project_key'] = key
    return key


def get_or_create_task(task_id, dimensions=None):
    ensure_task_project_key(task_id, dimensions)
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
                "last_active": time.time(),
                "last_client_poll_at": None,
                "last_worker_progress_at": time.time(),
                "failure_code": None,
                "timings": {"batch_durations": []},
                # 这个任务由哪个进程/哪份代码创建——排查"结果为什么还是老样子"时，
                # 先看这个是不是已经过期（stale=True），不用去猜有没有忘记重启。
                "runtime_version": runtime_version_report(),
            }
            save_on_create = True
        else:
            if dimensions is not None:
                ACTIVE_TASKS[task_id]["dimensions"] = dimensions
                save_on_create = True
    if save_on_create:
        save_task_to_disk(task_id)
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
    ensure_task_project_key(task_id, dimensions)
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
                "last_active": time.time(),
                "last_client_poll_at": None,
                "last_worker_progress_at": time.time(),
                "failure_code": None,
                "timings": {"batch_durations": []},
                "runtime_version": runtime_version_report(),
            }
            ACTIVE_TASKS[task_id] = t
        else:
            t["status"] = "running"
            t["events"] = []
            t["cancel_event"] = threading.Event()
            t["result"] = None
            t["error"] = None
            t["last_active"] = time.time()
            t["last_client_poll_at"] = None
            t["last_worker_progress_at"] = time.time()
            t["failure_code"] = None
            t["timings"] = {"batch_durations": []}
            # 重跑复用旧记录时也刷新一份——重试很可能就是在"先重启服务"之后点的，
            # 旧记录上的版本指纹不刷新，前端还是会照着上一次失败时的旧指纹判断。
            t["runtime_version"] = runtime_version_report()
            if dimensions is not None:
                t["dimensions"] = dimensions
    # 重跑会把 events 清空，落盘的 .jsonl 必须跟着整份重写而不是继续追加
    _TASK_FLUSHED_EVENTS[task_id] = 0
    save_task_to_disk(task_id)
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
    """删除 7 天前的终态任务。

    老实现是"从内存里摘掉 → 调 save_tasks_to_disk() 靠孤儿扫描顺手删文件"。
    现在显式删这几条自己的文件，不再让一次清理动作有能力扫掉整个目录。
    """
    now = time.time()
    to_delete = []
    with ACTIVE_TASKS_LOCK:
        for tid, t in ACTIVE_TASKS.items():
            if t["status"] in _TASK_TERMINAL_STATUSES and now - t["last_active"] > 604800:
                to_delete.append(tid)
        for tid in to_delete:
            del ACTIVE_TASKS[tid]
    for tid in to_delete:
        delete_task_files(tid)


# ============================================================================
# 项目工作台（Project Workbench）合流索引
# ----------------------------------------------------------------------------
# 任务列表 / 点子库 / 创意台账 / 画廊 描述的其实是同一条创意的四个生命周期切面
# （选题 → 激发任务 → 结果收藏 → 成片资产），但历史上它们之间没有共同主键，只能
# 靠标题模糊匹配互相反查——gallery_collect_references 那句"判定刻意从宽…宁可漏标
# 孤儿"就是被这个逼出来的，前端还另有 findSavedIdeaForSpark/findCompletedTaskForSpark
# 两套猜法。这里把四路数据按 project_key 合成一张项目表，/api/projects 直接回它，
# 前端不必再并发拉三个源自己 join。
#
# project_key 的来源优先级（make_idea_project_key 生成的 run_<task_id>__<title>）：
#   1) 激发任务的 result.project_key / dimensions.project_key —— 唯一权威来源；
#   2) 点子库条目的 project_key；
#   3) 两者都没有（历史数据确实存在，实测 2 条点子库记录里就有 1 条没有）时，
#      按 make_idea_project_key(id, title) 用同一个公式重建。
# 之外再挂两组别名兜底：
#   · task:<task_id> —— 点子库条目 id 与激发任务 id 同源（实测一致）；
#   · title:<归一化标题> —— 帧/视频/封面这些媒体子作业的 dimensions 里只有 theme，
#     既没有 id 也没有 project_key，只能靠标题挂回母项目。
# ============================================================================

# 帧序列/分步渲染/视频/封面：它们不是独立项目，而是某个激发项目下的子作业。
# 与 app.js 的 MEDIA_TASK_TYPES 同口径（那边用它把媒体任务整类挡在任务列表外，
# 结果是失败的媒体任务完全不可见；这里改成挂成子作业，失败照样看得到）。
MEDIA_TASK_TYPES = frozenset({'frames', 'staged_render', 'videos', 'cover'})

_PROJECT_TITLE_PREFIXES = ('做一个', '做个', '设计一个', '设计个')


def _proj_norm(value):
    """标题/主题的归一化匹配键。与 app.js 的 sparkNormKey 同口径。"""
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def _proj_title_variants(*values):
    """一条记录可用来撞标题的全部写法。

    "做一个X" 与 "X" 必须撞得上：合成任务的 dimensions.theme 是用户输入的整句
    （带"做一个"前缀），而它派生出的帧/视频子作业 dimensions.theme 却是去掉前缀的
    成品标题（实测数据如此）。不脱前缀的话子作业永远挂不回母项目。
    """
    out = []
    for value in values:
        key = _proj_norm(value)
        if not key:
            continue
        out.append(key)
        for prefix in _PROJECT_TITLE_PREFIXES:
            if key.startswith(prefix) and len(key) > len(prefix):
                out.append(key[len(prefix):])
    return list(dict.fromkeys(out))


def _proj_epoch(timestamp):
    """点子库的 'YYYY-MM-DD HH:MM:SS' 字符串 → epoch 秒；解析不了返回 0。"""
    text = str(timestamp or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return time.mktime(time.strptime(text, fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


_PROJ_ASSET_STATS_CACHE = {}  # pdir -> (cached_mtimes, cached_stats, cached_time)

def _proj_asset_stats(project_key, title, base_dir):
    """项目目录下的资产统计（文件数 / 字节数 / 最近产出时间 / 封面）。

    目录名不是 project_key 原文——_safe_project_name 会把 '__' 折成 '_'
    （outputs/ 下实际是 run_<id>_<title>）；更老的项目还可能是另外两套历史命名。
    顺着 project_key 撞不上时回落到 _get_project_dir(title)，它本来就负责认全三套。
    收集范围与 scan_gallery 的项目组一致：frames/ + videos/ + 项目根。
    """
    rel_candidates = []
    if project_key:
        rel_candidates.append(os.path.join(OUTPUT_ROOT, _safe_project_name(project_key)))
    if title:
        rel_candidates.append(_get_project_dir(title))

    for rel in rel_candidates:
        pdir = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
        if not os.path.isdir(pdir):
            continue

        # 获取目录以及子目录（frames, videos）的最近修改时间，以判断是否需要重新扫描
        mtimes = {}
        for sub in ('', 'frames', 'videos'):
            dpath = os.path.join(pdir, sub) if sub else pdir
            if os.path.isdir(dpath):
                try:
                    mtimes[sub] = os.path.getmtime(dpath)
                except OSError:
                    mtimes[sub] = 0.0

        now = time.time()
        cached = _PROJ_ASSET_STATS_CACHE.get(pdir)
        if cached:
            cached_mtimes, cached_stats, cached_time = cached
            # 如果目录修改时间完全没变，或者缓存距离现在不超过 3 秒（防短时间频繁重入），直接返回缓存
            if cached_mtimes == mtimes or now - cached_time < 3.0:
                return cached_stats

        file_count = 0
        total_bytes = 0
        latest = 0
        cover_rel = None
        cover_mtime = -1
        for sub in ('frames', 'videos', ''):
            dpath = os.path.join(pdir, sub) if sub else pdir
            if not os.path.isdir(dpath):
                continue
            try:
                names = os.listdir(dpath)
            except OSError:
                continue
            for fname in names:
                fpath = os.path.join(dpath, fname)
                if _gallery_media_type(fname) is None or not os.path.isfile(fpath):
                    continue
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                file_count += 1
                total_bytes += st.st_size
                latest = max(latest, int(st.st_mtime))
                # 封面就在项目根里（cover_*），顺手挑出最新的一张——项目行的缩略图
                # 因此不再只能靠"已收藏"的点子库条目供图，没收藏的项目也有封面看
                if not sub and _is_cover_filename(fname) and st.st_mtime > cover_mtime:
                    cover_mtime = st.st_mtime
                    cover_rel = '/' + os.path.relpath(fpath, base_dir).replace('\\', '/')

        stats = {
            'dir': os.path.relpath(pdir, base_dir).replace('\\', '/'),
            'file_count': file_count,
            'bytes': total_bytes,
            'latest_mtime': latest,
            'cover': cover_rel,
        }
        _PROJ_ASSET_STATS_CACHE[pdir] = (mtimes, stats, now)
        return stats

    return {'dir': None, 'file_count': 0, 'bytes': 0, 'latest_mtime': 0, 'cover': None}


def _proj_blank(project_key, kind='project'):
    return {
        'project_key': project_key,
        'kind': kind,              # project | job（挂不回母项目的孤立媒体作业）
        'title': '',
        'theme': '',
        'cover': None,
        'state': 'unknown',
        'saved': False,
        'has_failed_jobs': False,
        'image_count': None,
        'video_count': None,
        'timestamp': '',
        'updated_at': 0.0,
        'task': None,
        'library': None,
        'ledger': None,
        'sub_jobs': [],
        'assets': None,
    }


def _proj_task_view(task):
    """任务在项目行上的投影：只留列表要用的字段，绝不带 events/result 全文——
    /api/tasks 已经因为整包回传结果被 2.5s 轮询反复下载而做过一次瘦身
    （见 server.py 那段注释），这里从一开始就别把它放进来。"""
    dims = task.get('dimensions') if isinstance(task.get('dimensions'), dict) else {}
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    timeline = task_stage_timeline(task.get('id'))
    return {
        'id': task.get('id'),
        'status': task.get('status'),
        'error': task.get('error'),
        'last_active': task.get('last_active') or 0,
        'stage': (timeline[-1].get('stage') if timeline else None),
        'model': result.get('model'),
        # 重试/再跑一遍/跟进都要把原样的 dimensions 交回去（retryTask /
        # rerunCompletedTask / viewTask 的既有签名），所以这一份必须原样带上。
        # 它是纯参数（实测 ~800 字节），不是 events/result 那种大块正文。
        'dimensions': dims,
        'beats_count': dims.get('beats_count'),
        'beat_count_mode': dims.get('beat_count_mode'),
        'duration_seconds': ((result.get('timings') or {}).get('total_duration_seconds')
                             if isinstance(result.get('timings'), dict) else None),
        'token_usage': result.get('token_usage'),
    }


def _proj_state(entry):
    """项目行的主状态（工作台顶部 chips 的分桶依据）。

    运行中 > 失败/取消 > 已收藏 > 已完成。收藏排在失败之后是因为：compose 失败时
    根本没有结果可收藏，两者几乎不会同时成立；真同时成立时"失败"更需要被看见。
    媒体子作业的失败不改主状态（母项目本身是好的），只置 has_failed_jobs 让 UI 打旗。

    孤立媒体作业行（kind='job'，母项目的任务记录已被 7 天清理规则删掉）没有 task，
    主状态取它自己那批作业里最重的一档——否则它们会全部落进 'unknown'，
    连"失败"筛选都捞不出来，等于白挂了一行。
    """
    task = entry.get('task') or {}
    status = task.get('status')
    if status == 'running' or any(j.get('status') == 'running' for j in entry['sub_jobs']):
        return 'running'
    if status in ('failed', 'cancelled'):
        return status
    if entry['saved']:
        return 'saved'
    if status == 'completed':
        return 'completed'
    if not task and entry['sub_jobs']:
        job_states = {j.get('status') for j in entry['sub_jobs']}
        for candidate in ('failed', 'cancelled', 'completed'):
            if candidate in job_states:
                return candidate
    return status or 'unknown'


def build_projects_index(tasks=None, library_items=None, ledger_rows=None,
                         base_dir=None, with_assets=True):
    """把 任务 / 点子库 / 台账 / outputs 四路数据合流成一张项目表。

    参数传 None 时从真实数据源读取（ACTIVE_TASKS / library.json / topic_ledger.json）；
    测试传显式列表。任一路缺失或损坏都不能让整张表失败——工作台是用户找回自己项目的
    唯一入口，宁可少一列信息，不能整页空白。

    返回 list[dict]，按 updated_at 新 → 旧排序。
    """
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))

    if tasks is None:
        with ACTIVE_TASKS_LOCK:
            tasks = [
                {k: v for k, v in t.items() if k not in ('listeners', 'cancel_event')}
                for t in ACTIVE_TASKS.values()
            ]
    if library_items is None:
        library_items = read_library()
        if library_items is None:      # 读失败：宁可少一列，不整页失败
            library_items = []
    if ledger_rows is None:
        ledger_rows = read_ledger() or []

    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    library_items = [i for i in (library_items or []) if isinstance(i, dict)]
    ledger_rows = [r for r in (ledger_rows or []) if isinstance(r, dict)]

    projects = {}
    alias = {}

    def bind(project_key, *keys):
        for key in keys:
            if key:
                alias.setdefault(key, project_key)

    def lookup(*keys):
        for key in keys:
            hit = alias.get(key)
            if hit:
                return hit
        return None

    def dims_of(task):
        return task.get('dimensions') if isinstance(task.get('dimensions'), dict) else {}

    def result_of(task):
        return task.get('result') if isinstance(task.get('result'), dict) else {}

    # ── 1. 激发任务：项目表的脊柱，project_key 由它产生 ──────────────────
    for task in tasks:
        dims = dims_of(task)
        if dims.get('type') in MEDIA_TASK_TYPES:
            continue
        result = result_of(task)
        title = result.get('title') or dims.get('task_label') or dims.get('theme') or ''
        key = (result.get('project_key') or dims.get('project_key')
               or make_idea_project_key(task.get('id'), title))
        entry = projects.setdefault(key, _proj_blank(key))
        entry['title'] = entry['title'] or title
        entry['theme'] = entry['theme'] or dims.get('theme') or ''
        entry['task'] = _proj_task_view(task)
        if result.get('image_count') is not None:
            entry['image_count'] = result.get('image_count')
        if result.get('video_count') is not None:
            entry['video_count'] = result.get('video_count')
        entry['updated_at'] = max(entry['updated_at'], float(task.get('last_active') or 0))
        bind(key, f"task:{task.get('id')}",
             *[f"title:{v}" for v in _proj_title_variants(
                 title, dims.get('theme'), dims.get('task_label'))])

    # ── 2. 点子库：收藏态 ────────────────────────────────────────────────
    for item in library_items:
        title = item.get('title') or ''
        key = (item.get('project_key')
               or lookup(f"task:{item.get('id')}",
                         *[f"title:{v}" for v in _proj_title_variants(title, item.get('theme'))])
               or make_idea_project_key(item.get('id'), title))
        entry = projects.setdefault(key, _proj_blank(key))
        entry['title'] = entry['title'] or title
        entry['theme'] = entry['theme'] or item.get('theme') or ''
        entry['saved'] = True
        entry['timestamp'] = item.get('timestamp') or entry['timestamp']
        covers = item.get('covers') if isinstance(item.get('covers'), list) else []
        entry['cover'] = item.get('activeCoverUrl') or (covers[0] if covers else None) or entry['cover']
        if item.get('image_count') is not None:
            entry['image_count'] = item.get('image_count')
        if item.get('video_count') is not None:
            entry['video_count'] = item.get('video_count')
        frame_run = item.get('frameRun') if isinstance(item.get('frameRun'), dict) else {}
        entry['library'] = {
            'id': item.get('id'),
            'timestamp': item.get('timestamp'),
            'english_title': item.get('english_title'),
            'social_title_cn': item.get('social_title_cn'),
            'frame_count': len(frame_run.get('frames')) if isinstance(frame_run.get('frames'), list) else 0,
        }
        entry['updated_at'] = max(entry['updated_at'], _proj_epoch(item.get('timestamp')))
        bind(key, f"task:{item.get('id')}",
             *[f"title:{v}" for v in _proj_title_variants(title, item.get('theme'))])

    # ── 3. 媒体子作业：挂回母项目；挂不上的自成一行（否则失败的帧/视频任务
    #      像现在的任务抽屉那样被整类过滤掉，用户永远看不见）──────────────
    for task in tasks:
        dims = dims_of(task)
        job_type = dims.get('type')
        if job_type not in MEDIA_TASK_TYPES:
            continue
        job = {
            'id': task.get('id'),
            'type': job_type,
            'status': task.get('status'),
            'error': task.get('error'),
            'last_active': task.get('last_active') or 0,
            'theme': dims.get('theme') or '',
        }
        variants = _proj_title_variants(dims.get('theme'))
        # 2026-07-31（P3）起子作业创建时就带着母项目的 project_key，这是精确挂接；
        # 标题撞名只是老任务的回落（那时候子作业 dimensions 里只有一个 theme）
        key = dims.get('project_key')
        if not key or key not in projects:
            key = lookup(*[f"title:{v}" for v in variants])
        if key and key in projects:
            entry = projects[key]
        else:
            # 孤立作业按**标题**分组而不是按任务 id：同一个母项目跑过 3 次帧序列
            # 加 1 次封面时，按 id 建行会得到 4 行长得一模一样的记录。variants[-1]
            # 是脱掉「做一个」前缀的写法，帧任务（无前缀）与封面任务（有前缀）
            # 因此会落到同一行。
            group = variants[-1] if variants else str(task.get('id'))
            key = f"job:{group}"
            entry = projects.setdefault(key, _proj_blank(key, kind='job'))
            entry['title'] = entry['title'] or dims.get('theme') or job_type
            entry['theme'] = entry['theme'] or dims.get('theme') or ''
        entry['sub_jobs'].append(job)
        entry['updated_at'] = max(entry['updated_at'], float(task.get('last_active') or 0))

    # ── 4. 创意台账：选题与投放表现。台账里没被激发过的候选不进工作台
    #      （那是台账页自己的领域，工作台只管"已经跑过的项目"）───────────
    for row in ledger_rows:
        seed = row.get('creative_seed') if isinstance(row.get('creative_seed'), dict) else {}
        key = (row.get('project_key')
               or lookup(*[f"title:{v}" for v in _proj_title_variants(
                   seed.get('input_str'), row.get('one_line'))]))
        if not key or key not in projects:
            continue
        projects[key]['ledger'] = {
            'id': row.get('id'),
            'status': row.get('status'),
            'topic_dna': row.get('topic_dna'),
            'llm_score': row.get('llm_score'),
            'user_score': row.get('user_score'),
            'date': row.get('date'),
        }

    # ── 5. 资产统计 + 主状态 + 排序 ──────────────────────────────────────
    rows = []
    for key, entry in projects.items():
        if with_assets:
            # 孤立作业行的 key 是 job:<标题>，不是合法 project_key——只能靠标题
            # 回落到 _get_project_dir 的三套历史命名去找目录
            entry['assets'] = _proj_asset_stats(
                key if entry['kind'] == 'project' else None, entry['title'], base_dir)
            entry['updated_at'] = max(entry['updated_at'], float(entry['assets']['latest_mtime']))
            # 点子库记的封面优先（用户可能在多张里选过一张 activeCoverUrl），
            # 没收藏过的项目才用磁盘上那张兜底
            entry['cover'] = entry['cover'] or entry['assets'].get('cover')
        entry['has_failed_jobs'] = any(j.get('status') == 'failed' for j in entry['sub_jobs'])
        entry['sub_jobs'].sort(key=lambda j: j.get('last_active') or 0, reverse=True)
        entry['state'] = _proj_state(entry)
        rows.append(entry)

    rows.sort(key=lambda r: r.get('updated_at') or 0, reverse=True)
    return rows


def filter_projects(rows, state=None, query=None, sort='newest'):
    """工作台的服务端筛选/排序。点子库现在是全量渲染无分页，项目一多必然卡；
    分页交给调用方对返回值切片。"""
    out = list(rows or [])
    if state and state != 'all':
        if state == 'saved':
            out = [r for r in out if r.get('saved')]
        elif state == 'failed':
            # "失败"这一档要连带媒体子作业的失败一起捞出来——那正是现在完全看不见的那批
            out = [r for r in out if r.get('state') in ('failed', 'cancelled') or r.get('has_failed_jobs')]
        else:
            out = [r for r in out if r.get('state') == state]
    key = _proj_norm(query)
    if key:
        def hit(row):
            haystack = ' '.join(str(x or '') for x in (
                row.get('title'), row.get('theme'), row.get('project_key'),
                (row.get('task') or {}).get('id'), (row.get('ledger') or {}).get('topic_dna')))
            return key in _proj_norm(haystack)
        out = [r for r in out if hit(r)]
    if sort == 'oldest':
        out.sort(key=lambda r: r.get('updated_at') or 0)
    elif sort == 'title':
        out.sort(key=lambda r: _proj_norm(r.get('title')))
    else:
        out.sort(key=lambda r: r.get('updated_at') or 0, reverse=True)
    return out


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
