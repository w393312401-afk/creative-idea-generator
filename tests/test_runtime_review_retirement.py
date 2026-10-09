"""Retired quality rules cannot restart through legacy runtime entry points."""
from unittest.mock import Mock

import candidate_selection_pipeline as candidates
import chain_guard
import frame_continuity
import frame_generator
import pipeline_orchestrator as pipeline
import server_common
import stepped_pipeline as stepped


LEGACY_CONFIG = {
    'reviewsDisabled': False, 'chainGuardMode': 'autofix',
    'frameContinuityMode': 'strict', 'frameContinuityMaxRetries': 3,
    'optimizeVideoPromptsBeforeGen': True,
}
PROMPT = 'IMAGE 1: empty shell\nVIDEO 1: fit board\nIMAGE 2: fitted board\n'


def test_legacy_sequence_review_does_not_read_or_call_models(monkeypatch):
    check = Mock(side_effect=AssertionError('review model called'))
    read = Mock(side_effect=AssertionError('manifest read for review'))
    monkeypatch.setattr(pipeline, 'check_full_sequence_consistency', check)
    monkeypatch.setattr(pipeline, 'read_manifest', read)
    events = []
    result = pipeline._sequence_consistency_review(
        LEGACY_CONFIG, 'project', PROMPT, '/missing',
        on_progress=lambda *event: events.append(event), full=True)
    assert result == PROMPT
    assert events[-1][1]['retired'] is True
    assert events[-1][1]['passed'] is None
    check.assert_not_called()
    read.assert_not_called()


def test_direct_guards_cannot_restart_from_explicit_mode(monkeypatch):
    chat = Mock(side_effect=AssertionError('review model called'))
    monkeypatch.setattr(chain_guard, '_multimodal_chat', chat)
    monkeypatch.setattr(chain_guard, 'check_beat_consistency', chat)
    monkeypatch.setattr(chain_guard, 'check_anchor_consistency', chat)
    for result in (
        chain_guard.guard_beat(LEGACY_CONFIG, 'p', PROMPT, 1, '/missing'),
        chain_guard.guard_anchor(LEGACY_CONFIG, 'p', PROMPT, '/missing'),
        chain_guard.run_anchor_guard(LEGACY_CONFIG, 'p', PROMPT, '/missing',
                                     guard_mode='autofix', forward_build=True),
    ):
        assert result['verdict'] == 'retired'
        assert result['halt'] is False
    assert chain_guard.classify_chain_impact(LEGACY_CONFIG, ['old warning']) == []
    assert chain_guard.guard_autofix_enabled('autofix') is False
    assert chain_guard.guard_halt_enabled('halt') is False
    chat.assert_not_called()


def test_direct_local_continuity_never_reads_images(monkeypatch):
    pixels = Mock(side_effect=AssertionError('retired pixel review called'))
    monkeypatch.setattr(frame_continuity, '_pair_metrics', pixels)
    assert frame_continuity.continuity_mode(LEGACY_CONFIG) == 'off'
    assert frame_continuity.continuity_max_retries(LEGACY_CONFIG) == 0
    result = frame_continuity.analyze_frame('/missing/a', '/missing/b', mode='strict')
    assert result['status'] == 'retired'
    assert frame_continuity.measure_seam('/missing/a', '/missing/b', mode='strict') is None
    pixels.assert_not_called()


def test_candidate_generation_keeps_options_without_paid_scoring(monkeypatch):
    chat = Mock(side_effect=AssertionError('candidate review called'))
    monkeypatch.setattr(candidates, '_multimodal_chat', chat)
    result = candidates.evaluate_and_select_best_candidate(
        LEGACY_CONFIG, 'prompt', None, ['/a', '/b'], 1)
    assert result['retired'] is True
    assert result['best_index'] == 1
    assert [item['score'] for item in result['candidates']] == [None, None]
    assert all(item['score_source'] == 'review_retired' for item in result['candidates'])
    chat.assert_not_called()


def test_fix_verification_and_triptych_are_retired(monkeypatch):
    verify = Mock(side_effect=AssertionError('fix re-review called'))
    monkeypatch.setattr(pipeline, '_verify_review_violation', verify)
    monkeypatch.setattr(frame_continuity, 'measure_seam', verify)
    result = pipeline._reverify_frame_issues(
        LEGACY_CONFIG, 'project', 1, [{'text': 'old warning'}])
    assert result['retired'] is True and result['reviewed'] is False
    assert result['resolved'] == []
    assert pipeline._measure_fix_triptych(LEGACY_CONFIG, 'project', {}, {}, 1)['retired']
    verify.assert_not_called()


