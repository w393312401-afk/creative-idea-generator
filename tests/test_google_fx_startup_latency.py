"""Startup must wait for actual readiness without delaying already-ready sessions."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_image as I


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(H, "time", clock)
    monkeypatch.setattr(H, "log", lambda *a, **k: None)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    return clock


def test_ready_browser_connects_without_startup_sleep(clock):
    browser = object()
    p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser))

    assert H._connect_over_cdp_with_retry(p, "ws://local-session") is browser
    assert clock.sleeps == []


def test_cold_browser_connection_retains_retry_delay(clock):
    attempts = []
    browser = object()

    def connect(*args, **kwargs):
        attempts.append(clock.now)
        if len(attempts) == 1:
            raise ConnectionRefusedError("port not ready")
        return browser

    p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    assert H._connect_over_cdp_with_retry(p, "ws://local-session") is browser
    assert attempts == [0.0, 2.0]


class _ProjectPage:
    """Like sync Playwright, URL changes only when its event loop is pumped."""

    def __init__(self, clock, navigation_delay):
        self.url = "https://flow.google.com/"
        self.clock = clock
        self.navigation_delay = navigation_delay
        self.clicked = False
        self.pumps = []

    def locator(self, selector):
        button = SimpleNamespace(
            is_visible=lambda **kwargs: True,
            click=lambda **kwargs: setattr(self, "clicked", True),
        )
        return SimpleNamespace(first=button)

    def wait_for_timeout(self, milliseconds):
        assert self.clicked
        self.pumps.append(milliseconds)
        self.clock.now += milliseconds / 1000
        if self.clock.now >= self.navigation_delay:
            self.url = "https://flow.google.com/project/created"


@pytest.mark.parametrize("navigation_delay", [0.1, 17.0, 38.0])
def test_project_confirmation_pumps_events_and_keeps_slow_navigation_budget(
    clock, monkeypatch, navigation_delay,
):
    page = _ProjectPage(clock, navigation_delay)
    monkeypatch.setattr(H, "random_sleep", lambda *a: pytest.fail("fixed project settle delay"))

    assert H._click_new_project_button(page) is True
    assert navigation_delay <= clock.now <= navigation_delay + 0.21
    assert clock.sleeps == []
    assert page.pumps


def test_project_confirmation_cancel_propagates(clock, monkeypatch):
    page = _ProjectPage(clock, 20)

    def cancelled():
        if clock.now >= 0.2:
            raise RuntimeError("任务已取消")

    monkeypatch.setattr(H, "_check_cancelled", cancelled)
    with pytest.raises(RuntimeError, match="任务已取消"):
        H._click_new_project_button(page)
    assert clock.now == pytest.approx(0.2)


def test_new_project_waits_for_editor_instead_of_creating_another(monkeypatch):
    page = SimpleNamespace(url="https://flow.google.com/project/created")
    waits = []
    monkeypatch.setattr(H, "_find_fx_prompt_input", lambda *a, **k: None)
    monkeypatch.setattr(H, "_wait_for_fx_toolbar", lambda *a, **k: waits.append(k) or True)
    monkeypatch.setattr(H, "_click_new_project_button", lambda *a: pytest.fail("duplicate project"))

    H._prepare_fx_canvas(page, has_refs=False, require_fresh_canvas=True)
    assert len(waits) == 1


@pytest.mark.parametrize(
    "fresh, requested, actual, has_uuid, expected_wait",
    [
        (True, None, "new", True, False),
        (False, "old", "old", False, False),
        (False, "old", "replacement", True, False),
        (False, "old", "old", True, True),
    ],
)
def test_image_upload_waits_for_existing_tiles_only_when_reusing_bound_uuids(
    monkeypatch, tmp_path, fresh, requested, actual, has_uuid, expected_wait,
):
    uuid = "13408d9d-5fbf-4531-8823-8baf9cccde76"
    local = tmp_path / (f"frame_{uuid}.png" if has_uuid else "reference.png")
    local.write_bytes(b"reference")
    project = lambda name: f"https://flow.google.com/project/{name}" if name else None
    page = SimpleNamespace(url=project(actual), evaluate=lambda *a: [])
    waits, uploads = [], []
    req = SimpleNamespace(
        prompts=["one frame"], model="Nano Banana 2", ratio="9:16",
        images=[str(local)], require_fresh_canvas=fresh, project_url=project(requested),
    )
    monkeypatch.setattr(I, "sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(I, "_connect_fx_page", lambda *a, **k: (object(), page))
    monkeypatch.setattr(I, "_open_image_flow_canvas", lambda *a, **k: project(actual))
    monkeypatch.setattr(I, "_prepare_fx_canvas", lambda *a, **k: waits.append(k["has_refs"]))
    monkeypatch.setattr(I, "_verify_and_fix_fx_config", lambda *a, **k: "9:16")
    monkeypatch.setattr(I, "_find_fx_prompt_input", lambda *a, **k: object())
    monkeypatch.setattr(I, "_clear_prompt_reference_chips_image", lambda *a: None)
    monkeypatch.setattr(I, "read_prompt_bar_state", lambda *a: {"editor_empty": True})
    monkeypatch.setattr(I, "_add_flow_image_to_prompt", lambda *a: False)
    monkeypatch.setattr(I, "_upload_image_to_canvas_and_mount", lambda p, path: uploads.append(path) or uuid)
    monkeypatch.setattr(I, "_wait_for_flow_reference_ready", lambda *a, **k: pytest.fail("duplicate mount wait"))
    monkeypatch.setattr(I, "_check_cancelled", lambda: None)
    monkeypatch.setattr(I.cancel_flag, "is_cancelled", False)
    monkeypatch.setattr(I, "log", lambda *a, **k: None)

    def stop_before_generation(*args):
        raise RuntimeError("test stopped before any generation")

    monkeypatch.setattr(I, "inject_batch_image_observer", stop_before_generation)
    result = I._generate_images_batch_google_fx_single_attempt(req)

    assert result["message"] == "test stopped before any generation"
    assert waits == [expected_wait]
    assert uploads == [str(local)]
    assert result["uploaded_reference_uuids"] == {str(local): uuid}


def test_fresh_image_canvas_reuses_ready_project_list(monkeypatch):
    page = SimpleNamespace(url="https://flow.google.com/")
    monkeypatch.setattr(I, "_find_fx_prompt_input", lambda *a, **k: None)
    monkeypatch.setattr(I, "ensure_flow_workspace", lambda *a, **k: True)

    def create(target):
        target.url = "https://flow.google.com/project/new"
        return True

    monkeypatch.setattr(I, "_click_new_project_button", create)
    assert I._create_fresh_flow_canvas(page) == "https://flow.google.com/project/new"
