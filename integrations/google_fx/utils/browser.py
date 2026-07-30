# -*- coding: utf-8 -*-
"""
🛠️ 浏览器通用工具函数
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AdsPower 连接、Playwright 页面管理、下载、粘贴等通用功能。
"""

import os
import time
import random
import base64
import requests
import socket
from urllib.parse import urlparse
from pathlib import Path

from ..config import (
    PAGE_LOAD_TIMEOUT,
    DEFAULT_USER_ID,
    DEFAULT_PORT,
    OUTPUT_DIR,
    get_runtime_default_user_id,
    get_runtime_default_port,
)
from . import account_binding
from .logger import log


class BrowserSessionClosedError(RuntimeError):
    """The Playwright page/CDP browser was closed while an operation was waiting."""


def random_sleep(min_s=1.0, max_s=3.0):
    time.sleep(random.uniform(min_s, max_s))


def clean_path(path_str):
    """🛠️ 路径兼容性处理，必要时按 basename 在输出目录内自动纠偏。"""
    if not path_str:
        return ""
    path_str = path_str.strip().strip('"').strip("'")
    normalized = os.path.normpath(os.path.expanduser(path_str))

    if os.path.exists(normalized):
        return normalized

    basename = os.path.basename(normalized)
    if not basename:
        return normalized

    try:
        search_root = Path(OUTPUT_DIR)
        if search_root.exists():
            matches = [str(p) for p in search_root.rglob(basename) if p.is_file()]
            if len(matches) == 1:
                log(f"⚠️ 路径不存在，已按文件名自动纠偏: '{normalized}' -> '{matches[0]}'", "路径修复")
                return matches[0]
            if len(matches) > 1:
                # 🚨 纠偏必须锁定到同一个项目目录。每个项目的帧都叫 img_001.webp，
                # 只按 basename（甚至只按「上一级目录叫 frames」）去挑，等于在几十个
                # 项目里随手抓一张同名图——视频链会把**别的项目的帧**当成本段的首尾
                # 锚点传上去，生成一段跟本片毫无关系的片段（下载后靠锚点 MAD 才被
                # 拒收，重试还会再抓同一张，永远修不好）。
                # 所以这里只认「项目目录/子目录/文件名」三段尾巴完全一致的候选；
                # 认不出来就如实返回原路径（= 文件缺失），交给上游的缺帧闸门处理。
                tail = os.path.join(
                    os.path.basename(os.path.dirname(os.path.dirname(normalized))),
                    os.path.basename(os.path.dirname(normalized)),
                    basename,
                )
                preferred = [m for m in matches if m.endswith(os.sep + tail)] if os.sep in tail else []
                if len(preferred) == 1:
                    log(f"⚠️ 路径不存在，已按项目内同名文件纠偏: '{normalized}' -> '{preferred[0]}'", "路径修复")
                    return preferred[0]
                log(
                    f"⚠️ 路径不存在，且 {len(matches)} 个同名文件分属不同项目，"
                    f"无法安全纠偏（拒绝随手挑一张，避免张冠李戴）: '{normalized}'",
                    "路径修复",
                )
                return normalized
    except Exception as e:
        log(f"⚠️ 路径自动纠偏失败: {e}", "路径修复")

    return normalized


