"""合成完整成片后自动精剪：沿用最近一次精剪设置、同一次合成只排一次、缺段/关闭/失败都不影响合成。"""
import json

import pytest

import codex_video_editor as editor
import video_generator


def _job(root, project, job_id, created_at, **fields):
    directory = root / project / 'codex_edits' / job_id
    directory.mkdir(parents=True)
    (directory / '.state.json').write_text(json.dumps({'id': job_id, 'created_at': created_at, **fields}))


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    root = tmp_path / 'outputs'
    (root / 'p').mkdir(parents=True)
    monkeypatch.setattr(editor, 'OUTPUTS_DIR', root)
    return root


def test_latest_settings_follow_newest_job_across_projects(outputs):
    _job(outputs, 'p', 'a' * 32, '2026-10-07T10:00:00+00:00', mode='trim', model='gpt-6-luna', reasoning_effort='low')
    _job(outputs, 'q', 'b' * 32, '2026-10-08T10:00:00+00:00', mode='trim_speed', model='gpt-6-astra',
         reasoning_effort='max')
    assert editor.latest_settings() == {'mode': 'trim_speed', 'model': 'gpt-6-astra', 'reasoning_effort': 'max'}


def test_latest_settings_default_without_usable_record(outputs):
    assert editor.latest_settings() == {'mode': 'trim', 'model': editor.DEFAULT_MODEL,
                                        'reasoning_effort': editor.DEFAULT_REASONING_EFFORT}
    _job(outputs, 'p', 'c' * 32, '2026-10-08T10:00:00+00:00', mode='bogus', model='retired-model',
         reasoning_effort='ultra')
    assert editor.latest_settings() == {'mode': 'trim', 'model': editor.DEFAULT_MODEL,
                                        'reasoning_effort': editor.DEFAULT_REASONING_EFFORT}


def test_start_after_merge_is_idempotent_per_merged_file(outputs, monkeypatch):
    video = outputs / 'p' / '成片_2x.mp4'
    video.write_bytes(b'first merge')
    calls = []
    monkeypatch.setattr(editor, 'start', lambda source, **kw: calls.append((source, kw)) or {'id': 'j'})
    merged = {'status': 'success', 'file': 'outputs/p/成片_2x.mp4', 'url': '/outputs/p/%E6%88%90%E7%89%87_2x.mp4'}
    editor.start_after_merge(merged)
    editor.start_after_merge(dict(merged))
    assert calls[0][0] == calls[1][0] == '/outputs/p/%E6%88%90%E7%89%87_2x.mp4'
    assert calls[0][1]['request_id'] == calls[1][1]['request_id'], 'same merge must not queue twice'
    assert calls[0][1]['mode'] == 'trim'
    video.write_bytes(b'a re-merge rewrote the same file')
    editor.start_after_merge(merged)
    assert calls[2][1]['request_id'] != calls[0][1]['request_id'], 're-merge must queue a fresh edit'


@pytest.mark.parametrize('merged', [None, {'status': 'failed'}, {'status': 'success', 'partial': True}])
def test_start_after_merge_skips_incomplete_merges(outputs, monkeypatch, merged):
    monkeypatch.setattr(editor, 'start', lambda *a, **k: pytest.fail('must not edit an incomplete merge'))
    assert editor.start_after_merge(merged) is None


def test_merge_hook_starts_edit_and_records_it_on_the_merge(monkeypatch):
    monkeypatch.setattr(editor, 'start_after_merge',
                        lambda merged: {'id': 'job1', 'status': 'queued', 'model': 'm', 'reasoning_effort': 'high'})
    merged = {'status': 'success', 'url': '/outputs/p/x.mp4'}
    job = video_generator._auto_codex_edit_after_merge(merged, {})
    assert job['id'] == 'job1'
    assert merged['auto_codex_edit'] == {'job_id': 'job1', 'status': 'queued'}


def test_merge_hook_respects_switch_and_never_fails_the_merge(monkeypatch):
    monkeypatch.setattr(editor, 'start_after_merge', lambda merged: pytest.fail('switched off'))
    merged = {'status': 'success', 'url': '/outputs/p/x.mp4'}
    assert video_generator._auto_codex_edit_after_merge(merged, {'autoCodexEditAfterMerge': False}) is None

    def busy(merged):
        raise editor.VideoEditError('这条成片已有精剪任务正在处理', 409, 'EDITOR_BUSY')
    monkeypatch.setattr(editor, 'start_after_merge', busy)
    assert video_generator._auto_codex_edit_after_merge(merged, {}) is None
    assert 'auto_codex_edit' not in merged
