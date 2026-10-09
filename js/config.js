// --- config.js ---

/* =====================================================================
   配置中心与模型设置模块 (Config & Models)
   ---------------------------------------------------------------------
   管理 LLM 模型、生图模型、视频模型、提示词链路及系统运行参数的
   读取、持久化 (localStorage) 与 UI 同步。
   ===================================================================== */

const SKILL_PROFILE_CHOICES = [
    { value: 'auto', label: '自动', hint: '跟随视频模型' },
    { value: 'base', label: 'Veo · 单镜延时', hint: 'gemini-veo-restoration-composer' },
    { value: 'omni', label: 'Omni · 多镜头', hint: 'gemini-omni-restoration-composer' },
    { value: 'miniature', label: 'Miniature · 微缩多镜头', hint: 'gemini-miniature-restoration-composer' },
];

// /api/mode 下发的链路信息（app.js initServerMode 填）。规则表形如
// [['omni','omni']]：视频模型名（小写）含 needle 即用该 profile，都不命中回 default。
window.SKILL_PROFILE_RULES = window.SKILL_PROFILE_RULES || null;
window.SKILL_PROFILE_DEFAULT = window.SKILL_PROFILE_DEFAULT || 'base';

// /api/mode supplies only these public fields. Gateway addresses and credentials
// stay on the server and are never persisted in browser storage.
let flow2apiConfigured = null;
const VIDEO_SERVICE_PUBLIC_KEYS = ['videoProvider', 'videoModel', 'videoDuration', 'videoResolution', 'videoRefMode', 'flow2apiVideoConcurrency', 'videoRetryCount', 'videoContinuousGeneration'];

function applyServerVideoConfig(report) {
    if (!report || typeof report !== 'object') return;
    for (const key of VIDEO_SERVICE_PUBLIC_KEYS) {
        if (key === 'flow2apiVideoConcurrency') {
            const count = Number(report[key]);
            if (Number.isInteger(count) && count >= 1 && count <= 10) config[key] = count;
        } else if (key === 'videoRetryCount') {
            const count = report[key] === '' || report[key] == null ? NaN : Number(report[key]);
            if (Number.isInteger(count) && count >= 0 && count <= 10) config[key] = count;
        } else if (key === 'videoContinuousGeneration') {
            if (typeof report[key] === 'boolean') config[key] = report[key];
        } else if (typeof report[key] === 'string' && report[key]) config[key] = report[key];
    }
    if (typeof report.flow2apiConfigured === 'boolean') flow2apiConfigured = report.flow2apiConfigured;
    // Remove historical/custom connection values rather than forwarding them.
    delete config.flow2apiBaseUrl;
    delete config.flow2apiApiKey;
    delete config.flow2apiVideoTimeoutSeconds;
    const fields = {
        videoProvider: 'settings-video-provider', videoModel: 'settings-fx-video-model',
        videoDuration: 'settings-fx-video-duration', videoResolution: 'settings-fx-video-resolution',
        videoRefMode: 'settings-fx-video-ref-mode',
        flow2apiVideoConcurrency: 'settings-flow2api-video-concurrency',
        videoRetryCount: 'settings-video-retry-count',
        videoContinuousGeneration: 'settings-video-continuous-generation',
    };
    for (const [key, id] of Object.entries(fields)) {
        const select = document.getElementById(id);
        if (select) select.value = String(config[key] ?? DEFAULT_CONFIG[key]);
    }
    try { localStorage.setItem('spark_config', JSON.stringify(config)); } catch (_) {}
    updateFxVideoDurationVisibility();
    syncIdeationSkillProfilePicker();
}

function updateVideoProviderStatus() {
    const provider = document.getElementById('settings-video-provider');
    const model = document.getElementById('settings-fx-video-model');
    const reference = document.getElementById('settings-fx-video-ref-mode');
    const ratio = document.getElementById('settings-image-ratio');
    const status = document.getElementById('video-provider-status');
    const isFlow = (provider ? provider.value : config.videoProvider) === 'flow2api';
    const concurrencyGroup = document.getElementById('flow2api-video-concurrency-group');
    if (concurrencyGroup) concurrencyGroup.style.display = isFlow ? '' : 'none';
    if (model) {
        for (const option of model.options || []) {
            option.disabled = isFlow && option.value !== 'Omni Flash';
        }
    }
    if (reference) {
        for (const option of reference.options || []) {
            option.disabled = isFlow && option.value !== 'VIDEO_FRAMES';
        }
    }
    if (!status) return;
    const selectedModel = model ? model.value : config.videoModel;
    const incompatible = isFlow && selectedModel !== 'Omni Flash';
    const unsupportedReference = isFlow && (reference ? reference.value : config.videoRefMode) !== 'VIDEO_FRAMES';
    const selectedRatio = ratio ? ratio.value : config.imageAspectRatio;
    const unsupportedRatio = isFlow && !['16:9', '9:16'].includes(selectedRatio);
    status.dataset.state = incompatible || unsupportedReference || unsupportedRatio || (isFlow && flow2apiConfigured === false) ? 'error' : 'info';
    if (!isFlow) {
        status.textContent = '通过 AdsPower 中的 Flow 账号生成视频。';
    } else if (incompatible) {
        status.textContent = '当前模型不受 Flow2API 支持，请选择 Omni Flash；服务不会自动替换模型。';
    } else if (unsupportedReference) {
        status.textContent = 'Flow2API 当前仅支持「帧（首尾帧）」参考模式；请在高级设置中修改，服务不会将素材模式改为首尾帧提交。';
    } else if (unsupportedRatio) {
        status.textContent = 'Flow2API 视频当前仅支持 16:9 或 9:16；当前画幅已保留，请选择支持的画幅后生成。';
    } else if (flow2apiConfigured === false) {
        status.textContent = 'Flow2API 连接尚未配置，请在服务端配置 API Key。';
    } else {
        const connection = flow2apiConfigured ? '服务端连接已配置。' : '连接配置由服务端管理。';
        status.textContent = `Flow2API：${connection}使用下方时长、分辨率、参考模式和当前画幅；不支持的组合会明确报错。`;
    }
}

/** auto 模式下当前实际会走哪条链路；规则表还没到手时返回 null（不猜）。 */
function resolveAutoSkillProfile() {
    const rules = window.SKILL_PROFILE_RULES;
    if (!Array.isArray(rules) || !rules.length) return null;
    const haystack = String(config.videoModel || '').toLowerCase();
    for (const rule of rules) {
        const needle = String((rule && rule[0]) || '').toLowerCase();
        if (needle && haystack.includes(needle)) return String(rule[1] || '');
    }
    return window.SKILL_PROFILE_DEFAULT || 'base';
}

function syncSettingsSkillProfilePicker() {
    const select = document.getElementById('settings-skill-profile');
    if (!select) return;
    select.value = config.skillProfile || DEFAULT_CONFIG.skillProfile || 'auto';
}

const LLM_MODEL_PICKER_FAMILIES = [
    { key: 'gpt', label: 'GPT' },
    { key: 'claude', label: 'Claude' },
    { key: 'gemini', label: 'Gemini' },
];

