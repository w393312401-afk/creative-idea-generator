"""Flow2API directly retries; unresolved fixed native attempts stay protected."""
import json
from unittest.mock import Mock

import pytest

import video_generator as vg
from video_operations import VideoOperationStore


PROMPT = ('图片 1:\nStart view.\n\n图片 2:\nEnd view.\n\n'
          '视频 1:\nFirst camera move.\n\n视频 2:\nContinue the camera move.')


def manifest_attempt(**overrides):
    return {'videos': [{'slot': 1, 'status': 'success', 'last_attempt': {
        'submission_id': 'pending-submission', 'submission_pending': True,
        **overrides}}]}


@pytest.mark.parametrize('pipeline', ['frames', 'chain', 'auto_recovery'])
@pytest.mark.parametrize('receipt_kind', ['flow2api', 'legacy_adapter', 'fixed_native'])
def test_pending_policy_at_generator_entrypoints(tmp_path, monkeypatch, pipeline, receipt_kind):
    project = tmp_path / 'project'
    project.mkdir()
    path = project / 'manifest.json'
    attempt = {'provider': 'flow2api'} if receipt_kind == 'flow2api' else {}
    if receipt_kind == 'fixed_native':
        attempt = {'provider': 'google_fx', 'account_id': 'only-profile', 'fixed_video_account': True}
    path.write_text(json.dumps(manifest_attempt(**attempt)))
    before = path.read_bytes()
    monkeypatch.setattr(vg, '_get_project_dir', lambda _: str(project))
    monkeypatch.setattr(vg, 'load_slot_frames', lambda *a: ({1: 'start.webp', 2: 'end.webp'}, {}))
    service = Mock(side_effect=RuntimeError('provider reached'))
    monkeypatch.setattr(vg, '_get_video_generation_service', service)
    config = ({'videoProvider': 'google_fx', 'videoFixedUserId': 'only-profile'}
              if receipt_kind == 'fixed_native' else {'videoProvider': 'flow2api'})
    if pipeline == 'auto_recovery':
        import pipeline_orchestrator as po
        call = lambda: po._render_videos_with_recovery(config, 'project', PROMPT)
    else:
        generate = vg.generate_video_chain_sequence if pipeline == 'chain' else vg.generate_video_sequence
        call = lambda: generate(config, 'project', PROMPT, target_slots=[1])
    if receipt_kind == 'fixed_native':
        with pytest.raises(vg.VideoSubmissionPendingError) as error:
            call()
        assert error.value.pending == [{'slot': 1}]
        service.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match='provider reached'):
            call()
        service.assert_called_once_with(config)
    assert path.read_bytes() == before


@pytest.mark.parametrize('case', ['other_slot', 'other_project', 'settled', 'old_google_fx', 'retained_flow_artifact'])
def test_pending_guard_does_not_block_unrelated_or_settled_work(case):
    manifest = manifest_attempt()
    slots = {1}
    if case == 'other_slot':
        slots = {2}
    elif case == 'other_project':
        manifest = {}
        vg.get_or_create_task('videos_other_project', {'project_key': 'other', 'video_provider': 'flow2api'})
        VideoOperationStore().record_submission('videos_other_project', {
            'slot': 1, 'submission_id': 'other-submission', 'submission_pending': True})
    elif case == 'settled':
        manifest = manifest_attempt(submission_pending=False)
    else:
        manifest = {'videos': [{'slot': 1, 'last_attempt': {
            'account_id': 'legacy-account', 'project_url': 'legacy-project',
            'submission_pending': True}}]}
        if case == 'retained_flow_artifact':
            manifest['videos'][0].update(provider='flow2api', status='success', retained_previous=True)
        vg.get_or_create_task('videos_legacy', {'project_key': 'project', 'video_provider': 'google_fx'})
        VideoOperationStore().record_submission('videos_legacy', manifest['videos'][0]['last_attempt'] | {'slot': 1})
    vg.ensure_video_submissions_resolved('project', slots, manifest=manifest)


def test_settled_fixed_journal_overrides_stale_manifest_but_not_another_pending_submission():
    vg.get_or_create_task('videos_previous', {'project_key': 'project', 'video_provider': 'google_fx'})
    store = VideoOperationStore()
    fixed = {'provider': 'google_fx', 'fixed_video_account': True, 'account_id': 'only-profile'}
    store.record_submission('videos_previous', {
        **fixed, 'slot': 1, 'submission_id': 'pending-submission', 'submission_pending': False, 'confirmed': True})
    vg.ensure_video_submissions_resolved('project', {1}, manifest=manifest_attempt(**fixed))
    store.record_submission('videos_previous', {
        **fixed, 'slot': 1, 'submission_id': 'another-pending-submission', 'submission_pending': True})
    with pytest.raises(vg.VideoSubmissionPendingError):
        vg.ensure_video_submissions_resolved('project', {1}, manifest=manifest_attempt(**fixed))


def test_manifest_keeps_explicit_provider_on_latest_attempt(tmp_path):
    writer = vg._ManifestWriter(str(tmp_path / 'manifest.json'), {}, [1])
    writer.record({'slot': 1, 'status': 'failed', 'last_attempt': {
        'provider': 'flow2api', 'api_model': 'omni-1.1-flash-10s-portrait-360p',
        'submission_id': 'pending-submission', 'submission_pending': True}})
    assert writer.data['videos'][0]['last_attempt']['provider'] == 'flow2api'
    vg.ensure_video_submissions_resolved('project', {1}, manifest=writer.data)
    assert writer.data['videos'][0]['last_attempt']['submission_pending'] is True


