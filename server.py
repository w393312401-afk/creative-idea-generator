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
import contextlib
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
from server_common import (
    _get_project_dir, _safe_project_name, _LOG_PATH, _account_switch_interval,
    _IP_ROTATE_DISABLED,
)

# 日志历史回放最多从文件末尾回读这么多字节（够装远超 100 行了）
_LOG_TAIL_BYTES = 256 * 1024
from prompt_pipeline import _parse_prompt_slots, _format_prompt_block, _chat, _aux_model
from frame_generator import (
    _image_generation_model_for_request,
    _image_quality_to_label,
    _generate_text_image,
    _run_async_image_generation,
    _run_async_image_edit
)
from fx_control import FX_CONTROL, FxQueueCancelled, FxQueueTimeout
from fx_console import FX_CONFIG_SPEC, FxConfigStore, apply_direct_env

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
            config['_beat_audit'] = []
        content = call_llm(config, dimensions, on_progress=on_progress)
        result = parse_sections(content)

        if run_cancelled():
            raise GenerationCancelled("Generation cancelled by user")
            
        # skill 直出模式：文本阶段无审查，audit_md 只是直出模式的说明文案；
        # 一致性审查在帧渲染后对真实画面进行（pipeline_orchestrator）。
        result['repair_md'] = result.get('audit_md') or 'PASS — 工序与场景一致性检查通过，未发现违规，提示词未改动。'
        # 直出校验留痕可见化（2026-07-15 事故复盘）：结构校验的命中记录以前只进
        # 日志，"10/12 拍没有动作正文"要烧完视频额度才被发现。详情汇入 audit_md
        # （审核面板按 markdown 渲染），存在结构性硬伤时 repair_md 换成醒目摘要
        # （非 PASS 开头 → 前端审核面板自动展开+高亮），点"生成帧序列"之前就可见。
        beat_audit = config.get('_beat_audit') if isinstance(config, dict) else None
        if beat_audit:
            structural_beats = [r for r in beat_audit if r.get('structural')]
            unfixed = [r for r in structural_beats if not r.get('reworked')]
            milestone_rows = [r for r in beat_audit if r.get('milestone_name')]
            milestone_ok = [r for r in milestone_rows if r.get('milestone_status') in ('passed', 'reworked')]
            lines = [f"### 直出校验留痕（{len(beat_audit)} 拍有记录）", '']
            if milestone_rows:
                lines.append(
                    f"- **显著阶段里程碑骨架：{len(milestone_ok)}/{len(milestone_rows)} 拍通过或已回炉**")
            for rec in beat_audit:
                milestone_prefix = (f"「{rec.get('milestone_name')}」· "
                                    if rec.get('milestone_name') else '')
                if rec.get('structural'):
                    fixed = '已定向回炉重写' if rec.get('reworked') else '回炉未通过，保留原稿'
                    lines.append(f"- **第 {rec['beat']} 拍 · {milestone_prefix}结构性硬伤（{fixed}）**：" + '；'.join(rec['structural']))
                if rec.get('style'):
                    # style 桶现在混装 IMAGE 相似度瑕疵和 stage_scope 措辞瑕疵两类，
                    # image_reworked 是两者合并后的信号，不能再硬说"相似度"——
                    # 具体是哪一类，读下面拼进来的 rec['style'] 原文就看得出来。
                    img_rw = rec.get('image_reworked')
                    style_note = ('IMAGE 已回炉重写' if img_rw is True
                                  else 'IMAGE 回炉未通过，保留原稿' if img_rw is False
                                  else '仅留痕')
                    lines.append(f"- 第 {rec['beat']} 拍 · {milestone_prefix}风格瑕疵（{style_note}）：" + '；'.join(rec['style']))
            result['audit_md'] = ((result.get('audit_md') or '').rstrip() + '\n\n' + '\n'.join(lines)).strip()
            if structural_beats:
                summary = (f"直出校验发现 {len(structural_beats)} 拍存在结构性硬伤"
                           f"（{len(structural_beats) - len(unfixed)} 拍已定向回炉重写"
                           + (f"，{len(unfixed)} 拍回炉未通过保留原稿——生成帧序列前建议先处理" if unfixed else "")
                           + "），详情见下方审核报告。")
                result['repair_md'] = summary
            result['beat_audit'] = beat_audit
        result['skipped_checks_count'] = config.get('_skipped_checks', 0) if isinstance(config, dict) else 0
        
        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage
            
        images, videos = _parse_prompt_slots(result['prompt_block'])
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

        # 任务列表成功卡会展示本次实际使用的模型与「激发总时间」。耗时放在
        # 所有激发收尾工作（含社媒标题生成）之后计算，避免此前的 timings 少算
        # 最后一段模型调用。model 跟随任务结果落盘，之后即使全局模型切换，历史
        # 卡片仍能正确默认选中本次实际使用的模型。
        result['model'] = config.get('model') if isinstance(config, dict) else None
        result['project_key'] = make_idea_project_key(task_id, result.get('title'))
        result['timings'] = {
            'total_duration_seconds': round(time.time() - start_time, 2)
        }

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
    start_time = time.time()
    project_label = None
    if isinstance(dimensions, dict):
        project_label = dimensions.get('task_label') or dimensions.get('theme')
    project_key = make_idea_project_key(task_id, project_label)
    set_project_key_context(project_key)
    set_log_context(task_id)
    log('INFO', 'AUTORUN', "开始自治管线任务")
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            # 已取消 → 不再追加事件/广播，直接 raise 让 worker 快速退出
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from pipeline_orchestrator import run_autonomous_pipeline
        # 自治管线的渲染/视频阶段驱动共享 FX 浏览器：全程持有 FX 串行锁
        # （_FX_SERIAL_LOCK 定义在本模块靠后位置，调用时才解析，安全）
        with _fx_browser_slot(task_id, 'auto', t['cancel_event']):
            result = run_autonomous_pipeline(config, dimensions, on_progress=progress_cb)

        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage
        result['model'] = config.get('model') if isinstance(config, dict) else None
        result['project_key'] = project_key
        result.setdefault('timings', {})['total_duration_seconds'] = round(time.time() - start_time, 2)

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'AUTORUN', "自治管线任务完成")
    except ConnectionError:
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了生成任务"
                t["events"].append(('error', {'message': '用户取消了生成任务'}))
                finalize = True
        if finalize:
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
        set_project_key_context(None)


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


def rate_ok(ip, action='default'):
    """Per-IP, per-action sliding-window rate limit; only enforced in managed mode.

    2026-07-24: 之前所有端点共用同一个 ip 级预算(RATE_MAX/RATE_WINDOW)——高频的
    浏览/激发(/api/ideate)和低频的重活(/api/compose)抢同一份配额，随手翻几页
    灵感就能把当小时的合成配额提前挤没，表现为"合成中途 429"。现按 action 分桶，
    互不挤占；action 由调用方传入端点标识，默认桶名 'default' 兼容旧调用。
    rateLimitEnabled=false（server_config.json）时整套频控直接放行——个人自用
    场景没有别人会撞见这个链接，不需要防滥用。"""
    if not RATE_LIMIT_ENABLED or not SERVER_MANAGED:
        return True
    if ip in ('127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1'):
        return True
    now = time.time()
    key = f'{action}:{ip}'
    with _RATE_LOCK:
        bucket = [t for t in _RATE_BUCKET[key] if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_MAX:
            _RATE_BUCKET[key] = bucket
            return False
        bucket.append(now)
        _RATE_BUCKET[key] = bucket
        return True


def _get_account_pool():
    """Google Flow 内置号池服务。"""
    from integrations.google_fx.utils import account_pool
    return account_pool.AccountPool()


def _get_proxy_pool():
    """Google FX 代理号池服务（出口代理，与账号池对称）。"""
    from integrations.google_fx.utils import proxy_pool
    return proxy_pool.ProxyPool()


def _require_fx_admission(handler, required=True):
    if not required:
        return True
    allowed, message = FX_CONTROL.admission()
    if allowed:
        return True
    handler._send_json({'status': 'error', 'code': 'FX_NOT_ACCEPTING', 'message': message}, status=503)
    return False


# FX 运行配置：白名单、校验与版本栈都在 fx_console.py（那套规则本身有独立的测试面，
# 塞在 HTTP 路由旁边会淹没）。这里只保留一个进程内单例。
FX_CONFIG = FxConfigStore(
    config=SERVER_CONFIG,
    config_file=SERVER_CONFIG_FILE,
    versions_file=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'runtime', 'fx_config_versions.jsonl'),
    apply_overrides=apply_google_fx_runtime_overrides,
    audit=FX_CONTROL.audit,
)


_FX_TASK_TYPES = {'frames', 'videos', 'staged_render', 'auto'}


def _is_fx_task(task_id, task):
    dimensions = task.get('dimensions') if isinstance(task, dict) else {}
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    task_type = str(dimensions.get('type') or '').strip().lower()
    task_id = str(task_id or '')
    return task_type in _FX_TASK_TYPES or task_id.startswith(
        ('frames_', 'videos_', 'staged_', 'auto_'))


def _fx_task_stage(task):
    for event_type, data in reversed(task.get('events') or []):
        if isinstance(data, dict) and data.get('stage'):
            return str(data['stage'])
        if event_type not in ('text_chunk', 'heartbeat'):
            return str(event_type)
    return ''


# 阶段驻留时间超过这个秒数就在控制台标红：绝大多数"看起来卡住了"都是某个阶段在空转
# （典型的 180s 等工具栏），而不是整体慢。阈值按阶段分档，未列出的用默认值。
_FX_STAGE_SLOW_SECONDS = {
    'queue': 900,
    'manual_intervention': 3600,
}
_FX_STAGE_SLOW_DEFAULT = 300


def _fx_task_timeline(task_id, running, limit=12):
    """阶段时间线：每个阶段何时进入、驻留多久、是否偏慢。

    _fx_task_stage 只告诉你"最后一个阶段是什么"、没有耗时，于是"卡在哪一步、卡了
    多久"只能去日志里翻时间戳。轨迹由 server_common.record_task_stage 在事件广播的
    必经之路上记录（任务事件本身不带时间戳）。
    """
    rows = task_stage_timeline(task_id)
    now = time.time()
    for index, row in enumerate(rows):
        end = rows[index + 1]['at'] if index + 1 < len(rows) else (
            now if running else row['last_at'])
        row['duration_seconds'] = round(max(0.0, end - row['at']), 1)
        threshold = _FX_STAGE_SLOW_SECONDS.get(row['stage'], _FX_STAGE_SLOW_DEFAULT)
        row['slow'] = row['duration_seconds'] > threshold
        row['at_iso'] = datetime.fromtimestamp(row['at']).astimezone().isoformat()
    return rows[-limit:]


def _fx_manual_intervention(task_id, account=''):
    """任务当前是否卡在"等待人工登录/验证码"，以及还剩多久。

    wait_out_manual_intervention 会发 manual_intervention_detected/cleared/timeout
    事件，此前控制台完全没有体现——用户不知道服务正在等自己去浏览器里点两下，
    只看到任务"卡住了"。
    """
    state = None
    for row in task_stage_timeline(task_id):
        event_type = row.get('event_type') or ''
        if not event_type.startswith('manual_intervention_'):
            continue
        if event_type.endswith('_detected'):
            state = {
                'code': row.get('code') or '',
                'reason': row.get('message') or '',
                'max_wait_seconds': row.get('max_wait_seconds'),
                'since': row.get('at'),
                'account': account,
            }
        else:
            state = None  # cleared / timeout 都表示不再等待
    if state and isinstance(state.get('since'), (int, float)):
        elapsed = max(0.0, time.time() - state['since'])
        state['elapsed_seconds'] = round(elapsed, 1)
        if state.get('max_wait_seconds'):
            state['remaining_seconds'] = round(
                max(0.0, float(state['max_wait_seconds']) - elapsed), 1)
    return state


# 状态快照的短 TTL 缓存：控制台按秒级轮询，而快照要读若干状态文件 + 探一次端口。
# 缓存把"多个标签页/多次刷新"压成一次实际计算，同时保证数据新鲜度肉眼无差。
_FX_SNAPSHOT_TTL_SECONDS = 1.5
_FX_SNAPSHOT_CACHE = {'at': 0.0, 'value': None}
_FX_SNAPSHOT_LOCK = threading.Lock()


def google_fx_status_snapshot(force=False):
    """带 TTL 缓存的状态快照入口。"""
    with _FX_SNAPSHOT_LOCK:
        cached = _FX_SNAPSHOT_CACHE['value']
        if (not force and cached is not None
                and time.time() - _FX_SNAPSHOT_CACHE['at'] < _FX_SNAPSHOT_TTL_SECONDS):
            return cached
    value = _google_fx_status_snapshot()
    with _FX_SNAPSHOT_LOCK:
        _FX_SNAPSHOT_CACHE['at'] = time.time()
        _FX_SNAPSHOT_CACHE['value'] = value
    return value


def _inert_config_notes(config):
    """列出"存进去了、也热生效了，但被别的配置项压住跑不起来"的字段。

    保存接口用它给出如实回执。此前这类冲突只有一个后果：控制台弹"已保存并热生效"，
    实际行为一点没变，而唯一的解释写在手册第 4.1 节里。
    """
    notes = []
    if config.get('googleFxSequenceUserLock') and str(config.get('googleFxSequenceUserId') or '').strip():
        notes.append(
            f'换号节拍（每 {_account_switch_interval(config)} 个请求换一个号）：'
            '「锁定默认环境」开着时整条序列固定在同一个号上，这个节拍不会被用到')
    return notes


