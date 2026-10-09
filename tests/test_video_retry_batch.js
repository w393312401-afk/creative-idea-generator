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
    if (options.videos) owner.frameRun = { videos: options.videos };
    const elements = { 'videos-progress': { style: {} }, 'videos-meta': {} };
    let active = !!options.busy;
    let rec = null;
    const context = {
        console: { error() {} }, AbortController, crypto: require('crypto').webcrypto,
        ...(options.recovery ? { ProgressModel: require('../js/progress_model.js') } : {}),
        config: options.config || {}, currentIdea: owner,
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
            if (url === '/api/video-operation/reconcile') return { ok: true, json: async () => ({
                status: 'ok', outcomes: [{ slot: 3, state: 'pending', message: '原任务结果仍待确认' }] }) };
            if (options.submissionPending) return { ok: false, status: 409, text: async () => JSON.stringify({
                status: 'error', failure_code: 'SUBMISSION_PENDING', message: '原提交结果待确认',
                pending_submissions: [{ slot: 3, task_id: 'original-task', request_id: 'original-request' }] }) };
            if (options.httpError) return { ok: false, status: 400, text: async () => 'unavailable' };
            return { ok: true, json: async () => ({ task_id: 'one-batch' }) };
        },
        watchTaskUntilTerminal: async (_taskId, callbacks) => {
            const slots = records.requests.at(-1).body.target_slots;
            assert.deepStrictEqual(Array.from(rec.targetSlots), slots);
            if (options.recovery) {
                for (const index of slots) callbacks.onEvent('video_error', { index, total: slots.length, message: 'temporary failure' });
                for (const phase of ['querying', 'waiting', 'retrying']) {
                    callbacks.onEvent('video_recovery', { phase, slots, completed_slots: [],
                        retry_round: 2, next_retry_at: 2000000000, message: '后台自动恢复' });
                    assert.strictEqual(active, true, '恢复不能释放当前视频任务');
                    assert.strictEqual(rec.progressInfo.current, 0);
                    assert(rec.progressInfo.percent < 100);
                    assert.strictEqual(rec.progressState.recoveryPhase, phase);
                    assert.strictEqual(records.requests.length, 1, '前端不能另发补跑请求');
                }
            }
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
    for (const cancelled of [false, true]) {
        const { context, records, active } = setup({ recovery: true, cancelled });
        const result = await context.retryVideoSlots([2, 3, 4]);
        assert.strictEqual(result.status, cancelled ? 'cancelled' : 'completed');
        assert.strictEqual(records.requests.length, 1);
        assert.strictEqual(records.ends.length, 1);
        assert.strictEqual(active(), false);
        assert(records.renders.some(render => render.active), '恢复阶段即时重画等待卡');
        assert(records.persisted.some(rec => rec.progressState && rec.progressState.recoveryActive), '刷新缓存保留恢复阶段');
    }
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
        const { context, records, active, owner } = setup({ submissionPending: true });
        const result = await context.retryVideoSlots([3]);
        assert.strictEqual(result.status, 'submission_pending');
        assert.strictEqual(result.failed, 0, '未确认提交不能计为生成失败');
        assert.strictEqual(records.failed.length, 0, '不能重画生成失败卡');
        assert.strictEqual(records.requests.length, 1, '409不能自动重发新生成');
        assert.strictEqual(active(), false, '清掉的是本次被拒绝的请求，不假冒原任务仍在运行');
        assert.strictEqual(owner.frameRun.videos[0].last_attempt.task_id, 'original-task');
        assert(owner.frameRun.videos[0].last_attempt.submission_pending);
        assert(records.toasts.every(([, level]) => level === 'warning'));
        assert(!records.toasts.some(([message]) => message.includes('HTTP') || message.includes('failure_code')));
        await context.retryVideoSlots([3]);
        assert.strictEqual(records.requests.length, 1, '已知待确认卡不再提交生成请求');
    }
    for (const record of [
        { provider: 'flow2api', last_attempt: { submission_pending: true } },
        { last_attempt: { provider: 'flow2api', submission_pending: true } },
        { last_attempt: { submission_id: 'legacy-flow', submission_pending: true } },
        { provider: 'flow2api', status: 'success', url: '/old.mp4',
            last_attempt: { status: 'failed', submission_pending: true } },
        { provider: 'flow2api', last_attempt: { confirmed: true, recovery_state: 'recovery_failed' } },
    ]) {
        const video = { slot: 3, status: 'failed', ...record };
        const { context, records } = setup({ videos: [video] });
        const result = await context.retrySingleVideo(3);
        assert.strictEqual(result.status, 'completed', 'Flow2API旧待确认/取回失败允许普通单段重试');
        assert.deepStrictEqual(records.requests[0].body.target_slots, [3]);
        assert.strictEqual(records.requests[0].url, '/api/generate_videos');
        assert.strictEqual(records.requests.length, 1);
        assert(!records.toasts.some(([message]) => message.includes('核对')));
    }
    {
        const { context, records } = setup({ videos: [
            { slot: 2, provider: 'flow2api', status: 'failed', last_attempt: { submission_pending: true } },
            { slot: 3, status: 'failed', last_attempt: { submission_id: 'legacy-flow', submission_pending: true } },
        ] });
        const result = await context.bulkRetrySlots('video');
        assert.strictEqual(result.status, 'completed');
        assert.strictEqual(records.requests.length, 1, 'Flow2API待确认槽位也合并成一个重试请求');
        assert.deepStrictEqual(records.requests[0].body.target_slots, [2, 3, 4]);
    }
    for (const record of [
        { provider: 'google_fx', last_attempt: { fixed_video_account: true, submission_pending: true } },
        { provider: 'flow2api', last_attempt: { provider: 'google_fx', fixed_video_account: true, submission_pending: true } },
        { last_attempt: { provider: 'other_provider', submission_id: 'other', submission_pending: true } },
    ]) {
        const { context, records } = setup({ videos: [{ slot: 3, status: 'failed', ...record }] });
        const result = await context.retryVideoSlots([3]);
        assert.strictEqual(result.status, 'submission_pending', '保留固定原生账号和其他通道的原提交保护');
        assert.strictEqual(records.requests.length, 0);
    }
    for (const provider of ['flow2api', 'google_fx']) {
        const original = { slot: 3, provider: 'google_fx', status: 'success', url: '/old.mp4',
            last_attempt: { provider: 'google_fx', fixed_video_account: true,
                submission_id: 'native-pending', submission_pending: true } };
        const { context, records } = setup({ config: { videoProvider: provider }, videos: [original] });
        const result = await context.bulkRetrySlots('video');
        assert.strictEqual(result.status, provider === 'flow2api' ? 'completed' : 'submission_pending');
        assert.strictEqual(records.requests.length, provider === 'flow2api' ? 1 : 0,
            '当前Flow2API允许换通道直接重试；当前固定原生账号仍保留保护');
        if (provider === 'flow2api') {
            assert.strictEqual(records.requests[0].url, '/api/generate_videos');
            assert.strictEqual(records.requests[0].body.config.videoProvider, 'flow2api');
            assert.deepStrictEqual(records.requests[0].body.target_slots, [2, 3, 4]);
        }
        assert.strictEqual(original.last_attempt.submission_pending, true, '重试不伪造旧回执结清');
    }
    {
        const { context, records } = setup({ videos: [{ slot: 3, provider: 'flow2api', status: 'failed',
            last_attempt: { submission_pending: true } }] });
        const result = await context.reconcileVideoSubmission(3);
        assert.strictEqual(result.status, 'ok', 'Flow2API核对只查询原提交');
        assert.strictEqual(records.requests[0].url, '/api/video-operation/reconcile');
        assert.strictEqual(records.requests.length, 1);
        assert.strictEqual(records.reloaded.length, 1, '核对后重新读取服务端最新清单');
        assert.strictEqual(records.pending.length, 0, '查询原提交不渲染新生成占位');
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
        const persistence = api.slice(api.indexOf('function saveActiveBackgroundTasksToLocalStorage('),
            api.indexOf('// 后端 server_common.log()'));
        vm.createContext(context);
        vm.runInContext(persistence, context);
        context.saveActiveBackgroundTasksToLocalStorage();
        // 刷新会创建新页面上下文，只共享持久化 storage，不继承旧页活动登记/缓存。
        const refreshed = { ...context, ideaTasksById: {} };
        vm.createContext(refreshed);
        vm.runInContext(persistence, refreshed);
        refreshed.resumeActiveBackgroundTasksIfExists();
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
