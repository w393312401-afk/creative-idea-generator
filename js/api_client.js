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
 *  - 流结束但没读到终态事件时，用 /api/compose-status 仲裁真实状态；
 *  - 重连次数耗尽 ≠ 任务失败：这时后端的生成线程完全不知道客户端已经放弃——
 *    它是独立线程，会继续跑到底（甚至跑完监修模式里本该等人工确认的后续
 *    全部片段，全靠 600s 自动采用兜底，人工审阅形同虚设）。返回一个专门的
 *    'disconnected' 状态，让调用方既不误报"生成失败"，也不清掉任务登记——
 *    否则用户会看到卡片转圈很久后静默变回空占位，随后过一阵子刷新页面时
 *    发现后续帧莫名其妙全部"自己"生成完了（2026-07-20 用户实测复现）。
 *
 * @returns {Promise<{status: 'completed'|'failed'|'cancelled'|'disconnected', result?: any, error?: string}>}
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
            return { status: 'disconnected', error: '与服务的连接反复中断，已停止重连。任务可能仍在后台继续运行，请稍后重新打开本创意或刷新页面查看结果。' };
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

/* ── 多创意后台任务登记表 ────────────────────────────────────────────
   帧序列/视频/封面任务按「所属创意 id」登记进 ideaTasksById（js/state.js），
   不再是全局单槽位。事件到达时数据合并/DOM 绘制都必须走这里，用「发起任务
   时捕获的那个创意对象（ownerIdea）」，绝不能隐式落到可能已经切走的全局
   currentIdea——那正是"多任务共用同一个实时生成动态"（串数据+串显示）的根因。 */

function _ideaTaskSlot(ideaId) {
    if (!ideaId) return null;
    if (!ideaTasksById[ideaId]) ideaTasksById[ideaId] = { frames: null, videos: null, cover: null };
    return ideaTasksById[ideaId];
}

function getIdeaTaskRecord(ideaId, type) {
    const slot = ideaId && ideaTasksById[ideaId];
    return (slot && slot[type]) || null;
}

function isIdeaTaskActive(ideaId, type) {
    return !!getIdeaTaskRecord(ideaId, type);
}

function anyActiveTaskOfType(type) {
    return Object.keys(ideaTasksById).some(id => ideaTasksById[id] && ideaTasksById[id][type]);
}

/** 登记一个新任务；record 里额外字段（feedLines/progressInfo 等）按需自行补充。 */
function beginIdeaTask(ideaId, type, taskId, controller) {
    const slot = _ideaTaskSlot(ideaId);
    if (!slot) return null;
    const record = { taskId, controller: controller || null, total: 0, meta: '', progressState: null, progressInfo: null, feedLines: [], live: true };
    slot[type] = record;
    saveActiveBackgroundTasksToLocalStorage();
    if (typeof updateTabStatusDot === 'function') updateTabStatusDot();
    return record;
}

function endIdeaTask(ideaId, type) {
    const slot = ideaId && ideaTasksById[ideaId];
    if (slot) {
        slot[type] = null;
        if (!slot.frames && !slot.videos && !slot.cover) delete ideaTasksById[ideaId];
    }
    saveActiveBackgroundTasksToLocalStorage();
    if (typeof updateTabStatusDot === 'function') updateTabStatusDot();
}

/** 本次事件流的所属任务是否仍是该(idea,type)槽位里登记的那一个——用于取代旧的全局代际(epoch)守卫。 */
function isIdeaTaskCurrent(ideaId, type, taskId) {
    const rec = getIdeaTaskRecord(ideaId, type);
    return !!rec && rec.taskId === taskId;
}

/** 用户当前是否正停留在该创意页面——只有这样才允许直接改 DOM，否则只更新数据+缓冲区。 */
function isViewingIdea(ideaId) {
    return !!(currentIdea && ideaId && currentIdea.id === ideaId);
}

/** 按 id 找回创意对象：优先 currentIdea（同对象引用），否则从创意库找。找不到说明该创意从未收藏且已被切走。 */
function findIdeaObjectById(ideaId) {
    if (!ideaId) return null;
    if (currentIdea && currentIdea.id === ideaId) return currentIdea;
    return savedIdeas.find(item => item.id === ideaId) || null;
}

