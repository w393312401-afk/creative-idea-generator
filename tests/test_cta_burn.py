"""cta_burn：配置中心「成片引导」的服务端设置、自定义透明视频上传、精剪烧录前准备。"""
import json
import subprocess

import pytest

import cta_burn
from tools import engagement_cta
from tools.collage import resolve_binary

FFMPEG = resolve_binary("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg binary not available on this machine")


def _make(path, codec_args, source="color=c=white@0.5:s=90x160:r=30:d=2,format=yuva444p"):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", source, *codec_args, str(path)], check=True)
    return path.read_bytes()


def _alpha_mov(tmp_path, name="cta.mov"):
    return _make(tmp_path / name, ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"])


def test_defaults_burn_builtin_last_five_seconds(tmp_path):
    settings = cta_burn.load_settings(tmp_path)
    assert (settings["enabled"], settings["seconds"], settings["source"]) == (True, 5, "builtin")
    public = cta_burn.public_settings(tmp_path)
    assert public["custom"] is None
    assert public["builtin_preview_url"].startswith("/tools/engagement_cta.html?embed=1")
    assert "duration=5" in public["builtin_preview_url"]


def test_save_settings_validates_and_persists(tmp_path):
    public = cta_burn.save_settings({"enabled": False, "seconds": 8}, tmp_path)
    assert public["settings"] == {"enabled": False, "seconds": 8, "source": "builtin"}
    stored = json.loads((tmp_path / cta_burn.SETTINGS_NAME).read_text())
    assert stored["enabled"] is False and stored["seconds"] == 8
    for bad in ({"seconds": 4}, {"seconds": True}, {"enabled": "yes"}, {"source": "custom"},
                {"volume": 3}, {}, None):
        with pytest.raises(cta_burn.CtaSettingsError):
            cta_burn.save_settings(bad, tmp_path)
    assert cta_burn.load_settings(tmp_path)["seconds"] == 8


@needs_ffmpeg
def test_store_custom_alpha_video_switches_source_and_makes_preview(tmp_path):
    root = tmp_path / "cta"
    public = cta_burn.store_custom(_alpha_mov(tmp_path), "我的引导.mov", root)
    assert public["settings"]["source"] == "custom"
    custom = public["custom"]
    assert custom["name"] == "我的引导.mov"
    assert (custom["width"], custom["height"]) == (90, 160)
    assert custom["duration"] == pytest.approx(2.0, abs=0.05)
    assert custom["url"].startswith("/outputs/engagement_cta/custom/")
    preview = root / "custom" / custom["preview_url"].rsplit("/", 1)[-1]
    assert preview.is_file() and engagement_cta.probe(preview)["alpha"]
    assert not list((root / "custom").glob(".upload-*")), "临时上传文件应已清理"

    plan = cta_burn.prepare(root)
    assert plan["source"] == "custom" and plan["seconds"] == 5
    assert engagement_cta.probe(plan["overlay"])["alpha"]

    # 换一个新视频：旧文件删掉；移除后回到内置动画
    old = root / "custom" / custom["url"].rsplit("/", 1)[-1]
    webm = _make(tmp_path / "cta.webm", ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "40"],
                 source="color=c=red@0.6:s=90x160:r=30:d=1,format=yuva420p")
    replaced = cta_burn.store_custom(webm, "cta.webm", root)
    assert replaced["custom"]["name"] == "cta.webm" and not old.exists()
    removed = cta_burn.remove_custom(root)
    assert removed["custom"] is None and removed["settings"]["source"] == "builtin"
    assert not [p for p in (root / "custom").iterdir() if not p.name.startswith(".")]


@needs_ffmpeg
def test_store_custom_rejects_opaque_or_unsupported_video(tmp_path):
    opaque = _make(tmp_path / "opaque.mov", ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
                   source="color=c=white:s=90x160:r=30:d=1")
    with pytest.raises(cta_burn.CtaSettingsError, match="透明通道"):
        cta_burn.store_custom(opaque, "opaque.mov", tmp_path)
    with pytest.raises(cta_burn.CtaSettingsError, match=".mov"):
        cta_burn.store_custom(b"data", "clip.mp4", tmp_path)
    with pytest.raises(cta_burn.CtaSettingsError, match="无法读取"):
        cta_burn.store_custom(b"not a video", "broken.mov", tmp_path)
    assert cta_burn.load_settings(tmp_path)["custom"] is None
    assert not [p for p in (tmp_path / "custom").iterdir()]


@needs_ffmpeg
def test_missing_custom_file_falls_back_to_builtin(tmp_path, monkeypatch):
    public = cta_burn.store_custom(_alpha_mov(tmp_path), "cta.mov", tmp_path / "cta")
    (tmp_path / "cta" / "custom" / public["custom"]["url"].rsplit("/", 1)[-1]).unlink()
    settings = cta_burn.load_settings(tmp_path / "cta")
    assert settings["source"] == "builtin" and settings["custom_missing"]

    rendered = []

    def fake_cached_overlay(config, fps, cache_dir):
        rendered.append((config, fps, cache_dir))
        return tmp_path / "builtin.mov"

    monkeypatch.setattr(engagement_cta, "cached_overlay", fake_cached_overlay)
    plan = cta_burn.prepare(tmp_path / "cta")
    assert plan == {"source": "builtin", "seconds": 5, "name": cta_burn.BUILTIN_NAME,
                    "overlay": str(tmp_path / "builtin.mov")}
    assert rendered == [({"duration": 5.0}, 30, tmp_path / "cta" / ".cache")]


def test_prepare_returns_none_when_disabled(tmp_path):
    cta_burn.save_settings({"enabled": False}, tmp_path)
    assert cta_burn.prepare(tmp_path) is None
