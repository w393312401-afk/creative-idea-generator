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

(async () => {
  await testLightweightRefreshKeepsExistingCover();
  testBrokenLibraryCoverFallsBackToDiskCover();
  testOrphanJobsExposeSafeActions();
  await testOrphanJobActionsUseTheJobId();
  testOrphanRowGetsItsOwnActionRow();
  await testBulkJobActionsHitEachJobOnce();
  await testFindParentOmitsTheSyntheticJobKey();
  console.log('projects UI regression tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
