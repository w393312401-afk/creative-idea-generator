// --- replica_pipeline.js ---
// 爆款复刻 / 二创面板。后端见 replica_pipeline.py，方案见
// docs/replica_and_variant_pipeline_plan.md。
//
// 页面只有三个真状态：没任务、跑着、停在人工卡点。所有长耗时阶段走 SSE 推进度，
// 停下来之后页面是静止的——所以这里没有轮询。

let replicaJobs = [];
let replicaState = null;      // 当前打开的 job_state
let replicaTaskId = null;
let replicaSSE = null;
let replicaBusy = false;

// stage → 中文标签的**兜底**副本。真源在 replica_pipeline.py 的 STAGE_LABELS，
// 后端随 job 行下发 stage_label；这里只在拿不到时顶上（例如渲染一条本地拼出来的
// 乐观状态）。projects.js 也读这一份，不再自己抄第三份。
const REPLICA_STAGE_LABELS = {
    ingest: '已上传',
    extract: '抽帧中',
    confirm_cost: '待确认成本',
    review_frames: '逐帧读取',
    cluster_beats: '聚类节拍',
    review_beats: '待人工核对',
    mutate_beats: '二创改写中',
    compose: '合成提示词',
    audit: '门禁校验',
    audit_failed: '门禁未过',
    completed: '已完成',
    cancelled: '已取消',
};

// 用户看得见的四个阶段。后台十二个 stage 是状态机粒度，摆在 UI 上只会让人对着一个
// chip 找不到对应区块。与 replica_pipeline.PHASES 同序同义。
const REPLICA_PHASES = [
    { key: 'material', label: '素材', stages: ['ingest', 'extract', 'confirm_cost'] },
    { key: 'reverse', label: '反推', stages: ['review_frames', 'cluster_beats', 'mutate_beats'] },
    { key: 'review', label: '核对节拍', stages: ['review_beats'] },
    { key: 'deliver', label: '交付', stages: ['compose', 'audit', 'audit_failed', 'completed'] },
];

function replicaStageLabel(stageOrRow) {
    if (stageOrRow && typeof stageOrRow === 'object') {
        return stageOrRow.stage_label || REPLICA_STAGE_LABELS[stageOrRow.stage] || stageOrRow.stage || '';
    }
    return REPLICA_STAGE_LABELS[stageOrRow] || stageOrRow || '';
}

function replicaPhaseIndex(stage) {
    const at = REPLICA_PHASES.findIndex(p => p.stages.includes(stage));
    return at < 0 ? 0 : at;
}

// 节拍自己的施工阶段（beat.stage）。与 prompt_pipeline/reverse.py 的 _STAGE_LABELS_ZH
// 同源同义——那九个值是 Pass B 的闭集枚举，直接摆英文等于让核对的人先查一遍词典。
const REPLICA_BEAT_STAGE_LABELS = {
    demolition: '拆除清运',
    structural: '结构修复',
    rough_in: '隐蔽工程',
    enclosure: '封板封闭',
    surface: '面层饰面',
    floor: '地面收尾',
    fixtures: '灯具设备',
    furnishing: '家具软装',
    reveal: '成品揭示',
};

// 节拍卡片上按「一行一条」编辑的数组字段（timelapse-beats.schema.json 里的 array 项）。
const REPLICA_LIST_FIELDS = new Set([
    'package_operations', 'persistent_traces', 'visible_details', 'source_event_ids',
    'evidence_frames', 'reference_frames',
]);

const REPLICA_AXES = [
    { key: 'carrier', label: '载体替换', hint: '石屋 → 废弃巴士 / 船舱 / 地窖' },
    { key: 'environment', label: '地域环境', hint: '江南 → 北欧 / 沙漠' },
    { key: 'material', label: '材质风格', hint: '木作 → 清水混凝土' },
    { key: 'pacing', label: '节奏拍数', hint: '六拍 8s → 四拍 10s' },
    { key: 'reward', label: '结局奖励', hint: '工作室 → 茶室 / 民宿' },
];

const REPLICA_MAX_AXES = 2;

/* --- 反推段的模型与采样档位 ---
 *
 * 反推（Pass A 逐帧识别 + 峰值帧复核）此前只能改配置文件：`frameFactsModel` /
 * `peakVerifyModel` 两个键在 UI 上不存在，页面上的「LLM 模型」选择器管的是激发/
 * 合成那条链路，改它对反推毫无影响。而这两步恰恰是整条复刻线上最吃模型能力的
 * 地方——flash 读不出材料标签和完成范围，节拍就从源头错了。所以把它们摆到成本
 * 确认卡点上，和帧数、调用次数一起看。
 *
 * 模型清单直接复用 js/state.js 的 LLM_MODEL_GROUPS（不抄第二份；那边加了模型这里
 * 自动就有）。state.js 没加载时退回一份最小清单，保证选择器不会变成空下拉。
 */
const REPLICA_FALLBACK_MODELS = [
    { value: 'gemini-3.6-flash-high', label: 'gemini-3.6-flash-high' },
    { value: 'gemini-3.1-pro-high', label: 'gemini-3.1-pro-high' },
];

const REPLICA_PASS_A_DEFAULT_MODEL = 'gemini-3.6-flash-high';

function replicaModelChoices() {
    const groups = typeof LLM_MODEL_GROUPS !== 'undefined' ? LLM_MODEL_GROUPS : null;
    if (!groups) return REPLICA_FALLBACK_MODELS.slice();
    return ['gemini', 'gpt', 'claude']
        .flatMap(key => (groups[key] || []).map(m => ({ value: m.value, label: m.label })));
}

// 下拉里出现的值不一定在清单里（配置文件里手写过一个自定义模型名）：把当前值补进去，
// 否则下拉会自己跳到第一项，用户一保存就把配置文件里的选择改掉了。
function replicaModelSelect(id, current, extraOptions) {
    const choices = replicaModelChoices();
    const options = (extraOptions || []).concat(choices);
    const cur = String(current == null ? '' : current);
    if (cur && !options.some(o => o.value === cur)) {
        options.push({ value: cur, label: `${cur}（自定义）` });
    }
    return `<select id="${id}" class="replica-select">${options.map(o => `
        <option value="${escapeHtmlReplica(o.value)}" ${o.value === cur ? 'selected' : ''}
        >${escapeHtmlReplica(o.label)}</option>`).join('')}</select>`;
}

// 反推段的模型选择写回全局 config + localStorage，和激发页脚的模型选择器同一套
// 持久化（'spark_config'）——服务端读的是请求体里的 config，不另开一条存储。
function replicaSetConfigValue(key, value) {
    if (typeof config === 'undefined' || !config) return;
    config[key] = value;
    try {
        localStorage.setItem('spark_config', JSON.stringify(config));
    } catch (e) {
        console.warn('[replica] 配置写入 localStorage 失败', e);
    }
}

function replicaConfigValue(key, fallback) {
    const cfg = typeof config !== 'undefined' && config ? config : {};
    const v = cfg[key];
    return v === undefined || v === null || v === '' ? fallback : v;
}

// 抽帧密度档位。与 replica_pipeline.EXTRACT_FPS_CHOICES 同值同序；服务端会把认不出
// 的值回落到默认档，这里只负责把选项摆出来。
const REPLICA_FPS_CHOICES = [
    { value: 1, label: '1 fps（每秒 1 张，只看大轮廓）' },
    { value: 2, label: '2 fps（默认，延时片调过的基线）' },
    { value: 3, label: '3 fps' },
    { value: 4, label: '4 fps（慢工序推荐：刮腻子、铺砖）' },
    { value: 6, label: '6 fps（最密，抽帧耗时最长）' },
];
const REPLICA_DEFAULT_FPS = 2;

