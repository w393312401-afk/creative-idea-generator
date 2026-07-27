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
    notify_listeners, save_tasks_to_disk,
    apply_google_fx_runtime_overrides,
    read_manifest, write_manifest, strict_gates_enabled, qa_gate_level,
    # 号池轮转口径（帧序列与视频序列共用，见 server_common 的「换 IP 已全局关停」注释）
    _get_account_pool_service, _select_pool_account,
    _account_switch_interval, _account_in_cooldown,
    _account_has_credit, _account_rotation_ring, _next_unused_account,
)


def _get_google_fx_video_service():
    from integrations.google_fx.services import google_fx, google_fx_video
    from integrations.google_fx import models
    return google_fx_video, models


def _get_credit_helpers():
    from integrations.google_fx.services import google_fx_credit
    return google_fx_credit


def plan_generation_legs(pending_items, ring, switch_interval):
    """把待生成请求切成「腿」：每腿 switch_interval 个请求、绑定环里的下一个账号。

    所有腿都跑在同一个 IP 上（换 IP 已全局关停，见 server_common 的「换 IP 已全局关停」
    注释）。可换的账号不足 2 个时退化成单腿（user_id=None 表示沿用调用方已经设好的
    账号）。"""
    items = list(pending_items)
    if len(ring) <= 1:
        return [{'items': items, 'user_id': None}]
    legs = []
    for i in range(0, len(items), max(1, switch_interval)):
        idx = len(legs)
        legs.append({
            'items': items[i:i + max(1, switch_interval)],
            'user_id': ring[idx % len(ring)],
        })
    return legs


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


# ── i2v 提交前的帧对契约（2026-07-15 盐湖贝壳单复盘）──
# 两端锚点帧本身就注定了片段质量：近似复制对（MAD≤2.2）产出静止片段，空间断裂对
# （6→7 实测 47.2）产出冻结闪切/自由变形；正常施工推进对落在 4.8~17.3。
# 阈值留了余量：<3 判"将无变化"、>35 判"疑似空间断裂"。纯本地计算，零 LLM 成本。
_PAIR_TOO_SIMILAR_MAD = 3.0
_PAIR_TOO_DIFFERENT_MAD = 35.0


def frame_pair_contract(start_frame_path, end_frame_path):
    """i2v 提交前对首尾锚点帧做本地相似度双向检查。

    返回 (verdict, mad)，verdict ∈ 'ok' | 'too_similar' | 'too_different' | 'skipped'
    （环境异常/文件缺失时 'skipped'，不拦截——这只是提示性契约，硬拦截由质量门负责）。"""
    try:
        if not (start_frame_path and end_frame_path
                and os.path.exists(start_frame_path) and os.path.exists(end_frame_path)):
            return 'skipped', None
        import numpy as np
        mad = float(np.abs(_load_gray_thumb(start_frame_path) - _load_gray_thumb(end_frame_path)).mean())
        if mad < _PAIR_TOO_SIMILAR_MAD:
            return 'too_similar', mad
        if mad > _PAIR_TOO_DIFFERENT_MAD:
            return 'too_different', mad
        return 'ok', mad
    except Exception:
        return 'skipped', None


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


# 冻结片段本地判据（2026-07-15 盐湖贝壳单标定）：抽样帧（first/25%/50%/75%）两两
# 相邻 MAD——全程冻结的 vid_012 为 1.74/0.62/1.77，低运动但确有动静的 vid_002 为
# 7.35/5.15/5.67，正常片段 8~14。阈值取 3.0。
_FROZEN_CLIP_MAD = 3.0


def detect_frozen_clip(video_path, mid_frame_paths, tmp_dir):
    """本地冻结检测（零 LLM 成本）：视频前 3/4 程的抽样帧几乎无变化 = 冻结片段
    （含"冻结到最后一秒才闪切到尾帧"的 freeze-snap 变体，尾帧不参与冻结判定、
    只用来区分两种坏法）。返回 (frozen: bool, reason: str)；抽帧/环境异常返回
    (False, 'skipped:...')，交给后面的 VLM 过程门兜底。"""
    try:
        import numpy as np
        first_png = os.path.join(tmp_dir, 'freeze_first.png')
        if not _extract_video_frame(video_path, first_png, 'first'):
            return False, 'skipped:first_extract_failed'
        seq = [first_png] + list(mid_frame_paths)
        if len(seq) < 3:
            return False, 'skipped:not_enough_samples'
        grays = [_load_gray_thumb(p) for p in seq]
        mads = [float(np.abs(grays[i] - grays[i + 1]).mean()) for i in range(len(grays) - 1)]
        detail = '/'.join(f'{m:.2f}' for m in mads)
        if max(mads) >= _FROZEN_CLIP_MAD:
            return False, f'motion_ok:{detail}'
        last_png = os.path.join(tmp_dir, 'freeze_last.png')
        snap = ''
        if _extract_video_frame(video_path, last_png, 'last'):
            last_mad = float(np.abs(grays[-1] - _load_gray_thumb(last_png)).mean())
            if last_mad >= _FROZEN_CLIP_MAD:
                snap = f'，且在结尾闪切到尾帧（末段 MAD={last_mad:.1f}）'
        return True, (f'本地冻结检测：片段前 3/4 程抽样帧几乎无变化（相邻 MAD={detail}，'
                      f'阈值 {_FROZEN_CLIP_MAD}）{snap}——i2v 没有可执行的动作，产出了静止片段')
    except Exception as e:
        return False, f'skipped:{type(e).__name__}'


