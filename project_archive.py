"""Project-scoped retention: available final videos, beat evidence and current prompts.

Never accept deletion paths from a browser. Prepare durable exports before unlinking
anything; the hidden receipt makes interrupted cleanup retryable without edit state.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import server_common as store

MUTATION_LOCK = threading.RLock()
RECEIPT = '.project-archive.json'
PROMPTS_TEXT = '全套提示词.txt'
PROMPTS_DATA = '提示词数据.json'
BEAT_NAMES = frozenset(('timelapse_beats.json', 'beat_package.json', 'beat_ladder.json',
                        'beats.json', '节拍阶梯.md', '节拍数据.json', '反推节拍.json'))
ARCHIVE_COVER = 'archive_cover.jpg'
PROJECT_MUTATIONS = frozenset((
    '/api/projects/archive', '/api/projects/delete', '/api/tasks/clear',
    '/api/gallery/delete', '/api/tasks/delete', '/api/tasks/bulk_delete',
    '/api/library/item', '/api/library/item/delete', '/api/library/items/bulk_delete',
    '/api/library/delete_item', '/api/compose', '/api/auto_run',
    '/api/generate_frames', '/api/generate_frames_selection', '/api/render_staged',
    '/api/render_anchor', '/api/generate_videos', '/api/generate_video_chain',
    '/api/stepped/start', '/api/stepped/advance', '/api/merge_videos', '/api/generate_cover',
    '/api/switch_candidate', '/api/fix_frame_issue', '/api/undo_frame_fix',
    '/api/adopt_rejected_fix', '/api/flag_frame_issue', '/api/sequence_review',
    '/api/cover_roles', '/api/upload_video', '/api/upload_frame', '/api/swap_video_slots',
    '/api/swap_frame_slots', '/api/edit_prompts', '/api/delete_slot', '/api/restore_slot',
    '/api/project/rename',
))
ARCHIVE_ALLOWED = frozenset(('/api/projects/archive', '/api/projects/delete',
    '/api/gallery/delete', '/api/tasks/delete', '/api/tasks/bulk_delete',
    '/api/tasks/clear', '/api/library/item/delete', '/api/library/items/bulk_delete',
    '/api/library/delete_item'))


def _json(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def receipt(project_dir):
    path = Path(project_dir) / RECEIPT
    if path.is_symlink():
        return None
    data = _json(path)
    return data if isinstance(data, dict) else None


def _root(base_dir):
    return Path(os.path.abspath(os.path.join(base_dir, store.OUTPUT_ROOT)))


def _safe(path, root):
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError('归档文件路径无效')
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('归档文件必须位于该项目目录中') from None
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError('项目含符号链接，已停止归档以保护原文件')
        current = current.parent
    return path


def _files(directory):
    """No followed links, including links to missing files."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('项目含符号链接，已停止归档以保护原文件')
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError('项目含符号链接，已停止归档以保护原文件')
        for name in files:
            yield Path(parent) / name


def _url(path, root):
    return '/outputs/' + quote(Path(path).relative_to(root).as_posix(), safe='/')


def _entry(path, root, kind):
    return {'url': _url(path, root), 'name': Path(path).name, 'kind': kind}


def _url_path(url, root):
    if not isinstance(url, str) or not url.startswith('/outputs/'):
        raise ValueError('归档记录中的文件路径无效')
    return _safe(root / unquote(url[len('/outputs/'):]), root)


def archived_request(payload, base_dir=None):
    """Reject stale tabs trying to recreate media in an archived namespace."""
    if not isinstance(payload, dict):
        return False
    base_dir = base_dir or os.getcwd()
    source = payload.get('item') if isinstance(payload.get('item'), dict) else payload
    dims = source.get('dimensions') if isinstance(source.get('dimensions'), dict) else {}
    keys = [source.get('project_key'), source.get('title'), dims.get('project_key')]
    for key in keys:
        if not isinstance(key, str) or not key.strip():
            continue
        directory = store._proj_output_dir(key, key, base_dir)
        record = receipt(directory) if directory else None
        if record and record.get('status') in ('prepared', 'archived'):
            return True
    return bool(source.get('archived'))


