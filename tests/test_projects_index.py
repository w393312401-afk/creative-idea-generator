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
import sys
import json
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from server_common import (
    build_projects_index,
    filter_projects,
    make_idea_project_key,
    _safe_project_name,
)


TITLE = '废弃越野救护车车厢改造成戈壁离网独居暖阁'
PK = make_idea_project_key('1785458877351', TITLE)


@pytest.fixture(autouse=True)
def isolated_project_outputs(tmp_path, monkeypatch):
    """Receipt recovery is a light disk read too; never mix production archives into fixtures."""
    original = build_projects_index

    def isolated_index(*args, **kwargs):
        kwargs.setdefault('base_dir', str(tmp_path))
        return original(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], 'build_projects_index', isolated_index)


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
            'model': 'gemini-3.8-flash-high',
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
    assert row['state'] == 'ready'


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


def test_library_title_and_theme_win_over_the_task_record():
    """改名/改主题只写点子库条目（app.js renameIdeaToTheme 一键生成主题时同步改名），
    任务记录里的 result.title 是那次跑完就冻住的运行日志。让任务赢，工作台上会一直
    显示改名前的旧名字。"""
    rows = _index(tasks=[_compose_task()],
                  library_items=[_library_item(title='沼泽废屋爆改玻璃小屋',
                                               theme='沼泽废弃木屋改造成玻璃水上小屋')])

    assert len(rows) == 1
    assert rows[0]['title'] == '沼泽废屋爆改玻璃小屋'
    assert rows[0]['theme'] == '沼泽废弃木屋改造成玻璃水上小屋'


def test_blank_library_title_falls_back_to_the_task_title():
    rows = _index(tasks=[_compose_task()], library_items=[_library_item(title='', theme='')])

    assert len(rows) == 1
    assert rows[0]['title'] == TITLE
    assert rows[0]['theme'] == f'做一个{TITLE}'


def test_orphan_library_item_becomes_its_own_row():
    """任务记录早被 7 天清理规则删掉、只剩收藏的老创意，照样要出现在工作台。"""
    rows = _index(library_items=[_library_item(item_id='999', title='灯塔改造',
                                               project_key=None)])

    assert len(rows) == 1
    assert rows[0]['saved'] is True
    assert rows[0]['task'] is None
    assert rows[0]['state'] == 'ready'


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
    # 当前仍未恢复的失败进入需要处理状态，历史成功/失败仅留在生成记录中。
    assert row['state'] == 'failed'


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



@pytest.mark.parametrize('task_type', ['retired_module', 'unknown_worker'])
def test_unsupported_task_types_do_not_create_project_rows(task_type):
    old_task = _compose_task('old_worker', title='历史任务')
    old_task['dimensions']['type'] = task_type
    rows = _index(tasks=[old_task, _compose_task('current', title='当前项目')])
    assert [r['title'] for r in rows] == ['当前项目']


def test_saved_output_survives_its_task_type_being_retired():
    old_task = _compose_task('old_worker', title=TITLE)
    old_task['dimensions']['type'] = 'retired_module'
    rows = _index(tasks=[old_task], library_items=[_library_item()])
    assert len(rows) == 1
    assert rows[0]['saved'] is True
    assert rows[0]['task'] is None
    assert rows[0]['title'] == TITLE


def test_saved_output_from_retired_task_keeps_its_media_jobs():
    old_key = '倒伏巨木河岸隐居小屋（Veo修正版）'
    old_task = _compose_task('old_worker', project_key=old_key)
    old_task['dimensions']['type'] = 'retired_module'
    media = _media_task('videos_x', 'videos', 'completed', theme=old_key)
    media['dimensions']['project_key'] = old_key
    rows = _index(tasks=[old_task, media],
                  library_items=[_library_item(project_key=old_key)])
    assert len(rows) == 1
    assert [j['id'] for j in rows[0]['sub_jobs']] == ['videos_x']


def _seed_progress(tmp_path, manifest):
    import json
    pdir = tmp_path / 'outputs' / _safe_project_name(PK)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return pdir


def _progress_index(tmp_path, **kwargs):
    return _index(tasks=kwargs.pop('tasks', [_compose_task()]),
                  base_dir=str(tmp_path), **kwargs)[0]


