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
        beats: document.getElementById('slider-beats').value,
        beatCountMode: (document.getElementById('beat-count-mode') || {}).value || 'adaptive'
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
        setVal('beat-count-mode', state.beatCountMode || 'adaptive');
        
    } catch (e) {
        console.error("Failed to load selection state", e);
    }
    updateConfigSummary();
}

function updateConfigSummary() {
    // 选题来自选中（载入维度）过的灵感卡片（联网参考驱动），不再有基础场景主题
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
    const beatMode = ((document.getElementById('beat-count-mode') || {}).value || 'adaptive') === 'fixed'
        ? '固定' : '自适应上限';
    const pacingId = (typeof loadedIdeationCover !== 'undefined' && loadedIdeationCover
        && loadedIdeationCover.pacing_skeleton) || '';
    const pacingText = pacingId === 'dual_payoff' ? '内外双重完工'
        : (pacingId === 'nested_space_payoff' ? '双空间一比一复刻'
        : (pacingId === 'linear_milestone' ? '单线里程碑' : '未载入'));
    
    const summaryText = `${themeText}${anchorsStr} | 骨架:${pacingText}, 复杂度:${complexityText}, 预算:${budgetText}, 反差:${ratioVal}%, 尺度:${creativityText}, ${beatMode}:${beatsVal}拍`;
    
    const summaryEl = document.getElementById('config-summary-text');
    if (summaryEl) {
        summaryEl.textContent = summaryText;
    }

    // 摘要框本身在极简布局里不显示了（7 项里 4 项是永久固定的隐藏值），会变的
    // 三项改由激发轨呈现。这里搭车刷新——本函数已覆盖初始化 / 滑杆变更 /
    // 卡片载入 / 骨架勾选 / 面板切换这几乎全部时机。见 js/spark_rail.js
    if (typeof updateSparkRail === 'function') updateSparkRail();
}

function applyPreset(presetName) {
    const p = PRESETS[presetName];
    if (!p) return;

    // （#theme-selector 已随基础场景主题选择器一并移除，预设不再有主题这一项）

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
    // 预设/随机同样是用户拍板的拍数，之后载入灵感卡片不该再把它覆盖掉
    if (typeof markBeatsUserOverridden === 'function') markBeatsUserOverridden();

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

/* ── 激发维度内嵌「提示词链路」选择器 ─────────────────────────────────
   做哪个视频模型的提示词，就读哪个技能包、走哪套镜头语法：
     · base —— restoration-prompt-composer，Veo 系列，一条连续的施工延时；
     · omni —— gemini-omni-restoration-composer，Gemini Omni，多镜头组接（镜头数随片长）
       （远景/全景/中景/近景/特写/结果远景）+ UGC 手机拍摄质感，禁一镜到底。
   停在 auto 时由服务端按视频模型名推断（active_skill_profile）。UI 排在 LLM 模型
   之前：链路决定"提示词写成什么样"，模型只决定"谁来写"，前者更上位。

   ⚠️ 前端不自己判断"omni 该走哪条"：那张规则表由 /api/mode 的 skill_profile_rules
   下发（服务端 SKILL_PROFILE_VIDEO_MODEL_RULES 原样），这里只负责按表匹配显示。
   在这里硬编码一份 /omni/ 判断，就是给同一件事留了第二个会漂移的真相源。
   拿不到规则表（/api/mode 还没回、或旧服务端）时不猜：auto 的徽标退回只报视频
   模型名，让用户自己对照，而不是显示一个可能是错的链路名。 */
const SKILL_PROFILE_CHOICES = [
    { value: 'auto', label: '自动', hint: '跟随视频模型' },
    { value: 'base', label: 'Veo · 单镜延时', hint: 'restoration-prompt-composer' },
    { value: 'omni', label: 'Omni · 多镜头', hint: 'gemini-omni-restoration-composer' },
];

// /api/mode 下发的链路信息（app.js initServerMode 填）。规则表形如
// [['omni','omni']]：视频模型名（小写）含 needle 即用该 profile，都不命中回 default。
window.SKILL_PROFILE_RULES = window.SKILL_PROFILE_RULES || null;
window.SKILL_PROFILE_DEFAULT = window.SKILL_PROFILE_DEFAULT || 'base';

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

function syncIdeationSkillProfilePicker() {
    const wrap = document.getElementById('ideation-skill-profile-groups');
    if (!wrap) return;
    const current = config.skillProfile || 'auto';

    wrap.innerHTML = '';
    const group = document.createElement('div');
    group.className = 'model-family-group active';
    group.dataset.family = 'skill-profile';

    const tag = document.createElement('span');
    tag.className = 'model-family-tag';
    tag.textContent = '链路';
    group.appendChild(tag);

    const row = document.createElement('div');
    row.className = 'model-chip-row';
    SKILL_PROFILE_CHOICES.forEach(choice => {
        const isSelected = choice.value === current;
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'model-chip' + (isSelected ? ' selected' : '');
        chip.title = choice.value === 'auto'
            ? '跟随「FX 视频模型」下拉框：选 Omni Flash 走 Omni 链路，选 Veo 系列走 Veo 链路'
            : `${choice.hint}（钉死这条链路，不再跟随视频模型）`;

        const name = document.createElement('span');
        name.className = 'model-chip-name';
        name.textContent = choice.label;
        chip.appendChild(name);

        chip.addEventListener('click', () => {
            if ((config.skillProfile || 'auto') === choice.value) return;
            config.skillProfile = choice.value;
            localStorage.setItem('spark_config', JSON.stringify(config));
            const effective = choice.value === 'auto' ? resolveAutoSkillProfile() : choice.value;
            showToast(
                `提示词链路已切换：${choice.label}`
                + (choice.value === 'auto' && effective ? `（当前实际走 ${effective}）` : '')
                + '（下次激发/合成生效）',
                'success');
            syncIdeationSkillProfilePicker();
            if (typeof collapseIdeationPickerOnMobile === 'function') {
                collapseIdeationPickerOnMobile();
            }
        });
        row.appendChild(chip);
    });
    group.appendChild(row);
    wrap.appendChild(group);

    const hint = document.getElementById('ideation-skill-profile-active-hint');
    if (!hint) return;
    if (current !== 'auto') {
        const picked = SKILL_PROFILE_CHOICES.find(c => c.value === current);
        hint.textContent = `使用中 · ${picked ? picked.label : current}`;
        return;
    }
    const effective = resolveAutoSkillProfile();
    const effectiveLabel = effective
        && (SKILL_PROFILE_CHOICES.find(c => c.value === effective) || {}).label;
    hint.textContent = effectiveLabel
        ? `使用中 · ${effectiveLabel}（跟随 ${config.videoModel || '视频模型'}）`
        : `跟随视频模型 · ${config.videoModel || '未设置'}`;
}

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
    const current = config.model || 'gemini-3.6-flash-high';
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
                // 手机上芯片组是临时展开的（picker-collapsed，见 index.html）：
                // 选完就收回去，把页脚高度还给上面的案例库/灵感卡。
                if (typeof collapseIdeationPickerOnMobile === 'function') {
                    collapseIdeationPickerOnMobile();
                }
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
        ? normalizeGoogleFxImageModel(config.googleFxImageModel)
        : (config.imageModel || 'nano-banana-2');
    if (isFx) config.googleFxImageModel = current;

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
                config.googleFxImageModel = normalizeGoogleFxImageModel(sel.value);
                const fxSel = document.getElementById('settings-fx-image-model');
                if (fxSel) fxSel.value = config.googleFxImageModel;
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
        fxImageModelSelect.value = normalizeGoogleFxImageModel(config.googleFxImageModel);
    }
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
    syncIdeationSkillProfilePicker();
}

