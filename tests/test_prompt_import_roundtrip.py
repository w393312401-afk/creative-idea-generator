"""Uploaded prompts must keep the same slot/body mapping after server parsing."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from prompt_pipeline import _parse_prompt_slots, prompt_slots_list


ROOT = Path(__file__).resolve().parents[1]


def normalize_upload(text):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the upload-to-server regression')
    script = """
const fs = require('fs');
const vm = require('vm');
const ctx = { document: { getElementById: () => null } };
vm.createContext(ctx);
vm.runInContext('const SLOT_META_TAG_RE = /^(?:HERO|BRIDGE(?:\\\\s+TURN)?|CUT)$/i;', ctx);
vm.runInContext(fs.readFileSync('js/prompt_import.js', 'utf8'), ctx);
process.stdout.write(JSON.stringify(ctx.normalizePromptSetText(fs.readFileSync(0, 'utf8'))));
"""
    result = subprocess.run([node, '-e', script], input=text, text=True,
                            capture_output=True, check=True, cwd=ROOT)
    normalized = json.loads(result.stdout)
    assert normalized['ok'], normalized.get('error')
    return normalized


def test_repeated_headings_keep_all_85_images_and_84_video_bodies():
    lines = []
    for index in range(1, 86):
        lines.extend([f'### IMAGE_{index:03d}', '```text',
                      f'IMAGE {index}:', f'Frame scene number {index}.', '```'])
    for index in range(1, 85):
        lines.extend([f'### VIDEO_{index:03d}', '```text',
                      f'VIDEO {index}: Bind IMAGE {index} to IMAGE {index + 1}.',
                      f'IMAGE {index} as the exact first-frame image remains unchanged.',
                      'Complete the visible work without a camera jump.', '```'])
    report = normalize_upload('\n'.join(lines))
    images, videos = _parse_prompt_slots(report['text'])
    assert list(sorted(images)) == list(range(1, 86))
    assert list(sorted(videos)) == list(range(1, 85))
    for index in range(1, 85):
        assert videos[index]['body'] == (
            f'Bind IMAGE {index} to IMAGE {index + 1}.\n'
            f'IMAGE {index} as the exact first-frame image remains unchanged.\n'
            'Complete the visible work without a camera jump.')
    assert '在此填写' not in report['text']
    slots = prompt_slots_list(report['text'])
    assert slots['videos'][40]['index'] == 41
    assert slots['videos'][40]['body'] == videos[41]['body']


def test_missing_video_keeps_later_video_bound_to_its_original_image():
    report = normalize_upload('\n'.join([
        'IMAGE 1:', 'First state.', 'IMAGE 2:', 'Second state.',
        'IMAGE 3:', 'Third state.', 'IMAGE 4:', 'Fourth state.',
        'VIDEO 1:', 'First transition.', 'VIDEO 3:', 'Third transition.',
    ]))
    assert report['missingVideos'] == [2]
    slots = prompt_slots_list(report['text'])
    assert [item['index'] for item in slots['videos']] == [1, 3]
    assert slots['videos'][1]['body'] == 'Third transition.'


def test_backend_does_not_truncate_numbered_body_references():
    body = ('The builder enters.\nIMAGE 1 as the first frame is fixed.\n'
            'VIDEO 1 remains six seconds long.\nFinish with the room empty.')
    images, videos = _parse_prompt_slots(
        f'图片 1:\nA room.\n图片 2:\nA clean room.\n视频 1:\n{body}\n')
    assert len(images) == 2
    assert videos[1]['body'] == body


def test_range_heading_with_duration_parentheses_survives_server_parsing():
    report = normalize_upload('\n'.join([
        '### IMAGE_001 — Beginning (wide)', 'First state.',
        '### IMAGE_002 — End [wide]', 'Second state.',
        '### VIDEO_001 — IMAGE 1 → IMAGE 2 (6s)', 'Actual motion prompt.',
    ]))
    slots = prompt_slots_list(report['text'])
    assert [item['index'] for item in slots['images']] == [1, 2]
    assert slots['videos'] == [{'index': 1, 'body': 'Actual motion prompt.', 'meta': ''}]
