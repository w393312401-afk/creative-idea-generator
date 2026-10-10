"""Continuous recovery is cancellable and never replays accepted submissions."""

import copy
import threading
import time

import pytest

import server_common as common
import video_generation_recovery as recovery


PROMPT = '视频 1:\nfirst\n视频 2:\nsecond\n视频 3:\nthird\n'
ENABLED = {'videoContinuousGeneration': True, 'videoProvider': 'flow2api'}


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'videos').mkdir()
    common.write_manifest(str(tmp_path), {'videos': []})
    return tmp_path


def save(project, rows, **metadata):
    manifest = common.read_manifest(str(project)) or {}
    manifest.update(metadata, videos=copy.deepcopy(rows))
    common.write_manifest(str(project), manifest)
    return manifest


def success(project, slot, **kwargs):
    path = project / 'videos' / f'vid_{slot:03d}.mp4'
    path.write_bytes(f'accepted-{slot}'.encode())
    return {'slot': slot, 'sequence': slot, 'status': 'success', 'file': str(path),
            'url': f'/project/videos/vid_{slot:03d}.mp4', **kwargs}


def reconcile_unchanged(project, calls=None):
    def reconcile(project_key, slots, config, **kwargs):
        assert kwargs['latest_only'] is True
        assert 'cancel_check' in kwargs
        if calls is not None:
            calls.append(list(slots))
        return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
    return reconcile


def execute(project, generate, reconcile=None, **kwargs):
    return recovery.execute_video_generation_with_recovery(
        ENABLED, 'project', PROMPT, project_dir=str(project), generate_fn=generate,
        reconcile_fn=reconcile or reconcile_unchanged(project),
        wait_fn=kwargs.pop('wait_fn', lambda delay, cancel: None),
        target_slots=kwargs.pop('target_slots', [1, 2]), **kwargs)


@pytest.mark.parametrize('enabled', [False, None, 'true', 1])
def test_disabled_preserves_exact_single_round_semantics(project, enabled):
    calls = []
    expected = {'original-result': True}
    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        return expected
    result = recovery.execute_video_generation_with_recovery(
        {'videoContinuousGeneration': enabled}, 'project', PROMPT, generate_fn=generate,
        reconcile_fn=lambda *a, **k: pytest.fail('disabled reconciliation'),
        wait_fn=lambda *a: pytest.fail('disabled wait'), target_slots=[2],
        override_flagged=True, auto_merge=False, project_dir=str(project), recovery_only=True)
    assert result is expected
    assert len(calls) == 1
    assert calls[0][1] == {'on_progress': None, 'target_slots': [2],
                           'override_flagged': True, 'auto_merge': False}
    assert 'video_recovery' not in common.read_manifest(str(project))


def test_more_than_ten_rounds_retry_only_unfinished_targets(project):
    third = success(project, 3)
    first = success(project, 1)
    calls, waits, queries, events = [], [], [], []
    def generate(*args, target_slots, **kwargs):
        calls.append(list(target_slots))
        second = (success(project, 2) if len(calls) == 14
                  else {'slot': 2, 'status': 'failed', 'error': 'upstream timeout'})
        return save(project, [first, second, third], video_generation_stats={
            'last_run': {'failed_slots': [2], 'cancelled_slots': [2, 99], 'planned_slots': 1},
            'cumulative': {'submitted_requests': len(calls)}})
    result = recovery.execute_video_generation_with_recovery(
        {**ENABLED, 'videoMaxSlotAttempts': 20}, 'project', PROMPT, project_dir=str(project),
        generate_fn=generate, reconcile_fn=reconcile_unchanged(project, queries),
        wait_fn=lambda delay, cancel: waits.append(delay), target_slots=[1, 2],
        on_progress=lambda stage, details: events.append((stage, details)) or False)
    assert calls == [[1, 2]] + [[2]] * 13
    assert waits[:5] == [60, 120, 240, 480, 660]
    assert waits[5:] == [660] * 8
    assert all(slots == [2] for slots in queries)
    assert (project / 'videos' / 'vid_003.mp4').read_bytes() == b'accepted-3'
    assert result['video_recovery']['status'] == 'complete'
    assert result['video_recovery']['completed_slots'] == [1, 2]
    assert result['video_recovery']['retry_round'] == 13
    assert result['video_generation_stats']['last_run'] == {
        'failed_slots': [], 'cancelled_slots': [99], 'planned_slots': 2}
    assert result['video_generation_stats']['cumulative']['submitted_requests'] == 14
    persisted = common.read_manifest(str(project))
    assert persisted['video_recovery'] == result['video_recovery']
    assert persisted['video_generation_stats'] == result['video_generation_stats']
    states = [details for stage, details in events if stage == 'video_recovery']
    assert {'querying', 'waiting', 'retrying'} <= {state['phase'] for state in states}
    assert all(isinstance(state['next_retry_at'], float) for state in states
               if state['phase'] == 'waiting' and state['status'] == 'running')


