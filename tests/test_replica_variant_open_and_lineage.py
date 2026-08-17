# -*- coding: utf-8 -*-
"""
Tests for replica variant opening, lineage resolution, and deletion cleanup.
"""

import json
import os
import shutil
import tempfile
import pytest

import replica_pipeline


@pytest.fixture
def temp_jobs_root(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="test_replica_jobs_")
    monkeypatch.setattr(replica_pipeline, "jobs_root", lambda: temp_dir)
    monkeypatch.setattr(replica_pipeline, "job_dir", lambda jid: os.path.join(temp_dir, jid))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_lineage_sync_and_variant_open(temp_jobs_root):
    # 1. Create a baseline job
    baseline_id = "replica_base001"
    b_dir = os.path.join(temp_jobs_root, baseline_id)
    os.makedirs(os.path.join(b_dir, "review_frames"), exist_ok=True)
    with open(os.path.join(b_dir, "review_frames", "frame_0001.png"), "w") as f:
        f.write("dummy frame")

    b_state = {
        "job_id": baseline_id,
        "stage": "completed",
        "job_type": "baseline",
        "lineage_variants": ["replica_var001", "replica_deleted001"],
        "video_name": "test.mp4",
        "title": "Baseline Video",
    }
    with open(os.path.join(b_dir, ".replica_pipeline.json"), "w", encoding="utf-8") as f:
        json.dump(b_state, f)

    # 2. Create a variant job
    var_id = "replica_var001"
    v_dir = os.path.join(temp_jobs_root, var_id)
    os.makedirs(v_dir, exist_ok=True)
    v_state = {
        "job_id": var_id,
        "stage": "review_beats",
        "job_type": "variant",
        "variant_of": baseline_id,
        "parent_baseline_id": baseline_id,
        "lineage_variants": [],
        "title": "Variant 1 - Volcano",
    }
    with open(os.path.join(v_dir, ".replica_pipeline.json"), "w", encoding="utf-8") as f:
        json.dump(v_state, f)

    # 3. Create a 2nd generation variant job (chained)
    var2_id = "replica_var002_nested"
    v2_dir = os.path.join(temp_jobs_root, var2_id)
    os.makedirs(v2_dir, exist_ok=True)
    v2_state = {
        "job_id": var2_id,
        "stage": "completed",
        "job_type": "variant",
        "variant_of": var_id,
        "parent_baseline_id": var_id,
        "lineage_variants": [],
        "title": "Variant 2 - Cyberpunk",
    }
    with open(os.path.join(v2_dir, ".replica_pipeline.json"), "w", encoding="utf-8") as f:
        json.dump(v2_state, f)

    # Test baseline status: phantom deleted variant should be filtered out, and real variants included
    b_status = replica_pipeline.get_replica_status(baseline_id)
    assert b_status is not None
    assert "replica_deleted001" not in b_status["lineage_variants"]
    assert "replica_var001" in b_status["lineage_variants"]

    # Test 1st generation variant: frame_urls should resolve to baseline frames
    v_status = replica_pipeline.get_replica_status(var_id)
    assert v_status is not None
    assert "frame_0001.png" in v_status["frame_urls"]
    assert f"/outputs/replica_jobs/{baseline_id}/review_frames/frame_0001.png" in v_status["frame_urls"]["frame_0001.png"]

    # Test 2nd generation variant: root ancestor search should resolve to baseline frames
    v2_status = replica_pipeline.get_replica_status(var2_id)
    assert v2_status is not None
    assert "frame_0001.png" in v2_status["frame_urls"]
    assert f"/outputs/replica_jobs/{baseline_id}/review_frames/frame_0001.png" in v2_status["frame_urls"]["frame_0001.png"]

    # Test delete cleanup: deleting var_id should update parent's lineage
    replica_pipeline.delete_replica_job(var_id, force=True)
    b_status_after = replica_pipeline.get_replica_status(baseline_id)
    assert "replica_var001" not in b_status_after["lineage_variants"]
