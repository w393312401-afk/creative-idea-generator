const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(fetch, clipboard = { writeText: async () => {} }) {
  const elements = new Map();
  function element(id) {
    const node = { id, textContent: '', hidden: false, scrollTop: 0, scrollHeight: 1000, clientHeight: 200,
      listeners: {}, attributes: {}, children: [],
      addEventListener(type, handler) { this.listeners[type] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
      appendChild(child) { this.children.push(child); child.parentElement = this; elements.set(child.id, child); },
      insertBefore(child) { this.appendChild(child); },
    };
    if (id) elements.set(id, node);
    return node;
  }
  const host = element('fx-log-view');
  host.parentElement = element('panel');
  const toolbar = element('toolbar');
  element('fx-log-refresh').parentElement = toolbar;
  element('fx-log-task');
  const context = { console, AbortController, URLSearchParams, setTimeout, clearTimeout, fetch,
    navigator: { clipboard },
    document: { getElementById: id => elements.get(id), createElement: () => element(''), addEventListener() {} },
  };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(__dirname, '../js/google_fx_console.js'), 'utf8')
    .replace('const apiObject = {', 'const apiObject = { reader: { state, loadLogs, setLogFilter, setupLogReader, syncCurrentLogTask },');
  vm.runInContext(source, context);
  const reader = context.GoogleFxConsole.reader;
  reader.setupLogReader();
  return { host, elements, reader };
}
const response = lines => ({ ok: true, json: async () => ({ lines }) });
const flush = async () => { for (let index = 0; index < 12; index++) await Promise.resolve(); };

test('FX updates preserve an older log view until the user returns to the latest', async () => {
  let lines = ['line 1', 'line 2'];
  const { host, elements, reader } = setup(async () => response(lines));
  await reader.loadLogs();
  assert.equal(host.scrollTop, 1000);
  host.scrollTop = 100;
  host.listeners.scroll();
  lines = ['line 2', 'line 3'];
  await reader.loadLogs();
  assert.equal(host.textContent, 'line 1\nline 2', 'the rolling tail must not remove the text being read');
  assert.equal(host.scrollTop, 100);
  assert.equal(elements.get('fx-log-latest').hidden, false);
  assert.match(elements.get('fx-log-latest').textContent, /有新日志/);
  elements.get('fx-log-latest').listeners.click();
  assert.equal(host.textContent, 'line 2\nline 3');
  assert.equal(host.scrollTop, 1000);
  assert.equal(elements.get('fx-log-latest').hidden, true);
});

test('late responses from another task cannot replace the selected task log', async () => {
  const pending = [];
  const { host, reader } = setup(() => new Promise(resolve => pending.push(resolve)));
  reader.setLogFilter('task-a');
  const first = reader.loadLogs();
  reader.setLogFilter('task-b');
  const second = reader.loadLogs();
  pending[1](response(['task-b line']));
  await second;
  pending[0](response(['task-a line']));
  await first;
  assert.equal(host.textContent, 'task-b line');
});

test('read failures retain existing logs and do not take over a manual task filter', async () => {
  let fail = false;
  const { host, elements, reader } = setup(async () => {
    if (fail) throw Error('offline');
    return response(['saved line']);
  });
  reader.setLogFilter('manual-task');
  await reader.loadLogs();
  reader.state.lastTasks = [{ id: 'new-task', status: 'running' }];
  reader.syncCurrentLogTask();
  assert.equal(reader.state.logTaskFilter, 'manual-task');
  fail = true;
  await reader.loadLogs();
  assert.equal(host.textContent, 'saved line');
  assert.match(elements.get('fx-log-context').textContent, /日志同步失败/);
});

test('severity filtering requests whole records and ignores older level responses', async () => {
  const pending = [];
  const { host, elements, reader } = setup(url => new Promise(resolve => pending.push({ url, resolve })));
  const initial = reader.loadLogs();
  const level = elements.get('fx-log-level');
  level.value = 'error'; level.listeners.change();
  assert.match(pending[1].url, /level=error/);
  pending[1].resolve({ ok: true, json: async () => ({ lines: ['<script>bad</script>', 'detail'],
    records: [{ level: 'error', lines: ['<script>bad</script>', 'detail'] }] }) });
  await flush();
  pending[0].resolve(response(['old info'])); await initial;
  assert.match(host.innerHTML, /data-level="error"/);
  assert.match(host.innerHTML, /&lt;script&gt;bad&lt;\/script&gt;\ndetail/);
  assert.doesNotMatch(host.innerHTML, /old info|<script>/);
  assert.match(elements.get('fx-log-context').textContent, /仅错误 · 1 条记录/);
});

test('explicit pause freezes short logs and auto task switching until resume', async () => {
  const requests = [];
  const { host, elements, reader } = setup(async url => { requests.push(url); return response(['new lines']); });
  reader.setLogFilter('old-task', true); await reader.loadLogs();
  elements.get('fx-log-pause').listeners.click();
  host.scrollTop = host.scrollHeight; host.listeners.scroll();
  assert.equal(reader.state.logFollow, false, 'manual pause survives a scroll event at the bottom');
  reader.state.lastTasks = [{ id: 'new-task', status: 'running' }];
  reader.syncCurrentLogTask();
  assert.equal(reader.state.logTaskFilter, 'old-task');
  assert.equal(requests.length, 1);
  elements.get('fx-log-latest').listeners.click(); await flush();
  assert.equal(reader.state.logTaskFilter, 'new-task');
  assert.match(requests[1], /task_id=new-task/);
  assert.equal(reader.state.logFollow, true);
  assert.equal(reader.state.logPaused, false);
});

test('copy uses the paused visible result and resume applies pending severity together', async () => {
  let records = [{ level: 'warning', lines: ['\x1b[33mfirst warning\x1b[0m', 'warning detail'] }];
  const copied = [];
  const { host, elements, reader } = setup(async () => ({ ok: true,
    json: async () => ({ lines: records.flatMap(record => record.lines), records }) }),
    { writeText: async text => copied.push(text) });
  await reader.loadLogs();
  assert.doesNotMatch(host.innerHTML, /\x1b|\[33m/);
  elements.get('fx-log-pause').listeners.click();
  const original = host.innerHTML;
  records = [{ level: 'error', lines: ['new error'] }];
  await reader.loadLogs();
  assert.equal(host.innerHTML, original);
  await elements.get('fx-log-copy').listeners.click();
  assert.deepEqual(copied, ['first warning\nwarning detail']);
  elements.get('fx-log-pause').listeners.click();
  assert.match(host.innerHTML, /data-level="error">new error/);
  assert.doesNotMatch(host.innerHTML, /first warning/);
});

test('auto task updates do not overwrite an unsubmitted task filter draft', async () => {
  const requests = [];
  const { elements, reader } = setup(async url => { requests.push(url); return response(['old log']); });
  reader.setLogFilter('old-task', true); await reader.loadLogs();
  elements.get('fx-log-task').value = 'typing-manual-task';
  reader.state.lastTasks = [{ id: 'new-task', status: 'running' }];
  reader.syncCurrentLogTask();
  assert.equal(elements.get('fx-log-task').value, 'typing-manual-task');
  assert.equal(reader.state.logTaskFilter, 'old-task');
  assert.equal(requests.length, 1);
});