def test_user_directed_fix_keeps_result_and_undo_without_review_or_rollback(monkeypatch, tmp_path):
    project = tmp_path / 'project'
    frames = project / 'frames'
    frames.mkdir(parents=True)
    (frames / 'img_002.webp').write_bytes(b'original synthetic asset')
    monkeypatch.setattr(pipeline, '_get_project_dir', lambda title: str(project))
    server_common.write_manifest(str(project), {'title': 'p', 'frames': [{
        'sequence': 2, 'file': 'frames/img_002.webp',
        'quality_gate': 'sequence_review_flagged', 'vlm_qa_reason': 'old machine verdict'}]})
    rewrite = Mock(return_value=('requested transition', 'requested image state'))
    monkeypatch.setattr(pipeline, 'fix_beat_from_sequence_review', rewrite)
    def generate(*args, **kwargs):
        (frames / 'img_002.webp').write_bytes(b'user requested new asset')
    render = Mock(side_effect=generate)
    monkeypatch.setattr(frame_generator, 'generate_frame_sequence', render)
    blocked = Mock(side_effect=AssertionError('retired review or rollback called'))
    monkeypatch.setattr(pipeline, '_measure_fix_triptych', blocked)
    monkeypatch.setattr(pipeline, '_verify_review_violation', blocked)
    monkeypatch.setattr(frame_continuity, 'compare_triptych', blocked)
    monkeypatch.setattr(pipeline, 'undo_frame_fix', blocked)
    monkeypatch.setattr('tools.collage.build_keyframe_collage', lambda *a, **kw: None)
    result = pipeline.fix_frame_issue(
        dict(LEGACY_CONFIG, candidateSelection=False), 'p', PROMPT, 2,
        manual_reason='Change only the lamp to off')
    assert rewrite.call_args.args[3] == ['Change only the lamp to off']
    assert result['rolled_back'] is False and result['undoable'] is True
    assert result['triptych']['verdict'] == 'retired'
    assert result['reverify']['reviewed'] is False
    assert (frames / 'img_002.webp').read_bytes() == b'user requested new asset'
    blocked.assert_not_called()
    render.assert_called_once()


def _stepped_rig(monkeypatch, tmp_path):
    monkeypatch.setattr(stepped, '_get_project_dir', lambda title: str(tmp_path))
    monkeypatch.setattr(stepped, '_save_state', lambda *args, **kwargs: None)
    monkeypatch.setattr(stepped, '_enrich_state_with_refs', lambda state, *args: state)
    monkeypatch.setattr(stepped, 'persist_outline_delivery_ledger', lambda *args, **kw: None)
    monkeypatch.setattr(stepped, '_generate_batch_collage', lambda *args: None)
    monkeypatch.setattr(stepped, '_generate_full_collage', lambda *args: None)
    monkeypatch.setattr(stepped, 'compose_remaining_beats', lambda *args, **kw: PROMPT)
    batches = []
    monkeypatch.setattr(stepped, '_render_batch',
                        lambda config, title, block, sequences, cb: batches.append(sequences))
    optimizer = Mock(side_effect=AssertionError('retired prompt optimizer called'))
    monkeypatch.setattr(stepped, 'optimize_video_prompts_for_sequence', optimizer)
    return batches, optimizer


def test_stepped_retired_anchor_pause_runs_all_batches_without_approval(monkeypatch, tmp_path):
    batches, optimizer = _stepped_rig(monkeypatch, tmp_path)
    state = {'title': 'p', 'stage': 'review_anchor', 'image_1_prompt': 'shell',
             'packet': {}, 'beat_ladder': [], 'batches': [
                 {'sequences': [2], 'status': 'queued'},
                 {'sequences': [3], 'status': 'queued'}]}
    monkeypatch.setattr(stepped, '_load_state', lambda title: state)
    video = Mock(side_effect=AssertionError('frame-only task generated video'))
    monkeypatch.setattr(stepped, '_render_videos_with_recovery', video)
    result = stepped.advance_stepped_pipeline(
        'p', config=dict(LEGACY_CONFIG, _auto_generate_videos=False))
    assert result['stage'] == 'completed'
    assert result['quality_review_retired'] is True
    assert batches == [[2], [3]]
    assert [item['status'] for item in state['batches']] == ['rendered', 'rendered']
    assert stepped.REVIEW_STAGES == set()
    optimizer.assert_not_called()
    video.assert_not_called()


def test_old_final_pause_uses_original_prompt_and_retains_provider_failure(monkeypatch, tmp_path):
    _, optimizer = _stepped_rig(monkeypatch, tmp_path)
    state = {'title': 'p', 'stage': 'final_review', 'prompt_block': PROMPT}
    monkeypatch.setattr(stepped, '_load_state', lambda title: state)
    render = Mock(return_value={'videos': [{'slot': 1, 'status': 'failed'}]})
    monkeypatch.setattr(stepped, '_render_videos_with_recovery', render)
    monkeypatch.setattr(stepped, '_flow_video_outcome', lambda *a: {
        'completion_state': 'partial_failed', 'has_failures': True})
    result = stepped.advance_stepped_pipeline('p', config=LEGACY_CONFIG)
    assert result['has_failures'] is True
    assert result['completion_state'] == 'partial_failed'
    assert render.call_args.args[2] == PROMPT
    optimizer.assert_not_called()


def test_historical_quality_flags_do_not_reopen_pipeline_warnings():
    result = pipeline._flow_video_outcome(
        dict(LEGACY_CONFIG, videoProvider='flow2api'), {'videos': [{
            'slot': 1, 'status': 'success', 'process_warned': True,
            'anchor_mismatch_overridden': True}]}, PROMPT)
    assert result['completion_state'] == 'completed'
    assert result['has_quality_warnings'] is False
