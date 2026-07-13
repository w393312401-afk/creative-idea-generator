"""Autonomous staged pipelines for the restoration-prompt-composer production app.

Three entry points, sharing the same render/gate/recovery machinery:

- render_and_gate_single_frame: render ONE frame and run it through the Anchor
  Acceptance Gate, synchronously, returning the verdict directly (no task_id/polling).
  This is what makes staging real for a CONVERSATIONAL skill invocation: an agent
  following SKILL.md's Steps 1-11 directly in chat calls this (via /api/render_anchor,
  see server.py) mid-turn, right after composing IMAGE 1's prompt and before composing
  anything else, and only continues once it gets a real pass/fail back.

- run_autonomous_pipeline: dimensions in, everything out. Composes IMAGE 1's prompt
  itself (compose_anchor_and_packet), gates it via render_and_gate_single_frame, refines
  the Drift Lock packet against the accepted render, then composes and renders the rest
  (compose_remaining_beats). Used by server.py's /api/auto_run — the GUI/API-driven,
  dimensions-first path.

- run_staged_frame_rendering: an ALREADY-composed prompt_block in, staged rendering
  out. For the case where an agent already wrote the full IMAGE/VIDEO prompt text
  (typically AFTER already gating IMAGE 1 itself via render_and_gate_single_frame mid-
  turn) and now wants the rest rendered. If IMAGE 1 is already 'auto_approved' in
  manifest.json, this skips re-gating it — otherwise it gates it here too, so calling
  this endpoint alone (without a prior render_and_gate_single_frame call) still stages
  correctly. Used by server.py's /api/render_staged, which scripts/generate_frames.py
  calls instead of the old one-shot /api/generate_frames.

All three paths end with (or lead into) the same autonomous recovery passes over any
leftover 'vlm_qa_failed' frame and any rejected/blocked video clip, so nothing ever
dead-ends in a manual-review state.
"""
import os

from server_common import _get_project_dir
from manifest_store import QualityGate, load_manifest, get_frame_quality_gate, set_frame_quality_gate
from prompt_pipeline import (
    compose_anchor_and_packet,
    compose_remaining_beats,
    check_anchor_frame_compliance,
    refine_packet_from_accepted_anchor,
    fix_image_prompt_with_vlm_feedback,
    run_frame_qa_check,
    _format_prompt_block,
    _parse_prompt_slots,
    prompt_slots_list,
)
from frame_generator import generate_frame_sequence
from video_generator import generate_video_sequence

_MAX_ANCHOR_ATTEMPTS = 3
_MAX_RECOVERY_ATTEMPTS = 2


def _frame_path(title, sequence):
    return os.path.join(_get_project_dir(title), 'frames', f'img_{sequence:03d}.webp')


def _retry_frame_until_pass(config, title, sequence, images, videos, judge, on_progress=None,
                             max_attempts=_MAX_ANCHOR_ATTEMPTS):
    """Render `sequence` from `images`/`videos` (same shape _parse_prompt_slots returns),
    ask `judge(image_path, prompt) -> (passed, reason)`, and on failure correct that
    slot's prompt via fix_image_prompt_with_vlm_feedback and re-render. Mutates
    images[sequence] in place with the final prompt actually used. Shared by the frame-1
    Anchor Acceptance Gate and the post-render autonomous recovery pass over any
    leftover 'vlm_qa_failed' frame.
    Returns (passed: bool, reason: str)."""
    item = images[sequence]
    prompt = item['body'] if isinstance(item, dict) else item
    meta = item.get('meta', '') if isinstance(item, dict) else ''
    reason = None
    for attempt in range(1, max_attempts + 1):
        images[sequence] = {'body': prompt, 'meta': meta}
        prompt_block = _format_prompt_block(images, videos)
        generate_frame_sequence(config, title, prompt_block, on_progress=on_progress, target_sequences=[sequence])
        passed, reason = judge(_frame_path(title, sequence), prompt)
        if on_progress:
            on_progress('anchor_check', {'sequence': sequence, 'attempt': attempt, 'passed': passed, 'reason': reason})
        if passed:
            return True, reason
        if attempt < max_attempts:
            if on_progress:
                on_progress('anchor_retry', {'sequence': sequence, 'attempt': attempt, 'reason': reason})
            prompt = fix_image_prompt_with_vlm_feedback(config, prompt, reason)
    return False, reason


