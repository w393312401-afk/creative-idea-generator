const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'gallery.js'), 'utf8');

function element() {
    return {
        innerHTML: '', textContent: '', disabled: false, dataset: {}, listeners: {}, value: '',
        classList: { toggle() {}, add() {}, remove() {} },
        addEventListener(name, handler) { this.listeners[name] = handler; },
        setAttribute() {}, querySelectorAll() { return []; }, querySelector() { return null; },
    };
}

function harness() {
    const elements = Object.fromEntries([
        'gallery-groups', 'gallery-stats', 'gallery-filters', 'gallery-search', 'gallery-sort',
        'gallery-download-selected-btn', 'gallery-delete-selected-btn', 'gallery-select-all-btn',
        'gallery-selection-note', 'gallery-collapse-all-btn',
    ].map(id => [id, element()]));
    const downloads = [];
    const toasts = [];
    const confirmations = [];
    const ctx = {
        console, CSS: { escape: value => value },
        localStorage: { getItem() { return null; }, setItem() {} },
        setTimeout(callback) { callback(); return 1; }, clearTimeout() {},
        showToast: (...args) => toasts.push(args),
        escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
            .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
        document: {
            readyState: 'loading', addEventListener() {},
            getElementById: id => elements[id] || null,
            querySelectorAll() { return []; }, querySelector() { return null; },
            body: { appendChild() {} },
            createElement(tag) {
                assert.equal(tag, 'a');
                return { click() { downloads.push({ href: this.href, download: this.download }); }, remove() {} };
            },
        },
        window: { confirm(message) { confirmations.push(message); return true; } },
        fetch: async () => { throw new Error('unexpected request'); },
    };
    vm.createContext(ctx);
    vm.runInContext(source, ctx, { filename: 'gallery.js' });
    const item = (name, kind, extra = {}) => ({
        path: `outputs/项目/${name}`, url: `/outputs/项目/${name}`, name, kind,
        type: kind === 'video' || kind === 'merged' ? 'video' : 'image', mtime: 1, size: 1024, ...extra,
    });
    const data = {
        groups: [{ key: 'project', kind: 'project', title: '项目', idea_title: '用户项目名', items: [
            item('cover.webp', 'cover'), item('frame.webp', 'frame'), item('clip.mp4', 'video'),
            item('merged.mp4', 'merged'), item('edit.mp4', 'merged', { is_edited: true }),
        ] }],
        totals: { videos: 3, images: 2, bytes: 5120 },
    };
    ctx.fixture = data;
    vm.runInContext('galleryData = fixture;', ctx);
    return {
        ctx, elements, downloads, toasts, confirmations, data,
        run: code => vm.runInContext(code, ctx),
        selected: () => Array.from(ctx.gallerySelectedPaths()),
        select(...names) { names.forEach(name => vm.runInContext(`gallerySelected.add(${JSON.stringify(`outputs/项目/${name}`)})`, ctx)); },
        async settle() { await new Promise(resolve => setImmediate(resolve)); },
    };
}

function testCategoriesAndDetails() {
    const h = harness();
    assert.deepEqual(JSON.parse(JSON.stringify(h.ctx.galleryFilterCounts())), {
        all: 5, cover: 1, frame: 1, video: 1, merged: 2, studio: 0, orphan: 0,
    });
    h.run("galleryFilter = 'video'");
    assert.deepEqual(Array.from(h.ctx.galleryVisibleGroups()[0].items, item => item.name), ['clip.mp4']);
    h.run("galleryFilter = 'merged'");
    assert.deepEqual(Array.from(h.ctx.galleryVisibleGroups()[0].items, item => item.name), ['merged.mp4', 'edit.mp4']);
    h.ctx.renderGallery();
    const html = h.elements['gallery-groups'].innerHTML;
    assert.match(html, /删除筛选结果/);
    assert.match(html, /筛选 2 \/ 5/);
    assert.match(html, /<details class="gallery-group-details"><summary>项目详情<\/summary>/);
    assert.match(html, /<details class="gallery-file-details"><summary>详情<\/summary>/);
    assert.match(html, /<span class="g-name" title="edit.mp4">edit.mp4/);
    assert.match(html, /精剪成片/);
    assert.doesNotMatch(html, /删除整个项目|整个目录|g-group-dir/);
}

function testFilterAndSearchRemoveHiddenSelections() {
    const h = harness();
    h.select('cover.webp', 'clip.mp4', 'merged.mp4', 'edit.mp4');
    h.run("galleryFilter = 'merged'");
    h.ctx.renderGallery();
    assert.deepEqual(h.selected(), ['outputs/项目/merged.mp4', 'outputs/项目/edit.mp4']);
    assert.equal(h.run('gallerySelected.size'), 2);
    h.run("gallerySearch = 'edit'");
    h.ctx.renderGallery();
    assert.deepEqual(h.selected(), ['outputs/项目/edit.mp4']);
    h.run("gallerySearch = ''; galleryFilter = 'all'");
    h.ctx.renderGallery();
    assert.deepEqual(h.selected(), ['outputs/项目/edit.mp4'], 'cleared hidden selections must not reappear');
    assert.match(h.elements['gallery-selection-note'].textContent, /已选 1 项.*当前筛选/);
}

