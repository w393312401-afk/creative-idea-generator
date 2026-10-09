const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const media = fs.readFileSync(path.join(__dirname, '../js/media_renderer.js'), 'utf8');
function section(source, start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from);
    return source.slice(from, to);
}
const registry = section(api, 'function _ideaTaskSlot(', '/**\n * 把一个');
const discovery = section(api, '// 只读发现其它入口已启动的视频任务', '/** 持久化');
const persistence = section(api, 'function saveActiveBackgroundTasksToLocalStorage(', '// 后端 server_common.log()');
const manifest = section(api, 'function captureManifestReadState(', '/** 视频槽位卡片渲染');
const watcher = section(api, 'async function watchTaskUntilTerminal(', '// 视频请求在收到 task_id');
const operations = section(api, 'function newVideoRequestId(', '/* ── 多创意后台任务登记表');
const hydrators = section(media, 'function hydrateFramesPanel(', 'function hydrateCoverPanel(');
const renderIdea = section(media, 'function renderIdea(', '/**\n * 帧序列/视频/封面');
const deferred = () => {
    let resolve;
    const promise = new Promise(yes => { resolve = yes; });
    return { promise, resolve };
};
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(options = {}) {
    const owner = { id: 'fresh-client-id', title: 'Same visible title', project_key: 'exact-project' };
    const stored = new Map([['spark_active_background_tasks', JSON.stringify({ tasks: options.cached || [] })]]);
    const observed = { requests: [], streams: [], timers: [], saved: [] };
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, { style: {}, dataset: {}, textContent: '',
            classList: { add() {}, remove() {}, toggle() {} } });
        return elements.get(id);
    };
    const context = { console: { ...console, warn() {}, error() {} }, AbortController, TextDecoder, URLSearchParams,
        ideaTasksById: {}, savedIdeas: options.ideas || [owner], currentIdea: owner,
        localStorage: { getItem: key => stored.get(key) || null,
            setItem: (key, value) => stored.set(key, value) },
        getIdeaSaveTitle: idea => idea.project_key || idea.title,
        fetch: async (url, init = {}) => {
            const requestIndex = observed.requests.length;
            observed.requests.push({ url, init });
            if (options.wait) await options.wait.promise;
            if (options.offline) throw Error('offline');
            return { ok: true, json: async () => ({ tasks: options.taskReplies
                ? options.taskReplies[requestIndex] || [] : options.tasks || [] }) };
        },
        document: { getElementById: element }, slotRenderTarget: () => ({ querySelectorAll: () => [] }),
        renderFramesForIdea() {}, renderVideosForIdea() {}, framesFeedHydrate() {},
        updatePipelineBar() {}, setProgressBar() {},
        scheduleVideoOperationRecovery: (_idea, rec) => observed.timers.push(rec),
        saveCurrentIdeaState: () => observed.saved.push(context.currentIdea),
        persistIdeaItem: async () => {},
    };
    for (const [type, name] of [['frames', 'streamFramesProgress'], ['videos', 'streamVideosProgress'],
        ['cover', 'streamCoverProgress']]) {
        context[name] = (taskId, idea, targets, metadata = {}) => {
            observed.streams.push({ type, taskId, idea, targets, metadata });
            const rec = context.beginIdeaTask(idea.id, type, taskId, new AbortController());
            Object.assign(rec, metadata.resumeSnapshot || {}, { taskId, streaming: true,
                projectKey: idea.project_key, requestId: metadata.requestId });
            if (targets) rec[type === 'frames' ? 'targetSequences' : 'targetSlots'] = targets;
            context.saveActiveBackgroundTasksToLocalStorage();
            return new Promise(() => {});
        };
    }
    vm.createContext(context);
    vm.runInContext(registry + discovery + persistence + hydrators, context);
    return { context, owner, observed, stored, element,
        snapshots: () => JSON.parse(stored.get('spark_active_background_tasks')).tasks };
}
function runningTask(type, id, more = {}) {
    return { id, status: 'running', dimensions: { type, project_key: 'exact-project',
        ...(type === 'frames' ? { target_sequences: [1, 2, 3] }
            : { target_slots: [1, 2], request_id: 'child-request' }) },
        progress: { total: 3, current: 1, slot_status: { 1: 'done', 2: 'active' }, phase: 'generating', label: '生成中' },
        ...more };
}
(async () => {
    {
        const wait = deferred();
        const handoff = { status: 'started', task_id: 'child', target_slots: [1, 2], pending_slots: [2] };
        const s = setup({ wait, tasks: [runningTask('frames', 'parent', { auto_video: handoff }),
            runningTask('videos', 'child', { auto_video: handoff }),
            runningTask('frames', 'wrong', { dimensions: { type: 'frames', project_key: 'exact-project-extra' } })] });
        s.context.hydrateFramesPanel(s.owner); s.context.hydrateVideosPanel(s.owner);
        s.context.hydrateFramesPanel(s.owner); s.context.hydrateVideosPanel(s.owner);
        await tick(); assert.equal(s.observed.requests.length, 1, 'both parent and child share one server lookup');
        wait.resolve(); await tick(); await tick();
        assert.deepStrictEqual(s.observed.streams.map(stream => stream.taskId), ['parent', 'child']);
        assert.deepStrictEqual(Array.from(s.observed.streams[0].targets), [1, 2, 3]);
        assert.equal(s.observed.streams[0].metadata.progress.current, 1);
        assert.equal(s.observed.streams[1].metadata.autoVideo.pending_slots[0], 2);
        assert.equal(s.observed.streams[1].metadata.requestId, 'child-request');
        s.context.hydrateFramesPanel(s.owner); s.context.hydrateVideosPanel(s.owner);
        await tick(); assert.equal(s.observed.streams.length, 2, 'hydrate cannot duplicate an attached watcher');
        assert(s.observed.requests.every(call => !call.init.method && call.init.cache === 'no-store'));
        const params = new URL(s.observed.requests[0].url, 'http://localhost').searchParams;
        assert.equal(params.get('project_key'), 'exact-project', 'discovery excludes unrelated project history');
    }
    {
        const s = setup({ tasks: [runningTask('frames', 'misregistered', {
            status: 'failed', dimensions: { type: 'frames', project_key: 'another-project' } })] });
        const frame = s.context.beginIdeaTask(s.owner.id, 'frames', 'misregistered', new AbortController());
        Object.assign(frame, { streaming: false });
        const video = s.context.beginIdeaTask(s.owner.id, 'videos', 'old-video', new AbortController());
        Object.assign(video, { streaming: false });
        await s.context.reconnectRunningFrameTaskForIdea(s.owner);
        const params = new URL(s.observed.requests[0].url, 'http://localhost').searchParams;
        assert.deepStrictEqual(params.getAll('include_id'), ['misregistered', 'old-video']);
        assert.equal(s.observed.streams.length, 0, 'included mismatched terminal task must never acquire a watcher');
    }
    {
        const wait = deferred();
        const s = setup({ wait, taskReplies: [[], [runningTask('frames', 'late-record', {
            status: 'failed', dimensions: { type: 'frames', project_key: 'different-project' } })]] });
        const discovery = s.context.reconnectRunningFrameTaskForIdea(s.owner);
        await tick();
        const late = s.context.beginIdeaTask(s.owner.id, 'frames', 'late-record', new AbortController());
        late.streaming = false;
        wait.resolve();
        await discovery;
        assert.equal(s.observed.requests.length, 2, 'registration restored during lookup needs ownership reconciliation');
        const params = new URL(s.observed.requests[1].url, 'http://localhost').searchParams;
        assert.deepStrictEqual(params.getAll('include_id'), ['late-record']);
        assert.equal(s.observed.streams.length, 0, 'late mismatched registration cannot receive a synthetic watcher');
    }
    {
        for (const tasks of [[], [runningTask('frames', 'terminal-parent', { status: 'failed' })]]) {
            const s = setup({ tasks });
            const rec = s.context.beginIdeaTask(s.owner.id, 'frames', 'terminal-parent', new AbortController());
            Object.assign(rec, { streaming: false, targetSequences: [21, 22], total: 2 });
            await s.context.reconnectRunningFrameTaskForIdea(s.owner);
            assert.equal(s.observed.streams.length, 1,
                'Disconnected cached task still gets authoritative terminal reconciliation when no running task exists');
            assert.equal(s.observed.streams[0].taskId, 'terminal-parent');
            assert.strictEqual(s.observed.streams[0].metadata.resumeSnapshot, rec);
            assert(s.observed.requests.every(call => !call.init.method));
        }
    }
    {
        const orphan = { ideaId: 'old-client-id', projectKey: 'missing-project', type: 'frames',
            taskId: 'orphan', targetSequences: [3, 8], total: 2, progressState: { percent: 40 } };
        const s = setup({ cached: [orphan] });
        s.context.resumeActiveBackgroundTasksIfExists();
        const rec = s.context.beginIdeaTask(s.owner.id, 'videos', 'other-task');
        rec.targetSlots = [2]; s.context.saveActiveBackgroundTasksToLocalStorage();
        s.context.endIdeaTask(s.owner.id, 'videos');
        assert.deepStrictEqual(s.snapshots(), [orphan], 'another task ending cannot erase an unresolved old owner');
        s.context.currentIdea = { id: 'new-id', project_key: 'missing-project' };
        s.context.resumeActiveBackgroundTasksIfExists();
        assert.equal(s.observed.streams[0].idea.id, 'new-id', 'same canonical project repairs obsolete client identity');
        assert.deepStrictEqual(Array.from(s.observed.streams[0].targets), [3, 8]);
        assert.equal(s.observed.streams[0].metadata.resumeSnapshot.progressState.percent, 40);
        s.context.resumeActiveBackgroundTasksIfExists();
        assert.equal(s.observed.streams.length, 1);
        assert.equal(s.snapshots()[0].projectKey, 'missing-project');
    }
    {
        const cached = [{ ideaId: 'fresh-client-id', projectKey: 'exact-project', type: 'frames', taskId: 'parent',
            targetSequences: [2, 4], total: 2, meta: 'IMG 2/4', feedLines: [{ text: '已交付' }],
            progressState: { percent: 50, slotStatus: { 2: 'done', 4: 'active' } } },
        { ideaId: 'fresh-client-id', projectKey: 'exact-project', type: 'videos', taskId: 'child',
            requestId: 'child-request', targetSlots: [2], autoVideo: { status: 'started', pending_slots: [2] } }];
        const s = setup({ cached, offline: true });
        s.context.resumeActiveBackgroundTasksIfExists();
        s.context.resumeActiveBackgroundTasksIfExists();
        assert.equal(s.observed.streams.length, 2);
        assert.deepStrictEqual(Array.from(s.observed.streams[0].targets), [2, 4]);
        assert.equal(s.snapshots()[0].progressState.percent, 50);
        assert.equal(s.snapshots()[1].requestId, 'child-request');
        assert.equal(s.observed.requests.length, 0, 'local resume attaches original streams without new jobs');
        s.context.getIdeaTaskRecord(s.owner.id, 'frames').streaming = false;
        await s.context.reconnectRunningFrameTaskForIdea(s.owner);
        assert.equal(s.snapshots().length, 2, 'offline lookup keeps both original task records');
    }
    {
        const s = setup({ cached: [
            { ideaId: 'old-id', projectKey: 'exact-project', type: 'frames', taskId: 'same-parent' },
            { ideaId: 'fresh-client-id', projectKey: 'exact-project', type: 'frames', taskId: 'same-parent' },
        ] });
        s.context.savedIdeas.push({ id: 'old-id', project_key: 'exact-project' });
        s.context.resumeActiveBackgroundTasksIfExists();
        assert.equal(s.observed.streams.length, 1, 'obsolete aliases cannot attach two watchers to one server task');
        assert.strictEqual(s.observed.streams[0].idea, s.owner, 'exact project binds the actual viewed owner');
    }
    {
        const s = setup({ cached: [{ ideaId: 'fresh-client-id', projectKey: 'exact-project', type: 'videos',
            requestId: 'unknown-ack', requestEndpoint: '/api/generate_videos', requestBody: { request_id: 'unknown-ack' } }] });
        s.context.resumeActiveBackgroundTasksIfExists();
        assert.equal(s.observed.timers[0].readOnlyRecovery, true);
        const calls = [], timers = [];
        const ctx = { console, getIdeaTaskRecord: () => s.observed.timers[0],
            setTimeout: fn => { timers.push(fn); return timers.length; },
            fetch: async (url, init) => { calls.push({ url, init }); return { status: 404, ok: false }; } };
        vm.createContext(ctx); vm.runInContext(operations, ctx);
        ctx.scheduleVideoOperationRecovery(s.owner, s.observed.timers[0]); await timers[0]();
        assert.equal(calls.length, 1);
        assert(calls[0].url.startsWith('/api/video-operation?'));
        assert(!calls[0].init.method, 'uncertain refresh may query original request, never POST another generation');
    }
    {
        const calls = [];
        const ctx = { console: { warn() {}, error() {} }, TextDecoder, Date, AbortController,
            fetch: async (url, init) => { calls.push({ url, init });
                return url.includes('compose-stream') ? { status: 404, ok: false }
                    : { ok: true, json: async () => ({ status: 'running' }) }; } };
        vm.createContext(ctx); vm.runInContext(watcher, ctx);
        const result = await ctx.watchTaskUntilTerminal('still-running', { maxReconnects: 0, statusPollIntervalMs: 0 });
        assert.equal(result.status, 'disconnected', 'SSE 404 plus authoritative running keeps original task active');
        assert(calls.every(call => call.init.cache === 'no-store'));
    }
    {
        const s = setup();
        vm.runInContext(manifest, s.context);
        s.context.beginIdeaTask(s.owner.id, 'frames', 'live');
        s.owner.frameRun = { frames: [{ sequence: 1, file: 'old1' }, { sequence: 2, file: 'old2' }],
            videos: [{ slot: 1, file: 'old-video' }], auto_video: { pending_slots: [1, 2] } };
        const read = s.context.captureManifestReadState(s.owner);
        s.owner.frameRun.frames[0] = { sequence: 1, file: 'new1' };
        s.owner.frameRun.frames.push({ sequence: 3, file: 'new3' });
        s.owner.frameRun.frames = s.owner.frameRun.frames.filter(frame => frame.sequence !== 2);
        s.owner.frameRun.videos[0] = { slot: 1, file: 'new-video' };
        s.owner.frameRun.auto_video = { pending_slots: [2] };
        const merged = s.context.mergeManifestReadIntoIdea({ frames: [{ sequence: 1, file: 'old1' },
            { sequence: 2, file: 'old2' }], videos: [], auto_video: { pending_slots: [1, 2] } }, s.owner, read);
        assert.deepStrictEqual(Array.from(merged.frames, frame => frame.file), ['new1', 'new3']);
        assert.equal(merged.videos[0].file, 'new-video'); assert.deepStrictEqual(merged.auto_video.pending_slots, [2]);
        s.context.endIdeaTask(s.owner.id, 'frames');
        const inactive = s.context.captureManifestReadState(s.owner);
        const authority = { frames: [], videos: [] };
        assert.strictEqual(s.context.mergeManifestReadIntoIdea(authority, s.owner, inactive), authority);
        // 无改动的旧缓存不能复活服务器明确删除的帧。
        s.context.beginIdeaTask(s.owner.id, 'frames', 'another');
        assert.equal(s.context.mergeManifestReadIntoIdea(authority, s.owner,
            s.context.captureManifestReadState(s.owner)).frames.length, 0);
        const preferenceRead = s.context.captureManifestReadState(s.owner);
        s.owner.auto_generate_videos = false;
        s.owner.auto_generate_videos_preference_explicit = true;
        const preference = s.context.mergeManifestReadIntoIdea({ auto_generate_videos: true,
            auto_generate_videos_preference_explicit: false, frames: [], videos: [] }, s.owner, preferenceRead);
        assert.equal(preference.auto_generate_videos, false);
        assert.equal(preference.auto_generate_videos_preference_explicit, true);
        const textRead = s.context.captureManifestReadState(s.owner);
        s.owner.prompt_block = 'autofixed latest prompt';
        s.owner.prompt_slots = { images: [1, 2] };
        s.owner.frameRun.merged_video = { file: 'latest-merged.mp4' };
        s.owner.frameRun.video_generation_stats = { completed: 2 };
        s.owner.frameRun.future_terminal_field = { delivered: true };
        const terminal = s.context.mergeManifestReadIntoIdea({ frames: [], videos: [],
            prompt_block: 'stale prompt', prompt_slots: { images: [1] } }, s.owner, textRead);
        assert.equal(terminal.prompt_block, 'autofixed latest prompt');
        assert.equal(terminal.prompt_slots.images.length, 2);
        assert.equal(terminal.merged_video.file, 'latest-merged.mp4');
        assert.equal(terminal.video_generation_stats.completed, 2);
        assert.equal(terminal.future_terminal_field.delivered, true, 'all changed terminal metadata stays delivered');
        const deletionRead = s.context.captureManifestReadState(s.owner);
        delete s.owner.frameRun.future_terminal_field;
        const deleted = s.context.mergeManifestReadIntoIdea(terminal, s.owner, deletionRead);
        assert.equal(deleted.future_terminal_field, undefined, 'explicit metadata deletion cannot be resurrected');
    }
    for (const responseMode of ['stale', 'late-task-finished', 'active-404', 'finished-404', 'inactive-404-switch']) {
        const response = deferred(), s = setup();
        const ctx = s.context;
        Object.assign(ctx, { renderIdeaTitles() {}, renderRepairBanner() {}, renderAuditMarkdown: () => '',
            updateFavoriteButtonState() {}, renderCoversForIdea() {}, hydrateCoverPanel() {},
            safeSetImageSrc() {},
            fetch: async url => url.startsWith('/api/get_manifest') ? response.promise
                : { ok: true, json: async () => ({ tasks: [] }) } });
        vm.runInContext(manifest + renderIdea, ctx);
        s.owner.frameRun = { frames: [{ sequence: 1, file: 'before' }], videos: [] };
        if (!['inactive-404-switch', 'late-task-finished', 'finished-404'].includes(responseMode)) {
            ctx.beginIdeaTask(s.owner.id, 'frames', 'parent');
        }
        ctx.renderIdea(s.owner);
        if (['late-task-finished', 'finished-404'].includes(responseMode)) {
            ctx.beginIdeaTask(s.owner.id, 'frames', 'discovered-during-get');
        }
        if (['stale', 'late-task-finished', 'finished-404'].includes(responseMode)) {
            s.owner.frameRun.frames.push({ sequence: 2, file: 'delivered-during-read' });
            s.owner.frameRun.videos.push({ slot: 1, status: 'success', file: 'child-during-read' });
            if (responseMode !== 'stale') ctx.endIdeaTask(s.owner.id, 'frames');
            response.resolve(responseMode === 'finished-404' ? { ok: false, status: 404 }
                : { ok: true, json: async () => ({ frames: [{ sequence: 1, file: 'before' }], videos: [] }) });
            await tick(); await tick();
            assert.equal(s.owner.frameRun.frames[1].file, 'delivered-during-read');
            assert.equal(s.owner.frameRun.videos[0].file, 'child-during-read');
        } else {
            if (responseMode === 'inactive-404-switch') ctx.currentIdea = { id: 'other-idea' };
            response.resolve({ ok: false, status: 404 }); await tick(); await tick();
            if (responseMode === 'active-404') assert.equal(s.owner.frameRun.frames[0].file, 'before');
            else {
                assert.equal(s.owner.frameRun, undefined, 'inactive server deletion clears obsolete assets');
                assert.equal(s.observed.saved.length, 0, 'background manifest cannot save another visible owner');
            }
        }
    }
    console.log('Media refresh recovery tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
