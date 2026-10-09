"""Automatic retries keep each paid submission's recovery identity separate."""
import json

import pytest

import server
import video_generator as vg
from video_operations import VideoOperationStore


def bridge_for_slot(tmp_path):
    writer = vg._ManifestWriter(str(tmp_path / 'manifest.json'), {}, [7])
    task = server.get_or_create_task('videos_flow2api_retry', {
        'project_key': 'retry-project', 'video_provider': 'flow2api',
        'request_id': 'retry-request-123',
    })
    bridge = vg._BatchBridge(
        [{'plan': {'slot': 7, 'seq': 1, 'prompt': 'Camera move'}}],
        1, 'omni-1.1-flash', writer,
        lambda stage, details: server._video_progress(task['id'], task, stage, details),
    )
    return bridge, writer, task


def submission(submission_id, **extra):
    return {'provider': 'flow2api', 'api_model': 'omni-1.1-flash-10s-portrait-360p',
            'submission_id': submission_id, 'prompt_hash': 'same-prompt',
            'confirmed': False, 'submission_pending': True, **extra}


def test_retry_uses_fresh_manifest_identity_and_keeps_both_durable_receipts(tmp_path):
    bridge, writer, task = bridge_for_slot(tmp_path)
    original = submission('original-submission', operation_id='failed-operation',
                          upstream_task_id='failed-task', upstream_account_id='original-account',
                          client_submission_id='original-submission', upstream_accepted=True)
    bridge(0, 'request_submitting', original)
    bridge(0, 'request_resolved', {
        **original, 'submission_pending': False, 'upstream_error_code': 'video_generation_failed',
        'upstream_reason': 'original task failed',
    })
    bridge(0, 'video_warning', {'code': 'flow2api_auto_retry', 'index': 0, 'retry': 1})
    bridge(0, 'request_submitting', submission('retry-submission'))

    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    fresh = manifest['videos'][0]['last_attempt']
    assert fresh['submission_id'] == 'retry-submission'
    assert fresh['submission_pending'] is True and fresh['confirmed'] is False
    for field in ('operation_id', 'upstream_task_id', 'upstream_account_id',
                  'client_submission_id', 'upstream_accepted', 'upstream_error_code', 'upstream_reason'):
        assert field not in fresh
    receipts = {r['submission_id']: r for r in VideoOperationStore().submissions(task['id'])}
    assert set(receipts) == {'original-submission', 'retry-submission'}
    old = receipts['original-submission']
    assert old['submission_pending'] is False and old['operation_id'] == 'failed-operation'
    assert old['upstream_account_id'] == 'original-account'
    assert receipts['retry-submission']['submission_pending'] is True
    assert 'operation_id' not in receipts['retry-submission']
    assert all(r['project_key'] == 'retry-project' for r in receipts.values())
    vg.ensure_video_submissions_resolved('retry-project', {7}, manifest=manifest)
    assert receipts['retry-submission']['submission_pending'] is True

    bridge(0, 'request_accepted', submission('retry-submission', operation_id='retry-operation',
                                           upstream_account_id='retry-account', upstream_accepted=True))
    bridge(0, 'request_resolved', submission('retry-submission',
                                           submission_pending=False, confirmed=True))
    fresh = writer.data['videos'][0]['last_attempt']
    assert fresh['operation_id'] == 'retry-operation'
    assert fresh['upstream_account_id'] == 'retry-account'
    assert 'upstream_error_code' not in fresh and 'upstream_reason' not in fresh
    vg.ensure_video_submissions_resolved('retry-project', {7}, manifest=writer.data)
    receipts = {r['submission_id']: r for r in VideoOperationStore().submissions(task['id'])}
    assert receipts['original-submission'] == old
    assert receipts['retry-submission']['confirmed'] is True


def test_retry_warning_reports_project_slot_and_retains_latest_receipt(tmp_path):
    bridge, writer, task = bridge_for_slot(tmp_path)
    bridge(0, 'request_resolved', submission('failed-submission', submission_pending=False))
    bridge(0, 'video_warning', {
        'code': 'flow2api_auto_retry', 'index': 0, 'retry': 1, 'max_retries': 2,
        'message': 'Flow2API 视频生成失败，正在直接重试（1/2）',
    })
    stage, warning = task['events'][-1]
    assert stage == 'video_warning'
    assert warning['index'] == 7 and warning['current'] == 1 and warning['total'] == 1
    assert warning['retry'] == 1 and warning['max_retries'] == 2
    assert writer.data['videos'][0]['last_attempt']['submission_id'] == 'failed-submission'


def test_native_submission_diagnostic_survives_failure_and_remains_with_original_attempt(tmp_path):
    bridge, writer, task = bridge_for_slot(tmp_path)
    diagnostic = {'stage': 'native_submit', 'failure_kind': 'timeout',
                  'forwarding_started': False, 'page_closed': True, 'submit_attempts': 2}
    bridge(0, 'request_resolved', submission('failed-native', submission_pending=False,
                                           native_submission_diagnostic=diagnostic))
    assert writer.data['videos'][0]['last_attempt']['native_submission_diagnostic'] == diagnostic
    VideoOperationStore().record_submission(task['id'], submission('failed-native', slot=7,
                                                                  submission_pending=False))
    bridge(0, 'request_submitting', submission('new-native-attempt'))
    assert 'native_submission_diagnostic' not in writer.data['videos'][0]['last_attempt']
    receipts = {r['submission_id']: r for r in VideoOperationStore().submissions(task['id'])}
    assert receipts['failed-native']['native_submission_diagnostic'] == diagnostic
    assert 'native_submission_diagnostic' not in receipts['new-native-attempt']


def test_direct_retry_preserves_unresolved_original_receipt(tmp_path):
    bridge, writer, task = bridge_for_slot(tmp_path)
    original = submission('unresolved-original', operation_id='original-operation',
                          upstream_account_id='original-account')
    bridge(0, 'request_submitting', original)
    bridge(0, 'video_warning', {'code': 'flow2api_auto_retry', 'index': 0, 'retry': 1})
    bridge(0, 'request_submitting', submission('direct-retry'))

    latest = writer.data['videos'][0]['last_attempt']
    assert latest['submission_id'] == 'direct-retry'
    assert latest['submission_pending'] is True and latest['confirmed'] is False
    assert 'operation_id' not in latest and 'upstream_account_id' not in latest
    receipts = {row['submission_id']: row for row in VideoOperationStore().submissions(task['id'])}
    assert set(receipts) == {'unresolved-original', 'direct-retry'}
    assert receipts['unresolved-original']['operation_id'] == 'original-operation'
    assert all(row['submission_pending'] is True and row['confirmed'] is False for row in receipts.values())
    vg.ensure_video_submissions_resolved('retry-project', {7}, manifest=writer.data)
    assert all(row['submission_pending'] is True for row in VideoOperationStore().submissions(task['id']))


def test_cancel_during_retry_warning_keeps_original_submission_settled(tmp_path):
    bridge, writer, task = bridge_for_slot(tmp_path)
    bridge(0, 'request_resolved', submission('failed-submission', submission_pending=False))
    task['cancel_event'].set()
    with pytest.raises(ConnectionError):
        bridge(0, 'video_warning', {'code': 'flow2api_auto_retry', 'index': 0, 'retry': 1})
    receipt, = VideoOperationStore().submissions(task['id'])
    assert receipt['submission_id'] == 'failed-submission'
    assert receipt['submission_pending'] is False
    assert writer.data['videos'][0]['last_attempt']['submission_pending'] is False
    vg.ensure_video_submissions_resolved('retry-project', {7}, manifest=writer.data)
