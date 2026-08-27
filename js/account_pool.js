/* ============================================================
   生成号池管理
   —— 用户名下几个 Flow 账号的命名/备注/已知积分/启用状态；发起帧序列 / 视频
   生成前自动挑一个还有额度的账号使用（后端逻辑见 server_common.py 的
   _select_pool_account，帧序列与视频序列共用同一个池子）。命名（name）默认用 AdsPower 环境的邮箱地址，
   备注（note）是完全独立的可选字段——两者互不影响。规模小，UI 就是一个
   简单列表 + 添加行，不需要 trend-refs 管理弹窗那套搜索/排序/批量选择
   基础设施。
   ============================================================ */

let accountPoolCache = [];
let accountPoolProfileCache = []; // list_adspower_profiles() 结果，供选环境时把邮箱预填进"账号命名"
let accountPoolSortKey = 'serial_asc';
let accountPoolSelected = new Set(); // 多选已勾选的 user_id 集合
let accountPoolPriorityUserIds = new Set(); // 优先级浏览器实例集合
let accountPoolStrategy = 'credit_desc'; // 选号调度策略: credit_desc | expiration_asc | rotation
let accountPoolLastClickedIndex = -1; // 供 Shift 范围连选
let accountPoolViewMode = 'list'; // 视图模式: 'list' | 'grid'
let accountPoolFilter = 'all';    // 概览统计条选中的筛选: all|usable|disabled|risky|no2fa|priority
let accountPoolSearch = '';       // 搜索关键词（命名/备注/环境编号/user_id）
try {
    const savedMode = localStorage.getItem('spark_account_pool_view');
    if (savedMode === 'grid' || savedMode === 'list') accountPoolViewMode = savedMode;
} catch (e) {}
// 当前展开着凭据表单的 user_id（同时只允许一个）。存在这里而不是 DOM 里，是因为
// renderAccountPoolList() 会把整个列表重建一遍（保存凭据后必然触发一次），
// 状态放 DOM 上会在重建时丢失，表现为"一点保存表单就自己收起来了"。
let accountPoolOpenCredentialId = null;

async function loadAccountPoolStrategyConfig() {
    try {
        const resp = await fetch('/api/google-fx/config');
        const data = await resp.json();
        if (data && data.config) {
            const pri = data.config.googleFxPriorityUserIds;
            if (Array.isArray(pri)) {
                accountPoolPriorityUserIds = new Set(pri.map(String));
            } else if (typeof pri === 'string' && pri.trim()) {
                accountPoolPriorityUserIds = new Set(pri.split(',').map(s => s.trim()).filter(Boolean));
            }
            if (data.config.googleFxAccountStrategy) {
                accountPoolStrategy = String(data.config.googleFxAccountStrategy);
            }
        }
        const stratSelect = document.getElementById('account-pool-strategy-select');
        if (stratSelect) {
            stratSelect.value = accountPoolStrategy;
        }
    } catch (e) {
        console.error('Failed to load strategy config:', e);
    }
}

async function saveAccountPoolStrategyConfig() {
    // ⚠️ /api/google-fx/config 的 POST body 必须是 {patch: {...}}（server.py 读 body['patch']，
    // 再交给 fx_console.validate_patch）。直接把字段摊在顶层会被判成"patch 必须是非空对象"
    // 而以 400 拒绝——而这里原先既不带 patch 也不看 response.ok，于是优先级星标和选号
    // 策略在界面上"看起来保存成功"，实际上一个都没落盘。
    try {
        const resp = await fetch('/api/google-fx/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                patch: {
                    googleFxPriorityUserIds: Array.from(accountPoolPriorityUserIds),
                    googleFxAccountStrategy: accountPoolStrategy
                }
            })
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data || data.status !== 'ok') {
            const msg = (data && data.message) || `HTTP ${resp.status}`;
            console.error('Failed to save strategy config:', msg);
            if (typeof showToast === 'function') showToast(`选号策略保存失败：${msg}`, 'error');
        }
    } catch (e) {
        console.error('Failed to save strategy config:', e);
        if (typeof showToast === 'function') showToast(`选号策略保存失败：${e.message}`, 'error');
    }
}

async function toggleAccountPoolPriority(userId) {
    if (accountPoolPriorityUserIds.has(userId)) {
        accountPoolPriorityUserIds.delete(userId);
    } else {
        accountPoolPriorityUserIds.add(userId);
    }
    await saveAccountPoolStrategyConfig();
    renderAccountPoolList();
}

function formatAccountPoolTimestamp(iso) {
    if (!iso) return '从未检查';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '从未检查';
        return d.toLocaleString('zh-CN', { hour12: false });
    } catch (e) {
        return '从未检查';
    }
}

function isAccountPoolCoolingDown(account) {
    if (!account.cooldown_until) return false;
    const until = new Date(account.cooldown_until);
    return !isNaN(until.getTime()) && until.getTime() > Date.now();
}

function sortAccountPoolList(accounts, sortKey) {
    if (!Array.isArray(accounts)) return [];
    const list = [...accounts];
    list.sort((a, b) => {
        const imgA = a.image_task_count || 0;
        const imgB = b.image_task_count || 0;
        const vidA = a.video_task_count || 0;
        const vidB = b.video_task_count || 0;
        const totalA = a.task_count != null ? a.task_count : (imgA + vidA);
        const totalB = b.task_count != null ? b.task_count : (imgB + vidB);

        switch (sortKey) {
            case 'expires_asc': {
                const dA = a.expires_at ? new Date(a.expires_at).getTime() : Infinity;
                const dB = b.expires_at ? new Date(b.expires_at).getTime() : Infinity;
                if (dA !== dB) return dA - dB;
                return (b.credit || 0) - (a.credit || 0);
            }
            case 'credit_desc':
                if (a.credit == null && b.credit == null) return 0;
                if (a.credit == null) return 1;
                if (b.credit == null) return -1;
                return b.credit - a.credit;
            case 'credit_asc':
                if (a.credit == null && b.credit == null) return 0;
                if (a.credit == null) return 1;
                if (b.credit == null) return -1;
                return a.credit - b.credit;
            case 'tasks_desc':
                return totalB - totalA;
            case 'image_desc':
                return imgB - imgA;
            case 'video_desc':
                return vidB - vidA;
            case 'name_asc':
                return (a.name || a.user_id || '').localeCompare(b.name || b.user_id || '');
            case 'serial_asc':
            default: {
                const snA = parseInt(a.serial_number, 10);
                const snB = parseInt(b.serial_number, 10);
                if (!isNaN(snA) && !isNaN(snB)) return snA - snB;
                if (!isNaN(snA)) return -1;
                if (!isNaN(snB)) return 1;
                return (a.name || a.user_id || '').localeCompare(b.name || b.user_id || '');
            }
        }
    });
    return list;
}

// ── 概览筛选与搜索 ────────────────────────────────────────────────
// 号池长到几十个之后，真正常做的操作是"把出问题的那几个揪出来"，而不是
// 从头翻到尾。所以统计条里的每一格都直接是一次筛选，配合搜索框收敛可见集合；
// 「全选」和批量操作也一律只作用于当前可见集合——否则筛完再全选会误伤看不见的账号。
function isAccountPoolUsable(account) {
    if (account.disabled) return false;
    if (isAccountPoolCoolingDown(account)) return false;
    const minCredit = account.min_credit != null ? account.min_credit : 15;
    if (account.credit != null && account.credit < minCredit) return false;
    return true;
}

function accountPoolMatchesFilter(account, filter) {
    switch (filter) {
        case 'usable':
            return isAccountPoolUsable(account);
        case 'disabled':
            return !!account.disabled;
        case 'risky': {
            // 需要人管一下的：冷却中 / 积分不足 / 探测失败 / 健康分偏低
            if (isAccountPoolCoolingDown(account)) return true;
            if (account.last_probe_status === 'failed') return true;
            const minCredit = account.min_credit != null ? account.min_credit : 15;
            if (account.credit != null && account.credit < minCredit) return true;
            if (account.health_score != null && account.health_score < 65) return true;
            return false;
        }
        case 'no2fa':
            return !account.has_totp || !!account.auto_login_blocked;
        case 'priority':
            return accountPoolPriorityUserIds.has(account.user_id);
        case 'all':
        default:
            return true;
    }
}

// 行/卡片左侧那条状态色条的颜色。刻意复用 accountPoolMatchesFilter 的口径：
// 统计条说"3 个缺凭据"，列表里就该正好有 3 行是红条，两处判定分家迟早对不上。
function getAccountPoolTone(account) {
    if (account.disabled) return 'disabled';
    if (accountPoolMatchesFilter(account, 'no2fa')) return 'danger';
    if (accountPoolMatchesFilter(account, 'risky')) return 'warn';
    return 'good';
}

function accountPoolMatchesSearch(account, keyword) {
    if (!keyword) return true;
    const kw = keyword.trim().toLowerCase();
    if (!kw) return true;
    const haystack = [
        account.name,
        account.note,
        account.user_id,
        account.serial_number != null ? `#${account.serial_number}` : '',
        account.expires_at
    ].filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(kw);
}

// 当前可见（筛选 + 搜索 + 排序之后）的账号，渲染与批量操作共用同一份口径
function getVisibleAccountPoolAccounts() {
    const filtered = accountPoolCache.filter(a =>
        accountPoolMatchesFilter(a, accountPoolFilter) && accountPoolMatchesSearch(a, accountPoolSearch));
    return sortAccountPoolList(filtered, accountPoolSortKey);
}

function computeAccountPoolStats() {
    const stats = { all: 0, usable: 0, disabled: 0, risky: 0, no2fa: 0, priority: 0, credits: 0, hasCredits: false };
    accountPoolCache.forEach(a => {
        stats.all += 1;
        if (accountPoolMatchesFilter(a, 'usable')) stats.usable += 1;
        if (accountPoolMatchesFilter(a, 'disabled')) stats.disabled += 1;
        if (accountPoolMatchesFilter(a, 'risky')) stats.risky += 1;
        if (accountPoolMatchesFilter(a, 'no2fa')) stats.no2fa += 1;
        if (accountPoolMatchesFilter(a, 'priority')) stats.priority += 1;
        if (a.credit != null && a.credit > 0) {
            stats.credits += Number(a.credit);
            stats.hasCredits = true;
        }
    });
    return stats;
}

function setAccountPoolFilter(filter) {
    accountPoolFilter = filter || 'all';
    renderAccountPoolList();
}

