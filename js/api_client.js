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

/**
 * 把一个 'frame' 事件合并进 currentIdea（幂等：同序号覆盖）。
 * 注意：这里只写 localStorage 快照，不再每帧向服务端 POST 整个库
 * （旧行为在长任务里造成持续的大请求）；库同步集中在任务终态时进行。
 */
function applyFrameEventToIdea(f) {
    if (!f || !currentIdea) return;
    if (!currentIdea.frameRun) currentIdea.frameRun = { title: currentIdea.title, frames: [] };
    if (!currentIdea.frameRun.frames) currentIdea.frameRun.frames = [];
    const idx = currentIdea.frameRun.frames.findIndex(item => item.sequence === f.sequence);
    if (idx !== -1) {
        currentIdea.frameRun.frames[idx] = f;
    } else {
        currentIdea.frameRun.frames.push(f);
        currentIdea.frameRun.frames.sort((a, b) => a.sequence - b.sequence);
    }
    saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
    if (existingIdx !== -1) savedIdeas[existingIdx].frameRun = currentIdea.frameRun;
}

/** 任务终态时把 manifest 同步进 currentIdea 与创意库（含服务端持久化）。 */
async function syncFrameRunToLibrary(manifestData) {
    if (!currentIdea || !manifestData) return;
    currentIdea.frameRun = manifestData;
    saveCurrentIdeaState();
    const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
    if (existingIdx !== -1) {
        savedIdeas[existingIdx].frameRun = manifestData;
        await saveLibrary();
    }
}

/** 失败后从服务端 manifest 恢复已完成的部分结果。 */
async function reloadManifestIntoIdea() {
    if (!currentIdea) return;
    try {
        const resp = await fetch(`/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(currentIdea))}`);
        if (resp.ok) {
            await syncFrameRunToLibrary(await resp.json());
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
        if (parsed.framesTaskId) {
            activeBackgroundTasks.framesTaskId = parsed.framesTaskId;
            streamFramesProgress(parsed.framesTaskId);
        }
        if (parsed.videosTaskId) {
            activeBackgroundTasks.videosTaskId = parsed.videosTaskId;
            streamVideosProgress(parsed.videosTaskId);
        }
        if (parsed.coverTaskId) {
            activeBackgroundTasks.coverTaskId = parsed.coverTaskId;
            streamCoverProgress(parsed.coverTaskId);
        }
    } catch (e) {
        console.error("Failed to resume active background tasks:", e);
    }
}

function initLocalServiceLogs() {
    const drawer = document.getElementById('log-panel-drawer');
    const header = document.getElementById('log-panel-header');
    const toggleBtn = document.getElementById('toggle-log-btn');
    const clearBtn = document.getElementById('clear-log-btn');
    const output = document.getElementById('log-output-pre');
    const statusDot = drawer?.querySelector('.log-panel-status-dot');
    const autoscrollChk = document.getElementById('log-autoscroll-chk');
    
    if (!drawer || !header || !output) return;

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

    clearBtn.addEventListener('click', () => {
        output.textContent = '';
    });

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
            output.textContent = '[系统] 已连接到本地服务日志流...\n';
        });

        eventSource.addEventListener('history', (e) => {
            try {
                const parsed = JSON.parse(e.data);
                // Server sends {type, data} wrapper via _open_sse_stream
                const payload = parsed.data || parsed;
                if (payload.lines && payload.lines.length) {
                    output.textContent = payload.lines.join('');
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
                    output.textContent += payload.text;
                    // Limit total text length to prevent memory bloat (e.g. keep last 200,000 chars)
                    if (output.textContent.length > 200000) {
                        output.textContent = output.textContent.substring(output.textContent.length - 150000);
                    }
                    scrollToBottom();
                }
            } catch (err) {
                console.error("Failed to parse log line", err);
            }
        });

        eventSource.addEventListener('error', (e) => {
            if (statusDot) statusDot.className = 'log-panel-status-dot';
            output.textContent += '\n[错误] 与本地服务日志流断开连接，正在尝试重新连接...\n';
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

    activeBackgroundTasks.frames = true;
    updateTabStatusDot();

    const controller = new AbortController();

    try {
        const response = await fetch('/api/generate_frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: currentIdea.title,
                prompt_block: currentIdea.prompt_block,
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

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-frame-${seq}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
                if (type === 'frame') {
                    applyFrameEventToIdea(evData && evData.frame);
                    if (currentIdea) renderFramesForIdea(currentIdea);
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${evData.attempt} 次）...`;
                }
            }
        });

        if (watch.status === 'failed') throw new Error(watch.error || '未知错误');

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result);
            renderFramesForIdea(currentIdea);
            showToast(`第 ${seq} 帧重试生成成功。`, "success");
        }
    } catch (e) {
        console.error(`Failed to retry frame ${seq}:`, e);
        showToast(`第 ${seq} 帧重试失败: ${e.message}`, "error");

        // Restore state by reloading manifest or rendering whatever is local
        await reloadManifestIntoIdea();
        renderFramesForIdea(currentIdea);
    } finally {
        progress.style.display = 'none';
        activeBackgroundTasks.frames = false;
        updateTabStatusDot();
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

    activeBackgroundTasks.videos = true;
    updateTabStatusDot();

    const controller = new AbortController();
    
    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: currentIdea.title,
                prompt_block: currentIdea.prompt_block,
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

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-video-${slot}`,
            signal: controller.signal,
            onEvent: (type, evData) => {
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
            await syncFrameRunToLibrary(watch.result);
            renderVideosForIdea(currentIdea);
            showToast(`视频第 ${slot} 段重试成功。`, "success");
        }
    } catch (e) {
        console.error("Failed to retry video:", e);
        meta.textContent = `视频第 ${slot} 段重试失败: ${e.message}`;
        showToast(`视频第 ${slot} 段重试失败: ${e.message}`, "error");
        renderVideoSlotFailed(slot, e.message || '生成失败');
    } finally {
        progress.style.display = 'none';
        activeBackgroundTasks.videos = false;
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
    activeBackgroundTasks.videos = true;
    updateTabStatusDot();

    const controller = new AbortController();
    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: currentIdea.title,
                prompt_block: currentIdea.prompt_block,
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

        const watch = await watchTaskUntilTerminal(taskId, {
            label: `retry-missing-videos`,
            signal: controller.signal,
            onEvent: (type, evData) => {
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
            await syncFrameRunToLibrary(watch.result);
            renderVideosForIdea(currentIdea);
        }
        showToast("缺失片段重试完成，正在尝试重新合并...", "success");
        // 自动再合并一次；若仍有缺口，mergeVideos 会再次给出重试/强制选项
        if (typeof mergeVideos === 'function') {
            await mergeVideos();
        }
    } catch (e) {
        console.error("Failed to retry missing videos:", e);
        meta.textContent = `重试缺失片段失败: ${e.message}`;
        showToast(`重试缺失片段失败: ${e.message}`, "error");
    } finally {
        progress.style.display = 'none';
        activeBackgroundTasks.videos = false;
        updateTabStatusDot();
    }
}

