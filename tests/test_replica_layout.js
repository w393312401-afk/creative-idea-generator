// 复刻页的布局骨架：吸顶区段导航、吸底主操作栏、二创栏的出现条件。
//
// 这三样的失败方式都是**静默**的，页面照常渲染、控制台一片干净：
//   · 导航项指向一个不存在的锚点 —— 点它不滚动、不报错、什么都不发生；
//   · 同一个主操作在一屏之内出现两次 —— 用户得先判断这两个是不是同一个按钮；
//   · 还没有节拍就摆出「一键生成二创变体」—— 按下去只能失败（骨架都还没有）。
// 所以这里盯的是「哪一块在什么条件下出现」，不是样式。
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
// replicaState / replicaJobs 是 let 绑定，挂不到 sandbox 对象上，只能在上下文里赋值。
const call = (expr) => vm.runInContext(expr, sandbox);
const setState = (st) => call('replicaState = ' + JSON.stringify(st));

const beat = (id, start, end, stage, space) => ({
    id, start, end, stage, space,
    visual_subject: 'x', operation: 'op', package_operations: ['a', 'b'],
    visible_action: 'a', visible_result: 'r', state_before: 'b', state_after: 'af',
    persistent_traces: ['t1', 't2'], evidence_frames: ['f1.jpg'], workers_present: true,
    coverage_frames: [{ frame: 'c1.jpg', timestamp: 3 }],
});

const base = {
    job_id: 'job_a', video_name: 'demo.mp4',
    overview: { duration_sec: 60, frame_count: 96, change_event_count: 8, analysis_plan: { mode: 'plan' } },
    cost_estimate: { full: { frame_count: 38, batch_count: 12 }, degraded: {}, all: {} },
    frame_urls: {},
};

const withBeats = Object.assign({}, base, {
    stage: 'review_beats',
    beats: {
        beats: [beat('B01', 0, 5, 'demolition', 'main room'), beat('B02', 5, 10, 'surface', 'attic')],
        banned_elements: ['excavator'],
        scene_signature: 'a bunker',
        scene_constants: { materials: ['concrete'] },
        time_windows: [{
            start: 0, end: 5, frame_count: 10, workers_present_ratio: 0.8,
            appeared: ['x'], vanished: [], brief: [], baseline: [],
        }],
    },
    validation: [{ level: 'error', beat_id: 'B01', message: 'B01 违反施工依赖顺序' }],
});

const cases = {
    confirm_cost: Object.assign({}, base, { stage: 'confirm_cost' }),
    review_frames: Object.assign({}, base, { stage: 'review_frames' }),
    review_beats: withBeats,
    completed: Object.assign({}, withBeats, {
        stage: 'completed', validation: [], prompt_block: 'PROMPT', title: 'T',
    }),
};

call('replicaJobs = ' + JSON.stringify(
    [{ job_id: 'job_a', video_name: 'demo.mp4', stage: 'review_beats', beat_count: 2 }]));

const wholePage = () => call(
    'replicaRenderNavBar(replicaState) + replicaRenderHeaderToolbar(replicaState)'
    + ' + replicaRenderUploader() + replicaRenderJobList()'
    + ' + replicaRenderJob(replicaState) + replicaRenderBottomBar(replicaState)');

