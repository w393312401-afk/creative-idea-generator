const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
function section(source, start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from, `Missing source boundary: ${start}`);
    return source.slice(from, to);
}
const helpers = section(app, 'function autoVideoStorageKey(', 'function hasIdeaCover(');
const restoreProgress = section(app, 'function restoreMediaTaskProgress(', 'async function streamFramesProgress(');
const generation = section(app, 'async function generateFrames()', 'async function openCandidateSelectionModal(');
const framesWatcher = section(app, 'async function streamFramesProgress(', 'async function streamVideosProgress(');
const videosWatcher = section(app, 'async function streamVideosProgress(', 'async function streamCoverProgress(');
const review = section(api, 'async function confirmSequenceReviewOverride(', 'async function retrySingleVideo(');
const manifestMerge = section(api, 'function captureManifestReadState(', '/** 任务终态时');
const pipeline = section(app, 'const PIPELINE_STEPS =', '/** 概览页以外');

function setup() {
    const store = new Map(), records = new Map(), streams = [], feeds = [], renders = [], requests = [];
    const elements = {};
    for (const id of ['frames-auto-video-toggle', 'frames-auto-video-status', 'generate-frames-btn',
        'generate-frames-selection-btn', 'frames-progress', 'frames-meta', 'generate-videos-btn',
        'generate-video-chain-btn', 'videos-progress', 'videos-meta', 'video-slot-4', 'video-slot-9']) {
        elements[id] = { checked: false, style: {}, dataset: {}, hidden: false,
            addEventListener(type, callback) { this[type] = callback; } };
    }
    const context = {
        console, AbortController, Map, Set,
        currentIdea: { id: 'owner', title: 'Owner', prompt_block: 'prompts' }, savedIdeas: [],
        document: { getElementById: id => elements[id] || null },
        localStorage: { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
        config: { videoProvider: 'flow2api' }, window: {},
        getIdeaTaskRecord: (id, type) => records.get(`${id}:${type}`),
        beginIdeaTask: (id, type, taskId, controller) => {
            const rec = { taskId, controller };
            records.set(`${id}:${type}`, rec);
            return rec;
        },
        endIdeaTask: (id, type) => records.delete(`${id}:${type}`),
        isIdeaTaskActive: (id, type) => records.has(`${id}:${type}`),
        isIdeaTaskCurrent: (id, type, taskId) => records.get(`${id}:${type}`)?.taskId === taskId,
        isViewingIdea: id => context.currentIdea && context.currentIdea.id === id,
        getMergeSpeed: () => 4,
        computeDebugTargets: kind => kind === 'videos' ? [4, 9] : [2, 5],
        getIdeaSaveTitle: idea => idea.id,
        saveCurrentIdeaState() {}, persistIdeaItem: async () => true,
        framesFeedLine: (...args) => feeds.push(args),
        framesFeedReset() {}, framesFeedSetLive() {},
        streamVideosProgress: (taskId, idea, targets, metadata) => {
            streams.push({ taskId, idea, targets, metadata });
            records.set(`${idea.id}:videos`, { taskId, requestId: metadata.requestId, controller: new AbortController() });
            return Promise.resolve();
        },
        slotRenderTarget: () => ({ children: [], querySelectorAll: () => [] }),
        showToast() {}, setFrameGridButtonsBusy() {}, refreshSlotGridBusy() {},
        renderFramesForIdea: idea => renders.push(['frames', idea.id]),
        renderVideosForIdea: idea => renders.push(['videos', idea.id]),
        summarizeRunQuality: () => null,
        syncFrameRunToLibrary: async (manifest, idea) => { idea.frameRun = manifest; },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, json: async () => ({ task_id: 'frames-task', frames: [], videos: [] }) };
        },
    };
    vm.createContext(context);
    vm.runInContext(helpers, context);
    vm.runInContext(restoreProgress, context);
    return { context, elements, store, records, streams, feeds, renders, requests };
}
const handoff = () => ({ status: 'started', task_id: 'child-task', request_id: 'child-request',
    target_slots: [4, 9], frame_pairs: [
        { slot: 4, start_anchor_slot: 2, end_anchor_slot: 5, source: 'explicit' },
        { slot: 9, start_anchor_slot: 8, end_anchor_slot: null, source: 'single_frame' },
    ] });
