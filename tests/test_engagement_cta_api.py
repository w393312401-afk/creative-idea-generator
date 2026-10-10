"""配置中心「成片引导」的 HTTP 接口：读写设置、上传/移除自定义透明视频。"""
from email.message import Message
import io
import json
from urllib.parse import quote

import pytest

import cta_burn
import server
from tools.collage import resolve_binary


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cta_burn, "ROOT", tmp_path / "engagement_cta")


def _call(method, path, body=b"", headers=None):
    h = object.__new__(server.SparkRequestHandler)
    h.path = path
    h.command = method
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    for key, value in (headers or {}).items():
        h.headers[key] = value
    h.rfile = io.BytesIO(body)
    h.close_connection = False
    h._gate = lambda *a, **k: True
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    (h.do_GET if method == "GET" else h.do_POST)()
    return sent[0]


def _post_json(path, payload):
    return _call("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def test_get_returns_defaults():
    body, status = _call("GET", "/api/engagement-cta/settings")
    assert status == 200 and body["status"] == "ok"
    assert body["settings"] == {"enabled": True, "seconds": 5, "source": "builtin"}
    assert body["seconds_choices"] == list(cta_burn.SECONDS_CHOICES)


def test_post_settings_saves_and_rejects_invalid_values():
    body, status = _post_json("/api/engagement-cta/settings", {"patch": {"enabled": False, "seconds": 6}})
    assert status == 200 and body["settings"]["enabled"] is False and body["settings"]["seconds"] == 6
    body, status = _post_json("/api/engagement-cta/settings", {"patch": {"seconds": 3}})
    assert status == 400 and body["status"] == "error"
    body, status = _post_json("/api/engagement-cta/settings", {"patch": {"source": "custom"}})
    assert status == 400 and "上传" in body["message"]
    assert _call("GET", "/api/engagement-cta/settings")[0]["settings"]["seconds"] == 6


def test_upload_rejects_oversize_before_reading_body(monkeypatch):
    monkeypatch.setattr(cta_burn, "MAX_CUSTOM_BYTES", 10)
    body, status = _call("POST", "/api/engagement-cta/custom", b"x" * 11,
                         {"Content-Type": "application/octet-stream", "X-Filename": "big.mov"})
    assert status == 413


@pytest.mark.skipif(not resolve_binary("ffmpeg"), reason="ffmpeg binary not available on this machine")
def test_upload_and_remove_custom_video(tmp_path):
    import subprocess
    clip = tmp_path / "clip.mov"
    subprocess.run([resolve_binary("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=white@0.5:s=90x160:r=30:d=1,format=yuva444p",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(clip)], check=True)
    body, status = _call("POST", "/api/engagement-cta/custom", clip.read_bytes(),
                         {"Content-Type": "application/octet-stream", "X-Filename": quote("引导 动画.mov")})
    assert status == 200, body
    assert body["settings"]["source"] == "custom" and body["custom"]["name"] == "引导 动画.mov"

    body, status = _call("POST", "/api/engagement-cta/custom", b"not a video",
                         {"Content-Type": "application/octet-stream", "X-Filename": "bad.mov"})
    assert status == 400 and "无法读取" in body["message"]

    body, status = _post_json("/api/engagement-cta/custom/delete", {})
    assert status == 200 and body["custom"] is None and body["settings"]["source"] == "builtin"
