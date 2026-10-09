"""提示词包生成任务：排队、后台执行、取消、续跑、落盘与入库。

每个任务一个目录 beat_packs/<id>/：request.json（不含任何密钥）、state.json（状态与事件）、bible.json、
rows.json（已通过校验的段，逐批落盘）、<项目名_日期_v1>/（beat_package.json、说明文档、delivery-v1/）。
密钥只在内存里（入队时从服务端配置取一份），绝不写盘；服务重启后续跑由接口重新带上当时的配置。
"""
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import compose, generator, library_import, prompts, schema

BASE_DIR = Path(os.environ.get('BEAT_PACK_DIR') or Path(__file__).resolve().parent.parent / 'beat_packs')
DEFAULT_MODEL = 'claude-opus-5-5'
CLAUDE_MODELS = ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1', 'claude-haiku-5-5')
SEGMENTS = {'min': compose.MIN_SEGMENTS, 'max': compose.MAX_SEGMENTS, 'default': 28}
MAX_THEME, MAX_FRAMEWORK, MAX_CONSTRAINTS, MAX_TITLE = 6000, 40000, 3000, 60
MAX_EVENTS = 300
ACTIVE = ('queued', 'running')
RESUMABLE = ('failed', 'cancelled', 'interrupted')
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$')
JOB_ID_RE = re.compile(r'^bp_\d{8}_\d{6}_[0-9a-f]{6}$')
PROMPT_FILES = ('完整提示词.txt', '图片提示词.txt', '视频提示词.txt')

_LOCK = threading.RLock()
_JOBS = {}                      # id -> _Live（只含本进程里排过队/跑过的任务）
_QUEUE = queue.Queue()
_WORKERS = []
_RECONCILED = False
_MAX_WORKERS = 1


class BeatPackError(Exception):
    def __init__(self, message, code='invalid_request', status=400):
        super().__init__(message)
        self.code, self.status = code, status


class _Live:
    """进程内的运行时句柄：取消信号与当次配置（含密钥，只留内存）。"""

    def __init__(self, job_id, config):
        self.id = job_id
        self.config = config
        self.cancel = threading.Event()
        self.last_flush = 0.0


# ── 路径与落盘 ──────────────────────────────────────────────────

