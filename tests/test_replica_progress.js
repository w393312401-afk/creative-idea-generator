// 复刻页的进度条 + 证据帧灯箱。
//
// 这两块都是「不报错的静默失效」高发区，正是需要测试盯住的形态：
//   · SSE 帧带信封（{type,data}），读错一层不会报错，只是永远显示空白；
//   · 合成阶段的事件如果没人监听，页面就是一句话之后静止好几分钟。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(id) {
    return {
        id,
        style: {},
        textContent: '',
        innerHTML: '',
        dataset: {},
        children: [],
        addEventListener() {},
        removeAttribute() {},
        appendChild() {},
        closest() { return null; },
        querySelector(sel) { return this.children.find(c => `#${c.id}` === sel) || null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    };
}

const progressBox = element('replica-progress');
progressBox.children = ['replica-progress-stage', 'replica-progress-label',
                        'replica-progress-percent', 'replica-progress-fill',
                        'replica-progress-log'].map(element);

const root = element('replica-root');
root.children = [progressBox];

const sandbox = {
    console,
    document: {
        readyState: 'complete',
        addEventListener() {},
        getElementById: (id) => (id === 'replica-root' ? root : null),
        querySelectorAll: () => [],
        querySelector: () => null,
        // 推进动作会走 replicaToast（建节点挂 body）与 replicaShell（root.closest）。
        createElement: (tag) => element(`<${tag}>`),
        body: { appendChild() {} },
    },
    EventSource: function () {},
    setTimeout: () => 1,
    clearTimeout: () => {},
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

// ── 1. SSE 信封 ───────────────────────────────────────────────────────────────
// server._open_sse_stream 发的是 {"type": ..., "data": ...}。读成 .stage/.message
// 恒为 undefined，页面照常渲染一个空 chip——不报任何错，所以只能靠测试盯住。
assert.equal(
    call(`replicaEventPayload({ data: JSON.stringify({ type: 'replica_stage', data: { stage: 'compose', message: '在跑' } }) }).message`),
    '在跑');
assert.equal(
    call(`replicaEventPayload({ data: JSON.stringify({ stage: 'compose', message: '老格式' }) }).message`),
    '老格式', '不带信封的历史事件也要认');
assert.equal(call('replicaEventPayload({ data: "不是 JSON" })'), null);
assert.equal(call('replicaEventPayload(null)'), null);

// ── 2. 合成阶段的进度 ─────────────────────────────────────────────────────────
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'compose', message: '正在按 9 拍阶梯合成提示词' })`);
const atStageStart = call('replicaProgress.percent');
assert.equal(atStageStart, 68, 'compose 段的起点');
assert.equal(progressBox.style.display, 'block');
assert.equal(progressBox.querySelector('#replica-progress-stage').textContent, '合成提示词');

// 规划节拍阶梯的那几轮（此前整段静默，最长 4×150s）。
call(`replicaHandleComposerEvent('outline', '正在规划节拍阶梯（目标 9 拍，第 1/4 轮）…')`);
const planning = call('replicaProgress.percent');
assert.ok(planning > atStageStart, '规划中要能看出进度在走');
assert.equal(call('replicaProgress.label'), '正在规划节拍阶梯（目标 9 拍，第 1/4 轮）…');

// 逐拍产出：唯一有真实分子/分母的信号。
call(`replicaHandleComposerEvent('beat_ready', { index: 5, total: 9 })`);
assert.equal(call('replicaProgress.label'), '已产出第 5/9 拍的提示词');
const midway = call('replicaProgress.percent');
assert.ok(midway > planning && midway < 94, `compose 段内推进，实际 ${midway}`);
assert.equal(progressBox.querySelector('#replica-progress-percent').textContent,
             `${Math.round(midway)}%`);

// 百分比只增不减：几路事件交替到达时来回跳的进度条比没有更让人不安。
call(`replicaHandleComposerEvent('batch', { current: 1, total: 9 })`);
assert.ok(call('replicaProgress.percent') >= midway, '进度不能倒退');

// 最近几条事件留在日志里，且不重复上一条。
call(`replicaHandleComposerEvent('beat_ready', { index: 6, total: 9 })`);
call(`replicaHandleComposerEvent('beat_ready', { index: 6, total: 9 })`);
const logHtml = progressBox.querySelector('#replica-progress-log').innerHTML;
assert.equal((logHtml.match(/已产出第 6\/9 拍的提示词/g) || []).length, 1);
assert.ok(logHtml.includes('已产出第 5/9 拍的提示词'));

// 阶段前进后区间跟着走。
call(`replicaHandleStageEvent({ stage: 'audit', message: '正在扫禁用元素' })`);
assert.ok(call('replicaProgress.percent') >= 94);

// ── 3. 证据帧原地开图 ─────────────────────────────────────────────────────────
const card = call(`replicaRenderBeatCard(
    { job_id: 'replica_x', overview: {} },
    { id: 'B01', start: 0, end: 5, stage: 'enclosure',
      evidence_frames: ['review_001.png', 'review_002.png'],
      package_operations: ['cut', 'fit'], persistent_traces: ['a', 'b'],
      zh: { visible_action: '工人封板' } },
    0)`);
assert.ok(!card.includes('target="_blank"'), '证据帧不再新开标签页');
assert.ok(card.includes('data-lightbox-beat="0"') && card.includes('data-lightbox-at="1"'));
assert.ok(card.includes('封板封闭'), '施工阶段用中文标签');
assert.ok(card.includes('工人封板'), '中文对照要显示在英文原文下面');
assert.ok(card.includes('data-key="package_operations"'), '工序包必须可编辑——报错就指着它');
assert.ok(card.includes('data-key="macro_environment"'), '大环境识别项必须可编辑');
assert.ok(card.includes('data-key="visible_details"'), '细节识别项必须可编辑');

// 灯箱不可用时退回新窗口：点了没反应比多一个标签页更糟。
let opened = null;
sandbox.window.open = (url) => { opened = url; };
call(`replicaOpenLightbox([{ url: '/outputs/a.png' }], 0)`);
assert.equal(opened, '/outputs/a.png');

let lightboxArgs = null;
sandbox.openLightbox = (items, index) => { lightboxArgs = { items, index }; };
call(`replicaOpenLightbox([{ url: '/a.png' }, { url: '/b.png' }], 5)`);
assert.equal(lightboxArgs.items.length, 2);
assert.equal(lightboxArgs.index, 1, '越界的下标要夹回最后一张');

// ── 3.5 进度条必须用上后端给的分子/分母 ───────────────────────────────────────
//
// 后端多个阶段早就在事件里带 done/total，此前前端一个都没用：Pass A 是整条线最长的
// 一段，全程钉死在区间起点 15%，只有日志在滚。
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'review_frames', message: '逐帧事实提取 0/40', done: 0, total: 40 })`);
assert.equal(Math.round(call('replicaProgress.percent')), 15, '刚进这一段应落在区间起点');
call(`replicaHandleStageEvent({ stage: 'review_frames', message: '逐帧事实提取 20/40', done: 20, total: 40 })`);
const half = call('replicaProgress.percent');
assert.ok(half > 15 && half < 38, `半程必须落在区间内部，实得 ${half}`);
call(`replicaHandleStageEvent({ stage: 'review_frames', message: '逐帧事实提取 40/40', done: 40, total: 40 })`);
assert.ok(call('replicaProgress.percent') > half, '分子涨了，进度条必须跟着涨');

