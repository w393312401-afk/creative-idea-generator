const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let opened;
const ctx = {
    console, URLSearchParams,
    currentIdea: { frameRun: {} },
    resolvePromptSlots: idea => idea.slots || [],
    refFrameRoleLabel: () => '',
    openLightbox: (items, index) => { opened = { items, index }; },
};
vm.createContext(ctx);
for (const file of ['media_renderer.js', 'slot_card.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', file), 'utf8'), ctx);
}

function check(frames, slots, seq, expected) {
    ctx.currentIdea = { frameRun: { frames }, slots, ref_frames: { 1: '/ref/1.png' } };
    opened = null;
    ctx.openSlotLightbox('image', seq);
    if (expected === null) return assert.strictEqual(opened, null);
    assert.ok(opened);
    const selected = opened.items[opened.index];
    assert.strictEqual(selected.url, expected);
    const rendered = ctx.frameRenderItems(frames, slots).find(item => item.sequence === seq);
    assert.strictEqual(selected.url, rendered.frame.url || rendered.frame.file);
    assert.ok(selected.caption.includes(`第 ${seq} 拍`));
}
const slots = [1, 2, 4].map(index => ({ type: 'image', index }));
check([{ sequence: 1, url: '/1.png' }, { slot: 2, file: '/2.png' }], slots, 2, '/2.png');
check([{ sequence: '4', url: '/4.png' }, { sequence: '1', url: '/1.png' }], slots, 4, '/4.png');
check([{ url: '/1.png' }, { url: '/2.png' }], [], 2, '/2.png');
check([{ slot: 4, url: '/4.png' }], [], 4, '/4.png');
check([{ sequence: 1, url: '/1.png' }], slots, 2, null);
// 冲突字段必须优先按 sequence 配对，不能取到先出现的旧 slot。
check([{ sequence: 1, slot: 2, url: '/1.png' }, { sequence: 2, url: '/2.png' }], slots, 2, '/2.png');
// 更新清单后重新点击，读取新帧，不沿用旧快照。
check([{ sequence: 2, url: '/updated.png' }], slots, 2, '/updated.png');
console.log('slot lightbox consistency tests passed');
