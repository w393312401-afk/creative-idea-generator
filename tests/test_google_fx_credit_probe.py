import contextlib
import time

import pytest

from integrations.google_fx.services import google_fx_credit as credit


class _Locator:
    def __init__(self, texts=None, visible=True):
        self.texts = list(texts or [])
        self.visible = visible

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return 1 if self.texts else 0

    def is_visible(self, timeout=None):
        return self.visible and bool(self.texts)

    def inner_text(self, timeout=None):
        if len(self.texts) > 1:
            return self.texts.pop(0)
        return self.texts[0]


class _Page:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def locator(self, selector):
        return self.mapping.get(selector, _Locator())


def test_credit_parser_accepts_balance_lines_only():
    assert credit._extract_credit_number('1050 Google Flow credits') == 1050
    assert credit._extract_credit_number('1,050 credits remaining') == 1050
    assert credit._extract_credit_number('0 Google Flow credits') == 0
    assert credit._extract_credit_number('0 credits') == 0
    assert credit._extract_credit_number('剩余 88 积分') == 88
    assert credit._extract_credit_number('860 Google Flow 点数') == 860
    assert credit._extract_credit_number('Pro plan: 1,000 monthly Google Flow credits') is None
    assert credit._extract_credit_number('Daily Bonus: Enjoy 50 extra credits') is None


def test_is_credit_exhausted_message():
    # 英文真实/变体耗尽短语
    assert credit.is_credit_exhausted_message("0 Google Flow credits") is True
    assert credit.is_credit_exhausted_message("0 AI credits") is True
    assert credit.is_credit_exhausted_message("Get AI credits") is True
    assert credit.is_credit_exhausted_message("Used when you're out of Google Flow credits") is True
    assert credit.is_credit_exhausted_message("You have 0 credits left") is True
    assert credit.is_credit_exhausted_message("Insufficient Google Flow credits") is True
    assert credit.is_credit_exhausted_message("Run out of Flow credits") is True
    assert credit.is_credit_exhausted_message("Not enough credits for this generation") is True
    assert credit.is_credit_exhausted_message("Credits: 0") is True
    assert credit.is_credit_exhausted_message("Google Flow credits: 0") is True
    # 中文耗尽短语
    assert credit.is_credit_exhausted_message("积分余额为 0") is True
    assert credit.is_credit_exhausted_message("当前账号没有足够的积分") is True
    assert credit.is_credit_exhausted_message("积分已用完，请充值") is True
    assert credit.is_credit_exhausted_message("0 积分") is True
    assert credit.is_credit_exhausted_message("无可用积分") is True
    assert credit.is_credit_exhausted_message("额度耗尽") is True
    # 正常带额度/宣传文案不应误判
    assert credit.is_credit_exhausted_message("100 Google Flow credits") is False
    assert credit.is_credit_exhausted_message("1500 monthly Google Flow credits") is False
    assert credit.is_credit_exhausted_message("Generating video...") is False


def test_menu_scan_waits_for_two_stable_reads(monkeypatch):
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'credit_display', ['credit-link'])
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'account_menu_surface', ['account-dialog'])
    page = _Page({'credit-link': _Locator([
        'Loading…', '1050 Google Flow credits', '1050 Google Flow credits'
    ])})
    # 超时给足：这里要断言的是"连读到两次相同数字才接受"，不是它多快超时。
    # 卡着 0.05s 时整套测试并发跑起来第一轮就可能超时，变成偶发失败。
    assert credit._scan_menu_for_credit(page, timeout_seconds=5, poll_interval=0) == 1050


def test_menu_scan_never_reads_pricing_noise_outside_account_surface(monkeypatch):
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'credit_display', ['credit-link'])
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'account_menu_surface', ['account-dialog'])
    page = _Page({
        # 页面其它位置即使有套餐数字，探针也不会读取 body/global text。
        'body': _Locator(['Pro plan: 1,000 monthly Google Flow credits']),
        'account-dialog': _Locator(['Manage your Google Account']),
    })
    assert credit._scan_menu_for_credit(page, timeout_seconds=0, poll_interval=0) is None