// 与服务端 resolve_chat_model 一致：已下架的文本模型不会再以自定义/历史选项回流。
// 生图模型与用户自定义模型保持原值。
function normalizeLlmModel(value) {
    const model = String(value || '').trim();
    const lower = model.toLowerCase();
    if (lower.includes('image')) return model;
    // Claude 型号原样放行（走服务端 claudeBaseUrl 网关）；只有已下架的旧 GPT 迁移到当前型号。
    if (/^gpt-(?:3\.5|4o?|4\.\d+|5(?:\.\d+)?)(?:-|$)/.test(lower)) {
        return 'gpt-6.1-sol';
    }
    if (/^gemini-3-flash(?:-agent)?$/.test(lower)
            || /^gemini-3\.1-pro(?:-[a-z-]+)?$/.test(lower)
            || /^gemini-3\.[567]-flash(?:-[a-z-]+)?$/.test(lower)) {
        return 'gemini-3.8-flash-high';
    }
    const known = Object.values(LLM_MODEL_GROUPS).flat().find(item => item.value.toLowerCase() === lower);
    return known ? known.value : model;
}

function migrateRetiredLlmModels(settings) {
    let changed = false;
    for (const key of ['model', 'cheapModel', 'auxModel', 'reviewModel']) {
        if (!settings[key]) continue;
        const current = normalizeLlmModel(settings[key]);
        if (current !== settings[key]) {
            settings[key] = current;
            changed = true;
        }
    }
    return changed;
}

function syncSettingsLlmModelPicker() {
    const select = document.getElementById('settings-llm-model');
    if (!select) return;
    const current = normalizeLlmModel(config.model) || DEFAULT_CONFIG.model;
    config.model = current;
    select.innerHTML = '';

    LLM_MODEL_PICKER_FAMILIES.forEach(fam => {
        const optgroup = document.createElement('optgroup');
        optgroup.label = fam.label;
        const models = LLM_MODEL_GROUPS[fam.key] || [];
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.value;
            opt.textContent = m.label + (m.recommended ? '（推荐）' : '');
            optgroup.appendChild(opt);
        });
        select.appendChild(optgroup);
    });

    const isKnown = LLM_MODEL_PICKER_FAMILIES.some(
        fam => (LLM_MODEL_GROUPS[fam.key] || []).some(m => m.value === current)
    );
    if (!isKnown && current) {
        const customOpt = document.createElement('option');
        customOpt.value = current;
        customOpt.textContent = `${current}（自定义）`;
        select.appendChild(customOpt);
    }
    select.value = current;
}

// 向后兼容别名
function syncIdeationSkillProfilePicker() {
    syncSettingsSkillProfilePicker();
}

function syncIdeationLlmPicker() {
    syncSettingsLlmModelPicker();
}

function syncFramesImageModelPicker() {
    const sel = document.getElementById('frames-image-model');
    if (!sel) return;

    const isFx = (config.imageBackend || 'api') === 'google_fx';
    const options = isFx ? FX_IMAGE_MODELS : IMAGE_MODELS;
    const current = isFx
        ? normalizeGoogleFxImageModel(config.googleFxImageModel)
        : (normalizeApiImageModel(config.imageModel) || DEFAULT_CONFIG.imageModel);
    if (isFx) config.googleFxImageModel = current;
    else config.imageModel = current;

    sel.innerHTML = '';
    options.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.value;
        opt.textContent = m.label;
        if (!isFx) applyApiImageModelOptionAvailability(opt);
        sel.appendChild(opt);
    });
    if (!options.some(m => m.value === current)) {
        const opt = document.createElement('option');
        opt.value = current;
        opt.textContent = `${current} (自定义)`;
        if (!isFx) applyApiImageModelOptionAvailability(opt);
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
                config.googleFxImageModel = normalizeGoogleFxImageModel(sel.value);
                const fxSel = document.getElementById('settings-fx-image-model');
                if (fxSel) fxSel.value = config.googleFxImageModel;
            } else {
                config.imageModel = normalizeApiImageModel(sel.value);
                const apiSel = document.getElementById('settings-api-image-model');
                if (apiSel) apiSel.value = config.imageModel;
            }
            try {
                localStorage.setItem('spark_config', JSON.stringify(config));
            } catch (e) {
                console.warn('Failed to save config to localStorage:', e);
            }
            updateCoverModelDisplay();
            showToast(`帧序列生图模型已切换：${sel.value}（下次生成/单帧重试生效）`, 'success');
        });
    }
}

/* 配置中心「生成后端」里的 API 生图模型下拉：与上面的帧序列内嵌选择器是同一项
   设置（config.imageModel），两边互相回写。选项按 IMAGE_MODELS 动态渲染而不是写死在
   index.html 里——清单加一条模型时只改 js/state.js 一处，两个入口一起跟上。 */
function syncSettingsApiImageModelPicker() {
    const sel = document.getElementById('settings-api-image-model');
    if (!sel) return;
    const current = normalizeApiImageModel(config.imageModel) || DEFAULT_CONFIG.imageModel;
    config.imageModel = current;
    sel.innerHTML = '';
    IMAGE_MODELS.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.value;
        opt.textContent = m.label;
        applyApiImageModelOptionAvailability(opt);
        sel.appendChild(opt);
    });
    if (!IMAGE_MODELS.some(m => m.value === current)) {
        const opt = document.createElement('option');
        opt.value = current;
        opt.textContent = `${current} (自定义)`;
        applyApiImageModelOptionAvailability(opt);
        sel.appendChild(opt);
    }
    sel.value = current;
}

const _warnedUnavailableImageGatewayModels = new Set();

function syncImageGatewayModelAvailability(report) {
    setImageGatewayModelCatalog(report);
    const current = normalizeApiImageModel(config.imageModel);
    if (!isApiImageModelAvailable(current) && !_warnedUnavailableImageGatewayModels.has(current)) {
        _warnedUnavailableImageGatewayModels.add(current);
        showToast(`${unavailableApiImageModelMessage(current)} 已保留当前选择（封面与帧序列）。`, 'warning');
    }
    syncFramesImageModelPicker();
    syncSettingsApiImageModelPicker();
    updateCoverModelDisplay();
    if (typeof syncImageStudioModelAvailability === 'function') syncImageStudioModelAvailability();
}

