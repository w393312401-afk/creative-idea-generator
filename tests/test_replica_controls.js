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

// ── 3b. 反推通道（极速直读 / 标准深度）─────────────────────────────────────────
// 后端两条链路一直都在（run_reverse 按 reverseMode 分流），失败方式和上面几个旋钮
// 一样：选了但没生效。这里盯三件事——默认必须是极速（换默认等于给每一单静默涨价）、
// 老键 deepReverse 认得出来、写回时新老键一起写（只写新键的话残留的 deepReverse=true
// 会把「极速」这一项永远锁死）。
call(`config = {}`);
assert.equal(call('replicaReverseChannel()'), 'fast', '默认必须是极速直读');
call(`config = { reverseMode: 'deep' }`);
assert.equal(call('replicaReverseChannel()'), 'deep');
call(`config = { deepReverse: true }`);
assert.equal(call('replicaReverseChannel()'), 'deep', '老键 deepReverse 也要认');
call(`config = { reverseMode: 'fast' }`);
assert.equal(call('replicaReverseChannel()'), 'fast');

// 写回：模拟卡点上选中「标准深度」，再选回「极速」。
const fakeRoot = (channelValue) => ({
    querySelector(sel) {
        if (sel === 'input[name="replica-reverse-channel"]:checked') return { value: channelValue };
        return null;   // 模型下拉在这一节不参与
    },
});
call(`config = {}`);
const realReplicaRoot = sandbox.replicaRoot;
sandbox.replicaRoot = () => fakeRoot('deep');
call('replicaCaptureReverseSettings()');
assert.equal(call('config.reverseMode'), 'deep');
assert.equal(call('config.deepReverse'), true);
sandbox.replicaRoot = () => fakeRoot('fast');
call('replicaCaptureReverseSettings()');
assert.equal(call('config.reverseMode'), 'fast');
assert.equal(call('config.deepReverse'), false, '选回极速必须把老键一并清掉');
assert.equal(JSON.parse(call(`localStorage.getItem('spark_config')`)).reverseMode, 'fast');
sandbox.replicaRoot = realReplicaRoot;   // 后面几节还要用真的那个

// ── 3c. 合成通道（极速直通 / 标准合成）─────────────────────────────────────────
// 与反推通道同形，但失败更隐蔽：两条通道产出的提示词包长得一样，只有空间锁定包不同。
call(`config = {}`);
assert.equal(call('replicaComposeChannel()'), 'fast', '默认必须是极速直通');
assert.equal(call('replicaComposeChannelLabel()'), '极速直通');
call(`config = { composeMode: 'deep' }`);
assert.equal(call('replicaComposeChannel()'), 'deep');
assert.equal(call('replicaComposeChannelLabel()'), '标准合成');
call(`config = { deepCompose: true }`);
assert.equal(call('replicaComposeChannel()'), 'deep', '老键 deepCompose 也要认');

const fakeComposeRoot = (v) => ({
    querySelector(sel) {
        return sel === 'input[name="replica-compose-channel"]:checked' ? { value: v } : null;
    },
});
call(`config = {}`);
const realRoot2 = sandbox.replicaRoot;
sandbox.replicaRoot = () => fakeComposeRoot('deep');
call('replicaCaptureComposeChannel()');
assert.equal(call('config.composeMode'), 'deep');
assert.equal(call('config.deepCompose'), true);
sandbox.replicaRoot = () => fakeComposeRoot('fast');
call('replicaCaptureComposeChannel()');
assert.equal(call('config.composeMode'), 'fast');
assert.equal(call('config.deepCompose'), false, '选回极速必须把老键一并清掉');
sandbox.replicaRoot = realRoot2;

