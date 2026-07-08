/* =====================================================================
   SPARK Creative Idea Generator - Core Javascript Logic
   ===================================================================== */

// Default Configurations
const DEFAULT_CONFIG = {
    baseUrl: 'http://127.0.0.1:8046/v1',
    // Key intentionally NOT shipped to the browser. In server-managed (external) mode the
    // backend supplies the key from server_config.json. For local self-use, enter your key
    // once in the ⚙️ 配置中心 (it persists in this browser's localStorage).
    apiKey: '',
    model: 'gemini-3-flash-agent',
    imageModel: 'nano-banana-2',
    // 帧序列生成方式: 'api'（LLM 网关）| 'google_fx'（AdsPower 浏览器 UI 自动化）
    imageBackend: 'api',
    googleFxImageModel: 'Nano Banana 2',
    videoModel: 'Veo 3.1 - Lite [Lower Priority]',
    googleFxIpRotateRequests: 5,
    imageAspectRatio: '9:16',
    imageQuality: '2K'
};

// Global State
let config = { ...DEFAULT_CONFIG };

// HTML Escape Utility to prevent XSS / render breakage
// Function escapeHtml moved to modular JS file

// ---- External (server-managed) mode: access code + automatic header injection ----
// All /api/* requests transparently carry the access code (if one is set). If the server
// rejects it (401) we prompt for a new one and retry once. Implemented by wrapping window.fetch
// so every existing call site is covered without changes.
let ACCESS_CODE = localStorage.getItem('spark_access_code') || '';

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
let savedIdeas = [];
let currentIdea = null;
let activeInputTab = 'text';
let selectedVideoFile = null;

let customPresets = {};
let currentSlotFilter = 'all';
let activeBackgroundTasks = {
    cover: false,
    frames: false,
    videos: false
};

let generationState = {
    status: 'idle', // idle | composing | error
    startTime: 0,
    timerInterval: null,
    lastParams: null
};

// Global controllers for cancellation
let currentGenerationController = null;
let currentFramesController = null;
let currentVideosController = null;

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
    setupPromptControls();
    setupDragAndDrop();
    setupVideoUploadDragAndDrop();
    resumeActiveTaskIfExists();
    resumeActiveBackgroundTasksIfExists();
    startGlobalTasksBadgePolling();
    loadIdeationCards();
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
        beats: 15
    },
    industrial_relic: {
        theme: 'water_tower',
        anchors: ['single_slab_counter', 'living_wood_stair'],
        complexity: 3,
        budget: 2,
        ratio: 60,
        creativity: 2,
        beats: 18
    },
    retired_vehicle: {
        theme: 'submarine_cabin',
        anchors: ['carrier_cutout_window', 'single_slab_counter'],
        complexity: 3,
        budget: 3,
        ratio: 75,
        creativity: 3,
        beats: 20
    },
    contrast_novelty: {
        theme: 'hollow_oak',
        anchors: ['bark_camouflaged_hatch', 'rerouted_waterfall_shower'],
        complexity: 2,
        budget: 1,
        ratio: 50,
        creativity: 3,
        beats: 15
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
    };
    const buttons = {
        config: document.getElementById('main-tab-config'),
        results: document.getElementById('main-tab-results'),
        image: document.getElementById('main-tab-image'),
    };

    Object.keys(panels).forEach((key) => {
        if (panels[key]) panels[key].classList.toggle('mobile-active', key === tab);
        if (buttons[key]) buttons[key].classList.toggle('active', key === tab);
    });
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



// Mapping of image models by main model
const IMAGE_MODELS_BY_MAIN_MODEL = {
    'gemini-3-flash-agent': [
        { value: 'nano-banana-2', label: '🍌 Nano Banana 2' }
    ],
    'gpt-5.5': [
        { value: 'gpt-image-2', label: 'gpt-image-2' }
    ]
};

