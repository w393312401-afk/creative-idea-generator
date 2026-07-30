/* ==========================================================================
   Topic Ledger (创意台账) — 已用/候选选题的管理、状态流转、评分与投放表现回填。
   数据源是服务端 /api/ledger（topic_ledger.json 全量数组，2026-07-13 从
   used-topic-ledger.md 一次性迁移而来）。编辑（状态/表现打分/备注）是
   "本地改数组 → 整表 POST 回 /api/ledger"，与点子库 /api/library 同一套约定
   （见 server_common.write_ledger 的空写拒绝 + .bak 轮换防护）。
   删除（单条或批量/全选）一律走 /api/ledger/delete（按 id 列表删），不走整表
   回写——这样"选中全部再删除"不会撞上空列表覆盖防护（那道防护堵的是"客户端
   以为库是空的"这类状态错乱，按 id 删除天然不会触发它），也顺带修掉了"删除
   台账里最后一条"时整表回写会被 409 拒绝的边界情况。
   依赖宿主应用的 escapeHtml / showToast。
   ========================================================================== */

let ledgerLoaded = false;
let ledgerLoading = false;
let ledgerData = null;          // /api/ledger 的原始数组
let ledgerFilter = 'all';       // all | candidate | used | published | discarded
let ledgerSearch = '';
let ledgerSort = 'newest';      // newest | oldest | llm_score | user_score
const ledgerSelected = new Set();  // 已勾选的 id 集合（跨筛选/排序保留，供批量操作用）

const LEDGER_STATUS_LABELS = { candidate: '候选', used: '已用', published: '已发布', discarded: '已弃用' };

function ledgerTabEntered() {
    // 台账体量小（纯 JSON），不像画廊要扫描文件系统——每次进入都重新拉取，
    // 这样某条创意启动激发后立刻切回本页也能看到最新数据
    if (!ledgerLoading) refreshLedger();
}

