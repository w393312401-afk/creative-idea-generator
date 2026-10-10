"""The image reference fallback must use Flow's menu-owned native file chooser."""

from contextlib import contextmanager
from types import SimpleNamespace

from integrations.google_fx.services import google_fx_helpers as helpers


UUID = "a9144797-f070-4470-abf4-6c69e805fb7d"


class _Chooser:
    def __init__(self):
        self.path = None

    def set_files(self, path):
        self.path = path


class _Page:
    def __init__(self, chooser_opens=True):
        self.chooser_opens = chooser_opens
        self.clicked = False
        self.chooser = _Chooser()
        self.escapes = []
        self.uuid_reads = 0

    @contextmanager
    def expect_file_chooser(self, timeout):
        assert timeout == 3000
        yield SimpleNamespace(value=self.chooser)
        if not self.clicked or not self.chooser_opens:
            raise TimeoutError("Flow did not open a file chooser")


class _Action:
    def __init__(self, page):
        self.page = page

    def click(self, **kwargs):
        assert kwargs == {"timeout": 2500}
        self.page.clicked = True


def _prepare(monkeypatch, page):
    monkeypatch.setattr(helpers, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helpers, "_check_cancelled", lambda: None)
    monkeypatch.setattr(helpers, "_open_canvas_upload_menu", lambda _page: True)
    monkeypatch.setattr(helpers, "_find_add2_btn", lambda _page: object())
    monkeypatch.setattr(helpers, "_find_canvas_upload_action", lambda _page, _trigger: _Action(page))
    monkeypatch.setattr(helpers, "_safe_press_escape",
                        lambda _page, reason: page.escapes.append(reason))
    monkeypatch.setattr(helpers, "random_sleep", lambda *_args: None)

    def uuids(_page):
        page.uuid_reads += 1
        return set() if page.uuid_reads == 1 else {UUID}

    monkeypatch.setattr(helpers, "_get_panel_uuids", uuids)


def test_image_reference_mount_uses_native_chooser_without_dom_file_input(tmp_path, monkeypatch):
    source = tmp_path / "chain_ref_005.jpg"
    source.write_bytes(b"test image")
    page = _Page()
    _prepare(monkeypatch, page)
    mounted = []
    monkeypatch.setattr(helpers, "_add_flow_image_to_prompt",
                        lambda _page, uuid: mounted.append(uuid) or True)

    assert helpers._upload_image_to_canvas_and_mount(page, str(source)) == UUID
    assert page.chooser.path == str(source)
    assert mounted == [UUID]
    assert page.clicked


def test_missing_chooser_stops_without_mounting_or_no_reference_fallback(tmp_path, monkeypatch):
    source = tmp_path / "chain_ref_005.jpg"
    source.write_bytes(b"test image")
    page = _Page(chooser_opens=False)
    _prepare(monkeypatch, page)
    mounted = []
    monkeypatch.setattr(helpers, "_add_flow_image_to_prompt",
                        lambda *_args: mounted.append(True))

    assert helpers._upload_image_to_canvas_and_mount(page, str(source)) is False
    assert page.chooser.path is None
    assert mounted == []
    assert page.escapes == ["上传回退异常"]
