import os
import sys
import json
import time
import shutil
import re
import subprocess
import tempfile
import threading

from server_common import (
    SERVER_CONFIG, resolve_gateway, effective_config,
    OUTPUT_ROOT, SKILL_DIR, _get_project_dir, _safe_project_name,
    ACTIVE_TASKS_LOCK, ACTIVE_TASKS, get_or_create_task,
    notify_listeners, save_tasks_to_disk, ensure_adspower_on_path,
    read_manifest, write_manifest, strict_gates_enabled, qa_gate_level
)


def _get_google_fx_video_service():
    ensure_adspower_on_path()
    import services.google_fx
    from services import google_fx_video
    import models
    return google_fx_video, models


# ── 视频锚点帧校验 ──
# 2026-07-04 复盘（loft 任务）：Flow 画布 tile 追踪在换 IP 重试后可能绑定到旧的重复
# 卡片，导致下载到错误槽位的视频甚至完全无关任务的视频（实测 vid_008 下载到了一段
# 铁路隧道视频，vid_009/vid_010 内容整体错位两个槽位）。浏览器侧无法完全杜绝，
# 因此在下载落盘后做一次内容级校验：抽取视频首帧/尾帧与该槽位的首尾锚点图对比，
# 不匹配的直接判失败并删除文件，防止串片/文生视频混入成片。
# 实测同任务匹配段 MAD 在 1.5~10.3，错位段在 28+，阈值取 18。
_ANCHOR_MAD_THRESHOLD = 18.0


def _load_gray_thumb(path, size=(64, 114)):
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        return np.asarray(im.convert('L').resize(size), dtype=np.float32)


def _extract_video_frame(video_path, out_png, position):
    """position: 'first' | 'last'。返回 True 表示抽帧成功。"""
    if position == 'first':
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-frames:v", "1", out_png]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-sseof", "-0.3", "-i", video_path,
               "-frames:v", "1", "-update", "1", out_png]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace', timeout=60)
        return res.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0
    except Exception:
        return False


def verify_video_anchors(video_path, start_frame_path, end_frame_path, strict=False):
    """校验视频首帧/尾帧是否与锚点图一致。

    返回 (ok: bool, reason: str)。校验环境异常（ffmpeg/PIL 不可用等）时默认返回
    (True, 'skipped:...')，不拦截正常流程——该校验只用来挡住明确的串片；
    strict=True（strictGates 开启）时环境异常按校验失败处理，防止环境退化
    悄悄关掉串片检测。
    """
    import tempfile
    try:
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            checks = []
            for pos, anchor in (('first', start_frame_path), ('last', end_frame_path)):
                if not anchor or not os.path.exists(anchor):
                    continue
                png = os.path.join(td, f'{pos}.png')
                if not _extract_video_frame(video_path, png, pos):
                    if strict:
                        return False, f'strict:extract_{pos}_failed（严格模式下环境异常按失败处理）'
                    return True, f'skipped:extract_{pos}_failed'
                mad = float(np.abs(_load_gray_thumb(png) - _load_gray_thumb(anchor)).mean())
                checks.append((pos, mad))
            if not checks:
                if strict:
                    return False, 'strict:no_anchor（严格模式下锚点图缺失按失败处理）'
                return True, 'skipped:no_anchor'
            bad = [(pos, mad) for pos, mad in checks if mad > _ANCHOR_MAD_THRESHOLD]
            detail = ", ".join(f"{pos}={mad:.1f}" for pos, mad in checks)
            if bad:
                return False, detail
            return True, detail
    except Exception as e:
        if strict:
            return False, f'strict:{type(e).__name__}（严格模式下环境异常按失败处理）'
        return True, f'skipped:{type(e).__name__}'


def _extract_video_mid_frames(video_path, out_dir, fractions=(0.25, 0.5, 0.75)):
    """从视频中段按时长比例抽帧（默认 25%/50%/75% 三张），返回成功抽出的帧路径列表。
    时长探测失败返回 []（调用方按环境异常处理）；个别时间点抽帧失败则跳过该张。"""
    params = _ffprobe_video_params(video_path)
    duration = (params or {}).get('duration') or 0.0
    if duration <= 0:
        return []
    paths = []
    for i, frac in enumerate(fractions):
        t = max(0.0, min(duration * frac, duration - 0.05))
        out_png = os.path.join(out_dir, f'mid_{i}.png')
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video_path,
               "-frames:v", "1", out_png]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8', errors='replace', timeout=60)
            if res.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0:
                paths.append(out_png)
        except Exception:
            continue
    return paths


