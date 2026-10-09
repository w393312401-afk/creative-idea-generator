import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import server_common as common
import video_generator as vg
import video_submission_recovery as recovery
from video_operations import VideoOperationStore


MODEL = 'omni-1.1-flash-frames-10s-portrait-360p'


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / 'project'
    directory.mkdir()
    receipt = {'slot': 37, 'submission_id': 'original-submission', 'provider': 'flow2api',
               'project_key': 'project', 'submission_pending': True, 'confirmed': False,
               'prompt_hash': 'original-prompt', 'api_model': MODEL}
    manifest = {'videos': [{'slot': 37, 'status': 'failed', 'prompt': 'Original prompt',
        'model': 'Omni Flash', 'start_anchor_slot': 37, 'end_anchor_slot': 38,
        'last_attempt': {**receipt, 'id': 'original-attempt', 'status': 'failed'}}]}
    common.write_manifest(str(directory), manifest)
    monkeypatch.setattr(common, '_get_project_dir', lambda key: str(directory))
    monkeypatch.setattr(recovery, 'Flow2APIVideoService', lambda config: SimpleNamespace())
    store = VideoOperationStore(str(tmp_path / 'operations.sqlite3'))
    store.record_submission('original-task', receipt)
    return directory, manifest, receipt, store


def run(project, evidence, **kwargs):
    return recovery.reconcile_video_submissions('project', [37], {}, store=project[3],
        query=lambda *args: evidence, **kwargs)


def evidence(state, **kwargs):
    return {'client_submission_id': 'original-submission', 'state': state,
            'submission_pending': False, **kwargs}


@pytest.mark.parametrize('payload', [
    {}, evidence('not_found'), evidence('refused', upstream_accepted=None),
    evidence('refused', upstream_accepted=False, operation_id='paid-operation'),
    evidence('failed', upstream_accepted=True),
    evidence('confirmed', upstream_accepted=True, operation_id='paid-operation'),
    evidence('confirmed', upstream_accepted=True, operation_id='paid-operation',
             video_url='https://flow-content.google/original', submission_pending=True),
    evidence('refused', upstream_accepted=False, client_submission_id='another-submission'),
])
def test_ambiguous_evidence_never_unlocks_or_downloads(project, payload):
    result = run(project, payload, adopt=lambda *args: pytest.fail('ambiguous evidence must not download'))
    assert result['outcomes'][0]['state'] in ('pending', 'not_found')
    assert project[3].submissions('original-task')[0]['submission_pending'] is True
    assert common.read_manifest(str(project[0]))['videos'][0]['last_attempt']['submission_pending'] is True


def test_known_operation_cannot_be_replaced_by_different_result(project):
    receipt = {**project[2], 'operation_id': 'original-operation'}
    project[3].record_submission('original-task', receipt)
    result = run(project, evidence('confirmed', upstream_accepted=True, operation_id='another-operation',
                                  video_url='https://flow-content.google/original'))
    assert result['outcomes'][0]['state'] == 'pending'
    assert project[3].submissions('original-task')[0]['submission_pending'] is True


def test_paid_acceptance_cannot_be_downgraded_to_refusal_without_operation(project):
    project[3].record_submission('original-task', {**project[2], 'upstream_accepted': True})
    result = run(project, evidence('refused', upstream_accepted=False))
    assert result['outcomes'][0]['state'] == 'pending'
    assert project[3].submissions('original-task')[0]['submission_pending'] is True


@pytest.mark.parametrize('state,accepted,operation', [('refused', False, None), ('failed', True, 'paid-operation')])
def test_definitive_terminal_failure_settles_exact_receipt(project, state, accepted, operation):
    result = run(project, evidence(state, upstream_accepted=accepted, operation_id=operation))
    assert result['outcomes'][0]['state'] == state
    saved, = project[3].submissions('original-task')
    assert saved['submission_pending'] is False
    assert saved['upstream_accepted'] is accepted
    assert result['manifest']['videos'][0]['last_attempt']['submission_pending'] is False
    assert result['manifest']['videos'][0]['status'] == 'failed'
    assert recovery.project_receipts('project', [38], result['manifest'], project[3]) == []


