/* =====================================================================
   SPARK Creative Idea Generator - Core Javascript Logic
   ===================================================================== */

// 全局共享状态(DEFAULT_CONFIG / config / ACCESS_CODE / savedIdeas / currentIdea /
// generationState / streamEpochs / controllers 等)已抽出到 js/state.js(最先加载)。
// 本文件仅保留其初始化逻辑与运行时赋值(下方 fetch 包装器、initServerMode 等)。

// HTML Escape Utility to prevent XSS / render breakage
// Function escapeHtml moved to modular JS file

// ---- External (server-managed) mode: access code + automatic header injection ----
// All /api/* requests transparently carry the access code (if one is set). If the server
// rejects it (401) we prompt for a new one and retry once. Implemented by wrapping window.fetch
// so every existing call site is covered without changes.

// Function _setAccessHeader moved to modular JS file

(function patchFetchForAccessCode() {
    const _origFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        init = init ? { ...init } : {};
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        const isApi = url.indexOf('/api/') !== -1;
        if (isApi && ACCESS_CODE) _setAccessHeader(init, ACCESS_CODE);
        let resp = await _origFetch(input, init);
        if (isApi && resp.status === 401) {
            const entered = window.prompt('需要访问码才能使用本服务，请输入：', '');
            if (entered) {
                ACCESS_CODE = entered.trim();
                localStorage.setItem('spark_access_code', ACCESS_CODE);
                _setAccessHeader(init, ACCESS_CODE);
                resp = await _origFetch(input, init);
            }
        }
        return resp;
    };
})();

// On load, ask the server whether it is managed; if so hide the API 配置中心 (end users must
// not see/edit the server key) and pre-prompt for the access code.
async function initServerMode() {
    try {
        const m = await fetch('/api/mode').then(r => r.json());
        if (m && m.server_managed) {
            // Keep the settings button (gear button) permanently visible as requested by the user
            // const btn = document.getElementById('open-settings-btn');
            // if (btn) btn.style.display = 'none';
            if (m.needs_access_code && !ACCESS_CODE) {
                const entered = window.prompt('需要访问码才能使用本服务，请输入：', '');
                if (entered) {
                    ACCESS_CODE = entered.trim();
                    localStorage.setItem('spark_access_code', ACCESS_CODE);
                }
            }
        }
    } catch (e) {
        console.warn('mode check failed', e);
    }
}
document.addEventListener('DOMContentLoaded', initServerMode);

// Safe Clipboard Copy Helper with HTTP LAN Fallback
// Function copyText moved to modular JS file

// Custom Styled Dialog Prompt Modal (replacing window.prompt)
// Function customPrompt moved to modular JS file

// Custom Styled Confirm Modal (replacing window.confirm)
// Function customConfirm moved to modular JS file


// Initialize Elements
document.addEventListener('DOMContentLoaded', () => {
    // ── Minimal debounce utility (avoids lodash dep) ──
    window._debounce = function(fn, delay) {
        let t;
        return function(...args) {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), delay);
        };
    };

    loadConfig();
    loadLibrary();
    loadCustomPresets();
    initSliders();
    initSelectors();
    loadSelectionState();
    loadCurrentIdeaState();
    initCanvas();
    checkApiStatus();
    setupEventListeners();
    setupDragAndDrop();
    resumeActiveTaskIfExists();
    resumeActiveBackgroundTasksIfExists();
    startGlobalTasksBadgePolling();
    loadIdeationCards();
    updateDrawerTopOffset();
    window.addEventListener('resize', window._debounce(updateDrawerTopOffset, 150));
    const refreshBtn = document.getElementById('ideate-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            loadIdeationCards(true);
        });
    }
    initLocalServiceLogs();
});

// Function saveSelectionState moved to modular JS file

// Function loadSelectionState moved to modular JS file

// Function updateConfigSummary moved to modular JS file

const PRESETS = {
    nature_wonder: {
        theme: 'glacier_cave',
        anchors: ['water_glass_floor', 'bioluminescent_moss'],
        complexity: 2,
        budget: 2,
        ratio: 30,
        creativity: 3,
        beats: 5
    },
    industrial_relic: {
        theme: 'water_tower',
        anchors: ['single_slab_counter', 'living_wood_stair'],
        complexity: 3,
        budget: 2,
        ratio: 60,
        creativity: 2,
        beats: 8
    },
    retired_vehicle: {
        theme: 'submarine_cabin',
        anchors: ['carrier_cutout_window', 'single_slab_counter'],
        complexity: 3,
        budget: 3,
        ratio: 75,
        creativity: 3,
        beats: 10
    },
    contrast_novelty: {
        theme: 'hollow_oak',
        anchors: ['bark_camouflaged_hatch', 'rerouted_waterfall_shower'],
        complexity: 2,
        budget: 1,
        ratio: 50,
        creativity: 3,
        beats: 5
    }
};

// Function applyPreset moved to modular JS file

// Function safeSetImageSrc moved to modular JS file

// Function parsePromptBlock moved to modular JS file

// Function renderParsedPrompts moved to modular JS file

// Top-level workspace switch: exclusive single-panel view (config / results / image studio),
// used at every screen size. 'left'/'right' are accepted as aliases of 'config'/'results' for
// backward compatibility with any inline handlers still spelled the old way.
function switchMainTab(tabName) {
    const aliases = { left: 'config', right: 'results' };
    const tab = aliases[tabName] || tabName;

    const panels = {
        config: document.querySelector('.panel-left'),
        results: document.querySelector('.panel-right'),
        image: document.getElementById('panel-image-studio'),
        gallery: document.getElementById('panel-gallery'),
        ledger: document.getElementById('panel-ledger'),
    };
    const buttons = {
        config: document.getElementById('main-tab-config'),
        results: document.getElementById('main-tab-results'),
        image: document.getElementById('main-tab-image'),
        gallery: document.getElementById('main-tab-gallery'),
        ledger: document.getElementById('main-tab-ledger'),
    };

    Object.keys(panels).forEach((key) => {
        if (panels[key]) panels[key].classList.toggle('mobile-active', key === tab);
        if (buttons[key]) buttons[key].classList.toggle('active', key === tab);
    });

    // 图像工坊现与"创意工坊"同级挂在顶部 app-switcher 里，两者共享同一高亮态：
    // 切到 image 时把"创意工坊"熄灭，切回其余任一子标签时把它点亮。
    const workshopSwitcher = document.getElementById('switcher-workshop-btn');
    if (workshopSwitcher) workshopSwitcher.classList.toggle('active', tab !== 'image');

    // 画廊首次进入时才扫描本地文件（js/gallery.js 提供；懒加载避免拖慢启动）
    if (tab === 'gallery' && typeof galleryTabEntered === 'function') {
        galleryTabEntered();
    }
    // 创意台账：每次进入都重新拉取（体量小、纯 JSON 读取，代价远低于画廊的文件系统扫描），
    // 这样"存入备选"后立刻切回台账页也能看到最新数据，无需手动点刷新
    if (tab === 'ledger' && typeof ledgerTabEntered === 'function') {
        ledgerTabEntered();
    }
}

function switchTab(tabId) {
    localStorage.setItem('spark_active_tab', tabId);
    document.querySelectorAll('.result-tabs-bar .tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabId) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    document.querySelectorAll('.content-scroll-area .tab-panel').forEach(panel => {
        if (panel.id === `tab-panel-${tabId}`) {
            panel.classList.add('active');
        } else {
            panel.classList.remove('active');
        }
    });
}

function showDeleteConfirm(card, ideaId) {
    document.querySelectorAll('.delete-confirm-overlay').forEach(overlay => overlay.remove());
    
    const overlay = document.createElement('div');
    overlay.className = 'delete-confirm-overlay';
    overlay.innerHTML = `
        <span class="delete-confirm-text">确定删除此点子？</span>
        <button class="delete-confirm-btn yes">删除</button>
        <button class="delete-confirm-btn no">取消</button>
    `;
    
    overlay.querySelector('.yes').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteFromLibrary(ideaId);
    });
    
    overlay.querySelector('.no').addEventListener('click', (e) => {
        e.stopPropagation();
        overlay.remove();
    });
    
    overlay.addEventListener('click', (e) => {
        e.stopPropagation();
    });
    
    card.appendChild(overlay);
}

function updateFavoriteButtonState() {
    const saveBtn = document.getElementById('save-idea-btn');
    const saveBtnText = document.getElementById('save-idea-btn-text');
    if (!saveBtn || !currentIdea) return;
    
    const isSaved = savedIdeas.some(item => item.title === currentIdea.title);
    if (isSaved) {
        saveBtn.classList.add('favorited');
        if (saveBtnText) saveBtnText.textContent = '已收藏点子';
    } else {
        saveBtn.classList.remove('favorited');
        if (saveBtnText) saveBtnText.textContent = '收藏点子';
    }
}

let _scrollPending = false;
function appendLiveTerminal(chunk) {
    const body = document.getElementById('live-terminal-body');
    if (!body) return;
    const cursor = body.querySelector('.terminal-cursor');
    if (cursor) {
        const textNode = document.createTextNode(chunk);
        body.insertBefore(textNode, cursor);
    } else {
        body.textContent += chunk;
    }
    // Throttle auto-scroll with rAF to avoid forced layout on every chunk
    if (!_scrollPending) {
        _scrollPending = true;
        requestAnimationFrame(() => {
            body.scrollTop = body.scrollHeight;
            _scrollPending = false;
        });
    }
}

function startLoadingTimer(startTimeOverride = null) {
    clearInterval(generationState.timerInterval);
    const start = startTimeOverride ? startTimeOverride : Date.now();
    generationState.startTime = start;
    
    const timerVal = document.getElementById('loading-timer-val');
    
    ['step-1', 'step-2', 'step-3', 'step-4'].forEach(id => {
        updateLoadingStep(id, 'pending');
    });
    
    const terminalBody = document.getElementById('live-terminal-body');
    if (terminalBody) {
        terminalBody.innerHTML = '<span class="terminal-cursor"></span>';
        appendLiveTerminal("[SYSTEM] Initializing creative idea engine...\n[SYSTEM] Loading restoration-prompt-composer contract...\n");
    }

    clearLiveBeatsPanel();

    generationState.timerInterval = setInterval(() => {
        const elapsed = Date.now() - start;
        if (timerVal) {
            timerVal.textContent = (elapsed / 1000).toFixed(1);
        }
    }, 100);
}

function updateProgressUI(prog) {
    const stageText = document.getElementById('loading-stage-text');
    if (!stageText) return;

    if (prog.stage === 'outline') {
        stageText.textContent = prog.details || "正在解析场景维度与提取关键要素 (第 1 阶段)...";
        updateLoadingStep('step-1', 'active');
    } else if (prog.stage === 'batch') {
        const current = prog.details.current;
        const total = prog.details.total;
        stageText.textContent = `正在调用 LLM 合成多步施工节拍提示词 (第 2 阶段：批次 ${current}/${total})...`;
        updateLoadingStep('step-1', 'completed');
        updateLoadingStep('step-2', 'active');
    } else if (prog.stage === 'audit') {
        stageText.textContent = prog.details || "提示词已合成完毕，正在运行工序与场景一致性二次校验与修复分析 (第 3 阶段)...";
        updateLoadingStep('step-2', 'completed');
        updateLoadingStep('step-3', 'active');
    } else if (prog.stage === 'repair') {
        stageText.textContent = prog.details || "工序一致性校验返回有细微瑕疵，正在进行一致性修复以保时序因果 (第 4 阶段)...";
        updateLoadingStep('step-3', 'completed');
        updateLoadingStep('step-4', 'active');
    } else if (prog.stage === 'keyframe_extraction') {
        stageText.textContent = prog.details || "正在提取视频关键帧 (第 1 阶段)...";
        updateLoadingStep('step-1', 'active');
    } else if (prog.stage === 'cv_analysis') {
        stageText.textContent = prog.details || "正在使用计算机视觉算法分析运动与光照变化 (第 2 阶段)...";
        updateLoadingStep('step-1', 'completed');
        updateLoadingStep('step-2', 'active');
    } else if (prog.stage === 'semantic_metadata') {
        stageText.textContent = prog.details || "大模型多模态视频分析与时序语义提取中 (第 3 阶段)...";
        updateLoadingStep('step-2', 'completed');
        updateLoadingStep('step-3', 'active');
    } else if (prog.stage === 'prompt_composition') {
        stageText.textContent = prog.details || "正在合成 SCUP 契约提示词并进行物理一致性审计 (第 4 阶段)...";
        updateLoadingStep('step-3', 'completed');
        updateLoadingStep('step-4', 'active');
    }
}

function stopLoadingTimer() {
    if (generationState.timerInterval) {
        clearInterval(generationState.timerInterval);
        generationState.timerInterval = null;
    }
}

// Clears the loading screen's progressive-reveal live prompt panel. Runs before every
// new generation and before the final renderIdea(result) so stale live content never
// lingers behind the finished result view.
function clearLiveBeatsPanel() {
    const block = document.getElementById('live-beats-block');
    if (block) block.textContent = '';
    const panel = document.getElementById('live-beats-panel');
    if (panel) {
        panel.style.display = 'none';
        panel.classList.remove('revising');
    }
    const countEl = document.getElementById('live-beats-count');
    if (countEl) countEl.textContent = '0/0';
}

// Progressive per-beat reveal: as each beat's VIDEO/IMAGE prompt pair finishes on the
// backend (on_progress('beat_ready', ...)), show the growing prompt_block right away in
// the loading screen's live panel — mirroring how the final result view renders the raw
// markdown block (#idea-prompt-block) — instead of making the user wait for the whole
// 16-beat pipeline (+ audit pass) to show anything. Separate from updateProgressUI,
// which only drives the stage text / step dots.
function handleComposeProgressExtras(prog) {
    if (!prog || typeof prog !== 'object') return;
    const d = prog.details || {};

    if (prog.stage === 'beat_ready') {
        const block = document.getElementById('live-beats-block');
        if (block) {
            block.textContent = d.prompt_block || '';
            block.scrollTop = block.scrollHeight;
        }

        const panel = document.getElementById('live-beats-panel');
        if (panel) {
            panel.style.display = 'flex';
            panel.classList.remove('revising');
        }
        const countEl = document.getElementById('live-beats-count');
        if (countEl) countEl.textContent = `${d.index || 0}/${d.total || 0}`;

        if (d.index === 0) {
            appendLiveTerminal(`[BEAT] 起始画面 IMAGE 1 已生成。\n`);
        } else if (d.is_revision) {
            appendLiveTerminal(`[BEAT] 第 ${d.index} 拍已按审核意见重新生成完毕。\n`);
        } else {
            appendLiveTerminal(`[BEAT] 第 ${d.index} 拍提示词已生成 (${d.index}/${d.total})。\n`);
        }
    } else if (prog.stage === 'beat_revising') {
        const indices = d.indices || [];
        const panel = document.getElementById('live-beats-panel');
        if (panel && indices.length) panel.classList.add('revising');
        if (indices.length) {
            appendLiveTerminal(`[BEAT] 质检未通过，正在重新生成第 ${indices.join('、')} 拍...\n`);
        }
    }
}



// Mapping of image models by main model
// IMAGE_MODELS_BY_MAIN_MODEL 已弃用（2026-07-12 生图模型与 LLM 解耦）：
// 生图模型清单见 js/state.js 的 IMAGE_MODELS / FX_IMAGE_MODELS。
// updateImageModelOptions 已删除（配置中心模型下拉整体移除，见 js/config.js 内嵌选择器）

// Load configuration from localStorage
// Function loadConfig moved to modular JS file

// Helper to show/hide GPT port selector based on model selection
// Function updateGptPortVisibility moved to modular JS file

// Save configuration to localStorage
// Function saveConfig moved to modular JS file

// Reset configuration to default
// Function resetConfig moved to modular JS file

// Update the displayed cover generation parameters in the page description
// Function updateCoverModelDisplay moved to modular JS file

// Save the currently active idea state to localStorage so it survives page refresh
// Function saveCurrentIdeaState moved to modular JS file

