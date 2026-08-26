/* =====================================================================
   交互式多宫格检查器（Interactive 5-Column Collage Viewer）
   ---------------------------------------------------------------------
   提供全局视觉连续性快检能力：
   1. 5 列全景拼图画布 + 拖拽平移 + 无级滚轮缩放
   2. 局部 2.5x 悬浮放大镜（Loupe Magnifier）带十字准星
   3. 色彩/透视对齐基准线（可拖动地平基准线、三分构图网格、中心透视线）
   4. 邻帧交互式左右对比滑块（Adjacent Frame Split Comparison Slider）
   5. 单色明暗与高反差轮廓辅助滤镜
   ===================================================================== */

let collageViewerState = {
    open: false,
    mode: 'collage', // 'collage' | 'compare'
    collageUrl: '',
    frames: [],
    idea: null,
    zoom: 1.0,
    panX: 0,
    panY: 0,
    isDragging: false,
    dragStartX: 0,
    dragStartY: 0,
    magnifierActive: false,
    guidelines: {
        horizon: true,
        horizonY: 50, // 百分比
        thirds: false,
        crosshair: false,
    },
    filter: 'normal', // 'normal' | 'mono' | 'contrast'
    comparePair: [1, 2], // 默认对比第 1 帧与第 2 帧
    splitRatio: 50, // 对比滑块百分比 0 - 100
};

/**
 * 打开交互式多宫格检查器弹窗
 * @param {Object} opts
 * @param {string} [opts.collageUrl] 拼图 URL
 * @param {Object} [opts.idea] 创意对象（含 frameRun.frames）
 * @param {number} [opts.initialFrameSeq] 初始定位帧号
 * @param {string} [opts.initialMode] 初始模式 ('collage' | 'compare')
 */
function openCollageViewer(opts = {}) {
    const idea = opts.idea || (typeof currentIdea !== 'undefined' ? currentIdea : null);
    let collageUrl = opts.collageUrl || (idea && idea.collage_url) || (idea && idea.frameRun && idea.frameRun.collage_url) || '';
    
    // 提取有效帧列表
    let frames = [];
    if (idea && idea.frameRun && Array.isArray(idea.frameRun.frames) && idea.frameRun.frames.length) {
        frames = idea.frameRun.frames.filter(f => f.url || f.file);
    } else if (idea && Array.isArray(idea.frames) && idea.frames.length) {
        frames = idea.frames.filter(f => f.url || f.file);
    } else if (idea && Array.isArray(idea.covers) && idea.covers.length) {
        frames = idea.covers.map((c, i) => ({ sequence: i + 1, url: typeof c === 'string' ? c : (c.url || c.file) }));
    }

    // 若 frames 仍为空但有创意标题和图片数，构建默认帧路径
    if (!frames.length && idea && idea.title && (idea.image_count || (idea.frameRun && idea.frameRun.image_count))) {
        const count = idea.image_count || idea.frameRun.image_count || 0;
        for (let i = 1; i <= count; i++) {
            frames.push({
                sequence: i,
                url: `/outputs/${idea.title}/frames/img_${String(i).padStart(3, '0')}.webp`
            });
        }
    }

    if (!collageUrl && !frames.length) {
        if (typeof showToast === 'function') {
            showToast('当前尚未生成拼图或关键帧图片', 'warning');
        }
        return;
    }

    collageViewerState = {
        open: true,
        mode: opts.initialMode || 'collage',
        collageUrl,
        frames,
        idea,
        zoom: 1.0,
        panX: 0,
        panY: 0,
        isDragging: false,
        dragStartX: 0,
        dragStartY: 0,
        magnifierActive: false,
        guidelines: {
            horizon: true,
            horizonY: 50,
            thirds: false,
            crosshair: false,
        },
        filter: 'normal',
        comparePair: [
            opts.initialFrameSeq ? Math.max(1, opts.initialFrameSeq - 1) : 1,
            opts.initialFrameSeq || 2
        ],
        splitRatio: 50,
    };

    renderCollageViewerModal();
}

function closeCollageViewer() {
    collageViewerState.open = false;
    const modal = document.getElementById('collage-viewer-modal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 200);
    }
}

