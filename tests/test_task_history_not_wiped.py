"""任务历史不会被一次落盘动作误删。

2026-07-31 实际事故：一个直接调 /api/compose 处理函数的测试，在项目根目录建了 1 条
内存任务 non-idea-compose；落盘时孤儿清理把 tasks/ 里 5 个真实任务记录（含刚跑完的
11 拍成品）全当垃圾删了。旧防呆只挡"内存表为空"，挡不住"内存表里只有一条刚塞进来的"。
授权条件因此收紧为：本进程必须成功跑完过一次 load_tasks_from_disk。

2026-07-31 拆分存储（P2）之后，防线本身更进了一步：**落盘动作不再有能力删除任何
东西**。孤儿清理从 save_tasks_to_disk 里剥离成独立的 prune_orphan_task_files()，
只在启动加载完成后跑一次；删除任务改走显式的 delete_task_files()。所以下面分两组：

- 前两条：save_* 系列绝不删文件（新的结构性保证，比"授权条件"更强）；
- 后三条：那两段防呆原样转移到 prune_orphan_task_files 上，继续回归。
"""

import json
import os
import threading

import pytest

import server_common
from server_common import (
    load_tasks_from_disk,
    save_tasks_to_disk,
    save_task_to_disk,
    prune_orphan_task_files,
    delete_task_files,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('tasks', exist_ok=True)
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server_common, 'TASKS_LOADED_FROM_DISK', False)
    monkeypatch.setattr(server_common, '_TASK_FLUSHED_EVENTS', {})


def _seed_file(tid, status='completed'):
    """老格式（events/result 内联在 meta 里）—— 迁移路径也一并覆盖到。"""
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


def _meta_files():
    return sorted(f for f in os.listdir('tasks') if f.endswith('.json'))


# ── 落盘动作不再有能力删除任何东西 ───────────────────────────────────────

def test_history_survives_a_save_from_a_process_that_never_loaded():
    """The exact accident: one fresh in-memory task, five real files on disk."""
    for tid in ('1785423021030', 'cover_abc', 'frames_abc', 'videos_abc', 'videos_def'):
        _seed_file(tid)
    _seed_memory('non-idea-compose')

    save_tasks_to_disk()

    remaining = _meta_files()
    assert '1785423021030.json' in remaining
    assert len(remaining) == 6, remaining


def test_single_task_save_never_touches_other_tasks():
    """P2 的核心保证：写一条只碰它自己的文件。事故要复现，得先有一次落盘
    能够触及别的任务——现在这条路本身就不存在了。"""
    for tid in ('900', '901'):
        _seed_file(tid)
    load_tasks_from_disk()
    other = open(os.path.join('tasks', '901.json'), encoding='utf-8').read()

    server_common.ACTIVE_TASKS['900']['status'] = 'failed'
    save_task_to_disk('900')

    assert open(os.path.join('tasks', '901.json'), encoding='utf-8').read() == other
    assert '901.json' in _meta_files()


# ── 两段防呆转移到 prune_orphan_task_files 上，继续回归 ──────────────────

def test_prune_short_circuits_when_the_process_never_loaded():
    """内存表非空，但本进程没跑完过 load_tasks_from_disk —— 内存里那条不是
    "全部任务"而是某个调用方刚塞进来的子集，此时清理等于拿一条新任务把整部
    历史判成垃圾。"""
    for tid in ('1785423021030', 'cover_abc'):
        _seed_file(tid)
    _seed_memory('non-idea-compose')
    save_task_to_disk('non-idea-compose')      # 事故里这条也已落盘
    assert server_common.TASKS_LOADED_FROM_DISK is False

    assert prune_orphan_task_files() == 0
    assert _meta_files() == ['1785423021030.json', 'cover_abc.json',
                             'non-idea-compose.json']


def test_prune_short_circuits_on_an_empty_memory_table():
    """空内存 + 磁盘有任务文件 = 本实例没有（或还没）加载历史任务。"""
    _seed_file('900')

    assert prune_orphan_task_files() == 0
    assert _meta_files() == ['900.json']


def test_real_orphans_are_cleaned_once_history_is_loaded():
    _seed_file('900')
    _seed_file('901')
    load_tasks_from_disk()
    assert server_common.TASKS_LOADED_FROM_DISK is True

    del server_common.ACTIVE_TASKS['901']
    assert prune_orphan_task_files(grace_seconds=0) == 1

    assert _meta_files() == ['900.json']
    assert not os.path.exists(os.path.join('tasks', 'results', '901.json'))
    assert not os.path.exists(os.path.join('tasks', 'events', '901.jsonl'))


def test_prune_never_touches_freshly_written_files():
    """第三段防呆。只要有第二个写者（另一个服务实例，或本次加载之后刚落盘的
    新任务），它写下的文件在本进程眼里就是凭空冒出来的孤儿。

    2026-07-31 实际发生过：一个指向同一 tasks/ 的第二实例启动，把 4 个正在跑的
    真实任务记录当孤儿删了——内存里还在，一重启就没了。
    """
    _seed_file('900')
    load_tasks_from_disk()
    # 另一个写者在本进程加载之后落下的新任务
    _seed_file('901')

    assert prune_orphan_task_files() == 0
    assert '901.json' in _meta_files()


# ── 显式删除 ─────────────────────────────────────────────────────────────

def test_delete_task_files_removes_all_three_and_leaves_the_rest():
    for tid in ('900', '901'):
        _seed_file(tid)
    load_tasks_from_disk()
    save_tasks_to_disk()          # 迁移成拆分形态

    assert delete_task_files('900') is True

    assert _meta_files() == ['901.json']
    assert not os.path.exists(os.path.join('tasks', 'results', '900.json'))
    assert os.path.exists(os.path.join('tasks', 'results', '901.json'))


def test_delete_task_files_on_an_unknown_id_is_a_noop():
    _seed_file('900')
    load_tasks_from_disk()

    assert delete_task_files('nope') is False
    assert _meta_files() == ['900.json']
