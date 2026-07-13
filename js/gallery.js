/* ==========================================================================
   Gallery (画廊) — 本地历史媒体资产总览与管理。
   数据源是服务端 /api/gallery（实时扫描 outputs/：封面池 covers/、图像工坊
   image-station/、各项目的 frames/ + videos/ + 根目录合成视频），并带引用标注：
   封面 item.in_use（被点子库/任务引用）、项目组 group.orphan（无引用且超过
   24h 活跃宽限）。删除走 /api/gallery/delete，会真正移除本地磁盘文件并重同步
   项目 manifest。依赖宿主应用的 escapeHtml / showToast / openLightbox。
   ========================================================================== */

let galleryLoaded = false;          // 首次进入画廊标签页才扫描（app.js 调 galleryTabEntered）
let galleryLoading = false;
let galleryData = null;             // /api/gallery 的原始返回
let galleryFilter = 'all';          // all | cover | frame | video | studio | orphan
let gallerySearch = '';             // 文件名/项目名子串（不区分大小写）
let gallerySort = 'newest';         // newest | oldest | size | name
const gallerySelected = new Set();  // 已勾选 item.path 集合（跨筛选保留）
const galleryExpanded = new Set();  // 本次会话内点过"展开全部"的组 key
const GALLERY_TRUNCATE = 12;        // 每组默认最多显示的卡片数

// 折叠状态跨会话记住（存组 key 数组）
const GALLERY_COLLAPSED_LS_KEY = 'spark_gallery_collapsed';
const galleryCollapsed = new Set((() => {
    try { return JSON.parse(localStorage.getItem(GALLERY_COLLAPSED_LS_KEY) || '[]'); }
    catch (e) { return []; }
})());

const GALLERY_KIND_LABELS = {
    cover: '封面',
    studio: '工坊',
    frame: '帧',
    video: '片段',
    merged: '成片',
    other: '图片',
};

const GALLERY_GROUP_ICONS = { covers: '🖼️', studio: '🎨', project: '📁' };

function galleryTabEntered() {
    if (!galleryLoaded && !galleryLoading) refreshGallery();
}

function galleryFmtSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return Math.max(1, Math.round(bytes / 1024)) + ' KB';
}

function galleryFmtTime(mtime) {
    try {
        const d = new Date(mtime * 1000);
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    } catch (e) { return ''; }
}

// 文件名可能含空格/中文/引号等：属性值走 escapeHtml，URL 走 encodeURI 并补编
// encodeURI 不处理的 # / ?（静态服务器会把它们当 fragment/query 截断）
function galleryEncodeUrl(url) {
    return encodeURI(url).replace(/#/g, '%23').replace(/\?/g, '%3F');
}

async function refreshGallery() {
    if (galleryLoading) return;
    galleryLoading = true;
    const container = document.getElementById('gallery-groups');
    if (container && !galleryData) {
        container.innerHTML = '<div class="gallery-status">📡 正在扫描本地媒体文件…</div>';
    }
    try {
        const res = await fetch('/api/gallery');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && data.error) throw new Error(data.error);
        galleryData = data;
        galleryLoaded = true;
        // 清掉磁盘上已不存在的勾选项，避免"删除所选"携带幽灵路径
        const alive = new Set();
        (galleryData.groups || []).forEach(g => (g.items || []).forEach(it => alive.add(it.path)));
        [...gallerySelected].forEach(p => { if (!alive.has(p)) gallerySelected.delete(p); });
    } catch (e) {
        galleryLoading = false;
        if (container) {
            container.innerHTML = `<div class="gallery-status error">画廊加载失败：${escapeHtml(e.message)}</div>`;
        }
        return;
    }
    galleryLoading = false;
    renderGallery();
}

// 孤儿 = 无引用的历史遗留：孤儿项目组的全部文件，或封面池里没被任何点子/任务引用的封面。
// in_use 缺失（服务端引用收集失败的降级态）时不算孤儿——宁可漏标不可误标。
function galleryItemIsOrphan(group, item) {
    if (group.kind === 'project') return group.orphan === true;
    if (item.kind === 'cover') return item.in_use === false;
    return false;
}

