"""GET /api/projects 的接口契约（路由分支本身，不只是合流函数）。

这个接口是项目工作台的唯一数据源，页面每 4s/30s 打一次，所以这里盯三件事：
- 参数（state / q / sort / limit / offset / assets）真的被接上了，不是摆设；
- counts 在**完整表**上统计——chips 角标不能随当前筛选一起缩水；
- 合流失败回 500 并带原因，不静默回空表（空表会让用户以为项目都没了）。

驱动方式与 tests/test_account_pool_refresh_endpoint.py 一致：用
object.__new__ 拼一个只够跑单个分支的假 handler，不起真实 socket。
"""
import json

import prompt_pipeline
import server


def _handler(query_string=''):
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/projects' + (f'?{query_string}' if query_string else '')
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _row(key, title, state='completed', saved=False, has_failed_jobs=False, updated_at=100.0):
    return {
        'project_key': key, 'kind': 'project', 'title': title, 'theme': '',
        'cover': None, 'state': state, 'saved': saved,
        'has_failed_jobs': has_failed_jobs, 'image_count': None, 'video_count': None,
        'timestamp': '', 'updated_at': updated_at, 'task': None, 'library': None,
        'ledger': None, 'sub_jobs': [], 'assets': None,
    }


ROWS = [
    _row('run_a__x', '跑着的项目', state='running', updated_at=900.0),
    _row('run_b__y', '收藏的项目', state='saved', saved=True, updated_at=800.0),
    _row('run_c__z', '垮掉的项目', state='failed', updated_at=700.0),
    _row('run_d__w', '子作业垮了的项目', state='completed', has_failed_jobs=True, updated_at=600.0),
    _row('run_e__v', '普通完成的项目', state='completed', updated_at=500.0),
]


def _patch_index(monkeypatch, rows=None, capture=None):
    def fake(with_assets=True):
        if capture is not None:
            capture['with_assets'] = with_assets
        return list(ROWS if rows is None else rows)
    monkeypatch.setattr(server, 'build_projects_index', fake)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)


def test_returns_all_projects_with_counts(monkeypatch):
    _patch_index(monkeypatch)
    h, sent = _handler()
    h.do_GET()

    body, status = sent[0]
    assert status == 200
    assert body['total_count'] == 5
    assert body['filtered_count'] == 5
    assert len(body['projects']) == 5
    assert body['counts'] == {
        'all': 5, 'running': 1, 'completed': 2, 'saved': 1,
        # failed 档连带 has_failed_jobs 的项目一起算
        'failed': 2, 'archived': 0, 'active': 5,
    }


def test_state_filter_narrows_projects_but_not_counts(monkeypatch):
    """chips 角标必须在完整表上统计：点了「运行中」之后其余几档的数字要是
    原样，否则用户看到的就是一堆随点随变的 0，永远不知道该切哪一档。"""
    _patch_index(monkeypatch)
    h, sent = _handler('state=running')
    h.do_GET()

    body, _ = sent[0]
    assert [p['title'] for p in body['projects']] == ['跑着的项目']
    assert body['filtered_count'] == 1
    assert body['total_count'] == 5
    assert body['counts']['saved'] == 1
    assert body['counts']['completed'] == 2


def test_search_and_sort_params_are_wired(monkeypatch):
    _patch_index(monkeypatch)

    h, sent = _handler('q=垮掉')
    h.do_GET()
    assert [p['title'] for p in sent[0][0]['projects']] == ['垮掉的项目']

    h, sent = _handler('sort=oldest')
    h.do_GET()
    assert sent[0][0]['projects'][0]['title'] == '普通完成的项目'


def test_pagination(monkeypatch):
    _patch_index(monkeypatch)
    h, sent = _handler('limit=2&offset=1')
    h.do_GET()

    body, _ = sent[0]
    assert [p['title'] for p in body['projects']] == ['收藏的项目', '垮掉的项目']
    assert body['offset'] == 1
    assert body['total_count'] == 5


def test_bad_limit_and_offset_fall_back_to_defaults(monkeypatch):
    _patch_index(monkeypatch)
    h, sent = _handler('limit=abc&offset=-5')
    h.do_GET()

    body, _ = sent[0]
    assert len(body['projects']) == 5
    assert body['offset'] == 0


def test_assets_zero_skips_the_outputs_scan(monkeypatch):
    """轮询要能跳过 outputs/ 的目录遍历——否则每 4s 一次全盘 stat。"""
    capture = {}
    _patch_index(monkeypatch, capture=capture)

    h, _ = _handler('assets=0')
    h.do_GET()
    assert capture['with_assets'] is False

    h, _ = _handler()
    h.do_GET()
    assert capture['with_assets'] is True


