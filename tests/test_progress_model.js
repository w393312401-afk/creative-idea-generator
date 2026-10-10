const assert = require('assert');
const {
  normalizeGenerationProgress,
  progressFromEvents
} = require('../js/progress_model.js');

function next(eventType, eventData, taskType, state) {
  const progress = normalizeGenerationProgress(eventType, eventData, taskType, state);
  return progress;
}

let p = next('progress', { stage: 'outline', details: 'parse' }, 'compose');
assert.strictEqual(p.percent, 5);
p = next('progress', { stage: 'batch', details: { current: 8, total: 10 } }, 'compose', p.state);
assert.strictEqual(p.percent, 76);
const beforeRepair = p.percent;
p = next('progress', { stage: 'batch', details: { current: 2, total: 10 } }, 'compose', p.state);
assert.strictEqual(p.percent, beforeRepair, 'batch repair round must not move progress backward');
p = next('progress', { stage: 'audit', details: 'repair round' }, 'compose', p.state);
assert.ok(p.percent >= beforeRepair);

const videoEvents = [
  ['queue', { message: 'queued' }],
  ['start', { total: 3, slots: [3, 4, 5] }],
  ['video_start', { index: 4, current: 2, total: 3 }],
  ['video_done', { index: 4, current: 2, total: 3, video: { slot: 4 } }]
];
p = progressFromEvents(videoEvents, 'videos', 'running');
assert.strictEqual(p.slot, 4);
assert.strictEqual(p.current, 2);
assert.ok(p.percent > 5, 'video done should advance by terminal segment count');

const frameEvents = [
  ['queue', { message: 'queued' }],
  ['start', { total: 2 }],
  ['frame_start', { slot: 1, sequence: 1 }],
  ['frame_retry', { slot: 1, sequence: 1, attempt: 1, reason: 'no visible delta' }],
  ['frame', { current: 1, total: 2, frame: { slot: 1, sequence: 1, quality_gate: 'vlm_qa_failed' } }]
];
p = progressFromEvents(frameEvents, 'frames', 'running');
assert.strictEqual(p.percent, 50);
assert.match(p.label, /图片生成/);

p = progressFromEvents([
  ['start', { total: 2 }],
  ['frame_start', { slot: 2, sequence: 2 }],
  ['frame_continuity_check', { slot: 2, sequence: 2 }],
  ['frame_continuity_retry', { slot: 2, sequence: 2, attempt: 1 }]
], 'frames', 'running');
assert.strictEqual(p.status, 'retrying');
assert.match(p.label, /发现漂移/);

console.log('progress_model tests passed');

const partialVideos = [
  ['start', { total: 2 }],
  ['video_error', { index: 1, total: 2 }],
  ['video_done', { index: 2, total: 2 }],
];
p = progressFromEvents(partialVideos, 'videos', 'running');
assert.strictEqual(p.label, '已生成 1/2 段视频 · 1 段待重试');
p = progressFromEvents(partialVideos, 'videos', 'completed');
assert.strictEqual(p.label, '本次生成已结束 · 1 段待重试');
p = progressFromEvents(partialVideos.concat([['video_done', { index: 1, total: 2 }]]), 'videos', 'completed');
assert.strictEqual(p.label, '视频序列生成完成');

// 已处理失败不等于交付；恢复查询和等待仍属于同一个运行任务。
const allSlots = Array.from({ length: 27 }, (_, i) => i + 1);
let recovery = next('start', { total: 27, slots: allSlots }, 'videos');
for (const index of allSlots) recovery = next(index <= 24 ? 'video_done' : 'video_error',
  { index, current: index, total: 27 }, 'videos', recovery.state);
assert.strictEqual(recovery.current, 27, '旧进度会将失败计入已处理数');
const phases = ['querying', 'waiting', 'retrying'];
for (const phase of phases) {
  recovery = next('video_recovery', { phase, slots: [25, 26, 27],
    completed_slots: allSlots.slice(0, 24), retry_round: 2, next_retry_at: 2000000000,
    message: '自动恢复未完成片段' }, 'videos', recovery.state);
  assert.strictEqual(recovery.current, 24);
  assert.strictEqual(recovery.status, 'recovering');
  assert.strictEqual(recovery.state.recoveryPhase, phase);
  assert.strictEqual(recovery.state.retryRound, 2);
  assert.strictEqual(recovery.state.nextRetryAt, 2000000000);
  assert.strictEqual(recovery.state.slotStatus['25'], 'recovering');
  assert.strictEqual(recovery.state.slotStatus['1'], 'done');
  assert(recovery.percent < 85);
  assert.match(recovery.label, /24\/27/);
  assert(!recovery.label.includes('结束'));
}
recovery = next('video_start', { index: 25, current: 27, total: 27 }, 'videos', recovery.state);
assert.strictEqual(recovery.current, 24, '补跑提交序号不增加已交付数');
recovery = next('video_done', { index: 25, current: 27, total: 27 }, 'videos', recovery.state);
assert.strictEqual(recovery.current, 25);
const recovered = next('video_recovery', { phase: 'waiting', slots: [26, 27], completed_slots: [25] }, 'videos', recovery.state);
assert.strictEqual(recovered.current, 25, '渐进批次的恢复不能抹掉此前已完成槽位');
const cancelledRecovery = next('error', { message: '用户取消' }, 'videos', recovered.state);
assert.strictEqual(cancelledRecovery.state.slotStatus['26'], 'stopped');
assert.strictEqual(cancelledRecovery.state.slotStatus['25'], 'done');
