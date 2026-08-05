# -*- coding: utf-8 -*-
"""
🔓 Google 账号自动重新登录（2026-07-30）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
掉登录之前只有一条路：`wait_out_manual_intervention()` 停下来、发个横幅、等人
最多 20 分钟去 AdsPower 窗口里手动登录，等不到就把整批任务判失败。夜里跑批时
这等于"一掉登录就废一批"。

本模块补上"等人工"之前的那一步：号池里配了凭据的账号，直接把登录表单走完。
成功就当无事发生继续跑，失败**原样退回**既有的等人工逻辑——自动登录是加在前面
的一层机会，不是替换，任何一条它处理不了的路径（验证码、手机点确认、设备验证、
账号被封）都必须落回人工，绝不能自己瞎点。

━━ 为什么放在 utils/ 而不是 services/ ━━
它只依赖 Playwright 页面操作 + 凭据 + 选择器，不碰 Flow 的业务语义；而
`utils/browser.ensure_flow_workspace()` 需要调它。services 可以依赖 utils，反过来
不行，放 services 会让 utils→services 反向依赖。

━━ 安全护栏（这部分不要在没想清楚的时候放宽）━━
1. 每次调用**最多提交一次密码**。提交后还停在密码页 = 密码不对，立刻停手。
   不设这条就会变成拿错密码反复撞，Google 先上验证码、再锁号。
2. 连续 2 次凭据级失败进熔断（account_credentials.MAX_FAIL_STREAK），
   之后除非用户改凭据或手动解除，一律不再自动尝试。
3. 只填身份验证器的动态码，**绝不碰备用码输入框**——备用码是一次性的应急
   手段，自动填等于把用户的后路烧掉。
4. 遇到验证码/设备验证一律立即放弃；手机点确认页仅在账号已配置 TOTP 时，
   点击「Try another way」并切到身份验证器 App，不尝试代替用户确认手机通知。
5. 失败时不落 forensics 快照。别处失败都会存页面 HTML 帮排查，但这里的页面是
   登录表单，快照有把凭据写进 runtime 文件的风险，不值得。
"""

import time

from ..ui_selectors import UI_SELECTORS
from . import account_binding, account_credentials
from .browser import is_google_login_page
from .logger import log

_SEL = UI_SELECTORS.get("google_login", {})

# 单次自动登录的总预算。Google 的每一步（提交邮箱 → 密码页、提交密码 → 2FA 页）
# 都要几秒的跳转，三步加起来正常 15~30s；给 180s 是留给慢代理，超过就是卡住了。
DEFAULT_TIMEOUT_SECONDS = 180

# 每一步允许提交几次。密码是 1（见护栏 1）；邮箱可以 2（第一次可能被账号选择页
# 抢走焦点）；动态码 2（第一次撞上时间窗口边界的容错）。
_MAX_SUBMITS = {"provider": 1, "email": 2, "password": 1, "totp": 2,
                # 登录方式选择页只是点一下「用密码登录」，不提交任何凭据；给 2 次
                # 是容忍一次点击没生效，再多就是这一页根本走不通。
                "method_picker": 2}

_STEP_POLL_SECONDS = 1.0
# 提交一步之后等页面变化的时间。Google 的表单页跳转本身很快，但慢代理下
# 首字节可能要好几秒。
_STEP_SETTLE_SECONDS = 12.0

# 账号选择页上等账号列表渲染出来的时间。这一页的列表是 JS 渲染的，DOM 里还
# 没有元素时 `locator.count()` 立刻返回 0，不轮询就会在页面刚跳过来的瞬间
# 误判成"这页上既没有目标账号也没有使用其他账号"。
_CHOOSER_RENDER_SECONDS = 10.0


class AutoLoginResult:
    """一次自动登录的结果。

    reason 是给机器看的分类（决定要不要算进熔断），message 是给人看的中文说明
    （进日志、进控制台、进号池的 auto_login_error）。两者分开，避免出现"靠中文
    文案里有没有'密码'两个字来判断要不要熔断"这种脆弱逻辑。
    """

    __slots__ = ("ok", "reason", "message")

    def __init__(self, ok: bool, reason: str, message: str):
        self.ok = ok
        self.reason = reason
        self.message = message

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"AutoLoginResult(ok={self.ok}, reason={self.reason!r}, message={self.message!r})"


def _first_visible(page, selectors, timeout_ms: int = 400):
    """按顺序找第一个真正可见的元素。选择器列表是 fallback 链，不是"全都要"。"""
    for selector in selectors or ():
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible(timeout=timeout_ms):
                return locator
        except Exception:
            continue
    return None


def _page_text(page) -> str:
    try:
        return (page.inner_text("body") or "").replace("\n", " ").strip().lower()
    except Exception:
        return ""