// 吸底栏那颗 CTA 必须写明这一按走哪条通道：通道选择块在节拍卡片底部，可能离它很远，
// 而两条通道的差别（3~5 分钟 vs 30 秒、贴不贴原片）恰恰是按下去之前要知道的。
call(`config = { composeMode: 'deep' }`);
const barDeep = call(`replicaRenderBottomBar({ stage: 'review_beats', beats: { beats: [{ id: 'B01' }] }, validation: [] })`);
assert.ok(barDeep.includes('合成提示词（标准合成）'), '吸底 CTA 要带通道名');
call(`config = { composeMode: 'fast' }`);
const barFast = call(`replicaRenderBottomBar({ stage: 'review_beats', beats: { beats: [{ id: 'B01' }] }, validation: [] })`);
assert.ok(barFast.includes('合成提示词（极速直通）'));

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
// AI 修复只有一个入口，就在硬伤清单旁边（2026-08-25 从两个收回成一个，
// 理由见 test_replica_layout.js 第 6 节）。
assert.ok(beatsHtml.includes('id="replica-banner-autofix-btn"'), '硬伤横幅必须包含一键 AI 修复按钮');
assert.ok(!beatsHtml.includes('id="replica-autofix-btn"'), 'AI 修复不再在底部动作排出现第二次');
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
    // 节拍字段与下拉框：replicaSetBusy 现在也要锁它们（跑着的时候改的字会被这一轮的
    // 结果整份覆盖）。mock 必须按选择器分发——早先它对任何选择器都回同一份按钮数组，
    // 于是"锁输入框"那两个循环会把取消按钮一起 disable 掉，测试反而先炸。
    const mockInputs = [
        { readOnly: false, title: '', classList: { toggle: () => {} }, removeAttribute() { this.title = ''; } },
        { readOnly: false, title: '', classList: { toggle: () => {} }, removeAttribute() { this.title = ''; } },
    ];
    const mockSelects = [
        { disabled: false, classList: { toggle: () => {} } },
    ];
    const pick = (sel) => {
        if (sel === 'button') return mockButtons;
        if (sel === REPLICA_BEAT_SELECT_SELECTOR) return mockSelects;
        if (sel === REPLICA_BEAT_INPUT_SELECTOR) return mockInputs;
        return [];
    };
    document.getElementById = (id) => (id === 'replica-root' ? { querySelectorAll: pick } : null);
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

// ── 7. 运行中节拍字段必须只读 ─────────────────────────────────────────────────
// 此前 replicaSetBusy 只遍历 button，textarea 全程可编辑：autofix / 工艺精修跑完会
// 整份替换 replicaState.beats，那几分钟里敲的每一个键都写进一份马上被丢掉的文档，
// 结束时无声消失。用 readOnly 而不是 disabled——文本必须还能选中复制。
const inputs = call('mockInputs');
const selects = call('mockSelects');
assert.ok(inputs.every(el => el.readOnly === true), 'busy 态下节拍输入框必须只读');
assert.ok(inputs.every(el => !!el.title), 'busy 态下必须说清为什么不能改');
assert.ok(selects.every(el => el.disabled === true),
          'busy 态下施工阶段下拉框必须禁用（readOnly 对 select 无效）');

// 恢复 non-busy 态
call('replicaSetBusy(false);');
assert.ok(inputs.every(el => el.readOnly === false), '解除 busy 后节拍输入框必须恢复可编辑');
assert.ok(inputs.every(el => !el.title), '解除 busy 后那句解释必须撤掉');
assert.ok(selects.every(el => el.disabled === false), '解除 busy 后下拉框必须恢复可用');
assert.equal(uploadBtn.disabled, false, '解除 busy 后动作按钮应恢复可用');
assert.equal(topBtn.disabled, false, '解除 busy 后回到顶部按钮保持可用');
assert.equal(permBtn.disabled, true, '解除 busy 后带 data-perm-disabled 的按钮依然保持禁用');

// ── 「清理合成缓存」开关 ──────────────────────────────────────────────────
//
// 这个开关的失败方式和上面三个旋钮一模一样：勾了但没生效。它只在能合成的两个阶段
// 出现（review_beats 与 audit_failed/compose_failed 的重新合成），且勾选态要能挺过
// 一次吸底栏重建——那条栏每保存一次节拍就整段重建一回，状态存 DOM 里必被清掉。
const barFor = (stage, extra) => {
    call('replicaState = ' + JSON.stringify(Object.assign({
        job_id: 'job_a', video_name: 'demo.mp4', stage,
        beats: { beats: [{ id: 'B01' }], banned_elements: [] },
        validation: [],
    }, extra || {})));
    return call('replicaRenderBottomBar(replicaState)');
};

assert.match(barFor('review_beats'), /id="replica-reset-cache"/,
             '节拍卡点的吸底栏上必须有「清理合成缓存」开关');
