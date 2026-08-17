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

// 2. Check app.js logic
assert.ok(appJs.includes('isCandidateSelectionMode'), 'app.js must define isCandidateSelectionMode helper');
assert.ok(appJs.includes('generateFramesSelection'), 'generateFramesSelection must be referenced');
assert.ok(/generateFrames\(\)[\s\S]*?isCandidateSelectionMode\(\)[\s\S]*?generateFramesSelection\(\)/.test(appJs), 'generateFrames must route to generateFramesSelection when 4-in-1 is active');
assert.ok(appJs.includes('pipeline_candidate_selection_mode'), 'localStorage preference persistence must be present');

console.log('Candidate selection toggle tests passed successfully!');
