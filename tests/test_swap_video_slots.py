"""视频槽位换位接口 (/api/swap_video_slots) 的行为契约。

前端把一张视频卡片拖到另一张卡片上时调用。核心契约：槽位编号与网格位置固定不动，
动的只是"哪段视频落在哪个槽位"——因此落盘文件（vid_NNN.mp4 是槽位属性）必须真的
互换/改名，不能只改 manifest，否则合成成片与前端按槽位号取到的还是旧内容。

覆盖点：
- swap：两个槽位都有视频时对调文件与记录，描述内容本身的字段（prompt/model…）跟着
  内容走，slot/file/url 留在原位；anchor_check 作废（换位后首尾帧多半不再对应新槽位
  的锚点图）；merged_video 清掉。
- 目标槽位空着时退化成搬运：源槽位不再留下记录与文件。
- mode=copy：复制一份到目标槽位，源槽位原样保留。
- 源槽位没有落盘文件 / 两个槽位相同 / mode 非法时拒绝，且不动任何文件。
"""
import io
import json
import os
from email.message import Message

import pytest

import server


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


def _write_video(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)


@pytest.fixture
def project(tmp_path):
    project_dir = str(tmp_path / 'proj')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)

    _write_video(os.path.join(videos_dir, 'vid_001.mp4'), b'SLOT-ONE-BYTES')
    _write_video(os.path.join(videos_dir, 'vid_002.mp4'), b'SLOT-TWO-BYTES')

    manifest = {
        'title': 'swap_test',
        'frames': [],
        'videos': [
            {'slot': 1, 'sequence': 1, 'file': 'videos/vid_001.mp4', 'url': '/videos/vid_001.mp4',
             'prompt': 'clip one', 'model': 'veo', 'status': 'success', 'anchor_check': 'ok-1',
             'meta': '', 'is_hero': False},
            {'slot': 2, 'sequence': 2, 'file': 'videos/vid_002.mp4', 'url': '/videos/vid_002.mp4',
             'prompt': 'clip two', 'model': 'veo', 'status': 'success', 'anchor_check': 'ok-2',
             'meta': '[HERO]', 'is_hero': True},
        ],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'videos_dir': videos_dir}


def _swap_handler(payload):
    """构造一个只够跑 /api/swap_video_slots 分支的 SparkRequestHandler
    （同 test_upload_video.py 的手法，走端点里真实的 body 解析路径）。"""
    body = json.dumps(payload).encode('utf-8')
    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/swap_video_slots'
    headers = Message()
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


class TestSwap:
    def test_two_filled_slots_swap_files_and_entries(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['moved'] is False

        # 文件真的换了位：槽位文件名不变，内容对调
        assert _read(os.path.join(project['videos_dir'], 'vid_001.mp4')) == b'SLOT-TWO-BYTES'
        assert _read(os.path.join(project['videos_dir'], 'vid_002.mp4')) == b'SLOT-ONE-BYTES'

        videos = {v['slot']: v for v in _manifest(project)['videos']}
        assert sorted(videos) == [1, 2]
        # 描述内容本身的字段跟着内容走
        assert videos[1]['prompt'] == 'clip two'
        assert videos[2]['prompt'] == 'clip one'
        assert videos[1]['is_hero'] is True
        # 槽位属性留在原位
        assert videos[1]['file'].endswith('videos/vid_001.mp4')
        assert videos[1]['url'].endswith('/videos/vid_001.mp4')
        assert videos[1]['start_anchor_slot'] == 1
        assert videos[2]['start_anchor_slot'] == 2
        # 换位来源留痕、旧锚点结论作废
        assert videos[1]['swapped_from_slot'] == 2
        assert videos[2]['swapped_from_slot'] == 1
        assert videos[1]['anchor_check'] != 'ok-2'
        assert videos[2]['anchor_check'] != 'ok-1'
        assert videos[1]['source'] == 'manual_swap'

    def test_swap_clears_stale_merged_video(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]
        assert 'merged_video' not in _manifest(project)

    def test_empty_target_becomes_a_move(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 3})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['moved'] is True

        # 源槽位的文件与记录都随内容一起离开
        assert not os.path.exists(os.path.join(project['videos_dir'], 'vid_001.mp4'))
        assert _read(os.path.join(project['videos_dir'], 'vid_003.mp4')) == b'SLOT-ONE-BYTES'
        videos = {v['slot']: v for v in _manifest(project)['videos']}
        assert sorted(videos) == [2, 3]
        assert videos[3]['prompt'] == 'clip one'

    def test_sequence_renumbered_after_move(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 3})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]
        videos = sorted(_manifest(project)['videos'], key=lambda v: v['slot'])
        assert [v['sequence'] for v in videos] == [1, 2]


class TestCopy:
    def test_copy_leaves_source_intact(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 3, 'mode': 'copy'})
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['mode'] == 'copy'

        assert _read(os.path.join(project['videos_dir'], 'vid_001.mp4')) == b'SLOT-ONE-BYTES'
        assert _read(os.path.join(project['videos_dir'], 'vid_003.mp4')) == b'SLOT-ONE-BYTES'

        videos = {v['slot']: v for v in _manifest(project)['videos']}
        assert sorted(videos) == [1, 2, 3]
        assert videos[1]['anchor_check'] == 'ok-1', "源槽位没有被动过，旧的锚点结论仍然成立"
        assert videos[3]['swapped_from_slot'] == 1

    def test_copy_over_existing_slot_overwrites_target(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 2, 'mode': 'copy'})
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][1] == 200, sent[0][0]
        assert _read(os.path.join(project['videos_dir'], 'vid_002.mp4')) == b'SLOT-ONE-BYTES'
        videos = {v['slot']: v for v in _manifest(project)['videos']}
        assert videos[2]['prompt'] == 'clip one'


class TestValidation:
    def test_missing_source_file_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        os.remove(os.path.join(project['videos_dir'], 'vid_001.mp4'))
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][1] == 404, sent[0][0]
        # 目标槽位文件原封不动
        assert _read(os.path.join(project['videos_dir'], 'vid_002.mp4')) == b'SLOT-TWO-BYTES'
        assert 'merged_video' in _manifest(project), "被拒绝的请求不应改动 manifest"

    def test_same_slot_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 2, 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_bad_mode_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 1, 'to_slot': 2, 'mode': 'delete'})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400
        assert _read(os.path.join(project['videos_dir'], 'vid_001.mp4')) == b'SLOT-ONE-BYTES'

    def test_non_numeric_slot_rejected(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _swap_handler({'title': 'swap_test', 'from_slot': 'a', 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_unknown_project_returns_404(self, project, monkeypatch, tmp_path):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / 'nope'))
        h, sent = _swap_handler({'title': 'nope', 'from_slot': 1, 'to_slot': 2})
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 404