def test_progress_does_not_mistake_planned_counts_for_ready_media(tmp_path):
    row = _progress_index(tmp_path, library_items=[_library_item(frameRun={
        'frames': [{'sequence': 1, 'url': '/outputs/missing.webp'}],
    })])
    assert row['progress'] == {'image_ready': 0, 'image_total': 12,
                               'video_ready': 0, 'video_total': 12, 'merged': False,
                               'merged_available': False, 'merged_partial': False, 'merged_stale': False}


def test_progress_counts_only_available_adopted_slots_and_merged_output(tmp_path):
    pdir = _seed_progress(tmp_path, {
        'frames': [
            {'sequence': 1, 'file': 'frames/img_001.webp'},
            {'sequence': 1, 'file': 'frames/img_001.webp'},
            {'sequence': 2, 'file': 'frames/missing.webp'},
            {'sequence': 3, 'file': 'frames/empty.webp'},
            {'sequence': 4, 'candidates': [{'file': 'frames/candidate.webp'}]},
        ],
        'videos': [{'slot': 1, 'file': 'videos/vid_001.mp4'},
                   {'slot': 2, 'status': 'running'}],
        'merged_video': {'file': 'merged.mp4', 'status': 'success'},
    })
    (pdir / 'frames').mkdir()
    (pdir / 'videos').mkdir()
    for name in ('frames/img_001.webp', 'frames/candidate.webp', 'videos/vid_001.mp4', 'merged.mp4'):
        (pdir / name).write_bytes(b'ready')
    (pdir / 'frames/empty.webp').touch()
    row = _progress_index(tmp_path)
    assert row['progress'] == {'image_ready': 1, 'image_total': 12,
                               'video_ready': 1, 'video_total': 12, 'merged': True,
                               'merged_available': True, 'merged_partial': False, 'merged_stale': False}


def test_light_progress_poll_reuses_cache_without_listing_directories(tmp_path, monkeypatch):
    import server_common
    pdir = _seed_progress(tmp_path, {'frames': [{'sequence': 1, 'file': 'frames/img_001.webp'}]})
    (pdir / 'frames').mkdir()
    frame = pdir / 'frames/img_001.webp'
    frame.write_bytes(b'ready')
    assert _progress_index(tmp_path)['progress']['image_ready'] == 1
    original_read = server_common.read_manifest
    reads = []
    monkeypatch.setattr(server_common, 'read_manifest', lambda p: (reads.append(p), original_read(p))[1])
    monkeypatch.setattr(server_common.os, 'listdir', lambda *_: pytest.fail('light poll scanned a directory'))
    assert _progress_index(tmp_path)['progress']['image_ready'] == 1
    assert reads == []
    frame.unlink()
    assert _progress_index(tmp_path)['progress']['image_ready'] == 0
    assert len(reads) == 1


def test_progress_detects_manifest_update_in_place(tmp_path):
    import json
    pdir = _seed_progress(tmp_path, {'frames': []})
    (pdir / 'frames').mkdir()
    (pdir / 'frames/img_001.webp').write_bytes(b'ready')
    assert _progress_index(tmp_path)['progress']['image_ready'] == 0
    (pdir / 'manifest.json').write_text(json.dumps({'frames': [{'sequence': 1, 'file': 'frames/img_001.webp'}]}))
    assert _progress_index(tmp_path)['progress']['image_ready'] == 1


def test_merged_project_keeps_historical_failures_only_in_generation_records(tmp_path):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4'}})
    (pdir / 'merged.mp4').write_bytes(b'ready')
    row = _progress_index(tmp_path, tasks=[_compose_task(status='failed'),
        _media_task('failed_video', 'videos', 'failed')])
    assert row['state'] == 'completed'
    assert row['has_failed_jobs'] is False
    assert row['sub_jobs'][0]['status'] == 'failed'
    assert filter_projects([row], state='failed') == []


def test_new_success_clears_same_type_failure_but_new_failure_remains_actionable(tmp_path):
    old = _media_task('old_failure', 'videos', 'failed', last_active=100)
    new = _media_task('new_success', 'videos', 'completed', last_active=200)
    row = _progress_index(tmp_path, tasks=[_compose_task(), old, new])
    assert row['has_failed_jobs'] is False
    newer = _media_task('new_failure', 'videos', 'failed', last_active=300)
    row = _progress_index(tmp_path, tasks=[_compose_task(), old, new, newer])
    assert row['has_failed_jobs'] is True


def test_current_generation_stays_visible_even_when_merged_output_exists(tmp_path):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4'}})
    (pdir / 'merged.mp4').write_bytes(b'ready')
    row = _progress_index(tmp_path, tasks=[_compose_task(), _media_task('retry', 'videos', 'running')])
    assert row['state'] == 'running'
    assert row['progress']['merged'] is True


