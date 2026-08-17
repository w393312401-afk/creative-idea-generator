"""交互式多宫格检查器（Collage Viewer）行为测试。"""
import os
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_slot_grid_render import static_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IDEA = {
    "id": "cv_test_1",
    "title": "多宫格检查器测试",
    "prompt_block": "图片提示词\n图片 1:\nframe 1\n\n图片 2:\nframe 2",
    "collage_url": "/outputs/t/test_collage.jpg",
    "frameRun": {
        "collage_url": "/outputs/t/test_collage.jpg",
        "frames": [
            {"sequence": 1, "url": "/outputs/t/frames/img_001.webp"},
            {"sequence": 2, "url": "/outputs/t/frames/img_002.webp"},
            {"sequence": 3, "url": "/outputs/t/frames/img_003.webp"},
        ],
        "videos": [],
    },
}


@pytest.fixture
def page():
    with static_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/index.html", wait_until="load")
        pg.wait_for_function("() => typeof openCollageViewer === 'function'", timeout=5000)
        yield pg
        browser.close()


def test_open_collage_viewer_modal(page):
    page.evaluate("""idea => {
        currentIdea = idea;
        openCollageViewer({ idea: idea });
    }""", IDEA)

    page.wait_for_selector("#collage-viewer-modal", state="visible", timeout=3000)
    assert page.eval_on_selector("#collage-viewer-modal", "el => el.classList.contains('active')")

    # 验证模式切换
    page.eval_on_selector('[data-act="mode-compare"]', "el => el.click()")
    page.wait_for_selector(".cv-compare-container", state="visible", timeout=3000)

    # 验证切回拼图模式
    page.eval_on_selector('[data-act="mode-collage"]', "el => el.click()")
    page.wait_for_selector("#cv-canvas-viewport", state="visible", timeout=3000)

    # 验证关闭
    page.eval_on_selector(".cv-close-btn", "el => el.click()")
    page.wait_for_timeout(300)
    assert page.evaluate("() => !document.getElementById('collage-viewer-modal')")
