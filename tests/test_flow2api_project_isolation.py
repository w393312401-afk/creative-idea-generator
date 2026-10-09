"""Flow's HTTP concurrency must not race a project's frame/video manifest."""
import threading
from types import SimpleNamespace

import pytest

import server
import server_common
import pipeline_orchestrator
import stepped_pipeline


CONFIG = {'videoProvider': 'flow2api', 'imageBackend': 'api'}
BODY = {'request_id': 'flow-project-request-1', 'title': 'shared',
        'prompt_block': '图片 1:\nstart\n图片 2:\nend\n视频 1:\nmove', 'config': CONFIG}


def handler(path, body):
    request = object.__new__(server.SparkRequestHandler)
    request.path = path
    request._gate = lambda *a, **k: True
    request._read_json_body = lambda: dict(body)
    sent = []
    request._send_json = lambda value, status=200: sent.append((value, status))
    return request, sent


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    state = SimpleNamespace(calls=[], fail_start=False, project=str(tmp_path / 'shared'))

    class FakeThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            if state.fail_start:
                raise RuntimeError('thread start failed')
            state.calls.append((self.target, self.args))

    monkeypatch.setattr(server, 'threading', SimpleNamespace(**{**vars(threading), 'Thread': FakeThread}))
    monkeypatch.setattr(server, 'access_ok', lambda *a: True)
    monkeypatch.setattr(server, 'rate_ok', lambda *a: True)
    monkeypatch.setattr(server, '_client_ip', lambda *a: 'test')
    monkeypatch.setattr(server, 'effective_config', lambda cfg: dict(cfg or {}))
    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / str(title)))
    monkeypatch.setattr(server, 'read_manifest', lambda *a: None)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(server, 'prompt_delivery_block_reason', lambda *a: None)
    monkeypatch.setattr(server, '_require_fx_admission', lambda *a: pytest.fail('Flow API does not use Ads admission'))
    return state


def post(path, body):
    request, sent = handler(path, body)
    request.do_POST()
    assert len(sent) == 1
    return sent[0]


def test_same_project_different_requests_conflict_but_replay_recovers(dispatch):
    first, status = post('/api/generate_videos', BODY)
    assert status == 200
    replay, status = post('/api/generate_videos', BODY)
    assert status == 200 and replay['task_id'] == first['task_id']
    conflict, status = post('/api/generate_video_chain', {**BODY, 'request_id': 'flow-project-request-2'})
    assert status == 409 and conflict['failure_code'] == 'PROJECT_BUSY'
    assert len(dispatch.calls) == 1
    # Repeating the rejected identity cannot later start a fresh paid worker.
    repeated, status = post('/api/generate_video_chain', {**BODY, 'request_id': 'flow-project-request-2'})
    assert status == 200 and repeated['task_status'] == 'failed'
    assert len(dispatch.calls) == 1


def test_different_projects_remain_concurrent(dispatch):
    post('/api/generate_videos', BODY)
    _, status = post('/api/generate_videos', {
        **BODY, 'request_id': 'flow-project-request-other', 'title': 'independent'})
    assert status == 200 and len(dispatch.calls) == 2


def test_cancelled_worker_holds_project_against_video_and_frame_until_exit(dispatch):
    first, _ = post('/api/generate_videos', BODY)
    task = server.ACTIVE_TASKS[first['task_id']]
    task['status'] = 'cancelled'
    task['cancel_event'].set()
    assert server.claim_frame_run(dispatch.project, 'new-frame-worker') == first['task_id']
    _, status = post('/api/generate_videos', {**BODY, 'request_id': 'flow-project-request-2'})
    assert status == 409 and len(dispatch.calls) == 1
    server.release_frame_run(dispatch.project, first['task_id'])
    _, status = post('/api/generate_videos', {**BODY, 'request_id': 'flow-project-request-3'})
    assert status == 200 and len(dispatch.calls) == 2


@pytest.mark.parametrize('status', ['running', 'cancelled'])
def test_video_respects_unreleased_frame_worker_even_after_cancellation(dispatch, status):
    task = server.get_or_create_task('frame-owner')
    assert server.claim_frame_run(dispatch.project, 'frame-owner') is None
    task['status'] = status
    _, response_status = post('/api/generate_videos', BODY)
    assert response_status == 409 and not dispatch.calls
    assert server_common._ACTIVE_FRAME_RUNS[dispatch.project] == 'frame-owner'