def fast_paste(page, selector, text):
    """ ⚡ 极速粘贴函数 (模拟物理粘贴效果) """
    try:
        page.wait_for_selector(selector, state="visible", timeout=10000)

        # 核心逻辑：直接通过 JS 注入内容并触发必要的 input/change/paste 事件
        page.evaluate("""({selector, text}) => {
            const el = document.querySelector(selector);
            if (el) {
                el.focus();

                // 1. 设置内容
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                    el.value = text;
                } else {
                    el.innerText = text;
                }

                // 2. 模拟物理粘贴事件 (解决某些站点的监听问题)
                const dataTransfer = new DataTransfer();
                dataTransfer.setData('text/plain', text);
                const pasteEvent = new ClipboardEvent('paste', {
                    clipboardData: dataTransfer,
                    bubbles: true,
                    cancelable: true
                });
                el.dispatchEvent(pasteEvent);

                // 3. 触发常规输入事件
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));

                // 4. 针对内容可编辑区域的特殊处理
                if (el.getAttribute('contenteditable') === 'true') {
                    const range = document.createRange();
                    const sel = window.getSelection();
                    range.selectNodeContents(el);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }
            }
        }""", {"selector": selector, "text": text})

        # 即使 JS 成功，也补一个 Playwright 的 fill 确保状态机完全同步
        try: page.fill(selector, text, timeout=2000)
        except: pass

        # ⚡ 状态唤醒：针对某些前端框架 (React/Angular) 无法捕捉瞬间变更的问题
        # 增加一个物理按键动作（空格 + 退格），强制唤醒 UI 的数据绑定
        random_sleep(0.5, 1.0)
        try:
            target = page.locator(selector).first
            target.focus()
            target.press("Space")
            random_sleep(0.1, 0.3)
            target.press("Backspace")
            random_sleep(1, 2)
        except:
            pass

        log(f"⚡ 物理粘贴及UI唤醒成功: {len(text)} 个字符", "输入优化")

    except Exception as e:
        log(f"❌ Input Error: {e}", "Error")
        try: page.fill(selector, text)
        except: pass


def _is_ws_port_open(ws_url: str) -> bool:
    """🛠️ 检测 WebSocket URL 对应的端口是否已启用并且可以建立 TCP 连接。"""
    try:
        parsed = urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        if not port:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
            return True
    except Exception:
        return False


def get_ads_ws_url(user_id=None, port=None, auto_rotate_proxy=True,
                   max_start_attempts=3, start_timeout=45):
    """🔌 连接 AdsPower 并返回 WebSocket URL (替代 4 处重复代码)

    新增 auto_rotate_proxy: True 时，在启动浏览器前自动更换 Miya IP 代理。
    设为 False 可跳过轮换（如已刚轮换过，避免重复操作）。

    max_start_attempts / start_timeout：给短命的旁路动作（积分探针等）压缩启动重试
    预算用。默认值是生成任务的老行为（3 次 ×45s）；探针拿这个默认值最坏能在
    AdsPower 起不来时把浏览器占住两分半，把别的账号的"刷新积分"一起堵死。
    """
    # 账号解析走统一优先级（显式传参 > per-task 换号绑定 > 队列定向绑定 > 进程默认值），
    # 见 utils/account_binding。换号不再改 os.environ，所以这里必须问 account_binding，
    # 否则换号结果对本次连接不生效。
    user_id = account_binding.resolve_account(
        explicit=user_id,
        fallback=get_runtime_default_user_id() or DEFAULT_USER_ID,
    )
    if port is None:
        port = get_runtime_default_port() or DEFAULT_PORT

    # ── 代理轮换钩子 ──
    if auto_rotate_proxy:
        try:
            from .proxy_rotator import ProxyRotator
            rotator = ProxyRotator()
            if rotator.is_configured and rotator.auto_rotate:
                rotator.rotate_proxy(user_id=user_id, port=port)
        except Exception as e:
            log(f"⚠️ 代理轮换异常（不阻塞主流程）: {type(e).__name__}: {e}", "代理轮换")

    launch_args = '%5B%22--disable-features%3DHardwareMediaKeyHandling%22%2C%22--mute-audio%22%5D'
    api_start_url = f"http://127.0.0.1:{port}/api/v1/browser/start?user_id={user_id}&launch_args={launch_args}"
    api_stop_url = f"http://127.0.0.1:{port}/api/v1/browser/stop?user_id={user_id}"

    max_start_attempts = max(1, int(max_start_attempts))
    for attempt in range(1, max_start_attempts + 1):
        try:
            from . import cancel_flag
            if cancel_flag.is_cancelled:
                log("🛑 任务已取消，中断浏览器启动重试", "浏览器启动")
                raise RuntimeError("任务已被取消")
        except RuntimeError:
            raise
        except Exception:
            pass

        try:
            resp = requests.get(api_start_url, timeout=start_timeout).json()
            if resp["code"] != 0:
                raise Exception(resp.get("msg", "未知错误"))

            ws_url = resp["data"]["ws"]["puppeteer"]

            # 校验端口是否可用
            if _is_ws_port_open(ws_url):
                if attempt > 1:
                    log(f"✅ 第 {attempt} 次尝试成功启动浏览器，端口已启用: {ws_url}", "浏览器启动")
                return ws_url

            log(f"⚠️ AdsPower 返回的 WebSocket 端口未启用 (尝试 {attempt}/{max_start_attempts}): {ws_url}，尝试强制关闭并重启...", "浏览器启动")
        except Exception as start_err:
            log(f"⚠️ 启动浏览器失败 (尝试 {attempt}/{max_start_attempts}): {start_err}", "浏览器启动")
            if attempt == max_start_attempts:
                raise Exception(
                    f"AdsPower 启动失败 (已尝试 {max_start_attempts} 次): {start_err} (user_id={user_id}, port={port})"
                )

        # 强制关闭并等待
        try:
            requests.get(api_stop_url, timeout=10)
        except Exception:
            pass
        time.sleep(2)

    raise Exception(
        f"AdsPower 启动失败: 无法获取有效的 WebSocket 调试端口 (user_id={user_id}, port={port})"
    )


