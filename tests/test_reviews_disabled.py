"""Quality reviews are permanently retired, including legacy explicit false requests."""
from unittest.mock import patch
from types import SimpleNamespace
import json
from pathlib import Path

import pytest

import server_common as common
import prompt_pipeline as prompts
import video_generator as video
from frame_continuity import continuity_mode, continuity_max_retries
from pipeline_orchestrator import _sequence_consistency_review


@pytest.fixture(autouse=True)
def isolated_review_config(monkeypatch):
    monkeypatch.delenv('SPARK_REVIEWS_DISABLED', raising=False)
    with patch.dict(common.SERVER_CONFIG, {}, clear=True):
        yield


@pytest.mark.parametrize('managed', [False, True])
def test_master_overrides_all_gates_and_restores_preferences(managed):
    configured = {spec['key']: spec['default'] for spec in common.GATE_SETTINGS}
    configured.update(reviewsDisabled=True, strictGates=True,
                      frameContinuityMode='strict', frameContinuityMaxRetries=3)
    with patch.object(common, 'SERVER_MANAGED', managed):
        merged = common.effective_config(configured)
    assert common.reviews_disabled(merged)
    for spec in common.GATE_SETTINGS:
        key = spec['key']
        if key == 'reviewsDisabled':
            continue
        expected = {'bool': False, 'enum': 'off', 'int': 0}[spec['type']]
        assert common.gate_setting(key, merged) == expected, key
        assert merged[key] == expected
    assert continuity_mode(merged) == 'off'
    assert continuity_max_retries(merged) == 0
    merged['reviewsDisabled'] = False
    for spec in common.GATE_SETTINGS:
        if spec['key'] != 'reviewsDisabled':
            expected = {'bool': False, 'enum': 'off', 'int': 0}[spec['type']]
            assert common.gate_setting(spec['key'], merged) == expected


def test_server_master_can_be_explicitly_disabled_for_one_request():
    with patch.dict(common.SERVER_CONFIG, {'reviewsDisabled': True}):
        assert common.qa_gate_level({}) == 'off'
        assert common.qa_gate_level({'reviewsDisabled': False}) == 'off'
        assert common.reviews_disabled({'reviewsDisabled': 'false'})


def test_registry_report_retains_preferences_while_master_is_on():
    with patch.dict(common.SERVER_CONFIG, {
        'reviewsDisabled': True, 'chainGuardMode': 'halt', 'videoAnchorVerify': True,
    }):
        report = {row['key']: row for row in common.gate_settings_report()}
    assert report['reviewsDisabled']['server_value'] is True
    assert report['chainGuardMode']['server_value'] == 'off'
    assert report['videoAnchorVerify']['server_value'] is False
    assert all(row['retired'] and not row['editable'] for row in report.values())


def test_video_checks_skip_local_probes_and_vlm():
    config = {'reviewsDisabled': True, 'strictGates': True}
    with patch.object(video, 'detect_pace_break') as pace, \
         patch.object(video, 'detect_frozen_clip') as frozen, \
         patch.object(video, '_extract_video_mid_frames') as extract, \
         patch.object(prompts, '_multimodal_chat') as chat:
        assert video.check_video_process(config, 'clip', 'before', 'after', 'prompt')[0] == 'accept'
        assert video.verify_video_anchors('clip', 'before', 'after', config=config)[0]
        assert prompts.run_video_process_check(config, 'before', ['middle'], 'after', 'prompt')[0]
        for probe in (pace, frozen, extract, chat):
            probe.assert_not_called()


def test_full_review_skips_without_recording_a_pass_or_touching_manifest():
    events = []
    config = {'reviewsDisabled': True}
    with patch('pipeline_orchestrator.invalidate_stale_review_verdicts') as invalidate, \
         patch.object(prompts, '_multimodal_chat') as chat:
        assert _sequence_consistency_review(
            config, 'project', 'prompt', '/missing',
            on_progress=lambda stage, data: events.append((stage, data))) == 'prompt'
        result = prompts.check_full_sequence_consistency(config, 'prompt', {1: 'a', 2: 'b'})
        assert result['skipped']
        assert not result['global_reviewed']
        assert not result['global_attempted']
        assert prompts.check_anchor_consistency(config, 'prompt', 'a') is None
        assert prompts.check_beat_consistency(config, 'prompt', 1, 1, 'a', 'b') is None
        assert prompts.check_global_sequence_consistency(config, 'prompt', {1: 'a', 2: 'b'}) is None
        assert prompts.check_collage_macro_alignment(config, 'a', 'b') is None
        assert prompts._verify_review_violation(config, 'claim', ['a', 'b']) is None
        invalidate.assert_not_called()
        chat.assert_not_called()
    assert events[0][1]['skipped']
    assert not events[0][1]['passed']