// 回归：不带 done/total 的事件行为必须与改动前逐字一致——落到区间起点，别的什么都不做。
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'compose', message: '在合成' })`);
assert.equal(Math.round(call('replicaProgress.percent')), 68, '没有分子分母时仍落到区间起点');

// ── 3.6 人工卡点上的机器活要有自己的区间和名字 ───────────────────────────────
//
// autofix / 工艺精修 / 自动平衡 / 重做中文对照全挂在 review_beats 底下，而它的区间是
// ── 3.6 人工卡点与二创动作的机器活要有自己的区间和名字 ──────────────────────────
//
// autofix / 工艺精修 / 自动平衡 / 重做中文对照 / 二创变体派生全挂在 review_beats 底下，
// 而它的区间是零宽的 [68,68]：进度条纹丝不动，chip 上还写着「待人工核对」——界面在说"等你动手"，
// 其实是机器在跑。带 action 的事件必须走一段独立的动作区间，且 chip 必须显示动作名。
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'review_beats', action: 'refine_craft',
                               message: '工艺精修 3/12', done: 3, total: 12 })`);
const during = call('replicaProgress.percent');
assert.ok(during > 68, `动作跑起来之后进度条必须离开 68，实得 ${during}`);
assert.equal(call('replicaProgress.actionLabel'), '工艺精修',
             'chip 必须显示动作名，不能是「待人工核对」');

