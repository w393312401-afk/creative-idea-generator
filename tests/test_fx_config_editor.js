const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// A small form DOM models native input/select defaults, including missing options.
function setup() {
  const ids = new Map();
  const decode = value => String(value).replaceAll('&quot;', '"').replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
  class Element {
    constructor(tag = 'div', attrs = {}) {
      this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.listeners = {};
      this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith('data-'))
        .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value]));
      this.id = attrs.id || ''; this.checked = Object.hasOwn(attrs, 'checked');
      this.open = Object.hasOwn(attrs, 'open'); this.hidden = Object.hasOwn(attrs, 'hidden');
      this.disabled = Object.hasOwn(attrs, 'disabled'); this.textContent = ''; this.classList = { toggle() {} };
      this._value = attrs.value; this._html = '';
      if (this.id) ids.set(this.id, this);
    }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    setAttribute(name, value) { this.attrs[name] = value; }
    get value() {
      if (this.tagName === 'SELECT' && this._value === undefined) {
        const options = this.descendants().filter(node => node.tagName === 'OPTION');
        return (options.find(node => Object.hasOwn(node.attrs, 'selected')) || options[0])?.value ?? '';
      }
      return this._value ?? '';
    }
    set value(value) { this._value = String(value); }
    get innerHTML() { return this._html; }
    set innerHTML(html) {
      this._html = html; this.children = []; this._value = this.attrs.value;
      const stack = [this];
      for (const token of html.match(/<[^>]+>/g) || []) {
        if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
        const tag = token.match(/^<([a-z][\w-]*)/i)?.[1];
        if (!tag) continue;
        const attrs = {};
        const attrText = token.slice(tag.length + 1, -1);
        for (const attr of attrText.matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)')?/g)) {
          attrs[attr[1]] = decode(attr[2] ?? attr[3] ?? '');
        }
        const child = new Element(tag, attrs);
        child.parentElement = stack.at(-1); stack.at(-1).children.push(child);
        if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(tag)) stack.push(child);
      }
    }
    descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
    matches(selector) {
      if (selector === '[data-config-key]') return this.dataset.configKey !== undefined;
      if (selector === '[data-config-type="account_list"]') return this.dataset.configType === 'account_list';
      if (selector === 'select[data-config-account]') return this.tagName === 'SELECT' && this.dataset.configAccount !== undefined;
      if (selector === 'input[data-config-account-item]:checked') return this.tagName === 'INPUT' && this.dataset.configAccountItem !== undefined && this.checked;
      if (selector.startsWith('.')) return (this.attrs.class || '').split(' ').includes(selector.slice(1));
      return false;
    }
    querySelectorAll(selector) { return this.descendants().filter(node => node.matches(selector)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    contains(node) { return this === node || this.descendants().includes(node); }
    reportValidity() { return true; }
    scrollIntoView() { this.scrolled = true; }
    focus() { document.activeElement = this; }
  }
  const form = new Element('div', { id: 'fx-config-form' });
  for (const id of ['fx-config-save', 'fx-config-reload', 'fx-config-state', 'fx-config-note', 'fx-config-tab-indicator', 'fx-toast']) new Element('div', { id });
  for (const section of ['monitor', 'accounts', 'maintenance']) {
    new Element('section', { id: `fx-section-${section}`, class: 'fx-console-section' });
    new Element('button', { id: `fx-tab-${section}`, class: 'fx-section-tab', 'data-fx-section': section });
  }
  const document = {
    activeElement: null,
    addEventListener() {}, getElementById: id => ids.get(id) || null,
    querySelectorAll(selector) {
      if (selector.startsWith('#fx-config-form ')) return form.querySelectorAll(selector.slice('#fx-config-form '.length));
      if (selector === 'select[data-config-account]') return form.querySelectorAll(selector);
      return [...ids.values()].filter(node => node.matches(selector));
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
  const requests = [];
  let fetchHandler = async url => {
    if (url === '/api/google-fx/status') return { runtime: {}, tasks: [] };
    if (url === '/api/account-pool') return { accounts: [] };
    throw Error(`unexpected request: ${url}`);
  };
  const context = { console, document, AbortController, URLSearchParams,
    setTimeout: () => 1, clearTimeout() {},
    localStorage: { getItem() { return null; }, setItem() {} },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const result = await fetchHandler(url, options);
      return { ok: true, json: async () => result };
    },
  };
  vm.createContext(context);
  const code = fs.readFileSync(path.join(__dirname, '../js/google_fx_console.js'), 'utf8')
    .replace('const apiObject = {', 'const apiObject = { editor: { state, renderConfig, loadConfig, saveConfig, collectConfigPatch, readConfigValues, syncAccountConfigFields, updateConfigFeedback },');
  vm.runInContext(code, context);
  const editor = context.GoogleFxConsole.editor;
  const schema = {
    model: { type: 'enum', options: ['old', 'new', 'later'], default: 'old', hot: true, group: '模型', label: '模型' },
    priority: { type: 'account_list', default: [], hot: true, group: '号池', label: '优先账号' },
    googleFxSequenceUserId: { type: 'account', default: '', hot: true, group: '号池', label: '默认环境' },
    budget: { type: 'integer', min: 10, max: 1000, default: 120, hot: false, group: '超时', label: '等待上限' },
    silent: { type: 'bool', default: true, hot: true, group: '连接', label: '静默运行' },
    inactive: { type: 'integer', default: 5, hot: true, group: '号池', inactive: true },
  };
  const config = { model: 'old', priority: ['unknown-one', 'unknown-two'], googleFxSequenceUserId: 'missing', budget: 120, silent: true, inactive: 5 };
  editor.renderConfig({ config, schema });
  return {
    editor, form, ids, requests, config, schema, context,
    field: key => form.querySelectorAll('[data-config-key]').find(field => field.dataset.configKey === key),
    patch: () => JSON.parse(JSON.stringify(editor.collectConfigPatch())),
    fetch(handler) { fetchHandler = handler; },
  };
}

test('initial nonempty account preferences remain clean before and after pool loading', () => {
  const h = setup();
  assert.deepEqual(h.patch(), {});
  assert.equal(h.ids.get('fx-config-save').disabled, true);
  assert.equal(h.ids.get('fx-config-tab-indicator').hidden, true);
  assert.match(h.form.innerHTML, /unknown-one（已不在号池）/);
  h.editor.state.accounts = [{ user_id: 'unknown-one', name: 'Account One' }, { user_id: 'available', name: 'Account Two' }];
  h.editor.syncAccountConfigFields();
  assert.deepEqual(h.patch(), {}, 'refreshing account choices must not clear saved IDs');
  assert.match(h.field('priority').querySelector('.fx-config-account-list').innerHTML, /Account One/);
  assert.equal(h.field('googleFxSequenceUserId').value, 'missing');
  assert.equal(h.form.querySelectorAll('[data-config-key]').some(field => field.dataset.configKey === 'inactive'), false);
});

test('unknown saved enum stays selected instead of silently choosing the first option', () => {
  const h = setup();
  h.editor.renderConfig({ config: { ...h.config, model: 'saved-future-model' }, schema: h.schema });
  assert.equal(h.field('model').value, 'saved-future-model');
  assert.deepEqual(h.patch(), {});
});

test('section switching leaves the live configuration form and draft intact', () => {
  const h = setup();
  const field = h.field('model'); field.value = 'new';
  h.context.GoogleFxConsole.showSection('accounts');
  assert.equal(h.ids.get('fx-section-monitor').hidden, true);
  assert.equal(h.ids.get('fx-section-accounts').hidden, false);
  h.context.GoogleFxConsole.showSection('maintenance');
  assert.equal(h.field('model'), field);
  assert.deepEqual(h.patch(), { model: 'new' });
});

test('a config refresh preserves drafts and saving sends only changed fields', async () => {
  const h = setup();
  h.field('model').value = 'new';
  const draftField = h.field('model');
  h.editor.renderConfig({ config: { ...h.config, budget: 250 }, schema: h.schema });
  assert.equal(h.field('model'), draftField, 'incoming reads must not replace the input being edited');
  assert.deepEqual(h.patch(), { model: 'new' });
  h.fetch(async (url, options) => {
    if (url === '/api/google-fx/config') {
      assert.deepEqual(JSON.parse(options.body), { patch: { model: 'new' } });
      return { config: { ...h.config, budget: 250, model: 'new' }, changed: { model: 'new' } };
    }
    return url === '/api/account-pool' ? { accounts: [] } : { tasks: [] };
  });
  await h.editor.saveConfig();
  assert.equal(h.field('budget').value, '250');
  assert.deepEqual(h.patch(), {});
  assert.equal(h.ids.get('fx-config-save').disabled, true);
});

test('editing again while save is in flight keeps the later edit unsaved', async () => {
  const h = setup();
  let finish;
  h.field('model').value = 'new';
  h.fetch(url => url === '/api/google-fx/config' ? new Promise(resolve => { finish = resolve; })
    : url === '/api/account-pool' ? { accounts: [] } : { tasks: [] });
  const saving = h.editor.saveConfig();
  await h.editor.saveConfig();
  assert.equal(h.requests.filter(request => request.options?.method === 'POST').length, 1);
  h.field('model').value = 'later';
  finish({ config: { ...h.config, model: 'new' }, changed: { model: 'new' } });
  await saving;
  assert.equal(h.field('model').value, 'later');
  assert.deepEqual(h.patch(), { model: 'later' });
  assert.match(h.ids.get('fx-config-state').textContent, /1 项尚未保存/);
});

test('a failed save retains values and allows retry', async () => {
  const h = setup();
  h.field('budget').value = '180';
  h.fetch(async () => { throw Error('offline'); });
  await h.editor.saveConfig();
  assert.deepEqual(h.patch(), { budget: 180 });
  assert.equal(h.ids.get('fx-config-save').disabled, false);
  assert.match(h.ids.get('fx-config-note').textContent, /保存失败.*修改已保留/);
});

test('empty or malformed successful responses cannot clear an edited configuration', async () => {
  for (const payload of [{}, { config: {} }, { config: [] }, { config: { model: 'new' } }]) {
    const h = setup();
    h.field('budget').value = '180';
    h.fetch(() => payload);
    await h.editor.saveConfig();
    assert.deepEqual(h.patch(), { budget: 180 });
    assert.match(h.ids.get('fx-config-note').textContent, /服务器未返回保存结果/);
    await h.editor.loadConfig();
    assert.deepEqual(h.patch(), { budget: 180 });
    assert.match(h.ids.get('fx-config-note').textContent, /服务器未返回有效配置/);
  }
});

test('explicit reload discards the old draft but preserves edits made while waiting', async () => {
  const h = setup();
  h.field('budget').value = '180';
  let finish;
  h.fetch(() => new Promise(resolve => { finish = resolve; }));
  const loading = h.editor.loadConfig({ force: true, draft: {} });
  h.field('model').value = 'later';
  finish({ config: h.config, schema: h.schema });
  await loading;
  assert.equal(h.field('budget').value, '120');
  assert.deepEqual(h.patch(), { model: 'later' });
});

test('saved restart requirements stay visible and use readable labels', async () => {
  const h = setup();
  h.field('budget').value = '180';
  h.fetch(url => url === '/api/google-fx/config'
    ? { config: { ...h.config, budget: 180 }, changed: { budget: 180 }, restart_required: ['budget'] }
    : url === '/api/account-pool' ? { accounts: [] } : { tasks: [] });
  await h.editor.saveConfig();
  assert.match(h.ids.get('fx-config-state').textContent, /待重启/);
  assert.match(h.ids.get('fx-config-note').textContent, /等待上限.*当前未执行重启/);
  h.editor.renderConfig({ config: { ...h.config, budget: 180 }, schema: h.schema });
  assert.match(h.ids.get('fx-config-state').textContent, /待重启/);
});

test('an older GET cannot restore pre-save values', async () => {
  const h = setup();
  let finishRead;
  h.fetch((url, options) => {
    if (url === '/api/google-fx/config' && !options?.method) return new Promise(resolve => { finishRead = resolve; });
    if (url === '/api/google-fx/config') return { config: { ...h.config, model: 'new' }, changed: { model: 'new' } };
    return url === '/api/account-pool' ? { accounts: [] } : { tasks: [] };
  });
  const reading = h.editor.loadConfig();
  h.field('model').value = 'new';
  await h.editor.saveConfig();
  finishRead({ config: h.config, schema: h.schema });
  await reading;
  assert.equal(h.field('model').value, 'new');
  assert.deepEqual(h.patch(), {});
});
