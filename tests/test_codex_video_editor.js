const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'js/codex_video_editor.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const utils = fs.readFileSync(path.join(__dirname, '..', 'js/utils.js'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const accessHeaderCode = utils.slice(0, utils.indexOf('// 在本机文件管理器'));
const accessFetchCode = app.slice(app.indexOf('(function patchFetchForAccessCode()'), app.indexOf('// On load,'));
const ids = [...html.matchAll(/id="(codex-edit-[^"]+)"/g)].map(match => match[1]);
const settle = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const job = (id, source = '/outputs/a/final.mp4', status = 'running', extra = {}) => ({
    id, source, mode: 'trim', status, stage: status, message: '', logs: [],
    created_at: '2026-09-27T10:00:00Z', updated_at: '2026-09-27T10:00:00Z',
    ...(status === 'completed' ? { output: { url: `/outputs/a/${id}.mp4`, file: `outputs/a/${id}.mp4`, duration_seconds: 12, size_bytes: 1024 } } : {}), ...extra,
});
function harness(handler, storage = new Map(), browser = {}) {
    const elements = Object.fromEntries(ids.map(id => [id, {
        id, hidden: false, disabled: false, value: '', dataset: {}, textContent: '', innerHTML: '',
        open: false, listeners: {}, srcWrites: 0,
        addEventListener(type, callback) { this.listeners[type] = callback; },
        removeAttribute(name) { delete this['_' + name]; },
        pause() { this.paused = true; }, load() {},
        set src(value) { this._src = value; ++this.srcWrites; }, get src() { return this._src; },
    }]));
    const timers = new Map(), calls = [], revealed = [];
    let timerId = 0, uuid = 0;
    const context = { console, Date, Math, JSON, Headers, encodeURIComponent, decodeURIComponent,
        location: new URL(browser.origin || 'http://127.0.0.1:8085'),
        document: { getElementById: id => elements[id] || null },
        crypto: { randomUUID: () => 'request-' + ++uuid },
        sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
        clearTimeout: id => timers.delete(id),
        revealLocalFile: (...args) => revealed.push(args),
        fetch: async (url, options = {}) => { calls.push({ url, options }); return handler(url, options); },
    };
    vm.createContext(context);
    if (browser.accessCode !== undefined) {
        // Exercise the production access-code wrapper, including its header merge and 401 retry.
        context.window = context;
        context.ACCESS_CODE = browser.accessCode;
        context.prompt = () => browser.retryAccessCode || '';
        context.localStorage = { setItem() {} };
        assert.ok(accessHeaderCode.includes('function _setAccessHeader'));
        assert.ok(accessFetchCode.includes('patchFetchForAccessCode'));
        vm.runInContext(accessHeaderCode, context);
        vm.runInContext(accessFetchCode, context);
    }
    vm.runInContext(source, context);
    const element = name => elements['codex-edit-' + name];
    return { context, calls, timers, storage, revealed, element,
        sync: (source = '/outputs/a/final.mp4', extra = {}) => context.syncCodexVideoEditor(
            source ? { status: 'success', file: source, ...extra } : null, { title: '示例' }),
        event: (name, type = 'click', value) => { const target = element(name); if (value !== undefined) target.value = value; return target.listeners[type]({ target }); },
        tick: async () => { const pending = [...timers.values()]; timers.clear(); for (const { fn } of pending) fn(); await settle(); },
    };
}
function readBody(options) { return JSON.parse(options.body || '{}'); }
function readHeader(options, name) { return new Headers(options.headers || {}).get(name); }

