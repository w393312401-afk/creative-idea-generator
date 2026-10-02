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

import json
import os
import re
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
    "close_popup_btns",
    "credit_display",
}

# 静止的 Flow 项目页上必须存在的族。其余族属于落地页、配置/模型菜单、上传对话框、
# 生成结果卡片、错误态或 onboarding，仍然进探测报告，但当前页面状态下缺失属于
# 信息而非故障。
#
# 入选条件有两条，缺一不可：① 生产代码真的通过 UI_SELECTORS 读它；② 它在静止
# 工作台页上确实应该存在。历史上这里放过 create_btn / add_media_btn，前者已被删除
# （生产代码从不读它），后者由 _find_add2_btn 消费——这两条件必须同时核对，否则
# 探针会拿一个没人用的族去判定"服务是否可用"。
_REQUIRED_WORKSPACE_FAMILIES = {
    "prompt_input",
    "add_media_btn",
    "account_menu_trigger",
}

# 整组都是"只在别的域名上才存在"的族，探针跑在 Flow 工作台页上，它们必然全部
# 缺失——那是正常状态，不是选择器失效。
#
# google_login：accounts.google.com 的登录表单（utils/auto_login.py 用）。
# 这组的健康度没法靠静止页探针判断，只能看 auto_login 的真实运行结果
# （号池行上的"上次自动登录"状态）。不豁免的话，探针从此永远报 8 个 missing，
# "探针全绿"这个信号就废了。
_CONTEXTUAL_GROUPS = {
    "google_login",
}

# 这些族存的是文本关键词（模型名、aria-label 文案），不是 CSS 选择器。
# 交给 page.locator() 会被当成元素名解析（locator("Banana") → <banana>），
# 永远 count=0，于是每次探针都白报一条"全 N 层均未命中"。直接跳过，不进报告。
# 注意不能用"是否含 CSS 语法字符"自动判别：prompt_input 的首层就是裸 "textarea"。
_NON_SELECTOR_FAMILIES = {
    "config_btn_keywords",
}

# ⚠️ 探针的覆盖边界（别把"探针全绿"当成"选择器全好"）
#
# 探针只覆盖 UI_SELECTORS 里的扁平选择器列表。helpers 里还有几条定位逻辑不是
# 选择器列表，而是"选择器 + 运行期过滤"的算法，无法用 count() 探测：
#
#   fx_tab                —— 层是 [role='tab'] / button，靠 .filter(has_text=) 和
#                            aria-controls/data-state 的文本 blob 二次判定
#   fx_model_dropdown     —— 兜底层含裸 button，靠 _looks_like_model_button() 判定
#   fx_config_panel       —— 靠 aria-labelledby / aria-controls 关联关系定位
#   fx_menu_item          —— 选择器由运行期 label 拼成 f-string
#   fx_orientation_option —— 同上，按 pattern 拼
#
# 把这些塞进 UI_SELECTORS 让探针 count() 一遍是**有害的**：裸 button 永远命中，
# 探针会报绿而实际定位早就失败了。它们的健康度只能看 selector_stats 的运行期
# 命中层级（族名就是上面这几个），那是真实任务跑出来的数据。
# add_media_btn 是唯一一张能统一的扁平表，已经统一：helpers._find_add2_btn 直接读
# UI_SELECTORS，统计族名也对齐成 add_media_btn，所以探针和运行期统计说的是同一件事。


def _open_account_menu(page):
    from .google_fx_credit import _try_click_once, _account_menu_is_open, _wait_for_account_menu

    if _account_menu_is_open(page):
        return True
    triggers = UI_SELECTORS.get("google_fx", {}).get("account_menu_trigger", [])
    if not _try_click_once(page, triggers):
        raise RuntimeError("账号菜单触发器点不动（account_menu_trigger 可能已失效）")
    if not _wait_for_account_menu(page):
        raise RuntimeError("点了账号菜单触发器但菜单没打开")
    return True


def _open_config_panel(page):
    from .google_fx_helpers import find_fx_config_button

    # find_fx_config_button() returns (locator, status_text).  The deep probe
    # only needs the locator; treating the pair itself as a locator used to
    # fail every run with: AttributeError: 'tuple' object has no attribute
    # 'click'.
    btn, _status_text = find_fx_config_button(page)
    if not btn:
        raise RuntimeError("找不到底部配置按钮，无法打开配置面板")
    btn.click()
    time.sleep(0.8)
    return True


# deep 模式的场景表：每项 = (场景名, 打开动作, 该场景下才存在的目标族)。
# 只收录**一键可达且非破坏性**的场景。故意不收：
#   - flow_entry_btn / flow_onboarding_* —— 只在未初始化账号上出现，已初始化的
#     账号物理上无法复现这个状态，导航不过去。
#   - 裁剪对话框 / 下载按钮 —— 必须真的传图或真的跑完一次生成才会出现，那是 L2
#     自检的职责，且要烧积分。
_DEEP_PROBE_SCENARIOS = (
    ("账号菜单", _open_account_menu, ("account_menu_surface", "credit_display")),
    ("配置面板", _open_config_panel, ("config_panel_root",)),
)


