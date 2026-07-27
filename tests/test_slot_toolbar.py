"""槽位工具条（筛选 / 多选 / 尺寸档 / 跳到第一个问题）的行为契约。

方案见 docs/spark_result_slots_plan.md §F。工具条只读卡片上的
data-kind / data-badges——那是 renderSlotCard 按 slot_model 的判定写下的，
工具条再算一遍就会有第二套口径。这里同时守住这一点。
"""
import json
import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from test_slot_grid_render import static_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IDEA = {
    "id": "toolbar1",
    "title": "工具条",
    "prompt_block": "x",
    "prompt_slots": {
        "images": [{"index": i} for i in range(1, 7)],
        "videos": [{"index": i} for i in range(1, 6)],
    },
    "frameRun": {
        "frames": [
            {"sequence": 1, "url": "/outputs/t/frames/img_001.webp"},
            {"sequence": 2, "url": "/outputs/t/frames/img_002.webp",
             "quality_gate": "i2i_fallback_degraded"},
            {"sequence": 3, "url": "/outputs/t/frames/img_003.webp",
             "manual_issue": "门开反了"},
            # 4、5 缺失
            {"sequence": 6, "url": "/outputs/t/frames/img_006.webp"},
        ],
        "videos": [{"slot": 1, "url": "/outputs/t/videos/vid_001.mp4"}],
    },
}

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


@pytest.fixture
def page():
    with static_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/index.html", wait_until="load")
        pg.wait_for_function(
            "() => ['renderFramesForIdea','syncSlotToolbar','applySlotSize']"
            ".every(n => typeof window[n] === 'function')", timeout=20000)
        pg.route("**/outputs/**", lambda r: r.fulfill(
            status=200, content_type="image/png", body=PNG))
        pg.evaluate("""idea => {
            currentIdea = idea; savedIdeas = [idea];
            switchMainTab('results'); switchTab('overview');
            renderFramesForIdea(idea); renderVideosForIdea(idea);
            // 结果面板在静态服务下不会被应用自身激活，量布局前先放出来
            for (let el = document.getElementById('frames-grid'); el; el = el.parentElement) {
                if (getComputedStyle(el).display === 'none') el.style.display = 'block';
            }
        }""", IDEA)
        pg.wait_for_timeout(400)
        pg.__dict__["_page_errors"] = errors
        yield pg
        assert not errors, "页面不应有未捕获异常: %s" % errors[:3]
        browser.close()


def _visible(page, grid="frames-grid"):
    return page.evaluate("""gid => Array.from(
        document.querySelectorAll('#' + gid + ' .slot-card'))
        .filter(c => !c.classList.contains('slot-filtered-out'))
        .map(c => Number(c.dataset.seq))""", grid)


def _click_filter(page, which, type_="image"):
    page.eval_on_selector(
        '.slot-toolbar[data-slot-type="%s"] .slot-filter-btn[data-filter="%s"]' % (type_, which),
        "el => el.click()")


class TestFilter:
    def test_counts_come_from_the_rendered_cards(self, page):
        text = page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-count', "el => el.textContent")
        # 6 个槽位、4 张出图、2 枚带徽标（降级 + 人工标记）、2 个缺失
        assert "IMG 4/6" in text, text
        assert "⚠ 2" in text, text
        assert "缺 2" in text, text

    def test_flagged_filter_shows_only_badged_slots(self, page):
        _click_filter(page, "flagged")
        assert _visible(page) == [2, 3]

    def test_missing_filter_shows_only_ungenerated_slots(self, page):
        _click_filter(page, "missing")
        assert _visible(page) == [4, 5]

    def test_all_restores_everything(self, page):
        _click_filter(page, "missing")
        _click_filter(page, "all")
        assert _visible(page) == [1, 2, 3, 4, 5, 6]

    def test_filter_survives_a_full_regrid(self, page):
        """整格重渲会换掉所有卡片；筛选是网格级状态，不该被冲掉。"""
        _click_filter(page, "flagged")
        page.evaluate("() => renderFramesForIdea(currentIdea)")
        assert _visible(page) == [2, 3]

    def test_the_two_grids_filter_independently(self, page):
        _click_filter(page, "missing")
        assert _visible(page) == [4, 5]
        assert _visible(page, "videos-grid") == [1, 2, 3, 4, 5], \
            "视频网格不该被图片网格的筛选带着走"


