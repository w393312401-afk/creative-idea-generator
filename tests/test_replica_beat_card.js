// 节拍卡的渲染契约。
//
// 2026-08-25 这张卡片做过一次瘦身：22 段常驻说明文收进了 ⓘ、条数从说明文里拆成徽章、
// 字段分成四组、七个闭集下拉压成一行胶囊。这类改动的失败方式全是**静默**的——
// 页面照常渲染、控制台一片干净：
//   · 某一栏在搬家途中丢了 data-key —— 那一栏照常显示、照常能打字，就是永远存不下来；
//   · 某一栏的说明文没跟着搬进 ⓘ —— 用户看见一个 2~5 字的短名，再也问不出它要写什么；
//   · 条数越界不变红 —— 徽章从「说明」升级成「状态」的全部意义就在这一下；
//   · 可选字段该收的没收、该展开的收起来了 —— 有值却被折起来，人会以为那一栏是空的。
// 所以这里盯的是「哪个字段以什么形态出现在 DOM 里」，不是样式。
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

const baseBeat = {
    id: 'B05', start: 17.5, end: 22, stage: 'enclosure', space: 'riverbank clearing',
    operation: 'weld roof plates',
    package_operations: ['weld roof plates', 'weld hatch collar', 'stretch tarpaulin'],
    visible_details: ['flat steel deck plates', 'square collar frame',
                      'green PE tarpaulin roll', 'arc-weld flash'],
    visible_action: 'Craftsman welds roof plates.',
    visible_result: 'Steel roof fully sealed.',
    state_before: 'Open steel roof beam grid.',
    state_after: 'Roof one hundred percent plated.',
    persistent_traces: ['miter-welded hatch collar', 'green PE tarpaulin skin'],
    tool: 'manual arc welding torch',
    sfx: ['arc crackle', 'tarp rustling', 'boot scuffs'],
    shot_scale: 'wide', camera_angle: 'high', camera_bearing: 'front',
    lens_feel: 'wide', camera_move: 'static', time_treatment: 'timelapse',
    worker_count: 1, workers_present: true,
    cast_action: 'kneels to weld deck plates',
    light_state: 'overcast daylight',
    subject_placement: 'centred, two thirds of frame height',
    observed_shot_count: 1, observed_shot_seconds: 4.5,
    source_event_ids: ['E04'],
    confidence: 0.8, evidence_frames: ['f1.jpg'],
    zh: { operation: '焊接顶板' },
};

const render = (beat, extra = {}) => {
    const b = Object.assign({}, baseBeat, beat);
    const state = Object.assign({ job_id: 'j1', validation: [], beats: { beats: [b], speed_multiplier: 2.0 } }, extra);
    if (extra.validation) state.beats.validation = extra.validation;
    return vm.runInContext('replicaRenderBeatCard', sandbox)(state, b, extra.idx || 0, extra.prevSpace || '');
};

const html = render({});

// ── 回写通路：每一栏都得带着 data-key 出场 ────────────────────────────
// 保存走的是 [data-beat][data-key] 一条通用委托（replicaBindBeatEvents）。少一个 key
// 不会报错，只会让那一栏的改动永远存不进 replicaState。
const META = vm.runInContext('REPLICA_FIELD_META', sandbox);
const PARAMS = vm.runInContext('REPLICA_SHOT_PARAMS', sandbox);
for (const key of Object.keys(META)) {
    assert.ok(html.includes(`data-key="${key}"`), `字段 ${key} 没有渲染出回写用的 data-key`);
}
for (const [key] of PARAMS) {
    assert.ok(html.includes(`data-key="${key}"`), `闭集参数 ${key} 没有渲染出回写用的 data-key`);
}
assert.ok(html.includes('data-key="worker_count"'), '工人数要能回写');
assert.ok(html.includes('data-key="stage"'), '施工阶段要能回写');
assert.ok(html.includes('data-num="1"'), '工人数是数字栏，回写时要走清空=撤回标注那条分支');