function loadConfig() {
    const stored = localStorage.getItem('spark_config');
    if (stored) {
        try {
            config = { ...DEFAULT_CONFIG, ...JSON.parse(stored) };

            // 旧版 API 配置中心曾把“临时指定浏览器”和“换号频率”存在本地。
            // 两项现已统一由 Google FX 服务管理中心维护；删掉旧值，防止它们继续
            // 随每个请求覆盖服务端设置。
            if (Object.prototype.hasOwnProperty.call(config, 'googleFxUserId')
                    || Object.prototype.hasOwnProperty.call(config, 'googleFxIpRotateRequests')) {
                delete config.googleFxUserId;
                delete config.googleFxIpRotateRequests;
                localStorage.setItem('spark_config', JSON.stringify(config));
            }
            
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

            // Flow 已下线旧 Imagen/Image 4；历史 localStorage 自动迁移到新 Lite。
            const normalizedFxModel = normalizeGoogleFxImageModel(config.googleFxImageModel);
            if (normalizedFxModel !== config.googleFxImageModel) {
                config.googleFxImageModel = normalizedFxModel;
                localStorage.setItem('spark_config', JSON.stringify(config));
            }

            const normalizedImageModel = normalizeApiImageModel(config.imageModel);
            if (normalizedImageModel !== config.imageModel) {
                config.imageModel = normalizedImageModel;
                localStorage.setItem('spark_config', JSON.stringify(config));
            }

            // 同时迁移辅助模型，防止旧配置继续调用已从列表移除的文本模型。
            if (migrateRetiredLlmModels(config)) {
                localStorage.setItem('spark_config', JSON.stringify(config));
            }
        } catch (e) {
            console.error("Failed to parse stored config, using defaults", e);
        }
    }
    
    // 审查规则永久退役：旧 localStorage 也不能覆盖退役状态。
    if (typeof normalizeRetiredGateConfig === 'function' && normalizeRetiredGateConfig(config)) {
        try { localStorage.setItem('spark_config', JSON.stringify(config)); }
        catch (error) { console.error('Failed to migrate retired review settings', error); }
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
        fxImageModelSelect.value = normalizeGoogleFxImageModel(config.googleFxImageModel);
    }
    const videoProviderSelect = document.getElementById('settings-video-provider');
    if (videoProviderSelect) {
        videoProviderSelect.value = config.videoProvider || DEFAULT_CONFIG.videoProvider;
        videoProviderSelect.onchange = updateVideoProviderStatus;
    }
    const flow2apiConcurrencySelect = document.getElementById('settings-flow2api-video-concurrency');
    if (flow2apiConcurrencySelect) {
        flow2apiConcurrencySelect.value = String(config.flow2apiVideoConcurrency || DEFAULT_CONFIG.flow2apiVideoConcurrency);
    }
    const videoRetrySelect = document.getElementById('settings-video-retry-count');
    if (videoRetrySelect) videoRetrySelect.value = String(config.videoRetryCount ?? DEFAULT_CONFIG.videoRetryCount ?? 5);
    const continuousVideoSelect = document.getElementById('settings-video-continuous-generation');
    if (continuousVideoSelect) continuousVideoSelect.value = String(config.videoContinuousGeneration ?? true);
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        fxVideoModelSelect.value = config.videoModel || 'Veo 3.1 - Lite [Lower Priority]';
        fxVideoModelSelect.onchange = updateFxVideoDurationVisibility;
    }
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        // 老配置里可能存着空串（"沿用 Flow 面板当前时长"，2026-08-01 已取消）——空值在
        // 下拉框里已经没有对应项，会显示成一个空白选项，回落到默认时长。
        fxVideoDurationSelect.value = config.videoDuration || DEFAULT_CONFIG.videoDuration;
        fxVideoDurationSelect.onchange = updateOmniCreditEstimate;
    }
    const fxVideoResolutionSelect = document.getElementById('settings-fx-video-resolution');
    if (fxVideoResolutionSelect) {
        fxVideoResolutionSelect.value = config.videoResolution || DEFAULT_CONFIG.videoResolution;
        fxVideoResolutionSelect.onchange = updateOmniCreditEstimate;
    }
    const fxVideoRefModeSelect = document.getElementById('settings-fx-video-ref-mode');
    if (fxVideoRefModeSelect) {
        fxVideoRefModeSelect.value = config.videoRefMode || DEFAULT_CONFIG.videoRefMode;
        fxVideoRefModeSelect.onchange = updateVideoProviderStatus;
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
        imageRatioSelect.onchange = updateVideoProviderStatus;
    }

    // Load quality/clarity option
    const imageQualitySelect = document.getElementById('settings-image-quality');
    if (imageQualitySelect) {
        imageQualitySelect.value = config.imageQuality || '2K';
    }

    const candidateConcurrencySelect = document.getElementById('settings-candidate-concurrency');
    if (candidateConcurrencySelect) {
        candidateConcurrencySelect.value = String(config.candidateConcurrency || DEFAULT_CONFIG.candidateConcurrency || 4);
    }

    // 任务提醒设置
    const soundNotifCb = document.getElementById('settings-sound-notification');
    if (soundNotifCb) {
        soundNotifCb.checked = config.soundNotificationEnabled !== false;
    }
    const notifVolInput = document.getElementById('settings-notification-volume');
    const notifVolVal = document.getElementById('settings-notification-volume-val');
    if (notifVolInput) {
        const vol = typeof config.notificationVolume === 'number' ? config.notificationVolume : 80;
        notifVolInput.value = vol;
        if (notifVolVal) notifVolVal.textContent = `${vol}%`;
    }
    const taskbarFlashCb = document.getElementById('settings-taskbar-flash');
    if (taskbarFlashCb) {
        taskbarFlashCb.checked = config.taskbarFlashEnabled !== false;
    }
    const desktopNotifCb = document.getElementById('settings-desktop-notification');
    if (desktopNotifCb) {
        desktopNotifCb.checked = config.desktopNotificationEnabled !== false;
    }
    updateDesktopPermissionStatus();

    // 端口已永久固定（应用 8085 / 代理 8046，GPT 由服务端 resolve_gateway 路由），
    // 原「GPT 代理端口」选择器已移除，防止端口漂移。
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    syncSettingsApiImageModelPicker();
    syncSettingsLlmModelPicker();
    syncSettingsSkillProfilePicker();
    updateVideoProviderStatus();
}

// 显隐一律用空串还原（而不是写死 'block'）：配置中心的字段行是 CSS grid
// （.settings-field），内联 display:block 会把 label/控件/说明拍回竖排。
// 「帧序列生成方式」只决定生图走哪条路，所以只切生图模型那两行：api → API 生图模型
// （IMAGE_MODELS，含 gpt-image-2.5），google_fx → FX 生图模型。视频服务独立选择。
function updateFxImageModelVisibility() {
    const backendSelect = document.getElementById('settings-image-backend');
    const apiImageGroup = document.getElementById('api-image-model-group');
    const candConcurrencyGroup = document.getElementById('api-candidate-concurrency-group');
    const fxImageGroup = document.getElementById('fx-image-model-group');
    const showFx = backendSelect && backendSelect.value === 'google_fx';
    if (apiImageGroup) apiImageGroup.style.display = showFx ? 'none' : '';
    if (candConcurrencyGroup) candConcurrencyGroup.style.display = showFx ? 'none' : '';
    if (fxImageGroup) fxImageGroup.style.display = showFx ? '' : 'none';
    updateFxVideoDurationVisibility();
}

// Omni Flash 积分消耗对照表（根据 Google Labs Flow 实际扣费）
const OMNI_CREDIT_MATRIX = {
    '720p': { '4': 6, '6': 9, '8': 12, '10': 15 },
    '360p': { '4': 3, '6': 5, '8': 6, '10': 8 },
};

function updateOmniCreditEstimate() {
    const durSelect = document.getElementById('settings-fx-video-duration');
    const resSelect = document.getElementById('settings-fx-video-resolution');
    const costValEl = document.getElementById('omni-credit-cost-val');
    const costDescEl = document.getElementById('omni-credit-cost-desc');
    if (!durSelect || !resSelect || !costValEl) return;

    const dur = durSelect.value || '10';
    const res = resSelect.value || '720p';
    const cost = (OMNI_CREDIT_MATRIX[res] && OMNI_CREDIT_MATRIX[res][dur]) || (res === '360p' ? 8 : 15);

    costValEl.textContent = String(cost);
    if (costDescEl) {
        costDescEl.textContent = `${dur}s · ${res}`;
    }
}

