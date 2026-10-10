"""Directly retry Flow2API failures while preserving truthful attempt receipts."""
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest

from flow2api_video import Flow2APIVideoError, Flow2APIVideoService


MP4 = b'\x00\x00\x00\x18ftypisom' + b'original-video-and-audio-bytes' * 3


def sse(*payloads):
    return ''.join('data: ' + (p if isinstance(p, str) else json.dumps(p)) + '\n\n'
                   for p in payloads).encode()


def stream_response(*payloads):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          content=sse(*payloads))


def success_response(req, with_identity=False):
    payloads = []
    if with_identity:
        submission_id = req.headers['X-SPARK-Submission-ID']
        payloads.append({'submission': {'client_submission_id': submission_id,
            'operation_id': 'success-' + submission_id, 'account_id': 'new-account',
            'upstream_accepted': True}})
    payloads.extend([{'choices': [{'delta': {'content':
        "<video src='/tmp/synthetic.mp4'></video>"}, 'finish_reason': 'stop'}]}, '[DONE]'])
    return stream_response(*payloads)


def business_failure(req, kind='submission_not_started', **overrides):
    error = {'code': kind, 'submission_pending': False, 'upstream_accepted': False}
    if kind == 'upstream_refused':
        error['code'] = 'submission_rejected'
    elif kind == 'upstream_failed':
        submission_id = req.headers['X-SPARK-Submission-ID']
        error.update(code='video_generation_failed', upstream_accepted=True,
            client_submission_id=submission_id, operation_id='failed-' + submission_id,
            account_id='old-account', reason='synthetic_failure')
    error.update(overrides)
    return stream_response({'error': error})


def request(tmp_path, index=0):
    return {'prompt': f'clip {index}', 'model': 'Omni Flash', 'ratio': '16:9',
            'duration': '10', 'resolution': '360p', 'output_path': str(tmp_path / str(index))}


def clip_index(req):
    content = json.loads(req.content)['messages'][0]['content'][0]['text']
    return int(content.split()[-1])


def service(monkeypatch, handler, **config):
    monkeypatch.setattr('flow2api_video.shutil.which', lambda tool: '/synthetic/' + tool)
    adapter = Flow2APIVideoService({'flow2apiApiKey': 'test-key',
        'flow2apiVideoTimeoutSeconds': 2, 'flow2apiVideoConcurrency': 1, 'videoRetryCount': 2, **config})
    adapter._client_factory = lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kwargs)
    adapter._validate_video = AsyncMock(return_value={
        'duration': 10, 'width': 640, 'height': 360, 'has_audio': True})
    return adapter


def record_events(events):
    return lambda index, stage, details: events.append((index, stage, dict(details)))


@pytest.mark.parametrize('kind', ['submission_not_started', 'upstream_failed', 'upstream_refused'])
def test_settled_failure_retries_immediately_then_delivers_once(tmp_path, monkeypatch, kind):
    calls, events = [], []

    def handler(req):
        calls.append(req)
        if req.method == 'GET':
            assert 'authorization' not in req.headers
            return httpx.Response(200, content=MP4)
        if len([call for call in calls if call.method == 'POST']) == 1:
            return business_failure(req, kind)
        return success_response(req)

    adapter = service(monkeypatch, handler)
    result = adapter.generate_videos_batch_google_fx([request(tmp_path)], record_events(events))[0]
    posts = [call for call in calls if call.method == 'POST']
    assert adapter.max_retries == 2
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert result['status'] == 'success'
    assert result['confirmed'] is True and result['submission_pending'] is False
    assert Path(result['video_url']).read_bytes() == MP4
    assert result['submission_id'] == posts[1].headers['X-SPARK-Submission-ID']
    assert len([1 for _, stage, _ in events if stage == 'video_done']) == 1
    assert not any(stage == 'video_error' for _, stage, _ in events)
    warnings = [details for _, stage, details in events if stage == 'video_warning']
    assert len(warnings) == 1
    assert warnings[0]['code'] == 'flow2api_auto_retry'
    assert warnings[0]['retry'] == 1 and warnings[0]['max_retries'] == 2
    old_resolved = [details for _, stage, details in events if stage == 'request_resolved'
                    and details['submission_id'] == posts[0].headers['X-SPARK-Submission-ID']]
    assert len(old_resolved) == 1
    assert old_resolved[0]['submission_pending'] is False and old_resolved[0]['confirmed'] is False