def check_video_process(config, video_path, start_frame, end_frame, prompt):
    """段内过程门：verify_video_anchors 只钉住首尾两端，段内 8 秒是盲区（空心视频/
    幽灵内容的来源）。这里抽出中段帧交给 VLM（prompt_pipeline.run_video_process_check）
    判定"两锚点之间是否真的发生了描述的施工过程"，并把判定映射为动作：

    返回 (action, reason)，action ∈：
      'accept' —— 通过 / off 档跳过 / 环境·判定服务异常 fail-open 放行（reason 留痕）
      'warn'   —— lenient 档下检出硬伤：不拒收（重试要再烧一整段视频额度，宽松档
                   保留成片交用户决策），发 video_warning + manifest 留痕
      'reject' —— standard 档检出硬伤，或 strictGates 开启时环境/判定异常：删片重试
    """
    level = qa_gate_level(config)
    if level == 'off':
        return 'accept', 'Skipped (qaGateLevel=off: 质检门已关闭)'
    with tempfile.TemporaryDirectory() as td:
        mids = _extract_video_mid_frames(video_path, td)
        if not mids:
            if strict_gates_enabled(config):
                return 'reject', 'strict:mid_extract_failed（严格模式下环境异常按失败处理）'
            return 'accept', 'skipped:mid_extract_failed'
        from prompt_pipeline import run_video_process_check
        passed, reason = run_video_process_check(config, start_frame, mids, end_frame, prompt)
    if passed:
        return 'accept', reason
    if level == 'lenient':
        return 'warn', reason
    return 'reject', reason


# ════════════════════════════════════════════════════════════════════
# 帧 → 视频 生成编排（2026-07-04 重构）
# ════════════════════════════════════════════════════════════════════
# 职责划分：
#   rewrite_prompt_for_two_card_ui() —— 纯文本改写，可单测
#   load_slot_frames()               —— manifest → 槽位帧路径/质量门映射
#   plan_video_slots()               —— 纯决策：复用/生成/拦截，可单测
#   _ManifestWriter                  —— manifest.videos 增量合并落盘
#   _BatchBridge                     —— 批量脚本回调 → SPARK 进度事件 + 锚点校验拒收
#   generate_video_sequence()        —— 瘦编排器，装配以上部件调用 AdsPower 批量脚本
# 浏览器自动化在外部模块 google_fx_video.py（改动两侧任一文件都需重启 SPARK 进程）。

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _rel_url(abs_path):
    rel = os.path.relpath(abs_path, _BASE_DIR).replace('\\', '/')
    return rel, '/' + rel


def rewrite_prompt_for_two_card_ui(prompt, slot):
    """把提示词里的 IMAGE slot / IMAGE slot+1（含中文「图片 N」）改写为
    IMAGE 1 / IMAGE 2，对应 Google Labs Flow 两卡位（首帧/尾帧）UI。"""
    prompt = re.sub(rf'\bimage\s+{slot}\b', 'IMAGE 1', prompt, flags=re.IGNORECASE)
    prompt = re.sub(rf'\bimage\s+{slot + 1}\b', 'IMAGE 2', prompt, flags=re.IGNORECASE)
    prompt = re.sub(rf'图片\s*{slot}\b', 'IMAGE 1', prompt)
    prompt = re.sub(rf'图片\s*{slot + 1}\b', 'IMAGE 2', prompt)
    return prompt


def load_slot_frames(manifest_data, frames_dir, image_count):
    """从 manifest.frames 建立 槽位→帧绝对路径 与 槽位→质量门标记 的映射；
    manifest 缺失/为空时按 frames/img_NNN.webp 命名约定兜底。"""
    slot_to_path = {}
    slot_to_quality = {}
    for frame in (manifest_data or {}).get('frames', []):
        try:
            slot_to_path[frame['slot']] = os.path.join(_BASE_DIR, frame['file'].lstrip('/'))
            slot_to_quality[frame['slot']] = frame.get('quality_gate')
        except Exception:
            continue
    if not slot_to_path:
        for i in range(1, image_count + 1):
            guess_path = os.path.join(frames_dir, f'img_{i:03d}.webp')
            if os.path.exists(guess_path):
                slot_to_path[i] = os.path.abspath(guess_path)
    return slot_to_path, slot_to_quality


def load_stale_slots(manifest_data):
    """manifest.frames 里被标为 stale_lineage 的槽位集合：部分重生后仍派生自旧 i2i 链
    的帧（见 frame_generator.update_manifest_stale_status）。"""
    stale = set()
    for frame in (manifest_data or {}).get('frames', []):
        if isinstance(frame, dict) and frame.get('stale_lineage'):
            try:
                stale.add(int(frame['slot']))
            except (KeyError, TypeError, ValueError):
                continue
    return stale


