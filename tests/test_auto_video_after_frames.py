"""Ready frame pairs enqueue independent video work without blocking images."""
import contextlib
import copy
import threading
import time

import pytest
from PIL import Image

import server
import server_common
from video_operations import VideoOperationStore

PROMPT = ('图片 1:\nstart\n图片 2:\nmiddle\n图片 3:\nend\n'
          '视频 1 (图片 1 → 图片 2):\nBuild the room.\n'
          '视频 2 (图片 2 → 图片 3):\nFinish the room.\n')
REAL_FINISH = server._finish_video_generation


def test_continuous_auto_video_recovers_failed_segment_before_finishing(progressive, monkeypatch):
    import video_generation_recovery as recovery
    project, _, _, calls, _, add, create, merged = progressive
    original = server.generate_video_sequence
    failed_once = set()

    def generate(config, title, block, on_progress, target_slots, **kwargs):
        if target_slots == [1] and 1 not in failed_once:
            failed_once.add(1)
            calls.append([1])
            with server.manifest_lock(str(project)):
                data = server.read_manifest(str(project))
                data['videos'] = [{'slot': 1, 'status': 'failed', 'error': 'temporary service failure',
                                   'last_attempt': {'status': 'failed', 'submission_pending': False}}]
                data['video_generation_stats'] = {'last_run': {'failed_slots': [1]}}
                server.write_manifest(str(project), data)
            on_progress('video_error', {'index': 1, 'error': 'temporary service failure'})
            return data
        return original(config, title, block, on_progress, target_slots)

    def wait(delay, cancel_check):
        assert run.child['status'] == 'running'
        assert not any(stage == 'result' for stage, _ in run.child['events'])
        assert not cancel_check()

    def execute(*args, **kwargs):
        return recovery.execute_video_generation_with_recovery(*args, **kwargs, retry_delays=(0,),
            wait_fn=wait, reconcile_fn=lambda *a, **k: {
                'outcomes': [], 'manifest': server.read_manifest(str(project))})

    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(server, 'execute_video_generation_with_recovery', execute)
    run = create(videoContinuousGeneration=True)
    run.advance()
    run.advance(add(1))
    run.advance(add(2))
    wait_for_calls(progressive, [[1]])
    run.advance(add(3))
    result = finish_and_wait(run)
    assert calls == [[1], [2], [1]]
    assert run.child['outcome'] == result['auto_video']['status'] == 'completed'
    assert all(row['status'] == 'success' for row in result['videos'])
    assert len(merged) == 1
    events = [detail for stage, detail in run.child['events'] if stage == 'auto_video_updated']
    assert any(detail.get('phase') == 'waiting' and detail['status'] == 'started' for detail in events)


def wait_until(predicate, description='background video progress'):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f'Timed out waiting for {description}')
        threading.Event().wait(.005)


def wait_for_calls(progressive, expected):
    wait_until(lambda: len(progressive[3]) >= len(expected), f'video batches {expected}')
    assert progressive[3] == expected


def finish_and_wait(run, result=None, *, aborted=False):
    run.finish(result, aborted=aborted)
    if run.worker_thread is not None:
        assert run.worker_done.wait(5), 'Video worker did not settle after closing image input'
        run.worker_thread.join(timeout=1)
        assert not run.worker_thread.is_alive()
    return server.read_manifest(run.project_dir) or result