@pytest.mark.parametrize('kind', ['submission_not_started', 'upstream_failed', 'upstream_refused'])
def test_retry_limit_is_two_and_only_final_failure_is_reported(tmp_path, monkeypatch, kind):
    calls, events = [], []

    def handler(req):
        calls.append(req)
        assert req.method == 'POST'
        return business_failure(req, kind)

    result = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path)], record_events(events))[0]
    assert len(calls) == 3
    assert len({req.headers['X-SPARK-Submission-ID'] for req in calls}) == 3
    assert result['status'] == 'failed' and result['code'] == kind
    assert result['submission_pending'] is False and result['confirmed'] is False
    assert result['submission_id'] == calls[-1].headers['X-SPARK-Submission-ID']
    warnings = [details for _, stage, details in events if stage == 'video_warning']
    assert [warning['retry'] for warning in warnings] == [1, 2]
    assert all(warning['code'] == 'flow2api_auto_retry' and warning['max_retries'] == 2
               for warning in warnings)
    errors = [details for _, stage, details in events if stage == 'video_error']
    assert errors == [result]
    assert list(tmp_path.rglob('*.mp4')) == []


def test_each_retry_settles_old_receipt_before_new_post_and_clears_old_identity(tmp_path, monkeypatch):
    posts, events = [], []

    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        if posts:
            previous_id = posts[-1].headers['X-SPARK-Submission-ID']
            assert any(stage == 'request_resolved' and details['submission_id'] == previous_id
                       and details['submission_pending'] is False
                       for _, stage, details in events)
        posts.append(req)
        return business_failure(req, 'upstream_failed') if len(posts) < 3 else success_response(req, True)

    result = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path)], record_events(events))[0]
    assert result['status'] == 'success'
    identities = [req.headers['X-SPARK-Submission-ID'] for req in posts]
    assert len(identities) == len(set(identities)) == 3
    for identity in identities:
        uuid.UUID(identity)
    submitting = [details for _, stage, details in events if stage == 'request_submitting']
    assert [details['submission_id'] for details in submitting] == identities
    for details in submitting:
        assert details['submission_pending'] is True and details['confirmed'] is False
        assert details['upstream_accepted'] is False
        assert details['prompt_hash'] == hashlib.sha256(b'clip 0').hexdigest()
        assert not {'operation_id', 'client_submission_id', 'upstream_account_id',
                    'upstream_error_code', 'upstream_reason'} & details.keys()
    assert result['operation_id'] == 'success-' + identities[-1]
    assert result['client_submission_id'] == identities[-1]
    assert result['upstream_account_id'] == 'new-account'
    assert not {'upstream_error_code', 'upstream_reason'} & result.keys()
    for previous, following in zip(identities, identities[1:]):
        settled_position = next(i for i, (_, stage, details) in enumerate(events)
            if stage == 'request_resolved' and details['submission_id'] == previous)
        new_position = next(i for i, (_, stage, details) in enumerate(events)
            if stage == 'request_submitting' and details['submission_id'] == following)
        assert settled_position < new_position


def test_stale_first_attempt_identity_cannot_resolve_second_attempt(tmp_path, monkeypatch):
    posts = []

    def handler(req):
        posts.append(req)
        if len(posts) == 1:
            return business_failure(req, 'upstream_failed')
        return stream_response({'submission': {
            'client_submission_id': posts[0].headers['X-SPARK-Submission-ID'],
            'operation_id': 'old-operation', 'upstream_accepted': True}})

    result = service(monkeypatch, handler).generate_videos_batch_google_fx([request(tmp_path)])[0]
    assert len(posts) == 2
    assert result['code'] == 'stream_invalid' and result['submission_pending'] is True
    assert result['submission_id'] == posts[-1].headers['X-SPARK-Submission-ID']
    assert 'operation_id' not in result and 'client_submission_id' not in result
    assert list(tmp_path.rglob('*.mp4')) == []


@pytest.mark.parametrize('cancel_mode', ['cancel_check', 'callback'])
def test_cancellation_from_retry_warning_prevents_next_post(tmp_path, monkeypatch, cancel_mode):
    calls, events = [], []
    state = {'cancel': False}

    def handler(req):
        calls.append(req)
        return business_failure(req)

    def callback(index, stage, details):
        events.append((index, stage, dict(details)))
        if stage == 'video_warning':
            state['cancel'] = True
            if cancel_mode == 'callback':
                raise ConnectionError('cancel at retry warning')

    with pytest.raises(ConnectionError):
        service(monkeypatch, handler).generate_videos_batch_google_fx(
            [request(tmp_path), request(tmp_path, 1)], callback,
            cancel_check=lambda: state['cancel'])
    assert len(calls) == 1
    assert any(stage == 'request_resolved' and details['submission_pending'] is False
               for _, stage, details in events)
    assert not any(stage == 'video_error' for _, stage, _ in events)
    assert list(tmp_path.rglob('*.mp4')) == []


