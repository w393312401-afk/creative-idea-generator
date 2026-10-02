"""Gallery categories include published edits without exposing the editing workspace."""
import json
from urllib.parse import quote

import pytest

from server_common import scan_gallery


def write(path, data=b'media'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def edit_job(project, *, status='completed', job_id='a' * 32, output='work/final.mp4'):
    directory = project / 'codex_edits' / job_id
    final = write(directory / output)
    state = {'id': job_id, 'status': status, 'output': {'file': str(final)}}
    (directory / '.state.json').write_text(json.dumps(state), encoding='utf-8')
    return directory, final, state


def items(base):
    return [item for group in scan_gallery(base_dir=str(base))['groups'] for item in group['items']]


def test_completed_edits_are_finished_movies_without_input_or_intermediates(tmp_path):
    project = tmp_path / 'outputs' / '中文 项目 # 100%'
    write(project / 'frames' / 'frame.webp')
    write(project / 'videos' / 'clip.mp4')
    write(project / 'merged.mp4')
    directory, final, _ = edit_job(project)
    for artifact in ('input.mp4', '.agent.json', 'work/intermediate.mp4',
                     'work/evidence/contact.jpg', 'work/qa/frame.jpg', 'work/.internal.mp4'):
        write(directory / artifact)

    scanned = items(tmp_path)

    assert {item['name']: item['kind'] for item in scanned} == {
        'frame.webp': 'frame', 'clip.mp4': 'video', 'merged.mp4': 'merged', 'final.mp4': 'merged',
    }
    edited = next(item for item in scanned if item['name'] == 'final.mp4')
    assert edited['is_edited'] is True
    assert edited['path'] == final.relative_to(tmp_path).as_posix()
    # Gallery URLs are raw paths, encoded exactly once by galleryEncodeUrl.
    assert edited['url'] == '/' + edited['path']
    assert scan_gallery(base_dir=str(tmp_path))['totals']['videos'] == 3


@pytest.mark.parametrize('status', ['queued', 'running', 'failed', 'cancelled', 'interrupted'])
def test_unpublished_edit_outputs_do_not_enter_gallery(tmp_path, status):
    edit_job(tmp_path / 'outputs' / 'project', status=status)
    assert items(tmp_path) == []


@pytest.mark.parametrize('invalid', ['missing', 'empty', 'wrong_id', 'invalid_json', 'non_object',
                                    'outside_workspace', 'other_job', 'private', 'non_video', 'symlink'])
def test_invalid_completed_edit_output_is_not_published(tmp_path, invalid):
    project = tmp_path / 'outputs' / 'project'
    directory, final, state = edit_job(project)
    if invalid == 'missing':
        final.unlink()
    elif invalid == 'empty':
        final.write_bytes(b'')
    elif invalid == 'wrong_id':
        state['id'] = 'b' * 32
    elif invalid == 'invalid_json':
        (directory / '.state.json').write_text('{', encoding='utf-8')
        assert items(tmp_path) == []
        return
    elif invalid == 'non_object':
        state = []
    elif invalid == 'outside_workspace':
        state['output']['file'] = str(write(directory / 'input.mp4'))
    elif invalid == 'other_job':
        state['output']['file'] = str(write(project / 'codex_edits' / ('b' * 32) / 'work' / 'other.mp4'))
    elif invalid == 'private':
        state['output']['file'] = str(write(directory / 'work' / '.private' / 'final.mp4'))
    elif invalid == 'non_video':
        state['output']['file'] = str(write(directory / 'work' / 'sheet.jpg'))
    elif invalid == 'symlink':
        final.unlink()
        final.symlink_to(write(tmp_path / 'outside.mp4'))
    (directory / '.state.json').write_text(json.dumps(state), encoding='utf-8')
    assert items(tmp_path) == []


def test_symlink_and_private_media_are_not_scanned(tmp_path):
    project = tmp_path / 'outputs' / 'project'
    outside = write(tmp_path / 'outside.mp4')
    write(project / '.unpublished.mp4')
    (project / 'link.mp4').symlink_to(outside)
    write(tmp_path / 'external' / 'clip.mp4')
    (project / 'videos').symlink_to(tmp_path / 'external', target_is_directory=True)
    write(tmp_path / 'outputs' / '.internal' / 'clip.mp4')
    (tmp_path / 'outputs' / 'linked-project').symlink_to(tmp_path / 'external', target_is_directory=True)
    assert items(tmp_path) == []


@pytest.mark.parametrize('source_form', ['file', 'absolute', 'url'])
def test_manifest_distinguishes_legacy_finished_movie_inside_videos(tmp_path, source_form):
    project = tmp_path / 'outputs' / '项目 # 100%'
    movie = write(project / 'videos' / 'result.mp4')
    write(project / 'videos' / 'clip.mp4')
    relative = movie.relative_to(tmp_path).as_posix()
    merged = {'status': 'success'}
    if source_form == 'file':
        merged['file'] = relative
    elif source_form == 'absolute':
        merged['file'] = str(movie)
    else:
        merged['url'] = '/' + quote(relative, safe='/') + '?v=123'
    (project / 'manifest.json').write_text(json.dumps({'merged_video': merged}), encoding='utf-8')
    assert {item['name']: item['kind'] for item in items(tmp_path)} == {
        'result.mp4': 'merged', 'clip.mp4': 'video',
    }


def test_manifest_cannot_add_private_or_unscanned_media(tmp_path):
    project = tmp_path / 'outputs' / 'project'
    hidden = write(project / 'private' / 'result.mp4')
    (project / 'manifest.json').write_text(json.dumps({
        'merged_video': {'status': 'success', 'file': str(hidden)},
    }), encoding='utf-8')
    assert items(tmp_path) == []
