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
    apply_google_fx_runtime_overrides, fx_cancel_context, fx_request_deadline,
    read_manifest, write_manifest, strict_gates_enabled, qa_gate_level,
    resolve_video_duration,
    stamp_manifest_capabilities,
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
# 2026-07-31：22~35 这一带此前静默通过——够不上"疑似空间断裂"，却已经超出 8 秒 i2v
# 能连续插值的跨度。实测这一带的片段全都是"匀速推进→停滞→突进补足"的形态：模型
# 交不出连续过程，只能停一会儿再跳一步把差量补齐，观感就是推进过程中的跳变。
# 与 prompt_pipeline 的拍重门禁（rhythm_ladder_violations）分工：那道门看的是 ladder
# **声明**的变化量（工序数/格位/层族），是估算；这里看的是两张**已渲染**帧之间的
# 实测差量，是地面真相。声明得再规矩，帧渲出来跨度过大照样会跳。
_PAIR_TOO_WIDE_MAD = 22.0
# i2v 模型的单段基准输出时长。prompt_pipeline.VIDEO_DURATION 是同一个数的权威来源，
# 但这里不 import：video_generator 与 prompt_pipeline 互相引用，模块级 import 会成环
# （现有的 run_video_process_check 也是在函数体内延迟导入的）。只用于文案。
_CLIP_BASE_SECONDS = 8.0
# 运镜拍标签：这些拍的锚点跨度由镜头位移决定，不受"施工增量"类判据管辖。
_CAMERA_MOVE_META = ('HERO', 'BRIDGE', 'CUT')


def _is_camera_move_slot(meta):
    """槽位 meta 是否属于运镜拍（[HERO]/[BRIDGE]/[BRIDGE TURN]/[CUT]）。"""
    upper = str(meta or '').upper()
    return any(tag in upper for tag in _CAMERA_MOVE_META)


def frame_pair_contract(start_frame_path, end_frame_path):
    """i2v 提交前对首尾锚点帧做本地相似度双向检查。

    返回 (verdict, mad)，verdict ∈ 'ok' | 'too_similar' | 'too_wide' | 'too_different'
    | 'skipped'（环境异常/文件缺失时 'skipped'，不拦截——这只是提示性契约，硬拦截由
    质量门负责）。'too_wide' 介于 ok 与 too_different 之间：空间没断，但一段 8 秒
    片段承载不下这个差量，需要在两帧之间补一张中间帧把这一拍拆成两拍。"""
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
        if mad > _PAIR_TOO_WIDE_MAD:
            return 'too_wide', mad
        return 'ok', mad
    except Exception:
        return 'skipped', None


def _extract_video_frame(video_path, out_png, position, sseof_offset=0.3):
    """position: 'first' | 'last'。返回 True 表示抽帧成功。

    sseof_offset 是 'last' 的取帧窗口（从片尾往回数的秒数），取窗口内的**第一**帧。
    默认 0.3 秒是锚点校验/冻结检测标定过的口径，不要改；节奏采样要的是真正的末帧，
    自己传更小的偏移（见 clip_step_profile）。"""
    if position == 'first':
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-frames:v", "1", out_png]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-sseof", f"-{sseof_offset:.3f}",
               "-i", video_path, "-frames:v", "1", "-update", "1", out_png]
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


# ── 段内推进节奏检测（2026-07-31 "视频推进跳变"复盘）─────────────────────────
# detect_frozen_clip 判的是「整段冻住」（max 相邻 MAD < 3），而实测下来真正让人看到
# 跳变的不是冻结，是**速度不匀**：片段先匀速推进、中途停滞 1 秒左右、再突进一步把
# 差量补齐。那种片段的 max MAD 有 20+，冻结检测一路放行。
#
# 判据（16 步等距抽样，纯本地、零 LLM 成本、确定性）：
#   S1 停滞：≥2 个**连续**步长 < 0.35×中位步长 —— 推进中途停住了
#   S2 突进：峰值步长 ≥ 3.0×中位步长        —— 差量被压在一个窗口里补完
# 任一命中即判「推进不匀」。两条都用中位数做基准而不是绝对阈值：片段之间的总变化量
# 本来就差好几倍（实测 8~101），绝对阈值只会把大变化量的片段全部误报。
#
# 首末各 2 步（片头/片尾各 12.5%）不参与停滞判定：Out-and-In 契约要求工人退场后画面
# 静置成干净的交接帧，片尾本来就该停下来；片头是**同一条接缝的另一端**——i2v 起步时
# 会先保持一小段输入锚点帧再开始动，这段静置同样是设计内的。
# 2026-08-01 复盘：此前只豁免了片尾，于是片头静置被系统性地判成停滞——实测 36 个片段
# 里 5 次 S1 命中有 4 次落在 stall_at==0（片头第一格）。短片更严重：同一条片子裁到 4 秒
# 时片头连续 5 个采样步低于停滞线、6 秒时 3 个（片头静置的绝对时长不变，格子却变短了）。
_PACE_SAMPLES = 17            # 采样帧数（= 16 个步长），8 秒片段约 0.5 秒一格
_PACE_STALL_RATIO = 0.35      # 低于中位步长这个比例即算一个停滞步
_PACE_STALL_RUN = 2           # 连续停滞步达到这个数才判 S1
_PACE_HEAD_EXEMPT = 2         # 片头豁免的步数（起步静置），与片尾对称
_PACE_TAIL_EXEMPT = 2         # 末尾豁免的步数（收尾静置）
_PACE_PEAK_RATIO = 3.0        # 峰值/中位步长达到这个倍数即判 S2


def clip_step_profile(video_path, tmp_dir, samples=_PACE_SAMPLES):
    """等距抽样求片段的「推进速度曲线」。

    返回 (steps, duration, error)：steps 是 samples-1 个相邻采样帧的 MAD（numpy 数组），
    error 为 None 表示成功；失败时返回 (None, 0.0, 'skipped:...')。节奏检测与合并侧的
    时间重映射共用这一份测量，避免同一条片子被抽两遍帧。"""
    try:
        import numpy as np
        params = _ffprobe_video_params(video_path)
        duration = (params or {}).get('duration') or 0.0
        if duration <= 0:
            return None, 0.0, 'skipped:no_duration'
        # 末样本必须落在**最后一帧**上。其余 16 个采样点都在 duration/(samples-1) 的
        # 等距网格上，而 _extract_video_frame 默认的 0.3 秒回退窗口与网格无关：8 秒片
        # 的网格步是 0.5 秒，末样本却落在 7.7 秒（第 15 个采样点在 7.5 秒），末步实测
        # 只跨 0.2 秒——被系统性压低，污染中位步长和 steps.sum()（后者是重映射的归一化
        # 分母）。4 秒片直接出错：网格步 0.25 秒，0.3 秒偏移落到第 15 个采样点**之前**
        # （3.700 < 3.750），末步测的是一段倒着的区间（实测 -0.05 秒）。
        # 改成按帧率留两帧余量：窗口里稳定有帧可取，落点即片尾最后一两帧。
        fps = (params or {}).get('fps') or 0
        tail_off = min(0.3, 2.0 / fps) if fps > 0 else 0.3
        grays = []
        for i in range(samples):
            png = os.path.join(tmp_dir, f'pace_{i}.png')
            if i == samples - 1:
                # 末帧走 -sseof 的专用抽帧：按时间戳定位 duration 附近会落到最后一个
                # 有效帧之后（24fps 下最后一帧在 duration-0.042），抽不出任何东西。
                ok = _extract_video_frame(video_path, png, 'last', sseof_offset=tail_off)
            else:
                t = duration * i / (samples - 1)
                res = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video_path,
                     "-frames:v", "1", png],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace', timeout=60)
                ok = (res.returncode == 0 and os.path.exists(png)
                      and os.path.getsize(png) > 0)
            if not ok:
                return None, 0.0, f'skipped:sample_{i}_failed'
            grays.append(_load_gray_thumb(png))
        steps = np.array([float(np.abs(grays[i + 1] - grays[i]).mean())
                          for i in range(len(grays) - 1)])
        return steps, duration, None
    except Exception as e:
        return None, 0.0, f'skipped:{type(e).__name__}'


