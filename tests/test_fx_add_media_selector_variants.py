"""The Flow canvas can expose its upload menu in either toolbar variant."""

from integrations.google_fx.services import google_fx_helpers as helpers


PROMPT_ADD = "button[aria-label='Add ingredients to the prompt box']"
PROMPT_ADD_CLASS = "button.add-menu-trigger[aria-haspopup]"
HEADER_ADD = "button[aria-label='Add media menu']"


class _Button:
    def __init__(self, visible):
        self.visible = visible

    def is_visible(self, timeout=2000):
        return self.visible


class _Matches:
    def __init__(self, buttons):
        self.buttons = buttons

    def count(self):
        return len(self.buttons)

    def nth(self, index):
        return self.buttons[index]


class _Page:
    def __init__(self, buttons):
        self.buttons = buttons
        self.probed = []

    def locator(self, selector):
        self.probed.append(selector)
        buttons = self.buttons.get(selector, [])
        return _Matches(buttons if isinstance(buttons, list) else [buttons])


def test_prompt_add_is_found_when_header_add_is_hidden():
    prompt = _Button(True)
    page = _Page({PROMPT_ADD: prompt, HEADER_ADD: _Button(False)})

    assert helpers._find_add2_btn(page) is prompt
    assert HEADER_ADD in page.probed


def test_header_add_remains_available_for_older_canvas():
    header = _Button(True)
    page = _Page({HEADER_ADD: header})

    assert helpers._find_add2_btn(page) is header
    assert PROMPT_ADD not in page.probed


def test_prompt_add_class_handles_changed_accessibility_label():
    prompt = _Button(True)
    page = _Page({PROMPT_ADD_CLASS: prompt, HEADER_ADD: _Button(False)})

    assert helpers._find_add2_btn(page) is prompt
    assert page.probed[-2:] == [PROMPT_ADD, PROMPT_ADD_CLASS]


def test_canvas_add_is_preferred_when_both_variants_are_visible():
    prompt = _Button(True)
    header = _Button(True)
    page = _Page({PROMPT_ADD: prompt, HEADER_ADD: header})

    assert helpers._find_add2_btn(page) is header
    assert PROMPT_ADD not in page.probed


def test_unrelated_dialog_and_add_icons_are_not_upload_candidates():
    page = _Page({
        "button[aria-haspopup='dialog']": _Button(True),
        "button:has(mat-icon:text('add'))": _Button(True),
    })
    assert helpers._find_add2_btn(page) is None


def test_visible_second_header_button_is_used_when_stale_copy_is_hidden():
    visible = _Button(True)
    page = _Page({HEADER_ADD: [_Button(False), visible]})

    assert helpers._find_add2_btn(page) is visible
    assert page.probed == [HEADER_ADD]
