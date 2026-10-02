const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'projects.js'), 'utf8');
const files = [
  { url: '/outputs/p1/refined.mp4', name: 'refined.mp4', kind: 'refined_video' },
  { url: '/outputs/p1/beats.json', name: 'beats.json', kind: 'beats' },
  { url: '/outputs/p1/prompts.md', name: 'prompts.md', kind: 'prompts' },
];
const archive = { archived_at: 1790784000, retained_files: files,
  refined_videos: [files[0]], beats_url: files[1].url, prompts_url: files[2].url, deleted_bytes: 4096 };
const normal = { project_key: 'p1', kind: 'project', title: '项目一', saved: true,
  library: { id: 'lib1' }, task: { id: 'task1', status: 'completed' },
  assets: { dir: 'outputs/p1', file_count: 80, bytes: 8192 } };

function context() {
  const storage = new Map();
  const toasts = [];
  const ctx = {
    console: { log() {}, warn() {}, error() {} }, URLSearchParams,
    setTimeout: () => 1, clearTimeout() {},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
      .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    localStorage: { getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    document: { readyState: 'loading', addEventListener() {}, getElementById: () => null,
      querySelectorAll: () => [] },
    showToast: (...args) => toasts.push(args),
    switchMainTab() {}, config: { model: 'test' }, DEFAULT_CONFIG: { model: 'test' },
    savedIdeas: [], currentIdea: null,
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  ctx.storage = storage;
  ctx.toasts = toasts;
  return ctx;
}

function silenceRendering(ctx) {
  ctx.renderProjects = () => {};
  ctx.renderProjectsBulkBar = () => {};
  ctx.renderProjectDetail = () => {};
}

function result(data, ok = true) {
  return { ok, status: ok ? 200 : 409, json: async () => data };
}

async function testPreviewMustPrecedeConfirmationAndExecution() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.refreshProjects = async () => {};
  const calls = [];
  ctx.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, body });
    return result(body.preview ? { status: 'ok', projects: [{ project_key: 'p1', title: '<script>x</script>',
      retained_files: files, delete_count: 77, delete_bytes: 4096 }], errors: [] }
      : { status: 'ok', projects: [{ project_key: 'p1', archive }], count: 1, deleted_bytes: 4096, errors: [] });
  };
  ctx.customConfirm = async (message, label) => {
    assert.equal(calls.length, 1);
    assert.equal(calls[0].body.preview, true);
    assert.match(message, /成片视频（如有）、节拍数据文件和全套提示词/);
    assert.match(message, /保留精剪后的成片视频/);
    assert.match(message, /永久删除，无法恢复/);
    assert.match(message, /77 个文件/);
    assert.match(message, /refined\.mp4/);
    assert.match(message, /&lt;script&gt;/);
    assert.doesNotMatch(message, /<script>/);
    assert.match(label, /永久删除/);
    return true;
  };
  await ctx.projectsArchive([normal]);
  assert.deepEqual(calls, [
    { url: '/api/projects/archive', body: { project_keys: ['p1'], preview: true } },
    { url: '/api/projects/archive', body: { project_keys: ['p1'], preview: false } },
  ]);
  assert.equal(ctx.toasts.at(-1)[1], 'success');
  assert.equal(vm.runInContext('projectsSection', ctx), 'archived', 'successful archive moves into its own section');
  assert.equal(vm.runInContext('projectsSelectedKey', ctx), 'p1', 'the archived project remains selected');
}

