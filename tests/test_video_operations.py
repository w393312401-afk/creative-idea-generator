import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
import server
from video_operations import VideoOperationStore, VideoOperationConflict, request_fingerprint


BODY = {'request_id': 'video-request-123', 'title': 'operation-test', 'prompt_block': '视频 1:\nMove', 'target_slots': [1]}


def handler(path, body=None):
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h._gate = lambda *a, **k: True
    h._read_json_body = lambda: dict(body or {})
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


@pytest.fixture
def dispatch(monkeypatch):
    calls = []
    class FakeThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            calls.append((self.target, self.args))
    monkeypatch.setattr(server, 'threading', SimpleNamespace(**{**vars(threading), 'Thread': FakeThread}))
    monkeypatch.setattr(server, '_client_ip', lambda h: 'test')
    monkeypatch.setattr(server, 'rate_ok', lambda *a: True)
    monkeypatch.setattr(server, '_require_fx_admission', lambda h: True)
    monkeypatch.setattr(server, 'effective_config', lambda cfg: dict(cfg or {}))
    monkeypatch.setattr(server, 'read_manifest', lambda p: None)
    return calls


def test_parallel_identical_posts_dispatch_once(dispatch):
    def post(_):
        h, sent = handler('/api/generate_videos', BODY)
        h.do_POST()
        return sent[0]
    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(post, range(6)))
    assert {r[1] for r in responses} == {200}
    assert len({r[0]['task_id'] for r in responses}) == 1
    assert len(dispatch) == 1
    task = server.ACTIVE_TASKS[responses[0][0]['task_id']]
    assert task['dimensions']['request_id'] == BODY['request_id']
    assert task['dimensions']['target_slots'] == [1]


def test_request_survives_store_recreation_and_rejects_changed_body(tmp_path):
    path = str(tmp_path / 'ops.sqlite3')
    first, created = VideoOperationStore(path).reserve(BODY['request_id'], request_fingerprint(BODY, 'videos'))
    assert created
    recovered, created = VideoOperationStore(path).reserve(BODY['request_id'], request_fingerprint(dict(reversed(list(BODY.items()))), 'videos'))
    assert not created and first['task_id'] == recovered['task_id']
    with pytest.raises(VideoOperationConflict):
        VideoOperationStore(path).reserve(BODY['request_id'], request_fingerprint({**BODY, 'target_slots': [2]}, 'videos'))


def test_post_conflict_and_lookup(dispatch):
    h, first = handler('/api/generate_videos', BODY); h.do_POST()
    h, conflict = handler('/api/generate_videos', {**BODY, 'target_slots': [2]}); h.do_POST()
    assert conflict[0][1] == 409
    h, lookup = handler('/api/video-operation?request_id=' + BODY['request_id']); h.do_GET()
    assert lookup[0][0]['task_id'] == first[0][0]['task_id']
    assert len(dispatch) == 1


def test_cancel_before_registration_blocks_late_post(dispatch):
    h, cancelled = handler('/api/compose-cancel', {'request_id': BODY['request_id']}); h.do_POST()
    assert cancelled[0][1] == 200
    h, late = handler('/api/generate_videos', BODY); h.do_POST()
    assert late[0][0] == {'status': 'cancelled', 'request_id': BODY['request_id'], 'task_id': None}
    assert not dispatch
    h, lookup = handler('/api/video-operation?request_id=' + BODY['request_id']); h.do_GET()
    assert lookup[0][0]['status'] == 'cancelled'


def test_cancel_after_registration_resolves_task_by_request(dispatch):
    h, sent = handler('/api/generate_videos', BODY); h.do_POST()
    h, cancelled = handler('/api/compose-cancel', {'request_id': BODY['request_id']}); h.do_POST()
    task = server.ACTIVE_TASKS[sent[0][0]['task_id']]
    assert task['cancel_event'].is_set()
    assert task['status'] == 'cancelled'
    h, lookup = handler('/api/video-operation?request_id=' + BODY['request_id']); h.do_GET()
    assert lookup[0][0]['cancel_requested'] is True


def test_cannot_cancel_different_task_with_request_id(dispatch):
    h, sent = handler('/api/generate_videos', BODY); h.do_POST()
    h, cancelled = handler('/api/compose-cancel', {'request_id': BODY['request_id'], 'task_id': 'other'}); h.do_POST()
    assert cancelled[0][1] == 409
    assert not server.ACTIVE_TASKS[sent[0][0]['task_id']]['cancel_event'].is_set()


def test_failed_registration_never_dispatches_or_retries_as_new(dispatch, monkeypatch):
    monkeypatch.setattr(server, 'save_task_to_disk', lambda *a: False)
    h, sent = handler('/api/generate_videos', BODY); h.do_POST()
    assert sent[0][1] == 500
    h, retry = handler('/api/generate_videos', BODY); h.do_POST()
    assert retry[0][0]['task_status'] == 'failed'
    assert not dispatch


