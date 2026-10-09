"""The visual-delta gate must use and preserve automatically paired anchors."""
from pathlib import Path
from unittest.mock import patch

import pytest

import prompt_pipeline as pp
import server_common
from prompt_pipeline.video_optimizer import optimize_video_prompts_for_sequence
from prompt_pipeline.video_optimizer import _persist_optimization_results
from video_generator import resolve_video_frame_anchors


@pytest.fixture
def project(tmp_path, monkeypatch):
    frames = tmp_path / 'frames'
    frames.mkdir()
    # Intentionally identical bytes: frame numbers must also invalidate caching.
    for sequence in (1, 2, 5, 8):
        (frames / f'img_{sequence:03d}.webp').write_bytes(b'identical-frame-content')
    monkeypatch.setattr(server_common, '_get_project_dir', lambda title: str(tmp_path))
    return tmp_path


def block(body, meta=''):
    return pp._format_prompt_block(
        {sequence: f'Image {sequence} prompt' for sequence in (1, 2, 5, 8)},
        {1: {'body': body, 'meta': meta}},
    )


AUTO_CONFIG = {'videoFramePairing': 'auto', 'optimizeVideoPromptsBeforeGen': True}


@pytest.mark.parametrize('video_repaired', [False, True])
def test_background_optimizer_preserves_concurrent_image_and_video_repairs(project, video_repaired):
    original = pp._format_prompt_block({1: 'old image', 2: 'next image'},
        {1: {'body': 'original action', 'meta': ''}, 2: {'body': 'other action', 'meta': ''}})
    optimized = pp._format_prompt_block({1: 'old image', 2: 'next image'},
        {1: {'body': 'optimized action', 'meta': ''}, 2: {'body': 'old unrelated action', 'meta': ''}})
    latest = pp._format_prompt_block({1: 'accepted image repair', 2: 'next image'},
        {1: {'body': 'accepted video repair' if video_repaired else 'original action', 'meta': ''},
         2: {'body': 'latest unrelated action', 'meta': ''}})
    server_common.write_manifest(str(project), {'prompt_block': latest,
        'frames': [{'sequence': 2}], 'video_prompt_optimizations': {'2': {'fingerprint': 'new'}}})
    result = _persist_optimization_results(str(project), 'test', optimized,
        {'1': {'fingerprint': 'optimized'}, '2': {'fingerprint': 'old'}},
        original_prompt_block=original, target_slots=[1])
    images, videos = pp._parse_prompt_slots(result)
    assert images[1]['body'] == 'accepted image repair'
    assert videos[1]['body'] == ('accepted video repair' if video_repaired else 'optimized action')
    assert videos[2]['body'] == 'latest unrelated action'
    manifest = server_common.read_manifest(str(project))
    assert manifest['frames'] == [{'sequence': 2}]
    assert manifest['video_prompt_optimizations']['2']['fingerprint'] == 'new'
    assert ('1' in manifest['video_prompt_optimizations']) is not video_repaired


@pytest.mark.parametrize('start,end', [(5, 8), (8, 5)])
def test_optimizer_reads_declared_files_and_retains_mapping_after_rewrite(project, start, end):
    original = block(f'Use IMAGE {start} as first frame and IMAGE {end} as last frame. Build a floor.')
    events = []
    # Simulate a VLM losing the mapping entirely.
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               return_value='The worker pours concrete.') as optimize:
        result = optimize_video_prompts_for_sequence(
            AUTO_CONFIG, 'auto-pair-test', original,
            on_progress=lambda stage, details: events.append((stage, details)),
        )
    call = optimize.call_args.kwargs
    assert Path(call['start_frame_path']).name == f'img_{start:03d}.webp'
    assert Path(call['end_frame_path']).name == f'img_{end:03d}.webp'
    assert (call['start_seq'], call['end_seq']) == (start, end)
    _, videos = pp._parse_prompt_slots(result)
    pair = resolve_video_frame_anchors(1, videos[1], AUTO_CONFIG)
    assert (pair['start_anchor_slot'], pair['end_anchor_slot']) == (start, end)
    assert 'The worker pours concrete.' in videos[1]['body']
    record = server_common.read_manifest(str(project))['video_prompt_optimizations']['1']
    assert (record['start_anchor_slot'], record['end_anchor_slot']) == (start, end)
    assert record['start_frame'] == f'img_{start:03d}.webp'
    assert record['end_frame'] == f'img_{end:03d}.webp'
    assert record['frame_pairing_source'] == 'prompt_declaration'
    assert any(stage == 'video_optimization_slot' for stage, _ in events)


