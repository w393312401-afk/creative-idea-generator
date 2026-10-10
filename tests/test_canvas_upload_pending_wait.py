"""Slow pending uploads get a bounded wait without weakening frame ownership."""

from contextlib import contextmanager

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from integrations.google_fx.services import google_fx_video as V


OLD = '11111111-1111-4111-8111-111111111111'
NEW = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    path = tmp_path / 'img_010.webp'
    path.write_bytes(b'frame')
    env = SimpleNamespace(now=0, ready_at=120, pending_before=0, pending_after=1,
                          old=set(), result=[NEW], network=[], logs=[], escapes=0,
                          uploads=0, listeners=[])

    class Input:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def set_files(self, path):
            env.uploads += 1
            for url in env.network:
                env.listeners[0](SimpleNamespace(url=url))

    class Page:
        @contextmanager
        def expect_file_chooser(self, **kwargs):
            yield SimpleNamespace(value=Input())

        def on(self, event, handler):
            env.listeners.append(handler)

        def remove_listener(self, event, handler):
            env.listeners.remove(handler)

        def evaluate(self, script, filename):
            assert filename == 'img_010.webp'
            return env.pending_before if not env.uploads else env.pending_after

    monkeypatch.delenv('GOOGLE_FX_CANVAS_UPLOAD_MAX_WAIT_SECONDS', raising=False)
    monkeypatch.setattr(V.time, 'time', lambda: env.now)
    monkeypatch.setattr(V.time, 'sleep', lambda seconds: setattr(env, 'now', env.now + seconds))
    monkeypatch.setattr(V, '_open_canvas_upload_menu', lambda page: True)
    monkeypatch.setattr(V, '_find_add2_btn', lambda page: object())
    monkeypatch.setattr(V, '_find_canvas_upload_action',
                        lambda page, trigger: SimpleNamespace(click=lambda **kwargs: None))
    monkeypatch.setattr(V, '_safe_press_escape', lambda *a: setattr(env, 'escapes', env.escapes + 1))
    monkeypatch.setattr(V, '_get_panel_uuids', lambda page: env.old)
    monkeypatch.setattr(V, '_get_panel_uuid_order', lambda page, **kw:
                        list(env.old) + (env.result if env.now >= env.ready_at else []))
    monkeypatch.setattr(V, '_make_response_handler', lambda captured, **kw:
                        lambda response: captured.append((env.now, response.url)))
    monkeypatch.setattr(V, '_check_cancelled', lambda: None)
    monkeypatch.setattr(V, '_LAST_CONFIRMED_UPLOAD_EXPIRES', 0)
    monkeypatch.setattr(V, 'log', lambda message, *a: env.logs.append(message))
    env.run = lambda: V._upload_image_to_canvas(Page(), str(path))
    return env


def test_pending_upload_finishes_after_the_original_timeout(upload_env):
    env = upload_env
    assert env.run() == NEW
    assert env.now == 120
    assert env.uploads == 1
    assert sum('延长画布确认等待' in message for message in env.logs) == 1
    assert env.listeners == []
    assert env.escapes == 1


@pytest.mark.parametrize('configured,expected', [(None, 180), ('90', 90), ('9999', 600), ('bad', 180)])
def test_pending_upload_stops_at_a_configurable_total_limit(upload_env, monkeypatch, configured, expected):
    env = upload_env
    env.ready_at = 10000
    if configured is not None:
        monkeypatch.setenv('GOOGLE_FX_CANVAS_UPLOAD_MAX_WAIT_SECONDS', configured)
    assert env.run() is None
    assert env.now == expected
    assert env.uploads == 1
    assert env.listeners == []


@pytest.mark.parametrize('pending', [0, 2, None])
def test_missing_ambiguous_or_unreadable_pending_card_does_not_extend(upload_env, pending):
    env = upload_env
    env.pending_after = pending
    assert env.run() is None
    assert env.now == 45


def test_cancel_during_extended_wait_cleans_up_without_reupload(upload_env, monkeypatch):
    env = upload_env

    def cancel():
        if env.now >= 60:
            raise RuntimeError('任务已取消')

    monkeypatch.setattr(V, '_check_cancelled', cancel)
    with pytest.raises(RuntimeError, match='任务已取消'):
        env.run()
    assert env.now == 60
    assert env.uploads == 1
    assert env.listeners == []
    assert env.escapes == 1


def test_old_same_name_pending_upload_cannot_be_claimed_as_this_upload(upload_env):
    env = upload_env
    env.pending_before = 1
    env.ready_at = 20  # The older unknown upload finishes during this attempt.
    assert env.run() is None
    assert env.now == 45


def test_old_same_name_completed_image_stays_excluded_during_extended_wait(upload_env):
    env = upload_env
    env.old = {OLD}
    env.network = [f'https://flow-content.google/image/{OLD}']
    assert env.run() == NEW
    assert env.now == 120


def test_unrelated_network_response_never_becomes_upload_identity(upload_env):
    env = upload_env
    env.result = []
    env.network = [f'https://flow-content.google/image/{OTHER}']
    assert env.run() is None
    assert env.now == 180


def test_multiple_late_uuid_candidates_are_not_guessed(upload_env):
    env = upload_env
    env.result = [NEW, OTHER]
    assert env.run() is None
    assert env.now == 180


def test_pending_dom_probe_requires_exact_filename_and_pending_element():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to exercise the DOM probe JavaScript')

    class Page:
        def evaluate(self, script, filename):
            program = """
                const fs = require('fs');
                const input = JSON.parse(fs.readFileSync(0, 'utf8'));
                const rows = [
                    ['img_010.webp', true], ['img_010.webp', false],
                    ['img_010.webp.old', true], ['img_011.webp', true]
                ];
                const document = {querySelectorAll: selector => {
                    if (selector !== 'flow-grid-tile-container') throw Error(selector);
                    return rows.map(([name, pending]) => ({
                        getAttribute: key => key === 'aria-label' ? name : null,
                        querySelector: key => key === 'flow-pending-tile' && pending
                    }));
                }};
                process.stdout.write(JSON.stringify(eval('(' + input.script + ')')(input.filename)));
            """
            result = subprocess.run([node, '-e', program], input=json.dumps(dict(script=script, filename=filename)),
                                    text=True, capture_output=True, check=True)
            return json.loads(result.stdout)

    assert V._pending_canvas_upload_count(Page(), 'img_010.webp') == 1