def test_menu_scan_stops_immediately_when_browser_disconnects():
    class _Browser:
        def is_connected(self):
            return False

    page = _Page()
    page.context = type('Context', (), {'browser': _Browser()})()
    page.is_closed = lambda: False
    started = time.monotonic()

    with pytest.raises(credit.BrowserSessionClosedError, match='已关闭'):
        credit._scan_menu_for_credit(page, timeout_seconds=30)

    assert time.monotonic() - started < 0.5


def test_menu_scan_rejects_marketing_text_even_inside_dialog(monkeypatch):
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'credit_display', [])
    monkeypatch.setitem(credit.UI_SELECTORS['google_fx'], 'account_menu_surface', ['account-dialog'])
    page = _Page({'account-dialog': _Locator([
        'Upgrade now\n1,000 monthly Google Flow credits'
    ])})
    assert credit._scan_menu_for_credit(page, timeout_seconds=0, poll_interval=0) is None


class _FakeGate:
    """模拟宿主的 FX_CONTROL 闸门：浏览器被别人占着时一直不放行。"""

    def __init__(self, busy=True):
        self.busy = busy
        self.entered = False

    @contextlib.contextmanager
    def __call__(self, kind, cancel_check=None, priority=0, task_id=None):
        while self.busy:
            if cancel_check and cancel_check():
                # 控制面在等待循环里就是这么退出的（FxQueueCancelled 继承 ConnectionError）
                raise ConnectionError('任务在等待 Google FX 队列时被取消')
            time.sleep(0.01)
        self.entered = True
        yield


@pytest.fixture
def _gate(monkeypatch):
    from integrations.google_fx.utils import browser_gate

    gate = _FakeGate()
    monkeypatch.setattr(browser_gate, '_GATE', gate)
    yield gate
    monkeypatch.setattr(browser_gate, '_GATE', None)


def test_probe_gives_up_on_queue_instead_of_hanging_on_a_busy_browser(_gate):
    """浏览器被别的任务占着时，探针等到上限就收摊。

    没有这个上限，探测会挂在同步 HTTP 请求上直到别人用完浏览器（生成任务可以
    是几分钟），而"刷新积分"就是一个同步 POST——用户看到的就是按钮永远转圈。
    """
    started = time.monotonic()
    assert credit.probe_flow_credit('acct_busy', queue_wait_seconds=0.2) is None
    assert time.monotonic() - started < 5
    assert _gate.entered is False
    assert credit.get_last_probe_error_kind('acct_busy') == 'queue'
    assert '排队等待' in credit.get_last_probe_error('acct_busy')


def test_probe_logs_carry_task_id_and_restore_outer_context(monkeypatch, _gate):
    """控制台按 credit_probe_<账号> 过滤时，每一行都必须带同一任务标签。"""
    from integrations.google_fx.utils import logger as fx_logger

    seen_labels = []
    monkeypatch.setattr(
        credit, 'log',
        lambda *_args, **_kwargs: seen_labels.append(fx_logger.current_task_label()),
    )
    outer = fx_logger.set_task_label('outer_task')
    try:
        assert credit.probe_flow_credit(
            'acct_labeled', queue_wait_seconds=0.05, budget_seconds=0.1) is None
        assert seen_labels
        assert set(seen_labels) == {'credit_probe_acct_labeled'}
        assert fx_logger.current_task_label() == 'outer_task'
    finally:
        fx_logger.reset_task_label(outer)


def test_probe_queue_wait_reports_cancellation_separately(_gate):
    cancelled = {'value': True}
    assert credit.probe_flow_credit(
        'acct_cancel', queue_wait_seconds=30, cancel_check=lambda: cancelled['value']) is None
    assert credit.get_last_probe_error_kind('acct_cancel') == 'queue'
    assert '取消' in credit.get_last_probe_error('acct_cancel')


