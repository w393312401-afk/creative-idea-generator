/**
 * Unit test for benchmark reference comparison UI in stepped_pipeline.js and collage_viewer.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

console.log('Testing Benchmark Reference UI in stepped_pipeline.js & collage_viewer.js...');

const steppedJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'stepped_pipeline.js'), 'utf8');
const steppedCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'stepped_pipeline.css'), 'utf8');
const collageJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'collage_viewer.js'), 'utf8');
const collageCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'collage_viewer.css'), 'utf8');

// 1. Check stepped_pipeline.js
assert.ok(steppedJs.includes('stepped-dual-preview-grid'), 'stepped_pipeline.js must support dual preview grid for anchor review');
assert.ok(steppedJs.includes('stepped-paired-thumb-card'), 'stepped_pipeline.js must render paired thumb cards');
assert.ok(steppedJs.includes('openSteppedCollageViewerFromState'), 'stepped_pipeline.js must have openSteppedCollageViewerFromState');
assert.ok(steppedJs.includes('tag-ref'), 'stepped_pipeline.js must render ref tag for benchmark frame');

// 2. Check stepped_pipeline.css
assert.ok(steppedCss.includes('.stepped-dual-preview-grid'), 'stepped_pipeline.css must define .stepped-dual-preview-grid');
assert.ok(steppedCss.includes('.stepped-paired-thumb-card'), 'stepped_pipeline.css must define .stepped-paired-thumb-card');
assert.ok(steppedCss.includes('.stepped-dual-preview-badge.ref-badge'), 'stepped_pipeline.css must define .ref-badge');

// 3. Check collage_viewer.js
assert.ok(collageJs.includes('compareType === \'benchmark\''), 'collage_viewer.js must support benchmark compare mode');
assert.ok(collageJs.includes('tag-ref'), 'collage_viewer.js must render benchmark REF split tag');
assert.ok(collageJs.includes('tag-gen'), 'collage_viewer.js must render benchmark GEN split tag');
assert.ok(collageJs.includes('navigateBenchmarkSeq'), 'collage_viewer.js must have navigateBenchmarkSeq function');
assert.ok(collageJs.includes('cv-dual-collage-wrap'), 'collage_viewer.js must support dual collage mode');

// 4. Check collage_viewer.css
assert.ok(collageCss.includes('.cv-split-tag.tag-gen'), 'collage_viewer.css must define .tag-gen');
assert.ok(collageCss.includes('.cv-split-tag.tag-ref'), 'collage_viewer.css must define .tag-ref');
assert.ok(collageCss.includes('.cv-dual-collage-wrap'), 'collage_viewer.css must define .cv-dual-collage-wrap');

// 5. Check index.html & slot_toolbar.js
const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const slotToolbarJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'slot_toolbar.js'), 'utf8');
assert.ok(indexHtml.includes('slot-benchmark-btn'), 'index.html must include slot-benchmark-btn');
assert.ok(slotToolbarJs.includes('.slot-benchmark-btn'), 'slot_toolbar.js must bind slot-benchmark-btn');

// 6. Check slot_model.js & slot_card.js
const slotModelJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'slot_model.js'), 'utf8');
const slotCardJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'slot_card.js'), 'utf8');
assert.ok(slotModelJs.includes('hasBenchmarkRef'), 'slot_model.js must detect hasBenchmarkRef');
assert.ok(slotModelJs.includes('compare-benchmark'), 'slot_model.js must include compare-benchmark action');
assert.ok(slotCardJs.includes('\'compare-benchmark\':'), 'slot_card.js must handle compare-benchmark click');
assert.ok(slotCardJs.includes('爆款原片基准抽帧'), 'slot_card.js openSlotLightbox must pair benchmark reference frames');

// 7. Check app.js & media_renderer.js
const appJs = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const mediaRendererJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'media_renderer.js'), 'utf8');
assert.ok(appJs.includes('candidate-modal-benchmark-box'), 'app.js must render benchmark box in candidate selection modal');
assert.ok(mediaRendererJs.includes('/api/project/references'), 'media_renderer.js must auto-fetch project references');

console.log('All Benchmark Reference UI tests passed successfully!');
