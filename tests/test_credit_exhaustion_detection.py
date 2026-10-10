# -*- coding: utf-8 -*-
"""
🧪 积分耗尽的识别链路（2026-08-24 复盘回归）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯逻辑测试，页面用桩对象，不需要真实浏览器/AdsPower。

现场：账号 k1f11muk 在 14:21 探到 59 分，14:33 跑到第 5 段时积分见底。
表现出来的却是三条互不相干、且都不指向真因的错误：
  1. 14:33:54  "Generate 后未检测到新 tile"
     —— 这里明明调了 detect_page_credit_exhaustion()，但它在项目页上读不到余额
        （credit_display 那两个选择器只存在于头像菜单弹层里，菜单当时是关着的），
        于是返回 None。
  2. 14:35:34  "检测到未知弹窗但找不到明确的关闭按钮，未处理: Aug 24, 02:31 AM"
     —— 积分刷新提示弹窗盖住了配置面板，而清理逻辑认不出它的关闭按钮就撒手不管。
  3. 14:35:38  "切换Video配置未完成…面板未确认项: video_submode"
     —— 被弹窗挡住点不到 tab，报了个纯现象描述。这条文案不含任何积分关键词，
        video_generator 的 is_credit_exhausted_message() 认不出来 →
        不 mark_exhausted() → 号池继续把这个空号派出去，下一批同样挂法。

本文件锁住修复后的行为，避免以上三条任何一条回归。
"""

import pytest

from integrations.google_fx.services import google_fx_credit as C
from integrations.google_fx.services import google_fx_helpers as H


# ── 弱信号：只升级探测，绝不单独当耗尽判据 ──────────────────────────────────

@pytest.mark.parametrize("text", [
    "Your credits refresh Aug 24, 02:31 AM",
    "Credits will reset on the 1st",
    "你的积分将于 8 月 24 日刷新",
    "额度重置时间",
])
def test_refresh_wording_is_only_a_hint(text):
    """积分刷新措辞是弱信号：命中 hint，但绝不能被当成"耗尽"。

    误判的代价是把好账号自动停用 24 小时，而同样的措辞也会出现在套餐宣传里
    （"你的 1,000 月度积分将于每月 1 日刷新"）。
    """
    assert C.is_credit_hint_message(text) is True
    assert C.is_credit_exhausted_message(text) is False


def test_plan_promo_text_is_not_exhaustion():
    """套餐宣传数字不能被读成余额，也不能被读成耗尽。"""
    promo = "1,000 monthly Google Flow credits"
    assert C.is_credit_exhausted_message(promo) is False


@pytest.mark.parametrize("text", [
    "INSUFFICIENT_CREDITS: 账号积分余额为 0",
    "You're out of credits",
    "账号积分不足：实测 3 < 选号最低积分 15",
])
def test_hard_exhaustion_wording_still_detected(text):
    assert C.is_credit_exhausted_message(text) is True


# ── 深探：项目页上快路径读不到余额时，去头像菜单实读 ────────────────────────

class _StubPage:
    """最小页面桩：快路径扫不到任何积分线索。"""

    def __init__(self, dialog_texts=None):
        self._dialog_texts = dialog_texts or []
        self.url = "https://labs.google/fx/tools/flow/project/abc"

    def evaluate(self, script, *args):
        return list(self._dialog_texts)

    def locator(self, selector):
        return _StubLocator()


class _StubLocator:
    def __init__(self):
        self.first = self
        self.last = self

    def count(self):
        return 0

    def is_visible(self, timeout=None):
        return False


def test_fast_path_is_blind_on_project_page(monkeypatch):
    """复现坑本身：项目页上快路径什么都读不到，deep=False 只能返回 None。"""
    page = _StubPage()
    assert C.detect_page_credit_exhaustion(page) is None


def test_deep_probe_reads_real_balance_and_reports(monkeypatch):
    """deep=True：快路径没结论就点开头像菜单实读，低于阈值即判不足。"""
    page = _StubPage()
    monkeypatch.setattr(C, "_read_credit_from_account_menu", lambda p, **kw: 3)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 15)

    reason = C.detect_page_credit_exhaustion(page, deep=True)
    assert reason is not None
    assert "3" in reason
    # 实测读数要能被上层取回，避免号池一律记成 0
    assert C.measured_credit_from_reason(reason) == 3
    # 这条文案必须能被上层的积分判定认出来，否则不会 mark_exhausted()
    assert C.is_credit_exhausted_message(reason) is True


def test_deep_probe_clears_healthy_account(monkeypatch):
    """余额够用时深探必须返回 None——这次失败与积分无关，不能连累账号。"""
    page = _StubPage()
    monkeypatch.setattr(C, "_read_credit_from_account_menu", lambda p, **kw: 500)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 15)
    assert C.detect_page_credit_exhaustion(page, deep=True) is None


def test_deep_probe_unreadable_stays_silent(monkeypatch):
    """读不到数字就闭嘴：宁可报一个含糊的失败，也不能凭猜停用好账号。"""
    page = _StubPage()
    monkeypatch.setattr(C, "_read_credit_from_account_menu", lambda p, **kw: None)
    assert C.detect_page_credit_exhaustion(page, deep=True) is None


