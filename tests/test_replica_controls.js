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

// ── 4. 节拍阶梯硬伤时的 AI 修复按钮渲染 ──────────────────────────────────────
const sampleStateWithErrors = {
    beats: {
        beats: [
            { id: 'B01', start: 0, end: 5, stage: 'demolition' },
            { id: 'B02', start: 5, end: 10, stage: 'structural' },
        ],
        banned_elements: [],
    },
    validation: [
        { level: 'error', message: 'B02 违反施工顺序', beat_id: 'B02' }
    ]
};
call(`replicaState = ${JSON.stringify(sampleStateWithErrors)}`);
const beatsHtml = call(`replicaRenderBeats(replicaState)`);
assert.ok(beatsHtml.includes('id="replica-autofix-btn"'), '底部操作栏必须包含 AI 修复按钮');
assert.ok(beatsHtml.includes('id="replica-banner-autofix-btn"'), '硬伤横幅必须包含一键 AI 修复按钮');
assert.ok(beatsHtml.includes('AI 修复全部硬伤'), '横幅修复文案正确');
// 工艺精修与 AI 修复硬伤是两条路：那条修被判死的硬伤（有权改 stage/工序包），
// 这条只改措辞、画面内容一个字不动。0 硬伤的阶梯上只有这一条能动工艺 warn。
assert.ok(beatsHtml.includes('id="replica-refine-craft-btn"'), '底部操作栏必须包含工艺精修按钮');
assert.ok(beatsHtml.includes('不动 1:1'), '精修按钮必须写明它不改画面内容');

// ── 5. 变体任务折叠/展开下拉功能测试 ───────────────────────────────────────
const sampleJobs = [
    { job_id: 'job_parent', stage: 'completed', title: '母本视频', job_type: 'baseline' },
    { job_id: 'job_var1', stage: 'review_beats', title: '变体1', variant_of: 'job_parent', parent_baseline_id: 'job_parent', job_type: 'variant' },
    { job_id: 'job_var2', stage: 'completed', title: '变体2', variant_of: 'job_parent', parent_baseline_id: 'job_parent', job_type: 'variant' },
];
call(`replicaJobs = ${JSON.stringify(sampleJobs)}; replicaVariantFoldState = {};`);

// 默认折叠：母本行渲染，但变体行未展开挂载在母本下
let listHtml = call(`replicaRenderJobList()`);
assert.ok(listHtml.includes('👑 2 个变体 ▼'), '母本应渲染折叠状态的变体下拉按钮');

// 模拟点击展开变体
call(`
    const parentId = 'job_parent';
    const currentlyFolded = replicaVariantFoldState[parentId] !== false;
    replicaVariantFoldState[parentId] = !currentlyFolded;
`);
listHtml = call(`replicaRenderJobList()`);
assert.ok(listHtml.includes('👑 2 个变体 ▲'), '展开后下拉按钮应切换为收起箭头');
assert.ok(listHtml.includes('replica-job-variant-row'), '展开后应渲染子变体行');

// 模拟再次点击折叠
call(`
    const currentlyFolded2 = replicaVariantFoldState[parentId] !== false;
    replicaVariantFoldState[parentId] = !currentlyFolded2;
`);
// ── 6. 运行/忙碌态下导航与回到顶部等纯浏览操作不被 disable ─────────────────
// 在任务执行中（replicaSetBusy 为 true），导航栏、回到顶部、取消中断等交互必须保持可用。
call(`
    const mockButtons = [
        { id: 'replica-cancel-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: 'replica-bar-cancel-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: 'replica-bar-errors-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: '', dataset: { floatAction: 'top' }, classList: { contains: (c) => c === 'replica-float-btn', toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: '', dataset: { floatAction: 'save' }, classList: { contains: (c) => c === 'replica-float-save', toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: '', dataset: { navTarget: 'replica-sec-beats' }, classList: { contains: (c) => c === 'replica-nav-item', toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: '', dataset: { jumpBeat: 'B01' }, classList: { contains: (c) => c === 'replica-jump', toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: 'replica-upload-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: 'replica-bar-start-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: () => false, disabled: false },
        { id: 'perm-disabled-btn', dataset: {}, classList: { contains: () => false, toggle: () => {} }, hasAttribute: (a) => a === 'data-perm-disabled', disabled: false },
    ];
    document.getElementById = (id) => (id === 'replica-root' ? { querySelectorAll: () => mockButtons } : null);
    replicaSetBusy(true);
`);

const btns = call('mockButtons');
const cancelBtn1 = btns.find(b => b.id === 'replica-cancel-btn');
const cancelBtn2 = btns.find(b => b.id === 'replica-bar-cancel-btn');
const topBtn = btns.find(b => b.dataset.floatAction === 'top');
const navBtn = btns.find(b => b.classList.contains('replica-nav-item'));
const jumpBtn = btns.find(b => b.dataset.jumpBeat === 'B01');
const uploadBtn = btns.find(b => b.id === 'replica-upload-btn');
const startBtn = btns.find(b => b.id === 'replica-bar-start-btn');
const saveBtn = btns.find(b => b.dataset.floatAction === 'save');
const permBtn = btns.find(b => b.id === 'perm-disabled-btn');

assert.equal(cancelBtn1.disabled, false, 'replica-cancel-btn 在 busy 态下不应被禁用');
assert.equal(cancelBtn2.disabled, false, 'replica-bar-cancel-btn 在 busy 态下不应被禁用');
assert.equal(topBtn.disabled, false, '回到顶部按钮 [data-float-action="top"] 在 busy 态下不应被禁用');
assert.equal(navBtn.disabled, false, '吸顶导航项 .replica-nav-item 在 busy 态下不应被禁用');
assert.equal(jumpBtn.disabled, false, '跳轨定位按钮 [data-jump-beat] 在 busy 态下不应被禁用');

assert.equal(uploadBtn.disabled, true, '动作按钮 replica-upload-btn 在 busy 态下必须被禁用');
assert.equal(startBtn.disabled, true, '动作按钮 replica-bar-start-btn 在 busy 态下必须被禁用');
assert.equal(saveBtn.disabled, true, '保存按钮 replica-float-save 在 busy 态下必须被禁用');
assert.equal(permBtn.disabled, true, '带 data-perm-disabled 的按钮应始终禁用');

// 恢复 non-busy 态
call('replicaSetBusy(false);');
assert.equal(uploadBtn.disabled, false, '解除 busy 后动作按钮应恢复可用');
assert.equal(topBtn.disabled, false, '解除 busy 后回到顶部按钮保持可用');
assert.equal(permBtn.disabled, true, '解除 busy 后带 data-perm-disabled 的按钮依然保持禁用');

console.log('test_replica_controls.js: all assertions passed');