// Restore the last viewed idea from localStorage on page load
// Function loadCurrentIdeaState moved to modular JS file

// Load saved ideas library from API or localStorage fallback
async function loadLibrary() {
    try {
        const response = await fetch('/api/library');
        if (response.ok) {
            savedIdeas = await response.json();
            console.log("Successfully loaded library from local server file.");
            // 2026-07-12 整库清零事故防线：服务器返回“合法空库”但本地备份非空时，
            // 大概率是服务器文件被状态错乱的客户端清掉了（或刚发生过回滚）——采用
            // 本地备份并提示，绝不能让空结果静默吞掉最后一份幸存副本。不自动回写
            // 服务器：用户下一次正常保存动作会自然把恢复的库写回去。
            if (Array.isArray(savedIdeas) && savedIdeas.length === 0) {
                const stored = localStorage.getItem('spark_library');
                if (stored) {
                    try {
                        const backup = JSON.parse(stored);
                        if (Array.isArray(backup) && backup.length > 0) {
                            savedIdeas = backup;
                            console.warn(`Server library is empty but localStorage backup has ${backup.length} ideas — using the backup.`);
                            if (typeof showToast === 'function') {
                                showToast(`服务器创意库为空，已从本地备份恢复 ${backup.length} 条创意（保存任意改动即回写服务器）`, 'error');
                            }
                        }
                    } catch (err) {
                        console.error("Failed to parse localStorage library backup", err);
                    }
                }
            }
        } else {
            throw new Error(`Server returned HTTP ${response.status}`);
        }
    } catch (e) {
        console.warn("Failed to load library from server, falling back to localStorage", e);
        const stored = localStorage.getItem('spark_library');
        if (stored) {
            try {
                savedIdeas = JSON.parse(stored);
            } catch (err) {
                console.error("Failed to parse localStorage library", err);
            }
        }
    }
    renderLibrary();
}

// Save library to both API (server file) and localStorage (browser backup)
async function saveLibrary() {
    // 1. Always write to localStorage as a fallback backup
    localStorage.setItem('spark_library', JSON.stringify(savedIdeas));
    
    // 2. Attempt saving to server file database
    try {
        const response = await fetch('/api/library', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(savedIdeas)
        });
        if (!response.ok) {
            throw new Error(`Server returned HTTP ${response.status}`);
        }
        console.log("Successfully persisted library to local server file.");
    } catch (e) {
        console.warn("Failed to persist library to server, backed up in localStorage only", e);
    }
    
    renderLibrary();
}

// Check API status via the server-side ping (the server reaches the local proxy
// without the browser hitting CORS, and bypasses the Windows system proxy).
async function checkApiStatus() {
    const badge = document.getElementById('api-status-badge');
    badge.className = 'status-badge checking';
    badge.querySelector('.status-text').textContent = '检测 API 连接中...';

    try {
        const res = await fetch('/api/ping', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });
        const data = await res.json();

        if (res.ok && data.online) {
            badge.className = 'status-badge online';
            badge.querySelector('.status-text').textContent = `本地 ${config.model} 在线`;
            return true;
        } else {
            throw new Error(data.message || `HTTP Error ${res.status}`);
        }
    } catch (e) {
        badge.className = 'status-badge offline';
        badge.querySelector('.status-text').textContent = 'API 连接断开';
        console.error("API check failed:", e);
        return false;
    }
}

// Interactive Sliders Initialization
function initSliders() {
    const complexity = document.getElementById('slider-complexity');
    const budget = document.getElementById('slider-budget');
    const ratio = document.getElementById('slider-ratio');
    const creativity = document.getElementById('slider-creativity');
    const beats = document.getElementById('slider-beats');

    const complexityLabels = { 1: '轻量级改造', 2: '中等重工', 3: '硬核结构性改建' };
    const budgetLabels = { 1: '平民精简版', 2: '轻奢设计师级', 3: '顶奢艺术级定制' };
    // 注意：这些标签文字会作为 Creativity Scale 原样送进 LLM（generateIdea 直接取
    // val-creativity 的 textContent）——措辞必须保持写实取向，绝不能出现「科幻」类
    // 引导词（旧文案「脑洞大开 (极致科幻)」曾把整条产出带偏成科幻题材）。
    const creativityLabels = { 1: '常规务实', 2: '突破常规', 3: '脑洞大开 (写实奇观)' };

    // 反差强度/节拍数轨道上色到当前值，让拖动时能直接看到进度而不是只有上方的静态文字
    const updateFill = (input) => {
        const min = Number(input.min) || 0;
        const max = Number(input.max) || 100;
        const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
        input.style.setProperty('--fill-pct', `${pct}%`);
    };

    // 复杂度/预算/脑洞大开度只有 3 档，拖滑块去精确命中某一档很别扭，改成点选式分段按钮。
    // 底层 <input type=range> 保留（视觉隐藏）：config.js / prompt_pipeline.js 里大量代码
    // 按 id 直接读写它的 .value，分段按钮只是换了一层交互，不改数据模型。
    const fillSegmentLabels = (targetId, labels) => {
        const group = document.querySelector(`.segmented-control[data-target="${targetId}"]`);
        if (!group) return;
        group.querySelectorAll('.segment-btn').forEach((btn) => {
            btn.textContent = labels[btn.dataset.value] || btn.dataset.value;
        });
    };
    fillSegmentLabels('slider-complexity', complexityLabels);
    fillSegmentLabels('slider-budget', budgetLabels);
    fillSegmentLabels('slider-creativity', creativityLabels);

    const syncSegments = (input) => {
        const group = document.querySelector(`.segmented-control[data-target="${input.id}"]`);
        if (!group) return;
        group.querySelectorAll('.segment-btn').forEach((btn) => {
            const isActive = btn.dataset.value === String(input.value);
            btn.classList.toggle('active', isActive);
            btn.setAttribute('aria-pressed', String(isActive));
        });
    };

    document.querySelectorAll('.segmented-control').forEach((group) => {
        const input = document.getElementById(group.dataset.target);
        if (!input) return;
        group.querySelectorAll('.segment-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                input.value = btn.dataset.value;
                // 'input' 驱动即时的标签/摘要更新；'change' 是 saveSelectionState() 落盘到
                // localStorage 唯一挂钩的事件——原生滑块靠"松手"触发它，这里补发使其等效。
                input.dispatchEvent(new Event('input'));
                input.dispatchEvent(new Event('change'));
            });
        });
    });

    // 反差强度/节拍数两侧的 −/+：不想拖也能单步精调
    document.querySelectorAll('.slider-step-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            const input = document.getElementById(btn.dataset.target);
            if (!input) return;
            const step = Number(input.step) || 1;
            const min = Number(input.min);
            const max = Number(input.max);
            const next = Number(input.value) + Number(btn.dataset.step) * step;
            input.value = Math.min(max, Math.max(min, next));
            input.dispatchEvent(new Event('input'));
            input.dispatchEvent(new Event('change'));
        });
    });

    complexity.addEventListener('input', (e) => {
        document.getElementById('val-complexity').textContent = complexityLabels[e.target.value];
        syncSegments(e.target);
    });
    budget.addEventListener('input', (e) => {
        document.getElementById('val-budget').textContent = budgetLabels[e.target.value];
        syncSegments(e.target);
    });
    ratio.addEventListener('input', (e) => {
        const val = e.target.value;
        document.getElementById('val-ratio').textContent = `反差强度: ${val}%`;
        updateFill(e.target);
    });
    creativity.addEventListener('input', (e) => {
        document.getElementById('val-creativity').textContent = creativityLabels[e.target.value];
        syncSegments(e.target);
    });
    beats.addEventListener('input', (e) => {
        document.getElementById('val-beats').textContent = `${e.target.value} 拍`;
        updateFill(e.target);
    });

    // Fire initial displays
    complexity.dispatchEvent(new Event('input'));
    budget.dispatchEvent(new Event('input'));
    ratio.dispatchEvent(new Event('input'));
    creativity.dispatchEvent(new Event('input'));
    beats.dispatchEvent(new Event('input'));
}

// Theme & Anchor Selection Handling
function initSelectors() {
    // 基础场景主题选择器已从 GUI 移除（灵感改由联网参考案例库驱动，见
    // js/trend_refs.js）；#theme-selector 不复存在，这里不再绑定它的监听。

    // Anchor Selector (Multi-select) — section removed from the GUI; guard kept since
    // #anchor-selector no longer exists (anchors are now left for the composer to pick).
    const anchorFlex = document.getElementById('anchor-selector');
    if (anchorFlex) {
        anchorFlex.addEventListener('click', (e) => {
            const btn = e.target.closest('.anchor-node');
            if (!btn) return;

            btn.classList.toggle('active');
        });
    }
}

// Interactive Particle Background (Canvas)
function initCanvas() {
    const canvas = document.getElementById('particle-canvas');
    if (!canvas) return;
    // The particle field is disabled via CSS (#particle-canvas { display:none }). Bail before
    // starting a perpetual requestAnimationFrame loop that would read canvas.offsetParent every
    // frame — a forced-layout trigger that can cost a synchronous reflow during interactions —
    // just to early-return and draw nothing. If the field is re-enabled in CSS, this runs again.
    if (getComputedStyle(canvas).display === 'none') return;
    const ctx = canvas.getContext('2d');
    
    // Check prefers-reduced-motion
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReduced) {
        if (canvas) canvas.style.display = 'none';
        return;
    }
    
    let particles = [];
    const maxParticles = 60;
    
    function resize() {
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
    }
    let resizeTimer;
    // passive:true — browser does NOT need to wait for event handler before scrolling
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resize, 150);
    }, { passive: true });
    resize();
    
    class Particle {
        constructor() {
            this.reset();
        }
        
        reset() {
            this.x = Math.random() * canvas.width;
            this.y = Math.random() * canvas.height;
            this.size = Math.random() * 2 + 1;
            this.vx = Math.random() * 0.4 - 0.2;
            this.vy = Math.random() * 0.4 - 0.2;
            this.color = Math.random() > 0.5 ? 'rgba(0, 242, 254, 0.15)' : 'rgba(157, 78, 221, 0.15)';
        }
        
        update() {
            this.x += this.vx;
            this.y += this.vy;
            
            if (this.x < 0 || this.x > canvas.width || this.y < 0 || this.y > canvas.height) {
                this.reset();
            }
        }
        
        draw() {
            ctx.fillStyle = this.color;
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
        }
    }
    
    for (let i = 0; i < maxParticles; i++) {
        particles.push(new Particle());
    }
    
    // ── FPS cap at 30fps to cut GPU load in half without visual regression ──
    const TARGET_INTERVAL = 1000 / 30; // ~33ms between frames
    let lastFrameTime = 0;

    function animate(timestamp) {
        requestAnimationFrame(animate);

        // Skip all drawing while the tab is hidden or the canvas is display:none
        if (document.hidden || canvas.offsetParent === null) return;

        // Throttle: skip frame if not enough time has passed
        const delta = timestamp - lastFrameTime;
        if (delta < TARGET_INTERVAL) return;
        lastFrameTime = timestamp - (delta % TARGET_INTERVAL);

        ctx.clearRect(0, 0, canvas.width, canvas.height);

        particles.forEach(p => {
            p.update();
            p.draw();
        });
    }

    requestAnimationFrame(animate);
}

// Fetch and update cache size info in settings modal
async function updateCacheSizeInfo() {
    const cacheInfoSpan = document.getElementById('cache-size-info');
    if (!cacheInfoSpan) return;
    cacheInfoSpan.textContent = '计算中...';
    try {
        const resp = await fetch('/api/cache-info');
        if (resp.ok) {
            const data = await resp.json();
            const sizeKb = (data.packet_cache_size / 1024).toFixed(2);
            cacheInfoSpan.textContent = `${sizeKb} KB (${data.packet_cache_keys}个项目)`;
        } else {
            cacheInfoSpan.textContent = '获取失败';
        }
    } catch (e) {
        cacheInfoSpan.textContent = '获取失败';
    }
}

// --- Persistent panel (tasks/library) helpers -----------------------------
// Both drawers stay open once opened (no auto-close on outside click) so they
// can be used as a reference alongside the rest of the workspace; the header
// toggle button doubles as the "收起" (collapse) control, its label swapping
// to make that discoverable.
const DRAWER_TOGGLE_LABELS = {
    'toggle-tasks-btn': '任务列表',
    'toggle-library-btn': '点子库',
};

function setDrawerToggleOpenState(btnId, isOpen) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const label = DRAWER_TOGGLE_LABELS[btnId] || '';
    const labelSpan = btn.querySelector('span:not(.task-badge)');
    btn.classList.toggle('panel-open', isOpen);
    btn.title = isOpen ? `收起${label}` : label;
    if (labelSpan) labelSpan.textContent = isOpen ? '收起' : label;
}

function openLibraryDrawer() {
    const libraryDrawer = document.getElementById('library-drawer');
    if (!libraryDrawer) return;
    closeTasksDrawer();
    libraryDrawer.classList.add('active');
    setDrawerToggleOpenState('toggle-library-btn', true);
    renderLibrary();
}

function closeLibraryDrawer() {
    const libraryDrawer = document.getElementById('library-drawer');
    if (!libraryDrawer) return;
    libraryDrawer.classList.remove('active');
    setDrawerToggleOpenState('toggle-library-btn', false);
}

function openTasksDrawer() {
    const tasksDrawer = document.getElementById('tasks-drawer');
    if (!tasksDrawer) return;
    closeLibraryDrawer();
    tasksDrawer.classList.add('active');
    setDrawerToggleOpenState('toggle-tasks-btn', true);
    renderTasks();
    startTasksPolling();
}

function closeTasksDrawer() {
    const tasksDrawer = document.getElementById('tasks-drawer');
    if (!tasksDrawer) return;
    tasksDrawer.classList.remove('active');
    setDrawerToggleOpenState('toggle-tasks-btn', false);
    stopTasksPolling();
}

// The persistent drawers are position:fixed siblings of .app-container, so a plain
// `top: 0` box would paint over the header (and its own collapse button) rather than
// under it. Anchor the drawer below the real header+tab-bar height instead of a
// hardcoded pixel value, since that height differs across breakpoints.
function updateDrawerTopOffset() {
    const anchor = document.querySelector('.mobile-nav-tabs') || document.querySelector('.app-header');
    if (!anchor) return;
    const bottom = anchor.getBoundingClientRect().bottom;
    if (bottom > 0) {
        document.documentElement.style.setProperty('--drawer-top-offset', `${Math.ceil(bottom)}px`);
    }
}

