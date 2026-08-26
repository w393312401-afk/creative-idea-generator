// --- stepped_pipeline.js ---
// Stepped pipeline UI controller

let steppedState = null;
let steppedTaskId = null;
let steppedSSE = null;

const STAGE_LABELS = {
    'compose_phase1': '解析简报',
    'render_anchor': '渲染锚点帧',
    'review_anchor': '审核锚点帧',
    'compose_phase2': '组合提示词',
    'render_batch': '分批渲染',
    'review_batch': '批次审核',
    'final_review': '最终审查',
    'render_videos': '生成视频',
    'completed': '完成',
};

const STAGE_ICONS = {
    'compose_phase1': '📝',
    'render_anchor': '🖼',
    'review_anchor': '👁',
    'compose_phase2': '✏️',
    'render_batch': '🎨',
    'review_batch': '🔍',
    'final_review': '📋',
    'render_videos': '🎬',
    'completed': '✅',
};

const STAGE_ORDER = Object.keys(STAGE_LABELS);

/* --- API Functions --- */
function getAccessHeaders() {
    const accessCode = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    return {
        'Content-Type': 'application/json',
        'X-Access-Code': accessCode
    };
}

async function startSteppedPipeline(dimensions) {
    try {
        const res = await fetch('/api/stepped/start', {
            method: 'POST',
            headers: getAccessHeaders(),
            body: JSON.stringify(dimensions)
        });
        
        if (!res.ok) {
            const err = await res.text();
            throw new Error(`启动失败: ${res.status} ${err}`);
        }
        
        const data = await res.json();
        steppedTaskId = data.task_id;
        steppedState = data.pipeline_state;
        
        startSteppedSSE(steppedTaskId);
        updateSteppedUI(steppedState);
    } catch (e) {
        console.error('startSteppedPipeline error', e);
        alert(e.message);
    }
}

async function advanceSteppedPipeline(title, action) {
    setButtonsDisabled(true);
    try {
        const res = await fetch('/api/stepped/advance', {
            method: 'POST',
            headers: getAccessHeaders(),
            body: JSON.stringify({ title, action })
        });
        
        if (!res.ok) {
            const err = await res.text();
            throw new Error(`推进失败: ${res.status} ${err}`);
        }
        
        const data = await res.json();
        steppedState = data;
        updateSteppedUI(steppedState);
    } catch (e) {
        console.error('advanceSteppedPipeline error', e);
        alert(e.message);
    } finally {
        setButtonsDisabled(false);
    }
}

async function getSteppedStatus(title) {
    try {
        const res = await fetch(`/api/stepped/status?title=${encodeURIComponent(title)}`, {
            headers: getAccessHeaders()
        });
        if (res.ok) {
            const data = await res.json();
            steppedState = data;
            updateSteppedUI(steppedState);
        }
    } catch (e) {
        console.error('getSteppedStatus error', e);
    }
}

/* --- SSE Listener --- */
function startSteppedSSE(taskId) {
    if (steppedSSE) {
        steppedSSE.close();
    }
    
    // Using fetch/ReadableStream for SSE to allow X-Access-Code header, as requested by system conventions
    // Wait, the prompt specifically says: "SSE streaming via EventSource for real-time progress"
    // I will use EventSource as requested, appending access_code to query param just in case.
    const accessCode = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
    const url = `/api/compose-stream?task_id=${encodeURIComponent(taskId)}${accessCode ? '&access_code=' + encodeURIComponent(accessCode) : ''}`;
    
    steppedSSE = new EventSource(url);
    
    steppedSSE.addEventListener('stepped_stage', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.pipeline_state) {
                steppedState = data.pipeline_state;
                updateSteppedUI(steppedState);
            }
        } catch (err) {
            console.error('Error parsing stepped_stage', err);
        }
    });
    
    steppedSSE.addEventListener('progress', (e) => {
        try {
            const data = JSON.parse(e.data);
            renderSteppedLoading(document.getElementById('stepped-panel-content'), data.msg || '生成中...');
        } catch (err) { }
    });
    
    steppedSSE.addEventListener('result', (e) => {
        steppedSSE.close();
        if (steppedState) {
            steppedState.stage = 'completed';
            updateSteppedUI(steppedState);
        }
    });
    
    steppedSSE.addEventListener('error', (e) => {
        console.error('SSE Error', e);
        // EventSource will auto-reconnect on disconnect, but if it's a 404 or terminal, we should close
        if (steppedSSE.readyState === EventSource.CLOSED) {
            console.log('SSE connection closed');
        }
    });
}

