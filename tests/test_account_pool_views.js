const assert = require('assert');
const fs = require('fs');
const path = require('path');

const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const accountPoolJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'account_pool.js'), 'utf8');
const trendRefsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'trend_refs.js'), 'utf8');
const utilsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'utils.js'), 'utf8');
const promptImportJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'prompt_import.js'), 'utf8');
const promptLinterJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'prompt_linter.js'), 'utf8');
const collageViewerJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'collage_viewer.js'), 'utf8');
const panelsCss = fs.readFileSync(path.join(__dirname, '..', 'css', 'app', 'panels-tabs.css'), 'utf8');

console.log('Testing Account Pool View Switcher and Grid View...');

// 1. Check HTML structure for view switcher
assert.ok(indexHtml.includes('id="account-pool-view-list-btn"'), 'index.html must have account-pool-view-list-btn');
assert.ok(indexHtml.includes('id="account-pool-view-grid-btn"'), 'index.html must have account-pool-view-grid-btn');
assert.ok(indexHtml.includes('class="account-pool-view-switcher"'), 'index.html must have account-pool-view-switcher');

// 2. Check JS functions in account_pool.js
assert.ok(accountPoolJs.includes('accountPoolViewMode'), 'account_pool.js must track accountPoolViewMode');
assert.ok(accountPoolJs.includes('function setAccountPoolViewMode'), 'account_pool.js must have setAccountPoolViewMode');
assert.ok(accountPoolJs.includes('function renderAccountPoolGridCard'), 'account_pool.js must have renderAccountPoolGridCard');
assert.ok(accountPoolJs.includes('function renderAccountPoolListRow'), 'account_pool.js must have renderAccountPoolListRow');
assert.ok(accountPoolJs.includes('buildCreditBadgeElement'), 'account_pool.js must have credit badge builder');
assert.ok(accountPoolJs.includes('buildTaskCountBadgeElement'), 'account_pool.js must have task count badge builder');

// 3. Check CSS for Grid View and Switcher
assert.ok(panelsCss.includes('.account-pool-view-switcher'), 'panels-tabs.css must style .account-pool-view-switcher');
assert.ok(panelsCss.includes('.account-pool-view-btn'), 'panels-tabs.css must style .account-pool-view-btn');
assert.ok(panelsCss.includes('.account-pool-list.view-grid'), 'panels-tabs.css must style .account-pool-list.view-grid');
assert.ok(panelsCss.includes('.account-pool-card'), 'panels-tabs.css must style .account-pool-card');
assert.ok(panelsCss.includes('.account-pool-pill-badge'), 'panels-tabs.css must style .account-pool-pill-badge');

// 4. Check Modal Backdrop Click Closing
console.log('Testing Modal Backdrop Click Closing...');

// 4.1 settings-modal in app.js
assert.ok(appJs.includes("if (e.target === settingsModal) settingsModal.classList.remove('active')"),
    'app.js must close settingsModal on backdrop click');

// 4.2 global modal backdrop delegation in app.js
assert.ok(appJs.includes("e.target.classList.contains('modal') && e.target.classList.contains('active')"),
    'app.js must have global modal backdrop click delegation');

// 4.3 trend-refs-manage-modal in trend_refs.js
assert.ok(trendRefsJs.includes("if (e.target === modal) closeTrendRefsManageModal()"),
    'trend_refs.js must close modal on backdrop click');

// 4.4 customPrompt and customConfirm in utils.js
assert.ok(utilsJs.includes("if (e.target === modal)"),
    'utils.js must support backdrop click on dialogs');

// 4.5 prompt_import.js
assert.ok(promptImportJs.includes("if (e.target === modal) close()"),
    'prompt_import.js must support backdrop click');

// 4.6 prompt_linter.js
assert.ok(promptLinterJs.includes("if (e.target === modal)"),
    'prompt_linter.js must support backdrop click');

// 4.7 collage_viewer.js
assert.ok(collageViewerJs.includes("if (e.target === modal) closeCollageViewer()"),
    'collage_viewer.js must support backdrop click');

// 5. Check Single Account Zoom / Enlarge Detail Modal (单独点击放大号池情况)
console.log('Testing Account Pool Single Account Zoom Detail Modal...');
assert.ok(accountPoolJs.includes('function openAccountPoolDetailModal'), 'account_pool.js must have openAccountPoolDetailModal');
assert.ok(accountPoolJs.includes('function closeAccountPoolDetailModal'), 'account_pool.js must have closeAccountPoolDetailModal');
assert.ok(accountPoolJs.includes('function renderAccountPoolDetailModal'), 'account_pool.js must have renderAccountPoolDetailModal');
assert.ok(accountPoolJs.includes('account-pool-mini-btn zoom-btn'), 'account_pool.js must render zoom button');
assert.ok(accountPoolJs.includes("if (e.target === modal) closeAccountPoolDetailModal()"),
    'account_pool.js detail modal must close on backdrop click');
assert.ok(panelsCss.includes('.account-pool-detail-modal'), 'panels-tabs.css must style .account-pool-detail-modal');
assert.ok(panelsCss.includes('.apd-modal-content'), 'panels-tabs.css must style .apd-modal-content');

// 6. Check Health Score and Global HUD (方案一：轻量自动化)
console.log('Testing Health Score and Global HUD...');
assert.ok(indexHtml.includes('id="account-pool-global-hud"'), 'index.html must have account-pool-global-hud');
assert.ok(accountPoolJs.includes('function buildHealthScoreBadgeElement'), 'account_pool.js must have health score badge builder');
assert.ok(accountPoolJs.includes('function renderAccountPoolGlobalHUD'), 'account_pool.js must have renderAccountPoolGlobalHUD');
assert.ok(panelsCss.includes('.account-pool-global-hud'), 'panels-tabs.css must style .account-pool-global-hud');
assert.ok(panelsCss.includes('.badge-health-score'), 'panels-tabs.css must style .badge-health-score');

const ap = require('../js/account_pool.js');
assert.strictEqual(typeof ap.openAccountPoolDetailModal, 'function', 'openAccountPoolDetailModal must be exported');
assert.strictEqual(typeof ap.closeAccountPoolDetailModal, 'function', 'closeAccountPoolDetailModal must be exported');
assert.strictEqual(typeof ap.buildHealthScoreBadgeElement, 'function', 'buildHealthScoreBadgeElement must be exported');
assert.strictEqual(typeof ap.renderAccountPoolGlobalHUD, 'function', 'renderAccountPoolGlobalHUD must be exported');

// 7. Check 2FA Not Ready Red Background (2FA 没就绪用红底)
console.log('Testing 2FA Not Ready Red Background...');
assert.ok(accountPoolJs.includes('isNo2FA'), 'account_pool.js must detect isNo2FA status');
assert.ok(accountPoolJs.includes('is-no-2fa'), 'account_pool.js must assign is-no-2fa class');
assert.ok(panelsCss.includes('.account-pool-card-alert.is-no-2fa'), 'panels-tabs.css must style .account-pool-card-alert.is-no-2fa');
assert.ok(panelsCss.includes('.account-pool-login-meta.is-no-2fa'), 'panels-tabs.css must style .account-pool-login-meta.is-no-2fa');

console.log('All Account Pool Views, Single Account Zoom, Health Scores, Global HUD, 2FA Red Background, and Modal Backdrop Closing unit tests passed successfully!');




