"""Opening the Flow upload menu must not click its own CDK backdrop for 30 s."""

from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_helpers as H


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Elements:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class _Menu:
    def __init__(self, visible=True, upload=True, icon_only=False):
        self.visible = visible
        self.upload = upload
        self.icon_only = icon_only

    def is_visible(self):
        return self.visible

    def locator(self, selector):
        assert "Upload" in selector and "上传" in selector
        matches = self.upload and (not self.icon_only or "[aria-label='Upload media']" in selector)
        return _Elements([SimpleNamespace(is_visible=lambda: True)] if matches else [])


class _Trigger:
    def __init__(self, page):
        self.page = page

    def get_attribute(self, name, **kwargs):
        assert name == "aria-controls"
        return self.page.controlled_id

    def click(self, **kwargs):
        self.page.clicks.append(kwargs)
        assert kwargs == {"timeout": 2500}
        if self.page.fail_click:
            self.page.clock.sleep(kwargs["timeout"] / 1000)
            raise TimeoutError("CDK backdrop intercepts pointer events")
        if self.page.open_on_click:
            self.page.open_at = self.page.clock.now + self.page.delay


class _Page:
    def __init__(self, clock):
        self.clock = clock
        self.url = "https://flow.google.com/project/f0794a42-34a5-4f2c-af47-28c742dea056"
        self.controlled_id = "mat-menu-panel-0"
        self.open_at = None
        self.delay = 0.0
        self.open_on_click = True
        self.fail_click = False
        self.trigger_at = 0.0
        self.other_overlay = False
        self.other_upload_menu = False
        self.asset_picker = False
        self.mobile_picker = False
        self.clicks = []
        self.escapes = 0
        self.located = 0
        self.pending = False
        self.reloads = 0
        self.trigger_after_reload = None
        self.keyboard = SimpleNamespace(press=self.press)

    def reload(self, **kwargs):
        assert kwargs == {"timeout": 30000, "wait_until": "domcontentloaded"}
        self.reloads += 1
        if self.trigger_after_reload is not None:
            self.trigger_at = self.clock.now + self.trigger_after_reload

    def press(self, key):
        assert key == "Escape"
        self.escapes += 1
        self.other_overlay = False
        self.other_upload_menu = False

    def find_trigger(self):
        self.located += 1
        if self.clock.now < self.trigger_at:
            return None
        return _Trigger(self)

    def locator(self, selector):
        ready = self.open_at is not None and self.clock.now >= self.open_at
        if selector == "flow-pending-tile":
            return _Elements([object()] if self.pending else [])
        if "cdk-overlay-transparent-backdrop" in selector:
            return _Elements([object()] if self.other_overlay or ready else [])
        if selector.startswith("[id="):
            assert '"mat-menu-panel-0"' in selector
            return _Elements([_Menu()] if ready else [])
        assert selector == H._CANVAS_UPLOAD_MENU_SELECTOR
        if self.asset_picker:
            # Desktop ingredient picker: a CDK pane without role=menu/dialog.
            supported = "flow-add-menu-popover-content" in selector
            return _Elements([_Menu()] if ready and supported else [])
        if self.mobile_picker:
            return _Elements([_Menu(icon_only=True)] if ready else [])
        return _Elements([_Menu()] if ready or self.other_upload_menu else [])


@pytest.fixture
def page(monkeypatch):
    clock = _Clock()
    page = _Page(clock)
    monkeypatch.setattr(H, "log", lambda *a, **k: None)
    monkeypatch.setattr(H, "time", clock)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    monkeypatch.setattr(H, "_find_add2_btn", lambda page: page.find_trigger())
    return page


def test_open_media_menu_reuses_controlled_upload_without_clicking_backdrop(page):
    page.open_at = 0

    assert H._open_canvas_upload_menu(page) is True
    assert page.clicks == []
    assert page.escapes == 0
    assert page.clock.now == 0


def test_other_menu_is_not_accepted_and_escape_relocates_trigger(page):
    page.other_upload_menu = True
    page.other_overlay = True

    assert H._open_canvas_upload_menu(page) is True
    assert page.escapes == 1
    assert page.located == 2
    assert page.clicks == [{"timeout": 2500}]