def render_and_gate_single_frame(config, title, sequence, prompt, meta='', judge=None, on_progress=None):
    """Render exactly one frame and run it through an acceptance gate synchronously,
    returning the final verdict directly (no task_id/polling) so a caller — including a
    conversational agent mid-turn — can decide what to do next before composing anything
    else. Defaults to the Anchor Acceptance Gate judged purely from the prompt text
    (no separate Drift Lock packet available); callers with a real packet/parsed_brief
    (run_autonomous_pipeline) pass their own `judge`.
    Returns {'status': 'auto_approved'|'needs_human_review', 'reason', 'prompt',
    'image_path', 'project_dir'}."""
    if judge is None:
        def judge(image_path, current_prompt):
            return check_anchor_frame_compliance(config, image_path, current_prompt, {}, {})

    images = {sequence: {'body': prompt, 'meta': meta}}
    passed, reason = _retry_frame_until_pass(config, title, sequence, images, {}, judge, on_progress=on_progress)
    project_dir = _get_project_dir(title)
    status = QualityGate.AUTO_APPROVED if passed else QualityGate.NEEDS_HUMAN_REVIEW
    set_frame_quality_gate(project_dir, sequence, status, reason)
    return {
        'status': status,
        'reason': reason,
        'prompt': images[sequence]['body'],
        'image_path': _frame_path(title, sequence),
        'project_dir': project_dir,
    }


def _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=None):
    """Autonomous recovery pass: retry any frame still stuck at 'vlm_qa_failed' after
    the main render pass's own retries, using the same judge-and-fix loop as the Anchor
    Acceptance Gate but checking frame-to-frame motion continuity (run_vlm_qa_check)
    instead of tone/rule compliance. Returns the prompt_block, re-formatted only if a
    recovered prompt actually changed."""
    images, videos = _parse_prompt_slots(prompt_block)
    manifest = load_manifest(project_dir)
    if not manifest:
        return prompt_block
    failed_seqs = [f['sequence'] for f in manifest.get('frames', []) if f.get('quality_gate') == QualityGate.VLM_QA_FAILED]
    for seq in failed_seqs:
        video_item = videos.get(seq - 1)
        video_prompt = video_item['body'] if isinstance(video_item, dict) else (video_item or '')
        item = images.get(seq) or {}
        is_bridge = 'BRIDGE' in (item.get('meta', '') if isinstance(item, dict) else '').upper()

        def frame_judge(image_path, _prompt, _seq=seq, _video_prompt=video_prompt, _is_bridge=is_bridge):
            if not _video_prompt:
                return True, 'no video prompt to check against'
            prev_path = _frame_path(title, _seq - 1)
            image_1_path = _frame_path(title, 1)
            return run_frame_qa_check(config, image_1_path, prev_path, image_path, _video_prompt, _seq, is_bridge=_is_bridge)

        passed, reason = _retry_frame_until_pass(
            config, title, seq, images, videos, frame_judge,
            on_progress=on_progress, max_attempts=_MAX_RECOVERY_ATTEMPTS,
        )
        set_frame_quality_gate(project_dir, seq, QualityGate.AUTO_APPROVED if passed else QualityGate.VLM_QA_FAILED, reason)
    if failed_seqs:
        prompt_block = _format_prompt_block(images, videos)
    return prompt_block


def _render_videos_with_recovery(config, title, prompt_block, on_progress=None):
    """Render all videos, then run one autonomous retry pass over any slot that came
    back rejected/blocked (e.g. a failed Google FX anchor-match) instead of leaving it
    for a human to notice and re-trigger manually."""
    video_result = generate_video_sequence(config, title, prompt_block, on_progress=on_progress)
    failed_slots = [v['slot'] for v in video_result.get('videos', []) if v.get('status') != 'success']
    if failed_slots:
        if on_progress:
            on_progress('video_retry_autonomous', {'slots': failed_slots})
        video_result = generate_video_sequence(
            config, title, prompt_block, on_progress=on_progress, target_slots=failed_slots,
        )
    return video_result