def _edit_outputs(project, root):
    import codex_video_editor as editor
    outputs = []
    for state in project.glob('codex_edits/*/.state.json'):
        _safe(state, root)
        job = _json(state)
        if not isinstance(job, dict):
            raise ValueError('精剪任务记录无法读取，请先检查该任务')
        if (job.get('status') in editor.ACTIVE or editor._worker_alive(job)
                or editor._guardian_alive(job)):
            raise ValueError('项目仍有精剪任务在运行，请等待完成后归档')
        if job.get('status') != 'completed':
            continue
        if job.get('id') != state.parent.name or not re.fullmatch(r'[a-f0-9]{32}', state.parent.name):
            raise ValueError('精剪任务记录与目录不一致')
        output = job.get('output') or {}
        if not isinstance(output, dict):
            raise ValueError('精剪成片记录无效，请先检查该任务')
        raw = output.get('file')
        if not raw and output.get('url'):
            raw = _url_path(output['url'], root)
        if not raw:
            raise ValueError('已完成的精剪任务缺少成片路径')
        path = _safe(raw, state.parent / 'work')
        if path.suffix.lower() != '.mp4' or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('已完成的精剪成片缺失，已停止归档')
        outputs.append(path)
    return list(dict.fromkeys(outputs))


def _receipt_videos(project, root, pending):
    """Retry the exact published video kinds, including legacy refined receipts."""
    if not pending:
        return {}
    entries = pending.get('final_videos', pending.get('refined_videos', []))
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError('归档成片记录无效，已停止清理')
    videos = {}
    for entry in entries:
        kind = entry.get('kind', 'refined_video')
        if kind not in ('refined_video', 'merged_video'):
            raise ValueError('归档成片种类无效，已停止清理')
        path = _safe(_url_path(entry.get('url'), root), project)
        if path.suffix.lower() != '.mp4' or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('归档成片缺失，已停止清理')
        videos[path] = kind
    return videos


def archive_cover_url(record, project, root):
    """Return only an existing, project-local thumbnail published in the receipt."""
    if not isinstance(record, dict):
        return None
    retained = record.get('retained_files')
    covers = [entry.get('url') for entry in (retained if isinstance(retained, list) else [])
              if isinstance(entry, dict) and entry.get('kind') == 'cover']
    preferred = record.get('cover_url')
    if preferred in covers:
        covers.remove(preferred)
        covers.insert(0, preferred)
    for url in covers:
        try:
            path = _safe(_url_path(url, root), Path(os.path.abspath(project)))
            if (store._gallery_media_type(path.name) == 'image'
                    and path.is_file() and path.stat().st_size > 0):
                return _url(path, root)
        except (OSError, ValueError, TypeError):
            continue
    return None


def _cover_source_path(raw, project, root):
    """Selected images may be local to this project or in the legacy cover pool."""
    try:
        path = _merged_path(raw, project, root)
    except ValueError:
        # Historical library covers lived in outputs/covers, outside the project.
        try:
            if not isinstance(raw, str):
                return None
            url = urlsplit(raw).path
            path = (_url_path(url, root) if url.startswith('/outputs/')
                    else _safe(root / url[len('outputs/'):] if url.startswith('outputs/')
                               else Path(url), root))
            path = _safe(path, root / store.LEGACY_COVERS_DIRNAME)
        except (ValueError, TypeError):
            return None
    if store._gallery_media_type(path.name) != 'image':
        return None
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _thumbnail_bytes(path, *, video=False):
    """Generate a small JPEG in memory, so even archive previews are read-only."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        if video:
            result = subprocess.run([store.resolve_binary('ffmpeg'), '-hide_banner',
                '-loglevel', 'error', '-nostdin', '-i', str(path), '-frames:v', '1',
                '-vf', 'scale=640:640:force_original_aspect_ratio=decrease',
                '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                **store.get_subprocess_window_flags())
            if result.returncode or not result.stdout:
                return None
            source = io.BytesIO(result.stdout)
        else:
            source = path
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((640, 640), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=82, optimize=True)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError, subprocess.SubprocessError):
        return None


def _new_archive_cover(project, root, sources, row, final_videos):
    manifest = _json(project / 'manifest.json')
    manifest = manifest if isinstance(manifest, dict) else {}
    roles = manifest.get('cover_roles') or {}
    selected = [roles.get('project') if isinstance(roles, dict) else None,
                manifest.get('active_cover'), row.get('cover')]
    selected.extend(store.item_project_cover(source) for source in sources)
    disk_covers = sorted((path for path in project.iterdir()
                          if store._is_cover_filename(path.name) and path.is_file()),
                         key=lambda path: path.stat().st_mtime, reverse=True)
    selected.extend(str(path) for path in disk_covers)
    seen = set()
    for raw in selected:
        path = _cover_source_path(raw, project, root)
        if path is None or path in seen:
            continue
        seen.add(path)
        data = _thumbnail_bytes(path)
        if data:
            return data
    for path in final_videos:
        data = _thumbnail_bytes(path, video=True)
        if data:
            return data
    return None


def _merged_path(raw, project, root):
    """Resolve an explicit merged-video reference, never discover by filename."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('合成成片路径无效，已停止归档')
    raw = raw.strip()
    if '://' in raw:
        raise ValueError('合成成片必须位于该项目目录中')
    if raw.startswith('/outputs/'):
        path = _url_path(urlsplit(raw).path, root)
    elif raw.startswith('outputs/'):
        path = root / raw[len('outputs/'):]
    elif os.path.isabs(raw):
        path = Path(raw)
    elif raw.split('/', 1)[0] == project.name:
        path = root / raw
    else:
        path = project / raw
    return _safe(path, project)