@pytest.fixture
def progressive(monkeypatch, tmp_path, offline_fx_video_io):
    project = tmp_path / 'project'
    frames = project / 'frames'
    frames.mkdir(parents=True)
    manifest = {'title': 'test', 'frames': [], 'videos': [], 'prompt_block': PROMPT}
    server_common.write_manifest(str(project), manifest)
    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(project))
    monkeypatch.setattr(server, '_VIDEO_OPERATIONS', VideoOperationStore(str(tmp_path / 'ops.sqlite3')))
    saved, calls, events, merged = [], [], [], []
    runs = []
    coordinator_class = server._ProgressiveAutoVideo
    def create_coordinator(*args, **kwargs):
        run = coordinator_class(*args, **kwargs)
        runs.append(run)
        return run
    create_coordinator.runs = runs
    monkeypatch.setattr(server, '_ProgressiveAutoVideo', create_coordinator)
    monkeypatch.setattr(server, 'save_task_to_disk', lambda task_id: saved.append(task_id) or True)
    monkeypatch.setattr(server.FX_CONTROL, 'admission', lambda: (True, ''))
    monkeypatch.setattr(server, '_video_generation_slot', lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', lambda c, t, p, **k: p)
    def generate(config, title, block, on_progress, target_slots):
        parent = server.ACTIVE_TASKS['frames_auto_test']
        child_id = parent['result']['auto_video']['task_id']
        assert child_id in saved and 'frames_auto_test' in saved
        assert server.ACTIVE_TASKS[child_id]['cancel_event'] is not parent['cancel_event']
        calls.append(list(target_slots))
        completed = []
        with server.manifest_lock(str(project)):
            data = server.read_manifest(str(project))
            for slot in target_slots:
                path = project / 'videos' / f'vid_{slot:03d}.mp4'
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b'fake-video')
                row = {'slot': slot, 'status': 'success', 'file': str(path),
                       'start_anchor_slot': slot, 'end_anchor_slot': slot + 1}
                data['videos'] = [v for v in data['videos'] if v['slot'] != slot] + [row]
                completed.append((slot, row))
            server.write_manifest(str(project), data)
        for slot, row in completed:
            on_progress('video_done', {'index': slot, 'total': len(target_slots), 'video': row})
        return data
    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    def finish(result, *args):
        merged.append(copy.deepcopy(result))
        last_run = (result.get('video_generation_stats') or {}).get('last_run') or {}
        success = (len(result.get('videos', [])) == 2 and not last_run.get('failed_slots')
                   and not last_run.get('cancelled_slots'))
        result = {**result, 'completion_state': 'completed' if success else 'partial_failed'}
        if success:
            result['merged_video'] = {'file': str(project / 'videos' / 'merged.mp4')}
        with server.manifest_lock(str(project)):
            server.write_manifest(str(project), result)
        return result
    monkeypatch.setattr(server, '_finish_video_generation', finish)
    config = {'_auto_generate_videos': True, '_auto_generate_videos_explicit': True,
              '_project_key': 'project', 'videoProvider': 'flow2api', 'reviewsDisabled': True}
    task = server.get_or_create_task('frames_auto_test', {'type': 'frames', 'project_key': 'project'})
    server.claim_frame_run(str(project), 'frames_auto_test', until_released=True)
    def add_frame(slot, empty=False):
        path = frames / f'img_{slot:03d}.webp'
        if empty:
            path.write_bytes(b'')
        else:
            Image.new('RGB', (24, 24), (slot * 70, 10, 30)).save(path)
        row = {'sequence': slot, 'slot': slot, 'file': str(path), 'quality_gate': 'pending_manual_review'}
        with server.manifest_lock(str(project)):
            data = server.read_manifest(str(project))
            data['frames'] = [f for f in data['frames'] if f['sequence'] != slot] + [row]
            server.write_manifest(str(project), data)
        return {'sequence': slot, 'frame': row, 'skipped': False}
    def coordinator(**overrides):
        config.update(overrides)
        return server._ProgressiveAutoVideo('frames_auto_test', task, config, 'test', PROMPT,
            lambda stage, detail: events.append((stage, detail)))
    yield project, config, task, calls, events, add_frame, coordinator, merged
    for run in runs:
        if not run.closed:
            run.finish(aborted=True)
        if run.worker_thread is not None:
            assert run.worker_done.wait(5), 'Background worker leaked across test teardown'
            run.worker_thread.join(timeout=1)


def test_each_video_starts_as_soon_as_its_pair_is_ready(progressive):
    project, _, _, calls, events, add, factory, merges = progressive
    run = factory()
    run.advance(add(1))
    assert not calls
    run.advance(add(2))
    wait_for_calls(progressive, [[1]])
    assert not (project / 'frames' / 'img_003.webp').exists()
    assert not merges  # No premature merge between pairs.
    child = server.ACTIVE_TASKS[run.child_id]
    assert child['status'] == 'running'
    assert run.info['pending_slots'] == [2]
    run.advance(add(3))
    finish_and_wait(run, {})
    assert calls == [[1], [2]]
    assert child['status'] == 'completed'
    assert len(merges) == 1
    assert len([e for e, _ in events if e == 'auto_video_started']) == 1


@pytest.mark.parametrize('enabled', [True, False])
def test_explicit_auto_video_preference_survives_in_the_manifest_and_result(progressive, enabled):
    project, _, _, _, _, _, factory, _ = progressive
    run = factory(_auto_generate_videos=enabled, _auto_generate_videos_preference_explicit=True)
    if enabled:
        run._record('waiting', emit=False)
    result = finish_and_wait(run, {})
    manifest = server.read_manifest(str(project))
    assert manifest['auto_generate_videos'] is enabled
    assert manifest['auto_generate_videos_preference_explicit'] is True
    assert result['auto_generate_videos_preference_explicit'] is True


