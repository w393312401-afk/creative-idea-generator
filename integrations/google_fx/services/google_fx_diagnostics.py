# -*- coding: utf-8 -*-
"""
🩺 Google FX 诊断服务（2026-07-26 新增）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
两个能力，都是给"服务到底还能不能用"这个问题一个**当场可验证**的答案，
而不是等下一单真任务失败了再回头翻日志：

1. `probe_selectors(page)` —— 对 UI_SELECTORS 里每个选择器族在当前页面逐层
   count/visible 一遍，直接告出"哪个族已经失效、现在命中的是第几层兜底"。
   Flow 改版是这套自动化最常见的故障源，此前只能靠 selector_stats 的事后
   命中率反推。

2. `run_selftest(level)` —— 分级自检：
   - L0 只读：运行时导入、配置、AdsPower 端口、号池状态文件、runtime 可写性。
     不碰浏览器。
   - L1 连浏览器不提交：连 CDP、开 Flow、定位 prompt 输入/发送/配置按钮、
     跑一次选择器探针、读积分。**不点发送**。
   - L2 真实最小提交：在 L1 的基础上真的提交一次最小图片请求。

每一步都记录耗时，失败步骤带原因和取证目录，所以"卡在哪一步"是直接读出来的
而不是推断出来的。浏览器互斥由 browser_gate（宿主接 FX_CONTROL 队列）保证。
"""

import os
import socket
import time
from pathlib import Path

from ..config import AI_DIR, get_runtime_default_port, get_runtime_default_user_id
from ..ui_selectors import UI_SELECTORS, SELECTOR_VERSION
from ..utils.browser_gate import browser_slot
from ..utils.logger import log

# 探针检查的族里，这些属于"只在特定弹窗/面板打开后才存在"，页面静止时缺失是正常的，
# 不该报成选择器失效。探针会把它们标成 conditional 而不是 missing。
_CONDITIONAL_FAMILIES = {
    "media_picker_ready",
    "close_history_btn",
    "close_popup_btns",
    "credit_display",
    "menu_add_to_prompt",
    "upload_image_btn",
}


def probe_selectors(page, groups=None):
    """对 UI_SELECTORS 逐族逐层探测当前页面的命中情况。

    返回 {version, checked_at, url, families: [...]}，families 里每项：
      group/family/total_layers/hit_index/hit_selector/visible/state
    state: primary(主选择器命中) / fallback(靠兜底命中) / missing(全未命中)
           / conditional(条件性元素，未命中不算故障)
    """
    result = {
        "selector_version": SELECTOR_VERSION,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": "",
        "families": [],
    }
    try:
        result["url"] = page.url
    except Exception:
        pass

    for group, families in UI_SELECTORS.items():
        if groups and group not in groups:
            continue
        if not isinstance(families, dict):
            continue
        for family, selectors in families.items():
            if not isinstance(selectors, (list, tuple)):
                continue
            row = {
                "group": group,
                "family": family,
                "total_layers": len(selectors),
                "hit_index": -1,
                "hit_selector": "",
                "visible": False,
                "state": "missing",
            }
            for index, selector in enumerate(selectors):
                try:
                    locator = page.locator(selector)
                    count = locator.count()
                except Exception:
                    continue
                if not count:
                    continue
                row["hit_index"] = index
                row["hit_selector"] = str(selector)[:200]
                row["match_count"] = count
                try:
                    row["visible"] = bool(locator.first.is_visible(timeout=800))
                except Exception:
                    row["visible"] = False
                row["state"] = "primary" if index == 0 else "fallback"
                break
            if row["state"] == "missing" and family in _CONDITIONAL_FAMILIES:
                row["state"] = "conditional"
            result["families"].append(row)

    result["summary"] = {
        "total": len(result["families"]),
        "primary": sum(1 for r in result["families"] if r["state"] == "primary"),
        "fallback": sum(1 for r in result["families"] if r["state"] == "fallback"),
        "missing": sum(1 for r in result["families"] if r["state"] == "missing"),
        "conditional": sum(1 for r in result["families"] if r["state"] == "conditional"),
    }
    return result


