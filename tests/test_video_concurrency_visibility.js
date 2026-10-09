// Exercise the real video stream, retry consumer and card hydrator without starting a server task.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ProgressModel = require('../js/progress_model.js');
const { slotPendingState, videoSlotState } = require('../js/slot_model.js');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const app = read('app.js');
const api = read('js/api_client.js');
const media = read('js/media_renderer.js');
function section(source, start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from, `real frontend function boundaries must exist: ${start}`);
    return source.slice(from, to);
}

const realConsumers = [
    read('js/slot_model.js'),
    read('js/slot_card.js'),
    section(media, 'function renderVideosForIdea(', '// ── 封面用途分配'),
    section(media, 'function hydrateVideosPanel(', 'function hydrateCoverPanel('),
    section(api, 'function newVideoRequestId(', '/* ── 多创意后台任务登记表'),
    section(api, 'function renderVideoSlotPending(', '// startTasksPolling / stopTasksPolling'),
    section(api, 'async function retrySingleVideo(', '// setVideoUploadButtonsBusy'),
    section(app, 'function restoreMediaTaskProgress(', 'async function streamFramesProgress('),
    section(app, 'async function streamVideosProgress(', 'async function streamCoverProgress('),
].join('\n');

function element() {
    const node = { style: {}, dataset: {}, children: [], className: '', textContent: '',
        addEventListener() {}, removeAttribute(name) { delete this[name]; }, load() {},
        appendChild(child) { this.children.push(child); },
    };
    const descendants = new Map();
    Object.defineProperty(node, 'innerHTML', {
        get() { return this.html || ''; },
        set(value) { this.html = value; descendants.clear(); },
    });
    node.classList = {
        contains: cls => node.className.split(/\s+/).includes(cls),
        add: cls => { if (!node.classList.contains(cls)) node.className += ' ' + cls; },
        remove: cls => { node.className = node.className.split(/\s+/).filter(v => v !== cls).join(' '); },
        toggle: (cls, enabled) => enabled ? node.classList.add(cls) : node.classList.remove(cls),
    };
    node.querySelector = selector => {
        const exists = selector.startsWith('.')
            ? node.innerHTML.includes(selector.slice(1).split('[')[0])
            : node.innerHTML.includes('<' + selector);
        if (!exists) return null;
        if (!descendants.has(selector)) descendants.set(selector, element());
        return descendants.get(selector);
    };
    node.querySelectorAll = selector => selector.startsWith('.')
        ? node.children.filter(child => child.classList.contains(selector.slice(1))) : [];
    return node;
}

