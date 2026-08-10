/* ==========================================================================
   Gallery (画廊) — 本地历史媒体资产总览与管理。
   数据源是服务端 /api/gallery（实时扫描 outputs/：图像工坊 image-station/、
   各项目的 frames/ + videos/ + 根目录的合成视频与封面 cover_*，以及迁移前的
   历史封面池 covers/——新封面已跟着项目走），并带引用标注：
   封面 item.in_use（被点子库/任务引用）、项目组 group.orphan（无引用且超过
   24h 活跃宽限）。删除走 /api/gallery/delete，会真正移除本地磁盘文件并重同步
   项目 manifest。依赖宿主应用的 escapeHtml / showToast / openLightbox。
   ========================================================================== */

let galleryLoading = false;
let galleryData = null;             // /api/gallery 的原始返回
let galleryFilter = 'all';          // all | cover | frame | video | studio | orphan
let gallerySearch = '';             // 文件名/项目名子串（不区分大小写）
let gallerySort = 'newest';         // newest | oldest | size | name
const gallerySelected = new Set();  // 已勾选 item.path 集合（跨筛选保留）
const galleryExpanded = new Set();  // 本次会话内点过"展开全部"的组 key
const GALLERY_TRUNCATE = 12;        // 每组默认最多显示的卡片数

// 显示方式跨会话记住。三档只改 #gallery-groups 上的 view-* 类，卡片 HTML 与
// 数据完全不动 —— 切视图不该重扫磁盘，也不该丢掉已有的勾选和展开态。
const GALLERY_VIEW_LS_KEY = 'spark_gallery_view';
const GALLERY_VIEWS = ['grid', 'large', 'list'];
let galleryView = (() => {
    try {
        const v = localStorage.getItem(GALLERY_VIEW_LS_KEY);
        return GALLERY_VIEWS.includes(v) ? v : 'grid';
    } catch (e) { return 'grid'; }
})();

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

