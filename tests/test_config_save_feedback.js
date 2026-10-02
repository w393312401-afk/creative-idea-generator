const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../js/config.js'), 'utf8');
const section = source.slice(source.indexOf('/* 配置改动即时保存'), source.indexOf('function resetConfig()'));

function setup() {
    const flag = { hidden: true, dataset: {}, textContent: '' };
    const retry = { hidden: true };
    const timers = new Map();
    const requests = [];
    let nextId = 0;
    const ctx = vm.createContext({
        config: { videoModel: 'first' },
        document: { getElementById: id => id === 'settings-saved-flag' ? flag : retry },
        localStorage: { setItem() {} },
        applySettingsFormToConfig() {}, updateCoverModelDisplay() {}, syncFramesImageModelPicker() {},
        syncSettingsApiImageModelPicker() {}, syncIdeationSkillProfilePicker() {},
        setTimeout(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch(url, options) { return new Promise((resolve, reject) => requests.push({ patch: JSON.parse(options.body).patch, resolve, reject })); },
    });
    vm.runInContext(section, ctx);
    async function tick() {
        for (const [id, timer] of [...timers]) {
            if (timer.delay !== 300) continue;
            timers.delete(id);
            timer.fn();
        }
        await settle();
    }
    return { ctx, flag, retry, requests, tick };
}
async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
function success(request) { request.resolve({ ok: true, json: async () => ({ status: 'ok' }) }); }

(async () => {
    const a = setup();
    a.ctx.autoSaveConfig();
    assert.equal(a.flag.dataset.state, 'saving');
    assert.equal(a.requests.length, 0, 'rapid changes remain debounced');
    await a.tick();
    assert.equal(a.flag.dataset.state, 'saving', 'must wait for server acknowledgement');
    a.requests[0].resolve({ ok: false, status: 500, json: async () => ({ status: 'error' }) });
    await settle();
    assert.equal(a.flag.dataset.state, 'error');
    assert.equal(a.retry.hidden, false, 'failed sync offers retry and remains visible');
    a.ctx.autoSaveConfig();
    await a.tick();
    success(a.requests[1]);
    await settle();
    assert.equal(a.flag.dataset.state, 'saved');
    assert.equal(a.retry.hidden, true);

    const b = setup();
    b.ctx.autoSaveConfig();
    await b.tick();
    b.ctx.config.videoModel = 'latest';
    b.ctx.autoSaveConfig();
    await b.tick();
    assert.equal(b.requests.length, 1, 'writes must be serialized');
    success(b.requests[0]);
    await settle();
    assert.equal(b.flag.dataset.state, 'saving', 'old response cannot claim latest change saved');
    assert.equal(b.requests.length, 2);
    assert.equal(b.requests[1].patch.videoModel, 'latest');
    success(b.requests[1]);
    await settle();
    assert.equal(b.flag.dataset.state, 'saved');

    const c = setup();
    c.ctx.localStorage.setItem = () => { throw new Error('storage full'); };
    c.ctx.autoSaveConfig();
    await c.tick();
    assert.equal(c.flag.dataset.state, 'error');
    assert.equal(c.requests.length, 0, 'local storage failure must not claim saved or continue silently');
    console.log('config save feedback: 3 scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
