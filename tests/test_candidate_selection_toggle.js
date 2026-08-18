const assert = require('assert');
const fs = require('fs');
const path = require('path');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

// 1. Check DOM structure in HTML
assert.ok(!indexHtml.includes('pipeline-more-btn'), 'pipeline-more-btn must be removed');
assert.ok(!indexHtml.includes('pipeline-more-menu'), 'pipeline-more-menu must be removed');
assert.ok(indexHtml.includes('pipeline-selection-checkbox'), 'pipeline-selection-checkbox must be in HTML');
assert.ok(indexHtml.includes('pipeline-selection-mode-toggle'), 'pipeline-selection-mode-toggle must be in HTML');

const apiClientJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'api_client.js'), 'utf8');

// 2. Check app.js logic
assert.ok(appJs.includes('isCandidateSelectionMode'), 'app.js must define isCandidateSelectionMode helper');
assert.ok(appJs.includes('generateFramesSelection'), 'generateFramesSelection must be referenced');
assert.ok(/generateFrames\(\)[\s\S]*?isCandidateSelectionMode\(\)[\s\S]*?generateFramesSelection\(\)/.test(appJs), 'generateFrames must route to generateFramesSelection when 4-in-1 is active');
assert.ok(appJs.includes('pipeline_candidate_selection_mode'), 'localStorage preference persistence must be present');
assert.ok(/generation_mode:\s*'standard'/.test(appJs), 'generateFrames must send generation_mode: standard when 4-in-1 is off');
assert.ok(/candidate_selection:\s*false/.test(appJs), 'generateFrames must send candidate_selection: false when 4-in-1 is off');

// 3. Check js/api_client.js retryFrame logic
assert.ok(apiClientJs.includes('isCandidateSelectionMode'), 'retryFrame must check isCandidateSelectionMode');
assert.ok(/generation_mode:\s*isCand\s*\?\s*'candidate_selection'\s*:\s*'standard'/.test(apiClientJs), 'retryFrame must set generation_mode according to isCand');
assert.ok(/candidate_selection:\s*isCand/.test(apiClientJs), 'retryFrame must set candidate_selection according to isCand');

console.log('Candidate selection toggle tests passed successfully!');