def find_or_create_page(context, url_pattern, fallback_url=None):
    """🔍 查找/复用标签页，清理多余页面，确保浏览器标签栏始终保持单个 Flow 窗口/标签页。"""
    if not fallback_url and ("labs.google" in url_pattern or "/fx" in url_pattern):
        fallback_url = "https://labs.google/fx/tools/flow"

    pages = list(getattr(context, "pages", []))
    target_page = None

    # 1. 优先查找 URL 已经匹配 url_pattern、labs.google 或 /fx/tools/flow 的已打开 Flow 标签页
    for pg in reversed(pages):
        try:
            url = str(getattr(pg, "url", ""))
            if url_pattern in url or "labs.google" in url or "/fx/tools/flow" in url:
                target_page = pg
                break
        except Exception:
            continue

    # 2. 如果没有匹配到 Flow 页面，优先复用已有空白页/其它标签页，避免调用 context.new_page() 产生多余标签页
    if not target_page:
        for pg in reversed(pages):
            try:
                target_page = pg
                break
            except Exception:
                continue

    # 3. 如果 context 里连一个标签页都没有，才新建页面
    if not target_page:
        try:
            target_page = context.new_page()
        except Exception as e:
            log(f"⚠️ 创建新标签页失败: {e}", "浏览器管理")
            raise

    # 4. 如果目标页面的 URL 不符合预期且指定了 fallback_url，进行跳转
    if fallback_url:
        try:
            curr_url = str(getattr(target_page, "url", ""))
            if url_pattern not in curr_url:
                target_page.goto(fallback_url, timeout=PAGE_LOAD_TIMEOUT)
                random_sleep(1, 2)
        except Exception as e:
            log(f"⚠️ 标签页跳转 fallback_url ({fallback_url}) 异常: {e}", "浏览器管理")

    # 5. 【核心修复】：清理并关闭除 target_page 外的所有多余标签页，保证浏览器标签栏始终保持一个 Flow 窗口
    remaining_pages = list(getattr(context, "pages", []))
    for pg in remaining_pages:
        if pg != target_page:
            try:
                pg.close()
            except Exception:
                pass

    try:
        target_page.bring_to_front()
    except Exception:
        pass

    return target_page



def _any_visible(page, selectors) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                if locator.nth(index).is_visible(timeout=500):
                    return True
        except Exception:
            continue
    return False


def _flow_workspace_ready(page) -> bool:
    """落地页也预埋了隐藏 textarea，所以只认“可见”的工作台控件。"""
    if flow_onboarding_required(page):
        return False
    return _any_visible(page, [
        "textarea",
        "[contenteditable='true']",
        "button:has-text('New project')",
        "button:has-text('新建项目')",
        "button:has(i.google-symbols:text('add_2'))",
    ])


