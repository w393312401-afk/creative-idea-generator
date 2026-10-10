"""Automatic I2V pairing uses declared images without changing legacy projects."""

import json
import re

import pytest

import video_generator as vg


AUTO = {'videoFramePairing': 'auto'}


@pytest.mark.parametrize('item,expected,location', [
    ({'body': 'Use IMAGE 3 as first frame and IMAGE 9 as last frame.'}, (3, 9), 'body'),
    ({'body': '使用图片 3 作为首帧，图片 9 作为尾帧。'}, (3, 9), 'body'),
    ({'body': 'IMG3 → FRAME9'}, (3, 9), 'body'),
    ({'body': 'IMAGE 3, resulting in IMAGE 9'}, (3, 9), 'body'),
    ({'body': '首帧：3；尾帧：9'}, (3, 9), 'body'),
    ({'body': 'construction', 'meta': 'IMAGE 3 -> IMAGE 9'}, (3, 9), 'header'),
    ({'body': 'construction', 'summary': '首帧 图片3，尾帧 图片9'}, (3, 9), 'header'),
    ({'body': 'construction', 'header': 'First frame: IMG3; last frame: IMG9'}, (3, 9), 'header'),
    ({'body': 'Use IMAGE 3 as the sole starting-frame anchor.'}, (3, None), 'body'),
    ({'body': 'Use IMAGE 3 as the only first frame.'}, (3, None), 'body'),
    ({'body': '仅首帧：图片3'}, (3, None), 'body'),
    ({'body': 'IMAGE 3 -> IMAGE 9', 'meta': 'HERO'}, (3, None), 'body'),
])
def test_resolver_recognizes_body_and_header_declarations(item, expected, location):
    anchors = vg.resolve_video_frame_anchors(1, item, AUTO)
    assert (anchors['start_anchor_slot'], anchors['end_anchor_slot']) == expected
    assert anchors['source'] == 'prompt_declaration'
    assert anchors['declaration_location'] == location


def test_header_declaration_takes_precedence_over_body():
    anchors = vg.resolve_video_frame_anchors(1, {
        'meta': 'IMAGE 3 -> IMAGE 9',
        'body': 'Use IMAGE 1 as first frame and IMAGE 2 as last frame.',
    }, AUTO)
    assert (anchors['start_anchor_slot'], anchors['end_anchor_slot']) == (3, 9)
    assert anchors['declaration_location'] == 'header'


@pytest.mark.parametrize('config,body,meta', [
    ({}, 'Use IMAGE 3 as first frame and IMAGE 9 as last frame.', ''),
    ({'videoFramePairing': 'slot'}, 'IMAGE 3 -> IMAGE 9', ''),
    (AUTO, 'The worker compares IMAGE 3 with IMAGE 9.', ''),
    (AUTO, 'Construction timelapse.', ''),
    (AUTO, 'Hero reveal.', 'HERO'),
])
def test_absent_declaration_or_disabled_mode_keeps_slot_contract(config, body, meta):
    anchors = vg.resolve_video_frame_anchors(5, {'body': body, 'meta': meta}, config)
    assert anchors == {
        'start_anchor_slot': 5, 'end_anchor_slot': None if meta == 'HERO' else 6,
        'source': 'slot_contract', 'declaration_location': None,
    }


def _frames(tmp_path, slots=(1, 2, 3, 9)):
    result = {}
    for slot in slots:
        path = tmp_path / f'img_{slot:03d}.webp'
        path.write_bytes(b'frame')
        result[slot] = str(path)
    return result


def _plan(tmp_path, frames, **kwargs):
    return vg.plan_video_slots(
        {1: {'body': 'Use IMAGE 3 as first frame and IMAGE 9 as last frame. Worker paints.'}},
        frames, kwargs.pop('quality', {}), str(tmp_path), config=AUTO, **kwargs,
    )[0]


def test_nonadjacent_selected_frames_are_rewritten_and_recorded(tmp_path):
    frames = _frames(tmp_path)
    plan = _plan(tmp_path, frames)
    assert plan['action'] == 'generate'
    assert plan['start_frame'] == frames[3]
    assert plan['end_frame'] == frames[9]
    assert set(re.findall(r'IMAGE\s*(\d+)', plan['prompt'])) == {'1', '2'}
    info = vg._video_info(plan, 'Omni Flash', 'failed', 'offline')
    assert (info['start_anchor_slot'], info['end_anchor_slot']) == (3, 9)
    assert info['anchor_pairing_source'] == 'prompt_declaration'
    assert not info['is_single_frame']


