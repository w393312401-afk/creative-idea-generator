// Optional background Codex editing on the project computer. Keeps generated projects untouched.
(() => {
    'use strict';
    const API = '/api/codex-video-editor';
    const ACTIVE = new Set(['queued', 'running']);
    const LABELS = { queued: '等待开始', running: '正在精剪', completed: '精剪完成',
        failed: '精剪失败', cancelled: '已取消', interrupted: '精剪已中断' };
    const STAGES = { queued: '等待开始', preparing: '准备原片', reviewing_source: '审阅原片',
        rendering: '导出精剪视频', reviewing_output: '复核剪切边界', verifying: '校验成片和音轨',
        cancelling: '正在停止精剪' };
    const DEFAULT_MODEL = 'gpt-6.1-sol', LEGACY_DEFAULT_MODEL = 'gpt-6-sol', DEFAULT_EFFORT = 'high';
    const MODEL_LABELS = { 'gpt-6.1-sol': 'GPT-6.1 Sol', 'gpt-6-astra': 'GPT-6 Astra',
        'gpt-6-sol': 'GPT-6 Sol', 'gpt-6-luna': 'GPT-6 Luna' };
    const EFFORT_LABELS = { low: '低', medium: '中', high: '高', xhigh: '较高', max: '很高', ultra: '最高' };
    const effortValues = Object.keys(EFFORT_LABELS);
    const normalizeModel = model => Object.prototype.hasOwnProperty.call(MODEL_LABELS, model) ? model : DEFAULT_MODEL;
    const normalizeEffort = (effort, model) => {
        if (model === 'gpt-6-luna' && effort === 'ultra') return 'max';
        return effortValues.includes(effort) ? effort : DEFAULT_EFFORT;
    };
    const jobSettings = job => [
        job?.model ? MODEL_LABELS[job.model] || String(job.model) : '',
        job?.reasoning_effort ? `思考强度：${EFFORT_LABELS[job.reasoning_effort] || String(job.reasoning_effort)}` : '',
    ].filter(Boolean).join(' · ');
    const states = new Map();
    let current = null, epoch = 0, timer = null, bound = false;
    let capabilities = null, capabilityRequest = null;
    const el = name => document.getElementById('codex-edit-' + name);
    const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const sourceKey = value => {
        let path = String(value || '').replace(/\\/g, '/');
        const diskPath = path.startsWith('outputs/') || (path.indexOf('/outputs/') > 0 && !/^[a-z]+:\/\//i.test(path));
        if (!diskPath) {
            path = path.split(/[?#]/)[0];
            try { path = decodeURIComponent(path); } catch (_) { /* Keep malformed text inert. */ }
        }
        const outputAt = path.indexOf('/outputs/');
        if (outputAt >= 0) path = path.slice(outputAt + 1);
        return path.replace(/^\/+/, '');
    };
    const mediaUrl = value => {
        const key = sourceKey(value);
        return key.startsWith('outputs/') && !key.split('/').includes('..')
            ? '/' + key.split('/').map(encodeURIComponent).join('/') : '';
    };
    const jobTime = value => {
        const number = Number(value);
        return Number.isFinite(number) && number > 0 ? number < 1e12 ? number * 1000 : number : Date.parse(value) || 0;
    };
    const activeJob = state => state.jobs.find(job => ACTIVE.has(job.status));
    const elapsedLabel = job => {
        const start = jobTime(job.created_at);
        if (!start || Date.now() < start) return '';
        const seconds = Math.floor((Date.now() - start) / 1000);
        return (job.status === 'queued' ? '已等待 ' : '已运行 ') + (seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
            : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`);
    };
    const selectedJob = state => state.jobs.find(job => job.id === state.selectedId) || state.jobs[0];
    const isCurrent = (state, version) => current === state && epoch === version;
    const requestKey = state => 'spark_codex_video_edit_pending:' + state.key;
    function savePending(state) {
        try {
            if (state.pending) sessionStorage.setItem(requestKey(state), JSON.stringify(state.pending));
            else sessionStorage.removeItem(requestKey(state));
        } catch (_) { /* Storage is optional; in-memory deduplication still applies. */ }
    }
    function restorePending(state) {
        try {
            const data = JSON.parse(sessionStorage.getItem(requestKey(state)) || 'null');
            if (data && sourceKey(data.source) === state.key && data.request_id && ['trim', 'trim_speed'].includes(data.mode)) {
                const savedModel = data.model == null ? LEGACY_DEFAULT_MODEL : data.model;
                const savedEffort = data.reasoning_effort == null ? DEFAULT_EFFORT : data.reasoning_effort;
                state.model = normalizeModel(savedModel);
                state.effort = normalizeEffort(savedEffort, state.model);
                // Keep supported requests identical for a safe retry. Removed models
                // become a fresh draft instead of reusing an old request identity.
                state.pending = savedModel === state.model && savedEffort === state.effort
                    ? { ...data, model: state.model, reasoning_effort: state.effort } : null;
                state.mode = data.mode;
                state.notes = String(data.notes || '');
                state.draftTouched = true;
                savePending(state);
            }
        } catch (_) { /* Ignore an incomplete previous browser session. */ }
    }
    async function request(path, body) {
        // The shared fetch wrapper adds the access code without replacing this marker.
        const options = { headers: { 'X-SPARK-Codex-Editor': '1' } };
        if (body !== undefined) {
            options.method = 'POST';
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        const response = await fetch(API + path, options);
        let data;
        try { data = await response.json(); } catch (_) { data = {}; }
        if (!response.ok) {
            const error = new Error(data.message || data.error || '暂时无法连接精剪服务，请重试。');
            error.status = response.status;
            throw error;
        }
        return data;
    }
    function updateJobs(state, jobs) {
        state.jobs = (Array.isArray(jobs) ? jobs : [])
            .filter(job => job && job.id && sourceKey(job.source) === state.key)
            .sort((a, b) => jobTime(b.created_at) - jobTime(a.created_at));
        if (!state.jobs.some(job => job.id === state.selectedId)) {
            state.selectedId = (activeJob(state) || state.jobs[0] || {}).id || null;
        }
    }
    function mergeJob(state, job) {
        if (!job || sourceKey(job.source) !== state.key) throw new Error('精剪记录与当前成片不一致，请刷新后重试。');
        updateJobs(state, [job, ...state.jobs.filter(old => old.id !== job.id)]);
        state.selectedId = job.id;
    }
    function schedule() {
        clearTimeout(timer);
        timer = null;
        if (current && (activeJob(current) || current.pending || current.submitting || current.error)) {
            const state = current, version = epoch;
            timer = setTimeout(() => { if (isCurrent(state, version)) refreshJobs(state, version); }, 3000);
        }
    }
    async function refreshJobs(state, version) {
        if (!isCurrent(state, version)) return;
        if (state.submitting || state.cancelling) { schedule(); return; }
        const serial = ++state.loadSerial;
        state.loading = true;
        try {
            const data = await request('/jobs?source=' + encodeURIComponent(state.source));
            if (!isCurrent(state, version) || serial !== state.loadSerial) return;
            updateJobs(state, data.jobs);
            if (state.pending && state.jobs.some(job => job.request_id === state.pending.request_id)) {
                state.pending = null; savePending(state);
            }
            state.lastChecked = Date.now();
            if (!state.loaded && !state.draftTouched && !state.pending) {
                const recent = selectedJob(state);
                if (recent && ['trim', 'trim_speed'].includes(recent.mode)) state.mode = recent.mode;
                if (recent?.notes) state.notes = String(recent.notes);
                if (recent?.model) state.model = normalizeModel(recent.model);
                if (recent?.reasoning_effort) state.effort = normalizeEffort(recent.reasoning_effort, state.model);
            }
            state.error = '';
            state.loaded = true;
        } catch (error) {
            if (isCurrent(state, version) && serial === state.loadSerial) state.error = error.message;
        } finally {
            if (isCurrent(state, version) && serial === state.loadSerial) {
                state.loading = false;
                render();
                schedule();
            }
        }
    }
    async function checkCapabilities(force = false) {
        if (force) capabilities = null;
        if (!capabilities && !capabilityRequest) {
            capabilityRequest = request('/capabilities')
                .then(data => { capabilities = { available: !!data.available, message: data.message || '' }; })
                .catch(error => { capabilities = { available: false, message: error.message }; })
                .finally(() => { capabilityRequest = null; if (current) render(); });
        }
        return capabilityRequest;
    }
    function render() {
        const state = current;
        if (!state || !el('root')) return;
        const job = selectedJob(state), active = activeJob(state);
        const busy = state.submitting || !!active;
        const locked = busy || !!state.pending;
        el('mode').disabled = locked;
        el('model').disabled = locked;
        el('effort').disabled = locked;
        el('notes').disabled = locked;
        if (el('mode').value !== state.mode) el('mode').value = state.mode;
        if (el('model').value !== state.model) el('model').value = state.model;
        const effortSelect = el('effort');
        if (effortSelect.dataset.model !== state.model) {
            effortSelect.innerHTML = effortValues.filter(value => state.model !== 'gpt-6-luna' || value !== 'ultra')
                .map(value => `<option value="${value}">${EFFORT_LABELS[value]} · ${value}</option>`).join('');
            effortSelect.dataset.model = state.model;
        }
        if (effortSelect.value !== state.effort) effortSelect.value = state.effort;
        if (el('notes').value !== state.notes) el('notes').value = state.notes;
        el('mode-hint').textContent = state.mode === 'trim_speed'
            ? '施工段在当前成片基础上提速至 1.25 倍，成品展示保持当前速度。'
            : '只精剪停顿、重复和穿帮，保持当前成片速度。';
        el('capability').textContent = !capabilities ? '正在检查后台 Codex…'
            : capabilities.available ? '' : capabilities.message || '后台 Codex 暂不可用，请在运行项目的电脑上检查登录后重试。';
        if (el('capability').textContent === state.error) el('capability').textContent = '';
        el('check').hidden = !capabilities || capabilities.available;
        const start = el('start');
        start.disabled = !capabilities?.available || busy || !state.loaded;
        start.textContent = state.submitting ? '正在提交…' : active ? '精剪进行中' : state.pending ? '重试连接'
            : job && ['failed', 'cancelled', 'interrupted'].includes(job.status) ? '重试精剪'
            : job?.status === 'completed' ? '再剪一个版本' : '开始精剪';
        el('cancel').hidden = !active;
        el('cancel').disabled = state.cancelling || active?.stage === 'cancelling';
        el('cancel').textContent = state.cancelling || active?.stage === 'cancelling' ? '正在取消…' : '取消精剪';
        const summary = active ? active.stage === 'cancelling' ? '正在停止精剪' : LABELS[active.status] : job ? LABELS[job.status] || '生成记录' : '';
        el('summary-state').textContent = summary;
        // Keep the live job visible even when the settings or an older record are folded away.
        el('progress').hidden = !active && !state.submitting;
        el('progress-stage').textContent = state.submitting ? '正在提交精剪'
            : active ? `精剪中 · ${STAGES[active.stage] || LABELS[active.status]}` : '';
        el('progress-elapsed').textContent = active ? elapsedLabel(active) : '';
        el('progress-message').textContent = active ? active.message || LABELS[active.status]
            : state.submitting ? '正在交给后台 Codex…' : '';
        const status = el('status');
        status.dataset.state = state.error ? 'failed' : job?.status || '';
        status.textContent = state.error || (state.pending && !busy ? '上次提交尚未确认。重试连接会找回同一次精剪，不会重复提交。'
            : state.submitting ? '正在交给后台 Codex…'
            : active ? active.message || LABELS[active.status]
            : job ? job.error || job.message || LABELS[job.status] || ''
            : state.loading ? '正在读取精剪记录…' : '选择方式后开始，剪辑结果会另存为新文件。');
        const settings = jobSettings(job);
        el('job-config').hidden = !settings;
        el('job-config').textContent = settings ? `所选记录：${settings}` : '';
        el('refresh').hidden = !state.error;
        const history = el('history');
        const recent = state.jobs.slice(0, 8);
        if (job && !recent.some(item => item.id === job.id)) recent.push(job);
        const historySignature = recent.map(item => JSON.stringify([
            item.id, item.status, item.output_missing, item.mode, item.model, item.reasoning_effort, item.created_at,
        ])).join('|');
        if (history.dataset.signature !== historySignature) {
            history.innerHTML = recent.map(item => {
                const time = jobTime(item.created_at) ? new Date(jobTime(item.created_at)).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '近期记录';
                const settingLabel = jobSettings(item);
                return `<option value="${esc(item.id)}">${esc(time)} · ${item.mode === 'trim_speed' ? '精剪并提速' : '只精剪'}${settingLabel ? ' · ' + esc(settingLabel) : ''} · ${esc(item.output_missing ? '结果文件不可用' : LABELS[item.status] || item.status)}</option>`;
            }).join('');
            history.dataset.signature = historySignature;
        }
        if (job) history.value = job.id;
        el('history-wrap').hidden = state.jobs.length < 2;
        const outputJob = job?.status === 'completed' ? job : state.jobs.find(item => item.status === 'completed' && item.output && !item.output_missing);
        const output = !outputJob?.output_missing && outputJob?.output;
        el('output-title').textContent = outputJob && outputJob.id !== job?.id ? '上一版精剪结果' : '精剪结果';
        el('source-note').hidden = !job?.source_changed && !outputJob?.source_changed;
        el('source-note').textContent = outputJob?.source_changed
            ? '此精剪结果来自此前的成片版本，当前原片已更新。'
            : job?.source_changed ? '这次精剪使用的是此前的成片版本，当前原片已更新。' : '';
        const url = output && mediaUrl(output.url || output.file);
        el('output').hidden = !url;
        const player = el('player');
        if (url) {
            // Repeated polls must not restart a preview the user is watching.
            if (player.dataset.source !== url) { player.src = url; player.dataset.source = url; }
            const info = [];
            if (Number(output.duration_seconds) > 0) info.push(`${Number(output.duration_seconds).toFixed(1)} 秒`);
            if (Number(output.size_bytes) > 0) info.push(`${(Number(output.size_bytes) / 1048576).toFixed(1)} MB`);
            el('output-info').textContent = info.join(' · ');
            el('download').href = url;
            el('download').download = url.split('/').pop();
            el('reveal').dataset.path = url;
        } else if (player.dataset.source) {
            player.pause(); player.removeAttribute('src'); player.load(); delete player.dataset.source;
        }
        const logs = Array.isArray(job?.logs) ? job.logs.slice(-50) : [];
        el('logs').hidden = !logs.length;
        const logsSignature = JSON.stringify(logs);
        if (el('log-list').dataset.signature !== logsSignature) {
            el('log-list').innerHTML = logs.map(line => `<li>${esc(line.message)}</li>`).join('');
            el('log-list').dataset.signature = logsSignature;
        }
    }
    async function start() {
        const state = current, version = epoch;
        if (!state || state.submitting || activeJob(state) || !capabilities?.available || !state.loaded) return;
        if (!state.pending) {
            const requestId = typeof crypto !== 'undefined' && crypto.randomUUID
                ? crypto.randomUUID() : `edit-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            state.pending = { source: state.source, mode: state.mode, notes: state.notes.trim(),
                model: state.model, reasoning_effort: state.effort, request_id: requestId };
            savePending(state);
        }
        state.submitting = true;
        state.error = '';
        ++state.loadSerial; // A GET begun before this mutation cannot overwrite its result.
        state.loading = false;
        clearTimeout(timer);
        render();
        try {
            const data = await request('/jobs', state.pending);
            if (!isCurrent(state, version)) return;
            mergeJob(state, data.job);
            state.pending = null;
            savePending(state);
        } catch (error) {
            if (isCurrent(state, version)) {
                state.error = error.message;
                if (error.status >= 400 && error.status < 500) {
                    state.pending = null; savePending(state);
                }
            }
        } finally {
            state.submitting = false;
            if (current === state) { render(); schedule(); }
        }
    }
    async function cancel() {
        const state = current, version = epoch, job = state && activeJob(state);
        if (!job || state.cancelling || job.stage === 'cancelling') return;
        state.cancelling = true;
        state.error = '';
        ++state.loadSerial;
        state.loading = false;
        clearTimeout(timer);
        render();
        try {
            const data = await request('/cancel', { id: job.id });
            if (!isCurrent(state, version)) return;
            mergeJob(state, data.job);
        } catch (error) {
            if (isCurrent(state, version)) state.error = error.message;
        } finally {
            state.cancelling = false;
            if (current === state) { render(); schedule(); }
        }
    }
    function bind() {
        if (bound) return;
        bound = true;
        el('mode').addEventListener('change', event => { if (current) { current.mode = event.target.value; current.draftTouched = true; render(); } });
        el('model').addEventListener('change', event => {
            if (current) {
                current.model = normalizeModel(event.target.value);
                current.effort = normalizeEffort(current.effort, current.model);
                current.draftTouched = true;
                render();
            }
        });
        el('effort').addEventListener('change', event => {
            if (current) {
                current.effort = normalizeEffort(event.target.value, current.model);
                current.draftTouched = true;
                render();
            }
        });
        el('notes').addEventListener('input', event => { if (current) { current.notes = event.target.value; current.draftTouched = true; } });
        el('panel').addEventListener('toggle', () => {
            if (current && el('panel').open) { checkCapabilities(); refreshJobs(current, epoch); }
        });
        el('start').addEventListener('click', start);
        el('cancel').addEventListener('click', cancel);
        el('check').addEventListener('click', () => { checkCapabilities(true); render(); });
        el('refresh').addEventListener('click', () => { if (current) refreshJobs(current, epoch); });
        el('history').addEventListener('change', event => { if (current) { current.selectedId = event.target.value; render(); } });
        el('reveal').addEventListener('click', () => {
            if (typeof revealLocalFile === 'function') revealLocalFile(el('reveal').dataset.path, '精剪成片');
        });
    }
    globalThis.syncCodexVideoEditor = function (merged, idea) {
        const root = el('root');
        if (!root) return;
        bind();
        const rawSource = merged?.status === 'success' ? merged.url || merged.file || '' : '';
        const source = rawSource ? mediaUrl(rawSource) || String(rawSource) : '';
        root.hidden = !source;
        if (!source) {
            current = null; ++epoch; clearTimeout(timer); timer = null;
            const player = el('player');
            if (player.dataset.source) { player.pause(); player.removeAttribute('src'); player.load(); delete player.dataset.source; }
            return;
        }
        const key = sourceKey(source);
        const signature = JSON.stringify([merged.url, merged.file, merged.size_bytes, merged.duration_seconds,
            merged.speed, merged.updated_at, merged.mtime, merged.partial]);
        if (current?.key === key) {
            const changed = current.sourceSignature !== signature;
            current.sourceSignature = signature;
            if (!current.loading && !current.submitting && !current.cancelling &&
                (changed || (el('panel').open && Date.now() - (current.lastChecked || 0) >= 3000))) refreshJobs(current, epoch);
            return;
        }
        ++epoch; clearTimeout(timer); timer = null;
        if (!states.has(key)) {
            const state = { key, source, title: idea?.title || '', mode: 'trim', model: DEFAULT_MODEL,
                effort: DEFAULT_EFFORT, notes: '', jobs: [],
                selectedId: null, loaded: false, loading: false, loadSerial: 0, submitting: false,
                cancelling: false, pending: null, error: '' };
            restorePending(state);
            states.set(key, state);
        }
        current = states.get(key);
        current.sourceSignature = signature;
        el('panel').open = false;
        el('logs').open = false;
        el('history').dataset.signature = '';
        render();
        checkCapabilities();
        refreshJobs(current, epoch);
    };
})();
