const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const toolbar = fs.readFileSync(path.join(__dirname, '../js/slot_toolbar.js'), 'utf8');
const operationFns = api.slice(api.indexOf('function newVideoRequestId('), api.indexOf('/* ── 多创意后台任务登记表'));
const apiFns = api.slice(api.indexOf('async function retrySingleVideo('),
    api.indexOf('// setVideoUploadButtonsBusy'));
const missingFn = api.slice(api.indexOf('async function retryMissingVideos('));
const selectedFn = toolbar.slice(toolbar.indexOf('async function bulkRetrySlots('),
    toolbar.indexOf('/**\n * 批量删除整拍'));
const dirtyFns = toolbar.slice(toolbar.indexOf('let fixAllRunning ='),
    toolbar.indexOf('/**\n * 「一键全部修复」'));

function setup(options = {}) {
    const records = { requests: [], begins: [], ends: [], pending: [], failed: [],
        done: [], toasts: [], feeds: [], frames: [], merges: 0, synced: [],
        persisted: [], reloaded: [], renders: [] };
    const owner = { id: 'owner', title: 'Original project', prompt_block: 'prompts' };
    const elements = { 'videos-progress': { style: {} }, 'videos-meta': {} };
    let active = !!options.busy;
    let rec = null;
    const context = {
        console: { error() {} }, AbortController, crypto: require('crypto').webcrypto, config: {}, currentIdea: owner,
        getIdeaTaskRecord: () => active ? rec : null,
        handleManualInterventionEvent: (...args) => records.feeds.push(args),
        slotToolbarState: { video: { selected: new Set(options.selected || [4, 2, 3]) },
            image: { selected: new Set([4, 2, 3]) } },
        document: { getElementById: id => elements[id] || null },
        slotToolbarEls: () => ({ grid: { querySelectorAll: () =>
            (options.dirty || [2, 3, 4]).map(seq => ({ dataset: { seq: String(seq) } })) } }),
        isIdeaTaskActive: () => active,
        isViewingIdea: id => context.currentIdea.id === id,
        confirmSequenceReviewOverride: async () => {
            if (options.busyAfterReview) active = true;
            return { proceed: true, override: false };
        },
        customConfirm: async () => true,
        clearSlotSelection: type => context.slotToolbarState[type].selected.clear(),
        padSlot: n => String(n).padStart(3, '0'),
        beginIdeaTask: (...args) => { records.begins.push(args); active = true; rec = { controller: args[3] }; return rec; },
        endIdeaTask: (...args) => { records.ends.push(args); active = false; },
        saveActiveBackgroundTasksToLocalStorage: () => records.persisted.push(JSON.parse(JSON.stringify(rec))),
        refreshSlotGridBusy() {},
        renderSlotPending: (...args) => records.pending.push(args),
        renderVideoSlotDone: (...args) => records.done.push(args),
        renderVideoSlotFailed: (...args) => records.failed.push(args),
        renderVideosForIdea: idea => records.renders.push({ idea, active }),
        syncFrameRunToLibrary: async (result, idea) => { records.synced.push(idea); idea.frameRun = result; },
        reloadManifestIntoIdea: async idea => records.reloaded.push(idea),
        getIdeaSaveTitle: idea => idea.id,
        showToast: (...args) => records.toasts.push(args),
        framesFeedLine: (...args) => records.feeds.push(args),
        retrySingleFrame: async slot => records.frames.push(slot),
        mergeVideos: async () => records.merges++,
        fetch: async (url, req) => {
            records.requests.push({ url, body: JSON.parse(req.body) });
            if (options.httpError) return { ok: false, status: 400, text: async () => 'unavailable' };
            return { ok: true, json: async () => ({ task_id: 'one-batch' }) };
        },
        watchTaskUntilTerminal: async (_taskId, callbacks) => {
            const slots = records.requests.at(-1).body.target_slots;
            assert.deepStrictEqual(Array.from(rec.targetSlots), slots);
            if (options.disconnected) return { status: 'disconnected', error: 'lost connection' };
            if (options.cancelled) return { status: 'cancelled' };
            if (options.switchOwner) context.currentIdea = { id: 'other' };
            if (options.manual) callbacks.onEvent('manual_intervention_detected', { code: 'captcha_required' });
            const videos = slots.filter(slot => !options.absentSlots?.includes(slot)).map(slot => ({ slot,
                status: options.failedSlots?.includes(slot) ? 'failed' : 'success' }));
            for (const video of videos) callbacks.onEvent(video.status === 'success' ? 'video_done' : 'video_error',
                { index: video.slot, video, message: 'generation failed' });
            if (options.mergeError) callbacks.onEvent('merge_error', { message: options.mergeError });
            return { status: 'completed', result: { videos: options.retainedPrevious
                ? videos.map(v => ({ ...v, status: 'success', url: 'old.mp4', last_attempt: { status: 'failed' } }))
                : videos, ...(options.result || {}) } };
        },
    };
    vm.createContext(context);
    vm.runInContext(operationFns + '\n' + apiFns + '\n' + missingFn + '\n' + selectedFn + '\n' + dirtyFns, context);
    return { context, records, owner, elements, active: () => active };
}

