// --- api_client.js ---

/**
 * 统一的任务事件流消费引擎（断线恢复版）。
 *
 * 之前六处手写的 SSE 读取循环把「连接断开」当成「任务失败」，这是
 * “上游做好了但前端不同步”问题的根源。本引擎的保证：
 *  - 服务端在连接时会重放全部事件历史，所以 onEvent 处理器必须幂等；
 *  - 单行 JSON 解析失败只跳过该行，绝不杀死整个流；
 *  - 服务端 'error' 事件才是任务失败；连接断开本身不是；
 *  - 流断开且任务仍在运行时，按指数退避自动重连（成功连上即重置计数）；
 *  - 流结束但没读到终态事件时，用 /api/compose-status 仲裁真实状态。
 *
 * @returns {Promise<{status: 'completed'|'failed'|'cancelled', result?: any, error?: string}>}
 *          AbortError（用户取消）原样抛出，由调用方处理。
 */
async function watchTaskUntilTerminal(taskId, opts = {}) {
    const { onEvent, signal, label = 'task', maxReconnects = 8 } = opts;
    let reconnects = 0;
    let resultData = null;

    const emit = (type, data, raw) => {
        if (!onEvent) return;
        try {
            onEvent(type, data, raw);
        } catch (uiErr) {
            // UI 处理器抛错不能中断事件流
            console.error(`[watchTask:${label}] onEvent 处理器异常（已忽略）`, uiErr);
        }
    };

    const checkStatus = async () => {
        const res = await fetch(`/api/compose-status?task_id=${encodeURIComponent(taskId)}`, { signal });
        if (!res.ok) return null;
        return res.json();
    };

    while (true) {
        if (signal && signal.aborted) {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }

        let sawTerminal = null;
        try {
            const response = await fetch(`/api/compose-stream?task_id=${encodeURIComponent(taskId)}`, { signal });
            if (response.status === 404) {
                // 任务不在内存中（服务可能重启过）——用状态接口做最终仲裁
                let st = null;
                try { st = await checkStatus(); } catch (e) { if (e.name === 'AbortError') throw e; }
                if (st && st.status === 'completed') return { status: 'completed', result: st.result };
                if (st && st.status === 'cancelled') return { status: 'cancelled' };
                return { status: 'failed', error: (st && st.error) || '任务不存在（服务可能已重启）' };
            }
            if (!response.ok) {
                const errText = await response.text().catch(() => '');
                throw new Error(`HTTP ${response.status}: ${errText}`);
            }
            reconnects = 0;

            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';
            let streamTerminal = false;
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed || !trimmed.startsWith('data: ')) continue;
                    let parsed;
                    try {
                        parsed = JSON.parse(trimmed.substring(6));
                    } catch (parseErr) {
                        console.warn(`[watchTask:${label}] 跳过无法解析的事件行`, parseErr);
                        continue;
                    }
                    if (!parsed || !parsed.type) continue;
                    if (parsed.type === 'result') resultData = parsed.data;
                    emit(parsed.type, parsed.data, parsed);
                    if (parsed.type === 'error') {
                        sawTerminal = { status: 'failed', error: (parsed.data && parsed.data.message) || '未知错误' };
                    }
                    if (parsed.type === 'result' || parsed.type === 'error') {
                        // 终态事件已经送达：不再等底层连接真正关闭（done）才收尾——
                        // 服务端虽然会在发完终态事件后立刻关连接，但客户端这边
                        // 探测到 TCP 连接真正断开可能被本机杀软/代理/系统层面
                        // 延迟甚至卡住（本机日志里能看到反复的 WinError 10053/10054），
                        // 那样帧/视频明明已经全部生成完，进度条却会空转到连接
                        // 被判定断开为止。收到事件本身就是终态的权威来源，直接
                        // 结束读取即可。
                        streamTerminal = true;
                        break;
                    }
                }
                if (streamTerminal) {
                    try { await reader.cancel(); } catch (_) { /* noop：连接可能已经关闭 */ }
                    break;
                }
            }
        } catch (e) {
            if (e.name === 'AbortError') throw e;
            console.warn(`[watchTask:${label}] 事件流中断:`, e.message || e);
        }

        if (sawTerminal) return { ...sawTerminal, result: resultData };
        if (resultData) return { status: 'completed', result: resultData };

        // 流结束但没有终态事件：先问状态接口，任务真的还在跑才重连
        let st = null;
        try { st = await checkStatus(); } catch (e) { if (e.name === 'AbortError') throw e; }
        if (st) {
            if (st.status === 'completed') return { status: 'completed', result: st.result };
            if (st.status === 'failed') return { status: 'failed', error: st.error || '任务失败' };
            if (st.status === 'cancelled') return { status: 'cancelled' };
            if (st.status === 'not_found') return { status: 'failed', error: '任务不存在（服务可能已重启）' };
        }

        reconnects += 1;
        if (reconnects > maxReconnects) {
            return { status: 'failed', error: '与服务的连接反复中断，已停止重连。任务可能仍在后台运行，稍后可在任务列表中查看。' };
        }
        const delay = Math.min(15000, 1000 * Math.pow(2, reconnects - 1));
        emit('reconnecting', { attempt: reconnects, delay });
        await new Promise(r => setTimeout(r, delay));
    }
}

// ── 监修模式审阅面板（review_pause / review_resume 事件驱动） ──
let _reviewCountdownTimer = null;