function galleryItemMatchesFilter(group, item) {
    switch (galleryFilter) {
        case 'all': return true;
        case 'video': return item.type === 'video'; // 片段 + 合成成片
        case 'orphan': return galleryItemIsOrphan(group, item);
        default: return item.kind === galleryFilter; // cover / frame / studio
    }
}

function gallerySortItems(items) {
    const arr = [...items];
    switch (gallerySort) {
        case 'oldest': arr.sort((a, b) => a.mtime - b.mtime); break;
        case 'size': arr.sort((a, b) => b.size - a.size); break;
        case 'name': arr.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN')); break;
        default: arr.sort((a, b) => b.mtime - a.mtime);
    }
    return arr;
}

function gallerySortGroups(groups) {
    const arr = [...groups];
    switch (gallerySort) {
        case 'oldest':
            arr.sort((a, b) => Math.min(...a.items.map(i => i.mtime)) - Math.min(...b.items.map(i => i.mtime)));
            break;
        case 'size':
            arr.sort((a, b) => b.items.reduce((s, i) => s + i.size, 0) - a.items.reduce((s, i) => s + i.size, 0));
            break;
        case 'name':
            arr.sort((a, b) => a.title.localeCompare(b.title, 'zh-Hans-CN'));
            break;
        default:
            arr.sort((a, b) => Math.max(...b.items.map(i => i.mtime)) - Math.max(...a.items.map(i => i.mtime)));
    }
    return arr;
}

function galleryVisibleGroups() {
    if (!galleryData || !Array.isArray(galleryData.groups)) return [];
    const q = gallerySearch.trim().toLowerCase();
    const out = [];
    for (const g of galleryData.groups) {
        let items = (g.items || []).filter(it => galleryItemMatchesFilter(g, it));
        if (q) {
            const groupHit = (g.title || '').toLowerCase().includes(q);
            items = items.filter(it => groupHit
                || it.name.toLowerCase().includes(q)
                || it.path.toLowerCase().includes(q));
        }
        if (items.length) out.push({ ...g, items: gallerySortItems(items) });
    }
    return gallerySortGroups(out);
}

// 各筛选档的条目数（不含搜索词，让 chip 计数保持稳定的"分类体量"含义）
function galleryFilterCounts() {
    const counts = { all: 0, cover: 0, frame: 0, video: 0, studio: 0, orphan: 0 };
    for (const g of (galleryData && galleryData.groups) || []) {
        for (const it of g.items || []) {
            counts.all++;
            if (it.type === 'video') counts.video++;
            if (it.kind === 'cover') counts.cover++;
            if (it.kind === 'frame') counts.frame++;
            if (it.kind === 'studio') counts.studio++;
            if (galleryItemIsOrphan(g, it)) counts.orphan++;
        }
    }
    return counts;
}