// 二创变体正交派生动作 (mutate_orthogonal)
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'mutate_beats', action: 'mutate_orthogonal',
                               message: '正在调用大模型按四轴正交矩阵重构工序', done: 2, total: 5 })`);
const duringMutate = call('replicaProgress.percent');
assert.ok(duringMutate >= 45 && duringMutate <= 68, `mutate_orthogonal 区间必须落在 [45, 68]，实得 ${duringMutate}`);
assert.equal(call('replicaProgress.actionLabel'), '⚡ 正交二创变体派生',
             '二创派生期间 chip 必须显示「⚡ 正交二创变体派生」');
assert.equal(progressBox.querySelector('#replica-progress-stage').textContent, '⚡ 正交二创变体派生');

// 派生二创变体动作 (variant)
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'mutate_beats', action: 'variant',
                               message: '正在沿变异轴派生二创变体阶梯', done: 1, total: 4 })`);
const duringVariant = call('replicaProgress.percent');
assert.ok(duringVariant >= 45 && duringVariant <= 68, `variant 区间必须落在 [45, 68]，实得 ${duringVariant}`);
assert.equal(call('replicaProgress.actionLabel'), '🧬 派生二创变体');

// 峰值帧复核跟逐帧提取同属 review_frames 却发生在它之后：共用区间的话，"只增不减"
// 的进度条在逐帧提取跑满之后就再也动不了，那几十次强模型调用整段静默。
call('replicaResetProgress()');
call(`replicaHandleStageEvent({ stage: 'review_frames', done: 40, total: 40, message: '完' })`);
const afterPassA = call('replicaProgress.percent');
call(`replicaHandleStageEvent({ stage: 'review_frames', action: 'peak_verify',
                               message: '峰值帧复核 5/10', done: 5, total: 10 })`);
assert.ok(call('replicaProgress.percent') > afterPassA,
          '峰值复核必须能在逐帧提取跑满之后继续推进进度条');

// 动作结束、回到普通阶段事件时，chip 要回到阶段名，不能一直挂着动作名。
call(`replicaHandleStageEvent({ stage: 'review_beats', message: '已停在人工卡点' })`);
assert.equal(call('replicaProgress.actionLabel'), '', '动作结束后必须交还 chip');

// ── 3.7 AI 智能发散创意动态进度卡片与骨架屏渲染 ─────────────────────────────
call('replicaDiverging = true; replicaDivergeStep = 2; replicaDivergeStatusText = "正在检索联网爆款趋势...";');
const divergeHtml = call('replicaRenderAiIdeas({ ai_diverged_ideas: [] })');
assert.ok(divergeHtml.includes('replica-diverge-progress-card'), '发散中必须渲染动态发散进度卡片');
assert.ok(divergeHtml.includes('replica-ai-ideas-skeleton-grid'), '发散中必须渲染骨架屏占位网格');
assert.ok(divergeHtml.includes('正在检索联网爆款趋势...'), '发散中必须显示实时状态文字');
call('replicaDiverging = false;');

// ── 3.8 发散刺激器内联进度看板渲染 ──────────────────────────────────────────
const mutatorProgressHtml = call('replicaRenderMutatorProgress()');
assert.ok(mutatorProgressHtml.includes('replica-mutator-live-progress'), '必须支持渲染发散刺激器内联进度看板');