function renderAccountPoolStats() {
    const wrap = document.getElementById('account-pool-stats');
    if (!wrap) return;
    wrap.textContent = '';
    if (!accountPoolCache.length) {
        wrap.style.display = 'none';
        return;
    }
    wrap.style.display = '';

    const stats = computeAccountPoolStats();
    const tiles = [
        { key: 'all', icon: '📦', label: '全部账号', value: stats.all, tone: 'neutral', title: '池子里的全部账号' },
        { key: 'usable', icon: '🟢', label: '可用', value: stats.usable, tone: 'good', title: '已启用、未冷却、积分够用——现在就能被调度到' },
        { key: 'risky', icon: '⚠️', label: '需处理', value: stats.risky, tone: 'warn', title: '冷却中 / 积分不足 / 探测失败 / 健康分低于 65' },
        { key: 'no2fa', icon: '🔑', label: '缺凭据', value: stats.no2fa, tone: 'danger', title: '没配 2FA 密钥或自动登录已被熔断，掉登录后无法自己登回来' },
        { key: 'disabled', icon: '🚫', label: '已禁用', value: stats.disabled, tone: 'muted', title: '手动禁用或被自动停用的账号' },
        { key: 'priority', icon: '⭐', label: '优先级', value: stats.priority, tone: 'primary', title: '标了星的优先级浏览器实例' }
    ];

    tiles.forEach(tile => {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = `account-pool-stat-tile tone-${tile.tone}`
            + (accountPoolFilter === tile.key ? ' active' : '')
            + (tile.value === 0 && tile.key !== 'all' ? ' is-zero' : '');
        el.title = `${tile.title}\n点击只看这一类（再点一次取消筛选）`;
        el.innerHTML = `<span class="stat-icon">${tile.icon}</span>`
            + `<span class="stat-body"><span class="stat-value">${tile.value}</span>`
            + `<span class="stat-label">${tile.label}</span></span>`;
        el.addEventListener('click', () => {
            setAccountPoolFilter(accountPoolFilter === tile.key ? 'all' : tile.key);
        });
        wrap.appendChild(el);
    });

    // 总积分是纯仪表，不做筛选入口
    const creditTile = document.createElement('div');
    creditTile.className = 'account-pool-stat-tile tone-credit is-static';
    creditTile.title = '池子里已探测到的积分总和（未探测的账号不计入）';
    creditTile.innerHTML = `<span class="stat-icon">💎</span>`
        + `<span class="stat-body"><span class="stat-value">${stats.hasCredits ? stats.credits.toLocaleString() : '—'}</span>`
        + `<span class="stat-label">总积分</span></span>`;
    wrap.appendChild(creditTile);
}

function updateAccountPoolBulkBar() {
    const count = accountPoolSelected.size;
    const countEl = document.getElementById('account-pool-bulk-count');
    if (countEl) countEl.textContent = count > 0 ? `已选 ${count} 个账号` : '';

    const bar = document.getElementById('account-pool-bulk-actions-bar');
    if (bar) bar.hidden = (count === 0);

    const titleEl = document.getElementById('account-pool-bulk-title');
    if (titleEl) titleEl.textContent = `已选 ${count} 个`;

    const selAllBtn = document.getElementById('account-pool-select-all-btn');
    if (selAllBtn) {
        const allIds = getVisibleAccountPoolAccounts().map(a => a.user_id);
        const allSelected = allIds.length > 0 && allIds.every(id => accountPoolSelected.has(id));
        selAllBtn.textContent = allSelected ? '⬜ 取消全选' : '☑️ 全选';
        selAllBtn.classList.toggle('active', allSelected);
        selAllBtn.disabled = allIds.length === 0;
    }
}

async function loadAccountPool() {
    try {
        await loadAccountPoolStrategyConfig();
        const resp = await fetch('/api/account-pool');
        const data = await resp.json();
        if (data && data.status === 'ok' && Array.isArray(data.accounts)) {
            accountPoolCache = data.accounts;
        } else {
            accountPoolCache = [];
        }
    } catch (e) {
        console.error('Failed to load account pool:', e);
        accountPoolCache = [];
    }
    renderAccountPoolList();
}

async function loadAccountPoolAdspowerProfiles() {
    const select = document.getElementById('account-pool-add-select');
    if (!select) return;
    select.innerHTML = '<option value="">-- 从 AdsPower 环境里选一个 --</option>';
    accountPoolProfileCache = [];
    try {
        const resp = await fetch('/api/account-pool/adspower-profiles');
        const data = await resp.json();
        if (data && data.status === 'ok' && Array.isArray(data.profiles)) {
            accountPoolProfileCache = data.profiles;
            const known = new Set(accountPoolCache.map(a => a.user_id));
            data.profiles.forEach(p => {
                if (known.has(p.user_id)) return; // 已在池子里的不重复列出
                const opt = document.createElement('option');
                opt.value = p.user_id;
                const serialPrefix = p.serial_number ? `[#${p.serial_number}] ` : '';
                opt.textContent = `${serialPrefix}${p.name || '(无邮箱信息)'} — ${p.user_id}`;
                select.appendChild(opt);
            });
        }
    } catch (e) {
        console.error('Failed to load AdsPower profiles:', e);
    }
}

// 选中 AdsPower 环境后，若命名输入框还是空的，自动把该环境的邮箱地址带出来
// 作为账号命名的默认值（用户仍可编辑覆盖；备注是完全独立的可选字段）
function prefillAccountPoolNameFromSelection() {
    const select = document.getElementById('account-pool-add-select');
    const nameInput = document.getElementById('account-pool-add-name');
    if (!select || !nameInput || nameInput.value.trim()) return;
    const profile = accountPoolProfileCache.find(p => p.user_id === select.value);
    if (profile && profile.name) nameInput.value = profile.name;
}

function setAccountPoolViewMode(mode) {
    accountPoolViewMode = mode === 'grid' ? 'grid' : 'list';
    try {
        localStorage.setItem('spark_account_pool_view', accountPoolViewMode);
    } catch (e) {}
    updateAccountPoolViewSwitcherUI();
    renderAccountPoolList();
}

function updateAccountPoolViewSwitcherUI() {
    const listBtn = document.getElementById('account-pool-view-list-btn');
    const gridBtn = document.getElementById('account-pool-view-grid-btn');
    if (listBtn) listBtn.classList.toggle('active', accountPoolViewMode === 'list');
    if (gridBtn) gridBtn.classList.toggle('active', accountPoolViewMode === 'grid');
    const list = document.getElementById('account-pool-list');
    if (list) {
        list.classList.toggle('view-grid', accountPoolViewMode === 'grid');
        list.classList.toggle('view-list', accountPoolViewMode === 'list');
    }
}

function updateAccountPoolSearchUI() {
    const input = document.getElementById('account-pool-search');
    const clearBtn = document.getElementById('account-pool-search-clear');
    if (input && input.value !== accountPoolSearch) input.value = accountPoolSearch;
    if (clearBtn) clearBtn.hidden = !accountPoolSearch;
    const wrap = input ? input.closest('.account-pool-search-wrap') : null;
    if (wrap) wrap.classList.toggle('has-value', !!accountPoolSearch);
}

