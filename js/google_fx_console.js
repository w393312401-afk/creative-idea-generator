(function (global) {
  'use strict';

  const state = {
    initialized: false,
    active: false,
    loading: false,
    timer: null,
    highFreqUntil: 0,
    accounts: [],
    profilesLoaded: false,
    config: null,
    configSchema: null,
    configVersions: [],
    lastTasks: [],
    lastQueue: {},
    proxies: [],
    proxyEditingId: '',
    selftestRunning: false,
    logTaskFilter: '',
    logKeyword: '',
    expandedTask: null,
    accountSortKey: 'serial_asc'
  };

  const $ = (id) => typeof document !== 'undefined' ? document.getElementById(id) : null;
  const esc = (value) => {
    if (typeof global.escapeHtml === 'function') return global.escapeHtml(String(value ?? ''));
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  };

  // 账号状态判定顺序有讲究：禁用 > 登录失效 > 冷却 > 未探测 > 额度耗尽 > 可用。
  // "登录失效"必须排在"冷却"之前单独成一档——后端用 cooldown_reason 区分了
  // "登录失效（2 小时冷却，积分不动）"和"额度耗尽（24 小时冷却，积分清零）"，
  // 前端一律显示"冷却中"的话，用户不知道该去人工重新登录还是等额度恢复。
  // "未探测"也必须和"额度耗尽"分开：credit=null 表示从来没探测成功过，
  // 不是"没额度"，把它显示成 0 或"可用"都是在编造账号健康状态。
  function accountState(account, nowMs = Date.now()) {
    if (account && account.disabled) return { key: 'disabled', label: '已禁用', tone: 'bad' };
    const cooling = account && account.cooldown_until
      && Number.isFinite(Date.parse(account.cooldown_until))
      && Date.parse(account.cooldown_until) > nowMs;
    if (cooling && account.cooldown_reason === 'login_required') {
      return { key: 'login_required', label: '待人工登录', tone: 'bad' };
    }
    if (cooling) return { key: 'cooling', label: '冷却中', tone: 'warn' };
    if (account && account.last_probe_status === 'failed') {
      return { key: 'probe_failed', label: '探测失败', tone: 'bad' };
    }
    if (account && (account.credit === null || account.credit === undefined)) {
      return { key: 'unprobed', label: '未探测', tone: 'warn' };
    }
    if (account && account.credit_stale) return { key: 'stale', label: '缓存过期', tone: 'warn' };
    if (Number(account.credit) <= 0) return { key: 'empty', label: '额度不足', tone: 'bad' };
    return { key: 'ready', label: '可用', tone: 'good' };
  }

  // 代理状态判定：禁用 > 检测失败 > 未检测 > 可用。「未检测」不能显示成"可用"——
  // 一条从没连通过的代理和一条刚验过出口 IP 的代理是两回事（同 accountState 里
  // credit=null 不等于 0 的道理）。
  function proxyState(proxy) {
    if (!proxy) return { key: 'unknown', label: '未知', tone: 'warn' };
    if (proxy.disabled) return { key: 'disabled', label: '已禁用', tone: 'bad' };
    if (proxy.last_check_status === 'failed') return { key: 'failed', label: '检测失败', tone: 'bad' };
    if (proxy.last_check_status !== 'ok') return { key: 'unchecked', label: '未检测', tone: 'warn' };
    return { key: 'ok', label: '可用', tone: 'good' };
  }

  function creditLabel(account) {
    if (!account || account.credit === null || account.credit === undefined) return '未探测';
    if (!Number.isFinite(Number(account.credit))) return '未知';
    return `${account.credit}${account.credit_trustworthy === false ? '（缓存）' : ''}`;
  }

  function taskTypeLabel(type) {
    return ({ frames: '帧序列', videos: '视频序列', staged_render: '分步渲染', auto: '自治管线', stepped: '分步管线' })[type] || 'FX 任务';
  }

  function formatTime(value) {
    if (!value) return '尚未探测';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
    });
  }

  function formatDuration(seconds) {
    // null/undefined 必须走"—"，不能被 Number() 悄悄变成 0——阶段时间线里
    // "还没有耗时数据"和"耗时 0 秒"是两件不同的事。
    if (seconds === null || seconds === undefined || seconds === '') return '—';
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 60) return `${Math.round(value)}s`;
    if (value < 3600) return `${Math.floor(value / 60)}m${String(Math.round(value % 60)).padStart(2, '0')}s`;
    return `${Math.floor(value / 3600)}h${String(Math.floor((value % 3600) / 60)).padStart(2, '0')}m`;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    let data = {};
    try { data = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok || data.status === 'error') {
      throw new Error(data.message || data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function showToast(message, isError) {
    const toast = $('fx-toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = `fx-toast visible${isError ? ' error' : ''}`;
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => { toast.className = 'fx-toast'; }, 3200);
  }

  function setCard(id, title, subtitle, tone) {
    const value = $(`${id}-state`);
    const sub = $(`${id}-sub`);
    const card = value && value.closest('.fx-status-card');
    if (value) value.textContent = title;
    if (sub) sub.textContent = subtitle;
    if (card) card.dataset.tone = tone || 'neutral';
  }

  function renderStatus(data) {
    const runtime = data.runtime || {};
    const adspower = data.adspower || {};
    const execution = data.execution || {};
    const accounts = data.accounts || {};
    const config = data.configuration || {};
    const queue = data.queue || {};
    state.lastQueue = queue;

    setCard('fx-runtime', runtime.available ? '已就绪' : '不可用',
      runtime.available ? '内置包加载成功' : (runtime.error || '运行时导入失败'),
      runtime.available ? 'good' : 'bad');
    setCard('fx-adspower', adspower.online ? '在线' : '离线',
      `${adspower.host || '127.0.0.1'}:${adspower.port || '—'} · ${adspower.latency_ms ?? '—'}ms`,
      adspower.online ? 'good' : 'bad');

    const busy = Boolean(execution.lock_busy);
    const limits = queue.limits || {};
    setCard('fx-execution', busy ? '正在占用' : '空闲',
      `${queue.active_count || 0}/${limits.max_concurrent || 1} 执行 · ${queue.waiting_count || 0} 排队 · ${execution.active_requests || 0} 浏览器请求`,
      busy ? 'warn' : 'good');
    const available = accounts.ready ?? 0;
    const accountSub = [`${accounts.cooling || 0} 冷却`, `${accounts.disabled || 0} 禁用`];
    if (accounts.unprobed) accountSub.push(`${accounts.unprobed} 未探测`);
    if (accounts.login_required) accountSub.push(`${accounts.login_required} 待登录`);
    if (accounts.stale_credit) accountSub.push(`${accounts.stale_credit} 已过期`);
    if (accounts.probe_failed) accountSub.push(`${accounts.probe_failed} 探测失败`);
    setCard('fx-account', `${available} / ${accounts.total || 0}`, accountSub.join(' · '),
      available > 0 ? 'good' : ((accounts.total || 0) ? 'bad' : 'warn'));

    const proxies = data.proxies || {};
    const proxyTotal = proxies.total || 0;
    const proxySub = [`${proxies.ok || 0} 已验证`, `${proxies.unchecked || 0} 未检测`];
    if (proxies.failed) proxySub.push(`${proxies.failed} 失败`);
    if (proxies.disabled) proxySub.push(`${proxies.disabled} 禁用`);
    // 代理号池为空 = 走 AdsPower 环境自带的代理设置，是合法状态，不该报红。
    setCard('fx-proxy', proxyTotal ? `${proxies.enabled || 0} / ${proxyTotal}` : '未配置',
      proxyTotal ? proxySub.join(' · ') : '生成走 AdsPower 环境自带的代理设置',
      proxyTotal ? ((proxies.enabled || 0) > 0 ? 'good' : 'bad') : 'neutral');

    if ($('fx-config-image-model')) $('fx-config-image-model').textContent = config.image_model || '—';
    if ($('fx-config-video-model')) $('fx-config-video-model').textContent = config.video_model || '—';
    const refModeEl = $('fx-config-video-ref-mode');
    if (refModeEl) {
      const refModeMap = { 'VIDEO_FRAMES': '帧（首尾帧）', 'VIDEO_REFERENCES': '素材' };
      refModeEl.textContent = refModeMap[config.video_ref_mode] || config.video_ref_mode || '—';
    }
    // 锁定默认环境时轮转环只剩一个号，节拍值一次都用不上。状态条以前照样把它
    // 显示成正在生效的样子，用户只能靠翻手册才知道自己改的数字是死的。
    const switchEl = $('fx-config-switch');
    if (switchEl) {
      const switchInert = config.account_switch_effective === false;
      switchEl.textContent = `每 ${config.account_switch_requests || 5} 次请求`
        + (switchInert ? ' · 不生效' : '');
      switchEl.className = switchInert ? 'fx-danger-value' : '';
      switchEl.title = switchInert
        ? '「锁定默认环境」开着，整条序列固定在同一个号上，这个节拍不会被用到；取消锁定后才按节拍轮转号池'
        : '每这么多个请求换一个号池账号（IP 始终不变）';
    }
    if ($('fx-config-account')) $('fx-config-account').textContent = config.selected_user_id || '自动选择';
    const sequenceEl = $('fx-config-sequence-account');
    if (sequenceEl) {
      const seqId = config.sequence_user_id || '';
      const seqAccount = seqId && state.accounts.find((a) => String(a.user_id) === String(seqId));
      sequenceEl.textContent = seqId
        ? `${(seqAccount && (seqAccount.name || seqAccount.user_id)) || seqId}${config.sequence_user_locked ? ' · 已锁定' : ''}`
        : '自动选择';
      sequenceEl.title = seqId
        ? `序列生成默认浏览器环境：${seqId}${config.sequence_user_locked ? '（整条序列不换号）' : '（后续仍按换号节拍轮转）'}`
        : '未指定：按号池自动选号';
    }
    if ($('fx-sync-time')) $('fx-sync-time').textContent = `同步于 ${formatTime(data.checked_at)}`;

    // IP 轮换如实反映后端读到的真实配置，不再是写死的"已关闭"
    const rotationEl = $('fx-config-rotation');
    if (rotationEl) {
      const rotation = config.ip_rotation || {};
      rotationEl.textContent = config.ip_rotation_enabled
        ? `生效中（阈值 ${rotation.threshold}）`
        : (rotation.configured ? '已配置但不生效' : '未配置');
      rotationEl.className = config.ip_rotation_enabled ? 'fx-danger-value' : 'fx-safe-value';
    }
    const dryRunEl = $('fx-config-dryrun');
    if (dryRunEl) {
      dryRunEl.textContent = config.dry_run ? 'Dry-run 开启（不会真的生成）' : '正常提交';
      dryRunEl.className = config.dry_run ? 'fx-danger-value' : 'fx-safe-value';
    }

    const mode = queue.mode || 'running';
    const modeLabel = { running: '运行中', paused: '已暂停', draining: '排空中' }[mode] || mode;
    const modeBadge = $('fx-service-mode');
    if (modeBadge) {
      modeBadge.textContent = modeLabel;
      modeBadge.className = `fx-lock-badge fx-badge-${mode === 'running' ? 'good' : 'warn'}`;
    }
    if ($('fx-control-pause')) $('fx-control-pause').disabled = mode === 'paused';
    if ($('fx-control-drain')) $('fx-control-drain').disabled = mode === 'draining';
    if ($('fx-control-resume')) $('fx-control-resume').disabled = mode === 'running';

    const lockBadge = $('fx-lock-badge');
    if (lockBadge) {
      lockBadge.textContent = busy ? '浏览器锁：占用中' : '浏览器锁：空闲';
      lockBadge.className = `fx-lock-badge fx-badge-${busy ? 'warn' : 'good'}`;
    }

    renderLimits(limits);
    renderSlotDisplay(queue);
    renderOrphaned(queue.orphaned || []);
    renderDiagnostics(data.diagnostics || [], data.selectors || {});
    renderTasks(data.tasks || []);
  }

  function renderSlotDisplay(queue) {
    const container = $('fx-slot-display');
    if (!container) return;

    const activeList = queue.active_list || (queue.active ? [queue.active] : []);
    const waitingList = queue.waiting || [];
    const limits = queue.limits || {};

    let html = '';

    if (activeList.length > 0) {
      html += activeList.map((active) => {
        const id = esc(active.task_id);
        const kind = esc(active.kind || 'fx');
        const elapsed = active.elapsed_seconds ?? (Math.round((Date.now() / 1000 - active.started_at) * 10) / 10);
        const overdue = Boolean(active.overdue);
        const accountPin = active.account_pin ? `📌 绑定账号 ${esc(active.account_pin)}` : '';
        const cardClass = overdue ? 'fx-slot-card is-active is-overdue' : 'fx-slot-card is-active';

        const kindLabelMap = {
          'credit_probe': '账号积分探针 (credit_probe)',
          'selector_probe': '选择器探针 (selector_probe)',
          'selftest': '环境自检 (selftest)',
          'auto_login': '账号自动登录 (auto_login)',
          'frames': '图片/帧序列生成 (frames)',
          'videos': '视频生成 (videos)',
        };
        const kindText = kindLabelMap[kind] || `底层任务 (${kind})`;

        return `
          <div class="${cardClass}">
            <div class="fx-slot-info">
              <div class="fx-slot-title">
                <span class="fx-badge fx-badge-warn">⚡ 临界区占用中</span>
                <strong>${kindText}</strong>
                ${overdue ? '<span class="fx-badge fx-badge-bad">⚠️ 超时</span>' : ''}
              </div>
              <div class="fx-slot-meta">
                任务 ID: <code>${id}</code> · 已运行: <strong>${elapsed}s</strong> (上限 ${limits.task_timeout_seconds || 300}s) ${accountPin}
              </div>
            </div>
            <div class="fx-slot-actions">
              <button class="fx-button" data-slot-action="view_logs" data-task-id="${id}">📜 查看实时日志</button>
              <button class="fx-button fx-button-danger" data-slot-action="force_release" data-task-id="${id}">🚨 强行释放此占用</button>
            </div>
          </div>
        `;
      }).join('');
    } else {
      html += `
        <div class="fx-slot-card is-idle">
          <div class="fx-slot-info">
            <div class="fx-slot-title">
              <span class="fx-badge fx-badge-good">🟢 核心槽位空闲中</span>
              <span>当前无任何底层任务占用 Google FX 浏览器槽位</span>
            </div>
            <div class="fx-slot-meta">
              并发度: ${limits.max_concurrent || 1} · 单任务上限: ${limits.task_timeout_seconds || 300}s · 随时可正常接收新任务
            </div>
          </div>
        </div>
      `;
    }

    if (waitingList.length > 0) {
      html += `
        <div class="fx-panel-subheader" style="margin-top: 10px;">
          <h5>等待排队中的底层任务 (${waitingList.length})</h5>
        </div>
        <table class="fx-table">
          <thead>
            <tr><th>任务 ID</th><th>类型</th><th>等待时间</th><th>优先级</th><th>定向账号</th><th>操作</th></tr>
          </thead>
          <tbody>
            ${waitingList.map((row) => `
              <tr>
                <td><code>${esc(row.task_id)}</code></td>
                <td>${esc(row.kind)}</td>
                <td>${row.wait_seconds || 0}s</td>
                <td>P${row.priority || 0}</td>
                <td>${esc(row.account_pin || '未绑定')}</td>
                <td>
                  <button class="fx-button" data-slot-action="priority" data-delta="1" data-task-id="${esc(row.task_id)}">↑</button>
                  <button class="fx-button" data-slot-action="priority" data-delta="-1" data-task-id="${esc(row.task_id)}">↓</button>
                  <button class="fx-button fx-button-danger" data-slot-action="cancel" data-task-id="${esc(row.task_id)}">取消排队</button>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    container.innerHTML = html;
  }

  function renderLimits(limits) {
    if ($('fx-limit-concurrent') && document.activeElement !== $('fx-limit-concurrent')) {
      $('fx-limit-concurrent').value = limits.max_concurrent ?? 1;
    }
    if ($('fx-limit-exec-timeout') && document.activeElement !== $('fx-limit-exec-timeout')) {
      $('fx-limit-exec-timeout').value = limits.task_timeout_seconds ?? 0;
    }
    if ($('fx-limit-wait-timeout') && document.activeElement !== $('fx-limit-wait-timeout')) {
      $('fx-limit-wait-timeout').value = limits.queue_wait_timeout_seconds ?? 0;
    }
    const kinds = limits.kind_limits || {};
    const note = $('fx-limit-note');
    if (note) {
      const pairs = Object.keys(kinds).map((kind) => `${kind}:${kinds[kind]}`);
      note.textContent = pairs.length ? `分类配额 ${pairs.join('、')}` : '未设置分类配额';
    }
  }

  function renderOrphaned(rows) {
    const box = $('fx-orphaned');
    if (!box) return;
    if (!rows.length) {
      box.innerHTML = '';
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.innerHTML = `<div class="fx-orphan-title">上次退出时仍在队列中的任务（${rows.length}）</div>`
      + rows.map((row) => `<div class="fx-orphan-row">${esc(row.task_id)} · ${esc(row.kind || '')} · ${esc(row.was || '')}</div>`).join('')
      + '<button class="fx-button" data-fx-action="clear-orphaned" type="button">知道了，清除记录</button>';
  }

  function renderDiagnostics(items, selectors) {
    const list = $('fx-diagnostic-list');
    if (list) {
      const rows = items.length ? items : [{ level: 'ok', message: '未发现需要处理的问题' }];
      list.innerHTML = rows.map((item) =>
        `<div class="fx-diagnostic" data-level="${esc(item.level || 'warn')}">${esc(item.message)}</div>`
      ).join('');
    }
    if ($('fx-selector-count')) {
      $('fx-selector-count').textContent = `${selectors.families || 0} 组已记录`
        + (selectors.version ? ` · ${selectors.version}` : '');
    }
    const warnings = $('fx-selector-warnings');
    if (warnings) {
      const rows = selectors.warnings || [];
      warnings.innerHTML = rows.length
        ? rows.map((row) => `<div>${esc(row.family)} · 主选择器 ${Math.round((row.primary_ratio || 0) * 100)}% · miss ${row.miss || 0}</div>`).join('')
        : '<div>暂无选择器漂移信号</div>';
    }
  }

  function sortAccounts(accounts, sortKey) {
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
        case 'tasks_asc':
          return totalA - totalB;
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

  function renderAccounts(accounts) {
    state.accounts = accounts;
    const body = $('fx-account-table-body');
    if (!body) return;

    if ($('fx-account-sort-select')) {
      $('fx-account-sort-select').value = state.accountSortKey;
    }
    const thCredit = $('fx-th-sort-credit');
    if (thCredit) {
      thCredit.textContent = (state.accountSortKey === 'credit_desc') ? '↓' : (state.accountSortKey === 'credit_asc' ? '↑' : '↕');
    }
    const thTasks = $('fx-th-sort-tasks');
    if (thTasks) {
      thTasks.textContent = (state.accountSortKey === 'tasks_desc') ? '↓' : (state.accountSortKey === 'tasks_asc' ? '↑' : '↕');
    }

    if (!accounts.length) {
      body.innerHTML = '<tr><td colspan="6" class="fx-empty">号池为空，可从右上角导入 AdsPower 环境。</td></tr>';
      return;
    }

    const sortedAccounts = sortAccounts(accounts, state.accountSortKey);

    body.innerHTML = sortedAccounts.map((account) => {
      const status = accountState(account);
      const uid = esc(account.user_id);
      const note = account.note ? `<span class="fx-account-id">${esc(account.note)}</span>` : '';
      const cooldownButton = (status.key === 'cooling' || status.key === 'login_required')
        ? `<button class="fx-button" data-action="clear-cooldown" data-user-id="${uid}">解除冷却</button>` : '';
      const imgCount = account.image_task_count || 0;
      const vidCount = account.video_task_count || 0;
      const totalCount = account.task_count != null ? account.task_count : (imgCount + vidCount);
      const taskBadge = `<span class="fx-account-id" style="font-weight:600; color:var(--text-main, #333);">${totalCount}</span> <span class="fx-account-id" style="font-size:11px; color:var(--text-muted, #888);">(图${imgCount}/视${vidCount})</span>`;

      return `<tr>
        <td>${account.serial_number ? `<span class="fx-account-id" style="color:var(--text-muted, #888); font-weight:600; margin-right:4px;">[#${esc(account.serial_number)}]</span>` : ''}<span class="fx-account-name">${esc(account.name || account.user_id)}</span><span class="fx-account-id">${uid}</span>${note}</td>
        <td>${esc(creditLabel(account))}</td>
        <td>${taskBadge}</td>
        <td><span class="fx-badge fx-badge-${status.tone}" title="${esc(account.last_probe_error || account.cooldown_reason || '')}">${status.label}</span></td>
        <td title="${esc(account.last_probe_error || '')}">${esc(formatTime(account.last_probe_at || account.last_checked_at))}</td>
        <td><div class="fx-row-actions">
          <button class="fx-button" data-action="refresh" data-user-id="${uid}">刷新积分</button>
          <button class="fx-button" data-action="close-browser" data-user-id="${uid}">关闭</button>
          ${cooldownButton}
          <button class="fx-button" data-action="toggle" data-user-id="${uid}">${account.disabled ? '启用' : '禁用'}</button>
          <button class="fx-button" data-action="edit" data-user-id="${uid}">编辑</button>
          <button class="fx-button fx-button-danger" data-action="delete" data-user-id="${uid}">移除</button>
        </div></td>
      </tr>`;
    }).join('');
  }

  function renderProxies(proxies) {
    state.proxies = Array.isArray(proxies) ? proxies : [];
    const body = $('fx-proxy-table-body');
    if (!body) return;
    if (!state.proxies.length) {
      body.innerHTML = '<tr><td colspan="6" class="fx-empty">'
        + '代理号池为空：生成任务走 AdsPower 环境自带的代理设置。点右上角「添加代理」录入出口。'
        + '</td></tr>';
      return;
    }
    body.innerHTML = state.proxies.map((proxy) => {
      const status = proxyState(proxy);
      const id = esc(proxy.proxy_id);
      const auth = proxy.user
        ? `${esc(proxy.user)}${proxy.has_password ? ':***' : ''}@` : '';
      const note = proxy.note ? `<span class="fx-account-id">${esc(proxy.note)}</span>` : '';
      const latency = proxy.latency_ms != null ? ` · ${esc(proxy.latency_ms)}ms` : '';
      const exit = proxy.exit_ip
        ? `${esc(proxy.exit_ip)}<span class="fx-account-id">${esc(proxy.exit_location || '')}${latency}</span>`
        : '—';
      const bound = proxy.bound_user_id
        ? `${esc(proxy.bound_user_id)}<span class="fx-account-id">${esc(formatTime(proxy.applied_at))} 下发</span>`
        : '未下发';
      return `<tr>
        <td><span class="fx-account-name">${esc(proxy.label || proxy.endpoint)}</span>
          <span class="fx-proxy-endpoint">${esc(proxy.proxy_type)}://${auth}${esc(proxy.endpoint)}</span>${note}</td>
        <td>${exit}</td>
        <td><span class="fx-badge fx-badge-${status.tone}" title="${esc(proxy.last_check_error || '')}">${status.label}</span></td>
        <td>${bound}</td>
        <td title="${esc(proxy.last_check_error || '')}">${esc(proxy.last_check_at ? formatTime(proxy.last_check_at) : '尚未检测')}</td>
        <td><div class="fx-row-actions">
          <button class="fx-button" data-proxy-action="check" data-proxy-id="${id}">检测</button>
          <button class="fx-button" data-proxy-action="apply" data-proxy-id="${id}" title="把这条代理写进某个 AdsPower 环境">下发</button>
          <button class="fx-button" data-proxy-action="toggle" data-proxy-id="${id}">${proxy.disabled ? '启用' : '禁用'}</button>
          <button class="fx-button" data-proxy-action="edit" data-proxy-id="${id}">编辑</button>
          <button class="fx-button fx-button-danger" data-proxy-action="delete" data-proxy-id="${id}">移除</button>
        </div></td>
      </tr>`;
    }).join('');
  }

  async function loadProxies() {
    try {
      const data = await api('/api/proxy-pool');
      renderProxies(data.proxies || []);
    } catch (error) {
      const body = $('fx-proxy-table-body');
      if (body) body.innerHTML = `<tr><td colspan="6" class="fx-empty">代理号池读取失败：${esc(error.message)}</td></tr>`;
    }
  }

  function proxyFormFields() {
    return {
      type: $('fx-proxy-type'), host: $('fx-proxy-host'), port: $('fx-proxy-port'),
      user: $('fx-proxy-user'), password: $('fx-proxy-password'),
      label: $('fx-proxy-label'), note: $('fx-proxy-note')
    };
  }

  function openProxyForm(proxy) {
    const form = $('fx-proxy-form');
    if (!form) return;
    const fields = proxyFormFields();
    state.proxyEditingId = proxy ? String(proxy.proxy_id) : '';
    if (fields.type) fields.type.value = (proxy && proxy.proxy_type) || 'http';
    if (fields.host) fields.host.value = (proxy && proxy.host) || '';
    if (fields.port) fields.port.value = (proxy && proxy.port) || '';
    if (fields.user) fields.user.value = (proxy && proxy.user) || '';
    // 密码从不回传明文，编辑时留空即表示"沿用原密码"（后端 keep_password）。
    if (fields.password) {
      fields.password.value = '';
      fields.password.placeholder = (proxy && proxy.has_password) ? '留空 = 沿用原密码' : '';
    }
    if (fields.label) fields.label.value = (proxy && proxy.label) || '';
    if (fields.note) fields.note.value = (proxy && proxy.note) || '';
    const mode = $('fx-proxy-form-mode');
    if (mode) mode.textContent = proxy ? `编辑：${proxy.label || proxy.endpoint}` : '新增代理';
    form.hidden = false;
    if (fields.host) fields.host.focus();
  }

  function closeProxyForm() {
    state.proxyEditingId = '';
    const form = $('fx-proxy-form');
    if (form) form.hidden = true;
  }

  async function saveProxy() {
    const fields = proxyFormFields();
    const body = {
      proxy_id: state.proxyEditingId || '',
      proxy_type: fields.type ? fields.type.value : 'http',
      host: fields.host ? fields.host.value.trim() : '',
      port: fields.port ? fields.port.value.trim() : '',
      user: fields.user ? fields.user.value.trim() : '',
      password: fields.password ? fields.password.value : '',
      label: fields.label ? fields.label.value.trim() : '',
      note: fields.note ? fields.note.value.trim() : ''
    };
    if (!body.host || !body.port) { showToast('请填写代理地址和端口', true); return; }
    await post('/api/proxy-pool', body);
    closeProxyForm();
    await Promise.all([loadProxies(), refresh(true)]);
    showToast(body.proxy_id ? '代理已更新' : '代理已加入号池');
  }

  async function handleProxyAction(button) {
    const action = button.dataset.proxyAction;
    const proxyId = button.dataset.proxyId;
    const proxy = state.proxies.find((row) => String(row.proxy_id) === proxyId);
    if (!proxy) return;

    if (action === 'edit') { openProxyForm(proxy); return; }
    if (action === 'delete'
      && !global.confirm(`从代理号池移除「${proxy.label || proxy.endpoint}」？\n已下发到 AdsPower 环境的代理配置不会被回滚。`)) return;

    let path;
    const body = { proxy_id: proxyId };
    if (action === 'check') path = '/api/proxy-pool/check';
    if (action === 'toggle') { path = '/api/proxy-pool/toggle'; body.disabled = !proxy.disabled; }
    if (action === 'delete') path = '/api/proxy-pool/delete';
    if (action === 'apply') {
      const options = state.accounts.map((a) => `${a.user_id} (${a.name || '未命名'})`).join('\n');
      const value = global.prompt(
        '把这条代理下发到哪个 AdsPower 环境？（填 user_id）\n'
        + '注意：AdsPower 在浏览器启动时读代理，已经开着的窗口要关掉重开才换出口。\n\n'
        + `号池里的环境：\n${options || '（号池为空）'}`,
        proxy.bound_user_id || '');
      if (value === null) return;
      const userId = value.trim().split(' ')[0];
      if (!userId) { showToast('请填写要下发到的环境 user_id', true); return; }
      path = '/api/proxy-pool/apply';
      body.user_id = userId;
    }
    if (!path) return;

    const original = button.textContent;
    button.disabled = true;
    if (action === 'check') button.textContent = '检测中…';
    try {
      const res = await post(path, body);
      await Promise.all([loadProxies(), refresh(true)]);
      if (action === 'check') {
        showToast(`代理可用，出口 IP ${(res.proxy || {}).exit_ip || '未知'}`);
      } else if (action === 'apply') {
        showToast(res.message || '代理已下发');
      } else {
        showToast('代理号池已更新');
      }
    } catch (error) {
      if (action === 'check') await loadProxies();  // 失败原因已写进池子，重新拉一次展示
      showToast(`操作失败：${error.message}`, true);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function renderTimeline(task) {
    const rows = task.timeline || [];
    if (!rows.length) return '<div class="fx-empty">暂无阶段记录</div>';
    return '<div class="fx-timeline">' + rows.map((row) => `
      <div class="fx-timeline-row${row.slow ? ' slow' : ''}">
        <span class="fx-timeline-stage">${esc(row.stage)}${row.count > 1 ? ` ×${row.count}` : ''}</span>
        <span class="fx-timeline-duration">${esc(formatDuration(row.duration_seconds))}</span>
        <span class="fx-timeline-message">${esc(row.message || '')}</span>
      </div>`).join('') + '</div>';
  }

  function renderTasks(tasks) {
    state.lastTasks = tasks;
    const body = $('fx-task-table-body');
    if (!body) return;
    if (!tasks.length) {
      body.innerHTML = '<tr><td colspan="7" class="fx-empty">当前没有 FX 任务记录。</td></tr>';
      return;
    }
    body.innerHTML = tasks.map((task) => {
      const statusTone = task.status === 'running' ? 'warn' : (task.status === 'completed' ? 'good' : 'bad');
      const id = esc(task.id);
      const waiting = task.queue_state === 'waiting';
      const priorityActions = waiting
        ? `<button class="fx-button" data-task-action="priority" data-delta="1" data-task-id="${id}" title="提高优先级">↑</button>`
          + `<button class="fx-button" data-task-action="priority" data-delta="-1" data-task-id="${id}" title="降低优先级">↓</button>`
          + `<button class="fx-button" data-task-action="pin" data-task-id="${id}" title="指定这个任务用哪个账号">定向账号</button>`
        : '';
      const logBtn = `<button class="fx-button" data-task-action="view_logs" data-task-id="${id}" title="查看该任务的专属日志">📜 日志</button>`;
      const action = task.status === 'running'
        ? `${priorityActions}${logBtn}<button class="fx-button fx-button-danger" data-task-action="cancel" data-task-id="${id}">取消</button>`
        : logBtn;
      const queueLabel = task.queue_state === 'active'
        ? `执行中 ${formatDuration(task.elapsed_seconds)}${task.overdue ? ' ⚠超时' : ''}`
        : (waiting ? `第 ${task.queue_position} 位 · P${task.priority || 0}` : '—');
      const stuck = task.stuck_stage
        ? `<div class="fx-task-flag warn">阶段 ${esc(task.stuck_stage.stage)} 已 ${esc(formatDuration(task.stuck_stage.duration_seconds))}</div>` : '';
      const manual = task.manual_intervention ? `
        <div class="fx-task-flag bad">
          🛑 等待人工处理（${esc(task.manual_intervention.code || '未知')}）
          ${task.manual_intervention.remaining_seconds ? `· 剩余 ${esc(formatDuration(task.manual_intervention.remaining_seconds))}` : ''}
          <span class="fx-manual-hint">请到 AdsPower 浏览器窗口完成登录/验证${task.manual_intervention.account ? `（账号 ${esc(task.manual_intervention.account)}）` : ''}</span>
        </div>` : '';
      const pin = task.account_pin ? `<span class="fx-pin">📌 ${esc(task.account_pin)}</span>` : '';
      const expanded = state.expandedTask === task.id;
      const detail = expanded
        ? `<tr class="fx-task-detail"><td colspan="7">${renderTimeline(task)}</td></tr>` : '';
      return `<tr data-task-row="${id}">
        <td><span class="fx-account-name">${esc(task.theme || '未命名任务')}</span><span class="fx-task-id">${id}</span>${manual}${stuck}</td>
        <td>${esc(taskTypeLabel(task.type))}</td><td>${esc(queueLabel)}</td>
        <td><button class="fx-link" data-task-action="timeline" data-task-id="${id}">${esc(task.stage || '—')} ${expanded ? '▾' : '▸'}</button></td>
        <td>${esc(task.account || '自动')}${pin}</td>
        <td><span class="fx-badge fx-badge-${statusTone}" title="${esc(task.error || '')}">${esc(task.status || 'unknown')}</span></td>
        <td>${action}</td>
      </tr>${detail}`;
    }).join('');
  }

  // renderAccounts 之后必须补一次：配置表单和号池是两条独立的加载链（loadConfig /
  // refresh），先到的那条渲染时另一条的数据可能还没回来。
  function renderAccountsAndSyncConfig(accounts) {
    renderAccounts(accounts);
    syncAccountConfigFields();
  }

  async function refresh(silent) {
    if (state.loading) return;
    state.loading = true;
    const button = $('fx-refresh-status');
    if (button) { button.disabled = true; button.textContent = '同步中…'; }
    try {
      const [status, pool] = await Promise.all([
        api('/api/google-fx/status'),
        api('/api/account-pool')
      ]);
      // 账号先渲染：renderStatus 里的「序列默认环境」要拿号池的命名来显示
      renderAccountsAndSyncConfig(pool.accounts || []);
      renderStatus(status);
      if (!silent) showToast('Google FX 状态已刷新');
    } catch (error) {
      setCard('fx-runtime', '读取失败', error.message, 'bad');
      if (!silent) showToast(`刷新失败：${error.message}`, true);
    } finally {
      state.loading = false;
      if (button) { button.disabled = false; button.textContent = '刷新状态'; }
    }
  }

  // ── 运行配置（按 schema 分组渲染，如实标注需重启的字段）──────────────────

  // 'account' 型配置（如序列生成默认浏览器环境）的候选来自号池，不是 schema 里的
  // 固定 options——号池是会变的运行时数据。已存的值若已不在池子里也要留在列表里
  // 并标出来，否则一次保存就会把用户配的环境静默清空。
  function accountOptionsHtml(selected) {
    const current = String(selected ?? '');
    const rows = (state.accounts || []).map((account) => {
      const uid = String(account.user_id);
      const serial = account.serial_number ? `[#${esc(account.serial_number)}] ` : '';
      return `<option value="${esc(uid)}"${uid === current ? ' selected' : ''}>`
        + `${serial}${esc(account.name || uid)}</option>`;
    });
    if (current && !(state.accounts || []).some((a) => String(a.user_id) === current)) {
      rows.push(`<option value="${esc(current)}" selected>${esc(current)}（已不在号池）</option>`);
    }
    return `<option value=""${current ? '' : ' selected'}>自动（按号池选号）</option>` + rows.join('');
  }

  function syncAccountConfigFields() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('select[data-config-account]').forEach((select) => {
      if (document.activeElement === select) return;  // 用户正在选，别把它重建掉
      select.innerHTML = accountOptionsHtml(select.value);
    });
  }

  function configFieldHtml(key, spec, value) {
    const label = `${esc(spec.label || key)}${spec.hot ? '' : ' <span class="fx-restart-tag">需重启</span>'}`;
    if (spec.type === 'account') {
      return `<label class="fx-config-field"><span>${label}</span>
        <select data-config-key="${esc(key)}" data-config-account="1">${accountOptionsHtml(value)}</select></label>`;
    }
    if (spec.type === 'bool') {
      return `<label class="fx-config-field"><span>${label}</span>
        <input type="checkbox" data-config-key="${esc(key)}" ${value ? 'checked' : ''}></label>`;
    }
    if (spec.type === 'enum') {
      const options = (spec.options || []).map((option) =>
        `<option value="${esc(option)}"${String(option) === String(value ?? '') ? ' selected' : ''}>${esc(option || '不指定')}</option>`
      ).join('');
      return `<label class="fx-config-field"><span>${label}</span>
        <select data-config-key="${esc(key)}">${options}</select></label>`;
    }
    return `<label class="fx-config-field"><span>${label}</span>
      <input type="number" data-config-key="${esc(key)}" min="${esc(spec.min ?? 0)}" max="${esc(spec.max ?? 999999999)}" value="${esc(value ?? spec.default)}"></label>`;
  }

  function renderConfig(data) {
    state.config = data.config || {};
    state.configSchema = data.schema || {};
    state.configVersions = data.versions || [];

    const host = $('fx-config-form');
    if (host) {
      const groups = {};
      Object.keys(state.configSchema).forEach((key) => {
        const spec = state.configSchema[key];
        const group = spec.group || '其它';
        (groups[group] = groups[group] || []).push([key, spec]);
      });
      host.innerHTML = Object.keys(groups).map((group) => `
        <fieldset class="fx-config-group">
          <legend>${esc(group)}</legend>
          ${groups[group].map(([key, spec]) => configFieldHtml(key, spec, state.config[key])).join('')}
        </fieldset>`).join('');
    }

    const versions = $('fx-config-versions');
    if (versions) {
      versions.innerHTML = state.configVersions.length
        ? state.configVersions.map((row) => `<option value="${esc(row.id)}">${esc(formatTime(row.at))} · ${esc(row.action)}${row.note ? ` · ${esc(row.note)}` : ''}</option>`).join('')
        : '<option value="">暂无版本记录</option>';
    }
    const last = (data.audit || [])[0];
    if ($('fx-config-note')) {
      $('fx-config-note').textContent = last
        ? `最近修改：${formatTime(last.at)} · ${last.action} · 共 ${state.configVersions.length} 个可回滚版本`
        : `尚无配置修改记录（共 ${state.configVersions.length} 个版本）`;
    }
  }

  async function loadConfig() {
    try { renderConfig(await api('/api/google-fx/config')); }
    catch (error) { showToast(`配置读取失败：${error.message}`, true); }
  }

  function collectConfigPatch() {
    const patch = {};
    (document.querySelectorAll('[data-config-key]') || []).forEach((el) => {
      const key = el.dataset.configKey;
      const spec = (state.configSchema || {})[key];
      if (!spec) return;
      if (spec.type === 'bool') patch[key] = el.checked;
      else if (spec.type === 'integer') patch[key] = Number(el.value);
      else patch[key] = el.value;
    });
    return patch;
  }

  // 把 FX 控制台保存的模型设置同步到主界面的 localStorage（spark_config），
  // 避免主界面仍然发送旧模型覆盖服务端的最新选择。两个页面（index.html /
  // console.html）共享 localStorage，主页面刷新或跨标签页 storage 事件都会
  // 自动加载新值。
  const _FX_MODEL_SYNC_KEYS = ['videoModel', 'googleFxImageModel', 'videoDuration', 'videoRefMode'];

  function syncFxModelToMainConfig(serverConfig) {
    if (!serverConfig) return;
    try {
      const raw = localStorage.getItem('spark_config');
      if (!raw) return;
      const mainConfig = JSON.parse(raw);
      let dirty = false;
      for (const key of _FX_MODEL_SYNC_KEYS) {
        if (key in serverConfig && mainConfig[key] !== serverConfig[key]) {
          mainConfig[key] = serverConfig[key];
          dirty = true;
        }
      }
      if (dirty) {
        localStorage.setItem('spark_config', JSON.stringify(mainConfig));
      }
    } catch (e) {
      // localStorage 读写失败不阻塞控制台保存
    }
  }

  async function saveConfig() {
    const result = await post('/api/google-fx/config', { patch: collectConfigPatch() });
    renderConfig({
      config: result.config, schema: state.configSchema,
      versions: result.versions, audit: []
    });
    await refresh(true);
    // 同步模型设置到主界面的 localStorage
    syncFxModelToMainConfig(result.config);
    // 被别的配置项压住的字段排在"需重启"前面报：重启能解决，互相打架不能。
    if ((result.inert || []).length) {
      showToast(`已保存，但当前配置下跑不起来 —— ${result.inert.join('；')}`, true);
      return;
    }
    if ((result.restart_required || []).length) {
      showToast(`已保存，但这些字段要重启服务才生效：${result.restart_required.join('、')}`, true);
      return;
    }
    if (!Object.keys(result.changed || {}).length) {
      showToast('配置没有变化');
      return;
    }
    showToast('FX 配置已保存并热生效');
  }

  // ── 调试：自检 / 选择器探针 / 现场 / 日志 ──────────────────────────────────

  function renderSelftest(outcome) {
    const host = $('fx-selftest-result');
    if (!host) return;
    const badge = outcome.status === 'ok' ? 'good' : 'bad';
    host.innerHTML = `
      <div class="fx-selftest-head">
        <span class="fx-badge fx-badge-${badge}">L${outcome.level} ${outcome.status === 'ok' ? '通过' : '失败'}</span>
        <span>总耗时 ${esc(formatDuration((outcome.total_ms || 0) / 1000))}</span>
        ${outcome.failed_step ? `<span class="fx-selftest-failed">失败于：${esc(outcome.failed_step)}</span>` : ''}
      </div>
      <div class="fx-selftest-steps">${(outcome.steps || []).map((step) => `
        <div class="fx-selftest-step" data-status="${esc(step.status)}">
          <span class="fx-step-name">${esc(step.step)}</span>
          <span class="fx-step-ms">${step.ms ? esc(step.ms) + 'ms' : ''}</span>
          <span class="fx-step-detail">${esc(typeof step.detail === 'object' ? JSON.stringify(step.detail) : (step.detail || ''))}</span>
        </div>`).join('')}</div>`;
  }

  async function runSelftest(level) {
    if (state.selftestRunning) { showToast('自检正在进行中', true); return; }
    state.selftestRunning = true;
    triggerHighFreq(40000);
    const host = $('fx-selftest-result');
    if (host) host.innerHTML = `<div class="fx-empty">L${level} 自检进行中…${level >= 1 ? '（会占用浏览器，排在队列里执行）' : ''}</div>`;
    try {
      const data = await post('/api/google-fx/selftest', { level });
      renderSelftest(data.selftest || {});
      showToast(`L${level} 自检${(data.selftest || {}).status === 'ok' ? '通过' : '发现问题'}`,
        (data.selftest || {}).status !== 'ok');
      await loadCaptures();
    } catch (error) {
      if (host) host.innerHTML = `<div class="fx-diagnostic" data-level="error">${esc(error.message)}</div>`;
      showToast(`自检失败：${error.message}`, true);
    } finally {
      state.selftestRunning = false;
      triggerHighFreq(5000);
    }
  }

  function renderSelectorProbe(probe) {
    const host = $('fx-probe-result');
    if (!host) return;
    const summary = probe.summary || {};
    const rows = probe.families || [];
    const broken = rows.filter((row) => row.state === 'missing');
    const fallback = rows.filter((row) => row.state === 'fallback');
    // conditional = 只在弹窗/菜单/落地页等其它页面状态才存在的族。探针是单页快照，
    // 它们在工作台页未命中属于正常。折叠展示而不是丢掉——静默吞掉的话，这些族真
    // 坏了也看不出来。
    const conditional = rows.filter((row) => row.state === 'conditional');
    host.innerHTML = `
      <div class="fx-probe-summary">
        <span class="fx-badge fx-badge-${broken.length ? 'bad' : 'good'}">失效 ${summary.missing || 0}</span>
        <span class="fx-badge fx-badge-${fallback.length ? 'warn' : 'good'}">靠兜底 ${summary.fallback || 0}</span>
        <span class="fx-badge fx-badge-good">主选择器 ${summary.primary || 0}</span>
        <span class="fx-badge fx-badge-muted">条件性 ${summary.conditional || 0}</span>
        <span>选择器版本 ${esc(probe.selector_version || '—')}</span>
      </div>
      ${broken.length ? `<div class="fx-probe-list">${broken.map((row) =>
        `<div class="fx-diagnostic" data-level="error">${esc(row.group)}.${esc(row.family)} 全 ${row.total_layers} 层均未命中</div>`).join('')}</div>` : ''}
      ${fallback.length ? `<div class="fx-probe-list">${fallback.map((row) =>
        `<div class="fx-diagnostic" data-level="warn">${esc(row.group)}.${esc(row.family)} 命中第 ${row.hit_index + 1} 层兜底</div>`).join('')}</div>` : ''}
      ${conditional.length ? `<details class="fx-probe-conditional">
        <summary>条件性未命中 ${conditional.length} 项（当前页面状态下本就不存在，非故障）</summary>
        <div class="fx-probe-list">${conditional.map((row) =>
          `<div class="fx-diagnostic">${esc(row.group)}.${esc(row.family)}${
            row.probed_via ? ` 在「${esc(row.probed_via)}」下仍未命中` : ` 全 ${row.total_layers} 层均未命中`}</div>`).join('')}</div>
      </details>` : ''}
      ${renderDeepScenarios(probe.deep_scenarios)}`;
  }

  // deep 模式下每个场景的开关结果。场景打不开本身就是信号（比如账号菜单触发器
  // 失效），必须显示出来，不能只看族的命中状态。
  function renderDeepScenarios(scenarios) {
    if (!scenarios || !scenarios.length) return '';
    return `<div class="fx-probe-list">${scenarios.map((entry) => {
      if (entry.error || !entry.opened) {
        return `<div class="fx-diagnostic" data-level="error">场景「${esc(entry.scenario)}」没能打开：${esc(entry.error || '未知原因')}</div>`;
      }
      const detail = (entry.families || [])
        .map((f) => `${esc(f.family)}=${esc(f.state)}`).join('，') || '无目标族';
      const bad = (entry.families || []).some((f) => f.state === 'missing');
      return `<div class="fx-diagnostic" data-level="${bad ? 'error' : 'ok'}">场景「${esc(entry.scenario)}」已打开 · ${detail}</div>`;
    }).join('')}</div>`;
  }

  async function runSelectorProbe(deep) {
    triggerHighFreq(30000);
    const host = $('fx-probe-result');
    if (host) {
      host.innerHTML = `<div class="fx-empty">正在连接浏览器跑${deep ? '深度' : ''}选择器探针…</div>`;
    }
    try {
      renderSelectorProbe(await post('/api/google-fx/selector-probe', deep ? { deep: true } : {}));
    } catch (error) {
      if (host) host.innerHTML = `<div class="fx-diagnostic" data-level="error">${esc(error.message)}</div>`;
      showToast(`选择器探针失败：${error.message}`, true);
    } finally {
      triggerHighFreq(5000);
    }
  }

  async function loadCaptures() {
    const host = $('fx-capture-list');
    if (!host) return;
    try {
      const data = await api('/api/google-fx/captures?limit=40');
      const rows = data.captures || [];
      if (!rows.length) {
        host.innerHTML = `<div class="fx-empty">暂无失败现场${data.enabled ? '' : '（自动取证已关闭）'}</div>`;
        return;
      }
      host.innerHTML = rows.map((row) => {
        const links = (row.files || []).map((file) =>
          `<a class="fx-link" target="_blank" rel="noopener" href="/api/google-fx/capture-file?id=${encodeURIComponent(row.id)}&file=${encodeURIComponent(file)}">${esc(file)}</a>`
        ).join(' · ');
        return `<div class="fx-capture">
          <div class="fx-capture-head"><strong>${esc(row.tag)}</strong><span>${esc(row.at)}</span><span>${esc(row.bucket)}</span></div>
          <div class="fx-capture-why">${esc(row.why || '')}</div>
          <div class="fx-capture-files">${links}</div>
        </div>`;
      }).join('');
    } catch (error) {
      host.innerHTML = `<div class="fx-diagnostic" data-level="error">${esc(error.message)}</div>`;
    }
  }

  async function loadLogs() {
    const host = $('fx-log-view');
    if (!host) return;
    const params = new URLSearchParams({ limit: '150' });
    if (state.logTaskFilter) params.set('task_id', state.logTaskFilter);
    if (state.logKeyword) params.set('q', state.logKeyword);
    try {
      const data = await api(`/api/google-fx/logs?${params.toString()}`);
      const lines = data.lines || [];
      host.textContent = lines.length ? lines.join('\n') : '（没有匹配的日志行）';
      host.scrollTop = host.scrollHeight;
    } catch (error) {
      host.textContent = `日志读取失败：${error.message}`;
    }
  }

  async function loadAudit() {
    const host = $('fx-audit-list');
    if (!host) return;
    try {
      const data = await api('/api/google-fx/audit?limit=60');
      const rows = data.rows || [];
      host.innerHTML = rows.length ? rows.map((row) => `
        <div class="fx-audit-row">
          <span class="fx-audit-at">${esc(formatTime(row.at))}</span>
          <span class="fx-audit-action">${esc(row.action)}</span>
          <span class="fx-audit-task">${esc(row.task_id || '')}</span>
          <span class="fx-audit-detail">${esc(JSON.stringify(row.details || {}))}</span>
        </div>`).join('') : '<div class="fx-empty">暂无审计记录</div>';
    } catch (error) {
      host.innerHTML = `<div class="fx-diagnostic" data-level="error">${esc(error.message)}</div>`;
    }
  }

  // ── 使用说明书 ────────────────────────────────────────────────────────────
  // 内容来自 docs/google_fx_console_manual.md。这里只做够用的 Markdown 渲染：
  // 标题 / 表格 / 列表 / 围栏代码块 / 引用 / 分隔线 / 行内 code、粗体、链接。
  // 全程先 escape 再放行这几种标记，所以文档内容不会变成注入面。

  function inlineMd(text) {
    return esc(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      // 只放行相对/同源链接与 http(s)，不放行 javascript: 之类的协议
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|[^):\s]+)\)/g,
        (match, label, href) => `<a href="${href}" target="_blank" rel="noopener">${label}</a>`);
  }

  function splitRow(line) {
    const cells = line.split('|').map((cell) => cell.trim());
    if (cells.length && cells[0] === '') cells.shift();
    if (cells.length && cells[cells.length - 1] === '') cells.pop();
    return cells;
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').split('\n');
    let html = '';
    let index = 0;
    let listOpen = false;

    const closeList = () => { if (listOpen) { html += '</ul>'; listOpen = false; } };

    while (index < lines.length) {
      const line = lines[index];
      const trimmed = line.trim();

      if (trimmed.startsWith('```')) {
        closeList();
        const buffer = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith('```')) {
          buffer.push(lines[index]);
          index += 1;
        }
        index += 1;
        html += `<pre class="fx-manual-code">${esc(buffer.join('\n'))}</pre>`;
        continue;
      }

      const isTable = trimmed.includes('|') && index + 1 < lines.length
        && /^[\s|:\-]+$/.test(lines[index + 1]) && lines[index + 1].includes('-');
      if (isTable) {
        closeList();
        const header = splitRow(trimmed);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes('|')) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        html += '<div class="fx-manual-table-wrap"><table class="fx-manual-table"><thead><tr>'
          + header.map((cell) => `<th>${inlineMd(cell)}</th>`).join('')
          + '</tr></thead><tbody>'
          + rows.map((row) => '<tr>' + row.map((cell) => `<td>${inlineMd(cell)}</td>`).join('') + '</tr>').join('')
          + '</tbody></table></div>';
        continue;
      }

      const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        closeList();
        const level = Math.min(6, heading[1].length + 2);
        html += `<h${level}>${inlineMd(heading[2])}</h${level}>`;
        index += 1;
        continue;
      }

      if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
        closeList();
        html += '<hr>';
        index += 1;
        continue;
      }

      if (trimmed.startsWith('> ')) {
        closeList();
        // 连续的引用行合成一个 blockquote。Markdown 里一段引用通常软换行成好几行，
        // 逐行各生成一个 blockquote 会渲染成一叠独立的小方块。
        const quoted = [];
        while (index < lines.length && lines[index].trim().startsWith('> ')) {
          quoted.push(lines[index].trim().slice(2));
          index += 1;
        }
        html += `<blockquote>${inlineMd(quoted.join(' '))}</blockquote>`;
        continue;
      }

      const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
      if (bullet) {
        if (!listOpen) { html += '<ul>'; listOpen = true; }
        html += `<li>${inlineMd(bullet[1])}</li>`;
        index += 1;
        continue;
      }

      if (!trimmed) {
        closeList();
        index += 1;
        continue;
      }

      // 普通段落：把连续的非空行并成一段。Markdown 的段落内软换行不是段落分隔，
      // 逐行各生成一个 <p> 会把一句话拆成好几段，行距全乱。
      closeList();
      const paragraph = [];
      while (index < lines.length) {
        const current = lines[index].trim();
        if (!current || current.startsWith('```') || current.startsWith('> ')
            || /^[-*]\s+/.test(current) || /^#{1,6}\s+/.test(current)
            || /^(-{3,}|\*{3,})$/.test(current)
            || (current.includes('|') && index + 1 < lines.length
                && /^[\s|:\-]+$/.test(lines[index + 1]) && lines[index + 1].includes('-'))) {
          break;
        }
        paragraph.push(current);
        index += 1;
      }
      html += `<p>${inlineMd(paragraph.join(' '))}</p>`;
    }
    closeList();
    return html;
  }

  async function openManual() {
    const overlay = $('fx-manual-overlay');
    const body = $('fx-manual-body');
    if (!overlay || !body) return;
    overlay.hidden = false;
    body.innerHTML = '<p>正在加载说明书…</p>';
    body.focus();
    try {
      const data = await api('/api/google-fx/manual');
      body.innerHTML = renderMarkdown(data.markdown);
      body.scrollTop = 0;
      if ($('fx-manual-source')) $('fx-manual-source').textContent = data.path || '';
    } catch (error) {
      body.innerHTML = `<div class="fx-diagnostic" data-level="error">说明书读取失败：${esc(error.message)}</div>`;
    }
  }

  function closeManual() {
    const overlay = $('fx-manual-overlay');
    if (overlay) overlay.hidden = true;
  }

  // ── 交互 ─────────────────────────────────────────────────────────────────

  async function loadProfiles(force) {
    if (state.profilesLoaded && !force) return;
    const select = $('fx-profile-select');
    if (!select) return;
    select.disabled = true;
    try {
      const data = await api('/api/account-pool/adspower-profiles');
      const known = new Set(state.accounts.map((account) => String(account.user_id)));
      const profiles = (data.profiles || []).filter((profile) => !known.has(String(profile.user_id)));
      select.innerHTML = '<option value="">选择环境…</option>' + profiles.map((profile) => {
        const serial = profile.serial_number ? `[#${esc(profile.serial_number)}] ` : '';
        return `<option value="${esc(profile.user_id)}">${serial}${esc(profile.name || profile.user_id)}</option>`;
      }).join('');
      state.profilesLoaded = true;
    } catch (error) {
      select.innerHTML = '<option value="">AdsPower 环境读取失败</option>';
      showToast(`环境列表读取失败：${error.message}`, true);
    } finally {
      select.disabled = false;
    }
  }

  async function handleAccountAction(button) {
    const action = button.dataset.action;
    const userId = button.dataset.userId;
    const account = state.accounts.find((item) => String(item.user_id) === userId);
    if (!account) return;
    if (action === 'delete' && !global.confirm(`从号池移除「${account.name || userId}」？\nAdsPower 环境和登录数据不会被删除。`)) return;

    let path;
    let body = { user_id: userId };
    if (action === 'refresh') path = '/api/account-pool/refresh';
    if (action === 'close-browser') path = '/api/account-pool/close-browser';
    if (action === 'clear-cooldown') path = '/api/account-pool/clear-cooldown';
    if (action === 'toggle') { path = '/api/account-pool/toggle'; body.disabled = !account.disabled; }
    if (action === 'delete') path = '/api/account-pool/delete';
    if (action === 'edit') {
      const name = global.prompt('账号名称', account.name || userId);
      if (name === null) return;
      const note = global.prompt('备注（可留空）', account.note || '');
      if (note === null) return;
      path = '/api/account-pool';
      body = { user_id: userId, name, note };
    }
    if (!path) return;
    button.disabled = true;
    try {
      const res = await post(path, body);
      state.profilesLoaded = false;
      await refresh(true);
      if (action === 'close-browser') {
        showToast((res && res.message) || '已发送关闭浏览器指令');
      } else {
        showToast('账号池已更新');
      }
    } catch (error) {
      if (action === 'refresh') await refresh(true);
      showToast(`操作失败：${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  }

  function filterAndFocusLogs(taskId) {
    if (!taskId) return;
    state.logTaskFilter = String(taskId);
    const input = $('fx-log-task');
    if (input) input.value = taskId;
    loadLogs();
    const logPanel = $('fx-log-panel') || document.querySelector('.fx-log-panel');
    if (logPanel) {
      logPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    showToast(`已将日志筛选切换至任务：${taskId}`);
  }

  async function handleTaskAction(button) {
    const action = button.dataset.taskAction;
    const taskId = button.dataset.taskId;
    if (action === 'view_logs') {
      filterAndFocusLogs(taskId);
      return;
    }
    if (action === 'timeline') {
      state.expandedTask = state.expandedTask === taskId ? null : taskId;
      renderTasks(state.lastTasks);
      return;
    }
    if (action === 'priority') {
      button.disabled = true;
      try {
        const task = (state.lastTasks || []).find((row) => String(row.id) === taskId);
        await post('/api/google-fx/control', {
          action: 'reprioritize', task_id: taskId,
          priority: Number(task?.priority || 0) + Number(button.dataset.delta || 0)
        });
        await refresh(true);
      } catch (error) { showToast(`调整优先级失败：${error.message}`, true); }
      finally { button.disabled = false; }
      return;
    }
    if (action === 'pin') {
      const options = state.accounts.filter((a) => accountState(a).key === 'ready')
        .map((a) => `${a.user_id} (${a.name || '未命名'})`).join('\n');
      const value = global.prompt(
        `给这个排队任务指定 AdsPower 账号（留空=解除定向）：\n\n可用账号：\n${options || '（无可用账号）'}`,
        '');
      if (value === null) return;
      button.disabled = true;
      try {
        await post('/api/google-fx/control', {
          action: 'pin_account', task_id: taskId, user_id: value.trim().split(' ')[0]
        });
        await refresh(true);
        showToast(value.trim() ? '已定向账号' : '已解除定向');
      } catch (error) { showToast(`定向失败：${error.message}`, true); }
      finally { button.disabled = false; }
      return;
    }
    if (action === 'cancel') {
      if (!global.confirm('确定取消这个 FX 任务吗？浏览器会保持开启。')) return;
      button.disabled = true;
      try {
        await post('/api/compose-cancel', { task_id: taskId });
        await refresh(true);
        showToast('已发出取消信号');
      } catch (error) { showToast(`取消失败：${error.message}`, true); }
      finally { button.disabled = false; }
    }
  }

  function bind() {
    $('fx-refresh-status')?.addEventListener('click', () => refresh(false));
    $('fx-open-manual')?.addEventListener('click', openManual);
    $('fx-manual-close')?.addEventListener('click', closeManual);
    // 「API 交互文档」标签页里的第二个入口：说明书本来就该能在文档区找到
    const docsEntry = $('fx-manual-docs-entry');
    if (docsEntry) {
      docsEntry.addEventListener('click', openManual);
      docsEntry.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openManual(); }
      });
    }
    $('fx-manual-overlay')?.addEventListener('click', (event) => {
      if (event.target === event.currentTarget) closeManual();  // 点遮罩关闭
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('fx-manual-overlay')?.hidden) closeManual();
    });
    ['pause', 'drain', 'resume'].forEach((action) => {
      $(`fx-control-${action}`)?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await post('/api/google-fx/control', { action });
          await refresh(true);
          showToast(`服务控制已切换：${action}`);
        } catch (error) { showToast(`控制失败：${error.message}`, true); }
        finally { button.disabled = false; }
      });
    });

    $('fx-limit-save')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/google-fx/control', {
          action: 'limits',
          limits: {
            max_concurrent: Number($('fx-limit-concurrent')?.value || 1),
            task_timeout_seconds: Number($('fx-limit-exec-timeout')?.value || 0),
            queue_wait_timeout_seconds: Number($('fx-limit-wait-timeout')?.value || 0)
          }
        });
        await refresh(true);
        showToast('调度限额已更新');
      } catch (error) { showToast(`更新失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });

    // ⚠️ 这四个（以及下面「应用限额」）过去在 finally 里写 event.currentTarget：
    // 事件派发结束后 currentTarget 会被置空，而这些处理器一 await 就已经"结束"了，
    // 于是 finally 抛 TypeError、按钮永远停在 disabled——保存一次配置之后「保存配置」
    // 就再也点不动了。改成进函数时先把按钮引用存下来。
    $('fx-config-save')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await saveConfig(); }
      catch (error) { showToast(`保存失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-config-rollback')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!global.confirm('回滚到上一个配置版本？可以连续回滚，也可以重做。')) return;
      button.disabled = true;
      try {
        const result = await post('/api/google-fx/config', { action: 'rollback' });
        syncFxModelToMainConfig(result.config);
        await Promise.all([loadConfig(), refresh(true)]);
        showToast('已回滚一个版本');
      } catch (error) { showToast(`回滚失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-config-redo')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await post('/api/google-fx/config', { action: 'redo' });
        syncFxModelToMainConfig(result.config);
        await Promise.all([loadConfig(), refresh(true)]);
        showToast('已重做一个版本');
      } catch (error) { showToast(`重做失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-config-restore')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const select = $('fx-config-versions');
      if (!select || !select.value) { showToast('请先选择一个版本', true); return; }
      if (!global.confirm('恢复到选中的配置版本？')) return;
      button.disabled = true;
      try {
        const result = await post('/api/google-fx/config', { action: 'restore', version_id: select.value });
        syncFxModelToMainConfig(result.config);
        await Promise.all([loadConfig(), refresh(true)]);
        showToast('已恢复到选中版本');
      } catch (error) { showToast(`恢复失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });

    [0, 1, 2].forEach((level) => {
      $(`fx-selftest-l${level}`)?.addEventListener('click', () => {
        if (level === 2 && !global.confirm(
          'L2 会真的向 Flow 提交一次最小图片生成请求（消耗账号额度）。继续？')) return;
        runSelftest(level);
      });
    });
    $('fx-run-probe')?.addEventListener('click', () => runSelectorProbe(false));
    $('fx-run-deep-probe')?.addEventListener('click', () => {
      // 深度探测会真的点击线上页面（开账号菜单、开配置面板），先问一句。
      if (!confirm('深度探测会在浏览器里点开账号菜单和配置面板，随后按 Escape 收尾。确认继续？')) return;
      runSelectorProbe(true);
    });
    $('fx-reset-selector-stats')?.addEventListener('click', async () => {
      if (!global.confirm('清空选择器命中统计？修好选择器后清一次，漂移警告才会消失。')) return;
      try {
        const data = await post('/api/google-fx/selector-stats/reset', {});
        await refresh(true);
        showToast(`已清空 ${data.removed || 0} 组选择器统计`);
      } catch (error) { showToast(`清空失败：${error.message}`, true); }
    });
    $('fx-reload-captures')?.addEventListener('click', loadCaptures);
    $('fx-reload-audit')?.addEventListener('click', loadAudit);
    $('fx-log-refresh')?.addEventListener('click', () => triggerHighFreq(15000));
    $('fx-log-task')?.addEventListener('change', (event) => {
      state.logTaskFilter = event.target.value.trim();
      triggerHighFreq(15000);
    });
    $('fx-log-keyword')?.addEventListener('change', (event) => {
      state.logKeyword = event.target.value.trim();
      triggerHighFreq(15000);
    });

    $('fx-account-sort-select')?.addEventListener('change', (event) => {
      state.accountSortKey = event.target.value;
      renderAccounts(state.accounts);
    });

    if (typeof document !== 'undefined') {
      document.querySelectorAll('.fx-sortable-th').forEach((th) => {
        th.addEventListener('click', () => {
          const key = th.dataset.sortKey;
          if (key === 'credit') {
            state.accountSortKey = state.accountSortKey === 'credit_desc' ? 'credit_asc' : 'credit_desc';
          } else if (key === 'tasks') {
            state.accountSortKey = state.accountSortKey === 'tasks_desc' ? 'tasks_asc' : 'tasks_desc';
          }
          renderAccounts(state.accounts);
        });
      });
    }

    $('fx-import-profiles')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await post('/api/account-pool/import-all', {});
        state.profilesLoaded = false;
        await refresh(true);
        await loadProfiles(true);
        showToast(`导入完成：新增 ${result.added || 0}，跳过 ${result.skipped || 0}`);
      } catch (error) { showToast(`导入失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-add-profile')?.addEventListener('click', async (event) => {
      const select = $('fx-profile-select');
      if (!select || !select.value) { showToast('请先选择一个 AdsPower 环境', true); return; }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const option = select.options[select.selectedIndex];
        await post('/api/account-pool', { user_id: select.value, name: option.textContent || select.value, note: '' });
        state.profilesLoaded = false;
        await refresh(true);
        await loadProfiles(true);
        showToast('环境已加入号池');
      } catch (error) { showToast(`添加失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });

    $('fx-account-table-body')?.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (button) handleAccountAction(button);
    });
    $('fx-proxy-table-body')?.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-proxy-action]');
      if (button) handleProxyAction(button);
    });
    $('fx-proxy-refresh')?.addEventListener('click', async () => {
      await loadProxies();
      showToast('代理号池已刷新');
    });
    $('fx-proxy-toggle-form')?.addEventListener('click', () => {
      const form = $('fx-proxy-form');
      if (form && !form.hidden && !state.proxyEditingId) closeProxyForm();
      else openProxyForm(null);
    });
    $('fx-proxy-cancel')?.addEventListener('click', closeProxyForm);
    $('fx-proxy-save')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await saveProxy(); }
      catch (error) { showToast(`保存失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-proxy-import')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await post('/api/proxy-pool/import-legacy', {});
        await Promise.all([loadProxies(), refresh(true)]);
        showToast(result.message
          || `导入完成：新增 ${result.added || 0} 条，跳过 ${result.skipped || 0} 条`);
      } catch (error) { showToast(`导入失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });
    $('fx-task-table-body')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-task-action]');
      if (button) handleTaskAction(button);
    });

    $('fx-force-release-all')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!global.confirm('确定强行清空所有卡死槽位吗？这将立刻释放被占用的临界区。')) return;
      button.disabled = true;
      try {
        await post('/api/google-fx/control', { action: 'force_release_slot' });
        await refresh(true);
        showToast('已一键清空所有卡死槽位');
      } catch (error) { showToast(`释放失败：${error.message}`, true); }
      finally { button.disabled = false; }
    });

    $('fx-slot-refresh')?.addEventListener('click', () => refresh(false));

    $('fx-slot-display')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-slot-action]');
      if (!button) return;
      const action = button.dataset.slotAction;
      const taskId = button.dataset.taskId;

      if (action === 'view_logs') {
        filterAndFocusLogs(taskId);
        return;
      }

      if (action === 'force_release' || action === 'cancel') {
        if (!global.confirm(`确定要强行释放槽位占用任务 ${taskId} 吗？`)) return;
        button.disabled = true;
        try {
          await post('/api/google-fx/control', { action: 'force_release_slot', task_id: taskId });
          await refresh(true);
          showToast(`已强行释放任务 ${taskId} 的槽位占用`);
        } catch (error) { showToast(`释放失败：${error.message}`, true); }
        finally { button.disabled = false; }
      } else if (action === 'priority') {
        button.disabled = true;
        try {
          const delta = Number(button.dataset.delta || 0);
          await post('/api/google-fx/control', {
            action: 'reprioritize', task_id: taskId, priority: delta > 0 ? 50 : -10
          });
          await refresh(true);
        } catch (error) { showToast(`调整优先级失败：${error.message}`, true); }
        finally { button.disabled = false; }
      }
    });

    $('fx-console-root')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-fx-action="clear-orphaned"]');
      if (!button) return;
      button.disabled = true;
      try {
        await post('/api/google-fx/control', { action: 'clear_orphaned' });
        await refresh(true);
        triggerHighFreq(15000);
      } catch (error) { showToast(`清除失败：${error.message}`, true); }
    });

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && state.active) {
          triggerHighFreq(10000);
        }
      });
    }
  }

  let tickCount = 0;

  function triggerHighFreq(durationMs = 15000) {
    state.highFreqUntil = Math.max(state.highFreqUntil || 0, Date.now() + durationMs);
    loadLogs();
    scheduleNextTick(100);
  }

  function scheduleNextTick(delayMs) {
    if (state.timer) clearTimeout(state.timer);
    if (!state.active) return;
    state.timer = setTimeout(runTick, delayMs);
  }

  async function runTick() {
    if (!state.active) return;
    if (document.hidden) {
      scheduleNextTick(6000);
      return;
    }

    await refresh(true);

    const activeCount = state.lastQueue?.active_count || 0;
    const waitingCount = state.lastQueue?.waiting_count || 0;
    const isBusy = Boolean(state.selftestRunning || activeCount > 0 || waitingCount > 0 || Date.now() < state.highFreqUntil);

    tickCount++;
    if (isBusy) {
      // 动态高频轮询模式：1.5 秒更新一次日志与审计记录，呈现流畅的实时推流体验
      await Promise.all([loadLogs(), loadAudit()]);
      scheduleNextTick(1500);
    } else {
      // 空闲模式：每 3 个 6 秒周期（约 18 秒）更新一次日志与审计记录
      if (tickCount % 3 === 0) {
        await Promise.all([loadLogs(), loadAudit()]);
      }
      scheduleNextTick(6000);
    }
  }

  function init() {
    if (state.initialized || !$('fx-console-root')) return;
    state.initialized = true;
    bind();
  }

  function activate() {
    init();
    state.active = true;
    refresh(true);
    loadConfig();
    loadProfiles(false);
    loadProxies();
    loadCaptures();
    loadAudit();
    loadLogs();
    scheduleNextTick(1500);
  }

  function deactivate() {
    state.active = false;
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
  }

  const apiObject = {
    init, activate, deactivate, refresh, accountState, proxyState, taskTypeLabel,
    creditLabel, formatDuration, runSelftest, runSelectorProbe,
    renderMarkdown, openManual, closeManual, loadProxies
  };
  global.GoogleFxConsole = apiObject;
  if (typeof module !== 'undefined' && module.exports) module.exports = apiObject;
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', init);
})(typeof window !== 'undefined' ? window : globalThis);
