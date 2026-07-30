# -*- coding: utf-8 -*-
"""掉登录后的自动重新登录（integrations/google_fx/utils/auto_login.py）。

这个模块拿着用户的 Google 密码在真实登录页上点，所以测试的重点不是"能登进去"，
而是**它在不该动手的时候确实不动手**：
  · 最多提交一次密码（撞错密码会把号锁掉）
  · 验证码/手机确认/设备验证一律立刻放弃，不乱点
  · 熔断打开后连页面都不碰
  · 失败必须原样退回既有的"等人工"路径，绝不把主流程带崩
"""

import pytest

from integrations.google_fx.utils import account_credentials as creds
from integrations.google_fx.utils import auto_login, totp

_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_LOGIN_URL = "https://accounts.google.com/v3/signin/identifier"
_FLOW_URL = "https://labs.google/fx/tools/flow"


@pytest.fixture(autouse=True)
def _fast_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "_STATE_FILE", tmp_path / "account_credentials.json")
    # 真实节拍（每轮 1s、每步最多等 12s）会让这一组测试跑几分钟。
    monkeypatch.setattr(auto_login, "_STEP_POLL_SECONDS", 0.001)
    monkeypatch.setattr(auto_login, "_STEP_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(auto_login.time, "sleep", lambda s: None)
    # 别让 generate_fresh 为了等下一个 30s 窗口真的 sleep。
    monkeypatch.setattr(totp, "seconds_remaining", lambda **kw: 30.0)
    # 账号解析：测试一律显式传 user_id，不依赖进程默认账号。
    monkeypatch.setattr(auto_login.account_binding, "resolve_account",
                        lambda explicit=None, fallback=None: str(explicit or "").strip())


# ── 假页面 ──────────────────────────────────────────────────────
# 用一个"当前停在哪一步"的状态机模拟 Google 登录流程。真实流程的形状就是这样：
# 每提交一步页面换一批表单元素，而不是一个长表单。

class _Field:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.value = ""

    # Playwright Locator 接口里 auto_login 实际用到的那部分
    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def click(self, timeout=None):
        pass

    def fill(self, value):
        self.value = value

    def press_sequentially(self, value, delay=None):
        self.value = value

    def press(self, key):
        if key == "Enter":
            self.page.submit(self.kind, self.value)

    def inner_text(self, timeout=None):
        return self.page.error_text


class _Missing:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self, timeout=None):
        return False


class _NextButton:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def click(self, timeout=None):
        self.page.submit_current()


class _FakeLoginPage:
    """step 取 email / password / totp / chooser / captcha / done。"""

    # 哪一步渲染哪个输入框。选择器用 ui_selectors 里那一组的首选项。
    _FIELDS = {
        "email": ("input#identifierId", "email"),
        "password": ("input[type='password'][name='Passwd']", "password"),
        "totp": ("input#totpPin", "totp"),
    }

    def __init__(self, step="email", script=None, expect_password="pw",
                 expect_totp=None, error_text=""):
        self.step = step
        # script: {(step, 'ok'|'bad'): 下一步}。没写的转移默认停在原地。
        self.script = script or {}
        self.expect_password = expect_password
        self.expect_totp = expect_totp
        self.error_text = error_text
        self.submissions = []
        self._pending = None

    # -- Playwright Page 接口 --
    @property
    def url(self):
        if self.step == "done":
            return _FLOW_URL
        if self.step == "chooser":
            return "https://accounts.google.com/v3/signin/accountchooser"
        return _LOGIN_URL

    def is_closed(self):
        return False

    def evaluate(self, script):
        # is_google_login_page 的 URL 判定在 done 之外都会先命中，这里只需
        # 让 done 状态如实回 False。
        return self.step != "done"

    def inner_text(self, selector):
        if selector != "body":
            return ""
        return {
            "captcha": "Verify you're not a robot — captcha",
            "chooser": "Choose an account Use another account",
        }.get(self.step, f"Sign in step {self.step}")

    def locator(self, selector):
        selector_field = self._FIELDS.get(self.step)
        if selector_field and selector == selector_field[0]:
            field = _Field(self, selector_field[1])
            self._pending = field
            return field
        if selector.startswith("#") and "Next" in selector:
            return _NextButton(self)
        if selector == "#identifierNext button":
            return _NextButton(self)
        return _Missing()

    # -- 状态机 --
    def submit_current(self):
        if self._pending is not None:
            self.submit(self._pending.kind, self._pending.value)

    def submit(self, kind, value):
        self.submissions.append((kind, value))
        good = {
            "email": True,  # 邮箱这一步测试里一律当作正确
            "password": value == self.expect_password,
            "totp": self.expect_totp is None or value == self.expect_totp,
        }.get(kind, False)
        self.step = self.script.get((kind, "ok" if good else "bad"), self.step)

    def password_submits(self):
        return sum(1 for kind, _ in self.submissions if kind == "password")


