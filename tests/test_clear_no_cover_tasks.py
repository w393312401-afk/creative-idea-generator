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

    # 6. 结果中带 frameRun 帧序列（实际在激发结果中已有媒体）
    task_with_framerun = {
        'id': 'task_6',
        'status': 'completed',
        'dimensions': {'theme': '帧序列项目'},
        'result': {
            'title': '帧序列项目',
            'frameRun': {
                'frames': [
                    {'url': '/outputs/frames/frame_01.webp', 'prompt': '...'},
                    {'url': '/outputs/frames/frame_02.webp', 'prompt': '...'},
                ]
            }
        }
    }
    assert server_common.task_has_cover(task_with_framerun, base_dir='/tmp', library_items=[]) is True


def test_library_item_has_cover_detection(monkeypatch):
    # 1. 无封面条目
    item_no_cover = {
        'id': 'lib_1',
        'title': '无封面收藏',
        'covers': [],
    }
    assert server_common.library_item_has_cover(item_no_cover, base_dir='/tmp') is False

    # 2. 带 covers 数组
    item_with_covers = {
        'id': 'lib_2',
        'title': '带封面数组收藏',
        'covers': ['/outputs/test/cover_1.webp'],
    }
    assert server_common.library_item_has_cover(item_with_covers, base_dir='/tmp') is True

    # 3. 带 activeCoverUrl
    item_with_active_cover = {
        'id': 'lib_3',
        'title': '主封面收藏',
        'activeCoverUrl': '/outputs/test/main_cover.webp',
    }
    assert server_common.library_item_has_cover(item_with_active_cover, base_dir='/tmp') is True

    # 4. 带 coverRoles.project
    item_with_roles = {
        'id': 'lib_4',
        'title': '角色封面收藏',
        'coverRoles': {'project': '/outputs/test/project_cover.webp'},
    }
    assert server_common.library_item_has_cover(item_with_roles, base_dir='/tmp') is True

    # 5. 磁盘资产中包含封面
    monkeypatch.setattr(server_common, '_proj_asset_stats', lambda pk, title, bdir: {'cover': '/outputs/disk/cover.webp'})
    item_with_disk_cover = {
        'id': 'lib_5',
        'project_key': 'pk_disk',
        'title': '磁盘封面收藏',
    }
    assert server_common.library_item_has_cover(item_with_disk_cover, base_dir='/tmp') is True

    # 6. 带 frameRun.frames
    item_with_framerun = {
        'id': 'lib_6',
        'title': '帧序列收藏',
        'frameRun': {
            'frames': [{'url': '/outputs/lib6/frame_01.png'}]
        }
    }
    assert server_common.library_item_has_cover(item_with_framerun, base_dir='/tmp') is True


def test_clear_no_cover_tasks_endpoint(monkeypatch, tmp_path):
    deleted_task_files = []
    deleted_library_items = []
    deleted_output_titles = []

    monkeypatch.setattr(server, 'delete_task_files', lambda tid: deleted_task_files.append(tid))
    monkeypatch.setattr(server, 'delete_library_item', lambda iid: deleted_library_items.append(iid) or True)
    monkeypatch.setattr(server, 'delete_idea_output_files', lambda t, c=None: deleted_output_titles.append(t))

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

    # 模拟 library_items（包含有封面和无封面的收藏条目）
    lib_items = [
        {
            'id': 'lib_no_cover_1',
            'project_key': 'pk_no_cov_1',
            'title': '已收藏无封面1',
            'covers': [],
        },
        {
            'id': 'lib_no_cover_2',
            'project_key': 'pk_no_cov_2',
            'title': '已收藏无封面2',
        },
        {
            'id': 'lib_with_cover',
            'project_key': 'pk_with_cov',
            'title': '已收藏有封面',
            'covers': ['/outputs/pk_with_cov/cover_0.webp'],
        },
    ]

    monkeypatch.setattr(server, 'ACTIVE_TASKS', tasks)
    monkeypatch.setattr(server, 'read_library', lambda *a, **k: lib_items)

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
    # 2 个任务 + 2 个收藏 = 4 项
    assert body['count'] == 4
    assert body['deleted_task_count'] == 2
    assert body['deleted_library_count'] == 2
    assert set(body['deleted_library_ids']) == {'lib_no_cover_1', 'lib_no_cover_2'}

    # 验证只有 t_no_cover_completed 和 t_no_cover_failed 被删除
    assert 't_no_cover_completed' not in tasks
    assert 't_no_cover_failed' not in tasks
    assert 't_running' in tasks  # 运行中的任务不应被清空
    assert 't_with_cover' in tasks  # 有封面的任务不应被清空

    assert set(deleted_task_files) == {'t_no_cover_completed', 't_no_cover_failed'}
    assert set(deleted_library_items) == {'lib_no_cover_1', 'lib_no_cover_2'}
    assert set(deleted_output_titles) == {'pk_no_cov_1', 'pk_no_cov_2'}


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