def _flow_onboarding_kind(page):
    """返回 preferences/privacy；以中英为主，并覆盖当前实测印尼语。"""
    markers = {
        "preferences": (
            "use and shape ai tools for creativity",
            "gunakan dan bentuk alat ai untuk kreativitas",
            "使用和塑造创意 ai 工具",
            "使用并塑造创意 ai 工具",
        ),
        "privacy": (
            "review our privacy notice",
            "tinjau kebijakan privasi kami",
            "查看我们的隐私",
            "审阅我们的隐私",
        ),
    }
    try:
        dialogs = page.locator("[role='dialog']")
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if not dialog.is_visible(timeout=500):
                continue
            text = (dialog.inner_text(timeout=1000) or "").lower()
            for kind, needles in markers.items():
                if any(marker in text for marker in needles):
                    return kind
    except Exception:
        pass
    return None


def flow_onboarding_required(page) -> bool:
    return _flow_onboarding_kind(page) is not None


def _click_visible_dialog_button(dialog, selectors) -> bool:
    for selector in selectors:
        try:
            locator = dialog.locator(selector)
            for index in range(locator.count()):
                button = locator.nth(index)
                if button.is_visible(timeout=500) and button.is_enabled():
                    button.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False


def complete_flow_onboarding(page, timeout_seconds: float = 60.0) -> bool:
    """完成首次使用引导；营销邮件/研究邀请保持默认关闭。

    用户已明确授权把这些首次进入卡点纳入自动处理。偏好页只点“下一步”，不碰
    两个可选订阅；隐私页先滚动正文到底，再点“继续”。文案以中英为主，并兼容
    当前 k108nye6 实测的印尼语界面。
    """
    try:
        from ..ui_selectors import UI_SELECTORS
        fx = UI_SELECTORS.get("google_fx", {})
    except Exception:
        fx = {}
    next_selectors = fx.get("flow_onboarding_next_btn", ["button:has-text('Next')"])
    continue_selectors = fx.get("flow_onboarding_continue_btn", ["button:has-text('Continue')"])
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))

    while time.monotonic() < deadline:
        kind = _flow_onboarding_kind(page)
        if kind is None:
            return True
        dialogs = page.locator("[role='dialog']")
        target = None
        for index in range(dialogs.count()):
            candidate = dialogs.nth(index)
            if candidate.is_visible(timeout=500):
                target = candidate
                break
        if target is None:
            time.sleep(0.3)
            continue

        if kind == "preferences":
            log("🧭 Flow 首次使用偏好页：保持营销邮件/研究邀请关闭，点击下一步", "Flow导航")
            if not _click_visible_dialog_button(target, next_selectors):
                time.sleep(0.3)
                continue
        elif kind == "privacy":
            log("🧭 Flow 隐私说明页：滚动正文到底并点击继续", "Flow导航")
            try:
                target.evaluate("""dialog => {
                    const rows = Array.from(dialog.querySelectorAll('*'));
                    const scrollable = rows
                      .filter(el => el.scrollHeight > el.clientHeight + 20)
                      .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
                    if (scrollable) {
                      scrollable.scrollTop = scrollable.scrollHeight;
                      scrollable.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }
                }""")
            except Exception:
                pass
            time.sleep(0.4)
            if not _click_visible_dialog_button(target, continue_selectors):
                time.sleep(0.3)
                continue
        time.sleep(0.5)

    return _flow_onboarding_kind(page) is None


def is_google_login_page(page) -> bool:
    """检测当前页面是否为 Google 登录页面 / 账号选择页面 / 登录重定向。"""
    try:
        url = str(getattr(page, "url", "") or "").lower()
        if any(domain in url for domain in [
            "accounts.google.com",
            "accounts.youtube.com",
            "signin/accountchooser",
            "servicelogin",
            "signin/identifier",
            "signin/v2",
        ]):
            return True

        state = page.evaluate(r"""() => {
            const url = window.location.href.toLowerCase();
            if (url.includes('accounts.google.com') || url.includes('signin/accountchooser')) {
                return true;
            }
            const text = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const markers = [
                'choose an account',
                'sign in with google',
                'sign in to continue',
                'use another account',
                '选择账号',
                '登录以继续',
                '使用其他账号',
                '登录 google',
            ];
            if (markers.some(m => text.includes(m))) {
                return true;
            }
            if (document.querySelector('input[type="email"], input[name="identifier"], #identifierId')) {
                const title = (document.title || '').toLowerCase();
                if (title.includes('sign in') || title.includes('登录')) {
                    return true;
                }
            }
            return false;
        }""")
        return bool(state)
    except Exception:
        return False


