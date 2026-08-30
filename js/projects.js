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

// 显示方式（列表 / 网格 / 紧凑）。三档只改 #projects-list 上的 view-* 类，行的
// HTML 与数据完全不动——切视图不该重新拉一次 /api/projects，也不该丢掉勾选。
const PROJECTS_VIEW_LS_KEY = 'spark_projects_view';
const PROJECTS_VIEWS = ['list', 'grid', 'compact'];
let projectsView = (() => {
    try {
        const v = localStorage.getItem(PROJECTS_VIEW_LS_KEY);
        return PROJECTS_VIEWS.includes(v) ? v : 'list';
    } catch (e) { return 'list'; }
})();

// 多选。存 project_key，但作用域刻意限定在"当前筛选下可见的行"：批量动作要拿
// task.id / library.id 才能执行，而这些只在已加载的行里有；留着筛掉的行只会让
// "已选 12 项"点下去实际只动了 3 项。每次渲染按可见行收敛（见 renderProjects）。
const projectsSelected = new Set();
let projectsLastClickedKey = null;   // shift 连选的锚点

const PROJECT_STATE_LABELS = {
    running: '运行中', completed: '已完成', saved: '已收藏',
    failed: '已失败', cancelled: '已取消', unknown: '—',
};
const PROJECT_JOB_LABELS = {
    frames: '帧序列', staged_render: '分步渲染', videos: '视频', cover: '封面',
    stepped: '分步管线', stepped_advance: '管线推进',
};
const PROJECT_JOB_ICONS = {
    completed: '✓', failed: '✕', running: '⏳', cancelled: '⚪',
};
// 进度行上的阶段名。项目表只拿得到阶段字符串（没有 details），所以用不了
// progress_model 那套带参数的文案，这里给一份纯静态的中文映射；查不到的阶段
// 直接原样显示，总比把 batch_generating 这种内部名摆在用户面前强。
const PROJECT_STAGE_LABELS = {
    outline: '生成大纲', batch: '批量合成', batch_generating: '批量合成中',
    batch_generated: '批次完成', batch_retry: '批次重试', batch_failed: '批次失败',
    repair: '修复中', audit: '质量审计', compose: '合成提示词',
    frames: '生成帧序列', staged_render: '分步渲染', videos: '生成视频', cover: '生成封面',
    completed: '已完成', cancelled: '已取消',
    // 复刻线的 stage 不在这里抄第三份——真源是 replica_pipeline.py 的 STAGE_LABELS，
    // 由 js/replica_pipeline.js 的 REPLICA_STAGE_LABELS 兜底（它在本文件之前加载）。
    // 抄一份的代价很具体：新增一个 stage 要记得改三处，漏掉这处就在工作台上露出
    // `confirm_cost` 这种内部名。
};
function projectsStageLabel(stage) {
    if (!stage) return '准备中…';
    const replica = (typeof REPLICA_STAGE_LABELS !== 'undefined' && REPLICA_STAGE_LABELS) || {};
    return PROJECT_STAGE_LABELS[stage] || replica[stage] || stage;
}

/* ── 显示方式 ──────────────────────────────────────────────────────────── */