def test_first_round_keeps_none_target_semantics(project):
    calls = []
    def generate(*args, target_slots, **kwargs):
        calls.append(target_slots)
        return save(project, [success(project, slot) for slot in (1, 2, 3)])
    execute(project, generate, target_slots=None)
    assert calls == [None]


def test_reconcile_recovers_completed_video_without_new_submission(project):
    calls, events = [], []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': {
            'submission_id': 'paid-original', 'submission_pending': True,
            'operation_id': 'paid-operation', 'upstream_accepted': True}}])
    def reconcile(*args, **kwargs):
        row = success(project, 1, process_warned=True, last_attempt={
            'submission_id': 'paid-original', 'submission_pending': False,
            'status': 'success', 'operation_id': 'paid-operation', 'confirmed': True})
        return {'manifest': save(project, [row], video_generation_stats={
            'last_run': {'failed_slots': [1], 'cancelled_slots': [], 'planned_slots': 1}}),
            'outcomes': [{'slot': 1, 'state': 'recovered'}]}
    result = execute(project, generate, reconcile, target_slots=[1],
                     on_progress=lambda stage, details: events.append((stage, details)) or False,
                     wait_fn=lambda *a: pytest.fail('already recoverable needs no wait'))
    assert calls == [[1]]
    done, = [details for stage, details in events if stage == 'video_done']
    assert done['index'] == done['current'] == done['total'] == 1
    assert done['video']['process_warned'] is True
    assert done['video']['last_attempt']['submission_id'] == 'paid-original'
    assert result['video_generation_stats']['last_run']['failed_slots'] == []


def test_pending_unknown_retains_original_receipt_when_new_attempt_succeeds(project):
    original = {'submission_id': 'original', 'submission_pending': True,
                'upstream_accepted': None, 'provider': 'flow2api'}
    ledger = [copy.deepcopy(original)]
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        if len(calls) == 1:
            return save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': copy.deepcopy(original)}])
        return save(project, [success(project, 1, last_attempt={
            'submission_id': 'new-submission', 'submission_pending': False, 'status': 'success'})])
    result = execute(project, generate, target_slots=[1], retry_delays=(0,))
    assert calls == [[1], [1]]
    assert ledger == [original] and ledger[0]['submission_pending'] is True
    assert result['videos'][0]['last_attempt']['submission_id'] == 'new-submission'


@pytest.mark.parametrize('receipt,config', [
    ({'operation_id': 'paid-operation', 'submission_pending': True}, ENABLED),
    ({'upstream_task_id': 'paid-task', 'submission_pending': True}, ENABLED),
    ({'upstream_accepted': True, 'submission_pending': True}, ENABLED),
    ({'operation_id': 'paid-operation', 'submission_pending': False,
      'confirmed': True, 'recovery_state': 'recovery_failed'}, ENABLED),
    ({'submission_pending': True, 'fixed_video_account': True, 'provider': 'google_fx'},
     {**ENABLED, 'videoProvider': 'google_fx'}),
])
def test_accepted_or_native_pending_only_queries_until_recovered(project, receipt, config):
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': receipt}])
    queries, waits = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) < 3:
            return {'manifest': common.read_manifest(str(project)), 'outcomes': [
                {'slot': 1, 'state': 'pending' if receipt.get('submission_pending') else 'recovery_failed'}]}
        return {'manifest': save(project, [success(project, 1)]), 'outcomes': [
            {'slot': 1, 'state': 'recovered'}]}
    result = recovery.execute_video_generation_with_recovery(
        config, 'project', PROMPT, project_dir=str(project), target_slots=[1],
        recovery_only=True, generate_fn=lambda *a, **k: pytest.fail('must not replay accepted submission'),
        reconcile_fn=reconcile, wait_fn=lambda delay, cancel: waits.append(delay))
    assert queries == [[1], [1], [1]]
    assert waits == [60, 120]
    assert result['video_recovery']['status'] == 'complete'


