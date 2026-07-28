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
 *    它是独立线程，会继续跑到底。返回一个专门的
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

/**
 * 确保帧任务有可增量合并的本地清单，但绝不清空已有帧。
 *
 * 一个整单任务可能有多个内部阶段，每个阶段都会广播 `start`：例如先单独生成并
 * 验收 IMG 001，再分段生成 IMG 002..N。`start` 是进度阶段事件，不是“此前结果
 * 作废”的信号；若每次收到它都把 frames 置空，首帧会在第二阶段开始时从 UI 消失，
 * 且因为后续阶段不会再次广播已完成的首帧，只能等任务终态读取 manifest 才恢复。
 */
function ensureFrameRunForStart(ownerIdea) {
    if (!ownerIdea) return null;
    if (!ownerIdea.frameRun) ownerIdea.frameRun = { title: ownerIdea.title, frames: [] };
    if (!Array.isArray(ownerIdea.frameRun.frames)) ownerIdea.frameRun.frames = [];
    return ownerIdea.frameRun;
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
        // no-store：这个请求要的就是"服务端此刻的状态"。上传/换位刚落盘就来读，
        // 拿到浏览器启发式缓存里的上一份清单会让界面停在改动前的样子。
        const resp = await fetch(
            `/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(ownerIdea))}`,
            { cache: 'no-store' });
        if (resp.ok) {
            await syncFrameRunToLibrary(await resp.json(), ownerIdea);
        }
    } catch (err) {
        console.error('Failed to load partial manifest after failure:', err);
    }
}

/** 视频槽位卡片渲染（等待中/生成中/完成/失败），供事件流与重试路径共用。 */
// 生成过程中的实时槽位回调。四个函数名与签名保持不变（app.js 的事件流按名调用），
// 实现全部转调 js/slot_card.js 的统一渲染器——此前 renderVideoSlotDone 是另写的
// 一套模板，画出来的卡片没有重试/上传/删除按钮、没有 IMG N ➔ IMG N+1 标签、
// 不认英雄展示、也不设 draggable，与整格重渲出来的同状态卡片不是一回事。
function renderVideoSlotPending(slotIdx, text) {
    renderSlotPending('video', slotIdx, text);
}

function renderVideoSlotDone(idx, video) {
    if (!video) return;
    const busy = isIdeaTaskActive(currentIdea && currentIdea.id, 'videos');
    renderSlotById('video', idx, videoSlotState(video, { seq: Number(video.slot) || idx, busy }));
}

// busy=true：该创意的视频序列任务仍在跑（批量重试里其它槽位还没处理完）——
// 这个刚失败的槽位重新画出来的按钮也要保持禁用，否则用户点了又是一次
// "已在生成中" 的错误提示。
function renderVideoSlotFailed(idx, message, labelText = '生成失败', busy = false) {
    const st = videoSlotState({ slot: idx, status: 'failed', error: message }, { seq: idx, busy });
    st.statusText = labelText;
    st.title = message || labelText;
    renderSlotById('video', idx, st);
}

// 声明式硬切槽位（[CUT]，TBCP v2 hard_cut 变体）：该槽不生成视频，成片在此处直接
// 硬切拼接——画中性卡片而不是失败卡（无重试按钮，重试它没有意义）
function renderVideoSlotSkippedCut(idx, message) {
    renderSlotById('video', idx, videoSlotState(
        { slot: idx, status: 'skipped_cut', message }, { seq: idx }));
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
    // 连接状态点有两颗：dock 标题栏一颗，右下角静息胶囊一颗（收起时它是唯一
    // 可见的那颗）。统一按类名取全部，避免只更新其中一颗导致两处状态打架。
    const statusDots = document.querySelectorAll('.log-panel-status-dot');
    const pill = document.getElementById('log-pill');
    const pillBadge = document.getElementById('log-pill-badge');
    const resizer = document.getElementById('log-dock-resizer');
    const autoscrollChk = document.getElementById('log-autoscroll-chk');
    const levelChipsEl = document.getElementById('log-level-chips');
    const taskFilterEl = document.getElementById('log-task-filter');
    const searchInputEl = document.getElementById('log-search-input');
    const countEl = document.getElementById('log-filter-count');

    if (!drawer || !header || !linesEl) return;

    function setConnected(ok) {
        statusDots.forEach(dot => {
            dot.className = ok ? 'log-panel-status-dot connected' : 'log-panel-status-dot';
        });
    }

    // ── 静息态未读计数 ────────────────────────────────────────────────
    // dock 收起期间攒下的 ERROR/WARN 数顶到胶囊徽标上。这是把 45px 全宽横栏
    // 换掉之后仍然保住的那个信号：占位小了，但"出事了"反而更显眼——旧版这个
    // 信息只体现为横栏里一颗不带计数的小圆点。
    let unreadError = 0;
    let unreadWarn = 0;
    // 首次连接时服务端会回灌最近 100 行历史，那是"以前发生过的事"，不该在页面
    // 刚打开就顶一个红徽标出来；只统计连上之后新来的行。
    let replayingHistory = false;

    function renderPillBadge() {
        if (!pillBadge) return;
        const total = unreadError + unreadWarn;
        if (total <= 0) {
            pillBadge.hidden = true;
            return;
        }
        pillBadge.hidden = false;
        pillBadge.textContent = total > 99 ? '99+' : String(total);
        // 只有 WARN 没有 ERROR 时降一档配色，不用红色虚张声势
        pillBadge.classList.toggle('warn-only', unreadError === 0);
        if (pill) {
            pill.title = `本地服务工作日志：${unreadError} 个错误、${unreadWarn} 个警告未查看`;
        }
    }

    function bumpUnread(entry) {
        if (replayingHistory) return;
        if (drawer.classList.contains('expanded')) return; // 正开着看，不算未读
        if (entry.level === 'ERROR') unreadError++;
        else if (entry.level === 'WARN') unreadWarn++;
        else return;
        renderPillBadge();
    }

    function clearUnread() {
        unreadError = 0;
        unreadWarn = 0;
        renderPillBadge();
        if (pill) pill.title = '打开本地服务工作日志';
    }

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
        bumpUnread(entry);
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

    // ── 展开 / 收起 ──────────────────────────────────────────────────
    const LOG_DOCK_WIDTH_KEY = 'spark_log_dock_width';
    const LOG_DOCK_OPEN_KEY = 'spark_log_dock_open';

    function setLogDockOpen(open) {
        drawer.classList.toggle('expanded', open);
        document.body.classList.toggle('log-expanded', open);
        if (toggleBtn) toggleBtn.textContent = open ? '收起' : '展开';
        try { localStorage.setItem(LOG_DOCK_OPEN_KEY, open ? '1' : '0'); } catch (e) {}
        if (open) {
            // 日志 dock 与保存/任务抽屉同样停靠在右侧，同时打开会叠在一起；
            // 而且 420+380 在 1440 屏上只给主内容剩 640px，两边都难用——所以
            // 三者互斥，跟 openLibraryDrawer/openTasksDrawer 之间的关系一致。
            if (typeof closeLibraryDrawer === 'function') closeLibraryDrawer();
            if (typeof closeTasksDrawer === 'function') closeTasksDrawer();
            clearUnread();
            scrollToBottom();
        }
    }

    function toggleLog() {
        setLogDockOpen(!drawer.classList.contains('expanded'));
    }

    // 反向互斥：打开右侧任一抽屉时收起日志 dock（app.js 的抽屉开关调用它）
    window.collapseLogDock = () => setLogDockOpen(false);

    header.addEventListener('click', (e) => {
        // Prevent toggling when clicking action buttons/checkbox
        if (e.target.closest('.log-panel-actions')) return;
        toggleLog();
    }, { passive: true });

    toggleBtn.addEventListener('click', toggleLog);
    if (pill) pill.addEventListener('click', toggleLog);

    // Esc 收起：dock 覆盖在内容之上时（窄屏）尤其需要一个不用瞄准的退出方式
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && drawer.classList.contains('expanded')) {
            // 正在筛选框里打字时 Esc 先让给输入框自己
            if (document.activeElement && document.activeElement.closest('.log-panel-filters')) return;
            setLogDockOpen(false);
        }
    });

    // ── 宽度拖拽 ────────────────────────────────────────────────────
    const MIN_DOCK_WIDTH = 320;
    function applyDockWidth(px) {
        const max = Math.max(MIN_DOCK_WIDTH, window.innerWidth - 360); // 给主工作区留底线
        const w = Math.min(Math.max(px, MIN_DOCK_WIDTH), max);
        document.documentElement.style.setProperty('--log-dock-width', w + 'px');
        return w;
    }

    if (resizer) {
        let dragging = false;
        resizer.addEventListener('mousedown', (e) => {
            dragging = true;
            resizer.classList.add('dragging');
            // 拖拽期间关掉过渡，否则每帧都在补间，跟手感很差
            drawer.style.transition = 'none';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        window.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            applyDockWidth(window.innerWidth - e.clientX);
        });
        window.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            resizer.classList.remove('dragging');
            drawer.style.transition = '';
            document.body.style.userSelect = '';
            try {
                const cur = getComputedStyle(document.documentElement)
                    .getPropertyValue('--log-dock-width').trim();
                if (cur) localStorage.setItem(LOG_DOCK_WIDTH_KEY, parseInt(cur, 10));
            } catch (e) {}
        });
    }

    // 恢复上次的宽度与展开态
    try {
        const savedWidth = parseInt(localStorage.getItem(LOG_DOCK_WIDTH_KEY), 10);
        if (savedWidth > 0) applyDockWidth(savedWidth);
    } catch (e) {}
    try {
        if (localStorage.getItem(LOG_DOCK_OPEN_KEY) === '1') setLogDockOpen(true);
    } catch (e) {}
    renderPillBadge();

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

        setConnected(false); // Reset to disconnected
        // EventSource 无法携带自定义请求头：托管模式下用 query 参数传访问码
        const code = (typeof ACCESS_CODE !== 'undefined' && ACCESS_CODE) ? ACCESS_CODE : (localStorage.getItem('spark_access_code') || '');
        const streamUrl = code ? `/api/logs/stream?access_code=${encodeURIComponent(code)}` : '/api/logs/stream';
        eventSource = new EventSource(streamUrl);

        eventSource.addEventListener('open', () => {
            setConnected(true);
            clearAll();
            // 断线重连会重复触发 open：面板里的行已被 clearAll 清掉，未读计数
            // 也一并归零，否则徽标会累计上一段连接里早已被清空的那些行。
            clearUnread();
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
                    replayingHistory = true;
                    try {
                        payload.lines.forEach(line => appendLine(line.replace(/\n$/, '')));
                    } finally {
                        replayingHistory = false;
                    }
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
            setConnected(false);
            appendLine('[错误] 与本地服务日志流断开连接，正在尝试重新连接...');
            scrollToBottom();
            eventSource.close();
            // Reconnect after 3 seconds
            setTimeout(connectLogStream, 3000);
        });
    }

    connectLogStream();
}