// Modal & Drawer event bindings
function setupEventListeners() {
    // Settings Modal
    const openSettings = document.getElementById('open-settings-btn');
    const closeSettings = document.getElementById('settings-modal').querySelector('.close-btn');
    const settingsModal = document.getElementById('settings-modal');
    
    openSettings.addEventListener('click', () => {
        settingsModal.classList.add('active');
        updateCacheSizeInfo();
    });
    closeSettings.addEventListener('click', () => settingsModal.classList.remove('active'));
    
    // （API Key 输入框与可见性切换按钮已随死配置一并移除：托管模式密钥在服务端）

    document.getElementById('save-settings-btn').addEventListener('click', () => {
        saveConfig();
        settingsModal.classList.remove('active');
    });
    
    document.getElementById('reset-settings-btn').addEventListener('click', resetConfig);

    // Clear cache button handler
    const clearCacheBtn = document.getElementById('clear-cache-btn');
    if (clearCacheBtn) {
        clearCacheBtn.addEventListener('click', async () => {
            if (!confirm('确定要清理系统缓存（packet_cache.json）吗？这会清除已缓存的 LLM 激发数据。')) {
                return;
            }
            try {
                clearCacheBtn.disabled = true;
                clearCacheBtn.textContent = '清理中...';
                const resp = await fetch('/api/clear-cache', { method: 'POST' });
                const data = await resp.json();
                if (data.status === 'success') {
                    showToast('系统缓存清理成功', 'success');
                    updateCacheSizeInfo();
                } else {
                    showToast('清理失败: ' + data.message, 'error');
                }
            } catch (err) {
                showToast('请求出错: ' + err.message, 'error');
            } finally {
                clearCacheBtn.disabled = false;
                clearCacheBtn.textContent = '🧹 清理系统缓存';
            }
        });
    }

    // 2026-07-12 生图模型与 LLM 模型解耦：原「主模型 change → 重置生图模型下拉」
    // 的联动已删除（IMAGE_MODELS_BY_MAIN_MODEL 弃用）。生图模型清单固定为
    // IMAGE_MODELS，混搭组合（gemini LLM + gpt-image-2 生图）由服务端
    // resolve_gateway 按模型名自动路由。

    // Test API connection within Settings
    const testSettingsBtn = document.getElementById('test-settings-btn');
    if (testSettingsBtn) {
        testSettingsBtn.addEventListener('click', async () => {
            const originalText = testSettingsBtn.textContent;
            testSettingsBtn.textContent = '测试中...';
            testSettingsBtn.disabled = true;
            
            // 配置中心的 Base URL / API Key / 模型下拉均已移除：测试连接直接用
            // 当前生效的 config 对象（模型由激发页脚/帧序列卡片的内嵌选择器维护）
            const tempConfig = {
                baseUrl: config.baseUrl,
                apiKey: config.apiKey,
                model: config.model
            };
            
            try {
                const res = await fetch('/api/ping', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: tempConfig })
                });
                const data = await res.json();
                if (res.ok && data.online) {
                    showToast("连接测试成功！模型在线。（若修改了配置，保存后生效）", "success");
                    // 顺手刷新顶部状态徽章：徽章之前只在启动时检测一次，
                    // 代理恢复后过期的「离线」状态会一直硬拦生成按钮
                    checkApiStatus();
                } else {
                    showToast(`连接测试失败: ${data.message || '模型离线'}`, "error");
                }
            } catch (e) {
                showToast(`连接测试出错: ${e.message}`, "error");
            } finally {
                testSettingsBtn.textContent = originalText;
                testSettingsBtn.disabled = false;
            }
        });
    }

    // Library Drawer
    const openLibrary = document.getElementById('toggle-library-btn');
    const closeLibrary = document.getElementById('close-library-btn');
    const libraryDrawer = document.getElementById('library-drawer');
    const tasksDrawer = document.getElementById('tasks-drawer');

    openLibrary.addEventListener('click', () => {
        if (libraryDrawer.classList.contains('active')) {
            closeLibraryDrawer();
        } else {
            openLibraryDrawer();
        }
    });
    closeLibrary.addEventListener('click', closeLibraryDrawer);

    // Tasks Drawer
    const openTasks = document.getElementById('toggle-tasks-btn');
    const closeTasks = document.getElementById('close-tasks-btn');

    if (openTasks && closeTasks && tasksDrawer) {
        openTasks.addEventListener('click', () => {
            if (tasksDrawer.classList.contains('active')) {
                closeTasksDrawer();
            } else {
                openTasksDrawer();
            }
        });
        closeTasks.addEventListener('click', closeTasksDrawer);
    }

    // Tasks Drawer filter inputs and clear buttons
    const tasksSearchInput = document.getElementById('tasks-search');
    const tasksStatusSelect = document.getElementById('tasks-filter-status');
    const tasksTypeSelect = document.getElementById('tasks-filter-type');
    const clearCompletedBtn = document.getElementById('clear-completed-btn');
    const clearFailedBtn = document.getElementById('clear-failed-btn');

    if (tasksSearchInput) {
        // Debounce: avoid firing a network fetch on every single keystroke
        const debouncedSearch = _debounce((e) => {
            tasksSearchQuery = e.target.value;
            renderTasks();
        }, 300);
        tasksSearchInput.addEventListener('input', debouncedSearch);
    }
    if (tasksStatusSelect) {
        tasksStatusSelect.addEventListener('change', (e) => {
            tasksFilterStatus = e.target.value;
            renderTasks();
        });
    }
    if (tasksTypeSelect) {
        tasksTypeSelect.addEventListener('change', (e) => {
            tasksFilterType = e.target.value;
            renderTasks();
        });
    }
    if (clearCompletedBtn) {
        clearCompletedBtn.addEventListener('click', () => clearTasks('completed'));
    }
    if (clearFailedBtn) {
        clearFailedBtn.addEventListener('click', () => clearTasks('failed_cancelled'));
    }

    // View Active Generation Progress
    const viewProgressBtn = document.getElementById('view-progress-btn');
    if (viewProgressBtn) {
        viewProgressBtn.addEventListener('click', () => {
            const placeholderView = document.getElementById('output-placeholder-view');
            const loadingView = document.getElementById('output-loading-view');
            const contentView = document.getElementById('output-content-view');
            const errorView = document.getElementById('output-error-view');
            
            if (placeholderView) placeholderView.classList.remove('active');
            if (contentView) contentView.classList.remove('active');
            if (errorView) errorView.style.display = 'none';
            if (loadingView) loadingView.classList.add('active');
            
            updateActiveGenerationBanner();
        });
    }

    // Generation Action
    document.getElementById('generate-btn').addEventListener('click', () => generateIdea());
    document.getElementById('retry-btn').addEventListener('click', () => retryGeneration());

    // Cancel Generation Actions
    const cancelGenBtn = document.getElementById('cancel-generate-btn');
    if (cancelGenBtn) {
        cancelGenBtn.addEventListener('click', () => {
            const activeTaskId = localStorage.getItem('spark_active_task_id');
            if (activeTaskId) {
                fetch('/api/compose-cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: activeTaskId })
                }).catch(e => console.error("Failed to cancel active task on server:", e));
            }
            if (currentGenerationController) {
                currentGenerationController.abort();
                currentGenerationController = null;
                showToast("已取消创意激发", "info");
            }
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
        });
    }

    const cancelFramesBtn = document.getElementById('cancel-frames-btn');
    if (cancelFramesBtn) {
        cancelFramesBtn.addEventListener('click', () => {
            if (currentFramesController) {
                currentFramesController.abort();
                currentFramesController = null;
                showToast("已取消帧序列生成", "info");
            }
        });
    }

    const cancelVideosBtn = document.getElementById('cancel-videos-btn');
    if (cancelVideosBtn) {
        cancelVideosBtn.addEventListener('click', () => {
            if (currentVideosController) {
                currentVideosController.abort();
                currentVideosController = null;
                showToast("已取消视频序列生成", "info");
            }
        });
    }

    // Keyboard Hotkeys
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            // 面板守卫：只有「激发维度」工作区可见时才触发主合成，
            // 否则在图像工坊/结果页按 Ctrl+Enter 会静默发起一次隐藏的 LLM 合成任务
            const configPanel = document.querySelector('.panel-left');
            const cfgActive = configPanel && configPanel.classList.contains('mobile-active');
            if (cfgActive) {
                const genBtn = document.getElementById('generate-btn');
                if (genBtn && !genBtn.disabled) {
                    genBtn.click();
                }
            }
        }
        handleGlobalHotkeys(e);
    });

    // Preset Selection
    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const preset = e.currentTarget.dataset.preset;
            if (preset) {
                applyPreset(preset);
            }
        });
    });

    // Save state on slider input/change
    // rAF-gate the heavy updateConfigSummary (many DOM reads) so it runs
    // at most once per animation frame during rapid drag gestures
    let _configSummaryPending = false;
    const rafConfigSummary = () => {
        if (!_configSummaryPending) {
            _configSummaryPending = true;
            requestAnimationFrame(() => {
                updateConfigSummary();
                _configSummaryPending = false;
            });
        }
    };
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity', 'slider-beats'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', rafConfigSummary);
            el.addEventListener('change', saveSelectionState);
        }
    });

    // （旧 #theme-selector 点击存档监听已随主题选择器一起移除）

    // Tab buttons switching
    document.querySelectorAll('.result-tabs-bar .tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            switchTab(e.currentTarget.dataset.tab);
        });
    });

    // Idea Interaction Buttons
    document.getElementById('save-idea-btn').addEventListener('click', saveCurrentIdea);
    document.getElementById('export-idea-btn').addEventListener('click', exportIdeaMarkdown);
    document.getElementById('copy-prompt-btn').addEventListener('click', copyPromptToClipboard);
    document.getElementById('copy-prompt-btn-all').addEventListener('click', copyPromptToClipboard);
    document.getElementById('copy-tiktok-meta-btn').addEventListener('click', copyTikTokMetaToClipboard);
    document.getElementById('copy-tiktok-meta-cn-btn').addEventListener('click', copyTikTokMetaCnToClipboard);
    document.getElementById('make-cover-btn').addEventListener('click', () => generateCover());
    document.getElementById('generate-frames-btn').addEventListener('click', () => generateFrames());
    document.getElementById('generate-videos-btn').addEventListener('click', () => generateVideos());
    document.getElementById('merge-videos-btn').addEventListener('click', () => mergeVideos());
    const copyHookBtn = document.getElementById('copy-hook-btn');
    if (copyHookBtn) {
        copyHookBtn.addEventListener('click', () => {
            const hookValEl = document.getElementById('cover-hook-val');
            const val = hookValEl ? hookValEl.textContent : '';
            if (val) {
                copyText(val).then(() => {
                    showToast("英文文案已复制！", "success");
                }).catch(err => {
                    showToast("复制失败", "error");
                });
            }
        });
    }

    // Library search & filters
    const libSearch = document.getElementById('library-search');
    const libFilterTheme = document.getElementById('library-filter-theme');
    const libFilterTime = document.getElementById('library-filter-time');
    let searchTimeout = null;
    if (libSearch) {
        libSearch.addEventListener('input', () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(renderLibrary, 200);
        });
    }
    if (libFilterTheme) libFilterTheme.addEventListener('change', renderLibrary);
    if (libFilterTime) libFilterTime.addEventListener('change', renderLibrary);

    // Library Drawer buttons
    document.getElementById('export-all-btn').addEventListener('click', exportAllLibrary);
    
    const importBtn = document.getElementById('import-btn');
    const importFile = document.getElementById('import-file');
    importBtn.addEventListener('click', () => importFile.click());
    importFile.addEventListener('change', importLibrary);
}

// --- Tasks Drawer Functions & Polling ---
let tasksPollTimeout = null;
let currentPollInterval = 2500;
let tasksSearchQuery = '';
let tasksFilterStatus = '';
let tasksFilterType = '';

// 图片/视频生成类任务一律不进激发任务列表（2026-07-12 用户要求）：帧序列、
// 分步渲染、视频、封面的全过程都在各自模块内直播（含取消/重试入口），
// 激发任务列表只保留创意激发（compose / auto_run）任务
const MEDIA_TASK_TYPES = new Set(['frames', 'staged_render', 'videos', 'cover']);
const isIdeationTask = (t) => !MEDIA_TASK_TYPES.has((t.dimensions && t.dimensions.type) || 'idea');

async function renderTasks() {
    const tasksListContainer = document.getElementById('tasks-list');
    if (!tasksListContainer) return;

    try {
        const response = await fetch('/api/tasks');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const resData = await response.json();
        const tasks = (Array.isArray(resData) ? resData : (resData.tasks || []))
            .filter(isIdeationTask);

        // Update badge count
        updateTasksBadge(tasks);
        
        // Local filtering
        let filteredTasks = tasks.filter(task => {
            // 任务名优先用灵感卡片选题名（task_label），回退基础场景主题
            const theme = task.dimensions ? (task.dimensions.task_label || task.dimensions.theme || '未命名主题') : '未命名主题';
            const beats = (task.dimensions && task.dimensions.beats_count) ? ` (${task.dimensions.beats_count} 镜)` : '';
            const taskTitle = `${theme}${beats}`;

            if (tasksSearchQuery) {
                const q = tasksSearchQuery.toLowerCase();
                if (!taskTitle.toLowerCase().includes(q) && !String(task.id).includes(q)) {
                    return false;
                }
            }
            if (tasksFilterStatus) {
                if (task.status !== tasksFilterStatus) return false;
            }
            if (tasksFilterType) {
                const resolvedType = (task.dimensions && task.dimensions.type) || 'idea';
                if (resolvedType !== tasksFilterType) return false;
            }
            return true;
        });
        
        let html = '';
        if (filteredTasks.length === 0) {
            html = `<div class="tasks-empty">暂无符合筛选条件的任务</div>`;
        }
        // (loop body is skipped naturally when there are no filtered tasks)
        filteredTasks.forEach(task => {
            // frames_/videos_/cover_ 前缀的任务 ID 不是时间戳，直接 parseInt 会显示 Invalid Date
            const idMs = parseInt(task.id, 10);
            const dateStr = Number.isFinite(idMs) && String(idMs) === String(task.id)
                ? new Date(idMs).toLocaleString()
                : (task.last_active ? new Date(task.last_active * 1000).toLocaleString() : '—');
            // 任务名优先用灵感卡片选题名（task_label），回退基础场景主题
            const theme = task.dimensions ? (task.dimensions.task_label || task.dimensions.theme || '未命名主题') : '未命名主题';
            const beats = (task.dimensions && task.dimensions.beats_count) ? ` (${task.dimensions.beats_count} 镜)` : '';
            const taskTitle = `${theme}${beats}`;

            let statusLabel = '';
            let statusClass = '';
            let footerButtons = '';
            let progressHtml = '';
            let errorHtml = '';
            let tokenInfoHtml = '';
            
            if (task.status === 'running') {
                statusLabel = '运行中';
                statusClass = 'running';

                // Reuse the same ProgressModel the main loading view drives itself with, so the
                // drawer's mini progress bar tracks the real backend stages (outline/batch/audit/
                // repair, or the frames/videos/cover equivalents) instead of a stale, hand-rolled
                // stage list that no longer matches what the backend actually emits.
                const taskType = window.ProgressModel ? ProgressModel.inferTaskType(task.dimensions) : 'compose';
                const progressInfo = window.ProgressModel
                    ? ProgressModel.progressFromEvents(task.events || [], taskType, null)
                    : null;
                const progressPercent = progressInfo ? progressInfo.percent : 0;
                const currentStage = (progressInfo && progressInfo.label) || '准备中...';

                progressHtml = `
                    <div class="task-progress-container">
                        <div class="task-progress-text">
                            <span>${escapeHtml(currentStage)}</span>
                            <span>${progressPercent}%</span>
                        </div>
                        <div class="task-progress-bar">
                            <div class="task-progress-fill" style="width: ${progressPercent}%;"></div>
                        </div>
                    </div>
                `;
                
                footerButtons = `
                    <button class="task-action-btn view" onclick="viewTask('${task.id}', ${JSON.stringify(task.dimensions).replace(/"/g, '&quot;')})">查看</button>
                    <button class="task-action-btn cancel" onclick="cancelTask('${task.id}', event)">取消</button>
                `;
            } else if (task.status === 'completed') {
                statusLabel = '已完成';
                statusClass = 'completed';
                if (task.result && task.result.token_usage) {
                    const usage = task.result.token_usage;
                    tokenInfoHtml = `
                        <div class="task-token-info" style="font-size: 11px; color: var(--text-secondary, #94a3b8); margin-top: 8px; font-family: var(--font-mono, monospace);">
                            Tokens: ${usage.total_tokens} (I:${usage.prompt_tokens} O:${usage.completion_tokens}) | Calls: ${usage.api_calls}
                        </div>
                    `;
                }
                footerButtons = `
                    <button class="task-action-btn view" onclick="loadCompletedTask('${task.id}')">查看</button>
                    <button class="task-action-btn delete" onclick="deleteTask('${task.id}', event)">删除</button>
                `;
            } else if (task.status === 'failed') {
                statusLabel = '已失败';
                statusClass = 'failed';
                const errorMsg = task.error || '未知错误';
                errorHtml = `<div class="task-error-text">❌ 错误: ${escapeHtml(errorMsg)}</div>`;
                footerButtons = `
                    <button class="task-action-btn retry" onclick="retryTask('${task.id}', ${JSON.stringify(task.dimensions).replace(/"/g, '&quot;')}, event)">重试</button>
                    <button class="task-action-btn delete" onclick="deleteTask('${task.id}', event)">删除</button>
                `;
            } else if (task.status === 'cancelled') {
                statusLabel = '已取消';
                statusClass = 'cancelled';
                const errorMsg = task.error || '用户已取消';
                errorHtml = `<div class="task-error-text" style="color: var(--text-secondary, #94a3b8);">⚪ ${escapeHtml(errorMsg)}</div>`;
                footerButtons = `
                    <button class="task-action-btn delete" onclick="deleteTask('${task.id}', event)">删除</button>
                `;
            }
            
            html += `
                <div class="task-card" data-task-id="${task.id}">
                    <div class="task-card-header">
                        <div>
                            <div class="task-card-title">${escapeHtml(taskTitle)}</div>
                            <div class="task-card-date">${escapeHtml(dateStr)}</div>
                        </div>
                        <span class="task-status-badge ${statusClass}">${escapeHtml(statusLabel)}</span>
                    </div>
                    ${tokenInfoHtml}
                    ${progressHtml}
                    ${errorHtml}
                    <div class="task-card-footer">
                        ${footerButtons}
                    </div>
                </div>
            `;
        });
        
        // Skip the DOM teardown when the rendered output is byte-identical to the last poll.
        // `html` reflects only visible state (status, bucketed progress %, stage text, buttons) —
        // not the raw growing events array — so identical html means an identical view. This turns
        // most 2.5s poll ticks into a no-op and stops the drawer from snapping to the top and
        // dropping hover/focus while a task runs. When it does change, scroll position is preserved.
        if (html === _lastTasksRenderHtml) return;
        _lastTasksRenderHtml = html;
        const _prevScroll = tasksListContainer.scrollTop;
        tasksListContainer.innerHTML = html;
        tasksListContainer.scrollTop = _prevScroll;
    } catch (e) {
        console.error("Failed to render tasks list:", e);
        _lastTasksRenderHtml = null; // force a real re-render on the next successful poll
        tasksListContainer.innerHTML = `<div class="tasks-empty" style="color: #f87171;">加载任务列表失败: ${escapeHtml(e.message)}</div>`;
    }
}

