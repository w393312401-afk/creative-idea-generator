"""爆款延时视频「1:1 复刻 + 二创」流水线。

方案见 docs/replica_and_variant_pipeline_plan.md。形态对标 stepped_pipeline.py：
一个落盘的状态机，跑到人工卡点就停，由 /api/replica/advance 推进。

  ingest         收视频、探测、去重、建 job 目录
  extract        调 skill 的 analyze_timelapse_video.py（抽帧/事件/拼贴图）
  review_frames  Pass A 逐帧客观事实（prompt_pipeline.reverse）
  cluster_beats  Pass B 事实 + change_events → timelapse_beats.json
  review_beats   ⏸ 唯一必须有的人工卡点
  compose        beats 作为 Tier 3 绑定简报喂进现有合成器
  audit          banned_elements 门禁
  completed

二创从 review_beats 之后分叉：mutate → compose → audit，复用同一状态机。

与 stepped_pipeline 的一个关键差异：这条线**不渲染任何图**。用户要的是提示词，
渲染由既有的分步管线接手（beats 已经把拍数锁死，接上去就是 1:1）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime

import server_common
from server_common import skill_dir

STAGES = [
    'ingest',
    'extract',
    'confirm_cost',    # ⏸ 抽帧完成、预估已知，等用户确认采样档位再开始烧钱
    'review_frames',
    'cluster_beats',
    'review_beats',    # ⏸ PAUSE：对着证据帧核对节拍
    'mutate_beats',    # 二创分支
    'compose',
    'audit',
    'audit_failed',    # ⏸ banned 门禁命中：不入库、不算完成，等用户重写后重跑
    'completed',
    'cancelled',
]

REVIEW_STAGES = {'confirm_cost', 'review_beats', 'audit_failed'}

# stage → 中文标签的唯一真源。
#
# 这份映射此前在前端抄了两份（js/replica_pipeline.js 的 REPLICA_STAGE_LABELS 和
# js/projects.js 的 PROJECT_STAGE_LABELS），本仓库在 contract registry 上已经吃过一次
# "两份长得一样但互不相关"的亏。加一个 stage 要记得改三处，漏一处就在工作台上露出
# `cluster_beats` 这种内部名。现在后端随 job 行一起下发 stage_label，前端只留一份兜底。
STAGE_LABELS = {
    'ingest': '已上传',
    'extract': '抽帧中',
    'confirm_cost': '待确认成本',
    'review_frames': '逐帧读取',
    'cluster_beats': '聚类节拍',
    'review_beats': '待人工核对',
    'mutate_beats': '二创改写中',
    'compose': '合成提示词',
    'audit': '门禁校验',
    'audit_failed': '门禁未过',
    'completed': '已完成',
    'cancelled': '已取消',
}

# 用户看得见的四个阶段。后台十二个 stage 是状态机的内部粒度，摆在 UI 上只会让人
# 对着一个 chip 找不到对应区块——「聚类节拍」在页面上没有任何一块是它。
PHASES = [
    ('material', '素材', ('ingest', 'extract', 'confirm_cost')),
    ('reverse', '反推', ('review_frames', 'cluster_beats', 'mutate_beats')),
    ('review', '核对节拍', ('review_beats',)),
    ('deliver', '交付', ('compose', 'audit', 'audit_failed', 'completed')),
]


def stage_label(stage):
    return STAGE_LABELS.get(stage, stage or '')


def phase_of(stage):
    for key, _label, stages in PHASES:
        if stage in stages:
            return key
    return 'material'


def stage_catalog():
    """给前端的一份阶段目录。前端据此渲染阶梯指示，不再自己抄一份 stage 列表。"""
    return {
        'stages': list(STAGES),
        'labels': dict(STAGE_LABELS),
        'review_stages': sorted(REVIEW_STAGES),
        'phases': [{'key': k, 'label': lab, 'stages': list(s)} for k, lab, s in PHASES],
    }


_JOBS_DIRNAME = 'replica_jobs'
_STATE_FILENAME = '.replica_pipeline.json'

# 抽帧脚本的默认参数就是为延时调过的（见脚本 docstring），不要在这里再调低。
# 送审档位（degraded / plan / all）收窄或放开的是**送多少帧给模型**，那是 Pass A
# 一侧的事，与这里的抽帧密度是两个独立旋钮：抽帧决定「一共有多少帧可选」。
_ANALYZER_TIMEOUT_SEC = 1800

# 抽帧密度档位：基线 fps → (base_fps, dense_fps)。
#
# 为什么要能选：脚本默认 2fps 基线，一秒钟只留两张，慢工序（刮腻子、铺砖）在两张
# 之间就跨过了半个工序，Pass A 再怎么读也读不出中间发生了什么。密采只在「状态跳变
# ±0.5s」窗口里生效，跳变检测漏掉的渐变过程它救不了。抽帧是本地 ffmpeg，不花模型
# 钱——真正花钱的是送审档位，所以这一档可以放心往上调。
#
# dense_fps 跟着基线走（三倍、封顶 12）：基线抬上去之后还留着 6fps 的密采窗，等于
# 密采不再"密"，跳变点的分辨率反而被基线追平。
EXTRACT_FPS_CHOICES = (1.0, 2.0, 3.0, 4.0, 6.0)
DEFAULT_EXTRACT_FPS = 2.0


def normalize_extract_fps(value=None):
    """请求里的基线 fps → 档位表里的合法值。认不出来的一律回默认档。"""
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return DEFAULT_EXTRACT_FPS
    if fps not in EXTRACT_FPS_CHOICES:
        # 不做四舍五入到最近档：静默改档位比明确回落到默认档更难查。
        return DEFAULT_EXTRACT_FPS
    return fps


def _dense_fps_for(base_fps):
    return min(12.0, round(base_fps * 3, 3))


# ── job 目录与状态 ───────────────────────────────────────────────────────────

def jobs_root():
    # OUTPUT_ROOT 在测试里会被 patch，必须每次读模块属性而不是 import 时快照。
    return os.path.join(server_common.OUTPUT_ROOT, _JOBS_DIRNAME)


def job_dir(job_id):
    return os.path.join(jobs_root(), job_id)


def _state_path(job_id):
    return os.path.join(job_dir(job_id), _STATE_FILENAME)


def _load_state(job_id):
    path = _state_path(job_id)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_state(state):
    path = _state_path(state['job_id'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state['updated_at'] = datetime.now().isoformat()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return state


def job_display_name(job_id, state=None):
    """复刻任务在工作台/任务列表上的人话名字。

    优先级：compose 定下的标题 > 源视频文件名 > job_id 尾号。job 刚建出来时还没有
    标题（title 要跑到 compose 才写），但 video_name 从 ingest 起就在，足够用户认
    出这行是哪条素材——把裸 job_id 摆在工作台上等于没有名字。
    """
    if state is None:
        try:
            state = _load_state(job_id) or {}
        except Exception:
            state = {}
    state = state or {}
    name = (state.get('title') or '').strip()
    if not name:
        video_name = (state.get('video_name') or '').strip()
        name = os.path.splitext(video_name)[0] if video_name else ''
    if not name:
        name = f"未命名素材 {str(job_id or '')[-6:]}"
    return f"{'二创' if state.get('variant_of') else '复刻'}·{name}"


def list_replica_jobs():
    root = jobs_root()
    if not os.path.isdir(root):
        return []
    rows = []
    for name in os.listdir(root):
        state = _load_state(name)
        if not state:
            continue
        rows.append({
            'job_id': state.get('job_id'),
            'stage': state.get('stage'),
            'stage_label': stage_label(state.get('stage')),
            'phase': phase_of(state.get('stage')),
            'video_name': state.get('video_name'),
            'title': state.get('title'),
            'variant_of': state.get('variant_of'),
            'beat_count': len(((state.get('beats') or {}).get('beats')) or []),
            'error': state.get('error'),
            'created_at': state.get('created_at'),
            'updated_at': state.get('updated_at'),
        })
    rows.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
    return rows


def _sha256(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def find_job_by_video_hash(video_hash):
    """同一条视频不必重抽帧。抽帧是几分钟的 ffmpeg，重传一次就重跑一遍纯属浪费。"""
    for row in list_replica_jobs():
        state = _load_state(row['job_id'])
        if state and state.get('video_sha256') == video_hash and state.get('stage') != 'cancelled':
            return state
    return None


# ── ingest ───────────────────────────────────────────────────────────────────

def ingest_video(file_bytes, filename, config=None):
    """落盘视频、探测时长、按内容哈希去重，返回初始 state（stage=ingest）。

    还没开始烧钱，所以这一步同步跑完再回给前端；extract 之后才是后台任务。
    """
    os.makedirs(jobs_root(), exist_ok=True)
    job_id = f'replica_{uuid.uuid4().hex[:12]}'
    directory = job_dir(job_id)
    os.makedirs(directory, exist_ok=True)

    safe_name = os.path.basename(filename or 'source.mp4').replace(os.sep, '_')
    if not os.path.splitext(safe_name)[1]:
        safe_name += '.mp4'
    video_path = os.path.join(directory, safe_name)
    with open(video_path, 'wb') as f:
        f.write(file_bytes)

    video_hash = _sha256(video_path)
    existing = find_job_by_video_hash(video_hash)
    if existing:
        # 已经抽过帧的同一条视频：丢掉刚建的目录，直接把老 job 还回去。
        shutil.rmtree(directory, ignore_errors=True)
        existing['reused'] = True
        return existing

    state = {
        'job_id': job_id,
        'stage': 'ingest',
        'video_path': video_path,
        'video_name': safe_name,
        'video_sha256': video_hash,
        'media': _probe(video_path),
        'overview': None,
        'cost_estimate': None,
        'sampling': None,        # 抽帧密度，run_extract 定下来（EXTRACT_FPS_CHOICES）
        'review_scope': None,    # 送审档位，run_reverse 定下来（reverse.REVIEW_SCOPES）
        'degraded': False,
        'facts': None,
        'beats': None,
        'validation': [],
        'title': None,
        'prompt_block': None,
        'banned_hits': [],
        'variant_of': None,
        'mutation_axes': [],
        'error': None,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
    }
    return _save_state(state)


def _probe(video_path):
    """时长/分辨率/帧率。探测失败不是致命错误——抽帧脚本自己还会再探一次。"""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'format=duration:stream=width,height,avg_frame_rate',
             '-of', 'json', video_path],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(out)
        stream = next((s for s in data.get('streams') or [] if s.get('width')), {})
        return {
            'duration_sec': round(float((data.get('format') or {}).get('duration') or 0), 3),
            'width': stream.get('width'),
            'height': stream.get('height'),
            'frame_rate': stream.get('avg_frame_rate'),
        }
    except Exception as e:
        if sys.stdout:
            print(f'[REPLICA] ffprobe 探测失败（非致命）: {e}')
        return {}


# ── extract ──────────────────────────────────────────────────────────────────

def _analyzer_script():
    path = os.path.join(skill_dir('omni'), 'scripts', 'analyze_timelapse_video.py')
    if not os.path.exists(path):
        raise FileNotFoundError(f'找不到抽帧脚本: {path}')
    return path


def _purge_extract_products(directory):
    """重抽帧之前把上一轮的帧与帧事实清掉。

    送审帧的文件名是 `review_001.png` 这种**序号**，不是时间戳：换了抽帧密度，
    同一个名字指向的是另一个时刻的画面。留着旧的 `.frame_facts_cache.json`，
    Pass A 会按名字命中缓存，于是把上一档某一秒的观察安在这一档另一秒的帧上——
    整条节拍阶梯建在错位的事实上，而且全程没有任何报错。
    """
    shutil.rmtree(os.path.join(directory, 'review_frames'), ignore_errors=True)
    for name in ('.frame_facts_cache.json', 'frame_facts.json'):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def run_extract(state, on_progress=None, base_fps=None):
    """调 skill 自带的抽帧脚本。不重写它——它已经把延时特有的采样纪律做完了。

    `base_fps` 是基线抽帧密度（见 EXTRACT_FPS_CHOICES）。不传就沿用这条 job 上
    次用过的档位，没跑过就用默认档。
    """
    base = normalize_extract_fps(
        base_fps if base_fps is not None else (state.get('sampling') or {}).get('base_fps'))
    dense = _dense_fps_for(base)
    state['stage'] = 'extract'
    state['sampling'] = {'base_fps': base, 'dense_fps': dense}
    _save_state(state)
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'extract',
            'message': f'正在抽帧：基线 {base:g}fps + 状态跳变密采 {dense:g}fps + '
                       f'首尾密采（视频越长越久）…',
        })

    directory = job_dir(state['job_id'])
    _purge_extract_products(directory)
    proc = subprocess.run(
        [sys.executable, _analyzer_script(),
         '--video', state['video_path'], '--output-dir', directory,
         '--base-fps', str(base), '--dense-fps', str(dense)],
        capture_output=True, text=True, timeout=_ANALYZER_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'抽帧脚本失败（exit {proc.returncode}）:\n{(proc.stderr or "")[-2000:]}')

    overview_path = os.path.join(directory, 'video_overview.json')
    with open(overview_path, 'r', encoding='utf-8') as f:
        overview = json.load(f)

    # 拼贴图是门禁不是便利（SKILL.md 明说）。手工跑时人会看到 stdout 的 FAILED，
    # 产品化后没人看 stdout —— 必须在这里转成硬失败，否则就会出现「没见过整条序列
    # 就开始定义节拍」这种最不该发生的事。
    collage = overview.get('keyframe_collage')
    if not collage or not os.path.exists(collage):
        raise RuntimeError(
            '关键帧拼贴图生成失败。它是节拍映射的前置门禁：没有它，等于没看过整条'
            '序列就要定义节拍。请先修复 ffmpeg 环境再重跑抽帧。')

    from prompt_pipeline import reverse
    state['overview'] = {
        'path': overview_path,
        'collage': collage,
        'duration_sec': (overview.get('media_metadata') or {}).get('duration_sec'),
        'change_event_count': overview.get('change_event_count'),
        'frame_count': (overview.get('review_sampling') or {}).get('frame_count'),
        'analysis_plan': overview.get('analysis_plan'),
        'pace_metrics': overview.get('pace_metrics'),
        'contact_sheets': (overview.get('review_sampling') or {}).get('contact_sheets') or [],
        'sampling': dict(state['sampling']),
    }
    # 三档都算：用户要在确认卡点上比的就是「多花多少钱换多少帧」。键名 full 保持不动
    # （老状态文件与前端都在读它），它对应的是 analysis_plan 那一档。
    state['cost_estimate'] = {
        'degraded': reverse.estimate_pass_a_cost(overview, scope='degraded'),
        'full': reverse.estimate_pass_a_cost(overview, scope='plan'),
        'all': reverse.estimate_pass_a_cost(overview, scope='all'),
    }
    # 停在成本确认卡点，不直接往 Pass A 走。
    #
    # 方案 §2.1/§4 一直要求「预估摆给用户确认再开跑」，但在此之前 extract 结束后是
    # 直接续跑 Pass A 的，预估只作为一行 SSE 文案闪过去。结果是首跑永远走完整档，
    # UI 上那对「完整 / 降级」单选框只有重试时才有机会被看见——一道写在文档里、
    # 代码里不存在的卡点。Pass A 是整条线唯一的大额支出，它前面必须真的有个停顿。
    state['stage'] = 'confirm_cost'
    _save_state(state)

    if on_progress:
        plan = state['overview']['analysis_plan'] or {}
        full = (state['cost_estimate'] or {}).get('full') or {}
        every = (state['cost_estimate'] or {}).get('all') or {}
        on_progress('replica_stage', {
            'stage': 'confirm_cost',
            'message': (f'抽帧完成（基线 {base:g}fps）：{state["overview"]["frame_count"]} 帧、'
                        f'{state["overview"]["change_event_count"]} 个变化事件；'
                        f'计划档送审 {plan.get("required_count")} 帧'
                        f'（约 {full.get("batch_count", 0)} 次视觉调用），'
                        f'全部档送审 {every.get("frame_count", 0)} 帧'
                        f'（约 {every.get("batch_count", 0)} 次）。'
                        f'选好采样档位再开始反推——这一步是整条线的成本大头。'),
            'collage': collage,
            'cost_estimate': state['cost_estimate'],
        })
    return state


# ── review_frames + cluster_beats ────────────────────────────────────────────

def run_reverse(state, config, on_progress=None, degraded=False, scope=None):
    """Pass A + Pass B，跑完停在 review_beats 人工卡点。

    `scope` 是送审档位（reverse.REVIEW_SCOPES）；`degraded` 只是它的老写法，留着
    是因为磁盘上的老 job 状态还在用它。两个都给以 scope 为准。
    """
    from prompt_pipeline import reverse

    scope = reverse.normalize_review_scope(scope, degraded)
    directory = job_dir(state['job_id'])
    state['stage'] = 'review_frames'
    state['review_scope'] = scope
    state['degraded'] = scope == 'degraded'
    _save_state(state)

    facts = reverse.extract_frame_facts(config, directory, on_progress=on_progress,
                                        scope=scope)
    facts = reverse.verify_peak_frames(config, directory, facts, on_progress=on_progress)
    state['facts'] = {
        'path': os.path.join(directory, 'frame_facts.json'),
        'frame_count': facts.get('frame_count'),
        'model': facts.get('model'),
        'peak_verify_model': facts.get('peak_verify_model'),
        'peak_verified': facts.get('peak_verified', 0),
        'scope': facts.get('scope'),
        'degraded': facts.get('degraded'),
    }
    state['stage'] = 'cluster_beats'
    _save_state(state)

    beats = reverse.cluster_beats(config, directory, facts_payload=facts, on_progress=on_progress)
    beats['pipeline_id'] = state['job_id']
    # 人工卡点是整条链路上唯一拦得住幻觉的地方，而卡点上摆的是一屏英文——看不懂就核对
    # 不了，那道闸等于没有。补一份中文对照（英文仍是唯一事实源，见 translate_beats）。
    # 放在这里而不是 cluster_beats 里面：Pass B 的契约是「事实 → 节拍阶梯」，中文对照是
    # 这个 app 的界面需要，手工跑 Tier 4 的人并不需要它。
    reverse.translate_beats(config, beats, on_progress=on_progress)
    _write_beats(state, beats)

    state['stage'] = 'review_beats'
    _save_state(state)

    if on_progress:
        errors = [v for v in state['validation'] if v.get('level') == 'error']
        on_progress('replica_stage', {
            'stage': 'review_beats',
            'message': (f'节拍阶梯已生成：{len(beats.get("beats") or [])} 拍，'
                        f'{len(errors)} 项硬伤待处理。请对着证据帧核对后再合成提示词。'),
            'beats': beats,
            'validation': state['validation'],
        })
    return state


def _write_beats(state, beats):
    path = os.path.join(job_dir(state['job_id']), 'timelapse_beats.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(beats, f, ensure_ascii=False, indent=2)
    state['beats'] = beats
    state['validation'] = beats.get('validation') or []
    return path


def save_beats(job_id, beats):
    """人工卡点上保存用户改过的节拍，并立即重跑一遍校验。

    用户拆合了拍就可能拆出新的事件覆盖漏洞，所以不能只存不验。"""
    from prompt_pipeline import reverse

    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    if not isinstance(beats, dict) or not beats.get('beats'):
        raise ValueError('beats 不能为空')

    directory = job_dir(job_id)
    with open(os.path.join(directory, 'video_overview.json'), 'r', encoding='utf-8') as f:
        overview = json.load(f)

    reverse._renumber_beats(beats)
    beats['pipeline_id'] = job_id
    beats['validation'] = reverse.validate_beats(beats, overview)
    beats['edited_by_user'] = True
    # 改过的英文字段，其中文对照当场作废（见 prune_stale_translations）。前端只回传
    # 英文字段，所以 zh 是从上一版原样带回来的——不清理就会出现「中文还是旧的」。
    reverse.prune_stale_translations(state.get('beats'), beats)
    _write_beats(state, beats)
    return _save_state(state)


def translate_job_beats(config, job_id, on_progress=None):
    """给已有的节拍阶梯补/重做一份中文对照。

    存量任务（2026-08-11 之前跑的）的 beats 里没有 `zh`，卡点上只有英文；用户手改过
    英文之后对照也会被作废。两种情况都由这里补回来。
    """
    from prompt_pipeline import reverse

    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    beats = state.get('beats')
    if not beats or not beats.get('beats'):
        raise ValueError('还没有节拍阶梯')
    translated = reverse.translate_beats(config, beats, on_progress=on_progress)
    _write_beats(state, beats)
    _save_state(state)
    return state, translated


# ── 二创 ─────────────────────────────────────────────────────────────────────

def run_mutate(state, config, axis_spec, on_progress=None):
    """从已确认的 beats 派生一个变体 job。原 job 保持不动，可以反复派生。"""
    from prompt_pipeline import reverse

    source_beats = state.get('beats')
    if not source_beats:
        raise ValueError('还没有节拍阶梯，无法二创')

    variant_id = f'replica_{uuid.uuid4().hex[:12]}'
    variant_dir = job_dir(variant_id)
    os.makedirs(variant_dir, exist_ok=True)
    # 变体要能显示参考帧，但不该复制几百张 PNG——直接指回源 job 的目录。
    src_dir = job_dir(state['job_id'])
    for name in ('video_overview.json',):
        src = os.path.join(src_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(variant_dir, name))

    variant_state = {
        'job_id': variant_id,
        'stage': 'mutate_beats',
        'video_path': state.get('video_path'),
        'video_name': state.get('video_name'),
        'video_sha256': None,   # 变体不参与视频去重，否则会顶掉源 job
        'media': state.get('media'),
        'overview': state.get('overview'),
        'cost_estimate': None,
        'sampling': state.get('sampling'),
        'review_scope': state.get('review_scope'),
        'degraded': state.get('degraded'),
        'facts': state.get('facts'),
        'beats': None,
        'validation': [],
        'title': None,
        'prompt_block': None,
        'banned_hits': [],
        'variant_of': state['job_id'],
        'source_frames_dir': src_dir,
        'mutation_axes': list(axis_spec.get('axes') or []),
        'mutation_brief': axis_spec.get('brief') or '',
        'error': None,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
    }
    _save_state(variant_state)

    variant_beats = reverse.mutate_beats(config, source_beats, axis_spec, on_progress=on_progress)
    variant_beats['pipeline_id'] = variant_id
    # 二创改写过的英文字段，其中文对照刚被 _merge_variant 作废（见那里的注释）；补回来，
    # 否则变体的卡点又退回一屏英文。与 run_reverse 同一个理由、同一个位置。
    reverse.translate_beats(config, variant_beats, on_progress=on_progress)
    _write_beats(variant_state, variant_beats)

    variant_state['stage'] = 'review_beats'
    _save_state(variant_state)

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'review_beats',
            'message': (f'二创节拍已生成（{len(variant_beats.get("beats") or [])} 拍，'
                        f'变异轴：{"、".join(reverse.MUTATION_AXES[a] for a in variant_state["mutation_axes"])}）。'
                        f'请核对后再合成提示词。'),
            'job_id': variant_id,
            'beats': variant_beats,
            'validation': variant_state['validation'],
        })
    return variant_state


# ── compose + audit ──────────────────────────────────────────────────────────

def run_compose(state, config, dimensions=None, on_progress=None):
    """beats → 提示词包。走既有合成器，一行合成逻辑都不重写。

    beat_outline 非空会让 compose_anchor_and_packet 切进「清单一比一还原」——拍数
    锁死为节拍数，不再走自适应密度公式。这正是复刻要的语义。
    """
    from prompt_pipeline import reverse
    from prompt_pipeline import compose_anchor_and_packet, compose_remaining_beats

    beats = state.get('beats')
    if not beats:
        raise ValueError('还没有节拍阶梯，无法合成提示词')

    errors = [v for v in (state.get('validation') or []) if v.get('level') == 'error']
    if errors:
        raise RuntimeError(
            f'节拍阶梯还有 {len(errors)} 项硬伤未处理，先在审核卡点上修掉再合成：'
            + '；'.join(v['message'] for v in errors[:3]))

    state['stage'] = 'compose'
    _save_state(state)

    dims = reverse.beats_to_dimensions(beats, dimensions)
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': f'正在按 {len(beats.get("beats") or [])} 拍阶梯合成提示词（拍数已锁死）…',
        })

    try:
        compose_state = compose_anchor_and_packet(config, dims, on_progress=on_progress)
    except Exception as e:
        raise _translate_compose_failure(e, beats) from e
    state['title'] = compose_state.get('title')
    prompt_block = compose_remaining_beats(config, compose_state, on_progress=on_progress)
    state['prompt_block'] = prompt_block
    _write_compose_state(state, compose_state)
    _save_state(state)

    return run_audit(state, on_progress=on_progress)


def _compose_state_path(job_id):
    return os.path.join(job_dir(job_id), 'compose_state.json')


def _write_compose_state(state, compose_state):
    """把 Phase 1 的产物落盘，供「送去渲染」时原样接手。

    分步管线的 `start_stepped_pipeline` 自己会调一遍 `compose_anchor_and_packet`。若只把
    dimensions 递过去，它会**重新合成一遍**：既白付一次 Phase 1 的钱，更要命的是渲染出来
    的提示词不是这里通过 banned 门禁的那一份——审的是 A，渲的是 B，那道门禁也就白设了。
    所以这里存下 packet / ladder / 已编译提示词，交接时跳过重合成。

    packet 与 ladder 加起来几百 KB，单独一个文件，不塞进 .replica_pipeline.json——
    那份状态每推进一步都要整体重写，驮着这些东西会把每次 save 都变成一次大写盘。
    """
    payload = {k: compose_state.get(k) for k in (
        'theme', 'total_beats', 'parsed_brief', 'title', 'beat_ladder', 'packet',
        'brief_fingerprint', 'image_1_prompt', 'compiled_images', 'compiled_videos')}
    path = _compose_state_path(state['job_id'])
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        state['compose_state_path'] = path
    except (OSError, TypeError, ValueError) as e:
        # 存不下不该让整单失败——提示词已经拿到了，交接退回重合成那条路（贵，但能走）。
        if sys.stdout:
            print(f'[REPLICA] compose_state 落盘失败（非致命，交接将重新合成）: {e}')
        state['compose_state_path'] = None
    return state.get('compose_state_path')


def load_compose_state(job_id):
    """交接时读回 Phase 1 产物；读不到返回 None（调用方退回重新合成）。"""
    path = _compose_state_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    # compiled_images / compiled_videos 在 compose 里是 {int: str}，过一趟 JSON 会变成
    # {"1": str}。分步管线按整数下标取用，不还原回去会在第一拍就 KeyError。
    for key in ('compiled_images', 'compiled_videos'):
        slots = data.get(key)
        if isinstance(slots, dict):
            data[key] = {int(k): v for k, v in slots.items() if str(k).lstrip('-').isdigit()}
    return data if data.get('packet') and data.get('beat_ladder') else None


def handoff_to_render(job_id):
    """「送去分步管线渲染」要用的三样东西：dimensions、项目标题、已合成的 Phase 1 产物。

    只允许已经过 banned 门禁的任务往下走——`audit_failed` 的提示词里带着原片没有的
    东西，渲出来就是幻觉画面，那正是那道门禁要拦的。
    """
    from prompt_pipeline import reverse

    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    if state.get('stage') == 'audit_failed':
        raise ValueError(
            f'这一单命中了禁用元素（{"、".join((state.get("banned_hits") or [])[:5])}），'
            f'提示词里有原片不存在的东西，不能直接拿去渲染。先重写后重跑合成。')
    if not state.get('prompt_block'):
        raise ValueError('还没有提示词包，先完成合成再送去渲染')

    beats = state.get('beats') or {}
    dims = reverse.beats_to_dimensions(beats)
    return dims, (state.get('title') or job_display_name(job_id, state)), load_compose_state(job_id)


# 合成器预检报错（英文，逐条以 " | " 分隔）→ 中文说法 + 用户照着能做的动作。
# 每条规则写成 (匹配正则, 中文模板)；模板里的 {beat} 是合成器报的拍号。
# 口径的权威来源是 prompt_pipeline/frame_state.py 与 scene_state.py，这里只翻译，
# 不复制判据——复制一份迟早和本体漂开（见 _validate_composer_frame_contract 同款说明）。
_COMPOSE_FAILURE_RULES = (
    (r'Beat (?P<beat>\d+) declares (?P<n>\d+) operations',
     '第 {beat} 拍只申报了 {n} 道工序，合成器要求每拍 2~3 道紧密耦合、共同产出同一成果的'
     '工序。在「工序包」里补齐这一拍真实做过的其余工序，或把它与相邻的同一里程碑并成一拍。'),
    (r'Beat (?P<beat>\d+) declares fewer than two visible persistent traces',
     '第 {beat} 拍的「遗留痕迹」少于两条。补上这一拍在画面里留下、后续帧必须继承的两条可见痕迹。'),
    (r'Beat (?P<beat>\d+) repeats the preceding terminal state',
     '第 {beat} 拍的「结束状态」和上一拍逐字相同，等于这一拍什么都没推进。把它改写成本拍自己的终产物。'),
    (r'Beat (?P<beat>\d+) explicitly regresses',
     '第 {beat} 拍把已经完工的面又写回了裸露/缺失状态。除非这一拍真的是拆除，否则改掉这句倒退描述。'),
    (r'Beat (?P<beat>\d+) frame-state contract is missing: (?P<fields>[^.]+)',
     '第 {beat} 拍缺字段：{fields}。这几项是合成器排状态账的依据，必须逐项填上。'),
    (r'Beat (?P<beat>\d+) changes more than three composition cells',
     '第 {beat} 拍一次改动了三格以上的画面区域，锁死机位下读不出单一变化。拆成两拍。'),
    (r'Beat (?P<beat>\d+) is out of sequence',
     '第 {beat} 拍的编号与它在阶梯里的位置对不上。保存一次节拍阶梯会自动重编号。'),
)


def _translate_compose_failure(error, beats_doc=None):
    """把合成器的预检报错翻译成「用户照着能改」的话。

    2026-08-10：原先这里的第一分支是一段专门解释「清场规则打回照实复刻」的道歉文案。
    那条冲突已经在源头修掉了——`dimensions.reverse_engineered` 会让
    `validate_scene_states` 豁免清场那一条，复刻单不再撞它，所以那段翻译连同它要翻译的
    报错一起没有了。剩下的分支只兜底真正的状态账错误（时序、before 承接、重复移除），
    那些对复刻单同样是真缺陷，该报就报。

    2026-08-11：改成**逐条**翻译，并把合成器的拍号映射回用户在卡点上看得见的拍 ID
    （B01…）。此前只是把整串英文原样贴在一句中文导语后面：用户被告知"按报错指的拍号
    调整"，而报错里那个 "Beat 9" 既不是他看到的编号体系，报的规则也全是英文。
    """
    text = str(error)
    if 'Structured scene-state preflight' not in text and 'frame state' not in text.lower():
        return error

    raw = text.split('preflight rejected the beat ladder before prompt generation:', 1)[-1]
    beats = (beats_doc or {}).get('beats') or []
    lines, untranslated = [], []
    for item in [x.strip() for x in raw.split(' | ') if x.strip()]:
        for pattern, template in _COMPOSE_FAILURE_RULES:
            match = re.search(pattern, item)
            if not match:
                continue
            fields = match.groupdict()
            line = template.format(**fields)
            # 合成器的拍号是 1 起的阶梯下标；1:1 复刻下它与节拍阶梯逐位对应，
            # 把用户在卡点上认得的 B0x 一并标出来。
            try:
                beat = beats[int(fields.get('beat')) - 1]
            except (TypeError, ValueError, IndexError):
                beat = None
            if isinstance(beat, dict) and beat.get('id'):
                line = f'{line}（对应节拍阶梯的 {beat["id"]}）'
            lines.append(line)
            break
        else:
            untranslated.append(item)

    if not lines and not untranslated:
        return error
    body = '\n'.join(f'· {x}' for x in lines + untranslated)
    return RuntimeError(
        f'合成器的结构化状态预检打回了这条阶梯，共 {len(lines) + len(untranslated)} 项。'
        f'回到「节拍阶梯」按下面每一条改，改完保存会立刻重校验：\n{body}')


def _publish_to_library(state):
    """把提示词包写进创意库，让它在「项目」工作台里直接可见。

    不入库的话，提示词只活在复刻页的这一个 job 里——项目工作台是按 library / 台账 /
    任务 / outputs 四路合流出来的，复刻这条线不在任何一路上，等于产出了看不见的东西。
    入库失败不影响交付：提示词已经在 job 状态里了，这里只是多一个入口。
    """
    if not state.get('prompt_block'):
        return None
    try:
        from server_common import write_library_item
        # job_id 本身就以 replica_ 开头，别再套一层。
        item_id = state['job_id']
        kind = (f'二创变体（{"、".join(state.get("mutation_axes") or [])}）'
                if state.get('variant_of') else '爆款 1:1 复刻')
        item = {
            'id': item_id,
            'title': _library_title(state, kind),
            # theme 在项目工作台里被当成项目身份的别名（build_projects_index 用
            # title/theme 的变体互相挂接）。这里如果写死一个 "爆款 1:1 复刻"，所有
            # 复刻任务会共享同一个别名、在工作台上塌成一行，后写的把先写的标题顶掉。
            # 所以必须带上本单独有的信息。
            'theme': f'{kind} · {state.get("video_name") or state["job_id"]}',
            'prompt_block': state['prompt_block'],
            'timestamp': datetime.now().isoformat(),
            'source': 'replica',
            'replica_job_id': state['job_id'],
            'replica_variant_of': state.get('variant_of'),
            'source_video': state.get('video_name'),
            'collage_url': _outputs_url(state, (state.get('overview') or {}).get('collage')),
            'banned_hits': state.get('banned_hits') or [],
            'beat_count': len(((state.get('beats') or {}).get('beats')) or []),
        }
        write_library_item(item)
        state['library_id'] = item_id
        _save_state(state)
        return item_id
    except Exception as e:
        if sys.stdout:
            print(f'[REPLICA] 写入创意库失败（非致命，提示词仍在任务里）: {e}')
        return None


def _library_title(state, kind):
    """工作台上那行标题。

    合成器给的 title 是从「起始状态 改造为 终止状态」拼出来的，反推场景下这两段本身
    就是整句英文描述，拼出来两百多字，在项目列表里只会撑爆一行还看不出是哪一单。
    截断它，并挂上源视频名——那才是用户认得出的东西。
    """
    raw = ' '.join(str(state.get('title') or '').split())
    # 合成器会在标题末尾追加它自己起的中文名（"…改造成地下暖光隐居居所"），那才是
    # 人看得懂的那部分；前面一长串是我喂进去的英文主题。有中文尾巴就只要它。
    import re as _re
    tail = _re.search(r'([一-鿿][一-鿿\w·、，]{3,})\s*$', raw)
    if tail:
        raw = tail.group(1)
    elif len(raw) > 48:
        raw = raw[:48].rstrip(' ,.;·') + '…'
    video = state.get('video_name') or ''
    return ' · '.join(x for x in (raw or video, kind, video if raw else '') if x) or state['job_id']


def _outputs_url(state, abs_path):
    """绝对文件路径 → 前端能取到的 /outputs 相对 URL。"""
    if not abs_path:
        return None
    name = os.path.basename(abs_path)
    return f'/outputs/{_JOBS_DIRNAME}/{state.get("variant_of") or state["job_id"]}/{name}'


def run_audit(state, on_progress=None):
    """P0 门禁：banned_elements 命中即拦下交付。

    2026-08-10 之前这里扫完命中就 `_publish_to_library` + `stage='completed'`，只在文案里
    说一句"交付前必须重写"——没有任何东西拦着，提示词照样进创意库、任务照样显示已完成，
    用户拿它去渲染时画面里就长出了原片根本没有的东西。门禁不堵，就只是报告单。

    现在命中就停在 `audit_failed`：不入库、不算完成，UI 上给「按清单重写」的入口。
    这条路径与合成器侧的负面清单（composers/base.banned_elements_block）是两道，前者在
    写之前约束、后者在交付前复核；前者失手时后者必须真的拦得住，否则前者也就白加了。
    """
    from prompt_pipeline import reverse

    state['stage'] = 'audit'
    _save_state(state)

    banned = (state.get('beats') or {}).get('banned_elements') or []
    hits = reverse.banned_element_hits(state.get('prompt_block'), banned)
    state['banned_hits'] = hits

    if hits:
        state['stage'] = 'audit_failed'
        _save_state(state)
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'audit_failed',
                'message': (f'提示词包命中 {len(hits)} 个禁用元素（原片里并不存在）：'
                            f'{"、".join(hits[:5])}。已拦下交付，未写入创意库。'
                            f'去掉这些表述后重新合成，或者确认它们其实出现在原片里、'
                            f'把它们从节拍阶梯的「禁用元素」里删掉再重跑。'),
                'title': state.get('title'),
                'banned_hits': hits,
            })
        return state

    _publish_to_library(state)

    state['stage'] = 'completed'
    _save_state(state)

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'completed',
            'message': '提示词包已生成，并已写入创意库。',
            'title': state.get('title'),
            'banned_hits': [],
        })
    return state


# ── 对外入口 ─────────────────────────────────────────────────────────────────

def _begin(job_id):
    """取出 job 并清掉上次的失败标记。

    不清的话，这次跑成功了前端还挂着上一次的错误横幅。"""
    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    state['error'] = None
    _save_state(state)
    return state


def _overview_ready(job_id):
    """判据是「磁盘上有没有 video_overview.json」而不是 stage。

    抽帧中途失败的 job 停在 stage='extract'，只看 stage 会跳过重抽、直接掉进 Pass A
    报"找不到 overview"，用户永远重试不出来。"""
    return os.path.exists(os.path.join(job_dir(job_id), 'video_overview.json'))


def extract_replica_job(config, job_id, on_progress=None, base_fps=None):
    """只抽帧，停在 confirm_cost。

    与 Pass A 分成两个任务，是为了让「先看预估再决定烧不烧钱」这句话在代码里成立。
    抽帧是本地 ffmpeg，不花模型钱，所以它可以在用户没确认任何东西的情况下自己跑完。

    `base_fps` 与这条 job 上次用的密度不同时**必须**重抽：那正是用户点「按新密度
    重抽帧」的全部意思，沿用旧帧等于这个按钮什么也没做。
    """
    state = _begin(job_id)
    try:
        current = (state.get('sampling') or {}).get('base_fps')
        wants = normalize_extract_fps(base_fps) if base_fps is not None else None
        density_changed = wants is not None and normalize_extract_fps(current) != wants
        if (_overview_ready(job_id) and not density_changed
                and state.get('stage') not in ('ingest', 'extract')):
            # 已经抽过帧（例如换个送审档位回来重跑）：不重抽，直接回到确认卡点。
            state['stage'] = 'confirm_cost'
            return _save_state(state)
        return run_extract(state, on_progress=on_progress, base_fps=base_fps)
    except Exception as e:
        state['error'] = str(e)
        _save_state(state)
        raise


def start_replica_job(config, job_id, on_progress=None, degraded=False, scope=None):
    """Pass A → Pass B，停在 review_beats。抽帧没跑过就先补上。

    正常路径下 extract 已经在上一个任务里跑完、用户在 confirm_cost 上选了档位才到这里；
    `run_extract` 的兜底只为覆盖「抽帧中途失败后直接点重试」这一种情况。
    """
    state = _begin(job_id)
    try:
        if not _overview_ready(job_id) or state.get('stage') in ('ingest', 'extract'):
            state = run_extract(state, on_progress=on_progress)
        return run_reverse(state, config, on_progress=on_progress,
                           degraded=degraded, scope=scope)
    except Exception as e:
        state['error'] = str(e)
        _save_state(state)
        raise


def advance_replica_job(config, job_id, action='approve', payload=None,
                        on_progress=None):
    """推进人工卡点。

    approve  → 合成提示词（1:1 复刻）
    variant  → 派生二创变体（payload: {axes, brief}），返回新 job 的 state
    recluster→ 重跑 Pass B（Pass A 的帧事实走缓存，不重付视觉钱）
    translate→ 重做中文对照（纯文本调用，不碰英文原文，也不动 stage）
    """
    payload = payload or {}
    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    state['error'] = None
    _save_state(state)

    try:
        if action == 'approve':
            return run_compose(state, config,
                               dimensions=payload.get('dimensions'), on_progress=on_progress)
        if action == 'variant':
            return run_mutate(state, config, {
                'axes': payload.get('axes') or [],
                'brief': payload.get('brief') or '',
            }, on_progress=on_progress)
        if action == 'recluster':
            return run_reverse(state, config, on_progress=on_progress,
                               scope=state.get('review_scope'),
                               degraded=bool(state.get('degraded')))
        if action == 'translate':
            state, translated = translate_job_beats(config, job_id, on_progress=on_progress)
            if on_progress:
                on_progress('replica_stage', {
                    'stage': state.get('stage') or 'review_beats',
                    'message': (f'已更新 {translated} 拍的中文对照。'
                                if translated else '中文对照没有更新（模型没给出可用译文）。'),
                })
            return state
        raise ValueError(f'不支持的 action: {action}')
    except Exception as e:
        state['error'] = str(e)
        _save_state(state)
        raise


def frame_urls(state):
    """帧文件名 → 前端能直接用的 /outputs URL。

    此前这个映射写在前端：`replicaFrameUrl` 用 `/^scene_/` 正则猜这张帧在 storyboard
    还是 review_frames 目录下。目录布局是抽帧脚本的实现细节，前端去猜它，脚本一改
    目录名就是一片碎图，而且碎在"看证据帧核对节拍"这个最需要看图的地方。
    这里按磁盘上的实际位置解析，前端只管取。
    """
    base_job = state.get('variant_of') or state.get('job_id')
    directory = job_dir(base_job)
    urls = {}
    for sub in ('review_frames', 'storyboard'):
        folder = os.path.join(directory, sub)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            urls.setdefault(name, f'/outputs/{_JOBS_DIRNAME}/{base_job}/{sub}/{name}')
    return urls


def get_replica_status(job_id):
    state = _load_state(job_id)
    if not state:
        return None
    # beats 可能被外部（人工编辑）改过，以磁盘上的为准。
    beats_path = os.path.join(job_dir(job_id), 'timelapse_beats.json')
    if os.path.exists(beats_path):
        try:
            with open(beats_path, 'r', encoding='utf-8') as f:
                state['beats'] = json.load(f)
            state['validation'] = state['beats'].get('validation') or []
        except (OSError, ValueError):
            pass
    state['frame_urls'] = frame_urls(state)
    state['stage_label'] = stage_label(state.get('stage'))
    state['phase'] = phase_of(state.get('stage'))
    return state


def cancel_replica_job(job_id):
    state = _load_state(job_id)
    if not state:
        return None
    state['stage'] = 'cancelled'
    return _save_state(state)


def delete_replica_job(job_id):
    directory = job_dir(job_id)
    # 只允许删 jobs_root 之下的目录：job_id 来自请求体，不能让 ../ 走出去。
    if os.path.commonpath([os.path.abspath(directory), os.path.abspath(jobs_root())]) \
            != os.path.abspath(jobs_root()):
        raise ValueError('非法的 job_id')
    if os.path.isdir(directory):
        shutil.rmtree(directory, ignore_errors=True)
        return True
    return False
