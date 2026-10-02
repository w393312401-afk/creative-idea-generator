"""Archived merged/data-only projects retain correct counts and gallery labels."""
import json
from urllib.parse import quote

import pytest

import server_common as store


def _archive(tmp_path, videos, *, legacy=False):
    key = 'run_archive_media__归档项目'
    directory = tmp_path / 'outputs' / store._safe_project_name(key)
    directory.mkdir(parents=True)
    entries = []
    for relative, kind in videos:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'finished video')
        entries.append({'url': f'/outputs/{directory.name}/{relative}',
                        'name': path.name, 'kind': kind})
    record = {'status': 'archived', 'project_key': key, 'title': '归档项目',
              'archived_at': 1, 'refined_videos': [e for e in entries if e['kind'] == 'refined_video'],
              'retained_files': entries}
    if not legacy:
        record['final_videos'] = entries
        record['video_retention'] = 'refined' if record['refined_videos'] else 'merged' if entries else 'none'
    (directory / '.project-archive.json').write_text(json.dumps(record), encoding='utf-8')
    return directory, record


@pytest.mark.parametrize('videos,legacy', [
    ([('current.mp4', 'merged_video')], False),
    ([], False),
    ([('codex_edits/job/work/edited.mp4', 'refined_video')], True),
])
def test_archive_index_counts_final_videos_and_data_only(tmp_path, videos, legacy):
    _, record = _archive(tmp_path, videos, legacy=legacy)
    row, = store.build_projects_index(tasks=[], library_items=[], ledger_rows=[],
                                     base_dir=str(tmp_path), with_assets=False)
    assert row['state'] == 'archived'
    assert row['video_count'] == len(videos)
    assert row['progress']['merged_available'] == bool(videos)
    if not legacy:
        assert row['archive']['final_videos'] == record['final_videos']
        assert row['archive']['video_retention'] == record['video_retention']


@pytest.mark.parametrize('relative', ['current.mp4', 'videos/current.mp4'])
def test_archive_gallery_merged_video_has_no_edit_badge_or_duplicates(tmp_path, relative):
    _archive(tmp_path, [(relative, 'merged_video')])
    gallery = store.scan_gallery(base_dir=str(tmp_path))
    group, = gallery['groups']
    video, = group['items']
    assert video['kind'] == 'merged'
    assert not video['is_edited']
    assert gallery['totals']['videos'] == 1


def test_legacy_refined_archive_remains_in_gallery(tmp_path):
    _archive(tmp_path, [('codex_edits/job/work/edited.mp4', 'refined_video')], legacy=True)
    gallery = store.scan_gallery(base_dir=str(tmp_path))
    video, = gallery['groups'][0]['items']
    assert video['kind'] == 'merged'
    assert video['is_edited']


@pytest.mark.parametrize('with_assets', [False, True])
def test_archive_index_uses_only_a_published_existing_thumbnail(tmp_path, with_assets):
    directory, record = _archive(tmp_path, [('current.mp4', 'merged_video')])
    thumbnail = directory / 'archive_cover.jpg'
    thumbnail.write_bytes(b'archive thumbnail')
    cover_url = f'/outputs/{quote(directory.name)}/{thumbnail.name}'
    record['cover_url'] = cover_url
    record['retained_files'].append({'url': cover_url, 'name': thumbnail.name, 'kind': 'cover'})
    (directory / '.project-archive.json').write_text(json.dumps(record), encoding='utf-8')
    rows = store.build_projects_index(tasks=[], library_items=[{
        'id': 'archived', 'project_key': record['project_key'], 'title': record['title'],
        'archived': True, 'activeCoverUrl': '/outputs/deleted-cover.webp',
        'archive': {'cover_url': '/outputs/stale-cover.jpg'}}], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=with_assets)
    row, = rows
    assert row['cover'] == row['archive']['cover_url'] == cover_url
    assert row['image_count'] == 0
    assert store.scan_gallery(base_dir=str(tmp_path))['totals']['images'] == 0
    thumbnail.unlink()
    row, = store.build_projects_index(tasks=[], library_items=[], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=with_assets)
    assert row['cover'] is None


@pytest.mark.parametrize('url', [
    '/outputs/another-project/cover.jpg', '/outputs/../cover.jpg',
    'https://example.com/cover.jpg', 'javascript:alert(1)',
])
def test_archive_index_rejects_thumbnail_outside_its_project(tmp_path, url):
    directory, record = _archive(tmp_path, [])
    outside = tmp_path / 'outputs' / 'another-project' / 'cover.jpg'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'other project cover')
    record['cover_url'] = url
    record['retained_files'].append({'url': url, 'name': 'cover.jpg', 'kind': 'cover'})
    (directory / '.project-archive.json').write_text(json.dumps(record), encoding='utf-8')
    row, = store.build_projects_index(tasks=[], library_items=[], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=False)
    assert row['cover'] is None
