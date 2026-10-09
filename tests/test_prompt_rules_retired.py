"""Retired content rules cannot be revived by legacy requests or direct helpers."""
import copy
import json
from unittest.mock import Mock

import pytest

import frame_generator as frames
import prompt_pipeline as prompts


LEGACY_CONFIG = {
    'reviewsDisabled': False,
    'strictFrameStateContract': True,
    'strictGates': True,
    'qaGateLevel': 'standard',
    'chainGuardMode': 'autofix',
}
IDEATION_REVIEW_HELPERS = (
    'ideation_family_quota_violations',
    'outline_skeleton_violations',
    'outline_weight_violations',
    'pacing_skeleton_outline_violations',
    'ideation_topic_violations',
    '_outline_rich_entry_violations',
    '_outline_beat_property_violations',
    '_outline_rich_list_violations',
)


def formerly_rejected_idea(index):
    """Valid JSON/card shape, deliberately outside the former content contracts."""
    return {
        'title': f'天然石洞抛光成机械工作室 {index}',
        'dna': f'natural-grotto-{index} / machinery-workshop / surface-polish',
        'carrier': f'natural rock grotto {index}',
        'destiny': 'precision machinery workshop',
        'twist': 'transparent-material-wall',
        'salvage_en': 'No removed fitting is reused.',
        'pacing_skeleton': 'dual_payoff',
        'zone_map': ['declared-room-zone'],
        'beat_outline': [
            {
                'op': 'repair',
                'text': '只抛光同一片石壁',
                'en': '只抛光同一片石壁',
                'mat': ['stone'],
                'zone': 'unlisted-original-zone',
                'scope': 'unsupported-original-scope',
                'trace': 'Original trace wording retained: no new construction.',
            },
            {
                'op': 'repair',
                'text': '再次抛光同一片石壁，没有终景',
                'en': 'Again polish the identical surface without a reward.',
                'mat': ['stone'],
                'zone': 'different-unlisted-original-zone',
                'scope': 'another-unsupported-original-scope',
                'trace': 'A second original trace sentence, still unchanged.',
            },
        ],
    }


@pytest.fixture
def isolated_ideation(monkeypatch, tmp_path):
    """All text/model/trend IO is explicit; the real run_ideate still runs."""
    monkeypatch.setattr(prompts, 'active_skill_profile', lambda _config: 'base')
    monkeypatch.setattr(prompts, 'load_reference_file', Mock(return_value='fixture idea engine'))
    monkeypatch.setattr(prompts, 'load_used_topic_ledger', Mock(return_value=''))
    monkeypatch.setattr(prompts, 'skill_contract_report', Mock(return_value={
        'label': 'fixture', 'dir': str(tmp_path), 'source': 'fixture',
        'total': 1, 'missing': [],
    }))
    monkeypatch.setattr(prompts, 'read_ledger', Mock(return_value=[]))
    for name, result in (
        ('fetch_trend_snippet', ''),
        ('fetch_custom_url_snippet', ''),
        ('persist_trend_refs', []),
        ('load_trend_refs', []),
    ):
        monkeypatch.setattr(prompts, name, Mock(return_value=result))
    spies = {}
    for name in IDEATION_REVIEW_HELPERS + ('_strip_outline_rich_fields',):
        spies[name] = Mock(side_effect=AssertionError(f'retired content rule ran: {name}'))
        monkeypatch.setattr(prompts, name, spies[name])
    return spies


def assert_reviews_not_called(spies):
    for spy in spies.values():
        spy.assert_not_called()


def test_ideation_ignores_content_rules_and_preserves_original_rich_fields(
        isolated_ideation, monkeypatch):
    candidates = [formerly_rejected_idea(1), formerly_rejected_idea(2)]
    original = copy.deepcopy(candidates)
    chat = Mock(return_value=json.dumps(candidates, ensure_ascii=False))
    monkeypatch.setattr(prompts, '_chat', chat)

    result = prompts.run_ideate(
        dict(LEGACY_CONFIG), count=2, pacing_skeleton_ids=['dual_payoff'])

    assert set(result) == {'ideas', 'trend_refs'}
    assert result['trend_refs'] == []
    assert [idea['title'] for idea in result['ideas']] == [idea['title'] for idea in original]
    for delivered, expected in zip(result['ideas'], original):
        assert delivered['beat_outline'] == expected['beat_outline']
        assert delivered['pacing_skeleton'] == 'dual_payoff'
        assert delivered['recommended_beats'] == 1
        assert not delivered.get('pacing_downgraded')
        assert not delivered.get('degraded')
    chat.assert_called_once()
    assert chat.call_args.args[0]['reviewsDisabled'] is False
    assert_reviews_not_called(isolated_ideation)


def test_ideation_still_requests_missing_candidate_count_without_quality_retry(
        isolated_ideation, monkeypatch):
    chat = Mock(side_effect=[
        json.dumps([formerly_rejected_idea(1)], ensure_ascii=False),
        json.dumps([formerly_rejected_idea(2)], ensure_ascii=False),
    ])
    monkeypatch.setattr(prompts, '_chat', chat)

    result = prompts.run_ideate(
        dict(LEGACY_CONFIG), count=2, pacing_skeleton_ids=['dual_payoff'])

    assert len(result['ideas']) == 2
    assert [idea['title'] for idea in result['ideas']] == [
        formerly_rejected_idea(1)['title'], formerly_rejected_idea(2)['title']]
    assert chat.call_count == 2
    second_request = chat.call_args_list[1].args[2]
    assert 'Generate 1 ' in second_request
    assert 'failed the selected pacing skeleton acceptance gate' not in second_request
    assert_reviews_not_called(isolated_ideation)


