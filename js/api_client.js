// --- api_client.js ---

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
    });

    toggleBtn.addEventListener('click', toggleLog);

    clearBtn.addEventListener('click', () => {
        output.textContent = '';
    });

    function scrollToBottom() {
        if (autoscrollChk && autoscrollChk.checked) {
            const body = document.getElementById('log-panel-body');
            if (body) {
                body.scrollTop = body.scrollHeight;
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
        eventSource = new EventSource('/api/logs/stream');

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

        const streamResponse = await fetch(`/api/compose-stream?task_id=${taskId}`, {
            signal: controller.signal
        });

        if (!streamResponse.ok) {
            const errText = await streamResponse.text();
            throw new Error(`HTTP ${streamResponse.status}: ${errText}`);
        }

        const reader = streamResponse.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = '';
        let manifestData = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed) continue;

                if (trimmed.startsWith('data: ')) {
                    const jsonStr = trimmed.substring(6);
                    try {
                        const parsed = JSON.parse(jsonStr);
                        if (parsed.type === 'frame') {
                            const f = parsed.data.frame;
                            
                            // Save incrementally
                            if (currentIdea) {
                                if (!currentIdea.frameRun) {
                                    currentIdea.frameRun = { title: currentIdea.title, frames: [] };
                                }
                                if (!currentIdea.frameRun.frames) {
                                    currentIdea.frameRun.frames = [];
                                }
                                const idx = currentIdea.frameRun.frames.findIndex(item => item.sequence === f.sequence);
                                if (idx !== -1) {
                                    currentIdea.frameRun.frames[idx] = f;
                                } else {
                                    currentIdea.frameRun.frames.push(f);
                                    currentIdea.frameRun.frames.sort((a, b) => a.sequence - b.sequence);
                                }
                                saveCurrentIdeaState();
                                const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
                                if (existingIdx !== -1) {
                                    savedIdeas[existingIdx].frameRun = currentIdea.frameRun;
                                    saveLibrary();
                                }
                                // Update UI immediately so the user doesn't have to wait for the entire stream to finish
                                renderFramesForIdea(currentIdea);
                            }
                        } else if (parsed.type === 'result') {
                            manifestData = parsed.data;
                        } else if (parsed.type === 'error') {
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (err) {
                        console.error("Error parsing frame SSE data", err);
                        if (err.message) throw err;
                    }
                }
            }
        }

        if (manifestData) {
            currentIdea.frameRun = manifestData;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifestData;
                await saveLibrary();
            }
            renderFramesForIdea(currentIdea);
            showToast(`第 ${seq} 帧重试生成成功。`, "success");
        }
    } catch (e) {
        console.error(`Failed to retry frame ${seq}:`, e);
        showToast(`第 ${seq} 帧重试失败: ${e.message}`, "error");
        
        // Restore state by reloading manifest or rendering whatever is local
        try {
            const resp = await fetch(`/api/get_manifest?title=${encodeURIComponent(currentIdea.title)}`);
            if (resp.ok) {
                const manifest = await resp.json();
                currentIdea.frameRun = manifest;
                saveCurrentIdeaState();
                const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
                if (existingIdx !== -1) {
                    savedIdeas[existingIdx].frameRun = manifest;
                    await saveLibrary();
                }
            }
        } catch (err) {
            console.error("Failed to load partial manifest after failure:", err);
        }
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

        const streamResponse = await fetch(`/api/compose-stream?task_id=${taskId}`, {
            signal: controller.signal
        });

        if (!streamResponse.ok) {
            const errText = await streamResponse.text();
            throw new Error(`HTTP ${streamResponse.status}: ${errText}`);
        }

        const reader = streamResponse.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = '';
        let manifestData = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed) continue;

                if (trimmed.startsWith('data: ')) {
                    const jsonStr = trimmed.substring(6);
                    try {
                        const parsed = JSON.parse(jsonStr);
                        if (parsed.type === 'video_start') {
                            const idx = parsed.data.index;
                            meta.textContent = `正在生成视频: 正在处理第 ${idx} 段视频...`;
                        } else if (parsed.type === 'video_done') {
                            const v = parsed.data.video;
                            const idx = parsed.data.index;
                            
                            const slotEl = document.getElementById(`video-slot-${idx}`);
                            if (slotEl) {
                                slotEl.className = 'frame-card';
                                slotEl.style.cursor = 'default';
                                slotEl.innerHTML = `
                                    <video src="${v.url}" controls style="width:100%; aspect-ratio: 9/16; object-fit: cover; border-radius: 5px; display: block; background: #03050c;"></video>
                                    <span>VID ${String(v.slot).padStart(3, '0')}</span>
                                `;
                            }
                        } else if (parsed.type === 'video_error') {
                            const idx = parsed.data.index;
                            const msg = parsed.data.message || '生成失败';
                            const slotEl = document.getElementById(`video-slot-${idx}`);
                            if (slotEl) {
                                slotEl.className = 'frame-card video-failed-card';
                                slotEl.innerHTML = `
                                    <div class="video-failed-placeholder">
                                        <span class="error-icon">⚠️</span>
                                        <span class="error-text" title="${msg}">生成失败</span>
                                        <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${idx}">重试</button>
                                    </div>
                                    <span>VID ${String(idx).padStart(3, '0')}</span>
                                `;
                                slotEl.querySelector('.retry-video-btn').addEventListener('click', (e) => {
                                    e.stopPropagation();
                                    retrySingleVideo(idx);
                                });
                            }
                        } else if (parsed.type === 'result') {
                            manifestData = parsed.data;
                        } else if (parsed.type === 'error') {
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (err) {
                        console.error("Error parsing videos SSE data", err);
                        if (err.message) throw err;
                    }
                }
            }
        }

        if (manifestData) {
            currentIdea.frameRun = manifestData;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifestData;
                await saveLibrary();
            }
            renderVideosForIdea(currentIdea);
            showToast(`视频第 ${slot} 段重试成功。`, "success");
        }
    } catch (e) {
        console.error("Failed to retry video:", e);
        meta.textContent = `视频第 ${slot} 段重试失败: ${e.message}`;
        showToast(`视频第 ${slot} 段重试失败: ${e.message}`, "error");
        
        const slotEl = document.getElementById(`video-slot-${slot}`);
        if (slotEl) {
            slotEl.className = 'frame-card video-failed-card';
            slotEl.innerHTML = `
                <div class="video-failed-placeholder">
                    <span class="error-icon">⚠️</span>
                    <span class="error-text">生成失败</span>
                    <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${slot}">重试</button>
                </div>
                <span>VID ${String(slot).padStart(3, '0')}</span>
            `;
            slotEl.querySelector('.retry-video-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleVideo(slot);
            });
        }
    } finally {
        progress.style.display = 'none';
        activeBackgroundTasks.videos = false;
        updateTabStatusDot();
    }
}

