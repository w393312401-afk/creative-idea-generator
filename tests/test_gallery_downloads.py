"""Selected downloads must contain exactly the public gallery files requested."""
import io
import json
import os
from pathlib import Path
import types
from email.message import Message
import zipfile

import pytest

import gallery_downloads as downloads
from server_common import gallery_delete_files


@pytest.fixture(autouse=True)
def clean_downloads():
    downloads._cleanup()
    yield
    downloads._cleanup()


def write(base, relative, content=b'video bytes'):
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_zip_contains_only_selected_files_preserving_project_names(tmp_path):
    first = 'outputs/项目甲/成片 #100%.mp4'
    second = 'outputs/项目乙/frames/画面.webp'
    write(tmp_path, first, b'movie')
    write(tmp_path, second, b'image')
    write(tmp_path, 'outputs/项目甲/frames/unselected.webp')
    result = downloads.prepare_download([first, second, first], tmp_path)
    assert result['count'] == 2
    assert result['filename'].endswith('.zip')
    with downloads.open_download(result['url'].rsplit('/', 1)[-1]) as (stream, info):
        assert result['size_bytes'] == os.fstat(stream.fileno()).st_size
        with zipfile.ZipFile(stream) as archive:
            assert archive.namelist() == ['项目甲/成片 #100%.mp4', '项目乙/frames/画面.webp']
            assert archive.read('项目甲/成片 #100%.mp4') == b'movie'
            assert archive.read('项目乙/frames/画面.webp') == b'image'
            assert archive.testzip() is None


@pytest.mark.parametrize('relative', [
    'outside.mp4', 'outputs/../outside.mp4', 'outputs/project/manifest.json',
    'outputs/project/codex_edits/job/input.mp4',
    'outputs/project/codex_edits/job/work/evidence/frame.jpg',
    'outputs/project/codex_edits/job/work/unfinished.mp4',
    'outputs/project/intermediate/frame.png',
])
def test_private_and_unlisted_files_never_pack(tmp_path, relative):
    write(tmp_path, relative)
    with pytest.raises(downloads.GalleryDownloadError):
        downloads.prepare_download([relative], tmp_path)
    assert not downloads._ARCHIVES


@pytest.mark.parametrize('selection', [None, [], 'outputs/a.mp4', [123], [''], ['x'] * 1001])
def test_selection_shape_checked(selection):
    with pytest.raises(downloads.GalleryDownloadError):
        downloads.prepare_download(selection)


@pytest.mark.parametrize('kind', ['parent', 'leaf'])
def test_symlink_cannot_download_or_delete_outside_outputs(tmp_path, kind):
    secret = write(tmp_path, 'outside/secret.mp4', b'keep me')
    project = tmp_path / 'outputs/project'
    project.mkdir(parents=True)
    if kind == 'parent':
        (project / 'videos').symlink_to(secret.parent, target_is_directory=True)
        path = 'outputs/project/videos/secret.mp4'
    else:
        (project / 'secret.mp4').symlink_to(secret)
        path = 'outputs/project/secret.mp4'
    with pytest.raises(downloads.GalleryDownloadError):
        downloads.prepare_download([path], tmp_path)
    result = gallery_delete_files([path], base_dir=str(tmp_path), remove_empty_projects=False)
    assert result['deleted'] == []
    assert len(result['failed']) == 1
    assert secret.read_bytes() == b'keep me'


def test_delete_only_selected_media_preserves_project_and_intermediates(tmp_path):
    path = 'outputs/project/final.mp4'
    write(tmp_path, path)
    manifest = write(tmp_path, 'outputs/project/manifest.json', b'{"title":"keep"}')
    intermediate = write(tmp_path, 'outputs/project/intermediate/frame.jpg')
    result = gallery_delete_files([path], base_dir=str(tmp_path), remove_empty_projects=False)
    assert result['deleted'] == [path]
    assert result['removed_project_dirs'] == []
    assert result['affected_project_dirs'] == [str(manifest.parent)]
    assert manifest.read_bytes() == b'{"title":"keep"}'
    assert intermediate.exists()


