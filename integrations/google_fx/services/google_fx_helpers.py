# -*- coding: utf-8 -*-
"""
🛠️ Google FX Helpers (UI Interaction, Navigation, Upload & Canvas Helpers)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔒 LOCKED 2026-03-25 — 本文件包含 find_fx_config_button / check_fx_config /
   fix_fx_config (原定义于 services/google_fx.py，整体搬移，函数体逐字未改动)。
   禁止在未获用户明确指示前修改这三个函数的任何逻辑。
"""

import os
import re
import time
import random
import requests

from ..config import OUTPUT_DIR, get_runtime_max_wait_seconds
from ..utils.logger import log
from ..utils.browser import (
    random_sleep, clean_path, get_ads_ws_url, find_or_create_page, ensure_flow_workspace,
    _page_is_alive,
)
from ..ui_selectors import UI_SELECTORS, RATIO_MAP, ORIENT_ICON_MAP
from ..model_catalog import DEFAULT_GOOGLE_FX_IMAGE_MODEL, GOOGLE_FX_IMAGE_MODELS
from ..utils import selector_stats
from ..utils import cancel_flag
from .google_fx_dom import _click_first_visible, _find_first_visible, _safe_press_escape
from .google_fx_credit import detect_page_credit_exhaustion, is_credit_exhausted_message

_SLATE_EDITOR_SELECTOR = "[data-slate-editor='true']"


# ── _click_new_project_button ──
def _click_new_project_button(page, confirm_timeout=10.0):
    """点击新建项目，并以进入新的 ``/project/`` URL 作为成功条件。

    ``add_2`` 同时用于项目内的“创建/添加媒体”按钮，所以 Playwright 的 click
    没抛异常并不代表新项目已创建。中英文 UI 都必须通过导航结果确认，避免假阳性。
    """
    try:
        before_url = str(page.url or "")
    except Exception:
        before_url = ""

    def _project_navigation_confirmed():
        try:
            current_url = str(page.url or "")
        except Exception:
            return False
        if "/project/" not in current_url:
            return False
        return "/project/" not in before_url or current_url != before_url

    for sel in UI_SELECTORS["google_fx"].get("new_project_btn", []):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=5000)
                deadline = time.monotonic() + max(0.0, float(confirm_timeout))
                while True:
                    if _project_navigation_confirmed():
                        random_sleep(2, 4)
                        log(f"🆕 新建项目成功，已确认进入项目页 (sel={sel!r}, url={page.url})", "GoogleFX")
                        return True
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
                log(f"⚠️ 新建项目点击未生效，未进入新的项目页 (sel={sel!r})", "GoogleFX")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception as e:
            log(f"  ⚠️ 新建项目候选点击失败 (sel={sel!r}): {type(e).__name__}", "GoogleFX")
    return False


# ── _find_add2_btn ──
def _find_add2_btn(page):
    """多策略定位 add_2 (Create) 按钮，返回 Locator 或 None

    选择器来自 UI_SELECTORS['google_fx']['add_media_btn']，不再内联一份。
    以前这里自带 8 条选择器、按 family='fx_add2_btn' 记统计，而选择器探针探的是
    UI_SELECTORS 里另一份只有 1 条的 add_media_btn——两边测的根本不是同一个东西，
    探针报绿不代表生产路径能点得到。统一成一个源后，探针命中层级和运行期统计
    才对得上。
    """
    return _find_first_visible(
        page,
        UI_SELECTORS["google_fx"]["add_media_btn"],
        family="add_media_btn",
    )


# ── _wait_for_fx_toolbar ──
def _wait_for_fx_toolbar(page, timeout=30):
    """等待底部工具栏中的输入区出现。

    ⚠️ 进循环前先把智能体模式和弹窗清掉（2026-07-25）：智能体模式下底部标准工具栏
    被聊天框替换，而聊天框同样是 contenteditable，_find_fx_prompt_input 会立刻返回
    "就绪"——工具栏其实根本没加载，后面找配置按钮必然扑空（视频链路 2026-07-23 已
    确诊过同一现象）。这两个动作放在检测之前，才能拦住这种假阳性而不是等它下游报错。
    """
    log("📍 等待底部工具栏...", "GoogleFX")
    _dismiss_unexpected_overlays(page, "GoogleFX")
    _close_agent_settings_sidebar(page, "GoogleFX")
    _dismiss_active_agent_mode(page, "GoogleFX")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_fx_prompt_input(page, announce=False):
            log("✅ 底部工具栏已加载", "GoogleFX")
            return True
        # 浏览器/标签页已经关掉就别再等了：page 已死，后面每一轮检测都只会抛
        # TargetClosedError 被咽掉，白等满整个 timeout（见 _page_is_gone 注释）。
        if _page_is_gone(page):
            raise RuntimeError("等待底部工具栏时浏览器/标签页已关闭，停止等待")
        # 随时检测是否需要人工干预（登录、滑块、安全拦截等）
        try:
            _raise_if_manual_intervention_required(page, context_label="等待工具栏中")
        except RuntimeError as e:
            if "MANUAL_REQUIRED" in str(e):
                log(f"⚠️ 等待工具栏时检测到需要人工处理: {e}", "GoogleFX")
                raise
        time.sleep(1)

    # 案发现场保留
    try:
        from ..utils.ui_helpers import handle_element_not_found
        handle_element_not_found(page, "底部工具栏输入框")
    except Exception as e:
        log(f"⚠️ 保存案发现场截图失败: {e}", "GoogleFX")
    # 结构化取证（截图 + DOM + 选择器探针），落 runtime/fx_debug/ 供控制台下载。
    # 这一处是最常见的"卡在这里不动"入口，光靠日志看不出页面当时长什么样。
    try:
        from ..utils.forensics import capture
        capture(page, "wait_toolbar_timeout", f"等待底部工具栏超过 {timeout}s")
    except Exception:
        pass

    raise RuntimeError("等待底部工具栏超时，未检测到可用输入框")


# ── _extract_flow_image_uuid ──
def _extract_flow_image_uuid(image_ref: str):
    """从本地路径 / URL / 纯 UUID 中提取 Flow 图片 UUID。"""
    if not image_ref:
        return None
    value = str(image_ref).strip()
    if not value:
        return None

    uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    basename = os.path.basename(value.split('?', 1)[0].split('#', 1)[0])
    basename_matches = re.findall(uuid_pattern, basename, flags=re.IGNORECASE)
    if basename_matches:
        return basename_matches[-1]

    matches = re.findall(uuid_pattern, value, flags=re.IGNORECASE)
    return matches[-1] if matches else None


# 2026-08-01 清理：这里原有 _get_recent_flow_image_uuids()，是下面 _get_recent_flow_image_cards()
# 的一层"只取 uuid 列"的薄包装，全 repo 零调用者。需要 UUID 的地方走的是
# _get_panel_uuid_order() / _get_prompt_reference_uuids()。


# ── _get_recent_flow_image_cards ──
def _get_recent_flow_image_cards(page, limit=2):
    """
    获取画布里最近的一批图片卡片，返回 [{tile_id, uuid, top, left, area}]。
    只保留唯一 tile，避免新版 DOM 中同一张图被重复枚举。
    limit 仅作为"最少返回"的提示——函数始终返回 JS 扫描到的全部有效卡片，
    以确保按 UUID 匹配时不会因截断而漏掉目标。
    """
    try:
        cards = page.evaluate("""() => {
            const rows = [];
            const uuidRegex = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
            const seenTileIds = new Set();
            const tiles = Array.from(document.querySelectorAll('div[data-tile-id]'));
            for (const tile of tiles) {
                const tileId = tile.getAttribute('data-tile-id') || '';
                if (!tileId || seenTileIds.has(tileId)) continue;
                seenTileIds.add(tileId);
                const imgs = Array.from(tile.querySelectorAll('img'));
                const img = imgs.find((node) => {
                    const width = node.offsetWidth || node.naturalWidth || 0;
                    const height = node.offsetHeight || node.naturalHeight || 0;
                    return width > 40 && height > 40;
                });
                if (!img) continue;
                const src = img.currentSrc || img.src || '';
                const match = src.match(uuidRegex);
                if (!match) continue;
                const rect = tile.getBoundingClientRect();
                rows.push({
                    tile_id: tileId,
                    uuid: match[1],
                    top: rect.top || 0,
                    left: rect.left || 0,
                    area: (rect.width || 0) * (rect.height || 0),
                });
            }
            rows.sort((a, b) => {
                if (a.top !== b.top) return a.top - b.top;
                if (a.left !== b.left) return a.left - b.left;
                return b.area - a.area;
            });
            return rows;
        }""")
    except Exception as e:
        log(f"⚠️ 获取最新图片卡片失败: {type(e).__name__}", "GoogleFX")
        return []

    return [item for item in cards if item.get("uuid") and item.get("tile_id")]


# ── _find_tile_by_uuid_js ──
def _find_tile_by_uuid_js(page, uuid):
    """
    通过 img[src*=UUID] 在整个 DOM（含虚拟滚动区域）中精确定位卡片。
    找到后自动 scrollIntoView，返回 tile info dict 或 None。
    """
    if not uuid:
        return None
    try:
        result = page.evaluate("""(targetUuid) => {
            const imgs = Array.from(document.querySelectorAll('img[src*="' + targetUuid + '"]'));
            const big = imgs.find(i => {
                const w = i.offsetWidth || i.naturalWidth || 0;
                const h = i.offsetHeight || i.naturalHeight || 0;
                return w > 30 && h > 30;
            }) || imgs[0];
            if (!big) return null;
            const tile = big.closest('[data-tile-id]');
            if (!tile) return null;
            tile.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            const rect = tile.getBoundingClientRect();
            return {
                tile_id: tile.getAttribute('data-tile-id') || '',
                uuid: targetUuid,
                top: rect.top || 0,
                left: rect.left || 0,
                area: (rect.width || 0) * (rect.height || 0),
            };
        }""", uuid)
        if result and result.get("tile_id"):
            log(f"  🎯 通过 img[src*=UUID] 精确定位卡片: {uuid[:16]}... (tile={result['tile_id']})", "GoogleFX")
            return result
    except Exception as e:
        log(f"  ⚠️ _find_tile_by_uuid_js 失败: {type(e).__name__}", "GoogleFX")
    return None


# ── _resolve_flow_tile_info ──
def _resolve_flow_tile_info(page, uuid: str = "", tile_id: str = ""):
    """解析目标 Flow 卡片的位置与 tile_id，供 hover / toolbar 定位复用。"""
    try:
        return page.evaluate("""({ uuid, tileId }) => {
            const tile = tileId ? document.querySelector('[data-tile-id="' + tileId + '"]') : null;
            const tileImgs = tile ? Array.from(tile.querySelectorAll('img')) : [];
            const imgs = tileImgs.length > 0
                ? tileImgs
                : Array.from(document.querySelectorAll(uuid ? 'img[src*="' + uuid + '"]' : 'img'));
            const big = imgs.find(i => (i.offsetWidth || i.naturalWidth || 0) > 50);
            const target = big || imgs[0];
            if (!target) return null;
            const resolvedTile = tile || target.closest('[data-tile-id]');
            if (!resolvedTile) return null;
            const rect = resolvedTile.getBoundingClientRect();
            if (resolvedTile.scrollIntoView) {
                resolvedTile.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            } else if (target.scrollIntoView) {
                target.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            }
            return {
                w: rect.width,
                h: rect.height,
                top: rect.top,
                left: rect.left,
                right: rect.right,
                bottom: rect.bottom,
                centerX: rect.left + (rect.width / 2),
                centerY: rect.top + (rect.height / 2),
                hoverX: rect.right - Math.min(Math.max(rect.width * 0.18, 42), 120),
                hoverY: rect.top + Math.min(Math.max(rect.height * 0.2, 32), 110),
                tileId: resolvedTile.getAttribute('data-tile-id') || '',
            };
        }""", {"uuid": uuid or "", "tileId": tile_id or ""})
    except Exception as e:
        log(f"⚠️ _resolve_flow_tile_info 失败: {type(e).__name__}", "GoogleFX")
        return None


# ── _hover_flow_tile_for_toolbar ──
def _hover_flow_tile_for_toolbar(page, uuid: str = "", tile_id: str = ""):
    """
    让 Flow 卡片稳定进入 hover 态。
    先尝试 Locator.hover，再补一段真实鼠标轨迹与 JS mouseenter 事件。
    """
    info = _resolve_flow_tile_info(page, uuid=uuid, tile_id=tile_id)
    if not info:
        return None

    resolved_tile_id = (info.get("tileId") or tile_id or "").strip()
    tile_scope = page.locator(f"[data-tile-id='{resolved_tile_id}']").first if resolved_tile_id else None

    try:
        if tile_scope and tile_scope.is_visible(timeout=2000):
            tile_scope.hover()
            log("  ✅ Hover 命中 tile 容器", "GoogleFX")
    except Exception:
        pass

    try:
        page.mouse.move(max(info["left"] - 20, 5), max(info["top"] - 20, 5))
        page.mouse.move(info["centerX"], info["centerY"], steps=8)
        page.mouse.move(info["hoverX"], info["hoverY"], steps=10)
        log("  ✅ 鼠标轨迹已扫过 tile 工具栏区域", "GoogleFX")
    except Exception as e:
        log(f"  ⚠️ 鼠标 hover 轨迹失败: {type(e).__name__}", "GoogleFX")

    try:
        page.evaluate("""({ tileId }) => {
            const tile = tileId ? document.querySelector('[data-tile-id="' + tileId + '"]') : null;
            if (!tile) return false;
            for (const evtName of ['mouseenter', 'mouseover', 'mousemove']) {
                tile.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
            }
            const rect = tile.getBoundingClientRect();
            const hotspot = document.elementFromPoint(
                rect.right - Math.min(Math.max(rect.width * 0.18, 42), 120),
                rect.top + Math.min(Math.max(rect.height * 0.2, 32), 110),
            );
            if (hotspot) {
                for (const evtName of ['mouseenter', 'mouseover', 'mousemove']) {
                    hotspot.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
                }
            }
            return true;
        }""", {"tileId": resolved_tile_id})
    except Exception:
        pass

    random_sleep(0.4, 0.7)
    return info


# ── _click_flow_more_menu ──
def _click_flow_more_menu(page, uuid: str = "", tile_id: str = "") -> str:
    """点击目标卡片右上角 more_vert 菜单，成功时返回 aria-controls 指向的菜单 id。"""
    attempts = 4
    last_tile_id = tile_id or ""

    for attempt in range(1, attempts + 1):
        info = _hover_flow_tile_for_toolbar(page, uuid=uuid, tile_id=last_tile_id)
        if not info:
            return ""

        last_tile_id = (info.get("tileId") or last_tile_id or "").strip()
        tile_scope = page.locator(f"[data-tile-id='{last_tile_id}']").first if last_tile_id else None
        more_clicked = False
        menu_id = ""
        button_id = ""
        button_timeout = 1200 + (attempt - 1) * 500
        menu_wait_ms = 250 + (attempt - 1) * 180

        scopes = []
        if tile_scope is not None:
            try:
                toolbar_scope = tile_scope.locator("[role='toolbar']").first
                scopes.append(("tile-toolbar", toolbar_scope))
            except Exception:
                pass
            scopes.append(("tile", tile_scope))

        page.wait_for_timeout(menu_wait_ms)

        for scope_name, scope in scopes:
            for more_sel in [
                "button[aria-haspopup='menu']",
                "button:has(i:text-is('more_vert'))",
                "button:has(i:text-is('more_horiz'))",
                "button:has(i:text-is('menu'))",
                "button[aria-label='More']",
                "button[aria-label='更多']",
                "button[aria-label='More options']",
                "button[aria-label*='more' i]",
                "button[aria-label*='更多']",
                "button:has(span:text-is('More'))",
                "button:has(span:text-is('更多'))",
                "button:has-text('More')",
                "button:has-text('更多')",
                "button:has-text('more_vert')",
            ]:
                try:
                    btn = scope.locator(more_sel).last
                    if btn.is_visible(timeout=button_timeout):
                        box = btn.bounding_box()
                        # 防止误触底部控制栏按钮（如模型选择、画幅选择等）
                        viewport_height = page.viewport_size.get("height", 800) if page.viewport_size else 800
                        if box and box.get("y", 0) > viewport_height - 120:
                            continue

                        menu_id = (btn.get_attribute("aria-controls") or "").strip()
                        button_id = (btn.get_attribute("id") or "").strip()
                        btn.scroll_into_view_if_needed()
                        btn.click(force=False)
                        more_clicked = True
                        page.wait_for_timeout(menu_wait_ms)
                        menu_id = _resolve_open_flow_menu_id(page, button_id=button_id, menu_id_hint=menu_id)
                        log(
                            f"  ✅ 点击 more_vert (scope={scope_name}, sel={more_sel!r}, attempt={attempt}, "
                            f"button_id={button_id or '<none>'}, menu_id={menu_id or '<none>'})",
                            "GoogleFX",
                        )
                        break
                except Exception:
                    continue
            if more_clicked:
                break

        if not more_clicked:
            try:
                js_result = page.evaluate("""({ uuid, tileId }) => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const tile = tileId ? document.querySelector('[data-tile-id="' + tileId + '"]') : null;
                    const fallbackImg = uuid
                        ? Array.from(document.querySelectorAll('img[src*="' + uuid + '"]')).find((img) => {
                            const w = img.offsetWidth || img.naturalWidth || 0;
                            const h = img.offsetHeight || img.naturalHeight || 0;
                            return w > 40 && h > 40;
                        })
                        : null;
                    const resolvedTile = tile || (fallbackImg ? fallbackImg.closest('[data-tile-id]') : null);
                    if (!resolvedTile) return false;

                    for (const evtName of ['mouseenter', 'mouseover', 'mousemove']) {
                        resolvedTile.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
                    }

                    const rect = resolvedTile.getBoundingClientRect();
                    const candidates = Array.from(document.querySelectorAll('button,[role="button"]')).filter((btn) => {
                        if (!btn || btn.offsetParent === null) return false;
                        const r = btn.getBoundingClientRect();
                        // 排除页面底部控制栏（模型/比例/提示词输入框等）
                        if (r.top > window.innerHeight - 120 || btn.closest('footer, form, [role="form"], [data-testid*="prompt"]')) return false;

                        const iconText = (btn.querySelector('i, span, svg')?.innerText || '').toLowerCase();
                        const labelText = norm([
                            btn.innerText || '',
                            btn.getAttribute('aria-label') || '',
                            btn.getAttribute('title') || '',
                            iconText
                        ].join(' '));

                        // 按钮必须属于当前 tile 容器或其关联 toolbar，或者包含明确的 more 图标/文本
                        const isInsideTile = resolvedTile.contains(btn);
                        const hasMoreText = labelText.includes('more_vert') ||
                            labelText.includes('more_horiz') ||
                            labelText === 'more' ||
                            labelText.includes('more options') ||
                            labelText.includes('更多');

                        if (!isInsideTile && !hasMoreText) return false;

                        const horizontalNear = r.right >= rect.left - 20 && r.left <= rect.right + 20;
                        const verticalNear = r.bottom >= rect.top - 20 && r.top <= rect.top + Math.max(rect.height * 0.5, 120);
                        return horizontalNear && verticalNear;
                    });
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        if (ar.top !== br.top) return ar.top - br.top;
                        return br.left - ar.left;
                    });
                    const target = candidates[0];
                    for (const evtName of ['mouseenter', 'mouseover', 'mousemove']) {
                        target.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
                    }
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return {
                        clicked: true,
                        menuId: target.getAttribute('aria-controls') || '',
                        buttonId: target.getAttribute('id') || '',
                    };
                }""", {"uuid": uuid or "", "tileId": last_tile_id or ""})
                if js_result and js_result.get("clicked"):
                    more_clicked = True
                    menu_id = (js_result.get("menuId") or "").strip()
                    button_id = (js_result.get("buttonId") or "").strip()
                    page.wait_for_timeout(menu_wait_ms)
                    menu_id = _resolve_open_flow_menu_id(page, button_id=button_id, menu_id_hint=menu_id)
                    log(
                        f"  ✅ JS fallback 点击 more_vert (attempt={attempt}, "
                        f"button_id={button_id or '<none>'}, menu_id={menu_id or '<none>'})",
                        "GoogleFX",
                    )
            except Exception as e:
                log(f"  ⚠️ more_vert JS fallback: {type(e).__name__}", "GoogleFX")

        if more_clicked:
            return menu_id

        _safe_press_escape(page, f"_click_flow_more_menu attempt={attempt} cleanup")
        page.wait_for_timeout(220 + attempt * 220)
        log(f"  ⚠️ more_vert 第 {attempt} 次尝试失败，重新 hover", "GoogleFX")

    log(f"  ❌ Add to Prompt 失败 | stage=menu_button_missing | tile_id={last_tile_id or '<none>'} | menu_id=<none>", "GoogleFX")
    return ""


# ── _get_flow_menu_debug_info ──
def _get_flow_menu_debug_info(page, menu_id_hint: str = ""):
    """抓取当前打开菜单的简要诊断信息，便于排查 Add to Prompt 偶发失败。"""
    try:
        return page.evaluate("""({ menuId }) => {
            const visible = (el) => {
                if (!el || el.offsetParent === null) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 4 && rect.height > 4;
            };
            const hinted = menuId ? document.getElementById(menuId) : null;
            const menus = Array.from(document.querySelectorAll(
                '[role="menu"][data-state="open"], [data-radix-menu-content][data-state="open"], [role="menu"]'
            )).filter(visible);
            const root = (hinted && visible(hinted)) ? hinted : (menus.length ? menus[menus.length - 1] : null);
            if (!root) {
                return {
                    menuId: menuId || '',
                    menuFound: false,
                    menuText: '',
                    hasMenuitem: false,
                    itemTexts: [],
                };
            }

            const items = Array.from(root.querySelectorAll('[role="menuitem"], button, div, span'))
                .filter(visible)
                .map((el) => ((el.innerText || '').replace(/\\s+/g, ' ').trim()))
                .filter(Boolean);

            return {
                menuId: root.id || menuId || '',
                menuFound: true,
                menuText: ((root.innerText || '').replace(/\\s+/g, ' ').trim()).slice(0, 300),
                hasMenuitem: root.querySelectorAll('[role="menuitem"]').length > 0,
                itemTexts: items.slice(0, 8),
            };
        }""", {"menuId": menu_id_hint or ""})
    except Exception as e:
        return {
            "menuId": menu_id_hint or "",
            "menuFound": False,
            "menuText": "",
            "hasMenuitem": False,
            "itemTexts": [f"debug_error:{type(e).__name__}"],
        }


# ── _resolve_open_flow_menu_id ──
def _resolve_open_flow_menu_id(page, button_id: str = "", menu_id_hint: str = "") -> str:
    """根据触发按钮与当前可见菜单，尽量反查实际打开的菜单 id。"""
    try:
        menu_id = page.evaluate("""({ buttonId, menuId }) => {
            const visible = (el) => {
                if (!el || el.offsetParent === null) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 4 && rect.height > 4;
            };
            const menus = Array.from(document.querySelectorAll(
                '[role="menu"][data-state="open"], [data-radix-menu-content][data-state="open"], [role="menu"]'
            )).filter(visible);
            if (!menus.length) return '';

            const hinted = menuId ? menus.find((menu) => menu.id === menuId) : null;
            if (hinted) return hinted.id || '';

            const trigger = buttonId ? document.getElementById(buttonId) : null;
            if (trigger) {
                const ariaControls = trigger.getAttribute('aria-controls') || '';
                if (ariaControls) {
                    const controlled = menus.find((menu) => menu.id === ariaControls);
                    if (controlled) return controlled.id || ariaControls;
                }
                const labelled = menus.find((menu) => (menu.getAttribute('aria-labelledby') || '') === buttonId);
                if (labelled) return labelled.id || '';
                const triggerRect = trigger.getBoundingClientRect();
                const nearest = menus
                    .map((menu) => {
                        const rect = menu.getBoundingClientRect();
                        const dx = rect.left - triggerRect.left;
                        const dy = rect.top - triggerRect.bottom;
                        return { menu, score: Math.abs(dx) + Math.abs(dy) };
                    })
                    .sort((a, b) => a.score - b.score)[0];
                if (nearest?.menu) return nearest.menu.id || '';
            }

            return menus[menus.length - 1]?.id || '';
        }""", {"buttonId": button_id or "", "menuId": menu_id_hint or ""})
        return (menu_id or "").strip()
    except Exception as e:
        log(f"  ⚠️ 反查打开菜单失败: {type(e).__name__}", "GoogleFX")
        return ""


