const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() { this.children = []; this.value = ''; this.dataset = {}; this.listeners = {}; }
    set innerHTML(value) { this.children = []; }
    appendChild(child) { this.children.push(child); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    dispatch(type) { this.listeners[type](); }
}

function setup(imageModel) {
    const stored = new Map();
    if (imageModel) stored.set('spark_config', JSON.stringify({ imageModel }));
    const nodes = new Map(['settings-api-image-model', 'frames-image-model'].map(id => [id, new Element()]));
    const context = vm.createContext({
        console, window: { addEventListener() {} }, URLSearchParams,
        setTimeout: () => 1, clearTimeout() {}, showToast() {},
        localStorage: { getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value) },
        document: {
            readyState: 'loading', addEventListener() {}, querySelectorAll: () => [],
            getElementById: id => nodes.get(id) || null, createElement: () => new Element(),
        },
    });
    for (const file of ['js/state.js', 'js/config.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context, { filename: file });
    }
    vm.runInContext('updateDesktopPermissionStatus = function () {}; updateCoverModelDisplay = function () {};', context);
    context.loadConfig();
    return { context, stored, nodes };
}

const variants = ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare'];
const { context: defaults, nodes: defaultNodes } = setup();
const catalog = Array.from(vm.runInContext('IMAGE_MODELS', defaults));
const values = catalog.map(model => model.value);
assert.equal(defaultNodes.get('settings-api-image-model').value, 'nano-banana-2');
assert.equal(defaultNodes.get('frames-image-model').value, 'nano-banana-2');
assert.match(catalog.find(model => model.value === variants[0]).label, /最强.*高精度/);
assert.match(catalog.find(model => model.value === variants[1]).label, /快速/);
for (const legacy of ['nano-banana-2', 'gpt-image-2.5']) assert.ok(values.includes(legacy));
assert.ok(!values.includes('gpt-image-2'), 'retired GPT Image 2 is not offered');

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
for (const id of ['t2i-model', 'i2i-model']) {
    const select = html.match(new RegExp(`<select id="${id}"[^>]*>([\\s\\S]*?)</select>`));
    assert.ok(select, `${id} exists`);
    const options = [...select[1].matchAll(/<option value="([^"]+)"([^>]*)>([^<]+)<\/option>/g)];
    assert.deepEqual(options.map(option => option[1]).sort(), [...values].sort(), `${id} matches the shared catalog`);
    assert.deepEqual(options.filter(option => /\bselected\b/.test(option[2])).map(option => option[1]), ['nano-banana-2']);
    assert.match(options.find(option => option[1] === variants[0])[3], /最强.*高精度/);
    assert.match(options.find(option => option[1] === variants[1])[3], /快速/);
}

for (const model of [...variants, 'gpt-image-2.5']) {
    const { context, stored, nodes } = setup(model);
    const settings = nodes.get('settings-api-image-model');
    const frames = nodes.get('frames-image-model');
    assert.equal(settings.value, model, 'stored model survives loading in settings');
    assert.equal(frames.value, model, 'stored model survives loading in frame sequence');
    assert.deepEqual(settings.children.map(option => option.value), values);
    assert.deepEqual(frames.children.map(option => option.value), values);

    for (const selected of variants) {
        settings.value = selected;
        assert.equal(context.autoSaveConfig(), true);
        assert.equal(JSON.parse(stored.get('spark_config')).imageModel, selected, 'settings saves the full model ID');
        assert.equal(frames.value, selected, 'settings updates the frame sequence selection');

        const fromFrames = variants.find(value => value !== selected);
        frames.value = fromFrames;
        frames.dispatch('change');
        assert.equal(settings.value, fromFrames, 'frame sequence updates settings');
        assert.equal(JSON.parse(stored.get('spark_config')).imageModel, fromFrames, 'frame sequence saves the full model ID');
    }
}
for (const retired of ['gpt-image-2', 'gpt-image-2-2026-09-08', 'GPT-IMAGE-2']) {
    const { context, stored, nodes } = setup(retired);
    for (const id of ['settings-api-image-model', 'frames-image-model']) {
        const select = nodes.get(id);
        assert.equal(select.value, 'gpt-image-2.5');
        assert.deepEqual(select.children.map(option => option.value), values, 'retired model cannot reappear as a custom option');
    }
    assert.equal(JSON.parse(stored.get('spark_config')).imageModel, 'gpt-image-2.5');
    context.loadConfig();
    assert.equal(nodes.get('settings-api-image-model').children.length, values.length);
}
for (const model of ['gpt-image-2.5', ...variants, 'gpt-image-2.5-sunburst-2026-09-08', 'gpt-image-2-custom', 'custom-image-model']) {
    assert.equal(defaults.normalizeApiImageModel(model), model, 'migration matches only the retired base model and dated snapshots');
}
console.log('Image model variants: four selectors, defaults, compatibility and stored selections passed');
