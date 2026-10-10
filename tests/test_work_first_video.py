"""Work-first choreography keeps clean anchors without compulsory walk-in/out scenes."""
import pytest
import prompt_pipeline as pp
from prompt_pipeline.composers.base import BaseComposer
from prompt_pipeline.composers.omni import ensure_ladder_out_and_in


@pytest.mark.parametrize('entry', ['reaches in from off-frame', 'leans in from just off-frame'])
def test_edge_contact_and_withdrawal_are_valid_and_not_rewritten(entry):
    body = (f'The worker {entry} into first effective tool contact immediately, '
            'repeatedly fastening boards. The worker withdraws fully out of frame '
            'with the last fastening motion; the final frame is empty of people.')
    assert pp.check_out_and_in(body) == []
    assert pp.fix_out_and_in(body) == body
    assert ensure_ladder_out_and_in(body, ('construction',)) == body


def test_material_movement_does_not_satisfy_worker_boundaries():
    body = ('The worker repeatedly fastens boards with first effective tool contact immediately. '
            'A board enters from off-frame. Waste exits into a skip.')
    assert pp.check_out_and_in(body)


@pytest.mark.parametrize('body', ['The worker repeatedly fastens boards.',
                               'Two workers repeatedly fasten boards.'])
def test_fallback_boundaries_are_work_integrated_and_idempotent(body):
    fixed = pp.fix_out_and_in(body)
    assert 'one short reach or step' in fixed
    assert 'last working motion' in fixed
    assert pp.check_out_and_in(fixed) == []
    assert pp.fix_out_and_in(fixed) == fixed


def test_omni_fallback_is_work_integrated_and_idempotent():
    fixed = ensure_ladder_out_and_in('The worker repeatedly fastens boards.', ('construction',))
    assert 'one short reach or step' in fixed
    assert 'last working motion' in fixed
    assert pp.check_out_and_in(fixed) == []
    assert ensure_ladder_out_and_in(fixed, ('construction',)) == fixed


def test_generation_paths_share_work_first_policy():
    contract = {'beat': {'id': 'B01', 'operation': 'clearing', 'description': 'clearing soil'},
                'img_i_lighting': 'ambient', 'img_ip1_lighting': 'ambient',
                'family_contract': 'exterior', 'templates_cropped': '', 'anchor_rule': 'static'}
    prompts = [pp._batch_shared_system_prompt({}, 'SCUP', 'TBCP'),
               BaseComposer().single_beat_system_prompt(
                   {}, 1, contract, {}, {1: 'Image 1'}, {}, 'SCUP', 'TBCP')]
    for prompt in prompts:
        assert pp.WORK_FIRST_VIDEO_RULES in prompt
        assert 'Retain installed materials' in prompt
        assert 'do not impose identical second-by-second choreography' in prompt


def test_boundary_exemptions_remain_unchanged():
    body = 'The worker watches the finished room.'
    assert pp.fix_out_and_in(body, is_threshold_or_reveal=True) == body
    assert pp.check_out_and_in(body, is_threshold_or_reveal=True) == []
