const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'projects.js'), 'utf8');
const sandbox = {
  console,
  URLSearchParams,
  setTimeout: () => 1,
  clearTimeout: () => {},
  escapeHtml: value => String(value)
    .replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  document: {
    readyState: 'loading',
    addEventListener: () => {},
    getElementById: () => null,
    querySelectorAll: () => [],
  },
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'projects.js' });

async function testLightweightRefreshKeepsExistingCover() {
  vm.runInContext(`
    projectsRows = [{
      project_key: 'run_1',
      cover: '/outputs/run_1/cover_1.webp',
      assets: { cover: '/outputs/run_1/cover_1.webp', file_count: 1 }
    }];
    projectsLoading = false;
    renderProjects = function () {};
  `, sandbox);
  sandbox.fetch = async () => ({
    ok: true,
    json: async () => ({ projects: [{ project_key: 'run_1', cover: null, assets: null }] }),
  });

  await sandbox.refreshProjects({ assets: false, silent: true });
  const row = vm.runInContext('projectsRows[0]', sandbox);
  assert.equal(row.cover, '/outputs/run_1/cover_1.webp');
  assert.equal(row.assets.file_count, 1);
}

function testBrokenLibraryCoverFallsBackToDiskCover() {
  const html = sandbox.projectsCoverHtml({
    cover: '/outputs/old/missing.webp',
    assets: { cover: '/outputs/run_1/cover_2.webp' },
  });
  assert.match(html, /src="\/outputs\/old\/missing\.webp"/);
  assert.match(html, /data-fallback="\/outputs\/run_1\/cover_2\.webp"/);

  const img = { dataset: { fallback: '/outputs/run_1/cover_2.webp' }, src: '', outerHTML: '' };
  sandbox.projectsHandleCoverError(img);
  assert.equal(img.src, '/outputs/run_1/cover_2.webp');
  sandbox.projectsHandleCoverError(img);
  assert.match(img.outerHTML, /project-thumb-icon/);
}

function testOrphanJobsExposeSafeActions() {
  const failed = sandbox.projectsJobActionsHtml({ id: 'frames_failed', status: 'failed' });
  assert.match(failed, /data-act="delete-job"/);
  assert.match(failed, /data-job-id="frames_failed"/);
  assert.doesNotMatch(failed, /cancel-job/);

  const running = sandbox.projectsJobActionsHtml({ id: 'videos_running', status: 'running' });
  assert.match(running, /data-act="cancel-job"/);
  assert.match(running, /data-job-id="videos_running"/);
  assert.doesNotMatch(running, /delete-job/);
}

async function testOrphanJobActionsUseTheJobId() {
  const calls = [];
  sandbox.cancelTask = async id => calls.push(['cancel', id]);
  sandbox.deleteTask = async id => calls.push(['delete', id]);
  sandbox.refreshProjects = () => {};

  await sandbox.projectsRunAction('cancel-job', { task: null }, null, 'frames_123');
  await sandbox.projectsRunAction('delete-job', { task: null }, null, 'videos_456');
  assert.deepEqual(calls, [['cancel', 'frames_123'], ['delete', 'videos_456']]);
}

function testOrphanRowGetsItsOwnActionRow() {
  const orphan = {
    kind: 'job',
    title: '荒野钟表修缮室',
    sub_jobs: [
      { id: 'cover_1', type: 'cover', status: 'completed' },
      { id: 'frames_1', type: 'frames', status: 'running' },
    ],
  };
  const html = sandbox.projectsDetailActionsHtml(orphan);
  assert.match(html, /data-act="find-parent"/);
  assert.match(html, /data-act="cancel-all-jobs"[^>]*>[^<]*1/);
  assert.match(html, /data-act="delete-all-jobs"[^>]*>[^<]*1/);
  assert.match(html, /data-act="gallery-search"/);
  assert.match(html, /data-act="copy-title"/);
  // 没有 task 的行绝不能长出依赖 task.id 的按钮
  assert.doesNotMatch(html, /data-act="delete-task"/);
  assert.doesNotMatch(html, /data-act="rerun"/);

  // 有资产目录时走通用的「去画廊看资产」精确定位，不再给模糊搜索按钮
  const withAssets = sandbox.projectsDetailActionsHtml({
    ...orphan, assets: { file_count: 3, dir: 'outputs/run_1' },
  });
  assert.doesNotMatch(withAssets, /data-act="gallery-search"/);
  assert.match(withAssets, /data-act="gallery"/);

  // 普通项目行不受影响（「换模型再跑」的下拉要读宿主的模型配置）
  sandbox.config = { model: 'gpt-x' };
  sandbox.DEFAULT_CONFIG = { model: 'gpt-x' };
  const normal = sandbox.projectsDetailActionsHtml({
    kind: 'project', task: { id: 't1', status: 'completed' }, sub_jobs: [],
  });
  assert.doesNotMatch(normal, /find-parent|cancel-all-jobs|delete-all-jobs/);
}

