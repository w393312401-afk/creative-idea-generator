"""测试彻底清空（/api/tasks/clear）及各状态清空对爆款复刻任务与资产的绝对保护机制。"""
import os
import json
import shutil
import tempfile
import threading
import pytest

import server
import server_common


def test_is_replica_task_identification():
    # 1. 常规任务
    regular_task = {
        'id': 'idea_123',
        'status': 'completed',
        'dimensions': {'type': 'idea', 'theme': '常规改造项目'},
        'result': {'title': '常规改造项目'},
    }
    assert server_common.is_replica_task(regular_task) is False

    # 2. 任务 ID 以 replica 开头
    task_by_id = {
        'id': 'replica_abc123',
        'status': 'completed',
        'dimensions': {'theme': '复刻测试'},
    }
    assert server_common.is_replica_task(task_by_id) is True

    # 3. dimensions.type 为 replica 相关
    task_by_type = {
        'id': 'task_789',
        'dimensions': {'type': 'replica_advance'},
    }
    assert server_common.is_replica_task(task_by_type) is True

    # 4. dimensions.replica_job_id
    task_by_job = {
        'id': 'task_job_1',
        'dimensions': {'replica_job_id': 'replica_999'},
    }
    assert server_common.is_replica_task(task_by_job) is True

    # 5. source 为 replica
    task_by_src = {
        'id': 'task_src_1',
        'dimensions': {'source': 'replica'},
    }
    assert server_common.is_replica_task(task_by_src) is True

    # 6. 标题带有爆款复刻 / 二创变体特征
    task_by_title = {
        'id': 'task_title_1',
        'result': {'title': '海蚀洞改造 · 爆款 1:1 复刻 · TikTok.mp4'},
    }
    assert server_common.is_replica_task(task_by_title) is True


def test_is_replica_library_item_identification():
    # 1. 常规创意条目
    regular_item = {
        'id': 'item_1',
        'title': '常规木屋设计',
        'theme': '海景木屋',
        'creativity': '创意激发',
    }
    assert server_common.is_replica_library_item(regular_item) is False

    # 2. id 以 replica 开头
    replica_item_id = {
        'id': 'replica_456',
        'title': '复刻木屋',
    }
    assert server_common.is_replica_library_item(replica_item_id) is True

    # 3. source == 'replica'
    replica_item_src = {
        'id': 'custom_id_1',
        'source': 'replica',
        'title': '自建复刻条目',
    }
    assert server_common.is_replica_library_item(replica_item_src) is True

    # 4. replica_job_id
    replica_item_job = {
        'id': 'custom_id_2',
        'replica_job_id': 'replica_777',
    }
    assert server_common.is_replica_library_item(replica_item_job) is True

    # 5. creativity 字段
    replica_item_creativity = {
        'id': 'custom_id_3',
        'creativity': '爆款 1:1 复刻',
    }
    assert server_common.is_replica_library_item(replica_item_creativity) is True

    # 6. 标题或主题包含特征
    replica_item_title = {
        'id': 'custom_id_4',
        'title': '海边岩洞 · 爆款 1:1 复刻 · test.mp4',
    }
    assert server_common.is_replica_library_item(replica_item_title) is True


