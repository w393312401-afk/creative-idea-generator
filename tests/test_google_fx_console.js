const test = require('node:test');
const assert = require('node:assert/strict');
const fx = require('../js/google_fx_console.js');

test('account state prioritizes disabled and active cooldown', () => {
  assert.equal(fx.accountState({ disabled: true, credit: 100 }).key, 'disabled');
  assert.equal(fx.accountState({ cooldown_until: '2099-01-01T00:00:00Z', credit: 100 }, 0).key, 'cooling');
});

test('account state exposes empty credit and ready accounts', () => {
  assert.equal(fx.accountState({ credit: 0 }).key, 'empty');
  assert.equal(fx.accountState({ credit: 20 }).key, 'ready');
});

test('failed and stale credit are never shown as ready', () => {
  assert.equal(fx.accountState({ credit: 777, last_probe_status: 'failed' }).key, 'probe_failed');
  assert.equal(fx.accountState({ credit: 777, credit_stale: true }).key, 'stale');
  assert.equal(fx.creditLabel({ credit: 777, credit_trustworthy: false }), '777（缓存）');
});

test('login-required cooldown is distinguished from a plain cooldown', () => {
  // 后端把"登录失效（2h 冷却、积分不动）"和"额度耗尽（24h 冷却、积分清零）"
  // 用 cooldown_reason 分开了。前端一律显示"冷却中"的话，用户不知道该去人工
  // 重新登录还是等额度恢复。
  const login = fx.accountState({
    cooldown_until: '2099-01-01T00:00:00Z', cooldown_reason: 'login_required', credit: 50
  }, 0);
  assert.equal(login.key, 'login_required');
  assert.equal(login.tone, 'bad');

  const exhausted = fx.accountState({ cooldown_until: '2099-01-01T00:00:00Z', credit: 0 }, 0);
  assert.equal(exhausted.key, 'cooling');
});

test('never-probed credit is its own state, not "ready" and not "empty"', () => {
  // credit=null 表示从来没探测成功过。显示成 0（额度不足）或"可用"都是在编造
  // 账号健康状态——这正是 DEFAULT_CREDIT=1000 那个 bug 的形态。
  assert.equal(fx.accountState({ credit: null }).key, 'unprobed');
  assert.equal(fx.accountState({}).key, 'unprobed');
  assert.equal(fx.creditLabel({ credit: null }), '未探测');
  assert.equal(fx.creditLabel({ credit: 0 }), '0');
  assert.equal(fx.creditLabel({ credit: 1050 }), '1050');
});

test('expired cooldown does not keep an account cooling', () => {
  assert.equal(fx.accountState({ cooldown_until: '2000-01-01T00:00:00Z', credit: 9 }).key, 'ready');
});

test('durations render compactly across magnitudes', () => {
  assert.equal(fx.formatDuration(0), '0s');
  assert.equal(fx.formatDuration(45), '45s');
  assert.equal(fx.formatDuration(185), '3m05s');
  assert.equal(fx.formatDuration(7325), '2h02m');
  assert.equal(fx.formatDuration(null), '—');
  assert.equal(fx.formatDuration(-1), '—');
});

test('task labels cover every FX task family', () => {
  assert.equal(fx.taskTypeLabel('frames'), '帧序列');
  assert.equal(fx.taskTypeLabel('videos'), '视频序列');
  assert.equal(fx.taskTypeLabel('staged_render'), '分步渲染');
  assert.equal(fx.taskTypeLabel('auto'), '自治管线');
});

test('markdown renderer merges soft-wrapped lines into one paragraph', () => {
  // Markdown 段落内的软换行不是段落分隔。逐行各生成一个 <p> 会把一句话拆成
  // 好几段，说明书读起来行距全乱。
  const html = fx.renderMarkdown('一句话被软换行成\n两行文本。');
  assert.equal(html, '<p>一句话被软换行成 两行文本。</p>');
});

test('markdown renderer merges consecutive blockquote lines', () => {
  const html = fx.renderMarkdown('> 第一行\n> 第二行');
  assert.equal(html, '<blockquote>第一行 第二行</blockquote>');
});

test('markdown renderer handles tables, lists, code fences and rules', () => {
  const html = fx.renderMarkdown(
    ['| a | b |', '|---|---|', '| 1 | 2 |', '', '- x', '- y', '', '```', 'raw <b>', '```', '', '---'].join('\n'));
  assert.match(html, /<table class="fx-manual-table">/);
  assert.match(html, /<th>a<\/th><th>b<\/th>/);
  assert.match(html, /<ul><li>x<\/li><li>y<\/li><\/ul>/);
  assert.match(html, /<pre class="fx-manual-code">raw &lt;b&gt;<\/pre>/);
  assert.match(html, /<hr>/);
});

test('markdown renderer escapes HTML and rejects javascript: links', () => {
  // 说明书内容是我们自己写的，但渲染器不该成为注入面：先 escape 再放行标记。
  const html = fx.renderMarkdown('<img src=x onerror=alert(1)>\n\n[bad](javascript:alert(1))');
  assert.ok(!/<img/.test(html), '原始 HTML 标签必须被转义');
  assert.ok(!/<a /.test(html), 'javascript: 链接不该被渲染成 <a>');
});

test('markdown renderer keeps inline code, bold and relative links', () => {
  const html = fx.renderMarkdown('看 `code` 和 **粗体** 以及 [文档](docs/x.md)。');
  assert.match(html, /<code>code<\/code>/);
  assert.match(html, /<strong>粗体<\/strong>/);
  assert.match(html, /<a href="docs\/x\.md" target="_blank" rel="noopener">文档<\/a>/);
});
