"""Project archive must preserve published deliverables and isolate every deletion."""

import importlib
import io
import json
import threading
from email.message import Message
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest

import codex_video_editor
import server
import server_common


def test_removed_task_does_not_hide_its_still_running_worker(archive_env):
    project = _project(archive_env)
    server_common.ACTIVE_TASKS.pop(project['task_id'])
    release = threading.Event()
    started = threading.Event()

    def worker(task_id, config, title):
        started.set()
        release.wait(5)

    thread = threading.Thread(target=worker, args=(project['task_id'],
        {'_project_key': project['key']}, project['title']))
    thread.start()
    try:
        assert started.wait(1)
        before = _snapshot(project['dir'])
        _assert_rejected(_archive(archive_env, project))
        assert _snapshot(project['dir']) == before
    finally:
        release.set()
        thread.join(2)


CURRENT_PROMPTS = '图片 1:\nCurrent image prompt.\n\n视频 1:\nCurrent video prompt.'
OLDER_PROMPTS = '图片 1:\nOld image prompt.\n\n视频 1:\nOld video prompt.'
FINISHED_VIDEO = b'published fine-cut video'
BEATS = {'schema_version': 1, 'beats': [{'index': 1, 'operation': 'build'}]}


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """Never let the archiver or its edit lock read/write production state."""
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    library = tmp_path / 'library'
    tasks_root = tmp_path / 'tasks'
    # API/task guards use the module's production base_dir by default; an absolute
    # temporary root isolates those paths as well as direct calls below.
    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', str(outputs))
    monkeypatch.setattr(server, 'OUTPUT_ROOT', str(outputs))
    monkeypatch.setattr(server_common, 'LIBRARY_DIR', str(library))
    monkeypatch.setattr(server_common, 'DB_FILE', str(tmp_path / 'library.json'))
    monkeypatch.setattr(server_common, 'LEDGER_FILE', str(tmp_path / 'topic_ledger.json'))
    monkeypatch.setattr(server_common, 'TASKS_DIR', str(tasks_root))
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server_common, '_TASK_FLUSHED_EVENTS', {})
    monkeypatch.setattr(server_common, '_PROJ_ASSET_STATS_CACHE', {})
    monkeypatch.setattr(server_common, '_PROJ_PROGRESS_CACHE', {})
    monkeypatch.setattr(server_common, '_ACTIVE_FRAME_RUNS', {})
    monkeypatch.setattr(codex_video_editor, 'OUTPUTS_DIR', outputs)
    monkeypatch.setattr(codex_video_editor, '_worker_alive', lambda job: False)
    monkeypatch.setattr(codex_video_editor, '_guardian_alive', lambda job: False)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    module = importlib.import_module('project_archive')
    return {'root': tmp_path, 'outputs': outputs, 'library': library,
            'tasks': tasks_root, 'module': module, 'monkeypatch': monkeypatch}


def _project(env, number=1, *, completed=True, prompts=True, beats=True):
    key = f'run_{number}__归档小屋{number}'
    title = f'归档小屋{number}'
    directory = env['outputs'] / server_common._safe_project_name(key)
    directory.mkdir()
    (directory / 'source.mp4').write_bytes(b'old unedited merged video')
    (directory / 'frames').mkdir()
    (directory / 'frames' / 'img_001.webp').write_bytes(b'generated image')
    (directory / 'videos').mkdir()
    (directory / 'videos' / 'video_001.mp4').write_bytes(b'generated clip')
    (directory / 'cover.webp').write_bytes(b'cover')
    (directory / 'collage.jpg').write_bytes(b'collage')
    _json(directory / '.deleted_slots' / 'snapshot' / 'state.json', {'old': 'state'})
    _json(directory / 'unrelated.json', {'arbitrary': 'intermediate'})
    (directory / 'scratch.txt').write_text('temporary notes', encoding='utf-8')

    job_id = f'{number:032x}'
    job_dir = directory / 'codex_edits' / job_id
    output = job_dir / 'work' / 'edited_v2.mp4'
    output.parent.mkdir(parents=True)
    output.write_bytes(FINISHED_VIDEO)
    (output.parent / 'edited.mp4').write_bytes(b'obsolete edit version')
    _json(job_dir / 'work' / 'qa' / 'evidence.json', {'evidence': 'large evidence'})
    (job_dir / 'input.mp4').write_bytes(b'input copy')
    _json(job_dir / '.request.json', {'notes': 'edit request'})
    state = {'id': job_id, 'status': 'completed' if completed else 'failed',
             'source': f'/outputs/{directory.name}/source.mp4',
             'created_at': '2026-10-01T09:00:00', 'updated_at': '2026-10-01T10:00:00',
             'output': {'file': str(output),
                        'url': f'/outputs/{directory.name}/codex_edits/{job_id}/work/edited_v2.mp4',
                        'duration_seconds': 8, 'size_bytes': len(FINISHED_VIDEO)} if completed else None}
    _json(job_dir / '.state.json', state)
    _json(directory / 'manifest.json', {
        'title': title, 'frames': [{'file': str(directory / 'frames' / 'img_001.webp')}],
        'videos': [{'file': str(directory / 'videos' / 'video_001.mp4')}],
        'merged_video': {'file': str(directory / 'source.mp4')},
        'prompt_block': OLDER_PROMPTS if prompts else '',
    })
    if beats:
        _json(directory / 'timelapse_beats.json', BEATS)
    item = {'id': f'idea_{number}', 'project_key': key, 'title': title,
            'theme': title, 'timestamp': '2026-10-01T09:00:00',
            'prompt_block': CURRENT_PROMPTS if prompts else '',
            'imported_source_text': OLDER_PROMPTS if prompts else '',
            'prompt_slots': {'images': [{'index': 1, 'body': 'Current image prompt.'}],
                             'videos': [{'index': 1, 'body': 'Current video prompt.'}]} if prompts else {},
            'frameRun': {'frames': [{'file': str(directory / 'frames' / 'img_001.webp')}],
                         'merged_video': {'file': str(directory / 'source.mp4')}},
            'covers': [f'/outputs/{directory.name}/cover.webp'],
            'activeCoverUrl': f'/outputs/{directory.name}/cover.webp',
            'collage_url': f'/outputs/{directory.name}/collage.jpg',
            'audit_md': 'old review', 'repair_md': 'old repair', 'timings': {'old': 99}}
    server_common.write_library_item(item)
    task_id = f'task_{number}'
    task = {'id': task_id, 'status': 'completed',
            'dimensions': {'type': 'idea', 'project_key': key, 'theme': title},
            'result': {'title': title, 'project_key': key,
                       'prompt_block': OLDER_PROMPTS if prompts else ''},
            'events': [('completed', {'message': 'done'})], 'last_active': 1,
            'cancel_event': threading.Event()}
    server_common.ACTIVE_TASKS[task_id] = task
    assert server_common.save_task_to_disk(task_id)
    return {'key': key, 'title': title, 'dir': directory, 'item': item,
            'task_id': task_id, 'state': state, 'state_file': job_dir / '.state.json',
            'output': output, 'job_dir': job_dir}


