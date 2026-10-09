"""Only committed winners with settled guards can trigger progressive video work."""

from pathlib import Path

import pytest
from PIL import Image

import candidate_selection_pipeline as csp
import chain_guard
import frame_generator as fg
import server_common as sc


PROMPT = 'IMAGE 1: empty site\nVIDEO 1: building\nIMAGE 2: framing\nVIDEO 2: styling\nIMAGE 3: finished room'


@pytest.fixture
def image_project(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'frames').mkdir(parents=True)
    monkeypatch.setattr(fg, '_get_project_dir', lambda _title: str(project))
    monkeypatch.setattr(csp, '_get_project_dir', lambda _title: str(project))
    monkeypatch.setattr(fg, 'resolve_cover_reference', lambda *args, **kwargs: None)
    monkeypatch.setattr(csp, 'resolve_cover_reference', lambda *args, **kwargs: None)
    monkeypatch.setattr(fg, '_match_color_lab', lambda *args: None)
    monkeypatch.setattr(csp, '_match_color_lab', lambda *args: None)
    monkeypatch.setattr(fg, '_continuity_result', lambda *args, **kwargs: ({'status': 'passed'}, 'site'))
    monkeypatch.setattr(fg, 'continuity_max_retries', lambda _config: 0)
    monkeypatch.setattr(csp, '_generate_full_collage_from_frames', lambda *args, **kwargs: None)
    monkeypatch.setattr('tools.collage.build_keyframe_collage', lambda *args, **kwargs: None)
    return project


def _save_image(path, value=70):
    Image.new('RGB', (48, 64), (value, 100, 130)).save(path, 'WEBP')


def _fake_api_images(monkeypatch, trace):
    def generate(config, prompt, path):
        seq = int(Path(path).stem.rsplit('_', 1)[-1])
        trace.append(('image', seq))
        _save_image(path, 50 + seq * 20)
        return 'standard'

    monkeypatch.setattr(fg, '_generate_text_image', generate)
    monkeypatch.setattr(fg, '_generate_image_edit',
                        lambda config, prompt, ref, path, **kwargs: generate(config, prompt, path))


def _settled_guard(project, sequence, quality='auto_approved'):
    manifest = sc.read_manifest(str(project))
    for frame in manifest['frames']:
        if frame['sequence'] == sequence:
            frame['quality_gate'] = quality
            frame['guard_settled'] = True
    sc.write_manifest(str(project), manifest)


def test_api_ready_is_after_guard_and_before_next_image_and_preserves_interleaved_video(
        image_project, monkeypatch):
    trace = []
    _fake_api_images(monkeypatch, trace)

    def anchor(*args, **kwargs):
        trace.append(('guard', 1))
        _settled_guard(image_project, 1)
        return {'halt': False}

    def beat(config, title, prompt, index, project, **kwargs):
        seq = index + 1
        trace.append(('guard', seq))
        _settled_guard(image_project, seq)
        return {'halt': False}

    monkeypatch.setattr(chain_guard, 'run_anchor_guard', anchor)
    monkeypatch.setattr(chain_guard, 'guard_beat', beat)
    ready = []

    def progress(stage, data):
        if stage == 'frame':
            trace.append(('display', data['frame']['sequence']))
        if stage == 'frame_ready':
            seq = data['sequence']
            trace.append(('ready', seq))
            ready.append(data)
            assert data['frame']['guard_settled']
            assert data['frame']['quality_gate'] == 'auto_approved'
            assert sc.read_manifest(str(image_project))['frames'][seq - 1]['guard_settled']
            if seq == 2:
                manifest = sc.read_manifest(str(image_project))
                manifest.update(videos=[{'slot': 1, 'status': 'success', 'file': 'video.mp4'}],
                                video_generation_stats={'last_run': {'attempt_id': 'video-1'}},
                                auto_video={'status': 'running'},
                                video_prompt_optimizations={'1': {'optimized_prompt': 'optimized'}},
                                prompt_block='optimized video prompts')
                sc.write_manifest(str(image_project), manifest)
                trace.append(('video', 1))

    result = fg.generate_frame_sequence({
        'imageBackend': 'api', 'allowTextOnlyAnchor': True,
        'chainGuardMode': 'halt', '_auto_generate_videos': True,
    }, 'project', PROMPT, on_progress=progress)
    assert [data['sequence'] for data in ready] == [1, 2, 3]
    assert trace.index(('display', 2)) < trace.index(('guard', 2)) < trace.index(('ready', 2))
    assert trace.index(('ready', 2)) < trace.index(('video', 1)) < trace.index(('image', 3))
    committed = sc.read_manifest(str(image_project))
    assert committed['videos'] == [{'slot': 1, 'status': 'success', 'file': 'video.mp4'}]
    assert committed['video_generation_stats']['last_run']['attempt_id'] == 'video-1'
    assert committed['auto_video']['status'] == 'running'
    assert committed['prompt_block'] == 'optimized video prompts'
    assert committed['video_prompt_optimizations']['1']['optimized_prompt'] == 'optimized'
    assert result['videos'] == committed['videos']


