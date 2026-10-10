"""Keep funded profiles open; retire depleted profiles before replacement probes."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from integrations.google_fx.utils import account_binding, account_pool as ap, browser


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, '_STATE_FILE', tmp_path / 'account_pool.json')
    monkeypatch.setattr(ap, '_get_min_credit_threshold', lambda: 15)
    monkeypatch.setattr(ap.AccountPool, '_profile_info_map', lambda self: {})
    monkeypatch.setattr(browser, 'stop_ads_browser', lambda **kwargs: True)
    return ap.AccountPool()


def seed(pool, user_id, credit=100, **fields):
    pool.add_account(user_id, name=user_id, serial_number='1')
    state = ap._read_state()
    state[user_id].update(credit=credit, last_checked_at=ap._now_iso(),
                          last_probe_status='ok', credit_source='measured')
    state[user_id].update(fields)
    ap._write_state(state)


def opened(monkeypatch, *user_ids):
    monkeypatch.setattr(browser, 'list_running_ads_browsers',
                        lambda: [dict(user_id=uid) for uid in user_ids])


def test_open_funded_profile_wins_over_higher_balance_and_priority(pool, monkeypatch):
    seed(pool, 'open', 50)
    seed(pool, 'rich', 900)
    opened(monkeypatch, 'open')
    assert pool.pick_account(priority_user_ids=['rich'])['user_id'] == 'open'
    assert pool.pick_account(prefer_open=False)['user_id'] == 'rich'


def test_open_profile_exclusion_and_manual_disable_are_respected(pool, monkeypatch):
    seed(pool, 'open', 50)
    seed(pool, 'rich', 900)
    opened(monkeypatch, 'open', 'not-in-pool')
    assert pool.pick_open_account(exclude=['open']) is None
    pool.set_disabled('open', True)
    monkeypatch.setattr(browser, 'stop_ads_browser',
                        lambda **kwargs: pytest.fail('manual disable must not close a user window'))
    assert pool.pick_account()['user_id'] == 'rich'


def test_open_credit_is_rechecked_before_reuse(pool, monkeypatch):
    seed(pool, 'open', 50, last_checked_at='2020-01-01T00:00:00+00:00')
    seed(pool, 'rich', 900)
    opened(monkeypatch, 'open')
    probes = []

    def refresh(user_id, force=False):
        probes.append(user_id)
        pool.record_measured_credit(user_id, 30)
        return dict(ap._read_state()[user_id], user_id=user_id)

    monkeypatch.setattr(pool, 'refresh_credit', refresh)
    assert pool.pick_account()['user_id'] == 'open'
    assert probes == ['open']


def test_depleted_open_browser_closes_before_new_account_probe(pool, monkeypatch):
    seed(pool, 'empty', 0)
    seed(pool, 'next', None, last_checked_at=None)
    opened(monkeypatch, 'empty')
    events = []
    monkeypatch.setattr(browser, 'stop_ads_browser',
                        lambda user_id: events.append(('close', user_id)))

    def refresh(user_id, force=False):
        events.append(('probe', user_id))
        pool.record_measured_credit(user_id, 100)
        return dict(ap._read_state()[user_id], user_id=user_id)

    monkeypatch.setattr(pool, 'refresh_credit', refresh)
    assert pool.pick_account()['user_id'] == 'next'
    assert events == [('close', 'empty'), ('probe', 'next')]


def test_empty_probe_window_closes_before_trying_another_closed_profile(pool, monkeypatch):
    seed(pool, 'empty', 900, last_checked_at='2020-01-01T00:00:00+00:00')
    seed(pool, 'next', None, last_checked_at=None)
    events = []
    monkeypatch.setattr(browser, 'stop_ads_browser',
                        lambda user_id: events.append(('close', user_id)))

    def refresh(user_id, force=False):
        events.append(('probe', user_id))
        pool.record_measured_credit(user_id, 0 if user_id == 'empty' else 100)
        return dict(ap._read_state()[user_id], user_id=user_id)

    monkeypatch.setattr(pool, 'refresh_credit', refresh)
    assert pool.pick_account()['user_id'] == 'next'
    assert events == [('probe', 'empty'), ('close', 'empty'), ('probe', 'next')]


def test_cooldown_expiry_requires_measured_refill(pool, monkeypatch):
    seed(pool, 'empty', 80)
    pool.mark_exhausted('empty', cooldown_hours=1, credit=0)
    before = ap._now()
    monkeypatch.setattr(ap, '_now', lambda: before + timedelta(hours=2))
    state = ap._read_state()['empty']
    assert not state['disabled']
    assert state['credit'] == 0
    assert state['credit_recheck_required']
    from integrations.google_fx.services import google_fx_credit
    monkeypatch.setattr(google_fx_credit, 'probe_flow_credit', lambda *args: 120)
    chosen = pool.pick_account()
    assert chosen['user_id'] == 'empty'
    assert chosen['credit'] == 120
    assert not chosen.get('credit_recheck_required')


def test_still_empty_after_cycle_is_disabled_for_next_cycle(pool, monkeypatch):
    seed(pool, 'empty', 80)
    pool.mark_exhausted('empty', cooldown_hours=1, credit=0)
    before = ap._now()
    monkeypatch.setattr(ap, '_now', lambda: before + timedelta(hours=2))
    from integrations.google_fx.services import google_fx_credit
    monkeypatch.setattr(google_fx_credit, 'probe_flow_credit', lambda *args: 0)
    assert pool.pick_account() is None
    state = ap._read_state()['empty']
    assert state['disabled']
    assert ap._parse_iso(state['cooldown_until']) > ap._now()


def test_failed_probe_after_cycle_never_reuses_old_credit(pool, monkeypatch):
    seed(pool, 'empty', 80)
    pool.mark_exhausted('empty', cooldown_hours=1, credit=0)
    before = ap._now()
    monkeypatch.setattr(ap, '_now', lambda: before + timedelta(hours=2))
    from integrations.google_fx.services import google_fx_credit
    monkeypatch.setattr(google_fx_credit, 'probe_flow_credit', lambda *args: None)
    assert pool.account_is_usable('empty') is False
    assert pool.pick_account() is None
    state = ap._read_state()['empty']
    assert state['credit'] == 0
    assert state['credit_recheck_required']


def test_legacy_automatic_cooldown_deadline_is_persisted_once(pool, monkeypatch):
    seed(pool, 'legacy', 0, disabled=True, disabled_reason='zero_credit')
    first = ap._read_state()['legacy']['cooldown_until']
    before = ap._now()
    monkeypatch.setattr(ap, '_now', lambda: before + timedelta(hours=1))
    second = ap._read_state()['legacy']['cooldown_until']
    assert first == second
    monkeypatch.setattr(ap, '_now', lambda: before + timedelta(hours=25))
    assert ap._read_state()['legacy']['credit_recheck_required']


def test_manual_disabled_account_does_not_auto_reenable_after_cycle(pool, monkeypatch):
    seed(pool, 'manual', 0, disabled=True, cooldown_until=(ap._now() - timedelta(hours=1)).isoformat())
    assert ap._read_state()['manual']['disabled']
    assert pool.pick_account() is None


def test_background_credit_checks_keep_existing_window_and_respect_cooldown(pool, monkeypatch):
    seed(pool, 'open', 80, last_checked_at='2020-01-01T00:00:00+00:00')
    seed(pool, 'closed', 100, last_checked_at='2020-01-01T00:00:00+00:00')
    seed(pool, 'depleted', 0, last_checked_at='2020-01-01T00:00:00+00:00')
    opened(monkeypatch, 'open', 'depleted')
    assert [a['user_id'] for a in pool._silent_inspection_candidates()] == ['open']


def test_background_credit_checks_only_open_one_profile_from_idle(pool, monkeypatch):
    seed(pool, 'first', 80, last_checked_at='2020-01-01T00:00:00+00:00')
    seed(pool, 'second', 100, last_checked_at='2020-01-01T00:00:00+00:00')
    opened(monkeypatch)
    assert len(pool._silent_inspection_candidates()) == 1


@pytest.mark.parametrize('replacement', [None, {'user_id': 'next'}])
def test_switch_closes_before_selection_even_when_pool_empty(monkeypatch, replacement):
    events = []
    monkeypatch.setattr(browser, 'stop_ads_browser',
                        lambda user_id, port: events.append(('close', user_id)))
    monkeypatch.setattr(ap.AccountPool, 'pick_account',
                        lambda self, **kwargs: events.append(('pick', kwargs['exclude'])) or replacement)
    with account_binding.bound_task_account('current'):
        assert ap.switch_to_next_account() == replacement
    assert events == [('close', 'current'), ('pick', {'current'})]


def test_non_silent_browser_reuse_skips_start_proxy_and_window_changes(monkeypatch, ads_inventory_reader):
    calls = []
    ws = 'ws://127.0.0.1:9222/devtools/browser/alive'

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith('/browser/local-active'):
            return SimpleNamespace(json=lambda: {'code': 0, 'data': {'list': [{'user_id': 'open'}]}})
        assert url.endswith('/browser/active')
        return SimpleNamespace(json=lambda: {'code': 0, 'data': {'status': 'Active', 'ws': {'puppeteer': ws}}})

    monkeypatch.setattr(browser.requests, 'get', get)
    monkeypatch.setattr(browser, 'get_runtime_adspower_silent_mode', lambda: False)
    monkeypatch.setattr(browser, '_is_ws_port_open', lambda *args, **kwargs: True)
    from integrations.google_fx.utils import proxy_rotator
    monkeypatch.setattr(proxy_rotator, 'ProxyRotator', lambda: pytest.fail('must not rotate active browser'))
    monkeypatch.setattr(browser, 'suppress_browser_window', lambda *args, **kwargs: pytest.fail('must not touch active window'))
    assert browser.get_ads_ws_url(user_id='open') == ws
    assert len(calls) == 2


def test_closed_browser_status_does_not_reuse_stale_socket(monkeypatch):
    monkeypatch.setattr(browser.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        json=lambda: {'code': 0, 'data': {'status': 'Inactive'}}))
    monkeypatch.setattr(browser, '_find_running_browser_ws', lambda *args: pytest.fail('inactive is authoritative'))
    assert browser.get_running_ads_ws_url('closed') is None
