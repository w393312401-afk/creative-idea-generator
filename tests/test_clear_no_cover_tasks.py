"""测试清空所有无封面任务（task_has_cover 以及 /api/tasks/clear 的 no_cover 分支）的行为契约。"""
import os
import json
import threading
import pytest

import server
import server_common


def test_task_has_cover_detection():
    # 1. 无封面任务
    task_no_cover = {
        'id': 'task_1',
        'status': 'completed',
        'dimensions': {'theme': '无封面项目', 'task_label': '无封面项目'},
        'result': {'title': '无封面项目', 'image_count': 10},
    }
    assert server_common.task_has_cover(task_no_cover, base_dir='/tmp', library_items=[]) is False

    # 2. 结果中带 covers
    task_with_covers = {
        'id': 'task_2',
        'status': 'completed',
        'dimensions': {'theme': '有封面项目'},
        'result': {'title': '有封面项目', 'covers': ['/outputs/test/cover_1.webp']},
    }
    assert server_common.task_has_cover(task_with_covers, base_dir='/tmp', library_items=[]) is True

    # 3. 结果中带 cover 单图字段
    task_with_single_cover = {
        'id': 'task_3',
        'status': 'completed',
        'dimensions': {'theme': '单封面项目'},
        'result': {'title': '单封面项目', 'cover': '/outputs/test/cover.jpg'},
    }
    assert server_common.task_has_cover(task_with_single_cover, base_dir='/tmp', library_items=[]) is True

    # 4. 点子库中已收藏并带封面
    library_items = [{
        'id': 'task_4',
        'title': '已收藏项目',
        'covers': ['/outputs/test/cover_saved.webp'],
    }]
    task_in_library = {
        'id': 'task_4',
        'status': 'completed',
        'dimensions': {'theme': '已收藏项目'},
        'result': {'title': '已收藏项目'},
    }
    assert server_common.task_has_cover(task_in_library, base_dir='/tmp', library_items=library_items) is True

    # 5. 点子库中有对应 project_key 且带封面
    task_pk = {
        'id': 'task_5',
        'status': 'completed',
        'dimensions': {'project_key': 'pk_5', 'theme': 'PK项目'},
        'result': {'project_key': 'pk_5', 'title': 'PK项目'},
    }
    lib_pk = [{
        'id': 'other_id',
        'project_key': 'pk_5',
        'title': 'PK项目',
        'activeCoverUrl': '/outputs/pk/cover.webp',
    }]
    assert server_common.task_has_cover(task_pk, base_dir='/tmp', library_items=lib_pk) is True


def test_clear_no_cover_tasks_endpoint(monkeypatch, tmp_path):
    deleted_files = []
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: deleted_files.append(tid))

    # 模拟 ACTIVE_TASKS
    tasks = {
        't_running': {
            'id': 't_running',
            'status': 'running',
            'cancel_event': threading.Event(),
            'dimensions': {'theme': '运行中无封面'},
            'result': None,
        },
        't_no_cover_completed': {
            'id': 't_no_cover_completed',
            'status': 'completed',
            'cancel_event': threading.Event(),
            'dimensions': {'theme': '已完成无封面'},
            'result': {'title': '已完成无封面', 'covers': []},
        },
        't_no_cover_failed': {
            'id': 't_no_cover_failed',
            'status': 'failed',
            'cancel_event': threading.Event(),
            'dimensions': {'theme': '失败无封面'},
            'result': None,
        },
        't_with_cover': {
            'id': 't_with_cover',
            'status': 'completed',
            'cancel_event': threading.Event(),
            'dimensions': {'theme': '有封面'},
            'result': {'title': '有封面', 'covers': ['/outputs/c/cover.webp']},
        },
    }

    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)
    monkeypatch.setattr(server_common, 'read_library', lambda *a, **k: [])

    # 构造请求 handler
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
    assert body['count'] == 2

    # 验证只有 t_no_cover_completed 和 t_no_cover_failed 被删除
    assert 't_no_cover_completed' not in tasks
    assert 't_no_cover_failed' not in tasks
    assert 't_running' in tasks  # 运行中的任务不应被清空
    assert 't_with_cover' in tasks  # 有封面的任务不应被清空

    assert set(deleted_files) == {'t_no_cover_completed', 't_no_cover_failed'}


def test_tasks_bulk_delete_and_safety(monkeypatch):
    deleted_files = []
    purged_outputs = []
    monkeypatch.setattr(server, 'delete_task_files', lambda tid: deleted_files.append(tid))
    monkeypatch.setattr(server, 'delete_idea_output_files', lambda title, covers=None: purged_outputs.append(title))

    tasks = {
        't1': {'id': 't1', 'status': 'completed', 'cancel_event': threading.Event(), 'result': {'title': '项目1'}},
        't2': {'id': 't2', 'status': 'completed', 'cancel_event': threading.Event(), 'result': {'title': '项目2'}},
        't3': {'id': 't3', 'status': 'running', 'cancel_event': threading.Event(), 'result': {'title': '项目3'}},
    }
    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)

    # 1. 批量删除 t1 和 t2
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/tasks/bulk_delete'
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {'task_ids': ['t1', 't2']}

    h.do_POST()

    assert sent[0][1] == 200
    assert sent[0][0]['count'] == 2
    assert 't1' not in tasks and 't2' not in tasks
    assert 't3' in tasks
    assert set(deleted_files) == {'t1', 't2'}

    # 核心安全契约：删除任务记录绝不能调用 delete_idea_output_files 物理抹除 outputs/ 成片
    assert len(purged_outputs) == 0