function hideFrameReviewPanel() {
    const modal = document.getElementById('review-modal');
    if (modal) modal.style.display = 'none';
    if (_reviewCountdownTimer) {
        clearInterval(_reviewCountdownTimer);
        _reviewCountdownTimer = null;
    }
}

function showFrameReviewPanel(taskId, data) {
    const modal = document.getElementById('review-modal');
    const img = document.getElementById('review-img');
    const ctx = document.getElementById('review-context');
    const title = document.getElementById('review-title');
    const countdown = document.getElementById('review-countdown');
    const adoptBtn = document.getElementById('review-adopt-btn');
    const rerenderBtn = document.getElementById('review-rerender-btn');
    if (!modal || !img || !ctx || !adoptBtn || !rerenderBtn) return;

    const seq = data && data.sequence;
    const isSummary = seq === null || seq === undefined;
    if (title) {
        title.textContent = isSummary
            ? '🧑‍⚖️ 监修确认 — 阶段汇总'
            : `🧑‍⚖️ 监修确认 — IMG ${String(seq).padStart(3, '0')}`;
    }
    if (data && data.image_url) {
        // 重渲会覆盖同路径文件，必须绕浏览器缓存取新图（单张，无性能顾虑）
        img.src = `${data.image_url}?t=${Date.now()}`;
        img.style.display = 'block';
    } else {
        img.src = '';
        img.style.display = 'none';
    }
    ctx.textContent = (data && data.context) || '';
    rerenderBtn.style.display = isSummary ? 'none' : '';
    adoptBtn.textContent = isSummary ? '✅ 继续' : '✅ 采用并继续';

    const post = async (decision) => {
        adoptBtn.disabled = true;
        rerenderBtn.disabled = true;
        try {
            const res = await fetch('/api/frame-review', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: taskId, sequence: isSummary ? null : seq, decision })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            // review_resume 事件也会关面板；这里立即关，不等后端下一轮询（≤2s）
            hideFrameReviewPanel();
        } catch (e) {
            showToast(`监修决策提交失败: ${e.message}`, 'error');
            adoptBtn.disabled = false;
            rerenderBtn.disabled = false;
        }
    };
    adoptBtn.disabled = false;
    rerenderBtn.disabled = false;
    adoptBtn.onclick = () => post('adopt');
    rerenderBtn.onclick = () => post('rerender');

    if (_reviewCountdownTimer) clearInterval(_reviewCountdownTimer);
    const timeoutS = (data && data.timeout_seconds) || 600;
    const deadline = Date.now() + timeoutS * 1000;
    const tick = () => {
        const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
        if (countdown) {
            countdown.textContent = `⏳ ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} 后自动采用并继续`;
        }
        if (left <= 0 && _reviewCountdownTimer) {
            clearInterval(_reviewCountdownTimer);
            _reviewCountdownTimer = null;
        }
    };
    tick();
    _reviewCountdownTimer = setInterval(tick, 1000);

    modal.style.display = 'flex';
}

/**
 * 把一个 'frame' 事件合并进目标创意（幂等：同序号覆盖）。
 * 注意：这里只写 localStorage 快照，不再每帧向服务端 POST 整个库
 * （旧行为在长任务里造成持续的大请求）；库同步集中在任务终态时进行。
 *
 * @param {object} f 帧数据
 * @param {object} [idea] 该事件流实际归属的创意对象；省略时退回 currentIdea——
 *        但生成过程中用户可能已经切换到另一个创意，这时必须显式传入生成发起
 *        时捕获的创意引用，否则数据会被写进当前正在浏览的、不相关的创意里。
 */
function applyFrameEventToIdea(f, idea) {
    const targetIdea = idea || currentIdea;
    if (!f || !targetIdea) return;
    if (!targetIdea.frameRun) targetIdea.frameRun = { title: targetIdea.title, frames: [] };
    if (!targetIdea.frameRun.frames) targetIdea.frameRun.frames = [];
    const idx = targetIdea.frameRun.frames.findIndex(item => item.sequence === f.sequence);
    if (idx !== -1) {
        targetIdea.frameRun.frames[idx] = f;
    } else {
        targetIdea.frameRun.frames.push(f);
        targetIdea.frameRun.frames.sort((a, b) => a.sequence - b.sequence);
    }
    if (targetIdea === currentIdea) saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === targetIdea.id);
    if (existingIdx !== -1) savedIdeas[existingIdx].frameRun = targetIdea.frameRun;
}

/** 任务终态时把 manifest 同步进目标创意与创意库（含服务端持久化）。见 applyFrameEventToIdea 的 idea 参数说明。 */
async function syncFrameRunToLibrary(manifestData, idea) {
    const targetIdea = idea || currentIdea;
    if (!targetIdea || !manifestData) return;
    targetIdea.frameRun = manifestData;
    if (targetIdea === currentIdea) saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === targetIdea.id);
    if (existingIdx !== -1) {
        savedIdeas[existingIdx].frameRun = manifestData;
        await saveLibrary();
    }
}

/** 失败后从服务端 manifest 恢复已完成的部分结果。见 applyFrameEventToIdea 的 idea 参数说明。 */
async function reloadManifestIntoIdea(idea) {
    const targetIdea = idea || currentIdea;
    if (!targetIdea) return;
    try {
        const resp = await fetch(`/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(targetIdea))}`);
        if (resp.ok) {
            await syncFrameRunToLibrary(await resp.json(), targetIdea);
        }
    } catch (err) {
        console.error('Failed to load partial manifest after failure:', err);
    }
}