@pytest.mark.parametrize('failed_stage', ['request_resolved', 'video_warning'])
def test_retry_checkpoint_failure_aborts_batch_without_second_post(tmp_path, monkeypatch, failed_stage):
    calls, events = [], []

    def handler(req):
        calls.append(req)
        return business_failure(req)

    def callback(index, stage, details):
        events.append((index, stage, dict(details)))
        if stage == failed_stage:
            raise RuntimeError('checkpoint write failed')

    with pytest.raises(Flow2APIVideoError) as raised:
        service(monkeypatch, handler).generate_videos_batch_google_fx(
            [request(tmp_path), request(tmp_path, 1)], callback)
    assert raised.value.code == 'callback'
    assert len(calls) == 1
    assert not any(stage == 'video_error' for _, stage, _ in events)
    assert list(tmp_path.rglob('*.mp4')) == []


def test_recovered_refusal_does_not_stop_later_slots(tmp_path, monkeypatch):
    attempts = Counter()

    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        index = clip_index(req)
        attempts[index] += 1
        if index == 0 and attempts[index] == 1:
            return business_failure(req, 'upstream_refused')
        return success_response(req)

    adapter = service(monkeypatch, handler)
    results = adapter.generate_videos_batch_google_fx([request(tmp_path, i) for i in range(2)])
    assert [result['status'] for result in results] == ['success', 'success']
    assert attempts == {0: 2, 1: 1}
    assert adapter._stopped_by is None


def test_exhausted_refusal_stops_untouched_slots_after_all_retries(tmp_path, monkeypatch):
    posted, events = [], []

    def handler(req):
        posted.append(clip_index(req))
        return business_failure(req, 'upstream_refused')

    results = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path, i) for i in range(3)], record_events(events))
    assert posted == [0, 0, 0]
    assert results[0]['code'] == 'upstream_refused'
    assert all(result['code'] == 'batch_stopped' and result['stopped_by'] == 'upstream_refused'
               and 'submission_id' not in result for result in results[1:])
    assert len([1 for _, stage, _ in events if stage == 'video_warning']) == 2


@pytest.mark.parametrize('failure,expected', [
    (401, 'authorization'), (403, 'access_denied'),
    ('unavailable', 'upstream_unavailable'), ('pending', 'upstream_pending'),
    ('auth_pending', 'upstream_auth_pending'), ('disconnect', 'transport'),
])
def test_only_settled_transport_and_account_failures_retry(tmp_path, monkeypatch, failure, expected):
    posted, events = [], []

    def handler(req):
        posted.append(req)
        if isinstance(failure, int):
            return httpx.Response(failure)
        if failure == 'disconnect':
            raise httpx.ReadError('lost response', request=req)
        if failure == 'unavailable':
            return business_failure(req, 'no_available_token')
        if failure == 'auth_pending':
            return business_failure(req, 'video_status_unknown', submission_pending=True,
                upstream_accepted=True, reason='poll_auth_failed', operation_id='pending-operation',
                client_submission_id=req.headers['X-SPARK-Submission-ID'])
        return stream_response({'error': {'code': 'video_generation_failed', 'message': 'unknown result'}})

    result = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path)], record_events(events))[0]
    attempts = 3 if failure in (401, 403, 'unavailable') else 1
    assert len(posted) == attempts
    assert len({req.headers['X-SPARK-Submission-ID'] for req in posted}) == attempts
    assert result['code'] == expected
    assert [details['retry'] for _, stage, details in events if stage == 'video_warning'] == list(range(1, attempts))
    assert len([1 for _, stage, _ in events if stage == 'video_error']) == 1


@pytest.mark.parametrize('overrides', [
    {'submission_pending': True}, {'client_submission_id': None}, {'operation_id': None},
])
def test_generation_failure_without_settled_matching_receipt_does_not_retry(tmp_path, monkeypatch, overrides):
    posted = []

    def handler(req):
        posted.append(req)
        return business_failure(req, 'upstream_failed', **overrides)

    result = service(monkeypatch, handler).generate_videos_batch_google_fx([request(tmp_path)])[0]
    assert len(posted) == 1
    assert result['code'] == 'upstream_pending' and result['submission_pending'] is True


