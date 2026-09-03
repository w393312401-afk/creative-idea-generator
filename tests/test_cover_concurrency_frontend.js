// test_cover_concurrency_frontend.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const htmlPath = path.join(__dirname, '..', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

const appJsPath = path.join(__dirname, '..', 'app.js');
const appJs = fs.readFileSync(appJsPath, 'utf8');

// 1. Verify index.html contains generate-cover-concurrent-btn
assert.ok(html.includes('id="generate-cover-concurrent-btn"'), 'index.html should have generate-cover-concurrent-btn');
assert.ok(html.includes('🎯 并发生成封面'), 'index.html should have 🎯 并发生成封面 button text');
assert.ok(html.includes('封面图并发生成数量'), 'api-candidate-concurrency-group should mention cover concurrency');

// 2. Verify app.js binds generate-cover-concurrent-btn
assert.ok(appJs.includes("getElementById('generate-cover-concurrent-btn')"), 'app.js should bind generate-cover-concurrent-btn');
assert.ok(appJs.includes("generateCover({ concurrent: true })"), 'app.js should invoke generateCover with concurrent: true on button click');

// 3. Verify generateCover calculates concurrency count aligned with candidateConcurrency
assert.ok(appJs.includes("config.candidateConcurrency"), 'generateCover should read config.candidateConcurrency');
assert.ok(/concurrencyCount\s*=\s*isConcurrent\s*\?\s*candidateConcurrency\s*:\s*1/.test(appJs), 'concurrencyCount should match candidateConcurrency when concurrent');
assert.ok(/concurrent:\s*isConcurrent/.test(appJs), 'generateCover should send concurrent flag');
assert.ok(/count:\s*concurrencyCount/.test(appJs), 'generateCover should send concurrency count');

// 4. Verify streamCoverProgress processes covers array and cover_generating events
assert.ok(appJs.includes("type === 'cover_generating'"), 'streamCoverProgress should handle cover_generating event');
assert.ok(appJs.includes("data.covers"), 'streamCoverProgress should handle data.covers array');
assert.ok(appJs.includes("renderCoversForIdea(ownerIdea"), 'streamCoverProgress should render covers');

// 5. Verify updatePipelineBar displays concurrent cover label when candidate selection mode is on
assert.ok(/coverLabel\s*=\s*isCand\s*\?\s*['"]并发生成封面图['"]/.test(appJs), 'updatePipelineBar should show 并发生成封面图 when 4-in-1 is on');

console.log('✅ Cover concurrency frontend tests passed successfully!');
