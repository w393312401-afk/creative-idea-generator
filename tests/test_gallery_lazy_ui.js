'use strict';
require('../tools/offline_node.js');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'js', 'gallery.js'), 'utf8');

function element() {
    return { innerHTML: '', textContent: '', disabled: false, value: '', dataset: {}, listeners: {}, scrollTop: 80,
        classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {},
        addEventListener(name, handler) { this.listeners[name] = handler; },
        querySelectorAll() { return []; }, querySelector() { return null; } };
}

function harness() {
    const elements = Object.fromEntries(['gallery-groups', 'gallery-stats', 'gallery-filters', 'gallery-search',
        'gallery-sort', 'gallery-download-selected-btn', 'gallery-delete-selected-btn', 'gallery-select-all-btn',
        'gallery-selection-note', 'gallery-collapse-all-btn', 'gallery-refresh-btn']
        .map(id => [id, element()]));
    const requests = [], toasts = [], confirmations = [], downloads = [], lightboxes = [];
    const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
        .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const media = (name, kind, extra = {}) => ({ name, kind, path: `outputs/one/${name}`, url: `/outputs/one/${name}`,
        type: ['video', 'merged'].includes(kind) ? 'video' : 'image', size: 10, mtime: 1, ...extra });
    const groups = [{ key: 'one', title: 'One', idea_title: 'Renamed One', kind: 'project', orphan: false,
        items: Array.from({ length: 27 }, (_, index) => media(`frame-${index}.webp`, 'frame', { mtime: index + 1 }))
            .concat([media('cover.webp', 'cover', { in_use: true, mtime: 50 }), media('clip.mp4', 'video', { mtime: 51 }),
                media('final.mp4', 'merged', { mtime: 52 })]) },
        { key: 'two', title: 'Two', kind: 'project', orphan: false,
            items: [media('other.webp', 'frame', { path: 'outputs/two/other.webp', url: '/outputs/two/other.webp', mtime: 100 })] }];
    let snapshot = 'v1', hook = null;
    const ctx = { console, URLSearchParams, CSS: { escape: value => value }, escapeHtml,
        setTimeout(fn) { fn(); return 1; }, clearTimeout() {},
        localStorage: { getItem() { return null; }, setItem() {} },
        showToast: (...args) => toasts.push(args), openLightbox: (...args) => lightboxes.push(args),
        document: { readyState: 'loading', addEventListener() {}, getElementById: id => elements[id] || null,
            querySelectorAll() { return []; }, querySelector() { return null; }, body: { appendChild() {} },
            createElement(tag) {
                assert.equal(tag, 'a');
                return { click() { downloads.push({ href: this.href, download: this.download }); }, remove() {} };
            } },
        window: { confirm(message) { confirmations.push(message); return true; } },
    };
    const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
    const matches = (group, item, filter, query) => (filter === 'all' || (filter === 'orphan'
        ? group.orphan === true || item.in_use === false : item.kind === filter))
        && (!query || [group.title, group.idea_title, item.name, item.path].some(value => String(value || '').toLowerCase().includes(query)));
    function queried(url) {
        const filter = url.searchParams.get('filter') || 'all';
        const query = (url.searchParams.get('q') || '').toLowerCase();
        return groups.map(group => ({ ...group, total_count: group.items.length,
            items: group.items.filter(item => matches(group, item, filter, query)).sort((a, b) => {
                const sort = url.searchParams.get('sort');
                if (sort === 'oldest') return a.mtime - b.mtime;
                if (sort === 'size') return b.size - a.size;
                if (sort === 'name') return a.name.localeCompare(b.name);
                return b.mtime - a.mtime;
            }) })).filter(group => group.items.length);
    }
    function defaultRequest(url, init) {
        if (url.pathname === '/api/gallery/index') {
            const all = groups.flatMap(group => group.items);
            const counts = { all: all.length, frame: 0, cover: 0, video: 0, merged: 0, studio: 0, orphan: 0 };
            all.forEach(item => { if (item.kind in counts) counts[item.kind]++; });
            return response({ revision: snapshot, filter_counts: counts,
                totals: { images: all.filter(item => item.type === 'image').length,
                    videos: all.filter(item => item.type === 'video').length, bytes: all.length * 10 },
                groups: queried(url).map(({ items, ...group }) => ({ ...group, filtered_count: items.length, count: items.length,
                    bytes: items.length * 10, latest_mtime: Math.max(...items.map(item => item.mtime)),
                    oldest_mtime: Math.min(...items.map(item => item.mtime)) })) });
        }
        if (url.pathname === '/api/gallery/items') {
            if (url.searchParams.get('revision') !== snapshot) return response({ error: 'snapshot_changed', revision: snapshot }, 409);
            const group = queried(url).find(group => group.key === url.searchParams.get('group'));
            if (!group) return response({ error: 'not_found' }, 404);
            const offset = Number(url.searchParams.get('offset'));
            const limit = Number(url.searchParams.get('limit'));
            return response({ revision: snapshot, group_key: group.key, total_count: group.total_count,
                filtered_count: group.items.length, offset, has_more: offset + limit < group.items.length,
                items: group.items.slice(offset, offset + limit) });
        }
        if (url.pathname === '/api/gallery/download-zip') {
            return response({ url: '/api/gallery/download-zip/fixture', count: JSON.parse(init.body).paths.length });
        }
        if (url.pathname === '/api/gallery/delete') {
            const paths = JSON.parse(init.body).paths;
            groups.forEach(group => { group.items = group.items.filter(item => !paths.includes(item.path)); });
            snapshot += '-deleted';
            return response({ status: 'ok', deleted: paths, failed: [] });
        }
        throw new Error(`Unexpected request: ${url.pathname}`);
    }
    ctx.fetch = async (source, init) => {
        const url = new URL(source, 'https://fixture.invalid');
        const request = { url, source, init };
        requests.push(request);
        const override = hook && await hook(request);
        return override || defaultRequest(url, init);
    };
    vm.createContext(ctx);
    vm.runInContext(source, ctx, { filename: 'gallery.js' });
    ctx.initGallery();
    return { ctx, elements, requests, toasts, confirmations, downloads, lightboxes, groups, response,
        run: code => vm.runInContext(code, ctx),
        hook(fn) { hook = fn; }, bump() { snapshot += '-new'; },
        selected: () => Array.from(ctx.gallerySelectedPaths()),
        async settle() { await new Promise(resolve => setImmediate(resolve)); },
        requestPaths: () => requests.map(request => request.url.pathname) };
}

