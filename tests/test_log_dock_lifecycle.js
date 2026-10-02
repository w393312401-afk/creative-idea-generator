const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const semantics = require('../js/log_semantics.js');
const source = fs.readFileSync(path.join(__dirname, '../js/api_client.js'), 'utf8');

function setup(idea = null) {
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
  const context = { console, SparkLogSemantics: semantics, currentIdea: idea, ideaTasksById: {}, innerWidth: 1280,
    localStorage: { getItem: () => null, setItem() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '420px' }),
    document: { getElementById: id => nodes.get(id), querySelectorAll: () => dots, createElement: () => element(),
      createDocumentFragment: () => element(), addEventListener() {}, body: element(), documentElement: element() },
    addEventListener(type, fn) { listeners[type] = fn; },
    setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame() {}, escapeHtml: value => String(value),
    EventSource: class { addEventListener() {} close() {} },
  };
  context.window = context;
  vm.createContext(context);
  const start = source.indexOf('const _LOG_LINE_RE');
  const end = source.indexOf('// 帧/视频序列渲染都有串行锁');
  const code = source.slice(start, end).replace('    connectLogStream();\n}',
    '    connectLogStream(); globalThis.dockTest = { appendLine, renderOverview, setConnected, applyDockWidth, setLogScope, entries };\n}');
  vm.runInContext(code + '\ninitLocalServiceLogs();', context);
  return { context, nodes, dots, listeners, dock: context.dockTest };
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
  const { dock, nodes, context } = setup();
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
