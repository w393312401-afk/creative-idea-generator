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

const REPLICA_STAGE_LABELS = {
    ingest: '已上传',
    extract: '抽帧中',
    review_frames: '逐帧读取',
    cluster_beats: '聚类节拍',
    review_beats: '待人工核对',
    mutate_beats: '二创改写中',
    compose: '合成提示词',
    audit: '门禁校验',
    completed: '已完成',
    cancelled: '已取消',
};

const REPLICA_AXES = [
    { key: 'carrier', label: '载体替换', hint: '石屋 → 废弃巴士 / 船舱 / 地窖' },
    { key: 'environment', label: '地域环境', hint: '江南 → 北欧 / 沙漠' },
    { key: 'material', label: '材质风格', hint: '木作 → 清水混凝土' },
    { key: 'pacing', label: '节奏拍数', hint: '六拍 8s → 四拍 10s' },
    { key: 'reward', label: '结局奖励', hint: '工作室 → 茶室 / 民宿' },
];

const REPLICA_MAX_AXES = 2;

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

function replicaFrameUrl(state, name) {
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
}

function replicaRenderUploader() {
    return `
    <div class="replica-card replica-uploader">
        <div class="replica-card-title">① 上传成品视频</div>
        <p class="replica-hint">
            抽帧密度按延时视频调过（基线 2fps + 状态跳变密采 + 首尾密采），不需要你调参数。
            上传后会先给出待送审帧数与调用次数预估，确认了才开始烧钱。
        </p>
        <div class="replica-row">
            <input type="file" id="replica-file" accept="video/*" class="replica-file-input">
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
                <span class="replica-chip">${escapeHtmlReplica(REPLICA_STAGE_LABELS[job.stage] || job.stage)}</span>
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

function replicaRenderJob(state) {
    return `
    <div class="replica-card">
        <div class="replica-card-title">
            ${state.variant_of ? '🧬 二创变体' : '🎬 1:1 复刻'} · ${escapeHtmlReplica(state.video_name || state.job_id)}
            <span class="replica-chip">${escapeHtmlReplica(REPLICA_STAGE_LABELS[state.stage] || state.stage)}</span>
        </div>
        ${state.error ? `<div class="replica-banner replica-banner-error">${escapeHtmlReplica(state.error)}</div>` : ''}
        ${replicaRenderExtract(state)}
        ${replicaRenderProgress()}
        ${replicaRenderBeats(state)}
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
    // 判据是「有没有节拍」，不是 stage 白名单。按 stage 白名单渲染会让任何在抽帧之后
    // 失败的任务（Pass A 超时、Pass B 解析失败…）变成死胡同：错误看得见，却没有任何
    // 按钮能往下走，只能删任务重传。
    const hasBeats = !!(state.beats && (state.beats.beats || []).length);
    const canStart = !hasBeats;
    // 重跑过一次的（已有帧事实或上次失败），标清楚这是重试而且不重付视觉调用的钱。
    const isRetry = !!state.error || !!state.facts;

    return `
    <div class="replica-section">
        <div class="replica-card-title">② 抽帧结果</div>
        <div class="replica-metrics">
            <span>时长 ${ov.duration_sec ?? '—'}s</span>
            <span>抽帧 ${ov.frame_count ?? '—'} 张</span>
            <span>变化事件 ${ov.change_event_count ?? '—'} 个</span>
            <span>送审计划 ${(ov.analysis_plan || {}).mode || '—'}</span>
        </div>
        ${collage ? `<a href="${collage}" target="_blank" class="replica-collage-link">
            <img class="replica-collage" src="${collage}" alt="关键帧拼贴图" loading="lazy">
        </a>` : `<div class="replica-banner replica-banner-error">
            拼贴图缺失。它是节拍映射的前置门禁，缺了等于没看过整条序列就要定义节拍。</div>`}
        ${canStart ? `
        <div class="replica-cost">
            <label class="replica-radio">
                <input type="radio" name="replica-mode" value="full" ${state.degraded ? '' : 'checked'}>
                <span>完整（${full.frame_count || 0} 帧 / 约 ${full.batch_count || 0} 次视觉调用）</span>
            </label>
            <label class="replica-radio">
                <input type="radio" name="replica-mode" value="degraded" ${state.degraded ? 'checked' : ''}>
                <span>降级（${degraded.frame_count || 0} 帧 / 约 ${degraded.batch_count || 0} 次）——
                      只读事件的起止与峰值帧，<b>事件之间发生了什么全靠推断，节拍精度更低</b></span>
            </label>
            ${isRetry ? `<p class="replica-hint">
                ${state.facts ? `已读过 ${state.facts.frame_count || 0} 帧，帧事实走磁盘缓存——
                     重试只重跑聚类，不会重付视觉调用的钱。` : '上次没跑完，可以直接重试。'}
            </p>` : ''}
            <button type="button" id="replica-start-btn" class="action-btn primary-btn">
                ${isRetry ? '重试反推' : '开始反推'}
            </button>
        </div>` : ''}
    </div>`;
}

