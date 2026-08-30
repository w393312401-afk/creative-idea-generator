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
    IS_MAC,
    PAGE_LOAD_TIMEOUT,
    DEFAULT_USER_ID,
    DEFAULT_PORT,
    OUTPUT_DIR,
    get_runtime_default_user_id,
    get_runtime_default_port,
    get_runtime_adspower_silent_mode,
    get_runtime_adspower_window_position,
    get_runtime_adspower_window_size,
    get_runtime_adspower_headless,
    get_runtime_adspower_macos_window_mode,
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


def _macos_frontmost_app() -> str:
    """当前前台应用名；非 macOS 或取不到时返回空串。"""
    if not IS_MAC:
        return ""
    try:
        from .macos_window import frontmost_app
        return frontmost_app()
    except Exception:
        return ""


def suppress_browser_window(ws_url: str, previous_app: str = "", hide: bool = True) -> bool:
    """让刚启动的 AdsPower 浏览器别占着最前端（macOS 专用，其它平台 no-op）。

    hide=True 时隐藏浏览器 app 再归还焦点；hide=False 只归还焦点。返回是否真的
    隐藏成功。任何异常都吞掉——窗口没藏住只是碍眼，不该让生成任务挂掉。
    """
    if not IS_MAC:
        return False
    try:
        from .macos_window import hide_browser, restore_focus
        if hide:
            return hide_browser(ws_url, previous_app)
        restore_focus(previous_app)
        return False
    except Exception as e:
        log(f"⚠️ 抑制浏览器窗口时异常（不影响任务）: {type(e).__name__}: {e}", "浏览器启动")
        return False


def reveal_browser_window(ws_url: str) -> bool:
    """把隐藏起来的浏览器窗口显示并切到最前（macOS 专用，其它平台 no-op）。

    给"等待人工处理登录/验证码"用：那种时候抢焦点正是我们想要的，人得看见窗口。
    """
    if not IS_MAC:
        return False
    try:
        from .macos_window import show_browser
        return show_browser(ws_url)
    except Exception as e:
        log(f"⚠️ 恢复浏览器窗口时异常: {type(e).__name__}: {e}", "浏览器启动")
        return False


def reveal_hidden_browser_windows() -> int:
    """把所有被静默隐藏的浏览器窗口翻到最前（macOS 专用，其它平台返回 0）。

    人工接管链路只拿得到 Playwright 的 page，取不到 CDP 端口，所以按"本进程隐藏过
    谁"来恢复，而不是按某个具体 ws_url。
    """
    if not IS_MAC:
        return 0
    try:
        from .macos_window import reveal_hidden
        return reveal_hidden()
    except Exception as e:
        log(f"⚠️ 恢复浏览器窗口时异常: {type(e).__name__}: {e}", "浏览器启动")
        return 0


def rehide_browser_windows(previous_app: str = "") -> int:
    """人工处理结束后把翻出来的窗口重新藏回去（macOS 专用，其它平台返回 0）。"""
    if not IS_MAC:
        return 0
    try:
        from .macos_window import rehide_revealed
        return rehide_revealed(previous_app)
    except Exception as e:
        log(f"⚠️ 重新隐藏浏览器窗口时异常: {type(e).__name__}: {e}", "浏览器启动")
        return 0