def test_replayed_frame_boundary_does_not_resubmit(progressive):
    *_, add, factory, merges = progressive
    run = factory()
    one, two = add(1), add(2)
    run.advance(one)
    run.advance(two)
    child = run.child_id
    run.advance(two)
    assert run.child_id == child
    finish_and_wait(run, {})
    assert progressive[3] == [[1]]


def test_existing_files_wait_for_this_runs_settled_boundary(progressive):
    run = progressive[6]()
    for slot in (1, 2, 3):
        progressive[5](slot)
    run.advance()
    assert not progressive[3]
    run.advance({'sequence': 1, 'skipped': True})
    assert not progressive[3]
    run.advance({'sequence': 2, 'skipped': True})
    finish_and_wait(run, {})
    assert progressive[3] == [[1]]


def test_nonadjacent_declared_pair_waits_only_for_its_actual_anchors(progressive):
    project, config, task, calls, events, add, _, merges = progressive
    prompt = PROMPT.replace('图片 1 → 图片 2', '图片 1 → 图片 3')
    run = server._ProgressiveAutoVideo('frames_auto_test', task, config, 'test', prompt,
                                      lambda *a: None)
    run.advance(add(1))
    run.advance(add(2))
    assert not calls
    run.advance(add(3))
    finish_and_wait(run, {})
    assert calls == [[1, 2]]
    assert run.pairs[0]['end_anchor_slot'] == 3


def test_partial_image_run_does_not_block_already_ready_video(progressive):
    run = progressive[6]()
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    result = finish_and_wait(run, {})
    assert progressive[3] == [[1]]
    assert result['auto_video']['status'] == 'partial_failed'
    assert result['auto_video']['pending_slots'] == [2]
    assert server.ACTIVE_TASKS[run.child_id]['outcome'] == 'partial_failed'


def test_empty_frame_is_not_ready(progressive):
    run = progressive[6]()
    run.advance(progressive[5](1))
    run.advance(progressive[5](2, empty=True))
    result = finish_and_wait(run, {})
    assert not progressive[3]
    assert result['auto_video']['status'] == 'blocked'


def test_off_remains_image_only_and_clears_old_state(progressive):
    run = progressive[6](_auto_generate_videos=False)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    result = run.finish({'auto_video': {'task_id': 'old'}})
    assert result['auto_generate_videos'] is False
    assert 'auto_video' not in result
    assert not progressive[3]


def test_cancel_stops_next_pair_and_releases_child_claim(progressive):
    run = progressive[6]()
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    wait_for_calls(progressive, [[1]])
    progressive[2]['cancel_event'].set()
    with pytest.raises(ConnectionError):
        run.advance(progressive[5](3))
    server.ACTIVE_TASKS[run.child_id]['status'] = 'cancelled'  # Cancellation endpoint settles first.
    finish_and_wait(run, aborted=True)
    assert progressive[3] == [[1]]
    assert server.ACTIVE_TASKS[run.child_id]['status'] == 'cancelled'
    assert server.read_manifest(str(progressive[0]))['auto_video']['status'] == 'cancelled'
    assert server.claim_frame_run(str(progressive[0]), 'replacement') is None


def test_registration_failure_prevents_paid_submission(progressive, monkeypatch):
    run = progressive[6]()
    monkeypatch.setattr(server, 'save_task_to_disk', lambda task_id: False)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    assert not progressive[3]
    assert '保存失败' in run.info['message']
    finish_and_wait(run, {})
    assert server.ACTIVE_TASKS[run.child_id]['status'] == 'failed'


def test_later_successful_batch_keeps_earlier_failed_attempt_in_run_outcome(progressive, monkeypatch):
    run = progressive[6]()
    generate = server.generate_video_sequence
    def failed_first(*a, **k):
        generate(*a, **k)
        failed = [1] if k['target_slots'] == [1] else []
        with server.manifest_lock(str(progressive[0])):
            result = server.read_manifest(str(progressive[0]))
            result['video_generation_stats'] = {'last_run': {'failed_slots': failed}}
            server.write_manifest(str(progressive[0]), result)
        return result
    monkeypatch.setattr(server, 'generate_video_sequence', failed_first)
    for slot in (1, 2, 3):
        run.advance(progressive[5](slot))
    finish_and_wait(run, {})
    assert progressive[7][0]['video_generation_stats']['last_run']['failed_slots'] == [1]
    assert run.child['outcome'] == 'partial_failed'