function projectsApplyView() {
    const container = document.getElementById('projects-list');
    if (container) {
        PROJECTS_VIEWS.forEach(v => container.classList.toggle(`view-${v}`, v === projectsView));
    }
    document.querySelectorAll('#projects-view-switch .projects-view-btn').forEach(btn => {
        const on = btn.dataset.view === projectsView;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
}

function projectsSetView(view) {
    if (!PROJECTS_VIEWS.includes(view) || view === projectsView) return;
    projectsView = view;
    try { localStorage.setItem(PROJECTS_VIEW_LS_KEY, view); }
    catch (e) { /* 存储满/隐私模式：视图不持久化也能用 */ }
    projectsApplyView();
}

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
    return badges.join('');
}

function projectsAggregateJobs(p, maybeJobs) {
    let project = {};
    let subJobs = [];
    if (Array.isArray(p)) {
        subJobs = p;
        project = maybeJobs || {};
    } else if (p && typeof p === 'object') {
        project = p;
        subJobs = maybeJobs || p.sub_jobs || [];
    } else {
        return [];
    }
    if (!Array.isArray(subJobs) || !subJobs.length) return [];

    const order = ['frames', 'staged_render', 'stepped', 'stepped_advance', 'videos', 'cover'];
    const groups = new Map();

    subJobs.forEach(job => {
        if (!job) return;
        const type = job.type || 'unknown';
        if (!groups.has(type)) {
            groups.set(type, {
                type,
                total: 0,
                completed: 0,
                failed: 0,
                running: 0,
                cancelled: 0,
                unknown: 0,
            });
        }
        const g = groups.get(type);
        g.total++;
        const st = job.status || 'unknown';
        if (st === 'completed') g.completed++;
        else if (st === 'failed') g.failed++;
        else if (st === 'running') g.running++;
        else if (st === 'cancelled') g.cancelled++;
        else g.unknown++;
    });

    const result = Array.from(groups.values());
    result.sort((a, b) => {
        const ia = order.indexOf(a.type);
        const ib = order.indexOf(b.type);
        if (ia !== -1 && ib !== -1) return ia - ib;
        if (ia !== -1) return -1;
        if (ib !== -1) return 1;
        return a.type.localeCompare(b.type);
    });

    const hasMediaFrames = (project.image_count > 0)
        || (project.library && project.library.frame_count > 0)
        || (project.assets && project.assets.file_count > 0 && (project.cover || project.assets.cover));
    const hasMediaVideos = (project.video_count > 0);
    const hasMediaCover = !!project.cover || (project.assets && !!project.assets.cover);

    return result.map(g => {
        const typeLabel = PROJECT_JOB_LABELS[g.type] || g.type;
        let statusClass = 'completed';
        let icon = '✓';
        let label = '';
        let title = '';

        const typeHasMedia = (g.type === 'frames' || g.type === 'staged_render' || g.type === 'stepped' || g.type === 'stepped_advance') ? hasMediaFrames
                           : (g.type === 'videos') ? hasMediaVideos
                           : (g.type === 'cover') ? hasMediaCover
                           : (project.saved || project.state === 'completed');

        if (g.running > 0) {
            statusClass = 'running';
            icon = '⏳';
            label = `${typeLabel}生成中`;
            title = `${typeLabel}: ${g.running} 任务进行中${g.completed ? `, ${g.completed} 已完成` : ''}`;
        } else if (g.completed > 0 || typeHasMedia) {
            // 如果该类型有已完成任务，或者项目自身已有该媒体资产
            statusClass = 'completed';
            icon = '✓';
            if ((g.type === 'frames' || g.type === 'staged_render') && (project.image_count || (project.library && project.library.frame_count))) {
                const count = project.image_count || (project.library && project.library.frame_count);
                label = `${count}帧序列`;
            } else if (g.type === 'videos' && project.video_count) {
                label = `${project.video_count}镜视频`;
            } else {
                label = typeLabel;
            }
            if (g.failed > 0) {
                title = `${typeLabel}: 媒体已就绪（含 ${g.failed} 条历史重试/失败记录）`;
            } else {
                title = `${typeLabel}: 已就绪`;
            }
        } else if (g.failed > 0) {
            statusClass = 'failed';
            icon = '✕';
            label = `${typeLabel}失败`;
            title = `${typeLabel}: 生成失败（${g.failed} 次尝试未成功）`;
        } else if (g.cancelled === g.total) {
            statusClass = 'cancelled';
            icon = '⚪';
            label = `${typeLabel}已取消`;
            title = `${typeLabel}: 全部已取消`;
        } else {
            statusClass = 'completed';
            icon = '✓';
            label = typeLabel;
            title = `${typeLabel}: 已就绪`;
        }

        return {
            type: g.type,
            statusClass,
            icon,
            label,
            title,
            stats: g,
        };
    });
}

function projectsJobsHtml(p) {
    const agg = projectsAggregateJobs(p);
    if (!agg.length) return '';
    return `<div class="project-jobs">${agg.map(j => `
        <span class="project-job ${escapeHtml(j.statusClass)}" title="${escapeHtml(j.title)}">
            ${escapeHtml(j.icon)} ${escapeHtml(j.label)}
        </span>`).join('')}</div>`;
}

function projectsMetaHtml(p) {
    const bits = [];
    if (p.video_count) bits.push(`${p.video_count} 镜`);
    else if (p.image_count) bits.push(`${p.image_count} 帧`);
    else if (p.library && p.library.frame_count) bits.push(`${p.library.frame_count} 帧`);
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
        ? `<div class="project-progress"><span class="project-progress-stage" title="${escapeHtml(task.stage || '')}">${escapeHtml(projectsStageLabel(task.stage))}</span><span class="project-spinner"></span></div>`
        : '';
    const error = (p.state === 'failed' && task.error)
        ? `<div class="project-error" title="${escapeHtml(task.error)}">❌ ${escapeHtml(task.error)}</div>` : '';
    const badgesHtml = projectsBadgesHtml(p);

    const actionBtns = [];
    if (p.saved || task.status === 'completed') {
        actionBtns.push('<button type="button" class="project-card-btn primary" data-act="open" title="打开项目">🎬 打开</button>');
    }
    if (task.status === 'running') {
        actionBtns.push('<button type="button" class="project-card-btn primary" data-act="follow" title="跟进实时输出">👁 跟进</button>');
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        actionBtns.push('<button type="button" class="project-card-btn" data-act="retry" title="重试">🔁 重试</button>');
    }
    if (p.assets && p.assets.file_count) {
        actionBtns.push('<button type="button" class="project-card-btn" data-act="gallery" title="去画廊看资产">🖼️ 资产</button>');
    }
    const actionsOverlay = actionBtns.length
        ? `<div class="project-card-actions">${actionBtns.join('')}</div>`
        : '';

    // 勾选态不进 innerHTML（也不进 projectsRowSignature）：选中/取消是每次点击都
    // 发生的高频操作，重建整行 DOM 只为翻转一个 checked 太贵，而且会打断 hover。
    // 由 renderProjects 在行就位后直接改 .checked（见下）。
    return `
        <label class="project-check" title="选择（Shift 点击连选）"><input type="checkbox" class="p-check"></label>
        <div class="project-thumb">
            ${projectsCoverHtml(p)}
            <div class="project-thumb-overlay">
                <div class="project-thumb-badges">${badgesHtml}</div>
                ${actionsOverlay}
            </div>
        </div>
        <div class="project-main">
            <div class="project-title-line">
                <span class="project-title" title="${escapeHtml(p.title || '未命名项目')}">${escapeHtml(p.title || '未命名项目')}</span>
                <span class="project-badges-inline">${badgesHtml}</span>
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
        projectsSelected.clear();
        renderProjectsBulkBar();
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
        const checked = projectsSelected.has(key);
        row.classList.toggle('multi-selected', checked);
        const cb = row.querySelector('.p-check');
        if (cb) cb.checked = checked;
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
    // 勾选收敛到可见行：筛选切换/项目被删后，留在集合里的 key 已经取不到行，
    // 批量动作对它们无从下手，计数却还在涨。
    [...projectsSelected].forEach(k => { if (!seen.has(k)) projectsSelected.delete(k); });
    if (projectsLastClickedKey && !seen.has(projectsLastClickedKey)) projectsLastClickedKey = null;
    renderProjectsBulkBar();
    renderProjectDetail();
}

/* ── 多选与批量动作 ────────────────────────────────────────────────────── */

function projectsSelectedRows() {
    return (projectsRows || []).filter(p => projectsSelected.has(p.project_key));
}

function projectsSetChecked(key, on) {
    if (on) projectsSelected.add(key); else projectsSelected.delete(key);
    const row = document.querySelector(`#projects-list .project-row[data-key="${CSS.escape(key)}"]`);
    if (row) {
        row.classList.toggle('multi-selected', on);
        const cb = row.querySelector('.p-check');
        if (cb) cb.checked = on;
    }
    renderProjectsBulkBar();
}

// Shift 连选：以上一次点过的行为锚点，把两者之间的可见行统一设成本次的目标态。
function projectsSelectRange(fromKey, toKey, on) {
    const keys = (projectsRows || []).map(p => p.project_key);
    const a = keys.indexOf(fromKey);
    const b = keys.indexOf(toKey);
    if (a === -1 || b === -1) return;
    keys.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(k => projectsSetChecked(k, on));
}

function projectsToggleSelectAll() {
    const keys = (projectsRows || []).map(p => p.project_key);
    if (!keys.length) return;
    const allOn = keys.every(k => projectsSelected.has(k));
    keys.forEach(k => projectsSetChecked(k, !allOn));
}

// 批量条的按钮按"这批选中的行里有多少条真的能执行"来给：选了 5 个但只有 2 个
// 在跑，「取消运行中」就写 (2)；一条都不适用时按钮根本不出现，免得点下去空转。
function renderProjectsBulkBar() {
    const bar = document.getElementById('projects-bulkbar');
    if (!bar) return;
    const rows = projectsSelectedRows();
    if (!rows.length) {
        bar.hidden = true;
        bar.innerHTML = '';
        document.getElementById('projects-list')?.classList.remove('has-selection');
        projectsSyncSelectAllBtn();
        return;
    }
    bar.hidden = false;
    document.getElementById('projects-list')?.classList.add('has-selection');

    const running = rows.filter(p => (p.task || {}).status === 'running').length;
    const withTask = rows.filter(p => (p.task || {}).id || (p.sub_jobs || []).length > 0).length;
    const btns = [];
    if (running) btns.push(`<button type="button" class="projects-btn danger" data-bulk="cancel">✕ 取消运行中（${running}）</button>`);
    btns.push('<button type="button" class="projects-btn" data-bulk="copy-titles">📋 复制标题</button>');
    // 核心主操作：彻底删除所选项目（包含任务记录、点子库收藏及本地磁盘媒体）
    btns.push(`<button type="button" class="projects-btn danger" data-bulk="delete-projects" title="彻底删除所选项目：同步清除生成任务记录、点子库收藏及本地磁盘生成的图片/视频文件">🗑️ 彻底删除（${rows.length}）</button>`);
    if (withTask) btns.push(`<button type="button" class="projects-btn" data-bulk="delete-task" title="只删除生成任务记录与日志，已收藏的创意与磁盘素材不受影响">🧹 仅清除任务记录（${withTask}）</button>`);

    bar.innerHTML = `
        <span class="projects-bulk-count">已选 ${rows.length} 个项目</span>
        <div class="projects-bulk-actions">${btns.join('')}</div>
        <button type="button" class="projects-btn" data-bulk="clear">取消选择</button>`;
    projectsSyncSelectAllBtn();
}

function projectsSyncSelectAllBtn() {
    const btn = document.getElementById('projects-select-all-btn');
    if (!btn) return;
    const keys = (projectsRows || []).map(p => p.project_key);
    const allOn = keys.length > 0 && keys.every(k => projectsSelected.has(k));
    btn.textContent = allOn ? '⬜ 取消全选' : '☑️ 全选';
    btn.disabled = keys.length === 0;
}

// 批量项目彻底删除：调用 /api/projects/delete 一次性清理任务记录、点子库收藏及 outputs 媒体
async function projectsBulkDeleteProjects(rows) {
    if (!rows || !rows.length) return 0;
    try {
        const res = await fetch('/api/projects/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projects: rows }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.status === 'ok') {
            const deletedLibIds = new Set(data.deleted_library_ids || []);
            rows.forEach(p => {
                if (p.library && p.library.id) deletedLibIds.add(p.library.id);
                if (p.saved && p.id) deletedLibIds.add(p.id);
            });
            if (deletedLibIds.size && typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas)) {
                savedIdeas = savedIdeas.filter(i => !deletedLibIds.has(i.id));
                try { localStorage.setItem('spark_library', JSON.stringify(savedIdeas)); }
                catch (e) { console.warn('[library] localStorage 镜像写入失败', e); }
                if (typeof updateFavoriteButtonState === 'function') updateFavoriteButtonState();
            }
            return data.count || rows.length;
        }
    } catch (e) {
        console.warn('Bulk projects delete API failed, falling back to sequential', e);
    }

    // 失败回退：同时执行 unsave 和 delete-task
    const unsaved = await projectsBulkUnsave(rows);
    const taskIds = [];
    rows.forEach(p => {
        if ((p.task || {}).id) taskIds.push(p.task.id);
        (p.sub_jobs || []).forEach(j => { if (j && j.id) taskIds.push(j.id); });
    });
    if (taskIds.length) {
        await projectsBulkJobRequest('/api/tasks/delete', taskIds);
    }
    return rows.length;
}