def plan_video_slots(video_slots, slot_to_path, slot_to_quality, videos_dir, target_slots=None,
                      strict=False, verify_fn=None, gate_level='standard', stale_slots=None):
    """为每个视频槽位做生成决策。只读文件系统，不做任何写操作，可单测。

    video_slots: _parse_prompt_slots 的 videos 输出（{slot: str 或 {'body':...}}）
    target_slots: None=整单生成；列表=只处理这些槽位（显式重试）
    strict/verify_fn: 断点续传复用判定用，见下方注释；verify_fn 默认为
      verify_video_anchors，测试可注入假实现，避免依赖真实 ffmpeg/视频文件。

    返回按槽位升序的计划列表，每项：
      slot / seq / prompt（已改写 IMAGE 1/2）/ dest_path
      action: 'reuse'    —— 断点续传，直接复用已存在的 mp4
              'generate' —— 需要提交生成
              'blocked'  —— 前置条件不满足（缺帧/降级帧），reason 说明原因
      start_frame / end_frame: 锚点帧绝对路径
      delete_existing: 旧文件存在且即将被重新生成覆盖（显式重试，或断点续传时检测到
        与当前锚点帧不符的过期片段），调用方需先删除
    """
    if verify_fn is None:
        verify_fn = verify_video_anchors
    slots = sorted(video_slots.keys())
    if target_slots is not None:
        wanted = {int(x) for x in target_slots}
        slots = [s for s in slots if s in wanted]
    is_explicit_retry = target_slots is not None

    plans = []
    for seq, slot in enumerate(slots, start=1):
        item = video_slots[slot]
        prompt = item['body'] if isinstance(item, dict) else item
        meta = str(item.get('meta', '') if isinstance(item, dict) else '').upper()
        prompt = rewrite_prompt_for_two_card_ui(prompt, slot)
        dest_path = os.path.join(videos_dir, f'vid_{slot:03d}.mp4')
        start_p, end_p = slot_to_path.get(slot), slot_to_path.get(slot + 1)
        plan = {
            'slot': slot,
            'seq': seq,
            'prompt': prompt,
            'dest_path': dest_path,
            'start_frame': start_p,
            'end_frame': end_p,
            'delete_existing': False,
            'reason': '',
        }

        # 声明式硬切槽位（[CUT]，TBCP v2 hard_cut 变体）：切点两侧的帧不是同一机位的
        # 首尾帧对，送 i2v 只会在两张无关构图之间硬插值出扭曲变形——该槽不生成片段，
        # 合成时按"预期缺失"直接硬拼（见 merge_project_videos）。
        if 'CUT' in meta and 'BRIDGE' not in meta:
            plan['action'] = 'skip_cut'
            plan['reason'] = f"视频 {slot} 是声明式硬切槽位（[CUT]）：不生成视频片段，成片在此处直接硬切。"
            plans.append(plan)
            continue

        existing = os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
        if not is_explicit_retry and existing:
            # 断点续传：已存在的视频仍须与当前锚点帧内容一致才复用——起止帧可能在
            # 上一轮失败后被单独重渲（retrySingleFrame），旧片段这时已经过期，
            # 复用会重蹈 spark-video-mixup-postmortem 那类串片问题，须按缺失处理重渲。
            ok, verify_reason = verify_fn(dest_path, start_p, end_p, strict=strict)
            if ok:
                plan['action'] = 'reuse'
                plans.append(plan)
                continue
            plan['delete_existing'] = True
            plan['reason'] = f"已存在的片段与当前锚点帧不符（{verify_reason}），视为过期，将重新生成。"
        elif is_explicit_retry and existing:
            plan['delete_existing'] = True

        if not start_p or not os.path.exists(start_p):
            plan['action'] = 'blocked'
            plan['reason'] = f"视频 {slot} 所需的起始帧 IMAGE {slot} 不存在。请重新生成该帧！"
        elif not end_p or not os.path.exists(end_p):
            plan['action'] = 'blocked'
            plan['reason'] = f"视频 {slot} 所需的结束帧 IMAGE {slot + 1} 不存在。请重新生成该帧！"
        elif slot_to_quality.get(slot) == 'i2i_fallback_degraded' \
                or slot_to_quality.get(slot + 1) == 'i2i_fallback_degraded':
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的起始帧 IMAGE {slot} 或结束帧 IMAGE {slot + 1} 属于降级帧（i2i fallback degraded），"
                f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
            )
        elif gate_level != 'off' and (
            slot_to_quality.get(slot) in ('vlm_qa_failed', 'sequence_review_flagged')
            or slot_to_quality.get(slot + 1) in ('vlm_qa_failed', 'sequence_review_flagged')
        ):
            # 已知坏帧不再烧昂贵的视频生成额度：'vlm_qa_failed'（旧逐帧质检门终态，
            # 现已停用，仅为兼容旧 manifest 保留）/'sequence_review_flagged'（整套序列
            # 一致性审查修复轮次耗尽仍有问题）都是已知有问题、需要人工介入的终态。
            _bad_gate = slot_to_quality.get(slot)
            _bad = slot if _bad_gate in ('vlm_qa_failed', 'sequence_review_flagged') else slot + 1
            _bad_gate = slot_to_quality.get(_bad)
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_bad} 未通过一致性审查（{_bad_gate}），已拦截该段视频生成。"
                f"请重渲该帧（或将质检档位调为 off 放行）后重试。"
            )
        elif stale_slots and gate_level == 'standard' \
                and (slot in stale_slots or (slot + 1) in stale_slots):
            _stale = slot if slot in stale_slots else slot + 1
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_stale} 派生自旧的 i2i 链（上游帧已被单独重渲，"
                f"血统过期），已拦截以防跨链跳变。请顺序重渲 IMAGE {_stale} 及其后续帧，"
                f"或将质检档位调为 lenient 带警告放行。"
            )
        else:
            plan['action'] = 'generate'
            if stale_slots and (slot in stale_slots or (slot + 1) in stale_slots):
                _stale = slot if slot in stale_slots else slot + 1
                plan['warning'] = (
                    f"视频 {slot} 的锚点帧 IMAGE {_stale} 派生自旧 i2i 链（上游帧已被单独重渲），"
                    f"两端帧可能存在跨链色彩/内容漂移。"
                )
        plans.append(plan)
    return plans


