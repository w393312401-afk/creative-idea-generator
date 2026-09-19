"""Safety contracts for the read-only inventory and disposable baseline runner."""
import os
from pathlib import Path

import pytest

from tools.offline_pytest import make_guard
from tools import project_audit


def test_offline_guard_allows_only_disposable_writes(tmp_path):
    guard = make_guard(tmp_path)
    guard('open', (str(tmp_path / 'state.json'), 'w', os.O_WRONLY))
    guard('open', (os.devnull, 'w', os.O_WRONLY))
    with pytest.raises(PermissionError, match='outside disposable'):
        guard('open', (str(tmp_path.parent / 'production.json'), 'w', os.O_WRONLY))
    guard('open', (str(tmp_path.parent / 'source.py'), 'r', os.O_RDONLY))


@pytest.mark.parametrize('event', ['socket.connect', 'socket.connect_ex', 'socket.sendto',
                                  'subprocess.Popen', 'os.system', 'os.posix_spawn'])
def test_offline_guard_blocks_external_effects(tmp_path, event):
    with pytest.raises(PermissionError, match='OFFLINE_GUARD'):
        make_guard(tmp_path)(event, ())


def test_offline_guard_checks_both_rename_paths(tmp_path):
    with pytest.raises(PermissionError):
        make_guard(tmp_path)('os.rename', (str(tmp_path / 'a'), str(tmp_path.parent / 'b')))


def test_source_copy_excludes_secrets_assets_and_symlinks(tmp_path, monkeypatch):
    names = ['server.py', 'server_config.json', 'library.json', 'server_config.example.json',
             'artifacts/private.txt', 'tests/test_x.py', 'runtime/account_pool.json']
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    monkeypatch.setattr(project_audit, 'git_output', lambda *args: '\0'.join(names))
    assert {str(path) for path in project_audit.source_files(tmp_path)} == {
        'server.py', 'server_config.example.json', 'tests/test_x.py'}


def test_inventory_counts_files_not_symlinks(tmp_path):
    (tmp_path / 'a.txt').write_text('123')
    assert project_audit.tree_bytes(tmp_path) == 3


def test_guard_resolves_symlink_escape(tmp_path):
    link = tmp_path / 'escape'
    try:
        link.symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable on this platform')
    with pytest.raises(PermissionError):
        make_guard(tmp_path)('open', (str(link / 'outside'), 'w', os.O_WRONLY))
