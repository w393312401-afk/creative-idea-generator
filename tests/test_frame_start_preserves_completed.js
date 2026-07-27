// 帧任务会经历多个内部 start（首帧验收、剩余帧分段渲染）。后续 start 必须保留
// 已经通过 frame 事件合并进来的首帧，不能让它从 UI 数据源消失到任务终态才恢复。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'api_client.js'), 'utf8');
const start = source.indexOf('function ensureFrameRunForStart');
const end = source.indexOf('\n}\n', start) + 3;
assert.ok(start >= 0 && end > start, 'ensureFrameRunForStart helper must exist');

const ctx = {};
vm.createContext(ctx);
vm.runInContext(source.slice(start, end), ctx);

const firstFrame = { sequence: 1, url: '/outputs/demo/frames/img_001.webp' };
const idea = { title: 'demo', frameRun: { title: 'demo', frames: [firstFrame] } };
const sameRun = ctx.ensureFrameRunForStart(idea);
assert.strictEqual(sameRun.frames.length, 1);
assert.strictEqual(sameRun.frames[0].sequence, 1);
assert.strictEqual(sameRun.frames[0].url, firstFrame.url);

const freshIdea = { title: 'fresh' };
ctx.ensureFrameRunForStart(freshIdea);
assert.deepStrictEqual(Array.from(freshIdea.frameRun.frames), []);

console.log('frame start preservation tests passed');