# ── _click_flow_add_to_prompt ──
def _click_flow_add_to_prompt(page, menu_id_hint: str = "", tile_id_hint: str = "") -> bool:
    """点击已展开菜单中的 Add to Prompt，兼容大小写与 portal 渲染差异。"""
    add_patterns = [
        "Add to Prompt",
        "Add to prompt",
        "add to prompt",
        "添加到提示词",
        "添加到提示",
    ]

    open_menu = None
    if menu_id_hint:
        try:
            hinted = page.locator(f"[id=\"{menu_id_hint}\"]").first
            hinted.wait_for(state="visible", timeout=2500)
            open_menu = hinted
            log(f"  ✅ 已锁定打开菜单 (id={menu_id_hint})", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ menu_id 提示未命中 ({menu_id_hint}): {type(e).__name__}", "GoogleFX")

    for menu_sel in [
        "[role='menu'][data-state='open']",
        "[data-radix-menu-content][data-state='open']",
        "[role='menu']",
    ]:
        try:
            candidate = page.locator(menu_sel).last
            if candidate.is_visible(timeout=1500):
                open_menu = candidate
                log(f"  ✅ 已锁定打开菜单 (sel={menu_sel!r})", "GoogleFX")
                break
        except Exception:
            pass

    for add_sel in [
        "button[role='menuitem']:has-text('Add to Prompt')",
        "button[role='menuitemradio']:has-text('Add to Prompt')",
        "button[role='menuitemcheckbox']:has-text('Add to Prompt')",
        "[role='menuitem']:has-text('Add to Prompt')",
        "[role='menuitemradio']:has-text('Add to Prompt')",
        "[role='menuitemcheckbox']:has-text('Add to Prompt')",
        "button[role='menuitem']:has-text('Add to prompt')",
        "[role='menuitem']:has-text('Add to prompt')",
        "button[role='menuitemradio']:has-text('Add to prompt')",
        "button[role='menuitemcheckbox']:has-text('Add to prompt')",
        "button[role='menuitem']:has-text('添加到提示词')",
        "button[role='menuitemradio']:has-text('添加到提示词')",
        "button[role='menuitemcheckbox']:has-text('添加到提示词')",
        "[role='menuitem']:has-text('添加到提示词')",
        "[role='menuitemradio']:has-text('添加到提示词')",
        "[role='menuitemcheckbox']:has-text('添加到提示词')",
        "button[role='menuitem']:has-text('添加到提示')",
        "button[role='menuitemradio']:has-text('添加到提示')",
        "button[role='menuitemcheckbox']:has-text('添加到提示')",
        "[role='menuitem']:has-text('添加到提示')",
        "[role='menuitemradio']:has-text('添加到提示')",
        "[role='menuitemcheckbox']:has-text('添加到提示')",
    ]:
        try:
            scope = open_menu if open_menu is not None else page
            add_btn = scope.locator(add_sel).first
            if add_btn.is_visible(timeout=2500):
                add_btn.scroll_into_view_if_needed()
                try:
                    add_btn.click(force=False, timeout=2000)
                except Exception:
                    add_btn.click(force=True)
                log(f"  ✅ 点击 Add to Prompt (sel={add_sel!r})", "GoogleFX")
                return True
        except Exception:
            continue

    try:
        js_add_clicked = page.evaluate("""({ patterns, menuId }) => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const visible = (el) => {
                if (!el || el.offsetParent === null) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 4 && rect.height > 4;
            };

            const hinted = menuId ? document.getElementById(menuId) : null;
            const menus = Array.from(document.querySelectorAll(
                '[role="menu"][data-state="open"], [data-radix-menu-content][data-state="open"], [role="menu"]'
            )).filter(visible);

            const roots = [];
            if (hinted && visible(hinted)) roots.push(hinted);
            if (menus.length) roots.push(...menus.reverse());
            if (!roots.length) roots.push(document.body);
            for (const root of roots) {
                // 优先查找真正的 [role="menuitem"] 或 button 交互元素
                const candidates = Array.from(root.querySelectorAll('[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], button')).filter(visible);
                let target = candidates.find((item) => {
                    const text = norm(item.innerText || '');
                    const label = norm(item.getAttribute('aria-label') || '');
                    const title = norm(item.getAttribute('title') || '');
                    return patterns.some((pattern) => {
                        const p = norm(pattern);
                        return text === p || label === p || title === p || text.includes(p) || label.includes(p);
                    });
                });

                if (!target) {
                    const allItems = Array.from(root.querySelectorAll('div, span')).filter(visible);
                    const subItem = allItems.find((item) => {
                        const text = norm(item.innerText || '');
                        return patterns.some((pattern) => norm(pattern) === text);
                    });
                    if (subItem) {
                        target = subItem.closest('[role="menuitem"], button') || subItem;
                    }
                }

                if (!target) continue;
                if (typeof target.click === 'function') {
                    target.click();
                } else {
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                }
                return true;
            }
            return false;
        }""", {"patterns": add_patterns, "menuId": menu_id_hint or ""})
        if js_add_clicked:
            log("  ✅ JS fallback 点击 Add to Prompt", "GoogleFX")
            return True
    except Exception as e:
        log(f"  ⚠️ Add to Prompt JS fallback: {type(e).__name__}", "GoogleFX")

    debug_info = _get_flow_menu_debug_info(page, menu_id_hint=menu_id_hint)
    log(
        "  ❌ Add to Prompt 失败 | "
        f"stage=menu_item_not_found | tile_id={tile_id_hint or '<none>'} | "
        f"menu_id={debug_info.get('menuId') or menu_id_hint or '<none>'} | "
        f"menu_found={debug_info.get('menuFound')} | "
        f"has_menuitem={debug_info.get('hasMenuitem')} | "
        f"menu_text={debug_info.get('menuText')!r} | "
        f"items={debug_info.get('itemTexts')}",
        "GoogleFX",
    )
    _safe_press_escape(page, "_click_flow_add_to_prompt 关闭菜单")
    return False


# ── _mount_flow_images_to_prompt ──
def _mount_flow_images_to_prompt(page, image_refs, context_label="参考图"):
    """
    优先按传入 ref 在当前画布里命中对应卡片；命中不到时通过
    img[src*=UUID] 精确定位（含 scrollIntoView）。
    只有在「所有请求的 UUID 都已命中或通过精确定位找到」时才回退到可见卡片顺序
    （仅限单张参考图场景）。视频模式多张参考图场景下不做回退，直接返回已命中的卡片，
    避免挂载错误的图片导致顺序校验失败。
    返回成功挂载的 UUID 列表。
    """
    requested = [ref for ref in (image_refs or []) if str(ref or "").strip()]
    if not requested:
        return []

    desired_count = min(len(requested), 2)
    ordered_cards = []
    seen_tiles = set()
    # 视频请求通常携带精确 UUID。先用 img[src*=UUID] 定位，避免每个槽位都遍历
    # 整张历史画布（长项目可有 90+ 卡片，扫描会越来越慢且增加 hover 串卡风险）。
    visible_cards = []
    missing_requested = []

    for ref in requested:
        uuid = _extract_flow_image_uuid(str(ref))
        matched = _find_tile_by_uuid_js(page, uuid) if uuid else None
        if matched and matched.get("tile_id") not in seen_tiles:
            ordered_cards.append(matched)
            seen_tiles.add(matched.get("tile_id"))
        elif uuid:
            missing_requested.append(uuid)

    # 非 UUID 的旧调用方才需要扫描最近卡片；多参考图不做随意回退。
    if not any(_extract_flow_image_uuid(str(ref)) for ref in requested):
        visible_cards = _get_recent_flow_image_cards(page, limit=desired_count)
        for card in visible_cards:
            if card.get('tile_id') and card.get('tile_id') not in seen_tiles:
                ordered_cards.append(card)
                seen_tiles.add(card.get('tile_id'))
                if len(ordered_cards) >= desired_count:
                    break

    log(
        f"🧭 {context_label}: UUID 精确定位命中 {len(ordered_cards)}/{desired_count}"
        + (f"；兼容扫描 {len(visible_cards)} 张卡片" if visible_cards else "；跳过全画布扫描"),
        "GoogleFX",
    )

    if missing_requested:
        log(f"🔍 {context_label}: 尝试通过 img[src*=UUID] 精确定位 {len(missing_requested)} 张未命中卡片", "GoogleFX")
        still_missing = []
        for uuid in missing_requested:
            found = _find_tile_by_uuid_js(page, uuid)
            if found and found.get("tile_id") not in seen_tiles:
                ordered_cards.append(found)
                seen_tiles.add(found.get("tile_id"))
                log(f"  ✅ {context_label}: UUID {uuid[:16]}... 通过精确定位命中", "GoogleFX")
            else:
                still_missing.append(uuid)
        if still_missing:
            log(f"⚠️ {context_label}: 仍有 {len(still_missing)} 张未找到: {', '.join(u[:8] for u in still_missing[:4])}", "GoogleFX")

    if len(ordered_cards) < desired_count:
        # 🚨 不再回退到"随便一张可见卡片"：挂错图片比挂载失败更危险（会静默生成
        # 错误的首/尾帧视频而不报错）。找不到就如实报告未命中数量，让调用方按
        # mounted < expected 的既有校验逻辑走重试/失败流程。
        log(
            f"⚠️ {context_label}: 当前页未找到请求 UUID，跳过可见卡片回退 "
            f"({len(ordered_cards)}/{desired_count} 命中，避免挂错图片)",
            "GoogleFX",
        )

    mounted = []
    for idx, card in enumerate(ordered_cards[:desired_count], start=1):
        uuid = card.get("uuid") or ""
        tile_id = card.get("tile_id") or ""
        log(f"🖼️ {context_label}: 挂载第 {idx} 张卡片 ({uuid[:16]}...)", "GoogleFX")
        if not _add_flow_image_to_prompt(page, uuid, tile_id=tile_id):
            log(f"  ❌ {context_label}: Add to Prompt 失败 ({uuid[:16]}...)", "GoogleFX")
            continue
        ready, ready_sel = _wait_for_flow_reference_ready(
            page,
            timeout_seconds=15,
            settle_range=(0.5, 1.0),
        )
        if ready:
            log(f"  ✅ {context_label}: 已挂入提示词框 (sel={ready_sel!r})", "GoogleFX")
            mounted.append(uuid)
        else:
            log(f"  ⚠️ {context_label}: 挂载后未检测到就绪信号 ({uuid[:16]}...)", "GoogleFX")

    return mounted


# ── _wait_for_prompt_reference_change ──
def _wait_for_prompt_reference_change(page, previous_refs=None, expected_uuid: str = "", timeout_seconds: int = 12):
    """等待 Prompt 参考图列表发生真实变化，而不是只依赖菜单点击成功。"""
    before = [item for item in (previous_refs or []) if item]
    expected = (expected_uuid or "").strip().lower()
    before_norm = [item.lower() for item in before]
    deadline = time.time() + max(timeout_seconds, 1)

    while time.time() < deadline:
        current = _get_prompt_reference_uuids(page, limit=max(len(before) + 3, 4))
        current_norm = [item.lower() for item in current]
        if expected and expected in current_norm and current != before:
            return True, current
        if expected and expected in current_norm and expected in before_norm:
            return True, current
        if len(current) > len(before):
            return True, current
        if not expected and current != before:
            return True, current
        time.sleep(0.5)

    return False, _get_prompt_reference_uuids(page, limit=max(len(before) + 3, 4))


# ── _clear_prompt_reference_chips_video ──
# 2026-07-20: 与下方 _clear_prompt_reference_chips_image 是两份独立、行为不同的实现
# (JS dispatchEvent 单趟 vs Playwright locator + max_rounds 循环)，分别被视频/图片
# 生成流程实际调用。拆分搬移时按用户决定改名共存，不合并逻辑，避免真机行为回归。
def _clear_prompt_reference_chips_video(page):
    """只清空 Prompt bar 内的历史文字和参考图，不扫描或点击画布卡片。"""
    return _clear_prompt_reference_chips_image(page)


# ── _clear_existing_uploaded_frame ──
def _clear_existing_uploaded_frame(page, label: str):
    """兼容旧调用的安全空操作；清理只允许发生在 Prompt bar 内。"""
    return False


def _slot_container_has_thumbnail(container) -> bool:
    """帧槽位容器里是否已经出现缩略图（= 图片真的挂上去了）。"""
    for sel in ("img", "video", "canvas"):
        try:
            if container.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _upload_to_slot_directly(page, label: str, file_path: str, refs_before=None,
                             verify_timeout: int = 20) -> bool:
    """直接将本地图片上传到指定帧槽位 (Start/End)。

    ⚠️ set_input_files 成功 ≠ 图片挂上了槽位：这里定位槽位容器靠的是文案匹配、
    file input 拿的是页面上第一个 input[type=file]，两者都可能指错对象，而错了
    不会抛异常。之前这个函数只要 set_input_files 没报错就返回 True，调用方据此
    报"挂载完成 (2/2)"，接着提交一个**根本没有首尾帧**的请求——Flow 照单全收，
    按纯文本生成一段无关片段（文生视频），直到下载后锚点比对才被拒收。
    所以上传后必须实测槽位里出现了缩略图（或提示词区参考图数量确实变多），
    验不到就如实返回 False，让调用方走挂载失败的既有失败/重试路径。
    """
    selector = 'div[type="button"][aria-haspopup="dialog"], [aria-haspopup="dialog"], .jekiem, .EGCPj'
    containers = page.locator(selector)
    count = containers.count()

    target_container = None
    labels_to_try = {
        "Start": ["Start", "起始"],
        "End":   ["End",   "结束"],
    }.get(label, [label])

    for i in range(count):
        el = containers.nth(i)
        try:
            txt = el.inner_text().strip()
            if any(lbl in txt for lbl in labels_to_try):
                target_container = el
                break
        except Exception:
            pass

    if not target_container:
        log(f"  ❌ 未找到 {label} 帧槽位容器", "GoogleFX")
        return False

    try:
        target_container.click(force=True)
        random_sleep(1.0, 1.5)
    except Exception as e:
        log(f"  ❌ 点击 {label} 帧槽位容器失败: {e}", "GoogleFX")
        return False

    try:
        upload_menu_item = page.locator("button[role='menuitem']:has-text('上传'), button[role='menuitem']:has-text('Upload')").first
        if upload_menu_item.is_visible(timeout=1500):
            upload_menu_item.click(force=True)
            random_sleep(0.8, 1.2)
    except Exception:
        pass

    try:
        file_input = page.locator("input[type='file']").first
        if not file_input or file_input.count() == 0:
            log(f"  ❌ 未找到 {label} 上传对应的 file input", "GoogleFX")
            return False
        abs_path = os.path.abspath(file_path)
        file_input.set_input_files(abs_path)
        log(f"  ✅ {label} 槽位已设置输入文件: {os.path.basename(file_path)}", "GoogleFX")
        random_sleep(4.0, 6.0)  # 等待上传并就绪
    except Exception as e:
        log(f"  ❌ {label} 槽位上传文件异常: {e}", "GoogleFX")
        return False

    before_count = len([u for u in (refs_before or []) if u])
    deadline = time.time() + max(verify_timeout, 1)
    while time.time() < deadline:
        if _slot_container_has_thumbnail(target_container):
            log(f"  ✅ {label} 槽位已确认出现缩略图", "GoogleFX")
            return True
        if len(_get_prompt_reference_uuids(page, limit=max(before_count + 2, 2))) > before_count:
            log(f"  ✅ {label} 槽位上传后提示词区参考图数量已增加，判定挂载成功", "GoogleFX")
            return True
        time.sleep(1)

    log(
        f"  ❌ {label} 槽位上传后 {verify_timeout}s 内未见缩略图/参考图变化，"
        f"判定未真正挂载（不能带着空首尾帧提交）",
        "GoogleFX",
    )
    return False


# ── _mount_video_prompt_refs ──
def _mount_video_prompt_refs(page, start_ref: str = "", end_ref: str = "", start_path: str = "",
                             end_path: str = "", result_meta=None):
    """
    视频参考图的语义顺序固定为 Start -> End。
    Flow 当前 UI 保持插入顺序（先添加的在前），因此直接按语义顺序
    (Start→End) 挂载即可。仅在首选策略失败时才回退到反序尝试。

    result_meta: 可选 dict，回填本次挂载的实况供调用方在点 Generate 前复核：
      strategy —— 'prompt_chips'（画布卡片 Add to Prompt）/ 'frame_slots'
                  （回退：直接把本地文件传进 Start/End 槽位）/ ''（失败）
      expected —— 期望的画布 UUID 顺序（frame_slots 回退路径下不适用）
      refs     —— 挂载完成瞬间提示词区实际读到的参考图 UUID 顺序
    """
    meta = result_meta if isinstance(result_meta, dict) else {}
    meta.update({"strategy": "", "expected": [], "refs": []})

    _clear_prompt_reference_chips_video(page)

    semantic_refs = [ref for ref in [start_ref, end_ref] if str(ref or "").strip()]
    expected_uuids = [_extract_flow_image_uuid(ref) for ref in semantic_refs if _extract_flow_image_uuid(ref)]
    meta["expected"] = list(expected_uuids)
    if not semantic_refs:
        return []

    attempts = []
    if len(semantic_refs) == 2:
        attempts.append(("semantic_order", semantic_refs))
        reverse_refs = [ref for ref in [end_ref, start_ref] if str(ref or "").strip()]
        if reverse_refs != semantic_refs:
            attempts.append(("reverse_fallback", reverse_refs))
    else:
        attempts.append(("single_ref", semantic_refs))

    last_actual = []
    last_mounted = []
    for idx, (strategy_name, refs_to_mount) in enumerate(attempts, start=1):
        if idx > 1:
            _clear_prompt_reference_chips_video(page)

        log(f"🧭 视频参考图挂载策略: {strategy_name}", "GoogleFX")
        mounted = _mount_flow_images_to_prompt(
            page,
            refs_to_mount,
            context_label=f"视频参考卡片[{strategy_name}]",
        )
        actual_order = _get_prompt_reference_uuids(page, limit=len(expected_uuids) or 1)
        last_actual = actual_order
        last_mounted = mounted

        log(
            f"🧭 视频参考图顺序校验 | expected={expected_uuids} | actual={actual_order}",
            "GoogleFX",
        )

        if len(expected_uuids) == 1:
            if actual_order[:1] == expected_uuids[:1]:
                meta.update({"strategy": "prompt_chips", "refs": list(actual_order)})
                return mounted
        elif actual_order[:len(expected_uuids)] == expected_uuids:
            meta.update({"strategy": "prompt_chips", "refs": list(actual_order)})
            return mounted

    log(
        f"⚠️ 视频参考图顺序校验失败，尝试通过直接上传到 Start/End 槽位进行挂载... | expected={expected_uuids} | actual={last_actual} | mounted={last_mounted}",
        "GoogleFX",
    )
    _clear_prompt_reference_chips_video(page)

    # 回退路径要传几张、就必须验到几张。少验一张就等于放一段没有首尾帧的请求过去。
    wanted_slots = [
        ("Start", start_path, start_ref),
        ("End", end_path, end_ref),
    ]
    wanted_slots = [(lbl, p, ref) for lbl, p, ref in wanted_slots if p and os.path.exists(p)]

    slot_mounted = []
    for label, path_, ref in wanted_slots:
        refs_before = _get_prompt_reference_uuids(page, limit=4)
        if _upload_to_slot_directly(page, label, path_, refs_before=refs_before):
            slot_mounted.append(_extract_flow_image_uuid(ref) or f"{label.lower()}_uploaded")

    if wanted_slots and len(slot_mounted) == len(wanted_slots):
        log(f"✅ 槽位直接上传成功: {slot_mounted}", "GoogleFX")
        meta.update({
            "strategy": "frame_slots",
            "refs": _get_prompt_reference_uuids(page, limit=max(len(slot_mounted), 2)),
        })
        return slot_mounted

    if slot_mounted:
        # 只成了一半：宁可整段判失败重来，也不能提交一个只有首帧（或只有尾帧）的
        # 请求——Flow 会自行脑补另一端，产出的片段接不上相邻镜头。
        log(
            f"❌ 槽位直接上传只成功 {len(slot_mounted)}/{len(wanted_slots)} 张，判定挂载失败",
            "GoogleFX",
        )
    return []


# ── _upload_image_to_canvas_and_mount ──
def _upload_image_to_canvas_and_mount(page, local_path: str, timeout: int = 60):
    """
    回退策略：当画布上找不到参考图 UUID 时（如页面刷新后画布清空），
    通过 Create (add_2) → Upload 按钮将本地图片上传到画布，
    等待新图出现后自动 Add to Prompt。

    成功时返回**新上传那张图在画布上的 UUID**（非空字符串，对 `if ok:` 与旧的
    布尔用法完全兼容），失败返回 False。

    2026-08-23：此前这里成功也只返回 True，等于把刚拿到的 UUID 扔了。上传是为了
    补一张画布上没有的参考图，补完它就**在**画布上了——不把 UUID 交回去，调用方
    无从留档，下一次引用同一张图还是找不到 UUID、还是走上传。实测代价：某一帧的
    中选候选没能从 URL 解析出 UUID（留档落成 img_NNN_nouuid.jpg）之后，它的下一帧
    每次渲染、每次「修复此帧问题」都要重传一遍同一张图，永远好不了。
    """
    if not local_path or not os.path.exists(local_path):
        log(f"  ❌ 上传回退: 文件不存在 {local_path}", "GoogleFX")
        return False

    log(f"🔄 参考图回退: 通过上传方式挂载 {os.path.basename(local_path)}", "GoogleFX")

    known_uuids = _get_panel_uuids(page)

    add2_btn = _find_add2_btn(page)
    if not add2_btn:
        log("  ❌ 上传回退: 未找到 Create (add_2) 按钮", "GoogleFX")
        return False

    try:
        add2_btn.click()
        random_sleep(1.0, 1.5)
        log("  ✅ 已点击 Create 按钮", "GoogleFX")
    except Exception as e:
        log(f"  ❌ 上传回退: 点击 Create 失败: {e}", "GoogleFX")
        return False

    uploaded = False
    try:
        upload_sels = [
            "button:has-text('Upload')", "button:has-text('上传')",
            "[role='button']:has-text('Upload')", "[role='button']:has-text('上传')",
            "div[class*='upload']", "label:has-text('Upload')",
            "button[aria-label*='Upload']", "button[aria-label*='上传']",
            "button[role='menuitem']:has-text('上传')",
            "button[role='menuitem']:has-text('Upload')",
        ]
        for _up_sel in upload_sels:
            try:
                _matches = page.locator(_up_sel)
                for _idx in range(_matches.count()):
                    _el = _matches.nth(_idx)
                    if _el.is_visible(timeout=2000):
                        _el.click(force=True)
                        log(f"  ✅ 已点击 Upload 触发区域 ({_up_sel!r})", "GoogleFX")
                        random_sleep(0.8, 1.5)
                        break
                else:
                    continue
                break
            except Exception:
                continue

        file_input = None
        for _fi_sel in ["input[type='file']", "input[accept*='image']"]:
            try:
                _fi = page.locator(_fi_sel).first
                if _fi.count() > 0:
                    file_input = _fi
                    break
            except Exception:
                pass

        if file_input:
            abs_path = os.path.abspath(local_path)
            file_input.set_input_files(abs_path)
            log(f"  ✅ set_input_files: {os.path.basename(abs_path)}", "GoogleFX")
            uploaded = True
        else:
            log("  ❌ 上传回退: 未找到 file input", "GoogleFX")
            _safe_press_escape(page, "上传回退 file input 未找到")
            return False

    except Exception as e:
        log(f"  ❌ 上传回退: 上传操作失败: {e}", "GoogleFX")
        _safe_press_escape(page, "上传回退异常")
        return False

    if not uploaded:
        _safe_press_escape(page, "上传回退未完成")
        return False

    log("  ⏳ 等待上传图片出现在画布...", "GoogleFX")
    new_uuid = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_cancelled()   # 默认要等满 60s，取消得能在轮之间生效
        cur_uuids = _get_panel_uuids(page)
        new_uuids = cur_uuids - known_uuids
        if new_uuids:
            new_uuid = next(iter(new_uuids))
            log(f"  🎉 上传图片已出现: UUID={new_uuid[:16]}...", "GoogleFX")
            break
        time.sleep(2)

    if not new_uuid:
        log("  ❌ 上传回退: 等待超时，未检测到新图", "GoogleFX")
        _safe_press_escape(page, "上传回退超时")
        return False

    _safe_press_escape(page, "上传回退关闭对话框")
    random_sleep(0.5, 1.0)

    _ok = _add_flow_image_to_prompt(page, new_uuid)
    if _ok:
        log(f"  ✅ 上传回退成功: 参考图已挂入 Prompt (UUID={new_uuid[:16]}...)", "GoogleFX")
        # 返回 UUID 而不是 True：调用方据此把这张图留档成 img_NNN_<uuid>.jpg，
        # 下一次引用它就能直接挂画布 tile，不必再传一遍。
        return new_uuid
    else:
        log(f"  ❌ 上传回退: 图片已上传到画布但 Add to Prompt 失败", "GoogleFX")
        return False


# ── 提示词区分性切片 ──
def _clean_alnum(text):
    return re.sub(r'[^a-zA-Z0-9]', '', text or '').lower()


def _distinct_slices(prompts_map, slice_len=60):
    """为每个 tid 计算能区分彼此的提示词切片 {tid: slice}。

    🚨 2026-07-04 复盘根因：SPARK 所有视频段提示词都以同一段 boilerplate 开头
    （"Use the provided first frame and last frame as exact composition anchors..."），
    前 60 个字母数字字符完全相同。旧逻辑用"前 60 字符"做 tile 兜底匹配，等于所有
    任务都匹配同一批 tile —— 实测导致跨槽位甚至跨任务串片（loft 任务 vid_008 下载
    到了铁路隧道视频）。这里改为：去掉所有提示词的公共前缀后再取切片，保证切片
    只包含该段特有的内容（镜头/动作描述）。
    """
    if not prompts_map:
        return {}
    cleaned = {tid: _clean_alnum(p) for tid, p in prompts_map.items()}
    values = [v for v in cleaned.values() if v]
    if not values:
        return {tid: '' for tid in prompts_map}
    if len(values) >= 2:
        prefix_len = len(os.path.commonprefix(values))
    else:
        prefix_len = 0
    slices = {}
    for tid, v in cleaned.items():
        s = v[prefix_len:prefix_len + slice_len]
        if len(s) < 20:  # 公共前缀吃掉太多（或提示词过短）时回退到全文前缀
            s = v[:slice_len]
        slices[tid] = s
    return slices


# ── _inspect_all_pending_tiles ──
def _inspect_all_pending_tiles(page, tile_ids, prompts_map=None, slices_map=None):
    """批量扫描指定 tile_id 列表的生成状态，返回 {tile_id: {status, videoSrc, ...}}。

    slices_map: {tile_id: 区分性切片}。批量流程应传入基于全批次提示词计算的切片；
    不传时退化为按 prompts_map 内部对比计算（条目少时区分度有限）。
    """
    if not tile_ids:
        return {}
    if slices_map is None:
        slices_map = _distinct_slices(prompts_map or {})
    return page.evaluate("""([tileIds, slicesMap]) => {
        const results = {};
        const claimed = new Set();
        for (const tid of tileIds) {
            let tile = document.querySelector(`div[data-original-tile-id="${tid}"]`) ||
                       document.querySelector(`div[data-tile-id="${tid}"]`);

            if (!tile && slicesMap && slicesMap[tid]) {
                const cleanPrompt = slicesMap[tid];
                if (cleanPrompt) {
                    const allTiles = document.querySelectorAll('div[data-tile-id]');
                    for (const el of allTiles) {
                        // 不抢占已归属其他任务的 tile（同一次扫描或此前已被标记）
                        const stamped = el.getAttribute('data-original-tile-id');
                        if (stamped && stamped !== tid) continue;
                        const domId = el.getAttribute('data-tile-id');
                        if (claimed.has(domId)) continue;
                        const tileText = (el.innerText || '').replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
                        if (tileText.includes(cleanPrompt)) {
                            tile = el;
                            tile.setAttribute('data-original-tile-id', tid);
                            break;
                        }
                    }
                }
            }
            if (tile) claimed.add(tile.getAttribute('data-tile-id'));

            if (!tile) { results[tid] = {status:'missing',videoSrc:null,progress:null,failedText:null}; continue; }
            const text = (tile.innerText || '').toLowerCase();
            const videoEl = tile.querySelector('video');
            const sourceEl = videoEl ? videoEl.querySelector('source') : null;
            const videoSrc = (videoEl && (videoEl.currentSrc || videoEl.src)) || (sourceEl && sourceEl.src) || '';
            const thumbEl = tile.querySelector('img[alt="Video thumbnail"], img[alt="视频缩略图"]');
            const thumbSrc = thumbEl ? (thumbEl.currentSrc || thumbEl.src || '') : '';
            const progressMatch = (tile.innerText || '').match(/(\\d{1,3})\\s*%/);
            const hasProgress = progressMatch !== null;
            const hasCreditExhaustedText = (
                /\b(out of credits?|insufficient credits?|not enough credits?|credits? exhausted|credits? depleted|no credits? left|resource_exhausted|quota_exhausted|quota exceeded)\b/i.test(text)
                || /\b(out of (google )?flow credits?|insufficient (google )?flow credits?|not enough (google )?flow credits?|get (ai|flow) credits?)\b/i.test(text)
                || /\b(?:insufficient|out of|not enough|run out of)\s+(?:\w+\s+){0,3}credits?\b/i.test(text)
                || /\bget\s+(?:more\s+)?(?:ai\s+|flow\s+|google\s+flow\s+)?credits?\b/i.test(text)
                || /(?<!\d)0\s*(?:(?:google\s+)?flow\s+|ai\s+)?credits?\b/i.test(text)
                || /(?:credits?|credit\s+balance|flow\s+credits?|ai\s+credits?|积分|点数|额度|配额|余额)[:：=为是]\s*0(?!\d)/i.test(text)
                || /(积分不足|没有足够的积分|积分已用完|积分已耗尽|积分耗尽|积分用尽|点数不足|点数已用完|点数已耗尽|点数耗尽|额度不足|额度已用完|额度耗尽|配额不足|配额已用完|配额耗尽|无可用积分|无可用点数|没有可用积分|0\s*积分|0\s*点数)/.test(text)
                || /(?<!\d)0\s*(?:积分|点数)(?!\d)/.test(text)
            );
            const hasFailText = text.includes('failed') || text.includes('something went wrong')
                             || text.includes('unusual activity') || text.includes('help center')
                             || text.includes('出错了') || text.includes('生成失败')
                             || text.includes('失败') || text.includes('使用人数过多')
                             || hasCreditExhaustedText;
            const isVisible = (el) => {
                let cur = el;
                while (cur) {
                    const style = window.getComputedStyle(cur);
                    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                        return false;
                    }
                    cur = cur.parentElement;
                }
                return true;
            };
            const icons = Array.from(tile.querySelectorAll('i'));
            const hasWarningIcon = icons.some(i => {
                const t = (i.innerText || i.textContent || '').trim().toLowerCase();
                const isWarning = t === 'warning' || t === 'error' || t === 'error_outline';
                return isWarning && isVisible(i);
            });
            const failed = (hasFailText && hasWarningIcon) || hasCreditExhaustedText;
            // 🔧 2026-07-04: 收紧 IP 封禁判定。'help center'/'帮助中心' 是 Flow 所有失败
            // 卡片都会带的通用链接，此前把它算作 IP 被封的证据，导致普通生成失败也被
            // 当成封 IP → 整批中止 + 换 IP 重跑 → 大量重复提交。只认 unusual activity。
            const isIpBlocked = failed && (
                text.includes('unusual activity') || text.includes('异常活动')
            );
            const isCreditExhausted = failed && hasCreditExhaustedText;
            let status;
            if (videoSrc) {
                status = 'done';
            } else if (hasProgress || thumbSrc) {
                status = 'generating';
            } else if (failed) {
                status = 'failed';
            } else {
                status = 'generating';
            }
            results[tid] = {
                status: status,
                videoSrc: videoSrc || null,
                progress: (progressMatch ? Number(progressMatch[1]) : null),
                failedText: (status === 'failed') ? (tile.innerText || '') : null,
                isIpBlocked: isIpBlocked,
                isCreditExhausted: isCreditExhausted,
            };
        }
        return results;
    }""", [tile_ids, slices_map])


# ── _scan_canvas_tiles ──
def _scan_canvas_tiles(page):
    """扫描画布上全部 tile，按文档顺序返回
    [{tileId, originalTileId, textClean, videoSrc, failed}]。
    用于换 IP 重试后认领此前已提交且已生成完成的任务，避免重复提交。"""
    try:
        return page.evaluate(r"""() => {
            return Array.from(document.querySelectorAll('div[data-tile-id]')).map(el => {
                const videoEl = el.querySelector('video');
                const sourceEl = videoEl ? videoEl.querySelector('source') : null;
                const videoSrc = (videoEl && (videoEl.currentSrc || videoEl.src)) || (sourceEl && sourceEl.src) || '';
                const text = (el.innerText || '');
                const lower = text.toLowerCase();
                const failed = lower.includes('failed') || lower.includes('something went wrong')
                            || lower.includes('unusual activity') || lower.includes('生成失败')
                            || lower.includes('异常活动');
                return {
                    tileId: el.getAttribute('data-tile-id'),
                    originalTileId: el.getAttribute('data-original-tile-id') || null,
                    textClean: text.replace(/[^a-zA-Z0-9]/g, '').toLowerCase(),
                    videoSrc: videoSrc || null,
                    failed: failed,
                };
            });
        }""")
    except Exception as e:
        log(f"⚠️ _scan_canvas_tiles 失败: {type(e).__name__}: {e}", "GoogleFX")
        return []


# ── _wait_for_new_tile_id ──
def _wait_for_new_tile_id(page, before_tile_ids, timeout=20, expect_slice=None):
    """Generate 后等待画布出现新 data-tile-id，返回该 ID；超时返回 None。

    expect_slice: 该任务提示词的区分性切片（见 _distinct_slices）。提供时优先返回
    文本包含该切片的新 tile —— 防止同时出现多个新 tile（React 重渲染旧卡片换 id 等）
    时随手拿了别的任务的卡片（2026-07-04 复盘中 vid_009/010 内容整体错位的可疑机制）。
    """
    deadline = time.time() + timeout
    before_set = set(before_tile_ids or [])
    fallback_id = None
    while time.time() < deadline:
        tiles = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('div[data-tile-id]')).map(el => ({
                id: el.getAttribute('data-original-tile-id') || el.getAttribute('data-tile-id'),
                textClean: (el.innerText || '').replace(/[^a-zA-Z0-9]/g, '').toLowerCase(),
            }));
        }""")
        new_tiles = [t for t in tiles if t['id'] and t['id'] not in before_set]
        if new_tiles:
            if expect_slice:
                matched = [t for t in new_tiles if expect_slice in t['textClean']]
                if matched:
                    return matched[-1]['id']
                # 暂未匹配到文本（tile 可能尚未渲染提示词），记下候选继续等
                fallback_id = new_tiles[-1]['id']
            else:
                return new_tiles[-1]['id']
        time.sleep(0.5)
    if fallback_id:
        log(f"⚠️ 新 tile 文本未匹配到提示词切片，回退使用最新 tile {fallback_id[:16]}...", "GoogleFX")
    return fallback_id


# ── _fill_prompt_text ──
def _fill_prompt_text(page, input_el, prompt, has_refs=False):
    """向提示词输入框写入文本（复用 _generate_video_google_fx 中的多策略逻辑）。返回 True 表示成功。"""
    filled = False

    if has_refs:
        for attempt_label, fn in [
            ("insert_text", lambda: (
                input_el.click(), random_sleep(0.2, 0.3),
                page.keyboard.press("End"), random_sleep(0.1, 0.2),
                page.keyboard.insert_text(prompt), random_sleep(0.3, 0.5),
            )),
            ("execCommand", lambda: (
                input_el.click(), random_sleep(0.2, 0.3),
                page.evaluate("""(text) => {
                    const ed = document.querySelector('[data-slate-editor="true"]');
                    if (!ed) return; ed.focus();
                    const s = window.getSelection();
                    if (s && s.rangeCount) s.getRangeAt(0).collapse(false);
                    document.execCommand('insertText', false, text);
                }""", prompt), random_sleep(0.3, 0.5),
            )),
        ]:
            if filled:
                break
            try:
                fn()
                editor_text = input_el.inner_text().strip()
                if prompt[:15].lower() in editor_text.lower():
                    filled = True
                    log(f"✅ {attempt_label} 追加提示词成功: {len(prompt)} 字符", "GoogleFX")
            except Exception as e:
                log(f"⚠️ {attempt_label} 追加提示词失败: {e}", "GoogleFX")
    else:
        try:
            input_el.click()
            random_sleep(0.3, 0.5)
            page.keyboard.press("ControlOrMeta+a")
            random_sleep(0.1, 0.2)
            page.keyboard.press("Backspace")
            random_sleep(0.2, 0.3)
            page.evaluate("""(text) => {
                const ed = document.querySelector('[data-slate-editor="true"]');
                if (ed) { ed.focus(); ed.dispatchEvent(new InputEvent('beforeinput',
                    {inputType:'insertText',data:text,bubbles:true,cancelable:true,composed:true})); }
            }""", prompt)
            random_sleep(0.5, 0.8)
            slate_text = page.evaluate("""() => {
                const ed = document.querySelector('[data-slate-editor="true"]');
                return ed ? ed.textContent.trim() : '';
            }""")
            if slate_text and prompt[:15] in slate_text:
                filled = True
                log(f"✅ Slate insertText 成功: {len(prompt)} 字符", "GoogleFX")
        except Exception as e:
            log(f"⚠️ Slate insertText 失败: {e}", "GoogleFX")

        if not filled:
            try:
                input_el.click()
                random_sleep(0.2, 0.3)
                page.keyboard.press("ControlOrMeta+a")
                page.keyboard.press("Backspace")
                random_sleep(0.2, 0.3)
                page.keyboard.type(prompt, delay=20)
                filled = True
                log(f"✅ keyboard.type() 成功: {len(prompt)} 字符", "GoogleFX")
            except Exception as e:
                log(f"⚠️ keyboard.type() 失败: {e}", "GoogleFX")

        if not filled:
            try:
                input_el.click()
                random_sleep(0.2, 0.3)
                page.evaluate("""(t) => { navigator.clipboard.writeText(t); }""", prompt)
                page.keyboard.press("ControlOrMeta+v")
                random_sleep(0.5, 1.0)
                filled = True
                log(f"✅ 剪贴板粘贴尝试完成", "GoogleFX")
            except Exception as e:
                log(f"⚠️ 剪贴板粘贴失败: {e}", "GoogleFX")

    return filled


def _read_prompt_refs_settled(page, limit, attempts=3, settle=0.6):
    """复核专用读数：一次读空不算数，稳定读空才算数。

    写完提示词（视频链动辄两千多字）之后 Slate 会整棵重建，chip 与文本在同一棵树上，
    重建中途读到的空列表是**渲染过程**，不是「参考图掉了」。而这道复核的判负代价是
    整个片段作废重试，所以宁可多花一秒复读几次：只要任一次读到了参考图就以它为准，
    连着几次都读不到才认账。返回最后一次（或首个非空的）读数。
    """
    state = {"uuids": [], "scope": "", "ok": False}
    for attempt in range(max(attempts, 1)):
        state = read_prompt_reference_state(page, limit=limit)
        if state["uuids"]:
            return state
        if attempt < max(attempts, 1) - 1:
            time.sleep(settle)
    return state


def _video_refs_still_attached(page, mount_meta):
    """点 Generate 之前的最后一道复核：首尾帧是不是还挂在提示词框上。

    挂载成功和真正提交之间还隔着「关残留弹窗 → 写提示词 → 同步 React state」几步，
    每一步都可能把参考图 chip 挤掉（Slate 编辑器里 chip 和文本是同一棵树）。
    而 Flow 对"没有参考图的视频请求"不报错，它会安静地按纯文本生成一段无关片段。
    与其等下载后靠锚点 MAD 拒收（额度已经烧掉了），不如提交前再读一次 DOM。

    ⚠️ 判负必须建立在「确实读到了输入区、里面确实没有参考图」之上。读数本身失败
    （page.evaluate 抛异常）时既不能放行也不能当成掉图——放行会提交一段没有首尾帧的
    片段，当掉图则会把好好的片段判死。这种情况下只重试，读不出来就保持沉默地放行，
    交给下载后的锚点校验兜底：那是唯一还能拿到真实证据的环节。
    """
    meta = mount_meta or {}
    expected = [u for u in (meta.get("expected") or []) if u]
    if meta.get("strategy") == "prompt_chips" and expected:
        state = _read_prompt_refs_settled(page, limit=max(len(expected), 2))
        actual = state["uuids"]
        if actual[:len(expected)] == expected:
            return True
        if not state["ok"]:
            log(f"⚠️ 提交前复核：读不到输入区（DOM 扫描失败），按挂载时的校验结果放行，"
                f"改由下载后的锚点校验兜底 | expected={expected}", "GoogleFX")
            return True
        log(f"🚨 提交前复核：提示词区参考图已变化 | expected={expected} | actual={actual}"
            f" | scope={state['scope']}", "GoogleFX")
        return False

    refs = [u for u in (meta.get("refs") or []) if u]
    if refs:
        state = _read_prompt_refs_settled(page, limit=max(len(refs), 2))
        actual = state["uuids"]
        if len(actual) >= len(refs):
            return True
        if not state["ok"]:
            log(f"⚠️ 提交前复核：读不到输入区，按挂载时的校验结果放行，"
                f"改由下载后的锚点校验兜底 | 挂载时={len(refs)}", "GoogleFX")
            return True
        log(f"🚨 提交前复核：提示词区参考图数量减少 | 挂载时={len(refs)} | 现在={len(actual)}"
            f" | scope={state['scope']}", "GoogleFX")
        return False

    # frame_slots 回退路径下槽位缩略图不一定落在提示词区选择器的取值范围内，
    # 拿不到可比对的依据就不做二次判定（该路径的落地已在上传时逐张验过）。
    return True


# ── _submit_video_to_canvas ──
def _submit_video_to_canvas(page, req, before_tile_ids, expect_slice=None):
    """
    在画布上提交一个视频生成任务（不等待完成）。
    流程: 清理旧 chips → 挂载首尾帧 → 写提示词 → Generate → 等新 tile 出现
    返回 {"tile_id": str, "click_time": float}。
    挂载失败时抛 RuntimeError("CANVAS_MOUNT_FAILED:...")。
    expect_slice: 该任务提示词的区分性切片，用于核对新 tile 归属。
    """
    img_path = clean_path(req.image) if req.image else ""
    end_img_path = clean_path(req.end_image) if (hasattr(req, "end_image") and req.end_image) else ""
    has_start = bool(img_path)
    has_end = bool(end_img_path)

    # 声明了锚点帧却没有可挂载的画布引用 = 这一提交会变成纯文生视频。上游
    # (_submit_tasks 的锚点闸门) 已经拦过一道，这里是最后一道，防止将来新增调用方
    # 绕过闸门又把这个坑踩回来。
    declared_start = str(getattr(req, "original_image", "") or "").strip()
    declared_end = str(getattr(req, "original_end_image", "") or "").strip()
    if (declared_start and not has_start) or (declared_end and not has_end):
        raise RuntimeError(
            "CANVAS_MOUNT_FAILED:该片段声明了锚点帧但没有可挂载的画布引用，"
            "拒绝按无首尾帧的文生视频提交"
        )

    log(f"📤 提交任务: {req.prompt[:40]}... | 首帧={has_start} | 尾帧={has_end}", "GoogleFX")

    try:
        page.wait_for_timeout(500)
        # 🚨 之前这里用 page.locator('button:has(i:has-text("close"))') 无范围限制地扫描
        # 整个页面并点击——批量模式下，之前提交的任务此时可能仍在画布卡片(data-tile-id)里
        # 生成中，而 Flow 的"停止/取消生成"按钮同样用了 close 图标。结果是：提交第 2/3/4/5
        # 个任务时，会误点到还在生成中的前一个任务卡片的取消按钮，导致那个视频被我们自己的
        # 脚本悄悄取消掉——但 Flow 后端有时仍会把它生成完，只是前端要手动刷新一次才会同步，
        # 而 SPARK 这边早已按失败/超时处理并跳过了该视频。这里明确排除任何
        # [data-tile-id] 画布卡片内部的按钮，只清理提示词/工具栏区域残留的弹窗按钮。
        closed_count = page.evaluate("""() => {
            const isVisible = (el) => !!el && el.offsetParent !== null;
            const inCanvasTile = (el) => !!el.closest('div[data-tile-id]');
            const buttons = Array.from(document.querySelectorAll('button')).filter((btn) => {
                if (!isVisible(btn)) return false;
                if (inCanvasTile(btn)) return false;
                const icons = Array.from(btn.querySelectorAll('i'))
                    .map((i) => (i.textContent || '').trim().toLowerCase());
                return icons.includes('close');
            });
            for (const btn of buttons) {
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }
            return buttons.length;
        }""")
        if closed_count:
            log(f"🧹 已关闭 {closed_count} 个残留弹窗（已排除画布卡片，避免误取消生成中任务）", "GoogleFX")
            random_sleep(0.3, 0.5)
    except Exception:
        pass
    _clear_prompt_reference_chips_video(page)
    random_sleep(0.3, 0.5)

    prompt_has_refs = False
    mount_meta = {}
    if has_start or has_end:
        start_ref = req.image or img_path if has_start else ""
        end_ref = req.end_image or end_img_path if has_end else ""

        # 🔧 2026-07-03: 提取本地图片真实路径，以备在画布未命中时回退到直接对槽位进行文件上传
        start_local = ""
        end_local = ""

        def _is_local_path(val):
            if not val:
                return False
            val_str = str(val).strip()
            return os.path.exists(val_str) or "\\" in val_str or "/" in val_str or ":" in val_str

        if _is_local_path(start_ref):
            start_local = start_ref
        elif hasattr(req, "original_image") and req.original_image:
            start_local = req.original_image

        if _is_local_path(end_ref):
            end_local = end_ref
        elif hasattr(req, "original_end_image") and req.original_end_image:
            end_local = req.original_end_image

        mounted = _mount_video_prompt_refs(
            page,
            start_ref=start_ref,
            end_ref=end_ref,
            start_path=start_local,
            end_path=end_local,
            result_meta=mount_meta,
        )
        expected = min(len([r for r in [start_ref, end_ref] if str(r or "").strip()]), 2)
        prompt_has_refs = expected > 0 and len(mounted) >= expected
        if not prompt_has_refs:
            raise RuntimeError(f"CANVAS_MOUNT_FAILED:画布卡片挂载失败 ({len(mounted)}/{expected})")
        log(f"✅ 参考卡片挂载完成 ({len(mounted)}/{expected})", "GoogleFX")

    input_el = _find_fx_prompt_input(page, announce=False)
    if not input_el:
        raise RuntimeError("无法找到视频提示词输入框")
    if not _fill_prompt_text(page, input_el, req.prompt, has_refs=prompt_has_refs):
        raise RuntimeError("视频提示词输入失败")
    random_sleep(0.5, 1.0)

    try:
        input_el.click()
        random_sleep(0.1, 0.2)
        page.keyboard.press("End")
        if prompt_has_refs:
            page.keyboard.type(" ")
            random_sleep(0.15, 0.25)
        else:
            page.keyboard.type(" ")
            random_sleep(0.1, 0.15)
            page.keyboard.press("Backspace")
            random_sleep(0.2, 0.3)
    except Exception as e:
        log(f"⚠️ React state 同步失败: {type(e).__name__}", "GoogleFX")

    # 🚧 提交前最后一道锚点复核（见 _video_refs_still_attached）。放在节奏闸门之前：
    # 复核不过就没必要再等那几秒的提交间隔了。
    if prompt_has_refs and not _video_refs_still_attached(page, mount_meta):
        raise RuntimeError(
            "CANVAS_MOUNT_FAILED:提交前复核发现首尾帧已从提示词框丢失，"
            "拒绝按无首尾帧的文生视频提交"
        )

    # 提交节奏闸门（2026-07-26 补齐）：两条链都会 note_fx_submit()，但此前只有图片链
    # 调 fx_pacing_wait()，视频批量的连续提交完全没有最小间隔约束。风控看的是**两次
    # 提交之间**隔了多久，所以闸门必须贴在提交动作前面，两条链共用同一个时间戳。
    fx_pacing_wait(*fx_pacing_bounds())

    click_time = time.time()
    click_fx_send_button(page, input_el)
    note_fx_submit()   # 与图片链共用提交节奏闸门的参照点
    log("✅ 已点击 Generate", "GoogleFX")

    new_tile_id = _wait_for_new_tile_id(page, before_tile_ids, timeout=20, expect_slice=expect_slice)
    if not new_tile_id:
        # deep=True：这一条正是 2026-08-24 漏判的现场——积分跑干时 Flow 点了
        # Generate 不给 tile，页面上却不一定有任何耗尽文案，非得去头像菜单实读。
        page_credit_err = detect_page_credit_exhaustion(page, deep=True)
        if page_credit_err:
            raise RuntimeError(f"INSUFFICIENT_CREDITS: {page_credit_err}")
        raise RuntimeError("Generate 后未检测到新 tile")
    log(f"🎯 新 tile: {new_tile_id[:16]}...", "GoogleFX")

    # 立即为新 tile 设置 data-original-tile-id，防止后续生成过程中 React/UI 更新其 ID 后丢失匹配
    try:
        page.evaluate(f"""(feId) => {{
            const el = document.querySelector(`div[data-tile-id="${{feId}}"]`);
            if (el) {{
                el.setAttribute('data-original-tile-id', feId);
            }}
        }}""", new_tile_id)
    except Exception as e:
        log(f"⚠️ 为新 tile 设置 data-original-tile-id 失败: {e}", "GoogleFX")

    return {"tile_id": new_tile_id, "click_time": click_time}


# ==============================================================================
# 🔒 Google FX 配置面板锁定簇 (find_fx_config_button / check_fx_config / fix_fx_config)
# — 2026-03-25 LOCKED，由 services/google_fx.py 整体搬移至此，函数体逐字未改动。
# 锁定范围: find_fx_config_button / check_fx_config / fix_fx_config
# ==============================================================================

_ARIA_CONTROLS_RATIO_MAP = {
    # 按宽高比文字
    "9:16":          "PORTRAIT",
    "16:9":          "LANDSCAPE",
    "1:1":           "SQUARE",
    "3:4":           "PORTRAIT_3_4",
    "4:3":           "LANDSCAPE_4_3",
    # 按方向关键词 (orientation string)
    "portrait":      "PORTRAIT",
    "landscape":     "LANDSCAPE",
    "square":        "SQUARE",
    "portrait_3_4":  "PORTRAIT_3_4",
    "landscape_4_3": "LANDSCAPE_4_3",
}

def _normalize_video_duration_label(duration):
    """Return Google FX duration labels like 6s from values such as 6, "6", or "6s"."""
    if duration is None:
        return ""
    text = str(duration).strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*s?", text)
    if not match:
        return ""
    number = match.group(1)
    if number.endswith(".0"):
        number = number[:-2]
    return f"{number}s"

def _click_video_duration_tab(page, panel_scope, duration_label):
    """Click a video duration tab such as 4s, 6s, 8s, or 10s."""
    root = panel_scope or page
    duration_label = _normalize_video_duration_label(duration_label) or str(duration_label)
    selectors = [
        "button[role='tab']",
        "button[aria-controls*='DURATION']",
        "button[aria-controls*='duration']",
        "button",
    ]
    for sel in selectors:
        try:
            _dur_btn = root.locator(sel).filter(
                has_text=re.compile(f"^{re.escape(duration_label)}$", re.I)
            ).first
            if _dur_btn.is_visible(timeout=1500):
                _dur_btn.click(force=True)
                random_sleep(0.4, 0.8)
                return f"{sel} + 精确匹配 ({duration_label})"
        except Exception:
            pass

    if _click_fx_tab(page, duration_label, scope=panel_scope):
        return "tab fallback"
    return ""

def _switch_video_submode(page, target_suffix, scope=None):
    """切换视频子模式 tab: VIDEO_FRAMES (帧/首尾帧) 或 VIDEO_REFERENCES (素材)。

    使用 aria-controls 尾值匹配 (Radix UI 固定业务值，最稳定)。
    target_suffix: 'VIDEO_FRAMES' | 'VIDEO_REFERENCES'
    返回 True 表示成功点击。
    """
    root = scope or page

    # ── 优先级 0: aria-controls 尾值精确匹配 (最稳定) ──
    for _sel in [
        f"[aria-controls$='-{target_suffix}']",
        f"[aria-controls*='-{target_suffix}']",
        f"button[role='tab'][aria-controls$='-{target_suffix}']",
    ]:
        try:
            _btn = root.locator(_sel).first
            if _btn.is_visible(timeout=2000):
                # 检查是否已经选中
                if _btn.get_attribute("data-state") == "active" or _btn.get_attribute("aria-selected") == "true":
                    log(f"  ✅ 视频子模式已是 {target_suffix}，无需切换", "GoogleFX")
                    return True
                _btn.click(force=True)
                random_sleep(0.5, 0.8)
                log(f"  ✅ 视频子模式切换成功 (sel={_sel!r})", "GoogleFX")
                return True
        except Exception as _e:
            log(f"  ⚠️ _switch_video_submode sel={_sel!r}: {type(_e).__name__}", "GoogleFX")

    # ── 优先级 1: 文字标签匹配 ──
    _label_map = {
        "VIDEO_FRAMES": ["帧", "Frames", "frames"],
        "VIDEO_REFERENCES": ["素材", "References", "references"],
    }
    for _lbl in _label_map.get(target_suffix, []):
        try:
            _tab = root.locator("button[role='tab']").filter(
                has_text=re.compile(f"^.*{re.escape(_lbl)}.*$", re.I)
            ).first
            if _tab.is_visible(timeout=1500):
                if _tab.get_attribute("data-state") == "active":
                    log(f"  ✅ 视频子模式已是 {_lbl}，无需切换", "GoogleFX")
                    return True
                _tab.click(force=True)
                random_sleep(0.5, 0.8)
                log(f"  ✅ 视频子模式切换成功 (label={_lbl!r})", "GoogleFX")
                return True
        except Exception:
            pass

    # ── 优先级 2: JS 兜底 ──
    try:
        clicked = page.evaluate("""(suffix) => {
            const tabs = Array.from(document.querySelectorAll("[role='tab'], button"));
            const target = tabs.find(t => {
                const ac = t.getAttribute('aria-controls') || '';
                return ac.endsWith('-' + suffix) || ac.includes('-' + suffix);
            });
            if (!target || target.offsetParent === null) return false;
            if (target.getAttribute('data-state') === 'active') return 'already';
            target.click();
            return true;
        }""", target_suffix)
        if clicked == "already":
            log(f"  ✅ 视频子模式已是 {target_suffix} (JS)", "GoogleFX")
            return True
        if clicked:
            random_sleep(0.5, 0.8)
            log(f"  ✅ 视频子模式切换成功 (JS fallback)", "GoogleFX")
            return True
    except Exception as _e:
        log(f"  ⚠️ _switch_video_submode JS fallback: {type(_e).__name__}", "GoogleFX")

    return False

def find_fx_config_button(page):
    """
    找到底部工具栏的配置状态按钮。
    真实状态按钮永远同时包含「模型名」和「数量 (x1-x4 或 1x-4x)」，
    而面板内的模型下拉按钮只含 arrow_drop_down。
    务必区分这两种按钮。
    """
    model_kws = ["Banana", "Nano", "Imagen", "Video", "Veo", "Pro", "视频", "图片"]
    count_kws = ["x1", "x2", "x3", "x4", "1x", "2x", "3x", "4x"]

    def _clean_btn_text(text):
        clean = re.sub(r"\s+", " ", (text or "")).strip()
        for noise in ["arrow_drop_down", "arrow_forward", "arrow_back", "▾", "▴"]:
            clean = clean.replace(noise, "").strip()
        return re.sub(r"\s+", " ", clean).strip()

    def _search():
        # 策略1: 同时含「模型关键词」和「数量」——这才是真实状态按钮
        for mkw in model_kws:
            for ckw in count_kws:
                try:
                    btn = page.locator("button").filter(has_text=mkw).filter(has_text=ckw)
                    if btn.count():
                        for i in range(btn.count()):
                            candidate = btn.nth(i)
                            if not candidate.is_visible():
                                continue
                            txt = _clean_btn_text(candidate.inner_text())
                            if ckw in txt and any(kw in txt for kw in model_kws):
                                log(f"  找到配置按钮 ('{mkw}'+'{ckw}'): '{txt}'", "GoogleFX")
                                return candidate, txt
                except: pass

        # 策略2: 只含数量，但同时授含模型关键词（避免单独 x1 误匹配选项按钮）
        for ckw in count_kws:
            try:
                btns = page.locator("button").filter(has_text=ckw)
                for i in range(btns.count()):
                    b = btns.nth(i)
                    if not b.is_visible(): continue
                    txt = _clean_btn_text(b.inner_text())
                    if ckw in txt and any(kw in txt for kw in model_kws):
                        log(f"  找到配置按钮 (count='{ckw}'): '{txt}'", "GoogleFX")
                        return b, txt
            except: pass

        # 策略3: 兼容备用——只接受真正像状态摘要 of 按钮
        for pattern in model_kws:
            try:
                btns = page.locator("button").filter(has_text=pattern)
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    if not btn.is_visible():
                        continue
                    txt = _clean_btn_text(btn.inner_text())
                    has_count = any(ckw in txt for ckw in count_kws)
                    has_ratio = any(token in txt for token in ["crop_", "16:9", "9:16", "4:3", "3:4", "1:1"])
                    is_video_summary = txt.startswith("Video ") or txt.startswith("Video") or txt.startswith("视频") or "veo" in txt.lower()
                    if has_count or has_ratio or is_video_summary:
                        log(f"  找到配置按钮 (fallback '{pattern}'): '{txt}'", "GoogleFX")
                        return btn, txt
            except: pass
        return None, ""

    res_btn, res_txt = _search()
    if res_btn:
        return res_btn, res_txt

    # 当前 UI 的 Agent/Agen/智能体模式会显示 article_spark + tune，并把真正的
    # "Video/Image · 比例 · xN" 配置摘要藏掉。tune 打开的是图二“智能体设置”
    # 侧栏，绝不是图一的模型配置面板。先关错侧栏、退出智能体模式，再重搜摘要。
    _close_agent_settings_sidebar(page, "GoogleFX")
    if _dismiss_active_agent_mode(page, "GoogleFX"):
        res_btn, res_txt = _search()
        if res_btn:
            return res_btn, res_txt

    return None, ""

def _orientation_tokens(orientation):
    tokens = ORIENT_ICON_MAP.get(orientation, orientation)
    if isinstance(tokens, str):
        tokens = [tokens]
    return [t for t in tokens if t]

def _normalize_fx_status_text(text):
    """归一化状态栏/菜单文本，降低换行、图标、emoji 对匹配的干扰。"""
    clean = text or ""
    for noise in ["arrow_drop_down", "arrow_forward", "arrow_back", "▾", "▴"]:
        clean = clean.replace(noise, " ")
    clean = re.sub(r"[^\w\s:\-\.\[\]/]", " ", clean, flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip().lower()
    return clean

def _matches_model_status(text, model):
    if not model:
        return True
    clean = _normalize_fx_status_text(text)
    target = _normalize_fx_status_text(model)
    if not target:
        return True
    if "omni" in model.lower():
        return "omni" in clean or (("video" in clean or "视频" in clean) and "veo" not in clean)
    if model.lower().startswith("veo"):
        aliases = {target}
        if "lite" in target:
            aliases.update({"veo 3 1 lite", "veo lite"})
        if "fast" in target:
            aliases.update({"veo 3 1 fast", "veo fast"})
        if "quality" in target:
            aliases.update({"veo 3 1 quality", "veo quality"})
        if "lower priority" in target:
            aliases.update({"lower priority"})
            if "lite" in target:
                aliases.update({"lite lower priority"})
        return any(alias and alias in clean for alias in aliases)
    aliases = {target}
    if "nano banana 2 lite" in target:
        aliases.update({"nano banana 2 lite", "banana 2 lite"})
    elif "nano banana 2" in target:
        # Lite 的名称包含完整的 "Nano Banana 2"；不先排除会把 Lite
        # 误判为普通 2，导致自动化跳过真正的模型切换。
        if "nano banana 2 lite" in clean or "banana 2 lite" in clean:
            return False
        aliases.update({"nano banana 2", "banana 2"})
    if "nano banana pro" in target:
        aliases.update({"nano banana pro", "banana pro"})
    return any(alias and alias in clean for alias in aliases)

def _click_fx_tab(page, label, scope=None):
    """点击新版 Flow 的 tab 按钮，并尽量确认选中态。"""
    root = scope or page
    label_norm = _normalize_fx_status_text(label)
    patterns = [
        ("[role='tab']", True),
        ("button", True),
    ]
    for tab_idx, (selector, exact) in enumerate(patterns):
        try:
            btns = root.locator(selector).filter(has_text=re.compile(re.escape(label), re.I))
            count = btns.count()
            for i in range(count):
                btn = btns.nth(i)
                if not btn.is_visible():
                    continue
                blob = " ".join(filter(None, [
                    btn.inner_text() or "",
                    btn.get_attribute("id") or "",
                    btn.get_attribute("aria-label") or "",
                    btn.get_attribute("aria-controls") or "",
                    btn.get_attribute("data-state") or "",
                ]))
                blob_norm = _normalize_fx_status_text(blob)
                if label_norm not in blob_norm:
                    continue
                btn.click(force=True)
                random_sleep(0.4, 0.8)
                selector_stats.record_hit("fx_tab", tab_idx, selector=selector, total=len(patterns))
                try:
                    if btn.get_attribute("aria-selected") == "true" or btn.get_attribute("data-state") == "active":
                        return True
                except Exception:
                    return True
                if exact:
                    return True
        except Exception:
            pass

    # JS 兜底：有些 tab 在 Playwright 文本定位下会被动画层拦截
    try:
        clicked = page.evaluate("""(targetLabel) => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const target = norm(targetLabel);
            const btns = Array.from(document.querySelectorAll("[role='tab'],button"));
            const match = btns.find((b) => {
                if (b.offsetParent === null) return false;
                const blob = [
                    b.innerText || '',
                    b.id || '',
                    b.getAttribute('aria-label') || '',
                    b.getAttribute('aria-controls') || '',
                ].join(' ');
                return norm(blob).includes(target);
            });
            if (!match) return false;
            match.click();
            return true;
        }""", label)
        if clicked:
            random_sleep(0.4, 0.8)
            return True
    except Exception:
        pass
    selector_stats.record_hit("fx_tab", -1, total=len(patterns))
    return False

def _get_open_fx_config_panel(page, trigger_btn=None):
    """锁定当前打开的配置面板，优先使用 aria-labelledby 关联到底部摘要按钮。"""
    button_id = ""
    aria_controls = ""
    try:
        if trigger_btn is not None:
            button_id = (trigger_btn.get_attribute("id") or "").strip()
            aria_controls = (trigger_btn.get_attribute("aria-controls") or "").strip()
    except Exception:
        pass

    fallback_selectors = UI_SELECTORS["google_fx"].get("config_panel_root", [])
    total_layers = 2 + len(fallback_selectors)

    if button_id:
        try:
            panel = page.locator(
                f"[role='menu'][data-state='open'][aria-labelledby='{button_id}']"
            ).first
            if panel.is_visible(timeout=1500):
                selector_stats.record_hit("fx_config_panel", 0, selector="aria-labelledby", total=total_layers)
                return panel
        except Exception:
            pass

    if aria_controls:
        try:
            panel = page.locator(f"[id=\"{aria_controls}\"]").first
            if panel.is_visible(timeout=1500):
                selector_stats.record_hit("fx_config_panel", 1, selector="aria-controls", total=total_layers)
                return panel
        except Exception:
            pass

    for layer_idx, sel in enumerate(fallback_selectors):
        try:
            panel = page.locator(sel).first
            if panel.is_visible(timeout=1500):
                selector_stats.record_hit("fx_config_panel", 2 + layer_idx, selector=sel, total=total_layers)
                return panel
        except Exception:
            pass

    selector_stats.record_hit("fx_config_panel", -1, total=total_layers)
    return None

def _find_fx_model_dropdown(page, scope=None, target_model=None):
    """定位配置面板中的模型下拉按钮。"""
    root = scope or _get_open_fx_config_panel(page) or page
    image_tokens = ["Banana", "Nano", "Imagen", "Pro", "Lite"]
    video_tokens = ["Veo", "Video", "Quality", "Fast", "Lite", "Lower Priority", "3.1", "Omni", "Flash"]
    target_lower = str(target_model or "").lower()
    wants_video = bool(target_model) and any(token in target_lower for token in ("veo", "omni", "video"))
    wants_image = bool(target_model) and any(token in target_lower for token in ("banana", "imagen", "image"))
    model_tokens = video_tokens if wants_video else (image_tokens if wants_image else image_tokens + video_tokens)

    def _looks_like_model_button(btn):
        try:
            if not btn.is_visible():
                return False
            txt = btn.inner_text() or ""
            desc = " ".join(filter(None, [txt, btn.get_attribute("aria-label") or "", btn.get_attribute("id") or ""]))
            clean = _normalize_fx_status_text(desc)
            if not clean:
                return False
            if any(x in clean for x in [" x1", " x2", " x3", " x4"]):
                return False
            if any(token in clean for token in ["16:9", "9:16", "4:3", "3:4", "1:1", "frames", "ingredients", "image", "video"]):
                if not any(kw.lower() in clean for kw in ["veo", "banana", "imagen", "omni"]):
                    return False
            is_video_button = any(token in clean for token in ("veo", "omni", "video"))
            is_image_button = any(token in clean for token in ("banana", "imagen"))
            if wants_video and not is_video_button:
                return False
            if wants_image and not is_image_button:
                return False
            return any(kw.lower() in clean for kw in [t.lower() for t in model_tokens])
        except Exception:
            return False

    # 优先: aria-haspopup='menu' + 模型关键词 (Radix UI 菜单触发按钮，最稳定)
    for kw in model_tokens:
        try:
            kw_btns = root.locator("button[aria-haspopup='menu']").filter(has_text=kw)
            for i in range(kw_btns.count()):
                btn = kw_btns.nth(i)
                if _looks_like_model_button(btn):
                    log(f"  🎯 模型下拉按钮 (aria-haspopup='menu' + '{kw}')", "GoogleFX")
                    selector_stats.record_hit("fx_model_dropdown", 0, selector="button[aria-haspopup='menu']", total=2)
                    return btn
        except Exception:
            pass

    for sel in [
        "button:has-text('arrow_drop_down')",
        "button[aria-haspopup='menu']",
        "[role='menu'] button",
        "[role='dialog'] button",
        "button",
    ]:
        try:
            btns = root.locator(sel)
            for i in range(btns.count()):
                btn = btns.nth(i)
                if _looks_like_model_button(btn):
                    selector_stats.record_hit("fx_model_dropdown", 1, selector=sel, total=2)
                    return btn
        except Exception:
            pass
    selector_stats.record_hit("fx_model_dropdown", -1, total=2)
    return None

def _get_fx_model_dropdown_text(page, scope=None, target_model=None):
    """读取配置面板内模型下拉当前文字。"""
    try:
        model_dd = _find_fx_model_dropdown(page, scope=scope, target_model=target_model)
        if model_dd and model_dd.is_visible(timeout=1200):
            return (model_dd.inner_text() or "").strip()
    except Exception:
        pass
    return ""

def _click_fx_menu_item(page, label, button_id_hint="", menu_id_hint=""):
    """点击配置面板中的模型菜单项。"""
    target = _normalize_fx_status_text(label)
    resolved_menu_id = _resolve_open_flow_menu_id(page, button_id_hint, menu_id_hint)
    menu_scope = None
    if resolved_menu_id:
        try:
            menu_scope = page.locator(f"[id=\"{resolved_menu_id}\"]").first
            if not menu_scope.is_visible(timeout=1200):
                menu_scope = None
        except Exception:
            menu_scope = None

    scope = menu_scope or page
    el, _ = _click_first_visible(scope, [
        f"[role='menuitem']:has-text('{label}')",
        f"[role='option']:has-text('{label}')",
        f"li:has-text('{label}')",
        f"button:has-text('{label}')",
        f"div:has-text('{label}')",
    ], timeout=1200, force=True, family="fx_menu_item")
    if el:
        random_sleep(0.5, 1)
        return True

    try:
        clicked = page.evaluate("""({ targetLabel, menuId }) => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const target = norm(targetLabel);
            const root = menuId ? document.getElementById(menuId) : document;
            const nodes = Array.from(root.querySelectorAll("[role='menuitem'],[role='option'],li,button,div"));
            const match = nodes.find((node) => {
                if (node.offsetParent === null) return false;
                const text = norm(node.innerText);
                return text === target || text.includes(target);
            });
            if (!match) return false;
            match.click();
            return true;
        }""", {"targetLabel": label, "menuId": resolved_menu_id})
        if clicked:
            random_sleep(0.5, 1)
            return True
    except Exception:
        pass

    # 兜底：按规范化文本扫描所有可见节点，兼容新 UI 把模型渲染为普通 div/button 的情况
    try:
        nodes = scope.locator("[role='menuitem'], [role='option'], li, button, div")
        for i in range(nodes.count()):
            node = nodes.nth(i)
            if not node.is_visible():
                continue
            text = _normalize_fx_status_text(node.inner_text() or "")
            if not text:
                continue
            if text == target or target in text:
                node.click(force=True)
                random_sleep(0.5, 1)
                return True
    except Exception:
        pass
    return False

def _matches_orientation_text(text, orientation):
    haystack = (text or "").lower()
    return any(token.lower() in haystack for token in _orientation_tokens(orientation))


def _generation_count_aliases(count):
    """Return the stable numeric key and every UI spelling for a count value."""
    target = _normalize_fx_status_text(count).replace(" ", "")
    match = re.fullmatch(r"(?:x(\d+)|(\d+)x)", target)
    aliases = {target}
    number = ""
    if match:
        number = match.group(1) or match.group(2)
        aliases.update({f"x{number}", f"{number}x"})
    return number, aliases


def _matches_generation_count(text, count):
    """Match both Flow count spellings (legacy ``1x`` and current ``x1``)."""
    if not count:
        return True
    clean = _normalize_fx_status_text(text)
    _, aliases = _generation_count_aliases(count)
    tokens = set(re.findall(r"(?<!\w)(?:x\d+|\d+x)(?!\w)", clean))
    return bool(tokens & aliases)

def _click_orientation_option(page, orientation, scope=None):
    root = scope or page
    tokens = _orientation_tokens(orientation)
    patterns = [orientation] + tokens

    # ── 优先级 0: aria-controls 尾值精确匹配（Radix UI 固定业务值，最稳定）──
    _aria_key = (orientation or "").lower().replace(" ", "_").replace(":", "")
    _aria_suffix = _ARIA_CONTROLS_RATIO_MAP.get(_aria_key)
    if not _aria_suffix:
        # 也尝试按比例文字 (e.g. "9:16")
        for _k, _v in _ARIA_CONTROLS_RATIO_MAP.items():
            if _k in _aria_key or _aria_key in _k:
                _aria_suffix = _v
                break
    if _aria_suffix:
        for _sel in [
            f"[aria-controls$='-{_aria_suffix}']",
            f"[aria-controls*='-{_aria_suffix}']",
        ]:
            try:
                _btn = root.locator(_sel).last
                if _btn.is_visible(timeout=2000):
                    _btn.click(force=True)
                    random_sleep(0.5, 1)
                    log(f"  ✅ 点击比例 tab (aria-controls$='-{_aria_suffix}')", "GoogleFX")
                    return _aria_suffix
            except Exception as _e:
                log(f"  ⚠️ _click_orientation_option 优先级0 sel={_sel!r}: {type(_e).__name__}", "GoogleFX")

    # ── 优先级 1: 精确匹配新版 Flow UI tab（aria-controls 含 PORTRAIT / LANDSCAPE）──
    for pattern in patterns:
        for sel in [
            f"[role='tab'][aria-controls*='{pattern}']",
            f"[role='tab'][id*='{pattern}']",
            f"[role='tab']:has-text('{pattern}')",
        ]:
            try:
                option = root.locator(sel).last
                if option.is_visible():
                    option.click(force=True)
                    random_sleep(0.5, 1)
                    log(f"  ✅ 点击比例 tab (sel='{sel}')", "GoogleFX")
                    return pattern
            except Exception as e:
                log(f"  ⚠️ _click_orientation_option 优先级1 sel={sel!r}: {type(e).__name__}", "GoogleFX")

    # ── 优先级 2: 通用按钮 / 选项文本匹配 ──
    for pattern in patterns:
        el, _ = _click_first_visible(root, [
            f"button:has-text('{pattern}')",
            f"[role='option']:has-text('{pattern}')",
            f"[role='menuitem']:has-text('{pattern}')",
            f"li:has-text('{pattern}')",
            f"div:has-text('{pattern}')",
        ], force=True, family="fx_orientation_option")
        if el:
            random_sleep(0.5, 1)
            return pattern

    # ── 优先级 3: JS 兜底 ──
    escaped = [token.replace("\\", "\\\\").replace("'", "\\'") for token in patterns]
    try:
        page.evaluate(
            """(patterns) => {
                const candidates = Array.from(document.querySelectorAll(
                    '[role="tab"], button, [role="option"], [role="menuitem"], li, div'
                ));
                const target = candidates.find((el) => {
                    const text = (el.innerText || '').trim();
                    const ac = el.getAttribute('aria-controls') || '';
                    const id = el.id || '';
                    return patterns.some((p) => text.includes(p) || ac.includes(p) || id.includes(p));
                });
                if (target) target.click();
            }""",
            escaped,
        )
        random_sleep(0.5, 1)
        return "/".join(patterns)
    except Exception as e:
        log(f"  ⚠️ _click_orientation_option JS兜底失败: {type(e).__name__}: {e}", "GoogleFX")
        return None

def check_fx_config(status_text, model="Nano Banana 2", orientation="Portrait", count="1x", duration=None, want_video=False, resolved_model_text=""):
    """
    从状态文字判断当前配置是否正确。
    先清除图标噪声文字（arrow_drop_down 等）再判断。

    Known models:
      Video: Veo 3.1 - Lite | Veo 3.1 - Fast | Veo 3.1 - Quality
           | Veo 3.1 - Lite [Lower Priority]
      Image: Nano Banana Pro | Nano Banana 2 | Nano Banana 2 Lite
    """
    # 清除 Google FX UI 图标语义噪声，避免对分支结果产生干扰
    clean = _normalize_fx_status_text(status_text)

    checks = {}
    # 视频模式下，状态栏常只显示 "Video"，不能再把它当成模型验证通过。
    model_source = resolved_model_text if (want_video and resolved_model_text) else status_text
    checks["model"] = _matches_model_status(model_source, model)
    checks["orientation"] = _matches_orientation_text(clean, orientation) if orientation else True
    checks["count"] = _matches_generation_count(clean, count)
    if want_video and duration:
        duration_label = _normalize_video_duration_label(duration)
        checks["duration"] = (duration_label in clean) if duration_label else True
    if want_video:
        checks["mode"] = ("video" in clean) or ("视频" in clean) or ("veo" in clean)
    else:
        checks["mode"] = ("video" not in clean) and ("视频" not in clean) and ("veo" not in clean)
    return checks

def fix_fx_config(page, cfg_btn, checks, model="Nano Banana 2", orientation="Portrait", count="1x", duration=None, want_video=False, mode_label="", video_submode=None):
    """打开配置面板并修正不正确的配置项。"""
    log("⚙️ 需要修改配置，打开面板...", "GoogleFX")
    cfg_btn.click()
    random_sleep(1.5, 2.5)
    fix_info = {
        "resolved_model_text": "",
        "duration_clicked": False,
        "video_submode_clicked": False,
        "clicked_keys": [],
        "resolved_keys": [],
    }
    panel_scope = _get_open_fx_config_panel(page, cfg_btn) or page

    if not checks.get("mode", True):
        target_mode_name = "Video" if want_video else "Image"
        target_mode_cn = "视频" if want_video else "图片"
        aria_mode_suffix = "VIDEO" if want_video else "IMAGE"
        desired_mode_labels = [label for label in [mode_label, target_mode_name, target_mode_cn] if label]
        log(f"  → 切换到 {target_mode_name} 模式 (aria-controls$='-{aria_mode_suffix}')", "GoogleFX")
        try:
            # 优先: aria-controls 尾值 (Radix UI 固定业务值，最稳定)
            _mode_btn = panel_scope.locator(f"[aria-controls$='-{aria_mode_suffix}']").first
            if _mode_btn.is_visible(timeout=2000):
                _mode_btn.click(force=True)
                random_sleep(0.5, 0.8)
                log(f"  ✅ {target_mode_name} 已点击 (aria-controls$='-{aria_mode_suffix}')", "GoogleFX")
                fix_info["clicked_keys"].append("mode")
            else:
                mode_clicked = False
                for label in desired_mode_labels:
                    if _click_fx_tab(page, label, scope=panel_scope):
                        log(f"  ✅ {label} 已点击 (tab fallback)", "GoogleFX")
                        mode_clicked = True
                        fix_info["clicked_keys"].append("mode")
                        break
                if not mode_clicked:
                    log(f"  ⚠️ 未找到 {target_mode_name} 模式 tab", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ {target_mode_name} 模式切换异常: {e}", "GoogleFX")

    if not checks.get("orientation", True):
        log(f"  → 切换到 {orientation}", "GoogleFX")
        try:
            matched = _click_orientation_option(page, orientation, scope=panel_scope)
            if matched:
                log(f"  ✅ {orientation} 已点击 ({matched})", "GoogleFX")
                fix_info["clicked_keys"].append("orientation")
            else:
                log(f"  ⚠️ 未找到 {orientation} 对应选项", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ {orientation} 切换异常: {e}", "GoogleFX")

    if not checks.get("count", True):
        log(f"  → 切换到 {count}", "GoogleFX")
        try:
            # Flow 当前 UI 把数量写作 x1/x2，旧 UI 写作 1x/2x；调用方仍使用
            # 兼容参数 1x。优先按 aria-controls 的稳定业务值定位，避免拿 "1x"
            # 去精确匹配当前的 "x1" 而永远找不到。
            _count_number, _count_aliases = _generation_count_aliases(count)
            _count_btn = None
            if _count_number:
                _by_control = panel_scope.locator(
                    f"button[role='tab'][aria-controls$='-content-{_count_number}']"
                ).first
                if _by_control.is_visible(timeout=2000):
                    _count_btn = _by_control
            if _count_btn is None:
                for _label in _count_aliases:
                    _candidate = panel_scope.locator("button[role='tab']").filter(
                        has_text=re.compile(f"^{re.escape(_label)}$", re.I)
                    ).first
                    if _candidate.is_visible(timeout=1000):
                        _count_btn = _candidate
                        break
            if _count_btn is not None:
                _count_btn.click(force=True)
                random_sleep(0.4, 0.8)
                log(f"  ✅ {count} 已点击 (数量 tab: x{_count_number or '?'})", "GoogleFX")
                fix_info["clicked_keys"].append("count")
            elif _click_fx_tab(page, count, scope=panel_scope):
                log(f"  ✅ {count} 已点击 (tab fallback)", "GoogleFX")
                fix_info["clicked_keys"].append("count")
            else:
                log(f"  ⚠️ 未找到 {count} 数量 tab", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ {count} 切换异常: {e}", "GoogleFX")

    # 2026-07-19 复盘：模型切换必须先于时长 tab 检测——4s/6s/8s/10s 这几个时长
    # tab 只有 Omni Flash 的视频面板才会渲染，Veo 系列面板压根没有这个控件（见本文件
    # 顶部 _VALID_VIDEO_DURATIONS 旁的说明）。之前的顺序是先找时长 tab、模型切换放最后，
    # 于是当面板当时还停留在上一次任务用过的 Veo/Nano Banana 配置上时，
    # _click_video_duration_tab 必然找不到任何时长 tab（面板此刻还没切到 Omni Flash），
    # 拿到的 duration 检测结果永远是"未确认"，_verify_and_fix_fx_config 随即把它计入
    # unconfirmed 并抛出"配置未完成，停止生成"，致命错误直接打断整个批次——实测复现
    # 于画布参考图刚上传完成、紧接着提交第一段视频任务时（server.log 19:43:49）。
    # 把模型切换挪到时长检测之前，让面板先落到目标模型（Omni Flash）上，时长 tab 才有
    # 机会真正出现在 DOM 里。
    if not checks.get("model", True):
        log(f"  → 切换到 {model}", "GoogleFX")
        try:
            model_dd = _find_fx_model_dropdown(page, scope=panel_scope, target_model=model)

            if model_dd and model_dd.is_visible():
                curr = _get_fx_model_dropdown_text(page, scope=panel_scope, target_model=model)
                log(f"  模型下拉文字: '{curr}'", "GoogleFX")
                # 如果当前模型名已包含目标模型，无需切换
                if _matches_model_status(curr, model):
                    log(f"  ✅ 模型已正确: {model}", "GoogleFX")
                    fix_info["resolved_model_text"] = curr
                    fix_info["resolved_keys"].append("model")
                else:
                    model_dd.click(force=True)
                    random_sleep(1, 2)
                    # 等待下拉选项出现
                    page.wait_for_timeout(800)
                    model_btn_id = ""
                    try:
                        model_btn_id = (model_dd.get_attribute("id") or "").strip()
                    except Exception:
                        pass
                    selected = False
                    if _click_fx_menu_item(page, model, button_id_hint=model_btn_id):
                        log(f"  ✅ {model} 已选择 (full match)", "GoogleFX")
                        selected = True
                        fix_info["clicked_keys"].append("model")
                    # 策略2: 关键词匹配 (如 'Banana Pro', 'Banana 2 Lite')
                    if not selected:
                        # 取模型名中最具区分性的部分
                        keywords = [model]  # 首先尝试全名
                        if " " in model:
                            parts = model.split()
                            # 去掉过短的单词，取后半部分组合
                            keywords += [" ".join(parts[-2:]), " ".join(parts[-3:]), parts[-1]]
                        for kw in keywords:
                            if len(kw) < 2: continue
                            if _click_fx_menu_item(page, kw, button_id_hint=model_btn_id):
                                log(f"  ✅ {model} 已选择 (keyword='{kw}')", "GoogleFX")
                                selected = True
                                fix_info["clicked_keys"].append("model")
                            if selected: break
                    if not selected:
                        log(f"  ❌ 模型 '{model}' 选项未找到", "GoogleFX")
                    current_after = _get_fx_model_dropdown_text(page, scope=panel_scope, target_model=model)
                    fix_info["resolved_model_text"] = current_after
                    log(f"  模型下拉复检: '{current_after or '<空>'}'", "GoogleFX")
            else:
                log(f"  ❌ 模型下拉按钮未找到", "GoogleFX")
        except Exception as e:
            log(f"  ❌ 模型异常: {e}", "GoogleFX")
    elif want_video:
        fix_info["resolved_model_text"] = _get_fx_model_dropdown_text(page, scope=panel_scope, target_model=model)

    duration_label = _normalize_video_duration_label(duration)
    if want_video and not checks.get("duration", True) and duration_label:
        log(f"  → 切换时长: 先点 4s，再点 {duration_label}", "GoogleFX")
        try:
            baseline_match = _click_video_duration_tab(page, panel_scope, "4s")
            if baseline_match:
                log(f"  ✅ 4s 已点击 ({baseline_match})", "GoogleFX")
            else:
                log("  ⚠️ 未找到 4s 时长 tab，继续尝试目标时长", "GoogleFX")

            if duration_label == "4s" and baseline_match:
                log("  ✅ 目标时长 4s 已通过基准点击确认", "GoogleFX")
                fix_info["duration_clicked"] = True
                fix_info["clicked_keys"].append("duration")
            else:
                target_match = _click_video_duration_tab(page, panel_scope, duration_label)
                if target_match:
                    log(f"  ✅ {duration_label} 已点击 ({target_match})", "GoogleFX")
                    fix_info["duration_clicked"] = True
                    fix_info["clicked_keys"].append("duration")
                else:
                    log(f"  ⚠️ 未找到 {duration_label} 时长 tab", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ {duration_label} 切换异常: {e}", "GoogleFX")

    # ── 视频子模式切换 (帧 VIDEO_FRAMES / 素材 VIDEO_REFERENCES) ──
    if want_video and not checks.get("video_submode", True) and video_submode:
        _target_suffix = video_submode  # e.g. 'VIDEO_FRAMES'
        _submode_label = '帧' if video_submode == 'VIDEO_FRAMES' else '素材'
        log(f"  → 切换视频子模式到 {_submode_label} ({_target_suffix})", "GoogleFX")
        if _switch_video_submode(page, _target_suffix, scope=panel_scope):
            log(f"  ✅ 视频子模式已切换: {_submode_label}", "GoogleFX")
            fix_info["video_submode_clicked"] = True
            fix_info["clicked_keys"].append("video_submode")
        else:
            log(f"  ⚠️ 视频子模式切换失败: {_submode_label}", "GoogleFX")

    # 新版内联设置面板需要显式保存；旧版 Radix 菜单没有保存按钮，才按 Escape。
    saved = False
    if fix_info["clicked_keys"]:
        for label in ("Save", "保存", "应用", "Simpan"):
            try:
                buttons = page.locator("button").filter(has_text=re.compile(
                    rf"^\s*{re.escape(label)}\s*$", re.I))
                for i in range(buttons.count()):
                    button = buttons.nth(i)
                    if button.is_visible(timeout=500) and button.is_enabled():
                        button.click(timeout=5000)
                        log(f"  ✅ 新版配置面板已保存 ({label})", "GoogleFX")
                        saved = True
                        break
                if saved:
                    break
            except Exception:
                continue
    if not saved:
        try:
            page.keyboard.press("Escape")
        except Exception as e:
            log(f"  ⚠️ fix_fx_config 关闭面板 Escape 失败: {type(e).__name__}", "GoogleFX")
    random_sleep(1.5, 2.5)  # 等待底部工具栏状态按钮恢复显示正确内容
    return fix_info


# ==============================================================================
# 🔧 FX 配置校验/修复的调用入口 + 模型名规范化 (原 services/google_fx.py，函数体逐字未改动)
# ==============================================================================

def _normalize_ratio_value(ratio):
    """将用户传入的 ratio 规范化：去空格 + 小写 (以便 RATIO_MAP 查表)。"""
    value = (ratio or "").strip()
    if not value:
        return None
    return value.lower()

_VALID_IMAGE_MODELS = list(GOOGLE_FX_IMAGE_MODELS)

_VALID_VIDEO_MODELS = [
    "Veo 3.1 - Lite",
    "Veo 3.1 - Fast",
    "Veo 3.1 - Quality",
    "Omni Flash",
    "Veo 3.1 - Lite [Lower Priority]",
]

_MODEL_ALIAS = {
    # 通用名 (兼容 N8N 历史写法)
    "google fx":      "Nano Banana 2",
    "google_fx":      "Nano Banana 2",
    "fx":             "Nano Banana 2",
    "flow":           "Nano Banana 2",
    # 图片模型简写
    "nano banana":    "Nano Banana 2",
    "nano":           "Nano Banana 2",
    "banana":         "Nano Banana 2",
    "imagen":         "Nano Banana 2",
    "imagen 3":       "Nano Banana 2",
    "imagen3":        "Nano Banana 2",
    "nano banana 2 lite": "Nano Banana 2 Lite",
    "banana 2 lite":      "Nano Banana 2 Lite",
    "imagen 4":           "Nano Banana 2 Lite",
    "imagen4":            "Nano Banana 2 Lite",
    "image 4":            "Nano Banana 2 Lite",
    "image4":             "Nano Banana 2 Lite",
    # 视频模型简写
    "veo":            "Veo 3.1 - Fast",
    "veo3":           "Veo 3.1 - Fast",
    "veo 3":          "Veo 3.1 - Fast",
    "veo 3.1":        "Veo 3.1 - Fast",
    "veo3.1":         "Veo 3.1 - Fast",
    "veo lite":       "Veo 3.1 - Lite",
    "veo fast":       "Veo 3.1 - Fast",
    "veo quality":    "Veo 3.1 - Quality",
    "veo lite lp":    "Veo 3.1 - Lite [Lower Priority]",
    "omni":           "Omni Flash",
    "omni flash":     "Omni Flash",
    "omniflash":      "Omni Flash",
}

def _normalize_model_name(model: str, is_video: bool = False) -> str:
    """
    将任意模型名规范化为已知有效值。
    - 已知有效名直接返回
    - 别名/旧名映射到真实名
    - 完全未知的名称使用对应模式的默认值
    """
    if not model:
        default = _VALID_VIDEO_MODELS[0] if is_video else DEFAULT_GOOGLE_FX_IMAGE_MODEL
        log(f"  ⚠️ model 为空，使用默认值 '{default}'", "GoogleFX")
        return default

    model = model.strip()
    valid_pool = _VALID_VIDEO_MODELS if is_video else _VALID_IMAGE_MODELS

    # 已是有效名
    if model in valid_pool:
        return model

    # 别名查表 (不区分大小写)
    lower = model.lower()
    if lower in _MODEL_ALIAS:
        mapped = _MODEL_ALIAS[lower]
        if mapped in valid_pool:
            log(f"  ⚠️ 模型名 '{model}' → 映射为 '{mapped}'", "GoogleFX")
            return mapped
        else:
            log(f"  ⚠️ 模型名 '{model}' 别名映射 '{mapped}' 不属于当前模式，忽略并继续匹配", "GoogleFX")

    # 部分包含匹配 (如 'Veo 3.1 - Lite [Lower Priority]' 被缩写)
    for valid in valid_pool:
        if lower in valid.lower() or valid.lower() in lower:
            log(f"  ⚠️ 模型名 '{model}' → 部分匹配为 '{valid}'", "GoogleFX")
            return valid

    # 完全未知 → 使用默认值
    default = valid_pool[0]
    log(f"  ⚠️ 未知模型名 '{model}'，使用默认值 '{default}'", "GoogleFX")
    return default

# ── 底部配置按钮找不到时的自愈恢复 (2026-07-25) ────────────────────────────────
# 症状：日志里「✅ 底部工具栏已加载」紧跟着「未找到底部配置按钮」，中间 10 秒轮询
# 全程无变化，整批重试 4 次每次一模一样——说明这不是"还没渲染完"，光等不会自愈。
# 「输入框在、配置按钮不在」实测只有三种成因：
#   1. 页面停在智能体 (Agent) 对话模式：底部标准工具栏被聊天框替换掉，而聊天框
#      同样是 contenteditable，_find_fx_prompt_input 会把它当成工具栏就绪 → 假阳性。
#      （视频链路 2026-07-23 已确诊过同一现象，见 google_fx_video._wait_toolbar_ready）
#   2. 有弹窗（产品公告 / 条款更新）盖在工具栏上：按钮在 DOM 里但 is_visible 为假。
#   3. 压根没进项目页：_find_fx_prompt_input 的选择器宽到 "textarea" /
#      "[contenteditable='true']"，首页上任意输入框都能让它返回"就绪"。
# 所以恢复动作按这三条依次做，全都不成再刷新一次页面；仍然失败才留档报错——
# 留档是关键，此前这条路径一次现场都没保存过，DOM 到底长什么样无从查起。

_FX_CONFIG_BTN_POLL_SECS = 10


def _dump_visible_button_texts(page, limit=40):
    """抓当前页面所有可见按钮的文本/aria-label（配置按钮找不到时留证据用）。"""
    try:
        texts = page.evaluate("""(limit) => {
            const out = [];
            const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const b of nodes) {
                if (b.offsetParent === null) continue;
                const raw = b.innerText || b.getAttribute('aria-label') || '';
                const t = raw.replace(/\\s+/g, ' ').trim();
                if (!t) continue;
                out.push(t.slice(0, 60));
                if (out.length >= limit) break;
            }
            return out;
        }""", limit)
        return texts or []
    except Exception as e:
        log(f"  ⚠️ 抓取可见按钮文本失败: {type(e).__name__}", "GoogleFX")
        return []


def _click_latest_flow_project(page):
    """点开历史项目列表里最新的一个项目，点到了返回 True。
    与 _prepare_fx_canvas 里的同名 JS 逻辑一致（都是找第一个 /project/ 链接）。"""
    try:
        return bool(page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const projectLink = links.find(a => {
                const href = a.getAttribute('href') || '';
                return href.includes('/project/') || href.includes('/tools/flow/project/');
            });
            if (projectLink && projectLink.offsetParent !== null) {
                projectLink.click();
                return true;
            }
            return false;
        }"""))
    except Exception as e:
        log(f"  ⚠️ 点击历史项目异常: {type(e).__name__}: {e}", "GoogleFX")
        return False


