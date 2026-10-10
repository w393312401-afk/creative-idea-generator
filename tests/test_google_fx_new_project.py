import pytest

from integrations.google_fx.services import google_fx_helpers as helpers


class _Keyboard:
    def __init__(self):
        self.escapes = 0

    def press(self, key):
        if key == "Escape":
            self.escapes += 1


class _Button:
    def __init__(self, page, target_url=None):
        self.page = page
        self.target_url = target_url
        self.clicks = 0

    def is_visible(self, timeout=None):
        return True

    def click(self, timeout=None):
        self.clicks += 1
        if self.target_url:
            self.page.url = self.target_url


class _Locator:
    def __init__(self, button=None):
        self.button = button

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self.button is not None

    def click(self, timeout=None):
        if self.button is None:
            raise RuntimeError("locator did not match")
        self.button.click(timeout=timeout)


class _Page:
    def __init__(self, selectors):
        self.url = "https://labs.google/fx/tools/flow"
        self.selectors = selectors
        self.keyboard = _Keyboard()

    def locator(self, selector):
        return _Locator(self.selectors.get(selector))


@pytest.mark.parametrize(
    ("language", "text_selector"),
    [
        ("zh", "button:has-text('新建项目')"),
        ("en", "button:has-text('New project')"),
    ],
)
def test_new_project_is_confirmed_in_chinese_and_english(monkeypatch, language, text_selector):
    page = _Page({})
    real = _Button(page, f"https://labs.google/fx/tools/flow/project/{language}-123")
    decoy = _Button(page)  # 项目内同样使用 add_2 的“创建媒体”按钮
    page.selectors[text_selector] = real
    page.selectors["button:not([aria-haspopup]):has(i.google-symbols:text-is('add_2'))"] = decoy
    monkeypatch.setattr(helpers, "random_sleep", lambda *_: None)

    assert helpers._click_new_project_button(page, confirm_timeout=0) is True
    assert real.clicks == 1
    assert decoy.clicks == 0
    assert page.url.endswith(f"/project/{language}-123")


@pytest.mark.parametrize(
    "text_selector",
    ["button:has-text('新建项目')", "button:has-text('New project')"],
)
def test_false_click_is_rejected_in_both_languages(monkeypatch, text_selector):
    page = _Page({})
    decoy = _Button(page)
    page.selectors[text_selector] = decoy
    monkeypatch.setattr(helpers, "random_sleep", lambda *_: None)

    assert helpers._click_new_project_button(page, confirm_timeout=0) is False
    assert decoy.clicks == 1
    assert "/project/" not in page.url
    assert page.keyboard.escapes == 1
