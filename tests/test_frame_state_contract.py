from frame_generator import frame_chain_gate_enabled
from prompt_pipeline.frame_state import (
    build_frame_state_contract,
    compile_delta_image_prompt,
    validate_frame_state_contract,
)


def _beat(index, operation, *, package=None, after=None, space="primary"):
    return {
        "index": index,
        "space_id": space,
        "operation": operation,
        "milestone_name": f"{operation} milestone complete",
        "before_state": f"state before {operation}",
        "after_state": after or f"state after {operation} completed",
        "completion_extent": "the full visible work zone",
        "preserve_state": "all earlier completed features remain unchanged",
        "changed_grid_cells": ["B2", "C2"],
        "package_operations": package or [operation],
        "persistent_traces": ["fastener heads", "tool contact marks"],
    }


def test_state_contract_accepts_monotonic_single_delta_ladder():
    ladder = [
        _beat(1, "clearing"),
        _beat(2, "repair"),
        _beat(3, "rough-in"),
        _beat(4, "framing"),
        _beat(5, "drywall"),
        _beat(6, "painting"),
        _beat(7, "furnishing"),
    ]
    assert validate_frame_state_contract(build_frame_state_contract(ladder)) == []


def test_state_contract_rejects_overpacked_and_regressing_beat():
    ladder = [
        _beat(1, "painting", after="the full wall finish is completed and sealed"),
        _beat(
            2,
            "framing",
            package=["framing", "insulation", "drywall"],
            after="the same completed wall is raw and open again",
        ),
    ]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert any("only one primary operation or two tightly coupled" in error for error in errors)
    assert any("explicitly regresses" in error for error in errors)


def test_delta_prompt_keeps_camera_anchor_and_authoritative_state_under_budget():
    beat = _beat(2, "framing")
    original = (
        "Generate an image of a static wide tripod shot with horizon line locked. "
        "Locked anchors: steel hatch at Grid B2, curved rib at Grid C2. "
        + "Decorative low-priority prose. " * 100
    )
    result = compile_delta_image_prompt(original, beat, max_words=160)
    assert len(result.split()) <= 160
    assert "static wide tripod shot" in result
    assert "Locked anchors:" in result
    assert beat["milestone_name"] in result
    assert beat["after_state"] in result
    assert "no later-stage result" in result


def test_frame_chain_gate_defaults_on_and_respects_escape_hatches(monkeypatch):
    assert frame_chain_gate_enabled({"qaGateLevel": "standard"}) is True
    assert frame_chain_gate_enabled({"qaGateLevel": "standard", "frameChainGate": False}) is False
    assert frame_chain_gate_enabled({"qaGateLevel": "off"}) is False