def test_failed_continuity_frame_is_displayed_but_never_ready(image_project, monkeypatch):
    trace = []
    _fake_api_images(monkeypatch, trace)
    monkeypatch.setattr(fg, '_continuity_result', lambda *args, **kwargs:
                        ({'status': 'failed', 'reason': 'drift'} if args[5] == 2 else
                         {'status': 'passed'}, 'site'))
    events = []
    with pytest.raises(fg.FrameContinuityError, match='drift'):
        fg.generate_frame_sequence({'imageBackend': 'api', 'allowTextOnlyAnchor': True,
                                    'chainGuardMode': 'off', '_auto_generate_videos': True},
                                   'project', PROMPT, on_progress=lambda stage, data: events.append((stage, data)))
    assert [data['sequence'] for stage, data in events if stage == 'frame_ready'] == [1]
    assert [data['frame']['sequence'] for stage, data in events if stage == 'frame'] == [1, 2]
    assert ('image', 3) not in trace


def test_guard_halt_frame_never_becomes_ready(image_project, monkeypatch):
    trace = []
    _fake_api_images(monkeypatch, trace)
    monkeypatch.setattr(chain_guard, 'run_anchor_guard', lambda *args, **kwargs: {'halt': False})

    def halt(*args, **kwargs):
        _settled_guard(image_project, 2, 'sequence_review_flagged')
        return {'halt': True, 'issues': [{'text': 'wrong topology', 'severity': 'chain'}]}

    monkeypatch.setattr(chain_guard, 'guard_beat', halt)
    events = []
    fg.generate_frame_sequence({'imageBackend': 'api', 'allowTextOnlyAnchor': True,
                                'chainGuardMode': 'halt', '_auto_generate_videos': True},
                               'project', PROMPT, on_progress=lambda stage, data: events.append((stage, data)))
    assert [data['sequence'] for stage, data in events if stage == 'frame_ready'] == [1]
    assert ('image', 3) not in trace


def test_candidate_ready_uses_chosen_winner_after_evaluation_and_guard(image_project, monkeypatch):
    trace = []
    generating = {'active': False}

    def candidates(config, title, item, reference, sequence, **kwargs):
        generating['active'] = True
        trace.append(('candidates', sequence))
        folder = image_project / 'frames' / 'candidates' / f'frame_{sequence:03d}'
        folder.mkdir(parents=True)
        paths = []
        for index in range(1, 5):
            path = folder / f'candidate_{index}.webp'
            _save_image(path, index * 20)
            paths.append(str(path))
        generating['active'] = False
        return paths

    def evaluate(config, prompt, reference, paths, sequence, **kwargs):
        assert not generating['active']
        trace.append(('evaluation', sequence))
        return {'best_index': 3, 'selection_reason': 'best', 'candidates': []}

    def guard(*args, **kwargs):
        sequence = 1 if len(args) < 5 else args[3] + 1
        trace.append(('guard', sequence))
        _settled_guard(image_project, sequence)
        return {'halt': False}

    monkeypatch.setattr(csp, 'generate_frame_candidates', candidates)
    monkeypatch.setattr(csp, 'evaluate_and_select_best_candidate', evaluate)
    monkeypatch.setattr(chain_guard, 'run_anchor_guard', guard)
    monkeypatch.setattr(chain_guard, 'guard_beat', guard)
    ready = []

    def progress(stage, data):
        if stage == 'frame_ready':
            assert not generating['active']
            seq = data['sequence']
            trace.append(('ready', seq))
            ready.append(data)
            assert data['frame']['chosen_candidate_index'] == 3
            assert data['frame']['guard_settled']
            if seq == 2:
                manifest = sc.read_manifest(str(image_project))
                manifest['videos'] = [{'slot': 1, 'status': 'success'}]
                sc.write_manifest(str(image_project), manifest)

    result = csp.run_candidate_selection_frame_sequence({
        'imageBackend': 'api', 'chainGuardMode': 'halt', '_auto_generate_videos': True,
    }, 'project', PROMPT, on_progress=progress)
    assert [data['sequence'] for data in ready] == [1, 2, 3]
    assert trace.index(('candidates', 2)) < trace.index(('evaluation', 2)) < trace.index(('guard', 2)) < trace.index(('ready', 2))
    assert trace.index(('ready', 2)) < trace.index(('candidates', 3))
    assert result['videos'] == [{'slot': 1, 'status': 'success'}]


