"""Integration tests use a fake CLI and a generated one-second fixture, never Codex models."""
import concurrent.futures
import hashlib
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

import codex_video_editor as editor


FAKE_CLI = r'''
import json, os, pathlib, shutil, subprocess, sys, time
from PIL import Image
args = sys.argv[1:]
workspace = pathlib.Path(args[args.index('-C') + 1])
output_json = pathlib.Path(args[args.index('-o') + 1])
prompt = sys.stdin.read()
(workspace / '.received.json').write_text(json.dumps({'args':args, 'prompt':prompt}))
source = workspace.parent / 'input.mp4'
print(json.dumps({'type':'item.completed', 'item':{'type':'reasoning', 'text':'private reasoning sentinel'}}), flush=True)
print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message', 'text':'正在复核剪切边界。'}}), flush=True)
if '[fixture:cancel]' in prompt:
    child = subprocess.Popen([sys.executable, '-c', 'import pathlib,time; time.sleep(20); pathlib.Path(' + repr(str(workspace / 'late-child-write')) + ').write_text("bad")'], start_new_session=True)
    (workspace / '.child.pid').write_text(str(child.pid))
    while True: time.sleep(1)
if '[fixture:wait]' in prompt: time.sleep(1)
if '[fixture:barrier]' in prompt:
    (workspace / '.barrier-ready').touch()
    deadline = time.monotonic() + 10
    while len(list(workspace.parent.parent.glob('*/work/.barrier-ready'))) < 2:
        if time.monotonic() > deadline: raise SystemExit('another editor did not run concurrently')
        time.sleep(0.05)
if '[fixture:hold]' in prompt:
    (workspace / '.hold-ready').touch()
    deadline = time.monotonic() + 10
    while not (workspace / '.hold-release').exists():
        if time.monotonic() > deadline: raise SystemExit('test did not release held editor')
        time.sleep(0.05)
if '[fixture:no_output]' in prompt:
    output_json.write_text(json.dumps({'status':'completed', 'source_reviewed':True, 'qa_reviewed':True}))
    raise SystemExit(0)
if '[fixture:change_input]' in prompt:
    source.chmod(0o644)
    with source.open('ab') as stream: stream.write(b'modified')
output = workspace / 'edited.mp4'
shutil.copyfile(source, output)
planpath = workspace / 'edit-plan.json'
plan = {'source':str(source), 'output_fps':30, 'segments':[{'start':0, 'end':1, 'speed':1, 'reason':'保留有效动作'}]}
planpath.write_text(json.dumps(plan))
(workspace / 'review.md').write_text('Fixture only: simulated visual review of 0–1 seconds; no audio.')
report = {'source':str(source), 'output':str(output), 'plan':str(planpath), 'status':'complete', 'expected_frames':30,
          'segments':plan['segments'], 'validation':{key:True for key in ['frame_count_matches','video_duration_matches','audio_presence_and_duration_match','full_decode_passed']}}
output.with_suffix('.report.json').write_text(json.dumps(report))
for name, media in [('evidence',source), ('qa',output)]:
    folder = workspace / name
    folder.mkdir()
    Image.new('RGB',(8,8),'blue').save(folder / 'frame.png')
    evidence = {'source':str(media), 'analysis_range':{'start_seconds':0,'end_seconds':1}, 'frames':[{'file':'frame.png'}], 'sheets':[{'file':'frame.png'}]}
    (folder / 'evidence.json').write_text(json.dumps(evidence))
result = {'status':'completed', 'message':'fixture complete', 'output_file':str(output), 'review_file':str(workspace/'review.md'),
          'plan_file':str(planpath), 'qa_evidence_file':str(workspace/'qa/evidence.json'), 'source_reviewed':True,
          'qa_reviewed':'[fixture:qa_fail]' not in prompt, 'visual_reviewed_ranges':[{'start':0,'end':1}], 'audio_reviewed':'no_audio', 'error':''}
output_json.write_text(json.dumps(result))
'''