// 批量收藏删除：优先单次批量端点（/api/library/items/bulk_delete），大幅降低网络耗时与避免部分删除。
async function projectsBulkUnsave(rows) {
    const targets = rows.filter(p => p.saved && (p.library || {}).id);
    const targetIds = targets.map(p => p.library.id).filter(Boolean);
    if (!targetIds.length) return 0;

    const removed = new Set();
    try {
        const res = await fetch('/api/library/items/bulk_delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: targetIds }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.status === 'ok') {
            if (Array.isArray(data.items)) {
                data.items.filter(x => x.removed).forEach(x => removed.add(x.id));
            } else {
                targetIds.forEach(id => removed.add(id));
            }
        }
    } catch (e) {
        console.warn('Bulk unsave batch request failed, falling back to sequential', e);
    }

    // 失败时回落到单条删除兼容
    if (!removed.size) {
        for (const p of targets) {
            const id = p.library.id;
            const idea = (typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas))
                ? savedIdeas.find(i => i.id === id) : null;
            try {
                const res = await fetch('/api/library/item/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id,
                        title: idea && typeof getIdeaSaveTitle === 'function'
                            ? getIdeaSaveTitle(idea)
                            : (p.project_key || p.title || ''),
                        covers: (idea && idea.covers) || [],
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.status === 'ok') removed.add(id);
            } catch (e) {
                console.error('Fallback single unsave failed', id, e);
            }
        }
    }

    if (removed.size && typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas)) {
        savedIdeas = savedIdeas.filter(i => !removed.has(i.id));
        try { localStorage.setItem('spark_library', JSON.stringify(savedIdeas)); }
        catch (e) { console.warn('[library] localStorage 镜像写入失败', e); }
        if (typeof updateFavoriteButtonState === 'function') updateFavoriteButtonState();
    }
    return removed.size;
}

