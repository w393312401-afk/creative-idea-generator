from integrations.google_fx.services.google_fx_helpers import (
    _find_fx_model_dropdown,
    find_fx_config_button,
)


class _Element:
    def __init__(self, text, attrs=None):
        self.text = text
        self.attrs = attrs or {}

    def is_visible(self, timeout=None):
        return True

    def is_enabled(self):
        return True

    def inner_text(self, timeout=None):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)


class _Locator:
    def __init__(self, rows=()):
        self.rows = list(rows)

    @property
    def first(self):
        return _Locator(self.rows[:1])

    def count(self):
        return len(self.rows)

    def nth(self, index):
        return self.rows[index]

    def is_visible(self, timeout=None):
        return bool(self.rows)

    def filter(self, has_text=None):
        if has_text is None:
            return self
        if hasattr(has_text, 'search'):
            return _Locator([row for row in self.rows if has_text.search(row.text)])
        return _Locator([row for row in self.rows if str(has_text) in row.text])


class _ModelPage:
    def __init__(self):
        self.tune = _Element('tune\n设置')
        self.summary = _Element('Video\ncrop_9_16\nx2')
        self.image = _Element('🍌 Nano Banana 2 Lite\narrow_drop_down', {
            'aria-haspopup': 'menu', 'id': 'image-model'
        })
        self.video = _Element('Omni Flash\narrow_drop_down', {
            'aria-haspopup': 'menu', 'id': 'video-model'
        })

    def locator(self, selector):
        if selector == 'button':
            return _Locator([self.tune, self.summary, self.image, self.video])
        if selector == "button[aria-haspopup='menu']":
            return _Locator([self.image, self.video])
        return _Locator()


def test_tune_sidebar_is_not_confused_with_generation_config_summary():
    page = _ModelPage()
    button, text = find_fx_config_button(page)
    assert button is page.summary
    assert 'Video' in text and 'x2' in text


def test_model_dropdown_targets_image_and_video_families_separately():
    page = _ModelPage()
    assert _find_fx_model_dropdown(page, scope=page, target_model='Nano Banana 2') is page.image
    assert _find_fx_model_dropdown(page, scope=page, target_model='Omni Flash') is page.video
