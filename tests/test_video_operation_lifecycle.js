const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto').webcrypto;
const source = fs.readFileSync(require('path').join(__dirname, '../js/api_client.js'), 'utf8');
const helpers = source.slice(source.indexOf('function newVideoRequestId('), source.indexOf('/* ── 多创意后台任务登记表'));
const watcher = source.slice(source.indexOf('async function watchTaskUntilTerminal('), source.indexOf('// 视频请求在收到 task_id'));
const persistence = source.slice(source.indexOf('function saveActiveBackgroundTasksToLocalStorage('), source.indexOf('// 后端 server_common.log()'));
function setup(fetch) {
    let record = null;
    const snapshots = [], timers = [], calls = [], toasts = [];
    const owner = { id: 'idea', title: 'project' };
    const context = {
        console, crypto, AbortController, TextDecoder, Date, Uint8Array,
        document: { getElementById: () => null },
        fetch: (...args) => { calls.push(args); return fetch(...args); },
        setTimeout: callback => { timers.push(callback); return timers.length; },
        isIdeaTaskActive: () => !!record,
        beginIdeaTask: (_id, _type, taskId, controller) => (record = { taskId, controller }),
        getIdeaTaskRecord: () => record,
        endIdeaTask: () => { record = null; },
        saveActiveBackgroundTasksToLocalStorage: () => snapshots.push(JSON.parse(JSON.stringify(record))),
        isViewingIdea: () => true, renderVideosForIdea() {}, reloadManifestIntoIdea: async () => {},
        showToast: (...args) => toasts.push(args), streamVideosProgress: (...args) => calls.push(['watch', ...args]),
    };
    vm.createContext(context); vm.runInContext(helpers, context);
    return { context, owner, snapshots, timers, calls, record: () => record, toasts };
}
const ok = data => ({ ok: true, status: 200, json: async () => data });
const missing = () => ({ ok: false, status: 404 });
(async () => {
    {
        let posts = 0; const bodies = [];
        const body = { title: 'one', prompt_block: 'original', target_slots: [1, 2], config: { model: 'original' } };
        const s = setup(async (url, init) => {
            if (url.startsWith('/api/video-operation')) return missing();
            bodies.push(JSON.parse(init.body));
            assert(s.snapshots.some(snap => snap.requestBody && snap.requestId), 'identity and body must persist before POST');
            if (++posts === 1) { body.prompt_block = 'edited'; body.config.model = 'edited'; throw Error('response lost'); }
            return ok({ task_id: 'accepted' });
        });
        const rec = s.context.beginVideoOperation(s.owner, [1, 2]);
        const result = await s.context.submitVideoOperation(rec, '/api/generate_videos', body);
        assert.equal(result.task_id, 'accepted');
        assert.deepStrictEqual(bodies[0], bodies[1], 'replayed POST must preserve the original snapshot and request ID');
        assert.equal(bodies[0].prompt_block, 'original');
        assert.equal(bodies[0].config.model, 'original');
    }
    {
        const s = setup(async url => {
            if (url.startsWith('/api/video-operation')) return ok({ task_id: 'already-running' });
            throw Error('lost ACK');
        });
        const rec = s.context.beginVideoOperation(s.owner, [2]);
        const result = await s.context.submitVideoOperation(rec, '/api/generate_videos', { target_slots: [2] });
        assert.equal(result.task_id, 'already-running');
        assert.equal(s.calls.filter(([url]) => url === '/api/generate_videos').length, 1, 'lookup should recover accepted request without re-post');
    }
    {
        const s = setup(async () => { throw Error('offline'); });
        const rec = s.context.beginVideoOperation(s.owner, [3]);
        await assert.rejects(s.context.submitVideoOperation(rec, '/api/generate_videos', {}), e => e.uncertain);
        s.context.scheduleVideoOperationRecovery(s.owner, rec);
        assert.equal(s.record(), rec, 'uncertain request must retain task lock');
        assert.equal(s.timers.length, 1);
        assert(rec.requestBody.request_id);
    }
    {
        const s = setup(async url => {
            assert.equal(url, '/api/compose-cancel', 'cancel should not race a lookup');
            return ok({ status: 'cancelled' });
        });
        const rec = s.context.beginVideoOperation(s.owner, [4]);
        await s.context.cancelVideoOperation(s.owner, rec);
        assert.equal(JSON.parse(s.calls[0][1].body).request_id, rec.requestId);
        assert.equal(s.record(), null);
        assert(rec.controller.signal.aborted);
    }
    {
        const s = setup(async () => ok({ status: 'cancelled', task_id: null }));
        const rec = s.context.beginVideoOperation(s.owner, [4]);
        const result = await s.context.submitVideoOperation(rec, '/api/generate_videos', {});
        assert.equal(result.status, 'cancelled');
        assert(rec.cancelRequested, 'cancellation before registration must not start a watcher with null task ID');
    }
    {
        const s = setup(async () => { throw Error('offline'); });
        const rec = s.context.beginVideoOperation(s.owner, [4]);
        await s.context.cancelVideoOperation(s.owner, rec);
        assert.equal(s.record(), rec);
        assert(rec.cancelRequested);
        assert(!rec.controller.signal.aborted, 'uncertain cancel must retain the live watcher');
        await s.timers[0]();
        assert.equal(s.calls.length, 2, 'cancel recovery must retry the cancellation even before registration');
        assert(s.calls.every(([url]) => url === '/api/compose-cancel'));
    }
    {
        const stored = { tasks: [{ ideaId: 'idea', type: 'videos', taskId: null, requestId: 'saved-request',
            targetSlots: [3, 8], requestEndpoint: '/api/generate_videos', requestBody: { request_id: 'saved-request', prompt_block: 'saved' } }] };
        let resumed;
        const ctx = { console, AbortController, ideaTasksById: {}, localStorage: {
            getItem: () => JSON.stringify(stored), setItem() {},
        }, findIdeaObjectById: id => ({ id }), beginIdeaTask: () => ({}),
        scheduleVideoOperationRecovery: (idea, rec) => { resumed = rec; },
        streamVideosProgress() { throw Error('not resolved yet'); } };
        vm.createContext(ctx); vm.runInContext(persistence, ctx); ctx.resumeActiveBackgroundTasksIfExists();
        assert.equal(resumed.requestId, 'saved-request');
        assert.equal(resumed.requestBody.prompt_block, 'saved');
        assert.deepStrictEqual(Array.from(resumed.targetSlots), [3, 8]);
    }
    {
        let requests = 0; const attempts = [];
        const ctx = { console: { warn() {}, error() {} }, TextDecoder, Date, setTimeout: callback => callback(),
            fetch: async url => url.includes('compose-status') ? ok({ status: 'running' }) :
                (++requests, { ok: true, status: 200, body: { getReader: () => ({ read: async () => ({ done: true }) }) } }) };
        vm.createContext(ctx); vm.runInContext(watcher, ctx);
        const outcome = await ctx.watchTaskUntilTerminal('task', { maxReconnects: 2,
            onEvent: (type, data) => { if (type === 'reconnecting') attempts.push(data.attempt); } });
        assert.equal(outcome.status, 'disconnected');
        assert.equal(requests, 3);
        assert.deepStrictEqual(attempts, [1, 2]);
    }
    {
        let requests = 0; const delivered = [], headers = [];
        const ctx = { console, TextDecoder, Date, setTimeout: callback => callback(), fetch: async (url, options) => {
            if (url.includes('compose-status')) return ok({ status: 'running' });
            headers.push(options.headers); const n = ++requests; let consumed = false;
            const text = n === 1 ? 'id: 1\ndata: {"type":"video_done","data":{"index":1}}\n\n'
                : 'id: 1\ndata: {"type":"video_done","data":{"index":1}}\n\nid: 2\ndata: {"type":"result","data":{"videos":[]}}\n\n';
            return { ok: true, status: 200, body: { getReader: () => ({ read: async () => consumed ? { done: true }
                : (consumed = true, { done: false, value: new TextEncoder().encode(text) }), cancel: async () => {} }) } };
        }};
        vm.createContext(ctx); vm.runInContext(watcher, ctx);
        const result = await ctx.watchTaskUntilTerminal('task', { onEvent: type => delivered.push(type) });
        assert.equal(result.status, 'completed');
        assert.equal(headers[1]['Last-Event-ID'], '1');
        assert.equal(delivered.filter(type => type === 'video_done').length, 1, 'duplicate IDs must not re-render media');
    }
    for (const functionName of ['generateVideos', 'generateVideoChain']) {
        let accept;
        const s = setup(async () => new Promise(resolve => { accept = () => resolve(ok({ task_id: 'registered' })); }));
        s.owner.prompt_block = 'saved prompt'; s.owner.frameRun = { frames: [{ sequence: 1 }] };
        Object.assign(s.context, { currentIdea: s.owner, config: {},
            document: { getElementById: () => ({ style: {}, disabled: false }) },
            slotRenderTarget: () => ({ querySelectorAll: () => [] }),
            confirmSequenceReviewOverride: async () => ({ proceed: true, override: false }),
            computeDebugTargets: () => [1, 2], getIdeaSaveTitle: () => 'project-key', getMergeSpeed: () => 2 });
        const app = fs.readFileSync(require('path').join(__dirname, '../app.js'), 'utf8');
        vm.runInContext(app.slice(app.indexOf('async function generateVideos('), app.indexOf('function getMergeSpeed(')), s.context);
        const running = s.context[functionName]();
        await new Promise(resolve => setImmediate(resolve));
        assert(s.record() && s.record().requestId, `${functionName} must lock before POST response: ${JSON.stringify(s.toasts)}`);
        assert.deepStrictEqual(Array.from(s.record().targetSlots), [1, 2]);
        assert(s.snapshots.some(snap => snap.requestBody && snap.requestBody.request_id));
        accept(); await running;
        assert(s.calls.some(([name, id]) => name === 'watch' && id === 'registered'));
    }
    {
        const { videoSlotState } = require('../js/slot_model.js');
        for (const status of ['failed', 'cancelled']) {
            const st = videoSlotState({ slot: 1, status: 'success', url: 'old.mp4', last_attempt: { status } }, { seq: 1 });
            assert.equal(st.kind, 'ready'); assert.equal(st.url, 'old.mp4');
            assert(st.badges.some(b => b.id === `retry-${status}`));
            const cardSource = fs.readFileSync(require('path').join(__dirname, '../js/slot_card.js'), 'utf8');
            const toolbarSource = fs.readFileSync(require('path').join(__dirname, '../js/slot_toolbar.js'), 'utf8');
            const card = { dataset: {}, querySelector: () => null };
            const ui = { cacheBustedUrl: url => url };
            vm.createContext(ui);
            vm.runInContext(cardSource.slice(0, cardSource.indexOf('function renderSlotById('))
                + '\n' + toolbarSource.slice(toolbarSource.indexOf('function slotMatchesFilter('),
                    toolbarSource.indexOf('/**\n * 每次整格重渲')), ui);
            ui.renderSlotCard(card, st);
            assert(ui.slotMatchesFilter(card, 'flagged'), 'retained previous video must appear under problem filter');
            assert(!ui.slotMatchesFilter(card, 'missing'), 'playable previous video is not an ungenerated slot');
            assert(card.innerHTML.includes('slot-select-box'), 'problem video must remain selectable for batch retry');
            assert(st.actions.some(a => a.act === 'retry-video' && !a.disabled));
        }
    }
    console.log('Video operation lifecycle tests passed');
})().catch(error => { console.error(error); process.exit(1); });
