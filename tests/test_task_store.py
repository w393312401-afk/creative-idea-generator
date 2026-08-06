"""任务拆分持久层（tasks/<id>.json + events/<id>.jsonl + results/<id>.json）的行为契约。

老形态：单个 tasks/<id>.json 装 meta + events + result，而 save_tasks_to_disk() 每次
调用把**内存里所有任务整份重写一遍**。实测单文件 523 KB（events 375 KB + result
132 KB），server.py 里 13 处调用，同时挂 3 个任务时一次状态变更 ≈ 1.5 MB 同步写。

拆开之后要钉住的：
- 写一条只碰它自己的三份文件（这是"落盘不再能误删/误改别人"的结构性保证）；
- 运行中的事件流是**追加**，不是每次整份重写；
- 事件流变短（重跑清空 / 终态滤掉 text_chunk）时必须整份重写，不能留下旧尾巴；
- 老格式能读，并就地迁移，且迁移幂等、不丢数据；
- 重启后 running 任务转 failed 的既有行为不变。
"""

import json
import os
import threading

import pytest

import server_common
from server_common import (
    load_tasks_from_disk,
    save_task_to_disk,
    save_tasks_to_disk,
    delete_task_files,
    _task_paths,
    _task_file_id,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('tasks', exist_ok=True)
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})
    monkeypatch.setattr(server_common, 'TASKS_LOADED_FROM_DISK', False)
    monkeypatch.setattr(server_common, '_TASK_FLUSHED_EVENTS', {})


def _mem(tid, status='running', events=None, result=None, error=None):
    server_common.ACTIVE_TASKS[tid] = {
        'id': tid, 'status': status, 'events': list(events or []),
        'listeners': set(), 'cancel_event': threading.Event(),
        'dimensions': {'theme': f'主题 {tid}'}, 'result': result,
        'error': error, 'last_active': 5.0,
    }
    return server_common.ACTIVE_TASKS[tid]