# 在页面上按可见文本找一个可点的行，返回它的中心点坐标。
# 为什么不继续堆 CSS 选择器：Google 这几页的 DOM 形状换得很勤（li →
# div[role=link] → 只带 jsname 的自定义容器），每换一次就要追一次，而追不上的
# 代价是整条自动登录卡死。文本是这页上最稳定的东西。
#
# 两个关键点：
#  · 取**最内层**匹配元素。`*:has-text()` 这类写法会把 body/html 一起匹配上，
#    点 body 什么都不会发生——这正是"选择器看着匹配了却没反应"的经典坑。
#  · 返回坐标交给 Playwright 真点，而不是在 JS 里 el.click()。Google 用 jsaction
#    绑事件，合成事件有时不触发，真实鼠标事件一定触发。
_CLICKABLE_TEXT_SCRIPT = r"""(needles) => {
  const wanted = (needles || []).map(s => s.toLowerCase());
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // 粗筛用 textContent 而不是 innerText：innerText 每次都会触发一次布局，
  // 这一页有几千个节点，逐个取会明显拖慢。可见性另外用 rect 判。
  const matches = (el) => {
    const t = norm(el.textContent);
    return t && t.length <= 80 && wanted.some(w => t === w || t.startsWith(w));
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return false;
    return true;
  };

  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
    if (!matches(el)) continue;
    // 最内层优先：文档序里祖先排在前面，后代若也匹配同一段文本就让给后代。
    let deeper = false;
    for (const child of el.querySelectorAll('*')) {
      if (matches(child)) { deeper = true; break; }
    }
    if (deeper) continue;
    if (!visible(el)) continue;
    // 文本所在的节点本身常常不可点，向上找最近的可点容器。BOQ 页面上这层
    // 通常是带 jsaction / jsname 的 div，没有 role 也没有语义标签。
    let target = el;
    for (let i = 0; i < 4 && target.parentElement; i++) {
      if (target.matches('a,button,li,[role],[jsaction],[jsname],[tabindex]')) break;
      target = target.parentElement;
    }
    // 先滚到视野中央再量坐标：page.mouse.click 用的是视口坐标，元素在视口外
    // 时量出来的坐标点不到。
    try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
    const r = target.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    return {x: r.left + r.width / 2, y: r.top + r.height / 2,
            text: norm(el.textContent).slice(0, 60)};
  }
  return null;
}"""


def _click_by_text(page, needles, what: str) -> bool:
    """按可见文本点击页面上的一行。选择器链走不通时的兜底。"""
    try:
        hit = page.evaluate(_CLICKABLE_TEXT_SCRIPT, list(needles))
    except Exception:
        return False
    if not hit:
        return False
    try:
        page.mouse.click(float(hit["x"]), float(hit["y"]))
    except Exception as e:
        log(f"⚠️ 自动登录：按文本点击{what}失败 {type(e).__name__}: {e}", "自动登录")
        return False
    log(f"🔓 自动登录：按文本点中{what}（“{hit.get('text', '')}”）", "自动登录")
    return True


# 页面卡住时，把这页上到底有哪些可点的东西打进日志。没有这行，"卡在某个页面"
# 只能靠截图来回猜选择器。只取可见控件的短文本，不碰任何输入框的值。
_VISIBLE_OPTIONS_SCRIPT = r"""() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(
      'a,button,li,[role="link"],[role="button"],input[type="submit"]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const t = (el.innerText || el.value || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 60 || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
    if (out.length >= 10) break;
  }
  return out;
}"""


def _describe_options(page) -> str:
    try:
        options = page.evaluate(_VISIBLE_OPTIONS_SCRIPT)
    except Exception:
        return ""
    if not isinstance(options, (list, tuple)):
        return ""
    options = [str(o) for o in options if str(o).strip()]
    return "｜".join(options[:10])


def _log_stuck_page(page, step: str):
    options = _describe_options(page)
    url = _current_url(page)[:160]
    log(f"🔎 自动登录：停在 {step} 这一步，URL={url or '未知'}；"
        f"页面上可见的可点项 = [{options or '无'}]", "自动登录")


def _current_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "").lower()
    except Exception:
        return ""


def _browser_gone(page) -> bool:
    try:
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return True
    except Exception:
        return True
    return False


# ── 页面状态判定 ────────────────────────────────────────────────