def _video_info(plan, video_model, status, error=None):
    """由槽位计划生成 manifest.videos 条目。"""
    if status == 'success':
        rel, url = _rel_url(plan['dest_path'])
    else:
        rel, url = '', ''
    info = {
        'slot': plan['slot'],
        'sequence': plan['seq'],
        'file': rel,
        'url': url,
        'prompt': plan['prompt'],
        'model': video_model,
        'status': status,
    }
    if error:
        info['error'] = error
    return info


class _ManifestWriter:
    """manifest.json 的 videos 段增量写入：同槽位后写覆盖先写，按槽位升序排列。
    每次 record() 立即落盘——浏览器批量任务动辄十几分钟，进度必须实时可恢复。"""

    def __init__(self, manifest_path, manifest_data, all_slots):
        self.path = manifest_path
        self.data = manifest_data
        self.all_slots = sorted(all_slots)
        self.results = []  # 本次运行产生的 video_info（含失败），按发生顺序

    def record(self, video_info):
        self.results.append(video_info)
        self.save()

    def save(self):
        by_slot = {v['slot']: v for v in self.data.get('videos', [])}
        for v in self.results:
            by_slot[v['slot']] = v
        self.data['videos'] = [by_slot[s] for s in self.all_slots if s in by_slot]
        try:
            # 项目级锁 + 原子替换（write_manifest 内部处理 Windows 句柄占用重试）
            write_manifest(os.path.dirname(self.path), self.data)
        except Exception as e:
            print(f"Warning: could not write updated manifest.json ({e})")


class _BatchBridge:
    """把 google_fx_video 批量脚本的回调 (batch_idx, stage, details) 翻译成 SPARK
    进度事件并落盘 manifest。下载落盘后做锚点内容校验：视频首尾帧与该槽位锚点图
    不符（画布串片/错误下载）时删除文件并回传 'rejected'，批量脚本据此把该任务
    标记失败进入重试轮。"""

    def __init__(self, pending, total, video_model, writer, on_progress, strict=False,
                 process_check_fn=None):
        self.pending = pending          # [{'plan':..., 'req':..., 'temp_out_dir':...}]
        self.total = total
        self.video_model = video_model
        self.writer = writer
        self.on_progress = on_progress
        self.strict = strict            # strictGates：锚点校验环境异常按失败处理
        # 段内过程门（空心视频拦截）：plan -> ('accept'|'warn'|'reject', reason)。
        # None = 不检（旧调用方/测试兼容）；生产路径由 generate_video_sequence 注入
        # check_video_process 的闭包。
        self.process_check_fn = process_check_fn

    def _emit(self, stage, payload):
        if self.on_progress:
            return self.on_progress(stage, payload)
        return None

    def _fail(self, plan, message):
        self.writer.record(_video_info(plan, self.video_model, status='failed', error=message))
        self._emit('video_error', {
            'index': plan['slot'], 'current': plan['seq'],
            'total': self.total, 'message': message,
        })

    def __call__(self, batch_idx, stage, details):
        plan = self.pending[batch_idx]['plan']
        if stage == 'video_start':
            self._emit('video_start', {
                'index': plan['slot'], 'current': plan['seq'], 'total': self.total,
            })
        elif stage == 'video_done':
            generated_path = (details or {}).get('video_url')
            if not (generated_path and os.path.exists(generated_path)):
                self._fail(plan, '生成的视频文件不存在')
                return None
            shutil.move(generated_path, plan['dest_path'])
            ok, reason = verify_video_anchors(plan['dest_path'], plan['start_frame'], plan['end_frame'], strict=self.strict)
            if not ok:
                try:
                    os.remove(plan['dest_path'])
                except Exception:
                    pass
                self._fail(plan, (
                    f"下载的视频内容与槽位 {plan['slot']} 的首尾锚点帧不符 (MAD {reason})，"
                    f"疑似画布串片/错误下载，已拦截并删除。请重试该片段。"
                ))
                return 'rejected'  # 通知批量脚本该片段实际失败，可参与失败重试
            process_reason = None
            if self.process_check_fn is not None:
                action, process_reason = self.process_check_fn(plan)
                if action == 'reject':
                    try:
                        os.remove(plan['dest_path'])
                    except Exception:
                        pass
                    self._fail(plan, (
                        f"槽位 {plan['slot']} 的视频段内过程检测未通过（{process_reason}），"
                        f"疑似空心片段/无关内容，已拦截并删除。请重试该片段。"
                    ))
                    return 'rejected'  # 与锚点拒收同路径：批量脚本据此进入失败重试轮
            info = _video_info(plan, self.video_model, status='success')
            # 校验结果留痕：skipped:* 表示该片段其实没经过锚点核验（环境异常被放行）
            info['anchor_check'] = reason
            if process_reason is not None:
                # 段内过程检测留痕：PASS / WARN:... / Skipped(...) / skipped:...
                info['process_check'] = process_reason
            self.writer.record(info)
            if self.process_check_fn is not None and process_reason and action == 'warn':
                self._emit('video_warning', {
                    'index': plan['slot'], 'current': plan['seq'], 'total': self.total,
                    'message': f"VID {plan['slot']:03d} 段内过程检测告警（宽松档放行）：{process_reason}",
                })
            self._emit('video_done', {
                'index': plan['slot'], 'current': plan['seq'],
                'total': self.total, 'video': info,
            })
        elif stage == 'video_error':
            # 单段失败隔离：记录失败状态，其余槽位继续
            self._fail(plan, (details or {}).get('message') or '生成失败')
        return None