// Function startTasksPolling moved to modular JS file

// Function stopTasksPolling moved to modular JS file

// Function updateTasksBadge moved to modular JS file

// Last rendered tasks-list markup; used to skip no-op re-renders (see renderTasks).
let _lastTasksRenderHtml = null;

let globalBadgeTimeout = null;

async function startGlobalTasksBadgePolling() {
    if (globalBadgeTimeout) clearTimeout(globalBadgeTimeout);
    
    const poll = async () => {
        let hasRunning = false;
        try {
            const response = await fetch('/api/tasks');
            if (response.ok) {
                const resData = await response.json();
                // 与 renderTasks 同口径：图片/视频生成类任务不计入任务列表角标
                const tasks = (Array.isArray(resData) ? resData : (resData.tasks || []))
                    .filter(isIdeationTask);
                updateTasksBadge(tasks);
                hasRunning = tasks.some(t => t.status === 'running');
            }
        } catch (e) {
            console.warn("Background badge poll failed:", e);
        }
        
        const nextInterval = hasRunning ? 5000 : 30000;
        globalBadgeTimeout = setTimeout(poll, nextInterval);
    };
    
    poll();
}

async function viewTask(taskId, dimensions) {
    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');
    
    if (placeholderView) placeholderView.classList.remove('active');
    if (contentView) contentView.classList.remove('active');
    if (errorView) errorView.style.display = 'none';
    if (loadingView) loadingView.classList.add('active');
    
    if (genBtn) {
        genBtn.disabled = true;
        genBtn.classList.add('loading');
    }
    
    generationState.status = 'composing';
    updateActiveGenerationBanner();

    localStorage.setItem('spark_active_task_id', taskId);
    localStorage.setItem('spark_active_task_dimensions', JSON.stringify(dimensions));

    // 跟进任务时同样在 loading 视图展示选题名
    const viewTopicEl = document.getElementById('loading-topic-name');
    if (viewTopicEl) viewTopicEl.textContent = (dimensions && (dimensions.task_label || dimensions.theme)) || '';

    const startTimeOffset = parseInt(taskId, 10);
    startLoadingTimer(startTimeOffset);

    showToast("已重连至该任务的实时输出日志...", "info");
    
    try {
        await streamProgress(taskId, dimensions);
    } catch (e) {
        console.error("Error streaming reconnected task:", e);
    }
}

async function loadCompletedTask(taskId) {
    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');
    
    try {
        const res = await fetch(`/api/compose-status?task_id=${taskId}`);
        if (!res.ok) throw new Error("无法读取任务状态");
        
        const task = await res.json();
        if (task.status === 'completed' && task.result) {
            const data = task.result;
            const result = {
                id: task.id,
                title: data.title || '未命名创意',
                theme: task.dimensions ? task.dimensions.theme : '未命名主题',
                creativity: task.dimensions ? task.dimensions.creativity : '',
                prompt_block: data.prompt_block || data.raw || '',
                audit_md: data.audit_md || '',
                repair_md: data.repair_md || '',
                timestamp: new Date(parseInt(task.id, 10)).toLocaleString(),
                timings: data.timings || {},
                image_count: data.image_count || 0,
                video_count: data.video_count || 0,
                covers: data.covers || [],
                frameRun: data.frameRun || null,
                english_title: data.english_title || '',
                social_title_en: data.social_title_en || '',
                social_title_cn: data.social_title_cn || '',
                collage_url: data.collage_url || ''
            };
            
            currentIdea = result;
            saveCurrentIdeaState();
            generationState.status = 'idle';
            clearLiveBeatsPanel();
            renderIdea(result);
            switchMainTab('results');

            if (placeholderView) placeholderView.classList.remove('active');
            if (loadingView) loadingView.classList.remove('active');
            if (contentView) contentView.classList.add('active');
            if (errorView) errorView.style.display = 'none';
            
            if (genBtn) {
                genBtn.disabled = false;
                genBtn.classList.remove('loading');
            }
            
            updateActiveGenerationBanner();
            switchTab('prompts');
            showToast("历史生成任务结果已载入！", "success");
        } else {
            showToast("该任务未完成或数据损坏", "error");
        }
    } catch (e) {
        console.error("Failed to load completed task:", e);
        showToast(`载入任务失败: ${e.message}`, "error");
    }
}

async function cancelTask(taskId, event) {
    if (event) event.stopPropagation();
    
    try {
        const response = await fetch('/api/compose-cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: taskId })
        });
        if (response.ok) {
            showToast("正在取消该生成任务...", "info");
            const activeTaskId = localStorage.getItem('spark_active_task_id');
            if (activeTaskId === taskId && currentGenerationController) {
                currentGenerationController.abort();
                currentGenerationController = null;
            }
            setTimeout(renderTasks, 500);
        } else {
            showToast("取消任务失败", "error");
        }
    } catch (e) {
        console.error("Cancel task request failed:", e);
        showToast("请求取消失败", "error");
    }
}

async function deleteTask(taskId, event) {
    if (event) event.stopPropagation();
    
    const confirmed = await customConfirm("确定删除此生成任务记录吗？");
    if (!confirmed) return;
    
    try {
        const response = await fetch('/api/tasks/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: taskId })
        });
        if (response.ok) {
            showToast("生成记录已删除", "success");
            
            const activeTaskId = localStorage.getItem('spark_active_task_id');
            if (activeTaskId === taskId) {
                localStorage.removeItem('spark_active_task_id');
                localStorage.removeItem('spark_active_task_dimensions');
                if (currentGenerationController) {
                    currentGenerationController.abort();
                    currentGenerationController = null;
                }
                
                generationState.status = 'idle';
                const placeholderView = document.getElementById('output-placeholder-view');
                const loadingView = document.getElementById('output-loading-view');
                const contentView = document.getElementById('output-content-view');
                if (loadingView) loadingView.classList.remove('active');
                if (placeholderView) placeholderView.classList.add('active');
                if (contentView) contentView.classList.remove('active');
                updateActiveGenerationBanner();
                const genBtn = document.getElementById('generate-btn');
                if (genBtn) {
                    genBtn.disabled = false;
                    genBtn.classList.remove('loading');
                }
            }
            
            renderTasks();
        } else {
            showToast("删除任务记录失败", "error");
        }
    } catch (e) {
        console.error("Delete task request failed:", e);
        showToast("请求删除失败", "error");
    }
}

async function retryTask(taskId, dimensions, event) {
    if (event) event.stopPropagation();

    closeTasksDrawer();

    showToast("正在重新提交该生成任务...", "info");

    try {
        // 沿用被重试任务的 task_id：后端会原地重置这条终态记录复用，
        // 重试直接覆盖旧的失败记录，任务列表不再留一条失败 + 一条新记录
        await generateIdea({
            dimensions: dimensions,
            config: config,
            taskId: taskId
        });
    } catch (e) {
        console.error("Retry task failed:", e);
        showToast(`重试失败: ${e.message}`, "error");
    }
}

async function clearTasks(statusGroup) {
    let msg = "";
    if (statusGroup === "completed") {
        msg = "确定要清空所有【已完成】的任务记录吗？";
    } else if (statusGroup === "failed_cancelled") {
        msg = "确定要清空所有【已失败】和【已取消】的任务记录吗？";
    }
    
    const confirmed = await customConfirm(msg);
    if (!confirmed) return;
    
    try {
        const response = await fetch('/api/tasks/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status_group: statusGroup })
        });
        if (response.ok) {
            const data = await response.json();
            showToast(`清空成功，共删除 ${data.count} 条记录`, "success");
            
            // If the currently viewed task was deleted, reset the active task ID
            const activeTaskId = localStorage.getItem('spark_active_task_id');
            if (activeTaskId) {
                const listRes = await fetch('/api/tasks');
                if (listRes.ok) {
                    const resData = await listRes.json();
                    const tasks = Array.isArray(resData) ? resData : (resData.tasks || []);
                    const remains = tasks.some(t => t.id === activeTaskId);
                    if (!remains) {
                        localStorage.removeItem('spark_active_task_id');
                        localStorage.removeItem('spark_active_task_dimensions');
                        if (currentGenerationController) {
                            currentGenerationController.abort();
                            currentGenerationController = null;
                        }
                        generationState.status = 'idle';
                        const placeholderView = document.getElementById('output-placeholder-view');
                        const loadingView = document.getElementById('output-loading-view');
                        const contentView = document.getElementById('output-content-view');
                        if (loadingView) loadingView.classList.remove('active');
                        if (placeholderView) placeholderView.classList.add('active');
                        if (contentView) contentView.classList.remove('active');
                        updateActiveGenerationBanner();
                        const genBtn = document.getElementById('generate-btn');
                        if (genBtn) {
                            genBtn.disabled = false;
                            genBtn.classList.remove('loading');
                        }
                    }
                }
            }
            renderTasks();
        } else {
            showToast("清空任务失败", "error");
        }
    } catch (e) {
        console.error("Clear tasks request failed:", e);
        showToast("请求清空失败", "error");
    }
}

window.renderTasks = renderTasks;
window.startTasksPolling = startTasksPolling;
window.stopTasksPolling = stopTasksPolling;
window.viewTask = viewTask;
window.loadCompletedTask = loadCompletedTask;
window.cancelTask = cancelTask;
window.deleteTask = deleteTask;
window.retryTask = retryTask;
window.generateIdea = generateIdea;
window.clearTasks = clearTasks;