def test_fx_auto_generation_uses_single_image_batches_and_ready_after_batch_returns(
        image_project, monkeypatch, tmp_path):
    trace = []
    active = {'batch': False}
    batches = []
    monkeypatch.setattr(fg, 'apply_google_fx_runtime_overrides', lambda _config: None)
    monkeypatch.setattr(fg, '_get_google_fx_image_service', lambda: (object(), object()))
    monkeypatch.setattr(fg, '_get_account_pool_service', lambda: None)

    def batch(service, models, config, prompts, reference, **kwargs):
        active['batch'] = True
        sequence = len(batches) + 1
        batches.append(list(prompts))
        trace.append(('batch', sequence))
        folder = tmp_path / f'batch-{sequence}'
        folder.mkdir()
        path = folder / 'image.webp'
        _save_image(path, sequence * 20)
        kwargs['attempt_state']['used'] = 1
        active['batch'] = False
        return [str(path)], str(folder)

    monkeypatch.setattr(fg, '_fx_generate_batch', batch)

    def progress(stage, data):
        if stage == 'frame_ready':
            assert not active['batch']
            trace.append(('ready', data['sequence']))

    fg.generate_frame_sequence({'imageBackend': 'google_fx', 'allowTextOnlyAnchor': True,
                                'chainGuardMode': 'off', '_auto_generate_videos': True},
                               'project', PROMPT, on_progress=progress)
    assert [len(items) for items in batches] == [1, 1, 1]
    assert trace == [('batch', 1), ('ready', 1), ('batch', 2), ('ready', 2), ('batch', 3), ('ready', 3)]


def test_frame_writer_preserves_video_capabilities_and_state_only_when_enabled(image_project):
    original = {'frames': [{'slot': 1}], 'videos': [{'slot': 1, 'status': 'success'}],
                'capability_degraded': {'videos': {'issues': ['ffmpeg unavailable']}}}
    sc.write_manifest(str(image_project), original)
    stale_frame_snapshot = {'frames': [{'slot': 1}, {'slot': 2}], 'videos': [],
                            'capability_degraded': {'frames': {'issues': ['numpy unavailable']}}}
    fg.write_frame_manifest(str(image_project), stale_frame_snapshot, preserve_video_state=True)
    committed = sc.read_manifest(str(image_project))
    assert committed['videos'] == original['videos']
    assert set(committed['capability_degraded']) == {'frames', 'videos'}
    stale_frame_snapshot['videos'] = []
    fg.write_frame_manifest(str(image_project), stale_frame_snapshot, preserve_video_state=False)
    assert sc.read_manifest(str(image_project))['videos'] == []


def test_frame_writer_stops_progressive_work_if_manifest_could_not_commit(image_project, monkeypatch):
    sc.write_manifest(str(image_project), {'frames': []})
    monkeypatch.setattr(fg, 'write_manifest', lambda *args: None)
    with pytest.raises(RuntimeError, match='帧清单保存失败'):
        fg.write_frame_manifest(str(image_project), {'frames': [{'slot': 1}]}, preserve_video_state=True)


