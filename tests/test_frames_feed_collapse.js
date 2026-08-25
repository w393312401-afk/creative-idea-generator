// 帧序列实时动态的「就地刷新行」（app.js: framesFeedLine 的 key 参数）单测。
//
// 为什么值得单独测：一致性审查是逐拍并发跑的，每审完一拍回报一次。改造前这条
// 回报一拍灌一行，十几拍下来把真正要读的结论（哪几帧有问题、哪几帧没审完）顶出
// 可视区——而"干净"的那十几行没有一行值得单独占位。现在审干净的拍收进一条带 key
// 的计数行就地刷新，有发现的拍照旧各占一行。必须保证三件事：
//   ① 同 key 连续写只占一行，且内容是最后一次的；
//   ② 缓冲区（切走再切回来的回放源）与 DOM 的行数一致——否则回放会把折叠过的
//      进度重新摊开；
//   ③ 中间插进一条无 key 的行之后，同 key 的下一条要重新开一行，不能回头改写
//      已经被别的内容顶上去的那一行。
//
// app.js 是浏览器脚本（不是模块），用 vm + 最小 DOM 桩整段跑起来，再取出函数测。
//
// 跑法：node tests/test_frames_feed_collapse.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* ── 最小 DOM 桩：只实现 feed 那两个容器用到的部分 ────────────────────── */

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

// app.js 依赖的这几个来自别的脚本（js/api_client.js、js/utils.js）：在这里用桩顶上，
// 只关心 feed 这一段的行为
const rec = { feedLines: [], live: true };
sandbox.getIdeaTaskRecord = () => rec;
sandbox.isViewingIdea = () => true;
sandbox.escapeHtml = (s) => String(s);

const { framesFeedLine, framesFeedHydrate } = sandbox;
assert.strictEqual(typeof framesFeedLine, 'function', 'framesFeedLine 必须存在');

const text = (el) => String(el.innerHTML);
const domCount = () => feedLines.children.length;

/* ── ① 同 key 连续写只占一行 ─────────────────────────────────────────── */

for (let i = 1; i <= 12; i++) {
    framesFeedLine('idea-1', `逐拍审查 ${i}/12 拍`, 'ok', 'review-beat');
}
assert.strictEqual(domCount(), 1, '12 拍的进度只该占一行');
assert.strictEqual(rec.feedLines.length, 1, '缓冲区同样只留一条');
assert.ok(text(feedLines.children[0]).includes('12/12'), '留下的是最后一次的内容');

/* ── ② 无 key 的行照旧各占一行 ───────────────────────────────────────── */

framesFeedLine('idea-1', '　IMG 004：塔吊消失', 'warn');
framesFeedLine('idea-1', '　IMG 007：地面材质突变', 'warn');
assert.strictEqual(domCount(), 3, '有发现的拍必须各自留一行，不能被折叠掉');
assert.strictEqual(rec.feedLines.length, 3);

/* ── ③ 被顶上去之后，同 key 要重新开一行 ─────────────────────────────── */

framesFeedLine('idea-1', '逐拍审查 13/13 拍', 'ok', 'review-beat');
assert.strictEqual(domCount(), 4,
    '中间隔了别的行之后不能回头改写旧的那一行——那行已经不在末尾了');
assert.ok(text(feedLines.children[3]).includes('13/13'));

/* ── ④ 回放（切走再切回来）与实时渲染行数一致 ─────────────────────────── */

const beforeReplay = feedLines.children.map(text);
feedLines.children = [];
framesFeedHydrate('idea-1');
assert.strictEqual(domCount(), beforeReplay.length,
    '回放不能把折叠过的进度重新摊开');
assert.deepStrictEqual(feedLines.children.map(text), beforeReplay,
    '回放出来的每一行内容必须与实时渲染时一致');

/* ── ⑤ 上限截断仍然生效 ─────────────────────────────────────────────── */

for (let i = 0; i < 400; i++) framesFeedLine('idea-1', `line ${i}`);
assert.ok(domCount() <= 300, `DOM 行数必须裁到 300 以内，实际 ${domCount()}`);
assert.ok(rec.feedLines.length <= 300, '缓冲区同样有上限');

console.log('frames feed collapse tests passed');