def _in_flow_project_page(page):
    """当前是否真的停在某个 Flow 项目页（而不是首页/登录页/空白页）。"""
    try:
        return "/project/" in (page.url or "")
    except Exception:
        return False


def _recover_missing_fx_config_button(page, context_label):
    """底部配置按钮第一次没找到时的自愈流程，返回 (btn, status_text)；救不回来返回 (None, "")。"""

    def _poll(seconds, tag):
        for i in range(seconds):
            _check_cancelled()
            time.sleep(1)
            btn, txt = find_fx_config_button(page)
            if btn:
                log(f"  ✅ {context_label}: {tag}第 {i + 1} 秒找到底部配置按钮", "GoogleFX")
                return btn, txt
        return None, ""

    # ① 边等渲染，边处理"等多久都不会好"的两种情况
    agent_tried = False
    overlay_tried = False
    for i in range(_FX_CONFIG_BTN_POLL_SECS):
        _check_cancelled()
        time.sleep(1)
        btn, txt = find_fx_config_button(page)
        if btn:
            log(f"  ✅ {context_label}: 第 {i + 1} 次重试后找到底部配置按钮", "GoogleFX")
            return btn, txt
        if not agent_tried and i >= 2:
            agent_tried = True
            if _dismiss_active_agent_mode(page, "GoogleFX"):
                log(f"  🤖 {context_label}: 已退出智能体模式，重新查找配置按钮...", "GoogleFX")
                continue
        if not overlay_tried and i >= 4:
            overlay_tried = True
            if _dismiss_unexpected_overlays(page, "GoogleFX"):
                log(f"  🧹 {context_label}: 已清掉挡住工具栏的弹窗，重新查找配置按钮...", "GoogleFX")
                continue

    # ② 检查是不是压根没进项目页（工具栏"就绪"是被宽选择器骗的）
    if not _in_flow_project_page(page):
        log(f"  ⚠️ {context_label}: 当前不在 Flow 项目页 (url={_safe_page_url(page)})，"
            f"底部工具栏判定为假阳性，重新进入项目...", "GoogleFX")
        if not _click_latest_flow_project(page):
            try:
                page.goto("https://labs.google/fx/tools/flow", timeout=60000, wait_until="domcontentloaded")
                random_sleep(2, 4)
            except Exception as e:
                log(f"  ⚠️ 导航到 Flow 首页失败: {type(e).__name__}", "GoogleFX")
            if not _click_latest_flow_project(page):
                _click_new_project_button(page)
        random_sleep(3, 5)
        btn, txt = _poll(15, "重进项目后 ")
        if btn:
            return btn, txt

    # ③ 最后一招：刷新页面（换 IP 重连后页面可能卡在半死不活的状态，只有刷新能救）
    log(f"  🔄 {context_label}: 仍未找到配置按钮，刷新页面后再试一次...", "GoogleFX")
    try:
        page.reload(timeout=60000)
        random_sleep(3, 5)
    except Exception as e:
        log(f"  ⚠️ 刷新页面失败: {type(e).__name__}: {e}", "GoogleFX")
    _dismiss_unexpected_overlays(page, "GoogleFX")
    _dismiss_active_agent_mode(page, "GoogleFX")
    btn, txt = _poll(20, "刷新后 ")
    if btn:
        return btn, txt

    # ④ 救不回来：留现场（截图 + HTML + 可见按钮清单），否则下次还是一样没法查
    log(f"  📋 {context_label}: 当前页面可见按钮: {_dump_visible_button_texts(page)}", "GoogleFX")
    try:
        from ..utils.ui_helpers import handle_element_not_found
        handle_element_not_found(page, f"{context_label}底部配置按钮")
    except Exception as e:
        log(f"  ⚠️ 保存案发现场失败: {type(e).__name__}: {e}", "GoogleFX")
    return None, ""


