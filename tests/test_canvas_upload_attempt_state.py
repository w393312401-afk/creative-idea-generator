"""Menu failures cannot masquerade as a pending upload or leak browser state."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V


UUID = "13408d9d-5fbf-4531-8823-8baf9cccde76"


class Action:
    def __init__(self, page):
        self.page = page

    def click(self, **kwargs):
        assert self.page.listening, 'Every chooser click must have a listener'
        self.page.clicks += 1


class Page:
    def __init__(self, has_action=True, lookup_error=False, file_error=False):
        self.has_action = has_action
        self.lookup_error = lookup_error
        self.file_error = file_error
        self.listeners = []
        self.file_attempts = 0
        self.escapes = 0
        self.clicks = 0
        self.listening = False
        self.chooser_failures = 0

    def action(self):
        if self.lookup_error:
            raise RuntimeError("page disappeared")
        return Action(self) if self.has_action else None

    @contextmanager
    def expect_file_chooser(self, **kwargs):
        self.listening = True
        try:
            yield SimpleNamespace(value=self)
            if self.chooser_failures:
                self.chooser_failures -= 1
                raise TimeoutError('chooser did not open')
        finally:
            self.listening = False

    def set_files(self, path):
        self.file_attempts += 1
        if self.file_error:
            raise RuntimeError("file selection interrupted")

    def locator(self, selector):
        pytest.fail(f'Upload must not scan page-wide inputs/actions: {selector}')

    def title(self):
        return 'Flow'

    def on(self, event, handler):
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        self.listeners.remove(handler)


@pytest.fixture
def upload(tmp_path, monkeypatch):
    path = tmp_path / 'img_053.webp'
    path.write_bytes(b'frame')
    monkeypatch.setattr(V, 'log', lambda *a, **k: None)
    monkeypatch.setattr(V, '_open_canvas_upload_menu', lambda page: True)
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: object())
    monkeypatch.setattr(V, '_find_canvas_upload_action', lambda page, trigger: page.action())
    monkeypatch.setattr(V, '_safe_press_escape', lambda page, *a: setattr(page, 'escapes', page.escapes + 1))
    monkeypatch.setattr(V, '_find_canvas_upload_file_input', lambda page, trigger: None)
    monkeypatch.setattr(V, '_describe_canvas_upload_state', lambda page, action: 'snapshot')
    monkeypatch.setattr(V, '_get_panel_uuids', lambda page: set())
    monkeypatch.setattr(V, '_get_panel_uuid_order', lambda page, **kwargs: [UUID])
    monkeypatch.setattr(V, '_make_response_handler', lambda *a, **k: object())
    monkeypatch.setattr(V, '_check_cancelled', lambda: None)
    monkeypatch.setattr(V, 'random_sleep', lambda *a: None)
    return str(path)


def test_blocked_menu_reports_upload_not_started(upload, monkeypatch):
    page, state = Page(), {}
    monkeypatch.setattr(V, '_open_canvas_upload_menu', lambda page: False)
    assert V._upload_image_to_canvas(page, upload, upload_state=state) is None
    assert state['started'] is False
    assert state['failure_reason'] == '无法打开上传菜单'
    assert page.file_attempts == 0
    assert page.listeners == []
    assert page.escapes == 2


def test_missing_upload_action_cleans_listener_and_menu_after_bounded_recovery(upload):
    page, state = Page(has_action=False), {}
    assert V._upload_image_to_canvas(page, upload, upload_state=state) is None
    assert state['started'] is False
    assert state['failure_reason'] == '上传菜单中未找到文件选择入口'
    assert page.listeners == []
    assert page.escapes == 2


def test_menu_lookup_exception_also_cleans_listener_and_menu(upload):
    page, state = Page(has_action=False, lookup_error=True), {}
    with pytest.raises(RuntimeError, match='page disappeared'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is False
    assert page.listeners == []
    assert page.escapes == 1


def test_file_selection_error_is_ambiguous_and_never_reuploads(upload):
    page, state = Page(file_error=True), {}
    with pytest.raises(RuntimeError, match='file selection interrupted'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is True
    assert page.file_attempts == 1
    assert page.listeners == []
    assert page.escapes == 1


def test_success_cleans_listener_and_menu(upload):
    page, state = Page(), {}
    assert V._upload_image_to_canvas(page, upload, upload_state=state) == UUID
    assert state['started'] is True
    assert page.file_attempts == 1
    assert page.listeners == []
    assert page.escapes == 1


def test_cancel_after_upload_exits_poll_and_cleans_menu(upload, monkeypatch):
    page, state = Page(), {}

    def check():
        if page.file_attempts:
            raise RuntimeError('任务已取消')

    monkeypatch.setattr(V, '_check_cancelled', check)
    with pytest.raises(RuntimeError, match='任务已取消'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert page.listeners == []
    assert page.escapes == 1


def test_cancel_while_opening_menu_also_cleans_backdrop(upload, monkeypatch):
    page, state = Page(), {}

    def cancelled(page):
        raise RuntimeError('任务已取消')

    monkeypatch.setattr(V, '_open_canvas_upload_menu', cancelled)
    with pytest.raises(RuntimeError, match='任务已取消'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is False
    assert page.listeners == []
    assert page.escapes == 1


def test_chooser_set_files_error_never_clicks_upload_twice(upload):
    page, state = Page(file_error=True), {}
    with pytest.raises(RuntimeError, match='file selection interrupted'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is True
    assert page.clicks == 1
    assert page.file_attempts == 1
    assert page.listeners == []
    assert page.escapes == 1


def test_chooser_timeout_recovers_before_single_file_handoff(upload):
    page, state = Page(), {}
    page.chooser_failures = 1
    assert V._upload_image_to_canvas(page, upload, upload_state=state) == UUID
    assert state == {'started': True}
    assert page.clicks == 2
    assert page.file_attempts == 1
    assert page.listeners == []
    assert page.escapes == 2


def test_chooser_timeouts_stop_after_two_observed_clicks(upload):
    page, state = Page(), {}
    page.chooser_failures = 10
    assert V._upload_image_to_canvas(page, upload, upload_state=state) is None
    assert state['started'] is False
    assert '未打开文件选择器' in state['failure_reason']
    assert page.clicks == 2
    assert page.file_attempts == 0
    assert page.listeners == []
    assert page.escapes == 2


def test_lost_menu_action_is_reacquired_before_upload(upload, monkeypatch):
    page, state = Page(), {}
    monkeypatch.setattr(V, '_find_canvas_upload_action',
                        lambda page, trigger: page.action() if page.escapes else None)
    assert V._upload_image_to_canvas(page, upload, upload_state=state) == UUID
    assert state == {'started': True}
    assert page.clicks == 1
    assert page.file_attempts == 1
    assert page.listeners == []
    assert page.escapes == 2


@pytest.mark.parametrize('error_type', [
    ConnectionError,
    type('TargetClosedError', (RuntimeError,), {}),
    type('BrowserSessionClosedError', (RuntimeError,), {}),
])
def test_lost_browser_during_chooser_propagates_to_runner(upload, monkeypatch, error_type):
    page, state = Page(), {}

    def disconnected(**kwargs):
        raise error_type('browser disconnected')

    monkeypatch.setattr(page, 'expect_file_chooser', disconnected)
    with pytest.raises(error_type, match='browser disconnected'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is False
    assert page.file_attempts == 0
    assert page.listeners == []
    assert page.escapes == 1


def test_cancellation_after_chooser_before_set_files_never_retries(upload, monkeypatch):
    page, state = Page(), {}

    def check():
        if page.clicks:
            raise RuntimeError('任务已取消')

    monkeypatch.setattr(V, '_check_cancelled', check)
    with pytest.raises(RuntimeError, match='任务已取消'):
        V._upload_image_to_canvas(page, upload, upload_state=state)
    assert state['started'] is False
    assert page.clicks == 1
    assert page.file_attempts == 0
    assert page.listeners == []
    assert page.escapes == 1


@pytest.mark.parametrize('started,expected_wait', [(False, 0), (True, 10)])
def test_only_an_attempted_file_upload_waits_for_late_cards(upload, monkeypatch, started, expected_wait):
    req = SimpleNamespace(image=upload, end_image='', ratio='9:16')
    runner = V._ChunkRunner(1, 0, [req], {}, None, None)
    now = [0]
    monkeypatch.setattr(V.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: object())

    def fail_upload(page, path, upload_state, **kwargs):
        upload_state['started'] = started
        return None

    monkeypatch.setattr(V, '_upload_image_to_canvas', fail_upload)
    if started:
        assert runner._upload_references(Page(), [(0, req)]) == {}
    else:
        with pytest.raises(RuntimeError, match='尚未开始上传: 未找到可用上传入口'):
            runner._upload_references(Page(), [(0, req)])
    assert now[0] == expected_wait


def test_unavailable_upload_menu_stops_before_trying_the_next_reference(upload, monkeypatch, tmp_path):
    end_frame = tmp_path / 'end.webp'
    end_frame.write_bytes(b'end')
    req = SimpleNamespace(image=upload, end_image=str(end_frame), ratio='9:16')
    runner = V._ChunkRunner(1, 0, [req], {}, None, None)
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: None)
    monkeypatch.setattr(V, '_verify_and_fix_fx_config', lambda *a, **k: pytest.fail('must not switch to Image'))
    calls = []

    def closed_menu(page):
        calls.append(page)
        return False

    monkeypatch.setattr(V, '_open_canvas_upload_menu', closed_menu)
    with pytest.raises(RuntimeError, match='img_053.webp 尚未开始上传: 无法打开上传菜单'):
        runner._upload_references(Page(), [(0, req)])
    assert len(calls) == 2


def test_chooser_never_opening_falls_back_to_the_menu_file_input(upload, monkeypatch):
    page, state = Page(), {}
    page.chooser_failures = 10
    handed = []
    monkeypatch.setattr(
        V, '_find_canvas_upload_file_input',
        lambda page, trigger: SimpleNamespace(set_input_files=handed.append))
    assert V._upload_image_to_canvas(page, upload, upload_state=state) == UUID
    assert state == {'started': True}
    assert handed == [upload]
    assert page.file_attempts == 0
    assert page.listeners == []


def test_failure_reason_names_the_stuck_phase(upload):
    page, state = Page(), {}
    page.chooser_failures = 10
    assert V._upload_image_to_canvas(page, upload, upload_state=state) is None
    assert '卡在「等待文件选择器」' in state['failure_reason']


def _runner(upload):
    req = SimpleNamespace(image=upload, end_image='', ratio='9:16')
    runner = V._ChunkRunner(1, 0, [req], {}, None, None)
    return runner, req


def test_stuck_upload_entry_reloads_a_quiet_canvas_and_retries_once(upload, monkeypatch):
    runner, req = _runner(upload)
    runner._confirmed_config = ('cfg',)
    page = SimpleNamespace(url='https://flow.google.com/project/p1')
    outcomes = [(None, False), (UUID, True)]
    reloads = []

    def fake_upload(page, path, upload_state, **kwargs):
        uuid, started = outcomes.pop(0)
        upload_state['started'] = started
        return uuid

    monkeypatch.setattr(V, '_upload_image_to_canvas', fake_upload)
    monkeypatch.setattr(V, 'flow_project_id', lambda url: 'p1')
    monkeypatch.setattr(V, '_canvas_upload_reload_safe', lambda page, pid: True)
    monkeypatch.setattr(V, '_reload_canvas_for_upload_trigger',
                        lambda page, pid: reloads.append(pid) or object())
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: object())
    result = runner._upload_references(page, [(0, req)])
    assert reloads == ['p1']
    assert runner._confirmed_config is None
    assert list(result.values()) == [UUID]


def test_stuck_upload_entry_never_reloads_a_busy_canvas(upload, monkeypatch):
    runner, req = _runner(upload)
    page = SimpleNamespace(url='https://flow.google.com/project/p1')

    def fail_upload(page, path, upload_state, **kwargs):
        upload_state['started'] = False
        upload_state['failure_reason'] = '当前上传入口未打开文件选择器'
        return None

    monkeypatch.setattr(V, '_upload_image_to_canvas', fail_upload)
    monkeypatch.setattr(V, 'flow_project_id', lambda url: 'p1')
    monkeypatch.setattr(V, '_canvas_upload_reload_safe', lambda page, pid: False)
    monkeypatch.setattr(V, '_reload_canvas_for_upload_trigger',
                        lambda *a: pytest.fail('busy canvas must not reload'))
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: object())
    with pytest.raises(RuntimeError, match='尚未开始上传'):
        runner._upload_references(page, [(0, req)])
