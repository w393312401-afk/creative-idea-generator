// 成品区（成片 + 精剪）布局逻辑单测：
//   ① 有成片时封面默认收起、没成片时展开，但只在用户没手动选择过时才自动处理，且不写存档；
//   ② 管线条「成片」芯片 / 下载主按钮滚向成品区；成片未生成（区块隐藏）时退回视频片段区；
//   ③ 封面芯片仍会把收起的左栏展开；成品区不受左栏收起影响。
//
// app.js 是浏览器脚本，这里只按函数名抠出相关片段放进 vm，配最小 DOM 桩。
//
// 跑法：node tests/test_result_final_layout.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function grab(startMarker, endMarker) {
    const a = appSource.indexOf(startMarker);
    assert.ok(a >= 0, `app.js lost: ${startMarker}`);
    const b = appSource.indexOf(endMarker, a);
    assert.ok(b > a, `app.js lost: ${endMarker}`);
    return appSource.slice(a, b);
}

const snippet = [
    grab('function scrollToPipelineSection', "const RESULT_LEFT_COLUMN_STORAGE_KEY"),
    grab("const RESULT_LEFT_COLUMN_STORAGE_KEY", 'function initMergedVideoSettingsButton'),
    grab('function storedResultLeftColumnPref', 'function initResultLeftColumnToggle'),
].join('\n');

function harness({ stored = null, mergedVisible = false } = {}) {
    const storage = new Map(stored === null ? [] : [['spark_result_left_column_collapsed', stored]]);
    const scrolled = [];
    const classes = new Set(['active']);
    const mk = id => ({ id, style: {}, scrollIntoView() { scrolled.push(id); } });
    const els = {
        'tab-panel-overview': { classList: {
            contains: c => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) } },
        'result-left-column-toggle': { setAttribute() {}, title: '', querySelector: () => ({ textContent: '' }) },
        'merged-video-container': mk('merged-video-container'),
        'videos-section': mk('videos-section'),
        'cover-section': mk('cover-section'),
    };
    els['merged-video-container'].style.display = mergedVisible ? 'block' : 'none';
    els['tab-panel-overview'].id = 'tab-panel-overview';
    const ctx = {
        document: { getElementById: id => els[id] },
        localStorage: { getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, v) },
        switchTab() {},
    };
    vm.createContext(ctx);
    vm.runInContext(snippet, ctx);
    return { ctx, storage, scrolled, collapsed: () => classes.has('is-left-column-collapsed') };
}

// ① 自动折叠：只在没有存档偏好时发生，不写存档，来回切换跟随
{
    const h = harness();
    h.ctx.syncResultLeftColumnForMerged(true);
    assert.strictEqual(h.collapsed(), true, 'merged video → cover collapses by default');
    h.ctx.syncResultLeftColumnForMerged(false);
    assert.strictEqual(h.collapsed(), false, 'no merged video → cover expands again');
    h.ctx.syncResultLeftColumnForMerged(true);
    assert.strictEqual(h.storage.size, 0, 'auto collapse must not persist');
}
// 用户明确选过（存档为 0 或 1）后，自动逻辑不再插手
for (const pref of ['0', '1']) {
    const h = harness({ stored: pref });
    h.ctx.syncResultLeftColumnForMerged(true);
    const afterTrue = h.collapsed();
    h.ctx.syncResultLeftColumnForMerged(false);
    assert.strictEqual(h.collapsed(), afterTrue, `stored pref ${pref} must be respected`);
}

// ② 导航落点
{
    const h = harness({ mergedVisible: true });
    h.ctx.scrollToPipelineSection('merged-video-container');
    assert.deepStrictEqual(h.scrolled, ['merged-video-container']);
}
{
    const h = harness({ mergedVisible: false });
    h.ctx.scrollToPipelineSection('merged-video-container');
    assert.deepStrictEqual(h.scrolled, ['videos-section'], 'hidden final block falls back to the video section');
}

// ③ 封面芯片会展开被收起的左栏（并记为用户选择）
{
    const h = harness();
    h.ctx.setResultLeftColumnCollapsed(true);
    h.ctx.scrollToPipelineSection('cover-section');
    assert.strictEqual(h.collapsed(), false);
    assert.strictEqual(h.storage.get('spark_result_left_column_collapsed'), '0');
}

// 静态结构：管线条的「成片」芯片指向成品区；成品区不在左栏里
assert.match(html, /id="merge-videos-btn"[^>]*data-section="merged-video-container"/);
const leftStart = html.indexOf('id="result-left-column"');
const finalAt = html.indexOf('id="merged-video-container"');
assert.ok(finalAt > 0 && finalAt < leftStart, 'final block sits before (outside) the left column');

console.log('result final layout tests passed');
