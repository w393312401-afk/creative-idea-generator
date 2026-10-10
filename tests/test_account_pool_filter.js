/* 号池概览筛选 / 搜索的单测：
   统计条的每一格都是筛选入口，口径写错的表现是"点了没反应"或者
   "筛掉了本来该看见的账号"——都不会报错，所以这里把口径钉住。 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const pool = require(path.join(__dirname, '..', 'js', 'account_pool.js'));
const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const accountPoolJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'account_pool.js'), 'utf8');
const panelsCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'panels-tabs.css'), 'utf8');
const settingsCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'settings-center.css'), 'utf8');

console.log('Testing Account Pool overview stats / search / filter...');

// 1. HTML 挂点
assert.ok(indexHtml.includes('id="account-pool-stats"'), 'index.html must have account-pool-stats');
assert.ok(indexHtml.includes('id="account-pool-search"'), 'index.html must have account-pool-search');
assert.ok(indexHtml.includes('id="account-pool-search-clear"'), 'index.html must have account-pool-search-clear');
assert.ok(indexHtml.includes('id="account-pool-add-panel"'), 'index.html must collapse add/import row into account-pool-add-panel');

// 2. CSS
assert.ok(panelsCss.includes('.account-pool-stat-tile'), 'panels-tabs.css must style .account-pool-stat-tile');
assert.ok(panelsCss.includes('.account-pool-search-input'), 'panels-tabs.css must style .account-pool-search-input');
assert.ok(panelsCss.includes('.account-pool-add-panel'), 'panels-tabs.css must style .account-pool-add-panel');
assert.ok(settingsCss.includes('.account-pool-add-panel[open]'), 'settings-center.css must cap the expanded add panel height');

// 3. 筛选口径
const { accountPoolMatchesFilter, accountPoolMatchesSearch } = pool;
const healthy = { user_id: 'u1', name: 'a@x.com', credit: 300, min_credit: 15, has_totp: true, health_score: 100, serial_number: 66 };
const disabled = { user_id: 'u2', name: 'b@x.com', disabled: true, credit: 300, has_totp: true };
const lowCredit = { user_id: 'u3', name: 'c@x.com', credit: 2, min_credit: 15, has_totp: true };
const probeFailed = { user_id: 'u4', name: 'd@x.com', credit: 300, has_totp: true, last_probe_status: 'failed' };
const no2fa = { user_id: 'u5', name: 'e@x.com', credit: 300, has_totp: false };

assert.ok(accountPoolMatchesFilter(healthy, 'usable'), 'healthy account is usable');
assert.ok(!accountPoolMatchesFilter(disabled, 'usable'), 'disabled account is not usable');
assert.ok(!accountPoolMatchesFilter(lowCredit, 'usable'), 'below min_credit is not usable');
assert.ok(accountPoolMatchesFilter(disabled, 'disabled'), 'disabled filter catches disabled');
assert.ok(accountPoolMatchesFilter(lowCredit, 'risky'), 'risky filter catches low credit');
assert.ok(accountPoolMatchesFilter(probeFailed, 'risky'), 'risky filter catches probe failure');
assert.ok(!accountPoolMatchesFilter(healthy, 'risky'), 'healthy account is not risky');
assert.ok(accountPoolMatchesFilter(no2fa, 'no2fa'), 'no2fa filter catches missing TOTP');
assert.ok(!accountPoolMatchesFilter(healthy, 'no2fa'), 'account with TOTP is not flagged');
assert.ok(accountPoolMatchesFilter(disabled, 'all'), 'all filter passes everything');

// 4. 搜索：命名 / 备注 / 环境编号 / user_id 都能命中，且大小写无关
assert.ok(accountPoolMatchesSearch(healthy, ''), 'empty keyword matches everything');
assert.ok(accountPoolMatchesSearch(healthy, 'A@X.COM'), 'name match is case-insensitive');
assert.ok(accountPoolMatchesSearch(healthy, '#66'), 'serial number is searchable');
assert.ok(accountPoolMatchesSearch(healthy, 'u1'), 'user_id is searchable');
assert.ok(accountPoolMatchesSearch({ user_id: 'u9', note: '主力号' }, '主力'), 'note is searchable');
assert.ok(!accountPoolMatchesSearch(healthy, 'zzz'), 'non-matching keyword filters out');

// 5. 行/卡片左侧状态色条：必须跟统计条同口径，否则"3 个缺凭据"和红条行数对不上
const { getAccountPoolTone } = pool;
assert.strictEqual(getAccountPoolTone(healthy), 'good', 'healthy account gets the green bar');
assert.strictEqual(getAccountPoolTone(disabled), 'disabled', 'disabled wins over everything else');
assert.strictEqual(getAccountPoolTone(no2fa), 'danger', 'missing 2FA gets the red bar');
assert.strictEqual(getAccountPoolTone(lowCredit), 'warn', 'low credit gets the amber bar');
assert.strictEqual(getAccountPoolTone(probeFailed), 'warn', 'probe failure gets the amber bar');
assert.ok(panelsCss.includes('.account-pool-row.tone-danger'), 'panels-tabs.css must style the row tone bar');
assert.ok(accountPoolJs.includes('tone-${getAccountPoolTone(account)}'), 'rows and cards must carry the tone class');

// 6. 列表行的伸缩口径：主体不参与收缩、按钮组可换行且不许独吞宽度。
//    这三条一旦被后面的同名规则盖掉，窄栏下就会退回"徽章竖排、输入框宽度归零"。
assert.ok(/\.account-pool-main \{[^}]*flex: 1 0 300px/.test(panelsCss),
    '.account-pool-main must not shrink (flex-shrink 0)');
assert.strictEqual((panelsCss.match(/^\.account-pool-main \{/gm) || []).length, 1,
    'only one .account-pool-main rule may exist, otherwise the later one silently wins');
assert.ok(/\.account-pool-row-actions \{[^}]*flex-wrap: wrap/.test(panelsCss),
    '.account-pool-row-actions must be allowed to wrap');
assert.ok(panelsCss.includes('@container pool-list (max-width: 720px)'),
    'panels-tabs.css must stack the row via a container query on the list');
assert.ok(panelsCss.indexOf('@container pool-list') > panelsCss.indexOf('.account-pool-row-actions {'),
    'the container query must come after the base rules it overrides (container queries add no specificity)');

// 7. 「全选」必须跟着可见集合走，否则筛完再全选会误伤看不见的账号
assert.ok(accountPoolJs.includes('const allIds = getVisibleAccountPoolAccounts().map(a => a.user_id);'),
    'selectAllAccountPool must operate on the visible set');

console.log('All Account Pool overview stats / search / filter unit tests passed successfully!');
