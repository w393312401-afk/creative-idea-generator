"""Dependency-free compatibility rules for previously delivered media manifests.

No imports from generators or prompt_pipeline: slot planning must stay usable
without loading the composition engine. New work never emits this placeholder.
"""

HARD_CUT_PLACEHOLDER_PREFIX = 'DECLARED HARD CUT'


def is_legacy_hard_cut_placeholder(body):
    """Recognize frozen pre-2026-07-30 placeholder text, not a [CUT] tag."""
    return str(body or '').strip().upper().startswith(HARD_CUT_PLACEHOLDER_PREFIX)
