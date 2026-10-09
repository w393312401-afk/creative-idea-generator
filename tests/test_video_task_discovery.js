// Read-only task discovery must reconnect the exact visible project without submitting new video jobs.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ProgressModel = require('../js/progress_model.js');

const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const media = fs.readFileSync(path.join(__dirname, '../js/media_renderer.js'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const utils = fs.readFileSync(path.join(__dirname, '../js/utils.js'), 'utf8');
function section(source, start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from, `frontend source boundaries must exist: ${start}`);
    return source.slice(from, to);
}
const discovery = section(api, '// 只读发现其它入口已启动的视频任务', '/** 持久化');
const hydrate = section(media, 'function hydrateVideosPanel(', 'function hydrateCoverPanel(');
const realStream = section(app, 'async function streamVideosProgress(', 'async function streamCoverProgress(');
const progressRestore = section(app, 'function restoreMediaTaskProgress(', 'async function streamFramesProgress(');
const canonicalTitle = section(utils, 'function getIdeaSaveTitle(', '// 挂帧的三种角色');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
async function settles(promise) {
    let timer;
    try {
        return await Promise.race([promise, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('discovery must not await the long-lived video stream')), 1000);
        })]);
    } finally { clearTimeout(timer); }
}
const tick = () => new Promise(resolve => setImmediate(resolve));

function task(overrides = {}) {
    return { id: 'external-video-task', status: 'running',
        ...overrides,
        dimensions: { type: 'videos', project_key: 'canonical-project', title: 'Decorative title',
            request_id: 'external-request-id', target_slots: [3, '1', 3, 0, -1, 2.5, '2'],
            ...(overrides.dimensions || {}) },
    };
}

function setup(options = {}) {
    const owner = { id: 'owner', title: 'Same visible title', project_key: 'canonical-project' };
    const observed = { requests: [], streams: [], persisted: [], warnings: [], bars: [], renders: [] };
    const elements = Object.fromEntries(['generate-videos-btn', 'generate-video-chain-btn',
        'videos-progress', 'videos-meta'].map(id => [id, { style: {}, dataset: {}, textContent: '' }]));
    const grid = { children: [], querySelectorAll: () => [] };
    let record = options.record || null;
    const context = {
        console: { ...console, warn: (...args) => observed.warnings.push(args),
            error: (...args) => observed.warnings.push(args) },
        AbortController, URLSearchParams, ProgressModel, window: { ProgressModel },
        currentIdea: owner,
        getIdeaTaskRecord: id => id === owner.id ? record : null,
        isIdeaTaskActive: id => id === owner.id && !!record,
        isViewingIdea: id => context.currentIdea && context.currentIdea.id === id,
        beginIdeaTask: (_id, _type, taskId, controller) => record = { taskId, controller },
        endIdeaTask: () => { record = null; },
        saveActiveBackgroundTasksToLocalStorage: () => observed.persisted.push(JSON.parse(JSON.stringify(record))),
        fetch: async (url, init) => {
            observed.requests.push({ url, init });
            if (options.beforeResponse) await options.beforeResponse.promise;
            if (options.fetchError) throw options.fetchError;
            return { ok: options.ok !== false, status: options.ok === false ? 500 : 200,
                json: async () => options.payload || { tasks: options.tasks || [task()] } };
        },
        streamVideosProgress: (taskId, idea, targetSlots, metadata) => {
            observed.streams.push({ taskId, idea, targetSlots: Array.from(targetSlots || []), metadata });
            record = { taskId, controller: new AbortController(), targetSlots,
                requestId: metadata && metadata.requestId };
            return new Promise(() => {}); // Discovery must return while generation continues.
        },
        document: { getElementById: id => elements[id] || null },
        slotRenderTarget: () => grid,
        renderVideosForIdea: idea => observed.renders.push(idea),
        setProgressBar: (type, info) => observed.bars.push({ type, info }),
        watchTaskUntilTerminal: () => new Promise(() => {}),
        showToast() {}, refreshSlotGridBusy() {},
    };
    vm.createContext(context);
    vm.runInContext(canonicalTitle + '\n' + discovery + '\n' + hydrate
        + (options.realStream ? '\n' + progressRestore + '\n' + realStream : ''), context);
    return { context, owner, observed, elements, record: () => record,
        setRecord: value => { record = value; } };
}

function readOnlyRequests(observed) {
    assert(observed.requests.every(({ url, init }) => {
        const parsed = new URL(url, 'http://localhost');
        return parsed.pathname === '/api/tasks' && parsed.searchParams.get('limit') === '0'
            && Boolean(parsed.searchParams.get('project_key'))
            && (!init.method || init.method === 'GET') && init.cache === 'no-store' && !init.body;
    }),
    'discovery may only make an uncached read-only task-list request');
}

async function checkExternalTask() {
    for (const type of ['videos', 'video_chain']) {
        const fixture = setup({ tasks: [task({ dimensions: { type } })] });
        await settles(fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner));
        assert.strictEqual(fixture.observed.requests.length, 1);
        readOnlyRequests(fixture.observed);
        assert.strictEqual(fixture.observed.streams.length, 1);
        const stream = fixture.observed.streams[0];
        assert.strictEqual(stream.taskId, 'external-video-task');
        assert.strictEqual(stream.idea, fixture.owner);
        assert.deepStrictEqual(stream.targetSlots, [1, 2, 3]);
        assert.strictEqual(stream.metadata.requestId, 'external-request-id');
        assert.strictEqual(fixture.record().requestId, 'external-request-id');
        await settles(fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner));
        assert.strictEqual(fixture.observed.requests.length, 1, 'an attached record must prevent another lookup');
        assert.strictEqual(fixture.observed.streams.length, 1, 'an attached record must prevent another stream');
    }
}

