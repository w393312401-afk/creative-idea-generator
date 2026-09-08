from unittest.mock import patch
import re

from integrations.google_fx.services import google_fx_helpers as H


class Control:
    def __init__(self, name, visible=True, attrs=None, blocked=False):
        self.name, self.visible = name, visible
        self.attrs, self.blocked = attrs or {}, blocked
        self.clicks = 0

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name)

    def click(self, **kwargs):
        assert not kwargs.get('force')
        if self.blocked:
            raise RuntimeError('overlay intercepts pointer events')
        self.clicks += 1


class Group:
    def __init__(self, controls):
        self.controls = controls

    def count(self):
        return len(self.controls)

    def nth(self, i):
        return self.controls[i]

    def filter(self, has_text):
        return Group([b for b in self.controls if has_text.search(b.name)])


class Page:
    def __init__(self, controls=()):
        self.controls = controls

    def locator(self, selector):
        values = re.findall(r"data-value='([^']+)'", selector)
        return Group([b for b in self.controls if b.attrs.get('data-value') in values])

    def get_by_role(self, role, name):
        return Group([b for b in self.controls if name.search(b.name)])

    def evaluate(self, js):
        return []


def switch(page, mode='VIDEO_FRAMES', scope=None):
    with patch.object(H, 'random_sleep'), patch.object(H, 'log'):
        return H._switch_video_submode(page, mode, scope=scope)


def test_hidden_duplicate_does_not_shadow_visible_frames():
    hidden, live = Control('Frames', visible=False), Control('Frames')
    assert switch(Page([hidden, live]))
    assert hidden.clicks == 0 and live.clicks == 1


def test_submode_outside_settings_panel_is_found():
    live = Control('Ingredients')
    assert switch(Page([live]), 'VIDEO_REFERENCES', scope=Page())
    assert live.clicks == 1


def test_icon_only_control_accessible_name_is_supported():
    live = Control('Start & End')
    assert switch(Page([live]))
    assert live.clicks == 1


def test_inactive_class_does_not_count_as_active():
    live = Control('Frames', attrs={'class': 'inactive'})
    assert switch(Page([live]))
    assert live.clicks == 1


def test_selected_control_does_not_toggle_off():
    live = Control('Frames', attrs={'aria-pressed': 'true'})
    assert switch(Page([live]))
    assert live.clicks == 0


def test_unrelated_frame_actions_are_never_clicked():
    controls = [Control('Delete frames'), Control('Upload start frame')]
    assert not switch(Page(controls))
    assert not any(b.clicks for b in controls)


def test_missing_control_is_not_silently_accepted():
    assert not switch(Page([Control('Video')]))


def test_blocked_control_is_not_force_clicked():
    live = Control('Frames', blocked=True)
    assert not switch(Page([live]))
    assert live.clicks == 0


def test_exact_business_value_supports_localized_label():
    live = Control('Bingkai', attrs={'data-value': 'VIDEO_FRAMES'})
    assert switch(Page([live]))
    assert live.clicks == 1


def test_actual_material_labels_and_composer_state():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content("""<style>flow-toggles, .ingredient-bar-container, .frame-trigger {display:block}</style>
          <span class="settings-summary">Video · 360p · 10s crop_9_16 x1</span>
          <flow-toggles aria-label="Video type">
            <button role="radio" aria-checked="true"><mat-icon>crop_free</mat-icon><span class="toggle-text">Frames</span></button>
            <button role="radio" aria-checked="false"><mat-icon>chrome_extension</mat-icon><span class="toggle-text">Ingredients</span></button>
          </flow-toggles>
          <script>document.querySelectorAll('[role=radio]').forEach(b=>b.onclick=()=>{
            document.querySelectorAll('[role=radio]').forEach(x=>x.setAttribute('aria-checked','false'));
            b.setAttribute('aria-checked','true');
          });</script>""")
        assert H._video_frames_mode_is_active(page)
        assert H._switch_video_submode(page, 'VIDEO_REFERENCES')
        assert not H._video_frames_mode_is_active(page)
        assert H._switch_video_submode(page, 'VIDEO_FRAMES')
        assert H._video_frames_mode_is_active(page)
        page.locator('flow-toggles').evaluate('(el)=>el.remove()')
        assert not H._video_frames_mode_is_active(page)  # summary alone proves nothing
        page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
          '<div class="ingredient-bar-container"><div class="frame-trigger"><button>Start</button></div>'+
          '<button aria-label="Swap first and last frames">swap_horiz</button>'+
          '<div class="frame-trigger"><button>End</button></div></div>')""")
        assert H._video_frames_mode_is_active(page)
        page.locator('.ingredient-bar-container').evaluate('(el)=>el.style.display="none"')
        assert not H._video_frames_mode_is_active(page)
        browser.close()
