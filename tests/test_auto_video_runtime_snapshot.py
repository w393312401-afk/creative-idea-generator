"""Read APIs must not advertise a terminal automatic-video child as still running."""

import copy
import json

import pytest

import server
import server_common


PARENT_ID = 'frames_snapshot_parent'
CHILD_ID = 'videos_snapshot_child'


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / 'project'
    directory.mkdir()
    manifest = {
        'title': 'snapshot project',
        'frames': [{'slot': 1, 'file': 'frame.webp', 'quality_gate': 'auto_approved'}],
        'videos': [{'slot': 1, 'status': 'pending', 'file': '', 'last_attempt': {
            'submission_id': 'paid-operation', 'submission_pending': True,
            'upstream_task_id': 'remote-task', 'confirmed': True,
        }}],
        'prompt_block': 'IMAGE 1: image\nVIDEO 1: action',
        'auto_video': {
            'status': 'started', 'message': '正在生成视频 1',
            'task_id': CHILD_ID, 'request_id': 'auto_video:' + PARENT_ID,
            'frame_pairs': [{'slot': 1, 'start_anchor_slot': 1, 'end_anchor_slot': 2}],
            'target_slots': [1, 2], 'ready_slots': [1], 'queued_slots': [1],
            'pending_slots': [2], 'blocked_slots': [],
        },
    }
    server_common.write_manifest(str(directory), manifest)
    monkeypatch.setattr(server, '_get_project_dir', lambda _title: str(directory))
    # This regression isolates runtime-state projection from media disk migration.
    monkeypatch.setattr(server, 'sync_project_manifest_with_disk', lambda _directory: None)
    parent = server.get_or_create_task(PARENT_ID, {'type': 'frames', 'project_key': 'project'})
    child = server.get_or_create_task(CHILD_ID, {
        'type': 'videos', 'project_key': 'project', 'parent_frame_task_id': PARENT_ID,
        'progressive': True,
    })
    parent['result'] = copy.deepcopy(manifest)
    child['result'] = copy.deepcopy(manifest)
    parent['status'] = 'completed'
    return directory, manifest, parent, child


def response(path):
    handler = object.__new__(server.SparkRequestHandler)
    handler.path = path
    handler._gate = lambda: True
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))
    handler.do_GET()
    assert len(sent) == 1 and sent[0][1] == 200
    return sent[0][0]


@pytest.mark.parametrize('status,outcome,terminal_info,expected', [
    ('failed', None, None, 'blocked'),
    ('cancelled', None, None, 'cancelled'),
    ('completed', None, None, 'completed'),
    ('completed', 'partial_failed', None, 'partial_failed'),
    ('completed', 'completed_with_warnings', None, 'completed_with_warnings'),
    ('completed', None, {'status': 'partial_failed', 'message': 'Only one clip succeeded'}, 'partial_failed'),
    ('completed', None, {'status': 'skipped', 'message': 'No new clips needed'}, 'skipped'),
    ('completed', 'partial_failed', {'status': 'completed', 'message': 'old completion'}, 'partial_failed'),
])
def test_runtime_snapshot_uses_referenced_child_terminal_state_without_mutating_media(
        project, status, outcome, terminal_info, expected):
    _, manifest, _, child = project
    child.update(status=status, outcome=outcome, error='服务已重启，生成中断。' if status == 'failed' else None)
    if terminal_info:
        child['result']['auto_video'] = {**terminal_info, 'task_id': CHILD_ID}
    original = copy.deepcopy(manifest)
    child_result = copy.deepcopy(child['result'])
    snapshot = server._auto_video_runtime_snapshot(manifest)
    assert snapshot['auto_video']['status'] == expected
    assert snapshot['auto_video']['message'] != original['auto_video']['message']
    for key, value in original.items():
        if key != 'auto_video':
            assert snapshot[key] == value
    for key, value in original['auto_video'].items():
        if key not in ('status', 'message'):
            assert snapshot['auto_video'][key] == value
    assert manifest == original
    assert child['result'] == child_result


@pytest.mark.parametrize('status', ['running', 'queued', 'interrupted', 'missing'])
def test_unknown_or_nonterminal_child_does_not_clear_auto_video_state(project, status):
    _, manifest, _, child = project
    child['status'] = status
    if status == 'missing':
        server.ACTIVE_TASKS.pop(CHILD_ID)
        unrelated = server.get_or_create_task('videos_unrelated', {'type': 'videos'})
        unrelated['status'] = 'failed'
    assert server._auto_video_runtime_snapshot(manifest) == manifest


@pytest.mark.parametrize('status,outcome,expected', [
    ('failed', None, 'blocked'),
    ('cancelled', None, 'cancelled'),
    ('completed', 'partial_failed', 'partial_failed'),
])
def test_manifest_status_and_task_list_snapshots_agree_for_terminal_auto_child(project, status, outcome, expected):
    directory, original, parent, child = project
    child.update(status=status, outcome=outcome, error='stopped child' if status != 'completed' else None)
    snapshots = [response('/api/get_manifest?title=project')['auto_video']]
    for task_id in (PARENT_ID, CHILD_ID):
        status_response = response('/api/compose-status?task_id=' + task_id)
        snapshots.extend([status_response['auto_video'], status_response['result']['auto_video']])
    listed = response('/api/tasks?limit=0')['tasks']
    snapshots.extend(task['auto_video'] for task in listed if task['id'] in (PARENT_ID, CHILD_ID))
    assert len(snapshots) == 7
    assert all(info['status'] == expected for info in snapshots)
    assert all(info == snapshots[0] for info in snapshots)
    assert json.loads((directory / 'manifest.json').read_text()) == original
    assert parent['result'] == original
    assert child['result'] == original


def test_restart_interrupted_child_is_projected_as_blocked_without_rewriting_manifest(project):
    directory, original, parent, child = project
    parent['status'] = child['status'] = 'running'
    assert server_common.save_task_to_disk(PARENT_ID)
    assert server_common.save_task_to_disk(CHILD_ID)
    server.ACTIVE_TASKS.clear()
    server_common.load_tasks_from_disk()
    assert server.ACTIVE_TASKS[CHILD_ID]['status'] == 'failed'
    manifest = response('/api/get_manifest?title=project')
    child_status = response('/api/compose-status?task_id=' + CHILD_ID)
    assert manifest['auto_video']['status'] == 'blocked'
    assert '服务已重启' in manifest['auto_video']['message']
    assert child_status['result']['auto_video'] == manifest['auto_video']
    assert json.loads((directory / 'manifest.json').read_text()) == original
