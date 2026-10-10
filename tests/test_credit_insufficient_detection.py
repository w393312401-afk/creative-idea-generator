# -*- coding: utf-8 -*-
"""
🧪 「积分不够」而不只是「积分为 0」的识别
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
回归的是同一个坏账：运行时那条链路原本硬写死跟 0 比，于是页面上明明写着
"还剩 10 分"、低于用户配的「选号最低积分」、根本跑不动一段视频，也一律判成
"没耗尽"——上层把生成失败当普通失败，在同一个号上原地重试到耗光重试次数。

四处一起测：
  1. detect_page_credit_exhaustion 跟阈值比，不跟 0 比
  2. 实测读数按真实值写回号池，不再一律记成 0
  3. switch_to_next_account 默认吃用户配的阈值（原来硬默认 1）
  4. 轮转环的每条腿开跑前复核一次（环是按缓存值一次排定的，中间扣分没人记账）
"""

import pytest

from integrations.google_fx.services import google_fx_credit as credit
from integrations.google_fx.utils import account_pool as ap
import server_common


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_STATE_FILE", tmp_path / "account_pool.json")
    monkeypatch.setattr(ap, "_get_min_credit_threshold", lambda: 15)
    yield


class _Locator:
    def __init__(self, text=None):
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.text else 0

    def is_visible(self, timeout=None):
        return bool(self.text)

    def inner_text(self, timeout=None):
        return self.text


class _Page:
    """只实现 detect_page_credit_exhaustion 用到的那两个入口。"""

    def __init__(self, scanned=(), mapping=None):
        self.scanned = list(scanned)
        self.mapping = mapping or {}

    def evaluate(self, _script):
        return self.scanned

    def locator(self, selector):
        return self.mapping.get(selector, _Locator())


# ── 1. 跟阈值比，不跟 0 比 ──────────────────────────────

def test_balance_below_threshold_is_detected(monkeypatch):
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    page = _Page(mapping={"a[href*='credits']": _Locator("10 Google Flow credits")})
    reason = credit.detect_page_credit_exhaustion(page)
    assert reason is not None, "剩 10 分低于阈值 15，必须判为不可用"
    assert "10" in reason and "15" in reason
    # 措辞要跟"余额为 0"分得开，排障时一眼能看出是哪种
    assert "不足" in reason


def test_balance_at_threshold_is_not_flagged(monkeypatch):
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    page = _Page(mapping={"a[href*='credits']": _Locator("15 Google Flow credits")})
    assert credit.detect_page_credit_exhaustion(page) is None


def test_zero_balance_still_reads_as_zero(monkeypatch):
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    page = _Page(mapping={"a[href*='credits']": _Locator("0 Google Flow credits")})
    reason = credit.detect_page_credit_exhaustion(page)
    assert reason is not None and "余额为 0" in reason


def test_promo_copy_is_not_mistaken_for_balance(monkeypatch):
    """套餐宣传数字挂在弹窗/Toast 类容器里，只做关键词匹配，绝不当余额读。

    这是本模块开头那条硬性要求的延续：低于阈值就判死，误判一次的代价是把
    好账号禁用 24 小时，所以"能当余额读的选择器"必须是白名单。
    """
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    page = _Page(scanned=[{"text": "5 credits", "balance": False}])
    assert credit.detect_page_credit_exhaustion(page) is None

    # 同一句话挂在余额本体选择器上就要算数
    page2 = _Page(scanned=[{"text": "5 credits", "balance": True}])
    assert credit.detect_page_credit_exhaustion(page2) is not None


def test_scan_result_tolerates_plain_strings(monkeypatch):
    """老调用方/测试桩可能还回纯字符串数组，不能因此炸掉整个检测。"""
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    assert credit.detect_page_credit_exhaustion(_Page(scanned=["out of credits"])) is not None
    assert credit.detect_page_credit_exhaustion(_Page(scanned=["Generating..."])) is None


def test_min_usable_credit_follows_config(monkeypatch):
    monkeypatch.setattr(ap, "_get_min_credit_threshold", lambda: 40)
    assert credit.min_usable_credit() == 40


# ── 2. 实测读数按真实值写回，不再一律记成 0 ─────────────

def test_measured_credit_round_trip(monkeypatch):
    monkeypatch.setattr(credit, "min_usable_credit", lambda: 15)
    page = _Page(mapping={"a[href*='credits']": _Locator("12 Google Flow credits")})
    reason = credit.detect_page_credit_exhaustion(page)
    assert credit.measured_credit_from_reason(reason) == 12
    assert credit.measured_credit_from_reason("普通生成失败") is None


