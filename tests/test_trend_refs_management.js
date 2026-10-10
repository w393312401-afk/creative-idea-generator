const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() {
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.disabled = false;
        this.value = '';
        this._text = '';
        const classes = new Set();
        this.classList = {
            add: value => classes.add(value),
            remove: value => classes.delete(value),
            contains: value => classes.has(value),
            toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value),
        };
    }
    get textContent() { return this._text; }
    set textContent(value) { this._text = value; this.children = []; }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(event, callback) { this.listeners[event] = callback; }
    querySelectorAll() { return []; }
}

function createEnvironment({ failFirstLoad = false } = {}) {
    const ids = [
        'trend-refs-manage-open-btn', 'trend-refs-manage-modal',
        'trend-refs-manage-close-btn', 'trend-refs-manage-list',
        'trend-refs-manage-scope', 'trend-refs-manage-stats',
        'trend-refs-manage-filter', 'trend-refs-manage-sort',
        'trend-refs-manage-select-all-btn', 'trend-refs-manage-bulk-count',
        'trend-refs-manage-bulk-delete-btn', 'trend-refs-search-btn',
    ];
    const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
    elements['trend-refs-search-btn'].textContent = '搜一批新参考';
    const requests = [];
    const messages = [];
    const storage = new Map();
    let bootstrap;
    let failLoad = failFirstLoad;
    const context = vm.createContext({
        console: { error() {} }, setTimeout, clearTimeout,
        document: {
            getElementById: id => elements[id] || null,
            createElement: () => new Element(),
            addEventListener: (event, callback) => { if (event === 'DOMContentLoaded') bootstrap = callback; },
        },
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
        },
        config: { ideationSearchQuery: '修复参考' },
        showToast: (message, kind) => messages.push({ message, kind }),
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (url === '/api/trend-refs' && failLoad) {
                failLoad = false;
                throw new Error('offline');
            }
            const existing = { id: 'reference_1', label: '木屋修复', text: '可见工序和成品展示', created_at: '2026-09-01' };
            if (url === '/api/trend-refs') return { json: async () => ({ refs: [existing], cap: 60, archived_count: 0 }) };
            if (url === '/api/trend-refs/archive') return { json: async () => ({ refs: [] }) };
            if (url === '/api/trend-refs/search') return { json: async () => ({
                status: 'ok', refs: [existing, { id: 'reference_2', label: '家具修复', text: '工艺过程', created_at: '2026-09-02' }],
                added: ['reference_2'], cap: 60, archived_count: 0,
            }) };
            throw new Error(`Unexpected request: ${url}`);
        },
    });
    context.window = context;
    const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'trend_refs.js'), 'utf8');
    vm.runInContext(source, context);
    bootstrap();
    return { context, elements, requests, messages };
}

(async () => {
    const env = createEnvironment();
    assert.deepEqual(env.requests, [], '参考库应在打开时加载，不占用项目启动请求');
    await env.elements['trend-refs-manage-open-btn'].listeners.click();
    assert.equal(env.elements['trend-refs-manage-modal'].classList.contains('active'), true);
    assert.deepEqual(env.requests.map(r => r.url), ['/api/trend-refs', '/api/trend-refs/archive']);
    assert.equal(env.elements['trend-refs-manage-list'].children.length, 1, '仅有管理弹窗时也必须显示主库内容');

    await env.elements['trend-refs-search-btn'].listeners.click();
    assert.equal(env.elements['trend-refs-manage-list'].children.length, 2, '搜索完成后管理列表立即显示新增参考');
    assert.equal(env.elements['trend-refs-search-btn'].disabled, false);
    assert.equal(env.elements['trend-refs-search-btn'].textContent, '搜一批新参考');
    assert.equal(JSON.parse(env.requests.at(-1).options.body).config.ideationSearchQuery, '修复参考');
    assert.match(env.messages.at(-1).message, /新增 1 条/);

    const offline = createEnvironment({ failFirstLoad: true });
    await offline.elements['trend-refs-manage-open-btn'].listeners.click();
    assert.match(offline.elements['trend-refs-manage-list'].children[0].textContent, /载入失败/,
        '加载失败必须保留错误信息，不能被空列表覆盖');
    await offline.elements['trend-refs-manage-open-btn'].listeners.click();
    assert.equal(offline.elements['trend-refs-manage-list'].children.length, 1, '重新打开应允许重试加载');
    assert.match(offline.elements['trend-refs-manage-stats'].textContent, /主库 1/);
    console.log('trend refs management tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