@pytest.mark.parametrize('outcome', ['pending', 'timeout'])
def test_retry_that_becomes_unresolved_stops_before_third_post(tmp_path, monkeypatch, outcome):
    posted, events = [], []

    async def handler(req):
        posted.append(req)
        if len(posted) == 1:
            return business_failure(req)
        if outcome == 'timeout':
            await asyncio.Event().wait()
        return stream_response({'error': {'code': 'video_status_unknown', 'submission_pending': True,
            'upstream_accepted': True, 'operation_id': 'second-operation',
            'client_submission_id': req.headers['X-SPARK-Submission-ID']}})

    adapter = service(monkeypatch, handler, flow2apiVideoTimeoutSeconds=0.05)
    result = adapter.generate_videos_batch_google_fx([request(tmp_path)], record_events(events))[0]
    assert len(posted) == 2
    assert result['code'] == ('timeout' if outcome == 'timeout' else 'upstream_pending')
    assert result['submission_pending'] is True and result['confirmed'] is False
    assert result['submission_id'] == posted[-1].headers['X-SPARK-Submission-ID']
    assert len([1 for _, stage, _ in events if stage == 'video_warning']) == 1
    assert len([1 for _, stage, _ in events if stage == 'video_error']) == 1


def test_concurrent_retries_keep_slot_order_and_selected_stream_limit(tmp_path, monkeypatch):
    first_window, retry_window = asyncio.Event(), asyncio.Event()
    attempts, events, posted = Counter(), [], []
    active, peak = 0, 0

    async def handler(req):
        nonlocal active, peak
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        index = clip_index(req)
        attempts[index] += 1
        attempt = attempts[index]
        posted.append((index, attempt, req.headers['X-SPARK-Submission-ID']))
        active += 1
        peak = max(peak, active)
        try:
            if index < 3:
                gate = first_window if attempt == 1 else retry_window
                if sum(attempts[i] >= attempt for i in range(3)) == 3:
                    gate.set()
                await gate.wait()
            return business_failure(req) if attempt == 1 else success_response(req)
        finally:
            active -= 1

    adapter = service(monkeypatch, handler, flow2apiVideoConcurrency=3)
    results = adapter.generate_videos_batch_google_fx(
        [request(tmp_path, i) for i in range(7)], record_events(events))
    assert peak == 3 and active == 0
    assert attempts == {index: 2 for index in range(7)}
    assert len({identity for _, _, identity in posted}) == 14
    assert len([1 for _, stage, _ in events if stage == 'video_warning']) == 7
    assert not any(stage == 'video_error' for _, stage, _ in events)
    for index, result in enumerate(results):
        assert result['status'] == 'success'
        assert result['prompt_hash'] == hashlib.sha256(f'clip {index}'.encode()).hexdigest()
        assert Path(result['video_url']).parent == tmp_path / str(index)
        own_posts = [identity for slot, _, identity in posted if slot == index]
        assert result['submission_id'] == own_posts[-1]
        own_submissions = [details['submission_id'] for slot, stage, details in events
                           if slot == index and stage == 'request_submitting']
        assert own_submissions == own_posts


@pytest.mark.parametrize('failure', ['pending', 'auth_pending', 'disconnect', 'timeout'])
def test_unresolved_attempt_keeps_original_receipt_without_resubmission(
        tmp_path, monkeypatch, failure):
    posts, events = [], []

    async def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        posts.append(req)
        if len(posts) == 1:
            if failure == 'disconnect':
                raise httpx.ReadError('response connection lost', request=req)
            if failure == 'timeout':
                await asyncio.Event().wait()
            return business_failure(req, 'video_status_unknown', submission_pending=True,
                upstream_accepted=True, operation_id='original-pending-operation',
                client_submission_id=req.headers['X-SPARK-Submission-ID'],
                reason='poll_auth_failed' if failure == 'auth_pending' else 'poll_failed')
        return success_response(req, with_identity=True)

    adapter = service(monkeypatch, handler, flow2apiVideoTimeoutSeconds=0.04)
    result = adapter.generate_videos_batch_google_fx([request(tmp_path)], record_events(events))[0]
    assert len(posts) == 1
    original_id = posts[0].headers['X-SPARK-Submission-ID']
    assert result['status'] == 'failed' and result['submission_id'] == original_id
    assert result['submission_pending'] is True and result['confirmed'] is False
    assert not any(stage == 'request_resolved' for _, stage, _ in events)
    assert not any(stage in ('video_warning', 'video_done') for _, stage, _ in events)
    assert [details for _, stage, details in events if stage == 'video_error'] == [result]
    if failure in ('pending', 'auth_pending'):
        assert result['operation_id'] == 'original-pending-operation'