(async () => {
    {
        const { context, records, active } = setup();
        const result = await context.bulkRetrySlots('video');
        assert.strictEqual(records.requests.length, 1, '3 selected videos must share one API request');
        assert.deepStrictEqual(records.requests[0].body.target_slots, [2, 3, 4]);
        assert.strictEqual(records.begins.length, 1);
        assert.strictEqual(records.pending.length, 3);
        assert.strictEqual(records.done.length, 3);
        assert.strictEqual(records.ends.length, 1);
        assert.strictEqual(active(), false);
        assert.strictEqual(result.succeeded, 3);
        assert.strictEqual(records.merges, 0, 'backend already owns auto merge');
        assert(records.persisted.some(rec => rec.taskId === 'one-batch'
            && JSON.stringify(rec.targetSlots) === '[2,3,4]'), 'task ID and slot subset must be persisted before watching');
        assert(records.renders.every(render => !render.active), 'final render must occur after busy record is cleared');
    }
    {
        const { context, records } = setup({ dirty: [8, 2, 5], failedSlots: [5] });
        const result = await context.bulkRetryDirtySlots('video');
        assert.strictEqual(records.requests.length, 1, 'dirty videos must share one API request');
        assert.deepStrictEqual(records.requests[0].body.target_slots, [2, 5, 8]);
        assert.strictEqual(result.status, 'partial_failed');
        assert.strictEqual(result.succeeded, 2);
        assert.strictEqual(result.failed, 1);
        assert(records.feeds.some(([, text, level]) => text.includes('成功 2 段，失败 1 段') && level === 'warn'));
        assert(!records.toasts.some(([, level]) => level === 'success'), 'partial failure must not report all success');
        assert.strictEqual(vm.runInContext('retryDirtyRunning', context), false);
    }
    for (const options of [{ busy: true }, { busyAfterReview: true }]) {
        const { context, records } = setup(options);
        const result = await context.bulkRetrySlots('video');
        assert.strictEqual(result.status, 'skipped');
        assert.strictEqual(records.requests.length, 0, 'busy guard must prevent duplicate batch requests');
        assert(!records.toasts.some(([, level]) => level === 'success'));
    }
    {
        const { context, records, active } = setup({ httpError: true });
        const result = await context.bulkRetrySlots('video');
        assert.strictEqual(result.status, 'failed');
        assert.strictEqual(result.failed, 3);
        assert.strictEqual(records.failed.length, 3, 'request failure must settle every selected pending card');
        assert.strictEqual(active(), false);
        assert(!records.toasts.some(([, level]) => level === 'success'));
    }
    {
        const { context, records, active } = setup({ disconnected: true });
        const result = await context.retryVideoSlots([2, 3, 4]);
        assert.strictEqual(result.status, 'disconnected');
        assert.strictEqual(active(), true, 'connection loss must retain server task ownership');
        assert.strictEqual(records.ends.length, 0);
        await context.retryVideoSlots([2, 3, 4]);
        assert.strictEqual(records.requests.length, 1, 'disconnect must not allow duplicate paid request');
    }
    {
        const { context, records, owner } = setup({ switchOwner: true, failedSlots: [3] });
        await context.retryVideoSlots([2, 3, 4]);
        assert(records.done.every(([, , idea]) => idea === owner), 'delivery must remain bound to original idea');
        assert.strictEqual(records.failed.length, 0, 'do not paint errors onto a different idea');
        assert.strictEqual(records.synced[0], owner);
    }
    {
        const { context, records, active } = setup({ cancelled: true });
        const result = await context.retryVideoSlots([2, 3, 4]);
        assert.strictEqual(result.status, 'cancelled');
        assert.strictEqual(result.cancelled, 3);
        assert.strictEqual(active(), false);
        assert.strictEqual(records.reloaded.length, 1, 'cancelled task must reload partial server results');
        assert(records.failed.every(([, , label]) => label === '已取消'));
        assert(!records.toasts.some(([, level]) => level === 'success'));
    }
    {
        const { context, records } = setup();
        await context.retryMissingVideos([3, '2', 4, 3, 0, -1, 2.5]);
        assert.deepStrictEqual(records.requests[0].body.target_slots, [2, 3, 4]);
        assert.strictEqual(records.requests.length, 1);
        assert.strictEqual(records.merges, 0, 'missing retry must not merge same server result twice');
    }
    {
        const { context, records } = setup();
        await context.bulkRetrySlots('image');
        assert.deepStrictEqual(records.frames, [2, 3, 4], 'frame retry order must remain sequential');
        assert.strictEqual(records.requests.length, 0);
    }
    {
        const { context, records } = setup({ dirty: [4, 2, 3] });
        await context.bulkRetryDirtySlots('image');
        assert.deepStrictEqual(records.frames, [2, 3, 4], 'dirty frame retry remains sequential');
        assert.strictEqual(records.requests.length, 0);
    }
    {
        const { context, records } = setup();
        await context.retrySingleVideo(7);
        assert.deepStrictEqual(records.requests[0].body.target_slots, [7]);
        assert.strictEqual(records.requests.length, 1);
    }
    {
        const { context, records } = setup({ absentSlots: [3] });
        const result = await context.retryVideoSlots([2, 3, 4]);
        assert.strictEqual(result.failed, 1);
        assert.strictEqual(records.renders.length, 1, 'missing final slot must be re-rendered out of pending state');
        assert.strictEqual(records.renders[0].active, false);
    }
    for (const options of [
        { mergeError: '自动合并视频失败: disk full' },
        { result: { completion_state: 'partial_failed', has_failures: false } },
        { result: { completion_state: 'partial_failed', has_failures: true } },
    ]) {
        const { context, records } = setup(options);
        const result = await context.bulkRetryDirtySlots('video');
        assert.strictEqual(result.status, 'partial_failed');
        assert.strictEqual(result.succeeded, 3);
        assert.strictEqual(result.failed, 0);
        assert(!records.toasts.some(([, level]) => level === 'success'), 'overall incomplete/merge failure must not report success');
        assert(records.feeds.at(-1)[2] === 'warn');
        assert.strictEqual(records.merges, 0);
    }
    {
        const store = {};
        const calls = [];
        const context = {
            console, ideaTasksById: { owner: { videos: { taskId: 'batch-id', targetSlots: [2, 3, 4] } } },
            localStorage: { setItem: (key, value) => { store[key] = value; }, getItem: key => store[key] },
            findIdeaObjectById: id => ({ id }),
            streamVideosProgress: (...args) => calls.push(args),
        };
        vm.createContext(context);
        vm.runInContext(api.slice(api.indexOf('function saveActiveBackgroundTasksToLocalStorage('),
            api.indexOf('// 后端 server_common.log()')), context);
        context.saveActiveBackgroundTasksToLocalStorage();
        context.resumeActiveBackgroundTasksIfExists();
        assert.strictEqual(calls.length, 1);
        assert.strictEqual(calls[0][0], 'batch-id');
        assert.deepStrictEqual(Array.from(calls[0][2]), [2, 3, 4], 'refresh restore must retain selected subset');
    }
    {
        const { context, records } = setup({ manual: true, failedSlots: [3], retainedPrevious: true });
        const outcome = await context.retryVideoSlots([3]);
        assert.strictEqual(outcome.failed, 1, 'old success must not hide this retry failure');
        assert(records.feeds.some(args => args[0] === 'manual_intervention_detected'), 'retry must show human intervention');
    }
    console.log('Video retry batching tests passed');
})().catch(error => { console.error(error); process.exit(1); });