def _merged_output(project, root, sources):
    """The disk manifest wins; saved project references are legacy fallbacks."""
    manifest = _json(project / 'manifest.json')
    references = []
    if isinstance(manifest, dict) and manifest.get('merged_video'):
        references.append(manifest['merged_video'])
    for source in sources:
        frame_run = source.get('frameRun')
        if isinstance(frame_run, dict) and frame_run.get('merged_video'):
            references.append(frame_run['merged_video'])
        if source.get('merged_video'):
            references.append(source['merged_video'])
    for reference in references:
        if isinstance(reference, str):
            paths = [reference]
        elif isinstance(reference, dict):
            paths = [reference[field] for field in ('file', 'url')
                     if reference.get(field) is not None and reference.get(field) != '']
        else:
            raise ValueError('合成成片记录无效，已停止归档')
        for raw in paths:
            path = _merged_path(raw, project, root)
            if path.suffix.lower() == '.mp4' and path.is_file() and path.stat().st_size > 0:
                return path
    return None


def _sources(row, library, tasks, pending=None):
    keys = {key for key in (row['project_key'], row.get('media_key')) if key}
    lib_id = (row.get('library') or {}).get('id')
    library_ids = set((pending or {}).get('library_ids') or [])
    if lib_id is not None:
        library_ids.add(lib_id)
    items = [item for item in library if item.get('project_key') in keys or item.get('id') in library_ids]
    ids = {(row.get('task') or {}).get('id')}
    ids.update((pending or {}).get('task_ids') or [])
    ids.update(job.get('id') for job in row.get('sub_jobs', []))
    matched = []
    for task in tasks:
        dims, result = task.get('dimensions') or {}, task.get('result') or {}
        key = result.get('project_key') or dims.get('project_key')
        if key in keys or (task.get('id') in ids and (not key or key in keys)):
            matched.append(task)
    sources = list(items)
    sources.extend(task.get('result') for task in sorted(matched,
        key=lambda task: task.get('last_active') or 0, reverse=True)
        if isinstance(task.get('result'), dict))
    return items, matched, sources


