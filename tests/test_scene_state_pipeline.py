import prompt_pipeline as pp
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


def test_scene_state_rejects_removing_an_undeclared_object():
    beat = _beat(1, "clearing")
    beat["removed_objects"] = ["mystery cabinet"]
    errors = validate_scene_states(build_scene_states([beat]))
    assert any("undeclared object" in error for error in errors)


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


def test_render_gate_blocks_only_explicit_new_degraded_results():
    assert prompt_delivery_block_reason({"degraded": True})
    assert prompt_delivery_block_reason({"quality_gate": {"status": "failed"}})
    assert prompt_delivery_block_reason({"generation_source": "static_fallback"})
    assert prompt_delivery_block_reason({"prompt_block": "legacy"}) is None
    assert prompt_delivery_block_reason({"quality_gate": {"status": "passed"}}) is None
