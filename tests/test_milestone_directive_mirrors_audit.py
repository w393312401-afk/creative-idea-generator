"""_milestone_beat_directive must state, in the audits' own vocabulary, every rule the
deterministic post-generation audits enforce.

A 2026-07-30 live run (11 beats) failed 6/11 on the material-source/movement-path contract
and 6/11 on the large-tier full-coverage claim, then spent a rework round on each. Both rules
existed only as clauses buried in a run-on sentence, phrased in words the checker does not
match on. These tests pin the checklist to the checkers: if someone changes a checker's
required vocabulary without updating the directive, the beat skeleton silently stops teaching
the rule and every run pays for it in rework.
"""

import re

import prompt_pipeline as pp
from prompt_pipeline import (
    _milestone_beat_directive,
    check_milestone_image_prompt,
    check_milestone_video_prompt,
    check_stage_scope_wording,
)


def _beat(**overrides):
    beat = {
        'operation': 'build',
        'milestone_name': 'stud framing grid installed',
        'before_state': 'bare shell interior',
        'after_state': 'stud grid fills all bays',
        'completion_extent': 'all fourteen bays',
        'changed_grid_cells': ['B1', 'B2'],
        'package_operations': ['set studs', 'fit batts'],
        'primary_progress': 'stud count rising',
        'secondary_progress': 'batt bundle shrinking',
        'persistent_traces': ['screw heads', 'fibre dust'],
        'preserve_state': 'floor stays bare',
        'stage_scope': 'large',
    }
    beat.update(overrides)
    return beat


def test_directive_teaches_the_repeated_cycle_vocabulary():
    """check_milestone_video_prompt matches on a fixed token set — the directive must name it."""
    directive = _milestone_beat_directive(_beat()).lower()
    tokens = ('repeated', 'repeatedly', 'cycle by cycle', 'one by one',
              'course by course', 'row by row')
    assert [t for t in tokens if t in directive], directive


def test_directive_teaches_the_material_source_vocabulary():
    """The 6/11 failure mode: no rigid-container noun in the VIDEO prompt."""
    directive = _milestone_beat_directive(_beat()).lower()
    container_nouns = ('stock', 'bundle', 'stack', 'crate', 'bucket', 'barrow',
                       'carrier', 'container', 'pile', 'rack', 'bag', 'tray')
    named = [n for n in container_nouns if n in directive]
    assert len(named) >= 3, f'directive names too few container nouns: {named}'
    assert 'movement path' in directive


def test_directive_teaches_first_contact_vocabulary():
    directive = _milestone_beat_directive(_beat()).lower()
    assert re.search(r'\bfirst\b', directive)


def test_large_tier_demands_own_sentence_coverage_and_names_the_carryover_cues():
    """The full-coverage claim is graded per sentence; the directive must say so and must
    enumerate the carry-over cues that disqualify a sentence."""
    directive = _milestone_beat_directive(_beat(stage_scope='large'))
    low = directive.lower()
    assert [m for m in pp._STAGE_SCOPE_FULL_COVERAGE_MARKERS if m in low]
    for cue in ('remain', 'stay', 'unchanged', 'already', 'previously', 'prior'):
        assert cue in low, f'carry-over cue not taught: {cue}'


def test_small_and_default_tiers_are_told_the_inverse():
    for scope in ('small', 'default'):
        low = _milestone_beat_directive(_beat(stage_scope=scope)).lower()
        assert 'no full-coverage phrase' in low, scope


def test_no_coverage_rule_when_tier_is_absent():
    low = _milestone_beat_directive(_beat(stage_scope=None)).lower()
    assert 'full-coverage phrase' not in low


def test_directive_requires_declared_features_to_appear_in_the_image():
    directive = _milestone_beat_directive(_beat())
    assert '===TRACES===' in directive


def test_directive_requires_a_visible_worker_entering_and_exiting():
    low = _milestone_beat_directive(_beat()).lower()
    assert 'ghost work' in low
    assert 'enters at the start' in low and 'exits before the final frame' in low


def test_directive_stays_terse():
    """Budget guard. This block is repeated once per beat inside a SINGLE batched call, so its
    length is multiplied by ~11. A 2026-07-31 trial ran it at 3728 chars/beat and per-item
    compliance dropped (repeated-cycle and progress-line misses rose against the same theme's
    baseline). The numbered items must carry the checkers' vocabulary, not restate the contract
    block's field values — if this trips, cut prose, don't raise the ceiling."""
    directive = _milestone_beat_directive(_beat())
    assert len(directive) < 2600, len(directive)


def test_special_beats_still_get_no_milestone_directive():
    for special in ({'operation': 'threshold'}, {'operation': 'reward'},
                    {'bridge_stage': 1}, {'hard_cut': True}):
        assert _milestone_beat_directive(_beat(**special)) == ''


def test_contract_fields_are_still_carried_verbatim():
    """The header block feeds the checkers' keyword-overlap comparisons — it must keep
    quoting the planner's authoritative fields."""
    beat = _beat()
    directive = _milestone_beat_directive(beat)
    for field in ('milestone_name', 'before_state', 'after_state',
                  'completion_extent', 'primary_progress', 'secondary_progress',
                  'preserve_state'):
        assert beat[field] in directive, field


def test_a_prompt_pair_written_to_the_checklist_passes_every_audit():
    """End-to-end: the checklist is satisfiable — following it clears the same audits that
    flagged the 2026-07-30 run."""
    beat = _beat()
    image_prompt = (
        'The stud framing grid installed anchor now defines the compartment: a stud grid fills '
        'all bays across the entire interior, all fourteen bays framed out end to end. '
        'Screw heads catch the light along each stud face and fibre dust settles in the corners. '
        'The floor stays bare, its planking not yet laid.'
    )
    video_prompt = (
        'Continuous construction time-lapse, not real-time footage. At the very first moment the '
        'bare shell interior stands open; one lone worker enters carrying studs from a rigid crate '
        'stacked by the doorway, following that movement path to the wall line, and makes the first '
        'tool contact. The worker repeatedly performs the work cycle bay by bay, one by one: the '
        'stud count rising along the wall while the batt bundle shrinking beside the crate marks the '
        'stock draining. By the final moment a stud grid fills all bays across all fourteen bays, '
        'screw heads and fibre dust remain, and the worker exits the frame before the final frame, '
        'carrying tools and empty containers out.'
    )
    assert check_milestone_image_prompt(image_prompt, beat) == []
    assert check_stage_scope_wording(image_prompt, beat['stage_scope']) == []
    assert check_milestone_video_prompt(video_prompt, beat) == []
    assert pp.check_transition_shortcuts(video_prompt) == []