@pytest.mark.parametrize('backend', ['api', 'google_fx', 'candidate_selection'])
def test_reused_frames_are_committed_and_ready_without_regeneration(image_project, monkeypatch, backend):
    entries = []
    for sequence in range(1, 4):
        path = image_project / 'frames' / f'img_{sequence:03d}.webp'
        _save_image(path)
        entries.append({'slot': sequence, 'sequence': sequence, 'file': str(path),
                        'quality_gate': 'pending_manual_review'})
    sc.write_manifest(str(image_project), {'frames': entries})
    monkeypatch.setattr(fg, '_generate_text_image', lambda *args, **kwargs: pytest.fail('Reuse must not regenerate'))
    monkeypatch.setattr(fg, '_generate_image_edit', lambda *args, **kwargs: pytest.fail('Reuse must not regenerate'))
    monkeypatch.setattr(fg, '_fx_generate_batch', lambda *args, **kwargs: pytest.fail('Reuse must not regenerate'))
    monkeypatch.setattr(fg, 'apply_google_fx_runtime_overrides', lambda _config: None)
    monkeypatch.setattr(fg, '_get_google_fx_image_service', lambda: (object(), object()))
    monkeypatch.setattr(fg, '_get_account_pool_service', lambda: None)
    monkeypatch.setattr(csp, 'generate_frame_candidates', lambda *args, **kwargs: pytest.fail('Reuse must not regenerate'))
    ready = []

    def progress(stage, data):
        if stage == 'frame_ready':
            ready.append(data)
            assert data['skipped']
            assert data['frame']['file']
            assert any(frame['sequence'] == data['sequence'] for frame in sc.read_manifest(str(image_project))['frames'])

    config = {'imageBackend': backend, 'chainGuardMode': 'off', '_auto_generate_videos': True}
    function = csp.run_candidate_selection_frame_sequence if backend == 'candidate_selection' else fg.generate_frame_sequence
    function(config, 'project', PROMPT, on_progress=progress)
    assert [data['sequence'] for data in ready] == [1, 2, 3]


def test_soft_guard_flag_does_not_emit_ready_and_survives_later_frames(image_project, monkeypatch):
    trace = []
    _fake_api_images(monkeypatch, trace)
    monkeypatch.setattr(chain_guard, 'run_anchor_guard', lambda *args, **kwargs: {'halt': False})

    def guard(config, title, prompt, beat, project, **kwargs):
        _settled_guard(image_project, beat + 1, 'sequence_review_flagged' if beat == 1 else 'auto_approved')
        return {'halt': beat == 1, 'issues': [{'text': 'wrong topology', 'severity': 'chain'}]}

    monkeypatch.setattr(chain_guard, 'guard_beat', guard)
    monkeypatch.setattr(chain_guard, 'guard_autofix_enabled', lambda _mode: False)
    events = []
    fg.generate_frame_sequence({'imageBackend': 'api', 'allowTextOnlyAnchor': True,
                                'chainGuardMode': 'autofix_soft', '_auto_generate_videos': True},
                               'project', PROMPT, on_progress=lambda stage, data: events.append((stage, data)))
    assert [data['sequence'] for stage, data in events if stage == 'frame_ready'] == [1, 3]
    assert sc.read_manifest(str(image_project))['frames'][1]['quality_gate'] == 'sequence_review_flagged'


def test_internal_autofix_render_does_not_publish_ready_before_outer_guard(image_project, monkeypatch):
    trace = []
    _fake_api_images(monkeypatch, trace)
    events = []
    fg.generate_frame_sequence({'imageBackend': 'api', 'allowTextOnlyAnchor': True,
                                'chainGuardMode': 'autofix', '_auto_generate_videos': True},
                               'project', PROMPT, on_progress=lambda stage, data: events.append((stage, data)),
                               chain_guard_review=False)
    assert [data['frame']['sequence'] for stage, data in events if stage == 'frame'] == [1, 2, 3]
    assert [data for stage, data in events if stage == 'frame_ready'] == []