/** 视频槽位卡片渲染（等待中/生成中/完成/失败），供事件流与重试路径共用。 */
function renderVideoSlotPending(slotIdx, text) {
    const el = document.getElementById(`video-slot-${slotIdx}`);
    if (!el) return;
    el.className = 'frame-card placeholder-frame-card';
    el.innerHTML = `
        <div class="frame-placeholder-spinner">
            <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
        </div>
        <span>第 ${String(slotIdx).padStart(3, '0')} 段视频 (${text})</span>
    `;
}

function renderVideoSlotDone(idx, video) {
    const el = document.getElementById(`video-slot-${idx}`);
    if (!el || !video) return;
    el.className = 'frame-card';
    el.style.cursor = 'default';
    el.innerHTML = `
        <video controls style="width:100%; aspect-ratio: 9/16; object-fit: cover; border-radius: 5px; display: block; background: #03050c;"></video>
        <span>VID ${String(video.slot || idx).padStart(3, '0')}</span>
    `;
    el.querySelector('video').src = video.url;
}

function renderVideoSlotFailed(idx, message, labelText = '生成失败') {
    const el = document.getElementById(`video-slot-${idx}`);
    if (!el) return;
    el.className = 'frame-card video-failed-card';
    el.innerHTML = `
        <div class="video-failed-placeholder">
            <span class="error-icon">⚠️</span>
            <span class="error-text"></span>
            <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${idx}">重试</button>
        </div>
        <span>VID ${String(idx).padStart(3, '0')}</span>
    `;
    const errText = el.querySelector('.error-text');
    errText.textContent = labelText;
    errText.title = message || labelText;
    el.querySelector('.retry-video-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        retrySingleVideo(idx);
    });
}

// 声明式硬切槽位（[CUT]，TBCP v2 hard_cut 变体）：该槽不生成视频，成片在此处直接
// 硬切拼接——画中性卡片而不是失败卡（无重试按钮，重试它没有意义）
function renderVideoSlotSkippedCut(idx, message) {
    const el = document.getElementById(`video-slot-${idx}`);
    if (!el) return;
    el.className = 'frame-card video-failed-card';
    el.innerHTML = `
        <div class="video-failed-placeholder">
            <span class="error-icon">✂️</span>
            <span class="error-text"></span>
        </div>
        <span>VID ${String(idx).padStart(3, '0')}</span>
    `;
    const txt = el.querySelector('.error-text');
    txt.textContent = '声明式硬切（无片段）';
    txt.title = message || '声明式硬切槽位：不生成视频片段，成片在此处直接硬切。';
}

function startTasksPolling(interval = 2500) {
    stopTasksPolling();
    currentPollInterval = interval;
    const poll = async () => {
        await renderTasks();
        const tasksListContainer = document.getElementById('tasks-list');
        const hasRunning = tasksListContainer && tasksListContainer.querySelector('.task-card-header .running') !== null;
        const nextInterval = hasRunning ? 2500 : 10000;
        
        const tasksDrawer = document.getElementById('tasks-drawer');
        if (tasksDrawer && tasksDrawer.classList.contains('active')) {
            tasksPollTimeout = setTimeout(poll, nextInterval);
        }
    };
    tasksPollTimeout = setTimeout(poll, interval);
}

function stopTasksPolling() {
    if (tasksPollTimeout) {
        clearTimeout(tasksPollTimeout);
        tasksPollTimeout = null;
    }
}

function saveActiveBackgroundTasksToLocalStorage() {
    localStorage.setItem('spark_active_background_tasks', JSON.stringify({
        framesTaskId: activeBackgroundTasks.framesTaskId || null,
        videosTaskId: activeBackgroundTasks.videosTaskId || null,
        coverTaskId: activeBackgroundTasks.coverTaskId || null
    }));
}

function resumeActiveBackgroundTasksIfExists() {
    const saved = localStorage.getItem('spark_active_background_tasks');
    if (!saved) return;
    try {
        const parsed = JSON.parse(saved);
        // 刷新后重连：此时 currentIdea 是本标签页最后浏览的创意，只能假设它就是
        // 任务的归属创意（这是 spark_active_task_* 这套 localStorage 快照本身的
        // 局限，并非本次修复引入）——但仍需显式落到 *Owner 字段，后续事件的
        // DOM 绘制/数据写入才有依据可循，不会退化回"总是写当前 currentIdea"。
        if (parsed.framesTaskId) {
            activeBackgroundTasks.framesTaskId = parsed.framesTaskId;
            activeBackgroundTasks.framesOwner = currentIdea;
            streamFramesProgress(parsed.framesTaskId);
        }
        if (parsed.videosTaskId) {
            activeBackgroundTasks.videosTaskId = parsed.videosTaskId;
            activeBackgroundTasks.videosOwner = currentIdea;
            streamVideosProgress(parsed.videosTaskId);
        }
        if (parsed.coverTaskId) {
            activeBackgroundTasks.coverTaskId = parsed.coverTaskId;
            activeBackgroundTasks.coverOwner = currentIdea;
            streamCoverProgress(parsed.coverTaskId);
        }
    } catch (e) {
        console.error("Failed to resume active background tasks:", e);
    }
}

