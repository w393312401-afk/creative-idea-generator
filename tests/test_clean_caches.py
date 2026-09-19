from tools.clean_caches import candidates, clean


def test_only_regenerable_untracked_files_are_removed(tmp_path):
    names = ['tests/__pycache__/test_x.pyc', '.pytest_cache/v/cache/nodeids',
             'tests/.DS_Store', 'tests/__pycache__/keep.pyc', 'tests/test_x.py',
             'tests/__pycache__/notes.txt', 'outputs/__pycache__/media.pyc',
             '.venv/__pycache__/package.pyc', 'runtime/.DS_Store', '.git/.DS_Store']
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('data')
    selected = list(candidates(tmp_path, ['tests/__pycache__/keep.pyc']))
    assert {str(p.relative_to(tmp_path)) for p in selected} == set(names[:3])
    assert clean(tmp_path, selected) == 12
    assert all((tmp_path / name).exists() for name in names[3:])
    # A stale or hand-crafted cleanup list must still preserve source and tracked files.
    assert clean(tmp_path, [tmp_path / names[3], tmp_path / names[4]], [names[3]]) == 0


def test_symlinked_cache_and_changed_ancestry_are_preserved(tmp_path):
    import pytest
    outside = tmp_path / 'outputs'
    outside.mkdir()
    protected = outside / 'valuable.pyc'
    protected.write_text('keep')
    link = tmp_path / '__pycache__'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink unavailable')
    assert list(candidates(tmp_path)) == []
    assert clean(tmp_path, [link / 'valuable.pyc']) == 0
    assert protected.read_text() == 'keep'