# 这些文案出现 = 这一步自动化处理不了，必须立刻交回人工。放弃比乱点安全：
# 在设备验证页上乱点可能把账号推进更难恢复的状态。
# 登录方式选择页的**标题**。判定必须用标题而不是选项文案：
# "Enter your password" 在真正的密码页上也会出现（Material 的浮动 label 就是
# 这几个字，会进 innerText），拿它判定会把密码页也当成选择页，然后在 label 上
# 反复点。"Choose how you want to sign in" 只出现在选择页，没有这个歧义。
#
# 也**不能**靠"页面上有没有密码框"来区分：Flow 的登录是 BOQ 单页应用，选择页
# 往往已经把密码框预渲染在 DOM 里，点选项只是把它显示出来。用密码框做前提，
# 会让整段识别在真机上直接失效——2026-08-05 现场就是这么退回 challenge_picker
# 分支、最后报 no_authenticator_option 的。
_METHOD_PICKER_HEADINGS = (
    "choose how you want to sign in",
    "how do you want to sign in",
    "选择您要使用的登录方式",
    "选择登录方式",
    "選擇你要使用的登入方式",
    "pilih cara login",
)

# 但这句标题**两页都有**：两步验证的方式列表同样写着 "Choose how you want to
# sign in"，只是多一个 "2-Step Verification" 抬头。所以两者的真正分界是这组词，
# 而不是标题本身。判反了的后果是对称的：把 2FA 页当选择页会去点密码（点不到，
# 白耗两次），把选择页当 2FA 页则是现场那条 no_authenticator_option 死路。
_TWO_STEP_MARKERS = (
    "2-step verification",
    "two-step verification",
    "2 步验证",
    "两步验证",
    "兩步驗證",
)

# 选择页上「用密码登录」那一项的文案。只用于点击，不用于判定。
_PASSWORD_OPTION_TEXTS = (
    "enter your password",
    "输入您的密码",
    "输入密码",
    "輸入您的密碼",
    "masukkan sandi",
)


_HARD_STOP_MARKERS = (
    ("captcha", "captcha_required", "登录页出现了图形验证码"),
    ("not a robot", "captcha_required", "登录页出现了人机验证"),
    ("人机验证", "captcha_required", "登录页出现了人机验证"),
    # 只匹配 "check your phone"，不匹配裸的 "check your"——后者会把
    # "check your email"、"check your recovery options" 这类页面也判成
    # "要在手机上点确认"，报一个明确错误的原因比报"未知页面"更误导人。
    ("check your phone", "device_confirm_required", "Google 要求在手机上点确认（无法自动完成）"),
    ("tap yes", "device_confirm_required", "Google 要求在手机上点「是」（无法自动完成）"),
    ("2-step verification", "device_confirm_required",
     "Google 要求两步验证，但当前页面不是可自动填写的动态码页"),
    ("couldn't verify", "verification_required", "Google 无法验证这台设备"),
    ("unusual activity", "security_check", "Google 判定为异常活动，需要人工申诉"),
    ("unusual traffic", "security_check", "Google 判定为异常流量，需要人工处理"),
    ("account disabled", "account_rejected", "该 Google 账号已被停用"),
    ("账号已被停用", "account_rejected", "该 Google 账号已被停用"),
)


