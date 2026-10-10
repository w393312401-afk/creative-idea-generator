"""The automatic video child may share a project, never its parent's browser.

All browser calls, egress resolution and account checks are local mocks.
"""
import threading

import pytest

from fx_control import FxControlPlane, current_fx_task_id, FxQueueCancelled
from integrations.google_fx.utils import account_binding as binding
from integrations.google_fx.utils import browser, lease_registry


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '2')
    monkeypatch.setenv('SPARK_FX_EGRESS_POLICY', 'hard')
    plane = FxControlPlane(tmp_path / 'state.json', tmp_path / 'audit.jsonl')
    plane.egress_resolver = lambda account: f'egress-{account}'
    monkeypatch.setattr(lease_registry, '_PROVIDER', plane)
    monkeypatch.setattr(binding, '_ACCOUNT_OBSERVER', plane.note_account)
    return plane


def hold_parent(control, account='image', kind='frames'):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            with control.slot('parent', kind, project_key='project', want_account=account):
                with binding.bound_task_account(account):
                    ready.set()
                    release.wait(3)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(2)
    return release, thread, errors


@pytest.mark.parametrize('parent_kind', ['frames', 'frames_selection'])
def test_auto_child_shares_only_its_parent_project_with_a_different_profile(control, parent_kind):
    release, parent, errors = hold_parent(control, kind=parent_kind)
    try:
        with control.slot('child', 'videos', project_key='project', want_account='video',
                          parallel_parent_task_id='parent', wait_timeout=1):
            with binding.bound_task_account('video'):
                assert current_fx_task_id() == 'child'
                assert control.current_claim() == 'video'
                assert control.leased_user_ids(exclude_current=False) == {'image', 'video'}
                assert {row['task_id'] for row in control.board()['leases']} == {'parent', 'child'}
    finally:
        release.set()
        parent.join(2)
    assert not errors


@pytest.mark.parametrize('case', ['capacity', 'same_profile', 'same_egress'])
def test_child_waits_without_consuming_the_parent_profile(control, monkeypatch, case):
    account = 'image' if case == 'same_profile' else 'video'
    if case == 'capacity':
        monkeypatch.setenv('SPARK_FX_MAX_CONCURRENT', '1')
    if case == 'same_egress':
        control.egress_resolver = lambda _: 'shared-egress'
    release, parent, errors = hold_parent(control)
    entered = threading.Event()

    def child():
        try:
            with control.slot('child', 'videos', project_key='project', want_account=account,
                              parallel_parent_task_id='parent', wait_timeout=2):
                entered.set()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=child)
    worker.start()
    try:
        assert not entered.wait(0.05)
        assert control.leased_user_ids(exclude_current=False) == {'image'}
        assert len(control.board()['leases']) == 1
    finally:
        release.set()
        parent.join(2)
        worker.join(2)
    assert entered.is_set()
    assert not errors


@pytest.mark.parametrize('parent_id,kind', [('unrelated', 'videos'), ('parent', 'frames')])
def test_r2_exception_cannot_apply_to_other_tasks_or_image_workers(control, parent_id, kind):
    release, parent, errors = hold_parent(control)
    cancelled = threading.Event()
    result = []

    def child():
        try:
            with control.slot('child', kind, project_key='project', want_account='video',
                              parallel_parent_task_id=parent_id, cancel_check=cancelled.is_set):
                result.append('entered')
        except FxQueueCancelled:
            result.append('cancelled')

    worker = threading.Thread(target=child)
    worker.start()
    try:
        assert not cancelled.wait(0.05)
        cancelled.set()
        worker.join(2)
        assert result == ['cancelled']
    finally:
        cancelled.set()
        release.set()
        parent.join(2)
        worker.join(2)
    assert not errors


def test_r2_still_blocks_a_third_lease_of_the_same_project(control):
    control._leases = {
        1: {'seg_id': 1, 'task_id': 'parent', 'kind': 'frames', 'project_key': 'project', 'user_id': 'image'},
        2: {'seg_id': 2, 'task_id': 'other', 'kind': 'videos', 'project_key': 'project', 'user_id': 'other'},
    }
    blockers = control._blockers_locked({'task_id': 'child', 'kind': 'videos',
        'project_key': 'project', 'parallel_parent_task_id': 'parent', 'want_account': 'video'})
    assert any(row['type'] == 'project' and row['holder_task'] == 'other' for row in blockers)


