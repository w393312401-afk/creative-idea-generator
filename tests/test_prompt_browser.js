const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function element(tag = 'div') {
    const el = { tagName: tag.toUpperCase(), children: [], parentElement: null, className: '', dataset: {}, style: {},
        value: '', hidden: false, disabled: false, listeners: {}, attributes: {}, scrollHeight: 100,
        append(...children) { children.forEach(child => { child.parentElement = this; this.children.push(child); }); },
        appendChild(child) { this.append(child); return child; },
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
        async fire(type, event = {}) { for (const fn of this.listeners[type] || []) await fn({ target: this, preventDefault() {}, stopPropagation() {}, ...event }); },
        setAttribute(name, value) { this.attributes[name] = value; },
        matches(selector) { return selector.startsWith('.') ? this.classList.contains(selector.slice(1)) : this.tagName.toLowerCase() === selector; },
        querySelectorAll(selector) {
            const bits = selector.split(' '), results = [];
            const visit = node => { if (node.matches(bits.at(-1)) && (bits.length === 1 || node.parentElement?.closest(bits[0]))) results.push(node); node.children.forEach(visit); };
            this.children.forEach(visit); return results;
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; },
        remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(node => node !== this); this.parentElement = null; },
        focus() { this.focused = true; }, scrollIntoView() { this.scrolled = true; }, setSelectionRange() {},
    };
    el.classList = { contains: name => el.className.split(/\s+/).includes(name),
        add: name => { if (!el.classList.contains(name)) el.className = [el.className, name].filter(Boolean).join(' '); },
        remove: name => { el.className = el.className.split(/\s+/).filter(value => value !== name).join(' '); },
        toggle: name => { const added = !el.classList.contains(name); el.classList[added ? 'add' : 'remove'](name); return added; } };
    let ownText = '';
    Object.defineProperty(el, 'textContent', { get: () => ownText + el.children.map(child => child.textContent).join(''),
        set: value => { ownText = String(value); el.children = []; } });
    Object.defineProperty(el, 'innerHTML', { set: value => {
        ownText = ''; el.children = [];
        // Only the controls needed for these DOM behaviour tests; icons are inert.
        for (const match of String(value).matchAll(/<(span|button|textarea)\b([^>]*)>([\s\S]*?)<\/\1>/g)) {
            const child = element(match[1]); child.className = /class="([^"]*)"/.exec(match[2])?.[1] || '';
            child.textContent = match[3].replace(/<[^>]+>/g, ''); el.append(child);
        }
    } });
    return el;
}

const sample = name => `图片提示词\n图片 1:\n${name} wooden floor\n图片 2:\n${name} stone wall\n\n视频提示词\n视频 1:\n${name} worker builds floor`;
function harness(storage = new Map()) {
    const ids = ['prompt-box', 'idea-prompt-block', 'idea-prompt-editor', 'prompt-edit-hint',
        'toggle-fold-all-prompts-btn', 'edit-prompt-btn', 'auto-fix-prompt-btn', 'prompt-history-btn',
        'add-beat-btn', 'save-prompt-edit-btn', 'cancel-prompt-edit-btn', 'copy-prompt-btn',
        'prompt-browse-tools', 'prompt-search', 'prompt-search-clear', 'prompt-jump-type',
        'prompt-jump-number', 'prompt-jump-btn', 'prompt-browse-status'];
    const dom = Object.fromEntries(ids.map(id => [id, element()]));
    dom['toggle-fold-all-prompts-btn'].innerHTML = '<span>全部折叠</span>';
    const requests = [], histories = [];
    const ctx = { console, document: { getElementById: id => dom[id], createElement: element },
        localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
        showToast() {}, isIdeaTaskActive: () => false, customConfirm: async () => true,
        getIdeaSaveTitle: idea => idea.id, padSlot: value => String(value).padStart(3, '0'),
        slotPostJson: async (url, body) => { requests.push({ url, body }); return { prompt_block: body.prompt_block, prompt_slots: { authoritative: true }, image_count: 2 }; },
        recordPromptHistory: (...args) => histories.push(args),
        setTimeout: fn => { fn(); return 1; }, navigator: { clipboard: { writeText: async () => {} } },
    };
    vm.createContext(ctx);
    vm.runInContext(read('js/prompt_pipeline.js'), ctx);
    vm.runInContext(read('js/prompt_editor.js'), ctx);
    ctx.applyPromptBlockToIdea = async (owner, block, slots) => {
        owner.prompt_block = block; owner.prompt_slots = slots;
        if (ctx.currentIdea.id === owner.id) ctx.renderPromptDisplay(block);
    };
    ctx.mutateSlot = async options => { const data = await options.request(); await options.beforeApply(data); return true; };
    ctx.initPromptEditor();
    const open = (id, block = sample(id)) => {
        ctx.currentIdea = { id, prompt_block: block }; ctx.resetPromptEditor(); ctx.renderPromptDisplay(block); return ctx.currentIdea;
    };
    const cards = () => dom['idea-prompt-block'].querySelectorAll('.prompt-item-card');
    return { ctx, dom, storage, requests, histories, open, cards };
}

