"""项目工作台合流索引（build_projects_index / filter_projects）的行为契约。

这张表是「任务列表 + 点子库」合并后的唯一数据源，所以它的核心义务是**别把同一条
创意拆成两行**，以及**别因为某一路数据缺失就整页空白**。关注点：

- 四路数据（激发任务 / 点子库 / 媒体子作业 / 创意台账）按 project_key 合成一行；
- 点子库条目没有 project_key（历史数据确实如此）时，靠 task:<id> 与标题别名挂回同一行；
- 媒体子作业（frames/videos/cover）挂成母项目的 sub_jobs，挂不上的自成一行——
  它们现在被任务抽屉整类过滤掉，失败了完全看不见，这正是要修的；
- 台账行只做信息附加，从不凭空造出项目行；
- 主状态分桶与「失败」档要连带子作业失败一起捞；
- 任一路缺失/损坏都不抛异常。
"""
import os

import pytest

from server_common import (
    build_projects_index,
    filter_projects,
    make_idea_project_key,
    _safe_project_name,
)


TITLE = '废弃越野救护车车厢改造成戈壁离网独居暖阁'
PK = make_idea_project_key('1785458877351', TITLE)


def _compose_task(task_id='1785458877351', status='completed', project_key=PK,
                  title=TITLE, theme=None, last_active=5000.0, result=True):
    """一条激发任务。theme 默认带「做一个」前缀——真实数据就是这样，而它派生的
    媒体子作业 dimensions.theme 是去掉前缀的成品标题。"""
    task = {
        'id': task_id,
        'status': status,
        'error': None,
        'last_active': last_active,
        'dimensions': {
            'theme': theme if theme is not None else f'做一个{title}',
            'task_label': title,
            'beats_count': 11,
        },
        'result': None,
    }
    if result:
        task['result'] = {
            'title': title,
            'project_key': project_key,
            'image_count': 12,
            'video_count': 12,
            'model': 'gemini-3.6-flash-high',
            'timings': {'total_duration_seconds': 321.5},
        }
    return task


def _media_task(task_id, job_type, status, theme=TITLE, last_active=6000.0):
    return {
        'id': task_id,
        'status': status,
        'error': '渲染失败' if status == 'failed' else None,
        'last_active': last_active,
        'dimensions': {'type': job_type, 'theme': theme},
        'result': None,
    }


def _library_item(item_id='1785458877351', title=TITLE, project_key=PK, **extra):
    item = {
        'id': item_id,
        'title': title,
        'theme': f'做一个{title}',
        'timestamp': '2026-07-29 10:41:06',
        'covers': ['outputs/covers/cover_a.webp'],
        'image_count': 12,
        'video_count': 12,
    }
    if project_key is not None:
        item['project_key'] = project_key
    item.update(extra)
    return item


def _index(**kwargs):
    """默认关掉资产扫描：不涉及 outputs/ 的用例不该被文件系统影响。"""
    kwargs.setdefault('tasks', [])
    kwargs.setdefault('library_items', [])
    kwargs.setdefault('ledger_rows', [])
    kwargs.setdefault('with_assets', False)
    return build_projects_index(**kwargs)


# ── 合流：同一条创意只能有一行 ────────────────────────────────────────────

def test_task_and_library_merge_into_one_row_by_project_key():
    rows = _index(tasks=[_compose_task()], library_items=[_library_item()])

    assert len(rows) == 1
    row = rows[0]
    assert row['project_key'] == PK
    assert row['saved'] is True
    assert row['task']['id'] == '1785458877351'
    assert row['library']['id'] == '1785458877351'
    assert row['state'] == 'saved'


def test_library_item_without_project_key_still_merges_via_task_id():
    """历史数据里的点子库条目没有 project_key（实测 2 条里有 1 条没有）。
    它的 id 与激发任务 id 同源，必须靠这条别名挂回同一行，不能裂成两行。"""
    rows = _index(tasks=[_compose_task()],
                  library_items=[_library_item(project_key=None)])

    assert len(rows) == 1
    assert rows[0]['project_key'] == PK
    assert rows[0]['saved'] is True


def test_library_item_without_project_key_or_matching_task_id_merges_via_title():
    """连 id 都对不上的更老的数据（另一台机器导入的库），标题仍要能撞上。"""
    rows = _index(tasks=[_compose_task()],
                  library_items=[_library_item(item_id='legacy-xyz', project_key=None)])

    assert len(rows) == 1
    assert rows[0]['saved'] is True


def test_orphan_library_item_becomes_its_own_row():
    """任务记录早被 7 天清理规则删掉、只剩收藏的老创意，照样要出现在工作台。"""
    rows = _index(library_items=[_library_item(item_id='999', title='灯塔改造',
                                               project_key=None)])

    assert len(rows) == 1
    assert rows[0]['saved'] is True
    assert rows[0]['task'] is None
    assert rows[0]['state'] == 'saved'


# ── 媒体子作业 ────────────────────────────────────────────────────────────