@pytest.mark.parametrize('quality', ['sequence_review_flagged', 'frame_continuity_failed',
                                    'manual_flagged', 'i2i_fallback_degraded'])
def test_video_plan_ignores_review_flags_but_keeps_missing_file_checks(tmp_path, quality):
    frames = {}
    for seq in (1, 2):
        frame = tmp_path / f'{seq}.webp'
        frame.write_bytes(b'image')
        frames[seq] = str(frame)
    config = {'reviewsDisabled': True}
    with patch.object(video, 'frame_pair_contract') as pair:
        plan = video.plan_video_slots({1: 'motion'}, frames, {1: quality}, str(tmp_path),
                                     stale_slots={1}, config=config)[0]
        assert plan['action'] == 'generate'
        assert plan['reviews_skipped']
        assert not plan.get('warning')
        pair.assert_not_called()
        (tmp_path / '2.webp').unlink()
        plan = video.plan_video_slots({1: 'motion'}, frames, {}, str(tmp_path), config=config)[0]
        assert plan['action'] == 'blocked'


def test_video_resume_does_not_verify_or_replace_existing_video(tmp_path):
    existing = tmp_path / 'vid_001.mp4'
    existing.write_bytes(b'video')
    with patch.object(video, 'verify_video_anchors') as verify:
        plan = video.plan_video_slots({1: 'motion'}, {}, {}, str(tmp_path),
                                     config={'reviewsDisabled': True})[0]
        assert plan['action'] == 'reuse'
        assert not plan['delete_existing']
        verify.assert_not_called()


def test_candidate_selection_preserves_candidates_without_fake_scores():
    import candidate_selection_pipeline as candidates
    with patch.object(candidates, '_multimodal_chat') as chat:
        result = candidates.evaluate_and_select_best_candidate(
            {'reviewsDisabled': True}, 'prompt', None, ['a', 'b', 'c', 'd'], 1)
        chat.assert_not_called()
    assert result['review_skipped']
    assert result['best_index'] == 1
    assert len(result['candidates']) == 4
    assert all(candidate['score'] is None for candidate in result['candidates'])


def test_legacy_prompt_quality_metadata_does_not_block_disabled_reviews():
    from server import prompt_delivery_block_reason
    payload = {'quality_gate': {'status': 'failed'}, 'degraded': True,
               'config': {'reviewsDisabled': True}}
    assert prompt_delivery_block_reason(payload) is None
    payload['config']['reviewsDisabled'] = False
    assert prompt_delivery_block_reason(payload) is None


@pytest.mark.parametrize('partial', [False, True])
def test_merge_skips_anchor_and_pace_reviews_but_still_merges(tmp_path, partial):
    videos_dir = tmp_path / 'videos'
    videos_dir.mkdir()
    clip = videos_dir / 'vid_001.mp4'
    clip.write_bytes(b'clip')
    manifest = {
        'title': 'review-switch-test',
        'frames': [{'slot': seq} for seq in range(1, 4 if partial else 3)],
        'videos': [{'slot': 1, 'status': 'success', 'file': str(clip)}],
    }
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def fake_run(command, **kwargs):
        if command[0] == 'ffprobe':
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'streams': [{'codec_type': 'video', 'duration': '1.25'}],
                'format': {'duration': '1.25'}}), stderr='')
        Path(command[-1]).write_bytes(b'merged')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    with patch.object(video, 'verify_video_anchors') as anchors, \
         patch.object(video, 'retime_clips_for_merge') as pace, \
         patch.object(video, '_probe_merge_clip', return_value={
             'width': 160, 'height': 120, 'fps': 24.0, 'duration': 5.0, 'has_audio': False}), \
         patch.object(video.subprocess, 'run', side_effect=fake_run):
        merged = video.merge_project_videos(
            str(tmp_path), allow_partial=partial, cover_burn=False,
            config={'reviewsDisabled': True})
        assert merged['status'] == 'success'
        assert bool(merged.get('partial')) == partial
        anchors.assert_not_called()
        pace.assert_not_called()