// Omni Flash 时长与分辨率切换仅该模型面板提供（Veo 系列时长与分辨率固定），所以只在当前选中的
// 视频模型是 Omni Flash 时显示。
function updateFxVideoDurationVisibility() {
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    const durationGroup = document.getElementById('fx-video-duration-group');
    const resolutionGroup = document.getElementById('fx-video-resolution-group');
    const isOmni = fxVideoModelSelect && fxVideoModelSelect.value === 'Omni Flash';
    if (durationGroup) durationGroup.style.display = isOmni ? '' : 'none';
    if (resolutionGroup) resolutionGroup.style.display = isOmni ? '' : 'none';
    if (isOmni) updateOmniCreditEstimate();
    updateVideoProviderStatus();
}

// 把配置中心表单里的值收进 config 对象（不落盘）。saveConfig 与
// autoSaveConfig 共用，避免两条写入路径读的字段集合漂移。
function applySettingsFormToConfig() {
    const llmModelSelect = document.getElementById('settings-llm-model');
    if (llmModelSelect && llmModelSelect.value) {
        config.model = normalizeLlmModel(llmModelSelect.value);
    }
    const skillProfileSelect = document.getElementById('settings-skill-profile');
    if (skillProfileSelect && skillProfileSelect.value) {
        config.skillProfile = skillProfileSelect.value;
    }

    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        config.imageBackend = imageBackendSelect.value;
    }
    const apiImageModelSelect = document.getElementById('settings-api-image-model');
    // 空值只可能出现在选项还没渲染出来的时候，别拿它覆盖掉用户存着的模型
    if (apiImageModelSelect && apiImageModelSelect.value) {
        config.imageModel = normalizeApiImageModel(apiImageModelSelect.value);
    }
    const fxImageModelSelect = document.getElementById('settings-fx-image-model');
    if (fxImageModelSelect) {
        config.googleFxImageModel = normalizeGoogleFxImageModel(fxImageModelSelect.value);
    }
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        config.videoModel = fxVideoModelSelect.value;
    }
    const videoProviderSelect = document.getElementById('settings-video-provider');
    if (videoProviderSelect) {
        config.videoProvider = videoProviderSelect.value;
    }
    const flow2apiConcurrencySelect = document.getElementById('settings-flow2api-video-concurrency');
    if (flow2apiConcurrencySelect && flow2apiConcurrencySelect.value) {
        config.flow2apiVideoConcurrency = parseInt(flow2apiConcurrencySelect.value, 10) || DEFAULT_CONFIG.flow2apiVideoConcurrency;
    }
    const videoRetrySelect = document.getElementById('settings-video-retry-count');
    if (videoRetrySelect) {
        const count = videoRetrySelect.value === '' ? NaN : Number(videoRetrySelect.value);
        config.videoRetryCount = Number.isInteger(count) && count >= 0 && count <= 10 ? count : 5;
    }
    const continuousVideoSelect = document.getElementById('settings-video-continuous-generation');
    if (continuousVideoSelect && ['true', 'false'].includes(continuousVideoSelect.value)) {
        config.videoContinuousGeneration = continuousVideoSelect.value === 'true';
    }
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        config.videoDuration = fxVideoDurationSelect.value;
    }
    const fxVideoResolutionSelect = document.getElementById('settings-fx-video-resolution');
    if (fxVideoResolutionSelect) {
        config.videoResolution = fxVideoResolutionSelect.value;
    }
    const fxVideoRefModeSelect = document.getElementById('settings-fx-video-ref-mode');
    if (fxVideoRefModeSelect) {
        config.videoRefMode = fxVideoRefModeSelect.value;
    }
    const trendUrlsInput = document.getElementById('settings-ideation-trend-urls');
    if (trendUrlsInput) {
        config.ideationTrendUrls = trendUrlsInput.value.trim();
    }
    const searchQueryInput = document.getElementById('settings-ideation-search-query');
    if (searchQueryInput) {
        config.ideationSearchQuery = searchQueryInput.value.trim();
    }
    const ratioSelect = document.getElementById('settings-image-ratio');
    if (ratioSelect) config.imageAspectRatio = ratioSelect.value.trim();
    const qualitySelect = document.getElementById('settings-image-quality');
    if (qualitySelect) config.imageQuality = qualitySelect.value.trim();
    const candidateConcurrencySelect = document.getElementById('settings-candidate-concurrency');
    if (candidateConcurrencySelect && candidateConcurrencySelect.value) {
        config.candidateConcurrency = parseInt(candidateConcurrencySelect.value, 10) || 4;
    }

    // 任务提醒设置
    const soundNotifCb = document.getElementById('settings-sound-notification');
    if (soundNotifCb) config.soundNotificationEnabled = soundNotifCb.checked;

    const notifVolInput = document.getElementById('settings-notification-volume');
    if (notifVolInput) config.notificationVolume = parseInt(notifVolInput.value, 10) || 0;

    const taskbarFlashCb = document.getElementById('settings-taskbar-flash');
    if (taskbarFlashCb) config.taskbarFlashEnabled = taskbarFlashCb.checked;

    const desktopNotifCb = document.getElementById('settings-desktop-notification');
    if (desktopNotifCb) config.desktopNotificationEnabled = desktopNotifCb.checked;
}

function saveConfig() {
    return autoSaveConfig();
}

/* 配置改动即时保存，服务端确认后才显示已同步。失败保持可见，支持重试。 */
let settingsSavedFlagTimer = null;
let _settingsSaveRevision = 0;

function setSettingsSaveState(state, message) {
    const flag = document.getElementById('settings-saved-flag');
    const retry = document.getElementById('settings-sync-retry');
    if (settingsSavedFlagTimer) clearTimeout(settingsSavedFlagTimer);
    if (retry) retry.hidden = state !== 'error';
    if (!flag) return;
    flag.hidden = false;
    flag.dataset.state = state;
    flag.textContent = message;
    if (state === 'saved') {
        settingsSavedFlagTimer = setTimeout(() => { flag.hidden = true; }, 2400);
    }
}

const _FX_SERVER_SYNC_KEYS = ['videoProvider', 'videoModel', 'googleFxImageModel', 'videoDuration', 'videoResolution', 'videoRefMode', 'flow2apiVideoConcurrency', 'videoRetryCount', 'videoContinuousGeneration'];
let _fxSyncPending = null;
let _fxSyncQueued = null;
let _fxSyncInFlight = false;

async function flushFxModelSync() {
    if (_fxSyncInFlight || !_fxSyncQueued) return;
    const { patch, revision } = _fxSyncQueued;
    _fxSyncQueued = null;
    _fxSyncInFlight = true;
    try {
        const response = await fetch('/api/google-fx/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ patch }),
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'ok') throw new Error(data.message || `HTTP ${response.status}`);
        if (revision === _settingsSaveRevision) setSettingsSaveState('saved', '✓ 已保存并同步');
    } catch (error) {
        if (revision === _settingsSaveRevision) {
            setSettingsSaveState('error', '已存本机，服务端未同步');
        }
    } finally {
        _fxSyncInFlight = false;
        // 串行写入，避免较旧的请求在新设置之后落盘。
        if (_fxSyncQueued && !_fxSyncPending) flushFxModelSync();
    }
}

