/* ==========================================================================
   项目工作台（Project Workbench）—— 「激发任务列表」+「我的点子库」合并后的主页面。

   为什么合并：两者描述的是同一条创意的两个阶段（跑起来 → 收起来），但历史上是
   两个 380px 的右侧抽屉，互斥打开、还要和日志 dock 抢位置，各自一套搜索框；
   同一个项目在 任务列表/点子库/创意台账/画廊 四处各有一张卡，靠标题模糊匹配
   互相反查。现在四路数据在服务端按 project_key 合流成一张项目表
   （见 server_common.build_projects_index），本模块只负责渲染它。

   与旧任务抽屉的三点关键差异：
     · 列表是 keyed 增量渲染（按 project_key 逐行 diff），不是整块 innerHTML
       重绘 —— 旧 renderTasks 靠"整串 HTML 字符串比对"跳过重绘，一旦有任何变化
       就整块重置，hover/焦点/展开态全丢；
     · 轮询只在有运行中项目时才快（4s），静止时 30s，且带 assets=0 跳过 outputs/
       目录扫描；旧版是恒定 2.5s 全量 + 另一路 5s/30s 角标轮询两条轨；
     · 帧/视频/封面这些媒体作业不再被整类过滤掉（旧 MEDIA_TASK_TYPES 的做法让
       失败的帧任务完全不可见），而是挂成项目行下的子作业徽章。

   依赖宿主应用：escapeHtml / showToast / customConfirm / switchMainTab /
   openSparkProject / viewTask / loadCompletedTask / rerunCompletedTask /
   retryTask / cancelTask / deleteTask / deleteFromLibrary。
   见 docs/project_workbench_refactor_plan.md
   ========================================================================== */

let projectsRows = null;           // 当前页的项目行（服务端已筛选/排序）
let projectsCounts = {};           // chips 角标（在完整表上统计，不受筛选影响）
let projectsTotal = 0;
let projectsFilter = 'all';        // all | running | completed | saved | failed
let projectsSearch = '';
let projectsSort = 'newest';       // newest | oldest | title
let projectsSelectedKey = null;    // 详情 pane 当前选中的 project_key
let projectsLoading = false;
let projectsPollTimer = null;
let projectsTabActive = false;
let projectsSearchDebounce = null;

const PROJECT_STATE_LABELS = {
    running: '运行中', completed: '已完成', saved: '已收藏',
    failed: '已失败', cancelled: '已取消', unknown: '—',
};
const PROJECT_JOB_LABELS = {
    frames: '帧序列', staged_render: '分步渲染', videos: '视频', cover: '封面',
};
const PROJECT_JOB_ICONS = {
    completed: '✓', failed: '✕', running: '⏳', cancelled: '⚪',
};

/* ── 数据 ──────────────────────────────────────────────────────────────── */

function projectsTabEntered() {
    projectsTabActive = true;
    refreshProjects();
    projectsSchedulePoll();
}

function projectsTabLeft() {
    projectsTabActive = false;
    if (projectsPollTimer) {
        clearTimeout(projectsPollTimer);
        projectsPollTimer = null;
    }
}

// 轮询节奏跟着"有没有东西在跑"走。资产统计要遍历 outputs/ 下每个项目目录，
// 轮询时一律跳过（assets=0）——只有手动刷新和首次进入才算一遍文件数。
function projectsSchedulePoll() {
    if (projectsPollTimer) clearTimeout(projectsPollTimer);
    if (!projectsTabActive) return;
    const hasRunning = (projectsRows || []).some(p => p.state === 'running');
    projectsPollTimer = setTimeout(() => {
        if (!projectsTabActive) return;
        refreshProjects({ assets: false, silent: true }).finally(projectsSchedulePoll);
    }, hasRunning ? 4000 : 30000);
}