def test_recheck_after_wait_prevents_late_original_duplicate(project):
    calls, queries = [], []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': {
            'submission_pending': True, 'provider': 'flow2api', 'submission_id': 'original'}}])
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) == 1:
            return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
        return {'manifest': save(project, [success(project, 1)]), 'outcomes': []}
    execute(project, generate, reconcile, target_slots=[1], retry_delays=(0,))
    assert calls == [[1]] and len(queries) == 2


@pytest.mark.parametrize('receipt', [
    {'operation_id': 'paid-operation', 'submission_pending': True},
    {'upstream_task_id': 'paid-task', 'submission_pending': True},
    {'upstream_accepted': True, 'submission_pending': True},
    {'operation_id': 'paid-operation', 'submission_pending': False, 'confirmed': True,
     'recovery_state': 'recovery_failed'},
])
def test_existing_accepted_submission_is_protected_in_first_round(project, receipt):
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': receipt}])
    queries = []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) == 1:
            return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
        return {'manifest': save(project, [success(project, 1)]), 'outcomes': []}
    result = execute(project, lambda *a, **k: pytest.fail('first round must protect accepted task'),
                     reconcile, target_slots=[1], retry_delays=(0,))
    assert queries == [[1], [1]]
    assert result['video_recovery']['status'] == 'complete'


def test_first_round_keeps_explicit_delivered_regeneration_alongside_pending_slot(project):
    delivered = success(project, 1, last_attempt={'status': 'success', 'confirmed': True,
                                                'submission_pending': False, 'operation_id': 'old-done'})
    pending = {'slot': 2, 'status': 'failed', 'last_attempt': {
        'operation_id': 'still-paid', 'submission_pending': True}}
    save(project, [delivered, pending])
    calls, queries = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) == 1:
            return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
        return {'manifest': save(project, [delivered, success(project, 2)]), 'outcomes': []}
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return common.read_manifest(str(project))
    result = execute(project, generate, reconcile, retry_delays=(0,))
    assert calls == [[1]], 'pending task cannot suppress a requested delivered-slot regeneration'
    assert queries == [[2], [2]]
    assert result['video_recovery']['completed_slots'] == [1, 2]


def test_whole_order_with_pending_slot_reuses_delivered_videos_after_cancelled_attempt(project):
    """整单（target_slots=None）遇到待确认槽位时，已交付视频不得被当作显式重试重新提交。"""
    cancelled = {'status': 'cancelled', 'submission_pending': False}
    delivered = [success(project, 1, retained_previous=True, last_attempt=dict(cancelled)),
                 success(project, 2, last_attempt={'status': 'success', 'confirmed': True,
                                                   'submission_pending': False})]
    pending = {'slot': 3, 'status': 'failed', 'last_attempt': {
        'operation_id': 'still-paid', 'submission_pending': True, 'upstream_accepted': True}}
    save(project, delivered + [pending])
    calls, queries = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return common.read_manifest(str(project))
    def wait(delay, cancel):
        raise ConnectionError('视频生成已取消，已保留成功视频')
    with pytest.raises(ConnectionError):
        execute(project, generate, reconcile, target_slots=None, wait_fn=wait, retry_delays=(0,))
    assert calls == [], 'delivered slots 1-2 and pending slot 3 must not be submitted'
    assert queries[0] == [3]


