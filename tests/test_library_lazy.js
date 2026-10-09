const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const read = name => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');
const app = read('app.js'), config = read('js/config.js'), api = read('js/api_client.js');
function section(source, start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
    assert(from >= 0 && to > from, `${start} section exists`);
    return source.slice(from, to);
}
const librarySource = section(app, 'async function loadLibrary()', '// Check API status');
const navigationSource = section(app, 'function loadSavedIdea(', '// Export Library to JSON');
const exportSource = section(app, 'async function exportAllLibrary()', '// Import Library from JSON');
const favoriteSource = section(app, 'function updateFavoriteButtonState()', '// 实况终端');
const recoverySource = section(api, 'function saveActiveBackgroundTasksToLocalStorage()', '// 后端 server_common.log()');
const clone = value => JSON.parse(JSON.stringify(value));
const full = id => ({ id, title: `Project ${id}`, project_key: `project_${id}`, prompt_block: `full prompt ${id}`,
    audit_md: `audit ${id}`, repair_md: `repair ${id}`, covers: [`cover_${id}.webp`],
    frameRun: { frames: [{ sequence: 1, file: `frame_${id}.webp` }] },
    untouched: { nested: ['keep this', id] }, updated_at: 42 });
const summary = idea => ({ id: idea.id, title: idea.title, project_key: idea.project_key, cover_count: 1, frame_count: 1 });
function harness({ records = [full('a'), full('b')], backup = [], fail = false, storage: initial = {} } = {}) {
    const storage = new Map(Object.entries(initial));
    if (backup.length) storage.set('spark_library', JSON.stringify(backup));
    const requests = [], rendered = [], messages = [], nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, { attrs: {}, style: {}, textContent: '', classList: { add() {}, remove() {} },
            setAttribute(key, value) { this.attrs[key] = value; }, click() { this.clicked = true; }, remove() {} });
        return nodes.get(id);
    };
    const ctx = { console: { log() {}, warn() {}, error() {} }, savedIdeas: [], savedIdeaIndex: [], currentIdea: null,
        localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key) },
        document: { getElementById: node, createElement: () => node('download'), body: { appendChild() {} } },
        refreshProjects() {}, showToast: (...args) => messages.push(args), switchMainTab() {}, switchTab() {},
        syncCandidateModeToggleFromIdea() {}, saveCurrentIdeaState() {}, updateActiveGenerationBanner() {},
        renderIdea: idea => rendered.push(idea), AbortController,
        fetch: async (url, init = {}) => {
            requests.push({ url, init });
            if (fail) throw new Error('offline');
            let value;
            if (url === '/api/library/index') value = { items: records.map(summary), count: records.length };
            else if (url === '/api/library') value = records;
            else if (url.startsWith('/api/library/item?id=')) {
                const id = decodeURIComponent(url.split('id=')[1]);
                value = records.find(item => item.id === id);
                if (!value) return { status: 404, ok: false, json: async () => ({ error: 'not found' }) };
            } else if (url === '/api/library/item' && init.method === 'POST') value = { status: 'success' };
            else throw new Error(`unexpected request: ${url}`);
            return { status: 200, ok: true, json: async () => clone(value) };
        },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(librarySource + navigationSource + exportSource + favoriteSource, ctx);
    vm.runInContext(config.slice(config.indexOf('async function loadCurrentIdeaState()')), ctx);
    return { ctx, requests, storage, nodes, rendered, messages, records };
}

test('startup downloads only the index; current project recovery hydrates only its body', async () => {
    const h = harness({ backup: [full('offline-only')] });
    const previousBackup = h.storage.get('spark_library');
    await h.ctx.loadLibrary();
    await h.ctx.loadCurrentIdeaState();
    assert.deepEqual(h.requests.map(r => r.url), ['/api/library/index']);
    assert.equal(h.ctx.savedIdeas.length, 0);
    assert.equal(h.ctx.libraryEntries().length, 2);
    assert.equal(h.storage.get('spark_library'), previousBackup, 'index never overwrites complete offline bodies');
    h.storage.set('spark_current_idea_id', 'a');
    await h.ctx.loadCurrentIdeaState();
    assert.deepEqual(h.requests.map(r => r.url), ['/api/library/index', '/api/library/item?id=a']);
    assert.equal(h.ctx.currentIdea.prompt_block, 'full prompt a');
    assert.strictEqual(h.ctx.savedIdeas[0], h.ctx.currentIdea, 'events and saves share the recovered complete owner');
    assert.strictEqual(h.rendered[0], h.ctx.currentIdea);
});

test('concurrent hydration shares one request and opening a different project loads only that record', async () => {
    const h = harness();
    await h.ctx.loadLibrary();
    let release;
    const fetch = h.ctx.fetch;
    h.ctx.fetch = async (url, init) => {
        if (url.endsWith('id=a')) await new Promise(resolve => { release = resolve; });
        return fetch(url, init);
    };
    const first = h.ctx.ensureLibraryIdea('a'), second = h.ctx.ensureLibraryIdea('a');
    release();
    const [a, again] = await Promise.all([first, second]);
    assert.strictEqual(a, again);
    assert.equal(h.requests.filter(r => r.url.endsWith('id=a')).length, 1);
    await h.ctx.openSparkProject({ ideaId: 'b' });
    assert.equal(h.ctx.currentIdea.prompt_block, 'full prompt b');
    assert.equal(h.requests.filter(r => r.url.endsWith('id=b')).length, 1);
    await h.ctx.openSparkProject({ ideaId: 'b' });
    assert.equal(h.requests.filter(r => r.url.endsWith('id=b')).length, 1, 'hydrated project is reused');
    assert(!h.requests.some(r => r.url === '/api/library'));
});