async function testSummaryThenTwelveAndMore() {
    const h = harness();
    await h.ctx.refreshGallery();
    assert.deepEqual(h.requestPaths(), ['/api/gallery/index']);
    assert.match(h.elements['gallery-groups'].innerHTML, /30 项/);
    assert.doesNotMatch(h.elements['gallery-groups'].innerHTML, /class="gallery-card/);
    assert.equal(h.run('galleryFilterCounts().all'), 31);
    await h.ctx.galleryToggleCollapse('one');
    const first = h.requests.at(-1).url;
    assert.equal(first.pathname, '/api/gallery/items');
    assert.equal(first.searchParams.get('limit'), '12');
    assert.equal(h.run("galleryData.groups.find(g => g.key === 'one').items.length"), 12);
    assert.match(h.elements['gallery-groups'].innerHTML, /已显示 12 \/ 30/);
    const path = h.run("galleryData.groups.find(g => g.key === 'one').items[0].path");
    h.ctx.gallerySetSelected(path, true);
    await h.ctx.galleryLoadGroup('one', { more: true });
    assert.equal(h.requests.at(-1).url.searchParams.get('offset'), '12');
    assert.match(h.elements['gallery-groups'].innerHTML, /已显示 24 \/ 30/);
    assert.deepEqual(h.selected(), [path]);
    assert.equal(h.elements['gallery-groups'].scrollTop, 80);
    h.ctx.galleryToggleCollapse('one');
    assert.deepEqual(h.selected(), [path], 'folding a group never loses selected metadata');
}

async function testWholeScopeSelectionAndDownloadStayInFilter() {
    const h = harness();
    h.ctx.gallerySetFilter('frame');
    await h.ctx.refreshGallery();
    await h.ctx.galleryToggleScopeSelection(['one']);
    assert.equal(h.selected().length, 27, 'group selection must include unloaded pages');
    assert.equal(h.run("galleryData.groups.find(g => g.key === 'one').visibleCount"), 12);
    const itemRequests = h.requestPaths().filter(path => path === '/api/gallery/items').length;
    await h.ctx.galleryDownloadSelected();
    const download = h.requests.find(request => request.url.pathname === '/api/gallery/download-zip');
    const paths = JSON.parse(download.init.body).paths;
    assert.equal(paths.length, 27);
    assert(paths.every(path => path.includes('frame-')));
    assert.equal(h.requestPaths().filter(path => path === '/api/gallery/items').length, itemRequests,
        'the same query and snapshot reuse all loaded metadata');
    h.ctx.gallerySetSearch('frame-1.webp');
    await h.ctx.refreshGallery();
    assert.deepEqual(h.selected(), ['outputs/one/frame-1.webp']);
    h.ctx.gallerySetSearch('');
    await h.ctx.refreshGallery();
    assert.equal(h.selected().length, 1, 'cleared hidden selections never return');
    await h.ctx.galleryToggleScopeSelection();
    assert.equal(h.selected().length, 28, 'global all selects every matching group');
}

async function testCollapsedGroupDeletionIncludesAllMatchingMedia() {
    const h = harness();
    h.ctx.gallerySetFilter('frame');
    await h.ctx.refreshGallery();
    await h.ctx.galleryDeleteGroup('one');
    const request = h.requests.find(request => request.url.pathname === '/api/gallery/delete');
    const body = JSON.parse(request.init.body);
    assert.equal(body.paths.length, 27);
    assert.equal(body.remove_empty_projects, false);
    assert.match(h.confirmations[0], /27 个媒体文件/);
    assert(h.groups[0].items.every(item => item.kind !== 'frame'));
    assert.equal(h.groups[1].items.length, 1);
    assert(h.requests.some(request => request.url.searchParams.get('refresh') === '1'));
}

async function testSelectedGhostAndInUseWarning() {
    const h = harness();
    await h.ctx.refreshGallery();
    await h.ctx.galleryToggleScopeSelection(['one']);
    const deleted = h.groups[0].items.find(item => item.kind === 'frame').path;
    h.groups[0].items = h.groups[0].items.filter(item => item.path !== deleted);
    h.bump();
    await h.ctx.galleryDeletePaths(h.selected(), '所选媒体');
    const request = h.requests.find(request => request.url.pathname === '/api/gallery/delete');
    assert.equal(JSON.parse(request.init.body).paths.length, 29);
    assert(!JSON.parse(request.init.body).paths.includes(deleted));
    assert.match(h.confirmations[0], /1 个封面正被/);
}

async function testWholeGroupLightbox() {
    const h = harness();
    await h.ctx.refreshGallery();
    await h.ctx.galleryToggleCollapse('one');
    const path = h.run("galleryData.groups.find(g => g.key === 'one').items[0].path");
    await h.ctx.galleryOpenPreview(path);
    assert.equal(h.lightboxes[0][0].length, 30);
    assert.equal(h.lightboxes[0][1], 0);
    assert.equal(h.run("galleryData.groups.find(g => g.key === 'one').visibleCount"), 12);
}

async function testRenamedGroupKeepsMatchingSelection() {
    const h = harness();
    await h.ctx.refreshGallery();
    await h.ctx.galleryToggleScopeSelection(['one']);
    h.groups[0].idea_title = 'New Project Name';
    h.bump();
    h.ctx.gallerySetSearch('new project name');
    await h.ctx.refreshGallery();
    assert.equal(h.selected().length, 30, 'refresh updates selected group names before checking the search scope');
}

async function testLateIndexAndPageResponsesCannotCrossQueries() {
    const h = harness();
    let release;
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/index' && request.url.searchParams.get('filter') === 'all') {
            return new Promise(resolve => { release = resolve; });
        }
    });
    const old = h.ctx.refreshGallery();
    h.ctx.gallerySetFilter('video');
    await h.ctx.refreshGallery();
    release(h.response({ revision: 'old', groups: [], totals: {}, filter_counts: {} }));
    await old;
    assert.equal(h.run('galleryData.queryKey'), h.run('galleryQueryKey()'));
    assert.equal(h.run('galleryData.groups[0].filtered_count'), 1);
    assert.equal(h.run('galleryLoading'), false);
    h.hook(null);
    h.ctx.gallerySetFilter('all');
    await h.ctx.refreshGallery();
    let releasePage;
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/items') return new Promise(resolve => { releasePage = resolve; });
    });
    const opening = h.ctx.galleryToggleCollapse('one');
    h.ctx.gallerySetFilter('video');
    await h.ctx.refreshGallery({ suppressAutoLoad: true });
    releasePage(h.response({ revision: 'v1', group_key: 'one', items: h.groups[0].items.slice(0, 12),
        filtered_count: 30, total_count: 30, offset: 0 }));
    await opening;
    assert.equal(h.run('galleryData.groups[0].items.length'), 0);
    assert.equal(h.run('galleryData.groups[0].filtered_count'), 1);
}

