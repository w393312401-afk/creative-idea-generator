// 槽位状态模型（js/slot_model.js）的单测。这个模型是纯函数、无 DOM，所以可以
// 直接 require——在它出现之前，"这一格是什么状态"的判断散在十几处 innerHTML 里，
// 没有任何可测的口子。
//
// 跑法：node tests/test_slot_model.js
const assert = require('assert');
const {
    padSlot, slotIsStale, slotIsHero,
    frameSlotState, videoSlotState, slotPendingState,
    videoSlotLabel, summarizeSlotStates,
} = require('../js/slot_model.js');

const acts = (state) => state.actions.map(a => a.act);
const badges = (state) => state.badges.map(b => b.id);

// ── 基础 ────────────────────────────────────────────────────────────
assert.strictEqual(padSlot(3), '003');
assert.strictEqual(padSlot('12'), '012');

// ── 别名收口：同一语义的多种历史写法必须判成同一件事 ────────────────
assert.ok(slotIsStale({ stale_lineage: true }), 'stale_lineage 是当前写法');
assert.ok(slotIsStale({ quality_gate: 'stale' }), 'quality_gate=stale 是旧写法');
assert.ok(slotIsStale({ stale: true }), 'frame.stale 是更早的写法');
assert.ok(!slotIsStale({ quality_gate: 'auto_approved' }));

assert.ok(slotIsHero({ is_hero: true }), 'is_hero 是结构化字段');
assert.ok(slotIsHero({ meta: 'shot HERO panorama' }), 'meta 含 HERO 是旧写法');
assert.ok(!slotIsHero({ meta: 'ordinary bridge' }));

// ── 图片槽位：各 quality_gate 对应的徽标与操作 ────────────────────────
const ready = (over) => frameSlotState(
    Object.assign({ sequence: 3, url: '/outputs/x/frames/img_003.webp' }, over),
    { seq: 3 });

let s = ready({});
assert.strictEqual(s.kind, 'ready');
assert.strictEqual(s.label, 'IMG 003');
assert.deepStrictEqual(badges(s), []);
// 无问题的帧没有「修复此帧问题」出口
assert.deepStrictEqual(acts(s), ['describe-frame', 'retry-frame', 'upload-frame', 'delete-slot']);

s = ready({ quality_gate: 'i2i_fallback_degraded' });
assert.deepStrictEqual(badges(s), ['degraded']);
assert.ok(s.title.includes('降级为文生图'));

s = ready({ quality_gate: 'sequence_review_flagged', vlm_qa_reason: '空间跳变' });
assert.deepStrictEqual(badges(s), ['review-failed']);
assert.ok(acts(s).includes('fix-frame'), '审查未过必须给出定向修复入口');
assert.ok(s.title.includes('空间跳变'));

// 'vlm_qa_failed' 是已停用的逐帧质检门终态，旧 manifest 仍要认
s = ready({ quality_gate: 'vlm_qa_failed' });
assert.deepStrictEqual(badges(s), ['review-failed']);
assert.ok(s.title.includes('跳变或无变化'), '没有 reason 时用兜底文案');

// 人工标记与机器判定并存时只画「人工标记」，但两者都要进 hover 说明
s = ready({ quality_gate: 'sequence_review_flagged', vlm_qa_reason: 'R', manual_issue: '门开反了' });
assert.deepStrictEqual(badges(s), ['manual-flagged']);
assert.ok(s.badges[0].tip.includes('门开反了') && s.badges[0].tip.includes('R'));
assert.ok(acts(s).includes('fix-frame'));
assert.ok(s.actions.find(a => a.act === 'describe-frame').label === '改描述');

// 人工标记可以在没跑过审查的帧上单独成立
s = ready({ manual_issue: '墙面材质不对' });
assert.deepStrictEqual(badges(s), ['manual-flagged']);
assert.ok(acts(s).includes('fix-frame'));

s = ready({ quality_gate: 'auto_approved_degraded' });
assert.deepStrictEqual(badges(s), ['unverified']);
s = ready({ quality_gate: 'sequence_review_skipped' });
assert.deepStrictEqual(badges(s), ['review-skipped']);
s = ready({ quality_gate: 'auto_approved', vlm_qa_reason: 'WARN 轻微色偏' });
assert.deepStrictEqual(badges(s), ['warned']);
// WARN 必须在开头才算留痕，出现在中间的不算
s = ready({ quality_gate: 'auto_approved', vlm_qa_reason: '通过，无 WARN' });
assert.deepStrictEqual(badges(s), []);

// 多枚徽标同时成立：旧实现靠第二枚硬编码 left:45px，第三枚就没地方放
s = ready({ quality_gate: 'i2i_fallback_degraded', manual_issue: '透视歪', stale_lineage: true });
assert.deepStrictEqual(badges(s), ['degraded', 'manual-flagged', 'stale']);