function replicaRenderProgress() {
    return `<div id="replica-progress" class="replica-progress" style="display:none;"></div>`;
}

function replicaRenderBeats(state) {
    const doc = state.beats;
    if (!doc || !(doc.beats || []).length) return '';
    const violations = state.validation || doc.validation || [];
    const errors = violations.filter(v => v.level === 'error');
    const warns = violations.filter(v => v.level !== 'error');
    // 这一类告警不是"可以再想想"，而是"照这样按下去合成一定会被打回"。折叠起来等于没提示。
    const predictsFailure = warns.some(v => v.code === 'temporary_object_lingering');

    const banner = `
        ${errors.length ? `<div class="replica-banner replica-banner-error">
            <b>${errors.length} 项硬伤必须先修掉才能合成：</b>
            <ul>${errors.map(v => `<li>${escapeHtmlReplica(v.message)}</li>`).join('')}</ul>
        </div>` : `<div class="replica-banner replica-banner-ok">节拍阶梯已通过全部机械校验。</div>`}
        ${warns.length ? `<details class="replica-banner replica-banner-warn" ${predictsFailure ? 'open' : ''}>
            <summary>${predictsFailure
                ? `${warns.length} 项待确认，其中有一项会让合成直接失败`
                : `${warns.length} 项待人工确认`}</summary>
            <ul>${warns.map(v => `<li>${escapeHtmlReplica(v.message)}</li>`).join('')}</ul>
        </details>` : ''}`;

    const cards = (doc.beats || []).map((beat, idx) => replicaRenderBeatCard(state, beat, idx)).join('');
    const banned = (doc.banned_elements || []).map(x => escapeHtmlReplica(x)).join('、');

    return `
    <div class="replica-section">
        <div class="replica-card-title">③ 节拍阶梯（${doc.beats.length} 拍）——唯一的人工卡点</div>
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
            <button type="button" id="replica-compose-btn" class="action-btn primary-btn"
                    ${errors.length ? 'disabled title="先修掉硬伤"' : ''}>合成提示词</button>
        </div>
        ${replicaRenderVariantForm(state)}
    </div>`;
}