@pytest.fixture
def local_editor(tmp_path, monkeypatch):
    if os.name != 'posix' or not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('POSIX and local media tools required')
    root = tmp_path / 'outputs'
    project = root / '中文 项目'
    project.mkdir(parents=True)
    source = project / '合并 成片.mp4'
    subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=blue:s=64x64:r=30:d=1', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    cli = tmp_path / 'fake codex'
    cli.write_text('#!' + sys.executable + '\n' + FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setattr(editor, 'OUTPUTS_DIR', root)
    monkeypatch.setattr(editor, '_STOP_REQUESTED', False)
    monkeypatch.setenv('CODEX_VIDEO_EDITOR_BIN', str(cli))
    yield root, source
    # Terminate only workers created by this fixture, including assertion failures.
    for directory in editor._job_dirs():
        job = editor._read_json(directory / '.state.json')
        if job and editor._worker_alive(job):
            editor.cancel(job['id'])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not any(editor._worker_alive(editor._read_json(d / '.state.json') or {}) for d in editor._job_dirs()):
            break
        time.sleep(0.1)


def wait_job(source, expected=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = editor.list_jobs(str(source))
        job = jobs[0]
        if job['status'] not in editor.ACTIVE:
            if expected:
                assert job['status'] == expected, job
            return job
        time.sleep(0.05)
    pytest.fail(f'worker did not finish: {jobs}')


def test_real_detached_worker_uses_stdin_private_workspace_and_validates_output(local_editor):
    root, source = local_editor
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    job = editor.start('outputs/中文 项目/合并 成片.mp4', request_id='id with spaces',
                       model='gpt-6.1-sol', reasoning_effort='ultra')
    assert job['status'] == 'queued'
    assert (job['model'], job['reasoning_effort']) == ('gpt-6.1-sol', 'ultra')
    complete = wait_job(source, 'completed')
    assert complete['request_id'] == 'id with spaces'
    assert complete['source_changed'] is False
    assert complete['output']['duration_seconds'] == pytest.approx(1)
    assert Path(complete['output']['file']).is_file()
    assert complete['output']['url'].startswith('/outputs/%E4%B8%AD%E6%96%87%20%E9%A1%B9%E7%9B%AE/codex_edits/')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert 'private reasoning sentinel' not in json.dumps(complete)
    directory = next(editor._job_dirs())
    received = json.loads((directory / 'work/.received.json').read_text())
    request = editor._read_json(directory / '.request.json')
    assert (request['model'], request['reasoning_effort']) == ('gpt-6.1-sol', 'ultra')
    assert received['args'][:5] == ['--no-daemon', '-a', 'never', 'exec', '--json']
    assert '--sandbox' in received['args'] and 'workspace-write' in received['args']
    assert received['args'][received['args'].index('--model') + 1] == 'gpt-6.1-sol'
    assert received['args'][received['args'].index('-c') + 1] == 'model_reasoning_effort=ultra'
    assert received['args'][received['args'].index('-C') + 1] == str(directory / 'work')
    assert '$timelapse-video-editor' in received['prompt']
    assert str(editor.SKILL_DIR / 'SKILL.md') in received['prompt']
    assert received['args'][-1] == '-'
    assert not any(key.startswith('_') for key in complete)


def test_parallel_idempotency_and_independent_sources(local_editor):
    root, source = local_editor
    other = source.with_name('其他.mp4')
    shutil.copyfile(source, other)
    def start(_):
        return editor.start(str(source), notes='[fixture:barrier]', request_id='same-request')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        duplicates = [pool.submit(start, index) for index in range(5)]
        other_future = pool.submit(editor.start, str(other), notes='[fixture:barrier]', request_id='other-request')
        results = [future.result() for future in duplicates]
        second = other_future.result()
    assert len({job['id'] for job in results}) == 1
    assert (results[0]['model'], results[0]['reasoning_effort']) == ('gpt-6.1-sol', 'high')
    assert second['id'] != results[0]['id']
    assert second['source'] != results[0]['source']
    same_source = editor.start(str(source), notes='[fixture:barrier]', request_id='new-transport-request')
    assert same_source['id'] == results[0]['id']
    with pytest.raises(editor.VideoEditError) as changed:
        editor.start(str(source), mode='trim_speed', request_id='same-request')
    assert changed.value.status == 409
    for kwargs in ({'model': 'gpt-6-astra'}, {'reasoning_effort': 'max'}):
        with pytest.raises(editor.VideoEditError) as changed:
            editor.start(str(source), notes='[fixture:barrier]', request_id='same-request', **kwargs)
        assert changed.value.code == 'EDIT_REQUEST_CONFLICT'
    with pytest.raises(editor.VideoEditError) as busy:
        editor.start(str(source), notes='different edit while active', request_id='another-request')
    assert busy.value.code == 'EDITOR_BUSY'
    with pytest.raises(editor.VideoEditError) as changed:
        editor.start(str(other), notes='[fixture:barrier]', request_id='same-request')
    assert changed.value.code == 'EDIT_REQUEST_CONFLICT'
    # Each fake Codex waits for the other to enter its own workspace. A serial
    # implementation cannot make both jobs complete successfully.
    assert wait_job(source, 'completed')['id'] == results[0]['id']
    assert wait_job(other, 'completed')['id'] == second['id']
    directory = next(d for d in editor._job_dirs() if d.name == results[0]['id'])
    received = json.loads((directory / 'work/.received.json').read_text())
    assert received['args'][received['args'].index('--model') + 1] == 'gpt-6.1-sol'
    assert received['args'][received['args'].index('-c') + 1] == 'model_reasoning_effort=high'
    assert len(list(editor._job_dirs())) == 2


@pytest.mark.parametrize('model,reasoning_effort', [
    ('gpt-6.1-sol', 'ultra'), ('gpt-6.1-sol', 'low'),
    ('gpt-6-astra', 'ultra'), ('gpt-6-sol', 'ultra'), ('gpt-6-luna', 'max'),
    ('gpt-6-luna', 'low'),
])
def test_supported_model_reasoning_combinations(model, reasoning_effort):
    assert editor._model_settings(model, reasoning_effort) == (model, reasoning_effort)


@pytest.mark.parametrize('model,reasoning_effort,part', [
    ('gpt-5.5', 'high', '模型'), ('claude-sonnet-4-6', 'high', '模型'),
    ('', 'high', '模型'), (42, 'high', '模型'),
    ('gpt-6-luna', 'ultra', '思考强度'), ('gpt-6-sol', 'none', '思考强度'),
    ('gpt-6-astra', '', '思考强度'), ('gpt-6-sol', 42, '思考强度'),
])
def test_invalid_model_reasoning_is_rejected_before_source_or_worker(model, reasoning_effort, part):
    with pytest.raises(editor.VideoEditError, match=part) as failure:
        editor.start('missing.mp4', model=model, reasoning_effort=reasoning_effort)
    assert failure.value.status == 400


def test_legacy_queued_request_keeps_original_model_when_run_after_upgrade(local_editor):
    root, source = local_editor
    directory = source.parent / 'codex_edits' / ('a' * 32)
    directory.mkdir(parents=True)
    shutil.copyfile(source, directory / 'input.mp4')
    editor._write_json(directory / '.state.json', {
        'id': directory.name, 'source': '/outputs/中文 项目/合并 成片.mp4',
        'status': 'running', 'logs': [],
    })
    # The persisted request came from the version before model preferences.
    result = editor._run_codex(directory, {'mode': 'trim', 'notes': '', 'tools': editor._tools()})
    assert result['status'] == 'completed'
    received = editor._read_json(directory / 'work/.received.json')
    assert received['args'][received['args'].index('--model') + 1] == 'gpt-6-sol'
    assert received['args'][received['args'].index('-c') + 1] == 'model_reasoning_effort=high'


def test_cancel_kills_descendant_with_its_own_session(local_editor):
    _, source = local_editor
    job = editor.start(str(source), notes='[fixture:cancel]')
    directory = next(editor._job_dirs())
    pidfile = directory / 'work/.child.pid'
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), editor.list_jobs(str(source))
    child_pid = int(pidfile.read_text())
    assert editor._birth(editor._process_info(child_pid))
    response = editor.cancel(job['id'])
    assert response['stage'] == 'cancelling'
    cancelled = wait_job(source, 'cancelled')
    assert cancelled['output'] is None
    assert not editor._birth(editor._process_info(child_pid)), 'separately-sessioned descendant survived cancellation'
    assert not (directory / 'work/late-child-write').exists()
    assert source.is_file()


def test_cancelling_one_concurrent_edit_does_not_interrupt_other(local_editor):
    _, source = local_editor
    other = source.with_name('另一段成片.mp4')
    shutil.copyfile(source, other)
    cancelled_job = editor.start(str(source), notes='[fixture:cancel]', request_id='cancel-one')
    completing_job = editor.start(str(other), notes='[fixture:hold]', request_id='keep-other')
    assert cancelled_job['id'] != completing_job['id']
    directories = {directory.name: directory for directory in editor._job_dirs()}
    child_pid_file = directories[cancelled_job['id']] / 'work/.child.pid'
    other_workspace = directories[completing_job['id']] / 'work'
    deadline = time.monotonic() + 10
    while (not child_pid_file.exists() or not (other_workspace / '.hold-ready').exists()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid_file.exists(), editor.list_jobs(str(source))
    assert (other_workspace / '.hold-ready').exists(), editor.list_jobs(str(other))
    child_pid = int(child_pid_file.read_text())
    assert editor.cancel(cancelled_job['id'])['stage'] == 'cancelling'
    assert wait_job(source, 'cancelled')['id'] == cancelled_job['id']
    assert not editor._birth(editor._process_info(child_pid))
    assert editor.list_jobs(str(other))[0]['status'] in editor.ACTIVE
    (other_workspace / '.hold-release').touch()
    completed = wait_job(other, 'completed')
    assert completed['id'] == completing_job['id']
    assert Path(completed['output']['file']).is_file()
    assert source.is_file() and other.is_file()


@pytest.mark.parametrize('mode,error_part', [
    ('qa_fail', '视觉审阅'), ('no_output', '必需文件'), ('change_input', '输入副本被改动'),
])
def test_exit_zero_is_not_completion_without_qa_and_intact_input(local_editor, mode, error_part):
    _, source = local_editor
    editor.start(str(source), notes=f'[fixture:{mode}]')
    job = wait_job(source, 'failed')
    assert error_part in job['error']
    assert job['output'] is None


def test_validation_rejects_bad_sources_before_start(local_editor, tmp_path):
    root, source = local_editor
    outside = tmp_path / 'outside.mp4'
    shutil.copyfile(source, outside)
    link = source.with_name('link.mp4')
    link.symlink_to(outside)
    bad = source.with_name('not-video.mp4')
    bad.write_bytes(b'not an mp4')
    for value in (str(outside), str(link), '/outputs/../outside.mp4', str(bad), 'https://example.com/a.mp4'):
        with pytest.raises(editor.VideoEditError) as error:
            editor.start(value)
        assert error.value.status == 400
    assert not list(editor._job_dirs())


def test_source_replacement_marks_history_without_rewriting_job(local_editor):
    _, source = local_editor
    editor.start(str(source), request_id='old-source')
    job = wait_job(source, 'completed')
    directory = next(editor._job_dirs())
    original_state = (directory / '.state.json').read_bytes()
    replacement = source.with_suffix('.new.mp4')
    shutil.copyfile(source, replacement)
    os.replace(replacement, source)
    history = editor.list_jobs(str(source))[0]
    assert history['id'] == job['id'] and history['source_changed'] is True
    assert (directory / '.state.json').read_bytes() == original_state


def test_missing_or_reused_worker_pid_is_interrupted_not_completed(local_editor):
    _, source = local_editor
    job = editor.start(str(source), notes='[fixture:wait]')
    directory = next(editor._job_dirs())
    editor.cancel(job['id'])
    wait_job(source, 'cancelled')
    state = editor._read_json(directory / '.state.json')
    state.update(status='running', _worker_pid=os.getpid(), _worker_token='unrelated-process')
    editor._save(directory, state)
    recovered = editor.list_jobs(str(source))[0]
    assert recovered['status'] == 'interrupted'
    assert recovered['output'] is None


def test_non_posix_capability_is_unavailable_without_import_failure(monkeypatch):
    monkeypatch.setattr(editor, 'fcntl', None)
    assert editor.capabilities()['available'] is False
    assert 'POSIX' in editor.capabilities()['message']


def test_guardian_cleans_cli_and_separate_session_child_after_worker_is_killed(local_editor):
    _, source = local_editor
    editor.start(str(source), notes='[fixture:cancel]')
    directory = next(editor._job_dirs())
    pidfile = directory / 'work/.child.pid'
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pidfile.exists()
    child_pid = int(pidfile.read_text())
    # Allow one guardian snapshot after the fake CLI has spawned its detached child.
    time.sleep(0.3)
    state = editor._read_json(directory / '.state.json')
    assert editor._guardian_alive(state)
    os.kill(state['_worker_pid'], signal.SIGKILL)
    job = wait_job(source, 'interrupted')
    assert not editor._birth(editor._process_info(child_pid))
    assert job['output'] is None
    assert not (directory / 'work/late-child-write').exists()


def test_percent_in_project_directory_is_rejected_but_source_basename_is_supported(local_editor):
    root, source = local_editor
    bad_project = root / '项目100%'
    bad_project.mkdir()
    bad_source = bad_project / 'input.mp4'
    shutil.copyfile(source, bad_source)
    with pytest.raises(editor.VideoEditError, match='暂不支持路径含 %') as failure:
        editor.start(str(bad_source))
    assert failure.value.status == 400
    good_source = source.with_name('成片100%.mp4')
    source.rename(good_source)
    job = editor.start(str(good_source))
    assert '%25' in job['source']
    wait_job(good_source, 'completed')