function setup(options = {}) {
    const slots = options.slots || [21, 22, 23, 24, 25];
    const owner = { id: 'owner', title: 'Concurrent video project', prompt_block: 'saved prompts',
        prompt_slots: slots.map(index => ({ type: 'video', index })),
        frameRun: { frames: [{ sequence: 21 }], videos: options.videos || [] } };
    const grid = element();
    const elements = Object.fromEntries(['videos-meta', 'videos-progress', 'generate-videos-btn',
        'generate-video-chain-btn', 'videos-grid'].map(id => [id, element()]));
    elements['videos-grid'] = grid;
    const observed = { requests: [], bars: [], toasts: [], snapshots: [], ends: 0, errors: [] };
    let record = options.record || null;
    const context = {
        console: { ...console, error: (...args) => observed.errors.push(args) },
        AbortController, crypto: require('crypto').webcrypto,
        config: { videoProvider: 'flow2api' }, currentIdea: owner,
        ProgressModel, window: { ProgressModel },
        document: {
            createElement: () => element(),
            getElementById: id => elements[id] || grid.children.find(card => card.id === id) || null,
        },
        getIdeaTaskRecord: id => id === owner.id ? record : null,
        isIdeaTaskActive: id => id === owner.id && !!record,
        beginIdeaTask: (_id, _type, taskId, controller) => record = { taskId, controller },
        endIdeaTask: () => { observed.ends++; record = null; },
        isViewingIdea: id => context.currentIdea && context.currentIdea.id === id,
        saveActiveBackgroundTasksToLocalStorage: () => observed.snapshots.push(JSON.parse(JSON.stringify(record))),
        setProgressBar: (type, info) => observed.bars.push({ type, info }),
        slotRenderTarget: () => grid,
        clearSlotGrid: target => { target.children.length = 0; },
        placeSlotCard() {}, enableVideoSlotDnd() {}, refreshSlotGridBusy() {},
        cacheBustedUrl: url => url,
        resolvePromptSlots: idea => idea.prompt_slots || [],
        confirmSequenceReviewOverride: async () => ({ proceed: true, override: false }),
        getIdeaSaveTitle: idea => idea.title,
        showToast: (...args) => observed.toasts.push(args),
        syncFrameRunToLibrary: async (result, idea) => { idea.frameRun = result; },
        reloadManifestIntoIdea: async () => {}, summarizeRunQuality: () => null,
        handleManualInterventionEvent() {},
        fetch: async (url, request) => {
            observed.requests.push({ url, body: JSON.parse(request.body) });
            return { ok: true, json: async () => ({ task_id: 'concurrent-task' }) };
        },
        watchTaskUntilTerminal: async (_taskId, { onEvent }) => {
            await options.run({ context, owner, grid, elements, observed, record: () => record,
                emit: (type, data) => onEvent(type, data) });
            return options.outcome || { status: 'disconnected', error: 'test stream paused' };
        },
    };
    vm.createContext(context);
    vm.runInContext(realConsumers, context);
    context.renderVideosForIdea(owner);
    return { context, owner, grid, elements, observed, record: () => record };
}

function card(context, slot) {
    const result = context.document.getElementById(`video-slot-${slot}`);
    assert(result, `VID ${slot} must remain in the grid`);
    return result;
}
function pendingCard(context, slot, activity) {
    const item = card(context, slot);
    assert.strictEqual(item.dataset.kind, 'pending', `VID ${slot} must be pending while ${activity}`);
    assert.strictEqual(item.dataset.activity, activity);
    assert(item.classList.contains(`slot-video-${activity}`), 'active and queued cards must have distinct appearances');
    assert.strictEqual(item.innerHTML.includes('cover-spinner slot-spinner'), activity === 'active',
        'only an executing video may show the animated generation spinner');
    assert.match(item.querySelector('.slot-label').textContent,
        activity === 'active' ? /生成中/ : /等待中/);
}
function concurrentLabel(text, slots) {
    assert(text.includes(`并发处理中 ${slots.length} 段`), `active count must be visible: ${text}`);
    for (const slot of slots) assert(text.includes(`VID ${String(slot).padStart(3, '0')}`),
        `active VID ${slot} must be visible: ${text}`);
}
function activeState(rec, slots) {
    assert.deepStrictEqual(Array.from(ProgressModel.activeVideoSlots(rec.progressState)), slots);
    assert.deepStrictEqual(Array.from(rec.progressInfo.activeSlots), slots);
    assert.strictEqual(rec.progressInfo.activeCount, slots.length);
    if (slots.length) concurrentLabel(rec.progressInfo.label, slots);
}

