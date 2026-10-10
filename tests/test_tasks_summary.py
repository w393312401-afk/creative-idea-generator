"""Ordinary polling stays small; project recovery keeps authoritative ownership."""
import copy
import json

import pytest

import server


def task(tid, *, kind='idea', status='running', active=1, project='project', **updates):
    dimensions = {'type': kind, 'project_key': project, 'title': 'visible title',
                  'theme': 'theme', 'task_label': 'label', 'prompt_block': 'x' * 10000}
    return {'id': tid, 'status': status, 'last_active': active,
            'dimensions': dimensions, 'events': [('text_chunk', 'y' * 10000)],
            'result': {'prompt_block': 'z' * 10000}, 'error': None, **updates}


@pytest.fixture
def get_tasks(monkeypatch):
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)

    def get(path, tasks, *, gate=True):
        monkeypatch.setattr(server, 'ACTIVE_TASKS', {t['id']: t for t in tasks})
        handler = object.__new__(server.SparkRequestHandler)
        handler.path = path
        handler._gate = lambda: gate
        replies = []
        handler._send_json = lambda payload, status=200: replies.append((status, payload))
        handler.do_GET()
        return replies

    return get


def test_summary_is_gated_before_cleanup(get_tasks, monkeypatch):
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: pytest.fail('denied read did cleanup'))
    assert get_tasks('/api/tasks/summary', [], gate=False) == []
    assert get_tasks('/api/tasks?project_key=project&include_id=foreign', [], gate=False) == []


def test_summary_omits_prompts_events_progress_and_runtime_snapshots(get_tasks, monkeypatch):
    source = task('frame', kind='frames', result={'project_key': 'project',
                  'auto_video': {'task_id': 'child'}, 'frames': [{'image': 'big-url'}],
                  'token_usage': {'total': 100}, 'prompt_block': 'large'},
                  dimensions={'type': 'frames', 'project_key': 'project', 'theme': '主题',
                              'target_sequences': list(range(1000)), 'prompt_block': 'huge'})
    before = copy.deepcopy(source)
    monkeypatch.setattr(server, '_auto_video_runtime_snapshot',
                        lambda *_: pytest.fail('summary read runtime snapshot'))
    monkeypatch.setattr(server, '_media_task_progress_snapshot',
                        lambda *_: pytest.fail('summary scanned progress events'))
    status, data = get_tasks('/api/tasks/summary', [source])[0]
    assert status == 200
    assert data == {'tasks': [{'id': 'frame', 'status': 'running', 'outcome': 'running',
                              'last_active': 1, 'dimensions': {'type': 'frames',
                              'project_key': 'project', 'theme': '主题'},
                              'result': {'project_key': 'project'}}],
                    'counts': {'running': 1, 'ideation_running': 0}, 'total_count': 1}
    assert len(json.dumps(data).encode()) < 500
    assert source == before


def test_recent_hundred_summary_has_exact_counts_including_older_running_tasks(get_tasks):
    # Latest 100 are all finished media tasks; every running idea sits outside
    # that window. Badge counts must never depend on the returned page.
    tasks = [task(str(index), kind='videos', status='completed', active=index)
             for index in range(100, 200)]
    tasks += [task('old-' + kind, kind=kind, active=1)
              for kind in server.PROJECT_TASK_TYPES]
    tasks += [task('media', kind='frames', active=0), task('retired', kind='auto', active=0)]
    data = get_tasks('/api/tasks/summary', tasks)[0][1]
    assert data['total_count'] == 110
    assert len(data['tasks']) == 100
    assert [row['id'] for row in data['tasks']] == [str(index) for index in range(199, 99, -1)]
    assert data['counts'] == {'running': 10, 'ideation_running': 8}
    assert all(row['status'] == 'completed' for row in data['tasks'])