// ── 4. 推进动作一律先落盘 ─────────────────────────────────────────────────────
//
// 服务端所有节拍动作都从磁盘读 beats（autofix_job_beats 第一件事就是 _load_state），
// 跑完整份写回、收尾时再盖掉内存。前端不先落盘，用户这一轮的手工改动就在没有任何
// 报错的情况下蒸发。此前全线只有「合成」一个入口做对了，另外五个按钮各自漏掉。
//
// 这里盯的是「每一个 action」而不是某一个按钮：这类问题的复发方式，是下次新增第八个
// 推进动作时又忘了——按按钮写的测试抓不到那一次。
const calls = [];
sandbox.fetch = async (url) => {
    calls.push(url);
    return { ok: true, status: 200, statusText: 'OK',
             json: async () => ({ status: 'ok', job_state: { job_id: 'j1', beats: { beats: [{ id: 'B01' }] } },
                                  validation: [], task_id: 't1' }) };
};
sandbox.confirm = () => true;
sandbox.EventSource = function () { this.addEventListener = () => {}; this.close = () => {}; };

const runAdvance = async (action) => {
    calls.length = 0;
    call(`replicaState = { job_id: 'j1', beats: { beats: [{ id: 'B01' }] } };`);
    await call(`replicaAdvance('${action}', {}, undefined)`);
    return calls;
};

(async () => {
    for (const action of ['approve', 'autofix', 'fix_beats', 'autobalance',
                          'refine_craft', 'translate', 'variant']) {
        const urls = await runAdvance(action);
        const save = urls.indexOf('/api/replica/beats');
        const advance = urls.indexOf('/api/replica/advance');
        assert.ok(save !== -1, `${action} 必须先落盘再推进，否则模型读到的是上一次保存的版本`);
        assert.ok(save < advance, `${action} 的落盘必须发生在推进之前`);
    }

    // recluster 有意例外：它按设计就是丢掉这份阶梯重跑 Pass B，先存一次只是白写磁盘。
    const urls = await runAdvance('recluster');
    assert.ok(!urls.includes('/api/replica/beats'), 'recluster 不该先落盘——它本来就要丢掉这份阶梯');

    // 落盘失败必须就地中止。宁可让用户看见「保存失败」，也不能让一次静默的旧版本改写跑出去。
    sandbox.fetch = async () => ({ ok: false, status: 500, statusText: 'boom', json: async () => ({}) });
    calls.length = 0;
    call(`replicaState = { job_id: 'j1', beats: { beats: [{ id: 'B01' }] } };`);
    await call(`replicaAdvance('autofix', {}, undefined)`);
    assert.ok(!calls.includes('/api/replica/advance'), '落盘失败时不能继续推进');

    // ── 5. 未保存守卫 ────────────────────────────────────────────────────────
    // 切走会把 replicaState 整个换掉。此前不问一句：改了二十拍、顺手点了列表里另一条
    // 任务，没有任何提示，也回不来。
    sandbox.fetch = async () => ({ ok: true, status: 200, statusText: 'OK',
        json: async () => ({ status: 'ok', job_state: { job_id: 'j2', beats: { beats: [] } } }) });
    call(`replicaState = { job_id: 'j1', beats: { beats: [{ id: 'B01' }] } }; replicaDirty = true;`);
    let asked = 0;
    sandbox.confirm = () => { asked += 1; return false; };
    await call(`replicaLoadJob('j2')`);
    assert.equal(asked, 1, '有未保存改动时切任务必须先问一句');
    assert.equal(call('replicaState.job_id'), 'j1', '用户点了取消就必须留在原地');

    // 同一条任务的刷新（SSE 收尾、保存后回读）不该弹窗——那不是切走。
    asked = 0;
    sandbox.confirm = () => { asked += 1; return true; };
    await call(`replicaLoadJob('j1')`);
    assert.equal(asked, 0, '刷新当前任务不该弹确认');

    console.log('test_replica_progress.js: all assertions passed');
})().catch(e => { console.error(e); process.exit(1); });