function openCollageFrameLightbox(index = 0) {
    const st = collageViewerState;
    if (!st.frames || !st.frames.length) {
        if (st.collageUrl && typeof openLightbox === 'function') {
            openLightbox([{
                type: 'image',
                url: st.collageUrl,
                caption: `<strong>5 列全景多宫格拼图</strong> · ${st.idea && st.idea.title ? st.idea.title : ''}`
            }], 0);
        }
        return;
    }
    const safeIdx = Math.max(0, Math.min(index, st.frames.length - 1));
    const items = st.frames.map((f, i) => {
        const seq = f.sequence || (i + 1);
        const titleStr = (st.idea && st.idea.title) ? ` · ${st.idea.title}` : '';
        return {
            type: 'image',
            url: f.url || f.file,
            caption: `<strong>第 ${seq} 拍关键帧 (IMG ${String(seq).padStart(3, '0')})</strong> [${i + 1}/${st.frames.length}]${titleStr}`
        };
    });
    if (typeof openLightbox === 'function') {
        openLightbox(items, safeIdx);
    }
}

function renderCollageViewerModal() {
    let modal = document.getElementById('collage-viewer-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.className = 'modal active collage-viewer-modal';
        modal.id = 'collage-viewer-modal';
        modal.style.zIndex = '1200';
        document.body.appendChild(modal);
    }

    const st = collageViewerState;
    const totalFrames = st.frames.length;
    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));

    modal.innerHTML = `
        <div class="modal-content glass-panel cv-modal-content">
            <!-- 头部控制工具条 -->
            <div class="cv-header">
                <div class="cv-header-left">
                    <span class="cv-title-icon">🖼️</span>
                    <div class="cv-title-text">
                        <h3>交互式多宫格检查器 · 视觉连续性快检</h3>
                        <span class="cv-subtitle">5 列全局拼图 · 单帧点击放大 · 局部放大镜 · 邻帧对比 · 地平透视对齐</span>
                    </div>
                </div>

                <div class="cv-toolbar">
                    <!-- 模式切换 -->
                    <div class="cv-toolgroup cv-mode-group">
                        <button type="button" class="cv-tool-btn ${st.mode === 'collage' ? 'is-active' : ''}" data-act="mode-collage" title="5列全景多宫格拼图总览 (支持点击任一帧单独放大)">⊞ 5列拼图</button>
                        <button type="button" class="cv-tool-btn ${st.mode === 'compare' ? 'is-active' : ''}" data-act="mode-compare" title="前后帧左右滑动对比">⇄ 邻帧对比</button>
                    </div>

                    <!-- 单独放大入口 -->
                    <div class="cv-toolgroup cv-collage-only" ${st.mode !== 'collage' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn" data-act="open-lightbox-current" title="以高清晰度灯箱单独放大浏览每一帧 (支持 ← / → 键盘连续翻页)">🔍 单帧放大</button>
                    </div>

                    <!-- 放大镜开关 (Collage模式) -->
                    <div class="cv-toolgroup cv-collage-only" ${st.mode !== 'collage' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn ${st.magnifierActive ? 'is-active' : ''}" data-act="toggle-magnifier" title="开启/关闭跟随鼠标的高精局部放大镜 (2.5x)">🔬 悬浮放大镜</button>
                    </div>

                    <!-- 辅助基准线 (Collage模式) -->
                    <div class="cv-toolgroup cv-collage-only" ${st.mode !== 'collage' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn ${st.guidelines.horizon ? 'is-active' : ''}" data-act="toggle-horizon" title="水平地平基准线（可上下拖动）">📏 地平线</button>
                        <button type="button" class="cv-tool-btn ${st.guidelines.thirds ? 'is-active' : ''}" data-act="toggle-thirds" title="三分黄金分割构图线">⌗ 三分线</button>
                        <button type="button" class="cv-tool-btn ${st.guidelines.crosshair ? 'is-active' : ''}" data-act="toggle-crosshair" title="中心透视十字线">✚ 透视十字</button>
                    </div>

                    <!-- 滤镜切换 -->
                    <div class="cv-toolgroup">
                        <button type="button" class="cv-tool-btn ${st.filter === 'normal' ? 'is-active' : ''}" data-act="filter-normal" title="正常色彩">🎨 原始</button>
                        <button type="button" class="cv-tool-btn ${st.filter === 'mono' ? 'is-active' : ''}" data-act="filter-mono" title="单色黑白（快速检查明暗跳变）">◐ 单色</button>
                        <button type="button" class="cv-tool-btn ${st.filter === 'contrast' ? 'is-active' : ''}" data-act="filter-contrast" title="高反差（快速检查几何边缘）">⚡ 反差</button>
                    </div>

                    <!-- 缩放控制 (Collage模式) -->
                    <div class="cv-toolgroup cv-collage-only" ${st.mode !== 'collage' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn" data-act="zoom-out" title="缩小">-</button>
                        <span class="cv-zoom-display">${Math.round(st.zoom * 100)}%</span>
                        <button type="button" class="cv-tool-btn" data-act="zoom-in" title="放大">+</button>
                        <button type="button" class="cv-tool-btn" data-act="zoom-fit" title="自适应窗口大小">适应</button>
                    </div>

                    <!-- 下载与关闭 -->
                    <div class="cv-toolgroup">
                        ${st.collageUrl ? `<a href="${escapeStr(st.collageUrl)}" download target="_blank" class="cv-tool-btn" title="下载原图" style="text-decoration:none;">⬇</a>` : ''}
                        <button type="button" class="close-btn cv-close-btn" title="关闭 (Esc)">&times;</button>
                    </div>
                </div>
            </div>

            <!-- 主画布区域 -->
            <div class="cv-body">
                ${st.mode === 'collage' ? renderCollageViewHtml() : renderCompareViewHtml()}
            </div>

            <!-- 底部状态条 -->
            <div class="cv-footer">
                <div class="cv-footer-hints">
                    ${st.mode === 'collage' 
                        ? '💡 提示：点击拼图中任意画面即可<b>单独全屏放大</b>；按住鼠标左键可平移画布，滚轮无级缩放；地平线支持拖拽。'
                        : '💡 提示：拖动中间分界线滑动对比前后帧差异；支持快捷键「← / →」快速切换上一对/下一对相邻帧。'
                    }
                </div>
                <div class="cv-footer-stats">
                    共 <b>${totalFrames}</b> 帧 · 5 列多宫格
                </div>
            </div>
        </div>
    `;

    bindCollageViewerEvents(modal);
}

