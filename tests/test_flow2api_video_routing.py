"""SPARK's real slot/manifest pipelines use the selected video service offline."""

import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

import video_generator as vg
from integrations.google_fx import models
from integrations.google_fx.utils import account_binding


PROMPT = (
    '图片 1:\nA closed box.\n\n'
    '图片 2:\nA half-open box.\n\n'
    '图片 3:\nAn open box.\n\n'
    '视频 1:\nOpen the box from IMAGE 1 to IMAGE 2 slowly.\n\n'
    '视频 2:\nContinue opening from IMAGE 2 to IMAGE 3 slowly.\n'
)
LEGACY_PROJECT = 'https://labs.google/fx/tools/flow/project/legacy-ads-project'
LEGACY_UUIDS = [f'{i:08d}-1111-4111-8111-111111111111' for i in (1, 2, 3)]


class RecordingService:
    def __init__(self, provider):
        self.provider = provider
        self.requests = []
        self.batches = []

    def generate_videos_batch_google_fx(self, requests, on_progress=None, cancel_check=None):
        self.batches.append(list(requests))
        results = []
        for index, request in enumerate(requests):
            assert callable(cancel_check)
            assert not cancel_check()
            self.requests.append(request)
            ordinal = len(self.requests)
            path = Path(request.output_path) / f'download-{ordinal}.mp4'
            path.write_bytes(f'fake-video-{ordinal}'.encode())
            result = {'status': 'success', 'video_url': str(path)}
            if self.provider == 'flow2api':
                result.update(provider='flow2api', api_model=f'flow-api-model-{ordinal}')
                receipt = {'submission_id': f'submission-{ordinal}', 'prompt_hash': f'hash-{ordinal}',
                           'confirmed': False, 'submission_pending': True}
                on_progress(index, 'request_submitting', receipt)
                on_progress(index, 'request_submitted', receipt)
                result.update(receipt, confirmed=True, submission_pending=False)
            else:
                result.update(account_id='ads-legacy', flow_project_url=LEGACY_PROJECT)
                on_progress(index, 'request_submitted', {'confirmed': True})
            assert on_progress(index, 'video_done', result) != 'rejected'
            results.append(result)
        return results


@pytest.fixture
def routed_project(monkeypatch, tmp_path):
    project = tmp_path / 'project'
    frames = project / 'frames'
    frames.mkdir(parents=True)
    manifest = {
        'google_fx_project_url': LEGACY_PROJECT,
        'google_fx_project_account_id': 'ads-legacy',
        'google_fx_projects': {'ads-legacy': LEGACY_PROJECT},
        'frames': [],
        'videos': [],
    }
    for slot, media_id in enumerate(LEGACY_UUIDS, start=1):
        frame = frames / f'img_{slot:03d}.webp'
        frame.write_bytes(f'input-frame-{slot}'.encode())
        manifest['frames'].append({
            'slot': slot, 'sequence': slot,
            'file': str(frame.relative_to(tmp_path)), 'fx_uuid': media_id,
        })
    (project / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(vg, '_BASE_DIR', str(tmp_path))
    monkeypatch.setattr(vg, '_get_project_dir', lambda title: str(project))
    monkeypatch.setattr(vg, 'stamp_manifest_capabilities', lambda *a, **k: None)
    monkeypatch.setattr(vg, 'frame_pair_contract', lambda *a, **k: ('ok', 10.0))
    monkeypatch.setattr(vg, 'verify_video_anchors', lambda *a, **k: (True, 'verified-in-test'))
    monkeypatch.setattr(vg, 'check_video_process', lambda *a, **k: ('accept', 'checked-in-test'))
    monkeypatch.setattr(vg, 'generate_video_collage', Mock(return_value=None))
    monkeypatch.setattr(vg, 'merge_project_videos', Mock(return_value=None))
    extracted = []

    def extract(video, output, position, **kwargs):
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f'{Path(video).name}:{position}'.encode())
        extracted.append((Path(video).name, target.name, position))
        return True

    monkeypatch.setattr(vg, '_extract_video_frame', extract)
    return SimpleNamespace(project=project, frames=frames, extracted=extracted)


