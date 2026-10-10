/* ==========================================================================
   Gallery (画廊) — 本地历史媒体资产总览与管理。
   首屏读 /api/gallery/index，展开分组时分页读 /api/gallery/items。
   兼容完整 /api/gallery 数据（实时扫描 outputs/：图像工坊 image-station/、
   各项目的 frames/ + videos/ + 根目录的合成视频与封面 cover_*，以及迁移前的
   历史封面池 covers/——新封面已跟着项目走），并带引用标注：
   封面 item.in_use（被点子库/任务引用）、项目组 group.orphan（无引用且超过
   24h 活跃宽限）。删除走 /api/gallery/delete，会真正移除本地磁盘文件并重同步
   项目 manifest。依赖宿主应用的 escapeHtml / showToast / openLightbox。
   ========================================================================== */

let galleryLoading = false;
let galleryData = null;             // 分组索引及已加载页；兼容旧完整列表
let galleryFilter = 'all';          // all | cover | frame | video (片段) | merged (成片) | studio | orphan
let gallerySearch = '';             // 文件名/项目名子串（不区分大小写）
let gallerySort = 'newest';         // newest | oldest | size | name
const gallerySelected = new Set();  // 已勾选 item.path，仅保留在当前筛选范围内的选择
let galleryDownloading = false;
const galleryExpanded = new Set();  // 本次会话内点过"展开全部"的组 key
const GALLERY_TRUNCATE = 12;        // 每组默认最多显示的卡片数
const gallerySelectedItems = new Map(); // 跨页勾选的元数据，不能用已加载页裁剪选择
const galleryPageRequests = new Map();
let galleryRequestRevision = 0;
let galleryBulkLoading = false;
const galleryOpened = new Set((() => {
    try { return JSON.parse(localStorage.getItem('spark_gallery_opened') || '[]'); }
    catch (_) { return []; }
})());

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
    // 每次进入重取轻量计数。文件清单只在明确展开分组后加载。
    refreshGallery();
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

// 扫描进行中的 Promise：其它模块（项目工作台、图像工坊）跳进画廊时要等它跑完再定位，
// 之前靠 setTimeout(600) 猜扫描耗时，扫描慢一点就定位落空。
let galleryRefreshPromise = null;
let galleryRefreshQuery = '';

function galleryQueryKey() {
    return JSON.stringify([galleryFilter, gallerySearch.trim().toLowerCase(), gallerySort]);
}

function galleryQueryParams() {
    return new URLSearchParams({ filter: galleryFilter, q: gallerySearch.trim(), sort: gallerySort });
}

function refreshGallery(options = {}) {
    const key = galleryQueryKey();
    if (galleryLoading && galleryRefreshQuery === key && !options.force) return galleryRefreshPromise;
    const revision = ++galleryRequestRevision;
    galleryRefreshQuery = key;
    const promise = galleryDoRefresh(options, revision, key).finally(() => {
        if (galleryRefreshPromise === promise) galleryRefreshPromise = null;
        if (revision === galleryRequestRevision) galleryLoading = false;
    });
    galleryRefreshPromise = promise;
    return promise;
}

async function galleryDoRefresh(options, revision, key) {
    galleryLoading = true;
    const container = document.getElementById('gallery-groups');
    if (container && !galleryData) {
        container.innerHTML = '<div class="gallery-status">📡 正在汇总媒体计数…</div>';
    }
    try {
        const params = galleryQueryParams();
        if (options.force) params.set('refresh', '1');
        const res = await fetch(`/api/gallery/index?${params}`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && data.error) throw new Error(data.error);
        if (revision !== galleryRequestRevision || key !== galleryQueryKey()) return false;
        const previous = new Map((galleryData?.groups || []).map(group => [group.key, group]));
        const reusePages = galleryData?.lazy && galleryData.queryKey === key && galleryData.revision === data.revision;
        galleryData = { ...data, lazy: true, queryKey: key, groups: (data.groups || []).map(group => ({
            ...group, items: reusePages ? previous.get(group.key)?.items || [] : [],
            visibleCount: previous.get(group.key)?.visibleCount || GALLERY_TRUNCATE,
            loading: false, error: '',
        })) };
        galleryData.groups.forEach(group => {
            if (galleryOpened.has(group.key)) galleryCollapsed.delete(group.key);
            else galleryCollapsed.add(group.key);
            gallerySelectedItems.forEach(record => {
                if (record.group.key === group.key) record.group = { key: group.key, kind: group.kind,
                    title: group.title, idea_title: group.idea_title, orphan: group.orphan };
            });
        });
        galleryPruneSelection();
    } catch (e) {
        if (revision !== galleryRequestRevision || key !== galleryQueryKey()) return false;
        galleryLoading = false;
        if (container) {
            container.innerHTML = `<div class="gallery-status error">画廊加载失败：${escapeHtml(e.message)}</div>`;
        }
        return false;
    }
    if (revision !== galleryRequestRevision || key !== galleryQueryKey()) return false;
    galleryLoading = false;
    renderGallery();
    if (!options.suppressAutoLoad) {
        galleryData.groups.filter(group => galleryOpened.has(group.key)).forEach(group => {
            galleryLoadGroup(group.key).catch(() => {});
        });
    }
    return true;
}