def _archive(env, project, *, preview=False):
    return env['module'].archive_projects([project['key']], preview=preview,
                                         base_dir=str(env['root']))


def _set_merged_records(project, *, manifest=None, library=None, task=None):
    """Choose explicit final-output records without preserving unnamed slot MP4s."""
    manifest_file = project['dir'] / 'manifest.json'
    data = json.loads(manifest_file.read_text())
    data.pop('merged_video', None)
    if manifest is not None:
        data['merged_video'] = manifest
    _json(manifest_file, data)
    item = server_common.read_library_item(project['item']['id'])
    item['frameRun'].pop('merged_video', None)
    if library is not None:
        item['frameRun']['merged_video'] = library
    server_common.write_library_item(item)
    result = server_common.ACTIVE_TASKS[project['task_id']]['result']
    result.pop('merged_video', None)
    result.pop('frameRun', None)
    if task is not None:
        result['frameRun'] = {'merged_video': task}
    assert server_common.save_task_to_disk(project['task_id'])


def _retained_paths(env, row):
    paths = []
    for item in row['retained_files']:
        url = unquote(urlsplit(item['url']).path)
        assert url.startswith('/outputs/')
        path = env['outputs'] / url.removeprefix('/outputs/')
        assert path.is_file(), f'published archive URL has no file: {url}'
        paths.append(path)
    return paths


def _assert_rejected(result):
    assert result['count'] == 0
    assert result['projects'] == []
    assert result['errors']
    assert result['errors'][0]['message']


def _api(path, payload=None, *, method='POST', allowed=True, raw_body=None,
         content_type='application/json'):
    """Exercise real routing and JSON handling without starting a local service."""
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = path
    body = raw_body if raw_body is not None else (json.dumps(payload).encode('utf-8') if method == 'POST' else b'')
    handler.headers = Message()
    handler.headers['Content-Type'] = content_type
    handler.headers['Content-Length'] = str(len(body))
    handler.rfile = io.BytesIO(body)
    sent = []
    handler._send_json = lambda value, status=200: sent.append((value, status))

    def gate(*args, **kwargs):
        if not allowed:
            handler._send_json({'status': 'error', 'message': 'gate denied'}, status=401)
        return allowed

    handler._gate = gate
    getattr(handler, 'do_' + method)()
    assert len(sent) == 1
    return sent[0]


def test_preview_is_read_only_and_reports_disposable_files(archive_env):
    project = _project(archive_env)
    before = _snapshot(archive_env['root'])
    result = _archive(archive_env, project, preview=True)
    assert result['status'] == 'ok'
    assert not result['errors']
    assert result['count'] == 1
    row = result['projects'][0]
    assert row['project_key'] == project['key']
    assert row['delete_count'] > 0 and row['delete_bytes'] > 0
    # Acquiring the shared editor lock may create its root lock file.
    after = _snapshot(archive_env['root'])
    after.pop('outputs/.codex-edit.lock', None)
    before.pop('outputs/.codex-edit.lock', None)
    assert after == before
    assert project['task_id'] in server_common.ACTIVE_TASKS


def test_archive_keeps_completed_output_current_prompts_and_beats(archive_env):
    project = _project(archive_env)
    result = _archive(archive_env, project)
    assert result['status'] == 'ok' and result['count'] == 1
    assert not result['errors']
    row = result['projects'][0]
    assert row['video_retention'] == row['archive']['video_retention'] == 'refined'
    assert row['archive']['final_videos'] == row['archive']['refined_videos']
    assert [entry['kind'] for entry in row['archive']['final_videos']] == ['refined_video']
    retained = _retained_paths(archive_env, row)
    assert [path.read_bytes() for path in retained if path.suffix == '.mp4'] == [FINISHED_VIDEO]
    texts = [path.read_text(encoding='utf-8') for path in retained if path.suffix == '.txt']
    assert CURRENT_PROMPTS in texts
    assert OLDER_PROMPTS not in texts
    assert any(path.suffix == '.json' and json.loads(path.read_text()) == BEATS for path in retained)
    for unwanted in ('source.mp4', 'frames', 'videos', 'cover.webp', 'collage.jpg',
                     '.deleted_slots', 'unrelated.json', 'scratch.txt'):
        assert not (project['dir'] / unwanted).exists()
    assert not project['state_file'].exists()
    assert not (project['job_dir'] / 'input.mp4').exists()
    assert not (project['output'].parent / 'edited.mp4').exists()
    assert set(_snapshot(project['dir'])) == {
        str(path.relative_to(project['dir'])) for path in retained
    } | {archive_env['module'].RECEIPT}


def test_archive_removes_task_files_and_compacts_library(archive_env):
    project = _project(archive_env)
    result = _archive(archive_env, project)
    assert result['count'] == 1
    assert project['task_id'] not in server_common.ACTIVE_TASKS
    for filename in server_common._task_paths(project['task_id']):
        assert not Path(filename).exists()
    item_id = result['projects'][0]['library']['id']
    item = server_common.read_library_item(item_id)
    assert item and item['project_key'] == project['key']
    assert item.get('archive')
    for field in ('covers', 'activeCoverUrl', 'collage_url', 'audit_md', 'repair_md',
                  'imported_source_text', 'timings'):
        assert not item.get(field), f'obsolete {field} remained in archived library item'
    assert not ((item.get('frameRun') or {}).get('frames'))
    rows = server_common.build_projects_index(base_dir=str(archive_env['root']))
    assert any(row['project_key'] == project['key'] for row in rows)


@pytest.mark.parametrize('filename', ['timelapse_beats.json', 'beat_package.json'])
def test_archive_preserves_local_reverse_data(archive_env, filename):
    project = _project(archive_env, beats=False)
    _json(project['dir'] / filename, BEATS)
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    assert any(path.suffix == '.json' and json.loads(path.read_text()) == BEATS for path in retained)


