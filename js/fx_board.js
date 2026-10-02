/* ============================================================
   并发作战板（P0：只读）
   —— 数据来自 /api/google-fx/status 里的 board（或 /api/google-fx/board）。
   设计与规则见 docs/plans/multi_project_concurrency_plan.md §六。
   本文件分两层：buildModel* 是纯函数（便于单测，不碰 DOM），render 只负责拼 HTML。
   P0 只有一个浏览器名额，泳道上的占用是串行的；字段已按并发契约给出，
   后续期次放开并发后前端不需要改。
   ============================================================ */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FxBoard = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const WINDOW_SECONDS = 30 * 60;
  const MAX_LANES = 8;
  const MIN_BAR_PERCENT = 0.8;
  // 项目色相：只取一组预设，保证在暖纸色底/深色底上都有足够对比度；琥珀(40)留给告警，不分配给项目。
  const HUES = [190, 275, 130, 330, 220, 160, 300, 15];

  const STAGE_LABELS = { frames: '帧', videos: '视频' };
  const KIND_LABELS = {
    frames: '帧', videos: '视频', credit_probe: '积分探针', selector_probe: '选择器探针',
    selftest: '环境自检', auto_login: '自动登录',
  };
  const STAGE_STATE = {
    running: { icon: '▶', label: '运行中', cls: 'run' },
    running_idle: { icon: '▶', label: '进行中', cls: 'run' },
    waiting: { icon: '⏳', label: '排队·等浏览器', cls: 'wait' },
    done: { icon: '✓', label: '完成', cls: 'done' },
    failed: { icon: '✕', label: '失败', cls: 'fail' },
    cancelled: { icon: '—', label: '已取消', cls: 'todo' },
  };
  const ACCOUNT_STATE = {
    ready: '可用', unprobed: '未探测', low_credit: '积分不足', cooling: '冷却中', disabled: '已禁用',
  };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  // 同一个项目在任何页面、任何时刻都是同一个色相：按 project_key 做稳定哈希。
  function hueFor(projectKey) {
    const key = String(projectKey || '');
    if (!key) return null;
    let hash = 0;
    for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return HUES[hash % HUES.length];
  }

  function stageLabel(row) {
    return STAGE_LABELS[row.stage] || KIND_LABELS[row.kind] || row.kind || '任务';
  }

  function rowLabel(row) {
    return row.project_label || KIND_LABELS[row.kind] || row.task_id || '任务';
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes}m${String(total % 60).padStart(2, '0')}s`;
    return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}m`;
  }

  // ── 泳道 ─────────────────────────────────────────────

  function buildLanes(board) {
    const now = Number(board.server_ts) || Date.now() / 1000;
    const from = now - WINDOW_SECONDS;
    const accounts = board.accounts || [];
    const byUser = new Map(accounts.map((row) => [row.user_id, row]));
    const lanes = new Map();

    function laneFor(row) {
      const userId = String(row.user_id || row.account_pin || '');
      if (!lanes.has(userId)) {
        const known = byUser.get(userId);
        lanes.set(userId, {
          key: userId,
          label: row.account_label || (known && known.label) || (userId ? userId : '账号未识别'),
          credit: known ? known.credit : null,
          accountState: known ? known.state : null,
          segments: [],
          lastEnd: 0,
        });
      }
      return lanes.get(userId);
    }

    function addSegment(row, start, end, open) {
      const clippedStart = Math.max(start, from);
      if (end <= from) return;
      const lane = laneFor(row);
      const left = ((clippedStart - from) / WINDOW_SECONDS) * 100;
      const width = Math.max(MIN_BAR_PERCENT, ((end - clippedStart) / WINDOW_SECONDS) * 100);
      const state = open ? (row.state === 'stalled' ? 'stall' : row.state === 'force_released' ? 'cancelled' : 'run')
        : (row.outcome === 'error' ? 'error' : row.outcome === 'cancelled' || row.outcome === 'force_released' ? 'cancelled' : 'done');
      lane.segments.push({
        task_id: row.task_id,
        title: rowLabel(row),
        sub: `${stageLabel(row)} · ${open ? '占用' : ''}${formatDuration(end - start)}`,
        left: Math.min(left, 100 - MIN_BAR_PERCENT),
        width: Math.min(width, 100 - left),
        state,
        open,
        hue: hueFor(row.project_key),
      });
      lane.lastEnd = Math.max(lane.lastEnd, end);
    }

    (board.history || []).forEach((row) => addSegment(row, row.started_ts, row.ended_ts, false));
    (board.leases || []).forEach((row) => addSegment(row, row.started_ts, now, true));

    // 有活动的账号先排（最近占用的在前），再补"空闲可用"的账号，最多 MAX_LANES 行。
    const ordered = [...lanes.values()].sort((a, b) => b.lastEnd - a.lastEnd);
    accounts
      .filter((row) => row.state === 'ready' && !lanes.has(row.user_id))
      .forEach((row) => {
        ordered.push({ key: row.user_id, label: row.label, credit: row.credit, accountState: row.state, segments: [], lastEnd: 0 });
      });
    const visible = ordered.slice(0, MAX_LANES);
    return { lanes: visible, hidden: Math.max(0, ordered.length - visible.length), now, from };
  }

  // ── 项目 × 阶段 ───────────────────────────────────────

  function buildMatrix(board) {
    return (board.projects || []).map((project) => ({
      key: project.project_key || project.label,
      label: project.label,
      hue: hueFor(project.project_key || project.label),
      cells: ['frames', 'videos'].map((stage) => {
        const cell = (project.stages || {})[stage];
        const meta = cell ? (STAGE_STATE[cell.state] || { icon: '·', label: cell.state, cls: 'todo' }) : null;
        return { stage, task_id: cell ? cell.task_id : '', ...(meta || { icon: '', label: '—', cls: 'none' }) };
      }),
    }));
  }

  // ── 等待与冲突 ─────────────────────────────────────────

  function accountLabelOf(board, userId) {
    const row = (board.accounts || []).find((item) => item.user_id === userId);
    return row ? row.label : (userId || '账号未识别');
  }

  function buildRadar(board) {
    const items = [];
    const holders = new Map((board.leases || []).map((row) => [row.task_id, row]));

    // 疑似卡死：只是标记，不自动处理。排在最前面，因为它会让后面的任务一直排队。
    (board.leases || []).filter((row) => row.state === 'stalled').forEach((row) => {
      items.push({
        cls: 'stalled', icon: '⚠', tag: 'stalled',
        text: `「${rowLabel(row)}·${stageLabel(row)}」已 ${formatDuration(row.heartbeat_age_seconds)} 没有任何日志，占着账号 ${esc(accountLabelOf(board, row.user_id || row.account_pin))}`,
        why: '这只是标记，系统不会自动处理。确认卡死后可取消该任务，或在下方「当前浏览器任务」里强制释放。',
        actions: [{ action: 'view_logs', label: '查看日志', taskId: row.task_id }],
      });
    });

    // 已被强制释放、但底层线程还没退出的租约：名额仍被占着，别让人以为空出来了。
    (board.leases || []).filter((row) => row.state === 'force_released').forEach((row) => {
      items.push({
        cls: 'stalled', icon: '⛔', tag: '已强制释放',
        text: `「${rowLabel(row)}·${stageLabel(row)}」已被强制释放，但它的线程还没退出`,
        why: '为避免两个任务同时操作同一个浏览器，这个名额要等线程真正退出后才会空出来。',
        actions: [{ action: 'view_logs', label: '查看日志', taskId: row.task_id }],
      });
    });

    (board.waiting || []).forEach((row) => {
      const reasons = [];
      let icon = '⏳';
      let tag = '等名额';
      (row.blocked_by || []).forEach((item) => {
        const holder = holders.get(item.holder_task);
        const who = holder ? `「${rowLabel(holder)}·${stageLabel(holder)}」` : '其它任务';
        if (item.type === 'capacity') {
          reasons.push(`浏览器名额已满（${(board.capacity || {}).used}/${(board.capacity || {}).max}），${who}占用中`);
        } else if (item.type === 'account') {
          icon = '🔑'; tag = '账号被占';
          reasons.push(`它要用的账号正被${who}使用（R1：同一个账号同时只能一个任务用）`);
        } else if (item.type === 'ip') {
          icon = '🌐'; tag = '同出口';
          reasons.push(`它的出口与${who}相同（R5：同出口的账号不能同时在线，会触发关联风控）`);
        } else if (item.type === 'project') {
          icon = '🔒'; tag = item.enforced === false ? '项目互斥·观察' : '项目互斥';
          reasons.push(item.enforced === false
            ? `同项目的其它阶段也在进行（R2：目前只提示）`
            : `同项目的${who}正在进行（R2：同一项目同一时刻只能一个任务写入）`);
        }
      });
      if (!reasons.length) reasons.push('正在等待调度');
      reasons.push(`已排队 ${formatDuration(row.waited_seconds)}，第 ${row.position} 位`);
      items.push({
        cls: 'wait', icon, tag,
        text: `「${rowLabel(row)}·${stageLabel(row)}」等待浏览器`,
        why: reasons.join(' · '),
        actions: [{ action: 'view_logs', label: '查看日志', taskId: row.task_id }],
      });
    });

    const orphans = (board.open_browsers || []).filter((row) => row.orphan);
    if (orphans.length) {
      items.push({
        cls: 'orphan', icon: '❔', tag: '无主',
        text: `${orphans.length} 个无主浏览器：${orphans.map((row) => row.label).join('、')}`,
        why: '开着、没有任务在用、近 30 分钟也没被用过，占着内存。确认后可一键关闭；正在被任务使用或刚用过的不会被关。',
        actions: [{ action: 'close_orphans', label: '关闭无主浏览器' }],
      });
    }

    const recovered = board.recovered;
    if (recovered) {
      items.push({
        cls: 'recovered', icon: '↺', tag: '已回收',
        text: `上次服务退出时，「${recovered.task_id}」仍占用着浏览器${recovered.user_id ? `（账号 ${esc(accountLabelOf(board, recovered.user_id))}）` : ''}`,
        why: '该任务已被打断，租约已回收。对应的浏览器可能仍开着，如无人使用会在上方显示为无主。',
        actions: [],
      });
    }
    return items;
  }

  function buildCapacity(board) {
    const capacity = board.capacity || { max: 1, used: 0 };
    return { max: capacity.max, used: capacity.used, full: capacity.used >= capacity.max };
  }

  function buildModel(board) {
    const safe = board || {};
    return {
      capacity: buildCapacity(safe),
      lanes: buildLanes(safe),
      matrix: buildMatrix(safe),
      radar: buildRadar(safe),
      accounts: safe.accounts || [],
      openBrowsers: safe.open_browsers || [],
      waitingCount: (safe.waiting || []).length,
      stalledCount: (safe.leases || []).filter((row) => row.state === 'stalled').length,
      orphanCount: (safe.open_browsers || []).filter((row) => row.orphan).length,
    };
  }

  // ── 渲染 ─────────────────────────────────────────────

  function tickLabels() {
    return ['−30m', '−20m', '−10m', '现在'].map((text, index) => {
      const left = [0, 33.3, 66.6, 100][index];
      return `<span style="left:${left}%">${text}</span>`;
    }).join('');
  }

  function renderSegment(seg) {
    const style = seg.hue === null ? '' : `--h:${seg.hue};`;
    const kindClass = seg.hue === null ? ' sys' : '';
    return `<button type="button" class="fxb-bar ${seg.state}${kindClass}" style="${style}left:${seg.left.toFixed(2)}%;width:${seg.width.toFixed(2)}%"
      data-board-action="view_logs" data-task-id="${esc(seg.task_id)}" title="${esc(seg.title)} · ${esc(seg.sub)}（点击查看日志）">
      <b>${esc(seg.title)}</b><span>${esc(seg.sub)}</span></button>`;
  }

  function renderLanes(model) {
    const { lanes, hidden } = model.lanes;
    if (!lanes.length) {
      return '<div class="fx-empty">最近 30 分钟没有浏览器任务，号池里也没有可用账号。</div>';
    }
    const rows = lanes.map((lane) => {
      const credit = lane.credit === null || lane.credit === undefined ? '' : ` · 积分 ${esc(lane.credit)}`;
      const stateNote = lane.accountState && lane.accountState !== 'ready'
        ? `<div class="fxb-sub">${esc(ACCOUNT_STATE[lane.accountState] || lane.accountState)}</div>` : '';
      const track = lane.segments.length
        ? lane.segments.map(renderSegment).join('')
        : '<div class="fxb-idle">空闲 · 可接单</div>';
      return `<div class="fxb-lane">
        <div class="fxb-who"><b>${esc(lane.label)}</b>${credit}${stateNote}</div>
        <div class="fxb-track">${track}</div></div>`;
    }).join('');
    const more = hidden ? `<div class="fxb-note">另有 ${hidden} 个账号未显示</div>` : '';
    return `<div class="fxb-scroll"><div class="fxb-grid">
      <div class="fxb-axis"><span></span><div class="fxb-ticks">${tickLabels()}</div></div>${rows}</div></div>${more}`;
  }

  function renderMatrix(model) {
    if (!model.matrix.length) return '<div class="fx-empty">当前没有进行中或刚结束的项目任务。</div>';
    const rows = model.matrix.map((project) => {
      const cells = project.cells.map((cell) => {
        if (cell.cls === 'none') return '<td><span class="fxb-chip todo">—</span></td>';
        const style = cell.cls === 'run' && project.hue !== null ? ` style="--h:${project.hue}"` : '';
        return `<td><button type="button" class="fxb-chip ${cell.cls}"${style} data-board-action="view_logs"
          data-task-id="${esc(cell.task_id)}" title="点击查看该任务日志">${cell.icon} ${esc(cell.label)}</button></td>`;
      }).join('');
      const dot = project.hue === null ? '' : `<i class="fxb-dot" style="--h:${project.hue}"></i>`;
      return `<tr><td class="fxb-proj">${dot}${esc(project.label)}</td>${cells}</tr>`;
    }).join('');
    return `<div class="fxb-scroll"><table class="fxb-table">
      <thead><tr><th>项目</th><th>帧</th><th>视频</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderRadar(model) {
    if (!model.radar.length) return '<div class="fx-empty">没有任务在排队，也没有冲突。</div>';
    return model.radar.map((item) => {
      const buttons = (item.actions || []).map((act) => (act.action === 'close_orphans'
        ? `<button type="button" class="fx-button fx-button-danger" data-board-action="close_orphans">${esc(act.label)}</button>`
        : `<button type="button" class="fx-button" data-board-action="${esc(act.action)}" data-task-id="${esc(act.taskId)}">${esc(act.label)}</button>`)).join('');
      return `<div class="fxb-item ${esc(item.cls || '')}">
        <div class="fxb-ico">${item.icon}</div>
        <div class="fxb-msg">${esc(item.text)}${item.tag ? `<span class="fxb-tag">${esc(item.tag)}</span>` : ''}</div>
        <div class="fxb-acts">${buttons}</div>
        <div class="fxb-why">${esc(item.why)}</div></div>`;
    }).join('');
  }

  function renderResources(model) {
    if (!model.accounts.length) return '<div class="fx-empty">号池为空。</div>';
    const open = new Map(model.openBrowsers.map((row) => [row.user_id, row]));
    const BROWSER_MARK = { leased: '● 占用中', warm: '○ 保温', orphan: '❔ 无主', idle: '○ 已打开' };
    return `<div class="fxb-res">${model.accounts.map((row) => {
      const off = row.state === 'ready' ? '' : ' off';
      const credit = row.credit === null || row.credit === undefined ? '未探测' : row.credit;
      const browser = open.get(row.user_id);
      const mark = browser ? ` · ${BROWSER_MARK[browser.leased ? 'leased' : browser.orphan ? 'orphan' : browser.warm ? 'warm' : 'idle']}` : '';
      return `<span class="fxb-acct${off}" title="${esc(row.name || row.user_id)}">${esc(row.label)} · ${esc(ACCOUNT_STATE[row.state] || row.state)} · 积分 ${esc(credit)}${mark}</span>`;
    }).join('')}</div>`;
  }

  function renderHtml(board) {
    const model = buildModel(board);
    const cap = model.capacity;
    const meter = Array.from({ length: Math.max(1, cap.max) }, (_, i) => `<i class="${i < cap.used ? 'on' : ''}"></i>`).join('');
    return `
      <div class="fxb-head">
        <span class="fxb-pill${cap.full ? ' full' : ''}" title="同时可用的浏览器名额">浏览器 <span class="fxb-meter">${meter}</span> ${cap.used} / ${cap.max}</span>
        <span class="fxb-pill">排队 ${model.waitingCount}</span>
        ${model.stalledCount ? `<span class="fxb-pill full">⚠ 疑似卡死 ${model.stalledCount}</span>` : ''}
        ${model.orphanCount ? `<span class="fxb-pill full">❔ 无主浏览器 ${model.orphanCount}</span>` : ''}
        <span class="fxb-note">${cap.max > 1 ? `并发模式：最多 ${cap.max} 个任务同时占用浏览器，每个任务独占一个环境` : '当前为单浏览器模式：占用是串行的（可在「配置与维护 → 并发」调整）'}</span>
      </div>
      <section class="fxb-sec"><h5>账号泳道 <small>谁占着哪个浏览器 · 最近 30 分钟 · 颜色 = 项目</small></h5>${renderLanes(model)}</section>
      <section class="fxb-sec"><h5>项目 × 阶段 <small>点击查看该任务日志</small></h5>${renderMatrix(model)}</section>
      <section class="fxb-sec"><h5>等待与冲突 <small>谁在等谁</small></h5>${renderRadar(model)}</section>
      <section class="fxb-sec"><h5>账号 <small>号池当前状态</small></h5>${renderResources(model)}</section>`;
  }

  function render(container, board) {
    if (!container) return;
    container.innerHTML = renderHtml(board);
  }

  return {
    WINDOW_SECONDS, hueFor, formatDuration, buildLanes, buildMatrix, buildRadar,
    buildCapacity, buildModel, renderHtml, render,
  };
}));