def test_existing_request_recovers_while_admission_closed(dispatch, monkeypatch):
    h, first = handler('/api/generate_videos', BODY); h.do_POST()
    monkeypatch.setattr(server, '_require_fx_admission', lambda h: pytest.fail('must resolve existing first'))
    h, second = handler('/api/generate_videos', BODY); h.do_POST()
    assert first[0][0]['task_id'] == second[0][0]['task_id']


def test_flow2api_dispatch_bypasses_ads_admission_and_account(dispatch, monkeypatch):
    monkeypatch.setattr(server, '_require_fx_admission', lambda h: pytest.fail('Flow2API uses its own service'))
    h, sent = handler('/api/generate_videos', {**BODY, 'config': {
        'videoProvider': 'flow2api', 'googleFxUserId': 'must-not-close-ads'}})
    h.do_POST()
    assert sent[0][1] == 200
    assert len(dispatch) == 1
    assert server.ACTIVE_TASKS[sent[0][0]['task_id']]['dimensions']['userId'] is None
    assert server.ACTIVE_TASKS[sent[0][0]['task_id']]['dimensions']['video_provider'] == 'flow2api'


def test_new_flow_request_retries_pending_slot_and_original_request_still_recovers(dispatch, monkeypatch):
    body = {**BODY, 'config': {'videoProvider': 'flow2api'}}
    h, first = handler('/api/generate_videos', body); h.do_POST()
    task_id = first[0][0]['task_id']
    receipt = {'slot': 1, 'submission_id': 'pending-submission',
               'submission_pending': True, 'confirmed': False}
    VideoOperationStore().record_submission(task_id, receipt)
    # An older successful artifact does not settle its latest pending attempt.
    monkeypatch.setattr(server, 'read_manifest', lambda p: {'videos': [
        {'slot': 1, 'status': 'success', 'provider': 'flow2api', 'last_attempt': receipt}]})
    # A finished worker releases its project claim even if the receipt is pending.
    server.ACTIVE_TASKS[task_id]['status'] = 'failed'
    server._release_flow_project_run(dispatch[0][1][1], task_id)
    h, retry = handler('/api/generate_videos', {**body, 'request_id': 'new-request-123'}); h.do_POST()
    assert retry[0][1] == 200
    assert retry[0][0]['task_id'] != task_id
    assert server._VIDEO_OPERATIONS.lookup('new-request-123')['task_id'] == retry[0][0]['task_id']
    assert VideoOperationStore().submissions(task_id)[0]['submission_pending'] is True
    h, recovered = handler('/api/generate_videos', body); h.do_POST()
    assert recovered[0][1] == 200 and recovered[0][0]['task_id'] == task_id
    assert len(dispatch) == 2


@pytest.mark.parametrize('route', ['/api/generate_videos', '/api/generate_video_chain'])
@pytest.mark.parametrize('receipt_kind', ['flow2api', 'legacy_adapter', 'fixed_native'])
def test_journal_pending_policy_without_manifest(dispatch, route, receipt_kind):
    task_id = 'videos_previous_paid_task'
    provider = 'google_fx' if receipt_kind == 'fixed_native' else 'flow2api'
    server.get_or_create_task(task_id, {'project_key': BODY['title'],
        'video_provider': provider, 'request_id': 'previous-request-123'})
    receipt = {'slot': 1, 'submission_id': 'pending-submission', 'submission_pending': True}
    if receipt_kind == 'fixed_native':
        receipt.update(provider=provider, fixed_video_account=True, account_id='only-profile')
    elif receipt_kind == 'flow2api':
        receipt['provider'] = provider
    VideoOperationStore().record_submission(task_id, receipt)
    h, sent = handler(route, {**BODY, 'config': {'videoProvider': provider}}); h.do_POST()
    if receipt_kind == 'fixed_native':
        assert sent[0][1] == 409
        assert sent[0][0]['failure_code'] == 'SUBMISSION_PENDING'
        assert sent[0][0]['pending_submissions'][0]['task_id'] == task_id
        assert sent[0][0]['pending_submissions'][0]['provider'] == 'google_fx'
        assert not dispatch
    else:
        assert sent[0][1] == 200
        assert len(dispatch) == 1
    assert VideoOperationStore().submissions(task_id)[0]['submission_pending'] is True


def test_flow_direct_retry_waits_for_current_worker_to_exit(dispatch):
    body = {**BODY, 'config': {'videoProvider': 'flow2api'}}
    h, first = handler('/api/generate_videos', body); h.do_POST()
    task_id = first[0][0]['task_id']
    VideoOperationStore().record_submission(task_id, {
        'slot': 1, 'provider': 'flow2api', 'submission_id': 'pending-submission',
        'submission_pending': True})
    h, retry = handler('/api/generate_videos', {**body, 'request_id': 'new-request-123'}); h.do_POST()
    assert retry[0][1] == 409
    assert retry[0][0]['failure_code'] == 'PROJECT_BUSY'
    assert retry[0][0]['task_id'] == task_id
    assert len(dispatch) == 1