/* --- Utility --- */
function steppedImageUrl(imagePath) {
    if (!imagePath) return '';
    // Expected to serve from /outputs/...
    // If it's an absolute path from the server filesystem like /Users/.../outputs/..., 
    // extract just the /outputs/... part.
    const outputsIdx = imagePath.indexOf('/outputs/');
    if (outputsIdx !== -1) {
        return imagePath.substring(outputsIdx);
    }
    return imagePath;
}

function setButtonsDisabled(disabled) {
    const btns = document.querySelectorAll('.review-btn');
    btns.forEach(btn => btn.disabled = disabled);
}

/* --- Renderers --- */
function renderSteppedProgress(container, state) {
    if (!state || !state.stage) return;
    
    let html = `
        <div class="stepped-progress">
            <div class="stepped-progress-track">
                <div class="stepped-progress-fill" id="stepped-progress-fill"></div>
            </div>
    `;
    
    const currentIdx = STAGE_ORDER.indexOf(state.stage);
    
    STAGE_ORDER.forEach((stageKey, idx) => {
        let stageClass = 'queued';
        let badgeHtml = '';
        
        if (idx < currentIdx) {
            stageClass = 'completed';
            // Show duration badge if available
            if (state.timings && state.timings[stageKey]) {
                const duration = Math.round(state.timings[stageKey]);
                badgeHtml = `<div class="stepped-stage-badge">耗时 ${duration}s</div>`;
            }
        } else if (idx === currentIdx) {
            if (stageKey.includes('review')) {
                stageClass = 'paused';
            } else {
                stageClass = 'active';
            }
        }
        
        html += `
            <div class="stepped-stage ${stageClass}" data-stage="${stageKey}">
                ${badgeHtml}
                <div class="stepped-stage-icon">${STAGE_ICONS[stageKey]}</div>
                <div class="stepped-stage-label">${STAGE_LABELS[stageKey]}</div>
            </div>
        `;
    });
    
    html += `</div>`;
    container.innerHTML = html;
    
    // Update progress bar fill
    const fill = container.querySelector('#stepped-progress-fill');
    if (fill && currentIdx >= 0) {
        const percent = currentIdx === 0 ? 0 : (currentIdx / (STAGE_ORDER.length - 1)) * 100;
        fill.style.width = `${percent}%`;
    }
}

function renderSteppedReviewPanel(container, state) {
    if (!state || !state.stage) return;
    
    const { stage } = state;
    
    if (stage === 'review_anchor') {
        const imgUrl = steppedImageUrl(state.anchor_image_path);
        container.innerHTML = `
            <div class="review-panel-glass anchor-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">审核锚点帧 (Frame 1)</h3>
                </div>
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击单独放大查看锚点帧">
                    <img src="${imgUrl}" alt="Anchor Frame" onerror="this.src='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiNlZWVlZWUiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZm9udC1mYW1pbHk9InNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMjAiIGZpbGw9IiM5OTk5OTkiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JbWFnZSBOb3QgRm91bmQ8L3RleHQ+PC9zdmc+'" />
                </div>
                <div class="review-actions">
                    <button class="review-btn review-btn-approve" onclick="onSteppedApprove()">
                        ✅ Approve (通过)
                    </button>
                    <button class="review-btn review-btn-retry" onclick="onSteppedRetry()">
                        🔄 Retry (重绘)
                    </button>
                </div>
            </div>
        `;
    } else if (stage === 'review_batch') {
        const currentBatch = state.current_batch_index || 0;
        const batchInfo = (state.batches && state.batches[currentBatch]) || {};
        const imgUrl = steppedImageUrl(batchInfo.collage);
        const totalBatches = state.batches ? state.batches.length : 1;
        
        container.innerHTML = `
            <div class="review-panel-glass batch-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">批次审核 · 多宫格与单帧检视</h3>
                    <div class="batch-counter-badge">批次 ${currentBatch + 1}/${totalBatches}</div>
                </div>
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击放大多宫格拼图大图">
                    <img src="${imgUrl}" alt="Batch Collage" />
                </div>
                ${renderSteppedBatchFrameThumbsHtml(state, batchInfo)}
                <div class="review-actions">
                    <button class="review-btn review-btn-approve" onclick="onSteppedApprove()">
                        ✅ Approve (通过)
                    </button>
                    <button class="review-btn review-btn-retry" onclick="onSteppedRetry()">
                        🔄 Retry (重绘)
                    </button>
                    <button class="review-btn review-btn-skip" onclick="onSteppedSkip()">
                        ⏭ Skip (跳过)
                    </button>
                </div>
            </div>
        `;
    } else if (stage === 'final_review') {
        const imgUrl = steppedImageUrl(state.final_collage);
        container.innerHTML = `
            <div class="review-panel-glass final-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">最终审查 · 全局连贯性与单帧快检</h3>
                </div>
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击放大完整 5 列多宫格大图">
                    <img src="${imgUrl}" alt="Final Collage" />
                </div>
                ${renderSteppedFinalAllFramesThumbsHtml(state)}
                <div class="review-actions">
                    <button class="review-btn review-btn-approve" onclick="onSteppedApprove()">
                        ✅ Approve (生成视频)
                    </button>
                    <button class="review-btn review-btn-retry" onclick="onSteppedRetry()">
                        🔄 Retry (重回批次)
                    </button>
                </div>
            </div>
        `;
    } else if (stage === 'completed') {
        container.innerHTML = `
            <div class="review-panel-glass stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title" style="color: var(--color-success)">🎉 流程已完成</h3>
                </div>
                <p style="color: var(--text-secondary)">所有的帧和视频均已生成并审核完毕。</p>
            </div>
        `;
    } else {
        renderSteppedLoading(container, STAGE_LABELS[stage] + ' 中...');
    }
}