// ── 说明文：短名替下的那一整段话必须还在，只是换了个位置出现 ──────────
// 这一步真正的风险是「短名留下了、说明没跟过来」——用户看见「主导工序」四个字，
// 再也问不出它要的是三个词还是一整句。
for (const [key, meta] of Object.entries(META)) {
    assert.ok(meta.name && meta.name.length <= 8, `${key} 的短名要短（当前「${meta.name}」）`);
    assert.ok(meta.help && meta.help.length >= 10, `${key} 丢了说明文`);
}
assert.equal((html.match(/class="replica-field-help"/g) || []).length,
             (html.match(/class="replica-textarea"/g) || []).length,
             '每一栏都要配一枚 ⓘ');
assert.ok(html.includes('里程碑工序词'), '主导工序的说明文要出现在卡片里（收在 ⓘ 的 data-help 上）');
assert.ok(html.includes('tabindex="-1"'),
          'ⓘ 不承载动作，必须留在 Tab 序列之外——否则二十几个字段会变成四十几站');

// ── 条数徽章：越界要变红。这是它从「说明」升级成「状态」的全部意义 ────
assert.ok(/replica-field-count[^>]*>3\/3</.test(html), '工序包 3 道要渲染成 3/3');
assert.ok(/replica-field-count[^>]*>4\/6</.test(html), '细节识别项 4 条要渲染成 4/6');
assert.ok(!html.includes('is-bad'), '本拍所有条数都在范围内，不该有红徽章');

const short = render({ visible_details: ['only one'], package_operations: ['only one'] });
assert.ok(short.includes('replica-field-count is-bad'), '条数不够要把徽章标红');
assert.ok(/replica-field-count is-bad[^>]*>1\/6</.test(short), '细节识别项只有 1 条要红着显示 1/6');

const over = render({ micro_traces: ['a', 'b', 'c', 'd'] });
assert.ok(/replica-field-count is-bad[^>]*>4\/3</.test(over), '微观痕迹超过 3 条要标红');

// ── 分组：四组各就各位 ───────────────────────────────────────────────
for (const title of ['画面事实', '状态与痕迹', '拍摄与声音']) {
    assert.ok(html.includes(`>${title}</div>`), `缺分组「${title}」`);
}
assert.equal((html.match(/replica-group-title/g) || []).length, 3, '常驻分组恰好三组，第四组是可折叠的「更多」');
assert.ok(html.includes('replica-field-more'), '缺「更多」折叠区');

// ── 闭集参数压成一行胶囊，而不是七个整格 ──────────────────────────────
assert.equal((html.match(/class="replica-param"/g) || []).length, PARAMS.length + 1,
             '六个下拉 + 工人数，各一枚胶囊');
assert.ok(html.includes('replica-beat-params'), '参数行要有自己的容器（横向流式，不进字段栅格）');

// ── 可选字段的收放规则 ───────────────────────────────────────────────
// 有工人时人物动作神情在上面；清场帧收进「更多」——但绝不能拿掉：
// 「有人偶在旁观」仍然要写。
const moreStart = html.indexOf('replica-field-more');
assert.ok(html.indexOf('data-key="cast_action"') < moreStart,
          '有工人的拍，人物动作神情要在常驻分组里');

const cleared = render({ workers_present: false, worker_count: 0 });
assert.ok(cleared.includes('data-key="cast_action"'), '清场帧也要保留人物动作神情，只是收起来');
assert.ok(cleared.indexOf('data-key="cast_action"') > cleared.indexOf('replica-field-more'),
          '清场帧的人物动作神情要收进「更多」');

// 原片这一拍切过镜头，插入镜主体就不再是可选项。
const multishot = render({ observed_shot_count: 3 });
assert.ok(multishot.indexOf('data-key="insert_subject"') < multishot.indexOf('replica-field-more'),
          '原片多镜的拍，插入镜主体要提到常驻分组');
assert.ok(html.indexOf('data-key="insert_subject"') > moreStart,
          '原片一镜的拍，插入镜主体收进「更多」');