async function testBulkJobActionsHitEachJobOnce() {
  const posted = [];
  sandbox.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    if (Array.isArray(body.task_ids)) {
      body.task_ids.forEach(id => posted.push([url, id]));
    } else {
      posted.push([url, body.task_id]);
    }
    return { ok: true, json: async () => ({}) };
  };
  sandbox.customConfirm = async () => true;
  sandbox.showToast = () => {};
  sandbox.refreshProjects = () => {};

  const p = {
    kind: 'job',
    sub_jobs: [
      { id: 'a', status: 'completed' },
      { id: 'b', status: 'running' },
      { id: 'c', status: 'failed' },
      { id: null, status: 'failed' },      // 没有 id 的作业不该被发出去
    ],
  };
  await sandbox.projectsRunAction('delete-all-jobs', p, null, '');
  await sandbox.projectsRunAction('cancel-all-jobs', p, null, '');
  assert.deepEqual(posted, [
    ['/api/tasks/delete', 'a'],
    ['/api/tasks/delete', 'c'],
    ['/api/compose-cancel', 'b'],
  ]);

  // 取消确认后一个请求都不该发
  posted.length = 0;
  sandbox.customConfirm = async () => false;
  await sandbox.projectsRunAction('delete-all-jobs', p, null, '');
  assert.deepEqual(posted, []);
}

async function testFindParentOmitsTheSyntheticJobKey() {
  const calls = [];
  sandbox.openSparkProject = async args => { calls.push(args); return true; };
  await sandbox.projectsRunAction('find-parent', {
    kind: 'job', project_key: 'job:荒野钟表修缮室', title: '荒野钟表修缮室', theme: '荒野钟表',
  }, null, '');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].projectKey, undefined);
  assert.equal(calls[0].title, '荒野钟表修缮室');
  assert.equal(calls[0].seed, '荒野钟表');
}


function testSubJobsAggregation() {
  const pWithMedia = {
    image_count: 21,
    video_count: 4,
    progress: { image_ready: 21, image_total: 21, video_ready: 4, video_total: 4 },
    sub_jobs: [
      { id: 'f_1', type: 'frames', status: 'failed' },
      { id: 'f_2', type: 'frames', status: 'failed' },
      { id: 'v_1', type: 'videos', status: 'failed' },
    ],
  };

  const agg = sandbox.projectsAggregateJobs(pWithMedia);
  assert.equal(agg.length, 2);
  assert.equal(agg[0].type, 'frames');
  assert.equal(agg[0].statusClass, 'completed');
  assert.equal(agg[0].icon, '✓');
  assert.equal(agg[0].label, '帧序列 21/21');

  assert.equal(agg[1].type, 'videos');
  assert.equal(agg[1].statusClass, 'completed');
  assert.equal(agg[1].icon, '✓');
  assert.equal(agg[1].label, '视频 4/4');

  const html = sandbox.projectsJobsHtml(pWithMedia);
  assert.match(html, /✓ 帧序列 21\/21/);
  assert.match(html, /✓ 视频 4\/4/);

  // 无媒体且失败的项目
  const pFailed = {
    image_count: 0,
    video_count: 0,
    sub_jobs: [
      { id: 'f_1', type: 'frames', status: 'failed' },
    ],
  };
  const aggFailed = sandbox.projectsAggregateJobs(pFailed);
  assert.equal(aggFailed[0].statusClass, 'failed');
  assert.equal(aggFailed[0].icon, '✕');
  assert.equal(aggFailed[0].label, '帧序列失败');
}

function testRowInnerHtmlIncludesOverlayAndActions() {
  const rowHtml = sandbox.projectsRowInnerHtml({
    project_key: 'test_p1',
    title: '河畔树皮棚改造成地下避世静室',
    state: 'completed',
    saved: true,
    cover: '/outputs/test.webp',
    image_count: 21,
    progress: { image_ready: 21, image_total: 21, video_ready: 0, video_total: 4 },
    assets: { file_count: 46, bytes: 151 * 1024 * 1024 },
    updated_at: 1787287928,
    sub_jobs: [
      { id: 'f1', type: 'frames', status: 'completed' },
      { id: 'f2', type: 'frames', status: 'completed' },
    ],
  });

  assert.match(rowHtml, /class="project-thumb-overlay"/);
  assert.match(rowHtml, /class="project-thumb-badges"/);
  assert.match(rowHtml, /data-act="open"/);
  assert.match(rowHtml, /data-act="gallery"/);
  assert.match(rowHtml, /class="project-badges-inline"/);
  assert.match(rowHtml, /图片 21\/21 · 视频 0\/4/);
  assert.doesNotMatch(rowHtml, /project-next-hint/, 'next-step explanation belongs in the selected project detail');
}