for (const [name, state] of Object.entries(cases)) {
    setState(state);
    const html = wholePage();
    assert.ok(html.length > 500, `${name}: 渲染结果异常地短，多半是某个分支抛了`);

    // ── 1. 每个导航项都必须指向页面上真实存在的锚点 ────────────────────────────
    // 导航项的出现条件与对应区块的渲染条件写在两个地方，一改一漏就会出现死药丸。
    const navTargets = [...html.matchAll(/data-nav-target="([^"]+)"/g)].map(m => m[1]);
    for (const target of navTargets) {
        assert.ok(html.includes(`id="${target}"`),
                  `${name}: 导航项 #${target} 在页面上没有对应的锚点，点了会毫无反应`);
    }

    // ── 2. 主操作在一页里只出现一次 ────────────────────────────────────────────
    // 保存与合成常驻吸底操作栏，节拍区底部那一排不再重复它们。
    for (const label of ['保存并重校验', '合成提示词']) {
        const hits = (html.match(new RegExp('>\\s*' + label + '\\s*<', 'g')) || []).length;
        assert.ok(hits <= 1, `${name}: 「${label}」作为按钮出现了 ${hits} 次`);
    }

    // ── 3. 没有节拍就没有二创 ──────────────────────────────────────────────────
    // 二创是「骨架不动、只换内容」，骨架都还没有的时候那个入口不该存在。
    if (!(state.beats && state.beats.beats.length)) {
        assert.ok(!html.includes('replica-sec-variant'), `${name}: 还没有节拍就渲染了二创栏`);
        assert.ok(!html.includes('一键生成二创变体'), `${name}: 还没有节拍就渲染了二创按钮`);
    }
}

// ── 4. 双轨对比器不许再拿滤镜冒充变体 ──────────────────────────────────────────
// 右轨此前是同一张母本拼图加 `filter: saturate(1.2)`，而标题写着「肉眼 3 秒判定漂移」——
// 它永远判不出漂移。变体在这个阶段本来就没有画面（它派生的是提示词，不抽自己的帧）。
setState(withBeats);
call('replicaComparatorOpen = true');
const comparator = call('replicaRenderDualTrackComparator(replicaState)');
assert.ok(!comparator.includes('saturate('), '右轨不能再用滤镜伪造变体拼图');
assert.ok(comparator.includes('replica-comparator-empty'), '右轨应当给出空态与去处');
call('replicaComparatorOpen = false');

// ── 5. 吸底栏按校验结果给状态 ──────────────────────────────────────────────────
setState(withBeats);
const barWithErrors = call('replicaRenderBottomBar(replicaState)');
assert.ok(barWithErrors.includes('id="replica-bar-errors-btn"'),
          '硬伤计数要可点——它是这条栏上唯一说得出问题在哪的东西');
assert.ok(barWithErrors.includes('disabled'), '有硬伤时「合成提示词」必须禁用');
assert.ok(!barWithErrors.includes('replica-bar-autofix-btn'),
          'AI 修复挨着硬伤清单放（横幅 + 节拍区），吸底栏不再放第三个');

setState(cases.completed);
assert.ok(call('replicaRenderBottomBar(replicaState)').includes('replica-chip-ok'),
          '通过态走 .replica-chip-ok，不用内联颜色');

// ── 6. 每个动作只有一个入口，且长在它要修的那个东西旁边 ────────────────────────
//
// 2026-08-25：此前「AI 修复硬伤」在节拍区里有**两个**入口（硬伤横幅 + 区段底部
// 动作排），是当时刻意留的。现在收回成一个，理由有两条：
//   · 底部那一枚不是条件渲染的，0 硬伤时照样摆在那里——点下去无事发生。本文件
//     第 5 行立的规矩（同一个主操作不在一屏内出现两次）和 replica_pipeline.js 里
//     「摆一个点了必然报错的按钮比不摆更糟」是同一条，这一枚两条都犯了。
//   · 「离得远够不着」这个担心不成立：吸底栏上的「⚠️ N 项硬伤」本来就是一枚跳到
//     横幅的按钮，且常驻可见。从阶梯任何位置到那唯一的修复入口都只要一下。
// 同理，「自动平衡秒数/拆拍」也从底部动作排收回到比例条头部——超长/微拍的标记
// 就画在那条上。
setState(withBeats);
const beats = call('replicaRenderBeats(replicaState)');
assert.ok(beats.includes('id="replica-banner-autofix-btn"'), 'AI 修复挂在硬伤横幅上');
assert.ok(!beats.includes('id="replica-autofix-btn"'),
          'AI 修复只有横幅那一个入口，底部动作排不再放第二个');
