"""Read-only refresh snapshots retain progress even after early events leave the list tail."""
import pytest

import server


def test_auto_video_is_enabled_when_the_request_omits_the_switch():
    config = {}
    server._configure_frame_auto_video(config, {})
    assert config['_auto_generate_videos'] is True
    assert config['_auto_generate_videos_explicit'] is False
    assert config['videoFramePairing'] == 'auto'
    server._configure_frame_auto_video(config, {'auto_generate_videos': False})
    assert config['_auto_generate_videos'] is False
    assert config['_auto_generate_videos_explicit'] is True
    server._configure_frame_auto_video(config, {'auto_generate_videos': False,
                                               'auto_generate_videos_preference_explicit': True})
    assert config['_auto_generate_videos_preference_explicit'] is True
    with pytest.raises(ValueError, match='preference_explicit'):
        server._configure_frame_auto_video(config, {'auto_generate_videos_preference_explicit': 'true'})


def test_frame_snapshot_counts_full_history_and_retains_active_slot():
    task = {'dimensions': {'type': 'frames', 'target_sequences': [2, 5, 8]},
            'events': [('start', {'total': 3}),
                       ('frame', {'frame': {'sequence': 2}, 'current': 1, 'total': 3}),
                       ('frame', {'frame': {'sequence': 5}, 'current': 2, 'total': 3})]
                     + [('upstream_retry', {'message': 'retry'})] * 60
                     + [('frame_start', {'sequence': 8, 'total': 3})]}
    result = server._media_task_progress_snapshot(task)
    assert result['total'] == 3
    assert result['current'] == 2
    assert result['slotStatus'] == {'2': 'done', '5': 'done', '8': 'active'}
    assert result['percent'] == 65
    assert result['phase'] == 'frame_start'


def test_video_snapshot_uses_full_target_count_across_one_clip_batches():
    task = {'dimensions': {'type': 'videos', 'target_slots': [4, 9]},
            'events': [('start', {'total': 1, 'slots': [4]}),
                       ('video_done', {'index': 4, 'total': 1}),
                       ('video_done', {'index': 4, 'total': 1}),
                       ('start', {'total': 1, 'slots': [9]}),
                       ('video_start', {'index': 9, 'total': 1})]}
    result = server._media_task_progress_snapshot(task)
    assert result['total'] == 2
    assert result['current'] == 1
    assert result['slotStatus'] == {'4': 'done', '9': 'active'}
    assert result['percent'] == 50


def test_compose_tasks_do_not_gain_media_progress():
    assert server._media_task_progress_snapshot({'dimensions': {'type': 'compose'}}) is None


def test_task_list_exposes_progress_and_parent_auto_video_without_full_result(monkeypatch):
    handoff = {'status': 'started', 'task_id': 'child', 'target_slots': [1, 2],
               'queued_slots': [1], 'pending_slots': [2]}
    parent = {'id': 'parent', 'status': 'running', 'dimensions': {'type': 'frames', 'project_key': 'project'},
              'events': [('start', {'total': 3}), ('frame', {'frame': {'sequence': 1}, 'total': 3}),
                         ('frame', {'frame': {'sequence': 2}, 'total': 3})]
                        + [('queue', {'message': 'Waiting for pair'})] * 60,
              'result': {'auto_video': handoff, 'prompt_block': 'large prompt'}, 'error': None, 'last_active': 1}
    child = {'id': 'child', 'status': 'running',
             'dimensions': {'type': 'videos', 'parent_frame_task_id': 'parent', 'project_key': 'project',
                            'target_slots': [1, 2]},
             'events': [('video_done', {'index': 1, 'total': 1})], 'result': None, 'error': None, 'last_active': 2}
    monkeypatch.setattr(server, 'ACTIVE_TASKS', {'parent': parent, 'child': child})
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = '/api/tasks?limit=0'
    handler._gate = lambda: True
    responses = []
    handler._send_json = lambda value, status=200: responses.append(value)
    handler.do_GET()
    tasks = {task['id']: task for task in responses[0]['tasks']}
    assert tasks['parent']['progress']['current'] == 2
    assert tasks['child']['progress']['total'] == 2
    assert tasks['child']['progress']['current'] == 1
    assert tasks['parent']['auto_video'] == handoff
    assert tasks['child']['auto_video'] == handoff
    assert 'prompt_block' not in tasks['parent']['result']
    assert len(tasks['parent']['events']) == 50
    # Images may end before their independent video worker. Its own scheduling
    # snapshot must then win over the parent's completed, older result.
    parent['status'] = 'completed'
    updated = {**handoff, 'queued_slots': [1, 2], 'pending_slots': []}
    child['result'] = {'auto_video': updated}
    handler.do_GET()
    refreshed = {task['id']: task for task in responses[-1]['tasks']}
    assert refreshed['child']['auto_video'] == updated


def test_recovery_snapshot_counts_only_delivered_video_and_stays_running():
    task = {'dimensions': {'type': 'videos', 'target_slots': [1, 2, 3]},
            'status': 'running',
            'events': [('video_done', {'index': 1}),
                       ('video_error', {'index': 2}), ('video_error', {'index': 3}),
                       ('video_recovery', {'phase': 'waiting', 'slots': [2, 3],
                        'completed_slots': [1], 'message': '等待恢复后自动继续'})]}
    result = server._media_task_progress_snapshot(task)
    assert result['current'] == 1
    assert result['slotStatus'] == {'1': 'done', '2': 'recovering', '3': 'recovering'}
    assert result['phase'] == 'video_recovery'
    assert result['percent'] < 95
    assert result['message'] == '等待恢复后自动继续'
    assert task['status'] == 'running'
