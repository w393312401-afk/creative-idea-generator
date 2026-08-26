"""Deterministic, backend-neutral continuity checks for rendered frame chains.

The image model is still responsible for semantic construction.  This module only
answers two narrower questions that can be judged locally and cheaply:

* did the locked camera / unchanged part of the scene drift; and
* did the declared change region move at all?

It deliberately requires two independent hard signals before rejecting a frame.
Low-texture or unavailable OpenCV environments degrade to a warning, never a hard
failure.  The same result shape is stored by both the API and Google FX renderers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any


CONTRACT_VERSION = "frame-continuity-v1"
MODES = ("off", "balanced", "strict")

THRESHOLDS = {
    "balanced": {
        "translation_ratio": 0.025,
        "scale_delta": 0.03,
        "edge_difference": 0.28,
        "pixel_difference": 0.18,
        "min_inlier_ratio": 0.35,
        "min_progress": 0.035,
        "min_matches": 14,
    },
    "strict": {
        "translation_ratio": 0.018,
        "scale_delta": 0.022,
        "edge_difference": 0.22,
        "pixel_difference": 0.14,
        "min_inlier_ratio": 0.42,
        "min_progress": 0.045,
        "min_matches": 12,
    },
}

_GRID_RE = re.compile(r"(?<![A-Za-z0-9])([ABCabc][123])(?![A-Za-z0-9])")


def _gate(key: str, config: dict[str, Any] | None):
    """走 server_common.GATE_SETTINGS 这张唯一真源表取值（含 server_config.json /
    环境变量兜底）。本模块刻意保持 backend-neutral、可独立单测，所以是延迟导入
    并在 server_common 不可用时退回本地默认——两边的默认值必须一致，见
    GATE_SETTINGS 里 frameContinuityMode / frameContinuityMaxRetries 两项。"""
    try:
        from server_common import gate_setting
    except Exception:
        return None
    return gate_setting(key, config)


def continuity_mode(config: dict[str, Any] | None) -> str:
    resolved = _gate("frameContinuityMode", config)
    if resolved is not None:
        return resolved
    raw = (config or {}).get("frameContinuityMode", "balanced")
    mode = str(raw or "balanced").strip().lower()
    return mode if mode in MODES else "balanced"


def continuity_max_retries(config: dict[str, Any] | None) -> int:
    # 档位默认：strict 比 balanced 多重试一次。这条"按档位取默认"的规则是本模块
    # 独有的（GATE_SETTINGS 里 frameContinuityMaxRetries 的 default=1 只在没有档位
    # 上下文时用），所以显式配了才走总表，没配就按档位推。
    mode = continuity_mode(config)
    default = 2 if mode == "strict" else 1
    raw = (config or {}).get("frameContinuityMaxRetries")
    if raw is None:
        try:
            from server_common import SERVER_CONFIG
            raw = SERVER_CONFIG.get("frameContinuityMaxRetries")
        except Exception:
            raw = None
    if raw is None:
        return default
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        return default


def file_sha256(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def changed_grid_cells(prompt: str = "", beat: dict[str, Any] | None = None) -> list[str]:
    declared = (beat or {}).get("changed_grid_cells") or []
    cells = [str(cell).upper() for cell in declared if _GRID_RE.fullmatch(str(cell).strip())]
    if not cells:
        # Legacy/staged prompt blocks do not carry the beat ladder.  Limit fallback parsing
        # to clauses that explicitly describe a changed area; locked-anchor grid mentions
        # must not make the entire frame editable.
        clauses = re.findall(
            r"(?:changed (?:area|cells?)|change (?:area|cells?)|改动区域|变化区域)"
            r"[^.!?\n]{0,140}", prompt or "", flags=re.IGNORECASE,
        )
        cells = [m.upper() for clause in clauses for m in _GRID_RE.findall(clause)]
    return list(dict.fromkeys(cells))[:3]


def is_transition_frame(sequence: int, image_meta: str = "", incoming_video_meta: str = "",
                        beat: dict[str, Any] | None = None) -> bool:
    meta = f"{image_meta or ''} {incoming_video_meta or ''}".upper()
    return bool(
        sequence <= 1
        or any(tag in meta for tag in ("BRIDGE", "CUT", "REFRAME", "TURN"))
        or (beat or {}).get("bridge_stage")
        or (beat or {}).get("hard_cut")
        or (beat or {}).get("transition_stage") in {"camera_reframe", "crossing", "turn"}
    )


def family_map(image_sequences: list[int], videos: dict[int, Any]) -> dict[int, str]:
    family = 1
    result: dict[int, str] = {}
    for seq in sorted(image_sequences):
        incoming = videos.get(seq - 1) if seq > 1 else None
        meta = incoming.get("meta", "") if isinstance(incoming, dict) else ""
        if seq > 1 and any(tag in str(meta).upper() for tag in ("BRIDGE", "CUT", "REFRAME")):
            family += 1
        result[seq] = f"family-{family}"
    return result


def sidecar_path(project_dir: str) -> str:
    return os.path.join(project_dir, "continuity", "scene_families.json")


def load_sidecar(project_dir: str) -> dict[str, Any]:
    path = sidecar_path(project_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {"version": CONTRACT_VERSION, "families": {}}


def save_sidecar(project_dir: str, data: dict[str, Any]) -> None:
    path = sidecar_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def register_family_master(project_dir: str, family_id: str, sequence: int, image_path: str) -> dict[str, Any]:
    data = load_sidecar(project_dir)
    families = data.setdefault("families", {})
    existing = families.get(family_id)
    current_hash = file_sha256(image_path)
    if (existing and existing.get("image_sha256") and os.path.exists(existing.get("image_path", ""))
            and not (int(existing.get("sequence") or -1) == int(sequence)
                     and existing.get("image_sha256") != current_hash)):
        return existing
    entry: dict[str, Any] = {
        "sequence": int(sequence),
        "image_path": os.path.abspath(image_path),
        "image_sha256": current_hash,
    }
    try:
        import cv2
        import numpy as np
        raw = np.fromfile(image_path, dtype=np.uint8)
        image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if image is not None:
            entry["width"] = int(image.shape[1])
            entry["height"] = int(image.shape[0])
            entry["mean_bgr"] = [round(float(x), 2) for x in image.mean(axis=(0, 1))]
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            entry["feature_count"] = int(len(cv2.ORB_create(nfeatures=800).detect(gray, None)))
    except Exception:
        pass
    families[family_id] = entry
    data["version"] = CONTRACT_VERSION
    save_sidecar(project_dir, data)
    return entry


def family_master(project_dir: str, family_id: str) -> dict[str, Any] | None:
    entry = (load_sidecar(project_dir).get("families") or {}).get(family_id)
    return entry if isinstance(entry, dict) else None


def _load_gray(path: str, max_side: int = 720):
    import cv2
    import numpy as np
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"cannot decode image: {path}")
    height, width = image.shape[:2]
    scale = min(1.0, max_side / float(max(height, width)))
    if scale < 1.0:
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    return image


def grid_mask(shape: tuple[int, int], cells: list[str], expansion_ratio: float = 0.06):
    """Return a uint8 mask where 255 denotes the declared change region."""
    import numpy as np
    height, width = shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    if not cells:
        # Unknown delta: reserve the centre only.  This makes the geometry check useful while
        # keeping no-progress a warning (the caller knows the region was inferred).
        cells = ["B2"]
    x_pad, y_pad = round(width * expansion_ratio), round(height * expansion_ratio)
    for cell in cells:
        # Project notation is A/B/C from top to bottom and 1/2/3 from left to right
        # (e.g. C2 is the bottom-centre foreground band).
        row = ord(cell[0].upper()) - ord("A")
        col = int(cell[1]) - 1
        x0, x1 = round(col * width / 3), round((col + 1) * width / 3)
        y0, y1 = round(row * height / 3), round((row + 1) * height / 3)
        mask[max(0, y0 - y_pad):min(height, y1 + y_pad),
             max(0, x0 - x_pad):min(width, x1 + x_pad)] = 255
    return mask


def _pair_metrics(reference_path: str, candidate_path: str, cells: list[str]) -> dict[str, Any]:
    import cv2
    import numpy as np
    ref = _load_gray(reference_path)
    cur = _load_gray(candidate_path)
    if cur.shape != ref.shape:
        cur = cv2.resize(cur, (ref.shape[1], ref.shape[0]), interpolation=cv2.INTER_AREA)
    change_mask = grid_mask(ref.shape, cells)
    stable_mask = cv2.bitwise_not(change_mask)
    orb = cv2.ORB_create(nfeatures=1400, fastThreshold=12)
    kp1, des1 = orb.detectAndCompute(ref, stable_mask)
    kp2, des2 = orb.detectAndCompute(cur, stable_mask)
    good = []
    if des1 is not None and des2 is not None and len(des1) >= 2 and len(des2) >= 2:
        for pair in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des1, des2, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                good.append(pair[0])

    translation_ratio = scale_delta = None
    inlier_ratio = None
    aligned = cur
    if len(good) >= 6:
        src = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                       ransacReprojThreshold=3.0)
        if matrix is not None:
            scale = math.sqrt(float(matrix[0, 0]) ** 2 + float(matrix[0, 1]) ** 2)
            scale_delta = abs(scale - 1.0)
            translation_ratio = math.hypot(float(matrix[0, 2]), float(matrix[1, 2])) / max(ref.shape)
            inlier_ratio = float(inliers.mean()) if inliers is not None else None
            aligned = cv2.warpAffine(cur, matrix, (ref.shape[1], ref.shape[0]),
                                     flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    stable = stable_mask > 0
    changed = change_mask > 0
    edge_ref = cv2.Canny(ref, 70, 150)
    edge_raw = cv2.Canny(cur, 70, 150)
    edge_cur = cv2.Canny(aligned, 70, 150)
    raw_edge_diff = float(np.mean((edge_ref[stable] > 0) != (edge_raw[stable] > 0))) if stable.any() else 0.0
    edge_diff = float(np.mean((edge_ref[stable] > 0) != (edge_cur[stable] > 0))) if stable.any() else 0.0
    pixel_diff = float(np.mean(np.abs(ref[stable].astype(np.float32) - aligned[stable]) / 255.0)) if stable.any() else 0.0
    progress = float(np.mean(np.abs(ref[changed].astype(np.float32) - aligned[changed]) / 255.0)) if changed.any() else 0.0
    return {
        "matches": len(good),
        "reference_features": len(kp1),
        "candidate_features": len(kp2),
        "translation_ratio": None if translation_ratio is None else round(translation_ratio, 5),
        "scale_delta": None if scale_delta is None else round(scale_delta, 5),
        "inlier_ratio": None if inlier_ratio is None else round(inlier_ratio, 4),
        "edge_difference": round(edge_diff, 4),
        "unaligned_edge_difference": round(raw_edge_diff, 4),
        "pixel_difference": round(pixel_diff, 4),
        "change_region_difference": round(progress, 4),
    }


def analyze_frame(reference_path: str, candidate_path: str, *, prompt: str = "",
                  beat: dict[str, Any] | None = None, master_path: str | None = None,
                  mode: str = "balanced", cells: list[str] | None = None) -> dict[str, Any]:
    """``cells``：显式指定差量区，绕开从 prompt/beat 推断的那一步。

    修复门禁要用（见 pipeline_orchestrator._measure_fix_triptych）：修前 / 修后两次读数
    必须用**同一块**稳定区掩膜才可比，而定向修复恰恰会改写候选帧自己的正文——推断出来
    的 cells 前后不同，比较的就是两个不同口径的读数，档位差异可能纯粹来自掩膜位移。
    """
    if mode == "off":
        return {"version": CONTRACT_VERSION, "status": "skipped", "reason": "continuity mode is off"}
    mode = mode if mode in THRESHOLDS else "balanced"
    thresholds = THRESHOLDS[mode]
    cells = list(cells) if cells is not None else changed_grid_cells(prompt, beat)
    result: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "status": "passed",
        "changed_grid_cells": cells,
        "reference_hash": file_sha256(reference_path),
        "family_master_hash": file_sha256(master_path),
        "reasons": [],
    }
    try:
        previous = _pair_metrics(reference_path, candidate_path, cells)
        result["previous"] = previous
        if master_path and os.path.abspath(master_path) != os.path.abspath(reference_path):
            result["master"] = _pair_metrics(master_path, candidate_path, cells)
    except Exception as exc:
        # 探针跑不起来（缺 OpenCV、图片解不开）＝**没有证据**，不是"画面被改坏了"的
        # 证据。hard_votes 显式留空，三联屏门禁据此把它当软信号处理，不会因为一次
        # 环境抖动就回滚掉一次真修好的重渲（见 effective_seam_rank）。
        result.update(status="warned", reason=f"local continuity check unavailable: {exc}",
                      hard_votes=[], warning_votes=["probe_unavailable"])
        return result

    metrics = [result["previous"]] + ([result["master"]] if result.get("master") else [])
    hard_votes: set[str] = set()
    warning_votes: set[str] = set()
    for values in metrics:
        matches = values.get("matches", 0)
        if matches < thresholds["min_matches"]:
            warning_votes.add("low_texture")
            continue
        translation = values.get("translation_ratio")
        scale = values.get("scale_delta")
        if ((translation is not None and translation > thresholds["translation_ratio"])
                or (scale is not None and scale > thresholds["scale_delta"])):
            hard_votes.add("camera")
        if (translation is not None and translation > thresholds["translation_ratio"]
                and values.get("unaligned_edge_difference", 0) > 0.035):
            hard_votes.add("composition")
        if (values.get("inlier_ratio") is not None
                and values["inlier_ratio"] < thresholds["min_inlier_ratio"]):
            hard_votes.add("landmarks")
        if (values.get("edge_difference", 0) > thresholds["edge_difference"]
                and values.get("pixel_difference", 0) > thresholds["pixel_difference"]):
            hard_votes.add("structure")

    progress = result["previous"].get("change_region_difference", 0)
    no_progress = progress < thresholds["min_progress"]
    if len(hard_votes) >= 2:
        result["status"] = "failed"
        result["reasons"].append("high-confidence scene/camera drift: " + ", ".join(sorted(hard_votes)))
    elif hard_votes or warning_votes:
        result["status"] = "warned"
        if hard_votes:
            result["reasons"].append("single drift signal: " + ", ".join(sorted(hard_votes)))
        if warning_votes:
            result["reasons"].append("low-texture fallback; not enough reliable keypoints")
    if no_progress:
        if result["status"] == "passed":
            result["status"] = "warned"
        result["reasons"].append("declared change region shows too little visible progress")
        # A staged/legacy prompt without structured changed cells has no authoritative delta
        # region.  Record low progress, but do not spend another generation on an inferred B2.
        result["retry_recommended"] = bool(cells)
    else:
        result["retry_recommended"] = result["status"] == "failed"
    # 硬 / 软信号分开留痕：下游的三联屏门禁要据此区分「一个硬漂移信号」与「低纹理
    # 或差量区推进不足」这两种同样落 warned、代价却完全不同的读数。
    result["hard_votes"] = sorted(hard_votes)
    result["warning_votes"] = sorted(warning_votes | ({"no_progress"} if no_progress else set()))
    result["reason"] = "; ".join(result["reasons"]) or "continuity checks passed"
    return result


# ── 三联屏门禁 ───────────────────────────────────────────────────────────────
#
# analyze_frame 回答的是「这一张新渲的帧，相对它的参考帧漂了没有」——**向前建链**时
# 每渲一帧问一次。它回答不了定向修复真正要问的那个问题：把已经在链上的第 K 帧换掉
# 之后，它**两侧**的缝还咬得住吗？
#
# 修复此前是开环的（pipeline_orchestrator._reverify_frame_issues 只复核「原来那几条
# 问题解决没有」，从不问「有没有修出新问题」）。于是一次修复可以在消灭 A 问题的同时
# 把 K-1→K 的透视撕开，而整条链路一声不吭——用户看到的是「✅ 均已消失」。这与节拍层
# 那台「越修越坏」的永动机是同一台，只是贵得多：帧的一次修复是 4 选 1 重渲。
#
# 判据刻意与本模块其余部分同源：**只认状态档位的严格恶化**（passed → warned/failed，
# warned → failed）。数值变差但档位没跳的一律只报不拦——analyze_frame 本身就要求两个
# 独立硬信号才否决，门禁不该比它更神经质，误判一次的代价是把一次真修好的重渲丢掉。
#
# 这条克制原来只写在注释里，实现却没有兑现，两处走样：
#
# 1) **软 warned 也被当成恶化**。analyze_frame 的 warned 有两种来源：一个硬漂移信号
#    （camera/structure/landmarks/composition），或者纯软信号——低纹理关键点不够、
#    差量区推进不足。后者根本不是"画面被改坏了"，却同样把 passed 顶成 warned。于是
#    passed → warned 这条回滚线比 analyze_frame 自己的否决线（要两个硬信号）还紧。
#    现在按 effective_seam_rank 折算：软信号造成的 warned 在判定上等同 passed，原始
#    档位照常交出去给人看。
#
# 2) **右缝有结构性偏置**。右缝＝K → K+1，而 K+1 是从**修复前**的 K 图生图出来的：
#    修复越有效，K 与 K+1 越不像。拿"修前 K ⇄ K+1"当基线去比"修后 K ⇄ K+1"，等于
#    要求这次修复不要改动任何东西——门禁惩罚的正是它本该放行的那类修复。所以两条缝
#    的判据不对称（SEAM_POLICY）：
#      · 左缝 K-1 → K：上游是既成事实，修 K 不该动它，任何严格恶化都拦（strict）。
#      · 右缝 K → K+1：只在跌到 failed（两个独立硬信号）时才拦，那是"链真的断了"，
#        不是"下游还没跟上"。跌到 warned 照常报出来给人看，但不回滚。

TRIPTYCH_CONTRACT_VERSION = "frame-triptych-v2"

_STATUS_RANK = {"passed": 0, "warned": 1, "failed": 2}

# 两条缝的人类可读名。左缝＝上游继承（K-1 → K），右缝＝下游通道（K → K+1）。
SEAM_LABELS = {"left": "K-1 → K（上游继承锁）", "right": "K → K+1（下游通道锁）"}

# 每条缝的拦截口径，理由见上方注释。
SEAM_POLICY = {"left": "strict", "right": "failed_only"}


def seam_rank(result: dict[str, Any] | None) -> int | None:
    """把一条缝的读数折成可比较的档位；skipped / 读不出来一律 None（不参与判定）。"""
    return _STATUS_RANK.get((result or {}).get("status"))


def effective_seam_rank(result: dict[str, Any] | None) -> int | None:
    """判定用档位：软信号造成的 warned 折回 passed。

    只在读数**明确带着** hard_votes 字段（measure_seam 一定写）且为空时才折算。旧读数
    /外部构造的读数没有这个字段＝硬软不明，保守按原档位算。
    """
    rank = seam_rank(result)
    if rank != 1:
        return rank
    votes = (result or {}).get("hard_votes")
    if isinstance(votes, list) and not votes:
        return 0
    return rank


def measure_seam(reference_path: str | None, candidate_path: str | None, *,
                 prompt: str = "", beat: dict[str, Any] | None = None,
                 mode: str = "balanced", cells: list[str] | None = None) -> dict[str, Any] | None:
    """一条缝的连贯性读数。任一端缺图、或档位关闭时返回 None ＝这条缝不参与判定。

    `prompt` 取**候选帧**（缝的下游那一张）的正文：changed_grid_cells 圈出的是这一帧
    自己申报的差量区，稳定区判读要把它挖掉。左缝取第 K 帧的正文，右缝取第 K+1 帧的。

    `cells` 显式给定时直接用它，不再从 prompt/beat 推断——修前 / 修后两次读数必须用同一
    块掩膜才可比（见 analyze_frame 的同名参数）。读数里带回 `cells`，供第二次调用复用。
    """
    if mode == "off":
        return None
    if not reference_path or not candidate_path:
        return None
    if not (os.path.exists(reference_path) and os.path.exists(candidate_path)):
        return None
    result = analyze_frame(reference_path, candidate_path, prompt=prompt, beat=beat,
                           mode=mode, cells=cells)
    used_cells = result.get("changed_grid_cells")
    if used_cells is None:
        used_cells = list(cells) if cells is not None else changed_grid_cells(prompt, beat)
    rank = seam_rank(result)
    if rank is None:
        # skipped / skipped_transition：读数存在但不可比，照样交出去给人看，只是不判。
        return {"status": result.get("status"), "rank": None,
                "reason": result.get("reason") or result.get("status"), "metrics": None,
                "cells": used_cells, "hard_votes": result.get("hard_votes") or []}
    return {
        "status": result.get("status"),
        "rank": rank,
        "reason": result.get("reason"),
        "metrics": result.get("previous"),
        "cells": used_cells,
        "hard_votes": result.get("hard_votes") or [],
        "warning_votes": result.get("warning_votes") or [],
    }


def compare_triptych(before: dict[str, Any] | None,
                     after: dict[str, Any] | None) -> dict[str, Any]:
    """比对修前 / 修后的两条缝，回答「这次修复有没有把画面改坏」。

    只有**两侧都读得出档位**的那条缝才参与判定：修前读不出（缺图、低纹理、档位关闭）
    的缝，修后再差也没有基线可比，不能凭空判死一次重渲。

    两条缝的拦截口径不对称，见 SEAM_POLICY 与本节顶部的注释：左缝任何严格恶化都算
    恶化；右缝只有跌到 failed 才算——它天生偏向"修得越有效越难看"。

    返回 {'version', 'regressed': [seam…], 'verdict': 'ok'|'regressed'|'unjudged',
          'seams': {seam: {'before':…, 'after':…, 'regressed': bool, 'policy':…,
                           'worsened': bool}}}
    `worsened` ＝档位确实变差了（无论是否触发拦截），供界面把"报出来但不回滚"的那一类
    如实说出来。
    """
    before = before or {}
    after = after or {}
    seams: dict[str, Any] = {}
    regressed: list[str] = []
    judged = 0
    for seam in ("left", "right"):
        b, a = before.get(seam), after.get(seam)
        b_rank, a_rank = effective_seam_rank(b), effective_seam_rank(a)
        policy = SEAM_POLICY.get(seam, "strict")
        row: dict[str, Any] = {"before": b, "after": a, "regressed": False,
                               "policy": policy, "worsened": False}
        if b_rank is not None and a_rank is not None:
            judged += 1
            worsened = a_rank > b_rank
            row["worsened"] = worsened
            blocking = worsened and (policy != "failed_only" or a_rank >= _STATUS_RANK["failed"])
            if blocking:
                row["regressed"] = True
                regressed.append(seam)
        seams[seam] = row
    verdict = "regressed" if regressed else ("ok" if judged else "unjudged")
    return {"version": TRIPTYCH_CONTRACT_VERSION, "verdict": verdict,
            "regressed": regressed, "judged_seams": judged, "seams": seams,
            "worsened": [s for s in ("left", "right") if seams[s]["worsened"]]}


def describe_triptych(comparison: dict[str, Any] | None) -> str:
    """门禁结论的一句中文，直接进 on_progress 消息与日志。"""
    comparison = comparison or {}
    verdict = comparison.get("verdict")
    seams = comparison.get("seams") or {}

    def _line(seam):
        row = seams.get(seam) or {}
        b = (row.get("before") or {}).get("status")
        a = (row.get("after") or {}).get("status")
        return (f"{SEAM_LABELS.get(seam, seam)}：{b} → {a}"
                + (f"（{(row.get('after') or {}).get('reason')}）"
                   if (row.get("after") or {}).get("reason") else ""))

    if verdict == "regressed":
        return "；".join(_line(seam) for seam in (comparison.get("regressed") or []))
    # 档位变差、但按该缝的口径不构成拦截（几乎总是右缝 K → K+1：下游还派生自修复前的
    # K，它变得不像本来就是修复奏效的副产物）。不拦，但必须如实说出来。
    soft = [s for s in (comparison.get("worsened") or [])
            if not ((seams.get(s) or {}).get("regressed"))]
    if verdict == "ok":
        head = f"两侧缝均未恶化（判读了 {comparison.get('judged_seams')} 条缝）"
        if soft:
            head = ("；".join(_line(s) for s in soft)
                    + "，按该缝口径不构成拦截（下游尚未跟着重渲），已放行")
        return head
    return "没有可比的基线读数，本次不做门禁判定"


def transition_result(reference_path: str | None, master_path: str | None = None,
                      reason: str = "declared camera-family transition") -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "status": "skipped_transition",
        "reference_hash": file_sha256(reference_path),
        "family_master_hash": file_sha256(master_path),
        "reason": reason,
    }
