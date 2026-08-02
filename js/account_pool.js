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
// 当前展开着凭据表单的 user_id（同时只允许一个）。存在这里而不是 DOM 里，是因为
// renderAccountPoolList() 会把整个列表重建一遍（保存凭据后必然触发一次），
// 状态放 DOM 上会在重建时丢失，表现为"一点保存表单就自己收起来了"。
let accountPoolOpenCredentialId = null;

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

        // 只有配过凭据（或熔断了）才多占一行。号池可能有几十个账号，给每一行都挂上
        // 「自动登录：未配置」是纯噪音——没用这个功能的人不需要被提醒 40 次，
        // 「🔑 登录凭据」按钮的高亮状态已经把"配没配"说清楚了。
        const loginText = describeAccountAutoLogin(account);
        if (loginText) {
            const loginMeta = document.createElement('div');
            loginMeta.className = 'account-pool-meta account-pool-login-meta';
            loginMeta.textContent = loginText;
            main.appendChild(loginMeta);
        }

        row.appendChild(main);

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
        row.appendChild(credBtn);

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

        // 凭据表单是行的兄弟节点而不是子节点：.account-pool-row 是
        // display:flex + align-items:center 的单行布局，把一个多行表单塞进去会把
        // 那一行的垂直对齐全部搞乱。
        if (accountPoolOpenCredentialId === account.user_id) {
            list.appendChild(buildAccountCredentialForm(account));
        }
    });
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
    saveBtn.className = 'account-pool-mini-btn';
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
        unblockBtn.className = 'account-pool-mini-btn';
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

    const sortSelect = document.getElementById('account-pool-sort-select');
    if (sortSelect) {
        sortSelect.value = accountPoolSortKey;
        sortSelect.addEventListener('change', (e) => {
            accountPoolSortKey = e.target.value;
            renderAccountPoolList();
        });
    }

});