def _run_deep_probe(page, rows):
    """依次打开各场景，就地重探该场景下的目标族，覆盖静态那一遍的结论。

    每个场景独立 try/收尾：一个场景打不开不影响后面的场景，收尾一律按 Escape，
    避免把页面留在弹窗打开的状态上。
    """
    from .google_fx_dom import _safe_press_escape

    by_family = {row["family"]: row for row in rows}
    report = []
    fx = UI_SELECTORS.get("google_fx", {})

    for name, opener, targets in _DEEP_PROBE_SCENARIOS:
        entry = {"scenario": name, "opened": False, "families": [], "error": ""}
        try:
            opener(page)
            entry["opened"] = True
            for family in targets:
                selectors = fx.get(family)
                if not selectors:
                    continue
                row = by_family.get(family)
                if row is None:
                    continue
                fresh = _probe_family(page, "google_fx", family, selectors)
                # 场景已经打开了，这时候还未命中就是真失效，不再降级成 conditional。
                row.update(fresh)
                row["probed_via"] = name
                entry["families"].append({"family": family, "state": row["state"]})
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                _safe_press_escape(page, f"deep_probe:{name}")
            except Exception:
                pass
        report.append(entry)

    return report


def _probe_family(page, group, family, selectors):
    """单族逐层探测，返回未分类的 row（state 只有 primary/fallback/missing）。"""
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
    return row


def _classify_missing(row):
    """把"当前页面状态下本就不存在"的未命中降级成 conditional。"""
    if row["state"] != "missing":
        return row
    group, family = row["group"], row["family"]
    is_contextual = family in _CONDITIONAL_FAMILIES
    if group == "google_fx" and family not in _REQUIRED_WORKSPACE_FAMILIES:
        is_contextual = True
    if group == "common" or group in _CONTEXTUAL_GROUPS:
        is_contextual = True
    if is_contextual:
        row["state"] = "conditional"
    return row


def _summarize(families):
    return {
        "total": len(families),
        "primary": sum(1 for r in families if r["state"] == "primary"),
        "fallback": sum(1 for r in families if r["state"] == "fallback"),
        "missing": sum(1 for r in families if r["state"] == "missing"),
        "conditional": sum(1 for r in families if r["state"] == "conditional"),
    }


def _collect_alerts(families):
    """把"必需族没走主选择器"挑出来，作为**改版预警**返回。

    2026-09-05 复盘：Flow 改版当天，add_media_btn 已经掉到第 2 层兜底
    (`button[aria-label='Add media menu']`)，探针数据里明明白白写着，但结果只有
    一份 primary/fallback 计数摘要，没有任何一条"这个族出事了"的显式结论，
    于是没人注意到。兜底命中就是改版的**前兆**——主选择器已经死了，只是恰好还有
    一层接住；下一次改版把兜底也带走时就是整链失败。这里让它自己喊出来。
    """
    alerts = []
    for row in families:
        if row.get("group") != "google_fx":
            continue
        if row.get("family") not in _REQUIRED_WORKSPACE_FAMILIES:
            continue
        state = row.get("state")
        if state == "missing":
            alerts.append({
                "family": row["family"], "level": "critical", "state": state,
                "message": f"必需选择器族 {row['family']} 全部 {row['total_layers']} 层均未命中，"
                           f"该功能在当前 Flow UI 上已不可用",
            })
        elif state == "fallback":
            alerts.append({
                "family": row["family"], "level": "warning", "state": state,
                "hit_index": row.get("hit_index"),
                "message": f"必需选择器族 {row['family']} 靠第 {row.get('hit_index', 0) + 1} 层兜底命中"
                           f"（{row.get('hit_selector', '')}），主选择器已失效，疑似 Flow 改版",
            })
    return alerts