def test_capacity_busy_expiration_and_cleanup(tmp_path, monkeypatch):
    path = 'outputs/project/final.mp4'
    write(tmp_path, path)
    downloads._BUILD_SLOT.acquire()
    try:
        with pytest.raises(downloads.GalleryDownloadError) as error:
            downloads.prepare_download([path], tmp_path)
        assert error.value.status == 409
    finally:
        downloads._BUILD_SLOT.release()
    monkeypatch.setattr(downloads, 'MAX_BYTES', 1)
    with pytest.raises(downloads.GalleryDownloadError, match='8 GB'):
        downloads.prepare_download([path], tmp_path)
    monkeypatch.setattr(downloads, 'MAX_BYTES', 1024)
    result = downloads.prepare_download([path], tmp_path)
    token = result['url'].rsplit('/', 1)[-1]
    archive = Path(downloads._ARCHIVES[token]['path'])
    downloads._ARCHIVES[token]['expires_at'] = 0
    with pytest.raises(downloads.GalleryDownloadError) as error:
        with downloads.open_download(token):
            pytest.fail('expired archive opened')
    assert error.value.status == 410
    assert not archive.exists()
    assert not downloads._ARCHIVES


def test_missing_selected_file_returns_no_partial_zip(tmp_path, monkeypatch):
    first, second = 'outputs/project/a.mp4', 'outputs/project/b.mp4'
    write(tmp_path, first)
    write(tmp_path, second)
    original = downloads.scan_gallery

    def scan_then_remove(**kwargs):
        data = original(**kwargs)
        (tmp_path / second).unlink()
        return data

    monkeypatch.setattr(downloads, 'scan_gallery', scan_then_remove)
    with pytest.raises(downloads.GalleryDownloadError):
        downloads.prepare_download([first, second], tmp_path)
    assert not downloads._ARCHIVES


def request(path, method='POST', body=None, gate=True):
    import server
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.command = path, method
    handler.client_address = ('127.0.0.1', 1234)
    handler.server = types.SimpleNamespace(server_port=8085)
    handler.headers = Message()
    encoded = json.dumps(body).encode() if body is not None else b''
    for key, value in {'Host': '127.0.0.1:8085', 'Content-Type': 'application/json', 'Content-Length': str(len(encoded))}.items():
        handler.headers[key] = value
    handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
    handler._gate = lambda **kwargs: gate
    replies, headers, statuses = [], {}, []
    handler._send_json = lambda payload, status=200: replies.append((status, payload))
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = lambda: None
    getattr(handler, 'do_' + method)()
    return replies, statuses, headers, handler.wfile.getvalue()


def test_api_prepare_gated_then_token_download_streamed(tmp_path, monkeypatch):
    path = 'outputs/project/final.mp4'
    write(tmp_path, path, b'actual payload')
    monkeypatch.setattr(downloads, 'BASE_DIR', tmp_path)
    assert request('/api/gallery/download-zip', body={'paths': [path]}, gate=False)[0] == []
    assert not downloads._ARCHIVES
    replies = request('/api/gallery/download-zip', body={'paths': [path]})[0]
    assert replies[0][0] == 200
    package = replies[0][1]
    replies, statuses, headers, contents = request(package['url'], 'GET', gate=False)
    assert not replies and statuses == [200]
    assert headers['Content-Type'] == 'application/zip'
    assert headers['Content-Length'] == str(len(contents))
    assert 'attachment;' in headers['Content-Disposition']
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        assert archive.read('project/final.mp4') == b'actual payload'
    assert request('/api/gallery/download-zip/wrong', 'GET')[0][0][0] == 410
    assert request('/api/gallery/download-zip', body=[])[0][0][0] == 400


