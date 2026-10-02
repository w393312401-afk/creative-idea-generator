"""Exercise the installed skill's real mechanical output contract, without an AI call."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import codex_video_editor as editor


@pytest.mark.parametrize('mode,speed', [('trim', 1), ('trim_speed', 1.25)])
def test_installed_skill_artifacts_are_accepted(tmp_path, monkeypatch, mode, speed):
    scripts = editor.SKILL_DIR / 'scripts'
    if not all(shutil.which(name) for name in ('ffmpeg', 'ffprobe')) or not (scripts / 'render_edit.py').is_file():
        pytest.skip('Installed timelapse skill and media tools are required')
    pytest.importorskip('PIL')
    outputs = tmp_path / 'outputs'
    job = outputs / 'skill-contract' / 'codex_edits' / ('a' * 32)
    workspace = job / 'work'
    workspace.mkdir(parents=True)
    source = job / 'input.mp4'
    monkeypatch.setattr(editor, 'OUTPUTS_DIR', outputs)
    monkeypatch.setattr(editor, '_STOP_REQUESTED', False)

    def run(args):
        subprocess.run(args, check=True, capture_output=True, timeout=45)

    run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'testsrc2=size=64x64:rate=30:duration=1', '-an', '-c:v', 'libx264',
         '-pix_fmt', 'yuv420p', str(source)])
    plan = workspace / 'edit-plan.json'
    plan.write_text(json.dumps({'source': str(source), 'output_fps': 30, 'segments': [
        {'start': 0, 'end': 0.5, 'speed': speed, 'reason': 'Fixture construction'},
        {'start': 0.5, 'end': 1, 'speed': 1, 'reason': 'Fixture reveal'},
    ]}))
    output = workspace / 'edited.mp4'
    run([sys.executable, str(scripts / 'inspect_video.py'), '--video', str(source),
         '--output-dir', str(workspace / 'evidence'), '--sample-fps', '2'])
    run([sys.executable, str(scripts / 'render_edit.py'), '--plan', str(plan),
         '--output', str(output), '--work-dir', str(workspace / 'render')])
    run([sys.executable, str(scripts / 'inspect_video.py'), '--video', str(output),
         '--output-dir', str(workspace / 'qa'), '--sample-fps', '2'])
    review = workspace / 'review.md'
    review.write_text('Mechanical contract fixture: visual review is simulated for this test only.')
    final = {'output_file': str(output), 'review_file': str(review), 'plan_file': str(plan),
             'qa_evidence_file': str(workspace / 'qa' / 'evidence.json'),
             'source_reviewed': True, 'qa_reviewed': True,
             'visual_reviewed_ranges': [{'start': 0, 'end': 1}], 'audio_reviewed': 'no_audio'}
    result = editor._validate_result(job, {'mode': mode, 'tools': editor._tools()}, final)
    assert Path(result['file']).is_file()
    assert result['duration_seconds'] == (1 if speed == 1 else 0.9)
    assert result['url'].endswith('/work/edited.mp4')