/**
 * 把一个 'frame' 事件合并进 ownerIdea（幂等：同序号覆盖）。
 * 注意：这里只写 localStorage 快照，不再每帧向服务端 POST 整个库
 * （旧行为在长任务里造成持续的大请求）；库同步集中在任务终态时进行。
 */
function applyFrameEventToIdea(f, ownerIdea) {
    ownerIdea = ownerIdea || currentIdea;
    if (!f || !ownerIdea) return;
    // 一个 'frame' 事件 = 服务端刚把这帧写到磁盘（可能覆盖了同名旧文件）。
    // 在数据合并的唯一入口统一递增缓存版本，主生成/单帧重试/断线恢复
    // 之后的任何一次重渲都能拿到新图，而不是浏览器缓存里的旧帧。
    bustImageCache(f.url || f.file);
    if (!ownerIdea.frameRun) ownerIdea.frameRun = { title: ownerIdea.title, frames: [] };
    if (!ownerIdea.frameRun.frames) ownerIdea.frameRun.frames = [];
    const idx = ownerIdea.frameRun.frames.findIndex(item => item.sequence === f.sequence);
    if (idx !== -1) {
        ownerIdea.frameRun.frames[idx] = f;
    } else {
        ownerIdea.frameRun.frames.push(f);
        ownerIdea.frameRun.frames.sort((a, b) => a.sequence - b.sequence);
    }
    if (currentIdea && currentIdea.id === ownerIdea.id) saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === ownerIdea.id);
    if (existingIdx !== -1) savedIdeas[existingIdx].frameRun = ownerIdea.frameRun;
}

/** 任务终态时把 manifest 同步进 ownerIdea 与创意库（含服务端持久化）。 */
async function syncFrameRunToLibrary(manifestData, ownerIdea) {
    ownerIdea = ownerIdea || currentIdea;
    if (!ownerIdea || !manifestData) return;
    ownerIdea.frameRun = manifestData;
    if (currentIdea && currentIdea.id === ownerIdea.id) saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === ownerIdea.id);
    if (existingIdx !== -1) {
        savedIdeas[existingIdx].frameRun = manifestData;
        await saveLibrary();
    }
}