@pytest.mark.parametrize('provider,expected', [('flow2api', False), ('google_fx', True)])
def test_flow_cancel_does_not_change_google_fx_global(dispatch, monkeypatch, provider, expected):
    import builtins
    monkeypatch.setattr(builtins, 'google_fx_cancelled', False, raising=False)
    monkeypatch.setattr(server, 'get_fx_cancel_flag', lambda: SimpleNamespace(cancel_request=lambda _: False))
    monkeypatch.setattr(server.FX_CONTROL, 'audit', lambda *a, **kw: None)
    h, sent = handler('/api/generate_videos', {**BODY, 'config': {'videoProvider': provider}}); h.do_POST()
    task_id = sent[0][0]['task_id']
    h, cancelled = handler('/api/compose-cancel', {'task_id': task_id}); h.do_POST()
    assert cancelled[0][1] == 200
    assert server.ACTIVE_TASKS[task_id]['cancel_event'].is_set()
    assert builtins.google_fx_cancelled is expected


def test_progress_journals_project_scope_before_cancellation(dispatch):
    h, sent = handler('/api/generate_videos', {**BODY, 'config': {'videoProvider': 'flow2api'}}); h.do_POST()
    task_id = sent[0][0]['task_id']
    task = server.ACTIVE_TASKS[task_id]
    task['cancel_event'].set()
    with pytest.raises(ConnectionError):
        server._video_progress(task_id, task, 'request_submitting', {
            'index': 1, 'submission_id': 'submission-123', 'submission_pending': True})
    receipt, = VideoOperationStore().submissions(task_id)
    assert receipt['project_key'] == BODY['title']
    assert receipt['provider'] == 'flow2api'
    assert receipt['request_id'] == BODY['request_id']


@pytest.mark.parametrize('terminal', ['request_resolved', 'video_done', 'video_error'])
def test_flow2api_terminal_receipt_replaces_pending_even_after_cancel(monkeypatch, terminal):
    task = server.get_or_create_task('receipt-terminal-' + terminal)
    receipt = {'index': 1, 'submission_id': 'same-submission', 'prompt_hash': 'same-prompt',
               'confirmed': False, 'submission_pending': True}
    server._video_progress(task['id'], task, 'request_submitting', receipt)
    task['cancel_event'].set()
    resolved = {**receipt, 'confirmed': True, 'submission_pending': False}
    details = resolved if terminal == 'request_resolved' else {
        'index': 1, **({'video': {'last_attempt': resolved}} if terminal == 'video_done'
                     else {'last_attempt': resolved})}
    with pytest.raises(ConnectionError):
        server._video_progress(task['id'], task, terminal, details)
    saved, = VideoOperationStore().submissions(task['id'])
    assert saved['submission_id'] == 'same-submission'
    assert saved['confirmed'] is True
    assert saved['submission_pending'] is False


def test_submission_receipt_persists_even_if_cancelled(monkeypatch):
    task = server.get_or_create_task('videos_receipt_test')
    task['cancel_event'].set()
    details = {'index': 53, 'account_id': 'a', 'project_url': 'project', 'tile_id': None,
               'submission_pending': True, 'confirmed': False, 'prompt_hash': 'hash', 'password': 'excluded'}
    with pytest.raises(ConnectionError):
        server._video_progress(task['id'], task, 'request_submitted', details)
    receipt, = VideoOperationStore().submissions(task['id'])
    assert receipt['slot'] == 53
    assert receipt['submission_pending'] is True
    assert 'password' not in receipt


def test_sse_resumes_after_last_delivered_event():
    task = server.get_or_create_task('videos_stream_test', {'type': 'videos'})
    task.update(status='completed', events=[('video_done', {'slot': 1}), ('video_done', {'slot': 2}), ('result', {})])
    h, _ = handler('/api/compose-stream')
    emitted = []
    stop = threading.Event()
    h._stream_task_events(task, lambda t, d, event_id=None: emitted.append((t, event_id)), stop, '1')
    assert emitted == [('video_done', 2), ('result', 3)]
    assert stop.is_set() and not task['listeners']


def test_sse_catches_event_appended_during_replay():
    task = server.get_or_create_task('videos_stream_race', {'type': 'videos'})
    task['events'] = [('video_done', {'slot': 1})]
    emitted = []
    def send(t, d, event_id=None):
        emitted.append((t, event_id))
        if event_id == 1:
            task['events'].append(('result', {}))
            task['status'] = 'completed'
    h, _ = handler('/api/compose-stream')
    h._stream_task_events(task, send, threading.Event())
    assert emitted == [('video_done', 1), ('result', 2)]
