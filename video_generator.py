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
    notify_listeners, save_tasks_to_disk
)
from manifest_store import QualityGate


def _get_google_fx_video_service():
    import sys
    adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
    if adspower_path not in sys.path:
        sys.path.append(adspower_path)
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


def verify_video_anchors(video_path, start_frame_path, end_frame_path):
    """校验视频首帧/尾帧是否与锚点图一致。

    返回 (ok: bool, reason: str)。校验环境异常（ffmpeg/PIL 不可用等）时返回
    (True, 'skipped:...')，不拦截正常流程——该校验只用来挡住明确的串片。
    """
    import tempfile
    import numpy as np
    try:
        with tempfile.TemporaryDirectory() as td:
            checks = []
            for pos, anchor in (('first', start_frame_path), ('last', end_frame_path)):
                if not anchor or not os.path.exists(anchor):
                    continue
                png = os.path.join(td, f'{pos}.png')
                if not _extract_video_frame(video_path, png, pos):
                    return True, f'skipped:extract_{pos}_failed'
                mad = float(np.abs(_load_gray_thumb(png) - _load_gray_thumb(anchor)).mean())
                checks.append((pos, mad))
            if not checks:
                return True, 'skipped:no_anchor'
            bad = [(pos, mad) for pos, mad in checks if mad > _ANCHOR_MAD_THRESHOLD]
            detail = ", ".join(f"{pos}={mad:.1f}" for pos, mad in checks)
            if bad:
                return False, detail
            return True, detail
    except Exception as e:
        return True, f'skipped:{type(e).__name__}'


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


def plan_video_slots(video_slots, slot_to_path, slot_to_quality, videos_dir, target_slots=None):
    """为每个视频槽位做生成决策。只读文件系统，不做任何写操作，可单测。

    video_slots: _parse_prompt_slots 的 videos 输出（{slot: str 或 {'body':...}}）
    target_slots: None=整单生成；列表=只处理这些槽位（显式重试）

    返回按槽位升序的计划列表，每项：
      slot / seq / prompt（已改写 IMAGE 1/2）/ dest_path
      action: 'reuse'    —— 断点续传，直接复用已存在的 mp4
              'generate' —— 需要提交生成
              'blocked'  —— 前置条件不满足（缺帧/降级帧），reason 说明原因
      start_frame / end_frame: 锚点帧绝对路径
      delete_existing: 显式重试且旧文件存在，调用方需先删除
    """
    slots = sorted(video_slots.keys())
    if target_slots is not None:
        wanted = {int(x) for x in target_slots}
        slots = [s for s in slots if s in wanted]
    is_explicit_retry = target_slots is not None

    plans = []
    for seq, slot in enumerate(slots, start=1):
        item = video_slots[slot]
        prompt = item['body'] if isinstance(item, dict) else item
        prompt = rewrite_prompt_for_two_card_ui(prompt, slot)
        dest_path = os.path.join(videos_dir, f'vid_{slot:03d}.mp4')
        plan = {
            'slot': slot,
            'seq': seq,
            'prompt': prompt,
            'dest_path': dest_path,
            'start_frame': slot_to_path.get(slot),
            'end_frame': slot_to_path.get(slot + 1),
            'delete_existing': False,
            'reason': '',
        }

        # 断点续传：非显式重试时，已存在的有效视频直接复用
        if not is_explicit_retry and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            plan['action'] = 'reuse'
            plans.append(plan)
            continue
        if is_explicit_retry and os.path.exists(dest_path):
            plan['delete_existing'] = True

        start_p, end_p = plan['start_frame'], plan['end_frame']
        if not start_p or not os.path.exists(start_p):
            plan['action'] = 'blocked'
            plan['reason'] = f"视频 {slot} 所需的起始帧 IMAGE {slot} 不存在。请重新生成该帧！"
        elif not end_p or not os.path.exists(end_p):
            plan['action'] = 'blocked'
            plan['reason'] = f"视频 {slot} 所需的结束帧 IMAGE {slot + 1} 不存在。请重新生成该帧！"
        elif slot_to_quality.get(slot) == QualityGate.I2I_FALLBACK_DEGRADED \
                or slot_to_quality.get(slot + 1) == QualityGate.I2I_FALLBACK_DEGRADED:
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的起始帧 IMAGE {slot} 或结束帧 IMAGE {slot + 1} 属于降级帧（i2i fallback degraded），"
                f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
            )
        else:
            plan['action'] = 'generate'
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
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Warning: could not write updated manifest.json ({e})")