// Compose the full skill-grade prompt set via the server-side skill shell.
// The GUI only collects dimensions; the server runs them through the real
// restoration-prompt-composer contract and relays to the local LLM proxy.
// Compose the full skill-grade prompt set via the server-side skill shell.
// The GUI only collects dimensions; the server runs them through the real
// restoration-prompt-composer contract and relays to the local LLM proxy.
async function generateIdea(retryParams = null) {
    const badge = document.getElementById('api-status-badge');
    if (badge && badge.classList.contains('offline')) {
        // 徽章可能是启动时的过期状态（代理其间已恢复）——先实时复测一次再决定拦不拦
        const online = await checkApiStatus();
        if (!online) {
            showToast("⚠️ API 连接已断开，请先在配置中心检查连接并保存！", "error");
            const settingsModal = document.getElementById('settings-modal');
            if (settingsModal) {
                settingsModal.classList.add('active');
            }
            return;
        }
    }

    // 基础场景主题选择器已移除：非重试路径的选题只能来自「载入维度」过的灵感
    // 卡片（联网参考驱动）。没载入过就没有可合成的主题，在切换视图前先拦下。
    const loadedIdea = (typeof loadedIdeationCover !== 'undefined' && loadedIdeationCover) ? loadedIdeationCover : null;
    if (!retryParams && (!loadedIdea || !loadedIdea.input_str)) {
        showToast('请先在灵感推荐卡片点「载入维度」选定选题（或直接在卡片上一键合成）', 'error');
        return;
    }

    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');

    placeholderView.classList.remove('active');
    contentView.classList.remove('active');
    if (errorView) errorView.style.display = 'none';
    loadingView.classList.add('active');
    // 生成开始即切到「激发结果」工作区——旧的自动切换脚本指向已删除的 ID，
    // 导致用户点了激发却停在配置面板，以为没有反应
    switchMainTab('results');
    updateActiveGenerationBanner();

    if (genBtn) {
        genBtn.disabled = true;
        genBtn.classList.add('loading');
    }

    let dimensions, currentConf, reuseTaskId = null;
    if (retryParams) {
        dimensions = retryParams.dimensions;
        currentConf = retryParams.config;
        reuseTaskId = retryParams.taskId ? String(retryParams.taskId) : null;
    } else {
        // Collect GUI dimensions — 选题（theme=一键输入串）与任务名都取自已载入的
        // 灵感卡片；函数入口已保证 loadedIdea.input_str 存在
        const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
            .map(node => node.textContent.trim());

        dimensions = {
            theme: loadedIdea.input_str,
            task_label: loadedIdea.task_label || loadedIdea.input_str,
            // 与卡片「一键合成」路径对齐：封面与英文标题一并带给后端（有则复用）
            cover_url: loadedIdea.cover_url || null,
            english_title: loadedIdea.english_title || null,
            anchors: activeAnchors,
            complexity: document.getElementById('val-complexity').textContent,
            budget: document.getElementById('val-budget').textContent,
            ratio: document.getElementById('val-ratio').textContent,
            creativity: document.getElementById('val-creativity').textContent,
            beats_count: parseInt(document.getElementById('slider-beats').value, 10)
        };
        currentConf = { ...config };
    }

    // Generate unique taskId (retry reuses the failed record's id so the rerun
    // overwrites that record server-side instead of piling up a duplicate)
    const taskId = reuseTaskId || Date.now().toString();

    // Save last params for retry — include taskId so the error-view retry button
    // also overwrites this run's record instead of creating a new one
    generationState.lastParams = { dimensions, config: currentConf, taskId };
    generationState.status = 'composing';

    // 激发进行中在 loading 视图显著展示本单选题名（灵感卡片选题名或基础主题）
    const topicNameEl = document.getElementById('loading-topic-name');
    if (topicNameEl) topicNameEl.textContent = dimensions.task_label || dimensions.theme || '';

    setupLoadingSteps('compose');

    // Persist active task to localStorage so it survives refresh/close
    localStorage.setItem('spark_active_task_id', taskId);
    localStorage.setItem('spark_active_task_dimensions', JSON.stringify(dimensions));

    startLoadingTimer();

    try {
        // POST to compose starts the background worker thread on the server
        const response = await fetch('/api/compose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dimensions, config: currentConf, task_id: taskId })
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        // Connect to stream
        await streamProgress(taskId, dimensions);

    } catch (e) {
        stopLoadingTimer();
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');
        
        loadingView.classList.remove('active');
        contentView.classList.remove('active');
        generationState.status = 'idle';
        
        if (errorView) {
            errorView.style.display = 'flex';
        } else {
            placeholderView.classList.add('active');
        }
        
        const errMsgEl = document.getElementById('error-message-text');
        if (errMsgEl) {
            errMsgEl.textContent = `启动生成失败：${e.message || e}`;
        }
        showToast(`启动生成失败：${e.message || e}`, "error");
        
        if (genBtn) {
            genBtn.disabled = false;
            genBtn.classList.remove('loading');
        }
        updateActiveGenerationBanner();
    }
}

// Connect to background task SSE stream and update UI state
async function streamProgress(taskId, dimensions) {
    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');

    // 开启新一代 compose 流：先掐掉上一代（若有），旧流的收尾逻辑会被 epoch 守卫拦下
    const epoch = ++streamEpochs.compose;
    const isCurrent = () => epoch === streamEpochs.compose;
    if (currentGenerationController) {
        try { currentGenerationController.abort(); } catch (_) { /* noop */ }
    }
    const controller = new AbortController();
    currentGenerationController = controller;

    // 驱动 #generation-progress-* 进度条（之前这组 DOM 从未被任何 JS 更新过）
    const taskType = window.ProgressModel ? ProgressModel.inferTaskType(dimensions) : 'compose';
    let progressState = window.ProgressModel ? ProgressModel.createProgressState(taskType) : null;
    const applyComposeProgress = (type, data) => {
        if (!window.ProgressModel) return;
        const info = ProgressModel.normalizeGenerationProgress(type, data, taskType, progressState);
        progressState = info.state;
        setProgressBar('generation', info);
    };
    applyComposeProgress('init', null);

    try {
        const watch = await watchTaskUntilTerminal(taskId, {
            label: 'compose',
            signal: controller.signal,
            onEvent: (type, data) => {
                if (!isCurrent()) return;
                if (type === 'progress') {
                    updateProgressUI(data || {});
                    handleComposeProgressExtras(data || {});
                    applyComposeProgress(type, data);
                } else if (type === 'text_chunk') {
                    appendLiveTerminal(data);
                    applyComposeProgress(type, data);
                } else if (type === 'review_pause') {
                    // auto_run 任务（自治管线）的监修暂停也走同一块审阅面板
                    showFrameReviewPanel(taskId, data);
                } else if (type === 'review_resume') {
                    hideFrameReviewPanel();
                } else if (type === 'reconnecting') {
                    const stageText = document.getElementById('loading-stage-text');
                    if (stageText) stageText.textContent = `与服务的连接中断，正在自动重连（第 ${data.attempt} 次）...`;
                } else if (type === 'result' || type === 'error') {
                    hideFrameReviewPanel();
                    applyComposeProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;
        stopLoadingTimer();
        if (currentGenerationController === controller) currentGenerationController = null;
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');

        if (watch.status === 'cancelled') {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }
        if (watch.status === 'failed') {
            throw new Error(watch.error || '未知错误');
        }
        if (!watch.result) {
            throw new Error("模型未返回有效结果");
        }

        const data = watch.result;

        const result = {
            id: taskId,
            title: data.title || '未命名创意',
            theme: dimensions.theme || '未命名主题',
            creativity: dimensions.creativity || '',
            prompt_block: data.prompt_block || data.raw || '',
            audit_md: data.audit_md || '',
            repair_md: data.repair_md || '',
            // 复用的重试 id 可能是 auto_ 前缀（parseInt 得 NaN）→ 落到当前时间
            timestamp: new Date(Number.isFinite(parseInt(taskId, 10)) ? parseInt(taskId, 10) : Date.now()).toLocaleString(),
            timings: data.timings || {},
            image_count: data.image_count || 0,
            video_count: data.video_count || 0,
            collage_url: data.collage_url || '',
            covers: data.covers || [],
            frameRun: data.frameRun || null,
            english_title: data.english_title || '',
            social_title_en: data.social_title_en || '',
            social_title_cn: data.social_title_cn || ''
        };

        currentIdea = result;
        saveCurrentIdeaState();
        generationState.status = 'idle';

        clearLiveBeatsPanel();
        renderIdea(result);
        switchMainTab('results');

        updateLoadingStep('step-1', 'completed');
        updateLoadingStep('step-2', 'completed');
        updateLoadingStep('step-3', 'completed');
        updateLoadingStep('step-4', 'completed');

        // Immediately transition to outputs view, do not block the user
        switchTab('prompts');
        loadingView.classList.remove('active');
        contentView.classList.add('active');
        
        showToast("提示词集合合成成功！已开始在后台制作封面图。", "success");
        // Background asynchronous cover generation
        generateCover();

    } catch (e) {
        // 旧一代流的收尾不允许触碰新一代流的 UI（viewTask/retryTask 接管场景）
        if (!isCurrent()) return;
        stopLoadingTimer();
        if (currentGenerationController === controller) currentGenerationController = null;
        console.error("Failed to stream progress:", e);

        loadingView.classList.remove('active');
        contentView.classList.remove('active');

        generationState.status = 'idle';

        if (e.name === 'AbortError') {
            placeholderView.classList.add('active');
            showToast("合成已被取消或已超时", "info");
        } else {
            generationState.status = 'error';
            if (errorView) {
                errorView.style.display = 'flex';
            } else {
                placeholderView.classList.add('active');
            }

            let errorMsg = "合成发生阻碍，请检查本地 API 与 skill 路径。";
            if (e.message) {
                const raw = String(e.message);
                if (/network error|failed to fetch|networkerror|load failed/i.test(raw)) {
                    errorMsg = "合成连接已断开：与本地服务的实时连接中断。请确认 SPARK 服务（端口 8085）与 Antigravity 代理（端口 8046）仍在运行，然后点击「重试」。";
                } else {
                    errorMsg = `合成失败：${raw}`;
                }
            }

            const errMsgEl = document.getElementById('error-message-text');
            if (errMsgEl) {
                errMsgEl.textContent = errorMsg;
            }
            showToast(errorMsg, "error");
        }
    } finally {
        if (isCurrent()) {
            if (genBtn) {
                genBtn.disabled = false;
                genBtn.classList.remove('loading');
            }
            updateActiveGenerationBanner();
        }
    }
}

// Reconnect to active task if page refreshed/reopened
async function resumeActiveTaskIfExists() {
    const activeTaskId = localStorage.getItem('spark_active_task_id');
    const storedDimensions = localStorage.getItem('spark_active_task_dimensions');
    if (!activeTaskId || !storedDimensions) return;
    
    let dimensions;
    try {
        dimensions = JSON.parse(storedDimensions);
    } catch (e) {
        console.error("Failed to parse stored active task dimensions:", e);
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');
        return;
    }
    
    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');
    
    const resetUItoIdle = () => {
        generationState.status = 'idle';
        if (genBtn) {
            genBtn.disabled = false;
            genBtn.classList.remove('loading');
        }
        if (loadingView) loadingView.classList.remove('active');
        if (placeholderView) placeholderView.classList.add('active');
        if (contentView) contentView.classList.remove('active');
        if (errorView) errorView.style.display = 'none';
        updateActiveGenerationBanner();
    };

    try {
        const statusRes = await fetch(`/api/compose-status?task_id=${activeTaskId}`);
        if (!statusRes.ok) {
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
            resetUItoIdle();
            return;
        }
        
        const task = await statusRes.json();
        if (task.status === 'completed') {
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
            
            currentIdea = task.result;
            saveCurrentIdeaState();
            generationState.status = 'idle';
            renderIdea(task.result);
            
            if (placeholderView) placeholderView.classList.remove('active');
            if (loadingView) loadingView.classList.remove('active');
            if (contentView) contentView.classList.add('active');
            if (errorView) errorView.style.display = 'none';
            
            switchTab('prompts');
            showToast("检测到后台任务已完成，已载入结果！", "success");
            generateCover();
            if (genBtn) {
                genBtn.disabled = false;
                genBtn.classList.remove('loading');
            }
            updateActiveGenerationBanner();
            return;
        } else if (task.status === 'failed') {
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
            showToast(`后台任务生成失败: ${task.error || '未知原因'}`, "error");
            resetUItoIdle();
            return;
        } else if (task.status === 'running') {
            if (placeholderView) placeholderView.classList.remove('active');
            if (contentView) contentView.classList.remove('active');
            if (errorView) errorView.style.display = 'none';
            if (loadingView) loadingView.classList.add('active');
            switchMainTab('results');

            setupLoadingSteps('compose');

            const loadingHeader = loadingView.querySelector('h3');
            const loadingStage = document.getElementById('loading-stage-text');
            if (loadingHeader) loadingHeader.textContent = '正在按 skill 契约合成提示词...';
            if (loadingStage) loadingStage.textContent = '解析主题，拆解场景变量与施工节拍...';
            // 刷新恢复后同样展示本单选题名
            const resumeTopicEl = document.getElementById('loading-topic-name');
            if (resumeTopicEl) resumeTopicEl.textContent = (dimensions && (dimensions.task_label || dimensions.theme)) || '';

            if (genBtn) {
                genBtn.disabled = true;
                genBtn.classList.add('loading');
            }

            generationState.status = 'composing';
            updateActiveGenerationBanner();
            
            // Start timer with exact offset
            const startTimeOffset = parseInt(activeTaskId, 10);
            startLoadingTimer(startTimeOffset);
            
            showToast("已重连至后台正在执行的任务...", "info");
            
            // Reconnect to progress stream
            await streamProgress(activeTaskId, dimensions);
        } else {
            // Task not found or other state
            localStorage.removeItem('spark_active_task_id');
            localStorage.removeItem('spark_active_task_dimensions');
            resetUItoIdle();
        }
    } catch (e) {
        console.error("Failed to check active task status:", e);
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');
        resetUItoIdle();
    }
}

// Function saveActiveBackgroundTasksToLocalStorage moved to modular JS file

// Function resumeActiveBackgroundTasksIfExists moved to modular JS file

/**
 * 把一个已生成的帧渲染进对应槽位卡片。
 * 事件重放/重连会对同一槽位重复触发，所以这里用 on* 赋值（幂等）而不是
 * addEventListener（旧实现每次事件都往同一元素堆叠一套新监听器）。
 */
function updateFrameSlotCard(f) {
    if (!f) return;
    const slot = document.getElementById(`frame-slot-${f.sequence}`);
    if (!slot) return;
    slot.className = 'frame-card';
    slot.style.cursor = 'pointer';
    slot.title = `打开第 ${f.sequence} 帧`;
    slot.innerHTML = `
        <img src="" alt="Frame ${f.sequence}" loading="lazy">
        <div class="frame-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
            <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${f.sequence}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;">重试</button>
        </div>
        <span>IMG ${String(f.sequence).padStart(3, '0')}</span>
    `;
    // Live/retry SSE path: a frame just arrived from the backend and may have overwritten an
    // already-shown file (retry), so bump its cache version to force one fresh fetch. Passive
    // grid re-renders (media_renderer) use bust=false and stay on the browser cache.
    safeSetImageSrc(slot.querySelector('img'), f.url, true);

    slot.onmouseenter = () => {
        const actions = slot.querySelector('.frame-card-actions');
        if (actions) actions.style.opacity = '1';
    };
    slot.onmouseleave = () => {
        const actions = slot.querySelector('.frame-card-actions');
        if (actions) actions.style.opacity = '0';
    };
    slot.onclick = (e) => {
        if (e.target.classList.contains('retry-frame-btn')) return;
        const validFrames = (currentIdea && currentIdea.frameRun && currentIdea.frameRun.frames) || [];
        const mediaList = validFrames.map((frame) => ({
            type: 'image',
            url: frame.url || frame.file,
            caption: `<strong>第 ${frame.sequence} 帧 / 共 ${validFrames.length} 帧</strong>`
        }));
        const clickedIndex = validFrames.findIndex(frame => frame.sequence === f.sequence);
        openLightbox(mediaList, clickedIndex >= 0 ? clickedIndex : 0);
    };
    slot.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        retrySingleFrame(f.sequence);
    });
}

/* ── 帧序列实时生成动态 ─────────────────────────────────────────────
   生成过程直接在「连续帧序列生成」模块内滚动直播（逐帧渲染/质检结论/重试原因），
   不必打开任务列表。只增量追加行、贴底才自动跟随，行数上限 300。 */
function framesFeedSetLive(isLive) {
    const dot = document.getElementById('frames-feed-dot');
    if (dot) dot.classList.toggle('active', !!isLive);
}

function framesFeedReset(introText) {
    const wrap = document.getElementById('frames-live-feed');
    const lines = document.getElementById('frames-live-feed-lines');
    if (!wrap || !lines) return;
    lines.innerHTML = '';
    wrap.style.display = 'block';
    framesFeedSetLive(true);
    if (introText) framesFeedLine(introText);
}

function framesFeedLine(text, cls) {
    const lines = document.getElementById('frames-live-feed-lines');
    if (!lines) return;
    const wrap = document.getElementById('frames-live-feed');
    if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
    const nearBottom = lines.scrollHeight - lines.scrollTop - lines.clientHeight < 60;
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    const line = document.createElement('div');
    line.className = 'gen-feed-line' + (cls ? ` ${cls}` : '');
    const safeText = escapeHtml(String(text).length > 220 ? String(text).slice(0, 220) + '…' : String(text));
    line.innerHTML = `<span class="gen-feed-time">[${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}]</span> ${safeText}`;
    lines.appendChild(line);
    while (lines.children.length > 300) lines.removeChild(lines.firstChild);
    if (nearBottom) lines.scrollTop = lines.scrollHeight;
}

function framesFeedQualityLine(f) {
    if (!f) return;
    const seq = String(f.sequence || 0).padStart(3, '0');
    const gate = f.quality_gate;
    const reason = typeof f.vlm_qa_reason === 'string' ? f.vlm_qa_reason : '';
    if (gate === 'auto_approved') {
        if (reason.indexOf('WARN') === 0) {
            framesFeedLine(`✅ IMG ${seq} 完成（宽松放行留痕：${reason.replace(/^WARN:?\s*/, '')}）`, 'warn');
        } else {
            framesFeedLine(`✅ IMG ${seq} 完成（质检通过）`, 'ok');
        }
    } else if (gate === 'auto_approved_degraded') {
        framesFeedLine(`⚠️ IMG ${seq} 已放行（判定服务异常 fail-open：帧已渲染但未经核验，可单帧重试复检）`, 'warn');
    } else if (gate === 'vlm_qa_failed' || gate === 'sequence_review_flagged') {
        framesFeedLine(`❌ IMG ${seq} 一致性审查未通过（${reason || '原因未知'}），已保留末次渲染结果，可单帧重试`, 'err');
    } else if (gate === 'sequence_reviewed_pass') {
        framesFeedLine(`✅ IMG ${seq} 完成（一致性审查通过）`, 'ok');
    } else if (gate === 'pending_manual_review') {
        framesFeedLine(`✅ IMG ${seq} 完成（一致性审查将在整套序列渲染完毕后统一进行）`, 'ok');
    } else {
        framesFeedLine(`✅ IMG ${seq} 完成`, 'ok');
    }
}

async function streamFramesProgress(taskId) {
    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    const grid = document.getElementById('frames-grid');
    if (!btn || !progress || !meta || !grid) return;

    const epoch = ++streamEpochs.frames;
    const isCurrent = () => epoch === streamEpochs.frames;
    if (currentFramesController) {
        try { currentFramesController.abort(); } catch (_) { /* noop */ }
    }
    const controller = new AbortController();
    currentFramesController = controller;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '连接帧生成事件流...';

    activeBackgroundTasks.frames = true;
    updateTabStatusDot();

    let frameProgressState = window.ProgressModel ? ProgressModel.createProgressState('frames') : null;
    const applyFramesProgress = (type, data) => {
        if (!window.ProgressModel) return null;
        const info = ProgressModel.normalizeGenerationProgress(type, data, 'frames', frameProgressState);
        frameProgressState = info.state;
        setProgressBar('frames', info);
        return info;
    };
    applyFramesProgress('queue', { message: '连接帧生成事件流...' });
    framesFeedReset('🔌 已连接帧生成事件流，等待后台开始…');

    try {
        const watch = await watchTaskUntilTerminal(taskId, {
            label: 'frames',
            signal: controller.signal,
            onEvent: (type, data) => {
                if (!isCurrent()) return;
                if (type === 'start') {
                    applyFramesProgress('start', data);
                    const total = (data && data.total) || 0;
                    meta.textContent = `开始生成共 ${total} 帧序列图...`;
                    framesFeedLine(`🚀 开始生成，共 ${total} 帧（首帧文生图，后续逐帧图生图链式推进）`);

                    if (currentIdea) {
                        if (!currentIdea.frameRun) {
                            currentIdea.frameRun = { title: currentIdea.title, frames: [] };
                        }
                        currentIdea.frameRun.frames = [];
                        saveCurrentIdeaState();
                        const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
                        if (existingIdx !== -1) savedIdeas[existingIdx].frameRun = currentIdea.frameRun;
                    }

                    grid.innerHTML = '';
                    for (let i = 1; i <= total; i++) {
                        const placeholderCard = document.createElement('div');
                        placeholderCard.className = 'frame-card placeholder-frame-card';
                        placeholderCard.id = `frame-slot-${i}`;
                        placeholderCard.innerHTML = `
                            <div class="frame-placeholder-spinner">
                                <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                            </div>
                            <span>第 ${String(i).padStart(3, '0')} 帧 (等待中)</span>
                        `;
                        grid.appendChild(placeholderCard);
                    }
                } else if (type === 'frame') {
                    applyFramesProgress('frame', data);
                    const f = data && data.frame;
                    const cur = (data && data.current) || 0;
                    const tot = (data && data.total) || 0;
                    if (cur < tot) {
                        meta.textContent = `正在生成帧序列: ${cur}/${tot} (正在处理第 ${cur + 1} 帧)...`;
                    } else {
                        meta.textContent = `正在生成帧序列: ${cur}/${tot} (已生成完毕，正在整理)...`;
                    }
                    framesFeedQualityLine(f);
                    updateFrameSlotCard(f);
                    applyFrameEventToIdea(f);
                } else if (type === 'frame_start' || type === 'frame_retry' || type === 'queue' || type === 'frame_qa') {
                    const info = applyFramesProgress(type, data);
                    if (info && info.label) meta.textContent = info.label;
                    if (type === 'frame_retry') {
                        const reason = data && data.reason ? `：${data.reason}` : '';
                        framesFeedLine(`🔁 ${(info && info.label) || '质检重试'}${reason}`, 'warn');
                    } else if (type === 'frame_start' && info && info.label) {
                        framesFeedLine(`🎨 ${info.label}`);
                    } else if (type === 'frame_qa') {
                        const seq = data && (data.sequence || data.slot);
                        framesFeedLine(`🧪 IMG ${String(seq || 0).padStart(3, '0')} 质检判定中…`);
                    }
                } else if (type === 'upstream_retry') {
                    // 上游秒报错秒可见：后端每次尝试失败即时推送，不再闷头退避
                    const a = (data && data.attempt) || '?';
                    const m = (data && data.max_attempts) || '?';
                    const tail = data && data.retry_in
                        ? `，${data.retry_in}s 后自动重试（第 ${a}/${m} 次）`
                        : `（第 ${a}/${m} 次，此路终止——若有兜底/收尾会紧随其后，否则任务即将报错结束）`;
                    framesFeedLine(`⚠️ 上游报错：${(data && data.error) || '未知错误'}${tail}`, 'warn');
                    meta.textContent = `上游报错，自动重试中（第 ${a}/${m} 次）...`;
                } else if (type === 'model_fallback') {
                    const to = (data && data.to) || '兜底模型';
                    const seqNo = data && (data.sequence || data.slot);
                    framesFeedLine(`🔀 主模型配额耗尽，切换兜底模型 ${to} 继续渲染 IMG ${String(seqNo || 0).padStart(3, '0')}…`, 'warn');
                    meta.textContent = `主模型配额耗尽，兜底模型 ${to} 渲染中...`;
                } else if (type === 'review_pause') {
                    // 监修模式：关键点暂停，弹出审阅面板等待 采用/重渲
                    showFrameReviewPanel(taskId, data);
                    meta.textContent = (data && data.message) || '监修暂停，等待人工确认...';
                    framesFeedLine(`⏸️ ${(data && data.message) || '监修暂停，等待人工确认…'}`, 'warn');
                } else if (type === 'review_resume') {
                    hideFrameReviewPanel();
                    framesFeedLine(`▶️ ${(data && data.message) || '监修继续'}`);
                } else if (type === 'chain_drift_check' || type === 'anchor_recalibrated' || type === 'reanchor') {
                    // 检查点现实同步/链回望/重锚定：动态流留痕
                    if (data && data.message) {
                        framesFeedLine(`${type === 'reanchor' ? '⚓' : '🔭'} ${data.message}`,
                                       (type === 'reanchor' || (type === 'chain_drift_check' && data.passed === false)) ? 'warn' : undefined);
                    }
                } else if (type === 'sequence_review') {
                    meta.textContent = (data && data.message) || '正在对整套序列做一致性审查...';
                    framesFeedLine(`🔍 ${(data && data.message) || '正在对整套序列做一致性审查...'}`);
                } else if (type === 'sequence_review_result') {
                    if (data && data.message) {
                        framesFeedLine(`${data.passed ? '✅' : '🛠️'} ${data.message}`, data.passed ? 'ok' : 'warn');
                    } else if (data && data.passed) {
                        framesFeedLine('✅ 整套序列一致性审查通过', 'ok');
                    }
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${data.attempt} 次）...`;
                    framesFeedLine(`⚠️ 连接中断，正在重连（第 ${data.attempt} 次）…`, 'warn');
                } else if (type === 'result' || type === 'error') {
                    hideFrameReviewPanel();
                    applyFramesProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;
        hideFrameReviewPanel();
        if (currentFramesController === controller) currentFramesController = null;
        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (watch.status === 'cancelled') {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }
        if (watch.status === 'failed') {
            throw new Error(watch.error || '未知错误');
        }

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result);
            renderFramesForIdea(currentIdea);
            framesFeedLine(`🏁 帧序列全部完成，共 ${(watch.result.frames || []).length} 帧`, 'ok');
            showToast(`已成功生成 ${(watch.result.frames || []).length} 帧连续帧序列图。`, "success");
        }
    } catch (e) {
        if (!isCurrent()) return;
        if (currentFramesController === controller) currentFramesController = null;
        console.error("Failed to generate frames:", e);
        if (e.name === 'AbortError') {
            meta.textContent = '帧序列生成已被用户取消。';
            framesFeedLine('⏹ 帧序列生成已被用户取消', 'warn');
            showToast('已取消帧序列生成', 'info');
        } else {
            meta.textContent = `帧序列生成失败: ${e.message}`;
            framesFeedLine(`❌ 帧序列生成失败：${e.message}`, 'err');
            showToast(`帧序列生成失败: ${e.message}`, "error");
        }

        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (currentIdea) {
            renderFramesForIdea(currentIdea);
        }
    } finally {
        if (isCurrent()) {
            progress.style.display = 'none';
            btn.disabled = false;
            activeBackgroundTasks.frames = false;
            updateTabStatusDot();
            framesFeedSetLive(false);
        }
    }
}

async function streamVideosProgress(taskId) {
    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = document.getElementById('videos-grid');
    if (!btn || !progress || !meta || !grid) return;

    const epoch = ++streamEpochs.videos;
    const isCurrent = () => epoch === streamEpochs.videos;
    if (currentVideosController) {
        try { currentVideosController.abort(); } catch (_) { /* noop */ }
    }
    const controller = new AbortController();
    currentVideosController = controller;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '连接视频生成事件流...';

    activeBackgroundTasks.videos = true;
    updateTabStatusDot();

    let videoProgressState = window.ProgressModel ? ProgressModel.createProgressState('videos') : null;
    const applyVideoProgress = (eventType, eventData) => {
        if (!window.ProgressModel) return null;
        const progressInfo = ProgressModel.normalizeGenerationProgress(eventType, eventData, 'videos', videoProgressState);
        videoProgressState = progressInfo.state;
        setProgressBar('videos', progressInfo);
        return progressInfo;
    };
    applyVideoProgress('queue', { message: '连接视频生成事件流...' });

    // 失败/取消时把还挂着转圈的槽位统一改画失败卡
    const failPendingSlots = (message, labelText) => {
        grid.querySelectorAll('.placeholder-frame-card').forEach(card => {
            const slotMatch = card.id && card.id.match(/video-slot-(\d+)/);
            if (slotMatch) renderVideoSlotFailed(parseInt(slotMatch[1], 10), message, labelText);
        });
    };

    try {
        const watch = await watchTaskUntilTerminal(taskId, {
            label: 'videos',
            signal: controller.signal,
            onEvent: (type, data) => {
                if (!isCurrent()) return;
                if (type === 'start') {
                    applyVideoProgress('start', data);
                    const total = (data && data.total) || 0;
                    const slots = (data && data.slots) || [];
                    meta.textContent = `开始生成共 ${total} 段视频...`;
                    grid.innerHTML = '';
                    const slotsToRender = slots.length ? slots : Array.from({ length: total }, (_, i) => i + 1);
                    slotsToRender.forEach(slotIdx => {
                        const placeholderCard = document.createElement('div');
                        placeholderCard.className = 'frame-card placeholder-frame-card';
                        placeholderCard.id = `video-slot-${slotIdx}`;
                        grid.appendChild(placeholderCard);
                        renderVideoSlotPending(slotIdx, '等待中');
                    });
                } else if (type === 'video_start') {
                    applyVideoProgress('video_start', data);
                    meta.textContent = `正在生成视频: ${data.current}/${data.total} (正在处理第 ${data.index} 段视频)...`;
                    const slot = document.getElementById(`video-slot-${data.index}`);
                    if (slot && slot.classList.contains('placeholder-frame-card')) {
                        renderVideoSlotPending(data.index, '生成中...');
                    }
                } else if (type === 'video_done') {
                    applyVideoProgress('video_done', data);
                    meta.textContent = `正在生成视频: ${data.current}/${data.total}...`;
                    renderVideoSlotDone(data.index, data.video);
                } else if (type === 'video_error') {
                    applyVideoProgress('video_error', data);
                    const msg = (data && data.message) || '生成失败';
                    meta.textContent = `视频 ${data.index} 生成失败: ${msg}`;
                    renderVideoSlotFailed(data.index, msg);
                } else if (type === 'video_skipped') {
                    // 声明式硬切槽位（[CUT]）：不生成片段，按已完成计入进度
                    applyVideoProgress('video_done', data);
                    meta.textContent = `视频 ${data.index} 为声明式硬切槽位，已跳过生成`;
                    if (typeof renderVideoSlotSkippedCut === 'function') {
                        renderVideoSlotSkippedCut(data.index, data && data.message);
                    }
                } else if (type === 'queue') {
                    applyVideoProgress('queue', data);
                    meta.textContent = (data && data.message) || '正在排队等待生成视频...';
                } else if (type === 'merge_skip') {
                    applyVideoProgress('merge_skip', data);
                    meta.textContent = (data && data.message) || '由于存在失败片段，已跳过自动合并。';
                } else if (type === 'merge_start') {
                    applyVideoProgress('merge_start', data);
                    meta.textContent = '正在自动合并并加速视频 (2x Speed)...';
                } else if (type === 'merge_done') {
                    applyVideoProgress('merge_done', data);
                    meta.textContent = '所有视频已成功生成并合并加速！';
                } else if (type === 'merge_error') {
                    applyVideoProgress('merge_error', data);
                    meta.textContent = `自动合并视频失败: ${(data && data.message) || '未知错误'}`;
                    showToast(`自动合并失败: ${(data && data.message) || '未知错误'}`, "warning");
                } else if (type === 'reconnecting') {
                    meta.textContent = `连接中断，正在重连（第 ${data.attempt} 次）...`;
                } else if (type === 'result' || type === 'error') {
                    applyVideoProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;
        if (currentVideosController === controller) currentVideosController = null;
        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (watch.status === 'cancelled') {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }
        if (watch.status === 'failed') {
            throw new Error(watch.error || '未知错误');
        }

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result);
            renderVideosForIdea(currentIdea);
            showToast(`已成功生成 ${(watch.result.videos || []).length} 段连续视频。`, "success");
        }
    } catch (e) {
        if (!isCurrent()) return;
        if (currentVideosController === controller) currentVideosController = null;
        console.error("Failed to generate videos:", e);

        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (e.name === 'AbortError') {
            meta.textContent = '视频生成已被用户取消。';
            showToast('已取消视频生成', 'info');
            failPendingSlots('已被用户取消', '未生成');
        } else {
            meta.textContent = `视频生成失败: ${e.message}`;
            showToast(`视频生成失败: ${e.message}`, "error");
            failPendingSlots(e.message || '生成失败', '生成失败');

            await reloadManifestIntoIdea();
            if (currentIdea && currentIdea.frameRun) renderVideosForIdea(currentIdea);
        }
    } finally {
        if (isCurrent()) {
            progress.style.display = 'none';
            btn.disabled = false;
            activeBackgroundTasks.videos = false;
            updateTabStatusDot();
        }
    }
}

async function streamCoverProgress(taskId) {
    const loadingEl = document.getElementById('cover-img-loading');
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const makeBtn = document.getElementById('make-cover-btn');
    if (!loadingEl || !placeholderEl || !displayEl || !makeBtn) return;

    loadingEl.style.display = 'flex';
    placeholderEl.style.display = 'none';
    displayEl.style.display = 'none';
    makeBtn.disabled = true;

    activeBackgroundTasks.cover = true;
    updateTabStatusDot();

    const epoch = ++streamEpochs.cover;
    const isCurrent = () => epoch === streamEpochs.cover;

    try {
        const watch = await watchTaskUntilTerminal(taskId, { label: 'cover' });

        if (!isCurrent()) return;
        activeBackgroundTasks.coverTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (watch.status === 'failed' || watch.status === 'cancelled') {
            throw new Error(watch.error || '封面任务未完成');
        }
        if (!watch.result) {
            throw new Error("模型未返回有效结果");
        }

        const data = watch.result;
        const imageUrl = extractImageUrl(data.content);
        const englishTitle = data.english_title;

        if (!imageUrl) {
            throw new Error("无法从模型响应中解析出有效的封面图片 URL");
        }

        if (!currentIdea.covers) {
            currentIdea.covers = [];
        }
        currentIdea.covers.push(imageUrl);
        if (englishTitle) {
            currentIdea.english_title = englishTitle;
        }
        // 旧创意补齐发布用双语标题行（后端只在缺字段时才生成并随结果返回）
        if (data.social_title_en && !currentIdea.social_title_en) {
            currentIdea.social_title_en = data.social_title_en;
        }
        if (data.social_title_cn && !currentIdea.social_title_cn) {
            currentIdea.social_title_cn = data.social_title_cn;
        }
        saveCurrentIdeaState();
        renderIdeaTitles(currentIdea);

        const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
        if (existingIdx !== -1) {
            savedIdeas[existingIdx].covers = currentIdea.covers;
            if (englishTitle) {
                savedIdeas[existingIdx].english_title = englishTitle;
            }
            if (currentIdea.social_title_en) {
                savedIdeas[existingIdx].social_title_en = currentIdea.social_title_en;
            }
            if (currentIdea.social_title_cn) {
                savedIdeas[existingIdx].social_title_cn = currentIdea.social_title_cn;
            }
            await saveLibrary();
        }

        renderCoversForIdea(currentIdea, currentIdea.covers.length - 1);
        showToast("封面图制作成功！", "success");
    } catch (e) {
        if (!isCurrent()) return;
        console.error("Failed to generate cover:", e);
        showToast(`封面制作失败: ${e.message}`, "error");

        activeBackgroundTasks.coverTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (currentIdea && currentIdea.covers && currentIdea.covers.length > 0) {
            renderCoversForIdea(currentIdea, currentIdea.covers.length - 1);
        } else {
            placeholderEl.style.display = 'flex';
        }
    } finally {
        if (isCurrent()) {
            loadingEl.style.display = 'none';
            makeBtn.disabled = false;
            activeBackgroundTasks.cover = false;
            updateTabStatusDot();
        }
    }
}

function retryGeneration() {
    if (generationState.lastParams) {
        generateIdea(generationState.lastParams);
    } else {
        showToast("暂无可重试的参数记录", "error");
    }
}

function updateLoadingStep(id, status) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = status;
}

function setupLoadingSteps() {
    const step1 = document.getElementById('step-1');
    const step2 = document.getElementById('step-2');
    const step3 = document.getElementById('step-3');
    const step4 = document.getElementById('step-4');
    if (!step1 || !step2 || !step3 || !step4) return;

    // reset classes
    step1.className = 'pending';
    step2.className = 'pending';
    step3.className = 'pending';
    step4.className = 'pending';

    step1.textContent = '解析主题，拆解场景变量与施工节拍...';
    step2.textContent = '装配 Drift Lock 与九宫格锚点...';
    step3.textContent = '渲染 IMAGE 锚点与连续动作 VIDEO 链...';
    step4.textContent = '运行质量门 + 工序与场景一致性二次校验与修复...';
}

// Render the composed prompt set + quality audit report to the DOM.
// Function renderIdea moved to modular JS file

// Show the result of the second-pass construction-order / causality check.
// Function renderRepairBanner moved to modular JS file

// Split a Markdown table row into trimmed cells, dropping the leading/trailing
// empties produced by border pipes.
// Function splitTableRow moved to modular JS file

// Minimal Markdown renderer for the audit report: handles GitHub-style tables and
// plain paragraphs, escaping all HTML.
// Function renderAuditMarkdown moved to modular JS file

// Save currently active idea to Local Storage Library
function saveCurrentIdea() {
    if (!currentIdea) return;
    
    // Check if already saved
    if (savedIdeas.some(item => item.title === currentIdea.title)) {
        showToast("该创意已存在于点子库中", "error");
        return;
    }
    
    savedIdeas.unshift({ ...currentIdea });
    saveLibrary();
    updateFavoriteButtonState();
    showToast("成功保存至点子库！", "success");
}

// Populate Saved Ideas Library Sidebar
function renderLibrary() {
    const list = document.getElementById('library-list');
    if (!list) return;
    
    list.innerHTML = '';
    
    const query = (document.getElementById('library-search')?.value || '').trim().toLowerCase();
    const themeFilter = document.getElementById('library-filter-theme')?.value || '';
    const timeSort = document.getElementById('library-filter-time')?.value || 'newest';
    
    let filtered = [...savedIdeas];
    
    // Search filter
    if (query) {
        filtered = filtered.filter(idea => 
            (idea.title || '').toLowerCase().includes(query) ||
            (idea.theme || '').toLowerCase().includes(query) ||
            (idea.prompt_block || '').toLowerCase().includes(query)
        );
    }
    
    // Theme filter
    if (themeFilter) {
        filtered = filtered.filter(idea => idea.theme === themeFilter);
    }
    
    // Time sorting
    if (timeSort === 'oldest') {
        filtered.reverse();
    }
    
    if (filtered.length === 0) {
        list.innerHTML = `
            <div class="library-empty">
                没有找到匹配的点子。
            </div>
        `;
        return;
    }
    
    filtered.forEach(idea => {
        const card = document.createElement('div');
        card.className = 'saved-card';
        card.setAttribute('data-id', idea.id);
        
        let thumbHtml = `<div class="saved-card-thumb-icon">💡</div>`;
        if (idea.covers && idea.covers.length > 0) {
            const coverUrl = idea.covers[0];
            const isSafe = coverUrl.startsWith('http://') || 
                           coverUrl.startsWith('https://') || 
                           coverUrl.startsWith('data:image/') ||
                           coverUrl.startsWith('/') ||
                           coverUrl.startsWith('outputs/');
            if (isSafe) {
                thumbHtml = `<img src="${coverUrl}" alt="Thumbnail" onerror="this.onerror=null; this.outerHTML='<div class=&quot;saved-card-thumb-icon&quot;>💡</div>';">`;
            }
        }
        
        card.innerHTML = `
            <div class="saved-card-thumb">
                ${thumbHtml}
            </div>
            <div class="saved-card-info-content">
                <div class="saved-card-header">
                    <h4 class="safe-title"></h4>
                    <span class="saved-card-date">${idea.timestamp ? idea.timestamp.split(' ')[0] : '未知时间'}</span>
                </div>
                <div class="saved-card-footer">
                    <span class="saved-card-theme safe-theme"></span>
                    <button class="delete-saved-btn" title="删除">
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                    </button>
                </div>
            </div>
        `;
        
        card.querySelector('.safe-title').textContent = idea.title || '未命名创意';
        card.querySelector('.safe-theme').textContent = idea.theme || '未命名主题';
        
        card.addEventListener('click', (e) => {
            if (e.target.closest('.delete-saved-btn') || e.target.closest('.delete-confirm-overlay')) {
                e.stopPropagation();
                showDeleteConfirm(card, idea.id);
                return;
            }
            loadSavedIdea(idea);
        });
        
        list.appendChild(card);
    });
}

async function deleteFromLibrary(id) {
    const idea = savedIdeas.find(item => item.id === id);

    if (idea && idea.title) {
        try {
            await fetch('/api/library/delete_item', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: idea.title, covers: idea.covers || [] })
            });
        } catch (e) {
            console.error("Delete idea output files request failed:", e);
        }
    }

    savedIdeas = savedIdeas.filter(item => item.id !== id);
    saveLibrary();
    showToast("已从点子库删除，生成的图片/视频文件已一并清理", "success");
    updateFavoriteButtonState();
}

function loadSavedIdea(idea) {
    currentIdea = idea;
    saveCurrentIdeaState();
    renderIdea(idea);
    
    document.getElementById('output-placeholder-view').classList.remove('active');
    document.getElementById('output-loading-view').classList.remove('active');
    
    const errorView = document.getElementById('output-error-view');
    if (errorView) errorView.style.display = 'none';
    
    document.getElementById('output-content-view').classList.add('active');
    
    switchTab('overview');
    showToast("已载入收藏的创意", "success");
    updateActiveGenerationBanner();
}

// Export Library to JSON
function exportAllLibrary() {
    if (savedIdeas.length === 0) {
        showToast("库中暂无点子可供导出", "error");
        return;
    }
    
    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(savedIdeas, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute("href", dataStr);
    downloadAnchor.setAttribute("download", `spark_creative_library_${Date.now()}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
}

// Import Library from JSON
function importLibrary(e) {
    const file = e.target.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = function(evt) {
        try {
            const imported = JSON.parse(evt.target.result);
            if (Array.isArray(imported)) {
                // Merge without duplicates based on title
                const existingTitles = new Set(savedIdeas.map(item => item.title));
                let count = 0;
                imported.forEach(item => {
                    if (item.title && !existingTitles.has(item.title)) {
                        if (!item.id) item.id = Date.now().toString() + Math.random();
                        savedIdeas.push(item);
                        count++;
                    }
                });
                saveLibrary();
                showToast(`成功导入 ${count} 个新创意点子！`, "success");
            } else {
                throw new Error("Invalid file structure");
            }
        } catch (err) {
            showToast("导入失败，文件格式有误", "error");
            console.error(err);
        }
    };
    reader.readAsText(file);
}

// Export single idea to formatted Markdown
function exportIdeaMarkdown() {
    if (!currentIdea) return;

    let coversMarkdown = '';
    if (currentIdea.covers && currentIdea.covers.length > 0) {
        coversMarkdown = '\n---\n\n## 视频封面图\n\n' + currentIdea.covers.map((c, idx) => `### 封面 ${idx + 1}\n![Cover ${idx + 1}](${c})`).join('\n\n') + '\n';
    }

    let framesMarkdown = '';
    if (currentIdea.frameRun && currentIdea.frameRun.frames && currentIdea.frameRun.frames.length > 0) {
        framesMarkdown = '\n---\n\n## Sequential Frames\n\n'
            + `Manifest: ${currentIdea.frameRun.manifest || ''}\n\n`
            + currentIdea.frameRun.frames.map(f => `### img_${String(f.sequence).padStart(3, '0')}\n![Frame ${f.sequence}](${f.url})`).join('\n\n')
            + '\n';
    }

    const tiktokMeta = getIdeaTikTokMeta(currentIdea);
    let hookMarkdown = `\n*TikTok US 标题和 tags：${tiktokMeta.english}*\n*国内社媒标题和话题：${tiktokMeta.chinese}*\n`;

    const markdownContent = `# ${currentIdea.title}
*场景主题：${currentIdea.theme}*
*创意尺度：${currentIdea.creativity}*${hookMarkdown}
*生成时间：${currentIdea.timestamp || new Date().toLocaleString()}*

---

## 复制即用提示词集（图片 1–N+1 / 视频 1–N）

\`\`\`text
${currentIdea.prompt_block || ''}
\`\`\`
${coversMarkdown}
${framesMarkdown}
---

## 工序与场景一致性校验与修复

${currentIdea.repair_md || '（本次未执行工序与场景一致性校验）'}

---

## 提示词质量审核报告

${currentIdea.audit_md || '（本次未返回审核报告）'}
`;

    const blob = new Blob([markdownContent], { type: 'text/markdown;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute("href", url);
    downloadAnchor.setAttribute("download", `Spark_Idea_${currentIdea.title.replace(/\s+/g, '_')}.md`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
}

// Copy the full prompt set to clipboard
function copyTikTokMetaToClipboard() {
    const meta = getIdeaTikTokMeta(currentIdea);
    copyText(meta.english).then(() => {
        showToast("TikTok 标题和 tags 已复制！", "success");
    }).catch(err => {
        showToast("复制失败，请手动选择复制", "error");
        console.error(err);
    });
}

function copyTikTokMetaCnToClipboard() {
    const meta = getIdeaTikTokMeta(currentIdea);
    copyText(meta.chinese).then(() => {
        showToast("中文标题和话题已复制！", "success");
    }).catch(err => {
        showToast("复制失败，请手动选择复制", "error");
        console.error(err);
    });
}

function copyPromptToClipboard() {
    const text = (currentIdea && currentIdea.prompt_block) || document.getElementById('idea-prompt-block').textContent;
    copyText(text).then(() => {
        showToast("提示词集已复制到剪贴板！", "success");
    }).catch(err => {
        showToast("复制失败，请手动选择复制", "error");
        console.error(err);
    });
}

// Toast Alert System
// Function showToast moved to modular JS file

// Generate the complete IMAGE prompt chain as ordered still frames.
async function generateFrames() {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }

    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    if (!btn || !progress || !meta) return;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '准备生成帧序列...';

    activeBackgroundTasks.frames = true;
    updateTabStatusDot();

    try {
        const response = await fetch('/api/generate_frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(currentIdea),
                prompt_block: currentIdea.prompt_block
            })
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        
        activeBackgroundTasks.framesTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();

        await streamFramesProgress(taskId);
    } catch (e) {
        console.error("Failed to generate frames:", e);
        meta.textContent = `帧序列生成失败: ${e.message}`;
        showToast(`帧序列生成失败: ${e.message}`, "error");

        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (currentIdea) {
            renderFramesForIdea(currentIdea);
        }
        progress.style.display = 'none';
        btn.disabled = false;
        activeBackgroundTasks.frames = false;
        updateTabStatusDot();
    }
}


// Function renderFramesForIdea moved to modular JS file

// Function renderVideosForIdea moved to modular JS file

async function generateVideos() {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }

    if (!currentIdea.frameRun || !currentIdea.frameRun.frames || currentIdea.frameRun.frames.length === 0) {
        showToast("请先生成帧序列图！", "error");
        return;
    }

    // Check for frames that failed the post-render sequence consistency review
    if (currentIdea.frameRun && currentIdea.frameRun.frames) {
        const failedFrames = currentIdea.frameRun.frames.filter(f => f.quality_gate === 'vlm_qa_failed' || f.quality_gate === 'sequence_review_flagged');
        if (failedFrames.length > 0) {
            const frameSeqs = failedFrames.map(f => f.sequence).join(', ');
            const confirmed = await customConfirm(`⚠️ 警告：检测到第 ${frameSeqs} 帧未通过一致性审查。\n\n如果强行生成视频，对应的视频分段可能存在跳变、无动作或动作不一致的缺陷。\n\n确定要强制继续生成视频吗？`);
            if (!confirmed) {
                return;
            }
        }
    }

    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = document.getElementById('videos-grid');
    if (!btn || !progress || !meta || !grid) return;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '准备生成视频序列...';

    activeBackgroundTasks.videos = true;
    updateTabStatusDot();

    try {
        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                title: getIdeaSaveTitle(currentIdea),
                prompt_block: currentIdea.prompt_block
            })
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;
        
        activeBackgroundTasks.videosTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();

        await streamVideosProgress(taskId);
    } catch (e) {
        console.error("Failed to generate videos:", e);
        meta.textContent = `视频生成失败: ${e.message}`;
        showToast(`视频生成失败: ${e.message}`, "error");

        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        const placeholders = grid.querySelectorAll('.placeholder-frame-card');
        placeholders.forEach(card => {
            const slotMatch = card.id && card.id.match(/video-slot-(\d+)/);
            const slotIdx = slotMatch ? parseInt(slotMatch[1]) : null;
            if (slotIdx !== null) {
                card.className = 'frame-card video-failed-card';
                card.innerHTML = `
                    <div class="video-failed-placeholder">
                        <span class="error-icon">⚠️</span>
                        <span class="error-text" title="${e.message || '生成失败'}">生成失败</span>
                        <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${slotIdx}">重试</button>
                    </div>
                    <span>VID ${String(slotIdx).padStart(3, '0')}</span>
                `;
                card.querySelector('.retry-video-btn').addEventListener('click', (ev) => {
                    ev.stopPropagation();
                    retrySingleVideo(slotIdx);
                });
            }
        });

        progress.style.display = 'none';
        btn.disabled = false;
        activeBackgroundTasks.videos = false;
        updateTabStatusDot();
    }
}

async function mergeVideos(force = false) {
    if (!currentIdea || !currentIdea.title) {
        showToast("请先激发一个创意点子并生成视频！", "error");
        return;
    }

    const mergeBtn = document.getElementById('merge-videos-btn');
    const videosMeta = document.getElementById('videos-meta');
    if (!mergeBtn || !videosMeta) return;

    const originalText = mergeBtn.innerHTML;
    mergeBtn.disabled = true;
    mergeBtn.innerHTML = `
        <div class="cover-spinner" style="width:14px; height:14px; border-width:2px; margin-bottom:0; display:inline-block; vertical-align:middle; margin-right:6px;"></div>
        <span>${force ? '正在占位合并中...' : '正在合并中...'}</span>
    `;
    videosMeta.textContent = force
        ? "正在用占位帧填充缺口并合并预览片，请稍候..."
        : "正在调用 FFmpeg 合并并加速视频，此过程可能需要几秒钟，请稍候...";

    try {
        const response = await fetch('/api/merge_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: getIdeaSaveTitle(currentIdea),
                force: !!force
            })
        });

        const data = await response.json().catch(() => ({}));

        // 合成门禁拦截：缺失/串片片段 → 给出「重试」「强制合并」两条出路
        if (response.status === 409 && data.status === 'blocked') {
            renderMergeBlocked(data);
            return;
        }

        if (!response.ok) {
            throw new Error(data.message || data.error || `HTTP ${response.status}`);
        }

        if (data.status === 'ok') {
            if (!currentIdea.frameRun) {
                currentIdea.frameRun = {};
            }
            currentIdea.frameRun.merged_video = data.merged_video;
            saveCurrentIdeaState();

            const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = currentIdea.frameRun;
                await saveLibrary();
            }

            renderVideosForIdea(currentIdea);

            const mv = data.merged_video || {};
            if (mv.partial) {
                const slots = (mv.placeholder_slots || []).join(', ');
                showToast("已生成占位预览片（缺口用起始帧填充）", "success");
                videosMeta.innerHTML = `⚠️ 占位预览已生成：槽位 <b>${escapeHtml(slots)}</b> 为「缺失/串片」占位帧，成片仅供预览（无音轨）。建议重试这些片段后重新合并以获得完整成片。`;
            } else {
                showToast("视频合并并加速成功！", "success");
                videosMeta.textContent = "视频合并已完成！";
            }
        } else {
            throw new Error(data.message || '合并失败');
        }
    } catch (e) {
        console.error("Failed to merge videos:", e);
        showToast(`合并视频失败: ${e.message}`, "error");
        videosMeta.textContent = `合并视频失败: ${e.message}`;
    } finally {
        mergeBtn.disabled = false;
        mergeBtn.innerHTML = originalText;
    }
}

// 合成被门禁拦截时，在 videos-meta 区域渲染可操作面板：
//   ① 重试缺失/串片片段并自动重合   ② 强制合并（占位帧填充预览）
function renderMergeBlocked(data) {
    const videosMeta = document.getElementById('videos-meta');
    if (!videosMeta) return;

    const missing = (data.missing || []).map(Number).filter(Number.isFinite);
    const mismatched = (data.mismatched || []).map(Number).filter(Number.isFinite);
    const all = [...new Set([...missing, ...mismatched])].sort((a, b) => a - b);

    const parts = [];
    if (missing.length) parts.push(`缺失/失败：槽位 <b>${escapeHtml(missing.join(', '))}</b>`);
    if (mismatched.length) parts.push(`疑似串片：槽位 <b>${escapeHtml(mismatched.join(', '))}</b>`);

    videosMeta.innerHTML = `
        <div class="merge-blocked" style="text-align:left; line-height:1.7;">
            <div style="color:#f6c453; margin-bottom:8px;">⚠️ 已拦截合并（避免成片硬跳/串片）：${parts.join('；')}。</div>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button type="button" class="action-btn text-btn" id="merge-retry-missing-btn">🔁 重试这些片段并合并 (${all.length})</button>
                <button type="button" class="action-btn text-btn" id="merge-force-btn">⛰️ 强制合并（占位预览）</button>
            </div>
        </div>`;

    const retryBtn = document.getElementById('merge-retry-missing-btn');
    if (retryBtn) retryBtn.addEventListener('click', () => {
        if (typeof retryMissingVideos === 'function') {
            retryMissingVideos(all);
        } else {
            showToast('重试功能不可用', 'error');
        }
    });

    const forceBtn = document.getElementById('merge-force-btn');
    if (forceBtn) forceBtn.addEventListener('click', async () => {
        const ok = await customConfirm('强制合并会把缺失/串片的片段用「起始帧定格 + 缺失标注」填充，生成的成片仅供预览（无音轨），缺口不会被静默丢弃。确定继续吗？');
        if (ok) mergeVideos(true);
    });
}


// Function retrySingleFrame moved to modular JS file

// Function retrySingleVideo moved to modular JS file

// Generate TikTok 9:16 Video Cover using gemini-3.1-flash-image
async function generateCover() {
    if (!currentIdea) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    
    const loadingEl = document.getElementById('cover-img-loading');
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const makeBtn = document.getElementById('make-cover-btn');
    if (!loadingEl || !placeholderEl || !displayEl || !makeBtn) return;
    
    // Set loading state
    loadingEl.style.display = 'flex';
    placeholderEl.style.display = 'none';
    displayEl.style.display = 'none';
    makeBtn.disabled = true;
    
    activeBackgroundTasks.cover = true;
    updateTabStatusDot();
    
    try {
        const response = await fetch('/api/generate_cover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                id: currentIdea.id,
                title: currentIdea.title,
                theme: currentIdea.theme,
                prompt_block: currentIdea.prompt_block
            })
        });
        
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        
        const taskId = data.task_id;
        activeBackgroundTasks.coverTaskId = taskId;
        saveActiveBackgroundTasksToLocalStorage();
        
        await streamCoverProgress(taskId);
    } catch (e) {
        console.error("Failed to generate cover:", e);
        showToast(`封面制作失败: ${e.message}`, "error");
        
        activeBackgroundTasks.coverTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        // Restore state based on whether we already have covers
        if (currentIdea.covers && currentIdea.covers.length > 0) {
            renderCoversForIdea(currentIdea, currentIdea.covers.length - 1);
        } else {
            placeholderEl.style.display = 'flex';
        }
        loadingEl.style.display = 'none';
        makeBtn.disabled = false;
        activeBackgroundTasks.cover = false;
        updateTabStatusDot();
    }
}

