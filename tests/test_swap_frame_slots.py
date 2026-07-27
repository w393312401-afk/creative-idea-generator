"""帧槽位换位接口 (/api/swap_frame_slots) 的行为契约。

前端把一张帧卡片拖到另一张帧卡片上时调用，与 /api/swap_video_slots 同构：帧号与网格
位置固定不动，动的只是"哪张图落在哪一格"，落盘文件 img_NNN.webp 必须真的互换/改名
（视频配对、合成、前端都按帧号取文件）。

比视频换位多出来的一件事是 i2i 血统：帧序列是单链（每帧以上一帧为参考），任何一格
的图被换掉，较小那个帧号之后的帧就都还派生自旧链——统一标 stale_lineage；被拖动的
两格自己不标，那是人有意放在那里的画面。
"""
import io
import json
import os
from email.message import Message

import pytest
from PIL import Image

import server


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def project(tmp_path):
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    colors = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255), 4: (255, 255, 0)}
    for seq, color in colors.items():
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(frames_dir, f'img_{seq:03d}.webp'), format='WEBP')

    manifest = {
        'title': 'swap_frame_test',
        'aspect_ratio': '9:16',
        'frames': [
            {'slot': s, 'sequence': s, 'file': f'frames/img_{s:03d}.webp',
             'url': f'/frames/img_{s:03d}.webp', 'prompt': f'prompt {s}',
             'model': 'gemini', 'quality_gate': 'auto_approved', 'parent_hash': f'hash{s}'}
            for s in (1, 2, 3, 4)
        ],
        'videos': [
            {'slot': 1, 'sequence': 1, 'file': 'videos/vid_001.mp4', 'status': 'success'},
            {'slot': 2, 'sequence': 2, 'file': 'videos/vid_002.mp4', 'status': 'success'},
            {'slot': 3, 'sequence': 3, 'file': 'videos/vid_003.mp4', 'status': 'success'},
        ],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'frames_dir': frames_dir, 'colors': colors}


def _swap_handler(payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/swap_frame_slots'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _pixel(project, seq):
    with Image.open(os.path.join(project['frames_dir'], f'img_{seq:03d}.webp')) as img:
        return img.convert('RGB').getpixel((10, 10))


def _close(actual, expected, tol=12):
    return all(abs(a - b) <= tol for a, b in zip(actual, expected))


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


class TestSwapFrames:
    def test_two_filled_slots_swap_files_and_entries(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['moved'] is False

        # 图真的换了位（img_002 现在是原来第 3 帧的蓝色）
        assert _close(_pixel(project, 2), project['colors'][3])
        assert _close(_pixel(project, 3), project['colors'][2])

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert frames[2]['prompt'] == 'prompt 3'
        assert frames[3]['prompt'] == 'prompt 2'
        # 槽位属性留在原位
        assert frames[2]['file'].endswith('frames/img_002.webp')
        assert frames[3]['url'].endswith('/frames/img_003.webp')
        # 血统断开并留痕
        assert frames[2]['parent_hash'] == ''
        assert frames[2]['swapped_from_sequence'] == 3
        assert frames[3]['swapped_from_sequence'] == 2

    def test_downstream_frames_marked_stale_but_not_the_moved_ones(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert not frames[1].get('stale_lineage'), "换位点之前的帧不受影响"
        assert not frames[2].get('stale_lineage'), "被拖动的两格是人有意放的画面，不算残留"
        assert not frames[3].get('stale_lineage')
        assert frames[4].get('stale_lineage') is True

    def test_affected_video_slots_and_merged_video(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        # IMG 002 涉及 VID 001/002，IMG 003 涉及 VID 002/003
        assert body['affected_video_slots'] == [1, 2, 3]
        assert 'merged_video' not in _manifest(project)

    def test_empty_target_becomes_a_move(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 4, 'to_sequence': 5})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['moved'] is True

        assert not os.path.exists(os.path.join(project['frames_dir'], 'img_004.webp'))
        assert _close(_pixel(project, 5), project['colors'][4])
        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert 4 not in frames
        assert frames[5]['prompt'] == 'prompt 4'


class TestCopyFrames:
    def test_copy_leaves_source_intact(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 1,
                                 'to_sequence': 3, 'mode': 'copy'})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['mode'] == 'copy'

        assert _close(_pixel(project, 1), project['colors'][1])
        assert _close(_pixel(project, 3), project['colors'][1])
        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert frames[1]['parent_hash'] == 'hash1', "源格没有被动过"
        assert frames[3]['swapped_from_sequence'] == 1
        assert frames[4].get('stale_lineage') is True


class TestValidation:
    def test_missing_source_image_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        os.remove(os.path.join(project['frames_dir'], 'img_002.webp'))
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][1] == 404, sent[0][0]
        assert _close(_pixel(project, 3), project['colors'][3]), "被拒绝的请求不应动文件"
        assert 'merged_video' in _manifest(project)

    def test_same_slot_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_bad_mode_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 1,
                                 'to_sequence': 2, 'mode': 'burn'})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_unknown_project_returns_404(self, project, monkeypatch, tmp_path):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / 'nope'))
        h, sent = _swap_handler({'title': 'nope', 'from_sequence': 1, 'to_sequence': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 404

    def test_rejected_while_a_frame_run_is_active(self, project, monkeypatch):
        """渲染/修复 worker 会整体回写自己的 manifest 快照，这时候换位会被无声覆盖
        （图已经在盘上换了，清单里却还是旧的）——同 /api/upload_frame，直接 409。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        monkeypatch.setitem(server.ACTIVE_TASKS, 'frames_running',
                            {'status': 'running', 'events': [], 'type': 'frames'})
        assert server.claim_frame_run(project['dir'], 'frames_running') is None
        try:
            h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 1, 'to_sequence': 2})
            server.SparkRequestHandler.do_POST(h)
            assert sent[0][1] == 409
            assert _close(_pixel(project, 1), project['colors'][1]), "被拒绝的请求不应动文件"
        finally:
            server.release_frame_run(project['dir'], 'frames_running')
