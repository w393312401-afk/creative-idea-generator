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
    imageAspectRatio: '9:16',
    imageQuality: '2K'
};

// Global State
let config = { ...DEFAULT_CONFIG };

// HTML Escape Utility to prevent XSS / render breakage
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// ---- External (server-managed) mode: access code + automatic header injection ----
// All /api/* requests transparently carry the access code (if one is set). If the server
// rejects it (401) we prompt for a new one and retry once. Implemented by wrapping window.fetch
// so every existing call site is covered without changes.
let ACCESS_CODE = localStorage.getItem('spark_access_code') || '';

function _setAccessHeader(init, code) {
    const headers = new Headers((init && init.headers) || {});
    headers.set('X-Access-Code', code);
    init.headers = headers;
    return init;
}

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
function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    } else {
        return new Promise((resolve, reject) => {
            try {
                const textArea = document.createElement("textarea");
                textArea.value = text;
                textArea.style.top = "0";
                textArea.style.left = "0";
                textArea.style.position = "fixed";
                textArea.style.opacity = "0";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                const successful = document.execCommand('copy');
                document.body.removeChild(textArea);
                if (successful) {
                    resolve();
                } else {
                    reject(new Error("Fallback copy command failed"));
                }
            } catch (err) {
                reject(err);
            }
        });
    }
}

// Custom Styled Dialog Prompt Modal (replacing window.prompt)
function customPrompt(message, defaultValue = '') {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.id = 'custom-prompt-modal';
        modal.style.zIndex = '1100';
        
        modal.innerHTML = `
            <div class="modal-content glass-panel" style="max-width: 400px; border-color: var(--neon-cyan);">
                <div class="modal-header">
                    <h3>输入名称</h3>
                    <button class="close-btn">&times;</button>
                </div>
                <div class="modal-body" style="padding-top: 10px;">
                    <p style="margin-bottom: 12px; font-size: 13px; color: var(--text-secondary);">${message}</p>
                    <div class="form-group" style="margin-bottom: 0;">
                        <input type="text" id="custom-prompt-input" value="${defaultValue}" style="width:100%; border-color: rgba(255,255,255,0.15);" autofocus>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="action-btn text-btn secondary cancel-btn">取消</button>
                    <button class="action-btn text-btn primary confirm-btn" style="background: var(--neon-cyan); border-color: rgba(0,242,254,0.4); color: #000; font-weight:700;">确定</button>
                </div>
            </div>
        `;
        
        document.body.appendChild(modal);
        
        const input = modal.querySelector('#custom-prompt-input');
        input.focus();
        input.select();
        
        const close = () => {
            modal.classList.remove('active');
            setTimeout(() => modal.remove(), 200);
        };
        
        modal.querySelector('.close-btn').addEventListener('click', () => {
            close();
            resolve(null);
        });
        
        modal.querySelector('.cancel-btn').addEventListener('click', () => {
            close();
            resolve(null);
        });
        
        modal.querySelector('.confirm-btn').addEventListener('click', () => {
            const val = input.value;
            close();
            resolve(val);
        });
        
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                modal.querySelector('.confirm-btn').click();
            } else if (e.key === 'Escape') {
                modal.querySelector('.cancel-btn').click();
            }
        });
    });
}

// Custom Styled Confirm Modal (replacing window.confirm)
function customConfirm(message) {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.id = 'custom-confirm-modal';
        modal.style.zIndex = '1100';
        
        modal.innerHTML = `
            <div class="modal-content glass-panel" style="max-width: 400px; border-color: var(--neon-purple);">
                <div class="modal-header">
                    <h3>操作确认</h3>
                    <button class="close-btn">&times;</button>
                </div>
                <div class="modal-body" style="padding-top: 10px;">
                    <p style="font-size: 13.5px; line-height: 1.5; color: var(--text-secondary);">${message}</p>
                </div>
                <div class="modal-footer">
                    <button class="action-btn text-btn secondary cancel-btn">取消</button>
                    <button class="action-btn text-btn primary confirm-btn" style="background: var(--neon-purple); border-color: rgba(157,78,221,0.4); color: #fff; font-weight:600;">确定</button>
                </div>
            </div>
        `;
        
        document.body.appendChild(modal);
        
        const close = () => {
            modal.classList.remove('active');
            setTimeout(() => modal.remove(), 200);
        };
        
        modal.querySelector('.close-btn').addEventListener('click', () => {
            close();
            resolve(false);
        });
        
        modal.querySelector('.cancel-btn').addEventListener('click', () => {
            close();
            resolve(false);
        });
        
        modal.querySelector('.confirm-btn').addEventListener('click', () => {
            close();
            resolve(true);
        });
        
        modal.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                modal.querySelector('.confirm-btn').click();
            } else if (e.key === 'Escape') {
                modal.querySelector('.cancel-btn').click();
            }
        });
        
        modal.focus();
    });
}


// Initialize Elements
document.addEventListener('DOMContentLoaded', () => {
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
            loadIdeationCards();
        });
    }
    initLocalServiceLogs();
});

function saveSelectionState() {
    const activeThemeBtn = document.querySelector('#theme-selector .theme-btn.active');
    const selectedTheme = activeThemeBtn ? activeThemeBtn.dataset.value : 'hollow_oak';
    
    const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
        .map(node => node.dataset.value);
        
    const state = {
        theme: selectedTheme,
        anchors: activeAnchors,
        complexity: document.getElementById('slider-complexity').value,
        budget: document.getElementById('slider-budget').value,
        ratio: document.getElementById('slider-ratio').value,
        creativity: document.getElementById('slider-creativity').value,
        beats: document.getElementById('slider-beats').value
    };
    
    localStorage.setItem('spark_selection_state', JSON.stringify(state));
    updateConfigSummary();
}

