const assert = require('assert');
const { classify, eventKeyOf, aggregate, summarize, taskIdsForProject } = require('../js/log_semantics.js');

// 输入用的是 server.log 里真实出现过的行（数字/标题按需缩短），不是编出来的样例。
function entry(level, tag, text, task) {
  return { level, tag, text, task: task || '', raw: text, time: '12:00:00.000' };
}

// ── 用户自己点的取消不是故障 ────────────────────────────────────────
// 实测这是出现次数最多的 ERROR（11 次）。它进了概览就是最典型的无效信息。
{
  const cancelled = [
    entry('ERROR', 'TASK', 'error message=用户取消了生成任务'),
    entry('WARN', 'FRAMES', '帧序列任务已被用户取消 title=废弃导弹发射井改造成地下隐居卧室'),
    entry('INFO', 'CANCEL', '收到取消请求'),
  ];
  cancelled.forEach(e => assert.strictEqual(classify(e).severity, 'ignore', e.text));
  assert.deepStrictEqual(aggregate(cancelled), [], '取消类日志不该产生任何事件卡');
  const s = summarize(cancelled);
  assert.strictEqual(s.error, 0);
  assert.strictEqual(s.warn, 0);
  assert.strictEqual(s.tone, 'ok');
}

// ── 需要人处理 vs 会自愈，必须分到不同 severity ────────────────────
{
  const needsHuman = [
    'error message=Google FX 批量生图失败: MANUAL_REQUIRED:login_required:Google FX 页面初始化需要人工处理 (sign in)',
    '帧序列任务失败: 第 3 帧图生图失败: All accounts exhausted. Last error: HTTP 429',
    '帧序列任务失败: Google FX 批量生图失败: AdsPower 启动失败 (已尝试 3 次)',
    '帧间调色不可用，已跳过（本进程仅提示一次）: No module named \'cv2\'',
    'video_error message=视频 5 的锚点帧 IMAGE 6 派生自旧的 i2i 链（血统过期），已拦截以防跨链跳变',
    'video_error index=2 message=视频 2 所需的起始帧 IMAGE 3 不存在。请重新生成该帧！',
    'error message=IMG 4 当前没有记录待修复的问题——请先在帧网格点「描述问题」写下这一帧哪里不对',
  ];
  needsHuman.forEach(t => {
    const r = classify(entry('ERROR', 'FRAMES', t));
    assert.ok(r, '应命中规则: ' + t);
    assert.strictEqual(r.severity, 'error', t);
    assert.ok(r.title && r.hint, '需要人处理的事件必须有人话标题和说明: ' + r.id);
  });

  const selfHealing = [
    '限流/服务端错误，3.2s 后重试',
    '图生图（/chat/completions）外层尝试 2/5 失败: HTTP Error 429: Too Many Requests',
    'upstream_retry attempt=2 max_attempts=5 error=HTTP 429',
    'Request timed out: TimeoutError(\'timed out\')',
    'video_warning message=视频 3 的锚点帧 IMAGE 4 未通过一致性审查（sequence_review_flagged）',
    'video_warning message=视频 2 的首尾锚点帧差异过大（MAD=31.4），疑似空间断裂',
    '任务 credit_probe_k9 超时后 30s 内未自行退出临界区，看门狗执行强行槽位释放',
  ];
  selfHealing.forEach(t => {
    const r = classify(entry('WARN', 'HTTP', t));
    assert.ok(r, '应命中规则: ' + t);
    assert.strictEqual(r.severity, 'warn', t);
  });
}

// ── 「账号全部用尽」是终态，不能被泛化的「限流」规则先吃掉 ──────────
{
  const r = classify(entry('ERROR', 'FRAMES',
    'All accounts exhausted. Last error: HTTP 429: Too Many Requests'));
  assert.strictEqual(r.id, 'accounts-exhausted',
    '终态规则必须排在泛化的限流规则之前');
}

// ── 重复的同一件事聚成一张卡，不是 N 张 ────────────────────────────
{
  const spam = [];
  for (let i = 1; i <= 12; i++) {
    spam.push(entry('WARN', 'HTTP', `限流/服务端错误，${i}.5s 后重试`, 'frames_aa3f'));
  }
  const events = aggregate(spam);
  assert.strictEqual(events.length, 1, '12 条限流应聚成 1 张卡');
  assert.strictEqual(events[0].count, 12);
  assert.strictEqual(events[0].severity, 'warn');
}