def _google_fx_status_snapshot():
    """返回控制台所需的非敏感 FX 运维快照；不启动或关闭任何浏览器。

    ⚠️ 本函数必须保持"只读本地状态 + 一次端口 connect"的成本。控制台按秒级轮询它，
    任何会打 AdsPower HTTP 的调用（例如 list_accounts 的命名自愈）都必须排除在外，
    否则 AdsPower 一卡，"只读状态"接口跟着卡，控制台整体表现为假死。
    """
    checked_at = datetime.now().astimezone().isoformat()
    diagnostics = []

    runtime = {'available': False, 'package': 'integrations.google_fx', 'error': None}
    selector_rows = []
    cancel_active = 0
    runtime_config = None
    try:
        from integrations.google_fx import config as runtime_config
        from integrations.google_fx.services import google_fx_image, google_fx_video
        from integrations.google_fx.utils import selector_stats
        runtime['available'] = True
        runtime['path'] = str(runtime_config.CORE_DIR)
        selector_rows = selector_stats.summarize()
        cancel_active = get_fx_cancel_flag().active_count()
    except Exception as e:
        runtime['error'] = f'{type(e).__name__}: {e}'
        diagnostics.append({'level': 'error', 'code': 'runtime_unavailable',
                            'message': f'Google FX 运行时不可用：{runtime["error"]}'})

    cfg = effective_config({})
    port_value = cfg.get('adsPowerPort') or (
        runtime_config.get_runtime_default_port() if runtime_config is not None else '50325')
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        port = 50325

    started = time.perf_counter()
    adspower_online = False
    adspower_error = None
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.35):
            adspower_online = True
    except OSError as e:
        adspower_error = str(e)
    latency_ms = round((time.perf_counter() - started) * 1000)
    if not adspower_online:
        diagnostics.append({'level': 'error', 'code': 'adspower_offline',
                            'message': f'AdsPower 本地 API 端口 {port} 无法连接'})

    accounts = []
    try:
        # heal=False 是硬要求：命名自愈会打 AdsPower 本地 HTTP（含限频退避重试，
        # 最坏十几秒），放在秒级轮询的只读快照里会把 AdsPower 的卡顿传导成控制台假死。
        accounts = _get_account_pool().list_accounts(heal=False)
    except Exception as e:
        diagnostics.append({'level': 'error', 'code': 'account_pool_unavailable',
                            'message': f'号池读取失败：{e}'})

    now = datetime.now().astimezone()
    enabled = disabled = cooling = low_credit = ready = unprobed = login_required = 0
    stale_credit = probe_failed = 0
    for account in accounts:
        if account.get('disabled'):
            disabled += 1
            continue
        enabled += 1
        is_cooling = False
        cooldown_until = account.get('cooldown_until')
        if cooldown_until:
            try:
                if datetime.fromisoformat(cooldown_until) > now:
                    cooling += 1
                    is_cooling = True
            except (TypeError, ValueError):
                pass
        credit = account.get('credit')
        if account.get('last_probe_status') == 'failed':
            probe_failed += 1
        if credit is None:
            # 从未探测成功：既不算可用也不算耗尽。pick_account 会强制探测一次再决定，
            # 这里如实计入 unprobed，避免把"不知道"渲染成"有额度"。
            unprobed += 1
        elif account.get('credit_stale'):
            stale_credit += 1
        elif isinstance(credit, (int, float)) and credit <= 0:
            low_credit += 1
        elif account.get('credit_trustworthy', account.get('last_probe_status') != 'failed') and not is_cooling:
            ready += 1
        if account.get('cooldown_reason') == 'login_required':
            login_required += 1

    if not accounts:
        diagnostics.append({'level': 'warn', 'code': 'account_pool_empty',
                            'message': '号池为空，生成任务只能依赖手动指定的浏览器环境'})
    elif ready == 0:
        diagnostics.append({'level': 'error', 'code': 'no_available_account',
                            'message': '号池当前没有可立即使用的账号'})
    elif cooling:
        diagnostics.append({'level': 'warn', 'code': 'accounts_cooling',
                            'message': f'{cooling} 个账号处于冷却期'})
    if login_required:
        diagnostics.append({'level': 'error', 'code': 'accounts_login_required',
                            'message': f'{login_required} 个账号登录失效，需要人工在 AdsPower 浏览器里重新登录'})
    if unprobed:
        diagnostics.append({'level': 'warn', 'code': 'accounts_unprobed',
                            'message': f'{unprobed} 个账号从未成功探测过积分（选号时会先强制探测一次）'})
    if stale_credit:
        diagnostics.append({'level': 'warn', 'code': 'accounts_credit_stale',
                            'message': f'{stale_credit} 个账号的积分缓存已过期，不会直接用于选号'})
    if probe_failed:
        diagnostics.append({'level': 'warn', 'code': 'accounts_probe_failed',
                            'message': f'{probe_failed} 个账号最近一次积分探测失败，请检查登录或选择器'})

    # 代理号池摘要：只读一个本地 JSON，不做连通性检测（检测要走网络，秒级轮询扛不住）
    proxies = {'total': 0, 'enabled': 0, 'disabled': 0, 'ok': 0, 'failed': 0,
               'unchecked': 0, 'bound': 0}
    try:
        proxies = _get_proxy_pool().summary()
    except Exception as e:
        diagnostics.append({'level': 'warn', 'code': 'proxy_pool_unavailable',
                            'message': f'代理号池读取失败：{e}'})
    if proxies.get('failed'):
        diagnostics.append({
            'level': 'warn', 'code': 'proxies_failed',
            'message': f'{proxies["failed"]} 条代理最近一次连通性检测失败，不会参与轮换',
        })

    # 序列生成默认环境：钉了一个不在池子里/已禁用的环境，生成时会静默降级成自动
    # 选号——那正是最难自查的一类"配了但没生效"，在这里先报出来。
    sequence_user_id = str(cfg.get('googleFxSequenceUserId') or '').strip()
    sequence_locked = bool(cfg.get('googleFxSequenceUserLock')) and bool(sequence_user_id)
    if sequence_user_id:
        picked = next((a for a in accounts if str(a.get('user_id')) == sequence_user_id), None)
        if picked is None:
            diagnostics.append({
                'level': 'warn', 'code': 'sequence_account_missing',
                'message': (f'序列生成默认浏览器环境 {sequence_user_id} 不在号池里，'
                            '生成时会退回自动选号'),
            })
        elif picked.get('disabled'):
            diagnostics.append({
                'level': 'warn', 'code': 'sequence_account_disabled',
                'message': f'序列生成默认浏览器环境 {sequence_user_id} 已禁用，生成时会退回自动选号',
            })
    # 「锁定默认环境」会把轮转环压成单元素（_account_rotation_ring），于是切腿逻辑
    # 整个退化成单腿，换号节拍那个数字一次都不会被用到。这是设计如此，但此前只写在
    # 手册里：控制台照旧显示「换号节拍 每 N 次请求」并在保存时说「已热生效」，
    # 用户改了半天节拍却一次号都不换，正是最难自查的"配了但没生效"。
    if sequence_locked:
        diagnostics.append({
            'level': 'warn', 'code': 'switch_interval_inert',
            'message': (f'「锁定默认环境」已开启：整条序列固定跑在 {sequence_user_id} 上，'
                        f'换号节拍（每 {_account_switch_interval(cfg)} 个请求换一个号）不生效。'
                        '想按节拍轮转号池，请到「运行配置 → 号池」取消勾选锁定'),
        })

    selector_warnings = [row for row in selector_rows
                         if row.get('miss', 0) > 0 or row.get('primary_ratio', 1) < 0.8]
    if selector_warnings:
        diagnostics.append({'level': 'warn', 'code': 'selector_drift',
                            'message': f'{len(selector_warnings)} 组 Flow UI 选择器出现回退或未命中'})

    queue_snapshot = FX_CONTROL.snapshot()
    waiting_by_id = {row['task_id']: (index + 1, row) for index, row in enumerate(queue_snapshot['waiting'])}
    active_by_id = {row['task_id']: row for row in (queue_snapshot.get('active_list') or [])}

    with ACTIVE_TASKS_LOCK:
        fx_tasks = []
        for task_id, task in ACTIVE_TASKS.items():
            if not _is_fx_task(task_id, task):
                continue
            dimensions = task.get('dimensions') or {}
            queue_position, queue_row = waiting_by_id.get(task_id, (None, {}))
            active_row = active_by_id.get(task_id) or {}
            running = task.get('status') == 'running'
            account = dimensions.get('userId') or dimensions.get('googleFxUserId') or ''
            timeline = _fx_task_timeline(task_id, running)
            fx_tasks.append({
                'id': task_id,
                'type': dimensions.get('type') or 'fx',
                'status': task.get('status'),
                'stage': _fx_task_stage(task),
                'theme': dimensions.get('task_label') or dimensions.get('theme') or '',
                'account': account,
                'account_pin': queue_row.get('account_pin') or active_row.get('account_pin') or '',
                'error': task.get('error') or '',
                'last_active': task.get('last_active') or 0,
                'queue_state': ('active' if active_row else
                                ('waiting' if queue_position else 'outside')),
                'queue_position': queue_position,
                'priority': queue_row.get('priority', active_row.get('priority', 0)),
                'elapsed_seconds': active_row.get('elapsed_seconds'),
                'overdue': bool(active_row.get('overdue')),
                'timeline': timeline,
                'stuck_stage': next((row for row in reversed(timeline) if row.get('slow')), None),
                'manual_intervention': _fx_manual_intervention(task_id, account),
            })
    fx_tasks.sort(key=lambda row: row['last_active'], reverse=True)
    recent_failures = [row for row in fx_tasks if row['status'] == 'failed'][:5]
    if recent_failures:
        diagnostics.append({'level': 'warn', 'code': 'recent_failures',
                            'message': f'最近记录中有 {len(recent_failures)} 个 FX 任务失败'})
    blocked = [row for row in fx_tasks if row.get('manual_intervention')]
    if blocked:
        first = blocked[0]['manual_intervention']
        remaining = first.get('remaining_seconds')
        diagnostics.append({
            'level': 'error', 'code': 'manual_intervention_waiting',
            'message': (f'{len(blocked)} 个任务正在等待人工处理（{first.get("code") or "未知原因"}）'
                        + (f'，剩余 {int(remaining)}s' if remaining else '')
                        + '：请到对应 AdsPower 浏览器窗口完成登录/验证'),
        })
    stuck = [row for row in fx_tasks if row['status'] == 'running' and row.get('stuck_stage')]
    if stuck:
        worst = stuck[0]['stuck_stage']
        diagnostics.append({
            'level': 'warn', 'code': 'stage_stalled',
            'message': (f'{len(stuck)} 个运行中任务的当前阶段耗时偏长'
                        f'（{worst.get("stage")} 已 {int(worst.get("duration_seconds") or 0)}s）'),
        })
    if queue_snapshot.get('orphaned'):
        diagnostics.append({
            'level': 'warn', 'code': 'queue_orphaned',
            'message': (f'上次进程退出时有 {len(queue_snapshot["orphaned"])} 个任务仍在队列中'
                        '（服务被强制结束或崩溃），它们不会自动恢复'),
        })
    if queue_snapshot.get('mode') != 'running':
        diagnostics.append({
            'level': 'warn', 'code': 'service_not_accepting',
            'message': f'服务处于 {queue_snapshot.get("mode")} 模式，新任务会被拒绝',
        })

    selected_user_id = str(cfg.get('googleFxUserId') or os.environ.get(
        'ADSPOWER_DEFAULT_USER_ID', '')).strip()

    # IP 轮换状态如实读取，不再硬编码 False。换 IP 是全局关停的
    # （apply_google_fx_runtime_overrides 把 MIYA_ROTATE_THRESHOLD 写成够不到的阈值），
    # 但"关停"这件事此前只体现在一个写死的字面量上：一旦哪条路径漏了 override，
    # 控制台照旧显示"已关闭"而实际在换 IP。现在读真实配置与阈值来判定。
    rotation = {'configured': False, 'auto_rotate': False, 'threshold': None, 'effective': False}
    try:
        from integrations.google_fx.utils.proxy_rotator import ProxyRotator
        rotator = ProxyRotator()
        rotation['configured'] = bool(rotator.is_configured)
        rotation['auto_rotate'] = bool(rotator.auto_rotate)
        rotation['threshold'] = rotator.rotate_threshold
        rotation['effective'] = bool(
            rotator.is_configured and rotator.auto_rotate
            and rotator.rotate_threshold < _IP_ROTATE_DISABLED)
    except Exception as e:
        rotation['error'] = f'{type(e).__name__}: {e}'
    if rotation['effective']:
        diagnostics.append({
            'level': 'warn', 'code': 'ip_rotation_active',
            'message': (f'IP 轮换处于生效状态（阈值 {rotation["threshold"]}）。'
                        '实测换 IP 会打失效 Flow 登录 token，本项目默认应为关停'),
        })

    return {
        'status': 'ok',
        'checked_at': checked_at,
        'runtime': runtime,
        'adspower': {
            'online': adspower_online,
            'host': '127.0.0.1',
            'port': port,
            'latency_ms': latency_ms,
            'error': adspower_error,
        },
        'execution': {
            'lock_busy': bool(queue_snapshot.get('active')) or _FX_SERIAL_LOCK.locked(),
            'active_requests': cancel_active,
            'running_tasks': sum(1 for row in fx_tasks if row['status'] == 'running'),
        },
        'configuration': {
            'image_model': cfg.get('googleFxImageModel') or 'Nano Banana 2',
            'video_model': cfg.get('videoModel') or 'Veo 3.1 - Lite [Lower Priority]',
            'video_duration': cfg.get('videoDuration') or '',
            'account_switch_requests': _account_switch_interval(cfg),
            # 锁定默认环境时轮转环只剩一个号，节拍值形同虚设——如实标出来，
            # 别让状态条把一个死设置显示成正在生效
            'account_switch_effective': not sequence_locked,
            'selected_user_id': selected_user_id,
            'sequence_user_id': sequence_user_id,
            'sequence_user_locked': sequence_locked,
            'ip_rotation_enabled': rotation['effective'],
            'ip_rotation': rotation,
            'dry_run': os.environ.get('FX_DRY_RUN', '0') in ('1', 'true', 'yes'),
            'debug_capture': os.environ.get('GOOGLE_FX_DEBUG_CAPTURE', '1') not in ('0', 'false', 'no'),
        },
        'accounts': {
            'total': len(accounts),
            'enabled': enabled,
            'disabled': disabled,
            'cooling': cooling,
            'zero_credit': low_credit,
            'unprobed': unprobed,
            'stale_credit': stale_credit,
            'probe_failed': probe_failed,
            'login_required': login_required,
            'ready': ready,
        },
        'proxies': proxies,
        'tasks': fx_tasks[:20],
        'queue': queue_snapshot,
        'recent_failures': recent_failures,
        'selectors': {
            'families': len(selector_rows),
            'warnings': selector_warnings[:8],
            'version': _fx_selector_version(),
        },
        'diagnostics': diagnostics,
    }


def _fx_selector_version():
    try:
        from integrations.google_fx.ui_selectors import SELECTOR_VERSION
        return SELECTOR_VERSION
    except Exception:
        return ''


# FX 日志里带这些模块名的行才算"FX 相关"。日志格式是
# `[时:分:秒] │ 图标 │ 模块名 │ 内容`（见 integrations/google_fx/utils/logger.log）。
_FX_LOG_MODULES = ('GoogleFX', 'GoogleFX-Video', '账号池', '积分探针', '代理轮换',
                   '浏览器启动', '浏览器关闭', '取证', '自检', '路径修复', '输入优化',
                   'FX', 'VIDEOS', 'FRAMES', 'STAGED', 'CANCEL')


def _fx_log_path():
    """Return the same file used by integrations.google_fx.utils.logger.

    FX has its own rotating logger. Reading server_common._LOG_PATH happened to
    work only when stdout was also redirected to that file by the launcher; in
    normal/dev launches the console endpoint consequently returned an empty
    result even while the dedicated FX log was growing.
    """
    try:
        from integrations.google_fx.utils.logger import get_log_file_path
        return get_log_file_path()
    except Exception:
        return _LOG_PATH


def _fx_log_tail(task_id='', keyword='', limit=120, fx_only=True):
    """从日志文件尾部回读并按 FX 模块 / task_id / 关键字过滤。

    /api/logs/stream 是全量流：排查 FX 问题时 LLM 合成、技能契约等日志会把有用的
    行冲走。这里只回读文件末尾一段（日志上限本来就有轮转），不整份读进内存。
    """
    limit = max(1, min(int(limit), 800))
    task_id = str(task_id or '').strip()
    keyword = str(keyword or '').strip().lower()
    log_path = _fx_log_path()
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, 'rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            # 过滤越严就要回读越多才能凑够 limit 行
            window = _LOG_TAIL_BYTES * (4 if (task_id or keyword) else 1)
            if size > window:
                handle.seek(size - window)
                raw = handle.read()
                newline = raw.find(b'\n')
                raw = raw[newline + 1:] if newline != -1 else raw
            else:
                raw = handle.read()
        lines = raw.decode('utf-8', errors='replace').splitlines()
    except Exception:
        return []

    picked = []
    for line in reversed(lines):
        if task_id and f'[{task_id}]' not in line and task_id not in line:
            continue
        if keyword and keyword not in line.lower():
            continue
        if fx_only and not any(module in line for module in _FX_LOG_MODULES):
            continue
        picked.append(line)
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked


def generate_frames_worker(task_id, config, title, prompt_block, target_sequences):
    t = get_or_create_task(task_id)
    set_project_key_context(config.get('_project_key') if isinstance(config, dict) else None)
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
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
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
            # google_fx 后端共享同一个 AdsPower 浏览器、外部脚本的运行锁，以及
            # builtins.google_fx_cancelled 这个 SPARK 内部旗标：必须与视频生成互斥，
            # 否则新任务开跑时重置旗标/抢浏览器，会把另一个在跑的 FX 任务搅乱
            # （跨任务串片事故的同族问题）。请求 ID 由队列上下文绑定到当前任务，
            # 取消端点只撤销对应任务，不再影响队列中的其他任务。
            with _fx_serial_lock_for(config, task_id, 'frames', t['cancel_event']):
                if target_sequences is None:
                    # 整单渲染走编排层：分段渲染+检查点现实校准+链尾回望。
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
                    # 手动逐帧/子集重试路径本不挂一致性防护（见上面「整单渲染走编排层」
                    # 的注释）；但如果这次调用后整套序列的所有帧都已在磁盘落地，说明这
                    # 正是逐帧点完的最后一帧——趁此机会补跑一次链尾回望+整套一致性复审，
                    # 否则纯手动逐帧点满整个序列会永远吃不到这层保护（2026-07-22 喀斯特
                    # 洞穴/沙漠花岗岩两单实测：全程逐帧手动生成，第4帧门框清除失败、
                    # 第12帧严重跑偏，复审机制全程未被触发过，manifest 里所有帧的
                    # quality_gate 停留在初始的 pending_manual_review，从未被真正复审）。
                    try:
                        from pipeline_orchestrator import (
                            _frame_path, _chain_drift_lookback, _sequence_consistency_review,
                        )
                        images, _videos = _parse_prompt_slots(prompt_block)
                        if images and all(os.path.exists(_frame_path(title, s)) for s in images):
                            project_dir = _get_project_dir(title)
                            _chain_drift_lookback(config, title, prompt_block, project_dir,
                                                  on_progress=progress_cb)
                            _sequence_consistency_review(config, title, prompt_block, project_dir,
                                                         on_progress=progress_cb)
                            result = read_manifest(project_dir) or result
                    except ConnectionError:
                        raise
                    except Exception as review_err:
                        log('WARN', 'FRAMES',
                            f"子集/手动生成后补跑一致性复审失败（不影响已生成的帧）: {review_err}",
                            title=title)
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
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了帧序列生成"
                t["events"].append(('error', {'message': '用户取消了帧序列生成'}))
                finalize = True
        if finalize:
            notify_listeners(task_id, 'error', {'message': '用户取消了帧序列生成'})
        log('WARN', 'FRAMES', "帧序列任务已被用户取消", title=title)
        # 取消不关浏览器（2026-07-26）：见 /api/compose-cancel 里的同款说明。
    except Exception as e:
        log_exception('FRAMES')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'FRAMES', f"帧序列任务失败: {e}", title=title)
    finally:
        release_frame_run(_get_project_dir(title), task_id)
        save_tasks_to_disk()
        set_log_context(None)
        set_project_key_context(None)


def fix_frame_issue_worker(task_id, config, title, prompt_block, sequence, manual_reason=None):
    """人工在帧网格点击「修复此帧问题」后台工作线程：读取该帧记录的问题原因、
    优化提示词后图生图重渲（pipeline_orchestrator.fix_frame_issue）。与
    generate_frames_worker 共用同一个项目级互斥（claim_frame_run）——两者都会
    整体改写同一份 manifest.json，不能并发跑。

    manual_reason 是人工在修复对话框里现场写下的问题描述（可为空——此时只修
    manifest 上已记录的问题），会先落盘再参与改写，见 fix_frame_issue。"""
    t = get_or_create_task(task_id)
    set_project_key_context(config.get('_project_key') if isinstance(config, dict) else None)
    set_log_context(task_id)
    log('INFO', 'FRAMES', f"开始修复第 {sequence} 帧问题", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from frame_generator import set_upstream_event_sink, set_cancel_check_sink
        set_upstream_event_sink(lambda ev: progress_cb('upstream_retry', ev))
        set_cancel_check_sink(lambda: t["cancel_event"].is_set())
        try:
            from pipeline_orchestrator import fix_frame_issue
            with _fx_serial_lock_for(config, task_id, 'frame_fix', t['cancel_event']):
                result = fix_frame_issue(config, title, prompt_block, sequence,
                                         on_progress=progress_cb, manual_reason=manual_reason)
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
        log('INFO', 'FRAMES', f"第 {sequence} 帧问题修复完成", title=title)
    except ConnectionError:
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了修复"
                t["events"].append(('error', {'message': '用户取消了修复'}))
                finalize = True
        if finalize:
            notify_listeners(task_id, 'error', {'message': '用户取消了修复'})
        log('WARN', 'FRAMES', f"第 {sequence} 帧修复已被用户取消", title=title)
    except Exception as e:
        log_exception('FRAMES')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'FRAMES', f"第 {sequence} 帧修复失败: {e}", title=title)
    finally:
        release_frame_run(_get_project_dir(title), task_id)
        save_tasks_to_disk()
        set_log_context(None)
        set_project_key_context(None)


def sequence_review_worker(task_id, config, title, prompt_block):
    """人工在帧网格点击「运行一致性审查」后台工作线程：2026-07-24 起该审查不再随
    帧序列渲染自动触发，改成用户确认整套序列已渲染完成后手动点按钮才跑
    （pipeline_orchestrator.run_sequence_consistency_review）。与
    generate_frames_worker/fix_frame_issue_worker 共用同一个项目级互斥
    （claim_frame_run）——都会整体改写同一份 manifest.json，不能并发跑。只标记
    quality_gate，不改 prompt_block、不生成图片，故不需要 FX 串行锁。"""
    t = get_or_create_task(task_id)
    set_project_key_context(config.get('_project_key') if isinstance(config, dict) else None)
    set_log_context(task_id)
    log('INFO', 'FRAMES', "开始整套序列一致性审查", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        from frame_generator import set_upstream_event_sink, set_cancel_check_sink
        set_upstream_event_sink(lambda ev: progress_cb('upstream_retry', ev))
        set_cancel_check_sink(lambda: t["cancel_event"].is_set())
        try:
            from pipeline_orchestrator import run_sequence_consistency_review
            reviewed_prompt_block = run_sequence_consistency_review(config, title, prompt_block, on_progress=progress_cb)
        finally:
            set_upstream_event_sink(None)
            set_cancel_check_sink(None)
        result = {'prompt_block': reviewed_prompt_block}
        usage = stop_and_get_accounting()
        if usage:
            result['token_usage'] = usage

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))
        notify_listeners(task_id, 'result', result)
        log('INFO', 'FRAMES', "整套序列一致性审查完成", title=title)
    except ConnectionError:
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了审查"
                t["events"].append(('error', {'message': '用户取消了审查'}))
                finalize = True
        if finalize:
            notify_listeners(task_id, 'error', {'message': '用户取消了审查'})
        log('WARN', 'FRAMES', "一致性审查已被用户取消", title=title)
    except Exception as e:
        log_exception('FRAMES')
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = str(e)
            t["events"].append(('error', {'message': str(e)}))
        notify_listeners(task_id, 'error', {'message': str(e)})
        log('ERROR', 'FRAMES', f"一致性审查失败: {e}", title=title)
    finally:
        release_frame_run(_get_project_dir(title), task_id)
        save_tasks_to_disk()
        set_log_context(None)
        set_project_key_context(None)


