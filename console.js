// SPARK Developer Console Frontend Controller

document.addEventListener('DOMContentLoaded', () => {
  // State management
  let serverManaged = false;
  let needsAccessCode = false;
  let activeTab = '';
  let dashboardPollInterval = null;
  let dashboardRequestPending = false;

  // Cache DOM elements
  const navItems = document.querySelectorAll('.nav-item');
  const tabContents = document.querySelectorAll('.tab-content');
  const pageTitleLabel = document.getElementById('page-title-label');
  const pageTitleDesc = document.getElementById('page-title-desc');
  const localTokenInput = document.getElementById('local-token-input');
  
  // Status badges
  const statusGatewayDot = document.getElementById('status-gateway-dot');
  const statusGatewayText = document.getElementById('status-gateway-text');
  const statusAppDot = document.getElementById('status-app-dot');
  const statusAppText = document.getElementById('status-app-text');
  const managedModeBadge = document.getElementById('managed-mode-badge');
  const managedModeText = document.getElementById('managed-mode-text');

  // Stats
  const statActiveTasks = document.getElementById('stat-active-tasks');
  const statRateMax = document.getElementById('stat-rate-max');
  const statServerMode = document.getElementById('stat-server-mode');
  const taskTableBody = document.getElementById('task-table-body');
  const taskMonitorSyncTime = document.getElementById('task-monitor-sync-time');

  // 1. Initial Setup
  const init = () => {
    // Load local token from localStorage
    const savedToken = localStorage.getItem('spark_dev_token');
    if (savedToken) {
      localTokenInput.value = savedToken;
    }

    // Bind token input changes
    localTokenInput.addEventListener('input', (e) => {
      try {
        localStorage.setItem('spark_dev_token', e.target.value.trim());
      } catch (_) {}
    });

    // Toggle token visibility
    const tokenToggleBtn = document.getElementById('token-toggle-btn');
    const tokenToggleIcon = document.getElementById('token-toggle-icon');
    if (tokenToggleBtn && tokenToggleIcon) {
      tokenToggleBtn.addEventListener('click', () => {
        if (localTokenInput.type === 'password') {
          localTokenInput.type = 'text';
          // Change to Eye Slash icon
          tokenToggleIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.815 7.815 3 3m-3-3a3 3 0 0 1-4.243-4.243m0 0-3.65-3.65m0 0a3 3 0 0 1 4.243 4.243m-4.243-4.243L16.5 16.5" />
          `;
        } else {
          localTokenInput.type = 'password';
          // Change back to Eye icon
          tokenToggleIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" />
            <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
          `;
        }
      });
    }

    // Copy tunnel URL
    const copyTunnelBtn = document.getElementById('btn-copy-tunnel-url');
    const tunnelUrlCode = document.getElementById('tunnel-url-code');
    if (copyTunnelBtn && tunnelUrlCode) {
      copyTunnelBtn.addEventListener('click', () => {
        const urlText = tunnelUrlCode.textContent.trim();
        if (urlText && urlText !== '-') {
          // copyText（js/utils.js）在 http 局域网环境下自动回退 execCommand，
          // 直接调 navigator.clipboard 在非安全上下文里是 undefined，会静默 TypeError
          copyText(urlText).then(() => {
            const originalText = copyTunnelBtn.innerHTML;
            copyTunnelBtn.innerHTML = `
              <span style="color:var(--success)">✓ 已复制</span>
            `;
            setTimeout(() => {
              copyTunnelBtn.innerHTML = originalText;
            }, 1500);
          }).catch(() => {
            copyTunnelBtn.innerHTML = `<span style="color:var(--danger, #f87171)">复制失败</span>`;
            setTimeout(() => { copyTunnelBtn.innerHTML = '复制'; }, 1500);
          });
        }
      });
    }

    // Clear system cache in DevPortal
    const btnClearSystemCache = document.getElementById('btn-clear-system-cache');
    if (btnClearSystemCache) {
      btnClearSystemCache.addEventListener('click', async () => {
        if (!confirm('确定要清理系统缓存（packet_cache.json）吗？这会清除已缓存的 LLM 激发数据。')) {
          return;
        }
        try {
          btnClearSystemCache.disabled = true;
          btnClearSystemCache.textContent = '清理中...';
          const resp = await fetch('/api/clear-cache', { method: 'POST' });
          const data = await resp.json();
          if (data.status === 'success') {
            alert('系统缓存清理成功！');
            checkSystemStatuses();
          } else {
            alert('清理失败: ' + data.message);
          }
        } catch (err) {
          alert('请求出错: ' + err.message);
        } finally {
          btnClearSystemCache.disabled = false;
          btnClearSystemCache.textContent = '清理缓存';
        }
      });
    }

    // Start background status checks.
    // 页面隐藏时暂停轮询（旧行为：4 个接口每 3 秒永久轮询，即使标签页在后台）
    checkSystemStatuses();
    const startDashboardPolling = () => {
      if (dashboardPollInterval) return;
      dashboardPollInterval = setInterval(checkSystemStatuses, 3000);
    };
    const stopDashboardPolling = () => {
      if (dashboardPollInterval) {
        clearInterval(dashboardPollInterval);
        dashboardPollInterval = null;
      }
    };
    startDashboardPolling();
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        stopDashboardPolling();
      } else {
        checkSystemStatuses();
        startDashboardPolling();
      }
    });

    // Bind documentation panels
    renderDocSnippets();

  };

  // 2. Tab Navigation
  navItems.forEach(item => {
    item.addEventListener('click', () => {
      const targetTab = item.getAttribute('data-tab');
      swapTab(targetTab);
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', `#${targetTab}`);
      }
    });
  });

  function swapTab(targetTab, force = false) {
    if (targetTab === activeTab && !force) return;

    // Update active nav item
    navItems.forEach(nav => {
      if (nav.getAttribute('data-tab') === targetTab) {
        nav.classList.add('active');
      } else {
        nav.classList.remove('active');
      }
    });

    // Swap tab visibility
    tabContents.forEach(content => content.classList.remove('active'));
    const activeContent = document.getElementById(`tab-${targetTab}`);
    if (activeContent) {
      activeContent.classList.add('active');
    }

    activeTab = targetTab;
    
    const labelMap = {
      'google-fx': 'Google FX 服务管理',
      'docs': 'API 交互文档',
    };
    const descMap = {
      'google-fx': '管理 Flow 自动化运行时、AdsPower 连接、账号池与浏览器执行状态',
      'docs': '查看 SPARK API 请求格式与调用示例',
    };
    pageTitleLabel.textContent = labelMap[activeTab] || '开发者中心';
    if (pageTitleDesc) pageTitleDesc.textContent = descMap[activeTab] || '';
    if (globalThis.GoogleFxConsole) {
      if (activeTab === 'google-fx') globalThis.GoogleFxConsole.activate();
      else globalThis.GoogleFxConsole.deactivate();
    }
  }

  const requestedTab = window.location.hash.replace(/^#/, '');
  const initialTab = (requestedTab && Array.from(navItems).some(item => item.getAttribute('data-tab') === requestedTab))
    ? requestedTab
    : 'google-fx';
  swapTab(initialTab, true);

  window.addEventListener('hashchange', () => {
    const hashTab = window.location.hash.replace(/^#/, '');
    if (hashTab && Array.from(navItems).some(item => item.getAttribute('data-tab') === hashTab)) {
      swapTab(hashTab);
    }
  });

  // 3. Status Checking & Dynamic Dashboard
  async function checkSystemStatuses() {
    if (document.hidden || dashboardRequestPending) return;
    dashboardRequestPending = true;
    try {
      const modeResp = await fetch('/api/mode');
      if (!modeResp.ok) throw new Error('API server unreachable');
      const modeData = await modeResp.json();
      
      serverManaged = modeData.server_managed;
      needsAccessCode = modeData.needs_access_code;

      if (modeData && modeData.runtime_version) {
        const rv = modeData.runtime_version;
        const policy = rv.policy_version || '';
        const match = policy.match(/(v\d+)/);
        const shortPolicy = match ? match[1] : (policy ? policy.slice(0, 10) : '');
        const commit = rv.git_commit_short || (rv.git_commit ? rv.git_commit.slice(0, 7) : '');
        const dirty = rv.git_dirty ? '*' : '';
        const label = shortPolicy && commit ? `${shortPolicy} · ${commit}${dirty}` : (shortPolicy || commit || 'v1.0');
        const tooltip = `SPARK 运行时版本信息:\n• 策略版本: ${policy || '默认'}\n• Git 提交: ${commit ? commit + (rv.git_dirty ? ' (有修改)' : ' (干净)') : '未知'}\n• 服务启动时间: ${rv.service_start_time ? new Date(rv.service_start_time * 1000).toLocaleString('zh-CN', { hour12: false }) : '未知'}\n• 代码状态: ${rv.stale ? '⚠️ 已过期 (需重启)' : '✓ 最新'}`;

        const badge = document.getElementById('spark-version-badge');
        if (badge) {
          badge.textContent = label;
          badge.style.display = 'inline-flex';
          badge.classList.toggle('stale', Boolean(rv.stale));
          badge.title = tooltip;
        }

        const badgeConsole = document.getElementById('spark-version-text-console');
        const statusConsole = document.getElementById('spark-version-status-console');
        if (badgeConsole && statusConsole) {
          badgeConsole.textContent = label;
          statusConsole.style.display = 'inline-flex';
          statusConsole.title = tooltip;
        }
      }

      if (serverManaged) {
        if (statServerMode) statServerMode.textContent = '托管模式 (Managed)';
        if (managedModeBadge) {
          managedModeBadge.style.background = 'rgba(138, 43, 226, 0.15)';
          managedModeBadge.style.borderColor = 'var(--secondary)';
        }
        if (managedModeText) managedModeText.innerHTML = '<span style="color:#a78bfa">● 托管模式 (Managed)</span>';
        
        if (localTokenInput) {
          localTokenInput.placeholder = '当前为服务端托管模式，密钥从服务端加载';
          localTokenInput.disabled = true;
        }
      } else {
        if (statServerMode) statServerMode.textContent = '本地模式 (Local)';
        if (managedModeBadge) {
          managedModeBadge.style.background = 'rgba(255, 255, 255, 0.03)';
          managedModeBadge.style.borderColor = 'var(--border-color)';
        }
        if (managedModeText) managedModeText.innerHTML = '<span>● 本地模式 (Local)</span>';
        if (localTokenInput) {
          localTokenInput.placeholder = '输入 User Token 进行测试...';
          localTokenInput.disabled = false;
        }
      }

      const devToken = localTokenInput ? localTokenInput.value.trim() : '';
      const configPayload = {
        config: {
          baseUrl: 'http://127.0.0.1:8046/v1',
          apiKey: serverManaged ? '' : devToken
        }
      };

      const pingResp = await fetch('/api/ping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configPayload)
      });
      
      const pingData = await pingResp.json();
      if (statusGatewayDot && statusGatewayText) {
        if (pingData.online) {
          statusGatewayDot.className = 'status-dot online';
          statusGatewayText.textContent = '在线 (8046)';
        } else {
          statusGatewayDot.className = 'status-dot offline';
          statusGatewayText.textContent = '无法连接到本地网关';
        }
      }

      if (statusAppDot) statusAppDot.className = 'status-dot online';
      if (statusAppText) statusAppText.textContent = '在线 (8085)';

      // 托管模式下受门禁保护的端点需要携带访问码（侧栏凭证框）
      const gateHeaders = {};
      if (needsAccessCode && devToken) {
        gateHeaders['X-Access-Code'] = devToken;
      }

      // 任务表独立 try/catch：任务列表出错不能把整页服务状态误标为离线
      try {
        const tasksResp = await fetch('/api/tasks', { headers: gateHeaders });
        if (tasksResp.ok) {
          const resData = await tasksResp.json();
          const tasks = Array.isArray(resData) ? resData : (resData.tasks || []);
          const runningTasks = tasks.filter(t => t.status === 'running');
          if (statActiveTasks) statActiveTasks.textContent = runningTasks.length;
          if (taskTableBody) renderTasksTable(tasks);
        }
      } catch (taskErr) {
        console.warn('Failed to refresh task table', taskErr);
      }

      if (statRateMax) statRateMax.textContent = '20';

      // Get cache info
      try {
        const cacheResp = await fetch('/api/cache-info', { headers: gateHeaders });
        if (cacheResp.ok) {
          const cacheData = await cacheResp.json();
          const sizeKb = (cacheData.packet_cache_size / 1024).toFixed(2);
          const keysCount = cacheData.packet_cache_keys;
          const statCacheSize = document.getElementById('stat-cache-size');
          if (statCacheSize) {
            statCacheSize.textContent = `${sizeKb} KB (${keysCount}项)`;
          }
        }
      } catch (cacheErr) {
        console.warn('Failed to fetch cache info', cacheErr);
      }

    } catch (err) {
      if (statusAppDot) statusAppDot.className = 'status-dot offline';
      if (statusAppText) statusAppText.textContent = '已断开连接';
      if (statusGatewayDot) statusGatewayDot.className = 'status-dot offline';
      if (statusGatewayText) statusGatewayText.textContent = '未就绪';
      if (statServerMode) statServerMode.textContent = '未知';
    } finally {
      dashboardRequestPending = false;
    }
  }

  function renderTasksTable(tasks) {
    taskMonitorSyncTime.textContent = `上次更新: ${new Date().toLocaleTimeString()}`;
    
    if (!tasks || tasks.length === 0) {
      taskTableBody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align: center; color: var(--text-muted); padding: 40px 0;">
            目前没有正在运行或历史生成任务。
          </td>
        </tr>
      `;
      return;
    }

    taskTableBody.innerHTML = tasks.map(t => {
      // 主题名来自 LLM/用户输入，错误信息来自服务端——都必须转义再进 innerHTML
      // 任务名优先用灵感卡片选题名（task_label），回退基础场景主题
      const theme = escapeHtml(t.dimensions ? (t.dimensions.task_label || t.dimensions.theme || '未指定主题') : '应用内直呼生成');
      const duration = t.result && t.result.timings ? `${t.result.timings.total_duration_seconds}s` : '-';
      const beats = t.dimensions ? (t.dimensions.beats_count || 15) : '-';

      let themeDisplay = theme;
      if (t.result && t.result.token_usage) {
        const usage = t.result.token_usage;
        themeDisplay += `<div style="font-size: 11px; color: var(--text-muted); margin-top: 4px; font-family: var(--font-mono, monospace);">` +
                       `Tokens: ${usage.total_tokens} (I:${usage.prompt_tokens} O:${usage.completion_tokens}) | Calls: ${usage.api_calls}` +
                       `</div>`;
      }

      let statusBadge = '';
      if (t.status === 'running') {
        statusBadge = '<span class="badge badge-running">● 正在合成</span>';
      } else if (t.status === 'completed') {
        statusBadge = '<span class="badge badge-completed">● 已完成</span>';
      } else {
        statusBadge = `<span class="badge badge-failed" title="${escapeHtml(t.error || '')}">● 失败</span>`;
      }

      // 中止/删除的处理器在本页实现（consoleCancelTask/consoleDeleteTask）：
      // 旧代码引用的 cancelTask/deleteTask 只存在于 app.js，本页不加载它，点击即 ReferenceError
      const safeId = escapeHtml(String(t.id));
      let actionButtons = '';
      if (t.status === 'running') {
        actionButtons = `<button class="btn btn-sm btn-danger" onclick="consoleCancelTask('${safeId}')">中止</button>`;
      } else {
        actionButtons = `<button class="btn btn-sm" style="color:#f87171; border-color:transparent;" onclick="consoleDeleteTask('${safeId}')">删除</button>`;
      }

      return `
        <tr>
          <td style="font-family: var(--font-mono); font-size:13px; color: var(--primary);">${safeId}</td>
          <td style="font-weight: 500; color: var(--text-main);">${themeDisplay}</td>
          <td>${statusBadge}</td>
          <td>${beats}</td>
          <td style="font-family: var(--font-mono);">${duration}</td>
          <td>${actionButtons}</td>
        </tr>
      `;
    }).join('');
  }

  // 任务监控行内按钮的本页实现：直接 POST 后端并刷新状态
  const consoleTaskHeaders = () => {
    const headers = { 'Content-Type': 'application/json' };
    const token = localTokenInput ? localTokenInput.value.trim() : '';
    if (needsAccessCode && token) headers['X-Access-Code'] = token;
    return headers;
  };

  window.consoleCancelTask = async (taskId) => {
    try {
      const resp = await fetch('/api/compose-cancel', {
        method: 'POST',
        headers: consoleTaskHeaders(),
        body: JSON.stringify({ task_id: taskId })
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      checkSystemStatuses();
    } catch (e) {
      console.error('Cancel task failed:', e);
      alert(`中止任务失败: ${e.message}`);
    }
  };

  window.consoleDeleteTask = async (taskId) => {
    if (!window.confirm('确定删除此任务记录吗？')) return;
    try {
      const resp = await fetch('/api/tasks/delete', {
        method: 'POST',
        headers: consoleTaskHeaders(),
        body: JSON.stringify({ task_id: taskId })
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      checkSystemStatuses();
    } catch (e) {
      console.error('Delete task failed:', e);
      alert(`删除任务失败: ${e.message}`);
    }
  };

  // 6. API Documentation & Code Snippets Tabs
  const docMenuItems = document.querySelectorAll('.docs-menu-item');
  const docPanels = document.querySelectorAll('.doc-panel');
  let docSelectedLang = 'curl';

  docMenuItems.forEach(item => {
    item.addEventListener('click', () => {
      docMenuItems.forEach(mi => mi.classList.remove('active'));
      item.classList.add('active');

      const docId = item.getAttribute('data-doc');
      docPanels.forEach(p => p.style.display = 'none');
      const activePanel = document.getElementById(`doc-${docId}`);
      if (activePanel) {
        activePanel.style.display = 'block';
      }
      renderDocSnippets();
    });
  });

  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('code-tab-btn')) {
      const header = e.target.parentElement;
      const lang = e.target.getAttribute('data-lang');
      docSelectedLang = lang;

      header.querySelectorAll('.code-tab-btn').forEach(btn => btn.classList.remove('active'));
      e.target.classList.add('active');

      renderDocSnippets();
    }
  });

  // Syntax Highlighting Engine for cURL, Python, and JS
  function highlightCode(code, lang) {
    // Escape HTML
    let html = code
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // 单趟高亮：一个按优先级排列的选择分支正则，一次扫描完成。
    // 旧实现是链式 .replace —— 后面的字符串/引号规则会重新扫描前面插入的
    // <span class="..."> 标记本身，把 span 属性再包一层 span，页面上直接可见乱码。
    const wrap = (cls, text) => `<span class="${cls}">${text}</span>`;

    if (lang === 'curl') {
      html = html.replace(
        /(https?:\/\/[^\s"\\]+)|("application\/json"|"multipart\/form-data")|(YOUR_[A-Z_]+)|(Authorization:|Content-Type:|Bearer)|(\s--?[A-Za-z][A-Za-z-]*\b)|\b(curl)\b/g,
        (m, url, str, ph, hdr, flag, kw) => {
          if (url) return wrap('token-url', url);
          if (str) return wrap('token-string', str);
          if (ph) return wrap('token-string', ph);
          if (hdr) return wrap('token-comment', hdr);
          if (flag) return wrap('token-method', flag);
          if (kw) return wrap('token-keyword', kw);
          return m;
        }
      );
    } else if (lang === 'python') {
      html = html.replace(
        /(#.*$)|("[^"\n]*"|'[^'\n]*')|(https?:\/\/[^\s"']+)|(YOUR_[A-Z_]+)|\b(import|from|print|with|as)\b/gm,
        (m, comment, str, url, ph, kw) => {
          if (comment) return wrap('token-comment', comment);
          if (str) return wrap('token-string', str);
          if (url) return wrap('token-url', url);
          if (ph) return wrap('token-string', ph);
          if (kw) return wrap('token-keyword', kw);
          return m;
        }
      );
    } else if (lang === 'js' || lang === 'javascript') {
      html = html.replace(
        /(\/\/.*$)|("[^"\n]*"|'[^'\n]*'|`[^`]*`)|(https?:\/\/[^\s"'`]+)|(YOUR_[A-Z_]+)|\b(const|let|var|function|return|then|catch|console|log|method|headers|body|JSON|stringify|fetch)\b/gm,
        (m, comment, str, url, ph, kw) => {
          if (comment) return wrap('token-comment', comment);
          if (str) return wrap('token-string', str);
          if (url) return wrap('token-url', url);
          if (ph) return wrap('token-string', ph);
          if (kw) return wrap('token-keyword', kw);
          return m;
        }
      );
    }
    return html;
  }

  function renderDocSnippets() {
    // CSSOM may preserve an existing "display:none" spelling. Select the
    // active document by navigation state, not serialized style whitespace.
    const selected = document.querySelector('.docs-menu-item.active');
    const activePanel = selected && document.getElementById(`doc-${selected.getAttribute('data-doc')}`);
    if (!activePanel) return;

    const endpointId = activePanel.id.replace('doc-', '');
    const preBlock = activePanel.querySelector('.code-block');
    if (!preBlock) return;

    const currentHost = window.location.origin;
    const tunnelOrigin = serverManaged ? currentHost : 'https://your-tunnel.trycloudflare.com';
    const keyString = serverManaged ? 'YOUR_ACCESS_CODE' : 'YOUR_API_KEY';

    let code = '';
    
    if (endpointId === 'post-chat') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "model": "gemini-3.8-flash-high",
    "messages": [
      {"role": "system", "content": "You are a creative assistant."},
      {"role": "user", "content": "设计一个废弃巴士的改造点子"}
    ],
    "temperature": 0.7
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `from openai import OpenAI

client = OpenAI(
    base_url="${tunnelOrigin}/v1",
    api_key="${keyString}"
)

response = client.chat.completions.create(
    model="gemini-3.8-flash-high",
    messages=[
        {"role": "user", "content": "设计一个废弃巴士的改造点子"}
    ]
)
print(response.choices[0].message.content)`;
      } else {
        code = `fetch("${tunnelOrigin}/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    model: "gemini-3.8-flash-high",
    messages: [{"role": "user", "content": "设计一个废弃巴士的改造点子"}]
  })
})
.then(res => res.json())
.then(data => console.log(data.choices[0].message.content));`;
      }
    } 
    
    else if (endpointId === 'post-image-gen') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "model": "nano-banana-2",
    "prompt": "清晨薄雾中的现代森林住宅，真实摄影",
    "size": "9:16",
    "quality": "2K",
    "response_format": "b64_json"
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import base64
import requests

response = requests.post(
    "${tunnelOrigin}/v1/images/generations",
    headers={"Authorization": "Bearer ${keyString}"},
    json={
        "model": "nano-banana-2",
        "prompt": "清晨薄雾中的现代森林住宅，真实摄影",
        "size": "9:16",
        "quality": "2K",
        "response_format": "b64_json"
    }
)
image_data = response.json()["data"][0]["b64_json"]
with open("output.png", "wb") as f:
    f.write(base64.b64decode(image_data))`;
      } else {
        code = `fetch("${tunnelOrigin}/v1/images/generations", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    model: "nano-banana-2",
    prompt: "清晨薄雾中的现代森林住宅，真实摄影",
    size: "9:16",
    quality: "2K",
    response_format": "b64_json"
  })
})
.then(res => res.json())
.then(data => {
  const imgBase64 = data.data[0].b64_json;
  console.log("得到图像 Base64 长度:", imgBase64.length);
});`;
      }
    } 
    
    else if (endpointId === 'post-ideate') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/ideate \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "count": 8
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import requests

response = requests.post(
    "${tunnelOrigin}/api/ideate",
    headers={"Authorization": "Bearer ${keyString}"},
    json={"count": 8}
)
print("策划创意列表:", response.json()["ideas"])`;
      } else {
        code = `fetch("${tunnelOrigin}/api/ideate", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({ count: 8 })
})
.then(res => res.json())
.then(data => console.log("策划创意:", data.ideas));`;
      }
    } 
    
    else if (endpointId === 'post-compose') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/compose \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "dimensions": {
      "theme": "林间校车树屋",
      "anchors": ["废弃轮廓", "植物侵蚀", "复古改造"],
      "complexity": "高",
      "budget": "中等",
      "ratio": "80",
      "creativity": "极高",
      "beats_count": 15
    }
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import requests