async function testCancelAndPreviewFailureNeverDelete() {
  for (const mode of ['cancel', 'preview-error', 'unavailable-confirm']) {
    const ctx = context();
    silenceRendering(ctx);
    ctx.refreshProjects = async () => {};
    const calls = [];
    ctx.fetch = async (url, init) => {
      calls.push([url, JSON.parse(init.body)]);
      return mode === 'preview-error' ? result({ error: '缺少全套提示词' }, false)
        : result({ status: 'ok', projects: [{ project_key: 'p1', retained_files: files }], errors: [] });
    };
    if (mode === 'cancel') ctx.customConfirm = async () => false;
    await ctx.projectsArchive([normal]);
    assert.equal(calls.length, 1, mode);
    assert.equal(calls[0][0], '/api/projects/archive');
    assert.equal(calls[0][1].preview, true);
    assert.equal(vm.runInContext('projectsArchiving', ctx), false);
    if (mode !== 'cancel') assert.equal(ctx.toasts.at(-1)[1], 'error');
  }
}

async function testPartialSuccessKeepsFailedProjectsAndClearsOldMediaState() {
  const ctx = context();
  silenceRendering(ctx);
  let refreshed = 0;
  ctx.refreshProjects = async () => { refreshed++; };
  const failed = { ...normal, project_key: 'p2', title: '失败项目', library: { id: 'lib2' }, task: { id: 'task2' } };
  const missing = { ...normal, project_key: 'p3', title: '缺少提示词', library: { id: 'lib3' } };
  const running = { ...normal, project_key: 'p4', sub_jobs: [{ id: 'v4', status: 'running' }] };
  const archived = { ...normal, project_key: 'p5', archived: true, archive };
  ctx.rows = [normal, failed, missing, running, archived];
  ctx.savedIdeas = [{ id: 'lib1', project_key: 'p1', frameRun: { videos: ['old.mp4'] } },
    { id: 'lib2', project_key: 'p2', prompt_block: '应保留' }];
  ctx.currentIdea = ctx.savedIdeas[0];
  ctx.storage.set('spark_current_idea', JSON.stringify(ctx.currentIdea));
  ctx.storage.set('spark_current_idea_id', 'lib1');
  ctx.storage.set('spark_active_task_id', 'task1');
  ctx.storage.set('spark_active_task_dimensions', '{}');
  ctx.storage.set('spark_prompt_history_lib1', '[]');
  ctx.storage.set('spark_prompt_history_lib2', '[]');
  ctx.storage.set('spark_active_background_tasks', JSON.stringify({ tasks: [
    { ideaId: 'lib1', taskId: 'old-v1' }, { ideaId: 'lib2', taskId: 'task2' },
  ] }));
  vm.runInContext('projectsRows = rows; rows.forEach(p => projectsSelected.add(p.project_key));', ctx);
  const posted = [];
  ctx.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    posted.push(body);
    return result(body.preview ? { status: 'ok', projects: [
      { project_key: 'p1', retained_files: files }, { project_key: 'p2', retained_files: files },
    ], errors: [{ project_key: 'p3', title: '缺少提示词', message: '缺少全套提示词' }] }
      : { status: 'ok', projects: [{ project_key: 'p1', archive, library: { id: 'lib1' } }],
        errors: [{ project_key: 'p2', title: '失败项目', message: '文件已变更' }],
        deleted_library_ids: ['lib1'], count: 1, deleted_bytes: 4096 });
  };
  ctx.customConfirm = async message => {
    assert.match(message, /缺少提示词：缺少全套提示词/);
    assert.match(message, /跳过 2 个运行中、已归档/);
    return true;
  };
  await ctx.projectsArchive(ctx.rows);
  assert.deepEqual(posted[0].project_keys, ['p1', 'p2', 'p3']);
  assert.deepEqual(posted[1].project_keys, ['p1', 'p2']);
  assert.equal(refreshed, 1);
  assert.equal(ctx.currentIdea, null);
  for (const key of ['spark_current_idea', 'spark_current_idea_id', 'spark_active_task_id',
    'spark_active_task_dimensions', 'spark_prompt_history_lib1']) assert.equal(ctx.storage.has(key), false, key);
  assert.equal(ctx.storage.has('spark_prompt_history_lib2'), true);
  assert.equal(JSON.parse(ctx.storage.get('spark_active_background_tasks')).tasks.length, 1);
  assert.equal(ctx.savedIdeas.find(p => p.id === 'lib1').archived, true);
  assert.equal(ctx.savedIdeas.find(p => p.id === 'lib1').frameRun, undefined);
  assert.equal(ctx.savedIdeas.find(p => p.id === 'lib2').prompt_block, '应保留');
  assert.equal(vm.runInContext('projectsRows[0].archived', ctx), true);
  assert.equal(vm.runInContext('projectsRows[0].task', ctx), null);
  assert.equal(vm.runInContext('projectsRows.length', ctx), 2, 'only archived projects remain in the archive section');
  assert.equal(vm.runInContext('projectsRows.some(p => p.project_key === "p2")', ctx), false);
  assert.equal(vm.runInContext('projectsSelected.has("p1")', ctx), false);
  assert.equal(vm.runInContext('projectsSelected.has("p2")', ctx), false, 'selection is scoped to the visible section');
  assert.equal(ctx.toasts.at(-1)[1], 'error');
  assert.match(ctx.toasts.at(-1)[0], /已归档 1 个项目/);
  assert.match(ctx.toasts.at(-1)[0], /缺少提示词：缺少全套提示词/);
  assert.match(ctx.toasts.at(-1)[0], /失败项目：文件已变更/);
}

