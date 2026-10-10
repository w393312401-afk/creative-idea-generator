"""An unopened sibling must respect a batch stop before creating paid intent."""
import asyncio
import json

import httpx

from flow2api_video import Flow2APIVideoService


def test_sibling_opening_transport_does_not_post_after_account_failure(tmp_path, monkeypatch):
    monkeypatch.setattr('flow2api_video.shutil.which', lambda tool: '/synthetic/' + tool)
    adapter = Flow2APIVideoService({
        'flow2apiApiKey': 'test-key', 'flow2apiVideoConcurrency': 2,
        'flow2apiVideoTimeoutSeconds': 2, 'videoRetryCount': 2,
    })
    sibling_opening, release_sibling = asyncio.Event(), asyncio.Event()
    posts, events, clients = [], [], []

    def handler(req):
        assert req.method == 'POST'
        prompt = json.loads(req.content)['messages'][0]['content'][0]['text']
        posts.append(prompt)
        return httpx.Response(401)

    class OpeningClient(httpx.AsyncClient):
        async def __aenter__(self):
            await super().__aenter__()
            if self.ordinal == 0:
                # Both siblings have passed _generate's initial stop check.
                await sibling_opening.wait()
            elif self.ordinal == 1:
                sibling_opening.set()
                await release_sibling.wait()
            return self

    def client_factory(**kwargs):
        client = OpeningClient(transport=httpx.MockTransport(handler), **kwargs)
        client.ordinal = len(clients)
        clients.append(client)
        return client

    def callback(index, stage, details):
        events.append((index, stage, dict(details)))
        if index == 0 and stage == 'video_error':
            assert details['code'] == 'authorization'
            release_sibling.set()

    adapter._client_factory = client_factory
    requests = [{
        'prompt': f'clip {index}', 'model': 'Omni Flash', 'ratio': '16:9',
        'duration': '10', 'resolution': '360p', 'output_path': str(tmp_path / str(index)),
    } for index in range(3)]
    results = adapter.generate_videos_batch_google_fx(requests, callback)

    assert posts == ['clip 0'] * 3
    assert len(clients) == 4 and all(client.is_closed for client in clients)
    assert results[0]['code'] == 'authorization'
    for result in results[1:]:
        assert result['code'] == 'batch_stopped'
        assert result['stopped_by'] == 'authorization'
        assert result['submission_pending'] is False
        assert 'submission_id' not in result
    assert [index for index, stage, _ in events if stage == 'request_submitting'] == [0, 0, 0]
