# -*- coding: utf-8 -*-
"""
🧱 Google FX DOM 原语层 (L1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯"给页面+选择器列表,点/找/按键"的通用 Playwright 交互函数。
不了解任何 Google FX 业务概念（不知道 UI_SELECTORS/RATIO_MAP/配置面板等）。
被 google_fx_helpers.py (L2) 和 google_fx.py (L3) 调用，自身不依赖任何
其他 google_fx* 模块。
"""

from ..utils.logger import log
from ..utils.browser import random_sleep
from ..utils import selector_stats


def _click_first_visible(root, selectors, timeout=2000, force=False, log_prefix="", family=""):
    """遍历选择器列表，点击第一个可见元素。返回 (locator, sel_index) 或 (None, -1)。"""
    for idx, sel in enumerate(selectors):
        try:
            el = root.locator(sel).first
            if el.is_visible(timeout=timeout):
                el.click(force=force)
                if family:
                    selector_stats.record_hit(family, idx, selector=sel, total=len(selectors))
                return el, idx
        except Exception:
            pass
    if family:
        selector_stats.record_hit(family, -1, total=len(selectors))
    return None, -1


def _find_first_visible(root, selectors, timeout=2000, family=""):
    """遍历选择器列表，返回第一个可见元素 Locator 或 None（不点击）。"""
    for idx, sel in enumerate(selectors):
        try:
            el = root.locator(sel).first
            if el.is_visible(timeout=timeout):
                if family:
                    selector_stats.record_hit(family, idx, selector=sel, total=len(selectors))
                return el
        except Exception:
            pass
    if family:
        selector_stats.record_hit(family, -1, total=len(selectors))
    return None


def _safe_press_escape(page, context_label, sleep_range=None):
    """按 Escape 关闭当前弹层；失败时记录异常类型，避免静默吞掉。"""
    try:
        page.keyboard.press("Escape")
        if sleep_range:
            random_sleep(*sleep_range)
        return True
    except Exception as e:
        log(f"⚠️ {context_label}: keyboard.press('Escape') 失败: {type(e).__name__}", "GoogleFX")
        return False
