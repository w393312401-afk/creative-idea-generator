"""合并视图（一拍一列）的行为契约。

契约是 VID N ≡ IMG N → IMG N+1，但两个网格隔着一屏，判断"这一段接不接得上"
要跨屏对号。合并视图把两类卡片并进同一个 CSS 网格：第 N 列＝第 N 拍。

关键不变量：**两种视图下卡片内容完全一致**——合并视图只换容器与列位，
不改渲染器输出，所以卡片 id、操作按钮、拖拽、事件委托、勾选全都照旧成立。
方案见 docs/spark_result_slots_plan.md §F。
"""
import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from test_slot_grid_render import static_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IDEA = {
    "id": "merged1",
    "title": "拍轨",
    "prompt_block": "x",
    "prompt_slots": {
        "images": [{"index": i} for i in range(1, 6)],
        "videos": [{"index": i} for i in range(1, 5)],
    },
    "frameRun": {
        "frames": [
            {"sequence": i, "url": "/outputs/m/frames/img_%03d.webp" % i}
            for i in (1, 2, 3, 5)
        ],
        "videos": [
            {"slot": 1, "url": "/outputs/m/videos/vid_001.mp4"},
            {"slot": 2, "status": "failed", "error": "超时"},
            {"slot": 4, "url": "/outputs/m/videos/vid_004.mp4",
             "source": "manual_upload"},
        ],
    },
}

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


