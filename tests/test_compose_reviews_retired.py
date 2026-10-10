"""Retired composer rules preserve generated text and successful resume checkpoints."""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import prompt_pipeline as pp
from prompt_pipeline.composers.base import BaseComposer
from prompt_pipeline.composers.omni import OmniComposer


VIDEO = 'Single continuous take at 0.0–10.0s: a worker changes 50% w/ raw tools. Wet floor.'
IMAGE = 'A woman at 1.78m, clean royal blue shirt, 50% frame height, a wet floor and work tripod.'
OLD_CONFIG = {
    'reviewsDisabled': False, 'strictPromptPipelineV2': True,
    'strictFrameStateContract': True, 'qaGateLevel': 'standard',
    'videoModel': 'Omni Flash', 'videoDuration': '10',
    'composeBatchSize': 2, 'composeBatchRetryCount': 0,
    '_skipped_checks': 55,
}


def state(total=2):
    return {
        'theme': 'retired-rule-test', 'title': 'Retired rule test',
        'total_beats': total, 'parsed_brief': {'mode': 'Standard'},
        'beat_ladder': [
            {'index': i, 'operation': 'repair', 'description': f'step {i}', 'bridge_stage': None}
            for i in range(1, total + 1)
        ],
        'packet': {'lighting_phase_ladder': {str(i): 'ambient only' for i in range(1, total + 2)},
                   'object_ledger': []},
        'image_1_prompt': 'Original anchor.', 'compiled_images': {1: 'Original anchor.'},
        'compiled_videos': {}, 'brief_fingerprint': 'retired-composer',
    }


@pytest.fixture
def compose_context(tmp_path):
    with ExitStack() as stack:
        stack.enter_context(patch.object(pp, 'COMPOSE_CHECKPOINT_PATH', str(tmp_path / 'checkpoint.json')))
        stack.enter_context(patch.object(pp, 'load_reference_file', return_value=''))
        stack.enter_context(patch.object(pp, 'get_cropped_templates', return_value=''))
        yield stack


@pytest.mark.parametrize('composer_type', [BaseComposer, OmniComposer])
def test_public_quality_hooks_ignore_old_enable_values_and_do_not_modify_text(composer_type):
    composer = composer_type()
    composer.config = dict(OLD_CONFIG)
    with patch.object(pp, 'apply_proactive_fixes', side_effect=AssertionError('retired patch ran')), \
         patch.object(pp, 'validate_beat_prompts', side_effect=AssertionError('retired validation ran')), \
         patch.object(pp, '_chat', side_effect=AssertionError('retired rework called LLM')):
        assert composer.apply_proactive_fixes(
            1, VIDEO, IMAGE, {}, 'Standard', True, False, config=dict(OLD_CONFIG)) == (VIDEO, IMAGE)
        assert composer.validate_beat_prompts(1, VIDEO, IMAGE, {}, 'Standard', True, False) == []
        assert composer.split_structural_video_errors(['old content warning']) == ([], [])
        assert composer.rework_structural_video_beat(
            dict(OLD_CONFIG), 1, VIDEO, ['old content warning'], {}) == (VIDEO, None)
        assert composer.normalize_reworked_video(VIDEO) == VIDEO
        assert composer.video_profile_violations(VIDEO) == []
        assert composer.finalize_fallback_video(VIDEO, {}) == VIDEO
        assert composer.patch_milestone_video_prompt(VIDEO, {'before_state': 'different state'}) == VIDEO
        assert composer.patch_milestone_image_prompt(IMAGE, {'after_state': 'different state'}) == IMAGE


def test_omni_additional_public_normalizers_and_rework_preserve_text():
    composer = OmniComposer()
    composer.config = dict(OLD_CONFIG)
    with patch.object(pp, '_chat', side_effect=AssertionError('retired rework called LLM')):
        assert composer.fix_omni_video(1, VIDEO, {}, False, config=dict(OLD_CONFIG)) == VIDEO
        assert composer.normalize_omni_video(VIDEO, False, beat={'operation': 'repair'}) == VIDEO
        assert composer.ensure_pacing(VIDEO) == VIDEO
        assert composer.ensure_actor_engagement(VIDEO, ()) == VIDEO
        assert composer.deduplicate_boilerplate_phrases(VIDEO + '\n\n' + VIDEO) == VIDEO + '\n\n' + VIDEO
        assert composer.video_contract_errors(VIDEO) == []
        assert composer.rework_omni_multishot(dict(OLD_CONFIG), 1, VIDEO, {}) == (VIDEO, None)