const plain = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve;
    const promise = new Promise(yes => { resolve = yes; });
    return { promise, resolve };
}
function enableVideoWatcher(s) {
    const model = require('../js/progress_model.js');
    Object.assign(s.context, {
        console: { ...console, error() {} }, ProgressModel: model, window: { ProgressModel: model },
        saveActiveBackgroundTasksToLocalStorage() {}, setProgressBar() {},
        renderSlotCard() {}, slotPendingState: () => ({}), renderVideoSlotPending() {}, renderVideoSlotFailed() {},
        renderVideoSlotDone(index, video, idea) {
            idea.frameRun.videos = [...(idea.frameRun.videos || []).filter(item => item.slot !== index), video];
        },
        reloadManifestIntoIdea: async () => {}, renderPromptDisplay() {},
        padSlot: slot => String(slot).padStart(3, '0'),
    });
    vm.runInContext(manifestMerge + videosWatcher, s.context);
    s.context.syncFrameRunToLibrary = async (manifest, idea, readState) => {
        idea.frameRun = s.context.mergeManifestReadIntoIdea(manifest, idea, readState);
    };
}

(async () => {
    // The automatic handoff is visible outside the settings popover and defaults to enabled.
    const framesSection = html.slice(html.indexOf('id="frames-section"'), html.indexOf('id="videos-section"'));
    const settingsEnd = framesSection.indexOf('id="frames-info-pop"');
    assert(framesSection.indexOf('id="frames-auto-video-toggle"') > settingsEnd);
    assert(/id="frames-auto-video-toggle"[^>]*\bchecked\b/.test(framesSection));

    {
        const s = setup(), owner = s.context.currentIdea;
        assert.equal(s.context.isAutoGenerateVideosEnabled(owner), true);
        s.context.initAutoVideoControl();
        s.elements['frames-auto-video-toggle'].checked = true;
        s.elements['frames-auto-video-toggle'].change();
        assert.equal(owner.auto_generate_videos, true);
        assert.equal(s.store.get('spark_auto_generate_videos:owner'), 'true');
        const other = { id: 'other' };
        s.context.currentIdea = other;
        s.context.syncAutoVideoToggleFromIdea(other);
        assert.equal(s.elements['frames-auto-video-toggle'].checked, true, 'Each project defaults to automatic video until explicitly disabled');
        s.context.syncAutoVideoToggleFromIdea({ id: 'owner' });
        assert.equal(s.elements['frames-auto-video-toggle'].checked, true, 'A fresh owner snapshot restores its own local preference');
        s.context.syncAutoVideoToggleFromIdea(null);
        assert.equal(s.elements['frames-auto-video-toggle'].disabled, true);
        const restored = { id: 'manifest-owner', auto_generate_videos: false,
            frameRun: { auto_generate_videos: true } };
        s.context.syncAutoVideoToggleFromIdea(restored);
        assert.equal(restored.auto_generate_videos, true, 'A server manifest restores the project option without a local preference');
        s.store.set('spark_auto_generate_videos:owner', 'false');
        assert.equal(s.context.isAutoGenerateVideosEnabled(owner), false, 'A saved explicit off preference survives the new default');
        assert.equal(s.context.isAutoGenerateVideosEnabled({ id: 'legacy-off', auto_generate_videos: false,
            frameRun: { auto_generate_videos: false } }), true, 'An old default off value migrates to enabled');
        assert.equal(s.context.isAutoGenerateVideosEnabled({ id: 'explicit-off', auto_generate_videos: false,
            auto_generate_videos_preference_explicit: true }), false, 'A project explicit off preference remains disabled');
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        for (const phase of ['querying', 'waiting', 'retrying']) {
            s.context.handleAutoVideoHandoff(owner, { ...handoff(), phase,
                slots: [4, 9], retry_round: 2, message: `自动恢复 ${phase}` });
            assert.equal(owner.frameRun.auto_video.status, 'started');
            assert(s.elements['frames-auto-video-status'].textContent.includes(`自动恢复 ${phase}`));
            assert.equal(s.streams.length, 1, '恢复阶段更新不重启子任务观察者');
        }
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        owner.auto_generate_videos = false;
        owner.auto_generate_videos_preference_explicit = true;
        const original = { model: 'image-model' };
        assert.strictEqual(s.context.videoConfigForIdea(original, owner), original);
        owner.auto_generate_videos = true;
        const body = { config: original };
        s.context.applyAutoVideoRequestOptions(body, owner);
        assert.deepStrictEqual(plain(body), { config: { model: 'image-model', videoFramePairing: 'auto' },
            auto_generate_videos_preference_explicit: true,
            auto_generate_videos: true, merge_speed: 4, video_target_slots: [4, 9] });
        assert(!original.videoFramePairing, 'The shared config must remain unchanged');
        owner.auto_generate_videos = false;
        owner.frameRun = { video_frame_pairing: 'auto' };
        assert.equal(s.context.videoConfigForIdea(original, owner).videoFramePairing, 'auto',
            'Disabling future automatic handoff must preserve the mapping used by existing videos');
    }
    // Both image entry points submit the option once and leave child submission to the server.
    for (const enabled of [false, true]) {
        for (const [functionName, endpoint] of [['generateFrames', '/api/generate_frames'],
            ['generateFramesSelection', '/api/generate_frames_selection']]) {
            const s = setup();
            s.context.currentIdea.auto_generate_videos = enabled;
            s.context.currentIdea.auto_generate_videos_preference_explicit = true;
            Object.assign(s.context, {
                isCandidateSelectionMode: () => false, candidateSelectionModeIsExplicit: () => true,
                isSkipCoverReferenceEnabled: () => true, hasIdeaCover: () => false,
                withCoverReference: config => ({ ...config }), markCandidateSelectionMode() {},
                streamFramesProgress: (...args) => s.streams.push(args),
            });
            vm.runInContext(generation, s.context);
            await s.context[functionName]();
            assert.equal(s.requests.length, 1);
            assert.equal(s.requests[0].url, endpoint);
            const body = JSON.parse(s.requests[0].options.body);
            assert.equal(body.auto_generate_videos, enabled);
            assert.deepStrictEqual(body.target_sequences, [2, 5]);
            if (enabled) {
                assert.equal(body.config.videoFramePairing, 'auto');
                assert.deepStrictEqual(body.video_target_slots, [4, 9]);
            } else {
                assert(!body.video_target_slots);
            }
            assert.equal(s.streams.length, 1);
        }
    }
    {
        const s = setup(), owner = s.context.currentIdea, data = handoff();
        s.context.handleAutoVideoHandoff(owner, data);
        s.context.currentIdea = { id: 'other' };
        s.context.handleAutoVideoHandoff(owner, data);
        s.records.delete('owner:videos'); // A completed child still must not reconnect on frame result.
        s.context.handleAutoVideoHandoff(owner, data);
        assert.equal(s.streams.length, 1, 'Event replay and terminal frame result must attach exactly one watcher');
        assert.strictEqual(s.streams[0].idea, owner);
        assert.equal(s.streams[0].metadata.requestId, 'child-request');
        assert.deepStrictEqual(plain(s.streams[0].targets), [4, 9]);
        assert(s.elements['frames-auto-video-status'].textContent.includes('VID 004：IMG 002 → IMG 005'));
        assert(s.elements['frames-auto-video-status'].textContent.includes('VID 009：IMG 008'));
        assert(!s.elements['frames-auto-video-status'].textContent.includes('null'));
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        const pairs = Array.from({ length: 84 }, (_, index) => ({ slot: index + 1,
            start_anchor_slot: index + 1, end_anchor_slot: index + 2 }));
        const all = { ...handoff(), frame_pairs: pairs, target_slots: pairs.map(pair => pair.slot),
            ready_slots: [3, 4, 5, 6, 7], queued_slots: [3, 4, 5, 6, 7], pending_slots: [8] };
        s.context.handleAutoVideoHandoff(owner, all);
        const status = s.elements['frames-auto-video-status'];
        assert.equal((status.textContent.match(/VID /g) || []).length, 4, 'Long projects show at most four visible pairings');
        assert(status.textContent.includes('VID 007'));
        assert(!status.textContent.includes('VID 003'));
        assert(status.textContent.includes('…共 84 段'));
        assert.equal((status.title.match(/VID /g) || []).length, 84, 'The full mapping remains available in the tooltip');
        s.context.handleAutoVideoHandoff(owner, { ...all, queued_slots: [3, 4, 5, 6, 7, 8], pending_slots: [] });
        assert.equal(s.feeds.filter(([, text]) => text.startsWith('🔗')).length, 1, 'Unchanged full pairings do not repeat in the feed');
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        owner.frameRun = { frames: [], auto_video: { ...handoff(), status: 'blocked',
            request_id: 'auto_video:old-parent', message: '旧单已停止', queued_slots: [4, 9] } };
        s.context.handleAutoVideoHandoff(owner, { ...handoff(), request_id: 'auto_video:old-parent' });
        assert.equal(owner.frameRun.auto_video.status, 'blocked', 'Old replay cannot restart a failed child');
        assert.equal(s.streams.length, 0);
        s.context.handleAutoVideoHandoff(owner, { status: 'waiting', request_id: 'auto_video:new-parent',
            target_slots: [20, 21], ready_slots: [], queued_slots: [], pending_slots: [20, 21] });
        assert.equal(owner.frameRun.auto_video.status, 'waiting');
        assert.equal(owner.frameRun.auto_video.task_id, undefined, 'New parent must not inherit the previous child');
        assert.deepStrictEqual(plain(owner.frameRun.auto_video.target_slots), [20, 21]);
        assert.deepStrictEqual(plain(owner.frameRun.auto_video.queued_slots), []);
        assert.equal(owner.frameRun.auto_video.message, undefined);
        assert.equal(s.streams.length, 0);
        assert(s.elements['frames-auto-video-status'].textContent.includes('等待首尾帧就绪'));
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        const waiting = { status: 'waiting', target_slots: [4, 9], frame_pairs: handoff().frame_pairs,
            ready_slots: [], queued_slots: [], pending_slots: [4, 9] };
        s.context.handleAutoVideoHandoff(owner, waiting);
        assert.equal(s.streams.length, 0, 'Waiting for the first pair must not attach a child yet');
        assert(s.elements['frames-auto-video-status'].textContent.includes('等待首尾帧就绪'));
        const first = { ...handoff(), ready_slots: [4], queued_slots: [4], pending_slots: [9] };
        s.context.handleAutoVideoHandoff(owner, first);
        assert.equal(s.streams.length, 1);
        assert(s.elements['frames-auto-video-status'].textContent.includes('已调度 1/2 段'));
        assert(s.elements['frames-auto-video-status'].textContent.includes('1 段等待首尾帧'));
        const next = { ...first, ready_slots: [4, 9], queued_slots: [4, 9], pending_slots: [] };
        s.context.handleAutoVideoHandoff(owner, next);
        s.context.handleAutoVideoHandoff(owner, next);
        assert.equal(s.streams.length, 1, 'New ready pairs update the existing child without reconnecting');
        assert.equal(s.records.get('owner:videos').total, 2);
        assert.deepStrictEqual(plain(s.records.get('owner:videos').targetSlots), [4, 9]);
        assert(s.elements['frames-auto-video-status'].textContent.includes('已调度 2/2 段'));
        assert(!s.elements['frames-auto-video-status'].textContent.includes('等待首尾帧'));
        s.context.handleAutoVideoHandoff(owner, { ...next, status: 'completed' });
        assert(s.elements['frames-auto-video-status'].textContent.includes('自动视频已完成'));
        s.context.handleAutoVideoHandoff(owner, { ...next, status: 'completed_with_warnings', message: '部分片段存在质量风险' });
        assert(s.elements['frames-auto-video-status'].textContent.includes('部分片段存在质量风险'));
        s.context.handleAutoVideoHandoff(owner, { ...next, status: 'cancelled' });
        assert(s.elements['frames-auto-video-status'].textContent.includes('自动视频已取消'));
        assert(!s.elements['frames-auto-video-status'].textContent.includes('等待全部图片'));
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        const model = require('../js/progress_model.js');
        const bars = [];
        Object.assign(s.context, {
            ProgressModel: model, window: { ProgressModel: model },
            saveActiveBackgroundTasksToLocalStorage() {},
            setProgressBar: (_kind, info) => bars.push({ percent: info.percent, total: info.total, label: info.label }),
            renderSlotCard() {}, slotPendingState: () => ({}), renderVideoSlotPending() {},
            renderVideoSlotDone() {}, padSlot: slot => String(slot).padStart(3, '0'),
            watchTaskUntilTerminal: async (_taskId, opts) => {
                opts.onEvent('start', { total: 1, slots: [4] });
                opts.onEvent('video_start', { total: 1, index: 4 });
                opts.onEvent('video_done', { total: 1, index: 4, video: { slot: 4, file: 'one.mp4' } });
                opts.onEvent('start', { total: 1, slots: [9] });
                opts.onEvent('video_start', { total: 1, index: 9 });
                return { status: 'disconnected', error: 'offline mock' };
            },
        });
        vm.runInContext(videosWatcher, s.context);
        await s.context.streamVideosProgress('child-task', owner, [4, 9], { requestId: 'request' });
        const rec = s.records.get('owner:videos');
        assert.equal(rec.total, 2, 'One-slot batches cannot shrink the whole-task target count');
        assert.equal(rec.progressState.total, 2);
        assert.deepStrictEqual(plain(rec.targetSlots), [4, 9]);
        assert(bars.every(info => info.total === 2));
        for (let index = 1; index < bars.length; index++) {
            assert(bars[index].percent >= bars[index - 1].percent, 'New ready batches must not reset progress');
        }
        assert(bars.some(info => info.label.includes('已生成 1/2 段视频')));
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        const restored = { taskId: 'child-task', controller: new AbortController() };
        s.records.set('owner:videos', restored);
        s.context.handleAutoVideoHandoff(owner, handoff());
        s.context.handleAutoVideoHandoff(owner, handoff());
        assert.equal(s.streams.length, 0, 'A restored child watcher must be reused by replayed frame events and result');
        assert.strictEqual(s.records.get('owner:videos'), restored);
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        const existing = { taskId: 'manual-task', controller: new AbortController() };
        s.records.set('owner:videos', existing);
        s.context.handleAutoVideoHandoff(owner, handoff());
        assert.equal(s.streams.length, 0, 'Automatic handoff must not abort another active watcher');
        assert.strictEqual(s.records.get('owner:videos'), existing);
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        s.context.handleAutoVideoHandoff(owner, { status: 'blocked', message: 'IMG 005 尚未生成' });
        assert.equal(s.streams.length, 0);
        assert(s.elements['frames-auto-video-status'].textContent.includes('IMG 005 尚未生成'));
        assert.equal(s.elements['frames-auto-video-status'].dataset.status, 'blocked');
    }
    // Frame result is older than child delivery: keep delivered videos even if the latest read races.
    for (const completed of [false, true]) {
        const s = setup(), owner = s.context.currentIdea, data = handoff();
        const childVideo = { slot: 4, status: 'success', file: 'child.mp4' };
        vm.runInContext(framesWatcher, s.context);
        s.context.watchTaskUntilTerminal = async (_taskId, opts) => {
            opts.onEvent('auto_video_started', data);
            s.context.recordAutoVideoDelivery(owner, 'child-task', childVideo);
            if (completed) s.context.recordAutoVideoDelivery(owner, 'child-task', null,
                { frames: [{ sequence: 2, file: 'frame.png' }], videos: [childVideo], merged_video: { file: 'merged.mp4' } });
            return { status: 'completed', result: { frames: [{ sequence: 2, file: 'frame.png' }], videos: [], auto_video: data } };
        };
        await s.context.streamFramesProgress('parent-task', owner);
        assert.equal(s.streams.length, 1);
        assert.deepStrictEqual(plain(owner.frameRun.videos), [childVideo]);
        if (completed) assert.equal(owner.frameRun.merged_video.file, 'merged.mp4');
        assert(s.renders.some(([type, id]) => type === 'videos' && id === 'owner'));
        assert(s.requests.every(({ options }) => !options || !options.method || options.method === 'GET'),
            'Frame completion may only read the manifest, never submit video generation');
    }
    // 图片父任务可以先结束，视频独立继续；父结果/旧清单不能终止或回退视频。
    for (const completesDuringRead of [false, true]) {
        const s = setup(), owner = s.context.currentIdea, data = handoff();
        enableVideoWatcher(s);
        vm.runInContext(framesWatcher + pipeline
            + section(app, 'function runPipelineNext(', 'function initPipelineBar('), s.context);
        owner.covers = [{ url: 'cover.webp' }];
        owner.prompt_slots = [{ type: 'image', index: 2 }, { type: 'image', index: 5 },
            { type: 'video', index: 4 }, { type: 'video', index: 9 }];
        s.context.resolvePromptSlots = idea => idea.prompt_slots;
        const child = deferred(), read = deferred();
        const frames = [{ sequence: 2, file: 'image2.webp' }, { sequence: 5, file: 'image5.webp' }];
        const videos = [{ slot: 4, status: 'success', file: 'video4.mp4' },
            { slot: 9, status: 'success', file: 'video9.mp4' }];
        let childEvents, childWatchCount = 0;
        s.context.watchTaskUntilTerminal = async (taskId, opts) => {
            if (taskId === 'child-task') { childWatchCount++; childEvents = opts.onEvent; return child.promise; }
            opts.onEvent('auto_video_started', data);
            return { status: 'completed', result: { frames, videos: [], auto_video: data } };
        };
        s.context.fetch = async (url, options) => {
            s.requests.push({ url, options });
            if (completesDuringRead) await read.promise;
            return { ok: true, json: async () => ({ frames, videos: [], auto_video: data }) };
        };
        const parent = s.context.streamFramesProgress('parent-task', owner);
        await tick();
        const videoRec = s.records.get('owner:videos');
        assert.equal(videoRec.streaming, true);
        assert.equal(childWatchCount, 1);
        assert.equal(owner.frameRun.auto_video.status, 'started');
        const childResult = { frames, videos, auto_video: { ...data, status: 'completed', queued_slots: [4, 9] },
            prompt_block: 'latest optimized prompt', prompt_slots: owner.prompt_slots,
            merged_video: { status: 'success', file: 'merged.mp4' }, video_generation_stats: { completed: 2 } };
        if (completesDuringRead) {
            childEvents('auto_video_updated', childResult.auto_video);
            child.resolve({ status: 'completed', result: childResult });
            await tick(); read.resolve();
        }
        await parent;
        assert(!s.records.has('owner:frames'), 'parent completion only releases the frame task');
        assert.equal(s.elements['generate-frames-btn'].disabled, false, 'video child does not leave frame progress busy');
        if (!completesDuringRead) {
            assert.strictEqual(s.records.get('owner:videos'), videoRec);
            assert.equal(videoRec.controller.signal.aborted, false);
            assert.equal(owner.frameRun.auto_video.status, 'started');
            assert(s.elements['frames-auto-video-status'].textContent.includes('视频在后台生成'));
            const state = s.context.computePipelineState(owner);
            assert.equal(state.frames.busy, false);
            assert.equal(state.frames.done, true);
            assert.equal(state.videos.busy, true, 'pipeline retains the independent child busy state');
            s.elements['pipeline-bar'] = { querySelector: () => null };
            s.elements['pipeline-next-btn'] = { dataset: {}, disabled: false };
            s.elements['pipeline-next-text'] = {};
            s.context.updatePipelineBar();
            assert.equal(s.elements['pipeline-next-btn'].disabled, true);
            assert.equal(s.elements['pipeline-next-btn'].dataset.action, '');
            s.context.runPipelineNext();
            childEvents('auto_video_updated', { ...data, queued_slots: [4, 9], pending_slots: [] });
            assert.equal(childWatchCount, 1, 'child scheduling updates reuse the same watcher after parent completion');
            childEvents('video_done', { index: 4, video: videos[0], total: 2 });
            assert.equal(owner.frameRun.videos[0].file, 'video4.mp4');
            child.resolve({ status: 'completed', result: childResult }); await tick();
        }
        assert(!s.records.has('owner:videos'));
        assert.equal(owner.frameRun.auto_video.status, 'completed');
        assert.equal(owner.frameRun.prompt_block, 'latest optimized prompt');
        assert.equal(owner.frameRun.merged_video.file, 'merged.mp4');
        assert.equal(owner.frameRun.video_generation_stats.completed, 2);
        assert.deepStrictEqual(plain(owner.frameRun.frames), frames);
        assert.equal(childWatchCount, 1);
        assert(s.requests.every(({ options }) => !options || !options.method || options.method === 'GET'));
    }
    for (const outcome of ['cancelled', 'failed']) {
        const s = setup(), owner = s.context.currentIdea;
        enableVideoWatcher(s);
        const frameController = new AbortController();
        const frameRec = { taskId: 'running-parent', controller: frameController };
        s.records.set('owner:frames', frameRec);
        owner.frameRun = { frames: [{ sequence: 2, file: 'image2.webp' }], videos: [], auto_video: handoff() };
        if (outcome === 'cancelled') owner.frameRun.auto_video.status = 'completed';
        s.context.watchTaskUntilTerminal = async () => ({ status: outcome, error: 'mock video failure' });
        s.context.reloadManifestIntoIdea = async () => { owner.frameRun.auto_video = handoff(); };
        await s.context.streamVideosProgress('child-task', owner, [4, 9], { autoVideo: handoff() });
        assert.strictEqual(s.records.get('owner:frames'), frameRec);
        assert.equal(frameController.signal.aborted, false, 'child cancellation/failure cannot interrupt images');
        assert(!s.records.has('owner:videos'));
        assert.equal(owner.frameRun.auto_video.status, outcome === 'cancelled' ? 'cancelled' : 'partial_failed');
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        enableVideoWatcher(s);
        owner.frameRun = { frames: [{ sequence: 2, file: 'image2.webp' }], videos: [], auto_video: handoff() };
        const frameRec = { taskId: 'parent', controller: new AbortController() };
        const videoRec = { taskId: 'child-task', controller: new AbortController() };
        s.records.set('owner:frames', frameRec); s.records.set('owner:videos', videoRec);
        s.context.settleVideoOperationView = () => {};
        s.context.reloadManifestIntoIdea = async () => { owner.frameRun.auto_video = handoff(); };
        vm.runInContext(section(api, 'async function cancelVideoOperation(', '/* ── 多创意后台任务登记表'), s.context);
        await s.context.cancelVideoOperation(owner, videoRec);
        assert.equal(owner.frameRun.auto_video.status, 'cancelled');
        assert.strictEqual(s.records.get('owner:frames'), frameRec);
        assert.equal(frameRec.controller.signal.aborted, false);
        assert.equal(videoRec.controller.signal.aborted, true);
        assert.equal(JSON.parse(s.requests[0].options.body).task_id, 'child-task');
    }
    {
        const s = setup(), owner = s.context.currentIdea;
        owner.auto_generate_videos = false;
        owner.frameRun = { video_frame_pairing: 'auto', auto_video: handoff(), frames: [
            { sequence: 2, quality_gate: 'sequence_review_flagged' },
            { sequence: 4, quality_gate: 'rendered_no_gate' },
            { sequence: 5, quality_gate: 'rendered_no_gate' },
        ] };
        let confirmations = 0;
        s.context.customConfirm = async () => { confirmations += 1; return true; };
        vm.runInContext(review, s.context);
        const outcome = await s.context.confirmSequenceReviewOverride(owner, [4]);
        assert.equal(confirmations, 0, 'Retired review flags must not ask for generation approval');
        assert.deepStrictEqual(plain(outcome), { proceed: true, override: true });
    }
    console.log('Automatic image-to-video handoff tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