def test_api_delete_respects_explicit_scope(monkeypatch):
    import server
    calls = []
    def delete(paths, **kwargs):
        calls.append((paths, kwargs))
        return {'deleted': paths, 'failed': [], 'affected_project_dirs': []}
    monkeypatch.setattr(server, 'gallery_delete_files', delete)
    path = 'outputs/project/final.mp4'
    assert request('/api/gallery/delete', body={'paths': [path], 'remove_empty_projects': False})[0][0][0] == 200
    assert calls == [([path], {'remove_empty_projects': False})]
    assert request('/api/gallery/delete', body={'paths': [path], 'remove_empty_projects': 'false'})[0][0][0] == 400
    assert len(calls) == 1


def test_selected_delete_keeps_unselected_png_webp_and_png_manifest_entries(tmp_path, monkeypatch):
    """Exercise the real route and sync: deleting a video must not migrate frames."""
    import server
    from PIL import Image

    project = tmp_path / 'outputs/project'
    frames = project / 'frames'
    frames.mkdir(parents=True)
    for name, color in [('img_001.png', 'red'), ('img_001.webp', 'blue'), ('img_002.png', 'green')]:
        Image.new('RGB', (4, 4), color).save(frames / name)
    selected = 'outputs/project/final.mp4'
    write(tmp_path, selected)
    manifest_file = project / 'manifest.json'
    manifest_file.write_text(json.dumps({
        'title': 'project',
        'frames': [
            {'slot': slot, 'sequence': slot, 'file': f'outputs/project/frames/img_{slot:03d}.png',
             'url': f'/outputs/project/frames/img_{slot:03d}.png', 'prompt': f'preserve {slot}'}
            for slot in (1, 2)
        ],
        'videos': [], 'merged_video': {'file': selected},
    }))
    before = {str(path.relative_to(frames)): path.read_bytes() for path in frames.rglob('*') if path.is_file()}
    monkeypatch.setattr(server, '__file__', str(tmp_path / 'server.py'))
    monkeypatch.setattr(server, 'DB_FILE', str(tmp_path / 'missing-library.json'))
    monkeypatch.setattr(server, 'gallery_delete_files', lambda paths, **kwargs: gallery_delete_files(paths, base_dir=str(tmp_path), **kwargs))

    replies = request('/api/gallery/delete', body={'paths': [selected], 'remove_empty_projects': False})[0]

    assert replies[0][0] == 200
    assert replies[0][1]['deleted'] == [selected]
    assert replies[0][1]['removed_project_dirs'] == []
    assert not (tmp_path / selected).exists()
    after = {str(path.relative_to(frames)): path.read_bytes() for path in frames.rglob('*') if path.is_file()}
    assert after == before
    manifest = json.loads(manifest_file.read_text())
    assert 'merged_video' not in manifest
    assert [frame['file'] for frame in manifest['frames']] == [
        'outputs/project/frames/img_001.png', 'outputs/project/frames/img_002.png',
    ]
    assert [frame['prompt'] for frame in manifest['frames']] == ['preserve 1', 'preserve 2']


def test_default_manifest_sync_still_migrates_legacy_png(tmp_path, monkeypatch):
    import server
    from PIL import Image

    project = tmp_path / 'outputs/project'
    frames = project / 'frames'
    frames.mkdir(parents=True)
    source = frames / 'img_001.png'
    Image.new('RGB', (4, 4), 'red').save(source)
    (project / 'manifest.json').write_text(json.dumps({'title': 'project', 'frames': [], 'videos': []}))
    monkeypatch.setattr(server, '__file__', str(tmp_path / 'server.py'))
    monkeypatch.setattr(server, 'DB_FILE', str(tmp_path / 'missing-library.json'))

    server.sync_project_manifest_with_disk(str(project))

    assert not source.exists()
    assert (frames / 'img_001.webp').is_file()
    manifest = json.loads((project / 'manifest.json').read_text())
    assert manifest['frames'][0]['file'] == 'outputs/project/frames/img_001.webp'