function loadSelectionState() {
    const stored = localStorage.getItem('spark_selection_state');
    if (!stored) {
        updateConfigSummary();
        return;
    }
    
    try {
        const state = JSON.parse(stored);
        
        // Theme
        if (state.theme) {
            document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => {
                if (btn.dataset.value === state.theme) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
        }
        
        // Anchors
        if (Array.isArray(state.anchors)) {
            document.querySelectorAll('#anchor-selector .anchor-node').forEach(btn => {
                if (state.anchors.includes(btn.dataset.value)) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
        }
        
        // Sliders
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (el && val !== undefined) {
                el.value = val;
                el.dispatchEvent(new Event('input'));
            }
        };
        setVal('slider-complexity', state.complexity);
        setVal('slider-budget', state.budget);
        setVal('slider-ratio', state.ratio);
        setVal('slider-creativity', state.creativity);
        setVal('slider-beats', state.beats);
        
    } catch (e) {
        console.error("Failed to load selection state", e);
    }
    updateConfigSummary();
}

function updateConfigSummary() {
    const activeThemeBtn = document.querySelector('#theme-selector .theme-btn.active');
    const themeText = activeThemeBtn ? activeThemeBtn.querySelector('.theme-name').textContent.trim() : '未选主题';
    
    const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
        .map(node => {
            const text = node.textContent.trim();
            const bracketIdx = text.indexOf('(');
            return bracketIdx !== -1 ? text.substring(0, bracketIdx).trim() : text;
        });
        
    const anchorsStr = activeAnchors.length > 0 ? ` + ${activeAnchors.join('、')}` : '';
    
    const complexityVal = document.getElementById('slider-complexity').value;
    const complexityLabels = { 1: '轻量', 2: '中等', 3: '硬核' };
    const complexityText = complexityLabels[complexityVal] || '中等';
    
    const budgetVal = document.getElementById('slider-budget').value;
    const budgetLabels = { 1: '平民', 2: '轻奢', 3: '顶奢' };
    const budgetText = budgetLabels[budgetVal] || '轻奢';
    
    const ratioVal = document.getElementById('slider-ratio').value;
    
    const creativityVal = document.getElementById('slider-creativity').value;
    const creativityLabels = { 1: '常规', 2: '突破', 3: '脑洞' };
    const creativityText = creativityLabels[creativityVal] || '常规';
    
    const beatsVal = document.getElementById('slider-beats').value;
    
    const summaryText = `${themeText}${anchorsStr} | 复杂度:${complexityText}, 预算:${budgetText}, 反差:${ratioVal}%, 尺度:${creativityText}, ${beatsVal}拍`;
    
    const summaryEl = document.getElementById('config-summary-text');
    if (summaryEl) {
        summaryEl.textContent = summaryText;
    }
}

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

function applyPreset(presetName) {
    const p = PRESETS[presetName];
    if (!p) return;
    
    // Theme
    document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => {
        if (btn.dataset.value === p.theme) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    // Anchors
    document.querySelectorAll('#anchor-selector .anchor-node').forEach(btn => {
        if (p.anchors.includes(btn.dataset.value)) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    // Sliders
    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) {
            el.value = val;
            el.dispatchEvent(new Event('input'));
        }
    };
    setVal('slider-complexity', p.complexity);
    setVal('slider-budget', p.budget);
    setVal('slider-ratio', p.ratio);
    setVal('slider-creativity', p.creativity);
    setVal('slider-beats', p.beats);
    
    saveSelectionState();
    showToast(`已应用预设：${presetName === 'tech_hardcore' ? '科技硬核' : presetName === 'eco_natural' ? '自然生态' : '废土实用'}`, 'success');
}

function safeSetImageSrc(imgEl, url) {
    if (!imgEl) return;
    if (!url) {
        imgEl.removeAttribute('src');
        return;
    }
    const lower = url.trim().toLowerCase();
    const isSafe = lower.startsWith('http://') || 
                   lower.startsWith('https://') || 
                   lower.startsWith('data:image/') ||
                   lower.startsWith('/') ||
                   lower.startsWith('outputs/');
    if (isSafe) {
        // Add cache buster for local files to avoid caching issues during regeneration
        let finalUrl = url;
        if (!lower.startsWith('http://') && !lower.startsWith('https://') && !lower.startsWith('data:image/')) {
            const separator = url.includes('?') ? '&' : '?';
            finalUrl = url + separator + 't=' + Date.now();
        }
        imgEl.src = finalUrl;
    } else {
        console.warn("Blocked potentially unsafe image URL:", url);
        imgEl.removeAttribute('src');
    }
}

function parsePromptBlock(blockText) {
    const lines = (blockText || '').split('\n');
    const slots = [];
    let currentSlot = null;
    let currentBody = [];
    
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;
        
        const imgMatch = line.match(/^(?:图片)\s*(\d+)\s*:/i);
        const vidMatch = line.match(/^(?:视频)\s*(\d+)\s*:/i);
        
        if (imgMatch || vidMatch) {
            if (currentSlot) {
                currentSlot.body = currentBody.join('\n').trim();
                slots.push(currentSlot);
            }
            
            const isImage = !!imgMatch;
            const index = parseInt(isImage ? imgMatch[1] : vidMatch[1], 10);
            
            currentSlot = {
                type: isImage ? 'image' : 'video',
                index: index,
                label: isImage ? `图片提示词 ${index}` : `视频提示词 ${index}`,
                id: isImage ? `slot-image-${index}` : `slot-video-${index}`,
                body: ''
            };
            currentBody = [];
        } else if (line === '图片提示词' || line === '视频提示词' || line.startsWith('===') || line.startsWith('---') || line.startsWith('***') || line.startsWith('___')) {
            // Header or separators - skip
        } else {
            if (currentSlot) {
                currentBody.push(lines[i]);
            }
        }
    }
    
    if (currentSlot) {
        currentSlot.body = currentBody.join('\n').trim();
        slots.push(currentSlot);
    }
    
    return slots;
}

function renderParsedPrompts(blockText) {
    const slots = parsePromptBlock(blockText);
    const container = document.getElementById('parsed-prompts-container');
    const jumpPills = document.getElementById('jump-pills');
    
    if (!container) return;
    
    container.innerHTML = '';
    if (jumpPills) jumpPills.innerHTML = '';
    
    if (slots.length === 0) {
        container.innerHTML = '<p class="audit-empty">未解析到提示词槽位，请查看下方原始提示词。</p>';
        return;
    }
    
    slots.sort((a, b) => {
        if (a.type !== b.type) {
            return a.type === 'image' ? -1 : 1;
        }
        return a.index - b.index;
    });
    
    slots.forEach(slot => {
        if (jumpPills) {
            const pill = document.createElement('button');
            pill.type = 'button';
            pill.className = `jump-pill ${slot.type === 'image' ? 'image-pill' : 'video-pill'}`;
            pill.textContent = slot.type === 'image' ? `图 ${slot.index}` : `视 ${slot.index}`;
            pill.addEventListener('click', () => {
                const el = document.getElementById(slot.id);
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    el.style.borderColor = slot.type === 'image' ? 'var(--neon-cyan)' : 'var(--neon-purple)';
                    el.style.boxShadow = slot.type === 'image' ? '0 0 20px rgba(0,242,254,0.4)' : '0 0 20px rgba(157,78,221,0.4)';
                    setTimeout(() => {
                        el.style.borderColor = 'var(--border-color)';
                        el.style.boxShadow = 'none';
                    }, 1000);
                }
            });
            jumpPills.appendChild(pill);
        }
        
        const card = document.createElement('div');
        card.className = `prompt-slot-card ${slot.type}-slot`;
        card.id = slot.id;
        
        const header = document.createElement('div');
        header.className = 'prompt-slot-header';
        
        const label = document.createElement('span');
        label.className = 'slot-label';
        label.textContent = slot.type === 'image' ? `🖼️ 图片 ${slot.index}` : `🎬 视频 ${slot.index}`;
        header.appendChild(label);
        
        const actions = document.createElement('div');
        actions.className = 'slot-actions';
        
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-btn text-btn mini-btn';
        copyBtn.innerHTML = '复制';
        copyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyText(slot.body).then(() => {
                showToast(`${slot.type === 'image' ? '图片' : '视频'} ${slot.index} 提示词复制成功！`, 'success');
                copyBtn.innerHTML = '已复制！✓';
                copyBtn.classList.add('copied');
                setTimeout(() => {
                    copyBtn.innerHTML = '复制';
                    copyBtn.classList.remove('copied');
                }, 1000);
            }).catch(err => {
                showToast("复制失败", "error");
            });
        });
        
        const foldBtn = document.createElement('button');
        foldBtn.className = 'action-btn text-btn mini-btn';
        foldBtn.innerHTML = '收起';
        
        actions.appendChild(copyBtn);
        actions.appendChild(foldBtn);
        header.appendChild(actions);
        
        const body = document.createElement('div');
        body.className = 'prompt-slot-body';
        body.textContent = slot.body;
        
        foldBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (body.style.display === 'none') {
                body.style.display = 'block';
                foldBtn.innerHTML = '收起';
            } else {
                body.style.display = 'none';
                foldBtn.innerHTML = '展开';
            }
        });
        
        card.appendChild(header);
        card.appendChild(body);
        container.appendChild(card);
    });

    // Re-apply current slot filter to newly rendered slots
    applySlotFilter();
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
    // Auto-scroll to bottom
    body.scrollTop = body.scrollHeight;
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
function updateImageModelOptions(preserveValue = true) {
    const modelSelect = document.getElementById('settings-model');
    const imageModelSelect = document.getElementById('settings-image-model');
    if (!modelSelect || !imageModelSelect) return;

    const mainModel = modelSelect.value;
    const previousValue = imageModelSelect.value;
    
    imageModelSelect.innerHTML = '';

    const models = IMAGE_MODELS_BY_MAIN_MODEL[mainModel] || [
        { value: 'nano-banana-2', label: '🍌 Nano Banana 2' }
    ];

    models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.value;
        opt.textContent = m.label;
        imageModelSelect.appendChild(opt);
    });

    if (preserveValue && previousValue) {
        const optionExists = models.some(m => m.value === previousValue);
        if (optionExists) {
            imageModelSelect.value = previousValue;
            return;
        }
    }

    if (models.length > 0) {
        imageModelSelect.value = models[0].value;
    }
}