function replicaCurrentFps(state) {
    const v = state && state.sampling && state.sampling.base_fps;
    return REPLICA_FPS_CHOICES.some(c => c.value === Number(v)) ? Number(v) : REPLICA_DEFAULT_FPS;
}

// 单选框的 value → 后端档位名。'full' 这个 value 是三档之前留下来的（那时只有
// 完整/降级两档），后端叫它 'plan'——改 value 会让浏览器里缓存着旧页面的人静默换档，
// 所以在这里翻译，而不是去动 HTML。
const REPLICA_SCOPE_BY_MODE = { all: 'all', full: 'plan', degraded: 'degraded' };

function replicaScopeFromMode(value) {
    return REPLICA_SCOPE_BY_MODE[value] || 'plan';
}

function replicaFpsSelect(id, current) {
    return `<select id="${id}" class="replica-select">${REPLICA_FPS_CHOICES.map(c => `
        <option value="${c.value}" ${c.value === current ? 'selected' : ''}
        >${escapeHtmlReplica(c.label)}</option>`).join('')}</select>`;
}

/* --- API --- */

function replicaHeaders(json = true) {
    const code = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    const headers = {};
    if (json) headers['Content-Type'] = 'application/json';
    if (code) headers['X-Access-Code'] = code;
    return headers;
}

async function replicaFetch(url, options = {}) {
    const res = await fetch(url, options);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应下面统一报错 */ }
    if (!res.ok || (data && data.status === 'error')) {
        throw new Error((data && data.message) || `${res.status} ${res.statusText}`);
    }
    return data;
}

async function replicaLoadJobs() {
    const data = await replicaFetch('/api/replica/jobs', { headers: replicaHeaders() });
    replicaJobs = data.jobs || [];
    return replicaJobs;
}

async function replicaLoadJob(jobId) {
    const data = await replicaFetch(`/api/replica/status?job_id=${encodeURIComponent(jobId)}`,
        { headers: replicaHeaders() });
    replicaState = data.job_state;
    return replicaState;
}

/* --- 帧与拼图的 URL --- */
// 服务端给的是绝对文件路径；静态路由只认 /outputs 下的相对路径。变体 job 自己不存帧，
// 指回源 job 的目录。
function replicaFrameBase(state) {
    return `/outputs/replica_jobs/${state.variant_of || state.job_id}`;
}

// 服务端按磁盘上的实际位置给出 frame_urls；拿不到时才退回旧的猜法（老状态文件、
// 或者 /api/replica/jobs 那条精简行）。猜法本身是个隐患：目录布局是抽帧脚本的实现
// 细节，脚本一改目录名，证据帧就在最需要看图的地方碎成一片。
function replicaFrameUrl(state, name) {
    const known = (state.frame_urls || {})[name];
    if (known) return known;
    const dir = /^scene_/.test(name) ? 'storyboard' : 'review_frames';
    return `${replicaFrameBase(state)}/${dir}/${encodeURIComponent(name)}`;
}

function replicaCollageUrl(state) {
    const raw = state.overview && state.overview.collage;
    if (!raw) return null;
    const base = raw.split(/[\\/]/).pop();
    return `${replicaFrameBase(state)}/${encodeURIComponent(base)}`;
}

/* --- 渲染 --- */