@pytest.mark.parametrize('backend', ['api', 'google_fx', 'candidate_selection'])
def test_accepted_fix_is_committed_before_ready_preserving_other_video_optimizations(
        image_project, monkeypatch, tmp_path, backend):
    from prompt_pipeline import _parse_prompt_slots, _format_prompt_block
    import pipeline_orchestrator

    trace = []
    _fake_api_images(monkeypatch, trace)
    monkeypatch.setattr(fg, 'apply_google_fx_runtime_overrides', lambda _config: None)
    monkeypatch.setattr(fg, '_get_google_fx_image_service', lambda: (object(), object()))
    monkeypatch.setattr(fg, '_get_account_pool_service', lambda: None)
    generated = []

    def batch(service, models, config, prompts, reference, **kwargs):
        folder = tmp_path / f'fx-{len(generated)}'
        folder.mkdir()
        path = folder / 'image.webp'
        _save_image(path)
        generated.append(path)
        return [str(path)], str(folder)

    monkeypatch.setattr(fg, '_fx_generate_batch', batch)

    def candidates(config, title, item, reference, sequence, **kwargs):
        folder = image_project / 'frames' / 'candidates' / f'frame_{sequence:03d}'
        folder.mkdir(parents=True)
        paths = []
        for index in range(1, 5):
            path = folder / f'candidate_{index}.webp'
            _save_image(path)
            paths.append(str(path))
        return paths

    monkeypatch.setattr(csp, 'generate_frame_candidates', candidates)
    monkeypatch.setattr(csp, 'evaluate_and_select_best_candidate',
                        lambda *args, **kwargs: {'best_index': 1, 'candidates': []})
    monkeypatch.setattr(chain_guard, 'run_anchor_guard', lambda *args, **kwargs: {'halt': False})
    fixed = {'accepted': False}

    def guard(config, title, prompt, beat, project, **kwargs):
        failing = beat == 1 and not fixed['accepted']
        _settled_guard(image_project, beat + 1,
                       'sequence_review_flagged' if failing else 'auto_approved')
        return {'halt': failing, 'issues': [{'text': 'fix framing', 'severity': 'chain'}]}

    def fix(config, title, prompt, sequence, **kwargs):
        images, videos = _parse_prompt_slots(prompt)
        images[2] = {**images[2], 'body': 'fixed framing'}
        videos[1] = {**videos[1], 'body': 'fixed construction motion'}
        fixed['accepted'] = True
        return {'prompt_block': _format_prompt_block(images, videos), 'rolled_back': False}

    monkeypatch.setattr(chain_guard, 'guard_beat', guard)
    monkeypatch.setattr(pipeline_orchestrator, 'fix_frame_issue', fix)
    ready = []

    def progress(stage, data):
        if stage != 'frame_ready':
            return
        ready.append(data)
        if data['sequence'] == 1:
            latest = sc.read_manifest(str(image_project))
            images, videos = _parse_prompt_slots(latest['prompt_block'])
            videos[2] = {**videos[2], 'body': 'unrelated optimized motion'}
            latest['prompt_block'] = _format_prompt_block(images, videos)
            latest['video_generation_stats'] = {'last_run': {'attempt_id': 'video-previous'}}
            sc.write_manifest(str(image_project), latest)
        else:
            committed = sc.read_manifest(str(image_project))
            assert data['prompt_block'] == committed['prompt_block']
            images, videos = _parse_prompt_slots(data['prompt_block'])
            assert images[2]['body'] == 'fixed framing'
            assert videos[1]['body'] == 'fixed construction motion'
            assert videos[2]['body'] == 'unrelated optimized motion'
            assert committed['video_generation_stats']['last_run']['attempt_id'] == 'video-previous'

    config = {'imageBackend': backend, 'allowTextOnlyAnchor': True,
              'chainGuardMode': 'autofix', '_auto_generate_videos': True}
    function = csp.run_candidate_selection_frame_sequence if backend == 'candidate_selection' else fg.generate_frame_sequence
    result = function(config, 'project', PROMPT, on_progress=progress)
    assert fixed['accepted']
    assert [data['sequence'] for data in ready] == [1, 2, 3]
    assert result['prompt_block'] == sc.read_manifest(str(image_project))['prompt_block']


def test_prompt_commit_failure_cannot_publish_a_ready_frame(image_project, monkeypatch):
    path = image_project / 'frames' / 'img_001.webp'
    _save_image(path)
    sc.write_manifest(str(image_project), {
        'frames': [{'sequence': 1, 'slot': 1, 'file': str(path)}], 'prompt_block': PROMPT,
    })
    monkeypatch.setattr(fg, 'write_manifest', lambda *args: None)
    events = []
    with pytest.raises(RuntimeError, match='提示词保存失败'):
        fg.emit_frame_ready(str(image_project), 1, lambda *args: events.append(args), 1, 3,
                            prompt_block=PROMPT.replace('empty site', 'corrected site'),
                            previous_prompt_block=PROMPT)
    assert not events