def pace_verdict(steps, duration):
    """由速度曲线判「推进是否不匀」。返回 (broken: bool, reason: str)。

    与 detect_pace_break 分开是因为合并侧的时间重映射也要这个判定，且必须与检测侧
    **同一套判据**：重映射只对判定为跳变的片段动手，本来就匀的片子一律不碰——
    实测过"无差别重映射"会把个别本来通过的片子拉出新的停滞窗口，得不偿失。"""
    try:
        import numpy as np
        seconds_per_step = duration / len(steps)

        # S0 全程冻结：绝对判据，必须在相对判据之前。冻结片段的每一步都接近 0，
        # 中位数也接近 0，相对判据（步长/中位数）会算出"完美匀速"把它放行——
        # detect_frozen_clip 用的就是这条绝对阈值，这里沿用同一个常数。
        if float(steps.max()) < _FROZEN_CLIP_MAD:
            return True, (f'本地节奏检测：整段几乎无变化（峰值步长 {steps.max():.2f} < '
                          f'{_FROZEN_CLIP_MAD}）—— i2v 没有可执行的动作，产出了静止片段。')

        median = float(np.median(steps))
        if median <= 0:
            # 大半程冻死、只在个别窗口跳一下：中位数为 0，比值判据除不了。
            return True, ('本地节奏检测：过半采样步完全静止，全部变化压在个别窗口里完成 '
                          f'—— 观感是定格后的跳变。峰值步长={steps.max():.2f}')
        peak_ratio = float(steps.max()) / median
        detail = (f'中位步长={median:.2f} 峰值={steps.max():.2f} '
                  f'峰值/中位={peak_ratio:.1f}x')

        # S1 停滞：连续低于中位步长 35% **且**绝对变化量也确实小的步数（不含首尾豁免区）。
        # 两个条件缺一不可。只用相对判据时，一条整体很"猛"的片子里 MAD 3.3 的一步会被
        # 判成停滞——可它在绝对量上仍在正常推进。绝对下限沿用冻结检测的同一个常数：
        # 低于它才叫"没在动"。这条也让判据在时间重映射前后可比：重映射会抬高中位步长，
        # 纯相对判据会因此在一条**变得更匀**的片子上凭空报出停滞。
        stall_cut = min(_PACE_STALL_RATIO * median, _FROZEN_CLIP_MAD)
        body = steps[_PACE_HEAD_EXEMPT:max(0, len(steps) - _PACE_TAIL_EXEMPT)]
        longest_run = run = 0
        stall_at = None
        for i, value in enumerate(body):
            if value < stall_cut:
                run += 1
                if run > longest_run:
                    longest_run, stall_at = run, i - run + 1
            else:
                run = 0

        if longest_run >= _PACE_STALL_RUN:
            # stall_at 是 body 内的下标，报秒数要加回片头豁免的偏移
            start_s = (stall_at + _PACE_HEAD_EXEMPT) * seconds_per_step
            return True, (
                f'本地节奏检测：推进在第 {start_s:.1f}~{start_s + longest_run * seconds_per_step:.1f} 秒'
                f'停滞（连续 {longest_run} 个采样步低于中位步长的 {_PACE_STALL_RATIO:.0%}），'
                f'差量被压到后面补完 —— 观感是推进过程中的跳变。{detail}')
        if peak_ratio >= _PACE_PEAK_RATIO:
            peak_s = int(steps.argmax()) * seconds_per_step
            return True, (
                f'本地节奏检测：第 {peak_s:.1f} 秒处有一次突进（该步变化量是中位步长的 '
                f'{peak_ratio:.1f} 倍，阈值 {_PACE_PEAK_RATIO:.1f}），推进速度严重不匀 —— '
                f'观感是推进过程中的跳变。{detail}')
        return False, f'pace_ok:{detail}'
    except Exception as e:
        return False, f'skipped:{type(e).__name__}'


def detect_pace_break(video_path, tmp_dir):
    """本地推进节奏检测：抽样 + 判定。返回 (broken: bool, reason: str)。

    抽帧/环境异常一律返回 (False, 'skipped:...') —— 与 detect_frozen_clip 同款
    fail-open 语义，这是提示性判据，不该因为环境退化去拦截生成。"""
    steps, duration, error = clip_step_profile(video_path, tmp_dir)
    if error:
        return False, error
    return pace_verdict(steps, duration)


# 2026-08-02：恢复视频 VLM 内容复审。仍可通过 config.videoProcessVlmReview=false
# 针对单次任务关闭；qaGateLevel=off 继续作为整套质检的总开关。
#
# 2026-07-31 补充：这个开关此前在 check_video_process 的函数首行 return，把本地的
# detect_frozen_clip 也一起关掉了——那是纯本地、确定性、零成本的判据，没有误判风险，
# 属于误伤。用户当初反对的是 **VLM 误判导致删片重来**，不是反对知情。现在本地判据
# （冻结 + 推进节奏）恒定运行且**最高只到 warn 永不 reject**，被这个开关关掉的仅剩
# VLM 那一段。
_VIDEO_PROCESS_GATE_DISABLED = False


def _video_process_vlm_enabled(config):
    value = (config or {}).get('videoProcessVlmReview', True)
    if isinstance(value, str):
        value = value.strip().lower() not in ('0', 'false', 'no', 'off')
    return bool(value) and not _VIDEO_PROCESS_GATE_DISABLED


def check_video_process(config, video_path, start_frame, end_frame, prompt, meta=''):
    """段内过程门：verify_video_anchors 只钉住首尾两端，段内 8 秒是盲区（空心视频/
    幽灵内容 / 推进跳变的来源）。分两段：

    A. **本地判据**（冻结 + 推进节奏）——确定性、零 LLM 成本、恒定运行，
       **最高只到 'warn'，永不 'reject'**。这一段不受 _VIDEO_PROCESS_GATE_DISABLED
       管辖：那个开关关的是 VLM 复审的误判风险，不该连带把免费的客观测量一起关掉。
    B. **VLM 复审**（prompt_pipeline.run_video_process_check）——判"两锚点之间是否
       真的发生了描述的施工过程"，按档位映射拒收/告警；可由
       videoProcessVlmReview=false 对单次任务显式关闭。

    运镜拍（[HERO]/[BRIDGE]/[CUT]）跳过节奏判据：它们的速度曲线由镜头调度决定，
    推进/缓入缓出都是设计内的，用施工推进的匀速标准去量必然误报。

    返回 (action, reason)，action ∈：
      'accept' —— 通过 / off 档跳过 / 环境·判定服务异常 fail-open 放行（reason 留痕）
      'warn'   —— 检出本地硬伤，或 lenient 档下 VLM 检出硬伤：不拒收（重试要再烧
                   一整段视频额度），发 video_warning + manifest 留痕
      'reject' —— standard 档 VLM 检出硬伤，或 strictGates 开启时环境/判定异常
    """
    level = qa_gate_level(config)
    if level == 'off':
        return 'accept', 'Skipped (qaGateLevel=off: 质检门已关闭)'

    with tempfile.TemporaryDirectory() as td:
        # ── A. 本地判据（恒定运行，只告警）──
        if _is_camera_move_slot(meta):
            pace_reason = 'skipped:camera_move_slot（运镜拍不适用施工推进的匀速判据）'
        else:
            broken, pace_reason = detect_pace_break(video_path, td)
            if broken:
                return 'warn', pace_reason

        if not _video_process_vlm_enabled(config):
            return 'accept', ('Skipped VLM 复审 (videoProcessVlmReview=false)；'
                              f'本地节奏检测: {pace_reason}')

        # ── B. VLM 复审 ──
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
_FLAGGED_QUALITY_GATES = (
    'vlm_qa_failed', 'sequence_review_flagged', 'manual_flagged',
    'frame_continuity_failed',
)

_FLAGGED_GATE_LABELS = {
    'vlm_qa_failed': '未通过质检',
    'sequence_review_flagged': '未通过一致性审查',
    'frame_continuity_failed': '生成期场景连续性检查失败',
    'manual_flagged': '被人工标记存在问题',
}

# 「从来没被判过」的帧。渲染期不做任何审查（2026-08-05 起生成期一致性审查已整体
# 移除），一致性审查只能由用户手动触发（pipeline_orchestrator._sequence_consistency_review），
# 所以帧默认一直停在初始的 pending_manual_review 上。
# 它们不是坏帧，不该拦截；但此前也**完全没有任何提示**，于是未经判定的锚点帧照常烧
# 视频额度出片——实测某一单 12 帧里有 4 帧是这个状态，而汇总显示"审查通过"。
# 这里只做一件事：在花钱那一刻把"这张没人看过"说出来。
_UNREVIEWED_QUALITY_GATES = ('pending_manual_review', 'sequence_review_skipped')

