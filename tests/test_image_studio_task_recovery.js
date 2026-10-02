const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Blob, File } = require('node:buffer');
const source = fs.readFileSync(path.join(__dirname, '..', 'js/image_studio.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(__dirname, '..', 'js/state.js'), 'utf8');
const normalizerSource = stateSource.slice(stateSource.indexOf('function normalizeApiImageModel('), stateSource.indexOf('\nconst IMAGE_MODELS'));
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
function node(id = '') {
    return { id, dataset: {}, style: {}, value: '', textContent: '', innerHTML: '', children: [], disabled: false,
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {}, remove() { this.removed = true; },
        click() { this.clicked = (this.clicked || 0) + 1; },
        appendChild(child) { this.children.push(child); },
        setAttribute() {}, removeAttribute() {}, querySelector() { return null; },
    };
}
function harness() {
    const ids = ['imgstudio-retry-file-input', 'imgstudio-generate-btn', 't2i-prompt', 't2i-model', 'i2i-prompt', 'i2i-model', 'i2i-style',
        'imgstudio-tab-i2i', 'imgstudio-pane-i2i', 'imgstudio-mobtab-create', 'imgstudio-mobtab-result', 'i2i-upload-previews'];
    const nodes = Object.fromEntries(ids.map(id => [id, node(id)]));
    const calls = [], toasts = [], storage = new Map(), readers = [];
    class Reader {
        readAsDataURL(file) {
            this.file = file; readers.push(this);
            if (file.defer) return;
            if (file.fail) this.onerror();
            else this.onload({ target: { result: file.data || 'data:image/png;base64,Y2hvc2VuLXJlZmVyZW5jZQ==' } });
        }
    }
    class RecordedFormData { constructor() { this.entries = []; } append(...entry) { this.entries.push(entry); } }
    const ctx = { console, Blob, File, FormData: RecordedFormData, FileReader: Reader, AbortController, atob,
        Date, Math, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
        localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        document: { addEventListener() {}, getElementById: id => nodes[id] || null, querySelectorAll: () => [],
            querySelector: () => null, createElement: tag => node(tag) },
        config: { model: 'shared-model' }, showToast: (...args) => toasts.push(args),
        escapeHtml: value => String(value || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
        fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ task_id: 'backend-new' }) }; },
    };
    ctx.window = ctx;
    vm.createContext(ctx); vm.runInContext(normalizerSource, ctx); vm.runInContext(source, ctx);
    vm.runInContext('imgStudioRenderTaskListUI = () => {}; imgStudioPollTaskStatus = () => {};', ctx);
    const set = (name, value) => { ctx.fixture = value; vm.runInContext(`${name} = fixture`, ctx); };
    const get = name => vm.runInContext(name, ctx);
    return { ctx, calls, toasts, storage, readers, nodes, set, get };
}
const failedTask = () => ({ id: 'task_missing', type: 'i2i', status: 'failed', prompt: '保留房屋，更换天空',
    model: 'gpt-image-2', ratio: '16:9', quality: '2K', error: '上次失败',
    createdAt: 1000, finishedAt: 2000, stages: [], extraData: { style: 'cinematic', fileNames: ['old.png'] }, filesStripped: true });
const file = (name = 'replacement.png', extra = {}) => ({ name, type: 'image/png', size: 100, ...extra });

async function testReplacingReferencesRetriesOriginalTaskWithNewFiles(model = 'gpt-image-2') {
    const h = harness(), task = failedTask();
    const expectedModel = ['gpt-image-2', 'gpt-image-2-2026-09-08'].includes(model) ? 'gpt-image-2.5' : model;
    task.model = model;
    h.set('imgStudioTaskList', [task]);
    const draft = [{ name: 'unrelated-draft.png', base64: 'data:image/png;base64,ZHJhZnQ=' }];
    h.set('imgStudioUploadedFiles', draft);
    h.ctx.retryImageStudioTask(task.id);
    assert.equal(h.nodes['imgstudio-retry-file-input'].clicked, 1);
    assert.equal(h.calls.length, 0, 'opening the picker alone never submits a task');
    await h.ctx.imgStudioRetryWithReferenceFiles(task.id, [file('new-a.png'), file('new-b.png')]); await settle();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, '/api/image/edits');
    const entries = h.calls[0].options.body.entries;
    const value = key => entries.find(row => row[0] === key)?.[1];
    assert.equal(value('prompt'), task.prompt);
    assert.equal(value('model'), expectedModel);
    assert.equal(value('aspect_ratio'), '16:9');
    assert.equal(value('image_size'), '2K');
    assert.equal(value('style'), 'cinematic');
    assert.equal(value('image').name, 'new-a.png');
    assert.equal(value('image[]').name, 'new-b.png');
    assert.equal(await value('image').text(), 'chosen-reference');
    assert.equal(task.id, 'task_missing');
    assert.equal(task.status, 'pending');
    assert.equal(task.backendTaskId, 'backend-new');
    assert.equal(task.extraData.fileNames, undefined);
    assert.equal(h.get('imgStudioUploadedFiles'), draft, 'a retry must not replace the current creation draft');
    const saved = JSON.parse(h.storage.get('spark_image_tasks'))[0];
    assert.equal(saved.extraData.files, undefined, 'reference bytes remain transient');
    assert.deepEqual(saved.extraData.fileNames, ['new-a.png', 'new-b.png']);
    assert.equal(saved._referenceRetryPending, undefined);
    assert.equal(saved.model, model, 'reference retry preserves the exact model ID');
    assert.equal(saved.submittedModel, expectedModel, 'actual retry model is recorded without rewriting the original model');
}