test('saving a cover/frame change keeps all body fields and all unopened offline records; summaries cannot save', async () => {
    const offline = full('unopened');
    const h = harness({ backup: [offline] });
    await h.ctx.loadLibrary();
    const idea = await h.ctx.ensureLibraryIdea('a');
    idea.covers.push('new-cover.webp');
    idea.frameRun.frames.push({ sequence: 2, file: 'new-frame.webp' });
    assert.equal(await h.ctx.persistIdeaItem(idea), true);
    const sent = JSON.parse(h.requests.find(r => r.init.method === 'POST').init.body).item;
    assert.equal(sent.prompt_block, 'full prompt a');
    assert.equal(sent.audit_md, 'audit a');
    assert.deepEqual(sent.untouched, full('a').untouched);
    assert.equal(sent.frameRun.frames.length, 2);
    const mirror = JSON.parse(h.storage.get('spark_library'));
    assert.deepEqual(mirror.find(item => item.id === 'unopened'), offline);
    assert.equal(mirror.find(item => item.id === 'a').prompt_block, 'full prompt a');
    const before = h.storage.get('spark_library');
    const posts = h.requests.filter(r => r.init.method === 'POST').length;
    assert.equal(await h.ctx.persistIdeaItem(h.ctx.savedIdeaIndex[1]), false);
    assert.equal(h.requests.filter(r => r.init.method === 'POST').length, posts);
    assert.equal(h.storage.get('spark_library'), before);
});

test('offline recovery uses complete bodies; a server 404 cannot revive a deleted record', async () => {
    const h = harness({ backup: [full('a'), full('b')], fail: true });
    await h.ctx.loadLibrary();
    assert.equal(h.ctx.libraryEntries().length, 2);
    assert.equal((await h.ctx.ensureLibraryIdea('a')).audit_md, 'audit a');
    const online = harness({ backup: [full('deleted')] });
    await online.ctx.loadLibrary();
    assert.equal(await online.ctx.ensureLibraryIdea('deleted'), null);
    assert.equal(online.ctx.savedIdeas.length, 0);
});

test('favorites use the index, deletion preserves other backups, and archive preserves offline prompts', async () => {
    const h = harness({ backup: [full('a'), full('b')] });
    await h.ctx.loadLibrary();
    h.ctx.currentIdea = full('a');
    h.ctx.updateFavoriteButtonState();
    assert.equal(h.nodes.get('save-idea-btn-text').textContent, '已收藏点子');
    h.ctx.forgetLibraryIdeas(['b']);
    assert.equal(h.ctx.libraryEntries().length, 1);
    h.ctx.rememberLibraryArchive({ id: 'a', title: 'Project a', project_key: 'project_a', archived: true,
        archive: { final_videos: [{ url: 'archive.mp4' }] } });
    const mirror = JSON.parse(h.storage.get('spark_library'));
    assert.equal(mirror.length, 1);
    assert.equal(mirror[0].prompt_block, 'full prompt a');
    assert.equal(mirror[0].archived, true);
    assert.equal(mirror[0].frameRun, undefined);
    assert.deepEqual(mirror[0].covers, []);
    assert(!mirror[0]._librarySummary);
});

test('export retrieves all bodies only after the explicit user action', async () => {
    const h = harness();
    await h.ctx.loadLibrary();
    assert(!h.requests.some(r => r.url === '/api/library'));
    await h.ctx.exportAllLibrary();
    assert.equal(h.requests.filter(r => r.url === '/api/library').length, 1);
    const href = h.nodes.get('download').attrs.href;
    const exported = JSON.parse(decodeURIComponent(href.slice(href.indexOf(',') + 1)));
    assert.deepEqual(exported, h.records);
});

test('a restored background task hydrates only its complete owner and keeps the original task', async () => {
    const h = harness({ storage: { spark_active_background_tasks: JSON.stringify({ tasks: [
        { ideaId: 'b', projectKey: 'project_b', type: 'frames', taskId: 'original-task' },
    ] }) } });
    await h.ctx.loadLibrary();
    const records = new Map(), streams = [];
    h.ctx.ideaTasksById = {};
    h.ctx.findIdeaObjectById = id => h.ctx.savedIdeas.find(idea => idea.id === id) || null;
    h.ctx.getIdeaTaskRecord = (id, type) => records.get(`${id}:${type}`);
    h.ctx.streamFramesProgress = (taskId, owner) => {
        records.set(`${owner.id}:frames`, { taskId, streaming: true });
        streams.push({ taskId, owner });
    };
    vm.runInContext(recoverySource, h.ctx);
    await h.ctx.resumeActiveBackgroundTasksIfExists();
    assert.deepEqual(h.requests.map(r => r.url), ['/api/library/index', '/api/library/item?id=b']);
    assert.equal(streams.length, 1);
    assert.equal(streams[0].taskId, 'original-task');
    assert.equal(streams[0].owner.prompt_block, 'full prompt b');
    assert.strictEqual(streams[0].owner, h.ctx.savedIdeas[0]);
    await h.ctx.resumeActiveBackgroundTasksIfExists();
    assert.equal(streams.length, 1);
});
