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

# 「全部修复」用的单子：3 帧带待修问题（审查未过 ×2 + 人工标记 ×1），
# 另有一帧只是降级——降级修不了，不该进一键修复的名单。
FIX_IDEA = {
    "id": "fixall1",
    "title": "全部修复",
    "prompt_block": "x",
    "prompt_slots": {"images": [{"index": i} for i in range(1, 6)],
                     "videos": [{"index": i} for i in range(1, 5)]},
    "frameRun": {
        "frames": [
            {"sequence": 1, "url": "/outputs/t/frames/img_001.webp"},
            {"sequence": 2, "url": "/outputs/t/frames/img_002.webp",
             "quality_gate": "sequence_review_flagged", "vlm_qa_reason": "塔吊消失"},
            {"sequence": 3, "url": "/outputs/t/frames/img_003.webp",
             "quality_gate": "i2i_fallback_degraded"},
            {"sequence": 4, "url": "/outputs/t/frames/img_004.webp",
             "manual_issue": "门开反了"},
            {"sequence": 5, "url": "/outputs/t/frames/img_005.webp",
             "quality_gate": "sequence_review_flagged", "vlm_qa_reason": "层数对不上"},
        ],
        "videos": [],
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
            document.getElementById('frames-grid')?.scrollIntoView();
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


class TestUndoFixEntry:
    """「撤销修复」：修复是覆盖写同一个帧文件，只有真的存下过快照（manifest 上的
    fix_backup）的帧才给这个出口——不能画一枚点了必然报错的按钮。"""

    def test_button_only_shows_on_frames_with_a_snapshot(self, page):
        page.evaluate("""() => {
            currentIdea.frameRun.frames[0].fix_backup = { at: 'T1', reason: '塔吊消失' };
            renderFramesForIdea(currentIdea);
        }""")
        assert page.evaluate(
            """() => !!document.querySelector('#frame-slot-1 [data-act="undo-fix"]')""")
        assert page.evaluate(
            """() => !document.querySelector('#frame-slot-2 [data-act="undo-fix"]')"""), \
            "没修过的帧不该有撤销入口"

    def test_click_goes_to_undo_with_that_sequence(self, page):
        page.evaluate("""() => {
            window.__undone = [];
            window.undoFrameFix = (seq) => { window.__undone.push(seq); return Promise.resolve(); };
            currentIdea.frameRun.frames[0].fix_backup = { at: 'T1', reason: '塔吊消失' };
            renderFramesForIdea(currentIdea);
        }""")
        page.eval_on_selector('#frame-slot-1 [data-act="undo-fix"]', "el => el.click()")
        page.wait_for_function("() => window.__undone.length === 1", timeout=5000)
        assert page.evaluate("() => window.__undone") == [1]


class TestReviewScopeEntries:
    """一致性审查分两个入口：默认增量（只重审帧图变过的那几拍）与全量重审。
    全量会烧掉整套调用，点之前必须先说清代价。"""

    def test_full_review_button_confirms_then_asks_for_the_full_scope(self, page):
        page.evaluate("""() => {
            window.__scopes = [];
            window.__confirmed = 0;
            window.customConfirm = () => { window.__confirmed++; return Promise.resolve(true); };
            window.runSequenceReview = (scope) => { window.__scopes.push(scope); };
        }""")
        page.eval_on_selector("#run-full-sequence-review-btn", "el => el.click()")
        page.wait_for_function("() => window.__scopes.length === 1", timeout=5000)
        assert page.evaluate("() => window.__confirmed") == 1
        assert page.evaluate("() => window.__scopes") == ["full"]

    def test_declining_the_confirmation_runs_nothing(self, page):
        page.evaluate("""() => {
            window.__scopes = [];
            window.customConfirm = () => Promise.resolve(false);
            window.runSequenceReview = (scope) => { window.__scopes.push(scope); };
        }""")
        page.eval_on_selector("#run-full-sequence-review-btn", "el => el.click()")
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__scopes") == []

    def test_plain_review_button_uses_the_incremental_default(self, page):
        page.evaluate("""() => {
            window.__scopes = [];
            window.runSequenceReview = (scope) => { window.__scopes.push(scope); };
        }""")
        page.eval_on_selector("#run-sequence-review-btn", "el => el.click()")
        page.wait_for_function("() => window.__scopes.length === 1", timeout=5000)
        assert page.evaluate("() => window.__scopes") == [None]


FIX_ALL_BTN = '.slot-toolbar[data-slot-type="image"] .slot-fix-all-btn'


class TestFixAll:
    """「一键全部修复」：把所有画着「修复此帧问题」的格子依次走一遍定向修复。

    名单必须与卡片按钮同源（data-fixable，由 slot_model.frameIsFixable 判定），
    顺序必须升序（非首帧走图生图链式编辑，后面的帧要读到前面已修好的画面），
    并且每一帧都在轮到它的那一刻重新确认还要不要修。
    """

    def _load(self, page, idea=FIX_IDEA):
        page.evaluate("""idea => {
            currentIdea = idea; savedIdeas = [idea];
            renderFramesForIdea(idea);
        }""", idea)

    def _stub(self, page, script="return { status: 'ok', remaining: [] };"):
        page.evaluate("""body => {
            window.__fixed = [];
            window.customConfirm = () => Promise.resolve(true);
            // 回读 manifest 走的是真 fetch，静态服务下没有这个接口
            window.reloadManifestIntoIdea = () => Promise.resolve();
            window.fixFrameIssue = new Function('seq', 'window.__fixed.push(seq);' + body);
        }""", script)

    def _click(self, page):
        page.eval_on_selector(FIX_ALL_BTN, "el => el.click()")

    def test_button_counts_only_frames_that_have_something_to_fix(self, page):
        self._load(page)
        assert page.eval_on_selector(FIX_ALL_BTN, "el => !el.hidden")
        # 5 帧里 2/4/5 有待修问题；第 3 帧只是降级——降级修不了，不进名单
        assert page.eval_on_selector(FIX_ALL_BTN, "el => el.textContent") == "🛠 全部修复 (3)"

    def test_button_hides_when_nothing_needs_fixing(self, page):
        clean = json.loads(json.dumps(FIX_IDEA))
        for f in clean["frameRun"]["frames"]:
            f.pop("quality_gate", None)
            f.pop("manual_issue", None)
        self._load(page, clean)
        assert page.eval_on_selector(FIX_ALL_BTN, "el => el.hidden")

    def test_video_toolbar_has_no_fix_all_entry(self, page):
        assert page.evaluate(
            """() => !document.querySelector(
                '.slot-toolbar[data-slot-type="video"] .slot-fix-all-btn')"""), \
            "视频槽位没有「待修问题」这一说，不该给一键修复入口"

    def test_fixes_every_flagged_frame_in_ascending_order(self, page):
        self._load(page)
        self._stub(page)
        self._click(page)
        page.wait_for_function("() => window.__fixed.length === 3", timeout=5000)
        assert page.evaluate("() => window.__fixed") == [2, 4, 5]

    def test_skips_frames_whose_issue_is_already_gone(self, page):
        """前面几帧的修复可能把后面那条问题一并带掉：轮到它时要现取一次，
        对着一张没有记录问题的帧调修复接口，后端会直接报错。"""
        self._load(page)
        self._stub(page, """
            if (seq === 2) {
                currentIdea.frameRun.frames[3].manual_issue = '';
                renderFramesForIdea(currentIdea);
            }
            return { status: 'ok', remaining: [] };""")
        self._click(page)
        page.wait_for_function("() => window.__fixed.length === 2", timeout=5000)
        page.wait_for_timeout(200)
        assert page.evaluate("() => window.__fixed") == [2, 5]

    def test_a_cancel_stops_the_rest(self, page):
        """取消是对整轮批量的取消：继续去修下一帧等于点了取消什么也没发生。"""
        self._load(page)
        self._stub(page, "return { status: 'cancelled' };")
        self._click(page)
        page.wait_for_function("() => window.__fixed.length === 1", timeout=5000)
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__fixed") == [2]

    def test_a_failure_stops_the_rest(self, page):
        self._load(page)
        self._stub(page, "return { status: 'failed', error: '上游报错' };")
        self._click(page)
        page.wait_for_function("() => window.__fixed.length === 1", timeout=5000)
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__fixed") == [2]

    def test_a_second_click_cannot_start_an_overlapping_round(self, page):
        """帧与帧之间有一段没有任务登记的空隙（回读 manifest、弹确认框），
        光靠 isIdeaTaskActive 挡不住第二次点击。"""
        self._load(page)
        self._stub(page)
        page.evaluate("""() => {
            const b = document.querySelector('%s');
            b.click(); b.click();
        }""" % FIX_ALL_BTN)
        page.wait_for_function("() => window.__fixed.length === 3", timeout=5000)
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__fixed") == [2, 4, 5]

    def test_declining_the_confirmation_fixes_nothing(self, page):
        self._load(page)
        self._stub(page)
        page.evaluate("() => { window.customConfirm = () => Promise.resolve(false); }")
        self._click(page)
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__fixed") == []


RETRY_DIRTY_BTN = '.slot-toolbar[data-slot-type="image"] .slot-retry-dirty-btn'

DIRTY_IDEA = {
    "id": "dirty1",
    "title": "已改动重渲",
    "prompt_block": "x",
    "prompt_slots": {"images": [{"index": i} for i in range(1, 5)],
                     "videos": [{"index": i} for i in range(1, 4)]},
    "frameRun": {
        "frames": [
            {"sequence": 1, "url": "/outputs/t/frames/img_001.webp"},
            {"sequence": 2, "url": "/outputs/t/frames/img_002.webp", "prompt_dirty": True},
            {"sequence": 3, "url": "/outputs/t/frames/img_003.webp"},
            {"sequence": 4, "url": "/outputs/t/frames/img_004.webp", "prompt_dirty": True},
        ],
        "videos": [],
    },
}


class TestRetryDirty:
    def _load(self, page, idea=DIRTY_IDEA):
        page.evaluate("""idea => {
            currentIdea = idea; savedIdeas = [idea];
            renderFramesForIdea(idea);
        }""", idea)

    def _stub(self, page):
        page.evaluate("""() => {
            window.__retried = [];
            window.customConfirm = () => Promise.resolve(true);
            window.reloadManifestIntoIdea = () => Promise.resolve();
            window.retrySingleFrame = (seq) => {
                window.__retried.push(seq);
                return Promise.resolve();
            };
        }""")

    def test_button_shows_and_counts_dirty_slots(self, page):
        self._load(page)
        assert page.eval_on_selector(RETRY_DIRTY_BTN, "el => !el.hidden")
        assert page.eval_on_selector(RETRY_DIRTY_BTN, "el => el.textContent") == "⚡ 重渲已改动帧 (2)"

    def test_button_hides_when_no_dirty_slots(self, page):
        clean = json.loads(json.dumps(DIRTY_IDEA))
        for f in clean["frameRun"]["frames"]:
            f.pop("prompt_dirty", None)
        self._load(page, clean)
        assert page.eval_on_selector(RETRY_DIRTY_BTN, "el => el.hidden")

    def test_retries_dirty_slots_in_ascending_order(self, page):
        self._load(page)
        self._stub(page)
        page.eval_on_selector(RETRY_DIRTY_BTN, "el => el.click()")
        page.wait_for_function("() => window.__retried.length === 2", timeout=5000)
        assert page.evaluate("() => window.__retried") == [2, 4]


class TestMarqueeSelection:
    def test_shift_drag_across_cards_selects_them(self, page):
        """按住 Shift 并在卡片上方拖拽可框选多张卡片，且不会误开 Lightbox。"""
        page.evaluate("() => { window.__lb = 0; window.openLightbox = () => window.__lb++; }")

        box1 = page.eval_on_selector("#frame-slot-1", "el => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; }")
        box3 = page.eval_on_selector("#frame-slot-3", "el => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; }")

        # 按住 Shift 从卡片 1 拖拽到卡片 3
        page.keyboard.down("Shift")
        page.mouse.move(box1["x"] + 5, box1["y"] + 5)
        page.mouse.down()
        page.mouse.move(box3["x"] + box3["w"] - 5, box3["y"] + box3["h"] - 5, steps=5)
        page.mouse.up()
        page.keyboard.up("Shift")

        selected = page.evaluate("() => Array.from(slotToolbarState.image.selected).sort((a, b) => a - b)")
        assert 1 in selected and 2 in selected and 3 in selected, "框选应包含槽位 1、2、3: %s" % selected
        assert page.evaluate("() => window.__lb") == 0, "拖拽结束时不应误弹出 Lightbox"

    def test_drag_starting_outside_grid_selects_cards(self, page):
        """从卡片外围/顶部空白区域拖起，也能正常框选到卡片。"""
        box1 = page.eval_on_selector("#frame-slot-1", "el => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; }")
        box2 = page.eval_on_selector("#frame-slot-2", "el => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; }")

        # 从卡片外围上方拖拽到卡片 2
        page.mouse.move(box1["x"] - 20, box1["y"] - 15)
        page.mouse.down()
        page.mouse.move(box2["x"] + box2["w"] + 10, box2["y"] + box2["h"] + 10)
        page.mouse.up()

        selected = page.evaluate("() => Array.from(slotToolbarState.image.selected).sort((a, b) => a - b)")
        assert 1 in selected and 2 in selected, "外围拖拽应框选到卡片 1 和 2: %s" % selected

    def test_click_card_still_opens_lightbox(self, page):
        """单纯点击卡片图片（未拖拽）应正常打开 Lightbox。"""
        page.evaluate("() => { window.__lb = 0; window.openLightbox = () => window.__lb++; }")
        page.eval_on_selector("#frame-slot-1 img", "el => el.click()")
        assert page.evaluate("() => window.__lb") == 1, "单纯点击卡片图片应触发打开 Lightbox"

    def test_shift_drag_adds_to_existing_selection(self, page):
        """按住 Shift 框选可在已有选择基础上累加。"""
        page.eval_on_selector("#frame-slot-1 .slot-select-box", "el => el.click()")
        assert page.evaluate("() => Array.from(slotToolbarState.image.selected)") == [1]

        # 按住 Shift 框选第 3 拍
        box3 = page.eval_on_selector("#frame-slot-3", "el => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; }")
        page.keyboard.down("Shift")
        page.mouse.move(box3["x"] + 5, box3["y"] + 5)
        page.mouse.down()
        page.mouse.move(box3["x"] + box3["w"] - 5, box3["y"] + box3["h"] - 5)
        page.mouse.up()
        page.keyboard.up("Shift")

        selected = page.evaluate("() => Array.from(slotToolbarState.image.selected).sort((a, b) => a - b)")
        assert selected == [1, 3]

    def test_click_empty_space_clears_selection(self, page):
        """在网格空白区域单击应清空选中状态。"""
        page.eval_on_selector("#frame-slot-1 .slot-select-box", "el => el.click()")
        page.eval_on_selector("#frame-slot-2 .slot-select-box", "el => el.click()")
        assert page.evaluate("() => slotToolbarState.image.selected.size") == 2

        # 点击网格空白区域
        page.eval_on_selector("#frames-grid", "el => el.dispatchEvent(new MouseEvent('click', { bubbles: true }))")
        assert page.evaluate("() => slotToolbarState.image.selected.size") == 0

    def test_shift_click_checkbox_selects_range(self, page):
        """Shift + 点击勾选框支持区间连选。"""
        page.eval_on_selector("#frame-slot-2 .slot-select-box", "el => el.click()")
        assert page.evaluate("() => Array.from(slotToolbarState.image.selected)") == [2]

        # Shift + 点击第 5 拍勾选框
        page.click("#frame-slot-5 .slot-select-box", modifiers=["Shift"])

        selected = page.evaluate("() => Array.from(slotToolbarState.image.selected).sort((a, b) => a - b)")
        assert selected == [2, 3, 4, 5]