// 显隐一律用空串还原（而不是写死 'block'）：配置中心的字段行是 CSS grid
// （.settings-field），内联 display:block 会把 label/控件/说明拍回竖排。
function updateFxImageModelVisibility() {
    const backendSelect = document.getElementById('settings-image-backend');
    const fxImageGroup = document.getElementById('fx-image-model-group');
    const fxVideoGroup = document.getElementById('fx-video-model-group');
    const showFx = backendSelect && backendSelect.value === 'google_fx';
    if (fxImageGroup) fxImageGroup.style.display = showFx ? '' : 'none';
    if (fxVideoGroup) fxVideoGroup.style.display = showFx ? '' : 'none';
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
    durationGroup.style.display = (showFx && isOmni) ? '' : 'none';
}

// 把配置中心表单里的值收进 config 对象（不落盘）。saveConfig 与
// autoSaveConfig 共用，避免两条写入路径读的字段集合漂移。
function applySettingsFormToConfig() {
    // config.model / config.imageModel 不再从本弹窗读取：由激发页脚与帧序列卡片的
    // 内嵌选择器直接维护（改动即存），这里只负责其余生成参数
    const imageBackendSelect = document.getElementById('settings-image-backend');
    if (imageBackendSelect) {
        config.imageBackend = imageBackendSelect.value;
    }
    const fxImageModelSelect = document.getElementById('settings-fx-image-model');
    if (fxImageModelSelect) {
        config.googleFxImageModel = normalizeGoogleFxImageModel(fxImageModelSelect.value);
    }
    const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
    if (fxVideoModelSelect) {
        config.videoModel = fxVideoModelSelect.value;
    }
    const fxVideoDurationSelect = document.getElementById('settings-fx-video-duration');
    if (fxVideoDurationSelect) {
        config.videoDuration = fxVideoDurationSelect.value;
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
}

function saveConfig() {
    applySettingsFormToConfig();
    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    syncIdeationLlmPicker();
    syncIdeationSkillProfilePicker();
    showToast("API 配置保存成功！", "success");
    checkApiStatus();
}

/* ── 改动即存 ─────────────────────────────────────────────────────────
   配置中心的每个控件 change 即落盘，用户不必记得回头按保存按钮（和激发页脚
   的模型选择器、帧序列卡片的生图模型选择器是同一套约定）。反馈只在弹窗头部
   闪一个「✓ 已保存」——每改一项弹一次 toast 太吵。
   不在这里调 checkApiStatus()：本表单没有一项会改变 LLM 网关路由。 */
let settingsSavedFlagTimer = null;

function flashSettingsSaved() {
    const flag = document.getElementById('settings-saved-flag');
    if (!flag) return;
    flag.hidden = false;
    if (settingsSavedFlagTimer) clearTimeout(settingsSavedFlagTimer);
    settingsSavedFlagTimer = setTimeout(() => { flag.hidden = true; }, 1600);
}

// FX 模型设置由服务端 server_config.json 统一管理：前端改了之后要同步推到
// 服务端，否则 effective_config 会采用 SERVER_CONFIG 里的旧值（服务端优先）。
// 静默调用，失败不弹 toast——下一次生成请求仍然会把最新 config 带过去。
const _FX_SERVER_SYNC_KEYS = ['videoModel', 'googleFxImageModel', 'videoDuration'];
let _fxSyncPending = null;

function syncFxModelToServer() {
    const patch = {};
    for (const key of _FX_SERVER_SYNC_KEYS) {
        if (key in config) patch[key] = config[key];
    }
    if (!Object.keys(patch).length) return;
    // 防抖：快速连续切换模型时只发最后一次
    if (_fxSyncPending) clearTimeout(_fxSyncPending);
    _fxSyncPending = setTimeout(() => {
        _fxSyncPending = null;
        fetch('/api/google-fx/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ patch }),
        }).catch(() => {});  // 静默失败
    }, 300);
}