def _detect_step(page) -> tuple:
    """判断登录流程当前停在哪一步，返回 (step, detail)。

    顺序有讲究：先判"已经登进去了"（最常见的退出条件），再识别可自动处理的
    TOTP/验证方式切换入口，然后才判硬停条件。否则手机确认页里的
    "2-Step Verification / Tap Yes" 会在「Try another way」被看到前就提前退出。
    密码框排在邮箱框前面——账号选择页跳到密码页时，DOM 里可能还残留着隐藏的
    邮箱 input。
    """
    if _browser_gone(page):
        return "browser_gone", ""

    if not is_google_login_page(page):
        return "done", ""

    url = _current_url(page)
    text = _page_text(page)

    # Google 默认把不少账号推到手机通知确认页；只要页面明确提供「换一种方式」
    # 或已经展示身份验证器选项，就交给 challenge_picker 分支。这里必须位于
    # HARD_STOP_MARKERS 前面，因为同一页正文必然含有 "2-Step Verification"。
    if _first_visible(page, _SEL.get("totp_input")):
        return "totp", ""
    # 新版登录方式选择页（"Choose how you want to sign in: Enter your password /
    # Use your passkey / Try another way"）必须排在 challenge_picker 前面：它上面
    # 那个「Try another way」是登录方式入口，不是两步验证的换方式入口。反过来的
    # 顺序会把这一页当成 2FA picker，去找永远不存在的身份验证器选项，然后在
    # 「Try another way」的下级页里空转到超时。
    #
    # 判定以**标题文案**为准、选择器只作快路：这一页的行没有稳定的标签或 role，
    # 只按选择器认会漏掉，漏掉就退回上面那条卡死路径。
    if not any(marker in text for marker in _TWO_STEP_MARKERS):
        if (any(heading in text for heading in _METHOD_PICKER_HEADINGS)
                or _first_visible(page, _SEL.get("password_option"))):
            return "method_picker", ""
    # 「Try another way」**不是**两步验证页的专属标志：普通密码页底部就挂着一个
    # 同名链接（真机验证，2026-08-05：/v3/signin/challenge/pwd 上 password_input、
    # next_btn、try_another_way 三者同时命中）。只凭它判定，会把密码页当成 2FA
    # 页，然后我们自己点掉「Try another way」跳去登录方式选择页，再报
    # no_authenticator_option——用户看到的"卡在 Welcome 页"就是这么来的。
    # 所以：页面上只要有能填的密码框/邮箱框，就绝不是验证方式选择页。
    if not (_first_visible(page, _SEL.get("password_input"))
            or _first_visible(page, _SEL.get("email_input"))):
        if (_first_visible(page, _SEL.get("authenticator_option")) or
                _first_visible(page, _SEL.get("try_another_way"))):
            return "challenge_picker", ""

    # Flow 有时先落在 labs.google 自己的 Auth.js 中转页，而不是直接跳到
    # accounts.google.com。先点这里的 provider 按钮，后续仍由同一状态机处理。
    if _first_visible(page, _SEL.get("provider_signin")):
        return "provider", ""

    for needle, reason, message in _HARD_STOP_MARKERS:
        if needle in text:
            # "2-step verification" 只是标题时不算硬停——TOTP 输入页也叫这个名字。
            # 只有页面上同时没有动态码输入框才说明它真的在推手机确认。
            if reason == "device_confirm_required" and _first_visible(page, _SEL.get("totp_input")):
                continue
            return "hard_stop", (reason, message)

    if _first_visible(page, _SEL.get("password_input")):
        return "password", ""
    if "accountchooser" in url or "selectaccount" in url:
        # URL 停在账号选择页，但渲染出来的其实是邮箱表单：环境里的会话全部过期
        # 时 Google 会就地把列表换成 identifier 表单，URL 不跟着变。此时按
        # chooser 处理会卡在"找不到账号行"上直接放弃，而实际上填邮箱就能走完。
        if _first_visible(page, _SEL.get("email_input")):
            return "email", ""
        return "chooser", ""
    if _first_visible(page, _SEL.get("email_input")):
        return "email", ""
    return "unknown", ""


def _submit(page, locator, value: str) -> bool:
    """把 value 敲进输入框并提交这一步。

    逐字符输入而不是 fill()：Google 的登录表单对"整段值瞬间出现且不带任何
    keydown"的输入有识别能力，fill() 触发的登录更容易被推去人机验证。
    延迟取 40~90ms 量级，跟正常打字一个数量级。
    """
    try:
        locator.click(timeout=5000)
    except Exception:
        pass
    try:
        locator.fill("")  # 清掉可能的残留（账号选择页回退时邮箱框会预填）
    except Exception:
        pass

    typed = False
    for method in ("press_sequentially", "type"):
        fn = getattr(locator, method, None)
        if not callable(fn):
            continue
        try:
            fn(value, delay=60)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        try:
            locator.fill(value)
        except Exception as e:
            log(f"⚠️ 自动登录：输入框写入失败 {type(e).__name__}: {e}", "自动登录")
            return False

    time.sleep(0.4)
    next_btn = _first_visible(page, _SEL.get("next_btn"))
    if next_btn is not None:
        try:
            next_btn.click(timeout=5000)
            return True
        except Exception:
            pass
    # 「下一步」按钮定位不到时回退到回车。Google 的每一步表单都接受回车提交。
    try:
        locator.press("Enter")
        return True
    except Exception as e:
        log(f"⚠️ 自动登录：提交失败 {type(e).__name__}: {e}", "自动登录")
        return False