def test_archive_preserves_external_replica_beats_without_deleting_shared_job(archive_env):
    project = _project(archive_env, beats=False)
    other = _project(archive_env, number=2, beats=False)
    replica_id = 'replica_shared_1234'
    replica_dir = archive_env['outputs'] / 'replica_jobs' / replica_id
    _json(replica_dir / 'timelapse_beats.json', BEATS)
    _json(replica_dir / '.replica_pipeline.json', {'job_id': replica_id, 'beats': BEATS})
    (replica_dir / 'source.mp4').write_bytes(b'shared reverse source')
    for p in (project, other):
        item = server_common.read_library_item(p['item']['id'])
        item['replica_job_id'] = replica_id
        server_common.write_library_item(item)
    reverse_before = _snapshot(replica_dir)
    other_before = _snapshot(other['dir'])
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    assert any(path.suffix == '.json' and json.loads(path.read_text()) == BEATS for path in retained)
    assert _snapshot(replica_dir) == reverse_before
    assert _snapshot(other['dir']) == other_before
    assert server_common.read_library_item(other['item']['id'])['replica_job_id'] == replica_id


@pytest.mark.parametrize('retention', ['refined', 'merged', 'none'])
def test_archive_is_idempotent(archive_env, retention):
    project = _project(archive_env, completed=retention == 'refined')
    if retention == 'none':
        _set_merged_records(project)
    first = _archive(archive_env, project)
    assert first['count'] == 1
    preserved = {str(path): path.read_bytes() for path in _retained_paths(archive_env, first['projects'][0])}
    second = _archive(archive_env, project)
    assert second['count'] == 1 and not second['errors']
    assert second['projects'][0]['video_retention'] == retention
    assert second['projects'][0]['archive']['final_videos'] == first['projects'][0]['archive']['final_videos']
    assert second['projects'][0]['delete_count'] == 0
    assert {str(path): path.read_bytes() for path in _retained_paths(archive_env, second['projects'][0])} == preserved


def test_all_completed_edit_deliverables_survive_but_unpublished_versions_do_not(archive_env):
    project = _project(archive_env)
    second_id = 'f' * 32
    second_dir = project['dir'] / 'codex_edits' / second_id
    second_output = second_dir / 'work' / 'custom_final_name.mp4'
    second_output.parent.mkdir(parents=True)
    second_output.write_bytes(b'second published fine-cut video')
    (second_output.parent / 'edited.mp4').write_bytes(b'unpublished edit')
    state = dict(project['state'], id=second_id,
                 output={'file': str(second_output),
                         'url': f'/outputs/{project["dir"].name}/codex_edits/{second_id}/work/custom_final_name.mp4'})
    _json(second_dir / '.state.json', state)
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    assert {path.read_bytes() for path in retained if path.suffix == '.mp4'} == {
        FINISHED_VIDEO, b'second published fine-cut video'}
    assert not (second_output.parent / 'edited.mp4').exists()


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_active_edit_rejects_archive_without_touching_project(archive_env, status):
    project = _project(archive_env)
    project['state']['status'] = status
    _json(project['state_file'], project['state'])
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


@pytest.mark.parametrize('process', ['_worker_alive', '_guardian_alive'])
def test_live_edit_process_blocks_even_when_persisted_status_is_terminal(archive_env, process):
    project = _project(archive_env)
    archive_env['monkeypatch'].setattr(codex_video_editor, process, lambda job: True)
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


@pytest.mark.parametrize('status', ['running', 'queued'])
def test_running_generation_rejects_archive(archive_env, status):
    project = _project(archive_env)
    server_common.ACTIVE_TASKS[project['task_id']]['status'] = status
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert not server_common.ACTIVE_TASKS[project['task_id']]['cancel_event'].is_set()


def test_missing_prompts_still_refuses_all_deletion(archive_env):
    project = _project(archive_env, prompts=False)
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert server_common.read_library_item(project['item']['id']) is not None


@pytest.mark.parametrize('origin', ['manifest', 'library', 'task'])
@pytest.mark.parametrize('path_form', ['absolute', 'relative', 'url'])
def test_without_finished_edit_archives_explicit_merged_output(archive_env, origin, path_form):
    project = _project(archive_env, completed=False)
    merged = project['dir'] / 'source.mp4'
    expected = merged.read_bytes()
    if path_form == 'url':
        entry = {'url': '/outputs/' + quote(f'{project["dir"].name}/source.mp4', safe='/')}
    elif path_form == 'relative':
        entry = {'file': str(merged.relative_to(archive_env['root']))}
    else:
        entry = {'file': str(merged)}
    _set_merged_records(project, **{origin: entry})
    before = _snapshot(project['dir'])
    preview = _archive(archive_env, project, preview=True)
    assert preview['count'] == 1 and not preview['errors']
    assert preview['projects'][0]['video_retention'] == 'merged'
    assert _snapshot(project['dir']) == before
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    row = result['projects'][0]
    assert row['video_retention'] == row['archive']['video_retention'] == 'merged'
    assert row['archive']['refined_videos'] == []
    assert [entry['kind'] for entry in row['archive']['final_videos']] == ['merged_video']
    retained = _retained_paths(archive_env, row)
    assert [path for path in retained if path.suffix == '.mp4'] == [merged]
    assert merged.read_bytes() == expected
    assert not project['output'].exists()
    assert not (project['dir'] / 'videos').exists()
    assert not project['state_file'].exists()


def test_manifest_final_output_precedes_stale_saved_output(archive_env):
    project = _project(archive_env, completed=False)
    manifest_final = project['dir'] / 'current_final.mp4'
    manifest_final.write_bytes(b'current manifest completed merge')
    old_final = project['dir'] / 'source.mp4'
    _set_merged_records(project, manifest={'file': str(manifest_final)},
                        library={'file': str(old_final)})
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    assert [path for path in retained if path.suffix == '.mp4'] == [manifest_final]
    assert not old_final.exists()


def test_project_without_edit_job_still_keeps_merged_output(archive_env):
    project = _project(archive_env, completed=False)
    project['state_file'].unlink()
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    assert result['projects'][0]['video_retention'] == 'merged'
    retained = _retained_paths(archive_env, result['projects'][0])
    assert [path for path in retained if path.suffix == '.mp4'] == [project['dir'] / 'source.mp4']
    assert not project['output'].exists()


def test_without_explicit_final_video_archives_only_prompts_and_beats(archive_env):
    project = _project(archive_env, completed=False)
    _set_merged_records(project)
    # Raw slot videos and failed edit products exist, but neither is a final video.
    assert list(project['dir'].rglob('*.mp4'))
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    row = result['projects'][0]
    assert row['video_retention'] == row['archive']['video_retention'] == 'none'
    assert row['archive']['final_videos'] == row['archive']['refined_videos'] == []
    retained = _retained_paths(archive_env, row)
    assert {entry['kind'] for entry in row['retained_files']} == {'prompts', 'beats'}
    assert not list(project['dir'].rglob('*.mp4'))
    assert (project['dir'] / archive_env['module'].PROMPTS_TEXT).read_text() == CURRENT_PROMPTS
    assert any(path.name == 'timelapse_beats.json' for path in retained)