async function testRunningAndArchivedProjectsDoNotOfferGenerationOrArchive() {
  const ctx = context();
  const p = { ...normal, archived: true, archive, cover: '/outputs/p1/deleted-cover.webp' };
  const card = ctx.projectsRowInnerHtml(p);
  const actions = ctx.projectsDetailActionsHtml(p);
  assert.match(card, /data-act="view-archive"/);
  assert.doesNotMatch(card, /deleted-cover|data-act="open"|data-act="retry"/);
  assert.doesNotMatch(actions, /data-act="open"|data-act="rerun"|data-act="archive"|delete-project/);
  assert.equal(ctx.projectsCurrentStatus(p).state, 'archived');
  assert.equal(ctx.projectsCanArchive(p), false);
  assert.equal(ctx.projectsCanArchive({ ...normal, sub_jobs: [{ status: 'running' }] }), false);
  assert.doesNotMatch(ctx.projectsDetailActionsHtml({ ...normal, sub_jobs: [{ status: 'running' }] }), /data-act="archive"/);
  assert.equal(ctx.projectsCanArchive(normal), true, 'backend preview decides which finished video or data files to retain');
  ctx.openSparkProject = () => { throw new Error('must not reopen'); };
  ctx.retryTask = ctx.rerunCompletedTask = ctx.openSparkProject;
  for (const act of ['open', 'retry', 'rerun', 'find-parent', 'archive']) await ctx.projectsRunAction(act, p);
}

function testArchiveDetailsExposeFilesAndRejectUnsafeUrls() {
  const ctx = context();
  const pane = { innerHTML: '', dataset: {}, classList: { add() {}, remove() {} },
    querySelectorAll: () => [], querySelector: () => null };
  ctx.document.getElementById = id => id === 'projects-detail' ? pane : null;
  ctx.fixture = { ...normal, archived: true, archive: { ...archive, retained_files: [...files,
    { name: '危险链接', kind: 'prompts', url: 'javascript:alert(1)' },
    { name: '外部链接', kind: 'prompts', url: '//example.com/prompts.md' }] } };
  vm.runInContext('projectsRows = [fixture]; projectsSelectedKey = fixture.project_key;', ctx);
  ctx.renderProjectDetail();
  assert.match(pane.innerHTML, /已归档/);
  assert.match(pane.innerHTML, /<video controls preload="metadata" src="\/outputs\/p1\/refined\.mp4"/);
  assert.match(pane.innerHTML, /download="beats\.json"/);
  assert.match(pane.innerHTML, /download="prompts\.md"/);
  assert.match(pane.innerHTML, /归档时间/);
  assert.doesNotMatch(pane.innerHTML, /javascript:|example\.com|重新生成|打开项目/);
  assert.equal(ctx.projectsArchiveFiles(ctx.fixture).length, 3, 'fallback URLs do not duplicate retained files');
}