// Load configuration from localStorage
function loadConfig() {
    const stored = localStorage.getItem('spark_config');
    if (stored) {
        try {
            config = { ...DEFAULT_CONFIG, ...JSON.parse(stored) };
            
            // Auto-migrate legacy port 65038 defaults to new port 8045 defaults
            if (config.baseUrl === 'http://localhost:65038/v1' && config.apiKey === 'agt_codex_JG9xnyWXYBS4qMmO9Z0UKD3pbEOpHr7M') {
                config.baseUrl = DEFAULT_CONFIG.baseUrl;
                config.apiKey = DEFAULT_CONFIG.apiKey;
                config.model = DEFAULT_CONFIG.model;
                config.imageModel = DEFAULT_CONFIG.imageModel;
                config.imageAspectRatio = DEFAULT_CONFIG.imageAspectRatio;
                config.imageQuality = DEFAULT_CONFIG.imageQuality;
                localStorage.setItem('spark_config', JSON.stringify(config));
            }

            // Auto-migrate legacy port 8080 / 8045 defaults to new active port 8046 defaults
            if (config.baseUrl === 'http://127.0.0.1:8080/v1' || config.baseUrl === 'http://localhost:8080/v1' ||
                config.baseUrl === 'http://127.0.0.1:8045/v1' || config.baseUrl === 'http://localhost:8045/v1') {
                config.baseUrl = DEFAULT_CONFIG.baseUrl;
                localStorage.setItem('spark_config', JSON.stringify(config));
            }
        } catch (e) {
            console.error("Failed to parse stored config, using defaults", e);
        }
    }
    
    // Fill settings inputs
    document.getElementById('settings-base-url').value = config.baseUrl;
    document.getElementById('settings-api-key').value = config.apiKey;
    
    const modelSelect = document.getElementById('settings-model');
    // Ensure all options are checked, and append custom option if config.model is not in the list
    let optionExists = false;
    for (let i = 0; i < modelSelect.options.length; i++) {
        if (modelSelect.options[i].value === config.model) {
            optionExists = true;
            break;
        }
    }
    if (!optionExists) {
        const opt = document.createElement('option');
        opt.value = config.model;
        opt.textContent = `${config.model} (自定义)`;
        modelSelect.appendChild(opt);
    }
    modelSelect.value = config.model;

    // Dynamically update image model dropdown choices based on loaded main model
    updateImageModelOptions(false);

    // Load image model option
    const imageModelSelect = document.getElementById('settings-image-model');
    if (imageModelSelect) {
        let imgOptionExists = false;
        const currentImgModel = config.imageModel || 'nano-banana-2';
        for (let i = 0; i < imageModelSelect.options.length; i++) {
            if (imageModelSelect.options[i].value === currentImgModel) {
                imgOptionExists = true;
                break;
            }
        }
        if (!imgOptionExists) {
            const opt = document.createElement('option');
            opt.value = currentImgModel;
            opt.textContent = `${currentImgModel} (自定义)`;
            imageModelSelect.appendChild(opt);
        }
        imageModelSelect.value = currentImgModel;
    }

    // Load aspect ratio option
    const imageRatioSelect = document.getElementById('settings-image-ratio');
    if (imageRatioSelect) {
        imageRatioSelect.value = config.imageAspectRatio || '9:16';
    }

    // Load quality/clarity option
    const imageQualitySelect = document.getElementById('settings-image-quality');
    if (imageQualitySelect) {
        imageQualitySelect.value = config.imageQuality || '2K';
    }

    // Initialize GPT port selector value based on config.baseUrl
    const gptPortSelect = document.getElementById('settings-gpt-port');
    if (gptPortSelect) {
        let currentPort = '65038'; // default
        try {
            const match = config.baseUrl.match(/:(\d+)/);
            if (match) {
                currentPort = match[1];
            }
        } catch (e) {}
        
        let portOptionExists = false;
        for (let i = 0; i < gptPortSelect.options.length; i++) {
            if (gptPortSelect.options[i].value === currentPort) {
                portOptionExists = true;
                break;
            }
        }
        if (!portOptionExists) {
            const opt = document.createElement('option');
            opt.value = currentPort;
            opt.textContent = `${currentPort} (自定义)`;
            gptPortSelect.appendChild(opt);
        }
        gptPortSelect.value = currentPort;
    }
    
    updateGptPortVisibility();
    updateCoverModelDisplay();
}

// Helper to show/hide GPT port selector based on model selection
function updateGptPortVisibility() {
    const modelSelect = document.getElementById('settings-model');
    const gptPortGroup = document.getElementById('gpt-port-group');
    if (modelSelect && gptPortGroup) {
        gptPortGroup.style.display = modelSelect.value === 'gpt-5.5' ? 'block' : 'none';
    }
}

// Save configuration to localStorage
function saveConfig() {
    config.baseUrl = document.getElementById('settings-base-url').value.trim();
    config.apiKey = document.getElementById('settings-api-key').value.trim();
    config.model = document.getElementById('settings-model').value.trim();
    config.imageModel = document.getElementById('settings-image-model').value.trim();
    config.imageAspectRatio = document.getElementById('settings-image-ratio').value.trim();
    config.imageQuality = document.getElementById('settings-image-quality').value.trim();
    
    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    showToast("API 配置保存成功！", "success");
    checkApiStatus();
}

// Reset configuration to default
function resetConfig() {
    document.getElementById('settings-base-url').value = DEFAULT_CONFIG.baseUrl;
    document.getElementById('settings-api-key').value = DEFAULT_CONFIG.apiKey;
    document.getElementById('settings-model').value = DEFAULT_CONFIG.model;
    // Update the image model options list back to default options first
    updateImageModelOptions(false);
    document.getElementById('settings-image-model').value = DEFAULT_CONFIG.imageModel;
    document.getElementById('settings-image-ratio').value = DEFAULT_CONFIG.imageAspectRatio;
    document.getElementById('settings-image-quality').value = DEFAULT_CONFIG.imageQuality;
    updateGptPortVisibility();
}

