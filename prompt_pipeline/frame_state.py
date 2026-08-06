"""Deterministic frame-state contracts for restoration prompt sequences.

The language model may describe a beat fluently while still reversing construction
order or packing several unrelated states into one frame.  This module converts the
beat ladder into a small, inspectable state transition contract and rejects those
errors before any image-generation credits are spent.
"""

from __future__ import annotations

import re
from typing import Any


TRANSITION_OPERATIONS = {"threshold", "reward", "reframe"}

# "这个面又变回生料/裸露了" 的措辞。只认字面词是不够的：一句完工描述里出现这些词，
# 十有八九是被否定掉的（"…with no bare subfloor left exposed"、"…with no bare patches
# remaining"），那恰恰是**完工**的说法，不是回退。2026-08-06 实测：兜底梯子的
# flooring / painting 两档 after_state 都带 "no bare …"，跟在任何一条含
# complete/finished 的前序状态后面就被这道闸判成"显式回退"，整单直接 QUALITY_GATE_FAILED。
_REGRESSION_WORDS = r"(?:absent|missing|removed|bare|raw|open again)"
_NEGATED_REGRESSION_RE = re.compile(
    rf"\b(?:no|not|never|without|zero|free of)\b(?:\W+\w+){{0,3}}?\W+{_REGRESSION_WORDS}\b",
    re.IGNORECASE,
)
_REGRESSION_RE = re.compile(rf"\b{_REGRESSION_WORDS}\b", re.IGNORECASE)


def _asserts_regression(after: str) -> bool:
    """after_state 是否**正面主张**某个面回到了生料/缺失状态（否定式说法不算）。"""
    return bool(_REGRESSION_RE.search(_NEGATED_REGRESSION_RE.sub(" ", after or "")))