def test_chain_recovers_pending_predecessor_before_submitting_downstream(project):
    previous = success(project, 1, retained_previous=True, last_attempt={
        'status': 'failed', 'operation_id': 'paid-first', 'submission_pending': True,
        'upstream_accepted': True})
    save(project, [previous, {'slot': 2, 'status': 'failed'}])
    calls, queries = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) == 3:
            recovered = success(project, 1, last_attempt={
                'status': 'success', 'operation_id': 'paid-first',
                'submission_pending': False, 'confirmed': True})
            (project / 'videos' / 'vid_001.mp4').write_bytes(b'new-paid-predecessor')
            return {'manifest': save(project, [recovered, {'slot': 2, 'status': 'failed'}]),
                    'outcomes': [{'slot': 1, 'state': 'recovered'}]}
        return {'manifest': common.read_manifest(str(project)), 'outcomes': []}
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        manifest = common.read_manifest(str(project))
        assert manifest['videos'][0]['last_attempt']['submission_pending'] is False
        assert (project / 'videos' / 'vid_001.mp4').read_bytes() == b'new-paid-predecessor'
        return save(project, [manifest['videos'][0], success(project, 2)])
    result = execute(project, generate, reconcile, is_chain=True, retry_delays=(0,))
    assert calls == [[2]]
    assert result['video_recovery']['completed_slots'] == [1, 2]


def test_chain_recovers_target_external_predecessor_without_generating_it(project):
    previous = success(project, 1, retained_previous=True, last_attempt={
        'status': 'failed', 'operation_id': 'paid-external-first', 'submission_pending': True})
    save(project, [previous, {'slot': 2, 'status': 'failed'}])
    calls, queries = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        recovered = success(project, 1, last_attempt={'status': 'success', 'confirmed': True,
                                                     'submission_pending': False})
        return {'manifest': save(project, [recovered, {'slot': 2, 'status': 'failed'}]),
                'outcomes': [{'slot': 1, 'state': 'recovered'}]}
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        first = common.read_manifest(str(project))['videos'][0]
        assert first['last_attempt']['status'] == 'success'
        return save(project, [first, success(project, 2)])
    result = execute(project, generate, reconcile, target_slots=[2], is_chain=True,
                     retry_delays=(0,))
    assert queries == [[1]] and calls == [[2]]
    assert result['video_recovery']['completed_slots'] == [2]


def test_chain_recovery_never_skips_an_unfinished_predecessor(project):
    save(project, [{'slot': 1, 'status': 'failed'}, {'slot': 2, 'status': 'failed'},
                   {'slot': 3, 'status': 'failed'}])
    calls = []
    def generate(*args, **kwargs):
        slots = kwargs['target_slots']
        calls.append(slots)
        rows = common.read_manifest(str(project))['videos']
        rows[slots[0] - 1] = success(project, slots[0])
        return save(project, rows)
    result = execute(project, generate, target_slots=None, is_chain=True,
                     recovery_only=True, retry_delays=(0,))
    assert calls == [[1], [2], [3]]
    assert result['video_recovery']['completed_slots'] == [1, 2, 3]


def test_chain_missing_external_predecessor_waits_without_submitting_descendant(project):
    save(project, [{'slot': 1, 'status': 'failed'}, {'slot': 2, 'status': 'failed'}])
    cancelled, queries = threading.Event(), []
    def wait(*args):
        cancelled.set()
    with pytest.raises(ConnectionError):
        execute(project, lambda *a, **k: pytest.fail('cannot use unfinished external predecessor'),
                reconcile_unchanged(project, queries), target_slots=[2], is_chain=True,
                cancel_check=cancelled.is_set, wait_fn=wait, retry_delays=(0,))
    assert all(1 in slots for slots in queries)


def test_first_pass_only_protects_pending_and_generates_other_ready_slots_without_wait(project):
    pending = {'slot': 1, 'status': 'failed', 'last_attempt': {
        'operation_id': 'already-paid', 'submission_pending': True, 'upstream_accepted': True}}
    save(project, [pending, {'slot': 2, 'status': 'failed'}])
    calls, queries = [], []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [pending, success(project, 2)])
    result = execute(project, generate, reconcile_unchanged(project, queries),
                     first_pass_only=True, wait_fn=lambda *a: pytest.fail('ready queue must not wait'))
    assert queries == [[1]] and calls == [[2]]
    assert result['video_recovery']['status'] == 'running'
    assert result['video_recovery']['slots'] == [1]
    assert result['video_recovery']['completed_slots'] == [2]
    assert result['videos'][0]['last_attempt'] == pending['last_attempt']