def _safe_page_url(page):
    try:
        return page.url or "<空>"
    except Exception:
        return "<不可读>"


# 2026-08-01 清理：这里原有 _raise_if_config_invalid()，全 repo 零调用者。名字看着像
# "配置校验的统一出口"，实际早被 _verify_and_fix_fx_config() 末尾那段 unconfirmed 判定
# 取代了（那里才是真正会 raise 的地方）。它顺带带走了一处 forensics.capture("config_invalid")
# ——那个 capture 标签因此从来没产生过现场文件，别再去 Errors 目录里找它。

def _verify_and_fix_fx_config(page, model, ratio, want_video, context_label, mode_label="", duration=None, video_submode=None, count="1x"):
    """统一的配置校验→面板修复确认流程 (三个生成函数共用)。
    video_submode: 'VIDEO_FRAMES' | 'VIDEO_REFERENCES' | None (仅 want_video 时生效)
    """
    selected_ratio = _normalize_ratio_value(ratio)
    vid_ratio = RATIO_MAP.get(selected_ratio, selected_ratio) if selected_ratio else None
    cfg_btn, status_text = find_fx_config_button(page)
    if not cfg_btn:
        # 🔁 底部配置按钮找不到，最常见的原因是页面刚导航/浏览器刚重连（典型场景：换 IP 后
        # 重新连接浏览器，紧接着就要切到 Image 模式上传图片），底部工具栏还没渲染完成——
        # 之前这里第一次没找到就直接 raise，是"换 IP 后紧接着的批次必挂"的确诊根因之一
        # （2026-07-01 server.log 实测复现：换 IP 重连浏览器 8 秒后即报此错，整个批次直接
        # 中止）。改为短暂轮询等待，而不是一次没找到就致命报错。
        # 2026-07-25：纯轮询救不了智能体模式/弹窗/没进项目页这三种"等不会好"的情况
        # （实测 4 次整批重试，每次都是等满 10 秒后原样报错），改走带自愈动作的恢复流程。
        cfg_btn, status_text = _recover_missing_fx_config_button(page, context_label)
    if not cfg_btn:
        # 同上：底部工具栏被积分弹窗整块盖住时，自愈流程（退智能体/清弹窗/重进
        # 项目/刷新）一样救不回来，报"找不到配置按钮"同样是在描述现象而非原因。
        credit_err = detect_page_credit_exhaustion(page, deep=True)
        if credit_err:
            raise RuntimeError(
                f"INSUFFICIENT_CREDITS: {credit_err}"
                f"（{context_label}底部配置按钮被积分提示挡住）"
            )
        raise RuntimeError(
            f"{context_label}未找到底部配置按钮，无法确认配置，已停止生成"
            f"（已尝试退出智能体模式 / 清弹窗 / 重进项目页 / 刷新页面均无效；"
            f"当前页面 {_safe_page_url(page)}，现场已留档到 Errors 目录）"
        )
    checks = check_fx_config(status_text, model=model, orientation=vid_ratio, count=count, duration=duration, want_video=want_video)
    # video_submode 不在底部摘要中显示，始终标记为需要修复
    if want_video and video_submode:
        checks["video_submode"] = False
    if all(checks.values()):
        log("✅ 所有配置正确", "GoogleFX")
        return selected_ratio
    fix_info = fix_fx_config(page, cfg_btn, checks, model=model,
                             orientation=vid_ratio, count=count, duration=duration, want_video=want_video,
                             mode_label=mode_label, video_submode=video_submode)
    initially_failed = [k for k, v in checks.items() if not v]
    fixed_keys = set((fix_info or {}).get("clicked_keys") or []) | set((fix_info or {}).get("resolved_keys") or [])
    # A click only proves that Playwright found and clicked a menu item; it does not prove
    # that Flow applied the model.  The menu can close without changing the selection while
    # the page is still rendering.  Never submit a generation request until the dropdown's
    # post-click text confirms the requested model.
    if "model" in initially_failed:
        resolved_model_text = (fix_info or {}).get("resolved_model_text") or ""
        if _matches_model_status(resolved_model_text, model):
            fixed_keys.add("model")
        else:
            fixed_keys.discard("model")
    unconfirmed = [key for key in initially_failed if key not in fixed_keys]
    if unconfirmed:
        # 🧊 报"配置没切成"之前先问一句积分。2026-08-24 实测复盘：账号积分耗尽时
        # Flow 会弹一个「积分已用完 / 刷新时间」对话框盖住配置面板，面板里的 tab
        # 点不到，这里就抛「面板未确认项: video_submode」——一条既不指向真因、
        # 又不含任何积分关键词的文案。上层 video_generator 靠
        # is_credit_exhausted_message() 认积分失败，认不出来就不会 mark_exhausted()，
        # 于是号池继续把这个空号派出去，每批都以同样的假错误挂掉。
        # 全 repo 的积分探测原本只挂在"提交之后"（Generate 无新 tile / 轮询卡片），
        # 配置阶段一处都没有——这里是补上的那一处。
        # 深探要点开顶栏头像菜单，而这会儿配置面板还开着（fix_fx_config 打开的），
        # 会挡住头像。先按 Escape 收掉——反正接下来无论如何都要抛异常了。
        try:
            page.keyboard.press("Escape")
            random_sleep(0.3, 0.6)
        except Exception:
            pass
        credit_err = detect_page_credit_exhaustion(page, deep=True)
        if credit_err:
            raise RuntimeError(
                f"INSUFFICIENT_CREDITS: {credit_err}"
                f"（{context_label}配置面板被积分提示挡住，未确认项: {', '.join(unconfirmed)}）"
            )
        raise RuntimeError(
            f"{context_label}配置未完成，停止生成。当前状态: {status_text or '<空>'}；"
            f"面板未确认项: {', '.join(unconfirmed)}"
        )
    log(f"  ✅ 配置项已通过 UI 点击/面板状态确认: {', '.join(initially_failed)}；已跳过底部摘要二次确认", "GoogleFX")
    return selected_ratio