def _prepare(row, rows, library, tasks, base_dir):
    root = _root(base_dir)
    raw = store._proj_output_dir(row.get('media_key') or row['project_key'], row['title'], base_dir)
    if not raw:
        raise ValueError('找不到该项目的素材目录')
    project = _safe(raw, root)
    if project == root or project.parent != root:
        raise ValueError('归档目标不是独立项目目录')
    for other in rows:
        if other['project_key'] == row['project_key']:
            continue
        directory = store._proj_output_dir(other.get('media_key') or other['project_key'], other['title'], base_dir)
        if directory and Path(os.path.abspath(directory)) == project:
            raise ValueError('该素材目录被多个项目共用，已停止归档')
    files = list(_files(project))
    pending = receipt(project)
    if pending:
        for field in ('refined_videos', 'retained_files'):
            entries = pending.get(field)
            if not isinstance(entries, list) or any(not isinstance(entry, dict)
                    or not isinstance(entry.get('url'), str) for entry in entries):
                raise ValueError('归档记录无效，已停止清理')
        if 'final_videos' in pending:
            entries = pending['final_videos']
            if not isinstance(entries, list) or any(not isinstance(entry, dict)
                    or not isinstance(entry.get('url'), str)
                    or entry.get('kind') not in ('refined_video', 'merged_video') for entry in entries):
                raise ValueError('归档成片记录无效，已停止清理')
        for field in ('task_ids', 'library_ids'):
            ids = pending.get(field, [])
            if not isinstance(ids, list) or any(not isinstance(value, (str, int, float)) for value in ids):
                raise ValueError('归档关联记录无效，已停止清理')
    items, matched, sources = _sources(row, library, tasks, pending)
    if row.get('state') == 'running' or any(task.get('status') in ('running', 'queued') for task in matched):
        raise ValueError('项目仍有生成任务在运行，请等待完成后归档')
    with store._ACTIVE_FRAME_RUNS_LOCK:
        if store._ACTIVE_FRAME_RUNS.get(os.path.normcase(str(project))):
            raise ValueError('图片生成任务仍在收尾，请等待退出后归档')
    matched_ids = {task['id'] for task in matched}
    project_keys = {key for key in (row['project_key'], row.get('media_key')) if key}
    for thread in threading.enumerate():
        args = getattr(thread, '_args', ())
        worker_keys = {key for value in args[1:3] if isinstance(value, dict)
                       for key in (value.get('_project_key'), value.get('project_key')) if isinstance(key, str)}
        if (args and isinstance(args[0], str) and args[0] in matched_ids) or project_keys.intersection(worker_keys):
            raise ValueError('生成任务仍在收尾，请等待退出后归档')
    refined = _edit_outputs(project, root)
    final_videos = _receipt_videos(project, root, pending)
    if refined:
        final_videos.update({path: 'refined_video' for path in refined})
    elif not pending:
        merged = _merged_output(project, root, sources)
        if merged:
            final_videos[merged] = 'merged_video'
    video_retention = ('refined' if 'refined_video' in final_videos.values() else
                       'merged' if final_videos else 'none')
    prompt_source = next((source for source in sources if isinstance(source.get('prompt_block'), str)
                          and source['prompt_block'].strip()), None)
    if prompt_source is None:
        # Pending receipts can finish after the full library/tasks were already compacted.
        exported = _json(project / PROMPTS_DATA)
        if pending and isinstance(exported, dict) and exported.get('prompt_block'):
            prompt_source = exported
        else:
            manifest = _json(project / 'manifest.json') or {}
            prompt_source = manifest if manifest.get('prompt_block') else None
    if prompt_source is None:
        raise ValueError('未找到全套提示词，已停止归档以避免丢失内容')
    prompt_data = {key: prompt_source[key] for key in
        ('prompt_block', 'prompt_slots', 'global_image_prompt', 'global_video_prompt', 'control_prompt')
        if key in prompt_source}
    # Export current text exactly, without a lossy reformatter.
    exports = {project / PROMPTS_TEXT: prompt_data['prompt_block'].encode('utf-8'),
               project / PROMPTS_DATA: json.dumps(prompt_data, ensure_ascii=False, indent=2).encode('utf-8')}
    retained = dict(final_videos)
    retained.update({path: 'prompts' for path in exports})
    for path in files:
        if path.name in BEAT_NAMES and 'codex_edits' not in path.relative_to(project).parts:
            retained[path] = 'beats'
    external_cleanup = []
    external_dirs = []
    embedded_beats = set()
    for source in sources:
        for field in ('beats', 'beat_ladder', 'timelapse_beats', 'reverse_beats'):
            if field not in embedded_beats and isinstance(source.get(field), (dict, list)) and source[field]:
                path = project / (field + '.json')
                if path in retained:
                    path = project / ('关联_' + field + '.json')
                exports[path] = json.dumps(source[field], ensure_ascii=False, indent=2).encode('utf-8')
                retained[path] = 'beats'
                embedded_beats.add(field)
        job_id = source.get('replica_job_id')
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            continue
        directory = _safe(root / 'replica_jobs' / job_id, root)
        if not directory.is_dir():
            continue
        linked_files = list(_files(directory))
        beats = directory / 'timelapse_beats.json'
        beat_data = _json(beats)
        if beat_data is None:
            replica_state = _json(directory / '.replica_pipeline.json') or {}
            beat_data = replica_state.get('beats')
        if beat_data is None:
            raise ValueError('关联的反推节拍数据无法读取，已停止归档')
        destination = project / ('反推节拍_' + job_id + '.json')
        exports[destination] = (beats.read_bytes() if beats.is_file() and _json(beats) is not None
                                else json.dumps(beat_data, ensure_ascii=False, indent=2).encode('utf-8'))
        retained[destination] = 'beats'
        shared = any(item not in items and item.get('replica_job_id') == job_id for item in library)
        shared = shared or any(task not in matched and job_id in (
            (task.get('result') or {}).get('replica_job_id'),
            (task.get('dimensions') or {}).get('replica_job_id')) for task in tasks)
        if not shared:
            external_cleanup.extend(linked_files)
            external_dirs.append(directory)
    stepped = _json(project / '.stepped_pipeline.json') or {}
    if stepped.get('beat_ladder'):
        path = project / '生成节拍.json'
        exports[path] = json.dumps({'source': 'generated_plan', 'beat_ladder': stepped['beat_ladder']},
                                  ensure_ascii=False, indent=2).encode('utf-8')
        retained[path] = 'beats'
    if pending:
        for entry in pending.get('retained_files', []):
            path = _safe(_url_path(entry.get('url'), root), project)
            if path.is_file() and entry.get('kind') in ('beats', 'prompts', 'cover'):
                retained[path] = entry['kind']
    cover_url = archive_cover_url(pending, project, root)
    if not cover_url and (pending or {}).get('status') != 'archived':
        cover_data = _new_archive_cover(project, root, sources, row, final_videos)
        if cover_data:
            cover_path = _safe(project / ARCHIVE_COVER, project)
            exports[cover_path] = cover_data
            retained[cover_path] = 'cover'
            cover_url = _url(cover_path, root)
    deleted = [path for path in files if path not in retained and path.name != RECEIPT]
    deleted.extend(external_cleanup)
    # Associated task metadata, results and event logs are also removed.
    for task in matched:
        for path in map(Path, store._task_paths(task['id'])):
            _safe(path, Path(os.path.abspath(store.TASKS_DIR)))
            if path.is_file():
                deleted.append(path)
    deleted = list(dict.fromkeys(deleted))
    deleted_bytes = sum(path.stat().st_size for path in deleted)
    entries = [_entry(path, root, kind) for path, kind in retained.items()]
    archived_at = (pending or {}).get('archived_at') or time.time()
    archive = {'archived_at': archived_at, 'video_retention': video_retention, 'cover_url': cover_url,
               'final_videos': [_entry(path, root, kind) for path, kind in final_videos.items()],
               'refined_videos': [_entry(path, root, kind) for path, kind in final_videos.items()
                                  if kind == 'refined_video'],
               'prompts_url': _url(project / PROMPTS_TEXT, root),
               'beats_url': next((entry['url'] for entry in entries if entry['kind'] == 'beats'), None),
               'retained_files': entries, 'deleted_bytes': max(deleted_bytes, (pending or {}).get('deleted_bytes') or 0)}
    lib_id = items[0]['id'] if items else (pending or {}).get('library_id') or 'archive_' + hashlib.sha256(row['project_key'].encode()).hexdigest()[:20]
    public = {'project_key': row['project_key'], 'title': row['title'], 'retained_files': entries,
              'cover': cover_url,
              'video_retention': video_retention,
              'delete_count': len(deleted), 'delete_bytes': deleted_bytes, 'archive': archive,
              'library': {'id': lib_id}}
    return {'public': public, 'project': project, 'root': root, 'exports': exports,
            'retained': retained, 'deleted': deleted, 'items': items, 'tasks': matched,
            'row': row, 'pending': pending, 'external_dirs': external_dirs}