function renderCollageHotspotsHtml(st) {
    const frames = st.frames;
    if (!frames || !frames.length) return '';
    const totalFrames = frames.length;
    const cols = 5;
    const rows = Math.ceil(totalFrames / cols);

    const cells = frames.map((f, idx) => `
        <div class="cv-collage-hotspot-cell" data-frame-idx="${idx}" title="点击单独放大查看第 ${f.sequence || (idx + 1)} 帧">
            <span class="cv-hotspot-tag">IMG ${String(f.sequence || (idx + 1)).padStart(3, '0')} 🔍 放大</span>
        </div>
    `).join('');

    return `
        <div class="cv-collage-hotspot-grid" id="cv-collage-hotspot-grid" style="grid-template-columns: repeat(${cols}, 1fr); grid-template-rows: repeat(${rows}, 1fr);">
            ${cells}
        </div>
    `;
}

function renderCollageViewHtml() {
    const st = collageViewerState;
    const filterCls = st.filter === 'mono' ? 'cv-filter-mono' : (st.filter === 'contrast' ? 'cv-filter-contrast' : '');

    return `
        <div class="cv-canvas-viewport" id="cv-canvas-viewport">
            <div class="cv-canvas-transform" id="cv-canvas-transform" style="transform: translate(${st.panX}px, ${st.panY}px) scale(${st.zoom});">
                <div class="cv-image-container ${filterCls}" id="cv-image-container">
                    ${st.collageUrl 
                        ? `<img src="${st.collageUrl}" id="cv-collage-img" class="cv-collage-img" alt="5-Column Keyframe Collage" draggable="false">
                           ${renderCollageHotspotsHtml(st)}` 
                        : renderDynamicCollageGridHtml()
                    }
                    
                    <!-- 辅助线图层 -->
                    <div class="cv-guidelines-layer" id="cv-guidelines-layer">
                        ${st.guidelines.horizon ? `<div class="cv-line-horizon" id="cv-line-horizon" style="top: ${st.guidelines.horizonY}%;" title="可上下拖拽地平基准线"><span class="cv-line-handle">地平线 ${Math.round(st.guidelines.horizonY)}%</span></div>` : ''}
                        ${st.guidelines.thirds ? `
                            <div class="cv-grid-thirds">
                                <div class="cv-thirds-v" style="left: 33.33%;"></div>
                                <div class="cv-thirds-v" style="left: 66.66%;"></div>
                                <div class="cv-thirds-h" style="top: 33.33%;"></div>
                                <div class="cv-thirds-h" style="top: 66.66%;"></div>
                            </div>` : ''
                        }
                        ${st.guidelines.crosshair ? `
                            <div class="cv-crosshair">
                                <div class="cv-cross-v"></div>
                                <div class="cv-cross-h"></div>
                            </div>` : ''
                        }
                    </div>
                </div>
            </div>

            <!-- 局部悬浮放大镜 -->
            <div class="cv-loupe-magnifier" id="cv-loupe-magnifier" style="display: none;">
                <div class="cv-loupe-canvas-wrap">
                    <img src="${st.collageUrl || ''}" id="cv-loupe-img" class="cv-loupe-img ${filterCls}">
                </div>
                <div class="cv-loupe-crosshair"></div>
                <div class="cv-loupe-info">2.5×</div>
            </div>
        </div>
    `;
}