def _browser_session_is_closed(page) -> bool:
    """Return True when either the page or its CDP browser is definitively gone.

    Playwright's locator/evaluate calls raise TargetClosedError after AdsPower is
    closed. Several workspace probes intentionally swallow selector errors, so
    without this explicit check a dead page looks identical to a page whose UI
    has not loaded and waits until the full navigation timeout.
    """
    page_closed = getattr(page, "is_closed", None)
    if callable(page_closed):
        try:
            if page_closed():
                return True
        except Exception:
            return True

    context = getattr(page, "context", None)
    browser = getattr(context, "browser", None) if context is not None else None
    is_connected = getattr(browser, "is_connected", None)
    if callable(is_connected):
        try:
            if not is_connected():
                return True
        except Exception:
            return True
    return False


def attempt_auto_login(page, user_id=None, context_label="Flow导航", cancel_check=None):
    """掉登录时先试一次自动登录，成功返回 True。

    薄封装，存在的理由是"惰性 import + 异常隔离"这两件事在三个调用点重复：
    - 惰性 import：utils.auto_login 反过来要 import 本模块的 is_google_login_page，
      模块级互相 import 会成环。
    - 异常隔离：自动登录是**附加**能力，它自己炸掉绝不能把调用方的主流程带崩——
      调用方原本的行为（退回等人工）必须原样保留。

    没配凭据 / 熔断中 / 登录失败一律返回 False，调用方照旧走既有的人工处理路径。
    """
    try:
        from . import auto_login as _auto_login
    except Exception as e:
        log(f"⚠️ 自动登录模块不可用（{type(e).__name__}: {e}），按需人工处理", context_label)
        return False
    try:
        if not _auto_login.auto_login_available(user_id):
            return False
        return bool(_auto_login.try_auto_login(
            page, user_id=user_id, cancel_check=cancel_check,
            context_label=context_label))
    except Exception as e:
        log(f"⚠️ 自动登录过程异常（{type(e).__name__}: {e}），退回等待人工处理", context_label)
        return False


