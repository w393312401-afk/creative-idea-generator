# -*- coding: utf-8 -*-
"""序列生成默认浏览器环境（googleFxSequenceUserId / googleFxSequenceUserLock）。

优先级契约：手动 googleFxUserId > 序列默认环境（仅当此刻确实可用）> 号池自动选号。
默认环境不可用时必须**降级到自动选号**而不是让整批失败——但也不能装作用了它，
所以选号结果一律以返回值/写回 config 的 user_id 为准。

锁定只在默认环境真的被选中时生效：降级之后再锁，锁住的是替补账号，不是用户钉的
那个环境。
"""

import unittest
from unittest.mock import patch

import server_common
from fx_console import FX_CONFIG_SPEC, validate_patch
from server_common import (
    _account_rotation_ring,
    _select_pool_account,
    sequence_account_locked,
    sequence_default_account,
)


class _FakePool:
    def __init__(self, accounts=None, chosen=None):
        self._accounts = accounts or []
        self._chosen = chosen
        self.pick_calls = []

    def list_accounts(self):
        return list(self._accounts)

    def pick_account(self, min_credit=1, *args, **kwargs):
        self.pick_calls.append(min_credit)
        return self._chosen


def _account(user_id, **overrides):
    row = {'user_id': user_id, 'credit': 100, 'disabled': False, 'cooldown_until': None}
    row.update(overrides)
    return row


class TestSequenceDefaultAccountSelection(unittest.TestCase):

    def test_default_environment_is_used_when_available(self):
        pool = _FakePool(accounts=[_account('a'), _account('b')], chosen={'user_id': 'a'})
        config = {'googleFxSequenceUserId': 'b'}
        self.assertEqual(_select_pool_account(config, pool), 'b')
        self.assertEqual(config['googleFxUserId'], 'b')
        self.assertEqual(pool.pick_calls, [])  # 钉了可用环境就不必再自动选号

    def test_manual_user_id_still_wins_over_sequence_default(self):
        pool = _FakePool(accounts=[_account('a'), _account('b')], chosen={'user_id': 'a'})
        config = {'googleFxUserId': 'manual', 'googleFxSequenceUserId': 'b'}
        self.assertIsNone(_select_pool_account(config, pool))
        self.assertEqual(config['googleFxUserId'], 'manual')

    def test_empty_default_falls_through_to_auto_pick(self):
        pool = _FakePool(accounts=[_account('a')], chosen={'user_id': 'a'})
        config = {'googleFxSequenceUserId': '   '}
        self.assertEqual(_select_pool_account(config, pool), 'a')
        self.assertEqual(pool.pick_calls, [1])

    def test_unusable_default_degrades_to_auto_pick(self):
        """禁用/冷却/积分不足/根本不在池子里 —— 四种都必须降级，而不是硬用或报错。"""
        cases = {
            'missing': ([_account('a')], 'ghost'),
            'disabled': ([_account('a'), _account('b', disabled=True)], 'b'),
            'cooling': ([_account('a'),
                         _account('b', cooldown_until='2999-01-01T00:00:00+00:00')], 'b'),
            'low_credit': ([_account('a'), _account('b', credit=0)], 'b'),
        }
        for name, (accounts, preferred) in cases.items():
            with self.subTest(name):
                pool = _FakePool(accounts=accounts, chosen={'user_id': 'a'})
                config = {'googleFxSequenceUserId': preferred,
                          'videoAccountPoolMinCredit': 1}
                self.assertEqual(_select_pool_account(config, pool), 'a')
                self.assertEqual(config['googleFxUserId'], 'a')
                self.assertEqual(pool.pick_calls, [1])

    def test_expired_cooldown_does_not_block_the_default(self):
        pool = _FakePool(accounts=[_account('a'),
                                   _account('b', cooldown_until='2000-01-01T00:00:00+00:00')],
                         chosen={'user_id': 'a'})
        config = {'googleFxSequenceUserId': 'b'}
        self.assertEqual(_select_pool_account(config, pool), 'b')

    def test_unprobed_default_is_allowed(self):
        """积分未知（从未探测）不算积分不足——与 _account_has_credit 同一口径。"""
        pool = _FakePool(accounts=[_account('a'), _account('b', credit=None)],
                         chosen={'user_id': 'a'})
        config = {'googleFxSequenceUserId': 'b'}
        self.assertEqual(_select_pool_account(config, pool), 'b')


