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
