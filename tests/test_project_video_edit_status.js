const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const timers = [];
const now = Date.parse('2026-10-02T10:05:00Z');
class TestDate extends Date { static now() { return now; } }
const context = {
    console, URLSearchParams, Date: TestDate,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout() {},
    escapeHtml: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;')
        .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    document: { readyState: 'loading', addEventListener() {}, getElementById: () => null, querySelectorAll: () => [] },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js/projects.js'), 'utf8'), context);
const project = {
    project_key: 'p1', kind: 'project', state: 'completed', saved: true,
    task: { status: 'completed' }, progress: { merged: true, video_ready: 3, video_total: 3 },
};
const edit = status => ({ id: 'edit-1', status, stage: status, created_at: '2026-10-02T10:00:00Z',
    updated_at: '2026-10-02T10:02:00Z', source: '/outputs/p1/final.mp4' });

for (const status of ['queued', 'running']) {
    const p = { ...project, video_edit: { ...edit(status), stage: status === 'running' ? 'rendering' : status } };
    const current = context.projectsCurrentStatus(p);
    assert.equal(current.label, status === 'queued' ? '精剪排队中' : '正在精剪');
    assert.equal(current.state, 'running');
    assert.equal(current.running, true);
    assert.match(current.progress, /5 分 0 秒/);
    assert.match(current.progress, status === 'running' ? /导出精剪视频/ : /等待开始/);
    assert.equal(context.projectsIsRunning(p), true);
    assert.equal(context.projectsCanArchive(p), false);
    assert.match(context.projectsRowInnerHtml(p), /精剪/);
    context.pollingRows = [p];
    vm.runInContext('projectsRows = pollingRows; projectsTabActive = true; projectsSchedulePoll();', context);
    assert.equal(timers.at(-1).ms, 4000);
}
const running = { ...project, video_edit: edit('running') };
const stopping = { ...running, video_edit: { ...running.video_edit, stage: 'cancelling' } };
assert.equal(context.projectsCurrentStatus(stopping).label, '正在停止精剪');
assert.notEqual(context.projectsRowSignature(running), context.projectsRowSignature(stopping));
assert.notEqual(context.projectsRowSignature(running), context.projectsRowSignature({ ...running,
    video_edit: { ...running.video_edit, message: '正在复核' } }));
const completed = { ...project, video_edit: edit('completed') };
assert.equal(context.projectsCurrentStatus(completed).label, '精剪完成');
assert.match(context.projectsCurrentStatus(completed).progress, /耗时 2 分 0 秒/);
assert.equal(context.projectsIsRunning(completed), false);
assert.equal(context.projectsCurrentStatus({ ...completed, video_edit: { ...completed.video_edit, output_missing: true } }).label,
    '精剪文件不可用');
assert.equal(context.projectsCurrentStatus({ ...completed, video_edit: { ...completed.video_edit, source_changed: true } }).label,
    '旧版精剪完成');
for (const [status, label] of [['failed', '精剪失败'], ['cancelled', '精剪已取消'], ['interrupted', '精剪已中断']]) {
    const current = context.projectsCurrentStatus({ ...project, video_edit: edit(status) });
    assert.equal(current.label, label);
    assert.equal(current.running, false);
}
assert.equal(context.projectsCurrentStatus({ ...completed, archived: true, archive: { final_videos: [] } }).label, '已归档');
assert.equal(context.projectsCurrentStatus({ ...completed, archive_pending: true }).label, '归档待完成');
assert.equal(context.projectsCurrentStatus({ ...completed, sub_jobs: [{ type: 'videos', status: 'running' }] }).label, '视频生成中',
    'past edits do not hide a current generation job');
assert.equal(context.projectsCurrentStatus({ ...completed, progress: { merged_stale: true } }).label, '成片待更新');
assert.equal(context.projectsCurrentStatus(project).label, '已成片', 'existing projects without an edit keep their media state');
assert.doesNotMatch(context.projectsCurrentStatusHtml({ ...running, video_edit: { ...running.video_edit, message: '<script>alert(1)</script>' } }),
    /<script>/);
context.fetch = async () => ({ ok: true, json: async () => ({ projects: [running], counts: { active: 1, archived: 0 } }) });
context.renderProjects = () => {};
vm.runInContext('projectsRows = []; projectsLoading = false; projectsTabActive = true; projectsSchedulePoll();', context);
assert.equal(timers.at(-1).ms, 30000);
context.refreshProjects({ assets: false }).then(() => {
    assert.equal(timers.at(-1).ms, 4000, 'first load of an active edit immediately switches to fast polling');
    console.log('Project video edit status regression tests passed');
}).catch(error => { console.error(error); process.exitCode = 1; });