function renderGallery() {
    const container = document.getElementById('gallery-groups');
    if (!container || !galleryData) return;

    const groups = galleryVisibleGroups();
    const totals = galleryData.totals || {};
    const statsEl = document.getElementById('gallery-stats');
    if (statsEl) {
        statsEl.textContent = `${totals.images || 0} 张图片 · ${totals.videos || 0} 个视频 · ${galleryFmtSize(totals.bytes || 0)}`;
    }

    // 筛选 chips 带计数
    const counts = galleryFilterCounts();
    document.querySelectorAll('#gallery-filters .gallery-filter-chip').forEach(chip => {
        if (!chip.dataset.label) chip.dataset.label = chip.textContent.trim();
        const n = counts[chip.dataset.filter];
        chip.textContent = (n !== undefined) ? `${chip.dataset.label} (${n})` : chip.dataset.label;
    });

    if (!groups.length) {
        container.innerHTML = galleryFilter === 'orphan'
            ? '<div class="gallery-status">✨ 没有发现孤儿资产——所有文件都有归属</div>'
            : '<div class="gallery-status">🗂️ 这个分类下暂时没有匹配的本地媒体文件</div>';
        galleryUpdateToolbar();
        return;
    }

    container.innerHTML = groups.map(g => {
        const icon = GALLERY_GROUP_ICONS[g.kind] || '📁';
        const collapsed = galleryCollapsed.has(g.key);
        const expanded = galleryExpanded.has(g.key);
        const shown = collapsed ? [] : (expanded ? g.items : g.items.slice(0, GALLERY_TRUNCATE));
        const gBytes = g.items.reduce((s, it) => s + (it.size || 0), 0);
        const orphanBadge = g.orphan === true ? '<span class="g-orphan-badge" title="未被任何点子/任务引用的历史遗留项目">⚠ 孤儿</span>' : '';

        let gridHtml = '';
        if (!collapsed) {
            const cards = shown.map(it => galleryCardHtml(it)).join('');
            let moreBtn = '';
            if (!expanded && g.items.length > GALLERY_TRUNCATE) {
                moreBtn = `<button type="button" class="gallery-expand-btn" data-expand="1">▼ 展开全部 ${g.items.length} 项</button>`;
            } else if (expanded && g.items.length > GALLERY_TRUNCATE) {
                moreBtn = `<button type="button" class="gallery-expand-btn" data-expand="0">▲ 收起（保留前 ${GALLERY_TRUNCATE} 项）</button>`;
            }
            gridHtml = `<div class="gallery-grid">${cards}</div>${moreBtn}`;
        }

        return `
        <section class="gallery-group${collapsed ? ' collapsed' : ''}${g.orphan === true ? ' orphan' : ''}" data-group="${escapeHtml(g.key)}">
            <div class="gallery-group-head">
                <h3 class="g-group-title" title="点击折叠/展开">
                    <span class="g-collapse-caret">${collapsed ? '▸' : '▾'}</span>
                    <span class="g-group-icon">${icon}</span>${escapeHtml(g.title)}${orphanBadge}
                    <span class="g-count">${g.items.length} 项 · ${galleryFmtSize(gBytes)}</span>
                </h3>
                <div class="gallery-group-actions">
                    <button type="button" class="gallery-tool-btn small g-group-select">全选本组</button>
                    <button type="button" class="gallery-tool-btn small danger g-group-delete">删除本组</button>
                </div>
            </div>
            ${gridHtml}
        </section>`;
    }).join('');

    galleryUpdateToolbar();
}

function galleryCardHtml(it) {
    const sel = gallerySelected.has(it.path);
    const url = galleryEncodeUrl(it.url);
    const badge = GALLERY_KIND_LABELS[it.kind] || '';
    const inUseBadge = (it.kind === 'cover' && it.in_use === true)
        ? '<span class="gallery-inuse-badge" title="正被点子库或任务引用，删除会导致卡片破图">🔗 使用中</span>' : '';
    const thumb = it.type === 'video'
        ? `<video preload="metadata" src="${escapeHtml(url)}#t=0.1" muted playsinline></video>
           <span class="gallery-play-badge">▶</span>`
        : `<img loading="lazy" src="${escapeHtml(url)}" alt="${escapeHtml(it.name)}">`;
    return `
    <div class="gallery-card${sel ? ' selected' : ''}" data-path="${escapeHtml(it.path)}">
        <div class="gallery-thumb">
            ${thumb}
            ${badge ? `<span class="gallery-kind-badge">${badge}</span>` : ''}
            ${inUseBadge}
            <label class="gallery-check" title="选择"><input type="checkbox" class="g-check"${sel ? ' checked' : ''}></label>
            <div class="gallery-card-actions">
                <button type="button" class="g-act g-act-preview" title="放大预览">🔍</button>
                <button type="button" class="g-act g-act-download" title="下载文件">📥</button>
                <button type="button" class="g-act g-act-delete" title="删除本地文件">🗑️</button>
            </div>
        </div>
        <div class="gallery-card-meta">
            <span class="g-name" title="${escapeHtml(it.path)}">${escapeHtml(it.name)}</span>
            <span class="g-sub">${galleryFmtSize(it.size)} · ${galleryFmtTime(it.mtime)}</span>
        </div>
    </div>`;
}

function galleryUpdateToolbar() {
    const delBtn = document.getElementById('gallery-delete-selected-btn');
    if (delBtn) {
        const n = gallerySelected.size;
        delBtn.disabled = n === 0;
        delBtn.textContent = n > 0 ? `🗑️ 删除所选 (${n})` : '🗑️ 删除所选';
    }
    const selAllBtn = document.getElementById('gallery-select-all-btn');
    if (selAllBtn) {
        const visible = galleryVisibleGroups().flatMap(g => g.items.map(it => it.path));
        const allSelected = visible.length > 0 && visible.every(p => gallerySelected.has(p));
        selAllBtn.textContent = allSelected ? '⬜ 取消全选' : '☑️ 全选';
    }
}

