# -*- coding: utf-8 -*-
"""Unit tests verifying source fixes for living cast dynamics, camera cuts, and worker scrubbing in miniature & omni profiles."""

import pytest
from prompt_pipeline.composers.miniature import MiniatureComposer
from prompt_pipeline.composers.omni import OmniComposer
from prompt_pipeline.video_optimizer import optimize_single_video_prompt


def test_fix_miniature_video_scrubs_all_worker_and_craftsman_variants():
    """All variations of full-scale worker, craftsman, and t=0 starting sentences must be scrubbed."""
    composer = MiniatureComposer()
    raw_prompt = (
        "Use the provided image as the exact starting composition. "
        "At t=0, one worker is already at the work face in Grid B2 and makes first contact using a miniature shovel. "
        "In continuous time-lapse cycles, the craftsman works course by course, picking precast blocks. "
        "A single worker's bare hands and human fingers place the roof rafters."
    )
    cleaned = composer.fix_miniature_video(raw_prompt)
    assert 'one worker is already at the work face' not in cleaned
    assert 'the craftsman' not in cleaned
    assert 'human fingers' not in cleaned
    assert 'oversized human hands and fingers' in cleaned or 'giant hand' in cleaned or 'oversized hand' in cleaned


def test_fix_miniature_video_scrubs_static_and_unmoving_cast_phrases():
    """Banned static phrases like 'unmoving bystander figurines' must be scrubbed and converted to dynamic reactions."""
    composer = MiniatureComposer()
    raw_prompt = (
        "The scene opens with a locked macro working shot where an oversized human hand reaches in. "
        "while unmoving miniature bystander figurines stand watching quietly from the untouched diorama edge."
    )
    cleaned = composer.fix_miniature_video(raw_prompt)
    assert 'unmoving' not in cleaned
    assert 'watching quietly' not in cleaned
    assert 'curious miniature figurines' in cleaned or 'attentively tracking' in cleaned or 'tilt their heads' in cleaned


def test_fix_miniature_video_smoothes_zero_advancement_cut_freeze():
    """'with zero state advancement' must be replaced with fluid micro-physics phrasing to avoid temporal freezing."""
    composer = MiniatureComposer()
    raw_prompt = (
        "The camera cuts into a tight close-up insert on the tool contact point, "
        "capturing the material physics of splintering timber with zero state advancement."
    )
    cleaned = composer.fix_miniature_video(raw_prompt)
    assert 'zero state advancement' not in cleaned
    assert 'without skipping stages' in cleaned


def test_ensure_pacing_and_deduplicate_boilerplate():
    """Boilerplate in-shot and pacing phrases must not be appended multiple times."""
    composer = MiniatureComposer()
    # Prompt already having in-shot phrasing with slight variation
    raw_prompt = (
        "Use the provided image as anchor. "
        "The scene opens with a locked macro working shot where an oversized human hand reaches in. "
        "Inside every shot the frame keeps living from its first to its last moment — the giant hand's own motion, "
        "drifting dust, and the settling of craft debris never freeze, while the camera itself stays locked — "
        "and this beat's change advances only during the working shots. The only compressions in the clip fall between the shot transitions; "
        "no shot contains a hold, a stall, or a deferred step that is then delivered all at once. "
        "Near-field sound of wood. "
        "Inside every shot the frame keeps living from its first to its last moment — the giant hand's own motion, "
        "drifting dust, and the settling of craft debris never freeze, while the camera itself stays locked — "
        "and this beat's change advances only during the working shots. The only compressions in the clip fall exactly on the listed cut marks; "
        "no shot contains a hold, a stall, or a deferred step that is then delivered all at once."
    )
    pacing_fixed = composer.ensure_pacing(raw_prompt)
    # Count occurrences of the in-shot phrase
    count = pacing_fixed.count("Inside every shot the frame keeps living")
    assert count == 1, f"Expected exactly 1 in-shot occurrence, found {count}"


def test_ensure_living_cast_reaction_fallback():
    """When a miniature video prompt has zero mention of the living cast, the dynamic
    action-reaction fallback must be injected - in REAL-HUMAN wording (2026-08-30
    活物一律真人): the residents of a miniature set are small people, never resin figurines."""
    composer = MiniatureComposer()
    prompt_without_cast = (
        "Use the provided image as the exact starting composition. "
        "The scene opens with a locked macro working shot where an oversized human hand reaches in. "
        "The camera cuts into a close-up insert on the tool contact. "
        "Finally, the camera cuts back to the returning macro shot from the same locked macro setup, "
        "where the giant hand completes the work and withdraws."
    )
    fixed = composer.ensure_living_cast_reaction(prompt_without_cast)
    assert 'miniature-scale resident couple' in fixed
    assert 'figurine' not in fixed.lower()
    assert 'tilt their heads' in fixed or 'curiosity' in fixed or 'tracking' in fixed


def test_video_optimizer_profile_awareness(monkeypatch):
    """video_optimizer must use miniature rules when profile is miniature, and not inject 1.78m worker."""
    import json

    captured_system = []

    def fake_multimodal_chat(config, system_prompt, user_text, images, *args, **kwargs):
        captured_system.append(system_prompt)
        return json.dumps({
            'optimized_prompt': 'Use the provided image as the exact starting composition and environment anchor. Use IMAGE 1 as first frame. Optimized video.',
            'visual_delta_summary': 'Delta summary'
        })

    import prompt_pipeline as pp
    monkeypatch.setattr(pp, '_multimodal_chat', fake_multimodal_chat)
    monkeypatch.setattr('os.path.exists', lambda p: True)

    optimize_single_video_prompt(
        {}, 'fake_start.webp', 'fake_end.webp', 'Original miniature diorama prompt',
        slot_index=1, profile='miniature'
    )

    assert len(captured_system) == 1
    sys_prompt = captured_system[0]
    assert 'OVERSIZED REAL HUMAN HANDS' in sys_prompt
    assert '1.78m' not in sys_prompt
    assert 'miniature craft time-lapse' in sys_prompt