def job_dir(job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise BeatPackError('任务编号无效', 'bad_id', 400)
    return BASE_DIR / job_id


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _load_state(job_id):
    state = _read_json(job_dir(job_id) / 'state.json')
    if not isinstance(state, dict):
        raise BeatPackError('找不到这个任务', 'not_found', 404)
    return state


def _save_state(state):
    state['updated_at'] = time.time()
    _write_json(job_dir(state['id']) / 'state.json', state)


def _event(state, level, message):
    state.setdefault('events', []).append({'t': time.time(), 'level': level, 'message': str(message)})
    del state['events'][:-MAX_EVENTS]


# ── 请求校验 ────────────────────────────────────────────────────

def _text(value, name, maximum, required=False, minimum=0):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise BeatPackError(f'{name} 必须是文本')
    value = value.strip()
    if required and not value:
        raise BeatPackError(f'请填写{name}')
    if len(value) < minimum:
        raise BeatPackError(f'{name}至少 {minimum} 个字')
    if len(value) > maximum:
        raise BeatPackError(f'{name}不能超过 {maximum} 个字')
    return value


def _flag(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise BeatPackError('开关字段必须是 true/false')


def default_model(config):
    configured = str((config or {}).get('model') or '').strip()
    from server_common import is_claude_model
    return configured if is_claude_model(configured) else DEFAULT_MODEL


def normalize_request(body, config):
    if not isinstance(body, dict):
        raise BeatPackError('请求格式不正确')
    segments = body.get('segments', SEGMENTS['default'])
    if isinstance(segments, str) and segments.strip().isdigit():
        segments = int(segments.strip())
    if isinstance(segments, bool) or not isinstance(segments, int) or not SEGMENTS['min'] <= segments <= SEGMENTS['max']:
        raise BeatPackError(f"段数需要是 {SEGMENTS['min']}–{SEGMENTS['max']} 的整数")
    model = _text(body.get('model'), '模型', 80) or default_model(config)
    if not MODEL_RE.match(model):
        raise BeatPackError('模型名含有不允许的字符')
    title = re.sub(r'[\\/:*?"<>|\s]+', '', _text(body.get('title'), '项目标题', MAX_TITLE))
    return {
        'theme': _text(body.get('theme'), '创意主题', MAX_THEME, required=True, minimum=4),
        'framework': _text(body.get('framework'), '参考框架', MAX_FRAMEWORK),
        'constraints': _text(body.get('constraints'), '成品要求', MAX_CONSTRAINTS) or prompts.DEFAULT_CONSTRAINTS,
        'segments': segments, 'model': model,
        'review': _flag(body.get('review'), True), 'auto_import': _flag(body.get('auto_import'), True),
        'title': title,
    }


def _fingerprint(request, request_id):
    raw = json.dumps([request, str(request_id or '')], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


# ── 对外视图 ────────────────────────────────────────────────────

def public_job(state, detail=False):
    request = state.get('request') or {}
    result = state.get('result') or {}
    out = {
        'id': state['id'], 'status': state.get('status'), 'stage': state.get('stage'), 'message': state.get('message', ''),
        'progress': state.get('progress', 0), 'created_at': state.get('created_at'), 'updated_at': state.get('updated_at'),
        'finished_at': state.get('finished_at'), 'model': request.get('model'), 'segments': request.get('segments'),
        'theme': (request.get('theme') or '')[:160], 'title': state.get('title') or request.get('title') or '',
        'review': request.get('review'), 'auto_import': request.get('auto_import'),
        'stats': state.get('stats') or {}, 'error': state.get('error'),
        'resumable': state.get('status') in RESUMABLE and bool(state.get('has_bible')),
        'result': {k: result[k] for k in ('dir', 'images', 'videos', 'warnings', 'files', 'import', 'validation') if k in result} or None,
    }
    events = state.get('events') or []
    out['events'] = events if detail else events[-8:]
    return out


def list_jobs(limit=50):
    _reconcile()
    states = []
    if BASE_DIR.is_dir():
        for path in BASE_DIR.iterdir():
            state = _read_json(path / 'state.json') if JOB_ID_RE.match(path.name) else None
            if isinstance(state, dict) and state.get('id') == path.name:
                states.append(state)
    states.sort(key=lambda s: s.get('created_at') or 0, reverse=True)
    return [public_job(s) for s in states[:limit]]


def get_job(job_id):
    _reconcile()
    return public_job(_load_state(job_id), detail=True)


def running_job_ids():
    """仍在排队或运行的任务 id：服务重启/自动重载前必须检查，否则会掐断正在花钱的生成。"""
    with _LOCK:
        return sorted(job_id for job_id, live in _JOBS.items() if _status_of(job_id) in ACTIVE)


def _status_of(job_id):
    state = _read_json(job_dir(job_id) / 'state.json') or {}
    return state.get('status')


def _reconcile():
    """进程第一次访问时，把磁盘上标着排队/运行、但本进程并没有在跑的任务标成 interrupted。"""
    global _RECONCILED
    with _LOCK:
        if _RECONCILED:
            return
        _RECONCILED = True
        if not BASE_DIR.is_dir():
            return
        for path in BASE_DIR.iterdir():
            state = _read_json(path / 'state.json') if JOB_ID_RE.match(path.name) else None
            if isinstance(state, dict) and state.get('status') in ACTIVE and path.name not in _JOBS:
                state['status'] = 'interrupted'
                state['message'] = '服务重启时被中断；已完成的部分已保存，可以点“继续生成”。'
                _event(state, 'warn', state['message'])
                _save_state(state)


# ── 能力探测 ────────────────────────────────────────────────────

def capabilities(config):
    from server_common import SERVER_CONFIG, resolve_gateway
    config = config or {}
    model = default_model(config)
    base_url, api_key = resolve_gateway(model, config)
    dedicated = bool(str(config.get('claudeBaseUrl') or SERVER_CONFIG.get('claudeBaseUrl') or '').strip())
    return {
        'available': True,
        'default_model': model, 'models': list(CLAUDE_MODELS), 'segments': dict(SEGMENTS),
        'defaults': {'constraints': prompts.DEFAULT_CONSTRAINTS, 'review': True, 'auto_import': True},
        'gateway': {'route': 'claudeBaseUrl' if dedicated else 'baseUrl', 'has_key': bool(api_key)},
        'message': ('' if api_key else '未检测到网关密钥（claudeApiKey 或 apiKey）；若网关不需要密钥可忽略，否则请先在 server_config.json 里配置。'),
    }


# ── 入队、取消、续跑 ──────────────────────────────────────────────

def _new_id():
    return f"bp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def _enqueue(job_id, config):
    with _LOCK:
        _JOBS[job_id] = _Live(job_id, config)
        _ensure_workers()
    _QUEUE.put(job_id)


def _ensure_workers():
    _WORKERS[:] = [t for t in _WORKERS if t.is_alive()]
    while len(_WORKERS) < _MAX_WORKERS:
        thread = threading.Thread(target=_worker_loop, name=f'beat-pack-{len(_WORKERS)}', daemon=True)
        _WORKERS.append(thread)
        thread.start()


def set_concurrency(value):
    """并发任务数（1–3）。模型调用很长且按量计费，默认串行。"""
    global _MAX_WORKERS
    try:
        _MAX_WORKERS = max(1, min(3, int(value)))
    except (TypeError, ValueError):
        _MAX_WORKERS = 1


def start(body, config, request_id=''):
    _reconcile()
    request = normalize_request(body, config)
    fingerprint = _fingerprint(request, request_id)
    with _LOCK:
        for job_id in list(_JOBS):
            state = _read_json(job_dir(job_id) / 'state.json') or {}
            if state.get('status') in ACTIVE and state.get('fingerprint') == fingerprint:
                return public_job(state, detail=True)
        job_id = _new_id()
        now = time.time()
        state = {'id': job_id, 'status': 'queued', 'stage': 'queued', 'message': '已排队，等待开始。', 'progress': 0.0,
                 'created_at': now, 'updated_at': now, 'finished_at': None, 'fingerprint': fingerprint, 'request': request,
                 'events': [], 'stats': {}, 'error': None, 'result': None, 'has_bible': False}
        _event(state, 'info', f"已创建任务：{request['segments']} 段，模型 {request['model']}。")
        _write_json(job_dir(job_id) / 'request.json', request)
        _save_state(state)
        _enqueue(job_id, config)
    return public_job(state, detail=True)


def cancel(job_id):
    state = _load_state(job_id)
    with _LOCK:
        live = _JOBS.get(job_id)
        if state.get('status') == 'queued':
            state.update(status='cancelled', stage='cancelled', message='已取消（尚未开始）。', finished_at=time.time())
            _event(state, 'info', state['message'])
            _save_state(state)
        elif state.get('status') == 'running' and live:
            # 运行中的任务只有它自己的 worker 写 state.json；这里只发信号，取消结果由 worker 落盘。
            live.cancel.set()
            state['message'] = '正在取消…当前这次模型调用会在下一个数据块到达时中止。'
    return public_job(state, detail=True)


def resume(job_id, config):
    state = _load_state(job_id)
    if state.get('status') not in RESUMABLE:
        raise BeatPackError('只有失败、已取消或被中断的任务可以继续', 'not_resumable', 409)
    with _LOCK:
        state.update(status='queued', stage='queued', message='已重新排队，从已保存的进度继续。', error=None, finished_at=None)
        _event(state, 'info', state['message'])
        _save_state(state)
        _enqueue(job_id, config)
    return public_job(state, detail=True)


# ── 读结果 / 入库 ────────────────────────────────────────────────

def _delivery_dir(state):
    rel = (state.get('result') or {}).get('dir')
    if not rel:
        raise BeatPackError('这个任务还没有生成交付文件', 'no_result', 409)
    path = job_dir(state['id']) / rel / 'delivery-v1'
    if not path.is_dir():
        raise BeatPackError('交付文件已不存在', 'no_result', 409)
    return path


def prompt_text(job_id, name='完整提示词.txt'):
    if name not in PROMPT_FILES:
        raise BeatPackError('不支持的文件名', 'bad_file', 400)
    state = _load_state(job_id)
    return (_delivery_dir(state) / name).read_text(encoding='utf-8')


def import_job(job_id, title=None):
    """把已完成任务的完整提示词写进创意库；重名时由调用方换标题重试。"""
    state = _load_state(job_id)
    if state.get('status') != 'completed':
        raise BeatPackError('任务完成后才能导入', 'not_completed', 409)
    text = prompt_text(job_id)
    title = re.sub(r'[\\/:*?"<>|\s]+', '', _text(title, '项目标题', MAX_TITLE)) or state.get('title') or ''
    try:
        outcome = library_import.import_prompt_set(text, title, source_label=f'beat_pack:{job_id}')
    except library_import.ImportRefused as exc:
        raise BeatPackError(str(exc), exc.code, 409) from exc
    with _LOCK:
        state = _load_state(job_id)
        state.setdefault('result', {})['import'] = outcome
        state['title'] = title
        _event(state, 'info', f"已导入项目工作台：{title}")
        _save_state(state)
    return public_job(state, detail=True)


# ── 后台执行 ────────────────────────────────────────────────────

def _worker_loop():
    while True:
        job_id = _QUEUE.get()
        try:
            _run(job_id)
        except Exception as exc:                                   # 兜底：任何意外都要落成 failed，不能让任务卡在 running
            try:
                with _LOCK:
                    state = _load_state(job_id)
                    state.update(status='failed', message=f'内部错误：{exc}', finished_at=time.time(),
                                 error={'stage': state.get('stage'), 'message': f'内部错误：{exc}', 'issues': []})
                    _event(state, 'error', state['message'])
                    _save_state(state)
            except Exception:
                pass
        finally:
            with _LOCK:
                _JOBS.pop(job_id, None)
            _QUEUE.task_done()


def _make_chat(live, model, on_receive):
    import prompt_pipeline as pp
    from server_common import GenerationCancelled

    def chat(system, user, *, max_tokens, label):
        received = [0]

        def on_chunk(piece):
            if live.cancel.is_set():
                raise GenerationCancelled('已取消')
            received[0] += len(piece)
            on_receive(label, received[0])

        try:
            return pp._chat(live.config, system, user, temperature=0.7, max_tokens=max_tokens, timeout=900,
                            on_chunk=on_chunk, model=model)
        except GenerationCancelled as exc:
            raise generator.Cancelled() from exc
    return chat


def _run(job_id):
    with _LOCK:
        live = _JOBS.get(job_id)
        state = _load_state(job_id)
        if state.get('status') != 'queued' or live is None:
            return                                                  # 排队期间被取消
        state.update(status='running', stage='design', message='开始生成…', started_at=time.time())
        _save_state(state)
    directory = job_dir(job_id)
    request = state['request']
    bible = _read_json(directory / 'bible.json')
    rows = (_read_json(directory / 'rows.json') or {}).get('rows') or []

    def flush(force=False):
        if force or time.time() - live.last_flush > 2.0:
            live.last_flush = time.time()
            with _LOCK:
                _save_state(state)

    def on_event(level, message):
        with _LOCK:
            _event(state, level, message)
            flush(True)

    def on_progress(stage, message, fraction):
        with _LOCK:
            state.update(stage=stage, message=message, progress=round(fraction, 3))
            flush(True)

    def on_receive(label, chars):
        with _LOCK:
            state['message'] = f'{label}：模型正在输出（已收到 {chars:,} 字符）'
            flush()

    def on_bible(value):
        _write_json(directory / 'bible.json', value)
        with _LOCK:
            state['has_bible'] = True
            state['title'] = request.get('title') or library_import.default_title(value)

    def on_rows(value):
        _write_json(directory / 'rows.json', {'rows': value})

    gen = generator.Generator(
        _make_chat(live, request['model'], on_receive), request, bible=bible, rows=rows, on_event=on_event,
        on_bible=on_bible, on_rows=on_rows, on_progress=on_progress, cancelled=live.cancel.is_set)
    if bible:
        state['has_bible'] = True
        state['title'] = request.get('title') or library_import.default_title(bible)
    try:
        outcome = gen.run()
        state['stats'] = dict(gen.stats)
        _finish(state, directory, request, outcome, on_progress, on_event)
    except generator.Cancelled:
        _terminal(state, 'cancelled', '已取消；已完成的部分已保存，可以继续生成。', gen)
    except generator.GenerationFailed as exc:
        _terminal(state, 'failed', f'{exc.stage} 阶段失败：{exc.message}', gen,
                  error={'stage': exc.stage, 'message': exc.message, 'issues': exc.issues[:20]})


def _terminal(state, status, message, gen, error=None):
    with _LOCK:
        state.update(status=status, message=message, finished_at=time.time(), stats=dict(gen.stats), error=error)
        _event(state, 'error' if status == 'failed' else 'info', message)
        _save_state(state)


def _safe_name(text):
    return re.sub(r'[^\w一-鿿-]+', '', text or '') or 'pack'


def _finish(state, directory, request, outcome, on_progress, on_event):
    on_progress('export', '正在写出交付文件…', 0.97)
    bible = outcome.bible
    dirname = f"{_safe_name(bible.get('name'))}_{_safe_name(bible.get('subtitle'))}_{datetime.now().strftime('%Y%m%d')}_v1"
    theme = outcome.theme
    pkg = compose.compose_package(theme)
    report, delivery = compose.write_package_files(theme, pkg, directory / dirname)
    if delivery is None:
        raise generator.GenerationFailed('export', '校验器拒绝导出：' + '；'.join(f"{e['path']}: {e['message']}" for e in report['errors'][:5]))
    text = (delivery / '完整提示词.txt').read_text(encoding='utf-8')
    files = sorted(str(p.relative_to(directory / dirname)) for p in (directory / dirname).rglob('*') if p.is_file())
    result = {'dir': dirname, 'images': len(pkg['images']), 'videos': len(pkg['production_segments']), 'files': files,
              'warnings': [f"{w['code']}: {w['path']}" for w in report['warnings']], 'validation': report['overall_status'], 'import': None}
    with _LOCK:
        state['result'] = result
        state['title'] = request.get('title') or library_import.default_title(bible)
    if request.get('auto_import'):
        on_progress('import', '正在导入项目工作台…', 0.99)
        try:
            result['import'] = library_import.import_prompt_set(text, state['title'], source_label=f"beat_pack:{state['id']}")
            on_event('info', f"已导入项目工作台：{state['title']}（图片 {result['import']['images']}，视频 {result['import']['videos']}）")
        except library_import.ImportRefused as exc:
            result['import'] = {'status': 'refused', 'code': exc.code, 'message': str(exc)}
            on_event('warn', f'没有自动导入：{exc}。可以换个标题后点“导入项目”。')
    with _LOCK:
        state.update(status='completed', stage='done', progress=1.0, finished_at=time.time(),
                     message=f"完成：图片 {result['images']}，视频 {result['videos']}。", error=None)
        _event(state, 'info', state['message'])
        _save_state(state)