async function testRestoredVariantRetryPreservesOriginalModel() {
    for (const [model, expectedModel] of [
        ['gpt-image-2.5-sunburst', 'gpt-image-2.5-sunburst'],
        ['gpt-image-2.5-flare', 'gpt-image-2.5-flare'],
        ['gpt-image-2', 'gpt-image-2.5'],
        ['gpt-image-2-2026-09-08', 'gpt-image-2.5'],
    ]) {
        for (const type of ['t2i', 'i2i']) {
            const h = harness();
            h.ctx.config.imageModel = 'nano-banana-2';
            const archived = { ...failedTask(), type, model };
            h.storage.set('spark_image_tasks', JSON.stringify([archived]));
            h.ctx.imgStudioLoadTaskList();
            const restored = h.get('imgStudioTaskList')[0];
            assert.equal(restored.model, model);
            if (type === 'i2i') {
                restored.extraData.files = [{ name: 'restored.png', base64: 'data:image/png;base64,Y2hvc2VuLXJlZmVyZW5jZQ==' }];
            }
            h.ctx.retryImageStudioTask(restored.id);
            await settle();
            assert.equal(h.calls.length, 1);
            const body = h.calls[0].options.body;
            const sentModel = type === 't2i' ? JSON.parse(body).model
                : body.entries.find(entry => entry[0] === 'model')[1];
            assert.equal(sentModel, expectedModel, 'retry uses an available model despite a different global default');
            assert.equal(JSON.parse(h.storage.get('spark_image_tasks'))[0].model, model);
            assert.equal(JSON.parse(h.storage.get('spark_image_tasks'))[0].submittedModel, expectedModel);
            assert.equal(archived.model, model, 'loading and retrying preserve the original generation metadata');
            assert.equal(h.ctx.imgStudioTaskDisplayModel(restored), expectedModel);
            assert.match(h.ctx.imgStudioBuildFeedEntry(restored).innerHTML, new RegExp(`<span class="feed-model" translate="no">${expectedModel.replaceAll('.', '\\.')}</span>`), 'current task label shows the submitted model');
        }
    }
}

function testVariantHistoryReusePreservesOriginalModel() {
    for (const type of ['t2i', 'i2i']) {
        const h = harness();
        h.set('imgStudioCurrentTab', type);
        const modelSelect = h.nodes[`${type}-model`];
        modelSelect.options = ['nano-banana-2', 'gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2.5'].map(value => ({ value }));
        modelSelect.value = 'nano-banana-2';
        for (const [model, expectedModel] of [
            ['gpt-image-2.5-sunburst', 'gpt-image-2.5-sunburst'],
            ['gpt-image-2.5-flare', 'gpt-image-2.5-flare'],
            ['gpt-image-2', 'gpt-image-2.5'],
            ['gpt-image-2-2026-09-08', 'gpt-image-2.5'],
        ]) {
            const historyItem = { prompt: 'saved prompt', ratio: '16:9', quality: '2K', model };
            h.set('imgStudioHistory', [historyItem]);
            h.storage.set('spark_image_history', JSON.stringify([historyItem]));
            h.ctx.imgStudioReusePrompt(historyItem.prompt, historyItem.ratio, historyItem.quality, historyItem.model);
            assert.equal(modelSelect.value, expectedModel, 'reusing history selects an available model');
            assert.equal(h.nodes[`${type}-prompt`].value, 'saved prompt');
            assert.equal(h.get('imgStudioHistory')[0].model, model);
            assert.equal(JSON.parse(h.storage.get('spark_image_history'))[0].model, model, 'original history is not rewritten');
        }
        assert.equal(h.calls.length, 0, 'reusing history does not submit a generation');
    }
}

