# -*- coding: utf-8 -*-
import pytest
import prompt_pipeline as pp
from prompt_pipeline.composers.base import BaseComposer
from prompt_pipeline.video_optimizer import optimize_single_video_prompt


def test_batch_shared_system_prompt_contains_dynamic_choreography():
    packet = {'worker_choreography': 'one worker in yellow vest'}
    prompt = pp._batch_shared_system_prompt(packet, 'SCUP', 'TBCP')
    assert 'LIVING CAST & DYNAMIC WORKER CHOREOGRAPHY' in prompt
    assert 'Zero Frozen Figures' in prompt
    assert 'Action-Reaction Causal Chain' in prompt
    assert 'remain standing' in prompt


def test_base_composer_single_beat_system_prompt_contains_dynamic_choreography():
    composer = BaseComposer()
    contract = {
        'beat': {'id': 'B01', 'operation': 'clearing', 'description': 'clearing soil'},
        'img_i_lighting': 'ambient',
        'img_ip1_lighting': 'ambient',
        'family_contract': 'exterior',
        'templates_cropped': '',
        'anchor_rule': 'static',
    }
    packet = {'worker_choreography': 'one worker in yellow vest'}
    compiled_images = {1: 'Image 1 prompt'}
    compiled_videos = {}
    prompt = composer.single_beat_system_prompt(
        {}, 1, contract, packet, compiled_images, compiled_videos, 'SCUP', 'TBCP'
    )
    assert 'LIVING CAST & DYNAMIC WORKER CHOREOGRAPHY' in prompt
    assert 'Zero Frozen Figures' in prompt
    assert 'Action-Reaction Causal Chain' in prompt


def test_beat_block_text_distinguishes_working_vs_bystander_cast():
    working_contract = {
        'beat': {
            'id': 'B01',
            'operation': 'welding',
            'description': 'welds steel frame',
            'cast_action': 'stands on ladder grinding overhead beams, then crouches grinding floor grid welds',
        },
        'img_i_lighting': 'ambient',
        'img_ip1_lighting': 'ambient',
        'family_contract': 'exterior',
        'templates_cropped': '',
        'anchor_rule': 'static',
    }
    block_working = pp._beat_block_text(1, working_contract)
    assert 'They actively execute the physical work cycle without freezing' in block_working
    assert 'They watch, never touch the work' not in block_working

    bystander_contract = {
        'beat': {
            'id': 'B02',
            'operation': 'masonry',
            'description': 'craftsman builds stone wall',
            'cast_action': 'two figurines get up off the stone and turn to face the new wall',
        },
        'img_i_lighting': 'ambient',
        'img_ip1_lighting': 'ambient',
        'family_contract': 'exterior',
        'templates_cropped': '',
        'anchor_rule': 'static',
    }
    block_bystander = pp._beat_block_text(2, bystander_contract)
    assert 'They watch, never touch the work' in block_bystander
    assert 'They actively execute the physical work cycle' not in block_bystander

def test_video_optimizer_system_prompt_includes_worker_continuous_pose_transition():
    from unittest.mock import patch
    import json

    captured_system = []

    def fake_multimodal_chat(config, system_prompt, user_text, images, *args, **kwargs):
        captured_system.append(system_prompt)
        return json.dumps({
            'optimized_prompt': 'Use the provided first frame and last frame as exact composition anchors. Optimized video.',
            'visual_delta_summary': 'Delta summary'
        })

    with patch.object(pp, '_multimodal_chat', side_effect=fake_multimodal_chat), \
         patch('os.path.exists', return_value=True):
        optimize_single_video_prompt(
            {}, 'fake_start.webp', 'fake_end.webp', 'Original video prompt', slot_index=1
        )

    assert len(captured_system) == 1
    sys_prompt = captured_system[0]
    assert 'WORKER CONTINUOUS POSE TRANSITION' in sys_prompt
    assert 'landing pose' in sys_prompt
    assert 'Strictly FORBID static poses or leaving the character unmoving' in sys_prompt