def test_manual_and_empty_pool_fallback_cannot_bypass_an_atomic_claim(control, monkeypatch):
    release, parent, errors = hold_parent(control)
    monkeypatch.setattr(browser, 'get_runtime_default_user_id', lambda: 'image')
    monkeypatch.setattr(browser, 'get_running_ads_ws_url', lambda *a, **k: pytest.fail('Must claim before browser I/O'))
    try:
        with control.slot('child', 'videos', project_key='project', parallel_parent_task_id='parent'):
            with binding.bound_task_account('video'):
                with pytest.raises(lease_registry.AccountLeaseConflict):
                    binding.set_task_account('image')
                assert binding.current_task_account() == 'video'
                assert control.current_claim() == 'video'
            assert binding.resolve_account(fallback='image') == 'video', 'An existing claim wins over a changed process default'
            control.restore_claim(None)
            with pytest.raises(lease_registry.AccountLeaseConflict):
                binding.resolve_account(fallback='image')
            with pytest.raises(lease_registry.AccountLeaseConflict):
                browser.get_ads_ws_url(user_id='image', port=1)
            with pytest.raises(lease_registry.AccountLeaseConflict):
                browser.get_ads_ws_url(port=1)
    finally:
        release.set()
        parent.join(2)
    assert not errors


def test_claim_exception_is_fail_closed_but_standalone_binding_is_unchanged(monkeypatch):
    class Broken:
        def claim_account(self, user_id):
            raise RuntimeError('unavailable')

    monkeypatch.setattr(lease_registry, '_PROVIDER', Broken())
    with pytest.raises(lease_registry.AccountLeaseConflict):
        binding.set_task_account('unverified')
    assert binding.current_task_account() is None
    monkeypatch.setattr(lease_registry, '_PROVIDER', None)
    with binding.bound_task_account('standalone'):
        assert binding.resolve_account() == 'standalone'


def test_video_profile_startup_does_not_hold_the_image_profile_lifecycle_lock(control, monkeypatch):
    child_starting, release_child, image_connected = (threading.Event() for _ in range(3))
    cleaned = []
    errors = []
    monkeypatch.setattr(browser, 'get_running_ads_ws_url', lambda account, port=None: 'image-ws' if account == 'image' else None)
    monkeypatch.setattr(browser, 'ensure_profile_exclusive', lambda account, port=None: cleaned.append(account))

    def start(account, *args):
        if account == 'video':
            child_starting.set()
            assert release_child.wait(3)
        return f'{account}-ws'

    monkeypatch.setattr(browser, '_start_or_reuse_ads_browser', start)
    parent_ready = threading.Event()

    def image():
        try:
            with control.slot('parent', 'frames', project_key='project', want_account='image'):
                with binding.bound_task_account('image'):
                    parent_ready.set()
                    assert child_starting.wait(2)
                    assert browser.get_ads_ws_url(port=1) == 'image-ws'
                    image_connected.set()
                    release_child.wait(3)
        except BaseException as error:
            errors.append(error)

    def video():
        try:
            with control.slot('child', 'videos', project_key='project', want_account='video',
                              parallel_parent_task_id='parent', wait_timeout=2):
                with binding.bound_task_account('video'):
                    assert browser.get_ads_ws_url(port=1) == 'video-ws'
        except BaseException as error:
            errors.append(error)

    parent = threading.Thread(target=image)
    child = threading.Thread(target=video)
    parent.start()
    assert parent_ready.wait(2)
    child.start()
    try:
        assert child_starting.wait(2)
        assert image_connected.wait(1), 'Image connection must proceed while video startup is still waiting'
        assert cleaned == ['video'], 'A leased warm image profile bypasses another profile startup cleanup'
    finally:
        release_child.set()
        parent.join(2)
        child.join(2)
    assert not errors


def test_repeated_claim_of_an_owned_profile_does_not_repeat_egress_io(control):
    called = []
    control.egress_resolver = lambda account: called.append(account) or f'egress-{account}'
    with control.slot('parent', 'frames', project_key='project', want_account='image'):
        assert called == ['image']
        for _ in range(10):
            lease_registry.require_claim('image')
        assert called == ['image']


def test_warm_image_lease_ignores_a_video_task_changed_global_default(control, monkeypatch):
    with control.slot('parent', 'frames', project_key='project', want_account='image'):
        with binding.bound_task_account(None):
            assert binding.resolve_account(fallback='video-default') == 'image'
            monkeypatch.setattr(browser, 'get_runtime_default_user_id', lambda: 'video-default')
            monkeypatch.setattr(browser, 'get_running_ads_ws_url', lambda *a, **k: 'image-ws')
            monkeypatch.setattr(browser, '_start_or_reuse_ads_browser', lambda account, *a: f'{account}-ws')
            assert browser.get_ads_ws_url(port=1) == 'image-ws'
