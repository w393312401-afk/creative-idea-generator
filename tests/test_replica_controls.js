// 复刻页成本确认卡点上的两个旋钮：抽帧密度、送审档位，外加反推段的模型选择。
//
// 这三个控件的失败方式都是「选了但没生效」——页面照常渲染、请求照常发出、
// 后端收到的却是默认值，一路跑完才在账单和节拍精度上看出来。所以盯的是
// 渲染出的选中态与档位翻译，而不是样式。
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
    localStorage: {
        store: {},
        setItem(k, v) { this.store[k] = v; },
        getItem(k) { return this.store[k] || null; },
    },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const file of ['progress_model.js', 'replica_pipeline.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', file), 'utf8'),
                    sandbox, { filename: file });
}
const call = (expr) => vm.runInContext(expr, sandbox);

// ── 1. 送审档位的翻译 ─────────────────────────────────────────────────────────
// 单选框的 value 'full' 是两档时代留下来的，后端叫它 'plan'。翻译错了不会报错，
// 只会静默换档——而档位决定的是这一单送多少帧、花多少钱。
assert.equal(call(`replicaScopeFromMode('all')`), 'all');
assert.equal(call(`replicaScopeFromMode('full')`), 'plan');
assert.equal(call(`replicaScopeFromMode('degraded')`), 'degraded');
// 认不出来的一律回落到计划档，不能落到最贵的「全部」。
assert.equal(call(`replicaScopeFromMode(undefined)`), 'plan');
assert.equal(call(`replicaScopeFromMode('everything')`), 'plan');

// ── 2. 抽帧密度 ───────────────────────────────────────────────────────────────
// 没抽过帧、或者状态里存着一个不认识的值：都回默认档，而不是渲染出一个空下拉。
assert.equal(call('replicaCurrentFps(null)'), 2);
assert.equal(call(`replicaCurrentFps({ sampling: { base_fps: 4 } })`), 4);
assert.equal(call(`replicaCurrentFps({ sampling: { base_fps: 99 } })`), 2);

const fpsSelect = call(`replicaFpsSelect('replica-base-fps', 4)`);
assert.ok(fpsSelect.includes('value="4" selected'), '当前密度必须是选中项');
assert.equal((fpsSelect.match(/selected/g) || []).length, 1, '只能有一个选中项');

// ── 3. 反推模型选择 ───────────────────────────────────────────────────────────
// state.js 没加载时退回内置清单：空下拉等于这个选择器不存在。
const models = call('replicaModelChoices()');
assert.ok(models.length >= 2 && models.every(m => m.value && m.label));

// 配置文件里手写过的自定义模型名不在清单里。补进去，否则下拉自己跳到第一项，
// 用户一保存就把配置里的选择改掉了——而且看不出发生过什么。
const custom = call(`replicaModelSelect('replica-frame-model', 'my-private-vlm')`);
assert.ok(custom.includes('my-private-vlm'), '当前值必须出现在下拉里');
assert.ok(/my-private-vlm[^<]*（自定义）/.test(custom), '自定义值要标出来');
assert.equal((custom.match(/selected/g) || []).length, 1);

// 峰值复核的两个特殊项（跟随主模型 / 关闭）排在模型清单前面。
const peak = call(`replicaModelSelect('replica-peak-model', 'off',
    [{ value: '', label: '跟随主模型（默认）' }, { value: 'off', label: '关闭复核' }])`);
assert.ok(peak.indexOf('关闭复核') < peak.indexOf(models[0].label));
assert.ok(peak.includes('value="off" selected'));

// 选择写回全局 config + localStorage：不落盘的话下次开页面又回默认值，
// 而 recluster 这类路径读的正是 config。
call(`config = {}; replicaSetConfigValue('frameFactsModel', 'gemini-3.1-pro-high')`);
assert.equal(call(`config.frameFactsModel`), 'gemini-3.1-pro-high');
assert.equal(JSON.parse(call(`localStorage.getItem('spark_config')`)).frameFactsModel,
             'gemini-3.1-pro-high');
assert.equal(call(`replicaConfigValue('peakVerifyModel', 'fallback')`), 'fallback');

console.log('test_replica_controls.js: all assertions passed');
