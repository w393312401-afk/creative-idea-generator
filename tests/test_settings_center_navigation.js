const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Minimal DOM built from the real modal, so tests catch accidental reparenting.
let activeElement;
class Element {
    constructor(tag, attributes = {}) {
        this.tagName = tag.toUpperCase();
        this.attributes = attributes;
        this.children = [];
        this.ownText = '';
        this.listeners = {};
        this.dataset = {};
        this.style = {};
        this.className = attributes.class || '';
        this.id = attributes.id || '';
        this.value = attributes.value || '';
        this.open = 'open' in attributes;
        this.hidden = 'hidden' in attributes;
        this.disabled = 'disabled' in attributes;
        this.checked = 'checked' in attributes;
        for (const [key, value] of Object.entries(attributes)) {
            if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
        }
        for (const rule of (attributes.style || '').split(';')) {
            const [key, value] = rule.split(':');
            if (key && value) this.style[key.trim()] = value.trim();
        }
        this.classList = {
            contains: name => this.className.split(/\s+/).includes(name),
            toggle: (name, on) => {
                const values = new Set(this.className.split(/\s+/).filter(Boolean));
                if (on) values.add(name); else values.delete(name);
                this.className = [...values].join(' ');
            },
            add: name => this.classList.toggle(name, true),
            remove: name => this.classList.toggle(name, false),
        };
    }
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
    replaceChildren() { this.children = []; this.ownText = ''; }
    set textContent(value) { this.replaceChildren(); this.ownText = value; }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    setAttribute(key, value) { this.attributes[key] = value; }
    matches(selector) {
        return selector.split(',').some(part => {
            part = part.trim();
            if (part.includes(':not([disabled])')) return !this.disabled && this.matches(part.replace(':not([disabled])', ''));
            if (part.startsWith('.')) return this.classList.contains(part.slice(1));
            if (part.startsWith('[')) return part.slice(1, -1) in this.attributes;
            return this.tagName === part.toUpperCase();
        });
    }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    contains(child) { return child === this || this.children.some(el => el.contains(child)); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    dispatch(type, extra = {}) {
        const event = { target: this, preventDefault() {}, stopPropagation() { this.stopped = true; }, ...extra };
        for (let node = this; node && !event.stopped; node = node.parentElement) {
            for (const fn of node.listeners[type] || []) fn(event);
        }
    }
    click() { this.dispatch('click'); }
    focus() {
        if (activeElement === this) return;
        activeElement = this;
        for (const fn of this.listeners.focus || []) fn({ target: this });
    }
    scrollIntoView() { this.scrolled = true; }
}

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const modalHtml = html.slice(html.indexOf('<div class="modal" id="settings-modal">'), html.indexOf('<!-- Trend Refs Library Management Modal'));
const root = new Element('root');
const stack = [root];
const ids = new Map();
const voids = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
for (const token of modalHtml.match(/<!--[\s\S]*?-->|<[^>]+>|[^<]+/g)) {
    if (token.startsWith('<!--')) continue;
    const close = token.match(/^<\/([\w-]+)>/);
    if (close) {
        assert.equal(stack.at(-1).tagName, close[1].toUpperCase(), 'modal tags must be correctly nested');
        stack.pop();
    } else if (token.startsWith('<')) {
        const tag = token.match(/^<([\w-]+)/)[1];
        const attrs = {};
        const raw = token.slice(tag.length + 1, -1);
        for (const match of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[match[1]] = match[2] || '';
        const element = stack.at(-1).appendChild(new Element(tag, attrs));
        if (element.id) { assert.ok(!ids.has(element.id), `duplicate ID: ${element.id}`); ids.set(element.id, element); }
        if (!voids.has(tag)) stack.push(element);
    } else stack.at(-1).ownText += token;
}
assert.equal(stack.length, 1);
const get = id => ids.get(id) || null;
const source = fs.readFileSync(path.join(__dirname, '../js/config.js'), 'utf8');
const settings = source.slice(source.indexOf('const SETTINGS_SECTION_KEY'), source.indexOf('function updateDesktopPermissionStatus'));
let saves = 0;
const stored = [];
const ctx = vm.createContext({
    document: { getElementById: get, createElement: tag => new Element(tag) },
    localStorage: { setItem: (key, value) => stored.push([key, value]), getItem: () => null },
    autoSaveConfig: () => saves++, initNotificationSettingsEvents() {}, updateVideoProviderStatus() {},
});
vm.runInContext(settings, ctx);
ctx.initSettingsCenter();
ctx.initSettingsCenter();

for (const id of ['settings-llm-model', 'settings-image-backend', 'settings-api-image-model', 'settings-fx-image-model', 'settings-image-ratio', 'settings-image-quality', 'settings-fx-video-model']) {
    assert.equal(get(id).closest('details'), null, `${id} remains directly available`);
    assert.equal(get(id).closest('.settings-section').dataset.section, 'backend');
}
for (const id of ['settings-skill-profile', 'settings-candidate-concurrency', 'settings-fx-video-ref-mode', 'settings-ideation-search-query', 'settings-ideation-trend-urls', 'version-details-card', 'clear-cache-btn', 'clear-logs-btn']) {
    assert.equal(get(id).closest('details')?.open, false, `${id} is present, initially folded`);
}

const search = get('settings-search-input');
const results = get('settings-search-results');
search.value = '并发';
search.dispatch('input');
assert.equal(results.querySelectorAll('button').length, 1);
results.querySelector('button').click();
assert.equal(get('settings-generation-advanced').open, true);
assert.equal(activeElement, get('settings-candidate-concurrency'));
assert.equal(get('api-candidate-concurrency-group').scrolled, true);
assert.equal(results.hidden, true);
assert.equal(saves, 0, 'search and disclosure never save configuration');
assert.ok(stored.every(([key]) => key === 'spark_settings_section'));

get('api-candidate-concurrency-group').style.display = 'none';
search.value = '并发';
search.dispatch('input');
assert.equal(results.querySelectorAll('button').length, 0, 'backend-disabled controls must not appear in search');
get('api-candidate-concurrency-group').style.display = '';

let clears = 0;
get('clear-cache-btn').addEventListener('click', () => clears++);
search.value = '系统缓存';
search.dispatch('input');
results.querySelector('button').click();
assert.equal(get('settings-maintenance-advanced').open, true);
assert.equal(get('clear-cache-btn').closest('.settings-section').classList.contains('active'), true);
assert.equal(activeElement, get('clear-cache-btn'));
assert.equal(clears, 0, 'finding maintenance only focuses it, never executes cleanup');

search.value = '并发';
search.dispatch('input');
const first = results.querySelector('button');
first.focus();
first.dispatch('keydown', { key: 'Escape' });
assert.equal(results.hidden, true, 'Escape closes results without reopening on input focus');
assert.equal(activeElement, search);
assert.equal(get('gate-settings-search'), null, 'retired quality gates have no editable search controls');
search.dispatch('change');
assert.equal(saves, 0, 'global search cannot trigger autosave');
get('settings-candidate-concurrency').dispatch('change');
assert.equal(saves, 1, 'changing a folded field still saves exactly once');
assert.equal(get('settings-generation-advanced').open, true);

// The form reader must retain controls even after all advanced groups close.
vm.runInContext(source.slice(source.indexOf('function applySettingsFormToConfig()'), source.indexOf('function saveConfig()')), ctx);
ctx.config = {};
ctx.normalizeGoogleFxImageModel = value => value;
const values = {
    'settings-skill-profile': 'miniature', 'settings-candidate-concurrency': '2',
    'settings-fx-video-ref-mode': 'VIDEO_REFERENCES', 'settings-ideation-search-query': '  树屋  ',
    'settings-ideation-trend-urls': '  https://example.com/  ', 'settings-image-ratio': '16:9',
    'settings-image-quality': '4K',
};
for (const [id, value] of Object.entries(values)) { get(id).value = value; const details = get(id).closest('details'); if (details) details.open = false; }
ctx.applySettingsFormToConfig();
assert.equal(ctx.config.skillProfile, 'miniature');
assert.equal(ctx.config.candidateConcurrency, 2);
assert.equal(ctx.config.videoRefMode, 'VIDEO_REFERENCES');
assert.equal(ctx.config.ideationSearchQuery, '树屋');
assert.equal(ctx.config.ideationTrendUrls, 'https://example.com/');
assert.equal(ctx.config.imageAspectRatio, '16:9');
assert.equal(ctx.config.imageQuality, '4K');

vm.runInContext(source.slice(source.indexOf('function updateFxImageModelVisibility()'), source.indexOf('// 把配置中心表单里的值')), ctx);
get('settings-image-backend').value = 'google_fx';
get('settings-fx-video-model').value = 'Omni Flash';
ctx.updateFxImageModelVisibility();
assert.equal(get('api-image-model-group').style.display, 'none');
assert.equal(get('api-candidate-concurrency-group').style.display, 'none');
assert.equal(get('fx-image-model-group').style.display, '');
assert.equal(get('fx-video-duration-group').style.display, '');
get('settings-image-backend').value = 'api';
get('settings-fx-video-model').value = 'Veo 3.1 - Lite';
ctx.updateFxImageModelVisibility();
assert.equal(get('api-image-model-group').style.display, '');
assert.equal(get('api-candidate-concurrency-group').style.display, '');
assert.equal(get('fx-image-model-group').style.display, 'none');
assert.equal(get('fx-video-duration-group').style.display, 'none');
assert.equal(get('fx-video-resolution-group').style.display, 'none');
assert.notEqual(get('fx-video-model-group').style.display, 'none', 'video model is independent of image backend');
assert.notEqual(get('fx-video-ref-mode-group').style.display, 'none');
assert.equal(saves, 1, 'layout and visibility updates do not save');

console.log('Settings navigation: actual HTML structure, search/focus, no unintended writes, folded reads and backend visibility passed.');
