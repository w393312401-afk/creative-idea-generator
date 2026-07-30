"""check_transition_shortcuts must not flag the pipeline's own prohibition boilerplate.

The VIDEO template emits "Transition shortcuts like cross-dissolves, fade-ins, or jump cuts
are strictly forbidden." — clean_prompt_text() already treats that sentence as a rule
statement (negation-aware), so the audit must agree, otherwise every single beat reports a
phantom style violation and drags its IMAGE prompt into an unnecessary rework.
"""

from prompt_pipeline import check_transition_shortcuts

BOILERPLATE = (
    "Transition shortcuts like cross-dissolves, fade-ins, or jump cuts are strictly forbidden."
)


def test_own_boilerplate_is_not_flagged():
    prompt = (
        "The lone worker carries a bundle of pine boards in from a stacked crate. "
        + BOILERPLATE
        + " Sound effects: rhythmic thunk of a manual stapler."
    )
    assert check_transition_shortcuts(prompt) == []


def test_negated_variants_are_not_flagged():
    for sentence in (
        "No jump cut is used at any point.",
        "The build never suddenly appears; every course is laid by hand.",
        "Avoid any time skip between the first contact and the final frame.",
    ):
        assert check_transition_shortcuts(sentence) == []


def test_genuine_shortcut_is_still_flagged():
    errors = check_transition_shortcuts(
        "A jump cut lands on the finished roof as the transformation progresses."
    )
    assert len(errors) == 2
    assert any("jump cut" in e for e in errors)
    assert any("transformation progresses" in e for e in errors)


def test_shortcut_reported_once_even_when_repeated():
    errors = check_transition_shortcuts(
        "A jump cut opens the clip. Another jump cut closes it."
    )
    assert len(errors) == 1


def test_shortcut_in_a_clean_sentence_survives_a_negated_neighbour():
    errors = check_transition_shortcuts(BOILERPLATE + " The wall magically completes.")
    assert len(errors) == 1
    assert "magically" in errors[0]
