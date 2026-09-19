"""Exercise the skill's copy-ready example through actual video boundary/shot gates."""
from pathlib import Path
import re

import prompt_pipeline as pp
from prompt_pipeline.composers.omni import (
    OMNI_INSHOT_MARKER, OMNI_PACING_MARKER,
    ensure_ladder_out_and_in, ladder_for, omni_video_violations, video_word_targets,
)


SKILL = Path(__file__).resolve().parents[1] / 'skills/gemini-omni-restoration-composer'


def example_slots():
    example = (SKILL / 'examples/minimal-omni-restoration.md').read_text()
    block = next(block for block in re.findall(r'```text\n(.*?)\n```', example, re.S)
                 if block.startswith('图片提示词'))
    return pp._parse_prompt_slots(block)


def test_example_preserves_person_free_boundaries_without_added_choreography():
    images, videos = example_slots()
    assert sorted(images) == [1, 2]
    assert sorted(videos) == [1]
    body = videos[1]['body']
    assert pp.check_out_and_in(body) == []
    assert pp.fix_out_and_in(body) == body
    assert ensure_ladder_out_and_in(body, ladder_for(10)) == body
    for slot in images.values():
        assert not re.search(r'\b(worker|person|hands|torso)\b', slot['body'], re.I)


def test_example_keeps_shot_contract_and_budget():
    _, videos = example_slots()
    body = videos[1]['body']
    assert omni_video_violations(body, ladder=ladder_for(10), duration=10) == []
    assert len(body.split()) <= video_word_targets(4)[1]
    assert OMNI_INSHOT_MARKER in body.lower()
    assert OMNI_PACING_MARKER in body.lower()


def test_work_first_reference_is_reachable_from_composition_instructions():
    # The behavior guide must be discoverable by direct skill invocation, not just app code.
    main = (SKILL / 'SKILL.md').read_text()
    required = main.split('**Always load, every composition run:**', 1)[1].split(
        '**Load conditionally:**', 1)[0]
    refs = re.findall(r'`(references/[^`]+\.md)`', required)
    assert 'references/omni-work-first-rhythm.md' in refs
    for ref in refs:
        assert (SKILL / ref).is_file(), ref