function testMarkupLivesInsideFinishedResult() {
    const stack = [];
    const voids = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
    const clean = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
    for (const match of clean.matchAll(/<(\/?)([a-z][\w:-]*)\b([^>]*)>/gi)) {
        const [, closing, rawTag, attrs] = match, tag = rawTag.toLowerCase();
        if (closing) { const index = stack.map(item => item.tag).lastIndexOf(tag); if (index >= 0) stack.length = index; continue; }
        const id = /\bid="([^"]*)"/.exec(attrs)?.[1];
        if (id === 'codex-edit-root') assert.ok(stack.some(item => item.id === 'merged-video-container'), 'editor must live inside the finished-result block');
        // 精剪结果是最终产出，不能藏在默认折叠的设置面板里。
        if (id === 'codex-edit-output') assert.ok(!stack.some(item => item.id === 'codex-edit-panel'), 'edit output must stay outside the collapsed settings panel');
        if (!voids.has(tag) && !/\/$/.test(attrs.trim())) stack.push({ tag, id });
    }
    assert.equal(ids.length, new Set(ids).size, 'editor control ids must be unique');
    assert.match(html, /<script src="js\/codex_video_editor\.js[^>]*defer/);
    assert.match(html, /当前 Codex 登录与额度/);
    assert.match(html, /在运行项目的电脑上使用当前 Codex 登录与额度后台剪辑，公网访问也由这台电脑执行/);
    assert.match(html, /id="codex-edit-model"[^>]*>[^<]*<option value="gpt-6\.1-sol" selected>/);
    assert.match(html, /id="codex-edit-effort"[^>]*>[\s\S]*?<option value="high" selected>/);
}

async function testDefaultAndUnavailableAndCanonicalSource() {
    const h = harness((url) => response(url.endsWith('/capabilities') ? { available: false, message: '请登录 Codex' } : { jobs: [] }));
    h.sync(null); assert.equal(h.element('root').hidden, true);
    h.sync('/Users/test/project/outputs/中文/final.mp4'); await settle();
    assert.equal(h.element('root').hidden, false);
    assert.equal(h.element('mode').value, 'trim');
    assert.equal(h.element('model').value, 'gpt-6.1-sol');
    assert.equal(h.element('effort').value, 'high');
    assert.match(h.element('mode-hint').textContent, /保持当前成片速度/);
    assert.equal(h.element('start').disabled, true);
    assert.equal(h.element('capability').textContent, '请登录 Codex');
    assert.equal(decodeURIComponent(h.calls.find(call => call.url.includes('?source=')).url.split('=')[1]), '/outputs/%E4%B8%AD%E6%96%87/final.mp4');
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
}

async function testPublicHistoryAndMutationsKeepEditorAndAccessHeaders() {
    let latest = [job('previous', '/outputs/a/final.mp4', 'completed')], submissions = 0;
    const h = harness((url, options) => {
        assert.ok(url.startsWith('/api/codex-video-editor/'), 'public requests must use the current origin');
        assert.equal(readHeader(options, 'X-SPARK-Codex-Editor'), '1');
        assert.equal(readHeader(options, 'Origin'), null, 'the browser supplies Origin');
        const accessCode = readHeader(options, 'X-Access-Code');
        if (url.endsWith('/capabilities')) {
            assert.equal(accessCode, 'initial-code');
            return response({ available: true });
        }
        if (options.method !== 'POST') {
            assert.equal(accessCode, 'initial-code');
            return response({ jobs: latest });
        }
        assert.equal(readHeader(options, 'Content-Type'), 'application/json');
        if (url.endsWith('/jobs')) {
            if (++submissions === 1) {
                assert.equal(accessCode, 'initial-code');
                return response({ message: '访问码已更新' }, 401);
            }
            assert.equal(accessCode, 'updated-code');
            const body = readBody(options);
            latest = [job('new', body.source, 'running', { request_id: body.request_id }), ...latest];
            return response({ job: latest[0] });
        }
        assert.ok(url.endsWith('/cancel'));
        assert.equal(accessCode, 'updated-code');
        assert.equal(readBody(options).id, 'new');
        latest[0] = { ...latest[0], status: 'cancelled', stage: 'cancelled' };
        return response({ job: latest[0] });
    }, new Map(), { origin: 'https://ps.wushi-api.com', accessCode: 'initial-code', retryAccessCode: 'updated-code' });
    h.sync(); await settle();
    assert.equal(h.element('start').disabled, false, 'public capability and history checks allow editing');
    assert.equal(h.element('capability').textContent, '');
    assert.equal(h.element('player').src, '/outputs/a/previous.mp4');
    const reads = h.calls.filter(call => call.options.method !== 'POST');
    assert.equal(reads.length, 2);
    await h.event('start');
    assert.equal(h.element('summary-state').textContent, '正在精剪');
    const posted = h.calls.filter(call => call.options.method === 'POST');
    assert.equal(posted.length, 2);
    assert.deepEqual(readBody(posted[0].options), readBody(posted[1].options), 'access-code retry must preserve the request identity');
    await h.event('cancel');
    assert.equal(h.element('summary-state').textContent, '已取消');
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 3);
    for (const call of h.calls) {
        assert.equal(readHeader(call.options, 'X-SPARK-Codex-Editor'), '1');
        assert.ok(readHeader(call.options, 'X-Access-Code'));
    }
}

