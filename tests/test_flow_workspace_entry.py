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


def test_workspace_recovery_is_idempotent_when_already_entered():
    page = _LandingPage(already_ready=True)
    assert ensure_flow_workspace(page, timeout_seconds=0.01) is True
    assert page.clicked_y is None


def test_unknown_page_without_entry_or_workspace_returns_false():
    page = _LandingPage()
    page.hero = _Element(page, visible=False)
    page.footer = _Element(page, visible=False)
    assert ensure_flow_workspace(page, timeout_seconds=0.01) is False


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


def test_find_or_create_page_reuses_tab_and_closes_extras(monkeypatch):
    from integrations.google_fx.utils.browser import find_or_create_page

    class DummyPage:
        def __init__(self, url):
            self.url = url
            self.closed = False
            self.brought_to_front = False
            self.navigated_to = None

        def goto(self, url, timeout=None):
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
    assert p_flow.brought_to_front is True

    # 2. 只有空白页时，应该直接复用空白页进行跳转，而不额外新建标签页，且闭合多余标签
    p_blank2 = DummyPage("about:blank")
    ctx2 = DummyContext([p_blank2])

    selected2 = find_or_create_page(ctx2, "/fx/tools/flow", fallback_url="https://labs.google/fx/tools/flow")
    assert selected2 == p_blank2
    assert selected2.navigated_to == "https://labs.google/fx/tools/flow"
    assert len(ctx2.pages) == 1  # 没有产生新页面


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
