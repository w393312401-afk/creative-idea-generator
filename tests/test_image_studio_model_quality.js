const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../js/image_studio.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(__dirname, '../js/state.js'), 'utf8');
const normalizerSource = stateSource.slice(stateSource.indexOf('function normalizeApiImageModel('), stateSource.indexOf('\nconst IMAGE_MODELS'));
const css = fs.readFileSync(path.join(__dirname, '../css/image_studio.css'), 'utf8');
assert.match(css, /\.quality-desc\[data-variant-size-description="1"\]\s*\{\s*display:\s*block;/, 'variant size limits remain visible in the mobile layout');
const nodes = new Map();
const cards = new Map();
for (const tabId of ['t2i', 'i2i']) {
    const start = html.indexOf(`id="${tabId}-quality-selector"`);
    assert.ok(start > 0, `${tabId} quality selector exists`);
    const markup = html.slice(start);
    const list = [...markup.matchAll(/<div class="quality-card([^"]*)" data-quality="([^"]+)">([\s\S]*?)<\/div>/g)].slice(0, 3).map(match => {
        const descriptionMatch = match[3].match(/<span class="quality-desc"([^>]*)>([^<]*)<\/span>/);
        assert.ok(descriptionMatch, `${tabId} ${match[2]} has a resolution description`);
        const description = { dataset: {}, textContent: descriptionMatch[2], hidden: /\bhidden\b/.test(descriptionMatch[1]) };
        const qualityHintMatch = match[3].match(/<span class="quality-hint"([^>]*)>([^<]*)<\/span>/);
        const qualityHint = qualityHintMatch && { dataset: {}, textContent: qualityHintMatch[2], hidden: /\bhidden\b/.test(qualityHintMatch[1]) };
        return { quality: match[2], active: match[1].includes('active'), description, qualityHint,
            getAttribute: key => key === 'data-quality' ? match[2] : null,
            querySelector: selector => selector === '.quality-desc' ? description : selector === '.quality-hint' ? qualityHint : null };
    });
    assert.deepEqual(list.map(card => card.quality), ['1K', '2K', '4K']);
    cards.set(tabId, list);
    const listeners = {};
    nodes.set(`${tabId}-model`, { value: 'nano-banana-2', dataset: {}, listeners,
        options: ['nano-banana-2', 'gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2.5'].map(value => ({ value })),
        appendChild(option) { this.options.push(option); },
        addEventListener: (type, callback) => (listeners[type] ||= []).push(callback),
        dispatch: type => (listeners[type] || []).forEach(callback => callback()),
    });
    nodes.set(`${tabId}-model-quality-hint`, { hidden: true, textContent: '' });
    nodes.set(`${tabId}-prompt`, { value: '' });
}
const toasts = [];
const context = { console, showToast: (...args) => toasts.push(args), document: {
    addEventListener() {}, getElementById: id => nodes.get(id) || null,
    createElement: () => ({ value: '', textContent: '' }),
    querySelector: () => null,
    querySelectorAll: selector => {
        const tab = selector.match(/^#(t2i|i2i)-quality-selector \.quality-card$/)?.[1];
        return cards.get(tab) || [];
    },
} };
context.window = context;
vm.createContext(context);
vm.runInContext(normalizerSource, context);
vm.runInContext(source, context);
const original = new Map([...cards].map(([tab, list]) => [tab, list.map(card => ({
    quality: card.quality, active: card.active,
    text: card.description.textContent, hidden: card.description.hidden,
}))]));
context.initImageStudioModelQualityDescriptions();
context.initImageStudioModelQualityDescriptions();

for (const tabId of ['t2i', 'i2i']) {
    const select = nodes.get(`${tabId}-model`);
    assert.equal(select.listeners.change.length, 1, 'initialization does not double-bind changes');
    const hint = nodes.get(`${tabId}-model-quality-hint`);
    for (const [model, qualityHint] of [['gpt-image-2.5-sunburst', '最高画质'], ['gpt-image-2.5-flare', '自动画质']]) {
        select.value = model;
        select.dispatch('change');
        const descriptions = cards.get(tabId).map(card => card.description);
        assert.equal(descriptions[0].textContent, '标准尺寸');
        assert.match(descriptions[1].textContent, /最高 2048px/);
        assert.match(descriptions[2].textContent, /最高 3840px.*实验/);
        assert.ok(descriptions.every(description => !description.hidden));
        assert.ok(descriptions.every(description => description.dataset.variantSizeDescription === '1'));
        assert.ok(cards.get(tabId).every(card => !card.qualityHint || card.qualityHint.hidden), 'resolution choices do not show old quality tier labels for variants');
        assert.equal(hint.hidden, false);
        assert.ok(hint.textContent.includes(qualityHint));
        assert.doesNotMatch(descriptions.map(description => description.textContent).join(' '), /4096/);
        assert.deepEqual(cards.get(tabId).map(card => [card.quality, card.active]), original.get(tabId).map(card => [card.quality, card.active]), 'description updates preserve quality values and selection');
    }
    select.value = 'gpt-image-2.5';
    select.dispatch('change');
    assert.ok(cards.get(tabId).every(card => /网关自适应/.test(card.description.textContent)), 'base model promises no fixed output size');
    assert.match(hint.textContent, /网关自适应/);
    assert.doesNotMatch(cards.get(tabId).map(card => card.description.textContent).join(' '), /4096|3840|2048/);
    for (const model of ['nano-banana-2']) {
        select.value = model;
        select.dispatch('change');
        assert.deepEqual(cards.get(tabId).map(card => ({ quality: card.quality, active: card.active,
            text: card.description.textContent, hidden: card.description.hidden })), original.get(tabId), 'switching to other models restores original descriptions');
        assert.equal(hint.hidden, true);
        assert.ok(cards.get(tabId).every(card => card.description.dataset.variantSizeDescription === '0'));
        assert.ok(cards.get(tabId).every(card => !card.qualityHint || !card.qualityHint.hidden), 'other models recover the original quality tier labels');
    }

    vm.runInContext(`imgStudioCurrentTab = '${tabId}'`, context);
    context.imgStudioReusePrompt('saved prompt', '16:9', '2K', 'gpt-image-2.5-sunburst');
    assert.equal(select.value, 'gpt-image-2.5-sunburst');
    assert.match(cards.get(tabId)[2].description.textContent, /3840px/);
    assert.match(hint.textContent, /最高画质/);
    for (const retired of ['gpt-image-2', 'gpt-image-2-2026-09-08']) {
        context.imgStudioReusePrompt('retired saved prompt', '16:9', '2K', retired);
        assert.equal(select.value, 'gpt-image-2.5');
        assert.equal(select.options.filter(option => option.value === retired).length, 0);
        assert.equal(hint.hidden, false);
        assert.match(hint.textContent, /网关自适应/);
    }

    for (const [model, qualityHint] of [
        ['gpt-image-2.5-sunburst-2026-09-08', '最高画质'],
        ['gpt-image-2.5-flare-2026-09-08', '自动画质'],
    ]) {
        context.imgStudioReusePrompt('dated saved prompt', '16:9', '4K', model);
        assert.equal(select.value, model, 'dated history restores the complete model ID');
        assert.equal(select.options.filter(option => option.value === model).length, 1);
        assert.match(cards.get(tabId)[2].description.textContent, /3840px/);
        assert.ok(hint.textContent.includes(qualityHint));
        context.imgStudioReusePrompt('dated saved prompt', '16:9', '4K', model);
        assert.equal(select.options.filter(option => option.value === model).length, 1, 'repeated history reuse does not duplicate temporary options');
    }
    const restoredModel = select.value;
    for (const model of ['unknown-custom-model', 'gpt-image-2.5-sunburst-max', 'gpt-image-2.5-flare-2026-09-08-extra']) {
        context.imgStudioReusePrompt('other saved prompt', '16:9', '4K', model);
        assert.equal(select.value, restoredModel, 'unrecognized history model retains existing behavior');
        assert.equal(select.options.filter(option => option.value === model).length, 0);
    }
}
context.setImageGatewayModelCatalog({ status: 'known', models: ['gpt-image-2.5'] });
for (const tabId of ['t2i', 'i2i']) {
    const select = nodes.get(`${tabId}-model`);
    select.value = 'gpt-image-2.5-sunburst';
    context.syncImageStudioModelAvailability();
    assert.ok(select.options.find(option => option.value === select.value).disabled);
    assert.match(nodes.get(`${tabId}-model-quality-hint`).textContent, /当前网关不可用/);
    assert.doesNotMatch(nodes.get(`${tabId}-model-quality-hint`).textContent, /最高画质/);
    assert.ok(cards.get(tabId).every(card => card.description.textContent === '当前网关不可用'));
    vm.runInContext(`imgStudioCurrentTab = '${tabId}'`, context);
    context.imgStudioReusePrompt('unsupported saved prompt', '16:9', '2K', 'gpt-image-2.5-sunburst-2026-09-08');
    assert.equal(select.value, 'gpt-image-2.5');
    assert.match(toasts.at(-1)[0], /sunburst-2026-09-08.*当前网关不可用.*已.*选择 gpt-image-2\.5/);
}
context.setImageGatewayModelCatalog({ status: 'unknown', models: [] });
context.syncImageStudioModelAvailability();
assert.ok([...nodes.values()].filter(node => node.options).every(select => select.options.every(option => !option.disabled)), 'unknown catalog does not disable models');
console.log('Image Studio quality descriptions: model changes, size limits, restoration and history reuse passed');