function galleryScopeError(message, code) {
    const error = new Error(message);
    error.code = code;
    return error;
}

async function galleryFetchGroup(key, { more = false, complete = false } = {}) {
    if (!galleryData?.lazy) return (galleryVisibleGroups().find(group => group.key === key)?.items || []);
    const group = galleryData.groups.find(item => item.key === key);
    if (!group) return [];
    const query = galleryQueryKey(), epoch = galleryRequestRevision, snapshot = galleryData.revision;
    const target = complete ? group.filtered_count
        : Math.min(group.filtered_count, (group.visibleCount || GALLERY_TRUNCATE) + (more ? GALLERY_TRUNCATE : 0));
    const pendingKey = `${epoch}:${key}`;
    if (galleryPageRequests.has(pendingKey)) {
        await galleryPageRequests.get(pendingKey);
        if (epoch !== galleryRequestRevision || query !== galleryQueryKey()) throw galleryScopeError('筛选已改变，请重新操作', 'query_changed');
        return galleryFetchGroup(key, { more, complete });
    }
    if (group.items.length >= target) {
        if (!complete) group.visibleCount = target;
        renderGallery();
        return group.items;
    }
    group.loading = true;
    group.error = '';
    renderGallery();
    const promise = (async () => {
        let items = [...group.items];
        while (items.length < target) {
            const params = galleryQueryParams();
            params.set('group', key);
            params.set('offset', String(items.length));
            params.set('limit', String(complete ? 200 : GALLERY_TRUNCATE));
            params.set('revision', snapshot);
            const response = await fetch(`/api/gallery/items?${params}`);
            const data = await response.json();
            if (epoch !== galleryRequestRevision || query !== galleryQueryKey()) throw galleryScopeError('筛选已改变，请重新操作', 'query_changed');
            if (response.status === 409 || data.error === 'snapshot_changed'
                    || (response.ok && data.revision !== snapshot)) {
                throw galleryScopeError('媒体清单已更新，请重新加载后再操作', 'snapshot_changed');
            }
            if (!response.ok || data.error) throw new Error(data.message || data.error || `HTTP ${response.status}`);
            if (data.group_key !== key || data.filtered_count !== group.filtered_count || data.offset !== items.length
                    || !Array.isArray(data.items) || !data.items.length) {
                throw galleryScopeError('媒体清单发生变化，请重新加载后再操作', 'snapshot_changed');
            }
            items = items.concat(data.items);
        }
        if (epoch !== galleryRequestRevision || query !== galleryQueryKey()) throw galleryScopeError('筛选已改变，请重新操作', 'query_changed');
        group.items = items;
        if (!complete) group.visibleCount = target;
        items.forEach(item => {
            if (gallerySelected.has(item.path)) galleryRememberSelection(item, group);
        });
        if (items.length >= group.filtered_count) {
            const alive = new Set(items.map(item => item.path));
            gallerySelectedItems.forEach((record, path) => {
                if (record.group.key === key && !alive.has(path)) { gallerySelected.delete(path); gallerySelectedItems.delete(path); }
            });
        }
        return items;
    })();
    galleryPageRequests.set(pendingKey, promise);
    try { return await promise; }
    finally {
        galleryPageRequests.delete(pendingKey);
        group.loading = false;
        if (epoch === galleryRequestRevision && query === galleryQueryKey()) renderGallery();
    }
}

async function galleryLoadGroup(key, options = {}) {
    const query = galleryQueryKey();
    for (let attempt = 0; attempt < 2; attempt++) {
        try { return await galleryFetchGroup(key, options); }
        catch (error) {
            if (error.code === 'query_changed' || query !== galleryQueryKey()) return [];
            if (error.code === 'snapshot_changed' && attempt === 0) {
                await refreshGallery({ force: true, suppressAutoLoad: true });
                if (query !== galleryQueryKey()) return [];
                continue;
            }
            const group = galleryData?.groups.find(item => item.key === key);
            if (group) group.error = error.message;
            renderGallery();
            showToast(`加载媒体失败：${error.message}`, 'error');
            return [];
        }
    }
}