def test_is_replica_protected_path(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    replica_jobs_dir = out_dir / "replica_jobs"
    replica_jobs_dir.mkdir(parents=True, exist_ok=True)
    specific_job = replica_jobs_dir / "replica_001"
    specific_job.mkdir(parents=True, exist_ok=True)
    normal_proj = out_dir / "run_normal_proj"
    normal_proj.mkdir(parents=True, exist_ok=True)

    # 保护项
    assert server_common.is_replica_protected_path("replica_jobs", base_dir=str(tmp_path)) is True
    assert server_common.is_replica_protected_path(".gitkeep", base_dir=str(tmp_path)) is True
    assert server_common.is_replica_protected_path(str(replica_jobs_dir), base_dir=str(tmp_path)) is True
    assert server_common.is_replica_protected_path(str(specific_job), base_dir=str(tmp_path)) is True
    assert server_common.is_replica_protected_path("run_replica_some_title", base_dir=str(tmp_path)) is True

    # 非保护项
    assert server_common.is_replica_protected_path(str(normal_proj), base_dir=str(tmp_path)) is False


def test_clear_all_preserves_replica(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_common, 'OUTPUT_ROOT', str(out_dir))
    monkeypatch.setattr(server.server_common, 'OUTPUT_ROOT', str(out_dir))

    # 准备 outputs/ 下的文件结构
    replica_jobs_dir = out_dir / "replica_jobs"
    replica_jobs_dir.mkdir(parents=True, exist_ok=True)
    job_file = replica_jobs_dir / "replica_01" / "test.mp4"
    job_file.parent.mkdir(parents=True, exist_ok=True)
    job_file.write_text("dummy video")

    normal_dir = out_dir / "run_normal_123"
    normal_dir.mkdir(parents=True, exist_ok=True)
    normal_file = normal_dir / "frame_01.webp"
    normal_file.write_text("dummy frame")

    # 准备 ACTIVE_TASKS
    tasks = {
        'task_regular': {
            'id': 'task_regular',
            'status': 'completed',
            'dimensions': {'type': 'idea', 'theme': '常规任务'},
            'cancel_event': threading.Event(),
        },
        'replica_task_1': {
            'id': 'replica_task_1',
            'status': 'completed',
            'dimensions': {'type': 'replica_advance', 'replica_job_id': 'replica_01'},
            'cancel_event': threading.Event(),
        },
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)

    # 准备 library_items
    library_items = [
        {'id': 'lib_regular_1', 'title': '常规创意', 'covers': []},
        {'id': 'replica_01', 'title': '复刻创意 · 爆款 1:1 复刻', 'source': 'replica', 'replica_job_id': 'replica_01'},
    ]
    monkeypatch.setattr(server, 'read_library', lambda: list(library_items))

    deleted_library_ids = []
    def mock_del_lib(iid):
        deleted_library_ids.append(iid)
        return True
    monkeypatch.setattr(server, 'delete_library_item', mock_del_lib)
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: None)

    # 构造请求 handler 调用 /api/tasks/clear status_group="all"
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/tasks/clear'
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {'status_group': 'all'}

    h.do_POST()

    body, status = sent[0]
    assert status == 200
    assert body['status'] == 'ok'

    # 1. 验证常规任务被删，复刻任务保留
    assert 'task_regular' not in tasks
    assert 'replica_task_1' in tasks

    # 2. 验证常规点子被删，复刻点子保留
    assert 'lib_regular_1' in deleted_library_ids
    assert 'replica_01' not in deleted_library_ids

    # 3. 验证 outputs/ 目录下常规项目目录被删，replica_jobs 完好无损
    assert not normal_dir.exists()
    assert replica_jobs_dir.exists()
    assert job_file.exists()


def test_clear_no_cover_preserves_replica(tmp_path, monkeypatch):
    tasks = {
        'task_regular_no_cov': {
            'id': 'task_regular_no_cov',
            'status': 'completed',
            'dimensions': {'theme': '无封面常规'},
            'result': {'title': '无封面常规'},
            'cancel_event': threading.Event(),
        },
        'replica_no_cov': {
            'id': 'replica_no_cov',
            'status': 'completed',
            'dimensions': {'type': 'replica_advance', 'replica_job_id': 'replica_x'},
            'result': {'title': '复刻未出图'},
            'cancel_event': threading.Event(),
        },
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)

    library_items = [
        {'id': 'lib_no_cov', 'title': '无封面常规收藏', 'covers': []},
        {'id': 'replica_x', 'title': '复刻无封面收藏', 'source': 'replica', 'replica_job_id': 'replica_x', 'covers': []},
    ]
    monkeypatch.setattr(server, 'read_library', lambda: list(library_items))

    deleted_library_ids = []
    monkeypatch.setattr(server, 'delete_library_item', lambda iid: deleted_library_ids.append(iid) or True)
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: None)

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/tasks/clear'
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {'status_group': 'no_cover'}

    h.do_POST()

    body, status = sent[0]
    assert status == 200
    assert body['status'] == 'ok'

    # 常规无封面任务被清理，复刻任务保留
    assert 'task_regular_no_cov' not in tasks
    assert 'replica_no_cov' in tasks

    # 常规无封面收藏被清理，复刻收藏保留
    assert 'lib_no_cov' in deleted_library_ids
    assert 'replica_x' not in deleted_library_ids


def test_clear_completed_preserves_replica(monkeypatch):
    tasks = {
        'task_completed_regular': {
            'id': 'task_completed_regular',
            'status': 'completed',
            'dimensions': {'type': 'idea', 'theme': '已完成常规'},
            'cancel_event': threading.Event(),
        },
        'replica_completed': {
            'id': 'replica_completed',
            'status': 'completed',
            'dimensions': {'type': 'replica', 'replica_job_id': 'replica_comp'},
            'cancel_event': threading.Event(),
        },
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: None)

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/tasks/clear'
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {'status_group': 'completed'}

    h.do_POST()

    body, status = sent[0]
    assert status == 200
    assert 'task_completed_regular' not in tasks
    assert 'replica_completed' in tasks
