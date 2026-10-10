"""Offline merge lifecycle regressions: retain, publish, cancel, serialize."""
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
import video_generator as vg


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / 'videos'
    folder.mkdir()
    clip = folder / 'vid_001.mp4'
    clip.write_bytes(b'clip')
    data = {'title': 'retention', 'videos': [dict(slot=1, status='success', file=str(clip))]}
    (tmp_path / 'manifest.json').write_text(json.dumps(data))
    old = tmp_path / 'retention_2x.mp4'
    old.write_bytes(b'previous final')
    other = tmp_path / 'keep-export.mp4'
    other.write_bytes(b'other export')
    return tmp_path, old, other, data


@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('failure', ['encode', 'validate', 'cancel', None])
def test_full_and_partial_publish_only_valid_outputs(project, partial, failure):
    root, old, other, data = project
    if partial:
        data['videos'].append(dict(slot=2, status='failed', file=''))
        (root / 'manifest.json').write_text(json.dumps(data))
        old = root / 'retention_partial_2x.mp4'
        old.write_bytes(b'previous final')
    cancel = {'flag': False}
    events = []
    def command(cmd, **_kwargs):
        assert old.read_bytes() == b'previous final'
        assert Path(cmd[-1]).parent != root
        Path(cmd[-1]).write_bytes(b'new final')
        return subprocess.CompletedProcess(cmd, 1 if failure == 'encode' else 0, '', 'encoding failure')
    def validate(path, **_kwargs):
        assert old.read_bytes() == b'previous final'
        assert Path(path).read_bytes() == b'new final'
        if failure == 'validate':
            raise ValueError('invalid output')
        if failure == 'cancel':
            cancel['flag'] = True
        return 4.0
    with patch.object(vg, '_probe_merge_clip', return_value={
            'width': 160, 'height': 120, 'fps': 24.0, 'duration': 8.0, 'has_audio': False}), \
         patch.object(vg, '_clip_has_audio', return_value=False), \
         patch.object(vg, 'prepend_cover_intro', side_effect=lambda p,m,files,*a,**k:(files,None)), \
         patch.object(vg, '_run_media_command', side_effect=command), \
         patch.object(vg, '_validate_merged_output', side_effect=validate):
        kwargs = dict(allow_partial=partial, speed=2, config={'reviewsDisabled': True},
                      on_progress=lambda s,d: events.append((s,d)), cancel_check=lambda: cancel['flag'])
        if failure:
            with pytest.raises((RuntimeError, ValueError, ConnectionError)):
                vg.merge_project_videos(str(root), **kwargs)
            assert old.read_bytes() == b'previous final'
        else:
            result = vg.merge_project_videos(str(root), **kwargs)
            assert result['status'] == 'success'
            assert old.read_bytes() == b'new final'
    assert other.read_bytes() == b'other export'
    assert not list(root.glob('.merge-*'))
    assert all(stage == 'merge_progress' for stage, _ in events)


def test_running_media_child_is_terminated_on_cancel(tmp_path):
    began = time.monotonic()
    def cancelled():
        return time.monotonic() - began > .35
    children = []
    popen = subprocess.Popen
    def start(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    with patch.object(vg.subprocess, 'Popen', side_effect=start):
        with pytest.raises(ConnectionError):
            with vg._merge_operation(str(tmp_path), cancel_check=cancelled):
                vg._run_media_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                                      capture_output=True, text=True)
    assert time.monotonic() - began < 3
    assert children and children[0].poll() is not None


def test_media_command_timeout_is_bounded(tmp_path):
    with vg._merge_operation(str(tmp_path), on_progress=lambda *_: None):
        with pytest.raises(subprocess.TimeoutExpired):
            vg._run_media_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  capture_output=True, timeout=.1)


def test_cancel_during_probe_is_reported_as_cancellation(tmp_path):
    cancel = {'flag': False}

    def interrupted_probe(_path):
        cancel['flag'] = True
        return None  # Probe helpers tolerate failures, including terminated children.

    with patch.object(vg, '_ffprobe_video_params', side_effect=interrupted_probe):
        with vg._merge_operation(str(tmp_path), cancel_check=lambda: cancel['flag']):
            with pytest.raises(ConnectionError, match='合并已取消'):
                vg._probe_merge_clip(str(tmp_path / 'clip.mp4'))


def test_waiting_project_merge_can_cancel_without_entering(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    second_entered = []
    def first():
        with vg._merge_operation(str(tmp_path)):
            entered.set()
            assert release.wait(3)
    def second():
        with vg._merge_operation(str(tmp_path), cancel_check=cancel.is_set):
            second_entered.append(True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first)
        assert entered.wait(1)
        waiter = pool.submit(second)
        cancel.set()
        try:
            with pytest.raises(ConnectionError):
                waiter.result(timeout=2)
        finally:
            release.set()
        owner.result(timeout=2)
    assert not second_entered
