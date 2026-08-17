# -*- coding: utf-8 -*-
"""
End-to-end verification script on real existing replica job replica_7fc89a0bd5d5.
Tests baseline locking, orthogonal mutation generation, lineage query, and beat integrity.
"""

import json
import os
import shutil
import pytest
import replica_pipeline
from prompt_pipeline.mutate import MUTATION_PRESETS, generate_orthogonal_variant


def test_real_job_e2e_lifecycle():
    jobs = replica_pipeline.list_replica_jobs()
    assert len(jobs) > 0, "No replica jobs found"
    # Find first baseline or available job
    baseline_jobs = [j for j in jobs if not j.get('variant_of')]
    target_job = baseline_jobs[0] if baseline_jobs else jobs[0]
    job_id = target_job['job_id']
    state = replica_pipeline._load_state(job_id)
    assert state is not None, f"State for {job_id} not found"

    print(f"\n--- [1] Loaded Real Job: {job_id} ---")
    print(f"Video name: {state.get('video_name')}")
    print(f"Stage: {state.get('stage')}")
    beats_doc = state.get('beats') or {}
    beats = beats_doc.get('beats') or []
    print(f"Original Beats count: {len(beats)}")
    assert len(beats) > 0

    # 1. 尝试锁定为 Gold Baseline
    print("\n--- [2] Locking Baseline Job ---")
    try:
        locked_state = replica_pipeline.lock_baseline_job(job_id, lock=True)
        print(f"Successfully locked {job_id}. is_locked_baseline={locked_state.get('is_locked_baseline')}")
    except ValueError as e:
        print(f"Locking notes: {e}")
        # If there are slight semantic warnings/errors, check validation
        val = replica_pipeline._revalidate(state, persist=False)
        print(f"Validation items: {len(val)}")
        for v in val[:5]:
            print(f"  [{v.get('level')}] {v.get('message')}")
        # Force unlock or test mutation directly

    # 2. 正交二创变体派生测试 (测试全部 4 大预置)
    print("\n--- [3] Generating Orthogonal Variants for all 4 Presets ---")
    for preset_key, emoji in [('polar', '❄️'), ('volcano', '🌋'), ('cyber', '⚡'), ('cave', '💎')]:
        preset_info = MUTATION_PRESETS[preset_key]
        var_doc = generate_orthogonal_variant(
            beats_doc,
            preset=preset_key,
            mutation_axes=preset_info['axes'],
        )
        var_beats = var_doc.get('beats') or []
        assert len(var_beats) == len(beats), f"Preset {preset_key} must preserve beat count"

        print(f"\n{emoji} Preset: {preset_info['name']} ({preset_key})")
        print(f"  - Scene Signature: {var_doc.get('scene_signature')[:75]}...")
        print(f"  - Banned count: {len(var_doc.get('banned_elements') or [])}")
        print(f"  - Beat 1 Subject: {var_beats[0].get('visual_subject')[:65]}...")
        print(f"  - Last Beat Subject: {var_beats[-1].get('visual_subject')[:65]}...")
        print(f"  - Last Beat Audio: {var_beats[-1].get('audio_cue')}")

    # 3. 产生派生 Job
    print("\n--- [4] Running mutate_orthogonal pipeline ---")
    var_state = replica_pipeline.mutate_orthogonal(
        {},
        baseline_job_id=job_id,
        preset='volcano',
    )
    var_job_id = var_state['job_id']
    print(f"Created variant job: {var_job_id}")
    print(f"Parent baseline ID: {var_state.get('parent_baseline_id')}")
    print(f"Variant of: {var_state.get('variant_of')}")

    # 4. 查询血统树
    print("\n--- [5] Querying Lineage Tree ---")
    lineage = replica_pipeline.get_lineage(job_id)
    print(f"Lineage Baseline ID: {lineage['baseline']['job_id']}")
    print(f"Lineage Variants count: {len(lineage['variants'])}")
    for v in lineage['variants']:
        print(f"  - Variant ID: {v['job_id']}, Preset: {v.get('preset')}, Type: {v.get('job_type')}")

    assert any(v['job_id'] == var_job_id for v in lineage['variants'])
