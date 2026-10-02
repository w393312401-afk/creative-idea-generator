import time

import pytest

from integrations.google_fx.utils.browser import (
    BrowserSessionClosedError,
    ensure_flow_workspace,
)


class _Element:
    def __init__(self, page, y=0, visible=True, on_click=None, text='', dialog=False):
        self.page = page
        self.y = y
        self.visible = visible
        self.on_click = on_click
        self.text = text
        self.dialog = dialog

    def is_visible(self, timeout=None):
        return self.visible

    def is_enabled(self):
        return True

    def bounding_box(self):
        return {'x': 0, 'y': self.y, 'width': 300, 'height': 60}

    def click(self, timeout=None):
        self.page.clicked_y = self.y
        if self.on_click:
            self.on_click()

    def inner_text(self, timeout=None):
        return self.text

    def locator(self, selector):
        return self.page.dialog_locator(selector) if self.dialog else _Locator()

    def evaluate(self, script):
        self.page.scrolled = True


class _Locator:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class _LandingPage:
    def __init__(self, already_ready=False, onboarding=None, language='en'):
        self.ready = already_ready
        self.clicked_y = None
        self.onboarding = onboarding
        self.language = language
        self.scrolled = False
        self.hero = _Element(self, y=700, on_click=lambda: setattr(self, 'ready', True))
        self.footer = _Element(self, y=3400, on_click=lambda: setattr(self, 'ready', True))

    def locator(self, selector):
        if selector == "[role='dialog']" and self.onboarding:
            text = {
                ('preferences', 'en'): 'Use and shape AI tools for creativity',
                ('preferences', 'zh'): '使用和塑造创意 AI 工具',
                ('preferences', 'id'): 'Gunakan dan bentuk alat AI untuk kreativitas',
                ('privacy', 'en'): 'Review our privacy notice',
                ('privacy', 'zh'): '查看我们的隐私权声明',
                ('privacy', 'id'): 'Tinjau kebijakan privasi kami',
            }[(self.onboarding, self.language)]
            return _Locator([_Element(self, text=text, dialog=True)])
        if selector == "button:has-text('Create with Google Flow')" and not self.ready:
            return _Locator([self.hero, self.footer])
        if self.ready and selector == "button:has-text('New project')":
            return _Locator([_Element(self, y=100)])
        return _Locator()

    def dialog_locator(self, selector):
        labels = {
            ('preferences', 'en'): 'Next', ('preferences', 'zh'): '下一步',
            ('preferences', 'id'): 'Berikutnya', ('privacy', 'en'): 'Continue',
            ('privacy', 'zh'): '继续', ('privacy', 'id'): 'Lanjutkan',
        }
        label = labels.get((self.onboarding, self.language))
        if label and label in selector:
            def advance():
                self.onboarding = 'privacy' if self.onboarding == 'preferences' else None
                if self.onboarding is None:
                    self.ready = True
            return _Locator([_Element(self, on_click=advance)])
        return _Locator()


def test_landing_page_clicks_the_top_visible_cta_and_waits_for_workspace():
    page = _LandingPage()
    assert ensure_flow_workspace(page, timeout_seconds=0.01) is True
    assert page.clicked_y == 700


def test_landing_page_opens_target_blank_cta_in_the_existing_tab():
    class _LinkElement(_Element):
        def evaluate(self, script):
            if "closest('a[href]')" in script:
                return "https://labs.google/fx/tools/flow"
            return None

        def click(self, timeout=None):
            raise AssertionError("target=_blank CTA must not be clicked")

    class _TargetBlankLandingPage(_LandingPage):
        def __init__(self):
            super().__init__()
            self.navigated_to = None
            self.hero = _LinkElement(self, y=700)

        def goto(self, url, timeout=None, **kwargs):
            self.navigated_to = url
            self.ready = True

    page = _TargetBlankLandingPage()

    assert ensure_flow_workspace(page, timeout_seconds=0.01) is True
    assert page.navigated_to == "https://labs.google/fx/tools/flow"
    assert page.clicked_y is None


