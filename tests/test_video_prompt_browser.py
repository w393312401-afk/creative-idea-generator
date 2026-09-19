"""Isolated editor regression: no Flow navigation or generation requests."""
import pytest
from playwright.sync_api import sync_playwright, Error
from integrations.google_fx.services import google_fx_helpers as H


@pytest.fixture
def editor_page(monkeypatch):
    monkeypatch.setattr(H, 'random_sleep', lambda *a: None)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f'Local Playwright Chromium unavailable: {exc}')
        page = browser.new_page()
        page.set_content('''<div class="ProseMirror" contenteditable="true"
            style="width:420px;height:260px;overflow:auto;font:16px Arial">
            <span contenteditable="false" data-ref="one">IMAGE 1</span>
            <span contenteditable="false" data-ref="two">IMAGE 2</span></div>''')
        try:
            yield page, page.locator('.ProseMirror')
        finally:
            browser.close()


@pytest.mark.parametrize('prompt', [
    'The worker carefully installs concrete foundation pipes with realistic weight shifts. ' * 18,
    '第一段：墙体施工。\n第二段：铺设地面。\n保留首尾帧与现场细节。',
])
def test_fill_preserves_full_text_and_reference_chips(editor_page, prompt):
    page, editor = editor_page
    assert H._fill_prompt_text(page, editor, prompt, has_refs=True)
    H._verify_video_prompt_before_send(editor, prompt)
    assert editor.locator('[data-ref]').count() == 2
    # A repeated fill of an identical request must not append it twice.
    assert H._fill_prompt_text(page, editor, prompt, has_refs=True)
    H._verify_video_prompt_before_send(editor, prompt)


def test_midword_space_from_editor_interaction_is_detected(editor_page):
    page, editor = editor_page
    prompt = 'The worker uses concrete pipes and realistic weight shifts. ' * 20
    assert H._fill_prompt_text(page, editor, prompt, has_refs=True)
    # Reproduce a caret left inside a word after clicking a wrapped editor.
    editor.evaluate('''ed => {
        const walker = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
        let node; while (node = walker.nextNode()) {
            const index = node.textContent.indexOf('concrete');
            if (index < 0) continue;
            const range = document.createRange(); range.setStart(node, index + 3);
            range.collapse(true); const selection = getSelection();
            selection.removeAllRanges(); selection.addRange(range); ed.focus(); break;
        }
    }''')
    page.keyboard.type(' ')
    with pytest.raises(RuntimeError, match='PROMPT_TEXT_MISMATCH'):
        H._verify_video_prompt_before_send(editor, prompt)
