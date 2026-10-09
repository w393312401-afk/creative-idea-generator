"""Offline kernel-lock tests for SPARK / Flow credential maintenance admission."""
import contextlib
import fcntl
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from fx_control import FxAccountAdmissionBlocked, FxControlPlane, FxQueueTimeout


class Busy(Exception):
    reason = 'generation_busy'


class Guard:
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class LockFactory:
    def __init__(self, root):
        self.root, self.guards = root, []

    def open(self, profile):
        return os.open(self.root / (profile + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)

    def __call__(self, profile):
        if profile == 'unbound':
            return None
        fd = self.open(profile)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Busy() from None
        guard = Guard(fd)
        self.guards.append(guard)
        return guard

    @contextlib.contextmanager
    def maintenance(self, profile):
        fd = self.open(profile)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    factory = LockFactory(tmp_path)
    control = FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl', factory)
    return control, factory


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_explicit_profile_waits_for_maintenance_and_retries_without_browser_entry(setup):
    control, factory = setup
    entered, release = threading.Event(), threading.Event()

    def worker():
        with control.slot('frames-fixture', 'frames', want_account='a', wait_timeout=2):
            entered.set()
            release.wait(2)

    with factory.maintenance('a'):
        thread = threading.Thread(target=worker)
        thread.start()
        assert wait_until(lambda: bool(control.board()['waiting']))
        assert not entered.is_set()
        assert control.snapshot()['active_count'] == 0
        assert wait_until(lambda: control.board()['waiting'][0]['blocked_by'][0]['type'] == 'account_maintenance')
    assert entered.wait(2)
    with pytest.raises(BlockingIOError):
        with factory.maintenance('a'):
            pytest.fail('active generation must keep the shared account guard')
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    with factory.maintenance('a'):
        pass
    assert all(guard.fd is None for guard in factory.guards)


def test_maintained_profile_does_not_hold_up_another_account(setup):
    control, factory = setup
    entered, release = threading.Event(), threading.Event()

    def waiting():
        with control.slot('first', 'frames', want_account='a', wait_timeout=2):
            entered.set()
            release.wait(2)

    with factory.maintenance('a'):
        thread = threading.Thread(target=waiting)
        thread.start()
        assert wait_until(lambda: bool(control.snapshot()['waiting']))
        with control.slot('second', 'frames', want_account='b', wait_timeout=1):
            assert not entered.is_set()
            assert control.current_claim() == 'b'
    assert entered.wait(2)
    release.set()
    thread.join(2)
    assert not thread.is_alive()


def test_automatic_candidate_claim_is_guarded_before_use(setup):
    control, factory = setup
    with control.slot('automatic', 'frames'):
        with factory.maintenance('a'):
            assert control.claim_account('a') is False
            assert control.current_claim() is None
        assert control.claim_account('a') is True
        with pytest.raises(BlockingIOError):
            with factory.maintenance('a'):
                pass
    with factory.maintenance('a'):
        pass


def test_account_observer_cannot_bypass_a_denied_claim(setup):
    control, factory = setup
    with control.slot('automatic', 'frames'):
        with factory.maintenance('a'):
            with pytest.raises(FxAccountAdmissionBlocked, match='generation_busy'):
                control.note_account('a')
            assert control.current_claim() is None
        control.note_account('a')
        assert control.current_claim() == 'a'
        with pytest.raises(BlockingIOError):
            with factory.maintenance('a'):
                pass


def test_restore_and_switch_keep_all_touched_profiles_guarded_until_slot_exit(setup):
    control, factory = setup
    with control.slot('automatic', 'frames'):
        assert control.claim_account('a')
        control.restore_claim(None)
        assert control.claim_account('b')
        control.restore_claim('a')
        for profile in ('a', 'b'):
            with pytest.raises(BlockingIOError):
                with factory.maintenance(profile):
                    pass
    for profile in ('a', 'b'):
        with factory.maintenance(profile):
            pass
    assert all(guard.fd is None for guard in factory.guards)


def test_force_release_keeps_guard_until_worker_really_exits(setup):
    control, factory = setup
    entered, release = threading.Event(), threading.Event()

    def worker():
        with control.slot('stuck', 'frames', want_account='a'):
            entered.set()
            release.wait(2)
            assert control.claim_account('b') is False

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(2)
    control.force_release('stuck')
    with pytest.raises(BlockingIOError):
        with factory.maintenance('a'):
            pass
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    with factory.maintenance('a'):
        pass


def test_only_authenticated_host_kind_and_prefix_exempt_the_original_profile(setup):
    control, factory = setup
    with factory.maintenance('a'):
        with control.slot('cookie_login_fixture', 'auto_login', want_account='a', wait_timeout=0.2):
            assert control.claim_account('a')
        for task, kind in (('manual_login', 'auto_login'), ('cookie_login_fixture', 'frames')):
            with pytest.raises(FxQueueTimeout):
                with control.slot(task, kind, want_account='a', wait_timeout=0.05):
                    pytest.fail('ordinary tasks must not bypass account maintenance')


def test_trusted_login_cannot_use_a_different_profile_without_guard(setup):
    control, factory = setup
    with factory.maintenance('a'), factory.maintenance('b'):
        with control.slot('cookie_login_fixture', 'auto_login', want_account='a'):
            assert control.claim_account('b') is False
            with pytest.raises(FxAccountAdmissionBlocked):
                with control.slot('nested', 'credit_probe', want_account='b'):
                    pytest.fail('another profile is not covered by the host worker guard')


def test_nested_probes_acquire_and_retain_guards_for_new_profiles(setup):
    control, factory = setup
    with control.slot('outer', 'frames', want_account='a'):
        with control.slot('probe', 'credit_probe', want_account='b'):
            with pytest.raises(BlockingIOError):
                with factory.maintenance('b'):
                    pass
        with pytest.raises(BlockingIOError):
            with factory.maintenance('b'):
                pass
    with factory.maintenance('b'):
        pass


def test_unknown_factory_errors_fail_closed_without_private_details_in_board(setup):
    control, factory = setup

    def unavailable(_profile):
        raise RuntimeError('PRIVATE_FIXTURE_MUST_NOT_BE_PRINTED')

    control.account_admission_factory = unavailable
    with pytest.raises(FxQueueTimeout):
        with control.slot('fixture', 'frames', want_account='a', wait_timeout=0.05):
            pytest.fail('unreadable admission must not grant browser access')
    assert 'PRIVATE_FIXTURE' not in json.dumps(control.board())
    with control.slot('automatic', 'frames'):
        assert not control.claim_account('a')
        with pytest.raises(FxAccountAdmissionBlocked):
            control.note_account('a')


def test_cancellation_and_grant_failure_release_every_acquired_guard(setup, monkeypatch):
    control, factory = setup
    with pytest.raises(RuntimeError, match='fixture failure'):
        with control.slot('fixture', 'frames', want_account='a'):
            raise RuntimeError('fixture failure')
    assert all(guard.fd is None for guard in factory.guards)
    monkeypatch.setattr(control, '_grant_locked', lambda *_args: (_ for _ in ()).throw(RuntimeError('fixture grant')))
    with pytest.raises(RuntimeError, match='fixture grant'):
        with control.slot('fixture', 'frames', want_account='a'):
            pass
    assert all(guard.fd is None for guard in factory.guards)
    with factory.maintenance('a'):
        pass


def test_unbound_profiles_and_absent_callback_keep_existing_behavior(setup):
    control, factory = setup
    with control.slot('unbound-fixture', 'frames', want_account='unbound'):
        assert control.claim_account('unbound')
    control.account_admission_factory = None
    with factory.maintenance('a'):
        with control.slot('legacy-fixture', 'frames', want_account='a'):
            assert control.claim_account('a')


@pytest.mark.parametrize('occupancy', ['idle', 'maintenance', 'generation'])
def test_cleanup_protects_real_bound_account_and_releases_probe_guard(tmp_path, occupancy):
    import importlib.util
    from pathlib import Path

    # Load only the stdlib lock module, as the live SPARK bridge does, without
    # requiring Flow's optional package dependencies or touching its database.
    source = Path('/Users/fly/flow2api/src/core/account_admission.py')
    if not source.exists():
        pytest.skip('requires local Flow integration checkout')
    spec = importlib.util.spec_from_file_location('_flow_cleanup_admission_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    admission = module.AccountAdmission(tmp_path / 'flow.db')
    probes = []

    def factory(profile):
        assert profile == 'bound'
        guard = admission.generation(3)
        probes.append(guard)
        return guard

    control = FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl', factory)
    occupant = (contextlib.nullcontext() if occupancy == 'idle'
                else getattr(admission, occupancy)(3))
    with occupant:
        assert control.protected_from_cleanup('bound') is True
        assert len(probes) == (0 if occupancy == 'maintenance' else 1)
        assert all(guard.pass_fds == () for guard in probes)
        assert control.snapshot()['active_count'] == 0
        assert not control.board()['waiting']
        if occupancy == 'generation':
            with pytest.raises(module.AccountAdmissionBusy, match='generation_busy'):
                admission.maintenance(3)
        elif occupancy == 'maintenance':
            with pytest.raises(module.AccountAdmissionBusy, match='generation_busy'):
                admission.generation(3)
        else:
            with admission.maintenance(3):
                pass
    # A cleanup probe must not leave either owner or worker shared locks behind.
    with admission.maintenance(3):
        pass


def test_cleanup_keeps_unbound_profile_eligible_and_does_not_leak_other_guard(setup):
    control, factory = setup
    assert control.protected_from_cleanup('a') is True
    assert control.protected_from_cleanup('unbound') is False
    assert len(factory.guards) == 1
    assert factory.guards[0].fd is None
    with factory.maintenance('a'):
        pass


def test_cleanup_without_flow_integration_keeps_legacy_eligibility(setup):
    control, factory = setup
    control.account_admission_factory = None
    with factory.maintenance('a'):
        assert control.protected_from_cleanup('a') is False
    assert factory.guards == []


@pytest.mark.parametrize('failure', [
    'busy', 'unknown', 'false_guard', 'missing_close', 'invalid_close', 'close_raises',
])
def test_cleanup_preserves_profile_when_binding_or_probe_is_uncertain(setup, failure, capsys, caplog):
    control, _factory = setup
    calls = []
    private_detail = 'PRIVATE_CLEANUP_FIXTURE'

    def close():
        calls.append('close')
        raise RuntimeError(private_detail)

    def factory(profile):
        calls.append(profile)
        if failure == 'busy':
            raise Busy(private_detail)
        if failure == 'unknown':
            raise RuntimeError(private_detail)
        return {
            'false_guard': False,
            'missing_close': object(),
            'invalid_close': SimpleNamespace(close=None),
            'close_raises': SimpleNamespace(close=close),
        }[failure]

    control.account_admission_factory = factory
    assert control.protected_from_cleanup('a') is True
    assert calls == (['a', 'close'] if failure == 'close_raises' else ['a'])
    assert control.snapshot()['active_count'] == 0
    assert not control.board()['waiting']
    captured = capsys.readouterr()
    assert private_detail not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize('bridge_state', ['loaded', 'loader_error', 'missing', 'uninstalled'])
def test_bootstrap_installs_bridge_or_safe_rejection_without_cross_project_dependencies(setup, monkeypatch, bridge_state):
    import importlib.util
    import server
    from integrations.google_fx.utils import account_binding, browser_gate, lease_registry

    control, factory = setup
    control.account_admission_factory = None
    bridge = SimpleNamespace(profile_generation_lease=factory)
    loaded = []

    def load(_module):
        loaded.append(bridge_state)
        if bridge_state == 'loader_error':
            raise RuntimeError('PRIVATE_LOADER_FAILURE')

    original_isdir = os.path.isdir
    original_isfile = os.path.isfile
    monkeypatch.setattr(os.path, 'isdir', lambda path:
        bridge_state != 'uninstalled' if path == '/Users/fly/flow2api' else original_isdir(path))
    monkeypatch.setattr(os.path, 'isfile', lambda path:
        bridge_state != 'missing' if path == '/Users/fly/flow2api/local/account_admission_bridge.py' else original_isfile(path))
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', lambda *_: SimpleNamespace(loader=SimpleNamespace(exec_module=load)))
    monkeypatch.setattr(importlib.util, 'module_from_spec', lambda _: bridge)
    monkeypatch.setattr(server, 'FX_CONTROL', control)
    monkeypatch.setattr(server, 'apply_google_fx_runtime_overrides', lambda *_: None)
    monkeypatch.setattr(server, 'apply_direct_env', lambda *_: None)
    monkeypatch.setattr(server.FX_CONFIG, 'migrate_deprecated_values', lambda: None)
    for name in ('_FX_WATCHDOG_STARTED', '_FX_SELECTOR_DRIFT_STARTED', '_FX_OPEN_RECONCILE_STARTED'):
        monkeypatch.setattr(server, name, SimpleNamespace(is_set=lambda: True, set=lambda: None))
    messages = []
    monkeypatch.setattr(server, 'log', lambda *args, **_: messages.append(args))
    try:
        server.bootstrap_fx_runtime()
        if bridge_state in ('loader_error', 'missing'):
            with control.slot('automatic', 'frames'):
                assert control.claim_account('a') is False
            with pytest.raises(FxQueueTimeout):
                with control.slot('explicit', 'frames', want_account='a', wait_timeout=.02):
                    pytest.fail('unavailable installed integration must block browser entry')
            assert 'PRIVATE_LOADER_FAILURE' not in json.dumps(messages)
            assert any('busy_state_unknown' in str(message) for message in messages)
        elif bridge_state == 'uninstalled':
            assert control.account_admission_factory is None
            with factory.maintenance('a'):
                with control.slot('legacy', 'frames', want_account='a'):
                    assert control.claim_account('a')
        else:
            assert control.account_admission_factory is factory
        assert loaded == ([bridge_state] if bridge_state in ('loaded', 'loader_error') else [])
    finally:
        browser_gate.install(None)
        lease_registry.install(None)
        account_binding.install_pin_resolver(None)
        account_binding.install_account_observer(None)
