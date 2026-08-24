// 人工卡点上的空间标记：这一拍在哪个空间、哪一拍是过门。
//
// 过门是复刻里最容易整段消失的东西——原片进了三次门，成片只进了一次，而卡点上
// 一个字都看不出来（2026-08-14 复盘）。合成期按逐拍 space 序列标过门，所以卡点必须
// 让人一眼数出「有几处过门」：少标一处就是少走进一个房间，多标一处就是凭空多一次穿墙。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sandbox = {
    console,
    document: {
        readyState: 'complete',
        addEventListener() {},
        getElementById: () => null,
        querySelectorAll: () => [],
    },
    EventSource: function () {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    localStorage: { store: {}, setItem() {}, getItem: () => null },
};
// 模块在顶层给 window / document 挂了全局守卫（beforeunload 未保存拦截、Cmd+S）。
// 沙箱里 window 就是 sandbox 本身，不给它一个 addEventListener，整个文件在加载时就炸。
sandbox.addEventListener = () => {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const file of ['progress_model.js', 'replica_pipeline.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', file), 'utf8'),
                    sandbox, { filename: file });
}
const chip = (beat, idx, previous) => vm.runInContext(
    `replicaSpaceChip(${JSON.stringify(beat)}, ${idx}, ${JSON.stringify(previous || '')})`, sandbox);

// 换了空间名 = 这一拍机位穿过一道开口，卡点上必须写明。
const crossed = chip({ space: 'sleeping alcove' }, 9, 'main room');
assert.ok(crossed.includes('过门'), '换空间的那一拍要标成过门');
assert.ok(crossed.includes('sleeping alcove'));
assert.ok(crossed.includes('replica-chip-cross'), '过门 chip 有自己的样式，不能和普通 chip 一样淡');

// 同一个空间连着拍：只显示空间名，不标过门。大小写/空格不算换空间。
assert.ok(!chip({ space: 'main room' }, 5, 'Main Room').includes('过门'));
assert.ok(chip({ space: 'main room' }, 5, 'main room').includes('main room'));

// 首拍没有上一拍可比，永远不是过门。
assert.ok(!chip({ space: 'wooded slope' }, 0, '').includes('过门'));

// 存量任务（2026-08-14 之前跑的）没有这个字段：整块不渲染，不留一个空 chip。
assert.equal(chip({}, 3, 'main room'), '');

console.log('test_replica_space_chip.js: all assertions passed');