def test_video_exception_preserves_images_and_continues_image_worker(progressive, monkeypatch):
    run = progressive[6]()
    def fail(*a, **k):
        raise RuntimeError('provider failed')
    monkeypatch.setattr(server, 'generate_video_sequence', fail)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    run.advance(progressive[5](3))
    finish_and_wait(run, {})
    assert run.error == 'provider failed'
    assert len(server.read_manifest(str(progressive[0]))['frames']) == 3


def test_unexpected_video_finish_error_closes_child_stream_even_if_status_save_fails(progressive, monkeypatch):
    run = progressive[6]()
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    stream_stopped = threading.Event()
    received = []
    run.child['listeners'].add((lambda stage, detail: received.append((stage, detail)), stream_stopped))
    def fail_finish(*args, **kwargs):
        raise RuntimeError('unexpected video completion failure')
    monkeypatch.setattr(server, '_finish_video_generation', fail_finish)
    record = run._record
    def fail_error_save(status, *args, **kwargs):
        if status == 'blocked':
            raise RuntimeError('failed to persist terminal video status')
        return record(status, *args, **kwargs)
    monkeypatch.setattr(run, '_record', fail_error_save)
    run.advance(progressive[5](3))
    finish_and_wait(run, {})
    assert run.child['status'] == 'failed'
    assert run.child['error'] == 'unexpected video completion failure'
    assert stream_stopped.is_set(), 'A failed child must end the already connected SSE'
    errors = [detail for stage, detail in received if stage == 'error']
    assert errors == [{'message': 'unexpected video completion failure'}]
    assert any(stage == 'error' for stage, _ in run.child['events'])
    assert len(server.read_manifest(str(progressive[0]))['frames']) == 3
    assert not progressive[7]
    assert server.claim_frame_run(str(progressive[0]), 'replacement') is None


def test_incomplete_images_never_merge_with_old_successful_clips(progressive, monkeypatch):
    run = progressive[6]()
    data = server.read_manifest(str(progressive[0]))
    data['videos'] = [{'slot': 2, 'status': 'success', 'file': 'old-video.mp4'}]
    server.write_manifest(str(progressive[0]), data)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    monkeypatch.setattr(server, '_finish_video_generation', REAL_FINISH)
    def forbidden_merge(*a, **k):
        pytest.fail('Incomplete image input must stop merging old clips')
    monkeypatch.setattr(server, 'merge_project_videos', forbidden_merge)
    result = finish_and_wait(run, {})
    assert result['auto_video']['status'] == 'partial_failed'
    assert server.ACTIVE_TASKS[run.child_id]['result']['completion_state'] == 'partial_failed'


def test_subset_marks_downstream_lineage_before_submitting_its_video(progressive, monkeypatch):
    project, config, task, calls, _, add, _, _ = progressive
    for slot in (1, 2, 3):
        add(slot)
    config['reviewsDisabled'] = False
    config['qaGateLevel'] = 'balanced'
    run = server._ProgressiveAutoVideo('frames_auto_test', task, config, 'test', PROMPT,
                                      lambda *a: None, target_sequences=[2])
    run.advance(add(2))
    finish_and_wait(run, {})
    assert calls == [[1]]
    assert 2 in run.blocked
    third = next(f for f in server.read_manifest(str(project))['frames'] if f['sequence'] == 3)
    assert third['stale_lineage'] is True


def test_multiple_subset_boundaries_keep_previous_new_video(progressive):
    project, config, task, calls, _, add, _, _ = progressive
    run = server._ProgressiveAutoVideo('frames_auto_test', task, config, 'test', PROMPT,
                                      lambda *a: None, target_sequences=[1, 2, 3])
    for slot in (1, 2, 3):
        run.advance(add(slot))
    finish_and_wait(run, {})
    assert calls == [[1], [2]]
    assert [v['slot'] for v in server.read_manifest(str(project))['videos']] == [1, 2]


def test_committed_repair_prompt_is_used_for_following_video(progressive, monkeypatch):
    run = progressive[6]()
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    wait_for_calls(progressive, [[1]])
    details = progressive[5](3)
    revised = PROMPT.replace('Finish the room.', 'Restore the accepted window geometry.')
    with server.manifest_lock(str(progressive[0])):
        data = server.read_manifest(str(progressive[0]))
        data['prompt_block'] = revised
        server.write_manifest(str(progressive[0]), data)
    seen = []
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence',
        lambda c, t, p, **k: seen.append(p) or p)
    run.advance(details)
    result = finish_and_wait(run, {})
    assert 'Restore the accepted window geometry.' in seen[0]
    assert result['prompt_block'] == revised


