"""Retries publish only accepted replacements and keep durable attempt identity."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import video_generator as vg


@pytest.fixture
def attempt(tmp_path):
    videos = tmp_path / 'videos'
    videos.mkdir()
    dest = videos / 'vid_001.mp4'
    dest.write_bytes(b'previous accepted video')
    data = {'videos': [{'slot': 1, 'status': 'success', 'file': str(dest),
                        'url': '/old.mp4', 'model': 'old-model'}],
            'google_fx_project_url': 'frame-project', 'google_fx_project_account_id': 'frames'}
    writer = vg._ManifestWriter(str(tmp_path / 'manifest.json'), data, [1])
    plan = {'slot': 1, 'seq': 1, 'dest_path': str(dest), 'prompt': 'motion',
            'start_frame': None, 'end_frame': None, 'meta': ''}
    writer.record(vg._video_info(plan, 'new-model', status='running'))
    events = []
    bridge = vg._BatchBridge([{'plan': plan, 'req': SimpleNamespace(prompt='motion')}],
                             1, 'new-model', writer, lambda s, d: events.append((s, d)))
    source = tmp_path / 'download.mp4'
    source.write_bytes(b'new candidate video')
    return SimpleNamespace(root=tmp_path, dest=dest, data=data, writer=writer,
                           plan=plan, bridge=bridge, source=source, events=events)


@pytest.mark.parametrize('failure', ['anchor', 'process', 'upstream'])
def test_failed_retry_retains_old_media_and_marks_current_attempt(attempt, failure):
    a = attempt
    if failure == 'process':
        a.bridge.process_check_fn = lambda p: ('reject', 'irrelevant content')
    with patch.object(vg, 'verify_video_anchors', return_value=(failure != 'anchor', 'checked')):
        if failure == 'upstream':
            a.bridge(0, 'video_error', {'message': 'upload failed'})
        else:
            assert a.bridge(0, 'video_done', {'video_url': str(a.source)}) == 'rejected'
    a.writer.finish_unresolved()
    persisted = json.loads((a.root / 'manifest.json').read_text())
    entry = persisted['videos'][0]
    assert a.dest.read_bytes() == b'previous accepted video'
    assert entry['status'] == 'success' and entry['model'] == 'old-model'
    assert entry['retained_previous'] is True
    assert entry['last_attempt']['status'] == 'failed'
    assert persisted['video_generation_stats']['last_run']['failed_slots'] == [1]
    assert a.events[-1][0] == 'video_error'
    assert a.events[-1][1]['retained_previous'] is True
    assert not list(a.dest.parent.glob('.video-*'))


def test_success_checks_separate_candidate_before_atomic_publication(attempt):
    a = attempt
    def check(candidate, *_args, **_kwargs):
        assert candidate != str(a.dest)
        assert a.dest.read_bytes() == b'previous accepted video'
        assert open(candidate, 'rb').read() == b'new candidate video'
        return True, 'verified'
    with patch.object(vg, 'verify_video_anchors', side_effect=check):
        a.bridge(0, 'video_done', {'video_url': str(a.source), 'account_id': 'switched',
                                 'flow_project_url': 'switched-project'})
    assert a.dest.read_bytes() == b'new candidate video'
    entry = a.data['videos'][0]
    assert entry['last_attempt']['status'] == 'success'
    assert not entry.get('retained_previous')
    assert a.data['google_fx_projects'] == {'switched': 'switched-project'}
    assert a.data['google_fx_project_url'] == 'frame-project'
    assert a.data['google_fx_project_account_id'] == 'frames'


def test_cancel_before_publication_retains_previous_file(attempt):
    a = attempt
    a.bridge.cancel_check = lambda: True
    with patch.object(vg, 'verify_video_anchors', return_value=(True, 'ok')):
        with pytest.raises(ConnectionError):
            a.bridge(0, 'video_done', {'video_url': str(a.source)})
    a.writer.finish_unresolved('cancelled', 'user cancelled')
    assert a.dest.read_bytes() == b'previous accepted video'
    assert a.data['videos'][0]['last_attempt']['status'] == 'cancelled'
    assert a.data['video_generation_stats']['last_run']['cancelled_slots'] == [1]


def test_manifest_checkpoint_failure_rolls_back_file_and_record(attempt):
    a = attempt
    with patch.object(vg, 'verify_video_anchors', return_value=(True, 'ok')), \
         patch.object(vg, 'write_json_atomic', side_effect=OSError('disk full')):
        with pytest.raises(OSError, match='disk full'):
            a.bridge(0, 'video_done', {'video_url': str(a.source)})
    assert a.dest.read_bytes() == b'previous accepted video'
    assert a.data['videos'][0]['model'] == 'old-model'
    assert a.writer.attempt_outcomes[1]['status'] == 'running'
    assert not list(a.dest.parent.glob('.video-*'))


def test_submission_identity_and_pending_state_survive_error(attempt):
    a = attempt
    details = dict(account_id='account-b', project_url='canvas-b', tile_id='tile',
                   media_id='media', prompt_hash='hash', start_uuid='start', end_uuid='end',
                   refs=['start', 'end'], confirmed=False, submission_pending=True)
    a.bridge(0, 'request_submitted', details)
    emitted = a.events[-1][1]
    assert {k: emitted[k] for k in details} == details
    a.bridge(0, 'video_error', {'message': 'identity unresolved', 'submission_pending': True})
    a.writer.finish_unresolved()
    last = a.data['videos'][0]['last_attempt']
    assert {k: last[k] for k in details} == details
    assert last['status'] == 'failed'
    assert a.data['video_generation_stats']['last_run']['attempt_id'] == last['id']


def test_credit_failure_is_attributed_to_result_account_after_switch(attempt):
    a = attempt
    exhausted = []
    a.bridge.account_pool = SimpleNamespace(mark_exhausted=exhausted.append)
    a.bridge.pool_account_id = 'original-account'
    helpers = SimpleNamespace(is_credit_exhausted_message=lambda _: True)
    with patch.object(vg, '_get_credit_helpers', return_value=helpers):
        a.bridge(0, 'video_error', {'message': 'insufficient credits', 'account_id': 'switched-account'})
    assert exhausted == ['switched-account']
    assert a.data['videos'][0]['last_attempt']['account_id'] == 'switched-account'


def test_account_canvas_mapping_never_uses_other_recorded_owner():
    data = {'google_fx_project_url': 'frame-canvas', 'google_fx_project_account_id': 'a',
            'google_fx_projects': {'b': 'video-canvas-b'}}
    assert vg._video_project_for_account(data, 'a') == 'frame-canvas'
    assert vg._video_project_for_account(data, 'b') == 'video-canvas-b'
    assert vg._video_project_for_account(data, 'c') is None


@pytest.fixture
def generator_project(attempt, monkeypatch):
    from integrations.google_fx import models
    a = attempt
    a.writer.save()
    monkeypatch.setattr(vg, '_get_project_dir', lambda _: str(a.root))
    monkeypatch.setattr(vg, 'load_slot_frames', lambda *_: ({1: 'first', 2: 'last'}, {}))
    monkeypatch.setattr(vg, 'plan_video_slots', lambda *_args, **_kwargs: [
        dict(a.plan, action='generate', start_frame='first', end_frame='last')])
    monkeypatch.setattr(vg, '_get_account_pool_service', lambda: SimpleNamespace(mark_login_required=lambda _: None))
    monkeypatch.setattr(vg, '_select_pool_account', lambda *_: 'account-a')
    monkeypatch.setattr(vg, '_account_rotation_ring', lambda *_: ['account-a', 'account-b'])
    monkeypatch.setattr(vg, '_next_unused_account', lambda *_: 'account-b')
    monkeypatch.setattr(vg, 'apply_google_fx_runtime_overrides', lambda *_: None)
    a.models = models
    return a


def test_early_account_failure_preserves_existing_result(generator_project, monkeypatch):
    a = generator_project
    def unavailable(*_args):
        raise RuntimeError('no available account')
    monkeypatch.setattr(vg, '_select_pool_account', unavailable)
    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (object(), a.models))
    with pytest.raises(RuntimeError, match='no available account'):
        vg.generate_video_sequence({}, 'title', '视频 1:\nmove', target_slots=[1])
    data = json.loads((a.root / 'manifest.json').read_text())
    assert a.dest.read_bytes() == b'previous accepted video'
    assert data['videos'][0]['status'] == 'success'
    assert data['videos'][0]['last_attempt']['status'] == 'failed'
    assert data['video_generation_stats']['last_run']['failed_slots'] == [1]


def test_unconfirmed_paid_submission_is_not_resubmitted_on_login_switch(generator_project, monkeypatch):
    a = generator_project
    requests = []
    def generate(reqs, on_progress, cancel_check):
        requests.extend(reqs)
        on_progress(0, 'request_submitted', {'submission_pending': True, 'account_id': 'account-a',
                                            'project_url': 'video-a', 'tile_id': 'uncertain-tile'})
        on_progress(0, 'login_required_timeout', {'message': 'login expired'})
        on_progress(0, 'video_error', {'message': 'completion unknown', 'submission_pending': True})
        return []
    service = SimpleNamespace(generate_videos_batch_google_fx=generate)
    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (service, a.models))
    result = vg.generate_video_sequence({}, 'title', '视频 1:\nmove', target_slots=[1])
    assert len(requests) == 1
    assert result['videos'][0]['last_attempt']['submission_pending'] is True
    assert result['video_generation_stats']['last_run']['failed_slots'] == [1]


def test_flow_unusual_activity_stops_preplanned_next_account_leg(generator_project, monkeypatch):
    a = generator_project
    second = dict(a.plan, slot=2, seq=2,
                  dest_path=str(a.root / 'videos' / 'vid_002.mp4'),
                  start_frame='first', end_frame='last')
    monkeypatch.setattr(vg, 'plan_video_slots', lambda *_args, **_kwargs: [
        dict(a.plan, action='generate', start_frame='first', end_frame='last'),
        dict(second, action='generate')])
    monkeypatch.setattr(vg, 'plan_generation_legs', lambda items, ring, interval: [
        {'user_id': 'account-a', 'items': items[:1]},
        {'user_id': 'account-b', 'items': items[1:]},
    ])
    monkeypatch.setattr(vg, 'revalidate_leg_account', lambda *args: pytest.fail(
        'Flow restriction must stop before another account is considered'))
    calls, events = [], []

    def generate(reqs, on_progress, cancel_check):
        calls.append(len(reqs))
        on_progress(0, 'video_warning', {
            'code': 'flow_unusual_activity',
            'message': 'Flow 平台异常活动限制，停止本批次自动提交',
        })
        on_progress(0, 'video_error', {'message': 'We noticed some unusual activity.'})
        return []

    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (
        SimpleNamespace(generate_videos_batch_google_fx=generate), a.models))
    result = vg.generate_video_sequence({}, 'title', '视频 1:\nmove',
                                        on_progress=lambda stage, data: events.append((stage, data)),
                                        target_slots=[1, 2])
    assert calls == [1]
    assert a.dest.read_bytes() == b'previous accepted video'
    assert result['video_generation_stats']['last_run']['failed_slots'] == [1, 2]
    assert any(stage == 'video_warning' and data.get('code') == 'flow_unusual_activity'
               for stage, data in events)


def test_selected_account_canvas_is_used_and_switched_result_is_archived_separately(generator_project, monkeypatch):
    a = generator_project
    data = json.loads((a.root / 'manifest.json').read_text())
    data['google_fx_projects'] = {'account-a': 'video-a'}
    (a.root / 'manifest.json').write_text(json.dumps(data))
    requests = []
    def generate(reqs, on_progress, cancel_check):
        requests.extend(reqs)
        return [{'account_id': 'account-b', 'project_url': 'video-b', 'status': 'failed'}]
    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (
        SimpleNamespace(generate_videos_batch_google_fx=generate), a.models))
    result = vg.generate_video_sequence({}, 'title', '视频 1:\nmove', target_slots=[1])
    assert requests[0].project_url == 'video-a'
    assert result['google_fx_projects'] == {'account-a': 'video-a', 'account-b': 'video-b'}
    assert result['google_fx_project_url'] == 'frame-project'
    assert result['google_fx_project_account_id'] == 'frames'


@pytest.mark.parametrize('explicit_slots', [None, [1, 2]])
def test_chain_auto_merge_can_be_deferred_and_multi_slot_retry_regenerates(tmp_path, explicit_slots):
    from integrations.google_fx import models
    calls = []
    if explicit_slots:
        (tmp_path / 'videos').mkdir()
        (tmp_path / 'frames').mkdir()
        (tmp_path / 'frames' / 'img_002.webp').write_bytes(b'previous frame')
        entries = []
        for slot in explicit_slots:
            path = tmp_path / 'videos' / f'vid_{slot:03d}.mp4'
            path.write_bytes(b'old video')
            entries.append(dict(slot=slot, status='success', file=str(path)))
        (tmp_path / 'manifest.json').write_text(json.dumps({'videos': entries}))
    def generate(reqs, on_progress, cancel_check):
        calls.extend(reqs)
        source = tmp_path / 'download.mp4'
        source.write_bytes(b'new video')
        on_progress(0, 'video_done', {'video_url': str(source)})
        return []
    service = SimpleNamespace(generate_videos_batch_google_fx=generate)
    with patch.object(vg, '_get_project_dir', return_value=str(tmp_path)), \
         patch.object(vg, '_get_google_fx_video_service', return_value=(service, models)), \
         patch.object(vg, '_get_account_pool_service', return_value=object()), \
         patch.object(vg, '_select_pool_account', return_value=None), \
         patch.object(vg, 'apply_google_fx_runtime_overrides'), \
         patch.object(vg, 'verify_video_anchors', return_value=(True, 'ok')), \
         patch.object(vg, 'check_video_process', return_value=('accept', 'ok')), \
         patch.object(vg, '_extract_video_frame', return_value=False), \
         patch.object(vg, 'generate_video_collage'), \
         patch.object(vg, 'merge_project_videos') as merge:
        prompt = '视频 1:\nmovement' + ('\n\n视频 2:\nsecond movement' if explicit_slots else '')
        result = vg.generate_video_chain_sequence({}, 'title', prompt, target_slots=explicit_slots, auto_merge=False)
    merge.assert_not_called()
    assert len(calls) == (2 if explicit_slots else 1)
    assert result['videos'][0]['last_attempt']['status'] == 'success'