// Dynamically update image model dropdown choices based on selected main model
// Function updateImageModelOptions moved to modular JS file

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
        } else {
            throw new Error(data.message || `HTTP Error ${res.status}`);
        }
    } catch (e) {
        badge.className = 'status-badge offline';
        badge.querySelector('.status-text').textContent = 'API 连接断开';
        console.error("API check failed:", e);
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
    const creativityLabels = { 1: '常规务实', 2: '突破常规', 3: '脑洞大开 (极致科幻)' };

    complexity.addEventListener('input', (e) => {
        document.getElementById('val-complexity').textContent = complexityLabels[e.target.value];
    });
    budget.addEventListener('input', (e) => {
        document.getElementById('val-budget').textContent = budgetLabels[e.target.value];
    });
    ratio.addEventListener('input', (e) => {
        const val = e.target.value;
        document.getElementById('val-ratio').textContent = `反差强度: ${val}%`;
    });
    creativity.addEventListener('input', (e) => {
        document.getElementById('val-creativity').textContent = creativityLabels[e.target.value];
    });
    beats.addEventListener('input', (e) => {
        document.getElementById('val-beats').textContent = `${e.target.value} 拍`;
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
    // Theme Selector
    const themeGrid = document.getElementById('theme-selector');
    themeGrid.addEventListener('click', (e) => {
        const btn = e.target.closest('.theme-btn');
        if (!btn) return;
        
        themeGrid.querySelectorAll('.theme-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    });

    // Anchor Selector (Multi-select)
    const anchorFlex = document.getElementById('anchor-selector');
    anchorFlex.addEventListener('click', (e) => {
        const btn = e.target.closest('.anchor-node');
        if (!btn) return;
        
        btn.classList.toggle('active');
    });
}

// Interactive Particle Background (Canvas)
function initCanvas() {
    const canvas = document.getElementById('particle-canvas');
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

        // Skip all drawing while the tab is hidden
        if (document.hidden) return;

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
    
    // Toggle key visibility
    const toggleKey = document.getElementById('toggle-key-visibility');
    const keyInput = document.getElementById('settings-api-key');
    toggleKey.addEventListener('click', () => {
        if (keyInput.type === 'password') {
            keyInput.type = 'text';
            toggleKey.textContent = '🔒';
        } else {
            keyInput.type = 'password';
            toggleKey.textContent = '👁️';
        }
    });

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

    // Model select change handling (show/hide GPT port group and auto-update base URL)
    const modelSelect = document.getElementById('settings-model');
    const gptPortGroup = document.getElementById('gpt-port-group');
    const gptPortSelect = document.getElementById('settings-gpt-port');
    const baseUrlInput = document.getElementById('settings-base-url');

    if (modelSelect && gptPortGroup && gptPortSelect && baseUrlInput) {
        modelSelect.addEventListener('change', () => {
            updateGptPortVisibility();
            updateImageModelOptions(false);
            if (modelSelect.value === 'gpt-5.5') {
                const port = gptPortSelect.value;
                baseUrlInput.value = `http://localhost:${port}/v1`;
            } else if (modelSelect.value === 'gemini-3-flash-agent') {
                baseUrlInput.value = 'http://127.0.0.1:8046/v1';
            }
        });

        gptPortSelect.addEventListener('change', () => {
            const port = gptPortSelect.value;
            const currentUrl = baseUrlInput.value.trim();
            try {
                if (currentUrl.startsWith('http://') || currentUrl.startsWith('https://')) {
                    const urlObj = new URL(currentUrl);
                    urlObj.port = port;
                    baseUrlInput.value = urlObj.toString().replace(/\/$/, '');
                } else {
                    baseUrlInput.value = `http://localhost:${port}/v1`;
                }
            } catch (e) {
                baseUrlInput.value = `http://localhost:${port}/v1`;
            }
        });
    }

    // Test API connection within Settings
    const testSettingsBtn = document.getElementById('test-settings-btn');
    if (testSettingsBtn) {
        testSettingsBtn.addEventListener('click', async () => {
            const originalText = testSettingsBtn.textContent;
            testSettingsBtn.textContent = '测试中...';
            testSettingsBtn.disabled = true;
            
            const tempConfig = {
                baseUrl: document.getElementById('settings-base-url').value.trim(),
                apiKey: document.getElementById('settings-api-key').value.trim(),
                model: document.getElementById('settings-model').value.trim()
            };
            
            try {
                const res = await fetch('/api/ping', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: tempConfig })
                });
                const data = await res.json();
                if (res.ok && data.online) {
                    showToast("连接测试成功！模型在线。", "success");
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
        if (tasksDrawer) {
            tasksDrawer.classList.remove('active');
            stopTasksPolling();
        }
        libraryDrawer.classList.add('active');
        renderLibrary();
    });
    closeLibrary.addEventListener('click', () => libraryDrawer.classList.remove('active'));

    // Tasks Drawer
    const openTasks = document.getElementById('toggle-tasks-btn');
    const closeTasks = document.getElementById('close-tasks-btn');
    
    if (openTasks && closeTasks && tasksDrawer) {
        openTasks.addEventListener('click', () => {
            if (libraryDrawer) libraryDrawer.classList.remove('active');
            tasksDrawer.classList.add('active');
            renderTasks();
            startTasksPolling();
        });
        closeTasks.addEventListener('click', () => {
            tasksDrawer.classList.remove('active');
            stopTasksPolling();
        });
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

    // Video Reverse Actions
    const reverseBtn = document.getElementById('reverse-btn');
    if (reverseBtn) {
        reverseBtn.addEventListener('click', () => handleReverse());
    }

    const fpsInput = document.getElementById('fps-input');
    if (fpsInput) {
        fpsInput.addEventListener('input', (e) => {
            const valEl = document.getElementById('fps-value');
            if (valEl) valEl.textContent = parseFloat(e.target.value).toFixed(1);
        });
    }

    const uploadZone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('video-file-input');
    if (uploadZone && fileInput) {
        uploadZone.addEventListener('click', () => {
            if (!selectedVideoFile) fileInput.click();
        });

        fileInput.addEventListener('change', (e) => {
            handleFileSelect(e.target.files[0]);
        });
    }

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
            const genBtn = document.getElementById('generate-btn');
            if (genBtn && !genBtn.disabled) {
                genBtn.click();
            }
        }
        handleGlobalHotkeys(e);
    });

    // Advanced Workshop parameters toggle
    const advToggleBtn = document.getElementById('toggle-workshop-advanced');
    const advFields = document.getElementById('workshop-advanced-fields');
    if (advToggleBtn && advFields) {
        advToggleBtn.addEventListener('click', () => {
            if (advFields.style.display === 'none') {
                advFields.style.display = 'block';
                advToggleBtn.textContent = '收起 ▴';
            } else {
                advFields.style.display = 'none';
                advToggleBtn.textContent = '展开 ▾';
            }
        });
    }

    // Preset Selection
    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const preset = e.currentTarget.dataset.preset;
            if (preset) {
                applyPreset(preset);
            }
        });
    });

    document.getElementById('randomize-btn').addEventListener('click', randomizeDimensions);
    document.getElementById('save-preset-btn').addEventListener('click', saveCustomPreset);

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

    // Save state on selectors click - direct call, no redundant setTimeout
    document.getElementById('theme-selector').addEventListener('click', () => {
        saveSelectionState();
    });
    document.getElementById('anchor-selector').addEventListener('click', () => {
        saveSelectionState();
    });

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

    // Close drawers when clicking outside
    document.addEventListener('click', (e) => {
        const libraryDrawer = document.getElementById('library-drawer');
        const tasksDrawer = document.getElementById('tasks-drawer');
        const toggleLibBtn = document.getElementById('toggle-library-btn');
        const toggleTasksBtn = document.getElementById('toggle-tasks-btn');
        
        // For library drawer
        if (libraryDrawer && libraryDrawer.classList.contains('active')) {
            if (!libraryDrawer.contains(e.target) && 
                (!toggleLibBtn || !toggleLibBtn.contains(e.target))) {
                libraryDrawer.classList.remove('active');
            }
        }
        
        // For tasks drawer
        if (tasksDrawer && tasksDrawer.classList.contains('active')) {
            if (!tasksDrawer.contains(e.target) && 
                (!toggleTasksBtn || !toggleTasksBtn.contains(e.target))) {
                tasksDrawer.classList.remove('active');
                stopTasksPolling();
            }
        }
    });
}

// --- Tasks Drawer Functions & Polling ---
let tasksPollTimeout = null;
let currentPollInterval = 2500;
let tasksSearchQuery = '';
let tasksFilterStatus = '';
let tasksFilterType = '';

