const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const semantics = require('../js/log_semantics.js');
const source = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');

function setup(idea = null, options = {}) {
  const nodes = new Map();
  function element(id = '') {
    const classes = new Set();
    const node = { id, children: [], dataset: {}, style: { setProperty() {} },
      attributes: {}, listeners: {}, textContent: '', hidden: false,
      classList: { contains: value => classes.has(value), add: value => classes.add(value), remove: value => classes.delete(value),
        toggle(value, force) { const on = force === undefined ? !classes.has(value) : force; if (on) classes.add(value); else classes.delete(value); return on; } },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      setAttribute(name, value) { this.attributes[name] = value; },
      appendChild(child) { this.children.push(child); nodes.set(child.id, child); return child; },
      replaceChildren(...children) { this.children = children; },
      querySelectorAll() { return []; },
      get childElementCount() { return this.children.length; },
    };
    Object.defineProperty(node, 'innerHTML', { set(value) { this.html = value; this.children = []; }, get() { return this.html || ''; } });
    if (id) nodes.set(id, node);
    return node;
  }
  ['log-panel-drawer', 'log-panel-header', 'toggle-log-btn', 'clear-log-btn', 'log-output-lines',
    'log-pill', 'log-pill-badge', 'log-mode-tabs', 'log-event-list', 'log-headline', 'log-panel-foot'].forEach(element);
  const dots = [element(), element()];
  const listeners = {};
  const documentListeners = {};
  const streams = [];
  const timers = new Map();
  let timerId = 0;
  const context = { console, SparkLogSemantics: semantics, currentIdea: idea, ideaTasksById: {}, innerWidth: 1280,
    localStorage: { getItem: key => (options.saved || {})[key] ?? null, setItem() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '420px' }),
    document: { getElementById: id => nodes.get(id), querySelectorAll: () => dots, createElement: () => element(),
      createDocumentFragment: () => element(), hidden: !!options.hidden,
      addEventListener(type, fn) { documentListeners[type] = fn; }, body: element(), documentElement: element() },
    addEventListener(type, fn) { listeners[type] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); }, requestAnimationFrame() {}, escapeHtml: value => String(value),
    EventSource: class {
      constructor(url) { this.url = url; this.handlers = {}; this.closed = false; streams.push(this); }
      addEventListener(type, fn) { this.handlers[type] = fn; }
      close() { this.closed = true; }
      fire(type, payload = {}) { this.handlers[type]?.(payload); }
    },
  };
  context.window = context;
  vm.createContext(context);
  const start = source.indexOf('const _LOG_LINE_RE');
  const end = source.indexOf('// 帧/视频序列渲染都有串行锁');
  const code = source.slice(start, end).replace('    syncLogStream();\n}',
    '    syncLogStream(); globalThis.dockTest = { appendLine, renderOverview, setConnected, applyDockWidth, setLogScope, setLogDockOpen, entries };\n}');
  vm.runInContext(code + '\ninitLocalServiceLogs();', context);
  return { context, nodes, dots, listeners, documentListeners, streams, timers, dock: context.dockTest };
}
const error = (task, index) => `12:00:00.000 [ERROR] [TASK] [task=${task}] video_error index=${index} current=${index} total=47 message=warning`;

test('unread badge counts unresolved slot events after folding and task snapshots', () => {
  const { dock, nodes, listeners } = setup();
  dock.setConnected(true);
  dock.appendLine(error('v1', 44));
  dock.appendLine(error('v1', 45));
  dock.appendLine(error('v1', 45));
  assert.equal(dock.entries.length, 2, 'consecutive failures of different slots must not fold together');
  dock.renderOverview();
  assert.equal(nodes.get('log-pill-badge').textContent, '2');
  dock.appendLine('12:00:01.000 [INFO] [TASK] [task=v1] video_done index=44 current=44 total=47');
  dock.renderOverview();
  assert.equal(nodes.get('log-pill-badge').textContent, '1');
  listeners['spark:tasks-updated']({ detail: { tasks: [{ id: 'v1', status: 'completed', outcome: 'completed' }] } });
  dock.renderOverview();
  assert.equal(nodes.get('log-pill-badge').hidden, true);
});