def test_workspace_recovery_is_idempotent_when_already_entered():
    page = _LandingPage(already_ready=True)
    assert ensure_flow_workspace(page, timeout_seconds=0.01) is True
    assert page.clicked_y is None


def test_unknown_page_without_entry_or_workspace_returns_false():
    page = _LandingPage()
    page.hero = _Element(page, visible=False)
    page.footer = _Element(page, visible=False)
    assert ensure_flow_workspace(page, timeout_seconds=0.01) is False


def test_flow_project_crashed_recovers_via_back_button():
    class _CrashedPage:
        def __init__(self):
            self.crashed = True
            self.ready = False
            self.clicked = False

        def locator(self, selector):
            if "Back to projects" in selector and self.crashed:
                def on_click():
                    self.clicked = True
                    self.crashed = False
                    self.ready = True
                return _Locator([_Element(self, on_click=on_click)])
            if self.ready and selector == "button:has-text('New project')":
                return _Locator([_Element(self, y=100)])
            return _Locator()

        def inner_text(self, selector, timeout=None):
            return "Something went wrong." if self.crashed else ""

    page = _CrashedPage()
    assert ensure_flow_workspace(page, timeout_seconds=2) is True
    assert page.clicked is True
    assert page.ready is True


def test_flow_project_crashed_recovers_via_goto_fallback():
    class _CrashedPageNoBtn:
        def __init__(self):
            self.crashed = True
            self.ready = False
            self.navigated_to = None

        def locator(self, selector):
            if self.ready and selector == "button:has-text('New project')":
                return _Locator([_Element(self, y=100)])
            return _Locator()

        def inner_text(self, selector, timeout=None):
            return "Something went wrong." if self.crashed else ""

        def goto(self, url, timeout=None, **kwargs):
            self.navigated_to = url
            self.crashed = False
            self.ready = True

    page = _CrashedPageNoBtn()
    assert ensure_flow_workspace(page, timeout_seconds=2) is True
    # 断言对着常量而不是字面量：Flow 2026-09-05 搬到 flow.google.com，
    # 当时到处写死的地址就是这么集体失效的，用例不该再复制一份。
    from integrations.google_fx.utils.browser import FLOW_HOME_URL
    assert page.navigated_to == FLOW_HOME_URL
    assert page.ready is True


def test_closed_browser_fails_immediately_instead_of_waiting_for_navigation_timeout():
    class _Browser:
        def is_connected(self):
            return False

    page = _LandingPage()
    page.context = type('Context', (), {'browser': _Browser()})()
    page.is_closed = lambda: False
    started = time.monotonic()

    with pytest.raises(BrowserSessionClosedError, match='已关闭'):
        ensure_flow_workspace(page, timeout_seconds=25)

    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize('language', ['en', 'zh', 'id'])
def test_onboarding_is_completed_in_chinese_english_and_current_indonesian(language):
    page = _LandingPage(onboarding='preferences', language=language)
    assert ensure_flow_workspace(page, timeout_seconds=3) is True
    assert page.onboarding is None
    assert page.scrolled is True