def test_query_failure_retains_guard(project):
    def query(*args):
        raise OSError('query disconnected')
    result = recovery.reconcile_video_submissions('project', [37], {}, store=project[3], query=query)
    assert result['outcomes'][0]['state'] == 'pending'
    assert project[3].submissions('original-task')[0]['submission_pending'] is True


def test_pending_query_recovers_acceptance_identity_before_next_retry(project):
    result = run(project, {'client_submission_id': 'original-submission', 'state': 'pending',
        'submission_pending': True, 'upstream_accepted': True,
        'operation_id': 'accepted-operation', 'account_id': 7})
    saved, = project[3].submissions('original-task')
    attempt = result['manifest']['videos'][0]['last_attempt']
    for receipt in (saved, attempt):
        assert receipt['operation_id'] == 'accepted-operation'
        assert receipt['upstream_account_id'] == 7
        assert receipt['upstream_accepted'] is True
        assert receipt['submission_pending'] is True
    assert result['outcomes'][0]['state'] == 'pending'


def test_download_failure_retains_settlement_and_allows_original_download_retry(project):
    payload = evidence('confirmed', upstream_accepted=True, operation_id='paid-operation',
                       video_url='https://flow-content.google/original', api_model=MODEL)
    def broken(*args):
        assert project[3].submissions('original-task')[0]['submission_pending'] is False
        raise ValueError('Original download unavailable')
    result = run(project, payload, adopt=broken)
    assert result['outcomes'][0]['state'] == 'recovery_failed'
    assert result['manifest']['videos'][0]['last_attempt']['submission_pending'] is False
    assert len(recovery.project_receipts('project', [37], result['manifest'], project[3])) == 1


def test_paid_success_with_unavailable_delivery_settles_and_offers_original_retry(project):
    result = run(project, evidence('confirmed', upstream_accepted=True, operation_id='paid-operation',
                                  video_url=None, delivery_pending=True, api_model=MODEL))
    assert result['outcomes'][0]['state'] == 'recovery_failed'
    assert '再次取回原视频' in result['outcomes'][0]['message']
    saved, = project[3].submissions('original-task')
    assert saved['submission_pending'] is False and saved['confirmed'] is True
    assert saved['recovery_state'] == 'recovery_failed'
    assert len(recovery.project_receipts('project', [37], result['manifest'], project[3])) == 1


def test_media_recovery_uses_normal_gates_and_preserves_previous_on_rejection(project, monkeypatch):
    directory, manifest, receipt, store = project
    frames = directory / 'frames'
    frames.mkdir()
    (frames / 'img_037.webp').write_bytes(b'original-start')
    (frames / 'img_038.webp').write_bytes(b'original-end')
    destination = directory / 'videos' / 'vid_037.mp4'
    destination.parent.mkdir()
    destination.write_bytes(b'previous-success')
    manifest['videos'][0].update(status='success', file=str(destination), retained_previous=True)
    common.write_manifest(str(directory), manifest)
    monkeypatch.setattr(vg, 'verify_video_anchors', lambda *args, **kwargs: (False, 'mismatch'))
    monkeypatch.setattr(vg, 'check_video_process', lambda *args, **kwargs: pytest.fail('anchor rejection precedes process gate'))

    class DownloadOnly:
        async def _download(self, url, prepared):
            path = prepared['output_dir'] / 'original.mp4'
            path.write_bytes(b'recovered-original')
            return {'video_url': str(path), **prepared['submission']}
        async def _bounded(self, operation, cancel):
            return await operation
    monkeypatch.setattr(recovery, 'Flow2APIVideoService', lambda config: DownloadOnly())
    result = run(project, evidence('confirmed', upstream_accepted=True, operation_id='paid-operation',
                                  video_url='https://flow-content.google/original', api_model=MODEL))
    assert result['outcomes'][0]['state'] == 'recovery_failed'
    assert destination.read_bytes() == b'previous-success'
    assert result['manifest']['videos'][0]['status'] == 'success'
    assert result['manifest']['videos'][0]['last_attempt']['submission_pending'] is False


