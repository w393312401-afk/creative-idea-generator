"""tools/engagement_cta：引导动画的时间换算、叠加几何、逐帧渲染与叠加成片。

渲染用例会起无头 Chromium（约 2 秒），叠加用例用 FFmpeg 合成的小视频，不依赖 outputs/。
"""
import argparse
import subprocess

import pytest

from tools import engagement_cta as cta
from tools.collage import resolve_binary

FFMPEG = resolve_binary("ffmpeg")


def _args(**overrides):
    base = dict(labels=None, counts=None, avatar=None, headline=None, scale=None, duration=None, hold=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_start_end_number_and_from_end():
    assert cta.resolve_start("end", 40.0, 5.0) == 35.0
    assert cta.resolve_start("3", 40.0, 5.0) == 3.0
    assert cta.resolve_start("-8", 40.0, 5.0) == 32.0
    # 成片比动画短：从 0 开始，不出现负数
    assert cta.resolve_start("end", 4.0, 5.0) == 0.0
    assert cta.resolve_start("-5", 4.0, 5.0) == 0.0
    with pytest.raises(cta.CtaError):
        cta.resolve_start("soon", 40.0, 5.0)


def test_overlay_geometry_keeps_9x16_and_hugs_right_edge():
    assert cta.overlay_geometry(1080, 1920) == (1080, 1920, 0, 0)
    assert cta.overlay_geometry(360, 640) == (360, 640, 0, 0)
    # 横屏：按高度缩放、贴右
    assert cta.overlay_geometry(1920, 1080) == (608, 1080, 1312, 0)
    # 比 9:16 更窄更高：按宽度缩放、垂直居中
    assert cta.overlay_geometry(1080, 2340) == (1080, 1920, 0, 210)
    # 自定义方形素材放进竖屏成片：等比放大到满宽、垂直居中
    assert cta.overlay_geometry(1080, 1920, 500, 500) == (1080, 1080, 0, 420)


def test_build_config_only_carries_explicit_fields():
    assert cta.build_config(_args()) == {}
    config = cta.build_config(_args(labels="A,B,C,D,E", counts="1,2,3,4", scale=1.2, duration=6))
    assert config == {"labels": ["A", "B", "C", "D", "E"], "counts": ["1", "2", "3", "4"],
                      "scale": 1.2, "duration": 6.0}
    with pytest.raises(cta.CtaError):
        cta.build_config(_args(labels="Follow,Like"))
    with pytest.raises(cta.CtaError):
        cta.build_config(_args(duration=3))   # 五次点击挤不下


def test_cache_key_ignores_explicit_defaults():
    assert cta.config_key({}, 30) == cta.config_key({"duration": 5, "hold": 0.3}, 30)
    assert cta.config_key({}, 30) != cta.config_key({"duration": 6}, 30)


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


CHROMIUM_AVAILABLE = _chromium_available()


def _rail_boxes(page):
    return page.evaluate("""() => [...document.querySelectorAll('.avatar, .item > .icon')].map(el => {
        const box = el.getBoundingClientRect();
        return [box.x + box.width / 2, box.y + box.height / 2, box.width, box.height];
    })""")


@pytest.mark.skipif(not CHROMIUM_AVAILABLE, reason="Chromium for playwright not installed")
@pytest.mark.parametrize("config", [{}, {"counts": ["10", "20", "30", "40"]}])
def test_rail_matches_tiktok_reference_positions_with_or_without_counts(config):
    # 用户的 TikTok 预览截图换算到 1080×1920，头像、点赞、评论、收藏、分享。
    # 直接锁定可见元素的屏幕坐标：加自定义计数不能把整栏向上挪。
    expected = [
        (1000, 1128, 126, 126),
        (1000, 1278, 96, 96),
        (1000, 1410, 96, 96),
        (1000, 1542, 96, 96),
        (1000, 1674, 96, 96),
    ]
    with cta._Page(config) as page:
        page.page.evaluate("window.renderAt(0.3)")
        boxes = _rail_boxes(page.page)
    assert len(boxes) == len(expected)
    for box, target in zip(boxes, expected):
        assert box == pytest.approx(target, abs=0.01)


@pytest.mark.skipif(not CHROMIUM_AVAILABLE, reason="Chromium for playwright not installed")
@pytest.mark.parametrize("config", [
    {},
    {"labels": ["", "", "", "", ""]},
    {"labels": [" ", "\t", "", "\n", "  "]},
])
def test_default_and_blank_labels_have_no_text_or_empty_bubbles(config):
    with cta._Page(config) as page:
        assert page.page.locator("#layer .count").count() == 0
        assert page.page.locator("#layer .pill").count() == 0
        # 覆盖五次点击和退场后的保持阶段，避免只在初始隐藏帧看起来没有英文。
        for t in (0.3, 0.95, 1.7, 2.4, 3.15, 3.95, 4.4):
            page.page.evaluate("t => window.renderAt(t)", t)
            assert page.page.locator("#layer").text_content().strip() == ""


@pytest.mark.skipif(not CHROMIUM_AVAILABLE, reason="Chromium for playwright not installed")
def test_explicit_labels_keep_action_indices_and_numeric_counts_increment():
    config = {"labels": ["", "赞", " ", "", "转发"], "counts": ["10", "20", "30", "40"]}
    with cta._Page(config) as page:
        counts = page.page.locator("#layer .count")
        assert counts.all_text_contents() == ["10", "20", "30", "40"]
        assert page.page.locator("#layer .pill").all_text_contents() == ["赞", "转发"]

        # 空项不创建气泡，但后面的标签仍在原来的点赞、分享时刻和高度出现。
        for t, text, y in ((0.95, None, None), (1.7, "赞", 1278),
                           (2.4, None, None), (3.95, "转发", 1674)):
            page.page.evaluate("t => window.renderAt(t)", t)
            pills = page.page.locator("#layer .pill:visible")
            assert pills.all_text_contents() == ([] if text is None else [text])
            if text is not None:
                box = pills.bounding_box()
                assert box["y"] + box["height"] / 2 == pytest.approx(y, abs=0.01)

        page.page.evaluate("window.renderAt(4.4)")
        assert counts.all_text_contents() == ["11", "21", "31", "41"]


@pytest.mark.skipif(not CHROMIUM_AVAILABLE, reason="Chromium for playwright not installed")
def test_rendered_frames_are_transparent_and_icons_light_up(tmp_path):
    from PIL import Image

    # 1080×1920 的 TikTok 互动栏目标位置。
    heart, bookmark, avatar = (1000, 1278), (1000, 1542), (1000, 1128)
    with cta._Page({}) as page:
        assert page.duration == pytest.approx(5.0)
        page.shot(0.3, tmp_path / "before.png")   # 互动栏已入场，手势还在画外
        page.shot(4.4, tmp_path / "after.png")    # 五项都已点亮，手势已退场
    before = Image.open(tmp_path / "before.png").convert("RGBA")
    after = Image.open(tmp_path / "after.png").convert("RGBA")

    assert before.size == (cta.WIDTH, cta.HEIGHT)
    assert before.getpixel((10, 10))[3] == 0, "画布背景必须透明"
    assert before.getpixel(avatar)[3] == 0, "头像框内必须透明"
    assert after.crop((600, 1100, 900, 1740)).getchannel("A").getbbox() is None, \
        "默认引导不应在图标左侧留下英文标签或彩色空气泡"
    assert min(before.getpixel(heart)[:3]) > 240, "点赞前心形应为白色"
    assert min(before.getpixel(bookmark)[:3]) > 240, "收藏前书签应为白色"

    r, g, b, a = after.getpixel(heart)
    assert a == 255 and r > 240 and g < 70 and 60 < b < 110, "点赞后心形应为红色"
    r, g, b, a = after.getpixel(bookmark)
    assert a == 255 and r > 240 and 180 < g < 215 and b < 30, "收藏后书签应为黄色"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg binary not available on this machine")
def test_apply_places_overlay_in_window_and_keeps_length_and_audio(tmp_path):
    from PIL import Image

    video = tmp_path / "clip.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=360x640:r=30:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
                   check=True)
    # 假叠加层：1 秒半透明白色，带 alpha 的 ProRes 4444
    overlay = tmp_path / "overlay.mov"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=white@0.5:s=1080x1920:r=30:d=1,format=yuva444p",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(overlay)],
                   check=True)

    output = cta.apply(video, overlay, "2", tmp_path / "out.mp4")
    info = cta.probe(output)
    assert info["has_audio"]
    assert info["duration"] == pytest.approx(4.0, abs=0.1)

    def brightness_at(t):
        frame = tmp_path / f"f_{t}.png"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", str(t), "-i", str(output),
                        "-frames:v", "1", str(frame)], check=True)
        return Image.open(frame).convert("L").getpixel((180, 320))

    assert brightness_at(1.5) < 20, "叠加开始前画面应保持原样"
    assert 100 < brightness_at(2.5) < 160, "叠加窗口内应有半透明白色"
    assert brightness_at(3.5) < 20, "叠加结束后画面应恢复原样"

    with pytest.raises(cta.CtaError):
        cta.apply(video, overlay, "end", video)