// Update the displayed cover generation parameters in the page description
function updateCoverModelDisplay() {
    const modelEl = document.getElementById('cover-desc-model');
    if (modelEl) {
        modelEl.textContent = config.imageModel || 'nano-banana-2';
    }
    const ratioEl = document.getElementById('cover-desc-ratio');
    if (ratioEl) {
        ratioEl.textContent = config.imageAspectRatio || '9:16';
    }
    const qualityEl = document.getElementById('cover-desc-quality');
    if (qualityEl) {
        qualityEl.textContent = config.imageQuality || '2K';
    }
}

// Save the currently active idea state to localStorage so it survives page refresh
function saveCurrentIdeaState() {
    if (currentIdea) {
        localStorage.setItem('spark_current_idea', JSON.stringify(currentIdea));
    } else {
        localStorage.removeItem('spark_current_idea');
    }
}

// Restore the last viewed idea from localStorage on page load
function loadCurrentIdeaState() {
    const stored = localStorage.getItem('spark_current_idea');
    if (stored) {
        try {
            const idea = JSON.parse(stored);
            if (idea) {
                currentIdea = idea;
                renderIdea(idea);
                
                const placeholderView = document.getElementById('output-placeholder-view');
                const contentView = document.getElementById('output-content-view');
                const loadingView = document.getElementById('output-loading-view');
                
                if (placeholderView) placeholderView.classList.remove('active');
                if (loadingView) loadingView.classList.remove('active');
                if (contentView) contentView.classList.add('active');
                
                const lastTab = localStorage.getItem('spark_active_tab') || 'overview';
                switchTab(lastTab);
                
                updateActiveGenerationBanner();
            }
        } catch (e) {
            console.error("Failed to load current idea state", e);
        }
    }
}

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
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resize, 150);
    });
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
    
    function animate() {
        requestAnimationFrame(animate);

        // Skip all drawing while the tab is hidden; otherwise run at the display's
        // native refresh rate. Per-frame work is just a handful of dots, so it stays smooth.
        if (document.hidden) return;

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
        tasksSearchInput.addEventListener('input', (e) => {
            tasksSearchQuery = e.target.value;
            renderTasks();
        });
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
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity', 'slider-beats'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', updateConfigSummary);
            el.addEventListener('change', saveSelectionState);
        }
    });

    // Save state on selectors click
    document.getElementById('theme-selector').addEventListener('click', () => {
        setTimeout(saveSelectionState, 50);
    });
    document.getElementById('anchor-selector').addEventListener('click', () => {
        setTimeout(saveSelectionState, 50);
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
    document.getElementById('make-cover-btn').addEventListener('click', () => generateCover());
    document.getElementById('generate-frames-btn').addEventListener('click', () => generateFrames());
    document.getElementById('generate-videos-btn').addEventListener('click', () => generateVideos());
    document.getElementById('merge-videos-btn').addEventListener('click', () => mergeVideos());
    document.getElementById('copy-hook-btn').addEventListener('click', () => {
        const val = document.getElementById('cover-hook-val').textContent;
        if (val) {
            copyText(val).then(() => {
                showToast("英文文案已复制！", "success");
            }).catch(err => {
                showToast("复制失败", "error");
            });
        }
    });

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

function updateTasksBadge(tasksList) {
    const badge = document.getElementById('active-task-count');
    if (!badge) return;
    
    const runningTasksCount = tasksList.filter(t => t.status === 'running').length;
    if (runningTasksCount > 0) {
        badge.textContent = runningTasksCount;
        badge.style.display = 'flex';
    } else {
        badge.style.display = 'none';
    }
}

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
                            meta.textContent = `正在生成帧序列: ${cur}/${tot} (正在处理第 ${cur + 1} 帧)...`;
                            
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
                        } else if (parsed.type === 'merge_start') {
                            meta.textContent = '正在自动合并并加速视频 (2x Speed)...';
                        } else if (parsed.type === 'merge_done') {
                            meta.textContent = '所有视频已成功生成并合并加速！';
                        } else if (parsed.type === 'merge_error') {
                            meta.textContent = `自动合并视频失败: ${parsed.data.message || '未知错误'}`;
                            showToast(`自动合并失败: ${parsed.data.message || '未知错误'}`, "warning");
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
function renderIdea(result) {
    document.getElementById('idea-title').textContent = result.title || '未命名创意';
    document.getElementById('tag-theme').textContent = result.theme || '';
    document.getElementById('tag-creativity').textContent = result.creativity || '';

    renderRepairBanner(result.repair_md);
    document.getElementById('idea-prompt-block').textContent = result.prompt_block || '（本次未返回提示词内容）';
    document.getElementById('idea-audit').innerHTML = renderAuditMarkdown(result.audit_md);
    
    // Parse slots and render them
    renderParsedPrompts(result.prompt_block);
    
    // Collapsible Audit panel logic: default fold, auto expand & highlight on repair
    const auditDetails = document.getElementById('audit-details');
    const hasRepairs = result.repair_md && result.repair_md.trim() && 
                        !/^PASS/i.test(result.repair_md.trim()) && 
                        !result.repair_md.includes('未发现违规');
    if (auditDetails) {
        if (hasRepairs) {
            auditDetails.open = true;
            auditDetails.classList.add('warning-highlight');
        } else {
            auditDetails.open = false;
            auditDetails.classList.remove('warning-highlight');
        }
    }

    // Sync favorite state
    updateFavoriteButtonState();

    // Render collage preview if available
    const collageWrapper = document.getElementById('collage-preview-wrapper');
    const collageImg = document.getElementById('collage-preview-img');
    const collageDownload = document.getElementById('collage-download-link');
    if (collageWrapper && collageImg) {
        if (result.collage_url) {
            collageImg.src = result.collage_url;
            if (collageDownload) collageDownload.href = result.collage_url;
            collageWrapper.style.display = 'block';
            
            collageImg.onclick = () => {
                openLightbox([{
                    type: 'image',
                    url: result.collage_url,
                    caption: '<strong>视频关键帧多宫格拼图 (Keyframe Collage)</strong>'
                }], 0);
            };
        } else {
            collageWrapper.style.display = 'none';
            collageImg.src = '';
            collageImg.onclick = null;
        }
    }

    // Render covers
    renderCoversForIdea(result);

    // Asynchronously fetch latest manifest (frames & videos) from server if it exists
    fetch(`/api/get_manifest?title=${encodeURIComponent(result.title)}`)
        .then(resp => {
            if (resp.ok) {
                return resp.json();
            }
            throw new Error('Not found');
        })
        .then(manifest => {
            result.frameRun = manifest;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifest;
                saveLibrary();
            }
            renderFramesForIdea(result);
            renderVideosForIdea(result);
        })
        .catch(e => {
            // If not found or error, render using whatever is in result
            renderFramesForIdea(result);
            renderVideosForIdea(result);
        });
}

// Show the result of the second-pass construction-order / causality check.
function renderRepairBanner(repairMd) {
    const el = document.getElementById('idea-repair');
    if (!el) return;
    const rm = (repairMd || '').trim();
    if (!rm) {
        el.style.display = 'none';
        el.textContent = '';
        return;
    }
    const passed = /^PASS/i.test(rm) || rm.includes('未发现违规');
    el.style.display = 'block';
    el.className = 'repair-banner ' + (passed ? 'ok' : 'fixed');
    el.textContent = (passed ? '✅ 工序与场景一致性校验：' : '🔧 工序与场景一致性校验·已修复：\n') + rm;
}

// Split a Markdown table row into trimmed cells, dropping the leading/trailing
// empties produced by border pipes.
function splitTableRow(line) {
    const cells = line.split('|').map(c => c.trim());
    if (cells.length && cells[0] === '') cells.shift();
    if (cells.length && cells[cells.length - 1] === '') cells.pop();
    return cells;
}

// Minimal Markdown renderer for the audit report: handles GitHub-style tables and
// plain paragraphs, escaping all HTML.
function renderAuditMarkdown(md) {
    if (!md) return '<p class="audit-empty">（本次未返回审核报告）</p>';
    const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Escape HTML, then promote **bold** and `code` Markdown to inline tags.
    const inline = (s) => esc(s)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
    const lines = md.split('\n');
    let html = '';
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        const isTableHeader = line.includes('|') && i + 1 < lines.length &&
            /^[\s|:\-]+$/.test(lines[i + 1]) && lines[i + 1].includes('-');
        if (isTableHeader) {
            const header = splitTableRow(line);
            const rows = [];
            i += 2;
            while (i < lines.length && lines[i].includes('|')) {
                rows.push(splitTableRow(lines[i]));
                i++;
            }
            html += '<table class="audit-table"><thead><tr>' +
                header.map(h => `<th>${inline(h)}</th>`).join('') +
                '</tr></thead><tbody>' +
                rows.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
                '</tbody></table>';
            continue;
        }
        const t = line.trim();
        if (t) html += `<p>${inline(t.replace(/^#+\s*/, ''))}</p>`;
        i++;
    }
    return html || '<p class="audit-empty">（本次未返回审核报告）</p>';
}

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

    let hookMarkdown = '';
    if (currentIdea.english_title) {
        hookMarkdown = `\n*TikTok US 英文文案：${currentIdea.english_title}*\n`;
    }

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
function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    // Add icon
    const icon = type === 'success' ? '✓' : '✗';
    toast.innerHTML = `<span class="toast-icon">${icon}</span> <span class="toast-message">${message}</span>`;
    
    container.appendChild(toast);
    
    // Remove after 3s
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        setTimeout(() => toast.remove(), 200);
    }, 3000);
}

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
                title: currentIdea.title,
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


