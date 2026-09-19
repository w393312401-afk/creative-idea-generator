import json
from pathlib import Path
from unittest.mock import MagicMock
import base64
import pytest

from integrations.google_fx.services.flow_video_identity import parse_project_media, match_project_media
from integrations.google_fx.services.google_fx_helpers import _distinct_slices
from integrations.google_fx.services.google_fx_video import _ChunkRunner
from integrations.google_fx.models import VideoRequest


def records():
    data = json.loads((Path(__file__).parent / 'fixtures/flow_project_media.json').read_text(encoding='utf-8'))
    body = ")]}'\n\n123\n" + json.dumps([['wrb.fr', 'Zzl0ze', json.dumps([None, [], data])]])
    return data, parse_project_media(body, data[0][1])


def test_real_project_shape_preserves_identity_and_ordered_inputs():
    data, rows = records()
    assert len(rows) == len(data)
    row = rows[0]
    assert match_project_media(rows, row['prompt'], row['refs']) == row
    assert match_project_media(rows, row['prompt'], list(reversed(row['refs']))) is None
    assert match_project_media(rows, row['prompt'], submitted_at=row['created'] + 100) is None
    assert match_project_media(rows, row['prompt'], excluded=[row['media_id']]) is None
    assert parse_project_media(json.dumps(data), 'another-project') == []


def test_shared_prefix_and_duplicate_outputs_are_not_identity():
    _, rows = records()
    row = rows[0]
    assert match_project_media(rows, row['prompt'][:60]) is None
    assert match_project_media(rows + [dict(row, media_id='duplicate')], row['prompt']) is None
    assert _distinct_slices({1: row['prompt'], 2: row['prompt']}) == {1: '', 2: ''}


def test_rich_text_hyphen_wrap_does_not_lose_completed_video():
    row = dict(media_id='result', prompt='An extreme close- up reveals the trim.',
               refs=['first', 'last'], created=200)
    assert match_project_media([row], 'An extreme close-up reveals the trim.',
                               ['first', 'last'], 190) == row
    assert match_project_media([row], 'An extreme close-up reveals another trim.',
                               ['first', 'last'], 190) is None
    assert match_project_media([row], 'An extreme close-up reveals the trim.',
                               ['last', 'first'], 190) is None
    assert match_project_media([row, dict(row, media_id='duplicate')],
                               'An extreme close-up reveals the trim.') is None


def test_recovery_reuses_reader_with_bounded_refresh_and_cleanup(monkeypatch):
    from integrations.google_fx.services.flow_video_identity import ProjectVideoRecovery
    page = MagicMock()
    page.url = 'https://flow.google.com/project/3ab3cd1e-c52e-453b-8c1c-e6e996e72b27'
    clock = [100]
    monkeypatch.setattr('integrations.google_fx.services.flow_video_identity.time.monotonic', lambda: clock[0])
    with ProjectVideoRecovery(page) as recovery:
        for t in [100, 130, 160, 190, 220, 280, 400]:
            clock[0] = t
            recovery.read(lambda: None)
        page.context.new_page.assert_called_once()
        assert recovery.reader.goto.call_count == 3
    recovery.reader.close.assert_called_once()
    page.remove_listener.assert_called_once()


def test_verified_binding_survives_record_refresh_but_not_request_changes():
    from integrations.google_fx.services.flow_video_identity import ProjectVideoRecovery
    page = MagicMock()
    page.url = 'https://flow.google.com/project/3ab3cd1e-c52e-453b-8c1c-e6e996e72b27'
    session = ProjectVideoRecovery(page)
    row = dict(media_id='stable-media', prompt='exact prompt', refs=['first', 'last'], created=100)
    session.records[row['media_id']] = row
    request = dict(tile_id='temporary-dom-stamp', prompt='exact prompt', refs=['first', 'last'], click_time=100)
    assert session.match_request(request) == row
    session.records.clear()
    assert session.match_request(request) == row
    assert session.match_request(request, excluded=['stable-media']) is None
    assert session.match_request(dict(request, prompt='changed prompt')) is None
    assert session.match_request(dict(request, refs=['last', 'first'])) is None
    assert session.match_request(dict(request, click_time=200)) is None


def test_lost_dom_recovered_and_delivered_in_same_poll(monkeypatch, tmp_path):
    req = VideoRequest(prompt='exact full prompt', output_path=str(tmp_path))
    events = []
    runner = _ChunkRunner(1, 0, [req], {}, lambda *a: events.append(a), None)
    task = dict(tile_id='erased', sub_idx=0, idx=0, req=req, status='generating', click_time=1)
    states = {'erased': {'status': 'missing'}}
    calls = []
    def recover(page, requests, cancel, on_resolved=None):
        calls.append(requests)
        state = dict(status='done', videoSrc='https://flow-content.google/video/result')
        on_resolved('erased', state)
        assert [a[1] for a in events] == ['video_done']
        return {'erased': state}
    monkeypatch.setattr('integrations.google_fx.services.flow_video_identity.recover_project_videos', recover)
    monkeypatch.setattr('integrations.google_fx.services.google_fx_video.download_video_via_browser',
                        lambda *args: str(tmp_path / 'result.mp4'))
    runner._wake_ready_tiles(MagicMock(), [task], states)
    runner._deliver_ready_tasks(MagicMock(), [task], states)
    assert len(calls) == 1 and calls[0][0]['prompt'] == req.prompt
    assert [a[1] for a in events] == ['video_done']
    assert runner.completed == {0}


def test_download_redirect_fallback_validates_bytes(tmp_path):
    from integrations.google_fx.utils.browser import download_video_via_browser
    page = MagicMock()
    page.evaluate.side_effect = RuntimeError('Failed to fetch (CORS)')
    response = page.context.request.get.return_value
    response.status = 200
    payload = b'\0\0\0\x18ftypmp42' + b'x' * 2000
    response.body.return_value = payload
    path = download_video_via_browser(page, 'https://flow.google.com/asb/media=mm', str(tmp_path))
    assert Path(path).read_bytes() == payload
    response.dispose.assert_called_once()


def test_download_rejects_thumbnail_or_error_document(tmp_path):
    from integrations.google_fx.utils.browser import download_video_via_browser
    page = MagicMock()
    page.evaluate.return_value = base64.b64encode(b'<html>error</html>' * 100).decode()
    with pytest.raises(RuntimeError, match='MP4'):
        download_video_via_browser(page, 'https://flow.google.com/asb/image', str(tmp_path))
    assert not list(tmp_path.iterdir())
