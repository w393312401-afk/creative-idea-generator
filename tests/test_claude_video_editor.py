"""Claude Code 精剪引擎：用假 claude 命令行与一秒夹具验证真实的后台流程，从不调用真实模型。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

import codex_video_editor as editor
import cta_burn


FAKE_CLAUDE = r'''
import json, os, pathlib, shlex, shutil, sys, time
from PIL import Image
args = sys.argv[1:]
prompt = sys.stdin.read()
cwd = pathlib.Path.cwd()
(cwd / '.received.json').write_text(json.dumps({
    'args': args, 'prompt': prompt, 'cwd': str(cwd),
    'env': {key: os.environ.get(key) for key in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')},
    'skill_copy': sorted(path.name for path in (cwd / 'skill').iterdir()) if (cwd / 'skill').is_dir() else None}))
source = cwd.parent / 'input.mp4'
def emit(event): print(json.dumps(event), flush=True)
emit({'type': 'system', 'subtype': 'init', 'cwd': str(cwd), 'model': args[args.index('--model') + 1]})
emit({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'role': 'assistant', 'content': [
    {'type': 'thinking', 'thinking': 'private reasoning sentinel'}]}})
emit({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'role': 'assistant', 'content': [
    {'type': 'text', 'text': '正在复核剪切边界。'}]}})
if '[fixture:login]' in prompt:
    emit({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Invalid API key · Please run /login'})
    raise SystemExit(1)
if '[fixture:cancel]' in prompt:
    while True: time.sleep(1)
emit({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'role': 'assistant', 'content': [
    {'type': 'tool_use', 'id': 'toolu_render', 'name': 'Bash',
     'input': {'command': 'python3 ' + shlex.quote(str(cwd / 'skill/scripts/render_edit.py')) + ' --plan edit-plan.json'}}]}})
output = cwd / 'edited.mp4'
shutil.copyfile(source, output)
planpath = cwd / 'edit-plan.json'
plan = {'source': str(source), 'output_fps': 30, 'segments': [{'start': 0, 'end': 1, 'speed': 1, 'reason': '保留有效动作'}]}
planpath.write_text(json.dumps(plan))
(cwd / 'review.md').write_text('Fixture only: simulated visual review of 0–1 seconds; no audio.')
report = {'source': str(source), 'output': str(output), 'plan': str(planpath), 'status': 'complete', 'expected_frames': 30,
          'segments': plan['segments'], 'validation': {key: True for key in [
              'frame_count_matches', 'video_duration_matches', 'audio_presence_and_duration_match', 'full_decode_passed']}}
output.with_suffix('.report.json').write_text(json.dumps(report))
for name, media in [('evidence', source), ('qa', output)]:
    folder = cwd / name
    folder.mkdir()
    Image.new('RGB', (8, 8), 'blue').save(folder / 'frame.png')
    (folder / 'evidence.json').write_text(json.dumps({
        'source': str(media), 'analysis_range': {'start_seconds': 0, 'end_seconds': 1},
        'frames': [{'file': 'frame.png'}], 'sheets': [{'file': 'frame.png'}]}))
emit({'type': 'user', 'parent_tool_use_id': None, 'message': {'role': 'user', 'content': [
    {'type': 'tool_result', 'tool_use_id': 'toolu_render', 'content': 'ok', 'is_error': False}]}})
result = {'status': 'completed', 'message': 'fixture complete', 'output_file': str(output), 'review_file': str(cwd / 'review.md'),
          'plan_file': str(planpath), 'qa_evidence_file': str(cwd / 'qa/evidence.json'), 'source_reviewed': True,
          'qa_reviewed': True, 'visual_reviewed_ranges': [{'start': 0, 'end': 1}], 'audio_reviewed': 'no_audio', 'error': ''}
if '[fixture:text-json]' in prompt:
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '```json\n' + json.dumps(result) + '\n```',
          'total_cost_usd': 0.0, 'num_turns': 3})
elif '[fixture:checkpoint]' in prompt:
    (cwd / 'completion.json').write_text(json.dumps(result))
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 0.0, 'num_turns': 3})
else:
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(result),
          'structured_output': result, 'total_cost_usd': 0.4237, 'num_turns': 5})
'''


@pytest.fixture
def claude_editor(tmp_path, monkeypatch):
    if os.name != 'posix' or not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('POSIX and local media tools required')
    root = tmp_path / 'outputs'
    project = root / '中文 项目'
    project.mkdir(parents=True)
    source = project / '合并 成片.mp4'
    subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=blue:s=64x64:r=30:d=1', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    cli = tmp_path / 'fake claude'
    cli.write_text('#!' + sys.executable + '\n' + FAKE_CLAUDE)
    cli.chmod(0o755)
    # A tiny skill keeps the tests independent of whichever editing skills the machine has installed.
    skill = tmp_path / 'skill'
    (skill / 'scripts').mkdir(parents=True)
    (skill / 'SKILL.md').write_text('# timelapse-video-editor (fixture)\n')
    for script in ('inspect_video.py', 'render_edit.py'):
        (skill / 'scripts' / script).write_text('# fixture\n')
    (skill / '__pycache__').mkdir()
    (skill / '__pycache__' / 'junk.pyc').write_bytes(b'junk')
    monkeypatch.setattr(editor, 'SKILL_DIR', skill)
    monkeypatch.setattr(editor, 'CLAUDE_SKILL_DIR', tmp_path / 'no-claude-skill')
    monkeypatch.setattr(editor, 'OUTPUTS_DIR', root)
    monkeypatch.setattr(editor, '_STOP_REQUESTED', False)
    monkeypatch.setenv('CLAUDE_VIDEO_EDITOR_BIN', str(cli))
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CLAUDE_CODE_ENTRYPOINT', 'cli')
    cta_burn.save_settings({'enabled': False}, root / 'engagement_cta')
    yield root, source, skill
    for directory in editor._job_dirs():
        job = editor._read_json(directory / '.state.json')
        if job and editor._worker_alive(job):
            editor.cancel(job['id'])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not any(editor._worker_alive(editor._read_json(d / '.state.json') or {}) for d in editor._job_dirs()):
            break
        time.sleep(0.1)


def wait_job(source, expected=None, timeout=20):
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


def test_claude_job_runs_sandboxed_cli_with_structured_output_and_validates(claude_editor):
    root, source, skill = claude_editor
    job = editor.start('outputs/中文 项目/合并 成片.mp4', request_id='claude-1', engine='claude',
                       model='claude-sonnet-5-5', reasoning_effort='max')
    assert (job['engine'], job['model'], job['reasoning_effort']) == ('claude', 'claude-sonnet-5-5', 'max')
    complete = wait_job(source, 'completed')
    assert complete['engine'] == 'claude'
    assert complete['output']['duration_seconds'] == pytest.approx(1)
    assert Path(complete['output']['file']).is_file()
    assert 'private reasoning sentinel' not in json.dumps(complete)
    assert any('估算花费约 $0.42' in row['message'] for row in complete['logs'])
    assert any('正在按剪辑计划导出新视频' in row['message'] for row in complete['logs'])
    assert any('视频导出已完成' in row['message'] for row in complete['logs'])

    directory = next(editor._job_dirs())
    received = json.loads((directory / 'work/.received.json').read_text())
    args = received['args']
    value = lambda flag: args[args.index(flag) + 1]
    assert args[:1] == ['-p']
    assert (value('--output-format'), '--verbose' in args) == ('stream-json', True)
    assert value('--model') == 'claude-sonnet-5-5' and value('--effort') == 'max'
    # 隔离：不带宿主的 CLAUDE.md/技能/插件/钩子，文件工具限制在工作区，没有联网工具，也不会弹权限提示。
    for flag in ('--safe-mode', '--restricted', '--strict-mcp-config', '--no-session-persistence'):
        assert flag in args, flag
    assert (value('--permission-mode'), value('--permission-prompts')) == ('dontAsk', 'none')
    assert value('--tools') == 'Bash,Read,Write,Edit,Glob,Grep'
    # Bash 直接放行（否则 cd 出工作区读 input.mp4 会被 dontAsk 拒绝）；写入边界由下面的 OS 沙箱保证。
    assert value('--allowedTools') == 'Bash,Read,Write,Edit,Glob,Grep'
    sandbox = json.loads(value('--settings'))['sandbox']
    assert sandbox['enabled'] is True and sandbox['allowUnsandboxedCommands'] is False
    assert sandbox['failIfUnavailable'] is True
    assert json.loads(value('--json-schema')) == editor.RESULT_SCHEMA
    assert received['cwd'] == str((directory / 'work').resolve())
    assert received['env'] == {'CLAUDECODE': None, 'CLAUDE_CODE_ENTRYPOINT': None}
    # 技能被复制进工作区（缓存文件除外），提示词指向这份副本而不是原目录。
    assert received['skill_copy'] == ['SKILL.md', 'scripts']
    assert str(directory / 'work/skill/SKILL.md') in received['prompt']
    assert str(skill / 'SKILL.md') not in received['prompt']
    assert '$timelapse-video-editor' not in received['prompt']
    request = editor._read_json(directory / '.request.json')
    assert request['engine'] == 'claude'
    assert editor._read_json(directory / '.result.json')['status'] == 'completed'


def test_reply_text_json_and_checkpoint_are_accepted_when_structured_output_is_missing(claude_editor):
    _, source, _ = claude_editor
    for marker in ('text-json', 'checkpoint'):
        editor.start(str(source), notes=f'[fixture:{marker}]', request_id=marker, engine='claude')
        complete = wait_job(source, 'completed')
        assert Path(complete['output']['file']).is_file(), marker
        shutil.rmtree(next(editor._job_dirs()))


def test_login_failure_is_reported_without_leaking_the_raw_reply(claude_editor):
    _, source, _ = claude_editor
    editor.start(str(source), notes='[fixture:login]', engine='claude')
    failed = wait_job(source, 'failed')
    assert 'Claude Code 尚未登录' in failed['error']
    assert failed['output'] is None


def test_cancel_stops_the_claude_cli(claude_editor):
    _, source, _ = claude_editor
    job = editor.start(str(source), notes='[fixture:cancel]', engine='claude')
    directory = next(editor._job_dirs())
    deadline = time.monotonic() + 10
    while not (directory / 'work/.received.json').exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert editor.cancel(job['id'])['stage'] == 'cancelling'
    cancelled = wait_job(source, 'cancelled')
    assert cancelled['output'] is None and source.is_file()


def test_engine_changes_the_request_fingerprint_but_not_codex_history(claude_editor):
    _, source, _ = claude_editor
    first = editor.start(str(source), request_id='same', engine='claude', notes='[fixture:cancel]')
    with pytest.raises(editor.VideoEditError) as conflict:
        editor.start(str(source), request_id='same', engine='codex', notes='[fixture:cancel]')
    assert conflict.value.code == 'EDIT_REQUEST_CONFLICT'
    assert editor.start(str(source), request_id='same', engine='claude', notes='[fixture:cancel]')['id'] == first['id']
    editor.cancel(first['id'])
    wait_job(source, 'cancelled')


@pytest.mark.parametrize('engine,model,effort,part', [
    ('gemini', None, None, '引擎'), (42, None, None, '引擎'),
    ('claude', 'gpt-6.1-sol', 'high', '模型'), ('claude', 'claude-opus-4-1', 'high', '模型'), ('claude', '', 'high', '模型'),
    ('claude', 'claude-opus-5-5', 'ultra', '思考强度'), ('claude', 'claude-opus-5-5', '', '思考强度'),
    ('codex', 'claude-opus-5-5', 'high', '模型'),
])
def test_invalid_engine_model_effort_combinations_are_rejected_before_any_worker(engine, model, effort, part):
    with pytest.raises(editor.VideoEditError, match=part) as failure:
        editor.start('missing.mp4', engine=engine, model=model, reasoning_effort=effort)
    assert failure.value.status == 400


@pytest.mark.parametrize('model', ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1', 'claude-haiku-5-5'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_every_claude_model_supports_every_cli_effort(model, effort):
    assert editor._model_settings(model, effort, 'claude') == (model, effort)
    assert editor._model_settings(None, None, 'claude') == ('claude-opus-5-5', 'high')


def test_capabilities_report_each_engine_and_prefer_codex(claude_editor, monkeypatch, tmp_path):
    codex = tmp_path / 'fake codex'
    codex.write_text('#!/bin/sh\n')
    codex.chmod(0o755)
    monkeypatch.setenv('CODEX_VIDEO_EDITOR_BIN', str(codex))
    both = editor.capabilities()
    assert both['available'] is True and both['default_engine'] == 'codex'
    assert both['engines']['codex']['available'] and both['engines']['claude']['available']
    assert '本机 Claude Code' in both['engines']['claude']['message']
    monkeypatch.setenv('CODEX_VIDEO_EDITOR_BIN', str(tmp_path / 'missing'))
    only_claude = editor.capabilities()
    assert only_claude['available'] is True and only_claude['default_engine'] == 'claude'
    assert '缺少：codex' in only_claude['engines']['codex']['message']
    monkeypatch.setattr(editor, '_claude_binary', lambda: None)
    neither = editor.capabilities()
    assert neither['available'] is False and neither['default_engine'] == 'codex'
    assert '缺少：claude' in neither['engines']['claude']['message']


def test_missing_claude_blocks_only_the_claude_engine(claude_editor, monkeypatch):
    _, source, _ = claude_editor
    monkeypatch.setattr(editor, '_claude_binary', lambda: None)
    with pytest.raises(editor.VideoEditError) as unavailable:
        editor.start(str(source), engine='claude')
    assert (unavailable.value.status, unavailable.value.code) == (503, 'EDITOR_UNAVAILABLE')
    assert not list(editor._job_dirs())


def test_bundled_claude_code_is_found_by_newest_version(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    for version in ('2.1.9', '2.1.293', '2.1.100'):
        binary = home / 'Library/Application Support/Claude/claude-code' / version / 'abcdef012345' / 'claude.app/Contents/MacOS/claude'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.delenv('CLAUDE_VIDEO_EDITOR_BIN', raising=False)
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    assert '/2.1.293/' in editor._claude_binary()
    monkeypatch.setenv('CLAUDE_VIDEO_EDITOR_BIN', '/custom/claude')
    assert editor._claude_binary() == '/custom/claude'


def test_latest_settings_follow_a_claude_job_only_while_it_can_run(claude_editor, monkeypatch):
    _, source, _ = claude_editor
    editor.start(str(source), engine='claude', model='claude-haiku-5-5', reasoning_effort='low', mode='trim_speed')
    wait_job(source, 'completed')
    assert editor.latest_settings() == {'mode': 'trim_speed', 'model': 'claude-haiku-5-5',
                                        'reasoning_effort': 'low', 'engine': 'claude'}
    monkeypatch.setattr(editor, '_claude_binary', lambda: None)
    assert editor.latest_settings() == {'mode': 'trim_speed', 'model': editor.DEFAULT_MODEL,
                                        'reasoning_effort': editor.DEFAULT_REASONING_EFFORT}


def test_command_progress_maps_skill_scripts_to_stages(claude_editor):
    _, source, _ = claude_editor
    job = editor.start(str(source), notes='[fixture:cancel]', engine='claude')
    directory = next(editor._job_dirs())
    editor._command_progress(directory, [('inspect_video.py', ['--video', '/x/input.mp4'])], 'started')
    assert editor._read_json(directory / '.state.json')['stage'] == 'reviewing_source'
    editor._command_progress(directory, [('inspect_video.py', ['--video', '/x/edited.mp4'])], 'started')
    assert editor._read_json(directory / '.state.json')['stage'] == 'reviewing_output'
    editor._command_progress(directory, [('render_edit.py', [])], 'started')
    assert editor._read_json(directory / '.state.json')['stage'] == 'rendering'
    editor._command_progress(directory, [('render_edit.py', [])], 'completed', ok=False)
    assert editor._read_json(directory / '.state.json')['stage'] == 'rendering'
    editor._command_progress(directory, [('render_edit.py', [])], 'completed')
    assert editor._read_json(directory / '.state.json')['stage'] == 'reviewing_output'
    editor.cancel(job['id'])
    wait_job(source, 'cancelled')