def test_legacy_claim_keeps_its_existing_terminal_recovery(dispatch):
    task = server.get_or_create_task('legacy-owner')
    assert server.claim_frame_run(dispatch.project, 'legacy-owner') is None
    task['status'] = 'cancelled'
    assert server.claim_frame_run(dispatch.project, 'new-legacy-owner') is None


@pytest.mark.parametrize('failure', ['thread', 'checkpoint'])
def test_registration_failure_releases_project_without_dispatch(dispatch, monkeypatch, failure):
    if failure == 'thread':
        dispatch.fail_start = True
    else:
        monkeypatch.setattr(server, 'save_task_to_disk', lambda *a: False)
    _, status = post('/api/generate_videos', BODY)
    assert status == 500 and not dispatch.calls
    assert server.claim_frame_run(dispatch.project, 'following-frame') is None


@pytest.mark.parametrize('chain', [False, True])
def test_project_lease_covers_generation_and_merge_then_releases(dispatch, monkeypatch, chain):
    route = '/api/generate_video_chain' if chain else '/api/generate_videos'
    first, _ = post(route, BODY)
    task_id = first['task_id']
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', lambda *a, **k: BODY['prompt_block'])

    def generate(*args, **kwargs):
        assert server.claim_frame_run(dispatch.project, 'racing-frame') == task_id
        return {'videos': [{'slot': 1, 'status': 'success'}]}

    def merge(*args, **kwargs):
        assert server.claim_frame_run(dispatch.project, 'racing-frame') == task_id
        return None

    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(server, 'generate_video_chain_sequence', generate)
    monkeypatch.setattr(server, 'merge_project_videos', merge)
    worker, args = dispatch.calls[0]
    worker(*args)
    assert server.ACTIVE_TASKS[task_id]['status'] == 'completed'
    assert server.claim_frame_run(dispatch.project, 'following-frame') is None


@pytest.mark.parametrize('route,body', [
    ('/api/auto_run', {'task_id': 'auto-flow-next', 'dimensions': {'project_key': 'shared'}}),
    ('/api/render_staged', {'title': 'shared', 'display_title': 'Different human label'}),
    ('/api/stepped/start', {'project_key': 'shared', 'dimensions': {'theme': 'shared'}}),
    ('/api/stepped/advance', {'title': 'shared', 'action': 'approve'}),
])
def test_full_pipeline_cannot_bypass_running_video_project(dispatch, route, body):
    post('/api/generate_videos', BODY)
    _, status = post(route, {**body, 'config': CONFIG})
    assert status == 409 and len(dispatch.calls) == 1


@pytest.mark.parametrize('route,body', [
    ('/api/auto_run', {'task_id': 'auto_cancel_scope', 'dimensions': {'project_key': 'shared'}}),
    ('/api/render_staged', {'title': 'shared', 'display_title': 'Different human label'}),
    ('/api/stepped/start', {'project_key': 'shared', 'dimensions': {'theme': 'shared'}}),
    ('/api/stepped/advance', {'title': 'shared', 'action': 'approve'}),
])
@pytest.mark.parametrize('image_backend,video_provider,uses_fx', [
    ('api', 'flow2api', False), ('google_fx', 'flow2api', True), ('api', 'google_fx', True),
])
def test_pipeline_cancel_uses_startup_backend_capability(
        dispatch, monkeypatch, route, body, image_backend, video_provider, uses_fx):
    import builtins
    from unittest.mock import Mock
    monkeypatch.setattr(server, '_require_fx_admission', lambda *a: True)
    monkeypatch.setattr(builtins, 'google_fx_cancelled', False, raising=False)
    cancel_request = Mock(return_value=False)
    monkeypatch.setattr(server, 'get_fx_cancel_flag', lambda: SimpleNamespace(cancel_request=cancel_request))
    monkeypatch.setattr(server.FX_CONTROL, 'audit', lambda *a, **kw: None)
    config = {'imageBackend': image_backend, 'videoProvider': video_provider}
    response, status = post(route, {**body, 'config': config})
    assert status == 200
    task_id = response['task_id']
    task = server.ACTIVE_TASKS[task_id]
    assert task['dimensions']['fx_capable'] is uses_fx
    assert task['dimensions']['project_key'] == 'shared'
    assert task['dimensions']['video_provider'] == video_provider
    # These workers pass dimensions back into get_or_create_task; retain capability.
    if route in ('/api/auto_run', '/api/stepped/start'):
        assert dispatch.calls[0][1][2]['fx_capable'] is uses_fx
        assert dispatch.calls[0][1][2]['project_key'] == 'shared'
        assert dispatch.calls[0][1][2]['video_provider'] == video_provider
    server._video_progress(task_id, task, 'request_submitting', {
        'slot': 1, 'submission_id': 'pipeline-submission', 'submission_pending': True})
    receipt, = server._VIDEO_OPERATIONS.submissions(task_id)
    assert receipt['project_key'] == 'shared' and receipt['provider'] == video_provider
    # Cancel uses the saved startup choice even if current preferences differ.
    monkeypatch.setattr(server, 'effective_config', lambda *a: {
        'imageBackend': 'google_fx' if not uses_fx else 'api',
        'videoProvider': 'google_fx' if not uses_fx else 'flow2api'})
    _, status = post('/api/compose-cancel', {'task_id': task_id})
    assert status == 200 and task['cancel_event'].is_set()
    assert builtins.google_fx_cancelled is uses_fx
    assert cancel_request.call_count == int(uses_fx)


