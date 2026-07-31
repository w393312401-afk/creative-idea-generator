# -*- coding: utf-8 -*-
"""
🔎 Flow 积分探针
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
连上指定 AdsPower 账号的浏览器，打开 Flow 页面，读取 UI 上显示的积分余额。

✅ 2026-07-23 已用真实登录账号（AdsPower user_id: k1eu8kc5）实测确认：Flow
项目列表首页本身不显示积分数字，要点开顶栏头像菜单（ui_selectors.py 的
account_menu_trigger）才会弹出账号信息对话框，里面有一行
"{{数字}} Google Flow credits"。读完立刻按 Escape 关闭对话框，恢复页面原状。

⚠️ 硬性要求（用户明确指定）：检测必须走"点开头像菜单"这条路径，头像点不开
就直接判定失败返回 None，绝不允许退化成"抓取整页 HTML/文字里恰好出现的
数字"——Flow 页面的 i18n 文案里到处是各种套餐档位的定价宣传数字（如
"1,000 monthly Google Flow credits"），不先确认头像菜单真的点开就扫整页
文字，很容易把宣传文案误判成账号真实余额。

同时提供 is_credit_exhausted_message()：给视频批量生成失败时的原始错误文案
做一次关键词扫描，命中则认为是"账号积分耗尽"而非普通生成失败——这组关键词
是猜测性的，等真实遇到一次耗尽再用实测文案回填。
"""

import contextlib
import re
import threading
import time
from typing import Optional

from ..utils.browser import (
    get_ads_ws_url, find_or_create_page, ensure_flow_workspace,
    flow_onboarding_required, is_google_login_page, random_sleep,
    attempt_auto_login, BrowserSessionClosedError, _browser_session_is_closed,
)
from ..utils.browser_gate import browser_slot
from ..utils.logger import log, set_task_label, reset_task_label
from ..ui_selectors import UI_SELECTORS

# 真实文案是 "1050 Google Flow credits"（数字和 "credits" 之间隔着 1~3 个单词），
# 兜底同时兼容更简单的 "N credits" / "N 积分" / "Credits: N" 措辞。
_CREDIT_LINE_PATTERN = re.compile(
    r"^\s*(\d[\d,]*)\s+(?:(?:Google\s+)?Flow\s+)?credits?"
    r"(?:\s+(?:remaining|left|available))?\s*$",
    re.IGNORECASE,
)
_CREDIT_ZH_LINE_PATTERN = re.compile(
    r"^\s*(?:剩余\s*)?(\d[\d,]*)\s*(?:Google\s*Flow\s*)?(?:积分|点数)(?:余额|剩余)?\s*$",
    re.IGNORECASE,
)
_CREDIT_LABEL_PATTERN = re.compile(
    r"^\s*(?:Credits?|积分|点数)[:：]?\s*(\d[\d,]*)\s*$",
    re.IGNORECASE,
)
_PROBE_ERRORS = {}
_PROBE_ERRORS_LOCK = threading.Lock()

# ⏱️ 探针预算（2026-07-27）
# 探针在 FX_CONTROL 队列里独占浏览器，而每个子步骤此前只各管各的超时：AdsPower
# 启动 3 次重试 ×45s、goto 60s、进工作台 30s、读菜单 30s——一个进不去工作台的
# 账号能占着浏览器好几分钟。期间任何账号点「刷新积分」都被控制面判成 FX_BUSY
# 直接 409，整个功能看上去就是坏的（实测日志里连续 20+ 条 409，最后只能杀进程）。
# 所以整轮探测有一个硬性墙钟预算，各步骤的超时都从剩余预算里取。
PROBE_BUDGET_SECONDS = 150.0
# 排队等待单独限时：控制面的 queue_wait_timeout_seconds 是全局配置且默认 0（不限），
# 而这个探测挂在一个同步 HTTP 请求上，不能无限期挂着。
PROBE_QUEUE_WAIT_SECONDS = 100.0
# 探针不是生成任务，不值得为它把 AdsPower 重启三轮——留一次重试即可，剩下的
# 预算要留给真正读积分的部分。
PROBE_START_ATTEMPTS = 2
PROBE_START_TIMEOUT_SECONDS = 25