function renderSteppedLoading(container, text) {
    if (!container) return;
    container.innerHTML = `
        <div class="review-panel-glass stepped-loading stepped-review-container">
            <div>${text}</div>
            <div class="stepped-loading-bar">
                <div class="stepped-loading-fill"></div>
            </div>
        </div>
    `;
}

function renderSteppedBatchFrameThumbsHtml(state, batchInfo) {
    const sequences = batchInfo && Array.isArray(batchInfo.sequences) ? batchInfo.sequences : [];
    if (!sequences.length) return '';
    const title = state.title || '';
    const safeTitle = typeof escapeHtml === 'function' ? escapeHtml(title) : title;
    
    return `
        <div class="stepped-batch-frame-section">
            <div class="stepped-batch-frame-title">📸 批次单帧明细 (共 ${sequences.length} 帧 · 点击任一帧可单独放大)：</div>
            <div class="stepped-batch-frame-strip">
                ${sequences.map((seq, idx) => {
                    const frameUrl = steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`);
                    return `
                        <div class="stepped-frame-thumb-card" onclick="openSteppedSequencesLightbox('${safeTitle}', ${JSON.stringify(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 帧">
                            <div class="stepped-frame-thumb-box">
                                <img src="${frameUrl}" alt="Frame ${seq}" onerror="this.src='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMyMjIiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZm9udC1mYW1pbHk9InNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM4ODgiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JTUcg${seq}</dGV4dD48L3N2Zz4='" />
                                <span class="stepped-frame-zoom-tag">🔍</span>
                            </div>
                            <span class="stepped-frame-seq-name">IMG ${String(seq).padStart(3, '0')}</span>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function renderSteppedFinalAllFramesThumbsHtml(state) {
    const batches = state.batches || [];
    let sequences = [];
    batches.forEach(b => {
        if (Array.isArray(b.sequences)) sequences.push(...b.sequences);
    });
    // 去重并升序排序
    sequences = Array.from(new Set(sequences)).sort((a, b) => a - b);
    if (!sequences.length) return '';
    const title = state.title || '';
    const safeTitle = typeof escapeHtml === 'function' ? escapeHtml(title) : title;
    
    return `
        <div class="stepped-batch-frame-section">
            <div class="stepped-batch-frame-title">📸 全套关键帧单帧列表 (共 ${sequences.length} 帧 · 点击任一帧可单独放大)：</div>
            <div class="stepped-batch-frame-strip">
                ${sequences.map((seq, idx) => {
                    const frameUrl = steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`);
                    return `
                        <div class="stepped-frame-thumb-card" onclick="openSteppedSequencesLightbox('${safeTitle}', ${JSON.stringify(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 帧">
                            <div class="stepped-frame-thumb-box">
                                <img src="${frameUrl}" alt="Frame ${seq}" onerror="this.src='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMyMjIiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZm9udC1mYW1pbHk9InNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM4ODgiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JTUcg${seq}</dGV4dD48L3N2Zz4='" />
                                <span class="stepped-frame-zoom-tag">🔍</span>
                            </div>
                            <span class="stepped-frame-seq-name">IMG ${String(seq).padStart(3, '0')}</span>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function openSteppedSequencesLightbox(title, sequences, activeIdx = 0) {
    if (!sequences || !sequences.length) return;
    const items = sequences.map((seq, i) => ({
        type: 'image',
        url: steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`),
        caption: `<strong>第 ${seq} 拍关键帧 (IMG ${String(seq).padStart(3, '0')})</strong> [${i + 1}/${sequences.length}] · ${title}`
    }));
    if (typeof openLightbox === 'function') {
        openLightbox(items, activeIdx);
    }
}

/* --- Button Click Handlers --- */
function onSteppedApprove() {
    if (!steppedState || !steppedState.title) return;
    advanceSteppedPipeline(steppedState.title, 'approve');
}

function onSteppedRetry() {
    if (!steppedState || !steppedState.title) return;
    advanceSteppedPipeline(steppedState.title, 'retry');
}

function onSteppedSkip() {
    if (!steppedState || !steppedState.title) return;
    advanceSteppedPipeline(steppedState.title, 'skip');
}

/* --- Main Entry Points --- */
function initSteppedPipeline(containerEl, dimensions) {
    if (!containerEl) return;
    
    // Ensure the container is visible
    containerEl.style.display = 'block';
    
    // Setup base HTML structure
    containerEl.innerHTML = `
        <div id="stepped-progress-container"></div>
        <div id="stepped-panel-content"></div>
        <div style="margin-top: 16px; text-align: center;">
            <button class="icon-btn" onclick="destroySteppedPipeline()" style="display: inline-flex; justify-content: center;">
                ❌ 取消 / 退出
            </button>
        </div>
    `;
    
    // Hide default view if needed, assuming the parent orchestrator handles this or we add a class
    containerEl.classList.add('active');

    // Auto-start the pipeline with provided dimensions
    if (dimensions) {
        renderSteppedLoading(
            document.getElementById('stepped-panel-content'),
            '正在启动分步管线...'
        );
        startSteppedPipeline(dimensions);
    }
}

function destroySteppedPipeline() {
    if (steppedSSE) {
        steppedSSE.close();
        steppedSSE = null;
    }
    
    if (steppedTaskId) {
        // Optionally notify server to cancel
        const accessCode = typeof ACCESS_CODE !== 'undefined' ? ACCESS_CODE : '';
        fetch(`/api/compose-cancel?task_id=${encodeURIComponent(steppedTaskId)}`, {
            method: 'POST',
            headers: {
                'X-Access-Code': accessCode
            }
        }).catch(err => console.error('Error cancelling task', err));
    }
    
    steppedState = null;
    steppedTaskId = null;
    
    const container = document.getElementById('stepped-pipeline-panel');
    if (container) {
        container.style.display = 'none';
        container.classList.remove('active');
        container.innerHTML = '';
    }
}

let lastNotifiedStage = null;

function updateSteppedUI(state) {
    if (!state) return;
    
    const progressContainer = document.getElementById('stepped-progress-container');
    const panelContent = document.getElementById('stepped-panel-content');
    
    if (progressContainer) {
        renderSteppedProgress(progressContainer, state);
    }
    
    if (panelContent) {
        renderSteppedReviewPanel(panelContent, state);
    }

    // 强提醒：当阶段发生变更时按阶段触发多模态通知
    if (state.stage && state.stage !== lastNotifiedStage) {
        lastNotifiedStage = state.stage;
        if (typeof NotificationCenter !== 'undefined') {
            if (state.stage === 'review_anchor') {
                NotificationCenter.notify({
                    type: 'action_required',
                    title: '分步审核：锚点帧已就绪',
                    message: '基准锚点帧 Frame 1 已渲染完成，请在工作台确认是否通过'
                });
            } else if (state.stage === 'review_batch') {
                NotificationCenter.notify({
                    type: 'action_required',
                    title: '分步审核：批次关键帧已就绪',
                    message: `第 ${(state.current_batch_index != null ? state.current_batch_index + 1 : '')} 批关键帧已渲染完成，请审核多宫格拼图`
                });
            } else if (state.stage === 'final_review') {
                NotificationCenter.notify({
                    type: 'action_required',
                    title: '分步审核：进入最终审查',
                    message: '全套关键帧已生成完毕，请进行最终连贯性审查'
                });
            } else if (state.stage === 'completed') {
                NotificationCenter.notify({
                    type: 'success',
                    title: '分步管线全流程完成',
                    message: '所有的关键帧与视频序列已全部生成完毕！'
                });
            } else if (state.stage === 'failed') {
                NotificationCenter.notify({
                    type: 'error',
                    title: '分步管线生成失败',
                    message: state.error || '分步渲染过程中出现异常中断'
                });
            }
        }
    }
}