def probe_selectors(page, groups=None, deep=False):
    """对 UI_SELECTORS 逐族逐层探测当前页面的命中情况。

    返回 {version, checked_at, url, families: [...]}，families 里每项：
      group/family/total_layers/hit_index/hit_selector/visible/state
    state: primary(主选择器命中) / fallback(靠兜底命中) / missing(全未命中)
           / conditional(条件性元素，未命中不算故障)

    deep=False（默认）是**只读**的：只 count/is_visible，不点击任何东西，所以
    可以随时对生产页面跑。deep=True 会额外打开账号菜单和配置面板去验证弹窗态的
    族（见 _DEEP_PROBE_SCENARIOS），会真的点击页面并按 Escape 收尾——这是有副作用
    的，必须由调用方显式要求。
    """
    result = {
        "selector_version": SELECTOR_VERSION,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": "",
        "families": [],
        "deep": bool(deep),
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
            if family in _NON_SELECTOR_FAMILIES:
                continue
            result["families"].append(
                _classify_missing(_probe_family(page, group, family, selectors)))

    if deep:
        result["deep_scenarios"] = _run_deep_probe(page, result["families"])

    result["summary"] = _summarize(result["families"])
    result["alerts"] = _collect_alerts(result["families"])
    for alert in result["alerts"]:
        log(("🚨 " if alert["level"] == "critical" else "⚠️ ") + alert["message"],
            "选择器探针")
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


def _ensure_flow_project_open(page, toolbar_timeout=30):
    """Make the Flow prompt workspace available for browser diagnostics.

    The Flow landing page is a valid, fully loaded page, but it intentionally has
    no prompt editor.  L1 used to probe that landing page directly and therefore
    reported a selector failure on fresh accounts.  Enter an existing project
    when one is already open; otherwise create the project requested by the
    landing page, then wait for its toolbar before probing selectors.
    """
    from .google_fx_helpers import (
        _click_new_project_button,
        _find_fx_prompt_input,
        _wait_for_fx_toolbar,
    )

    try:
        in_project = "/project/" in str(page.url or "")
    except Exception:
        in_project = False

    # Flow may restore the browser directly into a usable generation workspace
    # whose current route does not contain /project/.  In that state there is a
    # visible prompt editor and intentionally no "New project" button.  Treat
    # the actual workspace UI as authoritative; use the URL only as a loading
    # hint for a conventional project route.
    workspace_visible = _find_fx_prompt_input(page, announce=False) is not None
    if not in_project and not workspace_visible and not _click_new_project_button(page):
        raise RuntimeError(
            "当前 Flow 页面既未检测到可见的提示词输入框，"
            "也没有可用的新建项目入口")

    _wait_for_fx_toolbar(page, timeout=toolbar_timeout)
    return page.url


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
        # 代理来源要说清楚：现在它可能来自控制台的代理号池，也可能来自 MIYA_* 环境
        # 变量。只说"已配置"的话，用户在两个地方都要翻一遍才知道用的是哪份。
        source = "代理号池" if ProxyRotator._pool_has_usable_proxy() else "MIYA_* 环境变量"
        if not rotator.is_configured:
            return "IP 轮换未配置（代理号池为空且 MIYA_PROXY_HOST/PORT 未设，不会换 IP）"
        if not rotator.auto_rotate:
            return f"代理已配置（来源：{source}）但自动轮换关闭"
        return (f"代理已配置（来源：{source}），轮换阈值 {rotator.rotate_threshold}"
                f"（≥100000 表示实际永不触发）")

    steps.run("运行时导入", check_runtime)
    steps.run("AdsPower 端口", check_port)
    steps.run("号池状态文件", check_pool)
    steps.run("runtime 目录可写", check_writable)
    steps.run("IP 轮换配置", check_rotation, critical=False)


def _submit_minimal_image(user_id=None):
    """Run the destructive L2 probe after the diagnostics browser has closed."""
    from ..models import ImageBatchRequest
    from ..utils.account_binding import bound_task_account
    from .google_fx_image import _generate_images_batch_google_fx_unlocked

    # BrowserEnvLockedRequest intentionally rejects user_id/port in request
    # bodies. Bind the selected diagnostics account through per-task context.
    request = ImageBatchRequest(
        prompts=["a plain light grey background, minimal, test render"],
    )
    with bound_task_account(user_id):
        outcome = _generate_images_batch_google_fx_unlocked(request)

    status = (outcome or {}).get("status")
    image_urls = (outcome or {}).get("image_urls") or []
    if status != "success" or not image_urls:
        raise RuntimeError(f"最小提交失败: {(outcome or {}).get('message') or outcome}")
    return {"status": status, "results": len(image_urls)}


def probe_image_config(page, scope=None):
    """L1自测：检测图片模式选项"""
    from .google_fx_helpers import find_fx_config_button
    scope = scope or page
    selectors = [
        "button.flow_tab_slider_trigger[aria-controls$='-IMAGE']",
        "button[role='tab'][aria-controls$='-IMAGE']",
        "button[role='tab']:has-text('Image')",
        "button[role='tab']:has-text('图片')",
        "button:has-text('Image')",
        "button:has-text('图片')",
    ]
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.is_visible(timeout=600):
                return f"图片模式选项可定位 ({sel})"
        except Exception:
            continue

    _, status_text = find_fx_config_button(page)
    if status_text:
        st_lower = status_text.lower()
        if any(kw in st_lower for kw in ["image", "图片", "nano", "imagen"]):
            return f"图片模式状态存在 ({status_text})"
    raise RuntimeError("未定位到图片模式选项")


def probe_video_config(page, scope=None):
    """L1自测：检测视频模式选项"""
    from .google_fx_helpers import find_fx_config_button
    scope = scope or page
    selectors = [
        "button.flow_tab_slider_trigger[aria-controls$='-VIDEO']",
        "button[role='tab'][aria-controls$='-VIDEO']",
        "button[role='tab']:has-text('Video')",
        "button[role='tab']:has-text('视频')",
        "button:has-text('Video')",
        "button:has-text('视频')",
    ]
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.is_visible(timeout=600):
                return f"视频模式选项可定位 ({sel})"
        except Exception:
            continue

    _, status_text = find_fx_config_button(page)
    if status_text:
        st_lower = status_text.lower()
        if any(kw in st_lower for kw in ["video", "视频", "veo"]):
            return f"视频模式状态存在 ({status_text})"
    raise RuntimeError("未定位到视频模式选项")


def probe_count_config(page, scope=None):
    """L1自测：检测数量配置选项 (1x/2x/4x)"""
    scope = scope or page
    matched = []
    count_tabs = UI_SELECTORS.get("google_fx", {}).get("count_tab", {})
    for key, sel in count_tabs.items():
        try:
            loc = scope.locator(sel).first
            if loc.is_visible(timeout=400):
                matched.append(key)
        except Exception:
            continue

    if not matched:
        for val in ["1x", "2x", "3x", "4x", "x1", "x2", "x3", "x4", "1", "2", "4"]:
            try:
                loc = scope.locator("button[role='tab'], button[role='radio'], .mat-button-toggle-button, mat-button-toggle, flow-toggles[aria-label='Output count'] button").filter(
                    has_text=re.compile(rf"^\s*{re.escape(val)}\s*$", re.I)
                ).first
                if loc.is_visible(timeout=300):
                    matched.append(val)
            except Exception:
                continue

    if not matched:
        try:
            count_toggles = scope.locator("flow-toggles[aria-label='Output count'], [aria-label='Output count']").first
            if count_toggles.is_visible(timeout=300):
                matched.append("flow-toggles:Output count")
        except Exception:
            pass

    if matched:
        return f"数量配置可定位 (命中: {', '.join(matched)})"
    raise RuntimeError("未定位到数量配置选项 (1x/2x/4x)")


def probe_duration_config(page, scope=None):
    """L1自测：检测时长配置选项 (4s/5s/6s/8s/10s)"""
    scope = scope or page
    matched = []
    durations = ["4s", "5s", "6s", "8s", "10s"]
    for dur in durations:
        try:
            loc = scope.locator("button[role='tab']").filter(
                has_text=re.compile(rf"^\s*{dur}\s*$", re.I)
            ).first
            if loc.is_visible(timeout=300):
                matched.append(dur)
        except Exception:
            continue

    if not matched:
        try:
            dur_locs = scope.locator("button[aria-controls*='DURATION'], button[aria-controls*='duration']").all()
            if dur_locs:
                matched.append("aria-controls:duration")
        except Exception:
            pass

    if matched:
        return f"时长配置可定位 (命中: {', '.join(matched)})"
    return "时长配置检测通过 (视当前模型面板动态暴露)"


def probe_orientation_config(page, scope=None):
    """L1自测：检测比例配置选项 (9:16/16:9/1:1/3:4/4:3)"""
    scope = scope or page
    matched = []
    ratio_tabs = UI_SELECTORS.get("google_fx", {}).get("ratio_tab", {})
    for ratio_name, sel in ratio_tabs.items():
        try:
            loc = scope.locator(sel).first
            if loc.is_visible(timeout=400):
                matched.append(ratio_name)
        except Exception:
            continue

    if not matched:
        for orient in ["9:16", "16:9", "1:1", "3:4", "4:3", "PORTRAIT", "LANDSCAPE", "SQUARE"]:
            try:
                loc = scope.locator(f"button:has-text('{orient}'), button[aria-label*='{orient}']").first
                if loc.is_visible(timeout=300):
                    matched.append(orient)
            except Exception:
                continue

    if matched:
        return f"比例配置可定位 (命中: {', '.join(matched)})"
    raise RuntimeError("未定位到比例/方向配置选项 (9:16/16:9/1:1等)")


def probe_ref_mode_config(page, scope=None):
    """L1自测：检测参考模式/视频子模式选项 (帧/素材/VIDEO_FRAMES/VIDEO_REFERENCES)"""
    scope = scope or page
    matched = []
    submode_selectors = [
        "button[aria-controls$='-VIDEO_FRAMES']",
        "button[aria-controls$='-VIDEO_REFERENCES']",
        "button[role='tab']:has-text('帧')",
        "button[role='tab']:has-text('素材')",
        "button[role='tab']:has-text('Frames')",
        "button[role='tab']:has-text('References')",
        "button[role='tab']:has-text('Ingest')",
        "button[role='tab']:has-text('Controls')",
        "button[role='tab']:has-text('Style')",
        "button[role='tab']:has-text('Ref')",
    ]
    for sel in submode_selectors:
        try:
            loc = scope.locator(sel).first
            if loc.is_visible(timeout=300):
                matched.append(sel.split("'")[-2] if "'" in sel else sel)
        except Exception:
            continue

    if matched:
        return f"参考模式选项可定位 (命中: {', '.join(matched)})"

    try:
        ref_btn = page.locator("button:has-text('Reference'), button:has-text('参考'), button[aria-label*='Reference']").first
        if ref_btn.is_visible(timeout=300):
            return "参考模式入口正常 (Reference 按钮)"
    except Exception:
        pass
    return "参考模式检测通过 (机制链路完好)"


def probe_upload_config(page):
    """L1自测：检测上传配置入口 (add_media_btn / file input / 上传槽位)"""
    from .google_fx_helpers import _find_add2_btn
    matched = []

    add_btn = _find_add2_btn(page)
    if add_btn:
        matched.append("add_media_btn(+)")

    try:
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            matched.append(f"file_input(x{file_inputs.count()})")
    except Exception:
        pass

    upload_selectors = [
        "button[aria-label*='Upload']",
        "button:has-text('Upload')",
        "button:has-text('上传')",
        "button.empty-chip:has-text('Start')",
        "button.empty-chip:has-text('End')",
        "button:has-text('Start')",
        "button:has-text('End')",
        "div:has-text('Start frame')",
        "div:has-text('End frame')",
        "div:has-text('首帧')",
        "div:has-text('尾帧')",
        "[data-testid='reference-chip']",
    ]
    for sel in upload_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=300):
                label = sel.split("'")[1] if "'" in sel else sel
                matched.append(label)
                break
        except Exception:
            continue

    if matched:
        return f"上传配置入口正常 (已匹配: {', '.join(matched)})"
    raise RuntimeError("未定位到上传配置入口 (add_media_btn / file input)")


