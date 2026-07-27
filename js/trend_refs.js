// --- trend_refs.js ---
// 联网参考案例库（左面板第 1 区，取代旧的固定六主题选择器）。
// 后端把每次联网搜索/网址摘要的结果沉淀进 trend_refs.json；本模块负责列表渲染、
// 多选勾选、删除与「搜一批新参考」（强制绕过 6 小时缓存重搜入库）。
// 选中集合存 localStorage，app.js 的 loadIdeationCards 通过 getSelectedTrendRefIds()
// 取走作为 /api/ideate 的 trend_ref_ids —— 选中的案例即成为该批灵感的首要创意来源。
// 全部文本用 textContent 渲染（LLM/网页产出不走 innerHTML，防注入）。

let trendRefsCache = [];
let trendRefsArchiveCache = [];
let trendRefsArchiveLoaded = false;
let trendRefsCapValue = null;
let trendRefsFilterQuery = '';

const TREND_REFS_SELECTED_KEY = 'spark_selected_trend_refs';

// 库内关键词过滤（label/正文子串,大小写不敏感）：条目多起来后单靠滚动找不好找。
// 只在有条目时露出筛选框，避免空库时占地方。
function refFilterMatches(ref, q) {
    if (!q) return true;
    const hay = `${ref.label || ''} ${ref.text || ''}`.toLowerCase();
    return hay.includes(q);
}

function updateTrendRefsSelectedNote() {
    const note = document.getElementById('trend-refs-selected-note');
    if (!note) return;
    const n = getSelectedTrendRefIds().length;
    note.textContent = n > 0
        ? `已选 ${n} 条（本批优先用你选的）`
        : '未勾选：本批将自动从案例库加权随机挑 1 条';
    // 这行同时是取材抽屉折叠态的摘要；勾选变化要让激发轨的 ① 芯片跟着变
    if (typeof updateSparkRail === 'function') updateSparkRail();
}

function getSelectedTrendRefIds() {
    try {
        const raw = JSON.parse(localStorage.getItem(TREND_REFS_SELECTED_KEY));
        return Array.isArray(raw) ? raw.filter(id => typeof id === 'string') : [];
    } catch (e) {
        return [];
    }
}

function saveSelectedTrendRefIds(ids) {
    localStorage.setItem(TREND_REFS_SELECTED_KEY, JSON.stringify(ids));
}

async function loadTrendRefs() {
    const list = document.getElementById('trend-refs-list');
    if (!list) return;
    try {
        const resp = await fetch('/api/trend-refs');
        const data = await resp.json();
        if (data && Array.isArray(data.refs)) {
            trendRefsCache = data.refs;
            // 清掉指向已删除条目的幽灵勾选
            const known = new Set(trendRefsCache.map(r => r.id));
            saveSelectedTrendRefIds(getSelectedTrendRefIds().filter(id => known.has(id)));
            renderTrendRefs();
            updateCapNote(data.cap, data.archived_count);
        } else {
            list.textContent = '';
            const err = document.createElement('div');
            err.className = 'trend-refs-empty';
            err.textContent = `案例库载入失败：${(data && data.error) || '未知错误'}`;
            list.appendChild(err);
        }
    } catch (e) {
        console.error('Failed to load trend refs:', e);
        list.textContent = '';
        const err = document.createElement('div');
        err.className = 'trend-refs-empty';
        err.textContent = '案例库载入失败，请检查本地服务';
        list.appendChild(err);
    }
}

// 软上限徽标："N / 60 条 · 已归档 M 条"，超上限的老旧未用参考会自动挪进归档
// （不硬删），archived_count 为 0/null 时不打扰用户，只在有归档可看时露出入口。
function updateCapNote(cap, archivedCount) {
    if (typeof cap === 'number') trendRefsCapValue = cap;
    const note = document.getElementById('trend-refs-cap-note');
    if (note) {
        note.textContent = trendRefsCapValue ? `${trendRefsCache.length} / ${trendRefsCapValue} 条` : '';
    }
    // archivedCount 省略时只刷新左侧计数(如手动删除后),不改动归档入口的显隐状态
    if (typeof archivedCount === 'undefined') return;
    const toggle = document.getElementById('trend-refs-archive-toggle');
    if (toggle) {
        const has = typeof archivedCount === 'number' && archivedCount > 0;
        toggle.hidden = !has;
        toggle.textContent = has ? `查看归档 (${archivedCount})` : '查看归档';
        if (!has) {
            const archiveList = document.getElementById('trend-refs-archive-list');
            if (archiveList) archiveList.hidden = true;
            trendRefsArchiveLoaded = false;
        }
    }
}

