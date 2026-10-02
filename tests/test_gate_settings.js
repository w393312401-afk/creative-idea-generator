const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
    constructor(tag = 'div') {
        this.tagName = tag;
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.classList = {
            toggle: (name, enabled) => {
                const classes = new Set(this.className.split(/\s+/).filter(Boolean));
                if (enabled) classes.add(name); else classes.delete(name);
                this.className = [...classes].join(' ');
            },
        };
    }
    set textContent(text) { this.text = text; this.children = []; }
    get textContent() { return this.text || ''; }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    dispatch(name) { for (const fn of this.listeners[name] || []) fn({ target: this }); }
    querySelectorAll() { return this.children; }
}

const roots = new Map();
for (const id of [
    'gate-settings-list', 'gate-setting-reviewsDisabled', 'gate-reviews-master',
    'gate-reviews-status', 'gate-settings-search', 'gate-settings-search-clear',
]) {
    const el = new Element();
    el.id = id;
    roots.set(id, el);
}
const descend = (element, predicate) => {
    if (predicate(element)) return element;
    for (const child of element.children) {
        const found = descend(child, predicate);
        if (found) return found;
    }
    return null;
};
const byId = id => roots.get(id) || descend(roots.get('gate-settings-list'), el => el.id === id);
let saved;
let saves = 0;
const ctx = {
    window: {},
    config: { strictFrameStateContract: false, frameContinuityMode: 'strict', otherSetting: 'keep' },
    document: { getElementById: byId, createElement: tag => new Element(tag) },
    autoSaveConfig: () => { saved = JSON.parse(JSON.stringify(ctx.config)); saves++; },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/gate_settings.js'), 'utf8'), ctx);
const specs = [
    { key: 'reviewsDisabled', type: 'bool', default: false, section: 'env', label: '一键关闭所有审查' },
    { key: 'strictFrameStateContract', type: 'bool', default: true, section: 'prompt', label: '状态契约' },
    { key: 'frameContinuityMode', type: 'enum', default: 'warn', options: ['off', 'warn', 'strict'], section: 'frame' },
];
ctx.window.GATE_SETTINGS_SPEC = specs;
ctx.renderGateSettingsPanel();
const master = byId('gate-setting-reviewsDisabled');
assert.strictEqual(master.checked, false);
assert.strictEqual(master.disabled, false);
assert.strictEqual(byId('gate-setting-strictFrameStateContract').value, 'false');
assert.strictEqual(byId('gate-setting-frameContinuityMode').value, 'strict');
assert.strictEqual(descend(byId('gate-settings-list'), el => el.dataset.gateKey === 'reviewsDisabled'), null,
    'Master switch must not also appear in the filtered list');

master.checked = true;
master.dispatch('change');
assert.strictEqual(saved.reviewsDisabled, true, 'Master switch must auto-save immediately');
assert.strictEqual(saved.strictFrameStateContract, false);
assert.strictEqual(saved.frameContinuityMode, 'strict', 'Turning off reviews must retain individual preferences');
assert.strictEqual(byId('gate-setting-frameContinuityMode').disabled, true);
assert.strictEqual(byId('gate-setting-frameContinuityMode').value, 'strict');
assert.match(byId('gate-reviews-status').textContent, /全部审查已关闭/);

const search = byId('gate-settings-search');
search.value = '找不到的审查';
search.dispatch('input');
assert.strictEqual(master.checked, true, 'Filtering the list must leave the master state unchanged');
assert.strictEqual(master.disabled, false, 'Master remains available when no individual gates match');
master.checked = false;
master.dispatch('change');
search.value = '';
search.dispatch('input');
assert.strictEqual(byId('gate-setting-frameContinuityMode').disabled, false);
assert.strictEqual(byId('gate-setting-frameContinuityMode').value, 'strict');
assert.strictEqual(saved.reviewsDisabled, false);
assert.strictEqual(saves, 2, 'Repeated rendering must not duplicate the change listener');

// A server-wide master default is reflected locally, and an explicit browser false wins.
delete ctx.config.reviewsDisabled;
specs[0].server_value = true;
ctx.renderGateSettingsPanel();
assert.strictEqual(master.checked, true);
ctx.config.reviewsDisabled = false;
ctx.renderGateSettingsPanel();
assert.strictEqual(master.checked, false);
ctx.resetGateSettings();
assert.strictEqual(master.checked, true, 'Reset restores the server master default');
assert.strictEqual(ctx.config.otherSetting, 'keep');
assert.strictEqual(byId('gate-setting-frameContinuityMode').value, 'warn');

// Requests can use the saved master setting before the server specs finish loading.
ctx.config.reviewsDisabled = true;
ctx.window.GATE_SETTINGS_SPEC = null;
assert.strictEqual(ctx.gateReviewsDisabled(), true);
ctx.renderGateSettingsPanel();
assert.strictEqual(master.disabled, true, 'Do not offer an unsupported switch on an old backend');
assert.match(byId('gate-reviews-status').textContent, /暂未读取到/);

// The same persisted switch must govern manual review and video continuation.
const apiSource = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
for (const name of ['runSequenceReview', 'confirmSequenceReviewOverride']) {
    const start = apiSource.indexOf(`async function ${name}(`);
    const end = apiSource.indexOf('\n}', start) + 2;
    vm.runInContext(apiSource.slice(start, end), ctx);
}
let confirmations = 0;
let requests = 0;
let lastRequest;
const notices = [];
Object.assign(ctx, {
    currentIdea: { id: 'review-test', title: 'Review test', prompt_block: 'test' },
    AbortController,
    showToast: message => notices.push(message),
    customConfirm: async () => { confirmations++; return false; },
    isIdeaTaskActive: () => false,
    beginIdeaTask: () => {},
    endIdeaTask: () => {},
    setFrameGridButtonsBusy: () => {},
    refreshSlotGridBusy: () => {},
    getIdeaSaveTitle: idea => idea.title,
    getIdeaTaskRecord: () => null,
    isViewingIdea: () => false,
    reloadManifestIntoIdea: async () => {},
    watchTaskUntilTerminal: async () => ({ status: 'completed' }),
    fetch: async (url, options) => {
        requests++;
        lastRequest = { url, body: JSON.parse(options.body) };
        return { ok: true, json: async () => ({ task_id: 'review-task' }) };
    },
});
for (const id of ['frames-progress', 'frames-meta']) roots.set(id, new Element());
const flaggedIdea = { frameRun: { frames: [{ sequence: 1, quality_gate: 'sequence_review_flagged' }] } };
(async () => {
    const skipped = await ctx.confirmSequenceReviewOverride(flaggedIdea);
    assert.strictEqual(skipped.proceed, true);
    assert.strictEqual(skipped.override, true);
    assert.strictEqual(confirmations, 0, 'Disabled reviews must not open a warning confirmation');
    await ctx.runSequenceReview('full');
    assert.strictEqual(requests, 0, 'Disabled manual review must not call the review API');
    assert.match(notices.pop(), /所有审查已关闭/);

    ctx.config.reviewsDisabled = false;
    const normal = await ctx.confirmSequenceReviewOverride(flaggedIdea);
    assert.strictEqual(normal.proceed, false, 'Restoring reviews must respect a declined warning');
    assert.strictEqual(normal.override, false);
    assert.strictEqual(confirmations, 1);
    await ctx.runSequenceReview('full');
    assert.strictEqual(requests, 1, 'Restoring reviews must permit manual review again');
    assert.strictEqual(lastRequest.url, '/api/sequence_review');
    assert.strictEqual(lastRequest.body.scope, 'full');
    assert.strictEqual(lastRequest.body.config.reviewsDisabled, false);

    // Manual merge also needs the browser preference, rather than server defaults.
    const appSource = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
    const mergeStart = appSource.indexOf('async function mergeVideos(');
    vm.runInContext(appSource.slice(mergeStart, appSource.indexOf('\n}', mergeStart) + 2), ctx);
    for (const id of ['merge-videos-btn', 'videos-meta']) roots.set(id, new Element());
    Object.assign(ctx, {
        getMergeSpeed: () => 1, mergeSpeedLabel: () => '无加速', getCoverBurn: () => 'off',
        updatePipelineBar: () => {}, saveCurrentIdeaState: () => {},
        renderVideosForIdea: () => {}, savedIdeas: [],
        fetch: async (url, options) => {
            lastRequest = { url, body: JSON.parse(options.body) };
            return { ok: true, json: async () => ({ status: 'ok', merged_video: { speed: 1 } }) };
        },
    });
    for (const disabled of [true, false]) {
        ctx.config.reviewsDisabled = disabled;
        await ctx.mergeVideos();
        assert.strictEqual(lastRequest.url, '/api/merge_videos');
        assert.strictEqual(lastRequest.body.config.reviewsDisabled, disabled);
    }
    console.log('Gate settings master switch and review integration tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