_UNREVIEWED_GATE_LABELS = {
    'pending_manual_review': '从未经过一致性审查',
    'sequence_review_skipped': '审查服务不可用，此帧未经审查',
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


def load_frame_canvas_uuids(manifest_data, slot_to_path):
    """帧文件绝对路径 → 该帧在 Flow 画布上的媒体 UUID（manifest.frames[].fx_uuid）。

    帧序列是在项目绑定的 Flow 画布上生成的，那张图本身就是画布资产。带上 UUID，
    视频阶段回到同一画布即可直接挂载，不必把整批帧再上传一轮。

    UUID 与本地文件的对应关系由写入方维持：手动换图会清掉 fx_uuid（server 的上传
    帧分支），交换槽位时 fx_uuid 跟着图一起搬（swap_frame_slots）。所以这里读到
    UUID 就意味着"该槽位当前这张本地图 == 画布上那张图"。同一路径出现两个不同
    UUID（异常状态）时两条都丢掉，宁可重传也不挂错帧。
    """
    slot_to_uuid = {}
    for frame in (manifest_data or {}).get('frames', []):
        if not isinstance(frame, dict):
            continue
        fx_uuid = str(frame.get('fx_uuid') or '').strip()
        if not fx_uuid:
            continue
        try:
            slot_to_uuid[int(frame['slot'])] = fx_uuid
        except (KeyError, TypeError, ValueError):
            continue

    uuid_by_path = {}
    conflicted = set()
    for slot, path in (slot_to_path or {}).items():
        fx_uuid = slot_to_uuid.get(slot)
        if not path or not fx_uuid:
            continue
        if uuid_by_path.get(path, fx_uuid) != fx_uuid:
            conflicted.add(path)
        uuid_by_path[path] = fx_uuid
    for path in conflicted:
        uuid_by_path.pop(path, None)
    return uuid_by_path


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


# 旧单硬切占位声明的正文前缀（与 prompt_pipeline.HARD_CUT_PLACEHOLDER_PREFIX 同一常量，
# 这里复制一份字面量是为了让 plan_video_slots 保持零依赖可单测；该字符串已冻结，新单不再
# 产生这种正文，见 prompt_pipeline.HARD_CUT_VIDEO_PLACEHOLDER 的说明）。
_LEGACY_HARD_CUT_PLACEHOLDER_PREFIX = 'DECLARED HARD CUT'


def _is_legacy_hard_cut_placeholder(body):
    """该 VIDEO 正文是不是 2026-07-30 之前落盘的硬切占位声明（那种槽位不生成视频）。"""
    return str(body or '').strip().upper().startswith(_LEGACY_HARD_CUT_PLACEHOLDER_PREFIX)


def _is_declared_editorial_cut(body, meta=''):
    """识别真正的剪辑硬切，而不是 ``[CUT]`` 标签下的推门过门镜头。

    ``CUT`` 仍可表示一段真实的跨越运镜；只有正文明确声明 editorial/hard cut，
    并同时否定插值/过渡画面时，才应跳过 I2V、交给合并器直接拼接相邻片段。
    """
    if 'CUT' not in str(meta or '').upper():
        return False
    text = ' '.join(str(body or '').lower().split())
    declares_cut = any(token in text for token in (
        'intentional editorial cut', 'declared hard cut', 'hard cut',
        '直接硬切', '编辑硬切', '剪辑硬切', '直切',
    ))
    forbids_interpolation = any(token in text for token in (
        'not an interpolated transformation', 'no generated in-between',
        'no morphing', 'no camera travel', '不做插值', '禁止插值', '不生成过渡',
    ))
    return declares_cut and forbids_interpolation


def plan_video_slots(video_slots, slot_to_path, slot_to_quality, videos_dir, target_slots=None,
                      strict=False, verify_fn=None, gate_level='standard', stale_slots=None,
                      override_flagged=False):
    """为每个视频槽位做生成决策。只读文件系统，不做任何写操作，可单测。

    video_slots: _parse_prompt_slots 的 videos 输出（{slot: str 或 {'body':...}}）
    target_slots: None=整单生成；列表=只处理这些槽位（显式重试）
    strict/verify_fn: 断点续传复用判定用，见下方注释；verify_fn 默认为
      verify_video_anchors，测试可注入假实现，避免依赖真实 ffmpeg/视频文件。
    override_flagged: 前端"确认风险，强制生成"一次性确认（与全局 qaGateLevel 配置
      无关，只对本次请求生效）。豁免的是一致性审查衍生的两道硬拦——
      'vlm_qa_failed'/'sequence_review_flagged' 质检终态与血统过期（stale_slots）。
      降级帧
      （i2i_fallback_degraded）是唯一保持不受影响、始终硬拦的独立门禁——降级帧
      本身已是明确的生成失败兜底产物，不属于"一致性审查有疑虑但帧本身完好"的
      范畴，强推只会浪费视频生成额度。（2026-07-30 核实：这个值自 f5a003b 起已无
      生产方，该门禁如今只对旧 manifest 生效，见下方分支上的注释。）True 时改判
      'generate' 并落一条 warning 说明是人工确认风险后强制放行的。

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
        # 旧单的硬切占位声明按原始正文识别（改写 IMAGE 编号之前），见下方 skip_cut 分支。
        is_legacy_cut_placeholder = _is_legacy_hard_cut_placeholder(prompt)
        is_editorial_cut = _is_declared_editorial_cut(prompt, meta)
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

        # 声明式切入槽位（[CUT]，TBCP v2 hard_cut 变体）现在照常生成视频：正文是一段
        # 普通的过门跨越镜头（门在片段里被推开、镜头推进入内），起止帧绑定与普通拍一致。
        # 2026-07-30 之前该槽被无条件跳过，成片在过门处只有两张静帧硬拼，用户看到的就是
        # "过门硬切镜头不生成"。
        #
        # 唯一仍然跳过的是切换前落盘的旧单：它们的正文是占位声明本身
        # （prompt_pipeline.HARD_CUT_VIDEO_PLACEHOLDER，开头即 'DECLARED HARD CUT'），
        # 拿这段声明去送 i2v 只会得到一段照读声明的废片。按正文识别而不是按 [CUT] 标签
        # 识别——标签在新单里保留着别的用途（帧渲染的 t2i 新链头、族锚、审查豁免）。
        if is_legacy_cut_placeholder or is_editorial_cut:
            plan['action'] = 'skip_cut'
            cut_kind = '声明式剪辑硬切' if is_editorial_cut else '旧单的硬切占位槽位'
            plan['reason'] = f"视频 {slot} 是{cut_kind}：不提交 I2V，成片在此处直接硬切。"
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
        # 注意：'i2i_fallback_degraded' 自 f5a003b 起已无生产方——当年产生它的是
        # "参考图缺失就退回文生图"那条静默降级路径，现在那里直接抛
        # RuntimeError('帧序列禁止退回文生图')，不再产出脱链帧。所以这一条如今只对
        # 旧 manifest 生效，留着是为了让那些老单仍被拦住。今天真正活着的降级信号是
        # 帧上的 degraded_reason（chat 通道降档），它按设计只做提示不拦截。
        elif slot_to_quality.get(start_slot) == 'i2i_fallback_degraded' \
                or slot_to_quality.get(slot + 1) == 'i2i_fallback_degraded':
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的起始帧 IMAGE {start_slot} 或结束帧 IMAGE {slot + 1} 属于降级帧（i2i fallback degraded），"
                f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
            )
        elif not override_flagged and (
            slot_to_quality.get(start_slot) in _FLAGGED_QUALITY_GATES
            or slot_to_quality.get(slot + 1) in _FLAGGED_QUALITY_GATES
        ) and (
            gate_level != 'off'
            or slot_to_quality.get(start_slot) == 'frame_continuity_failed'
            or slot_to_quality.get(slot + 1) == 'frame_continuity_failed'
        ):
            # 已知坏帧不再烧昂贵的视频生成额度，见 _FLAGGED_QUALITY_GATES。
            _bad = start_slot if slot_to_quality.get(start_slot) in _FLAGGED_QUALITY_GATES else slot + 1
            _bad_gate = slot_to_quality.get(_bad)
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_bad} {_FLAGGED_GATE_LABELS.get(_bad_gate, '未通过一致性审查')}"
                f"（{_bad_gate}），已拦截该段视频生成。"
                + ("请重渲或修复该帧后重试。"
                   if _bad_gate == 'frame_continuity_failed'
                   else "请重渲或修复该帧（或将质检档位调为 off 放行）后重试。")
            )
        elif not override_flagged and stale_slots and gate_level != 'off' \
                and (start_slot in stale_slots or (slot + 1) in stale_slots):
            # 2026-07-30：血统过期从"standard 拦 / lenient 只警告"改成"除 off 档一律拦"。
            # 理由是它和其它门禁不同类：一致性审查的判定有主观成分（可能误报，宽松档
            # 放行是合理档位），而血统过期是确定性事实——上游帧被单独换过，这一对锚点
            # 帧确实来自两条不同的 i2i 链，送 i2v 必然是跨链漂移。lenient 档只发一条
            # 警告，实际使用中等于没拦（警告和其它十几条混在一起滚过去），2026-07-27
            # 的 ice_cave slot3 事故就是这么走到成片的。要放行请走 override_flagged
            # （前端确认风险，见 confirmSequenceReviewOverride）或把档位调成 off。
            _stale = start_slot if start_slot in stale_slots else slot + 1
            plan['action'] = 'blocked'
            plan['reason'] = (
                f"视频 {slot} 的锚点帧 IMAGE {_stale} 派生自旧的 i2i 链（上游帧已被单独重渲，"
                f"血统过期），已拦截以防跨链跳变。请顺序重渲 IMAGE {_stale} 及其后续帧，"
                f"或在确认风险后强制生成。"
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
            # 未经判定的锚点帧：不拦（审查是手动的，没审不等于有问题），但必须说出来。
            _unjudged = [s for s in (start_slot, slot + 1)
                         if slot_to_quality.get(s) in _UNREVIEWED_QUALITY_GATES]
            if _unjudged:
                _names = '、'.join(f"IMAGE {s}" for s in _unjudged)
                _gate = slot_to_quality.get(_unjudged[0])
                warnings.append(
                    f"视频 {slot} 的锚点帧 {_names} {_UNREVIEWED_GATE_LABELS.get(_gate, '未经审查')}"
                    f"（{_gate}）——这段的画面一致性没有任何判定背书。若要先审再出片，"
                    f"请在帧网格点「一致性审查」。"
                )
            # i2v 帧对契约：两端锚点帧近乎相同→静止片段，差异过大→断裂/变形。
            # 提示性警告（不拦截）。英雄展示视频没有结束锚点（end_p 为 None），
            # frame_pair_contract 内部会自动跳过，不需要在这里额外特判。
            verdict, mad = frame_pair_contract(start_p, end_p)
            plan['pair_contract'] = {'verdict': verdict, 'mad': mad}
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
            elif verdict == 'too_wide' and not _is_camera_move_slot(meta):
                # 运镜拍（[BRIDGE]/[CUT]/[HERO]）不报：它们的大跨度来自镜头位移本身，
                # 是设计内的，补一张中间帧既无意义也无处可插。
                warnings.append(
                    f"视频 {slot} 的首尾锚点帧跨度偏大（MAD={mad:.1f}，连续插值上限约"
                    f"{_PAIR_TOO_WIDE_MAD:.0f}），一段 {_CLIP_BASE_SECONDS:.0f} 秒 i2v 装不下这个差量，"
                    f"大概率产出「推进→停滞→突进补足」的跳变片段——建议在 IMAGE {start_slot} 与 "
                    f"IMAGE {slot + 1} 之间补一张中间帧，把这一拍拆成两拍。"
                )
                plan['split_recommendation'] = {
                    'required': True,
                    'between_images': [start_slot, slot + 1],
                    'reason': 'anchor_span_too_wide',
                    'suggested_midpoint': (
                        f"在 IMAGE {start_slot} 与 IMAGE {slot + 1} 之间增加只完成约一半施工量的中间锚点"
                    ),
                }
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
        # 本段在成片里的时间缩放（按拍重分配，见 _paced_merge_filter）。显式落盘而不是
        # 每次合并时重新从 meta 解析：没有留痕的时间缩放不可复现，出了观感问题无法定位
        # 是哪一段的系数不对。
        'clip_speed': _clip_speed_from_meta(plan.get('meta', '')),
    }
    if plan.get('pair_contract'):
        info['pair_contract'] = plan['pair_contract']
    if plan.get('split_recommendation'):
        info['split_recommendation'] = plan['split_recommendation']
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
        # 视频阶段的能力印章：numpy/ffmpeg 缺失时防串片的首尾帧锚点校验、i2v 帧对
        # 契约、冻结检测全都静默跳过（返回 'skipped'/False，不拦截也不报错）。每次
        # 落盘都重盖一次，环境中途变化也能如实反映（stamp 内部按阶段覆盖，不累积）。
        stamp_manifest_capabilities(self.data, 'videos')
        try:
            # 项目级锁 + 原子替换（write_manifest 内部处理 Windows 句柄占用重试）
            write_manifest(os.path.dirname(self.path), self.data)
        except Exception as e:
            print(f"Warning: could not write updated manifest.json ({e})")


def _adapt_prompt_after_anchor_rejection(prompt, reason):
    """根据首/尾锚点偏差生成一次性的重试约束，避免原样盲重试。"""
    text = str(prompt or '')
    if 'ANCHOR_RETRY_ADAPTATION:' in text:
        return text
    values = {}
    for key, raw in re.findall(r'\b(first|last)\s*=\s*([0-9.]+)', str(reason or ''), re.I):
        try:
            values[key.lower()] = float(raw)
        except ValueError:
            pass
    first = values.get('first', 999.0)
    last = values.get('last', 999.0)
    if first <= 18 and last > 18:
        guidance = (
            'Preserve the opening composition, then reserve the final 20% of duration for a smooth '
            'settle into the provided last-frame geometry. The final frame must match IMAGE 2 literally.'
        )
    elif last <= 18 and first > 18:
        guidance = (
            'Begin with a one-second locked hold matching IMAGE 1 literally before any action starts; '
            'do not redesign, crop, relight, or move objects at the opening.'
        )
    else:
        guidance = (
            'Treat both supplied images as literal camera and layout constraints. Do not redesign, '
            'reframe, crop, relight, or invent a third composition; distribute the transformation '
            'uniformly and settle exactly on IMAGE 2.'
        )
    return f"{text.rstrip()}\n\nANCHOR_RETRY_ADAPTATION: {guidance}"


class _BatchBridge:
    """把 google_fx_video 批量脚本的回调 (batch_idx, stage, details) 翻译成 SPARK
    进度事件并落盘 manifest。下载落盘后做锚点内容校验：视频首尾帧与该槽位锚点图
    不符（画布串片/错误下载）时删除文件并回传 'rejected'，批量脚本据此把该任务
    标记失败进入重试轮。"""

    def __init__(self, pending, total, video_model, writer, on_progress, strict=False,
                 process_check_fn=None, account_pool=None, pool_account_id=None,
                 allow_anchor_mismatch=False):
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
        # 只在调用方显式确认风险（override_flagged）时启用。默认路径仍会删除锚点
        # 不匹配的视频；覆盖路径保留文件并留下结构化告警，供人工补片/硬切槽收尾。
        self.allow_anchor_mismatch = allow_anchor_mismatch
        # 本次批次是否观测到"登录失效等待人工处理超时"——generate_video_sequence
        # 据此判断要不要自动切换号池账号重试剩余失败槽位。
        self.saw_login_required_timeout = False
        # 批量脚本在风控失败重试时自己换掉的号池账号（account_switched 事件）。
        # 批次跑完后并入 used_account_ids，免得补跑那一轮又挑回刚被判过的号。
        self.switched_accounts = []
        self.stats = {
            'submitted_requests': 0,
            'downloaded_results': 0,
            'accepted_results': 0,
            'anchor_rejections': 0,
            'process_rejections': 0,
            'adaptive_retries': 0,
        }

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
        elif stage == 'request_submitted':
            self.stats['submitted_requests'] += 1
            self._emit('request_submitted', {
                'index': plan['slot'], 'current': plan['seq'], 'total': self.total,
            })
        elif stage == 'video_done':
            self.stats['downloaded_results'] += 1
            generated_path = (details or {}).get('video_url')
            if not (generated_path and os.path.exists(generated_path)):
                self._fail(plan, '生成的视频文件不存在')
                return None
            shutil.move(generated_path, plan['dest_path'])
            ok, reason = verify_video_anchors(plan['dest_path'], plan['start_frame'], plan['end_frame'], strict=self.strict)
            if not ok:
                if not self.allow_anchor_mismatch:
                    self.stats['anchor_rejections'] += 1
                    req = self.pending[batch_idx].get('req')
                    if req is not None:
                        adapted = _adapt_prompt_after_anchor_rejection(
                            getattr(req, 'prompt', ''), reason)
                        if adapted != getattr(req, 'prompt', ''):
                            req.prompt = adapted
                            plan['prompt'] = adapted
                            self.stats['adaptive_retries'] += 1
                            self._emit('video_retry_adapted', {
                                'index': plan['slot'], 'current': plan['seq'],
                                'total': self.total,
                                'message': f"槽位 {plan['slot']} 已根据锚点偏差改写重试约束。",
                            })
                    try:
                        os.remove(plan['dest_path'])
                    except Exception:
                        pass
                    self._fail(plan, (
                        f"下载的视频内容与槽位 {plan['slot']} 的首尾锚点帧不符 (MAD {reason})，"
                        f"疑似画布串片/错误下载，已拦截并删除。请重试该片段。"
                    ))
                    return 'rejected'  # 通知批量脚本该片段实际失败，可参与失败重试
                self._emit('video_warning', {
                    'index': plan['slot'], 'current': plan['seq'], 'total': self.total,
                    'message': (
                        f"槽位 {plan['slot']} 锚点校验未通过 (MAD {reason})；"
                        "已按显式风险覆盖保留该片段。"
                    ),
                })
            process_reason = None
            if self.process_check_fn is not None:
                action, process_reason = self.process_check_fn(plan)
                if action == 'reject':
                    self.stats['process_rejections'] += 1
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
            if not ok:
                info['anchor_mismatch_overridden'] = True
            if process_reason is not None:
                # 段内过程检测留痕：PASS / WARN:... / Skipped(...) / skipped:...
                info['process_check'] = process_reason
                if action == 'warn':
                    # 结构化告警标记：终态质量风险汇总据此计数，不用猜留痕文本语义
                    info['process_warned'] = True
            self.writer.record(info)
            self.stats['accepted_results'] += 1
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
    frame_canvas_uuids = load_frame_canvas_uuids(manifest_data, slot_to_path)
    canvas_project_url = str(manifest_data.get('google_fx_project_url') or '').strip()

    plans = plan_video_slots(videos, slot_to_path, slot_to_quality, videos_dir, target_slots,
                              strict=strict_gates_enabled(config),
                              gate_level=qa_gate_level(config),
                              stale_slots=load_stale_slots(manifest_data),
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
    #
    # 2026-08-01：Omni 侧改成**总是显式传时长**，不再允许"沿用面板当前时长"这个不可知态。
    # omni 的时间线提示词把镜头切点钉在秒上（见 composers/omni.py），提示词按 N 秒排的
    # 切点表，生成时却用了面板上残留的另一个时长，切点表当场作废。两边同走
    # server_common.resolve_video_duration，保证是同一个数。
    if 'omni' in str(video_model).strip().lower():
        video_duration = str(resolve_video_duration(config))
    else:
        video_duration = None
    writer = _ManifestWriter(manifest_path, manifest_data, videos.keys())
    run_bridges = []

    # 按计划分流：复用/拦截立即出结果，待生成的装配为批量请求
    pending_items = []
    for plan in plans:
        if plan['action'] == 'skip_cut':
            # 旧单的硬切占位槽：记入 manifest（status='skipped_cut'）供合成门禁识别为
            # 预期缺失，不提交生成、不算失败（新单的 [CUT] 槽走上面的正常生成分支）
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
            image_uuid=frame_canvas_uuids.get(plan['start_frame'] or '', ''),
            end_image_uuid=frame_canvas_uuids.get(plan['end_frame'] or '', ''),
            model=video_model,
            ratio=config.get('imageAspectRatio') or '9:16',
            duration=video_duration,
            output_path=temp_out_dir
        )
        pending_items.append({'plan': plan, 'req': req, 'temp_out_dir': temp_out_dir})

    # 项目绑定的 Flow 画布由 _run_leg 统一挂到每个 req 上（google_fx_project_url）。
    # 这里只报一下有多少段任务的锚点帧能直接认领画布资产、免掉重复上传那一轮。
    known_canvas_frames = sum(
        1 for it in pending_items
        if it['req'].image_uuid or it['req'].end_image_uuid
    )
    if canvas_project_url and known_canvas_frames:
        print(f"[VIDEO] 帧序列就在绑定的 Flow 画布上，{known_canvas_frames} 段任务的锚点帧免重复上传")

    if pending_items:
        account_pool = _get_account_pool_service()
        pool_account_id = _select_pool_account(config, account_pool)
        if pool_account_id:
            apply_google_fx_runtime_overrides(config)

        def _process_gate(plan):
            return check_video_process(config, plan['dest_path'], plan['start_frame'],
                                       plan['end_frame'], plan['prompt'],
                                       meta=plan.get('meta', ''))

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
            actual_account_id = account_id or pool_account_id or config.get('googleFxUserId')
            if actual_account_id:
                config['googleFxUserId'] = actual_account_id
                apply_google_fx_runtime_overrides(config)
            leg_bridge = _BatchBridge(items, len(plans), video_model, writer, on_progress,
                                      strict=strict_gates_enabled(config),
                                      process_check_fn=_process_gate,
                                      account_pool=account_pool,
                                      pool_account_id=actual_account_id,
                                      allow_anchor_mismatch=override_flagged)
            run_bridges.append(leg_bridge)
            from integrations.google_fx.utils import account_binding
            flow_project_url = writer.data.get('google_fx_project_url')
            for item in items:
                item['req'].project_url = flow_project_url
            # fx_cancel_context 必须包住这次调用：cancel_check 只有 _ChunkRunner 自己的
            # _check_cancel() 在读，而 L2 helpers 里那 10 处 _check_cancelled() 走的是
            # per-request 的 CancelState contextvar。视频链此前从不建这个上下文，于是
            # 那些检查在视频链上全是空转——最典型的是 _connect_fx_page 的 CDP 重连循环
            # （3 轮 ×（45s 启动超时 + 10×2s 重试）），它只认 contextvar，传进去的
            # cancel_check 只往下透给 find_or_create_page，重试循环本身读不到。
            # 帧链一直是对的（frame_generator._fx_generate_batch），这里对齐它。
            with account_binding.bound_task_account(actual_account_id), \
                    fx_cancel_context(cancel_check_cb, deadline=fx_request_deadline()):
                try:
                    fx_results = google_fx_video.generate_videos_batch_google_fx(
                        [it['req'] for it in items],
                        on_progress=leg_bridge,
                        cancel_check=cancel_check_cb
                    )
                    returned_url = next(
                        (r.get('project_url') for r in (fx_results or [])
                         if isinstance(r, dict) and r.get('project_url')),
                        None,
                    )
                    if returned_url:
                        writer.data['google_fx_project_url'] = returned_url
                        writer.save()
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
                        image_uuid=old_req.image_uuid, end_image_uuid=old_req.end_image_uuid,
                        model=old_req.model, ratio=old_req.ratio, duration=old_req.duration,
                        output_path=new_temp_dir,
                    )
                    retry_items.append({'plan': it['plan'], 'req': new_req, 'temp_out_dir': new_temp_dir})

                _run_leg(retry_items, next_account_id)

    stat_keys = (
        'submitted_requests', 'downloaded_results', 'accepted_results',
        'anchor_rejections', 'process_rejections', 'adaptive_retries',
    )
    run_stats = {key: sum(b.stats.get(key, 0) for b in run_bridges) for key in stat_keys}
    run_stats['planned_slots'] = len(plans)
    run_stats['generated_slots'] = sum(1 for p in plans if p.get('action') == 'generate')
    run_stats['skipped_cut_slots'] = [p['slot'] for p in plans if p.get('action') == 'skip_cut']
    previous = writer.data.get('video_generation_stats') or {}
    cumulative = {
        key: int(previous.get('cumulative', {}).get(key, 0) or 0) + int(run_stats.get(key, 0) or 0)
        for key in stat_keys
    }
    writer.data['video_generation_stats'] = {'last_run': run_stats, 'cumulative': cumulative}
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
        return "存在" + "；".join(parts) + "，已拒绝合并。请重试这些片段，或选择「跳过缺口合并」。"


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


def _normalize_merge_speed(speed):
    """只接受 UI 暴露的合并速率，避免任意值进入 FFmpeg filter。"""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        value = 2.0
    if value not in (1.0, 1.5, 2.0):
        raise ValueError("视频合并速率仅支持 1、1.5 或 2 倍")
    return value


def _merge_speed_slug(speed):
    return '1_5x' if speed == 1.5 else f'{int(speed)}x'


def _merge_filter(speed, has_audio):
    pts_factor = 1.0 / speed
    video_filter = f'[0:v]setpts={pts_factor:.10g}*PTS[v]'
    if has_audio:
        return f'{video_filter};[0:a]atempo={speed:g}[a]'
    return video_filter


# ── 节奏时间分配（docs/pacing_rhythm_balance_plan.md 第 3 层） ─────────────────
# 每段固定 8 秒的 i2v 输出装的信息量差着几倍，观感上就是同一条片子里既有跟不上的
# 段落也有拖沓的段落。合成侧把每拍的拍重换算成一个 setpts 系数，写进提示词块的
# meta（"PACE 1.21"），一路经 plan -> manifest 流到这里，在合并时兑现成屏幕时间。
# 拿不到标签的片段（老项目、手动上传、运镜拍）一律 1.0，行为与改造前完全一致。
_PACE_META_RE = re.compile(r'\bPACE\s+([0-9]*\.?[0-9]+)\b', re.IGNORECASE)
# 单段相对基准时长的缩放上限，兜住上游给出畸形系数的情况（prompt_pipeline 侧已经
# 夹逼在 4~11 秒 / 8 秒 = 0.5~1.375，这里是第二道保险，不是主约束）。
_CLIP_SPEED_MIN, _CLIP_SPEED_MAX = 0.5, 2.0


def _clip_speed_from_meta(meta):
    """从槽位 meta 里取 PACE 系数；没有标签或值非法时返回 1.0（= 不改变时长）。"""
    match = _PACE_META_RE.search(str(meta or ''))
    if not match:
        return 1.0
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return 1.0
    if value <= 0:
        return 1.0
    return min(_CLIP_SPEED_MAX, max(_CLIP_SPEED_MIN, value))


def _atempo_chain(tempo):
    """atempo 单次只接受 0.5~2.0，超出范围必须拆成链式。

    全局倍速 2.0 叠上一个 0.59 的慢速段时 tempo 会到 3.4，直接写 atempo=3.4 会让
    整次合并失败——而失败信息埋在 ffmpeg 的 stderr 里，很难定位回节奏分配这一步。
    """
    parts = []
    remaining = float(tempo)
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    parts.append(remaining)
    return ','.join(f'atempo={p:.6g}' for p in parts)


# ── 段内时间重映射（2026-07-31 "视频推进跳变"复盘，第 4 层兜底）────────────────
# 上面的 PACE 系数解决的是**段与段之间**的时间分配（哪一拍该占更多屏幕时间）。
# 它对**段内部**的速度不匀无能为力：一段"推进→停滞→突进"的片子整体拉长或压缩之后，
# 停滞和突进只是等比例缩放，跳变照旧。
#
# 这里做的是段内重分配：把测得的变化量曲线当作"这段时间里发生了多少事"，让屏幕时间
# 跟着变化量走——停滞窗口压短、突进窗口拉长，**总时长严格不变**。总时长不变有三个
# 直接好处：① 音轨原样透传，不需要 atempo 切碎重拼；② 上游 PACE 的 setpts 系数照旧
# 生效，两套时间缩放不打架；③ 首尾帧原样保留，verify_video_anchors 的锚点校验不受影响。
#
# 这是**兜底**不是根治：它改的是观感，不会让模型真的把差量匀开。真正的根治在上游
# （帧对跨度 too_wide → 补帧拆拍）和提示词（匀速正向约束）。
# Disabled by design: the video stage must realize the two approved anchors, not reinterpret
# missing construction causality after generation.  Global pixel MAD also confuses Omni's planned
# hard cuts with progress jumps, so retiming those peaks can only distort the edit.  Keep the helper
# for diagnostics/experiments, but production merges use the original clips unchanged.
_RETIME_ENABLED = False
_RETIME_SEGMENTS = 16          # 与 _PACE_SAMPLES-1 对齐，直接复用同一份曲线
_RETIME_STRENGTH = 0.6         # 向"按变化量分配"矫正的强度；1.0 = 完全按变化量
_RETIME_MIN_FACTOR = 0.5       # 单段时长相对等分时长的下限
_RETIME_MAX_FACTOR = 2.0       # 上限
# 矫正幅度低于这个倍数就不动手：省掉一次整段转码，也避免给本来就匀速的片子引入
# 无谓的重编码损失。
_RETIME_TRIGGER = 1.25
_RETIME_BALANCE_ITERS = 8      # 夹逼↔归一的迭代轮数，见 compute_retime_factors


def compute_retime_factors(steps, strength=_RETIME_STRENGTH):
    """由速度曲线算出每段的时长缩放系数（相对等分时长）。

    返回长度与 steps 相同的列表，元素为 out_dur / (T/N)，且**加权后总和恒等于 N**
    （总时长不变）。曲线退化（全零/长度不足）时返回 None。

    阻尼（strength<1）是刻意的：完全按变化量分配会把一个近乎静止的窗口压到接近零帧，
    观感从"停顿"变成"卡帧"，反而更糟。0.6 表示只把六成的不均衡矫正掉。
    """
    try:
        import numpy as np
        steps = np.asarray(steps, dtype=float)
        n = len(steps)
        if n < 2:
            return None
        total = float(steps.sum())
        if total <= 0:
            return None
        uniform = 1.0 / n
        target = (1.0 - strength) * uniform + strength * (steps / total)
        factors = target / uniform
        # 首末两段恒为 1.0，绝不缩放。片尾静置段（工人退场后的干净交接帧）在曲线上就是
        # 一个低变化窗口，按变化量分配必然把它压到下限——而那一段正是与下一段拼接的
        # 接缝。实测压缩后成片尾帧与原锚点帧 MAD 从 ~2 升到 6.0：段内跳变治好了，
        # 却在每个拼接点制造出新的跳变。这一段的"低变化"是设计内的，不是缺陷。
        #
        # 2026-08-01 复盘：片头是同一条接缝的**另一端**（本段片头静置接的是上一段的
        # 片尾静置），此前却没钉死，于是被同一套逻辑压到底——实测 23 个被重映射的片段
        # 里 factors[0] 中位 0.54、9 个直接顶到 0.5 下限，等于把每个剪辑点的缓冲砍掉
        # 一半。与 _PACE_HEAD_EXEMPT 是同一件事的两面：那边不把片头静置判成缺陷，
        # 这边就不该按缺陷去压它。
        factors[0] = 1.0
        factors[-1] = 1.0

        # 夹逼与钉桩都会破坏总和，而"先夹逼再一次性归一"会把没触底的段整体抬上去，
        # 抬过头就成了过度拉伸——实测一条本来判通过的片子被拉出了新的停滞窗口。
        # 改成夹逼↔归一交替迭代：每轮只把违约量摊到仍有余量的段上，几轮即收敛到
        # 「既落在 [min,max] 内、总和又等于 n」的解。两端已钉死，可调的只有中间段，
        # 它们的目标和相应地是 n-2（两端各占 1.0），总时长仍然严格不变。
        for _ in range(_RETIME_BALANCE_ITERS):
            factors = np.clip(factors, _RETIME_MIN_FACTOR, _RETIME_MAX_FACTOR)
            factors[0] = factors[-1] = 1.0
            mid = factors[1:-1]
            if not len(mid):
                break
            mid_sum = float(mid.sum())
            if mid_sum <= 0:
                return None
            deficit = (n - 2.0) - mid_sum
            if abs(deficit) < 1e-6:
                break
            # 只调整还没顶到相应边界的段，顶死的段再摊也摊不动
            room = ((mid < _RETIME_MAX_FACTOR) if deficit > 0
                    else (mid > _RETIME_MIN_FACTOR))
            if not room.any():
                break
            mid[room] += deficit / float(room.sum())
            factors[1:-1] = mid
        factors = np.clip(factors, _RETIME_MIN_FACTOR, _RETIME_MAX_FACTOR)
        factors[0] = 1.0
        factors[-1] = 1.0
        return [float(x) for x in factors]
    except Exception:
        return None


def _retime_filter(factors, duration, fps=None):
    """把时长系数编成 split+trim+setpts+concat 的 filter_complex（仅视频流）。

    末尾的 fps 滤镜不是可选项（2026-08-01 复盘）：setpts 只改时间戳，既不生成也不
    丢弃帧，于是压缩段（factor<1）的原帧挤进更短的时间、拉伸段（factor>1）的原帧被
    拉长驻留，concat 出来是一条**变帧率**的流。再交给 libx264 时输出帧率由 ffmpeg
    自己猜，实测 24fps 的 8 秒片段猜成 19.1fps（avg_frame_rate=153/8）——压缩段的真实
    帧被直接丢弃：192 帧掉到 153 帧（少了 20% 的画面内容），帧间隔从单一的 41.7ms
    劣化成 41.7/83.3ms 双峰，完全重复帧占比从 10.5% 升到 27.1%。也就是说这一层本来
    要修的「推进跳变」，恰恰被它自己制造了出来。锁回源帧率后帧数与 CFR 都回到原样。

    注意：拉伸段仍然只能靠复帧（没有光流插值），所以重复帧占比只降到 23% 左右、回不到
    源片水平。这是重映射的固有代价，也是它只该对判定为跳变的片段动手的原因之一。
    """
    n = len(factors)
    seg = duration / n
    outs = ''.join(f'[s{i}]' for i in range(n))
    parts = [f'[0:v]split={n}{outs}']
    for i, factor in enumerate(factors):
        start, end = i * seg, (i + 1) * seg
        # 末段的 end 不写死：浮点误差下写死会切掉最后一帧，而最后一帧就是锚点尾帧。
        trim = (f'trim=start={start:.6f}' if i == n - 1
                else f'trim=start={start:.6f}:end={end:.6f}')
        parts.append(f'[s{i}]{trim},setpts=(PTS-STARTPTS)*{factor:.6f}[t{i}]')
    concat = ''.join(f'[t{i}]' for i in range(n)) + f'concat=n={n}:v=1:a=0'
    # fps 探测失败时退回不加滤镜：宁可保留旧行为，也不要用猜出来的帧率去重采样。
    parts.append(f'{concat},fps={fps:g}[vout]' if fps else f'{concat}[vout]')
    return ';'.join(parts)


def retime_clip_even(src, dst, tmp_dir=None):
    """把片段内部的时间重新分配成"变化量随时间均匀"，总时长不变。

    返回 (ok: bool, reason: str)。ok=False 表示未改写（不需要 / 测不了 / 转码失败），
    调用方一律原样使用 src —— 这是观感优化，任何一步出问题都不该让合并失败。
    """
    if not _RETIME_ENABLED:
        return False, 'disabled'
    own_tmp = None
    try:
        if tmp_dir is None:
            own_tmp = tempfile.TemporaryDirectory()
            tmp_dir = own_tmp.name
        steps, duration, error = clip_step_profile(src, tmp_dir, samples=_RETIME_SEGMENTS + 1)
        if error:
            return False, error
        # 只对判定为跳变的片段动手。无差别重映射实测会把个别本来匀速的片子拉出新的
        # 停滞窗口（夹逼+归一把上界推过头），修 5 伤 1 不划算；本来就匀的片子不碰，
        # 顺带也省掉一次整段转码。
        broken, verdict_reason = pace_verdict(steps, duration)
        if not broken:
            return False, f'skipped:pace_ok（{verdict_reason}）'
        factors = compute_retime_factors(steps)
        if not factors:
            return False, 'skipped:degenerate_profile'
        spread = max(factors) / min(factors)
        if spread < _RETIME_TRIGGER:
            return False, f'skipped:already_even(spread={spread:.2f})'

        has_audio = _clip_has_audio(src)
        # 源帧率要锁回输出，否则 setpts 产出的变帧率流会在编码时被丢帧（见 _retime_filter）。
        src_fps = (_ffprobe_video_params(src) or {}).get('fps') or 0
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-filter_complex", _retime_filter(factors, duration, src_fps), "-map", "[vout]"]
        if has_audio:
            # 总时长不变 → 音轨原样拷贝即可，不需要变速。段内 A/V 会有亚秒级偏移，
            # 而 i2v 的音轨是环境底噪，没有需要对齐的音画事件。
            cmd.extend(["-map", "0:a", "-c:a", "copy"])
        # -t 钉死总时长：分段 setpts 会在每个切点上按帧取整，误差累加到片尾能多出
        # 2~3 帧（实测 8.0 → 8.08）。单段无所谓，但 12 段拼起来音轨（原样拷贝、仍是
        # 8.0）就会累积出接近一秒的音画偏移。多出来的那几帧落在片尾静置区，是重复帧，
        # 截掉不损失任何内容。
        cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-t", f"{duration:.6f}", dst])
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace', timeout=300)
        if res.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return False, f'ffmpeg_failed:{(res.stderr or "").strip()[:200]}'
        return True, (f'retimed(spread={spread:.2f}, '
                      f'factors={[round(f, 2) for f in factors]})')
    except Exception as e:
        return False, f'skipped:{type(e).__name__}'
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


def _clip_has_audio(path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', timeout=60)
        return 'audio' in (res.stdout or '').lower()
    except Exception:
        return False


def slot_meta_map(manifest_data):
    """槽位 -> meta 文案。

    is_hero 本来是 'HERO' in meta 的派生字段，但 server.py 在重生成时会从上一版
    manifest 继承它（见 4507 行附近），于是存在"只剩 is_hero、meta 里没有 HERO"的
    条目。这里反向把标签补回去，保证 _is_camera_move_slot 认得出来。"""
    out = {}
    for v in (manifest_data or {}).get('videos') or []:
        if not isinstance(v, dict):
            continue
        meta = str(v.get('meta') or '')
        if v.get('is_hero') and 'HERO' not in meta.upper():
            meta = f'{meta} [HERO]'.strip()
        out[v.get('slot')] = meta
    return out


def retime_clips_for_merge(video_files, tmp_dir, metas=None):
    """合并前逐段做内部时间重映射。返回 (paths, notes)。

    运镜拍（[HERO]/[BRIDGE]/[CUT]）整段跳过，与 check_video_process 的节奏判据共用
    同一条豁免（2026-08-01 补齐）：这些拍的速度曲线由镜头调度决定，推进/缓入缓出都是
    设计内的，用施工推进的匀速标准去量必然误报。此前检测门豁免了、重映射层却拿不到
    meta（只收到一串路径），于是照着误报把设计好的运镜节奏重新摊匀——一道门放行、
    另一道门动手改，正是两层判据必须同口径的反例。

    metas 缺省（历史调用方/无 manifest）时一律按普通拍处理，与改造前行为一致。
    任何一段失败都退回该段原文件——重映射是观感优化，不是正确性前提。"""
    paths, notes = [], []
    for idx, src in enumerate(video_files):
        meta = metas[idx] if metas and idx < len(metas) else ''
        if _is_camera_move_slot(meta):
            paths.append(src)
            notes.append(f'{os.path.basename(src)}: '
                         'skipped:camera_move_slot（运镜拍不适用施工推进的匀速判据）')
            continue
        dst = os.path.join(tmp_dir, f'retimed_{idx:03d}.mp4')
        ok, reason = retime_clip_even(src, dst, tmp_dir=tmp_dir)
        paths.append(dst if ok else src)
        notes.append(f'{os.path.basename(src)}: {reason}')
    return paths, notes


def _paced_merge_filter(clip_speeds, speed, has_audio):
    """多输入 concat filter：每段用自己的 setpts，再整体套上全局倍速。

    走多输入而不是 concat demuxer 的 outpoint：outpoint 只能截短不能拉长，而截短会
    砍掉片尾工人退场的收尾（Out-and-In 契约的观感前提）。变速也比截断更符合
    「时间流速」的语义。

    全局倍速与每段系数是**相乘**关系：setpts 用 clip/speed，atempo 用 speed/clip。
    并列处理会让两套时间缩放互相打架。
    """
    video_parts, audio_parts, concat_inputs = [], [], []
    for i, clip in enumerate(clip_speeds):
        pts = float(clip) / speed
        video_parts.append(f'[{i}:v]setpts={pts:.6g}*PTS[v{i}]')
        concat_inputs.append(f'[v{i}]')
        if has_audio:
            audio_parts.append(f'[{i}:a]{_atempo_chain(speed / float(clip))}[a{i}]')
            concat_inputs.append(f'[a{i}]')
    n = len(clip_speeds)
    if has_audio:
        concat = f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[v][a]"
        return ';'.join(video_parts + audio_parts + [concat])
    concat = f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[v]"
    return ';'.join(video_parts + [concat])


def merge_project_videos(project_dir, allow_partial=False, speed=2.0):
    """合并项目内全部视频片段。

    2026-07-04 复盘：之前失败/缺失的槽位会被静默跳过（loft 任务缺 6、7 两段仍合出了
    成片，观感为画面硬跳/回到初始状态），且串片的片段会原样混入。现在默认执行两道门禁：
    1) 槽位完整性 —— 依据 manifest.frames 推出应有片段数，缺失/失败即拒绝合并；
    2) 锚点一致性 —— 每段首尾帧须与对应锚点图匹配，不匹配即拒绝合并。
    allow_partial=True 时跳过两道门禁（用户显式确认后强制合并）。
    """
    speed = _normalize_merge_speed(speed)
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
        # 旧单的硬切占位槽位（status='skipped_cut'）是"预期缺失"：不算缺口、不占位填充，
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
            # 手动上传和显式风险覆盖在落盘前都已经得到用户确认。这里再复查会让 force
            # 形同虚设（下载阶段保留，合并阶段却再次拦下），因此直接信任并保留审计字段。
            if v.get('source') == 'manual_upload' or v.get('anchor_mismatch_overridden'):
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
            project_dir, manifest_data, expected_slots, good, missing, mismatched, speed=speed,
        )

    # 无缺口/无串片：走原有干净合并路径
    merge_slots = sorted(good)
    video_files = [good[s] for s in merge_slots]
    if not video_files:
        return None

    # 段内时间重映射：把每段内部"停滞→突进"的速度曲线摊匀（总时长不变，故不影响
    # 下面的 PACE 系数与音轨处理）。逐段 fail-open，失败的段原样进入合并。临时目录
    # 必须活到 ffmpeg 跑完，所以显式管理而不是用 with。
    retime_tmp = tempfile.TemporaryDirectory()
    try:
        meta_by_slot = slot_meta_map(manifest_data)
        video_files, retime_notes = retime_clips_for_merge(
            video_files, retime_tmp.name,
            metas=[meta_by_slot.get(s, '') for s in merge_slots])
        print(f"[INFO] 段内节奏重映射: {'; '.join(retime_notes)}")
    except Exception as retime_err:
        print(f"[WARN] 段内节奏重映射整体跳过（{retime_err}），按原片合并")

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
        
    output_filename = f"{chinese_name}_{_merge_speed_slug(speed)}.mp4"
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

    def _build_concat_demuxer_cmd():
        """改造前的原路径：concat demuxer + 一个全局 setpts。所有片段等长通过。"""
        base = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path]
        if has_audio:
            base.extend(["-filter_complex", _merge_filter(speed, True),
                         "-map", "[v]", "-map", "[a]", "-c:a", "aac"])
        else:
            base.extend(["-filter_complex", _merge_filter(speed, False), "-map", "[v]"])
        base.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path])
        return base

    # 节奏时间分配：只要有任何一段带了 PACE 标签就走多输入路径；一段都没有时
    # （老项目、手动上传、prompt_pipeline._RHYTHM_CLIP_TIMING=False）**原样走旧路径**，
    # 行为与改造前逐字节一致。
    def _slot_clip_speed(slot):
        entry = by_slot.get(slot) or {}
        declared = entry.get('clip_speed')
        if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0:
            return min(_CLIP_SPEED_MAX, max(_CLIP_SPEED_MIN, float(declared)))
        # 旧 manifest 没有 clip_speed 字段，但 meta 里可能已经有 PACE 标签
        return _clip_speed_from_meta(entry.get('meta'))

    clip_speeds = [_slot_clip_speed(s) for s in sorted(good)]
    paced = any(abs(k - 1.0) >= 0.01 for k in clip_speeds)

    if paced:
        cmd = ["ffmpeg", "-y"]
        for vf in video_files:
            cmd.extend(["-i", vf])
        cmd.extend(["-filter_complex", _paced_merge_filter(clip_speeds, speed, has_audio),
                    "-map", "[v]"])
        if has_audio:
            cmd.extend(["-map", "[a]", "-c:a", "aac"])
        cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path])
        print(f"[INFO] Merging {len(video_files)} videos to {output_path} "
              f"(speed={speed:g}x, has_audio={has_audio}, pace={[round(k, 2) for k in clip_speeds]})...")
    else:
        cmd = _build_concat_demuxer_cmd()
        print(f"[INFO] Merging {len(video_files)} videos to {output_path} (speed={speed:g}x, has_audio={has_audio})...")

    # encoding must be explicit: ffmpeg emits UTF-8, but Windows text-mode default is GBK,
    # which crashes the subprocess stderr reader thread mid-merge
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='replace')

    if paced and res.returncode != 0:
        # 多输入 concat filter 要求所有输入的流布局一致；has_audio 只探了第一段，
        # 若后面某段没有音轨，整次合并会失败。这种情况下退回等长的旧路径——
        # 宁可丢掉节奏分配，也不能让用户拿不到成片。
        print(f"[WARN] 按拍重变速的合并失败，回退等长拼接: {res.stderr[-400:]}")
        res = subprocess.run(_build_concat_demuxer_cmd(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             encoding='utf-8', errors='replace')

    try:
        os.remove(concat_list_path)
    except:
        pass
    try:
        retime_tmp.cleanup()
    except Exception:
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
            'speed': speed,
            'status': 'success'
        }
    else:
        print(f"[ERROR] ffmpeg merge failed with code {res.returncode}: {res.stderr}")
        raise RuntimeError(f"FFmpeg merge failed: {res.stderr}")


def _merge_skip_missing(project_dir, manifest_data, expected_slots, good, missing, mismatched, speed=2.0):
    """强制合并（allow_partial）：2026-07-22 改版——不再用起始锚点帧定格+「缺失」标注
    填充缺口（占位预览这套 filter_complex/drawtext 太重，且冻结帧撑时长的观感也不好），
    直接跳过缺失/串片的槽位，把仍然可用的片段按原顺序和所选速率拼接，跳过处是硬切。
    不是背地里丢弃缺口——门禁提示里用户已经看到具体缺了哪些槽位，这里把 skipped_slots
    带回给调用方展示，只是不再用假帧撑时长。"""
    speed = _normalize_merge_speed(speed)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    merge_slots = [s for s in expected_slots if s in good]
    video_files = [good[s] for s in merge_slots]
    if not video_files:
        return None
    skipped_slots = sorted(set(missing) | set(mismatched))

    # 段内时间重映射：与干净合并路径同款处理（见 merge_project_videos）。缺口合并
    # 本来就是降级产物，但每一段自己的推进节奏该匀还是要匀。
    retime_tmp = tempfile.TemporaryDirectory()
    try:
        meta_by_slot = slot_meta_map(manifest_data)
        video_files, retime_notes = retime_clips_for_merge(
            video_files, retime_tmp.name,
            metas=[meta_by_slot.get(s, '') for s in merge_slots])
        print(f"[INFO] 段内节奏重映射: {'; '.join(retime_notes)}")
    except Exception as retime_err:
        print(f"[WARN] 段内节奏重映射整体跳过（{retime_err}），按原片合并")

    concat_list_path = os.path.join(project_dir, 'concat_list.txt')
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for vf in video_files:
            f.write(f"file '{vf.replace(chr(92), '/')}'\n")

    title = manifest_data.get('title', '')
    chinese_name = _project_display_name(title)
    # 绝对路径：见 2026-07-22 的踩坑记录——project_dir 本身是相对路径（server_common.
    # OUTPUT_ROOT='outputs'），若 output_path 沿用相对拼接，一旦后续改动又把 ffmpeg 子
    # 进程的 cwd 切到 project_dir，就会被当成"project_dir 下再嵌一层 project_dir"解析。
    output_path = os.path.abspath(os.path.join(
        project_dir, f"{chinese_name}_partial_{_merge_speed_slug(speed)}.mp4"))

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
        cmd += ["-filter_complex", _merge_filter(speed, True),
                "-map", "[v]", "-map", "[a]", "-c:a", "aac"]
    else:
        cmd += ["-filter_complex", _merge_filter(speed, False), "-map", "[v]"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]

    print(f"[INFO] Skip-merge: {len(video_files)} segments, skipped {skipped_slots}, speed={speed:g}x -> {output_path}")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding='utf-8', errors='replace')
    try:
        os.remove(concat_list_path)
    except Exception:
        pass
    try:
        retime_tmp.cleanup()
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
        'speed': speed,
        'status': 'success',
        'partial': True,
        'skipped_slots': skipped_slots,
    }