def build_adspower_launch_args(silent: bool = True) -> tuple[str, list[str]]:
    """🛠️ 构造 AdsPower 启动所需的 Chromium 参数列表与 URL 编码串。

    静默后台模式（silent=True）：
    - --window-position=-10000,-10000：把窗口生成在屏幕可见范围之外。
      ⚠️ 仅 Windows 有效。macOS 的窗口服务器会把屏幕外坐标 clamp 回可见区域，而且
      Chromium 启动时会 activate 自己，所以光靠这个参数在 Mac 上根本挡不住抢焦点。
      参数保留是为了跨平台一致（在 Mac 上无害），Mac 侧真正干活的是
      utils/macos_window 的 app 级隐藏，由 get_ads_ws_url 在启动成功后调用。
    - --window-size=1280,800：确保标准化渲染视口，不影响 DOM / WebGL / Canvas
    - --disable-features=CalculateNativeWinOcclusion,HardwareMediaKeyHandling：
      禁用 Windows 窗口遮挡检测，防止窗口移出屏幕后 Chrome 自动挂起或降低帧率
    - --disable-backgrounding-occluded-windows：禁止后台/遮挡窗口休眠
    - --disable-renderer-backgrounding：禁止渲染进程在后台降低优先级
    - --mute-audio：静音
    - --no-first-run：跳过首次运行弹窗
    """
    import json
    import urllib.parse

    chrome_args = [
        "--disable-features=CalculateNativeWinOcclusion,HardwareMediaKeyHandling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--mute-audio",
        "--no-first-run",
    ]
    if silent:
        pos = get_runtime_adspower_window_position()
        size = get_runtime_adspower_window_size()
        chrome_args.extend([
            f"--window-position={pos}",
            f"--window-size={size}",
        ])

    encoded = urllib.parse.quote(json.dumps(chrome_args))
    return encoded, chrome_args


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

    silent_mode = get_runtime_adspower_silent_mode()
    launch_args, chrome_args = build_adspower_launch_args(silent=silent_mode)
    api_start_url = f"http://127.0.0.1:{port}/api/v1/browser/start?user_id={user_id}&launch_args={launch_args}"
    if get_runtime_adspower_headless():
        api_start_url += "&headless=1"
    api_stop_url = f"http://127.0.0.1:{port}/api/v1/browser/stop?user_id={user_id}"

    # macOS 上屏幕外坐标无效，得在启动后用 app 级隐藏收拾窗口；先记下当前前台应用，
    # 隐藏完把焦点还回去，用户正在打的字才不会被打断。
    mac_window_mode = get_runtime_adspower_macos_window_mode() if IS_MAC else "off"
    mac_suppress = (
        silent_mode
        and mac_window_mode != "off"
        and not get_runtime_adspower_headless()  # headless 压根没有窗口，不用管
    )
    previous_app = _macos_frontmost_app() if mac_suppress else ""

    if silent_mode:
        if IS_MAC:
            log(f"🤫 AdsPower 静默后台启动中 (macOS 窗口模式: {mac_window_mode})", "浏览器启动")
        else:
            log(f"🤫 AdsPower 静默后台启动中 (屏幕外坐标: {get_runtime_adspower_window_position()})", "浏览器启动")

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
                if mac_suppress:
                    suppress_browser_window(ws_url, previous_app,
                                            hide=(mac_window_mode == "hide"))
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


_PAGE_ALIVE_PROBE_TIMEOUT_MS = 2000