class TestSequenceAccountLock(unittest.TestCase):

    def test_lock_collapses_rotation_ring_to_one_account(self):
        pool = _FakePool(accounts=[_account('a'), _account('b'), _account('c')])
        config = {'googleFxSequenceUserId': 'b', 'googleFxSequenceUserLock': True}
        self.assertEqual(_account_rotation_ring(config, pool, 'b'), ['b'])

    def test_unlocked_default_still_rotates_through_the_pool(self):
        pool = _FakePool(accounts=[_account('a'), _account('b'), _account('c')])
        config = {'googleFxSequenceUserId': 'b'}
        ring = _account_rotation_ring(config, pool, 'b')
        self.assertEqual(ring[0], 'b')          # 选中的排最前
        self.assertEqual(set(ring), {'a', 'b', 'c'})

    def test_lock_does_not_pin_a_fallback_account(self):
        """默认环境降级了就不该锁：那样锁住的是替补，不是用户钉的环境。"""
        pool = _FakePool(accounts=[_account('a'), _account('b', disabled=True)])
        config = {'googleFxSequenceUserId': 'b', 'googleFxSequenceUserLock': True}
        ring = _account_rotation_ring(config, pool, 'a')
        self.assertEqual(ring, ['a'])  # 池子里本来就只剩 a 可用
        pool = _FakePool(accounts=[_account('a'), _account('c'),
                                   _account('b', disabled=True)])
        ring = _account_rotation_ring(config, pool, 'a')
        self.assertEqual(set(ring), {'a', 'c'})  # 没被锁死在 a 上

    def test_lock_without_a_default_is_not_a_lock(self):
        config = {'googleFxSequenceUserLock': True, 'googleFxSequenceUserId': ''}
        self.assertFalse(sequence_account_locked(config))
        self.assertEqual(sequence_default_account(config), '')
        pool = _FakePool(accounts=[_account('a'), _account('b')])
        self.assertEqual(set(_account_rotation_ring(config, pool, 'a')), {'a', 'b'})


class TestSequenceConfigValidation(unittest.TestCase):

    def test_spec_exposes_both_fields_as_hot(self):
        for key in ('googleFxSequenceUserId', 'googleFxSequenceUserLock'):
            self.assertIn(key, FX_CONFIG_SPEC)
            self.assertTrue(FX_CONFIG_SPEC[key]['hot'], f'{key} 应能热生效')

    def test_account_field_is_trimmed_and_length_guarded(self):
        self.assertEqual(validate_patch({'googleFxSequenceUserId': '  abc  '}),
                         {'googleFxSequenceUserId': 'abc'})
        self.assertEqual(validate_patch({'googleFxSequenceUserId': ''}),
                         {'googleFxSequenceUserId': ''})
        with self.assertRaises(ValueError):
            validate_patch({'googleFxSequenceUserId': 'x' * 65})

    def test_locking_without_an_environment_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_patch({'googleFxSequenceUserId': '',
                            'googleFxSequenceUserLock': True})
        # 有环境时正常通过
        self.assertEqual(
            validate_patch({'googleFxSequenceUserId': 'a',
                            'googleFxSequenceUserLock': True}),
            {'googleFxSequenceUserId': 'a', 'googleFxSequenceUserLock': True})


class TestDefaultAccountSingleSource(unittest.TestCase):

    def _assert_server_setting_wins(self, managed):
        server_config = {
            'apiKey': 'server-key',
            'googleFxIpRotateRequests': 15,
            'googleFxSequenceUserId': 'server-profile',
            'googleFxSequenceUserLock': True,
        }
        client_config = {
            'googleFxUserId': 'legacy-browser-override',
            'googleFxIpRotateRequests': 3,
            'googleFxSequenceUserId': 'client-profile',
            'googleFxSequenceUserLock': False,
        }
        with patch.object(server_common, 'SERVER_MANAGED', managed), \
                patch.object(server_common, 'SERVER_CONFIG', server_config):
            merged = server_common.effective_config(client_config)

        self.assertNotIn('googleFxUserId', merged)
        self.assertEqual(merged['googleFxIpRotateRequests'], 15)
        self.assertEqual(merged['googleFxSequenceUserId'], 'server-profile')
        self.assertTrue(merged['googleFxSequenceUserLock'])

    def test_managed_requests_cannot_override_service_default(self):
        self._assert_server_setting_wins(True)

    def test_local_requests_cannot_override_service_default(self):
        self._assert_server_setting_wins(False)


if __name__ == '__main__':
    unittest.main()