async function editAccountPoolExpiresDate(account) {
    const newDate = prompt(`设置账号 ${account.name || account.user_id} 的重置日期 (格式: YYYY-MM-DD，留空清空):`, account.expires_at || '');
    if (newDate === null) return;
    try {
        const resp = await fetch('/api/account-pool/expires-at', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: account.user_id, expires_at: newDate.trim() || null })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            showToast('已更新重置日期', 'success');
            await loadAccountPool();
        } else {
            showToast(`更新失败: ${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast(`更新失败: ${e.message}`, 'error');
    }
}

function buildCreditBadgeElement(account) {
    const badge = document.createElement('span');
    badge.className = 'account-pool-pill-badge';

    if (isAccountPoolCoolingDown(account)) {
        badge.classList.add('badge-cooldown');
        badge.textContent = account.cooldown_reason === 'login_required' ? '🔒 登录失效冷却中' : '🧊 额度冷却中';
        badge.title = `冷却至: ${formatAccountPoolTimestamp(account.cooldown_until)}`;
    } else if (account.disabled_reason === 'zero_credit' || (account.credit != null && account.credit < (account.min_credit ?? 15))) {
        badge.classList.add('badge-credit-low');
        badge.textContent = `🔴 积分 ${account.credit != null ? account.credit : 0} (不足)`;
        badge.title = '积分低于阈值，已自动停用调度';
    } else if (account.credit == null) {
        badge.classList.add('badge-credit-none');
        badge.textContent = '⚪ 积分未探测';
        badge.title = '尚未探测过积分，点击「刷新积分」可探测真实余额';
    } else if (account.credit <= 100) {
        badge.classList.add('badge-credit-medium');
        badge.textContent = `🟡 积分 ${account.credit}${account.credit_trustworthy === false ? ' (缓存)' : ''}`;
        badge.title = '积分余额偏低';
    } else {
        badge.classList.add('badge-credit-high');
        badge.textContent = `🟢 积分 ${account.credit}${account.credit_trustworthy === false ? ' (缓存)' : ''}`;
        badge.title = '积分余额充足';
    }
    return badge;
}

function buildTaskCountBadgeElement(account) {
    const imgCount = account.image_task_count || 0;
    const vidCount = account.video_task_count || 0;
    const totalCount = account.task_count != null ? account.task_count : (imgCount + vidCount);

    const badge = document.createElement('span');
    badge.className = 'account-pool-pill-badge badge-tasks';
    badge.textContent = `📊 任务 ${totalCount} (🖼️ ${imgCount} / 🎬 ${vidCount})`;
    badge.title = `总任务数: ${totalCount} (图片 ${imgCount} / 视频 ${vidCount})`;
    return badge;
}

function buildHealthScoreBadgeElement(account) {
    const score = account.health_score != null ? account.health_score : 100;
    const status = account.health_status || (score >= 85 ? 'excellent' : score >= 65 ? 'good' : score >= 40 ? 'warning' : 'poor');

    const badge = document.createElement('span');
    badge.className = `account-pool-pill-badge badge-health-score health-${status}`;

    let label = '极佳';
    if (status === 'poor') {
        label = '隐患';
    } else if (status === 'warning') {
        label = '注意';
    } else if (status === 'good') {
        label = '良好';
    }

    badge.textContent = `❤️ ${score}分 (${label})`;
    badge.title = `动态健康分: ${score} / 100\n基于可用额度、成功率、登录就绪度与冷却状态自动计算，智能调度优先挑选高分账号`;
    return badge;
}

function renderAccountPoolGlobalHUD() {
    const hud = document.getElementById('account-pool-global-hud');
    if (!hud) return;

    if (!accountPoolCache || !accountPoolCache.length) {
        hud.style.display = 'none';
        return;
    }

    hud.style.display = 'inline-flex';
    const total = accountPoolCache.length;
    const usable = accountPoolCache.filter(a => !a.disabled && !a.cooldown_until && (a.credit == null || a.credit >= (a.min_credit || 15))).length;

    let totalCredits = 0;
    let hasCredits = false;
    accountPoolCache.forEach(a => {
        if (a.credit != null && a.credit > 0) {
            totalCredits += Number(a.credit);
            hasCredits = true;
        }
    });

    const isAllExhausted = usable === 0;
    hud.className = `account-pool-global-hud ${isAllExhausted ? 'hud-danger' : usable < total / 2 ? 'hud-warning' : 'hud-healthy'}`;

    hud.innerHTML = `
        <span class="hud-status-dot"></span>
        <span class="hud-text">
            号池就绪: <strong>${usable}/${total}</strong>
            ${hasCredits ? `· 💎 <strong>${totalCredits.toLocaleString()}</strong> 积分` : ''}
            · ⚡ 自动漫游调度
        </span>
    `;
    hud.title = `点击打开号池配置中心\n总账号数: ${total}\n可用账号: ${usable}\n总积分: ${hasCredits ? totalCredits : '未全部探测'}\n异常自动漫游: 已激活 (Auto-Failover)`;
    hud.onclick = () => {
        if (typeof openAccountPoolManageModal === 'function') {
            openAccountPoolManageModal();
        }
    };
}

function createAccountPoolCheckbox(account, idx, sortedAccounts, itemContainer) {
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.className = 'account-pool-bulk-check';
    check.checked = accountPoolSelected.has(account.user_id);
    check.title = '勾选以进行批量操作 (支持 Shift 连选)';
    check.addEventListener('click', (e) => {
        if (e.shiftKey && accountPoolLastClickedIndex >= 0 && accountPoolLastClickedIndex !== idx) {
            const start = Math.min(accountPoolLastClickedIndex, idx);
            const end = Math.max(accountPoolLastClickedIndex, idx);
            const shouldCheck = check.checked;
            for (let i = start; i <= end; i++) {
                const targetAcc = sortedAccounts[i];
                if (!targetAcc) continue;
                if (shouldCheck) accountPoolSelected.add(targetAcc.user_id);
                else accountPoolSelected.delete(targetAcc.user_id);
            }
            renderAccountPoolList();
            return;
        }
        accountPoolLastClickedIndex = idx;
    });
    check.addEventListener('change', () => {
        if (check.checked) {
            accountPoolSelected.add(account.user_id);
        } else {
            accountPoolSelected.delete(account.user_id);
        }
        itemContainer.classList.toggle('selected', check.checked);
        updateAccountPoolBulkBar();
    });
    return check;
}

function createAccountPoolPriorityStar(account) {
    const isPriority = accountPoolPriorityUserIds.has(account.user_id);
    const star = document.createElement('span');
    star.className = 'account-pool-priority-star ' + (isPriority ? 'is-priority' : 'not-priority');
    star.textContent = isPriority ? '⭐' : '☆';
    star.title = isPriority
        ? '当前为优先级浏览器实例（生成时优先调度），点击取消优先'
        : '点击设为优先级浏览器实例（生成时优先调度）';
    star.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleAccountPoolPriority(account.user_id);
    });
    return star;
}

function createAccountPoolStatusBadge(account) {
    const statusBadge = document.createElement('span');
    statusBadge.className = 'account-pool-status-badge ' + (account.disabled ? 'is-disabled' : 'is-enabled');
    statusBadge.textContent = account.disabled ? '🚫 已禁用' : '✅ 已启用';
    statusBadge.title = account.disabled ? '当前已禁用，点击启用' : '当前已启用，点击禁用';
    statusBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleAccountPoolAccount(account.user_id, !account.disabled);
    });
    return statusBadge;
}

function createAccountPoolActionButtons(account) {
    const zoomBtn = document.createElement('button');
    zoomBtn.type = 'button';
    zoomBtn.className = 'account-pool-mini-btn zoom-btn';
    zoomBtn.textContent = '🔍 放大';
    zoomBtn.title = '单独放大查看并编辑该账号的全部详细信息与凭据';
    zoomBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openAccountPoolDetailModal(account.user_id);
    });

    // 按钮文案固定："📅 2026-09-30" 比 "📅 重置日期" 宽出一截，会让每一行的
    // 按钮组宽度都不一样（右边缘参差、个别行还会折行多占一层）。重置日期在
    // 元信息里已经有一枚 ⏳ 芯片了，按钮这里只用 is-on 的高亮表示"已设"。
    const expiresBtn = document.createElement('button');
    expiresBtn.type = 'button';
    expiresBtn.className = 'account-pool-mini-btn' + (account.expires_at ? ' is-on' : '');
    expiresBtn.textContent = '📅 重置日期';
    expiresBtn.title = account.expires_at
        ? `当前重置日期: ${account.expires_at}（点击修改，留空可清除）`
        : '设置该账号额度重置日期（选填，用于「重置日期最早优先」选号）';
    expiresBtn.addEventListener('click', () => editAccountPoolExpiresDate(account));

    const credBtn = document.createElement('button');
    credBtn.type = 'button';
    credBtn.className = 'account-pool-mini-btn'
        + (account.auto_login_ready ? ' is-on' : '')
        + (account.auto_login_blocked ? ' danger' : '');
    credBtn.textContent = '🔑 登录凭据';
    credBtn.title = '配置掉登录后自动重新登录用的邮箱 / 密码 / 2FA 密钥';
    credBtn.addEventListener('click', () => {
        accountPoolOpenCredentialId =
            accountPoolOpenCredentialId === account.user_id ? null : account.user_id;
        renderAccountPoolList();
    });

    const refreshBtn = document.createElement('button');
    refreshBtn.type = 'button';
    refreshBtn.className = 'account-pool-mini-btn';
    refreshBtn.textContent = '🔎 刷新积分';
    refreshBtn.title = '打开一次浏览器真实探测积分，比较慢';
    refreshBtn.addEventListener('click', () => refreshAccountPoolCredit(account.user_id, refreshBtn));

    const closeBrowserBtn = document.createElement('button');
    closeBrowserBtn.type = 'button';
    closeBrowserBtn.className = 'account-pool-mini-btn';
    closeBrowserBtn.textContent = '🛑 关闭浏览器';
    closeBrowserBtn.title = '关闭该 AdsPower 浏览器实例';
    closeBrowserBtn.addEventListener('click', () => closeAccountPoolBrowser(account.user_id, closeBrowserBtn));

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'account-pool-mini-btn danger';
    deleteBtn.textContent = '🗑️';
    deleteBtn.title = '从号池移除（不影响 AdsPower 里的浏览器环境本身）';
    deleteBtn.addEventListener('click', () => deleteAccountPoolAccount(account.user_id));

    return { zoomBtn, expiresBtn, credBtn, refreshBtn, closeBrowserBtn, deleteBtn };
}

function renderAccountPoolListRow(account, idx, sortedAccounts, list) {
    const isSelected = accountPoolSelected.has(account.user_id);
    const isPriority = accountPoolPriorityUserIds.has(account.user_id);
    const row = document.createElement('div');
    row.className = 'account-pool-row'
        + ` tone-${getAccountPoolTone(account)}`
        + (account.disabled ? ' disabled' : '')
        + (isSelected ? ' selected' : '');

    // 1. 批量多选复选框
    const check = createAccountPoolCheckbox(account, idx, sortedAccounts, row);
    row.appendChild(check);

    // 2. 序号徽章
    const seqBadge = document.createElement('span');
    seqBadge.className = 'account-pool-seq-badge';
    seqBadge.textContent = `#${idx + 1}`;
    seqBadge.title = `序号: 第 ${idx + 1} 个账号`;
    row.appendChild(seqBadge);

    // 3. 优先级星标 (⭐/☆)
    const star = createAccountPoolPriorityStar(account);
    row.appendChild(star);

    // 4. 启用/禁用状态徽章
    const statusBadge = createAccountPoolStatusBadge(account);
    row.appendChild(statusBadge);

    // 5. 主体信息
    const main = document.createElement('div');
    main.className = 'account-pool-main';

    const inputsWrap = document.createElement('div');
    inputsWrap.className = 'account-pool-row-inputs';

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'account-pool-name-input';
    nameInput.value = account.name || '';
    nameInput.placeholder = '账号命名（默认用邮箱地址）';
    nameInput.title = '账号命名——留空会自动回退为该环境的邮箱地址';
    nameInput.addEventListener('change', () => renameAccountPoolAccount(account.user_id, nameInput.value));
    inputsWrap.appendChild(nameInput);

    const noteInput = document.createElement('input');
    noteInput.type = 'text';
    noteInput.className = 'account-pool-note-input';
    noteInput.value = account.note || '';
    noteInput.placeholder = '备注（选填）';
    noteInput.title = '备注——纯自由文本，跟账号命名互不影响';
    noteInput.addEventListener('change', () => updateAccountPoolNote(account.user_id, noteInput.value));
    inputsWrap.appendChild(noteInput);

    main.appendChild(inputsWrap);

    // 元数据行 (Meta)
    const meta = document.createElement('div');
    meta.className = 'account-pool-meta';

    if (account.serial_number) {
        const snSpan = document.createElement('span');
        snSpan.className = 'account-pool-chip chip-sn';
        snSpan.textContent = `#${account.serial_number}`;
        snSpan.title = `AdsPower 环境编号: #${account.serial_number}`;
        meta.appendChild(snSpan);
    }

    const uidSpan = document.createElement('span');
    uidSpan.className = 'account-pool-chip chip-uid';
    uidSpan.textContent = `ID: ${account.user_id}`;
    uidSpan.title = `AdsPower user_id: ${account.user_id}`;
    meta.appendChild(uidSpan);

    const creditBadge = buildCreditBadgeElement(account);
    meta.appendChild(creditBadge);

    const healthBadge = buildHealthScoreBadgeElement(account);
    meta.appendChild(healthBadge);

    const taskBadge = buildTaskCountBadgeElement(account);
    meta.appendChild(taskBadge);

    if (account.expires_at) {
        const expBadge = document.createElement('span');
        expBadge.className = 'account-pool-chip chip-expires is-on';
        expBadge.textContent = `⏳ ${account.expires_at}`;
        expBadge.title = `重置日期: ${account.expires_at}`;
        meta.appendChild(expBadge);
    }

    if (isPriority) {
        const priSpan = document.createElement('span');
        priSpan.className = 'account-pool-chip chip-priority';
        priSpan.textContent = '⭐ 优先级实例';
        meta.appendChild(priSpan);
    }

    const probeFailed = account.last_probe_status === 'failed';
    const lastCheckSpan = document.createElement('span');
    lastCheckSpan.className = 'account-pool-meta-text';
    lastCheckSpan.textContent = `上次成功: ${formatAccountPoolTimestamp(account.last_checked_at)}${probeFailed ? ` · ⚠️ 探测失败: ${account.last_probe_error || '未知原因'}` : ''}`;
    meta.appendChild(lastCheckSpan);

    main.appendChild(meta);

    const loginText = describeAccountAutoLogin(account);
    if (loginText) {
        const loginMeta = document.createElement('div');
        const isNo2FA = !account.has_totp || account.auto_login_blocked;
        loginMeta.className = `account-pool-meta account-pool-login-meta ${isNo2FA ? 'alert-danger is-no-2fa' : 'alert-info'}`;
        loginMeta.textContent = loginText;
        main.appendChild(loginMeta);
    }

    row.appendChild(main);

    // 6. 操作按钮
    const actions = createAccountPoolActionButtons(account);
    const actionsWrap = document.createElement('div');
    actionsWrap.className = 'account-pool-row-actions';
    actionsWrap.append(actions.zoomBtn, actions.expiresBtn, actions.credBtn, actions.refreshBtn, actions.closeBrowserBtn, actions.deleteBtn);
    row.appendChild(actionsWrap);

    // 双击行打开单独放大详情
    row.addEventListener('dblclick', (e) => {
        if (!['INPUT', 'BUTTON', 'SELECT'].includes(e.target.tagName)) {
            openAccountPoolDetailModal(account.user_id);
        }
    });

    list.appendChild(row);

    if (accountPoolOpenCredentialId === account.user_id) {
        list.appendChild(buildAccountCredentialForm(account));
    }
}

function renderAccountPoolGridCard(account, idx, sortedAccounts, list) {
    const isSelected = accountPoolSelected.has(account.user_id);
    const isPriority = accountPoolPriorityUserIds.has(account.user_id);
    const card = document.createElement('div');
    card.className = 'account-pool-card'
        + ` tone-${getAccountPoolTone(account)}`
        + (account.disabled ? ' disabled' : '')
        + (isSelected ? ' selected' : '');

    // 1. 顶部栏 (Header)
    const header = document.createElement('div');
    header.className = 'account-pool-card-header';

    const headerLeft = document.createElement('div');
    headerLeft.className = 'account-pool-card-header-left';

    const check = createAccountPoolCheckbox(account, idx, sortedAccounts, card);
    headerLeft.appendChild(check);

    const seqBadge = document.createElement('span');
    seqBadge.className = 'account-pool-seq-badge';
    seqBadge.textContent = `#${idx + 1}`;
    seqBadge.title = `序号: 第 ${idx + 1} 个账号`;
    headerLeft.appendChild(seqBadge);

    if (account.serial_number) {
        const snSpan = document.createElement('span');
        snSpan.className = 'account-pool-chip chip-sn';
        snSpan.textContent = `#${account.serial_number}`;
        snSpan.title = `AdsPower 环境编号: #${account.serial_number}`;
        headerLeft.appendChild(snSpan);
    }
    header.appendChild(headerLeft);

    const headerRight = document.createElement('div');
    headerRight.className = 'account-pool-card-header-right';

    // 卡片头部的放大按钮只留图标：带上"放大"两个字，头部一行在 272px 的列里
    // 放不下，会折成两行，每张卡片白白多占 30px。列表视图仍是带文字的宽按钮。
    const cardZoomBtn = document.createElement('button');
    cardZoomBtn.type = 'button';
    cardZoomBtn.className = 'account-pool-mini-btn zoom-btn card-zoom-icon';
    cardZoomBtn.textContent = '🔍';
    cardZoomBtn.title = '单独放大查看并编辑该账号的全部详细信息与凭据（双击卡片同效）';
    cardZoomBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openAccountPoolDetailModal(account.user_id);
    });
    headerRight.appendChild(cardZoomBtn);

    const star = createAccountPoolPriorityStar(account);
    headerRight.appendChild(star);

    const statusBadge = createAccountPoolStatusBadge(account);
    headerRight.appendChild(statusBadge);

    header.appendChild(headerRight);
    card.appendChild(header);

    // 2. 主体 (Body)
    const body = document.createElement('div');
    body.className = 'account-pool-card-body';

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'account-pool-name-input card-name-input';
    nameInput.value = account.name || '';
    nameInput.placeholder = '账号命名（默认用邮箱）';
    nameInput.title = '账号命名——留空会自动回退为该环境的邮箱地址';
    nameInput.addEventListener('change', () => renameAccountPoolAccount(account.user_id, nameInput.value));
    body.appendChild(nameInput);

    const noteInput = document.createElement('input');
    noteInput.type = 'text';
    noteInput.className = 'account-pool-note-input card-note-input';
    noteInput.value = account.note || '';
    noteInput.placeholder = '备注（选填）';
    noteInput.title = '备注——纯自由文本，跟账号命名互不影响';
    noteInput.addEventListener('change', () => updateAccountPoolNote(account.user_id, noteInput.value));
    body.appendChild(noteInput);

    // 核心指标行 (Hero Metrics)
    const metricsRow = document.createElement('div');
    metricsRow.className = 'account-pool-card-metrics';

    const creditBadge = buildCreditBadgeElement(account);
    metricsRow.appendChild(creditBadge);

    const healthBadge = buildHealthScoreBadgeElement(account);
    metricsRow.appendChild(healthBadge);

    const taskBadge = buildTaskCountBadgeElement(account);
    metricsRow.appendChild(taskBadge);

    body.appendChild(metricsRow);

    // 详细信息条目
    const metaList = document.createElement('div');
    metaList.className = 'account-pool-card-meta-list';

    const uidRow = document.createElement('div');
    uidRow.className = 'account-pool-card-meta-item';
    uidRow.innerHTML = `<span class="meta-label">ID:</span><span class="meta-val monospace" title="${account.user_id}">${account.user_id}</span>`;
    metaList.appendChild(uidRow);

    const expRow = document.createElement('div');
    expRow.className = 'account-pool-card-meta-item';
    const expText = account.expires_at ? `⏳ ${account.expires_at}` : '📅 未设重置日期';
    expRow.innerHTML = `<span class="meta-label">重置:</span><span class="meta-val ${account.expires_at ? 'has-exp' : ''}">${expText}</span>`;
    metaList.appendChild(expRow);

    const timeRow = document.createElement('div');
    timeRow.className = 'account-pool-card-meta-item';
    timeRow.innerHTML = `<span class="meta-label">检查:</span><span class="meta-val">${formatAccountPoolTimestamp(account.last_checked_at)}</span>`;
    metaList.appendChild(timeRow);

    if (isPriority) {
        const priRow = document.createElement('div');
        priRow.className = 'account-pool-card-alert alert-priority';
        priRow.textContent = '⭐ 优先级实例（优先调度）';
        metaList.appendChild(priRow);
    }

    if (account.last_probe_status === 'failed') {
        const probeErr = document.createElement('div');
        probeErr.className = 'account-pool-card-alert alert-warning';
        probeErr.textContent = `⚠️ 最近探测失败: ${account.last_probe_error || '未知原因'}`;
        metaList.appendChild(probeErr);
    }

    const loginText = describeAccountAutoLogin(account);
    if (loginText) {
        const loginRow = document.createElement('div');
        const isNo2FA = !account.has_totp || account.auto_login_blocked;
        loginRow.className = `account-pool-card-alert ${isNo2FA ? 'alert-danger is-no-2fa' : 'alert-info'}`;
        loginRow.textContent = loginText;
        metaList.appendChild(loginRow);
    }

    body.appendChild(metaList);
    card.appendChild(body);

    // 3. 底部操作栏 (Footer)
    // 「放大」在卡片头部已经有一个了，底部不再重复放——省下的位置正好让
    // 剩下四个按钮排成规整的 2×2，不用再挤成一行溢出。
    const actions = createAccountPoolActionButtons(account);
    const footer = document.createElement('div');
    footer.className = 'account-pool-card-footer';
    footer.append(actions.expiresBtn, actions.credBtn, actions.deleteBtn, actions.refreshBtn, actions.closeBrowserBtn);
    card.appendChild(footer);

    // 4. 凭据表单内嵌
    if (accountPoolOpenCredentialId === account.user_id) {
        const credWrap = document.createElement('div');
        credWrap.className = 'account-pool-card-cred-wrap';
        credWrap.appendChild(buildAccountCredentialForm(account));
        card.appendChild(credWrap);
    }

    // 双击卡片打开单独放大详情
    card.addEventListener('dblclick', (e) => {
        if (!['INPUT', 'BUTTON', 'SELECT'].includes(e.target.tagName)) {
            openAccountPoolDetailModal(account.user_id);
        }
    });

    list.appendChild(card);
}

