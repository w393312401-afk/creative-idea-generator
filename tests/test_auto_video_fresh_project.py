"""Auto scheduling must persist its initial state before image providers create folders."""

import pytest

import server
import server_common


PROMPT = 'IMAGE 1: initial site\nIMAGE 2: completed site\nVIDEO 1 (IMAGE 1 -> IMAGE 2): build it'


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    project = tmp_path / 'outputs' / 'new-project'
    assert not project.exists()
    monkeypatch.setattr(server, '_get_project_dir', lambda _title: str(project))
    config = {
        '_auto_generate_videos': True,
        '_auto_generate_videos_explicit': True,
        '_project_key': 'new-project',
        'imageBackend': 'api',
        'videoProvider': 'flow2api',
        'reviewsDisabled': True,
    }
    task = server.get_or_create_task('frames_new_project', {
        'type': 'frames', 'project_key': 'new-project',
    })
    server.claim_frame_run(str(project), task['id'], until_released=True)
    return project, config, task


def test_auto_coordinator_can_record_waiting_state_before_first_image(fresh_project):
    project, config, task = fresh_project
    run = server._ProgressiveAutoVideo(task['id'], task, config, 'new project', PROMPT, lambda *args: None)
    try:
        run.advance()
        data = server_common.read_manifest(str(project))
        assert data['prompt_block'] == PROMPT
        assert data['auto_video']['status'] == 'waiting'
        assert data['auto_video']['request_id'] == 'auto_video:' + task['id']
        assert 'task_id' not in data['auto_video']
        assert data['auto_video']['pending_slots'] == [1]
        assert run.child_id is None
    finally:
        if project.exists():
            run.finish(aborted=True)
        server.release_frame_run(str(project), task['id'])


@pytest.mark.parametrize('candidate', [False, True])
def test_new_project_reaches_both_image_renderers_with_auto_enabled(fresh_project, monkeypatch, candidate):
    import candidate_selection_pipeline
    import pipeline_orchestrator

    project, config, task = fresh_project
    render_calls = []
    def render(*args, **kwargs):
        render_calls.append(True)
        # The image provider historically created this directory only here.
        # Its first call must remain reachable when automatic video is enabled.
        (project / 'frames').mkdir(parents=True, exist_ok=True)
        return {**server_common.read_manifest(str(project)), 'frames': [], 'videos': []}
    monkeypatch.setattr(pipeline_orchestrator, 'render_frames_for_task', render)
    monkeypatch.setattr(candidate_selection_pipeline, 'run_candidate_selection_frame_sequence', render)
    worker = server.generate_frames_selection_worker if candidate else server.generate_frames_worker
    worker(task['id'], config, 'new project', PROMPT, None)
    assert render_calls == [True], task.get('error')
    assert task['status'] == 'completed', task.get('error')
    assert server_common.read_manifest(str(project))['auto_video']['status'] == 'blocked'


def test_record_json_roundtrip_is_exact_after_directory_exists(fresh_project):
    project, config, task = fresh_project
    project.mkdir(parents=True)
    # JSON legitimately stringifies integer keys and turns tuples into lists.
    # _record reads that canonical form first, so these legacy fields do not
    # explain the immediate failures of entirely new project directories.
    server_common.write_manifest(str(project), {'notes': {1: ('start', 'end')}})
    run = server._ProgressiveAutoVideo(task['id'], task, config, 'new project', PROMPT, lambda *args: None)
    try:
        written = run._record('waiting', emit=False)
        assert server_common.read_manifest(str(project)) == written
        assert written['notes'] == {'1': ['start', 'end']}
    finally:
        run.finish(aborted=True)
        server.release_frame_run(str(project), task['id'])


def test_existing_project_waiting_state_has_new_request_before_child_exists(fresh_project):
    project, config, task = fresh_project
    project.mkdir(parents=True)
    server_common.write_manifest(str(project), {
        'prompt_block': PROMPT,
        'auto_video': {'status': 'blocked', 'task_id': 'videos_previous',
                       'request_id': 'auto_video:frames_previous', 'message': 'old failure'},
    })
    run = server._ProgressiveAutoVideo(task['id'], task, config, 'new project', PROMPT, lambda *args: None)
    try:
        run.advance()
        info = server_common.read_manifest(str(project))['auto_video']
        assert info['status'] == 'waiting'
        assert info['request_id'] == 'auto_video:' + task['id']
        assert 'task_id' not in info
        assert run.child_id is None
    finally:
        run.finish(aborted=True)
        server.release_frame_run(str(project), task['id'])


def test_real_manifest_write_failure_still_blocks_auto_scheduling(fresh_project, monkeypatch):
    project, config, task = fresh_project
    atomic_write = server_common.write_json_atomic
    def denied_manifest(path, data, **kwargs):
        if str(path).endswith('manifest.json'):
            raise PermissionError('manifest filesystem is not writable')
        return atomic_write(path, data, **kwargs)
    monkeypatch.setattr(server_common, 'write_json_atomic', denied_manifest)
    run = server._ProgressiveAutoVideo(task['id'], task, config, 'new project', PROMPT, lambda *args: None)
    try:
        with pytest.raises(RuntimeError, match='自动视频清单保存失败'):
            run.advance()
        assert project.is_dir()
        assert server_common.read_manifest(str(project)) is None
        assert run.child_id is None
        assert run.worker_thread is None
        assert run.queued == set()
    finally:
        server.release_frame_run(str(project), task['id'])
