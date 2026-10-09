"""Short-lived gallery metadata snapshots for summaries and paginated reads.

The original scan_gallery and download whitelist always remain fresh. Only the
two lazy read APIs use this cache; no media file contents are opened here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time

from server_common import gallery_collect_references, scan_gallery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_SECONDS = 3.0
MAX_PAGE_SIZE = 200
FILTERS = frozenset(('all', 'cover', 'frame', 'video', 'merged', 'studio', 'orphan'))
SORTS = frozenset(('newest', 'oldest', 'size', 'name'))

# These requests can change gallery files or their ownership. Long-running
# generation/edit jobs become visible through the TTL once their output exists.
# Polling, previews, downloads and unrelated POST requests do not invalidate it.
MUTATION_PATHS = frozenset((
    '/api/gallery/delete', '/api/projects/delete', '/api/projects/archive',
    '/api/project/rename', '/api/library/item', '/api/library/item/delete',
    '/api/library/items/bulk_delete', '/api/library/delete_item',
    '/api/tasks/delete', '/api/tasks/bulk_delete', '/api/tasks/clear',
    '/api/upload_video', '/api/upload_frame', '/api/cover_roles',
    '/api/delete_slot', '/api/restore_slot', '/api/swap_video_slots',
    '/api/swap_frame_slots', '/api/switch_candidate', '/api/undo_frame_fix',
    '/api/adopt_rejected_fix',
))

_REGISTRY_LOCK = threading.Lock()
_ROOT_LOCKS = {}
_SNAPSHOTS = {}
_GENERATIONS = {}


class GallerySnapshotChanged(Exception):
    def __init__(self, revision):
        super().__init__('snapshot_changed')
        self.revision = revision


class GalleryGroupNotFound(Exception):
    pass


def _root(base_dir):
    return os.path.realpath(os.path.abspath(base_dir or BASE_DIR))


def _root_lock(root):
    with _REGISTRY_LOCK:
        return _ROOT_LOCKS.setdefault(root, threading.RLock())


def invalidate(base_dir=None):
    """Discard cached metadata without waiting for scans or business locks.

    Mutation responses can still hold library/task/project locks. Invalidation
    must never acquire the scan lock: reference collection may be waiting for
    one of those same business locks. The generation also prevents a scan that
    started before this mutation from repopulating the cache afterward.
    """
    root = _root(base_dir)
    with _REGISTRY_LOCK:
        _SNAPSHOTS.pop(root, None)
        _GENERATIONS[root] = _GENERATIONS.get(root, 0) + 1


def _snapshot(base_dir=None, *, refresh=False, scanner=None, collect_references=None):
    root = _root(base_dir)
    # Holding the workspace lock across collection/scanning is single-flight:
    # concurrent index and item requests share the same completed scan.
    with _root_lock(root):
        with _REGISTRY_LOCK:
            previous = _SNAPSHOTS.get(root)
            generation = _GENERATIONS.get(root, 0)
        if not refresh and previous and time.monotonic() < previous['expires_at']:
            return previous
        try:
            refs = (collect_references or gallery_collect_references)()
        except Exception:
            # Same conservative downgrade as the legacy full gallery endpoint:
            # missing reference data never marks an item as an orphan.
            refs = None
        data = (scanner or scan_gallery)(base_dir=root, refs=refs)
        # Metadata and reference ownership determine the version, not cache age.
        # A forced/expired scan with unchanged files retains its revision.
        encoded = json.dumps(data, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode('utf-8')
        current = {
            'data': data,
            'revision': hashlib.sha256(encoded).hexdigest(),
            'expires_at': time.monotonic() + CACHE_SECONDS,
        }
        with _REGISTRY_LOCK:
            if _GENERATIONS.get(root, 0) == generation:
                _SNAPSHOTS[root] = current
        # A concurrent mutation may make this response outdated, but cannot
        # cache it. The next page rebuilds the snapshot and detects revision
        # changes rather than silently extending an outdated action scope.
        return current


def _query(filter, q, sort):
    filter, sort = filter or 'all', sort or 'newest'
    if filter not in FILTERS or sort not in SORTS:
        raise ValueError('画廊筛选或排序参数不正确')
    return filter, str(q or '').strip().lower(), sort


def _orphan(group, item):
    if group.get('kind') == 'project':
        return group.get('orphan') is True
    return item.get('kind') == 'cover' and item.get('in_use') is False


def _filtered(group, filter, q):
    group_hit = q and any(q in str(group.get(key) or '').lower()
                          for key in ('title', 'idea_title'))
    return [item for item in group.get('items', [])
            if (filter == 'all' or (_orphan(group, item) if filter == 'orphan'
                                   else item.get('kind') == filter))
            and (not q or group_hit or any(q in str(item.get(key) or '').lower()
                                          for key in ('name', 'path')))]


def _sort_items(items, sort):
    if sort == 'name':
        return sorted(items, key=lambda item: str(item.get('name') or '').lower())
    field = 'size' if sort == 'size' else 'mtime'
    return sorted(items, key=lambda item: item.get(field, 0), reverse=sort != 'oldest')


def _counts(groups):
    counts = dict.fromkeys(('all', 'cover', 'frame', 'video', 'merged', 'studio', 'orphan'), 0)
    for group in groups:
        for item in group.get('items', []):
            counts['all'] += 1
            if item.get('kind') in counts and item['kind'] not in ('all', 'orphan'):
                counts[item['kind']] += 1
            if _orphan(group, item):
                counts['orphan'] += 1
    return counts


def gallery_index(base_dir=None, *, filter='all', q='', sort='newest', refresh=False,
                  scanner=None, collect_references=None):
    filter, q, sort = _query(filter, q, sort)
    snapshot = _snapshot(base_dir, refresh=refresh, scanner=scanner,
                         collect_references=collect_references)
    data = snapshot['data']
    groups = []
    for group in data.get('groups', []):
        items = _filtered(group, filter, q)
        if not items:
            continue
        summary = {key: value for key, value in group.items()
                   if key not in ('items', 'bytes', 'latest_mtime', 'oldest_mtime')}
        summary.update(total_count=len(group.get('items', [])), count=len(items),
                       filtered_count=len(items), bytes=sum(item.get('size', 0) for item in items),
                       latest_mtime=max(item.get('mtime', 0) for item in items),
                       oldest_mtime=min(item.get('mtime', 0) for item in items))
        groups.append(summary)
    if sort == 'name':
        groups.sort(key=lambda group: str(group.get('title') or '').lower())
    else:
        field = {'oldest': 'oldest_mtime', 'size': 'bytes'}.get(sort, 'latest_mtime')
        groups.sort(key=lambda group: group[field], reverse=sort != 'oldest')
    # Every response is independent of the private cached data. Callers can
    # append rows or mutate selection metadata without corrupting later pages.
    return copy.deepcopy({
        'groups': groups, 'totals': data.get('totals', {}),
        'filter_counts': _counts(data.get('groups', [])), 'revision': snapshot['revision'],
    })


def gallery_items(group, base_dir=None, *, filter='all', q='', sort='newest', offset=0,
                  limit=12, revision='', refresh=False, scanner=None, collect_references=None):
    filter, q, sort = _query(filter, q, sort)
    if not isinstance(group, str) or not group:
        raise ValueError('缺少画廊分组 group')
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError):
        raise ValueError('画廊分页参数不正确') from None
    if offset < 0 or limit < 1:
        raise ValueError('画廊分页参数不正确')
    limit = min(limit, MAX_PAGE_SIZE)
    snapshot = _snapshot(base_dir, refresh=refresh, scanner=scanner,
                         collect_references=collect_references)
    if revision and revision != snapshot['revision']:
        raise GallerySnapshotChanged(snapshot['revision'])
    selected = next((g for g in snapshot['data'].get('groups', []) if g['key'] == group), None)
    if selected is None:
        raise GalleryGroupNotFound(group)
    items = _sort_items(_filtered(selected, filter, q), sort)
    return copy.deepcopy({
        'items': items[offset:offset + limit], 'group_key': group,
        'filtered_count': len(items), 'total_count': len(selected.get('items', [])),
        'offset': offset, 'has_more': offset + limit < len(items),
        'revision': snapshot['revision'],
    })