function renderAccountPoolList() {
    const list = document.getElementById('account-pool-list');
    if (!list) return;
    list.textContent = '';

    // 清理已被移除的选中 ID
    const validIds = new Set(accountPoolCache.map(a => a.user_id));
    for (const uid of accountPoolSelected) {
        if (!validIds.has(uid)) accountPoolSelected.delete(uid);
    }
    renderAccountPoolStats();
    updateAccountPoolBulkBar();
    updateAccountPoolViewSwitcherUI();
    updateAccountPoolSearchUI();

    if (accountPoolCache.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = '号池还是空的——展开下面的「添加账号」把 AdsPower 环境加进来，帧序列 / 视频生成会自动按额度选号。';
        list.appendChild(empty);
        renderAccountPoolGlobalHUD();
        return;
    }

    const sortedAccounts = getVisibleAccountPoolAccounts();

    // 筛选/搜索把结果筛空了：跟"池子本来就是空的"是两回事，给一个能退回去的出口
    if (sortedAccounts.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = `没有匹配的账号（共 ${accountPoolCache.length} 个）——换个关键词，或点下面的按钮取消筛选。`;
        const reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'account-pool-mini-btn primary';
        reset.style.marginTop = '10px';
        reset.textContent = '↺ 清除筛选与搜索';
        reset.addEventListener('click', () => {
            accountPoolFilter = 'all';
            accountPoolSearch = '';
            const input = document.getElementById('account-pool-search');
            if (input) input.value = '';
            renderAccountPoolList();
        });
        empty.appendChild(document.createElement('br'));
        empty.appendChild(reset);
        list.appendChild(empty);
        renderAccountPoolGlobalHUD();
        return;
    }

    sortedAccounts.forEach((account, idx) => {
        if (accountPoolViewMode === 'grid') {
            renderAccountPoolGridCard(account, idx, sortedAccounts, list);
        } else {
            renderAccountPoolListRow(account, idx, sortedAccounts, list);
        }
    });

    renderAccountPoolGlobalHUD();
}

// ============================================================
// 自动登录凭据
// —— 号池账号掉登录后，用这里存的邮箱/密码/2FA 密钥自己登回去，不再停下来等人工
//    （后端：integrations/google_fx/utils/auto_login.py）。
//    密码与 2FA 密钥明文存在服务端 runtime/account_credentials.json，接口从不
//    回传明文，所以前端永远只知道"有没有"，输入框留空一律表示"不修改"。
// ============================================================

// 返回空串 = 这一行不值得多占一行（没配过凭据，行为跟以前完全一样）。
function describeAccountAutoLogin(account) {
    if (account.auto_login_blocked) {
        return `⛔ 自动登录已熔断（连续凭据失败）：${account.auto_login_error || '原因未记录'} — 核对凭据后可解除`;
    }
    if (!account.auto_login_ready) {
        return '';
    }
    const parts = [`🔓 自动登录：已就绪（${account.login_email || '邮箱未知'}`
        + `${account.has_totp ? ' · 含 2FA' : ' · 无 2FA'}）`];
    if (account.auto_login_status === 'ok') {
        parts.push(`上次成功 ${formatAccountPoolTimestamp(account.auto_login_at)}`);
    } else if (account.auto_login_status === 'failed') {
        parts.push(`⚠️ 上次失败：${account.auto_login_error || '未知原因'}`);
    }
    return parts.join(' · ');
}

