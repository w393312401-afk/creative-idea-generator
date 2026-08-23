"""视频槽位手动上传接口 (/api/upload_video) 的行为契约。

覆盖点：
- 正常上传：直接落盘、更新 manifest.videos，
  并清掉过期的 merged_video（内容已变，旧合并成片不再准确）；已存在的旧文件
  会被新上传的内容覆盖。
- 手动上传即使首尾帧不匹配也直接放行写入，不弹窗拦截或返回 409。
- 上传内容统一经 ffmpeg 转码成 h264/yuv420p mp4，不依赖原始容器/编码。
"""
import io
import json
import os
import shutil
import subprocess
from email.message import Message

import pytest
from PIL import Image

import server


pytestmark = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason="需要本机安装 ffmpeg")


def _make_frame(path, rgb):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (64, 64), color=rgb).save(path)


def _make_two_tone_video(path, color1, color2, seg_seconds=0.6, fps=10):
    """生成前半段纯色 color1、后半段纯色 color2 的测试视频，模拟真实的首尾锚点内容。"""
    tmp_dir = os.path.dirname(path)
    seg1 = os.path.join(tmp_dir, os.path.basename(path) + '.seg1.mp4')
    seg2 = os.path.join(tmp_dir, os.path.basename(path) + '.seg2.mp4')
    for seg, color in ((seg1, color1), (seg2, color2)):
        subprocess.run([
            'ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
            '-i', f'color=c={color}:s=64x64:d={seg_seconds}:r={fps}',
            '-pix_fmt', 'yuv420p', seg,
        ], check=True)
    concat_list = os.path.join(tmp_dir, os.path.basename(path) + '.concat.txt')
    with open(concat_list, 'w', encoding='utf-8') as f:
        f.write(f"file '{seg1}'\nfile '{seg2}'\n")
    subprocess.run([
        'ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
        '-i', concat_list, '-c', 'copy', path,
    ], check=True)
    return path


_PROMPT_BLOCK = (
    "图片提示词\n"
    "图片 1:\nfirst frame prompt\n\n"
    "图片 2:\nsecond frame prompt\n\n"
    "视频提示词\n"
    "视频 1:\nmove from frame 1 to frame 2\n"
)


@pytest.fixture(autouse=True)
def _no_access_gate(monkeypatch):
    monkeypatch.setattr(server, 'ACCESS_CODE', '', raising=False)