class CreditProbeTimeout(RuntimeError):
    """整轮探测超出墙钟预算。"""


class CreditProbeCancelled(RuntimeError):
    """探测在排队或执行期间被取消。"""

# 猜测性关键词，未经真实"账号耗尽"案例验证；命中即认为该次失败是积分不足，
# 而不是网络/画布等其它原因。
CREDIT_EXHAUSTED_KEYWORDS = [
    "out of credits",
    "insufficient credits",
    "not enough credits",
    "no credits left",
    "you've run out of credits",
    "积分不足",
    "没有足够的积分",
    "积分已用完",
]


def _extract_credit_number(text: str) -> Optional[int]:
    """只接受一整行余额文案，拒绝 monthly/plan/bonus 等宣传数字。"""
    normalized = (text or "").replace("\xa0", " ")
    candidates = [line.strip() for line in normalized.splitlines() if line.strip()]
    # 部分组件把数字和标签拆成节点，inner_text 可能换行；同时尝试合并后的短文本。
    if len(candidates) <= 3:
        candidates.append(" ".join(candidates))
    for line in candidates:
        match = (
            _CREDIT_LINE_PATTERN.fullmatch(line)
            or _CREDIT_ZH_LINE_PATTERN.fullmatch(line)
            or _CREDIT_LABEL_PATTERN.fullmatch(line)
        )
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _set_probe_error(user_id: str, message: Optional[str], kind: str = 'probe'):
    """记录本次探测失败原因。

    kind='queue' 表示"探测压根没跑起来"（浏览器被别人占着、排队超时、被取消），
    跟"跑了但读不到积分"是两回事：前者不该把账号标记成探测失败，不然一次队列
    拥堵会让一排好账号在列表里挂上"⚠️ 最近探测失败"。
    """
    with _PROBE_ERRORS_LOCK:
        if message:
            _PROBE_ERRORS[str(user_id)] = {'message': str(message), 'kind': kind}
        else:
            _PROBE_ERRORS.pop(str(user_id), None)


def get_last_probe_error(user_id: str) -> Optional[str]:
    with _PROBE_ERRORS_LOCK:
        entry = _PROBE_ERRORS.get(str(user_id))
    return entry['message'] if entry else None


def get_last_probe_error_kind(user_id: str) -> Optional[str]:
    """'queue' = 没拿到浏览器；'probe' = 真的探测失败；None = 没有记录。"""
    with _PROBE_ERRORS_LOCK:
        entry = _PROBE_ERRORS.get(str(user_id))
    return entry['kind'] if entry else None


def _dismiss_popups(page):
    """尽力关掉挡路的通用弹窗/促销横幅（如 "Daily Bonus: Enjoy 50 extra
    credits..." + Dismiss 按钮），失败不影响主流程——ui_selectors.py 的
    common.close_popup_btns 本来就是给这类弹窗用的通用清单。"""
    for selector in UI_SELECTORS.get("common", {}).get("close_popup_btns", []):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=1500)
        except Exception:
            continue


def _try_click_once(page, selectors) -> bool:
    """对候选选择器各尝试点击一次，命中即返回 True。不在这里做轮询——外层
    `_read_credit_from_account_menu` 会把"点击 + 校验是否真的生效"作为一个
    整体重试单元，而不是分开重试"点击"和"读取"两个独立步骤。"""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0 or not locator.is_visible(timeout=1000):
                continue
            locator.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def is_credit_exhausted_message(message: str) -> bool:
    """粗粒度关键词匹配：判断一条生成失败文案是否指向"账号积分耗尽"。"""
    text = (message or "").lower()
    if not text:
        return False
    return any(keyword.lower() in text for keyword in CREDIT_EXHAUSTED_KEYWORDS)


