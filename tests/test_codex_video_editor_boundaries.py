"""Path, disclosure and early evidence checks for local Codex editing.

These tests never start Codex, FFmpeg, ffprobe, or a background worker. Video bytes
are deliberately fake: every result case must be rejected before media probing.
"""
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote, unquote

import pytest

import codex_video_editor as editor


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    root = tmp_path / 'outputs'
    root.mkdir()
    monkeypatch.setattr(editor, 'OUTPUTS_DIR', root)
    monkeypatch.setattr(editor, '_probe', lambda *_args, **_kwargs: pytest.fail('early rejection reached ffprobe'))
    monkeypatch.setattr(editor, '_run_checked', lambda *_args, **_kwargs: pytest.fail('boundary test started a media process'))
    return root


def write(path, contents=b'fake-video'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


@pytest.mark.parametrize('representation', ['relative', 'absolute', 'url', 'encoded_url'])
def test_source_resolves_chinese_project_video_in_supported_forms(outputs, representation):
    video = write(outputs / '中文项目' / '最终 成片.MP4')
    relative = 'outputs/中文项目/最终 成片.MP4'
    values = {'relative': relative, 'absolute': str(video), 'url': '/' + relative,
              'encoded_url': quote('/' + relative, safe='/')}
    path, public_url, project = editor._source(values[representation])
    assert path == video.resolve()
    assert unquote(public_url) == '/' + relative
    assert project == video.parent


@pytest.mark.parametrize('kind', ['file', 'parent', 'inside_root_target'])
def test_source_rejects_symlinks_even_if_the_target_is_a_video(outputs, tmp_path, kind):
    target = write((outputs / 'target' if kind == 'inside_root_target' else tmp_path / 'outside') / 'clip.mp4')
    project = outputs / 'project'
    project.mkdir()
    if kind == 'parent':
        (project / 'linked').symlink_to(target.parent, target_is_directory=True)
        candidate = project / 'linked' / target.name
    else:
        candidate = project / 'clip.mp4'
        candidate.symlink_to(target)
    with pytest.raises(editor.VideoEditError, match='符号链接'):
        editor._source(str(candidate))


@pytest.mark.parametrize('relative', [
    'project/image.webp', 'project/audio.mp3', 'project/report.json',
    'project/.state.json', 'project/.private.mp4', '.private/clip.mp4',
    'project/.internal/clip.mp4', 'loose.mp4',
])
def test_source_rejects_non_video_and_private_or_unowned_files(outputs, relative):
    candidate = write(outputs / relative)
    with pytest.raises(editor.VideoEditError):
        editor._source(str(candidate))


def test_source_rejects_empty_video_and_directory_named_mp4(outputs):
    empty = write(outputs / 'project/empty.mp4', b'')
    folder = outputs / 'project/directory.mp4'
    folder.mkdir()
    for candidate in (empty, folder):
        with pytest.raises(editor.VideoEditError, match='MP4'):
            editor._source(str(candidate))


@pytest.mark.parametrize('raw', [
    '/outputs/../outside/clip.mp4',
    'outputs/%2e%2e/outside/clip.mp4',
    '/outputs/project/../../outside/clip.mp4',
    '/outputs/project/%2e%2e/%2e%2e/outside/clip.mp4',
])
def test_source_rejects_parent_traversal_outside_outputs(outputs, tmp_path, raw):
    write(tmp_path / 'outside/clip.mp4')
    (outputs / 'project').mkdir()
    with pytest.raises(editor.VideoEditError):
        editor._source(raw)


@pytest.mark.parametrize('raw', [None, 123, '', '  ', 'project/clip.mp4',
                                  '/outputs/project/clip.mp4\x00', 'https://example.test/outputs/project/clip.mp4'])
def test_source_rejects_invalid_or_remote_inputs(outputs, raw):
    write(outputs / 'project/clip.mp4')
    with pytest.raises(editor.VideoEditError):
        editor._source(raw)


def test_public_job_exposes_request_identity_without_private_worker_fields(outputs):
    video = write(outputs / 'project/clip.mp4')
    job = {'id': 'a' * 32, 'source': '/outputs/project/clip.mp4', 'status': 'completed',
           '_request_id': 'browser-attempt-1', '_source_identity': editor._identity(video),
           '_worker_token': 'private-token', '_worker_pid': 12345, '_fingerprint': 'private-hash'}
    public = editor._public(job)
    assert public['request_id'] == 'browser-attempt-1'
    assert public['model'] is None and public['reasoning_effort'] is None  # Legacy state has no selection.
    assert public['source_changed'] is False
    assert not any(key.startswith('_') for key in public)
    assert all(value not in public.values() for value in ('private-token', 'private-hash'))
    original = video.stat()
    os.utime(video, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
    assert editor._public(job)['source_changed'] is True
    assert job['_request_id'] == 'browser-attempt-1'


def test_public_job_marks_missing_source_as_changed_and_keeps_result(outputs):
    output = {'url': '/outputs/project/edited.mp4'}
    public = editor._public({'source': '/outputs/project/missing.mp4', 'output': output, '_request_id': 'retry-key'})
    assert public['source_changed'] is True
    assert public['output'] is output
    assert public['request_id'] == 'retry-key'


def test_public_job_detects_deleted_result_without_rewriting_history(outputs):
    write(outputs / 'project/clip.mp4')
    result = write(outputs / 'project/codex_edits/job/work/edited.mp4')
    job = {'source': '/outputs/project/clip.mp4', 'status': 'completed',
           'output': {'url': '/outputs/project/codex_edits/job/work/edited.mp4'},
           'message': '精剪完成'}
    assert editor._public(job)['output_missing'] is False
    result.unlink()
    public = editor._public(job)
    assert public['output_missing'] is True
    assert '重新精剪' in public['message']
    assert job['message'] == '精剪完成'
    assert public['status'] == 'completed'


@pytest.fixture
def installed_dependencies(tmp_path, monkeypatch):
    executable = write(tmp_path / 'fake-tool', b'not executed')
    executable.chmod(0o700)
    skill = tmp_path / 'skill'
    write(skill / 'SKILL.md', b'skill')
    write(skill / 'scripts/inspect_video.py', b'inspect')
    write(skill / 'scripts/render_edit.py', b'render')
    tools = {name: str(executable) for name in ('codex', 'ffmpeg', 'ffprobe')}
    tools.update(skill=str(skill), python=sys.executable)
    monkeypatch.setattr(editor, '_tools', lambda: tools)
    monkeypatch.setattr(editor, 'SKILL_DIR', skill)
    monkeypatch.setattr(editor, 'fcntl', object())
    monkeypatch.setitem(sys.modules, 'PIL', object())
    return tools, skill


def test_capability_detects_installed_dependencies_without_launching_them(installed_dependencies):
    result = editor.capabilities()
    assert result['available'] is True
    assert '执行时检查登录' in result['message']


@pytest.mark.parametrize('missing', ['codex', 'ffmpeg', 'ffprobe', 'Pillow', 'fcntl', 'skill', 'inspect', 'render'])
def test_capability_reports_each_missing_dependency(installed_dependencies, monkeypatch, missing):
    tools, skill = installed_dependencies
    if missing in ('codex', 'ffmpeg', 'ffprobe'):
        tools[missing] = None
    elif missing == 'Pillow':
        monkeypatch.setitem(sys.modules, 'PIL', None)
    elif missing == 'fcntl':
        monkeypatch.setattr(editor, 'fcntl', None)
    else:
        file = {'skill': 'SKILL.md', 'inspect': 'scripts/inspect_video.py', 'render': 'scripts/render_edit.py'}[missing]
        (skill / file).unlink()
    result = editor.capabilities()
    assert result['available'] is False
    label = {'fcntl': 'POSIX', 'skill': 'timelapse-video-editor', 'inspect': 'timelapse-video-editor',
             'render': 'timelapse-video-editor'}.get(missing, missing)
    assert label in result['message']


def test_capability_rejects_non_executable_tool(installed_dependencies):
    tools, _ = installed_dependencies
    Path(tools['codex']).chmod(0o600)
    result = editor.capabilities()
    assert result['available'] is False
    assert 'codex' in result['message']


@pytest.fixture
def result_bundle(outputs):
    directory = outputs / 'project/codex_edits' / ('b' * 32)
    source = write(directory / 'input.mp4')
    workspace = directory / 'work'
    output = write(workspace / 'edited.mp4', b'fake-edited-video')
    plan = workspace / 'edit-plan.json'
    review = write(workspace / 'review.md', b'visual review notes')
    qa = workspace / 'qa/evidence.json'
    evidence = workspace / 'evidence/evidence.json'
    report = output.with_suffix('.report.json')
    documents = {
        'plan': {'source': str(source), 'segments': [{'start': 0, 'end': 5, 'speed': 1}]},
        'qa': {'source': str(output), 'frames': [{'file': 'frame.jpg'}], 'sheets': [{'file': 'sheet.jpg'}]},
        'evidence': {'source': str(source), 'frames': [{'file': 'frame.jpg'}], 'sheets': [{'file': 'sheet.jpg'}]},
        'report': {'source': str(source), 'output': str(output), 'plan': str(plan), 'status': 'complete',
                   'validation': {key: True for key in ('frame_count_matches', 'video_duration_matches',
                       'audio_presence_and_duration_match', 'full_decode_passed')}},
    }
    files = {'output': output, 'plan': plan, 'review': review, 'qa': qa, 'evidence': evidence, 'report': report}
    for key, document in documents.items():
        write(files[key], json.dumps(document).encode())
    final = {'status': 'completed', 'output_file': str(output), 'plan_file': str(plan), 'review_file': str(review),
             'qa_evidence_file': str(qa), 'source_reviewed': True, 'qa_reviewed': True,
             'visual_reviewed_ranges': [{'start': 0, 'end': 5}], 'audio_reviewed': 'no_audio'}
    request = {'mode': 'trim', 'tools': {'ffmpeg': 'must-not-run', 'ffprobe': 'must-not-run'}}
    return directory, request, final, files, documents


def rewrite(bundle, key, value):
    bundle[3][key].write_text(json.dumps(value), encoding='utf-8')


@pytest.mark.parametrize('missing', ['output', 'plan', 'review', 'qa', 'report', 'evidence'])
def test_result_requires_all_report_and_evidence_files_before_probing(result_bundle, missing):
    directory, request, final, files, _ = result_bundle
    files[missing].unlink()
    with pytest.raises(RuntimeError):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('flag', ['source_reviewed', 'qa_reviewed'])
def test_result_rejects_missing_visual_review_confirmation(result_bundle, flag):
    directory, request, final, _, _ = result_bundle
    final[flag] = False
    with pytest.raises(RuntimeError, match='视觉审阅'):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('document,field', [('plan', 'source'), ('report', 'source'), ('report', 'output'),
                                            ('report', 'plan'), ('evidence', 'source'), ('qa', 'source')])
def test_result_rejects_report_or_evidence_for_another_source(result_bundle, document, field):
    directory, request, final, _, documents = result_bundle
    documents[document][field] = str(directory / 'different-video.mp4')
    rewrite(result_bundle, document, documents[document])
    with pytest.raises(RuntimeError, match='不匹配|证据'):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('check', ['frame_count_matches', 'video_duration_matches',
                                   'audio_presence_and_duration_match', 'full_decode_passed'])
def test_result_rejects_unpassed_render_validation(result_bundle, check):
    directory, request, final, _, documents = result_bundle
    documents['report']['validation'][check] = False
    rewrite(result_bundle, 'report', documents['report'])
    with pytest.raises(RuntimeError, match='技术验证'):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('document,field', [('evidence', 'frames'), ('evidence', 'sheets'), ('qa', 'frames'), ('qa', 'sheets')])
def test_result_rejects_absent_visual_evidence_pages(result_bundle, document, field):
    directory, request, final, _, documents = result_bundle
    documents[document][field] = []
    rewrite(result_bundle, document, documents[document])
    with pytest.raises(RuntimeError, match='证据'):
        editor._validate_result(directory, request, final)


def test_result_rejects_input_hardlink_disguised_as_new_output(result_bundle):
    directory, request, final, files, _ = result_bundle
    files['output'].unlink()
    os.link(directory / 'input.mp4', files['output'])
    with pytest.raises(RuntimeError, match='输入当作结果'):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('kind', ['outside', 'symlink'])
def test_result_rejects_artifact_escape_before_media_probe(result_bundle, tmp_path, kind):
    directory, request, final, files, _ = result_bundle
    outside = write(tmp_path / 'outside.mp4')
    if kind == 'outside':
        final['output_file'] = str(outside)
    else:
        files['output'].unlink()
        files['output'].symlink_to(outside)
    with pytest.raises(RuntimeError, match='任务目录'):
        editor._validate_result(directory, request, final)


@pytest.mark.parametrize('representation', ['relative', 'absolute', 'encoded_url'])
def test_reserved_filename_characters_roundtrip_through_public_url(outputs, representation):
    relative = '项目/精剪#100%20?.mp4'
    video = write(outputs / relative)
    expected_url = '/outputs/' + quote(relative, safe='/')
    value = {'relative': 'outputs/' + relative, 'absolute': str(video), 'encoded_url': expected_url}[representation]
    path, public_url, _ = editor._source(value)
    assert path == video
    assert public_url == expected_url
    assert editor._source(public_url)[0] == video


@pytest.mark.parametrize('suffix', ['?v=1', '#preview'])
def test_source_url_rejects_actual_query_or_fragment(outputs, suffix):
    write(outputs / 'project/clip.mp4')
    with pytest.raises(editor.VideoEditError):
        editor._source('/outputs/project/clip.mp4' + suffix)


def test_source_rejects_encoded_null_with_a_domain_error(outputs):
    write(outputs / 'project/clip.mp4')
    with pytest.raises(editor.VideoEditError):
        editor._source('/outputs/project/clip%00.mp4')
