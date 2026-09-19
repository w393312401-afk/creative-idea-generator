const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const consoleSource = fs.readFileSync(path.join(root, 'console.js'), 'utf8');
const consoleHtml = fs.readFileSync(path.join(root, 'console.html'), 'utf8');
for (const obsolete of ['tab-models', 'tab-playground', 'sandbox-image-input']) {
    assert.ok(!consoleHtml.includes(`id="${obsolete}"`));
}
for (const obsolete of ['MODELS_DATA', 'setupMarketplaceFilters', 'setupVisualUploader', 'videoPollInterval']) {
    assert.ok(!consoleSource.includes(obsolete));
}
assert.ok(consoleHtml.includes('id="tab-docs"'));
assert.ok(consoleHtml.includes('id="tab-google-fx"'));

const projectSource = fs.readFileSync(path.join(root, 'js/projects.js'), 'utf8');
for (const [idea, savedId, taskId, expected] of [
    [null, '', '', ['projects']], [{id: 'saved'}, '', '', []],
    [null, 'saved', '', []], [null, '', 'running', []],
]) {
    const tabs = [];
    const element = { addEventListener() {}, classList: { toggle() {} } };
    const ctx = vm.createContext({
        currentIdea: idea, console, URLSearchParams,
        localStorage: { getItem(key) { return {spark_current_idea_id: savedId, spark_active_task_id: taskId}[key] || null; } },
        switchMainTab: tab => tabs.push(tab),
        document: {readyState: 'loading', addEventListener() {},
            getElementById: id => id === 'projects-list' ? element : null,
            querySelectorAll: () => []},
    });
    vm.runInContext(projectSource, ctx);
    vm.runInContext('initProjects()', ctx);
    assert.deepEqual(tabs, expected);
}

async function checkPolling() {
    let calls = 0;
    let reject;
    const ctx = vm.createContext({
        document: { hidden: false }, dashboardRequestPending: false,
        statusAppDot: null, statusAppText: null, statusGatewayDot: null,
        statusGatewayText: null, statServerMode: null,
        fetch: () => { calls++; return new Promise((_, fail) => { reject = fail; }); },
    });
    const start = consoleSource.indexOf('  async function checkSystemStatuses()');
    const end = consoleSource.indexOf('  function renderTasksTable(', start);
    assert.ok(start >= 0 && end > start);
    vm.runInContext(consoleSource.slice(start, end), ctx);
    const first = ctx.checkSystemStatuses();
    await ctx.checkSystemStatuses();
    assert.equal(calls, 1, 'in-flight refresh must not overlap');
    reject(new Error('offline'));
    await first;
    assert.equal(ctx.dashboardRequestPending, false, 'failure releases in-flight guard');
    ctx.document.hidden = true;
    await ctx.checkSystemStatuses();
    assert.equal(calls, 1, 'hidden page must not poll');
}
checkPolling().then(() => console.log('slimming UI contracts passed')).catch(error => {
    console.error(error);
    process.exitCode = 1;
});
