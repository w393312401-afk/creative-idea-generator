"""Shared manifest.json access layer + the QualityGate value vocabulary.

Before this, frame_generator.py, video_generator.py, and pipeline_orchestrator.py each
opened manifest.json with their own raw json.load/json.dump and their own ad-hoc frame
traversal, and a frame's 'quality_gate' field was a bare string literal written by several
independent call sites with no single place declaring which values are valid — the newest
value (NEEDS_HUMAN_REVIEW, written by pipeline_orchestrator's Anchor Acceptance Gate) was
never added to the frontend's badge/retry-affordance logic, so a run that permanently fails
that gate surfaces to the user as an ordinary "done" result with no distinct treatment. This
module is the single place new code should read/write manifest frame quality state from.

No dependency on prompt_pipeline/frame_generator/video_generator, so any of those (or
server.py) can import this without circular-import risk.
"""
import json
import os
from enum import Enum


class QualityGate(str, Enum):
    """Every value ever written to a manifest frame's 'quality_gate' field. A `str, Enum`
    member compares equal to its own .value (QualityGate.AUTO_APPROVED == 'auto_approved'
    is True), so existing string-literal comparisons keep working during an incremental
    migration away from raw literals."""
    AUTO_APPROVED = 'auto_approved'
    PENDING_MANUAL_REVIEW = 'pending_manual_review'
    VLM_QA_FAILED = 'vlm_qa_failed'
    I2I_FALLBACK_DEGRADED = 'i2i_fallback_degraded'
    NEEDS_HUMAN_REVIEW = 'needs_human_review'


def load_manifest(project_dir):
    """Read manifest.json for a project directory, or {} if it doesn't exist yet."""
    manifest_path = os.path.join(project_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return {}
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_manifest(project_dir, manifest):
    manifest_path = os.path.join(project_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def get_frame(manifest, sequence):
    """Look up a frame entry by its 'sequence' key, or None if absent."""
    for frame in manifest.get('frames', []):
        if frame.get('sequence') == sequence:
            return frame
    return None


def get_frame_quality_gate(project_dir, sequence):
    """Read a single frame's recorded quality_gate from manifest.json on disk, or None if
    no manifest/frame entry exists yet."""
    frame = get_frame(load_manifest(project_dir), sequence)
    return frame.get('quality_gate') if frame else None


def set_frame_quality_gate(project_dir, sequence, quality_gate, reason=None):
    """Read-modify-write one frame's quality_gate/vlm_qa_reason in manifest.json. A no-op
    if the manifest or that frame entry doesn't exist yet (nothing to overwrite)."""
    manifest = load_manifest(project_dir)
    frame = get_frame(manifest, sequence)
    if frame is None:
        return
    frame['quality_gate'] = quality_gate
    frame['vlm_qa_reason'] = reason
    save_manifest(project_dir, manifest)