def _seed_legacy(tid, status='completed', events=None, result=None):
    payload = {
        'id': tid, 'status': status,
        'events': events if events is not None else [['progress', {'stage': 'x'}]],
        'dimensions': {'theme': f'主题 {tid}'},
        'result': result if result is not None else {'title': tid},
        'error': None, 'last_active': 1.0,
    }
    with open(os.path.join('tasks', f'{tid}.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)


def _events_lines(tid):
    _, events_path, _ = _task_paths(tid)
    if not os.path.exists(events_path):
        return []
    with open(events_path, encoding='utf-8') as f:
        return [line for line in f.read().splitlines() if line.strip()]


# ── 布局与单条写 ──────────────────────────────────────────────────────────

def test_save_splits_into_three_files():
    _mem('t1', status='completed',
         events=[('progress', {'stage': 'a'}), ('result', {'title': 'x'})],
         result={'title': 'x', 'prompt_block': 'Y' * 5000})

    assert save_task_to_disk('t1') is True

    meta_path, events_path, result_path = _task_paths('t1')
    meta = json.load(open(meta_path, encoding='utf-8'))
    assert set(meta) == {
        'id', 'status', 'dimensions', 'error', 'last_active', 'format',
        'last_client_poll_at', 'last_worker_progress_at', 'failure_code', 'timings',
        'runtime_version',
    }
    # 正文绝不能留在 meta 里——那正是单文件 523 KB 的来源
    assert 'events' not in meta and 'result' not in meta
    assert len(_events_lines('t1')) == 2
    assert len(json.load(open(result_path, encoding='utf-8'))['prompt_block']) == 5000


def test_meta_stays_small_even_with_a_huge_result():
    _mem('t1', status='completed', events=[('progress', {'d': 'z' * 10000})],
         result={'prompt_block': 'Y' * 200000})
    save_task_to_disk('t1')

    meta_path, _, result_path = _task_paths('t1')
    assert os.path.getsize(meta_path) < 400
    assert os.path.getsize(result_path) > 200000


def test_saving_one_task_does_not_rewrite_another():
    _mem('a', status='completed', result={'title': 'a'})
    _mem('b', status='completed', result={'title': 'b'})
    save_tasks_to_disk()

    b_meta, b_events, b_result = _task_paths('b')
    before = {p: (os.path.getmtime(p), open(p, encoding='utf-8').read())
              for p in (b_meta, b_result) if os.path.exists(p)}

    server_common.ACTIVE_TASKS['a']['status'] = 'failed'
    save_task_to_disk('a')

    for path, (mtime, content) in before.items():
        assert open(path, encoding='utf-8').read() == content
        assert os.path.getmtime(path) == mtime


def test_save_of_an_unknown_task_is_a_noop():
    assert save_task_to_disk('ghost') is False
    assert os.listdir('tasks') == []


def test_result_file_is_not_written_when_there_is_no_result():
    _mem('t1', status='running')
    save_task_to_disk('t1')

    _, _, result_path = _task_paths('t1')
    assert not os.path.exists(result_path)


# ── 事件流：追加而不是整份重写 ───────────────────────────────────────────

def test_running_events_are_appended_not_rewritten():
    t = _mem('t1', status='running', events=[('progress', {'i': 1})])
    save_task_to_disk('t1')
    assert len(_events_lines('t1')) == 1

    t['events'].append(('progress', {'i': 2}))
    t['events'].append(('progress', {'i': 3}))
    save_task_to_disk('t1')

    lines = _events_lines('t1')
    assert len(lines) == 3
    assert [json.loads(l)[1]['i'] for l in lines] == [1, 2, 3]


def test_repeated_saves_without_new_events_do_not_duplicate():
    t = _mem('t1', status='running', events=[('progress', {'i': 1})])
    for _ in range(5):
        save_task_to_disk('t1')
    assert len(_events_lines('t1')) == 1


def test_shrinking_event_list_triggers_a_full_rewrite():
    """终态化会滤掉 text_chunk。若还按"追加"处理，磁盘上会留着已经被滤掉的
    那批 text_chunk，重启后事件流与内存对不上。"""
    t = _mem('t1', status='running', events=[
        ('text_chunk', 'aaa'), ('progress', {'i': 1}), ('text_chunk', 'bbb')])
    save_task_to_disk('t1')
    assert len(_events_lines('t1')) == 3

    t['events'] = [e for e in t['events'] if e[0] != 'text_chunk']
    t['events'].append(('result', {'title': 'x'}))
    t['status'] = 'completed'
    t['result'] = {'title': 'x'}
    save_task_to_disk('t1')

    lines = _events_lines('t1')
    assert [json.loads(l)[0] for l in lines] == ['progress', 'result']


def test_terminal_save_rewrites_so_disk_matches_memory():
    t = _mem('t1', status='running', events=[('progress', {'i': 1})])
    save_task_to_disk('t1')
    t['status'] = 'failed'
    t['error'] = '炸了'
    save_task_to_disk('t1')

    load_tasks_from_disk()
    assert [e[0] for e in server_common.ACTIVE_TASKS['t1']['events']] == ['progress']


def test_events_survive_a_round_trip():
    _mem('t1', status='completed', events=[
        ('progress', {'stage': '渲染', 'details': {'seq': 3}}),
        ('result', {'title': '成品'}),
    ], result={'title': '成品'})
    save_task_to_disk('t1')

    monkey = dict(server_common.ACTIVE_TASKS)
    server_common.ACTIVE_TASKS.clear()
    load_tasks_from_disk()

    t = server_common.ACTIVE_TASKS['t1']
    assert t['events'][0] == ('progress', {'stage': '渲染', 'details': {'seq': 3}})
    assert t['result'] == {'title': '成品'}
    assert isinstance(t['events'][0], tuple)      # 事件必须还是元组，SSE 重放靠它
    assert monkey  # 防止 lint 抱怨未使用


def test_a_truncated_event_line_does_not_kill_the_task():
    """写到一半崩溃会留下半截行。丢最后一行事件远好过丢掉整条任务记录。"""
    _mem('t1', status='completed', events=[('progress', {'i': 1})], result={'title': 'x'})
    save_task_to_disk('t1')
    _, events_path, _ = _task_paths('t1')
    with open(events_path, 'a', encoding='utf-8') as f:
        f.write('["progress", {"i": 2}')      # 没有换行、没有闭合

    server_common.ACTIVE_TASKS.clear()
    load_tasks_from_disk()

    assert 't1' in server_common.ACTIVE_TASKS
    assert len(server_common.ACTIVE_TASKS['t1']['events']) == 1


# ── 老格式迁移 ────────────────────────────────────────────────────────────

def test_legacy_format_is_read_and_migrated_in_place():
    _seed_legacy('900', events=[['progress', {'stage': 'a'}]], result={'title': '老结果'})

    load_tasks_from_disk()

    t = server_common.ACTIVE_TASKS['900']
    assert t['result'] == {'title': '老结果'}
    assert t['events'] == [('progress', {'stage': 'a'})]
    # 迁移后 meta 里不再内联正文，两份新文件就位
    meta_path, events_path, result_path = _task_paths('900')
    meta = json.load(open(meta_path, encoding='utf-8'))
    assert 'events' not in meta and 'result' not in meta
    assert os.path.exists(events_path) and os.path.exists(result_path)


def test_migration_does_not_duplicate_events():
    _seed_legacy('900', events=[['progress', {'i': 1}], ['progress', {'i': 2}]])
    load_tasks_from_disk()
    assert len(_events_lines('900')) == 2

    # 再存一次不能把同样的事件又追加一遍
    save_task_to_disk('900')
    assert len(_events_lines('900')) == 2


def test_migration_is_idempotent_across_restarts():
    _seed_legacy('900', events=[['progress', {'i': 1}]], result={'title': 'x'})
    load_tasks_from_disk()

    server_common.ACTIVE_TASKS.clear()
    load_tasks_from_disk()
    server_common.ACTIVE_TASKS.clear()
    load_tasks_from_disk()

    t = server_common.ACTIVE_TASKS['900']
    assert t['events'] == [('progress', {'i': 1})]
    assert t['result'] == {'title': 'x'}


def test_running_task_becomes_failed_after_restart():
    """既有行为，不能被这次改造弄丢。"""
    _seed_legacy('900', status='running', events=[])
    load_tasks_from_disk()

    t = server_common.ACTIVE_TASKS['900']
    assert t['status'] == 'failed'
    assert '服务已重启' in t['error']
    assert t['events'][-1][0] == 'error'


def test_restart_error_event_is_persisted_exactly_once():
    _seed_legacy('900', status='running', events=[['progress', {'i': 1}]])
    load_tasks_from_disk()
    save_task_to_disk('900')

    kinds = [json.loads(l)[0] for l in _events_lines('900')]
    assert kinds == ['progress', 'error']


# ── id 消毒 ───────────────────────────────────────────────────────────────

def test_task_id_cannot_escape_the_tasks_directory():
    """task_id 是客户端传上来的（/api/compose 的请求体里就有），直接拼进路径
    等于把路径穿越开给外部输入。"""
    assert '..' not in _task_file_id('../../etc/passwd')
    assert os.sep not in _task_file_id('a/b')

    _mem('../../evil', status='completed', result={'title': 'x'})
    save_task_to_disk('../../evil')

    written = [p for p in os.listdir('tasks') if p.endswith('.json')]
    assert written and all('evil' in p for p in written)


@pytest.mark.parametrize('tid', ['1785423021030', 'frames_2b5b0718', 'cover_abc-1', 'seqreview_x.y'])
def test_real_id_shapes_are_unchanged_by_sanitising(tid):
    """消毒对现有 id 必须是恒等的，否则已有任务文件会被孤立。"""
    assert _task_file_id(tid) == tid


def test_empty_id_is_rejected():
    with pytest.raises(ValueError):
        _task_file_id('')


# ── 删除 ──────────────────────────────────────────────────────────────────

def test_delete_clears_the_flush_state_so_a_reused_id_starts_clean():
    """重试会复用 task_id。删掉再建同名任务时，事件计数不清零的话新一轮的
    事件会被当成"已经落过盘"而漏写。"""
    t = _mem('t1', status='running', events=[('progress', {'i': 1})])
    save_task_to_disk('t1')
    delete_task_files('t1')

    t['events'] = [('progress', {'i': 9})]
    save_task_to_disk('t1')

    assert [json.loads(l)[1]['i'] for l in _events_lines('t1')] == [9]


# ── project_key：任务创建那一刻就定下（P3）────────────────────────────────

def test_task_gets_a_project_key_at_creation_not_at_result_time():
    """project_key 是「同一条创意」在 任务/点子库/台账/画廊 四处的硬主键。它以前
    要等结果阶段才生成，于是运行中的任务没有主键可用，四个界面只能靠标题模糊匹配
    互相反查（app.js 那一串 sparkNormKey）。"""
    dims = {'theme': '做一个废弃水塔改造', 'task_label': '废弃水塔改造'}
    server_common.get_or_create_task('1785400000000', dims)

    assert dims['project_key'] == 'run_1785400000000__废弃水塔改造'
    assert server_common.ACTIVE_TASKS['1785400000000']['dimensions']['project_key'] == dims['project_key']


def test_project_key_prefers_task_label_over_theme():
    """theme 是用户输入的整句（带"做一个"前缀），task_label 才是灵感卡片的选题名。
    自治管线一直用的是 task_label，compose 以前用的是 LLM 生成的 title——两条路径
    建出的键不一样，同一条创意会被拆成两份。现在统一到 task_label。"""
    dims = {'theme': '做一个X', 'task_label': 'X'}
    server_common.ensure_task_project_key('t9', dims)
    assert dims['project_key'] == 'run_t9__X'


def test_theme_is_the_fallback_when_there_is_no_task_label():
    dims = {'theme': '只有主题'}
    assert server_common.ensure_task_project_key('t9', dims) == 'run_t9__只有主题'


def test_an_existing_project_key_is_never_overwritten():
    """帧/视频/封面子作业创建时会显式带上母项目的 key，不能被本函数按自己的
    theme 重算掉——那正好会把子作业从母项目上摘下来。"""
    dims = {'type': 'frames', 'theme': 'X', 'project_key': 'run_parent__X'}
    assert server_common.ensure_task_project_key('frames_abc', dims) == 'run_parent__X'
    assert dims['project_key'] == 'run_parent__X'


def test_dimensions_without_any_label_gets_no_key():
    dims = {'userId': None}
    assert server_common.ensure_task_project_key('t9', dims) is None
    assert 'project_key' not in dims


def test_media_sub_jobs_attach_to_their_parent_by_key(tmp_path, monkeypatch):
    """有了显式 project_key，子作业不再靠标题撞回母项目。"""
    parent = {'id': 'p1', 'status': 'completed', 'error': None, 'last_active': 5.0,
              'dimensions': {'theme': '做一个X', 'task_label': 'X',
                             'project_key': 'run_p1__X'},
              'result': {'title': 'X', 'project_key': 'run_p1__X'}}
    child = {'id': 'frames_a', 'status': 'failed', 'error': 'boom', 'last_active': 6.0,
             'dimensions': {'type': 'frames', 'theme': '完全不同的标题',
                            'project_key': 'run_p1__X'},
             'result': None}

    rows = server_common.build_projects_index(tasks=[parent, child], library_items=[],
                                             ledger_rows=[], with_assets=False)

    assert len(rows) == 1, '标题对不上也必须靠 project_key 挂回母项目'
    assert rows[0]['has_failed_jobs'] is True