@pytest.mark.parametrize('composer_type', [BaseComposer, OmniComposer])
@pytest.mark.parametrize('missing_batch_sections', [False, True])
def test_generation_skips_all_polymorphic_quality_hooks_and_preserves_both_bodies(
        composer_type, missing_batch_sections, compose_context, capsys):
    composer = composer_type()
    current = state()
    config = dict(OLD_CONFIG)
    calls = []

    def chat(_config, _system, user, **_kwargs):
        calls.append(user)
        if 'Generate prompts for Beat ' in user:
            return f'===VIDEO===\n{VIDEO}\n===IMAGE===\n{IMAGE}\n===TRACES===\n[]'
        if missing_batch_sections:
            return 'Network succeeded but the response omitted the requested sections.'
        return '\n'.join(
            f'===BEAT {i} VIDEO===\n{VIDEO}\n===BEAT {i} IMAGE===\n{IMAGE}\n===BEAT {i} TRACES===\n[]'
            for i in (1, 2))

    forbidden = ['apply_proactive_fixes', 'validate_beat_prompts', 'split_structural_video_errors',
                 'rework_structural_video_beat', 'repair_beat_prompts', 'finalize_fallback_video']
    for name in forbidden:
        compose_context.enter_context(patch.object(composer, name, side_effect=AssertionError(name)))
    compose_context.enter_context(patch.object(pp, 'record_beat_audit', side_effect=AssertionError('audit')))
    compose_context.enter_context(patch.object(pp, 'reverify_beat_repairs', side_effect=AssertionError('reverify')))
    compose_context.enter_context(patch.object(pp, '_chat', side_effect=chat))
    output = composer.compose_remaining_beats(config, current)
    assert current['compiled_videos'] == {1: VIDEO, 2: VIDEO}
    assert current['compiled_images'] == {1: 'Original anchor.', 2: IMAGE, 3: IMAGE}
    assert len(calls) == (3 if missing_batch_sections else 1), 'Only omitted sections justify extra LLM requests'
    assert VIDEO in output and IMAGE in output
    assert '全部审查规则已永久退役' in output
    assert '[WARNING]' not in output, 'Stale skipped-check metadata must not produce quality warnings'
    logs = capsys.readouterr().out
    assert '[DIRECT]' not in logs
    assert '[WARN]' not in logs


def test_old_quality_fallback_count_does_not_discard_completed_resume_data(compose_context):
    current = state()
    current['compiled_images'].update({2: IMAGE, 3: IMAGE})
    current['compiled_videos'].update({1: VIDEO, 2: VIDEO})
    checkpoint = {'pass_beats_done': [1, 2], 'fallback_count': 999}
    load = compose_context.enter_context(patch.object(pp, 'load_compose_checkpoint', return_value=checkpoint))
    save = compose_context.enter_context(patch.object(pp, 'save_compose_checkpoint'))
    terminal = compose_context.enter_context(patch.object(pp, '_checkpoint_is_failed_terminal', side_effect=AssertionError('retired terminal gate')))
    chat = compose_context.enter_context(patch.object(pp, '_chat', side_effect=AssertionError('completed beats regenerated')))
    output = BaseComposer().compose_remaining_beats(dict(OLD_CONFIG), current)
    load.assert_called_once()
    terminal.assert_not_called()
    chat.assert_not_called()
    assert save.call_args.args[1]['pass_beats_done'] == [1, 2]
    assert save.call_args.args[1]['fallback_count'] == 999
    assert VIDEO in output and IMAGE in output


@pytest.mark.parametrize('strict_legacy', [True, False])
def test_missing_response_pair_still_fails_and_keeps_checkpoint(compose_context, strict_legacy):
    config = dict(OLD_CONFIG, strictPromptPipelineV2=strict_legacy)
    compose_context.enter_context(patch.object(pp, '_chat', return_value='===VIDEO===\nMotion only.'))
    save = compose_context.enter_context(patch.object(pp, 'save_compose_checkpoint'))
    with pytest.raises(pp.ComposeFailure) as failure:
        BaseComposer().compose_remaining_beats(config, state(1))
    assert failure.value.failure_code == 'BEAT_GENERATION_FAILED'
    assert save.call_args.args[1]['pass_beats_done'] == []
    assert save.call_args.args[1]['slot_states']['1'] == 'failed'


def test_network_failure_still_uses_configured_retry_and_then_fails(compose_context):
    config = dict(OLD_CONFIG, composeBatchRetryCount=1)
    chat = compose_context.enter_context(patch.object(pp, '_chat', side_effect=TimeoutError('network unavailable')))
    with pytest.raises(pp.ComposeFailure):
        BaseComposer().compose_remaining_beats(config, state(1))
    assert chat.call_count == 4, 'Two network attempts per batch and single-beat fallback remain'