// 批量操作及整组灯箱是用户明确请求的范围；先取齐全部元数据再行动。
// 列表翻页和折叠不会改变该范围，更不能把「删除本组」缩成已显示的 12 项。
async function galleryCollectScope(keys = null) {
    if (!galleryData?.lazy) return galleryVisibleGroups().filter(group => !keys || keys.includes(group.key));
    const query = galleryQueryKey();
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const refreshed = await refreshGallery({ force: attempt > 0, suppressAutoLoad: true });
            if (!refreshed || query !== galleryQueryKey()) throw galleryScopeError('筛选已改变，请重新操作', 'query_changed');
            const epoch = galleryRequestRevision, snapshot = galleryData.revision;
            const groups = galleryData.groups.filter(group => !keys || keys.includes(group.key));
            const checkScope = () => {
                if (query !== galleryQueryKey()) throw galleryScopeError('筛选已改变，请重新操作', 'query_changed');
                if (epoch !== galleryRequestRevision || snapshot !== galleryData.revision) {
                    throw galleryScopeError('画廊已刷新，请重新操作', 'query_changed');
                }
            };
            for (const group of groups) { checkScope(); await galleryFetchGroup(group.key, { complete: true }); checkScope(); }
            checkScope();
            return groups;
        } catch (error) {
            if (error.code === 'snapshot_changed' && attempt === 0 && query === galleryQueryKey()) continue;
            throw error;
        }
    }
}

function galleryRememberSelection(item, group) {
    gallerySelectedItems.set(item.path, { item, group: { key: group.key, kind: group.kind,
        title: group.title, idea_title: group.idea_title, orphan: group.orphan } });
}

function gallerySetFilter(filter) {
    galleryFilter = filter || 'all';
    document.querySelectorAll('#gallery-filters .gallery-filter-chip').forEach(c => {
        c.classList.toggle('active', (c.dataset.filter || 'all') === galleryFilter);
    });
}

// 直接写状态并同步输入框，绕开输入框 200ms 去抖——程序化跳转要立刻生效。
function gallerySetSearch(q) {
    gallerySearch = q || '';
    const input = document.getElementById('gallery-search');
    if (input) input.value = gallerySearch;
}

/**
 * 其它模块跳进画廊的统一入口（项目工作台「查看文件/在画廊查找」、图像工坊「在画廊查看」）。
 *   groupKey  定位到某个分组（目录名）：展开它、滚到视野、闪一下高亮
 *   search    预填搜索词
 *   filter    预设筛选档（all | cover | frame | video | merged | studio | orphan）
 * 先重置上次遗留的筛选/搜索再跳转——否则目标分组可能正好被旧筛选藏起来；
 * 并且等扫描真的完成才定位，而不是猜一个超时。返回是否定位成功。
 */
async function galleryFocus({ groupKey = '', search = '', filter = 'all' } = {}) {
    gallerySetFilter(filter);
    gallerySetSearch(search);
    switchMainTab('gallery');            // 进入画廊会触发一次重新扫描
    await refreshGallery();
    if (!galleryData) return false;      // 扫描失败，面板里已有错误提示
    if (groupKey) {
        galleryOpened.add(groupKey);
        galleryCollapsed.delete(groupKey);
        galleryPersistCollapsed();
        if (galleryData.lazy) await galleryLoadGroup(groupKey);
    }
    renderGallery();
    if (!groupKey) return true;
    const el = document.querySelector(`#gallery-groups .gallery-group[data-group="${CSS.escape(groupKey)}"]`);
    if (!el) {
        showToast('画廊里没找到这个项目的资产目录（可能已被清理）', 'info');
        return false;
    }
    galleryScrollToGroup(el);
    el.classList.add('flash-highlight');
    setTimeout(() => el.classList.remove('flash-highlight'), 1600);
    return true;
}

// 卡片里的 <video preload="metadata"> 与懒加载图片会在滚动之后才撑开高度，把平滑滚动的
// 目标挤出视野。所以先瞬时跳到位，再在布局稳定的前 1.5 秒内几次校正。
function galleryScrollToGroup(el) {
    const scroller = document.getElementById('gallery-groups');
    const align = () => {
        if (!el.isConnected || !scroller) return;
        const drift = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
        if (Math.abs(drift) > 24) scroller.scrollTop += drift;
    };
    align();
    [150, 400, 800, 1500].forEach(ms => setTimeout(align, ms));
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
        case 'video': return item.kind === 'video'; // 视频片段与成片分开
        case 'merged': return item.kind === 'merged';
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
    if (galleryData.lazy) return galleryData.queryKey === galleryQueryKey() ? galleryData.groups : [];
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
    if (galleryData?.lazy) return galleryData.filter_counts || {};
    const counts = { all: 0, cover: 0, frame: 0, video: 0, merged: 0, studio: 0, orphan: 0 };
    for (const g of (galleryData && galleryData.groups) || []) {
        for (const it of g.items || []) {
            counts.all++;
            if (it.kind === 'video') counts.video++;
            if (it.kind === 'merged') counts.merged++;
            if (it.kind === 'cover') counts.cover++;
            if (it.kind === 'frame') counts.frame++;
            if (it.kind === 'studio') counts.studio++;
            if (galleryItemIsOrphan(g, it)) counts.orphan++;
        }
    }
    return counts;
}

