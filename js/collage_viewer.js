/* =====================================================================
   交互式多宫格检查器 & 原片对标滑块（Interactive Collage & Benchmark Viewer）
   ---------------------------------------------------------------------
   提供工业级全景视觉连续性快检与原片对标能力：
   1. 共享 Stage (.cv-stage) 解决 9:16 vs 16:9 Letterbox 与几何投影错位
   2. 四种对比形态（SPLIT 裁切 / FADE 渐变 / DIFF 差异高亮 / SIDE 同步并排）+ Space 瞬时 A/B 闪烁
   3. 单游标 (focusSeq) + 并集拍数轴 (beatAxis) —— 缺帧项目亦能翻看全部原片抽帧
   4. AbortController 统一全局事件生命周期 + Pointer Events 拖拽与指针捕捉
   5. 双行胶片条导航 (.cv-filmstrip) + 审查结论带 (.cv-issue-bar) 就地处置 (修复/重渲)
   6. 分级重绘与相邻帧图片预加载，杜绝白闪与全量 innerHTML 重构
   ===================================================================== */

const CV_STORAGE_KEY = 'cv_viewer_prefs_v1';

let collageViewerState = {
    open: false,
    mode: 'collage', // 'collage' | 'compare'
    compareMode: 'split', // 'split' | 'fade' | 'diff' | 'side'
    compareType: 'benchmark', // 'benchmark' | 'adjacent'
    activeCollageTab: 'generated', // 'generated' | 'source' | 'dual'

    // 单游标与拍数轴
    focusSeq: 1,
    benchmarkFrameSeq: 1, // 兼容老代码
    comparePair: [1, 2], // 兼容老代码
    beatAxis: [], // [{ seq, hasGen, genUrl, hasRef, refUrl, issueReason, severity, isFixable, frame }]

    // 原始数据
    refFrames: {},
    frames: [],
    idea: null,
    totalBeats: 0,
    collageUrl: '',
    sourceCollageUrl: '',

    // 视口变换（平移与缩放）
    zoom: 1.0,
    panX: 0,
    panY: 0,
    isPanning: false,
    dragStartX: 0,
    dragStartY: 0,

    // 对比参数
    splitRatio: 50, // 0 - 100%
    fadeRatio: 50, // 0 - 100% (Gen 0% -> Ref 100%)
    isBlinking: false, // Space 键瞬时闪烁
    blinkTarget: 'ref', // 'ref' | 'gen'

    // 辅助线与工具（Stage 层共享）
    guidelines: {
        horizon: true,
        horizonY: 50, // %
        thirds: false,
        crosshair: false,
    },
    magnifierActive: false,
    filter: 'normal', // 'normal' | 'mono' | 'contrast'

    // 胶片条筛选
    stripFilter: 'all', // 'all' | 'flagged' | 'missing'

    // 事件控制器：window 级整场一只，DOM 级每次重绘换一只
    abortController: null,
    domAbortController: null,
    globalKeysBound: false,
};

/**
 * 图片加载失败时就地降级。
 * openCollageViewer 在 idea 没带 frames 时会按 image_count 拼出 img_00N.webp 路径，
 * 这些路径是猜的（stepped 流水线进来就是这种形态）——猜错时必须让界面说实话，
 * 否则缺 16 拍的项目胶片条会显示 17 个"已生成"，「⭕ 未生成」还恒为 0。
 */
function cvMarkBrokenImage(img) {
    if (!img) return;
    img.style.display = 'none';
    const holder = img.parentNode;
    if (holder) holder.classList.add('cv-img-broken');

    const seq = parseInt(img.dataset.cvSeq, 10);
    if (img.dataset.cvRole !== 'gen' || isNaN(seq)) return;
    const beat = (collageViewerState.beatAxis || []).find(b => b.seq === seq);
    if (!beat || !beat.synthetic || !beat.hasGen) return;
    beat.hasGen = false;
    beat.genUrl = '';
    refreshStripFilterCounts();
}

/** 碎图纠正台账后同步筛选按钮上的计数，不整条重绘（会打断正在看的那一拍）。 */
function refreshStripFilterCounts() {
    const st = collageViewerState;
    const strip = document.getElementById('cv-filmstrip');
    if (!strip) return;
    const missing = st.beatAxis.filter(b => !b.hasGen).length;
    const flagged = st.beatAxis.filter(b => b.severity !== 'none').length;
    const mBtn = strip.querySelector('[data-strip-filter="missing"]');
    if (mBtn) mBtn.textContent = `⭕ 未生成 (${missing})`;
    const fBtn = strip.querySelector('[data-strip-filter="flagged"]');
    if (fBtn) fBtn.textContent = `⚠ 仅看问题 (${flagged})`;
}

if (typeof window !== 'undefined') {
    window.cvMarkBrokenImage = cvMarkBrokenImage;
}

// 预加载缓存
const _cvImagePreloadCache = new Set();
function preloadImages(urls = []) {
    urls.forEach(u => {
        if (u && !_cvImagePreloadCache.has(u)) {
            _cvImagePreloadCache.add(u);
            const img = new Image();
            img.src = u;
        }
    });
}

function preloadAdjacentBeats(st) {
    if (!st || !st.beatAxis || !st.beatAxis.length) return;
    const curIdx = st.beatAxis.findIndex(b => b.seq === st.focusSeq);
    if (curIdx === -1) return;
    const urls = [];
    for (let offset = -2; offset <= 2; offset++) {
        const b = st.beatAxis[curIdx + offset];
        if (b) {
            if (b.genUrl) urls.push(b.genUrl);
            if (b.refUrl) urls.push(b.refUrl);
        }
    }
    preloadImages(urls);
}

// 本地存储偏好
function loadViewerPrefs(projectTitle) {
    try {
        const raw = localStorage.getItem(CV_STORAGE_KEY);
        if (!raw) return {};
        const all = JSON.parse(raw);
        return (projectTitle && all[projectTitle]) || all['__global__'] || {};
    } catch (_) {
        return {};
    }
}

function saveViewerPrefs(projectTitle, prefs) {
    try {
        const raw = localStorage.getItem(CV_STORAGE_KEY);
        const all = raw ? JSON.parse(raw) : {};
        if (projectTitle) all[projectTitle] = { ...(all[projectTitle] || {}), ...prefs };
        all['__global__'] = { ...(all['__global__'] || {}), ...prefs };
        localStorage.setItem(CV_STORAGE_KEY, JSON.stringify(all));
    } catch (_) {}
}

/**
 * 构造并集拍数轴 (beatAxis)
 * 解耦生成进度与对标轴，确保缺帧项目也能浏览全部原片抽帧
 */
function buildBeatAxis(idea, frames, refFrames, totalBeatsOpt) {
    const seqSet = new Set();

    // 1. 从 ref_frames 收集
    if (refFrames && typeof refFrames === 'object') {
        Object.keys(refFrames).forEach(k => {
            const num = parseInt(k, 10);
            if (!isNaN(num) && num > 0) seqSet.add(num);
        });
    }

    // 2. 从 frames 收集
    if (Array.isArray(frames)) {
        frames.forEach((f, i) => {
            const num = parseInt(f.sequence || (i + 1), 10);
            if (!isNaN(num) && num > 0) seqSet.add(num);
        });
    }

    // 3. 从 total_beats / image_count 收集
    let maxBeats = 0;
    if (typeof totalBeatsOpt === 'number' && totalBeatsOpt > 0) maxBeats = totalBeatsOpt;
    else if (idea) {
        maxBeats = idea.image_count || (idea.frameRun && idea.frameRun.image_count) || (idea.frameRun && idea.frameRun.total_beats) || idea.total_beats || 0;
        if (!maxBeats && idea.prompt_block) {
            const matches = idea.prompt_block.match(/(?:图片|帧|Frame|Image)\s*(\d+)/gi);
            if (matches && matches.length) maxBeats = matches.length;
        }
    }

    if (seqSet.size > 0) {
        const highestInSet = Math.max(...Array.from(seqSet));
        maxBeats = Math.max(maxBeats, highestInSet);
    }
    if (maxBeats <= 0) maxBeats = 1;

    for (let i = 1; i <= maxBeats; i++) {
        seqSet.add(i);
    }

    const sortedSeqs = Array.from(seqSet).sort((a, b) => a - b);
    const axis = sortedSeqs.map(seq => {
        const f = Array.isArray(frames) ? frames.find(item => Number(item.sequence) === seq) : null;
        const hasGen = !!(f && (f.url || f.file));
        const genUrl = hasGen ? (f.url || f.file) : '';
        const rawRef = refFrames ? (refFrames[seq] || refFrames[String(seq)]) : '';
        const hasRef = !!rawRef;
        const refUrl = hasRef ? (String(rawRef).indexOf('/outputs/') !== -1 ? String(rawRef).substring(String(rawRef).indexOf('/outputs/')) : String(rawRef)) : '';

        // 提取审查与质检问题
        let issueReason = '';
        let severity = 'none'; // 'failed' | 'warned' | 'dirty' | 'stale' | 'none'
        let isFixable = false;

        if (f) {
            issueReason = (f.continuity_check && f.continuity_check.reason)
                || f.manual_issue
                || f.vlm_qa_reason
                || f.degraded_reason
                || '';

            const isFailed = (f.quality_gate === 'frame_continuity_failed'
                || f.quality_gate === 'vlm_qa_failed'
                || f.quality_gate === 'sequence_review_flagged');

            const isWarned = (!isFailed && (
                f.quality_gate === 'auto_approved_degraded'
                || f.quality_gate === 'sequence_review_skipped'
                || (f.continuity_check && f.continuity_check.status === 'warned')
                || (f.quality_gate === 'auto_approved' && typeof f.vlm_qa_reason === 'string' && f.vlm_qa_reason.indexOf('WARN') === 0)
            ));

            const isDirty = !!f.prompt_dirty;
            const isStale = !!(f.stale_lineage || f.quality_gate === 'stale' || f.stale);

            if (isFailed || (f.manual_issue && f.manual_issue.trim())) {
                severity = 'failed';
                isFixable = true;
            } else if (isDirty) {
                severity = 'dirty';
                isFixable = true;
                if (!issueReason) issueReason = '提示词已改写，画面尚未重渲';
            } else if (isStale) {
                severity = 'stale';
                if (!issueReason) issueReason = '过期：父帧已被重渲，血统不一致';
            } else if (isWarned) {
                severity = 'warned';
            }
        }

        return {
            seq,
            hasGen,
            genUrl,
            synthetic: !!(f && f.synthetic),
            hasRef,
            refUrl,
            issueReason,
            severity,
            isFixable,
            frame: f || { sequence: seq },
        };
    });

    return axis;
}