function autoSaveConfig() {
    applySettingsFormToConfig();
    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    // 链路选择器停在 auto 时，徽标显示的是"跟着视频模型现在实际走哪条"——
    // 改了 FX 视频模型下拉框却不重刷，徽标就会停在旧链路上，看起来像没生效。
    syncIdeationSkillProfilePicker();
    // 同步 FX 模型设置到服务端 server_config.json
    syncFxModelToServer();
    flashSettingsSaved();
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
    // 表单已经全部改成「改动即存」，恢复默认自然也得当场落盘，否则关掉弹窗
    // 就悄悄回滚了（旧版靠底部「保存配置」兜底，那个按钮现在只是「完成」）。
    // 必须先把刚写回表单的默认值收回 config——上面几段只改了 DOM，
    // 直接持久化 config 会把用户的旧值原样存回去。
    applySettingsFormToConfig();
    localStorage.setItem('spark_config', JSON.stringify(config));
    updateCoverModelDisplay();
    syncFramesImageModelPicker();
    syncIdeationLlmPicker();
    syncIdeationSkillProfilePicker();
    showToast('已恢复默认配置', 'success');
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
}

function initSettingsCenter() {
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
            autoSaveConfig();
        });
    }

    switchSettingsSection(localStorage.getItem(SETTINGS_SECTION_KEY) || 'backend');
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
            for (const key of ['videoModel', 'googleFxImageModel', 'videoDuration']) {
                if (key in updated && config[key] !== updated[key]) {
                    config[key] = updated[key];
                    dirty = true;
                }
            }
            if (dirty) {
                // 刷新主界面的相关 UI 选择器
                const fxVideoModelSelect = document.getElementById('settings-fx-video-model');
                if (fxVideoModelSelect) fxVideoModelSelect.value = config.videoModel || '';
                const fxImageModelSelect = document.getElementById('settings-fx-image-model');
                if (fxImageModelSelect) fxImageModelSelect.value = config.googleFxImageModel || '';
                const fxDurationSelect = document.getElementById('settings-fx-video-duration');
                if (fxDurationSelect) fxDurationSelect.value = config.videoDuration || DEFAULT_CONFIG.videoDuration;
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
    
    // （#theme-selector 已移除；旧自定义预设档里残留的 p.theme 直接忽略）

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
    if (typeof markBeatsUserOverridden === 'function') markBeatsUserOverridden();

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
    // （随机主题这一步已随 #theme-selector 移除；场景主题现在由灵感卡片/联网参考决定）

    // 1. Select 1 to 3 random anchors
    const anchorNodes = Array.from(document.querySelectorAll('#anchor-selector .anchor-node'));
    if (anchorNodes.length > 0) {
        anchorNodes.forEach(node => node.classList.remove('active'));
        const numAnchors = Math.floor(Math.random() * 3) + 1;
        const shuffled = [...anchorNodes].sort(() => 0.5 - Math.random());
        for (let i = 0; i < Math.min(numAnchors, shuffled.length); i++) {
            shuffled[i].classList.add('active');
        }
    }
    
    // 2. Randomize sliders
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
    if (typeof markBeatsUserOverridden === 'function') markBeatsUserOverridden();

    saveSelectionState();
    showToast("🎲 随机激发配比已装配！", "success");
}