@pytest.fixture
def project(tmp_path):
    project_dir = str(tmp_path / 'proj')
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(videos_dir, exist_ok=True)

    frame1 = os.path.abspath(os.path.join(frames_dir, 'img_001.webp'))
    frame2 = os.path.abspath(os.path.join(frames_dir, 'img_002.webp'))
    _make_frame(frame1, (255, 0, 0))   # 红：槽位 1 的起始锚点
    _make_frame(frame2, (0, 255, 0))   # 绿：槽位 1 的结束锚点

    manifest = {
        'title': 'upload_video_test',
        'frames': [
            {'slot': 1, 'sequence': 1, 'file': frame1.replace('\\', '/'), 'url': '/x'},
            {'slot': 2, 'sequence': 2, 'file': frame2.replace('\\', '/'), 'url': '/x'},
        ],
        'videos': [],
    }
    with open(os.path.join(project_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)

    return {'dir': project_dir, 'frames_dir': frames_dir, 'videos_dir': videos_dir,
            'frame1': frame1, 'frame2': frame2}


def _upload_handler(fields, file_bytes, filename='clip.mp4', content_type='video/mp4'):
    """构造一个只够跑 /api/upload_video 分支的 SparkRequestHandler（同
    test_compose_cancel_latency.py 的 _cancel_handler 手法），body 是手搓的真实
    multipart/form-data，走的是端点里真实的 BytesParser 解析路径，不是打桩。"""
    boundary = 'testboundary123'
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\n'.encode('utf-8')
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8')
        body += f'{v}\r\n'.encode('utf-8')
    body += f'--{boundary}\r\n'.encode('utf-8')
    body += f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'.encode('utf-8')
    body += f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8')
    body += file_bytes
    body += b'\r\n'
    body += f'--{boundary}--\r\n'.encode('utf-8')
    body = bytes(body)

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/upload_video'
    headers = Message()
    headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    headers['Content-Length'] = str(len(body))
    h.headers = headers
    h.rfile = io.BytesIO(body)
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    return h, sent


def _read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


class TestUploadVideoAnchorMatch:
    def test_matching_anchors_upload_succeeds_and_updates_manifest(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])

        clip = _make_two_tone_video(str(tmp_path / 'clip_src.mp4'), '0xff0000', '0x00ff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200, body
        assert body['status'] == 'ok'
        video = body['video']
        assert video['slot'] == 1
        assert video['status'] == 'success'
        assert video['source'] == 'manual_upload'
        assert video['model'] == 'manual_upload'

        dest = os.path.join(project['videos_dir'], 'vid_001.mp4')
        assert os.path.exists(dest)
        assert os.path.getsize(dest) > 0

        with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
            mdata = json.load(f)
        assert [v['slot'] for v in mdata['videos']] == [1]
        assert mdata['videos'][0]['source'] == 'manual_upload'

    def test_upload_clears_stale_merged_video_entry(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        manifest_path = os.path.join(project['dir'], 'manifest.json')
        with open(manifest_path, encoding='utf-8') as f:
            mdata = json.load(f)
        mdata['merged_video'] = {'file': 'outputs/x/old_merged.mp4', 'status': 'success'}
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(mdata, f)

        clip = _make_two_tone_video(str(tmp_path / 'clip_src2.mp4'), '0xff0000', '0x00ff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][1] == 200, sent[0][0]
        with open(manifest_path, encoding='utf-8') as f:
            mdata_after = json.load(f)
        assert 'merged_video' not in mdata_after

    def test_upload_overwrites_existing_slot_file(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        old_path = os.path.join(project['videos_dir'], 'vid_001.mp4')
        with open(old_path, 'wb') as f:
            f.write(b'old fake bytes')

        clip = _make_two_tone_video(str(tmp_path / 'clip_src3.mp4'), '0xff0000', '0x00ff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)

        assert sent[0][1] == 200, sent[0][0]
        assert os.path.getsize(old_path) > len(b'old fake bytes')


class TestUploadVideoAnchorMismatch:
    def test_mismatched_anchors_succeed_directly(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])

        # 槽位 1 期望红→绿；这里传一段蓝→黄的视频，首尾都对不上，也应直接成功写入并返回 200。
        clip = _make_two_tone_video(str(tmp_path / 'mismatch.mp4'), '0x0000ff', '0xffff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200, body
        assert body['status'] == 'ok'
        assert body['video']['slot'] == 1
        assert 'anchor_check' in body['video']

        dest = os.path.join(project['videos_dir'], 'vid_001.mp4')
        assert os.path.exists(dest)

        with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
            mdata = json.load(f)
        assert len(mdata['videos']) == 1
        assert mdata['videos'][0]['slot'] == 1


class TestUploadVideoValidation:
    def test_missing_video_file_returns_400(self, project, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            b'',
        )
        # 没有文件内容时不附带 file part，模拟真实的"忘记选文件"提交
        boundary = 'testboundary123'
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="title"\r\n\r\nupload_video_test\r\n'
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="slot"\r\n\r\n1\r\n'
            f'--{boundary}--\r\n'
        ).encode('utf-8')
        h.rfile = io.BytesIO(body)
        headers = Message()
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        headers['Content-Length'] = str(len(body))
        h.headers = headers

        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 400

    def test_unknown_project_returns_404(self, project, monkeypatch, tmp_path):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: str(tmp_path / 'does_not_exist'))
        clip = _make_two_tone_video(str(tmp_path / 'clip404.mp4'), '0xff0000', '0x00ff00')
        h, sent = _upload_handler(
            {'title': 'nope', 'slot': '1', 'prompt_block': _PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 404


_HERO_PROMPT_BLOCK = (
    "图片提示词\n"
    "图片 1:\nfirst frame prompt\n\n"
    "图片 2:\nsecond frame prompt\n\n"
    "视频提示词\n"
    "视频 1:\nmove from frame 1 to frame 2\n\n"
    "视频 2 [HERO]:\nhero showcase clip\n"
)


class TestUploadVideoHeroMetaPropagation:
    """2026-07-23 修复：该槽位此前从未生成成功过（manifest.videos 里没有旧记录）时，
    手动上传的英雄展示视频（[HERO]）必须把 meta 里的 HERO 标记落盘——
    merge_project_videos 靠 meta 字段识别英雄片段（不是 is_hero），少了这个标记，
    合成阶段会直接把这段手动上传的视频当无关内容忽略，成片里悄悄漏掉这一段。"""

    def test_first_time_hero_upload_persists_hero_meta(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])

        # 槽位 2 是 HERO：只上传首帧（frame2，绿），因此传一段纯绿色视频。
        clip = _make_two_tone_video(str(tmp_path / 'hero_clip.mp4'), '0x00ff00', '0x00ff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '2', 'prompt_block': _HERO_PROMPT_BLOCK},
            _read_bytes(clip),
        )
        server.SparkRequestHandler.do_POST(h)

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200, body
        assert body['video']['is_hero'] is True
        assert 'HERO' in body['video']['meta'].upper()

        with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
            mdata = json.load(f)
        uploaded = next(v for v in mdata['videos'] if v['slot'] == 2)
        assert 'HERO' in uploaded['meta'].upper(), (
            "merge_project_videos 靠 meta 里的 HERO 标记识别英雄片段；"
            "没有旧 manifest 记录时必须落回 prompt_block 解析结果，否则会被合成阶段静默忽略"
        )


class TestManualUploadMergeRecognition:
    """测试手动上传的视频（尤其是 slot >= len(frames) 的末尾槽位，如 12 帧项目上传了 VID 12）
    在合成视频 (merge_project_videos) 时能被正确识别并完整并入成片。"""

    def test_manual_upload_at_last_slot_is_merged(self, project, tmp_path, monkeypatch):
        from video_generator import merge_project_videos

        monkeypatch.setattr(server, '_get_project_dir', lambda title: project['dir'])

        # 构造 12 个 frames 和前 11 段视频
        frames = []
        for i in range(1, 13):
            f_path = os.path.join(project['frames_dir'], f'frame_{i:03d}.png')
            _make_frame(f_path, (i * 15 % 255, 100, 100))
            frames.append({'slot': i, 'file': f'frames/frame_{i:03d}.png', 'status': 'success'})

        videos = []
        for i in range(1, 12):
            v_path = os.path.join(project['videos_dir'], f'vid_{i:03d}.mp4')
            _make_two_tone_video(v_path, '0xff0000', '0x00ff00')
            videos.append({'slot': i, 'file': f'videos/vid_{i:03d}.mp4', 'status': 'success'})

        with open(os.path.join(project['dir'], 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': 'upload_video_test', 'frames': frames, 'videos': videos}, f)

        # 手动上传第 12 段视频 (slot=12, 即 VID 012)
        clip12 = _make_two_tone_video(str(tmp_path / 'clip12.mp4'), '0x0000ff', '0xffff00')
        h, sent = _upload_handler(
            {'title': 'upload_video_test', 'slot': '12'},
            _read_bytes(clip12),
        )
        server.SparkRequestHandler.do_POST(h)
        assert sent[0][1] == 200

        # 执行合并视频
        result = merge_project_videos(project['dir'], speed=2.0)
        assert result is not None
        assert result['status'] == 'success'

        # 检查 concat 列表，确保 vid_012.mp4 被包含进来了
        with open(os.path.join(project['dir'], 'manifest.json'), encoding='utf-8') as f:
            mdata = json.load(f)
        assert len(mdata['videos']) == 12
        assert any(v['slot'] == 12 and v['source'] == 'manual_upload' for v in mdata['videos'])