def render_staged_worker(task_id, config, title, prompt_block):
    """Stages an ALREADY-composed prompt_block through pipeline_orchestrator's
    render/gate/recovery machinery (render IMAGE 1 -> Anchor Acceptance Gate -> render
    the rest -> autonomous recovery passes -> videos), instead of the old
    generate_frames_worker's one-shot full-batch render. Used by /api/render_staged,
    which scripts/generate_frames.py now calls so a skill-driven (agent-composed)
    prompt set also gets staged, gated rendering instead of a blind batch render."""
    t = get_or_create_task(task_id)
    set_project_key_context(config.get('_project_key') if isinstance(config, dict) else None)
    set_log_context(task_id)
    log('INFO', 'STAGED', "开始分步渲染任务", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()

        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
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
            with _fx_browser_slot(task_id, 'staged_render', t['cancel_event']):
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
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了分步渲染任务"
                t["events"].append(('error', {'message': '用户取消了分步渲染任务'}))
                finalize = True
        if finalize:
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
        release_frame_run(_get_project_dir(title), task_id)
        save_tasks_to_disk()
        set_log_context(None)
        set_project_key_context(None)


# FX 浏览器串行锁 —— FX_CONTROL 是唯一 admission 入口。
#
# 2026-07-26 修复（清单 S1）：这里原来是「先排 FX_CONTROL 队列，再裸 acquire 一把
# _FX_SERIAL_LOCK 作迁移期双保险」，而 _fx_serial_lock_for 在 task_id/cancel_event
# 缺省时又会**直接返回裸锁**、绕过队列。两条路径混用时：裸锁路径持有锁 → 已经拿到
# 队列 active 名额的任务无限期阻塞在 acquire 上 → FX_CONTROL 认为"有任务在执行"，
# 整条队列停滞，而控制台的取消只在 slot 的等待阶段生效，对已进入临界区的任务无效，
# 于是只能重启服务。
#
# 现在：所有路径都经过 FX_CONTROL.slot（无 task_id 的调用方也会拿到一个合成 id，
# 因此在控制台里同样可见）；_FX_SERIAL_LOCK 降级为纯粹的兜底互斥，且只能通过
# _fx_guard_lock 带超时 + 取消轮询地获取，永不无限期阻塞。
_FX_SERIAL_LOCK = threading.Lock()

# 兜底互斥锁的最长等待：正常情况下 FX_CONTROL 已保证单活跃，走到超时说明有调用方
# 绕过了队列（回归测试 tests/test_fx_serial_lock_guard.py 覆盖这个场景）。
_FX_GUARD_LOCK_TIMEOUT_SECONDS = 300


@contextlib.contextmanager
def _fx_guard_lock(label, cancel_check=None, timeout=None):
    """带超时与取消轮询地获取 _FX_SERIAL_LOCK。

    拿不到锁时抛错而不是永久阻塞——把"有人绕过队列"变成一条可诊断的失败，
    而不是整个服务静默卡死。
    """
    timeout = float(timeout if timeout is not None else _FX_GUARD_LOCK_TIMEOUT_SECONDS)
    deadline = time.time() + timeout
    while True:
        if cancel_check and cancel_check():
            raise GenerationCancelled("Generation cancelled by user")
        if _FX_SERIAL_LOCK.acquire(timeout=0.25):
            break
        if time.time() >= deadline:
            raise RuntimeError(
                f'等待 FX 浏览器兜底互斥锁超过 {timeout:.0f}s（{label}）。'
                'FX_CONTROL 队列已放行本任务，说明有调用方绕过了队列，请查看 runtime/fx_audit.jsonl'
            )
    try:
        yield
    finally:
        try:
            _FX_SERIAL_LOCK.release()
        except RuntimeError:
            pass


@contextlib.contextmanager
def _fx_browser_slot(task_id, kind, cancel_event=None, priority=0, cancel_check=None):
    """排队进入 FX 浏览器临界区。嵌套调用由 FX_CONTROL 自己的可重入判断放行。"""
    if cancel_check is None and cancel_event is not None:
        cancel_check = cancel_event.is_set
    account_pin = _fx_account_pin_for(task_id)
    # server_common.log() already carries task context, but the Google FX package
    # uses a separate logger. Keep both contexts aligned so the console's
    # task-specific live-log filter can actually find package-level messages.
    from integrations.google_fx.utils.logger import set_task_label, reset_task_label
    log_token = set_task_label(task_id)
    try:
        with FX_CONTROL.slot(task_id, kind, cancel_check=cancel_check,
                             priority=priority, account_pin=account_pin):
            with _fx_guard_lock(f'{kind}:{task_id}', cancel_check=cancel_check):
                yield
    finally:
        reset_task_label(log_token)


def _fx_account_pin_for(task_id):
    """任务被控制台钉过账号时取出来（钉在 waiting 条目上，进临界区时生效）。"""
    try:
        for row in FX_CONTROL.snapshot().get('waiting') or []:
            if str(row.get('task_id')) == str(task_id) and row.get('account_pin'):
                return row['account_pin']
    except Exception:
        pass
    return None


def _fx_serial_lock_for(config, task_id=None, kind='frames', cancel_event=None):
    """帧序列任务仅在走 google_fx 后端时才需要 FX 串行锁；纯 API 渲染不串行。"""
    uses_fx = isinstance(config, dict) and config.get('imageBackend') == 'google_fx'
    if not uses_fx:
        return contextlib.nullcontext()
    if task_id is None:
        # 无任务上下文的调用方也必须进队列，否则就是 S1 里那条绕过队列的裸锁路径。
        task_id = f'anon_{kind}_{int(time.time() * 1000)}'
    return _fx_browser_slot(task_id, kind, cancel_event)


# ── FX 运行时装配（2026-07-26）────────────────────────────────────────────────
# 内置包里需要浏览器的"旁路动作"（积分探针、选择器探针、自检）过去直接启浏览器，
# 完全绕开队列：生成任务跑着的时候点"刷新积分"就会去抢同一个 AdsPower profile。
# 这里把 FX_CONTROL 作为闸门装进包内的 browser_gate，于是旁路动作也排同一条队、
# 在控制台可见、可取消；嵌套调用（选号时的 stale 积分探测发生在已持有 slot 的
# 线程里）由 FX_CONTROL.slot 的可重入判断放行。

# 自带墙钟预算的旁路动作：它们占用浏览器的时间是有界的，所以别的旁路动作可以安心
# 排在它们后面，不必像遇到生成任务那样直接拒绝（见 /api/account-pool/refresh）。
FX_BOUNDED_BYPASS_KINDS = {'credit_probe', 'selector_probe', 'selftest'}


def _fx_browser_gate(kind, cancel_check=None, priority=0, task_id=None):
    """browser_gate 的宿主实现：把包内旁路动作接进 FX_CONTROL 队列。"""
    task_id = str(task_id or f'{kind}_{int(time.time() * 1000)}')
    return _fx_browser_slot(task_id, kind, priority=priority, cancel_check=cancel_check)


def _fx_queue_account_pin():
    """把当前活跃任务被钉的账号交给包内的账号解析链。"""
    try:
        from fx_control import current_account_pin
        return current_account_pin()
    except Exception:
        return None


_FX_WATCHDOG_STARTED = threading.Event()


def _cancel_fx_task(task_id, reason, actor='local'):
    """给一个 FX 任务发取消信号并立即终态化（浏览器保持开启）。

    /api/compose-cancel 与执行超时看门狗共用这一条路径，避免两处逻辑漂移。
    """
    task_id = str(task_id)
    finalized = False
    with ACTIVE_TASKS_LOCK:
        t = ACTIVE_TASKS.get(task_id)
        if t is not None:
            t["cancel_event"].set()
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = reason
                t["events"] = [evt for evt in t["events"] if evt[0] != 'text_chunk']
                t["events"].append(('error', {'message': reason}))
                finalized = True
    if finalized:
        notify_listeners(task_id, 'error', {'message': reason})
        save_tasks_to_disk()
    hit = False
    try:
        hit = bool(get_fx_cancel_flag().cancel_request(task_id))
    except Exception:
        pass
    FX_CONTROL.audit('task.cancel', task_id,
                     {'reason': reason, 'active_request_hit': hit}, actor=actor)
    return finalized or hit


def _fx_timeout_watchdog():
    """执行超时踢出：发取消信号；若超限过久线程硬卡死，强行释班 Slot 保障号池可用。"""
    while True:
        time.sleep(5)
        try:
            for row in FX_CONTROL.overdue_active():
                task_id = row.get('task_id')
                if not task_id:
                    continue
                elapsed = float(row.get('elapsed_seconds') or 0)
                limit = float(FX_CONTROL.limits().get('task_timeout_seconds') or 300)
                reason = (f"FX 任务执行超过 {limit}s （已运行 {elapsed}s），已按超时策略取消")
                FX_CONTROL.audit('queue.exec_timeout', task_id,
                                 {'elapsed_seconds': elapsed,
                                  'kind': row.get('kind')}, actor='watchdog')
                _cancel_fx_task(task_id, reason, actor='watchdog')
                # 超过限额 10 秒后底层仍未能放行槽位，判定为底线硬卡死，强制剔除以防全号池自锁
                if limit > 0 and elapsed > limit + 10:
                    log('WARN', 'FX_WATCHDOG', f"任务 {task_id} 超时后 10s 内未自行退出临界区，看门狗执行强行槽位释放")
                    FX_CONTROL.force_release_active(task_id=task_id, actor='watchdog')
        except Exception:
            continue


def bootstrap_fx_runtime():
    """启动时装配 FX 运行时的宿主钩子。幂等，测试里也可以直接调。"""
    try:
        from integrations.google_fx.utils import browser_gate, account_binding
        browser_gate.install(_fx_browser_gate)
        account_binding.install_pin_resolver(_fx_queue_account_pin)
    except Exception as e:
        log('WARN', 'FX', f"Google FX 运行时钩子安装失败，旁路动作将不受队列约束: {e}")
    # 换 IP 全局关停（server_common 的 _IP_ROTATE_DISABLED）过去只在每次生成前
    # 由 apply_google_fx_runtime_overrides 写入。启动时先写一次，任何路径都不会
    # 在"阈值还没写上"的窗口里连浏览器；/api/google-fx/status 也据此上报真实值。
    try:
        FX_CONFIG.migrate_deprecated_values()
        apply_google_fx_runtime_overrides(SERVER_CONFIG)
        apply_direct_env(FX_CONFIG.current())
        FX_CONFIG.ensure_baseline()
    except Exception as e:
        log('WARN', 'FX', f"Google FX 运行时环境变量初始化失败: {e}")
    if not _FX_WATCHDOG_STARTED.is_set():
        _FX_WATCHDOG_STARTED.set()
        threading.Thread(target=_fx_timeout_watchdog, name='fx-timeout-watchdog',
                         daemon=True).start()


def generate_videos_worker(task_id, config, title, prompt_block, target_slots, override_flagged=False):
    t = get_or_create_task(task_id)
    set_project_key_context(config.get('_project_key') if isinstance(config, dict) else None)
    set_log_context(task_id)
    log('INFO', 'VIDEOS', f"开始视频任务{f'（子集 {target_slots}）' if target_slots else '（整单）'}", title=title)
    try:
        from prompt_pipeline import start_accounting, stop_and_get_accounting
        start_accounting()
        def progress_cb(stage, details):
            if stage == 'cancel_check':
                return t["cancel_event"].is_set()
            if t["cancel_event"].is_set():
                raise GenerationCancelled("Generation cancelled by user")
            with ACTIVE_TASKS_LOCK:
                t["events"].append((stage, details))
            notify_listeners(task_id, stage, details)

        progress_cb('queue', {'message': '视频生成请求已加入队列，正在等待排队生成...'})

        with _fx_browser_slot(task_id, 'videos', t['cancel_event']):
            result = generate_video_sequence(
                config, title, prompt_block,
                on_progress=progress_cb,
                target_slots=target_slots,
                override_flagged=override_flagged
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
                # （merge_project_videos）同样按预期缺失处理；'skipped_bridge_hold'
                # 已停用，仅为兼容旧 manifest 保留
                if manifest_video_slots[slot].get('status') not in ('success', 'skipped_cut', 'skipped_bridge_hold'):
                    has_failures = True
                    break

            if has_failures:
                progress_cb('merge_skip', {'message': '检测到存在生成失败或缺失的视频片段，已跳过自动合并视频。'})
            else:
                # Try to automatically merge videos
                try:
                    merge_speed = config.get('_merge_speed', 2)
                    progress_cb('merge_start', {'message': f'正在自动以 {merge_speed}x 速率合并视频...'})
                    project_dir = _get_project_dir(title)
                    merged_info = merge_project_videos(project_dir, speed=merge_speed)
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
        finalize = False
        with ACTIVE_TASKS_LOCK:
            if t["status"] == "running":
                t["status"] = "cancelled"
                t["error"] = "用户取消了视频生成"
                t["events"].append(('error', {'message': '用户取消了视频生成'}))
                finalize = True
        if finalize:
            notify_listeners(task_id, 'error', {'message': '用户取消了视频生成'})
        log('WARN', 'VIDEOS', "视频任务已被用户取消", title=title)
        # 取消不关浏览器（2026-07-26）：见 /api/compose-cancel 里的同款说明。
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
        set_project_key_context(None)


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

        # 配额耗尽不再自动切兜底模型（原先切 gpt-image-2）：封面是整单的门面，
        # 换模型出来的画风与帧序列对不上，宁可明确报错等补额度后重出。
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


class _QuietConnResetMixin:
    """把"客户端悄悄掐掉空闲 keep-alive 连接"这类噪音从满屏 traceback 降成一行。

    HTTP/1.1 之后浏览器会留着若干条空闲连接备用，用完/离开页面时直接 RST。服务端
    正阻塞在 handle_one_request 的 readline 上，于是收到 WinError 10054/10053，
    socketserver 默认把它当成"处理请求时出异常"打整段 traceback——server.log 里
    实测 42 段全是这一种，纯噪音，却把真正的报错顶出了视野。
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            log('DEBUG', 'HTTP', f'客户端断开空闲连接: {type(exc).__name__}')
            return
        super().handle_error(request, client_address)


class DualStackHTTPServer(_QuietConnResetMixin, ThreadingMixIn, HTTPServer):
    # ThreadingMixIn so a long-running /api/compose call (the skill can take 60-180s)
    # does not block static file serving or library reads in the same browser session.
    address_family = socket.AF_INET6
    # Windows 上 SO_REUSEADDR 允许第二个实例静默绑定同一端口——这正是文档记录过的
    # “重复实例越积越多”的根源；显式 False + SO_EXCLUSIVEADDRUSE 让第二次启动立刻报错。
    allow_reuse_address = (sys.platform != 'win32')
    daemon_threads = True
    # listen backlog：基类默认只有 5。一次页面加载要并发拉 20 个 js + 8 个 css，
    # Chromium 每源开 6 条连接，再叠上 SSE 与另一个标签页，瞬时未 accept 的连接很容易
    # 超过 5，超出的会被内核直接拒掉，表现就是随机某个 <script> 加载失败。
    # tests/test_slot_grid_render.py 和 test_frames_cancel_settles_slots.py 里的静态
    # 桩服务早就按同样理由写了 128，唯独真正对外服务的这两个类一直漏掉。
    request_queue_size = 128

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class IPv4HTTPServer(_QuietConnResetMixin, ThreadingMixIn, HTTPServer):
    """IPv4 回退：本机禁用 IPv6 时 DualStackHTTPServer 无法启动。"""
    address_family = socket.AF_INET
    allow_reuse_address = (sys.platform != 'win32')
    daemon_threads = True
    request_queue_size = 128  # 同 DualStackHTTPServer，理由见那边注释

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
    # HTTP/1.0（此前的隐式默认值）不支持持久连接：每个页面加载要拉 14 个 js +
    # 8 个 css，每一个都得单独 TCP 三次握手再关闭。本机高频开关连接在 Windows
    # 上很容易撞上杀软/防火墙的回环拦截（WinError 10053 连接被中止），表现为
    # "部分脚本加载失败"红条，且频率与并发连接数正相关（server.log 已实锤
    # 225 次 10053/超时）。升级到 HTTP/1.1 后同一浏览器标签只维持约 6 条常驻
    # 连接并复用，把每次加载的连接次数从 20+ 降到个位数。
    # 前提：所有自定义响应体都必须带 Content-Length（否则复用连接的下一个请求
    # 会读不到正确的响应边界而卡死）——_send_json/do_OPTIONS/POST 兜底 404 已
    # 补齐；SSE 是无边界流，改为显式 close_connection 避免连接复用歧义。
    protocol_version = 'HTTP/1.1'

    # 访问日志此前无差别 print(f"LOG: ...")：实测 server.log 里 81% 的行是它产生的
    # （43,284 行中 35,249 行），且绝大多数是 "GET /outputs/xxx.mp4 304"——播放器
    # 反复回源验证缓存的正常往返，对"生成得怎么样"零信息量，却把真正有用的行冲得
    # 找不到。现在按状态码分级：4xx/5xx 里真正的异常照常留痕，正常往返只在显式
    # 打开 logHttpAccess 时才记（见 server_common.http_access_logging）。
    def _log_access(self, msg):
        if http_access_logging():
            log('INFO', 'HTTP', msg)

    def log_request(self, code='-', size='-'):
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        path = (self.path or '').split('?')[0]
        line = f'"{getattr(self, "requestline", "-")}" {code} {size}'
        if status >= 500:
            log('ERROR', 'HTTP', line)
        elif status >= 400 and not (status == 404 and path.startswith('/outputs/')):
            # /outputs 下的 404 例外：帧/视频文件名是确定性的，前端会在文件生成
            # 之前就来取，那个 404 是预期中的"还没好"，不是故障（end_headers 里
            # 那段缓存压制注释说的是同一件事）。
            log('WARN', 'HTTP', line)
        else:
            self._log_access(line)

    def log_error(self, format, *args):
        # send_error() 会先 log_error("code %d, message %s") 再走 send_response()，
        # 而后者已经经由 log_request 记过一条带状态码、且分好级的了——原样保留等于
        # 同一个 404 记两遍。"Request timed out" 同理：HTTP/1.1 持久连接到点自然
        # 过期，不是故障（实测 328 次全是这个）。这两类并进访问日志，其余按 WARN。
        try:
            msg = format % args
        except Exception:
            msg = str(format)
        if msg.startswith('code ') or msg.startswith('Request timed out'):
            self._log_access(msg)
        else:
            log('WARN', 'HTTP', msg)

    def log_message(self, format, *args):
        # http.server 里只有 log_request/log_error 会调到这里，两者都已单独覆盖。
        # 留一个兜底覆盖，是为了不让基类实现把无级别的裸文本直接甩给 stderr。
        try:
            msg = format % args
        except Exception:
            msg = str(format)
        self._log_access(msg)

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
            # 帧重试会原地覆盖同名 img_XXX.webp，画廊删除会移除文件；没有缓存头时
            # 浏览器按启发式缓存直接复用旧图——表现为"新帧生成了 UI 还是旧图"、
            # "已删除的帧仍能显示"。no-cache = 每次使用前带 If-Modified-Since 回源
            # 验证：未变走 304（只有头部往返，不重新下载，不会回到旧的重复全量
            # 下载问题），覆盖后立即拿新图，删除后立即 404。
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError) as e:
            # 客户端在响应写回前就断了（慢接口上很常见：积分探测要几十秒，用户
            # 中途刷新页面）。这时候连"再回一个 500"都写不出去，只会让 do_POST 的
            # except 分支再抛一次、被 socketserver 打成满屏 traceback。响应已经
            # 送不到任何人手里了，记一行就够。
            self.close_connection = True
            print(f"客户端已断开，响应未送达 ({self.command} {self.path}): {type(e).__name__}")

    def _gate(self, with_rate=False, rate_action='default'):
        """访问码（及可选频控）门禁。未设置访问码时恒放行。返回 False 时已回写响应。
        rate_action：频控桶名，同一 ip 下不同 action 各有独立配额（见 rate_ok）。"""
        if not access_ok(self):
            self._send_json({'status': 'error', 'message': '访问码无效或缺失'}, status=401)
            return False
        if with_rate and not rate_ok(_client_ip(self), rate_action):
            self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
            return False
        return True

    def _read_json_body(self):
        raw = getattr(self, '_body_bytes', None)
        if raw is None:
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length) if content_length else b''
        raw = raw or b'{}'
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
        # SSE bodies have no Content-Length (unbounded). Under HTTP/1.1 keep-alive
        # the server would otherwise try to read a *second* request off this same
        # socket once the stream ends — force this one connection closed instead,
        # matching the pre-existing "runs until the client disconnects" behavior.
        self.close_connection = True

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
        self.send_header('Content-Length', '0')
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
        elif path == '/api/deleted_slots':
            # 这单删过哪些拍、哪些还能撤销。数据源是 /api/delete_slot 写下的
            # .deleted_slots/<id>/ 快照目录，见 /api/restore_slot。
            if not self._gate():
                return
            title = query.get('title', [''])[0]
            if not title:
                self._send_json({'error': 'Missing title'}, status=400)
                return
            project_dir = _get_project_dir(title)
            root = os.path.join(project_dir, '.deleted_slots')
            items = []
            try:
                if os.path.isdir(root):
                    current_fp = manifest_fingerprint(read_manifest(project_dir) or {})
                    for name in sorted(os.listdir(root), reverse=True):
                        # 恢复点（*_restorepoint_of_*）是恢复操作自己留的底，
                        # 不是可撤销的删除，不列进来
                        if not re.fullmatch(r'\d{8}_\d{6}_\d+_slot_\d{3}', name):
                            continue
                        d = os.path.join(root, name)
                        if not os.path.isdir(d):
                            continue
                        state, removed = {}, {}
                        for fname, target in (('state.json', 'state'), ('removed.json', 'removed')):
                            fp = os.path.join(d, fname)
                            if os.path.isfile(fp):
                                try:
                                    with open(fp, 'r', encoding='utf-8') as f:
                                        loaded = json.load(f) or {}
                                except (json.JSONDecodeError, OSError):
                                    loaded = {}
                                if target == 'state':
                                    state = loaded
                                else:
                                    removed = loaded
                        expected_fp = state.get('manifest_after_fingerprint')
                        items.append({
                            'id': name,
                            'sequence': state.get('sequence') or removed.get('sequence'),
                            'created_at': state.get('created_at'),
                            'restored_at': state.get('restored_at'),
                            # 早于本次改造的快照没有 state.json，恢复仍可进行，
                            # 只是没法判断这单在删除之后有没有被改动过
                            'has_fingerprint': bool(expected_fp),
                            'diverged': bool(expected_fp) and expected_fp != current_fp,
                            'image_prompt': (removed.get('image_prompt') or '')[:160],
                            'video_prompt': (removed.get('video_prompt') or '')[:160],
                            'archived_frame_slots': state.get('archived_frame_slots') or [],
                            'archived_video_slots': state.get('archived_video_slots') or [],
                        })
                self._send_json({'status': 'ok', 'snapshots': items})
            except OSError as e:
                self._send_json({'error': str(e)}, status=500)
        elif path == '/api/logs/stream':
            # 服务日志包含内部路径/任务详情，必须过门禁（EventSource 用 ?access_code= 传码）
            if not self._gate():
                return
            send_event = None
            stop_evt = None
            try:
                send_event, stop_evt = self._open_sse_stream()
                
                # Send the last 100 lines first.
                # 全程二进制读再统一 decode，不用文本模式 seek()——文本模式的
                # seek() 只接受 tell() 给出的"不透明游标"，喂一个裸字节偏移量是
                # 未定义行为：偏移量一旦落在某个多字节 UTF-8 字符中间，
                # errors='replace' 会悄悄垫上"�"，前端看到的就是一串乱字符
                # （不是 print() 端输出了乱码，是这里读的时候自己读岔了）。
                # 只取文件末尾一段，不再整份 read()：日志上限是 8MB，而这里要的
                # 永远只是最后 100 行，每次连接/断线重连都把整份读进内存纯属浪费。
                if os.path.exists(_LOG_PATH):
                    try:
                        with open(_LOG_PATH, 'rb') as f:
                            size = os.fstat(f.fileno()).st_size
                            if size > _LOG_TAIL_BYTES:
                                f.seek(size - _LOG_TAIL_BYTES)
                                raw = f.read()
                                # 起点是裸字节偏移，多半落在某行（甚至某个多字节
                                # 字符）中间：丢掉第一段残行，剩下的就是干净的整行
                                nl = raw.find(b'\n')
                                raw = raw[nl + 1:] if nl != -1 else raw
                            else:
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
                        if full_result.get("model"):
                            result_summary["model"] = full_result["model"]
                        if full_result.get("project_key"):
                            result_summary["project_key"] = full_result["project_key"]
                        # 任务卡的镜头数必须用最终解析出的槽位数，不能用规划阶段
                        # beats_count；流水线可能另外追加 reward/HERO 镜头。
                        if full_result.get("image_count") is not None:
                            result_summary["image_count"] = full_result["image_count"]
                        if full_result.get("video_count") is not None:
                            result_summary["video_count"] = full_result["video_count"]
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
            # 技能契约缺失也在这里上报：它只影响生成质量、不影响接口可用性，因此过去
            # 只写进启动日志——而日常从浏览器用的人根本不看那个终端，等于没有告知。
            self._send_json({
                'server_managed': SERVER_MANAGED,
                'needs_access_code': bool(ACCESS_CODE),
                'skill_contract': skill_contract_report(),
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
            # 而不是静默降级为 [](同 /api/ledger 的整库清零教训)。附带 cap 与
            # 归档条数,供前端渲染"N / 上限 · 已归档 M"徽标(archived_count 读取
            # 失败时省略,不拖垮主库这条请求)
            if not self._gate():
                return
            refs = load_trend_refs()
            if refs is None:
                self._send_json({'error': '联网参考案例库文件读取失败'}, status=500)
                return
            archive = load_trend_refs_archive()
            self._send_json({
                'status': 'ok',
                'refs': list(reversed(refs)),
                'cap': TREND_REFS_CAP,
                'archived_count': len(archive) if archive is not None else None,
            })
        elif path == '/api/trend-refs/archive':
            # 归档库(软上限淘汰出主库、未使用过的旧参考)全量返回,新→旧
            if not self._gate():
                return
            archive = load_trend_refs_archive()
            if archive is None:
                self._send_json({'error': '联网参考归档文件读取失败'}, status=500)
                return
            self._send_json({'status': 'ok', 'refs': list(reversed(archive))})
        elif path == '/api/account-pool':
            # 视频生成号池:列出已添加的 Flow 账号(命名/备注/已知积分/上次检查时间/
            # 是否禁用)。池子为空时前端应显示"未启用号池"，行为回退到手动单选。
            if not self._gate():
                return
            try:
                pool = _get_account_pool()
                sort_by = query.get('sort_by', [None])[0] if 'sort_by' in query else None
                sort_order = query.get('sort_order', ['desc'])[0] if 'sort_order' in query else 'desc'
                self._send_json({'status': 'ok', 'accounts': pool.list_accounts(sort_by=sort_by, sort_order=sort_order)})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/proxy-pool':
            # 代理号池：出口代理列表（密码只报 has_password，不回传明文）。
            # 与 /api/account-pool 对称：那个是"用哪个号"，这个是"从哪个出口走"。
            if not self._gate():
                return
            try:
                pool = _get_proxy_pool()
                self._send_json({'status': 'ok', 'proxies': pool.list_proxies(),
                                 'summary': pool.summary()})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/account-pool/adspower-profiles':
            # 本机 AdsPower 里所有浏览器环境,供"添加账号"下拉框选择候选 user_id
            if not self._gate():
                return
            try:
                pool = _get_account_pool()
                self._send_json({'status': 'ok', 'profiles': pool.list_adspower_profiles()})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/google-fx/status':
            # 控制台聚合健康快照：只探测端口和内存/状态文件，不启动或关闭浏览器。
            # 带 1.5s TTL 缓存（多标签页/多次刷新压成一次实际计算）；?force=1 绕过。
            if not self._gate():
                return
            try:
                force = query.get('force', ['0'])[0] in ('1', 'true')
                self._send_json(google_fx_status_snapshot(force=force))
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/control':
            if not self._gate():
                return
            self._send_json({
                'status': 'ok',
                'queue': FX_CONTROL.snapshot(),
                'audit': FX_CONTROL.recent_audit(30),
            })
        elif path == '/api/google-fx/audit':
            # 队列/控制/取消/配置的审计流。此前只写进 runtime/fx_audit.jsonl，
            # 控制台完全没有展示入口，"谁在什么时候暂停了服务、哪个任务排了多久"
            # 只能自己去翻文件。
            if not self._gate():
                return
            try:
                self._send_json({
                    'status': 'ok',
                    'rows': FX_CONTROL.recent_audit(
                        int(query.get('limit', ['80'])[0] or 80),
                        action_prefix=(query.get('prefix', [''])[0] or None),
                        task_id=(query.get('task_id', [''])[0] or None),
                    ),
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/google-fx/config':
            if not self._gate():
                return
            self._send_json({
                'status': 'ok',
                'config': FX_CONFIG.current(),
                'schema': FX_CONFIG.schema(),
                'versions': FX_CONFIG.versions(int(query.get('limit', ['20'])[0] or 20)),
                'audit': FX_CONTROL.recent_audit(20, action_prefix='config.'),
            })
        elif path == '/api/google-fx/logs':
            # FX 专属日志尾巴：/api/logs/stream 是全量流，排查 FX 问题时噪音太大。
            # 支持按 task_id 与关键字过滤，控制台内嵌 tail 直接用。
            if not self._gate():
                return
            try:
                self._send_json({
                    'status': 'ok',
                    'lines': _fx_log_tail(
                        task_id=(query.get('task_id', [''])[0] or ''),
                        keyword=(query.get('q', [''])[0] or ''),
                        limit=int(query.get('limit', ['120'])[0] or 120),
                        fx_only=query.get('all', ['0'])[0] not in ('1', 'true'),
                    ),
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/google-fx/manual':
            # 控制台的「使用说明书」入口读它。文档随代码走（docs/ 下），
            # 不做成内嵌 HTML：这样文档能被 git diff 审阅，也能单独阅读。
            if not self._gate():
                return
            try:
                manual = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'docs', 'google_fx_console_manual.md')
                with open(manual, 'r', encoding='utf-8') as handle:
                    self._send_json({'status': 'ok', 'markdown': handle.read(),
                                     'path': 'docs/google_fx_console_manual.md'})
            except FileNotFoundError:
                self._send_json({'status': 'error',
                                 'message': '说明书文件缺失：docs/google_fx_console_manual.md'},
                                status=404)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/google-fx/captures':
            # 失败现场清单（截图/DOM/选择器探针结果）
            if not self._gate():
                return
            try:
                from integrations.google_fx.utils import forensics
                self._send_json({'status': 'ok', 'enabled': forensics.is_enabled(),
                                 'root': str(forensics.DEBUG_ROOT),
                                 'captures': forensics.list_captures(
                                     int(query.get('limit', ['60'])[0] or 60))})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
        elif path == '/api/google-fx/capture-file':
            # 下载单个现场文件。路径解析在 forensics 里做越界校验（防目录穿越）。
            if not self._gate():
                return
            try:
                from integrations.google_fx.utils import forensics
                resolved = forensics.resolve_capture_file(
                    query.get('id', [''])[0], query.get('file', [''])[0])
                if not resolved:
                    self._send_json({'status': 'error', 'message': '文件不存在或路径越界'}, status=404)
                    return
                ctype = mimetypes.guess_type(resolved)[0] or 'application/octet-stream'
                with open(resolved, 'rb') as handle:
                    payload = handle.read()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)
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
        # 持久连接（HTTP/1.1 keep-alive）下，若某个分支提前返回（如 _gate() 拒绝）
        # 或落到最后未匹配路径的 404 兜底，都不会去读请求体——残留在 socket 里
        # 的字节会被当成下一个请求的开头，直接冲掉同一条连接上后续复用的请求
        # （实测复现：POST 未匹配路径带 body 后，同连接下一个 GET 变成 501）。
        # 统一在分发前读一次、缓存到 self._body_bytes，各分支（含多段表单）从
        # 缓存取，不再各自现读 rfile。
        # hasattr 兜底：单测里有一批用 object.__new__(SparkRequestHandler) 直接
        # 拼一个只够跑单个分支的假 handler（不走真实 socket，没有 .headers/.rfile，
        # _read_json_body 也被单测自己打桩替换掉了），这里没有真实连接可读也不需要读。
        content_length = int(self.headers.get('Content-Length', 0) or 0) if hasattr(self, 'headers') else 0
        self._body_bytes = self.rfile.read(content_length) if content_length else b''

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
                    # 同一创意内部也必须自洽：浏览器旧页曾把 9 格 prompt_slots 与
                    # 10 帧 frameRun 一起回写，随后删除接口按较小的提示词编号操作，
                    # 最终误删了另一拍。带 frameRun 的记录若两边数量不同，说明页面
                    # 已过期或局部状态错位，整条写入必须失败并要求刷新。
                    inconsistent = []
                    if isinstance(data, list):
                        for idea in data:
                            if not isinstance(idea, dict):
                                continue
                            slots = idea.get('prompt_slots') or {}
                            slot_images = slots.get('images') if isinstance(slots, dict) else None
                            frame_run = idea.get('frameRun') or {}
                            run_frames = frame_run.get('frames') if isinstance(frame_run, dict) else None
                            if not isinstance(slot_images, list) or not isinstance(run_frames, list):
                                continue
                            prompt_count = len({item.get('index') for item in slot_images
                                                if isinstance(item, dict) and
                                                isinstance(item.get('index'), int)})
                            frame_count = len({item.get('sequence') or item.get('slot')
                                               for item in run_frames if isinstance(item, dict) and
                                               isinstance(item.get('sequence') or item.get('slot'), int)})
                            if prompt_count and frame_count and prompt_count != frame_count:
                                inconsistent.append({
                                    'id': idea.get('id'),
                                    'prompt_count': prompt_count,
                                    'frame_count': frame_count,
                                })
                    if inconsistent:
                        self._send_json({
                            'status': 'rejected',
                            'message': '创意页面槽位状态已过期，已阻止覆盖；请刷新页面后重试。',
                            'refresh_required': True,
                            'inconsistent_ideas': inconsistent,
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
            if not self._gate(with_rate=True, rate_action='trend_search'):
                return
            try:
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                new_refs = refresh_trend_refs(config)
                refs = load_trend_refs()
                if refs is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库文件读取失败'}, status=500)
                    return
                archive = load_trend_refs_archive()
                self._send_json({
                    'status': 'ok',
                    # 本批搜到的条目 id(文本与旧条目相同则 id 相同,前端据此算真正新增数)
                    'added': [r['id'] for r in new_refs],
                    'refs': list(reversed(refs)),
                    'cap': TREND_REFS_CAP,
                    'archived_count': len(archive) if archive is not None else None,
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/trend-refs/delete':
            # 案例库按 id 批量删除(显式 id 列表即确认过的意图,同 /api/ledger/delete)。
            # archive: true 时对归档库操作(永久删除已淘汰条目),否则对主库操作。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                ids = body.get('ids')
                if not isinstance(ids, list) or not ids:
                    self._send_json({'status': 'error', 'message': '缺少要删除的 ids'}, status=400)
                    return
                result = delete_trend_refs(ids, archive=bool(body.get('archive')))
                if result['remaining'] is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库文件读取失败'}, status=500)
                    return
                self._send_json({'status': 'ok', 'deleted': result['deleted']})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/control':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                action = str(body.get('action') or '').strip()
                actor = _client_ip(self)
                if action == 'reprioritize':
                    snapshot = FX_CONTROL.reprioritize(
                        str(body.get('task_id') or ''), body.get('priority', 0), actor=actor)
                elif action == 'limits':
                    # 并发度 / 分类配额 / 执行超时 / 排队超时
                    snapshot = FX_CONTROL.set_limits(body.get('limits') or {}, actor=actor)
                elif action == 'pin_account':
                    # 给排队中的任务定向绑定账号（进临界区时生效）
                    snapshot = FX_CONTROL.pin_account(
                        str(body.get('task_id') or ''), body.get('user_id') or '', actor=actor)
                elif action == 'clear_orphaned':
                    FX_CONTROL.clear_orphaned(actor=actor)
                    snapshot = FX_CONTROL.snapshot()
                elif action in ('force_release_slot', 'release_active', 'cancel_task'):
                    tid = body.get('task_id')
                    try:
                        cancel_flag = get_fx_cancel_flag()
                        if tid:
                            cancel_flag.cancel_request(str(tid))
                    except Exception:
                        pass
                    count = FX_CONTROL.force_release_active(task_id=tid, actor=actor)
                    if tid and tid in ACTIVE_TASKS:
                        with ACTIVE_TASKS_LOCK:
                            t = ACTIVE_TASKS[tid]
                            t["status"] = "cancelled"
                            t["error"] = "用户强行释放了底层任务槽位"
                        save_tasks_to_disk()
                    snapshot = FX_CONTROL.snapshot()
                else:
                    snapshot = FX_CONTROL.set_mode(action, actor=actor)
                google_fx_status_snapshot(force=True)  # 让控制台下一次轮询立刻看到新状态
                self._send_json({'status': 'ok', 'queue': snapshot})
            except KeyError as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=404)
            except (TypeError, ValueError) as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/config':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                actor = _client_ip(self)
                action = str(body.get('action') or '').strip()
                if action in ('rollback', 'restore', 'redo'):
                    # 版本栈：可以连续往前回退，也可以重做（原实现只能退一步，
                    # 而且第二次点击是重复套用同一份 before = 空操作）
                    outcome = FX_CONFIG.restore(
                        version_id=body.get('version_id') or None,
                        actor=actor,
                        direction='forward' if action == 'redo' else 'back')
                else:
                    outcome = FX_CONFIG.save(body.get('patch'), actor=actor)
                self._send_json({
                    'status': 'ok',
                    'config': outcome['config'],
                    'changed': outcome.get('changed') or {},
                    'version': outcome.get('version'),
                    'versions': FX_CONFIG.versions(20),
                    # 命中 hot=False 的字段时如实告诉用户要重启，别谎称热生效
                    'restart_required': sorted(
                        key for key in (outcome.get('changed') or {})
                        if not FX_CONFIG_SPEC[key].get('hot')),
                    # 写进去了、也热生效了，但被别的配置项压住跑不起来的字段
                    'inert': _inert_config_notes(outcome.get('config') or {}),
                })
            except KeyError as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=404)
            except ValueError as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/selftest':
            # 分级自检：L0 只读 / L1 连浏览器不提交 / L2 真实最小提交。
            # L1/L2 会占用浏览器，所以走 FX_CONTROL 队列 + 频控。
            if not self._gate(with_rate=True, rate_action='fx_selftest'):
                return
            try:
                body = self._read_json_body()
                level = int(body.get('level', 0) or 0)
                actor = _client_ip(self)
                if level >= 1:
                    allowed, message = FX_CONTROL.admission()
                    if not allowed:
                        self._send_json({'status': 'error', 'code': 'FX_NOT_ACCEPTING',
                                         'message': message}, status=503)
                        return
                FX_CONTROL.audit('diagnostics.selftest', details={'level': level}, actor=actor)
                from integrations.google_fx.services import google_fx_diagnostics
                outcome = google_fx_diagnostics.run_selftest(
                    level=level, user_id=(body.get('user_id') or '').strip() or None)
                FX_CONTROL.audit('diagnostics.selftest_done',
                                 details={'level': level, 'status': outcome['status'],
                                          'failed_step': outcome.get('failed_step')}, actor=actor)
                self._send_json({'status': 'ok', 'selftest': outcome})
            except ValueError as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/selector-probe':
            # 连一次浏览器只跑选择器探针（不提交、不改配置）
            if not self._gate(with_rate=True, rate_action='fx_selector_probe'):
                return
            try:
                body = self._read_json_body()
                allowed, message = FX_CONTROL.admission()
                if not allowed:
                    self._send_json({'status': 'error', 'code': 'FX_NOT_ACCEPTING',
                                     'message': message}, status=503)
                    return
                FX_CONTROL.audit('diagnostics.selector_probe', actor=_client_ip(self))
                from integrations.google_fx.services import google_fx_diagnostics
                self._send_json(google_fx_diagnostics.probe_selectors_live(
                    user_id=(body.get('user_id') or '').strip() or None,
                    deep=bool(body.get('deep'))))
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/google-fx/selector-stats/reset':
            # 修好选择器之后清掉历史 miss/兜底记录，否则漂移警告会永远挂着
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                from integrations.google_fx.utils import selector_stats
                removed = selector_stats.reset(body.get('family') or None)
                FX_CONTROL.audit('diagnostics.selector_stats_reset',
                                 details={'family': body.get('family') or 'ALL',
                                          'removed': removed}, actor=_client_ip(self))
                google_fx_status_snapshot(force=True)
                self._send_json({'status': 'ok', 'removed': removed})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool':
            # 号池新增/改名/改备注: {user_id, name, note}（同一 upsert 入口，
            # name=账号命名留空回退邮箱，note=备注按传入值直接覆盖）
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                pool = _get_account_pool()
                entry = pool.add_account(user_id, body.get('name') or '', body.get('note') or '', serial_number=body.get('serial_number') or '')
                self._send_json({'status': 'ok', 'account': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/import-all':
            # 一键把本机 AdsPower 里所有浏览器环境并入号池（已在池子里的原样跳过，
            # 不覆盖已改过的命名/备注，也不重置积分缓存与禁用/冷却状态）
            if not self._gate():
                return
            try:
                pool = _get_account_pool()
                result = pool.import_adspower_profiles()
                self._send_json({
                    'status': 'ok',
                    'added': len(result.get('added') or []),
                    'skipped': len(result.get('skipped') or []),
                    'total': result.get('total', 0),
                    'accounts': pool.list_accounts(),
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/delete':
            # 号池移除账号（显式 user_id 即确认过的意图，同 /api/ledger/delete 约定）
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                pool = _get_account_pool()
                pool.remove_account(user_id)
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/toggle':
            # 启用/禁用账号: {user_id, disabled}
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                pool = _get_account_pool()
                entry = pool.set_disabled(user_id, bool(body.get('disabled')))
                if entry is None:
                    self._send_json({'status': 'error', 'message': '账号不存在'}, status=404)
                    return
                self._send_json({'status': 'ok', 'account': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/refresh':
            # 强制真实探测一次积分（打开浏览器读 UI，比较慢，前端要有 loading 态）
            if not self._gate(with_rate=True, rate_action='account_refresh'):
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                # 探针现在也排 FX_CONTROL 队列（不再抢正在生成的浏览器），但生成任务
                # 可能占用浏览器好几分钟——那样这个 HTTP 请求就会一直挂着。所以生成
                # 任务占着浏览器时直接回 409 让用户稍后再点，而不是让请求静默长挂。
                #
                # ⚠️ 2026-07-27：这里原本对**任何**活跃任务都回 409，包括另一个积分
                # 探针。于是一个卡住的探针（进不去工作台的账号能占好几分钟）会让所有
                # 账号的"刷新积分"连续 409，功能整体看上去就是坏的。探针现在自带墙钟
                # 预算（PROBE_BUDGET_SECONDS）且排队等待有上限，排在另一个探针后面是
                # 有界的，所以只对生成类任务保留 409。
                queue = FX_CONTROL.snapshot()
                if queue.get('processing_paused'):
                    self._send_json({
                        'status': 'error', 'code': 'FX_PAUSED',
                        'message': 'Google FX 队列处于暂停模式，积分探测拿不到浏览器；请先在控制台恢复处理',
                    }, status=409)
                    return
                active_rows = queue.get('active_list') or []
                blocking = [row for row in active_rows
                            if str(row.get('kind')) not in FX_BOUNDED_BYPASS_KINDS]
                if blocking:
                    row = blocking[0]
                    busy = row.get('task_id') or '其它任务'
                    elapsed = row.get('elapsed_seconds')
                    elapsed_text = f'（已运行 {elapsed:.0f}s）' if isinstance(elapsed, (int, float)) else ''
                    self._send_json({
                        'status': 'error', 'code': 'FX_BUSY',
                        'message': (f'浏览器正被 {busy} {elapsed_text}占用，'
                                    f'积分探测需要同一个浏览器，请等它结束后再刷新'),
                    }, status=409)
                    return
                # 同一个账号的探测已经在跑：再排一个只会白等一轮浏览器。
                if any(str(row.get('task_id')) == f'credit_probe_{user_id}' for row in active_rows):
                    self._send_json({
                        'status': 'error', 'code': 'FX_PROBE_RUNNING',
                        'message': '该账号的积分探测正在进行中，请等这一轮出结果',
                    }, status=409)
                    return
                pool = _get_account_pool()
                entry = pool.refresh_credit(user_id, force=True)
                if entry is None:
                    self._send_json({'status': 'error', 'message': '账号不存在'}, status=404)
                    return
                if entry.get('last_probe_status') == 'blocked':
                    # 排在别的浏览器任务后面没等到——账号本身没问题，别报成探测失败。
                    self._send_json({
                        'status': 'error', 'code': 'FX_BUSY',
                        'message': entry.get('last_probe_error') or '浏览器忙，未能开始积分探测',
                        'account': entry,
                    }, status=409)
                    return
                if entry.get('last_probe_status') == 'failed':
                    self._send_json({
                        'status': 'error', 'code': 'CREDIT_PROBE_FAILED',
                        'message': entry.get('last_probe_error') or '未能读取可信积分余额',
                        'account': entry,
                    }, status=422)
                    return
                self._send_json({'status': 'ok', 'account': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/clear-cooldown':
            # 只解除冷却标记；积分与检查时间保持原值，避免伪造账号健康状态。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                entry = _get_account_pool().clear_cooldown(user_id)
                if entry is None:
                    self._send_json({'status': 'error', 'message': '账号不存在'}, status=404)
                    return
                self._send_json({'status': 'ok', 'account': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/account-pool/close-browser' or path == '/api/account-pool/close':
            # 关闭指定 user_id 的 AdsPower 浏览器实例
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                user_id = str(body.get('user_id') or '').strip()
                if not user_id:
                    self._send_json({'status': 'error', 'message': '缺少 user_id'}, status=400)
                    return
                pool = _get_account_pool()
                success, msg = pool.close_browser(user_id)
                if not success:
                    self._send_json({'status': 'error', 'message': msg}, status=500)
                    return
                self._send_json({'status': 'ok', 'message': msg, 'user_id': user_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool':
            # 代理号池新增/编辑：{proxy_id?, host, port, proxy_type, user, password,
            # label, note}。带 proxy_id 即编辑；编辑时不传 password 表示沿用原密码
            # （列表接口从不回传明文，否则改个备注就会把密码清空）。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                pool = _get_proxy_pool()
                entry = pool.add_proxy(
                    host=body.get('host'), port=body.get('port'),
                    proxy_type=body.get('proxy_type') or 'http',
                    user=body.get('user') or '', password=body.get('password') or '',
                    label=body.get('label') or '', note=body.get('note') or '',
                    proxy_id=body.get('proxy_id') or '',
                    keep_password=bool(body.get('proxy_id')),
                )
                self._send_json({'status': 'ok', 'proxy': entry})
            except (ValueError, KeyError) as e:
                self._send_json({'status': 'error', 'message': str(e).strip("'")}, status=400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool/delete':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                proxy_id = str(body.get('proxy_id') or '').strip()
                if not proxy_id:
                    self._send_json({'status': 'error', 'message': '缺少 proxy_id'}, status=400)
                    return
                if not _get_proxy_pool().remove_proxy(proxy_id):
                    self._send_json({'status': 'error', 'message': '代理不存在'}, status=404)
                    return
                self._send_json({'status': 'ok'})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool/toggle':
            # 启用/禁用代理：{proxy_id, disabled}。禁用的不参与轮换，也不能下发。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                proxy_id = str(body.get('proxy_id') or '').strip()
                if not proxy_id:
                    self._send_json({'status': 'error', 'message': '缺少 proxy_id'}, status=400)
                    return
                entry = _get_proxy_pool().set_disabled(proxy_id, bool(body.get('disabled')))
                if entry is None:
                    self._send_json({'status': 'error', 'message': '代理不存在'}, status=404)
                    return
                self._send_json({'status': 'ok', 'proxy': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool/check':
            # 走这条代理拿一次真实出口 IP。纯网络请求，不开浏览器、不排 FX 队列，
            # 所以不像 /api/account-pool/refresh 那样要看队列忙不忙。
            if not self._gate(with_rate=True, rate_action='proxy_check'):
                return
            try:
                body = self._read_json_body()
                proxy_id = str(body.get('proxy_id') or '').strip()
                if not proxy_id:
                    self._send_json({'status': 'error', 'message': '缺少 proxy_id'}, status=400)
                    return
                entry = _get_proxy_pool().check_proxy(proxy_id)
                if entry is None:
                    self._send_json({'status': 'error', 'message': '代理不存在'}, status=404)
                    return
                if entry.get('last_check_status') != 'ok':
                    self._send_json({
                        'status': 'error', 'code': 'PROXY_CHECK_FAILED',
                        'message': entry.get('last_check_error') or '代理不通',
                        'proxy': entry,
                    }, status=422)
                    return
                self._send_json({'status': 'ok', 'proxy': entry})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool/apply':
            # 把代理写进某个 AdsPower 环境的 userProxyConfig：{proxy_id, user_id}。
            # AdsPower 在浏览器启动时读代理，已开着的窗口要关掉重开才换出口。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                proxy_id = str(body.get('proxy_id') or '').strip()
                user_id = str(body.get('user_id') or '').strip()
                if not proxy_id or not user_id:
                    self._send_json({'status': 'error',
                                     'message': '缺少 proxy_id 或 user_id'}, status=400)
                    return
                entry = _get_proxy_pool().apply_to_profile(proxy_id, user_id)
                self._send_json({
                    'status': 'ok', 'proxy': entry,
                    'message': f'已写入环境 {user_id}，该浏览器下次启动时生效',
                })
            except KeyError as e:
                self._send_json({'status': 'error', 'message': str(e).strip("'")}, status=404)
            except ValueError as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/proxy-pool/import-legacy':
            # 把老的 runtime/proxy_pool.txt（proxy_rotator list 模式那份纯文本）
            # 并入代理号池，已存在的同 host:port 跳过。
            if not self._gate():
                return
            try:
                pool = _get_proxy_pool()
                result = pool.import_legacy_txt()
                self._send_json({'status': 'ok', 'proxies': pool.list_proxies(), **result})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/trend-refs/archive/restore':
            # 把归档里的条目挪回主库,供用户在前端"恢复"(不受软上限约束——用户的
            # 主动动作;挪回后若再超上限,靠下次搜索/换灵感触发的 persist 自动收敛)
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                ids = body.get('ids')
                if not isinstance(ids, list) or not ids:
                    self._send_json({'status': 'error', 'message': '缺少要恢复的 ids'}, status=400)
                    return
                result = restore_trend_refs(ids)
                if result['refs'] is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库/归档文件读取失败'}, status=500)
                    return
                self._send_json({
                    'status': 'ok',
                    'restored': result['restored'],
                    'refs': list(reversed(result['refs'])),
                    'archive': list(reversed(result['archive'])),
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/trend-refs/relabel':
            # 手动改名覆盖自动提炼的 label(自动提炼偶尔挑不到贴切关键词的兜底)。
            # archive: true 时对归档库条目操作。
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                ref_id = body.get('id')
                label = body.get('label')
                if not ref_id or not (label or '').strip():
                    self._send_json({'status': 'error', 'message': '缺少 id 或 label'}, status=400)
                    return
                result = relabel_trend_ref(ref_id, label, archive=bool(body.get('archive')))
                if result['refs'] is None:
                    self._send_json({'status': 'error', 'message': '联网参考案例库文件读取失败'}, status=500)
                    return
                if not result['ok']:
                    self._send_json({'status': 'error', 'message': '未找到该参考条目'}, status=404)
                    return
                self._send_json({'status': 'ok', 'refs': list(reversed(result['refs']))})
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
            if not self._gate(with_rate=True, rate_action='vlm_qa'):
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
            if not self._gate(with_rate=True, rate_action='image_vlm'):
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
                                if vid_word_count > 380:
                                    video_errs.append(f"VIDEO prompt word count ({vid_word_count}) exceeds limit of 380 words")
                                
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
                if not rate_ok(_client_ip(self), 'ideate'):
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
                pacing_skeleton_ids = body.get('pacing_skeleton_ids') or []
                remix_seed = body.get('remix_seed')

                result = run_ideate(config, count, theme=theme, theme_label=theme_label,
                                    trend_ref_ids=trend_ref_ids, remix_seed=remix_seed,
                                    pacing_skeleton_ids=pacing_skeleton_ids)
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
                if not rate_ok(_client_ip(self), 'compose'):
                    self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                dimensions = body.get('dimensions', {})
                config = effective_config(body.get('config'))
                task_id = body.get('task_id')
                if not task_id:
                    task_id = str(int(time.time() * 1000))

                # 灵感推荐只负责展示候选，不再整批入账。只有用户真正启动激发/合成
                # 的那一条，前端才会随 dimensions 带上 ledger_candidate；在创建后台
                # 任务前原子登记，确保未被激发的卡片不会污染台账及后续去重范围。
                # 这是请求级持久化元数据，不属于创意维度；取走后再交给合成器，避免
                # 它进入 LLM prompt、任务快照或断点续传指纹。
                ledger_candidate = dimensions.pop('ledger_candidate', None)
                if isinstance(ledger_candidate, dict):
                    register_ledger_candidates([ledger_candidate], source='Creative Activation')

                # Start background thread
                cleanup_old_tasks()
                # 重试沿用旧 task_id：终态记录被原地重置复用（覆盖被重试的记录），
                # 已在运行则不再起第二个 worker，前端拿到 ok 后重新挂流即可
                _, already_running = prepare_task_for_run(task_id, dimensions)
                if already_running:
                    self._send_json({'status': 'ok', 'task_id': task_id, 'already_running': True})
                    return

                # 联网参考案例库使用次数计次点：只有真正被「一键合成」的 idea 才算数
                # （灵感激发/浏览案例都不算），且必须是该 idea 确实借鉴过参考
                # （dimensions.trend_ref 非空，由 run_ideate 里 LLM 的 trend_ref
                # 字段透传而来）。candidate ids 由灵感激发时随 idea 一并带出
                # （见 prompt_pipeline._attach_trend_ref_ids）。非致命：失败不应
                # 拖垮合成本身。
                if dimensions.get('trend_ref') and dimensions.get('trend_ref_ids'):
                    try:
                        mark_trend_refs_used(dimensions['trend_ref_ids'])
                    except Exception as e:
                        if sys.stdout:
                            print(f"[TREND REFS] 合成计次失败（非致命）: {e}")

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
                if not rate_ok(_client_ip(self), 'compose'):
                    self._send_json({'status': 'error', 'message': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                dimensions = body.get('dimensions', {})
                config = effective_config(body.get('config'))
                if not _require_fx_admission(self):
                    return
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

        elif path == '/api/compose-cancel':
            if not self._gate():
                return
            try:
                body = self._read_json_body()
                task_id = body.get('task_id')
                if task_id and task_id in ACTIVE_TASKS:
                    log('INFO', 'CANCEL', "收到取消请求", task_id=task_id)
                    ACTIVE_TASKS[task_id]["cancel_event"].set()

                    # 所有类型的任务都立即终态化：旧写法只对非 FX 任务立即切
                    # cancelled，FX 任务（frames/videos/staged/auto）依赖 worker
                    # 在下一个 cancel_check 探测点自行感知——如果 worker 正卡在
                    # 一个长操作（90-240 秒 LLM 请求、浏览器自动化等），取消要
                    # 等到那个操作结束才生效，前端"正在取消"挂几分钟。现在统一
                    # 立即终态化，worker 凭 status 守卫发现记录已被终态化后静默退出。
                    dimensions = ACTIVE_TASKS[task_id].get("dimensions") or {}
                    id_prefix = str(task_id).split('_', 1)[0]
                    is_fx_capable = (
                        id_prefix in ('frames', 'videos', 'staged', 'auto')
                        or dimensions.get('type') in ('frames', 'videos', 'staged', 'auto_run')
                    )
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
                        # 纯 SPARK 内部旗标（video_generator 在腿边界读它）；外部 AdsPower
                        # 脚本自 2026-07 起不再读任何进程级全局标志。
                        builtins.google_fx_cancelled = True

                        # 取消 = 只发信号，不动浏览器（2026-07-26）。
                        # 原来这里会 stop_ads_browser()，那是"取消了半天停不下来、日志还在
                        # 疯狂检测"的直接放大器：脚本正在自己的等图轮询里，浏览器被抽走后
                        # 每一轮 DOM 扫描都抛 TargetClosedError 被 except 吞掉，照旧空转到
                        # MAX_WAIT_SECONDS；而且关浏览器会清掉 Flow 画布、打松登录 token，
                        # 下一单要从头重连重登。外部脚本自己的收尾注释也写着"保持浏览器开启，
                        # 避免画布被清空"。停止交给取消信号，浏览器留给下一单复用。
                        #
                        # 这里覆盖的是本进程里 fx_cancel_context 注册过的活跃请求；走别的
                        # 取消路径（SSE 断连、任务换代）时由那个上下文自己的守卫线程轮询
                        # cancel_event 兜底。
                        try:
                            cancel_flag = get_fx_cancel_flag()
                            n = 1 if cancel_flag.cancel_request(str(task_id)) else 0
                            FX_CONTROL.audit('task.cancel', str(task_id),
                                             {'reason': '用户取消了生成任务',
                                              'active_request_hit': bool(n)},
                                             actor=_client_ip(self))
                            log('INFO', 'CANCEL', f"已向任务自己的 FX 请求发出取消（命中 {n}，浏览器保持开启）",
                                task_id=task_id)
                        except Exception as flag_err:
                            log('WARN', 'CANCEL', f"通知外部 FX 脚本取消失败: {flag_err}", task_id=task_id)
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
                            task_result = task.get("result") or {}
                            title = task_result.get("project_key") or task_result.get("title")
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
                if not rate_ok(_client_ip(self), 'frames'):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                if not _require_fx_admission(self, config.get('imageBackend') == 'google_fx'):
                    return
                project_key = body.get('title', '')
                title = body.get('display_title') or project_key
                config['_project_key'] = project_key
                prompt_block = body.get('prompt_block', '')
                target_sequences = body.get('target_sequences')

                if not resolve_cover_reference(config, title):
                    self._send_json({
                        'status': 'error',
                        'message': '请先生成或选择封面图；第一帧必须以封面图进行图生图。',
                    }, status=400)
                    return

                import uuid
                task_id = f"frames_{uuid.uuid4().hex}"

                # 同一项目目录同一时刻只许一个渲染 worker 在跑：多标签页/刷新后
                # 重连早于点击、取消还没真正生效又点了重渲，都会撞出两个 worker
                # 各自拿着过期的 manifest 快照互相整体覆盖——已生成的帧因此"无故
                # 丢失"。已有 worker 在跑时不再起第二个，直接把它的 task_id 转告
                # 前端重新挂流（与 compose 的 already_running 同款语义）。
                project_dir = _get_project_dir(project_key)
                holder = claim_frame_run(project_dir, task_id)
                if holder:
                    self._send_json({'status': 'ok', 'task_id': holder, 'already_running': True})
                    return

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

        elif path == '/api/fix_frame_issue':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self), 'frames'):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                if not _require_fx_admission(self, config.get('imageBackend') == 'google_fx'):
                    return
                project_key = body.get('title', '')
                title = body.get('display_title') or project_key
                config['_project_key'] = project_key
                prompt_block = body.get('prompt_block', '')
                sequence = body.get('sequence')
                if not isinstance(sequence, int):
                    self._send_json({'status': 'error', 'message': 'sequence 必须是整数'}, status=400)
                    return
                manual_reason = body.get('manual_reason')
                manual_reason = manual_reason.strip() if isinstance(manual_reason, str) else None

                import uuid
                task_id = f"fixframe_{uuid.uuid4().hex}"

                # 与 /api/generate_frames 同款互斥：修复也会整体改写同一份 manifest，
                # 不能跟另一个渲染/修复 worker 同时跑同一个项目。
                project_dir = _get_project_dir(project_key)
                holder = claim_frame_run(project_dir, task_id)
                if holder:
                    self._send_json({'status': 'ok', 'task_id': holder, 'already_running': True})
                    return

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "frames", "theme": title, "userId": config.get('googleFxUserId') or None})

                threading.Thread(
                    target=fix_frame_issue_worker,
                    args=(task_id, config, title, prompt_block, sequence, manual_reason),
                    daemon=True
                ).start()

                self._send_json({'status': 'ok', 'task_id': task_id})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/flag_frame_issue':
            # 人工主动描述某一帧的问题：把描述记进 manifest（quality_gate 标成
            # manual_flagged），供随后的「修复此帧问题」当作待修问题使用。纯 manifest
            # 写入、不跑模型也不渲图，因此同步返回、不建后台任务。description 传空串
            # ＝撤销之前的人工标记（见 pipeline_orchestrator.set_manual_frame_issue）。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                sequence = body.get('sequence')
                if not isinstance(sequence, int):
                    self._send_json({'status': 'error', 'message': 'sequence 必须是整数'}, status=400)
                    return
                description = body.get('description')
                description = description.strip() if isinstance(description, str) else ''

                # 与渲染/修复 worker 同款项目级互斥：那些 worker 会拿着自己的 manifest
                # 快照整体回写，此刻插进去写的人工描述会被它们覆盖掉。宁可让人等渲染
                # 结束再标，也不要出现"标了但没了"。
                import uuid
                project_dir = _get_project_dir(title)
                claim_id = f"flagframe_{uuid.uuid4().hex}"
                holder = claim_frame_run(project_dir, claim_id)
                if holder:
                    self._send_json({'status': 'error',
                                     'message': '该创意的帧序列正在生成/修复中，请等它结束后再描述问题'},
                                    status=409)
                    return
                try:
                    from pipeline_orchestrator import set_manual_frame_issue
                    frame = set_manual_frame_issue(title, sequence, description)
                finally:
                    release_frame_run(project_dir, claim_id)

                log('INFO', 'FRAMES',
                    (f"人工标记第 {sequence} 帧问题：{description}" if description
                     else f"人工撤销第 {sequence} 帧的问题标记"),
                    title=title)
                self._send_json({'status': 'ok', 'frame': frame})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/sequence_review':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self), 'frames'):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                project_key = body.get('title', '')
                title = body.get('display_title') or project_key
                config['_project_key'] = project_key
                prompt_block = body.get('prompt_block', '')

                import uuid
                task_id = f"seqreview_{uuid.uuid4().hex}"

                # 与 /api/generate_frames、/api/fix_frame_issue 同款互斥：审查也会
                # 整体改写同一份 manifest，不能跟另一个渲染/修复 worker 同时跑同一个项目。
                project_dir = _get_project_dir(project_key)
                holder = claim_frame_run(project_dir, task_id)
                if holder:
                    self._send_json({'status': 'ok', 'task_id': holder, 'already_running': True})
                    return

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "frames", "theme": title})

                threading.Thread(
                    target=sequence_review_worker,
                    args=(task_id, config, title, prompt_block),
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
                if not rate_ok(_client_ip(self), 'frames'):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                if not _require_fx_admission(self):
                    return
                project_key = body.get('title', '')
                title = body.get('display_title') or project_key
                config['_project_key'] = project_key
                prompt_block = body.get('prompt_block', '')

                import uuid
                task_id = f"staged_{uuid.uuid4().hex}"

                # 与 /api/generate_frames 同款互斥：分步渲染同样长期整体覆写同一份
                # project manifest，不能跟另一个渲染 worker（含手动帧序列按钮）
                # 同时跑同一个项目。
                project_dir = _get_project_dir(project_key)
                holder = claim_frame_run(project_dir, task_id)
                if holder:
                    self._send_json({'status': 'ok', 'task_id': holder, 'already_running': True})
                    return

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
                if not rate_ok(_client_ip(self), 'frames'):
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
                if not rate_ok(_client_ip(self), 'videos'):
                    self._send_json({'error': '请求过于频繁，请稍后再试'}, status=429)
                    return
                body = self._read_json_body()
                config = effective_config(body.get('config'))
                if not _require_fx_admission(self):
                    return
                project_key = body.get('title', '')
                title = body.get('display_title') or project_key
                config['_project_key'] = project_key
                config['_merge_speed'] = body.get('merge_speed', 2)
                prompt_block = body.get('prompt_block', '')
                target_slots = body.get('target_slots')
                override_flagged = bool(body.get('override_flagged'))

                import uuid
                task_id = f"videos_{uuid.uuid4().hex}"

                cleanup_old_tasks()
                get_or_create_task(task_id, {"type": "videos", "theme": title, "userId": config.get('googleFxUserId') or None})

                threading.Thread(
                    target=generate_videos_worker,
                    args=(task_id, config, title, prompt_block, target_slots, override_flagged),
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
                    merged_info = merge_project_videos(
                        project_dir,
                        allow_partial=force,
                        speed=body.get('speed', 2),
                    )
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

        elif path == '/api/upload_video':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return

                content_type = self.headers.get('content-type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self._send_json({'error': 'Content-Type must be multipart/form-data'}, status=400)
                    return

                body_bytes = self._body_bytes

                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + body_bytes
                from email.parser import BytesParser
                from email.policy import default
                msg = BytesParser(policy=default).parsebytes(msg_bytes)

                fields = {}
                file_bytes = None
                file_name = None
                for part in msg.walk():
                    if part.get_content_disposition() != 'form-data':
                        continue
                    name = part.get_param('name', header='content-disposition')
                    if not name:
                        continue
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b''
                    if filename is not None:
                        if name == 'video' and payload:
                            file_bytes = payload
                            file_name = filename
                    else:
                        fields[name] = payload.decode('utf-8', errors='replace').strip()

                title = fields.get('title', '')
                slot_raw = fields.get('slot', '')
                force = fields.get('force', '').lower() in ('1', 'true', 'yes')
                prompt_block = fields.get('prompt_block', '')

                if not title or not slot_raw or not file_bytes:
                    self._send_json({'error': '缺少必要参数（title / slot / video 文件）'}, status=400)
                    return
                try:
                    slot = int(slot_raw)
                except ValueError:
                    self._send_json({'error': 'slot 必须是数字'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return

                frames_dir = os.path.join(project_dir, 'frames')
                videos_dir = os.path.join(project_dir, 'videos')
                os.makedirs(videos_dir, exist_ok=True)

                manifest_data = read_manifest(project_dir) or {'videos': [], 'frames': []}

                images_parsed, videos_parsed = {}, {}
                if prompt_block:
                    try:
                        images_parsed, videos_parsed = _parse_prompt_slots(prompt_block)
                    except Exception:
                        pass

                slot_to_path, _slot_to_quality = load_slot_frames(
                    manifest_data, frames_dir, len(images_parsed) or 999)

                existing_entry = next(
                    (v for v in manifest_data.get('videos', []) if v.get('slot') == slot), None)
                is_hero = bool(existing_entry and existing_entry.get('is_hero'))
                parsed_item = videos_parsed.get(slot)
                parsed_meta = parsed_item.get('meta', '') if isinstance(parsed_item, dict) else ''
                if not is_hero:
                    is_hero = 'HERO' in str(parsed_meta).upper()

                start_p = slot_to_path.get(slot)
                # 英雄展示视频只上传首帧、没有独立的结束锚点——end_p 留空，
                # verify_video_anchors 会自动只核对首帧，不核对尾帧。
                end_p = None if is_hero else slot_to_path.get(slot + 1)

                tmp_dir = tempfile.mkdtemp(prefix='upload_video_')
                dest_path = os.path.join(videos_dir, f'vid_{slot:03d}.mp4')
                try:
                    raw_ext = os.path.splitext(file_name or '')[1] or '.mp4'
                    raw_path = os.path.join(tmp_dir, f'raw{raw_ext}')
                    with open(raw_path, 'wb') as f:
                        f.write(file_bytes)

                    # 统一转码成 h264/yuv420p mp4：既保证 <video> 跨浏览器可播，
                    # 也保证后续合并成片（ffmpeg concat）的编码参数与其余槽位一致，
                    # 不会因为外部来源视频的容器/编码不同而拼接失败。
                    normalized_path = os.path.join(tmp_dir, 'normalized.mp4')
                    ffmpeg_cmd = ["ffmpeg", "-y", "-v", "error", "-i", raw_path,
                                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                                  "-movflags", "+faststart", normalized_path]
                    res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, encoding='utf-8', errors='replace', timeout=120)
                    if res.returncode != 0 or not os.path.exists(normalized_path):
                        self._send_json({
                            'error': f'视频转码失败，文件可能已损坏或格式不受支持: {(res.stderr or "")[-300:]}'
                        }, status=400)
                        return

                    ok, reason = verify_video_anchors(normalized_path, start_p, end_p, strict=False)
                    if not ok and not force:
                        self._send_json({
                            'status': 'anchor_mismatch',
                            'error': f'上传视频的首/尾帧与该槽位期望的锚点帧不符（{reason}），疑似传错文件。'
                                     f'如确认无误，可勾选"强制覆盖"后重新上传。',
                            'anchor_check': reason,
                        }, status=409)
                        return

                    for _attempt in range(5):
                        try:
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                            shutil.move(normalized_path, dest_path)
                            break
                        except (OSError, PermissionError):
                            time.sleep(0.2)
                    else:
                        self._send_json({'error': '目标文件被占用，写入失败，请重试'}, status=500)
                        return
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

                rel_path = os.path.relpath(
                    dest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                prior_prompt = (
                    (existing_entry or {}).get('prompt')
                    or (parsed_item.get('body') if isinstance(parsed_item, dict) else parsed_item)
                    or '手动上传视频'
                )
                video_info = {
                    'slot': slot,
                    'sequence': (existing_entry or {}).get('sequence', slot),
                    'file': rel_path,
                    'url': '/' + rel_path,
                    'prompt': prior_prompt,
                    'model': 'manual_upload',
                    'status': 'success',
                    'start_anchor_slot': (existing_entry or {}).get('start_anchor_slot', slot),
                    # merge_project_videos 靠 meta 里的 'HERO' 标记识别英雄展示视频（非
                    # is_hero 字段）；没有旧 manifest 记录时（该槽位此前从未生成成功过）
                    # 必须落回 prompt_block 解析出的 meta，否则手动上传的 HERO 视频会被
                    # 合成阶段直接忽略，成片里悄悄少了这一段。
                    'meta': (existing_entry or {}).get('meta') or parsed_meta,
                    'is_hero': is_hero,
                    'anchor_check': reason,
                    'source': 'manual_upload',
                }

                with manifest_lock(project_dir):
                    mdata = read_manifest(project_dir) or {'videos': []}
                    videos_list = [v for v in mdata.get('videos', []) if v.get('slot') != slot]
                    videos_list.append(video_info)
                    videos_list.sort(key=lambda v: v.get('slot', 0))
                    for idx, v in enumerate(videos_list):
                        v['sequence'] = idx + 1
                    mdata['videos'] = videos_list
                    # 手动覆盖单槽位视频：已合并成片不再反映当前内容，清掉避免误导
                    if 'merged_video' in mdata:
                        del mdata['merged_video']
                    write_manifest(project_dir, mdata)
                    # videos_list 复用同一个 video_info 对象，上面的 sequence 重排已经原地生效

                self._send_json({'status': 'ok', 'video': video_info})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/swap_video_slots':
            # 视频槽位之间的手动换位（前端把一张视频卡片拖到另一张卡片上时调用）。
            # 槽位编号与网格位置固定不动——动的只是"哪段视频落在哪个槽位"：
            #   mode=swap：两个槽位的视频对调（目标槽位没视频时退化成搬运，源槽位清空）
            #   mode=copy：把源槽位的视频复制一份到目标槽位，源槽位原样保留
            # 落盘文件名 vid_NNN.mp4 是槽位属性，因此文件真的要互换/改名，不能只改
            # manifest——合成成片（merge_project_videos）与前端都按槽位号取文件。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                mode = str(body.get('mode') or 'swap').lower()
                if mode not in ('swap', 'copy'):
                    self._send_json({'error': "mode 只能是 swap 或 copy"}, status=400)
                    return
                try:
                    from_slot = int(body.get('from_slot'))
                    to_slot = int(body.get('to_slot'))
                except (TypeError, ValueError):
                    self._send_json({'error': 'from_slot / to_slot 必须是数字'}, status=400)
                    return
                if from_slot == to_slot:
                    self._send_json({'error': '源槽位与目标槽位相同，无需换位'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return
                videos_dir = os.path.join(project_dir, 'videos')

                def _slot_path(slot):
                    return os.path.join(videos_dir, f'vid_{slot:03d}.mp4')

                def _rel_url(slot):
                    rel = os.path.relpath(
                        _slot_path(slot), os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                    return rel, '/' + rel

                def _rebase(entry, slot, origin_slot):
                    """把一条视频记录搬到另一个槽位：描述这段视频本身的字段（prompt /
                    model / status / meta / is_hero…）跟着内容一起走，只有槽位号、序号和
                    落盘路径属于新槽位。锚点校验结论必须作废——换位后这段视频的首尾帧
                    多半已经不再对应新槽位期望的锚点图，留着旧的 anchor_check 会让人
                    以为它验过了。"""
                    rel, url = _rel_url(slot)
                    moved = dict(entry)
                    moved['slot'] = slot
                    moved['file'] = rel
                    moved['url'] = url
                    moved['start_anchor_slot'] = slot
                    moved['anchor_check'] = '手动换位，未重新校验首尾帧锚点'
                    moved['swapped_from_slot'] = origin_slot
                    moved['source'] = 'manual_swap'
                    return moved

                with manifest_lock(project_dir):
                    mdata = read_manifest(project_dir) or {'videos': []}
                    videos = list(mdata.get('videos') or [])
                    src_entry = next((v for v in videos if v.get('slot') == from_slot), None)
                    dst_entry = next((v for v in videos if v.get('slot') == to_slot), None)
                    src_path, dst_path = _slot_path(from_slot), _slot_path(to_slot)

                    if src_entry is None or not os.path.exists(src_path):
                        self._send_json(
                            {'error': f'第 {from_slot} 段视频还没有可用的落盘文件，无法{"复制" if mode == "copy" else "换位"}'},
                            status=404)
                        return
                    dst_has_file = dst_entry is not None and os.path.exists(dst_path)

                    # 先动文件再改清单：文件动失败就直接报错返回，清单不会先一步说谎。
                    if mode == 'copy':
                        shutil.copyfile(src_path, dst_path)
                    elif dst_has_file:
                        tmp_path = dst_path + '.swap.tmp'
                        os.replace(src_path, tmp_path)
                        os.replace(dst_path, src_path)
                        os.replace(tmp_path, dst_path)
                    else:
                        os.replace(src_path, dst_path)

                    others = [v for v in videos if v.get('slot') not in (from_slot, to_slot)]
                    new_entries = [_rebase(src_entry, to_slot, from_slot)]
                    if mode == 'copy':
                        new_entries.append(dict(src_entry))  # 源槽位原样留下
                    elif dst_has_file:
                        new_entries.append(_rebase(dst_entry, from_slot, to_slot))
                    # else：搬运，源槽位记录随文件一起离开，不再留条目

                    videos_list = others + new_entries
                    videos_list.sort(key=lambda v: v.get('slot', 0))
                    for idx, v in enumerate(videos_list):
                        v['sequence'] = idx + 1
                    mdata['videos'] = videos_list
                    # 片段顺序变了，旧的合并成片不再反映当前内容
                    if 'merged_video' in mdata:
                        del mdata['merged_video']
                    write_manifest(project_dir, mdata)

                action = '复制' if mode == 'copy' else ('搬运' if not dst_has_file else '换位')
                log('INFO', 'VIDEOS', f'手动{action}视频：槽位 {from_slot} → {to_slot}', title=title)
                self._send_json({'status': 'ok', 'mode': mode, 'moved': not dst_has_file,
                                 'videos': videos_list})
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/swap_frame_slots':
            # 帧槽位之间的手动换位（前端把一张帧卡片拖到另一张卡片上时调用），与
            # /api/swap_video_slots 同构：槽位编号固定不动，动的只是"哪张图落在哪一格"。
            #   mode=swap：两格对调（目标格没图时退化成搬运）
            #   mode=copy：复制一份到目标格，源格原样保留
            #
            # 比视频换位多出来的一件事是 i2i 血统：帧序列是单链（每帧以上一帧为参考），
            # 任何一格的图被换掉，从较小的那个槽位往后的帧就都还派生自旧链——统一标
            # stale_lineage，与部分重生用的是同一个标记。被拖动的两格自己不标：那是
            # 人有意放在那里的画面，不是"忘了重生"的残留。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                mode = str(body.get('mode') or 'swap').lower()
                if mode not in ('swap', 'copy'):
                    self._send_json({'error': "mode 只能是 swap 或 copy"}, status=400)
                    return
                try:
                    from_seq = int(body.get('from_sequence'))
                    to_seq = int(body.get('to_sequence'))
                except (TypeError, ValueError):
                    self._send_json({'error': 'from_sequence / to_sequence 必须是数字'}, status=400)
                    return
                if from_seq == to_seq:
                    self._send_json({'error': '源槽位与目标槽位相同，无需换位'}, status=400)
                    return
                if from_seq < 1 or to_seq < 1:
                    self._send_json({'error': 'sequence 必须从 1 开始'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return

                # 同 /api/upload_frame：渲染/修复 worker 在跑时不许插队改 manifest
                import uuid
                claim_id = f"swapframe_{uuid.uuid4().hex}"
                holder = claim_frame_run(project_dir, claim_id)
                if holder:
                    self._send_json({'status': 'error',
                                     'message': '该创意的帧序列正在生成/修复中，请等它结束后再换位'},
                                    status=409)
                    return
                try:
                    frames_dir = os.path.join(project_dir, 'frames')
                    from frame_generator import _fx_relocate_frame_reference, _fx_extract_uuid

                    def _frame_path(seq):
                        return os.path.join(frames_dir, f'img_{seq:03d}.webp')

                    def _rebase_frame(entry, seq, origin_seq, fx_sidecars):
                        """把一条帧记录搬到另一格：描述这张图本身的字段（prompt / model /
                        质检结论…）跟着图走，只有槽位号与落盘路径属于新格子。血统在这里
                        断开（它不再派生自新位置的上一帧），parent_hash 清空。"""
                        rel = os.path.relpath(
                            _frame_path(seq), os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                        moved = dict(entry)
                        moved['slot'] = seq
                        moved['sequence'] = seq
                        moved['file'] = rel
                        moved['url'] = '/' + rel
                        moved['parent_hash'] = ''
                        moved['reference'] = None
                        moved['swapped_from_sequence'] = origin_seq
                        moved.pop('stale_lineage', None)
                        # FX 留档已经跟着图改名，记录里的 fx_src/fx_uuid 必须指向新文件；
                        # 这一格换上来的是没有画布 UUID 的图（手动上传/API 产出）就清干净，
                        # 免得留个指向别人留档的 UUID。
                        sidecar = fx_sidecars.get(seq)
                        if sidecar:
                            moved['fx_src'] = os.path.relpath(
                                sidecar, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                            moved['fx_uuid'] = _fx_extract_uuid(sidecar) or moved.get('fx_uuid')
                        else:
                            moved.pop('fx_src', None)
                            moved.pop('fx_uuid', None)
                        return moved

                    with manifest_lock(project_dir):
                        mdata = read_manifest(project_dir) or {}
                        frames_list = list(mdata.get('frames') or [])
                        src_entry = next((f for f in frames_list
                                          if f.get('sequence') == from_seq or f.get('slot') == from_seq), None)
                        dst_entry = next((f for f in frames_list
                                          if f.get('sequence') == to_seq or f.get('slot') == to_seq), None)
                        src_path, dst_path = _frame_path(from_seq), _frame_path(to_seq)

                        if not os.path.exists(src_path):
                            self._send_json(
                                {'error': f'第 {from_seq} 帧还没有可用的落盘图片，无法{"复制" if mode == "copy" else "换位"}'},
                                status=404)
                            return
                        if src_entry is None:
                            src_entry = {'slot': from_seq, 'sequence': from_seq}
                        dst_has_file = os.path.exists(dst_path)

                        # 先动文件再改清单：文件动失败就直接报错返回，清单不会先一步说谎
                        if mode == 'copy':
                            shutil.copyfile(src_path, dst_path)
                        elif dst_has_file:
                            tmp_path = dst_path + '.swap.tmp'
                            os.replace(src_path, tmp_path)
                            os.replace(dst_path, src_path)
                            os.replace(tmp_path, dst_path)
                        else:
                            os.replace(src_path, dst_path)

                        # FX 留档（frames/fx_src/img_NNN_<uuid>.jpg）跟着图一起换格：下一帧
                        # 挂参考只认这个文件名里的画布 UUID，留在原位就等于让后续生成继续
                        # 拿换位前的老图当参考。
                        fx_sidecars = _fx_relocate_frame_reference(
                            frames_dir, from_seq, to_seq, mode)

                        others = [f for f in frames_list
                                  if (f.get('sequence') or f.get('slot')) not in (from_seq, to_seq)]
                        new_entries = [_rebase_frame(src_entry, to_seq, from_seq, fx_sidecars)]
                        if mode == 'copy':
                            new_entries.append(dict(src_entry))  # 源格原样留下
                        elif dst_has_file and dst_entry is not None:
                            new_entries.append(_rebase_frame(dst_entry, from_seq, to_seq, fx_sidecars))
                        # else：搬运，源格的记录随图一起离开

                        frames_list = others + new_entries
                        frames_list.sort(key=lambda f: f.get('sequence', 0))
                        touched = {from_seq, to_seq}
                        earliest = min(from_seq, to_seq)
                        for f in frames_list:
                            seq_val = f.get('sequence') or 0
                            if seq_val > earliest and seq_val not in touched:
                                f['stale_lineage'] = True
                        mdata['frames'] = frames_list

                        # 图变了，按内容哈希记账的一致性审查结论自动作废
                        dropped = drop_stale_review_verdicts(mdata, project_dir)
                        if 'merged_video' in mdata:
                            del mdata['merged_video']
                        write_manifest(project_dir, mdata)

                    # 每一格图都是相邻两段视频的首/尾锚点
                    existing_video_slots = {v.get('slot') for v in (mdata.get('videos') or [])}
                    affected_videos = sorted({s for seq in touched
                                              for s in (seq - 1, seq)
                                              if s >= 1 and s in existing_video_slots})
                    action = '复制' if mode == 'copy' else ('搬运' if not dst_has_file else '换位')
                    log('INFO', 'FRAMES', f'手动{action}帧图：IMG {from_seq} → IMG {to_seq}', title=title)
                    self._send_json({'status': 'ok', 'mode': mode, 'moved': not dst_has_file,
                                     'frames': frames_list,
                                     'affected_video_slots': affected_videos,
                                     'dropped_review_frames': dropped})
                finally:
                    release_frame_run(project_dir, claim_id)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/upload_frame':
            # 帧槽位手动上传（前端把本地图片拖到某张帧卡片上时调用）：把图片转成
            # 与自动产出同款的 frames/img_NNN.webp 落盘，并改写 manifest 里该帧的记录。
            #
            # 三件必须同时做的事，漏一件就会让后续环节拿着过期结论跑：
            #   1) 这张图不是模型产出的，一切机器判定（质检/一致性审查/人工标记）
            #      都不再适用 —— gate 回落 pending_manual_review，判定字段清空；
            #   2) i2i 链在这里断了 —— 其后仍派生自旧链的帧标 stale_lineage；
            #   3) 该帧是相邻两段视频的首/尾锚点 —— 旧合并成片作废，并把受影响的
            #      视频槽位号回给前端提示重跑（不直接删视频记录：用户可能只是换张
            #      更好的同景别图，是否重跑该由他决定）。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return

                content_type = self.headers.get('content-type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self._send_json({'error': 'Content-Type must be multipart/form-data'}, status=400)
                    return

                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + self._body_bytes
                from email.parser import BytesParser
                from email.policy import default
                msg = BytesParser(policy=default).parsebytes(msg_bytes)

                fields = {}
                file_bytes = None
                for part in msg.walk():
                    if part.get_content_disposition() != 'form-data':
                        continue
                    name = part.get_param('name', header='content-disposition')
                    if not name:
                        continue
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b''
                    if filename is not None:
                        if name == 'image' and payload:
                            file_bytes = payload
                    else:
                        fields[name] = payload.decode('utf-8', errors='replace').strip()

                title = fields.get('title', '')
                seq_raw = fields.get('sequence', '')
                prompt_block = fields.get('prompt_block', '')
                if not title or not seq_raw or not file_bytes:
                    self._send_json({'error': '缺少必要参数（title / sequence / image 文件）'}, status=400)
                    return
                try:
                    sequence = int(seq_raw)
                except ValueError:
                    self._send_json({'error': 'sequence 必须是数字'}, status=400)
                    return
                if sequence < 1:
                    self._send_json({'error': 'sequence 必须从 1 开始'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return

                # 与 /api/flag_frame_issue 同款项目级互斥：渲染/修复 worker 会拿着自己的
                # manifest 快照整体回写，此刻插进去写的手动帧会被它覆盖掉（图还在盘上，
                # 清单里却没了）。宁可让人等渲染结束再传。
                import uuid
                claim_id = f"uploadframe_{uuid.uuid4().hex}"
                holder = claim_frame_run(project_dir, claim_id)
                if holder:
                    self._send_json({'status': 'error',
                                     'message': '该创意的帧序列正在生成/修复中，请等它结束后再上传'},
                                    status=409)
                    return
                try:
                    frames_dir = os.path.join(project_dir, 'frames')
                    os.makedirs(frames_dir, exist_ok=True)
                    dest_path = os.path.join(frames_dir, f'img_{sequence:03d}.webp')

                    manifest_data = read_manifest(project_dir) or {}
                    aspect_ratio = manifest_data.get('aspect_ratio') or '9:16'

                    from PIL import Image
                    from frame_generator import _crop_to_aspect_ratio
                    import io as _io
                    try:
                        img = Image.open(_io.BytesIO(file_bytes))
                        # 与模型产出帧同一条落盘管线：先按本单画幅居中裁剪再存 WebP，
                        # 否则一张 4:3 的手机照混进 9:16 链，i2v 配对必然构图跳变。
                        img = _crop_to_aspect_ratio(img.convert('RGB'), aspect_ratio)
                        img.save(dest_path, format='WEBP', quality=80)
                    except Exception as e:
                        self._send_json({'error': f'图片解析失败，可能不是合法的图片文件: {e}'}, status=400)
                        return

                    # The uploaded WebP has no Flow canvas UUID. Remove the old slot's UUID
                    # sidecar and any converted-reference cache; otherwise generation of the
                    # next frame mounts the pre-upload image instead of this new file.
                    from frame_generator import _fx_clear_frame_reference
                    cleared_fx_refs = _fx_clear_frame_reference(frames_dir, sequence)

                    rel_path = os.path.relpath(
                        dest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')

                    images_parsed = {}
                    if prompt_block:
                        try:
                            images_parsed, _videos_parsed = _parse_prompt_slots(prompt_block)
                        except Exception:
                            pass
                    parsed_item = images_parsed.get(sequence)
                    parsed_prompt = (parsed_item.get('body') if isinstance(parsed_item, dict) else parsed_item) or ''

                    with manifest_lock(project_dir):
                        mdata = read_manifest(project_dir) or {}
                        frames_list = list(mdata.get('frames') or [])
                        target = next((f for f in frames_list
                                       if f.get('sequence') == sequence or f.get('slot') == sequence), None)
                        if target is None:
                            target = {'slot': sequence, 'sequence': sequence}
                            frames_list.append(target)
                        target['slot'] = sequence
                        target['sequence'] = sequence
                        target['file'] = rel_path
                        target['url'] = '/' + rel_path
                        target['prompt'] = target.get('prompt') or parsed_prompt or '手动上传图片'
                        target['model'] = 'manual_upload'
                        target['source'] = 'manual_upload'
                        target['aspect_ratio'] = aspect_ratio
                        # 机器判定全部作废：这张图不是模型渲的，谁也没看过它
                        target['quality_gate'] = 'pending_manual_review'
                        target['vlm_qa_reason'] = None
                        for key in ('manual_issue', 'manual_flag_prev_gate', 'review_issues',
                                    'review_frames_sha256', 'stale_lineage'):
                            target.pop(key, None)
                        # i2i 血统在这一帧断开：它不派生自任何上一帧
                        target['parent_hash'] = ''
                        target['reference'] = None
                        # Manual uploads are local files, not the old Google FX canvas asset.
                        # Drop every field that could advertise or reuse the replaced asset.
                        for key in ('fx_uuid', 'fx_src', 'backend', 'transport', 'actual_pixels',
                                    'degraded_reason', 'anchor_reference'):
                            target.pop(key, None)

                        frames_list.sort(key=lambda f: f.get('sequence', 0))
                        # 其后的帧仍派生自被换掉的那张旧图——标 stale_lineage，
                        # 与部分重生（update_manifest_stale_status）用的是同一个标记
                        for f in frames_list:
                            if isinstance(f, dict) and (f.get('sequence') or 0) > sequence:
                                f['stale_lineage'] = True
                        mdata['frames'] = frames_list

                        dropped = drop_stale_review_verdicts(mdata, project_dir)
                        if 'merged_video' in mdata:
                            del mdata['merged_video']
                        write_manifest(project_dir, mdata)

                    # 这一帧是 vid_{seq-1}（尾锚点）与 vid_{seq}（首锚点）的锚点图
                    existing_video_slots = {v.get('slot') for v in (mdata.get('videos') or [])}
                    affected_videos = sorted(s for s in (sequence - 1, sequence)
                                             if s >= 1 and s in existing_video_slots)
                    log('INFO', 'FRAMES',
                        f'手动上传第 {sequence} 帧图片，覆盖 {rel_path}'
                        + (f'；已清理 {len(cleared_fx_refs)} 个旧 FX 参考留档' if cleared_fx_refs else '')
                        + (f'；受影响的视频槽位 {affected_videos}' if affected_videos else ''),
                        title=title)
                    self._send_json({'status': 'ok', 'frame': dict(target),
                                     'affected_video_slots': affected_videos,
                                     'dropped_review_frames': dropped})
                finally:
                    release_frame_run(project_dir, claim_id)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/delete_slot':
            # 删除一整拍（帧/视频卡片上的「删除」按钮）：图片 N 与视频 N 的提示词、落盘
            # 文件、manifest 记录一起从当前任务移除，其后所有图片/视频整体前移一位。
            # 真正动文件前必须先写 .deleted_slots 恢复快照；这是与编号压缩同一个
            # 临界区内的强制步骤，快照失败则整次删除失败，不允许降级成不可恢复删除。
            #
            # 为什么必须重新编号而不是留空洞：槽位号不是标签而是契约——视频 N 恒等于
            # IMG N → IMG N+1、视频数恒等于图片数-1，帧网格、配对门禁、合成成片全按这个
            # 推算。留一个空洞就得让每一处都学会跳过它；整体前移则让所有不变量继续成立。
            # 代价只有一处：跨过删除点的那一段视频（新 VID N-1）尾锚点换了张图，标出来
            # 待重跑，并在 anchor_check 上写明原因。
            #
            # 英雄展示视频（[HERO]，槽位号恒等于最后一张图）不随某一拍消失：它是全片的
            # 收尾镜头、不属于被删的那一拍——重新挂到新的最后一张图上。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                prompt_block = body.get('prompt_block') or ''
                try:
                    sequence = int(body.get('sequence'))
                except (TypeError, ValueError):
                    self._send_json({'error': 'sequence 必须是数字'}, status=400)
                    return
                if sequence < 1:
                    self._send_json({'error': 'sequence 必须从 1 开始'}, status=400)
                    return

                images, videos = _parse_prompt_slots(prompt_block)
                if not images:
                    self._send_json({'error': '提示词块里没有解析到任何图片槽位，无法删除'}, status=400)
                    return
                image_count = max(images)
                if sequence > image_count:
                    self._send_json(
                        {'error': f'第 {sequence} 拍不存在（本单共 {image_count} 张图片）'}, status=400)
                    return
                if image_count <= 1:
                    self._send_json({'error': '本单只剩最后一张图片，删掉就没有序列了'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                if not os.path.exists(project_dir):
                    self._send_json({'error': f'找不到项目目录: {title}'}, status=404)
                    return

                # 浏览器可能仍握着刷新前的旧 prompt_block。若它比磁盘 manifest 少一格，
                # 继续按旧编号删除会把另一拍误当成目标，并再次压缩整个序列。删除属于
                # 不可凭客户端旧状态猜测的操作：两边槽位数不一致时要求刷新后重试。
                manifest_guard = read_manifest(project_dir) or {}
                manifest_frame_slots = {
                    item.get('sequence') or item.get('slot')
                    for item in (manifest_guard.get('frames') or [])
                    if isinstance(item.get('sequence') or item.get('slot'), int)
                }
                manifest_image_count = len(manifest_frame_slots)
                if manifest_image_count and manifest_image_count != image_count:
                    self._send_json({
                        'status': 'rejected',
                        'error': (f'页面槽位状态已过期（页面 {image_count} 张，磁盘 '
                                  f'{manifest_image_count} 张）。已阻止删除，请刷新页面后再试。'),
                        'refresh_required': True,
                        'client_image_count': image_count,
                        'manifest_image_count': manifest_image_count,
                    }, status=409)
                    return

                import uuid
                claim_id = f"delslot_{uuid.uuid4().hex}"
                holder = claim_frame_run(project_dir, claim_id)
                if holder:
                    self._send_json({'status': 'error',
                                     'message': '该创意的帧序列正在生成/修复中，请等它结束后再删除'},
                                    status=409)
                    return
                try:
                    new_image_count = image_count - 1
                    hero_idx = next((i for i, item in videos.items()
                                     if 'HERO' in str((item or {}).get('meta', '')).upper()), None)
                    hero_item = videos.get(hero_idx) if hero_idx is not None else None

                    def _image_target(k):
                        """老图片槽位 k 删除后的新槽位号；None＝这一格被删掉了。"""
                        if k == sequence:
                            return None
                        return k - 1 if k > sequence else k

                    def _video_target(k):
                        """老视频槽位 k 删除后的新槽位号；None＝这一段不再存在。
                        普通视频前移后若超出"视频数 = 图片数-1"（删的是最后一拍时会
                        出现这种悬空段），一并删掉；英雄段单独改挂到新的最后一张图。"""
                        if hero_idx is not None and k == hero_idx:
                            return new_image_count if new_image_count >= 1 else None
                        if k == sequence:
                            return None
                        t = k - 1 if k > sequence else k
                        return t if t <= new_image_count - 1 else None

                    removed = {
                        'image_prompt': (images.get(sequence) or {}).get('body', ''),
                        'video_prompt': (videos.get(sequence) or {}).get('body', ''),
                    }

                    frames_dir = os.path.join(project_dir, 'frames')
                    videos_dir = os.path.join(project_dir, 'videos')
                    fx_src_dir = os.path.join(frames_dir, 'fx_src')

                    # 槽位换号表先算出来：快照要据此判断"哪些文件这次会被真的删掉"，
                    # 所以必须排在归档之前。
                    frame_targets = {k: _image_target(k) for k in range(1, image_count + 1)}
                    video_slots = set(range(1, image_count + 1)) | set(videos)
                    video_targets = {k: _video_target(k) for k in sorted(video_slots)}

                    # --- 可恢复快照：必须在任何 os.remove/os.replace 之前完成 ---
                    # 除了被删那一格，还保留整份删除前 manifest 和 prompt_block，
                    # 因为后续整体前移会改掉所有槽位号，单留一个媒体文件不足以可靠回滚。
                    archive_stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                    archive_dir = os.path.join(
                        project_dir, '.deleted_slots',
                        f'{archive_stamp}_slot_{sequence:03d}',
                    )
                    os.makedirs(archive_dir, exist_ok=False)
                    manifest_before = os.path.join(project_dir, 'manifest.json')
                    if os.path.isfile(manifest_before):
                        shutil.copy2(manifest_before, os.path.join(archive_dir, 'manifest.before.json'))
                    with open(os.path.join(archive_dir, 'prompt_block.before.txt'),
                              'w', encoding='utf-8') as f:
                        f.write(prompt_block)
                    with open(os.path.join(archive_dir, 'removed.json'),
                              'w', encoding='utf-8') as f:
                        json.dump({'title': title, 'sequence': sequence, **removed},
                                  f, ensure_ascii=False, indent=2)

                    # 归档"这次会被物理删除"的全部文件，而不只是第 sequence 拍：
                    # 删最后一拍时，跨过删除点的那一段视频（老槽位 image_count-1）
                    # 前移后会超出"视频数=图片数-1"而被一并删除——它此前不在快照里，
                    # 于是那种情况下的删除是不可逆的。
                    archived_frame_slots = []
                    archived_video_slots = []
                    for old, target in sorted(frame_targets.items()):
                        if target is not None:
                            continue
                        src = os.path.join(frames_dir, f'img_{old:03d}.webp')
                        if os.path.isfile(src):
                            shutil.copy2(src, os.path.join(archive_dir, os.path.basename(src)))
                            archived_frame_slots.append(old)
                        if os.path.isdir(fx_src_dir):
                            for name in os.listdir(fx_src_dir):
                                if name.startswith(f'img_{old:03d}_'):
                                    shutil.copy2(os.path.join(fx_src_dir, name),
                                                 os.path.join(archive_dir, name))
                    for old, target in sorted(video_targets.items()):
                        if target is not None:
                            continue
                        src = os.path.join(videos_dir, f'vid_{old:03d}.mp4')
                        if os.path.isfile(src):
                            shutil.copy2(src, os.path.join(archive_dir, os.path.basename(src)))
                            archived_video_slots.append(old)

                    # --- 提示词块：重新编号后回写 ---
                    images_new = {}
                    for k, item in images.items():
                        t = _image_target(k)
                        if t is not None:
                            images_new[t] = item
                    videos_new = {}
                    for k, item in videos.items():
                        t = _video_target(k)
                        if t is not None:
                            videos_new[t] = item
                    new_prompt_block = _format_prompt_block(images_new, videos_new)

                    # --- 磁盘文件：删除目标、其余整体前移 ---
                    def _apply_moves(dir_path, name_of, targets):
                        """targets: {老槽位号: 新槽位号|None}。新号恒 <= 老号，因此按老号
                        升序处理时目标文件名必然已经腾空，不需要中转文件。"""
                        for old in sorted(targets):
                            new = targets[old]
                            src = os.path.join(dir_path, name_of(old))
                            if not os.path.exists(src):
                                continue
                            if new is None:
                                os.remove(src)
                            elif new != old:
                                os.replace(src, os.path.join(dir_path, name_of(new)))

                    _apply_moves(frames_dir, lambda n: f'img_{n:03d}.webp', frame_targets)
                    _apply_moves(videos_dir, lambda n: f'vid_{n:03d}.mp4', video_targets)

                    # FX 路径给每帧留档的原始 jpg（frames/fx_src/img_NNN_<uuid>.jpg）跟着
                    # 一起改名：留在原位会让重试时按前缀找参考命中另一帧的旧图。
                    if os.path.isdir(fx_src_dir):
                        for fname in sorted(os.listdir(fx_src_dir)):
                            m = re.match(r'^img_(\d+)_(.*)$', fname)
                            if not m:
                                continue
                            old_seq = int(m.group(1))
                            target = frame_targets.get(old_seq, old_seq)
                            src = os.path.join(fx_src_dir, fname)
                            if target is None:
                                os.remove(src)
                            elif target != old_seq:
                                os.replace(src, os.path.join(fx_src_dir,
                                                             f'img_{target:03d}_{m.group(2)}'))

                    # --- manifest：记录跟着文件一起重新编号 ---
                    base_dir = os.path.dirname(os.path.abspath(__file__))

                    def _rel_url(path):
                        rel = os.path.relpath(path, base_dir).replace('\\', '/')
                        return rel, '/' + rel

                    with manifest_lock(project_dir):
                        mdata = read_manifest(project_dir) or {}

                        frames_new = []
                        for f in (mdata.get('frames') or []):
                            old_seq = f.get('sequence') or f.get('slot')
                            target = _image_target(old_seq) if isinstance(old_seq, int) else None
                            if target is None:
                                continue
                            rel, url = _rel_url(os.path.join(frames_dir, f'img_{target:03d}.webp'))
                            f['slot'] = target
                            f['sequence'] = target
                            f['file'] = rel
                            f['url'] = url
                            # 接缝及其之后的帧：现在这一格是原来的下一张，它的父帧已经
                            # 被删了，i2i 血统就此断开
                            if target >= sequence:
                                f['stale_lineage'] = True
                            frames_new.append(f)
                        frames_new.sort(key=lambda x: x.get('sequence', 0))
                        mdata['frames'] = frames_new

                        videos_new_entries = []
                        for v in (mdata.get('videos') or []):
                            old_slot = v.get('slot')
                            target = _video_target(old_slot) if isinstance(old_slot, int) else None
                            if target is None:
                                continue
                            rel, url = _rel_url(os.path.join(videos_dir, f'vid_{target:03d}.mp4'))
                            v['slot'] = target
                            v['file'] = rel
                            v['url'] = url
                            v['start_anchor_slot'] = target
                            if target == sequence - 1:
                                v['anchor_check'] = (f'第 {sequence} 拍已删除，本段尾锚点换了一张图，'
                                                     f'未重新校验')
                            videos_new_entries.append(v)
                        videos_new_entries.sort(key=lambda x: x.get('slot', 0))
                        for idx, v in enumerate(videos_new_entries):
                            v['sequence'] = idx + 1
                        mdata['videos'] = videos_new_entries

                        # 帧图整体换过位置：按帧号记账的审查结论与链回望结果全部失效
                        dropped = drop_stale_review_verdicts(mdata, project_dir)
                        mdata.pop('chain_drift', None)
                        if 'merged_video' in mdata:
                            del mdata['merged_video']
                        write_manifest(project_dir, mdata)

                    # 删除后的状态指纹：撤销时据此判断这单在删除之后有没有被继续
                    # 改动过（又生成/重试/上传/再删了别的拍）。指纹对不上仍可恢复，
                    # 但要用户明确确认会丢弃删除之后的新内容，见 /api/restore_slot。
                    with open(os.path.join(archive_dir, 'state.json'), 'w', encoding='utf-8') as f:
                        json.dump({
                            'title': title,
                            'sequence': sequence,
                            'created_at': datetime.now().isoformat(timespec='seconds'),
                            'image_count_before': image_count,
                            'image_count_after': new_image_count,
                            'hero_slot_before': hero_idx,
                            'archived_frame_slots': archived_frame_slots,
                            'archived_video_slots': archived_video_slots,
                            'manifest_after_fingerprint': manifest_fingerprint(mdata),
                        }, f, ensure_ascii=False, indent=2)

                    affected = [sequence - 1] if any(
                        v.get('slot') == sequence - 1 for v in videos_new_entries) else []
                    log('INFO', 'FRAMES',
                        f'删除第 {sequence} 拍（图片+视频提示词与文件），其后整体前移一位；'
                        f'剩余图片 {new_image_count} 张',
                        title=title)
                    self._send_json({'status': 'ok',
                                     'prompt_block': new_prompt_block,
                                     # 结构化槽位契约随之刷新：前端优先消费它，不回给它
                                     # 就只能退回前端正则解析（两套解析的行为差异是历史
                                     # 事故的前提，见 prompt_slots_list 的说明）
                                     'prompt_slots': prompt_slots_list(new_prompt_block),
                                     'frames': frames_new,
                                     'videos': videos_new_entries,
                                     'image_count': new_image_count,
                                     'affected_video_slots': affected,
                                     'dropped_review_frames': dropped,
                                     'removed': removed,
                                     'recovery_snapshot': archive_dir})
                finally:
                    release_frame_run(project_dir, claim_id)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/restore_slot':
            # 撤销一次「删除整拍」。删除时已在 .deleted_slots/<id>/ 落下完整快照
            # （删除前的 manifest 与 prompt_block ＋ 所有被物理删除的媒体文件），
            # 这里做它的逆操作：当前编号整体后移一位、归档文件放回原位、
            # manifest 与 prompt_block 还原成删除前那一份。
            #
            # 为什么是「整单回滚」而不是「把那一格插回去」：删除会给其后每一格
            # 重新编号，逐格插回等于重新推导每一格的来历；快照里存着整份删除前
            # manifest，直接还原它才是可靠语义——当初把整份 manifest 存进快照就是
            # 为了这一步（此前只有写快照的那一半，读回那一半从来没有实现，代价是
            # 一次误删要靠手写脚本捞回来，见 tools/recover_ice_cave_slot3.py）。
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                body = self._read_json_body()
                title = body.get('title', '')
                snapshot_id = str(body.get('snapshot_id') or '')
                force = bool(body.get('force'))
                current_prompt_block = body.get('prompt_block') or ''

                # snapshot_id 直接来自请求体、又要拼进路径：只允许快照目录的固定形状，
                # 挡掉 ../ 之类的穿越
                if not re.fullmatch(r'\d{8}_\d{6}_\d+_slot_\d{3}', snapshot_id):
                    self._send_json({'error': 'snapshot_id 格式不合法'}, status=400)
                    return

                project_dir = _get_project_dir(title)
                archive_dir = os.path.join(project_dir, '.deleted_slots', snapshot_id)
                if not os.path.isdir(archive_dir):
                    self._send_json({'error': f'找不到恢复快照: {snapshot_id}'}, status=404)
                    return

                prompt_before_path = os.path.join(archive_dir, 'prompt_block.before.txt')
                manifest_before_path = os.path.join(archive_dir, 'manifest.before.json')
                if not os.path.isfile(prompt_before_path):
                    self._send_json({'error': '快照缺少 prompt_block.before.txt，无法恢复'},
                                    status=422)
                    return

                import uuid
                claim_id = f"restoreslot_{uuid.uuid4().hex}"
                holder = claim_frame_run(project_dir, claim_id)
                if holder:
                    self._send_json({'status': 'error',
                                     'message': '该创意的帧序列正在生成/修复中，请等它结束后再恢复'},
                                    status=409)
                    return
                try:
                    with open(prompt_before_path, 'r', encoding='utf-8') as f:
                        old_prompt_block = f.read()
                    images_old, videos_old = _parse_prompt_slots(old_prompt_block)
                    if not images_old:
                        self._send_json({'error': '快照里的提示词块解析不出图片槽位'}, status=422)
                        return
                    image_count_old = max(images_old)

                    state = {}
                    state_path = os.path.join(archive_dir, 'state.json')
                    if os.path.isfile(state_path):
                        with open(state_path, 'r', encoding='utf-8') as f:
                            state = json.load(f) or {}
                    if state.get('restored_at'):
                        self._send_json({'error': '这份快照已经恢复过了'}, status=409)
                        return

                    removed_meta = {}
                    removed_path = os.path.join(archive_dir, 'removed.json')
                    if os.path.isfile(removed_path):
                        with open(removed_path, 'r', encoding='utf-8') as f:
                            removed_meta = json.load(f) or {}
                    sequence = int(state.get('sequence') or removed_meta.get('sequence') or 0)
                    if sequence < 1:
                        self._send_json({'error': '快照里没有记录被删除的拍号'}, status=422)
                        return

                    # 删除之后这单有没有被继续改动过（又生成/重试/上传/再删了别的拍）。
                    # 对不上仍可恢复，但必须由用户明确确认——恢复会把 manifest 整份
                    # 还原成删除前那一份，删除之后新产生的记录会随之丢失。
                    current_manifest = read_manifest(project_dir) or {}
                    expected_fp = state.get('manifest_after_fingerprint')
                    actual_fp = manifest_fingerprint(current_manifest)
                    diverged = bool(expected_fp) and expected_fp != actual_fp
                    if diverged and not force:
                        self._send_json({
                            'status': 'diverged',
                            'error': ('删除之后这单又被改动过（重新生成/重试/上传等）。'
                                      '继续恢复会把清单整份还原成删除前那一版，'
                                      '删除之后新产生的记录会丢失。'),
                            'snapshot_id': snapshot_id,
                        }, status=409)
                        return

                    # 与 /api/delete_slot 同一套换号规则，反过来用
                    new_image_count = image_count_old - 1
                    hero_idx = state.get('hero_slot_before')
                    if hero_idx is None:
                        hero_idx = next((i for i, item in videos_old.items()
                                         if 'HERO' in str((item or {}).get('meta', '')).upper()),
                                        None)

                    def _image_target_old(k):
                        if k == sequence:
                            return None
                        return k - 1 if k > sequence else k

                    def _video_target_old(k):
                        if hero_idx is not None and k == hero_idx:
                            return new_image_count if new_image_count >= 1 else None
                        if k == sequence:
                            return None
                        t = k - 1 if k > sequence else k
                        return t if t <= new_image_count - 1 else None

                    frames_dir = os.path.join(project_dir, 'frames')
                    videos_dir = os.path.join(project_dir, 'videos')
                    fx_src_dir = os.path.join(frames_dir, 'fx_src')

                    # 反向换号表：{当前槽位: 应还原到的老槽位}。老号恒 >= 当前号，
                    # 所以按老号降序处理时目标文件名必然已经腾空，不需要中转文件。
                    frame_back = {}
                    for k in range(1, image_count_old + 1):
                        t = _image_target_old(k)
                        if t is not None:
                            frame_back[t] = k
                    video_back = {}
                    for k in sorted(set(range(1, image_count_old + 1)) | set(videos_old)):
                        t = _video_target_old(k)
                        if t is not None:
                            video_back[t] = k

                    def _plan_moves(dir_path, name_of, back_map):
                        """先整体校验再执行：目标位置已被占用就整次拒绝，绝不半途改到
                        一半、留下一个既不是删除前也不是删除后的状态。

                        占用判断要按"执行到这一步时"的名字集合来算，而不是当前磁盘的
                        静态快照——按老号降序处理时，前一步已经把 img_003 搬去 img_004，
                        img_003 这个名字对下一步来说是空的。"""
                        occupied = set(os.listdir(dir_path)) if os.path.isdir(dir_path) else set()
                        moves = []
                        for cur, old in sorted(back_map.items(), key=lambda kv: -kv[1]):
                            if cur == old:
                                continue
                            src_name, dst_name = name_of(cur), name_of(old)
                            if src_name not in occupied:
                                continue
                            if dst_name in occupied:
                                raise RuntimeError(f'恢复目标位置已被占用: {dst_name}')
                            occupied.discard(src_name)
                            occupied.add(dst_name)
                            moves.append((os.path.join(dir_path, src_name),
                                          os.path.join(dir_path, dst_name)))
                        return moves

                    frame_moves = _plan_moves(
                        frames_dir, lambda n: f'img_{n:03d}.webp', frame_back)
                    video_moves = _plan_moves(
                        videos_dir, lambda n: f'vid_{n:03d}.mp4', video_back)

                    fx_moves = []
                    if os.path.isdir(fx_src_dir):
                        for cur, old in sorted(frame_back.items(), key=lambda kv: -kv[1]):
                            if cur == old:
                                continue
                            for fname in os.listdir(fx_src_dir):
                                m = re.match(r'^img_(\d+)_(.*)$', fname)
                                if not m or int(m.group(1)) != cur:
                                    continue
                                fx_moves.append((
                                    os.path.join(fx_src_dir, fname),
                                    os.path.join(fx_src_dir, f'img_{old:03d}_{m.group(2)}')))

                    # 恢复前先给「当前状态」也留一份快照：恢复本身不删除任何媒体
                    # 文件（只重命名 + 把归档文件放回空出来的位置），会被覆盖的只有
                    # manifest 与提示词块，所以存这两样就够回到恢复前。
                    restore_stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                    restore_point = os.path.join(
                        project_dir, '.deleted_slots',
                        f'{restore_stamp}_restorepoint_of_{snapshot_id}')
                    os.makedirs(restore_point, exist_ok=True)
                    cur_manifest_path = os.path.join(project_dir, 'manifest.json')
                    if os.path.isfile(cur_manifest_path):
                        shutil.copy2(cur_manifest_path,
                                     os.path.join(restore_point, 'manifest.before.json'))
                    if current_prompt_block:
                        with open(os.path.join(restore_point, 'prompt_block.before.txt'),
                                  'w', encoding='utf-8') as f:
                            f.write(current_prompt_block)

                    for src, dst in frame_moves + video_moves + fx_moves:
                        os.replace(src, dst)

                    # 归档的媒体文件放回原位（文件名本身带的就是老槽位号）
                    restored_files = []
                    for fname in sorted(os.listdir(archive_dir)):
                        if re.fullmatch(r'img_\d{3}\.webp', fname):
                            dst_dir = frames_dir
                        elif re.fullmatch(r'vid_\d{3}\.mp4', fname):
                            dst_dir = videos_dir
                        elif re.match(r'^img_\d{3}_.+', fname):
                            dst_dir = fx_src_dir
                        else:
                            continue
                        os.makedirs(dst_dir, exist_ok=True)
                        shutil.copy2(os.path.join(archive_dir, fname),
                                     os.path.join(dst_dir, fname))
                        restored_files.append(fname)

                    # manifest 还原成删除前那一份
                    with manifest_lock(project_dir):
                        if os.path.isfile(manifest_before_path):
                            with open(manifest_before_path, 'r', encoding='utf-8') as f:
                                mdata = json.load(f)
                        else:
                            mdata = current_manifest
                        write_manifest(project_dir, mdata)

                    # 标记这份快照已消费，避免同一份被恢复两次（第二次的换号前提
                    # 已经不成立，会把好好的一单推乱）
                    state.update({
                        'restored_at': datetime.now().isoformat(timespec='seconds'),
                        'restored_forced': bool(diverged and force),
                        'restore_point': restore_point,
                    })
                    with open(state_path, 'w', encoding='utf-8') as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)

                    log('INFO', 'FRAMES',
                        f'恢复第 {sequence} 拍（快照 {snapshot_id}），'
                        f'其后整体后移一位；现有图片 {image_count_old} 张'
                        + ('；已确认覆盖删除之后的改动' if diverged else ''),
                        title=title)
                    self._send_json({
                        'status': 'ok',
                        'sequence': sequence,
                        'prompt_block': old_prompt_block,
                        'prompt_slots': prompt_slots_list(old_prompt_block),
                        'frames': mdata.get('frames') or [],
                        'videos': mdata.get('videos') or [],
                        'image_count': image_count_old,
                        'restored_files': restored_files,
                        'forced': bool(diverged and force),
                        'restore_point': restore_point,
                    })
                finally:
                    release_frame_run(project_dir, claim_id)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, status=500)

        elif path == '/api/generate_cover':
            try:
                if not access_ok(self):
                    self._send_json({'error': '访问码无效或缺失'}, status=401)
                    return
                if not rate_ok(_client_ip(self), 'cover'):
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
                api_size = gpt_image_pixel_size(size) if final_model == 'gpt-image-2' else size

                payload = {
                    'model': final_model,
                    'prompt': prompt,
                    'size': api_size,
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

                body_bytes = self._body_bytes

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
            self._send_json({'error': 'Not found'}, status=404)


def sync_project_manifest_with_disk(project_dir):
    """按项目目录持锁调用真正的实现。这里整个扫描+改写是一轮跨越目录 I/O 的
    read-modify-write（读旧 manifest → 扫盘对比 → 整体重写 frames/videos 列表），
    与帧渲染 worker 逐帧 write_manifest() 用的是同一把 manifest_lock——否则谁
    用陈旧快照后写谁就会把对方刚落盘的帧整体覆盖掉（帧图片"无故被覆盖或丢失"
    的成因之一：本函数此前直接 open() 读文件，完全绕过了这把锁）。"""
    with manifest_lock(project_dir):
        return _sync_project_manifest_with_disk_locked(project_dir)


def _sync_project_manifest_with_disk_locked(project_dir):
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
    bootstrap_fx_runtime()
    # sys.stdout is None under pythonw (no console); guard prints so the server
    # also runs cleanly headless, while still logging when launched with python.exe.
    if sys.stdout:
        print(f"Starting SPARK server on port {PORT}...")
        print(f"Persisted library file will be saved at: {os.path.abspath(DB_FILE)}")
        # 技能契约文件缺失时旧行为是静默降级为空契约，生成质量悄悄劣化——
        # 启动时显式列出缺失文件，便于第一时间发现。清单是 server_common 里
        # SKILL_CONTRACT_FILES（合成真正会读的全部 8 个文件），此前这里只查了
        # 其中 2 个，另外 6 个缺失时全程无声。
        _skill = skill_contract_report()
        print(f"Skill contract source: {_skill['dir']} (来源: {_skill['source']})")
        if _skill['missing']:
            print(f"[WARN] 技能契约文件缺失 {len(_skill['missing'])}/{_skill['total']} 个，"
                  f"提示词合成将降级运行（形态矩阵/提示词模板/一致性协议按空契约处理）：")
            for rel in _skill['missing']:
                print(f"[WARN]   缺失: {os.path.join(_skill['dir'], rel)}")
            print('[WARN] 修复：在 server_config.json 里加一行 "skillDir": "技能包目录的本地路径"'
                  "（支持 ~ 与相对路径，改完不用重启，下一次激发/合成即生效），"
                  "或设环境变量 SKILL_DIR（优先级更高），或直接把契约文件补进上面这个目录。")
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