function checkProgressModel() {
    let info = ProgressModel.normalizeGenerationProgress('start', { total: 4, slots: [21, 22, 23, 24] }, 'videos');
    for (const index of [23, 21, 22]) {
        info = ProgressModel.normalizeGenerationProgress('video_start', { index, current: 4, total: 4 }, 'videos', info.state);
    }
    assert.deepStrictEqual(Array.from(info.activeSlots), [21, 22, 23]);
    assert.strictEqual(info.activeCount, 3);
    concurrentLabel(info.label, [21, 22, 23]);
    info = ProgressModel.normalizeGenerationProgress('video_start', { index: 22, total: 4 }, 'videos', info.state);
    assert.strictEqual(info.activeCount, 3, 'replayed start must not increase the active count');
    info = ProgressModel.normalizeGenerationProgress('video_done', { index: 23, current: 4, total: 4 }, 'videos', info.state);
    assert.strictEqual(info.activeCount, 2);
    assert.deepStrictEqual(Array.from(info.activeSlots), [21, 22]);
    concurrentLabel(info.label, [21, 22]);
    assert(!info.label.includes('VID 023'), 'the finished video must leave the active list');
    assert(info.percent < 40, 'request ordinal 4/4 must not become completed progress 4/4');
    info = ProgressModel.normalizeGenerationProgress('video_warning', { message: '平台正在收取结果' }, 'videos', info.state);
    concurrentLabel(info.label, [21, 22]);
    info = ProgressModel.normalizeGenerationProgress('video_error', { index: 21, total: 4, message: 'failed' }, 'videos', info.state);
    concurrentLabel(info.label, [22]);
    assert.strictEqual(info.state.slotStatus['21'], 'failed');
    info = ProgressModel.normalizeGenerationProgress('video_done', { index: 22, total: 4 }, 'videos', info.state);
    assert.strictEqual(info.activeCount, 0);
    assert(!info.label.includes('并发处理中'), 'zero active videos must not keep a stale concurrency label');
    for (const terminal of ['result', 'error']) {
        const running = ProgressModel.progressFromEvents([
            ['start', { total: 4, slots: [21, 22, 23, 24] }],
            ...[21, 22, 23].map(index => ['video_start', { index, total: 4 }]),
        ], 'videos', 'running');
        const stopped = ProgressModel.normalizeGenerationProgress(terminal,
            { message: 'task ended' }, 'videos', running.state);
        assert.strictEqual(stopped.activeCount, 0);
        assert.deepStrictEqual(Array.from(stopped.activeSlots), []);
        for (const slot of [21, 22, 23, 24]) assert.strictEqual(stopped.state.slotStatus[slot], 'stopped');
        assert(!Object.values(stopped.state.slotStatus).some(status => status === 'active' || status === 'queued'));
        assert(!stopped.label.includes('并发处理中'), 'terminal tasks must not retain active labels');
        assert.strictEqual(running.state.slotStatus['21'], 'active', 'normalization must not mutate the previous state');
    }
}

function checkSlotModel() {
    for (const activity of ['active', 'queued']) {
        const pending = slotPendingState('video', 21, activity === 'active' ? '生成中...' : '等待中', activity);
        assert.strictEqual(pending.activity, activity);
        for (const previous of [null, { slot: 21, status: 'failed' },
            { slot: 21, status: 'success', url: '/previous.mp4' }]) {
            const state = videoSlotState(previous, { seq: 21, busy: true, activity });
            assert.strictEqual(state.kind, 'pending', 'explicit current activity takes priority over the previous attempt');
            assert.strictEqual(state.activity, activity);
            assert.match(state.label, activity === 'active' ? /生成中/ : /等待中/);
        }
    }
    assert.strictEqual(videoSlotState({ slot: 21, status: 'success', url: '/previous.mp4' },
        { seq: 21, busy: true, pending: true }).kind, 'ready', 'legacy pending alone must preserve a completed result');
}

