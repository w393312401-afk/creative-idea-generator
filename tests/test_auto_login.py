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
    # 账号选择页的渲染等待同理；0.2s 足够跑完几轮扫描，又不会让用例真的卡 10 秒。
    monkeypatch.setattr(auto_login, "_CHOOSER_RENDER_SECONDS", 0.2)
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


class _Action:
    def __init__(self, page, name, next_step):
        self.page = page
        self.name = name
        self.next_step = next_step

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def click(self, timeout=None):
        self.page.clicks.append(self.name)
        self.page.step = self.next_step


class _Mouse:
    """page.mouse。按坐标点是文本定位那条兜底路径的最后一步。"""

    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.mouse_clicks.append((x, y))
        self.page.on_mouse_click(x, y)


class _FakeLoginPage:
    """step 取 provider / email / password / totp / chooser / captcha / done。"""

    # 哪一步渲染哪个输入框。选择器用 ui_selectors 里那一组的首选项。
    _FIELDS = {
        "email": ("input#identifierId", "email"),
        "password": ("input[type='password'][name='Passwd']", "password"),
        "totp": ("input#totpPin", "totp"),
    }

    def __init__(self, step="email", script=None, expect_password="pw",
                 expect_totp=None, error_text=""):
        self.step = step
        self.mouse = _Mouse(self)
        self.mouse_clicks = []
        # script: {(step, 'ok'|'bad'): 下一步}。没写的转移默认停在原地。
        self.script = script or {}
        self.expect_password = expect_password
        self.expect_totp = expect_totp
        self.error_text = error_text
        self.submissions = []
        self.clicks = []
        self._pending = None

    # -- Playwright Page 接口 --
    @property
    def url(self):
        if self.step in ("done", "provider"):
            return _FLOW_URL
        if self.step == "chooser":
            return "https://accounts.google.com/v3/signin/accountchooser"
        return _LOGIN_URL

    def is_closed(self):
        return False

    def evaluate(self, script, arg=None):
        # 三个不同的注入脚本：按文本找可点元素、列出可见控件、判定是不是登录页。
        if script.lstrip().startswith("(needles)"):
            return self.text_click_target(arg or [])
        if "input[type=\"submit\"]" in script:
            return list(self.visible_options())
        # is_google_login_page 的 URL 判定在 done 之外都会先命中，这里只需
        # 让 done 状态如实回 False。
        return self.step != "done"

    def text_click_target(self, needles):
        """默认：页面上没有任何可按文本点中的东西。"""
        return None

    def visible_options(self):
        return []

    def on_mouse_click(self, x, y):
        pass

    def inner_text(self, selector):
        if selector != "body":
            return ""
        return {
            "provider": "Try signing in with a different account. Sign in with Google",
            "captcha": "Verify you're not a robot — captcha",
            "chooser": "Choose an account Use another account",
            "phone_prompt": "2-Step Verification Check your phone and tap Yes",
            "challenge_picker": "2-Step Verification Choose how you want to sign in",
        }.get(self.step, f"Sign in step {self.step}")

    def locator(self, selector):
        if (self.step == "provider" and
                selector == "form[action*='/fx/api/auth/signin/google'] button[type='submit']"):
            return _Action(self, "sign_in_with_google", "email")
        if self.step == "phone_prompt" and selector == "button:has-text('Try another way')":
            return _Action(self, "try_another_way", "challenge_picker")
        if self.step == "challenge_picker" and selector == "[data-challengetype='6']":
            return _Action(self, "authenticator", "totp")
        # 图二底部仍有 Try another way；用于验证实现不会误点第二次。
        if self.step == "challenge_picker" and selector == "button:has-text('Try another way')":
            return _Action(self, "try_another_way_again", "unknown-interstitial")
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


class _AccountRow:
    """账号选择页里的一行。真实页面上账号原文在 data-identifier 属性里。"""

    def __init__(self, page, identifier):
        self.page = page
        self.identifier = identifier

    def get_attribute(self, name):
        return self.identifier if name == "data-identifier" else None

    def is_visible(self, timeout=None):
        return True

    def click(self, timeout=None):
        self.page.clicks.append(f"account:{self.identifier}")
        self.page.step = "password"


class _AccountRows:
    def __init__(self, page, identifiers):
        self.page = page
        self.identifiers = list(identifiers)

    def count(self):
        return len(self.identifiers)

    def nth(self, index):
        return _AccountRow(self.page, self.identifiers[index])

    @property
    def first(self):
        return self.nth(0) if self.identifiers else _Missing()