function escapeHtmlReplica(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function replicaRoot() {
    return document.getElementById('replica-root');
}

function replicaRender() {
    const root = replicaRoot();
    if (!root) return;
    root.innerHTML = `
        ${replicaRenderUploader()}
        ${replicaRenderJobList()}
        ${replicaState ? replicaRenderJob(replicaState) : ''}
    `;
    replicaBindEvents();
    // 重建 DOM 会把按钮的 disabled 一起丢掉。跑着的时候重渲染（例如刚开跑要让「中断」
    // 露出来）之后不重新落一次 busy，整排按钮就又可点了——用户能在 Pass A 跑到一半时
    // 再点一次「开始反推」。
    if (replicaBusy) replicaSetBusy(true);
    // 重建 DOM 也会把进度条清空（它是 JS 直接写进去的，不在模板里）。跑着的时候
    // 重渲染之后必须把当前进度重新画一遍，否则一次 replicaRender() 就让进度归零。
    if (replicaSSE && replicaProgress) replicaProgressPaint();
}

function replicaRenderUploader() {
    return `
    <div class="replica-card replica-uploader">
        <div class="replica-card-title">上传成品视频</div>
        <p class="replica-hint">
            抽帧密度默认按延时视频调过（基线 2fps + 状态跳变密采 + 首尾密采）。慢工序
            （刮腻子、铺砖这类两张之间就跨过半个工序的）可以往上调——抽帧是本地 ffmpeg，
            不花模型钱，只是更慢。上传后会先给出待送审帧数与调用次数预估，确认了才开始烧钱。
        </p>
        <div class="replica-row">
            <input type="file" id="replica-file" accept="video/*" class="replica-file-input">
            <label class="replica-inline-field">抽帧密度
                ${replicaFpsSelect('replica-upload-fps', REPLICA_DEFAULT_FPS)}
            </label>
            <button type="button" id="replica-upload-btn" class="action-btn primary-btn">上传并抽帧</button>
        </div>
        <div id="replica-upload-status" class="replica-status"></div>
    </div>`;
}

function replicaRenderJobList() {
    if (!replicaJobs.length) return '';
    const rows = replicaJobs.map(job => `
        <div class="replica-job-row ${replicaState && replicaState.job_id === job.job_id ? 'active' : ''}">
            <button type="button" class="replica-job-open" data-job="${escapeHtmlReplica(job.job_id)}">
                <span class="replica-job-name">
                    ${job.variant_of ? '🧬 ' : '🎬 '}${escapeHtmlReplica(job.video_name || job.job_id)}
                </span>
                <span class="replica-chip">${escapeHtmlReplica(replicaStageLabel(job))}</span>
                ${job.beat_count ? `<span class="replica-chip">${job.beat_count} 拍</span>` : ''}
                ${job.error ? '<span class="replica-chip replica-chip-error">出错</span>' : ''}
            </button>
            <button type="button" class="replica-mini-btn" data-delete="${escapeHtmlReplica(job.job_id)}"
                    title="删除这个任务及其抽帧产物">删除</button>
        </div>`).join('');
    return `
    <div class="replica-card">
        <div class="replica-card-title">已有任务</div>
        <div class="replica-job-list">${rows}</div>
    </div>`;
}

function replicaRenderPhases(state) {
    // 页面上原先编着 ①②③④⑤，后台却有十二个 stage —— chip 显示「聚类节拍」时用户
    // 在页面上找不到任何一块对应它。这条阶梯把两者对齐：四个阶段，各自对应页面上
    // 真实存在的一块区域。
    const at = replicaPhaseIndex(state.stage);
    const failed = state.stage === 'audit_failed' || !!state.error;
    return `<ol class="replica-phases">${REPLICA_PHASES.map((p, i) => {
        const cls = i < at ? 'done' : (i === at ? (failed ? 'failed' : 'current') : 'todo');
        return `<li class="replica-phase ${cls}"><span class="replica-phase-dot">${i + 1}</span>
            <span class="replica-phase-label">${p.label}</span></li>`;
    }).join('')}</ol>`;
}

function replicaRenderJob(state) {
    return `
    <div class="replica-card">
        <div class="replica-card-title">
            ${state.variant_of ? '🧬 二创变体' : '🎬 1:1 复刻'} · ${escapeHtmlReplica(state.video_name || state.job_id)}
            <span class="replica-chip">${escapeHtmlReplica(replicaStageLabel(state))}</span>
        </div>
        ${replicaRenderPhases(state)}
        ${state.error ? `<div class="replica-banner replica-banner-error">${escapeHtmlReplica(state.error)}</div>` : ''}
        ${replicaRenderExtract(state)}
        ${replicaRenderProgress()}
        <div id="replica-beats-host">${replicaRenderBeats(state)}</div>
        ${replicaRenderOutput(state)}
    </div>`;
}

function replicaRenderExtract(state) {
    const ov = state.overview;
    if (!ov) return '';
    const collage = replicaCollageUrl(state);
    const est = state.cost_estimate || {};
    const full = est.full || {};
    const degraded = est.degraded || {};
    const every = est.all || {};
    // 采样档位选择框任何时候都渲染。
    //
    // 原先的判据是 `canStart = !hasBeats`：一旦跑出过节拍就再也看不到这对单选框，
    // 想换个档位重跑 Pass A 只能删任务重传。更糟的是首跑——上传后代码直接续跑
    // Pass A，这个框在首跑时压根没机会出现，于是「先看预估再决定」从来没发生过，
    // 每一单都默默走了完整档。现在 extract 停在 confirm_cost，这里就是那个卡点。
    const hasBeats = !!(state.beats && (state.beats.beats || []).length);
    // 重跑过一次的（已有帧事实或上次失败），标清楚这是重试而且不重付视觉调用的钱。
    const isRetry = !!state.error || !!state.facts;
    const atCostGate = state.stage === 'confirm_cost';
    const startLabel = atCostGate ? '确认并开始反推'
        : (isRetry ? '重试反推' : (hasBeats ? '换档位重跑反推' : '开始反推'));
    const scope = state.review_scope || (state.degraded ? 'degraded' : 'plan');
    const fps = replicaCurrentFps(state);

    return `
    <div class="replica-section">
        <div class="replica-card-title">抽帧结果</div>
        <div class="replica-metrics">
            <span>时长 ${ov.duration_sec ?? '—'}s</span>
            <span>抽帧 ${ov.frame_count ?? '—'} 张</span>
            <span>基线 ${fps} fps</span>
            <span>变化事件 ${ov.change_event_count ?? '—'} 个</span>
            <span>送审计划 ${(ov.analysis_plan || {}).mode || '—'}</span>
        </div>
        <div class="replica-reextract">
            <label class="replica-inline-field">抽帧密度
                ${replicaFpsSelect('replica-base-fps', fps)}
            </label>
            <button type="button" id="replica-reextract-btn" class="action-btn text-btn">
                按新密度重抽帧
            </button>
            <p class="replica-hint">
                抽出来的帧太少（事件之间的推进看不出来）就往上调一档再抽一次。抽帧不花
                模型钱，但<b>会作废这条任务已有的帧事实与节拍</b>：帧文件名是序号不是
                时间戳，换了密度同一个名字指向的是另一时刻的画面，所以旧的读数必须全丢。
            </p>
        </div>
        ${collage ? `<img class="replica-collage" id="replica-collage" src="${collage}"
             alt="关键帧拼贴图" title="点开看大图" loading="lazy">`
        : `<div class="replica-banner replica-banner-error">
            拼贴图缺失。它是节拍映射的前置门禁，缺了等于没看过整条序列就要定义节拍。</div>`}
        <div class="replica-cost">
            ${atCostGate ? `<p class="replica-hint replica-cost-gate">
                <b>抽帧已完成，还没有开始花钱。</b>Pass A 是整条线的成本大头——
                下面三档决定送多少帧给多模态模型，选好再按开始。
            </p>` : ''}
            <label class="replica-radio">
                <input type="radio" name="replica-mode" value="all" ${scope === 'all' ? 'checked' : ''}>
                <span>全部（${every.frame_count || 0} 帧 / 约 ${every.batch_count || 0} 次视觉调用）——
                      抽出来多少送多少，识别最密、也最贵</span>
            </label>
            <label class="replica-radio">
                <input type="radio" name="replica-mode" value="full" ${scope === 'plan' ? 'checked' : ''}>
                <span>计划（${full.frame_count || 0} 帧 / 约 ${full.batch_count || 0} 次）——
                      脚本的送审计划：长片只挑约四成 + 每秒至少一张</span>
            </label>
            <label class="replica-radio">
                <input type="radio" name="replica-mode" value="degraded" ${scope === 'degraded' ? 'checked' : ''}>
                <span>降级（${degraded.frame_count || 0} 帧 / 约 ${degraded.batch_count || 0} 次）——
                      只读事件的起止与峰值帧，<b>事件之间发生了什么全靠推断，节拍精度更低</b></span>
            </label>
            <p class="replica-hint">
                觉得「识别的图片太少」时先看这里：抽出来的 ${ov.frame_count ?? '—'} 张里，
                计划档只送 ${full.frame_count || 0} 张。要更密就选「全部」；抽出来的总数
                本身不够，则回上面调抽帧密度重抽。
            </p>
            <div class="replica-model-picker">
                <div class="replica-card-subtitle">反推模型</div>
                <label class="replica-inline-field">逐帧识别（Pass A）
                    ${replicaModelSelect('replica-frame-model',
                        replicaConfigValue('frameFactsModel', REPLICA_PASS_A_DEFAULT_MODEL))}
                </label>
                <label class="replica-inline-field">峰值帧复核
                    ${replicaModelSelect('replica-peak-model',
                        replicaConfigValue('peakVerifyModel', ''),
                        [{ value: '', label: '跟随主模型（默认）' },
                         { value: 'off', label: '关闭复核（省这几次调用）' }])}
                </label>
                <p class="replica-hint">
                    逐帧识别读的是材料标签、工具类型、完成范围这类细节，模型弱一档就会
                    读糊，而节拍阶梯完全建在这些读数上。便宜的 flash 打底 + 强模型复核
                    峰值帧是默认组合；对精度不满意就把逐帧识别也换成强模型，代价是
                    ${full.batch_count || 0} 次调用全部按强模型计价。
                    ${state.facts && state.facts.model
                        ? `上一轮实际用的是 <code>${escapeHtmlReplica(state.facts.model)}</code>。` : ''}
                </p>
            </div>
            ${full.peak_frame_count ? `<p class="replica-hint">
                另加 ${full.peak_batch_count || 0} 次峰值帧复核（${full.peak_frame_count} 张）。
                节拍边界恰好落在这几帧上，读糊了整条阶梯会整体错位，所以默认开。
            </p>` : ''}
            ${isRetry ? `<p class="replica-hint">
                ${state.facts ? `已读过 ${state.facts.frame_count || 0} 帧，帧事实走磁盘缓存——
                     同一档、同一个逐帧识别模型重试不重付视觉调用的钱；换到更密的一档只
                     补付新增的那些帧。<b>换了逐帧识别模型则会全部重读</b>（缓存按模型分桶，
                     不然新模型的钱付了、拿到的还是旧模型的读数），换回去仍然免费。`
                    : '上次没跑完，可以直接重试。'}
            </p>` : ''}
            ${hasBeats && !atCostGate ? `<p class="replica-hint">
                已经有一份节拍阶梯了。换档位重跑会覆盖它——你在下面改过的内容会丢。
            </p>` : ''}
            <button type="button" id="replica-start-btn" class="action-btn primary-btn">
                ${startLabel}
            </button>
        </div>
    </div>`;
}

function replicaRenderProgress() {
    // 跑着的时候要有个能按停的东西。后端的 cancel_event 通路一直都在，只是从来没有
    // 按钮去按它——一轮跑错了的 Pass A 此前只能干等它烧完。
    const running = !!replicaSSE;
    return `
    <div id="replica-progress" class="replica-progress" style="display:${running ? 'block' : 'none'};">
        <div class="replica-progress-head">
            <span class="replica-chip" id="replica-progress-stage"></span>
            <span id="replica-progress-label"></span>
            <span class="replica-progress-percent" id="replica-progress-percent"></span>
        </div>
        <div class="replica-progress-track"><div class="replica-progress-fill" id="replica-progress-fill"></div></div>
        <ul class="replica-progress-log" id="replica-progress-log"></ul>
    </div>
    ${running ? `<div class="replica-actions">
        <button type="button" id="replica-cancel-btn" class="action-btn text-btn">中断这一轮</button>
    </div>` : ''}`;
}

/* --- 进度模型 ---
 *
 * 复刻线最长的那一段（合成提示词）此前在这个页面上是**完全静默**的：合成器一路广播
 * outline / batch / batch_generating / beat_ready 这些事件，页面却只监听 replica_stage，
 * 于是用户看到的是「正在按 9 拍阶梯合成提示词…」这一句，然后干等好几分钟。
 * 这里把两路合并成一条进度：replica_stage 决定处在哪个大阶段（给出百分比区间），
 * 合成器的事件在 compose 那一段里给出段内进度，文案直接复用 ProgressModel
 * （js/progress_model.js，与主生成页同一套口径，不另抄一份）。
 */

// 每个 replica stage 在整条进度上的区间。
const REPLICA_STAGE_RANGE = {
    ingest: [0, 3], extract: [3, 15], confirm_cost: [15, 15],
    review_frames: [15, 45], cluster_beats: [45, 68], mutate_beats: [45, 68],
    review_beats: [68, 68],
    compose: [68, 94], audit: [94, 99], audit_failed: [99, 99], completed: [100, 100],
};

// 合成器自己的事件（progress_model 认得的那一套）。beat_ready 单独处理：它带的是
// 「第几拍的提示词已经产出」，是这一段里唯一能给出真实分子/分母的信号。
const REPLICA_COMPOSER_EVENTS = [
    'outline', 'batch', 'batch_generating', 'batch_generated', 'batch_retry',
    'batch_failed', 'compose_soft_timeout', 'audit', 'repair', 'beat_ready',
];

let replicaProgress = null;

function replicaResetProgress() {
    replicaProgress = {
        stage: '',
        range: [0, 100],
        percent: 0,
        label: '',
        log: [],
        composeState: (window.ProgressModel && window.ProgressModel.createProgressState('compose')) || null,
    };
}

function replicaProgressPaint() {
    const root = replicaRoot();
    const box = root && root.querySelector('#replica-progress');
    if (!box || !replicaProgress) return;
    box.style.display = 'block';
    const set = (id, text) => {
        const el = box.querySelector(id);
        if (el) el.textContent = text;
    };
    set('#replica-progress-stage', replicaStageLabel(replicaProgress.stage) || '进行中');
    set('#replica-progress-label', replicaProgress.label || '');
    set('#replica-progress-percent', `${Math.round(replicaProgress.percent)}%`);
    const fill = box.querySelector('#replica-progress-fill');
    if (fill) fill.style.width = `${Math.max(2, Math.min(100, replicaProgress.percent))}%`;
    const log = box.querySelector('#replica-progress-log');
    if (log) {
        log.innerHTML = replicaProgress.log
            .map(line => `<li>${escapeHtmlReplica(line)}</li>`).join('');
    }
}

// 百分比只增不减：几路事件交替到达时来回跳的进度条比没有进度条更让人不安。
function replicaProgressUpdate(percent, label, stage) {
    if (!replicaProgress) replicaResetProgress();
    if (stage) replicaProgress.stage = stage;
    if (Number.isFinite(percent)) {
        replicaProgress.percent = Math.max(replicaProgress.percent, Math.min(100, percent));
    }
    if (label && label !== replicaProgress.label) {
        replicaProgress.label = label;
        // 只留最近 6 条，且不重复上一条——批量合成会连着推很多条同文案的事件。
        if (replicaProgress.log[replicaProgress.log.length - 1] !== label) {
            replicaProgress.log.push(label);
            replicaProgress.log = replicaProgress.log.slice(-6);
        }
    }
    replicaProgressPaint();
}

function replicaHandleStageEvent(detail) {
    const stage = (detail && detail.stage) || '';
    const range = REPLICA_STAGE_RANGE[stage] || replicaProgress.range || [0, 100];
    replicaProgress.range = range;
    // 进入一个新阶段时先落到它的区间起点，段内进度由该阶段自己的事件推进。
    replicaProgressUpdate(range[0], (detail && detail.message) || '', stage);
}

function replicaHandleComposerEvent(type, detail) {
    if (!replicaProgress) replicaResetProgress();
    const [lo, hi] = REPLICA_STAGE_RANGE[replicaProgress.stage] || REPLICA_STAGE_RANGE.compose;

    if (type === 'beat_ready') {
        const total = Number(detail && detail.total) || 0;
        const index = Number(detail && detail.index) || 0;
        const ratio = total ? Math.min(1, index / total) : 0;
        replicaProgressUpdate(lo + ratio * (hi - lo),
            total ? `已产出第 ${index}/${total} 拍的提示词` : '提示词逐拍产出中');
        return;
    }
    if (!window.ProgressModel) return;
    const out = window.ProgressModel.normalizeGenerationProgress(
        type, detail, 'compose', replicaProgress.composeState);
    replicaProgress.composeState = out.state;
    replicaProgressUpdate(lo + (Number(out.percent) || 0) / 100 * (hi - lo), out.label);
}

function replicaRenderBeats(state) {
    const doc = state.beats;
    if (!doc || !(doc.beats || []).length) return '';
    const violations = state.validation || doc.validation || [];
    const errors = violations.filter(v => v.level === 'error');
    const warns = violations.filter(v => v.level !== 'error');
    // 2026-08-10：这里原先把 temporary_object_lingering 特判成"会让合成直接失败"并强制
    // 展开。那条冲突已经在源头修掉（reverse_engineered 让合成器豁免清场规则），现在它
    // 只是一条"原片里确实有、复刻会照实保留"的提示，不再预示失败。

    const banner = `
        ${errors.length ? `<div class="replica-banner replica-banner-error">
            <b>${errors.length} 项硬伤必须先修掉才能合成：</b>
            <ul>${errors.map(v => `<li>${escapeHtmlReplica(v.message)}</li>`).join('')}</ul>
        </div>` : `<div class="replica-banner replica-banner-ok">节拍阶梯已通过全部机械校验。</div>`}
        ${warns.length ? `<details class="replica-banner replica-banner-warn">
            <summary>${warns.length} 项待人工确认</summary>
            <ul>${warns.map(v => `<li>${escapeHtmlReplica(v.message)}</li>`).join('')}</ul>
        </details>` : ''}`;

    const cards = (doc.beats || []).map((beat, idx) => replicaRenderBeatCard(state, beat, idx)).join('');
    const banned = (doc.banned_elements || []).map(x => escapeHtmlReplica(x)).join('、');

    return `
    <div class="replica-section">
        <div class="replica-card-title">节拍阶梯（${doc.beats.length} 拍）——唯一的人工卡点</div>
        <p class="replica-hint">
            这是整条链路上唯一能拦住「模型脑补了一个不存在的工序」的地方。对着证据帧核对，
            改完记得保存——保存会立刻重跑一遍校验。
        </p>
        ${banner}
        <div class="replica-beats">${cards}</div>
        <div class="replica-section">
            <label class="replica-field-label">禁用元素（原片里不存在，出现在提示词里即须重写）</label>
            <textarea id="replica-banned" class="replica-textarea" rows="2"
                      placeholder="用、分隔">${banned}</textarea>
        </div>
        <div class="replica-actions">
            <button type="button" id="replica-save-btn" class="action-btn text-btn">保存并重校验</button>
            <button type="button" id="replica-recluster-btn" class="action-btn text-btn"
                    title="帧事实走缓存，不会重付视觉调用的钱">重跑聚类</button>
            <button type="button" id="replica-translate-btn" class="action-btn text-btn"
                    title="只翻译，不改英文原文。改过英文的字段中文会先作废，按这里补回来">重译中文</button>
            <button type="button" id="replica-compose-btn" class="action-btn primary-btn"
                    ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>合成提示词</button>
        </div>
        ${replicaRenderVariantForm(state)}
    </div>`;
}

function replicaRenderBeatCard(state, beat, idx) {
    const frames = beat.evidence_frames || beat.reference_frames || [];
    const isRef = !beat.evidence_frames && (beat.reference_frames || []).length;
    // 证据帧原地开灯箱，不再 target="_blank"。核对是「看一眼帧、回来改这一拍」的
    // 来回动作，每看一帧就多一个标签页，用户得自己收拾一地窗口才能回到编辑器。
    // 灯箱走全局的那一份（js/lightbox.js）：点空白处 / Esc / 关闭键都能返回，
    // 同一拍的多张帧还能左右翻。
    const thumbs = frames.map((name, at) => `
        <img class="replica-thumb" src="${replicaFrameUrl(state, name)}"
             alt="${escapeHtmlReplica(name)}" title="${escapeHtmlReplica(name)}" loading="lazy"
             data-lightbox-beat="${idx}" data-lightbox-at="${at}">`).join('');

    // 中文对照：反推产出的是英文（下游提示词、相位判定、banned 门禁读的都是它），
    // 但人工卡点是给人看的。zh 只在这里显示，永远不回写英文字段。
    const zh = beat.zh || {};
    const mirror = (key) => {
        const value = zh[key];
        const text = Array.isArray(value) ? value.join(' / ') : value;
        return text ? `<span class="replica-field-zh">${escapeHtmlReplica(text)}</span>` : '';
    };

    const field = (key, label, rows = 1) => `
        <label class="replica-field">
            <span class="replica-field-label">${label}</span>
            <textarea class="replica-textarea" rows="${rows}" data-beat="${idx}" data-key="${key}"
                >${escapeHtmlReplica(Array.isArray(beat[key]) ? beat[key].join('\n') : beat[key])}</textarea>
            ${mirror(key)}
        </label>`;

    return `
    <div class="replica-beat" data-beat-index="${idx}">
        <div class="replica-beat-head">
            <b>${escapeHtmlReplica(beat.id)}</b>
            <span class="replica-chip">${beat.start}s – ${beat.end}s</span>
            <span class="replica-chip" title="${escapeHtmlReplica(beat.stage || '')}">${escapeHtmlReplica(
                REPLICA_BEAT_STAGE_LABELS[beat.stage] || beat.stage || '未分类')}</span>
            ${beat.source_event_ids && beat.source_event_ids.length
                ? `<span class="replica-chip">事件 ${escapeHtmlReplica(beat.source_event_ids.join(','))}</span>` : ''}
            <span class="replica-chip">${beat.workers_present ? '有工人' : '清场帧（锚点候选）'}</span>
            ${typeof beat.confidence === 'number' && beat.confidence < 0.5
                ? '<span class="replica-chip replica-chip-error">低置信</span>' : ''}
            <span class="replica-beat-tools">
                <button type="button" class="replica-mini-btn" data-split="${idx}" title="从中点拆成两拍">拆拍</button>
                <button type="button" class="replica-mini-btn" data-merge="${idx}" ${idx === 0 ? 'disabled' : ''}
                        title="并入上一拍">上并</button>
            </span>
        </div>
        <div class="replica-thumbs">${thumbs || '<span class="replica-hint">无证据帧</span>'}</div>
        ${isRef ? '<p class="replica-hint">变体：这些帧只作运镜与构图参考，不再是事实断言。</p>' : ''}
        <div class="replica-beat-fields">
            ${field('visual_subject', '画面主体')}
            ${field('operation', '主导工序')}
            ${field('package_operations',
                    `工序包（一行一道，须 2~3 道；当前 ${(beat.package_operations || []).length} 道）`, 2)}
            ${field('visible_action', '可见动作', 2)}
            ${field('visible_result', '可见结果', 2)}
            ${field('state_before', '起始状态（须写具体空间完成范围）', 2)}
            ${field('state_after', '结束状态（须写具体空间完成范围）', 2)}
            ${field('persistent_traces', '遗留痕迹（一行一条）', 2)}
        </div>
    </div>`;
}

function replicaRenderVariantForm(state) {
    if (state.stage !== 'review_beats' && state.stage !== 'completed') return '';
    const boxes = REPLICA_AXES.map(a => `
        <label class="replica-axis">
            <input type="checkbox" class="replica-axis-box" value="${a.key}">
            <span><b>${a.label}</b><em>${a.hint}</em></span>
        </label>`).join('');
    return `
    <div class="replica-section replica-variant">
        <div class="replica-card-title">二创（可选）</div>
        <p class="replica-hint">
            骨架不动，只沿轴换内容。<b>最多同时变两轴</b>——三轴以上产出的已经不是「参考爆款」，
            而是一个新选题，那条路走选题发动机更合适。
        </p>
        <div class="replica-axes">${boxes}</div>
        <textarea id="replica-variant-brief" class="replica-textarea" rows="2"
                  placeholder="想变成什么？例如：换成废弃双层巴士，落在北欧雪地"></textarea>
        <button type="button" id="replica-variant-btn" class="action-btn primary-btn">生成二创节拍</button>
    </div>`;
}

function replicaRenderOutput(state) {
    if (!state.prompt_block) return '';
    const hits = state.banned_hits || [];
    const blocked = state.stage === 'audit_failed';
    // 提示词只活在这一页里的话，用户合成完就没有下一步了。两个去向都要写出来：
    // 一个是创意库（项目工作台能看到），一个是分步管线（真正渲染成片）。
    return `
    <div class="replica-section">
        <div class="replica-card-title">提示词包${state.title ? ` · ${escapeHtmlReplica(state.title)}` : ''}</div>
        ${blocked ? `<div class="replica-banner replica-banner-error">
            <b>已拦下交付：命中 ${hits.length} 个禁用元素</b>（原片里并不存在）：
            ${escapeHtmlReplica(hits.join('、'))}。<br>
            这份提示词<b>没有写入创意库</b>，也不能送去渲染——照它渲出来的画面里会长出原片
            没有的东西。两个改法：把这些表述从节拍阶梯里去掉后重新合成；
            或者你确认它们其实出现在原片里，那就把它们从「禁用元素」里删掉再重跑。
        </div>` : `<div class="replica-banner replica-banner-ok">
            已通过禁用元素门禁，并写入创意库${state.library_id
                ? `（项目工作台可见：${escapeHtmlReplica(state.title || state.library_id)}）` : ''}。
        </div>`}
        <div class="replica-actions">
            <button type="button" id="replica-copy-btn" class="action-btn text-btn">复制全部</button>
            ${blocked
                ? `<button type="button" id="replica-recompose-btn" class="action-btn primary-btn"
                           title="回到节拍阶梯改完后重新合成">重新合成</button>`
                : `<button type="button" id="replica-render-btn" class="action-btn primary-btn"
                           title="用这份已过门禁的提示词直接开分步渲染，不重新合成">送去分步管线渲染</button>`}
        </div>
        <pre class="replica-prompt-block" id="replica-prompt-block">${escapeHtmlReplica(state.prompt_block)}</pre>
    </div>`;
}

/* --- 事件绑定 --- */

function replicaBindEvents() {
    const root = replicaRoot();
    if (!root) return;

    const on = (sel, fn, evt = 'click') => {
        const el = typeof sel === 'string' ? root.querySelector(sel) : sel;
        if (el) el.addEventListener(evt, fn);
    };

    on('#replica-upload-btn', replicaUpload);
    on('#replica-start-btn', replicaStart);
    on('#replica-reextract-btn', replicaReExtract);
    // 模型选择改一下就落盘，不必等到点「开始反推」：用户很可能选完就去点了别的动作
    // （比如 recluster），那条路径读的是 config，没落盘就等于没选。
    on('#replica-frame-model', replicaCaptureReverseModels, 'change');
    on('#replica-peak-model', replicaCaptureReverseModels, 'change');
    on('#replica-recompose-btn', replicaCompose);
    on('#replica-render-btn', replicaSendToRender);
    on('#replica-cancel-btn', replicaCancelRun);
    on('#replica-copy-btn', () => {
        const block = root.querySelector('#replica-prompt-block');
        if (block) navigator.clipboard.writeText(block.textContent).then(
            () => replicaToast('已复制'), () => replicaToast('复制失败', true));
    });

    root.querySelectorAll('.replica-job-open').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                await replicaLoadJob(btn.dataset.job);
                replicaRender();
            } catch (e) { replicaToast(e.message, true); }
        });
    });

    root.querySelectorAll('[data-delete]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const jobId = btn.dataset.delete;
            // 删的是几百张抽帧产物，不可撤销——问一句。
            if (!window.confirm('删除这个任务？抽帧产物会一并清掉，不可恢复。')) return;
            try {
                await replicaFetch('/api/replica/delete', {
                    method: 'POST', headers: replicaHeaders(),
                    body: JSON.stringify({ job_id: jobId }),
                });
                if (replicaState && replicaState.job_id === jobId) replicaState = null;
                await replicaLoadJobs();
                replicaRender();
                replicaToast('已删除');
            } catch (e) { replicaToast(e.message, true); }
        });
    });

    on('#replica-collage', () => replicaOpenLightbox([{
        url: replicaCollageUrl(replicaState),
        caption: '关键帧拼贴图（整条序列的一览）',
    }], 0));

    replicaBindBeatEvents(root);
}

