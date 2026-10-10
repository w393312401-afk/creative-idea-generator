// The compact action menu preserves delegated actions and never opens a preview by accident.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { frameSlotState, videoSlotState } = require('../js/slot_model.js');

const calls = [];
const ctx = {
    triggerFrameUpload: seq => calls.push(['upload-frame', seq]),
    triggerVideoUpload: seq => calls.push(['upload-video', seq]),
    retrySingleVideo: seq => calls.push(['retry-video', seq]),
    deleteSlotBeat: seq => calls.push(['delete-slot', seq]),
    reconcileVideoSubmission: seq => calls.push(['query-original', seq]),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'slot_card.js'), 'utf8'), ctx);
ctx.openSlotLightbox = (type, seq) => calls.push(['preview', type, seq]);

for (const pending of [
    { slot: 37, provider: 'flow2api', status: 'failed', last_attempt: { submission_pending: true } },
    { slot: 37, provider: 'flow2api', status: 'success', url: '/old.mp4', last_attempt: { submission_pending: true } },
]) {
    const state = videoSlotState(pending, { seq: 37 });
    const card = { dataset: {}, querySelector: () => null };
    ctx.renderSlotCard(card, state);
    assert(card.innerHTML.includes('data-act="query-video-submission"'));
    assert(card.innerHTML.includes('data-act="retry-video"'));
    assert(card.innerHTML.includes('slot-select-box'), '旧Flow2API待确认卡开放批量重试');
    assert(card.innerHTML.includes('data-primary="1"'));
    if (pending.status === 'failed') {
        assert.strictEqual(card.dataset.kind, 'submission-pending');
        assert(card.innerHTML.includes('data-act="query-video-submission" data-variant="default" data-primary="1"'));
    }
}

const fixedState = videoSlotState({ slot: 37, provider: 'google_fx', status: 'failed',
    last_attempt: { fixed_video_account: true, submission_pending: true } }, { seq: 37 });
const fixedCard = { dataset: {}, querySelector: () => null };
ctx.renderSlotCard(fixedCard, fixedState);
assert(!fixedCard.innerHTML.includes('data-act="retry-video"'));
assert(!fixedCard.innerHTML.includes('slot-select-box'), '固定原生账号待确认卡继续受保护');

for (const state of [
    frameSlotState({ sequence: 3, url: '/image.webp' }, { seq: 3 }),
    videoSlotState({ slot: 3, status: 'success', url: '/video.mp4' }, { slot: 3 }),
]) {
    const html = ctx.slotActionsHtml(state);
    assert.strictEqual((html.match(/data-primary="1"/g) || []).length, 1);
    assert.strictEqual((html.match(/<details /g) || []).length, 1);
    for (const action of state.actions) {
        assert.ok(html.includes(`data-act="${action.act}"`), `${action.act} stays accessible`);
    }
}

const listeners = {};
const card = { dataset: { seq: '3', type: 'image', kind: 'ready' }, classList: {
    toggle(name, enabled) { card[name] = enabled; },
} };
let focused = false;
const menu = {
    open: true,
    matches: selector => selector === '.slot-more-actions',
    closest: selector => selector === '.slot-card' ? card : null,
    querySelector: () => ({ focus() { focused = true; } }),
};
const other = { open: true };
const grid = {
    dataset: {}, contains: value => value === card,
    addEventListener: (name, handler) => { listeners[name] = handler; },
    querySelectorAll: () => [menu, other].filter(item => item.open),
};
ctx.document = { getElementById: () => grid };
ctx.bindSlotGrid('frames-grid');

function click(action, disabled = false) {
    const button = action ? { dataset: { act: action }, disabled,
        closest: selector => selector === '.slot-more-actions' ? menu : null } : null;
    const target = { closest: selector => selector === '.slot-card' ? card
        : selector === '.slot-action-btn' ? button
        : selector === '.slot-more-actions' ? menu : null };
    listeners.click({ target, stopPropagation() {} });
}

click(null);
assert.strictEqual(calls.length, 0, 'more disclosure must not open a preview');
click('upload-frame');
assert.deepStrictEqual(calls.pop(), ['upload-frame', 3]);
assert.strictEqual(menu.open, false, 'selection closes the menu');
click('delete-slot', true);
assert.strictEqual(calls.length, 0, 'disabled actions do not dispatch');
click('preview-slot');
assert.deepStrictEqual(calls.pop(), ['preview', 'image', 3]);
card.dataset.type = 'video';
click('retry-video');
assert.deepStrictEqual(calls.pop(), ['retry-video', 3]);
click('query-video-submission');
assert.deepStrictEqual(calls.pop(), ['query-original', 3]);
click('upload-video');
assert.deepStrictEqual(calls.pop(), ['upload-video', 3]);
click('preview-slot');
assert.deepStrictEqual(calls.pop(), ['preview', 'video', 3]);

menu.open = true;
listeners.toggle({ target: menu });
assert.strictEqual(card['is-actions-open'], true);
assert.strictEqual(other.open, false, 'opening one menu closes the previous menu');
listeners.keydown({ key: 'Escape', target: { closest: () => menu }, stopPropagation() {} });
assert.strictEqual(menu.open, false);
assert.ok(focused, 'Escape returns keyboard focus to the menu toggle');

console.log('slot card action tests passed');
