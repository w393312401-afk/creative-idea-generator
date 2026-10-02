"""Exercise the actual Canvas upload path against a local Chromium DOM.

No Flow account, network response, or generation is involved. Unlike locator
doubles, these fixtures retain hidden and unrelated matching elements, which
is what made the old page-wide Upload/input lookup fail in production.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Error, FileChooser, sync_playwright

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import selector_stats


UUID = "13408d9d-5fbf-4531-8823-8baf9cccde76"


@pytest.fixture(scope="module")
def canvas_browser():
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Local Playwright Chromium is not installed")
        # An installed browser that cannot launch is a real test failure.
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def canvas_page(canvas_browser, monkeypatch, offline_fx_video_io):
    context = canvas_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_default_timeout(1000)
    for module in (H, V):
        monkeypatch.setattr(module, "log", lambda *args, **kwargs: None)
        monkeypatch.setattr(module, "_check_cancelled", lambda: None)
        monkeypatch.setattr(module, "random_sleep", lambda *args: None)
    monkeypatch.setattr(selector_stats, "record_hit", lambda *args, **kwargs: None)
    monkeypatch.setattr(V, "_get_panel_uuids", lambda page: set())
    monkeypatch.setattr(V, "_get_panel_uuid_order", lambda page, **kwargs: [UUID])
    monkeypatch.setattr(V, "_pending_canvas_upload_count", lambda *args: 0)
    monkeypatch.setattr(V, "_make_response_handler", lambda *args, **kwargs: lambda response: None)
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def reference_image(tmp_path):
    image = tmp_path / "img_042.webp"
    image.write_bytes(b"local file selection regression fixture")
    return str(image)


def _render_picker(page, *, action=None, roleless=False, controlled=True,
                   stale_upload=False, unrelated_input=False, fail_choosers=0):
    action = action or '<button id="owned-upload">Upload</button>'
    menu_tag = "flow-add-menu-popover-content" if roleless else "div"
    menu_role = "" if roleless else 'role="menu"'
    controls = 'aria-controls="owned-menu"' if controlled else ""
    stale = '<button id="stale-upload" hidden>Upload</button>' if stale_upload else ""
    unrelated = '<input id="unrelated-input" type="file" hidden>' if unrelated_input else ""
    page.set_content(f"""
        {stale}
        {unrelated}
        <button id="add-media" aria-label="Add media menu" {controls}>Add media</button>
        <{menu_tag} id="owned-menu" {menu_role} style="display:none">
            {action}
        </{menu_tag}>
        <script>
        window.events = {{opens: 0, uploadClicks: 0, escapes: 0, files: [], unrelated: []}};
        const menu = document.getElementById('owned-menu');
        document.getElementById('add-media').onclick = () => {{
            events.opens++;
            menu.style.display = 'block';
        }};
        document.getElementById('owned-upload').onclick = () => {{
            events.uploadClicks++;
            menu.style.display = 'none';
            if (events.uploadClicks <= {fail_choosers}) return;
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = 'image/*';
            input.hidden = true;
            input.onchange = () => events.files.push(...Array.from(input.files, f => f.name));
            document.body.appendChild(input);
            input.click();
        }};
        const unrelated = document.getElementById('unrelated-input');
        if (unrelated) unrelated.onchange = () =>
            events.unrelated.push(...Array.from(unrelated.files, f => f.name));
        document.addEventListener('keydown', event => {{
            if (event.key === 'Escape') {{
                events.escapes++;
                menu.style.display = 'none';
            }}
        }});
        </script>
    """)


def _assert_one_owned_upload(page, state):
    events = page.evaluate("events")
    assert events["files"] == ["img_042.webp"]
    assert events["unrelated"] == []
    assert events["uploadClicks"] == 1
    assert state["started"] is True
    assert "failure_reason" not in state
    assert not page.locator("#owned-menu").is_visible()


def test_hidden_stale_upload_does_not_shadow_visible_owned_menu(canvas_page, reference_image):
    # The former primary selector found the hidden button; its fallback did
    # not support a role=menuitem that was not itself a button.
    _render_picker(canvas_page, stale_upload=True,
                   action='<div id="owned-upload" role="menuitem" tabindex="0">Upload</div>')
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) == UUID

    _assert_one_owned_upload(canvas_page, state)
    assert canvas_page.evaluate("events.opens") == 1


def test_unrelated_global_file_input_is_never_used(canvas_page, reference_image):
    _render_picker(canvas_page, unrelated_input=True)
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) == UUID

    _assert_one_owned_upload(canvas_page, state)
    assert canvas_page.locator("#unrelated-input").evaluate("input => input.files.length") == 0


def test_visible_upload_in_another_menu_is_never_clicked(canvas_page, reference_image):
    _render_picker(canvas_page)
    canvas_page.evaluate("""() => {
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        menu.innerHTML = '<button id="foreign-upload">Upload</button>';
        menu.querySelector('button').onclick = () => events.unrelated.push('clicked');
        document.body.prepend(menu);
    }""")
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) == UUID

    _assert_one_owned_upload(canvas_page, state)


@pytest.mark.parametrize("action", [
    '<button id="owned-upload" aria-label="Upload media"><span>arrow_upward</span></button>',
    '<button id="owned-upload">Upload media</button>',
    '<div id="owned-upload" role="menuitem" tabindex="0">上传</div>',
])
def test_roleless_picker_upload_uses_its_actual_file_chooser(canvas_page, reference_image, action):
    _render_picker(canvas_page, roleless=True, controlled=False, action=action)
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) == UUID

    _assert_one_owned_upload(canvas_page, state)


def test_chooser_timeout_reopens_menu_before_one_safe_retry(canvas_page, reference_image):
    _render_picker(canvas_page, fail_choosers=1)
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) == UUID

    events = canvas_page.evaluate("events")
    assert events["opens"] == 2
    assert events["uploadClicks"] == 2
    assert events["files"] == ["img_042.webp"]
    assert state["started"] is True
    assert "failure_reason" not in state
    assert not canvas_page.locator("#owned-menu").is_visible()


def test_repeated_chooser_timeout_stops_without_submitting_a_file(canvas_page, reference_image):
    _render_picker(canvas_page, fail_choosers=99)
    state = {}

    assert V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state) is None

    events = canvas_page.evaluate("events")
    assert events["opens"] == 2
    assert events["uploadClicks"] == 2
    assert events["files"] == []
    assert state["started"] is False
    assert state["failure_reason"]
    assert not canvas_page.locator("#owned-menu").is_visible()


def test_error_after_file_selection_never_reopens_or_uploads_again(
        canvas_page, reference_image, monkeypatch):
    _render_picker(canvas_page)
    original_set_files = FileChooser.set_files

    def select_then_fail(chooser, *args, **kwargs):
        original_set_files(chooser, *args, **kwargs)
        raise RuntimeError("file selection acknowledgement was interrupted")

    monkeypatch.setattr(FileChooser, "set_files", select_then_fail)
    state = {}

    with pytest.raises(RuntimeError, match="acknowledgement was interrupted"):
        V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state)

    events = canvas_page.evaluate("events")
    assert events["opens"] == 1
    assert events["uploadClicks"] == 1
    assert events["files"] == ["img_042.webp"]
    assert state["started"] is True
    assert not canvas_page.locator("#owned-menu").is_visible()


def test_closed_page_during_menu_lookup_propagates_to_session_recovery(
        canvas_page, reference_image):
    _render_picker(canvas_page)
    canvas_page.close()
    state = {}

    # Readiness helpers intentionally tolerate missing/detached elements. A
    # closed page must nevertheless reach the runner as a browser error,
    # instead of becoming the terminal "unable to open upload menu" failure.
    with pytest.raises(Error) as caught:
        V._upload_image_to_canvas(canvas_page, reference_image, upload_state=state)

    assert type(caught.value).__name__ == "TargetClosedError"
    assert state["started"] is False
