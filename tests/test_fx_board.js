/* 并发作战板（P0）纯函数 + 挂点单测。泳道位置算错、项目串色、HTML 没转义都不会报错，只会"看着不对"，所以钉住。 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const board = require(path.join(__dirname, '..', 'js', 'fx_board.js'));
const consoleHtml = fs.readFileSync(path.join(__dirname, '..', 'console.html'), 'utf8');
const consoleJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'google_fx_console.js'), 'utf8');
const projectsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'projects.js'), 'utf8');

console.log('Testing FX concurrency board (P0)...');

// 1. 挂点
assert.ok(consoleHtml.includes('id="fx-board"'), 'console.html must have #fx-board');
assert.ok(consoleHtml.includes('js/fx_board.js'), 'console.html must load fx_board.js before google_fx_console.js');
assert.ok(consoleHtml.indexOf('js/fx_board.js') < consoleHtml.indexOf('js/google_fx_console.js'), 'fx_board.js must load first');
assert.ok(consoleHtml.includes('css/console/fx-board.css'), 'console.html must link fx-board.css');
assert.ok(consoleJs.includes('renderBoard(data.board)'), 'status render must feed the board');
assert.ok(projectsJs.includes('projectsFxQueueBadge'), 'projects.js must render the fx queue badge');
assert.ok(projectsJs.includes('p.fx_queue'), 'projects row signature must include fx_queue so the badge refreshes');

// 2. 项目色相：稳定、同 key 同色、空 key 无色
assert.strictEqual(board.hueFor('run_1__校车'), board.hueFor('run_1__校车'));
assert.strictEqual(board.hueFor(''), null);
assert.ok(![40].includes(board.hueFor('run_1__校车')), 'amber (40) is reserved for warnings');

// 3. 泳道：位置、裁剪、历史/在用、空闲账号
const NOW = 10000;
const model = board.buildLanes({
  server_ts: NOW,
  accounts: [
    { user_id: 'a', label: '#70', credit: 900, state: 'ready' },
    { user_id: 'b', label: '#69', credit: 1000, state: 'ready' },
    { user_id: 'c', label: '#66', credit: 5, state: 'low_credit' },
  ],
  history: [
    { task_id: 'old', user_id: 'a', project_key: 'p1', project_label: '校车', stage: 'frames', started_ts: NOW - 3000, ended_ts: NOW - 1200, outcome: 'ok' },
    { task_id: 'edge', user_id: 'a', project_key: 'p1', stage: 'frames', started_ts: NOW - 2000, ended_ts: NOW - 1500, outcome: 'error' },
    { task_id: 'gone', user_id: 'a', started_ts: NOW - 5000, ended_ts: NOW - 4000, outcome: 'ok' },
  ],
  leases: [{ task_id: 'now', user_id: 'a', project_key: 'p1', project_label: '校车', stage: 'videos', started_ts: NOW - 900, account_label: '#70' }],
});
const laneA = model.lanes.find((lane) => lane.key === 'a');
assert.strictEqual(laneA.segments.length, 3, 'segments fully outside the 30m window are dropped');
const clipped = laneA.segments.find((s) => s.task_id === 'old');
assert.strictEqual(clipped.left, 0, 'segment starting before the window is clipped to the left edge');
assert.ok(Math.abs(clipped.width - (600 / 1800) * 100) < 0.01, 'clipped width = visible part only');
const open = laneA.segments.find((s) => s.task_id === 'now');
assert.strictEqual(open.state, 'run');
assert.ok(Math.abs(open.left + open.width - 100) < 0.01, 'the open lease reaches the "now" edge');
assert.strictEqual(laneA.segments.find((s) => s.task_id === 'edge').state, 'error');
assert.ok(model.lanes.some((lane) => lane.key === 'b' && lane.segments.length === 0), 'idle ready accounts get an empty lane');
assert.ok(!model.lanes.some((lane) => lane.key === 'c'), 'unavailable idle accounts are not lanes');
assert.strictEqual(model.lanes[0].key, 'a', 'lanes with recent activity come first');

// 没有账号标识的占用（比如探针）归入"账号未识别"，不丢
const orphanLane = board.buildLanes({ server_ts: NOW, leases: [{ task_id: 'probe', kind: 'credit_probe', started_ts: NOW - 60 }] });
assert.strictEqual(orphanLane.lanes[0].label, '账号未识别');
assert.strictEqual(orphanLane.lanes[0].segments[0].hue, null, 'tasks without a project use the neutral bar');

// 4. 矩阵
const matrix = board.buildMatrix({
  projects: [{ project_key: 'p1', label: '校车', stages: { videos: { state: 'waiting', task_id: 'v1' } } }],
});
assert.strictEqual(matrix[0].cells[0].cls, 'none', 'missing stage renders as a dash');
assert.strictEqual(matrix[0].cells[1].cls, 'wait');
assert.strictEqual(matrix[0].cells[1].task_id, 'v1');

// 5. 等待雷达：说清楚在等谁
const radar = board.buildRadar({
  leases: [{ task_id: 'h', project_label: '荒岛', stage: 'videos', kind: 'videos' }],
  waiting: [{ task_id: 'w', project_label: '校车', stage: 'frames', position: 1, waited_seconds: 125, blocked_by: [{ type: 'capacity', holder_task: 'h' }] }],
});
assert.strictEqual(radar.length, 1);
assert.ok(radar[0].why.includes('荒岛') && radar[0].why.includes('2m05s'), radar[0].why);
assert.strictEqual(radar[0].tag, '等名额');

// 5b. P1：卡死 / 无主 / 中断恢复 / 项目互斥观察
const p1 = {
  server_ts: NOW,
  accounts: [{ user_id: 'a', label: '#70', credit: 9, state: 'ready' }, { user_id: 'b', label: '#69', credit: 9, state: 'ready' }],
  leases: [{ task_id: 'f1', user_id: 'a', project_key: 'p', project_label: '校车', stage: 'frames', state: 'stalled', heartbeat_age_seconds: 400, started_ts: NOW - 900 }],
  waiting: [{ task_id: 'v1', project_label: '校车', project_key: 'p', stage: 'videos', position: 1, waited_seconds: 30,
    blocked_by: [{ type: 'capacity', holder_task: 'f1' }, { type: 'project', holder_task: 'f1', rule: 'R2', enforced: false }] }],
  open_browsers: [{ user_id: 'b', label: '#69', leased: false, warm: false, orphan: true }, { user_id: 'a', label: '#70', leased: true, warm: false, orphan: false }],
  recovered: { task_id: 'videos_dead', user_id: 'a' },
};
const stalledLane = board.buildLanes(p1).lanes.find((lane) => lane.key === 'a');
assert.strictEqual(stalledLane.segments[0].state, 'stall', 'a stalled lease renders as a stall bar, not a normal running bar');
const radar1 = board.buildRadar(p1);
assert.deepStrictEqual(radar1.map((item) => item.cls), ['stalled', 'wait', 'orphan', 'recovered'], 'stalled first, then queue, orphans, recovery');
assert.ok(radar1[0].text.includes('6m40s') && radar1[0].text.includes('#70'), radar1[0].text);
assert.ok(radar1[0].why.includes('不会自动处理'), 'stalled is flag-only and must say so');
assert.ok(radar1[1].why.includes('R2') && radar1[1].why.includes('只提示'), 'R2 is observe-only in P1 and must say so');
assert.strictEqual(radar1[2].actions[0].action, 'close_orphans');
assert.deepStrictEqual(radar1[3].actions, [], 'the recovered notice has no action');
const html1 = board.renderHtml(p1);
assert.ok(html1.includes('疑似卡死 1') && html1.includes('无主浏览器 1'));
assert.ok(html1.includes('● 占用中') && html1.includes('❔ 无主'), 'account chips show the browser state');
assert.ok(html1.includes('data-board-action="close_orphans"'));
assert.ok(consoleJs.includes("boardAction === 'close_orphans'") && consoleJs.includes('/api/google-fx/orphans/close'), 'console must wire the orphan cleanup button');

// 5c. P2：并发下的排队原因要讲清楚（账号 / 出口 / 项目 / 名额），强制释放的僵尸租约不能让人以为名额空了
const p2 = {
  server_ts: NOW,
  capacity: { max: 2, used: 2, per_project_max: 1, egress_policy: 'hard' },
  accounts: [],
  leases: [
    { task_id: 'a', user_id: 'x', project_key: 'p', project_label: '校车', stage: 'videos', state: 'running', started_ts: NOW - 60 },
    { task_id: 'z', user_id: 'y', project_key: 'q', project_label: '荒岛', stage: 'frames', state: 'force_released', started_ts: NOW - 600 },
  ],
  waiting: [
    { task_id: 'w1', project_label: '老屋', stage: 'videos', position: 1, waited_seconds: 5, blocked_by: [{ type: 'account', holder_task: 'a', rule: 'R1' }] },
    { task_id: 'w2', project_label: '秋日', stage: 'videos', position: 2, waited_seconds: 5, blocked_by: [{ type: 'ip', holder_task: 'a', rule: 'R5' }] },
    { task_id: 'w3', project_label: '校车', stage: 'frames', position: 3, waited_seconds: 5, blocked_by: [{ type: 'project', holder_task: 'a', rule: 'R2', enforced: true }] },
    { task_id: 'w4', project_label: '海岛', stage: 'videos', position: 4, waited_seconds: 5, blocked_by: [{ type: 'capacity', holder_task: 'a' }] },
  ],
};
const radar2 = board.buildRadar(p2);
const byTag = Object.fromEntries(radar2.map((item) => [item.tag, item]));
assert.ok(byTag['账号被占'].why.includes('R1') && byTag['账号被占'].why.includes('校车'), byTag['账号被占'].why);
assert.ok(byTag['同出口'].why.includes('R5'), 'ip blockers must cite R5');
assert.ok(byTag['项目互斥'].why.includes('同项目') && !byTag['项目互斥'].why.includes('只提示'), 'enforced project mutex is not "observe only"');
assert.ok(byTag['等名额'].why.includes('2/2'), 'capacity blockers show used/max');
assert.ok(byTag['已强制释放'].why.includes('线程真正退出'), 'a force-released lease keeps its slot and must say so');
const html2 = board.renderHtml(p2);
assert.ok(html2.includes('并发模式：最多 2 个任务'), 'the header must announce concurrent mode');
assert.ok(board.renderHtml({ capacity: { max: 1, used: 0 } }).includes('单浏览器模式'));
const zombieLane = board.buildLanes(p2).lanes.find((lane) => lane.key === 'y');
assert.strictEqual(zombieLane.segments[0].state, 'cancelled', 'force-released bars must not look like normal running bars');
assert.strictEqual(board.buildLanes(p2).lanes.filter((lane) => lane.segments.some((seg) => seg.open)).length, 2, 'concurrent leases land on separate lanes');

// 6. 容量与渲染
assert.deepStrictEqual(board.buildCapacity({ capacity: { max: 1, used: 1 } }), { max: 1, used: 1, full: true });
assert.strictEqual(board.formatDuration(59), '59s');
assert.strictEqual(board.formatDuration(3700), '1h01m');

const html = board.renderHtml({
  server_ts: NOW,
  capacity: { max: 1, used: 0 },
  leases: [{ task_id: 'x"><img src=x onerror=alert(1)>', user_id: 'a', project_label: '<b>bad</b>', stage: 'videos', started_ts: NOW - 10 }],
  accounts: [{ user_id: 'a', label: '#70', credit: 1, state: 'ready', name: '<script>' }],
});
assert.ok(!html.includes('<img src=x'), 'task ids must be escaped');
assert.ok(!html.includes('<b>bad</b>'), 'project labels must be escaped');
assert.ok(!html.includes('<script>'), 'account names must be escaped');

// 空数据不抛错
assert.doesNotThrow(() => board.renderHtml(null));
assert.doesNotThrow(() => board.renderHtml({}));

console.log('FX board tests passed.');
