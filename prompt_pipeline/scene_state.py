"""Authoritative structured scene-state transitions and deterministic prompt skeletons."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


TRANSITIONS = {"threshold", "reward", "reframe"}
REMOVAL = {"clearing", "demolition", "removal", "excavation"}
INSTALLATION = {"installation", "framing", "rough-in", "rough_in", "wiring", "plumbing", "lighting"}
WET_WORK = {"wet-work", "wet_work", "plastering", "concrete", "priming", "painting"}
COVERING = {"covering", "drywall", "paneling", "flooring", "finishing"}
FURNISHING = {"furnishing", "placement", "move-in", "move_in"}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def classify_material_flow(operation: str) -> str:
    op = _text(operation).lower().replace("_", "-")
    if op in REMOVAL:
        return "removal"
    if op in INSTALLATION:
        return "installation"
    if op in WET_WORK:
        return "wet_work"
    if op in COVERING:
        return "covering"
    if op in FURNISHING:
        return "furnishing"
    return "transition" if op in TRANSITIONS else "other"


def default_material_flow(operation: str) -> list[dict[str, str]]:
    family = classify_material_flow(operation)
    flows = {
        "removal": [("site_waste", "decrease"), ("waste_container", "increase"), ("offsite_waste", "increase")],
        "installation": [("offsite_inventory", "decrease"), ("installed_components", "increase")],
        "wet_work": [("wet_material", "decrease"), ("finished_surface", "increase"), ("cured_surface_next_anchor", "increase")],
        "covering": [("uncovered_area", "decrease"), ("finished_area", "increase")],
        "furnishing": [("offsite_objects", "decrease"), ("indoor_objects", "increase")],
    }
    return [{"store": store, "direction": direction} for store, direction in flows.get(family, [])]


def build_scene_states(beat_ladder: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile a ladder into state[N+1] = state[N] + validated_delta[N]."""
    states: list[dict[str, Any]] = []
    permanent: dict[str, Any] = {}
    known_objects: set[str] = set()
    previous_summary = "initial accepted scene state"
    for position, source in enumerate(beat_ladder or [], 1):
        beat = source if isinstance(source, dict) else {}
        operation = _text(beat.get("operation")).lower()
        before = deepcopy(permanent)
        introduced = [str(x) for x in (beat.get("introduced_objects") or []) if _text(x)]
        removed = [str(x) for x in (beat.get("removed_objects") or []) if _text(x)]
        delta = {
            "milestone": _text(beat.get("milestone_name") or beat.get("description")),
            "terminal_state": _text(beat.get("after_state")),
            "completion_extent": _text(beat.get("completion_extent")),
        }
        if operation not in TRANSITIONS and not beat.get("bridge_stage") and not beat.get("hard_cut"):
            permanent[f"beat_{position}"] = deepcopy(delta)
        for name in introduced:
            known_objects.add(name.lower())
        for name in removed:
            known_objects.discard(name.lower())
        states.append({
            "beat": int(beat.get("index") or position),
            "operation_type": operation,
            "changed_cells": list(dict.fromkeys(beat.get("changed_grid_cells") or [])),
            "before": before,
            "before_summary": previous_summary,
            "delta": delta,
            "after": deepcopy(permanent),
            "after_summary": delta["terminal_state"] or delta["milestone"],
            "preserve": [_text(beat.get("preserve_state"))] if _text(beat.get("preserve_state")) else [],
            "introduced_objects": introduced,
            "removed_objects": removed,
            "persistent_traces": [_text(x) for x in (beat.get("persistent_traces") or []) if _text(x)],
            "material_flow": deepcopy(beat.get("material_flow") or default_material_flow(operation)),
            "anchor_camera": {
                "family": _text(beat.get("camera_family") or beat.get("space_id") or "primary"),
                "locked": True,
                "world_coordinates_mutable": False,
            },
            "edit_shots": list(beat.get("edit_shots") or []),
            "known_objects_after": sorted(known_objects),
        })
        if operation not in TRANSITIONS and not beat.get("bridge_stage") and not beat.get("hard_cut"):
            previous_summary = delta["terminal_state"] or delta["milestone"]
    return states


def validate_material_flow(state: dict[str, Any]) -> list[str]:
    expected = default_material_flow(state.get("operation_type", ""))
    if not expected:
        return []
    actual = state.get("material_flow") or []
    pairs = {(str(x.get("store")), str(x.get("direction"))) for x in actual if isinstance(x, dict)}
    missing = [x for x in expected if (x["store"], x["direction"]) not in pairs]
    return [f"Beat {state.get('beat')} material flow is missing {x['store']}:{x['direction']}." for x in missing]


def validate_scene_states(states: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous_after: dict[str, Any] = {}
    known: set[str] = set()
    for expected, state in enumerate(states or [], 1):
        idx = state.get("beat") or expected
        if idx != expected:
            errors.append(f"Beat {idx} is out of sequence; expected {expected}.")
        if state.get("before") != previous_after:
            errors.append(f"Beat {idx} before state does not equal the preceding validated after state.")
        for name in state.get("removed_objects") or []:
            if name.lower() not in known:
                errors.append(f"Beat {idx} removes undeclared object '{name}'.")
        for name in state.get("introduced_objects") or []:
            known.add(name.lower())
        for name in state.get("removed_objects") or []:
            known.discard(name.lower())
        errors.extend(validate_material_flow(state))
        previous_after = deepcopy(state.get("after") or {})
    return errors


def compile_image_skeleton(camera_dna: str, landmarks: list[Any], state: dict[str, Any], lighting: str) -> str:
    return " ".join(filter(None, [
        f"Camera DNA: {_text(camera_dna)}.",
        f"Locked landmarks: {', '.join(_text(x) for x in landmarks if _text(x))}.",
        f"Inherited state: {_text(state.get('before'))}.",
        f"Only delta: {_text(state.get('delta'))}.",
        f"Preserve: {_text(state.get('preserve'))}.",
        f"Lighting: {_text(lighting)}.",
    ]))


def compile_video_skeleton(state: dict[str, Any]) -> str:
    return " ".join([
        f"First-frame state: {_text(state.get('before_summary'))}.",
        "The tool makes its first visible contact before any state changes.",
        f"Repeated physical actions execute only this delta: {_text(state.get('delta'))}.",
        f"Material path: {_text(state.get('material_flow'))}.",
        f"Last-frame state: {_text(state.get('after_summary'))}.",
    ])