def test_selected_frames_drive_quality_and_staleness_gates(tmp_path):
    frames = _frames(tmp_path)
    assert _plan(tmp_path, frames, quality={1: 'manual_flagged'})['action'] == 'generate'
    selected_bad = _plan(tmp_path, frames, quality={9: 'manual_flagged'})
    assert selected_bad['action'] == 'blocked'
    assert 'IMAGE 9' in selected_bad['reason']
    selected_stale = _plan(tmp_path, frames, stale_slots={3})
    assert selected_stale['action'] == 'blocked'
    assert 'IMAGE 3' in selected_stale['reason']


@pytest.mark.parametrize('missing,role', [(3, '起始帧'), (9, '结束帧')])
def test_missing_declared_frame_never_falls_back_to_adjacent_or_t2v(tmp_path, missing, role):
    frames = _frames(tmp_path)
    del frames[missing]
    (tmp_path / 'vid_001.mp4').write_bytes(b'old-video')
    plan = _plan(tmp_path, frames, existing_videos=[{
        'slot': 1, 'status': 'success', 'start_anchor_slot': 3, 'end_anchor_slot': 9,
    }])
    assert plan['action'] == 'blocked'
    assert f'{role} IMAGE {missing}' in plan['reason']
    assert (plan['start_anchor_slot'], plan['end_anchor_slot']) == (3, 9)
    assert (tmp_path / 'vid_001.mp4').read_bytes() == b'old-video'


def test_changing_recognized_pair_replaces_old_video_after_new_result(tmp_path):
    frames = _frames(tmp_path)
    old = tmp_path / 'vid_001.mp4'
    old.write_bytes(b'old-video')
    plan = _plan(tmp_path, frames, existing_videos=[{
        'slot': 1, 'status': 'success', 'start_anchor_slot': 1, 'end_anchor_slot': 2,
    }])
    assert plan['action'] == 'generate'
    assert plan['delete_existing']
    assert old.read_bytes() == b'old-video'


@pytest.mark.parametrize('invalid_kind', ['empty_file', 'directory'])
def test_incomplete_declared_frame_file_is_blocked(tmp_path, invalid_kind):
    frames = _frames(tmp_path)
    end = tmp_path / 'img_009.webp'
    end.unlink()
    if invalid_kind == 'directory':
        end.mkdir()
    else:
        end.touch()
    plan = _plan(tmp_path, frames)
    assert plan['action'] == 'blocked'
    assert '结束帧 IMAGE 9' in plan['reason']


def test_duplicate_two_frame_declaration_requires_explicit_single_frame(tmp_path):
    frames = _frames(tmp_path)
    plan = vg.plan_video_slots({1: {'body': 'IMAGE 3 -> IMAGE 3'}}, frames, {},
                               str(tmp_path), config=AUTO)[0]
    assert plan['action'] == 'blocked'
    assert '首尾帧都指向 IMAGE 3' in plan['reason']
    assert (plan['start_anchor_slot'], plan['end_anchor_slot']) == (3, 3)


@pytest.mark.parametrize('source_fields', [
    {'source': 'manual_upload'}, {'model': 'manual_upload'},
    {'source': 'manual_swap'}, {'swapped_from_slot': 8},
    {'swapped_from_sequence': 8}, {'anchor_mismatch_overridden': True},
])
def test_manual_video_arrangement_remains_trusted_in_auto_mode(tmp_path, source_fields):
    frames = _frames(tmp_path)
    old = tmp_path / 'vid_001.mp4'
    old.write_bytes(b'manually-arranged-video')
    plan = _plan(tmp_path, frames, existing_videos=[{
        'slot': 1, 'status': 'success', 'start_anchor_slot': 1, 'end_anchor_slot': 2,
        **source_fields,
    }], verify_fn=lambda *args, **kwargs: pytest.fail('Manual video should remain trusted'))
    assert plan['action'] == 'reuse'
    assert old.read_bytes() == b'manually-arranged-video'