def generate_video_sequence(config, title, prompt_block, on_progress=None, target_slots=None):
    import builtins
    builtins.google_fx_cancelled = False
    rotate_requests = config.get('googleFxIpRotateRequests')
    if rotate_requests is not None:
        os.environ['MIYA_ROTATE_THRESHOLD'] = str(rotate_requests)
    from prompt_pipeline import _parse_prompt_slots
    images, videos = _parse_prompt_slots(prompt_block)
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest_data = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
        except Exception as e:
            print(f"Warning: could not read manifest.json ({e})")

    slot_to_path, slot_to_quality = load_slot_frames(manifest_data, frames_dir, len(images))
    if not slot_to_path:
        raise RuntimeError('未找到已生成的帧图像。请先生成帧序列！')

    plans = plan_video_slots(videos, slot_to_path, slot_to_quality, videos_dir, target_slots,
                              strict=strict_gates_enabled(config),
                              gate_level=qa_gate_level(config),
                              stale_slots=load_stale_slots(manifest_data))

    if on_progress:
        on_progress('start', {
            'total': len(plans),
            'slots': [p['slot'] for p in plans],
        })

    google_fx_video, models = _get_google_fx_video_service()
    video_model = config.get('videoModel') or 'Veo 3.1 - Lite [Lower Priority]'
    writer = _ManifestWriter(manifest_path, manifest_data, videos.keys())

    # 按计划分流：复用/拦截立即出结果，待生成的装配为批量请求
    pending_items = []
    for plan in plans:
        if plan['action'] == 'skip_cut':
            # 声明式硬切：记入 manifest（status='skipped_cut'）供合成门禁识别为预期缺失，
            # 不提交生成、不算失败
            writer.record(_video_info(plan, video_model, status='skipped_cut'))
            if on_progress:
                on_progress('video_skipped', {
                    'index': plan['slot'], 'current': plan['seq'],
                    'total': len(plans), 'message': plan['reason'],
                })
            continue
        if plan['action'] == 'reuse':
            info = _video_info(plan, video_model, status='success')
            writer.record(info)
            if on_progress:
                on_progress('video_done', {
                    'index': plan['slot'], 'current': plan['seq'],
                    'total': len(plans), 'video': info,
                })
            continue
        if plan['action'] == 'blocked':
            writer.record(_video_info(plan, video_model, status='failed', error=plan['reason']))
            if on_progress:
                on_progress('video_error', {
                    'index': plan['slot'], 'current': plan['seq'],
                    'total': len(plans), 'message': plan['reason'],
                })
            continue
        # action == 'generate'
        if plan.get('warning'):
            print(f"[VIDEO GATE][WARN] {plan['warning']}")
            if on_progress:
                on_progress('video_warning', {
                    'index': plan['slot'], 'current': plan['seq'],
                    'total': len(plans), 'message': plan['warning'],
                })
        if plan['delete_existing']:
            try:
                os.remove(plan['dest_path'])
            except Exception as e:
                print(f"Warning: could not remove old video file {plan['dest_path']}: {e}")
        temp_out_dir = tempfile.mkdtemp()
        req = models.VideoRequest(
            prompt=plan['prompt'],
            image=plan['start_frame'],
            end_image=plan['end_frame'],
            model=video_model,
            ratio=config.get('imageAspectRatio') or '9:16',
            output_path=temp_out_dir
        )
        pending_items.append({'plan': plan, 'req': req, 'temp_out_dir': temp_out_dir})

    if pending_items:
        def _process_gate(plan):
            return check_video_process(config, plan['dest_path'], plan['start_frame'],
                                       plan['end_frame'], plan['prompt'])

        bridge = _BatchBridge(pending_items, len(plans), video_model, writer, on_progress,
                              strict=strict_gates_enabled(config),
                              process_check_fn=_process_gate)

        def cancel_check_cb():
            if on_progress:
                try:
                    # 空探测调用：连接已死/用户已取消时返回 True
                    return on_progress('cancel_check', None)
                except Exception:
                    return True
            return False

        try:
            google_fx_video.generate_videos_batch_google_fx(
                [it['req'] for it in pending_items],
                on_progress=bridge,
                cancel_check=cancel_check_cb
            )
        finally:
            for it in pending_items:
                shutil.rmtree(it['temp_out_dir'], ignore_errors=True)

    writer.save()
    manifest_data['manifest'] = '/' + os.path.relpath(manifest_path, _BASE_DIR).replace('\\', '/')
    manifest_data['project_dir'] = os.path.abspath(project_dir)
    return manifest_data


