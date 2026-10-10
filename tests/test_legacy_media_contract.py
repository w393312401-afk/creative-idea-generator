import ast
from pathlib import Path

import pytest

from legacy_media_contract import is_legacy_hard_cut_placeholder


@pytest.mark.parametrize('text, expected', [
    (None, False), ('', False), ('[CUT] push through the portal', False),
    ('  declared hard cut - no video clip', True),
    ('DECLARED HARD CUT', True), ('not a DECLARED HARD CUT', False),
])
def test_frozen_placeholder_recognition(text, expected):
    assert is_legacy_hard_cut_placeholder(text) is expected


def test_legacy_imports_share_one_implementation():
    import prompt_pipeline
    import video_generator
    assert prompt_pipeline.is_legacy_hard_cut_placeholder is is_legacy_hard_cut_placeholder
    assert video_generator._is_legacy_hard_cut_placeholder is is_legacy_hard_cut_placeholder


def test_compatibility_leaf_has_no_imports():
    path = Path(__file__).resolve().parents[1] / 'legacy_media_contract.py'
    tree = ast.parse(path.read_text())
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