async function renderTasks() {
    const tasksListContainer = document.getElementById('tasks-list');
    if (!tasksListContainer) return;
    
    try {
        const response = await fetch('/api/tasks');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const resData = await response.json();
        const tasks = Array.isArray(resData) ? resData : (resData.tasks || []);
        
        // Update badge count
        updateTasksBadge(tasks);
        
        // Local filtering
        let filteredTasks = tasks.filter(task => {
            const theme = task.dimensions ? (task.dimensions.theme || '未命名主题') : '未命名主题';
            const isVideoReverse = (task.dimensions && task.dimensions.type === 'reverse-video') || theme.startsWith('视频反推');
            const beats = (task.dimensions && task.dimensions.beats_count && !isVideoReverse) ? ` (${task.dimensions.beats_count} 镜)` : '';
            const taskTitle = isVideoReverse ? theme : `${theme}${beats}`;
            
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
                const taskType = task.dimensions ? task.dimensions.type : '';
                let resolvedType = taskType || '';
                if (!resolvedType) {
                    if (theme.startsWith('视频反推')) resolvedType = 'reverse-video';
                    else resolvedType = 'idea';
                }
                if (resolvedType !== tasksFilterType) return false;
            }
            return true;
        });
        
        if (filteredTasks.length === 0) {
            tasksListContainer.innerHTML = `<div class="tasks-empty">暂无符合筛选条件的任务</div>`;
            return;
        }
        
        let html = '';
        filteredTasks.forEach(task => {
            const dateStr = new Date(parseInt(task.id, 10)).toLocaleString();
            const theme = task.dimensions ? (task.dimensions.theme || '未命名主题') : '未命名主题';
            const isVideoReverse = (task.dimensions && task.dimensions.type === 'reverse-video') || theme.startsWith('视频反推');
            const beats = (task.dimensions && task.dimensions.beats_count && !isVideoReverse) ? ` (${task.dimensions.beats_count} 镜)` : '';
            const taskTitle = isVideoReverse ? theme : `${theme}${beats}`;
            
            let statusLabel = '';
            let statusClass = '';
            let footerButtons = '';
            let progressHtml = '';
            let errorHtml = '';
            let tokenInfoHtml = '';
            
            if (task.status === 'running') {
                statusLabel = '运行中';
                statusClass = 'running';
                
                // Get progress percent and current stage from events (using historical maximum)
                let progressPercent = 0;
                let currentStage = '准备中...';
                
                if (task.events && task.events.length > 0) {
                    let maxPercent = 0;
                    let lastStageText = '正在生成...';
                    
                    task.events.forEach(evt => {
                        const evtType = evt[0];
                        const evtData = evt[1];
                        
                        if (evtType === 'progress') {
                            const stage = evtData.stage;
                            let pct = 0;
                            if (stage === 'init') pct = 5;
                            else if (stage === 'text_composing') pct = 25;
                            else if (stage === 'text_composed') pct = 50;
                            else if (stage === 'checking') pct = 75;
                            else if (stage === 'repaired') pct = 90;
                            else if (stage === 'keyframe_extraction') pct = 15;
                            else if (stage === 'cv_analysis') pct = 40;
                            else if (stage === 'semantic_metadata') pct = 70;
                            else if (stage === 'prompt_composition') pct = 90;
                            
                            if (pct > maxPercent) {
                                maxPercent = pct;
                            }
                            lastStageText = evtData.details || evtData.stage || lastStageText;
                        } else if (evtType === 'text_chunk') {
                            if (maxPercent < 35) {
                                maxPercent = 35;
                                lastStageText = '深度创意激发中...';
                            }
                        }
                    });
                    
                    progressPercent = maxPercent;
                    currentStage = lastStageText;
                }
                
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
        
        tasksListContainer.innerHTML = html;
    } catch (e) {
        console.error("Failed to render tasks list:", e);
        tasksListContainer.innerHTML = `<div class="tasks-empty" style="color: #f87171;">加载任务列表失败: ${escapeHtml(e.message)}</div>`;
    }
}

// Function startTasksPolling moved to modular JS file

// Function stopTasksPolling moved to modular JS file

// Function updateTasksBadge moved to modular JS file

let globalBadgeTimeout = null;

async function startGlobalTasksBadgePolling() {
    if (globalBadgeTimeout) clearTimeout(globalBadgeTimeout);
    
    const poll = async () => {
        let hasRunning = false;
        try {
            const response = await fetch('/api/tasks');
            if (response.ok) {
                const resData = await response.json();
                const tasks = Array.isArray(resData) ? resData : (resData.tasks || []);
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
    const activeTaskId = localStorage.getItem('spark_active_task_id');
    if (generationState.status === 'composing' && activeTaskId !== taskId) {
        const confirmed = await customConfirm("当前有其他任务正在生成，切换查看该任务将接管主面板并替换当前日志流，确定继续吗？");
        if (!confirmed) return;
    } else {
        const contentView = document.getElementById('output-content-view');
        if (contentView && contentView.classList.contains('active')) {
            const confirmed = await customConfirm("查看该运行中任务将清空并接管当前主面板的展示内容，确定继续吗？");
            if (!confirmed) return;
        }
    }

    const tasksDrawer = document.getElementById('tasks-drawer');
    if (tasksDrawer) tasksDrawer.classList.remove('active');
    stopTasksPolling();
    
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
    if (generationState.status === 'composing') {
        const confirmed = await customConfirm("当前有任务正在运行，载入历史任务将中断当前生成的实时展示，确定继续吗？");
        if (!confirmed) return;
    } else {
        const contentView = document.getElementById('output-content-view');
        if (contentView && contentView.classList.contains('active')) {
            const confirmed = await customConfirm("载入此任务将覆盖当前主面板展示的内容，确定继续吗？");
            if (!confirmed) return;
        }
    }

    const tasksDrawer = document.getElementById('tasks-drawer');
    if (tasksDrawer) tasksDrawer.classList.remove('active');
    stopTasksPolling();
    
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
                theme: task.dimensions ? task.dimensions.theme : '视频反推',
                creativity: task.dimensions ? task.dimensions.creativity : '多模态反推',
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
                collage_url: data.collage_url || ''
            };
            
            currentIdea = result;
            saveCurrentIdeaState();
            generationState.status = 'idle';
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
    
    const tasksDrawer = document.getElementById('tasks-drawer');
    if (tasksDrawer) {
        tasksDrawer.classList.remove('active');
        stopTasksPolling();
    }
    
    showToast("正在重新提交该生成任务...", "info");
    
    try {
        await generateIdea({
            dimensions: dimensions,
            config: config
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
        showToast("⚠️ API 连接已断开，请先在配置中心检查连接并保存！", "error");
        const settingsModal = document.getElementById('settings-modal');
        if (settingsModal) {
            settingsModal.classList.add('active');
        }
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
    updateActiveGenerationBanner();

    if (genBtn) {
        genBtn.disabled = true;
        genBtn.classList.add('loading');
    }

    let dimensions, currentConf;
    if (retryParams) {
        dimensions = retryParams.dimensions;
        currentConf = retryParams.config;
    } else {
        // Collect GUI dimensions
        const activeThemeBtn = document.querySelector('#theme-selector .theme-btn.active');
        const themeName = activeThemeBtn ? activeThemeBtn.querySelector('.theme-name').textContent.trim() : '百年空心橡树';

        const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
            .map(node => node.textContent.trim());

        dimensions = {
            theme: themeName,
            anchors: activeAnchors,
            complexity: document.getElementById('val-complexity').textContent,
            budget: document.getElementById('val-budget').textContent,
            ratio: document.getElementById('val-ratio').textContent,
            creativity: document.getElementById('val-creativity').textContent,
            beats_count: parseInt(document.getElementById('slider-beats').value, 10)
        };
        currentConf = { ...config };
    }

    // Save last params for retry
    generationState.lastParams = { dimensions, config: currentConf };
    generationState.status = 'composing';

    setupLoadingSteps('compose');

    // Generate unique taskId
    const taskId = Date.now().toString();

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

    currentGenerationController = new AbortController();
    const timeoutId = setTimeout(() => {
        if (currentGenerationController) {
            currentGenerationController.abort();
        }
    }, 480000); // 8 minutes timeout

    try {
        const response = await fetch(`/api/compose-stream?task_id=${taskId}`, {
            signal: currentGenerationController.signal
        });

        clearTimeout(timeoutId);

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = '';
        let resultData = null;

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
                        if (parsed.type === 'progress') {
                            updateProgressUI(parsed.data);
                        } else if (parsed.type === 'text_chunk') {
                            appendLiveTerminal(parsed.data);
                        } else if (parsed.type === 'result') {
                            resultData = parsed.data;
                        } else if (parsed.type === 'error') {
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (e) {
                        console.error("Failed to parse compose progress chunk", e);
                        if (e.message) throw e;
                    }
                }
            }
        }

        stopLoadingTimer();
        currentGenerationController = null;
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');

        if (!resultData) {
            throw new Error("模型未返回有效结果");
        }

        const data = resultData;
        const isVideoReverse = dimensions.type === 'reverse-video' || (dimensions.theme && dimensions.theme.startsWith('视频反推'));
        
        const result = {
            id: taskId,
            title: data.title || '未命名创意',
            theme: dimensions.theme || '视频反推',
            creativity: dimensions.creativity || '多模态反推',
            prompt_block: data.prompt_block || data.raw || '',
            audit_md: data.audit_md || '',
            repair_md: data.repair_md || '',
            timestamp: new Date(parseInt(taskId, 10)).toLocaleString(),
            timings: data.timings || {},
            image_count: data.image_count || 0,
            video_count: data.video_count || 0,
            collage_url: data.collage_url || '',
            covers: data.covers || [],
            frameRun: data.frameRun || null,
            english_title: data.english_title || ''
        };

        currentIdea = result;
        saveCurrentIdeaState();
        generationState.status = 'idle';

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
        
        if (isVideoReverse) {
            showToast("视频反推提示词成功！", "success");
        } else {
            showToast("提示词集合合成成功！已开始在后台制作封面图。", "success");
            // Background asynchronous cover generation
            generateCover();
        }

    } catch (e) {
        stopLoadingTimer();
        currentGenerationController = null;
        console.error("Failed to stream progress:", e);

        loadingView.classList.remove('active');
        contentView.classList.remove('active');
        
        generationState.status = 'idle';

        const isVideoReverse = dimensions.type === 'reverse-video' || (dimensions.theme && dimensions.theme.startsWith('视频反推'));

        if (e.name === 'AbortError') {
            placeholderView.classList.add('active');
            showToast(isVideoReverse ? "视频反推已被取消" : "合成已被取消或已超时", "info");
        } else {
            generationState.status = 'error';
            if (errorView) {
                errorView.style.display = 'flex';
            } else {
                placeholderView.classList.add('active');
            }

            let errorMsg = isVideoReverse ? "视频反推发生错误。" : "合成发生阻碍，请检查本地 API 与 skill 路径。";
            if (e.message) {
                const raw = String(e.message);
                if (/network error|failed to fetch|networkerror|load failed/i.test(raw)) {
                    errorMsg = isVideoReverse
                        ? "反推连接已断开：与本地服务的实时连接中断。请确认 SPARK 服务仍在运行，然后点击「重试」。"
                        : "合成连接已断开：与本地服务的实时连接中断。请确认 SPARK 服务（端口 8085）与 Antigravity 代理（端口 8046）仍在运行，然后点击「重试」。";
                } else {
                    errorMsg = isVideoReverse ? `视频反推失败：${raw}` : `合成失败：${raw}`;
                }
            }

            const errMsgEl = document.getElementById('error-message-text');
            if (errMsgEl) {
                errMsgEl.textContent = errorMsg;
            }
            showToast(errorMsg, "error");
        }
    } finally {
        if (genBtn) {
            genBtn.disabled = false;
            genBtn.classList.remove('loading');
        }
        const reverseBtn = document.getElementById('reverse-btn');
        if (reverseBtn) {
            reverseBtn.disabled = false;
            reverseBtn.classList.remove('loading');
        }
        updateActiveGenerationBanner();
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
            
            const isVideoReverse = dimensions.type === 'reverse-video' || (dimensions.theme && dimensions.theme.startsWith('视频反推'));
            setupLoadingSteps(isVideoReverse ? 'reverse-video' : 'compose');

            const loadingHeader = loadingView.querySelector('h3');
            const loadingStage = document.getElementById('loading-stage-text');
            if (isVideoReverse) {
                if (loadingHeader) loadingHeader.textContent = '正在反向逆向视频工程...';
                if (loadingStage) loadingStage.textContent = '正在提取视频关键帧、计算亮度与运动变化，并提交给多模态大模型进行时序 and 物理一致性分析。';
                
                const reverseBtn = document.getElementById('reverse-btn');
                if (reverseBtn) {
                    reverseBtn.disabled = true;
                    reverseBtn.classList.add('loading');
                }
            } else {
                if (loadingHeader) loadingHeader.textContent = '正在按 skill 契约合成提示词...';
                if (loadingStage) loadingStage.textContent = '解析主题，拆解场景变量与施工节拍...';
                
                if (genBtn) {
                    genBtn.disabled = true;
                    genBtn.classList.add('loading');
                }
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

async function streamFramesProgress(taskId) {
    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    const grid = document.getElementById('frames-grid');
    if (!btn || !progress || !meta || !grid) return;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '连接帧生成事件流...';

    activeBackgroundTasks.frames = true;
    updateTabStatusDot();

    currentFramesController = new AbortController();

    try {
        const response = await fetch(`/api/compose-stream?task_id=${taskId}`, {
            signal: currentFramesController.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const reader = response.body.getReader();
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
                        if (parsed.type === 'start') {
                            const total = parsed.data.total;
                            meta.textContent = `开始生成共 ${total} 帧序列图...`;
                            
                            if (currentIdea) {
                                if (!currentIdea.frameRun) {
                                    currentIdea.frameRun = { title: currentIdea.title, frames: [] };
                                }
                                currentIdea.frameRun.frames = [];
                                saveCurrentIdeaState();
                                const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
                                if (existingIdx !== -1) {
                                    savedIdeas[existingIdx].frameRun = currentIdea.frameRun;
                                    saveLibrary();
                                }
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
                        } else if (parsed.type === 'frame') {
                            const f = parsed.data.frame;
                            const cur = parsed.data.current;
                            const tot = parsed.data.total;
                            if (cur < tot) {
                                meta.textContent = `正在生成帧序列: ${cur}/${tot} (正在处理第 ${cur + 1} 帧)...`;
                            } else {
                                meta.textContent = `正在生成帧序列: ${cur}/${tot} (已生成完毕，正在整理)...`;
                            }
                            
                            const slot = document.getElementById(`frame-slot-${f.sequence}`);
                            if (slot) {
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
                                safeSetImageSrc(slot.querySelector('img'), f.url);
                                
                                slot.addEventListener('mouseenter', () => {
                                    const actions = slot.querySelector('.frame-card-actions');
                                    if (actions) actions.style.opacity = '1';
                                });
                                slot.addEventListener('mouseleave', () => {
                                    const actions = slot.querySelector('.frame-card-actions');
                                    if (actions) actions.style.opacity = '0';
                                });
                                
                                slot.addEventListener('click', (e) => {
                                    if (e.target.classList.contains('retry-frame-btn')) return;
                                    const validFrames = (currentIdea && currentIdea.frameRun && currentIdea.frameRun.frames) || [];
                                    const mediaList = validFrames.map((frame) => ({
                                        type: 'image',
                                        url: frame.url || frame.file,
                                        caption: `<strong>第 ${frame.sequence} 帧 / 共 ${validFrames.length} 帧</strong>`
                                    }));
                                    const clickedIndex = validFrames.findIndex(frame => frame.sequence === f.sequence);
                                    openLightbox(mediaList, clickedIndex >= 0 ? clickedIndex : 0);
                                });
                                
                                slot.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
                                    e.stopPropagation();
                                    retrySingleFrame(f.sequence);
                                });
                            }

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
                            }
                        } else if (parsed.type === 'result') {
                            manifestData = parsed.data;
                        } else if (parsed.type === 'error') {
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (err) {
                        console.error("Error parsing frames SSE data", err);
                        if (err.message) throw err;
                    }
                }
            }
        }

        currentFramesController = null;
        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (manifestData) {
            currentIdea.frameRun = manifestData;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifestData;
                await saveLibrary();
            }
            renderFramesForIdea(currentIdea);
            showToast(`已成功生成 ${manifestData.frames.length} 帧连续帧序列图。`, "success");
        }
    } catch (e) {
        currentFramesController = null;
        console.error("Failed to generate frames:", e);
        if (e.name === 'AbortError') {
            meta.textContent = '帧序列生成已被用户取消。';
            showToast('已取消帧序列生成', 'info');
        } else {
            meta.textContent = `帧序列生成失败: ${e.message}`;
            showToast(`帧序列生成失败: ${e.message}`, "error");
        }
        
        activeBackgroundTasks.framesTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (currentIdea) {
            renderFramesForIdea(currentIdea);
        }
    } finally {
        progress.style.display = 'none';
        btn.disabled = false;
        activeBackgroundTasks.frames = false;
        updateTabStatusDot();
    }
}

async function streamVideosProgress(taskId) {
    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = document.getElementById('videos-grid');
    if (!btn || !progress || !meta || !grid) return;

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = '连接视频生成事件流...';

    activeBackgroundTasks.videos = true;
    updateTabStatusDot();

    currentVideosController = new AbortController();
    let videoProgressState = window.ProgressModel ? ProgressModel.createProgressState('videos') : null;
    const applyVideoProgress = (eventType, eventData) => {
        if (!window.ProgressModel) return null;
        const progressInfo = ProgressModel.normalizeGenerationProgress(eventType, eventData, 'videos', videoProgressState);
        videoProgressState = progressInfo.state;
        setProgressBar('videos', progressInfo);
        if (progressInfo.label) meta.textContent = progressInfo.label;
        return progressInfo;
    };
    applyVideoProgress('queue', { message: '连接视频生成事件流...' });

    try {
        const response = await fetch(`/api/compose-stream?task_id=${taskId}`, {
            signal: currentVideosController.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const reader = response.body.getReader();
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
                        if (parsed.type === 'start') {
                            applyVideoProgress('start', parsed.data);
                            const total = parsed.data.total;
                            const slots = parsed.data.slots || [];
                            meta.textContent = `开始生成共 ${total} 段视频...`;
                            grid.innerHTML = '';
                            
                            const slotsToRender = slots.length ? slots : Array.from({length: total}, (_, i) => i + 1);
                            slotsToRender.forEach(slotIdx => {
                                const placeholderCard = document.createElement('div');
                                placeholderCard.className = 'frame-card placeholder-frame-card';
                                placeholderCard.id = `video-slot-${slotIdx}`;
                                placeholderCard.innerHTML = `
                                    <div class="frame-placeholder-spinner">
                                        <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                                    </div>
                                    <span>第 ${String(slotIdx).padStart(3, '0')} 段视频 (等待中)</span>
                                `;
                                grid.appendChild(placeholderCard);
                            });
                        } else if (parsed.type === 'video_start') {
                            applyVideoProgress('video_start', parsed.data);
                            const cur = parsed.data.current;
                            const tot = parsed.data.total;
                            const idx = parsed.data.index;
                            meta.textContent = `正在生成视频: ${cur}/${tot} (正在处理第 ${idx} 段视频)...`;
                            
                            const slot = document.getElementById(`video-slot-${idx}`);
                            if (slot) {
                                slot.innerHTML = `
                                    <div class="frame-placeholder-spinner">
                                        <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                                    </div>
                                    <span>第 ${String(idx).padStart(3, '0')} 段视频 (生成中...)</span>
                                `;
                            }
                        } else if (parsed.type === 'video_done') {
                            applyVideoProgress('video_done', parsed.data);
                            const v = parsed.data.video;
                            const cur = parsed.data.current;
                            const tot = parsed.data.total;
                            const idx = parsed.data.index;
                            meta.textContent = `正在生成视频: ${cur}/${tot}...`;
                            
                            const slot = document.getElementById(`video-slot-${idx}`);
                            if (slot) {
                                slot.className = 'frame-card';
                                slot.style.cursor = 'default';
                                slot.innerHTML = `
                                    <video src="${v.url}" controls style="width:100%; aspect-ratio: 9/16; object-fit: cover; border-radius: 5px; display: block; background: #03050c;"></video>
                                    <span>VID ${String(v.slot).padStart(3, '0')}</span>
                                `;
                            }
                        } else if (parsed.type === 'video_error') {
                            applyVideoProgress('video_error', parsed.data);
                            const cur = parsed.data.current;
                            const tot = parsed.data.total;
                            const idx = parsed.data.index;
                            const msg = parsed.data.message || '生成失败';
                            meta.textContent = `视频 ${idx} 生成失败: ${msg}`;
                            
                            const slot = document.getElementById(`video-slot-${idx}`);
                            if (slot) {
                                slot.className = 'frame-card video-failed-card';
                                slot.innerHTML = `
                                    <div class="video-failed-placeholder">
                                        <span class="error-icon">⚠️</span>
                                        <span class="error-text" title="${msg}">生成失败</span>
                                        <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${idx}">重试</button>
                                    </div>
                                    <span>VID ${String(idx).padStart(3, '0')}</span>
                                `;
                                slot.querySelector('.retry-video-btn').addEventListener('click', (e) => {
                                    e.stopPropagation();
                                    retrySingleVideo(idx);
                                });
                            }
                        } else if (parsed.type === 'queue') {
                            applyVideoProgress('queue', parsed.data);
                            meta.textContent = parsed.data.message || '正在排队等待生成视频...';
                        } else if (parsed.type === 'merge_skip') {
                            applyVideoProgress('merge_skip', parsed.data);
                            meta.textContent = parsed.data.message || '由于存在失败片段，已跳过自动合并。';
                        } else if (parsed.type === 'merge_start') {
                            applyVideoProgress('merge_start', parsed.data);
                            meta.textContent = '正在自动合并并加速视频 (2x Speed)...';
                        } else if (parsed.type === 'merge_done') {
                            applyVideoProgress('merge_done', parsed.data);
                            meta.textContent = '所有视频已成功生成并合并加速！';
                        } else if (parsed.type === 'merge_error') {
                            applyVideoProgress('merge_error', parsed.data);
                            meta.textContent = `自动合并视频失败: ${parsed.data.message || '未知错误'}`;
                            showToast(`自动合并失败: ${parsed.data.message || '未知错误'}`, "warning");
                        } else if (parsed.type === 'result') {
                            applyVideoProgress('result', parsed.data);
                            manifestData = parsed.data;
                        } else if (parsed.type === 'error') {
                            applyVideoProgress('error', parsed.data);
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (err) {
                        console.error("Error parsing videos SSE data", err);
                        if (err.message) throw err;
                    }
                }
            }
        }

        currentVideosController = null;
        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (manifestData) {
            currentIdea.frameRun = manifestData;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifestData;
                await saveLibrary();
            }
            renderVideosForIdea(currentIdea);
            showToast(`已成功生成 ${manifestData.videos.length} 段连续视频。`, "success");
        }
    } catch (e) {
        currentVideosController = null;
        console.error("Failed to generate videos:", e);
        
        activeBackgroundTasks.videosTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (e.name === 'AbortError') {
            meta.textContent = '视频生成已被用户取消。';
            showToast('已取消视频生成', 'info');

            const placeholders = grid.querySelectorAll('.placeholder-frame-card');
            placeholders.forEach(card => {
                const slotMatch = card.id && card.id.match(/video-slot-(\d+)/);
                const slotIdx = slotMatch ? parseInt(slotMatch[1]) : null;
                if (slotIdx !== null) {
                    card.className = 'frame-card video-failed-card';
                    card.innerHTML = `
                        <div class="video-failed-placeholder">
                            <span class="error-icon">⚠️</span>
                            <span class="error-text" title="已被用户取消">未生成</span>
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
        } else {
            meta.textContent = `视频生成失败: ${e.message}`;
            showToast(`视频生成失败: ${e.message}`, "error");

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

            try {
                const resp = await fetch(`/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(currentIdea))}`);
                if (resp.ok) {
                    const manifest = await resp.json();
                    currentIdea.frameRun = manifest;
                    saveCurrentIdeaState();
                    const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
                    if (existingIdx !== -1) {
                        savedIdeas[existingIdx].frameRun = manifest;
                        await saveLibrary();
                    }
                    renderVideosForIdea(currentIdea);
                }
            } catch (err) {
                console.error("Failed to load partial manifest after failure:", err);
            }
        }
    } finally {
        progress.style.display = 'none';
        btn.disabled = false;
        activeBackgroundTasks.videos = false;
        updateTabStatusDot();
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

    try {
        const response = await fetch(`/api/compose-stream?task_id=${taskId}`);
        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.ok ? 'OK' : response.status}: ${errText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = '';
        let resultData = null;

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
                        if (parsed.type === 'result') {
                            resultData = parsed.data;
                        } else if (parsed.type === 'error') {
                            throw new Error(parsed.data.message || '未知错误');
                        }
                    } catch (err) {
                        console.error("Error parsing cover SSE data", err);
                        if (err.message) throw err;
                    }
                }
            }
        }

        activeBackgroundTasks.coverTaskId = null;
        saveActiveBackgroundTasksToLocalStorage();

        if (!resultData) {
            throw new Error("模型未返回有效结果");
        }

        const data = resultData;
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
        saveCurrentIdeaState();

        const existingIdx = savedIdeas.findIndex(item => item.id === currentIdea.id);
        if (existingIdx !== -1) {
            savedIdeas[existingIdx].covers = currentIdea.covers;
            if (englishTitle) {
                savedIdeas[existingIdx].english_title = englishTitle;
            }
            await saveLibrary();
        }

        renderCoversForIdea(currentIdea, currentIdea.covers.length - 1);
        showToast("封面图制作成功！", "success");
    } catch (e) {
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
        loadingEl.style.display = 'none';
        makeBtn.disabled = false;
        activeBackgroundTasks.cover = false;
        updateTabStatusDot();
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

function setupLoadingSteps(type) {
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
    
    if (type === 'reverse-video') {
        step1.textContent = '提取视频关键帧...';
        step2.textContent = '计算运动与光照变化 (CV 启发式)...';
        step3.textContent = '大模型多模态视频分析与时序语义提取...';
        step4.textContent = '合成 SCUP 契约提示词并进行物理一致性审计...';
    } else {
        step1.textContent = '解析主题，拆解场景变量与施工节拍...';
        step2.textContent = '装配 Drift Lock 与九宫格锚点...';
        step3.textContent = '渲染 IMAGE 锚点与连续动作 VIDEO 链...';
        step4.textContent = '运行质量门 + 工序与场景一致性二次校验与修复...';
    }
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
            document.getElementById('library-drawer').classList.remove('active');
        });
        
        list.appendChild(card);
    });
}

function deleteFromLibrary(id) {
    savedIdeas = savedIdeas.filter(item => item.id !== id);
    saveLibrary();
    showToast("已从点子库删除", "success");
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
    let hookMarkdown = `\n*TikTok US 推荐主题和 tags：${tiktokMeta.english}*\n*中文翻译：${tiktokMeta.chinese}*\n`;

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
        showToast("TikTok 主题和 hashtags 已复制！", "success");
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

    // Check for VLM QA failed frames
    if (currentIdea.frameRun && currentIdea.frameRun.frames) {
        const failedFrames = currentIdea.frameRun.frames.filter(f => f.quality_gate === 'vlm_qa_failed');
        if (failedFrames.length > 0) {
            const frameSeqs = failedFrames.map(f => f.sequence).join(', ');
            const confirmed = await customConfirm(`⚠️ 警告：检测到第 ${frameSeqs} 帧未能通过 VLM 图像质检（VLM 失败）。\n\n如果强行生成视频，对应的视频分段可能存在跳变、无动作或动作不一致的缺陷。\n\n确定要强制继续生成视频吗？`);
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

async function mergeVideos() {
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
        <span>正在合并中...</span>
    `;
    videosMeta.textContent = "正在调用 FFmpeg 合并并加速视频，此过程可能需要几秒钟，请稍候...";

    try {
        const response = await fetch('/api/merge_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: getIdeaSaveTitle(currentIdea)
            })
        });

        if (!response.ok) {
            const errText = await response.text();
            let parsedErr;
            try { parsedErr = JSON.parse(errText); } catch(e) {}
            throw new Error((parsedErr && (parsedErr.message || parsedErr.error)) || `HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
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
            showToast("视频合并并加速成功！", "success");
            videosMeta.textContent = "视频合并已完成！";
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

// =====================================================================
// Prompt Slots Filtering & Bulk Operations
// =====================================================================
function setupPromptControls() {
    // Filter buttons
    const filterBtns = document.querySelectorAll('.slot-filter-btn');
    filterBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            filterBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentSlotFilter = btn.dataset.filter;
            applySlotFilter();
        });
    });
    
    // Expand / Collapse all
    document.getElementById('expand-all-btn').addEventListener('click', () => {
        document.querySelectorAll('.prompt-slot-card').forEach(card => {
            const body = card.querySelector('.prompt-slot-body');
            const foldBtn = card.querySelector('.slot-actions button:last-child');
            if (body && foldBtn) {
                body.style.display = 'block';
                foldBtn.innerHTML = '收起';
            }
        });
    });
    
    document.getElementById('collapse-all-btn').addEventListener('click', () => {
        document.querySelectorAll('.prompt-slot-card').forEach(card => {
            const body = card.querySelector('.prompt-slot-body');
            const foldBtn = card.querySelector('.slot-actions button:last-child');
            if (body && foldBtn) {
                body.style.display = 'none';
                foldBtn.innerHTML = '展开';
            }
        });
    });
    
    // Copy all images / Copy all videos separately
    document.getElementById('copy-images-btn').addEventListener('click', (e) => {
        copySlotsByType('image', e.currentTarget);
    });
    
    document.getElementById('copy-videos-btn').addEventListener('click', (e) => {
        copySlotsByType('video', e.currentTarget);
    });
}

function applySlotFilter() {
    const cards = document.querySelectorAll('.prompt-slot-card');
    cards.forEach(card => {
        if (currentSlotFilter === 'all') {
            card.style.display = 'block';
        } else if (currentSlotFilter === 'image' && card.classList.contains('image-slot')) {
            card.style.display = 'block';
        } else if (currentSlotFilter === 'video' && card.classList.contains('video-slot')) {
            card.style.display = 'block';
        } else {
            card.style.display = 'none';
        }
    });
}

function copySlotsByType(type, btnEl) {
    const cards = Array.from(document.querySelectorAll(`.prompt-slot-card.${type}-slot`));
    if (cards.length === 0) {
        showToast(`当前无${type === 'image' ? '图片' : '视频'}提示词可复制`, 'error');
        return;
    }
    
    const promptsText = cards.map(card => {
        const label = card.querySelector('.slot-label').textContent.trim();
        const body = card.querySelector('.prompt-slot-body').textContent.trim();
        return `${label}:\n${body}`;
    }).join('\n\n');
    
    copyText(promptsText).then(() => {
        showToast(`成功复制所有${type === 'image' ? '图片' : '视频'}提示词！`, "success");
        
        // Visual feedback on button
        if (btnEl) {
            const originalText = btnEl.textContent;
            btnEl.textContent = "已复制！✓";
            btnEl.classList.add('copied');
            setTimeout(() => {
                btnEl.textContent = originalText;
                btnEl.classList.remove('copied');
            }, 1000);
        }
    }).catch(err => {
        showToast("复制失败", "error");
        console.error(err);
    });
}

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

// =====================================================================
// Video Reverse Engineering Handlers
// =====================================================================
window.switchInputTab = function(tab) {
    activeInputTab = tab;
    document.getElementById('input-tab-text').classList.toggle('active', tab === 'text');
    document.getElementById('input-tab-video').classList.toggle('active', tab === 'video');

    document.getElementById('input-pane-text').classList.toggle('active', tab === 'text');
    document.getElementById('input-pane-video').classList.toggle('active', tab === 'video');

    const footerText = document.getElementById('footer-pane-text');
    const footerVideo = document.getElementById('footer-pane-video');
    if (footerText) footerText.classList.toggle('active', tab === 'text');
    if (footerVideo) footerVideo.classList.toggle('active', tab === 'video');
};

window.clearSelectedFile = function(event) {
    if (event) event.stopPropagation();
    selectedVideoFile = null;
    document.getElementById('video-file-input').value = '';

    const zone = document.getElementById('upload-zone');
    const fileInfo = document.getElementById('selected-file-info');
    if (fileInfo) fileInfo.style.display = 'none';

    // Restore text
    if (zone) {
        const icon = zone.querySelector('.upload-icon');
        const txt = zone.querySelector('.upload-text');
        const hint = zone.querySelector('.upload-hint');
        if (icon) icon.style.display = 'block';
        if (txt) txt.style.display = 'block';
        if (hint) hint.style.display = 'block';
    }
};

function handleFileSelect(file) {
    if (!file) return;

    const name = file.name.toLowerCase();
    if (!name.endsWith('.mp4') && !name.endsWith('.mov') && !name.endsWith('.avi') && !name.endsWith('.webm')) {
        showToast('仅支持 .mp4, .mov, .avi, .webm 格式的视频文件！', 'error');
        clearSelectedFile();
        return;
    }

    selectedVideoFile = file;

    const zone = document.getElementById('upload-zone');
    const fileInfo = document.getElementById('selected-file-info');

    if (zone && fileInfo) {
        // Hide default text
        const icon = zone.querySelector('.upload-icon');
        const txt = zone.querySelector('.upload-text');
        const hint = zone.querySelector('.upload-hint');
        if (icon) icon.style.display = 'none';
        if (txt) txt.style.display = 'none';
        if (hint) hint.style.display = 'none';

        // Show file info
        fileInfo.style.display = 'flex';
        const nameEl = fileInfo.querySelector('.file-name');
        if (nameEl) {
            nameEl.textContent = file.name;
            nameEl.title = file.name;
        }
    }
}

async function handleReverse() {
    if (!selectedVideoFile) {
        showToast("请先上传一个视频文件！", "error");
        return;
    }

    const fps = document.getElementById('fps-input').value;
    const api = document.getElementById('api-select').value;
    const promptStyle = document.getElementById('prompt-style-select')?.value || 'clean';

    const placeholderView = document.getElementById('output-placeholder-view');
    const loadingView = document.getElementById('output-loading-view');
    const contentView = document.getElementById('output-content-view');
    const errorView = document.getElementById('output-error-view');
    const genBtn = document.getElementById('generate-btn');
    const reverseBtn = document.getElementById('reverse-btn');

    placeholderView.classList.remove('active');
    contentView.classList.remove('active');
    if (errorView) errorView.style.display = 'none';
    loadingView.classList.add('active');
    updateActiveGenerationBanner();

    if (reverseBtn) {
        reverseBtn.disabled = true;
        reverseBtn.classList.add('loading');
    }
    if (genBtn) {
        genBtn.disabled = true;
    }

    // Set custom loading text for video reverse engineering
    const loadingStage = document.getElementById('loading-stage-text');
    const loadingHeader = loadingView.querySelector('h3');

    // Generate unique taskId
    const taskId = Date.now().toString();
    const videoNameClean = selectedVideoFile.name.replace(/\.[^/.]+$/, "");
    const theme = `视频反推 (${videoNameClean})`;
    const creativity = api === 'openai' ? 'GPT-4o-Mini' : (api === 'gemini' ? 'Gemini-1.5-Flash' : '多模态反推');
    const dimensions = {
        theme: theme,
        creativity: creativity,
        type: 'reverse-video'
    };

    // Persist active task to localStorage so it survives refresh/close
    localStorage.setItem('spark_active_task_id', taskId);
    localStorage.setItem('spark_active_task_dimensions', JSON.stringify(dimensions));

    generationState.progressTaskType = 'reverse-video';
    generationState.progressState = window.ProgressModel ? ProgressModel.createProgressState('reverse-video') : null;
    setupLoadingSteps('reverse-video');

    if (loadingHeader) {
        loadingHeader.textContent = '正在反向逆向视频工程...';
    }
    if (loadingStage) {
        loadingStage.textContent = '正在提取视频关键帧、计算亮度与运动变化，并提交给多模态大模型进行时序 and 物理一致性分析。';
    }

    startLoadingTimer();
    
    // Log to terminal for cool visual progress feedback
    appendLiveTerminal("[SYSTEM] Video Upload received. Starting video reverse engineering pipeline...\n");
    appendLiveTerminal(`[SYSTEM] Video file: ${selectedVideoFile.name} (${(selectedVideoFile.size / (1024 * 1024)).toFixed(2)} MB)\n`);
    appendLiveTerminal(`[SYSTEM] Target sampling FPS: ${fps}\n`);
    appendLiveTerminal(`[SYSTEM] Prompt Style: ${promptStyle === 'clean' ? 'Clean Cinematic' : 'Strict Technical SCUP'}\n`);
    appendLiveTerminal(`[SYSTEM] Multi-modal API: ${api === 'auto' ? 'Auto-detecting Key' : api}\n`);
    appendLiveTerminal("[SYSTEM] Step 1: Extracting keyframes using FFmpeg...\n");

    const formData = new FormData();
    formData.append('file', selectedVideoFile);
    formData.append('fps', fps);
    formData.append('api', api);
    formData.append('prompt_style', promptStyle);
    formData.append('config', JSON.stringify(config));
    formData.append('task_id', taskId);

    currentGenerationController = new AbortController();

    try {
        const response = await fetch('/api/reverse-video', {
            method: 'POST',
            body: formData,
            signal: currentGenerationController.signal
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || `HTTP Error ${response.status}`);
        }

        // Wait for streaming progress
        await streamProgress(taskId, dimensions);

    } catch (err) {
        stopLoadingTimer();
        localStorage.removeItem('spark_active_task_id');
        localStorage.removeItem('spark_active_task_dimensions');
        currentGenerationController = null;
        console.error("Failed to reverse video:", err);

        loadingView.classList.remove('active');
        contentView.classList.remove('active');
        generationState.status = 'idle';

        if (err.name === 'AbortError') {
            placeholderView.classList.add('active');
            showToast("反推已被取消", "info");
        } else {
            generationState.status = 'error';
            if (errorView) {
                errorView.style.display = 'flex';
            } else {
                placeholderView.classList.add('active');
            }

            const errMsgEl = document.getElementById('error-message-text');
            const errorMsg = `反推失败：${err.message || '未知错误'}`;
            if (errMsgEl) {
                errMsgEl.textContent = errorMsg;
            }
            showToast(errorMsg, "error");
        }
    } finally {
        if (reverseBtn) {
            reverseBtn.disabled = false;
            reverseBtn.classList.remove('loading');
        }
        if (genBtn) {
            genBtn.disabled = false;
        }
        updateActiveGenerationBanner();
    }
}


function setupVideoUploadDragAndDrop() {
    const uploadZone = document.getElementById('upload-zone');
    if (!uploadZone) return;

    ['dragenter', 'dragover'].forEach(eventName => {
        uploadZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            uploadZone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        uploadZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            uploadZone.classList.remove('dragover');
        }, false);
    });

    uploadZone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length > 0) {
            handleFileSelect(files[0]);
        }
    });
}


// ==========================================================================
// Upstream Topic Ideation Engine (P2)
// --------------------------------------------------------------------------
let currentIdeatedIdeas = [];

async function loadIdeationCards(force = false) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    
    if (!force) {
        const cached = localStorage.getItem('ideation_cached_ideas');
        if (cached) {
            try {
                const parsed = JSON.parse(cached);
                if (parsed && Array.isArray(parsed) && parsed.length > 0) {
                    currentIdeatedIdeas = parsed;
                    renderIdeationCards(parsed);
                    return;
                }
            } catch (e) {
                console.error("Failed to parse cached ideas:", e);
            }
        }
    }
    
    container.innerHTML = '<div class="ideation-loading">正在寻找灵感中，请稍候...</div>';
    
    try {
        const response = await fetch('/api/ideate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                config: config,
                count: 4
            })
        });
        
        const data = await response.json();
        if (data.status === 'ok' && data.ideas) {
            currentIdeatedIdeas = data.ideas;
            localStorage.setItem('ideation_cached_ideas', JSON.stringify(data.ideas));
            renderIdeationCards(data.ideas);
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

/* ============================================================
   General Settings (通用设置) panel + 暗夜模式 toggle
   The general gear is ALWAYS visible (unlike the API-config gear,
   which app.js hides in managed mode). Theme persists in
   localStorage('spark_theme') and is shared across both apps
   (same origin), so main + image-station stay in sync.
   ============================================================ */
(function initThemeToggle() {
    const btn = document.getElementById('theme-toggle-btn');
    const icon = document.getElementById('theme-toggle-icon');
    if (!btn) return;
    const isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';
    const sync = () => {
        if (icon) icon.textContent = isDark() ? '☀️' : '🌙';
        btn.title = isDark() ? '切换到明亮模式' : '切换到暗夜模式';
    };
    sync();
    btn.addEventListener('click', () => {
        const next = !isDark();
        document.documentElement.setAttribute('data-theme', next ? 'dark' : 'light');
        try { localStorage.setItem('spark_theme', next ? 'dark' : 'light'); } catch (e) {}
        sync();
    });
})();

// Local Service Logs Stream
// Function initLocalServiceLogs moved to modular JS file

// --- LIGHTBOX SYSTEM ---
let lightboxItems = [];
let lightboxActiveIndex = -1;

// Function initLightbox moved to modular JS file

// Function openLightbox moved to modular JS file

// Function closeLightbox moved to modular JS file

// Function updateLightboxContent moved to modular JS file

// Function navigateLightbox moved to modular JS file

// Function handleLightboxKeydown moved to modular JS file

document.addEventListener('DOMContentLoaded', initLightbox);