def test_scoped_flow_receipt_survives_task_cleanup_without_blocking_retry():
    store = VideoOperationStore()
    receipt = {'slot': 1, 'submission_id': 'submission-123', 'submission_pending': True,
               'project_key': 'project', 'provider': 'flow2api', 'request_id': 'original-request-123'}
    store.record_submission('videos_removed_task', receipt)
    assert 'videos_removed_task' not in vg.ACTIVE_TASKS
    vg.ensure_video_submissions_resolved('project', {1}, manifest={})
    assert store.submissions('videos_removed_task')[0]['submission_pending'] is True
    vg.ensure_video_submissions_resolved('other_project', {1}, manifest={})
    # Legacy resolution callers omit scope, but still replace the original row.
    store.record_submission('videos_removed_task', {
        'slot': 1, 'submission_id': 'submission-123', 'submission_pending': False, 'confirmed': True})
    resolved, = store.submissions('videos_removed_task')
    assert resolved['provider'] == 'flow2api' and resolved['project_key'] == 'project'
    vg.ensure_video_submissions_resolved('project', {1}, manifest={})


@pytest.mark.parametrize('mode', ['auto', 'staged', 'stepped'])
@pytest.mark.parametrize('provider', ['flow2api', 'google_fx'])
def test_pipeline_entrypoints_respect_provider_pending_policy(monkeypatch, tmp_path, mode, provider):
    import pipeline_orchestrator as po
    import stepped_pipeline as stepped
    path = tmp_path / 'manifest.json'
    attempt = {'provider': provider}
    if provider == 'google_fx':
        attempt.update(fixed_video_account=True, account_id='only-profile')
    path.write_text(json.dumps(manifest_attempt(**attempt)))
    monkeypatch.setattr(vg, '_get_project_dir', lambda *a: str(tmp_path))
    monkeypatch.setattr(vg, 'load_slot_frames', lambda *a: ({1: 'start.webp', 2: 'end.webp'}, {}))
    service = Mock(side_effect=RuntimeError('provider reached'))
    monkeypatch.setattr(vg, '_get_video_generation_service', service)
    monkeypatch.setattr(po, 'compose_anchor_and_packet', lambda *a, **k: {
        'title': 'project', 'image_1_prompt': 'start', 'packet': {}, 'parsed_brief': {}, 'beat_ladder': []})
    monkeypatch.setattr(po, 'render_single_frame', lambda *a, **k: {
        'image_path': str(tmp_path / 'first.webp'), 'project_dir': str(tmp_path)})
    monkeypatch.setattr(po, 'refine_packet_from_accepted_anchor', lambda *a, **k: {})
    monkeypatch.setattr(po, 'compose_remaining_beats', lambda *a, **k: PROMPT)
    monkeypatch.setattr(po, 'prompt_block_from_output', lambda value: value)
    monkeypatch.setattr(po, 'persist_outline_delivery_ledger', lambda *a, **k: None)
    monkeypatch.setattr(po, 'generate_frame_sequence', lambda *a, **k: None)
    monkeypatch.setattr(po, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
    monkeypatch.setattr(po, '_get_project_dir', lambda *a: str(tmp_path))
    config = {'videoProvider': provider, 'imageBackend': 'api'}
    if provider == 'google_fx':
        config['videoFixedUserId'] = 'only-profile'
    expected = vg.VideoSubmissionPendingError if provider == 'google_fx' else RuntimeError
    message = '请先核对原任务' if provider == 'google_fx' else 'provider reached'
    with pytest.raises(expected, match=message):
        if mode == 'auto':
            po.run_autonomous_pipeline(config, {})
        elif mode == 'staged':
            po.run_staged_frame_rendering(config, 'project', PROMPT)
        else:
            state = {'stage': 'final_review', 'title': 'project', 'prompt_block': PROMPT}
            monkeypatch.setattr(stepped, '_load_state', lambda *a: state)
            monkeypatch.setattr(stepped, '_save_state', lambda *a: None)
            monkeypatch.setattr(stepped, '_enrich_state_with_refs', lambda state: state)
            monkeypatch.setattr(stepped, '_get_project_dir', lambda *a: str(tmp_path))
            monkeypatch.setattr(stepped, 'optimize_video_prompts_for_sequence', lambda *a, **k: PROMPT)
            stepped.advance_stepped_pipeline('project', config=config)
    if provider == 'google_fx':
        service.assert_not_called()
    else:
        service.assert_called_once_with(config)
    assert json.loads(path.read_text())['videos'][0]['last_attempt']['submission_pending'] is True


def test_flow_retry_can_switch_from_pending_fixed_native_attempt_without_settling_it():
    manifest = manifest_attempt(provider='google_fx', fixed_video_account=True, account_id='only-profile')
    vg.ensure_video_submissions_resolved('project', {1}, manifest=manifest, provider='flow2api')
    assert manifest['videos'][0]['last_attempt']['submission_pending'] is True
    with pytest.raises(vg.VideoSubmissionPendingError):
        vg.ensure_video_submissions_resolved('project', {1}, manifest=manifest, provider='google_fx')
