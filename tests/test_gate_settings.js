const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor(tag = 'div') {
        this.tagName = tag;
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.disabled = false;
        this.hidden = false;
    }
    set textContent(text) { this.text = text; this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
    appendChild(child) { this.children.push(child); return child; }
}
const roots = new Map([
    'gate-settings-list', 'gate-setting-reviewsDisabled', 'gate-reviews-status',
    'run-sequence-review-btn', 'run-full-sequence-review-btn',
].map(id => [id, new Element()]));
const stored = new Map();
const notices = [];
let requests = 0, confirmations = 0;
const ctx = vm.createContext({
    window: {}, console,
    config: { reviewsDisabled: false, frameContinuityMode: 'strict', videoAnchorVerify: true, otherSetting: 'keep' },
    DEFAULT_CONFIG: { reviewsDisabled: true, imageModel: 'keep-model', videoModel: 'Omni Flash' },
    document: { getElementById: id => roots.get(id) || null, createElement: tag => new Element(tag) },
    localStorage: { getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value) },
    showToast: message => notices.push(message),
    customConfirm: async () => { confirmations++; return false; },
    fetch: async () => { requests++; throw new Error('Retired review must never send a request'); },
    updateFxImageModelVisibility() {}, updateFxVideoDurationVisibility() {},
    updateDesktopPermissionStatus() {}, updateCoverModelDisplay() {},
    syncFramesImageModelPicker() {}, syncSettingsApiImageModelPicker() {},
    syncSettingsLlmModelPicker() {}, syncSettingsSkillProfilePicker() {}, updateVideoProviderStatus() {},
    normalizeGoogleFxImageModel: value => value, normalizeApiImageModel: value => value,
    migrateRetiredLlmModels: () => false,
    applySettingsFormToConfig() {}, syncIdeationSkillProfilePicker() {}, syncFxModelToServer() {},
    _settingsSaveRevision: 0,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/gate_settings.js'), 'utf8'), ctx);
const specs = [
    { key: 'reviewsDisabled', type: 'bool', default: false, server_value: false },
    { key: 'strictFrameStateContract', type: 'bool', default: true, server_value: true, label: '状态契约' },
    { key: 'frameContinuityMode', type: 'enum', default: 'strict', server_value: 'strict', label: '帧连续性' },
    { key: 'frameContinuityMaxRetries', type: 'int', default: 3, server_value: 3 },
];
assert.equal(ctx.gateReviewsDisabled(), true, 'Retirement applies before /api/mode responds');
ctx.renderGateSettingsPanel();
assert.equal(roots.get('gate-setting-reviewsDisabled').checked, true);
assert.equal(roots.get('gate-setting-reviewsDisabled').disabled, true);
assert.match(roots.get('gate-reviews-status').textContent, /永久退役/);
ctx.window.GATE_SETTINGS_SPEC = specs;
ctx.config.reviewsDisabled = false;
ctx.renderGateSettingsPanel();
assert.equal(ctx.config.reviewsDisabled, true, 'An old server response cannot restore reviews');
assert.equal(ctx.config.frameContinuityMode, undefined);
assert.equal(ctx.config.videoAnchorVerify, undefined);
assert.equal(ctx.config.otherSetting, 'keep');
assert.equal(ctx.gateSettingCurrentValue(specs[0]), true);
assert.equal(ctx.gateSettingCurrentValue(specs[1]), false);
assert.equal(ctx.gateSettingCurrentValue(specs[2]), 'off');
assert.equal(ctx.gateSettingCurrentValue(specs[3]), 0);
assert.match(roots.get('gate-settings-list').textContent, /状态契约：已退役/);
assert.equal(roots.get('gate-settings-list').dataset.status, 'retired');
assert.ok(roots.get('gate-settings-list').children.every(el => el.tagName !== 'input' && el.tagName !== 'select'));
for (const id of ['run-sequence-review-btn', 'run-full-sequence-review-btn']) {
    assert.equal(roots.get(id).hidden, true);
    assert.equal(roots.get(id).disabled, true);
}
ctx.config.reviewsDisabled = false;
ctx.config.strictFrameStateContract = true;
ctx.applyGateSettingFromControl(specs[1], { value: 'true' });
ctx.resetGateSettings();
assert.equal(ctx.config.reviewsDisabled, true);
assert.equal(ctx.config.strictFrameStateContract, undefined);

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
function loadFunction(source, name) {
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    assert.ok(start >= 0 && end > start, `Function ${name} exists`);
    vm.runInContext(source.slice(start, end), ctx);
}
const configSource = read('js/config.js');
loadFunction(configSource, 'loadConfig');
loadFunction(configSource, 'autoSaveConfig');
stored.set('spark_config', JSON.stringify({
    reviewsDisabled: false, strictFrameStateContract: true, qaGateLevel: 'standard',
    chainGuardMode: 'halt', videoProcessVlmReview: true, strictPromptPipelineV2: true,
    frameContinuityLocalEdit: 'strict', otherSetting: 'retained',
}));
ctx.window.GATE_SETTINGS_SPEC = null;
ctx.loadConfig();
let migrated = JSON.parse(stored.get('spark_config'));
assert.equal(ctx.config.reviewsDisabled, true);
assert.equal(migrated.reviewsDisabled, true, 'Loading old cache persists the retirement migration');
for (const key of ['strictFrameStateContract', 'qaGateLevel', 'chainGuardMode', 'videoProcessVlmReview', 'strictPromptPipelineV2', 'frameContinuityLocalEdit']) {
    assert.equal(migrated[key], undefined);
}
assert.equal(migrated.otherSetting, 'retained');
ctx.config.reviewsDisabled = false;
ctx.config.videoAnchorVerify = true;
ctx.autoSaveConfig();
migrated = JSON.parse(stored.get('spark_config'));
assert.equal(migrated.reviewsDisabled, true, 'Saving imported config cannot restore a retired rule');
assert.equal(migrated.videoAnchorVerify, undefined);

const apiSource = read('js/api_client.js');
for (const name of ['runSequenceReview', 'confirmSequenceReviewOverride']) {
    const start = apiSource.indexOf(`async function ${name}(`);
    vm.runInContext(apiSource.slice(start, apiSource.indexOf('\n}', start) + 2), ctx);
}
const html = read('index.html');
const panel = html.slice(html.indexOf('<section class="settings-section" data-section="gates">'), html.indexOf('<!-- ④ 联网参考 -->'));
assert.match(panel, /永久退役/);
assert.doesNotMatch(panel, /<(?:input|select|button)\b/, 'Retired settings offer no restoration controls');
for (const id of ['run-sequence-review-btn', 'run-full-sequence-review-btn']) {
    assert.match(html, new RegExp(`id="${id}" hidden disabled`));
}
(async () => {
    for (const disabled of [false, true, undefined]) {
        ctx.config.reviewsDisabled = disabled;
        const result = await ctx.confirmSequenceReviewOverride({
            frameRun: { frames: [{ quality_gate: 'sequence_review_flagged', stale_lineage: true }] },
        });
        assert.equal(result.proceed, true);
        assert.equal(result.override, true);
        await ctx.runSequenceReview('full');
    }
    assert.equal(requests, 0, 'No new manual review jobs are submitted');
    assert.equal(confirmations, 0, 'Historical quality flags no longer open confirmation dialogs');
    assert.ok(notices.every(message => /永久退役/.test(message)));
    console.log('Retired gates: old cache, old server, reset/import, manual review and generation continuation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