// Render the covers and thumbnails for the active idea
// Function renderCoversForIdea moved to modular JS file

// Robust extractor for Markdown images, raw URLs, or Base64 images from text content
// Function extractImageUrl moved to modular JS file

// =====================================================================
// Custom Presets Management
// =====================================================================
// Function loadCustomPresets moved to modular JS file

async function saveCustomPreset() {
    const presetName = await customPrompt("请输入此自定义预设的名称 (例如: 极简清水舱, 脑洞自然舱):");
    if (presetName === null) return; // User cancelled
    const trimmedName = presetName.trim();
    if (!trimmedName) {
        showToast("预设名称不能为空", "error");
        return;
    }

    const activeThemeBtn = document.querySelector('#theme-selector .theme-btn.active');
    const selectedTheme = activeThemeBtn ? activeThemeBtn.dataset.value : 'hollow_oak';
    
    const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
        .map(node => node.dataset.value);

    customPresets[trimmedName] = {
        theme: selectedTheme,
        anchors: activeAnchors,
        complexity: parseInt(document.getElementById('slider-complexity').value, 10),
        budget: parseInt(document.getElementById('slider-budget').value, 10),
        ratio: parseInt(document.getElementById('slider-ratio').value, 10),
        creativity: parseInt(document.getElementById('slider-creativity').value, 10),
        beats: parseInt(document.getElementById('slider-beats').value, 10)
    };

    localStorage.setItem('spark_custom_presets', JSON.stringify(customPresets));
    renderCustomPresets();
    showToast(`自定义预设 "${trimmedName}" 保存成功！`, "success");
}