@pytest.mark.parametrize('merged_flags', [{'partial': True}, {'outcome': 'partial_failed'}])
def test_partial_merge_is_available_but_needs_completion(tmp_path, merged_flags):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4', **merged_flags}})
    (pdir / 'merged.mp4').write_bytes(b'partial')
    row = _progress_index(tmp_path)
    assert row['progress']['merged_available'] is True
    assert row['progress']['merged_partial'] is True
    assert row['progress']['merged'] is False
    assert row['has_failed_jobs'] is True
    assert row['state'] == 'failed'


def test_partial_failed_business_outcome_is_not_mistaken_for_success(tmp_path):
    job = _media_task('partial', 'videos', 'completed')
    job['outcome'] = 'partial_failed'
    row = _progress_index(tmp_path, tasks=[_compose_task(), job])
    assert row['sub_jobs'][0]['outcome'] == 'partial_failed'
    assert row['has_failed_jobs'] is True
    assert row['state'] == 'failed'


def test_old_merge_does_not_hide_more_recent_failed_generation(tmp_path):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4'}})
    merged = pdir / 'merged.mp4'
    merged.write_bytes(b'old final')
    os.utime(merged, (100, 100))
    row = _progress_index(tmp_path, tasks=[_compose_task(),
        _media_task('recent_failure', 'videos', 'failed', last_active=200)])
    assert row['progress']['merged_available'] is True
    assert row['progress']['merged_stale'] is True
    assert row['progress']['merged'] is False
    assert row['has_failed_jobs'] is True
    assert filter_projects([row], state='failed') == [row]
    assert filter_projects([row], state='completed') == []


def test_newer_adopted_video_marks_previous_merge_outdated(tmp_path):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4'},
                                    'videos': [{'slot': 1, 'file': 'videos/vid_001.mp4'}]})
    (pdir / 'videos').mkdir()
    merged = pdir / 'merged.mp4'
    merged.write_bytes(b'old final')
    os.utime(merged, (100, 100))
    (pdir / 'videos/vid_001.mp4').write_bytes(b'new clip')
    row = _progress_index(tmp_path)
    assert row['progress']['merged_stale'] is True
    assert row['progress']['merged'] is False


def _seed_edit_job(tmp_path, *, job_id='a' * 32, status='completed', project_key=PK,
                   directory_name=None, created_at='2026-10-01T09:00:00+00:00',
                   updated_at='2026-10-01T10:00:00+00:00', stage=None, source_name='merged.mp4'):
    import codex_video_editor as editor
    project = tmp_path / 'outputs' / (directory_name or _safe_project_name(project_key))
    directory = project / 'codex_edits' / job_id
    directory.mkdir(parents=True, exist_ok=True)
    source = project / source_name
    if not source.exists():
        source.write_bytes(b'original merged video')
    source_url = '/outputs/' + quote(source.relative_to(tmp_path / 'outputs').as_posix(), safe='/')
    job = {'id': job_id, 'status': status, 'stage': stage or status,
           'source': source_url, 'created_at': created_at, 'updated_at': updated_at,
           'message': '正在审阅视频' if status == 'running' else '精剪任务状态',
           'output': None, '_worker_token': 'private-token', '_source_identity': editor._identity(source),
           'logs': [{'message': 'private event stream'}], '_request_id': 'private request'}
    if status == 'completed':
        output = directory / 'work' / 'edited.mp4'
        output.parent.mkdir()
        output.write_bytes(b'published edit')
        job['output'] = {'file': str(output), 'url': '/outputs/' + quote(output.relative_to(tmp_path / 'outputs').as_posix(), safe='/')}
    state = directory / '.state.json'
    state.write_text(json.dumps(job), encoding='utf-8')
    return directory, job


def test_light_project_poll_includes_durable_edit_summary_without_assets_scan(tmp_path, monkeypatch):
    import server_common
    import codex_video_editor as editor
    monkeypatch.setattr(editor, '_worker_alive', lambda job: True)
    monkeypatch.setattr(server_common, '_proj_asset_stats', lambda *args: pytest.fail('light poll scanned assets'))
    directory, job = _seed_edit_job(tmp_path, status='running', stage='reviewing_source')
    before = (directory / '.state.json').read_bytes()
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    edit = row['video_edit']
    assert row['state'] == 'running' and row['assets'] is None
    assert edit == {'id': job['id'], 'status': 'running', 'stage': 'reviewing_source',
                    'message': '正在审阅视频', 'progress': None, 'source': job['source'],
                    'source_changed': False, 'created_at': job['created_at'], 'updated_at': job['updated_at'],
                    'finished_at': None, 'output_url': None, 'output_missing': False}
    assert (directory / '.state.json').read_bytes() == before
    assert row['updated_at'] == datetime.fromisoformat(job['updated_at']).timestamp()
    assert 'private-token' not in json.dumps(row) and 'private event stream' not in json.dumps(row)