// 大环境识别项：首拍必填、过门拍必填、其余留空则收起。三种情形说三句不同的话。
const first = render({ macro_environment: [] }, { idx: 0 });
assert.ok(first.includes('锚点首拍必填'), '首拍的大环境说明要说「必填」');
assert.ok(first.indexOf('data-key="macro_environment"') < first.indexOf('replica-field-more'),
          '首拍的大环境识别项不能被折起来');

const threshold = render({ macro_environment: [], space: 'sleeping alcove' },
                         { idx: 4, prevSpace: 'main room' });
assert.ok(threshold.includes('过门新空间首拍'), '过门拍的大环境说明要说的是新空间');
assert.ok(threshold.indexOf('data-key="macro_environment"') < threshold.indexOf('replica-field-more'),
          '过门拍的大环境识别项不能被折起来');

const midway = render({ macro_environment: [] }, { idx: 4, prevSpace: 'riverbank clearing' });
assert.ok(midway.indexOf('data-key="macro_environment"') > midway.indexOf('replica-field-more'),
          '非首拍且空着的大环境识别项要收进「更多」，避免干扰大模型');

const midwayFilled = render({ macro_environment: ['riverbank, overcast'] },
                            { idx: 4, prevSpace: 'riverbank clearing' });
assert.ok(midwayFilled.indexOf('data-key="macro_environment"') < midwayFilled.indexOf('replica-field-more'),
          '非首拍但已经填了值的大环境识别项要留在上面——有值却被折起来，人会以为它是空的');

// ── 「更多」的默认开合 ───────────────────────────────────────────────
assert.ok(!/replica-field-more[^>]*\sopen/.test(html), '「更多」里一个值都没有时默认收起');
const withMore = render({ material_specs: ['6mm carbon steel deck plates'] });
assert.ok(/replica-field-more[^>]*\sopen/.test(withMore),
          '「更多」里有值就要默认展开——有值却被折起来，人会以为那一栏是空的');
assert.ok(/replica-field-more[^>]*\sopen/.test(
              render({}, { validation: [{ level: 'error', beat_id: 'B05', message: 'x' }] })),
          '本拍有硬伤时「更多」也要展开：修硬伤的人得看得见全部字段');
assert.ok(/replica-field-more[^>]*data-fold-key="B05:more"/.test(html),
          '「更多」的开合要记进 replicaFieldFoldState，否则每保存一次就得再翻一遍');

// ── 头部：只留「是哪一拍、多长、有没有事」 ────────────────────────────
assert.ok(html.includes('replica-beat-dot is-ok'), '无硬伤时状态点是绿的');
assert.ok(render({}, { validation: [{ level: 'error', beat_id: 'B05', message: 'x' }] })
              .includes('replica-beat-dot is-error'), '有硬伤时状态点要变红');
assert.ok(render({}, { validation: [{ level: 'warn', beat_id: 'B05', message: 'x' }] })
              .includes('replica-beat-dot is-warn'), '有待确认项时状态点是黄的');
assert.ok(render({ confidence: 0.3 }).includes('replica-beat-dot is-warn'), '低置信也算一种黄');
assert.ok(render({ confidence: 0.3 }).includes('低置信'), '低置信要在元信息行里点名');

// 降级下来的那几项进了元信息行，不再和状态抢同一种视觉权重。
const metaStart = html.indexOf('replica-beat-meta');
const bodyStart = html.indexOf('replica-beat-body');
for (const needle of ['data-key="stage"', 'riverbank clearing', '原片一镜', '有工人', '事件 E04']) {
    const at = html.indexOf(needle);
    assert.ok(at > metaStart && at < bodyStart, `「${needle}」应该落在元信息行里`);
}

// ── 中文对照：DOM 里在（供开关切换），默认由 CSS 收起 ──────────────────
assert.ok(html.includes('replica-field-zh'), '有中文对照的字段要渲染出对照层');
assert.ok(html.includes('焊接顶板'), '中文对照的内容要在 DOM 里，开关一开就能看见');
assert.ok(html.includes('data-zh-toggle'), '卡片头部要有中文对照开关');

console.log('test_replica_beat_card.js: all assertions passed');