// 帧序列渲染有串行锁（同一创意同时只能有一个帧任务在跑），点击某帧的
// 「生成/重试」后立即把网格里其余按钮禁用，别等下一次事件驱动的整格重渲
// 才生效——否则用户依次连续点击时，第一下之后的每一下都会先命中
// isIdeaTaskActive 的错误提示（2026-07-21 实机复现）。
function setFrameGridButtonsBusy(busy) {
    // 网格上的 .is-busy 是"当前忙态"的单一真相：期间被重画的卡片（例如图加载
    // 失败就地降级成占位卡）据此把自己的按钮也画成禁用态，见 slotGridIsBusy。
    const grid = slotRenderTarget('image');
    if (grid) grid.classList.toggle('is-busy', !!busy);
    document.querySelectorAll('#frames-grid .retry-frame-btn, #frames-grid .fix-frame-btn, #frames-grid .describe-frame-btn, #frames-grid .upload-frame-btn, #frames-grid .delete-slot-btn').forEach(btn => {
        btn.disabled = busy;
        btn.title = busy ? '该创意的帧序列正在生成/重试中，请稍候' : '';
    });
}

// 由各帧卡片的「上传」按钮调用：记下目标帧号，触发共用的隐藏文件选择器
// （见 index.html #frame-upload-input 与 app.js 里的 change 监听）。
function triggerFrameUpload(seq) {
    const input = document.getElementById('frame-upload-input');
    if (!input) return;
    input.dataset.seq = String(seq);
    input.click();
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

    renderSlotPending('image', seq, '重试中...');

    progress.style.display = 'flex';
    meta.textContent = `正在重试生成第 ${seq} 帧...`;

    const controller = new AbortController();
    const rec = beginIdeaTask(ownerIdea.id, 'frames', null, controller);
    // 只标记这一帧在范围内：renderFramesForIdea 靠这个字段决定哪些槽位该画
    // "等待中"，不设置的话会误把其余所有未生成槽位也画成"等待中"。
    rec.targetSequences = [seq];
    setFrameGridButtonsBusy(true);

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
                config: (typeof withCoverReference === 'function'
                    ? withCoverReference(config, ownerIdea) : config),
                title: getIdeaSaveTitle(ownerIdea),
                display_title: ownerIdea.title,
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
                        : `（第 ${a}/${m} 次，此路终止，任务即将报错结束）`;
                    feedLine(`⚠️ 上游报错：${(evData && evData.error) || '未知错误'}${tail}`, 'warn');
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
            setFrameGridButtonsBusy(false);
            // 清掉任务登记后再重渲一次：renderFramesForIdea 的 isFramePending 读的
            // 就是这条登记，上面 catch/成功分支那次重渲发生在登记还在的时候，没出图
            // 的槽位会继续画成「等待中」转圈——取消/失败后界面看起来还在跑。
            renderFramesForIdea(ownerIdea);
        }
    }
}

// 「修复此帧问题」——2026-07-23 监修模式改动：一致性审查发现问题后不再自动改写
// 提示词重渲，只标记+报告；人工看过 vlm_qa_reason 后点这个按钮才会真正触发定向
// 修复（/api/fix_frame_issue -> pipeline_orchestrator.fix_frame_issue）。与
// retrySingleFrame（盲重渲，同一提示词再来一次）的区别：这个会先用 VLM 反馈优化
// 提示词，再图生图重渲——首帧也不例外，后端保证不会退化成文生图推倒重来。
// 「描述问题」——人工主动描述帧序列某一帧的问题。一致性审查是机器视角、也不是
// 每次都跑，人自己看出来的毛病（比如"这一帧的塔吊凭空消失了"）此前没有任何入口
// 能录进去：只有被审查标记过的帧才显示「修复此帧问题」，没标记的帧点也没得点。
// 这里把人写的描述通过 /api/flag_frame_issue 记进 manifest 的 manual_issue
// （quality_gate 标成 manual_flagged），随后的修复就能拿它当待修问题——机器判定
// 与人工描述并存时两份一起交给提示词改写（见 pipeline_orchestrator.fix_frame_issue）。
// 描述留空＝撤销之前的人工标记。
async function describeFrameIssue(seq, existingIssue) {
    if (!currentIdea) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'frames')) {
        showToast("该创意的帧序列正在生成/修复中，请稍候", "error");
        return;
    }

    const seqLabel = String(seq).padStart(3, '0');
    const answer = await customTextarea({
        title: `描述 IMG ${seqLabel} 的问题`,
        message: '写清楚这一帧哪里不对：缺了什么、多了什么、和上一帧对不上的是哪个部位。'
               + '描述会用于定向重写该帧的提示词后图生图重渲，越具体越好。\n'
               + '留空并确定＝撤销之前记录的问题。（Ctrl/⌘+Enter 提交）',
        defaultValue: existingIssue || '',
        placeholder: '例：塔吊在上一帧还在画面右侧，这一帧凭空消失了；左侧脚手架的层数也从 3 层变成了 5 层。',
        confirmLabel: '记录问题',
        extraLabel: '记录并立即修复',
    });
    if (!answer) return;

    const description = (answer.value || '').trim();
    try {
        const resp = await fetch('/api/flag_frame_issue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: getIdeaSaveTitle(ownerIdea), display_title: ownerIdea.title, sequence: seq, description })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.status === 'error') {
            throw new Error(data.message || `HTTP ${resp.status}`);
        }

        await reloadManifestIntoIdea(ownerIdea);
        if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);

        if (typeof framesFeedLine === 'function') {
            framesFeedLine(ownerIdea.id,
                description ? `📝 已记录 IMG ${seqLabel} 的人工问题描述：${description}`
                            : `📝 已撤销 IMG ${seqLabel} 的人工问题标记`,
                description ? 'warn' : 'ok');
        }
        showToast(description ? `已记录第 ${seq} 帧的问题描述。` : `已撤销第 ${seq} 帧的问题标记。`, "success");

        // 「记录并立即修复」：描述已经落盘，这里不再重复传 manual_reason，
        // fix_frame_issue 会自己从 manifest 读出来（连同机器判定一起）。
        if (description && answer.action === 'extra') {
            await fixFrameIssue(seq);
        }
    } catch (e) {
        console.error(`Failed to flag frame ${seq}:`, e);
        showToast(`记录第 ${seq} 帧问题失败: ${e.message}`, "error");
    }
}