function testCurrentStatusUsesActualProgressAndLatestActivity() {
  const planned = { saved: true, image_count: 21, video_count: 4,
    progress: { image_ready: 0, image_total: 21, video_ready: 0, video_total: 4 } };
  assert.equal(sandbox.projectsCurrentStatus(planned).label, '待生成');
  assert.equal(sandbox.projectsCurrentStatus(planned).progress, '图片 0/21 · 视频 0/4');
  const complete = { ...planned, progress: { ...planned.progress, merged: true },
    has_failed_jobs: true, task: { status: 'failed' } };
  assert.equal(sandbox.projectsCurrentStatus(complete).label, '已成片');
  assert.doesNotMatch(sandbox.projectsBadgesHtml(complete), /失败|需要处理/);
  const retrying = { ...complete, sub_jobs: [{ type: 'videos', status: 'running' }] };
  assert.equal(sandbox.projectsCurrentStatus(retrying).label, '视频生成中');
  assert.equal(sandbox.projectsCurrentStatus(retrying).running, true);
  const partial = { ...complete, progress: { merged_available: true, merged_partial: true } };
  assert.equal(sandbox.projectsCurrentStatus(partial).label, '部分成片');
  assert.match(sandbox.projectsCurrentStatus(partial).next, /补齐/);
  assert.equal(sandbox.projectsCurrentStatus({ ...partial,
    progress: { merged_available: true, merged_stale: true } }).label, '成片待更新');
  const latest = sandbox.projectsAggregateJobs({ sub_jobs: [
    { type: 'videos', status: 'failed', last_active: 200 },
    { type: 'videos', status: 'completed', last_active: 100 },
  ] });
  assert.equal(latest[0].statusClass, 'failed');
  const noOutput = sandbox.projectsAggregateJobs({ image_count: 21, video_count: 4,
    sub_jobs: [{ type: 'videos', status: 'failed' }] });
  assert.equal(noOutput[0].statusClass, 'failed');
}

function testDetailsDefaultToCurrentProgressAndCollapsedHistory() {
  const pane = { innerHTML: '', dataset: {}, classList: { add() {}, remove() {} },
    querySelectorAll: () => [], querySelector: () => null };
  const ctx = { ...sandbox, config: { model: 'gpt-x' }, DEFAULT_CONFIG: { model: 'gpt-x' },
    document: { ...sandbox.document, getElementById: id => id === 'projects-detail' ? pane : null } };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  ctx.fixture = { project_key: 'internal-project-key', title: '示例项目', state: 'completed', saved: true,
    progress: { image_ready: 2, image_total: 2, video_ready: 1, video_total: 1, merged: true },
    task: { id: 'internal-task-id', status: 'completed' },
    sub_jobs: [{ id: 'historical-failure-id', type: 'videos', status: 'failed', error: '旧错误' }] };
  vm.runInContext('projectsRows = [fixture]; projectsSelectedKey = fixture.project_key;', ctx);
  ctx.renderProjectDetail();
  assert.match(pane.innerHTML, /已成片/);
  assert.match(pane.innerHTML, /<details class="projects-history" data-project-fold="history">/);
  assert.match(pane.innerHTML, /<details class="projects-technical" data-project-fold="technical">/);
  assert.match(pane.innerHTML, /<details class="projects-more-actions" data-project-fold="actions">/);
  assert.ok(pane.innerHTML.indexOf('打开项目') < pane.innerHTML.indexOf('更多操作'));
  assert.ok(pane.innerHTML.indexOf('data-project-fold="history"') < pane.innerHTML.indexOf('旧错误'));
  assert.ok(pane.innerHTML.indexOf('data-project-fold="technical"') < pane.innerHTML.indexOf('internal-project-key'));
  assert.doesNotMatch(pane.innerHTML, /<details[^>]+ open/);

  let writes = 0;
  let html = pane.innerHTML;
  Object.defineProperty(pane, 'innerHTML', { get: () => html, set: value => { writes++; html = value; } });
  ctx.renderProjectDetail();
  assert.equal(writes, 0, 'an unchanged poll must preserve the actual focused DOM node');

  let focusRestored = false;
  const history = { dataset: { projectFold: 'history' }, open: false,
    querySelector: () => ({ focus: () => { focusRestored = true; } }) };
  const technical = { dataset: { projectFold: 'technical' }, open: true };
  pane.querySelectorAll = selector => selector.endsWith('[open]') ? [history] : [history, technical];
  pane.contains = () => true;
  ctx.document.activeElement = { tagName: 'SUMMARY', closest: () => history };
  ctx.fixture.updated_at = 200;
  ctx.renderProjectDetail();
  assert.equal(history.open, true, 'polling preserves expanded records');
  assert.equal(technical.open, false, 'polling preserves collapsed technical details');
  assert.equal(focusRestored, true, 'changed progress restores the reader\'s disclosure focus');
  vm.runInContext("projectsRows = [{ ...fixture, project_key: 'another-project' }]; projectsSelectedKey = 'another-project';", ctx);
  ctx.renderProjectDetail();
  assert.equal(history.open, false, 'switching projects starts with collapsed records');
}