@pytest.mark.parametrize('invalid_first_response', [
    'This is not JSON.',
    json.dumps({'ideas': [formerly_rejected_idea(1)]}, ensure_ascii=False),
])
def test_ideation_keeps_json_and_list_shape_requirements(
        isolated_ideation, monkeypatch, invalid_first_response):
    candidate = formerly_rejected_idea(1)
    chat = Mock(side_effect=[invalid_first_response, json.dumps([candidate], ensure_ascii=False)])
    monkeypatch.setattr(prompts, '_chat', chat)

    result = prompts.run_ideate(
        dict(LEGACY_CONFIG), count=1, pacing_skeleton_ids=['dual_payoff'])

    assert len(result['ideas']) == 1
    assert result['ideas'][0]['title'] == candidate['title']
    assert result['ideas'][0]['beat_outline'] == candidate['beat_outline']
    assert chat.call_count == 2, 'Malformed JSON/container still needs a valid response'
    assert_reviews_not_called(isolated_ideation)


@pytest.fixture
def forbidden_content_io(monkeypatch):
    spies = {}
    for name in ('_chat', '_multimodal_chat', 'classify_image_space_layer',
                 'compress_prompt_to_budget', 'clean_prompt_text',
                 'fix_image_clean_frame_proactive'):
        spies[name] = Mock(side_effect=AssertionError(f'retired review/rewrite called: {name}'))
        monkeypatch.setattr(prompts, name, spies[name])
    return spies


def test_anchor_refinement_returns_original_packet_without_visual_review(
        forbidden_content_io, tmp_path):
    packet = {
        'camera_dna': 'Original arbitrary camera wording.',
        'world_lock': {'status': 'planned', 'terrain': 'unchanged original terrain'},
        'object_ledger': [{'name': 'original carried item'}],
    }
    baseline = copy.deepcopy(packet)

    result = prompts.refine_packet_from_accepted_anchor(
        dict(LEGACY_CONFIG), str(tmp_path / 'never-rendered-anchor.webp'), packet,
        {'carrier_arrives_on_camera': True})

    assert result is packet
    assert packet == baseline
    assert_reviews_not_called(forbidden_content_io)


def test_cover_reference_passes_without_layer_classification(forbidden_content_io, tmp_path):
    assert prompts.cover_reference_is_same_layer(
        dict(LEGACY_CONFIG), str(tmp_path / 'never-rendered-cover.webp'), 'interior'
    ) == (True, 'retired', '')
    assert_reviews_not_called(forbidden_content_io)


def test_adjacent_semantics_do_not_call_a_judge(forbidden_content_io):
    original = {1: {'body': 'A finished room.'}, 2: {'body': 'The same room becomes a ruin.'}}
    assert prompts.check_adjacent_frame_semantics_batch(dict(LEGACY_CONFIG), original) == {}
    assert original == {
        1: {'body': 'A finished room.'}, 2: {'body': 'The same room becomes a ruin.'}}
    assert_reviews_not_called(forbidden_content_io)


def test_proactive_quality_fixes_preserve_both_bodies_verbatim(forbidden_content_io):
    video = '  A worker changes 50% w/ tools. No mandated exit or camera ladder.\n'
    image = '\nA person with a tripod on a wet shiny floor; arbitrary 50% framing.  '

    assert prompts.apply_proactive_fixes(
        1, video, image, {'camera_dna': 'Different mandatory camera'},
        'Threshold', True, True,
        beat={'operation': 'flooring', 'after_state': 'Different demanded state'},
        config=dict(LEGACY_CONFIG), family='interior',
    ) == (video, image)
    assert_reviews_not_called(forbidden_content_io)


@pytest.mark.parametrize('quality_gate', ['sequence_review_flagged', 'manual_flagged'])
def test_ready_event_ignores_retired_quality_flags_for_a_nonempty_file(
        monkeypatch, tmp_path, quality_gate):
    path = tmp_path / 'synthetic-ready-fixture.webp'
    path.write_bytes(b'nonempty fixture file; no media was generated')
    frame = {'sequence': 7, 'slot': 7, 'file': str(path), 'quality_gate': quality_gate}
    read = Mock(return_value={'frames': [frame], 'prompt_block': 'Original prompt body.'})
    monkeypatch.setattr(frames, 'read_manifest', read)
    events = []

    frames.emit_frame_ready(str(tmp_path), 7, lambda *event: events.append(event), 2, 3)

    read.assert_called_once_with(str(tmp_path))
    assert len(events) == 1
    event_type, event = events[0]
    assert event_type == 'frame_ready'
    assert event['sequence'] == event['slot'] == 7
    assert event['frame'] == frame
    assert event['prompt_block'] == 'Original prompt body.'


@pytest.mark.parametrize('file_state', ['missing', 'empty'])
def test_ready_event_still_requires_an_existing_nonempty_file(monkeypatch, tmp_path, file_state):
    path = tmp_path / 'synthetic-missing-ready-fixture.webp'
    if file_state == 'empty':
        path.write_bytes(b'')
    monkeypatch.setattr(frames, 'read_manifest', Mock(return_value={'frames': [
        {'sequence': 7, 'slot': 7, 'file': str(path), 'quality_gate': 'manual_flagged'}]}))
    events = []

    frames.emit_frame_ready(str(tmp_path), 7, lambda *event: events.append(event), 2, 3)

    assert events == []
