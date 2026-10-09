const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../js/config.js'), 'utf8');

function setup() {
    const fields = {
        'settings-video-provider': { value: 'flow2api' },
        'settings-fx-video-model': { value: 'Omni Flash', options: [] },
        'settings-fx-video-ref-mode': { value: 'VIDEO_FRAMES', options: [] },
        'settings-image-ratio': { value: '9:16' },
        'settings-flow2api-video-concurrency': { value: '1' },
        'flow2api-video-concurrency-group': { style: { display: 'none' } },
        'video-provider-status': { textContent: '', dataset: {} },
        'settings-saved-flag': { hidden: true, dataset: {} },
    };
    const config = { videoProvider: 'flow2api', flow2apiVideoConcurrency: 1, fxMaxConcurrent: 1 };
    const timers = new Map();
    const requests = [];
    let saved = null;
    let nextTimer = 0;
    const context = vm.createContext({
        config, DEFAULT_CONFIG: { flow2apiVideoConcurrency: 3 },
        document: { getElementById: id => fields[id] || null },
        localStorage: { setItem(key, value) { saved = JSON.parse(value); } },
        updateFxVideoDurationVisibility() {}, syncIdeationSkillProfilePicker() {},
        updateCoverModelDisplay() {}, syncFramesImageModelPicker() {}, syncSettingsApiImageModelPicker() {},
        setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch: async (url, options) => {
            requests.push({ url, patch: JSON.parse(options.body).patch });
            return { ok: true, json: async () => ({ status: 'ok' }) };
        },
    });
    vm.runInContext(source.slice(source.indexOf('// /api/mode supplies only'), source.indexOf('/** auto 模式')), context);
    vm.runInContext(source.slice(source.indexOf('function applySettingsFormToConfig()'), source.indexOf('function resetConfig()')), context);
    return { context, fields, config, requests, timers, saved: () => saved };
}

test('server concurrency is copied as a number and shown only for Flow2API', () => {
    const h = setup();
    h.context.applyServerVideoConfig({ flow2apiVideoConcurrency: 7 });
    assert.equal(h.config.flow2apiVideoConcurrency, 7);
    assert.equal(h.fields['settings-flow2api-video-concurrency'].value, '7');
    assert.equal(h.saved().flow2apiVideoConcurrency, 7);
    assert.equal(h.config.fxMaxConcurrent, 1);
    h.context.updateVideoProviderStatus();
    assert.equal(h.fields['flow2api-video-concurrency-group'].style.display, '');
    h.fields['settings-video-provider'].value = 'google_fx';
    h.context.updateVideoProviderStatus();
    assert.equal(h.fields['flow2api-video-concurrency-group'].style.display, 'none');
    for (const value of [0, 11, 2.5, 'invalid']) h.context.applyServerVideoConfig({ flow2apiVideoConcurrency: value });
    assert.equal(h.config.flow2apiVideoConcurrency, 7, 'invalid readback must not replace a saved value');
});

test('form edits are saved to the server as integer concurrency and preserve AdsPower settings', async () => {
    const h = setup();
    h.fields['settings-flow2api-video-concurrency'].value = '5';
    assert.equal(h.context.autoSaveConfig(), true);
    assert.equal(h.config.flow2apiVideoConcurrency, 5);
    assert.equal(h.saved().flow2apiVideoConcurrency, 5);
    for (const timer of [...h.timers.values()]) if (timer.delay === 300) timer.fn();
    for (let i = 0; i < 6; i++) await Promise.resolve();
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/google-fx/config');
    assert.equal(h.requests[0].patch.flow2apiVideoConcurrency, 5);
    assert.equal(h.requests[0].patch.fxMaxConcurrent, undefined);
    assert.equal(h.fields['settings-saved-flag'].dataset.state, 'saved');
});

test('console saves update the main browser concurrency setting', () => {
    const consoleSource = fs.readFileSync(path.join(__dirname, '../js/google_fx_console.js'), 'utf8');
    const section = consoleSource.slice(consoleSource.indexOf('  const _FX_MODEL_SYNC_KEYS'), consoleSource.indexOf('  async function saveConfig()'));
    let saved = { videoProvider: 'flow2api', flow2apiVideoConcurrency: 1, fxMaxConcurrent: 2 };
    const context = vm.createContext({ localStorage: {
        getItem() { return JSON.stringify(saved); },
        setItem(key, value) { saved = JSON.parse(value); },
    } });
    vm.runInContext(section, context);
    context.syncFxModelToMainConfig({ flow2apiVideoConcurrency: 8 });
    assert.equal(saved.flow2apiVideoConcurrency, 8);
    assert.equal(saved.fxMaxConcurrent, 2);
});
