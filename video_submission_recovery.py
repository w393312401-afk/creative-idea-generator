"""Resolve original Flow submissions through queries and validated downloads only.

No function in this module dispatches a generation request. Missing identities,
query errors and ambiguous upstream states retain the duplicate-charge guard.
"""
import asyncio
import copy
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

import server_common as common
from flow2api_video import Flow2APIVideoService
from video_operations import VideoOperationStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def submission_evidence(receipt, payload):
    """Only exact, explicit final evidence can settle a receipt."""
    submission_id = receipt.get('submission_id')
    if not isinstance(payload, dict) or payload.get('client_submission_id') != submission_id:
        return 'pending'
    if payload.get('submission_pending') is not False:
        return 'not_found' if payload.get('state') == 'not_found' else 'pending'
    state, accepted = payload.get('state'), payload.get('upstream_accepted')
    operation = payload.get('operation_id')
    if receipt.get('operation_id') and operation != receipt['operation_id']:
        return 'pending'
    if (state == 'refused' and accepted is False and not operation
            and receipt.get('upstream_accepted') is not True):
        return 'refused'
    if state == 'failed' and accepted is True and operation:
        return 'failed'
    if (state == 'confirmed' and accepted is True and operation
            and (payload.get('video_url') or payload.get('delivery_pending') is True)):
        return 'confirmed'
    return 'not_found' if state == 'not_found' else 'pending'


def query_submission(service, receipt, *, cancel_check=None):
    async def query():
        url = urljoin(service._root_url, 'v1/submissions/' + quote(receipt['submission_id'], safe=''))
        async with service._client_factory(timeout=httpx.Timeout(50, connect=10),
                                           follow_redirects=False, trust_env=False) as client:
            request = asyncio.create_task(client.get(url, headers={'Authorization': f'Bearer {service._api_key}'}))
            try:
                while not request.done():
                    if cancel_check and cancel_check():
                        raise ConnectionError('用户取消了视频恢复')
                    await asyncio.wait({request}, timeout=.25)
                response = await request
            finally:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
            response.raise_for_status()
            return response.json()
    return asyncio.run(query())


def project_receipts(project_key, slots, manifest, store, *, latest_only=False):
    key = common._safe_project_name(project_key)
    receipts = list(store.project_submissions(key))
    # Older receipts predate durable scope. Use the original task's recorded
    # project/provider, never the currently selected generation service.
    with common.ACTIVE_TASKS_LOCK:
        tasks = [(tid, dict(task.get('dimensions') or {}))
                 for tid, task in common.ACTIVE_TASKS.items()]
    for task_id, dimensions in tasks:
        project = dimensions.get('project_key') or dimensions.get('theme')
        if not project or common._safe_project_name(project) != key:
            continue
        for receipt in store.submissions(task_id):
            if not receipt.get('project_key'):
                receipts.append((task_id, {**receipt, 'project_key': key,
                    'provider': receipt.get('provider') or dimensions.get('video_provider')}))
    wanted = None if slots is None else set(slots)
    videos = {v.get('slot'): v for v in manifest.get('videos', []) if isinstance(v, dict)}
    selected = {}
    for task_id, receipt in receipts:
        slot = receipt.get('slot')
        if receipt.get('provider') != 'flow2api' or not receipt.get('submission_id'):
            continue
        if wanted is not None and slot not in wanted:
            continue
        video = videos.get(slot, {})
        attempt = video.get('last_attempt') or {}
        if latest_only and attempt.get('submission_id') != receipt['submission_id']:
            continue
        undelivered = (attempt.get('submission_id') == receipt['submission_id']
                       and attempt.get('status', video.get('status')) != 'success')
        if receipt.get('submission_pending') is True or undelivered:
            selected[(task_id, slot, receipt['submission_id'])] = (task_id, receipt)
    return list(selected.values())


