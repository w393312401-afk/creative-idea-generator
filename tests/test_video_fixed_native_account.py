"""Strict account scope: no browser/network actions or production state writes."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from integrations.google_fx.services import google_fx_helpers as H
from integrations.google_fx.services import google_fx_video as V
from integrations.google_fx.utils import account_binding as A, account_pool as P, browser as B


def test_fixed_scope_restores_and_rejects_all_identity_overrides(monkeypatch):
    monkeypatch.setenv('ADSPOWER_DEFAULT_USER_ID', 'default')
    with A.bound_task_account('outer'):
        with A.bound_fixed_task_account('fixed'):
            assert A.current_fixed_task_account() == A.current_task_account() == 'fixed'
            assert A.resolve_account(fallback='different') == 'fixed'
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(A.current_fixed_task_account).result() is None
            for action in (lambda: A.set_task_account('other'),
                           lambda: A.resolve_account(explicit='other'),
                           lambda: A.bound_task_account('other').__enter__()):
                with pytest.raises(A.FixedAccountStopError):
                    action()
            with A.bound_fixed_task_account('fixed'):
                assert A.current_task_account() == 'fixed'
        assert A.current_task_account() == 'outer'
        assert A.current_fixed_task_account() is None
    assert A.current_task_account() is None


def test_fixed_switch_fails_before_closing_or_probing(monkeypatch):
    close, pick = Mock(), Mock()
    monkeypatch.setattr(B, 'stop_ads_browser', close)
    monkeypatch.setattr(P.AccountPool, 'pick_account', pick)
    monkeypatch.setattr(P, '_get_min_credit_threshold', lambda: pytest.fail('must stop before config lookup'))
    with A.bound_fixed_task_account('fixed'), pytest.raises(A.FixedAccountStopError):
        P.switch_to_next_account()
    close.assert_not_called()
    pick.assert_not_called()


def test_fixed_browser_start_does_not_rotate_or_close_other_profiles(monkeypatch):
    start, exclusive = Mock(return_value='local-ws'), Mock()
    monkeypatch.setattr(B, '_start_or_reuse_ads_browser', start)
    monkeypatch.setattr(B, 'ensure_profile_exclusive', exclusive)
    with A.bound_fixed_task_account('fixed'):
        assert B.get_ads_ws_url(port=123, auto_rotate_proxy=True) == 'local-ws'
    assert start.call_args.args[:3] == ('fixed', 123, False)
    exclusive.assert_not_called()


@pytest.mark.parametrize('existing_flow', [False, True])
def test_fixed_page_selection_preserves_user_tabs(monkeypatch, existing_flow):
    unrelated = SimpleNamespace(url='https://example.test/user-work', close=Mock(), goto=Mock())
    flow = SimpleNamespace(url='https://flow.google.com/project/test', close=Mock(), goto=Mock())
    context = SimpleNamespace(pages=[unrelated] + ([flow] if existing_flow else []), new_page=Mock(return_value=flow))
    monkeypatch.setattr(B, '_is_manageable_user_page', lambda p: True)
    monkeypatch.setattr(B, 'bring_page_to_front_if_allowed', lambda p: None)
    with A.bound_fixed_task_account('fixed'):
        assert B.find_or_create_page(context, 'flow.google.com', auto_login=False) is flow
    unrelated.close.assert_not_called()
    unrelated.goto.assert_not_called()
    flow.close.assert_not_called()
    assert context.new_page.call_count == (0 if existing_flow else 1)


@pytest.mark.parametrize('code', ['login_required', 'verification_required', 'captcha', 'security_check'])
def test_fixed_manual_intervention_stops_before_auto_login_or_wait(monkeypatch, code):
    monkeypatch.setattr(H, '_probe_manual_intervention', lambda *a: (code, 'needs attention'))
    login = Mock()
    monkeypatch.setattr(B, 'attempt_auto_login', login)
    with A.bound_fixed_task_account('fixed'), pytest.raises(A.FixedAccountStopError):
        H.wait_out_manual_intervention(object())
    login.assert_not_called()


def test_fixed_browser_auto_login_stops_before_credentials_access():
    with A.bound_fixed_task_account('fixed'), pytest.raises(A.FixedAccountStopError):
        B.attempt_auto_login(object())


def test_fixed_connection_failure_does_not_restart_or_close_profile(monkeypatch):
    stop = Mock()
    monkeypatch.setattr(H.requests, 'get', stop)
    monkeypatch.setattr(H, '_check_cancelled', lambda: None)
    monkeypatch.setattr(H, 'get_ads_ws_url', Mock(side_effect=RuntimeError('unavailable')))
    with A.bound_fixed_task_account('fixed'), pytest.raises(RuntimeError, match='unavailable'):
        H._connect_fx_page(object())
    H.get_ads_ws_url.assert_called_once()
    stop.assert_not_called()


@pytest.mark.parametrize('failure', [
    V._UnusualActivityError('unusual activity'), V._IPBlockedError('captcha'),
    V._CreditExhaustedError('credit exhausted'), RuntimeError('MANUAL_REQUIRED:login_required'),
    RuntimeError('Target page was closed'),
])
def test_fixed_runner_stops_once_and_preserves_complete_and_paid_pending(monkeypatch, failure):
    events = []
    reqs = [SimpleNamespace(prompt='test') for _ in range(3)]
    runner = V._ChunkRunner(3, 0, reqs, {}, lambda *a: events.append(a), None)
    runner._active_account_id = 'fixed'
    runner.project_url = 'original-project'
    runner.completed.add(0)
    runner.results[0] = dict(status='success', video_url='/saved.mp4')
    runner._submitted_tasks[1] = dict(status='generating', sub_idx=1, tile_id='paid',
                                     account_id='fixed', project_url='original-project',
                                     submission_id='original', fixed_video_account=True)
    run = Mock(side_effect=failure)
    monkeypatch.setattr(runner, '_run_round', run)
    monkeypatch.setattr(runner, '_mark_current_credit_exhausted', lambda *a: None)
    monkeypatch.setattr(runner, '_recover_unusual_activity', lambda: pytest.fail('no proxy rotation'))
    monkeypatch.setattr(runner, '_cooldown_and_switch_account', lambda: pytest.fail('no account switch'))
    with A.bound_fixed_task_account('fixed'):
        result = runner.run()
    run.assert_called_once()
    assert result[0]['video_url'] == '/saved.mp4'
    assert result[1]['submission_pending'] is True
    assert result[1]['submission_id'] == 'original'
    assert result[2]['status'] == 'failed'
    assert not [stage for _, stage, _ in events if stage == 'request_submitted']
    assert any(stage == 'request_resolved' for _, stage, _ in events)
    assert any(details.get('code') == 'fixed_video_account_stopped' for _, _, details in events)


def test_fixed_uncertain_receipt_is_never_released_for_resubmission():
    runner = V._ChunkRunner(1, 0, [SimpleNamespace(prompt='test')], {}, None, None)
    runner._unresolved_identity_subs.add(0)
    runner.results[0] = dict(submission_pending=True, submission_id='original')
    with A.bound_fixed_task_account('fixed'):
        assert runner._retry_uncertain_submissions(object(), [(0, runner.chunk[0])]) == []
    assert runner._unresolved_identity_subs == {0}
    assert runner.results[0]['submission_id'] == 'original'


def test_pending_preservation_never_promotes_unknown_click_to_confirmed():
    events = []
    runner = V._ChunkRunner(1, 0, [SimpleNamespace(prompt='test')], {}, lambda *a: events.append(a), None)
    runner._submitted_tasks[0] = dict(status='generating', confirmed=False, submission_id='unknown')
    with A.bound_fixed_task_account('fixed'):
        runner._preserve_fixed_pending()
    assert events[0][1] == 'request_resolved'
    assert events[0][2]['confirmed'] is False
    assert events[0][2]['submission_pending'] is True