async function toggleArchivePanel() {
    const archiveList = document.getElementById('trend-refs-archive-list');
    const toggle = document.getElementById('trend-refs-archive-toggle');
    if (!archiveList || !toggle) return;
    if (!archiveList.hidden) {
        archiveList.hidden = true;
        return;
    }
    archiveList.hidden = false;
    if (trendRefsArchiveLoaded) {
        renderArchiveRefs();
        return;
    }
    archiveList.textContent = '';
    const loading = document.createElement('div');
    loading.className = 'trend-refs-empty';
    loading.textContent = '正在载入归档...';
    archiveList.appendChild(loading);
    try {
        const resp = await fetch('/api/trend-refs/archive');
        const data = await resp.json();
        if (data && Array.isArray(data.refs)) {
            trendRefsArchiveCache = data.refs;
            trendRefsArchiveLoaded = true;
            renderArchiveRefs();
        } else {
            archiveList.textContent = '';
            const err = document.createElement('div');
            err.className = 'trend-refs-empty';
            err.textContent = `归档载入失败：${(data && data.error) || '未知错误'}`;
            archiveList.appendChild(err);
        }
    } catch (e) {
        console.error('Failed to load trend refs archive:', e);
        archiveList.textContent = '';
        const err = document.createElement('div');
        err.className = 'trend-refs-empty';
        err.textContent = '归档载入失败，请检查本地服务';
        archiveList.appendChild(err);
    }
}

function renderArchiveRefs() {
    const archiveList = document.getElementById('trend-refs-archive-list');
    if (!archiveList) return;
    archiveList.textContent = '';

    if (trendRefsArchiveCache.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = '归档是空的：案例库超过条数上限时，最老且从未被勾选用过的参考会自动挪到这里。';
        archiveList.appendChild(empty);
        return;
    }

    trendRefsArchiveCache.forEach(ref => {
        const item = document.createElement('div');
        item.className = 'trend-ref-item';

        const row = document.createElement('div');
        row.className = 'trend-ref-row';

        const main = document.createElement('div');
        main.className = 'trend-ref-main';
        const label = document.createElement('div');
        label.className = 'trend-ref-label';
        label.textContent = ref.label || '(未命名参考)';
        label.title = '双击改名';
        const meta = document.createElement('div');
        meta.className = 'trend-ref-meta';
        const usedSuffix = ref.used_count ? ` · 曾用 ${ref.used_count} 次` : '';
        meta.textContent = `${ref.source === 'custom_urls' ? '🔗 自定义网址' : '🌐 联网搜索'} · ${ref.created_at || ''}${usedSuffix}`;
        main.appendChild(label);
        main.appendChild(meta);
        label.addEventListener('dblclick', (e) => {
            e.stopPropagation();
            startRenameTrendRef(label, ref, renderArchiveRefs, { archive: true });
        });

        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'trend-ref-mini-btn';
        expandBtn.textContent = '详情';
        expandBtn.title = '展开/收起参考要点全文';

        const restoreBtn = document.createElement('button');
        restoreBtn.type = 'button';
        restoreBtn.className = 'trend-ref-mini-btn trend-ref-restore';
        restoreBtn.textContent = '♻ 恢复';
        restoreBtn.title = '恢复到案例库，可重新勾选使用';

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'trend-ref-mini-btn trend-ref-delete';
        delBtn.textContent = '×';
        delBtn.title = '从归档永久删除（不可恢复）';

        row.appendChild(main);
        row.appendChild(expandBtn);
        row.appendChild(restoreBtn);
        row.appendChild(delBtn);
        item.appendChild(row);

        const body = document.createElement('pre');
        body.className = 'trend-ref-text';
        body.textContent = ref.text || '';
        body.hidden = true;
        item.appendChild(body);

        expandBtn.addEventListener('click', () => {
            body.hidden = !body.hidden;
            expandBtn.textContent = body.hidden ? '详情' : '收起';
        });
        restoreBtn.addEventListener('click', () => restoreTrendRefFromArchive(ref.id, restoreBtn));
        delBtn.addEventListener('click', () => deleteArchivedTrendRef(ref.id, delBtn));

        archiveList.appendChild(item);
    });
}