async function testArchiveRefreshCannotRestoreOldCoverOrAssets() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.old = { ...normal, cover: '/outputs/p1/old.webp' };
  vm.runInContext('projectsRows = [old]; projectsSection = "archived";', ctx);
  ctx.fetch = async () => result({ projects: [{ project_key: 'p1', archived: true, archive, cover: null, assets: null }] });
  await ctx.refreshProjects({ assets: false });
  assert.equal(vm.runInContext('projectsRows[0].cover', ctx), null);
  assert.equal(vm.runInContext('projectsRows[0].assets', ctx), null);
}

async function testArchivedCoverSurvivesArchiveAndLightweightRefresh() {
  const ctx = context();
  silenceRendering(ctx);
  const cover = '/outputs/p1/archive_cover.jpg';
  const coverArchive = { ...archive, cover_url: cover,
    retained_files: [...files, { url: cover, name: 'archive_cover.jpg', kind: 'cover' }] };
  ctx.fixture = { ...normal, cover: '/outputs/p1/deleted-cover.webp' };
  vm.runInContext('projectsRows = [fixture]; projectsSection = "archived";', ctx);
  ctx.projectsApplyArchiveResults([{ project_key: 'p1', archive: coverArchive }], [ctx.fixture], []);
  const row = vm.runInContext('projectsRows[0]', ctx);
  assert.equal(row.cover, cover);
  assert.equal(row.assets.cover, cover);
  const card = ctx.projectsRowInnerHtml(row);
  assert.match(card, /<img src="\/outputs\/p1\/archive_cover\.jpg"/);
  assert.doesNotMatch(card, /deleted-cover/);
  const detail = ctx.projectsArchiveFilesHtml(row);
  assert.match(detail, /poster="\/outputs\/p1\/archive_cover\.jpg"/);
  assert.match(detail, /封面缩略图/);
  ctx.fetch = async () => result({ projects: [{ ...normal, archived: true, archive: coverArchive, cover, assets: null }] });
  await ctx.refreshProjects({ assets: false });
  assert.equal(vm.runInContext('projectsRows[0].cover', ctx), cover);
  assert.match(ctx.projectsCoverHtml(vm.runInContext('projectsRows[0]', ctx)), /archive_cover\.jpg/);
  ctx.fetch = async () => result({ projects: [{ ...normal, archived: true, archive, cover: null, assets: null }] });
  await ctx.refreshProjects({ assets: false });
  assert.equal(vm.runInContext('projectsRows[0].cover', ctx), null);
  assert.doesNotMatch(ctx.projectsCoverHtml(vm.runInContext('projectsRows[0]', ctx)), /<img/);
}

function testArchivedCoverFallbackUsesOnlyRetainedMedia() {
  const ctx = context();
  const cover = { url: '/outputs/p1/archive_cover.jpg', name: 'archive_cover.jpg', kind: 'cover' };
  const p = { ...normal, archived: true, cover: '/outputs/p1/deleted-cover.webp',
    archive: { ...archive, cover_url: '//external.example/cover.jpg', retained_files: [...files, cover] } };
  assert.match(ctx.projectsCoverHtml(p), /archive_cover\.jpg/);
  assert.doesNotMatch(ctx.projectsCoverHtml(p), /external\.example|deleted-cover/);
  assert.equal(ctx.projectsArchiveCoverUrl({ archive: { cover_url: 'javascript:alert(1)' } }), null);
  const image = { dataset: { placeholder: 'archive' }, outerHTML: '' };
  ctx.projectsHandleCoverError(image);
  assert.match(image.outerHTML, /📦/);
}

