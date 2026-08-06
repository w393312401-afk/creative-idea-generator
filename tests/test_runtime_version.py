"""运行时版本指纹（server_common.runtime_version_report / code_staleness_report）。

背景：2026-08-06 一次失败任务跑在旧进程上，修复它的代码已经落盘却没生效——旧进程
没重启。这层把"这个进程是不是还在跑旧代码"变成一个可读字段，而不是要靠人去猜。
"""

import os
import time

import pytest

import server_common
from server_common import code_staleness_report, runtime_version_report


@pytest.fixture
def _fake_source_tree(tmp_path, monkeypatch):
    """把核心源文件的搜索范围钉死在一个临时目录，不touch真实仓库文件。"""
    (tmp_path / 'prompt_pipeline').mkdir()
    server_file = tmp_path / 'server.py'
    server_file.write_text('# server\n', encoding='utf-8')
    pipeline_file = tmp_path / 'prompt_pipeline' / 'core.py'
    pipeline_file.write_text('# core\n', encoding='utf-8')
    monkeypatch.setattr(server_common, '_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setattr(server_common, '_CORE_SOURCE_GLOBS', (
        'server.py', os.path.join('prompt_pipeline', '*.py'),
    ))
    return server_file, pipeline_file


def test_not_stale_when_nothing_changed_after_start(_fake_source_tree, monkeypatch):
    server_file, pipeline_file = _fake_source_tree
    monkeypatch.setattr(server_common, 'SERVICE_START_TIME', time.time() + 5)
    report = code_staleness_report()
    assert report == {'stale': False, 'stale_files': []}


def test_stale_when_a_core_file_is_edited_after_service_start(_fake_source_tree, monkeypatch):
    server_file, pipeline_file = _fake_source_tree
    start = time.time()
    monkeypatch.setattr(server_common, 'SERVICE_START_TIME', start)
    # 服务"启动"之后再摸一次这个文件，模拟"改完代码没重启"
    future = start + 5
    os.utime(pipeline_file, (future, future))
    report = code_staleness_report()
    assert report['stale'] is True
    assert report['stale_files'] == ['prompt_pipeline/core.py']


def test_runtime_version_report_has_the_expected_shape(_fake_source_tree, monkeypatch):
    monkeypatch.setattr(server_common, 'SERVICE_START_TIME', time.time() + 5)
    report = runtime_version_report()
    assert set(report) == {
        'git_commit', 'git_commit_short', 'git_dirty', 'service_start_time',
        'skill_profile', 'stale', 'stale_files',
    }
    assert report['stale'] is False


def test_get_or_create_task_stamps_runtime_version(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    os.makedirs('tasks', exist_ok=True)
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server_common, 'TASKS_LOADED_FROM_DISK', False)
    monkeypatch.setattr(server_common, '_TASK_FLUSHED_EVENTS', {})
    t = server_common.get_or_create_task('rv-1', {'theme': 'x'})
    assert isinstance(t.get('runtime_version'), dict)
    assert 'stale' in t['runtime_version']