# ==============================================================================
# 🔁 Loop-1 payload — 原分散在 google_fx.py，是 helpers.py 里 8 处函数体内
# lazy import 唯一需要回指的 7 个名字。搬到同一文件后那些 lazy import 全部改为
# 普通同文件调用 (函数体逐字未改动)。
# ==============================================================================

def _raise_if_manual_intervention_required(page, context_label="Google FX"):
    """Detect login, captcha, or security gates and stop for manual handling."""
    try:
        state = page.evaluate(r"""() => {
            const url = (window.location.href || '').toLowerCase();
            if (url.includes('accounts.google.com') || url.includes('signin/accountchooser') || url.includes('servicelogin')) {
                return {code: 'login_required', reason: 'accounts.google.com login page', sample: url};
            }
            const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
            const lower = text.toLowerCase();
            const hasRecaptchaFrame = Array.from(document.querySelectorAll('iframe[src]')).some((frame) => {
                const src = (frame.getAttribute('src') || '').toLowerCase();
                if (src.includes('size=invisible')) return false;
                return src.includes('recaptcha') || src.includes('captcha') || src.includes('/anchor');
            });
            const patterns = [
                ['onboarding_required', ['review our privacy notice', 'use and shape ai tools for creativity', 'tinjau kebijakan privasi kami', 'gunakan dan bentuk alat ai untuk kreativitas', '查看我们的隐私']],
                ['captcha_required', ['captcha', 'recaptcha', 'not a robot', '机器人', '人机验证']],
                ['login_required', ['sign in', 'log in', 'login', '登录', 'signin', 'choose an account', 'use another account', 'signed out', '选择账号', '使用其他账号']],
                ['security_check', ['unusual traffic', 'unusual activity', 'suspicious', '安全检查', '验证身份']],
                ['verification_required', ['verify it is you', 'verify your identity', 'verification', '二次验证', '真人']]
            ];
            if (hasRecaptchaFrame) return {code: 'captcha_required', reason: 'recaptcha iframe detected'};
            for (const [code, needles] of patterns) {
                for (const needle of needles) {
                    if (lower.includes(needle.toLowerCase())) {
                        return {code, reason: needle, sample: text.slice(0, 500)};
                    }
                }
            }
            return null;
        }""")
        if state:
            raise RuntimeError(
                f"MANUAL_REQUIRED:{state.get('code')}:"
                f"{context_label}需要人工处理 ({state.get('reason')})"
            )
    except RuntimeError:
        raise
    except Exception as e:
        log(f"⚠️ 人工接管状态检测失败: {type(e).__name__}", "GoogleFX")