def test_probe_stops_at_its_wall_clock_budget(monkeypatch, _gate):
    """整轮预算封顶：慢步骤把预算耗光后，探针立刻收摊，不再往下走。

    没有这条，一个进不去工作台的账号能连着几分钟独占浏览器（各子步骤的超时是
    各管各的：启动 3×45s + goto 60s + 进工作台 30s + 读菜单 30s 会叠加），
    期间所有账号的"刷新积分"都被判成 FX_BUSY。
    """
    _gate.busy = False

    def slow_start(**kwargs):
        time.sleep(1.0)      # 比预算还长：回来时预算已经见底
        return 'ws://127.0.0.1:1/devtools/browser/fake'

    monkeypatch.setattr(credit, 'get_ads_ws_url', slow_start)
    assert credit.probe_flow_credit('acct_slow', budget_seconds=0.5, queue_wait_seconds=5) is None
    assert _gate.entered is True
    # 真的进了临界区才失败 = 账号/环境侧问题，保留 'probe' 语义（会标记探测失败）
    assert credit.get_last_probe_error_kind('acct_slow') == 'probe'
    assert '预算' in credit.get_last_probe_error('acct_slow')


def test_probe_leaves_no_cancel_context_behind_on_the_thread(monkeypatch, _gate):
    """探针收尾必须清掉自己的 CancelState。

    探针跑在 HTTP handler 线程上，keep-alive 的后续请求复用同一个线程。留着这条
    带 deadline 的状态，下一个自己没建上下文的调用一读 deadline_exceeded() 就是
    True，凭空炸出"请求超时：超出时间预算"。
    """
    from integrations.google_fx.utils import cancel_flag

    _gate.busy = False
    monkeypatch.setattr(credit, 'get_ads_ws_url',
                        lambda **kwargs: (_ for _ in ()).throw(Exception('boom')))

    credit.probe_flow_credit('acct_ctx', budget_seconds=0.5)
    time.sleep(0.6)      # 探针的 deadline 早过了

    assert cancel_flag.current_request_id() is None
    assert cancel_flag.is_cancelled is False
    assert cancel_flag.deadline_exceeded() is False


def test_probe_start_attempts_are_capped_for_the_probe(monkeypatch, _gate):
    """探针给 AdsPower 的启动重试预算必须比生成任务小，否则光启动就能占两分半。"""
    _gate.busy = False
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        raise Exception('boom')

    monkeypatch.setattr(credit, 'get_ads_ws_url', capture)
    credit.probe_flow_credit('acct_args')
    assert seen['max_start_attempts'] == credit.PROBE_START_ATTEMPTS
    assert seen['start_timeout'] == credit.PROBE_START_TIMEOUT_SECONDS
    assert seen['auto_rotate_proxy'] is False


def test_is_google_login_page():
    from integrations.google_fx.utils.browser import is_google_login_page

    class DummyPage:
        def __init__(self, url="", inner_text=""):
            self.url = url
            self._inner_text = inner_text

        def evaluate(self, script):
            url_lower = self.url.lower()
            if 'accounts.google.com' in url_lower or 'signin/accountchooser' in url_lower:
                return True
            text = self._inner_text.lower()
            markers = ['choose an account', 'sign in with google', 'try signing in with a different account',
                       'sign in to continue', 'use another account', '选择账号']
            return any(m in text for m in markers)

    # 包含 Google 登录 URL（如用户截图中显示的 accounts.google.com/v3/signin/accountchooser）
    p1 = DummyPage(url="https://accounts.google.com/v3/signin/accountchooser?client_id=365941595420")
    assert is_google_login_page(p1) is True

    # 包含登录特征 DOM
    p2 = DummyPage(url="https://labs.google/fx/tools/flow", inner_text="Choose an account to continue to AI Test Kitchen")
    assert is_google_login_page(p2) is True

    # 正常工作台页面
    p3 = DummyPage(url="https://labs.google/fx/tools/flow", inner_text="Create with Google Flow")
    assert is_google_login_page(p3) is False

    # labs.google 的 Auth.js 错误中转页（尚未进入 accounts.google.com）。
    p4 = DummyPage(
        url="https://labs.google/fx/tools/flow",
        inner_text="Try signing in with a different account.",
    )
    assert is_google_login_page(p4) is True