async function refreshProjects(options = {}) {
    const { assets = true, silent = false } = options;
    if (projectsLoading) return;
    projectsLoading = true;

    const container = document.getElementById('projects-list');
    if (container && !projectsRows && !silent) {
        container.innerHTML = '<div class="projects-status">📡 正在汇总项目…</div>';
    }
    try {
        const params = new URLSearchParams({
            state: projectsFilter,
            q: projectsSearch,
            sort: projectsSort,
            limit: '200',
        });
        if (!assets) params.set('assets', '0');
        const res = await fetch(`/api/projects?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data && data.error) throw new Error(data.error);

        // assets=0 的轮询回来的行没有资产统计。直接覆盖会让"19 个文件"在两次
        // 轮询之间闪成 0，所以只在这次确实带了资产时才接受新值。
        if (!assets && Array.isArray(projectsRows)) {
            const prev = new Map(projectsRows.map(p => [p.project_key, p]));
            (data.projects || []).forEach(p => {
                const old = prev.get(p.project_key);
                if (!old) return;
                if (!p.assets) p.assets = old.assets;
                // 轻量轮询（assets=0）不会扫描 outputs，因此未收藏项目的磁盘封面
                // 不会出现在响应里。保留上一次完整刷新拿到的封面，避免每次轮询后
                // 缩略图从真实图片退化成灯泡占位图。
                if (!p.cover && old.cover) p.cover = old.cover;
            });
        }
        projectsRows = data.projects || [];
        projectsCounts = data.counts || {};
        projectsTotal = data.total_count || 0;
    } catch (e) {
        projectsLoading = false;
        console.error('Failed to load projects', e);
        if (container && !silent) {
            container.innerHTML = `<div class="projects-status error">项目列表加载失败：${escapeHtml(e.message)}</div>`;
        }
        return;
    }
    projectsLoading = false;
    renderProjects();
}

function projectsFindRow(key) {
    return (projectsRows || []).find(p => p.project_key === key) || null;
}

/* ── 渲染 ──────────────────────────────────────────────────────────────── */

function projectsFormatTime(epochSeconds) {
    const ms = Number(epochSeconds) * 1000;
    if (!Number.isFinite(ms) || ms <= 0) return '—';
    const d = new Date(ms);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function projectsFormatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function projectsFormatDuration(totalSeconds) {
    const s = Number(totalSeconds);
    if (!Number.isFinite(s) || s < 0) return '';
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} 秒`;
    const whole = Math.round(s);
    const m = Math.floor(whole / 60);
    if (m < 60) return `${m} 分 ${whole % 60} 秒`;
    return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

function projectsSafeCoverUrl(url) {
    return typeof url === 'string' && (
        url.startsWith('http://') || url.startsWith('https://') ||
        url.startsWith('data:image/') || url.startsWith('/') || url.startsWith('outputs/'));
}

function projectsHandleCoverError(img) {
    const fallback = img && img.dataset ? img.dataset.fallback : '';
    if (fallback && !img.dataset.fallbackTried) {
        img.dataset.fallbackTried = '1';
        img.src = fallback;
        return;
    }
    if (img) img.outerHTML = '<div class="project-thumb-icon">💡</div>';
}

function projectsCoverHtml(p) {
    const candidates = [p.cover, p.assets && p.assets.cover]
        .filter(projectsSafeCoverUrl)
        .filter((url, index, all) => all.indexOf(url) === index);
    if (!candidates.length) return '<div class="project-thumb-icon">💡</div>';
    const fallback = candidates[1]
        ? ` data-fallback="${escapeHtml(candidates[1])}"`
        : '';
    return `<img src="${escapeHtml(candidates[0])}"${fallback} alt="" loading="lazy"
                 onerror="projectsHandleCoverError(this)">`;
}

function projectsBadgesHtml(p) {
    const badges = [`<span class="project-badge state-${escapeHtml(p.state)}">${escapeHtml(PROJECT_STATE_LABELS[p.state] || p.state)}</span>`];
    if (p.saved && p.state !== 'saved') badges.push('<span class="project-badge saved">已收藏</span>');
    if (p.kind === 'job') {
        badges.push('<span class="project-badge job" title="母项目的激发任务记录已被清理（任务记录只保留 7 天），只剩这些媒体作业">孤立作业</span>');
    }
    if (p.has_failed_jobs && p.state !== 'failed') {
        badges.push('<span class="project-badge warn" title="有媒体子作业失败——这类失败在旧任务列表里完全不可见">子作业失败</span>');
    }
    if (p.ledger && p.ledger.status) {
        const labels = { candidate: '候选', used: '已用', published: '已发布', discarded: '已弃用' };
        badges.push(`<span class="project-badge ledger">📒 ${escapeHtml(labels[p.ledger.status] || p.ledger.status)}</span>`);
    }
    return badges.join('');
}

function projectsJobsHtml(p) {
    if (!p.sub_jobs || !p.sub_jobs.length) return '';
    return `<div class="project-jobs">${p.sub_jobs.map(j => `
        <span class="project-job ${escapeHtml(j.status || '')}" title="${escapeHtml(j.error || j.status || '')}">
            ${escapeHtml(PROJECT_JOB_ICONS[j.status] || '·')} ${escapeHtml(PROJECT_JOB_LABELS[j.type] || j.type)}
        </span>`).join('')}</div>`;
}

function projectsMetaHtml(p) {
    const bits = [];
    if (p.video_count) bits.push(`${p.video_count} 镜`);
    else if (p.image_count) bits.push(`${p.image_count} 图`);
    if (p.assets && p.assets.file_count) {
        bits.push(`${p.assets.file_count} 个文件 · ${projectsFormatBytes(p.assets.bytes)}`);
    }
    bits.push(projectsFormatTime(p.updated_at));
    return `<div class="project-meta">${bits.map(escapeHtml).join(' · ')}</div>`;
}

function projectsRowInnerHtml(p) {
    const task = p.task || {};
    const running = p.state === 'running';
    const progress = running
        ? `<div class="project-progress"><span class="project-progress-stage">${escapeHtml(task.stage || '准备中…')}</span><span class="project-spinner"></span></div>`
        : '';
    const error = (p.state === 'failed' && task.error)
        ? `<div class="project-error">❌ ${escapeHtml(task.error)}</div>` : '';
    return `
        <div class="project-thumb">${projectsCoverHtml(p)}</div>
        <div class="project-main">
            <div class="project-title-line">
                <span class="project-title">${escapeHtml(p.title || '未命名项目')}</span>
                ${projectsBadgesHtml(p)}
            </div>
            ${projectsMetaHtml(p)}
            ${progress}
            ${error}
            ${projectsJobsHtml(p)}
        </div>`;
}

// 一行的可见状态指纹。只有它变了才重建这一行的 DOM——列表每 4s 轮询一次，
// 绝大多数 tick 下所有行都该是 no-op。
function projectsRowSignature(p) {
    const task = p.task || {};
    return JSON.stringify([
        p.title, p.theme, p.state, p.saved, p.kind, p.cover, p.has_failed_jobs,
        p.image_count, p.video_count, p.updated_at,
        task.status, task.stage, task.error,
        (p.assets || {}).file_count, (p.assets || {}).bytes,
        (p.ledger || {}).status,
        (p.sub_jobs || []).map(j => `${j.type}:${j.status}`).join(','),
    ]);
}

function renderProjects() {
    const container = document.getElementById('projects-list');
    if (!container || !projectsRows) return;

    // chips 角标
    document.querySelectorAll('#projects-filters .projects-filter-chip').forEach(chip => {
        if (!chip.dataset.label) chip.dataset.label = chip.textContent.trim();
        const n = projectsCounts[chip.dataset.filter];
        chip.textContent = (n !== undefined) ? `${chip.dataset.label} (${n})` : chip.dataset.label;
        chip.classList.toggle('active', chip.dataset.filter === projectsFilter);
    });
    const statsEl = document.getElementById('projects-stats');
    if (statsEl) statsEl.textContent = `共 ${projectsTotal} 个项目`;

    if (!projectsRows.length) {
        container.innerHTML = projectsTotal
            ? '<div class="projects-status">🗂️ 这个筛选下暂时没有匹配的项目</div>'
            : '<div class="projects-status">📁 还没有项目——去「激发维度」跑一次创意激发，它会自动出现在这里</div>';
        renderProjectDetail();
        return;
    }

    // keyed 增量渲染：按 project_key 逐行比对，只重建真正变了的行
    const existing = new Map();
    container.querySelectorAll('.project-row').forEach(el => existing.set(el.dataset.key, el));
    const seen = new Set();
    let anchor = null;   // 已就位的上一行，用于维持顺序

    projectsRows.forEach(p => {
        const key = p.project_key;
        seen.add(key);
        let row = existing.get(key);
        const sig = projectsRowSignature(p);
        if (!row) {
            row = document.createElement('div');
            row.className = 'project-row';
            row.dataset.key = key;
            row.innerHTML = projectsRowInnerHtml(p);
            row.dataset.sig = sig;
        } else if (row.dataset.sig !== sig) {
            row.innerHTML = projectsRowInnerHtml(p);
            row.dataset.sig = sig;
        }
        row.dataset.state = p.state;
        row.classList.toggle('selected', key === projectsSelectedKey);
        // 顺序维护：只有位置真的不对时才 insertBefore（移动节点会打断
        // :hover，能不动就不动）
        const expectedNext = anchor ? anchor.nextElementSibling : container.firstElementChild;
        if (expectedNext !== row) container.insertBefore(row, expectedNext);
        anchor = row;
    });

    existing.forEach((el, key) => { if (!seen.has(key)) el.remove(); });
    // 首次渲染时容器里可能还留着"正在汇总"的占位块
    container.querySelectorAll('.projects-status').forEach(el => el.remove());

    if (projectsSelectedKey && !seen.has(projectsSelectedKey)) projectsSelectedKey = null;
    renderProjectDetail();
}

/* ── 详情 pane ─────────────────────────────────────────────────────────── */

// 「换模型再跑」的下拉选项。原先长在任务抽屉的成功卡上，抽屉删除后搬到详情栏；
// rerunCompletedTask 读的就是这个 select（见 app.js）。
function projectsModelOptions(selectedModel) {
    const selected = selectedModel || config.model || DEFAULT_CONFIG.model;
    const families = (typeof LLM_MODEL_PICKER_FAMILIES !== 'undefined') ? LLM_MODEL_PICKER_FAMILIES : [];
    const groups = (typeof LLM_MODEL_GROUPS !== 'undefined') ? LLM_MODEL_GROUPS : {};
    const known = Object.values(groups).flat();
    return families.map(family => {
        const models = (groups[family.key] || []).slice();
        // 历史任务用过的模型可能已从选择器里下架，补一条免得选中项落空
        if (!known.some(m => m.value === selected) && family.key === 'gpt') {
            models.push({ value: selected, label: `${selected}（历史模型）` });
        }
        const options = models.map(m =>
            `<option value="${escapeHtml(m.value)}"${m.value === selected ? ' selected' : ''}>${escapeHtml(m.label)}</option>`
        ).join('');
        return `<optgroup label="${escapeHtml(family.label)}">${options}</optgroup>`;
    }).join('');
}

// 孤立作业行（kind='job'）没有 task，上面那一整排按钮（打开/跟进/再跑/重试/
// 删除任务记录）全都不成立，详情栏因此长期只剩一张事实表——用户看得见这行、
// 却什么都做不了。这里补的是它**能**做的几件事：找回母项目、批量收拾这批作业
// 记录、以及在画廊里按标题找回它产出的文件。
function projectsOrphanActionsHtml(p) {
    const jobs = (p.sub_jobs || []).filter(j => j && j.id);
    const running = jobs.filter(j => j.status === 'running');
    const settled = jobs.filter(j => j.status !== 'running');
    const btns = [
        '<button type="button" class="projects-btn primary" data-act="find-parent" title="按标题/主题去点子库和任务列表里反查母项目">🔍 找回母项目</button>',
    ];
    if (running.length) {
        btns.push(`<button type="button" class="projects-btn danger" data-act="cancel-all-jobs">✕ 取消全部运行中（${running.length}）</button>`);
    }
    // 资产按钮由通用分支给（有 assets.dir 时能精确定位分组）；找不到目录的孤立行
    // 只能退而求其次，把标题丢进画廊搜索框
    if (!(p.assets && p.assets.file_count)) {
        btns.push('<button type="button" class="projects-btn" data-act="gallery-search">🖼️ 在画廊里搜这个标题</button>');
    }
    btns.push('<button type="button" class="projects-btn" data-act="copy-title">📋 复制标题</button>');
    if (settled.length) {
        btns.push(`<button type="button" class="projects-btn danger" data-act="delete-all-jobs">🗑️ 清空全部作业记录（${settled.length}）</button>`);
    }
    return btns.join('');
}

function projectsDetailActionsHtml(p) {
    const task = p.task || {};
    const btns = [];
    if (p.kind === 'job') btns.push(projectsOrphanActionsHtml(p));
    if (p.saved || task.status === 'completed') {
        btns.push('<button type="button" class="projects-btn primary" data-act="open">🎬 打开项目</button>');
    }
    if (task.status === 'running') {
        btns.push('<button type="button" class="projects-btn primary" data-act="follow">👁 跟进实时输出</button>');
        btns.push('<button type="button" class="projects-btn danger" data-act="cancel">✕ 取消</button>');
    }
    if (task.status === 'completed') {
        btns.push(`<label class="projects-rerun">
            <select id="projects-rerun-model" class="projects-sort-select" aria-label="选择重新激发使用的模型">
                ${projectsModelOptions(task.model)}
            </select>
            <button type="button" class="projects-btn" data-act="rerun">♻️ 换模型再跑</button>
        </label>`);
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        btns.push('<button type="button" class="projects-btn" data-act="retry">🔁 重试</button>');
    }
    if (p.assets && p.assets.file_count) {
        btns.push('<button type="button" class="projects-btn" data-act="gallery">🖼️ 去画廊看资产</button>');
    }
    if (p.ledger) {
        btns.push('<button type="button" class="projects-btn" data-act="ledger">📒 去台账</button>');
    }
    if (p.saved) {
        btns.push('<button type="button" class="projects-btn danger" data-act="unsave">🗑️ 从点子库删除</button>');
    }
    if (task.id) {
        btns.push('<button type="button" class="projects-btn danger" data-act="delete-task">🗑️ 删除任务记录</button>');
    }
    return btns.join('');
}

function projectsJobActionsHtml(job) {
    if (!job || !job.id) return '';
    const jobId = escapeHtml(job.id);
    const buttons = [];
    if (job.status === 'running') {
        buttons.push(`<button type="button" class="projects-job-btn danger"
            data-act="cancel-job" data-job-id="${jobId}">取消</button>`);
    } else {
        buttons.push(`<button type="button" class="projects-job-btn danger"
            data-act="delete-job" data-job-id="${jobId}">删除记录</button>`);
    }
    return `<span class="projects-job-actions">${buttons.join('')}</span>`;
}

function renderProjectDetail() {
    const pane = document.getElementById('projects-detail');
    if (!pane) return;
    const p = projectsSelectedKey ? projectsFindRow(projectsSelectedKey) : null;
    if (!p) {
        pane.innerHTML = '<div class="projects-detail-empty">选中左侧任一项目查看详情</div>';
        pane.classList.remove('open');
        return;
    }
    pane.classList.add('open');

    const task = p.task || {};
    const facts = [
        ['状态', PROJECT_STATE_LABELS[p.state] || p.state],
        ['最近活动', projectsFormatTime(p.updated_at)],
        ['镜头 / 图片', [p.video_count ? `${p.video_count} 镜` : null,
                        p.image_count ? `${p.image_count} 图` : null].filter(Boolean).join(' · ') || '—'],
        ['激发耗时', projectsFormatDuration(task.duration_seconds) || '—'],
        ['使用模型', task.model || '—'],
        ['磁盘资产', p.assets && p.assets.file_count
            ? `${p.assets.file_count} 个文件 · ${projectsFormatBytes(p.assets.bytes)}`
            : '—'],
        ['收藏时间', (p.library && p.library.timestamp) || '未收藏'],
        ['任务 ID', task.id || '—'],
        ['project_key', p.project_key],
    ];
    if (task.token_usage) {
        const u = task.token_usage;
        facts.push(['Tokens', `${u.total_tokens} (I:${u.prompt_tokens} O:${u.completion_tokens}) · ${u.api_calls} 次调用`]);
    }
    if (p.ledger) {
        facts.push(['台账 DNA', p.ledger.topic_dna || '—']);
        facts.push(['评分', `LLM ${p.ledger.llm_score ?? '—'} · 表现 ${p.ledger.user_score ?? '—'}`]);
    }

    const jobs = (p.sub_jobs || []).length ? `
        <div class="projects-detail-section">
            <h4>媒体子作业</h4>
            <ul class="projects-job-list">
                ${p.sub_jobs.map(j => `
                <li class="${escapeHtml(j.status || '')}">
                    <span class="projects-job-head">
                        <span>${escapeHtml(PROJECT_JOB_ICONS[j.status] || '·')} ${escapeHtml(PROJECT_JOB_LABELS[j.type] || j.type)}</span>
                        ${projectsJobActionsHtml(j)}
                    </span>
                    <span class="projects-job-id">${escapeHtml(j.id || '')}</span>
                    ${j.error ? `<span class="projects-job-error">${escapeHtml(j.error)}</span>` : ''}
                </li>`).join('')}
            </ul>
        </div>` : '';

    pane.innerHTML = `
        <div class="projects-detail-head">
            <h3>${escapeHtml(p.title || '未命名项目')}</h3>
            <button type="button" class="projects-detail-close" data-act="close-detail" title="收起详情">&times;</button>
        </div>
        ${p.theme ? `<p class="projects-detail-theme">${escapeHtml(p.theme)}</p>` : ''}
        ${p.kind === 'job' ? `<p class="projects-detail-hint">🧩 孤立作业：母项目的激发任务记录已被清理（任务记录只保留 7 天），
            这里只剩这批媒体作业本身。下面的动作围绕"找回它/收拾它"，不会碰 outputs/ 里的文件。</p>` : ''}
        <div class="projects-detail-actions">${projectsDetailActionsHtml(p)}</div>
        <dl class="projects-facts">
            ${facts.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('')}
        </dl>
        ${jobs}`;
}

/* ── 动作 ──────────────────────────────────────────────────────────────── */

// 批量作业操作直接打端点，不走 deleteTask/cancelTask：那两个各自带一次
// customConfirm 和一条 toast，一行 4 个子作业就是 4 次确认 + 4 条提示。
// 这里确认与提示各只做一次，逐条失败也不打断剩下的。
async function projectsBulkJobRequest(url, ids) {
    let ok = 0;
    for (const id of ids) {
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: id }),
            });
            if (res.ok) ok++;
        } catch (e) {
            console.error('Bulk job request failed', url, id, e);
        }
    }
    return ok;
}