class _ManualInterventionTimeoutError(RuntimeError):
    """登录失效/验证码/安全拦截，等待人工处理超时。

    单独一个类型是为了让调用方能把「等人工没等到」与普通生成失败区分开：
    批量视频驱动循环收到它时只判本批剩余任务失败并留下明确原因，不炸穿
    整个批次（见 google_fx_video.py 的 run()）。"""


# 等待人工处理的最长时间：超过就放弃本批。默认 20 分钟，够人打开 AdsPower
# 窗口把登录/验证码处理掉；SPARK 前端横幅默认文案也按 1200s 显示。
_MANUAL_INTERVENTION_DEFAULT_MAX_WAIT = 1200
_MANUAL_INTERVENTION_POLL_SECONDS = 5


def _manual_intervention_max_wait():
    """每次调用重读环境变量，与 config.runtime_env_or_default 一套路数：
    SPARK 侧是在同一进程里改 os.environ 后立刻发起 FX 调用的，
    模块级常量会把改动冻在 import 那一刻。"""
    try:
        return max(int(os.getenv("GOOGLE_FX_MANUAL_WAIT_SECONDS",
                                 str(_MANUAL_INTERVENTION_DEFAULT_MAX_WAIT))), 60)
    except (TypeError, ValueError):
        return _MANUAL_INTERVENTION_DEFAULT_MAX_WAIT


def _probe_manual_intervention(page, context_label="Google FX"):
    """返回 (code, reason)；页面正常时返回 None。

    复用 _raise_if_manual_intervention_required 的页面检测（不重复那段 JS），
    只是把它的 "MANUAL_REQUIRED:<code>:<msg>" 异常拆回结构化字段。"""
    try:
        _raise_if_manual_intervention_required(page, context_label=context_label)
        return None
    except RuntimeError as e:
        msg = str(e)
        if not msg.startswith("MANUAL_REQUIRED:"):
            raise
        parts = msg.split(":", 2)
        return parts[1], (parts[2] if len(parts) > 2 else msg)


def wait_out_manual_intervention(page, context_label="Google FX", cancel_check=None,
                                 on_event=None, max_wait_secs=None):
    """检测到登录失效/验证码/安全拦截时暂停轮询，等人工在 AdsPower 窗口里处理完。

    页面本来就正常 → 立刻返回 True（不产生任何事件）。
    检测到拦截 → 发 detected 事件后每 5s 复检一次：页面恢复正常返回 True 并发
    cleared 事件；等满 max_wait_secs 仍未恢复返回 False 并发 timeout 事件。
    调用方负责决定超时后是判失败还是继续（视频批量驱动循环抛
    _ManualInterventionTimeoutError）。

    on_event(phase, code, reason, max_wait_secs)：phase 取 detected/cleared/timeout，
    由调用方转成 SPARK 进度事件驱动前端常驻横幅——只写日志人工看不见，
    脚本会白等到超时。
    cancel_check()：返回 True 时抛 ConnectionError 中止（用户取消不该被这里
    的长等待挡住）。
    """
    max_wait_secs = max_wait_secs or _manual_intervention_max_wait()

    state = _probe_manual_intervention(page, context_label)
    if not state:
        return True

    code, reason = state

    def _emit(phase):
        if not on_event:
            return
        try:
            on_event(phase, code, reason, max_wait_secs)
        except ConnectionError:
            raise
        except Exception as e:
            log(f"⚠️ 人工接管事件回调失败: {type(e).__name__}: {e}", "GoogleFX")

    # ── 等人工之前：能自己登回去就别叫人（2026-07-30）──────────────
    # 只对 login_required 生效。验证码/安全检查/设备验证本来就自动化不了，在那些
    # 情况上调自动登录纯属浪费时间——auto_login 自己也会立刻放弃，但那要先付一次
    # 页面探测的开销，还会在日志里留一行没有意义的"自动登录未成功"。
    #
    # 成功时**不发 detected 事件**：前端横幅是给人看的"快来处理"信号，自动恢复了
    # 却弹一个又立刻撤掉的横幅，只会让用户以为出了事故。整段自愈只留日志。
    if code == "login_required":
        from ..utils.browser import attempt_auto_login
        if attempt_auto_login(page, context_label=context_label, cancel_check=cancel_check):
            recheck = _probe_manual_intervention(page, context_label)
            if not recheck:
                log(f"✅ {context_label}掉登录已自动恢复，无需人工介入", "GoogleFX")
                return True
            # 登录动作报成功但页面还是被拦（比如登完立刻撞上验证码）：
            # 按新的拦截原因继续走等人工，不要沿用旧的 login_required。
            code, reason = recheck
            log(f"⚠️ {context_label}自动登录后页面仍被拦截 ({code}: {reason})", "GoogleFX")

    log(f"🛑 {context_label}检测到需要人工处理 ({code}: {reason})，"
        f"暂停等待人工在 AdsPower 浏览器窗口处理，最长 {max_wait_secs}s", "GoogleFX")
    _emit("detected")

    deadline = time.time() + max_wait_secs
    while time.time() < deadline:
        if cancel_check and cancel_check():
            raise ConnectionError("用户已取消（等待人工处理期间）")
        # 没有 cancel_check 的调用方（图片批量链路就是）也必须能被取消：不然人工拦截
        # 一旦命中，取消要等满 max_wait_secs（默认 20 分钟）才生效。
        _check_cancelled()
        time.sleep(_MANUAL_INTERVENTION_POLL_SECONDS)
        # 页面/浏览器被关掉：人工已经不可能在这个 page 上把拦截处理好了，
        # 再等下去只是把整批任务多压 20 分钟，直接按超时处置。
        if _page_is_gone(page):
            log(f"⛔ {context_label}等待人工处理期间浏览器/标签页已关闭，停止等待", "Error")
            _emit("timeout")
            return False
        try:
            still_blocked = _probe_manual_intervention(page, context_label)
        except Exception as e:
            # 页面/浏览器在人工处理期间被关掉等：当作没恢复继续等，等满超时由
            # 调用方统一处置，避免在这里抛出一个更难解释的错误。
            log(f"⚠️ 人工接管状态复检失败: {type(e).__name__}: {e}", "GoogleFX")
            continue
        if not still_blocked:
            log(f"✅ {context_label}人工处理已完成，继续执行", "GoogleFX")
            _emit("cleared")
            return True
        code, reason = still_blocked

    log(f"⛔ {context_label}等待人工处理超时 ({max_wait_secs}s)，放弃", "Error")
    try:
        from ..utils.forensics import capture
        capture(page, "manual_intervention_timeout",
                f"{context_label} 等待人工处理超时（{code}: {reason}）",
                extra={"code": code, "max_wait_secs": max_wait_secs})
    except Exception:
        pass
    _emit("timeout")
    return False


# ── 页面卡在意外状态时的两个自愈动作 ──────────────────────────────────────
# 「工具栏等不到」的两个已知非故障原因：页面停在智能体对话模式（标准输入框被
# 换成聊天框），或者有个没见过的弹窗盖在上面。光等都不会自愈，必须主动点掉。

def _close_agent_settings_sidebar(page, log_tag="GoogleFX", _attempt=0):
    """关闭图二的“智能体设置 / Agent settings / Setelan agen”右侧栏。"""
    try:
        state = page.evaluate(r"""() => {
            const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const titles = ['agent settings', '智能体设置', '代理设置', 'setelan agen'];
            const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
            const heading = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span'))
              .find(el => visible(el) && titles.includes(norm(el.textContent)));
            if (!heading) return {found:false, closed:false};
            let panel = heading;
            while (panel.parentElement && panel.parentElement !== document.body) {
              const rect = panel.getBoundingClientRect();
              if (rect.width > 250 && rect.height > innerHeight * 0.55) break;
              panel = panel.parentElement;
            }
            const buttons = Array.from(panel.querySelectorAll('button')).filter(visible);
            const close = buttons.find(btn => {
              const text = norm(btn.innerText + ' ' + (btn.getAttribute('aria-label') || ''));
              const icons = norm(Array.from(btn.querySelectorAll('i')).map(i => i.textContent).join(' '));
              return icons === 'close' || ['close','关闭','tutup'].includes(text);
            });
            if (close) { close.click(); return {found:true, closed:true}; }
            return {found:true, closed:false};
        }""")
        if state and state.get("found"):
            if not state.get("closed") and _attempt < 2:
                page.keyboard.press("Escape")
                random_sleep(0.2, 0.4)
                return _close_agent_settings_sidebar(page, log_tag, _attempt + 1)
            log("🧹 已关闭误开的智能体设置侧边栏", log_tag)
            random_sleep(0.4, 0.7)
            return True
    except Exception as e:
        log(f"⚠️ 关闭智能体设置侧栏异常: {type(e).__name__}: {e}", log_tag)
    return False


def _dismiss_active_agent_mode(page, log_tag="GoogleFX"):
    """智能体模式处于激活态时点掉它。点掉了返回 True，本来就没激活返回 False。

    换 IP 重连后页面有时会停在智能体对话模式，底部标准输入框被替换成聊天框，
    等多久都等不到工具栏。
    """
    try:
        # 新版不再可靠提供 aria-pressed。article_spark（智能体指令）和 tune
        #（智能体设置）同时可见、而配置摘要不存在，才判定处于智能体模式。
        agent_controls = page.locator(
            "button:has(i.google-symbols:text-is('article_spark')), "
            "button:has(i.google-symbols:text-is('tune'))"
        )
        has_agent_controls = any(
            agent_controls.nth(i).is_visible() for i in range(agent_controls.count())
        )
        if not has_agent_controls:
            return False

        for label in ("Agent", "智能体", "Agen"):
            buttons = page.locator("button").filter(has_text=re.compile(
                rf"^\s*{re.escape(label)}\s*$", re.I))
            for i in range(buttons.count()):
                btn = buttons.nth(i)
                if btn.is_visible():
                    log(f"🤖 检测到智能体模式，点击 {label} 退出以恢复生成配置摘要", log_tag)
                    btn.click(force=True)
                    random_sleep(0.8, 1.2)
                    return True
    except Exception as e:
        log(f"⚠️ 取消智能体模式时异常: {type(e).__name__}: {e}", log_tag)
    return False


# 弹窗上「确认/关闭」类按钮的文案（小写匹配）。只认这些明确的关闭动作，
# 避免把 FX 自己的功能面板当成弹窗点掉。
_OVERLAY_DISMISS_TEXTS = (
    "got it", "dismiss", "no thanks", "not now", "maybe later", "close", "ok", "okay",
    "continue", "accept", "i agree", "知道了", "我知道了", "好的", "关闭", "确定",
    "以后再说", "暂不", "同意", "继续",
    # 2026-08-24 补：积分耗尽/刷新提示弹窗用的是这几种收尾按钮，原清单一个都不认，
    # 于是弹窗一直盖在配置面板上，下游报成"面板未确认项: video_submode"。
    "back", "return", "cancel", "done", "finish", "later", "skip", "no, thanks",
    "返回", "取消", "完成", "跳过", "稍后", "下次再说",
)
# 出现这些内容的对话框是 FX 自己的配置/功能面板，不是意外弹窗，绝不能点掉
_OVERLAY_KEEP_TOKENS = ("16:9", "9:16", "veo", "nano banana", "imagen", "outputs per prompt")


def _dismiss_unexpected_overlays(page, log_tag="GoogleFX"):
    """清理挡在页面上的未知弹窗（产品公告 / 条款更新等）。点掉了返回 True。

    只处理 role=dialog/alertdialog 且带明确关闭按钮的弹层；识别不出关闭动作
    的一律不动——宁可让下游报"点不到元素"，也不要瞎点把 FX 自己的面板关掉。
    """
    dismissed = False
    try:
        dialogs = page.locator("[role='dialog'], [role='alertdialog']")
        for i in range(min(dialogs.count(), 5)):
            dialog = dialogs.nth(i)
            try:
                if not dialog.is_visible():
                    continue
                body = (dialog.inner_text() or "").strip()
            except Exception:
                continue
            lower_body = body.lower()
            if any(tok in lower_body for tok in _OVERLAY_KEEP_TOKENS):
                continue  # FX 自己的配置面板

            clicked = False
            buttons = dialog.locator("button")
            for j in range(min(buttons.count(), 12)):
                btn = buttons.nth(j)
                try:
                    if not btn.is_visible():
                        continue
                    btn_text = (btn.inner_text() or "").strip().lower()
                    aria = (btn.get_attribute("aria-label") or "").strip().lower()
                except Exception:
                    continue
                label = btn_text or aria
                if not label:
                    continue
                if any(kw in label for kw in _OVERLAY_DISMISS_TEXTS):
                    log(f"🧹 清理未知弹窗: 点击 '{label}' (弹窗首行: {body.splitlines()[0][:60] if body else ''})", log_tag)
                    btn.click(force=True)
                    random_sleep(0.5, 1.0)
                    clicked = True
                    break
            if clicked:
                dismissed = True
                continue

            # 认不出关闭按钮时的兜底：先按 Escape，再看弹窗是不是真的没了。
            # 原来这里只打一条日志就放着不管，弹窗继续盖住底部工具栏/配置面板，
            # 下游只能报"点不到元素"——2026-08-24 那次积分耗尽就是这么被伪装成
            # "面板未确认项: video_submode" 的。
            # Escape 对 Radix/cdk 这类 modal 是标准关闭手势，且上面的
            # _OVERLAY_KEEP_TOKENS 已经把 FX 自己的配置面板挡在外面了，安全。
            escaped = False
            try:
                page.keyboard.press("Escape")
                random_sleep(0.4, 0.7)
                escaped = not dialog.is_visible()
            except Exception:
                escaped = False

            if escaped:
                dismissed = True
                log(f"🧹 清理未知弹窗: Escape 生效 (弹窗首行: {body.splitlines()[0][:60] if body else ''})", log_tag)
            elif body:
                first_line = body.splitlines()[0][:80]
                log(f"⚠️ 检测到未知弹窗但找不到明确的关闭按钮，未处理: {first_line}", log_tag)
                # 关不掉的弹窗，至少把全文留档：积分类提示的关键信息常常不在首行
                # （典型是首行只有一个刷新时间），只打首行等于把线索丢了。
                try:
                    from .google_fx_credit import is_credit_hint_message
                    if is_credit_exhausted_message(body) or is_credit_hint_message(body):
                        log(f"  💡 该弹窗疑似与积分有关，全文: {body[:400]!r}", log_tag)
                except Exception:
                    pass
    except Exception as e:
        log(f"⚠️ 清理未知弹窗时异常: {type(e).__name__}: {e}", log_tag)
    return dismissed


