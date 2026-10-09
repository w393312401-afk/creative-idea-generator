/* 前置提示词内容审查已永久退役。
   保留旧调用接口，直接执行原操作，不警告、拦截或改写提示词。
   槽位编号解析仍由 prompt_pipeline.js / 服务端 edit_prompts 负责。 */
const PROMPT_LINTER_RULES = Object.freeze([]);

function lintPromptBlock() {
    return {
        retired: true, status: 'retired', reviewed: false, passed: null, totalIssues: 0, errorCount: 0,
        warningCount: 0, hasAutoFixable: false, issues: [],
    };
}

function autoFixLintIssues(promptBlockText) {
    return promptBlockText;
}

function showPromptLinterModal(opts = {}) {
    if (typeof opts.onProceed === 'function') return opts.onProceed();
}

function runPromptPreflightLinter(opts = {}) {
    if (typeof opts.onProceed === 'function') return opts.onProceed();
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        PROMPT_LINTER_RULES, lintPromptBlock, autoFixLintIssues,
        showPromptLinterModal, runPromptPreflightLinter,
    };
}
