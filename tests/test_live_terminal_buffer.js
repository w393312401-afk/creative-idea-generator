// 实况终端写入缓冲（app.js: appendLiveTerminal / flushLiveTerminal）的单测。
//
// 为什么值得单独测：compose 是流式的，一次激发会推来几千到上万条 text_chunk。
// 改造前每条 chunk 都直接 createTextNode + insertBefore，生成期间主线程被 DOM
// 写入占满，点按钮/切标签全都排在后面 —— 这是「UI 交互延迟高」的主因之一。
// 改成按帧合批 + 定长截断后，必须保证三件事不退化：
//   ① 一帧内来多少 chunk，只落一次 DOM；
//   ② 闪烁光标 <span class="terminal-cursor"> 不能被 textContent 覆写掉；
//   ③ 内容超过上限时从行首截断，不能把一行劈成半截。
//
// app.js 是浏览器脚本（不是模块），所以这里用 vm + 最小 DOM 桩把它整段跑起来，
// 再取出 sandbox 里的函数来测。DOMContentLoaded 只是登记回调、不会真跑，
// 因此 vm 里没有副作用。
//
// 跑法：node tests/test_live_terminal_buffer.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* ── 最小 DOM 桩 ──────────────────────────────────────────────────────── */

class StubNode {
    constructor(className = '') {
        this.className = className;
        this.children = [];
    }
}

class StubTerminal {
    constructor() {
        this._text = '';
        this.children = [];
        this.scrollTop = 0;
        this.writeCount = 0;   // textContent 被赋值的次数 = 落 DOM 的次数
    }
    get textContent() { return this._text; }
    set textContent(v) {
        this._text = String(v);
        this.children = [];    // 与真实 DOM 一致：赋值 textContent 会清空子节点
        this.writeCount++;
    }
    get scrollHeight() { return this._text.length; }
    querySelector(sel) {
        if (sel !== '.terminal-cursor') return null;
        return this.children.find(c => c.className === 'terminal-cursor') || null;
    }
    appendChild(node) { this.children.push(node); return node; }
    insertBefore(node) { this.children.push(node); return node; }
    set innerHTML(_v) { /* startLoadingTimer 会用它重置，这里不参与断言 */ }
}

const terminal = new StubTerminal();
const rafQueue = [];

const sandbox = {
    console,
    document: {
        getElementById: (id) => (id === 'live-terminal-body' ? terminal : null),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        createTextNode: (t) => ({ nodeValue: t }),
        body: { classList: { add() {}, remove() {}, toggle() {} } },
    },
    requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length; },
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

const { appendLiveTerminal, resetLiveTerminal } = sandbox;
// 顶层 `function` 声明会挂到 vm 的全局对象上，`const` 不会 —— 上限值只能求值取。
const LIVE_TERMINAL_MAX_CHARS = vm.runInContext('LIVE_TERMINAL_MAX_CHARS', sandbox);
assert.ok(Number.isFinite(LIVE_TERMINAL_MAX_CHARS) && LIVE_TERMINAL_MAX_CHARS > 0,
    'LIVE_TERMINAL_MAX_CHARS 必须是正数');
assert.strictEqual(typeof appendLiveTerminal, 'function', 'appendLiveTerminal 必须存在');
assert.strictEqual(typeof resetLiveTerminal, 'function', 'resetLiveTerminal 必须存在');

/** 把排队的 rAF 回调跑完（模拟浏览器绘制一帧） */
function drainFrame() {
    const pending = rafQueue.splice(0, rafQueue.length);
    pending.forEach(fn => fn());
}

/* ── ① 一帧内多条 chunk 只落一次 DOM ────────────────────────────────── */

terminal.writeCount = 0;
terminal.children = [new StubNode('terminal-cursor')];
resetLiveTerminal();

for (let i = 0; i < 500; i++) appendLiveTerminal(`tok${i} `);
assert.strictEqual(terminal.writeCount, 0, '未到下一帧时不应该碰 DOM');
assert.strictEqual(rafQueue.length, 1, '500 条 chunk 只排一个 rAF');

drainFrame();
assert.strictEqual(terminal.writeCount, 1, '500 条 chunk 合并成一次 DOM 写入');
assert.ok(terminal.textContent.startsWith('tok0 '), '首个 chunk 在最前');
assert.ok(terminal.textContent.endsWith('tok499 '), '末个 chunk 在最后');

/* ── ② 光标节点必须活下来 ──────────────────────────────────────────── */

assert.ok(terminal.querySelector('.terminal-cursor'),
    '闪烁光标不能被 textContent 赋值冲掉（它是"还在生成"的唯一视觉信号）');
assert.strictEqual(terminal.children.length, 1, '光标不应该被重复追加');

appendLiveTerminal('more\n');
drainFrame();
assert.ok(terminal.querySelector('.terminal-cursor'), '再次写入后光标仍在');
assert.strictEqual(terminal.children.length, 1, '每帧只保留一个光标');

/* ── ③ 超长内容按行首截断 ──────────────────────────────────────────── */

resetLiveTerminal();
terminal.children = [new StubNode('terminal-cursor')];

const line = 'x'.repeat(99) + '\n';               // 每行 100 字符
const lines = Math.ceil((LIVE_TERMINAL_MAX_CHARS * 2) / 100);
for (let i = 0; i < lines; i++) appendLiveTerminal(line);
drainFrame();

assert.ok(terminal.textContent.length <= LIVE_TERMINAL_MAX_CHARS,
    `终端内容必须裁到 ${LIVE_TERMINAL_MAX_CHARS} 以内，实际 ${terminal.textContent.length}`);
assert.strictEqual(terminal.textContent.split('\n')[0], 'x'.repeat(99),
    '截断必须落在行首，不能把一行劈成半截');

/* ── ④ 自动滚到底 ─────────────────────────────────────────────────── */

assert.strictEqual(terminal.scrollTop, terminal.scrollHeight, '落盘后应滚到底部');

/* ── ⑤ 空 chunk 不排帧 ────────────────────────────────────────────── */

rafQueue.length = 0;
appendLiveTerminal('');
appendLiveTerminal(undefined);
assert.strictEqual(rafQueue.length, 0, '空 chunk 不该白排一帧');

console.log('live terminal buffer tests passed');