// 已被 api_client 连续折叠过的行（repeatCount>1）计数要累加，不能只算 1
{
  const folded = entry('WARN', 'HTTP', '限流/服务端错误，3.2s 后重试', 't1');
  folded.repeatCount = 7;
  assert.strictEqual(aggregate([folded])[0].count, 7);
}

// 不同任务的同一种问题分开成卡（不然没法知道是哪条片子出的事）
{
  const events = aggregate([
    entry('WARN', 'HTTP', '限流，3s 后重试', 'frames_aaa'),
    entry('WARN', 'HTTP', '限流，3s 后重试', 'frames_bbb'),
  ]);
  assert.strictEqual(events.length, 2);
}

// ── 兜底：不认识的 ERROR 必须出卡，绝不静默 ────────────────────────
{
  const weird = entry('ERROR', 'TASK', '第 7 帧渲染管线抛出了未预期的内部状态 XYZ-42');
  assert.strictEqual(classify(weird), null, '这条本就不该命中任何规则');
  const events = aggregate([weird]);
  assert.strictEqual(events.length, 1, '未识别的 ERROR 必须兜底出卡');
  assert.strictEqual(events[0].recognized, false);
  assert.strictEqual(events[0].severity, 'error');
  assert.ok(events[0].title.length > 0);

  // 同形状的未识别错误仍然合并（数字不同不算两件事）
  const many = aggregate([
    entry('ERROR', 'TASK', '第 3 帧渲染管线抛出了未预期的内部状态 XYZ-42'),
    entry('ERROR', 'TASK', '第 9 帧渲染管线抛出了未预期的内部状态 XYZ-42'),
  ]);
  assert.strictEqual(many.length, 1);
  assert.strictEqual(many[0].count, 2);
}

// ── 未识别的 WARN/INFO/OTHER 不进概览（准入门槛 = 值得人看一眼） ────
{
  const noise = [
    entry('OTHER', '', 'LOG: "GET /outputs/xxx.mp4 HTTP/1.1" 304 -'),
    entry('INFO', 'TASK', 'frame_start slot=3 sequence=3 total=12'),
    entry('WARN', 'TASK', '某个还没写进规则表的提醒'),
  ];
  assert.deepStrictEqual(aggregate(noise), []);
}

// ── 排序：需要处理的排在会自愈的前面 ───────────────────────────────
{
  const events = aggregate([
    entry('WARN', 'HTTP', '限流，3s 后重试'),
    entry('ERROR', 'FRAMES', 'AdsPower 启动失败 (已尝试 3 次)'),
  ]);
  assert.strictEqual(events[0].severity, 'error');
  assert.strictEqual(events[1].severity, 'warn');
}

// ── 页脚统计与状态条那句话 ─────────────────────────────────────────
{
  const s = summarize([
    entry('INFO', 'FRAMES', '帧序列任务完成 title=废弃导弹发射井'),
    entry('INFO', 'FRAMES', '帧序列任务完成 title=退役潜艇舱'),
    entry('WARN', 'HTTP', '限流，3s 后重试'),
  ]);
  assert.strictEqual(s.done, 2);
  assert.strictEqual(s.warn, 1);
  assert.strictEqual(s.error, 0);
  assert.strictEqual(s.tone, 'warn');

  const bad = summarize([entry('ERROR', 'FRAMES', 'AdsPower 启动失败')]);
  assert.strictEqual(bad.tone, 'error');
  assert.match(bad.headline, /需要你处理/);
}

// ── eventKeyOf 必须和 aggregate 算出同一个 key ─────────────────────
// 面板的「查看明细」靠 eventKeyOf 反查"这张卡的最后一行在哪"。两处一旦算得不
// 一样（例如分隔符不同），点了就什么都定位不到，而且是静默失效。
{
  const rows = [
    entry('ERROR', 'FRAMES', 'AdsPower 启动失败 (已尝试 3 次)', 'frames_aaa'),
    entry('WARN', 'HTTP', '限流，3s 后重试', 'frames_bbb'),
    entry('ERROR', 'TASK', '未预期的内部状态 XYZ-42', 'frames_ccc'),
  ];
  const keys = new Set(aggregate(rows).map(e => e.key));
  assert.strictEqual(keys.size, 3);
  rows.forEach(r => {
    const k = eventKeyOf(r);
    assert.ok(keys.has(k), 'eventKeyOf 与 aggregate 的 key 不一致: ' + JSON.stringify(k));
  });
  // 不进概览的行反查必须是 null，不能返回一个永远匹配不上的假 key
  assert.strictEqual(eventKeyOf(entry('ERROR', 'TASK', 'error message=用户取消了生成任务')), null);
  assert.strictEqual(eventKeyOf(entry('INFO', 'TASK', 'frame_start slot=3')), null);
}