class _BatchBridge:
    """把 google_fx_video 批量脚本的回调 (batch_idx, stage, details) 翻译成 SPARK
    进度事件并落盘 manifest。下载落盘后做锚点内容校验：视频首尾帧与该槽位锚点图
    不符（画布串片/错误下载）时删除文件并回传 'rejected'，批量脚本据此把该任务
    标记失败进入重试轮。"""

    def __init__(self, pending, total, video_model, writer, on_progress):
        self.pending = pending          # [{'plan':..., 'req':..., 'temp_out_dir':...}]
        self.total = total
        self.video_model = video_model
        self.writer = writer
        self.on_progress = on_progress

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
            ok, reason = verify_video_anchors(plan['dest_path'], plan['start_frame'], plan['end_frame'])
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
            info = _video_info(plan, self.video_model, status='success')
            self.writer.record(info)
            self._emit('video_done', {
                'index': plan['slot'], 'current': plan['seq'],
                'total': self.total, 'video': info,
            })
        elif stage == 'video_error':
            # 单段失败隔离：记录失败状态，其余槽位继续
            self._fail(plan, (details or {}).get('message') or '生成失败')
        return None


def _clear_previous_outputs(videos_dir, manifest_data):
    """整单重跑（非重试）时清空旧视频文件与 manifest.videos，
    防止断点续传把上一轮的旧视频当成本轮结果。"""
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
    if 'videos' in manifest_data:
        manifest_data['videos'] = []


# Serializes access to the single shared AdsPower browser session used by
# generate_videos_batch_google_fx. Enforced here at the function boundary (rather than as a
# call-site convention) so every caller — the manual /api/generate_videos path, and the
# autonomous auto_run/render_staged paths that reach this via
# pipeline_orchestrator._render_videos_with_recovery — is serialized the same way. Before this,
# only the manual path wrapped its call in a lock, leaving auto_run/render_staged free to run
# concurrently against the same browser session (or against a manual request).
_VIDEO_GEN_SERIAL_LOCK = threading.Lock()


def generate_video_sequence(config, title, prompt_block, on_progress=None, target_slots=None):
    with _VIDEO_GEN_SERIAL_LOCK:
        return _generate_video_sequence_locked(config, title, prompt_block, on_progress=on_progress, target_slots=target_slots)


def _generate_video_sequence_locked(config, title, prompt_block, on_progress=None, target_slots=None):
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

    if target_slots is None:
        _clear_previous_outputs(videos_dir, manifest_data)

    plans = plan_video_slots(videos, slot_to_path, slot_to_quality, videos_dir, target_slots)

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
        bridge = _BatchBridge(pending_items, len(plans), video_model, writer, on_progress)

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


class VideoMergeBlocked(RuntimeError):
    """merge_project_videos deliberately refused to merge (slot-completeness or
    anchor-consistency gate) — distinct from an unexpected failure (e.g. ffmpeg crashing),
    so callers can treat the two differently (e.g. a soft 'skipped, try again later' status
    vs a hard 'something went wrong' error)."""


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

    # ── 门禁 1: 槽位完整性 ──
    if frames and not allow_partial:
        expected_slots = list(range(1, len(frames)))
        by_slot = {v.get('slot'): v for v in videos}
        missing = []
        for slot in expected_slots:
            v = by_slot.get(slot)
            if not v or v.get('status') != 'success' or not v.get('file') \
                    or not os.path.exists(_resolve_abs(v['file'])):
                missing.append(slot)
        if missing:
            raise VideoMergeBlocked(
                f"存在失败或缺失的视频片段（槽位 {', '.join(map(str, missing))}），已拒绝合并。"
                f"请先重试这些片段，或确认后强制合并。"
            )

    # Filter and sort by slot index
    video_files = []
    slot_of_file = {}
    # Make sure we only check files that exist
    for v in sorted(videos, key=lambda x: x.get('slot', 0)):
        if v.get('status') == 'success' and v.get('file'):
            abs_path = _resolve_abs(v['file'])
            if os.path.exists(abs_path):
                video_files.append(abs_path)
                slot_of_file[abs_path] = v.get('slot')

    if not video_files:
        return None

    # ── 门禁 2: 每段首尾帧与锚点图一致（拦截串片/文生视频混入） ──
    if frames and not allow_partial:
        base_dir = os.path.dirname(os.path.abspath(__file__))

        def _resolve_frame(entry):
            if not entry or not entry.get('file'):
                return None
            p = os.path.join(base_dir, entry['file'].lstrip('/'))
            if not os.path.exists(p):
                p = os.path.join(project_dir, 'frames', os.path.basename(entry['file']))
            return p if os.path.exists(p) else None

        frame_by_slot = {f.get('slot'): f for f in frames}
        mismatched = []
        for vf in video_files:
            slot = slot_of_file.get(vf)
            start_p = _resolve_frame(frame_by_slot.get(slot))
            end_p = _resolve_frame(frame_by_slot.get((slot or 0) + 1))
            ok, reason = verify_video_anchors(vf, start_p, end_p)
            if not ok:
                mismatched.append(f"{slot} (MAD {reason})")
        if mismatched:
            raise VideoMergeBlocked(
                f"以下片段内容与锚点帧不符，疑似串片/错误下载：槽位 {'; '.join(mismatched)}。"
                f"已拒绝合并，请重新生成这些片段。"
            )
        
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


