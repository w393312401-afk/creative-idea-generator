const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ProgressModel = require('../js/progress_model.js');
const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const configSource = fs.readFileSync(path.join(__dirname, '../js/config.js'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function section(source, start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from, `Missing source boundary: ${start}`);
    return source.slice(from, to);
}
function setup() {
    const records = new Map(), bars = [], pending = [], failed = [], snapshots = [], cards = new Map();
    const owner = { id: 'owner', title: 'Owner', project_key: 'Owner_canonical', frameRun: { frames: [], videos: [] } };
    const elements = new Map();
    const grid = { children: [], querySelectorAll: () => [], appendChild(card) { cards.set(card.id, card); this.children.push(card); } };
    const context = {
        console, AbortController, Map, Set, ProgressModel, window: { ProgressModel }, currentIdea: owner, savedIdeas: [owner],
        document: { getElementById(id) {
            if (id.startsWith('video-slot-')) return cards.get(id) || null;
            if (!elements.has(id)) elements.set(id, { style: {}, dataset: {}, classList: { toggle() {} } });
            return elements.get(id);
        }, createElement: () => ({ style: {}, dataset: {} }) },
        localStorage: { getItem: () => null, setItem() {} },
        getIdeaSaveTitle: idea => idea.project_key,
        getIdeaTaskRecord: (id, kind) => records.get(`${id}:${kind}`),
        beginIdeaTask: (id, kind, taskId, controller) => {
            const rec = { taskId, controller, total: 0, feedLines: [] };
            records.set(`${id}:${kind}`, rec); return rec;
        },
        endIdeaTask: (id, kind) => records.delete(`${id}:${kind}`),
        isIdeaTaskActive: (id, kind) => records.has(`${id}:${kind}`),
        isViewingIdea: id => id === owner.id,
        slotRenderTarget: () => grid,
        saveActiveBackgroundTasksToLocalStorage: () => snapshots.push(plain(Array.from(records.values()).map(rec => ({
            total: rec.total, current: rec.current, progressState: rec.progressState, autoVideo: rec.autoVideo,
            targetSequences: rec.targetSequences, targetSlots: rec.targetSlots, streaming: rec.streaming
        })))),
        setProgressBar: (kind, info) => bars.push({ kind, ...plain(info) }),
        framesFeedLine() {}, framesFeedReset() {}, framesFeedHydrate() {}, framesFeedSetLive() {}, framesFeedQualityLine() {},
        ensureFrameRunForStart: idea => idea.frameRun,
        applyFrameEventToIdea(frame, idea) {
            const map = new Map(idea.frameRun.frames.map(row => [row.sequence, row]));
            map.set(frame.sequence, frame); idea.frameRun.frames = Array.from(map.values());
        },
        updateFrameSlotCard() {}, setFrameGridButtonsBusy() {}, refreshSlotGridBusy() {},
        saveCurrentIdeaState() {}, renderFramesForIdea() {}, renderVideosForIdea() {}, renderAutoVideoStatus() {},
        syncAutoVideoToggleFromIdea() {}, showToast() {}, enableVideoSlotDnd() {}, placeSlotCard() {}, clearSlotGrid() {},
        slotPendingState: (_kind, slot) => ({ slot }), renderSlotCard: (_card, state) => pending.push(state.slot),
        renderVideoSlotPending: slot => pending.push(slot), renderVideoSlotFailed: slot => failed.push(slot),
        renderVideoSlotDone(slot, video, idea) {
            const rows = new Map(idea.frameRun.videos.map(row => [row.slot, row]));
            rows.set(slot, video); idea.frameRun.videos = Array.from(rows.values());
        }, padSlot: slot => String(slot).padStart(3, '0'),
    };
    vm.createContext(context);
    vm.runInContext(section(app, 'function autoVideoStorageKey(', 'function hasIdeaCover('), context);
    vm.runInContext(section(app, 'function restoreMediaTaskProgress(', 'async function streamCoverProgress('), context);
    return { context, owner, records, bars, pending, failed, snapshots, elements, cards };
}

test('frame recovery restores counts, targets, feed and auto video before replaying history', async () => {
    const s = setup();
    s.owner.frameRun.frames = Array.from({ length: 6 }, (_, i) => ({ sequence: i + 1, file: `f${i + 1}.png` }));
    const autoVideo = { status: 'waiting', target_slots: [1, 2], queued_slots: [], pending_slots: [1, 2] };
    s.context.watchTaskUntilTerminal = async (_task, opts) => {
        const rec = s.records.get('owner:frames');
        assert.equal(rec.total, 10);
        assert.equal(rec.current, 6);
        assert.equal(rec.projectKey, 'Owner_canonical');
        assert.equal(rec.progressState.phase, 'frame');
        assert.equal(rec.progressInfo.status, 'running');
        assert.equal(rec.feedLines.length, 1);
        assert.deepEqual(plain(rec.autoVideo), autoVideo);
        opts.onEvent('start', { total: 1 });
        opts.onEvent('frame_start', { sequence: 2, total: 1 });
        opts.onEvent('frame', { current: 1, total: 1, frame: { sequence: 1, file: 'f1.png' } });
        return { status: 'disconnected', error: 'offline mock' };
    };
    await s.context.streamFramesProgress('frame-task', s.owner, null, {
        projectKey: 'Owner_canonical', autoVideo,
        resumeSnapshot: { total: 9, targetSequences: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            feedLines: [{ text: 'already delivered', time: '2026-10-04T01:00:00Z' }],
            progressState: { total: 9, current: 5, percent: 55, slotStatus: { 1: 'done', 2: 'done' } } },
        progress: { total: 10, current: 6, percent: 59, phase: 'frame', label: '图片生成 6/10',
            slot_status: Object.fromEntries(Array.from({ length: 6 }, (_, i) => [i + 1, 'done'])) }
    });
    const rec = s.records.get('owner:frames');
    assert.equal(rec.total, 10);
    assert.equal(rec.current, 6);
    assert.equal(rec.progressState.current, 6);
    assert.equal(rec.streaming, false);
    assert.equal(s.owner.frameRun.frames.length, 6);
    assert(s.bars.every(info => info.total === 10 && info.current >= 6 && info.percent >= 59));
    assert(s.snapshots.some(rows => rows.some(row => row.current === 6 && row.total === 10)));
});

test('video recovery preserves delivered cards and never starts a second watcher for the same task', async () => {
    const s = setup();
    s.owner.frameRun.videos = [{ slot: 5, status: 'success', file: 'ready.mp4' }];
    let finish, calls = 0;
    s.context.watchTaskUntilTerminal = async (_task, opts) => {
        calls++;
        if (calls > 1) return { status: 'disconnected', error: 'offline mock' };
        opts.onEvent('start', { total: 1, slots: [5] });
        opts.onEvent('video_start', { index: 5, total: 1, current: 1 });
        opts.onEvent('video_error', { index: 5, total: 1, message: 'old error' });
        return await new Promise(resolve => { finish = resolve; });
    };
    const active = s.context.streamVideosProgress('video-task', s.owner, [5, 9], {
        progress: { total: 2, current: 1, phase: 'video_done', label: '已生成 1/2 段视频', slotStatus: { 5: 'done', 9: 'queued' } }
    });
    const rec = s.records.get('owner:videos');
    const controller = rec.controller;
    await s.context.streamVideosProgress('video-task', s.owner, [5, 9], {
        resumeSnapshot: { total: 2, current: 1, progressState: { percent: 47, slotStatus: { 5: 'done' } } }
    });
    assert.equal(calls, 1);
    assert.equal(s.records.get('owner:videos'), rec);
    assert.equal(rec.controller, controller);
    assert.equal(controller.signal.aborted, false);
    assert.equal(rec.progressState.slotStatus[5], 'done');
    assert(!s.pending.includes(5));
    assert(!s.failed.includes(5));
    finish({ status: 'disconnected', error: 'offline mock' });
    await active;
    assert.equal(rec.streaming, false);
    await s.context.streamVideosProgress('video-task', s.owner, [5, 9]);
    // The second disconnected listener reaches watch, rather than being mistaken for a live listener.
    assert.equal(calls, 2);
});

test('a fresh rerun does not count files from the previous generation', () => {
    const s = setup();
    s.owner.frameRun.frames = [{ sequence: 1, file: 'old.png' }];
    s.owner.frameRun.videos = [{ slot: 1, status: 'success', file: 'old.mp4' }];
    for (const kind of ['frames', 'videos']) {
        const rec = { taskId: `fresh-${kind}`, feedLines: [] };
        s.context.restoreMediaTaskProgress(rec, kind, s.owner, [1]);
        assert.equal(rec.current, 0);
        assert.deepEqual(plain(rec.progressState.slotStatus), {});
        assert(rec.progressInfo.percent < 100);
        const restored = { taskId: `fresh-${kind}`, feedLines: [] };
        s.context.restoreMediaTaskProgress(restored, kind, s.owner, [1], {
            resumeSnapshot: { current: 0, progressState: { current: 0, slotStatus: {} } },
            progress: { total: 1, current: 0, slot_status: {} }
        });
        assert.equal(restored.current, 0, 'Resuming an unstarted rerun does not count old files');
        assert.deepEqual(plain(restored.progressState.slotStatus), {});
    }
});

test('server progress restores an active retry even when the browser cached done', () => {
    const s = setup();
    s.owner.frameRun.videos = [{ slot: 5, status: 'success', file: 'old.mp4' }];
    const rec = { taskId: 'video-task', feedLines: [] };
    s.context.restoreMediaTaskProgress(rec, 'videos', s.owner, [5, 9], {
        resumeSnapshot: { progressState: { total: 2, current: 1, percent: 47, slotStatus: { 5: 'done' } } },
        progress: { total: 2, current: 0, phase: 'video_start', slot_status: { 5: 'active', 9: 'queued' } }
    });
    assert.equal(rec.progressState.slotStatus[5], 'active');
    assert.equal(rec.progressInfo.status, 'running');
});

test('an active child with 100 percent remains running while waiting for the parent', () => {
    const s = setup();
    const rec = { taskId: 'video-task', feedLines: [] };
    s.records.set('owner:videos', rec);
    s.context.restoreMediaTaskProgress(rec, 'videos', s.owner, [5, 9], {
        progress: { total: 2, current: 2, percent: 100, phase: 'waiting_for_frames', label: '等待后续帧', slot_status: { 5: 'done', 9: 'done' } }
    });
    assert.equal(rec.progressInfo.percent, 100);
    assert.equal(rec.progressInfo.status, 'running');
    assert.equal(rec.progressInfo.phase, 'waiting_for_frames');
});

test('refresh during automatic video recovery counts delivered slots and preserves waiting state', () => {
    const s = setup();
    const rec = { taskId: 'recovering-video-task', feedLines: [] };
    s.records.set('owner:videos', rec);
    const targets = Array.from({ length: 27 }, (_, i) => i + 1);
    s.context.restoreMediaTaskProgress(rec, 'videos', s.owner, targets, {
        resumeSnapshot: { total: 27, current: 27, progressState: { current: 27, percent: 100,
            slotStatus: Object.fromEntries(targets.map(slot => [slot, slot <= 24 ? 'done' : 'failed'])) } },
        progress: { total: 27, current: 24, percent: 95, phase: 'video_recovery', message: '等待自动恢复',
            slot_status: Object.fromEntries(targets.map(slot => [slot, slot <= 24 ? 'done' : 'recovering'])) }
    });
    assert.equal(rec.current, 24);
    assert.equal(rec.progressInfo.current, 24);
    assert(rec.progressInfo.percent < 85);
    assert.equal(rec.progressInfo.status, 'running');
    assert.equal(rec.progressState.phase, 'video_recovery');
    assert.equal(rec.progressState.recoveryActive, true);
    assert.deepEqual(plain(rec.progressState.recoverySlots), [25, 26, 27]);
    assert.equal(s.records.get('owner:videos'), rec);
});

test('automatic handoff replay cannot shrink queued pairs or roll back terminal state', () => {
    const s = setup();
    s.records.set('owner:videos', { taskId: 'child', streaming: true });
    const latest = { task_id: 'child', status: 'started', target_slots: [5, 9], ready_slots: [5, 9], queued_slots: [5, 9], pending_slots: [] };
    s.context.handleAutoVideoHandoff(s.owner, latest);
    s.context.handleAutoVideoHandoff(s.owner, { ...latest, ready_slots: [5], queued_slots: [5], pending_slots: [9] });
    assert.deepEqual(plain(s.owner.frameRun.auto_video.queued_slots), [5, 9]);
    assert.deepEqual(plain(s.owner.frameRun.auto_video.pending_slots), []);
    s.context.handleAutoVideoHandoff(s.owner, { ...latest, status: 'completed' });
    s.context.handleAutoVideoHandoff(s.owner, latest);
    assert.equal(s.owner.frameRun.auto_video.status, 'completed');
});

test('startup waits for library and current project before resuming background tasks', async () => {
    const calls = [];
    let callback, releaseLibrary, releaseCurrent;
    const context = {
        document: { addEventListener: (_event, fn) => { callback = fn; } },
        window: { addEventListener() {} }, currentIdea: null, console, setTimeout, clearTimeout,
        loadLibrary: () => { calls.push('library'); return new Promise(resolve => { releaseLibrary = resolve; }); },
        loadCurrentIdeaState: () => { calls.push('current'); return new Promise(resolve => { releaseCurrent = resolve; }); },
        resumeActiveBackgroundTasksIfExists: () => calls.push('resume-background'),
    };
    for (const name of ['loadConfig', 'initCanvas', 'checkApiStatus', 'setupEventListeners', 'setupDragAndDrop',
        'initDebugLimitControls', 'initAutoVideoControl', 'initCoverBurnControl', 'resumeActiveTaskIfExists',
        'startGlobalTasksBadgePolling', 'updateDrawerTopOffset', 'initLocalServiceLogs', 'syncAutoVideoToggleFromIdea']) {
        context[name] = () => calls.push(name);
    }
    vm.createContext(context);
    vm.runInContext(section(app, "document.addEventListener('DOMContentLoaded', async () =>", '// Function saveSelectionState'), context);
    const startup = callback();
    assert(!calls.includes('current'));
    assert(!calls.includes('resume-background'));
    releaseLibrary(); await new Promise(resolve => setImmediate(resolve));
    assert(calls.includes('current'));
    assert(!calls.includes('resume-background'));
    releaseCurrent(); await startup;
    assert(calls.indexOf('library') < calls.indexOf('current'));
    assert(calls.indexOf('current') < calls.indexOf('resume-background'));
});

test('current project recovery waits for the full server record and keeps the same owner object', async () => {
    let release;
    const rendered = [];
    const context = {
        console, currentIdea: null,
        localStorage: { getItem: key => key === 'spark_current_idea_id' ? 'owner' : null, removeItem() {} },
        fetch: () => new Promise(resolve => { release = () => resolve({ ok: true,
            json: async () => ({ id: 'owner', project_key: 'Owner_canonical', frameRun: { frames: [{ sequence: 1, file: 'ready.png' }] } }) }); }),
        document: { getElementById: () => ({ classList: { add() {}, remove() {} } }) },
        renderIdea: idea => rendered.push(idea), switchTab() {}, updateActiveGenerationBanner() {},
    };
    vm.createContext(context);
    // loadCurrentIdeaState is the last function in this module.
    vm.runInContext(configSource.slice(configSource.indexOf('async function loadCurrentIdeaState()')), context);
    const restoration = context.loadCurrentIdeaState();
    assert.equal(context.currentIdea, null);
    release(); await restoration;
    assert.equal(rendered.length, 1);
    assert.equal(rendered[0], context.currentIdea);
    assert.equal(context.currentIdea.frameRun.frames[0].file, 'ready.png');
});
