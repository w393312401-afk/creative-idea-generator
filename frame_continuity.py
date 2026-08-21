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
                  mode: str = "balanced") -> dict[str, Any]:
    if mode == "off":
        return {"version": CONTRACT_VERSION, "status": "skipped", "reason": "continuity mode is off"}
    mode = mode if mode in THRESHOLDS else "balanced"
    thresholds = THRESHOLDS[mode]
    cells = changed_grid_cells(prompt, beat)
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
        result.update(status="warned", reason=f"local continuity check unavailable: {exc}")
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
    result["reason"] = "; ".join(result["reasons"]) or "continuity checks passed"
    return result


def transition_result(reference_path: str | None, master_path: str | None = None,
                      reason: str = "declared camera-family transition") -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "status": "skipped_transition",
        "reference_hash": file_sha256(reference_path),
        "family_master_hash": file_sha256(master_path),
        "reason": reason,
    }
