"""A single running task survives provider failures and completes without a new request."""
import contextlib
import copy

import pytest

import server
import server_common as common
import video_generation_recovery as recovery


PROMPT = ('图片 1:\nstart\n图片 2:\nmiddle\n图片 3:\nend\n'
          '视频 1 (图片 1 → 图片 2):\nBuild.\n'
          '视频 2 (图片 2 → 图片 3):\nFinish.\n')


@pytest.mark.parametrize('chain', [False, True])
def test_same_worker_survives_more_than_ten_rounds_and_merges_once(monkeypatch, tmp_path, chain):
    task_id = 'continuous_worker_chain' if chain else 'continuous_worker_sequence'
    directory = tmp_path / 'project'
    directory.mkdir()
    common.write_manifest(str(directory), {'videos': [], 'prompt_block': PROMPT})
    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(directory))
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
    monkeypatch.setattr(server, 'save_task_to_disk', lambda *a: True)
    calls, waits, merges = [], [], []
    lease = {'held': False}

    @contextlib.contextmanager
    def slot(*args):
        assert not lease['held']
        lease['held'] = True
        try:
            yield
        finally:
            lease['held'] = False

    monkeypatch.setattr(server, '_video_generation_slot', slot)

    def generate(config, title, block, on_progress, target_slots, **kwargs):
        assert lease['held']
        calls.append(list(target_slots))
        manifest = common.read_manifest(str(directory))
        rows = {v['slot']: v for v in manifest['videos']}
        for number in target_slots:
            failed = number == 2 and len(calls) <= 12
            if failed:
                row = {'slot': number, 'status': 'failed', 'error': 'temporary service failure',
                       'last_attempt': {'status': 'failed', 'submission_pending': False}}
                on_progress('video_error', {'index': number, 'error': row['error']})
            else:
                path = directory / f'vid_{number:03d}.mp4'
                path.write_bytes(b'validated-test-video')
                row = {'slot': number, 'status': 'success', 'file': str(path),
                       'last_attempt': {'status': 'success', 'submission_pending': False}}
                on_progress('video_done', {'index': number, 'video': row})
            rows[number] = row
        manifest['videos'] = list(rows.values())
        manifest['video_generation_stats'] = {'last_run': {
            'failed_slots': [v['slot'] for v in rows.values() if v['status'] == 'failed']}}
        common.write_manifest(str(directory), manifest)
        return manifest

    def wait(delay, cancel_check):
        assert not lease['held'], 'waiting must release the browser/account generation lease'
        task = server.ACTIVE_TASKS[task_id]
        assert task['status'] == 'running'
        assert not any(stage in ('result', 'error') for stage, _ in task['events'])
        assert not cancel_check()
        waits.append(delay)

    def execute(*args, **kwargs):
        return recovery.execute_video_generation_with_recovery(*args, **kwargs,
            retry_delays=(0,), wait_fn=wait,
            reconcile_fn=lambda *a, **k: {'outcomes': [], 'manifest': common.read_manifest(str(directory))})

    monkeypatch.setattr(server, 'execute_video_generation_with_recovery', execute)
    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(server, 'generate_video_chain_sequence', generate)
    monkeypatch.setattr(server, 'merge_project_videos',
                        lambda *a, **k: merges.append(1) or {'url': '/merged.mp4'})
    config = {'videoProvider': 'flow2api', 'videoContinuousGeneration': True,
              'videoMaxSlotAttempts': 20}
    worker = server.generate_video_chain_worker if chain else server.generate_videos_worker
    worker(task_id, config, 'project', PROMPT, [1, 2])
    task = server.ACTIVE_TASKS[task_id]
    assert task['status'] == 'completed' and task['outcome'] == 'completed'
    assert calls == [[1, 2]] + [[2]] * 12
    assert len(waits) == 12 and len(merges) == 1
    assert sum(stage == 'result' for stage, _ in task['events']) == 1
    assert task['result']['video_recovery']['status'] == 'complete'
    assert task['result']['video_generation_stats']['last_run']['failed_slots'] == []
    assert all(v['status'] == 'success' for v in task['result']['videos'])
    assert copy.deepcopy(common.read_manifest(str(directory)))['video_recovery']['status'] == 'complete'