@pytest.mark.parametrize("silent", [True, False])
def test_find_or_create_page_reuses_tab_and_closes_extras(monkeypatch, silent):
    from integrations.google_fx.utils.browser import find_or_create_page
    monkeypatch.setattr(
        "integrations.google_fx.utils.browser.get_runtime_adspower_silent_mode", lambda: silent)
    monkeypatch.setattr(
        "integrations.google_fx.utils.browser.get_runtime_adspower_headless", lambda: False)

    class DummyPage:
        def __init__(self, url):
            self.url = url
            self.closed = False
            self.brought_to_front = False
            self.navigated_to = None

        def goto(self, url, timeout=None, **kwargs):
            self.navigated_to = url
            self.url = url

        def close(self):
            self.closed = True

        def bring_to_front(self):
            self.brought_to_front = True

    class DummyContext:
        def __init__(self, pages):
            self.pages = pages

        def new_page(self):
            p = DummyPage("about:blank")
            self.pages.append(p)
            return p

    # 1. 多个标签页（含 Flow 标签页）时，应该复用 Flow 标签页并关闭其他多余标签页
    p_blank = DummyPage("about:blank")
    p_flow = DummyPage("https://labs.google/fx/tools/flow")
    p_extra = DummyPage("https://example.com")
    ctx1 = DummyContext([p_blank, p_flow, p_extra])

    selected1 = find_or_create_page(ctx1, "/fx/tools/flow")
    assert selected1 == p_flow
    assert p_flow.closed is False
    assert p_blank.closed is True
    assert p_extra.closed is True
    assert p_flow.brought_to_front is (not silent)

    # 2. 只有空白页时，应该直接复用空白页进行跳转，而不额外新建标签页，且闭合多余标签
    p_blank2 = DummyPage("about:blank")
    ctx2 = DummyContext([p_blank2])

    selected2 = find_or_create_page(ctx2, "/fx/tools/flow", fallback_url="https://labs.google/fx/tools/flow")
    assert selected2 == p_blank2
    assert selected2.navigated_to == "https://labs.google/fx/tools/flow"
    assert len(ctx2.pages) == 1  # 没有产生新页面


def test_find_or_create_page_handles_frame_detached():
    """测试在跳转 fallback_url 触发 Frame has been detached 时能安全恢复页面而不挂掉。"""
    from integrations.google_fx.utils.browser import find_or_create_page

    class DetachedPage:
        def __init__(self):
            self.url = "about:blank"

        def goto(self, url, timeout=None, **kwargs):
            self.url = "https://labs.google/fx/tools/flow"
            raise Exception("Page.goto: Frame has been detached.")

        def bring_to_front(self):
            pass

    class DummyContext:
        def __init__(self):
            self.pages = [DetachedPage()]

    ctx = DummyContext()
    page = find_or_create_page(ctx, "/fx/tools/flow", fallback_url="https://labs.google/fx/tools/flow", auto_login=False)
    assert page.url == "https://labs.google/fx/tools/flow"


def test_find_or_create_page_attempts_login_immediately(monkeypatch):
    """所有 FX 服务共用入口：拿到登录页时不等后续控件超时，立刻自动登录。"""
    from integrations.google_fx.utils import browser as browser_utils

    calls = []
    monkeypatch.setattr(browser_utils, "is_google_login_page", lambda page: True)
    monkeypatch.setattr(
        browser_utils, "attempt_auto_login",
        lambda page, **kwargs: calls.append((page, kwargs)) or True,
    )

    class DummyPage:
        url = "https://accounts.google.com/v3/signin/identifier"

        def bring_to_front(self):
            pass

    class DummyContext:
        pages = [DummyPage()]

    page = browser_utils.find_or_create_page(
        DummyContext(), "accounts.google.com", user_id="fx-user-1",
        auto_login_timeout_seconds=25, context_label="测试浏览器启动")

    assert calls == [(page, {
        "user_id": "fx-user-1",
        "context_label": "测试浏览器启动",
        "cancel_check": None,
        "timeout_seconds": 25,
    })]


def test_find_or_create_page_can_disable_eager_login(monkeypatch):
    """测试登录入口需要自己返回详细结果，可以显式关闭公共入口的前置尝试。"""
    from integrations.google_fx.utils import browser as browser_utils

    monkeypatch.setattr(browser_utils, "is_google_login_page", lambda page: True)
    monkeypatch.setattr(
        browser_utils, "attempt_auto_login",
        lambda *args, **kwargs: pytest.fail("auto_login=False 时不应自动登录"),
    )

    class DummyPage:
        url = "https://accounts.google.com/v3/signin/identifier"

        def bring_to_front(self):
            pass

    class DummyContext:
        pages = [DummyPage()]

    browser_utils.find_or_create_page(
        DummyContext(), "accounts.google.com", auto_login=False)