def test_recovered_video_replaces_only_original_slot(project, monkeypatch):
    directory, manifest, receipt, store = project
    (directory / 'frames').mkdir()
    for slot in (37, 38):
        (directory / 'frames' / f'img_{slot:03d}.webp').write_bytes(b'frame')
    destination = directory / 'videos' / 'vid_036.mp4'
    destination.parent.mkdir()
    destination.write_bytes(b'unrelated-success')
    manifest['videos'].insert(0, {'slot': 36, 'status': 'success', 'file': str(destination)})
    common.write_manifest(str(directory), manifest)
    monkeypatch.setattr(vg, 'verify_video_anchors', lambda *args, **kwargs: (True, 'PASS'))
    monkeypatch.setattr(vg, 'check_video_process', lambda *args, **kwargs: ('accept', 'PASS'))
    class DownloadOnly:
        async def _download(self, url, prepared):
            path = prepared['output_dir'] / 'original.mp4'
            path.write_bytes(b'recovered-original')
            return {'video_url': str(path), **prepared['submission']}
        async def _bounded(self, operation, cancel):
            return await operation
    monkeypatch.setattr(recovery, 'Flow2APIVideoService', lambda config: DownloadOnly())
    result = run(project, evidence('confirmed', upstream_accepted=True, operation_id='paid-operation',
                                  video_url='https://flow-content.google/original', api_model=MODEL))
    assert result['outcomes'][0]['state'] == 'recovered'
    assert destination.read_bytes() == b'unrelated-success'
    assert (directory / 'videos' / 'vid_037.mp4').read_bytes() == b'recovered-original'
    assert result['manifest']['videos'][1]['last_attempt']['id'] == 'original-attempt'
    assert result['manifest']['video_recoveries'][0]['new_generation_requests'] == 0
    assert store.submissions('original-task')[0]['recovery_state'] == 'recovered'


def test_recorded_custom_frame_path_and_legacy_sequence_are_used(project):
    directory = project[0]
    frame = directory / 'custom-anchor.webp'
    frame.write_bytes(b'original-frame')
    manifest = {'frames': [{'sequence': 37, 'file': str(frame)}]}
    assert recovery._frame_path(directory, manifest, 37) == str(frame)
    with pytest.raises(ValueError):
        recovery._frame_path(directory, {'frames': [{'sequence': 37, 'file': str(directory.parent / 'outside.webp')}]}, 37)


def test_journal_identity_stays_stable_when_upstream_identity_arrives(tmp_path):
    store = VideoOperationStore(str(tmp_path / 'store.sqlite3'))
    original = {'slot': 37, 'provider': 'flow2api', 'project_key': 'project',
                'submission_id': 'original-submission', 'submission_pending': True}
    store.record_submission('task', original)
    store.record_submission('task', {**original, 'account_id': 'new-provenance',
        'operation_id': 'original-operation', 'submission_pending': False, 'confirmed': True})
    store.record_submission('task', original)
    saved, = store.submissions('task')
    assert saved['submission_pending'] is False
    assert saved['confirmed'] is True
    assert saved['operation_id'] == 'original-operation'


def test_recovery_endpoint_denies_live_project_before_query(monkeypatch):
    import server
    common.get_or_create_task('live-original', {'project_key': 'project', 'type': 'videos'})
    handler = object.__new__(server.SparkRequestHandler)
    sent = []
    handler._send_json = lambda data, status=200: sent.append((data, status))
    monkeypatch.setattr(recovery, 'reconcile_video_submissions', lambda *args, **kwargs: pytest.fail('live worker cannot reconcile'))
    handler._reconcile_video_operation({'title': 'project', 'target_slots': [37]})
    assert sent[0][1] == 409
    assert sent[0][0]['failure_code'] == 'PROJECT_BUSY'