// 原地开图：点空白处 / Esc / 关闭键返回，多张帧可左右翻。
// 走全局灯箱（js/lightbox.js），不自造第二个——控制台与主页面已经共用它了。
// 灯箱不可用时退回新窗口，宁可多一个标签页也不能点了没反应。
function replicaOpenLightbox(items, index) {
    const usable = (items || []).filter(x => x && x.url);
    if (!usable.length) return;
    if (typeof openLightbox === 'function') {
        openLightbox(usable, Math.max(0, Math.min(index || 0, usable.length - 1)));
        return;
    }
    window.open(usable[Math.max(0, index || 0)].url, '_blank');
}

// 节拍区自己的绑定，单独一函数。
//
// 这样 replicaRefreshBeats 可以只重建节拍区、只重绑节拍区的监听。整页重建加整页重绑
// 会在未变的节点上叠加第二份监听（点一次触发两次），而节拍区里恰恰是拆拍/合拍这种
// 重复执行会直接改坏数据的操作。
function replicaBindBeatEvents(scope) {
    if (!scope) return;
    const on = (sel, fn, evt = 'click') => {
        const el = scope.querySelector(sel);
        if (el) el.addEventListener(evt, fn);
    };

    on('#replica-save-btn', (e) => replicaSaveBeats(true, e.currentTarget));
    on('#replica-recluster-btn', (e) => replicaAdvance('recluster', {}, e.currentTarget));
    // 先落盘再翻译：翻译在服务端读的是已保存的那一份，不先保存就会翻旧的英文。
    on('#replica-translate-btn', async (e) => {
        const btn = e.currentTarget;
        if (!(await replicaSaveBeats(false, btn))) {
            replicaToast('保存失败，未翻译', true);
            return;
        }
        replicaAdvance('translate', {}, btn);
    });
    on('#replica-compose-btn', (e) => replicaCompose(e.currentTarget));
    on('#replica-variant-btn', (e) => replicaVariant(e.currentTarget));

    // 证据帧：原地开灯箱。一拍的几张帧作为一组传进去，左右方向键就能在同一拍内翻。
    scope.querySelectorAll('[data-lightbox-beat]').forEach(img => {
        img.addEventListener('click', () => {
            const beats = ((replicaState || {}).beats || {}).beats || [];
            const beat = beats[parseInt(img.dataset.lightboxBeat, 10)];
            if (!beat) return;
            const frames = beat.evidence_frames || beat.reference_frames || [];
            replicaOpenLightbox(frames.map(name => ({
                url: replicaFrameUrl(replicaState, name),
                caption: `${beat.id || ''} ${name}`,
            })), parseInt(img.dataset.lightboxAt, 10) || 0);
        });
    });

    scope.querySelectorAll('[data-split]').forEach(btn => {
        btn.addEventListener('click', () => replicaSplitBeat(parseInt(btn.dataset.split, 10)));
    });
    scope.querySelectorAll('[data-merge]').forEach(btn => {
        btn.addEventListener('click', () => replicaMergeBeat(parseInt(btn.dataset.merge, 10)));
    });

    // 编辑即写回内存 model。原先是每次动作前用 replicaCollectBeats 全量扫一遍 DOM 反推
    // 出文档——那要求 DOM 必须一直是唯一真相，于是任何局部刷新都不敢做，只能整页重建，
    // 滚动位置和焦点每次都丢。现在 model 是真相，DOM 只是它的投影。
    scope.querySelectorAll('[data-beat][data-key]').forEach(el => {
        el.addEventListener('input', () => {
            const beat = ((replicaState || {}).beats || {}).beats || [];
            const target = beat[parseInt(el.dataset.beat, 10)];
            if (!target) return;
            const key = el.dataset.key;
            // 数组字段按键名判定，不看当前值的类型：老任务里可能压根没有这个键
            // （undefined 不是数组），一次编辑就会把一个数组字段写成字符串，
            // 保存时 schema 校验直接判死。
            target[key] = (REPLICA_LIST_FIELDS.has(key) || Array.isArray(target[key]))
                ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
                : el.value.trim();
        });
    });

    const banned = scope.querySelector('#replica-banned');
    if (banned) {
        banned.addEventListener('input', () => {
            if (!replicaState || !replicaState.beats) return;
            replicaState.beats.banned_elements = replicaSplitList(banned.value);
        });
    }

    // 只在轴数超上限时拦一下，别默默改用户的勾选——用户自己取消一个，比我们替他决定
    // 丢掉哪一个要好。
    scope.querySelectorAll('.replica-axis-box').forEach(box => {
        box.addEventListener('change', () => {
            const checked = scope.querySelectorAll('.replica-axis-box:checked');
            if (checked.length > REPLICA_MAX_AXES) {
                box.checked = false;
                replicaToast(`最多同时变 ${REPLICA_MAX_AXES} 条轴`, true);
            }
        });
    });
}