async function checkFullStream() {
    const fixture = setup({
        run({ context, elements, emit, record }) {
            emit('start', { total: 4, slots: [21, 22, 23, 24] });
            for (const slot of [21, 22, 23, 24]) pendingCard(context, slot, 'queued');
            assert.strictEqual(card(context, 25).dataset.kind, 'missing', 'unrequested slots stay outside the queue');
            for (const index of [21, 22, 23]) {
                emit('video_start', { index, current: index - 20, total: 4 });
                pendingCard(context, index, 'active');
            }
            activeState(record(), [21, 22, 23]);
            concurrentLabel(elements['videos-meta'].textContent, [21, 22, 23]);
            pendingCard(context, 24, 'queued');
            emit('video_done', { index: 23, current: 4, total: 4,
                video: { slot: 23, status: 'success', url: '/new-23.mp4' } });
            activeState(record(), [21, 22]);
            concurrentLabel(elements['videos-meta'].textContent, [21, 22]);
            assert.strictEqual(card(context, 23).dataset.kind, 'ready');
            context.renderVideosForIdea(context.currentIdea);
            pendingCard(context, 21, 'active');
            pendingCard(context, 22, 'active');
            pendingCard(context, 24, 'queued');
            assert.strictEqual(card(context, 23).dataset.kind, 'ready', 'rerender must retain the terminal result');
            emit('video_error', { index: 21, current: 1, total: 4, message: 'failed attempt' });
            activeState(record(), [22]);
            concurrentLabel(elements['videos-meta'].textContent, [22]);
            assert.strictEqual(card(context, 21).dataset.kind, 'failed');
        },
    });
    await fixture.context.streamVideosProgress('concurrent-task', fixture.owner, [21, 22, 23, 24]);
    assert.strictEqual(fixture.observed.requests.length, 0, 'stream visibility must not submit a generation request');
}

async function checkRetryStream() {
    const fixture = setup({ videos: [{ slot: 21, status: 'failed', error: 'previous attempt' },
        { slot: 22, status: 'success', url: '/previous-22.mp4' }],
        run({ context, owner, elements, emit, record }) {
            for (const slot of [21, 22, 23]) pendingCard(context, slot, 'queued');
            for (const index of [21, 22, 23]) {
                emit('video_start', { index, current: index - 20, total: 3 });
                pendingCard(context, index, 'active');
            }
            activeState(record(), [21, 22, 23]);
            concurrentLabel(elements['videos-meta'].textContent, [21, 22, 23]);
            context.renderVideosForIdea(owner);
            for (const slot of [21, 22, 23]) pendingCard(context, slot, 'active');
            emit('video_done', { index: 22, current: 3, total: 3,
                video: { slot: 22, status: 'success', url: '/retried-22.mp4' } });
            activeState(record(), [21, 23]);
            concurrentLabel(elements['videos-meta'].textContent, [21, 23]);
            assert.strictEqual(card(context, 22).dataset.url, '/retried-22.mp4');
            emit('video_error', { index: 21, current: 1, total: 3, message: 'failed again' });
            activeState(record(), [23]);
            concurrentLabel(elements['videos-meta'].textContent, [23]);
        },
    });
    const result = await fixture.context.retryVideoSlots([23, 21, 22]);
    assert.strictEqual(result.status, 'disconnected');
    assert.strictEqual(fixture.observed.requests.length, 1, 'one batch must remain one generation request');
    assert.deepStrictEqual(fixture.observed.requests[0].body.target_slots, [21, 22, 23]);
}

async function checkBackgroundHydration() {
    const fixture = setup({
        run({ context, owner, elements, emit, record }) {
            emit('start', { total: 4, slots: [21, 22, 23, 24] });
            emit('video_start', { index: 21, current: 1, total: 4 });
            context.currentIdea = { id: 'other', prompt_block: 'other project',
                prompt_slots: [{ type: 'video', index: 81 }] };
            context.renderVideosForIdea(context.currentIdea);
            const otherLabel = elements['videos-meta'].textContent;
            emit('video_start', { index: 22, current: 2, total: 4 });
            emit('video_start', { index: 23, current: 3, total: 4 });
            activeState(record(), [21, 22, 23]);
            assert.strictEqual(elements['videos-meta'].textContent, otherLabel,
                'background starts must not overwrite the other project');
            context.currentIdea = owner;
            context.hydrateVideosPanel(owner);
            for (const slot of [21, 22, 23]) pendingCard(context, slot, 'active');
            pendingCard(context, 24, 'queued');
            concurrentLabel(elements['videos-meta'].textContent, [21, 22, 23]);
            emit('video_done', { index: 22, current: 2, total: 4,
                video: { slot: 22, status: 'success', url: '/restored-22.mp4' } });
            context.hydrateVideosPanel(owner);
            assert.strictEqual(card(context, 22).dataset.kind, 'ready');
            for (const slot of [21, 23]) pendingCard(context, slot, 'active');
            concurrentLabel(elements['videos-meta'].textContent, [21, 23]);
        },
    });
    await fixture.context.streamVideosProgress('concurrent-task', fixture.owner, [21, 22, 23, 24]);
}