# 2026-07-22：用户明确要求视频阶段暂停这道内容复审——画面走向在帧序列阶段已经定稿，
# 视频只管生成，不应该因为这道门的误判（如第9槽把地毯消失误判为"空心片段"）被删片
# 重来。改回 False 即可恢复 qa_gate_level 档位映射（standard 拒收/lenient 警告/off 跳过）。
_VIDEO_PROCESS_GATE_DISABLED = True


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
    if _VIDEO_PROCESS_GATE_DISABLED:
        return 'accept', 'Skipped (视频段内过程复审已按用户要求暂停 2026-07-22)'
    level = qa_gate_level(config)
    if level == 'off':
        return 'accept', 'Skipped (qaGateLevel=off: 质检门已关闭)'
    with tempfile.TemporaryDirectory() as td:
        mids = _extract_video_mid_frames(video_path, td)
        if not mids:
            if strict_gates_enabled(config):
                return 'reject', 'strict:mid_extract_failed（严格模式下环境异常按失败处理）'
            return 'accept', 'skipped:mid_extract_failed'
        # 本地冻结预筛（零 LLM 成本、确定性）：冻结/冻结闪切是明确硬伤，直接定档，
        # 省掉一次多模态判定调用；有动静的片段才交给 VLM 判过程内容。
        frozen, freeze_reason = detect_frozen_clip(video_path, mids, td)
        if frozen:
            if level == 'lenient':
                return 'warn', freeze_reason
            return 'reject', freeze_reason
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


def rewrite_prompt_for_two_card_ui(prompt, slot, start_slot=None):
    """把提示词里的 IMAGE start_slot / IMAGE slot+1（含中文「图片 N」）改写为
    IMAGE 1 / IMAGE 2，对应 Google Labs Flow 两卡位（首帧/尾帧）UI。start_slot 默认
    等于 slot（含单一过门拍——起止帧绑定和普通拍完全一样，无需重定向）；参数保留
    作为通用覆盖钩子。"""
    start_slot = slot if start_slot is None else start_slot
    prompt = re.sub(rf'\bimage\s+{start_slot}\b', 'IMAGE 1', prompt, flags=re.IGNORECASE)
    prompt = re.sub(rf'\bimage\s+{slot + 1}\b', 'IMAGE 2', prompt, flags=re.IGNORECASE)
    prompt = re.sub(rf'图片\s*{start_slot}\b', 'IMAGE 1', prompt)
    prompt = re.sub(rf'图片\s*{slot + 1}\b', 'IMAGE 2', prompt)
    return prompt


# 已知有问题、需要人工介入的帧终态：'vlm_qa_failed'（旧逐帧质检门终态，现已停用，
# 仅为兼容旧 manifest 保留）/'sequence_review_flagged'（整套序列一致性审查检出问题）
# /'manual_flagged'（人工主动描述了这一帧的问题，见
# pipeline_orchestrator.set_manual_frame_issue）。三者一律不再烧昂贵的视频生成额度，
# 除非用户显式 override_flagged 确认风险。
_FLAGGED_QUALITY_GATES = ('vlm_qa_failed', 'sequence_review_flagged', 'manual_flagged')

_FLAGGED_GATE_LABELS = {
    'vlm_qa_failed': '未通过质检',
    'sequence_review_flagged': '未通过一致性审查',
    'manual_flagged': '被人工标记存在问题',
}