async function deleteCustomPreset(name, event) {
    if (event) event.stopPropagation();
    if (!customPresets[name]) return;
    
    const confirmed = await customConfirm(`确定删除自定义预设 "${name}" 吗？`);
    if (confirmed) {
        delete customPresets[name];
        localStorage.setItem('spark_custom_presets', JSON.stringify(customPresets));
        renderCustomPresets();
        showToast(`已删除预设 "${name}"`, "success");
    }
}

// Function applyCustomPreset moved to modular JS file

// Function renderCustomPresets moved to modular JS file

// =====================================================================
// Smart Randomizer
// =====================================================================
// Function randomizeDimensions moved to modular JS file

// 提示词槽位卡片相关的 setupPromptControls / applySlotFilter / copySlotsByType 已移除：
// 提示词页现在只展示原始 Markdown 块，不再解析成槽位卡片（parsePromptBlock 仍保留供帧序列使用）。

// =====================================================================
// Drag & Drop and Hotkeys Setup
// =====================================================================
function setupDragAndDrop() {
    const drawer = document.getElementById('library-drawer');
    if (!drawer) return;
    
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        drawer.addEventListener(eventName, preventDefaults, false);
    });
    
    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }
    
    ['dragenter', 'dragover'].forEach(eventName => {
        drawer.addEventListener(eventName, () => drawer.classList.add('dragover'), false);
    });
    
    ['dragleave', 'drop'].forEach(eventName => {
        drawer.addEventListener(eventName, () => drawer.classList.remove('dragover'), false);
    });
    
    drawer.addEventListener('drop', handleDrop, false);
    
    function handleDrop(e) {
        const dt = e.dataTransfer;
        const files = dt.files;
        
        if (files.length > 0) {
            const file = files[0];
            if (file.type === "application/json" || file.name.endsWith('.json')) {
                const fakeEvent = { target: { files: [file] } };
                importLibrary(fakeEvent);
            } else {
                showToast("仅支持导入 JSON 格式的库文件", "error");
            }
        }
    }
}