def test_media_jobs_attach_to_parent_project_not_separate_rows():
    """子作业的 theme 是去掉「做一个」前缀的成品标题，必须挂回母项目。"""
    rows = _index(tasks=[
        _compose_task(),
        _media_task('frames_aaa', 'frames', 'completed'),
        _media_task('videos_bbb', 'videos', 'failed'),
    ])

    assert len(rows) == 1
    row = rows[0]
    assert {j['type'] for j in row['sub_jobs']} == {'frames', 'videos'}
    assert row['has_failed_jobs'] is True
    # 子作业失败不改母项目主状态——母项目本身是好的，只打旗
    assert row['state'] == 'completed'


def test_unmatched_media_job_still_shows_up_as_its_own_row():
    """挂不回任何母项目的媒体作业不能被丢弃。任务抽屉现在把这一整类过滤掉
    （app.js 的 MEDIA_TASK_TYPES），失败的帧/视频任务因此完全不可见。"""
    rows = _index(tasks=[_media_task('frames_zzz', 'frames', 'failed',
                                     theme='一个从未激发过的主题')])

    assert len(rows) == 1
    assert rows[0]['kind'] == 'job'
    assert rows[0]['project_key'] == 'job:一个从未激发过的主题'
    assert rows[0]['has_failed_jobs'] is True
    # 没有 task 的孤立作业行必须继承作业自己的状态，否则落进 'unknown'
    # 就连「失败」筛选都捞不出来
    assert rows[0]['state'] == 'failed'
    assert filter_projects(rows, state='failed') == rows


def test_unmatched_media_jobs_group_by_title_not_by_task_id():
    """同一个母项目跑过 3 次帧序列 + 1 次封面时，按任务 id 建行会得到 4 行
    长得一模一样的记录（实测真实数据里就是这样）。封面任务的 theme 带
    「做一个」前缀、帧任务不带，两者也必须落到同一行。"""
    rows = _index(tasks=[
        _media_task('frames_a', 'frames', 'failed', theme='林间双舱睡眠小屋'),
        _media_task('frames_b', 'frames', 'failed', theme='林间双舱睡眠小屋'),
        _media_task('cover_c', 'cover', 'completed', theme='做一个林间双舱睡眠小屋'),
    ])

    assert len(rows) == 1
    assert len(rows[0]['sub_jobs']) == 3
    assert rows[0]['state'] == 'failed'


def test_running_media_job_makes_parent_project_running():
    rows = _index(tasks=[
        _compose_task(),
        _media_task('videos_bbb', 'videos', 'running'),
    ])

    assert rows[0]['state'] == 'running'


# ── 台账 ──────────────────────────────────────────────────────────────────

def test_ledger_row_enriches_matching_project():
    ledger = [{
        'id': 'ledger-1',
        'status': 'published',
        'topic_dna': 'ambulance / gobi / off-grid',
        'llm_score': 23,
        'user_score': 8,
        'date': '2026-07-29',
        'creative_seed': {'input_str': f'做一个{TITLE}'},
    }]
    rows = _index(tasks=[_compose_task()], ledger_rows=ledger)

    assert len(rows) == 1
    assert rows[0]['ledger']['status'] == 'published'
    assert rows[0]['ledger']['user_score'] == 8


def test_ledger_row_never_creates_a_project_row():
    """还没被激发过的候选选题属于台账页，不该出现在项目工作台里。"""
    ledger = [{'id': 'ledger-2', 'status': 'candidate',
               'creative_seed': {'input_str': '一个八字不合的选题'}}]
    rows = _index(tasks=[_compose_task()], ledger_rows=ledger)

    assert len(rows) == 1
    assert rows[0]['ledger'] is None


# ── 容错：缺一路不能整表失败 ──────────────────────────────────────────────

@pytest.mark.parametrize('bad', [None, [], [None, 'garbage', 123]])
def test_missing_or_garbage_sources_do_not_raise(bad):
    rows = build_projects_index(tasks=bad, library_items=bad, ledger_rows=bad,
                                with_assets=False)
    assert isinstance(rows, list)


def test_task_without_result_still_yields_a_row():
    """运行中的任务还没有 result（因而也没有 project_key），要按 id+标题重建键。"""
    rows = _index(tasks=[_compose_task(status='running', result=False)])

    assert len(rows) == 1
    assert rows[0]['state'] == 'running'
    assert rows[0]['project_key'].startswith('run_1785458877351__')


def test_corrupt_library_file_degrades_to_empty_not_exception(monkeypatch):
    """read_library 读损坏文件返回 None。只读路径按"少一列信息"降级，
    绝不能让工作台整页失败（写路径另有 409 防护，不走这里）。"""
    import server_common
    monkeypatch.setattr(server_common, 'read_library', lambda path=None: None)
    monkeypatch.setattr(server_common, 'read_ledger', lambda path=None: None)
    monkeypatch.setattr(server_common, 'ACTIVE_TASKS', {})

    rows = build_projects_index(with_assets=False)
    assert rows == []


# ── 资产统计 ──────────────────────────────────────────────────────────────

