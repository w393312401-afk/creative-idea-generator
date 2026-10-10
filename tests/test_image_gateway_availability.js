const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() { this.children = []; this.value = ''; this.dataset = {}; this.listeners = {}; this.textContent = ''; }
    set innerHTML(value) { this.children = []; }
    get options() { return this.children; }
    appendChild(child) { this.children.push(child); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
}

function setup(model, report) {
    const storage = new Map([['spark_config', JSON.stringify({ imageModel: model })]]);
    const ids = ['settings-api-image-model', 'frames-image-model', 't2i-model', 'i2i-model',
        't2i-prompt', 'i2i-prompt', 't2i-model-quality-hint', 'i2i-model-quality-hint'];
    const nodes = new Map(ids.map(id => [id, new Element()]));
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    for (const tabId of ['t2i', 'i2i']) {
        const select = nodes.get(`${tabId}-model`);
        const markup = html.match(new RegExp(`<select id="${tabId}-model"[^>]*>([\\s\\S]*?)</select>`))[1];
        for (const match of markup.matchAll(/<option value="([^"]+)"([^>]*)>([^<]+)<\/option>/g)) {
            const option = new Element();
            option.value = match[1]; option.textContent = match[3];
            select.appendChild(option);
            if (/\bselected\b/.test(match[2])) select.value = option.value;
        }
    }
    const calls = [], toasts = [];
    const context = { console, URLSearchParams, AbortController, setTimeout: () => 1, clearTimeout() {},
        localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
        document: { readyState: 'loading', addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
            getElementById: id => nodes.get(id) || null, createElement: () => new Element() },
        showToast: (...args) => toasts.push(args),
        fetch: async (url, options) => {
            calls.push({ url, options });
            return { ok: true, json: async () => url === '/api/mode' ? { image_gateway_models: report } : { task_id: 'mock-task' } };
        },
    };
    context.window = context;
    context.addEventListener = () => {};
    vm.createContext(context);
    for (const file of ['js/state.js', 'js/config.js', 'js/image_studio.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context, { filename: file });
    }
    const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
    vm.runInContext(app.slice(app.indexOf('async function initServerMode()'), app.indexOf("document.addEventListener('DOMContentLoaded', initServerMode)")), context);
    vm.runInContext('updateDesktopPermissionStatus = () => {}; updateCoverModelDisplay = () => {};', context);
    context.loadConfig();
    return { context, nodes, storage, calls, toasts };
}

(async () => {
    for (const original of ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2.5-flare-2026-09-08']) {
        const h = setup(original, { status: 'known', models: ['gpt-image-2.5'] });
        const savedConfig = h.storage.get('spark_config');
        await h.context.initServerMode();
        assert.equal(vm.runInContext('config.imageModel', h.context), original, 'unavailable metadata preserves the selected model');
        assert.equal(h.storage.get('spark_config'), savedConfig, 'availability never overwrites the saved preference');
        assert.ok(h.toasts.some(([text]) => text.includes(original) && /当前网关不可用.*请改选 gpt-image-2\.5.*已保留当前选择/.test(text)), 'warning explains unavailability and the retained selection');
        for (const id of ['settings-api-image-model', 'frames-image-model', 't2i-model', 'i2i-model']) {
            const select = h.nodes.get(id);
            for (const option of select.options) {
                const unsupported = /^gpt-image-2\.5-/.test(option.value);
                assert.equal(option.disabled, unsupported, `${id} ${option.value}`);
                if (unsupported) assert.match(option.textContent, /当前网关不可用/);
            }
            assert.equal(select.value, ['t2i-model', 'i2i-model'].includes(id) ? 'nano-banana-2' : original);
        }
        h.context.syncImageGatewayModelAvailability({ status: 'known', models: ['gpt-image-2.5'] });
        assert.equal(h.toasts.length, 1, 'repeated metadata warns only once for the selected model');
        h.context.syncImageGatewayModelAvailability({ status: 'known', models: [
            'gpt-image-2.5', 'gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', original,
        ] });
        for (const id of ['settings-api-image-model', 'frames-image-model', 't2i-model', 'i2i-model']) {
            const select = h.nodes.get(id);
            assert.ok(select.options.every(option => !option.disabled && !option.textContent.includes('当前网关不可用')), `${id} re-enables supported models`);
            assert.equal(select.value, ['t2i-model', 'i2i-model'].includes(id) ? 'nano-banana-2' : original, 'restored support keeps the existing selection');
        }
        assert.equal(vm.runInContext('config.imageModel', h.context), original);
        assert.equal(h.storage.get('spark_config'), savedConfig, 'restored support keeps the persisted preference');
        h.context.loadConfig();
        assert.equal(vm.runInContext('config.imageModel', h.context), original, 'saved preference survives config reload');
        h.context.syncImageGatewayModelAvailability({ status: 'known', models: ['gpt-image-2.5'] });
        assert.equal(h.toasts.length, 1, 'support changes do not repeat the warning within the page session');
        h.context.syncImageGatewayModelAvailability({ status: 'unknown', models: [], message: 'temporarily unavailable' });
        for (const id of ['settings-api-image-model', 'frames-image-model', 't2i-model', 'i2i-model']) {
            assert.ok(h.nodes.get(id).options.every(option => !option.disabled && !option.textContent.includes('当前网关不可用')), 'unknown status does not mis-disable models');
        }
    }

    const unknown = setup('gpt-image-2.5-sunburst', { status: 'unknown', models: [] });
    await unknown.context.initServerMode();
    assert.equal(vm.runInContext('config.imageModel', unknown.context), 'gpt-image-2.5-sunburst');
    assert.equal(unknown.toasts.length, 0);
    assert.ok(unknown.nodes.get('settings-api-image-model').options.every(option => !option.disabled));

    const dated = 'gpt-image-2.5-sunburst-2026-09-08';
    const exact = setup(dated, { status: 'known', models: ['gpt-image-2.5', dated] });
    await exact.context.initServerMode();
    assert.equal(vm.runInContext('config.imageModel', exact.context), dated, 'exact supported dated model remains selected');
    assert.equal(exact.context.isApiImageModelAvailable('gpt-image-2.5-sunburst'), false);
    assert.equal(exact.context.isApiImageModelAvailable(dated), true);
    assert.equal(exact.context.isApiImageModelAvailable('nano-banana-2'), true);

    const empty = setup('gpt-image-2.5-sunburst', { status: 'known', models: [] });
    await empty.context.initServerMode();
    assert.equal(vm.runInContext('config.imageModel', empty.context), 'gpt-image-2.5-sunburst', 'missing base model never causes an unsupported automatic fallback');
    assert.equal(JSON.parse(empty.storage.get('spark_config')).imageModel, 'gpt-image-2.5-sunburst');
    assert.equal(empty.toasts.length, 1);
    assert.match(empty.toasts[0][0], /当前网关不可用.*请改选当前可用的图片模型.*已保留当前选择/);
    console.log('Image gateway availability: retained preferences, one-time warnings, restored support, four selectors and exact dated IDs passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