def _get_panel_uuid_order(page):
    """同 _get_panel_uuids，但按 DOM 文档顺序返回**列表**（去重，保留首次出现的位置）。

    上传归属判定需要回答"这一批新冒出来的卡片里，哪一张是刚刚传上去的那一张"。
    集合的迭代顺序是哈希序，跟新旧毫无关系——用 next(iter(set)) 去挑就是在掷骰子，
    掷错的后果是这张本地图被安上别人的 UUID，随后整条 path→uuid 映射错位，
    生成出一批挂着错误首尾帧的视频。所以凡是要判新旧/判唯一性的地方都必须用它。
    """
    ordered = []
    seen = set()
    try:
        srcs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img[src*="getMediaUrlRedirect"]'))
                .map(img => img.getAttribute('src') || '');
        }""")
    except Exception as e:
        log(f"  ⚠️ _get_panel_uuid_order 失败: {e}", "GoogleFX")
        return ordered
    for src in srcs or []:
        m = re.search(r'name=([0-9a-f\-]{30,})', src)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ordered.append(m.group(1))
    return ordered


def _get_panel_uuids(page):
    """纯 DOM 扫描页面中现有图片缓存，不主动打开/关闭 add_2 面板。"""
    return set(_get_panel_uuid_order(page))

def _find_fx_prompt_input(page, announce=False):
    """定位 Google FX 底部输入框，优先 Slate.js 编辑器。"""
    selectors = [_SLATE_EDITOR_SELECTOR] + UI_SELECTORS["google_fx"].get("prompt_input", [])
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                if announce and sel == _SLATE_EDITOR_SELECTOR:
                    log("📝 检测到 Slate.js 编辑器", "GoogleFX")
                return el
        except Exception:
            pass
    return None

def _wait_for_flow_reference_ready(page, timeout_seconds=30, settle_range=None):
    """
    等待图生图参考图在 Flow 输入区真正挂载完成。
    返回 (ready: bool, matched_selector: str)。
    检测到就绪信号后仅做一次短暂随机稳定等待，避免每轮都硬等 10s。
    ✅ 2026-04-05 更新: 增加基于实测 DOM 的稳定选择器
    """
    ready_selectors = [
        # ✅ 实测最稳定: 底部输入框内出现图片缩略图
        "div[contenteditable='true'] img",
        "div[data-slate-editor='true'] img",
        # ✅ 实测: 相同机位第二张片内图片写入框
        "div[data-slate-editor] img",
        # ✅ 2026-04-11 实测: 视频模式 Add to Prompt 会把图片挂到输入框左侧素材槽
        "button[data-card-open] img[alt*='present in your collection']",
        "button[data-card-open] i:text-is('cancel')",
        # 备用: Remove 按钮 / ingredient 提示
        "[aria-label*='Remove']:visible",
        "span:has-text('This is your ingredient')",
        # 备用: 集合图片
        "img[alt*='present in your collection']",
    ]

    deadline = time.time() + max(timeout_seconds, 1)
    while time.time() < deadline:
        try:
            # 尝试传统 Selector
            for sel in ready_selectors:
                if page.locator(sel).first.is_visible(timeout=100):
                    if settle_range:
                        lo, hi = settle_range
                        random_sleep(lo, hi)
                    return True, sel

            # 增加大范围兜底 JS 探测：视频模式下，底部输入区左侧素材槽也算成功挂载
            js_found = page.evaluate("""() => {
                const editor = document.querySelector("div[role='textbox'][contenteditable='true'], textarea");
                if (!editor) return false;
                let container = editor;
                for (let i = 0; i < 6; i++) {
                    if (container.parentElement) container = container.parentElement;
                }
                const promptImgs = Array.from(container.querySelectorAll("img")).filter((img) => {
                    const rect = img.getBoundingClientRect();
                    return rect.top > window.innerHeight - 360 && ((img.offsetHeight || 0) > 20 || (img.offsetWidth || 0) > 20);
                });
                const hasPromptMedia = promptImgs.some((img) => {
                    const alt = (img.getAttribute("alt") || "").toLowerCase();
                    return alt.includes("present in your collection") || alt.includes("generated image");
                });
                const hasCancelChip = Array.from(container.querySelectorAll("button i")).some((icon) => {
                    const rect = icon.getBoundingClientRect();
                    const text = (icon.textContent || "").trim().toLowerCase();
                    return rect.top > window.innerHeight - 360 && text === "cancel";
                });
                return hasPromptMedia || hasCancelChip;
            }""")

            if js_found:
                if settle_range:
                    lo, hi = settle_range
                    random_sleep(lo, hi)
                return True, "js_dom_sniffer"

        except Exception:
            pass

        time.sleep(1)
    return False, ""

def read_prompt_reference_state(page, limit=4):
    """读取提示词输入区里参考图的视觉顺序，并说明这次读数**可不可信**。

    返回 {"uuids": [...], "scope": "bar"|"document"|"", "ok": bool}。
    ``ok=False`` 只表示"没读到"，不表示"没有参考图"——两者必须分开，见下。

    提示词很长时输入条会向上扩展，参考图的 ``rect.top`` 可能落到视口底部
    420px 之外。旧实现把这种正常布局误判成「参考图已脱落」，视频链随后放弃
    提交；下一槽位开始时又会清理输入条，于是用户看到的就是「提示词被清空但
    没有提交」。这里和清理逻辑一样，按 Slate 编辑器 + ``arrow_forward`` 的
    DOM 结构锁定输入条，不再用窗口坐标猜测。

    2026-08-02：结构定位本身也会落空——写完两千字提示词后 Flow 会把编辑器再包一层
    滚动容器，``arrow_forward`` 于是被顶出 8 层祖先之外；页面上残留另一个更靠前的
    Slate 编辑器时，走的更是从头就错的那条链。旧实现此时 ``return []``，把「我没找到
    输入条」说成了「输入条里一张参考图都没有」，调用方（尤其是提交前的锚点复核）
    只能当成首尾帧掉了，于是整场 24 个片段全部拒绝提交、反复重试反复失败。
    ``_PROMPT_BAR_JS`` 早就是 ``scope = bar || document`` 的写法，这里对齐它：
    定位不到输入条就退回全文档扫 chip 签名（``button[data-card-open]`` 里的缩略图，
    画布卡片不带这个属性），并把 scope 如实报给调用方。
    """
    try:
        state = page.evaluate("""() => {
            const uuidRegex = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
            const editor = document.querySelector("[data-slate-editor='true']");
            let bar = null;
            if (editor) {
                let candidate = editor.parentElement;
                for (let depth = 0; candidate && depth < 8; depth++) {
                    const icons = Array.from(candidate.querySelectorAll('i'))
                        .map((i) => (i.textContent || '').trim());
                    if (icons.includes('arrow_forward')) { bar = candidate; break; }
                    candidate = candidate.parentElement;
                }
            }
            // 定位不到输入条时退回全文档，但只认 chip 签名：画布卡片不是
            // button[data-card-open]，不会混进来冒充参考图。
            const scope = bar || document;
            const selector = bar
                ? "button[data-card-open] img, [data-slate-editor='true'] img"
                : "button[data-card-open] img";

            const imgs = Array.from(scope.querySelectorAll(selector));
            const seen = new Set();
            const rows = [];
            for (const img of imgs) {
                if (!img || img.offsetParent === null) continue;
                const rect = img.getBoundingClientRect();
                if ((rect.width || 0) < 20 || (rect.height || 0) < 20) continue;
                const src = img.currentSrc || img.src || '';
                const match = src.match(uuidRegex);
                if (!match) continue;
                const uuid = match[1];
                if (seen.has(uuid)) continue;
                seen.add(uuid);
                rows.push({ uuid, top: rect.top || 0, left: rect.left || 0 });
            }
            rows.sort((a, b) => {
                if (a.top !== b.top) return a.top - b.top;
                return a.left - b.left;
            });
            return {uuids: rows.map((row) => row.uuid), scope: bar ? 'bar' : 'document'};
        }""")
    except Exception as e:
        log(f"⚠️ 读取 Prompt 参考图顺序失败: {type(e).__name__}", "GoogleFX")
        return {"uuids": [], "scope": "", "ok": False}
    state = state or {}
    uuids = list(state.get("uuids") or [])
    return {
        "uuids": uuids[:max(limit, 1)],
        "scope": state.get("scope") or "",
        "ok": bool(state.get("scope")),
    }


def _get_prompt_reference_uuids(page, limit=4):
    """提示词输入区里参考图的视觉顺序（UUID 列表）。

    读数是否可信要用 read_prompt_reference_state()：本函数把「读不到」压成了空列表，
    只适合那些「拿到几张算几张」的调用点。凡是要据此判定失败的地方都不能用它。
    """
    return read_prompt_reference_state(page, limit=limit)["uuids"]

def click_fx_send_button(page, input_el=None):
    """点击发送按钮 (新版 UI: arrow icon / aria-label / Create / Enter)

    FX_DRY_RUN=1 时不真的提交（见 services/google_fx_diagnostics 的 Dry-run 说明）：
    这是全部链路唯一的提交动作出口，所以拦在这里就能"走完全部 DOM 定位与配置校验，
    但一张图都不生成"——没有账号额度时验证 UI 兼容性，或让 agent 离线复现选择器
    问题，都只能靠这个开关。
    """
    try:
        from .google_fx_diagnostics import dry_run_enabled
        if dry_run_enabled():
            log("🧪 FX_DRY_RUN=1：跳过真实提交（定位与配置校验已全部走完）", "GoogleFX")
            return True
    except Exception:
        pass

    sent = False
    # 方法0: Generate 按钮（新版 Flow UI 首选，aria-controls 稳定 Radix UI）
    # ✅ Patch: scroll_into_view + hover + 随机停顿，避免直接 click 触发反自动化检测
    try:
        gen_btn = page.locator("button").filter(
            has_text=re.compile(r"^Generate$", re.I)
        ).last
        if gen_btn.is_visible(timeout=1500):
            gen_btn.scroll_into_view_if_needed()
            gen_btn.hover()
            random_sleep(0.3, 0.7)   # 鼠标停在按钮上的自然停顿
            gen_btn.click()
            sent = True
            log("✅ 已点击 Generate 按钮", "GoogleFX")
    except Exception as e:
        log(f"  ⚠️ click_fx_send Generate: {type(e).__name__}", "GoogleFX")
    # 方法1: 找包含 arrow icon 的按钮
    if not sent:
        try:
            all_btns = page.locator("button:visible")
            cnt = all_btns.count()
            for bi in range(cnt - 1, max(cnt - 10, -1), -1):
                try:
                    b = all_btns.nth(bi)
                    t = b.inner_text().strip()
                    if 'arrow_forward' in t or 'send' in t or 'arrow_upward' in t:
                        b.click()
                        sent = True
                        log("✅ 已点击发送 (arrow icon)", "GoogleFX")
                        break
                except Exception as e:
                    log(f"  ⚠️ click_fx_send 方法1 内层: {type(e).__name__}", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ click_fx_send 方法1 外层: {type(e).__name__}: {e}", "GoogleFX")
    # 方法2: aria-label
    if not sent:
        for label in ["Send", "send", "Submit", "submit", "Create"]:
            try:
                sb = page.locator(f"button[aria-label*='{label}']").last
                if sb.is_visible():
                    sb.click()
                    sent = True
                    log(f"✅ 已点击发送 (aria-label: {label})", "GoogleFX")
                    break
            except Exception as e:
                log(f"  ⚠️ click_fx_send 方法2 label={label!r}: {type(e).__name__}", "GoogleFX")
    # 方法3: Create / Generate 按钮（文字包含匹配兜底）
    if not sent:
        for _btn_text in ["Generate", "Create"]:
            try:
                fallback_btn = page.locator("button").filter(has_text=_btn_text).last
                if fallback_btn.is_visible():
                    fallback_btn.click()
                    sent = True
                    log(f"✅ 已点击 {_btn_text} 按钮", "GoogleFX")
                    break
            except Exception as e:
                log(f"  ⚠️ click_fx_send 方法3 {_btn_text}: {type(e).__name__}: {e}", "GoogleFX")
    # 方法4: Enter
    if not sent and input_el:
        input_el.press("Enter")
        log("⚠️ 按 Enter 提交", "GoogleFX")
        sent = True

    if sent:
        try:
            from ..utils.proxy_rotator import ProxyRotator
            ProxyRotator().increment_request_counter(1)
        except Exception as e:
            log(f"⚠️ 递增代理请求计数器异常: {e}", "GoogleFX")

    return sent

def _add_flow_image_to_prompt(page, image_ref: str, tile_id: str = "") -> bool:
    """
    在 Flow 画布中找到指定图片，hover → [role='toolbar'] → more_vert → Add to Prompt。
    实测 DOM (2026-04-07):
      - hover tile 后出现 [role='toolbar']（含 favorite / redo / more_vert 三个按钮）
      - more_vert 按钮: button[aria-haspopup='menu'] 在 toolbar 内
      - 点击后弹出 [role='menu'][data-state='open']
      - Add to Prompt: button[role='menuitem']:has-text('Add to Prompt')
    image_ref: UUID | 完整 getMediaUrlRedirect URL | 含 UUID 的本地路径
    返回 True 表示成功，False 表示失败（不中断生成流程）。
    """
    # 提取 UUID（支持: 纯UUID / getMediaUrlRedirect URL / 含UUID的本地路径）
    uuid = _extract_flow_image_uuid(image_ref)
    if not uuid and not tile_id:
        log(f"⚠️ 无法解析参考图定位信息: '{str(image_ref)[:60]}'", "GoogleFX")
        return False

    ref_label = (uuid or tile_id or "unknown")[:16]
    log(f"🖼️ 图生图: hover tile → more_vert → Add to Prompt ({ref_label}...)", "GoogleFX")

    try:
        max_mount_attempts = 2
        for mount_attempt in range(1, max_mount_attempts + 1):
            # 挂参考图这一段全是 hover/点菜单/等 chip 的重试，一轮好几秒，
            # 取消得能在轮之间生效（上层不再靠关浏览器硬停）。
            _check_cancelled()
            _safe_press_escape(page, f"_add_flow_image_to_prompt 起始清理 attempt={mount_attempt}")
            if mount_attempt == 1:
                random_sleep(0.3, 0.5)
            else:
                log(f"  ↺ 挂载重试第 {mount_attempt} 次，放慢点击节奏", "GoogleFX")
                random_sleep(0.9, 1.4)

            # 1. 确认画布上有此图，获取位置信息并锁定 tile
            canvas_img = _resolve_flow_tile_info(page, uuid=uuid or "", tile_id=tile_id or "")

            if not canvas_img:
                log(f"  ⚠️ 画布上未找到目标卡片: {ref_label}...", "GoogleFX")
                return False

            tile_id = (canvas_img.get("tileId") or "").strip()
            log(
                f"  🎯 找到画布图片 {canvas_img['w']:.0f}×{canvas_img['h']:.0f}px"
                + (f" | tile={tile_id[:12]}..." if tile_id else "")
                + (f" | mount_attempt={mount_attempt}" if max_mount_attempts > 1 else ""),
                "GoogleFX",
            )

            # 2. 进入 hover 态并尝试直接点击 toolbar 中的 "Add to prompt" 按钮
            before_refs = _get_prompt_reference_uuids(page, limit=6)
            _hover_flow_tile_for_toolbar(page, uuid=uuid or "", tile_id=tile_id)

            clicked_direct = False
            if tile_id:
                try:
                    tile_scope = page.locator(f"[data-tile-id='{tile_id}']").first
                    toolbar = tile_scope.locator("[role='toolbar']").first
                    if toolbar.is_visible(timeout=1000):
                        # 查找精准候选按钮（只匹配明确的 Add to prompt / 添加到提示词，防止误触页面其他 Add 按钮）
                        for direct_sel in [
                            "button[aria-label='Add to prompt' i]",
                            "button[title='Add to prompt' i]",
                            "button[aria-label='Add to Prompt']",
                            "button[title='Add to Prompt']",
                            "button[aria-label='添加到提示词']",
                            "button[title='添加到提示词']",
                            "button:has-text('Add to prompt')",
                            "button:has-text('Add to Prompt')",
                            "button:has-text('添加到提示词')",
                        ]:
                            btn = toolbar.locator(direct_sel).first
                            if btn.is_visible(timeout=500):
                                btn.click(force=False)
                                log(f"  ✅ [Direct Click] 在 toolbar 中直接点击了 {direct_sel!r}", "GoogleFX")
                                clicked_direct = True
                                break
                except Exception as _direct_err:
                    log(f"  ⚠️ 尝试直接点击 toolbar 按钮失败: {_direct_err}", "GoogleFX")

            menu_id_hint = ""
            if not clicked_direct:
                # 3. 备选: 点击 more_vert 弹出菜单再选择
                menu_id_hint = _click_flow_more_menu(page, uuid=uuid or "", tile_id=tile_id)
                menu_open = False
                for menu_sel in [
                    "[role='menu'][data-state='open']",
                    "[data-radix-menu-content][data-state='open']",
                    "[role='menu']",
                ]:
                    try:
                        if page.locator(menu_sel).last.is_visible(timeout=500 + (mount_attempt - 1) * 300):
                            menu_open = True
                            break
                    except Exception:
                        continue
                if not menu_id_hint and not menu_open:
                    log(
                        f"  ❌ Add to Prompt 失败 | stage=menu_not_open | tile_id={tile_id or '<none>'} | "
                        f"menu_id=<none> | refs_before={before_refs} | mount_attempt={mount_attempt}",
                        "GoogleFX",
                    )
                    if mount_attempt < max_mount_attempts:
                        continue
                    return False

                if mount_attempt == 1:
                    random_sleep(0.4, 0.7)
                else:
                    random_sleep(0.8, 1.2)

                # 4. 点击 Add to Prompt 菜单项
                if not _click_flow_add_to_prompt(page, menu_id_hint=menu_id_hint, tile_id_hint=tile_id):
                    if mount_attempt < max_mount_attempts:
                        continue
                    return False

            ready, ready_sel = _wait_for_flow_reference_ready(
                page,
                timeout_seconds=10 + (mount_attempt - 1) * 4,
                settle_range=(0.3, 0.6) if mount_attempt == 1 else (0.8, 1.2),
            )
            changed, after_refs = _wait_for_prompt_reference_change(
                page,
                previous_refs=before_refs,
                expected_uuid=uuid or "",
                timeout_seconds=8 + (mount_attempt - 1) * 4,
            )
            if ready or changed:
                log(
                    f"✅ 参考图 {uuid[:16]}... 已加入 Prompt | ready={ready} | "
                    f"selector={ready_sel or '<none>'} | refs_after={after_refs} | mount_attempt={mount_attempt}",
                    "GoogleFX",
                )
                return True

            after_refs_norm = [item.lower() for item in after_refs] if after_refs else []
            if uuid and uuid.lower() in after_refs_norm:
                log(
                    f"✅ 参考图 {uuid[:16]}... 已在 references 列表中命中（兜底）| selector={ready_sel or '<none>'} | "
                    f"refs_after={after_refs} | mount_attempt={mount_attempt}",
                    "GoogleFX",
                )
                return True

            log(
                f"  ❌ Add to Prompt 失败 | stage=prompt_ref_not_attached | tile_id={tile_id or '<none>'} | "
                f"menu_id={menu_id_hint or '<none>'} | refs_before={before_refs} | "
                f"refs_after={after_refs} | mount_attempt={mount_attempt}",
                "GoogleFX",
            )
            _safe_press_escape(page, f"_add_flow_image_to_prompt 挂图失败收尾 attempt={mount_attempt}")

        return False

    except Exception as e:
        log(f"⚠️ _add_flow_image_to_prompt 失败: {e}", "GoogleFX")
        _safe_press_escape(page, "_add_flow_image_to_prompt 异常收尾")
        return False


# ==============================================================================
# 🌐 页面连接 + 画布/卡片维护 (原 services/google_fx.py，函数体逐字未改动)
# ==============================================================================

def _connect_over_cdp_with_retry(playwright_ctx, ws_url, max_attempts=10, delay_secs=2.0):
    """CDP 连接包装器：如果遇到 Web Socket 连接被拒绝 (ECONNREFUSED) 则重试，最多重试 10 次。"""
    last_err = None
    time.sleep(1.0)  # 首次连接前先等待 1 秒，给 Chromium 端口绑定留出缓冲时间
    for attempt in range(1, max_attempts + 1):
        try:
            return playwright_ctx.chromium.connect_over_cdp(ws_url, timeout=30000)
        except Exception as e:
            last_err = e
            log(f"  ⚠️ connect_over_cdp 失败 (尝试 {attempt}/{max_attempts}): {type(e).__name__}: {e}", "GoogleFX")
            if attempt < max_attempts:
                time.sleep(delay_secs)
    raise last_err

def _connect_fx_page(playwright_ctx, cancel_check=None, on_event=None,
                     allow_account_switch=True):
    """连接 Adspower 浏览器并导航到 Google FX 页面。返回 (browser, page)。

    增强:
    1. 如果连接 CDP 失败 (比如 profile 卡死导致调试端口未开启)，重启当前 profile
       后重试，最多重试 3 次；本地连接故障不触发账号切换。
    2. 检测到 Google 安全拦截 (unusual activity / security check) 时，
       自动关闭浏览器 → 换号 → 重启浏览器 → 重试导航，最多重试 1 次。
    3. 检测到登录失效 / 验证码 / 二次验证（换号/换 IP 都解决不了、只能人工在 AdsPower
       窗口里处理的那几类）时，若调用方给了 on_event 能把状态透出给人看，
       就暂停等人工处理完再继续；等不到则抛 _ManualInterventionTimeoutError。
       没有 on_event 的调用方（图片批量 / 单条视频，没有进度事件通道）维持
       原样直接抛错——宁可让上层报一个明确的错，也不要静默干等 20 分钟。
    """
    max_conn_attempts = 3
    browser = None
    tried_accounts = set()   # Google 安全拦截后换号时，避免换回已失败的账号

    for attempt in range(1, max_conn_attempts + 1):
        # 连接阶段（含 profile 卡死 → stop → 重开浏览器）一轮就要几十秒，
        # 中途点的取消原来要等这三轮全跑完才可能生效。
        _check_cancelled()
        try:
            ws_url = get_ads_ws_url()
            browser = _connect_over_cdp_with_retry(playwright_ctx, ws_url)
            break
        except Exception as e:
            if cancel_flag.is_cancelled:
                log("🛑 任务已取消，放弃连接重试", "GoogleFX")
                raise RuntimeError("任务已取消") from e
            log(f"⚠️ 连接 AdsPower/CDP 失败 (尝试 {attempt}/{max_conn_attempts}): {type(e).__name__}: {e}", "GoogleFX")
            if attempt >= max_conn_attempts:
                log("❌ 已达到最大连接重试次数，放弃连接", "GoogleFX")
                raise e

            # 关闭可能处于残留状态的浏览器
            try:
                from ..config import get_runtime_default_user_id, get_runtime_default_port, DEFAULT_USER_ID, DEFAULT_PORT
                from ..utils import account_binding
                user_id = account_binding.resolve_account(
                    fallback=get_runtime_default_user_id() or DEFAULT_USER_ID,
                )
                port = get_runtime_default_port() or DEFAULT_PORT
                url = f"http://127.0.0.1:{port}/api/v1/browser/stop?user_id={user_id}"
                requests.get(url, timeout=10)
            except Exception:
                pass

            # AdsPower/CDP failure only describes the local browser/profile state.
            # It is not evidence of a Google generation/card/account failure.  The
            # profile was stopped above, so get_ads_ws_url() will restart the same
            # profile on the next attempt.  Do not force an account rotation here.
            log("🔄 AdsPower/CDP 连接失败，正在重启当前浏览器配置后重试（不换号）...", "GoogleFX")

            time.sleep(2)

    context = browser.contexts[0]
    page = find_or_create_page(
        context, "labs.google", cancel_check=cancel_check,
        context_label="Google FX 页面初始化")

    # 快速探活：find_or_create_page 在 Frame detached 后尽力恢复了页面，
    # 但恢复后的 page 仍可能不可用（比如 CDP 连接已断、浏览器已被关闭）。
    # 此处做一次 2s 探活，不可用时立即新建页面并导航，避免后续操作静默挂死。
    if not _page_is_alive(page):
        log("⚠️ find_or_create_page 返回的页面不可用，新建页面并导航", "GoogleFX")
        try:
            page = context.new_page()
            page.goto("https://labs.google/fx/tools/flow", timeout=60000, wait_until="domcontentloaded")
            random_sleep(1, 2)
        except Exception as new_page_err:
            log(f"⚠️ 新建页面也失败: {type(new_page_err).__name__}: {new_page_err}", "GoogleFX")
            raise

    page.bring_to_front()
    if "labs.google" not in page.url:
        # 2026-07-19 复盘：这里曾经是唯一一处不带 try/except 的 Flow 导航——
        # google_fx_video.py 的 _prepare_page 里两处同款 page.goto 都用
        # try/except 包住只记警告不重新抛出（页面偶发慢一点，_wait_toolbar_ready
        # 的等待+刷新重试足够自愈），唯独这里裸调用。一次 60s 导航超时（网络
        # 抖动/Google 页面偶发缓慢）就会顺着 _run_round 的 with 块一路冒到
        # run() 的通用 except，被记成"批量生成过程发生致命错误"直接放弃
        # ——整个 chunk（最多 5 段视频）连一次重试机会都没有就全部判失败。
        try:
            page.goto("https://labs.google/fx/tools/flow", timeout=60000, wait_until="domcontentloaded")
            random_sleep(1, 2)
        except Exception as nav_err:
            log(f"⚠️ 导航到 Flow 首页超时/失败: {type(nav_err).__name__}: {nav_err}，继续尝试后续步骤...", "GoogleFX")

    ensure_flow_workspace(page)

    try:
        _raise_if_manual_intervention_required(page, context_label="Google FX 页面初始化")
    except RuntimeError as e:
        err_msg = str(e).lower()
        if "login_required" in err_msg or "verification_required" in err_msg:
            # 登录失效 / 二次验证：换 IP 没用，只能人工处理
            if not on_event:
                raise
            if not wait_out_manual_intervention(
                page, context_label="Google FX 页面初始化",
                cancel_check=cancel_check, on_event=on_event,
            ):
                raise _ManualInterventionTimeoutError(
                    f"Google FX 页面初始化需要人工处理，等待超时: {e}"
                )
        elif "security_check" in err_msg or "unusual" in err_msg or "captcha" in err_msg:
            if not allow_account_switch:
                raise RuntimeError(
                    "PINNED_CANVAS_ACCOUNT_UNAVAILABLE: 原画布所属账号触发安全验证，"
                    "为避免迭代落入其他账号的新画布，已停止自动换号"
                ) from e
            log("⚠️ 检测到 Google 安全拦截，尝试换号后重试...", "GoogleFX")
            # 关闭当前浏览器
            try:
                browser.close()
            except Exception:
                pass
            # 换号：安全拦截是账号侧的风控评分，换出口 IP 救不回来
            switched = _switch_account_on_failure(force_switch=True, exclude=tried_accounts)
            if switched:
                tried_accounts.add(switched)
            # 重新启动浏览器（换号后连的是新 profile；IP 轮换交回正常节奏，
            # 这里不额外触发一次，跟原来"刚换过就不重复换"的意图一致）
            ws_url = get_ads_ws_url(auto_rotate_proxy=False)
            browser = _connect_over_cdp_with_retry(playwright_ctx, ws_url)
            context = browser.contexts[0]
            page = find_or_create_page(
                context, "labs.google", cancel_check=cancel_check,
                context_label="Google FX 换号后浏览器启动")
            page.bring_to_front()
            try:
                page.goto("https://labs.google/fx/tools/flow", timeout=60000, wait_until="domcontentloaded")
                random_sleep(2, 4)
            except Exception as nav_err:
                log(f"⚠️ 换 IP 后导航到 Flow 首页超时/失败: {type(nav_err).__name__}: {nav_err}，继续尝试后续步骤...", "GoogleFX")
            ensure_flow_workspace(page)
            # 再次检测，如果仍然被拦截：能透出状态就等人工处理，否则直接抛
            if on_event:
                if not wait_out_manual_intervention(
                    page, context_label="Google FX 换 IP 后重试",
                    cancel_check=cancel_check, on_event=on_event,
                ):
                    raise _ManualInterventionTimeoutError(
                        "Google FX 换 IP 后仍被拦截，等待人工处理超时"
                    )
            else:
                _raise_if_manual_intervention_required(page, context_label="Google FX 换 IP 后重试")
        else:
            raise

    return browser, page

def _prepare_fx_canvas(page, has_refs, require_fresh_canvas=False):
    """准备 Flow 画布并等待工具栏；绝不删除或清理画布上的媒体卡片。

    ``require_fresh_canvas``：本次任务刚拿到一块全新画布，下面那条"优先打开最新历史
    项目"的兜底必须整条禁用——最新历史项目正是上一个任务的画布，2026-08-05 的串图
    事故就是这么发生的。此时输入框没就绪只能新建项目，不能回历史项目里找。
    """

    # 如果当前没有打开任何项目（即输入框/工具栏不存在），优先尝试打开最新历史项目，找不到再新建项目
    toolbar_exists = _find_fx_prompt_input(page, announce=False) is not None
    if not toolbar_exists and require_fresh_canvas:
        log("📍 输入框未就绪；本次任务要求全新画布，直接新建项目（不回历史项目）", "GoogleFX")
        try:
            _click_new_project_button(page)
        except Exception as e:
            log(f"⚠️ 新建项目失败: {e}", "GoogleFX")
    elif not toolbar_exists:
        log("📍 未检测到活跃项目输入框，优先尝试打开最新历史项目...", "GoogleFX")
        project_clicked = False
        try:
            project_clicked = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                // 查找 href 包含 /project/ 或 /tools/flow/project/ 的链接
                const projectLink = links.find(a => {
                    const href = a.getAttribute('href') || '';
                    return href.includes('/project/') || href.includes('/tools/flow/project/');
                });
                if (projectLink && projectLink.offsetParent !== null) {
                    projectLink.click();
                    return true;
                }
                return false;
            }""")
            if project_clicked:
                log("✅ 成功点击最新历史项目卡片，等待加载...", "GoogleFX")
                random_sleep(3, 5)
        except Exception as e:
            log(f"⚠️ 尝试打开历史项目异常: {e}", "GoogleFX")

        if not project_clicked:
            log("📍 未能打开历史项目，尝试新建项目...", "GoogleFX")
            try:
                if not _click_new_project_button(page):
                    log("⚠️ 未能通过标准按钮新建项目，尝试直接导航到 Flow URL 刷新并新建项目", "GoogleFX")
                    page.goto("https://labs.google/fx/tools/flow", timeout=60000, wait_until="domcontentloaded")
                    random_sleep(2, 4)
                    project_clicked_retry = page.evaluate("""() => {
                        const links = Array.from(document.querySelectorAll('a'));
                        const projectLink = links.find(a => {
                            const href = a.getAttribute('href') || '';
                            return href.includes('/project/') || href.includes('/tools/flow/project/');
                        });
                        if (projectLink && projectLink.offsetParent !== null) {
                            projectLink.click();
                            return true;
                        }
                        return false;
                    }""")
                    if project_clicked_retry:
                        log("✅ 刷新后成功点击最新历史项目卡片，等待加载...", "GoogleFX")
                        random_sleep(3, 5)
                    else:
                        _click_new_project_button(page)
            except Exception as e:
                log(f"⚠️ 强制新建项目失败: {e}", "GoogleFX")

    if has_refs:
        # 如果有参考图，且是在项目内，等待画布卡片加载完毕
        try:
            log("⏳ 等待画布图片卡片加载...", "GoogleFX")
            page.locator("div[data-tile-id]").first.wait_for(state="visible", timeout=10000)
            log("✅ 画布图片卡片已加载", "GoogleFX")
        except Exception:
            log("⚠️ 等待画布图片卡片超时，可能画布为空，将继续后续流程", "GoogleFX")
    # 现读而不是用 import 时冻结的 MAX_WAIT_SECONDS：控制台改「单张/单条最长等待」
    # 后本轮就该跟着变（理由见 config.get_runtime_max_wait_seconds 的 docstring）。
    # 这是 config 里点名"一律走 get_runtime_*"之后唯一漏掉的等待点。
    _wait_for_fx_toolbar(page, timeout=get_runtime_max_wait_seconds())

def _count_error_cards(page):
    """用 JS 数唯一 Failed 卡片 DOM 元素，避免多选择器重复计数。
    🔧 2026-05-16: 只计 warning/error icon 确认的失败卡片，不触发自动重试。
    """
    try:
        return page.evaluate("""() => {
            const seen = new Set();
            const tiles = Array.from(document.querySelectorAll('div[data-tile-id]'));
            for (const tile of tiles) {
                const tileId = tile.getAttribute('data-tile-id');
                if (!tileId || seen.has(tileId)) continue;
                const t = (tile.innerText || '').toLowerCase();
                const hasFailText = t.includes('failed') || t.includes('something went wrong') || t.includes('unusual activity') || t.includes('help center') || t.includes('失败') || t.includes('出错了') || t.includes('生成失败');
                if (!hasFailText) continue;
                // 额外校验: 必须有 warning/error icon 且可见
                const isVisible = (el) => {
                    let cur = el;
                    while (cur) {
                        const style = window.getComputedStyle(cur);
                        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                            return false;
                        }
                        cur = cur.parentElement;
                    }
                    return true;
                };
                const icons = Array.from(tile.querySelectorAll('i'));
                const hasWarningIcon = icons.some(i => {
                    const txt = (i.innerText || i.textContent || '').trim().toLowerCase();
                    const isWarning = txt === 'warning' || txt === 'error' || txt === 'error_outline';
                    return isWarning && isVisible(i);
                });
                if (hasWarningIcon) seen.add(tileId);
            }
            return seen.size;
        }""")
    except Exception as e:
        log(f"  ⚠️ _count_error_cards JS 失败: {type(e).__name__}", "GoogleFX")
        return 0