async function testServerArchiveStateReconcilesALostExecutionResponse() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.old = normal;
  ctx.savedIdeas = [{ id: 'lib1', project_key: 'p1', frameRun: { videos: ['old.mp4'] } }];
  ctx.currentIdea = ctx.savedIdeas[0];
  ctx.storage.set('spark_current_idea', JSON.stringify(ctx.currentIdea));
  ctx.storage.set('spark_current_idea_id', 'lib1');
  vm.runInContext('projectsRows = [old];', ctx);
  ctx.fetch = async () => result({ projects: [{ ...normal, archived: true, archive }] });
  await ctx.refreshProjects();
  assert.equal(ctx.currentIdea, null);
  assert.equal(ctx.storage.has('spark_current_idea'), false);
  assert.equal(ctx.savedIdeas[0].frameRun, undefined);
  assert.equal(ctx.savedIdeas[0].archived, true);
  assert.equal(vm.runInContext('projectsRows.length', ctx), 0, 'an externally archived project leaves the active section');
}

async function testArchivedProjectNavigationSurvivesAnOlderInflightRefresh() {
  const ctx = context();
  silenceRendering(ctx);
  let finishOld;
  let calls = 0;
  ctx.fetch = async url => {
    calls++;
    if (calls === 1) return new Promise(resolve => { finishOld = () => resolve(result({ projects: [normal] })); });
    assert.match(url, /state=archived/);
    assert.match(url, /scope=archived/);
    return result({ projects: [{ ...normal, archived: true, archive }] });
  };
  ctx.switchMainTab = () => ctx.refreshProjects({ assets: false });
  const oldRefresh = ctx.refreshProjects();
  await ctx.openArchivedProject('p1');
  finishOld();
  await oldRefresh;
  assert.equal(calls, 2);
  assert.equal(vm.runInContext('projectsSelectedKey', ctx), 'p1');
  assert.equal(vm.runInContext('projectsRows[0].archived', ctx), true);
  assert.equal(vm.runInContext('projectsSection', ctx), 'archived');
}

async function testSectionSwitchInvalidatesAnOlderInflightRefresh() {
  const ctx = context();
  silenceRendering(ctx);
  let finishOld;
  const requests = [];
  ctx.fetch = async url => {
    requests.push(url);
    if (requests.length === 1) return new Promise(resolve => { finishOld = () => resolve(result({ projects: [normal] })); });
    assert.match(url, /scope=archived/);
    assert.match(url, /state=archived/);
    return result({ projects: [{ ...normal, archived: true, archive }], counts: { active: 3, archived: 1 }, total_count: 1 });
  };
  const oldRefresh = ctx.refreshProjects();
  await ctx.projectsSetSection('archived');
  finishOld();
  await oldRefresh;
  assert.equal(requests.length, 2);
  assert.equal(vm.runInContext('projectsRows[0].archived', ctx), true);
  assert.equal(vm.runInContext('projectsTotal', ctx), 1);
}

function testArchiveEmptyStateHasNoImportAction() {
  const ctx = context();
  const container = { innerHTML: '' };
  ctx.document.getElementById = id => id === 'projects-list' ? container : null;
  vm.runInContext('projectsSection = "archived"; projectsRows = [];', ctx);
  ctx.renderProjects();
  assert.match(container.innerHTML, /暂无归档项目/);
  assert.doesNotMatch(container.innerHTML, /import-prompts/);
  vm.runInContext('projectsSearch = "未找到";', ctx);
  ctx.renderProjects();
  assert.match(container.innerHTML, /没有匹配的项目/);
}