async function browseAndPersistence() {
    const h = harness(), owner = h.open('A');
    await h.cards()[0].querySelector('.prompt-item-header').fire('click');
    assert(h.cards()[0].classList.contains('is-collapsed'));
    h.ctx.renderPromptDisplay(owner.prompt_block);
    assert(h.cards()[0].classList.contains('is-collapsed'), 'same-project rerenders restore per-card fold');
    h.dom['prompt-search'].value = 'WOODEN'; await h.dom['prompt-search'].fire('input');
    assert.deepEqual(h.cards().map(card => card.hidden), [false, true, true]);
    assert(!h.cards()[0].classList.contains('is-collapsed'), 'matching text is exposed');
    assert.equal(owner.prompt_block, sample('A'), 'search never edits saved source');
    await h.dom['prompt-search-clear'].fire('click');
    assert(h.cards()[0].classList.contains('is-collapsed'), 'clearing search restores fold preference');
    h.dom['prompt-search'].value = 'no match'; await h.dom['prompt-search'].fire('input');
    assert(h.cards().every(card => card.hidden));
    assert.match(h.dom['prompt-browse-status'].textContent, /未找到/);
    h.dom['prompt-jump-type'].value = 'video'; h.dom['prompt-jump-number'].value = '1';
    await h.dom['prompt-jump-btn'].fire('click');
    assert.equal(h.dom['prompt-search'].value, '');
    assert(h.cards()[2].focused && h.cards()[2].scrolled);
    h.dom['prompt-search'].value = 'wooden'; await h.dom['prompt-search'].fire('input');
    h.open('B');
    assert.equal(h.dom['prompt-search'].value, '');
    assert.equal(h.dom['prompt-jump-type'].value, 'image');
    assert.equal(h.dom['prompt-jump-number'].value, '');
    assert(h.cards().every(card => !card.hidden && !card.classList.contains('is-collapsed')));
    h.open('A'); assert(h.cards()[0].classList.contains('is-collapsed'));
    const reloaded = harness(h.storage); reloaded.open('A');
    assert(reloaded.cards()[0].classList.contains('is-collapsed'), 'fold preference survives page reload');
    assert.equal(reloaded.dom['prompt-search'].value, '', 'search is not restored across pages');
    const section = reloaded.dom['idea-prompt-block'].querySelector('.prompt-section');
    await section.querySelector('.prompt-section-header').fire('click');
    reloaded.ctx.renderPromptDisplay(reloaded.ctx.currentIdea.prompt_block);
    assert(reloaded.dom['idea-prompt-block'].querySelector('.prompt-section').classList.contains('is-collapsed'));
    await reloaded.dom['toggle-fold-all-prompts-btn'].fire('click');
    reloaded.ctx.renderPromptDisplay(reloaded.ctx.currentIdea.prompt_block);
    assert(reloaded.cards().every(card => card.classList.contains('is-collapsed')), 'batch folds survive rerender');
    reloaded.dom['prompt-search'].value = 'wooden';
    assert.equal(reloaded.ctx.jumpToPromptInBrowser('image', 999), false);
    assert.equal(reloaded.dom['prompt-search'].value, 'wooden', 'invalid jump leaves search unchanged');
}

async function inlineSaveOwnership() {
    const h = harness(), owner = h.open('A'), wait = deferred();
    h.ctx.slotPostJson = async (url, body) => { h.requests.push({ url, body }); return wait.promise; };
    const card = h.cards()[0]; await card.querySelector('.prompt-item-edit-btn').fire('click');
    h.ctx.enterPromptEdit();
    assert.equal(h.dom['idea-prompt-block'].hidden, false, 'whole editor cannot hide an unsaved inline draft');
    card.querySelector('.prompt-inline-textarea').value = 'changed A';
    const saving = card.querySelector('.prompt-inline-save-btn').fire('click');
    assert.equal(h.requests[0].body.title, 'A');
    const other = h.open('B');
    wait.resolve({ prompt_block: sample('SERVER A'), prompt_slots: { authoritative: true } }); await saving;
    assert.equal(owner.prompt_block, sample('SERVER A'));
    assert.equal(other.prompt_block, sample('B'));
    assert.equal(h.dom['idea-prompt-block'].dataset.rawText, sample('B'));
    assert.equal(h.histories.at(-1)[1], sample('SERVER A'), 'history uses server-authoritative text');
    const oldCard = h.cards()[0]; await oldCard.querySelector('.prompt-item-edit-btn').fire('click');
    oldCard.querySelector('.prompt-inline-textarea').value = 'stale B'; h.open('C');
    await oldCard.querySelector('.prompt-inline-save-btn').fire('click');
    assert.equal(h.requests.length, 1, 'detached old editor cannot write current project');
}

async function fullSaveOwnership() {
    const h = harness(), owner = h.open('A'), wait = deferred();
    h.ctx.enterPromptEdit(); h.dom['idea-prompt-editor'].value = sample('edited A');
    h.ctx.slotPostJson = async (url, body) => { h.requests.push({ url, body }); return wait.promise; };
    const saving = h.ctx.savePromptEdit();
    for (let i = 0; i < 12; i++) await Promise.resolve();
    assert.equal(h.requests.length, 1);
    h.open('B'); h.ctx.enterPromptEdit(); h.dom['idea-prompt-editor'].value = sample('B draft');
    wait.resolve({ prompt_block: sample('SERVER A'), prompt_slots: { authoritative: true }, image_count: 2 }); await saving;
    assert.equal(owner.prompt_block, sample('SERVER A'));
    assert.equal(h.dom['idea-prompt-editor'].hidden, false, 'A completion cannot close B editor');
    assert.equal(h.dom['idea-prompt-editor'].value, sample('B draft'));
    assert.equal(h.dom['idea-prompt-editor'].disabled, false);

    const confirmation = deferred(); h.ctx.customConfirm = () => confirmation.promise;
    const pending = h.ctx.savePromptEdit(); h.open('C'); confirmation.resolve(true); await pending;
    assert.equal(h.requests.length, 1, 'switch during confirmation cancels the stale write');
}

(async () => {
    await browseAndPersistence(); await inlineSaveOwnership(); await fullSaveOwnership();
    console.log('prompt browser, persistence and save ownership tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