def _page_is_alive(page, timeout_ms=_PAGE_ALIVE_PROBE_TIMEOUT_MS) -> bool:
    """快速探活：能在 timeout_ms 内于页面上下文里跑通一次求值即视为可用。

    ⚠️ 必须用 wait_for_function 而不是 evaluate：sync 版 `Page.evaluate()` 的签名是
    `evaluate(expression, arg=None)`，**没有 timeout 参数**（只有 `Locator.evaluate()`
    有）。`page.evaluate("1", timeout=2000)` 会抛 TypeError，被本函数的 except 吞掉，
    于是任何页面——包括完全健康的——都被判成"已死"。那正是 2026-08-02 之前的行为：
    _connect_fx_page 每次都据此新建标签页并重新导航，标签页逐次累积，
    _recover_valid_page 也永远走不到"复用已有活页"分支。
    `Page.wait_for_function(expression, *, timeout=...)` 才是带真超时的等价物。
    """
    try:
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return False
        page.wait_for_function("1", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _is_manageable_user_page(page) -> bool:
    """判断一个 page 是否为可操作/可关闭的普通用户标签页。
    Chromium 内部页面（chrome://, chrome-extension://, devtools://, edge://, brave:// 等）
    不可作为工作台标签页复用，调用 pg.close() 还会因 CDP Target.closeTarget 阻塞/挂死。
    """
    if page is None:
        return False
    try:
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return False
        url = str(getattr(page, "url", "") or "").strip().lower()
        if not url or url.startswith("about:blank"):
            return True
        for scheme in ("chrome://", "chrome-extension://", "devtools://", "edge://", "brave://", "view-source:"):
            if url.startswith(scheme):
                return False
        return True
    except Exception:
        return False


def _recover_valid_page(context, broken_page):
    """从 context.pages 中找到一个可用的 page 对象；都不行就新建。

    detached frame / target closed 后原 page 对象可能处于"未关闭但不可用"
    的状态（is_closed() 返回 False，但所有 locator / evaluate 调用都会
    超时挂死）。本函数用 _page_is_alive 做快速探活，筛掉这些僵尸页面。
    """
    candidates = [
        p for p in getattr(context, "pages", [])
        if _is_manageable_user_page(p) and not (callable(getattr(p, "is_closed", None)) and p.is_closed())
    ]
    # 优先选不是 broken_page 的其他活页
    for pg in reversed(candidates):
        if pg is broken_page:
            continue
        if _page_is_alive(pg):
            return pg
    # 其次看 broken_page 自己是否其实还活着
    if broken_page in candidates and _page_is_alive(broken_page):
        return broken_page
    # 全部不可用，新建
    try:
        return context.new_page()
    except Exception as e:
        log(f"⚠️ 恢复页面时新建标签页也失败: {e}", "浏览器管理")
        raise


def find_or_create_page(context, url_pattern, fallback_url=None, *, user_id=None,
                        auto_login=True, auto_login_timeout_seconds=None,
                        cancel_check=None, context_label="Google FX 浏览器启动"):
    """🔍 获取 Google FX 标签页，并在落到登录页时立即尝试自动登录。

    这是生图、视频、积分探针和诊断服务共用的浏览器入口。把自动登录放在这里，
    可以保证服务刚打开/接管 AdsPower 浏览器就处理掉登录，而不是等到后续找不到
    Flow 控件后才进入人工接管分支。

    自动登录失败不会在这里抛错；页面保持原状交给调用方原有的人工处理逻辑。
    user_id 对积分探针、诊断等非任务绑定调用必须显式传入，防止拿默认账号凭据
    登录另一个 AdsPower profile。
    """
    if not fallback_url and ("labs.google" in url_pattern or "/fx" in url_pattern):
        fallback_url = "https://labs.google/fx/tools/flow"

    pages = [p for p in getattr(context, "pages", []) if _is_manageable_user_page(p)]
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
                try:
                    target_page.goto(fallback_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
                except Exception as goto_err:
                    err_text = str(goto_err)
                    # 如果因页面重定向或 frame 替换导致 "Frame has been detached" 或 "Target closed"
                    if "detached" in err_text.lower() or "target closed" in err_text.lower():
                        # 不论 is_closed 状态如何，都尝试恢复一个可用页面
                        # （detached frame != closed：page 对象可能仍 open 但 frame 已不可用，
                        #   导致后续所有 locator/evaluate 操作静默挂死）
                        target_page = _recover_valid_page(context, target_page)
                        curr_url = str(getattr(target_page, "url", ""))
                        if url_pattern in curr_url or "labs.google" in curr_url or "accounts.google" in curr_url:
                            log(f"ℹ️ 标签页跳转 fallback_url 触发重定向/Frame解绑，恢复后页面已就绪: {curr_url}", "浏览器管理")
                        else:
                            # 恢复后的页面 URL 仍不对，重新导航一次
                            log("⚠️ 标签页跳转 fallback_url 触发 Frame 解绑，尝试恢复页面后重新导航", "浏览器管理")
                            try:
                                target_page.goto(fallback_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
                            except Exception as retry_err:
                                log(f"⚠️ 恢复页面后重新导航仍失败: {type(retry_err).__name__}: {retry_err}", "浏览器管理")
                    else:
                        raise goto_err
                random_sleep(1, 2)
        except Exception as e:
            err_str = str(e).lower()
            if "detached" in err_str or "target closed" in err_str:
                # 外层也可能因为 getattr(target_page, "url") 等操作在 detached page 上抛异常
                target_page = _recover_valid_page(context, target_page)
                log(f"⚠️ 标签页跳转 fallback_url ({fallback_url}) 异常（已恢复页面）: {e}", "浏览器管理")
            else:
                log(f"⚠️ 标签页跳转 fallback_url ({fallback_url}) 异常: {e}", "浏览器管理")

    # 5. 【核心修复】：清理并关闭除 target_page 外的所有多余用户标签页，保证浏览器标签栏始终保持一个 Flow 窗口
    # 注意：绝不关闭 chrome:// 等内部页面，否则会导致 CDP 挂起死锁
    remaining_pages = [p for p in getattr(context, "pages", []) if _is_manageable_user_page(p)]
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

    # fallback 导航可能被 Google 重定向到 accounts.google.com。此处统一前置
    # 自愈；ensure_flow_workspace / wait_out_manual_intervention 仍保留二次兜底，
    # 用于登录重定向在本函数返回后才发生的慢页面。
    if auto_login and (is_google_login_page(target_page) or wait_for_login_redirect(target_page, timeout_seconds=2.0)):
        attempt_auto_login(
            target_page,
            user_id=user_id,
            context_label=context_label,
            cancel_check=cancel_check,
            timeout_seconds=auto_login_timeout_seconds,
        )

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
            "signin/challenge",
            "signin/v2",
        ]):
            return True

        state = page.evaluate(r"""() => {
            const url = window.location.href.toLowerCase();
            if (url.includes('accounts.google.com') || url.includes('signin/accountchooser') || url.includes('signin/challenge')) {
                return true;
            }
            const text = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const providerForm = document.querySelector(
                "form[action*='/fx/api/auth/signin/google'], form[action*='/api/auth/signin/google']"
            );
            if (providerForm) {
                return true;
            }
            const markers = [
                'choose an account',
                'sign in with google',
                'try signing in with a different account',
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


def wait_for_login_redirect(page, timeout_seconds: float = 8.0) -> bool:
    """等页面稳定下来再回答"是不是掉登录了"。

    Flow 是 SPA：导航回来时 domcontentloaded 已经触发，但会话失效的重定向
    （labs.google → accounts.google.com）还要几百毫秒到几秒才发生。这个空档里
    问 is_google_login_page 一定得到 False，于是**掉登录被当成已登录**——
    2026-08-05 真机验证时，一个明明停在账号选择页的环境，「测试登录」回的是
    "这个账号当前已是登录状态"。

    已经进了工作台就立刻返回 False，不白等满整个预算。
    """
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while True:
        if is_google_login_page(page):
            return True
        if _flow_workspace_ready(page):
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _open_flow_entry_in_current_page(page, entry) -> bool:
    """Open the landing-page CTA without letting it create a duplicate tab.

    Flow currently renders the CTA as a link that may use ``target=_blank``.
    A normal Playwright click therefore leaves the landing tab open and creates
    a second Flow tab after ``find_or_create_page`` has already cleaned the
    context.  Resolve the link's absolute URL from the DOM and navigate the
    page we already own instead.  Non-link/button variants fall back to the
    normal click path.
    """
    try:
        href = entry.evaluate("""element => {
            const link = element.closest && element.closest('a[href]');
            return link ? link.href : '';
        }""")
    except Exception:
        href = ""

    if isinstance(href, str) and href.strip():
        page.goto(
            href.strip(),
            timeout=PAGE_LOAD_TIMEOUT,
            wait_until="domcontentloaded",
        )
        return True

    entry.click(timeout=5000)
    return True


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


def attempt_auto_login(page, user_id=None, context_label="Flow导航", cancel_check=None,
                       timeout_seconds=None):
    """掉登录时先试一次自动登录，成功返回 True。

    薄封装，存在的理由是"惰性 import + 异常隔离"这两件事在三个调用点重复：
    - 惰性 import：utils.auto_login 反过来要 import 本模块的 is_google_login_page，
      模块级互相 import 会成环。
    - 异常隔离：自动登录是**附加**能力，它自己炸掉绝不能把调用方的主流程带崩——
      调用方原本的行为（退回等人工）必须原样保留。

    没配凭据 / 熔断中 / 登录失败一律返回 False，调用方照旧走既有的人工处理路径。
    """
    # 普通生图/视频服务通常依赖当前任务绑定，不显式传 user_id；服务刚启动且尚未
    # 建立任务绑定时，则必须回退到控制台配置的默认 AdsPower 环境。旧逻辑只做
    # 前半段，导致“默认环境打开即掉登录”看得到登录页却永远找不到对应凭据。
    user_id = account_binding.resolve_account(
        explicit=user_id,
        fallback=get_runtime_default_user_id() or DEFAULT_USER_ID,
    )

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
            context_label=context_label, timeout_seconds=timeout_seconds))
    except Exception as e:
        log(f"⚠️ 自动登录过程异常（{type(e).__name__}: {e}），退回等待人工处理", context_label)
        return False


# 崩溃页的"证据"必须是崩溃页独有的控件。整份 flow_project_error_btn 不能直接拿来
# **判定**：它末尾那两条 `button:has(i:text('arrow_back'))` 是通用返回箭头，正常项目
# 画布的页头一直挂着它——用它判崩溃，等于每次进画布都判一次崩溃。
_FLOW_CRASH_ONLY_BTNS = (
    "button:has-text('Back to projects')",
    "button:has-text('Back to project')",
    "button:has-text('返回项目')",
    "button:has-text('Kembali ke project')",
    "button:has-text('Kembali ke proyek')",
    "a:has-text('Back to projects')",
    "a:has-text('返回项目')",
)
_FLOW_CRASH_TEXTS = ("something went wrong", "出错了", "terjadi kesalahan")


def _flow_project_crashed(page) -> bool:
    """检测是否停留在项目崩溃/失效页面（例如 'Something went wrong.' + 'Back to projects'）。

    ⚠️ 2026-08-17：这个判定曾经把**健康画布**判成崩溃页，整条单画布复用因此从未生效。
    两个误判源：
    1. 页头的通用返回箭头 `arrow_back`（见 _FLOW_CRASH_ONLY_BTNS 的说明）；
    2. 画布上任意一张 Failed 卡片的正文就是 "Something went wrong"
       （与 _count_error_cards 认的是同一串文案），而这里是全 body 子串匹配。
    误判的代价不是多等一会儿：`ensure_flow_workspace` 会"恢复"——把页面退回项目列表，
    于是绑定的画布丢了、`_open_image_flow_canvas` 找不到编辑器就新建一块空白画布，
    上一帧的结果 tile 不在新画布上，参考图只能一张张重新上传。
    工作台还能用就绝不是崩溃页，这是最可靠的一条否证，放在最前面。
    """
    try:
        # 只认"编辑器可见"这一条否证，不用 _flow_workspace_ready：后者把项目列表页的
        # New project 按钮也算 ready，而真正的崩溃页可能还挂着同一套导航外壳。
        if _any_visible(page, ["textarea", "[contenteditable='true']"]):
            return False
    except Exception:
        pass
    if _any_visible(page, _FLOW_CRASH_ONLY_BTNS):
        return True
    try:
        body_text = (page.inner_text("body", timeout=500) or "").lower()
        if any(token in body_text for token in _FLOW_CRASH_TEXTS):
            return True
    except Exception:
        pass
    return False


def _recover_from_flow_project_crash(page) -> bool:
    """从项目崩溃页返回 Flow 主工作台列表"""
    log("🔄 检测到 Google Flow 项目异常崩溃页 (Something went wrong)，正在返回工作台...", "Flow导航")
    try:
        from ..ui_selectors import UI_SELECTORS
        selectors = UI_SELECTORS.get("google_fx", {}).get("flow_project_error_btn", [])
    except Exception:
        selectors = ["button:has-text('Back to projects')", "button:has-text('返回项目')"]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                btn = locator.nth(index)
                if btn.is_visible(timeout=500) and btn.is_enabled():
                    btn.click(timeout=3000)
                    time.sleep(1.0)
                    return True
        except Exception:
            continue
    # 兜底：如果按钮无法点击或不存在，直接 goto 回 /fx/tools/flow
    try:
        page.goto("https://labs.google/fx/tools/flow", timeout=30000, wait_until="domcontentloaded")
        time.sleep(1.0)
        return True
    except Exception as e:
        log(f"⚠️ 无法自动跳转回 Flow 工作台: {e}", "Flow导航")
        return False


def ensure_flow_workspace(page, timeout_seconds: float = 30.0, user_id=None) -> bool:
    """若账号停在 Flow 产品介绍页或项目崩溃页，自动进入/恢复真正工作台。

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
                    # 必须重置/延长截止时间，避免因登录表单耗时导致下一轮判定直接超时。
                    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    time.sleep(1.0)
                    continue
            log("🔒 检测到 Google 登录页面，无法自动进入工作台，提示人工处理登录", "Flow导航")
            return False
        if flow_onboarding_required(page):
            if not complete_flow_onboarding(page, timeout_seconds=max(1.0, deadline - time.monotonic())):
                log("⚠️ Flow 首次使用引导未能自动完成", "Flow导航")
                return False
            deadline = time.monotonic() + max(20.0, float(timeout_seconds))
            continue
        if _flow_project_crashed(page):
            if not _recover_from_flow_project_crash(page):
                time.sleep(0.5)
            else:
                deadline = max(deadline, time.monotonic() + 15.0)
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
                    _open_flow_entry_in_current_page(page, entry)
                    clicked = True
                    # 点击进入后给工作台加载预留充足时间
                    deadline = max(deadline, time.monotonic() + 15.0)
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
