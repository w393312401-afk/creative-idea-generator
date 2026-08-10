import pytest

import prompt_pipeline as pp
from prompt_pipeline.frame_state import (
    build_frame_state_contract,
    validate_frame_state_contract,
)
from prompt_pipeline.scene_state import (
    build_scene_states,
    classify_material_flow,
    validate_scene_states,
)
from server import prompt_delivery_block_reason


def _beat(index, operation):
    return {
        "index": index,
        "operation": operation,
        "description": f"work {index}",
        "milestone_name": f"milestone {index}",
        "before_state": f"before {index}",
        "after_state": f"after {index}",
        "completion_extent": "full declared zone",
        "preserve_state": "all earlier permanent work remains unchanged",
        "changed_grid_cells": ["B2", "C2"],
        "package_operations": [operation, "placement"],
        "introduced_objects": [],
        "removed_objects": [],
        "persistent_traces": ["trace one", "trace two"],
    }


def test_material_flow_classifier_covers_required_operation_families():
    assert classify_material_flow("clearing") == "removal"
    assert classify_material_flow("framing") == "installation"
    assert classify_material_flow("painting") == "wet_work"
    assert classify_material_flow("flooring") == "covering"
    assert classify_material_flow("furnishing") == "furnishing"


def test_scene_state_chain_is_monotonic_and_material_flow_is_compiled():
    states = build_scene_states([_beat(1, "clearing"), _beat(2, "framing")])
    assert states[1]["before"] == states[0]["after"]
    assert validate_scene_states(states) == []
    assert {x["store"] for x in states[0]["material_flow"]} >= {"site_waste", "waste_container"}
    assert {x["store"] for x in states[1]["material_flow"]} >= {"offsite_inventory", "installed_components"}


def test_scene_state_allows_removing_an_original_trauma_object_without_prior_introduction():
    """2026-08-06 修复：系统提示词承诺 removed_objects 可以是原始现场自带、从未被
    introduced_objects 声明过的物体（翻新题材早期清理拍的常态）。校验器之前对此
    无条件报错，跟提示词矛盾，导致真实合成 6/6 次全部倒在这道闸上。"""
    beat = _beat(1, "clearing")
    beat["removed_objects"] = ["original salvage debris"]
    errors = validate_scene_states(build_scene_states([beat]))
    assert not any("undeclared object" in error for error in errors)
    assert not any("again without a fresh introduction" in error for error in errors)


def test_scene_state_rejects_removing_the_same_object_twice_without_reintroduction():
    beat1 = _beat(1, "clearing")
    beat1["removed_objects"] = ["mystery cabinet"]
    beat2 = _beat(2, "clearing")
    beat2["removed_objects"] = ["mystery cabinet"]
    errors = validate_scene_states(build_scene_states([beat1, beat2]))
    assert any("again without a fresh introduction" in error for error in errors)


def test_scene_state_allows_removal_after_a_fresh_reintroduction():
    beat1 = _beat(1, "clearing")
    beat1["removed_objects"] = ["salvage crate"]
    beat2 = _beat(2, "furnishing")
    beat2["introduced_objects"] = ["salvage crate"]
    beat3 = _beat(3, "clearing")
    beat3["removed_objects"] = ["salvage crate"]
    errors = validate_scene_states(build_scene_states([beat1, beat2, beat3]))
    assert not any("again without a fresh introduction" in error for error in errors)


def test_scene_state_rejects_removing_a_persistent_structural_element():
    """地板/拱门这类结构件一旦铺设就必须全程继承，不存在"先装后拆"的合法叙事。"""
    beat1 = _beat(1, "wet-work")
    beat1["introduced_objects"] = ["marble archway"]
    beat2 = _beat(2, "clearing")
    beat2["removed_objects"] = ["marble archway"]
    errors = validate_scene_states(build_scene_states([beat1, beat2]))
    assert any("persistent structural element" in error for error in errors)


def test_scene_state_rejects_lingering_temporary_object_at_furnishing_phase():
    """施工工具/设备是临时态：进入 furnishing 阶段时必须已经清场。"""
    beat1 = _beat(1, "installation")
    beat1["introduced_objects"] = ["scaffolding"]
    beat2 = _beat(2, "furnishing")
    errors = validate_scene_states(build_scene_states([beat1, beat2]))
    assert any("temporary construction objects" in error for error in errors)


def test_reverse_engineered_ladders_may_keep_temporary_objects_through_furnishing():
    """复刻线豁免清场那一条。

    这条规则审的是施工纪律，对原创单成立；而复刻单交付的是对一条真实成片的转录，
    原片里那块防护布确实铺到了搬家具那一拍。对着照实转录判"你不该这样施工"，
    拦下的不是缺陷是真实——在此之前它会把每一单复刻都判死。
    """
    beat1 = _beat(1, "installation")
    beat1["introduced_objects"] = ["scaffolding"]
    beat2 = _beat(2, "furnishing")
    states = build_scene_states([beat1, beat2])
    assert not any("temporary construction objects" in e
                   for e in validate_scene_states(states, allow_lingering_temporaries=True))