async function projectsRunBulkAction(act) {
    const rows = projectsSelectedRows();
    if (!rows.length) return;

    switch (act) {
        case 'clear':
            projectsSelected.clear();
            document.querySelectorAll('#projects-list .project-row.multi-selected').forEach(el => {
                el.classList.remove('multi-selected');
                const cb = el.querySelector('.p-check');
                if (cb) cb.checked = false;
            });
            renderProjectsBulkBar();
            break;

        case 'copy-titles': {
            const text = rows.map(p => p.title || '未命名项目').join('\n');
            try {
                await navigator.clipboard.writeText(text);
                showToast(`已复制 ${rows.length} 个标题`, 'success');
            } catch (e) {
                console.warn('Clipboard write failed', e);
                showToast('复制失败，请手动选中标题', 'error');
            }
            break;
        }

        case 'cancel': {
            const ids = rows.filter(p => (p.task || {}).status === 'running' && p.task.id).map(p => p.task.id);
            if (!ids.length) return;
            if (!await projectsConfirm(`确定取消这 ${ids.length} 个运行中的项目吗？`)) return;
            const ok = await projectsBulkJobRequest('/api/compose-cancel', ids);
            showToast(ok === ids.length
                ? `已请求取消 ${ok} 个项目`
                : `${ids.length} 个项目中 ${ok} 个已请求取消，其余失败`,
                ok === ids.length ? 'info' : 'error');
            refreshProjects({ assets: false });
            break;
        }

        case 'delete-projects': {
            if (!await projectsConfirm(
                `确定彻底删除这 ${rows.length} 个项目吗？\n⚠ 将同步清除任务记录、点子库收藏以及本地磁盘生成的图片/视频文件，不可恢复。`)) return;
            const ok = await projectsBulkDeleteProjects(rows);
            showToast(ok ? `已彻底删除 ${ok} 个项目及对应磁盘素材` : '删除失败，请稍后重试', ok ? 'success' : 'error');
            projectsSelected.clear();
            refreshProjects();
            break;
        }

        case 'delete-task': {
            const ids = rows.filter(p => (p.task || {}).id).map(p => p.task.id);
            if (!ids.length) return;
            if (!await projectsConfirm(
                `确定清除这 ${ids.length} 条任务记录吗？（仅清理生成记录与日志，已收藏的创意与本地磁盘素材文件不受影响）`)) return;
            const ok = await projectsBulkJobRequest('/api/tasks/delete', ids);
            showToast(ok === ids.length
                ? `已清除 ${ok} 条任务记录`
                : `${ids.length} 条记录中清除了 ${ok} 条，其余失败`,
                ok === ids.length ? 'success' : 'error');
            projectsSelected.clear();
            refreshProjects({ assets: false });
            break;
        }

        case 'unsave': {
            const targets = rows.filter(p => p.saved && (p.library || {}).id);
            if (!targets.length) return;
            if (!await projectsConfirm(
                `确定从点子库彻底删除这 ${targets.length} 个创意吗？\n⚠ 对应在 outputs/ 目录已生成的图片与成片文件会一并彻底清除，不可恢复。`)) return;
            const ok = await projectsBulkUnsave(targets);
            showToast(ok === targets.length
                ? `已从点子库彻底删除 ${ok} 个创意及对应磁盘素材`
                : `${targets.length} 个创意中删除了 ${ok} 个，其余失败`,
                ok === targets.length ? 'success' : 'error');
            projectsSelected.clear();
            refreshProjects();
            break;
        }

        default:
            break;
    }
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
    // 二创只对**复刻来的**项目露出：它去的是爆款复刻面板的「二创变体」栏，那一栏
    // 是沿变异轴改写既有节拍阶梯，没有阶梯就没有可改的东西。激发出来的项目不在这
    // 条线上，给它按钮等于给一个必然落空的入口。
    if (projectReplicaJobId(p)) {
        btns.push('<button type="button" class="projects-btn" data-act="remix" title="到爆款复刻的「二创变体」栏，沿变异轴从本项目的节拍阶梯派生衍生方案">♻️ 二创</button>');
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
    // 核心主删除操作：彻底删除该项目（同时清理任务记录、点子库收藏及本地媒体文件）
    btns.push('<button type="button" class="projects-btn danger" data-act="delete-project" title="彻底删除该项目：同步清除生成任务记录、点子库收藏及本地磁盘生成的图片/视频文件">🗑️ 彻底删除项目</button>');
    if (task.id && p.saved) {
        btns.push('<button type="button" class="projects-btn danger" data-act="delete-task" title="只删除生成任务记录与日志，已收藏的创意与本地磁盘素材文件不受影响">🧹 仅清除任务记录</button>');
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

// 批量作业操作：优先使用批量端点，单次网络请求完成；失败时回落到逐条重试。
async function projectsBulkJobRequest(url, ids) {
    const validIds = (ids || []).filter(Boolean);
    if (!validIds.length) return 0;

    // 优先单次批量请求
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_ids: validIds, task_id: validIds[0] }),
        });
        if (res.ok) {
            const data = await res.json().catch(() => ({}));
            if (typeof data.count === 'number') return data.count;
            return validIds.length;
        }
    } catch (e) {
        console.warn('Batch job request failed, falling back to sequential', url, e);
    }

    // 回落到逐条发送
    let ok = 0;
    for (const id of validIds) {
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: id }),
            });
            if (res.ok) ok++;
        } catch (e) {
            console.error('Bulk job request fallback failed', url, id, e);
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
        case 'remix':
            await startProjectRemix(p);
            break;
        case 'delete-project': {
            const title = p.title || p.project_key || '未命名项目';
            if (!await projectsConfirm(
                `确定彻底删除项目「${title}」吗？\n⚠ 将同步清除任务记录、点子库收藏以及本地磁盘已生成的图片/视频文件，不可恢复。`)) return;
            const ok = await projectsBulkDeleteProjects([p]);
            showToast(ok ? `项目「${title}」及关联资产已彻底删除` : '删除失败，请稍后重试', ok ? 'success' : 'error');
            if (projectsSelectedKey === p.project_key) {
                projectsSelectedKey = null;
            }
            refreshProjects();
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
        default:
            break;
    }
}