def test_assets_resolve_through_safe_project_name(tmp_path):
    """outputs/ 下的目录名不是 project_key 原文：_safe_project_name 会把 '__'
    折成 '_'。直接拿 project_key 当目录名会永远统计到 0 个文件。"""
    base = str(tmp_path)
    pdir = os.path.join(base, 'outputs', _safe_project_name(PK))
    os.makedirs(os.path.join(pdir, 'frames'))
    for name in ('img_001.webp', 'img_002.webp'):
        with open(os.path.join(pdir, 'frames', name), 'wb') as f:
            f.write(b'xxxx')
    with open(os.path.join(pdir, 'manifest.json'), 'w') as f:
        f.write('{}')

    rows = build_projects_index(tasks=[_compose_task()], library_items=[],
                                ledger_rows=[], base_dir=base, with_assets=True)

    assets = rows[0]['assets']
    assert assets['file_count'] == 2       # manifest.json 不是媒体，不计入
    assert assets['bytes'] == 8
    assert assets['dir'].endswith(_safe_project_name(PK))


def test_missing_project_dir_reports_zero_assets(tmp_path):
    rows = build_projects_index(tasks=[_compose_task()], library_items=[],
                                ledger_rows=[], base_dir=str(tmp_path), with_assets=True)

    assert rows[0]['assets']['file_count'] == 0
    assert rows[0]['assets']['dir'] is None
    assert rows[0]['assets']['cover'] is None
    assert rows[0]['cover'] is None


def _write_project_cover(base, name, content=b'xxxx'):
    pdir = os.path.join(base, 'outputs', _safe_project_name(PK))
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, name)
    with open(path, 'wb') as f:
        f.write(content)
    return path


def test_uncollected_project_takes_its_cover_from_disk(tmp_path):
    """封面跟项目打包在一起之后，项目行的缩略图不再只能由点子库条目供给——
    没收藏过的项目也有封面（老布局里封面在全局池，行上只能是空的）。"""
    base = str(tmp_path)
    old = _write_project_cover(base, 'cover_100.webp')
    new = _write_project_cover(base, 'cover_200.webp')
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))

    rows = build_projects_index(tasks=[_compose_task()], library_items=[],
                                ledger_rows=[], base_dir=base, with_assets=True)

    assert rows[0]['cover'] == f'/outputs/{_safe_project_name(PK)}/cover_200.webp'


def test_library_cover_wins_over_the_one_on_disk(tmp_path):
    """收藏过的项目用户可能在多张封面里选过一张（activeCoverUrl），那是他的选择。"""
    base = str(tmp_path)
    _write_project_cover(base, 'cover_200.webp')

    rows = build_projects_index(
        tasks=[_compose_task()],
        library_items=[_library_item(covers=['/outputs/x/cover_1.webp'],
                                     activeCoverUrl='/outputs/x/cover_2.webp')],
        ledger_rows=[], base_dir=base, with_assets=True)

    assert rows[0]['cover'] == '/outputs/x/cover_2.webp'


def test_project_frames_are_not_mistaken_for_covers(tmp_path):
    base = str(tmp_path)
    pdir = os.path.join(base, 'outputs', _safe_project_name(PK), 'frames')
    os.makedirs(pdir)
    with open(os.path.join(pdir, 'img_001.webp'), 'wb') as f:
        f.write(b'xxxx')

    rows = build_projects_index(tasks=[_compose_task()], library_items=[],
                                ledger_rows=[], base_dir=base, with_assets=True)

    assert rows[0]['assets']['file_count'] == 1
    assert rows[0]['cover'] is None


# ── 排序与筛选 ────────────────────────────────────────────────────────────

def test_rows_sorted_newest_first():
    rows = _index(tasks=[
        _compose_task('old', project_key='run_old__a', title='旧项目', last_active=100.0),
        _compose_task('new', project_key='run_new__b', title='新项目', last_active=900.0),
    ])

    assert [r['title'] for r in rows] == ['新项目', '旧项目']


def test_filter_failed_bucket_includes_projects_with_failed_sub_jobs():
    rows = _index(tasks=[
        _compose_task(),
        _media_task('videos_bbb', 'videos', 'failed'),
        _compose_task('other', status='failed', project_key='run_other__x', title='垮掉的项目'),
    ])

    failed = filter_projects(rows, state='failed')
    assert len(failed) == 2


def test_filter_saved_and_search():
    rows = _index(tasks=[_compose_task(),
                         _compose_task('other', project_key='run_other__x', title='灯塔改造')],
                  library_items=[_library_item()])

    assert len(filter_projects(rows, state='saved')) == 1
    assert len(filter_projects(rows, query='灯塔')) == 1
    assert len(filter_projects(rows, query='不存在的词')) == 0


def test_filter_sort_oldest_reverses_order():
    rows = _index(tasks=[
        _compose_task('old', project_key='run_old__a', title='旧项目', last_active=100.0),
        _compose_task('new', project_key='run_new__b', title='新项目', last_active=900.0),
    ])

    assert [r['title'] for r in filter_projects(rows, sort='oldest')] == ['旧项目', '新项目']
