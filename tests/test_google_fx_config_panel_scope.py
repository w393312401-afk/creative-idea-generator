"""The Flow settings overlay must not be confused with other open menus."""

import pytest
from playwright.sync_api import Error, sync_playwright

from integrations.google_fx.services import google_fx_helpers as H


pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as error:
            pytest.skip(f"Local Playwright Chromium unavailable: {error}")
        context = browser.new_context()
        context.route("**/*", lambda route: route.abort())
        tab = context.new_page()
        tab.set_default_timeout(1000)
        try:
            yield tab
        finally:
            context.close()
            browser.close()


def _settings_html(initially_open):
    hidden = "" if initially_open else "hidden"
    return f"""
        <button id="summary">Video · 360p · 10s crop_9_16 x1</button>
        <div id="upload" role="menu" data-state="open" class="cdk-overlay-pane">
          <button role="menuitem">Upload</button>
          <button role="menuitem">Omni 1.1 Flash</button>
        </div>
        <div id="settings" class="cdk-overlay-pane" {hidden}>
          <flow-prompt-box-settings class="settings-content-overlay">
            <div class="settings-content">
              <flow-toggles aria-label="Mode"><button role="radio">Video</button></flow-toggles>
              <button aria-label="Select model family" aria-haspopup="menu">
                <span class="model-select-trigger-content">Omni 1.1 Flash</span>
                <mat-icon>arrow_drop_down</mat-icon>
              </button>
            </div>
          </flow-prompt-box-settings>
        </div>
    """


def test_settings_panel_ignores_first_unrelated_visible_menu(page):
    page.set_content(_settings_html(initially_open=True))
    panel = H._get_open_fx_config_panel(page, page.locator("#summary"))
    assert panel is not None
    assert panel.get_attribute("id") == "settings"
    model = H._find_fx_model_dropdown(page, scope=panel, target_model="Omni Flash")
    assert model is not None
    assert "Omni 1.1 Flash" in model.inner_text()


def test_config_reopens_settings_after_dismissing_upload_menu(page, monkeypatch):
    page.set_content(_settings_html(initially_open=False))
    page.evaluate("""() => {
        window.settingsClicks = 0;
        document.querySelector('#summary').onclick = () => {
            window.settingsClicks++;
            document.querySelector('#settings').hidden = false;
        };
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') document.querySelector('#upload').hidden = true;
        });
    }""")
    monkeypatch.setattr(H, "random_sleep", lambda *args: None)
    assert H._get_open_fx_config_panel(page, page.locator("#summary")) is None

    info = H.fix_fx_config(
        page, page.locator("#summary"),
        {"model": False, "mode": True, "orientation": True, "count": True},
        model="Omni Flash", want_video=True,
    )

    assert page.evaluate("window.settingsClicks") == 1
    assert page.locator("#upload").is_hidden()
    assert info["resolved_model_text"].startswith("Omni 1.1 Flash")
    assert "model" in info["resolved_keys"]


def test_open_settings_shell_waits_for_delayed_model_without_toggling_closed(page, monkeypatch):
    page.set_content("""
        <button id="summary">Video · 360p · 10s crop_9_16 x1</button>
        <div id="settings" class="cdk-overlay-pane">
          <flow-prompt-box-settings class="settings-content-overlay">
            <div class="settings-content">
              <flow-toggles aria-label="Mode"><button role="radio">Video</button></flow-toggles>
              <div id="model-mount"></div>
            </div>
          </flow-prompt-box-settings>
        </div>
    """)
    page.evaluate("""() => {
        window.settingsClicks = 0;
        document.querySelector('#summary').onclick = () => {
            window.settingsClicks++;
            document.querySelector('#settings').hidden = true;
        };
        setTimeout(() => {
            document.querySelector('#model-mount').innerHTML =
                '<button aria-label="Select model family" aria-haspopup="menu">' +
                'Omni 1.1 Flash <span>arrow_drop_down</span></button>';
        }, 150);
    }""")
    monkeypatch.setattr(H, "random_sleep", lambda *args: None)

    info = H.fix_fx_config(
        page, page.locator("#summary"),
        {"model": False, "mode": True, "orientation": True, "count": True},
        model="Omni Flash", want_video=True,
    )

    assert page.evaluate("window.settingsClicks") == 0
    assert info["resolved_model_text"].startswith("Omni 1.1 Flash")
    assert "model" in info["resolved_keys"]


def test_settings_trigger_retries_only_when_panel_stays_closed(page, monkeypatch):
    page.set_content(_settings_html(initially_open=False))
    page.evaluate("""() => {
        window.settingsClicks = 0;
        document.querySelector('#summary').onclick = () => {
            window.settingsClicks++;
            if (window.settingsClicks === 2) document.querySelector('#settings').hidden = false;
        };
    }""")
    # Keep the retry test fast; the real helper has a bounded hydration wait.
    monkeypatch.setattr(
        H, "_wait_for_open_fx_config_panel",
        lambda tab, trigger, seconds=3.0: H._get_open_fx_config_panel(tab, trigger),
    )
    panel = H._ensure_open_fx_config_panel(page, page.locator("#summary"))
    assert panel is not None
    assert panel.get_attribute("id") == "settings"
    assert page.evaluate("window.settingsClicks") == 2
