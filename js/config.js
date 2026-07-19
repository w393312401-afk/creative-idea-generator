// --- config.js ---

function saveSelectionState() {
    // 基础场景主题选择器已移除；state.theme 不再保存（旧档里残留的值被忽略）
    const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
        .map(node => node.dataset.value);

    const state = {
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

        // （主题选择器已移除，旧档 state.theme 直接忽略）

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
    // 选题来自「载入维度」过的灵感卡片（联网参考驱动），不再有基础场景主题
    const themeText = (typeof loadedIdeationCover !== 'undefined' && loadedIdeationCover && loadedIdeationCover.task_label)
        ? loadedIdeationCover.task_label : '未载入灵感卡';

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

// updateImageModelOptions 已删除（2026-07-12）：配置中心的 LLM/生图模型下拉整体移除，
// 模型选择完全由激发页脚 #ideation-llm-model 与帧序列卡片 #frames-image-model 承担。

/* ── 激发维度内嵌 LLM 模型选择器 ─────────────────────────────────────
   LLM 主模型（激发/合成/审核/质检判定共用 config.model）的唯一 UI 入口：
   在激发按钮上方直接切换，改动即写 localStorage，下次激发/合成生效。
   （配置中心的主模型下拉已于 2026-07-12 移除。）
   2026-07-13 改为分组芯片单选器：GPT/Gemini/Claude 每个供应商一行，模型平铺成
   胶囊按钮，全局有且只有一个 .selected（此前的三个并排 <select> 方案光看选中项
   容易误以为三个都在生效——用户当日反馈"用哪个模型没有唯一性"）。点任意芯片即
   切换 config.model；label 旁的徽标（#ideation-llm-model-active-hint）另行点名
   当前生效模型作双保险。整个分组区每次 sync 全量重建，事件直接绑在新节点上。 */
const LLM_MODEL_PICKER_FAMILIES = [
    { key: 'gpt', label: 'GPT' },
    { key: 'gemini', label: 'Gemini' },
    { key: 'claude', label: 'Claude' },
];

function syncIdeationLlmPicker() {
    const wrap = document.getElementById('ideation-llm-groups');
    if (!wrap) return;
    const current = config.model || 'gemini-3-flash-agent';
    const isKnown = LLM_MODEL_PICKER_FAMILIES.some(
        fam => LLM_MODEL_GROUPS[fam.key].some(m => m.value === current)
    );

    wrap.innerHTML = '';
    LLM_MODEL_PICKER_FAMILIES.forEach(fam => {
        const models = LLM_MODEL_GROUPS[fam.key].slice();
        // 当前生效模型不属于任何一组（比如手动改过 localStorage 的自定义模型名）：
        // 追加展示在 GPT 组末尾，避免用户看不到当前实际生效的值。
        if (!isKnown && fam.key === 'gpt') {
            models.push({ value: current, label: `${current}（自定义）` });
        }

        const group = document.createElement('div');
        group.className = 'model-family-group'
            + (models.some(m => m.value === current) ? ' active' : '');
        group.dataset.family = fam.key;

        const tag = document.createElement('span');
        tag.className = 'model-family-tag';
        tag.textContent = fam.label;
        group.appendChild(tag);

        const row = document.createElement('div');
        row.className = 'model-chip-row';
        models.forEach(m => {
            const isSelected = m.value === current;
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'model-chip' + (isSelected ? ' selected' : '');
            chip.title = isSelected
                ? `${m.value}（当前生效，激发/提示词合成/审核/质检判定共用）`
                : `点击切换为 ${m.value}`;

            const name = document.createElement('span');
            name.className = 'model-chip-name';
            name.textContent = m.label;
            chip.appendChild(name);
            if (m.recommended) {
                const badge = document.createElement('span');
                badge.className = 'model-chip-badge';
                badge.textContent = '推荐';
                chip.appendChild(badge);
            }

            chip.addEventListener('click', () => {
                if (config.model === m.value) return;
                config.model = m.value;
                localStorage.setItem('spark_config', JSON.stringify(config));
                showToast(`LLM 模型已切换：${m.value}（下次激发/合成生效）`, 'success');
                syncIdeationLlmPicker();
                // 顶栏「本地 xxx 在线」徽章即时复测：不同供应商路由到不同网关
                // （gpt→codex / 其余→8046），不复测的话徽章会一直挂着旧模型名
                // 和旧网关的在线状态。ping 只查网关可达（毫秒级、零 token），
                // 不发真实补全验模型——8046 间歇抽风，单点真调用探测误报率高，
                // 激发链路自有重试兜底。
                checkApiStatus();
            });
            row.appendChild(chip);
        });
        group.appendChild(row);
        wrap.appendChild(group);
    });

    const hint = document.getElementById('ideation-llm-model-active-hint');
    if (hint) hint.textContent = `使用中 · ${current}`;
}

/* ── 帧序列模块内嵌生图模型选择器 ────────────────────────────────────
   生图模型的唯一 UI 入口（配置中心的生图模型下拉已于 2026-07-12 移除）：
   在「连续帧序列生成」卡片里直接切换。选项集随帧后端切换
   （api → IMAGE_MODELS 绑 config.imageModel；google_fx → FX_IMAGE_MODELS
   绑 config.googleFxImageModel，后者仍与配置中心的 FX 生图下拉同步），
   改动即写 localStorage，下次生成/单帧重试生效。 */
function syncFramesImageModelPicker() {
    const sel = document.getElementById('frames-image-model');
    if (!sel) return;

    const isFx = (config.imageBackend || 'api') === 'google_fx';
    const options = isFx ? FX_IMAGE_MODELS : IMAGE_MODELS;
    const current = isFx
        ? (config.googleFxImageModel || 'Nano Banana 2')
        : (config.imageModel || 'nano-banana-2');

    sel.innerHTML = '';
    options.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.value;
        opt.textContent = m.label;
        sel.appendChild(opt);
    });
    if (!options.some(m => m.value === current)) {
        const opt = document.createElement('option');
        opt.value = current;
        opt.textContent = `${current} (自定义)`;
        sel.appendChild(opt);
    }
    sel.value = current;
    sel.title = isFx
        ? 'Google FX（AdsPower 浏览器）生图模型；改动即存，下次生成/单帧重试生效'
        : 'API 后端生图模型（封面图共用）；改动即存，下次生成/单帧重试生效';

    if (!sel.dataset.bound) {
        sel.dataset.bound = '1';
        sel.addEventListener('change', () => {
            const fx = (config.imageBackend || 'api') === 'google_fx';
            if (fx) {
                config.googleFxImageModel = sel.value;
                const fxSel = document.getElementById('settings-fx-image-model');
                if (fxSel) fxSel.value = sel.value;
            } else {
                config.imageModel = sel.value;
            }
            localStorage.setItem('spark_config', JSON.stringify(config));
            updateCoverModelDisplay();
            showToast(`帧序列生图模型已切换：${sel.value}（下次生成/单帧重试生效）`, 'success');
        });
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
    // （Base URL / API Key 输入框已移除：托管模式下 effective_config 不透传
    //   浏览器端的这两项，密钥与网关地址由 server_config.json 统一管理；
    //   config.baseUrl / config.apiKey 仍保留在 localStorage 供非托管兜底。
    //   LLM 模型 / 生图模型下拉也已移除：改由激发页脚 #ideation-llm-model 与
    //   帧序列卡片 #frames-image-model 内嵌选择器管理（见文件尾部 sync 函数）。）

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
        fxVideoModelSelect.onchange = updateFxVideoDurationVisibility;
    }
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        fxVideoDurationSelect.value = config.videoDuration || '';
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        fxIpRotateRequestsInput.value = config.googleFxIpRotateRequests !== undefined ? config.googleFxIpRotateRequests : 5;
    }
    const fxBrowserIdInput = document.getElementById('settings-fx-browser-id');
    if (fxBrowserIdInput) {
        fxBrowserIdInput.value = config.googleFxUserId || '';
    }
    const supervisedSelect = document.getElementById('settings-supervised-mode');
    if (supervisedSelect) {
        supervisedSelect.value = config.supervisedMode ? 'on' : 'off';
    }
    const trendUrlsInput = document.getElementById('settings-ideation-trend-urls');
    if (trendUrlsInput) {
        trendUrlsInput.value = config.ideationTrendUrls || '';
    }
    const searchQueryInput = document.getElementById('settings-ideation-search-query');
    if (searchQueryInput) {
        searchQueryInput.value = config.ideationSearchQuery || '';
    }
    updateFxImageModelVisibility();
    updateFxVideoDurationVisibility();

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
    syncFramesImageModelPicker();
    syncIdeationLlmPicker();
}