// 结构化违规按条进 hover，不再拼成一根字符串
s = ready({ review_issues: [{ layer: 'global', text: '施工顺序倒置', frames: [3, 4] }] });
assert.ok(s.title.includes('[跨帧] 施工顺序倒置（涉及 IMG 003/004）'));

// 人工换位/上传只进 hover 说明（既有行为，不额外画徽标）
s = ready({ swapped_from_sequence: 7, source: 'manual_upload' });
assert.deepStrictEqual(badges(s), []);
assert.ok(s.title.includes('人工从 IMG 007 拖过来'));
assert.ok(s.title.includes('人工上传的本地图片'));

// ── 未生成 / 等待中：pending 只在该槽位真在任务范围内时成立 ───────────
// （2026-07-20 实机事故：重试 IMG 003 时 004-012 全被画成"等待中"）
s = frameSlotState(null, { seq: 5, pending: false });
assert.strictEqual(s.kind, 'missing');
assert.strictEqual(s.statusText, '未生成/已失效');
assert.deepStrictEqual(acts(s), ['retry-frame', 'upload-frame', 'delete-slot']);
assert.strictEqual(s.actions[0].label, '生成');

s = frameSlotState(null, { seq: 5, pending: true });
assert.strictEqual(s.kind, 'pending');
assert.deepStrictEqual(acts(s), [], '等待中的格子不该有任何操作按钮');

// 只有 file、没有 url 的旧记录也算有图
s = frameSlotState({ sequence: 2, file: 'outputs/x/frames/img_002.webp' }, { seq: 2 });
assert.strictEqual(s.kind, 'ready');

// ── busy：只影响 disabled 与 title，绝不改变按钮的存在与否 ─────────────
const idle = ready({ manual_issue: 'x' });
const busy = frameSlotState(
    { sequence: 3, url: '/o/img_003.webp', manual_issue: 'x' }, { seq: 3, busy: true });
assert.deepStrictEqual(acts(busy), acts(idle), 'busy 不得增删按钮');
assert.ok(busy.actions.every(a => a.disabled));
assert.ok(idle.actions.every(a => !a.disabled));
assert.ok(busy.actions.every(a => a.title.includes('请稍候')));

// ── 视频槽位 ────────────────────────────────────────────────────────
assert.strictEqual(videoSlotLabel(3, false), 'VID 003 (IMG 003 ➔ IMG 004)');
assert.strictEqual(videoSlotLabel(11, true), 'VID 011 (英雄展示 · 完工全景)');

let v = videoSlotState({ slot: 3, url: '/outputs/x/videos/vid_003.mp4' }, { seq: 3 });
assert.strictEqual(v.kind, 'ready');
assert.strictEqual(v.label, 'VID 003 (IMG 003 ➔ IMG 004)');
assert.deepStrictEqual(acts(v), ['retry-video', 'upload-video', 'delete-slot']);
assert.ok(v.draggable);

v = videoSlotState({ slot: 11, url: '/o/vid_011.mp4', is_hero: true }, { seq: 11 });
assert.strictEqual(v.label, 'VID 011 (英雄展示 · 完工全景)');

v = videoSlotState(
    { slot: 4, url: '/o/vid_004.mp4', source: 'manual_upload', swapped_from_slot: 6 }, { seq: 4 });
assert.deepStrictEqual(badges(v), ['manual-upload', 'swapped']);
assert.ok(v.badges[1].tip.includes('VID 006'));

v = videoSlotState({ slot: 2, status: 'failed', error: 'FX 队列超时' }, { seq: 2 });
assert.strictEqual(v.kind, 'failed');
assert.strictEqual(v.title, 'FX 队列超时');
assert.deepStrictEqual(acts(v), ['retry-video', 'upload-video', 'delete-slot']);
assert.ok(!v.draggable, '失败的格子不能当换位的源');

v = videoSlotState(null, { seq: 8, pending: false });
assert.strictEqual(v.kind, 'missing');
assert.strictEqual(v.actions[0].label, '生成');
assert.strictEqual(videoSlotState(null, { seq: 8, pending: true }).kind, 'pending');

// 声明式硬切是"预期缺失"（合并门禁 server.py:1395 同口径），不是失败：
// 重渲清单时此前会因为没有 url 被当成 failed，画出带重试按钮的失败卡
['skipped_cut', 'skipped_bridge_hold'].forEach(status => {
    const cut = videoSlotState({ slot: 5, status }, { seq: 5 });
    assert.strictEqual(cut.kind, 'cut', `${status} 必须画成中性卡`);
    assert.deepStrictEqual(acts(cut), [], `${status} 不该给重试出口`);
});

// ── 汇总 ────────────────────────────────────────────────────────────
const states = [
    ready({}),
    ready({ stale_lineage: true }),
    frameSlotState(null, { seq: 9 }),
    slotPendingState('image', 10, '等待中'),
];
assert.deepStrictEqual(summarizeSlotStates(states),
    { total: 4, ready: 2, pending: 1, missing: 1, flagged: 1 });

console.log('slot model tests passed');