async function testPublicForbiddenShowsReasonAndCannotDispatch() {
    const message = '此公网地址未获准使用 Codex 精剪';
    const h = harness((url, options) => {
        assert.equal(readHeader(options, 'X-SPARK-Codex-Editor'), '1');
        assert.equal(readHeader(options, 'X-Access-Code'), 'test-code');
        return response({ message }, 403);
    }, new Map(), { origin: 'https://ps.wushi-api.com', accessCode: 'test-code' });
    h.sync(); await settle();
    assert.equal(h.element('start').disabled, true);
    assert.equal([h.element('capability').textContent, h.element('status').textContent].filter(text => text === message).length, 1);
    await h.event('start');
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
}

async function testSubmitDeduplicatesAndNetworkRetryKeepsRequestId() {
    const submitted = [], first = deferred();
    let latest = [], count = 0;
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (options.method === 'POST') { submitted.push(readBody(options)); return ++count === 1 ? first.promise : response({ job: job('one') }); }
        return response({ jobs: latest });
    });
    h.sync(); await settle();
    h.event('mode', 'change', 'trim_speed'); h.event('notes', 'input', '结尾多留两秒');
    h.event('model', 'change', 'gpt-6-astra'); h.event('effort', 'change', 'ultra');
    const sending = h.event('start'); h.event('start');
    assert.equal(submitted.length, 1);
    assert.equal(h.element('start').disabled, true);
    first.reject(new Error('连接中断')); await sending; await settle();
    assert.equal(h.element('start').textContent, '重试连接');
    await h.event('start');
    assert.equal(submitted.length, 2);
    assert.equal(submitted[0].request_id, submitted[1].request_id);
    assert.equal(submitted[0].mode, 'trim_speed');
    assert.equal(submitted[0].notes, '结尾多留两秒');
    assert.equal(submitted[0].model, 'gpt-6-astra');
    assert.equal(submitted[0].reasoning_effort, 'ultra');
    assert.equal(submitted[1].model, 'gpt-6-astra');
    assert.equal(submitted[1].reasoning_effort, 'ultra');
    assert.equal(h.element('start').disabled, true);
    latest = [job('one', '/outputs/a/final.mp4', 'completed')];
    await h.tick();
    assert.equal(h.element('start').textContent, '再剪一个版本');
    await h.event('start');
    assert.notEqual(submitted[2].request_id, submitted[0].request_id);
}

async function testLunaExcludesUltraAndClampsPreviousChoice() {
    const submitted = [];
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (options.method === 'POST') { submitted.push(readBody(options)); return response({ job: job('one') }); }
        return response({ jobs: [] });
    });
    h.sync(); await settle();
    h.event('effort', 'change', 'ultra');
    h.event('model', 'change', 'gpt-6-luna');
    assert.equal(h.element('effort').value, 'max');
    assert.doesNotMatch(h.element('effort').innerHTML, /value="ultra"/);
    await h.event('start');
    assert.equal(submitted[0].model, 'gpt-6-luna');
    assert.equal(submitted[0].reasoning_effort, 'max');
}

