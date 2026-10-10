"""The current Flow frame bar owns Start/End order, including after Swap."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from integrations.google_fx.services import google_fx_helpers as H


START = "641e0ebc-ed0d-4e6b-a45a-4edd829b9e66"
END = "24def6cc-52a7-41b4-9c06-1c2efda4ffed"
OTHER = "3d206430-34f1-4c42-bb0f-b477cf94b6c0"


@pytest.fixture(scope="module")
def slot_browser():
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Local Playwright Chromium is not installed")
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def slot_page(slot_browser, monkeypatch, offline_fx_video_io):
    context = slot_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    monkeypatch.setattr(H, "log", lambda *args: None)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    try:
        yield page
    finally:
        context.close()


def _render_slots(page, refs=(START, END), *, crop_shift=False, swaps=True, delay=60):
    first_style = "transform:translateY(110px)" if crop_shift else ""
    slots = "".join(
        f'<div class="frame-trigger"><button class="chip-container">'
        f'<img class="chip-image" width="56" height="56" '
        f'style="{first_style if index == 0 else ""}" '
        f'src="https://test.invalid/image/{uuid}"></button></div>'
        for index, uuid in enumerate(refs)
    )
    page.set_content(f"""
        <flow-prompt-box style="display:block">
            <div class="ProseMirror" contenteditable="true"></div>
            <flow-ingredient-bar style="display:flex">{slots}
                <button id="swap" aria-label="Swap first and last frames">Swap</button>
            </flow-ingredient-bar>
        </flow-prompt-box>
        <script>
        window.swapClicks = 0;
        document.getElementById('swap').onclick = () => {{
            swapClicks++;
            if ({str(swaps).lower()}) setTimeout(() => {{
                const images = document.querySelectorAll('.frame-trigger img');
                const first = images[0].src;
                images[0].src = images[1].src;
                images[1].src = first;
            }}, {delay});
        }};
        </script>
    """)


def test_crop_geometry_cannot_reverse_semantic_start_end_order(slot_page):
    _render_slots(slot_page, crop_shift=True)
    # The Start image is visually below End; slot ownership is unchanged.
    assert slot_page.locator('.frame-trigger img').nth(0).bounding_box()['y'] > \
        slot_page.locator('.frame-trigger img').nth(1).bounding_box()['y'] + 12
    assert H.read_prompt_reference_state(slot_page)['uuids'] == [START, END]


def test_end_only_keeps_empty_start_and_cannot_pass_start_frame_gate(slot_page, monkeypatch):
    _render_slots(slot_page, (OTHER, START))
    slot_page.locator('.frame-trigger').nth(0).evaluate("slot => slot.innerHTML = '<button>Start</button>'")
    state = H.read_prompt_reference_state(slot_page)
    assert state['uuids'] == [START]
    assert state['frame_slots'] == ['', START]
    assert not H._video_refs_still_attached(
        slot_page, {'strategy': 'prompt_chips', 'expected': [START]},
    )
    monkeypatch.setattr(H, '_clear_prompt_reference_chips_video', lambda page: None)
    monkeypatch.setattr(H, '_mount_flow_images_to_prompt', lambda *args, **kwargs: [START])
    assert H._mount_video_prompt_refs(slot_page, start_ref=START) == []
    assert slot_page.evaluate('swapClicks') == 0


def test_hidden_stale_frame_bar_cannot_shadow_current_slots(slot_page):
    _render_slots(slot_page)
    slot_page.evaluate("""uuid => {
        const old = document.querySelector('flow-ingredient-bar').cloneNode(true);
        old.style.display = 'none';
        old.querySelector('img').src = 'https://test.invalid/image/' + uuid;
        document.querySelector('flow-prompt-box').prepend(old);
    }""", OTHER)
    assert H.read_prompt_reference_state(slot_page)['uuids'] == [START, END]


def test_reversed_pair_waits_for_asynchronous_swap_update(slot_page):
    _render_slots(slot_page, (END, START))
    assert H._repair_reversed_video_frame_slots(slot_page, [START, END], [END, START])
    assert slot_page.evaluate('swapClicks') == 1
    assert H.read_prompt_reference_state(slot_page)['uuids'] == [START, END]


@pytest.mark.parametrize('expected,actual', [
    ([START, END], [START, END]),
    ([START, END], [END]),
    ([START, END], [OTHER, START]),
    ([START, START], [START, START]),
    ([START], [END]),
])
def test_swap_never_changes_correct_missing_wrong_or_duplicate_refs(slot_page, expected, actual):
    _render_slots(slot_page)
    assert not H._repair_reversed_video_frame_slots(slot_page, expected, actual)
    assert slot_page.evaluate('swapClicks') == 0


def test_click_without_order_change_is_not_a_success(slot_page):
    _render_slots(slot_page, (END, START), swaps=False)
    assert not H._repair_reversed_video_frame_slots(
        slot_page, [START, END], [END, START], timeout=0.15,
    )
    assert slot_page.evaluate('swapClicks') == 1


def test_mount_repairs_reversed_pair_without_clearing_or_uploading_again(slot_page, monkeypatch):
    _render_slots(slot_page, (END, START))
    clears, mounts = [], []
    monkeypatch.setattr(H, '_clear_prompt_reference_chips_video', lambda page: clears.append(True))
    monkeypatch.setattr(H, '_mount_flow_images_to_prompt',
                        lambda page, refs, **kwargs: mounts.append(list(refs)) or list(refs))
    monkeypatch.setattr(H, '_upload_to_slot_directly',
                        lambda *args, **kwargs: pytest.fail('Must reuse the correctly identified pair'))
    meta = {}
    assert H._mount_video_prompt_refs(slot_page, START, END, result_meta=meta) == [START, END]
    assert len(clears) == 1
    assert mounts == [[START, END]]
    assert meta == {'strategy': 'prompt_chips', 'expected': [START, END], 'refs': [START, END]}


def test_cancellation_during_swap_wait_propagates(slot_page, monkeypatch):
    _render_slots(slot_page, (END, START), swaps=False)

    def check_cancelled():
        if slot_page.evaluate('swapClicks'):
            raise RuntimeError('任务已取消')

    monkeypatch.setattr(H, '_check_cancelled', check_cancelled)
    with pytest.raises(RuntimeError, match='任务已取消'):
        H._repair_reversed_video_frame_slots(slot_page, [START, END], [END, START])
    assert slot_page.evaluate('swapClicks') == 1