async function testSnapshotRetryIsBoundedAndNeverExecutesPartialDelete() {
    const h = harness();
    await h.ctx.refreshGallery();
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/items') {
            h.bump();
            return h.response({ error: 'snapshot_changed' }, 409);
        }
    });
    await h.ctx.galleryDeleteGroup('one');
    assert.equal(h.requestPaths().filter(path => path === '/api/gallery/items').length, 2);
    assert.equal(h.confirmations.length, 0);
    assert(!h.requests.some(request => request.init?.method === 'POST'));
    assert.match(h.toasts.at(-1)[0], /操作未执行/);
    assert.equal(h.run('galleryBulkLoading'), false);
}

async function testOneSnapshotChangeRecovers() {
    const h = harness();
    await h.ctx.refreshGallery();
    let bumped = false;
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/items' && !bumped) { bumped = true; h.bump(); }
    });
    await h.ctx.galleryToggleCollapse('one');
    assert.equal(h.run("galleryData.groups.find(g => g.key === 'one').items.length"), 12);
    assert.equal(h.requestPaths().filter(path => path === '/api/gallery/items').length, 2);
    assert.equal(h.toasts.length, 0);
}

async function testRefreshDuringCollectionStopsWholeScopeAction() {
    const h = harness();
    await h.ctx.refreshGallery();
    let pending;
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/items' && request.url.searchParams.get('group') === 'two') {
            return new Promise(resolve => { pending = resolve; });
        }
    });
    const selecting = h.ctx.galleryToggleScopeSelection();
    while (!pending) await h.settle();
    await h.ctx.refreshGallery({ suppressAutoLoad: true });
    pending(h.response({ revision: 'v1', group_key: 'two', items: h.groups[1].items,
        filtered_count: 1, total_count: 1, offset: 0 }));
    await selecting;
    assert.equal(h.selected().length, 0, 'a refresh cannot turn global selection into a partially loaded scope');
    assert.match(h.toasts.at(-1)[0], /操作未执行/);
}