// 只重建节拍区，不动页面其余部分，并把滚动位置放回去。
function replicaRefreshBeats() {
    const host = replicaRoot() && replicaRoot().querySelector('#replica-beats-host');
    if (!host) { replicaRender(); return; }
    const top = window.scrollY;
    host.innerHTML = replicaRenderBeats(replicaState);
    replicaBindBeatEvents(host);
    if (replicaBusy) replicaSetBusy(true);
    window.scrollTo({ top });
}

// 禁用元素的分隔符。UI 提示"用、分隔"，而原先的解析是 /[、,\n]/ —— 不含全角逗号，
// 用户打一个「，」整串就塌成一个元素，然后禁用清单静默失效。
function replicaSplitList(text) {
    return String(text || '').split(/[、，,;；\n]/).map(s => s.trim()).filter(Boolean);
}

// 反馈打在固定浮层上，而不是页面顶端上传卡片里的那个 #replica-upload-status。
// 原先所有提示——包括你在页面底部点「保存并重校验」得到的那句——都写进上传卡片，
// 几十拍的编辑器一撑开，它就在视口外了：操作看上去毫无反应。
let replicaToastTimer = null;

function replicaToast(msg, isError) {
    if (isError) console.error('[replica]', msg);
    let el = document.getElementById('replica-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'replica-toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = `replica-toast show ${isError ? 'is-error' : ''}`;
    if (replicaToastTimer) clearTimeout(replicaToastTimer);
    // 报错留久一点：它通常带着"照着改"的指路，一闪而过等于没说。
    replicaToastTimer = setTimeout(() => { el.className = 'replica-toast'; },
                                   isError ? 12000 : 5000);
}

// 转圈只加在**触发操作的那个按钮**上。原先是整页 disable：几十个按钮一起变灰，
// 用户既看不出是哪一步在跑，也无法取消。
function replicaSetBusy(busy, activeBtn) {
    replicaBusy = busy;
    const root = replicaRoot();
    if (!root) return;
    root.querySelectorAll('button').forEach(b => {
        // 取消按钮必须在跑的时候还能按——它存在的全部意义就是打断正在跑的东西。
        if (b.id === 'replica-cancel-btn') return;
        b.disabled = busy || b.hasAttribute('data-perm-disabled');
        b.classList.toggle('is-running', busy && b === activeBtn);
    });
}

/* --- 动作 --- */

async function replicaUpload() {
    const input = replicaRoot().querySelector('#replica-file');
    const file = input && input.files && input.files[0];
    if (!file) { replicaToast('请先选择一个视频文件', true); return; }
    const fpsEl = replicaRoot().querySelector('#replica-upload-fps');
    const baseFps = fpsEl ? Number(fpsEl.value) : REPLICA_DEFAULT_FPS;

    replicaSetBusy(true);
    replicaToast(`正在上传 ${file.name}（${(file.size / 1048576).toFixed(1)} MB）…`);
    try {
        const form = new FormData();
        form.append('video', file, file.name);
        const data = await replicaFetch('/api/replica/upload', {
            method: 'POST', headers: replicaHeaders(false), body: form,
        });
        replicaState = data.job_state;
        await replicaLoadJobs();
        replicaRender();
        replicaToast(data.reused
            ? '这条视频之前已经抽过帧，直接复用旧任务（抽帧是几分钟的 ffmpeg，不必重跑）'
            : '上传完成，开始抽帧。抽完会先给出成本预估，确认了才进入反推。');
        // 只续抽帧，不续 Pass A。原先这里 `await replicaStart()` 一路跑到 Pass B，
        // 成本预估在中途作为一行文案闪过，用户没有任何机会介入——那正是「先确认再
        // 烧钱」这道卡点形同虚设的原因。
        if (!data.reused) await replicaExtract(baseFps);
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

async function replicaExtract(baseFps) {
    if (!replicaState) return;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/extract', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({
                job_id: replicaState.job_id,
                base_fps: baseFps == null ? undefined : Number(baseFps),
                config: replicaConfig(),
            }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

// 换密度重抽帧。已有的帧事实与节拍会作废（帧名是序号不是时间戳，见后端
// _purge_extract_products），所以先问一句再动。
async function replicaReExtract() {
    if (!replicaState) return;
    const fpsEl = replicaRoot().querySelector('#replica-base-fps');
    const baseFps = fpsEl ? Number(fpsEl.value) : REPLICA_DEFAULT_FPS;
    if (baseFps === replicaCurrentFps(replicaState)) {
        replicaToast('抽帧密度没变，不必重抽。先在左边选一个新的密度。', true);
        return;
    }
    const hasWork = !!(replicaState.facts
        || (replicaState.beats && (replicaState.beats.beats || []).length));
    if (hasWork && !window.confirm(
        `按 ${baseFps}fps 重抽帧？这条任务已有的帧事实与节拍会一并作废（帧文件名是序号，`
        + '换了密度就对不上原来的时刻），Pass A 需要重跑、重新付费。')) return;
    await replicaExtract(baseFps);
}

// 反推段的模型选择：先落进全局 config（写 localStorage），再随请求体发出去。
// 落盘是为了下一条任务、下一次开页面还是这个选择——不落的话每次都回默认值，
// 用户会以为自己选过了。
function replicaCaptureReverseModels() {
    const root = replicaRoot();
    if (!root) return;
    const frameEl = root.querySelector('#replica-frame-model');
    if (frameEl) replicaSetConfigValue('frameFactsModel', frameEl.value);
    const peakEl = root.querySelector('#replica-peak-model');
    // 空值 = 跟随主模型：写空字符串，reverse._peak_verify_model 会回落到 config.model。
    if (peakEl) replicaSetConfigValue('peakVerifyModel', peakEl.value);
}

async function replicaStart() {
    if (!replicaState) return;
    const modeEl = replicaRoot().querySelector('input[name="replica-mode"]:checked');
    const scope = replicaScopeFromMode(modeEl && modeEl.value);
    replicaCaptureReverseModels();
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/start', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({
                job_id: replicaState.job_id, scope,
                degraded: scope === 'degraded',   // 老服务端只认这个键
                config: replicaConfig(),
            }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaAdvance(action, payload = {}, btn) {
    if (!replicaState) return;
    replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/advance', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, action, payload, config: replicaConfig() }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
        replicaRender();   // 让「中断这一轮」露出来
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaCompose(btn) {
    // 合成前先把编辑器里的改动落盘：不然用户改了半天，合成用的还是磁盘上的旧节拍。
    const saved = await replicaSaveBeats(false);
    if (!saved) return;
    replicaAdvance('approve', {}, btn instanceof HTMLElement ? btn : undefined);
}

async function replicaSendToRender() {
    if (!replicaState) return;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/handoff', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, config: replicaConfig() }),
        });
        // 交接之后这条任务就归分步管线了——复刻页不接管它的进度，把用户送过去。
        replicaToast(`已送去分步管线：${data.title || ''}。` +
            (data.reused_compose ? '沿用了已过门禁的提示词，未重新合成。' : '') +
            '去「分步管线」页看渲染进度。');
        if (typeof switchMainTab === 'function') switchMainTab('stepped');
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

function replicaVariant(btn) {
    const root = replicaRoot();
    const axes = Array.from(root.querySelectorAll('.replica-axis-box:checked')).map(b => b.value);
    if (!axes.length) { replicaToast('至少勾一条变异轴', true); return; }
    const brief = (root.querySelector('#replica-variant-brief') || {}).value || '';
    replicaAdvance('variant', { axes, brief }, btn instanceof HTMLElement ? btn : undefined);
}

function replicaConfig() {
    // `config` 是 js/state.js 的全局配置对象。复刻这条线只用到里面的网关与模型字段；
    // 取不到就交空对象，服务端的 effective_config 会补上服务端权威配置。
    return typeof config !== 'undefined' && config ? config : {};
}

/* --- 节拍编辑 --- */

// model 就是真相：textarea 的 input 事件已经把每一次编辑写回 replicaState.beats
// （见 replicaBindBeatEvents）。这里只做一次深拷贝，不再全量扫 DOM 反推文档。
function replicaCollectBeats() {
    if (!replicaState || !replicaState.beats) return null;
    return JSON.parse(JSON.stringify(replicaState.beats));
}

async function replicaSaveBeats(rerender, btn) {
    const doc = replicaCollectBeats();
    if (!doc) return false;
    if (btn) replicaSetBusy(true, btn);
    try {
        const data = await replicaFetch('/api/replica/beats', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, beats: doc }),
        });
        replicaState = data.job_state;
        if (rerender) {
            // 只重建节拍区。整页重建会把用户滚到的位置一起丢掉，而节拍区恰恰是这一页
            // 最长的一块——几十拍改到一半被弹回顶端，等于每保存一次就罚一次。
            replicaRefreshBeats();
            const errors = (data.validation || []).filter(v => v.level === 'error');
            replicaToast(errors.length ? `已保存，仍有 ${errors.length} 项硬伤` : '已保存，校验通过');
        }
        return true;
    } catch (e) {
        replicaToast(e.message, true);
        return false;
    } finally {
        if (btn) replicaSetBusy(false);
    }
}