async function testPendingArchiveRemainsReadOnlyAndCanFinishCleanup() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.refreshProjects = async () => {};
  const pending = { ...normal, archived: true, archive_pending: true, archive };
  assert.equal(ctx.projectsCanArchive(pending), true);
  assert.equal(ctx.projectsCurrentStatus(pending).label, '归档待完成');
  assert.match(ctx.projectsCurrentStatus(pending).next, /保留文件已保存，点击完成归档/);
  const actions = ctx.projectsDetailActionsHtml(pending);
  assert.match(actions, /data-act="archive"[^>]*>📦 完成归档/);
  assert.doesNotMatch(actions, /data-act="open"|data-act="retry"|data-act="rerun"/);
  assert.match(ctx.projectsArchiveFilesHtml(pending), /保留文件已保存，点击完成归档继续清理剩余内容/);
  assert.doesNotMatch(ctx.projectsArchiveFilesHtml(pending), /其余项目文件已永久删除/);
  assert.notEqual(ctx.projectsRowSignature(pending), ctx.projectsRowSignature({ ...pending, archive_pending: false }),
    'lightweight polling updates the pending state without changing archive files');
  ctx.fixture = pending;
  vm.runInContext('projectsRows = [fixture];', ctx);
  const calls = [];
  ctx.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, body });
    return result(body.preview ? { status: 'ok', projects: [{ project_key: 'p1', retained_files: files }], errors: [] }
      : { status: 'ok', projects: [{ project_key: 'p1', archive }], count: 1, deleted_bytes: 4096, errors: [] });
  };
  ctx.customConfirm = async (message, label) => {
    assert.match(message, /完成归档 1 个项目/);
    assert.match(label, /完成归档并永久删除剩余内容/);
    return true;
  };
  await ctx.projectsRunAction('archive', pending);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/projects/archive');
  assert.equal(calls[0].body.preview, true);
  assert.equal(calls[1].body.preview, false);
  assert.equal(vm.runInContext('projectsRows[0].archive_pending', ctx), false);
  assert.equal(ctx.projectsCanArchive(vm.runInContext('projectsRows[0]', ctx)), false);
  assert.equal(ctx.projectsCurrentStatus(vm.runInContext('projectsRows[0]', ctx)).label, '已归档');
}

async function testBulkFinishArchiveTargetsOnlyPendingProjects() {
  const ctx = context();
  const bar = { innerHTML: '', hidden: true };
  ctx.document.getElementById = id => id === 'projects-bulkbar' ? bar : null;
  const pending = { ...normal, project_key: 'pending', archived: true, archive_pending: true, archive };
  const completed = { ...normal, project_key: 'archived', archived: true, archive };
  ctx.rows = [pending, normal, completed];
  vm.runInContext('projectsRows = rows; rows.forEach(p => projectsSelected.add(p.project_key));', ctx);
  ctx.renderProjectsBulkBar();
  assert.match(bar.innerHTML, /data-bulk="finish-archive"[^>]*>📦 完成归档（1）/);
  assert.match(bar.innerHTML, /data-bulk="archive"[^>]*>📦 归档（1）/);
  const targets = [];
  ctx.projectsArchive = async rows => targets.push(rows.map(p => p.project_key));
  await ctx.projectsRunBulkAction('finish-archive');
  await ctx.projectsRunBulkAction('archive');
  assert.deepEqual(JSON.parse(JSON.stringify(targets)), [['pending'], ['p1']]);
}

function testNormalProjectDetailsShowExistingBeatFilesWithoutArchiving() {
  const ctx = context();
  const pane = { innerHTML: '', dataset: {}, classList: { add() {}, remove() {} },
    querySelectorAll: () => [], querySelector: () => null };
  ctx.document.getElementById = id => id === 'projects-detail' ? pane : null;
  ctx.fixture = { ...normal, beat_files: [
    { url: '/outputs/p1/%E5%8F%8D%E6%8E%A8%E8%8A%82%E6%8B%8D.json', name: '反推节拍.json', kind: 'beats' },
    { url: '/outputs/p1/beat_package.json', name: 'beat_package.json', kind: 'beats' },
    { url: 'javascript:alert(1)', name: '不可信资料.json' },
  ] };
  vm.runInContext('projectsRows = [fixture]; projectsSelectedKey = fixture.project_key;', ctx);
  ctx.renderProjectDetail();
  assert.match(pane.innerHTML, /<h4>节拍数据<\/h4>/);
  assert.match(pane.innerHTML, /download="反推节拍\.json"/);
  assert.match(pane.innerHTML, /download="beat_package\.json"/);
  assert.match(pane.innerHTML, /target="_blank" rel="noopener">查看/);
  assert.doesNotMatch(pane.innerHTML, /不可信资料|javascript:|其余项目文件已永久删除|<h4>归档文件<\/h4>/);
  assert.equal(ctx.projectsBeatFilesHtml({}), '');
  assert.equal(ctx.projectsBeatFilesHtml({ beat_files: [] }), '');
}