class TestSelection:
    def _select(self, page, seqs):
        for s in seqs:
            page.eval_on_selector("#frame-slot-%d .slot-select-box" % s, "el => el.click()")

    def test_selecting_reveals_bulk_actions_with_a_count(self, page):
        self._select(page, [2, 4])
        assert page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-bulk',
            "el => !el.hidden")
        assert page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] [data-bulk=\"retry\"]',
            "el => el.textContent") == "重试所选 (2)"

    def test_selection_survives_a_full_regrid(self, page):
        self._select(page, [2, 4])
        page.evaluate("() => renderFramesForIdea(currentIdea)")
        assert page.evaluate("""() => Array.from(
            document.querySelectorAll('#frames-grid .slot-card.is-selected'))
            .map(c => Number(c.dataset.seq))""") == [2, 4]
        assert page.eval_on_selector("#frame-slot-2 .slot-select-box", "el => el.checked")

    def test_selection_drops_slots_that_no_longer_exist(self, page):
        """删除会让槽位总数变小；留着不存在的槽位号，批量操作会打到别的拍上。"""
        self._select(page, [5, 6])
        page.evaluate("""() => {
            currentIdea.prompt_slots.images = currentIdea.prompt_slots.images.slice(0, 4);
            renderFramesForIdea(currentIdea);
        }""")
        assert page.evaluate("() => Array.from(slotToolbarState.image.selected)") == []
        assert page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-bulk', "el => el.hidden")

    def test_clicking_the_checkbox_does_not_open_the_lightbox(self, page):
        page.evaluate("() => { window.__lb = 0; window.openLightbox = () => window.__lb++; }")
        self._select(page, [1])
        assert page.evaluate("() => window.__lb") == 0

    def test_clear_button_empties_the_selection(self, page):
        self._select(page, [1, 2])
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] [data-bulk="clear"]', "el => el.click()")
        assert page.evaluate("() => slotToolbarState.image.selected.size") == 0


class TestBulkDeleteOrder:
    def test_bulk_delete_goes_from_the_last_beat_backwards(self, page):
        """删除会把其后所有槽位整体前移一位。按升序删的话，删完第 3 拍之后
        原来的第 5 拍已经变成第 4 拍，接着去删「第 5 拍」打到的是另一拍。"""
        page.evaluate("""() => {
            window.__deleted = [];
            window.customConfirm = () => Promise.resolve(true);
            window.deleteSlotBeat = (seq) => { window.__deleted.push(seq); return Promise.resolve(true); };
        }""")
        for s in (2, 5, 3):
            page.eval_on_selector("#frame-slot-%d .slot-select-box" % s, "el => el.click()")
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] [data-bulk="delete"]', "el => el.click()")
        page.wait_for_function("() => window.__deleted.length === 3", timeout=5000)
        assert page.evaluate("() => window.__deleted") == [5, 3, 2]

    def test_bulk_retry_goes_in_ascending_order(self, page):
        page.evaluate("""() => {
            window.__retried = [];
            window.customConfirm = () => Promise.resolve(true);
            window.retrySingleFrame = (seq) => { window.__retried.push(seq); return Promise.resolve(); };
        }""")
        for s in (5, 2, 3):
            page.eval_on_selector("#frame-slot-%d .slot-select-box" % s, "el => el.click()")
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] [data-bulk="retry"]', "el => el.click()")
        page.wait_for_function("() => window.__retried.length === 3", timeout=5000)
        assert page.evaluate("() => window.__retried") == [2, 3, 5]


class TestSizeAndJump:
    @pytest.mark.parametrize("size,expected", [("S", "88px"), ("M", "120px"), ("L", "168px")])
    def test_size_preset_drives_the_grid_track_width(self, page, size, expected):
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-size-btn[data-size="%s"]' % size,
            "el => el.click()")
        assert page.eval_on_selector(
            "#frames-grid", "el => el.style.getPropertyValue('--slot-min')") == expected

    def test_size_is_remembered(self, page):
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-size-btn[data-size="L"]',
            "el => el.click()")
        assert page.evaluate("() => localStorage.getItem('slot_grid_size')") == "L"

    def test_jump_button_only_shows_when_something_is_flagged(self, page):
        assert page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-jump-btn', "el => !el.hidden")
        assert page.eval_on_selector(
            '.slot-toolbar[data-slot-type="video"] .slot-jump-btn', "el => el.hidden"), \
            "视频网格这一单没有带徽标的槽位，不该给跳转入口"

    def test_jump_flashes_the_first_flagged_slot(self, page):
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-jump-btn', "el => el.click()")
        assert page.eval_on_selector(
            "#frame-slot-2", "el => el.classList.contains('slot-flash')")
