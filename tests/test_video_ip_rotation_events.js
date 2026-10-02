const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ProgressModel = require('../js/progress_model.js');

const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const fullStream = app.slice(app.indexOf('async function streamVideosProgress('),
    app.indexOf('async function streamCoverProgress('));
const retryStream = api.slice(api.indexOf('async function retrySingleVideo('),
    api.indexOf('// setVideoUploadButtonsBusy'));
const events = [
    ['video_warning', { message: '平台异常活动拦截，最多等待 30 秒收取在途视频' }],
    ['ip_rotating', { retry: 2, message: '平台异常活动拦截，正在第 2 次换 IP，验证出口…' }],
    ['ip_rotated', { retry: 2, old_ip: '192.0.2.1', new_ip: '192.0.2.2',
        message: '第 2 次换 IP，已验证：192.0.2.1 → 192.0.2.2，继续未完成视频' }],
    ['ip_rotation_failed', { retry: 3, message: '可用出口已用尽，已停止重试' }],
];

async function checkStream(kind, viewing) {
    const owner = { id: 'owner', title: 'project', prompt_block: 'saved' };
    const elements = {};
    let record;
    const observed = [], toasts = [], progress = [];
    const context = {
        console, AbortController, currentIdea: owner, config: {}, ProgressModel,
        window: { ProgressModel },
        document: { getElementById: id => elements[id] ||= { style: {}, textContent: '' } },
        getIdeaTaskRecord: () => record,
        isIdeaTaskActive: () => false,
        beginIdeaTask: () => record = {},
        beginVideoOperation: () => record = { controller: new AbortController() },
        submitVideoOperation: async () => ({ task_id: 'task-1' }),
        isViewingIdea: () => viewing,
        slotRenderTarget: () => ({}),
        saveActiveBackgroundTasksToLocalStorage() {}, refreshSlotGridBusy() {},
        renderSlotPending() {},
        confirmSequenceReviewOverride: async () => ({ proceed: true, override: false }),
        getIdeaSaveTitle: () => 'project',
        setProgressBar: (_kind, data) => progress.push(data),
        showToast: (...args) => toasts.push(args),
        watchTaskUntilTerminal: async (_id, { onEvent }) => {
            for (const [type, data] of events) {
                onEvent(type, data);
                observed.push({ type, meta: record.meta, text: elements['videos-meta'].textContent,
                    last: record.lastIpRotation });
            }
            return { status: 'disconnected', error: 'test disconnect' };
        },
    };
    vm.createContext(context);
    vm.runInContext(kind === 'full' ? fullStream : retryStream, context);
    if (kind === 'full') await context.streamVideosProgress('task-1', owner);
    else await context.retryVideoSlots([2, 3], { ownerIdea: owner });

    assert.strictEqual(observed.length, 4);
    observed.forEach((item, index) => {
        assert.strictEqual(item.meta, events[index][1].message, 'background task retains recovery status');
        if (item.type !== 'video_warning') assert.strictEqual(item.last.stage, item.type);
        if (viewing) assert.strictEqual(item.text, item.meta);
        else assert.strictEqual(item.text, '', 'background task must not overwrite the visible project');
    });
    assert.strictEqual(toasts.filter(([message]) => message === events[2][1].message).length, viewing ? 1 : 0);
    assert.strictEqual(toasts.filter(([message, level]) => message === events[3][1].message && level === 'error').length,
        viewing ? 1 : 0);
    if (kind === 'full' && viewing) assert.strictEqual(progress.at(-1).status, 'failed');
}

(async () => {
    let before = ProgressModel.progressFromEvents([
        ['start', { total: 36 }], ['video_done', { index: 1, total: 36 }],
    ], 'videos', 'running');
    for (const [stage, details] of events) {
        const next = ProgressModel.normalizeGenerationProgress(stage, details, 'videos', before.state);
        assert.strictEqual(next.percent, before.percent, 'changing IP must not complete or reset a clip');
        assert.deepStrictEqual(next.state.slotStatus, before.state.slotStatus);
        assert.strictEqual(next.slot, null);
        assert.strictEqual(next.label, details.message);
        assert.strictEqual(next.status, stage === 'ip_rotation_failed' ? 'failed'
            : stage === 'video_warning' ? 'active' : 'retrying');
        before = next;
    }
    for (const kind of ['full', 'retry']) for (const viewing of [true, false]) await checkStream(kind, viewing);
    console.log('video IP rotation event tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