// 后端 server_common.log() 的行格式：HH:MM:SS.mmm [LEVEL] [tag] [task=xxx] message
// 未套用这个格式的旧 print()/异常回溯行匹配不上，统一归入 'OTHER' 级别——
// 默认仍然显示，不会让没迁移的日志静默消失，只是没有级别/任务过滤的加成。
const _LOG_LINE_RE = /^(\d{2}:\d{2}:\d{2}\.\d{3})\s+\[(\w+)\s*\]\s+\[([^\]]+)\](?:\s+\[task=([^\]]+)\])?\s?(.*)$/;
const _LOG_KNOWN_LEVELS = ['ERROR', 'WARN', 'INFO', 'DEBUG'];

function initLocalServiceLogs() {
    const drawer = document.getElementById('log-panel-drawer');
    const header = document.getElementById('log-panel-header');
    const toggleBtn = document.getElementById('toggle-log-btn');
    const clearBtn = document.getElementById('clear-log-btn');
    const linesEl = document.getElementById('log-output-lines');
    const statusDot = drawer?.querySelector('.log-panel-status-dot');
    const autoscrollChk = document.getElementById('log-autoscroll-chk');
    const levelChipsEl = document.getElementById('log-level-chips');
    const taskFilterEl = document.getElementById('log-task-filter');
    const searchInputEl = document.getElementById('log-search-input');
    const countEl = document.getElementById('log-filter-count');

    if (!drawer || !header || !linesEl) return;

    const MAX_LINES = 3000;
    // DEBUG 默认关掉（HTTP 每次尝试都打一条，太吵）；其余级别含未识别的 OTHER 默认全开。
    const activeLevels = new Set(['ERROR', 'WARN', 'INFO', 'OTHER']);
    let taskFilter = '';
    let searchFilter = '';
    const entries = []; // 与 linesEl 的子节点一一对应，顺序一致
    // DOM 节点 → entry 的反查（展开重复徽标用）：用 WeakMap 而不是给每个节点存
    // 数组下标，是因为超过 MAX_LINES 淘汰最老的行时下标会整体错位，重新编号
    // 又是每加一行就要遍历一遍全部子节点——量一大就是明显的卡顿。WeakMap 不
    // 关心行在数组里的位置，淘汰旧行时旧节点自然被 GC，完全不用重新编号。
    const elToEntry = new WeakMap();

    function escapeRegExp(s) {
        return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function highlightSearch(safeHtml) {
        if (!searchFilter) return safeHtml;
        try {
            const re = new RegExp(escapeRegExp(searchFilter), 'ig');
            return safeHtml.replace(re, m => `<mark>${m}</mark>`);
        } catch (e) {
            return safeHtml;
        }
    }

    function parseLine(raw) {
        const m = _LOG_LINE_RE.exec(raw);
        if (!m) return { level: 'OTHER', time: '', tag: '', task: '', text: raw, raw };
        const level = (m[2] || '').trim().toUpperCase();
        return {
            level: _LOG_KNOWN_LEVELS.includes(level) ? level : 'OTHER',
            time: m[1], tag: m[3] || '', task: m[4] || '', text: m[5] || '', raw
        };
    }

    // 同一路重试/限流告警反复触发时，文案里唯一变化的通常只有次数/秒数这类
    // 数字（"第 1/5 次" → "第 2/5 次"）。把数字都抹掉再比较，形状相同就认为是
    // 同一件事在重复，而不是要求逐字节完全相等（那样几乎永远碰不上）。
    function shapeKey(entry) {
        return entry.level + '|' + entry.tag + '|' + entry.task + '|' +
            entry.text.replace(/\d+(\.\d+)?/g, '#');
    }

    const MAX_REPEAT_ITEMS = 20;

    function passesFilter(entry) {
        if (!activeLevels.has(entry.level)) return false;
        if (taskFilter && entry.task.indexOf(taskFilter) === -1) return false;
        if (searchFilter && entry.raw.toLowerCase().indexOf(searchFilter.toLowerCase()) === -1) return false;
        return true;
    }

    function renderLineEl(entry) {
        const div = document.createElement('div');
        div.className = `log-line lvl-${entry.level}`;
        elToEntry.set(div, entry);
        if (!passesFilter(entry)) div.classList.add('log-line-hidden');
        let head;
        if (entry.time) {
            head =
                `<span class="log-line-time">${entry.time}${entry.lastTime && entry.lastTime !== entry.time ? '~' + entry.lastTime : ''}</span>` +
                `<span class="log-line-level">${entry.level}</span>` +
                (entry.tag ? `<span class="log-line-tag">[${escapeHtml(entry.tag)}]</span>` : '') +
                (entry.task ? `<span class="log-line-task" data-task="${escapeHtml(entry.task)}" title="点击按此任务过滤">task=${escapeHtml(entry.task)}</span>` : '') +
                `<span class="log-line-msg">${highlightSearch(escapeHtml(entry.text))}</span>`;
        } else {
            head = `<span class="log-line-msg">${highlightSearch(escapeHtml(entry.raw))}</span>`;
        }
        if (entry.repeatCount > 1) {
            const shown = entry.repeatItems.length;
            const omitted = entry.repeatCount - 1 - shown;
            head += `<span class="log-line-repeat-badge">×${entry.repeatCount} 展开</span>`;
            const itemsHtml = entry.repeatItems
                .map(it => `<div class="log-line-repeat-item">${it.time || ''} ${escapeHtml(it.raw)}</div>`)
                .join('') + (omitted > 0 ? `<div class="log-line-repeat-item">…中间还有 ${omitted} 条未保留</div>` : '');
            head += `<div class="log-line-repeat-detail">${itemsHtml}</div>`;
        }
        div.innerHTML = head;
        return div;
    }

    function updateCount() {
        if (!countEl) return;
        let visible = 0;
        for (const entry of entries) { if (passesFilter(entry)) visible++; }
        countEl.textContent = `${visible}/${entries.length} 行（含折叠重复）`;
    }

    function appendEntry(entry) {
        // 连续重复折叠：新行与上一行"形状"相同就合并成一条 ×N，而不是逐条铺开
        // 刷屏——这是"日志纯净度"最直接的对症（同一个上游限流重试 10 次，
        // 之前是 10 行几乎一模一样的告警，现在是 1 行 + 可展开的 ×10）。
        const last = entries[entries.length - 1];
        if (last && shapeKey(last) === shapeKey(entry)) {
            last.repeatCount = (last.repeatCount || 1) + 1;
            last.lastTime = entry.time || last.lastTime;
            if (last.repeatItems.length < MAX_REPEAT_ITEMS) {
                last.repeatItems.push({ time: entry.time, raw: entry.raw });
            }
            const newEl = renderLineEl(last);
            if (linesEl.lastChild) linesEl.replaceChild(newEl, linesEl.lastChild);
            else linesEl.appendChild(newEl);
            return;
        }
        entry.repeatCount = 1;
        entry.repeatItems = [];
        entries.push(entry);
        linesEl.appendChild(renderLineEl(entry));
        while (entries.length > MAX_LINES) {
            entries.shift();
            if (linesEl.firstChild) linesEl.removeChild(linesEl.firstChild);
        }
    }

    function appendLine(raw) {
        if (!raw) return;
        appendEntry(parseLine(raw));
    }

    // 增量 'log' 事件送来的是任意长度的文本块（服务端从文件末尾增量 read()），
    // 可能横跨多行也可能一行没写完；缓存半行，凑齐整行再解析渲染，避免一行
    // 日志被拆成两条、正则匹配失败退化成 OTHER。
    let pendingPartial = '';
    function appendChunk(text) {
        const combined = pendingPartial + text;
        const parts = combined.split('\n');
        pendingPartial = parts.pop();
        parts.forEach(line => { if (line) appendLine(line); });
    }

    function reapplyVisibility() {
        entries.forEach((entry, i) => {
            const el = linesEl.children[i];
            if (el) el.classList.toggle('log-line-hidden', !passesFilter(entry));
        });
        updateCount();
    }

    // 关键词变了，高亮标记必须重新渲染（不只是显示/隐藏），但仍复用已解析好的
    // entries，不重新请求/重新 parse 原始文本。
    function rerenderAll() {
        linesEl.innerHTML = '';
        entries.forEach(entry => linesEl.appendChild(renderLineEl(entry)));
        updateCount();
    }

    function clearAll() {
        entries.length = 0;
        pendingPartial = '';
        linesEl.innerHTML = '';
        updateCount();
    }

    if (levelChipsEl) {
        levelChipsEl.querySelectorAll('.log-level-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const level = chip.dataset.level;
                if (activeLevels.has(level)) {
                    activeLevels.delete(level);
                    chip.classList.remove('active');
                } else {
                    activeLevels.add(level);
                    chip.classList.add('active');
                }
                reapplyVisibility();
            });
        });
    }

    let taskFilterDebounce = null;
    if (taskFilterEl) {
        taskFilterEl.addEventListener('input', () => {
            clearTimeout(taskFilterDebounce);
            taskFilterDebounce = setTimeout(() => {
                taskFilter = taskFilterEl.value.trim();
                reapplyVisibility();
            }, 150);
        });
    }

    let searchDebounce = null;
    if (searchInputEl) {
        searchInputEl.addEventListener('input', () => {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(() => {
                searchFilter = searchInputEl.value.trim();
                rerenderAll();
            }, 150);
        });
    }

    // 点某行的任务标签，直接把任务过滤框填上并聚焦——快速把整个抽屉收敛到
    // "这一个任务从头到尾发生了什么"，不用手动去复制粘贴任务 id。
    // 点某行的 ×N 徽标，原地展开/收起被折叠掉的那些重复行的完整时间戳原文——
    // 折叠只是不铺开显示，信息并没有真的丢，需要时随时能看全。
    linesEl.addEventListener('click', (e) => {
        const taskSpan = e.target.closest('.log-line-task');
        if (taskSpan && taskFilterEl) {
            taskFilterEl.value = taskSpan.dataset.task;
            taskFilter = taskSpan.dataset.task;
            reapplyVisibility();
            taskFilterEl.focus();
            return;
        }
        const badge = e.target.closest('.log-line-repeat-badge');
        if (badge) {
            const detail = badge.parentElement.querySelector('.log-line-repeat-detail');
            if (detail) {
                const open = detail.classList.toggle('open');
                badge.textContent = open ? '收起' : `×${elToEntry.get(badge.closest('.log-line')).repeatCount} 展开`;
            }
        }
    });

    // 只看问题：一键只留 WARN/ERROR，再点一次恢复到默认的 ERROR/WARN/INFO/其他——
    // 排查"哪里出了问题"时最常见的第一步操作，不用一个个去点级别徽标。
    const problemsOnlyBtn = document.getElementById('log-problems-only-btn');
    if (problemsOnlyBtn) {
        let savedLevels = null;
        problemsOnlyBtn.addEventListener('click', () => {
            const isActive = problemsOnlyBtn.classList.toggle('active');
            if (isActive) {
                savedLevels = new Set(activeLevels);
                activeLevels.clear();
                activeLevels.add('ERROR');
                activeLevels.add('WARN');
            } else {
                activeLevels.clear();
                (savedLevels || new Set(['ERROR', 'WARN', 'INFO', 'OTHER'])).forEach(l => activeLevels.add(l));
            }
            if (levelChipsEl) {
                levelChipsEl.querySelectorAll('.log-level-chip').forEach(chip => {
                    chip.classList.toggle('active', activeLevels.has(chip.dataset.level));
                });
            }
            reapplyVisibility();
        });
    }

    // Toggle expand/collapse
    function toggleLog() {
        const isExpanded = drawer.classList.toggle('expanded');
        document.body.classList.toggle('log-expanded', isExpanded);
        toggleBtn.textContent = isExpanded ? '收起' : '展开';
        if (isExpanded) {
            scrollToBottom();
        }
    }

    header.addEventListener('click', (e) => {
        // Prevent toggling when clicking action buttons/checkbox
        if (e.target.closest('.log-panel-actions')) return;
        toggleLog();
    }, { passive: true });

    toggleBtn.addEventListener('click', toggleLog);

    clearBtn.addEventListener('click', clearAll);

    // ── rAF-throttled scroll: reading scrollHeight causes forced layout,
    //    batch to at most one DOM read per animation frame ──
    let _logScrollPending = false;
    function scrollToBottom() {
        if (autoscrollChk && autoscrollChk.checked) {
            if (!_logScrollPending) {
                _logScrollPending = true;
                requestAnimationFrame(() => {
                    const body = document.getElementById('log-panel-body');
                    if (body) body.scrollTop = body.scrollHeight;
                    _logScrollPending = false;
                });
            }
        }
    }

    // Connect to SSE Log Stream
    let eventSource = null;
    function connectLogStream() {
        if (eventSource) {
            eventSource.close();
        }

        if (statusDot) statusDot.className = 'log-panel-status-dot'; // Reset to disconnected
        // EventSource 无法携带自定义请求头：托管模式下用 query 参数传访问码
        const code = (typeof ACCESS_CODE !== 'undefined' && ACCESS_CODE) ? ACCESS_CODE : (localStorage.getItem('spark_access_code') || '');
        const streamUrl = code ? `/api/logs/stream?access_code=${encodeURIComponent(code)}` : '/api/logs/stream';
        eventSource = new EventSource(streamUrl);

        eventSource.addEventListener('open', () => {
            if (statusDot) statusDot.className = 'log-panel-status-dot connected';
            clearAll();
            appendLine('[系统] 已连接到本地服务日志流...');
            scrollToBottom();
        });

        eventSource.addEventListener('history', (e) => {
            try {
                const parsed = JSON.parse(e.data);
                // Server sends {type, data} wrapper via _open_sse_stream
                const payload = parsed.data || parsed;
                if (payload.lines && payload.lines.length) {
                    clearAll();
                    payload.lines.forEach(line => appendLine(line.replace(/\n$/, '')));
                    scrollToBottom();
                }
            } catch (err) {
                console.error("Failed to parse log history", err);
            }
        });

        eventSource.addEventListener('log', (e) => {
            try {
                const parsed = JSON.parse(e.data);
                // Server sends {type, data} wrapper via _open_sse_stream
                const payload = parsed.data || parsed;
                if (payload.text) {
                    appendChunk(payload.text);
                    updateCount();
                    scrollToBottom();
                }
            } catch (err) {
                console.error("Failed to parse log line", err);
            }
        });

        eventSource.addEventListener('error', (e) => {
            if (statusDot) statusDot.className = 'log-panel-status-dot';
            appendLine('[错误] 与本地服务日志流断开连接，正在尝试重新连接...');
            scrollToBottom();
            eventSource.close();
            // Reconnect after 3 seconds
            setTimeout(connectLogStream, 3000);
        });
    }

    connectLogStream();
}

