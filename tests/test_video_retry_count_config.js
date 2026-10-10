const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../js/config.js'), 'utf8');
const fields = {
    'settings-video-retry-count': { value: '5' },
    'settings-video-continuous-generation': { value: 'true' },
    'settings-saved-flag': { hidden: true, dataset: {} },
};
const config = { videoRetryCount: 5 };
const timers = new Map(), requests = [];
let saved, timerId = 0;
const context = vm.createContext({
    config, DEFAULT_CONFIG: { videoRetryCount: 5 },
    document: { getElementById: id => fields[id] || null },
    localStorage: { setItem: (_key, value) => { saved = JSON.parse(value); } },
    updateFxVideoDurationVisibility() {}, syncIdeationSkillProfilePicker() {},
    updateCoverModelDisplay() {}, syncFramesImageModelPicker() {}, syncSettingsApiImageModelPicker() {},
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
        requests.push({ url, patch: JSON.parse(options.body).patch });
        return { ok: true, json: async () => ({ status: 'ok' }) };
    },
});
vm.runInContext(source.slice(source.indexOf('// /api/mode supplies only'), source.indexOf('/** auto 模式')), context);
vm.runInContext(source.slice(source.indexOf('function applySettingsFormToConfig()'), source.indexOf('function resetConfig()')), context);

(async () => {
    for (const value of [false, true]) {
        context.applyServerVideoConfig({ videoContinuousGeneration: value });
        assert.equal(config.videoContinuousGeneration, value);
        assert.equal(fields['settings-video-continuous-generation'].value, String(value));
        assert.equal(saved.videoContinuousGeneration, value);
    }
    context.applyServerVideoConfig({ videoContinuousGeneration: 'false' });
    assert.equal(config.videoContinuousGeneration, true, '服务端布尔配置不接受字符串覆盖');
    for (const value of [0, 5, 10]) {
        context.applyServerVideoConfig({ videoRetryCount: value });
        assert.equal(config.videoRetryCount, value);
        assert.equal(fields['settings-video-retry-count'].value, String(value));
        assert.equal(saved.videoRetryCount, value);
    }
    for (const value of [-1, 11, 1.5, '', null, 'invalid']) {
        context.applyServerVideoConfig({ videoRetryCount: value });
        assert.equal(config.videoRetryCount, 10, 'Invalid server readback must not replace the valid count');
    }
    fields['settings-video-retry-count'].value = '0';
    context.autoSaveConfig();
    assert.equal(config.videoRetryCount, 0, 'Zero means no retries and must not fall back to five');
    assert.equal(saved.videoRetryCount, 0);
    for (const timer of [...timers.values()]) if (timer.delay === 300) timer.fn();
    for (let i = 0; i < 6; i++) await Promise.resolve();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/google-fx/config');
    assert.equal(requests[0].patch.videoRetryCount, 0);
    assert.equal(requests[0].patch.videoContinuousGeneration, true);
    assert.equal(fields['settings-saved-flag'].dataset.state, 'saved');
    fields['settings-video-retry-count'].value = 'bad';
    context.applySettingsFormToConfig();
    assert.equal(config.videoRetryCount, 5);
    const state = fs.readFileSync(path.join(__dirname, '../js/state.js'), 'utf8');
    assert.match(state, /videoRetryCount:\s*5/);
    assert.match(state, /videoContinuousGeneration:\s*true/);
    fields['settings-video-continuous-generation'].value = 'false';
    context.applySettingsFormToConfig();
    assert.equal(config.videoContinuousGeneration, false, '允许显式关闭持续接续');
    console.log('Video retry count settings: defaults, zero, bounds and server persistence passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
