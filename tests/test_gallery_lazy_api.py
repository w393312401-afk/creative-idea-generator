"""Lazy gallery reads preserve whole-scope counts while paging shared snapshots."""
import copy
from email.message import Message
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest

import gallery_index as lazy
from server_common import gallery_collect_references, scan_gallery


def write(base, relative, *, mtime=100, size=1):
    path = base / 'outputs' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def media(tmp_path, monkeypatch):
    write(tmp_path, 'covers/unused.webp', mtime=100)
    write(tmp_path, 'covers/used.webp', mtime=101)
    write(tmp_path, 'image-station/studio.webp', mtime=102)
    for index in range(27):
        write(tmp_path, f'project/frames/frame_{index:02d}.webp',
              mtime=200 + index, size=index + 1)
    write(tmp_path, 'project/videos/clip.mp4', mtime=300, size=10)
    write(tmp_path, 'project/final.mp4', mtime=301, size=20)
    write(tmp_path, 'project/cover_one.webp', mtime=302, size=3)
    write(tmp_path, 'orphan/frames/old.webp', mtime=50)
    refs = gallery_collect_references(library_items=[{
        'id': 'idea', 'title': 'Renamed Workbench', 'project_key': 'project',
        'covers': ['outputs/covers/used.webp', 'outputs/project/cover_one.webp'],
    }], tasks=[])
    monkeypatch.setattr(lazy, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(lazy, 'gallery_collect_references', lambda: refs)
    lazy.invalidate()
    yield tmp_path, refs
    lazy.invalidate()


def flattened(data):
    return [item for group in data['groups'] for item in group['items']]


def test_index_has_no_file_lists_and_counts_match_full_gallery(media):
    base, refs = media
    full = scan_gallery(base_dir=str(base), refs=refs)
    index = lazy.gallery_index()
    assert index['totals'] == full['totals']
    assert index['filter_counts'] == {
        'all': 34, 'cover': 3, 'frame': 28, 'video': 1,
        'merged': 1, 'studio': 1, 'orphan': 2,
    }
    assert all('items' not in group for group in index['groups'])
    assert {group['key']: group['total_count'] for group in index['groups']} == {
        'project': 30, 'orphan': 1, 'covers': 2, 'image-station': 1,
    }
    project = next(group for group in index['groups'] if group['key'] == 'project')
    assert project['count'] == project['filtered_count'] == 30
    assert project['idea_id'] == 'idea'
    assert project['idea_title'] == 'Renamed Workbench'
    assert project['orphan'] is False
    assert project['latest_mtime'] == 302
    assert project['oldest_mtime'] == 200


def test_page_sequence_has_no_duplicates_or_omissions_and_default_is_twelve(media):
    revision = lazy.gallery_index()['revision']
    first = lazy.gallery_items('project', revision=revision)
    assert len(first['items']) == 12
    assert first['group_key'] == 'project'
    assert first['offset'] == 0 and first['has_more'] is True
    assert first['total_count'] == first['filtered_count'] == 30
    pages = [lazy.gallery_items('project', offset=offset, revision=revision)
             for offset in (0, 12, 24)]
    paths = [item['path'] for page in pages for item in page['items']]
    assert len(paths) == len(set(paths)) == 30
    assert pages[-1]['has_more'] is False
    assert lazy.gallery_items('project', offset=30)['items'] == []


@pytest.mark.parametrize('filter,query,expected', [
    ('frame', '', 27), ('all', 'RENAMED WORKBENCH', 30),
    ('video', 'project', 1), ('merged', 'final', 1),
    ('cover', 'outputs/project', 1), ('all', 'frame_01', 1),
    ('orphan', '', 0), ('all', 'missing', 0),
])
def test_filter_and_search_match_whole_group_scope(media, filter, query, expected):
    index = lazy.gallery_index(filter=filter, q=query)
    page = lazy.gallery_items('project', filter=filter, q=query, limit=200)
    assert page['total_count'] == 30
    assert page['filtered_count'] == len(page['items']) == expected
    project = next((group for group in index['groups'] if group['key'] == 'project'), None)
    if expected:
        assert project['filtered_count'] == expected
        assert project['bytes'] == sum(item['size'] for item in page['items'])
        assert project['latest_mtime'] == max(item['mtime'] for item in page['items'])
        assert project['oldest_mtime'] == min(item['mtime'] for item in page['items'])
    else:
        assert project is None
    # Category chips and the global storage summary ignore both filter and q.
    assert index['filter_counts'] == lazy.gallery_index()['filter_counts']
    assert index['totals'] == lazy.gallery_index()['totals']


def test_orphan_filter_uses_group_ownership_and_explicit_cover_in_use(media):
    index = lazy.gallery_index(filter='orphan')
    assert {group['key']: group['filtered_count'] for group in index['groups']} == {
        'orphan': 1, 'covers': 1,
    }
    assert lazy.gallery_items('covers', filter='orphan')['items'][0]['name'] == 'unused.webp'


@pytest.mark.parametrize('sort,field,reverse', [
    ('newest', 'mtime', True), ('oldest', 'mtime', False),
    ('size', 'size', True), ('name', 'name', False),
])
def test_all_sort_modes_apply_before_pagination(media, sort, field, reverse):
    all_items = lazy.gallery_items('project', sort=sort, limit=200)['items']
    assert [item[field] for item in all_items] == sorted(
        [item[field] for item in all_items], reverse=reverse)
    assert lazy.gallery_items('project', sort=sort, offset=12)['items'] == all_items[12:24]
    index = lazy.gallery_index(sort=sort)
    group_field = {'newest': 'latest_mtime', 'oldest': 'oldest_mtime',
                   'size': 'bytes', 'name': 'title'}[sort]
    values = [group[group_field] for group in index['groups']]
    assert values == sorted(values, reverse=reverse)


def test_cache_ttl_refresh_and_explicit_invalidation(media, monkeypatch):
    base, refs = media
    calls = []
    clock = [10.0]
    monkeypatch.setattr(lazy.time, 'monotonic', lambda: clock[0])

    def scan(**kwargs):
        calls.append(kwargs['base_dir'])
        return scan_gallery(**kwargs)

    monkeypatch.setattr(lazy, 'scan_gallery', scan)
    first = lazy.gallery_index()
    lazy.gallery_items('project')
    lazy.gallery_index(filter='frame')
    assert calls == [str(base)]
    assert lazy.gallery_index(refresh=True)['revision'] == first['revision']
    assert len(calls) == 2
    clock[0] += 3.01
    assert lazy.gallery_index()['revision'] == first['revision']
    assert len(calls) == 3
    lazy.invalidate()
    lazy.gallery_items('project')
    assert len(calls) == 4


def test_content_revision_changes_for_media_and_ownership(media, monkeypatch):
    base, refs = media
    original = lazy.gallery_index()['revision']
    write(base, 'project/frames/new.webp', mtime=999)
    assert lazy.gallery_index()['revision'] == original  # within TTL
    updated = lazy.gallery_index(refresh=True)['revision']
    assert updated != original
    with pytest.raises(lazy.GallerySnapshotChanged) as error:
        lazy.gallery_items('project', revision=original)
    assert error.value.revision == updated
    modified_refs = copy.deepcopy(refs)
    modified_refs['project_owners']['project']['idea_title'] = 'Another title'
    monkeypatch.setattr(lazy, 'gallery_collect_references', lambda: modified_refs)
    assert lazy.gallery_index(refresh=True)['revision'] != updated


def test_cached_metadata_cannot_be_corrupted_by_response_mutation(media):
    index = lazy.gallery_index()
    revision = index['revision']
    index['totals']['images'] = -1
    index['groups'][0]['title'] = 'modified'
    page = lazy.gallery_items('project')
    page['items'][0]['path'] = 'modified'
    next_index = lazy.gallery_index()
    assert next_index['revision'] == revision
    assert next_index['totals']['images'] > 0
    assert next_index['groups'][0]['title'] != 'modified'
    assert lazy.gallery_items('project')['items'][0]['path'] != 'modified'


def test_workspace_snapshots_are_isolated(media, tmp_path_factory):
    other = tmp_path_factory.mktemp('other-gallery')
    write(other, 'other-project/frames/unique.webp')
    index = lazy.gallery_index(base_dir=other)
    assert [group['key'] for group in index['groups']] == ['other-project']
    assert index['totals']['images'] == 1
    assert lazy.gallery_index()['totals']['images'] == 32
    lazy.invalidate(other)


def test_parallel_reads_share_one_in_flight_scan(media, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def scan(**kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return scan_gallery(**kwargs)

    monkeypatch.setattr(lazy, 'scan_gallery', scan)
    with ThreadPoolExecutor(max_workers=3) as pool:
        index = pool.submit(lazy.gallery_index)
        assert entered.wait(5)
        pages = [pool.submit(lazy.gallery_items, 'project') for _ in range(2)]
        release.set()
        revisions = [index.result()['revision']] + [page.result()['revision'] for page in pages]
    assert len(calls) == 1
    assert len(set(revisions)) == 1


def test_invalidation_does_not_wait_for_scan_or_publish_its_outdated_result(media, monkeypatch):
    base, _ = media
    entered, release = threading.Event(), threading.Event()
    calls = []

    def scan(**kwargs):
        calls.append(1)
        data = scan_gallery(**kwargs)
        if len(calls) == 1:
            # Capture the old list before the write; hold the scanner afterward
            # so invalidation has to complete while single-flight is locked.
            entered.set()
            assert release.wait(5)
        return data

    monkeypatch.setattr(lazy, 'scan_gallery', scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lazy.gallery_index)
        assert entered.wait(5)
        write(base, 'project/frames/added.webp', mtime=999)
        invalidation = pool.submit(lazy.invalidate)
        try:
            assert invalidation.result(timeout=0.5) is None
            assert not first.done()
        finally:
            # The old implementation must fail promptly without leaving its
            # blocked scan/invalidation threads alive after the assertion.
            release.set()
        outdated = first.result()
    fresh = lazy.gallery_index()
    assert len(calls) == 2  # invalidated scan never repopulated the short cache
    assert fresh['filter_counts']['all'] == outdated['filter_counts']['all'] + 1
    assert fresh['revision'] != outdated['revision']
    assert lazy.gallery_index()['revision'] == fresh['revision']
    assert len(calls) == 2
    with pytest.raises(lazy.GallerySnapshotChanged):
        lazy.gallery_items('project', revision=outdated['revision'])


def test_invalidation_is_independent_of_reference_collection_business_lock(media, monkeypatch):
    _, refs = media
    business_lock = threading.Lock()
    collecting = threading.Event()

    def collect():
        collecting.set()
        with business_lock:
            return refs

    monkeypatch.setattr(lazy, 'gallery_collect_references', collect)
    # Model a successful save still holding a business lock while its response
    # invalidates, with gallery reference collection waiting for that lock.
    business_lock.acquire()
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(lazy.gallery_index)
        try:
            assert collecting.wait(5)
            invalidation = pool.submit(lazy.invalidate)
            assert invalidation.result(timeout=0.5) is None
            assert not scan.done()
        finally:
            business_lock.release()
        scan.result()
    root = lazy._root(None)
    with lazy._REGISTRY_LOCK:
        assert root not in lazy._SNAPSHOTS


def test_reference_failure_downgrades_without_false_orphans(media, monkeypatch):
    def broken():
        raise RuntimeError('reference store unavailable')

    monkeypatch.setattr(lazy, 'gallery_collect_references', broken)
    index = lazy.gallery_index()
    assert index['filter_counts']['all'] == 34
    assert index['filter_counts']['orphan'] == 0
    assert all('orphan' not in group for group in index['groups'])


@pytest.mark.parametrize('options', [
    {'offset': -1}, {'limit': 0}, {'offset': 'x'}, {'limit': 'x'},
    {'sort': 'unknown'}, {'filter': 'unknown'},
])
def test_invalid_paging_and_query_are_rejected(media, options):
    with pytest.raises(ValueError):
        lazy.gallery_items('project', **options)


def test_page_size_is_capped_and_unknown_group_is_distinct(media):
    base, _ = media
    for index in range(220):
        write(base, f'large/frames/{index:03d}.webp')
    page = lazy.gallery_items('large', limit=1000)
    assert len(page['items']) == lazy.MAX_PAGE_SIZE
    assert page['has_more'] is True
    with pytest.raises(lazy.GalleryGroupNotFound):
        lazy.gallery_items('missing')


def request(path, *, gate=True, method='GET', body=None):
    import server
    handler = object.__new__(server.SparkRequestHandler)
    handler.path, handler.command = path, method
    handler.headers = Message()
    encoded = json.dumps(body).encode() if body is not None else b''
    handler.headers['Content-Length'] = str(len(encoded))
    handler.headers['Content-Type'] = 'application/json'
    handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
    replies = []
    handler._gate = lambda **kwargs: gate
    handler._send_json = lambda payload, status=200: replies.append((status, payload))
    getattr(handler, 'do_' + method)()
    return replies


def test_read_api_gate_index_page_errors_and_legacy_fresh_scan(media, monkeypatch):
    import server
    _, refs = media
    scans = []

    def scan(**kwargs):
        scans.append(1)
        # The legacy full API omits base_dir; isolate it to these fixture files.
        kwargs.setdefault('base_dir', lazy.BASE_DIR)
        return scan_gallery(**kwargs)

    monkeypatch.setattr(server, 'scan_gallery', scan)
    monkeypatch.setattr(server, 'gallery_collect_references', lambda: refs)
    assert request('/api/gallery/index', gate=False) == []
    assert request('/api/gallery/items?group=project', gate=False) == []
    assert not scans
    status, index = request('/api/gallery/index')[0]
    assert status == 200 and all('items' not in group for group in index['groups'])
    status, page = request('/api/gallery/items?' + urlencode({
        'group': 'project', 'revision': index['revision'],
    }))[0]
    assert status == 200 and len(page['items']) == 12 and len(scans) == 1
    assert request('/api/gallery/items?group=missing')[0][0] == 404
    assert request('/api/gallery/items?group=project&offset=x')[0][0] == 400
    assert request('/api/gallery/items')[0][0] == 400
    status, changed = request('/api/gallery/items?group=project&revision=old')[0]
    assert status == 409 and changed == {'error': 'snapshot_changed', 'revision': index['revision']}
    request('/api/gallery/index?refresh=1')
    assert len(scans) == 2
    assert 'items' in request('/api/gallery')[0][1]['groups'][0]
    request('/api/gallery')
    assert len(scans) == 4


def test_delete_api_discards_cached_file_list_immediately(media, monkeypatch):
    import server
    from server_common import gallery_delete_files
    base, refs = media
    monkeypatch.setattr(server, 'gallery_collect_references', lambda: refs)
    monkeypatch.setattr(server, 'gallery_delete_files',
                        lambda paths, **kwargs: gallery_delete_files(paths, base_dir=str(base), **kwargs))
    monkeypatch.setattr(server, 'sync_project_manifest_with_disk', lambda *args, **kwargs: None)
    before = request('/api/gallery/index')[0][1]
    relative = 'outputs/project/frames/frame_00.webp'
    assert request('/api/gallery/delete', method='POST', body={
        'paths': [relative], 'remove_empty_projects': False,
    })[0][1]['deleted'] == [relative]
    after = request('/api/gallery/index')[0][1]
    assert before['revision'] != after['revision']
    assert after['filter_counts']['all'] == before['filter_counts']['all'] - 1
    assert relative not in {item['path'] for item in
                            request('/api/gallery/items?group=project&limit=200')[0][1]['items']}


@pytest.mark.parametrize('path,body,status,payload,invalidates', [
    ('/api/gallery/delete', {}, 200, {'status': 'ok'}, True),
    ('/api/projects/delete', {}, 200, {'status': 'ok'}, True),
    ('/api/project/rename', {}, 200, {'status': 'ok'}, True),
    ('/api/library/item', {}, 200, {'status': 'success'}, True),
    ('/api/library/item/delete', {}, 200, {'status': 'success'}, True),
    ('/api/library/items/bulk_delete', {}, 200, {'status': 'success'}, True),
    ('/api/projects/archive', {'preview': False}, 200, {'status': 'ok'}, True),
    ('/api/projects/archive', {'preview': True}, 200, {'status': 'ok'}, False),
    ('/api/projects/archive', {}, 200, {'status': 'ok'}, False),
    ('/api/gallery/delete', {}, 400, {'status': 'error'}, False),
    ('/api/gallery/delete', {}, 200, {'status': 'error'}, False),
    ('/api/gallery/delete', {}, 200, {'error': 'failed'}, False),
    ('/api/reveal_file', {}, 200, {'status': 'ok'}, False),
    ('/api/gallery/download-zip', {}, 200, {'status': 'ok'}, False),
    ('/api/ping', {}, 200, {'status': 'ok'}, False),
])
def test_unified_mutation_hook_only_invalidates_successful_relevant_requests(
        media, monkeypatch, path, body, status, payload, invalidates):
    import server
    calls = []
    monkeypatch.setattr(lazy, 'invalidate', lambda *args, **kwargs: calls.append(1))
    monkeypatch.setattr(server.SparkRequestHandler, '_do_POST_impl',
                        lambda self: self._send_json(payload, status=status))
    monkeypatch.setattr(server.SparkRequestHandler, '_read_json_body', lambda self: body)
    assert request(path, method='POST', body=body) == [(status, payload)]
    assert bool(calls) is invalidates
