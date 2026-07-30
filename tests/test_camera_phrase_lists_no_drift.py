"""fix_camera_contradictions must strip every clause check_camera_contradictions flags.

When the fixer's vocabulary is narrower than the audit's, the audit reports a contradiction
that no rework round can ever clear — the 2026-07-30 run's beat 3 carried a bare "static
tripod" clause in a moving crossing clip: the fixer only knew "static tripod shot", so the
finding survived to the report. Both sides now read from the same audit-phrase constants.
"""

import re

import pytest

from prompt_pipeline import (
    _MOVING_CAMERA_AUDIT_PHRASES,
    _MOVING_CAMERA_PHRASES,
    _STATIC_CAMERA_AUDIT_PHRASES,
    _STATIC_CAMERA_PHRASES,
    check_camera_contradictions,
    fix_camera_contradictions,
)


@pytest.mark.parametrize('phrase', _STATIC_CAMERA_AUDIT_PHRASES)
def test_fixer_covers_every_static_audit_phrase(phrase):
    assert any(re.search(p, phrase, re.IGNORECASE) for p in _STATIC_CAMERA_PHRASES), phrase


@pytest.mark.parametrize('phrase', _MOVING_CAMERA_AUDIT_PHRASES)
def test_fixer_covers_every_moving_audit_phrase(phrase):
    assert any(re.search(p, phrase, re.IGNORECASE) for p in _MOVING_CAMERA_PHRASES), phrase


@pytest.mark.parametrize('phrase', _STATIC_CAMERA_AUDIT_PHRASES)
def test_moving_prompt_is_clean_after_the_fixer(phrase):
    prompt = (
        f'The shot opens on a {phrase} framing the doorway. '
        'The camera then advances coaxially across the sill into the compartment.'
    )
    assert check_camera_contradictions(prompt, is_moving=True)
    fixed = fix_camera_contradictions(prompt, is_moving=True)
    assert check_camera_contradictions(fixed, is_moving=True) == []


@pytest.mark.parametrize('phrase', _MOVING_CAMERA_AUDIT_PHRASES)
def test_static_prompt_is_clean_after_the_fixer(phrase):
    prompt = (
        'The camera holds a locked eye-level perspective on the compartment. '
        f'A second clause describes the camera {phrase} toward the far wall.'
    )
    assert check_camera_contradictions(prompt, is_moving=False)
    fixed = fix_camera_contradictions(prompt, is_moving=False)
    assert check_camera_contradictions(fixed, is_moving=False) == []


def test_clean_prompts_are_left_alone():
    moving = 'The camera advances coaxially through the opening with no yaw, tilt, roll, or side-step.'
    assert check_camera_contradictions(moving, is_moving=True) == []
    assert fix_camera_contradictions(moving, is_moving=True) == moving