def install_flow_service(monkeypatch):
    service = RecordingService('flow2api')
    module = ModuleType('flow2api_video')
    module.Flow2APIVideoService = Mock(return_value=service)
    monkeypatch.setitem(sys.modules, 'flow2api_video', module)

    def forbidden(*args, **kwargs):
        pytest.fail('Flow2API pipeline attempted AdsPower account/canvas/runtime access')

    for name in (
        '_get_google_fx_video_service', '_get_account_pool_service', '_select_pool_account',
        'apply_google_fx_runtime_overrides', '_video_project_for_account',
        '_get_credit_helpers', 'fx_cancel_context',
    ):
        monkeypatch.setattr(vg, name, forbidden)
    monkeypatch.setattr(account_binding, 'bound_task_account', forbidden)
    return service, module.Flow2APIVideoService


def run_pipeline(pipeline, config):
    if pipeline == 'chain':
        return vg.generate_video_chain_sequence(config, 'routing-test', PROMPT, auto_merge=False)
    return vg.generate_video_sequence(config, 'routing-test', PROMPT)


@pytest.mark.parametrize('pipeline', ['frames', 'chain'])
@pytest.mark.parametrize('duration,resolution,ratio', [('4', '720p', '16:9'), ('10', '360p', '9:16')])
def test_flow2api_preserves_requests_and_records_provider_without_adspower(
        monkeypatch, routed_project, pipeline, duration, resolution, ratio):
    service, constructor = install_flow_service(monkeypatch)
    config = {
        'videoProvider': 'flow2api', 'videoModel': 'Omni Flash', 'videoDuration': duration,
        'videoResolution': resolution, 'imageAspectRatio': ratio, 'videoRefMode': 'VIDEO_FRAMES',
        'googleFxUserId': 'must-not-bind-this-account', 'qaGateLevel': 'off',
        'flow2apiBaseUrl': 'http://127.0.0.1:38000/v1', 'flow2apiApiKey': 'test-server-key',
    }
    result = run_pipeline(pipeline, config)
    constructor.assert_called_once_with(config)
    assert len(service.requests) == 2
    assert [len(batch) for batch in service.batches] == ([2] if pipeline == 'frames' else [1, 1])
    assert config['googleFxUserId'] == 'must-not-bind-this-account'
    for request in service.requests:
        assert request.model == 'Omni Flash'
        assert request.duration == duration
        assert request.resolution == resolution
        assert request.ratio == ratio
        assert not request.project_url
        assert not request.image_uuid
        assert not request.end_image_uuid
        assert not Path(request.output_path).exists(), 'temporary download directory must be cleaned'

    first, second = service.requests
    if pipeline == 'frames':
        assert first.image == str(routed_project.frames / 'img_001.webp')
        assert first.end_image == str(routed_project.frames / 'img_002.webp')
        assert second.image == str(routed_project.frames / 'img_002.webp')
        assert second.end_image == str(routed_project.frames / 'img_003.webp')
        assert 'IMAGE 1 to IMAGE 2' in second.prompt, 'each paired request uses local anchor numbering'
    else:
        assert first.image == first.end_image == ''
        assert second.image == str(routed_project.frames / 'img_002.webp')
        assert second.end_image == ''
        assert (routed_project.frames / 'img_002.webp').read_bytes() == b'vid_001.mp4:last'
        assert ('vid_001.mp4', 'img_002.webp', 'last') in routed_project.extracted
        assert result['generation_channel'] == 'video_chain'

    persisted = json.loads((routed_project.project / 'manifest.json').read_text())
    assert persisted['google_fx_projects'] == {'ads-legacy': LEGACY_PROJECT}
    for ordinal, video in enumerate(persisted['videos'], start=1):
        assert video['slot'] == ordinal
        assert video['status'] == 'success'
        assert video['provider'] == 'flow2api'
        assert video['api_model'] == f'flow-api-model-{ordinal}'
        assert video['last_attempt']['submission_id'] == f'submission-{ordinal}'
        assert video['last_attempt']['confirmed'] is True
        assert video['last_attempt']['submission_pending'] is False
        assert video['anchor_check'] == 'verified-in-test'
        assert video['process_check'] == 'checked-in-test'
        assert (routed_project.project / 'videos' / f'vid_{ordinal:03d}.mp4').read_bytes() == f'fake-video-{ordinal}'.encode()
    assert 'test-server-key' not in json.dumps(persisted)
    if pipeline == 'frames':
        assert persisted['video_generation_stats']['last_run']['submitted_requests'] == 2
    vg.merge_project_videos.assert_not_called()


