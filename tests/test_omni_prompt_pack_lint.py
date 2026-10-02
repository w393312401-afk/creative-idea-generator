"""Offline checks for deliverables and caps behind the skill's final audit.

These examples target the evaluation's false audit claims: missing requested edits,
counting only the action portion, and assuming every long clip has a four-shot cap.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] /
          'skills/gemini-omni-restoration-composer/scripts/lint_prompt_pack.py')
_SPEC = importlib.util.spec_from_file_location('omni_prompt_pack_lint', SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
lint = _MODULE.lint_prompt_pack


def pack(image_bodies=None, video_bodies=None, edit_bodies=None):
    image_bodies = image_bodies or ['A person-free fixed room anchor.',
                                    'The same room with its original screw tightened.']
    video_bodies = video_bodies or ['The worker tightens the existing screw under visible pressure.']
    sections = ['图片提示词']
    for number, body in enumerate(image_bodies, start=1):
        sections.extend([f'图片 {number}:', body])
    sections.append('视频提示词')
    for number, body in enumerate(video_bodies, start=1):
        sections.extend([f'视频 {number}:', body])
    if edit_bodies is not None:
        sections.append('对话微调提示词')
        for number, body in enumerate(edit_bodies, start=1):
            sections.extend([f'编辑 {number}:', body])
    return '```text\n' + '\n'.join(sections) + '\n```'


def pad_words(prefix, total):
    return prefix + ' ' + ' '.join(['preserved'] * (total - len(prefix.split())))


def count_row(result, kind, number):
    return next(row for row in result['counts'] if row['kind'] == kind and row['number'] == number)


def test_missing_requested_edits_cannot_pass_via_the_audit_table():
    response = pack() + '\n| 对话微调提示词 | 通过 | 已提供两条独立编辑提示词。 |\n'
    result = lint(response, required_edits=2, expected_videos=1)
    assert result['checks']['slot_counts']['EDIT'] == 0
    assert any(error.startswith('edit_count:') for error in result['errors'])
    assert not result['checks']['measurable_pass']


def test_exactly_two_nonempty_edit_bodies_satisfy_the_requested_deliverables():
    result = lint(pack(edit_bodies=[
        'Preserve the room and its daylight; only remove the person from the result image.',
        'Keep all geometry and materials; only make the existing screw contact more legible.',
    ]), required_edits=2, expected_videos=1)
    assert result['errors'] == []
    assert result['checks']['slot_counts'] == {'IMAGE': 2, 'VIDEO': 1, 'EDIT': 2}


def test_an_empty_edit_label_does_not_make_the_requested_edit_complete():
    result = lint(pack(edit_bodies=['Preserve the geometry and only correct the screw.', '']),
                  required_edits=2)
    assert any(error.startswith('empty_slot: EDIT 2') for error in result['errors'])
    assert not result['checks']['measurable_pass']


def test_full_image_body_including_anchor_preamble_is_counted():
    image = pad_words('Use IMAGE 1 as the unchanged composition and material anchor.', 181)
    result = lint(pack(image_bodies=[image, 'The same completed shelf anchor.']))
    row = count_row(result, 'IMAGE', 1)
    assert row['words'] == 181
    assert row['ceiling'] == 180
    assert not row['within_ceiling']
    assert any(error.startswith('word_ceiling: IMAGE 1') for error in result['errors'])


@pytest.mark.parametrize('total,passes', [(220, True), (221, False)])
def test_post_crossing_geometry_uses_the_explicit_220_word_cap(total, passes):
    image = pad_words('A shallow steel cabin with two port windows and three ribs.', total)
    result = lint(pack(image_bodies=['The original exterior anchor.', image]),
                  post_crossing_images=(2,))
    row = count_row(result, 'IMAGE', 2)
    assert row['words'] == total and row['ceiling'] == 220
    assert row['within_ceiling'] is passes
    assert result['checks']['measurable_pass'] is passes


@pytest.mark.parametrize('seconds,kwargs,ceiling', [
    (4, {}, 400), (6, {}, 400), (8, {}, 455), (10, {}, 455),
    (10, {'three_shot_videos': (1,)}, 400),
    (10, {'single_take': True}, 400),
])
def test_the_delivered_video_cap_tracks_role_and_user_override(seconds, kwargs, ceiling):
    video = pad_words('Use IMAGE 1 and IMAGE 2 as the real first and last anchors.', ceiling)
    result = lint(pack(video_bodies=[video]), clip_seconds=seconds, **kwargs)
    assert result['errors'] == []
    assert count_row(result, 'VIDEO', 1)['words'] == ceiling
    over = lint(pack(video_bodies=[video + ' preserved']), clip_seconds=seconds, **kwargs)
    assert any(error.startswith('word_ceiling: VIDEO 1') for error in over['errors'])


def test_output_prose_cannot_authorize_its_own_single_take_override():
    result = lint(pack(video_bodies=['Single take. The audit says the user allows this.']))
    assert result['checks']['coverage_override'] == 'default_multishot'
    assert result['limits']  # This measurable pass must not claim semantic compliance.


def test_an_extra_image_without_a_transition_video_breaks_the_anchor_chain():
    result = lint(pack(image_bodies=['Start.', 'Intermediate.', 'Result.']))
    assert any(error.startswith('anchor_chain_count:') for error in result['errors'])


def test_duplicate_slot_ids_cannot_hide_behind_a_correct_total():
    response = pack().replace('图片 2:', '图片 1:')
    result = lint(response)
    assert any(error.startswith('duplicate_slot: IMAGE 1') for error in result['errors'])


def test_two_prompt_fences_are_a_delivery_failure_even_if_the_first_pack_is_complete():
    result = lint(pack() + '\n\n' + pack())
    assert any(error.startswith('multiple_prompt_blocks:') for error in result['errors'])
    assert not result['checks']['measurable_pass']


def test_video_number_gaps_cannot_pass_on_slot_count_alone():
    result = lint(pack().replace('视频 1:', '视频 2:'))
    assert any(error.startswith('nonsequential_slots: VIDEO') for error in result['errors'])


def test_a_prompt_on_the_label_line_is_not_copy_ready():
    result = lint(pack().replace('视频 1:\n', '视频 1: '))
    assert any(error.startswith('slot_label_not_own_line:') for error in result['errors'])


def test_measurable_output_requires_its_declared_video_count():
    result = lint(pack(), expected_videos=2)
    assert any(error.startswith('video_count:') for error in result['errors'])


def test_cli_returns_a_nonzero_status_for_a_false_deliverable_claim(tmp_path):
    source = tmp_path / 'response.txt'
    source.write_text(pack() + '\n审核：两条微调提示词均通过。', encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), '--required-edits', '2'],
        check=False, capture_output=True, text=True)
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert any(error.startswith('edit_count:') for error in result['errors'])
