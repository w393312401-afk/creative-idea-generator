const assert = require('assert');
const fs = require('fs');
const path = require('path');

const collageViewerJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'collage_viewer.js'), 'utf8');
const steppedPipelineJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'stepped_pipeline.js'), 'utf8');
const collageViewerCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'collage_viewer.css'), 'utf8');
const steppedPipelineCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'stepped_pipeline.css'), 'utf8');
const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

console.log('Testing Click-to-Enlarge (单独点击放大) functionality...');

// 1. Check Collage Viewer functions and structures
assert.ok(collageViewerJs.includes('function openCollageFrameLightbox'), 'collage_viewer.js must have openCollageFrameLightbox');
assert.ok(collageViewerJs.includes('function renderCollageHotspotsHtml'), 'collage_viewer.js must have renderCollageHotspotsHtml');
assert.ok(collageViewerJs.includes('cv-collage-hotspot-cell'), 'collage_viewer.js must render cv-collage-hotspot-cell');
assert.ok(collageViewerJs.includes('cv-dyn-cell-zoom-icon'), 'collage_viewer.js must have zoom icon on dynamic cells');
assert.ok(collageViewerJs.includes('open-lightbox-current'), 'collage_viewer.js must have toolbar button to open lightbox');
assert.ok(collageViewerJs.includes('tagA.addEventListener(\'click\''), 'collage_viewer.js must support clicking compare tag A to enlarge');
assert.ok(collageViewerJs.includes('tagB.addEventListener(\'click\''), 'collage_viewer.js must support clicking compare tag B to enlarge');

// Test collage frame lightbox item builder logic
const cv = require('../js/collage_viewer.js');
assert.strictEqual(typeof cv.openCollageFrameLightbox, 'function', 'openCollageFrameLightbox must be exported');
assert.strictEqual(typeof cv.openCollageViewer, 'function', 'openCollageViewer must be exported');

// 2. Check Stepped Pipeline functions and structures
assert.ok(steppedPipelineJs.includes('function renderSteppedBatchFrameThumbsHtml'), 'stepped_pipeline.js must have renderSteppedBatchFrameThumbsHtml');
assert.ok(steppedPipelineJs.includes('function renderSteppedFinalAllFramesThumbsHtml'), 'stepped_pipeline.js must have renderSteppedFinalAllFramesThumbsHtml');
assert.ok(steppedPipelineJs.includes('function openSteppedSequencesLightbox'), 'stepped_pipeline.js must have openSteppedSequencesLightbox');
assert.ok(steppedPipelineJs.includes('stepped-frame-thumb-card'), 'stepped_pipeline.js must render stepped-frame-thumb-card');
assert.ok(steppedPipelineJs.includes('stepped-frame-zoom-tag'), 'stepped_pipeline.js must render stepped-frame-zoom-tag');

// 3. Check CSS styles
assert.ok(collageViewerCss.includes('.cv-collage-hotspot-grid'), 'collage_viewer.css must have .cv-collage-hotspot-grid');
assert.ok(collageViewerCss.includes('.cv-collage-hotspot-cell'), 'collage_viewer.css must have .cv-collage-hotspot-cell');
assert.ok(collageViewerCss.includes('.cv-hotspot-tag'), 'collage_viewer.css must have .cv-hotspot-tag');
assert.ok(collageViewerCss.includes('.cv-dyn-cell:hover'), 'collage_viewer.css must have hover effect on .cv-dyn-cell');

assert.ok(steppedPipelineCss.includes('.stepped-batch-frame-section'), 'stepped_pipeline.css must have .stepped-batch-frame-section');
assert.ok(steppedPipelineCss.includes('.stepped-frame-thumb-card'), 'stepped_pipeline.css must have .stepped-frame-thumb-card');
assert.ok(steppedPipelineCss.includes('.stepped-thumb-zoom-badge'), 'stepped_pipeline.css must have .stepped-thumb-zoom-badge');

// 4. Check index.html hints
assert.ok(indexHtml.includes('点击多宫格可逐帧单独放大'), 'index.html must have tooltip for collage click to enlarge');

console.log('All Click-to-Enlarge (单独点击放大) unit tests passed successfully!');