async function testZipUsesOneNormalDownloadAndCurrentSelection() {
    const h = harness();
    h.select('clip.mp4', 'merged.mp4', 'edit.mp4');
    h.run("galleryFilter = 'merged'"); // Even before re-render, hidden choices cannot enter the request.
    let finish;
    const requests = [];
    h.ctx.fetch = async (url, options) => {
        requests.push({ url, options });
        return new Promise(resolve => { finish = resolve; });
    };
    const first = h.ctx.galleryDownloadSelected();
    await h.ctx.galleryDownloadSelected();
    assert.equal(requests.length, 1, 'repeated clicks while packing must not create duplicate ZIPs');
    assert.equal(h.elements['gallery-download-selected-btn'].disabled, true);
    assert.equal(requests[0].url, '/api/gallery/download-zip');
    assert.deepEqual(JSON.parse(requests[0].options.body), { paths: ['outputs/项目/merged.mp4', 'outputs/项目/edit.mp4'] });
    finish({ ok: true, json: async () => ({ url: '/api/gallery/download-zip/token', filename: 'selected.zip', count: 2 }) });
    await first;
    assert.deepEqual(h.downloads, [{ href: '/api/gallery/download-zip/token', download: 'selected.zip' }]);
    assert.equal(h.elements['gallery-download-selected-btn'].disabled, false);
    assert.match(h.toasts.at(-1)[0], /2 个文件/);
}

async function testZipFailureRestoresDownloadControl() {
    const h = harness();
    h.select('merged.mp4', 'edit.mp4');
    h.ctx.fetch = async () => ({ ok: false, status: 409, json: async () => ({ message: '文件正在更新，请稍后重新下载' }) });
    await h.ctx.galleryDownloadSelected();
    assert.equal(h.downloads.length, 0);
    assert.equal(h.elements['gallery-download-selected-btn'].disabled, false);
    assert.match(h.toasts.at(-1)[0], /文件正在更新/);
}

async function testSingleFileDoesNotCreateZip() {
    const h = harness();
    const item = h.data.groups[0].items[3];
    item.name = '成片 # 100%.mp4';
    item.path = 'outputs/项目/' + item.name;
    item.url = '/' + item.path;
    h.select(item.name);
    await h.ctx.galleryDownloadSelected();
    assert.deepEqual(h.downloads, [{ href: '/outputs/%E9%A1%B9%E7%9B%AE/%E6%88%90%E7%89%87%20%23%20100%25.mp4', download: item.name }]);
}

async function testDeleteSelectedRetainsRecordsAndDoesNotSendHiddenAssets() {
    const h = harness();
    h.ctx.initGallery();
    h.select('cover.webp', 'clip.mp4', 'merged.mp4', 'edit.mp4');
    h.run("galleryFilter = 'merged'; gallerySearch = 'edit'");
    const requests = [];
    h.ctx.fetch = async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => options
            ? { status: 'ok', deleted: JSON.parse(options.body).paths, failed: [] } : h.data };
    };
    h.elements['gallery-delete-selected-btn'].listeners.click();
    await h.settle();
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        paths: ['outputs/项目/edit.mp4'], remove_empty_projects: false,
    });
    assert.match(h.confirmations[0], /当前筛选中选中的 1 个文件/);
    assert.match(h.confirmations[0], /项目记录与此范围以外的文件保留/);
    assert.equal(requests[1].url, '/api/gallery');
}

function testGroupDeleteMeansFilteredMediaEvenWhenCollapsed() {
    const h = harness();
    h.ctx.initGallery();
    h.run("galleryFilter = 'merged'; galleryCollapsed.add('project')");
    const calls = [];
    h.ctx.galleryDeletePaths = (paths, label) => calls.push({ paths: Array.from(paths), label });
    const group = { dataset: { group: 'project' } };
    h.elements['gallery-groups'].listeners.click({ target: { closest(selector) {
        if (selector === '.gallery-group') return group;
        if (selector === '.g-group-delete') return {};
        return null;
    } } });
    assert.deepEqual(calls[0].paths, ['outputs/项目/merged.mp4', 'outputs/项目/edit.mp4']);
    assert.match(calls[0].label, /「用户项目名」当前筛选中的 2 个媒体文件/);
}

(async () => {
    testCategoriesAndDetails();
    testFilterAndSearchRemoveHiddenSelections();
    await testZipUsesOneNormalDownloadAndCurrentSelection();
    await testZipFailureRestoresDownloadControl();
    await testSingleFileDoesNotCreateZip();
    await testDeleteSelectedRetainsRecordsAndDoesNotSendHiddenAssets();
    testGroupDeleteMeansFilteredMediaEvenWhenCollapsed();
    console.log('gallery UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