@pytest.mark.parametrize('invalid_file', ['missing', 'empty', 'non_mp4'])
def test_unavailable_saved_merge_is_not_preserved_as_final_video(archive_env, invalid_file):
    project = _project(archive_env, completed=False)
    if invalid_file == 'missing':
        candidate = project['dir'] / 'missing.mp4'
    elif invalid_file == 'empty':
        candidate = project['dir'] / 'empty.mp4'
        candidate.touch()
    else:
        candidate = project['dir'] / 'wrong_container.mov'
        candidate.write_bytes(b'non MP4 file')
    _set_merged_records(project, manifest={'file': str(candidate)})
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    assert result['projects'][0]['video_retention'] == 'none'
    assert result['projects'][0]['archive']['final_videos'] == []
    assert not candidate.exists()


def test_missing_manifest_merge_can_use_existing_saved_final_video(archive_env):
    project = _project(archive_env, completed=False)
    merged = project['dir'] / 'source.mp4'
    _set_merged_records(project, manifest={'file': str(project['dir'] / 'removed_old_final.mp4')},
                        library={'file': str(merged)})
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    assert result['projects'][0]['video_retention'] == 'merged'
    assert merged.exists()


@pytest.mark.parametrize('path_form', ['absolute', 'url', 'traversal'])
def test_merged_output_outside_project_is_rejected_without_deletion(archive_env, path_form):
    project = _project(archive_env, completed=False)
    other = _project(archive_env, number=2)
    if path_form == 'url':
        record = {'url': f'/outputs/{other["dir"].name}/source.mp4'}
    elif path_form == 'traversal':
        record = {'file': str(project['dir'] / '..' / other['dir'].name / 'source.mp4')}
    else:
        record = {'file': str(other['dir'] / 'source.mp4')}
    _set_merged_records(project, manifest=record)
    before = _snapshot(project['dir'])
    other_before = _snapshot(other['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert _snapshot(other['dir']) == other_before


@pytest.mark.parametrize('bad_path', [123, ['source.mp4'], {'file': 'source.mp4'}])
def test_bad_merged_path_type_is_rejected_without_deletion(archive_env, bad_path):
    project = _project(archive_env, completed=False)
    _set_merged_records(project, manifest={'file': bad_path})
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


def test_completed_state_missing_final_file_rejects_archive(archive_env):
    project = _project(archive_env)
    project['output'].unlink()
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


def test_completed_output_outside_project_rejects_archive(archive_env):
    project = _project(archive_env)
    other = _project(archive_env, number=2)
    project['state']['output']['file'] = str(other['output'])
    project['state']['output']['url'] = other['state']['output']['url']
    _json(project['state_file'], project['state'])
    before = _snapshot(project['dir'])
    other_before = _snapshot(other['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert _snapshot(other['dir']) == other_before


@pytest.mark.parametrize('field', ['refined_videos', 'final_videos'])
def test_pending_archive_receipt_cannot_whitelist_another_project_file(archive_env, field):
    project = _project(archive_env)
    other = _project(archive_env, number=2)
    record = {'status': 'prepared', 'refined_videos': [], 'retained_files': []}
    record[field] = [{'url': other['state']['output']['url'], 'kind': 'refined_video'}]
    _json(project['dir'] / archive_env['module'].RECEIPT, record)
    before = _snapshot(project['dir'])
    other_before = _snapshot(other['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert _snapshot(other['dir']) == other_before


@pytest.mark.parametrize('link_kind', ['project', 'internal_file', 'internal_dir'])
def test_symbolic_links_refuse_archive_and_never_delete_target(archive_env, link_kind):
    project = _project(archive_env)
    external = archive_env['root'] / 'unrelated'
    external.mkdir()
    (external / 'valuable.txt').write_text('untouched', encoding='utf-8')
    if link_kind == 'project':
        moved = archive_env['root'] / 'moved-project'
        project['dir'].rename(moved)
        project['dir'].symlink_to(moved, target_is_directory=True)
    elif link_kind == 'internal_file':
        (project['dir'] / 'shortcut.txt').symlink_to(external / 'valuable.txt')
    else:
        (project['dir'] / 'shortcut').symlink_to(external, target_is_directory=True)
    before = _snapshot(archive_env['root'])
    _assert_rejected(_archive(archive_env, project))
    after = _snapshot(archive_env['root'])
    for snapshot in (before, after):
        snapshot.pop('outputs/.codex-edit.lock', None)
    assert after == before
    assert (external / 'valuable.txt').read_text() == 'untouched'


def test_unknown_and_traversal_project_keys_never_touch_other_projects(archive_env):
    project = _project(archive_env)
    before = _snapshot(project['dir'])
    result = archive_env['module'].archive_projects(
        ['../outside', '/absolute/outside', 'unknown-project'], preview=False,
        base_dir=str(archive_env['root']))
    assert result['count'] == 0 and len(result['errors']) == 3
    assert _snapshot(project['dir']) == before


def test_archive_of_one_project_does_not_change_other_library_or_task(archive_env):
    project = _project(archive_env)
    other = _project(archive_env, number=2)
    other_before = _snapshot(other['dir'])
    item_before = server_common.read_library_item(other['item']['id'])
    task_before = {path: Path(path).read_bytes() for path in server_common._task_paths(other['task_id'])}
    assert _archive(archive_env, project)['count'] == 1
    assert _snapshot(other['dir']) == other_before
    assert server_common.read_library_item(other['item']['id']) == item_before
    assert all(Path(path).read_bytes() == data for path, data in task_before.items())
    assert other['task_id'] in server_common.ACTIVE_TASKS


@pytest.mark.parametrize('retention', ['refined', 'merged', 'none'])
@pytest.mark.parametrize('failure', ['unlink', 'library_write'])
def test_failed_cleanup_is_retryable_after_edit_state_has_been_deleted(archive_env, failure, retention):
    project = _project(archive_env, completed=retention == 'refined')
    if retention == 'none':
        _set_merged_records(project)
    if failure == 'unlink':
        original_unlink = Path.unlink
        failing_path = Path(server_common._task_paths(project['task_id'])[1])

        def fail_one_task_file(path, *args, **kwargs):
            if path == failing_path:
                raise OSError('simulated cleanup interruption')
            return original_unlink(path, *args, **kwargs)

        with archive_env['monkeypatch'].context() as patch:
            patch.setattr(Path, 'unlink', fail_one_task_file)
            failed = _archive(archive_env, project)
    else:
        def fail_library_write(*args, **kwargs):
            raise OSError('simulated library write interruption')

        with archive_env['monkeypatch'].context() as patch:
            patch.setattr(server_common, 'write_library_item', fail_library_write)
            failed = _archive(archive_env, project)
    _assert_rejected(failed)
    assert not project['state_file'].exists()
    record = json.loads((project['dir'] / archive_env['module'].RECEIPT).read_text())
    assert record['status'] == 'prepared'
    assert record['video_retention'] == retention
    assert [entry['kind'] for entry in record['final_videos']] == {
        'refined': ['refined_video'], 'merged': ['merged_video'], 'none': []}[retention]
    preserved = {str(path): path.read_bytes() for path in _retained_paths(archive_env, record)}
    videos = [data for path, data in preserved.items() if path.endswith('.mp4')]
    assert videos == {'refined': [FINISHED_VIDEO], 'merged': [b'old unedited merged video'], 'none': []}[retention]
    assert CURRENT_PROMPTS.encode() in preserved.values()
    retried = _archive(archive_env, project)
    assert retried['count'] == 1 and not retried['errors']
    assert retried['projects'][0]['video_retention'] == retention
    assert retried['projects'][0]['archive']['final_videos'] == record['final_videos']
    assert {str(path): path.read_bytes() for path in _retained_paths(archive_env, retried['projects'][0])} == preserved
    assert project['task_id'] not in server_common.ACTIVE_TASKS
    assert server_common.read_library_item(retried['projects'][0]['library']['id'])['archived']


def test_legacy_refined_receipt_remains_retryable_without_new_video_fields(archive_env):
    project = _project(archive_env)
    first = _archive(archive_env, project)
    assert first['count'] == 1 and not first['errors']
    receipt_file = project['dir'] / archive_env['module'].RECEIPT
    record = json.loads(receipt_file.read_text())
    record.pop('final_videos')
    record.pop('video_retention')
    _json(receipt_file, record)
    retained_before = {str(path): path.read_bytes() for path in _retained_paths(archive_env, record)}
    retried = _archive(archive_env, project)
    assert retried['count'] == 1 and not retried['errors']
    row = retried['projects'][0]
    assert row['video_retention'] == 'refined'
    assert row['archive']['final_videos'] == record['refined_videos']
    assert {str(path): path.read_bytes() for path in _retained_paths(archive_env, row)} == retained_before


def test_receipt_final_video_missing_blocks_retry_without_deleting_remaining_data(archive_env):
    project = _project(archive_env, completed=False)
    first = _archive(archive_env, project)
    assert first['count'] == 1 and not first['errors']
    (project['dir'] / 'source.mp4').unlink()
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


def test_export_failure_prevents_any_deletion(archive_env):
    project = _project(archive_env)
    before = _snapshot(project['dir'])

    def fail_export(path, data):
        raise OSError('simulated export failure')

    archive_env['monkeypatch'].setattr(archive_env['module'], '_write_bytes', fail_export)
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before
    assert project['task_id'] in server_common.ACTIVE_TASKS


def test_missing_library_still_exports_task_prompts_and_creates_archive_row(archive_env):
    project = _project(archive_env)
    assert server_common.delete_library_item(project['item']['id'])
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    assert OLDER_PROMPTS in [path.read_text() for path in retained if path.suffix == '.txt']
    assert server_common.read_library_item(result['projects'][0]['library']['id'])['archived']


def test_api_defaults_to_preview_then_executes_the_same_project(archive_env):
    project = _project(archive_env)
    before = _snapshot(project['dir'])
    preview, status = _api('/api/projects/archive', {'project_keys': [project['key']]})
    assert status == 200 and preview['count'] == 1 and not preview['errors']
    assert preview['projects'][0]['delete_count'] > 0
    assert _snapshot(project['dir']) == before
    executed, status = _api('/api/projects/archive',
                            {'project_keys': [project['key']], 'preview': False})
    assert status == 200 and executed['count'] == 1 and not executed['errors']
    assert executed['deleted_bytes'] == preview['projects'][0]['delete_bytes']
    assert server_common.read_library_item(executed['projects'][0]['library']['id'])['archived']
    assert not (project['dir'] / 'source.mp4').exists()


@pytest.mark.parametrize('payload', [None, [], {}, {'project_keys': 'one'},
    {'project_keys': []}, {'project_keys': [None]}, {'project_keys': ['  ']},
    {'project_keys': ['one'], 'preview': 'false'}, {'project_keys': ['one'], 'preview': 0},
    {'project_keys': ['one'] * 201}])
def test_api_invalid_archive_input_is_400_without_mutation(archive_env, payload):
    project = _project(archive_env)
    before = _snapshot(project['dir'])
    body, status = _api('/api/projects/archive', payload)
    assert status == 400 and body['status'] == 'error' and body['message']
    assert _snapshot(project['dir']) == before


@pytest.mark.parametrize('preview', [True, False])
def test_api_gate_blocks_preview_and_execute(archive_env, preview):
    project = _project(archive_env)
    before = _snapshot(archive_env['root'])
    body, status = _api('/api/projects/archive',
                        {'project_keys': [project['key']], 'preview': preview}, allowed=False)
    assert status == 401 and body['message'] == 'gate denied'
    assert _snapshot(archive_env['root']) == before


def test_projects_endpoint_lists_archived_state_and_keeps_global_counts(archive_env):
    project = _project(archive_env)
    other = _project(archive_env, number=2)
    assert _archive(archive_env, project)['count'] == 1
    all_body, status = _api('/api/projects', method='GET')
    assert status == 200 and all_body['total_count'] == 2
    assert all_body['counts']['all'] == 2 and all_body['counts']['archived'] == 1
    assert all_body['counts']['saved'] == 1
    filtered, status = _api('/api/projects?state=archived', method='GET')
    assert status == 200 and filtered['filtered_count'] == 1
    assert filtered['counts'] == all_body['counts']
    row = filtered['projects'][0]
    assert row['project_key'] == project['key'] and row['state'] == 'archived'
    assert row['archived'] and not row['saved'] and not row['task'] and not row['sub_jobs']
    saved, status = _api('/api/projects?state=saved', method='GET')
    assert status == 200
    assert [row['project_key'] for row in saved['projects']] == [other['key']]


def test_archived_video_stays_visible_in_gallery_without_job_state(archive_env):
    project = _project(archive_env)
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not project['state_file'].exists()
    gallery = server_common.scan_gallery(base_dir=str(archive_env['root']))
    assert gallery['totals'] == {'images': 0, 'videos': 1, 'bytes': len(FINISHED_VIDEO)}
    assert len(gallery['groups']) == 1
    items = gallery['groups'][0]['items']
    assert len(items) == 1 and items[0]['type'] == 'video'
    assert items[0]['kind'] == 'merged' and items[0]['is_edited'] is True
    assert items[0]['size'] == len(FINISHED_VIDEO)


def test_archive_asset_stats_include_prompt_and_beat_documents(archive_env):
    project = _project(archive_env)
    result = _archive(archive_env, project)
    retained = _retained_paths(archive_env, result['projects'][0])
    stats = server_common._proj_asset_stats(project['key'], project['title'], str(archive_env['root']))
    assert stats['file_count'] == len(retained)
    assert stats['bytes'] == sum(path.stat().st_size for path in retained)
    assert stats['bytes'] > len(FINISHED_VIDEO)
    assert stats['cover'] is None


def test_manifest_sync_does_not_recreate_deleted_generation_manifest(archive_env):
    project = _project(archive_env)
    assert _archive(archive_env, project)['count'] == 1
    assert not (project['dir'] / 'manifest.json').exists()
    before = _snapshot(project['dir'])
    server.sync_project_manifest_with_disk(str(project['dir']))
    assert _snapshot(project['dir']) == before
    assert not (project['dir'] / 'manifest.json').exists()


@pytest.mark.parametrize('mutation', ['write_manifest', 'get_or_create_task', 'prepare_task_for_run'])
def test_generation_mutations_refuse_archived_project(archive_env, mutation):
    project = _project(archive_env)
    assert _archive(archive_env, project)['count'] == 1
    before = _snapshot(project['dir'])
    with pytest.raises(ValueError, match='归档'):
        if mutation == 'write_manifest':
            server_common.write_manifest(str(project['dir']), {'frames': []})
        else:
            getattr(server_common, mutation)(
                'new_job', {'type': 'frames', 'project_key': project['key'], 'theme': project['title']})
    assert _snapshot(project['dir']) == before
    assert 'new_job' not in server_common.ACTIVE_TASKS


@pytest.mark.parametrize('output', ['bad record', ['bad record'], 123,
    {'file': 123}, {'file': ['bad path']}, {'file': {'bad': 'path'}}, {'url': 123}])
def test_malformed_completed_output_returns_project_error(archive_env, output):
    project = _project(archive_env)
    project['state']['output'] = output
    _json(project['state_file'], project['state'])
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


@pytest.mark.parametrize('field,value', [
    ('refined_videos', None), ('refined_videos', 'bad record'),
    ('refined_videos', [123]), ('refined_videos', [{'url': 123}]),
    ('final_videos', None), ('final_videos', 'bad record'),
    ('final_videos', [123]), ('final_videos', [{'url': 123}]),
    ('retained_files', None), ('retained_files', {'url': '/outputs/unknown'}),
    ('retained_files', ['bad record']), ('retained_files', [{}]),
])
def test_malformed_prepared_receipt_returns_project_error(archive_env, field, value):
    project = _project(archive_env)
    record = {'status': 'prepared', 'refined_videos': [], 'retained_files': []}
    record[field] = value
    _json(project['dir'] / archive_env['module'].RECEIPT, record)
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


def test_receipt_with_invalid_final_video_kind_refuses_cleanup(archive_env):
    project = _project(archive_env, completed=False)
    _json(project['dir'] / archive_env['module'].RECEIPT, {
        'status': 'prepared', 'refined_videos': [], 'retained_files': [],
        'final_videos': [{'url': f'/outputs/{project["dir"].name}/source.mp4', 'kind': 'prompts'}]})
    before = _snapshot(project['dir'])
    _assert_rejected(_archive(archive_env, project))
    assert _snapshot(project['dir']) == before


@pytest.mark.parametrize('path', ['/api/generate_frames', '/api/edit_prompts', '/api/library/item'])
def test_stale_page_mutation_api_returns_archived_409(archive_env, path):
    project = _project(archive_env)
    assert _archive(archive_env, project)['count'] == 1
    before = _snapshot(project['dir'])
    payload = {'project_key': project['key'], 'title': project['key'],
               'prompt_block': CURRENT_PROMPTS}
    if path == '/api/library/item':
        payload = {'item': project['item']}
    body, status = _api(path, payload)
    assert status == 409 and body['failure_code'] == 'PROJECT_ARCHIVED'
    assert _snapshot(project['dir']) == before


def test_stale_library_record_cannot_overwrite_archive(archive_env):
    project = _project(archive_env)
    assert _archive(archive_env, project)['count'] == 1
    before = server_common.read_library_item(project['item']['id'])
    with pytest.raises(ValueError, match='归档'):
        server_common.write_library_item(project['item'])
    assert server_common.read_library_item(project['item']['id']) == before


def test_no_library_failed_compaction_stays_listed_and_retries_from_receipt(archive_env):
    project = _project(archive_env)
    assert server_common.delete_library_item(project['item']['id'])

    def fail_library_write(*args, **kwargs):
        raise OSError('simulated library write interruption')

    with archive_env['monkeypatch'].context() as patch:
        patch.setattr(server_common, 'write_library_item', fail_library_write)
        failed = _archive(archive_env, project)
    _assert_rejected(failed)
    assert project['task_id'] not in server_common.ACTIVE_TASKS
    assert server_common.read_library() == []
    rows = server_common.build_projects_index(base_dir=str(archive_env['root']), with_assets=False)
    pending = next(row for row in rows if row['project_key'] == project['key'])
    assert pending['archived'] and pending['archive_pending']
    assert pending['state'] == 'archived'
    preserved = {str(path): path.read_bytes() for path in _retained_paths(archive_env, pending['archive'])}
    retried = _archive(archive_env, project)
    assert retried['count'] == 1 and not retried['errors']
    assert {str(path): path.read_bytes() for path in _retained_paths(archive_env, retried['projects'][0])} == preserved
    assert server_common.read_library_item(retried['projects'][0]['library']['id'])['archived']


def test_current_library_embedded_beats_take_precedence_over_old_task(archive_env):
    project = _project(archive_env, beats=False)
    current = {'beats': [{'index': 1, 'operation': 'current library operation'}]}
    older = {'beats': [{'index': 1, 'operation': 'obsolete task operation'}]}
    item = server_common.read_library_item(project['item']['id'])
    item['reverse_beats'] = current
    server_common.write_library_item(item)
    server_common.ACTIVE_TASKS[project['task_id']]['result']['reverse_beats'] = older
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    retained = _retained_paths(archive_env, result['projects'][0])
    beat_documents = [json.loads(path.read_text()) for path in retained
                      if path.suffix == '.json' and ('节拍' in path.name or path.name == 'reverse_beats.json')]
    assert current in beat_documents
    assert older not in beat_documents


def test_waiting_task_save_cannot_resurrect_archived_task_files(archive_env):
    project = _project(archive_env)
    original_paths = server_common._task_paths
    snapshot_taken = threading.Event()
    outcome = []

    def observed_paths(task_id, tasks_dir=None):
        paths = original_paths(task_id, tasks_dir)
        if threading.current_thread().name == 'late-task-save':
            snapshot_taken.set()
        return paths

    archive_env['monkeypatch'].setattr(server_common, '_task_paths', observed_paths)
    writer = threading.Thread(name='late-task-save', daemon=True,
                              target=lambda: outcome.append(server_common.save_task_to_disk(project['task_id'])))
    with server_common._TASK_IO_LOCK:
        writer.start()
        assert snapshot_taken.wait(timeout=2), 'writer never took the task snapshot'
        archived = _archive(archive_env, project)
        assert archived['count'] == 1 and not archived['errors']
        assert project['task_id'] not in server_common.ACTIVE_TASKS
    writer.join(timeout=2)
    assert not writer.is_alive() and outcome == [False]
    assert all(not Path(path).exists() for path in original_paths(project['task_id']))


def test_bulk_archive_last_shared_reverse_owner_cleans_job_after_both_export_beats(archive_env):
    projects = [_project(archive_env, number=number, beats=False) for number in (1, 2)]
    replica_id = 'replica_bulk_shared_1234'
    replica_dir = archive_env['outputs'] / 'replica_jobs' / replica_id
    _json(replica_dir / 'timelapse_beats.json', BEATS)
    _json(replica_dir / '.replica_pipeline.json', {'job_id': replica_id, 'beats': BEATS})
    _json(replica_dir / 'review_frames' / 'evidence.json', {'large': 'evidence'})
    (replica_dir / 'source.mp4').write_bytes(b'shared original')
    for project in projects:
        item = server_common.read_library_item(project['item']['id'])
        item['replica_job_id'] = replica_id
        server_common.write_library_item(item)
    result = archive_env['module'].archive_projects(
        [project['key'] for project in projects], preview=False, base_dir=str(archive_env['root']))
    assert result['count'] == 2 and not result['errors']
    assert not replica_dir.exists()
    for row in result['projects']:
        retained = _retained_paths(archive_env, row)
        assert any(path.suffix == '.json' and json.loads(path.read_text()) == BEATS for path in retained)
        assert FINISHED_VIDEO in [path.read_bytes() for path in retained if path.suffix == '.mp4']


@pytest.mark.parametrize('holder', ['frame_run', 'worker_thread'])
def test_cancelled_generation_still_closing_blocks_archive(archive_env, holder):
    project = _project(archive_env)
    server_common.ACTIVE_TASKS[project['task_id']]['status'] = 'cancelled'
    before = _snapshot(project['dir'])
    release = threading.Event()
    entered = threading.Event()
    worker = None
    if holder == 'frame_run':
        server_common._ACTIVE_FRAME_RUNS[str(project['dir'])] = project['task_id']
    else:
        def closing_worker(task_id):
            entered.set()
            release.wait(timeout=5)

        worker = threading.Thread(target=closing_worker, args=(project['task_id'],), daemon=True,
                                  name='archive-test-closing-worker')
        worker.start()
        assert entered.wait(timeout=2)
    try:
        _assert_rejected(_archive(archive_env, project))
        assert _snapshot(project['dir']) == before
    finally:
        release.set()
        if worker:
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_prepared_receipt_remembers_legacy_task_identity_for_cleanup_retry(archive_env):
    project = _project(archive_env)
    original_id = project['task_id']
    task = server_common.ACTIVE_TASKS.pop(original_id)
    server_common.delete_task_files(original_id)
    # A historical task's derived key remains run_1__<title>, but neither saved
    # dimensions nor result has an explicit project_key for retry matching.
    legacy_id = '1'
    task['id'] = legacy_id
    task['dimensions'].pop('project_key')
    task['result'].pop('project_key')
    server_common.ACTIVE_TASKS[legacy_id] = task
    assert server_common.save_task_to_disk(legacy_id)
    project['task_id'] = legacy_id
    result_file = Path(server_common._task_paths(legacy_id)[2])
    original_unlink = Path.unlink

    def interrupt_result_cleanup(path, *args, **kwargs):
        if path == result_file:
            raise OSError('simulated legacy task cleanup interruption')
        return original_unlink(path, *args, **kwargs)

    with archive_env['monkeypatch'].context() as patch:
        patch.setattr(Path, 'unlink', interrupt_result_cleanup)
        failed = _archive(archive_env, project)
    _assert_rejected(failed)
    record = json.loads((project['dir'] / archive_env['module'].RECEIPT).read_text())
    assert record['status'] == 'prepared' and record['task_ids'] == [legacy_id]
    assert not project['state_file'].exists() and result_file.exists()
    retried = _archive(archive_env, project)
    assert retried['count'] == 1 and not retried['errors']
    assert legacy_id not in server_common.ACTIVE_TASKS
    assert all(not Path(path).exists() for path in server_common._task_paths(legacy_id))


@pytest.mark.parametrize('media', ['frame', 'video'])
def test_multipart_stale_upload_is_rejected_before_any_file_creation(archive_env, media):
    project = _project(archive_env)
    assert _archive(archive_env, project)['count'] == 1
    archive_env['monkeypatch'].setattr(server, 'access_ok', lambda handler: True)
    boundary = 'archive-test-boundary'
    fields = {'title': project['key'], 'sequence' if media == 'frame' else 'slot': '1'}
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    file_field = 'image' if media == 'frame' else 'video'
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="new.bin"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n'.encode() + b'fake media bytes\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    before = _snapshot(archive_env['root'])
    body, status = _api('/api/upload_' + media, raw_body=b''.join(parts),
                        content_type='multipart/form-data; boundary=' + boundary)
    assert status == 409 and body['failure_code'] == 'PROJECT_ARCHIVED'
    assert _snapshot(archive_env['root']) == before


def test_archived_request_recognizes_historical_ascii_directory_by_title(archive_env):
    title = '崖边巨石改造成海景卧室'
    legacy = archive_env['outputs'] / server_common._legacy_ascii_project_name(title)
    legacy.mkdir()
    _json(legacy / archive_env['module'].RECEIPT,
          {'status': 'archived', 'project_key': title, 'title': title})
    assert archive_env['module'].archived_request({'title': title}, base_dir=str(archive_env['root']))
    assert archive_env['module'].archived_request({'title': title})


def _real_cover(path, color, size=(1200, 800)):
    from PIL import Image
    Image.new('RGB', size, color).save(path)


def test_archive_retains_selected_thumbnail_and_preview_is_read_only(archive_env):
    project = _project(archive_env)
    _real_cover(project['dir'] / 'cover.webp', 'red')
    selected = project['dir'] / 'cover_selected.webp'
    _real_cover(selected, 'blue')
    manifest = json.loads((project['dir'] / 'manifest.json').read_text())
    manifest['cover_roles'] = {'project': str(selected)}
    _json(project['dir'] / 'manifest.json', manifest)
    before = _snapshot(archive_env['root'])
    preview = _archive(archive_env, project, preview=True)
    assert preview['count'] == 1 and not preview['errors']
    cover_entry, = [entry for entry in preview['projects'][0]['retained_files'] if entry['kind'] == 'cover']
    assert cover_entry['name'] == archive_env['module'].ARCHIVE_COVER
    assert preview['projects'][0]['archive']['cover_url'] == cover_entry['url']
    assert _snapshot(archive_env['root']) == before

    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    public = result['projects'][0]
    thumbnail = project['dir'] / archive_env['module'].ARCHIVE_COVER
    from PIL import Image
    with Image.open(thumbnail) as image:
        assert image.width <= 640 and image.height <= 640
        red, green, blue = image.getpixel((0, 0))
        assert blue > 240 and red < 15 and green < 15
    assert not selected.exists() and not (project['dir'] / 'cover.webp').exists()
    item = server_common.read_library_item(public['library']['id'])
    assert item['archive']['cover_url'] == cover_entry['url']
    assert server_common.library_index_entry(item)['cover'] == cover_entry['url']
    for with_assets in (False, True):
        row, = server_common.build_projects_index(tasks=[], library_items=[], ledger_rows=[],
            base_dir=str(archive_env['root']), with_assets=with_assets)
        assert row['cover'] == row['archive']['cover_url'] == cover_entry['url']
        assert row['image_count'] == 0
        if with_assets:
            assert row['assets']['cover'] == cover_entry['url']
    gallery = server_common.scan_gallery(base_dir=str(archive_env['root']))
    assert gallery['totals']['images'] == 0
    assert gallery['totals']['videos'] == 1


def test_data_only_archive_can_keep_a_cover_without_final_video(archive_env):
    project = _project(archive_env, completed=False)
    _set_merged_records(project)
    _real_cover(project['dir'] / 'cover.webp', 'green')
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    archive = result['projects'][0]['archive']
    assert archive['video_retention'] == 'none' and not archive['final_videos']
    assert archive['cover_url']
    assert len([entry for entry in archive['retained_files'] if entry['kind'] == 'cover']) == 1


def test_prepared_archive_retry_preserves_the_same_thumbnail(archive_env):
    project = _project(archive_env)
    _real_cover(project['dir'] / 'cover.webp', 'orange')
    with archive_env['monkeypatch'].context() as patch:
        patch.setattr(server_common, 'write_library_item', lambda item: (_ for _ in ()).throw(OSError('disk full')))
        interrupted = _archive(archive_env, project)
    assert interrupted['count'] == 0 and interrupted['errors']
    thumbnail = project['dir'] / archive_env['module'].ARCHIVE_COVER
    saved_bytes = thumbnail.read_bytes()
    assert json.loads((project['dir'] / archive_env['module'].RECEIPT).read_text())['status'] == 'prepared'
    result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['errors']
    assert thumbnail.read_bytes() == saved_bytes
    again = _archive(archive_env, project)
    assert again['projects'][0]['archive']['cover_url'] == result['projects'][0]['archive']['cover_url']
    assert thumbnail.read_bytes() == saved_bytes


def test_old_archive_cover_repair_is_durable_and_idempotent(archive_env):
    import shutil
    import subprocess
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg is required for the real video thumbnail repair')
    project = _project(archive_env)
    subprocess.run([server_common.resolve_binary('ffmpeg'), '-hide_banner', '-loglevel', 'error',
        '-nostdin', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=96x64:r=4', '-t', '0.5',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(project['output'])], check=True)
    with archive_env['monkeypatch'].context() as patch:
        patch.setattr(archive_env['module'], '_thumbnail_bytes', lambda *args, **kwargs: None)
        result = _archive(archive_env, project)
    assert result['count'] == 1 and not result['projects'][0]['archive']['cover_url']
    # Include legacy refined-only receipts in the repair contract.
    receipt_file = project['dir'] / archive_env['module'].RECEIPT
    record = json.loads(receipt_file.read_text())
    record.pop('final_videos')
    record.pop('video_retention')
    _json(receipt_file, record)
    before = _snapshot(project['dir'])
    repaired = archive_env['module'].restore_archive_covers([project['key']], base_dir=str(archive_env['root']))
    assert repaired['count'] == 1 and not repaired['errors']
    assert repaired['projects'][0]['restored']
    for relative, contents in before.items():
        if relative != archive_env['module'].RECEIPT:
            assert (project['dir'] / relative).read_bytes() == contents
    item = server_common.read_library_item(result['projects'][0]['library']['id'])
    assert item['archive']['cover_url'] == repaired['projects'][0]['cover_url']
    row, = server_common.build_projects_index(tasks=[], library_items=[], ledger_rows=[],
        base_dir=str(archive_env['root']), with_assets=False)
    assert row['cover'] == repaired['projects'][0]['cover_url']
    after = _snapshot(archive_env['root'])
    again = archive_env['module'].restore_archive_covers([project['key']], base_dir=str(archive_env['root']))
    assert again['count'] == 1 and not again['errors']
    assert not again['projects'][0]['restored']
    assert _snapshot(archive_env['root']) == after


@pytest.mark.parametrize('failure', ['extract', 'library'])
def test_old_archive_cover_repair_failure_preserves_original_data(archive_env, failure):
    project = _project(archive_env)
    result = _archive(archive_env, project)
    assert result['count'] == 1
    before = _snapshot(archive_env['root'])
    if failure == 'library':
        image = io.BytesIO()
        from PIL import Image
        Image.new('RGB', (20, 20), 'red').save(image, format='JPEG')
        archive_env['monkeypatch'].setattr(archive_env['module'], '_thumbnail_bytes',
                                         lambda *args, **kwargs: image.getvalue())
        archive_env['monkeypatch'].setattr(server_common, 'write_library_item',
                                         lambda item: (_ for _ in ()).throw(OSError('disk full')))
    repaired = archive_env['module'].restore_archive_covers([project['key']], base_dir=str(archive_env['root']))
    assert repaired['count'] == 0 and repaired['errors']
    assert _snapshot(archive_env['root']) == before
