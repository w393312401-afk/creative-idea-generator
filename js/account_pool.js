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

async function loadAccountPool() {
    try {
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

function renderAccountPoolList() {
    const list = document.getElementById('account-pool-list');
    if (!list) return;
    list.textContent = '';

    if (accountPoolCache.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'trend-refs-empty';
        empty.textContent = '号池还是空的——添加至少一个账号后，帧序列 / 视频生成会自动按额度选号。';
        list.appendChild(empty);
        return;
    }

    const sortedAccounts = sortAccountPoolList(accountPoolCache, accountPoolSortKey);

    sortedAccounts.forEach(account => {
        const row = document.createElement('div');
        row.className = 'account-pool-row' + (account.disabled ? ' disabled' : '');

        const enableLabel = document.createElement('label');
        enableLabel.className = 'account-pool-enable-toggle';
        enableLabel.title = account.disabled ? '已禁用，点击启用' : '已启用，点击禁用';
        const enableChk = document.createElement('input');
        enableChk.type = 'checkbox';
        enableChk.checked = !account.disabled;
        enableChk.addEventListener('change', () => toggleAccountPoolAccount(account.user_id, !enableChk.checked));
        enableLabel.appendChild(enableChk);
        row.appendChild(enableLabel);

        const main = document.createElement('div');
        main.className = 'account-pool-main';

        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.className = 'account-pool-name-input';
        nameInput.value = account.name || '';
        nameInput.placeholder = '账号命名（默认用邮箱地址）';
        nameInput.title = '账号命名——留空会自动回退为该环境的邮箱地址';
        nameInput.addEventListener('change', () => renameAccountPoolAccount(account.user_id, nameInput.value));
        main.appendChild(nameInput);

        const noteInput = document.createElement('input');
        noteInput.type = 'text';
        noteInput.className = 'account-pool-note-input';
        noteInput.value = account.note || '';
        noteInput.placeholder = '备注（选填）';
        noteInput.title = '备注——纯自由文本，跟账号命名互不影响';
        noteInput.addEventListener('change', () => updateAccountPoolNote(account.user_id, noteInput.value));
        main.appendChild(noteInput);

        const meta = document.createElement('div');
        meta.className = 'account-pool-meta';
        let coolBadge = '';
        if (isAccountPoolCoolingDown(account)) {
            // cooldown_reason 由后端 mark_login_required()/mark_exhausted() 写入，
            // 区分"登录失效等人工处理"（几小时短冷却）和"积分耗尽"（24h长冷却），
            // 不然用户看到冷却中会先怀疑是不是又没额度了。
            coolBadge = account.cooldown_reason === 'login_required' ? ' · 🔒 登录失效冷却中' : ' · 🧊 额度冷却中';
        }
        const creditText = account.credit == null ? '未探测'
            : `${account.credit}${account.credit_trustworthy === false ? '（缓存，不用于选号）' : ''}`;
        const probeText = account.last_probe_status === 'failed'
            ? ` · ⚠️ 最近探测失败：${account.last_probe_error || '未知原因'}` : '';
        const serialText = account.serial_number ? `编号: #${account.serial_number} · ` : '';
        const imgCount = account.image_task_count || 0;
        const vidCount = account.video_task_count || 0;
        const totalCount = account.task_count != null ? account.task_count : (imgCount + vidCount);
        const taskText = ` · 任务数: ${totalCount} (🖼️ ${imgCount} / 🎬 ${vidCount})`;
        meta.textContent = `${serialText}user_id: ${account.user_id} · 积分 ${creditText}${taskText} · 上次成功: ${formatAccountPoolTimestamp(account.last_checked_at)}${probeText}${coolBadge}`;
        main.appendChild(meta);

        row.appendChild(main);

        const refreshBtn = document.createElement('button');
        refreshBtn.type = 'button';
        refreshBtn.className = 'account-pool-mini-btn';
        refreshBtn.textContent = '🔎 刷新积分';
        refreshBtn.title = '打开一次浏览器真实探测积分，比较慢';
        refreshBtn.addEventListener('click', () => refreshAccountPoolCredit(account.user_id, refreshBtn));
        row.appendChild(refreshBtn);

        const closeBrowserBtn = document.createElement('button');
        closeBrowserBtn.type = 'button';
        closeBrowserBtn.className = 'account-pool-mini-btn';
        closeBrowserBtn.textContent = '🛑 关闭浏览器';
        closeBrowserBtn.title = '关闭该 AdsPower 浏览器实例';
        closeBrowserBtn.addEventListener('click', () => closeAccountPoolBrowser(account.user_id, closeBrowserBtn));
        row.appendChild(closeBrowserBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'account-pool-mini-btn danger';
        deleteBtn.textContent = '🗑️';
        deleteBtn.title = '从号池移除（不影响 AdsPower 里的浏览器环境本身）';
        deleteBtn.addEventListener('click', () => deleteAccountPoolAccount(account.user_id));
        row.appendChild(deleteBtn);

        list.appendChild(row);
    });
}

async function addAccountPoolAccount() {
    const select = document.getElementById('account-pool-add-select');
    const nameInput = document.getElementById('account-pool-add-name');
    const noteInput = document.getElementById('account-pool-add-note');
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
                note: noteInput ? noteInput.value : ''
            })
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            if (nameInput) nameInput.value = '';
            if (noteInput) noteInput.value = '';
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

document.addEventListener('DOMContentLoaded', () => {
    const addBtn = document.getElementById('account-pool-add-btn');
    if (addBtn) addBtn.addEventListener('click', addAccountPoolAccount);
    const importAllBtn = document.getElementById('account-pool-import-all-btn');
    if (importAllBtn) importAllBtn.addEventListener('click', () => importAllAccountPoolProfiles(importAllBtn));
    const addSelect = document.getElementById('account-pool-add-select');
    if (addSelect) addSelect.addEventListener('change', prefillAccountPoolNameFromSelection);

    const sortSelect = document.getElementById('account-pool-sort-select');
    if (sortSelect) {
        sortSelect.value = accountPoolSortKey;
        sortSelect.addEventListener('change', (e) => {
            accountPoolSortKey = e.target.value;
            renderAccountPoolList();
        });
    }

});