def load_slot_frames(manifest_data, frames_dir, image_count):
    """从 manifest.frames 建立 槽位→帧绝对路径 与 槽位→质量门标记 的映射；
    manifest 缺失/为空时按 frames/img_NNN.webp 命名约定兜底。

    manifest.frames[].file 的正常形态是仓库相对路径（"outputs/<项目>/frames/img_001.webp"，
    lstrip('/') 是为了兼容误存成 url 形态的 "/outputs/..."）。但若某条记录存的是绝对路径，
    无条件 join 会拼出 "<仓库>/private/var/..." 这种不存在的路径，而下游（verify_video_anchors
    / plan_video_slots）对"锚点文件不存在"的处理是静默跳过——串片检测会悄无声息地失效而
    日志上看不出异常。因此拼不出实际文件时，回退到把原值当绝对路径用。"""
    slot_to_path = {}
    slot_to_quality = {}
    for frame in (manifest_data or {}).get('frames', []):
        try:
            raw = frame['file']
            resolved = os.path.join(_BASE_DIR, raw.lstrip('/'))
            if not os.path.exists(resolved) and os.path.isabs(raw) and os.path.exists(raw):
                resolved = raw
            slot_to_path[frame['slot']] = resolved
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


def load_drift_break_slots(manifest_data):
    """manifest.chain_drift 里 FAIL 段覆盖的视频槽位集合。

    链回望（pipeline_orchestrator._chain_drift_lookback）对每个镜头族记录
    {family_anchor, mid, tail, passed, reason}；passed=False 表示锚点帧与族尾帧之间
    存在身份/机位断裂——断裂点落在 [anchor, tail] 区间内的某一对帧上，因此该区间内
    所有相邻帧对（视频槽位 anchor..tail-1）都可能横跨断裂。2026-07-15 盐湖贝壳单
    正是 anchor=6/tail=9 段 FAIL 被无视，vid_006 在室外/室内两张无关帧之间自由变形。
    此前链回望是纯检测型（结果只写 manifest），这里把它接进视频配对门禁。"""
    slots = set()
    for entry in (manifest_data or {}).get('chain_drift', []):
        if not isinstance(entry, dict) or entry.get('passed') is not False:
            continue
        try:
            anchor = int(entry['family_anchor'])
            tail = int(entry['tail'])
        except (KeyError, TypeError, ValueError):
            continue
        slots.update(range(anchor, tail))
    return slots