// 这个项目对应哪条爆款复刻作业。两个来源，都不用后端改字段：
//   1. 复刻产出入库时 library item 的 id 直接写的就是 job_id（replica_pipeline.py
//      _library_item：「job_id 本身就以 replica_ 开头，别再套一层」）；
//   2. 复刻相关的任务在 dimensions 里带 replica_job_id（server_common._replica_job_of）。
// 取不到就说明这个项目不是复刻线上的，二创无从谈起 —— 调用方据此决定露不露按钮。
function projectReplicaJobId(p) {
    if (!p) return '';
    const libId = String((p.library || {}).id || '');
    if (/^replica/.test(libId)) return libId;
    const dims = ((p.task || {}).dimensions) || {};
    return String(dims.replica_job_id || '').trim();
}

// 二创：把该项目对应的复刻作业在「爆款复刻」面板里打开，并定位到「二创变体」栏。
//
// 这里刻意**不**走 /api/ideate 的 remix_seed 那条路：那条路的落点是激发维度页的灵感
// 卡网格，那一页已经下线，调用它只会静默返回、再配一个"正在激发…"的假提示。复刻面板
// 的二创变体是现存唯一活着的二创机制（沿变异轴改写节拍阶梯，见 replicaMutateOrthogonal）。
async function startProjectRemix(p) {
    const jobId = projectReplicaJobId(p);
    const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };

    if (!jobId) {
        toast('这个项目不是从爆款复刻来的，没有可改写的节拍阶梯', 'error');
        return;
    }
    if (typeof switchMainTab !== 'function' || typeof replicaLoadJob !== 'function') {
        toast('复刻工作台尚未加载完成，请稍后重试', 'error');
        return;
    }

    let state;
    try {
        // 取数必须排在切页**之前**：switchMainTab 会触发 replicaTabEntered，它在
        // replicaState 为空时会自作主张打开作业列表里的第一条。先把 state 落定，
        // 那个兜底分支就不会跟我们抢；否则两个请求赛跑，用户可能落在别的作业上。
        state = await replicaLoadJob(jobId);
    } catch (e) {
        toast(`打开复刻作业失败：${e.message}`, 'error');
        return;
    }

    switchMainTab('replica');

    // 节拍还没反推出来就没有「二创变体」栏可去（该栏与导航项都以 beats 非空为条件
    // 渲染）。这时候滚到当前作业本身，并说清楚下一步该干什么。
    const beats = ((state || {}).beats || {}).beats || [];
    if (!beats.length) toast('该复刻作业还没有节拍阶梯，先跑完反推再做二创', 'info');

    // 与上面「去画廊看资产」同一套：复刻面板由 replicaTabEntered 异步重渲一次，
    // 立刻定位会被那次重渲把滚动位置顶回顶部。
    setTimeout(() => {
        if (typeof replicaFocusSection !== 'function') return;
        // 逐个回落：每一栏都有自己的渲染条件（二创发散要 beats 非空），
        // 条件不满足时那个锚点根本不存在，而 replicaFocusSection 找不到锚点是
        // 彻底静默的——不滚动、不报错。所以按可能性从窄到宽试，别只赌一个。
        const targets = beats.length
            ? ['replica-sec-variant', 'replica-sec-current-job']
            : ['replica-sec-current-job', 'replica-sec-jobs'];
        targets.some(id => replicaFocusSection(id));
    }, 600);
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

    projectsApplyView();
    document.getElementById('projects-view-switch')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.projects-view-btn');
        if (btn) projectsSetView(btn.dataset.view);
    });

    document.getElementById('projects-select-all-btn')?.addEventListener('click', projectsToggleSelectAll);

    document.getElementById('projects-bulkbar')?.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-bulk]');
        if (btn) projectsRunBulkAction(btn.dataset.bulk);
    });

    // 行点击 = 快捷动作 / 勾选 / 选中并展开详情
    container.addEventListener('click', (e) => {
        const row = e.target.closest('.project-row');
        if (!row) return;
        const key = row.dataset.key;

        // 快捷操作按钮（卡片上的打开/跟进/重试/资产等）
        const actBtn = e.target.closest('[data-act]');
        if (actBtn) {
            e.stopPropagation();
            const act = actBtn.dataset.act;
            const p = projectsFindRow(key);
            if (p) projectsRunAction(act, p, e, actBtn.dataset.jobId || '');
            return;
        }

        // 勾选框走多选，不动详情 pane：勾 8 行做批量删除时不该顺带把详情翻 8 次
        if (e.target.closest('.project-check')) {
            const cb = row.querySelector('.p-check');
            // label 包 checkbox，点击已由浏览器翻转过，以最终状态为准
            const on = cb ? cb.checked : !projectsSelected.has(key);
            if (e.shiftKey && projectsLastClickedKey && projectsLastClickedKey !== key) {
                projectsSelectRange(projectsLastClickedKey, key, on);
            } else {
                projectsSetChecked(key, on);
            }
            projectsLastClickedKey = key;
            return;
        }

        projectsSelectedKey = (projectsSelectedKey === key) ? null : key;
        container.querySelectorAll('.project-row.selected').forEach(el => el.classList.remove('selected'));
        if (projectsSelectedKey) row.classList.add('selected');
        renderProjectDetail();
    });

    // 双击卡片快速打开项目
    container.addEventListener('dblclick', (e) => {
        const row = e.target.closest('.project-row');
        if (!row || e.target.closest('.project-check') || e.target.closest('[data-act]')) return;
        const key = row.dataset.key;
        const p = projectsFindRow(key);
        if (!p) return;
        if (p.saved || (p.task && p.task.status === 'completed')) {
            projectsRunAction('open', p, e);
        } else if (p.task && p.task.status === 'running') {
            projectsRunAction('follow', p, e);
        }
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