def test_silent_initial_progress_write_failure_stops_before_generation(project, monkeypatch):
    monkeypatch.setattr(common, 'write_manifest', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='恢复进度保存失败'):
        execute(project, lambda *a, **k: pytest.fail('missing durable progress must block generation'))


def test_silent_recovery_progress_write_failure_blocks_additional_submission(project, monkeypatch):
    original_write = common.write_manifest
    calls = []
    def fail_recovery_write(directory, data):
        if (data.get('video_recovery') or {}).get('retry_round', 0) > 0:
            return
        original_write(directory, data)
    monkeypatch.setattr(common, 'write_manifest', fail_recovery_write)
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': {
            'submission_id': 'original', 'submission_pending': True, 'provider': 'flow2api'}}])
    with pytest.raises(RuntimeError, match='恢复进度保存失败'):
        execute(project, generate, target_slots=[1], retry_delays=(0,))
    assert calls == [[1]]
    assert common.read_manifest(str(project))['videos'][0]['last_attempt']['submission_pending'] is True


def test_cancel_during_wait_preserves_success_and_pending(project):
    cancelled = threading.Event()
    original = {'submission_id': 'original', 'submission_pending': True}
    def generate(*args, **kwargs):
        return save(project, [success(project, 1), {'slot': 2, 'status': 'failed', 'last_attempt': original}])
    def wait(delay, cancel_check):
        cancelled.set()
        recovery.wait_for_video_recovery(delay, cancel_check)
    with pytest.raises(ConnectionError, match='已取消'):
        execute(project, generate, wait_fn=wait, cancel_check=cancelled.is_set)
    manifest = common.read_manifest(str(project))
    assert manifest['video_recovery']['status'] == 'cancelled'
    assert manifest['video_recovery']['completed_slots'] == [1]
    assert manifest['videos'][1]['last_attempt'] == original
    assert (project / 'videos' / 'vid_001.mp4').read_bytes() == b'accepted-1'


def test_default_wait_observes_cancellation_without_waiting_full_delay():
    event = threading.Event()
    timer = threading.Timer(0.03, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionError):
            recovery.wait_for_video_recovery(60, event.is_set)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize('error', [ConnectionError('network disconnected'), TimeoutError('provider timeout')])
def test_transient_round_exception_retries_when_not_cancelled(project, error):
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        if len(calls) == 1:
            raise error
        return save(project, [success(project, 1)])
    result = execute(project, generate, target_slots=[1], cancel_check=lambda: False,
                     retry_delays=(0,))
    assert calls == [[1], [1]] and result['video_recovery']['status'] == 'complete'


def test_network_query_error_does_not_cancel_or_replay_accepted_operation(project):
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': {
        'operation_id': 'paid-operation', 'submission_pending': True}}])
    queries = []
    def reconcile(*args, **kwargs):
        queries.append(1)
        if len(queries) == 1:
            raise ConnectionError('query disconnected')
        return {'manifest': save(project, [success(project, 1)]), 'outcomes': []}
    result = execute(project, lambda *a, **k: pytest.fail('accepted operation cannot replay'),
                     reconcile, target_slots=[1], recovery_only=True,
                     cancel_check=lambda: False, retry_delays=(0,))
    assert len(queries) == 2 and result['video_recovery']['status'] == 'complete'


def test_cancellation_after_partial_provider_write_records_completed_slots(project):
    event = threading.Event()
    def generate(*args, **kwargs):
        save(project, [success(project, 1), {'slot': 2, 'status': 'cancelled'}])
        event.set()
        raise ConnectionError('cancelled provider')
    with pytest.raises(ConnectionError):
        execute(project, generate, cancel_check=event.is_set)
    state = common.read_manifest(str(project))['video_recovery']
    assert state['status'] == 'cancelled' and state['completed_slots'] == [1]


def test_recovery_progress_write_keeps_concurrent_frame_updates(project):
    def generate(*args, **kwargs):
        return save(project, [success(project, 1)], frames=[{'slot': 99, 'file': 'new-frame'}],
                    cover='new-cover')
    execute(project, generate, target_slots=[1])
    latest = common.read_manifest(str(project))
    assert latest['frames'] == [{'slot': 99, 'file': 'new-frame'}]
    assert latest['cover'] == 'new-cover'


