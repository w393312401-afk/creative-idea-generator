"""整拍删除接口 (/api/delete_slot) 的行为契约。

删除第 N 拍＝「图片 N」与「视频 N」的提示词、落盘文件、manifest 记录一起消失，其后
所有图片/视频整体前移一位。

为什么是重新编号而不是留空洞：槽位号是契约不是标签——视频 N 恒等于 IMG N → IMG N+1、
视频数恒等于图片数-1，帧网格/配对门禁/合成成片全按这个推算。整体前移让这些不变量继续
成立，代价只有一处：跨过删除点的那一段视频（新 VID N-1）尾锚点换了张图，标出来待重跑。

英雄展示视频（[HERO]，槽位号恒等于最后一张图）是全片的收尾镜头、不属于被删的那一拍，
删完改挂到新的最后一张图上。
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


def _prompt_block(image_count=4, video_count=3, hero_at=None):
    lines = ['图片提示词']
    for i in range(1, image_count + 1):
        lines += [f'图片 {i}:', f'image prompt {i}', '']
    lines.append('视频提示词')
    for i in range(1, video_count + 1):
        tag = ' [HERO]' if hero_at == i else ''
        lines += [f'视频 {i}{tag}:', f'video prompt {i}', '']
    return '\n'.join(lines).strip()


@pytest.fixture
def project(tmp_path):
    """4 张图 + 3 段视频，每个文件内容各不相同，便于断言"哪个文件搬到了哪一格"。"""
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(videos_dir, exist_ok=True)

    colors = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255), 4: (255, 255, 0)}
    for seq, color in colors.items():
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(frames_dir, f'img_{seq:03d}.webp'), format='WEBP')
    for slot in (1, 2, 3):
        with open(os.path.join(videos_dir, f'vid_{slot:03d}.mp4'), 'wb') as f:
            f.write(f'VIDEO-{slot}'.encode('utf-8'))

    manifest = {
        'title': 'delete_slot_test',
        'aspect_ratio': '9:16',
        'frames': [
            {'slot': s, 'sequence': s, 'file': f'frames/img_{s:03d}.webp',
             'url': f'/frames/img_{s:03d}.webp', 'prompt': f'image prompt {s}',
             'quality_gate': 'auto_approved'}
            for s in (1, 2, 3, 4)
        ],
        'videos': [
            {'slot': s, 'sequence': s, 'file': f'videos/vid_{s:03d}.mp4',
             'url': f'/videos/vid_{s:03d}.mp4', 'prompt': f'video prompt {s}',
             'status': 'success', 'anchor_check': f'ok-{s}'}
            for s in (1, 2, 3)
        ],
        'chain_drift': [{'family_anchor': 1, 'tail': 4, 'passed': True}],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'frames_dir': frames_dir, 'videos_dir': videos_dir,
            'colors': colors}


def _delete_handler(payload):
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/delete_slot'
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


def _video_bytes(project, slot):
    with open(os.path.join(project['videos_dir'], f'vid_{slot:03d}.mp4'), 'rb') as f:
        return f.read()


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


class TestDeleteMiddleBeat:
    """删掉中间的第 2 拍：图片 3/4 前移成 2/3，视频 3 前移成 2。"""

    def test_prompt_block_is_renumbered_without_holes(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        images, videos = server._parse_prompt_slots(body['prompt_block'])
        assert sorted(images) == [1, 2, 3], "编号必须连续，不能留空洞"
        assert sorted(videos) == [1, 2]
        assert images[2]['body'] == 'image prompt 3', "原图片 3 前移到了图片 2"
        assert videos[2]['body'] == 'video prompt 3'
        assert 'image prompt 2' not in body['prompt_block']
        assert 'video prompt 2' not in body['prompt_block']
        assert body['image_count'] == 3

    def test_files_are_renamed_on_disk(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        assert _close(_pixel(project, 1), project['colors'][1])
        assert _close(_pixel(project, 2), project['colors'][3]), "原第 3 帧搬到了第 2 格"
        assert _close(_pixel(project, 3), project['colors'][4])
        assert not os.path.exists(os.path.join(project['frames_dir'], 'img_004.webp'))

        assert _video_bytes(project, 1) == b'VIDEO-1'
        assert _video_bytes(project, 2) == b'VIDEO-3'
        assert not os.path.exists(os.path.join(project['videos_dir'], 'vid_003.mp4'))

    def test_manifest_entries_follow_the_files(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        mdata = _manifest(project)
        frames = {f['sequence']: f for f in mdata['frames']}
        assert sorted(frames) == [1, 2, 3]
        assert frames[2]['prompt'] == 'image prompt 3'
        assert frames[2]['file'].endswith('frames/img_002.webp')
        videos = {v['slot']: v for v in mdata['videos']}
        assert sorted(videos) == [1, 2]
        assert videos[2]['prompt'] == 'video prompt 3'
        assert videos[2]['file'].endswith('videos/vid_002.mp4')
        assert [v['sequence'] for v in mdata['videos']] == [1, 2]

    def test_seam_frames_marked_stale_and_seam_video_flagged(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        mdata = _manifest(project)
        frames = {f['sequence']: f for f in mdata['frames']}
        assert not frames[1].get('stale_lineage'), "删除点之前的帧不受影响"
        assert frames[2].get('stale_lineage') is True, "现在这一格是原来的下一张，父帧已被删"
        assert frames[3].get('stale_lineage') is True

        videos = {v['slot']: v for v in mdata['videos']}
        assert videos[1]['anchor_check'] != 'ok-1', "VID 001 的尾锚点换了一张图，旧结论作废"
        assert '删除' in videos[1]['anchor_check']
        assert body['affected_video_slots'] == [1]

    def test_merged_video_and_chain_drift_dropped(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        mdata = _manifest(project)
        assert 'merged_video' not in mdata
        assert 'chain_drift' not in mdata, "链回望结论按帧号记账，整体前移后全部失效"

    def test_fx_source_archives_are_renumbered(self, project, monkeypatch):
        """FX 路径给每帧留档的原始 jpg 按 img_NNN_ 前缀查找参考：留在原位会让重试
        时命中另一帧的旧图。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        fx_dir = os.path.join(project['frames_dir'], 'fx_src')
        os.makedirs(fx_dir, exist_ok=True)
        for seq in (1, 2, 3, 4):
            with open(os.path.join(fx_dir, f'img_{seq:03d}_uuid{seq}.jpg'), 'wb') as f:
                f.write(f'FX-{seq}'.encode('utf-8'))

        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        assert sorted(os.listdir(fx_dir)) == [
            'img_001_uuid1.jpg', 'img_002_uuid3.jpg', 'img_003_uuid4.jpg']