function updateFxImageModelVisibility() {
    const backendSelect = document.getElementById('settings-image-backend');
    const fxImageGroup = document.getElementById('fx-image-model-group');
    const fxVideoGroup = document.getElementById('fx-video-model-group');
    const fxIpGroup = document.getElementById('fx-ip-rotate-requests-group');
    const fxBrowserIdGroup = document.getElementById('fx-browser-id-group');
    const showFx = backendSelect && backendSelect.value === 'google_fx';
    if (fxImageGroup) fxImageGroup.style.display = showFx ? 'block' : 'none';
    if (fxVideoGroup) fxVideoGroup.style.display = showFx ? 'block' : 'none';
    if (fxIpGroup) fxIpGroup.style.display = showFx ? 'block' : 'none';
    if (fxBrowserIdGroup) fxBrowserIdGroup.style.display = showFx ? 'block' : 'none';
    updateFxVideoDurationVisibility();
}

// Omni Flash 时长切换仅该模型面板提供（Veo 系列时长固定）：需同时满足
// 「FX 后端已开启」与「当前选中的视频模型是 Omni Flash」两个条件才显示。
function updateFxVideoDurationVisibility() {
    const backendSelect = document.getElementById('settings-image-backend');
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    const durationGroup = document.getElementById('fx-video-duration-group');
    if (!durationGroup) return;
    const showFx = backendSelect && backendSelect.value === 'google_fx';
    const isOmni = fxVideoModelSelect && fxVideoModelSelect.value === 'Omni Flash';
    durationGroup.style.display = (showFx && isOmni) ? 'block' : 'none';
}