async function testLightRefreshUpdatesBeatFilesAndClearsExternalRemoval() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.fixture = { ...normal, beat_files: [{ url: '/outputs/p1/beat_package.json', name: 'beat_package.json', kind: 'beats' }] };
  vm.runInContext('projectsRows = [fixture];', ctx);
  ctx.fetch = async () => result({ projects: [{ ...normal, beat_files: [] }] });
  await ctx.refreshProjects({ assets: false });
  assert.equal(vm.runInContext('projectsRows[0].beat_files.length', ctx), 0);
  ctx.fetch = async () => result({ projects: [{ ...normal,
    beat_files: [{ url: '/outputs/p1/beats.json', name: 'beats.json', kind: 'beats' }] }] });
  await ctx.refreshProjects({ assets: false });
  assert.equal(vm.runInContext('projectsRows[0].beat_files[0].name', ctx), 'beats.json');
}

async function testUnrefinedArchiveKeepsTheCurrentMergedVideo() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.refreshProjects = async () => {};
  const merged = { url: '/outputs/p1/merged.mp4', name: 'merged.mp4', kind: 'merged_video' };
  const retained = [merged, files[1], files[2]];
  const mergedArchive = { ...archive, refined_videos: [], final_videos: [merged], retained_files: retained };
  ctx.fixture = normal;
  vm.runInContext('projectsRows = [fixture];', ctx);
  const calls = [];
  ctx.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push(body);
    return result(body.preview ? { status: 'ok', projects: [{ project_key: 'p1',
      video_retention: 'merged', retained_files: retained, archive: mergedArchive }], errors: [] }
      : { status: 'ok', projects: [{ project_key: 'p1', archive: mergedArchive }], errors: [], deleted_bytes: 4096 });
  };
  ctx.customConfirm = async message => {
    assert.match(message, /未完成精剪，将保留当前合成成片/);
    assert.match(message, /merged\.mp4/);
    assert.doesNotMatch(message, /保留精剪后的成片视频/);
    return true;
  };
  await ctx.projectsArchive([normal]);
  assert.equal(calls.length, 2);
  const row = vm.runInContext('projectsRows[0]', ctx);
  assert.equal(row.video_count, 1);
  const html = ctx.projectsArchiveFilesHtml(row);
  assert.match(html, /<video controls preload="metadata" src="\/outputs\/p1\/merged\.mp4"/);
  assert.match(html, /<strong>合成成片<\/strong>/);
  assert.match(html, /download="merged\.mp4"/);
  assert.doesNotMatch(html, /精剪成片/);
  assert.match(ctx.projectsDetailActionsHtml(row), /data-act="gallery"/);
  assert.equal(ctx.projectsArchiveVideos({ archive }).length, 1, 'legacy refined_videos receipts still work');
}