def test_the_replica_exemption_is_scoped_to_that_one_rule():
    """豁免只放清场那一条。状态账自身对不对得上（时序、重复移除、持久件不得拆除）
    与题材来源无关，复刻单错了照样是错。"""
    beat1 = _beat(1, "installation")
    beat1["introduced_objects"] = ["marble archway"]
    beat2 = _beat(2, "covering")
    beat2["removed_objects"] = ["marble archway"]
    errors = validate_scene_states(build_scene_states([beat1, beat2]),
                                   allow_lingering_temporaries=True)
    assert any("persistent structural element" in error for error in errors)


def test_scene_state_allows_persistent_and_cleared_temporary_objects_through_full_flow():
    """正常翻新流程不应被误伤：工具及时清场、结构件全程保留都不该报错。"""
    beat1 = _beat(1, "installation")
    beat1["introduced_objects"] = ["scaffolding", "marble archway"]
    beat2 = _beat(2, "covering")
    beat2["removed_objects"] = ["scaffolding"]
    beat3 = _beat(3, "furnishing")
    errors = validate_scene_states(build_scene_states([beat1, beat2, beat3]))
    assert not any("temporary construction objects" in error for error in errors)
    assert not any("persistent structural element" in error for error in errors)


def test_outline_fallback_preserves_every_source_row_and_reference():
    brief = {
        "beat_outline": [
            {"op": "clearing", "text": "clear the original debris"},
            {"op": "framing", "text": "frame the original alcove"},
        ],
        "mode": "Standard",
    }
    ladder = pp.compile_outline_fallback_ladder(brief, 2)
    assert [(x["operation"], x["description"]) for x in ladder] == [
        ("clearing", "clear the original debris"),
        ("framing", "frame the original alcove"),
    ]
    assert [x["outline_refs"] for x in ladder] == [[1], [2]]


def test_outline_fallback_gives_every_beat_its_own_terminal_state():
    """同一道工序在一张卡里出现两次，兜底梯子不能发出逐字相同的 after_state。

    2026-08-06：状态字段按 operation 从模板取，两拍清理 / 两拍封板就拿到完全一样的
    终态，被 validate_frame_state_contract 的"重复终态"闸判死。那道闸没判错——它读到
    的确实是两个一模一样的终态；错的是这里发出了两份。
    """
    brief = {
        "mode": "Standard",
        "beat_outline": [
            {"op": "clearing", "text": "清理石屋内积渣与残破废铁"},
            {"op": "clearing", "text": "清运室外杂草与碎石堆"},
            {"op": "drywall", "text": "钉装环保欧松板封闭内衬"},
            {"op": "drywall", "text": "封装屋顶保温欧松板"},
        ],
    }
    ladder = pp.compile_outline_fallback_ladder(brief, 4)
    assert len({x["after_state"] for x in ladder}) == len(ladder)
    for beat, entry in zip(ladder, brief["beat_outline"]):
        assert entry["text"] in beat["after_state"]
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert not any("repeats the preceding terminal state" in error for error in errors)


def test_outline_fallback_without_a_card_outline_fails_planning_by_default():
    """No card outline to compile from: allow_generic defaults True (existing callers
    keep the old permissive shape), but production compose passes allow_generic=False
    so this becomes a planning-stage ComposeFailure instead of the hollow "stage N
    complete" ladder — the exact text a 2026-08-06 incident traced back to this path."""
    brief = {"mode": "Standard"}
    # Default stays permissive so callers that don't opt in are unaffected.
    ladder = pp.compile_outline_fallback_ladder(brief, 4)
    assert ladder and ladder[0]["milestone_name"] == "stage 1 complete"

    with pytest.raises(pp.ComposeFailure) as exc:
        pp.compile_outline_fallback_ladder(brief, 4, allow_generic=False)
    assert exc.value.failure_code == "PLANNING_NO_OUTLINE"


def test_outline_fallback_with_a_real_outline_ignores_allow_generic():
    """A genuine card outline is never rejected by the strict flag — allow_generic only
    gates the *no outline at all* branch, not a normally-compiled ladder."""
    brief = {"beat_outline": [{"op": "clearing", "text": "clear debris"}], "mode": "Standard"}
    ladder = pp.compile_outline_fallback_ladder(brief, 1, allow_generic=False)
    assert ladder[0]["operation"] == "clearing"


def test_render_gate_blocks_only_explicit_new_degraded_results():
    assert prompt_delivery_block_reason({"degraded": True})
    assert prompt_delivery_block_reason({"quality_gate": {"status": "failed"}})
    assert prompt_delivery_block_reason({"generation_source": "static_fallback"})
    assert prompt_delivery_block_reason({"prompt_block": "legacy"}) is None
    assert prompt_delivery_block_reason({"quality_gate": {"status": "passed"}}) is None