class PartialMergeBlocked(RuntimeError):
    """合成门禁拦截：项目存在缺失/失败片段或串片片段。携带槽位清单供前端决策。"""
    def __init__(self, missing, mismatched, message=None):
        self.missing = list(missing or [])
        self.mismatched = list(mismatched or [])
        super().__init__(message or self._default_message())

    def _default_message(self):
        parts = []
        if self.missing:
            parts.append(f"缺失/失败片段（槽位 {', '.join(map(str, self.missing))}）")
        if self.mismatched:
            parts.append(f"内容与锚点不符、疑似串片（槽位 {', '.join(map(str, self.mismatched))}）")
        return "存在" + "；".join(parts) + "，已拒绝合并。请重试这些片段，或选择「强制合并（占位填充）」。"


def _ffprobe_video_params(path):
    """探测视频的 width/height/fps/duration；失败返回 None。"""
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate:format=duration",
               "-of", "json", path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace', check=True)
        info = json.loads(res.stdout)
        st = (info.get('streams') or [{}])[0]
        rate = st.get('r_frame_rate') or '24/1'
        num, _, den = rate.partition('/')
        den = den or '1'
        fps = (float(num) / float(den)) if float(den) else 24.0
        return {
            'width': int(st.get('width') or 0),
            'height': int(st.get('height') or 0),
            'fps': round(fps, 3),
            'duration': float((info.get('format') or {}).get('duration') or 0) or 0.0,
        }
    except Exception as e:
        print(f"[WARN] _ffprobe_video_params failed for {path}: {e}")
        return None


def _find_label_font():
    """定位一个可用于 drawtext 的字体（尽量支持中文），找不到返回 None。"""
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            continue
    return None


def _project_display_name(title):
    """项目中文名：优先 library.json 主题中的中文，其次标题中文，最后安全化项目名。"""
    chinese_name = ""
    library_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'library.json')
    if os.path.exists(library_path):
        try:
            with open(library_path, 'r', encoding='utf-8') as lf:
                lib_data = json.load(lf)
            if isinstance(lib_data, list):
                for item in lib_data:
                    if item.get('title') == title:
                        theme_chinese = "".join(re.findall(r'[一-龥]+', item.get('theme', '')))
                        if theme_chinese:
                            chinese_name = theme_chinese
                            break
        except Exception as le:
            print(f"Warning: could not read library.json for theme lookup ({le})")
    if not chinese_name and title:
        title_chinese = "".join(re.findall(r'[一-龥]+', title))
        if title_chinese:
            chinese_name = title_chinese
    if not chinese_name:
        chinese_name = _safe_project_name(title)
    return chinese_name