@pytest.mark.parametrize('pipeline', ['frames', 'chain'])
@pytest.mark.parametrize('provider', [None, 'google_fx'])
@pytest.mark.parametrize('retries', [0, 2])
def test_existing_google_fx_factory_and_account_context_remain_compatible(
        monkeypatch, routed_project, pipeline, provider, retries):
    service = RecordingService('google_fx')
    old_factory = Mock(return_value=(service, models))
    monkeypatch.setattr(vg, '_get_google_fx_video_service', old_factory)
    pool = Mock()
    pool_factory = Mock(return_value=pool)
    selector = Mock(return_value='ads-legacy')
    overrides = Mock()
    monkeypatch.setattr(vg, '_get_account_pool_service', pool_factory)
    monkeypatch.setattr(vg, '_select_pool_account', selector)
    monkeypatch.setattr(vg, '_account_rotation_ring', lambda *a: ['ads-legacy'])
    monkeypatch.setattr(vg, 'apply_google_fx_runtime_overrides', overrides)
    bound = []

    @contextlib.contextmanager
    def bind(account):
        bound.append(account)
        yield

    monkeypatch.setattr(account_binding, 'bound_task_account', bind)
    monkeypatch.setattr(vg, 'fx_cancel_context', lambda *a, **k: contextlib.nullcontext())
    module = ModuleType('flow2api_video')
    module.Flow2APIVideoService = Mock(side_effect=AssertionError('unexpected Flow2API construction'))
    monkeypatch.setitem(sys.modules, 'flow2api_video', module)
    config = {'videoModel': 'Omni Flash', 'videoDuration': '10', 'videoResolution': '360p',
              'qaGateLevel': 'off', 'videoRetryCount': retries}
    if provider is not None:
        config['videoProvider'] = provider
    result = run_pipeline(pipeline, config)
    old_factory.assert_called_once_with()
    pool_factory.assert_called_once_with()
    selector.assert_called_once_with(config, pool)
    assert overrides.called
    assert bound and all(account == 'ads-legacy' for account in bound)
    assert all(request.project_url == LEGACY_PROJECT for request in service.requests)
    assert all(request.retry_count == retries for request in service.requests)
    assert len(service.requests) == 2
    assert all(video['status'] == 'success' for video in result['videos'])
    module.Flow2APIVideoService.assert_not_called()
    if pipeline == 'frames':
        assert service.requests[0].image_uuid == LEGACY_UUIDS[0]
        assert service.requests[0].end_image_uuid == LEGACY_UUIDS[1]