def _selftest_browser(steps, level, user_id=None, cancel_check=None):
    """L1/L2 的浏览器部分。L2 会真的提交一次最小请求。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        steps.note("Playwright", "failed", "playwright 未安装，无法做 L1/L2 自检")
        return

    from ..utils.browser import get_ads_ws_url, find_or_create_page, ensure_flow_workspace
    from ..utils.forensics import capture
    from .google_fx_helpers import _find_fx_prompt_input, find_fx_config_button, _get_open_fx_config_panel, FLOW_HOST_HINTS
    from .google_fx_dom import _safe_press_escape

    holder = {}

    def connect():
        ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
        holder["browser"] = holder["pw"].chromium.connect_over_cdp(ws_url, timeout=20000)
        return f"CDP 已连接 {ws_url}"

    def open_flow():
        context = holder["browser"].contexts[0]
        holder["page"] = find_or_create_page(
            context, FLOW_HOST_HINTS, fallback_url="https://flow.google.com/",
            user_id=user_id, cancel_check=cancel_check, context_label="Google FX 自检浏览器启动")
        ensure_flow_workspace(holder["page"], user_id=user_id)
        return holder["page"].url

    def enter_project():
        return _ensure_flow_project_open(holder["page"])

    def locate_prompt():
        el = _find_fx_prompt_input(holder["page"], announce=False)
        if el is None:
            raise RuntimeError("找不到 prompt 输入框（选择器可能已失效）")
        return "prompt 输入框可定位"

    def locate_config_button():
        btn, _status_text = find_fx_config_button(holder["page"])
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

    with sync_playwright() as pw:
        holder["pw"] = pw
        ok = steps.run("连接 AdsPower 浏览器", connect)
        if ok:
            ok = steps.run("打开 Flow 页面", open_flow)
        if ok:
            ok = steps.run("进入 Flow 项目", enter_project)
        if ok:
            steps.run("定位 prompt 输入框", locate_prompt)
            steps.run("定位配置按钮", locate_config_button)

            # 打开配置面板获取 scope 以检测图片、视频、数量、时长、比例、参考模式
            panel_scope = None
            try:
                btn, _ = find_fx_config_button(holder["page"])
                if btn:
                    panel_scope = _get_open_fx_config_panel(holder["page"], btn)
                    if not panel_scope or not panel_scope.is_visible():
                        btn.click()
                        holder["page"].wait_for_timeout(1200)
                        panel_scope = _get_open_fx_config_panel(holder["page"], btn)
            except Exception:
                pass

            p_scope = panel_scope or holder["page"]

            steps.run("检测图片配置", lambda: probe_image_config(holder["page"], scope=p_scope))
            steps.run("检测视频配置", lambda: probe_video_config(holder["page"], scope=p_scope))
            steps.run("检测数量配置", lambda: probe_count_config(holder["page"], scope=p_scope))
            steps.run("检测时长配置", lambda: probe_duration_config(holder["page"], scope=p_scope))
            steps.run("检测比例配置", lambda: probe_orientation_config(holder["page"], scope=p_scope))
            steps.run("检测参考模式", lambda: probe_ref_mode_config(holder["page"], scope=p_scope))

            # 探测完毕后收起配置面板
            try:
                _safe_press_escape(holder["page"], "selftest_config")
            except Exception:
                pass

            steps.run("检测上传配置", lambda: probe_upload_config(holder["page"]))
            steps.run("选择器探针", run_selector_probe)
            steps.run("读取账号积分", read_credit, critical=False)
        if not ok or any(r["status"] == "failed" for r in steps.rows):
            path = capture(holder.get("page"), "selftest", "自检存在失败步骤",
                           bucket="selftest")
            if path:
                steps.note("失败现场已保存", "warn", path)

    # The production generator owns its own sync_playwright() lifecycle.  Run
    # it only after the read-only diagnostics connection above has closed;
    # nesting two sync Playwright managers in one thread is unsupported.
    if level >= 2 and not any(r["status"] == "failed" for r in steps.rows):
        steps.run("最小真实提交", lambda: _submit_minimal_image(user_id))
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
                          task_id=f'selftest_l{level}_{int(time.time())}', user_id=user_id):
            _selftest_browser(steps, level, user_id=user_id, cancel_check=cancel_check)
    elif level >= 1:
        steps.note("浏览器自检", "warn", "L0 已有失败步骤，跳过浏览器部分")
    outcome = steps.result(level)
    log(f"🩺 自检结束: {outcome['status']}"
        f"{'（失败于 ' + outcome['failed_step'] + '）' if outcome['failed_step'] else ''}", "自检")
    return outcome


def probe_selectors_live(user_id=None, cancel_check=None, deep=False):
    """连一次浏览器只跑选择器探针（不提交、不改配置）。

    deep=True 会额外点开账号菜单和配置面板来验证弹窗态的族，收尾按 Escape。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "error", "message": "playwright 未安装"}
    from ..utils.browser import get_ads_ws_url, find_or_create_page, ensure_flow_workspace
    from .google_fx_helpers import FLOW_HOST_HINTS

    with browser_slot('selector_probe', cancel_check=cancel_check, priority=35,
                      task_id=f'selector_probe_{int(time.time())}', user_id=user_id):
        with sync_playwright() as pw:
            ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
            browser = pw.chromium.connect_over_cdp(ws_url, timeout=20000)
            page = find_or_create_page(browser.contexts[0], FLOW_HOST_HINTS,
                                       fallback_url="https://flow.google.com/",
                                       user_id=user_id, cancel_check=cancel_check,
                                       context_label="选择器探针浏览器启动")
            ensure_flow_workspace(page, user_id=user_id)
            if deep:
                _ensure_flow_project_open(page)
            probe = probe_selectors(page, deep=deep)
    probe["status"] = "ok"
    return probe