async function testDefinitiveBadRequestAllowsCorrection() {
    const submitted = [];
    const h = harness((url, options) => {
        if (options.method === 'POST') {
            submitted.push(readBody(options));
            return response({ message: '要求过长，请缩短' }, 400);
        }
        return response(url.endsWith('/capabilities') ? { available: true } : { jobs: [] });
    });
    h.sync(); await settle(); await h.event('start');
    assert.equal(submitted[0].model, 'gpt-6.1-sol');
    assert.equal(submitted[0].reasoning_effort, 'high');
    assert.equal(h.element('mode').disabled, false);
    assert.equal(h.element('model').disabled, false);
    assert.equal(h.element('effort').disabled, false);
    assert.equal(h.element('notes').disabled, false);
    assert.equal(h.storage.size, 0);
    assert.match(h.element('status').textContent, /要求过长/);
}

async function testPendingRequestRestoresFromServerAfterReload() {
    const storage = new Map();
    const first = harness((url, options) => {
        if (options.method === 'POST') throw new Error('connection lost');
        return response(url.endsWith('/capabilities') ? { available: true } : { jobs: [] });
    }, storage);
    first.sync(); await settle();
    first.event('model', 'change', 'gpt-6-astra');
    first.event('effort', 'change', 'max');
    await first.event('start');
    const requestId = JSON.parse([...storage.values()][0]).request_id;
    assert.equal(JSON.parse([...storage.values()][0]).model, 'gpt-6-astra');
    assert.equal(JSON.parse([...storage.values()][0]).reasoning_effort, 'max');
    const restored = harness(url => response(url.endsWith('/capabilities') ? { available: true } : {
        jobs: [job('restored', '/outputs/a/final.mp4', 'completed', { request_id: requestId })],
    }), storage);
    restored.sync(); await settle();
    assert.equal(restored.element('mode').disabled, false);
    assert.equal(restored.element('model').value, 'gpt-6-astra');
    assert.equal(restored.element('effort').value, 'max');
    assert.equal(restored.element('start').textContent, '再剪一个版本');
    assert.equal(storage.size, 0);
    assert.equal(restored.element('output').hidden, false);
}

async function testUpgradeKeepsSupportedAndLegacyPendingRequestIdentity() {
    for (const savedSettings of [
        { model: 'gpt-6-sol', reasoning_effort: 'ultra' },
        {}, // Before model preferences, the fixed default was GPT-6 Sol + high.
    ]) {
        const saved = { source: '/outputs/a/final.mp4', mode: 'trim_speed', notes: '保留结尾',
            request_id: 'request-before-upgrade', ...savedSettings };
        const storage = new Map([['spark_codex_video_edit_pending:outputs/a/final.mp4', JSON.stringify(saved)]]);
        const submitted = [];
        const h = harness((url, options) => {
            if (options.method === 'POST') {
                submitted.push(readBody(options));
                return response({ job: job('existing', saved.source, 'queued', { ...savedSettings }) });
            }
            return response(url.endsWith('/capabilities') ? { available: true } : { jobs: [] });
        }, storage);
        h.sync(); await settle();
        assert.equal(h.element('model').value, 'gpt-6-sol');
        assert.equal(h.element('effort').value, savedSettings.reasoning_effort || 'high');
        assert.equal(h.element('notes').value, saved.notes);
        await h.event('start');
        assert.equal(submitted[0].request_id, saved.request_id);
        assert.equal(submitted[0].model, 'gpt-6-sol');
        assert.equal(submitted[0].reasoning_effort, savedSettings.reasoning_effort || 'high');
    }
}