def merge_project_videos(project_dir, allow_partial=False):
    """合并项目内全部视频片段。

    2026-07-04 复盘：之前失败/缺失的槽位会被静默跳过（loft 任务缺 6、7 两段仍合出了
    成片，观感为画面硬跳/回到初始状态），且串片的片段会原样混入。现在默认执行两道门禁：
    1) 槽位完整性 —— 依据 manifest.frames 推出应有片段数，缺失/失败即拒绝合并；
    2) 锚点一致性 —— 每段首尾帧须与对应锚点图匹配，不匹配即拒绝合并。
    allow_partial=True 时跳过两道门禁（用户显式确认后强制合并）。
    """
    manifest_path = os.path.join(project_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest_data = json.load(f)

    videos = manifest_data.get('videos', [])
    frames = manifest_data.get('frames', [])

    def _resolve_abs(rel):
        abs_path = os.path.abspath(rel.lstrip('/'))
        if not os.path.exists(abs_path):
            abs_path = os.path.abspath(os.path.join(project_dir, 'videos', os.path.basename(rel)))
        return abs_path

    # ── 槽位分类：good（成功且锚点一致）/ missing（缺失/失败）/ mismatched（串片） ──
    base_dir = os.path.dirname(os.path.abspath(__file__))

    def _resolve_frame(entry):
        if not entry or not entry.get('file'):
            return None
        p = os.path.join(base_dir, entry['file'].lstrip('/'))
        if not os.path.exists(p):
            p = os.path.join(project_dir, 'frames', os.path.basename(entry['file']))
        return p if os.path.exists(p) else None

    by_slot = {v.get('slot'): v for v in videos}
    frame_by_slot = {f.get('slot'): f for f in frames}
    good = {}          # slot -> 绝对路径（可用片段）
    missing = []       # 缺失/失败/文件不存在
    mismatched = []    # 首尾帧与锚点不符（疑似串片）
    expected_slots = []

    if frames:
        # 声明式硬切槽位（status='skipped_cut'）是"预期缺失"：不算缺口、不占位填充，
        # 合成时相邻两段直接相接=成片在此处硬切（TBCP v2 hard_cut 变体的既定语义）。
        skipped_cut = {v.get('slot') for v in videos
                       if isinstance(v, dict) and v.get('status') == 'skipped_cut'}
        expected_slots = [s for s in range(1, len(frames)) if s not in skipped_cut]
        for slot in expected_slots:
            v = by_slot.get(slot)
            if not v or v.get('status') != 'success' or not v.get('file'):
                missing.append(slot)
                continue
            abs_path = _resolve_abs(v['file'])
            if not os.path.exists(abs_path):
                missing.append(slot)
                continue
            start_p = _resolve_frame(frame_by_slot.get(slot))
            end_p = _resolve_frame(frame_by_slot.get(slot + 1))
            # 合并门禁没有请求级 config，strict 开关直接取服务端配置
            ok, reason = verify_video_anchors(abs_path, start_p, end_p, strict=strict_gates_enabled())
            if not ok:
                mismatched.append(slot)
                continue
            good[slot] = abs_path
    else:
        # 无 frames（历史/兜底）：不做完整性/锚点门禁，直接取所有成功片段
        for v in sorted(videos, key=lambda x: x.get('slot', 0)):
            if v.get('status') == 'success' and v.get('file'):
                abs_path = _resolve_abs(v['file'])
                if os.path.exists(abs_path):
                    good[v.get('slot')] = abs_path
        expected_slots = sorted(good)

    # 默认（allow_partial=False）：有缺口/串片一律拒绝，携带槽位清单抛出供前端决策
    if not allow_partial and (missing or mismatched):
        raise PartialMergeBlocked(missing, mismatched)

    # 强制合并且确有缺口：用起始锚点帧定格 + 缺失标注填充，保持时间轴与顺序（绝不静默丢弃）
    if allow_partial and (missing or mismatched):
        return _merge_with_placeholders(
            project_dir, manifest_data, expected_slots, good,
            missing, mismatched, frame_by_slot,
        )

    # 无缺口/无串片：走原有干净合并路径
    video_files = [good[s] for s in sorted(good)]
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
            res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding='utf-8', errors='replace', check=True)
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
    # encoding must be explicit: ffmpeg emits UTF-8, but Windows text-mode default is GBK,
    # which crashes the subprocess stderr reader thread mid-merge
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='replace')

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
            dur_res = subprocess.run(dur_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding='utf-8', errors='replace', check=True)
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