def run_autonomous_pipeline(config, dimensions, on_progress=None):
    """Runs the full staged pipeline autonomously, composing its own prompt text.
    Returns a result dict with status='completed', or status='needs_human_review' if
    IMAGE 1 still fails the Anchor Acceptance Gate after _MAX_ANCHOR_ATTEMPTS retries —
    a rare escape hatch, not the routine path."""
    state = compose_anchor_and_packet(config, dimensions, on_progress=on_progress)
    title = state['title']

    def anchor_judge(image_path, prompt):
        return check_anchor_frame_compliance(config, image_path, prompt, state['packet'], state['parsed_brief'])

    gate = render_and_gate_single_frame(
        config, title, 1, state['image_1_prompt'], judge=anchor_judge, on_progress=on_progress,
    )
    state['image_1_prompt'] = gate['prompt']
    state['compiled_images'][1] = gate['prompt']

    if gate['status'] != 'auto_approved':
        if on_progress:
            on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
        return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': gate['project_dir']}

    if on_progress:
        on_progress('packet_refine_start', {'message': '正在依据已确认的首帧修正 Drift Lock 数据包...'})
    state['packet'] = refine_packet_from_accepted_anchor(config, gate['image_path'], state['packet'])
    if on_progress:
        on_progress('packet_refined', {'message': 'Drift Lock 数据包已依据实际渲染结果修正。'})

    project_dir = gate['project_dir']
    prompt_block = compose_remaining_beats(config, state, on_progress=on_progress)
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress, target_sequences=None)
    prompt_block = _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=on_progress)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }


def run_staged_frame_rendering(config, title, prompt_block, on_progress=None):
    """Runs staged, gated rendering over an ALREADY-composed prompt_block, without
    re-deriving any prompt text. If IMAGE 1 is already 'auto_approved' in manifest.json
    (typically because the calling agent already gated it inline via
    render_and_gate_single_frame / /api/render_anchor before composing the rest), this
    skips re-gating it — otherwise it gates it here, so this endpoint alone still stages
    correctly even if called without a prior single-frame gate call.
    Returns a result dict with status='completed' or 'needs_human_review'."""
    images, videos = _parse_prompt_slots(prompt_block)
    if 1 not in images:
        raise RuntimeError('未在 prompt_block 中找到 图片 1: 提示词，无法分步渲染')

    project_dir = _get_project_dir(title)

    if get_frame_quality_gate(project_dir, 1) == QualityGate.AUTO_APPROVED:
        if on_progress:
            on_progress('anchor_check', {'sequence': 1, 'attempt': 0, 'passed': True, 'reason': '此前已通过判定，跳过重复渲染'})
    else:
        item = images[1]
        prompt = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        gate = render_and_gate_single_frame(config, title, 1, prompt, meta=meta, on_progress=on_progress)
        images[1] = {'body': gate['prompt'], 'meta': meta}
        if gate['status'] != 'auto_approved':
            if on_progress:
                on_progress('needs_human_review', {'sequence': 1, 'reason': gate['reason']})
            return {'title': title, 'status': 'needs_human_review', 'reason': gate['reason'], 'project_dir': project_dir}

    prompt_block = _format_prompt_block(images, videos)
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress, target_sequences=None)
    prompt_block = _recover_failed_frames(config, title, prompt_block, project_dir, on_progress=on_progress)
    video_result = _render_videos_with_recovery(config, title, prompt_block, on_progress=on_progress)

    return {
        'title': title,
        'status': 'completed',
        'prompt_block': prompt_block,
        'prompt_slots': prompt_slots_list(prompt_block),
        'project_dir': project_dir,
        'videos': video_result,
    }