class TestDeleteLastBeat:
    """删掉最后一张图：没有「视频 4」可删，前移后 VID 003 没有尾帧可指——这一段
    悬空视频必须一起消失，否则视频数与图片数-1 对不上。"""

    def test_dangling_tail_video_removed(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 4,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        images, videos = server._parse_prompt_slots(body['prompt_block'])
        assert sorted(images) == [1, 2, 3]
        assert sorted(videos) == [1, 2], "视频数恒等于图片数-1"
        assert not os.path.exists(os.path.join(project['videos_dir'], 'vid_003.mp4'))
        assert not os.path.exists(os.path.join(project['frames_dir'], 'img_004.webp'))
        assert _video_bytes(project, 1) == b'VIDEO-1'
        assert _video_bytes(project, 2) == b'VIDEO-2'


class TestHeroVideo:
    """英雄展示视频（[HERO]，槽位号＝最后一张图）是全片收尾镜头，不属于某一拍：
    删掉一拍后改挂到新的最后一张图上。"""

    def test_hero_reattaches_to_the_new_last_image(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        block = _prompt_block(image_count=4, video_count=4, hero_at=4)
        with open(os.path.join(project['videos_dir'], 'vid_004.mp4'), 'wb') as f:
            f.write(b'VIDEO-HERO')

        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                   'prompt_block': block})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        images, videos = server._parse_prompt_slots(body['prompt_block'])
        assert sorted(images) == [1, 2, 3]
        assert sorted(videos) == [1, 2, 3]
        assert 'HERO' in videos[3]['meta'].upper(), "英雄段改挂到新的最后一张图"
        assert videos[3]['body'] == 'video prompt 4'
        assert _video_bytes(project, 3) == b'VIDEO-HERO'


class TestValidation:
    def test_out_of_range_sequence_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 9,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400
        assert _close(_pixel(project, 1), project['colors'][1]), "被拒绝的请求不应动文件"

    def test_last_remaining_image_cannot_be_deleted(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 1,
                                   'prompt_block': _prompt_block(image_count=1, video_count=0)})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400
        assert os.path.exists(os.path.join(project['frames_dir'], 'img_001.webp'))

    def test_empty_prompt_block_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 1,
                                   'prompt_block': ''})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_unknown_project_returns_404(self, project, monkeypatch, tmp_path):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / 'nope'))
        h, sent = _delete_handler({'title': 'nope', 'sequence': 2,
                                   'prompt_block': _prompt_block()})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 404

    def test_rejected_while_a_frame_run_is_active(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        monkeypatch.setitem(server.ACTIVE_TASKS, 'frames_running',
                            {'status': 'running', 'events': [], 'type': 'frames'})
        assert server.claim_frame_run(project['dir'], 'frames_running') is None
        try:
            h, sent = _delete_handler({'title': 'delete_slot_test', 'sequence': 2,
                                       'prompt_block': _prompt_block()})
            server.SparkRequestHandler.do_POST(h)
            assert sent[0][1] == 409
            assert _close(_pixel(project, 2), project['colors'][2]), "被拒绝的请求不应动文件"
        finally:
            server.release_frame_run(project['dir'], 'frames_running')