async function testInvalidOrCancelledSelectionNeverSendsPartialReferences() {
    for (const chosen of [[], [file('bad.txt', { type: 'text/plain' })], [file('large.png', { size: 11 * 1024 * 1024 })],
        [file('good.png'), file('broken.png', { fail: true })]]) {
        const h = harness(), task = failedTask(), original = task.extraData;
        h.set('imgStudioTaskList', [task]);
        h.ctx.chooseImageStudioRetryReferences(task.id);
        await h.ctx.imgStudioRetryWithReferenceFiles(task.id, chosen); await settle();
        assert.equal(h.calls.length, 0);
        assert.equal(task.status, 'failed');
        assert.equal(task.extraData, original);
        assert.equal(task._referenceRetryPending, undefined);
    }
}

async function testLateFileReadDoesNotReviveDeletedTask() {
    const h = harness(), task = failedTask();
    h.set('imgStudioTaskList', [task]); h.ctx.chooseImageStudioRetryReferences(task.id);
    const reading = h.ctx.imgStudioRetryWithReferenceFiles(task.id, [file('slow.png', { defer: true })]);
    assert.match(h.ctx.imgStudioFeedActionsHTML(task), /disabled[^>]*>正在读取/);
    h.set('imgStudioTaskList', []);
    h.readers[0].onload({ target: { result: 'data:image/png;base64,bmV3' } });
    await reading;
    assert.equal(h.calls.length, 0);
    assert.equal(task.extraData.files, undefined);
}

function testClearCompletedKeepsFailuresReferencesAndHistory() {
    const h = harness(), missing = failedTask();
    const tasks = [{ id: 'good', status: 'completed', image: '/outputs/good.webp' },
        { id: 'running', status: 'pending' }, { id: 'failed', status: 'failed' },
        { id: 'cancelled', status: 'cancelled' }, missing];
    const history = [{ id: 'historic', image: '/outputs/good.webp' }];
    h.set('imgStudioTaskList', tasks); h.set('imgStudioHistory', history);
    h.ctx.clearCompletedImageStudioTasks();
    assert.deepEqual(Array.from(h.get('imgStudioTaskList'), item => item.id), ['running', 'failed', 'cancelled', 'task_missing']);
    assert.equal(h.get('imgStudioHistory'), history);
    assert.equal(h.calls.length, 0);
    assert.deepEqual(JSON.parse(h.storage.get('spark_image_tasks')).map(item => item.id), ['running', 'failed', 'cancelled', 'task_missing']);
    assert.match(h.toasts.at(-1)[0], /1 条成功记录/);
}

async function testReferenceReadinessPreventsEmptyImageSubmission() {
    const h = harness();
    h.set('imgStudioCurrentTab', 'i2i');
    h.set('imgStudioUploadedFiles', [{ name: 'loading.png', base64: '' }]);
    h.nodes['i2i-prompt'].value = 'replace sky'; h.nodes['i2i-model'].value = 'gpt-image-2';
    h.ctx.imgStudioTriggerGeneration();
    assert.equal(h.get('imgStudioTaskList').length, 0);
    assert.equal(h.calls.length, 0);
    assert.match(h.toasts.at(-1)[0], /还在读取/);
    const task = { ...failedTask(), status: 'pending', extraData: { files: [{ name: 'broken.png', base64: '' }] } };
    h.set('imgStudioTaskList', [task]); await h.ctx.imgStudioRunTaskFetch(task);
    assert.equal(h.calls.length, 0);
    assert.equal(task.status, 'failed');
    assert.equal(h.ctx.imgStudioTaskNeedsReferences(task), true);
}

function testRestoredMissingReferencesHaveAnActionableEntry() {
    const h = harness(), task = failedTask();
    h.storage.set('spark_image_tasks', JSON.stringify([task])); h.ctx.imgStudioLoadTaskList();
    const restored = h.get('imgStudioTaskList')[0];
    assert.equal(h.ctx.imgStudioTaskNeedsReferences(restored), true);
    assert.match(h.ctx.imgStudioFeedActionsHTML(restored), /chooseImageStudioRetryReferences/);
    assert.match(h.ctx.imgStudioFeedActionsHTML(restored), /补图后重试/);
    assert.match(h.ctx.imgStudioTaskSummary(restored), /原有设置/);
    assert.equal(h.calls.length, 0);
}