function syncFxModelToServer(revision = _settingsSaveRevision) {
    const patch = {};
    for (const key of _FX_SERVER_SYNC_KEYS) {
        if (key in config) patch[key] = config[key];
    }
    if (!Object.keys(patch).length) {
        setSettingsSaveState('saved', '✓ 已保存');
        return;
    }
    setSettingsSaveState('saving', '正在保存…');
    _fxSyncQueued = { patch, revision };
    if (_fxSyncPending) clearTimeout(_fxSyncPending);
    _fxSyncPending = setTimeout(() => {
        _fxSyncPending = null;
        flushFxModelSync();
    }, 300);
}

function autoSaveConfig() {
    applySettingsFormToConfig();
    if (typeof normalizeRetiredGateConfig === 'function') normalizeRetiredGateConfig(config);
    const revision = ++_settingsSaveRevision;
    try {
        localStorage.setItem('spark_config', JSON.stringify(config));
    } catch (error) {
        if (_fxSyncPending) clearTimeout(_fxSyncPending);
        _fxSyncPending = null;
        _fxSyncQueued = null;
        setSettingsSaveState('error', '本机保存失败，请重试');
        return false;
    }
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    syncSettingsApiImageModelPicker();
    syncIdeationSkillProfilePicker();
    syncFxModelToServer(revision);
    return true;
}

function resetConfig() {
    // 模型项已无配置中心表单：直接重置 config 对象，尾部的 sync 会刷新两个内嵌选择器
    //（与其余表单项一致，点「保存配置」后才整体持久化到 localStorage）
    config.model = DEFAULT_CONFIG.model;
    config.imageModel = DEFAULT_CONFIG.imageModel;
    config.googleFxImageModel = DEFAULT_CONFIG.googleFxImageModel;
    config.videoProvider = DEFAULT_CONFIG.videoProvider;
    const videoProviderSelect = document.getElementById('settings-video-provider');
    if (videoProviderSelect) videoProviderSelect.value = DEFAULT_CONFIG.videoProvider;
    const flow2apiConcurrencySelect = document.getElementById('settings-flow2api-video-concurrency');
    if (flow2apiConcurrencySelect) flow2apiConcurrencySelect.value = String(DEFAULT_CONFIG.flow2apiVideoConcurrency);
    const videoRetrySelect = document.getElementById('settings-video-retry-count');
    if (videoRetrySelect) videoRetrySelect.value = String(DEFAULT_CONFIG.videoRetryCount ?? 5);
    const continuousVideoSelect = document.getElementById('settings-video-continuous-generation');
    if (continuousVideoSelect) continuousVideoSelect.value = String(DEFAULT_CONFIG.videoContinuousGeneration ?? true);
    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        imageBackendSelect.value = DEFAULT_CONFIG.imageBackend;
    }
    // API 生图模型现在有表单项了：只改 config.imageModel 不改下拉框，
    // 末尾的 applySettingsFormToConfig 会把旧值原样读回来（等于没恢复）。
    const apiImageModelSelect = document.getElementById('settings-api-image-model');
    if (apiImageModelSelect) {
        syncSettingsApiImageModelPicker();
        apiImageModelSelect.value = DEFAULT_CONFIG.imageModel;
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
    const fxVideoResolutionSelect = document.getElementById('settings-fx-video-resolution');
    if (fxVideoResolutionSelect) {
        fxVideoResolutionSelect.value = DEFAULT_CONFIG.videoResolution;
    }
    const fxVideoRefModeSelect = document.getElementById('settings-fx-video-ref-mode');
    if (fxVideoRefModeSelect) {
        fxVideoRefModeSelect.value = DEFAULT_CONFIG.videoRefMode;
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
    const candidateConcurrencySelect = document.getElementById('settings-candidate-concurrency');
    if (candidateConcurrencySelect) {
        candidateConcurrencySelect.value = String(DEFAULT_CONFIG.candidateConcurrency || 4);
    }
    updateFxImageModelVisibility();
    updateFxVideoDurationVisibility();
    // 表单已经全部改成「改动即存」，恢复默认自然也得当场落盘，否则关掉弹窗
    // 就悄悄回滚了（旧版靠底部「保存配置」兜底，那个按钮现在只是「完成」）。
    // 必须先把刚写回表单的默认值收回 config——上面几段只改了 DOM，
    // 直接持久化 config 会把用户的旧值原样存回去。
    applySettingsFormToConfig();
    // 恢复默认也保持所有审查规则的永久退役状态。
    if (typeof resetGateSettings === 'function') resetGateSettings();
    syncSettingsLlmModelPicker();
    syncSettingsSkillProfilePicker();
    autoSaveConfig();
}

/* ══════════════════════════════════════════════════════════════════════
   配置中心 · 分区导航
   左栏 nav 与右栏 section 用 data-section 配对，一次只显示一个。
   记住上次停留的分区（多数改动是回同一个地方微调）。
   ══════════════════════════════════════════════════════════════════════ */
const SETTINGS_SECTION_KEY = 'spark_settings_section';

function switchSettingsSection(name) {
    const nav = document.getElementById('settings-nav');
    const pane = document.getElementById('settings-pane');
    if (!nav || !pane) return;

    const sections = Array.from(pane.querySelectorAll('.settings-section'));
    const target = sections.some(s => s.dataset.section === name)
        ? name : (sections[0] ? sections[0].dataset.section : null);
    if (!target) return;

    nav.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === target);
    });
    sections.forEach(sec => {
        sec.classList.toggle('active', sec.dataset.section === target);
    });
    pane.scrollTop = 0;
    localStorage.setItem(SETTINGS_SECTION_KEY, target);

    // 号池分区：每次进来重读一次池子和 AdsPower 环境列表（别人可能在
    // AdsPower 里新建了环境，或者积分刚被别的任务扣过）。
    if (target === 'pool') {
        if (typeof loadAccountPool === 'function') loadAccountPool();
        if (typeof loadAccountPoolAdspowerProfiles === 'function') loadAccountPoolAdspowerProfiles();
    }
    // 成片引导：设置在服务端，每次进来重读（可能在另一台设备上改过）
    if (target === 'cta' && window.EngagementCtaSettings) window.EngagementCtaSettings.load();
}

function findSettingsFields(query) {
    const pane = document.getElementById('settings-pane');
    const words = String(query || '').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    if (!pane || !words.length) return [];
    return Array.from(pane.querySelectorAll('.settings-field')).filter(field => {
        const section = field.closest('.settings-section');
        if (!section || section.dataset.section === 'pool') return false;
        // Closed details are searchable; controls disabled by the chosen backend are not.
        for (let node = field; node && node !== section; node = node.parentElement) {
            if (node.hidden || node.style.display === 'none') return false;
        }
        const label = field.querySelector('label');
        const hint = field.querySelector('.settings-hint');
        const text = `${label ? label.textContent : ''} ${hint ? hint.textContent : ''}`.toLocaleLowerCase();
        return words.every(word => text.includes(word));
    });
}