/**
 * 统一快捷入口：打开原片基准对标滑块
 * @param {Object} opts
 * @param {number} [opts.seq] 拍号
 * @param {string} [opts.source] 来源
 */
function openBenchmarkCompare(opts = {}) {
    return openCollageViewer({
        ...opts,
        initialMode: 'compare',
        compareType: 'benchmark',
        initialFrameSeq: opts.seq || opts.initialFrameSeq || 1,
    });
}

/**
 * 打开交互式多宫格检查器弹窗
 * @param {Object} opts
 * @param {string} [opts.collageUrl] 拼图 URL
 * @param {string} [opts.sourceCollageUrl] 爆款原片拼图 URL
 * @param {Object} [opts.refFrames] 节拍原片抽帧字典 { seq: url }
 * @param {Object} [opts.idea] 创意对象
 * @param {number} [opts.initialFrameSeq] 初始定位帧号
 * @param {string} [opts.initialMode] 初始模式 ('collage' | 'compare')
 * @param {string} [opts.compareType] 对比子模式 ('adjacent' | 'benchmark')
 * @param {string} [opts.compareMode] 对比形态 ('split' | 'fade' | 'diff' | 'side')
 */
function openCollageViewer(opts = {}) {
    const idea = opts.idea || (typeof currentIdea !== 'undefined' ? currentIdea : null);
    let collageUrl = opts.collageUrl || (idea && idea.collage_url) || (idea && idea.frameRun && idea.frameRun.collage_url) || '';
    let sourceCollageUrl = opts.sourceCollageUrl || (idea && idea.source_collage) || (idea && idea.source_collage_url) || '';
    let rawRefFrames = opts.refFrames || (idea && idea.ref_frames) || (idea && idea.reference_frames) || (idea && idea.frameRun && idea.frameRun.ref_frames) || {};
    const refRoles = opts.refFrameRoles || (idea && idea.ref_frame_roles) || (idea && idea.frameRun && idea.frameRun.ref_frame_roles) || {};

    // 归一化 refFrames
    const refFrames = {};
    if (rawRefFrames && typeof rawRefFrames === 'object') {
        Object.entries(rawRefFrames).forEach(([k, v]) => {
            const num = parseInt(k, 10);
            if (!isNaN(num) && v) {
                const u = String(v);
                refFrames[num] = u.indexOf('/outputs/') !== -1 ? u.substring(u.indexOf('/outputs/')) : u;
            }
        });
    }
    const hasRefs = Object.keys(refFrames).length > 0;

    // 提取帧列表
    let frames = [];
    if (idea && idea.frameRun && Array.isArray(idea.frameRun.frames) && idea.frameRun.frames.length) {
        frames = idea.frameRun.frames.filter(f => f.url || f.file);
    } else if (idea && Array.isArray(idea.frames) && idea.frames.length) {
        frames = idea.frames.filter(f => f.url || f.file);
    } else if (idea && Array.isArray(idea.covers) && idea.covers.length) {
        frames = idea.covers.map((c, i) => ({ sequence: i + 1, url: typeof c === 'string' ? c : (c.url || c.file) }));
    }

    // 构造默认帧路径（若有数量但尚未生成图片文件对象）
    if (!frames.length && idea && idea.title && (idea.image_count || (idea.frameRun && idea.frameRun.image_count))) {
        const count = idea.image_count || idea.frameRun.image_count || 0;
        for (let i = 1; i <= count; i++) {
            frames.push({
                sequence: i,
                url: `/outputs/${idea.title}/frames/img_${String(i).padStart(3, '0')}.webp`,
                synthetic: true, // 路径是按数量猜的，不是台账里确有的文件
            });
        }
    }

    // 构造全域拍数轴
    const beatAxis = buildBeatAxis(idea, frames, refFrames, opts.totalBeats || (idea && (idea.total_beats || idea.image_count)));

    if (!collageUrl && !frames.length && !sourceCollageUrl && !hasRefs && !beatAxis.length) {
        if (typeof showToast === 'function') {
            showToast('当前尚未生成拼图或关键帧图片', 'warning');
        }
        return;
    }

    const initSeq = opts.initialFrameSeq || opts.seq || 1;
    let compType = opts.compareType || 'adjacent';
    if (!opts.compareType && (opts.initialMode === 'compare' || opts.seq) && hasRefs) {
        compType = 'benchmark';
    }

    // 读取持久化偏好
    const projectTitle = (idea && (idea.title || idea.project_title)) || '';
    const prefs = loadViewerPrefs(projectTitle);

    // 清理之前的两级控制器
    if (collageViewerState.abortController) {
        collageViewerState.abortController.abort();
        collageViewerState.abortController = null;
    }
    if (collageViewerState.domAbortController) {
        collageViewerState.domAbortController.abort();
        collageViewerState.domAbortController = null;
    }
    const abortController = new AbortController();

    collageViewerState = {
        open: true,
        mode: opts.initialMode || 'collage',
        compareMode: opts.compareMode || prefs.compareMode || 'split',
        compareType: compType,
        activeCollageTab: opts.activeCollageTab || 'generated',
        focusSeq: initSeq,
        benchmarkFrameSeq: initSeq,
        comparePair: [
            initSeq > 1 ? initSeq - 1 : 1,
            initSeq > 1 ? initSeq : 2
        ],
        beatAxis,
        refFrames,
        frames,
        idea,
        totalBeats: beatAxis.length,
        collageUrl,
        sourceCollageUrl,
        zoom: 1.0,
        panX: 0,
        panY: 0,
        isPanning: false,
        dragStartX: 0,
        dragStartY: 0,
        magnifierActive: false,
        guidelines: {
            horizon: prefs.guidelines ? !!prefs.guidelines.horizon : true,
            horizonY: (prefs.guidelines && prefs.guidelines.horizonY) || 50,
            thirds: prefs.guidelines ? !!prefs.guidelines.thirds : false,
            crosshair: prefs.guidelines ? !!prefs.guidelines.crosshair : false,
        },
        filter: prefs.filter || 'normal',
        splitRatio: typeof prefs.splitRatio === 'number' ? prefs.splitRatio : 50,
        fadeRatio: typeof prefs.fadeRatio === 'number' ? prefs.fadeRatio : 50,
        isBlinking: false,
        blinkTarget: 'ref',
        stripFilter: 'all',
        abortController,
        domAbortController: null,
        globalKeysBound: false,
    };

    // 预加载当前拍及相邻拍
    preloadAdjacentBeats(collageViewerState);

    // 异步拉取原片基准抽帧（增强嗅探）
    let projTitle = (idea && (idea.title || idea.project_title || idea.project_key)) || opts.title || '';
    let replicaJobId = (idea && (idea.replica_job_id || idea.source_job_id || (typeof idea.id === 'string' && idea.id.startsWith('replica_') ? idea.id : ''))) || opts.jobId || '';

    if (!replicaJobId) {
        const candidateStrings = [
            projTitle,
            idea && idea.collage_url,
            idea && idea.source_collage,
            idea && idea.frameRun && idea.frameRun.collage_url,
            ...(Array.isArray(frames) ? frames.map(f => f.url || f.file || '') : [])
        ].filter(Boolean);

        for (const str of candidateStrings) {
            const m = str.match(/(?:replica_|run_replica_)([a-f0-9]{12})/i);
            if (m) {
                replicaJobId = `replica_${m[1]}`;
                break;
            }
        }
    }

    if (!projTitle && Array.isArray(frames) && frames.length) {
        const firstUrl = frames[0].url || frames[0].file || '';
        const mDir = firstUrl.match(/\/outputs\/([^/]+)\//);
        if (mDir) {
            projTitle = mDir[1];
        }
    }

    if (projTitle || replicaJobId) {
        const fetchUrl = `/api/project/references?title=${encodeURIComponent(projTitle)}&job_id=${encodeURIComponent(replicaJobId)}&total_beats=${beatAxis.length || ''}`;
        fetch(fetchUrl)
            .then(res => res.json())
            .then(data => {
                if (data && data.status === 'ok') {
                    let changed = false;
                    if (data.ref_frames && Object.keys(data.ref_frames).length) {
                        Object.entries(data.ref_frames).forEach(([k, v]) => {
                            const num = parseInt(k, 10);
                            if (!isNaN(num) && v) {
                                const u = String(v);
                                const normU = u.indexOf('/outputs/') !== -1 ? u.substring(u.indexOf('/outputs/')) : u;
                                if (collageViewerState.refFrames[num] !== normU) {
                                    collageViewerState.refFrames[num] = normU;
                                    changed = true;
                                }
                            }
                        });
                        if (idea) {
                            idea.ref_frames = collageViewerState.refFrames;
                            if (idea.frameRun) idea.frameRun.ref_frames = collageViewerState.refFrames;
                        }
                        if (changed) {
                            collageViewerState.beatAxis = buildBeatAxis(collageViewerState.idea, collageViewerState.frames, collageViewerState.refFrames, collageViewerState.totalBeats);
                            if (collageViewerState.mode === 'compare' && collageViewerState.compareType === 'adjacent' && !opts.compareType) {
                                collageViewerState.compareType = 'benchmark';
                            }
                        }
                    }
                    if (data.source_collage_url) {
                        const cu = String(data.source_collage_url);
                        const normCu = cu.indexOf('/outputs/') !== -1 ? cu.substring(cu.indexOf('/outputs/')) : cu;
                        if (collageViewerState.sourceCollageUrl !== normCu) {
                            collageViewerState.sourceCollageUrl = normCu;
                            changed = true;
                        }
                        if (idea) {
                            idea.source_collage = normCu;
                            if (idea.frameRun) idea.frameRun.source_collage = normCu;
                        }
                    }
                    if (changed && collageViewerState.open) {
                        renderCollageViewerModal();
                    }
                }
            })
            .catch(() => {});
    }

    renderCollageViewerModal();
}

function closeCollageViewer() {
    if (collageViewerState.abortController) {
        collageViewerState.abortController.abort();
        collageViewerState.abortController = null;
    }
    if (collageViewerState.domAbortController) {
        collageViewerState.domAbortController.abort();
        collageViewerState.domAbortController = null;
    }
    collageViewerState.globalKeysBound = false;
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

/**
 * 模态窗全量渲染（仅在打开弹窗或切换主模式时调用）
 */
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
    const totalFrames = st.beatAxis.length || st.frames.length;
    const hasRefs = !!(st.refFrames && Object.keys(st.refFrames).length);
    const hasSourceCollage = !!st.sourceCollageUrl;
    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));

    modal.innerHTML = `
        <div class="modal-content glass-panel cv-modal-content">
            <!-- 头部控制工具条 -->
            <div class="cv-header">
                <div class="cv-header-left">
                    <span class="cv-title-icon">🎯</span>
                    <div class="cv-title-text">
                        <h3>交互式多宫格检查器 · 原片对标与视觉连续性</h3>
                        <span class="cv-subtitle">共享 Stage 几何对齐 · 4 种对比形态 · 空格瞬时闪烁 · 双行胶片条 · 地平透视对齐</span>
                    </div>
                </div>

                <div class="cv-toolbar">
                    <!-- 主模式切换 -->
                    <div class="cv-toolgroup cv-mode-group">
                        <button type="button" class="cv-tool-btn ${st.mode === 'collage' ? 'is-active' : ''}" data-act="mode-collage" title="5列全景多宫格拼图总览 (快捷键 M)">⊞ 5列拼图</button>
                        <button type="button" class="cv-tool-btn ${st.mode === 'compare' ? 'is-active' : ''}" data-act="mode-compare" title="前后帧/原片基准对标 (快捷键 C)">⇄ 对比滑块</button>
                    </div>

                    <!-- 拼图源切换 (Collage模式 + 存在原片拼图) -->
                    ${(st.mode === 'collage' && hasSourceCollage) ? `
                    <div class="cv-toolgroup cv-collage-only">
                        <button type="button" class="cv-tool-btn ${st.activeCollageTab === 'generated' ? 'is-active' : ''}" data-act="collage-tab-gen" title="查看本次生成的 5 列拼图">🌟 生成拼图</button>
                        <button type="button" class="cv-tool-btn ${st.activeCollageTab === 'source' ? 'is-active' : ''}" data-act="collage-tab-source" title="查看爆款原片 5 列拼图">🎯 原片拼图</button>
                        <button type="button" class="cv-tool-btn ${st.activeCollageTab === 'dual' ? 'is-active' : ''}" data-act="collage-tab-dual" title="左右并排双拼图同屏比对">⚡ 左右双拼</button>
                    </div>
                    ` : ''}

                    <!-- 对比形态切换 (Compare模式) -->
                    <div class="cv-toolgroup cv-compare-only" ${st.mode !== 'compare' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn ${st.compareMode === 'split' ? 'is-active' : ''}" data-act="compare-mode-split" title="滑动裁切 (快捷键 1)">✂ 滑动</button>
                        <button type="button" class="cv-tool-btn ${st.compareMode === 'fade' ? 'is-active' : ''}" data-act="compare-mode-fade" title="透明度渐变 (快捷键 2)">🌫 渐变</button>
                        <button type="button" class="cv-tool-btn ${st.compareMode === 'diff' ? 'is-active' : ''}" data-act="compare-mode-diff" title="差异高亮发光 (快捷键 3)">⚡ 差异</button>
                        <button type="button" class="cv-tool-btn ${st.compareMode === 'side' ? 'is-active' : ''}" data-act="compare-mode-side" title="同步双焦并排 (快捷键 4)">⫽ 并排</button>
                    </div>

                    <!-- 对比子模式 (Compare模式 + 存在原片抽帧) -->
                    ${hasRefs ? `
                    <div class="cv-toolgroup cv-compare-only" ${st.mode !== 'compare' ? 'style="display:none;"' : ''}>
                        <button type="button" class="cv-tool-btn ${st.compareType === 'benchmark' ? 'is-active' : ''}" data-act="compare-type-benchmark" title="生成帧 vs 爆款原片抽帧 (Gen vs Ref)">🎯 原片对标</button>
                        <button type="button" class="cv-tool-btn ${st.compareType === 'adjacent' ? 'is-active' : ''}" data-act="compare-type-adjacent" title="前后帧递进对比 (A vs B)">⇄ 邻帧前后</button>
                    </div>
                    ` : ''}

                    <!-- 辅助基准线 (Stage 级通用) -->
                    <div class="cv-toolgroup">
                        <button type="button" class="cv-tool-btn ${st.guidelines.horizon ? 'is-active' : ''}" data-act="toggle-horizon" title="水平地平基准线（可上下拖拽，横跨两侧） (快捷键 H)">📏 地平线</button>
                        <button type="button" class="cv-tool-btn ${st.guidelines.thirds ? 'is-active' : ''}" data-act="toggle-thirds" title="三分黄金分割构图线 (快捷键 T)">⌗ 三分线</button>
                        <button type="button" class="cv-tool-btn ${st.guidelines.crosshair ? 'is-active' : ''}" data-act="toggle-crosshair" title="中心透视十字线 (快捷键 X)">✚ 透视十字</button>
                        <button type="button" class="cv-tool-btn ${st.magnifierActive ? 'is-active' : ''}" data-act="toggle-magnifier" title="开启/关闭局部 2.5x 悬浮放大镜 (快捷键 Z)">🔬 放大镜</button>
                    </div>

                    <!-- 滤镜切换 -->
                    <div class="cv-toolgroup">
                        <button type="button" class="cv-tool-btn ${st.filter === 'normal' ? 'is-active' : ''}" data-act="filter-normal" title="正常色彩">🎨 原始</button>
                        <button type="button" class="cv-tool-btn ${st.filter === 'mono' ? 'is-active' : ''}" data-act="filter-mono" title="单色黑白（快速检查明暗跳变）">◐ 单色</button>
                        <button type="button" class="cv-tool-btn ${st.filter === 'contrast' ? 'is-active' : ''}" data-act="filter-contrast" title="高反差（快速检查几何边缘）">⚡ 反差</button>
                    </div>

                    <!-- 缩放控制 -->
                    <div class="cv-toolgroup">
                        <button type="button" class="cv-tool-btn" data-act="zoom-out" title="缩小 (-)">-</button>
                        <span class="cv-zoom-display">${Math.round(st.zoom * 100)}%</span>
                        <button type="button" class="cv-tool-btn" data-act="zoom-in" title="放大 (+)">+</button>
                        <button type="button" class="cv-tool-btn" data-act="zoom-fit" title="重置缩放与平移 (快捷键 0)">适应</button>
                    </div>

                    <!-- 动作与关闭 -->
                    <div class="cv-toolgroup">
                        <button type="button" class="cv-tool-btn" data-act="open-lightbox-current" title="单独放大浏览当前帧">🔍 单帧</button>
                        ${st.collageUrl ? `<a href="${escapeStr(st.collageUrl)}" download target="_blank" class="cv-tool-btn" title="下载原图" style="text-decoration:none;">⬇</a>` : ''}
                        <button type="button" class="close-btn cv-close-btn" title="关闭 (Esc)">&times;</button>
                    </div>
                </div>
            </div>

            <!-- 主画布区域 -->
            <div class="cv-body" id="cv-main-body">
                ${st.mode === 'collage' ? renderCollageViewHtml() : renderCompareViewHtml()}
            </div>

            <!-- 底部导航与状态条 -->
            <div class="cv-footer" id="cv-footer">
                ${st.mode === 'compare' ? renderFilmstripHtml() : ''}
                <div class="cv-footer-bottom">
                    <div class="cv-footer-hints">
                        ${st.mode === 'collage'
                            ? '💡 提示：点击拼图中任意画面<b>全屏放大</b>；按住鼠标平移画布，滚轮无级缩放；地平线支持拖拽。'
                            : '💡 提示：<b>长按 [空格]</b> 瞬时闪烁原片基准；<b>[1-4]</b> 切换对比形态；<b>[← / →]</b> 换拍；<b>[Shift + ←/→]</b> 跳至问题拍；<b>[0]</b> 居中/复位。'
                        }
                    </div>
                    <div class="cv-footer-stats">
                        共 <b>${totalFrames}</b> 拍 · ${hasRefs ? `<span style="color:#f59e0b;">已绑定 ${Object.keys(st.refFrames).length} 拍原片抽帧</span>` : '未绑定原片抽帧'}
                    </div>
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

    // 双拼图模式 (Dual Collage)
    if (st.activeCollageTab === 'dual' && (st.collageUrl || st.frames.length) && st.sourceCollageUrl) {
        return `
            <div class="cv-canvas-viewport" id="cv-canvas-viewport">
                <div class="cv-canvas-transform" id="cv-canvas-transform" style="transform: translate(${st.panX}px, ${st.panY}px) scale(${st.zoom});">
                    <div class="cv-dual-collage-wrap ${filterCls}">
                        <div class="cv-dual-collage-col">
                            <span class="cv-dual-collage-title gen-title">🌟 本次生成 5 列拼图</span>
                            ${st.collageUrl 
                                ? `<img src="${st.collageUrl}" class="cv-dual-collage-img" draggable="false" onerror="this.style.display='none'; var fb = document.getElementById('cv-dual-gen-fallback'); if(fb) fb.style.display='grid';">
                                   <div id="cv-dual-gen-fallback" style="display:none;">${renderDynamicCollageGridHtml()}</div>`
                                : renderDynamicCollageGridHtml()
                            }
                        </div>
                        <div class="cv-dual-collage-col">
                            <span class="cv-dual-collage-title ref-title">🎯 爆款原片 5 列拼图</span>
                            <img src="${st.sourceCollageUrl}" class="cv-dual-collage-img" draggable="false" />
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    const isSourceTab = (st.activeCollageTab === 'source' && st.sourceCollageUrl);
    const currentCollageUrl = isSourceTab ? st.sourceCollageUrl : st.collageUrl;

    return `
        <div class="cv-canvas-viewport" id="cv-canvas-viewport">
            <div class="cv-canvas-transform" id="cv-canvas-transform" style="transform: translate(${st.panX}px, ${st.panY}px) scale(${st.zoom});">
                <div class="cv-image-container ${filterCls}" id="cv-image-container">
                    ${currentCollageUrl 
                        ? `<img src="${currentCollageUrl}" id="cv-collage-img" class="cv-collage-img" alt="5-Column Keyframe Collage" draggable="false" onerror="this.style.display='none'; var fb = document.getElementById('cv-dyn-grid-fallback'); if(fb) fb.style.display='grid';">
                           ${(!isSourceTab && st.frames.length) ? `<div id="cv-dyn-grid-fallback" style="display:none;">${renderDynamicCollageGridHtml()}</div>${renderCollageHotspotsHtml(st)}` : ''}` 
                        : renderDynamicCollageGridHtml()
                    }
                    
                    <!-- 辅助线图层 -->
                    ${renderGuidelinesHtml(st)}
                </div>
            </div>

            <!-- 局部悬浮放大镜 -->
            ${renderLoupeMagnifierHtml(currentCollageUrl || '', filterCls)}
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

function renderGuidelinesHtml(st) {
    return `
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
    `;
}

function renderLoupeMagnifierHtml(imgUrl, filterCls) {
    return `
        <div class="cv-loupe-magnifier" id="cv-loupe-magnifier" style="display: none;">
            <div class="cv-loupe-canvas-wrap">
                <img src="${imgUrl}" id="cv-loupe-img" class="cv-loupe-img ${filterCls}">
            </div>
            <div class="cv-loupe-crosshair"></div>
            <div class="cv-loupe-info">2.5×</div>
        </div>
    `;
}

/**
 * 渲染对比视图（Compare View）
 * 包含共享 Stage、4 种对比形态与审查结论带
 */
function renderCompareViewHtml() {
    const st = collageViewerState;
    const filterCls = st.filter === 'mono' ? 'cv-filter-mono' : (st.filter === 'contrast' ? 'cv-filter-contrast' : '');
    const curSeq = st.focusSeq || 1;
    const curBeat = st.beatAxis.find(b => b.seq === curSeq) || st.beatAxis[0] || { seq: curSeq };

    // 获取 GEN 与 REF URL
    let urlGen = curBeat.genUrl || '';
    let urlRef = curBeat.refUrl || '';
    let labelGen = `🌟 IMG ${String(curSeq).padStart(3, '0')}`;
    let labelRef = `🎯 REF ${String(curSeq).padStart(3, '0')}`;

    if (st.compareType === 'adjacent') {
        const [seqA, seqB] = st.comparePair;
        const beatA = st.beatAxis.find(b => b.seq === seqA) || { seq: seqA };
        const beatB = st.beatAxis.find(b => b.seq === seqB) || { seq: seqB };
        urlGen = beatA.genUrl || '';
        urlRef = beatB.genUrl || '';
        labelGen = `IMG ${String(seqA).padStart(3, '0')} (前)`;
        labelRef = `IMG ${String(seqB).padStart(3, '0')} (后)`;
    }

    return `
        <div class="cv-compare-container">
            <!-- 顶部快捷切换工具条 -->
            <div class="cv-compare-topbar">
                <div class="cv-beat-indicator">
                    <button type="button" class="action-btn text-btn mini-btn" data-act="prev-beat" title="上一拍 (←)">◀ 上一拍</button>
                    <span class="cv-beat-tag">第 <b id="cv-cur-seq-num">${curSeq}</b> 拍 / 共 ${st.beatAxis.length} 拍</span>
                    <button type="button" class="action-btn text-btn mini-btn" data-act="next-beat" title="下一拍 (→)">下一拍 ▶</button>
                </div>

                ${st.compareMode === 'fade' ? `
                <div class="cv-fade-control">
                    <span class="cv-fade-label">GEN</span>
                    <input type="range" class="cv-fade-slider" id="cv-fade-slider" min="0" max="100" value="${st.fadeRatio}" title="透明度渐变 0-100%">
                    <span class="cv-fade-label">REF (${st.fadeRatio}%)</span>
                </div>
                ` : ''}

                <div class="cv-topbar-quick-actions">
                    <button type="button" class="action-btn text-btn mini-btn cv-blink-btn" data-act="blink-toggle" title="长按空格键或点击快速瞬时比对">⚡ 闪烁比对 (Space)</button>
                </div>
            </div>

            <!-- 主视口与共享 Stage -->
            <div class="cv-split-viewport ${filterCls}" id="cv-split-viewport">
                <!-- 共享 9:16 Stage 容器 -->
                <div class="cv-stage-wrapper" id="cv-stage-wrapper" style="transform: translate(${st.panX}px, ${st.panY}px) scale(${st.zoom});">
                    <div class="cv-stage cv-mode-${st.compareMode} ${st.isBlinking ? 'cv-is-blinking' : ''}" id="cv-compare-stage" style="--split: ${st.splitRatio}%; --fade: ${st.fadeRatio / 100};">
                        
                        <!-- 并排模式 (SIDE) 专用双视口 -->
                        ${st.compareMode === 'side' ? `
                            <div class="cv-side-wrapper">
                                <div class="cv-side-pane cv-side-gen">
                                    <span class="cv-split-tag tag-gen cv-tag-clickable" data-tag-role="gen">${labelGen}</span>
                                    ${urlGen 
                                        ? `<img src="${urlGen}" class="cv-stage-img" draggable="false" data-cv-seq="${curSeq}" data-cv-role="gen" onerror="cvMarkBrokenImage(this)" />`
                                        : `<div class="cv-stage-empty gen-empty"><span>第 ${curSeq} 拍未生成</span></div>`
                                    }
                                </div>
                                <div class="cv-side-pane cv-side-ref">
                                    <span class="cv-split-tag tag-ref cv-tag-clickable" data-tag-role="ref">${labelRef}</span>
                                    ${urlRef 
                                        ? `<img src="${urlRef}" class="cv-stage-img" draggable="false" />`
                                        : `<div class="cv-stage-empty ref-empty"><span>第 ${curSeq} 拍无原片抽帧</span></div>`
                                    }
                                </div>
                            </div>
                        ` : `
                            <!-- 下层图：REF / Frame B -->
                            <div class="cv-stage-layer cv-stage-ref" id="cv-stage-ref-layer">
                                ${urlRef 
                                    ? `<img src="${urlRef}" class="cv-compare-img cv-ref-img" draggable="false" alt="Reference Frame">`
                                    : `<div class="cv-stage-empty ref-empty"><span id="cv-ref-empty-text">第 ${curSeq} 拍暂无关联原片抽帧</span></div>`
                                }
                                <span class="cv-split-tag tag-ref cv-tag-clickable" id="cv-ref-tag" data-tag-role="ref" title="点击单独放大查看原片基准">${labelRef}</span>
                            </div>

                            <!-- 上层图：GEN / Frame A -->
                            <div class="cv-stage-layer cv-stage-gen" id="cv-stage-gen-layer">
                                ${urlGen 
                                    ? `<img src="${urlGen}" class="cv-compare-img cv-gen-img" draggable="false" alt="Generated Frame" data-cv-seq="${curSeq}" data-cv-role="gen" onerror="cvMarkBrokenImage(this)">`
                                    : `<div class="cv-stage-empty gen-empty"><span id="cv-gen-empty-text">第 ${curSeq} 拍尚未生成关键帧</span></div>`
                                }
                                <span class="cv-split-tag tag-gen cv-tag-clickable" id="cv-gen-tag" data-tag-role="gen" title="点击单独放大查看生成帧">${labelGen}</span>
                            </div>

                            <!-- 分割滑块线 (仅 SPLIT 模式) -->
                            <div class="cv-split-divider" id="cv-split-divider" role="slider" aria-label="对比分割线" aria-valuenow="${st.splitRatio}" aria-valuemin="0" aria-valuemax="100" tabindex="0">
                                <div class="cv-divider-handle">
                                    <span>◀ ▶</span>
                                </div>
                            </div>
                        `}

                        <!-- 共享 Stage 辅助基准线 -->
                        ${renderGuidelinesHtml(st)}
                    </div>
                </div>

                <!-- 局部放大镜 -->
                ${renderLoupeMagnifierHtml(urlGen || urlRef || '', filterCls)}
            </div>

            <!-- Stage 下方审查结论带与就地处置 -->
            <div class="cv-issue-bar" id="cv-issue-bar">
                ${renderIssueBarHtml(curBeat)}
            </div>
        </div>
    `;
}

function renderIssueBarHtml(curBeat) {
    if (!curBeat) return '';
    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
    
    let badgeCls = 'badge-clean';
    let badgeText = '✅ 视觉连续性正常';
    let msgText = '画面基准稳定，空间与机位无明显漂移';

    if (curBeat.severity === 'failed') {
        badgeCls = 'badge-failed';
        badgeText = '🔴 审查未通过';
        msgText = curBeat.issueReason || '检测到场景或机位跳变';
    } else if (curBeat.severity === 'dirty') {
        badgeCls = 'badge-dirty';
        badgeText = '🟠 提示词已改';
        msgText = curBeat.issueReason || '提示词已被改写，画面尚未按新提示词重渲';
    } else if (curBeat.severity === 'stale') {
        badgeCls = 'badge-stale';
        badgeText = '🟠 血统过期';
        msgText = curBeat.issueReason || '父帧已被重渲，本帧血统不一致';
    } else if (curBeat.severity === 'warned') {
        badgeCls = 'badge-warned';
        badgeText = '🟡 宽松放行留痕';
        msgText = curBeat.issueReason || '存在微小瑕疵但已放行';
    } else if (!curBeat.hasGen) {
        badgeCls = 'badge-missing';
        badgeText = '⭕ 尚未生成';
        msgText = '本拍尚未生成关键帧画面，可点击右侧生成或先对标原片';
    }

    return `
        <div class="cv-issue-info">
            <span class="cv-issue-badge ${badgeCls}">${badgeText}</span>
            <span class="cv-issue-text">${escapeStr(msgText)}</span>
        </div>
        <div class="cv-issue-actions">
            ${curBeat.isFixable ? `<button type="button" class="action-btn mini-btn cv-btn-fix" data-act="fix-current-beat" title="定向修复本拍提示词并重新生成">🛠 修复此拍</button>` : ''}
            ${curBeat.hasGen 
                ? `<button type="button" class="action-btn mini-btn cv-btn-retry" data-act="retry-current-beat" title="重新生成本拍关键帧">⚡ 重渲此拍</button>`
                : `<button type="button" class="action-btn mini-btn cv-btn-gen" data-act="gen-current-beat" title="立即生成本拍关键帧">✨ 生成此拍</button>`
            }
        </div>
    `;
}

/**
 * 底部双行胶片条导航 (Filmstrip)
 */
function renderFilmstripHtml() {
    const st = collageViewerState;
    const filter = st.stripFilter;
    let filteredAxis = st.beatAxis;

    if (filter === 'flagged') {
        filteredAxis = st.beatAxis.filter(b => b.severity !== 'none');
    } else if (filter === 'missing') {
        filteredAxis = st.beatAxis.filter(b => !b.hasGen);
    }

    const flaggedCount = st.beatAxis.filter(b => b.severity !== 'none').length;
    const missingCount = st.beatAxis.filter(b => !b.hasGen).length;

    const cols = filteredAxis.map(b => {
        const isActive = b.seq === st.focusSeq;
        let borderCls = '';
        if (b.severity === 'failed') borderCls = 'border-failed';
        else if (b.severity === 'dirty' || b.severity === 'stale') borderCls = 'border-dirty';
        else if (b.severity === 'warned') borderCls = 'border-warned';
        else if (b.hasGen) borderCls = 'border-ok';
        else borderCls = 'border-missing';

        return `
            <div class="cv-strip-col ${isActive ? 'is-active' : ''} ${borderCls}" data-strip-seq="${b.seq}" title="第 ${b.seq} 拍 (点击直达)">
                <div class="cv-strip-header">P${b.seq}</div>
                <!-- 上行：生成帧 GEN -->
                <div class="cv-strip-cell cv-strip-gen">
                    ${b.hasGen 
                        ? `<img src="${b.genUrl}" draggable="false" alt="IMG ${b.seq}" data-cv-seq="${b.seq}" data-cv-role="gen" onerror="cvMarkBrokenImage(this)">`
                        : `<div class="cv-strip-placeholder">缺帧</div>`
                    }
                </div>
                <!-- 下行：原片抽帧 REF -->
                <div class="cv-strip-cell cv-strip-ref">
                    ${b.hasRef 
                        ? `<img src="${b.refUrl}" draggable="false" alt="REF ${b.seq}">`
                        : `<div class="cv-strip-placeholder ref-none">无原片</div>`
                    }
                </div>
            </div>
        `;
    }).join('');

    return `
        <div class="cv-filmstrip" id="cv-filmstrip">
            <div class="cv-filmstrip-toolbar">
                <span class="cv-strip-title">🎞️ 双行胶片条 (上:GEN / 下:REF)</span>
                <div class="cv-strip-filters">
                    <button type="button" class="cv-strip-filter-btn ${filter === 'all' ? 'is-active' : ''}" data-strip-filter="all">全部 (${st.beatAxis.length})</button>
                    <button type="button" class="cv-strip-filter-btn ${filter === 'flagged' ? 'is-active' : ''}" data-strip-filter="flagged">⚠ 仅看问题 (${flaggedCount})</button>
                    <button type="button" class="cv-strip-filter-btn ${filter === 'missing' ? 'is-active' : ''}" data-strip-filter="missing">⭕ 未生成 (${missingCount})</button>
                </div>
            </div>
            <div class="cv-filmstrip-track" id="cv-filmstrip-track">
                ${cols || '<div class="cv-strip-empty-hint">当前筛选下无匹配拍数</div>'}
            </div>
        </div>
    `;
}

// ── 分级重绘与状态更新函数 ──────────────────────────────────────────

/**
 * 局部更新 Compare 视口内的帧画面与审查结论，避免全量重建 DOM
 */
function updateCompareStageContent() {
    const st = collageViewerState;
    const curSeq = st.focusSeq || 1;
    const curBeat = st.beatAxis.find(b => b.seq === curSeq) || st.beatAxis[0] || { seq: curSeq };

    // 1. 更新当前拍号显示
    const seqNumEl = document.getElementById('cv-cur-seq-num');
    if (seqNumEl) seqNumEl.textContent = curSeq;

    // 2. 更新 GEN 与 REF 画面
    let urlGen = curBeat.genUrl || '';
    let urlRef = curBeat.refUrl || '';
    let labelGen = `🌟 IMG ${String(curSeq).padStart(3, '0')}`;
    let labelRef = `🎯 REF ${String(curSeq).padStart(3, '0')}`;

    if (st.compareType === 'adjacent') {
        const [seqA, seqB] = st.comparePair;
        const beatA = st.beatAxis.find(b => b.seq === seqA) || { seq: seqA };
        const beatB = st.beatAxis.find(b => b.seq === seqB) || { seq: seqB };
        urlGen = beatA.genUrl || '';
        urlRef = beatB.genUrl || '';
        labelGen = `IMG ${String(seqA).padStart(3, '0')} (前)`;
        labelRef = `IMG ${String(seqB).padStart(3, '0')} (后)`;
    }

    // SIDE 模式走的是 .cv-side-pane 双视口，没有 #cv-stage-*-layer；
    // 不单独处理的话并排模式下换拍只会动拍号，画面纹丝不动
    if (st.compareMode === 'side') {
        applySidePane(document.querySelector('.cv-side-gen'), urlGen, labelGen, 'gen', curSeq);
        applySidePane(document.querySelector('.cv-side-ref'), urlRef, labelRef, 'ref', curSeq);
        updateIssueBarAndStrip(curBeat, curSeq);
        preloadAdjacentBeats(st);
        return;
    }

    const genLayer = document.getElementById('cv-stage-gen-layer');
    if (genLayer) {
        let imgGen = genLayer.querySelector('.cv-gen-img');
        let emptyGen = genLayer.querySelector('.gen-empty');
        if (urlGen) {
            if (emptyGen) emptyGen.remove();
            if (!imgGen) {
                imgGen = document.createElement('img');
                imgGen.className = 'cv-compare-img cv-gen-img';
                imgGen.draggable = false;
                imgGen.setAttribute('onerror', 'cvMarkBrokenImage(this)');
                genLayer.prepend(imgGen);
            }
            imgGen.dataset.cvSeq = curSeq;
            imgGen.dataset.cvRole = 'gen';
            genLayer.classList.remove('cv-img-broken');
            imgGen.src = urlGen;
            imgGen.style.display = 'block';
        } else {
            if (imgGen) imgGen.style.display = 'none';
            if (!emptyGen) {
                emptyGen = document.createElement('div');
                emptyGen.className = 'cv-stage-empty gen-empty';
                emptyGen.innerHTML = `<span id="cv-gen-empty-text">第 ${curSeq} 拍尚未生成关键帧</span>`;
                genLayer.prepend(emptyGen);
            }
        }
    }

    const refLayer = document.getElementById('cv-stage-ref-layer');
    if (refLayer) {
        let imgRef = refLayer.querySelector('.cv-ref-img');
        let emptyRef = refLayer.querySelector('.ref-empty');
        if (urlRef) {
            if (emptyRef) emptyRef.remove();
            if (!imgRef) {
                imgRef = document.createElement('img');
                imgRef.className = 'cv-compare-img cv-ref-img';
                imgRef.draggable = false;
                refLayer.prepend(imgRef);
            }
            imgRef.src = urlRef;
            imgRef.style.display = 'block';
        } else {
            if (imgRef) imgRef.style.display = 'none';
            if (!emptyRef) {
                emptyRef = document.createElement('div');
                emptyRef.className = 'cv-stage-empty ref-empty';
                emptyRef.innerHTML = `<span id="cv-ref-empty-text">第 ${curSeq} 拍暂无关联原片抽帧</span>`;
                refLayer.prepend(emptyRef);
            }
        }
    }

    // 更新标签
    const genTag = document.getElementById('cv-gen-tag');
    if (genTag) genTag.textContent = labelGen;
    const refTag = document.getElementById('cv-ref-tag');
    if (refTag) refTag.textContent = labelRef;

    // 3. 更新放大镜图片
    const loupeImg = document.getElementById('cv-loupe-img');
    if (loupeImg) loupeImg.src = urlGen || urlRef || '';

    // 4-5. 审查结论带与胶片条
    updateIssueBarAndStrip(curBeat, curSeq);

    // 6. 预加载相邻拍
    preloadAdjacentBeats(st);
}

/** SIDE 模式单侧视口的画面/占位切换。 */
function applySidePane(pane, url, label, role, curSeq) {
    if (!pane) return;
    let img = pane.querySelector('.cv-stage-img');
    let empty = pane.querySelector('.cv-stage-empty');
    if (url) {
        if (empty) empty.remove();
        if (!img) {
            img = document.createElement('img');
            img.className = 'cv-stage-img';
            img.draggable = false;
            img.setAttribute('onerror', 'cvMarkBrokenImage(this)');
            pane.appendChild(img);
        }
        img.dataset.cvSeq = curSeq;
        img.dataset.cvRole = role;
        pane.classList.remove('cv-img-broken');
        img.style.display = '';
        img.src = url;
    } else {
        if (img) img.remove();
        if (!empty) {
            empty = document.createElement('div');
            empty.className = `cv-stage-empty ${role}-empty`;
            pane.appendChild(empty);
        }
        empty.innerHTML = `<span>${role === 'gen' ? `第 ${curSeq} 拍未生成` : `第 ${curSeq} 拍无原片抽帧`}</span>`;
    }
    const tag = pane.querySelector(`[data-tag-role="${role}"]`);
    if (tag) tag.textContent = label;
}

/** 结论带重绘 + 胶片条高亮/居中。两条换拍路径共用。 */
function updateIssueBarAndStrip(curBeat, curSeq) {
    const issueBar = document.getElementById('cv-issue-bar');
    if (issueBar) issueBar.innerHTML = renderIssueBarHtml(curBeat);

    const track = document.getElementById('cv-filmstrip-track');
    if (!track) return;
    let active = null;
    track.querySelectorAll('.cv-strip-col').forEach(col => {
        const isCur = parseInt(col.dataset.stripSeq, 10) === curSeq;
        col.classList.toggle('is-active', isCur);
        if (isCur) active = col;
    });
    // 只滚胶片条自己。scrollIntoView 会连带滚动所有可滚祖先，
    // 弹窗主体会跟着一起跳。
    if (active) {
        const left = active.offsetLeft - (track.clientWidth - active.offsetWidth) / 2;
        track.scrollTo({ left: Math.max(0, left), behavior: 'smooth' });
    }
}

/**
 * 局部切换对比形态
 */
function setCompareMode(mode) {
    const st = collageViewerState;
    st.compareMode = mode;
    saveViewerPrefs(st.idea && st.idea.title, { compareMode: mode });

    // 更新工具栏激活状态
    const modal = document.getElementById('collage-viewer-modal');
    if (modal) {
        modal.querySelectorAll('[data-act^="compare-mode-"]').forEach(btn => {
            btn.classList.toggle('is-active', btn.dataset.act === `compare-mode-${mode}`);
        });
    }

    // 拼图模式下不存在 compare 子树，别把画布替换掉
    if (st.mode !== 'compare') return;

    // side / fade 的 DOM 结构不同，整棵 compare 子树重建。辅助线、放大镜、
    // 审查结论带全都长在这棵树里——只重绑 slider 会让它们集体失联。
    const body = (modal || document).querySelector('#cv-main-body');
    if (!body) return;
    body.innerHTML = renderCompareViewHtml();
    const scope = modal || document;
    bindCompareSliderEvents(scope);
    bindGuidelinesAndLoupeEvents(scope);
    bindIssueBarEvents(scope);
}

/**
 * 局部更新分割比例 (纯 CSS 变量写入)
 */
function setSplitRatio(ratio) {
    const st = collageViewerState;
    st.splitRatio = Math.max(0, Math.min(100, ratio));
    const stage = document.getElementById('cv-compare-stage');
    if (stage) {
        stage.style.setProperty('--split', `${st.splitRatio}%`);
    }
    const divider = document.getElementById('cv-split-divider');
    if (divider) {
        divider.setAttribute('aria-valuenow', Math.round(st.splitRatio));
    }
}

/**
 * 局部更新渐变比例
 */
function setFadeRatio(ratio) {
    const st = collageViewerState;
    st.fadeRatio = Math.max(0, Math.min(100, ratio));
    const stage = document.getElementById('cv-compare-stage');
    if (stage) {
        stage.style.setProperty('--fade', `${st.fadeRatio / 100}`);
    }
    const slider = document.getElementById('cv-fade-slider');
    if (slider) slider.value = st.fadeRatio;
}

/**
 * 局部更新滤镜
 */
function setFilter(filter) {
    const st = collageViewerState;
    st.filter = filter;
    saveViewerPrefs(st.idea && st.idea.title, { filter });

    const modal = document.getElementById('collage-viewer-modal');
    if (modal) {
        modal.querySelectorAll('[data-act^="filter-"]').forEach(btn => {
            btn.classList.toggle('is-active', btn.dataset.act === `filter-${filter}`);
        });
        const target = modal.querySelector('.cv-image-container') || modal.querySelector('.cv-split-viewport') || modal.querySelector('.cv-dual-collage-wrap');
        if (target) {
            target.classList.remove('cv-filter-mono', 'cv-filter-contrast');
            if (filter === 'mono') target.classList.add('cv-filter-mono');
            else if (filter === 'contrast') target.classList.add('cv-filter-contrast');
        }
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
    const transformEl = document.getElementById('cv-canvas-transform') || document.getElementById('cv-stage-wrapper');
    if (transformEl) {
        transformEl.style.transform = `translate(${collageViewerState.panX}px, ${collageViewerState.panY}px) scale(${collageViewerState.zoom})`;
    }
}

/** DOM 级监听共用的 signal：每次全量重绘换一只控制器。 */
function cvDomSignal() {
    const c = collageViewerState.domAbortController;
    return c ? c.signal : undefined;
}

/**
 * window 级键盘监听。**整场只挂一次**——它绑在 st.abortController 上，
 * 而那只控制器要到关闭弹窗才 abort。若跟着 renderCollageViewerModal() 一起重挂，
 * 每切一次拼图源/开一次辅助线就会多叠一层，按一下「→」会一次性跳好几拍。
 */
function bindGlobalKeyEvents() {
    const st = collageViewerState;
    if (st.globalKeysBound) return;
    const signal = st.abortController ? st.abortController.signal : undefined;

    const onKeyDown = (e) => {
        if (!collageViewerState.open) return;
        // 如果焦点在输入框中，不拦截快捷键
        if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;

        if (e.key === 'Escape') {
            closeCollageViewer();
        } else if (e.key === '1') {
            if (collageViewerState.mode === 'compare') setCompareMode('split');
        } else if (e.key === '2') {
            if (collageViewerState.mode === 'compare') setCompareMode('fade');
        } else if (e.key === '3') {
            if (collageViewerState.mode === 'compare') setCompareMode('diff');
        } else if (e.key === '4') {
            if (collageViewerState.mode === 'compare') setCompareMode('side');
        } else if (e.code === 'Space') {
            e.preventDefault();
            if (collageViewerState.mode === 'compare' && !collageViewerState.isBlinking) {
                collageViewerState.isBlinking = true;
                const stage = document.getElementById('cv-compare-stage');
                if (stage) stage.classList.add('cv-is-blinking');
            }
        } else if (e.key === '0') {
            st.zoom = 1.0;
            st.panX = 0;
            st.panY = 0;
            updateTransform();
            setSplitRatio(50);
            setFadeRatio(50);
        } else if (e.key === 'ArrowLeft') {
            if (collageViewerState.mode === 'compare') {
                if (e.shiftKey) jumpToAdjacentFlaggedBeat(-1);
                else navigateFocusSeq(-1);
            }
        } else if (e.key === 'ArrowRight') {
            if (collageViewerState.mode === 'compare') {
                if (e.shiftKey) jumpToAdjacentFlaggedBeat(1);
                else navigateFocusSeq(1);
            }
        }
    };

    const onKeyUp = (e) => {
        if (!collageViewerState.open) return;
        if (e.code === 'Space') {
            if (collageViewerState.isBlinking) {
                collageViewerState.isBlinking = false;
                const stage = document.getElementById('cv-compare-stage');
                if (stage) stage.classList.remove('cv-is-blinking');
            }
        }
    };

    window.addEventListener('keydown', onKeyDown, { signal });
    window.addEventListener('keyup', onKeyUp, { signal });
    st.globalKeysBound = true;
}

/**
 * 弹窗内 DOM 事件绑定。每次全量重绘调用一次，开头先掐掉上一批监听——
 * DOM 节点虽然随 innerHTML 一起没了，但挂在 window / 长寿节点上的那些不会。
 */
function bindCollageViewerEvents(modal) {
    const st = collageViewerState;
    if (st.domAbortController) st.domAbortController.abort();
    st.domAbortController = new AbortController();
    const signal = st.domAbortController.signal;

    bindGlobalKeyEvents();

    // 1. 关闭按钮
    const closeBtn = modal.querySelector('.cv-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeCollageViewer, { signal });
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeCollageViewer();
    }, { signal });

    // 2. 头部工具栏点击委托
    const toolbar = modal.querySelector('.cv-toolbar');
    if (toolbar) {
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
            } else if (act === 'compare-mode-split') {
                setCompareMode('split');
            } else if (act === 'compare-mode-fade') {
                setCompareMode('fade');
            } else if (act === 'compare-mode-diff') {
                setCompareMode('diff');
            } else if (act === 'compare-mode-side') {
                setCompareMode('side');
            } else if (act === 'compare-type-adjacent') {
                st.compareType = 'adjacent';
                renderCollageViewerModal();
            } else if (act === 'compare-type-benchmark') {
                st.compareType = 'benchmark';
                renderCollageViewerModal();
            } else if (act === 'collage-tab-gen') {
                st.activeCollageTab = 'generated';
                renderCollageViewerModal();
            } else if (act === 'collage-tab-source') {
                st.activeCollageTab = 'source';
                renderCollageViewerModal();
            } else if (act === 'collage-tab-dual') {
                st.activeCollageTab = 'dual';
                renderCollageViewerModal();
            } else if (act === 'open-lightbox-current') {
                const idx = st.frames.findIndex(f => Number(f.sequence) === Number(st.focusSeq));
                openCollageFrameLightbox(idx !== -1 ? idx : 0);
            } else if (act === 'toggle-magnifier') {
                st.magnifierActive = !st.magnifierActive;
                renderCollageViewerModal();
            } else if (act === 'toggle-horizon') {
                st.guidelines.horizon = !st.guidelines.horizon;
                saveViewerPrefs(st.idea && st.idea.title, { guidelines: st.guidelines });
                renderCollageViewerModal();
            } else if (act === 'toggle-thirds') {
                st.guidelines.thirds = !st.guidelines.thirds;
                saveViewerPrefs(st.idea && st.idea.title, { guidelines: st.guidelines });
                renderCollageViewerModal();
            } else if (act === 'toggle-crosshair') {
                st.guidelines.crosshair = !st.guidelines.crosshair;
                saveViewerPrefs(st.idea && st.idea.title, { guidelines: st.guidelines });
                renderCollageViewerModal();
            } else if (act === 'filter-normal') {
                setFilter('normal');
            } else if (act === 'filter-mono') {
                setFilter('mono');
            } else if (act === 'filter-contrast') {
                setFilter('contrast');
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
        }, { signal });
    }

    if (st.mode === 'collage') {
        bindCollageCanvasEvents(modal);
    } else {
        bindCompareSliderEvents(modal);
    }

    // 绑定通用辅助线与胶片条事件
    bindGuidelinesAndLoupeEvents(modal);
    bindFilmstripEvents(modal);
    bindIssueBarEvents(modal);
}

function bindCollageCanvasEvents(modal) {
    const st = collageViewerState;
    const viewport = modal.querySelector('#cv-canvas-viewport');
    if (!viewport) return;
    const signal = cvDomSignal();

    // 滚轮缩放
    viewport.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY < 0 ? 1.12 : 0.89;
        applyZoom(st.zoom * delta);
    }, { passive: false, signal });

    // Pointer Events 画布平移
    let isPanning = false;
    let startX = 0, startY = 0;
    let downClientX = 0, downClientY = 0;

    viewport.addEventListener('pointerdown', (e) => {
        if (e.target.closest('#cv-line-horizon')) return;
        isPanning = true;
        viewport.setPointerCapture(e.pointerId);
        downClientX = e.clientX;
        downClientY = e.clientY;
        startX = e.clientX - st.panX;
        startY = e.clientY - st.panY;
        viewport.style.cursor = 'grabbing';
    }, { signal });

    viewport.addEventListener('pointermove', (e) => {
        if (!isPanning) return;
        st.panX = e.clientX - startX;
        st.panY = e.clientY - startY;
        updateTransform();
    }, { signal });

    const endPan = (e) => {
        if (isPanning) {
            isPanning = false;
            try { viewport.releasePointerCapture(e.pointerId); } catch (_) {}
            viewport.style.cursor = '';

            const dist = Math.hypot(e.clientX - downClientX, e.clientY - downClientY);
            if (dist < 6) {
                const cell = e.target.closest('[data-frame-idx]');
                if (cell && cell.dataset.frameIdx !== undefined) {
                    const idx = parseInt(cell.dataset.frameIdx, 10);
                    if (!isNaN(idx)) openCollageFrameLightbox(idx);
                }
            }
        }
    };

    viewport.addEventListener('pointerup', endPan, { signal });
    viewport.addEventListener('pointercancel', endPan, { signal });
}

function bindCompareSliderEvents(modal) {
    const st = collageViewerState;
    const signal = cvDomSignal();
    const splitViewport = modal.querySelector('#cv-split-viewport');
    const divider = modal.querySelector('#cv-split-divider');

    // 1. 顶部控制栏按钮
    modal.querySelectorAll('[data-act="prev-beat"]').forEach(b => {
        b.addEventListener('click', () => navigateFocusSeq(-1), { signal });
    });
    modal.querySelectorAll('[data-act="next-beat"]').forEach(b => {
        b.addEventListener('click', () => navigateFocusSeq(1), { signal });
    });

    // 2. 渐变滑块
    const fadeSlider = modal.querySelector('#cv-fade-slider');
    if (fadeSlider) {
        fadeSlider.addEventListener('input', (e) => {
            setFadeRatio(parseInt(e.target.value, 10));
        }, { signal });
    }

    // 3. 分割拖拽与键盘无障碍
    const stageEl = modal.querySelector('#cv-compare-stage');
    if (stageEl && st.compareMode === 'split') {
        let isSliding = false;

        // 基准必须是 stage 而不是 viewport：--split 是相对 stage 宽度的百分比，
        // 而 stage 是视口里居中的一条 9:16 窄带，两者宽度差着一大截。
        // 用 viewport 量出来的比例会让分割线跟不上光标，放大后错得更离谱。
        const updateSplitByClientX = (clientX) => {
            const rect = stageEl.getBoundingClientRect();
            if (!rect.width) return;
            setSplitRatio(((clientX - rect.left) / rect.width) * 100);
        };

        // 在整块 stage 上按下即拖。只认那条 2px 的分割线的话，
        // 手柄还是 pointer-events:none，实际可抓宽度就是两个像素。
        stageEl.addEventListener('pointerdown', (e) => {
            if (e.target.closest('#cv-line-horizon') || e.target.closest('.cv-tag-clickable')) return;
            isSliding = true;
            try { stageEl.setPointerCapture(e.pointerId); } catch (_) {}
            updateSplitByClientX(e.clientX);
        }, { signal });

        stageEl.addEventListener('pointermove', (e) => {
            if (!isSliding) return;
            updateSplitByClientX(e.clientX);
        }, { signal });

        const endSlide = (e) => {
            if (!isSliding) return;
            isSliding = false;
            try { stageEl.releasePointerCapture(e.pointerId); } catch (_) {}
            saveViewerPrefs(st.idea && st.idea.title, { splitRatio: st.splitRatio });
        };

        stageEl.addEventListener('pointerup', endSlide, { signal });
        stageEl.addEventListener('pointercancel', endSlide, { signal });
    }

    if (divider) {
        // 分割线键盘微调
        divider.addEventListener('keydown', (e) => {
            const step = e.shiftKey ? 10 : 1;
            // 焦点在分割线上时方向键归它，别再冒泡去 window 上换拍
            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                e.stopPropagation();
                setSplitRatio(st.splitRatio - step);
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                e.stopPropagation();
                setSplitRatio(st.splitRatio + step);
            } else if (e.key === 'Home') {
                e.preventDefault();
                setSplitRatio(0);
            } else if (e.key === 'End') {
                e.preventDefault();
                setSplitRatio(100);
            } else if (e.key === '0' || e.key === 'Enter') {
                e.preventDefault();
                setSplitRatio(50);
            }
        }, { signal });
    }

    // 4. 视口滚轮缩放与平移
    if (splitViewport) {
        splitViewport.addEventListener('wheel', (e) => {
            e.preventDefault();
            const delta = e.deltaY < 0 ? 1.12 : 0.89;
            applyZoom(st.zoom * delta);
        }, { passive: false, signal });
    }

    // 5. 点击标签单独放大
    modal.querySelectorAll('.cv-tag-clickable').forEach(tag => {
        tag.addEventListener('click', (e) => {
            e.stopPropagation();
            const role = tag.dataset.tagRole;
            const curBeat = st.beatAxis.find(b => b.seq === st.focusSeq) || {};
            if (role === 'gen' && curBeat.genUrl && typeof openLightbox === 'function') {
                openLightbox([{
                    type: 'image',
                    url: curBeat.genUrl,
                    caption: `<strong>第 ${curBeat.seq} 拍关键帧 (IMG ${String(curBeat.seq).padStart(3, '0')})</strong> [本次生成]`
                }], 0);
            } else if (role === 'ref' && curBeat.refUrl && typeof openLightbox === 'function') {
                openLightbox([{
                    type: 'image',
                    url: curBeat.refUrl,
                    caption: `<strong>第 ${curBeat.seq} 拍原片基准抽帧 (REF ${String(curBeat.seq).padStart(3, '0')})</strong> ${refFrameRoleLabel(refRoles, curBeat.seq)}`
                }], 0);
            }
        }, { signal });
    });

    // 保持老测试用例对 tagA/tagB 的监听契约
    const tagA = modal.querySelector('.cv-split-tag.tag-a');
    if (tagA) {
        tagA.addEventListener('click', (e) => {
            e.stopPropagation();
            const seqA = st.comparePair[0];
            const idx = st.frames.findIndex(f => Number(f.sequence) === Number(seqA));
            openCollageFrameLightbox(idx !== -1 ? idx : 0);
        }, { signal });
    }
    const tagB = modal.querySelector('.cv-split-tag.tag-b');
    if (tagB) {
        tagB.addEventListener('click', (e) => {
            e.stopPropagation();
            const seqB = st.comparePair[1];
            const idx = st.frames.findIndex(f => Number(f.sequence) === Number(seqB));
            openCollageFrameLightbox(idx !== -1 ? idx : 1);
        }, { signal });
    }
}

function bindGuidelinesAndLoupeEvents(modal) {
    const st = collageViewerState;
    const signal = cvDomSignal();
    const horizonLine = modal.querySelector('#cv-line-horizon');
    const stageOrContainer = modal.querySelector('#cv-compare-stage') || modal.querySelector('#cv-image-container');
    const magnifier = modal.querySelector('#cv-loupe-magnifier');
    const loupeImg = modal.querySelector('#cv-loupe-img');
    const viewport = modal.querySelector('#cv-split-viewport') || modal.querySelector('#cv-canvas-viewport');

    // 地平线拖拽 (Pointer Events)
    if (horizonLine && stageOrContainer) {
        let isDraggingH = false;

        horizonLine.addEventListener('pointerdown', (e) => {
            e.stopPropagation();
            isDraggingH = true;
            horizonLine.setPointerCapture(e.pointerId);
        }, { signal });

        horizonLine.addEventListener('pointermove', (e) => {
            if (!isDraggingH) return;
            const rect = stageOrContainer.getBoundingClientRect();
            let relY = ((e.clientY - rect.top) / rect.height) * 100;
            relY = Math.max(5, Math.min(95, relY));
            st.guidelines.horizonY = relY;
            horizonLine.style.top = `${relY}%`;
            const handleText = horizonLine.querySelector('.cv-line-handle');
            if (handleText) handleText.textContent = `地平线 ${Math.round(relY)}%`;
        }, { signal });

        const endDragH = (e) => {
            if (isDraggingH) {
                isDraggingH = false;
                try { horizonLine.releasePointerCapture(e.pointerId); } catch (_) {}
                saveViewerPrefs(st.idea && st.idea.title, { guidelines: st.guidelines });
            }
        };

        horizonLine.addEventListener('pointerup', endDragH, { signal });
        horizonLine.addEventListener('pointercancel', endDragH, { signal });
    }

    // 局部悬浮放大镜交互 (2.5x Loupe)
    if (st.magnifierActive && viewport && magnifier && loupeImg && stageOrContainer) {
        viewport.addEventListener('pointermove', (e) => {
            const rect = stageOrContainer.getBoundingClientRect();
            if (e.clientX >= rect.left && e.clientX <= rect.right &&
                e.clientY >= rect.top && e.clientY <= rect.bottom) {
                magnifier.style.display = 'block';
                const loupeSize = 180;
                magnifier.style.left = `${e.clientX - loupeSize / 2}px`;
                magnifier.style.top = `${e.clientY - loupeSize / 2}px`;

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
        }, { signal });

        viewport.addEventListener('pointerleave', () => {
            magnifier.style.display = 'none';
        }, { signal });
    }
}

function bindFilmstripEvents(modal) {
    const st = collageViewerState;
    const signal = cvDomSignal();
    const filmstrip = modal.querySelector('#cv-filmstrip');
    if (!filmstrip) return;

    filmstrip.addEventListener('click', (e) => {
        // 筛选按钮
        const filterBtn = e.target.closest('[data-strip-filter]');
        if (filterBtn) {
            st.stripFilter = filterBtn.dataset.stripFilter;
            const footer = modal.querySelector('#cv-footer');
            if (footer) {
                footer.innerHTML = `
                    ${renderFilmstripHtml()}
                    <div class="cv-footer-bottom">
                        <div class="cv-footer-hints">
                            💡 提示：<b>长按 [空格]</b> 瞬时闪烁原片基准；<b>[1-4]</b> 切换形态；<b>[← / →]</b> 换拍；<b>[Shift + ←/→]</b> 跳至问题拍；<b>[0]</b> 居中/复位。
                        </div>
                        <div class="cv-footer-stats">
                            共 <b>${st.beatAxis.length}</b> 拍 · ${Object.keys(st.refFrames).length ? `<span style="color:#f59e0b;">已绑定 ${Object.keys(st.refFrames).length} 拍原片抽帧</span>` : '未绑定原片抽帧'}
                        </div>
                    </div>
                `;
                bindFilmstripEvents(modal);
            }
            return;
        }

        // 拍数卡片点击
        const col = e.target.closest('[data-strip-seq]');
        if (col) {
            const seq = parseInt(col.dataset.stripSeq, 10);
            if (!isNaN(seq)) {
                st.focusSeq = seq;
                st.benchmarkFrameSeq = seq;
                updateCompareStageContent();
            }
        }
    }, { signal });
}

function bindIssueBarEvents(modal) {
    const st = collageViewerState;
    const signal = cvDomSignal();
    const issueBar = modal.querySelector('#cv-issue-bar');
    if (!issueBar) return;

    issueBar.addEventListener('click', async (e) => {
        const fixBtn = e.target.closest('[data-act="fix-current-beat"]');
        const retryBtn = e.target.closest('[data-act="retry-current-beat"]');
        const genBtn = e.target.closest('[data-act="gen-current-beat"]');

        const curSeq = st.focusSeq;

        if (fixBtn) {
            fixBtn.disabled = true;
            fixBtn.textContent = '修复中...';
            if (typeof fixFrameIssue === 'function') {
                try {
                    await fixFrameIssue(curSeq);
                    if (typeof showToast === 'function') showToast(`第 ${curSeq} 拍修复指令已下发`, 'success');
                } catch (err) {
                    if (typeof showToast === 'function') showToast(`修复失败: ${err.message || err}`, 'error');
                }
            } else if (typeof showToast === 'function') {
                showToast(`已触发第 ${curSeq} 拍修复`, 'info');
            }
            fixBtn.disabled = false;
            fixBtn.textContent = '🛠 修复此拍';
        } else if (retryBtn || genBtn) {
            const btn = retryBtn || genBtn;
            btn.disabled = true;
            btn.textContent = '请求中...';
            if (typeof retrySingleFrame === 'function') {
                try {
                    await retrySingleFrame(curSeq);
                    if (typeof showToast === 'function') showToast(`第 ${curSeq} 拍生成任务已启动`, 'success');
                } catch (err) {
                    if (typeof showToast === 'function') showToast(`请求失败: ${err.message || err}`, 'error');
                }
            } else if (typeof showToast === 'function') {
                showToast(`已触发第 ${curSeq} 拍生成`, 'info');
            }
            btn.disabled = false;
            btn.textContent = retryBtn ? '⚡ 重渲此拍' : '✨ 生成此拍';
        }
    }, { signal });
}

// ── 导航与辅助方法 ──────────────────────────────────────────────────

function navigateFocusSeq(delta) {
    const st = collageViewerState;
    const axis = st.beatAxis;
    if (!axis || !axis.length) return;

    let curIdx = axis.findIndex(b => b.seq === st.focusSeq);
    if (curIdx === -1) curIdx = 0;

    let newIdx = curIdx + delta;
    if (newIdx < 0) newIdx = 0;
    if (newIdx > axis.length - 1) newIdx = axis.length - 1;

    st.focusSeq = axis[newIdx].seq;
    st.benchmarkFrameSeq = st.focusSeq;
    st.comparePair = [
        st.focusSeq > 1 ? st.focusSeq - 1 : 1,
        st.focusSeq > 1 ? st.focusSeq : 2
    ];

    updateCompareStageContent();
}

function jumpToAdjacentFlaggedBeat(direction = 1) {
    const st = collageViewerState;
    const axis = st.beatAxis;
    if (!axis || !axis.length) return;

    let curIdx = axis.findIndex(b => b.seq === st.focusSeq);
    if (curIdx === -1) curIdx = 0;

    let targetIdx = -1;
    if (direction > 0) {
        for (let i = curIdx + 1; i < axis.length; i++) {
            if (axis[i].severity !== 'none' || !axis[i].hasGen) {
                targetIdx = i;
                break;
            }
        }
    } else {
        for (let i = curIdx - 1; i >= 0; i--) {
            if (axis[i].severity !== 'none' || !axis[i].hasGen) {
                targetIdx = i;
                break;
            }
        }
    }

    if (targetIdx !== -1) {
        st.focusSeq = axis[targetIdx].seq;
        st.benchmarkFrameSeq = st.focusSeq;
        updateCompareStageContent();
        if (typeof showToast === 'function') {
            showToast(`已跳转至第 ${st.focusSeq} 拍（${axis[targetIdx].issueReason || '待关注拍'}）`, 'info');
        }
    } else if (typeof showToast === 'function') {
        showToast(direction > 0 ? '后续无待关注拍' : '前面无待关注拍', 'info');
    }
}

// 兼容老代码的导出方法
function navigateBenchmarkSeq(delta) {
    navigateFocusSeq(delta);
}

function navigateComparePair(delta) {
    const st = collageViewerState;
    const axis = st.beatAxis;
    if (!axis || axis.length < 2) return;

    let curIdx = axis.findIndex(b => b.seq === st.comparePair[0]);
    if (curIdx === -1) curIdx = 0;

    let newIdx = curIdx + delta;
    if (newIdx < 0) newIdx = 0;
    if (newIdx > axis.length - 2) newIdx = axis.length - 2;

    st.comparePair = [axis[newIdx].seq, axis[newIdx + 1].seq];
    st.focusSeq = axis[newIdx].seq;
    updateCompareStageContent();
}

// Node 单测与模块化导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        openCollageViewer,
        openBenchmarkCompare,
        closeCollageViewer,
        openCollageFrameLightbox,
        collageViewerState,
        buildBeatAxis,
        navigateBenchmarkSeq,
        navigateComparePair,
    };
}
