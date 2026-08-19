/* =====================================================================
   顶部工作区标签栏的自定义（排序 / 隐藏）
   ---------------------------------------------------------------------
   标签栏（.mobile-nav-tabs）里的入口按需使用，这里给这条栏加一个
   末尾的「⋯」按钮，点开后可以拖拽排序、按眼睛图标隐藏，偏好落在
   localStorage（spark_nav_prefs），下次开页面直接生效。

   两条刻意的约束：
   1. 隐藏即彻底隐藏：被隐藏的标签在标签栏中不会因为处于 active 状态而意外显现；
      若当前激活的标签被隐藏，自动平滑切换到第一个可见标签。
   2. 至少保留一个可见标签，全部隐藏会让整条栏变成空盒子。

   顶部 app-switcher（创意工坊 / 图像工坊 / 控制台）不在管辖范围内：那三个
   是应用级入口（其中两个是真链接，跳到别的 HTML 页），藏掉等于把出口封死。
   ===================================================================== */

(function () {
    'use strict';

    const PREFS_KEY = 'spark_nav_prefs';
    const BAR_SELECTOR = '.mobile-nav-tabs';
    const BTN_SELECTOR = '.mobile-nav-btn';

    // { order: ['main-tab-config', ...], hidden: ['main-tab-replica', ...] }
    function readPrefs() {
        try {
            const raw = localStorage.getItem(PREFS_KEY);
            if (!raw) return { order: [], hidden: [] };
            const parsed = JSON.parse(raw);
            return {
                order: Array.isArray(parsed.order) ? parsed.order.filter(x => typeof x === 'string') : [],
                hidden: Array.isArray(parsed.hidden) ? parsed.hidden.filter(x => typeof x === 'string') : [],
            };
        } catch (e) {
            console.warn('[nav] 读取标签栏偏好失败，回退默认布局', e);
            return { order: [], hidden: [] };
        }
    }

    function writePrefs(prefs) {
        try {
            localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
        } catch (e) {
            console.warn('[nav] 保存标签栏偏好失败', e);
        }
    }

    function getBar() {
        return document.querySelector(BAR_SELECTOR);
    }

    // DOM 里的所有标签按钮（含已隐藏的），按当前 DOM 顺序
    function getButtons() {
        const bar = getBar();
        if (!bar) return [];
        return Array.from(bar.querySelectorAll(BTN_SELECTOR)).filter(btn => btn.id);
    }

    function labelOf(btn) {
        const text = btn.querySelector('.btn-text');
        return (text ? text.textContent : btn.textContent).trim();
    }

    function iconOf(btn) {
        const icon = btn.querySelector('.btn-icon');
        return icon ? icon.textContent.trim() : '•';
    }

    /* ---------- 应用偏好：重排 DOM + 打隐藏标记 ---------- */
    function applyNavPrefs() {
        const bar = getBar();
        if (!bar) return;
        const prefs = readPrefs();
        const buttons = getButtons();
        const byId = new Map(buttons.map(btn => [btn.id, btn]));

        // 存过的顺序在前（跳过已不存在的 id），没在偏好里出现的新标签按原 DOM 顺序补在后面。
        // 这样以后加了新标签页，老用户不会因为偏好里没有它就看不到。
        const ordered = [];
        prefs.order.forEach(id => {
            const btn = byId.get(id);
            if (btn && !ordered.includes(btn)) ordered.push(btn);
        });
        buttons.forEach(btn => { if (!ordered.includes(btn)) ordered.push(btn); });

        const customizeBtn = bar.querySelector('.nav-customize-btn');
        ordered.forEach(btn => bar.appendChild(btn));
        if (customizeBtn) bar.appendChild(customizeBtn); // 「⋯」永远吊在最右

        const hidden = new Set(prefs.hidden);
        if (hidden.size >= ordered.length) hidden.clear(); // 兜底：不允许全隐藏
        ordered.forEach(btn => btn.classList.toggle('nav-hidden', hidden.has(btn.id)));

        // 如果当前高亮的标签属于已隐藏标签，自动切到第一个可见标签，确保始终停留在可见模块
        const activeBtn = ordered.find(btn => btn.classList.contains('active'));
        if (activeBtn && hidden.has(activeBtn.id)) {
            const firstVisible = ordered.find(btn => !hidden.has(btn.id));
            if (firstVisible && typeof switchMainTab === 'function') {
                const tabKey = firstVisible.id.replace(/^main-tab-/, '');
                switchMainTab(tabKey);
            }
        }
    }

    /* ---------- 「⋯」入口 ---------- */
    function ensureCustomizeButton() {
        const bar = getBar();
        if (!bar || bar.querySelector('.nav-customize-btn')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'nav-customize-btn';
        btn.title = '自定义标签栏（排序 / 隐藏）';
        btn.setAttribute('aria-label', '自定义标签栏');
        btn.textContent = '⋯';
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            togglePanel();
        });
        bar.appendChild(btn);
    }

    /* ---------- 自定义面板 ---------- */
    let panelEl = null;

    function togglePanel() {
        if (panelEl) { closePanel(); return; }
        openPanel();
    }

    function closePanel() {
        if (!panelEl) return;
        panelEl.remove();
        panelEl = null;
        document.removeEventListener('click', onOutsideClick, true);
        document.removeEventListener('keydown', onEscape, true);
    }

    function onOutsideClick(e) {
        if (!panelEl) return;
        if (panelEl.contains(e.target)) return;
        if (e.target.closest && e.target.closest('.nav-customize-btn')) return;
        closePanel();
    }

    function onEscape(e) {
        if (e.key === 'Escape') closePanel();
    }

    function openPanel() {
        const bar = getBar();
        if (!bar) return;

        panelEl = document.createElement('div');
        panelEl.className = 'nav-customize-panel';
        panelEl.innerHTML = `
            <div class="ncp-head">
                <strong>自定义标签栏</strong>
                <span class="ncp-hint">拖动排序 · 点眼睛隐藏</span>
            </div>
            <div class="ncp-list"></div>
            <div class="ncp-foot">
                <button type="button" class="ncp-reset">恢复默认</button>
                <button type="button" class="ncp-close">完成</button>
            </div>
        `;
        bar.appendChild(panelEl);
        renderList();

        panelEl.querySelector('.ncp-reset').addEventListener('click', () => {
            writePrefs({ order: [], hidden: [] });
            applyNavPrefs();
            renderList();
            if (typeof showToast === 'function') showToast('标签栏已恢复默认', 'success');
        });
        panelEl.querySelector('.ncp-close').addEventListener('click', closePanel);

        // 捕获阶段监听，避免面板内的按钮点击先冒泡到 document 把自己关掉
        document.addEventListener('click', onOutsideClick, true);
        document.addEventListener('keydown', onEscape, true);
    }

    function renderList() {
        if (!panelEl) return;
        const list = panelEl.querySelector('.ncp-list');
        list.innerHTML = '';

        getButtons().forEach(btn => {
            const isHidden = btn.classList.contains('nav-hidden');
            const row = document.createElement('div');
            row.className = 'ncp-row' + (isHidden ? ' is-hidden' : '');
            row.draggable = true;
            row.dataset.id = btn.id;
            row.innerHTML = `
                <span class="ncp-grip" aria-hidden="true">⠿</span>
                <span class="ncp-icon">${iconOf(btn)}</span>
                <span class="ncp-label"></span>
                <span class="ncp-moves">
                    <button type="button" class="ncp-move" data-dir="-1" title="上移">▲</button>
                    <button type="button" class="ncp-move" data-dir="1" title="下移">▼</button>
                </span>
                <button type="button" class="ncp-eye" title="${isHidden ? '显示' : '隐藏'}">${isHidden ? '🙈' : '👁'}</button>
            `;
            row.querySelector('.ncp-label').textContent = labelOf(btn);

            row.querySelector('.ncp-eye').addEventListener('click', () => toggleHidden(btn.id));
            row.querySelectorAll('.ncp-move').forEach(mv => {
                mv.addEventListener('click', () => moveBy(btn.id, Number(mv.dataset.dir)));
            });
            attachDrag(row, list);
            list.appendChild(row);
        });
    }

    function currentOrder() {
        return getButtons().map(btn => btn.id);
    }

    function toggleHidden(id) {
        const prefs = readPrefs();
        const hidden = new Set(prefs.hidden);
        if (hidden.has(id)) {
            hidden.delete(id);
        } else {
            const total = getButtons().length;
            if (hidden.size + 1 >= total) {
                if (typeof showToast === 'function') showToast('至少要留一个可见标签', 'warning');
                return;
            }
            hidden.add(id);
        }
        writePrefs({ order: currentOrder(), hidden: Array.from(hidden) });
        applyNavPrefs();
        renderList();
    }

    function moveBy(id, dir) {
        const order = currentOrder();
        const i = order.indexOf(id);
        const j = i + dir;
        if (i < 0 || j < 0 || j >= order.length) return;
        order.splice(j, 0, order.splice(i, 1)[0]);
        writePrefs({ order, hidden: readPrefs().hidden });
        applyNavPrefs();
        renderList();
    }

    /* ---------- 面板内的拖拽排序 ---------- */
    let dragId = null;

    function attachDrag(row, list) {
        row.addEventListener('dragstart', (e) => {
            dragId = row.dataset.id;
            row.classList.add('is-dragging');
            try { e.dataTransfer.setData('text/plain', dragId); } catch (_) {}
            if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
        });
        row.addEventListener('dragend', () => {
            row.classList.remove('is-dragging');
            dragId = null;
            commitDomOrder(list);
        });
        row.addEventListener('dragover', (e) => {
            e.preventDefault();
            if (!dragId || dragId === row.dataset.id) return;
            const dragged = list.querySelector(`.ncp-row[data-id="${dragId}"]`);
            if (!dragged) return;
            const rect = row.getBoundingClientRect();
            const after = (e.clientY - rect.top) > rect.height / 2;
            list.insertBefore(dragged, after ? row.nextSibling : row);
        });
    }

    // 拖拽结束时以面板里的行顺序为准写回偏好
    function commitDomOrder(list) {
        const order = Array.from(list.querySelectorAll('.ncp-row')).map(r => r.dataset.id);
        if (!order.length) return;
        writePrefs({ order, hidden: readPrefs().hidden });
        applyNavPrefs();
    }

    /* ---------- 启动 ---------- */
    function init() {
        if (!getBar()) return;
        ensureCustomizeButton();
        applyNavPrefs();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // 调试/外部复位用
    window.applyNavPrefs = applyNavPrefs;
    window.resetNavPrefs = function () {
        writePrefs({ order: [], hidden: [] });
        applyNavPrefs();
    };
})();