def test_pending_attempt_fails_without_resubmission_or_false_settlement(tmp_path, monkeypatch):
    posts, events = [], []

    def handler(req):
        posts.append(req)
        assert req.method == 'POST'
        identity = req.headers['X-SPARK-Submission-ID']
        return business_failure(req, 'video_status_unknown', submission_pending=True,
            upstream_accepted=True, operation_id='pending-' + identity,
            client_submission_id=identity)

    result = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path)], record_events(events))[0]
    identities = [req.headers['X-SPARK-Submission-ID'] for req in posts]
    assert len(identities) == len(set(identities)) == 1
    assert result['status'] == 'failed' and result['code'] == 'upstream_pending'
    assert result['submission_pending'] is True and result['confirmed'] is False
    assert result['submission_id'] == identities[-1]
    assert result['operation_id'] == 'pending-' + identities[-1]
    assert not any(stage == 'request_resolved' for _, stage, _ in events)
    receipts = [details for _, stage, details in events if stage == 'request_submitting']
    assert [details['submission_id'] for details in receipts] == identities
    assert all(details['submission_pending'] is True and details['confirmed'] is False
               for details in receipts)
    assert [details['retry'] for _, stage, details in events if stage == 'video_warning'] == []
    assert [details for _, stage, details in events if stage == 'video_error'] == [result]


def test_each_attempt_gets_full_timeout_despite_previous_elapsed_time(tmp_path, monkeypatch):
    posts = []

    async def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        posts.append(req)
        # Each attempt is below its 100 ms deadline; all three together exceed it.
        await asyncio.sleep(0.06)
        return business_failure(req) if len(posts) < 3 else success_response(req)

    result = service(monkeypatch, handler, flow2apiVideoTimeoutSeconds=0.1).generate_videos_batch_google_fx(
        [request(tmp_path)])[0]
    assert len(posts) == 3
    assert result['status'] == 'success'
    assert result['submission_id'] == posts[-1].headers['X-SPARK-Submission-ID']


def test_rejected_delivery_retries_with_callback_updated_prompt_and_new_identity(tmp_path, monkeypatch):
    posts, events, rejected_files = [], [], []
    item = request(tmp_path)

    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        posts.append(req)
        return success_response(req)

    def callback(index, stage, details):
        events.append((index, stage, dict(details)))
        if stage == 'video_done' and len(posts) == 1:
            rejected_files.append(Path(details['video_url']))
            item['prompt'] = 'adapted prompt after project validation'
            return 'rejected'

    result = service(monkeypatch, handler).generate_videos_batch_google_fx([item], callback)[0]
    assert len(posts) == 2
    first, second = [req.headers['X-SPARK-Submission-ID'] for req in posts]
    assert first != second and result['submission_id'] == second
    prompts = [json.loads(req.content)['messages'][0]['content'][0]['text'] for req in posts]
    assert prompts == ['clip 0', 'adapted prompt after project validation']
    assert result['status'] == 'success'
    assert result['prompt_hash'] == hashlib.sha256(prompts[-1].encode()).hexdigest()
    assert all(not path.exists() for path in rejected_files)
    assert Path(result['video_url']).read_bytes() == MP4
    assert len(list(tmp_path.rglob('*.mp4'))) == 1
    assert len([1 for _, stage, _ in events if stage == 'video_warning']) == 1
    assert not any(stage == 'video_error' for _, stage, _ in events)


@pytest.mark.parametrize('failure', ['download', 'video_invalid'])
def test_local_delivery_failure_directly_regenerates_and_delivers_clean_file(
        tmp_path, monkeypatch, failure):
    posts, downloads, events = [], [], []

    def handler(req):
        if req.method == 'GET':
            downloads.append(req)
            if failure == 'download' and len(downloads) == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=MP4)
        posts.append(req)
        return success_response(req)

    adapter = service(monkeypatch, handler)
    if failure == 'video_invalid':
        adapter._validate_video.side_effect = [Flow2APIVideoError('video_invalid'),
            {'duration': 10, 'width': 640, 'height': 360, 'has_audio': True}]
    result = adapter.generate_videos_batch_google_fx([request(tmp_path)], record_events(events))[0]
    assert len(posts) == len(downloads) == 2
    assert result['status'] == 'success'
    assert result['submission_id'] == posts[-1].headers['X-SPARK-Submission-ID']
    assert Path(result['video_url']).read_bytes() == MP4
    assert len(list(tmp_path.rglob('*.mp4'))) == 1
    assert not list(tmp_path.rglob('*.part.mp4'))
    assert len([1 for _, stage, _ in events if stage == 'video_warning']) == 1
    assert len([1 for _, stage, _ in events if stage == 'video_done']) == 1