async function testRemovedModelDraftUsesNewDefaultAndNewRequestIdentity() {
    const saved = { source: '/outputs/a/final.mp4', mode: 'trim_speed', notes: '保留结尾',
        model: 'claude-sonnet-4-6', reasoning_effort: 'high', request_id: 'request-before-upgrade' };
    const storage = new Map([['spark_codex_video_edit_pending:outputs/a/final.mp4', JSON.stringify(saved)]]);
    const submitted = [];
    const h = harness((url, options) => {
        if (options.method === 'POST') {
            submitted.push(readBody(options));
            return response({ job: job('new') });
        }
        return response(url.endsWith('/capabilities') ? { available: true } : { jobs: [
            job('historical', saved.source, 'completed', { model: saved.model, reasoning_effort: 'high' }),
        ] });
    }, storage);
    h.sync(); await settle();
    assert.equal(h.element('model').value, 'gpt-6.1-sol');
    assert.equal(h.element('model').disabled, false);
    assert.equal(h.element('notes').value, saved.notes);
    assert.equal(h.element('mode').value, saved.mode);
    assert.match(h.element('job-config').textContent, /claude-sonnet-4-6/);
    assert.equal(h.element('player').src, '/outputs/a/historical.mp4');
    assert.equal(storage.size, 0);
    await h.event('start');
    assert.equal(submitted[0].model, 'gpt-6.1-sol');
    assert.equal(submitted[0].notes, saved.notes);
    assert.notEqual(submitted[0].request_id, saved.request_id);
}

async function testOldGetAndPostResponsesDoNotChangeAnotherSource() {
    const getA = deferred(), postB = deferred();
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (options.method === 'POST') return postB.promise;
        const source = decodeURIComponent(url.split('=')[1]);
        if (source.includes('/a/')) return getA.promise;
        return response({ jobs: [] });
    });
    h.sync(); h.sync('/outputs/b/final.mp4'); await settle();
    getA.resolve(response({ jobs: [job('old-A', '/outputs/a/final.mp4', 'completed')] })); await settle();
    assert.equal(h.element('output').hidden, true);
    const submit = h.event('start'); h.sync('/outputs/c/final.mp4'); await settle();
    postB.resolve(response({ job: job('old-B', '/outputs/b/final.mp4', 'completed') })); await submit;
    assert.equal(h.element('output').hidden, true);
    assert.equal(h.element('start').textContent, '开始精剪');
}

async function testAnotherSourceCanStartWhileFirstIsRunning() {
    const submitted = [];
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (options.method === 'POST') {
            const body = readBody(options);
            submitted.push(body);
            return response({ job: job('running-B', body.source, 'queued') });
        }
        const source = decodeURIComponent(url.split('=')[1]);
        return response({ jobs: source.includes('/a/') ? [job('running-A')] : [] });
    });
    h.sync('/outputs/a/final.mp4'); await settle();
    assert.equal(h.element('start').disabled, true);
    h.sync('/outputs/b/final.mp4'); await settle();
    assert.equal(h.element('start').disabled, false);
    await h.event('start');
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].source, '/outputs/b/final.mp4');
    assert.equal(h.element('start').disabled, true);
    h.sync('/outputs/a/final.mp4'); await settle();
    assert.equal(h.element('summary-state').textContent, '正在精剪');
    assert.equal(h.element('cancel').hidden, false);
}

async function testCancelWaitsForWorkerAndPreservesPreviousResult() {
    let recent = [job('active', '/outputs/a/final.mp4', 'running', { created_at: 2000 }),
        job('previous', '/outputs/a/final.mp4', 'completed', { created_at: 1000 })];
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (url.endsWith('/cancel')) {
            assert.equal(readBody(options).id, 'active');
            recent[0] = job('active', '/outputs/a/final.mp4', 'running', { stage: 'cancelling', message: '正在停止精剪…', created_at: 2000 });
            return response({ job: recent[0] });
        }
        return response({ jobs: recent });
    });
    h.sync(); await settle();
    assert.equal(h.element('player').src, '/outputs/a/previous.mp4');
    assert.equal(h.element('output-title').textContent, '上一版精剪结果');
    await h.event('cancel');
    assert.equal(h.element('cancel').disabled, true);
    assert.match(h.element('status').textContent, /正在停止/);
    const requests = h.calls.length; await h.event('cancel'); assert.equal(h.calls.length, requests);
    recent[0] = job('active', '/outputs/a/final.mp4', 'cancelled', { created_at: 2000 });
    await h.tick();
    assert.equal(h.element('cancel').hidden, true);
    assert.equal(h.element('start').textContent, '重试精剪');
    assert.equal(h.element('player').src, '/outputs/a/previous.mp4');
}

