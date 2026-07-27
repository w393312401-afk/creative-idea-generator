# -*- coding: utf-8 -*-
"""
🎯 UI 弹性操作工具
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
弹性点击、可见性检查、弹窗关闭、MutationObserver 注入等 UI 操作工具。
选择器由 ui_selectors.py 集中管理。
"""

import os
import time

from ..config import OUTPUT_DIR
from .logger import log
from .browser import random_sleep
from ..ui_selectors import UI_SELECTORS


def handle_element_not_found(page, expected_elements_desc):
    """ 📸 案发现场保留：当关键元素找不到时，保存截图和HTML，极大加快人工排查速度 """
    error_dir = os.path.join(OUTPUT_DIR, "Errors")
    if not os.path.exists(error_dir):
        try: os.makedirs(error_dir)
        except: pass

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    screenshot_path = os.path.join(error_dir, f"error_{timestamp}.png")
    html_path = os.path.join(error_dir, f"error_{timestamp}.html")

    try:
        page.screenshot(path=screenshot_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"❌ 找不到关键UI [{expected_elements_desc}]", "Error")
        log(f"📸 案发现场已保存到: {screenshot_path}", "Error")
    except Exception as e:
        log(f"⚠️ 保存案发现场失败: {e}", "Error")


def robust_click(page, selector_list, timeout=5000, force=False, required=True):
    """ 🛡️ 弹性点击：尝试列表中的多个备用选择器，只要找到一个就点击 """
    if not isinstance(selector_list, list):
        selector_list = [selector_list]

    for sel in selector_list:
        try:
            target = page.locator(sel).first
            if target.is_visible(timeout=timeout//len(selector_list) + 500):
                log(f"✅ 弹性定位器命中: {sel}", "System")
                target.click(force=force)
                return True
        except:
            continue

    # 如果全都没找到，记录案发现场
    if required:
        handle_element_not_found(page, str(selector_list))
    return False


def check_visibility(page, selector_list, timeout=3000):
    """ 🛡️ 弹性可见性检查 """
    if not isinstance(selector_list, list):
        selector_list = [selector_list]
    for sel in selector_list:
        try:
            if page.locator(sel).first.is_visible(timeout=timeout//len(selector_list) + 500):
                return True
        except:
            continue
    return False


def close_possible_popups(page):
    """ 🛡️ 尝试关闭各种弹窗 (选择器来自 selectors.py) """
    try:
        for sel in UI_SELECTORS["common"]["close_popup_btns"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible():
                    log(f"🛡️ 关闭弹窗: {sel}", "系统")
                    btn.click()
                    random_sleep(0.5, 1.0)
            except: pass
    except Exception as e:
        log(f"🛡️ 弹窗检查出错 (非致命): {e}", "系统")


def inject_image_observer(page, existing_imgs):
    """注入 MutationObserver 实时监听新 img 出现 (比轮询更快)"""
    try:
        page.evaluate("""(existingUrls) => {
            window.__newImgSrc = null;
            const observer = new MutationObserver((mutations) => {
                if (window.__newImgSrc) return;
                for (const m of mutations) {
                    for (const node of m.addedNodes) {
                        if (node.nodeType !== 1) continue;
                        const imgs = node.tagName === 'IMG' ? [node] : Array.from(node.querySelectorAll ? node.querySelectorAll('img') : []);
                        for (const img of imgs) {
                            if (!img.src || existingUrls.includes(img.src)) continue;
                            const check = () => {
                                const rect = img.getBoundingClientRect();
                                if (rect.width >= 100 && rect.height >= 100) {
                                    window.__newImgSrc = img.src;
                                }
                            };
                            if (img.complete && img.naturalWidth > 0) { check(); }
                            else { img.addEventListener('load', check); }
                        }
                    }
                    if (m.type === 'attributes' && m.attributeName === 'src' && m.target.tagName === 'IMG') {
                        const img = m.target;
                        if (img.src && !existingUrls.includes(img.src)) {
                            const check2 = () => {
                                const rect = img.getBoundingClientRect();
                                if (rect.width >= 100 && rect.height >= 100) {
                                    window.__newImgSrc = img.src;
                                }
                            };
                            if (img.complete && img.naturalWidth > 0) { check2(); }
                            else { img.addEventListener('load', check2); }
                        }
                    }
                }
            });
            observer.observe(document.body, {
                childList: true, subtree: true,
                attributes: true, attributeFilter: ['src']
            });
        }""", list(existing_imgs))
        log("  ✅ MutationObserver 已注入", "GoogleFX")
    except Exception as e:
        log(f"  ⚠️ MutationObserver 注入失败: {e}", "GoogleFX")


def inject_batch_image_observer(page, existing_imgs):
    """注入 MutationObserver 实时监听多个新 img 出现"""
    try:
        page.evaluate("""(existingUrls) => {
            window.__newImgSrcs = [];
            const observer = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    for (const node of m.addedNodes) {
                        if (node.nodeType !== 1) continue;
                        const imgs = node.tagName === 'IMG' ? [node] : Array.from(node.querySelectorAll ? node.querySelectorAll('img') : []);
                        for (const img of imgs) {
                            if (!img.src || existingUrls.includes(img.src) || window.__newImgSrcs.includes(img.src)) continue;
                            const check = () => {
                                const rect = img.getBoundingClientRect();
                                if (rect.width >= 100 && rect.height >= 100) {
                                    if (!window.__newImgSrcs.includes(img.src)) {
                                        window.__newImgSrcs.push(img.src);
                                    }
                                }
                            };
                            if (img.complete && img.naturalWidth > 0) { check(); }
                            else { img.addEventListener('load', check); }
                        }
                    }
                    if (m.type === 'attributes' && m.attributeName === 'src' && m.target.tagName === 'IMG') {
                        const img = m.target;
                        if (img.src && !existingUrls.includes(img.src) && !window.__newImgSrcs.includes(img.src)) {
                            const check2 = () => {
                                const rect = img.getBoundingClientRect();
                                if (rect.width >= 100 && rect.height >= 100) {
                                    if (!window.__newImgSrcs.includes(img.src)) {
                                        window.__newImgSrcs.push(img.src);
                                    }
                                }
                            };
                            if (img.complete && img.naturalWidth > 0) { check2(); }
                            else { img.addEventListener('load', check2); }
                        }
                    }
                }
            });
            observer.observe(document.body, {
                childList: true, subtree: true,
                attributes: true, attributeFilter: ['src']
            });
        }""", list(existing_imgs))
        log("  ✅ Batch MutationObserver 已注入", "GoogleFX")
    except Exception as e:
        log(f"  ⚠️ Batch MutationObserver 注入失败: {e}", "GoogleFX")
