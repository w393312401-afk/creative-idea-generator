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


// 「♻️ 二创」按钮：激发维度页下线后，remix_seed 那条路已经没有落点，按钮改为把项目
// 对应的复刻作业在爆款复刻面板里打开。所以它只对**复刻来的**项目露出——给激发出来
// 的项目留一个必然落空的入口，正是上一版的毛病。
function testRemixButtonOnlyShowsForReplicaProjects() {
  const replicaProject = { title: '复刻项目', saved: true, library: { id: 'replica_ab12cd34ef56' } };
  const ideatedProject = { title: '激发项目', saved: true, library: { id: 'idea_9f8e7d' } };

  assert.equal(sandbox.projectReplicaJobId(replicaProject), 'replica_ab12cd34ef56');
  assert.equal(sandbox.projectReplicaJobId(ideatedProject), '');
  // 老任务只在 dimensions 里带 replica_job_id
  assert.equal(
    sandbox.projectReplicaJobId({ task: { dimensions: { replica_job_id: 'replica_legacy01' } } }),
    'replica_legacy01');

  assert.match(sandbox.projectsDetailActionsHtml(replicaProject), /data-act="remix"/);
  assert.doesNotMatch(sandbox.projectsDetailActionsHtml(ideatedProject), /data-act="remix"/);
}

// 取数必须排在切页之前：switchMainTab 触发的 replicaTabEntered 在 replicaState 为空时
// 会打开作业列表的第一条，先切页就是让两个请求赛跑。
async function testRemixLoadsTheJobBeforeSwitchingTabs() {
  const order = [];
  sandbox.switchMainTab = tab => order.push(`switch:${tab}`);
  sandbox.replicaLoadJob = async jid => {
    order.push(`load:${jid}`);
    return { job_id: jid, beats: { beats: [{ id: 'B01' }, { id: 'B02' }] } };
  };
  sandbox.replicaFocusSection = () => true;
  sandbox.showToast = () => {};

  await sandbox.startProjectRemix({ title: 'X', library: { id: 'replica_zzz111' } });
  assert.deepEqual(order, ['load:replica_zzz111', 'switch:replica']);
}

// 没有节拍阶梯就没有二创可做（复刻面板的 canMutate = beatsCount > 0）。这时候不能
// 一声不吭地把人扔在页面上——上一版的假提示就是这么来的。
async function testRemixWithoutBeatsSaysSo() {
  const toasts = [];
  sandbox.switchMainTab = () => {};
  sandbox.replicaLoadJob = async jid => ({ job_id: jid, beats: { beats: [] } });
  sandbox.replicaFocusSection = () => true;
  sandbox.showToast = msg => toasts.push(msg);

  await sandbox.startProjectRemix({ title: 'X', library: { id: 'replica_nobeats' } });
  assert.equal(toasts.length, 1);
  assert.match(toasts[0], /节拍阶梯/);
}

// 取作业失败时不切页：切过去只会看到上一条作业，比留在原地更让人困惑。
async function testRemixDoesNotSwitchWhenLoadFails() {
  const toasts = [];
  let switched = false;
  sandbox.switchMainTab = () => { switched = true; };
  sandbox.replicaLoadJob = async () => { throw new Error('job 不存在'); };
  sandbox.showToast = msg => toasts.push(msg);

  await sandbox.startProjectRemix({ title: 'X', library: { id: 'replica_gone' } });
  assert.equal(switched, false);
  assert.match(toasts[0], /打开复刻作业失败/);
}

function testSubJobsAggregation() {
  const pWithMedia = {
    image_count: 21,
    video_count: 4,
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
  assert.equal(agg[0].label, '21帧序列');

  assert.equal(agg[1].type, 'videos');
  assert.equal(agg[1].statusClass, 'completed');
  assert.equal(agg[1].icon, '✓');
  assert.equal(agg[1].label, '4镜视频');

  const html = sandbox.projectsJobsHtml(pWithMedia);
  assert.match(html, /✓ 21帧序列/);
  assert.match(html, /✓ 4镜视频/);

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
  assert.match(rowHtml, /✓ 21帧序列/);
}

(async () => {
  await testLightweightRefreshKeepsExistingCover();
  testBrokenLibraryCoverFallsBackToDiskCover();
  testOrphanJobsExposeSafeActions();
  await testOrphanJobActionsUseTheJobId();
  testOrphanRowGetsItsOwnActionRow();
  await testBulkJobActionsHitEachJobOnce();
  await testFindParentOmitsTheSyntheticJobKey();
  testRemixButtonOnlyShowsForReplicaProjects();
  await testRemixLoadsTheJobBeforeSwitchingTabs();
  await testRemixWithoutBeatsSaysSo();
  await testRemixDoesNotSwitchWhenLoadFails();
  testSubJobsAggregation();
  testRowInnerHtmlIncludesOverlayAndActions();
  console.log('projects UI regression tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
