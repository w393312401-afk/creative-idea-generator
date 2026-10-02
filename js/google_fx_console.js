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
    configBaseline: null,
    configSaving: false,
    configLoadSeq: 0,
    configMessage: '',
    configError: '',
    configRemoteUpdate: false,
    configRestartRequired: [],
    section: 'monitor',
    lastTasks: [],
    lastQueue: {},
    proxies: [],
    proxyEditingId: '',
    selftestRunning: false,
    logTaskFilter: '',
    logKeyword: '',
    logAutoTask: true,
    logFollow: true,
    logPaused: false,
    logLevel: 'all',
    logRenderedLines: [],
    logRenderedRecords: [],
    logPendingLines: null,
    logPendingRecords: null,
    logQueryKey: '',
    logRequestSeq: 0,
    logError: '',
    expandedTask: null,
    accountSortKey: 'serial_asc',
    selectedAccounts: new Set(),
    lastAccountClickedIndex: -1
  };

  const $ = (id) => typeof document !== 'undefined' ? document.getElementById(id) : null;
  const esc = (value) => {
    if (typeof global.escapeHtml === 'function') return global.escapeHtml(String(value ?? ''));
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  };

  // 账号状态判定顺序有讲究：禁用 > 登录失效 > 图片限额 > 积分不足 > 冷却 > 未探测 > 可用。
  // "登录失效"必须排在"冷却"之前单独成一档——后端用 cooldown_reason 区分了
  // "登录失效（2 小时冷却，积分不动）"和"额度耗尽（24 小时冷却，积分清零）"，
  // 前端一律显示"冷却中"的话，用户不知道该去人工重新登录还是等额度恢复。
  // "未探测"也必须和"额度耗尽"分开：credit=null 表示从来没探测成功过，
  // 不是"没额度"，把它显示成 0 或"可用"都是在编造账号健康状态。
  function accountState(account, nowMs = Date.now()) {
    const cooling = account && account.cooldown_until
      && Number.isFinite(Date.parse(account.cooldown_until))
      && Date.parse(account.cooldown_until) > nowMs;
    if (account && account.disabled) {
      if (account.disabled_reason === 'zero_credit') {
        return { key: 'empty', label: cooling ? '积分不足 · 周期停用' : '积分不足 · 待复核', tone: 'warn' };
      }
      return { key: 'disabled', label: '已禁用', tone: 'bad' };
    }
    if (cooling && account.cooldown_reason === 'login_required') {
      return { key: 'login_required', label: '待人工登录', tone: 'bad' };
    }
    if (cooling && (account.cooldown_reason === 'image_quota_exceeded' || account.cooldown_reason === 'daily_limit_reached' || account.cooldown_reason === 'daily_limit' || account.cooldown_reason === 'image_limit_exceeded' || String(account.last_generation_error || '').includes('图片余额超限'))) {
      return { key: 'image_limit', label: '图片余额超限', tone: 'bad' };
    }
    if (account && (account.cooldown_reason === 'quota_exhausted'
      || account.disabled_reason === 'zero_credit'
      || (account.credit != null && Number(account.credit) < Number(account.min_credit ?? 15)))) {
      return { key: 'empty', label: '积分不足', tone: 'bad' };
    }
    if (cooling) return { key: 'cooling', label: '冷却中', tone: 'warn' };
    if (account && account.last_probe_status === 'failed') {
      return { key: 'probe_failed', label: '探测失败', tone: 'bad' };
    }
    if (account && (account.credit === null || account.credit === undefined)) {
      return { key: 'unprobed', label: '未探测', tone: 'warn' };
    }
    if (account && account.credit_stale) return { key: 'stale', label: '缓存过期', tone: 'warn' };
    if (Number(account.credit) <= 0) return { key: 'empty', label: '积分不足', tone: 'bad' };
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

  function taskState(task = {}) {
    if (task.status === 'running') {
      if (task.manual_intervention) return { key: 'manual', label: '等待登录或验证', tone: 'bad' };
      if (task.queue_state === 'waiting') return { key: 'queued', label: '排队中', tone: 'neutral' };
      if (/retry|ip_rotating/.test(task.stage || '')) return { key: 'recovering', label: '自动恢复中', tone: 'warn' };
      return { key: 'running', label: '生成中', tone: 'neutral' };
    }
    if (task.status === 'completed') {
      const outcome = task.outcome || task.result?.completion_state;
      if (outcome === 'partial_failed' || task.has_failures || task.result?.has_failures) {
        return { key: 'partial_failed', label: '部分完成', tone: 'warn' };
      }
      if (outcome === 'completed_with_warnings' || task.has_quality_warnings || task.result?.has_quality_warnings) {
        return { key: 'completed_with_warnings', label: '已完成 · 有提醒', tone: 'warn' };
      }
      return { key: 'completed', label: '已完成', tone: 'good' };
    }
    return ({
      queued: { key: 'queued', label: '排队中', tone: 'neutral' },
      pending: { key: 'pending', label: '等待开始', tone: 'neutral' },
      paused: { key: 'paused', label: '等待确认', tone: 'neutral' },
      cancelled: { key: 'cancelled', label: '已取消', tone: 'neutral' },
      failed: { key: 'failed', label: '失败', tone: 'bad' },
    })[task.status] || { key: 'unknown', label: '状态未知', tone: 'neutral' };
  }

  function stageLabel(stage) {
    return ({ queue: '等待调度', queued: '等待调度', submitting: '提交请求',
      generating: '生成中', polling: '等待生成结果', downloading: '下载素材',
      frame_start: '准备生成帧', frame: '帧已生成', video_start: '准备生成视频',
      video_done: '片段已完成', video_error: '片段未成功', video_warning: '生成提醒',
      video_retry_autonomous: '自动重试', upstream_retry: '自动重试',
      ip_rotating: '恢复网络', ip_rotated: '网络已恢复', ip_rotation_failed: '网络恢复未成功',
      manual_intervention: '等待人工处理', manual_intervention_detected: '等待登录或验证',
      manual_intervention_cleared: '验证已完成', manual_intervention_timeout: '等待处理超时',
      merge: '合并视频', merging: '合并视频', result: '结果已返回', error: '任务已停止',
      completed: '已完成', cancelled: '已取消' })[stage] || stage || '—';
  }

  function currentLogTask(tasks, queue = {}) {
    const active = queue.active_list || (queue.active ? [queue.active] : []);
    const activeTask = active.find(row => (tasks || []).some(task => task.id === row.task_id && task.status === 'running'));
    return activeTask?.task_id || (tasks || []).find(task => task.status === 'running')?.id || '';
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

  async function api(path, options = {}) {
    const timeoutMs = options.timeout ?? 8000;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const fetchOpts = { ...options, signal: options.signal || controller.signal };
      delete fetchOpts.timeout;
      const response = await fetch(path, fetchOpts);
      let data = {};
      try { data = await response.json(); } catch (_) { /* empty body */ }
      if (!response.ok || data.status === 'error') {
        throw new Error(data.message || data.error || `HTTP ${response.status}`);
      }
      return data;
    } catch (err) {
      if (err.name === 'AbortError') {
        throw new Error(`请求超时 (${timeoutMs / 1000}s)`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
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

    const activeTask = queue.active;
    const busy = Boolean(execution.lock_busy || activeTask);
    const activeRecord = activeTask && (data.tasks || []).find(task => task.id === activeTask.task_id);
    const activeLabel = activeRecord?.theme || (activeTask ? taskTypeLabel(activeTask.kind) : '浏览器占用中');
    setCard('fx-execution', busy ? '正在执行' : '空闲',
      busy ? activeLabel : '当前没有任务执行',
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
    // 旧换号节拍只为配置兼容保留，不再按请求数主动更换有额度的环境。
    const switchEl = $('fx-config-switch');
    if (switchEl) {
      switchEl.textContent = '复用至额度不足';
      switchEl.className = 'fx-safe-value';
      switchEl.title = '自动选号优先复用已打开且额度足够的浏览器；额度不足后周期停用 24 小时，关闭该浏览器并换号继续。手动指定或锁定默认环境优先。';
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
        ? `序列首选浏览器环境：${seqId}${config.sequence_user_locked ? '（优先使用指定环境，额度不足仍自动换号）' : '（无可复用浏览器时优先使用）'}`
        : '未指定：优先复用已打开且额度足够的浏览器；没有可用窗口时按号池策略选号';
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
    const silentModeEl = $('fx-config-silent-mode');
    if (silentModeEl) {
      const isSilent = config.silent_mode !== false;
      silentModeEl.textContent = isSilent ? '静默后台 (防抢焦)' : '正常可见窗口';
      silentModeEl.className = isSilent ? 'fx-safe-value' : 'fx-danger-value';
      silentModeEl.title = isSilent
        ? 'AdsPower 浏览器在屏幕外 (-10000, -10000) 运行，不弹窗抢焦点'
        : 'AdsPower 浏览器以可见窗口运行（适合调试/人工登录）';
    }

    const modeBadge = $('fx-service-mode');
    if (modeBadge) {
      const unavailable = !runtime.available || !adspower.online;
      modeBadge.textContent = !runtime.available ? '运行时不可用' : !adspower.online ? '等待浏览器服务'
        : config.dry_run ? '演练模式 · 不提交' : busy ? '任务执行中' : '服务已就绪';
      modeBadge.className = `fx-lock-badge fx-badge-${unavailable ? 'bad' : busy || config.dry_run ? 'warn' : 'good'}`;
    }

    const lockBadge = $('fx-lock-badge');
    if (lockBadge) {
      lockBadge.textContent = busy ? '浏览器锁：占用中' : '浏览器锁：空闲';
      lockBadge.className = `fx-lock-badge fx-badge-${busy ? 'warn' : 'good'}`;
    }

    renderSlotDisplay(queue);
    renderBoard(data.board);
    renderDiagnostics(data.diagnostics || [], data.selectors || {});
    renderTasks(data.tasks || []);
  }

  function renderBoard(board) {
    const container = $('fx-board');
    if (!container || !global.FxBoard) return;
    // 只读面板：渲染失败不能拖垮整页状态刷新。
    try {
      global.FxBoard.render(container, board);
    } catch (error) {
      container.innerHTML = `<div class="fx-empty">作战板渲染失败：${esc(error.message)}</div>`;
    }
  }

  function renderSlotDisplay(queue) {
    const container = $('fx-slot-display');
    if (!container) return;

    const active = queue.active;
    if (active) {
      const id = esc(active.task_id);
      const kind = esc(active.kind || 'fx');
      const accountPin = active.account_pin ? `📌 绑定账号 ${esc(active.account_pin)}` : '';

      const kindLabelMap = {
        'credit_probe': '账号积分探针 (credit_probe)',
        'selector_probe': '选择器探针 (selector_probe)',
        'selftest': '环境自检 (selftest)',
        'auto_login': '账号自动登录 (auto_login)',
        'frames': '图片/帧序列生成 (frames)',
        'videos': '视频生成 (videos)',
      };
      const kindText = kindLabelMap[kind] || `底层任务 (${kind})`;

      container.innerHTML = `
        <div class="fx-slot-card is-active">
          <div class="fx-slot-info">
            <div class="fx-slot-title">
              <span class="fx-badge fx-badge-warn">⚡ 临界区占用中</span>
              <strong>${kindText}</strong>
            </div>
            <div class="fx-slot-meta">
              任务 ID: <code>${id}</code> ${active.started_at ? `· 开始于: <strong>${esc(formatTime(active.started_at))}</strong>` : ''} ${accountPin}
            </div>
          </div>
          <div class="fx-slot-actions">
            <button class="fx-button" data-slot-action="view_logs" data-task-id="${id}">📜 查看实时日志</button>
            <details class="fx-action-menu"><summary class="fx-button">维护</summary><div class="fx-action-menu-body">
              <button class="fx-button fx-button-danger" data-slot-action="force_release" data-task-id="${id}">强行释放此占用</button>
            </div></details>
          </div>
        </div>
      `;
    } else {
      container.innerHTML = `
        <div class="fx-slot-card is-idle">
          <div class="fx-slot-info">
            <div class="fx-slot-title">
              <span class="fx-badge fx-badge-good">🟢 核心槽位空闲中</span>
              <span>当前无任何底层任务占用 Google FX 浏览器槽位，随时可正常接收新任务</span>
            </div>
          </div>
        </div>
      `;
    }
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

  function updateConsoleAccountBulkBar() {
    const count = state.selectedAccounts ? state.selectedAccounts.size : 0;
    const bar = $('fx-account-bulk-bar');
    const countEl = $('fx-account-bulk-count');
    if (countEl) countEl.textContent = count > 0 ? `已选 ${count} 项` : '';
    if (bar) {
      bar.style.display = count > 0 ? 'flex' : 'none';
    }
    const selectAllChk = $('fx-account-select-all');
    if (selectAllChk) {
      const accounts = state.accounts || [];
      const allSelected = accounts.length > 0 && accounts.every(a => state.selectedAccounts && state.selectedAccounts.has(a.user_id));
      selectAllChk.checked = allSelected;
      selectAllChk.disabled = accounts.length === 0;
    }
  }

  function renderAccounts(accounts) {
    state.accounts = accounts;
    const body = $('fx-account-table-body');
    if (!body) return;

    if (!state.selectedAccounts) state.selectedAccounts = new Set();
    const validIds = new Set((accounts || []).map(a => a.user_id));
    for (const uid of state.selectedAccounts) {
      if (!validIds.has(uid)) state.selectedAccounts.delete(uid);
    }
    updateConsoleAccountBulkBar();

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
      body.innerHTML = '<tr><td colspan="8" class="fx-empty">号池为空，可从右上角导入 AdsPower 环境。</td></tr>';
      return;
    }

    const sortedAccounts = sortAccounts(accounts, state.accountSortKey);

    const priorityList = Array.isArray(state.config?.googleFxPriorityUserIds)
      ? state.config.googleFxPriorityUserIds.map(String)
      : (typeof state.config?.googleFxPriorityUserIds === 'string'
        ? state.config.googleFxPriorityUserIds.split(',').map(s => s.trim())
        : []);
    const prioritySet = new Set(priorityList);

    body.innerHTML = sortedAccounts.map((account, index) => {
      const status = accountState(account);
      const uid = esc(account.user_id);
      const isSelected = state.selectedAccounts && state.selectedAccounts.has(account.user_id);
      const isPriority = prioritySet.has(account.user_id);
      const priStar = isPriority ? `<span title="优先级浏览器实例（生成时优先调度）" style="color:#eab308; font-size:13px; margin-right:4px; user-select:none;">⭐</span>` : '';
      const note = account.note ? `<span class="fx-account-id">${esc(account.note)}</span>` : '';
      const expTag = account.expires_at ? `<span class="fx-account-id" style="color:var(--text-muted, #888); font-size:11px; margin-left:4px;">[重置:${esc(account.expires_at)}]</span>` : '';
      const cooldownButton = (status.key === 'cooling' || status.key === 'login_required')
        ? `<button class="fx-button" data-action="clear-cooldown" data-user-id="${uid}">解除冷却</button>` : '';
      const imgCount = account.image_task_count || 0;
      const vidCount = account.video_task_count || 0;
      const totalCount = account.task_count != null ? account.task_count : (imgCount + vidCount);
      const taskBadge = `<span class="fx-account-id" style="font-weight:600; color:var(--text-main, #333);">${totalCount}</span> <span class="fx-account-id" style="font-size:11px; color:var(--text-muted, #888);">(图${imgCount}/视${vidCount})</span>`;

      return `<tr class="${isSelected ? 'fx-row-selected' : ''}">
        <td style="text-align:center;"><input type="checkbox" class="fx-account-select" data-user-id="${uid}" ${isSelected ? 'checked' : ''} style="cursor:pointer;"></td>
        <td style="text-align:center; font-weight:700; color:var(--text-muted, #888); font-size:11px;">#${index + 1}</td>
        <td>${priStar}${account.serial_number ? `<span class="fx-account-id" style="color:var(--text-muted, #888); font-weight:600; margin-right:4px;">[环境#${esc(account.serial_number)}]</span>` : ''}<span class="fx-account-name">${esc(account.name || account.user_id)}</span><span class="fx-account-id">${uid}</span>${expTag}${note}</td>
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
      const priority = proxy.source === 'airport'
        ? (String(proxy.fallback_tier) === '0' ? '机场 · 美国优先' : '机场 · 备用')
        : '静态 · 优先';
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
          <span class="fx-badge fx-badge-muted">${esc(priority)}</span>
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
        <span class="fx-timeline-stage">${esc(stageLabel(row.stage))}${row.count > 1 ? ` ×${row.count}` : ''}</span>
        <span class="fx-timeline-duration">${esc(formatDuration(row.duration_seconds))}</span>
        <span class="fx-timeline-message">${esc(row.message || '')}</span>
      </div>`).join('') + '</div>';
  }

  function renderTasks(tasks) {
    state.lastTasks = tasks;
    syncCurrentLogTask();
    const body = $('fx-task-table-body');
    if (!body) return;
    if (!tasks.length) {
      body.innerHTML = '<tr><td colspan="7" class="fx-empty">当前没有 FX 任务记录。</td></tr>';
      return;
    }
    body.innerHTML = tasks.map((task) => {
      const displayState = taskState(task);
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
        ? `<div class="fx-task-flag warn">阶段 ${esc(stageLabel(task.stuck_stage.stage))} 已 ${esc(formatDuration(task.stuck_stage.duration_seconds))}</div>` : '';
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
        <td><button class="fx-link" data-task-action="timeline" data-task-id="${id}">${esc(stageLabel(task.stage))} ${expanded ? '▾' : '▸'}</button></td>
        <td>${esc(task.account || '自动')}${pin}</td>
        <td><span class="fx-badge fx-badge-${displayState.tone}" title="${esc(task.error || '')}">${esc(displayState.label)}</span></td>
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
      renderAccountsAndSyncConfig((pool && pool.accounts) || []);
      renderStatus(status || {});
      if (!silent) showToast('Google FX 状态已刷新');
    } catch (error) {
      setCard('fx-runtime', '读取失败', error.message, 'bad');
      setCard('fx-adspower', '读取失败', '服务端未响应', 'bad');
      setCard('fx-execution', '未知', '状态同步失败', 'bad');
      setCard('fx-account', '读取失败', '号池未加载', 'bad');
      setCard('fx-proxy', '读取失败', '代理未加载', 'bad');
      const diagList = $('fx-diagnostic-list');
      if (diagList) {
        diagList.innerHTML = `<div class="fx-diagnostic" data-level="error">Google FX 状态同步失败：${esc(error.message)}</div>`;
      }
      const accBody = $('fx-account-table-body');
      if (accBody && !state.accounts.length) {
        accBody.innerHTML = `<tr><td colspan="6" class="fx-empty">获取账号列表失败：${esc(error.message)}</td></tr>`;
      }
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
    return `<option value=""${current ? '' : ' selected'}>自动（优先复用可用浏览器）</option>` + rows.join('');
  }

  function syncAccountConfigFields() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('select[data-config-account]').forEach((select) => {
      if (document.activeElement === select) return;  // 用户正在选，别把它重建掉
      select.innerHTML = accountOptionsHtml(select.value);
    });
    document.querySelectorAll('#fx-config-form [data-config-type="account_list"]').forEach((field) => {
      if (field.contains(document.activeElement)) return;
      const selected = Array.from(field.querySelectorAll('input[data-config-account-item]:checked'),
        checkbox => checkbox.dataset.configAccountItem);
      const list = field.querySelector('.fx-config-account-list');
      if (list) list.innerHTML = configAccountListHtml(selected);
    });
  }

  function configAccountListHtml(value) {
    const selected = new Set((Array.isArray(value) ? value : String(value || '').split(','))
      .map(String).map(value => value.trim()).filter(Boolean));
    const accounts = [...(state.accounts || [])];
    selected.forEach(uid => {
      if (!accounts.some(account => String(account.user_id) === uid)) accounts.push({ user_id: uid, name: `${uid}（已不在号池）` });
    });
    return accounts.map(account => {
      const uid = String(account.user_id);
      const serial = account.serial_number ? `[#${esc(account.serial_number)}] ` : '';
      return `<label class="fx-config-account-option"><input type="checkbox" data-config-account-item="${esc(uid)}"${selected.has(uid) ? ' checked' : ''}>
        <span>${serial}${esc(account.name || uid)}</span></label>`;
    }).join('') || '<span class="fx-config-empty">暂无号池账号</span>';
  }

  function configFieldHtml(key, spec, value) {
    const label = `${esc(spec.label || key)}${spec.hot ? '' : ' <span class="fx-restart-tag">需重启</span>'}`;
    if (spec.inactive) {
      return `<label class="fx-config-field"><span>${label}</span>
        <input type="number" value="${esc(value ?? spec.default)}" disabled aria-label="${esc(spec.label || key)}">
        <small>${esc(spec.hint || '仅兼容旧配置，不参与当前调度。')}</small></label>`;
    }
    if (spec.type === 'account') {
      return `<label class="fx-config-field"><span>${label}</span>
        <select data-config-key="${esc(key)}" data-config-account="1">${accountOptionsHtml(value)}</select></label>`;
    }
    if (spec.type === 'account_list') {
      return `<div class="fx-config-field" data-config-key="${esc(key)}" data-config-type="account_list">
        <span>${label}</span>
        <div class="fx-config-account-list">${configAccountListHtml(value)}</div>
      </div>`;
    }
    if (spec.type === 'bool') {
      return `<label class="fx-config-field"><span>${label}</span>
        <input type="checkbox" data-config-key="${esc(key)}" ${value ? 'checked' : ''}></label>`;
    }
    if (spec.type === 'enum') {
      const current = value ?? spec.default ?? '';
      const values = [...(spec.options || [])];
      const labels = ({
        videoRefMode: { VIDEO_FRAMES: '帧（首尾帧）', VIDEO_REFERENCES: '素材（主体与风格）', INGREDIENTS: '素材（主体与风格）' },
        googleFxAccountStrategy: { credit_desc: '积分最多优先', expiration_asc: '重置日期最早优先', rotation: '均衡使用' },
        adsPowerMacWindowMode: { hide: '隐藏浏览器窗口', focus: '仅归还焦点', off: '不干预窗口' },
      })[key] || {};
      if (!values.some(option => String(option) === String(current))) values.push(current);
      const options = values.map((option) =>
        `<option value="${esc(option)}"${String(option) === String(current) ? ' selected' : ''}>${esc(labels[option] || option || '不指定')}</option>`
      ).join('');
      return `<label class="fx-config-field"><span>${label}</span>
        <select data-config-key="${esc(key)}">${options}</select></label>`;
    }
    return `<label class="fx-config-field"><span>${label}</span>
      <input type="number" data-config-key="${esc(key)}" min="${esc(spec.min ?? 0)}" max="${esc(spec.max ?? 999999999)}" value="${esc(value ?? spec.default)}"></label>`;
  }

  function showSection(section, targetId) {
    if (!['monitor', 'accounts', 'maintenance'].includes(section)) return;
    const changed = state.section !== section;
    state.section = section;
    if (typeof document === 'undefined') return;
    document.querySelectorAll('.fx-console-section').forEach((panel) => {
      panel.hidden = panel.id !== `fx-section-${section}`;
    });
    document.querySelectorAll('.fx-section-tab').forEach((button) => {
      const selected = button.dataset.fxSection === section;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    if (targetId) $(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    else if (changed) $(`fx-section-${section}`)?.scrollIntoView({ behavior: 'auto', block: 'start' });
  }

  function configValueEqual(left, right, spec = {}) {
    if (spec.type === 'account_list') {
      const normalize = (value) => (Array.isArray(value) ? value : String(value || '').split(','))
        .map(String).map(value => value.trim()).filter(Boolean).sort();
      return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
    }
    if (spec.type === 'integer') return Number(left) === Number(right);
    if (spec.type === 'bool') return Boolean(left) === Boolean(right);
    return String(left ?? '') === String(right ?? '');
  }

  function readConfigValues() {
    const values = {};
    if (typeof document === 'undefined') return values;
    (document.querySelectorAll('#fx-config-form [data-config-key]') || []).forEach((el) => {
      const key = el.dataset.configKey;
      const spec = (state.configSchema || {})[key];
      if (!spec || spec.inactive) return;
      if (spec.type === 'bool') values[key] = el.checked;
      else if (spec.type === 'integer') values[key] = el.value === '' ? null : Number(el.value);
      else if (spec.type === 'account_list') {
        values[key] = Array.from(el.querySelectorAll('input[data-config-account-item]:checked'),
          checkbox => checkbox.dataset.configAccountItem);
      } else values[key] = el.value;
    });
    return values;
  }

  function configDelta(values, baseline) {
    const patch = {};
    Object.entries(values).forEach(([key, value]) => {
      const spec = (state.configSchema || {})[key];
      if (spec && !spec.inactive && !configValueEqual(value, baseline?.[key] ?? spec.default, spec)) patch[key] = value;
    });
    return patch;
  }

  function collectConfigPatch() {
    return state.configBaseline ? configDelta(readConfigValues(), state.configBaseline) : {};
  }

  function updateConfigFeedback() {
    const count = Object.keys(collectConfigPatch()).length;
    const saving = state.configSaving;
    const restart = state.configRestartRequired.length > 0;
    const label = saving ? '正在保存…' : state.configError ? '保存 / 读取失败'
      : count ? `${count} 项尚未保存` : restart ? '已保存 · 待重启' : state.configBaseline ? '已与服务器同步' : '正在读取配置';
    const status = $('fx-config-state');
    if (status) {
      status.textContent = label;
      status.dataset.state = state.configError ? 'error' : saving ? 'saving' : count || restart ? 'pending' : 'saved';
    }
    const button = $('fx-config-save');
    if (button) {
      button.disabled = saving || !state.configBaseline || count === 0;
      button.textContent = saving ? '保存中…' : count ? `保存 ${count} 项更改` : '保存配置';
    }
    const reload = $('fx-config-reload');
    if (reload) reload.disabled = saving;
    const indicator = $('fx-config-tab-indicator');
    if (indicator) {
      indicator.hidden = !count && !restart;
      indicator.textContent = count ? '· 未保存' : '· 待重启';
    }
    const note = $('fx-config-note');
    if (note) {
      const restartNames = state.configRestartRequired.map(key => (state.configSchema?.[key] || {}).label || key);
      note.textContent = [
        state.configError,
        saving ? '正在写入本次修改，之后继续编辑的内容会保留为未保存。' : state.configMessage,
        count && !saving ? '当前修改仅保留在此页面，点击保存后才会写入。' : '',
        state.configRemoteUpdate ? '服务器配置已更新；你的编辑已保留，保存只提交修改过的字段。' : '',
        restart ? `本次会话已保存的以下设置需要重启服务后生效：${restartNames.join('、')}。当前未执行重启。` : '',
      ].filter(Boolean).join(' ');
    }
  }

  function renderConfig(data, options = {}) {
    const pending = options.draft || collectConfigPatch();
    const config = data.config || {};
    const preserveEditing = !options.force && state.configBaseline && Object.keys(pending).length > 0;
    state.config = config;
    state.configSchema = data.schema || state.configSchema || {};
    state.configVersions = data.versions || [];
    state.configError = '';
    if (preserveEditing) {
      state.configRemoteUpdate = Object.entries(config).some(([key, value]) =>
        !configValueEqual(value, state.configBaseline[key], state.configSchema[key]));
      updateConfigFeedback();
      return;
    }
    const host = $('fx-config-form');
    const advancedOpen = $('fx-config-advanced')?.open;
    state.configBaseline = { ...config };
    state.configRemoteUpdate = false;
    if (host) {
      const groups = { common: {}, advanced: {} };
      const values = { ...config, ...pending };
      Object.entries(state.configSchema).forEach(([key, spec]) => {
        const common = !spec.inactive && (spec.group === '模型' ||
          ['adsPowerSilentMode', 'googleFxSequenceUserId', 'googleFxSequenceUserLock'].includes(key));
        const bucket = groups[common ? 'common' : 'advanced'];
        const group = spec.group || '其它';
        (bucket[group] = bucket[group] || []).push([key, spec]);
      });
      const renderGroups = (bucket) => Object.entries(bucket).map(([group, fields]) => `
        <fieldset class="fx-config-group"><legend>${esc(group)}</legend>
          ${fields.map(([key, spec]) => configFieldHtml(key, spec, values[key] ?? spec.default)).join('')}
        </fieldset>`).join('');
      host.innerHTML = `<div class="fx-config-common"><h5>常用配置</h5><div class="fx-config-form">${renderGroups(groups.common)}</div></div>`
        + `<details id="fx-config-advanced" class="fx-config-advanced"${advancedOpen ? ' open' : ''}><summary>高级参数 <span>连接、号池策略、超时、节奏、去重与调试</span></summary>
          <div class="fx-config-form">${renderGroups(groups.advanced)}</div></details>`;
    }
    if (!options.force) state.configMessage = '已读取服务器配置；自动刷新不会覆盖未保存的修改。';
    updateConfigFeedback();
  }

  function validConfigPayload(data) {
    const config = data?.config;
    const schema = data?.schema || state.configSchema;
    if (!config || typeof config !== 'object' || Array.isArray(config) || !schema || typeof schema !== 'object') return false;
    const keys = Object.keys(schema).filter(key => !schema[key].inactive);
    return keys.length > 0 && keys.every(key => Object.hasOwn(config, key));
  }

  async function loadConfig(options = {}) {
    if (state.configSaving) return;
    const sequence = ++state.configLoadSeq;
    const beforeReload = options.force ? readConfigValues() : null;
    try {
      const data = await api('/api/google-fx/config');
      if (sequence !== state.configLoadSeq || state.configSaving) return;
      if (!validConfigPayload(data)) throw new Error('服务器未返回有效配置');
      const renderOptions = beforeReload
        ? { ...options, draft: configDelta(readConfigValues(), beforeReload) } : options;
      renderConfig(data, renderOptions);
    } catch (error) {
      if (sequence !== state.configLoadSeq) return;
      state.configError = `配置读取失败：${error.message}。页面中的编辑已保留。`;
      updateConfigFeedback();
      showToast(`配置读取失败：${error.message}`, true);
    }
  }

  // 把 FX 控制台保存的模型设置同步到主界面的 localStorage（spark_config），
  // 避免主界面仍然发送旧模型覆盖服务端的最新选择。两个页面（index.html /
  // console.html）共享 localStorage，主页面刷新或跨标签页 storage 事件都会
  // 自动加载新值。
  const _FX_MODEL_SYNC_KEYS = ['videoModel', 'googleFxImageModel', 'videoDuration', 'videoResolution', 'videoRefMode'];

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
    if (state.configSaving) return;
    const patch = collectConfigPatch();
    if (!Object.keys(patch).length) {
      state.configMessage = '配置没有变化。';
      updateConfigFeedback();
      return;
    }
    const submittedValues = readConfigValues();
    for (const element of document.querySelectorAll('#fx-config-form [data-config-key]')) {
      if (Object.hasOwn(patch, element.dataset.configKey) && typeof element.reportValidity === 'function' && !element.reportValidity()) return;
    }
    state.configSaving = true;
    state.configError = '';
    ++state.configLoadSeq; // A GET started before this save cannot restore the old configuration.
    updateConfigFeedback();
    try {
      const result = await post('/api/google-fx/config', { patch });
      if (!validConfigPayload(result)) throw new Error('服务器未返回保存结果，请重试');
      const laterEdits = configDelta(readConfigValues(), submittedValues);
      state.configRestartRequired = [...new Set([...state.configRestartRequired, ...(result.restart_required || [])])];
      state.configMessage = Object.keys(result.changed || {}).length
        ? '本次修改已保存。可即时更新的设置已应用，当前任务不会重新开始。'
        : '服务器中已是相同配置。';
      if ((result.inert || []).length) state.configMessage += ` ${result.inert.join('；')}`;
      renderConfig({ config: result.config, schema: state.configSchema, versions: result.versions },
        { force: true, draft: laterEdits });
      syncFxModelToMainConfig(result.config);
      await refresh(true);
      showToast(state.configRestartRequired.length ? '配置已保存，部分设置需重启后生效' : '配置已保存');
    } catch (error) {
      state.configError = `保存失败：${error.message}。修改已保留，请重试。`;
      showToast(`保存失败：${error.message}`, true);
    } finally {
      state.configSaving = false;
      updateConfigFeedback();
    }
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

  // The API returns a rolling tail. Freeze the displayed tail while reading older lines.
  function countNewLogLines(previous, next) {
    if (!previous.length) return next.length;
    for (let overlap = Math.min(previous.length, next.length); overlap > 0; overlap--) {
      if (previous.slice(-overlap).every((line, index) => line === next[index])) return next.length - overlap;
    }
    return next.length;
  }

  function updateLogReaderControls() {
    const latest = $('fx-log-latest');
    if (latest) {
      latest.hidden = state.logFollow;
      const added = state.logPendingLines ? countNewLogLines(state.logRenderedLines, state.logPendingLines) : 0;
      latest.textContent = added ? `有新日志 · 回到最新` : '回到最新';
    }
    const context = $('fx-log-context');
    if (context) {
      const scope = state.logTaskFilter ? `${state.logAutoTask ? '当前任务' : '指定任务'}：${state.logTaskFilter}`
        : state.logAutoTask ? '暂无运行任务，显示最近日志' : '全部 FX 日志';
      const levelLabel = { all: '全部级别', warning: '警告和错误', error: '仅错误' }[state.logLevel];
      const reading = state.logFollow ? '跟随最新' : '暂停跟随';
      const count = state.logRenderedRecords.length
        ? `${state.logRenderedRecords.length} 条记录` : `${state.logRenderedLines.length} 行`;
      context.textContent = state.logError ? `${scope} · 日志同步失败，将自动重试`
        : `${scope} · ${levelLabel} · ${count} · ${reading}`;
      context.title = state.logError || scope;
    }
    $('fx-log-current')?.setAttribute('aria-pressed', String(state.logAutoTask));
    $('fx-log-all')?.setAttribute('aria-pressed', String(!state.logAutoTask && !state.logTaskFilter));
    const pause = $('fx-log-pause');
    if (pause) {
      pause.textContent = state.logFollow ? '暂停跟随' : '继续跟随';
      pause.setAttribute('aria-pressed', String(!state.logFollow));
    }
    const copy = $('fx-log-copy');
    if (copy) copy.disabled = !state.logRenderedLines.length;
  }

  function renderLogLines(lines, records = []) {
    const host = $('fx-log-view');
    if (!host) return;
    state.logRenderedLines = lines;
    state.logRenderedRecords = records;
    state.logPendingLines = null;
    state.logPendingRecords = null;
    const text = lines.length ? lines.join('\n') : '（没有匹配的日志行）';
    if (records.length) {
      const markup = records.map(record => {
        const level = ['error', 'warning', 'info', 'debug'].includes(record.level) ? record.level : 'info';
        return `<span class="fx-log-record" data-level="${level}">${esc(record.lines.join('\n'))}</span>`;
      }).join('\n');
      if (host.innerHTML !== markup) host.innerHTML = markup;
    } else if (host.textContent !== text) host.textContent = text;
    if (state.logFollow) host.scrollTop = host.scrollHeight;
    updateLogReaderControls();
  }

  function setLogFilter(taskId, automatic = false) {
    state.logAutoTask = automatic;
    state.logTaskFilter = String(taskId || '');
    const input = $('fx-log-task');
    if (input) input.value = state.logTaskFilter;
    updateLogReaderControls();
  }

  function syncCurrentLogTask() {
    if (!state.logAutoTask || !state.logFollow) return;
    const input = $('fx-log-task');
    if (input && String(input.value || '').trim() !== state.logTaskFilter.trim()) return;
    const taskId = currentLogTask(state.lastTasks, state.lastQueue);
    // Keep the just-finished task in view until another task starts.
    if (taskId && taskId !== state.logTaskFilter) {
      setLogFilter(taskId, true);
      loadLogs();
    }
  }

  function resumeLogFollowing() {
    state.logPaused = false;
    state.logFollow = true;
    const taskId = state.logAutoTask && currentLogTask(state.lastTasks, state.lastQueue);
    if (taskId && taskId !== state.logTaskFilter) {
      setLogFilter(taskId, true);
      loadLogs();
    } else {
      renderLogLines(state.logPendingLines || state.logRenderedLines, state.logPendingRecords || state.logRenderedRecords);
    }
  }

  function setupLogReader() {
    const host = $('fx-log-view');
    const toolbar = $('fx-log-refresh')?.parentElement;
    if (!host || !toolbar || $('fx-log-latest')) return;
    const controls = [
      ['fx-log-current', '跟随当前任务', () => {
        state.logPaused = false;
        state.logFollow = true;
        setLogFilter(currentLogTask(state.lastTasks, state.lastQueue), true);
        loadLogs();
      }],
      ['fx-log-all', '全部日志', () => { setLogFilter(''); loadLogs(); }],
      ['fx-log-latest', '回到最新', resumeLogFollowing],
      ['fx-log-pause', '暂停跟随', () => {
        if (!state.logFollow) { resumeLogFollowing(); return; }
        state.logPaused = true;
        state.logFollow = false;
        updateLogReaderControls();
      }],
      ['fx-log-copy', '复制当前结果', async () => {
        if (!state.logRenderedLines.length) return;
        try {
          await global.navigator.clipboard.writeText(state.logRenderedLines.join('\n'));
          showToast('已复制当前显示的日志');
        } catch (_) { showToast('复制失败，请选中日志文字后复制', true); }
      }],
    ];
    controls.forEach(([id, label, action]) => {
      const button = document.createElement('button');
      button.id = id;
      button.type = 'button';
      button.className = 'fx-button';
      button.textContent = label;
      button.addEventListener('click', action);
      const destination = ['fx-log-current', 'fx-log-all'].includes(id) ? $('fx-log-scope-controls') || toolbar : toolbar;
      destination.appendChild(button);
    });
    const level = document.createElement('select');
    level.id = 'fx-log-level';
    level.className = 'fx-select';
    level.setAttribute('aria-label', '日志级别');
    level.innerHTML = '<option value="all">全部级别</option><option value="warning">警告和错误</option><option value="error">仅错误</option>';
    level.value = state.logLevel;
    level.addEventListener('change', () => {
      state.logLevel = ['all', 'warning', 'error'].includes(level.value) ? level.value : 'all';
      loadLogs();
    });
    toolbar.insertBefore(level, $('fx-log-refresh'));
    const context = document.createElement('p');
    context.id = 'fx-log-context';
    context.setAttribute('aria-live', 'polite');
    host.parentElement.insertBefore(context, host);
    host.addEventListener('scroll', () => {
      state.logFollow = !state.logPaused && host.scrollHeight - host.clientHeight - host.scrollTop < 24;
      if (state.logFollow && state.logPendingLines) renderLogLines(state.logPendingLines, state.logPendingRecords || []);
      else updateLogReaderControls();
    }, { passive: true });
    updateLogReaderControls();
  }

  async function loadLogs() {
    const host = $('fx-log-view');
    if (!host) return;
    const params = new URLSearchParams({ limit: '150' });
    params.set('level', state.logLevel);
    if (state.logTaskFilter) params.set('task_id', state.logTaskFilter);
    if (state.logKeyword) params.set('q', state.logKeyword);
    const queryKey = params.toString();
    if (queryKey !== state.logQueryKey) {
      state.logQueryKey = queryKey;
      state.logFollow = true;
      state.logPaused = false;
      state.logPendingLines = null;
      state.logPendingRecords = null;
      state.logRenderedLines = [];
      state.logRenderedRecords = [];
      state.logError = '';
      host.textContent = '正在读取…';
      updateLogReaderControls();
    }
    const requestId = ++state.logRequestSeq;
    try {
      const data = await api(`/api/google-fx/logs?${queryKey}`);
      if (requestId !== state.logRequestSeq || queryKey !== state.logQueryKey) return;
      state.logError = '';
      const displayLine = value => String(value).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
      const lines = Array.isArray(data.lines) ? data.lines.map(displayLine) : [];
      const records = Array.isArray(data.records) ? data.records
        .filter(record => record && Array.isArray(record.lines))
        .map(record => ({ level: record.level, lines: record.lines.map(displayLine) })) : [];
      if (state.logFollow) renderLogLines(lines, records);
      else {
        state.logPendingLines = lines;
        state.logPendingRecords = records;
        updateLogReaderControls();
      }
    } catch (error) {
      if (requestId !== state.logRequestSeq) return;
      state.logError = error.message;
      if (!state.logRenderedLines.length) host.textContent = `日志同步失败：${error.message}`;
      updateLogReaderControls();
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
  // 内容来自 docs/guides/google_fx_console_manual.md。这里只做够用的 Markdown 渲染：
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
      const url = force ? '/api/account-pool/adspower-profiles?force=1' : '/api/account-pool/adspower-profiles';
      const data = await api(url, { timeout: 10000 });
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
    showSection('monitor');
    if (!taskId) return;
    setLogFilter(taskId);
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
    $('fx-console-root')?.addEventListener('click', (event) => {
      const link = event.target.closest('[data-fx-section]');
      if (!link) return;
      event.preventDefault();
      showSection(link.dataset.fxSection, link.dataset.fxTarget);
    });
    document.querySelector('.fx-section-nav')?.addEventListener('keydown', (event) => {
      const tabs = Array.from(document.querySelectorAll('.fx-section-tab'));
      const index = tabs.indexOf(event.target);
      if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      showSection(tabs[next].dataset.fxSection);
      tabs[next].focus();
    });
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

    $('fx-config-save')?.addEventListener('click', saveConfig);
    const configChanged = () => {
      state.configError = '';
      updateConfigFeedback();
    };
    $('fx-config-form')?.addEventListener('input', configChanged);
    $('fx-config-form')?.addEventListener('change', configChanged);
    $('fx-config-reload')?.addEventListener('click', () => {
      const dirty = Object.keys(collectConfigPatch()).length > 0;
      if (dirty && !global.confirm('放弃当前未保存的配置修改，并重新读取服务器配置？')) return;
      loadConfig({ force: true, draft: {} });
    });
    global.addEventListener?.('beforeunload', (event) => {
      if (!Object.keys(collectConfigPatch()).length) return;
      event.preventDefault();
      event.returnValue = '';
    });
    $('fx-audit-details')?.addEventListener('toggle', (event) => {
      if (event.currentTarget.open) loadAudit();
    });
    $('fx-audit-refresh')?.addEventListener('click', loadAudit);

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
    $('fx-clear-captures')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!global.confirm('确定要清空所有失败现场及本地文件吗？\n此操作将物理删除 runtime/fx_debug/ 下的所有截图与快照数据且不可恢复。')) return;
      button.disabled = true;
      try {
        const data = await post('/api/google-fx/captures/clear', {});
        showToast(data.message || '已清空失败现场数据');
        await loadCaptures();
      } catch (error) {
        showToast(`清空失败：${error.message}`, true);
      } finally {
        button.disabled = false;
      }
    });
    $('fx-reload-captures')?.addEventListener('click', loadCaptures);
    setupLogReader();
    $('fx-log-refresh')?.addEventListener('click', () => triggerHighFreq(15000));
    $('fx-log-task')?.addEventListener('change', (event) => {
      setLogFilter(event.target.value.trim());
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

    $('fx-account-select-all')?.addEventListener('change', (event) => {
      const checked = event.target.checked;
      if (!state.selectedAccounts) state.selectedAccounts = new Set();
      if (checked) {
        (state.accounts || []).forEach(a => state.selectedAccounts.add(a.user_id));
      } else {
        state.selectedAccounts.clear();
      }
      renderAccounts(state.accounts || []);
    });

    $('fx-account-table-body')?.addEventListener('click', (event) => {
      const chk = event.target.closest('.fx-account-select');
      if (chk) {
        const uid = chk.dataset.userId;
        if (!uid) return;
        if (!state.selectedAccounts) state.selectedAccounts = new Set();
        const sorted = sortAccounts(state.accounts || [], state.accountSortKey);
        const idx = sorted.findIndex(a => a.user_id === uid);
        if (event.shiftKey && state.lastAccountClickedIndex >= 0 && state.lastAccountClickedIndex !== idx) {
          const start = Math.min(state.lastAccountClickedIndex, idx);
          const end = Math.max(state.lastAccountClickedIndex, idx);
          const shouldCheck = chk.checked;
          for (let i = start; i <= end; i++) {
            const a = sorted[i];
            if (!a) continue;
            if (shouldCheck) state.selectedAccounts.add(a.user_id);
            else state.selectedAccounts.delete(a.user_id);
          }
          renderAccounts(state.accounts || []);
          return;
        }
        state.lastAccountClickedIndex = idx;
        if (chk.checked) state.selectedAccounts.add(uid);
        else state.selectedAccounts.delete(uid);
        chk.closest('tr')?.classList.toggle('fx-row-selected', chk.checked);
        updateConsoleAccountBulkBar();
        return;
      }
      const button = event.target.closest('button[data-action]');
      if (button) handleAccountAction(button);
    });

    $('fx-account-bulk-clear')?.addEventListener('click', () => {
      if (state.selectedAccounts) state.selectedAccounts.clear();
      renderAccounts(state.accounts || []);
    });

    $('fx-account-bulk-priority')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const currentList = Array.isArray(state.config?.googleFxPriorityUserIds)
        ? state.config.googleFxPriorityUserIds.map(String)
        : [];
      const currentSet = new Set(currentList);
      uids.forEach(u => currentSet.add(u));
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/google-fx/config', { patch: { googleFxPriorityUserIds: Array.from(currentSet) } });
        await refresh(true);
        showToast(`已将 ${uids.length} 个账号设为优先级实例`);
      } catch (e) {
        showToast(`设为优先失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-unpriority')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const currentList = Array.isArray(state.config?.googleFxPriorityUserIds)
        ? state.config.googleFxPriorityUserIds.map(String)
        : [];
      const currentSet = new Set(currentList);
      uids.forEach(u => currentSet.delete(u));
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/google-fx/config', { patch: { googleFxPriorityUserIds: Array.from(currentSet) } });
        await refresh(true);
        showToast(`已取消 ${uids.length} 个账号的优先级`);
      } catch (e) {
        showToast(`取消优先失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-expires')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const val = global.prompt(`批量设置重置日期：\n请输入要应用到选中的 ${uids.length} 个账号的重置日期 (格式: YYYY-MM-DD，留空清空)：`, '');
      if (val === null) return;
      const expDate = val.trim() || null;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const res = await post('/api/account-pool/expires-at', { user_ids: uids, expires_at: expDate });
        await refresh(true);
        showToast(`已成功为 ${res.updated_count || uids.length} 个账号更新重置日期`);
      } catch (e) {
        showToast(`批量设置重置日期失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-enable')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/account-pool/toggle', { user_ids: uids, disabled: false });
        await refresh(true);
        showToast(`已批量启用 ${uids.length} 个账号`);
      } catch (e) {
        showToast(`批量启用失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-disable')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/account-pool/toggle', { user_ids: uids, disabled: true });
        await refresh(true);
        showToast(`已批量禁用 ${uids.length} 个账号`);
      } catch (e) {
        showToast(`批量禁用失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-password')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const pwd = global.prompt(`批量设置密码：\n请输入要应用到选中的 ${uids.length} 个账号的统一密码：`, 'Sharpal2025');
      if (pwd === null) return;
      const password = pwd.trim();
      if (!password) {
        showToast('密码不能为空', true);
        return;
      }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const res = await post('/api/account-pool/credentials', { user_ids: uids, password: password });
        await refresh(true);
        showToast(`已为 ${res.count || uids.length} 个账号统一更新登录密码`);
      } catch (e) {
        showToast(`批量设置密码失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-close')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/account-pool/close-browser', { user_ids: uids });
        showToast(`已向 ${uids.length} 个账号发送关闭浏览器指令`);
      } catch (e) {
        showToast(`批量关闭失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-delete')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      if (!global.confirm(`确定要从号池移除选中的 ${uids.length} 个账号吗？（不影响 AdsPower 里的浏览器环境）`)) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post('/api/account-pool/delete', { user_ids: uids });
        if (state.selectedAccounts) state.selectedAccounts.clear();
        state.profilesLoaded = false;
        await refresh(true);
        await loadProfiles(true);
        showToast(`已成功移除 ${uids.length} 个账号`);
      } catch (e) {
        showToast(`批量移除失败：${e.message}`, true);
      } finally {
        button.disabled = false;
      }
    });

    $('fx-account-bulk-refresh')?.addEventListener('click', async (event) => {
      const uids = Array.from(state.selectedAccounts || []);
      if (!uids.length) return;
      const button = event.currentTarget;
      const originalText = button.textContent;
      button.disabled = true;
      let okCount = 0;
      let failCount = 0;
      for (let i = 0; i < uids.length; i++) {
        button.textContent = `⏳ 探测中 (${i + 1}/${uids.length})...`;
        try {
          await post('/api/account-pool/refresh', { user_id: uids[i] });
          okCount++;
        } catch (e) {
          failCount++;
        }
      }
      button.disabled = false;
      button.textContent = originalText;
      await refresh(true);
      showToast(`批量刷新完成：成功 ${okCount} 个${failCount > 0 ? `，跳过/失败 ${failCount} 个` : ''}`);
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

    $('fx-board')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-board-action]');
      if (!button) return;
      if (button.dataset.boardAction === 'view_logs') {
        if (button.dataset.taskId) filterAndFocusLogs(button.dataset.taskId);
      } else if (button.dataset.boardAction === 'close_orphans') {
        if (!global.confirm('关闭所有「无主」浏览器？\n只会关闭没有任务在用、且近 30 分钟没被用过的环境；账号登录状态不受影响。')) return;
        button.disabled = true;
        try {
          const result = await post('/api/google-fx/orphans/close', {});
          showToast(`已关闭 ${result.closed || 0} 个无主浏览器`);
          await refresh(true);
        } catch (error) { showToast(`关闭失败：${error.message}`, true); }
        finally { button.disabled = false; }
      }
    });

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
      // 动态高频轮询模式：1.5 秒更新一次日志
      await loadLogs();
      scheduleNextTick(1500);
    } else {
      // 空闲模式：每 3 个 6 秒周期（约 18 秒）更新一次日志
      if (tickCount % 3 === 0) {
        await loadLogs();
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
    refresh(true).catch((e) => console.warn('FX console refresh failed:', e));
    loadConfig().catch((e) => console.warn('FX console loadConfig failed:', e));
    loadProfiles(false).catch((e) => console.warn('FX console loadProfiles failed:', e));
    loadProxies().catch((e) => console.warn('FX console loadProxies failed:', e));
    loadCaptures().catch((e) => console.warn('FX console loadCaptures failed:', e));
    loadLogs().catch((e) => console.warn('FX console loadLogs failed:', e));
    scheduleNextTick(1500);
  }

  function deactivate() {
    state.active = false;
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
  }

  const apiObject = {
    init, activate, deactivate, refresh, showSection, accountState, proxyState, taskTypeLabel,
    creditLabel, formatDuration, taskState, stageLabel, currentLogTask, countNewLogLines, runSelftest, runSelectorProbe,
    renderMarkdown, openManual, closeManual, loadProxies
  };
  global.GoogleFxConsole = apiObject;
  if (typeof module !== 'undefined' && module.exports) module.exports = apiObject;
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', init);
})(typeof window !== 'undefined' ? window : globalThis);