def test_optimizer_baseline_does_not_resubmit_unrelated_clip_after_frame_repair(progressive, monkeypatch):
    run = progressive[6]()
    def optimize(c, t, block, **k):
        optimized = block.replace('Build the room.', 'Build the room with a locked camera.')
        with server.manifest_lock(str(progressive[0])):
            data = server.read_manifest(str(progressive[0]))
            data['prompt_block'] = optimized
            server.write_manifest(str(progressive[0]), data)
        return optimized
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', optimize)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    wait_for_calls(progressive, [[1]])
    details = progressive[5](3)
    with server.manifest_lock(str(progressive[0])):
        data = server.read_manifest(str(progressive[0]))
        data['prompt_block'] = data['prompt_block'].replace('Finish the room.', 'Restore the accepted geometry.')
        server.write_manifest(str(progressive[0]), data)
    run.advance(details)
    finish_and_wait(run, {})
    assert progressive[3] == [[1], [2]]


def test_repaired_pair_changed_during_optimization_waits_for_its_new_anchor(progressive, monkeypatch):
    project, config, task, calls, events, add, _, merges = progressive
    block = PROMPT.replace('视频 1 ', '图片 4:\nafter\n视频 1 ')
    run = server._ProgressiveAutoVideo('frames_auto_test', task, config, 'test', block,
                                      lambda stage, detail: events.append((stage, detail)))
    optimizing, release_optimizer = threading.Event(), threading.Event()
    submissions = []
    generation = server.generate_video_sequence
    def optimize(c, t, prompt, **kwargs):
        if kwargs['target_slots'] == [1]:
            optimizing.set()
            assert release_optimizer.wait(5)
        return prompt
    def video(c, title, prompt, on_progress, target_slots):
        submissions.append(list(target_slots))
        # Mirror the generator's missing-anchor failure rather than letting the
        # fake provider create a successful clip from an absent frame.
        if target_slots == [1] and not (project / 'frames' / 'img_004.webp').exists():
            with server.manifest_lock(str(project)):
                data = server.read_manifest(str(project))
                data['video_generation_stats'] = {'last_run': {'failed_slots': [1]}}
                server.write_manifest(str(project), data)
            return data
        return generation(c, title, prompt, on_progress=on_progress, target_slots=target_slots)
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', optimize)
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    run.advance(add(1))
    run.advance(add(2))
    try:
        assert optimizing.wait(5)
        ready = add(3)
        repaired = block.replace('图片 1 → 图片 2', '图片 1 → 图片 4')
        with server.manifest_lock(str(project)):
            data = server.read_manifest(str(project))
            data['prompt_block'] = repaired
            data['prompt_slots'] = server.prompt_slots_list(repaired)
            server.write_manifest(str(project), data)
        run.advance(ready)
        release_optimizer.set()
        wait_until(lambda: run._queue.unfinished_tasks == 0, 'ready batches after the accepted repair')
        assert submissions == [[2]], 'Revised VIDEO 1 must wait until IMAGE 4 is ready'
        assert 1 not in run.processed
        assert 1 not in run.ready
        assert run.child['status'] == 'running'
        assert not merges
        run.advance(add(4))
        final = finish_and_wait(run, {})
    finally:
        release_optimizer.set()
    assert submissions == [[2], [1]]
    assert calls == [[2], [1]]
    assert run.child['outcome'] == 'completed'
    assert final['auto_video']['status'] == 'completed'
    assert len(merges) == 1
    assert len(merges[0]['frames']) == 4