class _Steps:
    """自检步骤记录器：每步都留耗时，失败即停并保留原因。"""

    def __init__(self):
        self.rows = []
        self._started = time.perf_counter()

    def run(self, name, fn, critical=True):
        started = time.perf_counter()
        row = {"step": name, "status": "ok", "ms": 0, "detail": ""}
        try:
            detail = fn()
            row["detail"] = detail if isinstance(detail, (str, int, float, dict, list)) else ""
        except Exception as e:
            row["status"] = "failed" if critical else "warn"
            row["detail"] = f"{type(e).__name__}: {e}"
        row["ms"] = round((time.perf_counter() - started) * 1000)
        self.rows.append(row)
        return row["status"] == "ok"

    def note(self, name, status, detail=""):
        self.rows.append({"step": name, "status": status, "ms": 0, "detail": detail})

    def result(self, level):
        failed = [r for r in self.rows if r["status"] == "failed"]
        return {
            "level": level,
            "status": "failed" if failed else "ok",
            "total_ms": round((time.perf_counter() - self._started) * 1000),
            "failed_step": failed[0]["step"] if failed else None,
            "steps": self.rows,
        }


def _selftest_l0(steps):
    def check_runtime():
        from .. import config as _cfg
        from . import google_fx_image, google_fx_video  # noqa: F401  真的 import 一遍
        return f"包路径 {_cfg.CORE_DIR}"

    def check_port():
        port = int(get_runtime_default_port() or 50325)
        started = time.perf_counter()
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        return f"AdsPower 本地 API {port} 可连接（{round((time.perf_counter() - started) * 1000)}ms）"

    def check_pool():
        from ..utils.account_pool import AccountPool
        accounts = AccountPool().list_accounts(heal=False)
        probed = sum(1 for a in accounts if a.get("credit_probed"))
        return f"号池 {len(accounts)} 个账号，其中 {probed} 个有真实积分探测记录"

    def check_writable():
        runtime = Path(AI_DIR) / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        probe = runtime / ".selftest_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return f"{runtime} 可写"

    def check_rotation():
        from ..utils.proxy_rotator import ProxyRotator
        rotator = ProxyRotator()
        if not rotator.is_configured:
            return "IP 轮换未配置（不会换 IP）"
        if not rotator.auto_rotate:
            return "IP 轮换已配置但自动轮换关闭"
        return (f"IP 轮换已配置，阈值 {rotator.rotate_threshold}"
                f"（≥100000 表示实际永不触发）")

    steps.run("运行时导入", check_runtime)
    steps.run("AdsPower 端口", check_port)
    steps.run("号池状态文件", check_pool)
    steps.run("runtime 目录可写", check_writable)
    steps.run("IP 轮换配置", check_rotation, critical=False)