function initSettingsSearch() {
    const input = document.getElementById('settings-search-input');
    const results = document.getElementById('settings-search-results');
    const wrap = document.getElementById('settings-search-wrap');
    const modal = document.getElementById('settings-modal');
    if (!input || !results || !wrap || input.dataset.bound) return;
    input.dataset.bound = '1';

    const close = () => {
        results.hidden = true;
        input.setAttribute('aria-expanded', 'false');
    };
    const search = () => {
        results.replaceChildren();
        if (!input.value.trim()) { close(); return; }
        const matches = findSettingsFields(input.value);
        const description = document.createElement('p');
        description.className = 'settings-search-description';
        description.textContent = matches.length
            ? `找到 ${matches.length} 项${matches.length > 10 ? '，先显示前 10 项，可缩小关键词' : ''}`
            : '没有匹配项。仅查找当前生成方式可用的设置；账号请到「生成号池」查看。';
        results.appendChild(description);
        matches.slice(0, 10).forEach(field => {
            const section = field.closest('.settings-section');
            const label = field.querySelector('label');
            const title = section.querySelector('.settings-section-title');
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'settings-search-result';
            const name = document.createElement('span');
            name.textContent = label.textContent;
            const location = document.createElement('small');
            location.textContent = title ? title.textContent : '';
            button.appendChild(name);
            button.appendChild(location);
            button.addEventListener('click', () => {
                switchSettingsSection(section.dataset.section);
                for (let node = field.parentElement; node && node !== section; node = node.parentElement) {
                    if (node.tagName === 'DETAILS') node.open = true;
                }
                const previous = modal && modal.querySelector('.settings-search-highlight');
                if (previous) previous.classList.remove('settings-search-highlight');
                field.classList.add('settings-search-highlight');
                const control = field.querySelector('input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled])');
                if (!control) field.tabIndex = -1;
                (control || field).focus({ preventScroll: true });
                field.scrollIntoView({ block: 'center' });
                input.value = '';
                close();
            });
            results.appendChild(button);
        });
        results.hidden = false;
        input.setAttribute('aria-expanded', 'true');
    };
    input.addEventListener('input', search);
    input.addEventListener('focus', search);
    wrap.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !results.hidden) {
            event.preventDefault();
            event.stopPropagation();
            input.focus();
            close();
        } else if ((event.key === 'ArrowDown' || event.key === 'Enter') && event.target === input && !results.hidden) {
            const first = results.querySelector('button');
            if (first) {
                event.preventDefault();
                if (event.key === 'Enter') first.click();
                else first.focus();
            }
        }
    });
    if (modal) modal.addEventListener('click', event => {
        if (!wrap.contains(event.target)) close();
    });
}

function initSettingsCenter() {
    initSettingsSearch();
    const retrySave = document.getElementById('settings-sync-retry');
    if (retrySave && !retrySave.dataset.bound) {
        retrySave.dataset.bound = '1';
        retrySave.addEventListener('click', autoSaveConfig);
    }
    const nav = document.getElementById('settings-nav');
    if (nav && !nav.dataset.bound) {
        nav.dataset.bound = '1';
        nav.addEventListener('click', (e) => {
            const btn = e.target.closest('.settings-nav-item');
            if (btn) switchSettingsSection(btn.dataset.section);
        });
    }

    // 改动即存：整个右栏做事件委托，新增字段自动纳入，不用再补绑定。
    // 号池分区的控件自己有更专门的处理（增删改直接打服务端），排除在外。
    const pane = document.getElementById('settings-pane');
    if (pane && !pane.dataset.bound) {
        pane.dataset.bound = '1';
        pane.addEventListener('change', (e) => {
            const el = e.target;
            if (!el.matches('input, select, textarea')) return;
            if (el.closest('.account-pool-manage-body')) return;
            if (el.closest('[data-settings-navigation]')) return;
            // 自己直接写服务端的分区（成片引导）不进 localStorage 的 config
            if (el.closest('[data-own-save]')) return;
            autoSaveConfig();
        });
    }

    initNotificationSettingsEvents();
    switchSettingsSection(localStorage.getItem(SETTINGS_SECTION_KEY) || 'backend');
}

function updateDesktopPermissionStatus() {
    const statusEl = document.getElementById('desktop-permission-status');
    const reqBtn = document.getElementById('request-desktop-permission-btn');
    if (!statusEl) return;
    if (typeof window === 'undefined' || !('Notification' in window)) {
        statusEl.textContent = '（当前浏览器不支持桌面通知）';
        if (reqBtn) reqBtn.disabled = true;
        return;
    }
    if (Notification.permission === 'granted') {
        statusEl.textContent = '✓ 桌面通知已授权';
        statusEl.style.color = 'var(--success-color, #10b981)';
        if (reqBtn) {
            reqBtn.textContent = '✓ 已授权';
            reqBtn.disabled = true;
        }
    } else if (Notification.permission === 'denied') {
        statusEl.textContent = '⚠️ 已被浏览器拦截（请在地址栏锁头图标中开启通知）';
        statusEl.style.color = 'var(--error-color, #ef4444)';
        if (reqBtn) {
            reqBtn.textContent = '已被拦截';
            reqBtn.disabled = true;
        }
    } else {
        statusEl.textContent = '（尚未授权，点击左侧按钮开启）';
        statusEl.style.color = 'var(--text-secondary, #94a3b8)';
        if (reqBtn) {
            reqBtn.textContent = '🔑 授权桌面通知权限';
            reqBtn.disabled = false;
        }
    }
}