def _merge_with_placeholders(project_dir, manifest_data, expected_slots, good,
                             missing, mismatched, frame_by_slot):
    """强制合并（allow_partial）：缺失/串片槽位用起始锚点帧定格 + 「缺失」标注填充，
    保持时间轴对齐与顺序，输出带 _partial 后缀的预览成片（视频轨、2x 加速、无音轨）。
    绝不静默丢弃缺口——这正是当初加合成门禁的原因。"""
    bad = set(missing) | set(mismatched)
    base_dir = os.path.dirname(os.path.abspath(__file__))

    def _resolve_frame(entry):
        if not entry or not entry.get('file'):
            return None
        p = os.path.join(base_dir, entry['file'].lstrip('/'))
        if not os.path.exists(p):
            p = os.path.join(project_dir, 'frames', os.path.basename(entry['file']))
        return p if os.path.exists(p) else None

    # 目标画面参数：优先探测一段真实视频；无则给竖屏默认值
    params = None
    for s in sorted(good):
        params = _ffprobe_video_params(good[s])
        if params and params.get('width') and params.get('height'):
            break
    width = (params or {}).get('width') or 1080
    height = (params or {}).get('height') or 1920
    fps = (params or {}).get('fps') or 24.0
    seg_dur = (params or {}).get('duration') or 5.0
    if seg_dur <= 0:
        seg_dur = 5.0

    norm = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,format=yuv420p")

    # 把字体拷进项目目录并用相对名引用，规避 filtergraph 里 Windows 盘符冒号(C:)的转义问题
    font = _find_label_font()
    font_rel = None
    if font:
        try:
            ext = os.path.splitext(font)[1] or '.ttf'
            shutil.copyfile(font, os.path.join(project_dir, f"_ph_font{ext}"))
            font_rel = f"_ph_font{ext}"
        except Exception as _fe:
            print(f"[WARN] copy label font failed: {_fe}")
            font_rel = None

    inputs = []
    filter_parts = []
    concat_labels = []
    placeholder_slots = []
    label_files = []
    idx = 0
    for slot in expected_slots:
        if slot in good:
            inputs += ["-i", good[slot]]
            filter_parts.append(f"[{idx}:v]{norm}[v{idx}]")
        else:
            placeholder_slots.append(slot)
            frame_path = _resolve_frame(frame_by_slot.get(slot))
            if frame_path:
                inputs += ["-loop", "1", "-t", f"{seg_dur:.3f}", "-i", frame_path]
                chain = (f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                         f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                         f"eq=brightness=-0.30:saturation=0.55,fps={fps},setsar=1,format=yuv420p")
            else:
                inputs += ["-f", "lavfi", "-t", f"{seg_dur:.3f}",
                           "-i", f"color=c=0x1a0f0f:s={width}x{height}:r={fps}"]
                chain = f"[{idx}:v]setsar=1,format=yuv420p"
            if font_rel:
                reason = '串片' if slot in set(mismatched) else '缺失'
                label_rel = f"_ph_label_{slot}.txt"
                label_file = os.path.join(project_dir, label_rel)
                try:
                    with open(label_file, 'w', encoding='utf-8') as lf:
                        lf.write(f"片段 {slot} {reason} · SEGMENT {slot} MISSING")
                    label_files.append(label_file)
                    # 相对名（无冒号），配合 ffmpeg cwd=project_dir 使用
                    chain += (f",drawbox=x=0:y=(ih-160)/2:w=iw:h=160:color=black@0.55:t=fill,"
                              f"drawtext=fontfile={font_rel}:textfile={label_rel}:reload=0:"
                              f"fontcolor=white:fontsize={max(28, width // 22)}:"
                              f"x=(w-text_w)/2:y=(h-text_h)/2")
                except Exception as _le:
                    print(f"[WARN] placeholder label write failed slot {slot}: {_le}")
            filter_parts.append(chain + f"[v{idx}]")
        concat_labels.append(f"[v{idx}]")
        idx += 1

    if idx == 0:
        return None

    filtergraph = ";".join(filter_parts) + ";" + "".join(concat_labels)
    filtergraph += f"concat=n={idx}:v=1:a=0[vc];[vc]setpts=0.5*PTS[vout]"

    title = manifest_data.get('title', '')
    chinese_name = _project_display_name(title)
    output_path = os.path.join(project_dir, f"{chinese_name}_partial_2x.mp4")

    # 清理项目根下旧 mp4（保持单一成片）
    for fname in os.listdir(project_dir):
        if fname.lower().endswith('.mp4') and os.path.isfile(os.path.join(project_dir, fname)):
            try:
                os.remove(os.path.join(project_dir, fname))
            except Exception as e:
                print(f"Warning: could not remove old merged file {fname} ({e})")

    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", filtergraph,
           "-map", "[vout]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]
    print(f"[INFO] Partial merge: {idx} segments, {len(placeholder_slots)} placeholder(s) "
          f"{placeholder_slots} -> {output_path}")
    res = subprocess.run(cmd, cwd=project_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding='utf-8', errors='replace')

    for lf in label_files:
        try:
            os.remove(lf)
        except Exception:
            pass
    if font_rel:
        try:
            os.remove(os.path.join(project_dir, font_rel))
        except Exception:
            pass

    if res.returncode != 0:
        print(f"[ERROR] partial ffmpeg failed: {res.stderr}")
        raise RuntimeError(f"占位合并失败（FFmpeg）：{res.stderr[-400:]}")

    rel_path = os.path.relpath(output_path, base_dir).replace('\\', '/')
    duration = 0.0
    try:
        dur_res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', check=True)
        duration = float(dur_res.stdout.strip())
    except Exception:
        pass

    return {
        'file': rel_path,
        'url': '/' + rel_path,
        'size_bytes': os.path.getsize(output_path),
        'duration_seconds': round(duration, 2),
        'status': 'success',
        'partial': True,
        'placeholder_slots': placeholder_slots,
    }
