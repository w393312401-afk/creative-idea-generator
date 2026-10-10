const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const linter = require('../js/prompt_linter.js');

assert.equal(linter.PROMPT_LINTER_RULES.length, 0, 'All content review rules are retired');
let modals = 0;
const ctx = vm.createContext({
    document: { createElement() { modals++; throw new Error('No preflight dialogs allowed'); } },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/prompt_linter.js'), 'utf8'), ctx);
const examples = [
    '', null, undefined,
    '图片 1:\n50% w/ bg props approx. a wet floor with zigzag neon.',
    '图片 3 [FURNISHING]:\nA leaking ceiling, cracked concrete floor, work tripods and power cables.',
    '图片 1:\n（在此填写第 1 张图片的提示词）',
    '图片 1:\nA one-point perspective with an endless narrow tunnel and vanishing point.',
    '图片 1:\nMiniature diorama on an isometric diamond grid, two figurines remain standing in clean royal blue shirts.',
];
let proceeded = 0, fixed = 0, cancelled = 0;
for (const promptBlock of examples) {
    const report = linter.lintPromptBlock(promptBlock, { force: true });
    assert.equal(report.retired, true);
    assert.equal(report.passed, null);
    assert.equal(report.reviewed, false);
    assert.equal(report.totalIssues, 0);
    assert.equal(report.errorCount, 0);
    assert.equal(report.warningCount, 0);
    assert.deepEqual(report.issues, []);
    assert.equal(linter.autoFixLintIssues(promptBlock), promptBlock, 'Retirement must not rewrite supplied text');
    const result = ctx.runPromptPreflightLinter({
        promptBlock,
        onProceed: () => { proceeded++; return 'continued'; },
        onAutoFixApplied: () => fixed++, onCancelOrEdit: () => cancelled++,
    });
    assert.equal(result, 'continued');
}
ctx.showPromptLinterModal({ report: { passed: false, errorCount: 99 }, onProceed: () => proceeded++ });
assert.equal(proceeded, examples.length + 1, 'Even old direct modal callers proceed once');
assert.equal(modals, 0);
assert.equal(fixed, 0);
assert.equal(cancelled, 0);

// Retirement leaves the existing numbered slot parser available and unchanged.
const pipeline = fs.readFileSync(path.join(__dirname, '../js/prompt_pipeline.js'), 'utf8');
vm.runInContext(pipeline.slice(0, pipeline.indexOf('\n}\n', pipeline.indexOf('function parsePromptBlock')) + 3), ctx);
const slots = ctx.parsePromptBlock('图片 1:\nFirst image.\n\n图片 2:\nSecond image.\n\n视频 1:\nMotion.');
assert.equal(slots.length, 3);
assert.equal(slots[0].type, 'image');
assert.equal(slots[1].index, 2);
assert.equal(slots[2].type, 'video');
console.log('Retired prompt preflight: all old risks pass, text is preserved, no modal, slot parsing retained.');