def _update_attempt(project_dir, receipt, *, state, message=None):
    with common.manifest_lock(str(project_dir)):
        manifest = common.read_manifest(str(project_dir))
        if not isinstance(manifest, dict):
            raise ValueError('项目记录读取失败，已停止接回视频')
        for video in manifest.get('videos', []):
            if not isinstance(video, dict) or video.get('slot') != receipt.get('slot'):
                continue
            attempt = video.get('last_attempt') or {}
            if attempt.get('submission_id') != receipt['submission_id']:
                continue
            attempt.update(receipt)
            attempt['recovery_state'] = state
            if state in ('refused', 'failed', 'recovery_failed'):
                attempt.update(status='failed', error=message, finished_at=_now())
                if video.get('status') != 'success':
                    video.update(status='failed', error=message)
            video['last_attempt'] = attempt
        common.write_json_atomic(str(project_dir / 'manifest.json'), manifest)
    return manifest


def _frame_path(project_dir, manifest, slot):
    if slot is None:
        return None
    entry = next((f for f in manifest.get('frames', [])
                  if isinstance(f, dict) and f.get('slot', f.get('sequence')) == slot), {})
    raw = entry.get('file')
    root = Path(__file__).resolve().parent
    candidate = (Path(raw) if Path(str(raw)).is_absolute() else root / str(raw)) if raw else project_dir / 'frames' / f'img_{slot:03d}.webp'
    candidate = candidate.resolve()
    if not candidate.is_relative_to(project_dir.resolve()) or not candidate.is_file():
        raise ValueError('原视频锚点文件缺失，已保留原提交记录')
    return str(candidate)


