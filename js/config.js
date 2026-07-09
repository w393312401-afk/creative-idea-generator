// --- config.js ---

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

function applyPreset(presetName) {
    const p = PRESETS[presetName];
    if (!p) return;

    // Theme：先确认预设指向的主题按钮存在，再清空重选；
    // 否则一个失配的预设会把所有主题全部取消激活
    const targetThemeBtn = document.querySelector(`#theme-selector .theme-btn[data-value="${p.theme}"]`);
    if (targetThemeBtn) {
        document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => {
            btn.classList.toggle('active', btn === targetThemeBtn);
        });
    }
    
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
    const PRESET_LABELS = {
        nature_wonder: '自然奇观',
        industrial_relic: '工业遗迹',
        retired_vehicle: '退役载具',
        contrast_novelty: '反差猎奇'
    };
    showToast(`已应用预设：${PRESET_LABELS[presetName] || presetName}`, 'success');
}

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

function loadConfig() {
    const stored = localStorage.getItem('spark_config');
    if (stored) {
        try {
            config = { ...DEFAULT_CONFIG, ...JSON.parse(stored) };
            
            // Auto-migrate legacy port 65038 defaults to new port 8045 defaults.
            // 只按 baseUrl + 密钥前缀识别旧默认值——绝不能在前端源码里内联完整密钥
            if (config.baseUrl === 'http://localhost:65038/v1' && config.apiKey && config.apiKey.startsWith('agt_codex_')) {
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

    // Load frame sequence backend + Google FX image model options
    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        imageBackendSelect.value = config.imageBackend || 'api';
        imageBackendSelect.onchange = updateFxImageModelVisibility;
    }
    const fxImageModelSelect = document.getElementById('settings-fx-image-model');
    if (fxImageModelSelect) {
        fxImageModelSelect.value = config.googleFxImageModel || 'Nano Banana 2';
    }
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        fxVideoModelSelect.value = config.videoModel || 'Veo 3.1 - Lite [Lower Priority]';
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        fxIpRotateRequestsInput.value = config.googleFxIpRotateRequests !== undefined ? config.googleFxIpRotateRequests : 5;
    }
    updateFxImageModelVisibility();

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

    // 端口已永久固定（应用 8085 / 代理 8046，gpt-5.5 由服务端 resolve_gateway 固定路由），
    // 原「GPT 代理端口」选择器已移除，防止端口漂移。
    updateCoverModelDisplay();
}

function updateFxImageModelVisibility() {
    const backendSelect = document.getElementById('settings-image-backend');
    const fxImageGroup = document.getElementById('fx-image-model-group');
    const fxVideoGroup = document.getElementById('fx-video-model-group');
    const fxIpGroup = document.getElementById('fx-ip-rotate-requests-group');
    const showFx = backendSelect && backendSelect.value === 'google_fx';
    if (fxImageGroup) fxImageGroup.style.display = showFx ? 'block' : 'none';
    if (fxVideoGroup) fxVideoGroup.style.display = showFx ? 'block' : 'none';
    if (fxIpGroup) fxIpGroup.style.display = showFx ? 'block' : 'none';
}

function saveConfig() {
    config.baseUrl = document.getElementById('settings-base-url').value.trim();
    config.apiKey = document.getElementById('settings-api-key').value.trim();
    config.model = document.getElementById('settings-model').value.trim();
    config.imageModel = document.getElementById('settings-image-model').value.trim();
    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        config.imageBackend = imageBackendSelect.value;
    }
    const fxImageModelSelect = document.getElementById('settings-fx-image-model');
    if (fxImageModelSelect) {
        config.googleFxImageModel = fxImageModelSelect.value;
    }
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        config.videoModel = fxVideoModelSelect.value;
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        const val = parseInt(fxIpRotateRequestsInput.value.trim(), 10);
        config.googleFxIpRotateRequests = isNaN(val) ? 5 : val;
    }
    config.imageAspectRatio = document.getElementById('settings-image-ratio').value.trim();
    config.imageQuality = document.getElementById('settings-image-quality').value.trim();

    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    showToast("API 配置保存成功！", "success");
    checkApiStatus();
}

function resetConfig() {
    document.getElementById('settings-base-url').value = DEFAULT_CONFIG.baseUrl;
    document.getElementById('settings-api-key').value = DEFAULT_CONFIG.apiKey;
    document.getElementById('settings-model').value = DEFAULT_CONFIG.model;
    // Update the image model options list back to default options first
    updateImageModelOptions(false);
    document.getElementById('settings-image-model').value = DEFAULT_CONFIG.imageModel;
    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        imageBackendSelect.value = DEFAULT_CONFIG.imageBackend;
    }
    const fxImageModelSelect = document.getElementById('settings-fx-image-model');
    if (fxImageModelSelect) {
        fxImageModelSelect.value = DEFAULT_CONFIG.googleFxImageModel;
    }
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        fxVideoModelSelect.value = DEFAULT_CONFIG.videoModel;
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        fxIpRotateRequestsInput.value = DEFAULT_CONFIG.googleFxIpRotateRequests;
    }
    document.getElementById('settings-image-ratio').value = DEFAULT_CONFIG.imageAspectRatio;
    document.getElementById('settings-image-quality').value = DEFAULT_CONFIG.imageQuality;
    updateFxImageModelVisibility();
}

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

function saveCurrentIdeaState() {
    if (currentIdea) {
        localStorage.setItem('spark_current_idea', JSON.stringify(currentIdea));
    } else {
        localStorage.removeItem('spark_current_idea');
    }
}

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