/** 失败后从服务端 manifest 恢复已完成的部分结果。 */
async function reloadManifestIntoIdea(ownerIdea) {
    ownerIdea = ownerIdea || currentIdea;
    if (!ownerIdea) return;
    try {
        const resp = await fetch(`/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(ownerIdea))}`);
        if (resp.ok) {
            await syncFrameRunToLibrary(await resp.json(), ownerIdea);
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

/** 持久化"当前挂着哪些后台任务"，供刷新页面后重连。改为一份按创意 id 登记的列表
 *（旧格式每种任务只有一个全局槽位，无法区分任务属于哪个创意）。 */
function saveActiveBackgroundTasksToLocalStorage() {
    const tasks = [];
    Object.keys(ideaTasksById).forEach(ideaId => {
        const slot = ideaTasksById[ideaId];
        ['frames', 'videos', 'cover'].forEach(type => {
            if (slot[type]) tasks.push({ ideaId, type, taskId: slot[type].taskId });
        });
    });
    localStorage.setItem('spark_active_background_tasks', JSON.stringify({ tasks }));
}

function resumeActiveBackgroundTasksIfExists() {
    const saved = localStorage.getItem('spark_active_background_tasks');
    if (!saved) return;
    try {
        const parsed = JSON.parse(saved);
        let tasks = Array.isArray(parsed.tasks) ? parsed.tasks : [];
        if (!Array.isArray(parsed.tasks)) {
            // 兼容旧的单槽位格式（无 ideaId）：只能把它归给当时的 currentIdea，最好努力。
            const legacyOwner = currentIdea && currentIdea.id;
            if (legacyOwner) {
                if (parsed.framesTaskId) tasks.push({ ideaId: legacyOwner, type: 'frames', taskId: parsed.framesTaskId });
                if (parsed.videosTaskId) tasks.push({ ideaId: legacyOwner, type: 'videos', taskId: parsed.videosTaskId });
                if (parsed.coverTaskId) tasks.push({ ideaId: legacyOwner, type: 'cover', taskId: parsed.coverTaskId });
            }
        }
        tasks.forEach(t => {
            if (!t || !t.ideaId || !t.taskId) return;
            const idea = findIdeaObjectById(t.ideaId);
            if (!idea) {
                console.warn('恢复后台任务失败：本地找不到所属创意', t);
                return;
            }
            if (t.type === 'frames') streamFramesProgress(t.taskId, idea);
            else if (t.type === 'videos') streamVideosProgress(t.taskId, idea);
            else if (t.type === 'cover') streamCoverProgress(t.taskId, idea);
        });
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
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'frames')) {
        showToast("该创意的帧序列已在生成/重试中，请稍候", "error");
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

    const controller = new AbortController();
    const rec = beginIdeaTask(ownerIdea.id, 'frames', null, controller);
    // 只标记这一帧在范围内：renderFramesForIdea 靠这个字段决定哪些槽位该画
    // "等待中"，不设置的话会误把其余所有未生成槽位也画成"等待中"。
    rec.targetSequences = [seq];

    // 单帧重试同样在模块内的实时生成动态里直播（helpers 定义在后加载的 app.js，
    // 本函数只在用户点击时运行，届时必已就绪；仍加 typeof 护栏防御加载序变动）。
    // framesFeedLine 内部自己判断是否仍停留在这个创意页面才画 DOM，未停留时只缓冲。
    const feedLine = (text, cls) => { if (typeof framesFeedLine === 'function') framesFeedLine(ownerIdea.id, text, cls); };
    if (typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, true);
    feedLine(`🔁 重试渲染 IMG ${String(seq).padStart(3, '0')}…`);

    // 与服务器失联（非任务真的失败）时置真：后端渲染线程是独立线程，跟客户端
    // 有没有人在看毫无关系，会一直跑到底。这种情况下绝不能在 finally 里
    // endIdeaTask——那样这条任务在客户端就再也没人认领了，用户可能对同一帧
    // 再点一次"生成"，跟后台那个还没死的线程并发写同一张图。
    let disconnected = false;

    try {
        const response = await fetch('/api/generate_frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
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
        const rec = getIdeaTaskRecord(ownerIdea.id, 'frames');
        if (rec) rec.taskId = taskId;

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-frame-${seq}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (type === 'frame') {
                    if (typeof framesFeedQualityLine === 'function') framesFeedQualityLine(ownerIdea.id, evData && evData.frame, true);
                    applyFrameEventToIdea(evData && evData.frame, ownerIdea);
                    if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
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
                    if (isViewingIdea(ownerIdea.id)) meta.textContent = `主模型配额耗尽，兜底模型 ${to} 渲染中...`;
                } else if (type === 'reconnecting') {
                    if (isViewingIdea(ownerIdea.id)) meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                    feedLine(`⚠️ 连接中断，正在重连（第 ${evData.attempt} 次）…`, 'warn');
                }
            }
        });

        if (watch.status === 'disconnected') {
            disconnected = true;
            feedLine(`⚠️ ${watch.error}`, 'warn');
            showToast(`第 ${seq} 帧：${watch.error}`, "warning");
            return;
        }
        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, ownerIdea);
            if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
            showToast(`第 ${seq} 帧重试生成成功。`, "success");
        }
    } catch (e) {
        console.error(`Failed to retry frame ${seq}:`, e);
        feedLine(`❌ IMG ${String(seq).padStart(3, '0')} 重试失败：${e.message}`, 'err');
        showToast(`第 ${seq} 帧重试失败: ${e.message}`, "error");

        // Restore state by reloading manifest or rendering whatever is local
        await reloadManifestIntoIdea(ownerIdea);
        if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
    } finally {
        if (isViewingIdea(ownerIdea.id) && typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, false);
        // 失联分支保留任务登记（内存 ideaTasksById + localStorage）：下次刷新页面
        // resumeActiveBackgroundTasksIfExists 才有机会重新接上这条任务的事件流。
        if (!disconnected) {
            endIdeaTask(ownerIdea.id, 'frames');
        }
        if (isViewingIdea(ownerIdea.id) && !disconnected) {
            progress.style.display = 'none';
        }
    }
}

