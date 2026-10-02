"""conftest 的真实运行数据写保护：拦得住、不误伤。

用例里触发的拦截会登记违规；`caught` 在用例结束时核对并移除这些预期违规，
避免 autouse 的 teardown 检查把本文件自身判失败。
"""
import os
import shutil
import sys
import tempfile
import uuid

import pytest


def _conftest():
    return next(m for m in list(sys.modules.values())
                if getattr(m, '_guard_real_state', None) and hasattr(m, '_state_write_violations'))


@pytest.fixture
def caught():
    module = _conftest()
    start = len(module._state_write_violations)
    expected = []
    yield expected
    got = module._state_write_violations[start:]
    del module._state_write_violations[start:]
    assert len(got) == len(expected), got
    for line, needle in zip(got, expected):
        assert needle in line, (needle, line)


def _probe(*parts):
    # 唯一且不存在的文件名：守卫失效时也不会碰到任何真实文件。
    return os.path.join(_conftest()._REPO_ROOT, *parts[:-1], f'{parts[-1]}{uuid.uuid4().hex}.json')


def test_blocks_write_into_runtime(caught):
    path = _probe('runtime', 'isolation_probe_')
    caught.append('open runtime/isolation_probe_')
    with pytest.raises(PermissionError, match='TEST_ISOLATION'):
        open(path, 'w')
    assert not os.path.exists(path)


def test_blocks_root_state_file_delete_rename_and_rmtree(caught):
    root_file = _probe('state_probe_')
    caught.extend(['os.remove state_probe_', 'os.rename ', 'shutil.rmtree outputs/'])
    with pytest.raises(PermissionError):
        os.remove(root_file)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'x.json')
        open(src, 'w').close()
        with pytest.raises(PermissionError):
            os.replace(src, root_file)
    with pytest.raises(PermissionError):
        shutil.rmtree(_probe('outputs', 'tree_probe_')[:-5])


def test_allows_tmp_cleanup_reads_and_whitelisted_paths(caught, tmp_path):
    module = _conftest()
    # TemporaryDirectory 清理按 dir_fd 删 manifest.json 这类裸名，不能被当成仓库根的状态文件。
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'sub'))
        open(os.path.join(tmp, 'sub', 'manifest.json'), 'w').close()
    (tmp_path / 'library.json').write_text('{}')
    with open(os.path.join(module._REPO_ROOT, 'pyproject.toml'), 'r', encoding='utf-8') as f:
        assert f.read()
    assert module._real_state_path(os.path.join(module._REPO_ROOT, 'outputs', 'e2e_restore_demo', 'a')) is None
    assert module._real_state_path(os.path.join(module._REPO_ROOT, 'tests', 'x.json')) is None
    assert module._real_state_path(os.path.join(module._REPO_ROOT, 'library.json')) == 'library.json'
