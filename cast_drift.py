"""剪影级人物漂移探针 —— 对着成片判「这一段还是不是同一个人」。

这是人物一致性三层里的第三层（见 docs/character_consistency_plan.md）：

  L1 词表层  每段逐字重述同一组锚点词  → cast_lock（提示词写对了没有）
  L2 像素层  参考图 / seed 钉住脸       → 未实现
  L3 验收层  对着真实画面比对           → 本模块

L1 是**必要不充分**的：它保证提示词写对了，不保证模型照做了。本模块是提示词到成片
之间唯一的闭环。

## 它测什么，不测什么

测的是**每个 T1 部位的登记颜色在画面里占多大面积**，在 CIE LAB 空间按 ΔE76 容差统计，
然后跨段比较。协议第 8 节列的失败模式里最贵的那几条（段间换装、手套时有时无、
胡须变络腮）全是这一级的 —— 它们在像素上就是「那块颜色整片消失了」。

**刻意不做人物检测。** 只有 Pillow + numpy 可用（这是 requirements.txt 的既有约束），
跑不了人体分割。但这里也不需要：判据是**同一个项目内跨段的相对变化**，不是绝对定位。
外套在第 1-6 段稳定占 4% 画面、第 7 段掉到 0.1%，那就是换装了 —— 无论那 4% 具体
落在画面哪里。

**它抓不到的**：换成了相近色（橄榄绿→军绿，ΔE 落在容差内）；人物太小以致任何服装
颜色都不足以形成可测面积；场景本身大面积存在同色物体（沙漠里的土黄裤子）。
第三种会造成**假阴性**（漂了也看不出来，因为背景一直在贡献覆盖率），这是本探针最大
的盲区，报告里对每个部位都给出基线覆盖率，就是让人能自己判断这个部位可不可信。

## 为什么"看不见"不等于"漂了"

一个部位在所有段里覆盖率都近似为零，最可能的原因是参考色没标对、或者这个部位在这
条片子里根本没入过镜 —— 不是「每一段都换了装」。这种情况一律报 `inconclusive`，
绝不报 drift。把探针测不了的东西报成缺陷，是让人从此忽略整个报告的最快方式。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any


CONTRACT_VERSION = "cast-drift-v1"

# ΔE76。12 是"同一件衣服在不同光照/角度下"与"换了一件衣服"之间的经验分界，按
# cast-registry.json 的 banned 表实测校准（见 tests/test_cast_drift.py::TestTolerance）：
#
#   faded olive-green ↔ khaki        29.5   要分开
#   dark-brown boots  ↔ tan boots    51.6   要分开
#   charcoal-grey     ↔ black        21.0   要分开
#   faded olive-green ↔ army green   14.5   要分开 ← 余量最窄的一对
#   同色 ±12% 亮度                     6.2   不能分开
#
# 余量最窄的是橄榄绿↔军绿（14.5 对容差 12）。这意味着强烈的日照/阴影切换有可能把这
# 一对糊在一起，漏报一次换装。把容差再调低会开始把光照变化报成漂移 —— 那种假阳性
# 比这个漏报更贵，因为它会让人从此忽略整份报告。
DEFAULT_DELTA_E = 12.0

# 低于这个覆盖率就认为"探针在这条片子里看不见这个部位"。0.2% 画面 ≈ 竖幅 1080x1920
# 里约 4100 像素，大致是中景人物一只手套的量级。
MIN_PRESENCE = 0.002

# 上限。任何一段超过它，就说明这个参考色**不只**命中服装 —— 它同时命中了场景里的大块
# 东西，该部位的任何覆盖率数字都不可解释。
#
# 这不是假想的边界情况：拿真实成片实测时，白安全帽的参考色 #b3bcc2 同时命中天空，
# 外景段覆盖率 13~20%、近景段 1~2%，于是"基线 1.78%、某段塌到 33%"被报成换帽子，
# 而真正变的只是构图里的天空占比。人物服装在竖幅中景里最多到 8~10%，15% 以上一定
# 掺了场景。
#
# 宁可把一个部位判成不可用，也不要输出一条自信的假 FAIL —— 假阳性会让人从此忽略
# 整份报告，那比少报一个部位贵得多。
MAX_PRESENCE = 0.15

# 某段覆盖率跌到全片基线的这个比例以下 = 这个部位在这一段消失了。0.35 留出了
# 「同一件外套背对镜头、只露出小半」的余量。
COLLAPSE_RATIO = 0.35

# 每段抽几帧。均匀采样，不取首尾（进离场半秒里人物可能只有一部分在画面内）。
FRAMES_PER_SEGMENT = 8

_MAX_SIDE = 480


def _numpy():
    """numpy 缺失时明确报错而不是静默降级。

    requirements.txt 里 numpy 那段注释记了这个教训：本地视觉探针缺 numpy 时静默返回
    'skipped'，后果是整套内容级校验悄悄失效而日志上看不出异常。这里不重蹈覆辙 ——
    调用方拿到的是一个说明白了的异常，不是一份看起来干净的空报告。
    """
    try:
        import numpy as np
        return np
    except ImportError as e:
        raise RuntimeError(
            "cast_drift 需要 numpy（requirements.txt 已声明）。缺它时本探针不降级运行："
            "一份空的漂移报告和一份干净的漂移报告在日志里长得一模一样。") from e


# ── 颜色空间 ────────────────────────────────────────────────────────────────

def srgb_to_lab(arr):
    """sRGB uint8 数组 → CIE LAB（D65）。arr 形状 (..., 3)，返回同形状 float。

    自己写而不是拉 skimage/opencv：这两个都不在 requirements.txt 里，而这段是
    教科书公式，二十行。frame_generator.py 里那份 LAB 用的是 OpenCV 的 8-bit 编码
    （L/a/b 全在 0..255、128 为中性），与这里的 CIE 口径**不通用**，别互相套用。
    """
    np = _numpy()
    rgb = arr.astype(np.float64) / 255.0
    # sRGB 逆伽马
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    r, g, b = linear[..., 0], linear[..., 1], linear[..., 2]
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    # D65 白点
    x, y, z = x / 0.95047, y / 1.00000, z / 1.08883
    eps, kappa = 216.0 / 24389.0, 24389.0 / 27.0
    fx, fy, fz = (np.where(t > eps, np.cbrt(t), (kappa * t + 16.0) / 116.0) for t in (x, y, z))
    return np.stack([116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1)


def hex_to_rgb(value):
    """'#4a5233' / '4a5233' → (74, 82, 51)。"""
    s = str(value or "").strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"参考色必须是 6 位十六进制 sRGB，收到 {value!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(c)) for c in rgb[:3])


# ── 单帧测量 ────────────────────────────────────────────────────────────────

def load_frame(path, max_side=_MAX_SIDE):
    """读一帧并缩到 max_side。缩图对覆盖率统计是无损的 —— 测的是面积**比例**。"""
    from PIL import Image
    np = _numpy()
    with Image.open(path) as im:
        im = im.convert("RGB")
        scale = min(1.0, max_side / float(max(im.size)))
        if scale < 1.0:
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                           Image.LANCZOS)
        return np.asarray(im, dtype="uint8")


def slot_coverage(frame_rgb, reference_rgb, delta_e=DEFAULT_DELTA_E):
    """这一帧里有多大比例的像素落在参考色的 ΔE 球内。"""
    np = _numpy()
    lab = srgb_to_lab(frame_rgb)
    ref = srgb_to_lab(np.array(reference_rgb, dtype="uint8").reshape(1, 1, 3))[0, 0]
    dist = np.sqrt(((lab - ref) ** 2).sum(axis=-1))
    return float((dist <= delta_e).mean())


def calibrated_slots(cast):
    """已登记参考色的 T1 部位：[(slot, required_phrase, (r,g,b)), ...]。"""
    out = []
    for anchor in (cast or {}).get("tier1_anchors", []):
        ref = anchor.get("reference_srgb")
        if not ref:
            continue
        try:
            out.append((anchor["slot"], anchor.get("required", ""), hex_to_rgb(ref)))
        except ValueError as e:
            print(f"[WARN] cast_drift: 部位 {anchor.get('slot')!r} 的 reference_srgb 无效：{e}")
    return out


# ── 抽帧 ────────────────────────────────────────────────────────────────────

def _ffmpeg_binary():
    try:
        import server_common
        if hasattr(server_common, "resolve_binary"):
            return server_common.resolve_binary("ffmpeg")
    except Exception:
        pass
    return "ffmpeg"


def extract_frames(video_path, count=FRAMES_PER_SEGMENT, out_dir=None):
    """从一段视频里均匀抽 count 帧，返回帧文件路径列表。

    ffmpeg 是本项目的既有硬依赖（replica_pipeline 的抽帧链路就靠它，缺它时那条链路
    直接判失败），不是本模块新引入的。

    刻意跳过首尾各 10%：具名人物模式下进离场压缩在首尾各半秒内，那半秒里人物往往
    只有一部分在画面内，拿它做覆盖率基线会把正常的进场判成"外套不见了"。
    """
    owned = out_dir is None
    out_dir = out_dir or tempfile.mkdtemp(prefix="cast_drift_")
    pattern = os.path.join(out_dir, "f_%03d.png")
    cmd = [_ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y",
           "-i", video_path,
           # thumbnail=N 在每 N 帧里挑最有代表性的一张，天然避开转场糊帧；
           # 配 -frames:v 限制总数。比按时间点 seek 快得多，也不需要先探时长。
           "-vf", f"thumbnail={max(2, 90 // max(1, count))},scale={_MAX_SIDE}:-1",
           "-frames:v", str(count), "-vsync", "vfr", pattern]
    try:
        import server_common
        win_flags = server_common.get_subprocess_window_flags() if hasattr(server_common, "get_subprocess_window_flags") else {}
    except Exception:
        win_flags = {}
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, **win_flags)
    if proc.returncode != 0:
        if owned:
            _cleanup(out_dir)
        raise RuntimeError(f"ffmpeg 抽帧失败（exit {proc.returncode}）：{(proc.stderr or '')[-500:]}")
    frames = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".png"))
    if not frames:
        if owned:
            _cleanup(out_dir)
        raise RuntimeError(f"ffmpeg 没有从 {video_path} 抽出任何帧")
    return frames


def _cleanup(directory):
    try:
        for f in os.listdir(directory):
            os.unlink(os.path.join(directory, f))
        os.rmdir(directory)
    except OSError:
        pass


# ── 逐段与全片 ──────────────────────────────────────────────────────────────

def analyze_segment(frame_paths, slots, delta_e=DEFAULT_DELTA_E):
    """一段的每个部位覆盖率（取各帧中位数）。

    中位数而不是均值：一段里总有一两帧是人物背对镜头或被机具挡住的，均值会被那几帧
    拖低，中位数不会。
    """
    np = _numpy()
    per_slot = {slot: [] for slot, _, _ in slots}
    for path in frame_paths:
        frame = load_frame(path)
        for slot, _phrase, rgb in slots:
            per_slot[slot].append(slot_coverage(frame, rgb, delta_e))
    return {slot: float(np.median(vals)) if vals else 0.0 for slot, vals in per_slot.items()}


def analyze_project(segments, cast, delta_e=DEFAULT_DELTA_E,
                    min_presence=MIN_PRESENCE, max_presence=MAX_PRESENCE,
                    collapse_ratio=COLLAPSE_RATIO):
    """跨段比较。`segments` 是 [(label, [frame_path, ...]), ...]。

    每个部位先判可用性，只有可用的部位才参与结论：
      不可用·太少   全片基线低于 min_presence —— 参考色不对 / 没入过镜 / 人物太小
      不可用·掺场景 任一段超过 max_presence —— 该色同时命中天空/地面等大块场景
      可用          在这两者之间

    可用部位里，某段覆盖率塌到全片基线的 collapse_ratio 以下 = 这一段这个部位换了。
    """
    np = _numpy()
    slots = calibrated_slots(cast)
    if not slots:
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "uncalibrated",
            "message": ("该角色的 T1 部位没有任何 reference_srgb，剪影级探针无法运行。"
                        "先用 --calibrate 从一张已认可的帧上取色。"),
            "segments": [], "slots": [],
        }

    measured = [(label, analyze_segment(paths, slots, delta_e)) for label, paths in segments]

    slot_reports, findings = [], []
    for slot, phrase, rgb in slots:
        series = [cov[slot] for _label, cov in measured]
        baseline = float(np.median(series)) if series else 0.0
        peak = max(series) if series else 0.0
        if baseline < min_presence:
            usable, note = False, (
                f"全片基线覆盖率 {baseline:.4%} 低于 {min_presence:.2%}，本部位判不了。"
                f"多半是参考色标错了、或这个部位在这条片子里没入过镜 —— 不是每段都换了装。")
        elif peak > max_presence:
            usable, note = False, (
                f"有段覆盖率达 {peak:.2%}，超过 {max_presence:.0%} —— 这个参考色同时命中了"
                f"场景里的大块东西（天空、地面、墙面），该部位的覆盖率不可解释。换一个更"
                f"专属于该服装的取色点，或接受这个部位在这条片子里判不了。")
        else:
            usable, note = True, None
        slot_reports.append({
            "slot": slot, "required": phrase, "reference_srgb": rgb_to_hex(rgb),
            "baseline_coverage": round(baseline, 5),
            "peak_coverage": round(peak, 5),
            "usable": usable, "note": note,
        })
        if not usable:
            continue
        for (label, cov) in measured:
            if cov[slot] < baseline * collapse_ratio:
                findings.append({
                    "segment": label, "slot": slot, "required": phrase,
                    "coverage": round(cov[slot], 5), "baseline": round(baseline, 5),
                    "verdict": "drifted",
                    "detail": (f"{slot}：覆盖率 {cov[slot]:.3%}，全片基线 {baseline:.3%}，"
                               f"塌到 {cov[slot] / baseline:.0%}。登记写法是 {phrase!r}。"),
                })

    # 一个可判部位都没有 ≠ 通过。这时探针什么都没说，报 ok 就是在用一句"未见漂移"
    # 冒充一次真的检查 —— 和它要取代的人眼放水是同一种错误，只是换了个更权威的口气。
    usable_count = sum(1 for s in slot_reports if s["usable"])
    if not usable_count:
        status = "inconclusive"
    elif findings:
        status = "drift_detected"
    else:
        status = "ok"

    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "usable_slots": usable_count,
        "delta_e": delta_e,
        "slots": slot_reports,
        "segments": [{"label": label, "coverage": {k: round(v, 5) for k, v in cov.items()}}
                     for label, cov in measured],
        "findings": findings,
    }


# ── 标定 ────────────────────────────────────────────────────────────────────

def sample_reference(frame_path, x_ratio, y_ratio, patch=5):
    """从一张已认可的帧上按相对坐标取一小块的中位色，返回 hex。

    取中位数而不是单像素：单像素会取到压缩噪点或高光，标出来的参考色偏一大截。
    坐标用相对值（0..1），这样同一组坐标能用在任何分辨率的帧上。
    """
    np = _numpy()
    frame = load_frame(frame_path, max_side=10000)  # 标定不缩图，要真实像素
    h, w = frame.shape[:2]
    cx, cy = int(round(x_ratio * (w - 1))), int(round(y_ratio * (h - 1)))
    x0, x1 = max(0, cx - patch), min(w, cx + patch + 1)
    y0, y1 = max(0, cy - patch), min(h, cy + patch + 1)
    block = frame[y0:y1, x0:x1].reshape(-1, 3)
    return rgb_to_hex(np.median(block, axis=0))


def write_reference(registry_path, cast_id, slot, hex_value):
    """把标定出来的参考色写回 cast-registry.json 的对应 T1 部位。

    直接写回注册表而不是另存一份：身份块正文的唯一副本原则同样适用于参考色 ——
    两处各写一份就是下一次漂移的源头。
    """
    hex_to_rgb(hex_value)  # 提前校验格式
    with open(registry_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for entry in data.get("cast", []):
        if entry.get("id") != cast_id:
            continue
        for anchor in entry.get("tier1_anchors", []):
            if anchor.get("slot") == slot:
                anchor["reference_srgb"] = hex_value
                with open(registry_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                return True
        raise KeyError(f"角色 {cast_id!r} 没有 T1 部位 {slot!r}")
    raise KeyError(f"注册表里没有角色 {cast_id!r}")


# ── 项目输入 ────────────────────────────────────────────────────────────────

def segments_from_project(project_dir, count=FRAMES_PER_SEGMENT, work_dir=None):
    """从 outputs/<项目>/manifest.json 读出视频段，逐段抽帧。

    模式 A 下人物只活在 VIDEO 段里 —— IMAGE 锚点按 Clean Frame Boundary 契约无人，
    对它们跑人物漂移探针只会得到一堆 inconclusive。所以这里只读 videos。
    """
    manifest_path = os.path.join(project_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    repo_root = os.path.dirname(os.path.abspath(__file__))
    out = []
    for item in manifest.get("videos") or []:
        raw = item.get("file") if isinstance(item, dict) else None
        if not raw:
            continue
        # manifest 里存的是仓库根相对路径（"outputs/<项目>/videos/vid_001.mp4"）。
        # 项目目录被整体搬走过时那条路径就断了，回落到按 basename 在本目录找。
        for path in (raw if os.path.isabs(raw) else os.path.join(repo_root, raw),
                     os.path.join(project_dir, "videos", os.path.basename(str(raw)))):
            if os.path.exists(path):
                break
        else:
            print(f"[WARN] cast_drift: 视频文件不存在，跳过：{raw}")
            continue
        label = f"VIDEO {item.get('slot') or item.get('sequence') or len(out) + 1}"
        sub = os.path.join(work_dir, label.replace(" ", "_")) if work_dir else None
        if sub:
            os.makedirs(sub, exist_ok=True)
        out.append((label, extract_frames(path, count=count, out_dir=sub)))
    return out


def format_report(report):
    """与协议第 6 节 QC 表第 1～8 项对齐的人读报告。"""
    lines = [f"contract: {report['contract_version']}  status: {report['status']}"]
    if report["status"] == "uncalibrated":
        lines.append(report["message"])
        return "\n".join(lines)
    lines.append(f"ΔE 容差: {report['delta_e']}")
    lines.append("\n部位基线（覆盖率过低的部位判不了，不计入结论）：")
    for s in report["slots"]:
        mark = "✓" if s["usable"] else "—"
        lines.append(f"  {mark} {s['slot']:<9} {s['reference_srgb']}  "
                     f"基线 {s['baseline_coverage']:.3%}  {s['required']}")
        if s["note"]:
            lines.append(f"      {s['note']}")
    if report["status"] == "inconclusive":
        lines.append("\n没有任何可判部位 —— 本次检查什么都没能说明。"
                     "\n这不是通过：先按上面每个部位的说明重新标定参考色，再跑一次。")
    elif report["findings"]:
        lines.append(f"\n{len(report['findings'])} 处剪影级漂移：")
        for f in report["findings"]:
            lines.append(f"  FAIL {f['segment']}  {f['detail']}")
    else:
        lines.append(f"\n{report['usable_slots']} 个可判部位在所有段里覆盖率稳定，"
                     f"未见剪影级漂移。")
    return "\n".join(lines)
