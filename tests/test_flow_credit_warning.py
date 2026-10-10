"""Regression for the Flow composer warning and redesigned account menu."""

import pytest

from playwright.sync_api import Error, sync_playwright

from integrations.google_fx.services import google_fx_credit as credit
from integrations.google_fx.ui_selectors import UI_SELECTORS


@pytest.fixture(scope="module")
def local_browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"Local Playwright Chromium unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(local_browser, monkeypatch):
    context = local_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_default_timeout(1000)
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    monkeypatch.setattr(credit, "bring_page_to_front_if_allowed", lambda page: None)
    try:
        yield page
    finally:
        context.close()


@pytest.mark.parametrize("attribute", ["aria-label", "title"])
def test_icon_only_insufficient_credit_warning_is_detected(page, monkeypatch, attribute):
    page.set_content(f'<button {attribute}="Insufficient credits warning">warning</button>')
    monkeypatch.setattr(credit, "_deep_probe_credit_via_menu",
                        lambda *args, **kwargs: pytest.fail("explicit warning must not wait for menu probe"))

    reason = credit.detect_page_credit_exhaustion(page)

    assert "Insufficient credits warning" in reason
    assert credit.is_credit_exhausted_message(reason)
    assert credit.measured_credit_from_reason(reason) is None


@pytest.mark.parametrize("hidden", [
    'style="display:none"', 'style="visibility:hidden"',
    'style="opacity:0"', 'aria-hidden="true"',
])
@pytest.mark.parametrize("attribute", ["aria-label", "title"])
def test_hidden_credit_warning_is_not_an_account_failure(page, hidden, attribute):
    page.set_content(f'<div {hidden}><button {attribute}="Insufficient credits warning">warning</button></div>')
    assert credit.detect_page_credit_exhaustion(page) is None


@pytest.mark.parametrize("label", [
    "Upgrade for 1,000 monthly Google Flow credits", "5 credits",
])
def test_non_error_attribute_is_not_a_measured_balance(page, label):
    page.set_content(f'<button title="{label}">Upgrade</button>')
    assert credit.detect_page_credit_exhaustion(page) is None


def test_warning_button_does_not_imply_account_menu_is_open(page):
    page.set_content('<button aria-label="Insufficient credits warning">warning</button>')
    assert credit._account_menu_is_open(page) is False


@pytest.mark.parametrize("attribute", ["aria-label", "aria-description", "title"])
def test_existing_account_panel_preserves_real_credit_with_warning(page, attribute):
    page.set_content(f"""
        <button aria-label="Insufficient credits warning">warning</button>
        <div aria-label="Account settings">
          <div aria-label="Credits display">
            <a href="https://example.invalid/flow_ai_credits_page"
               {attribute}="1 Google Flow credit">1</a>
          </div>
        </div>
    """)

    reason = credit.detect_page_credit_exhaustion(page)

    assert credit.measured_credit_from_reason(reason) == 1
    assert credit._scan_menu_for_credit(page, timeout_seconds=1, poll_interval=0) == 1


def test_menu_probe_opens_account_details_despite_composer_warning(page):
    page.set_content("""
        <button aria-label="Account details"
                onclick="document.getElementById('account-panel').hidden=false; window.opens=(window.opens||0)+1">Account</button>
        <button aria-label="Insufficient credits warning">warning</button>
        <div id="account-panel" aria-label="Account settings" hidden>
          <div aria-label="Credits display">
            <a href="https://example.invalid/flow_ai_credits_page"
               aria-description="1 Google Flow credit">1</a>
          </div>
        </div>
    """)

    assert credit._read_credit_from_account_menu(page, overall_timeout_seconds=3) == 1
    assert page.evaluate("window.opens") == 1


def test_menu_probe_does_not_click_image_tiles_when_avatar_is_missing(page):
    page.set_content("""
        <button id="tile" onclick="window.tileClicks=(window.tileClicks||0)+1">
          <img src="https://lh3.googleusercontent.com/a/tile-image" alt="scene image">
        </button>
    """)

    assert credit._try_click_once(page, UI_SELECTORS["google_fx"]["account_menu_trigger"]) is False
    assert page.evaluate("window.tileClicks || 0") == 0


def test_failed_account_probe_closes_overlay_before_canvas_upload(page, monkeypatch):
    page.set_content("""
        <button aria-label="Account details" onclick="document.getElementById('overlay').hidden=false">
          Account
        </button>
        <div id="overlay" hidden>Loading account details</div>
        <script>
          document.addEventListener('keydown', event => {
            if (event.key === 'Escape') document.getElementById('overlay').hidden = true;
          });
        </script>
    """)
    monkeypatch.setattr(credit, "_wait_for_account_menu", lambda page: False)

    assert credit._read_credit_from_account_menu(page, overall_timeout_seconds=0.1) is None
    assert page.locator("#overlay").is_hidden()


def test_bare_credit_count_is_read_only_from_dedicated_balance_element(page):
    page.set_content('<div aria-label="Account settings"><span class="credits-count">1</span></div>')
    assert credit._scan_menu_for_credit(page, timeout_seconds=1, poll_interval=0) == 1
    page.set_content('<div aria-label="Account settings">1</div>')
    assert credit._scan_menu_for_credit(page, timeout_seconds=0, poll_interval=0) is None