function handleGlobalHotkeys(e) {
    // Alt + R: Randomize dimensions
    if (e.altKey && e.key.toLowerCase() === 'r') {
        e.preventDefault();
        const randBtn = document.getElementById('randomize-btn');
        if (randBtn) randBtn.click();
    }
    
    // Alt + L: Toggle My Library Drawer
    if (e.altKey && e.key.toLowerCase() === 'l') {
        e.preventDefault();
        const toggleLibBtn = document.getElementById('toggle-library-btn');
        const closeLibBtn = document.getElementById('close-library-btn');
        const libraryDrawer = document.getElementById('library-drawer');
        
        if (libraryDrawer) {
            if (libraryDrawer.classList.contains('active')) {
                if (closeLibBtn) closeLibBtn.click();
            } else {
                if (toggleLibBtn) toggleLibBtn.click();
            }
        }
    }
    
    // Alt + T: Toggle Tasks Drawer
    if (e.altKey && e.key.toLowerCase() === 't') {
        e.preventDefault();
        const toggleTasksBtn = document.getElementById('toggle-tasks-btn');
        const closeTasksBtn = document.getElementById('close-tasks-btn');
        const tasksDrawer = document.getElementById('tasks-drawer');
        
        if (tasksDrawer) {
            if (tasksDrawer.classList.contains('active')) {
                if (closeTasksBtn) closeTasksBtn.click();
            } else {
                if (toggleTasksBtn) toggleTasksBtn.click();
            }
        }
    }
    
    // Alt + S: Toggle Settings Modal
    if (e.altKey && e.key.toLowerCase() === 's') {
        e.preventDefault();
        const settingsModal = document.getElementById('settings-modal');
        const openSettingsBtn = document.getElementById('open-settings-btn');
        const closeSettingsBtn = settingsModal ? settingsModal.querySelector('.close-btn') : null;
        
        if (settingsModal) {
            if (settingsModal.classList.contains('active')) {
                if (closeSettingsBtn) closeSettingsBtn.click();
            } else {
                if (openSettingsBtn) openSettingsBtn.click();
            }
        }
    }
    
    // Esc: Close modal / drawer / overlays
    if (e.key === 'Escape') {
        // Close modal
        const settingsModal = document.getElementById('settings-modal');
        if (settingsModal && settingsModal.classList.contains('active')) {
            const closeSettingsBtn = settingsModal.querySelector('.close-btn');
            if (closeSettingsBtn) closeSettingsBtn.click();
        }
        
        // Close drawer
        const libraryDrawer = document.getElementById('library-drawer');
        if (libraryDrawer && libraryDrawer.classList.contains('active')) {
            const closeLibBtn = document.getElementById('close-library-btn');
            if (closeLibBtn) closeLibBtn.click();
        }
        
        // Close delete confirmation overlays
        document.querySelectorAll('.delete-confirm-overlay').forEach(overlay => overlay.remove());
    }
}

// =====================================================================
// Tab Loading Dot Progress Perception
// =====================================================================
function updateTabStatusDot() {
    const dot = document.getElementById('overview-status-dot');
    if (!dot) return;
    
    if (activeBackgroundTasks.cover || activeBackgroundTasks.frames || activeBackgroundTasks.videos) {
        dot.style.display = 'inline-block';
        dot.className = 'tab-status-dot active';
        dot.title = `后台生成中: ${activeBackgroundTasks.cover ? '封面图 ' : ''}${activeBackgroundTasks.frames ? '帧序列 ' : ''}${activeBackgroundTasks.videos ? '视频序列' : ''}`;
    } else {
        dot.style.display = 'inline-block';
        dot.className = 'tab-status-dot completed';
        dot.title = '后台任务已全部完成';
        setTimeout(() => {
            if (!activeBackgroundTasks.cover && !activeBackgroundTasks.frames && !activeBackgroundTasks.videos) {
                dot.style.display = 'none';
            }
        }, 3000);
    }
}

// Active Generation Banner display updater
function updateActiveGenerationBanner() {
    const banner = document.getElementById('active-generation-banner');
    if (!banner) return;
    
    const loadingView = document.getElementById('output-loading-view');
    const isComposing = generationState.status === 'composing';
    const isLoadingVisible = loadingView && loadingView.classList.contains('active');
    
    if (isComposing && !isLoadingVisible) {
        banner.style.display = 'flex';
    } else {
        banner.style.display = 'none';
    }
}

// ==========================================================================
// Upstream Topic Ideation Engine (P2)
// --------------------------------------------------------------------------
let currentIdeatedIdeas = [];

async function loadIdeationCards(force = false) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;

    // 灵感推荐由联网参考案例库驱动（js/trend_refs.js）：勾选了参考就直接从选中
    // 案例取材；没勾选则后端自动联网搜索（结果沉淀回案例库）
    const selIds = (typeof getSelectedTrendRefIds === 'function') ? getSelectedTrendRefIds() : [];
    const selKey = selIds.slice().sort().join(',');

    if (!force) {
        const cached = localStorage.getItem('ideation_cached_ideas');
        // 缓存与生成时勾选的联网参考集合绑定：勾选变了缓存即视为过期重新生成，
        // 不能把按别的参考取材的灵感当成这批参考的
        const cachedSel = localStorage.getItem('ideation_cached_trend_sel');
        if (cached && cachedSel === selKey) {
            try {
                const parsed = JSON.parse(cached);
                if (parsed && Array.isArray(parsed) && parsed.length > 0) {
                    currentIdeatedIdeas = parsed;
                    try {
                        currentIdeationTrendRefs = JSON.parse(localStorage.getItem('ideation_cached_trend_refs')) || [];
                    } catch (e2) {
                        currentIdeationTrendRefs = [];
                    }
                    renderIdeationCards(parsed);
                    return;
                }
            } catch (e) {
                console.error("Failed to parse cached ideas:", e);
            }
        }
    }

    container.innerHTML = selIds.length > 0
        ? `<div class="ideation-loading">正在从选中的 ${selIds.length} 条联网参考案例中取材激发灵感，请稍候...</div>`
        : `<div class="ideation-loading">正在自动联网搜索趋势并寻找灵感中，请稍候...</div>`;

    try {
        const response = await fetch('/api/ideate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                config: config,
                count: 4,
                trend_ref_ids: selIds
            })
        });

        const data = await response.json();
        if (data.status === 'ok' && data.ideas) {
            currentIdeatedIdeas = data.ideas;
            currentIdeationTrendRefs = Array.isArray(data.trend_refs) ? data.trend_refs : [];
            localStorage.setItem('ideation_cached_ideas', JSON.stringify(data.ideas));
            localStorage.setItem('ideation_cached_trend_refs', JSON.stringify(currentIdeationTrendRefs));
            localStorage.setItem('ideation_cached_trend_sel', selKey);
            renderIdeationCards(data.ideas);
            // 自动搜索路径会往案例库沉淀新参考，让左侧列表跟上
            if (selIds.length === 0 && typeof loadTrendRefs === 'function') loadTrendRefs();
        } else {
            container.innerHTML = `<div class="ideation-error">加载失败: ${data.message || '未知错误'}</div>`;
        }
    } catch (e) {
        console.error("Failed to load ideated cards:", e);
        container.innerHTML = `<div class="ideation-error">加载失败，请检查网络或配置</div>`;
    }
}

// Function renderIdeationCards moved to modular JS file

// Function selectIdeationCard moved to modular JS file

// Function composeIdeationCard moved to modular JS file

// Function mapEnglishCarrierToValue moved to modular JS file

// Function mapTwistToAnchorValue moved to modular JS file

/* 暗夜模式 toggle 已抽出到 js/theme_toggle.js(双前端共享,index.html 加载)*/

// Local Service Logs Stream
// Function initLocalServiceLogs moved to modular JS file

// --- LIGHTBOX SYSTEM 已整体抽出到 js/lightbox.js ---
// 全局控制器函数 + 状态(lightboxItems/lightboxActiveIndex)+ 初始化均在该共享模块;
// 本文件其余处仍直接调用全局 openLightbox()。
