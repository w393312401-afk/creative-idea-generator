// 链上守卫结论上屏的单测（app.js: framesFeedQualityLine 的 guardPending 分支）。
//
// 为什么值得单独测：'frame' 事件里的 quality_gate 是守卫**跑之前**的读数——4选1 线
// 恒为 auto_approved（那是候选优选的结论，不是一致性审查的结论），其余线是初始态
// pending_manual_review。改造前前端拿它直接打"完成（质检通过）"，等于在守卫开口之前
// 替它抢答；几十秒后守卫判废、manifest 落 flag，卡片下一次重画突然变红，用户看到的
// 就是"当时生成好好的没跳问题，继续生成后面几帧时又回头说这帧有问题"。
//
// 必须保证三件事：
//   ① 还有守卫要审时，这一行只说"已渲染 + 审查中"，绝不出现"质检通过"；
//   ② 4选1 的优选理由仍然留痕（那句话本身有用，只是不能当质检结论）；
//   ③ 守卫关掉/已出结论时，原有话术一字不改（通过是通过、判废是判废）。
//
// app.js 是浏览器脚本（不是模块），用 vm + 最小 DOM 桩整段跑起来，再取出函数测。
//
// 跑法：node tests/test_chain_guard_feed.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class StubEl {
    constructor(id) {
        this.id = id;
        this.children = [];
        this.style = {};
        this.dataset = {};
        this.className = '';
        this.innerHTML = '';
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this.classList = { add() {}, remove() {}, toggle() {} };
    }
    get lastElementChild() { return this.children[this.children.length - 1] || null; }
    get firstChild() { return this.children[0] || null; }
    appendChild(node) { this.children.push(node); return node; }
    removeChild(node) {
        const i = this.children.indexOf(node);
        if (i >= 0) this.children.splice(i, 1);
        return node;
    }
}

const feedLines = new StubEl('frames-live-feed-lines');
const feedWrap = new StubEl('frames-live-feed');
const els = { 'frames-live-feed-lines': feedLines, 'frames-live-feed': feedWrap };

const sandbox = {
    console,
    document: {
        getElementById: (id) => els[id] || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        createElement: () => new StubEl(''),
        createTextNode: (t) => ({ nodeValue: t }),
        body: { classList: { add() {}, remove() {}, toggle() {} } },
    },
    requestAnimationFrame: () => 1,
    setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: () => Promise.reject(new Error('no network in tests')),
    AbortController,
    URLSearchParams,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8'),
    sandbox,
    { filename: 'app.js' }
);

const rec = { feedLines: [], live: true };
sandbox.getIdeaTaskRecord = () => rec;
sandbox.isViewingIdea = () => true;
sandbox.escapeHtml = (s) => String(s);

const { framesFeedQualityLine } = sandbox;
assert.strictEqual(typeof framesFeedQualityLine, 'function', 'framesFeedQualityLine 必须存在');

const lastLine = () => rec.feedLines[rec.feedLines.length - 1] || { text: '', cls: '' };

/* ── ① 守卫还没开口时不许替它答"质检通过" ────────────────────────────── */

framesFeedQualityLine('idea-1', {
    sequence: 4,
    quality_gate: 'auto_approved',
    vlm_qa_reason: 'AI 4选1 鉴别优选 (候选 #3): 构图对标原片',
}, false, true);
let line = lastLine();
assert.ok(!line.text.includes('质检通过'),
    `守卫未出结论时不能说"质检通过"，实际：${line.text}`);
assert.ok(line.text.includes('审查中'), `应当明说还在审查，实际：${line.text}`);
assert.ok(line.text.includes('IMG 004'), '帧号要能对上');

/* ── ② 4选1 的优选理由仍然留痕 ───────────────────────────────────────── */

assert.ok(line.text.includes('AI 4选1 鉴别优选 (候选 #3)'),
    `优选理由不该被这次改造弄丢，实际：${line.text}`);

/* 初始态那条同理（非 4选1 线的 pending_manual_review） */
framesFeedQualityLine('idea-2', { sequence: 5, quality_gate: 'pending_manual_review' }, false, true);
line = lastLine();
assert.ok(line.text.includes('审查中') && !line.text.includes('渲染不做审查'),
    `守卫开着的时候不能说"渲染不做审查"，实际：${line.text}`);

/* ── ③ 守卫关掉 / 已出结论时，原有话术一字不改 ───────────────────────── */

framesFeedQualityLine('idea-3', {
    sequence: 6,
    quality_gate: 'auto_approved',
    vlm_qa_reason: 'AI 4选1 鉴别优选 (候选 #1): ok',
}, false, false);
line = lastLine();
assert.ok(line.text.includes('质检通过'), `守卫关掉时保持原话术，实际：${line.text}`);
assert.strictEqual(line.cls, 'ok');

framesFeedQualityLine('idea-4', {
    sequence: 7,
    quality_gate: 'sequence_review_flagged',
    vlm_qa_reason: '机位俯仰角度过高',
}, false, true);
line = lastLine();
assert.ok(line.text.includes('一致性审查未通过'),
    `已经判废的帧必须照旧报红，不能被 guardPending 吞掉，实际：${line.text}`);
assert.strictEqual(line.cls, 'err');
assert.ok(line.text.includes('机位俯仰角度过高'), '判废原因要带出来');

/* ── ④ 后端推的每一个 chain_guard_* 事件，前端都必须有分支接住 ───────────
   事件链没有 else 兜底：漏接的事件既不报错也不上屏，就这么静默丢了（守卫的
   逐拍结论此前整整一段时间一个字都没上过屏，正是这么丢的）。新增 halt 之外
   的档位时尤其容易漏——软档只有事件这一条声音，接不住等于没做。            */

const guardEmitters = ['chain_guard.py', 'frame_generator.py', 'candidate_selection_pipeline.py'];
const emitted = new Set();
guardEmitters.forEach((name) => {
    const src = fs.readFileSync(path.join(__dirname, '..', name), 'utf8');
    const re = /on_progress\(\s*'(chain_guard_[a-z_]+)'/g;
    let m;
    while ((m = re.exec(src)) !== null) emitted.add(m[1]);
});
assert.ok(emitted.size >= 5, `应当扫到后端推的守卫事件，实际只有 ${emitted.size} 个`);
assert.ok(emitted.has('chain_guard_soft_continue'), '软档事件必须由后端推出来');

const appSrc = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
emitted.forEach((evt) => {
    assert.ok(appSrc.includes(`'${evt}'`),
        `app.js 没有接住 ${evt} 事件——事件链没有 else 兜底，漏接就是静默丢弃`);
});

console.log('chain guard feed tests passed');