// manualReason：可选，人工在别处现场写的问题描述；后端会先把它落盘再参与改写
// （见 pipeline_orchestrator.fix_frame_issue）。走 describeFrameIssue 记录过的
// 描述已经在 manifest 上，无需从这里再传一次。
async function fixFrameIssue(seq, manualReason) {
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

    renderSlotPending('image', seq, '修复中...');

    progress.style.display = 'flex';
    meta.textContent = `正在依据问题描述修复第 ${seq} 帧...`;

    const controller = new AbortController();
    const rec = beginIdeaTask(ownerIdea.id, 'frames', null, controller);
    rec.targetSequences = [seq];
    setFrameGridButtonsBusy(true);

    const feedLine = (text, cls) => { if (typeof framesFeedLine === 'function') framesFeedLine(ownerIdea.id, text, cls); };
    if (typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, true);
    feedLine(`🔧 开始修复 IMG ${String(seq).padStart(3, '0')}…`);

    // 与服务器失联（非任务真的失败）时置真，同 retrySingleFrame 的同款说明。
    let disconnected = false;

    try {
        const response = await fetch('/api/fix_frame_issue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(ownerIdea),
                display_title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
                sequence: seq,
                manual_reason: (manualReason || '').trim() || undefined
            }),
            signal: controller.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        const rec2 = getIdeaTaskRecord(ownerIdea.id, 'frames');
        if (rec2) rec2.taskId = taskId;

        let reverify = null;
        const watch = await watchTaskUntilTerminal(taskId, {
            label: `fix-frame-${seq}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (type === 'frame') {
                    applyFrameEventToIdea(evData && evData.frame, ownerIdea);
                    if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
                } else if (type === 'frame_issue_fix_start') {
                    feedLine(`🔧 ${(evData && evData.message) || `正在优化 IMG ${String(seq).padStart(3, '0')} 的提示词…`}`);
                } else if (type === 'frame_issue_fix_render' || type === 'frame_start') {
                    feedLine(`🎨 ${(evData && evData.message) || `正在重渲 IMG ${String(seq).padStart(3, '0')}…`}`);
                } else if (type === 'frame_issue_reverify') {
                    // 修复闭环（2026-07-25）：重渲后对着新画面逐条复核"到底修好没有"
                    feedLine(`🔎 ${(evData && evData.message) || '正在复核问题是否已解决…'}`);
                } else if (type === 'frame_issue_reverify_result') {
                    reverify = evData || null;
                    const ok = evData && !(evData.remaining || []).length;
                    feedLine(`${ok ? '✅' : '⚠️'} ${(evData && evData.message) || '复核完成'}`,
                             ok ? 'ok' : 'warn');
                } else if (type === 'upstream_retry') {
                    const a = (evData && evData.attempt) || '?';
                    const m = (evData && evData.max_attempts) || '?';
                    const tail = evData && evData.retry_in
                        ? `，${evData.retry_in}s 后自动重试（第 ${a}/${m} 次）`
                        : `（第 ${a}/${m} 次，此路终止，任务即将报错结束）`;
                    feedLine(`⚠️ 上游报错：${(evData && evData.error) || '未知错误'}${tail}`, 'warn');
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
            // fix_frame_issue 返回 {prompt_block, reason}，不是完整 manifest——不能
            // 像 retrySingleFrame 那样整体替换 frameRun（那会清空其它帧的数据）。
            // prompt_block 里对应的提示词已被定向重写，必须写回：后续重试/再次
            // 修复/生成视频都读这份文本。
            if (watch.result.prompt_block) {
                ownerIdea.prompt_block = watch.result.prompt_block;
                if (currentIdea && currentIdea.id === ownerIdea.id) saveCurrentIdeaState();
                const existingIdx = savedIdeas.findIndex(item => item.id === ownerIdea.id);
                if (existingIdx !== -1) {
                    savedIdeas[existingIdx].prompt_block = watch.result.prompt_block;
                    await saveLibrary();
                }
                if (isViewingIdea(ownerIdea.id)) {
                    const blockEl = document.getElementById('idea-prompt-block');
                    if (blockEl) blockEl.textContent = ownerIdea.prompt_block;
                }
            }
            if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
            // 复核结果决定这次修复该不该报"成功"：仍有问题时报 warning，别让人
            // 看着一个绿勾以为修好了（修复流程此前是开环的，没人回答这个问题）
            const remaining = (reverify && reverify.remaining) || [];
            if (remaining.length) {
                feedLine(`⚠️ IMG ${String(seq).padStart(3, '0')} 重渲完成，但仍有 ${remaining.length} 条问题未解决`, 'warn');
                showToast(`第 ${seq} 帧已重渲，但复核发现仍有问题：${remaining.join('；')}`, "warning");
            } else {
                feedLine(`✅ IMG ${String(seq).padStart(3, '0')} 修复完成`, 'ok');
                showToast(`第 ${seq} 帧已修复。`, "success");
            }
        }
    } catch (e) {
        console.error(`Failed to fix frame ${seq}:`, e);
        feedLine(`❌ IMG ${String(seq).padStart(3, '0')} 修复失败：${e.message}`, 'err');
        showToast(`第 ${seq} 帧修复失败: ${e.message}`, "error");

        await reloadManifestIntoIdea(ownerIdea);
        if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
    } finally {
        if (isViewingIdea(ownerIdea.id) && typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, false);
        if (!disconnected) {
            endIdeaTask(ownerIdea.id, 'frames');
        }
        if (isViewingIdea(ownerIdea.id) && !disconnected) {
            progress.style.display = 'none';
            setFrameGridButtonsBusy(false);
            // 清掉任务登记后再重渲一次：renderFramesForIdea 的 isFramePending 读的
            // 就是这条登记，上面 catch/成功分支那次重渲发生在登记还在的时候，没出图
            // 的槽位会继续画成「等待中」转圈——取消/失败后界面看起来还在跑。
            renderFramesForIdea(ownerIdea);
        }
    }
}

// 「运行一致性审查」——2026-07-24 起一致性审查不再随帧序列渲染自动触发，用户
// 确认整套序列（含视频提示词描述的施工顺序/空间关系）已经全部渲染完成后，手动
// 点这个按钮才会跑（/api/sequence_review -> pipeline_orchestrator.
// run_sequence_consistency_review）。纯粹是可选的人工检查工具，不阻塞视频生成：
// 结果只把 manifest 里对应帧的 quality_gate 标记成 sequence_review_flagged /
// sequence_reviewed_pass，真正的改写要等用户看过原因后点「修复此帧问题」
// （fixFrameIssue）。整套序列还没渲完时后端会直接跳过并说明原因，不算错误。
async function runSequenceReview() {
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
    const reviewBtn = document.getElementById('run-sequence-review-btn');
    if (!progress || !meta) return;

    progress.style.display = 'flex';
    meta.textContent = '正在对整套序列做一致性审查...';

    const controller = new AbortController();
    beginIdeaTask(ownerIdea.id, 'frames', null, controller);
    setFrameGridButtonsBusy(true);
    if (reviewBtn) reviewBtn.disabled = true;

    const feedLine = (text, cls) => { if (typeof framesFeedLine === 'function') framesFeedLine(ownerIdea.id, text, cls); };
    if (typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, true);
    feedLine('🔍 开始整套序列一致性审查…');

    let disconnected = false;

    try {
        const response = await fetch('/api/sequence_review', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(ownerIdea),
                display_title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block
            }),
            signal: controller.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        const rec2 = getIdeaTaskRecord(ownerIdea.id, 'frames');
        if (rec2) rec2.taskId = taskId;

        let reviewedBeats = 0;
        let reviewSummary = null;
        const watch = await watchTaskUntilTerminal(taskId, {
            label: 'sequence-review',
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (type === 'sequence_review') {
                    meta.textContent = (evData && evData.message) || '正在对整套序列做一致性审查...';
                    feedLine(`🔍 ${(evData && evData.message) || '正在对整套序列做一致性审查...'}`);
                } else if (type === 'sequence_review_beat') {
                    // 逐拍进度（2026-07-25 并发化后逐拍回报）：审查此前是几分钟的
                    // 静默黑洞，只有开始/结束两条消息，看着像卡死
                    reviewedBeats += 1;
                    const total = (evData && evData.total) || 0;
                    if (total) meta.textContent = `一致性审查中… 已完成 ${reviewedBeats}/${total} 拍`;
                    const cls = evData && evData.reviewed === false ? 'warn'
                        : ((evData && evData.issues && evData.issues.length) ? 'warn' : 'ok');
                    feedLine(`　${(evData && evData.message) || '逐拍审查进行中…'}`, cls);
                } else if (type === 'review_invalidated') {
                    // 上一轮审查之后有帧被重渲，那些结论已经作废（帧内容哈希对不上）
                    feedLine(`♻️ ${(evData && evData.message) || '部分帧的旧审查结论已作废'}`, 'warn');
                } else if (type === 'sequence_review_result') {
                    if (evData && evData.message) {
                        feedLine(`${evData.passed ? '✅' : '🛠️'} ${evData.message}`, evData.passed ? 'ok' : 'warn');
                    } else if (evData && evData.passed) {
                        feedLine('✅ 整套序列一致性审查通过', 'ok');
                    }
                    reviewSummary = evData || null;
                } else if (type === 'upstream_retry') {
                    const a = (evData && evData.attempt) || '?';
                    const m = (evData && evData.max_attempts) || '?';
                    const tail = evData && evData.retry_in
                        ? `，${evData.retry_in}s 后自动重试（第 ${a}/${m} 次）`
                        : `（第 ${a}/${m} 次，此路终止，任务即将报错结束）`;
                    feedLine(`⚠️ 上游报错：${(evData && evData.error) || '未知错误'}${tail}`, 'warn');
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                    feedLine(`⚠️ 连接中断，正在重连（第 ${evData.attempt} 次）…`, 'warn');
                }
            }
        });

        if (watch.status === 'disconnected') {
            disconnected = true;
            feedLine(`⚠️ ${watch.error}`, 'warn');
            showToast(`一致性审查：${watch.error}`, "warning");
            return;
        }
        if (watch.status === 'cancelled') throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        // 结果只改了 manifest 里各帧的 quality_gate 标记，prompt_block 未变——
        // 从服务端重新拉一次 manifest 让帧网格的徽标反映最新审查结果。
        await reloadManifestIntoIdea(ownerIdea);
        if (isViewingIdea(ownerIdea.id)) renderFramesForIdea(ownerIdea);
        feedLine('✅ 一致性审查已完成', 'ok');
        // 只审了已渲染前缀、或有帧没审成时给一条明确 toast——这两种情况下"审查完成"
        // 不等于"整单都查过了"，光看绿色徽标会误判
        if (reviewSummary && reviewSummary.partial) {
            const n = (reviewSummary.reviewed_sequences || []).length;
            showToast(`一致性审查只覆盖了已渲染的前 ${n} 帧，其余帧渲完后请再跑一次。`, "warning");
        } else if (reviewSummary && (reviewSummary.unreviewed_sequences || []).length) {
            showToast(`有 ${reviewSummary.unreviewed_sequences.length} 帧未审完，已标记为「未审查」。`, "warning");
        }
    } catch (e) {
        if (e.name === 'AbortError') {
            feedLine('⏹️ 一致性审查已取消', 'warn');
        } else {
            console.error('Failed to run sequence review:', e);
            feedLine(`❌ 一致性审查失败：${e.message}`, 'err');
            showToast(`一致性审查失败: ${e.message}`, "error");
        }
    } finally {
        if (isViewingIdea(ownerIdea.id) && typeof framesFeedSetLive === 'function') framesFeedSetLive(ownerIdea.id, false);
        if (!disconnected) {
            endIdeaTask(ownerIdea.id, 'frames');
        }
        if (isViewingIdea(ownerIdea.id) && !disconnected) {
            progress.style.display = 'none';
            setFrameGridButtonsBusy(false);
        }
        if (reviewBtn) reviewBtn.disabled = false;
    }
}

// 与 setFrameGridButtonsBusy 同理：视频序列同一创意同时只能有一个任务在跑，
// 点击「重试」后立即禁用网格里其余按钮，不等下一次事件驱动的重渲。
function setVideoGridButtonsBusy(busy) {
    const grid = slotRenderTarget('video');
    if (grid) grid.classList.toggle('is-busy', !!busy);
    document.querySelectorAll('#videos-grid .retry-video-btn, #videos-grid .delete-slot-btn').forEach(btn => {
        btn.disabled = busy;
        btn.title = busy ? '该创意的视频序列正在生成/重试中，请稍候' : '';
    });
}

// 一致性审查确认风险后放行：提交视频生成/重试请求前，检查涉及的锚点帧是否带
// 'vlm_qa_failed'/'sequence_review_flagged' 标记；若有则弹窗确认，确认后必须把
// override_flagged 一并带给后端——否则后端 plan_video_slots 仍会照旧按 quality_gate
// 拦截该槽位，前端"确认风险"只是白问了一遍（2026-07-23 修复：此前三处调用点里
// 只有 generateVideos 弹了确认框，但确认结果从未传给后端，等于白确认）。
// slots=null 表示不按槽位收窄，检查整套 frameRun.frames（整单生成场景）；传入槽位
// 数组时只检查这些槽位涉及的锚点帧对（slot 与 slot+1，与 plan_video_slots 的判断
// 范围一致）。返回 {proceed, override}：proceed=false 表示用户取消，调用方应直接
// return，不要发起请求。
async function confirmSequenceReviewOverride(ownerIdea, slots) {
    const frames = (ownerIdea.frameRun && ownerIdea.frameRun.frames) || [];
    if (!frames.length) return { proceed: true, override: false };
    let candidates;
    if (slots && slots.length) {
        const relevantSeqs = new Set();
        slots.forEach(slot => { relevantSeqs.add(slot); relevantSeqs.add(slot + 1); });
        candidates = frames.filter(f => relevantSeqs.has(f.sequence));
    } else {
        candidates = frames;
    }
    const failedFrames = candidates.filter(f => f.quality_gate === 'vlm_qa_failed' || f.quality_gate === 'sequence_review_flagged');
    if (!failedFrames.length) return { proceed: true, override: false };
    const frameSeqs = failedFrames.map(f => f.sequence).join(', ');
    const confirmed = await customConfirm(`⚠️ 警告：检测到第 ${frameSeqs} 帧未通过一致性审查。\n\n如果强行生成视频，对应的视频分段可能存在跳变、无动作或动作不一致的缺陷。\n\n确定要强制继续生成视频吗？`);
    return { proceed: confirmed, override: confirmed };
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

    const reviewCheck = await confirmSequenceReviewOverride(ownerIdea, [slot]);
    if (!reviewCheck.proceed) return;

    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const slotCard = document.getElementById(`video-slot-${slot}`);
    if (!progress || !meta || !slotCard) return;

    renderSlotPending('video', slot, '重试中...');

    progress.style.display = 'flex';
    meta.textContent = `正在重试生成第 ${slot} 段视频...`;

    const controller = new AbortController();
    const rec = beginIdeaTask(ownerIdea.id, 'videos', null, controller);
    // 只标记这一段在范围内：renderVideosForIdea 靠这个字段决定哪些槽位该画
    // "等待中"，不设置的话会误把其余所有未生成槽位也画成"等待中"。
    rec.targetSlots = [slot];
    setVideoGridButtonsBusy(true);
    // 与服务器失联（非任务真的失败）：后端渲染线程不知道客户端已经放弃，会继续
    // 跑到底——这种情况绝不能在 finally 里 endIdeaTask，否则没人能重新接上它。
    let disconnected = false;

    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(ownerIdea),
                display_title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
                target_slots: [slot],
                override_flagged: reviewCheck.override,
                merge_speed: typeof getMergeSpeed === 'function' ? getMergeSpeed() : 2
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
            if (isViewingIdea(ownerIdea.id)) {
                progress.style.display = 'none';
                setVideoGridButtonsBusy(false);
            }
        }
    }
}

// 与 setVideoGridButtonsBusy 同理，管的是每张卡片上的「上传」按钮。
function setVideoUploadButtonsBusy(busy) {
    document.querySelectorAll('#videos-grid .upload-video-btn').forEach(btn => {
        btn.disabled = busy;
        btn.title = busy ? '该创意的视频序列正在生成/重试中，请稍候' : '手动上传本地视频文件覆盖此槽位';
    });
}

// 由各视频槽位卡片的「上传」按钮调用：记下目标槽位，触发共用的隐藏文件选择器
// （见 index.html #video-upload-input 与 app.js 里的 change 监听）。
function triggerVideoUpload(slot) {
    const input = document.getElementById('video-upload-input');
    if (!input) return;
    input.dataset.slot = String(slot);
    input.click();
}

// 帧/视频槽位的手动改动（上传覆盖、换位、复制）同一时刻只允许一件在飞。
// 按钮路径本来靠 setXxxButtonsBusy 挡住并发，但拖拽绕过按钮——连着往几张卡片上
// 甩文件会让多个请求同时读改写同一份 manifest，谁最后写完就以谁为准，前面几次
// 的结果被静默吃掉。这里是所有入口共用的那道闸。
let _slotMutationBusy = false;

function beginSlotMutation(what) {
    if (_slotMutationBusy) {
        showToast(`还有一次${what}没完成，请等它结束`, "error");
        return false;
    }
    _slotMutationBusy = true;
    return true;
}

function endSlotMutation() {
    _slotMutationBusy = false;
}

// request() 返回它表示"这次操作已经由调用方自行了结了"（例如 409 之后弹确认框、
// 用户点了取消，或确认后带 force 重新走了一遍）：外壳跳过 apply 与成功提示，
// 只负责把闸门和忙态收干净。
const SLOT_MUTATION_HANDLED = Symbol('slot-mutation-handled');

function _slotScopeFlags(scope) {
    return {
        frames: scope === 'frames' || scope === 'both',
        videos: scope === 'videos' || scope === 'both',
    };
}

function _setSlotScopeBusy(scope, busy) {
    const t = _slotScopeFlags(scope);
    if (t.frames) setFrameGridButtonsBusy(busy);
    if (t.videos) {
        setVideoGridButtonsBusy(busy);
        setVideoUploadButtonsBusy(busy);
    }
}

function _renderSlotScope(ownerIdea, scope) {
    if (!isViewingIdea(ownerIdea.id)) return;
    const t = _slotScopeFlags(scope);
    if (t.frames) renderFramesForIdea(ownerIdea);
    if (t.videos) renderVideosForIdea(ownerIdea);
}

/**
 * 槽位改动的统一事务外壳。
 *
 * 六个入口（上传帧 / 上传视频 / 帧换位 / 视频换位 / 删除整拍 / 恢复整拍）此前
 * 各自手写同一套流程：查前置条件 → 抢闸门 → 画乐观占位 → 请求 → 就地 patch →
 * 重渲 → 拉权威清单 → 再重渲 → toast → finally 放闸门 + 解忙态。六份拷贝里
 * 任何一处漏掉 endSlotMutation() 就是一次静默卡死，漏掉 isViewingIdea() 判断
 * 就是把 A 创意的结果画进正看着的 B 创意。收成一处之后这两类错误没有地方再犯。
 *
 * opts：
 *   what          闸门冲突提示里的操作名
 *   ownerIdea     本次操作归属的创意——异步回来后据它判断还该不该写 DOM
 *   scope         'frames' | 'videos' | 'both'：重渲涉及哪些网格
 *   busyScope     忙态涉及哪些网格，默认同 scope
 *   blockOn       哪些后台任务在跑时不许改动（数组），默认按 scope 推
 *   guard         false＝闸门已由外层持有（批量上传、二次确认后的续跑）
 *   requireIdea   需要 currentIdea（默认 true）
 *   requirePrompt 需要 prompt_block（上传/删除类）
 *   meta          请求期间写进 meta 行的文案
 *   pending       乐观 UI（画转圈占位等），在请求前调用
 *   request       async () => data —— 唯一各不相同的那一段；抛错即失败
 *   bust          (data) => [清单项] 需要作废浏览器缓存的条目
 *   beforeApply   async (data) => void，写 manifest patch 之前（如落回提示词块）
 *   patch         (data) => manifest patch；不给就跳过整套 apply 流程
 *   success       (data) => string|null 成功提示
 *   extraToasts   (data) => [[文案, 类型]] 附加提示
 *   failure       (e) => string 失败提示
 *
 * 返回 true＝成功，false＝失败/前置条件不满足/被调用方自行了结。
 */
async function mutateSlot(opts) {
    const {
        what = '槽位改动', scope = 'both', guard = true,
        requireIdea = true, requirePrompt = false,
        meta, pending, request, bust, beforeApply, patch,
        success, extraToasts, failure,
    } = opts;
    const busyScope = opts.busyScope || scope;

    const ownerIdea = opts.ownerIdea || currentIdea;
    if (requireIdea && !ownerIdea) {
        showToast('请先激发一个创意点子！', 'error');
        return false;
    }
    if (requirePrompt && !(ownerIdea && ownerIdea.prompt_block)) {
        showToast('请先激发一个创意点子！', 'error');
        return false;
    }

    const blockOn = opts.blockOn || Object.entries(_slotScopeFlags(scope))
        .filter(([, on]) => on).map(([k]) => k);
    for (const type of blockOn) {
        if (isIdeaTaskActive(ownerIdea.id, type)) {
            showToast(type === 'frames'
                ? '该创意的帧序列正在生成/修复中，请稍候'
                : '该创意的视频序列正在生成/重试中，请稍候', 'error');
            return false;
        }
    }

    if (guard && !beginSlotMutation(what)) return false;

    const metaEl = document.getElementById(
        scope === 'frames' ? 'frames-meta' : 'videos-meta');
    if (meta && metaEl && isViewingIdea(ownerIdea.id)) metaEl.textContent = meta;
    if (pending && isViewingIdea(ownerIdea.id)) pending();
    _setSlotScopeBusy(busyScope, true);

    try {
        const data = await request();
        if (data === SLOT_MUTATION_HANDLED) return false;

        if (bust) (bust(data) || []).forEach(e => e && bustImageCache(e.url || e.file));
        if (beforeApply) await beforeApply(data);

        if (patch) {
            // 服务端已经落盘、也把新记录回给我们了：先就地更新本地清单让界面立刻
            // 反映结果，随后的 reloadManifestIntoIdea 再拉一次权威清单（它同时
            // 负责把创意库持久化到服务端）——界面不该干等那一次网络往返。
            applyManifestPatchToIdea(ownerIdea, patch(data));
            _renderSlotScope(ownerIdea, scope);
            await reloadManifestIntoIdea(ownerIdea);
            _renderSlotScope(ownerIdea, scope);
        }

        const msg = success ? success(data) : null;
        if (msg) showToast(msg, 'success');
        (extraToasts ? extraToasts(data) || [] : []).forEach(
            ([text, type]) => showToast(text, type || 'info'));
        return true;
    } catch (e) {
        console.error(`[slot] ${what} 失败:`, e);
        showToast(failure ? failure(e) : `${what}失败: ${e.message}`, 'error');
        _renderSlotScope(ownerIdea, scope);
        return false;
    } finally {
        if (guard) endSlotMutation();
        if (isViewingIdea(ownerIdea.id)) _setSlotScopeBusy(busyScope, false);
    }
}

/** 统一的 JSON 请求 + 错误归一：非 2xx 或回包里带 error/status=error 都算失败。 */
async function slotRequestJson(url, init) {
    const resp = await fetch(url, init);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.status === 'error' || data.error) {
        throw new Error(data.error || data.message || `HTTP ${resp.status}`);
    }
    return data;
}

function slotPostJson(url, payload) {
    return slotRequestJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

// 手动上传/换位改的都是同名文件（img_NNN.webp、vid_NNN.mp4）：路径不变、内容变了，
// 浏览器凭路径命中缓存，重渲出来的还是旧图/旧片——必须把受影响槽位的缓存版本推进
// 一格，下一次渲染才会回源。entries 是后端回的清单，keyName 是槽位字段名
// （视频是 slot、帧是 sequence），wanted 是这次真正动过的槽位号。
// 服务端已经落盘、也已经把新记录回给我们了：先就地更新本地清单，让调用方能立刻重渲，
// 拖完手就看到结果。随后的 reloadManifestIntoIdea 仍会拉一次权威清单——它同时负责把
// 创意库持久化到服务端（一次网络往返），界面不该干等着它才敢刷新。
function applyManifestPatchToIdea(ownerIdea, patch) {
    if (!ownerIdea || !patch) return;
    if (!ownerIdea.frameRun) {
        ownerIdea.frameRun = { title: ownerIdea.title, frames: [], videos: [] };
    }
    const run = ownerIdea.frameRun;
    if (Array.isArray(patch.frames)) run.frames = patch.frames;
    if (Array.isArray(patch.videos)) run.videos = patch.videos;
    if (patch.frame) {
        run.frames = Array.isArray(run.frames) ? run.frames : [];
        const idx = run.frames.findIndex(f => f.sequence === patch.frame.sequence);
        if (idx === -1) run.frames.push(patch.frame); else run.frames[idx] = patch.frame;
        run.frames.sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
    }
    if (patch.video) {
        run.videos = Array.isArray(run.videos) ? run.videos : [];
        const idx = run.videos.findIndex(v => v.slot === patch.video.slot);
        if (idx === -1) run.videos.push(patch.video); else run.videos[idx] = patch.video;
        run.videos.sort((a, b) => (a.slot || 0) - (b.slot || 0));
    }
    // 上传/换位都改变了片段内容，后端已把 merged_video 从 manifest 删掉；
    // 本地也要跟着删，否则成品播放器还挂在那里放一段已经对不上的旧成片。
    if (patch.dropMerged) delete run.merged_video;
}

// 手动上传本地视频文件覆盖某个槽位（例如外部渠道单独补的片段，或对自动化产出
// 不满意想换一版）。上传前后端会用与自动生成同一套 verify_video_anchors 校验
// 首尾帧是否匹配该槽位期望的锚点图——不匹配时先返回 409，这里弹确认框，用户
// 确认"仍要覆盖"后带 force=true 重新提交才会真正落盘，防止选错文件误覆盖好片段。
async function uploadVideoToSlot(slot, file, force = false) {
    const ownerIdea = currentIdea;
    const progress = document.getElementById('videos-progress');
    if (ownerIdea && !document.getElementById(`video-slot-${slot}`)) return;

    return mutateSlot({
        what: `第 ${slot} 段视频上传`,
        ownerIdea, scope: 'videos', requirePrompt: true,
        // force=true 是同一次用户操作在 409 确认后的续跑，闸门在外层那次调用手里
        guard: !force,
        meta: `正在上传第 ${slot} 段视频...`,
        pending: () => {
            renderSlotPending('video', slot, '上传中...');
            if (progress) progress.style.display = 'flex';
        },
        request: async () => {
            const formData = new FormData();
            formData.append('title', getIdeaSaveTitle(ownerIdea));
            formData.append('slot', String(slot));
            formData.append('prompt_block', ownerIdea.prompt_block || '');
            if (force) formData.append('force', 'true');
            formData.append('video', file, file.name);

            const resp = await fetch('/api/upload_video', { method: 'POST', body: formData });
            if (resp.status === 409) {
                // 首尾帧与该槽位期望的锚点图不符，疑似传错文件：先退回原样，
                // 由用户确认后带 force 重来，防止误覆盖一段好片子
                const data = await resp.json().catch(() => ({}));
                _renderSlotScope(ownerIdea, 'videos');
                const proceed = await customConfirm(escapeHtml(
                    data.error || '上传视频的首/尾帧与该槽位期望的锚点帧不符，疑似传错文件。是否仍要强制覆盖？'));
                if (proceed) await uploadVideoToSlot(slot, file, true);
                else showToast('已取消上传。', 'info');
                return SLOT_MUTATION_HANDLED;
            }
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.status !== 'ok' || !data.video) {
                throw new Error(data.message || data.error || `HTTP ${resp.status}`);
            }
            return data;
        },
        // 覆盖的是同名 vid_NNN.mp4：不推进缓存版本，卡片会继续播旧片
        bust: (d) => [d.video],
        patch: (d) => ({ video: d.video, dropMerged: true }),
        success: () => `第 ${slot} 段视频已手动覆盖成功。`,
    }).finally(() => {
        if (progress && ownerIdea && isViewingIdea(ownerIdea.id)) progress.style.display = 'none';
    });
}

// 视频槽位之间的手动换位/复制（拖拽一张视频卡片到另一张卡片上时调用，见
// media_renderer.enableVideoSlotDnd）。槽位编号与网格位置固定不变，动的只是"哪段
// 视频落在哪个槽位"；目标槽位空着时后端自动退化成搬运（源槽位随之清空），
// mode='copy' 则是复制一份过去、源槽位原样保留。
// 换位后首尾帧多半不再对应新槽位期望的锚点图，后端会把 anchor_check 标成"未重新
// 校验"，这里额外提示一句，免得事后误以为它验过了。
async function swapVideoSlots(fromSlot, toSlot, mode = 'swap') {
    if (!Number.isFinite(fromSlot) || !Number.isFinite(toSlot) || fromSlot === toSlot) return false;
    return mutateSlot({
        what: '视频换位',
        scope: 'videos',
        meta: mode === 'copy'
            ? `正在把第 ${fromSlot} 段视频复制到第 ${toSlot} 段...`
            : `正在把第 ${fromSlot} 段与第 ${toSlot} 段视频换位...`,
        request: () => slotPostJson('/api/swap_video_slots', {
            title: getIdeaSaveTitle(currentIdea),
            from_slot: fromSlot, to_slot: toSlot, mode,
        }),
        // 换位是"文件名不变、内容互换"——不推进缓存版本的话，重渲出来的两张卡片
        // 会原样播浏览器缓存里的旧片，界面看着像什么都没发生。
        bust: (d) => (d.videos || []).filter(
            v => v && [fromSlot, toSlot].includes(Number(v.slot))),
        patch: (d) => ({ videos: d.videos, dropMerged: true }),
        success: (d) => `已把第 ${fromSlot} 段视频`
            + `${mode === 'copy' ? '复制' : (d.moved ? '搬运' : '换位')}`
            + `到第 ${toSlot} 段（首尾帧未重新校验，必要时重跑该槽位）。`,
    });
}

// 帧槽位之间的手动换位/复制（拖一张帧卡片到另一张帧卡片上时调用）。与视频换位同构，
// 多出来的是 i2i 血统：帧序列是单链，任何一格的图被换掉，较小那个槽位之后的帧就都还
// 派生自旧链——后端统一标 stale_lineage，前端据此显示 Stale 徽标。
async function swapFrameSlots(fromSeq, toSeq, mode = 'swap') {
    if (!Number.isFinite(fromSeq) || !Number.isFinite(toSeq) || fromSeq === toSeq) return false;
    return mutateSlot({
        what: '帧换位',
        // 帧换位会让视频锚点失效，两个网格都要重渲；但只有帧网格在跑任务时才该
        // 拦住这次操作，忙态也只落在帧网格上（与改造前一致）
        scope: 'both', busyScope: 'frames', blockOn: ['frames'],
        meta: mode === 'copy'
            ? `正在把第 ${fromSeq} 帧复制到第 ${toSeq} 帧...`
            : `正在把第 ${fromSeq} 帧与第 ${toSeq} 帧换位...`,
        request: () => slotPostJson('/api/swap_frame_slots', {
            title: getIdeaSaveTitle(currentIdea),
            from_sequence: fromSeq, to_sequence: toSeq, mode,
        }),
        bust: (d) => (d.frames || []).filter(
            f => f && [fromSeq, toSeq].includes(Number(f.sequence))),
        patch: (d) => ({ frames: d.frames, dropMerged: true }),
        success: (d) => `已把第 ${fromSeq} 帧`
            + `${mode === 'copy' ? '复制' : (d.moved ? '搬运' : '换位')}到第 ${toSeq} 帧。`,
        extraToasts: (d) => {
            const affected = d.affected_video_slots || [];
            return affected.length
                ? [[`受影响的视频片段：VID ${affected.map(padSlot).join(' / ')}，建议重跑。`, 'info']]
                : [];
        },
    });
}

// 一次拖进来多张图片：从落点那一格起按文件名顺序往后依次填。文件多于剩余槽位时
// 多出来的直接丢弃并如实告知——帧槽位数由提示词条数决定，不能凭上传凭空多出几帧。
async function uploadFramesFromDrop(startSeq, files) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    const images = Array.from(files || []).filter(f => isImageFileLike(f))
        .sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh-CN', { numeric: true }));
    if (!images.length) return;
    if (images.length === 1) {
        await uploadFrameToSlot(startSeq, images[0]);
        return;
    }

    const totalSlots = countPromptImageSlots(currentIdea);
    const room = totalSlots ? Math.max(0, totalSlots - startSeq + 1) : images.length;
    const batch = images.slice(0, room);
    if (!batch.length) {
        showToast(`第 ${startSeq} 帧之后已没有槽位可填。`, "error");
        return;
    }

    const endSeq = startSeq + batch.length - 1;
    const dropped = images.length - batch.length;
    const proceed = await customConfirm(
        `将按文件名顺序把 ${batch.length} 张图片依次覆盖到 IMG ${String(startSeq).padStart(3, '0')} – `
        + `IMG ${String(endSeq).padStart(3, '0')}：<br>`
        + batch.map((f, i) => `IMG ${String(startSeq + i).padStart(3, '0')} ← ${escapeHtml(f.name)}`).join('<br>')
        + (dropped ? `<br><br>另有 ${dropped} 张超出槽位范围，将被忽略。` : '')
    );
    if (!proceed) {
        showToast('已取消批量上传。', 'info');
        return;
    }

    // 批量整体持有闸门，逐张调用时不再重复抢；每张各自完成 patch/重渲，
    // 所以这里不给 patch，外壳只负责闸门与忙态。
    await mutateSlot({
        what: '批量上传',
        scope: 'frames', requirePrompt: true,
        request: async () => {
            for (let i = 0; i < batch.length; i++) {
                await uploadFrameToSlot(startSeq + i, batch[i], true);
            }
            return {};
        },
        success: () => `已批量覆盖 IMG ${padSlot(startSeq)} – IMG ${padSlot(endSeq)}`
            + `（共 ${batch.length} 张）。`,
    });
}

// 删除一整拍：图片 N 与视频 N 的提示词、落盘文件、manifest 记录一起消失，其后所有
// 图片/视频整体前移一位（槽位号是契约不是标签——视频 N 恒等于 IMG N → IMG N+1，
// 留空洞会让配对/合成的每一处都得学会跳过它，见 server.py /api/delete_slot）。
// 服务端会在重新编号前强制保存 .deleted_slots 恢复快照；确认框仍逐条
// 列清当前任务中会消失什么，但不再误导用户认为磁盘上永久不可恢复。
// opts.skipConfirm：批量删除时用——那条路径已经一次性列清了要删哪几拍，
// 每拍再弹一次确认只会让人机械点确定。返回 true/false 供批量循环判断要不要继续。
async function deleteSlotBeat(seq, opts = {}) {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return false;
    }
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'frames') || isIdeaTaskActive(ownerIdea.id, 'videos')) {
        showToast("该创意的帧/视频序列正在生成中，请等它结束后再删除", "error");
        return false;
    }
    const sequence = Number(seq);
    if (!Number.isFinite(sequence) || sequence < 1) return false;

    const slots = (typeof resolvePromptSlots === 'function' ? resolvePromptSlots(ownerIdea) : []) || [];
    const imageSlots = slots.filter(s => s.type === 'image');
    const videoSlot = slots.find(s => s.type === 'video' && s.index === sequence);
    const imageCount = imageSlots.length;
    if (imageCount <= 1) {
        showToast("本单只剩最后一张图片，删掉就没有序列了", "error");
        return false;
    }

    const label = String(sequence).padStart(3, '0');
    const tail = sequence < imageCount
        ? `<li>IMG ${String(sequence + 1).padStart(3, '0')} 及其后所有图片/视频<b>整体前移一位</b>（磁盘文件一起改名）</li>`
        : '';
    const seam = sequence > 1
        ? `<li>VID ${String(sequence - 1).padStart(3, '0')} 的尾锚点会换成另一张图，标记为待重跑</li>`
        : '';
    if (!opts.skipConfirm) {
        const proceed = await customConfirm(
            `<b>删除第 ${sequence} 拍</b>（会先自动保存恢复快照）：`
            + '<ul style="margin:8px 0 0 18px; line-height:1.7;">'
            + `<li>提示词「图片 ${sequence}」${videoSlot ? `与「视频 ${sequence}」` : ''}从提示词块中删除</li>`
            + `<li>IMG ${label}${videoSlot ? ` 与 VID ${label}` : ''} 的文件删除</li>`
            + tail + seam
            + '</ul>'
        );
        if (!proceed) {
            showToast('已取消删除。', 'info');
            return false;
        }
    }
    return mutateSlot({
        what: `删除第 ${sequence} 拍`,
        ownerIdea, scope: 'both', requirePrompt: true,
        blockOn: [],   // 前置检查在上面做过了（要同时拦帧与视频任务）
        request: () => slotPostJson('/api/delete_slot', {
            title: getIdeaSaveTitle(ownerIdea),
            sequence,
            prompt_block: ownerIdea.prompt_block || '',
        }),
        // 整体前移＝一大批同名文件换了内容，逐个作废缓存版本，否则网格拿旧图重排
        bust: (d) => [].concat(d.frames || [], d.videos || []),
        // 提示词块是这一切的源头（槽位总数按它算），必须先落到创意对象上再重渲
        beforeApply: async (d) => {
            if (d.prompt_block) {
                await applyPromptBlockToIdea(ownerIdea, d.prompt_block, d.prompt_slots);
            }
        },
        patch: (d) => ({ frames: d.frames, videos: d.videos, dropMerged: true }),
        success: (d) => `已删除第 ${sequence} 拍，其后整体前移一位（现有 ${d.image_count} 张图片）。`,
        extraToasts: (d) => {
            const out = [];
            // 撤销入口紧跟着删除出现：快照就在手边，此刻是最可能想反悔的时候。
            // 过了这一会儿仍可从 ⚙「已删除的拍」里恢复，见 openDeletedSlotsPanel。
            const snapshotId = String(d.recovery_snapshot || '')
                .split(/[\\/]/).filter(Boolean).pop();
            if (snapshotId && !opts.skipConfirm) offerSlotRestore(ownerIdea, snapshotId, sequence);
            const affected = d.affected_video_slots || [];
            if (affected.length) {
                out.push([`VID ${affected.map(padSlot).join(' / ')} 的尾锚点已改变，建议重跑这一段。`, 'info']);
            }
            return out;
        },
    });
}

// =====================================================================
// 撤销删除整拍
//   删除侧一直在写 .deleted_slots/<id>/ 快照（删除前的整份 manifest 与
//   prompt_block ＋ 所有被物理删除的媒体文件），但读回那一半此前从未实现，
//   一次误删只能靠手写一次性脚本捞回来（tools/recover_ice_cave_slot3.py）。
//   这里补上读回：紧跟删除的一次性「撤销」，以及 ⚙ 弹层里的「已删除的拍」列表。
// =====================================================================

/** 删除成功后立刻给一个限时撤销出口。30 秒是"还在看着这一格"的窗口。 */
function offerSlotRestore(ownerIdea, snapshotId, sequence) {
    showToast(`第 ${sequence} 拍已删除`, 'info', 30000, {
        label: '撤销',
        onClick: () => restoreSlotSnapshot(ownerIdea, snapshotId),
    });
}

/**
 * 恢复一份删除快照。走与 deleteSlotBeat 同一套流程：抢闸门 → 请求 → 提示词块
 * 落回创意对象 → 乐观 patch → 重渲 → 拉权威清单 → 再重渲。
 *
 * 服务端可能回 409 status='diverged'：删除之后这单又被改动过（重新生成/重试/
 * 上传等），恢复会把清单整份还原成删除前那一版、丢掉之后新产生的记录。这种
 * 情况必须由用户确认后带 force=true 重来，不能默默覆盖。
 */
async function restoreSlotSnapshot(ownerIdea, snapshotId, force = false) {
    if (!ownerIdea) return false;
    return mutateSlot({
        what: '恢复整拍',
        ownerIdea, scope: 'both',
        // force=true 是同一次用户操作在 409 确认后的续跑，闸门在外层那次调用手里
        guard: !force,
        request: async () => {
            const resp = await fetch('/api/restore_slot', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title: getIdeaSaveTitle(ownerIdea),
                    snapshot_id: snapshotId,
                    // 当前提示词块一并交给服务端存进"恢复点"，让恢复本身也可回头
                    prompt_block: ownerIdea.prompt_block || '',
                    force: !!force,
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.status === 409 && data.status === 'diverged') {
                // 删除之后这单又被改动过：恢复会把清单整份还原成删除前那一版、
                // 丢掉之后新产生的记录，必须由用户确认后带 force 重来
                const proceed = await customConfirm(escapeHtml(
                    data.error || '这单在删除之后被改动过，仍要恢复吗？'));
                if (proceed) await restoreSlotSnapshot(ownerIdea, snapshotId, true);
                else showToast('已取消恢复。', 'info');
                return SLOT_MUTATION_HANDLED;
            }
            if (!resp.ok || data.status === 'error' || data.error) {
                throw new Error(data.error || data.message || `HTTP ${resp.status}`);
            }
            return data;
        },
        // 整体后移＝一大批同名文件换了内容，逐个作废缓存版本
        bust: (d) => [].concat(d.frames || [], d.videos || []),
        beforeApply: async (d) => {
            if (d.prompt_block) {
                await applyPromptBlockToIdea(ownerIdea, d.prompt_block, d.prompt_slots);
            }
        },
        patch: (d) => ({ frames: d.frames, videos: d.videos, dropMerged: true }),
        success: (d) => `已恢复第 ${d.sequence} 拍，其后整体后移一位（现有 ${d.image_count} 张图片）。`,
        failure: (e) => `恢复失败: ${e.message}`,
    });
}

async function fetchDeletedSlots(ownerIdea) {
    const resp = await fetch('/api/deleted_slots?title='
        + encodeURIComponent(getIdeaSaveTitle(ownerIdea)));
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    return data.snapshots || [];
}

/** ⚙ 弹层里的「已删除的拍」：撤销窗口过去之后仍能从这里恢复。 */
async function openDeletedSlotsPanel() {
    if (!currentIdea) {
        showToast('请先打开一个创意', 'error');
        return;
    }
    const ownerIdea = currentIdea;
    let snapshots;
    try {
        snapshots = await fetchDeletedSlots(ownerIdea);
    } catch (e) {
        showToast(`读取已删除的拍失败: ${e.message}`, 'error');
        return;
    }
    const pending = snapshots.filter(s => !s.restored_at);
    if (!pending.length) {
        showToast('这单没有可恢复的删除记录。', 'info');
        return;
    }

    const modal = document.createElement('div');
    modal.className = 'modal active';
    modal.style.zIndex = '1100';
    modal.innerHTML = `
        <div class="modal-content glass-panel" style="max-width: 520px;">
            <div class="modal-header">
                <h3>已删除的拍</h3>
                <button class="close-btn" type="button">&times;</button>
            </div>
            <div class="modal-body deleted-slots-body"></div>
        </div>`;
    const body = modal.querySelector('.deleted-slots-body');
    pending.forEach(s => {
        const row = document.createElement('div');
        row.className = 'deleted-slot-row';
        const info = document.createElement('div');
        info.className = 'deleted-slot-info';
        const head = document.createElement('strong');
        head.textContent = `第 ${s.sequence} 拍`
            + (s.created_at ? ` · ${s.created_at.replace('T', ' ')}` : '');
        const desc = document.createElement('span');
        // 提示词原文来自 LLM，一律 textContent
        desc.textContent = s.image_prompt || '（无图片提示词记录）';
        info.append(head, desc);
        if (s.diverged) {
            const warn = document.createElement('span');
            warn.className = 'deleted-slot-warn';
            warn.textContent = '删除之后这单又被改动过，恢复会覆盖那些改动';
            info.appendChild(warn);
        }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'action-btn text-btn mini-btn';
        btn.textContent = '恢复';
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            const ok = await restoreSlotSnapshot(ownerIdea, s.id);
            if (ok) close(); else btn.disabled = false;
        });
        row.append(info, btn);
        body.appendChild(row);
    });

    const close = () => {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 200);
    };
    modal.querySelector('.close-btn').addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    document.body.appendChild(modal);
}

// 提示词块被服务端改写后落回创意对象：currentIdea、localStorage 快照、创意库、
// 提示词页展示的原始 Markdown 四处必须一起更新——只改其中一处，下一次生成/重试
// 读到的还是旧提示词（同 fixFrameIssue 里改写提示词后的那套处理）。
async function applyPromptBlockToIdea(ownerIdea, promptBlock, promptSlots) {
    if (!ownerIdea || !promptBlock) return;
    ownerIdea.prompt_block = promptBlock;
    // 结构化槽位契约必须跟着换：resolvePromptSlots 优先用它，留着删除前那一份
    // 会让帧网格继续按旧条数画格子。后端没回新的就删掉，退回正则解析兜底。
    if (promptSlots) ownerIdea.prompt_slots = promptSlots;
    else delete ownerIdea.prompt_slots;
    if (currentIdea && currentIdea.id === ownerIdea.id) saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === ownerIdea.id);
    if (existingIdx !== -1) {
        savedIdeas[existingIdx].prompt_block = promptBlock;
        if (promptSlots) savedIdeas[existingIdx].prompt_slots = promptSlots;
        else delete savedIdeas[existingIdx].prompt_slots;
        await saveLibrary();
    }
    if (isViewingIdea(ownerIdea.id)) {
        const blockEl = document.getElementById('idea-prompt-block');
        if (blockEl) blockEl.textContent = promptBlock;
    }
}

// 提示词里「图片 N:」的条数＝帧槽位总数（与 renderFramesForIdea 用的是同一套契约）。
// 解析不出来时返回 0，调用方按"不限制"处理，由后端兜底。
function countPromptImageSlots(idea) {
    try {
        const slots = resolvePromptSlots(idea) || [];
        return slots.filter(s => s.type === 'image').length;
    } catch (e) {
        return 0;
    }
}

// 手动上传本地图片覆盖某个帧槽位（拖图片到帧卡片上，见 media_renderer.enableFrameSlotDnd）。
// 后端会把图片按本单画幅裁剪后转存成同款 img_NNN.webp，并把该帧的机器判定全部作废、
// 其后各帧标为 stale_lineage（i2i 血统在这一帧断开）。
// skipGuard：批量上传（uploadFramesFromDrop）时用——闸门由那次批量操作整体持有。
async function uploadFrameToSlot(seq, file, skipGuard = false) {
    const ownerIdea = currentIdea;
    return mutateSlot({
        what: `第 ${seq} 帧上传`,
        ownerIdea, scope: 'both', busyScope: 'frames',
        blockOn: ['frames'], requirePrompt: true,
        // skipGuard：批量上传时闸门由那次批量操作整体持有
        guard: !skipGuard,
        pending: () => renderSlotPending('image', seq, '上传中...'),
        request: async () => {
            const formData = new FormData();
            formData.append('title', getIdeaSaveTitle(ownerIdea));
            formData.append('sequence', String(seq));
            formData.append('prompt_block', ownerIdea.prompt_block || '');
            formData.append('image', file, file.name);
            return slotRequestJson('/api/upload_frame', { method: 'POST', body: formData });
        },
        // 覆盖的是同名 img_NNN.webp：不推进缓存版本，卡片会继续显示旧图
        bust: (d) => [d.frame],
        patch: (d) => ({ frame: d.frame, dropMerged: true }),
        // 批量上传时逐张弹提示会刷屏，收尾由 uploadFramesFromDrop 统一报一次
        success: () => skipGuard ? null : `第 ${seq} 帧已用本地图片覆盖。`,
        extraToasts: (d) => {
            const affected = (!skipGuard && d.affected_video_slots) || [];
            return affected.length
                ? [[`这张图是 VID ${affected.map(padSlot).join(' / ')} 的首尾锚点，建议重跑这些片段。`, 'info']]
                : [];
        },
        failure: (e) => `第 ${seq} 帧上传失败: ${e.message}`,
    });
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

    const reviewCheck = await confirmSequenceReviewOverride(ownerIdea, slots);
    if (!reviewCheck.proceed) return;

    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    if (!progress || !meta) return;

    slots.forEach(slot => {
        const slotCard = document.getElementById(`video-slot-${slot}`);
        if (slotCard) {
            renderSlotPending('video', slot, '重试中...');
        }
    });

    progress.style.display = 'flex';
    meta.textContent = `正在重试缺失的 ${slots.length} 段视频（槽位 ${slots.join(', ')}）...`;

    const controller = new AbortController();
    const rec = beginIdeaTask(ownerIdea.id, 'videos', null, controller);
    rec.targetSlots = slots;
    setVideoGridButtonsBusy(true);
    // 与服务器失联（非任务真的失败）：后端渲染线程不知道客户端已经放弃，会继续
    // 跑到底——这种情况绝不能在 finally 里 endIdeaTask，否则没人能重新接上它。
    let disconnected = false;
    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(ownerIdea),
                display_title: ownerIdea.title,
                prompt_block: ownerIdea.prompt_block,
                target_slots: slots,
                override_flagged: reviewCheck.override,
                merge_speed: typeof getMergeSpeed === 'function' ? getMergeSpeed() : 2
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
                    renderVideoSlotFailed(evData.index, evData.message || '生成失败', '生成失败', true);
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
            if (isViewingIdea(ownerIdea.id)) {
                progress.style.display = 'none';
                setVideoGridButtonsBusy(false);
            }
        }
    }
}