function galleryFindItem(path) {
    for (const g of (galleryData && galleryData.groups) || []) {
        const hit = (g.items || []).find(it => it.path === path);
        if (hit) return hit;
    }
    return null;
}

function gallerySetSelected(path, on) {
    if (on) gallerySelected.add(path); else gallerySelected.delete(path);
    const card = document.querySelector(`#gallery-groups .gallery-card[data-path="${CSS.escape(path)}"]`);
    if (card) {
        card.classList.toggle('selected', on);
        const cb = card.querySelector('.g-check');
        if (cb) cb.checked = on;
    }
    galleryUpdateToolbar();
}

function galleryToggleCollapse(key) {
    if (galleryCollapsed.has(key)) galleryCollapsed.delete(key);
    else galleryCollapsed.add(key);
    try {
        localStorage.setItem(GALLERY_COLLAPSED_LS_KEY, JSON.stringify([...galleryCollapsed]));
    } catch (e) { /* 存储满/隐私模式：折叠状态不持久化也能用 */ }
    renderGallery();
}

function galleryOpenPreview(path) {
    // 灯箱在"当前筛选+排序下所在组"内左右翻页（含被截断未显示的卡片）
    const groups = galleryVisibleGroups();
    for (const g of groups) {
        const idx = g.items.findIndex(it => it.path === path);
        if (idx !== -1) {
            const items = g.items.map(it => ({
                type: it.type === 'video' ? 'video' : 'image',
                url: galleryEncodeUrl(it.url),
                caption: `${escapeHtml(g.title)} / ${escapeHtml(it.name)}`,
            }));
            if (typeof openLightbox === 'function') openLightbox(items, idx);
            return;
        }
    }
}