function initNotificationSettingsEvents() {
    const volInput = document.getElementById('settings-notification-volume');
    const volVal = document.getElementById('settings-notification-volume-val');
    if (volInput && !volInput.dataset.bound) {
        volInput.dataset.bound = '1';
        volInput.addEventListener('input', () => {
            const v = parseInt(volInput.value, 10) || 0;
            if (volVal) volVal.textContent = `${v}%`;
            config.notificationVolume = v;
            autoSaveConfig();
        });
    }

    const testSuccessBtn = document.getElementById('test-sound-success-btn');
    if (testSuccessBtn && !testSuccessBtn.dataset.bound) {
        testSuccessBtn.dataset.bound = '1';
        testSuccessBtn.addEventListener('click', async () => {
            if (typeof NotificationCenter !== 'undefined') {
                const status = await NotificationCenter.playSuccessSound();
                if (typeof showToast === 'function') {
                    const v = typeof config.notificationVolume === 'number' ? config.notificationVolume : 80;
                    // 试听没声音要说清原因：浏览器把 AudioContext 挂起、音量拉到 0、
                    // 或者根本不支持 Web Audio，三种情况以前都表现成「点了没反应」。
                    if (status === 'suspended') {
                        showToast('⚠️ 浏览器暂时挂起了音频，请先在页面空白处点一下再试听', 'warning', 5000);
                    } else if (status === 'muted') {
                        showToast('⚠️ 当前提示音量为 0%（或声音提醒已关闭），听不到是正常的', 'warning', 5000);
                    } else if (status === 'unsupported') {
                        showToast('⚠️ 当前浏览器不支持 Web Audio，无法播放提示音', 'error', 5000);
                    } else {
                        showToast(`🔊 正在播放成功和弦提示音（当前音量: ${v}%）`, 'success', 2500);
                    }
                }
            }
        });
    }

    const testErrorBtn = document.getElementById('test-sound-error-btn');
    if (testErrorBtn && !testErrorBtn.dataset.bound) {
        testErrorBtn.dataset.bound = '1';
        testErrorBtn.addEventListener('click', async () => {
            if (typeof NotificationCenter !== 'undefined') {
                const status = await NotificationCenter.playErrorSound();
                if (typeof showToast === 'function') {
                    const v = typeof config.notificationVolume === 'number' ? config.notificationVolume : 80;
                    // 试听没声音要说清原因：浏览器把 AudioContext 挂起、音量拉到 0、
                    // 或者根本不支持 Web Audio，三种情况以前都表现成「点了没反应」。
                    if (status === 'suspended') {
                        showToast('⚠️ 浏览器暂时挂起了音频，请先在页面空白处点一下再试听', 'warning', 5000);
                    } else if (status === 'muted') {
                        showToast('⚠️ 当前提示音量为 0%（或声音提醒已关闭），听不到是正常的', 'warning', 5000);
                    } else if (status === 'unsupported') {
                        showToast('⚠️ 当前浏览器不支持 Web Audio，无法播放提示音', 'error', 5000);
                    } else {
                        showToast(`🚨 正在播放失败警示双音（当前音量: ${v}%）`, 'error', 2500);
                    }
                }
            }
        });
    }

    const testActionBtn = document.getElementById('test-sound-action-btn');
    if (testActionBtn && !testActionBtn.dataset.bound) {
        testActionBtn.dataset.bound = '1';
        testActionBtn.addEventListener('click', async () => {
            if (typeof NotificationCenter !== 'undefined') {
                const status = await NotificationCenter.playActionRequiredSound();
                if (typeof showToast === 'function') {
                    const v = typeof config.notificationVolume === 'number' ? config.notificationVolume : 80;
                    // 试听没声音要说清原因：浏览器把 AudioContext 挂起、音量拉到 0、
                    // 或者根本不支持 Web Audio，三种情况以前都表现成「点了没反应」。
                    if (status === 'suspended') {
                        showToast('⚠️ 浏览器暂时挂起了音频，请先在页面空白处点一下再试听', 'warning', 5000);
                    } else if (status === 'muted') {
                        showToast('⚠️ 当前提示音量为 0%（或声音提醒已关闭），听不到是正常的', 'warning', 5000);
                    } else if (status === 'unsupported') {
                        showToast('⚠️ 当前浏览器不支持 Web Audio，无法播放提示音', 'error', 5000);
                    } else {
                        showToast(`🔔 正在播放待审核门铃音（当前音量: ${v}%）`, 'info', 2500);
                    }
                }
            }
        });
    }

    const reqBtn = document.getElementById('request-desktop-permission-btn');
    if (reqBtn && !reqBtn.dataset.bound) {
        reqBtn.dataset.bound = '1';
        reqBtn.addEventListener('click', async () => {
            if (typeof NotificationCenter !== 'undefined') {
                const res = await NotificationCenter.requestDesktopPermission();
                updateDesktopPermissionStatus();
                if (typeof showToast === 'function') {
                    if (res === 'granted') {
                        showToast('✓ 桌面系统通知权限已开启！', 'success', 3000);
                    } else if (res === 'denied') {
                        showToast('⚠️ 桌面通知权限被拦截，请在浏览器地址栏左侧锁头图标中允许通知。', 'warning', 6000);
                    }
                }
            }
        });
    }

    const testFullBtn = document.getElementById('test-full-notification-btn');
    if (testFullBtn && !testFullBtn.dataset.bound) {
        testFullBtn.dataset.bound = '1';
        testFullBtn.addEventListener('click', async () => {
            if (typeof NotificationCenter !== 'undefined') {
                await NotificationCenter.testInstantNotification('success');
                if (typeof showToast === 'function') {
                    showToast('已触发立即测试提醒！若已切到其他软件，桌面卡片与任务栏闪烁已弹出。', 'info', 4000);
                }
            }
        });
    }

    const testCountdownBtn = document.getElementById('test-countdown-notification-btn');
    if (testCountdownBtn && !testCountdownBtn.dataset.bound) {
        testCountdownBtn.dataset.bound = '1';
        testCountdownBtn.addEventListener('click', () => {
            if (typeof NotificationCenter !== 'undefined') {
                NotificationCenter.startCountdownTest(3, 'success');
            }
        });
    }
}