async function retrySingleFrame(seq) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }

    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    const slotCard = document.getElementById(`frame-slot-${seq}`);
    if (!progress || !meta || !slotCard) return;

    slotCard.className = 'frame-card placeholder-frame-card';
    slotCard.innerHTML = `
        <div class="frame-placeholder-spinner">
            <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
        </div>
        <span>第 ${String(seq).padStart(3, '0')} 帧 (重试中...)</span>
    `;

    progress.style.display = 'flex';
    meta.textContent = `正在重试生成第 ${seq} 帧...`;

    // 单帧重试由用户点击当前正在看的那个创意的网格触发，天然归属于 currentIdea；
    // 捕获一份引用，避免重试期间用户切走创意导致结果写错/画错地方（与主生成
    // 流程 app.js streamFramesProgress 的 owningIdea 同一套道理）。
    const owningIdea = currentIdea;
    const isDisplayed = () => currentIdea === owningIdea;
    activeBackgroundTasks.frames = true;
    activeBackgroundTasks.framesOwner = owningIdea;
    updateTabStatusDot();

    // 单帧重试同样在模块内的实时生成动态里直播（helpers 定义在后加载的 app.js，
    // 本函数只在用户点击时运行，届时必已就绪；仍加 typeof 护栏防御加载序变动）
    const feedLine = (text, cls) => { if (isDisplayed() && typeof framesFeedLine === 'function') framesFeedLine(text, cls); };
    if (typeof framesFeedSetLive === 'function') framesFeedSetLive(true);
    feedLine(`🔁 重试渲染 IMG ${String(seq).padStart(3, '0')}…`);

    const controller = new AbortController();

    try {
        const response = await fetch('/api/generate_frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: owningIdea.title,
                prompt_block: owningIdea.prompt_block,
                target_sequences: [seq]
            }),
            signal: controller.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        // 主生成/单帧重试共用同一个「取消」按钮和 activeBackgroundTasks.frames*
        // 记账：不在这里记录 taskId，取消按钮找不到要取消的任务 id，点了也没用。
        activeBackgroundTasks.framesTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-frame-${seq}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (type === 'frame') {
                    const f = evData && evData.frame;
                    applyFrameEventToIdea(f, owningIdea);
                    if (!isDisplayed()) return;
                    if (typeof framesFeedQualityLine === 'function') framesFeedQualityLine(f);
                    // 重试覆盖的是同一个文件名（img_00N.webp），必须强制刷新这张图的
                    // 缓存版本号，否则浏览器可能继续展示重试前的裂图/旧图——与主生成
                    // 流程（app.js streamFramesProgress）里 updateFrameSlotCard 同款处理
                    if (f && typeof updateFrameSlotCard === 'function') {
                        updateFrameSlotCard(f);
                    } else {
                        renderFramesForIdea(owningIdea);
                    }
                } else if (type === 'frame_start') {
                    feedLine(`🎨 IMG ${String(seq).padStart(3, '0')} 渲染中…`);
                } else if (type === 'frame_qa') {
                    feedLine(`🧪 IMG ${String(seq).padStart(3, '0')} 质检判定中…`);
                } else if (type === 'frame_retry') {
                    const reason = evData && evData.reason ? `：${evData.reason}` : '';
                    feedLine(`🔁 IMG ${String(seq).padStart(3, '0')} 质检重试 ${evData && evData.attempt ? evData.attempt : ''}${reason}`, 'warn');
                } else if (type === 'upstream_retry') {
                    const a = (evData && evData.attempt) || '?';
                    const m = (evData && evData.max_attempts) || '?';
                    const tail = evData && evData.retry_in
                        ? `，${evData.retry_in}s 后自动重试（第 ${a}/${m} 次）`
                        : `（第 ${a}/${m} 次，此路终止——若有兜底/收尾会紧随其后，否则任务即将报错结束）`;
                    feedLine(`⚠️ 上游报错：${(evData && evData.error) || '未知错误'}${tail}`, 'warn');
                } else if (type === 'model_fallback') {
                    const to = (evData && evData.to) || '兜底模型';
                    feedLine(`🔀 主模型配额耗尽，切换兜底模型 ${to} 继续渲染 IMG ${String(seq).padStart(3, '0')}…`, 'warn');
                    if (isDisplayed()) meta.textContent = `主模型配额耗尽，兜底模型 ${to} 渲染中...`;
                } else if (type === 'reconnecting') {
                    if (isDisplayed()) meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                    feedLine(`⚠️ 连接中断，正在重连（第 ${evData.attempt} 次）…`, 'warn');
                }
            }
        });

        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, owningIdea);
            if (isDisplayed()) renderFramesForIdea(owningIdea);
            showToast(`《${getIdeaSaveTitle(owningIdea)}》第 ${seq} 帧重试生成成功。`, "success");
        }
    } catch (e) {
        console.error(`Failed to retry frame ${seq}:`, e);
        feedLine(`❌ IMG ${String(seq).padStart(3, '0')} 重试失败：${e.message}`, 'err');
        showToast(`《${getIdeaSaveTitle(owningIdea)}》第 ${seq} 帧重试失败: ${e.message}`, "error");

        // Restore state by reloading manifest or rendering whatever is local
        await reloadManifestIntoIdea(owningIdea);
        if (isDisplayed()) renderFramesForIdea(owningIdea);
    } finally {
        activeBackgroundTasks.frames = false;
        activeBackgroundTasks.framesOwner = null;
        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();
        if (typeof syncFramesPanelToCurrentIdea === 'function') {
            syncFramesPanelToCurrentIdea();
        } else if (isDisplayed()) {
            progress.style.display = 'none';
        }
        updateTabStatusDot();
        if (typeof framesFeedSetLive === 'function') framesFeedSetLive(false);
    }
}

