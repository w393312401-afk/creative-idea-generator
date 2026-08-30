"""测试项目工作台彻底删除（/api/projects/delete）、点子库批量删除及清空契约。"""

import os
import shutil
import threading
import pytest
import server
import server_common


def test_delete_library_items_and_clear_regular(tmp_path, monkeypatch):
    """测试 delete_library_items 批量删除与 clear_regular_library 清空常规点子库。"""
    lib_dir = tmp_path / "test_lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_common, 'LIBRARY_DIR', str(lib_dir))

    # 写入 3 个常规条目 + 1 个复刻条目
    item1 = {'id': 'idea_1', 'title': '常规点子1', 'project_key': 'p1'}
    item2 = {'id': 'idea_2', 'title': '常规点子2', 'project_key': 'p2'}
    item3 = {'id': 'idea_3', 'title': '常规点子3', 'project_key': 'p3'}
    item_replica = {
        'id': 'replica_job_99',
        'title': '复刻创意 · 爆款 1:1 复刻',
        'source': 'replica',
        'replica_job_id': 'replica_job_99',
        'project_key': 'replica_p99'
    }

    server_common.write_library_item(item1, library_dir=str(lib_dir))
    server_common.write_library_item(item2, library_dir=str(lib_dir))
    server_common.write_library_item(item3, library_dir=str(lib_dir))
    server_common.write_library_item(item_replica, library_dir=str(lib_dir))

    # 1. 测试 delete_library_items 批量删除 item1 和 item2
    deleted = server_common.delete_library_items(['idea_1', 'idea_2'], library_dir=str(lib_dir))
    assert 'idea_1' in deleted
    assert 'idea_2' in deleted
    assert server_common.read_library_item('idea_1', library_dir=str(lib_dir)) is None
    assert server_common.read_library_item('idea_2', library_dir=str(lib_dir)) is None
    assert server_common.read_library_item('idea_3', library_dir=str(lib_dir)) is not None
    assert server_common.read_library_item('replica_job_99', library_dir=str(lib_dir)) is not None

    # 2. 测试 clear_regular_library 清空剩余常规点子库（idea_3 被清，replica_job_99 保留）
    cleared = server_common.clear_regular_library(library_dir=str(lib_dir))
    assert 'idea_3' in cleared
    assert 'replica_job_99' not in cleared
    assert server_common.read_library_item('idea_3', library_dir=str(lib_dir)) is None
    assert server_common.read_library_item('replica_job_99', library_dir=str(lib_dir)) is not None


def test_api_projects_delete_endpoint(tmp_path, monkeypatch):
    """测试 /api/projects/delete 端点彻底清理项目、任务与资产，并豁免复刻项目。"""
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', str(out_dir))
    monkeypatch.setattr(server.server_common, 'OUTPUT_ROOT', str(out_dir))

    # 准备磁盘媒体目录
    p1_dir = out_dir / "proj_normal_1"
    p1_dir.mkdir(parents=True, exist_ok=True)
    (p1_dir / "frame_01.webp").write_text("frame data")

    replica_dir = out_dir / "replica_jobs" / "replica_01"
    replica_dir.mkdir(parents=True, exist_ok=True)
    (replica_dir / "video.mp4").write_text("video data")

    # 准备 ACTIVE_TASKS
    tasks = {
        'task_proj_1': {
            'id': 'task_proj_1',
            'status': 'completed',
            'dimensions': {'type': 'idea', 'theme': '常规项目1'},
            'cancel_event': threading.Event(),
        },
        'sub_job_1': {
            'id': 'sub_job_1',
            'status': 'completed',
            'dimensions': {'type': 'frames', 'theme': '常规项目1'},
            'cancel_event': threading.Event(),
        },
        'task_replica_1': {
            'id': 'task_replica_1',
            'status': 'completed',
            'dimensions': {'type': 'replica_advance', 'replica_job_id': 'replica_01'},
            'cancel_event': threading.Event(),
        },
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)

    deleted_task_files = []
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: deleted_task_files.append(tid))

    deleted_library_ids = []
    def mock_del_lib(ids, library_dir=None):
        deleted_library_ids.extend(ids)
        return ids
    monkeypatch.setattr(server, 'delete_library_items', mock_del_lib)

    # 待删除项目列表（包含 1 个常规项目，1 个复刻项目）
    projects_to_delete = [
        {
            'project_key': 'proj_normal_1',
            'title': '常规项目1',
            'task': {'id': 'task_proj_1'},
            'sub_jobs': [{'id': 'sub_job_1'}],
            'library': {'id': 'lib_1'},
            'saved': True,
        },
        {
            'project_key': 'replica_01',
            'title': '复刻项目 · 爆款 1:1 复刻',
            'task': {'id': 'task_replica_1'},
            'library': {'id': 'replica_01'},
            'saved': True,
        }
    ]

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/projects/delete'
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {'projects': projects_to_delete}

    h.do_POST()

    body, status = sent[0]
    assert status == 200
    assert body['status'] == 'ok'

    # 显式选中的常规项目与复刻项目均被彻底清理
    assert 'task_proj_1' not in tasks
    assert 'sub_job_1' not in tasks
    assert 'task_replica_1' not in tasks
    assert 'task_proj_1' in deleted_task_files
    assert 'sub_job_1' in deleted_task_files
    assert 'task_replica_1' in deleted_task_files

    # 点子库条目被清理
    assert 'lib_1' in deleted_library_ids
    assert 'replica_01' in deleted_library_ids

    # 常规项目目录被清理
    assert not p1_dir.exists()