function galleryDownload(it) {
    const a = document.createElement('a');
    a.href = galleryEncodeUrl(it.url);
    a.download = it.name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

async function galleryDeletePaths(paths, label) {
    if (!paths.length) return;
    const inUseCount = paths.filter(p => {
        const it = galleryFindItem(p);
        return it && it.in_use === true;
    }).length;
    let msg = `确定要删除${label}吗？\n共 ${paths.length} 个文件，将从本地磁盘永久删除，不可恢复。`;
    if (inUseCount > 0) {
        msg = `⚠️ 注意：其中 ${inUseCount} 个封面正被点子库或任务引用，删除后对应卡片会破图！\n\n${msg}`;
    }
    const ok = window.confirm(msg);
    if (!ok) return;
    try {
        const res = await fetch('/api/gallery/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status !== 'ok') {
            throw new Error(data.message || `HTTP ${res.status}`);
        }
        const nDel = (data.deleted || []).length;
        const failed = data.failed || [];
        const nDirs = (data.removed_project_dirs || []).length;
        const dirNote = nDirs ? `，并移除 ${nDirs} 个项目文件夹` : '';
        if (failed.length) {
            showToast(`已删除 ${nDel} 个文件${dirNote}，${failed.length} 个失败（如：${failed[0].error}）`, 'error');
        } else {
            showToast(`已删除 ${nDel} 个文件${dirNote}`, 'success');
        }
        paths.forEach(p => gallerySelected.delete(p));
        await refreshGallery();
    } catch (e) {
        showToast(`删除失败：${e.message}`, 'error');
    }
}

function initGallery() {
    const container = document.getElementById('gallery-groups');
    if (!container) return; // console.html 等页面没有画廊

    // 筛选 chips
    const filters = document.getElementById('gallery-filters');
    filters?.addEventListener('click', (e) => {
        const chip = e.target.closest('.gallery-filter-chip');
        if (!chip) return;
        galleryFilter = chip.dataset.filter || 'all';
        filters.querySelectorAll('.gallery-filter-chip').forEach(c => c.classList.toggle('active', c === chip));
        renderGallery();
    });

    // 搜索（去抖 200ms）
    const searchInput = document.getElementById('gallery-search');
    let searchTimer = null;
    searchInput?.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            gallerySearch = searchInput.value || '';
            renderGallery();
        }, 200);
    });

    // 排序
    document.getElementById('gallery-sort')?.addEventListener('change', (e) => {
        gallerySort = e.target.value || 'newest';
        renderGallery();
    });

    document.getElementById('gallery-refresh-btn')?.addEventListener('click', () => {
        galleryData = null; // 强制显示扫描中状态
        refreshGallery();
    });

    document.getElementById('gallery-select-all-btn')?.addEventListener('click', () => {
        const visible = galleryVisibleGroups().flatMap(g => g.items.map(it => it.path));
        const allSelected = visible.length > 0 && visible.every(p => gallerySelected.has(p));
        visible.forEach(p => { if (allSelected) gallerySelected.delete(p); else gallerySelected.add(p); });
        renderGallery();
    });

    document.getElementById('gallery-delete-selected-btn')?.addEventListener('click', () => {
        galleryDeletePaths([...gallerySelected], `所选的 ${gallerySelected.size} 个文件`);
    });

    // 卡片与分组操作：单一事件委托，重渲染后无需重绑
    container.addEventListener('click', (e) => {
        const groupEl = e.target.closest('.gallery-group');

        if (e.target.closest('.g-group-select')) {
            const key = groupEl?.dataset.group;
            const group = galleryVisibleGroups().find(g => g.key === key);
            if (!group) return;
            const paths = group.items.map(it => it.path);
            const allSelected = paths.every(p => gallerySelected.has(p));
            paths.forEach(p => { if (allSelected) gallerySelected.delete(p); else gallerySelected.add(p); });
            renderGallery();
            return;
        }
        if (e.target.closest('.g-group-delete')) {
            const key = groupEl?.dataset.group;
            const group = galleryVisibleGroups().find(g => g.key === key);
            if (!group) return;
            // 无筛选遮挡时删整组 = 项目文件夹连同隐藏残留一起消失（后端按
            // "画廊可见媒体清零"自动 rmtree），确认文案要说清楚
            const raw = (galleryData.groups || []).find(g => g.key === key);
            const isFullProject = group.kind === 'project' && raw && group.items.length === (raw.items || []).length;
            const label = isFullProject
                ? `「${group.title}」全部 ${group.items.length} 个文件（整个项目文件夹连同 manifest、插帧中间产物将一并移除）`
                : `「${group.title}」当前显示的 ${group.items.length} 个文件`;
            galleryDeletePaths(group.items.map(it => it.path), label);
            return;
        }
        // 组标题（含 caret）点击 → 折叠/展开
        if (e.target.closest('.g-group-title')) {
            const key = groupEl?.dataset.group;
            if (key) galleryToggleCollapse(key);
            return;
        }
        // 展开全部 / 收起
        const expandBtn = e.target.closest('.gallery-expand-btn');
        if (expandBtn) {
            const key = groupEl?.dataset.group;
            if (!key) return;
            if (expandBtn.dataset.expand === '1') galleryExpanded.add(key);
            else galleryExpanded.delete(key);
            renderGallery();
            return;
        }

        const card = e.target.closest('.gallery-card');
        if (!card) return;
        const path = card.dataset.path;
        const item = galleryFindItem(path);
        if (!item) return;

        if (e.target.closest('.g-check') || e.target.closest('.gallery-check')) {
            // label 包 checkbox：以 checkbox 最终状态为准（label 点击会自行翻转）
            const cb = card.querySelector('.g-check');
            gallerySetSelected(path, cb ? cb.checked : !gallerySelected.has(path));
            return;
        }
        if (e.target.closest('.g-act-preview')) { galleryOpenPreview(path); return; }
        if (e.target.closest('.g-act-download')) { galleryDownload(item); return; }
        if (e.target.closest('.g-act-delete')) { galleryDeletePaths([path], `文件「${item.name}」`); return; }
        if (e.target.closest('.gallery-thumb')) { galleryOpenPreview(path); return; }
    });
}

// 兼容 defer 加载顺序：DOM 就绪后初始化一次
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initGallery);
} else {
    initGallery();
}