def test_pipeline_defers_video_phase_until_image_browser_scope_exits(monkeypatch, tmp_path):
    import pipeline_orchestrator as pipeline
    task_id = 'continuous_staged_pipeline'
    common.write_manifest(str(tmp_path), {'title': 'project', 'videos': [], 'prompt_block': PROMPT})
    lease = {'held': False}
    calls, waiting, merges = [], [], []
    config = {'imageBackend': 'google_fx', 'videoProvider': 'flow2api', 'videoContinuousGeneration': True}

    @contextlib.contextmanager
    def browser(*args):
        lease['held'] = True
        try:
            yield
        finally:
            lease['held'] = False

    def render(runtime, title, block, on_progress):
        assert lease['held']
        assert runtime['_defer_video_to_worker'] is True
        assert runtime['videoContinuousGeneration'] is True
        video_result = pipeline._render_videos_with_recovery(runtime, title, block, project_dir=str(tmp_path))
        assert not calls
        return {'title': title, 'prompt_block': block, 'project_dir': str(tmp_path),
                'videos': video_result, 'status': 'partial_failed', 'has_failures': True}

    def generate(runtime, title, block, on_progress, target_slots, **kwargs):
        assert not lease['held'], 'the image browser scope must have exited before video work'
        target_slots = target_slots or [1, 2]
        calls.append(target_slots)
        data = common.read_manifest(str(tmp_path))
        data['videos'] = []
        for number in target_slots:
            path = tmp_path / f'vid_{number}.mp4'
            path.write_bytes(b'validated-video')
            row = {'slot': number, 'status': 'success', 'file': str(path)}
            data['videos'].append(row)
            on_progress('video_done', {'index': number, 'video': row})
        common.write_manifest(str(tmp_path), data)
        return data

    def wait(delay, cancel_check):
        assert not lease['held']
        assert server.ACTIVE_TASKS[task_id]['status'] == 'running'
        waiting.append(1)

    def execute(*args, **kwargs):
        return recovery.execute_video_generation_with_recovery(*args, **kwargs, retry_delays=(0,),
            wait_fn=wait, reconcile_fn=lambda *a, **k: {'outcomes': [], 'manifest': common.read_manifest(str(tmp_path))})

    monkeypatch.setattr(server, '_fx_browser_slot', browser)
    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path))
    monkeypatch.setattr(server, 'save_task_to_disk', lambda *a: True)
    monkeypatch.setattr(server, 'execute_video_generation_with_recovery', execute)
    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(pipeline, 'run_staged_frame_rendering', render)
    monkeypatch.setattr(server, 'merge_project_videos', lambda *a, **k: merges.append(1) or {'url': '/merged.mp4'})
    server.render_staged_worker(task_id, config, 'project', PROMPT)
    task = server.ACTIVE_TASKS[task_id]
    assert task['status'] == 'completed' and task['outcome'] == 'completed'
    assert calls == [[1, 2]] and waiting == [] and merges == [1]
    assert task['result']['videos']['video_recovery']['status'] == 'complete'


def test_deferred_stepped_pipeline_keeps_video_stage_running(monkeypatch, tmp_path):
    import stepped_pipeline as stepped
    state = {'stage': 'final_review', 'title': 'project', 'prompt_block': PROMPT}
    common.write_manifest(str(tmp_path), {'videos': [], 'prompt_block': PROMPT})
    saved, events = [], []
    monkeypatch.setattr(stepped, '_load_state', lambda *a: copy.deepcopy(state))
    monkeypatch.setattr(stepped, '_save_state', lambda title, value: saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(stepped, '_enrich_state_with_refs', lambda value: value)
    monkeypatch.setattr(stepped, '_get_project_dir', lambda *a: str(tmp_path))
    monkeypatch.setattr(stepped, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
    result = stepped.advance_stepped_pipeline('project', config={
        'videoProvider': 'flow2api', 'videoContinuousGeneration': True, '_defer_video_to_worker': True},
        on_progress=lambda *event: events.append(event))
    assert result['stage'] == 'render_videos'
    assert all(value.get('stage') != 'completed' for value in saved)
    assert not any(stage == 'stepped_stage' and details.get('stage') == 'completed'
                   for stage, details in events)