async function testHistoryPreviewLogsAndSourceVersionStayStable() {
    let recent = [job('new', '/outputs/中文/final.mp4', 'running', {
        created_at: 2000, model: 'gpt-6-astra', reasoning_effort: 'ultra',
    }), job('old', '/outputs/中文/final.mp4', 'completed', {
        created_at: 1000, model: 'gpt-6-luna', reasoning_effort: 'max',
        source_changed: true, logs: [{ message: '<script>unsafe</script>' }],
    })];
    const h = harness(url => response(url.endsWith('/capabilities') ? { available: true } : { jobs: recent }));
    h.sync('/Users/test/project/outputs/中文/final.mp4', { url: '/outputs/%E4%B8%AD%E6%96%87/final.mp4' }); await settle();
    assert.equal(h.element('model').value, 'gpt-6-astra');
    assert.equal(h.element('effort').value, 'ultra');
    assert.match(h.element('history').innerHTML, /GPT-6 Astra · 思考强度：最高/);
    assert.match(h.element('history').innerHTML, /GPT-6 Luna · 思考强度：很高/);
    h.event('history', 'change', 'old');
    assert.equal(h.element('job-config').textContent, '所选记录：GPT-6 Luna · 思考强度：很高');
    h.element('logs').open = true;
    const sourceBefore = h.element('player').src, writes = h.element('player').srcWrites;
    await h.tick();
    assert.equal(h.element('history').value, 'old');
    assert.equal(h.element('player').src, sourceBefore);
    assert.equal(h.element('player').srcWrites, writes);
    assert.equal(h.element('logs').open, true);
    assert.match(h.element('log-list').innerHTML, /&lt;script&gt;/);
    assert.match(h.element('source-note').textContent, /此前的成片版本/);
    h.event('reveal'); assert.deepEqual(h.revealed[0], ['/outputs/a/old.mp4', '精剪成片']);
    h.sync(null);
    assert.equal(h.element('root').hidden, true);
    assert.equal(h.element('player').paused, true);
    assert.equal(h.timers.size, 0);
}

async function testReplacingSameSourceChecksVersionAgain() {
    let requests = 0;
    const h = harness(url => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        ++requests;
        return response({ jobs: [job('old', '/outputs/a/final.mp4', 'completed', { source_changed: requests > 1 })] });
    });
    h.sync(undefined, { size_bytes: 100 }); await settle();
    h.sync(undefined, { size_bytes: 200 }); await settle();
    assert.equal(requests, 2);
    assert.match(h.element('source-note').textContent, /此前的成片版本/);
}

async function testEncodedOutputUsesUrlForPreviewDownloadAndReveal() {
    const h = harness(url => response(url.endsWith('/capabilities') ? { available: true } : { jobs: [
        job('encoded', '/outputs/a/final.mp4', 'completed', { output: {
            file: '/Users/test/project/outputs/a/剪辑#100%.mp4',
            url: '/outputs/a/%E5%89%AA%E8%BE%91%23100%25.mp4',
        } }),
    ] }));
    h.sync(); await settle();
    const expected = '/outputs/a/%E5%89%AA%E8%BE%91%23100%25.mp4';
    assert.equal(h.element('player').src, expected);
    assert.equal(h.element('download').href, expected);
    h.event('reveal'); assert.equal(h.revealed[0][0], expected);
}

async function testOlderPollCannotEraseNewlySubmittedJob() {
    let reads = 0;
    const oldPoll = deferred(), submit = deferred();
    const h = harness((url, options) => {
        if (url.endsWith('/capabilities')) return response({ available: true });
        if (options.method === 'POST') return submit.promise;
        return ++reads === 1 ? response({ jobs: [] }) : oldPoll.promise;
    });
    h.sync(); await settle();
    const polling = h.event('refresh');
    const sending = h.event('start');
    submit.resolve(response({ job: job('newly-started') })); await sending;
    oldPoll.resolve(response({ jobs: [] })); await polling; await settle();
    assert.equal(h.element('start').disabled, true);
    assert.equal(h.element('cancel').hidden, false);
    assert.equal(h.element('summary-state').textContent, '正在精剪');
}

