# -*- coding: utf-8 -*-
"""
方案一「轻量自动化 · 零运维流水线模式」测试套件
- 动态健康分计算 (calculate_account_health_score)
- 乐观积分预扣 (optimistic_deduct_credit)
- 智能健康分选号排序 (pick_account)
- 异常无感漫游重选 (failover_and_select_next_account)
- 后台静默巡检启动与停止 (start/stop_silent_inspector)
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import server_common
from integrations.google_fx.utils import account_pool
from integrations.google_fx.utils.account_pool import (
    AccountPool, calculate_account_health_score,
)


class TestAccountPoolHealthAndAutoPilot(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher = patch.object(account_pool, '_STATE_FILE', Path(self.tmp_dir) / 'runtime' / 'account_pool.json')
        self.patcher.start()
        self.pool = AccountPool()

    def tearDown(self):
        self.pool.stop_silent_inspector()
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_calculate_account_health_score_rules(self):
        # 1. 完美账号: 100 分 (或有自动登录 +5 分，最高 100)
        perfect_acc = {
            "credit": 200,
            "disabled": False,
            "cooldown_until": None,
            "consecutive_failures": 0,
            "last_probe_status": "ok",
        }
        score = calculate_account_health_score(perfect_acc, {"auto_login_ready": True})
        self.assertEqual(score, 100)

        # 2. 积分较低 (15 <= credit < 50): -15 分 -> 85
        low_credit_acc = dict(perfect_acc, credit=30)
        score = calculate_account_health_score(low_credit_acc)
        self.assertEqual(score, 85)

        # 3. 积分临界 (0 < credit < 15): -40 分 -> 60
        critical_credit_acc = dict(perfect_acc, credit=10)
        score = calculate_account_health_score(critical_credit_acc)
        self.assertEqual(score, 60)

        # 4. 连续失败 2 次: -40 分
        failing_acc = dict(perfect_acc, consecutive_failures=2)
        score = calculate_account_health_score(failing_acc)
        self.assertEqual(score, 60)

        # 5. 处于冷却期: -50 分
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        cooldown_acc = dict(perfect_acc, cooldown_until=future_iso)
        score = calculate_account_health_score(cooldown_acc)
        self.assertEqual(score, 50)

        # 6. 禁用账号: -90 分 -> 10 分
        disabled_acc = dict(perfect_acc, disabled=True)
        score = calculate_account_health_score(disabled_acc)
        self.assertEqual(score, 10)

    def test_optimistic_deduct_credit(self):
        self.pool.add_account("user_opt", name="Opt User", note="test")
        self.pool.record_measured_credit("user_opt", 100)

        # 图片预扣 1
        res = self.pool.optimistic_deduct_credit("user_opt", amount=1)
        self.assertIsNotNone(res)
        self.assertEqual(res["credit"], 99)

        # 视频预扣 10
        res2 = self.pool.optimistic_deduct_credit("user_opt", amount=10)
        self.assertEqual(res2["credit"], 89)

        # 预扣超出余额 -> 0 不变负数
        res3 = self.pool.optimistic_deduct_credit("user_opt", amount=200)
        self.assertEqual(res3["credit"], 0)
        self.assertTrue(res3["disabled"])  # 积分低于 15 自动触发禁用同步

    def test_health_score_in_list_accounts(self):
        self.pool.add_account("user_h1", name="H1")
        self.pool.record_measured_credit("user_h1", 150)
        self.pool.add_account("user_h2", name="H2")
        self.pool.record_measured_credit("user_h2", 20)

        accounts = self.pool.list_accounts(heal=False, sort_by="health_score", sort_order="desc")
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["user_id"], "user_h1")
        self.assertGreater(accounts[0]["health_score"], accounts[1]["health_score"])
        self.assertEqual(accounts[0]["health_status"], "excellent")
        self.assertEqual(accounts[1]["health_status"], "excellent" if accounts[1]["health_score"] >= 85 else "good")

    def test_pick_account_prioritizes_high_health_score(self):
        # 创建两个账号，余额都够，但一个频繁失败，一个完全健康
        self.pool.add_account("bad_acc", name="Bad Acc")
        self.pool.record_measured_credit("bad_acc", 200)
        self.pool.record_generation_failure("bad_acc", "some failure")
        self.pool.record_generation_failure("bad_acc", "some failure 2")

        self.pool.add_account("good_acc", name="Good Acc")
        self.pool.record_measured_credit("good_acc", 180)

        chosen = self.pool.pick_account(min_credit=15)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["user_id"], "good_acc")

    def test_failover_and_select_next_account(self):
        self.pool.add_account("acc_primary", name="Primary")
        self.pool.record_measured_credit("acc_primary", 100)
        self.pool.add_account("acc_standby", name="Standby")
        self.pool.record_measured_credit("acc_standby", 80)

        config = {"googleFxUserId": "acc_primary", "videoAccountPoolMinCredit": 15}

        # 模拟 acc_primary 发生 quota 耗尽，触发无感漫游
        next_id = server_common.failover_and_select_next_account(
            config, self.pool, failed_user_id="acc_primary", reason="QuotaExceeded: 积分已用尽"
        )
        self.assertEqual(next_id, "acc_standby")
        self.assertEqual(config["googleFxUserId"], "acc_standby")

        # 检查 acc_primary 已被冷却且 reason 为 quota_exhausted
        accounts = self.pool.list_accounts(heal=False)
        primary = next(a for a in accounts if a["user_id"] == "acc_primary")
        self.assertIsNotNone(primary.get("cooldown_until"))
        self.assertEqual(primary.get("cooldown_reason"), "quota_exhausted")

        # 模拟 acc_standby 发生单日生成上限 (图片余额超限)
        self.pool.add_account("acc_third", name="Third")
        self.pool.record_measured_credit("acc_third", 50)
        next_id2 = server_common.failover_and_select_next_account(
            config, self.pool, failed_user_id="acc_standby", reason="You've reached the daily limit for Nano Banana 2 generations"
        )
        self.assertEqual(next_id2, "acc_third")
        standby = next(a for a in self.pool.list_accounts(heal=False) if a["user_id"] == "acc_standby")
        self.assertEqual(standby.get("cooldown_reason"), "image_quota_exceeded")
        self.assertEqual(standby.get("last_generation_error"), "图片余额超限")

    def test_silent_inspector_lifecycle(self):
        # 测试巡检线程启动与关闭不会抛异常
        self.pool.start_silent_inspector(interval_seconds=3600)
        self.assertIsNotNone(account_pool.AccountPool._inspector_thread)
        self.assertTrue(account_pool.AccountPool._inspector_thread.is_alive())
        self.pool.stop_silent_inspector()


if __name__ == '__main__':
    unittest.main()