function gallerySelectedPaths() {
    if (galleryData?.lazy) {
        const visible = galleryData.queryKey === galleryQueryKey()
            ? new Set(galleryData.groups.map(group => group.key)) : null;
        const query = gallerySearch.trim().toLowerCase();
        return [...gallerySelected].filter(path => {
            const record = gallerySelectedItems.get(path);
            if (!record || (visible && !visible.has(record.group.key))
                    || !galleryItemMatchesFilter(record.group, record.item)) return false;
            return !query || [record.group.title, record.group.idea_title, record.item.name, record.item.path]
                .some(value => String(value || '').toLowerCase().includes(query));
        });
    }
    const allowed = new Set(galleryVisibleGroups().flatMap(group => group.items.map(item => item.path)));
    return [...gallerySelected].filter(path => allowed.has(path));
}

function galleryPruneSelection() {
    const selected = new Set(gallerySelectedPaths());
    [...gallerySelected].forEach(path => {
        if (!selected.has(path)) { gallerySelected.delete(path); gallerySelectedItems.delete(path); }
    });
}


function renderGallery() {
    const container = document.getElementById('gallery-groups');
    if (!container || !galleryData) return;
    const scrollTop = container.scrollTop;

    const groups = galleryVisibleGroups();
    galleryPruneSelection();
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
        const shown = collapsed ? [] : (galleryData.lazy ? g.items.slice(0, g.visibleCount || GALLERY_TRUNCATE)
            : (expanded ? g.items : g.items.slice(0, GALLERY_TRUNCATE)));
        const matchCount = galleryData.lazy ? g.filtered_count : g.items.length;
        const gBytes = galleryData.lazy ? g.bytes : g.items.reduce((s, it) => s + (it.size || 0), 0);
        const orphanBadge = g.orphan === true ? '<span class="g-orphan-badge" title="未被任何点子/任务引用的历史遗留项目">⚠ 孤儿</span>' : '';
        // 只有项目组才谈得上"回到激发项目"——封面池与图像工坊不属于任何一单合成。
        // idea_id 是服务端按目录命名反查到的点子库归属（见 gallery_collect_references）；
        // 没反查到也照样给按钮，前端还能按目录名里的 run_<task_id> 落到任务记录上。
        const openProjectBtn = g.kind === 'project'
            ? `<button type="button" class="gallery-tool-btn small g-group-open-project" title="打开这批素材所属的激发项目（提示词/封面/帧与视频）"><span class="g-btn-ico">🎬</span><span class="g-btn-label">打开项目</span></button>`
            : '';
        const locateProjectBtn = g.kind === 'project'
            ? `<button type="button" class="gallery-tool-btn small g-group-locate-project" title="在项目页里定位这个项目（状态、归档、任务记录）"><span class="g-btn-ico">📁</span><span class="g-btn-label">项目页</span></button>`
            : '';

        let gridHtml = '';
        if (!collapsed) {
            const cards = shown.map(it => galleryCardHtml(it)).join('');
            let moreBtn = '';
            if (galleryData.lazy) {
                if (g.error) moreBtn = `<div class="gallery-status error">${escapeHtml(g.error)} <button type="button" class="gallery-expand-btn" data-expand="retry">重新加载</button></div>`;
                else if (g.loading) moreBtn = '<div class="gallery-status">正在加载媒体清单…</div>';
                else if (shown.length < matchCount) {
                    moreBtn = `<button type="button" class="gallery-expand-btn" data-expand="more">加载更多（已显示 ${shown.length} / ${matchCount} 项）</button>`;
                }
            } else if (!expanded && g.items.length > GALLERY_TRUNCATE) {
                moreBtn = `<button type="button" class="gallery-expand-btn" data-expand="1">▼ 展开全部 ${g.items.length} 项</button>`;
            } else if (expanded && g.items.length > GALLERY_TRUNCATE) {
                moreBtn = `<button type="button" class="gallery-expand-btn" data-expand="0">▲ 收起（保留前 ${GALLERY_TRUNCATE} 项）</button>`;
            }
            gridHtml = `<div class="gallery-grid">${cards}</div>${moreBtn}`;
        }

        // 项目名称作为标题；磁盘路径收进可展开详情。
        // 改过名的项目可能仍沿用旧目录，路径可在详情里核对。
        const groupName = g.idea_title || g.title;
        const totalCount = galleryData.lazy ? g.total_count
            : ((galleryData.groups || []).find(raw => raw.key === g.key)?.items || []).length;
        const filtered = totalCount !== matchCount;
        const groupDetails = g.kind === 'project'
            ? `<details class="gallery-group-details"><summary>项目详情</summary><div>本地目录 <code>outputs/${escapeHtml(g.title)}</code></div></details>`
            : '';
        const deleteLabel = filtered ? '删除筛选结果' : '删除本组媒体';


        return `
        <section class="gallery-group${collapsed ? ' collapsed' : ''}${g.orphan === true ? ' orphan' : ''}" data-group="${escapeHtml(g.key)}">
            <div class="gallery-group-head">
                <h3 class="g-group-title" title="点击折叠/展开${g.kind === 'project' ? `\n本地目录 outputs/${escapeHtml(g.title)}` : ''}">
                    <span class="g-collapse-caret">${collapsed ? '▸' : '▾'}</span>
                    <span class="g-group-icon">${icon}</span><span class="g-group-name">${escapeHtml(groupName)}</span>${orphanBadge}
                    <span class="g-count">${filtered ? `筛选 ${matchCount} / ${totalCount}` : matchCount} 项 · ${galleryFmtSize(gBytes)}</span>
                </h3>
                <div class="gallery-group-actions">
                    ${openProjectBtn}
                    ${locateProjectBtn}
                    <button type="button" class="gallery-tool-btn small g-group-select" title="${filtered ? '选择本组当前筛选结果' : '选择本组全部媒体'}"><span class="g-btn-ico">☑</span><span class="g-btn-label">选择${filtered ? '筛选结果' : '本组媒体'}</span></button>
                    <button type="button" class="gallery-tool-btn small danger g-group-delete" title="仅删除本组当前筛选中的 ${matchCount} 个媒体文件"><span class="g-btn-ico">🗑️</span><span class="g-btn-label">${deleteLabel}</span></button>
                </div>
            </div>
            ${groupDetails}
            ${gridHtml}
        </section>`;
    }).join('');

    if (Number.isFinite(scrollTop)) container.scrollTop = scrollTop;
    galleryUpdateToolbar();
}