def test_recovery_endpoint_releases_claim_on_query_error(monkeypatch):
    import server
    handler = object.__new__(server.SparkRequestHandler)
    released = []
    monkeypatch.setattr(server, '_claim_flow_project_run', lambda *args: None)
    monkeypatch.setattr(server, '_release_flow_project_run', lambda *args: released.append(args[1]))
    monkeypatch.setattr(recovery, 'reconcile_video_submissions', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('missing project')))
    with pytest.raises(ValueError, match='missing project'):
        handler._reconcile_video_operation({'title': 'project', 'target_slots': [37]})
    assert len(released) == 1


def test_recovery_holder_blocks_google_fx_generation_but_not_other_project(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, '_get_project_dir', lambda key: str(tmp_path / key))
    common.claim_frame_run(str(tmp_path / 'project'), 'video_recovery_original', until_released=True)
    try:
        assert server._claim_flow_project_run({'videoProvider': 'google_fx'}, 'new-task', 'project') == 'video_recovery_original'
        assert server._claim_flow_project_run({'videoProvider': 'google_fx'}, 'new-task', 'different-project') is None
    finally:
        common.release_frame_run(str(tmp_path / 'project'), 'video_recovery_original')


def test_recovery_checkpoint_populates_old_task_result_and_preserves_missing_slot_failure(monkeypatch):
    import server
    task = common.get_or_create_task('original-task', {'project_key': 'project', 'type': 'videos'})
    task.update(status='failed', result=None)
    manifest = {'prompt_block': '视频 37:\nOriginal.\n\n视频 38:\nStill missing.',
                'videos': [{'slot': 37, 'status': 'success', 'last_attempt': {'status': 'success'}}]}
    saved = []
    monkeypatch.setattr(server, '_claim_flow_project_run', lambda *args: None)
    monkeypatch.setattr(server, '_release_flow_project_run', lambda *args: None)
    monkeypatch.setattr(server, 'save_task_to_disk', lambda tid: saved.append(tid) or True)
    def reconcile(*args, on_checkpoint, **kwargs):
        on_checkpoint('original-task', {'slot': 37, 'submission_pending': False}, manifest)
        return {'status': 'ok', 'manifest': manifest, 'outcomes': []}
    monkeypatch.setattr(recovery, 'reconcile_video_submissions', reconcile)
    handler = object.__new__(server.SparkRequestHandler)
    handler._send_json = lambda *args, **kwargs: None
    handler._reconcile_video_operation({'title': 'project', 'target_slots': [37]})
    assert task['result']['videos'][0]['status'] == 'success'
    assert task['result']['has_failures'] is True
    assert task['result']['completion_state'] == 'partial_failed'
    assert task['status'] == 'failed'
    assert saved == ['original-task']


def test_automatic_recovery_queries_only_current_attempt(project):
    directory, manifest, original, store = project
    current = {**original, 'submission_id': 'current-submission'}
    store.record_submission('current-task', current)
    manifest['videos'][0]['last_attempt'].update(submission_id='current-submission')
    common.write_manifest(str(directory), manifest)
    queried = []
    def query(service, receipt):
        queried.append(receipt['submission_id'])
        return {}
    recovery.reconcile_video_submissions('project', [37], {}, store=store,
                                        query=query, latest_only=True)
    assert queried == ['current-submission']
    assert store.submissions('original-task')[0]['submission_pending'] is True


def test_automatic_query_cancel_stops_before_read_or_download(project):
    with pytest.raises(ConnectionError, match='取消'):
        recovery.reconcile_video_submissions('project', [37], {}, store=project[3],
            cancel_check=lambda: True, query=lambda *a: pytest.fail('no query after cancel'))
    assert project[3].submissions('original-task')[0]['submission_pending'] is True