function projectsConfirm(message) {
    return (typeof customConfirm === 'function')
        ? customConfirm(message)
        : Promise.resolve(confirm(message));
}

async function projectsRunAction(act, p, event, jobId) {
    const task = p.task || {};
    switch (act) {
        case 'open':
            // project_key 现在是硬主键，openSparkProject 优先用它定位，标题/DNA
            // 只作为历史数据的回落（见 app.js findCompletedTaskForSpark）
            await openSparkProject({
                ideaId: (p.library || {}).id || null,
                projectKey: p.kind === 'project' ? p.project_key : '',
                seed: p.theme || '',
                title: p.title || '',
                label: p.title || '该项目',
            });
            break;
        case 'follow':
            switchMainTab('results');
            viewTask(task.id, task.dimensions || {});
            break;
        case 'cancel':
            await cancelTask(task.id, event);
            refreshProjects({ assets: false });
            break;
        case 'rerun':
            await rerunCompletedTask(task.id, task.dimensions || {}, event);
            refreshProjects({ assets: false });
            break;
        case 'retry':
            await retryTask(task.id, task.dimensions || {}, event);
            refreshProjects({ assets: false });
            break;
        case 'delete-task':
            await deleteTask(task.id, event);
            refreshProjects();
            break;
        case 'cancel-job':
            if (!jobId) return;
            await cancelTask(jobId, event);
            refreshProjects({ assets: false });
            break;
        case 'delete-job':
            if (!jobId) return;
            await deleteTask(jobId, event);
            refreshProjects();
            break;
        case 'find-parent':
            // 孤立作业只剩标题/主题可用（母任务记录已被 7 天规则清掉），所以
            // 不传 projectKey——job:<标题> 不是合法 project_key，传进去只会让
            // findCompletedTaskForSpark 的主键直查白跑一趟。
            await openSparkProject({
                seed: p.theme || '',
                title: p.title || '',
                label: p.title || '该作业',
            });
            break;
        case 'cancel-all-jobs': {
            const ids = (p.sub_jobs || []).filter(j => j.id && j.status === 'running').map(j => j.id);
            if (!ids.length) return;
            if (!await projectsConfirm(`确定取消这 ${ids.length} 个运行中的媒体作业吗？`)) return;
            const ok = await projectsBulkJobRequest('/api/compose-cancel', ids);
            showToast(ok === ids.length
                ? `已请求取消 ${ok} 个作业`
                : `${ids.length} 个作业中 ${ok} 个已请求取消，其余失败`,
                ok === ids.length ? 'info' : 'error');
            refreshProjects({ assets: false });
            break;
        }
        case 'delete-all-jobs': {
            const ids = (p.sub_jobs || []).filter(j => j.id && j.status !== 'running').map(j => j.id);
            if (!ids.length) return;
            if (!await projectsConfirm(
                `确定删除这 ${ids.length} 条作业记录吗？只删记录，outputs/ 里已生成的文件不受影响。`)) return;
            const ok = await projectsBulkJobRequest('/api/tasks/delete', ids);
            showToast(ok === ids.length
                ? `已删除 ${ok} 条作业记录`
                : `${ids.length} 条记录中删除了 ${ok} 条，其余失败`,
                ok === ids.length ? 'success' : 'error');
            refreshProjects();
            break;
        }
        case 'gallery-search': {
            switchMainTab('gallery');
            const title = p.title || '';
            // 画廊是懒加载的，等它把搜索框和分组渲染出来再填词；搜索框自带
            // 200ms 去抖，所以要派 input 事件而不是直接改 gallerySearch
            setTimeout(() => {
                const input = document.getElementById('gallery-search');
                if (!input) return;
                input.value = title;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                showToast(`已在画廊里按「${title}」筛选`, 'info');
            }, 600);
            break;
        }
        case 'copy-title': {
            const title = p.title || '';
            try {
                await navigator.clipboard.writeText(title);
                showToast('标题已复制', 'success');
            } catch (e) {
                console.warn('Clipboard write failed', e);
                showToast('复制失败，请手动选中标题', 'error');
            }
            break;
        }
        case 'unsave':
            await deleteFromLibrary((p.library || {}).id);
            refreshProjects();
            break;
        case 'gallery': {
            switchMainTab('gallery');
            const dir = (p.assets || {}).dir || '';
            const groupKey = dir.split('/').pop();
            // 画廊是懒加载的，等它渲染完再定位
            setTimeout(() => {
                const el = document.querySelector(`#gallery-groups .gallery-group[data-group="${CSS.escape(groupKey)}"]`);
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    el.classList.add('flash-highlight');
                    setTimeout(() => el.classList.remove('flash-highlight'), 1600);
                } else {
                    showToast('画廊里没找到这个项目的资产目录（可能已被清理）', 'info');
                }
            }, 600);
            break;
        }
        case 'ledger': {
            switchMainTab('ledger');
            const id = (p.ledger || {}).id;
            setTimeout(() => {
                const tr = document.querySelector(`#ledger-body tr[data-id="${CSS.escape(String(id))}"]`);
                if (tr) {
                    tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    tr.classList.add('flash-highlight');
                    setTimeout(() => tr.classList.remove('flash-highlight'), 1600);
                }
            }, 600);
            break;
        }
        default:
            break;
    }
}