function renderDynamicCollageGridHtml() {
    const st = collageViewerState;
    if (!st.frames.length) return '<div class="cv-empty-hint">暂无可用帧画面</div>';
    
    const cells = st.frames.map((f, idx) => `
        <div class="cv-dyn-cell" data-frame-idx="${idx}" title="点击单独放大查看第 ${f.sequence || (idx + 1)} 帧">
            <img src="${f.url || f.file}" alt="Frame ${f.sequence || (idx + 1)}" draggable="false">
            <span class="cv-dyn-cell-label">IMG ${String(f.sequence || (idx + 1)).padStart(3, '0')}</span>
            <span class="cv-dyn-cell-zoom-icon">🔍 放大</span>
        </div>
    `).join('');

    return `<div class="cv-dyn-grid" id="cv-dyn-grid" style="display: grid; grid-template-columns: repeat(5, 240px); gap: 6px; background: #000; padding: 6px; border-radius: 6px;">${cells}</div>`;
}

function renderCompareViewHtml() {
    const st = collageViewerState;
    const frames = st.frames;
    const [seqA, seqB] = st.comparePair;

    const frameA = frames.find(f => Number(f.sequence) === Number(seqA)) || frames[0] || {};
    const frameB = frames.find(f => Number(f.sequence) === Number(seqB)) || frames[1] || frames[0] || {};

    const urlA = frameA.url || frameA.file || '';
    const urlB = frameB.url || frameB.file || '';
    const filterCls = st.filter === 'mono' ? 'cv-filter-mono' : (st.filter === 'contrast' ? 'cv-filter-contrast' : '');

    // 构建帧选择器 options
    const optionsA = frames.map(f => `<option value="${f.sequence}" ${f.sequence === frameA.sequence ? 'selected' : ''}>IMG ${String(f.sequence).padStart(3, '0')}</option>`).join('');
    const optionsB = frames.map(f => `<option value="${f.sequence}" ${f.sequence === frameB.sequence ? 'selected' : ''}>IMG ${String(f.sequence).padStart(3, '0')}</option>`).join('');

    return `
        <div class="cv-compare-container">
            <!-- 对比选择栏 -->
            <div class="cv-compare-selector-bar">
                <button type="button" class="action-btn text-btn mini-btn" data-act="prev-pair" title="切换到上一对相邻帧 (←)">◀ 上一拍对比</button>
                <div class="cv-pair-picks">
                    <span class="cv-pair-label">基准帧 A (左):</span>
                    <select class="cv-frame-select" id="cv-select-frame-a">${optionsA}</select>
                    <span class="cv-pair-arrow">➔ 递进帧 B (右):</span>
                    <select class="cv-frame-select" id="cv-select-frame-b">${optionsB}</select>
                </div>
                <button type="button" class="action-btn text-btn mini-btn" data-act="next-pair" title="切换到下一对相邻帧 (→)">下一拍对比 ▶</button>
            </div>

            <!-- 分割滑块视口 -->
            <div class="cv-split-viewport ${filterCls}" id="cv-split-viewport">
                <!-- 下层图 (Frame B) -->
                <div class="cv-split-layer cv-layer-b">
                    <img src="${urlB}" class="cv-compare-img" draggable="false">
                    <span class="cv-split-tag tag-b">IMG ${String(frameB.sequence || seqB).padStart(3, '0')} (后)</span>
                </div>

                <!-- 上层图 (Frame A，按百分比裁切) -->
                <div class="cv-split-layer cv-layer-a" id="cv-layer-a" style="width: ${st.splitRatio}%;">
                    <img src="${urlA}" class="cv-compare-img" draggable="false">
                    <span class="cv-split-tag tag-a">IMG ${String(frameA.sequence || seqA).padStart(3, '0')} (前)</span>
                </div>

                <!-- 滑动分割线 -->
                <div class="cv-split-divider" id="cv-split-divider" style="left: ${st.splitRatio}%;">
                    <div class="cv-divider-handle">
                        <span>◀ ▶</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function bindCollageViewerEvents(modal) {
    const st = collageViewerState;

    // 1. 关闭按钮与 Esc
    modal.querySelector('.cv-close-btn').addEventListener('click', closeCollageViewer);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeCollageViewer();
    });
    const onKeyDown = (e) => {
        if (!collageViewerState.open) {
            window.removeEventListener('keydown', onKeyDown);
            return;
        }
        if (e.key === 'Escape') {
            closeCollageViewer();
        } else if (e.key === 'ArrowLeft' && collageViewerState.mode === 'compare') {
            navigateComparePair(-1);
        } else if (e.key === 'ArrowRight' && collageViewerState.mode === 'compare') {
            navigateComparePair(1);
        }
    };
    window.addEventListener('keydown', onKeyDown);

    // 2. 工具栏点击委托
    const toolbar = modal.querySelector('.cv-toolbar');
    toolbar.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        const act = btn.dataset.act;

        if (act === 'mode-collage') {
            st.mode = 'collage';
            renderCollageViewerModal();
        } else if (act === 'mode-compare') {
            st.mode = 'compare';
            renderCollageViewerModal();
        } else if (act === 'open-lightbox-current') {
            openCollageFrameLightbox(0);
        } else if (act === 'toggle-magnifier') {
            st.magnifierActive = !st.magnifierActive;
            renderCollageViewerModal();
        } else if (act === 'toggle-horizon') {
            st.guidelines.horizon = !st.guidelines.horizon;
            renderCollageViewerModal();
        } else if (act === 'toggle-thirds') {
            st.guidelines.thirds = !st.guidelines.thirds;
            renderCollageViewerModal();
        } else if (act === 'toggle-crosshair') {
            st.guidelines.crosshair = !st.guidelines.crosshair;
            renderCollageViewerModal();
        } else if (act === 'filter-normal') {
            st.filter = 'normal';
            renderCollageViewerModal();
        } else if (act === 'filter-mono') {
            st.filter = 'mono';
            renderCollageViewerModal();
        } else if (act === 'filter-contrast') {
            st.filter = 'contrast';
            renderCollageViewerModal();
        } else if (act === 'zoom-in') {
            applyZoom(st.zoom * 1.25);
        } else if (act === 'zoom-out') {
            applyZoom(st.zoom * 0.8);
        } else if (act === 'zoom-fit') {
            st.zoom = 1.0;
            st.panX = 0;
            st.panY = 0;
            updateTransform();
        }
    });

    if (st.mode === 'collage') {
        bindCollageCanvasEvents(modal);
    } else {
        bindCompareSliderEvents(modal);
    }
}

function applyZoom(newZoom) {
    const st = collageViewerState;
    st.zoom = Math.max(0.4, Math.min(5.0, newZoom));
    updateTransform();
    const display = document.querySelector('.cv-zoom-display');
    if (display) display.textContent = `${Math.round(st.zoom * 100)}%`;
}

function updateTransform() {
    const transformEl = document.getElementById('cv-canvas-transform');
    if (transformEl) {
        transformEl.style.transform = `translate(${collageViewerState.panX}px, ${collageViewerState.panY}px) scale(${collageViewerState.zoom})`;
    }
}

function bindCollageCanvasEvents(modal) {
    const st = collageViewerState;
    const viewport = modal.querySelector('#cv-canvas-viewport');
    const container = modal.querySelector('#cv-image-container');
    const magnifier = modal.querySelector('#cv-loupe-magnifier');
    const loupeImg = modal.querySelector('#cv-loupe-img');
    const horizonLine = modal.querySelector('#cv-line-horizon');

    if (!viewport) return;

    // 滚轮缩放
    viewport.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY < 0 ? 1.12 : 0.89;
        applyZoom(st.zoom * delta);
    }, { passive: false });

    // 拖拽画布平移与单帧点击放大判定
    let isPanning = false;
    let startX = 0, startY = 0;
    let downClientX = 0, downClientY = 0;

    viewport.addEventListener('mousedown', (e) => {
        if (e.target.closest('#cv-line-horizon')) return; // 避免冲突
        isPanning = true;
        downClientX = e.clientX;
        downClientY = e.clientY;
        startX = e.clientX - st.panX;
        startY = e.clientY - st.panY;
        viewport.style.cursor = 'grabbing';
    });

    window.addEventListener('mousemove', (e) => {
        if (!isPanning) return;
        st.panX = e.clientX - startX;
        st.panY = e.clientY - startY;
        updateTransform();
    });

    window.addEventListener('mouseup', (e) => {
        if (isPanning) {
            isPanning = false;
            if (viewport) viewport.style.cursor = '';
            
            // 若位移极小（< 6px），判定为纯点击动作，触发单帧放大
            const dist = Math.hypot(e.clientX - downClientX, e.clientY - downClientY);
            if (dist < 6) {
                const cell = e.target.closest('[data-frame-idx]');
                if (cell && cell.dataset.frameIdx !== undefined) {
                    const idx = parseInt(cell.dataset.frameIdx, 10);
                    if (!isNaN(idx)) {
                        openCollageFrameLightbox(idx);
                    }
                }
            }
        }
    });

    // 放大镜交互 (Loupe Magnifier)
    if (st.magnifierActive && container && magnifier && loupeImg) {
        viewport.addEventListener('mousemove', (e) => {
            const rect = container.getBoundingClientRect();
            if (e.clientX >= rect.left && e.clientX <= rect.right &&
                e.clientY >= rect.top && e.clientY <= rect.bottom) {
                magnifier.style.display = 'block';
                const loupeSize = 180;
                const loupeX = e.clientX - loupeSize / 2;
                const loupeY = e.clientY - loupeSize / 2;
                magnifier.style.left = `${loupeX}px`;
                magnifier.style.top = `${loupeY}px`;

                // 计算内部图片偏移 (2.5x 放大)
                const relX = (e.clientX - rect.left) / rect.width;
                const relY = (e.clientY - rect.top) / rect.height;

                const magZoom = 2.5;
                const imgW = rect.width * magZoom;
                const imgH = rect.height * magZoom;

                loupeImg.style.width = `${imgW}px`;
                loupeImg.style.height = `${imgH}px`;
                loupeImg.style.transform = `translate(${-relX * imgW + loupeSize / 2}px, ${-relY * imgH + loupeSize / 2}px)`;
            } else {
                magnifier.style.display = 'none';
            }
        });

        viewport.addEventListener('mouseleave', () => {
            magnifier.style.display = 'none';
        });
    }

    // 地平线拖拽调整 (Draggable Horizon Line)
    if (horizonLine && container) {
        let isDraggingHorizon = false;
        horizonLine.addEventListener('mousedown', (e) => {
            e.stopPropagation();
            isDraggingHorizon = true;
        });

        window.addEventListener('mousemove', (e) => {
            if (!isDraggingHorizon) return;
            const rect = container.getBoundingClientRect();
            let relY = ((e.clientY - rect.top) / rect.height) * 100;
            relY = Math.max(5, Math.min(95, relY));
            st.guidelines.horizonY = relY;
            horizonLine.style.top = `${relY}%`;
            const handleText = horizonLine.querySelector('.cv-line-handle');
            if (handleText) handleText.textContent = `地平线 ${Math.round(relY)}%`;
        });

        window.addEventListener('mouseup', () => {
            isDraggingHorizon = false;
        });
    }
}

function bindCompareSliderEvents(modal) {
    const st = collageViewerState;
    const splitViewport = modal.querySelector('#cv-split-viewport');
    const layerA = modal.querySelector('#cv-layer-a');
    const divider = modal.querySelector('#cv-split-divider');
    const selectA = modal.querySelector('#cv-select-frame-a');
    const selectB = modal.querySelector('#cv-select-frame-b');

    // 1. 下拉框选择帧
    if (selectA) {
        selectA.addEventListener('change', (e) => {
            st.comparePair[0] = parseInt(e.target.value, 10);
            renderCollageViewerModal();
        });
    }
    if (selectB) {
        selectB.addEventListener('change', (e) => {
            st.comparePair[1] = parseInt(e.target.value, 10);
            renderCollageViewerModal();
        });
    }

    // 2. 上一对/下一对按钮
    modal.querySelectorAll('[data-act="prev-pair"]').forEach(b => {
        b.addEventListener('click', () => navigateComparePair(-1));
    });
    modal.querySelectorAll('[data-act="next-pair"]').forEach(b => {
        b.addEventListener('click', () => navigateComparePair(1));
    });

    // 3. 分割滑块拖动
    if (splitViewport && layerA && divider) {
        let isSliding = false;

        const updateSplit = (clientX) => {
            const rect = splitViewport.getBoundingClientRect();
            let ratio = ((clientX - rect.left) / rect.width) * 100;
            ratio = Math.max(0, Math.min(100, ratio));
            st.splitRatio = ratio;
            layerA.style.width = `${ratio}%`;
            divider.style.left = `${ratio}%`;
        };

        splitViewport.addEventListener('mousedown', (e) => {
            isSliding = true;
            updateSplit(e.clientX);
        });

        window.addEventListener('mousemove', (e) => {
            if (!isSliding) return;
            updateSplit(e.clientX);
        });

        window.addEventListener('mouseup', () => {
            isSliding = false;
        });
    }

    // 4. 点击对比标签单独放大
    const tagA = modal.querySelector('.cv-split-tag.tag-a');
    if (tagA) {
        tagA.style.cursor = 'pointer';
        tagA.title = '点击单独放大查看基准帧 A';
        tagA.addEventListener('click', (e) => {
            e.stopPropagation();
            const seqA = st.comparePair[0];
            const idx = st.frames.findIndex(f => Number(f.sequence) === Number(seqA));
            openCollageFrameLightbox(idx !== -1 ? idx : 0);
        });
    }
    const tagB = modal.querySelector('.cv-split-tag.tag-b');
    if (tagB) {
        tagB.style.cursor = 'pointer';
        tagB.title = '点击单独放大查看递进帧 B';
        tagB.addEventListener('click', (e) => {
            e.stopPropagation();
            const seqB = st.comparePair[1];
            const idx = st.frames.findIndex(f => Number(f.sequence) === Number(seqB));
            openCollageFrameLightbox(idx !== -1 ? idx : 1);
        });
    }
}

function navigateComparePair(delta) {
    const st = collageViewerState;
    const frames = st.frames;
    if (frames.length < 2) return;

    let currentIdx = frames.findIndex(f => Number(f.sequence) === Number(st.comparePair[0]));
    if (currentIdx === -1) currentIdx = 0;

    let newIdx = currentIdx + delta;
    if (newIdx < 0) newIdx = 0;
    if (newIdx > frames.length - 2) newIdx = frames.length - 2;

    st.comparePair = [frames[newIdx].sequence, frames[newIdx + 1].sequence];
    renderCollageViewerModal();
}

// Node 单测导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        openCollageViewer,
        closeCollageViewer,
        openCollageFrameLightbox,
        collageViewerState,
    };
}
