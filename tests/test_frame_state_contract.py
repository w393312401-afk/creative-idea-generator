from prompt_pipeline.frame_state import (
    build_frame_state_contract,
    compile_delta_image_prompt,
    validate_frame_state_contract,
)


# 每个相位的同族伴随工序：一个普通施工拍要申报 2~3 道紧密工序，同族配对不会额外
# 触发相位冲突判据。与 deterministic_fallback_beat_ladder 里的 phase_companion 同源。
_COMPANION = {
    "clearing": "demolition", "repair": "placement", "rough-in": "wiring",
    "framing": "insulation", "drywall": "paneling", "flooring": "painting",
    "painting": "priming", "furnishing": "lighting",
}


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
        "package_operations": package or [operation, _COMPANION[operation]],
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
            package=["framing", "insulation", "drywall", "flooring"],
            after="the same completed wall is raw and open again",
        ),
    ]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert any("2 to 3 tightly coupled operations" in error for error in errors)
    assert any("explicitly regresses" in error for error in errors)


def test_state_contract_does_not_read_a_negated_mention_as_a_regression():
    """"…with no bare subfloor left exposed" 是**完工**的说法，不是回退。

    2026-08-06：回退判据只按字面找 bare/raw/absent，而兜底梯子的 flooring / painting
    两档 after_state 恰好都带 "no bare …"；跟在任何一条含 complete/finished 的前序
    状态后面就被判成"显式回退"，整单直接 QUALITY_GATE_FAILED，且这道闸在循环外，
    没有任何回炉通路能救。
    """
    for after in (
        "the finish flooring fully covers the entire floor area edge to edge with no bare "
        "subfloor left exposed",
        "the entire surface is coated in even, fully dry finish paint with no bare patches remaining",
        "every framed surface is panelled with no seams left raw",
        "ceiling and left walls are 100% insulated and fully enclosed with raw OSB sheathing panels",
        "all perimeter framing bays are infilled with raw unrendered red clay brick masonry",
    ):
        ladder = [
            _beat(1, "drywall", after="the panel run is completed and sealed across the span"),
            _beat(2, "flooring", after=after),
        ]
        errors = validate_frame_state_contract(build_frame_state_contract(ladder))
        assert not any("explicitly regresses" in error for error in errors), after


def test_state_contract_still_rejects_a_positively_asserted_regression():
    ladder = [
        _beat(1, "drywall", after="the panel run is completed and sealed across the span"),
        _beat(2, "flooring", after="that same completed wall is bare framing again"),
    ]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert any("explicitly regresses" in error for error in errors)


def test_state_contract_rejects_a_single_operation_beat():
    """下限 2：一道工序的拍不再合规。这条与 milestone_ladder_violations 和 ladder
    schema 第 13 条是同一口径——三处任意一处漂开，梯子就会通过规划验收后被这道
    循环外硬闸判死，用户等满整轮规划只拿到一句 RuntimeError。"""
    ladder = [_beat(1, "framing", package=["framing"])]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert any("declares 1 operations" in error for error in errors)


def _crossing(index, stage, *, before, after):
    return {
        "index": index,
        "space_id": "primary",
        "operation": "threshold",
        "bridge_stage": index,
        "transition_stage": stage,
        "milestone_name": "",
        "before_state": before,
        "after_state": after,
        "preserve_state": "this beat builds nothing",
        "changed_grid_cells": [],
        "package_operations": [],
        "persistent_traces": [],
    }


def test_state_contract_rejects_crossing_stages_cloned_from_one_marker():
    """Expanding one crossing marker used to copy its state fields into every stage, and the
    validator skipped transitions wholesale — so three identical beats shipped as three
    consecutive clips walking through the same door (2026-08-05 petrified-cypress run)."""
    cloned = "Camera is fully positioned inside the petrified root cavity."
    ladder = [
        _beat(1, "clearing"),
        _crossing(2, "door_hardware_open", before="Camera is outside the arch.", after=cloned),
        _crossing(3, "threshold_partial", before="Camera is outside the arch.", after=cloned),
        _crossing(4, "interior_establish", before="Camera is outside the arch.", after=cloned),
    ]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert [e for e in errors if "repeats beat 2 verbatim" in e]
    assert sum("transition that repeats" in e for e in errors) == 2


def test_state_contract_accepts_crossing_stages_with_their_own_camera_states():
    ladder = [
        _beat(1, "clearing"),
        _crossing(2, "door_hardware_open",
                  before="Camera stands outside with the opening still closed off.",
                  after="Camera still stands outside; the entrance is open and dark beyond it."),
        _crossing(3, "threshold_partial",
                  before="Camera stands outside the open entrance on the site side of the sill.",
                  after="Camera has crossed the sill locally; the opening edge stays in frame."),
        _crossing(4, "interior_establish",
                  before="Camera holds the partial landed view with the far interior occluded.",
                  after="Camera has settled on the establishing axis and the raw space reads whole."),
    ]
    assert validate_frame_state_contract(build_frame_state_contract(ladder)) == []


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