async function testRepeatedConnectionErrorIsShownOnce() {
    const h = harness(() => response({ message: '暂时无法连接' }, 503));
    h.sync(); await settle();
    const messages = [h.element('capability').textContent, h.element('status').textContent];
    assert.equal(messages.filter(text => text === '暂时无法连接').length, 1);
}

async function testDeletedGalleryResultHasNoBrokenPreview() {
    let recent = [job('result', '/outputs/a/final.mp4', 'completed')];
    const h = harness(url => response(url.endsWith('/capabilities') ? { available: true } : { jobs: recent }));
    h.sync(); await settle();
    assert.equal(h.element('output').hidden, false);
    recent = [{ ...recent[0], output_missing: true, message: '精剪结果文件已删除或不可用，可以重新精剪。' }];
    await h.event('refresh'); await settle();
    assert.equal(h.element('output').hidden, true);
    assert.equal(h.element('player').src, undefined);
    assert.match(h.element('history').innerHTML, /结果文件不可用/);
    assert.match(h.element('status').textContent, /重新精剪/);
}

async function testLiveProgressStaysVisibleWithFoldedPanelAndOlderSelection() {
    let recent = [job('active', '/outputs/a/final.mp4', 'running', {
        stage: 'rendering', message: '正在导出成片', created_at: Date.now() - 125000,
    }), job('old', '/outputs/a/final.mp4', 'completed', { created_at: Date.now() - 600000 })];
    const h = harness(url => response(url.endsWith('/capabilities') ? { available: true } : { jobs: recent }));
    h.sync(); await settle();
    h.element('panel').open = false;
    h.event('history', 'change', 'old');
    assert.equal(h.element('progress').hidden, false);
    assert.equal(h.element('progress-stage').textContent, '精剪中 · 导出精剪视频');
    assert.match(h.element('progress-elapsed').textContent, /已运行 2 分/);
    assert.equal(h.element('progress-message').textContent, '正在导出成片');
    recent = [{ ...recent[0], stage: 'verifying', message: '正在校验音轨' }, recent[1]];
    await h.tick();
    assert.equal(h.element('progress-stage').textContent, '精剪中 · 校验成片和音轨');
    assert.equal(h.element('progress-message').textContent, '正在校验音轨');
    recent = [{ ...recent[0], status: 'completed', stage: 'completed' }, recent[1]];
    await h.tick();
    assert.equal(h.element('progress').hidden, true);
    h.sync(null);
    assert.equal(h.element('root').hidden, true);
}

(async () => {
    testMarkupLivesInsideFinishedResult();
    await testDefaultAndUnavailableAndCanonicalSource();
    await testPublicHistoryAndMutationsKeepEditorAndAccessHeaders();
    await testPublicForbiddenShowsReasonAndCannotDispatch();
    await testSubmitDeduplicatesAndNetworkRetryKeepsRequestId();
    await testLunaExcludesUltraAndClampsPreviousChoice();
    await testDefinitiveBadRequestAllowsCorrection();
    await testPendingRequestRestoresFromServerAfterReload();
    await testUpgradeKeepsSupportedAndLegacyPendingRequestIdentity();
    await testRemovedModelDraftUsesNewDefaultAndNewRequestIdentity();
    await testOldGetAndPostResponsesDoNotChangeAnotherSource();
    await testAnotherSourceCanStartWhileFirstIsRunning();
    await testCancelWaitsForWorkerAndPreservesPreviousResult();
    await testHistoryPreviewLogsAndSourceVersionStayStable();
    await testReplacingSameSourceChecksVersionAgain();
    await testEncodedOutputUsesUrlForPreviewDownloadAndReveal();
    await testOlderPollCannotEraseNewlySubmittedJob();
    await testRepeatedConnectionErrorIsShownOnce();
    await testDeletedGalleryResultHasNoBrokenPreview();
    await testLiveProgressStaysVisibleWithFoldedPanelAndOlderSelection();
    console.log('Codex video editor UI regression tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
