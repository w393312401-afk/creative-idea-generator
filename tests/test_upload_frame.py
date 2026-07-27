"""帧槽位手动上传接口 (/api/upload_frame) 的行为契约。

前端把一张本地图片拖到帧卡片上时调用。除了把图片按本单画幅裁剪后转存成与自动产出
同款的 frames/img_NNN.webp，还有三件必须同时做到的事——漏一件就会让后续环节拿着
过期结论继续跑：

1. 这张图不是模型渲的，一切机器判定（质检 / 一致性审查 / 人工标记）都不再适用；
2. i2i 链在这一帧断开，其后仍派生自旧链的帧要标 stale_lineage；
3. 该帧是相邻两段视频的首/尾锚点，旧合并成片作废，受影响的视频槽位号回给前端提示。

另外与 /api/flag_frame_issue 同款：该项目有渲染/修复 worker 在跑时拒绝写入（409），
否则 worker 收尾时的整体回写会把手动上传的记录覆盖掉。
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


_PROMPT_BLOCK = (
    "图片提示词\n"
    "图片 1:\nfirst frame prompt\n\n"
    "图片 2:\nsecond frame prompt\n\n"
    "图片 3:\nthird frame prompt\n\n"
    "视频提示词\n"
    "视频 1:\nframe 1 to 2\n\n"
    "视频 2:\nframe 2 to 3\n"
)


@pytest.fixture
def project(tmp_path):
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    for seq, color in ((1, (255, 0, 0)), (2, (0, 255, 0)), (3, (0, 0, 255))):
        Image.new('RGB', (90, 160), color=color).save(
            os.path.join(frames_dir, f'img_{seq:03d}.webp'), format='WEBP')

    manifest = {
        'title': 'upload_frame_test',
        'aspect_ratio': '9:16',
        'frames': [
            {'slot': 1, 'sequence': 1, 'file': 'frames/img_001.webp', 'url': '/frames/img_001.webp',
             'prompt': 'first frame prompt', 'model': 'gemini', 'quality_gate': 'sequence_reviewed_pass',
             'vlm_qa_reason': 'looks fine', 'parent_hash': 'abc123'},
            {'slot': 2, 'sequence': 2, 'file': 'frames/img_002.webp', 'url': '/frames/img_002.webp',
             'prompt': 'second frame prompt', 'model': 'gemini', 'quality_gate': 'sequence_review_flagged',
             'vlm_qa_reason': '跳变', 'manual_issue': '塔吊消失了', 'parent_hash': 'def456'},
            {'slot': 3, 'sequence': 3, 'file': 'frames/img_003.webp', 'url': '/frames/img_003.webp',
             'prompt': 'third frame prompt', 'model': 'gemini', 'quality_gate': 'auto_approved',
             'parent_hash': 'ghi789'},
        ],
        'videos': [
            {'slot': 1, 'sequence': 1, 'file': 'videos/vid_001.mp4', 'status': 'success'},
            {'slot': 2, 'sequence': 2, 'file': 'videos/vid_002.mp4', 'status': 'success'},
        ],
        'merged_video': {'file': 'outputs/x/merged.mp4', 'status': 'success'},
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'frames_dir': frames_dir}


def _png_bytes(size=(120, 200), color=(200, 120, 40)):
    buf = io.BytesIO()
    Image.new('RGB', size, color=color).save(buf, format='PNG')
    return buf.getvalue()


def _upload_handler(fields, file_bytes, filename='shot.png', content_type='image/png',
                    include_file=True):
    """手搓 multipart/form-data，走端点里真实的 BytesParser 解析路径（同
    test_upload_video.py 的手法）。"""
    boundary = 'frameboundary456'
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\n'.encode('utf-8')
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8')
        body += f'{v}\r\n'.encode('utf-8')
    if include_file:
        body += f'--{boundary}\r\n'.encode('utf-8')
        body += f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode('utf-8')
        body += f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8')
        body += file_bytes
        body += b'\r\n'
    body += f'--{boundary}--\r\n'.encode('utf-8')
    body = bytes(body)

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/upload_frame'
    headers = Message()
    headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _manifest(project):
    with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


class TestUploadFrameSuccess:
    def test_image_is_stored_as_webp_in_project_aspect_ratio(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(size=(400, 400)),  # 方图：必须按 9:16 居中裁剪后落盘
        )
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        assert body['status'] == 'ok'

        dest = os.path.join(project['frames_dir'], 'img_002.webp')
        with Image.open(dest) as img:
            assert img.format == 'WEBP'
            w, h_px = img.size
            assert abs((w / h_px) - (9 / 16)) < 0.01, f"落盘尺寸 {img.size} 未按 9:16 裁剪"

    def test_machine_verdicts_are_dropped_for_the_uploaded_frame(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        frame = next(f for f in _manifest(project)['frames'] if f['sequence'] == 2)
        assert frame['quality_gate'] == 'pending_manual_review'
        assert frame['vlm_qa_reason'] is None
        assert 'manual_issue' not in frame, "这张图谁也没看过，旧的人工标记不能留着"
        assert frame['model'] == 'manual_upload'
        assert frame['source'] == 'manual_upload'
        assert frame['parent_hash'] == '', "手动上传的帧不派生自任何上一帧，i2i 血统在此断开"
        assert frame['file'].endswith('frames/img_002.webp')

    def test_downstream_frames_marked_stale_lineage(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert not frames[1].get('stale_lineage'), "上游帧不受影响"
        assert not frames[2].get('stale_lineage'), "被替换的这一帧自己不是过期血统"
        assert frames[3].get('stale_lineage') is True

    def test_merged_video_dropped_and_affected_slots_reported(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)

        body, status = sent[0]
        assert status == 200, body
        # IMG 002 是 VID 001 的尾锚点、VID 002 的首锚点
        assert body['affected_video_slots'] == [1, 2]
        assert 'merged_video' not in _manifest(project)

    def test_first_frame_only_affects_its_own_slot(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '1', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]
        assert sent[0][0]['affected_video_slots'] == [1]

    def test_frame_absent_from_manifest_is_created(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '4', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200, sent[0][0]

        frames = {f['sequence']: f for f in _manifest(project)['frames']}
        assert 4 in frames
        assert frames[4]['file'].endswith('frames/img_004.webp')
        assert os.path.exists(os.path.join(project['frames_dir'], 'img_004.webp'))


class TestUploadFrameValidation:
    def test_non_image_payload_returns_400(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
            b'this is definitely not an image',
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400
        # 原图保持不动
        with Image.open(os.path.join(project['frames_dir'], 'img_002.webp')) as img:
            assert img.convert('RGB').getpixel((0, 0))[1] > 200

    def test_missing_file_returns_400(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '2'}, b'', include_file=False)
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_bad_sequence_returns_400(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_frame_test', 'sequence': '0', 'prompt_block': _PROMPT_BLOCK},
            _png_bytes(),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_unknown_project_returns_404(self, project, monkeypatch, tmp_path):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / 'nope'))
        h, sent = _upload_handler(
            {'title': 'nope', 'sequence': '1', 'prompt_block': _PROMPT_BLOCK}, _png_bytes())
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 404

    def test_rejected_while_a_frame_run_is_active(self, project, monkeypatch):
        """渲染/修复 worker 会拿着自己的 manifest 快照整体回写，此刻插进去写的手动帧
        会被它覆盖掉（图在盘上、清单里却没有）——同 /api/flag_frame_issue，直接 409。"""
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        # 占位只有在占用者确实是运行中的任务时才算数（陈旧占位会被自动收回）
        monkeypatch.setitem(server.ACTIVE_TASKS, 'someone_elses_task',
                            {'status': 'running', 'events': [], 'type': 'frames'})
        assert server.claim_frame_run(project['dir'], 'someone_elses_task') is None
        try:
            h, sent = _upload_handler(
                {'title': 'upload_frame_test', 'sequence': '2', 'prompt_block': _PROMPT_BLOCK},
                _png_bytes(),
            )
            server.SparkRequestHandler.do_POST(h)
            assert sent[0][1] == 409
            frame = next(f for f in _manifest(project)['frames'] if f['sequence'] == 2)
            assert frame['model'] == 'gemini', "被拒绝的上传不应改动 manifest"
        finally:
            server.release_frame_run(project['dir'], 'someone_elses_task')