async function testArchiveWithoutVideoKeepsOnlyPromptsAndBeatData() {
  const ctx = context();
  silenceRendering(ctx);
  ctx.refreshProjects = async () => {};
  const retained = [files[1], files[2]];
  const dataArchive = { ...archive, final_videos: [], refined_videos: [], retained_files: retained };
  ctx.fixture = normal;
  vm.runInContext('projectsRows = [fixture];', ctx);
  const calls = [];
  ctx.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push(body);
    return result(body.preview ? { status: 'ok', projects: [{ project_key: 'p1',
      video_retention: 'none', retained_files: retained, archive: dataArchive }], errors: [] }
      : { status: 'ok', projects: [{ project_key: 'p1', archive: dataArchive }], errors: [], deleted_bytes: 4096 });
  };
  ctx.customConfirm = async message => {
    assert.match(message, /没有可保留的成片视频，只保留全套提示词和节拍数据/);
    assert.doesNotMatch(message, /保留精剪后的成片视频|保留当前合成成片/);
    return true;
  };
  await ctx.projectsArchive([normal]);
  assert.equal(calls.length, 2);
  const row = vm.runInContext('projectsRows[0]', ctx);
  assert.equal(row.video_count, 0);
  const html = ctx.projectsArchiveFilesHtml(row);
  assert.match(html, /未保留成片视频，只保留全套提示词和节拍数据/);
  assert.match(html, /download="beats\.json"/);
  assert.match(html, /download="prompts\.md"/);
  assert.doesNotMatch(html, /<video/);
  assert.doesNotMatch(ctx.projectsDetailActionsHtml(row), /data-act="gallery"/);
  assert.doesNotMatch(ctx.projectsRowInnerHtml(row), /data-act="gallery"/);
  assert.equal(ctx.projectsArchiveVideos({ archive: { ...archive, final_videos: [] } }).length, 0,
    'an explicitly empty final_videos list must not resurrect old refined videos');
}

async function testSectionSwitchRecoversFromAnOlderFailedRequest() {
  const ctx = context();
  silenceRendering(ctx);
  const calls = [];
  let rejectOld;
  ctx.fetch = async url => {
    calls.push(url);
    if (calls.length === 1) return new Promise((resolve, reject) => { rejectOld = reject; });
    return result({ projects: [{ ...normal, archived: true, archive }], counts: { active: 0, archived: 1 } });
  };
  const first = ctx.refreshProjects();
  await ctx.projectsSetSection('archived');
  rejectOld(new Error('旧项目请求失败'));
  await first;
  assert.equal(calls.length, 2);
  assert.match(calls[1], /scope=archived/);
  assert.equal(vm.runInContext('projectsRows[0].archived', ctx), true);
  assert.equal(vm.runInContext('projectsLoading', ctx), false);
}

(async () => {
  await testPreviewMustPrecedeConfirmationAndExecution();
  await testCancelAndPreviewFailureNeverDelete();
  await testPartialSuccessKeepsFailedProjectsAndClearsOldMediaState();
  await testRunningAndArchivedProjectsDoNotOfferGenerationOrArchive();
  testArchiveDetailsExposeFilesAndRejectUnsafeUrls();
  await testArchiveRefreshCannotRestoreOldCoverOrAssets();
  await testArchivedCoverSurvivesArchiveAndLightweightRefresh();
  testArchivedCoverFallbackUsesOnlyRetainedMedia();
  await testServerArchiveStateReconcilesALostExecutionResponse();
  await testArchivedProjectNavigationSurvivesAnOlderInflightRefresh();
  await testSectionSwitchInvalidatesAnOlderInflightRefresh();
  await testSectionSwitchRecoversFromAnOlderFailedRequest();
  testArchiveEmptyStateHasNoImportAction();
  await testPendingArchiveRemainsReadOnlyAndCanFinishCleanup();
  await testBulkFinishArchiveTargetsOnlyPendingProjects();
  testNormalProjectDetailsShowExistingBeatFilesWithoutArchiving();
  await testLightRefreshUpdatesBeatFilesAndClearsExternalRemoval();
  await testUnrefinedArchiveKeepsTheCurrentMergedVideo();
  await testArchiveWithoutVideoKeepsOnlyPromptsAndBeatData();
  console.log('projects archive UI regression tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