def test_header_pairing_wins_and_wrong_vlm_opening_is_replaced(project):
    original = block('Use IMAGE 1 as first frame and IMAGE 2 as last frame. Build a floor.',
                     meta='IMAGE 8 → IMAGE 5')
    wrong_result = (
        'Use the provided first frame and last frame as exact composition anchors. '
        'Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; '
        'the worker pours concrete.'
    )
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               return_value=wrong_result) as optimize:
        result = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', original)
    assert (optimize.call_args.kwargs['start_seq'], optimize.call_args.kwargs['end_seq']) == (8, 5)
    _, videos = pp._parse_prompt_slots(result)
    assert 'Use IMAGE 8 as the actual first-frame image and IMAGE 5 as the actual last-frame image.' in videos[1]['body']
    assert 'Use IMAGE 1 as' not in videos[1]['body']
    assert 'the worker pours concrete.' in videos[1]['body']


@pytest.mark.parametrize('meta,body', [
    ('', 'Use the provided reference image (IMAGE 8) as the sole starting-frame anchor. Reveal the room.'),
    ('HERO', 'Use IMAGE 8 as first frame and IMAGE 5 as last frame. Reveal the room.'),
])
def test_single_frame_clips_skip_two_frame_visual_delta(project, meta, body):
    original = block(body, meta)
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt') as optimize:
        result = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', original)
    assert result == original
    optimize.assert_not_called()


def test_cache_keeps_pair_and_invalidates_changed_ids_with_identical_bytes(project):
    first = block('Use IMAGE 5 as first frame and IMAGE 8 as last frame. Build a floor.')
    swapped = block('Use IMAGE 8 as first frame and IMAGE 5 as last frame. Build a floor.')
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               return_value='The worker pours concrete.') as optimize:
        optimized = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', first)
        assert optimize.call_count == 1
        cached = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', optimized)
        assert cached == optimized
        assert optimize.call_count == 1
        changed = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', swapped)
        assert optimize.call_count == 2
    _, videos = pp._parse_prompt_slots(changed)
    pair = resolve_video_frame_anchors(1, videos[1], AUTO_CONFIG)
    assert (pair['start_anchor_slot'], pair['end_anchor_slot']) == (8, 5)


def test_manifest_auto_mode_survives_retry_without_config_override(project):
    server_common.write_manifest(str(project), {'video_frame_pairing': 'auto', 'frames': []})
    original = block('Use IMAGE 8 as first frame and IMAGE 5 as last frame. Build a floor.')
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               return_value='The worker pours concrete.') as optimize:
        optimize_video_prompts_for_sequence({}, 'auto-pair-test', original)
    assert (optimize.call_args.kwargs['start_seq'], optimize.call_args.kwargs['end_seq']) == (8, 5)


def test_explicit_legacy_mode_keeps_adjacent_images(project):
    server_common.write_manifest(str(project), {'video_frame_pairing': 'auto', 'frames': []})
    original = block('Use IMAGE 8 as first frame and IMAGE 5 as last frame. Build a floor.')
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               return_value='The worker pours concrete.') as optimize:
        optimize_video_prompts_for_sequence({'videoFramePairing': 'adjacent'}, 'auto-pair-test', original)
    assert (optimize.call_args.kwargs['start_seq'], optimize.call_args.kwargs['end_seq']) == (1, 2)


def test_missing_declared_frame_does_not_fall_back_to_available_adjacent_pair(project):
    (project / 'frames' / 'img_008.webp').unlink()
    original = block('Use IMAGE 5 as first frame and IMAGE 8 as last frame. Build a floor.')
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt') as optimize:
        result = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', original)
    assert result == original
    optimize.assert_not_called()


def test_vlm_fallback_keeps_actions_next_to_arrow_declaration(project):
    # The optimizer returns its input on a service error. Normalizing that input
    # must keep work instructions that share a sentence with the frame mapping.
    original = block('placeholder').replace(
        'placeholder', 'IMAGE 8 → IMAGE 5, the worker pours concrete and levels the floor.',
    )
    with patch('prompt_pipeline.video_optimizer.optimize_single_video_prompt',
               side_effect=lambda **kwargs: kwargs['original_video_prompt']):
        result = optimize_video_prompts_for_sequence(AUTO_CONFIG, 'auto-pair-test', original)
    _, videos = pp._parse_prompt_slots(result)
    assert 'the worker pours concrete and levels the floor.' in videos[1]['body']
    pair = resolve_video_frame_anchors(1, videos[1], AUTO_CONFIG)
    assert (pair['start_anchor_slot'], pair['end_anchor_slot']) == (8, 5)