test('current project is the default scope with an explicit all-logs escape', () => {
  const { dock, nodes, listeners } = setup({ id: 'idea', project_key: 'one' });
  listeners['spark:tasks-updated']({ detail: { tasks: [
    { id: 'v1', status: 'running', dimensions: { project_key: 'one' } },
    { id: 'v2', status: 'failed', dimensions: { project_key: 'two' } },
  ] } });
  dock.setConnected(true);
  dock.appendLine(error('v1', 1));
  dock.appendLine(error('v2', 2));
  dock.renderOverview();
  assert.equal(nodes.get('log-event-list').children.length, 1);
  assert.match(nodes.get('log-panel-foot').textContent, /当前项目/);
  dock.setLogScope('all');
  assert.equal(nodes.get('log-event-list').children.length, 2);
});

test('disconnected log state survives overview rendering and small screens overlay', () => {
  const { dock, nodes, context } = setup(null, { saved: { spark_log_dock_open: '1' } });
  dock.setConnected(false);
  dock.renderOverview();
  assert.match(nodes.get('log-headline').textContent, /日志同步中断/);
  assert.equal(nodes.get('log-pill').dataset.connected, 'false');
  dock.applyDockWidth(420);
  assert.equal(context.document.body.classList.contains('log-docked'), false);
  context.innerWidth = 1600;
  dock.applyDockWidth(420);
  assert.equal(context.document.body.classList.contains('log-docked'), true);
});

test('collapsed and hidden logs pause traffic, foreground resumes with history', () => {
  const s = setup();
  assert.equal(s.streams.length, 0, 'folded startup must not download logs');
  assert.match(s.nodes.get('log-headline').textContent, /日志同步已暂停/);
  s.dock.setLogDockOpen(true);
  assert.equal(s.streams.length, 1);
  const first = s.streams[0];
  first.fire('open');
  first.fire('history', { data: JSON.stringify({ data: { lines: [error('v1', 4)] } }) });
  assert.equal(s.dock.entries.length, 1);
  s.context.document.hidden = true;
  s.documentListeners.visibilitychange();
  assert.equal(first.closed, true);
  s.dock.renderOverview();
  assert.match(s.nodes.get('log-headline').textContent, /日志同步已暂停/);
  first.fire('error');
  assert.equal([...s.timers.values()].filter(timer => timer.ms === 3000).length, 0);
  s.context.document.hidden = false;
  s.documentListeners.visibilitychange();
  assert.equal(s.streams.length, 2);
  const second = s.streams[1];
  first.fire('open'); first.fire('log', { data: JSON.stringify({ text: error('old', 5) + '\n' }) });
  first.fire('history', { data: JSON.stringify({ lines: [error('old', 6)] }) }); first.fire('error');
  assert.equal(second.closed, false, 'retired stream cannot close a new stream');
  assert.equal(s.dock.entries.length, 1, 'retired stream cannot change history');
  second.fire('open');
  second.fire('history', { data: JSON.stringify({ lines: [error('v2', 7)] }) });
  assert.equal(s.dock.entries[0].task, 'v2');
  s.dock.setLogDockOpen(false);
  assert.equal(second.closed, true);
  s.documentListeners.visibilitychange();
  assert.equal(s.streams.length, 2, 'foreground folded drawer remains paused');
});

test('closing logs cancels retry and saved detail-open state connects safely', () => {
  const s = setup(null, { saved: { spark_log_dock_open: '1', spark_log_dock_mode: 'detail' } });
  assert.equal(s.streams.length, 1, 'saved detail-open startup must avoid temporal dead zone');
  const stream = s.streams[0];
  stream.fire('error');
  assert.equal(stream.closed, true);
  assert.equal([...s.timers.values()].filter(timer => timer.ms === 3000).length, 1);
  stream.fire('error');
  assert.equal([...s.timers.values()].filter(timer => timer.ms === 3000).length, 1, 'duplicate error cannot stack retries');
  s.dock.setLogDockOpen(false);
  assert.equal([...s.timers.values()].filter(timer => timer.ms === 3000).length, 0);
  s.dock.setLogDockOpen(true);
  assert.equal(s.streams.length, 2);
});

test('preview remains available while generation buttons are busy', () => {
  const preview = { dataset: { act: 'preview-slot', idleTitle: '播放' } };
  const generate = { dataset: { act: 'generate' } };
  const context = { document: { getElementById: () => null }, slotRenderTarget: () => ({ querySelectorAll: () => [preview, generate] }) };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('const SLOT_BUSY_TIP'), source.indexOf('function setFrameGridButtonsBusy')), context);
  context.setSlotGridButtonsBusy('video', true);
  assert.equal(preview.disabled, false);
  assert.equal(generate.disabled, true);
});