def _selftest_browser(steps, level, user_id=None, cancel_check=None):
    """L1/L2 的浏览器部分。L2 会真的提交一次最小请求。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        steps.note("Playwright", "failed", "playwright 未安装，无法做 L1/L2 自检")
        return

    from ..utils.browser import get_ads_ws_url, find_or_create_page, ensure_flow_workspace
    from ..utils.forensics import capture
    from .google_fx_helpers import _find_fx_prompt_input, find_fx_config_button

    holder = {}

    def connect():
        ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
        holder["browser"] = holder["pw"].chromium.connect_over_cdp(ws_url, timeout=20000)
        return f"CDP 已连接 {ws_url}"

    def open_flow():
        context = holder["browser"].contexts[0]
        holder["page"] = find_or_create_page(
            context, "labs.google", fallback_url="https://labs.google/fx/tools/flow")
        ensure_flow_workspace(holder["page"])
        return holder["page"].url

    def locate_prompt():
        el = _find_fx_prompt_input(holder["page"], announce=False)
        if el is None:
            raise RuntimeError("找不到 prompt 输入框（选择器可能已失效）")
        return "prompt 输入框可定位"

    def locate_config_button():
        btn = find_fx_config_button(holder["page"])
        if not btn:
            raise RuntimeError("找不到底部配置按钮")
        return "配置按钮可定位"

    def run_selector_probe():
        probe = probe_selectors(holder["page"])
        holder["selectors"] = probe
        summary = probe["summary"]
        if summary["missing"]:
            raise RuntimeError(
                f"{summary['missing']} 个选择器族在当前页面完全未命中"
                f"（fallback {summary['fallback']} 个）")
        return summary

    def read_credit():
        from .google_fx_credit import _read_credit_from_account_menu
        credit = _read_credit_from_account_menu(holder["page"])
        if credit is None:
            raise RuntimeError("账号菜单读积分失败")
        return f"当前账号积分 {credit}"

    def submit_minimal():
        from ..models import ImageBatchRequest
        request = ImageBatchRequest(
            prompts=["a plain light grey background, minimal, test render"],
            user_id=user_id or get_runtime_default_user_id() or "",
        )
        from .google_fx_image import _generate_images_batch_google_fx_unlocked
        outcome = _generate_images_batch_google_fx_unlocked(request)
        status = (outcome or {}).get("status")
        if status == "failed":
            raise RuntimeError(f"最小提交失败: {(outcome or {}).get('message') or outcome}")
        return {"status": status, "results": len((outcome or {}).get("results") or [])}

    with sync_playwright() as pw:
        holder["pw"] = pw
        ok = steps.run("连接 AdsPower 浏览器", connect)
        if ok:
            ok = steps.run("打开 Flow 页面", open_flow)
        if ok:
            steps.run("定位 prompt 输入框", locate_prompt)
            steps.run("定位配置按钮", locate_config_button)
            steps.run("选择器探针", run_selector_probe)
            steps.run("读取账号积分", read_credit, critical=False)
            if level >= 2:
                steps.run("最小真实提交", submit_minimal)
        if not ok or any(r["status"] == "failed" for r in steps.rows):
            path = capture(holder.get("page"), "selftest", "自检存在失败步骤",
                           bucket="selftest")
            if path:
                steps.note("失败现场已保存", "warn", path)
    if holder.get("selectors"):
        steps.note("选择器探针摘要", "ok", holder["selectors"]["summary"])


def run_selftest(level=0, user_id=None, cancel_check=None):
    """跑一次分级自检。level: 0=只读 / 1=连浏览器不提交 / 2=真实最小提交。"""
    level = max(0, min(2, int(level)))
    steps = _Steps()
    log(f"🩺 开始 Google FX 自检（L{level}）", "自检")
    _selftest_l0(steps)
    if level >= 1 and not any(r["status"] == "failed" for r in steps.rows):
        # 浏览器部分排 FX_CONTROL 队列：自检不能和真任务抢同一个 profile。
        with browser_slot('selftest', cancel_check=cancel_check, priority=30,
                          task_id=f'selftest_l{level}_{int(time.time())}'):
            _selftest_browser(steps, level, user_id=user_id, cancel_check=cancel_check)
    elif level >= 1:
        steps.note("浏览器自检", "warn", "L0 已有失败步骤，跳过浏览器部分")
    outcome = steps.result(level)
    log(f"🩺 自检结束: {outcome['status']}"
        f"{'（失败于 ' + outcome['failed_step'] + '）' if outcome['failed_step'] else ''}", "自检")
    return outcome


def probe_selectors_live(user_id=None, cancel_check=None):
    """连一次浏览器只跑选择器探针（不提交、不改配置）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "error", "message": "playwright 未安装"}
    from ..utils.browser import get_ads_ws_url, find_or_create_page, ensure_flow_workspace

    with browser_slot('selector_probe', cancel_check=cancel_check, priority=35,
                      task_id=f'selector_probe_{int(time.time())}'):
        with sync_playwright() as pw:
            ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
            browser = pw.chromium.connect_over_cdp(ws_url, timeout=20000)
            page = find_or_create_page(browser.contexts[0], "labs.google",
                                       fallback_url="https://labs.google/fx/tools/flow")
            ensure_flow_workspace(page)
            probe = probe_selectors(page)
    probe["status"] = "ok"
    return probe


# ── Dry-run ────────────────────────────────────────────────────────────────
# FX_DRY_RUN=1：走完全部 DOM 定位、配置校验、参考图挂载，但**不点发送**。
# 这是在没有真账号额度、或者只想验证"UI 还认不认得我们"的时候唯一安全的验证方式，
# 也是让 agent 在离线环境里复现选择器类问题的开关。实现点在
# helpers.click_fx_send_button：那是所有链路唯一的提交动作出口。

def dry_run_enabled():
    return os.environ.get("FX_DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