/* ── 初始化 ────────────────────────────────────────────────────────────── */

function initProjects() {
    const container = document.getElementById('projects-list');
    if (!container) return;   // console.html 等页面没有工作台面板

    document.getElementById('projects-filters')?.addEventListener('click', (e) => {
        const chip = e.target.closest('.projects-filter-chip');
        if (!chip) return;
        projectsFilter = chip.dataset.filter || 'all';
        refreshProjects({ assets: false });
    });

    document.getElementById('projects-search')?.addEventListener('input', (e) => {
        projectsSearch = e.target.value || '';
        clearTimeout(projectsSearchDebounce);
        projectsSearchDebounce = setTimeout(() => refreshProjects({ assets: false }), 220);
    });

    document.getElementById('projects-sort')?.addEventListener('change', (e) => {
        projectsSort = e.target.value || 'newest';
        refreshProjects({ assets: false });
    });

    document.getElementById('projects-refresh-btn')?.addEventListener('click', () => {
        projectsRows = null;      // 强制显示加载态，并重新统计资产
        refreshProjects();
    });

    // 行点击 = 选中并展开详情
    container.addEventListener('click', (e) => {
        const row = e.target.closest('.project-row');
        if (!row) return;
        projectsSelectedKey = (projectsSelectedKey === row.dataset.key) ? null : row.dataset.key;
        container.querySelectorAll('.project-row.selected').forEach(el => el.classList.remove('selected'));
        if (projectsSelectedKey) row.classList.add('selected');
        renderProjectDetail();
    });

    // 详情里的动作按钮
    document.getElementById('projects-detail')?.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        const act = btn.dataset.act;
        if (act === 'close-detail') {
            projectsSelectedKey = null;
            document.querySelectorAll('#projects-list .project-row.selected')
                .forEach(el => el.classList.remove('selected'));
            renderProjectDetail();
            return;
        }
        const p = projectsFindRow(projectsSelectedKey);
        if (!p) return;
        projectsRunAction(act, p, e, btn.dataset.jobId || '');
    });
}

// header 的「⚡ 进行中」角标：跳到工作台并预选运行中。旧版是两个抽屉切换按钮
// 各自一套开合状态 + 一路独立的 5s/30s 角标轮询，现在统一成一个入口。
function openProjectsWorkbench(filter) {
    if (filter) projectsFilter = filter;
    const chip = document.querySelector(`#projects-filters .projects-filter-chip[data-filter="${filter || 'all'}"]`);
    if (chip) {
        document.querySelectorAll('#projects-filters .projects-filter-chip')
            .forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
    }
    switchMainTab('projects');
}

// 兼容 defer 加载顺序：DOM 就绪后初始化一次
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initProjects);
} else {
    initProjects();
}