function renderFramesForIdea(idea) {
    const grid = document.getElementById('frames-grid');
    const meta = document.getElementById('frames-meta');
    if (!grid || !meta) return;

    const frameRun = idea && idea.frameRun;
    const frames = (frameRun && frameRun.frames) || [];
    grid.innerHTML = '';

    // If there are no frames, and no prompt_block, show empty
    if (!frames.length && (!idea || !idea.prompt_block)) {
        meta.textContent = '尚未生成任何帧序列。';
        return;
    }

    // Get expected image slots
    const slots = parsePromptBlock(idea ? idea.prompt_block : '');
    const imageSlots = slots.filter(s => s.type === 'image').sort((a, b) => a.index - b.index);

    if (imageSlots.length === 0) {
        if (!frames.length) {
            meta.textContent = '尚未生成任何帧序列。';
            return;
        }
    }

    const totalFramesCount = imageSlots.length || frames.length;
    const manifestText = (frameRun && frameRun.manifest) ? ` 清单: ${frameRun.manifest}` : '';
    const dirText = (frameRun && frameRun.project_dir) ? `，保存在 ${frameRun.project_dir || 'outputs'}.${manifestText}` : '';
    
    const generatedCount = frames.filter(f => f.url || f.file).length;
    meta.textContent = `已生成 ${generatedCount}/${totalFramesCount} 帧连续帧序列图${dirText}`;

    // Loop through the slots (or frames if slots is empty)
    const itemsToRender = imageSlots.length > 0 
        ? imageSlots.map((slot, idx) => {
            const seq = idx + 1; // 1-based sequence
            const frame = frames.find(f => f.sequence === seq || f.slot === slot.index);
            return {
                sequence: seq,
                slot: slot.index,
                frame: frame
            };
          })
        : frames.map((f, idx) => ({
            sequence: f.sequence || (idx + 1),
            slot: f.slot || (idx + 1),
            frame: f
          }));

    itemsToRender.forEach(item => {
        const seq = item.sequence;
        const frame = item.frame;
        
        const card = document.createElement('div');
        card.id = `frame-slot-${seq}`;
        
        const hasImage = frame && (frame.url || frame.file);
        
        if (hasImage) {
            const isDegraded = frame.quality_gate === 'i2i_fallback_degraded';
            const isVlmFailed = frame.quality_gate === 'vlm_qa_failed';
            card.className = 'frame-card' + (isDegraded ? ' degraded-card' : '') + (isVlmFailed ? ' vlm-failed-card' : '');
            card.style.cursor = 'pointer';
            
            let hoverTitle = `打开第 ${seq} 帧`;
            if (isDegraded) hoverTitle += ' (降级为文生图)';
            if (isVlmFailed) hoverTitle += ` (VLM 检查未通过: ${frame.vlm_qa_reason || '跳变或无变化'})`;
            card.title = hoverTitle;
            
            card.innerHTML = `
                <img src="" alt="Frame ${seq}" loading="lazy">
                ${isDegraded ? '<div class="degraded-badge">降级</div>' : ''}
                ${isVlmFailed ? '<div class="vlm-failed-badge" title="' + (frame.vlm_qa_reason || '').replace(/"/g, '&quot;') + '">VLM 失败</div>' : ''}
                <div class="frame-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
                    <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;">重试</button>
                </div>
                <span>IMG ${String(seq).padStart(3, '0')}</span>
            `;
            
            safeSetImageSrc(card.querySelector('img'), frame.url || frame.file);
            
            // Hover effect to show action buttons
            card.addEventListener('mouseenter', () => {
                const actions = card.querySelector('.frame-card-actions');
                if (actions) actions.style.opacity = '1';
            });
            card.addEventListener('mouseleave', () => {
                const actions = card.querySelector('.frame-card-actions');
                if (actions) actions.style.opacity = '0';
            });
            
            // Click on the card opens lightbox (excluding the retry button)
            card.addEventListener('click', (e) => {
                if (e.target.classList.contains('retry-frame-btn')) return;
                
                // Get all valid frames for the lightbox
                const validFrames = itemsToRender
                    .filter(i => i.frame && (i.frame.url || i.frame.file))
                    .map(i => i.frame);
                
                const mediaList = validFrames.map((f) => ({
                    type: 'image',
                    url: f.url || f.file,
                    caption: `<strong>第 ${f.sequence} 帧 / 共 ${validFrames.length} 帧</strong>`
                }));
                
                const clickedIndex = validFrames.findIndex(f => f.sequence === seq);
                openLightbox(mediaList, clickedIndex >= 0 ? clickedIndex : 0);
            });
            
            card.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleFrame(seq);
            });
        } else {
            // Missing or failed frame
            card.className = 'frame-card video-failed-card';
            card.style.cursor = 'default';
            card.innerHTML = `
                <div class="video-failed-placeholder">
                    <span class="error-icon">⚠️</span>
                    <span class="error-text" style="font-size: 11px; color: var(--text-secondary);">未生成/已失效</span>
                    <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}">生成</button>
                </div>
                <span>IMG ${String(seq).padStart(3, '0')}</span>
            `;
            
            card.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleFrame(seq);
            });
        }
        
        grid.appendChild(card);
    });
}