function galleryApplyView() {
    const container = document.getElementById('gallery-groups');
    if (container) {
        GALLERY_VIEWS.forEach(v => container.classList.toggle(`view-${v}`, v === galleryView));
    }
    document.querySelectorAll('#gallery-view-switch .gallery-view-btn').forEach(btn => {
        const on = btn.dataset.view === galleryView;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
}

function gallerySetView(view) {
    if (!GALLERY_VIEWS.includes(view) || view === galleryView) return;
    galleryView = view;
    try { localStorage.setItem(GALLERY_VIEW_LS_KEY, view); }
    catch (e) { /* 存储满/隐私模式：视图不持久化也能用 */ }
    galleryApplyView();
}

function galleryTabEntered() {
    // 每次切进画廊都重新扫描（不再只扫一次）：磁盘上的帧序列/视频会在其他
    // 标签页里被重试、修复、重渲，画廊若只信"本会话扫过一次"的旧结果，
    // 切回来也看不到这些原地覆盖的新文件。扫描本身是轻量的目录遍历，
    // galleryLoading 仍防止同时并发发起第二次。
    if (!galleryLoading) refreshGallery();
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

// 同名文件被原地覆盖（帧重试/修复等）时，浏览器仍会把旧字节当缓存命中——
// /api/gallery 每次都是新扫描出的磁盘真值 mtime，拿它当版本号做 cache-bust
// 最可靠（不依赖前端别处是否记得给这张图 bump 过版本）。
function galleryVersionedUrl(it) {
    return galleryEncodeUrl(it.url) + '?v=' + (it.mtime || 0);
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
            // 项目名与目录名都要能搜到：改过名的项目两者不一样，用户搜的多半是
            // 现在这一屏上显示的项目名（见渲染处 g.idea_title || g.title）
            const groupHit = (g.title || '').toLowerCase().includes(q)
                || (g.idea_title || '').toLowerCase().includes(q);
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
        // 只有项目组才谈得上"回到激发项目"——封面池与图像工坊不属于任何一单合成。
        // idea_id 是服务端按目录命名反查到的点子库归属（见 gallery_collect_references）；
        // 没反查到也照样给按钮，前端还能按目录名里的 run_<task_id> 落到任务记录上。
        const openProjectBtn = g.kind === 'project'
            ? `<button type="button" class="gallery-tool-btn small g-group-open-project" title="打开这批素材所属的激发项目（提示词/封面/帧与视频）">🎬 打开项目</button>`
            : '';

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

        // 标题显示归属创意的**项目名**，目录名退到后面的小字。
        // 只显示目录名的话，改过名的项目在这里对不上号：磁盘目录未必跟着标题走
        //（改名时有作业在跑就不搬目录，早期改的名压根没搬过），于是画廊上挂着的是
        // 「run_import_xxx_粘贴的文本」这种导入时的临时名，用户认不出那是哪一单。
        // 反过来目录名也不能丢——这一屏管的是磁盘，删除一组删的就是那个文件夹。
        const groupName = g.idea_title || g.title;
        const dirNote = (g.kind === 'project' && g.idea_title && g.idea_title !== g.title)
            ? `<span class="g-group-dir" title="磁盘目录名（删除本组删的就是它）">outputs/${escapeHtml(g.title)}</span>`
            : '';

        return `
        <section class="gallery-group${collapsed ? ' collapsed' : ''}${g.orphan === true ? ' orphan' : ''}" data-group="${escapeHtml(g.key)}">
            <div class="gallery-group-head">
                <h3 class="g-group-title" title="点击折叠/展开">
                    <span class="g-collapse-caret">${collapsed ? '▸' : '▾'}</span>
                    <span class="g-group-icon">${icon}</span>${escapeHtml(groupName)}${orphanBadge}${dirNote}
                    <span class="g-count">${g.items.length} 项 · ${galleryFmtSize(gBytes)}</span>
                </h3>
                <div class="gallery-group-actions">
                    ${openProjectBtn}
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
    const url = galleryVersionedUrl(it);
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
        </div>
        <!-- 操作条是缩略图的兄弟节点而不是它的子节点：网格/大图视图靠负 margin 把它
             压回缩略图底边（视觉与旧版一致），列表视图则用 order 把它甩到行尾成为
             一排常显小按钮 —— 挂在缩略图里面就只能跟着缩略图一起被压成一列。 -->
        <div class="gallery-card-actions">
            <button type="button" class="g-act g-act-preview" title="放大预览">🔍</button>
            <button type="button" class="g-act g-act-download" title="下载文件">📥</button>
            <button type="button" class="g-act g-act-reveal" title="在本机文件管理器中显示">📂</button>
            <button type="button" class="g-act g-act-delete" title="删除本地文件">🗑️</button>
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
    const collapseAllBtn = document.getElementById('gallery-collapse-all-btn');
    if (collapseAllBtn) {
        const groups = galleryVisibleGroups();
        const allCollapsed = groups.length > 0 && groups.every(g => galleryCollapsed.has(g.key));
        collapseAllBtn.textContent = allCollapsed ? '⬇️ 全部展开' : '⬆️ 全部收起';
        collapseAllBtn.disabled = groups.length === 0;
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

function galleryPersistCollapsed() {
    try {
        localStorage.setItem(GALLERY_COLLAPSED_LS_KEY, JSON.stringify([...galleryCollapsed]));
    } catch (e) { /* 存储满/隐私模式：折叠状态不持久化也能用 */ }
}

function galleryToggleCollapse(key) {
    if (galleryCollapsed.has(key)) galleryCollapsed.delete(key);
    else galleryCollapsed.add(key);
    galleryPersistCollapsed();
    renderGallery();
}

// 工具栏"全部收起/展开"：作用于当前筛选下可见的分组（而非磁盘上的全部分组，
// 与"全选"按钮的可见范围保持一致）。只要还有一个可见组是展开的就先全部收起；
// 已经全部收起时再点则整体展开——同一个按钮双态切换，和"全选/取消全选"同款交互。
function galleryToggleCollapseAll() {
    const groups = galleryVisibleGroups();
    if (!groups.length) return;
    const allCollapsed = groups.every(g => galleryCollapsed.has(g.key));
    groups.forEach(g => {
        if (allCollapsed) galleryCollapsed.delete(g.key);
        else galleryCollapsed.add(g.key);
    });
    galleryPersistCollapsed();
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
                url: galleryVersionedUrl(it),
                caption: `${escapeHtml(g.title)} / ${escapeHtml(it.name)}`,
            }));
            if (typeof openLightbox === 'function') openLightbox(items, idx);
            return;
        }
    }
}

// 「🎬 激发项目」：从项目组跳回产出这批素材的那一单激发。服务端已按目录命名
// 反查过点子库（idea_id/idea_title），反查不到时把目录名当 project_key 交给
// openSparkProject，让它按 run_<task_id>_ 前缀落到任务记录上（合成完但没收藏
// 进点子库的项目只有任务记录）。
async function galleryOpenSparkProject(group) {
    if (typeof openSparkProject !== 'function') {
        showToast('激发结果工作区尚未加载完成，请稍后重试', 'error');
        return;
    }
    await openSparkProject({
        ideaId: group.idea_id || null,
        title: group.idea_title || group.title || '',
        projectKey: group.key || '',
        label: group.idea_title || group.title || '该项目',
    });
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

    galleryApplyView();
    document.getElementById('gallery-view-switch')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.gallery-view-btn');
        if (btn) gallerySetView(btn.dataset.view);
    });

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

    document.getElementById('gallery-collapse-all-btn')?.addEventListener('click', galleryToggleCollapseAll);

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

        if (e.target.closest('.g-group-open-project')) {
            const key = groupEl?.dataset.group;
            const group = galleryVisibleGroups().find(g => g.key === key);
            if (group) galleryOpenSparkProject(group);
            return;
        }
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
            // 删除要删的是目录，所以确认文案里项目名与目录名都得报出来——改过名的
            // 项目两者不一样，只报一个都可能让人删错东西
            const named = group.idea_title && group.idea_title !== group.title
                ? `${group.idea_title}（目录 outputs/${group.title}）` : group.title;
            const label = isFullProject
                ? `「${named}」全部 ${group.items.length} 个文件（整个项目文件夹连同 manifest、插帧中间产物将一并移除）`
                : `「${named}」当前显示的 ${group.items.length} 个文件`;
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
        // 定位到本机文件：item.path 就是 outputs/ 下的相对路径（服务端扫描出来的真值）
        if (e.target.closest('.g-act-reveal')) { revealLocalFile(item.path, item.name); return; }
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
