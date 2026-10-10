"""Flow duration regressions using local HTML only; never connect to an account."""

from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, sync_playwright

from integrations.google_fx.services import google_fx_helpers as H


pytestmark = pytest.mark.usefixtures("offline_fx_video_io")


@pytest.fixture(autouse=True)
def no_real_waits_or_logs(monkeypatch):
    monkeypatch.setattr(H, "random_sleep", lambda *args: None)
    monkeypatch.setattr(H, "log", lambda *args: None)


def _checks(status, duration="8s"):
    return H.check_fx_config(
        status,
        model="Omni Flash",
        resolved_model_text="Omni Flash",
        orientation="Portrait",
        count="1x",
        duration=duration,
        want_video=True,
        resolution="720p",
    )


@pytest.mark.parametrize("label", ["8秒", "8 秒", "8\u00a0秒", "8 秒钟", "8 秒鐘", "8 s", "8s", "8 seconds"])
def test_summary_recognizes_localized_duration(label):
    checks = _checks(f"视频 · 720p · {label} crop_9_16 x1")
    assert all(checks.values()), checks


@pytest.mark.parametrize(
    "status,requested",
    [
        ("视频 · 720p · 8 秒 crop_9_16 x1", "10s"),
        ("Video · 720p · 18s crop_9_16 x1", "8s"),
        ("Video · 720p · 108s crop_9_16 x1", "8s"),
        ("Video · 720p · 8.5s crop_9_16 x1", "8s"),
        ("视频 · 720p · 18 秒 crop_9_16 x1", "8s"),
        ("视频 · 720p crop_9_16 x1", "8s"),
        ("视频 · 720p crop_9_16 x1", "720s"),
        ("视频 · 720p · 8 crop_9_16 x1", "8s"),
    ],
)
def test_summary_requires_exact_duration_and_seconds_unit(status, requested):
    assert _checks(status, requested)["duration"] is False


def test_correct_chinese_summary_does_not_reopen_configuration(monkeypatch):
    page = Mock()
    monkeypatch.setattr(
        H, "find_fx_config_button",
        lambda page: (Mock(), "视频 · 720p · 8 秒 crop_9_16 x1"),
    )
    # Model verification is a separate concern: hold it confirmed to isolate
    # whether the localized duration triggers an unnecessary repair.
    monkeypatch.setattr(H, "_matches_model_status", lambda *args: True)
    monkeypatch.setattr(H, "_video_frames_mode_is_active", lambda page: True)
    repair = Mock(side_effect=AssertionError("already-correct settings were reopened"))
    monkeypatch.setattr(H, "fix_fx_config", repair)

    assert H._verify_and_fix_fx_config(
        page, "Omni Flash", "9:16", True, "切换Video",
        duration="8s", video_submode="VIDEO_FRAMES", resolution="720p",
    ) == "9:16"
    repair.assert_not_called()


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
def local_page(local_browser):
    context = local_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_default_timeout(1000)
    try:
        yield page
    finally:
        context.close()


def _set_content(page, html):
    page.set_content(html)
    page.evaluate("""() => {
        window.clicked = [];
        document.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (button) window.clicked.push(button.id);
        });
    }""")


def _duration_buttons(unit=" 秒"):
    return "".join(
        f'<button id="duration-{duration}" role="radio">{duration}{unit}</button>'
        for duration in (4, 6, 8, 10)
    )


@pytest.mark.parametrize("duration", [4, 6, 8, 10])
def test_clicks_chinese_duration_controls(local_page, duration):
    _set_content(local_page, f"""
        <div id="panel"><flow-toggles aria-label="视频时长">
        {_duration_buttons()}
        </flow-toggles></div>
    """)
    assert H._click_video_duration_tab(
        local_page, local_page.locator("#panel"), f"{duration}s",
    )
    assert local_page.evaluate("window.clicked") == [f"duration-{duration}"]


@pytest.mark.parametrize("container", ["flow-toggles", "legacy-tabs"])
def test_english_duration_controls_remain_supported(local_page, container):
    if container == "flow-toggles":
        controls = f'<flow-toggles aria-label="Video duration">{_duration_buttons("s")}</flow-toggles>'
    else:
        controls = '<button id="duration-8" role="tab" aria-controls="settings-DURATION_8">8s</button>'
    _set_content(local_page, f'<div id="panel">{controls}</div>')
    assert H._click_video_duration_tab(local_page, local_page.locator("#panel"), "8s")
    assert local_page.evaluate("window.clicked") == ["duration-8"]


