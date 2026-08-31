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
        const refPath = state.ref_frames && (state.ref_frames[1] || state.ref_frames['1']);
        const refUrl = refPath ? steppedImageUrl(refPath) : null;
        
        let previewHtml = '';
        if (refUrl) {
            previewHtml = `
                <div class="stepped-dual-preview-grid">
                    <div class="stepped-dual-preview-col">
                        <span class="stepped-dual-preview-badge gen-badge">🌟 生成锚点帧 (IMG 001)</span>
                        <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击单独放大查看生成锚点帧">
                            <img src="${imgUrl}" alt="Generated Anchor Frame" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                            <span class="stepped-thumb-zoom-badge">🔍 放大生成帧</span>
                        </div>
                    </div>
                    <div class="stepped-dual-preview-col">
                        <span class="stepped-dual-preview-badge ref-badge">🎯 原片基准抽帧 (REF 001)</span>
                        <div class="review-image-wrapper ref-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${refUrl}') : null" title="点击单独放大查看原片基准抽帧">
                            <img src="${refUrl}" alt="Benchmark Ref Frame" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                            <span class="stepped-thumb-zoom-badge" style="color: #f59e0b; border-color: rgba(245, 158, 11, 0.4);">🎯 放大原片帧</span>
                        </div>
                    </div>
                </div>
                <div class="stepped-toolbar-actions">
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedCollageViewerFromState(1, 'compare')" title="打开左右分屏对比滑块">⇄ 原片基准分屏滑块对标</button>
                </div>
            `;
        } else {
            previewHtml = `
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击单独放大查看锚点帧">
                    <img src="${imgUrl}" alt="Anchor Frame" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                    <span class="stepped-thumb-zoom-badge">🔍 放大锚点帧</span>
                </div>
            `;
        }

        container.innerHTML = `
            <div class="review-panel-glass anchor-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">审核锚点帧 (Frame 1)${refUrl ? ' · 原片基准对标' : ''}</h3>
                </div>
                ${previewHtml}
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
        const hasRefs = !!(state.ref_frames && Object.keys(state.ref_frames).length);
        const hasSrcCollage = !!state.source_collage;
        const firstSeqInBatch = (batchInfo.sequences && batchInfo.sequences[0]) || 2;
        
        container.innerHTML = `
            <div class="review-panel-glass batch-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">批次审核 · 多宫格与单帧检视${hasRefs ? ' (含原片对标)' : ''}</h3>
                    <div class="batch-counter-badge">批次 ${currentBatch + 1}/${totalBatches}</div>
                </div>
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击放大多宫格拼图大图">
                    <img src="${imgUrl}" alt="Batch Collage" />
                    <span class="stepped-thumb-zoom-badge">🔍 放大批次拼图</span>
                </div>
                <div class="stepped-toolbar-actions">
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedCollageViewerFromState(${firstSeqInBatch}, 'compare')" title="打开左右分屏对比滑块">⇄ 交互式滑块对比${hasRefs ? ' (支持原片对标)' : ''}</button>
                    ${hasSrcCollage ? `<button type="button" class="stepped-mini-tool-btn" onclick="openSteppedCollageViewerFromState(null, 'collage')" title="打开多宫格拼图检查器">⊞ 原片/生成 5 列拼图对标</button>` : ''}
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedTriptychModal('${state.title}', ${firstSeqInBatch}, ${state.total_beats || 12})" title="打开三联屏审查门禁">📐 三联屏连续性审查 (K-1 ⇄ K ⇄ K+1)</button>
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
        const hasRefs = !!(state.ref_frames && Object.keys(state.ref_frames).length);
        const hasSrcCollage = !!state.source_collage;
        
        container.innerHTML = `
            <div class="review-panel-glass final-review-panel stepped-review-container">
                <div class="review-header">
                    <h3 class="review-title">最终审查 · 全局连贯性与单帧快检${hasRefs ? ' (全量原片对标)' : ''}</h3>
                </div>
                <div class="review-image-wrapper" onclick="typeof openLightbox === 'function' ? openLightbox('${imgUrl}') : null" title="点击放大完整 5 列多宫格大图">
                    <img src="${imgUrl}" alt="Final Collage" />
                    <span class="stepped-thumb-zoom-badge">🔍 放大 5 列大图</span>
                </div>
                <div class="stepped-toolbar-actions">
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedCollageViewerFromState(1, 'collage')" title="打开交互式 5 列拼图检查器">🖼️ 交互式多宫格检查器</button>
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedCollageViewerFromState(1, 'compare')" title="打开分屏对比滑块">⇄ 逐拍分屏滑块对标${hasRefs ? ' (原片/生成)' : ''}</button>
                    <button type="button" class="stepped-mini-tool-btn" onclick="openSteppedTriptychModal('${state.title}', 2, ${state.total_beats || 12})" title="打开三联屏审查门禁">📐 三联屏连续性审查</button>
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

// 缩略图加载失败时的占位图
const STEPPED_FRAME_PLACEHOLDER = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMyMjIiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZm9udC1mYW1pbHk9InNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM4ODgiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JTUc8L3RleHQ+PC9zdmc+';

// 把一个值序列化成能安全塞进 HTML 内联事件属性的 JS 字面量
function steppedAttrArg(value) {
    const json = JSON.stringify(value === undefined ? null : value);
    return typeof escapeHtml === 'function' ? escapeHtml(json) : json;
}

function renderSteppedBatchFrameThumbsHtml(state, batchInfo) {
    const sequences = batchInfo && Array.isArray(batchInfo.sequences) ? batchInfo.sequences : [];
    if (!sequences.length) return '';
    const title = state.title || '';
    const refFrames = state.ref_frames || {};
    const hasAnyRef = sequences.some(seq => refFrames[seq] || refFrames[String(seq)]);
    
    return `
        <div class="stepped-batch-frame-section">
            <div class="stepped-batch-frame-title">
                <span>📸 批次单帧明细 (共 ${sequences.length} 帧${hasAnyRef ? ' · 🌟生成帧 / 🎯原片抽帧配对' : ''})：</span>
            </div>
            <div class="stepped-batch-frame-strip">
                ${sequences.map((seq, idx) => {
                    const frameUrl = steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`);
                    const refPath = refFrames[seq] || refFrames[String(seq)];
                    const refUrl = refPath ? steppedImageUrl(refPath) : null;
                    
                    if (refUrl) {
                        return `
                            <div class="stepped-paired-thumb-card" onclick="openSteppedSequencesLightbox(${steppedAttrArg(title)}, ${steppedAttrArg(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 拍 (含原片对照)">
                                <div class="stepped-paired-thumb-split">
                                    <div class="stepped-paired-box gen-box">
                                        <img src="${frameUrl}" alt="IMG ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                        <span class="stepped-paired-tag tag-gen">IMG ${seq}</span>
                                    </div>
                                    <div class="stepped-paired-box ref-box">
                                        <img src="${refUrl}" alt="REF ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                        <span class="stepped-paired-tag tag-ref">REF ${seq}</span>
                                    </div>
                                </div>
                                <div class="stepped-paired-meta">
                                    <span class="seq-title">第 ${seq} 拍</span>
                                    <span class="diff-hint" onclick="event.stopPropagation(); openSteppedTriptychModal('${title}', ${seq}, ${state.total_beats || 12})" title="打开第 ${seq} 拍三联屏动态审查">📐 三联屏</span>
                                </div>
                            </div>
                        `;
                    }
                    
                    return `
                        <div class="stepped-frame-thumb-card" onclick="openSteppedSequencesLightbox(${steppedAttrArg(title)}, ${steppedAttrArg(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 帧">
                            <div class="stepped-frame-thumb-box">
                                <img src="${frameUrl}" alt="Frame ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                <span class="stepped-frame-zoom-tag">🔍</span>
                            </div>
                            <span class="stepped-frame-seq-name" onclick="event.stopPropagation(); openSteppedTriptychModal('${title}', ${seq}, ${state.total_beats || 12})" title="点击打开三联屏审查">IMG ${String(seq).padStart(3, '0')} 📐</span>
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
    sequences = Array.from(new Set(sequences)).sort((a, b) => a - b);
    if (!sequences.length) return '';
    const title = state.title || '';
    const refFrames = state.ref_frames || {};
    const hasAnyRef = sequences.some(seq => refFrames[seq] || refFrames[String(seq)]);
    
    return `
        <div class="stepped-batch-frame-section">
            <div class="stepped-batch-frame-title">
                <span>📸 全套关键帧单帧列表 (共 ${sequences.length} 帧${hasAnyRef ? ' · 🌟生成帧 / 🎯原片抽帧配对' : ''})：</span>
            </div>
            <div class="stepped-batch-frame-strip">
                ${sequences.map((seq, idx) => {
                    const frameUrl = steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`);
                    const refPath = refFrames[seq] || refFrames[String(seq)];
                    const refUrl = refPath ? steppedImageUrl(refPath) : null;
                    
                    if (refUrl) {
                        return `
                            <div class="stepped-paired-thumb-card" onclick="openSteppedSequencesLightbox(${steppedAttrArg(title)}, ${steppedAttrArg(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 拍 (含原片对照)">
                                <div class="stepped-paired-thumb-split">
                                    <div class="stepped-paired-box gen-box">
                                        <img src="${frameUrl}" alt="IMG ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                        <span class="stepped-paired-tag tag-gen">IMG ${seq}</span>
                                    </div>
                                    <div class="stepped-paired-box ref-box">
                                        <img src="${refUrl}" alt="REF ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                        <span class="stepped-paired-tag tag-ref">REF ${seq}</span>
                                    </div>
                                </div>
                                <div class="stepped-paired-meta">
                                    <span class="seq-title">第 ${seq} 拍</span>
                                    <span class="diff-hint" onclick="event.stopPropagation(); openSteppedTriptychModal('${title}', ${seq}, ${state.total_beats || 12})" title="打开第 ${seq} 拍三联屏动态审查">📐 三联屏</span>
                                </div>
                            </div>
                        `;
                    }
                    
                    return `
                        <div class="stepped-frame-thumb-card" onclick="openSteppedSequencesLightbox(${steppedAttrArg(title)}, ${steppedAttrArg(sequences)}, ${idx})" title="点击单独放大查看第 ${seq} 帧">
                            <div class="stepped-frame-thumb-box">
                                <img src="${frameUrl}" alt="Frame ${seq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                                <span class="stepped-frame-zoom-tag">🔍</span>
                            </div>
                            <span class="stepped-frame-seq-name" onclick="event.stopPropagation(); openSteppedTriptychModal('${title}', ${seq}, ${state.total_beats || 12})" title="点击打开三联屏审查">IMG ${String(seq).padStart(3, '0')} 📐</span>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function openSteppedSequencesLightbox(title, sequences, activeIdx = 0) {
    if (!sequences || !sequences.length) return;
    const refFrames = (steppedState && steppedState.ref_frames) || {};
    const refRoles = (steppedState && steppedState.ref_frame_roles) || {};
    const items = [];
    let initialItemIdx = 0;
    
    sequences.forEach((seq, i) => {
        const isCurrentTarget = (i === activeIdx);
        if (isCurrentTarget) {
            initialItemIdx = items.length;
        }
        
        // 1. 生成图
        items.push({
            type: 'image',
            url: steppedImageUrl(`/outputs/${title}/frames/img_${String(seq).padStart(3, '0')}.webp`),
            caption: `<strong>第 ${seq} 拍关键帧 (IMG ${String(seq).padStart(3, '0')})</strong> [${i + 1}/${sequences.length}] · ${title}`
        });
        
        // 2. 原片基准抽帧（若有）
        const refPath = refFrames[seq] || refFrames[String(seq)];
        if (refPath) {
            items.push({
                type: 'image',
                url: steppedImageUrl(refPath),
                caption: `<strong>第 ${seq} 拍原片基准抽帧 (REF ${String(seq).padStart(3, '0')})</strong> ${refFrameRoleLabel(refRoles, seq)} · ${title}`
            });
        }
    });
    
    if (typeof openLightbox === 'function') {
        openLightbox(items, initialItemIdx);
    }
}

function openSteppedCollageViewerFromState(initialSeq = 1, initialMode = 'collage') {
    if (!steppedState) return;
    const state = steppedState;
    const currentBatch = state.current_batch_index || 0;
    const batchInfo = (state.batches && state.batches[currentBatch]) || {};
    const collageUrl = state.final_collage || batchInfo.collage || '';
    
    const idea = {
        id: state.pipeline_id || 'stepped',
        title: state.title,
        collage_url: collageUrl,
        ref_frames: state.ref_frames || {},
        source_collage: state.source_collage || '',
        image_count: state.total_beats || 0,
    };
    
    if (typeof openCollageViewer === 'function') {
        openCollageViewer({
            idea,
            collageUrl: steppedImageUrl(collageUrl),
            sourceCollageUrl: steppedImageUrl(state.source_collage || ''),
            refFrames: state.ref_frames || {},
            initialFrameSeq: initialSeq || 1,
            initialMode: initialMode || 'collage',
            compareType: (initialMode === 'compare' && state.ref_frames && Object.keys(state.ref_frames).length) ? 'benchmark' : 'adjacent',
        });
    }
}

function openSteppedTriptychModal(title, currentSeq, totalBeats = null) {
    if (!title || !currentSeq) return;
    const existing = document.getElementById('stepped-triptych-modal');
    if (existing) existing.remove();

    const maxBeats = totalBeats || (steppedState && steppedState.total_beats) || 12;
    const prevSeq = currentSeq > 1 ? currentSeq - 1 : null;
    const nextSeq = currentSeq < maxBeats ? currentSeq + 1 : null;

    const prevUrl = prevSeq ? steppedImageUrl(`/outputs/${title}/frames/img_${String(prevSeq).padStart(3, '0')}.webp`) : null;
    const curUrl = steppedImageUrl(`/outputs/${title}/frames/img_${String(currentSeq).padStart(3, '0')}.webp`);
    const nextUrl = nextSeq ? steppedImageUrl(`/outputs/${title}/frames/img_${String(nextSeq).padStart(3, '0')}.webp`) : null;

    const modal = document.createElement('div');
    modal.className = 'stepped-triptych-modal active';
    modal.id = 'stepped-triptych-modal';

    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));

    modal.innerHTML = `
        <div class="stepped-triptych-content glass-panel">
            <div class="triptych-header">
                <div class="triptych-title-group">
                    <span style="font-size: 20px;">📐</span>
                    <div>
                        <h3>三联屏审查门禁 (3-Frame Triptych Gate) · 第 ${currentSeq} 拍</h3>
                        <span style="font-size: 12px; color: var(--text-muted);">双向夹具约束比对：[Frame ${prevSeq || '起帧'} ⚓] ← [Frame ${currentSeq} 🎯] → [Frame ${nextSeq || '收尾'} 🔒]</span>
                    </div>
                </div>
                <button type="button" class="close-btn triptych-close-btn">&times;</button>
            </div>
            
            <div class="triptych-body">
                <div class="triptych-grid">
                    <!-- Col 1: K-1 前向基底 -->
                    <div class="triptych-col-card">
                        <div class="triptych-col-header">
                            <span class="triptych-badge badge-prev">${prevSeq ? `IMG ${String(prevSeq).padStart(3, '0')}` : '初始锚点'}</span>
                            <span style="font-size: 12px; color: var(--text-muted);">前向物理基底 (K-1)</span>
                        </div>
                        <div class="triptych-img-wrap" onclick="typeof openLightbox === 'function' && '${prevUrl}' ? openLightbox('${prevUrl}') : null">
                            ${prevUrl ? `<img src="${prevUrl}" alt="Frame ${prevSeq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />` : '<div class="triptych-empty-slot"><span>⚓ 序列首帧</span><span>（以原景地貌/锚点为基底）</span></div>'}
                        </div>
                        <div class="triptych-col-meta">
                            <b>前向锚定约束：</b>第 ${currentSeq} 帧必须 100% 物理继承本帧的硬装结构、地面平整度与机位透视。
                        </div>
                    </div>

                    <!-- Col 2: K 当前审查帧 -->
                    <div class="triptych-col-card is-current">
                        <div class="triptych-col-header">
                            <span class="triptych-badge badge-current">IMG ${String(currentSeq).padStart(3, '0')}</span>
                            <span style="font-size: 12px; font-weight: 700; color: #60a5fa;">当前审查/交付物 (K)</span>
                        </div>
                        <div class="triptych-img-wrap" onclick="typeof openLightbox === 'function' ? openLightbox('${curUrl}') : null">
                            <img src="${curUrl}" alt="Frame ${currentSeq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />
                        </div>
                        <div class="triptych-col-meta">
                            <b>当拍交付成果：</b>100% 全量体现当拍施工产物，零凭空变化，施工废料与物料守恒。
                        </div>
                    </div>

                    <!-- Col 3: K+1 后向承接 -->
                    <div class="triptych-col-card">
                        <div class="triptych-col-header">
                            <span class="triptych-badge badge-succ">${nextSeq ? `IMG ${String(nextSeq).padStart(3, '0')}` : '最终揭示'}</span>
                            <span style="font-size: 12px; color: var(--text-muted);">后向承接通道 (K+1)</span>
                        </div>
                        <div class="triptych-img-wrap" onclick="typeof openLightbox === 'function' && '${nextUrl}' ? openLightbox('${nextUrl}') : null">
                            ${nextUrl ? `<img src="${nextUrl}" alt="Frame ${nextSeq}" onerror="this.src='${STEPPED_FRAME_PLACEHOLDER}'" />` : '<div class="triptych-empty-slot"><span>🏁 最终揭示帧</span><span>（通往收尾与全貌定格）</span></div>'}
                        </div>
                        <div class="triptych-col-meta">
                            <b>后向通道锁：</b>当前帧交付物必须是通往下一阶段的自然且唯一前置条件，无冲突多余道具。
                        </div>
                    </div>
                </div>

                <!-- 审查三大检查点 -->
                <div class="triptych-checklist-panel">
                    <div class="triptych-checklist-title">📋 连续性三联屏快检清单 (Continuity Gate Checklist)：</div>
                    <div class="triptych-check-items">
                        <div class="triptych-check-item">
                            <span>🔍</span>
                            <div><b>1. 空间与透视连续性</b>：后墙水渍线、梁架走向、窗洞位置及地平线基准严格连续，无 45° 菱形旋转或管道拉伸。</div>
                        </div>
                        <div class="triptych-check-item">
                            <span>🪵</span>
                            <div><b>2. 材质单调与零回退</b>：已铺地板保持温润哑光实木质感（无湿水镜面化），已修破损零回退。</div>
                        </div>
                        <div class="triptych-check-item">
                            <span>⚖️</span>
                            <div><b>3. 全域差量与物料守恒</b>：顶/中/底/边际物理差量 100% 具备工具与动作因果链，物料与废料守恒。</div>
                        </div>
                    </div>
                </div>

                <!-- 三级差量修复通道 -->
                <div class="triptych-remediation-bar">
                    <div style="font-size: 12px; color: var(--text-muted);">
                        <b>🛠️ 差量分级修复方案：</b>发现瑕疵时，请按严重程度选择修复通道（严禁脱离上下文单帧盲抽）：
                    </div>
                    <div class="remediation-level-btns">
                        <button type="button" class="remediation-btn" onclick="alert('【Level 1 局部蒙版修复建议】\\n建议在生图画布中针对瑕疵区域（如人偶手势/多余小工具）绘制 Mask 局部重绘，锁定背景 3D 空间像素 100% 不动。')" title="适用于局部小瑕疵、人物微调">
                            🖌️ Level 1 局部蒙版重绘
                        </button>
                        <button type="button" class="remediation-btn" onclick="alert('【Level 2 图生图定向重渲建议】\\n必须以 Frame ${prevSeq || 1} 为图生图 (I2I) 底图并施加深度图/线稿控制，严禁使用文生图 (T2I) 盲抽。')" title="适用于光照/视角微调">
                            🧬 Level 2 I2I定向重绘
                        </button>
                        <button type="button" class="remediation-btn" onclick="alert('【Level 3 连锁重构预警】\\n若第 ${currentSeq} 帧必须做结构级重构，将触发连带连锁预警，自动从本帧起向后链式同步修正下游提示词。')" title="适用于结构级变更">
                            ⚡ Level 3 三明治连锁修复
                        </button>
                    </div>
                </div>
            </div>

            <div class="triptych-footer">
                <button type="button" class="action-btn text-btn secondary triptych-close-action">关闭</button>
                <button type="button" class="action-btn text-btn" onclick="document.getElementById('stepped-triptych-modal').remove(); typeof switchTab === 'function' ? switchTab('editor') : null;">✏️ 前往提示词编辑器微调</button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    const close = () => {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 200);
    };

    modal.querySelector('.triptych-close-btn').addEventListener('click', close);
    modal.querySelector('.triptych-close-action').addEventListener('click', close);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });
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