def test_ensure_flow_workspace_resets_deadline_on_auto_login_success(monkeypatch):
    """当自动登录耗时较长（例如 25s）但成功时，ensure_flow_workspace 必须重置 deadline，
    给新进入的工作台页面预留充分的加载时间，而不是在登录完成瞬间直接因旧 deadline 超时判失败。"""
    from integrations.google_fx.utils import browser as browser_utils

    login_states = [True, False]
    workspace_states = [False, False, True]

    def mock_is_login(page):
        return login_states.pop(0) if login_states else False

    def mock_workspace_ready(page):
        return workspace_states.pop(0) if workspace_states else True

    auto_login_called = []
    def mock_auto_login(page, **kwargs):
        auto_login_called.append(True)
        return True

    monkeypatch.setattr(browser_utils, "is_google_login_page", mock_is_login)
    monkeypatch.setattr(browser_utils, "wait_for_login_redirect", lambda *a, **kw: False)
    monkeypatch.setattr(browser_utils, "_flow_workspace_ready", mock_workspace_ready)
    monkeypatch.setattr(browser_utils, "flow_onboarding_required", lambda page: False)
    monkeypatch.setattr(browser_utils, "_flow_project_crashed", lambda page: False)
    monkeypatch.setattr(browser_utils, "attempt_auto_login", mock_auto_login)

    class DummyPage:
        def wait_for_load_state(self, *a, **kw):
            pass

    page = DummyPage()
    # 传入极短的 initial timeout (0.1s)，验证 auto_login 成功后重置 deadline 让 workspace 成功判定
    result = ensure_flow_workspace(page, timeout_seconds=0.1, user_id="test-user")
    assert result is True
    assert len(auto_login_called) == 1



# ── 2026-08-17：崩溃页误判把健康画布"恢复"回项目列表，单画布复用整条失效 ──
# 误判被 ensure_flow_workspace 当真之后，页面被退回项目列表，绑定的画布就此丢失；
# 上层找不到编辑器只能新建一块空白画布，于是每帧一个新 project_url，参考图 UUID
# 在新画布上挂不到，只能一张张重新上传。

def _crash_probe_page(*, editor_visible, body_text="", back_button=False,
                      arrow_back=False):
    class _Page:
        def __init__(self):
            self.clicked_y = None

        def locator(self, selector):
            if selector in ("textarea", "[contenteditable='true']") and editor_visible:
                return _Locator([_Element(self)])
            if "arrow_back" in selector and arrow_back:
                return _Locator([_Element(self)])
            if "Back to projects" in selector and back_button:
                return _Locator([_Element(self)])
            return _Locator()

        def inner_text(self, selector, timeout=None):
            return body_text

    return _Page()


def test_healthy_canvas_with_header_back_arrow_is_not_a_crash():
    """页头的通用 arrow_back 图标按钮在正常画布上一直都在，不能当崩溃证据。"""
    from integrations.google_fx.utils.browser import _flow_project_crashed

    page = _crash_probe_page(editor_visible=True, arrow_back=True)
    assert _flow_project_crashed(page) is False


def test_failed_tile_text_on_a_working_canvas_is_not_a_crash():
    """画布上一张 Failed 卡片的正文同样是 'Something went wrong'，不能整页判崩。"""
    from integrations.google_fx.utils.browser import _flow_project_crashed

    page = _crash_probe_page(
        editor_visible=True,
        body_text="Frame 3 Something went wrong. Try again",
    )
    assert _flow_project_crashed(page) is False


def test_real_crash_page_is_still_detected():
    from integrations.google_fx.utils.browser import _flow_project_crashed

    assert _flow_project_crashed(
        _crash_probe_page(editor_visible=False, back_button=True)
    ) is True
    assert _flow_project_crashed(
        _crash_probe_page(editor_visible=False, body_text="Something went wrong.")
    ) is True