@contextlib.contextmanager
def _probe_cancel_context(task_id: str, wall_deadline: float):
    """把探针登记进 per-request 取消注册表，并给它一个绝对 deadline。

    有了这条登记，控制台对 `credit_probe_<user_id>` 点"取消"才真的能停下正在跑的
    探测（cancel_flag.cancel_request 按 request_id 精确置位）；deadline 则让运行时
    里那些 deadline_exceeded() 兜底的等待循环提前收摊。

    ⚠️ 已经处在别人的取消上下文里时（选号时的 stale 刷新跑在生成任务的线程上）
    不新建：init_context 会覆盖 contextvar，把外层生成任务的 CancelState 顶掉，
    那之后用户对生成任务点取消就再也传不进 FX 运行时了。
    """
    from ..utils import cancel_flag

    if cancel_flag.current_request_id() is not None:
        class _OuterState:
            """只读外层状态：嵌套探测该跟着外层生成任务一起被取消。"""

            @property
            def val(self):
                return cancel_flag.is_cancelled

        yield _OuterState()
        return
    state = cancel_flag.init_context(task_id, deadline=wall_deadline)
    cancel_flag.register(task_id, state)
    try:
        yield state
    finally:
        cancel_flag.unregister(task_id)
        # 必须清干净：探针跑在 HTTP handler 线程上，keep-alive 的后续请求复用同一个
        # 线程，留着这条已过期的 deadline 会让下一个调用凭空"超出时间预算"。
        cancel_flag.clear_context()