function renderVideosForIdea(idea) {
    const grid = document.getElementById('videos-grid');
    const meta = document.getElementById('videos-meta');
    if (!grid || !meta) return;

    const frameRun = idea && idea.frameRun;
    const videos = (frameRun && frameRun.videos) || [];
    grid.innerHTML = '';

    const mergedContainer = document.getElementById('merged-video-container');
    const mergedPlayer = document.getElementById('merged-video-player');
    const mergedInfo = document.getElementById('merged-video-info');
    const mergedDownload = document.getElementById('merged-video-download');

    if (mergedContainer) {
        if (frameRun && frameRun.merged_video && frameRun.merged_video.status === 'success') {
            const mv = frameRun.merged_video;
            mergedContainer.style.display = 'block';
            if (mergedPlayer) {
                mergedPlayer.src = mv.url;
            }
            if (mergedDownload) {
                mergedDownload.href = mv.url;
                mergedDownload.download = `${idea.title || 'video'}_merged_2x.mp4`;
            }
            if (mergedInfo) {
                const sizeMB = mv.size_bytes ? (mv.size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '未知大小';
                const durationSec = mv.duration_seconds ? mv.duration_seconds + ' 秒' : '未知时长';
                mergedInfo.textContent = `文件大小: ${sizeMB} | 视频时长: ${durationSec}`;
            }
        } else {
            mergedContainer.style.display = 'none';
            if (mergedPlayer) {
                mergedPlayer.removeAttribute('src');
                mergedPlayer.load();
            }
        }
    }

    if (!videos.length) {
        meta.textContent = '尚未生成任何视频序列。';
        return;
    }

    const manifestText = frameRun.manifest ? ` 清单: ${frameRun.manifest}` : '';
    meta.textContent = `已生成 ${videos.length} 段连续视频，保存在 ${frameRun.project_dir || 'outputs'}.${manifestText}`;

    videos.forEach(video => {
        const card = document.createElement('div');
        card.id = `video-slot-${video.slot}`;
        
        const isFailed = video.status === 'failed' || (!video.url && !video.file);
        
        const startImg = String(video.slot).padStart(3, '0');
        const endImg = String(video.slot + 1).padStart(3, '0');
        const labelText = `VID ${String(video.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})`;
        
        if (isFailed) {
            card.className = 'frame-card video-failed-card';
            card.style.cursor = 'default';
            card.innerHTML = `
                <div class="video-failed-placeholder">
                    <span class="error-icon">⚠️</span>
                    <span class="error-text" title="${video.error || '生成失败'}">生成失败</span>
                    <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${video.slot}">重试</button>
                </div>
                <span>${labelText}</span>
            `;
            card.querySelector('.retry-video-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleVideo(video.slot);
            });
        } else {
            card.className = 'frame-card';
            card.style.cursor = 'pointer';
            card.innerHTML = `
                <div class="video-preview-wrapper" style="position: relative; width: 100%; aspect-ratio: 9/16; border-radius: 5px; overflow: hidden; background: #03050c;">
                    <video src="${video.url}" loop muted playsinline style="width:100%; height:100%; object-fit: cover; display: block;"></video>
                    <div class="video-play-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.25); transition: all 0.2s ease;">
                        <span class="play-icon" style="font-size: 2rem; color: #fff; opacity: 0.85; transition: all 0.2s ease;">▶</span>
                    </div>
                </div>
                <span>${labelText}</span>
            `;
            
            const videoEl = card.querySelector('video');
            const playOverlay = card.querySelector('.video-play-overlay');
            const playIcon = card.querySelector('.play-icon');
            
            card.addEventListener('mouseenter', () => {
                videoEl.play().catch(() => {});
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0)';
                if (playIcon) playIcon.style.opacity = '0';
            });
            card.addEventListener('mouseleave', () => {
                videoEl.pause();
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0.25)';
                if (playIcon) playIcon.style.opacity = '0.85';
            });
            
            card.addEventListener('click', () => {
                const validVideos = videos.filter(v => v.url || v.file);
                const mediaList = validVideos.map((v, idx) => {
                    const startImg = String(v.slot).padStart(3, '0');
                    const endImg = String(v.slot + 1).padStart(3, '0');
                    return {
                        type: 'video',
                        url: v.url || v.file,
                        caption: `<strong>VID ${String(v.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})</strong>`
                    };
                });
                const clickedIndex = validVideos.indexOf(video);
                openLightbox(mediaList, clickedIndex);
            });
        }
        grid.appendChild(card);
    });
}

async function generateVideos() {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }

    if (!currentIdea.frameRun || !currentIdea.frameRun.frames || currentIdea.frameRun.frames.length === 0) {
        showToast("请先生成帧序列图！", "error");
        return;
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
                title: currentIdea.title,
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
                title: currentIdea.title
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
function renderCoversForIdea(idea, activeIndex = 0) {
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const historyContainer = document.getElementById('cover-history-container');
    const thumbnailsEl = document.getElementById('cover-history-thumbnails');
    const hookDisplay = document.getElementById('cover-hook-display');
    const hookVal = document.getElementById('cover-hook-val');
    
    // Render English hook text if it exists
    if (hookDisplay && hookVal) {
        if (idea.english_title) {
            hookDisplay.style.display = 'flex';
            hookVal.textContent = idea.english_title;
        } else {
            hookDisplay.style.display = 'none';
            hookVal.textContent = '';
        }
    }
    
    const covers = idea.covers || [];
    
    if (covers.length === 0) {
        placeholderEl.style.display = 'flex';
        displayEl.style.display = 'none';
        historyContainer.style.display = 'none';
        return;
    }
    
    // Bound activeIndex
    if (activeIndex < 0 || activeIndex >= covers.length) {
        activeIndex = covers.length - 1;
    }
    
    // Set up displayEl load/error handlers before setting src
    displayEl.onload = () => {
        displayEl.style.display = 'block';
        placeholderEl.style.display = 'none';
    };
    displayEl.onerror = () => {
        displayEl.style.display = 'none';
        placeholderEl.style.display = 'flex';
    };
    
    // Update main image display
    safeSetImageSrc(displayEl, covers[activeIndex]);
    
    // Set up click on main image to open in lightbox on the current page
    displayEl.onclick = () => {
        const mediaList = covers.map((c, idx) => ({
            type: 'image',
            url: c,
            caption: `<strong>${idea.title} - 封面 ${idx + 1}/${covers.length}</strong>`
        }));
        openLightbox(mediaList, activeIndex);
    };
    
    // Update history thumbnails
    historyContainer.style.display = 'flex';
    thumbnailsEl.innerHTML = '';
    
    covers.forEach((coverUrl, idx) => {
        const thumb = document.createElement('div');
        thumb.className = `cover-thumb ${idx === activeIndex ? 'active' : ''}`;
        thumb.innerHTML = `<img src="" alt="Thumbnail ${idx + 1}" loading="lazy">`;
        
        const img = thumb.querySelector('img');
        img.onerror = () => {
            thumb.remove();
            // If all thumbnails are removed/hidden, hide the history container
            if (thumbnailsEl.children.length === 0) {
                historyContainer.style.display = 'none';
            }
        };
        
        safeSetImageSrc(img, coverUrl);
        
        thumb.addEventListener('click', () => {
            renderCoversForIdea(idea, idx);
        });
        
        thumbnailsEl.appendChild(thumb);
    });
}

// Robust extractor for Markdown images, raw URLs, or Base64 images from text content
function extractImageUrl(content) {
    if (!content) return null;
    
    // 1. Check if it's a markdown image: ![alt](url)
    const markdownRegex = /!\[.*?\]\((.*?)\)/;
    const match = content.match(markdownRegex);
    if (match && match[1]) {
        return match[1].trim();
    }
    
    // 2. Check if it's a raw URL starting with http, https, data:, or a local path starting with / or outputs/
    const urlRegex = /(https?:\/\/[^\s\)]+|data:image\/[^\s\)]+|\/outputs\/[^\s\)]+|outputs\/[^\s\)]+)/;
    const urlMatch = content.match(urlRegex);
    if (urlMatch && urlMatch[1]) {
        return urlMatch[1].trim();
    }
    
    // 3. Fallback: if it's just the content itself, trim and return if it starts with valid protocols or paths
    const trimmed = content.trim();
    if (trimmed.startsWith('http://') || 
        trimmed.startsWith('https://') || 
        trimmed.startsWith('data:image/') ||
        trimmed.startsWith('/') ||
        trimmed.startsWith('outputs/')) {
        return trimmed;
    }
    
    return null;
}