function galleryCardHtml(it) {
    const sel = gallerySelected.has(it.path);
    const url = galleryVersionedUrl(it);
    const badge = it.is_edited ? '精剪成片' : (GALLERY_KIND_LABELS[it.kind] || '');
    const inUseBadge = (it.kind === 'cover' && it.in_use === true)
        ? '<span class="gallery-inuse-badge" title="正被点子库或任务引用，删除会导致卡片破图">🔗 使用中</span>' : '';
    const previewUrl = it.type === 'video' ? `${url}#t=0.1` : url;
    const sourceAttrs = typeof MediaPreview !== 'undefined'
        ? MediaPreview.attrs(previewUrl) : `src="${escapeHtml(previewUrl)}"`;
    const thumb = it.type === 'video'
        ? `<video preload="metadata" ${sourceAttrs} muted playsinline></video>
           <span class="gallery-play-badge">▶</span>`
        : `<img loading="lazy" ${sourceAttrs} alt="${escapeHtml(it.name)}">`;
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
            ${it.type === 'video' ? '' : '<button type="button" class="g-act g-act-i2i" title="送去图像工坊·图生图，作为参考图">🎨</button>'}
            <button type="button" class="g-act g-act-download" title="下载文件">📥</button>
            <button type="button" class="g-act g-act-reveal" title="在本机文件管理器中显示">📂</button>
            <button type="button" class="g-act g-act-delete" title="删除本地文件">🗑️</button>
        </div>
        <!-- 正文：网格视图是「文件名 | 大小」一行；列表视图把 g-sub 的三个 span 拆成表格列；
             完整信息（时间/路径）在 title 悬停提示里，大图视图才展开「详情」。 -->
        <div class="gallery-card-meta" title="${escapeHtml(`${it.name}\n${galleryFmtSize(it.size)} · ${galleryFmtTime(it.mtime)}\n${it.path}`)}">
            <span class="g-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
            <span class="g-sub"><span class="g-kind">${escapeHtml(badge || '图片')}</span><span class="g-size">${galleryFmtSize(it.size)}</span><span class="g-time">${galleryFmtTime(it.mtime)}</span></span>
            <details class="gallery-file-details"><summary>详情</summary><dl>
                <dt>更新时间</dt><dd>${galleryFmtTime(it.mtime)}</dd>
                <dt>本地路径</dt><dd><code>${escapeHtml(it.path)}</code></dd>
            </dl></details>
        </div>
    </div>`;
}

function galleryUpdateToolbar() {
    const selected = gallerySelectedPaths();
    const downloadBtn = document.getElementById('gallery-download-selected-btn');
    if (downloadBtn) {
        downloadBtn.disabled = !selected.length || galleryDownloading;
        downloadBtn.textContent = galleryDownloading ? '正在打包…' : `下载所选${selected.length ? ` (${selected.length})` : ''}`;
    }
    // 选择操作条只在有选中项时出现（没选东西时不占位、不摆一排灰按钮）
    const selectionBar = document.getElementById('gallery-selection-bar');
    if (selectionBar) selectionBar.hidden = selected.length === 0;
    const selectionNote = document.getElementById('gallery-selection-note');
    if (selectionNote) selectionNote.textContent = selected.length
        ? `已选 ${selected.length} 项 · 下载和删除仅作用于当前筛选中的所选媒体`
        : '选择仅在当前筛选内保留';
    const delBtn = document.getElementById('gallery-delete-selected-btn');
    if (delBtn) {
        const n = selected.length;
        delBtn.disabled = n === 0;
        delBtn.textContent = n > 0 ? `🗑️ 删除所选 (${n})` : '🗑️ 删除所选';
    }
    const selAllBtn = document.getElementById('gallery-select-all-btn');
    if (selAllBtn) {
        const visible = galleryVisibleGroups().flatMap(g => g.items.map(it => it.path));
        const total = galleryData?.lazy ? galleryVisibleGroups().reduce((sum, group) => sum + group.filtered_count, 0) : visible.length;
        const allSelected = galleryData?.lazy ? total > 0 && selected.length === total
            : visible.length > 0 && visible.every(p => gallerySelected.has(p));
        selAllBtn.textContent = galleryBulkLoading ? '正在准备完整清单…' : allSelected ? '取消本筛选全选' : '全选筛选结果';
        selAllBtn.disabled = total === 0 || galleryBulkLoading;
    }
    const collapseAllBtn = document.getElementById('gallery-collapse-all-btn');
    if (collapseAllBtn) {
        const groups = galleryVisibleGroups();
        const allCollapsed = groups.length > 0 && groups.every(g => galleryCollapsed.has(g.key));
        collapseAllBtn.textContent = allCollapsed ? '⬇️ 全部展开' : '⬆️ 全部收起';
        collapseAllBtn.disabled = groups.length === 0;
    }
    if (galleryBulkLoading) {
        if (downloadBtn) downloadBtn.disabled = true;
        if (delBtn) delBtn.disabled = true;
    }
    document.querySelectorAll('#gallery-groups .g-group-select, #gallery-groups .g-group-delete')
        .forEach(button => { button.disabled = galleryBulkLoading; });
}

function galleryFindItem(path) {
    for (const g of (galleryData && galleryData.groups) || []) {
        const hit = (g.items || []).find(it => it.path === path);
        if (hit) return hit;
    }
    return gallerySelectedItems.get(path)?.item || null;
}

function gallerySetSelected(path, on) {
    if (on) {
        gallerySelected.add(path);
        const group = galleryData?.groups.find(item => item.items.some(media => media.path === path));
        const item = group?.items.find(media => media.path === path);
        if (item) galleryRememberSelection(item, group);
    } else { gallerySelected.delete(path); gallerySelectedItems.delete(path); }
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
        localStorage.setItem('spark_gallery_opened', JSON.stringify([...galleryOpened]));
    } catch (e) { /* 存储满/隐私模式：折叠状态不持久化也能用 */ }
}

function galleryToggleCollapse(key) {
    if (galleryCollapsed.has(key)) { galleryCollapsed.delete(key); galleryOpened.add(key); }
    else { galleryCollapsed.add(key); galleryOpened.delete(key); }
    galleryPersistCollapsed();
    renderGallery();
    if (galleryData?.lazy && !galleryCollapsed.has(key)) return galleryLoadGroup(key);
}

// 工具栏"全部收起/展开"：作用于当前筛选下可见的分组（而非磁盘上的全部分组，
// 与"全选"按钮的可见范围保持一致）。只要还有一个可见组是展开的就先全部收起；
// 已经全部收起时再点则整体展开——同一个按钮双态切换，和"全选/取消全选"同款交互。
function galleryToggleCollapseAll() {
    const groups = galleryVisibleGroups();
    if (!groups.length) return;
    const allCollapsed = groups.every(g => galleryCollapsed.has(g.key));
    groups.forEach(g => {
        if (allCollapsed) { galleryCollapsed.delete(g.key); galleryOpened.add(g.key); }
        else { galleryCollapsed.add(g.key); galleryOpened.delete(g.key); }
    });
    galleryPersistCollapsed();
    renderGallery();
    if (galleryData?.lazy && allCollapsed) groups.forEach(group => galleryLoadGroup(group.key));
}

async function galleryOpenPreview(path) {
    // 灯箱在"当前筛选+排序下所在组"内左右翻页（含被截断未显示的卡片）
    let groups = galleryVisibleGroups();
    if (galleryData?.lazy) {
        const key = groups.find(group => group.items.some(item => item.path === path))?.key;
        if (!key) return;
        try { groups = await galleryCollectScope([key]); }
        catch (error) { showToast(`预览未打开：${error.message}`, 'error'); return; }
    }
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

// 「🎨」：把画廊里的图送进图像工坊·图生图当参考图（与渲染室的「送去图生图」同一条通路）。
// 会替换图生图里已有的参考图——和渲染室那个按钮行为一致。
function galleryUseAsReference(it) {
    if (typeof imgStudioSendToImageToImage !== 'function') {
        showToast('图像工坊尚未加载完成，请稍后重试', 'error');
        return;
    }
    switchMainTab('image');
    imgStudioSendToImageToImage(galleryVersionedUrl(it), `ref_${it.name || 'gallery.png'}`);
}

// 「📁 项目页」：从项目组回到项目工作台，选中并展开对应项目。
function galleryLocateInProjects(group) {
    if (typeof projectsLocate !== 'function') {
        showToast('项目工作台尚未加载完成，请稍后重试', 'error');
        return;
    }
    projectsLocate({ projectKey: group.key || '', title: group.idea_title || group.title || '' });
}

function galleryDownload(it) {
    const a = document.createElement('a');
    a.href = galleryEncodeUrl(it.url);
    a.download = it.name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

async function galleryWithBulkAction(action) {
    if (galleryBulkLoading) return;
    galleryBulkLoading = true;
    galleryUpdateToolbar();
    try { return await action(); }
    catch (error) { showToast(`操作未执行：${error.message}`, error.code === 'query_changed' ? 'info' : 'error'); }
    finally { galleryBulkLoading = false; renderGallery(); }
}

async function galleryToggleScopeSelection(keys = null) {
    if (!galleryData?.lazy) {
        const groups = galleryVisibleGroups().filter(group => !keys || keys.includes(group.key));
        const paths = groups.flatMap(group => group.items.map(item => item.path));
        const allSelected = paths.length > 0 && paths.every(path => gallerySelected.has(path));
        paths.forEach(path => { if (allSelected) gallerySelected.delete(path); else gallerySelected.add(path); });
        renderGallery();
        return;
    }
    return galleryWithBulkAction(async () => {
        const groups = await galleryCollectScope(keys);
        const paths = groups.flatMap(group => group.items.map(item => item.path));
        const allSelected = paths.length > 0 && paths.every(path => gallerySelected.has(path));
        groups.forEach(group => group.items.forEach(item => {
            if (allSelected) { gallerySelected.delete(item.path); gallerySelectedItems.delete(item.path); }
            else { gallerySelected.add(item.path); galleryRememberSelection(item, group); }
        }));
    });
}

async function galleryResolvePaths(paths) {
    if (!galleryData?.lazy) return paths;
    const keys = [...new Set(paths.map(path => gallerySelectedItems.get(path)?.group.key
        || galleryData.groups.find(group => group.items.some(item => item.path === path))?.key).filter(Boolean))];
    if (!keys.length) return [];
    const groups = await galleryCollectScope(keys);
    const alive = new Set(groups.flatMap(group => group.items.map(item => item.path)));
    return paths.filter(path => alive.has(path));
}

function galleryDeleteGroup(key) {
    if (!galleryData?.lazy) {
        const group = galleryVisibleGroups().find(item => item.key === key);
        if (group) return galleryDeletePaths(group.items.map(item => item.path),
            `「${group.idea_title || group.title}」当前筛选中的 ${group.items.length} 个媒体文件`);
        return;
    }
    return galleryWithBulkAction(async () => {
        const group = (await galleryCollectScope([key]))[0];
        if (!group) return;
        await galleryDeletePaths(group.items.map(item => item.path),
            `「${group.idea_title || group.title}」当前筛选中的 ${group.items.length} 个媒体文件`, { validated: true });
    });
}

async function galleryDownloadSelected(options = {}) {
    if (galleryDownloading) return;
    if (galleryData?.lazy && !options.validated) {
        const requested = gallerySelectedPaths();
        return galleryWithBulkAction(async () => {
            const paths = await galleryResolvePaths(requested);
            if (paths.length) await galleryDownloadSelected({ validated: true, paths });
        });
    }
    const paths = options.paths || gallerySelectedPaths();
    if (!paths.length) return;
    if (paths.length === 1) { galleryDownload(galleryFindItem(paths[0])); return; }
    galleryDownloading = true;
    galleryUpdateToolbar();
    try {
        const response = await fetch('/api/gallery/download-zip', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || typeof data.url !== 'string' || !data.url.startsWith('/api/gallery/download-zip/')) {
            throw new Error(data.message || (response.ok ? '下载链接无效，请重试' : `HTTP ${response.status}`));
        }
        const link = document.createElement('a');
        link.href = data.url;
        link.download = data.filename || '画廊所选素材.zip';
        document.body.appendChild(link);
        link.click();
        link.remove();
        showToast(`已准备 ${data.count || paths.length} 个文件的打包下载`, 'success');

    } catch (error) {
        showToast(`下载失败：${error.message}`, 'error');
    } finally {
        galleryDownloading = false;
        galleryUpdateToolbar();
    }
}


async function galleryDeletePaths(paths, label, options = {}) {
    if (!paths.length) return;
    if (galleryData?.lazy && !options.validated) {
        return galleryWithBulkAction(async () => {
            const current = await galleryResolvePaths(paths);
            if (current.length) await galleryDeletePaths(current, label, { validated: true });
        });
    }
    const inUseCount = paths.filter(p => {
        const it = galleryFindItem(p);
        return it && it.in_use === true;
    }).length;
    let msg = `确定删除${label}吗？\n删除范围：${paths.length} 个媒体文件，将从本地磁盘永久删除。项目记录与此范围以外的文件保留。`;
    if (inUseCount > 0) {
        msg = `⚠️ 注意：其中 ${inUseCount} 个封面正被点子库或任务引用，删除后对应卡片会破图！\n\n${msg}`;
    }
    const ok = window.confirm(msg);
    if (!ok) return;
    try {
        const res = await fetch('/api/gallery/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths, remove_empty_projects: false }),
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
        paths.forEach(p => { gallerySelected.delete(p); gallerySelectedItems.delete(p); });
        await refreshGallery({ force: true });
    } catch (e) {
        showToast(`删除失败：${e.message}`, 'error');
    }
}

function initGallery() {
    const container = document.getElementById('gallery-groups');
    if (!container) return; // console.html 等页面没有画廊

    galleryApplyView();
    // 图像工坊渲染室页头的「在画廊查看」：直达「图像工坊」筛选档
    document.getElementById('imgstudio-open-gallery-btn')?.addEventListener('click', () => {
        galleryFocus({ filter: 'studio' });
    });
    document.getElementById('gallery-view-switch')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.gallery-view-btn');
        if (btn) gallerySetView(btn.dataset.view);
    });

    // 筛选 chips
    const filters = document.getElementById('gallery-filters');
    filters?.addEventListener('click', (e) => {
        const chip = e.target.closest('.gallery-filter-chip');
        if (!chip) return;
        gallerySetFilter(chip.dataset.filter);
        if (!galleryData || galleryData.lazy) refreshGallery(); else renderGallery();
    });

    // 搜索（去抖 200ms）
    const searchInput = document.getElementById('gallery-search');
    let searchTimer = null;
    searchInput?.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            gallerySetSearch(searchInput.value || '');
            if (!galleryData || galleryData.lazy) refreshGallery(); else renderGallery();
        }, 200);
    });

    // 排序
    document.getElementById('gallery-sort')?.addEventListener('change', (e) => {
        gallerySort = e.target.value || 'newest';
        if (!galleryData || galleryData.lazy) refreshGallery(); else renderGallery();
    });

    document.getElementById('gallery-refresh-btn')?.addEventListener('click', () => {
        galleryData = null; // 强制显示扫描中状态
        refreshGallery({ force: true });
    });

    document.getElementById('gallery-collapse-all-btn')?.addEventListener('click', galleryToggleCollapseAll);

    document.getElementById('gallery-select-all-btn')?.addEventListener('click', () => galleryToggleScopeSelection());

    document.getElementById('gallery-clear-selection-btn')?.addEventListener('click', () => {
        gallerySelected.clear();
        gallerySelectedItems.clear();
        renderGallery();
    });
    document.getElementById('gallery-download-selected-btn')?.addEventListener('click', galleryDownloadSelected);
    document.getElementById('gallery-delete-selected-btn')?.addEventListener('click', () => {
        const paths = gallerySelectedPaths();
        if (galleryData?.lazy) {
            galleryWithBulkAction(async () => {
                const current = await galleryResolvePaths(paths);
                if (current.length) await galleryDeletePaths(current, `当前筛选中选中的 ${current.length} 个文件`, { validated: true });
            });
        } else galleryDeletePaths(paths, `当前筛选中选中的 ${paths.length} 个文件`);
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
        if (e.target.closest('.g-group-locate-project')) {
            const key = groupEl?.dataset.group;
            const group = galleryVisibleGroups().find(g => g.key === key);
            if (group) galleryLocateInProjects(group);
            return;
        }
        if (e.target.closest('.g-group-select')) {
            const key = groupEl?.dataset.group;
            if (key) galleryToggleScopeSelection([key]);
            return;
        }
        if (e.target.closest('.g-group-delete')) {
            const key = groupEl?.dataset.group;
            if (key) galleryDeleteGroup(key);
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
            if (galleryData?.lazy) {
                galleryLoadGroup(key, { more: expandBtn.dataset.expand === 'more' });
                return;
            }
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
        if (e.target.closest('.g-act-i2i')) { galleryUseAsReference(item); return; }
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
