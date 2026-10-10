"""Explicit video account pins must never fall back or leak into image/default state."""
import copy
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import server
import server_common as common
import video_generator as vg
from integrations.google_fx import models
from integrations.google_fx.utils import account_binding, lease_registry
from test_flow2api_video_routing import routed_project, run_pipeline
from video_operations import VideoOperationStore


class Pool:
    def __init__(self, account=None):
        self.account = account or {'user_id': 'only-profile', 'credit': 100, 'disabled': False}
        self.pick_account = Mock(side_effect=AssertionError('fixed task must never select another profile'))
        self.pick_open_account = Mock(side_effect=AssertionError('fixed task must never inspect another browser'))

    def list_accounts(self, heal=False):
        assert heal is False
        return [dict(self.account), {'user_id': 'other-profile', 'credit': 1000}]


@pytest.mark.parametrize('change', [
    {'disabled': True},
    {'cooldown_until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    {'credit': 0}, {'credit': None},
])
def test_unavailable_fixed_profile_stops_without_pool_fallback(monkeypatch, change):
    pool = Pool({'user_id': 'only-profile', 'credit': 100, **change})
    claim = Mock(return_value=True)
    monkeypatch.setattr(lease_registry, 'claim', claim)
    with pytest.raises(RuntimeError, match='未切换其他环境'):
        common._select_pool_account({'videoFixedUserId': 'only-profile'}, pool)
    pool.pick_account.assert_not_called()
    pool.pick_open_account.assert_not_called()
    claim.assert_not_called()


def test_fixed_profile_lease_conflict_is_not_an_excuse_to_select_another(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(lease_registry, 'claim', lambda _: False)
    with pytest.raises(RuntimeError, match='占用'):
        common._select_pool_account({'videoFixedUserId': 'only-profile'}, pool)
    pool.pick_account.assert_not_called()


def test_fixed_selection_overrides_old_soft_default_without_mutating_it(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(lease_registry, 'claim', lambda _: True)
    config = {'videoFixedUserId': 'only-profile', 'googleFxUserId': 'other-profile',
              'googleFxSequenceUserId': 'other-profile', 'googleFxSequenceUserLock': True}
    before = copy.deepcopy(common.SERVER_CONFIG)
    assert common._select_pool_account(config, pool) == 'only-profile'
    assert config['googleFxUserId'] == 'only-profile'
    assert config['googleFxSequenceUserId'] == 'other-profile'
    assert common.SERVER_CONFIG == before


def test_generic_client_config_cannot_pin_image_or_other_task(monkeypatch):
    for managed in (False, True):
        monkeypatch.setattr(common, 'SERVER_MANAGED', managed)
        config = common.effective_config({'videoFixedUserId': 'only-profile', 'googleFxUserId': 'only-profile'})
        assert 'videoFixedUserId' not in config
        assert 'googleFxUserId' not in config


@pytest.fixture
def video_dispatch(monkeypatch):
    calls = []
    class Thread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            calls.append((self.target, self.args))
    monkeypatch.setattr(server, 'threading', SimpleNamespace(**{**vars(threading), 'Thread': Thread}))
    monkeypatch.setattr(server, 'rate_ok', lambda *_: True)
    monkeypatch.setattr(server, '_client_ip', lambda *_: 'test')
    monkeypatch.setattr(server, '_require_fx_admission', lambda *_: True)
    monkeypatch.setattr(server, 'read_manifest', lambda *_: None)
    monkeypatch.setattr(server, 'effective_config', lambda cfg: dict(cfg or {'videoProvider': 'google_fx'}))
    monkeypatch.setattr(common, '_get_account_pool_service', lambda: Pool())
    return calls


def post(body, route='/api/generate_videos'):
    request = object.__new__(server.SparkRequestHandler)
    request.path = route
    request._gate = lambda *a, **k: True
    request._read_json_body = lambda: body
    sent = []
    request._send_json = lambda payload, status=200: sent.append((payload, status))
    request.do_POST()
    return sent[0]


@pytest.mark.parametrize('route', ['/api/generate_videos', '/api/generate_video_chain'])
def test_video_endpoint_records_and_scopes_valid_fixed_profile(video_dispatch, route):
    before = copy.deepcopy(common.SERVER_CONFIG)
    response, status = post({'request_id': 'fixed-video-request', 'title': 'test-fixed',
                             'prompt_block': '视频 1:\nmove', 'target_slots': [1],
                             'videoFixedUserId': 'only-profile'}, route)
    assert status == 200
    assert len(video_dispatch) == 1
    config = video_dispatch[0][1][1]
    assert config['videoFixedUserId'] == config['googleFxUserId'] == 'only-profile'
    dimensions = server.ACTIVE_TASKS[response['task_id']]['dimensions']
    assert dimensions['video_fixed_user_id'] == dimensions['userId'] == 'only-profile'
    assert common.SERVER_CONFIG == before


@pytest.mark.parametrize('pin', ['', '../other', None, ['only-profile'], 'unknown-profile'])
def test_invalid_fixed_profile_is_rejected_before_worker(video_dispatch, pin):
    _, status = post({'request_id': 'invalid-fixed-request', 'title': 'test-fixed', 'videoFixedUserId': pin})
    assert status == 400
    assert video_dispatch == []


def test_fixed_profile_is_not_accepted_for_flow2api(video_dispatch):
    _, status = post({'request_id': 'wrong-provider-request', 'videoFixedUserId': 'only-profile',
                      'config': {'videoProvider': 'flow2api'}})
    assert status == 400
    assert video_dispatch == []


@pytest.mark.parametrize('pipeline', ['frames', 'chain'])
def test_fixed_native_pipeline_binds_only_requested_profile_without_global_changes(
        monkeypatch, routed_project, pipeline):
    seen = []
    class Service:
        def generate_videos_batch_google_fx(self, requests, on_progress, cancel_check):
            results = []
            for index, request in enumerate(requests):
                assert account_binding.current_fixed_task_account() == 'only-profile'
                assert account_binding.resolve_account() == 'only-profile'
                seen.append(request)
                path = Path(request.output_path) / 'native.mp4'
                path.write_bytes(b'native fixed-account output')
                result = {'status': 'success', 'video_url': str(path), 'account_id': 'only-profile'}
                on_progress(index, 'video_done', result)
                results.append(result)
            return results
    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (Service(), models))
    monkeypatch.setattr(vg, '_get_account_pool_service', Pool)
    monkeypatch.setattr(lease_registry, 'claim', lambda _: True)
    monkeypatch.setattr(vg, 'apply_google_fx_runtime_overrides', lambda *_: pytest.fail('must not rewrite global/image defaults'))
    monkeypatch.setattr(vg, '_account_rotation_ring', lambda *_: pytest.fail('fixed task must not build a fallback ring'))
    monkeypatch.setenv('ADSPOWER_DEFAULT_USER_ID', 'unrelated-image-profile')
    before = copy.deepcopy(common.SERVER_CONFIG)
    result = run_pipeline(pipeline, {'videoProvider': 'google_fx', 'videoFixedUserId': 'only-profile',
        'googleFxSequenceUserId': 'other-profile', 'googleFxSequenceUserLock': True,
        'videoModel': 'Omni Flash', 'videoDuration': '10', 'videoResolution': '360p',
        'imageAspectRatio': '9:16', 'qaGateLevel': 'off'})
    assert len(seen) == 2
    assert all((r.model, r.duration, r.resolution, r.ratio) == ('Omni Flash', '10', '360p', '9:16') for r in seen)
    assert all(v['status'] == 'success' for v in result['videos'])
    assert account_binding.current_fixed_task_account() is None
    assert os.environ['ADSPOWER_DEFAULT_USER_ID'] == 'unrelated-image-profile'
    assert common.SERVER_CONFIG == before


def test_fixed_chain_stops_after_runner_warning_and_retains_pending_receipt(monkeypatch, routed_project):
    calls = []
    class Service:
        def generate_videos_batch_google_fx(self, requests, on_progress, cancel_check):
            calls.extend(requests)
            receipt = {'submission_id': 'native-unresolved', 'submission_pending': True,
                       'confirmed': False, 'account_id': 'only-profile', 'fixed_video_account': True}
            on_progress(0, 'request_submitting', receipt)
            on_progress(0, 'video_warning', {'code': 'fixed_video_account_stopped', 'message': '验证拦截'})
            on_progress(0, 'video_error', {**receipt, 'message': '结果待核对'})
            return [{'status': 'failed', **receipt}]
    monkeypatch.setattr(vg, '_get_google_fx_video_service', lambda: (Service(), models))
    monkeypatch.setattr(vg, '_get_account_pool_service', Pool)
    monkeypatch.setattr(lease_registry, 'claim', lambda _: True)
    monkeypatch.setattr(vg, 'apply_google_fx_runtime_overrides', lambda *_: pytest.fail('must stay task scoped'))
    result = run_pipeline('chain', {'videoProvider': 'google_fx', 'videoFixedUserId': 'only-profile',
        'videoModel': 'Omni Flash', 'videoDuration': '10', 'videoResolution': '360p', 'qaGateLevel': 'off'})
    assert len(calls) == 1
    first, second = result['videos']
    assert first['last_attempt']['submission_pending'] is True
    assert first['last_attempt']['submission_id'] == 'native-unresolved'
    assert first['last_attempt']['fixed_video_account'] is True
    assert second['status'] == 'failed'
    assert '未切换其他环境' in second['error']
    assert not second['last_attempt'].get('submission_pending')
    assert account_binding.current_fixed_task_account() is None


@pytest.mark.parametrize('fixed', [True, False, 'true'])
def test_only_new_fixed_native_receipts_block_duplicate_submission(fixed):
    manifest = {'videos': [{'slot': 1, 'last_attempt': {
        'provider': 'google_fx', 'account_id': 'only-profile', 'submission_id': 'fixed-attempt',
        'fixed_video_account': fixed, 'submission_pending': True}}]}
    if fixed is True:
        with pytest.raises(vg.VideoSubmissionPendingError):
            vg.ensure_video_submissions_resolved('fixed-project', {1}, manifest=manifest)
    else:
        vg.ensure_video_submissions_resolved('fixed-project', {1}, manifest=manifest)


def test_fixed_receipt_resolution_updates_pending_identity_after_media_arrives():
    store = VideoOperationStore()
    receipt = {'slot': 1, 'submission_id': 'fixed-attempt', 'fixed_video_account': True,
               'account_id': 'only-profile', 'project_key': 'fixed-project',
               'provider': 'google_fx', 'submission_pending': True}
    store.record_submission('fixed-task', receipt)
    with pytest.raises(vg.VideoSubmissionPendingError):
        vg.ensure_video_submissions_resolved('fixed-project', {1}, manifest={})
    store.record_submission('fixed-task', {
        **receipt, 'tile_id': 'arrived-tile', 'media_id': 'arrived-media',
        'submission_pending': False, 'confirmed': True})
    resolved, = store.submissions('fixed-task')
    assert resolved['media_id'] == 'arrived-media'
    vg.ensure_video_submissions_resolved('fixed-project', {1}, manifest={})