def adopt_result(service, project_dir, manifest, receipt, payload, config, checkpoint, *, cancel_check=None):
    """Run the original slot's media, anchor and process checks before replacement."""
    import video_generator as vg
    if not payload.get('video_url'):
        raise ValueError('上游已确认原视频成功，下载会话暂不可用；恢复会话后再次取回原视频')
    slot = receipt['slot']
    old = next((v for v in manifest.get('videos', []) if v.get('slot') == slot), None)
    if not old or (old.get('last_attempt') or {}).get('submission_id') != receipt['submission_id']:
        raise ValueError('该槽位已属于另一次尝试，原视频仍保留在上游')
    model = (old.get('last_attempt') or {}).get('api_model') or payload.get('api_model')
    if payload.get('api_model') and model != payload['api_model']:
        raise ValueError('原任务模型与该槽位记录不一致')
    match = re.fullmatch(r'omni-1\.1-flash(?:-frames)?-(4|6|8|10)s-(portrait|landscape)-(360|720)p', str(model))
    if not match:
        raise ValueError('原任务缺少可核验的视频参数')
    seconds, orientation, edge = int(match[1]), match[2], int(match[3])
    size = (edge * 16 // 9, edge)
    if orientation == 'portrait':
        size = size[::-1]
    start_slot = old.get('start_anchor_slot', slot)
    end_slot = old.get('end_anchor_slot')
    if 'end_anchor_slot' not in old and manifest.get('generation_channel') != 'video_chain' and not old.get('is_hero'):
        end_slot = slot + 1
    plan = {'slot': slot, 'seq': old.get('sequence', slot), 'prompt': old.get('prompt', ''),
            'meta': old.get('meta', ''), 'start_anchor_slot': start_slot,
            'end_anchor_slot': end_slot,
            'start_frame': _frame_path(project_dir, manifest, start_slot) if '-frames-' in model else None,
            'end_frame': _frame_path(project_dir, manifest, end_slot),
            'dest_path': str(project_dir / 'videos' / f'vid_{slot:03d}.mp4')}
    Path(plan['dest_path']).parent.mkdir(parents=True, exist_ok=True)
    writer = vg._ManifestWriter(str(project_dir / 'manifest.json'), copy.deepcopy(manifest),
                                [v['slot'] for v in manifest.get('videos', []) if 'slot' in v])
    old_attempt = old.get('last_attempt') or {}
    writer.attempt_id = old_attempt.get('id') or writer.attempt_id
    writer.started_at = old_attempt.get('started_at') or writer.started_at
    writer.attempt_outcomes = {v['slot']: {'status': v.get('status', 'failed'),
                                          **copy.deepcopy(v.get('last_attempt') or {})}
                               for v in manifest.get('videos', []) if 'slot' in v}
    # _ManifestWriter starts a new stats snapshot; recovery belongs to the
    # original attempt and must preserve its history and unrelated failures.
    writer.data['video_generation_stats'] = copy.deepcopy(manifest.get('video_generation_stats') or {})
    writer.data['video_generation_stats'].setdefault('last_run', {})

    def progress(stage, details):
        if stage == 'request_resolved':
            checkpoint(receipt)

    def process_gate(item):
        return vg.check_video_process(config, item['dest_path'], item['start_frame'],
                                      item['end_frame'], item['prompt'], meta=item.get('meta', ''))

    with tempfile.TemporaryDirectory(prefix='.recover-', dir=project_dir / 'videos') as temporary:
        prepared = {'output_dir': Path(temporary), 'model': model, 'duration': seconds,
                    'expected_size': size, 'submission': receipt}
        result = asyncio.run(service._bounded(service._download(payload['video_url'], prepared), cancel_check))
        bridge = vg._BatchBridge([{'plan': plan, 'req': None}], len(writer.all_slots),
                                 old.get('model', 'Omni Flash'), writer, progress,
                                 strict=vg.strict_gates_enabled(config), process_check_fn=process_gate,
                                 config=config)
        bridge(0, 'video_done', result)
        if writer.attempt_outcomes[slot].get('status') != 'success':
            raise ValueError('原视频已找回，但未通过该槽位验收，原成功片段已保留')
        if manifest.get('generation_channel') == 'video_chain':
            # Replacing a saved downstream anchor would invalidate later clips.
            # Restore a missing tail only; existing generated frames stay intact.
            tail = project_dir / 'frames' / f'img_{slot + 1:03d}.webp'
            if not tail.exists():
                tail.parent.mkdir(parents=True, exist_ok=True)
                if not vg._extract_video_frame(plan['dest_path'], str(tail), 'last', sseof_offset=0.15):
                    raise ValueError('原视频已恢复，但尾帧提取失败')
                rel = os.path.relpath(tail, Path(__file__).resolve().parent).replace('\\', '/')
                writer.record_frame({'slot': slot + 1, 'sequence': slot + 1,
                    'file': rel, 'url': '/' + rel, 'quality_gate': 'extracted_from_video_last_frame',
                    'source': f'vid_{slot:03d}.mp4'})
        writer.data.pop('merged_video', None)
        writer.data.setdefault('video_recoveries', []).append({'slot': slot,
            'submission_id': receipt['submission_id'], 'operation_id': receipt.get('operation_id'),
            'method': 'original_operation_query', 'new_generation_requests': 0, 'recovered_at': _now()})
        writer.save()
    return writer.data


def reconcile_video_submissions(project_key, slots, config, *, store=None, on_checkpoint=None,
                                query=None, adopt=None, latest_only=False, cancel_check=None):
    store = store or VideoOperationStore()
    directory = Path(common._get_project_dir(project_key)).resolve()
    manifest = common.read_manifest(str(directory))
    if not isinstance(manifest, dict):
        raise ValueError('项目记录不存在')
    from project_archive import receipt as archive_receipt
    if (archive_receipt(str(directory)) or {}).get('status') in ('prepared', 'archived'):
        raise ValueError('项目已归档，不能接回视频')
    candidates = project_receipts(project_key, slots, manifest, store, latest_only=latest_only)
    if not candidates:
        return {'status': 'ok', 'outcomes': [], 'manifest': manifest}
    service = Flow2APIVideoService({**config, 'videoProvider': 'flow2api'})
    outcomes = []
    for task_id, original in candidates:
        if cancel_check and cancel_check():
            raise ConnectionError('用户取消了视频恢复')
        slot = original['slot']
        try:
            payload = (query(service, original) if query else
                       query_submission(service, original, cancel_check=cancel_check))
            state = submission_evidence(original, payload)
        except Exception:
            if cancel_check and cancel_check():
                raise ConnectionError('用户取消了视频恢复')
            outcomes.append({'slot': slot, 'state': 'pending', 'message': '原任务查询中断，仍保留未决保护'})
            continue
        if state in ('pending', 'not_found'):
            # A pending query may reveal acceptance/identity that was lost when
            # the original SSE disconnected. Preserve that anchor before an
            # unattended worker decides whether another submission is allowed.
            operation = payload.get('operation_id') if isinstance(payload, dict) else None
            if (isinstance(payload, dict)
                    and payload.get('client_submission_id') == original['submission_id']
                    and payload.get('upstream_accepted') is True
                    and (not original.get('operation_id') or operation == original['operation_id'])):
                discovered = {**original, 'submission_pending': True, 'upstream_accepted': True}
                for key in ('operation_id', 'upstream_task_id', 'upstream_account_id', 'api_model'):
                    if payload.get(key) is not None:
                        discovered[key] = payload[key]
                if payload.get('account_id') is not None and 'upstream_account_id' not in payload:
                    discovered['upstream_account_id'] = payload['account_id']
                saved = store.record_submission(task_id, discovered)
                manifest = _update_attempt(directory, discovered, state='pending')
                if on_checkpoint:
                    on_checkpoint(task_id, saved, manifest)
            message = ('上游尚无可核验的对应记录，仍保留未决保护' if state == 'not_found'
                       else '原任务结果仍待确认，请稍后再核对')
            outcomes.append({'slot': slot, 'state': state, 'message': message})
            continue
        resolved = {**original, 'submission_pending': False, 'confirmed': state == 'confirmed',
                    'upstream_accepted': payload['upstream_accepted'], 'recovery_state': state,
                    'recovered_at': _now()}
        for key in ('operation_id', 'upstream_task_id', 'upstream_account_id', 'api_model'):
            if payload.get(key) is not None:
                resolved[key] = payload[key]
        if payload.get('account_id') is not None and 'upstream_account_id' not in payload:
            resolved['upstream_account_id'] = payload['account_id']

        def checkpoint(value, task_id=task_id):
            saved = store.record_submission(task_id, value)
            if on_checkpoint:
                on_checkpoint(task_id, saved, common.read_manifest(str(directory)) or {})

        # Persist definitive settlement before media I/O. A failed download
        # cannot turn a known terminal operation back into a duplicate-charge risk.
        checkpoint(resolved)
        message = ('上游确认未开始生成，恢复服务后可重试' if state == 'refused'
                   else '上游确认原任务生成失败，可以重试该段')
        manifest = _update_attempt(directory, resolved, state=state,
                                   message=message if state != 'confirmed' else None)
        if state == 'confirmed':
            if cancel_check and cancel_check():
                raise ConnectionError('用户取消了视频恢复')
            try:
                if adopt:
                    manifest = adopt(service, directory, manifest, resolved, payload, config, checkpoint)
                else:
                    manifest = adopt_result(service, directory, manifest, resolved, payload,
                                            config, checkpoint, cancel_check=cancel_check)
                state, message = 'recovered', '已找回原任务视频并通过验收，没有重新生成'
            except Exception as error:
                if cancel_check and cancel_check():
                    raise ConnectionError('用户取消了视频恢复')
                state, message = 'recovery_failed', str(error)
                manifest = _update_attempt(directory, resolved, state=state, message=message)
        resolved['recovery_state'] = state
        if state == 'recovered':
            manifest = _update_attempt(directory, resolved, state=state)
        checkpoint(resolved)
        outcomes.append({'slot': slot, 'state': state, 'message': message})
    return {'status': 'ok', 'outcomes': outcomes, 'manifest': manifest}