async function restoreTrendRefFromArchive(id, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
        const resp = await fetch('/api/trend-refs/archive/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [id] })
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            trendRefsCache = data.refs;
            trendRefsArchiveCache = data.archive;
            renderTrendRefs();
            renderArchiveRefs();
            updateCapNote(trendRefsCapValue, trendRefsArchiveCache.length);
            if (isTrendRefsManageModalOpen()) renderTrendRefsManageList();
            showToast(data.restored > 0 ? '已恢复到案例库' : '该参考已在案例库中', 'success');
        } else {
            showToast(`恢复失败：${data.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('恢复失败，请检查本地服务', 'error');
    } finally {
        btn.disabled = false;
    }
}

async function deleteArchivedTrendRef(id, btn) {
    if (btn.dataset.confirming !== '1') {
        btn.dataset.confirming = '1';
        btn.textContent = '确认删除？';
        btn.classList.add('confirming');
        setTimeout(() => {
            btn.dataset.confirming = '';
            btn.textContent = '×';
            btn.classList.remove('confirming');
        }, 3000);
        return;
    }
    try {
        const resp = await fetch('/api/trend-refs/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [id], archive: true })
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            trendRefsArchiveCache = trendRefsArchiveCache.filter(r => r.id !== id);
            renderArchiveRefs();
            updateCapNote(trendRefsCapValue, trendRefsArchiveCache.length);
            if (isTrendRefsManageModalOpen()) renderTrendRefsManageList();
            showToast('已从归档永久删除', 'success');
        } else {
            showToast(`删除失败：${data.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('删除失败，请检查本地服务', 'error');
    }
}

function toggleTrendRefSelection(id) {
    const selected = getSelectedTrendRefIds();
    const idx = selected.indexOf(id);
    if (idx === -1) {
        selected.push(id);
    } else {
        selected.splice(idx, 1);
    }
    saveSelectedTrendRefIds(selected);
    renderTrendRefs();
}

async function relabelTrendRef(id, newLabel, btn, { archive } = {}) {
    const label = (newLabel || '').trim();
    if (!label) return false;
    try {
        const resp = await fetch('/api/trend-refs/relabel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, label, archive: !!archive })
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            const cache = archive ? trendRefsArchiveCache : trendRefsCache;
            const entry = cache.find(r => r.id === id);
            if (entry) entry.label = label;
            return true;
        }
        showToast(`改名失败：${data.message || '未知错误'}`, 'error');
    } catch (e) {
        showToast('改名失败，请检查本地服务', 'error');
    }
    return false;
}

// 点「改名」把 label 文本替换成一个内联输入框；回车/失焦提交，Esc 取消。
// 自动提炼的关键词偶尔挑得不够贴切，留个手动兜底不用整条删了重搜。
function startRenameTrendRef(labelEl, ref, renderFn, { archive } = {}) {
    if (labelEl.querySelector('input')) return;
    const original = ref.label || '';
    labelEl.textContent = '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'trend-ref-rename-input';
    input.value = original;
    input.maxLength = 80;
    labelEl.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const commit = async () => {
        if (done) return;
        done = true;
        const val = input.value.trim();
        if (!val || val === original) {
            renderFn();
            return;
        }
        const ok = await relabelTrendRef(ref.id, val, null, { archive });
        if (!ok) ref.label = original;
        renderFn();
    };
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        else if (e.key === 'Escape') { done = true; renderFn(); }
    });
    input.addEventListener('blur', commit);
    input.addEventListener('click', (e) => e.stopPropagation());
}

