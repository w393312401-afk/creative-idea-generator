"""IP recovery is task-level progress, including when no clip can be selected."""
from unittest.mock import Mock

import pytest

from video_generator import _BatchBridge


@pytest.mark.parametrize('stage,expected', [
    ('ip_rotating', '正在第 2 次换 IP'),
    ('ip_rotated', '第 2 次换 IP，已验证：192.0.2.1 → 192.0.2.2'),
    ('ip_rotation_failed', '已停止重试'),
])
def test_rotation_progress_reaches_task_stream_without_changing_clip_state(stage, expected):
    events = []
    writer = Mock()
    bridge = _BatchBridge([], 36, 'veo', writer,
                          lambda name, data: events.append((name, data)))
    details = {'retry': 2, 'old_ip': '192.0.2.1', 'new_ip': '192.0.2.2',
               'user_id': 'profile-1', 'reason': 'unusual_activity', 'blocked_ip_count': 2}

    bridge(99, stage, details)

    assert len(events) == 1
    assert events[0][0] == stage
    payload = events[0][1]
    assert payload.items() >= details.items()
    assert payload['total'] == 36
    assert expected in payload['message']
    assert 'index' not in payload
    assert 'message' not in details
    writer.record.assert_not_called()


def test_rotation_failure_keeps_upstream_stop_reason():
    progress = Mock()
    bridge = _BatchBridge([], 36, 'veo', Mock(), progress)
    message = '可用出口已用尽，已停止重试'
    bridge(0, 'ip_rotation_failed', {'message': message, 'retry': 7})
    assert progress.call_args.args == ('ip_rotation_failed', {
        'message': message, 'retry': 7, 'total': 36,
    })


def test_bounded_wait_warning_reaches_progress_without_clip_lookup():
    progress = Mock()
    bridge = _BatchBridge([], 36, 'veo', Mock(), progress)
    bridge(0, 'video_warning', {'message': '最多等待 30 秒收取在途视频'})
    progress.assert_called_once_with('video_warning', {
        'message': '最多等待 30 秒收取在途视频', 'total': 36,
    })


def test_account_switching_is_visible_before_a_replacement_is_found():
    progress = Mock()
    bridge = _BatchBridge([], 36, 'veo', Mock(), progress)
    bridge(0, 'account_switching', {'previous': 'account-a', 'retry': 3,
                                  'reason': 'unusual_activity', 'message': '旧出口失败'})
    stage, payload = progress.call_args.args
    assert stage == 'video_warning'
    assert payload['previous'] == 'account-a' and payload['retry'] == 3
    assert '核对备用账号积分并切号' in payload['message']