function buildAccountCredentialForm(account) {
    const form = document.createElement('div');
    form.className = 'account-pool-cred-form';

    const title = document.createElement('div');
    title.className = 'account-pool-cred-title';
    title.textContent = `🔑 ${account.name || account.user_id} 的自动登录凭据`;
    form.appendChild(title);

    if (!account.has_totp) {
        const no2faTip = document.createElement('div');
        no2faTip.className = 'account-pool-card-alert alert-danger is-no-2fa';
        no2faTip.style.margin = '4px 0 8px 0';
        no2faTip.textContent = '⚠️ 2FA 密钥未就绪：建议保存 2FA 密钥，以便自动处理 Google 两步验证';
        form.appendChild(no2faTip);
    }

    const fields = document.createElement('div');
    fields.className = 'account-pool-cred-fields';

    const emailInput = document.createElement('input');
    emailInput.type = 'text';
    emailInput.autocomplete = 'off';
    emailInput.placeholder = 'Google 邮箱';
    // 邮箱是唯一会回传原文的字段。没配过凭据时用账号命名兜底——号池的命名默认就是
    // AdsPower 环境名，而那通常正是邮箱，省掉一次手输。
    emailInput.value = account.login_email
        || (String(account.name || '').includes('@') ? account.name : '');
    fields.appendChild(emailInput);

    const passwordInput = document.createElement('input');
    passwordInput.type = 'password';
    passwordInput.autocomplete = 'new-password';
    passwordInput.placeholder = account.has_password ? '密码（已保存，留空=不修改）' : 'Google 密码';
    fields.appendChild(passwordInput);

    const totpInput = document.createElement('input');
    totpInput.type = 'password';
    totpInput.autocomplete = 'new-password';
    totpInput.placeholder = account.has_totp
        ? '2FA 密钥（已保存，留空=不修改）'
        : '2FA 密钥（base32，选填）';
    totpInput.title = '在 Google 两步验证里绑定「身份验证器 App」时，二维码下方那串'
        + ' base32 密钥（形如 abcd efgh ijkl mnop）。填了才能全自动过两步验证。';
    fields.appendChild(totpInput);

    form.appendChild(fields);

    // 只有已经存了 2FA 密钥时才给"移除"入口。留空表示"不修改"，所以单靠输入框
    // 没法表达"我要删掉已存的密钥"——需要一个显式动作。
    let dropTotp = null;
    if (account.has_totp) {
        const label = document.createElement('label');
        label.className = 'account-pool-cred-check';
        dropTotp = document.createElement('input');
        dropTotp.type = 'checkbox';
        label.appendChild(dropTotp);
        label.appendChild(document.createTextNode(' 移除已保存的 2FA 密钥（该账号已关闭两步验证时勾选）'));
        form.appendChild(label);
    }

    const actions = document.createElement('div');
    actions.className = 'account-pool-cred-actions';

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'account-pool-mini-btn primary';
    saveBtn.textContent = '💾 保存';
    saveBtn.addEventListener('click', () => saveAccountCredentials(account.user_id, {
        email: emailInput.value,
        password: passwordInput.value,
        totpSecret: totpInput.value,
        dropTotp: Boolean(dropTotp && dropTotp.checked),
    }, saveBtn));
    actions.appendChild(saveBtn);

    const testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'account-pool-mini-btn';
    testBtn.textContent = '🔓 测试登录';
    testBtn.title = '立刻开一次浏览器，用存好的凭据把这个号登进去。比较慢（要走完整个登录表单）。';
    testBtn.disabled = !account.auto_login_ready;
    testBtn.addEventListener('click', () => testAccountLogin(account.user_id, testBtn));
    actions.appendChild(testBtn);

    if (account.auto_login_blocked) {
        const unblockBtn = document.createElement('button');
        unblockBtn.type = 'button';
        unblockBtn.className = 'account-pool-mini-btn warning';
        unblockBtn.textContent = '♻️ 解除熔断';
        unblockBtn.title = '确认凭据没问题（上次失败是网络/环境原因）时点这里恢复自动登录';
        unblockBtn.addEventListener('click', () => resetAccountLoginBreaker(account.user_id, unblockBtn));
        actions.appendChild(unblockBtn);
    }

    if (account.auto_login_ready || account.has_totp) {
        const clearBtn = document.createElement('button');
        clearBtn.type = 'button';
        clearBtn.className = 'account-pool-mini-btn danger';
        clearBtn.textContent = '🗑️ 清除凭据';
        clearBtn.addEventListener('click', () => deleteAccountCredentials(account.user_id, clearBtn));
        actions.appendChild(clearBtn);
    }

    form.appendChild(actions);

    const hint = document.createElement('div');
    hint.className = 'account-pool-cred-hint';
    hint.textContent = '密码与 2FA 密钥明文保存在本机 runtime/account_credentials.json'
        + '（已排除出 git，文件权限收到仅本人可读），接口不会把明文回传给浏览器。'
        + '连续 2 次凭据失败会自动熔断停止重试，避免撞错密码把账号锁掉。';
    form.appendChild(hint);

    return form;
}

