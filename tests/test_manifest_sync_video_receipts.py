"""Disk reconciliation must retain unfinished video jobs and paid submission receipts.

Both startup migration and reading /api/get_manifest reconcile the same manifest.
An absent MP4 is expected for a failed/running job; it must not erase the evidence
needed to recover a submitted result without paying for a duplicate generation.
"""

import json

import pytest

import server


@pytest.fixture
def project(tmp_path, monkeypatch):
    # run_migrations derives outputs/ from server.__file__, so isolate that path
    # as well as the normal project's media paths before invoking the real entrypoint.
    monkeypatch.setattr(server, '__file__', str(tmp_path / 'server.py'))
    monkeypatch.setattr(server, 'DB_FILE', str(tmp_path / 'library.json'))
    project_dir = tmp_path / 'outputs' / 'receipt-test'
    (project_dir / 'videos').mkdir(parents=True)
    monkeypatch.setattr(server, '_get_project_dir', lambda _title: str(project_dir))
    return project_dir


def _seed(project, videos):
    data = {'title': 'receipt-test', 'frames': [], 'videos': videos}
    (project / 'manifest.json').write_text(json.dumps(data), encoding='utf-8')


def _load(project):
    return json.loads((project / 'manifest.json').read_text(encoding='utf-8'))


def _sync(entrypoint, project):
    if entrypoint == 'startup':
        server.run_migrations()
        return _load(project)
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/get_manifest?title=receipt-test'
    handler._gate = lambda: True
    sent = []
    handler._send_json = lambda obj, status=200: sent.append((obj, status))
    handler.do_GET()
    assert len(sent) == 1 and sent[0][1] == 200
    assert sent[0][0] == _load(project)
    return sent[0][0]


@pytest.mark.parametrize('entrypoint', ['startup', 'get_manifest'])
def test_sync_retains_unfinished_jobs_and_submission_receipts(project, entrypoint):
    videos = []
    for slot, status in enumerate(('failed', 'pending', 'running', 'queued', 'cancelled'), 1):
        videos.append({
            'slot': slot, 'sequence': slot, 'status': status,
            # Both empty file paths and not-yet-created paths occur in real jobs.
            'file': '' if slot % 2 else f'outputs/receipt-test/videos/vid_{slot:03d}.mp4',
            'url': '', 'prompt': f'Prompt {slot}', 'error': 'Result not downloaded',
            'last_attempt': {
                'account_id': 'account-a', 'project_url': 'https://example.test/project/a',
                'tile_id': f'tile-{slot}', 'confirmed': True,
                'submission_pending': slot == 1, 'status': status,
            },
        })
    # The receipt is authoritative even when a legacy row has a different status.
    videos.append({
        'slot': 6, 'sequence': 6, 'status': 'success', 'file': '',
        'last_attempt': {'tile_id': 'unresolved-tile', 'submission_pending': True},
    })
    _seed(project, videos)
    # A new disk result forces an actual rewrite rather than relying on a no-op.
    (project / 'videos' / 'vid_007.mp4').write_bytes(b'completed-video')

    for _ in range(2):
        data = _sync(entrypoint, project)
        assert data['videos'][:6] == videos
        assert data['videos'][6]['slot'] == 7
        assert data['videos'][6]['status'] == 'success'


@pytest.mark.parametrize('entrypoint', ['startup', 'get_manifest'])
def test_sync_still_removes_deleted_success_and_restores_disk_result(project, entrypoint):
    deleted = {
        'slot': 1, 'sequence': 1, 'status': 'success',
        'file': 'outputs/receipt-test/videos/vid_001.mp4',
        'last_attempt': {'confirmed': True, 'submission_pending': False},
    }
    kept = {
        'slot': 2, 'sequence': 2, 'status': 'success', 'file': 'stale/path.mp4',
        'prompt': 'Keep existing prompt',
        'last_attempt': {'tile_id': 'completed-tile', 'submission_pending': False},
    }
    _seed(project, [deleted, kept])
    (project / 'videos' / 'vid_002.mp4').write_bytes(b'existing-video')
    (project / 'videos' / 'vid_003.mp4').write_bytes(b'orphan-video')

    data = _sync(entrypoint, project)

    assert [v['slot'] for v in data['videos']] == [2, 3]
    saved = data['videos'][0]
    assert saved['status'] == 'success'
    assert saved['prompt'] == kept['prompt']
    assert saved['last_attempt'] == kept['last_attempt']
    assert saved['file'] == 'outputs/receipt-test/videos/vid_002.mp4'
    assert data['videos'][1]['status'] == 'success'