@pytest.mark.parametrize('candidate', [False, True])
def test_both_image_workers_finish_while_first_video_is_blocked(progressive, monkeypatch, candidate):
    import candidate_selection_pipeline
    import pipeline_orchestrator
    video_entered, release_video, images_done, parent_done = (threading.Event() for _ in range(4))
    submissions = []
    generation = server.generate_video_sequence
    def video(*a, **k):
        submissions.append(k['target_slots'])
        if k['target_slots'] == [1]:
            video_entered.set()
            assert release_video.wait(5), 'Test did not release the first video'
        return generation(*a, **k)
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    monkeypatch.setattr(server, '_fx_serial_lock_for', lambda *a: contextlib.nullcontext())
    def images(*a, on_progress, **k):
        for slot in (1, 2, 3):
            ready = progressive[5](slot)
            on_progress('frame', ready)
            on_progress('frame_ready', ready)
            if slot == 2:
                assert video_entered.wait(5), 'First pair was not submitted independently'
            if slot == 3:
                # Replayed boundaries enqueue neither another clip nor another child.
                on_progress('frame_ready', ready)
        images_done.set()
        return server.read_manifest(str(progressive[0]))
    monkeypatch.setattr(pipeline_orchestrator, 'render_frames_for_task', images)
    monkeypatch.setattr(candidate_selection_pipeline, 'run_candidate_selection_frame_sequence', images)
    worker = server.generate_frames_selection_worker if candidate else server.generate_frames_worker
    def invoke_worker():
        try:
            worker('frames_auto_test', progressive[1], 'test', PROMPT, None)
        finally:
            parent_done.set()
    driver = threading.Thread(target=invoke_worker, daemon=True)
    driver.start()
    try:
        assert video_entered.wait(5)
        assert images_done.wait(1), 'Image 3 waited for video 1'
        assert parent_done.wait(1), 'Image parent waited for video finalization'
        task = progressive[2]
        assert task['status'] == 'completed'
        assert len(task['result']['frames']) == 3
        assert task['result']['auto_video']['status'] == 'started'
        run = server._ProgressiveAutoVideo.runs[-1]
        child = server.ACTIVE_TASKS[run.child_id]
        assert child['status'] == 'running'
        assert run.queued == {1, 2}
        assert submissions == [[1]]
        assert not progressive[7]
        assert not run.worker_done.is_set()
        assert len([event for event, _ in task['events'] if event == 'auto_video_started']) == 1
        assert len([item for item in server.ACTIVE_TASKS.values()
                    if (item.get('dimensions') or {}).get('parent_frame_task_id') == 'frames_auto_test']) == 1
    finally:
        release_video.set()
        driver.join(timeout=5)
    assert not driver.is_alive()
    assert run.worker_done.wait(5)
    assert submissions == [[1], [2]]
    assert child['status'] == 'completed'
    assert child['outcome'] == 'completed'
    assert len(progressive[7]) == 1
    assert len(progressive[7][0]['frames']) == 3
    assert [v['slot'] for v in progressive[7][0]['videos']] == [1, 2]
    final = server.read_manifest(str(progressive[0]))
    assert final['auto_video']['status'] == 'completed'
    assert final['merged_video']
    assert child['result']['merged_video'] == final['merged_video']
    assert progressive[2]['status'] == 'completed'


def test_finish_closes_input_without_waiting_for_video_or_merge(progressive, monkeypatch):
    run = progressive[6]()
    entered, release, producer_done = (threading.Event() for _ in range(3))
    generation = server.generate_video_sequence
    def video(*a, **k):
        if k['target_slots'] == [1]:
            entered.set()
            assert release.wait(5)
        return generation(*a, **k)
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    result = {}
    def finish_images():
        run.advance(progressive[5](3))
        run.finish(result)
        producer_done.set()
    producer = threading.Thread(target=finish_images, daemon=True)
    try:
        assert entered.wait(5)
        producer.start()
        assert producer_done.wait(1), 'frame_ready or finish waited for a paid video call'
        assert run.closed
        assert not run.worker_done.is_set()
        assert result['auto_video']['task_id'] == run.child_id
        assert result['auto_video']['status'] == 'started'
        assert run.queued == {1, 2}
        assert not progressive[7]
    finally:
        release.set()
        if producer.ident is not None:
            producer.join(timeout=5)
    assert run.worker_done.wait(5)
    assert progressive[3] == [[1], [2]]
    assert len(progressive[7]) == 1


def test_cancelled_video_child_does_not_cancel_or_block_remaining_images(progressive, monkeypatch):
    run = progressive[6]()
    entered, release = threading.Event(), threading.Event()
    submissions = []
    def video(*a, **k):
        submissions.append(k['target_slots'])
        entered.set()
        assert release.wait(5)
        if run.child['cancel_event'].is_set():
            raise server.GenerationCancelled('video child cancelled')
        pytest.fail('Cancelled child must not publish another clip')
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    try:
        assert entered.wait(5)
        assert run.child['cancel_event'] is not progressive[2]['cancel_event']
        run.child['cancel_event'].set()
        run.advance(progressive[5](3))
        result = run.finish({})
        assert len(server.read_manifest(str(progressive[0]))['frames']) == 3
        assert not progressive[2]['cancel_event'].is_set()
        assert result['auto_video']['task_id'] == run.child_id
    finally:
        release.set()
    assert run.worker_done.wait(5)
    assert submissions == [[1]]
    assert run.child['status'] == 'cancelled'
    assert server.read_manifest(str(progressive[0]))['auto_video']['status'] == 'cancelled'
    assert not progressive[7]
    assert server.claim_frame_run(str(progressive[0]), 'replacement') is None