function replicaRenderBeatCard(state, beat, idx) {
    const frames = beat.evidence_frames || beat.reference_frames || [];
    const isRef = !beat.evidence_frames && (beat.reference_frames || []).length;
    const thumbs = frames.map(name => `
        <a href="${replicaFrameUrl(state, name)}" target="_blank" title="${escapeHtmlReplica(name)}">
            <img class="replica-thumb" src="${replicaFrameUrl(state, name)}" alt="${escapeHtmlReplica(name)}" loading="lazy">
        </a>`).join('');

    const field = (key, label, rows = 1) => `
        <label class="replica-field">
            <span class="replica-field-label">${label}</span>
            <textarea class="replica-textarea" rows="${rows}" data-beat="${idx}" data-key="${key}"
                >${escapeHtmlReplica(Array.isArray(beat[key]) ? beat[key].join('\n') : beat[key])}</textarea>
        </label>`;

    return `
    <div class="replica-beat" data-beat-index="${idx}">
        <div class="replica-beat-head">
            <b>${escapeHtmlReplica(beat.id)}</b>
            <span class="replica-chip">${beat.start}s – ${beat.end}s</span>
            <span class="replica-chip">${escapeHtmlReplica(beat.stage || '未分类')}</span>
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
        <div class="replica-card-title">④ 二创（可选）</div>
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
    return `
    <div class="replica-section">
        <div class="replica-card-title">⑤ 提示词包${state.title ? ` · ${escapeHtmlReplica(state.title)}` : ''}</div>
        ${hits.length ? `<div class="replica-banner replica-banner-error">
            命中 ${hits.length} 个禁用元素（原片里并不存在）：${escapeHtmlReplica(hits.join('、'))}。
            交付前必须重写这些表述。</div>` : ''}
        <div class="replica-actions">
            <button type="button" id="replica-copy-btn" class="action-btn text-btn">复制全部</button>
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
    on('#replica-save-btn', () => replicaSaveBeats(true));
    on('#replica-recluster-btn', () => replicaAdvance('recluster'));
    on('#replica-compose-btn', replicaCompose);
    on('#replica-variant-btn', replicaVariant);
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

    root.querySelectorAll('[data-split]').forEach(btn => {
        btn.addEventListener('click', () => replicaSplitBeat(parseInt(btn.dataset.split, 10)));
    });
    root.querySelectorAll('[data-merge]').forEach(btn => {
        btn.addEventListener('click', () => replicaMergeBeat(parseInt(btn.dataset.merge, 10)));
    });

    // 只在轴数超上限时拦一下，别默默改用户的勾选——用户自己取消一个，比我们替他决定
    // 丢掉哪一个要好。
    root.querySelectorAll('.replica-axis-box').forEach(box => {
        box.addEventListener('change', () => {
            const checked = root.querySelectorAll('.replica-axis-box:checked');
            if (checked.length > REPLICA_MAX_AXES) {
                box.checked = false;
                replicaToast(`最多同时变 ${REPLICA_MAX_AXES} 条轴`, true);
            }
        });
    });
}

function replicaToast(msg, isError) {
    const el = replicaRoot() && replicaRoot().querySelector('#replica-upload-status');
    if (el) {
        el.textContent = msg;
        el.className = `replica-status ${isError ? 'replica-status-error' : ''}`;
    }
    if (isError) console.error('[replica]', msg);
}

function replicaSetBusy(busy) {
    replicaBusy = busy;
    const root = replicaRoot();
    if (!root) return;
    root.querySelectorAll('button').forEach(b => { b.disabled = busy || b.hasAttribute('data-perm-disabled'); });
}

/* --- 动作 --- */

async function replicaUpload() {
    const input = replicaRoot().querySelector('#replica-file');
    const file = input && input.files && input.files[0];
    if (!file) { replicaToast('请先选择一个视频文件', true); return; }

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
            : '上传完成，接下来开始抽帧');
        if (!data.reused) await replicaStart();
    } catch (e) {
        replicaToast(e.message, true);
    } finally {
        replicaSetBusy(false);
    }
}

async function replicaStart() {
    if (!replicaState) return;
    const modeEl = replicaRoot().querySelector('input[name="replica-mode"]:checked');
    const degraded = modeEl ? modeEl.value === 'degraded' : false;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/start', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, degraded, config: replicaConfig() }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaAdvance(action, payload = {}) {
    if (!replicaState) return;
    replicaSetBusy(true);
    try {
        const data = await replicaFetch('/api/replica/advance', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, action, payload, config: replicaConfig() }),
        });
        replicaTaskId = data.task_id;
        replicaOpenSSE(replicaTaskId);
    } catch (e) {
        replicaToast(e.message, true);
        replicaSetBusy(false);
    }
}