def ensure_flow_workspace(page, timeout_seconds: float = 30.0, user_id=None) -> bool:
    """若账号停在 Flow 产品介绍页，点击首屏 CTA 进入真正工作台。

    页面同时渲染首屏和页尾两个同文案按钮；不能盲点 `.first`。这里选择纵坐标
    最小的可见按钮（即用户截图红框处），点击后必须看到可见输入框或“新建项目”
    才算成功。已经在工作台时为幂等空操作。

    user_id：撞上登录页时用它去取凭据自动重登。**积分探针这类显式指定账号的
    调用方必须传**——省略时按 account_binding 解析当前上下文，而探针并不绑定
    上下文账号，会拿到进程默认账号的凭据去登另一个号。
    """
    try:
        from ..ui_selectors import UI_SELECTORS
        selectors = UI_SELECTORS.get("google_fx", {}).get("flow_entry_btn", [])
    except Exception:
        selectors = ["button:has-text('Create with Google Flow')"]

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    clicked = False
    auto_login_tried = False
    while time.monotonic() < deadline:
        if _browser_session_is_closed(page):
            raise BrowserSessionClosedError(
                "AdsPower 浏览器或 Flow 标签页已关闭，积分探测已立即停止")
        if is_google_login_page(page):
            # 每次 ensure_flow_workspace 只自动登录一次：失败了在同一个循环里
            # 反复重试等于绕开 auto_login 内部那套"最多提交一次密码"的护栏。
            if not auto_login_tried:
                auto_login_tried = True
                if attempt_auto_login(page, user_id=user_id, context_label="Flow导航"):
                    # 登录成功后页面通常已经跳回 Flow，但可能落在产品介绍页，
                    # 所以不直接 return True，交给下一轮循环照常判定工作台。
                    continue
            log("🔒 检测到 Google 登录页面，无法自动进入工作台，提示人工处理登录", "Flow导航")
            return False
        if flow_onboarding_required(page):
            if not complete_flow_onboarding(page, timeout_seconds=max(1.0, deadline - time.monotonic())):
                log("⚠️ Flow 首次使用引导未能自动完成", "Flow导航")
                return False
            continue
        if _flow_workspace_ready(page):
            if clicked:
                log("✅ 已从产品介绍页进入 Google Flow 工作台", "Flow导航")
            return True
        if not clicked:
            candidates = []
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    for index in range(locator.count()):
                        item = locator.nth(index)
                        if not item.is_visible(timeout=500) or not item.is_enabled():
                            continue
                        try:
                            box = item.bounding_box()
                            top = float(box.get("y", 10**9)) if box else 10**9
                        except Exception:
                            top = 10**9
                        candidates.append((top, index, item))
                except Exception:
                    continue
            if candidates:
                _, _, entry = min(candidates, key=lambda row: (row[0], row[1]))
                log("🚪 检测到 Google Flow 产品介绍页，点击首屏 Create with Google Flow 进入工作台", "Flow导航")
                try:
                    entry.click(timeout=5000)
                    clicked = True
                except Exception as e:
                    log(f"⚠️ 点击 Flow 工作台入口失败: {type(e).__name__}: {e}", "Flow导航")
                    return False
        time.sleep(0.4)
    log("⚠️ Flow 页面未在时限内出现工作台控件或可点击入口", "Flow导航")
    return False


def download_video_via_browser(page, video_url, output_dir, prefix="video"):
    """🎬 通过浏览器 fetch + base64 下载视频 (替代 2 处重复代码)"""
    log(f"🎬 同步下载视频到本地...", prefix)
    b64_data = page.evaluate("""async (url) => {
        const response = await fetch(url);
        const blob = await response.blob();
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result.split(',')[1]);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    }""", video_url)
    video_bytes = base64.b64decode(b64_data)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    filename = f"{prefix}_{int(time.time())}.mp4"
    local_path = os.path.join(output_dir, filename)
    with open(local_path, "wb") as f:
        f.write(video_bytes)
    log(f"✅ 视频已保存至: {local_path}", "下载")
    return local_path


def close_ads_browser(user_id=None, port=None) -> tuple[bool, str]:
    """🛑 关闭 AdsPower 浏览器并释放 profile 锁，返回 (是否成功, 提示消息)"""
    user_id = account_binding.resolve_account(
        explicit=user_id,
        fallback=get_runtime_default_user_id() or DEFAULT_USER_ID,
    )
    if port is None:
        port = get_runtime_default_port() or DEFAULT_PORT
    url = f"http://127.0.0.1:{port}/api/v1/browser/stop?user_id={user_id}"
    log(f"🛑 正在关闭 AdsPower 浏览器: user_id={user_id}, port={port}", "浏览器关闭")
    try:
        resp = requests.get(url, timeout=10)
        log(f"🛑 浏览器关闭 API 返回: {resp.text}", "浏览器关闭")
        if resp.status_code == 200:
            try:
                data = resp.json()
                if data.get("code") == 0:
                    return True, data.get("msg") or "已成功发送关闭浏览器指令"
                else:
                    return False, data.get("msg") or f"AdsPower 返回错误: {data.get('code')}"
            except Exception:
                return True, "已发送关闭浏览器指令"
        return False, f"AdsPower 响应异常状态码 {resp.status_code}"
    except Exception as e:
        log(f"🛑 关闭浏览器失败: {str(e)}", "浏览器关闭")
        return False, f"关闭浏览器请求失败: {str(e)}"


def stop_ads_browser(user_id=None, port=None) -> bool:
    """🛑 关闭 AdsPower 浏览器并释放 profile 锁（兼容布尔值返回）"""
    ok, _ = close_ads_browser(user_id=user_id, port=port)
    return ok