assert.match(barFor('audit_failed', { prompt_block: 'P' }), /id="replica-reset-cache"/,
             '「重新合成」旁边同样要有这个开关——改完规则重跑走的正是这条路');
assert.match(barFor('completed', { prompt_block: 'P', title: 'T' }),
             /id="replica-reset-cache"/,
             '已完成态支持重新合成提示词，必须有「清理合成缓存」开关');
assert.match(barFor('completed', { prompt_block: 'P', title: 'T' }),
             /id="replica-bar-recompose-btn"/,
             '已完成态必须有「重新合成」按钮，无需重跑聚类即可重新制作提示词');

call('replicaResetCache = false;');
assert.doesNotMatch(barFor('review_beats'), /id="replica-reset-cache" checked/,
                    '默认不勾：断点续传省的是几分钟大模型钱');
call('replicaResetCache = true;');
assert.match(barFor('review_beats'), /id="replica-reset-cache" checked/,
             '勾选态存在模块变量里，吸底栏重建后必须还原成勾上');
call('replicaResetCache = false;');

// ── 逐拍拍摄角度（2026-08-25）─────────────────────────────────────────────
// 俯仰与方位是两根互相独立的轴，卡片上必须是两个下拉：同一拍可以既是低角度仰拍、
// 又是从侧面拍的，捏成一栏就得二选一。闭集给下拉不给输入框——自由文本会被规划器
// 当创作提示接着发挥。
assert.ok(beatsHtml.includes('data-key="camera_angle"'), '节拍卡必须有拍摄角度下拉');
assert.ok(beatsHtml.includes('data-key="camera_bearing"'), '节拍卡必须有机位方位下拉');
assert.ok(beatsHtml.includes('低角度仰拍'), '角度选项要写人话，不能只有英文枚举值');
assert.ok(beatsHtml.includes('虫视'), '虫视/鸟瞰这类极端角度必须可选');
assert.ok(!beatsHtml.includes('data-key="camera_angle"><textarea'), '角度是闭集，不给输入框');

// ── 焦段 / 构图 / 时间处理（2026-08-25）───────────────────────────────────
assert.ok(beatsHtml.includes('data-key="lens_feel"'), '节拍卡必须有焦段感下拉');
assert.ok(beatsHtml.includes('data-key="subject_placement"'), '节拍卡必须有主体构图输入');
assert.ok(beatsHtml.includes('data-key="time_treatment"'), '节拍卡必须有时间处理下拉');
assert.ok(beatsHtml.includes('长焦'), '焦段选项要写人话');
assert.ok(beatsHtml.includes('实时'), '时间处理必须能标成实时——成品巡览拍不是延时');
// 构图要说清写什么：位置、占比、地平线，且分数写汉字（数字会被图像模型画进画面）
assert.ok(beatsHtml.includes('几分之几'), '构图栏要交代占比怎么写');

// ── 全局识别项：人物外形（2026-08-24）─────────────────────────────────────
// 场景恒常特征是「整片一直存在、会写进每一条提示词」的那一栏。人物外形属于它：
// 工序每拍都变，穿的那件衣服不变。它必须和其余各栏一样可编辑——统计与模型都会误判，
// 用户删得掉才敢让它进每一条提示词。
const sceneHtml = call(`replicaRenderSceneConstants({
    scene_signature: '一座长着青苔的混凝土掩体',
    scene_constants: { cast: ['浅棕肤色的南亚男性，短黑发，褪色红长袖T恤，深蓝牛仔裤，棕色皮靴'] }
})`);
assert.ok(sceneHtml.includes('data-scene-key="cast"'), '场景恒常特征必须有人物外形一栏');
assert.ok(sceneHtml.includes('data-scene-key="grade"'), '场景恒常特征必须有全片影调一栏');
assert.ok(sceneHtml.includes('data-scene-key="ambient_sound"'), '场景恒常特征必须有常驻环境声一栏');
assert.ok(sceneHtml.includes('别写「电影感」'), '影调栏要挡住情绪词——那不是影调');
assert.ok(sceneHtml.includes('人种'), '人物栏必须点名人种/肤色——它是「同一个人」的判据');
assert.ok(sceneHtml.includes('褪色红长袖T恤'), '已有的人物读数要回填进输入框');

console.log('test_replica_controls.js: all assertions passed');