def test_legacy_create_menu_without_aria_controls_can_be_reused(page):
    page.controlled_id = ""
    page.open_at = 0

    assert H._open_canvas_upload_menu(page) is True
    assert page.clicks == []


def test_click_waits_for_actual_upload_action(page):
    page.delay = 0.35

    assert H._open_canvas_upload_menu(page) is True
    assert page.clock.now == pytest.approx(0.4)
    assert len(page.clicks) == 1


def test_new_canvas_waits_for_delayed_upload_control(page):
    page.delay = 2.2
    assert H._open_canvas_upload_menu(page) is True
    assert len(page.clicks) == 1


def test_add_media_toolbar_can_reappear_after_credit_probe_rerender(page):
    page.trigger_at = 0.8

    assert H._open_canvas_upload_menu(page) is True
    assert page.clock.now == pytest.approx(1.0)
    assert len(page.clicks) == 1


def test_hidden_toolbar_can_reappear_after_a_minute_without_reloading_pending_video(page):
    page.pending = True
    page.trigger_at = 65.0

    assert H._open_canvas_upload_menu(page) is True
    assert page.clock.now == pytest.approx(65.0)
    assert page.reloads == 0
    assert page.clicks == [{"timeout": 2500}]


def test_quiet_project_reloads_once_then_waits_for_editor_and_trigger(page, monkeypatch):
    page.trigger_at = float("inf")
    page.trigger_after_reload = 0.5
    editor = SimpleNamespace(
        inner_text=lambda **kwargs: "", input_value=lambda **kwargs: "",
    )
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda _page: editor)
    monkeypatch.setattr(
        H, "read_prompt_reference_state",
        lambda _page: {"scope": "bar", "uuids": []},
    )

    assert H._open_canvas_upload_menu(page) is True
    assert page.reloads == 1
    assert page.clock.now == pytest.approx(20.5)
    assert page.clicks == [{"timeout": 2500}]


@pytest.mark.parametrize("pending,draft,refs,expected", [
    (True, "", [], False),
    (False, "Unsubmitted prompt", [], False),
    (False, "", ["existing-reference"], False),
    (False, "", [], True),
])
def test_reload_guard_preserves_pending_tasks_and_composer(
    page, monkeypatch, pending, draft, refs, expected,
):
    page.pending = pending
    editor = SimpleNamespace(
        inner_text=lambda **kwargs: draft,
        input_value=lambda **kwargs: draft,
    )
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda _page: editor)
    monkeypatch.setattr(
        H, "read_prompt_reference_state",
        lambda _page: {"scope": "bar", "uuids": refs},
    )

    assert H._canvas_upload_reload_safe(
        page, H.flow_project_id(page.url),
    ) is expected


def test_desktop_roleless_asset_picker_upload_is_recognized_and_reused(page):
    page.controlled_id = ""
    page.asset_picker = True
    assert H._open_canvas_upload_menu(page) is True
    assert H._open_canvas_upload_menu(page) is True
    assert len(page.clicks) == 1
    assert page.escapes == 0


def test_mobile_icon_upload_control_is_recognized(page):
    page.controlled_id = ""
    page.mobile_picker = True
    assert H._open_canvas_upload_menu(page) is True
    assert len(page.clicks) == 1


def test_intercepted_clicks_stop_after_two_short_attempts_without_force(page):
    page.fail_click = True
    page.other_overlay = True

    assert H._open_canvas_upload_menu(page) is False
    assert page.clicks == [{"timeout": 2500}, {"timeout": 2500}]
    assert page.clock.now == pytest.approx(5)
    assert page.escapes == 1


def test_silent_click_without_menu_does_not_report_upload_ready(page):
    page.open_on_click = False

    assert H._open_canvas_upload_menu(page) is False
    assert len(page.clicks) == 2
    assert page.clock.now == pytest.approx(8)


def test_cancel_during_menu_wait_propagates_without_second_click(page, monkeypatch):
    page.open_on_click = False

    def check_cancel():
        if page.clock.now >= 0.2:
            raise RuntimeError("任务已取消")

    monkeypatch.setattr(H, "_check_cancelled", check_cancel)
    with pytest.raises(RuntimeError, match="任务已取消"):
        H._open_canvas_upload_menu(page)
    assert page.clock.now == pytest.approx(0.2)
    assert len(page.clicks) == 1