class _ChooserPage(_FakeLoginPage):
    """账号选择页。列表是 JS 渲染的：前 render_after_scans 轮扫描时 DOM 里空无一物。

    这正是线上事故的形状——不模拟"晚一点才渲染"，任何单次扫描的实现都能过测试。
    """

    def __init__(self, identifiers=(), use_another=False, render_after_scans=0, **kwargs):
        super().__init__(step="chooser", **kwargs)
        self._identifiers = list(identifiers)
        self._use_another = use_another
        self._render_after_scans = render_after_scans
        self.scans = 0

    def _rendered(self):
        return self.scans >= self._render_after_scans

    def locator(self, selector):
        if selector == "[data-identifier]":
            # 每轮的第一个选择器，拿它当"扫描了一遍"的计数点。
            self.scans += 1
            return _AccountRows(self, self._identifiers if self._rendered() else [])
        if (self._rendered() and self._use_another
                and selector == "li:has-text('Use another account')"):
            return _Action(self, "use_another_account", "email")
        if self.step != "chooser":
            return super().locator(selector)
        return _Missing()


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


def test_flow_auth_provider_page_continues_into_google_login():
    """labs.google 的 Auth.js 中转页应先点 Google provider，再复用既有登录流程。"""
    _configure()
    page = _FakeLoginPage(step="provider", script={
        ("email", "ok"): "password",
        ("password", "ok"): "done",
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert result.reason == "logged_in"
    assert page.clicks == ["sign_in_with_google"]
    assert page.submissions == [("email", "me@example.com"), ("password", "pw")]


def test_flow_auth_provider_button_is_not_submitted_repeatedly():
    """中转页不跳转时只提交一次，避免在网络异常下反复 POST 登录表单。"""
    _configure()
    page = _FakeLoginPage(step="provider")
    original_locator = page.locator

    def stuck_locator(selector):
        if selector == "form[action*='/fx/api/auth/signin/google'] button[type='submit']":
            return _Action(page, "sign_in_with_google", "provider")
        return original_locator(selector)

    page.locator = stuck_locator
    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == "provider_stuck"
    assert page.clicks == ["sign_in_with_google"]


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


def test_phone_prompt_switches_to_authenticator_then_fills_totp():
    """手机通知确认页应按图一→图二→TOTP 输入框的顺序自动切换。"""
    _configure(with_totp=True)
    expected_code = totp.generate(_SECRET)
    page = _FakeLoginPage(step="phone_prompt", expect_totp=expected_code, script={
        ("totp", "ok"): "done",
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["try_another_way", "authenticator"]
    assert page.submissions == [("totp", expected_code)]


def test_challenge_picker_selects_authenticator_before_bottom_try_another_way():
    """已经在图二时直接选 Authenticator，不能再点底部同名入口。"""
    _configure(with_totp=True)
    expected_code = totp.generate(_SECRET)
    page = _FakeLoginPage(step="challenge_picker", expect_totp=expected_code, script={
        ("totp", "ok"): "done",
    })

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["authenticator"]
    assert page.submissions == [("totp", expected_code)]


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


# ── 登录方式选择页 ──────────────────────────────────────────────
# 2026-08-05 现场截图：账号已确认后 Google 先出
# "Welcome / Choose how you want to sign in: Enter your password / Use your
# passkey / Try another way"。这一页也有「Try another way」，但它是登录方式
# 入口而不是两步验证的换方式入口——按 challenge_picker 处理会去找永远不存在的
# 身份验证器选项，然后在下级页里空转到 180s 预算耗尽。

class _MethodPickerPage(_FakeLoginPage):
    """密码之前的登录方式选择页。同页并存「用密码登录」和「Try another way」。"""

    def __init__(self, **kwargs):
        super().__init__(step="method_picker", **kwargs)

    def inner_text(self, selector):
        if selector != "body":
            return ""
        if self.step != "method_picker":
            return super().inner_text(selector)
        return ("Welcome me@example.com Choose how you want to sign in: "
                "Enter your password Use your passkey Try another way")

    def locator(self, selector):
        if self.step != "method_picker":
            return super().locator(selector)
        # BOQ 单页应用会把密码框预渲染在选择页的 DOM 里。判定一旦以"有没有
        # 密码框"为前提，真机上就会直接失效——2026-08-05 现场正是如此。
        if selector == "input[type='password'][name='Passwd']":
            return _Field(self, "password")
        if selector == "[data-challengetype='1']":
            return _Action(self, "enter_password", "password")
        # 同页并存的 2FA 换方式入口。实现如果误点它，用例就会看见这次点击。
        if selector == "button:has-text('Try another way')":
            return _Action(self, "try_another_way", "unknown-interstitial")
        return _Missing()


class _BoqMethodPickerPage(_MethodPickerPage):
    """线上真实形状：整页 BOQ 渲染，混淆类名 + jsaction，**没有任何**稳定的
    选择器可用（用户提供的页面 DOM 证实了这点）。这一页只能靠正文文案识别、
    靠坐标点击。选择器链在这里全部落空——这正是"改了选择器还是卡死"的原因。
    """

    def locator(self, selector):
        if self.step != "method_picker":
            return _FakeLoginPage.locator(self, selector)
        # 连预渲染的密码框都在，但整页没有一个可用的选择器。
        if selector == "input[type='password'][name='Passwd']":
            return _Field(self, "password")
        return _Missing()

    def text_click_target(self, needles):
        text = self.inner_text("body").lower()
        for needle in needles:
            if needle in text:
                return {"x": 120.0, "y": 480.0, "text": needle}
        return None

    def on_mouse_click(self, x, y):
        if self.step == "method_picker":
            self.clicks.append("enter_password_by_text")
            self.step = "password"

    def visible_options(self):
        return ["Enter your password", "Use your passkey", "Try another way"]


def test_boq_method_picker_is_clicked_by_text_when_no_selector_matches():
    """线上这一页没有能用的选择器。识别必须走正文文案，点击必须走坐标——
    两者缺一，自动登录就退回到"卡在 Welcome 页"的老样子。"""
    _configure(with_totp=True)
    page = _BoqMethodPickerPage(script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["enter_password_by_text"]
    assert page.mouse_clicks == [(120.0, 480.0)]
    assert page.submissions == [("password", "pw")]


def test_boq_method_picker_reports_what_the_page_offered_when_it_cannot_click():
    """点不动时要把这页上有哪些可点项写进日志，否则只能靠截图来回猜。"""
    _configure()

    class _Unclickable(_BoqMethodPickerPage):
        def text_click_target(self, needles):
            return None

    page = _Unclickable()
    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == "method_picker_click_failed"
    assert page.mouse_clicks == []


def test_password_page_with_a_try_another_way_link_is_still_the_password_step():
    """真机形状（2026-08-05 用只读探针在 /v3/signin/challenge/pwd 上取到）：
    密码页底部本来就有一个「Try another way」链接，password_input、next_btn、
    try_another_way 三者同时命中。只凭 try_another_way 判 2FA，就会在密码页上
    点掉那个链接、跳去登录方式选择页，最后报 no_authenticator_option——那正是
    用户看到的"卡在 Welcome 页"。
    """
    _configure(with_totp=True)

    class _PasswordPageWithTryAnotherWay(_FakeLoginPage):
        def inner_text(self, selector):
            if selector != "body":
                return ""
            if self.step != "password":
                return super().inner_text(selector)
            return ("Welcome me@example.com Enter your password Show password "
                    "Next Try another way")

        def locator(self, selector):
            if (self.step == "password"
                    and selector == "button:has-text('Try another way')"):
                # 点到它就等于走错路：用例会在 clicks 里看到这次点击。
                return _Action(self, "try_another_way", "unknown-interstitial")
            return super().locator(selector)

    page = _PasswordPageWithTryAnotherWay(
        step="password", script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == []
    assert page.submissions == [("password", "pw")]


def test_real_password_page_is_not_mistaken_for_the_method_picker():
    """反向护栏：真正的密码页上，Material 的浮动 label 也是「Enter your password」
    这几个字。拿选项文案判定就会在 label 上反复点而永远填不进密码——所以判定
    只认标题「Choose how you want to sign in」。"""
    _configure()

    class _PasswordPage(_FakeLoginPage):
        def inner_text(self, selector):
            if selector != "body":
                return ""
            if self.step != "password":
                return super().inner_text(selector)
            return "Welcome me@example.com Enter your password Show password Next"

    page = _PasswordPage(step="password", script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.submissions == [("password", "pw")]
    assert page.mouse_clicks == []


def test_method_picker_chooses_password_instead_of_try_another_way():
    _configure(with_totp=True)
    page = _MethodPickerPage(script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["enter_password"]
    assert page.submissions == [("password", "pw")]


def test_method_picker_without_totp_configured_still_reaches_the_password_form():
    """这一页跟两步验证无关，没配 TOTP 的账号也必须能走到密码框。"""
    _configure(with_totp=False)
    page = _MethodPickerPage(script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["enter_password"]


def test_method_picker_that_never_moves_is_not_clicked_forever():
    """点不动时要如实报这一页走不通，而不是空转到超时。"""
    _configure()

    class _StuckMethodPicker(_MethodPickerPage):
        def locator(self, selector):
            if selector == "[data-challengetype='1']":
                return _Action(self, "enter_password", "method_picker")
            return super().locator(selector)

    page = _StuckMethodPicker()
    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == "method_picker_stuck"
    assert page.clicks == ["enter_password", "enter_password"]
    assert page.submissions == []


# ── 账号选择页 ──────────────────────────────────────────────────
# 2026-08-05 线上事故：账号 k1anmo58 检测到掉登录后 1 秒内就报 chooser_stuck。
# 一秒是扫不完那几个选择器的——列表还没进 DOM 时 count() 立刻返回 0，实现
# 扫一遍就下了结论。这一组用例全部围绕"必须等列表渲染"。

def test_chooser_waits_for_the_account_list_to_render():
    _configure()
    page = _ChooserPage(identifiers=["me@example.com"], render_after_scans=3,
                        script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["account:me@example.com"]
    assert page.submissions == [("password", "pw")]


def test_chooser_matches_the_account_row_case_insensitively():
    """Google 列表里显示的邮箱大小写未必和用户在号池里填的一致。"""
    _configure()
    page = _ChooserPage(identifiers=["ME@Example.com"],
                        script={("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["account:ME@Example.com"]


def test_chooser_falls_back_to_use_another_account_after_it_renders():
    _configure()
    page = _ChooserPage(use_another=True, render_after_scans=2, script={
        ("email", "ok"): "password", ("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.clicks == ["use_another_account"]
    assert page.submissions == [("email", "me@example.com"), ("password", "pw")]


def test_chooser_still_gives_up_when_nothing_ever_renders():
    """等待是为了少误判，不是为了赖着不走：等满预算仍是空页面就照旧退回人工。"""
    _configure()
    page = _ChooserPage()

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is False
    assert result.reason == "chooser_stuck"
    assert page.submissions == []


def test_chooser_url_rendering_the_email_form_is_handled_as_the_email_step():
    """环境里会话全部过期时，Google 就地把列表换成邮箱表单，URL 仍是
    accountchooser。按 chooser 处理会死在"找不到账号行"，其实填邮箱就能走完。"""
    _configure()

    class _ChooserUrlEmailForm(_FakeLoginPage):
        @property
        def url(self):
            if self.step == "done":
                return _FLOW_URL
            return "https://accounts.google.com/v3/signin/accountchooser"

    page = _ChooserUrlEmailForm(step="email", script={
        ("email", "ok"): "password", ("password", "ok"): "done"})

    result = auto_login.try_auto_login(page, user_id="u1")

    assert result.ok is True
    assert page.submissions == [("email", "me@example.com"), ("password", "pw")]


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


# ── 掉登录的识别本身 ────────────────────────────────────────────

def test_login_redirect_is_awaited_before_declaring_the_session_alive(monkeypatch):
    """Flow 是 SPA：会话失效的重定向发生在 domcontentloaded **之后**。抢答会把
    掉登录报成"已是登录状态"——2026-08-05 真机上「测试登录」就这么答错过，
    而这正是"登录失效识别不了"的源头。
    """
    from integrations.google_fx.utils import browser as browser_utils

    monkeypatch.setattr(browser_utils.time, "sleep", lambda s: None)
    monkeypatch.setattr(browser_utils, "_flow_workspace_ready", lambda page: False)

    calls = {"n": 0}

    def _late_redirect(page):
        # 前三次问都还停在 labs.google，第四次才跳到 accounts.google.com。
        calls["n"] += 1
        return calls["n"] > 3

    monkeypatch.setattr(browser_utils, "is_google_login_page", _late_redirect)

    assert browser_utils.wait_for_login_redirect(object(), timeout_seconds=5) is True
    assert calls["n"] == 4


def test_login_redirect_wait_returns_early_once_the_workspace_is_up(monkeypatch):
    """已经进了工作台就别白等满预算——这个等待挂在每次导航之后。"""
    from integrations.google_fx.utils import browser as browser_utils

    monkeypatch.setattr(browser_utils.time, "sleep", lambda s: None)
    monkeypatch.setattr(browser_utils, "is_google_login_page", lambda page: False)
    monkeypatch.setattr(browser_utils, "_flow_workspace_ready", lambda page: True)

    assert browser_utils.wait_for_login_redirect(object(), timeout_seconds=60) is False


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


def test_attempt_auto_login_uses_runtime_default_account_when_user_id_is_omitted(monkeypatch):
    """Google FX 主服务不显式传账号时，也必须使用当前默认 AdsPower 环境凭据。"""
    from integrations.google_fx.utils import browser as browser_utils

    _configure(user_id="default-fx")
    monkeypatch.setattr(browser_utils, "get_runtime_default_user_id", lambda: "default-fx")
    # 本模块的 autouse fixture 为其它状态机用例把共享解析器收紧成“只认显式值”；
    # 这条用例专门验证 fallback，因此恢复与生产一致的优先级语义。
    monkeypatch.setattr(
        browser_utils.account_binding, "resolve_account",
        lambda explicit=None, fallback=None: str(explicit or fallback or "").strip(),
    )
    page = _FakeLoginPage(step="email", script={
        ("email", "ok"): "password",
        ("password", "ok"): "done",
    })

    assert browser_utils.attempt_auto_login(page, user_id=None) is True
    assert page.submissions == [("email", "me@example.com"), ("password", "pw")]