function ledgerGenId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return `ledger-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function ledgerTodayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function refreshLedger() {
    if (ledgerLoading) return;
    ledgerLoading = true;
    const container = document.getElementById('ledger-body');
    if (container && !ledgerData) {
        container.innerHTML = '<div class="ledger-status">📡 正在加载创意台账…</div>';
    }
    try {
        const res = await fetch('/api/ledger');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && data.error) throw new Error(data.error);
        ledgerData = Array.isArray(data) ? data : [];
        ledgerLoaded = true;
        // 清掉磁盘上已不存在的勾选项，避免"批量删除"携带幽灵 id
        const aliveIds = new Set(ledgerData.map(r => r.id));
        [...ledgerSelected].forEach(id => { if (!aliveIds.has(id)) ledgerSelected.delete(id); });
    } catch (e) {
        ledgerLoading = false;
        if (container) {
            container.innerHTML = `<div class="ledger-status error">台账加载失败：${escapeHtml(e.message)}</div>`;
        }
        return;
    }
    ledgerLoading = false;
    renderLedger();
}

async function saveLedgerToServer() {
    try {
        const res = await fetch('/api/ledger', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ledgerData || []),
        });
        const data = await res.json().catch(() => ({}));
        // 409 = 服务端主动拒写（空表覆盖 / 未声明的缩量，见 server_common.write_ledger）。
        // 这类拒绝几乎总是"本页开着的这段时间里服务端追加了候选"，重新加载就能解决，
        // 所以原文报出服务端的解释，再拉一次最新数据——别让用户以为改动已经存下来了。
        if (res.status === 409) {
            showToast(data.message || '服务器拒绝了这次台账保存，已重新加载最新数据。', 'error');
            await refreshLedger();
            return false;
        }
        if (!res.ok || data.status !== 'success') {
            throw new Error(data.message || `HTTP ${res.status}`);
        }
        return true;
    } catch (e) {
        showToast(`台账保存失败：${e.message}，已重新加载最新数据`, 'error');
        await refreshLedger();
        return false;
    }
}

function ledgerVisibleRows() {
    if (!Array.isArray(ledgerData)) return [];
    const q = ledgerSearch.trim().toLowerCase();
    let rows = ledgerData.filter(r => ledgerFilter === 'all' || r.status === ledgerFilter);
    if (q) {
        rows = rows.filter(r =>
            (r.topic_dna || '').toLowerCase().includes(q) ||
            (r.one_line || '').toLowerCase().includes(q));
    }
    const arr = [...rows];
    switch (ledgerSort) {
        case 'oldest': arr.sort((a, b) => (a.date || '').localeCompare(b.date || '')); break;
        case 'llm_score': arr.sort((a, b) => (b.llm_score ?? -1) - (a.llm_score ?? -1)); break;
        case 'user_score': arr.sort((a, b) => (b.user_score ?? -1) - (a.user_score ?? -1)); break;
        default: arr.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    }
    return arr;
}

function ledgerFilterCounts() {
    const counts = { all: 0, candidate: 0, used: 0, published: 0, discarded: 0 };
    for (const r of ledgerData || []) {
        counts.all++;
        if (counts[r.status] !== undefined) counts[r.status]++;
    }
    return counts;
}

function renderLedger() {
    const container = document.getElementById('ledger-body');
    if (!container || !ledgerData) return;

    const statsEl = document.getElementById('ledger-stats');
    if (statsEl) statsEl.textContent = `共 ${ledgerData.length} 条`;

    const counts = ledgerFilterCounts();
    document.querySelectorAll('#ledger-filters .ledger-filter-chip').forEach(chip => {
        if (!chip.dataset.label) chip.dataset.label = chip.textContent.trim();
        const n = counts[chip.dataset.filter];
        chip.textContent = (n !== undefined) ? `${chip.dataset.label} (${n})` : chip.dataset.label;
    });

    const rows = ledgerVisibleRows();
    if (!rows.length) {
        container.innerHTML = ledgerData.length
            ? '<div class="ledger-status">🗂️ 这个分类下暂时没有匹配的条目</div>'
            : '<div class="ledger-status">📒 台账还是空的——真正启动激发的创意会自动收录到这里</div>';
        ledgerUpdateBulkBar();
        return;
    }

    const statusOptions = Object.entries(LEDGER_STATUS_LABELS)
        .map(([v, label]) => `<option value="${v}">${label}</option>`).join('');

    container.innerHTML = `
    <table class="ledger-table">
        <thead>
            <tr>
                <th class="l-col-check"></th>
                <th class="l-col-date">日期</th>
                <th class="l-col-dna">Topic DNA</th>
                <th class="l-col-line">一句话选题</th>
                <th class="l-col-status">状态</th>
                <th class="l-col-score" title="激发时 LLM 打的分（25 分制）">LLM 评分</th>
                <th class="l-col-score" title="人工回填的投放表现分（0-5）">表现打分</th>
                <th class="l-col-note">表现备注</th>
                <th class="l-col-source">来源</th>
                <th class="l-col-actions"></th>
            </tr>
        </thead>
        <tbody>
            ${rows.map(r => `
            <tr data-id="${escapeHtml(r.id)}">
                <td class="l-col-check"><input type="checkbox" class="l-check"${ledgerSelected.has(r.id) ? ' checked' : ''}></td>
                <td class="l-col-date">${escapeHtml(r.date || '')}</td>
                <td class="l-col-dna" title="${escapeHtml(r.topic_dna || '')}">${escapeHtml(r.topic_dna || '')}</td>
                <td class="l-col-line" title="${escapeHtml(r.one_line || '')}">${escapeHtml(r.one_line || '')}</td>
                <td class="l-col-status">
                    <select class="ledger-status-select" data-field="status">${statusOptions}</select>
                </td>
                <td class="l-col-score">${r.llm_score === null || r.llm_score === undefined ? '—' : escapeHtml(String(r.llm_score))}</td>
                <td class="l-col-score">
                    <input type="number" class="ledger-score-input" data-field="user_score" min="0" max="5" step="1"
                           value="${r.user_score === null || r.user_score === undefined ? '' : escapeHtml(String(r.user_score))}" placeholder="0-5">
                </td>
                <td class="l-col-note">
                    <input type="text" class="ledger-note-input" data-field="performance_note"
                           value="${escapeHtml(r.performance_note || '')}" placeholder="发布表现备注…">
                </td>
                <td class="l-col-source" title="${escapeHtml(r.source || '')}">${escapeHtml(r.source || '')}</td>
                <td class="l-col-actions">
                    <button type="button" class="ledger-tool-btn small l-open-project-btn" title="打开这条创意合成出来的激发项目（提示词/封面/帧与视频）">🎬 打开项目</button>
                    <button type="button" class="ledger-tool-btn small l-remix-btn" title="以这条创意为母题激发一批二创方案">♻️ 二创</button>
                    <button type="button" class="ledger-tool-btn small danger l-delete-btn" title="删除本条">🗑️</button>
                </td>
            </tr>`).join('')}
        </tbody>
    </table>`;

    // 状态下拉框的选中值不能走字符串模板拼 selected（跨行会互相干扰），渲染后统一赋值
    container.querySelectorAll('tr[data-id]').forEach(tr => {
        const id = tr.dataset.id;
        const row = ledgerData.find(r => r.id === id);
        const sel = tr.querySelector('.ledger-status-select');
        if (sel && row) sel.value = row.status || 'used';
    });

    ledgerUpdateBulkBar();
}

function ledgerUpdateBulkBar() {
    const n = ledgerSelected.size;
    const countEl = document.getElementById('ledger-bulk-count');
    if (countEl) countEl.textContent = n > 0 ? `已选 ${n} 项` : '';
    const delBtn = document.getElementById('ledger-bulk-delete-btn');
    if (delBtn) {
        delBtn.disabled = n === 0;
        delBtn.textContent = n > 0 ? `🗑️ 批量删除所选 (${n})` : '🗑️ 批量删除所选';
    }
    const applyBtn = document.getElementById('ledger-bulk-status-apply-btn');
    if (applyBtn) applyBtn.disabled = n === 0;
    const selAllBtn = document.getElementById('ledger-select-all-btn');
    if (selAllBtn) {
        const visible = ledgerVisibleRows().map(r => r.id);
        const allSelected = visible.length > 0 && visible.every(id => ledgerSelected.has(id));
        selAllBtn.textContent = allSelected ? '⬜ 取消全选' : '☑️ 全选';
    }
}

function ledgerFindRow(id) {
    return (ledgerData || []).find(r => r.id === id);
}

function initLedger() {
    const container = document.getElementById('ledger-body');
    if (!container) return; // console.html 等页面没有台账面板

    const filters = document.getElementById('ledger-filters');
    filters?.addEventListener('click', (e) => {
        const chip = e.target.closest('.ledger-filter-chip');
        if (!chip) return;
        ledgerFilter = chip.dataset.filter || 'all';
        filters.querySelectorAll('.ledger-filter-chip').forEach(c => c.classList.toggle('active', c === chip));
        renderLedger();
    });

    const searchInput = document.getElementById('ledger-search');
    let searchTimer = null;
    searchInput?.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            ledgerSearch = searchInput.value || '';
            renderLedger();
        }, 200);
    });

    document.getElementById('ledger-sort')?.addEventListener('change', (e) => {
        ledgerSort = e.target.value || 'newest';
        renderLedger();
    });

    document.getElementById('ledger-refresh-btn')?.addEventListener('click', () => {
        ledgerData = null;
        refreshLedger();
    });

    document.getElementById('ledger-select-all-btn')?.addEventListener('click', () => {
        const visible = ledgerVisibleRows().map(r => r.id);
        const allSelected = visible.length > 0 && visible.every(id => ledgerSelected.has(id));
        visible.forEach(id => { if (allSelected) ledgerSelected.delete(id); else ledgerSelected.add(id); });
        renderLedger();
    });

    document.getElementById('ledger-bulk-delete-btn')?.addEventListener('click', ledgerBulkDelete);
    document.getElementById('ledger-bulk-status-apply-btn')?.addEventListener('click', ledgerBulkApplyStatus);

    // 编辑：状态/表现打分/备注变更时更新本地数组并整表回写
    container.addEventListener('change', (e) => {
        const field = e.target.dataset && e.target.dataset.field;
        if (!field) return;
        const tr = e.target.closest('tr[data-id]');
        const row = tr && ledgerFindRow(tr.dataset.id);
        if (!row) return;
        if (field === 'user_score') {
            const v = e.target.value.trim();
            row.user_score = v === '' ? null : Math.max(0, Math.min(5, Number(v) || 0));
            e.target.value = row.user_score === null ? '' : String(row.user_score);
        } else if (field === 'performance_note') {
            row.performance_note = e.target.value;
        } else if (field === 'status') {
            row.status = e.target.value;
        }
        saveLedgerToServer();
    });

    container.addEventListener('click', (e) => {
        if (e.target.classList.contains('l-check')) {
            const tr = e.target.closest('tr[data-id]');
            if (!tr) return;
            if (e.target.checked) ledgerSelected.add(tr.dataset.id);
            else ledgerSelected.delete(tr.dataset.id);
            ledgerUpdateBulkBar();
            return;
        }
        if (e.target.closest('.l-open-project-btn')) {
            const tr = e.target.closest('tr[data-id]');
            const row = tr && ledgerFindRow(tr.dataset.id);
            if (!row) return;
            openLedgerSparkProject(row);
            return;
        }
        if (e.target.closest('.l-remix-btn')) {
            const tr = e.target.closest('tr[data-id]');
            const row = tr && ledgerFindRow(tr.dataset.id);
            if (!row) return;
            startLedgerRemix(row);
            return;
        }
        if (e.target.closest('.l-delete-btn')) {
            const tr = e.target.closest('tr[data-id]');
            const row = tr && ledgerFindRow(tr.dataset.id);
            if (!row) return;
            ledgerDeleteIds([row.id], `「${row.one_line || row.topic_dna}」这条台账记录`);
        }
    });
}

// 「🎬 激发项目」：从台账这一行跳回它合成出来的项目（点子库记录优先，其次
// 已完成的任务记录）。入账时 one_line 取的是灵感卡片选题名、creative_seed
// .input_str 取的是同一张卡的一键输入串——两者正好对上点子库的 title/theme
// 与任务 dimensions 的 task_label/theme，解析细节见 app.js openSparkProject。
async function openLedgerSparkProject(row) {
    if (typeof openSparkProject !== 'function') {
        showToast('激发结果工作区尚未加载完成，请稍后重试', 'error');
        return;
    }
    await openSparkProject({
        dna: row.topic_dna || '',
        seed: (row.creative_seed && row.creative_seed.input_str) || '',
        title: row.one_line || '',
        label: row.one_line || row.topic_dna || '该创意',
    });
}

async function startLedgerRemix(row) {
    if (typeof loadIdeationCards !== 'function' || typeof switchMainTab !== 'function') {
        showToast('二创工作区尚未加载完成，请稍后重试', 'error');
        return;
    }
    switchMainTab('config');
    showToast(`正在以「${row.one_line || row.topic_dna || '该创意'}」为母题激发二创方案`, 'info');
    await loadIdeationCards(true, { remixSeed: row });
    document.getElementById('ideation-cards-container')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// 按 id 列表删除（单条删除按钮 / 批量删除所选都走这里），命中服务端专用的
// /api/ledger/delete——不像编辑走的整表回写那样会被"空列表覆盖非空"防护拦住，
// 全选删空也能做到。
async function ledgerDeleteIds(ids, label) {
    if (!ids.length) return;
    const ok = window.confirm(`确定要删除${label}吗？此操作不可撤销。`);
    if (!ok) return;
    try {
        const res = await fetch('/api/ledger/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status !== 'ok') {
            throw new Error(data.message || `HTTP ${res.status}`);
        }
        const idSet = new Set(ids);
        ledgerData = (ledgerData || []).filter(r => !idSet.has(r.id));
        ids.forEach(id => ledgerSelected.delete(id));
        showToast(`已删除 ${data.deleted} 条台账记录`, 'success');
        renderLedger();
    } catch (e) {
        showToast(`删除失败：${e.message}`, 'error');
        await refreshLedger();
    }
}

function ledgerBulkDelete() {
    const ids = [...ledgerSelected];
    if (!ids.length) return;
    ledgerDeleteIds(ids, `选中的 ${ids.length} 条台账记录`);
}

// 批量设置状态：不涉及删条目，走既有的整表回写即可（数量不变，不会撞上空列表防护）
async function ledgerBulkApplyStatus() {
    const ids = [...ledgerSelected];
    if (!ids.length) return;
    const select = document.getElementById('ledger-bulk-status-select');
    const status = select ? select.value : 'used';
    const idSet = new Set(ids);
    (ledgerData || []).forEach(r => { if (idSet.has(r.id)) r.status = status; });
    const ok = await saveLedgerToServer();
    if (ok) {
        showToast(`已将 ${ids.length} 条设为「${LEDGER_STATUS_LABELS[status] || status}」`, 'success');
        ledgerSelected.clear();
    }
    renderLedger();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLedger);
} else {
    initLedger();
}
