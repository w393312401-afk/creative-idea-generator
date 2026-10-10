"""Exercise parallel HTTP streams, rolling refill, and paid-receipt safety."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from flow2api_video import Flow2APIVideoError, Flow2APIVideoService


MP4 = b'\x00\x00\x00\x18ftypisom' + b'original-video-and-audio-bytes' * 3


def sse(*payloads):
    return ''.join('data: ' + (p if isinstance(p, str) else json.dumps(p)) + '\n\n' for p in payloads).encode()


def success():
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(
        {'choices': [{'delta': {'content': "<video src='/tmp/synthetic.mp4'></video>"}, 'finish_reason': 'stop'}]}, '[DONE]'))


def request(tmp_path, index):
    return {'prompt': f'clip {index}', 'model': 'Omni Flash', 'ratio': '16:9',
            'duration': '10', 'resolution': '360p', 'output_path': str(tmp_path / str(index))}


def clip_index(request):
    prompt = json.loads(request.content)['messages'][0]['content'][0]['text']
    return int(prompt.split()[-1])


def service(monkeypatch, handler, **config):
    monkeypatch.setattr('flow2api_video.shutil.which', lambda tool: '/synthetic/' + tool)
    result = Flow2APIVideoService({'flow2apiApiKey': 'test-key', 'flow2apiVideoTimeoutSeconds': 2, **config})
    result._client_factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    result._validate_video = AsyncMock(return_value={'duration': 10, 'width': 640, 'height': 360, 'has_audio': True})
    return result


def test_default_three_streams_refill_before_slowest_finishes_and_keep_slot_order(tmp_path, monkeypatch):
    posted, completed, events = [], [], []
    initial_window, refill = asyncio.Event(), asyncio.Event()
    active, peak = 0, 0

    async def handler(req):
        nonlocal active, peak
        if req.method == 'GET':
            assert 'authorization' not in req.headers
            return httpx.Response(200, content=MP4)
        index = clip_index(req)
        posted.append(index)
        active += 1
        peak = max(peak, active)
        if len(posted) == 3:
            initial_window.set()
        if index == 3:
            refill.set()
        await initial_window.wait()
        if index == 0:
            await refill.wait()
        active -= 1
        return success()

    def callback(index, stage, details):
        events.append((index, stage, details))
        if stage == 'video_done':
            completed.append(index)

    adapter = service(monkeypatch, handler)
    results = adapter.generate_videos_batch_google_fx([request(tmp_path, i) for i in range(7)], callback)
    assert adapter.concurrency == 3
    assert peak == 3
    assert posted == list(range(7))
    assert completed.index(1) < completed.index(0)
    assert len({r['submission_id'] for r in results}) == 7
    for index, result in enumerate(results):
        assert result['status'] == 'success'
        assert result['confirmed'] is True and result['submission_pending'] is False
        assert result['has_audio'] is True
        assert Path(result['video_url']).parent == tmp_path / str(index)
        assert Path(result['video_url']).read_bytes() == MP4
        assert result['prompt_hash'] == hashlib.sha256(f'clip {index}'.encode()).hexdigest()
        own_events = [stage for i, stage, _ in events if i == index]
        assert own_events.index('request_submitting') < own_events.index('request_submitted')
        assert own_events.index('request_resolved') < own_events.index('video_done')


@pytest.mark.parametrize('limit', [1, 2, 5, 10])
def test_selected_limit_is_honored_by_real_async_transport(tmp_path, monkeypatch, limit):
    gate = asyncio.Event()
    active, peak, count = 0, 0, 0

    async def handler(req):
        nonlocal active, peak, count
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        count += 1
        active += 1
        peak = max(peak, active)
        if count == limit:
            gate.set()
        await gate.wait()
        active -= 1
        return success()

    results = service(monkeypatch, handler, flow2apiVideoConcurrency=limit).generate_videos_batch_google_fx(
        [request(tmp_path, i) for i in range(limit + 2)])
    assert all(result['status'] == 'success' for result in results)
    assert count == limit + 2 and peak == limit


@pytest.mark.parametrize('value,expected', [(None, 3), ('bad', 3), (True, 3), (2.5, 3),
                                         (5.0, 5), (0, 1), (-2, 1), ('5', 5), (99, 10)])
def test_concurrency_bounds(monkeypatch, value, expected):
    adapter = service(monkeypatch, lambda req: pytest.fail('configuration must not send HTTP'),
                      flow2apiVideoConcurrency=value)
    assert adapter.concurrency == expected


def test_auth_failure_stops_queued_slots_but_drains_other_sent_requests(tmp_path, monkeypatch):
    gate, drain = asyncio.Event(), asyncio.Event()
    posted, events = [], []

    async def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, content=MP4)
        index = clip_index(req)
        posted.append(index)
        if len(posted) == 3:
            gate.set()
        await gate.wait()
        if index == 0:
            return httpx.Response(401)
        await drain.wait()
        return success()

    def callback(index, stage, details):
        events.append((index, stage, details))
        if index == 0 and stage == 'video_error':
            drain.set()

    results = service(monkeypatch, handler).generate_videos_batch_google_fx(
        [request(tmp_path, i) for i in range(8)], callback)
    assert posted == [0, 1, 2, 0, 0, 0, 0, 0]  # Five additional attempts for settled HTTP 401.
    assert results[0]['code'] == 'authorization'
    assert [result['status'] for result in results[1:3]] == ['success', 'success']
    for result in results[3:]:
        assert result['code'] == 'batch_stopped'
        assert result['stopped_by'] == 'authorization'
        assert result['submission_pending'] is False
        assert 'submission_id' not in result
    assert [i for i, stage, _ in events if stage == 'request_submitting'] == posted


def test_receipt_callback_failure_prevents_all_sibling_paid_posts(tmp_path, monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return success()

    def callback(index, stage, details):
        if stage == 'request_submitting':
            raise RuntimeError('receipt write failed')

    with pytest.raises(Flow2APIVideoError, match='视频交付状态保存失败'):
        service(monkeypatch, handler).generate_videos_batch_google_fx(
            [request(tmp_path, i) for i in range(7)], callback)
    assert calls == []
    assert list(tmp_path.rglob('*.mp4')) == []


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self, on_read, chunk=b': heartbeat\n\n'):
        self.on_read, self.chunk, self.closed = on_read, chunk, False

    async def __aiter__(self):
        yield self.chunk
        self.on_read()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def test_cancel_closes_every_active_stream_and_retains_all_original_receipts(tmp_path, monkeypatch):
    streams, events, posted = [], [], []
    state = {'cancel': False}

    def read():
        if len(posted) == 3:
            state['cancel'] = True

    async def handler(req):
        posted.append(clip_index(req))
        stream = WaitingStream(read)
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    with pytest.raises(ConnectionError):
        service(monkeypatch, handler).generate_videos_batch_google_fx(
            [request(tmp_path, i) for i in range(8)],
            lambda *args: events.append(args), cancel_check=lambda: state['cancel'])
    assert posted == [0, 1, 2]
    assert all(stream.closed for stream in streams)
    receipts = [details for _, stage, details in events if stage == 'request_submitting']
    assert len(receipts) == 3
    assert len({r['submission_id'] for r in receipts}) == 3
    assert all(r['submission_pending'] is True and r['confirmed'] is False for r in receipts)
    assert list(tmp_path.rglob('*.mp4')) == []


def test_delivery_checkpoint_failure_closes_sibling_downloads_and_removes_partial_files(tmp_path, monkeypatch):
    gate = asyncio.Event()
    download_count = 0
    streams, events, posts = [], [], []

    async def handler(req):
        nonlocal download_count
        if req.method == 'POST':
            posts.append(clip_index(req))
            return success()
        download_count += 1
        if download_count == 1:
            await gate.wait()
            return httpx.Response(200, content=MP4)
        stream = WaitingStream(lambda: gate.set() if len(streams) == 2 else None, MP4[:32])
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    def callback(index, stage, details):
        events.append((index, stage, details))
        if stage == 'video_done':
            raise RuntimeError('manifest write failed')

    with pytest.raises(Flow2APIVideoError):
        service(monkeypatch, handler).generate_videos_batch_google_fx(
            [request(tmp_path, i) for i in range(8)], callback)
    assert posts == [0, 1, 2]
    assert len(streams) == 2 and all(stream.closed for stream in streams)
    assert len([1 for _, stage, _ in events if stage == 'request_resolved']) == 3
    assert list(tmp_path.rglob('*.mp4')) == []