def test_cancel_preserves_legacy_pipeline_metadata_fallback(dispatch, monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, 'google_fx_cancelled', False, raising=False)
    monkeypatch.setattr(server, 'get_fx_cancel_flag', lambda: SimpleNamespace(cancel_request=lambda _: False))
    monkeypatch.setattr(server.FX_CONTROL, 'audit', lambda *a, **kw: None)
    task = server.get_or_create_task('staged_legacy', {'type': 'staged_render'})
    _, status = post('/api/compose-cancel', {'task_id': task['id']})
    assert status == 200 and builtins.google_fx_cancelled is True


@pytest.mark.parametrize('route,body', [
    ('/api/auto_run', {'task_id': 'reuse-cancelled', 'dimensions': {'project_key': 'shared'}}),
    ('/api/stepped/advance', {'task_id': 'reuse-cancelled', 'title': 'shared', 'action': 'approve'}),
])
def test_cancelled_pipeline_is_not_reset_before_its_worker_exits(dispatch, route, body):
    task = server.get_or_create_task('reuse-cancelled')
    assert server._claim_flow_project_run(dict(CONFIG), task['id'], 'shared') is None
    task['status'] = 'cancelled'
    task['cancel_event'].set()
    original_event = task['cancel_event']
    _, status = post(route, {**body, 'config': CONFIG})
    assert status == 409 and not dispatch.calls
    assert task['status'] == 'cancelled' and task['cancel_event'] is original_event


@pytest.mark.parametrize('kind', ['auto', 'staged', 'stepped_start', 'stepped_advance'])
@pytest.mark.parametrize('late_result', ['return', 'exception'])
def test_full_pipeline_cannot_overwrite_cancellation(dispatch, monkeypatch, kind, late_result):
    task_id = 'pipeline-late-' + kind
    task = server.get_or_create_task(task_id)
    config = dict(CONFIG)
    assert server._claim_flow_project_run(config, task_id, 'shared') is None

    def finish_after_cancel(*args, **kwargs):
        task['status'] = 'cancelled'
        task['cancel_event'].set()
        task['error'] = 'cancelled by user'
        if late_result == 'exception':
            raise RuntimeError('late transport failure')
        return {'stage': 'completed', 'title': 'shared', 'videos': {}}

    monkeypatch.setattr(pipeline_orchestrator, 'run_autonomous_pipeline', finish_after_cancel)
    monkeypatch.setattr(pipeline_orchestrator, 'run_staged_frame_rendering', finish_after_cancel)
    monkeypatch.setattr(stepped_pipeline, 'start_stepped_pipeline', finish_after_cancel)
    monkeypatch.setattr(stepped_pipeline, 'advance_stepped_pipeline', finish_after_cancel)
    workers = {
        'auto': (server.auto_run_worker, (task_id, config, {'project_key': 'shared'})),
        'staged': (server.render_staged_worker, (task_id, config, 'shared', BODY['prompt_block'])),
        'stepped_start': (server.stepped_pipeline_start_worker, (task_id, config, {})),
        'stepped_advance': (server.stepped_pipeline_advance_worker, (task_id, config, 'shared', 'approve')),
    }
    worker, args = workers[kind]
    worker(*args)
    assert task['status'] == 'cancelled' and task['error'] == 'cancelled by user'
    assert not any(event[0] in ('result', 'stepped_paused') for event in task['events'])
    assert server.claim_frame_run(dispatch.project, 'following-frame') is None