def test_parent_cancel_propagates_to_child_and_discards_queued_pairs(progressive, monkeypatch):
    run = progressive[6]()
    entered, release = threading.Event(), threading.Event()
    submissions = []
    def video(*a, **k):
        submissions.append(k['target_slots'])
        entered.set()
        assert release.wait(5)
        assert run.child['cancel_event'].is_set()
        raise server.GenerationCancelled('image parent cancelled')
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    try:
        assert entered.wait(5)
        run.advance(progressive[5](3))
        assert run.queued == {1, 2}
        progressive[2]['cancel_event'].set()
        run.finish(aborted=True)
        assert run.child['cancel_event'].is_set()
        assert not run.worker_done.is_set()
    finally:
        release.set()
    assert run.worker_done.wait(5)
    assert submissions == [[1]]
    assert run.child['status'] == 'cancelled'
    assert server.read_manifest(str(progressive[0]))['auto_video']['status'] == 'cancelled'
    assert not progressive[7]


@pytest.mark.parametrize('candidate', [False, True])
def test_video_failure_isolated_from_real_image_parent(progressive, monkeypatch, candidate):
    import candidate_selection_pipeline
    import pipeline_orchestrator
    monkeypatch.setattr(server, '_fx_serial_lock_for', lambda *a: contextlib.nullcontext())
    def video(*a, **k):
        raise RuntimeError('provider failed independently')
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    def images(*a, on_progress, **k):
        for slot in (1, 2, 3):
            if slot == 3:
                wait_until(lambda: server._ProgressiveAutoVideo.runs[-1].error,
                           'video provider failure before the remaining image')
            ready = progressive[5](slot)
            on_progress('frame', ready)
            on_progress('frame_ready', ready)
        return server.read_manifest(str(progressive[0]))
    monkeypatch.setattr(pipeline_orchestrator, 'render_frames_for_task', images)
    monkeypatch.setattr(candidate_selection_pipeline, 'run_candidate_selection_frame_sequence', images)
    worker = server.generate_frames_selection_worker if candidate else server.generate_frames_worker
    worker('frames_auto_test', progressive[1], 'test', PROMPT, None)
    run = server._ProgressiveAutoVideo.runs[-1]
    assert run.worker_done.wait(5)
    assert progressive[2]['status'] == 'completed'
    assert len(progressive[2]['result']['frames']) == 3
    assert run.child['status'] == 'failed'
    assert run.error == 'provider failed independently'
    assert server.read_manifest(str(progressive[0]))['auto_video']['status'] == 'blocked'
    assert not progressive[2]['cancel_event'].is_set()
    assert not progressive[7]


@pytest.mark.parametrize('body', [{'auto_generate_videos': 'true'},
    {'video_target_slots': []}, {'video_target_slots': [True]}, {'video_target_slots': [-1]}])
def test_invalid_auto_options_rejected(body):
    with pytest.raises(ValueError):
        server._configure_frame_auto_video({}, body)


def test_auto_options_preserve_debug_targets():
    config = {}
    server._configure_frame_auto_video(config, {'auto_generate_videos': True,
        'video_target_slots': [3, 1, 3], 'merge_speed': 2})
    assert config['_auto_video_target_slots'] == [1, 3]
    assert config['videoFramePairing'] == 'auto'
    assert config['_merge_speed'] == 2


def frame_handler(monkeypatch, path, body):
    import project_archive
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = path
    handler.headers = {}
    handler.client_address = ('127.0.0.1', 1)
    handler._read_json_body = lambda: body
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((status, payload))
    monkeypatch.setattr(server, 'access_ok', lambda h: True)
    monkeypatch.setattr(server, 'rate_ok', lambda *a: True)
    monkeypatch.setattr(server, 'effective_config', lambda c: {'imageBackend': 'api', 'videoProvider': 'flow2api'})
    monkeypatch.setattr(project_archive, 'archived_request', lambda body: False)
    return handler, responses


@pytest.mark.parametrize('path,worker', [('/api/generate_frames', 'generate_frames_worker'),
    ('/api/generate_frames_selection', 'generate_frames_selection_worker')])
def test_http_frame_entries_pass_auto_options(progressive, monkeypatch, path, worker):
    dispatched = []
    class Thread:
        def __init__(self, *, target, args, daemon):
            dispatched.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(server.threading, 'Thread', Thread)
    server.release_frame_run(str(progressive[0]), 'frames_auto_test')
    handler, responses = frame_handler(monkeypatch, path, {'title': 'project', 'prompt_block': PROMPT,
        'auto_generate_videos': True, 'video_target_slots': [1], 'merge_speed': 2,
        'generation_mode': 'standard', 'generation_mode_explicit': True})
    handler.do_POST()
    assert responses[0][0] == 200
    target, args = dispatched[0]
    assert target is getattr(server, worker)
    assert args[1]['_auto_generate_videos'] is True
    assert args[1]['_auto_video_target_slots'] == [1]