def test_single_frame_declaration_does_not_require_or_describe_a_last_frame(tmp_path):
    frames = _frames(tmp_path, (9,))
    plan = vg.plan_video_slots({1: {
        'body': 'Use IMAGE 9 as the sole starting-frame anchor. Smooth hero reveal.'}},
        frames, {}, str(tmp_path), config=AUTO)[0]
    assert plan['action'] == 'generate'
    assert plan['start_frame'] == frames[9]
    assert plan['end_frame'] is None
    assert plan['is_single_frame']
    assert set(re.findall(r'IMAGE\s*(\d+)', plan['prompt'])) == {'1'}
    info = vg._video_info(plan, 'Omni Flash', 'failed')
    assert info['is_single_frame']
    assert not info['is_hero']
    assert info['end_anchor_slot'] is None


@pytest.mark.parametrize('explicit_mode,expected_mode', [(None, 'auto'), ('slot', 'slot')])
def test_generation_inherits_saved_pairing_contract_unless_explicitly_overridden(
        tmp_path, monkeypatch, explicit_mode, expected_mode):
    frames = _frames(tmp_path)
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'video_frame_pairing': 'auto',
        'frames': [{'slot': slot, 'file': path} for slot, path in frames.items()],
    }))
    monkeypatch.setattr(vg, '_get_project_dir', lambda _title: str(tmp_path))
    monkeypatch.setattr(vg, 'ensure_video_submissions_resolved', lambda *args, **kwargs: None)
    seen = {}

    def inspect_plan(*args, **kwargs):
        seen.update(kwargs['config'])
        raise RuntimeError('stop before provider submission')

    monkeypatch.setattr(vg, 'plan_video_slots', inspect_plan)
    config = {'videoProvider': 'flow2api'}
    if explicit_mode is not None:
        config['videoFramePairing'] = explicit_mode
    with pytest.raises(RuntimeError, match='stop before provider submission'):
        vg.generate_video_sequence(config, 'test', '视频 1: IMAGE 3 -> IMAGE 9')
    assert seen['videoFramePairing'] == expected_mode
    if explicit_mode is None:
        assert 'videoFramePairing' not in config


def test_merge_preserves_nonhero_single_frame_anchor(tmp_path, monkeypatch):
    (tmp_path / 'frames').mkdir()
    (tmp_path / 'videos').mkdir()
    frames = _frames(tmp_path / 'frames', (9,))
    video = tmp_path / 'videos' / 'vid_001.mp4'
    video.write_bytes(b'video')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'title': 'single-frame', 'frames': [{'slot': 9, 'file': frames[9]}],
        'videos': [{'slot': 1, 'status': 'success', 'file': str(video),
                    'start_anchor_slot': 9, 'end_anchor_slot': None, 'is_single_frame': True}],
    }))
    checked = []
    monkeypatch.setattr(vg, 'verify_video_anchors', lambda path, start, end, **kwargs:
                        checked.append((start, end)) or (True, ''))
    monkeypatch.setattr(vg, '_merge_selected_files', lambda *args, **kwargs: {'status': 'success'})
    assert vg.merge_project_videos(str(tmp_path)) == {'status': 'success'}
    assert checked == [(frames[9], None)]


def test_auto_merge_uses_declared_video_slots_without_phantom_adjacent_slots(tmp_path, monkeypatch):
    (tmp_path / 'frames').mkdir()
    (tmp_path / 'videos').mkdir()
    frames = _frames(tmp_path / 'frames', (3, 9))
    video = tmp_path / 'videos' / 'vid_005.mp4'
    video.write_bytes(b'video')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'title': 'nonadjacent', 'video_frame_pairing': 'auto',
        'prompt_block': '视频 5: IMAGE 3 -> IMAGE 9',
        'frames': [{'slot': slot, 'file': path} for slot, path in frames.items()],
        'videos': [{'slot': 5, 'status': 'success', 'file': str(video),
                    'start_anchor_slot': 3, 'end_anchor_slot': 9}],
    }))
    checked = []
    monkeypatch.setattr(vg, 'verify_video_anchors', lambda path, start, end, **kwargs:
                        checked.append((start, end)) or (True, ''))
    merged_slots = []
    monkeypatch.setattr(vg, '_merge_selected_files', lambda project, manifest, slots, *args, **kwargs:
                        merged_slots.extend(slots) or {'status': 'success'})
    assert vg.merge_project_videos(str(tmp_path)) == {'status': 'success'}
    assert merged_slots == [5]
    assert checked == [(frames[3], frames[9])]
    # Explicit legacy mode retains its adjacent-slot expectations even on an auto project.
    with pytest.raises(vg.PartialMergeBlocked) as blocked:
        vg.merge_project_videos(str(tmp_path), config={'videoFramePairing': 'slot'})
    assert blocked.value.missing == [1]