def _write_bytes(path, data):
    temporary = path.with_name('.' + path.name + '.archive-tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _execute(plan):
    project, public = plan['project'], plan['public']
    for path, data in plan['exports'].items():
        _write_bytes(path, data)
        if path.read_bytes() != data:
            raise OSError('归档导出验证失败，已停止清理')
    for path in plan['retained']:
        if not path.is_file() or path.stat().st_size <= 0:
            raise OSError('待保留的归档文件缺失，已停止清理')
    record = dict(public['archive'], status='prepared', project_key=public['project_key'], title=public['title'],
                  library_id=public['library']['id'], task_ids=[task['id'] for task in plan['tasks']],
                  library_ids=[item['id'] for item in plan['items']])
    _write_bytes(project / RECEIPT, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
    # The receipt and all three kinds of deliverables are durable before cleanup begins.
    for path in plan['deleted']:
        path.unlink(missing_ok=True)
    for parent, dirs, _ in os.walk(project, topdown=False):
        for directory in dirs:
            path = Path(parent) / directory
            if not any(path.iterdir()):
                path.rmdir()
    for directory in plan['external_dirs']:
        for parent, _, _ in os.walk(directory, topdown=False):
            path = Path(parent)
            if not any(path.iterdir()):
                path.rmdir()
    for task in plan['tasks']:
        store.ACTIVE_TASKS.pop(task['id'], None)
        store._TASK_FLUSHED_EVENTS.pop(task['id'], None)
    compact = {'id': public['library']['id'], 'project_key': public['project_key'],
               'title': public['title'], 'theme': plan['row'].get('theme') or public['title'],
               'timestamp': plan['row'].get('timestamp') or '', 'archived': True,
               'archive': public['archive']}
    store.write_library_item(compact)
    extra_ids = [item['id'] for item in plan['items'] if item['id'] != compact['id']]
    if extra_ids:
        store.delete_library_items(extra_ids)
    record['status'] = 'archived'
    _write_bytes(project / RECEIPT, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
    store._PROJ_ASSET_STATS_CACHE.pop(str(project), None)
    store._PROJ_PROGRESS_CACHE.pop(str(project), None)
    return public


def archive_projects(project_keys, *, preview=True, base_dir=None):
    if not isinstance(project_keys, list) or not project_keys or any(
            not isinstance(key, str) or not key.strip() for key in project_keys):
        raise ValueError('请选择需要归档的项目')
    if len(project_keys) > 200:
        raise ValueError('每次最多归档 200 个项目')
    base_dir = base_dir or str(Path(__file__).resolve().parent)
    import codex_video_editor as editor
    # Preview is read-only. Execution excludes detached editor admission and project
    # mutations; ACTIVE_TASKS_LOCK closes the background-worker registration race.
    edit_lock = contextlib.nullcontext() if preview else editor._lock()
    result = {'status': 'ok', 'projects': [], 'errors': [], 'count': 0,
              'deleted_bytes': 0, 'deleted_library_ids': []}
    with MUTATION_LOCK, edit_lock, store.ACTIVE_TASKS_LOCK, store.LIBRARY_LOCK, store._TASK_IO_LOCK:
        library = store.read_library()
        if library is None:
            raise ValueError('项目库无法读取，已停止归档')
        tasks = list(store.ACTIVE_TASKS.values())
        rows = store.build_projects_index(tasks=tasks, library_items=library, base_dir=base_dir, with_assets=False)
        by_key = {row['project_key']: row for row in rows}
        for key in dict.fromkeys(project_keys):
            row = by_key.get(key)
            try:
                if not row:
                    raise ValueError('项目不存在，请刷新项目列表')
                plan = _prepare(row, rows, library, tasks, base_dir)
                if row.get('archived') and plan['pending'] and plan['pending'].get('status') == 'archived':
                    public = dict(plan['public'], delete_count=0, delete_bytes=0)
                else:
                    public = plan['public'] if preview else _execute(plan)
                result['projects'].append(public)
                if not preview:
                    result['deleted_bytes'] += public['delete_bytes']
                    result['deleted_library_ids'].extend(item['id'] for item in plan['items'])
                    # The last owner in a bulk archive can now clear shared reverse
                    # intermediates after earlier owners have exported their beats.
                    library = store.read_library()
                    if library is None:
                        raise RuntimeError('归档后项目库读取失败')
                    tasks = list(store.ACTIVE_TASKS.values())
            except (ValueError, OSError, RuntimeError) as error:
                result['errors'].append({'project_key': key, 'title': (row or {}).get('title') or key,
                                         'message': str(error)})
        result['count'] = len(result['projects'])
    return result


def restore_archive_covers(project_keys=None, *, base_dir=None):
    """Explicit, idempotent repair for old archives; never called by a GET handler.

    Only published receipt videos may provide missing thumbnails. Each failure
    rolls back thumbnail, receipt and compact library changes; video files are
    never opened for writing.
    """
    if project_keys is not None and (not isinstance(project_keys, list)
            or any(not isinstance(key, str) or not key.strip() for key in project_keys)):
        raise ValueError('请选择需要恢复封面缩略图的归档项目')
    base_dir = base_dir or str(Path(__file__).resolve().parent)
    root = _root(base_dir)
    requested = set(project_keys) if project_keys is not None else None
    result = {'status': 'ok', 'projects': [], 'errors': [], 'count': 0}
    with MUTATION_LOCK, store.LIBRARY_LOCK:
        library = store.read_library()
        if library is None:
            raise ValueError('项目库无法读取，已停止恢复封面缩略图')
        found = set()
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir() or directory.name.startswith('.'):
                continue
            record = receipt(directory)
            if not record or record.get('status') != 'archived':
                continue
            key = record.get('project_key')
            if not isinstance(key, str) or not key or (requested is not None and key not in requested):
                continue
            found.add(key)
            snapshots = {}
            started_write = False
            try:
                project = _safe(directory, root)
                cover_url = archive_cover_url(record, project, root)
                if cover_url:
                    result['projects'].append({'project_key': key, 'cover': cover_url,
                                               'cover_url': cover_url, 'restored': False})
                    continue
                videos = _receipt_videos(project, root, record)
                data = next((data for path in videos
                             if (data := _thumbnail_bytes(path, video=True))), None)
                if not data:
                    raise ValueError('归档成片无法提取封面缩略图')
                thumbnail = _safe(project / ARCHIVE_COVER, project)
                cover_url = _url(thumbnail, root)
                updated = dict(record, cover_url=cover_url)
                retained = record.get('retained_files')
                if not isinstance(retained, list) or any(not isinstance(entry, dict)
                        or not isinstance(entry.get('url'), str) for entry in retained):
                    raise ValueError('归档记录中的保留文件无效')
                updated['retained_files'] = [entry for entry in retained
                    if isinstance(entry, dict) and entry.get('kind') != 'cover']
                updated['retained_files'].append(_entry(thumbnail, root, 'cover'))
                existing = next((item for item in library if item.get('project_key') == key), {})
                lib_id = (record.get('library_id') or existing.get('id')
                          or 'archive_' + hashlib.sha256(key.encode()).hexdigest()[:20])
                updated['library_id'] = lib_id
                archive = {field: value for field, value in updated.items()
                           if field not in ('status', 'project_key', 'title', 'library_id',
                                            'task_ids', 'library_ids')}
                compact = {'id': lib_id, 'project_key': key, 'title': record.get('title') or key,
                    'theme': existing.get('theme') or record.get('title') or key,
                    'timestamp': existing.get('timestamp') or '', 'archived': True, 'archive': archive}
                _, index_path, _ = store._library_paths()
                library_path = Path(store._library_item_path(lib_id))
                for path in (thumbnail, project / RECEIPT, library_path, Path(index_path)):
                    if path.is_symlink():
                        raise ValueError('归档封面恢复目标含符号链接')
                    snapshots[path] = path.read_bytes() if path.is_file() else None
                started_write = True
                _write_bytes(thumbnail, data)
                _write_bytes(project / RECEIPT,
                             json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8'))
                store.write_library_item(compact)
                store._PROJ_ASSET_STATS_CACHE.pop(str(project), None)
                result['projects'].append({'project_key': key, 'cover': cover_url,
                                           'cover_url': cover_url, 'restored': True})
            except (OSError, ValueError, RuntimeError) as error:
                if started_write:
                    for path, previous in snapshots.items():
                        if previous is None:
                            path.unlink(missing_ok=True)
                        else:
                            _write_bytes(path, previous)
                result['errors'].append({'project_key': key, 'message': str(error)})
        for key in sorted((requested or set()) - found):
            result['errors'].append({'project_key': key, 'message': '未找到已完成归档的项目'})
    result['count'] = len(result['projects'])
    return result