@pytest.mark.parametrize('status,result,outcome', [
    ('completed', {'project_key': 'project'}, 'completed'),
    ('completed', {'has_quality_warnings': True}, 'completed_with_warnings'),
    ('completed', {'frames': [{'status': 'failed'}]}, 'partial_failed'),
    ('completed', {'completion_state': 'partial_failed'}, 'partial_failed'),
    ('failed', {'completion_state': 'completed'}, 'failed'),
    ('cancelled', {'completion_state': 'completed'}, 'cancelled'),
    ('running', {'completion_state': 'partial_failed'}, 'running'),
])
def test_terminal_summary_preserves_log_resolution_outcomes(get_tasks, status, result, outcome):
    row = get_tasks('/api/tasks/summary', [task('media', kind='frames', status=status,
                  result=result, outcome='completed')])[0][1]['tasks'][0]
    assert row['outcome'] == outcome
    if status == 'completed':
        assert row['result']['completion_state'] == outcome
        assert row['result']['has_failures'] is (outcome == 'partial_failed')
    else:
        assert 'completion_state' not in row.get('result', {})


def test_scope_uses_exact_hard_key_then_exact_legacy_title_before_heavy_work(get_tasks, monkeypatch):
    sources = [task('own'), task('terminal', status='failed'),
               task('result-owned', dimensions={'type': 'videos'}, result={'project_key': 'project'}),
               task('legacy', dimensions={'type': 'frames', 'title': 'project'}),
               task('prefix', project='project-extra'),
               task('wrong-hard-key', project='different', result={'project_key': 'project'}),
               task('wrong-result-key', dimensions={'title': 'project'},
                    result={'project_key': 'other'}),
               task('legacy-prefix', dimensions={'title': 'project-extra'})]
    scanned = []
    monkeypatch.setattr(server, '_auto_video_runtime_snapshot', lambda result: scanned.append(result) or result)
    monkeypatch.setattr(server, '_media_task_progress_snapshot', lambda _: None)
    data = get_tasks('/api/tasks?limit=0&project_key=project', sources)[0][1]
    assert {row['id'] for row in data['tasks']} == {'own', 'terminal', 'result-owned', 'legacy'}
    assert data['total_count'] == len(scanned) == 4
    assert next(row for row in data['tasks'] if row['id'] == 'terminal')['status'] == 'failed'


def test_include_id_keeps_foreign_terminal_ownership_for_restored_task_guard(get_tasks, monkeypatch):
    monkeypatch.setattr(server, '_media_task_progress_snapshot', lambda _: None)
    sources = [task('own'), task('foreign-frame', project='other', status='failed'),
               task('foreign-video', project='another', status='cancelled'),
               task('foreign-prefix', project='other')]
    query = ('/api/tasks?limit=0&project_key=project&include_id=foreign-frame'
             '&include_id=foreign-video&include_id=foreign-frame&include_id=unknown')
    data = get_tasks(query, sources)[0][1]
    assert data['total_count'] == 3
    rows = {row['id']: row for row in data['tasks']}
    assert set(rows) == {'own', 'foreign-frame', 'foreign-video'}
    assert rows['foreign-frame']['dimensions']['project_key'] == 'other'
    assert rows['foreign-video']['status'] == 'cancelled'


@pytest.mark.parametrize('query,expected', [('', ['new', 'middle', 'old']),
    ('?limit=0', ['new', 'middle', 'old']), ('?limit=-1', ['new', 'middle', 'old']),
    ('?limit=1', ['new']), ('?limit=invalid', ['new', 'middle', 'old']),
    ('?project_key=missing&include_id=unknown', [])])
def test_legacy_listing_order_and_limit_survive_and_limit_precedes_snapshot(get_tasks, monkeypatch,
                                                                           query, expected):
    sources = [task('old', status='completed', active=1),
               task('new', status='completed', active=3),
               task('middle', status='completed', active=2)]
    scanned = []
    monkeypatch.setattr(server, '_auto_video_runtime_snapshot', lambda result: scanned.append(result) or result)
    data = get_tasks('/api/tasks' + query, sources)[0][1]
    assert [row['id'] for row in data['tasks']] == expected
    assert len(scanned) == len(expected)
    assert data['total_count'] == (3 if expected else 0)
