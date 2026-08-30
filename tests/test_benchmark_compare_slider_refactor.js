/**
 * Unit tests for Benchmark Compare Slider & Collage Viewer Refactoring
 * (原片对标滑块重构与交互式多宫格检查器测试)
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

console.log('Testing Benchmark Compare Slider Refactoring...');

const cvJsPath = path.join(__dirname, '..', 'js', 'collage_viewer.js');
const cvCssPath = path.join(__dirname, '..', 'css', 'app', 'collage_viewer.css');
const toolbarJsPath = path.join(__dirname, '..', 'js', 'slot_toolbar.js');

const cvJs = fs.readFileSync(cvJsPath, 'utf8');
const cvCss = fs.readFileSync(cvCssPath, 'utf8');
const toolbarJs = fs.readFileSync(toolbarJsPath, 'utf8');

const cv = require('../js/collage_viewer.js');

// 1. Exported Symbols
assert.strictEqual(typeof cv.openCollageViewer, 'function', 'openCollageViewer must be exported');
assert.strictEqual(typeof cv.openBenchmarkCompare, 'function', 'openBenchmarkCompare must be exported');
assert.strictEqual(typeof cv.closeCollageViewer, 'function', 'closeCollageViewer must be exported');
assert.strictEqual(typeof cv.openCollageFrameLightbox, 'function', 'openCollageFrameLightbox must be exported');
assert.strictEqual(typeof cv.buildBeatAxis, 'function', 'buildBeatAxis must be exported');
assert.ok(cv.collageViewerState, 'collageViewerState must be exported');

// 2. buildBeatAxis: Decoupling beat axis from generation progress
const mockIdea17 = {
    title: 'TestProject',
    image_count: 17,
    ref_frames: {
        1: '/outputs/TestProject/refs/ref_001.png',
        2: '/outputs/TestProject/refs/ref_002.png',
        3: '/outputs/TestProject/refs/ref_003.png',
        17: '/outputs/TestProject/refs/ref_017.png'
    },
    frameRun: {
        frames: [
            { sequence: 1, url: '/outputs/TestProject/frames/img_001.webp' } // Only 1 generated frame
        ]
    }
};

const axis17 = cv.buildBeatAxis(mockIdea17, mockIdea17.frameRun.frames, mockIdea17.ref_frames, 17);
assert.strictEqual(axis17.length, 17, 'Beat axis must contain all 17 beats even if only 1 frame is generated');

// Beat 1: Has gen and has ref
const beat1 = axis17.find(b => b.seq === 1);
assert.ok(beat1, 'Beat 1 must exist');
assert.strictEqual(beat1.hasGen, true, 'Beat 1 must have hasGen=true');
assert.strictEqual(beat1.hasRef, true, 'Beat 1 must have hasRef=true');
assert.strictEqual(beat1.genUrl, '/outputs/TestProject/frames/img_001.webp');

// Beat 2: Missing gen, has ref
const beat2 = axis17.find(b => b.seq === 2);
assert.ok(beat2, 'Beat 2 must exist');
assert.strictEqual(beat2.hasGen, false, 'Beat 2 must have hasGen=false');
assert.strictEqual(beat2.hasRef, true, 'Beat 2 must have hasRef=true');
assert.strictEqual(beat2.refUrl, '/outputs/TestProject/refs/ref_002.png');

// Beat 5: Missing gen, missing ref (placeholder available)
const beat5 = axis17.find(b => b.seq === 5);
assert.ok(beat5, 'Beat 5 must exist');
assert.strictEqual(beat5.hasGen, false, 'Beat 5 must have hasGen=false');
assert.strictEqual(beat5.hasRef, false, 'Beat 5 must have hasRef=false');

// 3. Issue Severity Extraction in buildBeatAxis
const mockIdeaWithIssues = {
    title: 'IssueProject',
    image_count: 3,
    ref_frames: { 1: 'r1', 2: 'r2', 3: 'r3' },
    frameRun: {
        frames: [
            { sequence: 1, url: 'u1', quality_gate: 'frame_continuity_failed', continuity_check: { reason: 'Camera elevation jump' } },
            { sequence: 2, url: 'u2', prompt_dirty: true },
            { sequence: 3, url: 'u3', quality_gate: 'auto_approved' }
        ]
    }
};
const axisIssues = cv.buildBeatAxis(mockIdeaWithIssues, mockIdeaWithIssues.frameRun.frames, mockIdeaWithIssues.ref_frames, 3);
assert.strictEqual(axisIssues[0].severity, 'failed', 'Frame 1 must be marked as failed');
assert.strictEqual(axisIssues[0].isFixable, true, 'Frame 1 must be fixable');
assert.strictEqual(axisIssues[0].issueReason, 'Camera elevation jump');

assert.strictEqual(axisIssues[1].severity, 'dirty', 'Frame 2 must be marked as dirty');
assert.strictEqual(axisIssues[1].isFixable, true, 'Frame 2 must be fixable');

assert.strictEqual(axisIssues[2].severity, 'none', 'Frame 3 must be clean');

// 4. Check AbortController and Pointer Events in code
assert.ok(cvJs.includes('new AbortController()'), 'collage_viewer.js must use AbortController');
assert.ok(cvJs.includes('abortController.abort()'), 'collage_viewer.js must abort controller on close');
assert.ok(cvJs.includes('setPointerCapture'), 'collage_viewer.js must use setPointerCapture for pointer events');
assert.ok(cvJs.includes('releasePointerCapture'), 'collage_viewer.js must release pointer capture');

// 5. Check 4 Compare Modes and Space Blink in code
assert.ok(cvJs.includes('compare-mode-split'), 'collage_viewer.js must support SPLIT mode');
assert.ok(cvJs.includes('compare-mode-fade'), 'collage_viewer.js must support FADE mode');
assert.ok(cvJs.includes('compare-mode-diff'), 'collage_viewer.js must support DIFF mode');
assert.ok(cvJs.includes('compare-mode-side'), 'collage_viewer.js must support SIDE mode');
assert.ok(cvJs.includes('cv-is-blinking'), 'collage_viewer.js must support Space key blinking');

// 6. Check Dual-Row Filmstrip and Issue Bar
assert.ok(cvJs.includes('cv-filmstrip'), 'collage_viewer.js must render cv-filmstrip');
assert.ok(cvJs.includes('cv-strip-gen'), 'collage_viewer.js must render cv-strip-gen row');
assert.ok(cvJs.includes('cv-strip-ref'), 'collage_viewer.js must render cv-strip-ref row');
assert.ok(cvJs.includes('cv-issue-bar'), 'collage_viewer.js must render cv-issue-bar');
assert.ok(cvJs.includes('data-act="fix-current-beat"'), 'collage_viewer.js must have fix beat action button');
assert.ok(cvJs.includes('data-act="retry-current-beat"'), 'collage_viewer.js must have retry beat action button');

// 7. Check CSS Rules
assert.ok(cvCss.includes('.cv-stage'), 'collage_viewer.css must define .cv-stage');
assert.ok(cvCss.includes('.cv-mode-split'), 'collage_viewer.css must define .cv-mode-split');
assert.ok(cvCss.includes('.cv-mode-fade'), 'collage_viewer.css must define .cv-mode-fade');
assert.ok(cvCss.includes('.cv-mode-diff'), 'collage_viewer.css must define .cv-mode-diff');
assert.ok(cvCss.includes('.cv-mode-side'), 'collage_viewer.css must define .cv-mode-side');
assert.ok(cvCss.includes('.cv-filmstrip'), 'collage_viewer.css must define .cv-filmstrip');
assert.ok(cvCss.includes('.cv-issue-bar'), 'collage_viewer.css must define .cv-issue-bar');
assert.ok(cvCss.includes('.cv-split-divider:focus-visible'), 'collage_viewer.css must define focus-visible for divider');

// 8. Check Toolbar Integration
assert.ok(toolbarJs.includes('openBenchmarkCompare'), 'slot_toolbar.js must integrate openBenchmarkCompare');

// ── 9. 回归：window 键盘监听整场只挂一次 ──────────────────────────
// renderCollageViewerModal() 每次全量重绘都会调 bindCollageViewerEvents()。
// 若 keydown 跟着一起重挂，切一次拼图源/开一次辅助线就多叠一层，
// 按一下「→」会一次性跳好几拍——这正是重构要消灭的那个缺陷。
assert.strictEqual(
    (cvJs.match(/window\.addEventListener\('keydown'/g) || []).length, 1,
    'window keydown must be registered from exactly one place');
assert.ok(cvJs.includes('function bindGlobalKeyEvents()'),
    'global key listeners must live in their own once-only binder');
assert.ok(cvJs.includes('if (st.globalKeysBound) return;'),
    'bindGlobalKeyEvents must be idempotent');
assert.ok(/function bindCollageViewerEvents\(modal\) \{[\s\S]{0,220}domAbortController\.abort\(\)/.test(cvJs),
    'bindCollageViewerEvents must abort the previous DOM-level controller first');
assert.ok(cvJs.includes('function cvDomSignal()'),
    'DOM listeners must share a per-render signal helper');

// ── 10. 回归：切换对比形态后整棵 compare 子树都要重绑 ──────────────
// setCompareMode 重建 #cv-main-body，辅助线/放大镜/审查结论带都长在这棵树里。
const setModeBody = cvJs.slice(cvJs.indexOf('function setCompareMode('),
    cvJs.indexOf('function setSplitRatio('));
assert.ok(setModeBody.includes('bindCompareSliderEvents('), 'setCompareMode must rebind slider events');
assert.ok(setModeBody.includes('bindGuidelinesAndLoupeEvents('), 'setCompareMode must rebind guideline/loupe events');
assert.ok(setModeBody.includes('bindIssueBarEvents('), 'setCompareMode must rebind issue bar events');
assert.ok(setModeBody.includes("st.mode !== 'compare'"), 'setCompareMode must not rewrite the collage canvas');

// ── 11. 回归：SIDE 模式换拍必须真的换图 ────────────────────────────
assert.ok(cvJs.includes('function applySidePane('), 'side-by-side panes need their own updater');
assert.ok(/compareMode === 'side'[\s\S]{0,400}applySidePane/.test(cvJs),
    'updateCompareStageContent must handle the side layout');

// ── 12. 回归：分割比例以 stage 为基准，而非整个视口 ────────────────
const sliderBody = cvJs.slice(cvJs.indexOf('function bindCompareSliderEvents('),
    cvJs.indexOf('function bindGuidelinesAndLoupeEvents('));
assert.ok(sliderBody.includes("modal.querySelector('#cv-compare-stage')"),
    'split drag must measure the stage, not the viewport');
assert.ok(!/const rect = splitViewport\.getBoundingClientRect\(\)/.test(sliderBody),
    'split ratio must not be derived from the viewport rect');
assert.ok(sliderBody.includes("stageEl.addEventListener('pointerdown'"),
    'the whole stage must be draggable (a 2px divider is not a hit target)');

// ── 13. 回归：猜出来的帧路径不得谎报"已生成" ──────────────────────
assert.ok(cvJs.includes('function cvMarkBrokenImage('), 'broken images must degrade in place');
assert.ok(cvJs.includes('synthetic: true'), 'fabricated frame paths must be flagged');
const axisSynthetic = cv.buildBeatAxis(
    { title: 'T', image_count: 3 },
    [{ sequence: 1, url: 'u1', synthetic: true }],
    {}, 3);
assert.strictEqual(axisSynthetic[0].synthetic, true, 'buildBeatAxis must surface the synthetic flag');
assert.strictEqual(axisSynthetic[1].synthetic, false, 'real absence must not be marked synthetic');

// ── 14. 回归：CSS 选择器与工具条入口 ───────────────────────────────
assert.ok(cvCss.includes('.cv-stage.cv-mode-side'),
    'SIDE mode must target .cv-stage itself, not a descendant');
assert.ok(!/\.cv-mode-side\s+\.cv-stage\s*\{/.test(cvCss),
    'the never-matching descendant selector must be gone');
assert.ok(/\.cv-stage\s*\{[^}]*isolation:\s*isolate/.test(cvCss),
    'DIFF blending must be isolated to the stage');
assert.ok(cvCss.includes('.cv-strip-cell.cv-img-broken::after'),
    'filmstrip must show a placeholder when a guessed path 404s');

assert.ok(!toolbarJs.includes('image-slot-grid'),
    'slot_toolbar.js must not reference a grid id that does not exist');
assert.ok(toolbarJs.includes('function firstVisibleSlotSeq('),
    'toolbar entry must resolve the seq from the real grid');
assert.ok(!/curIdea\.title \|\|/.test(toolbarJs.slice(
    toolbarJs.indexOf('const hasIdea'), toolbarJs.indexOf('bmBtn.hidden'))),
    'benchmark button must require actual reference material, not just a title');

console.log('All Benchmark Compare Slider Refactoring tests passed successfully!');