// =====================================================================
// Custom Presets Management
// =====================================================================
function loadCustomPresets() {
    const stored = localStorage.getItem('spark_custom_presets');
    if (stored) {
        try {
            customPresets = JSON.parse(stored);
        } catch (e) {
            console.error("Failed to parse custom presets", e);
            customPresets = {};
        }
    }
    renderCustomPresets();
}

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

function applyCustomPreset(name) {
    const p = customPresets[name];
    if (!p) return;
    
    // Theme
    document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => {
        if (btn.dataset.value === p.theme) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    // Anchors
    document.querySelectorAll('#anchor-selector .anchor-node').forEach(btn => {
        if (p.anchors.includes(btn.dataset.value)) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    // Sliders
    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el && val !== undefined) {
            el.value = val;
            el.dispatchEvent(new Event('input'));
        }
    };
    setVal('slider-complexity', p.complexity);
    setVal('slider-budget', p.budget);
    setVal('slider-ratio', p.ratio);
    setVal('slider-creativity', p.creativity);
    setVal('slider-beats', p.beats);
    
    saveSelectionState();
    showToast(`已应用自定义预设：${name}`, 'success');
}

function renderCustomPresets() {
    const container = document.getElementById('custom-presets-list');
    if (!container) return;
    
    container.innerHTML = '';
    const keys = Object.keys(customPresets);
    
    if (keys.length === 0) {
        container.style.display = 'none';
        return;
    }
    
    container.style.display = 'flex';
    keys.forEach(name => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'custom-preset-btn';
        btn.textContent = name;
        
        const deleteIcon = document.createElement('span');
        deleteIcon.className = 'delete-preset-icon';
        deleteIcon.innerHTML = '&times;';
        deleteIcon.title = '删除此预设';
        deleteIcon.addEventListener('click', (e) => deleteCustomPreset(name, e));
        
        btn.appendChild(deleteIcon);
        btn.addEventListener('click', () => applyCustomPreset(name));
        
        container.appendChild(btn);
    });
}

// =====================================================================
// Smart Randomizer
// =====================================================================
function randomizeDimensions() {
    // 1. Select a random theme
    const themeBtns = Array.from(document.querySelectorAll('#theme-selector .theme-btn'));
    if (themeBtns.length > 0) {
        themeBtns.forEach(btn => btn.classList.remove('active'));
        const randomThemeBtn = themeBtns[Math.floor(Math.random() * themeBtns.length)];
        randomThemeBtn.classList.add('active');
    }
    
    // 2. Select 1 to 3 random anchors
    const anchorNodes = Array.from(document.querySelectorAll('#anchor-selector .anchor-node'));
    if (anchorNodes.length > 0) {
        anchorNodes.forEach(node => node.classList.remove('active'));
        const numAnchors = Math.floor(Math.random() * 3) + 1;
        const shuffled = [...anchorNodes].sort(() => 0.5 - Math.random());
        for (let i = 0; i < Math.min(numAnchors, shuffled.length); i++) {
            shuffled[i].classList.add('active');
        }
    }
    
    // 3. Randomize sliders
    const setRandomVal = (id, min, max) => {
        const el = document.getElementById(id);
        if (el) {
            const randomVal = Math.floor(Math.random() * (max - min + 1)) + min;
            el.value = randomVal;
            el.dispatchEvent(new Event('input'));
        }
    };
    
    setRandomVal('slider-complexity', 1, 3);
    setRandomVal('slider-budget', 1, 3);
    setRandomVal('slider-ratio', 0, 100);
    setRandomVal('slider-creativity', 1, 3);
    setRandomVal('slider-beats', 5, 25);
    
    saveSelectionState();
    showToast("🎲 随机激发配比已装配！", "success");
}

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

async function loadIdeationCards() {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    
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
            renderIdeationCards(data.ideas);
        } else {
            container.innerHTML = `<div class="ideation-error">加载失败: ${data.message || '未知错误'}</div>`;
        }
    } catch (e) {
        console.error("Failed to load ideated cards:", e);
        container.innerHTML = `<div class="ideation-error">加载失败，请检查网络或配置</div>`;
    }
}

function renderIdeationCards(ideas) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    
    if (ideas.length === 0) {
        container.innerHTML = '<div class="ideation-loading">暂无灵感推荐</div>';
        return;
    }
    
    container.innerHTML = '';
    ideas.forEach((idea, idx) => {
        const card = document.createElement('div');
        card.className = 'ideation-card';
        card.dataset.index = idx;
        
        const familyClass = idea.carrier_family || 'natural';
        const familyText = {
            'natural': '🌲 自然',
            'man-made': '🏚️ 遗迹',
            'vehicle': '🚢 载具',
            'fantasy': '🔮 幻想'
        }[familyClass] || '自然';
        
        card.innerHTML = `
            <div class="ideation-card-header">
                <div class="ideation-card-title">${idea.title}</div>
                <div class="ideation-card-score">${idea.score}分</div>
            </div>
            <div class="ideation-card-metadata">
                <span class="ideation-card-tag ${familyClass}">${familyText}</span>
                <span class="ideation-card-tag">反差强度: ${idea.score >= 23 ? '极高' : '高'}</span>
            </div>
            <div class="ideation-card-body">
                <div>载体: ${idea.carrier} (${idea.env})</div>
                <div>现状: ${idea.trauma}</div>
                <div class="ideation-card-twist">招牌反差: ${idea.twist_zh || idea.twist}</div>
            </div>
            <div class="ideation-card-actions">
                <button type="button" class="ideation-card-btn select-action-btn">载入维度</button>
                <button type="button" class="ideation-card-btn primary compose-action-btn">一键合成</button>
            </div>
        `;
        
        // Clicking the card itself loads the dimensions
        card.addEventListener('click', (e) => {
            if (e.target.classList.contains('compose-action-btn')) return;
            selectIdeationCard(idx);
        });
        
        // Clicking "一键合成" directly starts compose
        const composeBtn = card.querySelector('.compose-action-btn');
        composeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            composeIdeationCard(idx);
        });
        
        container.appendChild(card);
    });
}