def test_is_credit_exhausted_message_comprehensive():
    # 英文关键词与变体
    assert credit.is_credit_exhausted_message("Out of credits") is True
    assert credit.is_credit_exhausted_message("You've run out of credits") is True
    assert credit.is_credit_exhausted_message("You have run out of credits to generate video") is True
    assert credit.is_credit_exhausted_message("Insufficient credits") is True
    assert credit.is_credit_exhausted_message("Not enough credits to continue") is True
    assert credit.is_credit_exhausted_message("No credits left in your account") is True
    assert credit.is_credit_exhausted_message("0 credits remaining") is True
    assert credit.is_credit_exhausted_message("Credits: 0") is True
    assert credit.is_credit_exhausted_message("0 Google Flow credits") is True
    assert credit.is_credit_exhausted_message("0 credits") is True
    assert credit.is_credit_exhausted_message("0 credit") is True
    assert credit.is_credit_exhausted_message("Resource_exhausted: quota exceeded") is True
    assert credit.is_credit_exhausted_message("Not enough Google Flow and AI credits to perform this action") is True

    # 中文关键词与变体
    assert credit.is_credit_exhausted_message("当前账号积分不足") is True
    assert credit.is_credit_exhausted_message("没有足够的积分生成视频") is True
    assert credit.is_credit_exhausted_message("积分已用完") is True
    assert credit.is_credit_exhausted_message("点数不足") is True
    assert credit.is_credit_exhausted_message("点数已用完") is True
    assert credit.is_credit_exhausted_message("额度不足") is True
    assert credit.is_credit_exhausted_message("额度耗尽") is True
    assert credit.is_credit_exhausted_message("配额已耗尽") is True
    assert credit.is_credit_exhausted_message("剩余 0 积分") is True
    assert credit.is_credit_exhausted_message("积分: 0") is True
    assert credit.is_credit_exhausted_message("0 积分") is True
    assert credit.is_credit_exhausted_message("0积分") is True
    assert credit.is_credit_exhausted_message("0 点数") is True
    assert credit.is_credit_exhausted_message("0点数") is True
    assert credit.is_credit_exhausted_message("点数余额为 0") is True
    assert credit.is_credit_exhausted_message("积分余额为 0") is True
    assert credit.is_credit_exhausted_message("无可用积分") is True

    # 正常正数积分（严防以 0 结尾的正数被子串 "0 credits" / "0积分" 误伤）
    assert credit.is_credit_exhausted_message("100 credits") is False
    assert credit.is_credit_exhausted_message("100 Google Flow credits") is False
    assert credit.is_credit_exhausted_message("1000 credits") is False
    assert credit.is_credit_exhausted_message("500 credits") is False
    assert credit.is_credit_exhausted_message("50 credits") is False
    assert credit.is_credit_exhausted_message("200 credits") is False
    assert credit.is_credit_exhausted_message("10 credits") is False
    assert credit.is_credit_exhausted_message("1 credit") is False
    assert credit.is_credit_exhausted_message("100积分") is False
    assert credit.is_credit_exhausted_message("500积分") is False
    assert credit.is_credit_exhausted_message("1000 积分") is False
    assert credit.is_credit_exhausted_message("50 点数") is False
    assert credit.is_credit_exhausted_message("100点数") is False
    assert credit.is_credit_exhausted_message("Credits: 100") is False
    assert credit.is_credit_exhausted_message("Credits: 50") is False
    assert credit.is_credit_exhausted_message("剩余 100 积分") is False
    assert credit.is_credit_exhausted_message("积分余额: 500") is False

    # 正常非积分耗尽报错、营销文案与进度数字
    assert credit.is_credit_exhausted_message("未找到底部配置按钮") is False
    assert credit.is_credit_exhausted_message("网络连接超时") is False
    assert credit.is_credit_exhausted_message("Target page is closed") is False
    assert credit.is_credit_exhausted_message("0% generating") is False
    assert credit.is_credit_exhausted_message("seed: 0, 1080p, 1 credit") is False
    assert credit.is_credit_exhausted_message("Daily Bonus: Enjoy 50 extra credits") is False
    assert credit.is_credit_exhausted_message("Upgrade plan to get more credits") is False
    assert credit.is_credit_exhausted_message("Rate limit exceeded, please retry in 5s") is False
    assert credit.is_credit_exhausted_message("") is False


