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
   见 docs/plans/project_workbench_refactor_plan.md
   ========================================================================== */

let projectsRows = null;           // 当前页的项目行（服务端已筛选/排序）
let projectsCounts = {};           // chips 角标（在完整表上统计，不受筛选影响）
let projectsTotal = 0;
let projectsSection = 'active';    // active | archived：制作中的项目和归档独立分区
let projectsFilter = 'all';        // 活动项目的状态筛选：all | running | completed | saved | failed
let projectsSearch = '';
let projectsSort = 'newest';       // newest | oldest | title
let projectsSelectedKey = null;    // 详情 pane 当前选中的 project_key
let projectsLoading = false;
let projectsPollTimer = null;
let projectsTabActive = false;
let projectsSearchDebounce = null;
let projectsArchiving = false;
let projectsRefreshRevision = 0;   // 归档后拒绝此前发出的旧素材响应
let projectsPendingArchivedKey = null;

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
    failed: '已失败', cancelled: '已取消', ready: '待继续', partial: '部分完成', archived: '已归档', archive_pending: '归档待完成', unknown: '—',
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
};
function projectsStageLabel(stage) {
    if (!stage) return '准备中…';
    return PROJECT_STAGE_LABELS[stage] || stage;
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

/* ── 项目 / 项目归档分区 ───────────────────────────────────────────────── */

function projectsIsArchived(p) {
    return Boolean(p.archived || p.archive_pending || p.state === 'archived');
}

function projectsUpdateSectionUi() {
    document.querySelectorAll('#projects-sections .projects-section-btn').forEach(btn => {
        if (!btn.dataset.label) btn.dataset.label = btn.textContent.trim();
        const count = btn.dataset.section === 'archived' ? projectsCounts.archived
            : projectsCounts.active ?? (projectsCounts.all !== undefined && projectsCounts.archived !== undefined
                ? Math.max(0, projectsCounts.all - projectsCounts.archived) : undefined);
        btn.textContent = count !== undefined ? `${btn.dataset.label} (${count})` : btn.dataset.label;
        const active = btn.dataset.section === projectsSection;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    const archived = projectsSection === 'archived';
    const filters = document.getElementById('projects-filters');
    if (filters) filters.hidden = archived;
    const hint = document.getElementById('projects-section-hint');
    if (hint) hint.textContent = archived
        ? '集中查看归档成片、节拍数据与提示词，继续完成待清理的归档'
        : '查看生成进度，继续制作或下载成片';
    const search = document.getElementById('projects-search');
    if (search) {
        search.placeholder = archived ? '搜索归档项目名称' : '搜索项目名称';
        search.setAttribute('aria-label', archived ? '搜索归档项目' : '搜索项目');
    }
}

function projectsApplySection(section, { clearSearch = false } = {}) {
    const next = section === 'archived' ? 'archived' : 'active';
    if (projectsSection !== next) {
        projectsSection = next;
        projectsFilter = 'all';
        projectsSelectedKey = null;
        projectsSelected.clear();
        projectsLastClickedKey = null;
        projectsPendingArchivedKey = null;
        if (projectsRows) projectsRows = projectsRows.filter(p => projectsIsArchived(p) === (next === 'archived'));
        projectsTotal = next === 'archived' ? projectsCounts.archived ?? 0
            : projectsCounts.active ?? Math.max(0, (projectsCounts.all || 0) - (projectsCounts.archived || 0));
        projectsRefreshRevision++;
    }
    if (clearSearch) {
        projectsSearch = '';
        const search = document.getElementById('projects-search');
        if (search) search.value = '';
    }
    projectsUpdateSectionUi();
}

async function projectsSetSection(section) {
    if (section === projectsSection) return;
    projectsApplySection(section);
    renderProjects();
    await refreshProjects();
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
    const hasRunning = (projectsRows || []).some(projectsIsRunning);
    projectsPollTimer = setTimeout(() => {
        if (!projectsTabActive) return;
        refreshProjects({ assets: false, silent: true }).finally(projectsSchedulePoll);
    }, hasRunning ? 4000 : 30000);
}

async function refreshProjects(options = {}) {
    const { assets = true, silent = false } = options;
    if (projectsLoading) return;
    projectsLoading = true;
    const revision = projectsRefreshRevision;

    const container = document.getElementById('projects-list');
    if (container && !projectsRows && !silent) {
        container.innerHTML = '<div class="projects-status">📡 正在汇总项目…</div>';
    }
    try {
        const params = new URLSearchParams({
            scope: projectsSection,
            state: projectsSection === 'archived' ? 'archived' : projectsFilter,
            q: projectsSearch,
            sort: projectsSort,
            limit: '200',
        });
        if (!assets) params.set('assets', '0');
        const res = await fetch(`/api/projects?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data && data.error) throw new Error(data.error);
        if (revision !== projectsRefreshRevision) {
            projectsLoading = false;
            return refreshProjects();
        }
        // 执行响应丢失、或另一个窗口归档后，以服务端的归档状态清理本窗口旧媒体。
        const previousRows = projectsRows || [];
        const oldRows = new Map(previousRows.map(p => [p.project_key, p]));
        const newlyArchived = (data.projects || []).filter(p => p.archived && oldRows.has(p.project_key)
            && !oldRows.get(p.project_key).archived);
        if (newlyArchived.length) projectsApplyArchiveResults(newlyArchived, previousRows, []);

        // assets=0 的轮询回来的行没有资产统计。直接覆盖会让"19 个文件"在两次
        // 轮询之间闪成 0，所以只在这次确实带了资产时才接受新值。
        if (!assets && Array.isArray(projectsRows)) {
            const prev = new Map(projectsRows.map(p => [p.project_key, p]));
            (data.projects || []).forEach(p => {
                const old = prev.get(p.project_key);
                if (!old || p.archived || old.archived) return;
                if (!p.assets) p.assets = old.assets;
                // 轻量轮询（assets=0）不会扫描 outputs，因此未收藏项目的磁盘封面
                // 不会出现在响应里。保留上一次完整刷新拿到的封面，避免每次轮询后
                // 缩略图从真实图片退化成灯泡占位图。
                if (!p.cover && old.cover) p.cover = old.cover;
            });
        }
        // 同时防御旧版服务端：所有活动状态筛选都不能混入归档项目。
        projectsRows = (data.projects || []).filter(p => projectsIsArchived(p) === (projectsSection === 'archived'));
        if (projectsPendingArchivedKey && projectsRows.some(p => p.project_key === projectsPendingArchivedKey && p.archived)) {
            projectsSelectedKey = projectsPendingArchivedKey;
            projectsPendingArchivedKey = null;
        }
        projectsCounts = data.counts || {};
        projectsTotal = projectsSection === 'archived'
            ? projectsCounts.archived ?? data.filtered_count ?? projectsRows.length
            : projectsCounts.active ?? Math.max(0, (data.total_count || 0) - (projectsCounts.archived || 0));
    } catch (e) {
        projectsLoading = false;
        if (revision !== projectsRefreshRevision) return refreshProjects();
        console.error('Failed to load projects', e);
        if (container && !silent) {
            container.innerHTML = `<div class="projects-status error">项目列表加载失败：${escapeHtml(e.message)}</div>`;
        }
        return;
    }
    projectsLoading = false;
    renderProjects();
    projectsSchedulePoll();
}

function projectsFindRow(key) {
    return (projectsRows || []).find(p => p.project_key === key) || null;
}

function projectsIsRunning(p) {
    return p.state === 'running' || (p.task || {}).status === 'running'
        || (p.sub_jobs || []).some(j => j.status === 'running')
        || ['queued', 'running'].includes(p.video_edit?.status);
}

function projectsCanArchive(p) {
    return Boolean(p.project_key && p.kind !== 'job' && (!p.archived || p.archive_pending) && !projectsIsRunning(p));
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
    if (img) img.outerHTML = `<div class="project-thumb-icon">${img.dataset?.placeholder === 'archive' ? '📦' : '💡'}</div>`;
}

function projectsArchiveCoverUrl(p) {
    const archive = p.archive || {};
    const files = Array.isArray(archive.retained_files) ? archive.retained_files : [];
    return [archive.cover_url, ...files.filter(file => file && file.kind === 'cover').map(file => file.url)]
        .find(projectsSafeArchiveUrl) || null;
}

function projectsCoverHtml(p) {
    const archived = projectsIsArchived(p);
    const candidates = (archived ? [projectsArchiveCoverUrl(p)] : [p.cover, p.assets && p.assets.cover])
        .filter(projectsSafeCoverUrl)
        .filter((url, index, all) => all.indexOf(url) === index);
    if (!candidates.length) return `<div class="project-thumb-icon">${archived ? '📦' : '💡'}</div>`;
    const fallback = candidates[1]
        ? ` data-fallback="${escapeHtml(candidates[1])}"`
        : '';
    return `<img src="${escapeHtml(candidates[0])}"${fallback}${archived ? ' data-placeholder="archive"' : ''} alt="" loading="lazy"
                 onerror="projectsHandleCoverError(this)">`;
}

const PROJECT_EDIT_STAGE_LABELS = {
    queued: '等待开始', preparing: '准备原片', reviewing_source: '审阅原片',
    rendering: '导出精剪视频', reviewing_output: '复核剪切边界', verifying: '校验成片和音轨',
    cancelling: '正在停止精剪',
};

function projectsEditElapsed(edit) {
    const epoch = value => {
        const number = Number(value);
        return Number.isFinite(number) && number > 0 ? number < 1e12 ? number * 1000 : number : Date.parse(value) || 0;
    };
    const start = epoch(edit.created_at);
    const active = ['queued', 'running'].includes(edit.status);
    const end = active ? Date.now() : epoch(edit.finished_at || edit.updated_at);
    if (!start || !end || end < start) return '';
    const seconds = Math.floor((end - start) / 1000);
    const duration = seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
        : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
    return `${active ? edit.status === 'queued' ? '已等待' : '已运行' : '耗时'} ${duration}`;
}

function projectsCurrentStatus(p) {
    if (p.archive_pending) return { label: '归档待完成', state: 'archive_pending',
        next: '保留文件已保存，点击完成归档继续清理剩余内容', progress: '', running: false };
    if (p.archived) return { label: '已归档', state: 'archived',
        next: projectsArchiveVideos(p).length ? '查看成片、节拍数据和全套提示词' : '查看节拍数据和全套提示词', progress: '', running: false };
    const task = p.task || {};
    const progress = p.progress || {};
    const imageReady = Number(progress.image_ready) || 0;
    const videoReady = Number(progress.video_ready) || 0;
    const imageTotal = Number(progress.image_total) || Number(p.image_count) || 0;
    const videoTotal = Number(progress.video_total) || Number(p.video_count) || 0;
    const jobs = p.sub_jobs || [];
    const active = jobs.filter(j => j.status === 'running');
    const parts = [];
    if (imageTotal || imageReady) parts.push(`图片 ${imageReady}/${Math.max(imageTotal, imageReady)}`);
    if (videoTotal || videoReady) parts.push(`视频 ${videoReady}/${Math.max(videoTotal, videoReady)}`);
    const edit = p.video_edit;
    if (edit && ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'].includes(edit.status)
        && (['queued', 'running'].includes(edit.status) || (task.status !== 'running' && !active.length
            && !progress.merged_partial && !progress.merged_stale))) {
        const running = ['queued', 'running'].includes(edit.status);
        const missing = edit.status === 'completed' && edit.output_missing;
        const outdated = edit.status === 'completed' && edit.source_changed;
        const labels = { queued: '精剪排队中', running: '正在精剪', completed: '精剪完成',
            failed: '精剪失败', cancelled: '精剪已取消', interrupted: '精剪已中断' };
        const label = edit.stage === 'cancelling' && running ? '正在停止精剪'
            : missing ? '精剪文件不可用' : outdated ? '旧版精剪完成' : labels[edit.status];
        const stage = running ? PROJECT_EDIT_STAGE_LABELS[edit.stage] || '正在精剪' : '';
        if (stage) parts.push(stage);
        const elapsed = projectsEditElapsed(edit);
        if (elapsed) parts.push(elapsed);
        const next = running ? edit.message || '打开项目查看精剪进度；原片和已完成版本会保留'
            : missing ? '精剪结果文件不可用，打开项目重新精剪'
            : outdated ? '当前原片已更新，打开项目查看旧版结果或重新精剪'
            : edit.status === 'completed' ? '打开项目，查看或下载精剪成片'
            : edit.message || '打开项目查看精剪记录，可重新开始精剪';
        return { label, state: running ? 'running' : missing || ['failed', 'interrupted'].includes(edit.status)
            ? 'failed' : edit.status === 'cancelled' ? 'cancelled' : 'completed',
            next, progress: parts.join(' · '), running };
    }
    let label = '待生成', state = 'saved', next = '打开项目，开始生成图片';
    if (task.status === 'running' || active.length) {
        label = active.length
            ? [...new Set(active.map(j => PROJECT_JOB_LABELS[j.type] || '媒体'))].join('、') + '生成中'
            : projectsStageLabel(task.stage);
        state = 'running';
        next = active.length ? '打开项目查看当前进度' : '查看当前生成进度';
    } else if (progress.merged_partial) {
        label = '部分成片'; state = 'failed'; next = '打开项目，补齐缺失视频后重新合并';
    } else if (progress.merged_stale) {
        label = '成片待更新'; state = 'failed'; next = '旧成片仍可查看，完成当前生成后重新合并';
    } else if (progress.merged) {
        label = '已成片'; state = 'completed'; next = '打开项目，查看或下载成片';
    } else if (p.has_failed_jobs || task.status === 'failed' || task.status === 'cancelled' || task.outcome === 'partial_failed') {
        label = task.status === 'cancelled' ? '已取消' : '需要处理'; state = 'failed';
        next = p.saved || task.status === 'completed' ? '打开项目，检查并重试未完成的生成' : '查看生成记录后重试';
    } else if (videoTotal && videoReady >= videoTotal) {
        label = '视频已齐'; state = 'completed'; next = '打开项目，合并成片';
    } else if (imageTotal && imageReady >= imageTotal) {
        label = '图片已齐'; state = 'completed'; next = '打开项目，继续生成视频';
    } else if (imageReady || videoReady) {
        label = '部分完成'; next = videoReady ? '打开项目，补齐未完成的视频' : '打开项目，补齐未完成的图片';
    } else if (p.kind === 'job') {
        label = p.state === 'completed' ? '生成已结束' : (PROJECT_STATE_LABELS[p.state] || '生成记录');
        state = p.state || 'unknown'; next = '找回项目，或在画廊查看已生成文件';
    }
    return { label, state, next, progress: parts.join(' · '), running: state === 'running' };
}

function projectsCurrentStatusHtml(p, showLabel = true) {
    const status = projectsCurrentStatus(p);
    return `<div class="project-current-status" role="status">${status.running ? '<span class="project-spinner" aria-hidden="true"></span>' : ''}
        ${showLabel ? `<strong>${escapeHtml(status.label)}</strong>` : ''}${status.progress ? `<span>${escapeHtml(status.progress)}</span>` : ''}</div>
        ${showLabel ? `<div class="project-next-hint">${escapeHtml(status.next)}</div>` : ''}`;
}

function projectsBadgesHtml(p) {
    const status = projectsCurrentStatus(p);
    const badges = [`<span class="project-badge state-${escapeHtml(status.state)}">${escapeHtml(status.label)}</span>`];
    if (p.kind === 'job') badges.push('<span class="project-badge job" title="未找到关联的项目，可按标题找回">生成记录</span>');
    const fxBadge = projectsFxQueueBadge(p.fx_queue);
    if (fxBadge) badges.push(fxBadge);
    return badges.join('');
}

// 浏览器占用/排队徽标：来自 /api/projects 的 fx_queue（后端只读标注）。
// 排队时让用户知道"没卡死，是在等浏览器名额"，并指出在等谁。
function projectsFxQueueBadge(mark) {
    if (!mark) return '';
    if (mark.state === 'waiting') {
        const waited = Number(mark.waited_seconds);
        const waitedText = Number.isFinite(waited) && waited >= 5 ? `，已等 ${Math.round(waited)} 秒` : '';
        const holder = mark.holder_task ? `正被 ${mark.holder_task} 占用` : '浏览器被占用';
        return `<span class="project-badge fx-queue waiting" title="${escapeHtml(`${holder}${waitedText}`)}">⏳ 排队等浏览器</span>`;
    }
    if (mark.state === 'active') {
        return '<span class="project-badge fx-queue active" title="本项目当前占用着浏览器">🖥 占用浏览器</span>';
    }
    return '';
}

function projectsAggregateJobs(p, maybeJobs) {
    const project = Array.isArray(p) ? (maybeJobs || {}) : (p || {});
    const jobs = Array.isArray(p) ? p : (maybeJobs || project.sub_jobs || []);
    const groups = new Map();
    [...jobs].sort((a, b) => (b.last_active || 0) - (a.last_active || 0)).forEach(job => {
        if (!groups.has(job.type)) groups.set(job.type, { latest: job, running: 0 });
        if (job.status === 'running') groups.get(job.type).running++;
    });
    const progress = project.progress || {};
    return Array.from(groups, ([type, group]) => {
        const typeLabel = PROJECT_JOB_LABELS[type] || type;
        const field = ['frames', 'staged_render'].includes(type) ? 'image'
            : ['videos', 'video_chain'].includes(type) ? 'video' : '';
        const ready = field ? Number(progress[field + '_ready']) || 0 : 0;
        const total = field ? Number(progress[field + '_total']) || 0 : 0;
        const mediaReady = total > 0 && ready >= total;
        let statusClass = group.running ? 'running' : group.latest.outcome === 'partial_failed' ? 'failed' : group.latest.status || 'unknown';
        if (!group.running && ((mediaReady && group.latest.outcome !== 'partial_failed') || progress.merged)) statusClass = 'completed';
        const labels = { running: `${typeLabel}生成中`, failed: `${typeLabel}失败`,
            cancelled: `${typeLabel}已取消`, completed: ready ? `${typeLabel} ${ready}${total ? '/' + total : ''}` : `${typeLabel}生成已结束` };
        return { type, statusClass, icon: PROJECT_JOB_ICONS[statusClass] || '·',
            label: labels[statusClass] || typeLabel,
            title: '当前结果；过往尝试见生成记录', stats: group };
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
    if (p.assets && p.assets.file_count) {
        bits.push(`${p.assets.file_count} 个文件 · ${projectsFormatBytes(p.assets.bytes)}`);
    }
    bits.push(projectsFormatTime(p.updated_at));
    return `<div class="project-meta">${bits.map(escapeHtml).join(' · ')}</div>`;
}

function projectsRowInnerHtml(p) {
    const task = p.task || {};
    const badgesHtml = projectsBadgesHtml(p);

    const actionBtns = [];
    if (p.archived) {
        actionBtns.push('<button type="button" class="project-card-btn primary" data-act="view-archive" title="查看归档文件">📦 查看归档</button>');
    } else if (p.saved || task.status === 'completed') {
        actionBtns.push('<button type="button" class="project-card-btn primary" data-act="open" title="打开项目">🎬 打开</button>');
    }
    if (!p.archived && task.status === 'running') {
        actionBtns.push('<button type="button" class="project-card-btn primary" data-act="follow" title="跟进实时输出">👁 跟进</button>');
    }
    if (!p.archived && (task.status === 'failed' || task.status === 'cancelled')) {
        actionBtns.push('<button type="button" class="project-card-btn" data-act="retry" title="重试">🔁 重试</button>');
    }
    if ((!p.archived && p.assets && p.assets.file_count) || (p.archived && projectsArchiveVideos(p).length)) {
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
            ${projectsCurrentStatusHtml(p, false)}
        </div>`;
}

// 一行的可见状态指纹。只有它变了才重建这一行的 DOM——列表每 4s 轮询一次，
// 绝大多数 tick 下所有行都该是 no-op。
function projectsRowSignature(p) {
    const task = p.task || {};
    return JSON.stringify([
        p.title, p.theme, p.state, p.saved, p.kind, p.cover, p.has_failed_jobs, p.archived, p.archive_pending, p.archive,
        p.image_count, p.video_count, p.updated_at, p.progress, p.video_edit,
        p.video_edit && ['queued', 'running'].includes(p.video_edit.status) ? projectsEditElapsed(p.video_edit) : '',
        task.status, task.stage, task.error, task.outcome,
        p.fx_queue ? [p.fx_queue.state, p.fx_queue.position, p.fx_queue.holder_task] : null,
        (p.assets || {}).file_count, (p.assets || {}).bytes,
        (p.sub_jobs || []).map(j => `${j.type}:${j.status}:${j.outcome || ''}:${j.last_active || 0}`).join(','),
    ]);
}

function renderProjects() {
    const container = document.getElementById('projects-list');
    projectsUpdateSectionUi();
    if (!container || !projectsRows) return;
    const visibleRows = projectsRows.filter(p => projectsIsArchived(p) === (projectsSection === 'archived'));

    // chips 角标
    document.querySelectorAll('#projects-filters .projects-filter-chip').forEach(chip => {
        if (!chip.dataset.label) chip.dataset.label = chip.textContent.trim();
        const n = chip.dataset.filter === 'all' ? projectsCounts.active ?? projectsTotal
            : projectsCounts[chip.dataset.filter];
        chip.textContent = (n !== undefined) ? `${chip.dataset.label} (${n})` : chip.dataset.label;
        chip.classList.toggle('active', chip.dataset.filter === projectsFilter);
    });
    const statsEl = document.getElementById('projects-stats');
    if (statsEl) statsEl.textContent = `共 ${projectsTotal} 个${projectsSection === 'archived' ? '归档' : ''}项目`;

    if (!visibleRows.length) {
        container.innerHTML = projectsTotal || projectsSearch
            ? '<div class="projects-status">🗂️ 这个筛选下暂时没有匹配的项目</div>'
            : projectsSection === 'archived'
                ? '<div class="projects-status">📦 暂无归档项目<br>在项目详情或批量操作中归档后，会集中显示在这里。</div>'
            : '<div class="projects-status">📁 还没有项目，导入图片和视频提示词即可开始。<br><button type="button" class="projects-btn primary" data-act="import-prompts">导入提示词</button></div>';
        projectsSelectedKey = null;
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

    visibleRows.forEach(p => {
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

    const running = rows.filter(p => !p.archived && (p.task || {}).status === 'running').length;
    const withTask = rows.filter(p => !p.archived && ((p.task || {}).id || (p.sub_jobs || []).length > 0)).length;
    const archivable = rows.filter(p => !p.archive_pending && projectsCanArchive(p)).length;
    const pendingArchives = rows.filter(p => p.archive_pending && projectsCanArchive(p)).length;
    const btns = [];
    if (running) btns.push(`<button type="button" class="projects-btn danger" data-bulk="cancel">✕ 取消运行中（${running}）</button>`);
    btns.push('<button type="button" class="projects-btn" data-bulk="copy-titles">📋 复制标题</button>');
    if (archivable) btns.push(`<button type="button" class="projects-btn" data-bulk="archive"${projectsArchiving ? ' disabled' : ''} title="保留成片（如有）、节拍数据、全套提示词和封面缩略图，其余永久删除">📦 归档（${archivable}）</button>`);
    if (pendingArchives) btns.push(`<button type="button" class="projects-btn" data-bulk="finish-archive"${projectsArchiving ? ' disabled' : ''} title="保留文件已保存，继续清理剩余内容">📦 完成归档（${pendingArchives}）</button>`);
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
            const ids = rows.filter(p => !p.archived && (p.task || {}).status === 'running' && p.task.id).map(p => p.task.id);
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

        case 'archive':
            await projectsArchive(rows.filter(p => !p.archive_pending && projectsCanArchive(p)));
            break;
        case 'finish-archive':
            await projectsArchive(rows.filter(p => p.archive_pending && projectsCanArchive(p)));
            break;

        case 'delete-task': {
            const ids = rows.filter(p => !p.archived && (p.task || {}).id).map(p => p.task.id);
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
            const targets = rows.filter(p => !p.archived && p.saved && (p.library || {}).id);
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
    const requested = selectedModel || config.model || DEFAULT_CONFIG.model;
    const selected = (typeof normalizeLlmModel === 'function') ? normalizeLlmModel(requested) : requested;
    const families = (typeof LLM_MODEL_PICKER_FAMILIES !== 'undefined') ? LLM_MODEL_PICKER_FAMILIES : [];
    const groups = (typeof LLM_MODEL_GROUPS !== 'undefined') ? LLM_MODEL_GROUPS : {};
    const known = Object.values(groups).flat();
    return families.map(family => {
        const models = (groups[family.key] || []).slice();
        // 保留自定义模型；已下架的文本模型在上面迁移到当前型号。
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
    const btns = [];
    if (running.length) btns.push(`<button type="button" class="projects-btn danger" data-act="cancel-all-jobs">取消全部生成（${running.length}）</button>`);
    btns.push('<button type="button" class="projects-btn" data-act="copy-title">复制标题</button>');
    if (settled.length) btns.push(`<button type="button" class="projects-btn danger" data-act="delete-all-jobs">清除生成记录（${settled.length}）</button>`);
    return btns.join('');
}

function projectsDetailActionsHtml(p) {
    if (p.archived) return (p.archive_pending && projectsCanArchive(p)
        ? `<button type="button" class="projects-btn primary" data-act="archive"${projectsArchiving ? ' disabled' : ''}>📦 完成归档</button>` : '')
        + (projectsArchiveVideos(p).length ? '<button type="button" class="projects-btn" data-act="gallery">在画廊查看成片</button>' : '')
        + '<button type="button" class="projects-btn" data-act="copy-title">复制标题</button>';
    const task = p.task || {};
    const btns = [], more = [];
    if (p.kind === 'job') {
        btns.push('<button type="button" class="projects-btn primary" data-act="find-parent">找回项目</button>');
        if (!(p.assets && p.assets.file_count)) btns.push('<button type="button" class="projects-btn" data-act="gallery-search">在画廊查找</button>');
        more.push(projectsOrphanActionsHtml(p));
    }
    if (task.status === 'running') {
        btns.push('<button type="button" class="projects-btn primary" data-act="follow">查看进度</button>');
        more.push('<button type="button" class="projects-btn danger" data-act="cancel">取消生成</button>');
    } else if (p.saved || task.status === 'completed') {
        btns.push('<button type="button" class="projects-btn primary" data-act="open">打开项目</button>');
    }
    if (task.status === 'completed') {
        more.push(`<label class="projects-rerun">
            <select id="projects-rerun-model" class="projects-sort-select" aria-label="选择重新生成使用的模型">${projectsModelOptions(task.model)}</select>
            <button type="button" class="projects-btn" data-act="rerun">换模型重新生成</button>
        </label>`);
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        (p.saved ? more : btns).push('<button type="button" class="projects-btn" data-act="retry">重试生成</button>');
    }
    if (p.assets && p.assets.file_count) btns.push('<button type="button" class="projects-btn" data-act="gallery">查看文件</button>');
    if (projectsCanArchive(p)) btns.push(`<button type="button" class="projects-btn" data-act="archive"${projectsArchiving ? ' disabled' : ''} title="先查看保留文件和清理范围">📦 归档项目</button>`);
    more.push('<button type="button" class="projects-btn danger" data-act="delete-project" title="删除项目、生成记录及本地生成的图片和视频">彻底删除项目</button>');
    if (task.id && p.saved) more.push('<button type="button" class="projects-btn danger" data-act="delete-task" title="保留已收藏项目和本地生成文件">仅清除生成记录</button>');
    return btns.join('') + `<details class="projects-more-actions" data-project-fold="actions"><summary>更多操作</summary><div class="projects-detail-actions">${more.join('')}</div></details>`;
}

function projectsSafeArchiveUrl(url) {
    return typeof url === 'string' && ((url.startsWith('/') && !url.startsWith('//')) || url.startsWith('outputs/'));
}

function projectsArchiveVideos(p) {
    const archive = p.archive || {};
    const videos = Array.isArray(archive.final_videos) ? archive.final_videos : archive.refined_videos || [];
    return videos.filter(file => file && projectsSafeArchiveUrl(file.url))
        .map(file => ({ ...file, kind: file.kind === 'merged_video' ? 'merged_video' : 'refined_video' }));
}

function projectsArchiveFiles(p) {
    const archive = p.archive || {};
    const files = [...(Array.isArray(archive.retained_files) ? archive.retained_files : [])];
    projectsArchiveVideos(p).forEach(file => files.push(file));
    if (archive.beats_url) files.push({ url: archive.beats_url, name: '反推节拍数据.json', kind: 'beats' });
    if (archive.prompts_url) files.push({ url: archive.prompts_url, name: '全套提示词.md', kind: 'prompts' });
    const seen = new Set();
    return files.filter(file => {
        if (!file || !projectsSafeArchiveUrl(file.url) || seen.has(file.url)) return false;
        seen.add(file.url);
        return true;
    });
}

function projectsFilesListHtml(files) {
    const labels = { refined_video: '精剪成片', merged_video: '合成成片', beats: '节拍数据', prompts: '全套提示词', cover: '封面缩略图' };
    return `<ul>${files.map(file => `<li><span><strong>${escapeHtml(labels[file.kind] || '保留文件')}</strong><small>${escapeHtml(file.name || file.url.split('/').pop())}</small></span>
        <a class="projects-btn" href="${escapeHtml(file.url)}" target="_blank" rel="noopener">查看</a>
        <a class="projects-btn" href="${escapeHtml(file.url)}" download="${escapeHtml(file.name || '')}">下载</a></li>`).join('')}</ul>`;
}

function projectsBeatFilesHtml(p) {
    const seen = new Set();
    const files = (Array.isArray(p.beat_files) ? p.beat_files : []).filter(file => {
        if (!file || !projectsSafeArchiveUrl(file.url) || seen.has(file.url)) return false;
        seen.add(file.url);
        return true;
    }).map(file => ({ ...file, kind: 'beats' }));
    return files.length ? `<div class="projects-archive-files"><h4>节拍数据</h4>${projectsFilesListHtml(files)}</div>` : '';
}

function projectsArchiveFilesHtml(p) {
    const files = projectsArchiveFiles(p);
    const videos = projectsArchiveVideos(p);
    const cover = projectsArchiveCoverUrl(p);
    return `<div class="projects-archive-files"><h4>归档文件</h4>
        <p class="projects-detail-hint">${p.archive_pending
            ? '保留文件已保存，点击完成归档继续清理剩余内容。'
            : videos.length ? `保留成片、节拍数据和全套提示词${cover ? '，以及封面缩略图' : ''}，其余项目文件已永久删除。`
                : `未保留成片视频，只保留全套提示词和节拍数据（如有）${cover ? '，以及封面缩略图' : ''}，其余项目文件已永久删除。`}</p>
        ${videos.map(file => `<figure class="projects-archive-video"><video controls preload="metadata" src="${escapeHtml(file.url)}"${cover ? ` poster="${escapeHtml(cover)}"` : ''}></video><figcaption>${escapeHtml(file.name || '成片')}</figcaption></figure>`).join('')}
        ${projectsFilesListHtml(files)}
        ${files.length ? '' : '<p class="projects-detail-hint">归档文件信息暂时不可用，请刷新项目列表。</p>'}</div>`;
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

function projectsJobListHtml(jobs) {
    return `<ul class="projects-job-list">${jobs.map(j => `
        <li class="${escapeHtml(j.status || '')}">
            <span class="projects-job-head"><span>${escapeHtml(PROJECT_JOB_ICONS[j.status] || '·')} ${escapeHtml(PROJECT_JOB_LABELS[j.type] || j.type)} · ${escapeHtml(j.outcome === 'partial_failed' ? '部分完成，需要处理' : PROJECT_STATE_LABELS[j.status] || j.status || '等待中')}</span>${projectsJobActionsHtml(j)}</span>
            <span class="project-meta">${escapeHtml(projectsFormatTime(j.last_active))}</span>
            ${j.error ? `<span class="projects-job-error">${escapeHtml(j.error)}</span>` : ''}
            <details class="projects-technical" data-project-fold="job-${escapeHtml(j.id || '')}"><summary>技术详情</summary><span class="projects-job-id">${escapeHtml(j.id || '')}</span></details>
        </li>`).join('')}</ul>`;
}

function renderProjectDetail() {
    const pane = document.getElementById('projects-detail');
    if (!pane) return;
    const p = projectsSelectedKey ? projectsFindRow(projectsSelectedKey) : null;
    if (!p) {
        pane.innerHTML = '<div class="projects-detail-empty">选中左侧任一项目查看详情</div>';
        pane.classList.remove('open');
        delete pane.dataset.projectKey;
        delete pane.dataset.renderSignature;
        return;
    }
    // Polling should preserve the reader's expanded records and model selection.
    const sameProject = pane.dataset.projectKey === p.project_key;
    const signature = JSON.stringify([p, projectsArchiving]);
    if (sameProject && pane.dataset.renderSignature === signature) return;
    const focused = document.activeElement && pane.contains(document.activeElement) ? document.activeElement : null;
    const focusedFold = focused?.tagName === 'SUMMARY'
        ? focused.closest('details[data-project-fold]')?.dataset.projectFold : null;
    const focusedId = focused?.id;
    const openFolds = new Set(sameProject
        ? Array.from(pane.querySelectorAll('details[data-project-fold][open]'), el => el.dataset.projectFold) : []);
    const rerunModel = sameProject ? pane.querySelector('#projects-rerun-model')?.value : null;
    pane.dataset.projectKey = p.project_key;
    pane.dataset.renderSignature = signature;
    pane.classList.add('open');

    const task = p.task || {};
    const facts = [
        ['最近活动', projectsFormatTime(p.updated_at)],
        ['生成文件', p.assets && p.assets.file_count
            ? `${p.assets.file_count} 个 · ${projectsFormatBytes(p.assets.bytes)}` : '暂无'],
    ];
    if (p.archived) {
        facts.unshift(['归档时间', projectsFormatTime((p.archive || {}).archived_at)]);
        facts.push(['已释放空间', projectsFormatBytes((p.archive || {}).deleted_bytes)]);
    }
    const technical = [
        ['生成耗时', projectsFormatDuration(task.duration_seconds) || '—'],
        ['使用模型', task.model || '—'], ['记录 ID', task.id || '—'], ['项目 ID', p.project_key],
    ];
    if (task.token_usage) {
        const u = task.token_usage;
        technical.push(['Tokens', `${u.total_tokens} (I:${u.prompt_tokens} O:${u.completion_tokens}) · ${u.api_calls} 次调用`]);
    }
    const factsHtml = entries => `<dl class="projects-facts">${entries.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('')}</dl>`;
    const active = p.archived ? [] : (p.sub_jobs || []).filter(j => j.status === 'running');
    const history = p.archived ? [] : (p.sub_jobs || []).filter(j => j.status !== 'running');
    const taskError = !p.archived && task.error ? `<p class="projects-job-error">${escapeHtml(task.error)}</p>` : '';
    pane.innerHTML = `
        <div class="projects-detail-head"><h3>${escapeHtml(p.title || '未命名项目')}</h3>
            <button type="button" class="projects-detail-close" data-act="close-detail" title="收起详情">&times;</button></div>
        ${projectsCurrentStatusHtml(p)}
        <div class="projects-detail-actions">${projectsDetailActionsHtml(p)}</div>
        ${p.kind === 'job' ? '<p class="projects-detail-hint">未找到关联项目，生成记录和已生成文件仍可查看。</p>' : ''}
        ${p.theme && p.theme !== p.title ? `<p class="projects-detail-theme">${escapeHtml(p.theme)}</p>` : ''}
        ${factsHtml(facts)}
        ${p.archived ? projectsArchiveFilesHtml(p) : projectsBeatFilesHtml(p)}
        ${active.length ? `<div class="projects-detail-section"><h4>正在生成</h4>${projectsJobListHtml(active)}</div>` : ''}
        ${history.length || taskError ? `<details class="projects-history" data-project-fold="history"><summary>生成记录${history.length ? `（${history.length}）` : ''}</summary>${taskError}${projectsJobListHtml(history)}</details>` : ''}
        <details class="projects-technical" data-project-fold="technical"><summary>技术详情</summary>${factsHtml(technical)}</details>`;
    pane.querySelectorAll('details[data-project-fold]').forEach(el => { el.open = openFolds.has(el.dataset.projectFold); });
    if (rerunModel) {
        const select = pane.querySelector('#projects-rerun-model');
        if (select && Array.from(select.options).some(option => option.value === rerunModel)) select.value = rerunModel;
    }
    if (sameProject && focusedFold) {
        Array.from(pane.querySelectorAll('details[data-project-fold]'))
            .find(el => el.dataset.projectFold === focusedFold)?.querySelector('summary')?.focus({ preventScroll: true });
    } else if (sameProject && focusedId) {
        document.getElementById(focusedId)?.focus({ preventScroll: true });
    }
}

/* ── 动作 ──────────────────────────────────────────────────────────────── */

function projectsArchiveErrorText(errors) {
    return (errors || []).map(error => `${error.title || error.project_key || '项目'}：${error.message || '归档失败'}`).join('；');
}

function projectsArchiveRetentionText(p) {
    const files = p.retained_files || (p.archive || {}).retained_files || [];
    const retention = p.video_retention || (files.some(file => file.kind === 'refined_video') ? 'refined'
        : files.some(file => file.kind === 'merged_video') ? 'merged' : 'none');
    if (retention === 'merged') return '未完成精剪，将保留当前合成成片。';
    if (retention === 'none') return '没有可保留的成片视频，只保留全套提示词和节拍数据（如有）。';
    return '保留精剪后的成片视频。';
}

function projectsArchivePreviewHtml(projects, errors, skipped, completing = false) {
    const count = projects.reduce((sum, p) => sum + (Number(p.delete_count) || 0), 0);
    const bytes = projects.reduce((sum, p) => sum + (Number(p.delete_bytes) || 0), 0);
    return `<strong>${completing ? '完成归档' : '归档'} ${projects.length} 个项目</strong><br>保留成片视频（如有）、节拍数据文件和全套提示词，以及封面缩略图（如有）。<br>
        <strong class="projects-archive-warning">其余文件、生成记录和旧素材将永久删除，无法恢复。</strong><br>
        共清理 ${count} 个文件，释放约 ${projectsFormatBytes(bytes)}。
        <span class="projects-archive-preview">${projects.map(p => `<span class="projects-archive-preview-project"><strong>${escapeHtml(p.title || p.project_key)}</strong>
            <span>${escapeHtml(projectsArchiveRetentionText(p))}</span>
            <span>保留：${(p.retained_files || []).map(file => escapeHtml(file.name || file.url || '文件')).join('、') || '暂无文件'}</span>
            ${(p.retained_files || []).some(file => file.kind === 'beats') ? '' : '<span class="projects-archive-warning">未找到明确关联的节拍数据，此项目归档将不包含节拍文件。</span>'}
            <span>删除 ${Number(p.delete_count) || 0} 个文件 · ${projectsFormatBytes(p.delete_bytes)}</span></span>`).join('')}</span>
        ${errors.length ? `<span class="projects-archive-warning">以下项目无法归档，将跳过：${escapeHtml(projectsArchiveErrorText(errors))}</span><br>` : ''}
        ${skipped ? `另跳过 ${skipped} 个运行中、已归档或未关联项目。<br>` : ''}
        确定执行归档并永久删除上述其余内容吗？`;
}

function projectsApplyArchiveResults(results, sourceRows, deletedLibraryIds) {
    const byKey = new Map(results.map(result => [result.project_key, result]));
    const successfulRows = sourceRows.filter(p => byKey.has(p.project_key));
    const ideaIds = new Set((deletedLibraryIds || []).map(String));
    const taskIds = new Set();
    successfulRows.forEach(p => {
        if ((p.library || {}).id) ideaIds.add(String(p.library.id));
        if (p.id) ideaIds.add(String(p.id));
        if ((p.task || {}).id) { ideaIds.add(String(p.task.id)); taskIds.add(String(p.task.id)); }
        (p.sub_jobs || []).forEach(job => { if (job.id) taskIds.add(String(job.id)); });
    });
    const matches = idea => Boolean(idea && (byKey.has(idea.project_key) || ideaIds.has(String(idea.id))));
    if (typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas)) {
        savedIdeas = savedIdeas.filter(idea => !matches(idea));
        successfulRows.forEach(p => {
            const result = byKey.get(p.project_key);
            const id = (result.library || {}).id || result.library_id || (p.library || {}).id;
            if (id) savedIdeas.push({ id, project_key: p.project_key, title: p.title, theme: p.theme,
                archived: true, archive_pending: Boolean(result.archive_pending), archive: result.archive || result });
        });
        try { localStorage.setItem('spark_library', JSON.stringify(savedIdeas)); } catch (_) {}
    }
    try {
        const snapshot = JSON.parse(localStorage.getItem('spark_current_idea') || 'null');
        if (matches(snapshot) || ideaIds.has(localStorage.getItem('spark_current_idea_id'))) {
            localStorage.removeItem('spark_current_idea');
            localStorage.removeItem('spark_current_idea_id');
        }
        if (taskIds.has(localStorage.getItem('spark_active_task_id'))) {
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
        }
        ideaIds.forEach(id => localStorage.removeItem(`spark_prompt_history_${id}`));
        const background = JSON.parse(localStorage.getItem('spark_active_background_tasks') || 'null');
        if (background && Array.isArray(background.tasks)) {
            background.tasks = background.tasks.filter(task => !ideaIds.has(String(task.ideaId)) && !taskIds.has(String(task.taskId)));
            localStorage.setItem('spark_active_background_tasks', JSON.stringify(background));
        }
    } catch (error) { console.warn('归档后的本地恢复状态清理失败', error); }
    if (typeof currentIdea !== 'undefined' && matches(currentIdea)) {
        currentIdea = null;
        if (typeof resetPromptEditor === 'function') resetPromptEditor();
        document.querySelectorAll('#output-content-view video, #output-content-view audio').forEach(media => {
            media.pause();
            media.removeAttribute('src');
            media.load();
        });
        document.getElementById('output-content-view')?.classList.remove('active');
        document.getElementById('output-placeholder-view')?.classList.add('active');
        if (typeof switchMainTab === 'function') switchMainTab('projects');
    }
    if (typeof updateFavoriteButtonState === 'function') updateFavoriteButtonState();
    projectsRows = (projectsRows || []).map(p => {
        const result = byKey.get(p.project_key);
        if (!result) return p;
        const archive = result.archive || result;
        const cover = projectsArchiveCoverUrl({ archive });
        return { ...p, state: 'archived', archived: true, archive_pending: Boolean(result.archive_pending), archive, cover,
            task: null, sub_jobs: [], progress: {}, image_count: 0, video_count: projectsArchiveVideos({ archive }).length,
            assets: { dir: (p.assets || {}).dir || '', file_count: (archive.retained_files || []).length, bytes: 0, cover } };
    });
    results.forEach(result => projectsSelected.delete(result.project_key));
    projectsRefreshRevision++;
    if (typeof galleryData !== 'undefined') galleryData = null;
    if (typeof gallerySelected !== 'undefined') gallerySelected.clear();
    renderProjects();
}

async function projectsArchiveRequest(projectKeys, preview) {
    const response = await fetch('/api/projects/archive', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_keys: projectKeys, preview }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.status !== 'ok') throw new Error(data.error || data.message || `HTTP ${response.status}`);
    return data;
}

async function projectsArchive(rows) {
    if (projectsArchiving) return;
    const targets = (rows || []).filter(projectsCanArchive);
    if (!targets.length) return;
    projectsArchiving = true;
    renderProjectsBulkBar();
    renderProjectDetail();
    let submitted = false;
    try {
        const keys = [...new Set(targets.map(p => p.project_key))];
        const preview = await projectsArchiveRequest(keys, true);
        const requested = new Set(keys);
        const errors = Array.isArray(preview.errors) ? preview.errors : [];
        const executable = (preview.projects || []).filter(p => requested.has(p.project_key)
            && !errors.some(error => error.project_key === p.project_key));
        if (!executable.length) {
            showToast(projectsArchiveErrorText(errors) || '没有可归档的项目，请刷新列表后重试', 'error');
            return;
        }
        if (typeof customConfirm !== 'function') throw new Error('归档确认窗口不可用，请刷新后重试');
        const completing = targets.every(p => p.archive_pending);
        if (!await customConfirm(projectsArchivePreviewHtml(executable, errors, rows.length - targets.length, completing),
            completing ? '完成归档并永久删除剩余内容' : '归档并永久删除其余内容', '取消')) return;
        submitted = true;
        const result = await projectsArchiveRequest(executable.map(p => p.project_key), false);
        const completed = (result.projects || []).filter(p => requested.has(p.project_key));
        if (completed.length) {
            projectsApplyArchiveResults(completed, targets, result.deleted_library_ids);
            projectsApplySection('archived', { clearSearch: true });
            projectsSelectedKey = completed[0].project_key;
            projectsPendingArchivedKey = completed[0].project_key;
            renderProjects();
        }
        const executionErrors = Array.isArray(result.errors) ? result.errors : [];
        const allErrors = [...errors, ...executionErrors];
        const missing = executable.length - completed.length - executionErrors.length;
        const summary = completed.length ? `已归档 ${completed.length} 个项目，释放 ${projectsFormatBytes(result.deleted_bytes)}` : '归档失败';
        showToast(`${summary}${allErrors.length ? `；${projectsArchiveErrorText(allErrors)}` : ''}${missing > 0 ? `；${missing} 个项目未归档，请刷新后检查` : ''}`,
            allErrors.length || missing > 0 || !completed.length ? 'error' : 'success');
    } catch (error) {
        console.error('Project archive failed', error);
        showToast(`归档失败：${error.message}${submitted ? '，请刷新查看实际结果后再重试' : ''}`, 'error');
    } finally {
        projectsArchiving = false;
        if (submitted) projectsRefreshRevision++;
        renderProjectsBulkBar();
        // 按钮的禁用状态不属于项目数据，强制更新详情。
        const pane = document.getElementById('projects-detail');
        if (pane) delete pane.dataset.renderSignature;
        renderProjectDetail();
        await refreshProjects();
    }
}

async function openArchivedProject(projectKey) {
    projectsApplySection('archived', { clearSearch: true });
    projectsPendingArchivedKey = projectKey;
    projectsRefreshRevision++;
    switchMainTab('projects');
    await refreshProjects();
    if (projectsFindRow(projectKey)?.archived) {
        projectsSelectedKey = projectKey;
        projectsPendingArchivedKey = null;
        renderProjects();
    }
}

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
    if (p.archived && !(p.archive_pending && act === 'archive')
        && !['view-archive', 'gallery', 'gallery-search', 'copy-title'].includes(act)) return;
    const task = p.task || {};
    switch (act) {
        case 'archive':
            await projectsArchive([p]);
            break;
        case 'view-archive':
            projectsSelectedKey = p.project_key;
            renderProjects();
            break;
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
                `确定删除这 ${ids.length} 条生成记录吗？只删记录，本地已生成的文件不受影响。`)) return;
            const ok = await projectsBulkJobRequest('/api/tasks/delete', ids);
            showToast(ok === ids.length
                ? `已删除 ${ok} 条生成记录`
                : `${ids.length} 条记录中删除了 ${ok} 条，其余失败`,
                ok === ids.length ? 'success' : 'error');
            refreshProjects();
            break;
        }
        case 'gallery-search': {
            // 统一走画廊的 galleryFocus：等扫描完成、先清掉上次遗留的筛选再填词
            const title = p.title || '';
            if (typeof galleryFocus !== 'function') { switchMainTab('gallery'); break; }
            await galleryFocus({ search: title });
            showToast(`已在画廊里按「${title}」筛选`, 'info');
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
            const archiveVideo = p.archived ? projectsArchiveVideos(p)[0] : null;
            const dir = (p.assets || {}).dir || (archiveVideo ? archiveVideo.url.split('/').slice(0, -1).join('/') : '');
            const groupKey = dir.split('/').pop();
            if (typeof galleryFocus !== 'function') { switchMainTab('gallery'); break; }
            await galleryFocus({ groupKey });
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
    projectsUpdateSectionUi();

    // Empty launches begin at the workbench; keep restored results and active
    // generation on their existing resume path. Respect hidden-tab preferences.
    if (!currentIdea && !localStorage.getItem('spark_current_idea_id') &&
        !localStorage.getItem('spark_active_task_id')) {
        switchMainTab('projects');
    }

    document.getElementById('projects-sections')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.projects-section-btn');
        if (btn) projectsSetSection(btn.dataset.section);
    });

    document.getElementById('projects-filters')?.addEventListener('click', (e) => {
        const chip = e.target.closest('.projects-filter-chip');
        if (!chip || projectsSection !== 'active') return;
        projectsFilter = chip.dataset.filter || 'all';
        projectsRefreshRevision++;
        refreshProjects({ assets: false });
    });

    document.getElementById('projects-search')?.addEventListener('input', (e) => {
        projectsSearch = e.target.value || '';
        projectsRefreshRevision++;
        clearTimeout(projectsSearchDebounce);
        projectsSearchDebounce = setTimeout(() => refreshProjects({ assets: false }), 220);
    });

    document.getElementById('projects-sort')?.addEventListener('change', (e) => {
        projectsSort = e.target.value || 'newest';
        projectsRefreshRevision++;
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
        if (e.target.closest('[data-act="import-prompts"]')) {
            document.getElementById('projects-import-prompt-btn')?.click();
            return;
        }
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
        if (p.archived) {
            projectsRunAction('view-archive', p, e);
        } else if (p.saved || (p.task && p.task.status === 'completed')) {
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
// 画廊分组的 key 是磁盘目录名，而 project_key 不一定与目录名相等（导入的项目 key 里
// 是双下划线、目录名是单下划线），所以按「项目 key 或资产目录名」两种方式找行。
function projectsFindRowByGalleryKey(key) {
    if (!key) return null;
    return projectsFindRow(key)
        || (projectsRows || []).find(p => ((p.assets || {}).dir || '').split('/').pop() === key)
        || null;
}

// 从画廊分组回到项目页定位：切到「进行中/全部」、清空搜索、等行出现后选中并滚到视野。
// 找不到（已归档或标题被改）时退一步按标题搜索，让用户至少落在相关结果上。
async function projectsLocate({ projectKey = '', title = '' } = {}) {
    projectsApplySection('active', { clearSearch: true });
    openProjectsWorkbench('all');
    let row = null;
    const t0 = Date.now();
    while (Date.now() - t0 < 3000) {
        row = projectsFindRowByGalleryKey(projectKey);
        if (row) break;
        await new Promise(r => setTimeout(r, 120));
    }
    if (row) {
        projectsSelectedKey = row.project_key;
        renderProjects();
        const el = document.querySelector(`#projects-list .project-row[data-key="${CSS.escape(row.project_key)}"]`);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            el.classList.add('flash-highlight');
            setTimeout(() => el.classList.remove('flash-highlight'), 1600);
        }
        return true;
    }
    if (title) {
        const input = document.getElementById('projects-search');
        if (input) {
            input.value = title;
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        showToast(`项目页里没有精确匹配的项目，已按「${title}」搜索`, 'info');
    } else {
        showToast('项目页里没找到这个项目（可能已归档或被清理）', 'info');
    }
    return false;
}

function openProjectsWorkbench(filter) {
    projectsApplySection(filter === 'archived' ? 'archived' : 'active');
    if (filter && filter !== 'archived') projectsFilter = filter;
    projectsRefreshRevision++;
    const chip = document.querySelector(`#projects-filters .projects-filter-chip[data-filter="${filter || 'all'}"]`);
    if (chip) {
        document.querySelectorAll('#projects-filters .projects-filter-chip')
            .forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
    }
    switchMainTab('projects');
    refreshProjects({ assets: false });
}

// 兼容 defer 加载顺序：DOM 就绪后初始化一次
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initProjects);
} else {
    initProjects();
}