def test_mark_exhausted_records_real_balance():
    pool = ap.AccountPool()
    pool.add_account("user_low")
    pool.mark_exhausted("user_low", credit=12)

    state = ap._read_state()
    # 页面上写着 12 就记 12——记成 0 会在控制台显示一个根本不存在的余额
    assert state["user_low"]["credit"] == 12
    assert state["user_low"]["disabled"] is True
    assert state["user_low"]["cooldown_reason"] == "quota_exhausted"


def test_mark_exhausted_without_measurement_falls_back_to_zero():
    pool = ap.AccountPool()
    pool.add_account("user_unknown")
    pool.mark_exhausted("user_unknown")
    assert ap._read_state()["user_unknown"]["credit"] == 0


# ── 3. 换号默认吃配置阈值 ───────────────────────────────

def test_switch_to_next_account_defaults_to_configured_threshold(monkeypatch):
    seen = {}

    def _fake_pick(self, min_credit=1, exclude=None):
        seen["min_credit"] = min_credit
        return None

    monkeypatch.setattr(ap.AccountPool, "pick_account", _fake_pick)
    monkeypatch.setattr(ap.account_binding, "resolve_account", lambda fallback=None: "cur")
    ap.switch_to_next_account()
    # 原来这里硬默认 1，配置里的阈值根本到不了运行时换号
    assert seen["min_credit"] == 15


# ── 4. 切腿前复核 ───────────────────────────────────────

def _probed(pool, user_id, credit_value):
    state = ap._read_state()
    state[user_id]["credit"] = credit_value
    state[user_id]["last_checked_at"] = ap._now_iso()
    state[user_id]["last_probe_status"] = "ok"
    ap._write_state(state)


def test_account_is_usable_matches_pick_account_semantics():
    pool = ap.AccountPool()
    pool.add_account("rich")
    pool.add_account("poor")
    _probed(pool, "rich", 90)
    _probed(pool, "poor", 3)  # 低于阈值 → _read_state 读的时候就自动禁用

    assert pool.account_is_usable("rich") is True
    assert pool.account_is_usable("poor") is False
    assert pool.account_is_usable("nobody") is False


def test_account_is_usable_reprobes_after_tasks_ran(monkeypatch):
    """上次探测之后又跑过任务 = 那之后扣了多少分没人记账，必须重探。"""
    pool = ap.AccountPool()
    pool.add_account("worker")
    _probed(pool, "worker", 90)
    state = ap._read_state()
    state["worker"]["last_success_at"] = ap._now_iso()
    ap._write_state(state)

    from integrations.google_fx.services import google_fx_credit as credit_module
    probed = []
    monkeypatch.setattr(credit_module, "probe_flow_credit",
                        lambda user_id, port=None: (probed.append(user_id) or 4))

    assert pool.account_is_usable("worker") is False
    assert probed == ["worker"], "缓存写着 90，不重探就会把一个只剩 4 分的号排上去"


def test_revalidate_leg_account_swaps_to_next_usable():
    pool = ap.AccountPool()
    for uid in ("burnt", "fresh"):
        pool.add_account(uid)
    _probed(pool, "burnt", 2)
    _probed(pool, "fresh", 80)

    config = {"videoAccountPoolMinCredit": 15}
    got = server_common.revalidate_leg_account(config, pool, "burnt", ["burnt", "fresh"], [])
    assert got == "fresh"


def test_revalidate_leg_account_keeps_usable_account():
    pool = ap.AccountPool()
    pool.add_account("fine")
    _probed(pool, "fine", 80)
    config = {"videoAccountPoolMinCredit": 15}
    assert server_common.revalidate_leg_account(config, pool, "fine", ["fine"], []) == "fine"


def test_revalidate_leg_account_returns_none_when_pool_dry():
    pool = ap.AccountPool()
    pool.add_account("burnt")
    _probed(pool, "burnt", 2)
    config = {"videoAccountPoolMinCredit": 15}
    assert server_common.revalidate_leg_account(config, pool, "burnt", ["burnt"], []) is None


def test_revalidate_leg_account_fails_open_on_old_pool_object():
    """号池对象没有 account_is_usable（老调用方/测试桩）时不能拖垮生成。"""
    class _Old:
        pass

    got = server_common.revalidate_leg_account({}, _Old(), "a", ["a", "b"], [])
    assert got == "a"