# ── 画布卡片结构取证 ──────────────────────────────────────────────────────
# 2026-09-06：视频在 Flow 上明明生成完了，代码却一直判成"还在生成"，直到单任务
# 300s 超时。原因是提交之后就把卡片跟丢了——Angular 把"加载中占位"整块换成结果
# 卡片时销毁重建了节点，我们盖的 data-spark-tile-id 随之消失，两条兜底（按首尾帧
# UUID 反查、按提示词文本匹配）在新版结果卡上都不成立。
#
# 要修就必须先看清楚"新版结果卡片到底长什么样"：它带不带稳定 id？带不带首尾帧的
# img？显不显示提示词？完成后的视频是 <video src> 还是 blob/MSE？
# 这个 dump 就是回答这几个问题用的，**只读**：不点击、不导航、不改配置。
_CANVAS_TILES_JS = r"""({ maxTiles, textLimit }) => {
    const SEL = 'flow-grid-tile-container, div[data-tile-id], flow-tile-container, flow-image-tile';
    const cut = (v, n) => {
        const s = String(v == null ? '' : v);
        return s.length > n ? s.slice(0, n) + '…' : s;
    };
    const attrsOf = (el) => {
        const out = {};
        for (const a of Array.from(el.attributes || [])) out[a.name] = cut(a.value, 160);
        return out;
    };
    // 卡片内部的元素骨架：只保留标签 + 关键属性，不含文本与样式。
    const skeleton = (root, maxNodes) => {
        const rows = [];
        const KEEP = /^(id|class|role|type|href|src|alt|title|poster|download|controls)$/;
        const walk = (el, depth) => {
            if (rows.length >= maxNodes) return;
            const parts = [];
            for (const a of Array.from(el.attributes || [])) {
                if (KEEP.test(a.name) || a.name.startsWith('data-') || a.name.startsWith('aria-')) {
                    parts.push(a.name + '=' + JSON.stringify(cut(a.value, 100)));
                }
            }
            rows.push('  '.repeat(Math.min(depth, 20)) + el.tagName.toLowerCase()
                      + (parts.length ? ' ' + parts.join(' ') : ''));
            for (const c of Array.from(el.children)) walk(c, depth + 1);
        };
        walk(root, 0);
        return rows.join('\n');
    };

    const all = Array.from(document.querySelectorAll(SEL))
        .filter((el) => !el.parentElement || !el.parentElement.closest(SEL));
    const tiles = all.slice(0, maxTiles).map((el, i) => {
        const rect = el.getBoundingClientRect();
        return {
            index: i,
            tag: el.tagName.toLowerCase(),
            attrs: attrsOf(el),
            rect: { w: Math.round(rect.width), h: Math.round(rect.height),
                    top: Math.round(rect.top), left: Math.round(rect.left) },
            innerText: cut((el.innerText || '').replace(/\s+/g, ' '), textLimit),
            imgs: Array.from(el.querySelectorAll('img')).slice(0, 8).map((img) => ({
                src: cut(img.currentSrc || img.src || img.getAttribute('src'), 240),
                mediaId: img.getAttribute('data-media-id') || null,
                alt: cut(img.getAttribute('alt'), 80),
                w: img.offsetWidth, h: img.offsetHeight,
            })),
            videos: Array.from(el.querySelectorAll('video')).slice(0, 4).map((v) => ({
                src: cut(v.getAttribute('src'), 240),
                currentSrc: cut(v.currentSrc, 240),
                poster: cut(v.getAttribute('poster'), 240),
                readyState: v.readyState,
                duration: (isFinite(v.duration) ? v.duration : null),
                sources: Array.from(v.querySelectorAll('source')).map(
                    (s) => cut(s.getAttribute('src') || s.src, 240)),
            })),
            anchors: Array.from(el.querySelectorAll('a[href]')).slice(0, 6).map(
                (a) => cut(a.getAttribute('href'), 240)),
            // 所有 data-* / aria-* 属性（含后代）：找"稳定 id"就靠这张表
            dataAttrs: (() => {
                const seen = {};
                for (const node of [el, ...Array.from(el.querySelectorAll('*')).slice(0, 400)]) {
                    for (const a of Array.from(node.attributes || [])) {
                        if (a.name.startsWith('data-') || a.name.startsWith('aria-')) {
                            if (!(a.name in seen)) seen[a.name] = cut(a.value, 120);
                        }
                    }
                }
                return seen;
            })(),
            buttons: Array.from(el.querySelectorAll('button')).slice(0, 12).map((b) => ({
                label: cut(b.getAttribute('aria-label') || (b.innerText || '').trim(), 60),
                haspopup: b.getAttribute('aria-haspopup') || null,
            })),
            skeleton: skeleton(el, 120),
        };
    });

    return {
        url: location.href,
        at: new Date().toISOString(),
        counts: {
            'flow-grid-tile-container': document.querySelectorAll('flow-grid-tile-container').length,
            'div[data-tile-id]': document.querySelectorAll('div[data-tile-id]').length,
            'flow-tile-container': document.querySelectorAll('flow-tile-container').length,
            'flow-image-tile': document.querySelectorAll('flow-image-tile').length,
            'video': document.querySelectorAll('video').length,
            'img[data-media-id]': document.querySelectorAll('img[data-media-id]').length,
            'outermost-tiles': all.length,
        },
        tiles: tiles,
        truncated: all.length > tiles.length,
    };
}"""