function selectIdeationCard(index) {
    const idea = currentIdeatedIdeas[index];
    if (!idea) return;
    
    // Highlight selected card
    document.querySelectorAll('.ideation-card').forEach((card, idx) => {
        if (idx === index) {
            card.classList.add('selected');
        } else {
            card.classList.remove('selected');
        }
    });
    
    // 1. Select matching carrier theme in GUI
    const themeVal = mapEnglishCarrierToValue(idea.carrier);
    const themeBtn = document.querySelector(`#theme-selector .theme-btn[data-value="${themeVal}"]`);
    if (themeBtn) {
        document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => btn.classList.remove('active'));
        themeBtn.classList.add('active');
    }
    
    // 2. Select matching anchors
    const mappedAnchorVal = mapTwistToAnchorValue(idea.dna);
    document.querySelectorAll('#anchor-selector .anchor-node').forEach(node => {
        if (node.dataset.value === mappedAnchorVal) {
            node.classList.add('active');
        } else {
            node.classList.remove('active');
        }
    });
    
    // 3. Populate slider values
    document.getElementById('slider-complexity').value = 3;
    document.getElementById('slider-budget').value = 2;
    document.getElementById('slider-ratio').value = 50;
    document.getElementById('slider-creativity').value = 3;
    
    // Trigger input events to update labels
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity'].forEach(id => {
        document.getElementById(id).dispatchEvent(new Event('input'));
    });
    
    showToast(`已载入灵感: ${idea.title}。可以在下方微调维度并点击生成！`, "success");
    saveSelectionState();
}

function composeIdeationCard(index) {
    const idea = currentIdeatedIdeas[index];
    if (!idea) return;
    
    const dimensions = {
        theme: idea.input_str,
        anchors: [idea.twist_zh || idea.twist],
        complexity: "硬核重工",
        budget: "轻奢设计师级",
        ratio: "50% (外壳粗野 ↔ 内里精致)",
        creativity: "脑洞大开",
        beats_count: 15
    };
    
    showToast(`🚀 开始一键合成灵感: ${idea.title}...`, "success");
    
    generateIdea({
        dimensions: dimensions,
        config: { ...config }
    });
}

function mapEnglishCarrierToValue(carrier) {
    const c = carrier.toLowerCase();
    if (c.includes('oak') || c.includes('tree')) return 'hollow_oak';
    if (c.includes('glacier') || c.includes('ice')) return 'glacier_cave';
    if (c.includes('submarine') || c.includes('sub')) return 'submarine_cabin';
    if (c.includes('tower')) return 'water_tower';
    if (c.includes('ship') || c.includes('wreck') || c.includes('trawler')) return 'shipwreck_hull';
    if (c.includes('missile') || c.includes('silo')) return 'missile_silo';
    if (c.includes('geode') || c.includes('amethyst')) return 'giant_geode';
    if (c.includes('sea cave') || c.includes('cave')) return 'sea_cave';
    return 'hollow_oak';
}

function mapTwistToAnchorValue(dna) {
    const d = dna.toLowerCase();
    if (d.includes('window') || d.includes('cutout')) return 'carrier_cutout_window';
    if (d.includes('floor') || d.includes('glass')) return 'water_glass_floor';
    if (d.includes('hatch') || d.includes('roof')) return 'bark_camouflaged_hatch';
    if (d.includes('stair') || d.includes('spiral')) return 'living_wood_stair';
    if (d.includes('moss') || d.includes('bioluminescent') || d.includes('light')) return 'bioluminescent_moss';
    if (d.includes('counter') || d.includes('slab')) return 'single_slab_counter';
    if (d.includes('shower') || d.includes('waterfall')) return 'rerouted_waterfall_shower';
    return 'carrier_cutout_window';
}

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

// --- LIGHTBOX SYSTEM ---
let lightboxItems = [];
let lightboxActiveIndex = -1;

function initLightbox() {
    const modal = document.getElementById('lightbox-modal');
    const closeBtn = document.getElementById('close-lightbox-btn');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');
    
    if (!modal) return;
    
    closeBtn?.addEventListener('click', closeLightbox);
    prevBtn?.addEventListener('click', () => navigateLightbox(-1));
    nextBtn?.addEventListener('click', () => navigateLightbox(1));
    
    // Close on clicking outside the content
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeLightbox();
        }
    });
}

function openLightbox(items, index) {
    lightboxItems = items;
    lightboxActiveIndex = index;
    
    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;
    
    modal.style.display = 'flex';
    updateLightboxContent();
    
    document.addEventListener('keydown', handleLightboxKeydown);
}

function closeLightbox() {
    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;
    modal.style.display = 'none';
    
    const video = document.getElementById('lightbox-video');
    if (video) {
        video.pause();
        video.src = '';
    }
    
    document.removeEventListener('keydown', handleLightboxKeydown);
}

function updateLightboxContent() {
    const img = document.getElementById('lightbox-img');
    const video = document.getElementById('lightbox-video');
    const caption = document.getElementById('lightbox-caption');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');
    
    if (!img || !video || !caption) return;
    
    if (lightboxActiveIndex < 0 || lightboxActiveIndex >= lightboxItems.length) {
        closeLightbox();
        return;
    }
    
    const item = lightboxItems[lightboxActiveIndex];
    
    if (item.type === 'video') {
        img.style.display = 'none';
        video.src = item.url;
        video.style.display = 'block';
        video.play().catch(err => console.log("Auto-play prevented", err));
    } else {
        video.style.display = 'none';
        video.pause();
        video.src = '';
        img.src = item.url;
        img.style.display = 'block';
    }
    
    if (item.caption) {
        caption.innerHTML = item.caption;
        caption.style.display = 'block';
    } else {
        caption.style.display = 'none';
    }
    
    if (lightboxItems.length <= 1) {
        if (prevBtn) prevBtn.style.display = 'none';
        if (nextBtn) nextBtn.style.display = 'none';
    } else {
        if (prevBtn) prevBtn.style.display = 'flex';
        if (nextBtn) nextBtn.style.display = 'flex';
    }
}

function navigateLightbox(direction) {
    if (lightboxItems.length <= 1) return;
    
    lightboxActiveIndex += direction;
    if (lightboxActiveIndex < 0) {
        lightboxActiveIndex = lightboxItems.length - 1;
    } else if (lightboxActiveIndex >= lightboxItems.length) {
        lightboxActiveIndex = 0;
    }
    
    updateLightboxContent();
}

function handleLightboxKeydown(e) {
    if (e.key === 'ArrowLeft') {
        navigateLightbox(-1);
    } else if (e.key === 'ArrowRight') {
        navigateLightbox(1);
    } else if (e.key === 'Escape') {
        closeLightbox();
    }
}

document.addEventListener('DOMContentLoaded', initLightbox);
