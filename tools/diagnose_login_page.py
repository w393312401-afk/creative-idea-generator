# -*- coding: utf-8 -*-
"""只读诊断：把 AdsPower 环境里当前 Google 登录页的真实形状打出来。

用法：  python tools/diagnose_login_page.py <user_id>

存在的理由：登录页失败时**不留 forensics 快照**（auto_login 模块开头护栏 5：
那页是登录表单，快照有把凭据写进 runtime 文件的风险）。于是"卡在某个页面"
只能靠截图来回猜选择器——2026-08-05 就这么来回猜了三轮。这个脚本补上那个
缺口：它只读不点，打印 auto_login 的状态机**实际看到**的东西。

只读是硬约束：不点击、不输入、不提交。唯一的副作用是把标签页导航到 Flow。
"""

import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from playwright.sync_api import sync_playwright  # noqa: E402

from integrations.google_fx.utils import auto_login  # noqa: E402
from integrations.google_fx.utils.browser import (  # noqa: E402
    find_or_create_page, get_ads_ws_url, is_google_login_page,
)
from integrations.google_fx.utils.browser_gate import browser_slot  # noqa: E402

_SELECTOR_FAMILIES = (
    "provider_signin", "email_input", "password_input", "totp_input",
    "password_option", "try_another_way", "authenticator_option",
    "use_another_account", "next_btn",
)

FLOW_URL = "https://labs.google/fx/tools/flow"


def dump(page, title: str):
    print("=" * 70)
    print(title)
    print("URL              :", page.url)
    print("是否登录页       :", is_google_login_page(page))
    print("状态机判定       :", auto_login._detect_step(page))
    print("可见可点项       :", auto_login._describe_options(page) or "（无）")
    print("-" * 70)
    for family in _SELECTOR_FAMILIES:
        hit = auto_login._first_visible(page, auto_login._SEL.get(family))
        print(f"  {family:<22}{'命中' if hit is not None else '—'}")
    print("-" * 70)
    # 正文是判定 method_picker / hard_stop 的依据，原样打出来最有用。
    print("正文（前 800 字）:")
    print(auto_login._page_text(page)[:800] or "（空）")
    print("=" * 70)


def main(user_id: str, advance_email: str = ""):
    with browser_slot("login_diagnose", priority=40, task_id=f"diag_{user_id}"):
        ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url, timeout=30000)
            context = browser.contexts[0]
            page = find_or_create_page(
                context, "/fx/tools/flow", fallback_url=FLOW_URL,
                user_id=user_id, auto_login=False)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass

            # 卡片是 JS 渲染的，导航刚回来时页面还停在 loading——这时候判会得到
            # "不是登录页"。等到**确实是登录页**或**确实进了工作台**再看，
            # 而不是"正文非空"就算数（Flow 的 loading 页正文也非空）。
            for _ in range(60):
                text = auto_login._page_text(page)
                if is_google_login_page(page):
                    break
                if text and "loading" not in text.replace(" ", ""):
                    break
                page.wait_for_timeout(1000)

            dump(page, "【当前页面】")

            if advance_email:
                # 只点账号选择页上的那一行账号。这一步不提交任何凭据，目的是
                # 看清它后面那一页（自动登录正是卡在那里）。
                if auto_login._click_account_row_by_attribute(page, advance_email):
                    page.wait_for_timeout(4000)
                    dump(page, "【点掉账号行之后】")
                else:
                    print("！未能在账号选择页上定位到", advance_email)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tools/diagnose_login_page.py <user_id> [要点开的账号邮箱]")
        raise SystemExit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