// 拆拍 / 合拍：改完立刻落盘，并用服务端重排过 id 的那一份回写。
//
// 原先只改本地就 replicaRender()，服务端的 _renumber_beats 没跑过——拆完页面上会
// 并排出现两个 B03，用户还得记着再点一次「保存」。落盘顺带把校验也重跑了，
// 「这一刀拆出了什么问题」当场就能看见。
async function replicaPersistBeats(message) {
    const ok = await replicaSaveBeats(false);
    replicaRefreshBeats();
    replicaToast(ok ? message : '改动只在本地，落盘失败——请再点一次「保存并重校验」', !ok);
}

function replicaSplitBeat(idx) {
    const doc = replicaCollectBeats();
    if (!doc) return;
    const beat = doc.beats[idx];
    const mid = Math.round(((beat.start + beat.end) / 2) * 1000) / 1000;
    if (mid <= beat.start || mid >= beat.end) { replicaToast('拍窗太短，拆不开', true); return; }
    const second = JSON.parse(JSON.stringify(beat));
    beat.end = mid;
    second.start = mid;
    // 事件与证据帧不自动分配：谁属于哪一半只有看过帧才知道，替用户猜等于制造假事实。
    second.source_event_ids = [];
    doc.beats.splice(idx + 1, 0, second);
    replicaState.beats = doc;
    replicaPersistBeats('已拆成两拍并保存。事件与证据帧没有自动分配——请对着帧手工分给正确的那一半。');
}