async function replicaCompose() {
    // 合成前先把编辑器里的改动落盘：不然用户改了半天，合成用的还是磁盘上的旧节拍。
    const saved = await replicaSaveBeats(false);
    if (!saved) return;
    replicaAdvance('approve');
}

function replicaVariant() {
    const root = replicaRoot();
    const axes = Array.from(root.querySelectorAll('.replica-axis-box:checked')).map(b => b.value);
    if (!axes.length) { replicaToast('至少勾一条变异轴', true); return; }
    const brief = (root.querySelector('#replica-variant-brief') || {}).value || '';
    replicaAdvance('variant', { axes, brief });
}

function replicaConfig() {
    // `config` 是 js/state.js 的全局配置对象。复刻这条线只用到里面的网关与模型字段；
    // 取不到就交空对象，服务端的 effective_config 会补上服务端权威配置。
    return typeof config !== 'undefined' && config ? config : {};
}

/* --- 节拍编辑 --- */

function replicaCollectBeats() {
    const root = replicaRoot();
    if (!replicaState || !replicaState.beats) return null;
    const doc = JSON.parse(JSON.stringify(replicaState.beats));

    root.querySelectorAll('[data-beat][data-key]').forEach(el => {
        const beat = doc.beats[parseInt(el.dataset.beat, 10)];
        if (!beat) return;
        const key = el.dataset.key;
        beat[key] = Array.isArray(beat[key])
            ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
            : el.value.trim();
    });

    const banned = root.querySelector('#replica-banned');
    if (banned) {
        doc.banned_elements = banned.value.split(/[、,\n]/).map(s => s.trim()).filter(Boolean);
    }
    return doc;
}

async function replicaSaveBeats(rerender) {
    const doc = replicaCollectBeats();
    if (!doc) return false;
    try {
        const data = await replicaFetch('/api/replica/beats', {
            method: 'POST', headers: replicaHeaders(),
            body: JSON.stringify({ job_id: replicaState.job_id, beats: doc }),
        });
        replicaState = data.job_state;
        if (rerender) {
            replicaRender();
            const errors = (data.validation || []).filter(v => v.level === 'error');
            replicaToast(errors.length ? `已保存，仍有 ${errors.length} 项硬伤` : '已保存，校验通过');
        }
        return true;
    } catch (e) {
        replicaToast(e.message, true);
        return false;
    }
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
    replicaRender();
    replicaToast('已拆成两拍。事件与证据帧没有自动分配——请对着帧手工分给正确的那一半，再保存。');
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
    replicaRender();
    replicaToast('已合并。若两拍是不同的物理工序，合并会违反「一拍一工序」——保存后校验会告诉你。');
}

/* --- SSE --- */

function replicaOpenSSE(taskId) {
    if (replicaSSE) replicaSSE.close();
    const code = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    replicaSSE = new EventSource(
        `/api/compose-stream?task_id=${encodeURIComponent(taskId)}${code ? '&access_code=' + encodeURIComponent(code) : ''}`);

    const progress = () => replicaRoot() && replicaRoot().querySelector('#replica-progress');

    replicaSSE.addEventListener('replica_stage', (e) => {
        try {
            const d = JSON.parse(e.data);
            const el = progress();
            if (el) {
                el.style.display = 'block';
                el.innerHTML = `<span class="replica-chip">${escapeHtmlReplica(
                    REPLICA_STAGE_LABELS[d.stage] || d.stage)}</span> ${escapeHtmlReplica(d.message || '')}`;
            }
        } catch (err) { /* 单条事件解析失败不该打断整条流 */ }
    });

    const finish = async (msg, isError) => {
        if (replicaSSE) { replicaSSE.close(); replicaSSE = null; }
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

    replicaSSE.addEventListener('replica_paused', () => finish('已停在人工卡点，请核对节拍'));
    replicaSSE.addEventListener('result', () => finish('提示词包已生成'));
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
        let msg = '任务失败';
        try { msg = JSON.parse(e.data).message || msg; } catch (err) { /* 保留默认文案 */ }
        finish(msg, true);
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
}
