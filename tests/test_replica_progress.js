// 复刻页的进度条 + 证据帧灯箱。
//
// 这两块都是「不报错的静默失效」高发区，正是需要测试盯住的形态：
//   · SSE 帧带信封（{type,data}），读错一层不会报错，只是永远显示空白；
//   · 合成阶段的事件如果没人监听，页面就是一句话之后静止好几分钟。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(id) {
    return {
        id,
        style: {},
        textContent: '',
        innerHTML: '',
        dataset: {},
        children: [],
        addEventListener() {},
        querySelector(sel) { return this.children.find(c => `#${c.id}` === sel) || null; },
        querySelectorAll() { return []; },
    };
}

const progressBox = element('replica-progress');
progressBox.children = ['replica-progress-stage', 'replica-progress-label',
                        'replica-progress-percent', 'replica-progress-fill',
                        'replica-progress-log'].map(element);

const root = element('replica-root');
root.children = [progressBox];

const sandbox = {
    console,
    document: {
        readyState: 'complete',
        addEventListener() {},
        getElementById: (id) => (id === 'replica-root' ? root : null),
        querySelectorAll: () => [],
    },
    EventSource: function () {},
    setTimeout: () => 1,
    clearTimeout: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const file of ['progress_model.js', 'replica_pipeline.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', file), 'utf8'),
                    sandbox, { filename: file });
}

const call = (expr) => vm.runInContext(expr, sandbox);

// ── 1. SSE 信封 ───────────────────────────────────────────────────────────────
// server._open_sse_stream 发的是 {"type": ..., "data": ...}。读成 .stage/.message
// 恒为 undefined，页面照常渲染一个空 chip——不报任何错，所以只能靠测试盯住。
assert.equal(
    call(`replicaEventPayload({ data: JSON.stringify({ type: 'replica_stage', data: { stage: 'compose', message: '在跑' } }) }).message`),
    '在跑');
assert.equal(
    call(`replicaEventPayload({ data: JSON.stringify({ stage: 'compose', message: '老格式' }) }).message`),
    '老格式', '不带信封的历史事件也要认');
assert.equal(call('replicaEventPayload({ data: "不是 JSON" })'), null);
assert.equal(call('replicaEventPayload(null)'), null);

// ── 2. 合成阶段的进度 ─────────────────────────────────────────────────────────
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'compose', message: '正在按 9 拍阶梯合成提示词' })`);
const atStageStart = call('replicaProgress.percent');
assert.equal(atStageStart, 68, 'compose 段的起点');
assert.equal(progressBox.style.display, 'block');
assert.equal(progressBox.querySelector('#replica-progress-stage').textContent, '合成提示词');

// 规划节拍阶梯的那几轮（此前整段静默，最长 4×150s）。
call(`replicaHandleComposerEvent('outline', '正在规划节拍阶梯（目标 9 拍，第 1/4 轮）…')`);
const planning = call('replicaProgress.percent');
assert.ok(planning > atStageStart, '规划中要能看出进度在走');
assert.equal(call('replicaProgress.label'), '正在规划节拍阶梯（目标 9 拍，第 1/4 轮）…');

// 逐拍产出：唯一有真实分子/分母的信号。
call(`replicaHandleComposerEvent('beat_ready', { index: 5, total: 9 })`);
assert.equal(call('replicaProgress.label'), '已产出第 5/9 拍的提示词');
const midway = call('replicaProgress.percent');
assert.ok(midway > planning && midway < 94, `compose 段内推进，实际 ${midway}`);
assert.equal(progressBox.querySelector('#replica-progress-percent').textContent,
             `${Math.round(midway)}%`);

// 百分比只增不减：几路事件交替到达时来回跳的进度条比没有更让人不安。
call(`replicaHandleComposerEvent('batch', { current: 1, total: 9 })`);
assert.ok(call('replicaProgress.percent') >= midway, '进度不能倒退');

// 最近几条事件留在日志里，且不重复上一条。
call(`replicaHandleComposerEvent('beat_ready', { index: 6, total: 9 })`);
call(`replicaHandleComposerEvent('beat_ready', { index: 6, total: 9 })`);
const logHtml = progressBox.querySelector('#replica-progress-log').innerHTML;
assert.equal((logHtml.match(/已产出第 6\/9 拍的提示词/g) || []).length, 1);
assert.ok(logHtml.includes('已产出第 5/9 拍的提示词'));

// 阶段前进后区间跟着走。
call(`replicaHandleStageEvent({ stage: 'audit', message: '正在扫禁用元素' })`);
assert.ok(call('replicaProgress.percent') >= 94);

// ── 3. 证据帧原地开图 ─────────────────────────────────────────────────────────
const card = call(`replicaRenderBeatCard(
    { job_id: 'replica_x', overview: {} },
    { id: 'B01', start: 0, end: 5, stage: 'enclosure',
      evidence_frames: ['review_001.png', 'review_002.png'],
      package_operations: ['cut', 'fit'], persistent_traces: ['a', 'b'],
      zh: { visible_action: '工人封板' } },
    0)`);
assert.ok(!card.includes('target="_blank"'), '证据帧不再新开标签页');
assert.ok(card.includes('data-lightbox-beat="0"') && card.includes('data-lightbox-at="1"'));
assert.ok(card.includes('封板封闭'), '施工阶段用中文标签');
assert.ok(card.includes('工人封板'), '中文对照要显示在英文原文下面');
assert.ok(card.includes('data-key="package_operations"'), '工序包必须可编辑——报错就指着它');

// 灯箱不可用时退回新窗口：点了没反应比多一个标签页更糟。
let opened = null;
sandbox.window.open = (url) => { opened = url; };
call(`replicaOpenLightbox([{ url: '/outputs/a.png' }], 0)`);
assert.equal(opened, '/outputs/a.png');

let lightboxArgs = null;
sandbox.openLightbox = (items, index) => { lightboxArgs = { items, index }; };
call(`replicaOpenLightbox([{ url: '/a.png' }, { url: '/b.png' }], 5)`);
assert.equal(lightboxArgs.items.length, 2);
assert.equal(lightboxArgs.index, 1, '越界的下标要夹回最后一张');

console.log('test_replica_progress.js: all assertions passed');