def test_unknown_provider_is_rejected_before_any_service_creation(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(vg, '_get_google_fx_video_service', factory)
    with pytest.raises(ValueError, match='不支持的视频服务配置'):
        vg._get_video_generation_service({'videoProvider': 'unknown'})
    factory.assert_not_called()


def test_flow2api_single_anchor_hero_keeps_its_input_frame(monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)
    config = {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash',
              'videoDuration': '10', 'videoResolution': '360p', 'qaGateLevel': 'off'}
    prompt = PROMPT + '\n视频 3 [HERO]:\nMove around the completed box in IMAGE 3.\n'
    result = vg.generate_video_sequence(config, 'routing-test', prompt, target_slots=[3])
    assert len(service.requests) == 1
    assert service.requests[0].image == str(routed_project.frames / 'img_003.webp')
    assert service.requests[0].end_image == ''
    assert result['videos'][0]['is_hero'] is True
    assert result['videos'][0]['provider'] == 'flow2api'


def test_flow2api_existing_project_reuses_success_and_explicit_retry_replaces_only_target(
        monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)
    config = {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash',
              'videoDuration': '10', 'videoResolution': '360p', 'qaGateLevel': 'off'}
    first = vg.generate_video_sequence(config, 'routing-test', PROMPT)
    assert len(service.requests) == 2
    second = vg.generate_video_sequence(config, 'routing-test', PROMPT)
    assert len(service.requests) == 2, 'existing successful clips must not submit again'
    assert [item['api_model'] for item in first['videos']] == [item['api_model'] for item in second['videos']]
    retried = vg.generate_video_sequence(config, 'routing-test', PROMPT, target_slots=[2])
    assert len(service.requests) == 3
    assert service.requests[-1].image == str(routed_project.frames / 'img_002.webp')
    assert service.requests[-1].end_image == str(routed_project.frames / 'img_003.webp')
    assert retried['videos'][0]['api_model'] == 'flow-api-model-1'
    assert retried['videos'][1]['api_model'] == 'flow-api-model-3'


def test_flow2api_out_of_order_batch_completion_keeps_slot_receipts_and_progress(
        monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)

    def generate(requests, on_progress, cancel_check):
        service.batches.append(list(requests))
        results = [None] * len(requests)
        for index in reversed(range(len(requests))):
            request = requests[index]
            path = Path(request.output_path) / f'download-{index}.mp4'
            path.write_bytes(f'slot-{index + 1}'.encode())
            receipt = {'provider': 'flow2api', 'api_model': f'api-model-{index + 1}',
                       'submission_id': f'concurrent-{index + 1}',
                       'confirmed': False, 'submission_pending': True}
            on_progress(index, 'request_submitting', receipt)
            on_progress(index, 'request_submitted', receipt)
            result = {'status': 'success', 'video_url': str(path),
                      **receipt, 'confirmed': True, 'submission_pending': False}
            on_progress(index, 'video_done', result)
            results[index] = result
        return results

    monkeypatch.setattr(service, 'generate_videos_batch_google_fx', generate)
    events = []
    result = vg.generate_video_sequence(
        {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash', 'qaGateLevel': 'off',
         'flow2apiVideoConcurrency': 3},
        'routing-test', PROMPT, on_progress=lambda stage, data: events.append((stage, data)))
    assert [len(batch) for batch in service.batches] == [2]
    assert [data['index'] for stage, data in events if stage == 'video_done'] == [2, 1]
    assert [video['slot'] for video in result['videos']] == [1, 2]
    for video in result['videos']:
        slot = video['slot']
        assert video['last_attempt']['submission_id'] == f'concurrent-{slot}'
        assert video['last_attempt']['submission_pending'] is False
        assert (routed_project.project / 'videos' / f'vid_{slot:03d}.mp4').read_bytes() == f'slot-{slot}'.encode()
    stats = result['video_generation_stats']['last_run']
    assert stats['submitted_requests'] == stats['accepted_results'] == 2
    assert stats['failed_slots'] == stats['cancelled_slots'] == []


def test_flow2api_batch_cancellation_keeps_every_started_slot_pending(
        monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)

    def generate(requests, on_progress, cancel_check):
        for index in reversed(range(len(requests))):
            receipt = {'provider': 'flow2api', 'submission_id': f'concurrent-{index + 1}',
                       'confirmed': False, 'submission_pending': True}
            on_progress(index, 'request_submitting', receipt)
            on_progress(index, 'request_submitted', receipt)
        raise ConnectionError('cancel concurrent generation')

    monkeypatch.setattr(service, 'generate_videos_batch_google_fx', generate)
    with pytest.raises(ConnectionError, match='cancel concurrent'):
        vg.generate_video_sequence({'videoProvider': 'flow2api', 'videoModel': 'Omni Flash',
                                    'qaGateLevel': 'off'}, 'routing-test', PROMPT)
    persisted = json.loads((routed_project.project / 'manifest.json').read_text())
    assert [video['slot'] for video in persisted['videos']] == [1, 2]
    for video in persisted['videos']:
        assert video['status'] == 'cancelled'
        assert video['last_attempt']['submission_pending'] is True
        assert video['last_attempt']['submission_id'] == f"concurrent-{video['slot']}"
    vg.ensure_video_submissions_resolved('routing-test', {1, 2}, manifest=persisted)
    assert all(video['last_attempt']['submission_pending'] is True for video in persisted['videos'])
    assert persisted['video_generation_stats']['last_run']['cancelled_slots'] == [1, 2]


@pytest.mark.parametrize('retain_previous', [False, True])
def test_flow2api_chain_failed_predecessor_never_submits_with_existing_frames(
        monkeypatch, routed_project, retain_previous):
    service, _ = install_flow_service(monkeypatch)
    if retain_previous:
        dest = routed_project.project / 'videos' / 'vid_001.mp4'
        dest.parent.mkdir()
        dest.write_bytes(b'previous accepted video')
        manifest_path = routed_project.project / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['videos'] = [{'slot': 1, 'status': 'success', 'file': str(dest),
                               'provider': 'flow2api'}]
        manifest_path.write_text(json.dumps(manifest))

    def generate(requests, on_progress, cancel_check):
        service.requests.extend(requests)
        failure = {'status': 'failed', 'provider': 'flow2api', 'code': 'authorization',
                   'message': 'Flow2API authorization failed', 'submission_pending': False}
        on_progress(0, 'video_error', failure)
        return [failure]

    monkeypatch.setattr(service, 'generate_videos_batch_google_fx', generate)
    result = vg.generate_video_chain_sequence(
        {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash', 'qaGateLevel': 'off'},
        'routing-test', PROMPT, target_slots=[1, 2], auto_merge=False)
    assert len(service.requests) == 1
    first, second = result['videos']
    assert first['last_attempt']['status'] == 'failed'
    assert second['status'] == 'failed'
    assert '本次未成功交付' in second['error']
    assert result['video_generation_stats']['last_run']['failed_slots'] == [1, 2]
    assert (routed_project.frames / 'img_002.webp').read_bytes() == b'input-frame-2'
    if retain_previous:
        assert first['retained_previous'] is True
        assert dest.read_bytes() == b'previous accepted video'


def test_flow2api_chain_tail_extraction_failure_never_falls_back_to_old_frame(
        monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)
    monkeypatch.setattr(vg, '_extract_video_frame', lambda *a, **k: False)
    result = vg.generate_video_chain_sequence(
        {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash', 'qaGateLevel': 'off'},
        'routing-test', PROMPT, auto_merge=False)
    assert len(service.requests) == 1
    assert result['videos'][0]['status'] == 'success'
    assert result['videos'][1]['status'] == 'failed'
    assert '缺少上一段视频' in result['videos'][1]['error']
    assert (routed_project.frames / 'img_002.webp').read_bytes() == b'input-frame-2'


def test_flow2api_chain_selected_slot_refreshes_old_image_from_preceding_video(
        monkeypatch, routed_project):
    service, _ = install_flow_service(monkeypatch)
    videos_dir = routed_project.project / 'videos'
    videos_dir.mkdir()
    (videos_dir / 'vid_001.mp4').write_bytes(b'previous accepted video')
    vg.generate_video_chain_sequence(
        {'videoProvider': 'flow2api', 'videoModel': 'Omni Flash', 'qaGateLevel': 'off'},
        'routing-test', PROMPT, target_slots=[2], auto_merge=False)
    assert len(service.requests) == 1
    assert service.requests[0].image == str(routed_project.frames / 'img_002.webp')
    assert (routed_project.frames / 'img_002.webp').read_bytes() == b'vid_001.mp4:last'
    assert ('vid_001.mp4', 'img_002.webp', 'last') in routed_project.extracted


@pytest.mark.parametrize('failure', [
    {'status': 'failed', 'last_attempt': {'submission_pending': True}},
    {'status': 'failed', 'last_attempt': {'submission_pending': False}},
    {'status': 'success', 'retained_previous': True,
     'last_attempt': {'status': 'failed', 'submission_pending': True}},
])
def test_autonomous_pipeline_does_not_resubmit_flow2api_failures(monkeypatch, failure):
    import pipeline_orchestrator as po
    result = {'videos': [{'slot': 1, **failure}]}
    generate = Mock(return_value=result)
    progress = Mock()
    monkeypatch.setattr(po, 'generate_video_sequence', generate)
    actual = po._render_videos_with_recovery({'videoProvider': 'flow2api'}, 'test', PROMPT,
                                            on_progress=progress)
    assert actual is result
    assert generate.call_count == 1
    assert not any(call.args[0] == 'video_retry_autonomous' for call in progress.call_args_list)


@pytest.mark.parametrize('provider', [None, 'google_fx'])
def test_autonomous_pipeline_keeps_one_provider_retry_owner(monkeypatch, provider):
    import pipeline_orchestrator as po
    failed = {'videos': [{'slot': 1, 'status': 'success'}, {'slot': 2, 'status': 'failed'},
                         {'slot': 3, 'status': 'skipped_cut'}]}
    generate = Mock(return_value=failed)
    monkeypatch.setattr(po, 'generate_video_sequence', generate)
    config = {'videoRetryCount': 5}
    if provider is not None:
        config['videoProvider'] = provider
    result = po._render_videos_with_recovery(config, 'test', PROMPT)
    assert result is failed
    assert generate.call_count == 1
    assert generate.call_args.args[0]['videoRetryCount'] == 5
    assert 'target_slots' not in generate.call_args.kwargs