@pytest.mark.parametrize('path', ['/api/generate_frames', '/api/generate_frames_selection'])
def test_http_reconnects_to_image_parent_then_blocks_on_independent_video_child(progressive, monkeypatch, path):
    run = progressive[6]()
    entered, release = threading.Event(), threading.Event()
    generation = server.generate_video_sequence
    def video(*a, **k):
        entered.set()
        assert release.wait(5)
        return generation(*a, **k)
    monkeypatch.setattr(server, 'generate_video_sequence', video)
    run.advance(progressive[5](1))
    run.advance(progressive[5](2))
    try:
        assert entered.wait(5)
        handler, responses = frame_handler(monkeypatch, path, {'title': 'project', 'prompt_block': PROMPT})
        handler.do_POST()
        assert responses[0][0] == 200
        assert responses[0][1]['already_running'] is True
        assert responses[0][1]['task_id'] == 'frames_auto_test'
        run.finish({})
        handler, responses = frame_handler(monkeypatch, path, {'title': 'project', 'prompt_block': PROMPT})
        handler.do_POST()
        assert responses[0][0] == 409
        assert responses[0][1]['failure_code'] == 'PROJECT_BUSY'
        assert responses[0][1]['task_id'] == run.child_id
    finally:
        release.set()
    assert run.worker_done.wait(5)
    assert progressive[3] == [[1]]


def test_managed_config_preserves_pairing(monkeypatch):
    monkeypatch.setattr(server_common, 'SERVER_MANAGED', True)
    assert server_common.effective_config({'videoFramePairing': 'auto'})['videoFramePairing'] == 'auto'


def _failing_slot_one(progressive, monkeypatch, *, fail_times):
    import video_generation_recovery as recovery
    project, _, _, calls, *_ = progressive
    original = server.generate_video_sequence
    failures = []

    def generate(config, title, block, on_progress, target_slots, **kwargs):
        if target_slots == [1] and len(failures) < fail_times:
            failures.append(1)
            calls.append([1])
            with server.manifest_lock(str(project)):
                data = server.read_manifest(str(project))
                data['videos'] = [v for v in data['videos'] if v['slot'] != 1] + [
                    {'slot': 1, 'status': 'failed', 'error': 'upstream refused',
                     'last_attempt': {'status': 'failed', 'submission_pending': False,
                                      'error': 'upstream refused'}}]
                server.write_manifest(str(project), data)
            on_progress('video_error', {'index': 1, 'error': 'upstream refused'})
            return data
        return original(config, title, block, on_progress, target_slots)

    def execute(*args, **kwargs):
        return recovery.execute_video_generation_with_recovery(*args, **kwargs, retry_delays=(0,),
            wait_fn=lambda *a: None, reconcile_fn=lambda *a, **k: {
                'outcomes': [], 'manifest': server.read_manifest(str(project))})

    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(server, 'execute_video_generation_with_recovery', execute)
    monkeypatch.setattr(server, 'VIDEO_RETRY_DELAYS', (0,))


def test_failed_segment_is_retried_during_run_before_later_segments(progressive, monkeypatch):
    _, _, _, calls, _, add, create, merged = progressive
    _failing_slot_one(progressive, monkeypatch, fail_times=1)
    run = create(videoContinuousGeneration=True)
    run.advance()
    run.advance(add(1))
    run.advance(add(2))
    # The retry runs while images are still being produced, not after every segment.
    wait_for_calls(progressive, [[1], [1]])
    run.advance(add(3))
    result = finish_and_wait(run)
    assert calls == [[1], [1], [2]]
    assert run.child['outcome'] == 'completed'
    assert all(row['status'] == 'success' for row in result['videos'])
    assert len(merged) == 1


def test_segment_failing_past_attempt_limit_finishes_partial_instead_of_hanging(progressive, monkeypatch):
    _, _, _, calls, _, add, create, merged = progressive
    _failing_slot_one(progressive, monkeypatch, fail_times=99)
    run = create(videoContinuousGeneration=True, videoMaxSlotAttempts=2)
    run.advance()
    run.advance(add(1))
    run.advance(add(2))
    wait_for_calls(progressive, [[1], [1]])
    run.advance(add(3))
    finish_and_wait(run)
    assert calls == [[1], [1], [2]]
    assert run.child['outcome'] == 'partial_failed'
    assert 1 in run.blocked and '已停止自动重试' in run.blocked[1]
    assert '已停止自动重试' in run.info['message']
