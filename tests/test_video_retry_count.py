"""Configurable extra attempts must never duplicate an unresolved submission."""
import asyncio
import json

import httpx
import pytest

import server_common as sc
from flow2api_video import Flow2APIVideoService
from fx_console import FxConfigStore, normalize_video_retry_count, validate_patch
from video_generator import _ManifestWriter


def service(monkeypatch, handler, **config):
    monkeypatch.setattr('flow2api_video.shutil.which', lambda name: '/fake/' + name)
    instance = Flow2APIVideoService({'flow2apiApiKey': 'fake-key',
        'flow2apiVideoTimeoutSeconds': 0.03, **config})
    instance._client_factory = lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kwargs)
    return instance


def request(tmp_path):
    return {'prompt': 'clip', 'model': 'Omni Flash', 'ratio': '16:9',
            'duration': '10', 'resolution': '360p', 'output_path': str(tmp_path)}


def settled_failure():
    payload = {'error': {'code': 'submission_not_started',
                       'submission_pending': False, 'upstream_accepted': False}}
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          content=('data: ' + json.dumps(payload) + '\n\n').encode())


@pytest.mark.parametrize('setting,attempts', [(None, 6), (0, 1), (2, 3), (10, 11)])
def test_flow2api_extra_attempt_budget_and_fresh_receipts(tmp_path, monkeypatch, setting, attempts):
    posts, events = [], []
    def handler(req):
        posts.append(req)
        return settled_failure()
    config = {} if setting is None else {'videoRetryCount': setting}
    result = service(monkeypatch, handler, **config).generate_videos_batch_google_fx(
        [request(tmp_path)], lambda idx, stage, detail: events.append((stage, detail)))[0]
    assert len(posts) == attempts
    assert len({post.headers['X-SPARK-Submission-ID'] for post in posts}) == attempts
    assert result['submission_pending'] is False
    warnings = [detail for stage, detail in events if stage == 'video_warning']
    assert [detail['retry'] for detail in warnings] == list(range(1, attempts))
    assert all(detail['max_retries'] == attempts - 1 for detail in warnings)


@pytest.mark.parametrize('outcome', ['pending', 'disconnect', 'timeout'])
def test_flow2api_unresolved_status_never_spends_retry_budget(tmp_path, monkeypatch, outcome):
    posts = []
    async def handler(req):
        posts.append(req)
        if outcome == 'disconnect':
            raise httpx.ReadError('lost response', request=req)
        if outcome == 'timeout':
            await asyncio.Event().wait()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                             content=b'data: {"error":{"code":"video_status_unknown","submission_pending":true}}\n\n')
    result = service(monkeypatch, handler).generate_videos_batch_google_fx([request(tmp_path)])[0]
    assert len(posts) == 1
    assert result['submission_pending'] is True
    assert result['submission_id'] == posts[0].headers['X-SPARK-Submission-ID']


def test_config_keeps_zero_and_server_setting_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, 'SERVER_CONFIG', {'videoRetryCount': 0})
    assert sc.effective_config({'videoRetryCount': 5})['videoRetryCount'] == 0
    assert sc.video_service_config_report()['videoRetryCount'] == 0
    store = FxConfigStore({'videoRetryCount': 0}, str(tmp_path / 'settings.json'))
    assert store.current()['videoRetryCount'] == 0
    assert validate_patch({'videoRetryCount': 5}) == {'videoRetryCount': 5}
    for value in (-1, 11, True, 1.5, 'invalid'):
        with pytest.raises(ValueError):
            validate_patch({'videoRetryCount': value})
    assert [normalize_video_retry_count(value) for value in (None, '0', '5', -1, 11)] == [5, 0, 5, 0, 10]


def test_video_writer_preserves_new_frames_scheduler_and_other_video_slots(tmp_path):
    initial = {'frames': [{'slot': 1, 'file': 'old'}], 'videos': [],
               'auto_video': {'status': 'waiting'}, 'prompt_block': 'old prompt'}
    sc.write_manifest(str(tmp_path), initial)
    writer = _ManifestWriter(str(tmp_path / 'manifest.json'), dict(initial), [1])
    concurrent = {'frames': [{'slot': 1, 'file': 'old'}, {'slot': 2, 'file': 'new'}],
                  'videos': [{'slot': 9, 'status': 'success', 'file': 'other-video'}],
                  'auto_video': {'status': 'running', 'submitted_slots': [1]},
                  'prompt_block': 'new prompt',
                  'capability_degraded': {'frames': {'issues': ['frame-warning']}}}
    sc.write_manifest(str(tmp_path), concurrent)
    writer.record({'slot': 1, 'status': 'success', 'file': 'our-video'})
    current = sc.read_manifest(str(tmp_path))
    assert current['frames'] == concurrent['frames']
    assert current['auto_video'] == concurrent['auto_video']
    assert current['prompt_block'] == 'new prompt'
    assert [video['slot'] for video in current['videos']] == [1, 9]
    assert current['capability_degraded']['frames'] == concurrent['capability_degraded']['frames']


def test_pipeline_has_one_provider_retry_owner(monkeypatch):
    import pipeline_orchestrator as po
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        return {'videos': [{'slot': 1, 'status': 'failed'}]}
    monkeypatch.setattr(po, 'generate_video_sequence', generate)
    result = po._render_videos_with_recovery({'videoRetryCount': 5}, 'project', 'prompt')
    assert len(calls) == 1
    assert result['videos'][0]['status'] == 'failed'


def test_video_extracted_frame_merges_with_latest_rendered_frames(tmp_path):
    initial = {'frames': [{'slot': 1, 'file': 'anchor'}], 'videos': []}
    sc.write_manifest(str(tmp_path), initial)
    writer = _ManifestWriter(str(tmp_path / 'manifest.json'), dict(initial), [1])
    concurrent = {**initial, 'frames': [*initial['frames'], {'slot': 2, 'file': 'rendered'}],
                  'auto_video': {'status': 'waiting'}}
    sc.write_manifest(str(tmp_path), concurrent)
    writer.record_frame({'slot': 3, 'file': 'extracted'})
    writer.save()
    current = sc.read_manifest(str(tmp_path))
    assert [frame['slot'] for frame in current['frames']] == [1, 2, 3]
    assert current['auto_video'] == concurrent['auto_video']