def _configure(user_id="u1", password="pw", with_totp=False):
    creds.save(user_id, email="me@example.com", password=password,
               totp_secret=_SECRET if with_totp else None)


# ── 不该动手的情况 ──────────────────────────────────────────────

def test_not_on_login_page_is_an_idempotent_noop():
    """调用点可以无脑调它，不必先自己判断在不在登录页。"""
    _configure()
    page = _FakeLoginPage(step="done")

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert result.reason == "not_needed"
    assert page.submissions == []


def test_without_credentials_nothing_is_typed():
    page = _FakeLoginPage(step="email")

    result = auto_login.try_auto_login(page, user_id="unconfigured")

    assert result.ok is False
    assert result.reason == "no_credentials"
    assert page.submissions == []


def test_open_breaker_does_not_touch_the_page_at_all():
    """熔断的全部意义就是"别再撞了"。哪怕只是填一次邮箱也是多余的风险。"""
    _configure()
    creds.record_attempt("u1", "failed", "wrong_password", "x")
    creds.record_attempt("u1", "failed", "wrong_password", "x")
    page = _FakeLoginPage(step="email")

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.reason == "breaker_open"
    assert page.submissions == []


@pytest.mark.parametrize("body_text,expected_reason", [
    ("Verify you're not a robot — captcha", "captcha_required"),
    ("Check your phone — tap Yes to sign in", "device_confirm_required"),
    ("We detected unusual activity on this account", "security_check"),
    ("This account disabled by administrator", "account_rejected"),
])
def test_hard_stops_give_up_without_clicking_anything(body_text, expected_reason, monkeypatch):
    """验证码/手机确认/风控页自动化处理不了。在这些页面上乱点可能把账号推进
    更难恢复的状态，所以必须立刻退回人工。"""
    _configure()
    page = _FakeLoginPage(step="email")
    monkeypatch.setattr(page, "inner_text", lambda selector: body_text)

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == expected_reason
    assert page.submissions == []


def test_captcha_does_not_count_toward_the_breaker():
    """出验证码跟密码对不对无关。算进熔断会让功能在账号完全正常时自己关掉。"""
    _configure()
    for _ in range(3):
        page = _FakeLoginPage(step="email")
        page.inner_text = lambda selector: "please solve the captcha"
        auto_login.try_auto_login(page, user_id="u1")

    assert creds.is_blocked("u1") is False


# ── 正常登录 ────────────────────────────────────────────────────