async function deleteTrendRef(id, btn) {
    // 两步确认：第一次点击变成「确认删除？」，3 秒内再点才真删
    if (btn.dataset.confirming !== '1') {
        btn.dataset.confirming = '1';
        btn.textContent = '确认删除？';
        btn.classList.add('confirming');
        setTimeout(() => {
            btn.dataset.confirming = '';
            btn.textContent = '×';
            btn.classList.remove('confirming');
        }, 3000);
        return;
    }
    try {
        const resp = await fetch('/api/trend-refs/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [id] })
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            trendRefsCache = trendRefsCache.filter(r => r.id !== id);
            saveSelectedTrendRefIds(getSelectedTrendRefIds().filter(sid => sid !== id));
            renderTrendRefs();
            updateCapNote();
            if (isTrendRefsManageModalOpen()) renderTrendRefsManageList();
            showToast('已删除该联网参考', 'success');
        } else {
            showToast(`删除失败：${data.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('删除失败，请检查本地服务', 'error');
    }
}

function renderTrendRefs() {
    const list = document.getElementById('trend-refs-list');
    if (!list) return;
    list.textContent = '';
    updateTrendRefsSelectedNote();

    const filterInput = document.getElementById('trend-refs-filter');
    if (filterInput) filterInput.hidden = trendRefsCache.length === 0;

    // 空库时把取材抽屉摊开一次当引导（见 js/spark_rail.js）
    if (typeof maybeGuideEmptyRefLibrary === 'function') {
        maybeGuideEmptyRefLibrary(trendRefsCache.length);
    }

    if (trendRefsCache.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = '案例库还是空的：点右上「🔍 搜一批新参考」，或直接换一批灵感（自动搜索的结果会沉淀到这里）。';
        list.appendChild(empty);
        return;
    }

    const q = trendRefsFilterQuery.trim().toLowerCase();
    const visible = trendRefsCache.filter(ref => refFilterMatches(ref, q));
    if (visible.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = `没有匹配「${trendRefsFilterQuery}」的参考`;
        list.appendChild(empty);
        return;
    }

    const selected = new Set(getSelectedTrendRefIds());
    visible.forEach(ref => {
        const item = document.createElement('div');
        item.className = 'trend-ref-item' + (selected.has(ref.id) ? ' selected' : '');

        const row = document.createElement('div');
        row.className = 'trend-ref-row';

        const check = document.createElement('span');
        check.className = 'trend-ref-check';
        check.textContent = selected.has(ref.id) ? '✓' : '';

        const main = document.createElement('div');
        main.className = 'trend-ref-main';
        const label = document.createElement('div');
        label.className = 'trend-ref-label';
        label.textContent = ref.label || '(未命名参考)';
        label.title = '双击改名';
        const meta = document.createElement('div');
        meta.className = 'trend-ref-meta';
        const usedSuffix = ref.used_count ? ` · 曾用 ${ref.used_count} 次` : '';
        meta.textContent = `${ref.source === 'custom_urls' ? '🔗 自定义网址' : '🌐 联网搜索'} · ${ref.created_at || ''}${usedSuffix}`;
        main.appendChild(label);
        main.appendChild(meta);

        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'trend-ref-mini-btn';
        expandBtn.textContent = '详情';
        expandBtn.title = '展开/收起参考要点全文';

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'trend-ref-mini-btn trend-ref-delete';
        delBtn.textContent = '×';
        delBtn.title = '从案例库删除此参考';

        row.appendChild(check);
        row.appendChild(main);
        row.appendChild(expandBtn);
        row.appendChild(delBtn);
        item.appendChild(row);

        const body = document.createElement('pre');
        body.className = 'trend-ref-text';
        body.textContent = ref.text || '';
        body.hidden = true;
        item.appendChild(body);

        row.addEventListener('click', (e) => {
            if (e.target === expandBtn || e.target === delBtn) return;
            toggleTrendRefSelection(ref.id);
        });
        label.addEventListener('dblclick', (e) => {
            e.stopPropagation();
            startRenameTrendRef(label, ref, renderTrendRefs, { archive: false });
        });
        expandBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            body.hidden = !body.hidden;
            expandBtn.textContent = body.hidden ? '详情' : '收起';
        });
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteTrendRef(ref.id, delBtn);
        });

        list.appendChild(item);
    });
}

async function searchNewTrendRefs() {
    const btn = document.getElementById('trend-refs-search-btn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const prevText = btn.textContent;
    btn.textContent = '🔍 搜索中…（约 1–2 分钟）';
    try {
        const resp = await fetch('/api/trend-refs/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: (typeof config !== 'undefined' ? config : {}) })
        });
        const data = await resp.json();
        if (data.status === 'ok' && Array.isArray(data.refs)) {
            const prevIds = new Set(trendRefsCache.map(r => r.id));
            trendRefsCache = data.refs;
            const batch = Array.isArray(data.added) ? data.added : [];
            const newCount = batch.filter(id => !prevIds.has(id)).length;
            // 自动勾选本批搜到的参考（含与旧条目重复命中的），方便直接开下一批灵感
            const selected = new Set(getSelectedTrendRefIds());
            batch.forEach(id => selected.add(id));
            saveSelectedTrendRefIds([...selected]);
            renderTrendRefs();
            updateCapNote(data.cap, data.archived_count);
            trendRefsArchiveLoaded = false; // 本轮搜索可能触发了新的自动归档,下次展开面板时重新拉取
            if (batch.length === 0) {
                showToast('本次联网搜索没有返回结果（已回退旧缓存或搜索失败），请稍后再试', 'info');
            } else if (newCount === 0) {
                showToast('搜索完成：结果与库中已有参考相同，已为你勾选', 'info');
            } else {
                showToast(`搜索完成：新增 ${newCount} 条联网参考，已自动勾选`, 'success');
            }
        } else {
            showToast(`搜索失败：${data.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        console.error('Trend refs search failed:', e);
        showToast('搜索失败，请检查网络或本地服务', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = prevText;
    }
}

// ── 自定义搜索方向（就地面板，与「配置中心」的同名设置双向同步）───────────
// 之前改搜索词/参考网址只能去配置中心，跟触发搜索的按钮不在一处，容易压根没
// 被发现——库里案例长期用同一条默认搜索词，内容自然越搜越像。这里直接读写
// localStorage 的 spark_config（不依赖全局 config 变量是否已在本脚本执行时
// 初始化完成，脚本加载顺序上 trend_refs.js 早于 app.js 调用 loadConfig()）。
function readStoredIdeationDirection() {
    try {
        const raw = JSON.parse(localStorage.getItem('spark_config') || '{}');
        return {
            query: (raw.ideationSearchQuery || '').trim(),
            urls: (raw.ideationTrendUrls || '').trim(),
        };
    } catch (e) {
        return { query: '', urls: '' };
    }
}

function saveIdeationDirection(query, urls) {
    try {
        const raw = JSON.parse(localStorage.getItem('spark_config') || '{}');
        raw.ideationSearchQuery = query;
        raw.ideationTrendUrls = urls;
        localStorage.setItem('spark_config', JSON.stringify(raw));
    } catch (e) {
        console.error('Failed to persist ideation search direction:', e);
    }
    // 保持内存里的全局 config 与配置中心弹窗（若已渲染过）同步，不用等下次刷新页面
    if (typeof config !== 'undefined') {
        config.ideationSearchQuery = query;
        config.ideationTrendUrls = urls;
    }
    const settingsQuery = document.getElementById('settings-ideation-search-query');
    if (settingsQuery) settingsQuery.value = query;
    const settingsUrls = document.getElementById('settings-ideation-trend-urls');
    if (settingsUrls) settingsUrls.value = urls;
}

function initTrendRefsDirectionPanel() {
    const toggle = document.getElementById('trend-refs-direction-toggle');
    const panel = document.getElementById('trend-refs-direction-panel');
    const queryInput = document.getElementById('trend-refs-search-query');
    const urlsInput = document.getElementById('trend-refs-trend-urls');
    if (!toggle || !panel || !queryInput || !urlsInput) return;

    const stored = readStoredIdeationDirection();
    queryInput.value = stored.query;
    urlsInput.value = stored.urls;

    toggle.addEventListener('click', () => {
        panel.hidden = !panel.hidden;
    });
    const commit = () => saveIdeationDirection(queryInput.value.trim(), urlsInput.value.trim());
    queryInput.addEventListener('blur', commit);
    urlsInput.addEventListener('blur', commit);
}

// ── 案例库管理弹窗（🗂️ 管理全部）─────────────────────────────────────────
// 侧栏紧凑列表只负责"选案例喂给下一批灵感"；条目多起来后浏览/整理需要更大
// 空间和排序/批量操作。这是独立于创意台账的一套视图——两者数据模型不同
// （used_count/source vs. llm_score/status），不合并数据，只借创意台账已经
// 验证过的 toolbar/bulk-bar UI 范式（css/ledger.css，全局已加载）。
let trendRefsManageScope = 'main';
let trendRefsManageFilter = '';
let trendRefsManageSort = 'created_desc';
let trendRefsManageBulkSelected = new Set();

function isTrendRefsManageModalOpen() {
    const modal = document.getElementById('trend-refs-manage-modal');
    return !!modal && modal.classList.contains('active');
}

async function ensureTrendRefsArchiveLoaded() {
    if (trendRefsArchiveLoaded) return;
    try {
        const resp = await fetch('/api/trend-refs/archive');
        const data = await resp.json();
        if (data && Array.isArray(data.refs)) {
            trendRefsArchiveCache = data.refs;
            trendRefsArchiveLoaded = true;
        }
    } catch (e) {
        console.error('Failed to load trend refs archive:', e);
    }
}

async function openTrendRefsManageModal() {
    const modal = document.getElementById('trend-refs-manage-modal');
    if (!modal) return;
    trendRefsManageBulkSelected = new Set();
    modal.classList.add('active');
    renderTrendRefsManageList();
    await ensureTrendRefsArchiveLoaded();
    renderTrendRefsManageList(); // 归档懒加载完成后（若还没加载过）刷新一次统计/归档列表
}

function closeTrendRefsManageModal() {
    const modal = document.getElementById('trend-refs-manage-modal');
    if (modal) modal.classList.remove('active');
}

function trendRefsManageVisibleList() {
    const source = trendRefsManageScope === 'archive' ? trendRefsArchiveCache : trendRefsCache;
    const q = trendRefsManageFilter.trim().toLowerCase();
    const filtered = source.filter(ref => refFilterMatches(ref, q));
    const sorted = [...filtered];
    switch (trendRefsManageSort) {
        case 'created_asc':
            sorted.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
            break;
        case 'used_desc':
            sorted.sort((a, b) => (b.used_count || 0) - (a.used_count || 0));
            break;
        case 'label_asc':
            sorted.sort((a, b) => (a.label || '').localeCompare(b.label || '', 'zh-Hans-CN'));
            break;
        case 'created_desc':
        default:
            sorted.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
            break;
    }
    return sorted;
}

function updateTrendRefsManageBulkBar() {
    const count = trendRefsManageBulkSelected.size;
    const countEl = document.getElementById('trend-refs-manage-bulk-count');
    if (countEl) countEl.textContent = count > 0 ? `已选 ${count} 条` : '';
    const delBtn = document.getElementById('trend-refs-manage-bulk-delete-btn');
    if (delBtn) {
        delBtn.disabled = count === 0;
        delBtn.textContent = count > 0 ? `🗑️ 批量删除所选 (${count})` : '🗑️ 批量删除所选';
    }
    const selAllBtn = document.getElementById('trend-refs-manage-select-all-btn');
    if (selAllBtn) {
        const visible = trendRefsManageVisibleList().map(r => r.id);
        const allSelected = visible.length > 0 && visible.every(id => trendRefsManageBulkSelected.has(id));
        selAllBtn.textContent = allSelected ? '⬜ 取消全选' : '☑️ 全选';
    }
}

function updateTrendRefsManageStats() {
    const stats = document.getElementById('trend-refs-manage-stats');
    if (!stats) return;
    const capText = trendRefsCapValue ? ` / ${trendRefsCapValue}` : '';
    stats.textContent = `主库 ${trendRefsCache.length}${capText} 条 · 归档 ${trendRefsArchiveCache.length} 条`;
}

function renderTrendRefsManageList() {
    const list = document.getElementById('trend-refs-manage-list');
    if (!list) return;
    updateTrendRefsManageStats();
    list.textContent = '';

    const isArchive = trendRefsManageScope === 'archive';
    const source = isArchive ? trendRefsArchiveCache : trendRefsCache;
    const sorted = trendRefsManageVisibleList();
    updateTrendRefsManageBulkBar();

    if (sorted.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = source.length === 0
            ? (isArchive ? '归档是空的' : '案例库还是空的')
            : `没有匹配「${trendRefsManageFilter}」的参考`;
        list.appendChild(empty);
        return;
    }

    sorted.forEach(ref => {
        const item = document.createElement('div');
        item.className = 'trend-ref-item' + (trendRefsManageBulkSelected.has(ref.id) ? ' selected' : '');

        const row = document.createElement('div');
        row.className = 'trend-ref-row';

        const check = document.createElement('input');
        check.type = 'checkbox';
        check.className = 'trend-ref-bulk-check';
        check.checked = trendRefsManageBulkSelected.has(ref.id);
        check.title = '勾选以批量操作';

        const main = document.createElement('div');
        main.className = 'trend-ref-main';
        const label = document.createElement('div');
        label.className = 'trend-ref-label';
        label.textContent = ref.label || '(未命名参考)';
        label.title = '双击改名';
        const meta = document.createElement('div');
        meta.className = 'trend-ref-meta';
        const usedSuffix = ref.used_count ? ` · 曾用 ${ref.used_count} 次` : '';
        meta.textContent = `${ref.source === 'custom_urls' ? '🔗 自定义网址' : '🌐 联网搜索'} · ${ref.created_at || ''}${usedSuffix}`;
        main.appendChild(label);
        main.appendChild(meta);

        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'trend-ref-mini-btn';
        expandBtn.textContent = '详情';
        expandBtn.title = '展开/收起参考要点全文';

        row.appendChild(check);
        row.appendChild(main);
        row.appendChild(expandBtn);

        let restoreBtn = null;
        if (isArchive) {
            restoreBtn = document.createElement('button');
            restoreBtn.type = 'button';
            restoreBtn.className = 'trend-ref-mini-btn trend-ref-restore';
            restoreBtn.textContent = '♻ 恢复';
            restoreBtn.title = '恢复到案例库，可重新勾选使用';
            row.appendChild(restoreBtn);
        }

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'trend-ref-mini-btn trend-ref-delete';
        delBtn.textContent = '×';
        delBtn.title = isArchive ? '从归档永久删除（不可恢复）' : '从案例库删除此参考';
        row.appendChild(delBtn);

        item.appendChild(row);

        const body = document.createElement('pre');
        body.className = 'trend-ref-text';
        body.textContent = ref.text || '';
        body.hidden = true;
        item.appendChild(body);

        check.addEventListener('change', () => {
            if (check.checked) trendRefsManageBulkSelected.add(ref.id);
            else trendRefsManageBulkSelected.delete(ref.id);
            item.classList.toggle('selected', check.checked);
            updateTrendRefsManageBulkBar();
        });
        label.addEventListener('dblclick', () => {
            startRenameTrendRef(label, ref, renderTrendRefsManageList, { archive: isArchive });
        });
        expandBtn.addEventListener('click', () => {
            body.hidden = !body.hidden;
            expandBtn.textContent = body.hidden ? '详情' : '收起';
        });
        if (restoreBtn) {
            restoreBtn.addEventListener('click', async () => {
                await restoreTrendRefFromArchive(ref.id, restoreBtn);
                renderTrendRefsManageList();
            });
        }
        // 删除按钮是两步确认（见 deleteTrendRef/deleteArchivedTrendRef）：第一次点击只是
        // 把按钮变成「确认删除？」，此时不能重渲染列表——重渲染会换一批全新 DOM 节点，
        // armed 状态（dataset.confirming）跟着丢失，用户看起来像"点了没反应"。只在
        // 第二次点击（真正触发了删除请求）之后才刷新。
        delBtn.addEventListener('click', async () => {
            const wasConfirming = delBtn.dataset.confirming === '1';
            if (isArchive) await deleteArchivedTrendRef(ref.id, delBtn);
            else await deleteTrendRef(ref.id, delBtn);
            if (wasConfirming) renderTrendRefsManageList();
        });

        list.appendChild(item);
    });
}

async function trendRefsManageBulkDelete() {
    const ids = [...trendRefsManageBulkSelected];
    if (ids.length === 0) return;
    const isArchive = trendRefsManageScope === 'archive';
    const ok = window.confirm(
        isArchive
            ? `确定要从归档永久删除选中的 ${ids.length} 条参考吗？此操作不可撤销。`
            : `确定要从案例库删除选中的 ${ids.length} 条参考吗？此操作不可撤销。`
    );
    if (!ok) return;
    try {
        const resp = await fetch('/api/trend-refs/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids, archive: isArchive })
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            const idSet = new Set(ids);
            if (isArchive) {
                trendRefsArchiveCache = trendRefsArchiveCache.filter(r => !idSet.has(r.id));
            } else {
                trendRefsCache = trendRefsCache.filter(r => !idSet.has(r.id));
                saveSelectedTrendRefIds(getSelectedTrendRefIds().filter(id => !idSet.has(id)));
                renderTrendRefs();
            }
            trendRefsManageBulkSelected = new Set();
            renderTrendRefsManageList();
            updateCapNote(trendRefsCapValue, trendRefsArchiveCache.length);
            showToast(`已删除 ${data.deleted} 条`, 'success');
        } else {
            showToast(`批量删除失败：${data.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量删除失败，请检查本地服务', 'error');
    }
}

function initTrendRefsManageModal() {
    const openBtn = document.getElementById('trend-refs-manage-open-btn');
    if (openBtn) openBtn.addEventListener('click', openTrendRefsManageModal);
    const closeBtn = document.getElementById('trend-refs-manage-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeTrendRefsManageModal);

    const scopeBar = document.getElementById('trend-refs-manage-scope');
    if (scopeBar) {
        scopeBar.querySelectorAll('.ledger-filter-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                scopeBar.querySelectorAll('.ledger-filter-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                trendRefsManageScope = chip.dataset.scope;
                trendRefsManageBulkSelected = new Set();
                renderTrendRefsManageList();
            });
        });
    }

    const filterInput = document.getElementById('trend-refs-manage-filter');
    if (filterInput) {
        filterInput.addEventListener('input', () => {
            trendRefsManageFilter = filterInput.value;
            renderTrendRefsManageList();
        });
    }
    const sortSelect = document.getElementById('trend-refs-manage-sort');
    if (sortSelect) {
        sortSelect.addEventListener('change', () => {
            trendRefsManageSort = sortSelect.value;
            renderTrendRefsManageList();
        });
    }
    const selAllBtn = document.getElementById('trend-refs-manage-select-all-btn');
    if (selAllBtn) {
        selAllBtn.addEventListener('click', () => {
            const visible = trendRefsManageVisibleList().map(r => r.id);
            const allSelected = visible.length > 0 && visible.every(id => trendRefsManageBulkSelected.has(id));
            visible.forEach(id => { if (allSelected) trendRefsManageBulkSelected.delete(id); else trendRefsManageBulkSelected.add(id); });
            renderTrendRefsManageList();
        });
    }
    const bulkDeleteBtn = document.getElementById('trend-refs-manage-bulk-delete-btn');
    if (bulkDeleteBtn) bulkDeleteBtn.addEventListener('click', trendRefsManageBulkDelete);
}

document.addEventListener('DOMContentLoaded', () => {
    const searchBtn = document.getElementById('trend-refs-search-btn');
    if (searchBtn) searchBtn.addEventListener('click', searchNewTrendRefs);
    const archiveToggle = document.getElementById('trend-refs-archive-toggle');
    if (archiveToggle) archiveToggle.addEventListener('click', toggleArchivePanel);
    const filterInput = document.getElementById('trend-refs-filter');
    if (filterInput) {
        filterInput.addEventListener('input', () => {
            trendRefsFilterQuery = filterInput.value;
            renderTrendRefs();
        });
    }
    initTrendRefsDirectionPanel();
    initTrendRefsManageModal();
    loadTrendRefs();
});