def test_detect_page_credit_exhaustion():
    class DummyDialogPage:
        def __init__(self, dialog_texts=None, credit_text=None):
            self.dialog_texts = dialog_texts or []
            self.credit_text = credit_text

        def evaluate(self, script):
            return self.dialog_texts

        def locator(self, selector):
            return _Locator([self.credit_text] if self.credit_text else [])

    # 1. 弹窗命中积分耗尽
    p1 = DummyDialogPage(dialog_texts=["You've run out of credits\nUpgrade now to continue creating."])
    res1 = credit.detect_page_credit_exhaustion(p1)
    assert res1 is not None
    assert "页面提示积分耗尽" in res1
    assert "You've run out of credits" in res1

    # 2. 顶栏/菜单显示 0 积分
    p2 = DummyDialogPage(dialog_texts=[], credit_text="0 Google Flow credits")
    res2 = credit.detect_page_credit_exhaustion(p2)
    assert res2 is not None
    assert "0" in res2

    # 3. 正常正数页面（1050 / 100 / 500 / 50 积分均不能被误判）
    p3 = DummyDialogPage(dialog_texts=[], credit_text="1050 Google Flow credits")
    assert credit.detect_page_credit_exhaustion(p3) is None

    p4 = DummyDialogPage(dialog_texts=[], credit_text="100 Google Flow credits")
    assert credit.detect_page_credit_exhaustion(p4) is None

    p5 = DummyDialogPage(dialog_texts=[], credit_text="500 Google Flow credits")
    assert credit.detect_page_credit_exhaustion(p5) is None

    p6 = DummyDialogPage(dialog_texts=[], credit_text="50 Google Flow credits")
    assert credit.detect_page_credit_exhaustion(p6) is None


def test_is_manageable_user_page_and_find_or_create_page_exclusion():
    from integrations.google_fx.utils.browser import _is_manageable_user_page, find_or_create_page

    class DummyPageInternal:
        def __init__(self, url):
            self.url = url
            self.closed = False
            self.brought_to_front = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def bring_to_front(self):
            self.brought_to_front = True

        def goto(self, url, **kwargs):
            self.url = url

    # 1. 验证内部协议页面识别
    assert _is_manageable_user_page(None) is False
    assert _is_manageable_user_page(DummyPageInternal("chrome://omnibox-popup.top-chrome/")) is False
    assert _is_manageable_user_page(DummyPageInternal("chrome-extension://abcdef/popup.html")) is False
    assert _is_manageable_user_page(DummyPageInternal("devtools://devtools/bundled/inspector.html")) is False
    assert _is_manageable_user_page(DummyPageInternal("about:blank")) is True
    assert _is_manageable_user_page(DummyPageInternal("https://labs.google/fx/tools/flow")) is True

    # 2. 验证 find_or_create_page 不会选择内部页面，也不会调用内部页面的 close()
    omnibox_p1 = DummyPageInternal("chrome://omnibox-popup.top-chrome/")
    omnibox_p2 = DummyPageInternal("chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html")
    flow_page = DummyPageInternal("https://labs.google/fx/tools/flow")
    extra_user_page = DummyPageInternal("https://example.com")

    class DummyContext:
        def __init__(self, pages):
            self.pages = list(pages)

        def new_page(self):
            p = DummyPageInternal("about:blank")
            self.pages.append(p)
            return p

    ctx = DummyContext([omnibox_p1, omnibox_p2, flow_page, extra_user_page])
    chosen = find_or_create_page(ctx, "/fx/tools/flow", auto_login=False)

    assert chosen is flow_page
    # 内部页面绝不能被 close
    assert omnibox_p1.closed is False
    assert omnibox_p2.closed is False
    # 多余的普通用户页被正常清理
    assert extra_user_page.closed is True
