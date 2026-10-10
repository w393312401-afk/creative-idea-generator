"""Progress detection and bounded process discovery without starting processes."""
from types import SimpleNamespace

import pytest

import codex_video_editor as editor


FIRST_BIRTH = 'Tue Oct 6 10:00:00 2026'
SECOND_BIRTH = 'Tue Oct 6 10:01:00 2026'


def process_line(pid, parent, birth=FIRST_BIRTH, state='S', command='python task.py'):
    return f'{pid} {parent} {birth} {state} {command}\n'


def snapshot(monkeypatch, contents, returncode=0):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args == ['/bin/ps', '-axo', 'pid=,ppid=,lstart=,stat=,command=']
        return SimpleNamespace(stdout=contents, returncode=returncode)

    monkeypatch.setattr(editor.subprocess, 'run', run)
    monkeypatch.setattr(editor, '_process_info', lambda *_: pytest.fail('discovery queried an individual PID'))
    return calls


def test_descendant_discovery_prunes_history_using_one_identity_snapshot(monkeypatch):
    process = SimpleNamespace(pid=100, poll=lambda: None,
                              _edit_descendants={pid: FIRST_BIRTH for pid in range(200, 2200)})
    contents = (process_line(100, 1) + process_line(200, 1) + process_line(201, 1, SECOND_BIRTH)
                + process_line(202, 1, state='Z') + process_line(2200, 100)
                + process_line(2201, 2200) + process_line(2202, 200))
    calls = snapshot(monkeypatch, contents)
    assert editor._remember_descendants(process) == {
        200: FIRST_BIRTH, 2200: FIRST_BIRTH, 2201: FIRST_BIRTH, 2202: FIRST_BIRTH,
    }
    assert len(calls) == 1
    assert process._edit_descendants == editor._remember_descendants(process)
    assert len(calls) == 2


def test_observed_root_is_checked_in_snapshot_and_reused_pid_is_excluded(monkeypatch):
    process = editor._ObservedProcess(100, FIRST_BIRTH)
    process.poll = lambda: pytest.fail('observed root made a separate identity lookup')
    process._edit_descendants = {200: FIRST_BIRTH}
    calls = snapshot(monkeypatch, process_line(100, 1, SECOND_BIRTH) + process_line(101, 100)
                     + process_line(200, 1) + process_line(201, 200))
    assert editor._remember_descendants(process) == {200: FIRST_BIRTH, 201: FIRST_BIRTH}
    assert len(calls) == 1


def test_snapshot_failure_preserves_owned_identities_for_safe_cleanup(monkeypatch):
    process = SimpleNamespace(pid=100, poll=lambda: None, _edit_descendants={200: FIRST_BIRTH})
    snapshot(monkeypatch, '', returncode=1)
    assert editor._remember_descendants(process) == {200: FIRST_BIRTH}


def test_descendant_signal_rechecks_identity_after_snapshot(monkeypatch):
    process = SimpleNamespace(_edit_descendants={200: FIRST_BIRTH, 201: FIRST_BIRTH})
    monkeypatch.setattr(editor, '_process_info', lambda pid: (
        f'{FIRST_BIRTH if pid == 200 else SECOND_BIRTH} S python task.py'))
    calls = []
    monkeypatch.setattr(editor.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    editor._signal_descendants(process, editor.signal.SIGTERM)
    assert calls == [(200, editor.signal.SIGTERM)]


@pytest.fixture
def updates(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(editor, '_update', lambda directory, message, stage=None: calls.append((message, stage)))
    (tmp_path / 'work').mkdir()
    return tmp_path, calls


def command_event(command, kind='item.started', exit_code=None):
    return {'type': kind, 'item': {'type': 'command_execution', 'command': command, 'exit_code': exit_code}}


@pytest.mark.parametrize('command', [
    'cat /skill/scripts/render_edit.py',
    '/bin/zsh -lc "sed -n \'1,200p\' /skill/scripts/inspect_video.py"',
    'echo "python /skill/scripts/render_edit.py --plan edit-plan.json"',
    'python helper.py /skill/scripts/render_edit.py',
    'python /skill/scripts/render_edit.py --help',
    'python /skill/scripts/inspect_video.py -h',
    'python -c "print(\'render_edit.py\')"',
    '/bin/zsh -lc "python - <<\'PY\'\ntext = \'已执行 scripts/render_edit.py\'\nPY"',
    "cat <<'DOC'\npython /skill/scripts/render_edit.py --plan edit-plan.json\nDOC",
    'rg render_edit.py inspect_video.py',
])
def test_reading_scripts_or_narrative_text_does_not_change_stage(updates, command):
    directory, calls = updates
    editor._handle_event(directory, command_event(command))
    editor._handle_event(directory, command_event(command, 'item.completed', 0))
    assert calls == []


@pytest.mark.parametrize('command', [
    'python /skill/scripts/render_edit.py --plan edit-plan.json',
    '/venv/bin/python3.12 -u /skill/scripts/render_edit.py --plan edit-plan.json',
    '/bin/zsh -lc "cd work && /venv/bin/python /skill/scripts/render_edit.py --plan edit-plan.json"',
    '/usr/bin/env MODE=trim python /skill/scripts/render_edit.py',
    '/skill/scripts/render_edit.py --plan edit-plan.json',
])
def test_render_invocation_and_successful_completion_update_the_phase(updates, command):
    directory, calls = updates
    editor._handle_event(directory, command_event(command))
    assert calls[-1][1] == 'rendering'
    editor._handle_event(directory, command_event(command, 'item.completed', 0))
    assert calls[-1][1] == 'reviewing_output'
    assert '导出已完成' in calls[-1][0]


def test_failed_render_does_not_report_export_completion(updates):
    directory, calls = updates
    editor._handle_event(directory, command_event('python /skill/scripts/render_edit.py', 'item.completed', 1))
    assert calls == []


@pytest.mark.parametrize('video,stage', [
    ('../input.mp4', 'reviewing_source'), ('edited.mp4', 'reviewing_output'),
])
def test_inspection_uses_the_actual_video_even_when_an_edit_plan_exists(updates, video, stage):
    directory, calls = updates
    (directory / 'work/edit-plan.json').write_text('{}')
    editor._handle_event(directory, command_event(
        f'/bin/zsh -lc "python /skill/scripts/inspect_video.py --video {video} --output-dir qa"'))
    assert calls[-1][1] == stage


def test_inspection_with_inline_video_option_recognizes_output_before_plan(updates):
    directory, calls = updates
    editor._handle_event(directory, command_event('python inspect_video.py --video=edited.mp4 --output-dir qa'))
    assert calls[-1][1] == 'reviewing_output'


def test_script_after_heredoc_is_detected_without_inspecting_its_body(updates):
    directory, calls = updates
    command = "cat <<'DOC'\npython render_edit.py\nDOC\npython inspect_video.py --video edited.mp4"
    editor._handle_event(directory, command_event(command))
    assert calls[-1][1] == 'reviewing_output'