function replicaMergeBeat(idx) {
    const doc = replicaCollectBeats();
    if (!doc || idx === 0) return;
    const prev = doc.beats[idx - 1];
    const cur = doc.beats[idx];
    prev.end = cur.end;
    prev.state_after = cur.state_after;
    prev.visible_result = cur.visible_result;
    prev.source_event_ids = [...(prev.source_event_ids || []), ...(cur.source_event_ids || [])];
    prev.evidence_frames = [...(prev.evidence_frames || []), ...(cur.evidence_frames || [])];
    prev.persistent_traces = [...(prev.persistent_traces || []), ...(cur.persistent_traces || [])];
    doc.beats.splice(idx, 1);
    replicaState.beats = doc;
    replicaPersistBeats('已合并并保存。若两拍是不同的物理工序，上方校验会告诉你。');
}

/* --- SSE --- */

// 服务端的 SSE 帧统一是 {"type": ..., "data": ...}（见 server._open_sse_stream）。
// 此前这个文件到处直接读 JSON.parse(e.data).stage / .message —— 读的是信封而不是信，
// 恒为 undefined：进度框里那个 chip 一直是空的、失败提示恒为「任务失败」四个字。
// 老格式（不带信封）也一并兼容，免得 replay 的历史事件形态不同就炸。
function replicaEventPayload(event) {
    if (!event || !event.data) return null;
    let parsed;
    try {
        parsed = JSON.parse(event.data);
    } catch (err) {
        return null;
    }
    if (parsed && typeof parsed === 'object' && 'type' in parsed && 'data' in parsed) {
        return parsed.data;
    }
    return parsed;
}

