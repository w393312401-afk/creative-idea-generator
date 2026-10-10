"""普通项目展示补入根目录的节拍文件，不扫描精剪工作目录或持久化到点子库。"""
from copy import deepcopy
from urllib.parse import unquote

import pytest

import server_common as store


def _project(tmp_path):
    key = 'run_beats__节拍项目'
    directory = tmp_path / 'outputs' / store._safe_project_name(key)
    directory.mkdir(parents=True)
    return key, directory


def test_root_beat_files_are_listed_without_recursive_walk(tmp_path, monkeypatch):
    key, directory = _project(tmp_path)
    names = ['反推节拍.json', 'beat_package.json', '节拍阶梯.md', '节拍数据.json']
    for name in names:
        (directory / name).write_text('{}', encoding='utf-8')
    (directory / '普通资料.json').write_text('{}', encoding='utf-8')
    nested = directory / 'codex_edits' / 'work' / 'frames'
    nested.mkdir(parents=True)
    (nested / 'beats.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(store.os, 'walk', lambda *args, **kwargs: pytest.fail('beat files recursively scanned media'))

    files = store._proj_beat_files(key, '节拍项目', str(tmp_path))

    assert {file['name'] for file in files} == set(names)
    assert {file['kind'] for file in files} == {'beats'}
    for file in files:
        assert unquote(file['url']) == f'/outputs/{directory.name}/{file["name"]}'


def test_beat_file_links_do_not_follow_symlinks(tmp_path):
    key, directory = _project(tmp_path)
    outside = tmp_path / 'private.json'
    outside.write_text('{}', encoding='utf-8')
    (directory / '反推节拍.json').symlink_to(outside)
    (directory / '节拍数据.json').mkdir()

    assert store._proj_beat_files(key, '节拍项目', str(tmp_path)) == []


def test_full_index_exposes_beats_without_mutating_library(tmp_path):
    key, directory = _project(tmp_path)
    (directory / '反推节拍.json').write_text('{}', encoding='utf-8')
    item = {'id': 'beat-library', 'project_key': key, 'title': '节拍项目', 'prompt_block': '原提示词'}
    before = deepcopy(item)

    row = store.build_projects_index(tasks=[], library_items=[item], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=True)[0]

    assert [file['name'] for file in row['beat_files']] == ['反推节拍.json']
    assert item == before
    assert 'beat_files' not in item


def test_light_index_lists_beats_without_scanning_media_and_detects_removal(tmp_path, monkeypatch):
    key, directory = _project(tmp_path)
    file = directory / '反推节拍.json'
    file.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(store, '_proj_asset_stats', lambda *args: pytest.fail('light poll scanned media assets'))
    monkeypatch.setattr(store.os, 'walk', lambda *args, **kwargs: pytest.fail('light poll recursively scanned media'))

    row = store.build_projects_index(tasks=[], library_items=[{'id': 'beat-library',
        'project_key': key, 'title': '节拍项目'}], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=False)[0]

    assert [entry['name'] for entry in row['beat_files']] == ['反推节拍.json']
    file.unlink()
    row = store.build_projects_index(tasks=[], library_items=[{'id': 'beat-library',
        'project_key': key, 'title': '节拍项目'}], ledger_rows=[],
        base_dir=str(tmp_path), with_assets=False)[0]
    assert row['beat_files'] == []
