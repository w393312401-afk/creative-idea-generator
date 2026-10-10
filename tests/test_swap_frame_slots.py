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
from frame_generator import _fx_find_ref_for


_FX_UUIDS = {
    1: '11111111-1111-1111-1111-111111111111',
    2: '22222222-2222-2222-2222-222222222222',
    3: '33333333-3333-3333-3333-333333333333',
    4: '44444444-4444-4444-4444-444444444444',
}


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def project(tmp_path):
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)
    fx_src_dir = os.path.join(frames_dir, 'fx_src')
    os.makedirs(fx_src_dir, exist_ok=True)

    colors = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255), 4: (255, 255, 0)}
    for seq, color in colors.items():
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(frames_dir, f'img_{seq:03d}.webp'), format='WEBP')
        # FX 路径给每帧留的原始 jpg：下一帧就是靠文件名里的 UUID 挂参考的
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(fx_src_dir, f'img_{seq:03d}_{_FX_UUIDS[seq]}.jpg'), format='JPEG')
    Image.new('RGB', (90, 160), color=colors[2]).save(
        os.path.join(fx_src_dir, 'chain_ref_002.jpg'), format='JPEG')

    manifest = {
        'title': 'swap_frame_test',
        'aspect_ratio': '9:16',
        'frames': [
            {'slot': s, 'sequence': s, 'file': f'frames/img_{s:03d}.webp',
             'url': f'/frames/img_{s:03d}.webp', 'prompt': f'prompt {s}',
             'model': 'gemini', 'quality_gate': 'auto_approved', 'parent_hash': f'hash{s}',
             'backend': 'google_fx', 'fx_uuid': _FX_UUIDS[s],
             'fx_src': f'frames/fx_src/img_{s:03d}_{_FX_UUIDS[s]}.jpg'}
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

    return {'dir': project_dir, 'frames_dir': frames_dir, 'fx_src_dir': fx_src_dir,
            'colors': colors}


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

    def test_fx_uuid_sidecars_follow_the_images(self, project, monkeypatch):
        """换位只搬 webp、留档留在原位的话，第 3 帧的下一帧仍会按 img_003_<老UUID>
        去画布上挂换位前的那张老图——整条链继续照着老画面续图。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        names = os.listdir(project['fx_src_dir'])
        assert f'img_002_{_FX_UUIDS[3]}.jpg' in names
        assert f'img_003_{_FX_UUIDS[2]}.jpg' in names
        assert f'img_002_{_FX_UUIDS[2]}.jpg' not in names
        assert f'img_003_{_FX_UUIDS[3]}.jpg' not in names
        assert not any(n.endswith('.relocate.tmp') for n in names)
        # 换了画面的那格，本地转档缓存也作废
        assert 'chain_ref_002.jpg' not in names
        # 第 4 帧续链时挂到的是现在真的落在第 3 格的那张图
        assert _fx_find_ref_for(project['frames_dir'], 4).endswith(f'img_003_{_FX_UUIDS[2]}.jpg')

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert frames[2]['fx_uuid'] == _FX_UUIDS[3]
        assert frames[2]['fx_src'].endswith(f'img_002_{_FX_UUIDS[3]}.jpg')
        assert frames[3]['fx_uuid'] == _FX_UUIDS[2]

    def test_move_leaves_no_sidecar_behind(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 4, 'to_sequence': 5})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        names = os.listdir(project['fx_src_dir'])
        assert f'img_005_{_FX_UUIDS[4]}.jpg' in names
        assert not any(n.startswith('img_004_') for n in names), '源格图已经走了，留档不能留下'
        assert _fx_find_ref_for(project['frames_dir'], 5) is None

    def test_swap_with_an_uploaded_slot_drops_the_stale_uuid(self, project, monkeypatch):
        """目标格是手动上传的图（没有画布 UUID）：换过来之后源格必须也不带 UUID，
        否则下一帧又会照着被换走的那张老图续链。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        os.remove(os.path.join(project['fx_src_dir'], f'img_003_{_FX_UUIDS[3]}.jpg'))

        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 2, 'to_sequence': 3})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        names = os.listdir(project['fx_src_dir'])
        assert f'img_003_{_FX_UUIDS[2]}.jpg' in names
        assert not any(n.startswith('img_002_') for n in names)
        assert _fx_find_ref_for(project['frames_dir'], 3) is None

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert 'fx_uuid' not in frames[2] and 'fx_src' not in frames[2]

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

    def test_copy_duplicates_the_source_sidecar(self, project, monkeypatch):
        """两格现在是同一张图，UUID 留档也该是同一个；目标格的旧留档必须消失，
        否则第 4 帧还会挂到被覆盖掉的那张老图。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_frame_test', 'from_sequence': 1,
                                 'to_sequence': 3, 'mode': 'copy'})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        names = os.listdir(project['fx_src_dir'])
        assert f'img_001_{_FX_UUIDS[1]}.jpg' in names, '源格原样保留'
        assert f'img_003_{_FX_UUIDS[1]}.jpg' in names
        assert f'img_003_{_FX_UUIDS[3]}.jpg' not in names

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert frames[1]['fx_uuid'] == _FX_UUIDS[1]
        assert frames[3]['fx_uuid'] == _FX_UUIDS[1]


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