def _wait_for_step_change(page, previous_step: str, timeout_seconds: float) -> str:
    """等页面离开 previous_step。返回新的 step（超时就返回原 step）。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(_STEP_POLL_SECONDS)
        step, _ = _detect_step(page)
        if step != previous_step:
            return step
    return previous_step


def _visible_error(page) -> str:
    """读登录页上的错误提示。用来把"密码错"和"网络慢还没跳转"分开。"""
    node = _first_visible(page, _SEL.get("error_text"), timeout_ms=300)
    if node is None:
        return ""
    try:
        return (node.inner_text() or "").replace("\n", " ").strip()[:200]
    except Exception:
        return ""


def _click_account_row_by_attribute(page, email: str) -> bool:
    """按 data-identifier / data-email 属性精确匹配账号行并点击。

    比 :has-text 稳两点：属性里是账号原文（列表里的可见文本可能被截断成
    "jia…@gmail.com"），且属性比较可以大小写不敏感（Google 显示的邮箱大小写
    未必和用户在号池里填的一致）。
    """
    target = (email or "").strip().lower()
    if not target:
        return False

    for attr in _SEL.get("account_row_attrs") or ():
        try:
            rows = page.locator(f"[{attr}]")
            count = min(int(rows.count()), 20)
        except Exception:
            continue
        for index in range(count):
            try:
                row = rows.nth(index)
                if (row.get_attribute(attr) or "").strip().lower() != target:
                    continue
                if not row.is_visible(timeout=600):
                    continue
                row.click(timeout=5000)
                log(f"🔓 自动登录：在账号选择页按 {attr} 选中 {email}", "自动登录")
                return True
            except Exception:
                continue
    return False


def _pick_account_from_chooser(page, email: str) -> bool:
    """账号选择页：优先直接点目标邮箱那一行，找不到再点「使用其他账号」。

    直接点目标邮箱能少走一整步（不用再填邮箱），而且能避免一个真实的坑：
    环境里如果登过多个号，点「使用其他账号」重新填邮箱，Google 有时会把新号
    追加成第二个登录身份而不是替换，Flow 之后用的还是旧号。

    整段要**轮询等待**而不是扫一遍就下结论：账号列表是 JS 渲染的，元素还没
    进 DOM 时 `count()` 立刻返回 0（连 is_visible 的 600ms 都等不到），单次
    扫描会在页面刚跳过来的那一瞬间就误判成"这页上什么都没有"。线上真实事故
    （2026-08-05，账号 k1anmo58）就是检测到掉登录后 1 秒内报 chooser_stuck。
    """
    deadline = time.monotonic() + _CHOOSER_RENDER_SECONDS

    while True:
        if _click_account_row_by_attribute(page, email):
            return True

        if email:
            # 邮箱里可能有需要转义的字符，用 Playwright 的 :has-text 传入原文即可。
            for selector in (f"li:has-text('{email}')",
                             f"div[role='link']:has-text('{email}')",
                             f"div[role='button']:has-text('{email}')"):
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible(timeout=600):
                        locator.click(timeout=5000)
                        log(f"🔓 自动登录：在账号选择页直接选中 {email}", "自动登录")
                        return True
                except Exception:
                    continue

        another = _first_visible(page, _SEL.get("use_another_account"))
        if another is not None:
            try:
                another.click(timeout=5000)
                log("🔓 自动登录：账号选择页未列出目标账号，点「使用其他账号」", "自动登录")
                return True
            except Exception:
                pass

        # 等待期间页面可能自己走掉（列表渲染完直接跳密码页，或 URL 变成邮箱
        # 表单）。这不是失败，交回主状态机按新的一步继续。
        step, _ = _detect_step(page)
        if step != "chooser":
            return True

        if time.monotonic() >= deadline:
            return False
        time.sleep(_STEP_POLL_SECONDS)


def _switch_to_authenticator(page) -> bool:
    """两步验证停在别的方式（短信/手机点确认）时，切到身份验证器 App 那一项。"""
    # 图二的方式列表本身也有一项叫「Try another way」。因此必须先找
    # Authenticator；如果反过来，会在已经进入列表后又点一次底部入口，跳离目标页。
    option = _first_visible(page, _SEL.get("authenticator_option"))
    if option is None:
        trigger = _first_visible(page, _SEL.get("try_another_way"))
        if trigger is None:
            return False
        try:
            trigger.click(timeout=5000)
            log("🔓 自动登录：手机确认页点击「Try another way」", "自动登录")
        except Exception:
            return False

        # 慢代理下方式列表不会立即出现，轮询等待，而不是固定睡 1.5 秒后误判失败。
        wait_deadline = time.monotonic() + _STEP_SETTLE_SECONDS
        while time.monotonic() < wait_deadline:
            time.sleep(_STEP_POLL_SECONDS)
            option = _first_visible(page, _SEL.get("authenticator_option"))
            if option is not None:
                break

    if option is None:
        return False
    try:
        option.click(timeout=5000)
        log("🔓 自动登录：已切换到身份验证器 App 验证方式", "自动登录")
        return True
    except Exception:
        return False


# ── 主流程 ──────────────────────────────────────────────────────

def try_auto_login(page, user_id: str = None, timeout_seconds: float = None,
                   cancel_check=None, context_label: str = "Google FX") -> AutoLoginResult:
    """尝试把当前这个停在 Google 登录页的浏览器重新登进去。

    page 不在登录页时是幂等空操作（返回 ok，reason='not_needed'），所以调用方
    可以无脑在"疑似掉登录"的地方调它，不必先自己判断。

    user_id 省略时按 account_binding 的优先级解析当前上下文账号——生成链路里
    账号是绑好的；积分探针那种显式指定账号的路径必须**显式传入**，否则会拿到
    进程默认账号的凭据去登另一个号。
    """
    user_id = account_binding.resolve_account(explicit=user_id).strip()
    if not user_id:
        return AutoLoginResult(False, "no_account", "无法确定当前是哪个账号，跳过自动登录")

    step, detail = _detect_step(page)
    if step == "done":
        return AutoLoginResult(True, "not_needed", "当前不在登录页，无需自动登录")
    if step == "browser_gone":
        return AutoLoginResult(False, "browser_gone", "浏览器/标签页已关闭")

    creds = account_credentials.get(user_id)
    if not creds or not creds.get("email") or not creds.get("password"):
        return AutoLoginResult(
            False, "no_credentials",
            f"账号 {user_id} 没配登录凭据，无法自动登录（在配置中心 › 生成号池里补邮箱和密码）")

    if account_credentials.is_blocked(user_id):
        return AutoLoginResult(
            False, "breaker_open",
            f"账号 {user_id} 的自动登录已熔断（连续 "
            f"{account_credentials.MAX_FAIL_STREAK} 次凭据失败），"
            "请先核对密码/2FA 密钥再手动解除")

    email = creds["email"]
    password = creds["password"]
    has_totp = bool(creds.get("totp_secret"))
    budget = float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
    deadline = time.monotonic() + budget

    log(f"🔓 {context_label} 检测到掉登录，开始自动重新登录账号 {email}"
        f"（{user_id}，2FA={'已配置' if has_totp else '未配置'}，预算 {budget:.0f}s）", "自动登录")

    submits = {"provider": 0, "email": 0, "password": 0, "totp": 0,
               "method_picker": 0}
    result = _run_login_steps(page, email, password, has_totp, user_id,
                              submits, deadline, cancel_check)

    account_credentials.record_attempt(
        user_id, "ok" if result.ok else "failed", result.reason, result.message)
    if result.ok:
        log(f"✅ 账号 {email} 自动重新登录成功，{context_label} 继续执行", "自动登录")
    else:
        log(f"⚠️ 账号 {email} 自动登录未成功（{result.reason}）: {result.message}"
            "；退回等待人工处理", "自动登录")
        if result.reason == "wrong_totp":
            # 时钟偏差是这个错误最常见、最不容易想到的成因，点名提醒。
            log("💡 2FA 码被拒的常见原因是本机时钟不准：动态码按本地时间算，"
                "偏差超过 ±30 秒就一定对不上。请检查系统时间是否为自动同步。", "自动登录")
    return result


def _run_login_steps(page, email, password, has_totp, user_id,
                     submits, deadline, cancel_check) -> AutoLoginResult:
    """状态机主体。每一轮重新判定当前在哪一步，再执行对应动作。

    写成"每轮重新判定"而不是"按邮箱→密码→2FA 顺序往下走"，是因为 Google 的流程
    不是固定的：有的号不出账号选择页，有的号密码后直接进，有的号会插一页"确认
    恢复邮箱"。按固定顺序驱动会在任何一次插页面前失控。
    """
    last_step = None
    idle_rounds = 0

    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            return AutoLoginResult(False, "cancelled", "自动登录期间被取消")

        step, detail = _detect_step(page)

        if step == "done":
            return AutoLoginResult(True, "logged_in", "已重新登录")
        if step == "browser_gone":
            return AutoLoginResult(False, "browser_gone", "自动登录期间浏览器/标签页被关闭")
        if step == "hard_stop":
            reason, message = detail
            return AutoLoginResult(False, reason, message)

        if step == "chooser":
            if not _pick_account_from_chooser(page, email):
                _log_stuck_page(page, "账号选择页")
                return AutoLoginResult(
                    False, "chooser_stuck",
                    f"账号选择页等了 {_CHOOSER_RENDER_SECONDS:.0f}s，既没出现目标账号那一行，"
                    "也没出现「使用其他账号」入口")
            _wait_for_step_change(page, "chooser", _STEP_SETTLE_SECONDS)
            continue

        if step == "provider":
            if submits["provider"] >= _MAX_SUBMITS["provider"]:
                return AutoLoginResult(
                    False, "provider_stuck",
                    "点击 Google 登录按钮后仍停在 Flow 登录中转页")
            provider = _first_visible(page, _SEL.get("provider_signin"))
            if provider is None:
                # 页面可能刚好正在跳转，交给下一轮重新判断。
                time.sleep(_STEP_POLL_SECONDS)
                continue
            try:
                submits["provider"] += 1
                provider.click(timeout=5000)
                log("🔓 自动登录：点击 Flow 登录中转页的「Sign in with Google」", "自动登录")
            except Exception as e:
                return AutoLoginResult(
                    False, "provider_submit_failed",
                    f"Flow 登录中转页的 Google 登录按钮点击失败（{type(e).__name__}: {e}）")
            _wait_for_step_change(page, "provider", _STEP_SETTLE_SECONDS)
            continue

        if step == "method_picker":
            if submits["method_picker"] >= _MAX_SUBMITS["method_picker"]:
                _log_stuck_page(page, "登录方式选择页")
                return AutoLoginResult(
                    False, "method_picker_stuck",
                    "点了登录方式选择页上的「用密码登录」，页面仍停在原处")

            submits["method_picker"] += 1
            clicked = False
            option = _first_visible(page, _SEL.get("password_option"))
            if option is not None:
                try:
                    option.click(timeout=5000)
                    log("🔓 自动登录：登录方式选择页选择「用密码登录」", "自动登录")
                    clicked = True
                except Exception as e:
                    log(f"⚠️ 自动登录：「用密码登录」按选择器点击失败 "
                        f"{type(e).__name__}: {e}，改按文本点", "自动登录")
            # 选择器没命中就按文本点。这一页的行没有稳定标签/role，选择器链
            # 追不上 Google 改版是常态，文本兜底才是这里真正靠得住的一层。
            if not clicked:
                clicked = _click_by_text(page, _PASSWORD_OPTION_TEXTS, "「用密码登录」")
            if not clicked:
                _log_stuck_page(page, "登录方式选择页")
                return AutoLoginResult(
                    False, "method_picker_click_failed",
                    "登录方式选择页上点不到「用密码登录」（选择器和文本定位都没命中）")
            _wait_for_step_change(page, "method_picker", _STEP_SETTLE_SECONDS)
            continue

        if step == "challenge_picker":
            if not has_totp:
                return AutoLoginResult(
                    False, "totp_not_configured",
                    "Google 要求两步验证，但这个账号没配 TOTP 密钥")
            if not _switch_to_authenticator(page):
                # 找不到身份验证器时，先看看这页上是不是有「用密码登录」——说明
                # 我们其实落在了登录方式列表而不是两步验证列表（真正的 2FA 列表
                # 里不会有这一项，所以点不到就照旧失败，不会误伤）。
                if (submits["method_picker"] < _MAX_SUBMITS["method_picker"]
                        and _click_by_text(page, _PASSWORD_OPTION_TEXTS, "「用密码登录」")):
                    submits["method_picker"] += 1
                    _wait_for_step_change(page, "challenge_picker", _STEP_SETTLE_SECONDS)
                    continue
                _log_stuck_page(page, "两步验证方式列表")
                return AutoLoginResult(
                    False, "no_authenticator_option",
                    "两步验证页上找不到「身份验证器 App」选项（可能只开了短信或手机确认）")
            _wait_for_step_change(page, "challenge_picker", _STEP_SETTLE_SECONDS)
            continue

        if step in ("email", "password", "totp"):
            if submits[step] >= _MAX_SUBMITS[step]:
                return _classify_stuck(page, step)

            if step == "email":
                value, selectors = email, _SEL.get("email_input")
            elif step == "password":
                value, selectors = password, _SEL.get("password_input")
            else:
                if not has_totp:
                    return AutoLoginResult(
                        False, "totp_not_configured",
                        "Google 要求输入两步验证码，但这个账号没配 TOTP 密钥")
                value = account_credentials.current_totp(user_id)
                if not value:
                    return AutoLoginResult(False, "totp_unavailable",
                                           "TOTP 密钥无法算出动态码，请检查密钥是否填错")
                selectors = _SEL.get("totp_input")

            field = _first_visible(page, selectors)
            if field is None:
                # 判定说在这一步、但输入框又拿不到：页面正在跳转的中间态，
                # 下一轮重判即可，不算一次提交。
                idle_rounds += 1
                if idle_rounds > 10:
                    return AutoLoginResult(False, "field_not_found",
                                           f"停在 {step} 步骤但找不到可输入的表单框")
                time.sleep(_STEP_POLL_SECONDS)
                continue

            idle_rounds = 0
            submits[step] += 1
            label = {"email": "邮箱", "password": "密码", "totp": "两步验证码"}[step]
            log(f"🔓 自动登录：提交{label}（第 {submits[step]} 次）", "自动登录")
            if not _submit(page, field, value):
                return AutoLoginResult(False, "submit_failed", f"{label}提交动作失败")

            new_step = _wait_for_step_change(page, step, _STEP_SETTLE_SECONDS)
            if new_step == step:
                # 还停在原地：可能是错了，也可能只是慢。让下一轮的
                # submits 上限 + _classify_stuck 去分辨，不在这里下结论。
                error = _visible_error(page)
                if error:
                    log(f"⚠️ 自动登录：{label}这一步页面报错 — {error}", "自动登录")
            last_step = step
            continue

        # unknown：Google 插了一页我们不认识的东西（确认恢复邮箱、同意条款…）。
        # 不乱点，等它自己跳转（有些是纯过场页），等不动就交回人工。
        idle_rounds += 1
        if idle_rounds > 15:
            _log_stuck_page(page, "无法识别的页面")
            return AutoLoginResult(
                False, "unknown_page",
                "登录流程停在一个无法识别的页面，需要人工在 AdsPower 窗口里处理")
        time.sleep(_STEP_POLL_SECONDS)

    return AutoLoginResult(False, "timeout",
                           f"自动登录超时（{'仍停在 ' + last_step if last_step else '未完成'}）")


def _classify_stuck(page, step: str) -> AutoLoginResult:
    """提交次数用尽还停在同一步：把原因判准，因为它决定要不要熔断。"""
    error = _visible_error(page)
    if step == "password":
        return AutoLoginResult(
            False, "wrong_password",
            f"密码提交后仍停在密码页，判定密码不正确{'（页面提示：' + error + '）' if error else ''}")
    if step == "totp":
        return AutoLoginResult(
            False, "wrong_totp",
            f"两步验证码被拒绝{'（页面提示：' + error + '）' if error else ''}")
    return AutoLoginResult(
        False, "wrong_email",
        f"邮箱提交后仍停在邮箱页，可能是账号名不对{'（页面提示：' + error + '）' if error else ''}")


def run_standalone_login(user_id: str, port=None,
                         timeout_seconds: float = None) -> AutoLoginResult:
    """主动开一次浏览器把这个账号登进去（控制台的「测试登录」按钮）。

    跟 try_auto_login 的区别：那个是"你已经有一个停在登录页的 page 了"，这个负责
    从零把 AdsPower 拉起来、连 CDP、导航到 Flow。用途是让用户配完凭据能当场验证，
    而不是等某天夜里掉登录才发现密码填错了。

    必须走 browser_slot：它跟生成任务抢的是同一个 AdsPower profile，绕开闸门会
    在别人跑批时抢走浏览器、按 Escape、搅乱 Flow 画布（browser_gate 模块开头那段
    说明的就是这个事故）。优先级跟积分探针同级——都是短、且用户正盯着结果的动作。
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        return AutoLoginResult(False, "no_account", "缺少 user_id")

    creds = account_credentials.get(user_id)
    if not creds or not creds.get("email") or not creds.get("password"):
        return AutoLoginResult(False, "no_credentials",
                               "这个账号还没配邮箱和密码，先保存凭据再测试")

    from playwright.sync_api import sync_playwright

    from .browser import (find_or_create_page, get_ads_ws_url,
                          wait_for_login_redirect)
    from .browser_gate import browser_slot

    flow_url = "https://labs.google/fx/tools/flow"
    entered_slot = False
    try:
        with browser_slot('auto_login', priority=40, task_id=f'auto_login_{user_id}'):
            entered_slot = True
            ws_url = get_ads_ws_url(user_id=user_id, port=port, auto_rotate_proxy=False)
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(ws_url, timeout=30000)
                context = browser.contexts[0]
                # 这里要返回 try_auto_login 的详细结果，禁用公共浏览器入口的
                # bool 型前置尝试，避免同一次“测试登录”被执行两遍。
                page = find_or_create_page(
                    context, "/fx/tools/flow", fallback_url=flow_url,
                    user_id=user_id, auto_login=False)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass

                # 必须等重定向稳定再判：Flow 的会话失效重定向发生在
                # domcontentloaded 之后，抢答会把掉登录报成"已是登录状态"。
                if not wait_for_login_redirect(page):
                    # 已经是登录状态。这不是"测试失败"——用户想确认的正是"这个号能
                    # 用"，而它现在就能用。但也别谎称"自动登录成功"（压根没登）。
                    return AutoLoginResult(True, "already_logged_in",
                                           "这个账号当前已是登录状态，无需重新登录")

                return try_auto_login(page, user_id=user_id,
                                      timeout_seconds=timeout_seconds,
                                      context_label="测试登录")
    except Exception as e:
        if not entered_slot:
            return AutoLoginResult(False, "browser_busy",
                                   "浏览器正被其它 FX 任务占用，没能拿到浏览器，请稍后再试")
        log(f"❌ 账号 {user_id} 测试登录异常: {type(e).__name__}: {e}", "自动登录")
        return AutoLoginResult(False, "exception", f"{type(e).__name__}: {e}")


def auto_login_available(user_id: str = None) -> bool:
    """这个账号现在能不能自动登录：配了凭据且没在熔断。

    调用点用它来决定日志文案（"尝试自动登录" vs "直接等人工"），避免在根本没配
    凭据的账号上打一行"自动登录失败"，那会让人以为功能坏了。
    """
    user_id = account_binding.resolve_account(explicit=user_id).strip()
    if not user_id:
        return False
    return (account_credentials.has_credentials(user_id)
            and not account_credentials.is_blocked(user_id))
