// --- trend_refs.js ---
// 联网参考案例库（左面板第 1 区，取代旧的固定六主题选择器）。
// 后端把每次联网搜索/网址摘要的结果沉淀进 trend_refs.json；本模块负责列表渲染、
// 多选勾选、删除与「搜一批新参考」（强制绕过 6 小时缓存重搜入库）。
// 选中集合存 localStorage，app.js 的 loadIdeationCards 通过 getSelectedTrendRefIds()
// 取走作为 /api/ideate 的 trend_ref_ids —— 选中的案例即成为该批灵感的首要创意来源。
// 全部文本用 textContent 渲染（LLM/网页产出不走 innerHTML，防注入）。

let trendRefsCache = [];

const TREND_REFS_SELECTED_KEY = 'spark_selected_trend_refs';

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

    if (trendRefsCache.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = '案例库还是空的：点右上「🔍 搜一批新参考」，或直接换一批灵感（自动搜索的结果会沉淀到这里）。';
        list.appendChild(empty);
        return;
    }

    const selected = new Set(getSelectedTrendRefIds());
    trendRefsCache.forEach(ref => {
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
        const meta = document.createElement('div');
        meta.className = 'trend-ref-meta';
        meta.textContent = `${ref.source === 'custom_urls' ? '🔗 自定义网址' : '🌐 联网搜索'} · ${ref.created_at || ''}`;
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

document.addEventListener('DOMContentLoaded', () => {
    const searchBtn = document.getElementById('trend-refs-search-btn');
    if (searchBtn) searchBtn.addEventListener('click', searchNewTrendRefs);
    loadTrendRefs();
});
