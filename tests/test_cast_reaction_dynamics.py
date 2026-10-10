"""Unit tests for Living Cast Dynamic Reflex and Action-Reaction Causal Interlock rules."""

import pytest
from prompt_pipeline import build_outline_plan_block, _beat_block_text
from prompt_pipeline.composers.miniature import MiniatureComposer






def test_outline_plan_block_injects_action_reaction_causal_chain():
    """build_outline_plan_block should inject the action-reaction causal chain rule when cast is present."""
    raw_plan = [{
        'op': 'clear shack',
        'text': 'The craftsman hand removes the old wooden shack and places a rolled blueprint.',
        'cast': 'As hand enters -> figurines look up in awe -> stand up',
    }]
    _, block = build_outline_plan_block(raw_plan, max_total_beats=10)
    assert 'ACTION-REACTION CAUSAL CHAIN' in block
    assert 'CAST:' in block


def test_beat_block_text_injects_causal_interlock_rule():
    """_beat_block_text should inject action-reaction causal chain instructions when cast_action is present."""
    contract = {
        'beat': {
            'id': 'B01',
            'cast_action': 'As hand descends -> figurines look up -> stand up',
            'operation': 'clear shack',
            'summary': 'clear shack',
        },
        'shot_family': 'macro_working',
        'lighting_phase': 'ambient',
        'carrier_anchor_rule': '',
        'img_i_lighting': 'ambient',
        'img_ip1_lighting': 'ambient',
        'family_contract': 'macro',
        'anchor_rule': 'static',
        'templates_cropped': 'exemplars',
    }
    block = _beat_block_text(1, contract)
    assert 'ACTION-REACTION CAUSAL CHAIN' in block
    assert 'CAST IN FRAME' in block


def test_miniature_composer_system_prompt_contains_living_cast_interlock():
    """MiniatureComposer system prompts must contain LIVING CAST action-reaction interlock rules."""
    composer = MiniatureComposer()
    config = {'model': 'gemini-2.5-pro', 'api_key': 'test'}
    packet = {
        'parsed_brief': {
            'carrier': 'miniature diorama',
            'env': 'workshop',
            'trauma': 'broken wood',
            'destiny': 'finished cottage',
            'reward': 'warm lights glow',
        }
    }
    prompt = composer.batch_system_prompt(config, packet, None, None)
    assert 'LIVING CAST' in prompt
    assert 'ACTION-REACTION CAUSAL INTERLOCK' in prompt