def test_project_edit_summary_prefers_active_job_over_newer_completed_job(tmp_path, monkeypatch):
    import codex_video_editor as editor
    monkeypatch.setattr(editor, '_worker_alive', lambda job: True)
    _, active = _seed_edit_job(tmp_path, status='running', stage='rendering')
    _seed_edit_job(tmp_path, job_id='b' * 32, created_at='2026-10-02T09:00:00+00:00', source_name='other.mp4')
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['video_edit']['id'] == active['id'] and row['video_edit']['stage'] == 'rendering'
    assert row['state'] == 'running'


def test_project_edit_summary_tracks_state_updates_and_available_output(tmp_path, monkeypatch):
    import codex_video_editor as editor
    monkeypatch.setattr(editor, '_worker_alive', lambda job: True)
    directory, job = _seed_edit_job(tmp_path, status='running', stage='reviewing_source')
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['video_edit']['stage'] == 'reviewing_source'
    output = directory / 'work' / '精剪 成片.mp4'
    output.parent.mkdir()
    output.write_bytes(b'edited')
    job.update(status='completed', stage='completed', message='精剪完成',
               updated_at='2026-10-02T11:00:00+00:00', output={'file': str(output)})
    (directory / '.state.json').write_text(json.dumps(job))
    edit = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]['video_edit']
    assert edit['status'] == 'completed' and edit['message'] == '精剪完成'
    assert edit['finished_at'] == job['updated_at'] and edit['output_missing'] is False
    assert edit['output_url'] == '/outputs/' + quote(output.relative_to(tmp_path / 'outputs').as_posix(), safe='/')


def test_lost_edit_worker_is_interrupted_without_mutating_job_file(tmp_path, monkeypatch):
    import codex_video_editor as editor
    monkeypatch.setattr(editor, '_worker_alive', lambda job: False)
    monkeypatch.setattr(editor, '_guardian_alive', lambda job: False)
    directory, _ = _seed_edit_job(tmp_path, status='running')
    before = (directory / '.state.json').read_bytes()
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['video_edit']['status'] == 'interrupted'
    assert row['state'] == 'failed' and row['has_failed_jobs'] is True
    assert filter_projects([row], state='failed') == [row]
    assert (directory / '.state.json').read_bytes() == before


def test_newly_queued_edit_keeps_startup_grace_period(tmp_path, monkeypatch):
    import codex_video_editor as editor
    monkeypatch.setattr(editor, '_worker_alive', lambda job: False)
    monkeypatch.setattr(editor, '_guardian_alive', lambda job: False)
    now = datetime.now(timezone.utc).isoformat()
    _seed_edit_job(tmp_path, status='queued', created_at=now, updated_at=now)
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['video_edit']['status'] == 'queued' and row['state'] == 'running'


@pytest.mark.parametrize('status', ['failed', 'interrupted', 'completed'])
def test_edit_failure_or_missing_output_remains_actionable_with_original_merged_video(tmp_path, status):
    directory, job = _seed_edit_job(tmp_path, status=status)
    project = directory.parent.parent
    (project / 'manifest.json').write_text(json.dumps({'merged_video': {'file': 'merged.mp4'}}))
    if status == 'completed':
        (directory / 'work' / 'edited.mp4').unlink()
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['progress']['merged'] is True
    assert row['state'] == 'failed' and row['has_failed_jobs'] is True
    assert filter_projects([row], state='failed') == [row]
    assert filter_projects([row], state='completed') == []
    assert row['video_edit']['output_url'] is None
    assert row['video_edit']['output_missing'] is (status == 'completed')


def test_cancelled_edit_is_in_cancel_filter_even_with_original_merged_video(tmp_path):
    directory, _ = _seed_edit_job(tmp_path, status='cancelled')
    (directory.parent.parent / 'manifest.json').write_text(json.dumps({'merged_video': {'file': 'merged.mp4'}}))
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['progress']['merged'] is True
    assert row['state'] == 'cancelled'
    assert filter_projects([row], state='failed') == [row]
    assert filter_projects([row], state='completed') == []