def probe_flow_credit(
    user_id: str,
    port: Optional[str] = None,
    budget_seconds: Optional[float] = None,
    queue_wait_seconds: Optional[float] = None,
    cancel_check=None,
) -> Optional[int]:
    """打开指定账号的 Flow 页面，点开账号菜单读出积分余额；失败返回 None（不
    抛异常，调用方按"保留旧缓存值"处理，避免探测失败误伤好账号）。

    ⚠️ 2026-07-26：本函数会**真的启动/复用浏览器**，所以必须经过 browser_slot
    闸门（宿主把它接到 FX_CONTROL 队列）。此前它完全绕开队列，生成任务运行中点一下
    "刷新积分"就会抢同一个 AdsPower profile、bring_to_front、按 Escape，直接搅乱
    Flow 画布状态。选号时的 stale 刷新发生在已持有 slot 的线程里，由控制面的可重入
    判断直接放行，不会自锁。

    ⚠️ 2026-07-27：整轮探测有硬性墙钟预算（budget_seconds），排队等待另有上限
    （queue_wait_seconds）。没有这两条时，一个卡在产品介绍页/AdsPower 起不来的账号
    会占着浏览器好几分钟，把所有账号的"刷新积分"一起拖成 FX_BUSY。
    """
    _set_probe_error(user_id, None)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("⚠️ playwright 未安装，无法探测积分", "积分探针")
        _set_probe_error(user_id, "playwright 未安装，无法探测积分")
        return None

    # 下限只是防 0/负数，不代表推荐值：生产路径都走 PROBE_* 默认值。
    budget = max(0.5, float(PROBE_BUDGET_SECONDS if budget_seconds is None else budget_seconds))
    queue_wait = max(0.1, float(
        PROBE_QUEUE_WAIT_SECONDS if queue_wait_seconds is None else queue_wait_seconds))
    deadline = time.monotonic() + budget
    wait_deadline = time.monotonic() + min(queue_wait, budget)
    task_id = f'credit_probe_{user_id}'
    log_token = set_task_label(task_id)
    log(f"开始探测账号 {user_id} 的 Flow 积分", "积分探针")

    # 进入临界区后才登记的 CancelState：控制台对 credit_probe_<user_id> 点"取消"
    # 就是把它置位。只在拿到自己的 state 之后才读它——排队阶段读 contextvar 可能
    # 撞上同一个线程上一轮留下的已取消状态，白白把这次探测判成取消。
    probe_state = {'obj': None}

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _cancelled() -> bool:
        state = probe_state['obj']
        if state is not None and state.val:
            return True
        if cancel_check is None:
            return False
        try:
            return bool(cancel_check())
        except Exception:
            # 谓词自己炸了按取消处理：宁可早停也不要空转到超时。
            return True

    def _slot_cancel() -> bool:
        """排队阶段的放弃条件：调用方取消，或等待超过 queue_wait。"""
        return _cancelled() or time.monotonic() >= wait_deadline

    def _checkpoint():
        if _cancelled():
            raise CreditProbeCancelled("探测已被取消")
        if _remaining() <= 0:
            raise CreditProbeTimeout(f"整轮探测超过 {budget:.0f}s 预算仍未读到积分")

    def _step_timeout(cap: float) -> float:
        """单步超时永远不超过剩余总预算，保证整轮不会突破 budget。"""
        return max(1.0, min(float(cap), _remaining()))

    credit = None
    page_text_snippet = ""
    entered_slot = False
    try:
        # 探针优先级高于生成任务：它很短，且选号要靠它的结果，不该排在长任务后面。
        with browser_slot('credit_probe', cancel_check=_slot_cancel,
                          priority=40, task_id=task_id):
            entered_slot = True
            _checkpoint()
            with _probe_cancel_context(task_id, wall_deadline=time.time() + _remaining()) as state:
                probe_state['obj'] = state
                # 先把浏览器拉起来再起 playwright 驱动：AdsPower 起不来是最常见的
                # 失败，没必要为它先付一次驱动进程的启动开销。
                ws_url = get_ads_ws_url(
                    user_id=user_id, port=port, auto_rotate_proxy=False,
                    max_start_attempts=PROBE_START_ATTEMPTS,
                    start_timeout=PROBE_START_TIMEOUT_SECONDS,
                )
                _checkpoint()
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(ws_url, timeout=int(_step_timeout(20.0) * 1000))
                    context = browser.contexts[0]
                    flow_url = "https://labs.google/fx/tools/flow"
                    page = find_or_create_page(
                        context, "/fx/tools/flow", fallback_url=flow_url,
                        user_id=user_id,
                        auto_login_timeout_seconds=_step_timeout(45.0),
                        cancel_check=_cancelled,
                        context_label="积分探针浏览器启动",
                    )
                    _checkpoint()
                    if "/fx/tools/flow" not in str(getattr(page, "url", "")):
                        page.goto(flow_url, timeout=int(_step_timeout(45.0) * 1000),
                                  wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state(
                            "domcontentloaded", timeout=int(_step_timeout(15.0) * 1000))
                    except Exception:
                        pass

                    _checkpoint()
                    if is_google_login_page(page):
                        # 配了凭据就先自己登一次再说。探针是选号的前置动作，
                        # 一个"只是掉了登录"的好账号不该因此被跳过。
                        # user_id 必须显式传：探针不绑定 account_binding 上下文。
                        if not attempt_auto_login(page, user_id=user_id,
                                                  context_label="积分探针",
                                                  cancel_check=_cancelled):
                            _set_probe_error(user_id, "🔒 遇到 Google 登录页面，请人工打开 AdsPower 完成登录")
                            log(f"🔒 账号 {user_id} 积分探测终止：检测到 Google 登录页面，提醒人工处理", "积分探针")
                            return None
                        _checkpoint()

                    if not ensure_flow_workspace(page, timeout_seconds=_step_timeout(25.0),
                                                 user_id=user_id):
                        if is_google_login_page(page):
                            _set_probe_error(user_id, "🔒 遇到 Google 登录页面，请人工打开 AdsPower 完成登录")
                            log(f"🔒 账号 {user_id} 积分探测终止：检测到 Google 登录页面，提醒人工处理", "积分探针")
                            return None
                        if flow_onboarding_required(page):
                            raise RuntimeError("Flow 首次使用隐私说明等待用户确认")
                        raise RuntimeError("Flow 仍停留在产品介绍页，未能进入工作台")

                    _checkpoint()
                    credit = _read_credit_from_account_menu(
                        page,
                        overall_timeout_seconds=_step_timeout(30.0),
                        should_stop=_cancelled,
                    )
                    if credit is None:
                        # 读不到积分时先分清原因：被取消/超预算要走各自的文案，
                        # 不能一律记成"菜单里没有可信余额"（那是在冤枉账号）。
                        _checkpoint()
                        try:
                            page_text_snippet = page.inner_text("body")[:300]
                        except Exception:
                            pass

        if credit is None:
            if not get_last_probe_error(user_id):
                _set_probe_error(user_id, "账号菜单未打开，或菜单内未加载出可信积分余额")
            log(
                f"⚠️ 账号 {user_id} 积分探测未命中: {get_last_probe_error(user_id)}",
                "积分探针",
            )
            if page_text_snippet:
                log(f"📄 页面文字片段（供排查选择器用）: {page_text_snippet}", "积分探针")
        return credit
    except Exception as e:
        # 还没进临界区就抛出 = 卡在队列上（控制面的 FxQueueCancelled/FxQueueTimeout）。
        # 这跟"探测本身失败"是两回事，报错文案要能让用户看出该等还是该查账号。
        if not entered_slot:
            message = ("浏览器被其它 FX 任务占用，"
                       f"排队等待 {queue_wait:.0f}s 仍未拿到浏览器，请稍后再试")
            if _cancelled():
                message = "探测在排队时被取消"
            _set_probe_error(user_id, message, kind='queue')
            log(f"⏳ 账号 {user_id} 积分探测未能进入浏览器队列: {message}", "积分探针")
            return None
        if isinstance(e, CreditProbeCancelled):
            # 人为中止，跟账号健康无关，别记成"最近探测失败"。
            _set_probe_error(user_id, str(e), kind='queue')
            log(f"🛑 账号 {user_id} 积分探测被取消", "积分探针")
            return None
        if isinstance(e, BrowserSessionClosedError):
            # 人工关闭浏览器不是页面结构错误，必须快速、准确地结束，不能误报成
            # "仍停留在产品介绍页"并把剩余导航超时全部耗完。
            _set_probe_error(user_id, str(e), kind='probe')
            log(f"🛑 账号 {user_id} 积分探测停止: {e}", "积分探针")
            return None
        _set_probe_error(user_id, f"{type(e).__name__}: {e}"
                         if not isinstance(e, CreditProbeTimeout) else str(e))
        log(f"❌ 账号 {user_id} 积分探测异常: {type(e).__name__}: {e}", "积分探针")
        return None
    finally:
        # 探针常跑在复用的 HTTP handler 线程上，必须精确还原外层上下文，不能泄漏到
        # 同一线程承接的下一次请求。
        reset_task_label(log_token)


def _account_menu_is_open(page) -> bool:
    for selector in UI_SELECTORS.get("google_fx", {}).get("credit_display", []):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible(timeout=500):
                return True
        except Exception:
            continue
    for selector in UI_SELECTORS.get("google_fx", {}).get("account_menu_surface", []):
        try:
            locator = page.locator(selector).last
            if locator.count() and locator.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _wait_for_account_menu(page, timeout_seconds: float = 2.5) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _browser_session_is_closed(page):
            raise BrowserSessionClosedError(
                "AdsPower 浏览器或 Flow 标签页已关闭，积分探测已立即停止")
        if _account_menu_is_open(page):
            return True
        time.sleep(0.2)
    return False


def _read_credit_from_account_menu(page, overall_timeout_seconds: float = 30.0,
                                   should_stop=None) -> Optional[int]:
    """点开顶栏头像菜单，读账号对话框里的积分文字，读完关闭对话框。

    2026-07-23 用户明确要求：检测必须走"点开头像菜单"这条路径，不能靠抓取
    整页 HTML/文字里恰好出现的数字兜底——Flow 页面的 i18n 文案里到处都是
    "1,000 monthly Google Flow credits" 这类定价方案宣传文案（不同套餐档位
    各种数字都有），如果不先确认头像菜单真的点开了就去扫整页文字，很容易
    把定价宣传的数字误当成账号真实余额。所以：全程只信 credit_display 里
    列的具体元素选择器，绝不做"扫全页文字"兜底；读不到就判定失败返回 None。

    2026-07-23 实测踩坑：`.click()` 调用不抛异常 ≠ 页面真的响应了这次点击
    ——账号浏览器刚被重新拉起（冷启动）时，头像按钮有时已经在 DOM 里
    （locator().count() 拿到 1、click() 也不报错），但 React 应用还没完成
    hydration，点击对它没有实际效果，账号对话框根本没弹出来。如果只重试
    "点击"本身或只重试"读取积分"，两边各自看都"成功"了（点击没报错、
    scan 只是暂时没找到），于是把"点了但没反应"这种情况漏掉。修复方式：
    把"点击头像 + 尝试读积分"当成一个整体重试单元，反复整轮重试，而不是
    分别重试两个独立步骤。"""
    try:
        page.bring_to_front()
    except Exception:
        pass

    trigger_selectors = UI_SELECTORS.get("google_fx", {}).get("account_menu_trigger", [])
    deadline = time.monotonic() + overall_timeout_seconds

    while time.monotonic() < deadline:
        if _browser_session_is_closed(page):
            raise BrowserSessionClosedError(
                "AdsPower 浏览器或 Flow 标签页已关闭，积分探测已立即停止")
        if should_stop is not None and should_stop():
            log("🛑 探测在读取账号菜单期间被取消", "积分探针")
            return None
        if is_google_login_page(page):
            log("🔒 探测期间页面为 Google 登录页，中断探测", "积分探针")
            return None
        _dismiss_popups(page)
        menu_open = _account_menu_is_open(page)
        clicked = menu_open or _try_click_once(page, trigger_selectors)

        if clicked and (menu_open or _wait_for_account_menu(page)):
            remaining = max(0.5, deadline - time.monotonic())
            credit = _scan_menu_for_credit(page, timeout_seconds=min(8.0, remaining))
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            if credit is not None:
                return credit
            # 点击没报错但没读到积分数字：可能是点了没反应（hydration 延迟），
            # 整轮（点击+读取）重来，而不是继续死等这一次的对话框冒出数字。

        time.sleep(0.5)

    log("⚠️ 多次尝试点开账号头像菜单/读取积分均未成功，判定探测失败（不做整页兜底扫描，避免误读到定价宣传文案里的数字）", "积分探针")
    return None


def _scan_menu_for_credit(page, timeout_seconds: float = 8.0, poll_interval: float = 0.4) -> Optional[int]:
    """只在头像菜单已点开的前提下调用：轮询 ui_selectors.py 里 credit_display
    列出的具体元素选择器，不做整页文字兜底扫描。

    2026-07-23 实测踩坑（"检测不准"反馈的真因）：账号对话框打开是同步的，但
    积分数字是异步加载的——先短暂显示 i18n 的 "Loading…" 占位文案，过一会才
    替换成真实数字，耗时随网络情况波动（同一账号有时 1 秒内就好，有时要
    好几秒）。固定 sleep 一次就去读，读到的经常还是占位文案，被误判成"没有
    数字"而沿用旧缓存值。这里改成轮询到超时为止，而不是只读一次。"""
    selectors = UI_SELECTORS.get("google_fx", {}).get("credit_display", [])
    surfaces = UI_SELECTORS.get("google_fx", {}).get("account_menu_surface", [])
    deadline = time.monotonic() + timeout_seconds
    candidate = None
    stable_reads = 0

    while True:
        if _browser_session_is_closed(page):
            raise BrowserSessionClosedError(
                "AdsPower 浏览器或 Flow 标签页已关闭，积分探测已立即停止")
        observed = None
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=500):
                    continue
                text = locator.inner_text(timeout=1500)
                observed = _extract_credit_number(text)
                if observed is not None:
                    break
            except Exception:
                continue

        # 语义链接改版失效时，只扫描已经确认打开的账号弹层，不扫描 body。
        if observed is None:
            for selector in surfaces:
                try:
                    locator = page.locator(selector).last
                    if locator.count() == 0 or not locator.is_visible(timeout=500):
                        continue
                    observed = _extract_credit_number(locator.inner_text(timeout=1500))
                    if observed is not None:
                        break
                except Exception:
                    continue

        if observed is not None:
            if observed == candidate:
                stable_reads += 1
            else:
                candidate, stable_reads = observed, 1
            # 连续两轮读到同一个数再接受，避免异步更新期间读取到旧 DOM。
            if stable_reads >= 2:
                return observed
        else:
            candidate, stable_reads = None, 0

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)