async function retrySingleVideo(slot) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'videos')) {
        showToast("该创意的视频序列已在生成/重试中，请稍候", "error");
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

    const controller = new AbortController();
    beginIdeaTask(ownerIdea.id, 'videos', null, controller);
    // 与服务器失联（非任务真的失败）：后端渲染线程不知道客户端已经放弃，会继续
    // 跑到底——这种情况绝不能在 finally 里 endIdeaTask，否则没人能重新接上它。
    let disconnected = false;

    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
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
        const rec = getIdeaTaskRecord(ownerIdea.id, 'videos');
        if (rec) rec.taskId = taskId;

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-video-${slot}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (!isViewingIdea(ownerIdea.id)) return;
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

        if (watch.status === 'disconnected') {
            disconnected = true;
            if (isViewingIdea(ownerIdea.id)) meta.textContent = watch.error;
            showToast(`视频第 ${slot} 段：${watch.error}`, "warning");
            return;
        }
        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, ownerIdea);
            if (isViewingIdea(ownerIdea.id)) renderVideosForIdea(ownerIdea);
            showToast(`视频第 ${slot} 段重试成功。`, "success");
        }
    } catch (e) {
        console.error("Failed to retry video:", e);
        if (isViewingIdea(ownerIdea.id)) {
            meta.textContent = `视频第 ${slot} 段重试失败: ${e.message}`;
            renderVideoSlotFailed(slot, e.message || '生成失败');
        }
        showToast(`视频第 ${slot} 段重试失败: ${e.message}`, "error");
    } finally {
        if (!disconnected) {
            endIdeaTask(ownerIdea.id, 'videos');
            if (isViewingIdea(ownerIdea.id)) progress.style.display = 'none';
        }
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
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'videos')) {
        showToast("该创意的视频序列已在生成/重试中，请稍候", "error");
        return;
    }

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

    const controller = new AbortController();
    beginIdeaTask(ownerIdea.id, 'videos', null, controller);
    // 与服务器失联（非任务真的失败）：后端渲染线程不知道客户端已经放弃，会继续
    // 跑到底——这种情况绝不能在 finally 里 endIdeaTask，否则没人能重新接上它。
    let disconnected = false;
    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
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
        const rec = getIdeaTaskRecord(ownerIdea.id, 'videos');
        if (rec) rec.taskId = taskId;

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-missing-videos`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (!isViewingIdea(ownerIdea.id)) return;
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

        if (watch.status === 'disconnected') {
            disconnected = true;
            if (isViewingIdea(ownerIdea.id)) meta.textContent = watch.error;
            showToast(`重试缺失片段：${watch.error}`, "warning");
            return;
        }
        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');
        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, ownerIdea);
            if (isViewingIdea(ownerIdea.id)) renderVideosForIdea(ownerIdea);
        }
        showToast("缺失片段重试完成，正在尝试重新合并...", "success");
        // 自动再合并一次；若仍有缺口，mergeVideos 会再次给出重试/强制选项（仅在仍停留于本创意时才有意义）
        if (isViewingIdea(ownerIdea.id) && typeof mergeVideos === 'function') {
            await mergeVideos();
        }
    } catch (e) {
        console.error("Failed to retry missing videos:", e);
        if (isViewingIdea(ownerIdea.id)) meta.textContent = `重试缺失片段失败: ${e.message}`;
        showToast(`重试缺失片段失败: ${e.message}`, "error");
    } finally {
        if (!disconnected) {
            endIdeaTask(ownerIdea.id, 'videos');
            if (isViewingIdea(ownerIdea.id)) progress.style.display = 'none';
        }
    }
}

