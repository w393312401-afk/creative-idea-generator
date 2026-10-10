const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const api = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');
const watcher = api.slice(api.indexOf('async function watchTaskUntilTerminal('), api.indexOf('// 视频请求在收到 task_id'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const ok = data => ({ ok: true, json: async () => data });

function setup(statuses, { streamPending = false, resultEvent = false } = {}) {
    const requests = [], events = [], timers = new Map();
    let nextTimer = 0, statusReads = 0, readerCancels = 0, streamSignal;
    const ctx = {
        console: { warn() {}, error() {} }, TextDecoder, AbortController, Date,
        setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (url.includes('/api/compose-status')) {
                const status = statuses[Math.min(statusReads++, statuses.length - 1)];
                if (status instanceof Error) throw status;
                return ok(status);
            }
            streamSignal = options.signal;
            if (streamPending) return new Promise(() => {});
            let delivered = false;
            return { ok: true, status: 200, body: { getReader: () => ({
                read() {
                    if (delivered) return new Promise(() => {});
                    delivered = true;
                    const body = resultEvent ? 'data: {"type":"result","data":{"frames":[]}}\n\n' : ': keepalive\n\n';
                    return Promise.resolve({ done: false, value: new TextEncoder().encode(body) });
                },
                // A broken transport may never finish cancellation either.
                cancel() { readerCancels++; return new Promise(() => {}); }
            }) } };
        }
    };
    vm.createContext(ctx); vm.runInContext(watcher, ctx);
    return {
        ctx, requests, events, timers,
        start: options => ctx.watchTaskUntilTerminal('original-task', {
            statusPollIntervalMs: 25, onEvent: (type, data) => events.push({ type, data }), ...options
        }),
        async poll() {
            await tick();
            const [id, fn] = timers.entries().next().value || [];
            assert(fn, 'the active watcher must schedule another status check');
            timers.delete(id); await fn(); await tick();
        },
        get cancels() { return readerCancels; },
        get signal() { return streamSignal; }
    };
}

(async () => {
    {
        const s = setup([{ status: 'failed', error: '保存自动视频清单失败' }]);
        const result = await s.start();
        assert.equal(result.status, 'failed');
        assert.equal(result.error, '保存自动视频清单失败');
        assert.equal(s.events.filter(event => event.type === 'error').length, 1);
        assert(s.signal.aborted, 'terminal recovery closes the original HTTP stream');
        assert.equal(s.timers.size, 0);
    }
    {
        const s = setup([{ status: 'running' }, { status: 'failed', error: '已停止' }]);
        const pending = s.start(); await s.poll();
        const result = await pending;
        assert.equal(result.status, 'failed', 'heartbeats and a pending read cannot hide a failed backend task');
        assert(s.cancels > 0);
        assert.equal(s.timers.size, 0);
        assert(s.requests.every(request => !request.options.method && request.options.cache === 'no-store'));
    }
    {
        const s = setup([Error('offline'), { status: 'running' }, { status: 'cancelled' }]);
        let settled = false;
        const pending = s.start().then(result => { settled = true; return result; });
        await s.poll();
        assert.equal(settled, false, 'offline status checks retain the original task');
        await s.poll();
        assert.equal((await pending).status, 'cancelled');
        assert.equal(s.timers.size, 0);
    }
    {
        const manifest = { frames: [{ sequence: 1 }], videos: [{ slot: 1 }],
            auto_video: { status: 'completed', task_id: 'original-task' } };
        const s = setup([{ status: 'completed', result: manifest }], { streamPending: true });
        const result = await s.start();
        assert.strictEqual(result.result, manifest, 'the status result restores delivered media when SSE never connects');
        assert.deepStrictEqual(s.events.map(event => event.type), ['auto_video_updated', 'result']);
        assert(s.signal.aborted);
    }
    {
        const s = setup([{ status: 'running' }], { resultEvent: true });
        const result = await s.start();
        assert.equal(result.status, 'completed', 'terminal SSE does not await a stuck reader.cancel');
        assert.equal(s.timers.size, 0);
    }
    {
        const s = setup([{ status: 'running' }]);
        const controller = new AbortController();
        const pending = s.start({ signal: controller.signal });
        await tick(); controller.abort();
        await assert.rejects(pending, error => error.name === 'AbortError');
        assert.equal(s.timers.size, 0);
        assert(s.requests.every(request => !request.url.includes('compose-cancel')),
            'closing a watcher must not cancel any server generation');
    }
    console.log('Task terminal status recovery tests passed (6 cases)');
})().catch(error => { console.error(error); process.exit(1); });