def test_scope_excludes_archives_before_filtering_and_pagination(monkeypatch):
    archived = {**_row('archived', '归档项目', state='archived', updated_at=1000), 'archived': True}
    _patch_index(monkeypatch, rows=[archived, *ROWS])
    h, sent = _handler('scope=active&limit=2&offset=1')
    h.do_GET()
    body, status = sent[0]
    assert status == 200
    assert body['total_count'] == body['filtered_count'] == 5
    assert [row['project_key'] for row in body['projects']] == ['run_b__y', 'run_c__z']
    assert body['counts'] == {'all': 5, 'active': 5, 'archived': 1,
                              'running': 1, 'completed': 2, 'saved': 1, 'failed': 2}

    h, sent = _handler('scope=active&state=failed')
    h.do_GET()
    body, _ = sent[0]
    assert body['total_count'] == 5 and body['filtered_count'] == 2
    assert body['counts']['completed'] == 2 and body['counts']['archived'] == 1


def test_archive_scope_keeps_partition_counts_and_only_archive_rows(monkeypatch):
    archives = [{**_row(f'archive_{index}', f'已归档{index}', state='archived'), 'archived': True}
                for index in range(2)]
    _patch_index(monkeypatch, rows=[*archives, *ROWS])
    h, sent = _handler('scope=archived&state=archived&limit=1&offset=1')
    h.do_GET()
    body, status = sent[0]
    assert status == 200
    assert body['total_count'] == body['filtered_count'] == 2
    assert [row['project_key'] for row in body['projects']] == ['archive_1']
    assert body['counts'] == {'all': 2, 'active': 5, 'archived': 2,
                              'running': 0, 'completed': 0, 'saved': 0, 'failed': 0}


def test_omitted_scope_preserves_all_projects_and_global_state_counts(monkeypatch):
    archived = {**_row('archive', '已归档', state='archived'), 'archived': True}
    _patch_index(monkeypatch, rows=[archived, *ROWS])
    h, sent = _handler()
    h.do_GET()
    body, _ = sent[0]
    assert body['total_count'] == 6 and len(body['projects']) == 6
    assert body['counts']['all'] == 6 and body['counts']['completed'] == 2
    assert body['counts']['active'] == 5 and body['counts']['archived'] == 1


def test_light_poll_preserves_edit_status_in_project_rows(monkeypatch):
    edit = {'id': 'a' * 32, 'status': 'running', 'stage': 'reviewing_source',
            'message': '正在审阅视频', 'progress': None}
    row = {**ROWS[0], 'video_edit': edit}
    capture = {}
    _patch_index(monkeypatch, rows=[row], capture=capture)
    h, sent = _handler('scope=active&assets=0')
    h.do_GET()
    body, status = sent[0]
    assert status == 200 and capture['with_assets'] is False
    assert body['projects'][0]['video_edit'] == edit


def test_index_failure_returns_500_not_an_empty_table(monkeypatch):
    """空表和"合流失败"在界面上长得一样（都是"还没有项目"），但含义
    天差地别。失败必须是 500 带原因，让前端能报出来。"""
    def boom(with_assets=True):
        raise RuntimeError('磁盘炸了')
    monkeypatch.setattr(server, 'build_projects_index', boom)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)

    h, sent = _handler()
    h.do_GET()

    body, status = sent[0]
    assert status == 500
    assert '磁盘炸了' in body['error']


def test_response_is_json_serializable(monkeypatch):
    """行里若混进 set / Event 这类对象（ACTIVE_TASKS 里就有 listeners 和
    cancel_event），_send_json 会在真实请求里炸掉，这里提前拦住。"""
    _patch_index(monkeypatch)
    h, sent = _handler()
    h.do_GET()

    json.dumps(sent[0][0], ensure_ascii=False)


def test_project_references_reads_project_assets_by_title(monkeypatch, tmp_path):
    project_dir = tmp_path / 'outputs' / 'project_1'
    project_dir.mkdir(parents=True)
    calls = []

    def references(directory, total_beats):
        calls.append((directory, total_beats))
        return ({1: str(project_dir / 'review_frames' / 'frame_1.png')},
                {1: 'benchmark'}, str(project_dir / 'source_collage.jpg'))

    monkeypatch.setattr(server, '_get_project_dir', lambda title: str(project_dir))
    monkeypatch.setattr(prompt_pipeline, 'find_reference_frames_with_roles', references)
    h, sent = _handler()
    h.path = '/api/project/references?title=project_1&total_beats=5'
    h.do_GET()

    body, status = sent[0]
    assert status == 200
    assert calls == [(str(project_dir), 5)]
    assert body['ref_frames'] == {1: '/outputs/project_1/review_frames/frame_1.png'}
    assert body['ref_frame_roles'] == {1: 'benchmark'}
    assert body['source_collage_url'] == '/outputs/project_1/source_collage.jpg'


def test_project_references_without_output_directory_returns_empty(monkeypatch, tmp_path):
    missing = str(tmp_path / 'missing_outputs')
    monkeypatch.setattr(server, '_get_project_dir', lambda title: missing)
    monkeypatch.setattr(server, 'OUTPUT_ROOT', missing)
    h, sent = _handler()
    h.path = '/api/project/references?title=missing_project'
    h.do_GET()

    body, status = sent[0]
    assert status == 200
    assert body == {'status': 'ok', 'ref_frames': {}, 'ref_frame_roles': {},
                    'source_collage_url': None}