def test_refresh_hint_escalates_to_deep_probe(monkeypatch):
    """页面上出现刷新提示（弱信号）时，即使调用方没开 deep 也要升级去实读。"""
    page = _StubPage(dialog_texts=[
        {"text": "Aug 24, 02:31 AM\nYour credits refresh then", "balance": False},
    ])
    monkeypatch.setattr(C, "_read_credit_from_account_menu", lambda p, **kw: 0)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 15)

    reason = C.detect_page_credit_exhaustion(page)
    assert reason is not None
    assert C.measured_credit_from_reason(reason) == 0


# ── 换号判定：新文案必须能触发换号，且不被 UI 自动化词拦下 ──────────────────

def test_config_gate_message_triggers_account_switch():
    """配置闸门抛出的文案要判成"账号自身问题"，而不是 UI 自动化毛病。

    "配置未完成" 在 _AUTOMATION_ERROR_TOKENS 里（判定不换号）；积分判定必须
    排在它前面，否则积分耗尽会被当成脚本 bug，在同一个空号上原地重试到死。
    """
    msg = ("INSUFFICIENT_CREDITS: 账号积分余额为 0 (当前读数: 0 Google Flow credits) "
           "[credit=0]（切换Video配置面板被积分提示挡住，未确认项: video_submode）")
    should_switch, verdict = H._classify_failure_for_switch(msg)
    assert should_switch is True, verdict


def test_plain_config_failure_still_not_a_switch():
    """真·配置失败（积分充足）仍旧不该换号——不能因为这次修复矫枉过正。"""
    msg = "切换Video配置未完成，停止生成。当前状态: Video · 720p · 8s crop_9_16 x1；面板未确认项: video_submode"
    should_switch, verdict = H._classify_failure_for_switch(msg)
    assert should_switch is False, verdict


# ── 视频链：helpers 抛的 INSUFFICIENT_CREDITS 必须被翻译成换号信号 ──────────

def test_video_chain_recognizes_credit_signal():
    from integrations.google_fx.services import google_fx_video as V

    assert V._looks_like_credit_exhaustion(
        RuntimeError("INSUFFICIENT_CREDITS: 账号积分余额为 0")) is True
    assert V._looks_like_credit_exhaustion(
        RuntimeError("You are out of credits")) is True
    assert V._looks_like_credit_exhaustion(
        RuntimeError("CANVAS_MOUNT_FAILED:画布卡片挂载失败 (1/2)")) is False


# ── 批次中途复核：不能把已经花掉积分、正在生成的片子扔了 ────────────────────

class _StubRunner:
    """只装出 _credit_checkpoint 需要的那几个属性。"""

    def __init__(self, credit):
        from integrations.google_fx.services import google_fx_video as V
        self._submits_since_credit_check = V._CREDIT_RECHECK_EVERY_SUBMITS - 1
        self._credit = credit


def _make_checkpoint(monkeypatch, credit):
    from integrations.google_fx.services import google_fx_video as V
    from integrations.google_fx.services import google_fx_credit as C
    from integrations.google_fx.utils import account_pool as P

    monkeypatch.setattr(C, "read_page_credit_via_menu", lambda page, **kw: credit)
    monkeypatch.setattr(C, "min_usable_credit", lambda: 15)
    monkeypatch.setattr(P.AccountPool, "record_measured_credit",
                        lambda self, uid, c: None)
    runner = _StubRunner(credit)
    return V._ChunkRunner._credit_checkpoint.__get__(runner, _StubRunner)


def test_checkpoint_reports_when_dry(monkeypatch):
    """余额低于阈值 → 返回原因字符串（而不是直接抛，抛会丢掉在飞任务）。"""
    checkpoint = _make_checkpoint(monkeypatch, credit=2)
    reason = checkpoint(object(), remaining_after=4)
    assert reason is not None
    assert "积分不足" in reason


def test_checkpoint_silent_when_healthy(monkeypatch):
    """余额充足 → None，批次照常继续。"""
    checkpoint = _make_checkpoint(monkeypatch, credit=900)
    assert checkpoint(object(), remaining_after=4) is None


def test_checkpoint_only_warns_when_budget_short(monkeypatch):
    """够跑下一段、但跑不完剩下这些 → 只告警，不喊停。

    喊停的代价是可能压根没有下一个可用号（run() 会 break，剩余任务全判失败），
    而"能跑一段是一段"不会比现在更差。
    """
    checkpoint = _make_checkpoint(monkeypatch, credit=40)   # ≥15，但 < 4*15
    assert checkpoint(object(), remaining_after=4) is None


def test_checkpoint_throttles(monkeypatch):
    """没到复核间隔就直接返回，不去点头像菜单（那是秒级开销）。"""
    from integrations.google_fx.services import google_fx_video as V
    from integrations.google_fx.services import google_fx_credit as C

    calls = []
    monkeypatch.setattr(C, "read_page_credit_via_menu",
                        lambda page, **kw: calls.append(1) or 0)
    runner = _StubRunner(0)
    runner._submits_since_credit_check = 0
    checkpoint = V._ChunkRunner._credit_checkpoint.__get__(runner, _StubRunner)
    assert checkpoint(object(), remaining_after=4) is None
    assert calls == []