@pytest.mark.parametrize('error', [ValueError('invalid config'),
                                  RuntimeError('未找到已生成的帧图像。请先生成帧序列！')])
def test_configuration_or_missing_frames_stop_without_looping(project, error):
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        raise error
    with pytest.raises(type(error), match=str(error)):
        execute(project, generate, reconcile=lambda *a, **k: pytest.fail('no recovery of invalid inputs'))
    assert calls == [1]
    assert common.read_manifest(str(project))['video_recovery']['status'] == 'failed'


def test_missing_frame_record_stops_instead_of_repeated_paid_calls(project):
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        return save(project, [{'slot': 1, 'status': 'failed',
                               'error': '视频 1 所需的起始帧 IMAGE 1 不存在。请重新生成该帧！'}])
    with pytest.raises(recovery.VideoRecoveryBlockedError) as failure:
        execute(project, generate, target_slots=[1])
    assert calls == [1] and failure.value.slots == [1]


@pytest.mark.parametrize('artifact', ['missing', 'empty', 'retained_failed', 'pending'])
def test_recovery_only_does_not_accept_undelivered_or_retained_success(project, artifact):
    row = success(project, 1)
    if artifact == 'missing':
        (project / 'videos' / 'vid_001.mp4').unlink()
    elif artifact == 'empty':
        (project / 'videos' / 'vid_001.mp4').write_bytes(b'')
    elif artifact == 'retained_failed':
        row.update(retained_previous=True, last_attempt={'status': 'failed'})
    else:
        row['last_attempt'] = {'status': 'success', 'submission_pending': True,
                               'provider': 'flow2api'}
    save(project, [row])
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [success(project, 1, last_attempt={'status': 'success'})])
    result = execute(project, generate, target_slots=[1], recovery_only=True, retry_delays=(0,))
    assert calls == [[1]] and result['video_recovery']['status'] == 'complete'


def test_recovery_only_keeps_delivered_targets_and_skipped_slots(project):
    preserved = success(project, 1)
    save(project, [preserved, {'slot': 2, 'status': 'failed'}, {'slot': 3, 'status': 'skipped_cut'}])
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [preserved, success(project, 2), {'slot': 3, 'status': 'skipped_cut'}])
    result = execute(project, generate, target_slots=None, recovery_only=True, retry_delays=(0,))
    assert calls == [[2]]
    assert result['video_recovery']['completed_slots'] == [1, 2, 3]


def test_chain_generator_keywords_are_forwarded_unchanged(project):
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        return save(project, [success(project, 1)])
    execute(project, generate, target_slots=[1], is_chain=True, auto_merge=False,
            override_flagged=True)
    assert calls[0]['auto_merge'] is False
    assert calls[0]['override_flagged'] is True


def _held_flow2api(started_at):
    return {'operation_id': 'stuck-operation', 'submission_pending': True,
            'upstream_accepted': True, 'provider': 'flow2api', 'submission_id': 'stuck',
            'upstream_error_code': 'video_status_unknown', 'started_at': started_at}


def test_flow2api_accepted_submission_pending_past_timeout_is_resubmitted(project):
    from datetime import datetime, timedelta, timezone
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1900)).isoformat()
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': _held_flow2api(stale)}])
    calls, queries = [], []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        return {'manifest': common.read_manifest(str(project)),
                'outcomes': [{'slot': 1, 'state': 'pending'}]}
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [success(project, 1)])
    result = execute(project, generate, reconcile, target_slots=[1], retry_delays=(0,),
                     recovery_only=True)
    assert calls == [[1]]
    assert result['video_recovery']['status'] == 'complete'


def test_flow2api_recent_pending_submission_stays_protected(project):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': _held_flow2api(fresh)}])
    queries = []
    def reconcile(*args, **kwargs):
        queries.append(args[1])
        if len(queries) < 3:
            return {'manifest': common.read_manifest(str(project)),
                    'outcomes': [{'slot': 1, 'state': 'pending'}]}
        return {'manifest': save(project, [success(project, 1)]),
                'outcomes': [{'slot': 1, 'state': 'recovered'}]}
    result = execute(project, lambda *a, **k: pytest.fail('fresh accepted task must not replay'),
                     reconcile, target_slots=[1], retry_delays=(0,), recovery_only=True)
    assert result['video_recovery']['status'] == 'complete'