function testProcessIsCollapsedAndUpdatesDoNotResetItsState() {
    const h = harness();
    const task = { ...failedTask(), type: 't2i', status: 'pending', backendTaskId: 'backend', lastStage: 'upstream rendering', stages: [{ t: 1000, text: '<raw stage>' }] };
    const built = h.ctx.imgStudioBuildFeedEntry(task);
    assert.match(built.innerHTML, /<details class="feed-process"><summary>生成过程/);
    assert.doesNotMatch(built.innerHTML, /<details[^>]*\bopen\b/);
    const parts = Object.fromEntries(['.feed-model', '.feed-status-chip', '.feed-actions', '.feed-current-status', '.feed-error-details', '.feed-stage-lines', '.feed-entry-result', '.feed-process'].map(key => [key, node()]));
    const entry = node(); entry.querySelector = selector => parts[selector];
    h.ctx.imgStudioUpdateFeedEntry(entry, task);
    assert.equal(parts['.feed-current-status'].textContent, '正在生成图片…');
    assert.match(parts['.feed-stage-lines'].children[0].innerHTML, /&lt;raw stage&gt;/);
    parts['.feed-process'].open = true;
    task.lastStage = '保存到磁盘'; task.stages.push({ t: 2000, text: '保存到磁盘' });
    h.ctx.imgStudioUpdateFeedEntry(entry, task);
    assert.equal(parts['.feed-process'].open, true);
    task.submittedModel = 'gpt-image-2.5';
    h.ctx.imgStudioUpdateFeedEntry(entry, task);
    assert.equal(parts['.feed-model'].textContent, 'gpt-image-2.5');
    assert.equal(parts['.feed-stage-lines'].children.length, 2);
    assert.equal(parts['.feed-current-status'].textContent, '图片已生成，正在保存…');
    task.status = 'failed'; task.error = '401: upstream detailed authentication error';
    h.ctx.imgStudioUpdateFeedEntry(entry, task);
    assert.match(parts['.feed-current-status'].textContent, /配置中心/);
    assert.equal(parts['.feed-error-details'].textContent, task.error);
    assert.equal(parts['.feed-process'].open, true);
}

async function testGeneratedImageCanBecomeAnActualReference() {
    const h = harness();
    h.ctx.fetch = async () => ({ blob: async () => new Blob(['generated'], { type: 'image/webp' }) });
    h.ctx.imgStudioSendToImageToImage('/outputs/generated.webp', 'generated.webp'); await settle();
    const references = h.get('imgStudioUploadedFiles');
    assert.equal(references.length, 1);
    assert.match(references[0].base64, /^data:image\/png;base64,/);
    assert.notEqual(references[0].base64, '/outputs/generated.webp');
}

async function testUnavailableVariantRetryAndGenerationNeverSubmit() {
    for (const type of ['t2i', 'i2i']) {
        for (const model of ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare-2026-09-08']) {
            const h = harness();
            h.ctx.setImageGatewayModelCatalog({ status: 'known', models: ['gpt-image-2.5'] });
            const task = { ...failedTask(), type, model };
            h.set('imgStudioTaskList', [task]);
            h.ctx.retryImageStudioTask(task.id);
            await settle();
            assert.equal(h.calls.length, 0);
            assert.equal(task.status, 'failed');
            assert.equal(task.model, model, 'unsupported retry keeps the original model instead of silently changing it');
            assert.equal(task.submittedModel, undefined);
            assert.match(h.toasts.at(-1)[0], /当前网关不可用.*改选 gpt-image-2\.5/);

            await h.ctx.imgStudioRunTaskFetch(task);
            assert.equal(h.calls.length, 0, 'direct submission also refuses unsupported models');
            assert.equal(h.ctx.imgStudioAddTask(type, 'prompt', model, '16:9', '2K', null), null);
            h.set('imgStudioCurrentTab', type);
            h.nodes[`${type}-model`].value = model;
            h.nodes[`${type}-prompt`].value = 'prompt';
            h.ctx.imgStudioTriggerGeneration();
            assert.equal(h.calls.length, 0);
            assert.match(h.toasts.at(-1)[0], /当前网关不可用/);
            assert.equal(h.get('imgStudioTaskList').length, 1);
        }
    }
}

(async () => {
    assert.match(html, /id="imgstudio-retry-file-input"[^>]*accept="image\/\*"[^>]*multiple[^>]*hidden/);
    await testReplacingReferencesRetriesOriginalTaskWithNewFiles();
    for (const model of ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2-2026-09-08']) {
        await testReplacingReferencesRetriesOriginalTaskWithNewFiles(model);
    }
    await testRestoredVariantRetryPreservesOriginalModel();
    testVariantHistoryReusePreservesOriginalModel();
    await testInvalidOrCancelledSelectionNeverSendsPartialReferences();
    await testLateFileReadDoesNotReviveDeletedTask();
    testClearCompletedKeepsFailuresReferencesAndHistory();
    await testReferenceReadinessPreventsEmptyImageSubmission();
    testRestoredMissingReferencesHaveAnActionableEntry();
    testProcessIsCollapsedAndUpdatesDoNotResetItsState();
    await testGeneratedImageCanBecomeAnActualReference();
    await testUnavailableVariantRetryAndGenerationNeverSubmit();
    console.log('Image Studio task recovery tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