async function checkContinuousRecovery() {
    const fixture = setup({
        run({ context, owner, elements, emit, record }) {
            emit('start', { total: 4, slots: [21, 22, 23, 24] });
            emit('video_done', { index: 21, total: 4, video: { slot: 21, status: 'success', url: '/ready-21.mp4' } });
            for (const index of [22, 23, 24]) emit('video_error', { index, current: 4, total: 4, message: 'temporary failure' });
            const sameRecord = record();
            for (const phase of ['querying', 'waiting', 'retrying']) {
                emit('video_recovery', { phase, slots: [22, 23, 24], completed_slots: [21],
                    retry_round: 2, message: '自动恢复未完成片段' });
                assert.strictEqual(record(), sameRecord, '恢复不能结束任务登记或开启另一观察者');
                assert.strictEqual(record().current, 1);
                assert(record().progressInfo.percent < 40);
                assert.strictEqual(elements['videos-progress'].style.display, 'flex');
                assert.strictEqual(elements['generate-videos-btn'].disabled, true);
                assert.strictEqual(card(context, 21).dataset.kind, 'ready');
                for (const slot of [22, 23, 24]) {
                    const pending = card(context, slot);
                    assert.strictEqual(pending.dataset.kind, 'pending');
                    assert.strictEqual(pending.dataset.activity, 'recovering');
                    assert.match(pending.querySelector('.slot-label').textContent,
                        phase === 'querying' ? /自动核对/ : phase === 'waiting' ? /等待自动重试/ : /自动补跑/);
                }
                context.currentIdea = { id: 'other', prompt_block: 'other', prompt_slots: [] };
                context.currentIdea = owner;
                context.hydrateVideosPanel(owner);
                assert.match(elements['videos-meta'].textContent, /自动恢复/);
                assert.strictEqual(card(context, 22).dataset.activity, 'recovering', '返回项目保留恢复阶段');
            }
            emit('video_start', { index: 22, current: 4, total: 4 });
            pendingCard(context, 22, 'active');
            assert.strictEqual(record().current, 1);
            emit('video_done', { index: 22, total: 4, video: { slot: 22, status: 'success', url: '/new-22.mp4' } });
            assert.strictEqual(record().current, 2);
            assert.strictEqual(card(context, 22).dataset.kind, 'ready');
        }
    });
    await fixture.context.streamVideosProgress('recovering-task', fixture.owner, [21, 22, 23, 24]);
    assert(fixture.record(), '网络断线时仍保留原恢复任务');
    assert.strictEqual(fixture.observed.ends, 0);
    const cancelled = setup({ outcome: { status: 'cancelled' },
        run({ emit }) {
            emit('start', { total: 3, slots: [21, 22, 23] });
            emit('video_done', { index: 21, total: 3, video: { slot: 21, status: 'success', url: '/kept.mp4' } });
            emit('video_recovery', { phase: 'waiting', slots: [22, 23], completed_slots: [21], message: '自动等待' });
        }
    });
    await cancelled.context.streamVideosProgress('cancel-recovery-task', cancelled.owner, [21, 22, 23]);
    assert.strictEqual(cancelled.record(), null, '显式取消结束持续恢复任务');
    assert.strictEqual(card(cancelled.context, 21).dataset.kind, 'ready');
    assert.strictEqual(card(cancelled.context, 21).dataset.url, '/kept.mp4');
    assert.notStrictEqual(card(cancelled.context, 22).dataset.kind, 'pending');
}