// 跨标签页同步：FX 控制台（console.html）在另一个标签页修改了 spark_config
// 中的模型设置后，storage 事件会触发这里的重载——主页面不用手动刷新就能感知
// 到控制台的改动（选择器自动跟新值、下一次生成发的就是新模型）。
if (typeof window !== 'undefined') {
    window.addEventListener('storage', (e) => {
        if (e.key !== 'spark_config' || !e.newValue) return;
        try {
            const updated = JSON.parse(e.newValue);
            let dirty = false;
            for (const key of _FX_SERVER_SYNC_KEYS) {
                if (key in updated && config[key] !== updated[key]) {
                    config[key] = updated[key];
                    dirty = true;
                }
            }
            if (dirty) {
                // 刷新主界面的相关 UI 选择器
                const videoProviderSelect = document.getElementById('settings-video-provider');
                if (videoProviderSelect) videoProviderSelect.value = config.videoProvider || DEFAULT_CONFIG.videoProvider;
                const flow2apiConcurrencySelect = document.getElementById('settings-flow2api-video-concurrency');
                if (flow2apiConcurrencySelect) flow2apiConcurrencySelect.value = String(config.flow2apiVideoConcurrency || DEFAULT_CONFIG.flow2apiVideoConcurrency);
                const videoRetrySelect = document.getElementById('settings-video-retry-count');
                if (videoRetrySelect) videoRetrySelect.value = String(config.videoRetryCount ?? DEFAULT_CONFIG.videoRetryCount ?? 5);
                const continuousVideoSelect = document.getElementById('settings-video-continuous-generation');
                if (continuousVideoSelect) continuousVideoSelect.value = String(config.videoContinuousGeneration ?? true);
                const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
                if (fxVideoModelSelect) fxVideoModelSelect.value = config.videoModel || '';
                const fxImageModelSelect = document.getElementById('settings-fx-image-model');
                if (fxImageModelSelect) fxImageModelSelect.value = config.googleFxImageModel || '';
                const fxDurationSelect = document.getElementById('settings-fx-video-duration');
                if (fxDurationSelect) fxDurationSelect.value = config.videoDuration || DEFAULT_CONFIG.videoDuration;
                const fxResolutionSelect = document.getElementById('settings-fx-video-resolution');
                if (fxResolutionSelect) fxResolutionSelect.value = config.videoResolution || DEFAULT_CONFIG.videoResolution;
                const fxRefModeSelect = document.getElementById('settings-fx-video-ref-mode');
                if (fxRefModeSelect) fxRefModeSelect.value = config.videoRefMode || DEFAULT_CONFIG.videoRefMode;
                if (typeof syncIdeationSkillProfilePicker === 'function') syncIdeationSkillProfilePicker();
                if (typeof updateFxVideoDurationVisibility === 'function') updateFxVideoDurationVisibility();
            }
        } catch (_) {}
    });
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

function sanitizeIdeaForStorage(idea, level = 0) {
    if (!idea || typeof idea !== 'object') return null;

    if (level >= 3) {
        return {
            id: idea.id,
            title: idea.title || '',
            theme: idea.theme || '',
            creativity: idea.creativity || '',
            timestamp: idea.timestamp || '',
            _is_stub: true
        };
    }

    const cleanUrl = (url, maxLen = 4096) => {
        if (!url || typeof url !== 'string') return url;
        if (url.startsWith('data:')) {
            if (level >= 1 || url.length > maxLen) {
                return null;
            }
        }
        return url;
    };

    const clone = { ...idea };

    // 1. candidateImages: 候选图片通常包含海量 base64，不宜写入 localStorage
    if (clone.candidateImages) {
        if (level >= 1) {
            delete clone.candidateImages;
        } else {
            const safeCandidates = {};
            let hasAny = false;
            Object.keys(clone.candidateImages).forEach(k => {
                const list = clone.candidateImages[k];
                if (Array.isArray(list)) {
                    const filtered = list.map(item => {
                        if (typeof item === 'string') {
                            return cleanUrl(item, 2048);
                        } else if (item && typeof item === 'object' && item.url) {
                            return { ...item, url: cleanUrl(item.url, 2048) };
                        }
                        return null;
                    }).filter(Boolean);
                    if (filtered.length > 0) {
                        safeCandidates[k] = filtered;
                        hasAny = true;
                    }
                }
            });
            if (hasAny) {
                clone.candidateImages = safeCandidates;
            } else {
                delete clone.candidateImages;
            }
        }
    }

    // 2. covers
    if (Array.isArray(clone.covers)) {
        clone.covers = clone.covers.map(c => cleanUrl(c, 2048)).filter(Boolean);
    }

    // 3. frameRun
    if (clone.frameRun && typeof clone.frameRun === 'object') {
        const fr = { ...clone.frameRun };
        if (fr.collage && typeof fr.collage === 'string' && fr.collage.startsWith('data:')) {
            fr.collage = null; // 拼图为大图 base64 时剔除，避免挤爆 quota
        }
        if (Array.isArray(fr.frames)) {
            fr.frames = fr.frames.map(f => {
                if (!f || typeof f !== 'object') return f;
                const safeF = { ...f };
                if (safeF.url && typeof safeF.url === 'string' && safeF.url.startsWith('data:') && safeF.url.length > 2048) {
                    safeF.url = null;
                }
                if (safeF.candidates || safeF.candidateImages) {
                    delete safeF.candidates;
                    delete safeF.candidateImages;
                }
                return safeF;
            });
        }
        clone.frameRun = fr;
    }

    // 4. Level 2 剪枝调试大字段
    if (level >= 2) {
        delete clone.audit_md;
        delete clone.raw_response;
        delete clone.debug_logs;
    }

    return clone;
}

/**
 * 尝试清理 localStorage 中的次要缓存项以释放空间
 */
function pruneStaleLocalStorage() {
    try {
        // 1. 清理过期的 prompt history
        const phKeys = [];
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key && key.startsWith('spark_prompt_history_')) {
                phKeys.push(key);
            }
        }
        if (phKeys.length > 20) {
            phKeys.slice(20).forEach(k => {
                try { localStorage.removeItem(k); } catch (_) {}
            });
        }

        // 2. 压缩 spark_image_history
        const imgHistStr = localStorage.getItem('spark_image_history');
        if (imgHistStr && imgHistStr.length > 200000) {
            try {
                const hist = JSON.parse(imgHistStr);
                if (Array.isArray(hist) && hist.length > 3) {
                    localStorage.setItem('spark_image_history', JSON.stringify(hist.slice(0, 3)));
                }
            } catch (_) {}
        }
    } catch (_) {}
}

function saveCurrentIdeaState() {
    if (!currentIdea) {
        try {
            localStorage.removeItem('spark_current_idea');
            localStorage.removeItem('spark_current_idea_id');
        } catch (_) {}
        return;
    }

    // 记录 ID 存根供恢复
    try {
        if (currentIdea.id) {
            localStorage.setItem('spark_current_idea_id', String(currentIdea.id));
        }
    } catch (_) {}

    // 分级尝试序列化与写入，绝不向外抛出 QuotaExceededError 异常
    for (let level = 0; level <= 3; level++) {
        try {
            const payload = sanitizeIdeaForStorage(currentIdea, level);
            if (!payload) break;
            const str = JSON.stringify(payload);
            localStorage.setItem('spark_current_idea', str);
            return; // 成功保存
        } catch (e) {
            const isQuota = e.name === 'QuotaExceededError' || e.code === 22 || e.number === 0x8007000E || (e.message && e.message.includes('quota'));
            if (isQuota) {
                console.warn(`[saveCurrentIdeaState] localStorage 配额超限 (level ${level})，尝试降级压缩...`, e);
                if (level === 0) {
                    pruneStaleLocalStorage();
                }
            } else {
                console.warn('[saveCurrentIdeaState] 保存当前创意状态失败:', e);
                break;
            }
        }
    }

    // 若极度紧张连 Level 3 都失败，至少记录 ID 并静默兜底，不中断前端操作流程
    try {
        if (currentIdea && currentIdea.id) {
            localStorage.setItem('spark_current_idea_id', String(currentIdea.id));
        }
    } catch (_) {}
}

async function loadCurrentIdeaState() {
    const stored = localStorage.getItem('spark_current_idea');
    const storedId = localStorage.getItem('spark_current_idea_id');
    let loadedIdea = null;

    if (stored) {
        try {
            loadedIdea = JSON.parse(stored);
        } catch (e) {
            console.error("Failed to parse stored current idea state", e);
        }
    }

    // A cached editing view may belong to a project archived from another window.
    // Verify its server record before exposing controls that can recreate assets.
    const recoveryId = (loadedIdea || {}).id || storedId;
    if (recoveryId) {
        try {
            let latest;
            if (typeof ensureLibraryIdea === 'function') {
                latest = await ensureLibraryIdea(recoveryId, { refresh: true });
            } else {
                const response = await fetch(`/api/library/item?id=${encodeURIComponent(recoveryId)}`);
                latest = response.ok ? await response.json() : null;
            }
            if (latest && latest.id) loadedIdea = { ...(loadedIdea || {}), ...latest };
        } catch (e) { /* Existing offline recovery still applies. */ }
    }
    if (loadedIdea && loadedIdea.archived) {
        currentIdea = null;
        localStorage.removeItem('spark_current_idea');
        localStorage.removeItem('spark_current_idea_id');
        if (typeof openArchivedProject === 'function') await openArchivedProject(loadedIdea.project_key);
        return;
    }

    if (loadedIdea) {
        currentIdea = loadedIdea;
        if (typeof cacheLibraryIdea === 'function' && typeof libraryEntries === 'function'
            && libraryEntries().some(item => String(item.id) === String(loadedIdea.id))) {
            cacheLibraryIdea(loadedIdea);
        }
        renderIdea(loadedIdea);
        
        const placeholderView = document.getElementById('output-placeholder-view');
        const contentView = document.getElementById('output-content-view');
        const loadingView = document.getElementById('output-loading-view');
        
        if (placeholderView) placeholderView.classList.remove('active');
        if (loadingView) loadingView.classList.remove('active');
        if (contentView) contentView.classList.add('active');
        
        const lastTab = localStorage.getItem('spark_active_tab') || 'overview';
        switchTab(lastTab);
        
        updateActiveGenerationBanner();

        // 完整记录已在上方 await 后恢复；不再异步替换 owner 对象，避免抹掉接流期间的增量结果。
    }
}