async function checkExactProjectIdentity() {
    const rejected = [
        task({ dimensions: { project_key: 'another-project', title: 'canonical-project',
            display_title: 'Same visible title', task_label: 'canonical-project' } }),
        task({ dimensions: { project_key: 'canonical-project-extra', title: 'canonical-project' } }),
        task({ dimensions: { project_key: undefined, title: 'Same visible title',
            display_title: 'canonical-project', task_label: 'canonical-project' } }),
        task({ dimensions: { project_key: undefined, title: 'canonical-project-extra' } }),
        task({ dimensions: { project_key: undefined, title: 'canonical-project' } }),
        task({ dimensions: { type: 'frames' } }),
        ...['completed', 'failed', 'cancelled', 'queued'].map(status => task({ status })),
    ];
    for (const candidate of rejected) {
        const fixture = setup({ tasks: [candidate] });
        await settles(fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner));
        assert.strictEqual(fixture.observed.streams.length, 0,
            `task must not be attached through broad title or status matching: ${JSON.stringify(candidate)}`);
        assert.strictEqual(fixture.record(), null);
        readOnlyRequests(fixture.observed);
    }
    const legacy = setup({ payload: [task({ dimensions: { project_key: undefined, title: 'canonical-project' } })] });
    delete legacy.owner.project_key;
    legacy.owner.title = 'canonical-project';
    await settles(legacy.context.reconnectRunningVideoTaskForIdea(legacy.owner));
    assert.strictEqual(legacy.observed.streams.length, 1, 'legacy exact canonical title remains recoverable');
    const choose = setup({ tasks: [rejected[0], task({ id: 'correct-project-task' })] });
    await settles(choose.context.reconnectRunningVideoTaskForIdea(choose.owner));
    assert.strictEqual(choose.observed.streams[0].taskId, 'correct-project-task');
}

async function checkSingleFlightHydration() {
    const response = deferred();
    const fixture = setup({ beforeResponse: response });
    fixture.context.hydrateVideosPanel(fixture.owner);
    fixture.context.hydrateVideosPanel(fixture.owner);
    const pending = fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner);
    await tick();
    assert.strictEqual(fixture.observed.requests.length, 1, 'repeated hydration must share one pending task lookup');
    response.resolve();
    await settles(pending);
    await tick();
    assert.strictEqual(fixture.observed.streams.length, 1);
    fixture.context.hydrateVideosPanel(fixture.owner);
    await tick();
    assert.strictEqual(fixture.observed.requests.length, 1);
    assert.strictEqual(fixture.observed.streams.length, 1);
    readOnlyRequests(fixture.observed);
}

async function checkRecoveryRaces() {
    {
        const existing = { taskId: 'already-attached' };
        const fixture = setup({ record: existing });
        await settles(fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner));
        assert.strictEqual(fixture.record(), existing);
        assert.strictEqual(fixture.observed.requests.length, 0);
        assert.strictEqual(fixture.observed.streams.length, 0);
    }
    for (const race of ['local-resume', 'project-switch', 'canonical-change']) {
        const response = deferred();
        const fixture = setup({ beforeResponse: response });
        const pending = fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner);
        await tick();
        const resumed = { taskId: 'locally-resumed' };
        if (race === 'local-resume') fixture.setRecord(resumed);
        else if (race === 'project-switch') fixture.context.currentIdea = { id: 'other', project_key: 'another-project' };
        else fixture.owner.project_key = 'renamed-project';
        response.resolve();
        await settles(pending);
        assert.strictEqual(fixture.observed.streams.length, 0, `discovery must recheck ${race} after fetching`);
        assert.strictEqual(fixture.record(), race === 'local-resume' ? resumed : null);
        readOnlyRequests(fixture.observed);
    }
}

async function checkRealStreamMetadata() {
    const fixture = setup({ realStream: true });
    await settles(fixture.context.reconnectRunningVideoTaskForIdea(fixture.owner));
    assert.strictEqual(fixture.record().taskId, 'external-video-task');
    assert.strictEqual(fixture.record().requestId, 'external-request-id',
        'the real stream must preserve the external operation identity on its own record');
    assert.deepStrictEqual(Array.from(fixture.record().targetSlots), [1, 2, 3]);
    assert(fixture.observed.persisted.some(rec => rec && rec.requestId === 'external-request-id'
        && JSON.stringify(rec.targetSlots) === '[1,2,3]'), 'identity and target subset must be saved before watching');
    assert.strictEqual(fixture.elements['videos-progress'].style.display, 'flex');
    readOnlyRequests(fixture.observed);
}

(async () => {
    await checkExternalTask();
    await checkExactProjectIdentity();
    await checkSingleFlightHydration();
    await checkRecoveryRaces();
    await checkRealStreamMetadata();
    console.log('video task discovery tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
