// test_candidate_concurrency_settings.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

// 1. Verify index.html contains the candidate concurrency selector
const htmlPath = path.join(__dirname, '..', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

assert.ok(html.includes('id="api-candidate-concurrency-group"'), 'index.html should have api-candidate-concurrency-group');
assert.ok(html.includes('id="settings-candidate-concurrency"'), 'index.html should have settings-candidate-concurrency select');
assert.ok(html.includes('4选1 候选并发度'), 'index.html should have 4选1 候选并发度 label');
assert.ok(html.includes('value="4" selected'), '4 concurrency should be default selected');

// 2. Verify js/state.js includes candidateConcurrency: 4 in DEFAULT_CONFIG
const statePath = path.join(__dirname, '..', 'js', 'state.js');
const stateCode = fs.readFileSync(statePath, 'utf8');
assert.ok(stateCode.includes('candidateConcurrency: 4'), 'js/state.js should have candidateConcurrency: 4 in DEFAULT_CONFIG');

// 3. Verify js/config.js handles candidateConcurrency
const configPath = path.join(__dirname, '..', 'js', 'config.js');
const configCode = fs.readFileSync(configPath, 'utf8');
assert.ok(configCode.includes('settings-candidate-concurrency'), 'js/config.js should reference settings-candidate-concurrency');
assert.ok(configCode.includes('api-candidate-concurrency-group'), 'js/config.js should handle api-candidate-concurrency-group visibility');

console.log('✅ Candidate concurrency frontend settings tests passed successfully!');