def test_email_then_password_logs_in():
    _configure()
    page = _FakeLoginPage(step="email", script={
        ("email", "ok"): "password",
        ("password", "ok"): "done",
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert result.reason == "logged_in"
    assert page.submissions == [("email", "me@example.com"), ("password", "pw")]


def test_two_factor_flow_fills_the_generated_code():
    _configure(with_totp=True)
    expected_code = totp.generate(_SECRET)
    page = _FakeLoginPage(step="email", expect_totp=expected_code, script={
        ("email", "ok"): "password",
        ("password", "ok"): "totp",
        ("totp", "ok"): "done",
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.submissions[-1] == ("totp", expected_code)


def test_success_records_ok_and_clears_previous_error():
    _configure()
    creds.record_attempt("u1", "failed", "wrong_password", "上次密码错了")
    page = _FakeLoginPage(step="email", script={
        ("email", "ok"): "password", ("password", "ok"): "done"})

    auto_login.try_auto_login(page, user_id="u1")

    entry = creds.public_entry("u1", creds.get("u1"))
    assert entry["auto_login_status"] == "ok"
    assert entry["auto_login_fail_streak"] == 0
    assert entry["auto_login_error"] is None


# ── 失败分类 ────────────────────────────────────────────────────

def test_wrong_password_is_submitted_exactly_once():
    """本模块最重要的一条护栏。没有它，一个填错的密码会在每次掉登录时被反复
    提交，Google 先上验证码、再锁号。"""
    _configure(password="wrong")
    page = _FakeLoginPage(step="email", expect_password="right", script={
        ("email", "ok"): "password",
        # ("password", "bad") 没写 = 停在密码页，正是密码错时的真实表现
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == "wrong_password"
    assert page.password_submits() == 1


def test_wrong_password_increments_the_breaker():
    _configure(password="wrong")

    def _attempt():
        page = _FakeLoginPage(step="email", expect_password="right",
                              script={("email", "ok"): "password"})
        return auto_login.try_auto_login(page, user_id="u1")

    _attempt()
    assert creds.is_blocked("u1") is False
    _attempt()
    assert creds.is_blocked("u1") is True


def test_rejected_totp_is_classified_as_wrong_totp():
    _configure(with_totp=True)
    page = _FakeLoginPage(step="email", expect_totp="000000", script={
        ("email", "ok"): "password", ("password", "ok"): "totp"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.reason == "wrong_totp"


def test_two_factor_page_without_a_configured_secret_bails_out():
    """没配 TOTP 密钥却撞上两步验证：如实说清楚，别报成"密码错"——那会让用户
    去改一个本来没问题的密码。"""
    _configure(with_totp=False)
    page = _FakeLoginPage(step="email", script={
        ("email", "ok"): "password", ("password", "ok"): "totp"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.reason == "totp_not_configured"
    assert page.password_submits() == 1


def test_timeout_when_the_page_never_moves():
    _configure()
    page = _FakeLoginPage(step="unknown-interstitial")

    result = auto_login.try_auto_login(page, user_id="u1", timeout_seconds=0.05)

    assert result.ok is False
    assert result.reason in ("unknown_page", "timeout")
    assert page.submissions == []


def test_cancellation_stops_immediately():
    _configure()
    page = _FakeLoginPage(step="email", script={("email", "ok"): "password"})

    result = auto_login.try_auto_login(page, user_id="u1", cancel_check=lambda: True)

    assert result.reason == "cancelled"
    assert page.submissions == []


# ── 与调用方的契约 ──────────────────────────────────────────────

def test_auto_login_available_reflects_config_and_breaker():
    assert auto_login.auto_login_available("u1") is False
    _configure()
    assert auto_login.auto_login_available("u1") is True

    creds.record_attempt("u1", "failed", "wrong_password", "x")
    creds.record_attempt("u1", "failed", "wrong_password", "x")
    assert auto_login.auto_login_available("u1") is False


def test_attempt_auto_login_swallows_exceptions_so_callers_never_break():
    """自动登录是**附加**能力。它自己炸掉绝不能把生成任务/积分探针带崩——
    调用方原本的行为（退回等人工）必须原样保留。"""
    from integrations.google_fx.utils.browser import attempt_auto_login

    _configure()

    class _ExplodingPage:
        @property
        def url(self):
            raise RuntimeError("CDP 连接炸了")

        def is_closed(self):
            raise RuntimeError("CDP 连接炸了")

        def evaluate(self, script):
            raise RuntimeError("CDP 连接炸了")

        def inner_text(self, selector):
            raise RuntimeError("CDP 连接炸了")

        def locator(self, selector):
            raise RuntimeError("CDP 连接炸了")

    assert attempt_auto_login(_ExplodingPage(), user_id="u1") is False


def test_attempt_auto_login_skips_quietly_when_nothing_is_configured():
    """没配凭据的账号不该打一行"自动登录失败"——那会让人以为功能坏了。"""
    from integrations.google_fx.utils.browser import attempt_auto_login

    page = _FakeLoginPage(step="email")
    assert attempt_auto_login(page, user_id="unconfigured") is False
    assert page.submissions == []
