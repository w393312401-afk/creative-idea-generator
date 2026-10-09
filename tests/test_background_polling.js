const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const pipeline = fs.readFileSync(path.join(__dirname, '../js/prompt_pipeline.js'), 'utf8');
const projects = fs.readFileSync(path.join(__dirname, '../js/projects.js'), 'utf8');
const section = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const tick = () => new Promise(resolve => setImmediate(resolve));

function clock() {
  const timers = new Map();
  let id = 0;
  return {
    timers,
    setTimeout(fn, ms) { const key = ++id; timers.set(key, { fn, ms }); return key; },
    clearTimeout(key) { timers.delete(key); },
    fire(ms) {
      const entry = [...timers].find(([, timer]) => timer.ms === ms);
      assert(entry, `expected a ${ms} ms timer`);
      timers.delete(entry[0]);
      return entry[1].fn();
    },
    count(ms) { return [...timers.values()].filter(timer => timer.ms === ms).length; },
  };
}

function badgeFixture(hidden = false) {
  const scheduler = clock();
  const requests = [], events = [], warnings = [];
  const listeners = new Set();
  const badge = { textContent: '', style: {} };
  const context = {
    AbortController,
    console: { warn: (...args) => warnings.push(args) },
    document: {
      hidden,
      getElementById: () => badge,
      addEventListener(type, fn) { assert.equal(type, 'visibilitychange'); listeners.add(fn); },
      removeEventListener(_type, fn) { listeners.delete(fn); },
    },
    window: { dispatchEvent: event => events.push(event) },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    setTimeout: scheduler.setTimeout, clearTimeout: scheduler.clearTimeout,
    fetch: (url, init) => new Promise((resolve, reject) => {
      requests.push({ url, init, reject,
        reply(data, ok = true) { resolve({ ok, status: ok ? 200 : 500, json: async () => data }); },
      });
    }),
  };
  vm.createContext(context);
  vm.runInContext(section(pipeline, 'function updateTasksBadge(', 'function renderRepairBanner(') +
    section(app, 'const IDEATION_TASK_TYPES', 'async function viewTask('), context);
  return { context, scheduler, requests, events, badge, warnings, listeners,
    visibility(value) { context.document.hidden = value; [...listeners].forEach(fn => fn()); },
  };
}

test('summary polling uses exact counts beyond recent snapshots and single inflight', async () => {
  const s = badgeFixture();
  await s.context.startGlobalTasksBadgePolling();
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].url, '/api/tasks/summary');
  assert.equal(s.requests[0].init.cache, 'no-store');
  s.visibility(false); s.visibility(false);
  assert.equal(s.requests.length, 1, 'visibility signals cannot overlap a pending request');
  s.requests[0].reply({ tasks: [{ id: 'recent', status: 'completed', dimensions: { type: 'idea' } }],
    counts: { running: 120, ideation_running: 105 }, total_count: 500 });
  await tick();
  assert.equal(s.badge.textContent, 105, 'older running tasks still count');
  assert.equal(s.badge.style.display, 'flex');
  assert.equal(s.events.length, 1);
  assert.equal(s.events[0].detail.tasks[0].id, 'recent');
  assert.equal(s.scheduler.count(5000), 1);
  assert.equal(s.scheduler.count(15000), 0);
  s.scheduler.fire(5000);
  assert.equal(s.requests.length, 2);
});

test('hidden cancels ordinary requests, foreground refresh ignores aborted late payloads', async () => {
  const s = badgeFixture();
  await s.context.startGlobalTasksBadgePolling();
  s.visibility(true);
  assert.equal(s.requests[0].init.signal.aborted, true);
  assert.equal(s.scheduler.timers.size, 0);
  s.visibility(false);
  assert.equal(s.requests.length, 2, 'foreground starts immediately');
  s.requests[0].reply({ tasks: [], counts: { running: 99, ideation_running: 99 } });
  await tick();
  assert.equal(s.events.length, 0, 'late cancelled payload must not update UI');
  s.requests[1].reply({ tasks: [], counts: { running: 0, ideation_running: 0 } });
  await tick();
  assert.equal(s.events.length, 1);
  assert.equal(s.badge.style.display, 'none');
  assert.equal(s.scheduler.count(30000), 1);
  s.visibility(true);
  assert.equal(s.scheduler.timers.size, 0, 'hidden idle page has no scheduled poll');
});

test('timeouts and failures release guards and do not accept stale results', async () => {
  const s = badgeFixture();
  await s.context.startGlobalTasksBadgePolling();
  s.scheduler.fire(15000);
  assert.equal(s.requests[0].init.signal.aborted, true);
  assert.equal(s.scheduler.count(30000), 1);
  s.scheduler.fire(30000);
  assert.equal(s.requests.length, 2);
  s.requests[0].reply({ tasks: [], counts: { running: 3, ideation_running: 3 } });
  s.requests[1].reply({}, false);
  await tick();
  assert.equal(s.events.length, 0);
  assert.equal(s.warnings.length, 1);
  assert.equal(s.scheduler.count(30000), 1);
  s.scheduler.fire(30000);
  s.requests[2].reject(new Error('offline'));
  await tick();
  assert.equal(s.scheduler.count(30000), 1, 'network error can retry');
  assert.equal(s.scheduler.count(15000), 0);
});

test('restart replaces controller and listener; hidden startup makes no requests', async () => {
  const s = badgeFixture(true);
  await s.context.startGlobalTasksBadgePolling();
  assert.equal(s.requests.length, 0);
  s.visibility(false);
  assert.equal(s.requests.length, 1);
  await s.context.startGlobalTasksBadgePolling();
  assert.equal(s.requests.length, 2);
  assert.equal(s.listeners.size, 1);
  assert.equal(s.requests[0].init.signal.aborted, true);
  s.requests[0].reply({ tasks: [], counts: { ideation_running: 10 } });
  s.requests[1].reply({ tasks: [], counts: { running: 0, ideation_running: 0 } });
  await tick();
  assert.equal(s.events.length, 1);
  assert.equal(s.scheduler.count(30000), 1);
});

test('project polling pauses while hidden and restores only the active workbench', async () => {
  const scheduler = clock();
  const calls = [];
  const context = { document: { hidden: false }, projectsTabActive: false, projectsPollTimer: null,
    projectsRows: [{ state: 'running' }], projectsIsRunning: p => p.state === 'running',
    setTimeout: scheduler.setTimeout, clearTimeout: scheduler.clearTimeout,
    refreshProjects: async options => { calls.push(options); },
  };
  vm.createContext(context);
  vm.runInContext(section(projects, 'function projectsTabEntered()', 'async function refreshProjects('), context);
  context.projectsTabEntered();
  assert.equal(calls.length, 1);
  assert.equal(scheduler.count(4000), 1);
  scheduler.fire(4000); await tick();
  assert.equal(calls[1].assets, false);
  context.document.hidden = true;
  context.projectsVisibilityChanged();
  assert.equal(scheduler.timers.size, 0);
  context.projectsSchedulePoll();
  assert.equal(scheduler.timers.size, 0);
  context.document.hidden = false;
  context.projectsRows = [];
  context.projectsVisibilityChanged(); await tick();
  assert.equal(calls.length, 3);
  assert.equal(scheduler.count(30000), 1);
  context.projectsTabLeft();
  context.document.hidden = true; context.projectsVisibilityChanged();
  context.document.hidden = false; context.projectsVisibilityChanged();
  assert.equal(calls.length, 3, 'inactive workbench stays paused');
  assert.equal(scheduler.timers.size, 0);
});