def _snapshot(page):
    """两种视图下都能拿到的、与容器无关的卡片描述。"""
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('.slot-card')).map(c => ({
            id: c.id, type: c.dataset.type, seq: Number(c.dataset.seq),
            kind: c.dataset.kind, badges: c.dataset.badges,
            draggable: c.draggable,
            label: (c.querySelector('.slot-label') || {}).textContent,
            acts: Array.from(c.querySelectorAll('.slot-action-btn')).map(b => b.dataset.act),
        })).sort((a, b) => a.id.localeCompare(b.id))""")


@pytest.fixture
def page():
    with static_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/index.html", wait_until="load")
        pg.wait_for_function(
            "() => ['renderFramesForIdea','setSlotMergedView','slotRenderTarget']"
            ".every(n => typeof window[n] === 'function')", timeout=20000)
        pg.route("**/outputs/**", lambda r: r.fulfill(
            status=200, content_type="image/png", body=PNG))
        pg.evaluate("""idea => {
            localStorage.removeItem('slot_merged_view');
            currentIdea = idea; savedIdeas = [idea];
            switchMainTab('results'); switchTab('overview');
            setSlotMergedView(false, false);
            for (let el = document.getElementById('frames-grid'); el; el = el.parentElement) {
                if (getComputedStyle(el).display === 'none') el.style.display = 'block';
            }
        }""", IDEA)
        pg.wait_for_timeout(300)
        yield pg
        assert not errors, "页面不应有未捕获异常: %s" % errors[:3]
        browser.close()


def _merge(page, on=True):
    page.evaluate("on => setSlotMergedView(on)", on)
    page.wait_for_timeout(250)


class TestContainerRouting:
    def test_split_view_keeps_the_two_grids(self, page):
        counts = page.evaluate("""() => ({
            frames: document.querySelectorAll('#frames-grid .slot-card').length,
            videos: document.querySelectorAll('#videos-grid .slot-card').length,
            beats: document.querySelectorAll('#beats-grid .slot-card').length,
        })""")
        assert counts == {"frames": 5, "videos": 4, "beats": 0}

    def test_merged_view_moves_everything_into_one_container(self, page):
        _merge(page)
        counts = page.evaluate("""() => ({
            frames: document.querySelectorAll('#frames-grid .slot-card').length,
            videos: document.querySelectorAll('#videos-grid .slot-card').length,
            beats: document.querySelectorAll('#beats-grid .slot-card').length,
        })""")
        assert counts == {"frames": 0, "videos": 0, "beats": 9}, \
            "合并后两个原网格必须清空，否则页面上会出现两份同样的卡片"

    def test_toggling_back_restores_the_split_layout(self, page):
        _merge(page)
        _merge(page, False)
        counts = page.evaluate("""() => ({
            frames: document.querySelectorAll('#frames-grid .slot-card').length,
            videos: document.querySelectorAll('#videos-grid .slot-card').length,
            beats: document.querySelectorAll('#beats-grid .slot-card').length,
        })""")
        assert counts == {"frames": 5, "videos": 4, "beats": 0}

    def test_a_regrid_in_merged_view_only_replaces_its_own_row(self, page):
        """两个渲染器共用一个容器：单独重渲帧网格时不能把视频那一行抹掉。"""
        _merge(page)
        page.evaluate("() => renderFramesForIdea(currentIdea)")
        counts = page.evaluate("""() => ({
            img: document.querySelectorAll('#beats-grid .slot-card[data-type=\\"image\\"]').length,
            vid: document.querySelectorAll('#beats-grid .slot-card[data-type=\\"video\\"]').length,
        })""")
        assert counts == {"img": 5, "vid": 4}


class TestSameContent:
    def test_cards_are_identical_in_both_views(self, page):
        split = _snapshot(page)
        _merge(page)
        merged = _snapshot(page)
        assert merged == split, "合并视图只换容器与列位，卡片内容必须一字不差"


class TestPlacement:
    def test_each_beat_gets_its_own_column_with_img_above_vid(self, page):
        _merge(page)
        placed = page.evaluate("""() => Array.from(
            document.querySelectorAll('#beats-grid .slot-card')).map(c => ({
                type: c.dataset.type, seq: Number(c.dataset.seq),
                col: c.style.gridColumn, row: c.style.gridRow,
            }))""")
        for item in placed:
            assert item["col"] == str(item["seq"]), \
                "第 N 拍必须落在第 N 列，实得 %s" % item
            assert item["row"] == ("2" if item["type"] == "video" else "1")

    def test_a_missing_video_leaves_a_visible_gap_in_its_column(self, page):
        """VID 003 这一单没有任何记录，但槽位契约里它存在——合并视图下它应该
        作为"未生成"卡出现在第 3 列下行，而不是让第 3 列空着看不出缺口。"""
        _merge(page)
        cell = page.evaluate("""() => {
            const c = document.getElementById('video-slot-3');
            return c && { kind: c.dataset.kind, col: c.style.gridColumn, row: c.style.gridRow };
        }""")
        assert cell == {"kind": "missing", "col": "3", "row": "2"}

    def test_split_view_clears_the_explicit_placement(self, page):
        _merge(page)
        _merge(page, False)
        assert page.eval_on_selector(
            "#frame-slot-2", "el => el.style.gridColumn") == "", \
            "回到拆分视图必须清掉列位，否则卡片会按拍号排进自然流里的错误位置"


class TestInteractionsStillWork:
    def test_action_buttons_are_delegated_in_merged_view(self, page):
        """委托绑在容器上：合并视图换了容器，#beats-grid 也必须挂上，
        否则所有卡片按钮在合并视图下集体失灵。"""
        _merge(page)
        page.evaluate("""() => {
            window.__calls = [];
            window.retrySingleFrame = s => window.__calls.push(['frame', s]);
            window.retrySingleVideo = s => window.__calls.push(['video', s]);
        }""")
        page.eval_on_selector('#frame-slot-4 [data-act="retry-frame"]', "el => el.click()")
        page.eval_on_selector('#video-slot-2 [data-act="retry-video"]', "el => el.click()")
        assert page.evaluate("() => window.__calls") == [["frame", 4], ["video", 2]]

    def test_lightbox_uses_the_cards_own_type_not_the_container(self, page):
        """#beats-grid 里两类卡片混排，类型必须从卡片上现取。"""
        _merge(page)
        page.evaluate("() => { window.__lb = []; window.openLightbox = (list, i) =>"
                      " window.__lb.push([list[i] && list[i].type, list.length]); }")
        page.eval_on_selector("#frame-slot-1 img", "el => el.click()")
        page.eval_on_selector("#video-slot-1 video", "el => el.click()")
        assert page.evaluate("() => window.__lb") == [["image", 4], ["video", 2]]

    def test_selection_is_still_tracked_per_type(self, page):
        _merge(page)
        page.eval_on_selector("#frame-slot-2 .slot-select-box", "el => el.click()")
        page.eval_on_selector("#video-slot-1 .slot-select-box", "el => el.click()")
        state = page.evaluate("""() => ({
            image: Array.from(slotToolbarState.image.selected),
            video: Array.from(slotToolbarState.video.selected),
        })""")
        assert state == {"image": [2], "video": [1]}, \
            "同一个容器里两类卡片的选中集不能混在一起"

    def test_filter_applies_per_type_inside_the_shared_container(self, page):
        _merge(page)
        page.eval_on_selector(
            '.slot-toolbar[data-slot-type="image"] .slot-filter-btn[data-filter="missing"]',
            "el => el.click()")
        visible = page.evaluate("""() => Array.from(
            document.querySelectorAll('#beats-grid .slot-card'))
            .filter(c => !c.classList.contains('slot-filtered-out'))
            .map(c => c.id).sort()""")
        # 图片只剩缺失的 IMG 004；视频那一行一个都不该被筛掉
        assert visible == ["frame-slot-4", "video-slot-1", "video-slot-2",
                           "video-slot-3", "video-slot-4"], \
            "图片筛选不该连带影响视频那一行，实得 %s" % visible


class TestPersistence:
    def test_merged_view_is_remembered(self, page):
        _merge(page)
        assert page.evaluate("() => localStorage.getItem('slot_merged_view')") == "1"
        _merge(page, False)
        assert page.evaluate("() => localStorage.getItem('slot_merged_view')") == "0"
