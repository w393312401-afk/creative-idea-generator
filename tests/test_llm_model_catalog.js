const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() { this.children = []; this.value = ''; }
    set innerHTML(value) { this.children = []; }
    appendChild(child) { this.children.push(child); }
}

function setup(storedConfig) {
    const stored = new Map();
    if (storedConfig) stored.set('spark_config', JSON.stringify(storedConfig));
    const picker = new Element();
    const context = vm.createContext({
        console, window: { addEventListener() {} }, URLSearchParams,
        localStorage: { getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value) },
        document: {
            readyState: 'loading', addEventListener() {}, querySelectorAll: () => [],
            getElementById: id => id === 'settings-llm-model' ? picker : null,
            createElement: () => new Element(),
        },
        escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    });
    for (const file of ['js/state.js', 'js/config.js', 'js/projects.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context, { filename: file });
    }
    vm.runInContext('updateDesktopPermissionStatus = function () {}; updateCoverModelDisplay = function () {};', context);
    return { context, picker, stored };
}

const currentGpt = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna'];
for (const legacy of ['claude-sonnet-4-6', 'Claude-Opus-4-6-Thinking', 'gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-4o', 'gpt-4.1', 'gpt-3.5-turbo']) {
    const { context, picker, stored } = setup({
        model: legacy, cheapModel: 'gpt-5.5', auxModel: 'claude-sonnet-4-6',
        reviewModel: 'gemini-3.7-flash-high', imageModel: 'gpt-image-2.5',
        videoModel: 'Omni Flash', imageAspectRatio: '16:9',
    });
    context.loadConfig();
    assert.equal(picker.value, 'gpt-6.1-sol', legacy);
    assert.deepEqual(picker.children.map(group => group.label), ['GPT', 'Gemini']);
    assert.deepEqual(picker.children[0].children.map(option => option.value), currentGpt);
    const persisted = JSON.parse(stored.get('spark_config'));
    assert.equal(persisted.model, 'gpt-6.1-sol');
    assert.equal(persisted.cheapModel, 'gpt-6.1-sol');
    assert.equal(persisted.auxModel, 'gpt-6.1-sol');
    assert.equal(persisted.reviewModel, 'gemini-3.8-flash-high');
    assert.equal(persisted.imageModel, 'gpt-image-2.5');
    assert.equal(persisted.videoModel, 'Omni Flash');
    assert.equal(persisted.imageAspectRatio, '16:9');

    const options = context.projectsModelOptions(legacy);
    assert.match(options, /value="gpt-6\.1-sol" selected/);
    assert.doesNotMatch(options, /claude|gpt-5\.|gemini-3\.7|历史模型/);
    context.loadConfig();
    assert.equal(picker.children[0].children.length, 4, 'repeat loading does not duplicate options');
}

for (const model of currentGpt) {
    const { context, picker } = setup({ model });
    context.loadConfig();
    assert.equal(picker.value, model, 'current GPT selection survives loading');
    assert.match(context.projectsModelOptions(model), new RegExp(`value="${model.replaceAll('.', '\\.')}" selected`));
}

for (const model of ['gemini-3-flash-agent', 'gemini-3.1-pro-high', 'gemini-3.5-flash-low', 'gemini-3.6-flash-medium', 'gemini-3.7-flash-tiered']) {
    const { context, picker, stored } = setup({ model });
    context.loadConfig();
    assert.equal(picker.value, 'gemini-3.8-flash-high');
    assert.equal(JSON.parse(stored.get('spark_config')).model, 'gemini-3.8-flash-high');
}

const { context, picker } = setup({ model: 'custom-model' });
context.loadConfig();
assert.equal(picker.value, 'custom-model');
assert.equal(picker.children.at(-1).value, 'custom-model');
assert.match(context.projectsModelOptions('custom-model'), /value="custom-model" selected/);
for (const model of ['gpt-image-2', 'gpt-image-2.5', 'gemini-3.1-flash-image', 'custom-model', 'gpt-50-custom', 'gpt-3.50']) {
    assert.equal(context.normalizeLlmModel(model), model);
}
picker.value = 'gpt-6-luna';
context.applySettingsFormToConfig();
assert.equal(vm.runInContext('config.model', context), 'gpt-6-luna', 'settings saves the chosen current GPT');
console.log('LLM catalog, stored configuration and project rerun migration passed');