async function checkRestartedTask() {
    let endedRecord;
    const fixture = setup({
        videos: [{ slot: 21, status: 'running' }, { slot: 22, status: 'running' },
            { slot: 23, status: 'success', url: '/preserved-23.mp4' }],
        outcome: { status: 'failed', error: 'server restarted' },
        run({ context, emit, record }) {
            emit('start', { total: 3, slots: [21, 22, 24] });
            emit('video_start', { index: 21, total: 3 });
            emit('video_start', { index: 22, total: 3 });
            pendingCard(context, 21, 'active');
            pendingCard(context, 22, 'active');
            emit('error', { message: 'server restarted' });
            endedRecord = record();
            activeState(endedRecord, []);
        },
    });
    await fixture.context.streamVideosProgress('missing-after-restart', fixture.owner, [21, 22, 24]);
    assert.strictEqual(fixture.record(), null, 'a server-confirmed terminal task must release the frontend record');
    assert.strictEqual(endedRecord.progressState.slotStatus['21'], 'stopped');
    assert.strictEqual(endedRecord.progressState.slotStatus['22'], 'stopped');
    assert.strictEqual(endedRecord.progressState.slotStatus['24'], 'stopped');
    for (const slot of [21, 22, 24]) {
        const item = card(fixture.context, slot);
        assert.notStrictEqual(item.dataset.kind, 'pending', 'a stale running manifest cannot imply a live task');
        assert.notStrictEqual(item.dataset.activity, 'active');
    }
    assert.strictEqual(card(fixture.context, 23).dataset.kind, 'ready');
    assert.strictEqual(card(fixture.context, 23).dataset.url, '/preserved-23.mp4');
    assert(!fixture.elements['videos-meta'].textContent.includes('并发处理中'));
}

async function checkRestartedTaskWithoutReplay() {
    const previouslyRunning = ProgressModel.progressFromEvents([
        ['start', { total: 3, slots: [21, 22, 24] }],
        ['video_start', { index: 21, total: 3 }],
        ['video_start', { index: 22, total: 3 }],
    ], 'videos', 'running');
    const fixture = setup({
        record: { controller: new AbortController(), targetSlots: [21, 22, 24],
            progressState: previouslyRunning.state, progressInfo: previouslyRunning },
        videos: [{ slot: 21, status: 'running' }, { slot: 22, status: 'running' },
            { slot: 23, status: 'success', url: '/preserved-23.mp4' }],
        outcome: { status: 'failed', error: '服务已重启，任务不存在' },
        run() {}, // A task-status lookup can report failure without replaying any SSE events.
    });
    pendingCard(fixture.context, 21, 'active');
    pendingCard(fixture.context, 22, 'active');
    pendingCard(fixture.context, 24, 'queued');
    await fixture.context.streamVideosProgress('missing-after-restart', fixture.owner, [21, 22, 24]);
    assert.strictEqual(fixture.record(), null);
    assert.strictEqual(fixture.elements['videos-progress'].style.display, 'none');
    const checkSettledCards = () => {
        for (const item of fixture.grid.children) {
            assert(!item.classList.contains('slot-video-active'));
            assert(!item.classList.contains('slot-video-queued'));
            assert.notStrictEqual(item.dataset.kind, 'pending', 'a failed task cannot retain live placeholders');
        }
        assert.strictEqual(card(fixture.context, 23).dataset.kind, 'ready');
        assert.strictEqual(card(fixture.context, 23).dataset.url, '/preserved-23.mp4');
    };
    checkSettledCards();
    fixture.context.hydrateVideosPanel(fixture.owner);
    checkSettledCards();
    assert.strictEqual(fixture.elements['videos-progress'].style.display, 'none');
    assert(!fixture.elements['videos-meta'].textContent.includes('并发处理中'));
}

(async () => {
    checkProgressModel();
    checkSlotModel();
    await checkFullStream();
    await checkRetryStream();
    await checkBackgroundHydration();
    await checkContinuousRecovery();
    await checkRestartedTask();
    await checkRestartedTaskWithoutReplay();
    console.log('video concurrency visibility tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