def test_flow2api_hold_without_timestamp_expires_after_observed_timeout(project):
    save(project, [{'slot': 1, 'status': 'failed', 'last_attempt': _held_flow2api(None)}])
    calls, clock = [], [1000.0]
    def wait(delay, cancel):
        clock[0] += 400
    def generate(*args, **kwargs):
        calls.append(kwargs['target_slots'])
        return save(project, [success(project, 1)])
    def reconcile(*args, **kwargs):
        return {'manifest': common.read_manifest(str(project)),
                'outcomes': [{'slot': 1, 'state': 'pending'}]}
    import unittest.mock as mock
    with mock.patch.object(recovery.time, 'monotonic', lambda: clock[0]):
        result = recovery.execute_video_generation_with_recovery(
            {**ENABLED, 'flow2apiVideoTimeoutSeconds': 1000}, 'project', PROMPT,
            project_dir=str(project), target_slots=[1], recovery_only=True,
            generate_fn=generate, reconcile_fn=reconcile, wait_fn=wait, retry_delays=(0,))
    assert calls == [[1]]
    assert result['video_recovery']['status'] == 'complete'


def _always_failing(project, calls, keep=()):
    def generate(*args, target_slots, **kwargs):
        calls.append(list(target_slots))
        rows = [success(project, slot) for slot in keep]
        rows += [{'slot': slot, 'status': 'failed', 'error': 'upstream refused',
                  'last_attempt': {'status': 'failed', 'submission_pending': False,
                                   'error': 'upstream refused'}}
                 for slot in target_slots if slot not in keep]
        return save(project, rows)
    return generate


def test_slot_failing_every_time_gives_up_after_attempt_limit(project):
    calls = []
    with pytest.raises(recovery.VideoRecoveryExhaustedError) as failure:
        recovery.execute_video_generation_with_recovery(
            {**ENABLED, 'videoMaxSlotAttempts': 3}, 'project', PROMPT, project_dir=str(project),
            generate_fn=_always_failing(project, calls), reconcile_fn=reconcile_unchanged(project),
            wait_fn=lambda delay, cancel: None, target_slots=[2])
    assert calls == [[2]] * 3
    assert failure.value.slots == [2]
    assert '已自动提交 3 次' in str(failure.value) and 'upstream refused' in str(failure.value)
    state = common.read_manifest(str(project))['video_recovery']
    assert state['status'] == 'failed' and state['slots'] == [2]


def test_attempt_budget_is_shared_through_attempt_counts(project):
    calls, counts = [], {2: 5}
    with pytest.raises(recovery.VideoRecoveryExhaustedError):
        recovery.execute_video_generation_with_recovery(
            ENABLED, 'project', PROMPT, project_dir=str(project), recovery_only=True,
            generate_fn=_always_failing(project, calls), reconcile_fn=reconcile_unchanged(project),
            wait_fn=lambda delay, cancel: None, target_slots=[2], attempt_counts=counts)
    assert calls == [[2]] and counts == {2: 6}


def test_given_up_slot_does_not_stop_other_unfinished_slots(project):
    calls, attempt = [], {'n': 0}
    def generate(*args, target_slots, **kwargs):
        calls.append(list(target_slots))
        attempt['n'] += 1
        rows = []
        for slot in (1, 2):
            if slot == 1 and attempt['n'] >= 3:
                rows.append(success(project, 1))
            else:
                rows.append({'slot': slot, 'status': 'failed', 'error': 'x',
                             'last_attempt': {'status': 'failed', 'submission_pending': False}})
        return save(project, rows)
    with pytest.raises(recovery.VideoRecoveryExhaustedError) as failure:
        recovery.execute_video_generation_with_recovery(
            {**ENABLED, 'videoMaxSlotAttempts': 4}, 'project', PROMPT, project_dir=str(project),
            generate_fn=generate, reconcile_fn=reconcile_unchanged(project),
            wait_fn=lambda delay, cancel: None, target_slots=[1, 2])
    assert failure.value.slots == [2]
    assert (project / 'videos' / 'vid_001.mp4').exists()