// ── 空输入不炸 ─────────────────────────────────────────────────────
assert.deepStrictEqual(aggregate([]), []);
assert.deepStrictEqual(aggregate(null), []);
assert.strictEqual(summarize([]).tone, 'ok');
assert.strictEqual(classify(null), null);
assert.strictEqual(classify({}), null);

console.log('log_semantics tests passed');

// Task lifecycle, rather than raw historical ERROR lines, drives actionable counts.
{
  const fail = index => entry('ERROR', 'TASK', `video_error index=${index} current=${index} total=47 message=warning`, 'v1');
  const done = index => entry('INFO', 'TASK', `video_done index=${index} current=${index} total=47`, 'v1');
  const rows = [fail(44), fail(45)];
  rows[0].unread = rows[1].unread = true;
  let events = aggregate(rows);
  assert.equal(events.length, 2, 'different failed slots must remain distinct');
  assert.equal(events[0].title, '第 44 段视频未生成成功');
  assert(!events[0].hint.includes('没有对应的说明'));
  events = aggregate([...rows, done(44)]);
  assert.equal(events.length, 1);
  assert.equal(events[0].slot, 45);
  assert.equal(events[0].unread, true);
  assert.equal(aggregate([...rows, done(44), fail(44)]).length, 2, 'new failures reopen resolved items');
  const cancelled = { v1: { status: 'cancelled' } };
  assert.equal(aggregate(rows, { tasks: cancelled }).length, 0);
  assert.equal(aggregate(rows, { tasks: { v1: { status: 'completed', outcome: 'completed' } } }).length, 0);
  assert.equal(aggregate(rows, { tasks: { v1: { status: 'completed', outcome: 'partial_failed' } } }).length, 2);
  assert.equal(aggregate(rows, { tasks: { another: { status: 'completed', outcome: 'completed' } } }).length, 2);
}
{
  const rows = [entry('WARN', 'TASK', 'upstream_retry attempt=2 max_attempts=5 error=HTTP 429', 'f1')];
  assert.equal(aggregate(rows)[0].title, '正在自动恢复');
  assert.equal(aggregate([...rows, entry('INFO', 'TASK', 'frame sequence=1', 'f1')]).length, 0);
  assert.equal(aggregate(rows, { tasks: { f1: { status: 'failed' } } })[0].severity, 'error');
  assert.equal(classify(entry('ERROR', 'TASK', 'error message=HTTP 429')).severity, 'error');
  assert.equal(classify(entry('WARN', 'TASK', 'HTTP 403 unusual activity')).id, 'flow-restricted');
  assert.equal(classify(entry('WARN', 'HTTP', 'SSE client disconnected: [WinError 10053]')).severity, 'ignore');
  const manual = entry('ERROR', 'TASK', 'manual_intervention_detected code=login_required', 'f1');
  assert.equal(aggregate([manual, entry('INFO', 'TASK', 'manual_intervention_cleared', 'f1')]).length, 0);
  const warned = entry('WARN', 'TASK', 'video_warning message=未通过一致性审查', 'f1');
  const resolved = aggregate([manual, warned], { tasks: { f1: { status: 'completed', outcome: 'completed_with_warnings' } } });
  assert.equal(resolved.length, 1, 'successful tasks retain real quality warnings only');
  assert.equal(resolved[0].id, 'review-flagged');
}
{
  const idea = { id: 'idea1', project_key: 'project-one', title: '同名项目' };
  const tasks = [
    { id: 'v1', dimensions: { project_key: 'project-one' } },
    { id: 'f1', result: { project_key: 'project-one' } },
    { id: 'other', dimensions: { project_key: 'project-two', theme: '同名项目' } },
  ];
  assert.deepEqual([...taskIdsForProject(tasks, idea, { cover: { taskId: 'cover1' } })], ['cover1', 'v1', 'f1']);
  assert.equal(taskIdsForProject(tasks, null), null);
}