async function saveAccountCredentials(userId, values, btnEl) {
    const email = String(values.email || '').trim();
    if (!email) {
        showToast('邮箱不能为空', 'error');
        return;
    }
    // 只把用户实际动过的字段发上去。密码/2FA 留空 = 不修改，这跟后端
    // /api/account-pool/credentials 的「字段缺省 = 不改」约定是同一件事：
    // 列表接口不回传明文，输入框天生是空的，把空当成清空会让人改个邮箱就丢密码。
    const payload = { user_id: userId, email };
    if (String(values.password || '')) payload.password = values.password;
    if (values.dropTotp) {
        payload.totp_secret = '';
    } else if (String(values.totpSecret || '').trim()) {
        payload.totp_secret = values.totpSecret;
    }

    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ 保存中...'; }
    try {
        const resp = await fetch('/api/account-pool/credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            await loadAccountPool();
            showToast('凭据已保存，掉登录时会自动重新登录', 'success');
        } else if (resp.status === 404) {
            showToast('本地服务没有这个接口——请重启本地服务后再试', 'error');
        } else {
            showToast(`保存失败：${(data && (data.message || data.error)) || `HTTP ${resp.status}`}`, 'error');
        }
    } catch (e) {
        showToast('保存失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = originalText; }
    }
}

async function testAccountLogin(userId, btnEl) {
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ 登录中...'; }
    try {
        const resp = await fetch('/api/account-pool/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        await loadAccountPool();
        if (data && data.status === 'ok') {
            showToast(data.message || '登录成功', 'success');
        } else if (resp.status === 409) {
            // 浏览器忙 / 队列暂停：账号本身没问题，等会儿再点就行，
            // 报成失败会让用户跑去改凭据。
            showToast((data && data.message) || '浏览器忙，请稍后再试', 'warning');
        } else {
            showToast(`登录失败：${(data && (data.message || data.error)) || `HTTP ${resp.status}`}`, 'error');
        }
    } catch (e) {
        showToast('测试登录失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = originalText; }
    }
}

async function resetAccountLoginBreaker(userId, btnEl) {
    if (btnEl) btnEl.disabled = true;
    try {
        const resp = await fetch('/api/account-pool/reset-breaker', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            await loadAccountPool();
            showToast('已解除熔断，下次掉登录会重新尝试自动登录', 'success');
        } else {
            showToast(`解除失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('解除失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) btnEl.disabled = false;
    }
}

async function deleteAccountCredentials(userId, btnEl) {
    if (!confirm('确定要清除这个账号的登录凭据吗？清除后掉登录会重新变成停下来等人工处理。')) return;
    if (btnEl) btnEl.disabled = true;
    try {
        const resp = await fetch('/api/account-pool/credentials/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            await loadAccountPool();
            showToast('凭据已清除', 'success');
        } else {
            showToast(`清除失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('清除失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) btnEl.disabled = false;
    }
}

async function addAccountPoolAccount() {
    const select = document.getElementById('account-pool-add-select');
    const nameInput = document.getElementById('account-pool-add-name');
    const noteInput = document.getElementById('account-pool-add-note');
    const expiresInput = document.getElementById('account-pool-add-expires');
    const userId = select ? select.value : '';
    if (!userId) {
        showToast('请先选一个 AdsPower 环境', 'error');
        return;
    }
    try {
        const resp = await fetch('/api/account-pool', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_id: userId,
                name: nameInput ? nameInput.value : '',
                note: noteInput ? noteInput.value : '',
                expires_at: expiresInput && expiresInput.value ? expiresInput.value : undefined
            })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            if (nameInput) nameInput.value = '';
            if (noteInput) noteInput.value = '';
            if (expiresInput) expiresInput.value = '';
            await loadAccountPool();
            await loadAccountPoolAdspowerProfiles();
            showToast('已添加到号池', 'success');
        } else {
            showToast(`添加失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('添加失败，请检查本地服务', 'error');
    }
}

async function bulkSetAccountPoolPriority(isPriority) {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    userIds.forEach(uid => {
        if (isPriority) accountPoolPriorityUserIds.add(uid);
        else accountPoolPriorityUserIds.delete(uid);
    });
    await saveAccountPoolStrategyConfig();
    renderAccountPoolList();
    showToast(isPriority
        ? `已将选中的 ${userIds.length} 个账号设为优先级实例（生成时优先调度）`
        : `已取消选中的 ${userIds.length} 个账号的优先级`, 'success');
}

async function bulkSetAccountPoolExpires() {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    const current = prompt(`批量设置重置日期：\n请输入要应用到选中的 ${userIds.length} 个账号的重置日期 (格式: YYYY-MM-DD，留空清空):`, '');
    if (current === null) return;
    const expVal = current.trim() || null;
    try {
        const resp = await fetch('/api/account-pool/expires-at', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: userIds, expires_at: expVal })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            await loadAccountPool();
            showToast(`已成功为 ${data.updated_count || userIds.length} 个账号更新重置日期`, 'success');
        } else {
            showToast(`批量设置重置日期失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量设置重置日期失败，请检查本地服务', 'error');
    }
}

// 一键把本机 AdsPower 所有环境并入号池：环境有几十个时挨着选下拉框太累。
// 已在池子里的账号后端原样跳过（不覆盖命名/备注，也不重置积分与禁用/冷却状态），
// 所以这个按钮可以反复点，等于"同步一下新建的环境"。
async function importAllAccountPoolProfiles(btnEl) {
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '⏳ 读取 AdsPower 环境...';
    }
    try {
        const resp = await fetch('/api/account-pool/import-all', { method: 'POST' });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            if (Array.isArray(data.accounts)) {
                accountPoolCache = data.accounts;
                renderAccountPoolList();
            } else {
                await loadAccountPool();
            }
            await loadAccountPoolAdspowerProfiles();
            if (data.total === 0) {
                showToast('没读到任何 AdsPower 环境——确认 AdsPower 已启动且开了本地 API', 'error');
            } else if (data.added === 0) {
                showToast(`号池已包含全部 ${data.total} 个环境，无需新增`, 'info');
            } else {
                showToast(`已添加 ${data.added} 个环境${data.skipped ? `，跳过已存在的 ${data.skipped} 个` : ''}`, 'success');
            }
        } else if (resp.status === 404) {
            // 本地服务还跑着加这个接口之前的旧代码：未匹配路由走 404 兜底，
            // 回的是 {error:'Not found'} 而不是 {status,message}，照旧写法只能
            // 显示"未知错误"，让人以为是号池坏了。直说要重启。
            showToast('本地服务没有这个接口——请重启本地服务（run.sh / run.command）后再试', 'error');
        } else {
            const detail = (data && (data.message || data.error)) || `HTTP ${resp.status}`;
            showToast(`一键添加失败：${detail}`, 'error');
        }
    } catch (e) {
        showToast('一键添加失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = originalText;
        }
    }
}

// 命名跟备注是两个独立字段，但共用同一个 upsert 接口（POST /api/account-pool），
// 所以改其中一个时要把另一个的当前值原样带上，不然会被留空覆盖掉。
async function renameAccountPoolAccount(userId, name) {
    const cached = accountPoolCache.find(a => a.user_id === userId);
    const note = cached ? (cached.note || '') : '';
    try {
        const resp = await fetch('/api/account-pool', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, name, note })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            const idx = accountPoolCache.findIndex(a => a.user_id === userId);
            if (idx >= 0) accountPoolCache[idx] = data.account;
            renderAccountPoolList(); // 命名留空时后端会回退成邮箱，需要重渲染显示回填结果
        } else {
            showToast(`改命名失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('改命名失败，请检查本地服务', 'error');
    }
}

async function updateAccountPoolNote(userId, note) {
    const cached = accountPoolCache.find(a => a.user_id === userId);
    const name = cached ? (cached.name || '') : '';
    try {
        const resp = await fetch('/api/account-pool', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, name, note })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            const idx = accountPoolCache.findIndex(a => a.user_id === userId);
            if (idx >= 0) accountPoolCache[idx] = data.account;
        } else {
            showToast(`改备注失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('改备注失败，请检查本地服务', 'error');
    }
}

async function toggleAccountPoolAccount(userId, disabled) {
    try {
        const resp = await fetch('/api/account-pool/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, disabled })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            const idx = accountPoolCache.findIndex(a => a.user_id === userId);
            if (idx >= 0) accountPoolCache[idx] = data.account;
            renderAccountPoolList();
        } else {
            showToast(`切换失败：${(data && data.message) || '未知错误'}`, 'error');
            renderAccountPoolList();
        }
    } catch (e) {
        showToast('切换失败，请检查本地服务', 'error');
        renderAccountPoolList();
    }
}

async function refreshAccountPoolCredit(userId, btnEl) {
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '⏳ 探测中...';
    }
    try {
        const resp = await fetch('/api/account-pool/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        if (data && data.account) {
            const idx = accountPoolCache.findIndex(a => a.user_id === userId);
            if (idx >= 0) accountPoolCache[idx] = data.account;
            renderAccountPoolList();
        }
        if (data && data.status === 'ok') {
            showToast(`积分：${data.account.credit}`, 'success');
        } else if (data && ['FX_BUSY', 'FX_PROBE_RUNNING', 'FX_PAUSED'].includes(data.code)) {
            // 浏览器被占着不是"刷新失败"——账号没问题，等一会再点就行。
            // 报成失败会让人以为账号坏了，跑去清冷却/重登。
            showToast(data.message, 'warning');
        } else {
            showToast(`刷新失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('刷新失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = originalText;
        }
    }
}

async function closeAccountPoolBrowser(userId, btnEl) {
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '⏳ 关闭中...';
    }
    try {
        const resp = await fetch('/api/account-pool/close-browser', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            showToast(data.message || '已发送关闭浏览器指令', 'success');
        } else {
            showToast(`关闭浏览器失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('关闭浏览器失败，请检查本地服务', 'error');
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = originalText;
        }
    }
}

async function deleteAccountPoolAccount(userId) {
    if (!confirm('确定要从号池移除这个账号吗？（不会影响 AdsPower 里的浏览器环境本身）')) return;
    try {
        const resp = await fetch('/api/account-pool/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            accountPoolCache = accountPoolCache.filter(a => a.user_id !== userId);
            renderAccountPoolList();
            await loadAccountPoolAdspowerProfiles();
            showToast('已移除', 'success');
        } else {
            showToast(`移除失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('移除失败，请检查本地服务', 'error');
    }
}

// ============================================================
// 单账号单独放大详情看板 (Account Detail Enlarge Modal)
// ============================================================

let accountPoolDetailModalUserId = null;

function openAccountPoolDetailModal(userId) {
    if (!userId) return;
    accountPoolDetailModalUserId = userId;
    const account = accountPoolCache.find(a => a.user_id === userId);
    if (!account) return;
    renderAccountPoolDetailModal(account);
}

function closeAccountPoolDetailModal() {
    accountPoolDetailModalUserId = null;
    const modal = document.getElementById('account-pool-detail-modal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 180);
    }
}

function renderAccountPoolDetailModal(account) {
    let modal = document.getElementById('account-pool-detail-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.className = 'modal active account-pool-detail-modal';
        modal.id = 'account-pool-detail-modal';
        modal.style.zIndex = '1100';
        document.body.appendChild(modal);
    }

    const sortedAccounts = sortAccountPoolList(accountPoolCache, accountPoolSortKey);
    const currentIndex = sortedAccounts.findIndex(a => a.user_id === account.user_id);
    const hasPrev = currentIndex > 0;
    const hasNext = currentIndex < sortedAccounts.length - 1;
    const isPriority = accountPoolPriorityUserIds.has(account.user_id);

    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));

    modal.innerHTML = `
        <div class="modal-content glass-panel apd-modal-content">
            <!-- 头部导航与状态 -->
            <div class="apd-header">
                <div class="apd-header-left">
                    <span style="font-size: 20px;">👤</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="account-pool-seq-badge">#${currentIndex + 1}</span>
                        ${account.serial_number ? `<span class="account-pool-chip chip-sn">#${escapeStr(account.serial_number)}</span>` : ''}
                        <span style="font-weight: 700; font-size: 14.5px;">${escapeStr(account.name || account.user_id)}</span>
                    </div>
                </div>

                <div class="apd-header-right">
                    <!-- 上一个/下一个账号切换 -->
                    <button type="button" class="apd-nav-btn" id="apd-prev-btn" ${!hasPrev ? 'disabled' : ''} title="切换到上一个账号 (←)">◀ 上一个</button>
                    <span style="font-size: 11.5px; color: var(--text-muted);">${currentIndex + 1} / ${sortedAccounts.length}</span>
                    <button type="button" class="apd-nav-btn" id="apd-next-btn" ${!hasNext ? 'disabled' : ''} title="切换到下一个账号 (→)">下一个 ▶</button>

                    <!-- 优先级星标与状态 -->
                    <span class="account-pool-priority-star ${isPriority ? 'is-priority' : 'not-priority'}" id="apd-star" style="font-size: 18px; margin: 0 4px;" title="${isPriority ? '取消优先' : '设为优先级'}">${isPriority ? '⭐' : '☆'}</span>
                    <span class="account-pool-status-badge ${account.disabled ? 'is-disabled' : 'is-enabled'}" id="apd-status-badge" style="cursor: pointer;">${account.disabled ? '🚫 已禁用' : '✅ 已启用'}</span>

                    <button type="button" class="close-btn apd-close-btn" title="关闭 (Esc)">&times;</button>
                </div>
            </div>

            <!-- 主体两栏网格 -->
            <div class="apd-body">
                <div class="apd-grid-2col">
                    <!-- 左栏：基础信息与状态指标 -->
                    <div style="display: flex; flex-direction: column; gap: 14px;">
                        <!-- 账号标识卡片 -->
                        <div class="apd-section-card">
                            <div class="apd-section-title">🏷️ 账号命名与备注</div>
                            <div class="apd-field">
                                <label class="apd-field-label">账号命名：</label>
                                <input type="text" class="apd-input" id="apd-name-input" value="${escapeStr(account.name || '')}" placeholder="默认使用邮箱地址">
                            </div>
                            <div class="apd-field">
                                <label class="apd-field-label">备注说明：</label>
                                <input type="text" class="apd-input" id="apd-note-input" value="${escapeStr(account.note || '')}" placeholder="选填，如主号/备用/欠费号">
                            </div>
                            <div class="apd-field">
                                <label class="apd-field-label">AdsPower 环境 ID：</label>
                                <div style="display: flex; gap: 6px; align-items: center;">
                                    <input type="text" class="apd-input" value="${escapeStr(account.user_id)}" readonly style="font-family: var(--font-mono, monospace); font-size: 11.5px; opacity: 0.85;">
                                    <button type="button" class="account-pool-mini-btn" id="apd-copy-id-btn" title="复制 ID">📋 复制</button>
                                </div>
                            </div>
                        </div>

                        <!-- 积分与任务看板 -->
                        <div class="apd-section-card">
                            <div class="apd-section-title">📊 积分与任务统计</div>
                            <div class="apd-hero-metrics">
                                <div class="apd-hero-metric-box">
                                    <span class="apd-hero-metric-label">积分状态</span>
                                    <div class="apd-hero-metric-val" id="apd-credit-val"></div>
                                    <span style="font-size: 10.5px; color: var(--text-muted); margin-top: 2px;">上次探测: ${formatAccountPoolTimestamp(account.last_checked_at)}</span>
                                </div>
                                <div class="apd-hero-metric-box">
                                    <span class="apd-hero-metric-label">动态健康分</span>
                                    <div class="apd-hero-metric-val" id="apd-health-val"></div>
                                    <span style="font-size: 10.5px; color: var(--text-muted); margin-top: 2px;">智能调度首选依据</span>
                                </div>
                                <div class="apd-hero-metric-box">
                                    <span class="apd-hero-metric-label">累计任务数</span>
                                    <div class="apd-hero-metric-val">
                                        <span>${account.task_count != null ? account.task_count : ((account.image_task_count || 0) + (account.video_task_count || 0))}</span>
                                        <span style="font-size: 11px; font-weight: 500; color: var(--text-muted);">次</span>
                                    </div>
                                    <span style="font-size: 10.5px; color: var(--text-muted); margin-top: 2px;">🖼️ 图 ${account.image_task_count || 0} · 🎬 视频 ${account.video_task_count || 0}</span>
                                </div>
                            </div>
                            <div style="display: flex; gap: 8px; align-items: center; margin-top: 4px;">
                                <button type="button" class="account-pool-mini-btn primary" id="apd-refresh-credit-btn" style="flex: 1;">🔎 立即探测并刷新积分</button>
                            </div>
                            ${account.last_probe_status === 'failed' ? `
                                <div class="account-pool-card-alert alert-warning" style="margin-top: 4px;">
                                    ⚠️ 最近探测失败：${escapeStr(account.last_probe_error || '未知网络或登录错误')}
                                </div>
                            ` : ''}
                        </div>

                        <!-- 额度重置日期 -->
                        <div class="apd-section-card">
                            <div class="apd-section-title">⏳ 额度重置日期</div>
                            <div class="apd-field">
                                <label class="apd-field-label">重置日期（用于重置日期最早优先选号）：</label>
                                <div style="display: flex; gap: 8px; align-items: center;">
                                    <input type="date" class="apd-input" id="apd-expires-input" value="${account.expires_at || ''}">
                                    <button type="button" class="account-pool-mini-btn" id="apd-save-expires-btn">💾 保存</button>
                                    <button type="button" class="account-pool-mini-btn danger" id="apd-clear-expires-btn" title="清除重置日期">✖ 清除</button>
                                </div>
                            </div>
                            <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px;">
                                <button type="button" class="account-pool-mini-btn" data-add-days="30">+30天</button>
                                <button type="button" class="account-pool-mini-btn" data-add-days="90">+90天</button>
                                <button type="button" class="account-pool-mini-btn" data-add-days="180">+半年</button>
                                <button type="button" class="account-pool-mini-btn" data-add-days="365">+1年</button>
                            </div>
                        </div>
                    </div>

                    <!-- 右栏：自动登录凭据与环境控制 -->
                    <div style="display: flex; flex-direction: column; gap: 14px;">
                        <!-- 自动登录凭据卡片 -->
                        <div class="apd-section-card" id="apd-cred-card">
                            <div class="apd-section-title">🔑 自动登录凭据 (Auto-Login)</div>
                            <div id="apd-cred-form-container"></div>
                        </div>

                        <!-- 环境浏览器控制 -->
                        <div class="apd-section-card">
                            <div class="apd-section-title">🌐 AdsPower 浏览器实例控制</div>
                            <p style="font-size: 11.5px; color: var(--text-muted); margin: 0; line-height: 1.5;">
                                可直接关闭或管理该账号对应的 AdsPower 浏览器窗口。
                            </p>
                            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px;">
                                <button type="button" class="account-pool-mini-btn" id="apd-close-browser-btn">🛑 关闭浏览器</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 底部操作栏 -->
            <div class="apd-footer">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <button type="button" class="account-pool-mini-btn danger" id="apd-delete-account-btn">🗑️ 从号池移除该账号</button>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <button type="button" class="action-btn text-btn mini-btn apd-close-btn" style="padding: 6px 16px;">完成并关闭</button>
                </div>
            </div>
        </div>
    `;

    bindAccountPoolDetailModalEvents(modal, account, sortedAccounts, currentIndex);
}

function bindAccountPoolDetailModalEvents(modal, account, sortedAccounts, currentIndex) {
    const hasPrev = currentIndex > 0;
    const hasNext = currentIndex < sortedAccounts.length - 1;
    const prevAccount = hasPrev ? sortedAccounts[currentIndex - 1] : null;
    const nextAccount = hasNext ? sortedAccounts[currentIndex + 1] : null;

    // 1. 关闭与背景点击
    modal.querySelectorAll('.apd-close-btn').forEach(btn => {
        btn.addEventListener('click', closeAccountPoolDetailModal);
    });
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeAccountPoolDetailModal();
    });

    // 2. 键盘事件监听 (Esc 关闭, ←/→ 切换)
    const onDetailKeyDown = (e) => {
        if (!accountPoolDetailModalUserId) {
            window.removeEventListener('keydown', onDetailKeyDown);
            return;
        }
        if (e.key === 'Escape') {
            closeAccountPoolDetailModal();
        } else if (e.key === 'ArrowLeft' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) {
            if (prevAccount) openAccountPoolDetailModal(prevAccount.user_id);
        } else if (e.key === 'ArrowRight' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) {
            if (nextAccount) openAccountPoolDetailModal(nextAccount.user_id);
        }
    };
    window.addEventListener('keydown', onDetailKeyDown);

    // 3. 上一个 / 下一个导航
    const prevBtn = modal.querySelector('#apd-prev-btn');
    if (prevBtn && prevAccount) {
        prevBtn.addEventListener('click', () => openAccountPoolDetailModal(prevAccount.user_id));
    }
    const nextBtn = modal.querySelector('#apd-next-btn');
    if (nextBtn && nextAccount) {
        nextBtn.addEventListener('click', () => openAccountPoolDetailModal(nextAccount.user_id));
    }

    // 4. 优先级星标与状态切换
    const star = modal.querySelector('#apd-star');
    if (star) {
        star.addEventListener('click', async () => {
            await toggleAccountPoolPriority(account.user_id);
            const updated = accountPoolCache.find(a => a.user_id === account.user_id);
            if (updated) renderAccountPoolDetailModal(updated);
        });
    }
    const statusBadge = modal.querySelector('#apd-status-badge');
    if (statusBadge) {
        statusBadge.addEventListener('click', async () => {
            await toggleAccountPoolAccount(account.user_id, !account.disabled);
            const updated = accountPoolCache.find(a => a.user_id === account.user_id);
            if (updated) renderAccountPoolDetailModal(updated);
        });
    }

    // 5. 账号命名与备注修改
    const nameInput = modal.querySelector('#apd-name-input');
    if (nameInput) {
        nameInput.addEventListener('change', () => renameAccountPoolAccount(account.user_id, nameInput.value));
    }
    const noteInput = modal.querySelector('#apd-note-input');
    if (noteInput) {
        noteInput.addEventListener('change', () => updateAccountPoolNote(account.user_id, noteInput.value));
    }

    // 6. 复制 ID
    const copyBtn = modal.querySelector('#apd-copy-id-btn');
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            if (navigator.clipboard) {
                navigator.clipboard.writeText(account.user_id);
                showToast('已复制环境 ID', 'success');
            }
        });
    }

    // 7. 积分与健康分指标卡片与刷新
    const creditVal = modal.querySelector('#apd-credit-val');
    if (creditVal) {
        creditVal.appendChild(buildCreditBadgeElement(account));
    }
    const healthVal = modal.querySelector('#apd-health-val');
    if (healthVal) {
        healthVal.appendChild(buildHealthScoreBadgeElement(account));
    }
    const refreshBtn = modal.querySelector('#apd-refresh-credit-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            await refreshAccountPoolCredit(account.user_id, refreshBtn);
            const updated = accountPoolCache.find(a => a.user_id === account.user_id);
            if (updated) renderAccountPoolDetailModal(updated);
        });
    }

    // 8. 重置日期操作
    const expiresInput = modal.querySelector('#apd-expires-input');
    const saveExpBtn = modal.querySelector('#apd-save-expires-btn');
    if (saveExpBtn && expiresInput) {
        saveExpBtn.addEventListener('click', async () => {
            const val = expiresInput.value.trim();
            account.expires_at = val;
            try {
                const resp = await fetch('/api/account-pool/expires-at', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: account.user_id, expires_at: val })
                });
                const data = await resp.json();
                if (data && data.status === 'ok') {
                    showToast('重置日期已更新', 'success');
                    renderAccountPoolList();
                    renderAccountPoolDetailModal(account);
                } else {
                    showToast(data.message || '更新失败', 'error');
                }
            } catch (e) {
                showToast('更新失败，请检查服务', 'error');
            }
        });
    }

    const clearExpBtn = modal.querySelector('#apd-clear-expires-btn');
    if (clearExpBtn && expiresInput) {
        clearExpBtn.addEventListener('click', async () => {
            expiresInput.value = '';
            account.expires_at = '';
            try {
                const resp = await fetch('/api/account-pool/expires-at', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: account.user_id, expires_at: '' })
                });
                const data = await resp.json();
                if (data && data.status === 'ok') {
                    showToast('已清除重置日期', 'success');
                    renderAccountPoolList();
                    renderAccountPoolDetailModal(account);
                } else {
                    showToast(data.message || '清除失败', 'error');
                }
            } catch (e) {
                showToast('清除失败，请检查服务', 'error');
            }
        });
    }

    modal.querySelectorAll('[data-add-days]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const days = parseInt(btn.dataset.addDays, 10);
            const base = account.expires_at ? new Date(account.expires_at) : new Date();
            base.setDate(base.getDate() + days);
            const yyyy = base.getFullYear();
            const mm = String(base.getMonth() + 1).padStart(2, '0');
            const dd = String(base.getDate()).padStart(2, '0');
            const newDate = `${yyyy}-${mm}-${dd}`;
            account.expires_at = newDate;
            if (expiresInput) expiresInput.value = newDate;
            try {
                const resp = await fetch('/api/account-pool/expires-at', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: account.user_id, expires_at: newDate })
                });
                const data = await resp.json();
                if (data && data.status === 'ok') {
                    showToast(`重置日期已设置为 ${newDate}`, 'success');
                    renderAccountPoolList();
                    renderAccountPoolDetailModal(account);
                }
            } catch (e) {}
        });
    });

    // 9. 嵌入凭据表单
    const credFormContainer = modal.querySelector('#apd-cred-form-container');
    if (credFormContainer) {
        credFormContainer.appendChild(buildAccountCredentialForm(account));
    }

    // 10. 关闭浏览器
    const closeBrowserBtn = modal.querySelector('#apd-close-browser-btn');
    if (closeBrowserBtn) {
        closeBrowserBtn.addEventListener('click', () => closeAccountPoolBrowser(account.user_id, closeBrowserBtn));
    }

    // 11. 删除账号
    const deleteBtn = modal.querySelector('#apd-delete-account-btn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', async () => {
            await deleteAccountPoolAccount(account.user_id);
            closeAccountPoolDetailModal();
        });
    }
}

// 号池不再有自己的弹窗（原 #account-pool-manage-modal 已并入 API 配置中心的
// 「生成号池」分区）：这里只负责打开配置中心并切到那个分区，池子数据由
// switchSettingsSection('pool') 顺带刷新。留给其它入口（快捷键/别处按钮）调用。
function openAccountPoolManageModal() {
    const modal = document.getElementById('settings-modal');
    if (!modal) return;
    if (!modal.classList.contains('active')) {
        const openBtn = document.getElementById('open-settings-btn');
        if (openBtn) openBtn.click();
        else modal.classList.add('active');
    }
    if (typeof switchSettingsSection === 'function') switchSettingsSection('pool');
}

async function exportAccountPoolConfig() {
    const includeCreds = confirm("导出提示：\n是否同时导出明文密码与 2FA TOTP 密钥？\n\n【确定】导出包含账号密码与 2FA 密钥的完整配置\n【取消】仅导出号池环境列表与备注配置（不含明文凭据）");
    try {
        const resp = await fetch(`/api/account-pool/export?include_credentials=${includeCreds ? 1 : 0}`);
        const data = await resp.json();
        const jsonStr = JSON.stringify(data, null, 2);
        const blob = new Blob([jsonStr], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const nowStr = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '_');
        a.href = url;
        a.download = `account_pool_export_${nowStr}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(includeCreds ? '已导出完整号池配置（含凭据与 2FA）' : '已导出号池配置（不含敏感凭据）', 'success');
    } catch (e) {
        showToast(`导出失败: ${e.message || e}`, 'error');
    }
}

async function handleAccountPoolImportFile(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    event.target.value = '';

    try {
        const text = await file.text();
        const json = JSON.parse(text);
        if (!json || !Array.isArray(json.accounts)) {
            showToast('导入失败：文件格式错误，未包含有效的 accounts 数组', 'error');
            return;
        }

        const count = json.accounts.length;
        const hasCreds = json.accounts.some(a => a.email || a.password || a.totp_secret || a.secret);
        const msg = `确认导入该号池配置文件？\n包含账号数：${count} 个\n包含登录凭据与 2FA：${hasCreds ? '是' : '否'}\n\n点击【确定】进行增量导入合并，系统会自动校验 2FA 密钥格式。`;
        if (!confirm(msg)) return;

        const resp = await fetch('/api/account-pool/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(json)
        });
        const result = await resp.json();
        if (result && result.status === 'ok') {
            if (Array.isArray(result.accounts)) {
                accountPoolCache = result.accounts;
            } else {
                await loadAccountPool();
            }
            renderAccountPoolList();
            showToast(`导入成功：新增 ${result.added}，更新 ${result.updated}，更新凭据 ${result.credentials_saved}`, 'success');
        } else {
            showToast(`导入失败：${result.message || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast(`导入失败：JSON 解析错误 (${e.message || e})`, 'error');
    }
}

function selectAllAccountPool() {
    // 只作用于当前可见集合：筛完/搜完再按全选，勾中的就是眼前这些
    const allIds = getVisibleAccountPoolAccounts().map(a => a.user_id);
    const allSelected = allIds.length > 0 && allIds.every(id => accountPoolSelected.has(id));
    if (allSelected) {
        accountPoolSelected.clear();
    } else {
        allIds.forEach(id => accountPoolSelected.add(id));
    }
    renderAccountPoolList();
}

function clearAccountPoolSelection() {
    accountPoolSelected.clear();
    renderAccountPoolList();
}

async function bulkToggleAccountPool(disabled) {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    try {
        const resp = await fetch('/api/account-pool/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: userIds, disabled })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            if (Array.isArray(data.accounts)) {
                const map = new Map(data.accounts.map(a => [a.user_id, a]));
                accountPoolCache.forEach((a, idx) => {
                    if (map.has(a.user_id)) accountPoolCache[idx] = map.get(a.user_id);
                });
            } else {
                await loadAccountPool();
            }
            renderAccountPoolList();
            showToast(`已批量${disabled ? '禁用' : '启用'} ${userIds.length} 个账号`, 'success');
        } else {
            showToast(`批量操作失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量操作失败，请检查本地服务', 'error');
    }
}

async function bulkCloseAccountPoolBrowsers() {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    try {
        const resp = await fetch('/api/account-pool/close-browser', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: userIds })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            showToast(`已向 ${userIds.length} 个账号发送关闭浏览器指令`, 'success');
        } else {
            showToast(`批量关闭失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量关闭失败，请检查本地服务', 'error');
    }
}

async function bulkDeleteAccountPoolAccounts() {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    if (!confirm(`确定要从号池移除选中的 ${userIds.length} 个账号吗？（不会影响 AdsPower 里的浏览器环境本身）`)) return;
    try {
        const resp = await fetch('/api/account-pool/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: userIds })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            accountPoolCache = accountPoolCache.filter(a => !accountPoolSelected.has(a.user_id));
            accountPoolSelected.clear();
            renderAccountPoolList();
            await loadAccountPoolAdspowerProfiles();
            showToast(`已成功从号池移除 ${userIds.length} 个账号`, 'success');
        } else {
            showToast(`批量移除失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量移除失败，请检查本地服务', 'error');
    }
}

async function bulkRefreshAccountPoolCredit(btnEl) {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = `⏳ 探测中 (0/${userIds.length})...`; }
    let successCount = 0;
    let failCount = 0;
    for (let i = 0; i < userIds.length; i++) {
        const uid = userIds[i];
        if (btnEl) btnEl.textContent = `⏳ 探测中 (${i + 1}/${userIds.length})...`;
        try {
            const resp = await fetch('/api/account-pool/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: uid })
            });
            const data = await resp.json();
            if (data && data.account) {
                const idx = accountPoolCache.findIndex(a => a.user_id === uid);
                if (idx >= 0) accountPoolCache[idx] = data.account;
                renderAccountPoolList();
            }
            if (data && data.status === 'ok') {
                successCount++;
            } else {
                failCount++;
            }
        } catch (e) {
            failCount++;
        }
    }
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = originalText; }
    showToast(`批量刷新完成：成功 ${successCount} 个${failCount > 0 ? `，跳过/失败 ${failCount} 个` : ''}`, successCount > 0 ? 'success' : 'warning');
}

async function bulkSetAccountPoolPassword() {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    const pwd = prompt(`批量设置密码：\n请输入要应用到选中的 ${userIds.length} 个账号的统一密码：`, 'Sharpal2025');
    if (pwd === null) return;
    const password = pwd.trim();
    if (!password) {
        showToast('密码不能为空', 'warning');
        return;
    }
    try {
        const resp = await fetch('/api/account-pool/credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: userIds, password: password })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            await loadAccountPool();
            showToast(`已成功为 ${data.count || userIds.length} 个账号更新密码为统一凭据`, 'success');
        } else {
            showToast(`批量设置密码失败：${(data && data.message) || '未知错误'}`, 'error');
        }
    } catch (e) {
        showToast('批量设置密码失败，请检查本地服务', 'error');
    }
}

async function bulkExportAccountPoolSelected() {
    const userIds = Array.from(accountPoolSelected);
    if (!userIds.length) return;
    const includeCreds = confirm(`导出提示：\n是否同时导出所选 ${userIds.length} 个账号的明文密码与 2FA TOTP 密钥？\n\n【确定】导出包含账号密码与 2FA 密钥的完整配置\n【取消】仅导出号池环境列表与备注配置（不含明文凭据）`);
    try {
        const resp = await fetch(`/api/account-pool/export?include_credentials=${includeCreds ? 1 : 0}`);
        const data = await resp.json();
        if (data && Array.isArray(data.accounts)) {
            const selectedSet = new Set(userIds);
            data.accounts = data.accounts.filter(a => selectedSet.has(a.user_id));
        }
        const jsonStr = JSON.stringify(data, null, 2);
        const blob = new Blob([jsonStr], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const nowStr = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '_');
        a.href = url;
        a.download = `account_pool_selected_${userIds.length}_${nowStr}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`已导出 ${userIds.length} 个所选账号配置`, 'success');
    } catch (e) {
        showToast(`导出失败: ${e.message || e}`, 'error');
    }
}

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
        const addBtn = document.getElementById('account-pool-add-btn');
        if (addBtn) addBtn.addEventListener('click', addAccountPoolAccount);
        const importAllBtn = document.getElementById('account-pool-import-all-btn');
        if (importAllBtn) importAllBtn.addEventListener('click', () => importAllAccountPoolProfiles(importAllBtn));
        const addSelect = document.getElementById('account-pool-add-select');
        if (addSelect) addSelect.addEventListener('change', prefillAccountPoolNameFromSelection);

        const exportBtn = document.getElementById('account-pool-export-btn');
        if (exportBtn) exportBtn.addEventListener('click', exportAccountPoolConfig);

        const importBtn = document.getElementById('account-pool-import-btn');
        const importFileInput = document.getElementById('account-pool-import-file-input');
        if (importBtn && importFileInput) {
            importBtn.addEventListener('click', () => importFileInput.click());
            importFileInput.addEventListener('change', handleAccountPoolImportFile);
        }

        // 搜索框：输入即筛，✕ 一键清空，Esc 也当清空用
        const searchInput = document.getElementById('account-pool-search');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                accountPoolSearch = e.target.value || '';
                renderAccountPoolList();
            });
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    e.stopPropagation(); // 别顺手把整个配置中心关了
                    accountPoolSearch = '';
                    searchInput.value = '';
                    renderAccountPoolList();
                }
            });
        }
        const searchClearBtn = document.getElementById('account-pool-search-clear');
        if (searchClearBtn) {
            searchClearBtn.addEventListener('click', () => {
                accountPoolSearch = '';
                if (searchInput) searchInput.value = '';
                renderAccountPoolList();
                if (searchInput) searchInput.focus();
            });
        }

        // 添加/导入导出面板的展开状态记住，别每次进来都要重点一遍
        const addPanel = document.getElementById('account-pool-add-panel');
        if (addPanel) {
            try {
                if (localStorage.getItem('spark_account_pool_add_open') === '1') addPanel.open = true;
            } catch (e) {}
            addPanel.addEventListener('toggle', () => {
                try {
                    localStorage.setItem('spark_account_pool_add_open', addPanel.open ? '1' : '0');
                } catch (e) {}
            });
        }

        const sortSelect = document.getElementById('account-pool-sort-select');
        if (sortSelect) {
            sortSelect.value = accountPoolSortKey;
            sortSelect.addEventListener('change', (e) => {
                accountPoolSortKey = e.target.value;
                renderAccountPoolList();
            });
        }

        const stratSelect = document.getElementById('account-pool-strategy-select');
        if (stratSelect) {
            stratSelect.value = accountPoolStrategy;
            stratSelect.addEventListener('change', async (e) => {
                accountPoolStrategy = e.target.value;
                await saveAccountPoolStrategyConfig();
                showToast(`选号调度策略已切换为：${stratSelect.options[stratSelect.selectedIndex].text}`, 'success');
            });
        }

        // 多选与批量操作按钮绑定
        const selectAllBtn = document.getElementById('account-pool-select-all-btn');
        if (selectAllBtn) selectAllBtn.addEventListener('click', selectAllAccountPool);

        const bulkClearBtn = document.getElementById('account-pool-bulk-clear-btn');
        if (bulkClearBtn) bulkClearBtn.addEventListener('click', clearAccountPoolSelection);

        const bulkPriorityBtn = document.getElementById('account-pool-bulk-priority-btn');
        if (bulkPriorityBtn) bulkPriorityBtn.addEventListener('click', () => bulkSetAccountPoolPriority(true));

        const bulkUnpriorityBtn = document.getElementById('account-pool-bulk-unpriority-btn');
        if (bulkUnpriorityBtn) bulkUnpriorityBtn.addEventListener('click', () => bulkSetAccountPoolPriority(false));

        const bulkExpiresBtn = document.getElementById('account-pool-bulk-expires-btn');
        if (bulkExpiresBtn) bulkExpiresBtn.addEventListener('click', bulkSetAccountPoolExpires);

        const bulkEnableBtn = document.getElementById('account-pool-bulk-enable-btn');
        if (bulkEnableBtn) bulkEnableBtn.addEventListener('click', () => bulkToggleAccountPool(false));

        const bulkDisableBtn = document.getElementById('account-pool-bulk-disable-btn');
        if (bulkDisableBtn) bulkDisableBtn.addEventListener('click', () => bulkToggleAccountPool(true));

        const bulkPasswordBtn = document.getElementById('account-pool-bulk-password-btn');
        if (bulkPasswordBtn) bulkPasswordBtn.addEventListener('click', bulkSetAccountPoolPassword);

        const bulkRefreshBtn = document.getElementById('account-pool-bulk-refresh-btn');
        if (bulkRefreshBtn) bulkRefreshBtn.addEventListener('click', () => bulkRefreshAccountPoolCredit(bulkRefreshBtn));

        const bulkCloseBtn = document.getElementById('account-pool-bulk-close-btn');
        if (bulkCloseBtn) bulkCloseBtn.addEventListener('click', bulkCloseAccountPoolBrowsers);

        const bulkDeleteBtn = document.getElementById('account-pool-bulk-delete-btn');
        if (bulkDeleteBtn) bulkDeleteBtn.addEventListener('click', bulkDeleteAccountPoolAccounts);

        const bulkExportBtn = document.getElementById('account-pool-bulk-export-btn');
        if (bulkExportBtn) bulkExportBtn.addEventListener('click', bulkExportAccountPoolSelected);

        // 视图切换按钮绑定 (列表 / 网格)
        const viewListBtn = document.getElementById('account-pool-view-list-btn');
        if (viewListBtn) viewListBtn.addEventListener('click', () => setAccountPoolViewMode('list'));

        const viewGridBtn = document.getElementById('account-pool-view-grid-btn');
        if (viewGridBtn) viewGridBtn.addEventListener('click', () => setAccountPoolViewMode('grid'));

        updateAccountPoolViewSwitcherUI();
    });
}

// Node 单测导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        setAccountPoolViewMode,
        openAccountPoolDetailModal,
        closeAccountPoolDetailModal,
        renderAccountPoolList,
        sortAccountPoolList,
        getAccountPoolTone,
        getVisibleAccountPoolAccounts,
        accountPoolMatchesFilter,
        accountPoolMatchesSearch,
        computeAccountPoolStats,
        renderAccountPoolStats,
        setAccountPoolFilter,
        buildHealthScoreBadgeElement,
        renderAccountPoolGlobalHUD,
        accountPoolCache,
    };
}