# ── 新建项目失败 = 落回旧画布，历史卡片必须照样拍快照 ────────────────────
# 2026-09-06 复盘：整份 server.log 里"新建项目成功"出现 0 次，tasks/results 里连续
# 15 个任务的 google_fx_project_url 是同一个 id——新建项目其实一直在失败，每个任务
# 都堆在上一次那张画布上。而 _snapshot_preexisting_tiles 当时只在"绑定画布"时才拍
# 快照，理由是"自建的新项目画布本来就是空的"——这个前提在新建失败时根本不成立，
# 于是历史卡片既没被排除在认领之外，也没人知道画布是脏的。

def _dirty_runner():
    from integrations.google_fx.services import google_fx_video as V
    return V._ChunkRunner(total_reqs=1, chunk_start=0, chunk=[], all_slices={},
                          on_progress=None, cancel_check=None)


def test_failed_new_project_marks_canvas_dirty_and_snapshots_history(monkeypatch):
    from integrations.google_fx.services import google_fx_video as V

    runner = _dirty_runner()
    runner.canvas_is_bound = False
    runner.canvas_is_dirty = True  # _prepare_page 在新建项目失败时置位
    monkeypatch.setattr(V, "_scan_canvas_tiles", lambda page: [
        {"tileId": "old_1", "originalTileId": "old_1"},
        {"tileId": "old_2", "originalTileId": "old_2"},
    ])

    runner._snapshot_preexisting_tiles(object())

    assert runner.preexisting_tile_ids == {"old_1", "old_2"}, \
        "沿用旧画布时历史卡片必须排除在认领之外，否则重试轮会认领别的任务的成片"


def test_clean_self_made_canvas_still_skips_the_snapshot(monkeypatch):
    from integrations.google_fx.services import google_fx_video as V

    runner = _dirty_runner()
    runner.canvas_is_bound = False
    runner.canvas_is_dirty = False
    monkeypatch.setattr(V, "_scan_canvas_tiles",
                        lambda page: pytest.fail("干净画布不该白扫一遍"))

    runner._snapshot_preexisting_tiles(object())
    assert runner.preexisting_tile_ids == set()


# ── 新建项目的确认窗口不能卡在 navigation 落地前一瞬间 ────────────────────
# 2026-09-06 只读探针实测：从 flow.google.com 首页点 button.new-project-button 到
# URL 变成 /project/<新 id>，耗时**略超过 15 秒**。原来 confirm_timeout=15.0 每次
# 都在落地前一瞬间到期 → 这个函数从来没返回过 True（server.log 里"新建项目成功"
# 出现 0 次），而项目其实每次都建出来了。调用方于是打出"画布可能残留历史卡片"的
# 假警报，帧链靠 `or _find_fx_prompt_input(...)` 兜底才没出事。

class _LateNavPage:
    """点击后第 N 次读 url 才变成新项目页——模拟落地慢于确认窗口。"""

    def __init__(self, flip_after_reads):
        self._reads = 0
        self._flip = flip_after_reads
        self._clicked = False

    @property
    def url(self):
        self._reads += 1
        if self._clicked and self._reads >= self._flip:
            return "https://flow.google.com/project/91049bf9-e2c6-4ef7-ade0-e2eb379083bf"
        return "https://flow.google.com/"

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return True

    def click(self, **kwargs):
        self._clicked = True

    class _Kb:
        @staticmethod
        def press(_k):
            return None

    keyboard = _Kb()


def test_new_project_confirm_window_is_not_shorter_than_measured_navigation():
    """契约：确认窗口必须明显宽于实测的 ~15s，否则每次成功都被判成失败。"""
    import inspect
    from integrations.google_fx.services import google_fx_helpers as H

    sig = inspect.signature(H._click_new_project_button)
    assert sig.parameters["confirm_timeout"].default >= 30.0


def test_navigation_landing_just_after_the_deadline_still_counts_as_success(monkeypatch):
    from integrations.google_fx.services import google_fx_helpers as H

    page = _LateNavPage(flip_after_reads=4)
    monkeypatch.setattr(H, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(H.time, "sleep", lambda *_a: None)

    # confirm_timeout=0：主循环一轮就到期，只剩收尾那一次宽限复查
    assert H._click_new_project_button(page, confirm_timeout=0.0) is True