async function retrySingleVideo(slot) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }

    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const slotCard = document.getElementById(`video-slot-${slot}`);
    if (!progress || !meta || !slotCard) return;

    slotCard.className = 'frame-card placeholder-frame-card';
    slotCard.innerHTML = `
        <div class="frame-placeholder-spinner">
            <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
        </div>
        <span>第 ${String(slot).padStart(3, '0')} 段视频 (重试中...)</span>
    `;

    progress.style.display = 'flex';
    meta.textContent = `正在重试生成第 ${slot} 段视频...`;

    // 见 retrySingleFrame 里 owningIdea 的说明。
    const owningIdea = currentIdea;
    const isDisplayed = () => currentIdea === owningIdea;
    activeBackgroundTasks.videos = true;
    activeBackgroundTasks.videosOwner = owningIdea;
    updateTabStatusDot();

    const controller = new AbortController();

    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: owningIdea.title,
                prompt_block: owningIdea.prompt_block,
                target_slots: [slot]
            }),
            signal: controller.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        // 见 retrySingleFrame 里的同款说明：取消按钮要靠这个字段找到任务 id。
        activeBackgroundTasks.videosTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-video-${slot}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (!isDisplayed()) return;
                if (type === 'video_start') {
                    meta.textContent = `正在生成视频: 正在处理第 ${evData.index} 段视频...`;
                } else if (type === 'video_done') {
                    renderVideoSlotDone(evData.index, evData.video);
                } else if (type === 'video_error') {
                    renderVideoSlotFailed(evData.index, evData.message || '生成失败');
                } else if (type === 'queue') {
                    meta.textContent = (evData && evData.message) || '正在排队等待生成视频...';
                } else if (type === 'merge_skip') {
                    meta.textContent = (evData && evData.message) || '由于存在失败片段，已跳过自动合并。';
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                }
            }
        });

        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, owningIdea);
            if (isDisplayed()) renderVideosForIdea(owningIdea);
            showToast(`《${getIdeaSaveTitle(owningIdea)}》视频第 ${slot} 段重试成功。`, "success");
        }
    } catch (e) {
        console.error("Failed to retry video:", e);
        showToast(`《${getIdeaSaveTitle(owningIdea)}》视频第 ${slot} 段重试失败: ${e.message}`, "error");
        if (isDisplayed()) {
            meta.textContent = `视频第 ${slot} 段重试失败: ${e.message}`;
            renderVideoSlotFailed(slot, e.message || '生成失败');
        }
    } finally {
        activeBackgroundTasks.videos = false;
        activeBackgroundTasks.videosOwner = null;
        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();
        if (typeof syncVideosPanelToCurrentIdea === 'function') {
            syncVideosPanelToCurrentIdea();
        } else if (isDisplayed()) {
            progress.style.display = 'none';
        }
        updateTabStatusDot();
    }
}