assert.ok(beats.includes('id="replica-autobalance-btn"'), '自动平衡挂在比例条头部');
assert.ok(!beats.includes('id="replica-actions-autobalance-btn"'),
          '自动平衡只有比例条那一个入口，底部动作排不再放第二个');
assert.ok(!beats.includes('id="replica-save-btn"'), '节拍区不再重复「保存并重校验」');
assert.ok(!beats.includes('id="replica-compose-btn"'), '节拍区不再重复「合成提示词」');

// 全片概览合并成一块：比例条只画时间分配，跳轨条只管谁有事、点它去哪。
assert.ok(beats.includes('replica-ladder-overview'), '缺全片概览');
assert.ok(beats.includes('replica-ladder-bar'), '缺时长比例条');
assert.ok(beats.includes('replica-ladder-chips'), '缺跳轨条');
assert.ok(!beats.includes('replica-timeline-block'),
          '旧的「胶卷时间轴」块已并入比例条，不该再渲染');
assert.ok(beats.includes('id="replica-toggle-fold-all"'), '全部折叠/展开并进了概览头部');
// 比例条上的每一段都必须是 button：此前是 <div>，能点但键盘走不到。
assert.ok(/<button[^>]*class="[^"]*replica-ladder-seg/.test(beats),
          '比例条的每一段是 button，不是可点的 div');

// ── 8. 任务列表按注意力分组、成本醒目与血缘折叠 ─────────────────────────
call('replicaJobs = ' + JSON.stringify([
    {
        job_id: 'job_parent',
        title: '母本房屋改造',
        stage: 'confirm_cost',
        attention: 'waiting_you',
        cost_estimate: { full: { frame_count: 80, batch_count: 2 } },
        lineage_variants: ['job_variant_1'],
    },
    {
        job_id: 'job_variant_1',
        title: '母本房屋改造 · 赛博朋克',
        variant_of: 'job_parent',
        stage: 'review_beats',
        attention: 'waiting_you',
        beat_count: 5,
    },
    {
        job_id: 'job_completed',
        title: '完成的地下室',
        stage: 'completed',
        attention: 'done',
        beat_count: 6,
    },
    {
        job_id: 'job_archived',
        title: '老旧归档任务',
        stage: 'archived',
        attention: 'stalled',
        archived: true,
    },
]));

const jobListHtml = call('replicaRenderJobList()');
assert.ok(jobListHtml.includes('待你处理'), '列表必须展示「待你处理」分组');
assert.ok(jobListHtml.includes('💰 待确认: 80 帧 / 约 2 次调用'), 'confirm_cost 阶段必须直接在行上显示预估成本');
assert.ok(jobListHtml.includes('👑 1 个变体'), '母本必须展示变体折叠按钮');
assert.ok(jobListHtml.includes('data-rename="job_parent"'), '每行必须有改名按钮');
assert.ok(jobListHtml.includes('data-archive="job_parent"'), '未归档任务必须有归档按钮');
assert.ok(!jobListHtml.includes('data-archive="job_archived"'), '已归档任务不再显示归档按钮');
assert.ok(jobListHtml.includes('id="replica-job-search-input"'), '必须包含搜索过滤输入框');

// 搜索过滤生效验证
call('replicaJobListSearchQuery = "赛博朋克"');
const searchFilteredHtml = call('replicaRenderJobList()');
assert.ok(searchFilteredHtml.includes('赛博朋克'), '搜索命中项应当保留');
assert.ok(!searchFilteredHtml.includes('完成的地下室'), '搜索未命中项应当被过滤');
call('replicaJobListSearchQuery = ""');

// 展开血缘变体测试
call('replicaVariantFoldState["job_parent"] = false');
const unfoldedHtml = call('replicaRenderJobList()');
assert.ok(unfoldedHtml.includes('replica-job-variant-row'), '展开后应当渲染缩进变体行');

console.log('test_replica_layout.js: all assertions passed');

