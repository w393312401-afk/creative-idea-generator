import contextlib
import copy

import pytest
import server

PROMPT = '图片 1:\nstart\n图片 2:\nend\n视频 1:\nmove\n'


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    state = {'browser_busy': False, 'merged': 0, 'chain_deferred': None}
    @contextlib.contextmanager
    def slot(*args):
        state['browser_busy'] = True
        try:
            yield
        finally:
            state['browser_busy'] = False
    def generate(*args, **kwargs):
        assert state['browser_busy']
        state['chain_deferred'] = kwargs.get('auto_merge')
        return copy.deepcopy(state.get('result', {'videos': [{'slot': 1, 'status': 'success'}]}))
    def merge(*args, **kwargs):
        assert not state['browser_busy'], 'local encoding must release browser queue'
        assert callable(kwargs['cancel_check']) and callable(kwargs['on_progress'])
        state['merged'] += 1
        if state.get('cancel_merge'):
            raise ConnectionError('cancelled')
        if state.get('fail_merge'):
            raise RuntimeError('ffmpeg failed')
        kwargs['on_progress']('merge_progress', {'phase': 'encoding', 'elapsed_seconds': 1})
        return {'url': '/test.mp4'}
    monkeypatch.setattr(server, '_fx_browser_slot', slot)
    monkeypatch.setattr(server, 'generate_video_sequence', generate)
    monkeypatch.setattr(server, 'generate_video_chain_sequence', generate)
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
    monkeypatch.setattr(server, 'merge_project_videos', merge)
    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path))
    monkeypatch.setattr(server, 'read_manifest', lambda path: None)
    return state


@pytest.mark.parametrize('chain', [False, True])
def test_encoding_runs_outside_browser_slot(worker_env, chain):
    worker = server.generate_video_chain_worker if chain else server.generate_videos_worker
    worker('videos_worker_test', {}, 'test', PROMPT, [1])
    task = server.ACTIVE_TASKS['videos_worker_test']
    assert task['status'] == 'completed' and task['outcome'] == 'completed'
    assert worker_env['merged'] == 1
    if chain:
        assert worker_env['chain_deferred'] is False
    assert any(evt[0] == 'merge_progress' for evt in task['events'])


def test_old_success_does_not_hide_failed_retry(worker_env):
    worker_env['result'] = {'videos': [{'slot': 1, 'status': 'success', 'retained_previous': True}],
                           'video_generation_stats': {'last_run': {'failed_slots': [1]}}}
    server.generate_videos_worker('videos_failed_retry', {}, 'test', PROMPT, [1])
    task = server.ACTIVE_TASKS['videos_failed_retry']
    assert task['outcome'] == 'partial_failed'
    assert task['result']['has_failures'] is True
    assert worker_env['merged'] == 0


@pytest.mark.parametrize('chain', [False, True])
def test_merge_cancellation_stays_cancelled(worker_env, chain):
    worker_env['cancel_merge'] = True
    worker = server.generate_video_chain_worker if chain else server.generate_videos_worker
    worker('videos_cancel_merge', {}, 'test', PROMPT, [1])
    task = server.ACTIVE_TASKS['videos_cancel_merge']
    assert task['status'] == 'cancelled'
    assert not any(evt[0] == 'result' for evt in task['events'])


def test_failed_merge_reports_partial_failure(worker_env):
    worker_env['fail_merge'] = True
    server.generate_videos_worker('videos_failed_merge', {}, 'test', PROMPT, [1])
    result = server.ACTIVE_TASKS['videos_failed_merge']['result']
    assert result['completion_state'] == 'partial_failed'
    assert result['merge_error'] == 'ffmpeg failed'


def test_cancelled_optimizer_never_starts_generation(worker_env, monkeypatch):
    def cancel(*args, **kwargs):
        raise ConnectionError('cancelled')
    monkeypatch.setattr(server, 'optimize_video_prompts_for_sequence', cancel)
    server.generate_videos_worker('videos_cancel_opt', {}, 'test', PROMPT, [1])
    assert server.ACTIVE_TASKS['videos_cancel_opt']['status'] == 'cancelled'
    assert worker_env['merged'] == 0


@pytest.mark.parametrize('chain', [False, True])
def test_late_exception_cannot_overwrite_confirmed_cancellation(worker_env, monkeypatch, chain):
    def late_error(*args, **kwargs):
        task = server.ACTIVE_TASKS['videos_late_error']
        task['cancel_event'].set()
        task['status'] = 'cancelled'
        task['error'] = '用户取消了视频生成'
        raise RuntimeError('late browser timeout')
    monkeypatch.setattr(server, 'generate_video_chain_sequence' if chain else 'generate_video_sequence', late_error)
    worker = server.generate_video_chain_worker if chain else server.generate_videos_worker
    worker('videos_late_error', {}, 'test', PROMPT, [1])
    task = server.ACTIVE_TASKS['videos_late_error']
    assert task['status'] == 'cancelled'
    assert task['error'] == '用户取消了视频生成'