// 批量重试缺失/串片的视频槽位（供「合并被拦截」时一键重试用）。
// 重试完成后自动再走一次合并，若仍有缺口会再次弹出可操作选项。
async function retryMissingVideos(slots) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    slots = (slots || []).map(Number).filter(s => Number.isFinite(s));
    if (!slots.length) return;

    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    if (!progress || !meta) return;

    slots.forEach(slot => {
        const slotCard = document.getElementById(`video-slot-${slot}`);
        if (slotCard) {
            slotCard.className = 'frame-card placeholder-frame-card';
            slotCard.innerHTML = `
                <div class="frame-placeholder-spinner">
                    <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                </div>
                <span>第 ${String(slot).padStart(3, '0')} 段视频 (重试中...)</span>
            `;
        }
    });

    progress.style.display = 'flex';
    meta.textContent = `正在重试缺失的 ${slots.length} 段视频（槽位 ${slots.join(', ')}）...`;
    // 见 retrySingleFrame 里 owningIdea 的说明。
    const owningIdea = currentIdea;
    const isDisplayed = () => currentIdea === owningIdea;
    activeBackgroundTasks.videos = true;
    activeBackgroundTasks.videosOwner = owningIdea;
    updateTabStatusDot();

    const controller = new AbortController();
    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: owningIdea.title,
                prompt_block: owningIdea.prompt_block,
                target_slots: slots
            }),
            signal: controller.signal
        });
        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }
        const data = await response.json();
        const taskId = data.task_id;
        // 见 retrySingleFrame 里的同款说明：取消按钮要靠这个字段找到任务 id。
        activeBackgroundTasks.videosTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-missing-videos`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (!isDisplayed()) return;
                if (type === 'video_start') {
                    meta.textContent = `正在生成视频: 正在处理第 ${evData.index} 段视频...`;
                } else if (type === 'video_done') {
                    renderVideoSlotDone(evData.index, evData.video);
                } else if (type === 'video_error') {
                    renderVideoSlotFailed(evData.index, evData.message || '生成失败');
                } else if (type === 'queue') {
                    meta.textContent = (evData && evData.message) || '正在排队等待生成视频...';
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                }
            }
        });

        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');
        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, owningIdea);
            if (isDisplayed()) renderVideosForIdea(owningIdea);
        }
        showToast(`《${getIdeaSaveTitle(owningIdea)}》缺失片段重试完成，正在尝试重新合并...`, "success");
        // 自动再合并一次；若仍有缺口，mergeVideos 会再次给出重试/强制选项
        if (isDisplayed() && typeof mergeVideos === 'function') {
            await mergeVideos();
        }
    } catch (e) {
        console.error("Failed to retry missing videos:", e);
        showToast(`《${getIdeaSaveTitle(owningIdea)}》重试缺失片段失败: ${e.message}`, "error");
        if (isDisplayed()) meta.textContent = `重试缺失片段失败: ${e.message}`;
    } finally {
        activeBackgroundTasks.videos = false;
        activeBackgroundTasks.videosOwner = null;
        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();
        if (typeof syncVideosPanelToCurrentIdea === 'function') {
            syncVideosPanelToCurrentIdea();
        } else if (isDisplayed()) {
            progress.style.display = 'none';
        }
        updateTabStatusDot();
    }
}