def test_available_refined_result_is_completed_without_original_manifest_entry(tmp_path):
    _seed_edit_job(tmp_path)
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['progress']['merged'] is False
    assert row['video_edit']['output_url']
    assert row['state'] == 'completed'
    assert filter_projects([row], state='completed') == [row]


def test_edit_source_changed_reports_previous_version_without_failing_completion(tmp_path):
    directory, _ = _seed_edit_job(tmp_path)
    (directory.parent.parent / 'merged.mp4').write_bytes(b'regenerated video')
    row = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]
    assert row['video_edit']['source_changed'] is True
    assert row['video_edit']['status'] == 'completed' and row['has_failed_jobs'] is False


def test_same_title_projects_do_not_share_an_edit_summary(tmp_path):
    other_key = make_idea_project_key('another-run', TITLE)
    _, job = _seed_edit_job(tmp_path, project_key=PK)
    rows = _index(tasks=[_compose_task(), _compose_task('another-run', project_key=other_key)], base_dir=str(tmp_path))
    by_key = {row['project_key']: row for row in rows}
    assert by_key[PK]['video_edit']['id'] == job['id']
    assert by_key[other_key]['video_edit'] is None


def test_legacy_title_directory_requires_manifest_project_identity(tmp_path):
    directory, job = _seed_edit_job(tmp_path, directory_name=_safe_project_name(TITLE))
    assert _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]['video_edit'] is None
    (directory.parent.parent / 'manifest.json').write_text(json.dumps({'project_key': PK}))
    assert _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]['video_edit']['id'] == job['id']


def test_corrupt_or_cross_project_edit_states_do_not_break_or_contaminate_index(tmp_path):
    directory, job = _seed_edit_job(tmp_path)
    state = directory / '.state.json'
    for invalid in ('invalid json', json.dumps([]), json.dumps({**job, 'id': 'b' * 32}),
                    json.dumps({**job, 'status': []}), json.dumps({**job, 'source': '/outputs/other/source.mp4'})):
        state.write_text(invalid)
        assert _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]['video_edit'] is None


def test_edit_summary_never_serves_symlinked_output(tmp_path):
    directory, _ = _seed_edit_job(tmp_path)
    output = directory / 'work' / 'edited.mp4'
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside')
    output.unlink()
    output.symlink_to(outside)
    edit = _index(tasks=[_compose_task()], base_dir=str(tmp_path))[0]['video_edit']
    assert edit['output_missing'] is True and edit['output_url'] is None


def test_saved_complete_project_is_in_both_completed_and_saved_filters(tmp_path):
    pdir = _seed_progress(tmp_path, {'merged_video': {'file': 'merged.mp4'}})
    (pdir / 'merged.mp4').write_bytes(b'final')
    row = _progress_index(tmp_path, library_items=[_library_item()])
    assert row['state'] == 'completed'
    assert row['saved'] is True
    assert filter_projects([row], state='completed') == [row]
    assert filter_projects([row], state='saved') == [row]


@pytest.mark.parametrize('media_stage', ['prompts', 'images', 'videos'])
def test_unmerged_project_never_appears_in_completed_filter(tmp_path, media_stage):
    frames = [{'sequence': 1, 'file': 'frames/img_001.webp'}] if media_stage != 'prompts' else []
    videos = [{'slot': 1, 'file': 'videos/vid_001.mp4'}] if media_stage == 'videos' else []
    pdir = _seed_progress(tmp_path, {'frames': frames, 'videos': videos})
    (pdir / 'frames').mkdir()
    (pdir / 'videos').mkdir()
    if frames:
        (pdir / 'frames/img_001.webp').write_bytes(b'frame')
    if videos:
        (pdir / 'videos/vid_001.mp4').write_bytes(b'video')
    row = _progress_index(tmp_path, library_items=[_library_item(image_count=1, video_count=1)])
    assert row['state'] == 'ready'
    assert row['saved'] is True
    assert filter_projects([row], state='completed') == []
    assert filter_projects([row], state='saved') == [row]


def test_orphan_generation_record_keeps_its_own_completed_state(tmp_path):
    row = _progress_index(tmp_path, tasks=[_media_task('orphan', 'videos', 'completed')])
    assert row['kind'] == 'job'
    assert row['state'] == 'completed'
    assert filter_projects([row], state='completed') == [row]
