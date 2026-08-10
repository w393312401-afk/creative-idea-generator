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
    'review_frames',
    'cluster_beats',
    'review_beats',    # ⏸ PAUSE：对着证据帧核对节拍
    'mutate_beats',    # 二创分支
    'compose',
    'audit',
    'completed',
    'cancelled',
]

REVIEW_STAGES = {'review_beats'}

_JOBS_DIRNAME = 'replica_jobs'
_STATE_FILENAME = '.replica_pipeline.json'

# 抽帧脚本的默认参数就是为延时调过的（见脚本 docstring），不要在这里再调低。
# 只有 degraded 模式会收窄送审帧数，那是 Pass A 一侧的事，与抽帧密度无关。
_ANALYZER_TIMEOUT_SEC = 1800


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


def run_extract(state, on_progress=None):
    """调 skill 自带的抽帧脚本。不重写它——它已经把延时特有的采样纪律做完了。"""
    state['stage'] = 'extract'
    _save_state(state)
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'extract',
            'message': '正在抽帧：基线 2fps + 状态跳变密采 + 首尾密采（视频越长越久）…',
        })

    directory = job_dir(state['job_id'])
    proc = subprocess.run(
        [sys.executable, _analyzer_script(),
         '--video', state['video_path'], '--output-dir', directory],
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
    }
    state['cost_estimate'] = {
        'full': reverse.estimate_pass_a_cost(overview, degraded=False),
        'degraded': reverse.estimate_pass_a_cost(overview, degraded=True),
    }
    state['stage'] = 'review_frames'
    _save_state(state)

    if on_progress:
        plan = state['overview']['analysis_plan'] or {}
        on_progress('replica_stage', {
            'stage': 'extract',
            'message': (f'抽帧完成：{state["overview"]["frame_count"]} 帧、'
                        f'{state["overview"]["change_event_count"]} 个变化事件；'
                        f'待送审 {plan.get("required_count")} 帧'),
            'collage': collage,
            'cost_estimate': state['cost_estimate'],
        })
    return state


# ── review_frames + cluster_beats ────────────────────────────────────────────

def run_reverse(state, config, on_progress=None, degraded=False):
    """Pass A + Pass B，跑完停在 review_beats 人工卡点。"""
    from prompt_pipeline import reverse

    directory = job_dir(state['job_id'])
    state['stage'] = 'review_frames'
    state['degraded'] = bool(degraded)
    _save_state(state)

    facts = reverse.extract_frame_facts(config, directory, on_progress=on_progress,
                                        degraded=degraded)
    facts = reverse.verify_peak_frames(config, directory, facts, on_progress=on_progress)
    state['facts'] = {
        'path': os.path.join(directory, 'frame_facts.json'),
        'frame_count': facts.get('frame_count'),
        'model': facts.get('model'),
        'peak_verified': facts.get('peak_verified', 0),
        'degraded': facts.get('degraded'),
    }
    state['stage'] = 'cluster_beats'
    _save_state(state)

    beats = reverse.cluster_beats(config, directory, facts_payload=facts, on_progress=on_progress)
    beats['pipeline_id'] = state['job_id']
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
    _write_beats(state, beats)
    return _save_state(state)


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
        raise _translate_compose_failure(e) from e
    state['title'] = compose_state.get('title')
    prompt_block = compose_remaining_beats(config, compose_state, on_progress=on_progress)
    state['prompt_block'] = prompt_block
    _save_state(state)

    return run_audit(state, on_progress=on_progress)


def _translate_compose_failure(error):
    """把合成器的预检报错翻译成「用户照着能改」的话。

    合成器的场景状态预检是为**原创**选题写的产线规则；反推来的阶梯照实描述了原片，
    两者冲突时它抛的是一句英文 preflight 报错，用户在「爆款复刻」页上看到它完全不知道
    该动哪一拍的哪个字段。这里只做翻译与指路，不修改也不绕过那道闸——它拦得对。
    """
    text = str(error)
    if 'temporary construction' in text and 'furnishing' in text:
        import re as _re
        beats = sorted(set(_re.findall(r'Beat (\d+)', text)))
        objects = sorted(set(_re.findall(r"'([^']+)'", text)))
        return RuntimeError(
            f'合成器的清场规则打回了这条阶梯：进入软装阶段的第 {"、".join(beats)} 拍，'
            f'画面里还留着临时施工物（{"、".join(objects)}），而搬家具进场时不该还有这些东西。\n'
            f'原片里它确实一直在，所以这不是读错帧——是「照实复刻」和产线规则冲突了。'
            f'回到节拍阶梯改任一处即可：\n'
            f'  · 在进入软装的那一拍之前，给某一拍的「可见动作」补上清场（例如 "工人卷起并搬走地面防护布"）；\n'
            f'  · 或者把它从相关拍的「可见细节 / 遗留痕迹」里删掉。\n'
            f'（原始报错：{text[:300]}）')
    if 'Structured scene-state preflight' in text or 'frame state' in text.lower():
        return RuntimeError(
            f'合成器的结构化状态预检打回了这条阶梯。多数情况是反推出来的拍序与产线规则冲突，'
            f'回到节拍阶梯按报错指的拍号调整后重试。\n原始报错：{text[:600]}')
    return error


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
    """P0 门禁：banned_elements 命中即交付前必须重写。"""
    from prompt_pipeline import reverse

    state['stage'] = 'audit'
    _save_state(state)

    banned = (state.get('beats') or {}).get('banned_elements') or []
    hits = reverse.banned_element_hits(state.get('prompt_block'), banned)
    state['banned_hits'] = hits

    _publish_to_library(state)

    state['stage'] = 'completed'
    _save_state(state)

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'completed',
            'message': ('提示词包已生成。' if not hits else
                        f'提示词包已生成，但命中 {len(hits)} 个禁用元素（原片里并不存在）：'
                        f'{"、".join(hits[:5])}。交付前必须重写这些表述。'),
            'title': state.get('title'),
            'banned_hits': hits,
        })
    return state


# ── 对外入口 ─────────────────────────────────────────────────────────────────

def start_replica_job(config, job_id, on_progress=None, degraded=False):
    """extract → Pass A → Pass B，停在 review_beats。"""
    state = _load_state(job_id)
    if not state:
        raise ValueError(f'找不到复刻任务 {job_id}')
    # 重试要先把上次的失败标记清掉，否则跑成功了前端还挂着旧的错误横幅。
    state['error'] = None
    _save_state(state)
    try:
        # 判据是「磁盘上有没有 video_overview.json」而不是 stage：抽帧中途失败的 job
        # 停在 stage='extract'，只看 stage 会跳过重抽、直接掉进 Pass A 报"找不到
        # overview"，用户永远重试不出来。
        overview_ready = os.path.exists(
            os.path.join(job_dir(state['job_id']), 'video_overview.json'))
        if not overview_ready or state.get('stage') in ('ingest', 'extract'):
            state = run_extract(state, on_progress=on_progress)
        return run_reverse(state, config, on_progress=on_progress, degraded=degraded)
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
                               degraded=bool(state.get('degraded')))
        raise ValueError(f'不支持的 action: {action}')
    except Exception as e:
        state['error'] = str(e)
        _save_state(state)
        raise


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