function replicaOpenSSE(taskId) {
    if (replicaSSE) replicaSSE.close();
    const code = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    replicaSSE = new EventSource(
        `/api/compose-stream?task_id=${encodeURIComponent(taskId)}${code ? '&access_code=' + encodeURIComponent(code) : ''}`);

    replicaResetProgress();

    replicaSSE.addEventListener('replica_stage', (e) => {
        replicaHandleStageEvent(replicaEventPayload(e) || {});
    });

    // 合成器的事件此前没有任何监听器：整个 compose 阶段（复刻线最长的一段）在页面上
    // 表现为一句话之后长时间静止，用户无从判断是在跑还是已经卡死。
    REPLICA_COMPOSER_EVENTS.forEach(type => {
        replicaSSE.addEventListener(type, (e) => {
            replicaHandleComposerEvent(type, replicaEventPayload(e));
        });
    });

    const finish = async (msg, isError) => {
        if (replicaSSE) { replicaSSE.close(); replicaSSE = null; }
        replicaTaskId = null;
        replicaSetBusy(false);
        try {
            await replicaLoadJobs();
            // 变体会派生出新 job_id，跟过去而不是停在源任务上。
            const newest = replicaJobs[0];
            const target = (newest && newest.variant_of === (replicaState || {}).job_id)
                ? newest.job_id : (replicaState || {}).job_id;
            if (target) await replicaLoadJob(target);
        } catch (err) { /* 列表刷新失败不该盖掉上面的完成/失败提示 */ }
        replicaRender();
        if (msg) replicaToast(msg, isError);
    };

    // 停下来了，但停在哪个卡点决定了下一步该干什么。原先三个卡点共用一句"请核对节拍"，
    // 停在成本确认或门禁未过时那句话是错的。
    const PAUSE_MESSAGES = {
        confirm_cost: '抽帧完成，还没开始花钱。选好采样档位再按「确认并开始反推」。',
        review_beats: '已停在人工卡点，请对着证据帧核对节拍。',
        audit_failed: '命中禁用元素，已拦下交付（未入库）。见下方提示词区。',
    };
    replicaSSE.addEventListener('replica_paused', (e) => {
        const stage = ((replicaEventPayload(e) || {}).stage) || '';
        finish(PAUSE_MESSAGES[stage] || '任务已暂停，请看当前阶段的操作区。',
               stage === 'audit_failed');
    });
    replicaSSE.addEventListener('result', () => finish('提示词包已生成，并已写入创意库'));
    replicaSSE.addEventListener('error', (e) => {
        // EventSource 把两种完全不同的东西塞进同一个事件名：服务端发的 `error` 事件
        // （带 data，是真的任务失败），和浏览器的连接层错误（无 data，重连期间也会
        // 触发）。不区分就会在一次网络抖动后把用户的任务报成失败。
        if (!e.data) {
            if (replicaSSE && replicaSSE.readyState === EventSource.CLOSED) {
                finish('与服务端的连接已断开，正在拉取最新状态');
            }
            return;
        }
        finish((replicaEventPayload(e) || {}).message || '任务失败', true);
    });
}

/* --- 入口 --- */

async function replicaTabEntered() {
    try {
        await replicaLoadJobs();
        if (!replicaState && replicaJobs.length) {
            await replicaLoadJob(replicaJobs[0].job_id);
        }
    } catch (e) {
        console.error('replicaTabEntered', e);
    }
    replicaRender();
    replicaReattach();
}

// 刷新页面 / 切走再回来之后，把还在跑的任务接回来。
//
// 在此之前这里什么都不做：一个跑了十五分钟的 Pass A，刷新一次就彻底失联——页面不显示
// 任何"在跑"的迹象，还会摆出「开始反推」按钮，用户再点一次就把同一笔视觉调用付两遍。
// job 行上的 active_task_id 由 /api/replica/jobs 下发（服务端按 replica_job_id 在
// ACTIVE_TASKS 里找 running 的那条）。
function replicaReattach() {
    if (replicaSSE || !replicaState) return;
    const row = replicaJobs.find(j => j.job_id === replicaState.job_id);
    const taskId = row && row.active_task_id;
    if (!taskId) return;
    replicaTaskId = taskId;
    replicaSetBusy(true);
    replicaOpenSSE(taskId);
    replicaRender();   // 让「中断这一轮」和进度条在重连后立刻可见
    replicaToast('这条任务还在后台跑，已重新接上进度');
}

async function replicaCancelRun() {
    if (!replicaState) return;
    if (!window.confirm('中断正在跑的这一轮？已经读过的帧会留在缓存里，重试时不重复付费。')) return;
    try {
        await replicaFetch('/api/replica/cancel', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id }),
        });
        replicaToast('已请求中断，正在等当前这一步收尾…');
    } catch (e) {
        replicaToast(e.message, true);
    }
}