def dump_canvas_tiles(page, max_tiles=12, text_limit=400):
    """只读 dump 画布上每张卡片的结构。返回 dict；失败时返回 {'error': ...}。"""
    try:
        return page.evaluate(_CANVAS_TILES_JS,
                             {"maxTiles": int(max_tiles), "textLimit": int(text_limit)})
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def canvas_tiles_collector(max_tiles=12, text_limit=400):
    """给 forensics.capture(collectors=...) 用的 collector。"""
    def _collect(page):
        return dump_canvas_tiles(page, max_tiles=max_tiles, text_limit=text_limit)
    return _collect


def capture_canvas_snapshot(user_id=None, cancel_check=None, bucket="canvas_snapshot",
                            why="手动取证：画布卡片结构"):
    """连一次浏览器，把**当前画布**原样存一份现场。只读。

    刻意不调 find_or_create_page / ensure_flow_workspace：那两个会导航、会关掉
    多余标签页、崩溃页还会点"新建项目"——而这次取证的全部价值就在于画布上那批
    已经生成好的视频，把画布弄没了就白跑了。找不到 Flow 标签页时如实报错，
    让人先把画布打开。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "error", "message": "playwright 未安装"}
    from ..utils.browser import get_ads_ws_url, is_flow_url

    with browser_slot('canvas_snapshot', cancel_check=cancel_check, priority=35,
                      task_id=f'canvas_snapshot_{int(time.time())}', user_id=user_id):
        with sync_playwright() as pw:
            ws_url = get_ads_ws_url(user_id=user_id, auto_rotate_proxy=False)
            browser = pw.chromium.connect_over_cdp(ws_url, timeout=20000)
            pages = []
            for ctx in browser.contexts:
                pages.extend(getattr(ctx, "pages", []) or [])
            target = None
            for pg in reversed(pages):
                try:
                    if is_flow_url(pg.url):
                        target = pg
                        break
                except Exception:
                    continue
            if target is None:
                return {"status": "error",
                        "message": "浏览器里没有打开着的 Flow 标签页——请先在 AdsPower 里"
                                   "打开那张有已生成视频的画布，再重跑一次取证"}
            from ..utils import forensics
            slot = forensics.capture(
                target, "canvas_snapshot", why, bucket=bucket,
                collectors={"canvas_tiles.json": canvas_tiles_collector()},
            )
            summary = dump_canvas_tiles(target, max_tiles=1)
    return {
        "status": "ok",
        "slot": slot,
        "url": summary.get("url"),
        "counts": summary.get("counts"),
        "message": f"现场已保存到 {slot}；重点看 canvas_tiles.json 与 dom_skeleton.txt",
    }


# ── 选择器基线与漂移巡检 ──────────────────────────────────────────────────
# 2026-09-05 复盘的第 3 条：探针和自检都只有手动 HTTP 入口，没有任何定时触发。
# 于是 Flow 改版这件事只能靠"下一单真任务失败 + 烧掉一批积分"来通知我们。
# 这里补上：定期跑一次只读探针，和上一次的结果**逐族对比**，命中层级往后退、
# 或者从命中变成未命中，都当场喊出来。整个过程不提交、不花积分。

def selector_baseline_path():
    override = os.environ.get("GOOGLE_FX_SELECTOR_BASELINE_FILE", "").strip()
    if override:
        return override
    return str(AI_DIR / "runtime" / "selector_baseline.json")


def _families_by_key(probe):
    rows = {}
    for row in (probe or {}).get("families", []) or []:
        rows[f"{row.get('group')}/{row.get('family')}"] = row
    return rows


def load_selector_baseline():
    try:
        with open(selector_baseline_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_selector_baseline(probe):
    """把这次探针结果存成新基线。写失败只记日志，绝不影响调用方。"""
    path = selector_baseline_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(probe, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log(f"⚠️ 写选择器基线失败: {type(e).__name__}: {e}", "选择器巡检")
        return False


# 状态的"健康度"排序：越大越糟。用来判断这次比上次是变好还是变坏。
_STATE_RANK = {"primary": 0, "fallback": 1, "conditional": 2, "missing": 3}


def diff_selector_baseline(probe, baseline=None):
    """把本次探针结果和基线逐族对比，返回**变坏了**的族。

    只报退化（primary→fallback、fallback→missing、命中层级往后退），不报好转——
    好转不需要人管，混在一起只会稀释信号。
    """
    baseline = baseline if baseline is not None else load_selector_baseline()
    if not baseline:
        return []
    old_rows = _families_by_key(baseline)
    changes = []
    for key, row in _families_by_key(probe).items():
        old = old_rows.get(key)
        if not old:
            continue
        old_state, new_state = old.get("state"), row.get("state")
        old_rank = _STATE_RANK.get(old_state, 9)
        new_rank = _STATE_RANK.get(new_state, 9)
        old_index, new_index = old.get("hit_index", -1), row.get("hit_index", -1)
        degraded_state = new_rank > old_rank
        # 命中层级往后退 = 主选择器死了、靠更后面的兜底接住，是改版的前兆
        degraded_layer = (old_index >= 0 and new_index > old_index)
        if not (degraded_state or degraded_layer):
            continue
        changes.append({
            "family": key,
            "was": {"state": old_state, "hit_index": old_index,
                    "hit_selector": old.get("hit_selector", "")},
            "now": {"state": new_state, "hit_index": new_index,
                    "hit_selector": row.get("hit_selector", "")},
            "message": (f"{key}: {old_state}(第 {old_index + 1} 层) → "
                        f"{new_state}(第 {new_index + 1} 层)"
                        if new_index >= 0 else
                        f"{key}: {old_state}(第 {old_index + 1} 层) → {new_state}(全未命中)"),
        })
    return changes


def run_selector_drift_check(user_id=None, cancel_check=None, save=True):
    """跑一次只读探针 → 和基线对比 → 记日志 → 更新基线。返回结构化结果。

    定时巡检和手动排查都走这里。不提交、不改配置、不花积分，唯一成本是占用
    浏览器槽位几十秒，所以调用方应挑 FX 空闲的时候调。
    """
    probe = probe_selectors_live(user_id=user_id, cancel_check=cancel_check)
    if probe.get("status") != "ok":
        return {"status": "error", "message": probe.get("message", "探针未成功"),
                "probe": probe}
    baseline = load_selector_baseline()
    changes = diff_selector_baseline(probe, baseline)
    alerts = probe.get("alerts") or []

    if baseline is None:
        log("📌 选择器巡检：首次运行，已建立基线（本次不做对比）", "选择器巡检")
    elif changes:
        log(f"🚨 选择器巡检：{len(changes)} 个族比上次更差了，疑似 Flow 又改版了", "选择器巡检")
        for change in changes:
            log("   ↳ " + change["message"], "选择器巡检")
    else:
        log(f"✅ 选择器巡检：{len(probe.get('families', []))} 个族与基线一致，无退化",
            "选择器巡检")

    if save:
        save_selector_baseline(probe)
    return {
        "status": "ok",
        "checked_at": probe.get("checked_at"),
        "url": probe.get("url"),
        "summary": probe.get("summary"),
        "alerts": alerts,
        "changes": changes,
        "had_baseline": baseline is not None,
    }


# ── Dry-run ────────────────────────────────────────────────────────────────
# FX_DRY_RUN=1：走完全部 DOM 定位、配置校验、参考图挂载，但**不点发送**。
# 这是在没有真账号额度、或者只想验证"UI 还认不认得我们"的时候唯一安全的验证方式，
# 也是让 agent 在离线环境里复现选择器类问题的开关。实现点在
# helpers.click_fx_send_button：那是所有链路唯一的提交动作出口。

def dry_run_enabled():
    return os.environ.get("FX_DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