def test_unknown_aria_label_uses_panel_scoped_fallback(local_page):
    _set_content(local_page, f"""
        <button id="history">10s</button>
        <div id="panel"><flow-toggles aria-label="生成长度选项">
        {_duration_buttons()}
        </flow-toggles></div>
    """)
    assert H._click_video_duration_tab(local_page, local_page.locator("#panel"), "10s")
    assert local_page.evaluate("window.clicked") == ["duration-10"]


def test_hidden_duplicate_does_not_mask_visible_duration(local_page):
    _set_content(local_page, """
        <div id="panel">
          <flow-toggles aria-label="Video duration" hidden>
            <button id="hidden-container">8s</button>
          </flow-toggles>
          <flow-toggles aria-label="Video duration">
            <button id="hidden-button" hidden>8s</button>
            <button id="duration-8">8s</button>
          </flow-toggles>
        </div>
    """)
    assert H._click_video_duration_tab(local_page, local_page.locator("#panel"), "8s")
    assert local_page.evaluate("window.clicked") == ["duration-8"]


def test_icon_text_does_not_obscure_duration_label(local_page):
    _set_content(local_page, """
        <div id="panel"><flow-toggles aria-label="视频时长">
          <button id="duration-8"><mat-icon>check</mat-icon>
            <span class="toggle-text">8 秒</span></button>
        </flow-toggles></div>
    """)
    assert H._click_video_duration_tab(local_page, local_page.locator("#panel"), "8s")
    assert local_page.evaluate("window.clicked") == ["duration-8"]


def test_duration_does_not_match_longer_value_or_escape_panel(local_page):
    _set_content(local_page, """
        <div id="history"><button id="history-8">8s</button></div>
        <div id="panel"><flow-toggles aria-label="视频时长">
          <button id="duration-18">18 秒</button>
        </flow-toggles></div>
    """)
    assert not H._click_video_duration_tab(local_page, local_page.locator("#panel"), "8s")
    assert local_page.evaluate("window.clicked") == []


def test_missing_target_in_duration_container_does_not_click_history(local_page):
    _set_content(local_page, """
        <button id="history-8">8s</button>
        <flow-toggles aria-label="Video duration">
          <button id="duration-18">18s</button>
        </flow-toggles>
    """)
    # fix_fx_config can fall back to page scope if a panel wrapper is absent.
    assert not H._click_video_duration_tab(local_page, local_page, "8s")
    assert local_page.evaluate("window.clicked") == []


def test_verify_repairs_chinese_eight_seconds_to_ten_seconds(local_page, monkeypatch):
    _set_content(local_page, f"""
        <button id="config">视频 · 720p · 8 秒 crop_9_16 x1</button>
        <div id="panel">
          <button id="model">Omni Flash</button>
          <flow-toggles aria-label="视频时长">{_duration_buttons()}</flow-toggles>
          <button id="save">保存</button>
        </div>
    """)
    local_page.evaluate("""() => {
        window.pendingDuration = '8';
        document.querySelectorAll('flow-toggles button').forEach(button => {
            button.onclick = () => { window.pendingDuration = button.id.split('-')[1]; };
        });
        document.querySelector('#save').onclick = () => {
            document.querySelector('#config').textContent =
                `视频 · 720p · ${window.pendingDuration} 秒 crop_9_16 x1`;
        };
    }""")
    panel = local_page.locator("#panel")
    monkeypatch.setattr(
        H, "find_fx_config_button",
        lambda page: (page.locator("#config"), page.locator("#config").inner_text()),
    )
    monkeypatch.setattr(H, "_get_open_fx_config_panel", lambda *args: panel)
    monkeypatch.setattr(H, "_find_fx_model_dropdown", lambda *args, **kwargs: local_page.locator("#model"))
    monkeypatch.setattr(H, "_get_fx_model_dropdown_text", lambda *args, **kwargs: "Omni Flash")
    monkeypatch.setattr(H, "detect_page_credit_exhaustion", lambda *args, **kwargs: None)

    assert H._verify_and_fix_fx_config(
        local_page, "Omni Flash", "9:16", True, "切换Video",
        duration="10s", resolution="720p",
    ) == "9:16"
    assert local_page.evaluate("window.clicked") == ["duration-4", "duration-10", "save"]
    assert _checks(local_page.locator("#config").inner_text(), "10s")["duration"] is True
