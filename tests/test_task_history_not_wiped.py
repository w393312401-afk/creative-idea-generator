"""save_tasks_to_disk 的孤儿清理必须先拿到授权，否则会删光真实任务历史。

2026-07-31 实际事故：一个直接调 /api/compose 处理函数的测试，在项目根目录建了 1 条
内存任务 non-idea-compose；落盘时孤儿清理把 tasks/ 里 5 个真实任务记录（含刚跑完的
11 拍成品）全当垃圾删了。旧防呆只挡"内存表为空"，挡不住"内存表里只有一条刚塞进来的"。
授权条件收紧为：本进程必须成功跑完过一次 load_tasks_from_disk。
"""

import json
import os
import threading

import pytest

import server_common
from server_common import load_tasks_from_disk, save_tasks_to_disk


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('tasks', exist_ok=True)
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server_common, 'TASKS_LOADED_FROM_DISK', False)


def _seed_file(tid, status='completed'):
    payload = {
        'id': tid, 'status': status, 'events': [], 'dimensions': {'theme': 't'},
        'result': {'title': tid}, 'error': None, 'last_active': 1.0,
    }
    with open(os.path.join('tasks', f'{tid}.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f)


def _seed_memory(tid):
    server_common.ACTIVE_TASKS[tid] = {
        'id': tid, 'status': 'running', 'events': [], 'listeners': set(),
        'cancel_event': threading.Event(), 'dimensions': {'theme': 't'},
        'result': None, 'error': None, 'last_active': 2.0,
    }


def test_history_survives_a_save_from_a_process_that_never_loaded():
    """The exact accident: one fresh in-memory task, five real files on disk."""
    for tid in ('1785423021030', 'cover_abc', 'frames_abc', 'videos_abc', 'videos_def'):
        _seed_file(tid)
    _seed_memory('non-idea-compose')

    save_tasks_to_disk()

    remaining = sorted(os.listdir('tasks'))
    assert '1785423021030.json' in remaining
    assert len(remaining) == 6, remaining


def test_real_orphans_are_still_cleaned_once_history_is_loaded():
    _seed_file('900')
    _seed_file('901')
    load_tasks_from_disk()
    assert server_common.TASKS_LOADED_FROM_DISK is True

    del server_common.ACTIVE_TASKS['901']
    save_tasks_to_disk()

    assert sorted(os.listdir('tasks')) == ['900.json']


def test_empty_memory_table_still_short_circuits():
    _seed_file('900')
    save_tasks_to_disk()
    assert os.listdir('tasks') == ['900.json']