async function testArchiveHasASeparateSectionAndScopedQueries() {
  const makeButton = section => ({ dataset: { section }, textContent: section === 'active' ? '项目' : '项目归档',
    classList: { toggle(name, on) { this[name] = on; } },
    setAttribute(name, value) { this[name] = value; } });
  const buttons = [makeButton('active'), makeButton('archived')];
  const filters = { hidden: false };
  const search = { value: '测试', setAttribute(name, value) { this[name] = value; } };
  const hint = { textContent: '' };
  const ctx = { ...sandbox, document: { ...sandbox.document,
    getElementById: id => ({ 'projects-filters': filters, 'projects-search': search, 'projects-section-hint': hint })[id] || null,
    querySelectorAll: selector => selector.includes('projects-sections') ? buttons : [],
  } };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  const active = { project_key: 'active', title: '活动项目' };
  const archived = { project_key: 'archived', archived: true, title: '归档项目' };
  const pending = { project_key: 'pending', archive_pending: true, title: '未完成归档' };
  const calls = [];
  ctx.fetch = async url => {
    calls.push(new URLSearchParams(url.split('?')[1]));
    return { ok: true, json: async () => ({ projects: [active, archived, pending],
      counts: { active: 3, archived: 2, all: 3, running: 1, saved: 1 }, total_count: 3 }) };
  };
  for (const filter of ['all', 'running', 'completed', 'saved', 'failed']) {
    ctx.filter = filter;
    vm.runInContext('projectsFilter = filter;', ctx);
    await ctx.refreshProjects();
    assert.equal(calls.at(-1).get('scope'), 'active');
    assert.equal(calls.at(-1).get('state'), filter);
    assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(projectsRows.map(p => p.project_key))', ctx)), ['active'],
      'all ordinary status filters must exclude archived and pending archives');
  }
  vm.runInContext('projectsSearch = "测试"; projectsSort = "title"; projectsSelectedKey = "active"; projectsSelected.add("active");', ctx);
  await ctx.projectsSetSection('archived');
  assert.equal(calls.at(-1).get('scope'), 'archived');
  assert.equal(calls.at(-1).get('state'), 'archived');
  assert.equal(calls.at(-1).get('q'), '测试', 'archive search remains available');
  assert.equal(calls.at(-1).get('sort'), 'title', 'archive sorting remains available');
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(projectsRows.map(p => p.project_key))', ctx)), ['archived', 'pending']);
  assert.equal(vm.runInContext('projectsSelectedKey', ctx), null);
  assert.equal(vm.runInContext('projectsSelected.size', ctx), 0);
  assert.equal(vm.runInContext('projectsTotal', ctx), 2);
  assert.equal(filters.hidden, true);
  assert.equal(search.placeholder, '搜索归档项目名称');
  assert.equal(buttons[1]['aria-pressed'], 'true');
  assert.equal(buttons[0]['aria-pressed'], 'false');
  assert.equal(buttons[0].textContent, '项目 (3)');
  assert.equal(buttons[1].textContent, '项目归档 (2)');
  assert.match(hint.textContent, /归档成片/);
  await ctx.projectsSetSection('active');
  assert.equal(calls.at(-1).get('scope'), 'active');
  assert.equal(calls.at(-1).get('state'), 'all', 'returning to projects starts from the active all filter');
  assert.equal(filters.hidden, false);
  assert.equal(buttons[0]['aria-pressed'], 'true');
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  assert.match(html, /id="projects-sections"[^>]*aria-label="项目分区"/);
  assert.match(html, /data-section="archived"[^>]*>项目归档<\/button>/);
  assert.doesNotMatch(html, /projects-filter-chip[^>]*data-filter="archived"/);
}

(async () => {
  await testLightweightRefreshKeepsExistingCover();
  testBrokenLibraryCoverFallsBackToDiskCover();
  testOrphanJobsExposeSafeActions();
  await testOrphanJobActionsUseTheJobId();
  testOrphanRowGetsItsOwnActionRow();
  await testBulkJobActionsHitEachJobOnce();
  await testFindParentOmitsTheSyntheticJobKey();
  testCurrentStatusUsesActualProgressAndLatestActivity();
  testDetailsDefaultToCurrentProgressAndCollapsedHistory();
  await testArchiveHasASeparateSectionAndScopedQueries();
  testSubJobsAggregation();
  testRowInnerHtmlIncludesOverlayAndActions();
  console.log('projects UI regression tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