function saveConfig() {
    // config.model / config.imageModel 不再从本弹窗读取：由激发页脚与帧序列卡片的
    // 内嵌选择器直接维护（改动即存），这里只负责其余生成参数并整体持久化
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
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        config.videoDuration = fxVideoDurationSelect.value;
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        const val = parseInt(fxIpRotateRequestsInput.value.trim(), 10);
        config.googleFxIpRotateRequests = isNaN(val) ? 5 : val;
    }
    const fxBrowserIdInput = document.getElementById('settings-fx-browser-id');
    if (fxBrowserIdInput) {
        config.googleFxUserId = fxBrowserIdInput.value.trim();
    }
    const supervisedSelect = document.getElementById('settings-supervised-mode');
    if (supervisedSelect) {
        config.supervisedMode = supervisedSelect.value === 'on';
    }
    const trendUrlsInput = document.getElementById('settings-ideation-trend-urls');
    if (trendUrlsInput) {
        config.ideationTrendUrls = trendUrlsInput.value.trim();
    }
    const searchQueryInput = document.getElementById('settings-ideation-search-query');
    if (searchQueryInput) {
        config.ideationSearchQuery = searchQueryInput.value.trim();
    }
    config.imageAspectRatio = document.getElementById('settings-image-ratio').value.trim();
    config.imageQuality = document.getElementById('settings-image-quality').value.trim();

    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    syncIdeationLlmPicker();
    showToast("API 配置保存成功！", "success");
    checkApiStatus();
}

function resetConfig() {
    // 模型项已无配置中心表单：直接重置 config 对象，尾部的 sync 会刷新两个内嵌选择器
    //（与其余表单项一致，点「保存配置」后才整体持久化到 localStorage）
    config.model = DEFAULT_CONFIG.model;
    config.imageModel = DEFAULT_CONFIG.imageModel;
    config.googleFxImageModel = DEFAULT_CONFIG.googleFxImageModel;
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
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        fxVideoDurationSelect.value = DEFAULT_CONFIG.videoDuration;
    }
    const fxIpRotateRequestsInput = document.getElementById('settings-fx-ip-rotate-requests');
    if (fxIpRotateRequestsInput) {
        fxIpRotateRequestsInput.value = DEFAULT_CONFIG.googleFxIpRotateRequests;
    }
    const fxBrowserIdInput = document.getElementById('settings-fx-browser-id');
    if (fxBrowserIdInput) {
        fxBrowserIdInput.value = DEFAULT_CONFIG.googleFxUserId;
    }
    const supervisedSelect = document.getElementById('settings-supervised-mode');
    if (supervisedSelect) {
        supervisedSelect.value = DEFAULT_CONFIG.supervisedMode ? 'on' : 'off';
    }
    const trendUrlsInput = document.getElementById('settings-ideation-trend-urls');
    if (trendUrlsInput) {
        trendUrlsInput.value = DEFAULT_CONFIG.ideationTrendUrls;
    }
    const searchQueryInput = document.getElementById('settings-ideation-search-query');
    if (searchQueryInput) {
        searchQueryInput.value = DEFAULT_CONFIG.ideationSearchQuery;
    }
    document.getElementById('settings-image-ratio').value = DEFAULT_CONFIG.imageAspectRatio;
    document.getElementById('settings-image-quality').value = DEFAULT_CONFIG.imageQuality;
    updateFxImageModelVisibility();
    updateFxVideoDurationVisibility();
    syncFramesImageModelPicker();
    syncIdeationLlmPicker();
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
    setRandomVal('slider-beats', 5, 15);
    
    saveSelectionState();
    showToast("🎲 随机激发配比已装配！", "success");
}

