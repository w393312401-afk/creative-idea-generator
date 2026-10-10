"""Reference mounting waits for the requested frame, without fixed upload pauses."""

from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_helpers as H


UUID_A = "13408d9d-5fbf-4531-8823-8baf9cccde76"
UUID_B = "363878e9-216a-4c49-9411-10f09f95cc66"


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
    monkeypatch.setattr(H, "log", lambda *a, **k: None)
    monkeypatch.setattr(H, "time", clock)
    monkeypatch.setattr(H, "_check_cancelled", lambda: None)
    return clock


def test_exact_reference_ready_returns_without_sleep(clock, monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_A])

    assert H._wait_for_prompt_reference_change(object(), expected_uuid=UUID_A) == (True, [UUID_A])
    assert clock.sleeps == []


def test_exact_reference_waits_for_async_mount(clock, monkeypatch):
    monkeypatch.setattr(
        H, "_get_prompt_reference_uuids",
        lambda *a, **k: [UUID_A] if clock.now >= 0.6 else [],
    )

    assert H._wait_for_prompt_reference_change(object(), expected_uuid=UUID_A) == (True, [UUID_A])
    assert clock.now == pytest.approx(0.75)


def test_other_reference_growth_does_not_satisfy_expected_uuid(clock, monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_B])

    assert H._wait_for_prompt_reference_change(
        object(), previous_refs=[], expected_uuid=UUID_A, timeout_seconds=2,
    ) == (False, [UUID_B])
    assert clock.now == pytest.approx(2)


def test_reference_wait_stops_on_cancel(clock, monkeypatch):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])

    def cancelled():
        if clock.now >= 0.5:
            raise RuntimeError("任务已取消")

    monkeypatch.setattr(H, "_check_cancelled", cancelled)
    with pytest.raises(RuntimeError, match="任务已取消"):
        H._wait_for_prompt_reference_change(object(), expected_uuid=UUID_A)
    assert clock.now == pytest.approx(0.5)


class _DirectToolbar:
    @property
    def first(self):
        return self

    def locator(self, _selector):
        return self

    def is_visible(self, **_kwargs):
        return True

    def click(self, **_kwargs):
        pass


def _patch_mount_interactions(monkeypatch):
    monkeypatch.setattr(H, "random_sleep", lambda *a, **k: None)
    monkeypatch.setattr(H, "_safe_press_escape", lambda *a, **k: None)
    monkeypatch.setattr(H, "_hover_flow_tile_for_toolbar", lambda *a, **k: None)
    monkeypatch.setattr(H, "flow_tile_locator", lambda *a, **k: _DirectToolbar())
    monkeypatch.setattr(H, "_resolve_flow_tile_info", lambda *a, **k: {
        "tileId": "requested-frame", "w": 100, "h": 100,
    })

    def legacy_wait_must_not_run(*args, **kwargs):
        pytest.fail("Known UUIDs must not wait again for legacy ingredient selectors")

    monkeypatch.setattr(H, "_wait_for_flow_reference_ready", legacy_wait_must_not_run)


def test_add_known_reference_skips_legacy_wait(clock, monkeypatch):
    _patch_mount_interactions(monkeypatch)
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_A])

    assert H._add_flow_image_to_prompt(object(), UUID_A) is True
    assert clock.sleeps == []


def test_add_wrong_reference_is_rejected_with_one_wait_per_attempt(clock, monkeypatch):
    _patch_mount_interactions(monkeypatch)
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [UUID_B])

    assert H._add_flow_image_to_prompt(object(), UUID_A) is False
    # The old sequential budgets were (10 + 8) + (14 + 12) = 44 s.
    assert clock.now == pytest.approx(24)


def test_mount_two_video_frames_has_no_duplicate_readiness_wait(clock, monkeypatch):
    _patch_mount_interactions(monkeypatch)
    mounted = []
    monkeypatch.setattr(H, "_clear_prompt_reference_chips_video", lambda page: mounted.clear())
    monkeypatch.setattr(H, "_find_tile_by_uuid_js", lambda page, uuid: {
        "uuid": uuid, "tile_id": uuid,
    })
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: list(mounted))

    def add_frame(page, uuid, **kwargs):
        mounted.append(uuid)
        return True

    monkeypatch.setattr(H, "_add_flow_image_to_prompt", add_frame)
    meta = {}
    assert H._mount_video_prompt_refs(
        object(), start_ref=UUID_A, end_ref=UUID_B, result_meta=meta,
    ) == [UUID_A, UUID_B]
    assert meta["refs"] == [UUID_A, UUID_B]
    assert meta["strategy"] == "prompt_chips"
    assert clock.sleeps == []


class _CountLocator:
    def __init__(self, count=0, on_upload=None):
        self._count = count
        self._on_upload = on_upload

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def is_visible(self, **kwargs):
        return bool(self._count)

    def set_input_files(self, path):
        self._on_upload()


class _SlotPage:
    def __init__(self, clock, upload_delay):
        self.clock = clock
        self.upload_delay = upload_delay
        self.uploaded_at = None
        self.slot = SimpleNamespace(
            inner_text=lambda: "Start",
            click=lambda **kwargs: None,
            locator=self.thumbnail,
        )

    def uploaded(self):
        self.uploaded_at = self.clock.now

    def thumbnail(self, selector):
        ready = (
            self.upload_delay is not None
            and self.uploaded_at is not None
            and self.clock.now >= self.uploaded_at + self.upload_delay
        )
        return _CountLocator(int(ready and selector == "img"))

    def locator(self, selector):
        if "haspopup" in selector:
            return SimpleNamespace(count=lambda: 1, nth=lambda index: self.slot)
        if selector == "input[type='file']":
            return _CountLocator(1, on_upload=self.uploaded)
        return _CountLocator(0)


@pytest.mark.parametrize("upload_delay, expected_wait", [(0, 0), (0.6, 0.75), (None, 2)])
def test_slot_upload_checks_completion_without_fixed_pause(
    clock, monkeypatch, tmp_path, upload_delay, expected_wait,
):
    monkeypatch.setattr(H, "_get_prompt_reference_uuids", lambda *a, **k: [])
    monkeypatch.setattr(H, "random_sleep", lambda low, high: clock.sleep((low + high) / 2))
    page = _SlotPage(clock, upload_delay)
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")

    assert H._upload_to_slot_directly(
        page, "Start", str(image), verify_timeout=2,
    ) is (upload_delay is not None)
    # Menu interaction retains its 1–1.5 s allowance. After selecting the file,
    # the old 4–6 s mandatory sleep is entirely replaced by real readiness.
    assert page.uploaded_at == pytest.approx(1.25)
    assert clock.now - page.uploaded_at == pytest.approx(expected_wait)