async function testQueryChangeWhileInitialIndexIsLoading() {
    const h = harness();
    let pending;
    h.hook(request => {
        if (request.url.pathname === '/api/gallery/index' && request.url.searchParams.get('filter') === 'all') {
            return new Promise(resolve => { pending = resolve; });
        }
    });
    const loading = h.ctx.refreshGallery();
    h.elements['gallery-filters'].listeners.click({ target: { closest() { return { dataset: { filter: 'video' } }; } } });
    await h.settle();
    pending(h.response({ revision: 'v1', groups: [], totals: {}, filter_counts: {} }));
    await loading;
    assert.equal(h.run('galleryLoading'), false);
    assert.equal(h.run('galleryData.groups[0].filtered_count'), 1);
    assert.equal(h.run('galleryData.queryKey'), h.run('galleryQueryKey()'));
}

(async () => {
    await testSummaryThenTwelveAndMore();
    await testWholeScopeSelectionAndDownloadStayInFilter();
    await testCollapsedGroupDeletionIncludesAllMatchingMedia();
    await testSelectedGhostAndInUseWarning();
    await testWholeGroupLightbox();
    await testRenamedGroupKeepsMatchingSelection();
    await testLateIndexAndPageResponsesCannotCrossQueries();
    await testSnapshotRetryIsBoundedAndNeverExecutesPartialDelete();
    await testOneSnapshotChangeRecovers();
    await testRefreshDuringCollectionStopsWholeScopeAction();
    await testQueryChangeWhileInitialIndexIsLoading();
    console.log('lazy gallery network and scope regression tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