def _delete_failed_cards(page):
    """兼容旧调用的安全空操作：媒体生成永远不删除 Flow 画布卡片。"""
    return 0

# ── 输入区（prompt bar）的结构定位 ──
# 2026-07-26 用只读探针 dump 真机 DOM 后重写。此前所有定位都靠
# 「rect.top >= innerHeight - 420」这条视口几何判定来区分「输入区」和「页面其它
# 地方」，既脆（窗口高度一变就偏）又不表达意图。真实结构是稳定的，直接照结构走：
#
#   <div>                              ← prompt bar：含编辑器 + 发送键 arrow_forward
#     <div><div data-slate-editor>…</div></div>
#     <div>… add_2 / Agent / 模型 / <i>arrow_forward</i> …</div>
#     <div>                            ← 有内容时才出现的「清空」区
#       <button><i>close</i><span>Clear prompt</span></button>
#     </div>
#     <button data-card-open="false">   ← 参考图 chip（每张一个）
#       <div><img alt="A piece of media … present in your collection."></div>
#       <div><i>cancel</i></div>
#     </button>
#   </div>
#
# 于是：bar = 编辑器往上第一个含 arrow_forward 的祖先；chip = bar 内带 <img> 的
# button[data-card-open]。两者都不再依赖坐标。
_PROMPT_BAR_JS = """
    const _ed = document.querySelector("[data-slate-editor='true']");
    let bar = null;
    if (_ed) {
        let c = _ed.parentElement, d = 0;
        while (c && d < 8) {
            const icons = Array.from(c.querySelectorAll('i')).map(i => (i.textContent || '').trim());
            if (icons.includes('arrow_forward')) { bar = c; break; }
            c = c.parentElement; d++;
        }
    }
    const scope = bar || document;
    function _vis(el) {
        let cur = el;
        while (cur) {
            const s = window.getComputedStyle(cur);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
            cur = cur.parentElement;
        }
        return true;
    }
    // 参考图 chip：必须带缩略图，排除工具栏上那些同样是 button 的控件
    const chips = Array.from(scope.querySelectorAll("button[data-card-open]"))
        .filter(b => _vis(b) && b.querySelector('img'));
    // 「Clear prompt」一键清空（文字 + 全部参考图）。textContent 里同时含图标名
    // 'close' 与无障碍标签 'Clear prompt'，所以用 includes 而不是全等。
    const clearBtn = Array.from(scope.querySelectorAll('button'))
        .find(b => _vis(b) && (b.textContent || '').toLowerCase().includes('clear prompt')) || null;
    // Slate 只在编辑器为空时渲染 placeholder，所以它在 == 编辑器已空。
    // 绝不能拿 innerText 判空：placeholder 文案本身就有 29 个字符，
    // 会让「已经空了」被误判成「还剩 29 字没清掉」。
    const editorEmpty = !_ed || !!_ed.querySelector('[data-slate-placeholder]');
"""


def read_prompt_bar_state(page):
    """输入区当前状态：{'chips': n, 'has_clear': bool, 'editor_empty': bool, 'bar': bool}。

    清理的每一步都拿它复核——判空一律走这里，不要在别处用 inner_text() 自己判，
    否则会被 Slate 的 placeholder 骗（见 _PROMPT_BAR_JS 里的说明）。
    """
    try:
        st = page.evaluate("() => {" + _PROMPT_BAR_JS + """
            return {chips: chips.length, has_clear: !!clearBtn,
                    editor_empty: editorEmpty, bar: !!bar};
        }""")
    except Exception as e:
        log(f"  ⚠️ 读取输入区状态失败: {type(e).__name__}", "GoogleFX")
        return {"chips": 0, "has_clear": False, "editor_empty": True, "bar": False}
    return st or {"chips": 0, "has_clear": False, "editor_empty": True, "bar": False}


# 2026-08-01 清理：这里原有 _count_prompt_reference_chips()，一行包装
# read_prompt_bar_state(page)["chips"]，全 repo 零调用者——所有调用点都是直接读
# read_prompt_bar_state() 的返回（那样一次调用能同时拿到 chips / editor_empty / has_clear，
# 走包装反而要多打一次 DOM）。


def _dump_prompt_bar_for_diagnosis(page, why):
    """清不干净时把输入区结构打进日志——UI 一改版就得靠这段还原现场，
    不然只能看到「还剩 N 个」却不知道 N 个长什么样。"""
    try:
        html = page.evaluate("() => {" + _PROMPT_BAR_JS + """
            const parts = chips.slice(0, 3).map(c => c.outerHTML.slice(0, 600));
            return JSON.stringify({
                barTail: bar ? bar.outerHTML.slice(-1200) : null,
                chipHtml: parts,
            });
        }""")
        log(f"  🔬 输入区结构快照（{why}）: {str(html)[:1800]}", "GoogleFX")
    except Exception as e:
        log(f"  ⚠️ 输入区结构快照失败: {type(e).__name__}", "GoogleFX")


def _click_clear_prompt_button(page):
    """点「Clear prompt」——Flow 自带的一键清空，文字 + 全部参考图一起清掉。"""
    try:
        return bool(page.evaluate("() => {" + _PROMPT_BAR_JS + """
            if (!clearBtn) return false;
            clearBtn.click();
            return true;
        }"""))
    except Exception as e:
        log(f"  ⚠️ 点击 Clear prompt 失败: {type(e).__name__}", "GoogleFX")
        return False


def _click_one_chip_cancel(page):
    """回退：真鼠标点掉一个 chip 上的 ✕（<i>cancel</i> 覆盖层）。

    刻意用真实鼠标坐标而不是 el.click()：✕ 是叠在 chip 按钮上的覆盖层，
    合成事件容易冒泡到 chip 按钮本身（那是「打开卡片」，不是「移除」）。
    """
    try:
        box = page.evaluate("() => {" + _PROMPT_BAR_JS + """
            for (const c of chips) {
                const icon = Array.from(c.querySelectorAll('i'))
                    .find(i => (i.textContent || '').trim().toLowerCase() === 'cancel');
                if (!icon) continue;
                const r = icon.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) continue;
                return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }
            return null;
        }""")
    except Exception as e:
        log(f"  ⚠️ 定位 chip ✕ 失败: {type(e).__name__}", "GoogleFX")
        return False
    if not box:
        return False
    try:
        page.mouse.move(box["x"], box["y"])
        random_sleep(0.15, 0.3)
        page.mouse.click(box["x"], box["y"])
        return True
    except Exception as e:
        log(f"  ⚠️ 点击 chip ✕ 失败: {type(e).__name__}", "GoogleFX")
        return False


def _clear_prompt_reference_chips_image(page, max_rounds=8):
    """（图片生成流程使用；见上方 _clear_prompt_reference_chips_video 的改名说明）

    把输入区恢复成「空文字 + 零参考图」。返回清掉的参考图 chip 数。

    2026-07-26 用只读探针 dump 真机 DOM 后重写。此前两版都是「找 chip 上的移除键
    逐个点」，而真机上 Flow 自带一个一键清空按钮：

        <button><i>close</i><span>Clear prompt</span></button>

    它就挂在输入条里（有内容时才渲染），一下把提示词文字和所有参考图 chip 全清掉，
    比逐个点 chip 稳得多，也不用再猜每个 chip 的移除控件长什么样。

    被这次重写修掉的三个坑：
      1. 旧扫描要求 button[data-card-open] 里有文字恰为 'cancel'/'close' 的 <i>，
         并且元素得落在视口底部 420px 内。前者在 chip 未 hover 时未必成立，后者
         窗口一变高就整体失配——两个条件任一不满足，函数就静默数出 0 个、一个也不清。
      2. 判空用 inner_text()。Slate 空编辑器会渲染 placeholder
         「What do you want to create?」，innerText 因此恒为 29~30 字，
         「已经空了」被当成「还剩 29 字」。
      3. 回退选择器 button[data-state='closed'] i 是 page-wide 的，能匹配页面上每个
         折叠态 Radix 触发器，点下去就是在无关按钮上乱点。

    现在：优先 Clear prompt（每点一次都复核），点不动再逐个 ✕，仍清不掉就把输入区
    结构快照打进日志，并如实上报残留。
    """
    removed_total = 0
    try:
        st = read_prompt_bar_state(page)
        if not st.get("bar"):
            log("  ⚠️ 未定位到输入条（页面可能还没进项目），跳过清理", "GoogleFX")
            return 0
        before = int(st.get("chips") or 0)
        if before == 0 and st.get("editor_empty"):
            return 0   # 本来就是干净的：一次点击都不发

        # ── 主路径：Clear prompt 一键清空 ──
        for _ in range(3):
            if not st.get("has_clear"):
                break
            if not _click_clear_prompt_button(page):
                break
            random_sleep(0.25, 0.45)
            st = read_prompt_bar_state(page)
            if not st.get("chips") and st.get("editor_empty"):
                break

        # ── 回退：逐个点 chip 上的 ✕，每点一次复核一次 ──
        stalled = 0
        for _ in range(max_rounds):
            cur = int(st.get("chips") or 0)
            if cur == 0:
                break
            if not _click_one_chip_cancel(page):
                break
            random_sleep(0.25, 0.45)
            st = read_prompt_bar_state(page)
            after = int(st.get("chips") or 0)
            if after >= cur:
                stalled += 1
                if stalled >= 2:
                    log(f"  ⚠️ 仍有 {after} 个参考图 chip 点不掉，停止重试", "GoogleFX")
                    break
            else:
                stalled = 0

        remaining = int(st.get("chips") or 0)
        removed_total = max(0, before - remaining)

        if removed_total:
            log(f"🧹 清除 {removed_total} 个历史参考图 chip（输入区）", "GoogleFX")
        if remaining or not st.get("editor_empty"):
            log(f"  ⚠️ 输入区未清干净：残留 chip={remaining}, 文字已空={st.get('editor_empty')}",
                "GoogleFX")
            _dump_prompt_bar_for_diagnosis(page, f"残留 chip={remaining}")
    except Exception as e:
        log(f"  ⚠️ _clear_prompt_reference_chips_image 失败: {type(e).__name__}: {e}", "GoogleFX")
    return removed_total

def _mount_uuid_as_ref(page, uuid):
    """
    将刚上传到画廸的图片挂载到输入框参考区。
    步骤: 关闭 add_2 面板 → 等待画布卡片 → more_vert → Add to Prompt
    """
    try:
        _safe_press_escape(page, "_mount_uuid_as_ref 关闭资产面板")
        random_sleep(0.5, 0.8)
        try:
            page.locator(f"[data-tile-id] img[src*='{uuid}']").first.wait_for(
                state="visible", timeout=10000
            )
        except Exception:
            pass  # 没有 data-tile-id 也继续尝试
        ok = _add_flow_image_to_prompt(page, uuid)
        if ok:
            log(f"  ✅ 参考图已挂载: {uuid[:16]}...", "GoogleFX")
        else:
            log(f"  ⚠️ _add_flow_image_to_prompt 返回 False: {uuid[:16]}...", "GoogleFX")
        return ok
    except Exception as e:
        log(f"  ⚠️ 挂载参考图失败: {e}", "GoogleFX")
        _safe_press_escape(page, "挂载参考图失败后关闭弹层")
        return False


# ==============================================================================
# 📡 媒体捕获 / 输出目录 (原 services/google_fx.py，函数体逐字未改动；
# 只被 google_fx_video.py / google_fx_image.py 使用，挪到这里后 google_fx.py
# 不再需要被 video.py/image.py 反向导入)
# ==============================================================================

def _make_response_handler(captured_data, mode="video"):
    """创建网络响应拦截回调 (视频/图片模式)，捕获的 URL 追加到 captured_data。"""
    def handler(response):
        try:
            url = response.url
            ct = (response.headers.get("content-type", "") or "").lower()
            cl = int(response.headers.get("content-length", 0) or 0)
            lower_url = url.lower()
            if mode == "video":
                if ("video" in ct or ".mp4" in lower_url) and cl > 50000:
                    captured_data.append((time.time(), url))
                    log(f"📡 捕获视频资源: {url[:80]}", "GoogleFX")
                elif "mediaUrlRedirect" in url or "media.get" in url:
                    log(f"📡 捕获视频API响应: {url[:100]}", "GoogleFX")
            elif mode == "image":
                if "video" in ct or "/video/" in lower_url or ".mp4" in lower_url:
                    return
                # 路径1: 重定向跟随 (Playwright 自动 307)
                redir = response.request.redirected_from
                if redir:
                    orig = redir.url
                    if "getMediaUrlRedirect" in orig or "MediaUrlRedirect" in orig:
                        captured_data.append((time.time(), url))
                        log(f"📡 捕获图片重定向: {url[:100]}", "GoogleFX")
                        return
                # 路径2: 直接匹配 redirect 请求
                if "getMediaUrlRedirect" in url or "MediaUrlRedirect" in url:
                    captured_data.append((time.time(), url))
                    return
                # 路径3: GCS 图片资源
                if "storage.googleapis.com" in url and ("ai-sandbox" in url or "videofx" in url):
                    if "image/" in ct or cl > 10000 or re.search(r"\.(png|jpe?g|webp)(\?|$)", url, re.I):
                        captured_data.append((time.time(), url))
                        log(f"📡 捕获GCS图片: {url[:100]}", "GoogleFX")
                        return
                # 路径4: Google 域名图片
                if any(k in url for k in ["googleusercontent.com", "gstatic.com", "ggpht.com"]):
                    if ("image/" in ct and cl > 10000) or re.search(r"\.(png|jpe?g|webp)(\?|$)", url, re.I):
                        captured_data.append((time.time(), url))
                        return
                # 路径5: 兜底大图
                if "image/" in ct and cl > 50000 and "labs.google" not in url:
                    captured_data.append((time.time(), url))
                    log(f"📡 捕获大图片: {url[:80]} ({cl}B)", "GoogleFX")
        except Exception as e:
            log(f"  ⚠️ handle_response 异常: {type(e).__name__}", "GoogleFX")
    return handler

def _ensure_output_dir(req, default_subdir):
    """解析并创建输出目录，保持现有默认规则不变。"""
    output_dir = req.output_path if (hasattr(req, "output_path") and req.output_path) else os.path.join(OUTPUT_DIR, default_subdir)
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


# ==============================================================================
# ⏹️ 取消检测 + 紧急代理轮换 + 抓包缓冲上限 (原 services/google_fx.py，
# 函数体逐字未改动；只被 google_fx.py 自身与 google_fx_image.py 使用，挪到这里
# 后 google_fx_image.py 不再需要反向导入 google_fx.py)
# ==============================================================================

def _check_cancelled():
    if cancel_flag.is_cancelled:
        log("🛑 任务已取消，终止执行", "GoogleFX")
        raise RuntimeError("任务已取消")
    if cancel_flag.deadline_exceeded():
        log("⏰ 请求已超出时间预算，终止执行", "GoogleFX")
        raise RuntimeError("请求超时：超出时间预算 (request budget exceeded)")


def _cancellable_sleep(seconds, step=0.5):
    """分段睡，每段之间 _check_cancelled()。

    调用方原来是一句 time.sleep(15~45)：用户点取消后要干等这一觉睡完才可能停下来。
    冷却/节奏等待这类长睡眠是"取消了半天没反应"里占比最大的一段，尤其在上层已经
    不再靠关浏览器来硬停之后（关浏览器会清空 Flow 画布、打松登录 token）。"""
    remaining = float(seconds)
    while remaining > 0:
        _check_cancelled()
        chunk = min(remaining, step)
        time.sleep(chunk)
        remaining -= chunk
    _check_cancelled()

# ── 失败重试的干预条件 (2026-07-25/26) ────────────────────────────────────────
# 第一版旧行为：图片链路 except 到任何异常都强制换一次 IP。于是 UI 自动化自己的
# 毛病（找不到配置按钮、找不到输入框、配置没点对、脚本 Bug）也照样换 IP——实测
# 一次"未找到底部配置按钮"打了 4 次强制换 IP，而生成过程中一张图都没提交过、一条
# 生成报错都没有。
# 2026-07-26 起再进一步：干预动作本身从「换 IP」统一改成「换号」。换 IP 只换出口
# 地址，账号侧的风控评分不跟着重置（被判 unusual activity 的号换个 IP 照样被判），
# 而频繁换 IP 还会关浏览器重连、把 Flow 登录 token 打失效。IP 轮换回归
# MIYA_ROTATE_THRESHOLD 配置的正常节奏，风控类失败一律换号重试。
# 判定口径不变，只是结论从"换不换 IP"变成"换不换号"：
#   - 命中 _RISK_CONTROL_ERROR_TOKENS → 换号
#   - 命中 _AUTOMATION_ERROR_TOKENS   → 不换（UI 自动化/脚本自身的问题，换了也没用）
#   - 都不命中（未知错误）            → 默认不换，只留日志说明判定结果；
#     确实需要"宁可错换"的场景把 MIYA_ROTATE_ON_UNKNOWN_ERROR=1 打开即可
#     （沿用这个环境变量名，避免已有 .env 失效）。

_RISK_CONTROL_ERROR_TOKENS = (
    "异常活动", "unusual activity", "suspicious", "风控",
    "所有 prompt 均未捕获到图片", "生成失败", "something went wrong",
    "quota", "配额", "rate limit", "too many requests", "429", "403",
    "blocked", "forbidden", "被封", "ip 被封", "ip blocked",
    "net::err_", "proxy", "代理",
)

_AUTOMATION_ERROR_TOKENS = (
    "未找到底部配置按钮", "无法找到输入框", "配置未选对", "配置未完成",
    "等待底部工具栏超时", "浏览器/标签页已关闭", "manual_required", "需要人工处理",
    "flow_canvas_unavailable", "usable flow canvas", "flow workspace",
    "任务已取消", "超出时间预算", "request budget exceeded",
    "no prompts provided", "timeout", "not found", "locator",
    "attributeerror", "typeerror", "keyerror", "importerror", "modulenotfounderror",
)

_ACCOUNT_LOGIN_ERROR_TOKENS = (
    "manual_required:login_required",
    "google 登录页面",
    "google login page",
    "sign in to google",
    "account_login_required",
)


def _classify_failure_for_switch(reason):
    """判断这次失败该不该换号，返回 (should_switch, 判定说明)。"""
    text = (str(reason) or "").lower()
    if not text.strip():
        return False, "无错误信息"
    # 登录页是账号自身状态，不是选择器/脚本故障。必须先于通用
    # manual_required 自动化词判断，否则会在同一失效账号上反复重试。
    for token in _ACCOUNT_LOGIN_ERROR_TOKENS:
        if token in text:
            return True, f"账号登录失效（命中 '{token}'）"
    # 积分/配额耗尽也是账号自身状态，必须先于自动化类（如 timeout）判断，
    # 避免"超时未捕获到 URL（积分耗尽）"被 timeout 拦截而拒绝换号。
    from .google_fx_credit import is_credit_exhausted_message
    if is_credit_exhausted_message(text) or any(tok in text for tok in (
        "insufficient_credits", "quota_exhausted", "resource_exhausted", "quota_exceeded"
    )):
        return True, "账号积分/配额耗尽（命中积分耗尽特征）"
    # 自动化类先判：'timeout'/'not found' 这类词在两边都可能出现，
    # 但"UI 元素超时/找不到"远比风控类超时常见，误判成换号的代价更大。
    for token in _AUTOMATION_ERROR_TOKENS:
        if token in text:
            return False, f"UI 自动化/脚本类错误（命中 '{token}'）"
    for token in _RISK_CONTROL_ERROR_TOKENS:
        if token in text:
            return True, f"疑似账号/出口风控类错误（命中 '{token}'）"
    if os.environ.get("MIYA_ROTATE_ON_UNKNOWN_ERROR", "0") == "1":
        return True, "未知错误类型（MIYA_ROTATE_ON_UNKNOWN_ERROR=1，按需换号）"
    return False, "未知错误类型（默认不换号，如需相反行为设 MIYA_ROTATE_ON_UNKNOWN_ERROR=1）"


def _switch_account_on_failure(reason=None, force_switch=False, exclude=()):
    """检测到**生成**报错时，切到号池里的下一个可用账号（原「紧急换 IP」的位置）。

    reason：本次失败的错误信息。不传 = 无条件换号，只有确定是生成侧失败的调用点
    才该这么用；带 reason 的调用点会先过 _classify_failure_for_switch，UI 自动化
    自己的毛病不再触发换号。
    force_switch：调用方已经确认这是生成侧失败，跳过分类直接换。
    exclude：本轮已经试过的 user_id，避免换回刚失败的号。

    返回换上的 user_id（没换成返回 None）。换号写 per-task 的 account_binding
    上下文（不再改进程级环境变量），下一次开浏览器时生效——调用方负责重连浏览器。
    """
    if reason is not None and not force_switch:
        should_switch, verdict = _classify_failure_for_switch(reason)
        if not should_switch:
            log(f"⏭️ 跳过换号：{verdict}；生成过程未出现风控相关报错", "GoogleFX")
            return None
        log(f"🚨 换号判定：{verdict}", "GoogleFX")
    try:
        from ..utils.account_pool import switch_to_next_account
        log("🔁 检测到生成报错/卡片异常，正在切换号池账号...", "GoogleFX")
        chosen = switch_to_next_account(exclude=exclude)
        if chosen:
            log(f"✅ 已切到账号 {chosen['user_id']}（{chosen.get('name') or '未命名'}）", "GoogleFX")
            return chosen["user_id"]
        log("ℹ️ 换号跳过：号池里没有可换的账号，沿用当前账号重试", "GoogleFX")
    except Exception as e:
        log(f"⚠️ 换号失败: {type(e).__name__}: {e}", "GoogleFX")
    return None

# ── 提交节奏闸门 ────────────────────────────────────────────────────────────
# 历史：每次进批量生成前无条件 sleep(15~25s)「降低提交节奏被识别风险」。但风控看
# 的是**两次提交之间**隔了多久，而不是脚本启动前干等了多久——而一条腿跑完往往已
# 经过去好几分钟（每张图串行等生成），这一觉纯属白睡。2026-07-26 实测 server.log：
# 21 次 pacing delay 睡掉 376s，其中绝大多数前面刚跑完一整条腿。
# 现行为：只补齐「距上次真实提交」还差的那部分，够了就不睡。本进程还没提交过任何
# 东西时（刚起服务/第一条腿）也不睡——没有需要拉开的间隔。
# 两条链（帧/视频）在 SPARK 里是同进程 import 调用的，所以模块级时间戳跨腿有效。
_LAST_FX_SUBMIT_TS = 0.0


def note_fx_submit():
    """记录一次真实提交的时刻，供 fx_pacing_wait 计算间隔。"""
    global _LAST_FX_SUBMIT_TS
    _LAST_FX_SUBMIT_TS = time.time()


def fx_pacing_bounds():
    """提交间隔的下/上界（秒）。可热调：SPARK 控制台的运行配置会写这两个环境变量。

    min >= max 时退化为固定间隔 min，避免 random.uniform 拿到反向区间。
    """
    from ..config import runtime_env_or_default
    try:
        low = float(runtime_env_or_default("GOOGLE_FX_PACING_MIN_SECONDS", "15"))
    except (TypeError, ValueError):
        low = 15.0
    try:
        high = float(runtime_env_or_default("GOOGLE_FX_PACING_MAX_SECONDS", "25"))
    except (TypeError, ValueError):
        high = 25.0
    low = max(0.0, low)
    return (low, max(low, high))


def fx_pacing_wait(min_gap=15.0, max_gap=25.0, log_tag="GoogleFX"):
    """确保距上一次真实提交至少隔 min_gap~max_gap 秒，返回实际睡眠秒数。"""
    if _LAST_FX_SUBMIT_TS <= 0:
        log("🕐 本进程尚无提交记录，跳过节奏等待", log_tag)
        return 0.0
    target = random.uniform(min_gap, max_gap)
    remain = target - (time.time() - _LAST_FX_SUBMIT_TS)
    if remain <= 0:
        log(f"🕐 距上次提交已隔 {time.time() - _LAST_FX_SUBMIT_TS:.0f}s ≥ {target:.1f}s，无需节奏等待", log_tag)
        return 0.0
    log(f"🕐 Pacing: 距上次提交仅 {time.time() - _LAST_FX_SUBMIT_TS:.0f}s，补足 {remain:.1f}s 后开始", log_tag)
    _cancellable_sleep(remain)
    return remain


def _page_is_gone(page):
    """浏览器/标签页已经没了。再怎么轮询都不会好，调用方应立刻失败而不是等满超时。

    背景：_raise_if_manual_intervention_required 对任何非 RuntimeError 异常都只
    记一行日志然后咽掉，于是浏览器被关掉之后 _wait_for_fx_toolbar 会拿着一个死
    page 一秒一次地空转到超时。2026-07-26 实测 server.log 里 177 次
    「人工接管状态检测失败: TargetClosedError」＝ 180s 纯空转。
    """
    try:
        return page.is_closed()
    except Exception:
        return True


_CAPTURED_DATA_MAXLEN = 200