# Coarse construction phases.  These are deliberately conservative: equal ranks are
# allowed, forward movement is allowed, and a lower rank is only allowed for an
# explicit clearing/removal operation or a registered new space.
_PHASE_RANK = {
    "clearing": 0,
    "demolition": 0,
    "repair": 1,
    "placement": 1,
    "rough-in": 2,
    "rough_in": 2,
    "wiring": 2,
    "plumbing": 2,
    "framing": 3,
    "insulation": 4,
    "drywall": 5,
    "paneling": 5,
    "priming": 6,
    "painting": 6,
    "flooring": 6,
    "lighting": 7,
    "furnishing": 8,
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _space_id(beat: dict[str, Any]) -> str:
    return _text(beat.get("space_id") or beat.get("space_family") or "primary").lower()


def _phase(beat: dict[str, Any]) -> int | None:
    tokens = [_text(beat.get("operation")).lower()]
    tokens.extend(_text(x).lower() for x in (beat.get("package_operations") or []))
    ranks = [_PHASE_RANK[t] for t in tokens if t in _PHASE_RANK]
    return min(ranks) if ranks else None


def build_frame_state_contract(beat_ladder: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a serializable state transition record for every delivered beat."""
    contracts: list[dict[str, Any]] = []
    for position, beat in enumerate(beat_ladder or [], start=1):
        beat = beat if isinstance(beat, dict) else {}
        op = _text(beat.get("operation")).lower()
        package = [_text(x).lower() for x in (beat.get("package_operations") or []) if _text(x)]
        contracts.append({
            "beat": int(beat.get("index") or position),
            "space": _space_id(beat),
            "operation": op,
            "before": _text(beat.get("before_state")),
            "delta": _text(beat.get("milestone_name") or beat.get("description")),
            "after": _text(beat.get("after_state")),
            "preserve": _text(beat.get("preserve_state")),
            "forbidden": _text(beat.get("forbidden_state") or "all later-stage results and any regression"),
            "changed_grid_cells": list(dict.fromkeys(beat.get("changed_grid_cells") or [])),
            "package_operations": package,
            "persistent_traces": [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)],
            "phase": _phase(beat),
            "transition": bool(
                op in TRANSITION_OPERATIONS or beat.get("bridge_stage") or beat.get("hard_cut")
            ),
        })
    return contracts


def validate_frame_state_contract(
    contracts: list[dict[str, Any]],
    *,
    min_package_operations: int = 2,
    max_package_operations: int = 3,
) -> list[str]:
    """Validate one-delta-per-frame and monotonic construction state.

    The check is intentionally text-light.  It validates declared structured fields,
    not prose similarity, so it is deterministic and safe to run as a hard gate.
    """
    errors: list[str] = []
    seen_after: dict[str, str] = {}
    seen_transitions: dict[tuple[str, str], int] = {}

    for expected, item in enumerate(contracts or [], start=1):
        idx = item.get("beat") or expected
        if idx != expected:
            errors.append(f"Beat {idx} is out of sequence; expected beat {expected}.")
        if item.get("transition"):
            # A transition carries no construction state, so the milestone checks below do not
            # apply — but it must still be a distinct step.  Expanding one crossing marker into
            # several stages used to clone the marker's state fields verbatim, and skipping the
            # whole loop body meant the identical stages passed clean and shipped as several
            # consecutive clips of the same crossing.
            signature = (_text(item.get("delta")).lower(), _text(item.get("after")).lower())
            if any(signature):
                first = seen_transitions.get(signature)
                if first is not None:
                    errors.append(
                        f"Beat {idx} is a transition that repeats beat {first} verbatim; every "
                        f"crossing stage needs its own camera-position state."
                    )
                else:
                    seen_transitions[signature] = idx
            continue

        missing = [name for name in ("before", "delta", "after", "preserve") if not item.get(name)]
        if missing:
            errors.append(f"Beat {idx} frame-state contract is missing: {', '.join(missing)}.")

        package = item.get("package_operations") or []
        # 口径与 milestone_ladder_violations / ladder schema 第 13 条严格一致：
        # 一个普通施工拍申报 2~3 道紧密相关的工序。三处曾各写各的（schema 说 1~3、
        # 里程碑闸判 1~3、这里判 1~2），于是一条 3 工序的收口拍能一路通过规划验收，
        # 再被这道循环外硬闸判死，整单只拿到一句报错。改这里时务必同步那两处。
        if not min_package_operations <= len(package) <= max_package_operations:
            errors.append(
                f"Beat {idx} declares {len(package)} operations; a frame must carry "
                f"{min_package_operations} to {max_package_operations} tightly coupled operations "
                f"that share one terminal product."
            )
        if len(item.get("changed_grid_cells") or []) > 3:
            errors.append(f"Beat {idx} changes more than three composition cells.")
        if len(item.get("persistent_traces") or []) < 2:
            errors.append(f"Beat {idx} declares fewer than two visible persistent traces.")

        space = item.get("space") or "primary"
        op = item.get("operation") or ""
        after = _text(item.get("after")).lower()
        previous_after = seen_after.get(space)
        # Exact or near-exact state rewrites are a strong signal that the planner did not
        # actually advance this frame.  Avoid broad semantic guessing here.
        if previous_after and after == previous_after:
            errors.append(f"Beat {idx} repeats the preceding terminal state instead of advancing it.")
        if after:
            seen_after[space] = after

        # Catch explicit wording of a completed surface becoming raw/absent again.  This
        # complements the operation-rank check without trying to understand all materials.
        if previous_after and _asserts_regression(after):
            if op not in {"clearing", "demolition", "repair"} and any(
                word in previous_after for word in ("complete", "completed", "finished", "sealed", "installed")
            ):
                errors.append(f"Beat {idx} explicitly regresses a previously completed state.")

    return errors


def compile_delta_image_prompt(
    original: str,
    beat: dict[str, Any] | None,
    *,
    max_words: int = 160,
) -> str:
    """Compile an ordinary milestone IMAGE into a concise state-delta prompt.

    Camera and locked-anchor sentences are preserved verbatim.  The remaining prose is
    replaced by the ladder's authoritative state fields, making the current frame's one
    requested change much more salient to the image model.
    """
    if not isinstance(beat, dict):
        return _text(original)
    op = _text(beat.get("operation")).lower()
    if op in TRANSITION_OPERATIONS or beat.get("bridge_stage") or beat.get("hard_cut"):
        return _text(original)
    required = ("milestone_name", "after_state", "completion_extent", "preserve_state")
    if any(not _text(beat.get(field)) for field in required):
        # Standalone repair helpers and legacy callers may provide only an operation.
        # They have no authoritative state delta to compile, so preserve their prompt.
        return _text(original)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", _text(original)) if s.strip()]
    camera = sentences[0] if sentences else "Generate one documentary photograph from the locked camera."
    anchor = next((s for s in sentences if s.lower().startswith("locked anchors:")), "")
    parts = [camera]
    if anchor and anchor != camera:
        parts.append(anchor)

    preserve = _text(beat.get("preserve_state"))
    milestone = _text(beat.get("milestone_name"))
    after = _text(beat.get("after_state"))
    extent = _text(beat.get("completion_extent"))
    traces = [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)][:3]

    if preserve:
        parts.append(f"Inherited state remains unchanged: {preserve}.")
    parts.append(f"Only visible construction delta in this frame: {milestone}.")
    if after:
        parts.append(f"Completed terminal state: {after}.")
    if extent:
        parts.append(f"Completion extent: {extent}.")
    if traces:
        parts.append(f"Visible physical evidence remains: {', '.join(traces)}.")
    parts.append(
        "Show no unrelated work, no later-stage result, and no regression of any previously completed feature."
    )
    parts.append("One clean documentary photograph with no active construction and no text artifacts.")

    result = " ".join(parts)
    words = result.split()
    if len(words) > max_words:
        # Preserve camera, anchors and the authoritative state delta; trim only the tail.
        result = " ".join(words[:max_words]).rstrip(" ,;:") + "."
    return result