response = requests.post(
    "${tunnelOrigin}/api/compose",
    headers={"Authorization": "Bearer ${keyString}"},
    json={
        "dimensions": {
            "theme": "林间校车树屋",
            "anchors": ["废弃轮廓", "植物侵蚀", "复古改造"],
            "complexity": "高",
            "budget": "中等",
            "ratio": "80",
            "creativity": "极高",
            "beats_count": 15
        }
    }
)
task_id = response.json()["task_id"]
print("时光机异步任务启动成功，任务ID:", task_id)`;
      } else {
        code = `fetch("${tunnelOrigin}/api/compose", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    dimensions: {
      theme: "林间校车树屋",
      anchors: ["废弃轮廓", "植物侵蚀", "复古改造"],
      complexity: "高",
      budget: "中等",
      ratio: "80",
      creativity: "极高",
      beats_count: 15
    }
  })
})
.then(res => res.json())
.then(data => console.log("合成任务已启动，ID:", data.task_id));`;
      }
    } 
    
    else if (endpointId === 'post-image-edit') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/image/edits \\
  -H "Authorization: Bearer ${keyString}" \\
  -F "image=@/path/to/source.png" \\
  -F "prompt=将画面光线改为清晨，保持构图和后院餐桌桌椅不变" \\
  -F "model=nano-banana-2" \\
  -F "aspect_ratio=9:16" \\
  -F "image_size=2K" \\
  -F "response_format=b64_json"`;
      } else if (docSelectedLang === 'python') {
        code = `import requests
import base64

with open("source.png", "rb") as ref:
    response = requests.post(
        "${tunnelOrigin}/api/image/edits",
        headers={"Authorization": "Bearer ${keyString}"},
        files={"image": ("source.png", ref, "image/png")},
        data={
            "model": "nano-banana-2",
            "prompt": "将画面光线改为清晨，保持构图和后院餐桌桌椅不变",
            "aspect_ratio": "9:16",
            "image_size": "2K",
            "response_format": "b64_json"
        }
    )
b64_data = response.json()["data"][0]["b64_json"]
with open("edited_output.png", "wb") as f:
    f.write(base64.b64decode(b64_data))`;
      } else {
        code = `const formData = new FormData();
const imageFile = document.getElementById("file-input").files[0];

formData.append("image", imageFile);
formData.append("prompt", "将画面光线改为清晨，保持构图和后院餐桌桌椅不变");
formData.append("model", "nano-banana-2");
formData.append("aspect_ratio", "9:16");
formData.append("image_size", "2K");
formData.append("response_format", "b64_json");

fetch("${tunnelOrigin}/api/image/edits", {
  method: "POST",
  headers: {
    "Authorization": "Bearer ${keyString}"
  },
  body: formData
})
.then(res => res.json())
.then(data => console.log("编辑图像生成成功！"));`;
      }
    }

    preBlock.innerHTML = highlightCode(code, docSelectedLang);
  }

  window.copyDocCode = (blockId) => {
    const preBlock = document.getElementById(blockId);
    if (!preBlock) return;

    copyText(preBlock.textContent).then(() => {
      const copyBtn = preBlock.parentElement.querySelector('.btn-copy-code');
      const originalSvg = copyBtn.innerHTML;
      copyBtn.innerHTML = `<span style="font-size:10px; font-weight:600; color:var(--success);">✓</span>`;
      setTimeout(() => {
        copyBtn.innerHTML = originalSvg;
      }, 1500);
    }).catch(err => {
      console.error('Copy doc code failed:', err);
    });
  };

  // Start initialization
  init();
});

/* 暗夜模式 toggle 已抽出到 js/theme_toggle.js(双前端共享,console.html 加载)*/

/* ============================================================
   Mobile Sidebar Toggle & Filter Panel Drawer Control
   ============================================================ */
(function initMobileDrawers() {
  // Elements
  const mobileMenuToggle = document.getElementById('mobile-menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  
  if (!sidebarBackdrop) return;

  // Function to close all mobile drawers
  const closeAllDrawers = () => {
    if (sidebar) sidebar.classList.remove('open');
    sidebarBackdrop.classList.remove('active');
  };

  // Toggle Sidebar
  if (mobileMenuToggle && sidebar) {
    mobileMenuToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      sidebar.classList.add('open');
      sidebarBackdrop.classList.add('active');
    });
  }

  // Dismiss Drawers via Backdrop click
  sidebarBackdrop.addEventListener('click', () => {
    closeAllDrawers();
  });

  // Auto-close Sidebar Drawer when clicking navigation items on mobile screens
  const navLinks = document.querySelectorAll('.sidebar .nav-item, .sidebar .nav-item-link');
  navLinks.forEach(link => {
    link.addEventListener('click', () => {
      if (window.innerWidth <= 1024) {
        closeAllDrawers();
      }
    });
  });
})();