def plan_video_slots(video_slots, slot_to_path, slot_to_quality, videos_dir, target_slots=None,
                      strict=False, verify_fn=None, gate_level='standard', stale_slots=None,
                      drift_slots=None, override_flagged=False):
    """为每个视频槽位做生成决策。只读文件系统，不做任何写操作，可单测。

    video_slots: _parse_prompt_slots 的 videos 输出（{slot: str 或 {'body':...}}）
    target_slots: None=整单生成；列表=只处理这些槽位（显式重试）
    strict/verify_fn: 断点续传复用判定用，见下方注释；verify_fn 默认为
      verify_video_anchors，测试可注入假实现，避免依赖真实 ffmpeg/视频文件。
    override_flagged: 前端"确认风险，强制生成"一次性确认（与全局 qaGateLevel 配置
      无关，只对本次请求生效）。豁免的是一致性审查衍生的三道硬拦——
      'vlm_qa_failed'/'sequence_review_flagged' 质检终态、血统过期（stale_slots）、
      链回望空间断裂（drift_slots）——2026-07-24 修复前只豁免了第一道，用户已在
      前端确认风险，血统过期/链回望断裂两道仍照旧拦截，等于"确认了但没全部生效"，
      已确认风险的帧仍不会被提交生成（不会上传到视频后端）。降级帧
      （i2i_fallback_degraded）是唯一保持不受影响、始终硬拦的独立门禁——降级帧
      本身已是明确的生成失败兜底产物，不属于"一致性审查有疑虑但帧本身完好"的
      范畴，强推只会浪费视频生成额度。True 时改判 'generate' 并落一条 warning
      说明是人工确认风险后强制放行的。

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
        # 英雄展示视频（[HERO]，默认收尾步骤）：唯一来源锚点是帧序列最后一张整体
        # 完工图，没有独立的"下一张"结束帧可绑定——只上传首帧，走单图生视频
        # （见 prompt_pipeline._compose_hero_showcase_video），不设结束锚点。
        is_hero = 'HERO' in meta

        # 单一过门拍（[BRIDGE]/[BRIDGE TURN]）：本拍的 VIDEO 是过门唯一可见片段，
        # 起止帧绑定和普通拍完全一样（IMAGE slot -> IMAGE slot+1），无需重定向。
        start_slot = slot

        prompt = rewrite_prompt_for_two_card_ui(prompt, slot, start_slot=start_slot)
        dest_path = os.path.join(videos_dir, f'vid_{slot:03d}.mp4')
        start_p = slot_to_path.get(start_slot)
        end_p = None if is_hero else slot_to_path.get(slot + 1)
        plan = {
            'slot': slot,
            'seq': seq,
            'prompt': prompt,
            'dest_path': dest_path,
            'start_frame': start_p,
            'end_frame': end_p,
            'start_anchor_slot': start_slot,
            'delete_existing': False,
            'reason': '',
            'meta': meta,
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
            plan['reason'] = f"视频 {slot} 所需的起始帧 IMAGE {start_slot} 不存在。请重新生成该帧！"
        elif not is_hero and (not end_p or not os.path.exists(end_p)):
            plan['action'] = 'blocked'
            plan['reason'] = f"视频 {slot} 所需的结束帧 IMAGE {slot + 1} 不存在。请重新生成该帧！"
        elif slot_to_quality.get(start_slot) == 'i2i_fallback_degraded' \
                or slot_to_quality.get(slot + 1) == 'i2i_fallback_degraded':
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的起始帧 IMAGE {start_slot} 或结束帧 IMAGE {slot + 1} 属于降级帧（i2i fallback degraded），"
                f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
            )
        elif not override_flagged and gate_level != 'off' and (
            slot_to_quality.get(start_slot) in _FLAGGED_QUALITY_GATES
            or slot_to_quality.get(slot + 1) in _FLAGGED_QUALITY_GATES
        ):
            # 已知坏帧不再烧昂贵的视频生成额度，见 _FLAGGED_QUALITY_GATES。
            _bad = start_slot if slot_to_quality.get(start_slot) in _FLAGGED_QUALITY_GATES else slot + 1
            _bad_gate = slot_to_quality.get(_bad)
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_bad} {_FLAGGED_GATE_LABELS.get(_bad_gate, '未通过一致性审查')}"
                f"（{_bad_gate}），已拦截该段视频生成。"
                f"请重渲或修复该帧（或将质检档位调为 off 放行）后重试。"
            )
        elif not override_flagged and stale_slots and gate_level == 'standard' \
                and (start_slot in stale_slots or (slot + 1) in stale_slots):
            _stale = start_slot if start_slot in stale_slots else slot + 1
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_stale} 派生自旧的 i2i 链（上游帧已被单独重渲，"
                f"血统过期），已拦截以防跨链跳变。请顺序重渲 IMAGE {_stale} 及其后续帧，"
                f"或将质检档位调为 lenient 带警告放行。"
            )
        elif not override_flagged and drift_slots and gate_level == 'standard' \
                and (slot in drift_slots or start_slot in drift_slots):
            # 链回望确认该族段存在身份/机位断裂（manifest.chain_drift FAIL）：断裂
            # 区间内的帧对送 i2v 只会得到冻结闪切或自由变形片段，standard 档拦截。
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 位于链回望检测到的空间断裂族段内（chain_drift FAIL）：两端帧"
                f"可能不属于同一空间/机位，生成只会得到跳变或变形片段。请重渲断裂族段的帧"
                f"（或将质检档位调为 lenient 带警告放行）后重试。"
            )
        else:
            plan['action'] = 'generate'
            warnings = []
            _flagged_quality = _FLAGGED_QUALITY_GATES
            if override_flagged and (slot_to_quality.get(start_slot) in _flagged_quality
                                      or slot_to_quality.get(slot + 1) in _flagged_quality):
                _bad = start_slot if slot_to_quality.get(start_slot) in _flagged_quality else slot + 1
                _bad_gate = slot_to_quality.get(_bad)
                warnings.append(
                    f"视频 {slot} 的锚点帧 IMAGE {_bad} "
                    f"{_FLAGGED_GATE_LABELS.get(_bad_gate, '未通过一致性审查')}（{_bad_gate}），"
                    f"已按用户确认风险强制生成，请留意画面跳变/内容缺陷。"
                )
            if stale_slots and (start_slot in stale_slots or (slot + 1) in stale_slots):
                _stale = start_slot if start_slot in stale_slots else slot + 1
                warnings.append(
                    f"视频 {slot} 的锚点帧 IMAGE {_stale} 派生自旧 i2i 链（上游帧已被单独重渲），"
                    f"两端帧可能存在跨链色彩/内容漂移。"
                )
            if drift_slots and (slot in drift_slots or start_slot in drift_slots):
                warnings.append(
                    f"视频 {slot} 位于链回望检测到的空间断裂族段内（chain_drift FAIL），"
                    f"两端帧可能不属于同一空间/机位，片段存在跳变/变形风险。"
                )
            # i2v 帧对契约：两端锚点帧近乎相同→静止片段，差异过大→断裂/变形。
            # 提示性警告（不拦截）：断裂的硬拦截由 chain_drift 门负责，这里兜住
            # 链回望覆盖不到的帧对（如族内相邻帧渲成了复制帧）。英雄展示视频没有
            # 结束锚点（end_p 为 None），frame_pair_contract 内部会自动跳过，
            # 不需要在这里额外特判。
            verdict, mad = frame_pair_contract(start_p, end_p)
            if verdict == 'too_similar':
                warnings.append(
                    f"视频 {slot} 的首尾锚点帧近乎相同（MAD={mad:.1f}），i2v 大概率产出"
                    f"静止无变化片段——建议先重渲其中一帧或合并相邻拍。"
                )
            elif verdict == 'too_different':
                warnings.append(
                    f"视频 {slot} 的首尾锚点帧差异过大（MAD={mad:.1f}），疑似空间断裂，"
                    f"i2v 只能冻结闪切或自造画面过渡——建议改为 [CUT] 硬切或重渲帧。"
                )
            if warnings:
                plan['warning'] = ' '.join(warnings)
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
        # 起始锚点帧编号，现在总是等于自身 slot；字段保留供 merge_project_videos 的
        # 锚点一致性核对读取，兼容旧 manifest 里 TBCP v3 Bridge SPAN 的重定向记录。
        'start_anchor_slot': plan.get('start_anchor_slot', plan['slot']),
        'meta': plan.get('meta', ''),
        # 英雄展示视频（默认收尾步骤）：只上传首帧、无独立结束锚点，前端据此选用
        # 不同的槽位标签/文案，merge_project_videos 据此把它当可选附加片段处理。
        'is_hero': 'HERO' in str(plan.get('meta', '')).upper(),
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
                 process_check_fn=None, account_pool=None, pool_account_id=None):
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
        # 账号池：仅在本次生成由号池自动选号时才非空（手动指定/池子为空时都是
        # None），用于把"积分耗尽"的生成失败反馈给号池标记冷却。
        self.account_pool = account_pool
        self.pool_account_id = pool_account_id
        # 本次批次是否观测到"登录失效等待人工处理超时"——generate_video_sequence
        # 据此判断要不要自动切换号池账号重试剩余失败槽位。
        self.saw_login_required_timeout = False
        # 批量脚本在风控失败重试时自己换掉的号池账号（account_switched 事件）。
        # 批次跑完后并入 used_account_ids，免得补跑那一轮又挑回刚被判过的号。
        self.switched_accounts = []

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
        if stage.startswith('manual_intervention_'):
            # 登录失效/验证码/安全拦截：连接/页面级状态，不属于某一个具体分段，
            # 原样转发给 SPARK 事件流以驱动前端常驻横幅，不落 manifest。
            if stage == 'manual_intervention_timeout' and (details or {}).get('code') == 'login_required':
                # 记下来供 generate_video_sequence 在批次跑完后判断要不要
                # 自动切换号池账号重试剩余失败槽位。
                self.saw_login_required_timeout = True
            slot = None
            try:
                slot = self.pending[batch_idx]['plan'].get('slot')
            except Exception:
                pass
            self._emit(stage, {**(details or {}), 'index': slot, 'total': self.total})
            return None
        if stage == 'account_switched':
            # 批量脚本被判异常活动后换号重试（原来是换 IP，2026-07-26 统一改成换号）。
            # 换号是连接/账号级动作，不属于某个具体分段：把后续的积分耗尽标记跟着
            # 指到新账号上，否则会把失败记到已经不在用的那个号头上。
            new_id = (details or {}).get('user_id')
            if new_id:
                self.switched_accounts.append(new_id)
                if self.pool_account_id:
                    self.pool_account_id = new_id
            self._emit('video_warning', {
                'total': self.total,
                'message': (f"批量脚本被判异常活动，已换号重试："
                            f"{(details or {}).get('previous') or '当前账号'} → {new_id}"),
            })
            return None
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
                if action == 'warn':
                    # 结构化告警标记：终态质量风险汇总据此计数，不用猜留痕文本语义
                    info['process_warned'] = True
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
            message = (details or {}).get('message') or '生成失败'
            if self.pool_account_id and self.account_pool is not None:
                try:
                    if _get_credit_helpers().is_credit_exhausted_message(message):
                        self.account_pool.mark_exhausted(self.pool_account_id)
                except Exception as e:
                    print(f"Warning: 账号池积分耗尽标记失败 ({e})")
            self._fail(plan, message)
        return None


def generate_video_sequence(config, title, prompt_block, on_progress=None, target_slots=None,
                             override_flagged=False):
    import builtins
    builtins.google_fx_cancelled = False
    apply_google_fx_runtime_overrides(config)
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
                              stale_slots=load_stale_slots(manifest_data),
                              drift_slots=load_drift_break_slots(manifest_data),
                              override_flagged=override_flagged)

    if on_progress:
        on_progress('start', {
            'total': len(plans),
            'slots': [p['slot'] for p in plans],
        })

    google_fx_video, models = _get_google_fx_video_service()
    video_model = config.get('videoModel') or 'Veo 3.1 - Lite [Lower Priority]'
    # 时长 tab（4s/6s/8s/10s）只有 Omni Flash 模型的 Flow 面板才提供；Veo 系列面板没有
    # 这个 tab，误传会导致自动化脚本在那边找不到目标 tab、把 duration 判为未确认项而
    # 直接拒绝生成（见 google_fx.py 的 _verify_and_fix_fx_config）。这里按模型收窄，
    # 避免用户切换模型后配置面板里残留的旧时长值影响非 Omni 模型的生成。
    video_duration = config.get('videoDuration') or None
    if video_duration and str(video_model).strip().lower() != 'omni flash':
        video_duration = None
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
            end_image=plan['end_frame'] or '',
            model=video_model,
            ratio=config.get('imageAspectRatio') or '9:16',
            duration=video_duration,
            output_path=temp_out_dir
        )
        pending_items.append({'plan': plan, 'req': req, 'temp_out_dir': temp_out_dir})

    if pending_items:
        account_pool = _get_account_pool_service()
        pool_account_id = _select_pool_account(config, account_pool)
        if pool_account_id:
            apply_google_fx_runtime_overrides(config)

        def _process_gate(plan):
            return check_video_process(config, plan['dest_path'], plan['start_frame'],
                                       plan['end_frame'], plan['prompt'])

        def cancel_check_cb():
            if on_progress:
                try:
                    # 空探测调用：连接已死/用户已取消时返回 True
                    return on_progress('cancel_check', None)
                except Exception:
                    return True
            return False

        def _run_leg(items, account_id):
            """跑一条腿：绑定这一腿的号池账号，再交给批量脚本。返回 bridge。"""
            if account_id:
                config['googleFxUserId'] = account_id
                apply_google_fx_runtime_overrides(config)
            leg_bridge = _BatchBridge(items, len(plans), video_model, writer, on_progress,
                                      strict=strict_gates_enabled(config),
                                      process_check_fn=_process_gate,
                                      account_pool=account_pool,
                                      pool_account_id=(account_id or pool_account_id) if pool_account_id else None)
            try:
                google_fx_video.generate_videos_batch_google_fx(
                    [it['req'] for it in items],
                    on_progress=leg_bridge,
                    cancel_check=cancel_check_cb
                )
            finally:
                for it in items:
                    shutil.rmtree(it['temp_out_dir'], ignore_errors=True)
            return leg_bridge

        # 换号（不换 IP）：号池自动选号且可用账号 ≥2 个时才切腿，否则退化为单批次
        ring = _account_rotation_ring(config, account_pool, pool_account_id) if pool_account_id else []
        legs = plan_generation_legs(pending_items, ring, _account_switch_interval(config))
        used_account_ids = []
        login_required_accounts = []
        try:
            for idx, leg in enumerate(legs):
                # 腿与腿之间是唯一能干净停下来的位置：用户已取消就别再开下一个浏览器
                if idx > 0 and (getattr(builtins, 'google_fx_cancelled', False) or cancel_check_cb()):
                    break
                if idx > 0 and on_progress:
                    on_progress('video_warning', {
                        'total': len(plans),
                        'message': (f"换号继续：第 {idx + 1}/{len(legs)} 段改用号池账号 {leg['user_id']} "
                                    f"跑 {len(leg['items'])} 个片段（保持当前 IP，不换 IP）"),
                    })
                leg_bridge = _run_leg(leg['items'], leg['user_id'])
                leg_account_id = leg['user_id'] or pool_account_id
                if leg_account_id and leg_account_id not in used_account_ids:
                    used_account_ids.append(leg_account_id)
                # 批量脚本在这一腿内部换过的号也算"已用过"：补跑那一轮再挑回去
                # 等于把刚被判异常活动的号又推上去。
                for switched in leg_bridge.switched_accounts:
                    if switched not in used_account_ids:
                        used_account_ids.append(switched)
                if leg_bridge.saw_login_required_timeout and leg_account_id \
                        and leg_account_id not in login_required_accounts:
                    login_required_accounts.append(leg_account_id)
        finally:
            # 兜底：被 break/异常跳过的腿，临时目录还没被 _run_leg 清掉
            for it in pending_items:
                shutil.rmtree(it['temp_out_dir'], ignore_errors=True)

        # 🔁 2026-07-24: 号池自动选号的批次里，如果观测到"登录失效等待人工处理
        # 超时"（人工没能在 20 分钟内处理完），换一个账号很可能就能跑通剩下的
        # 失败槽位——不必等人工手动点"重试失败片段"。只在自动选号（非手动
        # 指定账号）时触发，且只补一轮，避免账号池被跑穿也无限换号刷屏。
        # 2026-07-25: 挑重试账号改走轮转环。原来这里二次调用 _select_pool_account 其实
        # 永远返回 None——首次选号已经把 googleFxUserId 写回 config，二次调用会把它当成
        # 手动指定而直接跳过，这条自动换号重试路径从未真正生效过。
        if pool_account_id and login_required_accounts and not getattr(builtins, 'google_fx_cancelled', False):
            for acct in login_required_accounts:
                try:
                    account_pool.mark_login_required(acct)
                except Exception as e:
                    print(f"Warning: 账号池登录失效标记失败 ({e})")
            done_slots = {v['slot'] for v in writer.data.get('videos', []) if v.get('status') == 'success'}
            retry_source = [it for it in pending_items if it['plan']['slot'] not in done_slots]
            next_account_id = _next_unused_account(
                config, account_pool, ring, set(used_account_ids) | set(login_required_accounts))
            if retry_source and next_account_id:
                if on_progress:
                    on_progress('video_warning', {
                        'total': len(plans),
                        'message': (f"检测到账号登录失效且等待人工处理超时，已自动切换号池账号 "
                                    f"重试剩余 {len(retry_source)} 个片段"),
                    })
                retry_items = []
                for it in retry_source:
                    new_temp_dir = tempfile.mkdtemp()
                    old_req = it['req']
                    new_req = models.VideoRequest(
                        prompt=old_req.prompt, image=old_req.image, end_image=old_req.end_image,
                        model=old_req.model, ratio=old_req.ratio, duration=old_req.duration,
                        output_path=new_temp_dir,
                    )
                    retry_items.append({'plan': it['plan'], 'req': new_req, 'temp_out_dir': new_temp_dir})

                _run_leg(retry_items, next_account_id)

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
        return "存在" + "；".join(parts) + "，已拒绝合并。请重试这些片段，或选择「跳过缺口合并（2倍速）」。"


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
        # 相邻两段直接相接（成片在此处硬切）。'skipped_bridge_hold' 已停用（单一过门拍
        # 收编后不再有需要跳过的 HOLD 槽位），仅为兼容旧 manifest 保留识别。
        skipped_cut = {v.get('slot') for v in videos
                       if isinstance(v, dict) and v.get('status') in ('skipped_cut', 'skipped_bridge_hold')}
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
            # 手动上传（/api/upload_video）在落盘前已经做过同一套锚点校验，且不匹配时
            # 要求用户显式 force 确认覆盖——这里再复查一遍会让 force 形同虚设（用户
            # 明知不匹配还是选择了这个文件，合并时却被当"串片"拦下）。手动上传的槽位
            # 直接信任，不重新跑锚点门禁。
            if v.get('source') == 'manual_upload':
                good[slot] = abs_path
                continue
            # start_anchor_slot 现在总是等于 slot（单一过门拍起止帧绑定和普通拍一样）；
            # 字段仍保留读取，兼容旧 manifest 里 TBCP v3 Bridge SPAN 的重定向记录。
            start_slot = v.get('start_anchor_slot') or slot
            start_p = _resolve_frame(frame_by_slot.get(start_slot))
            end_p = _resolve_frame(frame_by_slot.get(slot + 1))
            # 合并门禁没有请求级 config，strict 开关直接取服务端配置
            ok, reason = verify_video_anchors(abs_path, start_p, end_p, strict=strict_gates_enabled())
            if not ok:
                mismatched.append(slot)
                continue
            good[slot] = abs_path

        # 英雄展示视频（[HERO]，默认收尾步骤）：可选附加片段，不参与上面的完整性/
        # 锚点门禁——缺失或未通过校验时直接跳过，绝不阻塞主体成片的合并。成功时
        # 追加到 expected_slots 末尾（它的槽位号总是大于所有正片槽位），随主体
        # 一起拼接进最终成片，作为收尾的完工展示镜头。
        hero_entries = [v for v in videos if isinstance(v, dict) and 'HERO' in str(v.get('meta', '')).upper()]
        if hero_entries:
            hero_v = hero_entries[0]
            hero_slot = hero_v.get('slot')
            if hero_v.get('status') == 'success' and hero_v.get('file'):
                hero_abs = _resolve_abs(hero_v['file'])
                if os.path.exists(hero_abs):
                    if hero_v.get('source') == 'manual_upload':
                        # 手动上传同样信任 /api/upload_video 已做过的锚点校验/force 确认。
                        good[hero_slot] = hero_abs
                        expected_slots = expected_slots + [hero_slot]
                    else:
                        hero_anchor_slot = hero_v.get('start_anchor_slot') or hero_slot
                        hero_anchor_p = _resolve_frame(frame_by_slot.get(hero_anchor_slot))
                        # 只上传首帧，没有独立的结束锚点——只核对片段首帧，不核对尾帧。
                        ok, _reason = verify_video_anchors(hero_abs, hero_anchor_p, None,
                                                            strict=strict_gates_enabled())
                        if ok:
                            good[hero_slot] = hero_abs
                            expected_slots = expected_slots + [hero_slot]
                        else:
                            print(f"[WARN] 英雄展示视频（槽位 {hero_slot}）锚点校验未通过，跳过附加合并。")
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

    # 强制合并且确有缺口：跳过缺失/串片槽位，仅拼接可用片段（用户已在门禁提示里看到
    # 具体缺了哪些槽位，不是背地里丢弃）
    if allow_partial and (missing or mismatched):
        return _merge_skip_missing(
            project_dir, manifest_data, expected_slots, good, missing, mismatched,
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


def _merge_skip_missing(project_dir, manifest_data, expected_slots, good, missing, mismatched):
    """强制合并（allow_partial）：2026-07-22 改版——不再用起始锚点帧定格+「缺失」标注
    填充缺口（占位预览这套 filter_complex/drawtext 太重，且冻结帧撑时长的观感也不好），
    直接跳过缺失/串片的槽位，把仍然可用的片段按原顺序拼接、2x 加速，跳过处是硬切。
    不是背地里丢弃缺口——门禁提示里用户已经看到具体缺了哪些槽位，这里把 skipped_slots
    带回给调用方展示，只是不再用假帧撑时长。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    video_files = [good[s] for s in expected_slots if s in good]
    if not video_files:
        return None
    skipped_slots = sorted(set(missing) | set(mismatched))

    concat_list_path = os.path.join(project_dir, 'concat_list.txt')
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for vf in video_files:
            f.write(f"file '{vf.replace(chr(92), '/')}'\n")

    title = manifest_data.get('title', '')
    chinese_name = _project_display_name(title)
    # 绝对路径：见 2026-07-22 的踩坑记录——project_dir 本身是相对路径（server_common.
    # OUTPUT_ROOT='outputs'），若 output_path 沿用相对拼接，一旦后续改动又把 ffmpeg 子
    # 进程的 cwd 切到 project_dir，就会被当成"project_dir 下再嵌一层 project_dir"解析。
    output_path = os.path.abspath(os.path.join(project_dir, f"{chinese_name}_partial_2x.mp4"))

    # 清理项目根下旧 mp4（保持单一成片）
    for fname in os.listdir(project_dir):
        if fname.lower().endswith('.mp4') and os.path.isfile(os.path.join(project_dir, fname)):
            try:
                os.remove(os.path.join(project_dir, fname))
            except Exception as e:
                print(f"Warning: could not remove old merged file {fname} ({e})")

    has_audio = False
    probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_files[0]]
    try:
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding='utf-8', errors='replace', check=True)
        has_audio = "audio" in res.stdout.lower()
    except Exception as probe_err:
        print(f"[DEBUG] ffprobe check failed: {probe_err}")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path]
    if has_audio:
        cmd += ["-filter_complex", "[0:v]setpts=0.5*PTS[v];[0:a]atempo=2.0[a]",
                "-map", "[v]", "-map", "[a]", "-c:a", "aac"]
    else:
        cmd += ["-filter_complex", "[0:v]setpts=0.5*PTS[v]", "-map", "[v]"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]

    print(f"[INFO] Skip-merge: {len(video_files)} segments, skipped {skipped_slots} -> {output_path}")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding='utf-8', errors='replace')
    try:
        os.remove(concat_list_path)
    except Exception:
        pass

    if res.returncode != 0:
        print(f"[ERROR] skip-merge ffmpeg failed: {res.stderr}")
        raise RuntimeError(f"跳过缺口合并失败（FFmpeg）：{res.stderr[-400:]}")

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
        'skipped_slots': skipped_slots,
    }
