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
        // 「视频模型名 → 提示词链路」的规则表由服务端下发，前端只按表匹配显示
        // （见 js/config.js resolveAutoSkillProfile）。在前端硬编码一份 omni 判断，
        // 就是给同一件事留了第二个会漂移的真相源。拿到后重刷一次链路选择器：
        // initServerMode 是异步的，首帧渲染时 auto 的徽标还没有规则可用。
        if (m && Array.isArray(m.skill_profile_rules)) {
            window.SKILL_PROFILE_RULES = m.skill_profile_rules;
        }
        if (m && m.skill_profile_default) {
            window.SKILL_PROFILE_DEFAULT = m.skill_profile_default;
        }
        if (typeof syncIdeationSkillProfilePicker === 'function') {
            syncIdeationSkillProfilePicker();
        }
        // 质量门禁总表（server_common.GATE_SETTINGS）：配置中心的开关面板照它渲染。
        // 与 skill_profile_rules 同一个约定——表在服务端，前端只渲染不复制。
        if (m && Array.isArray(m.gate_settings)) {
            window.GATE_SETTINGS_SPEC = m.gate_settings;
            if (typeof renderGateSettingsPanel === 'function') renderGateSettingsPanel();
        }
        // 服务代码已过期：这个进程仍在跑旧代码，磁盘上已经有更新的核心文件没生效——
        // 常见于"改完代码就直接复跑同一个任务，忘了先重启服务"。不区分改动是否与
        // 本次任务相关：宁可偶尔提示一次不必要的重启，也不要让"修复已经落盘但没生效"
        // 悄悄发生而没人知道（见 server_common.code_staleness_report）。
        if (m && m.runtime_version && m.runtime_version.stale) {
            const rv = m.runtime_version;
            const files = Array.isArray(rv.stale_files) ? rv.stale_files : [];
            const preview = files.slice(0, 5).join('、') + (files.length > 5 ? ` 等 ${files.length} 个文件` : '');
            showToast(
                `⚠️ 服务代码已过期：${preview || '核心文件'}在本次服务启动后被修改过，`
                + `当前进程仍在用旧代码运行。请重启后端服务后再生成，否则可能拿到"看似已修复、实际未生效"的结果。`,
                'error', 15000);
        }
        // 技能契约缺失只劣化生成质量、不影响接口可用性，过去仅写进启动日志——
        // 从浏览器用的人看不到那个终端，等于没有告知。这里提示一次。
        // 两个 profile（base / omni）都要查：只查当前激活的那个，等于把"另一个包
        // 没装好"留到用户切视频模型的那一刻才炸。缺失的那个是不是当前激活的，
        // 决定文案是"生成质量正在降级"还是"切过去就会降级"。
        const reports = (m && Array.isArray(m.skill_contracts) && m.skill_contracts.length)
            ? m.skill_contracts
            : (m && m.skill_contract ? [m.skill_contract] : []);
        for (const sc of reports) {
            if (!sc) continue;
            const isActive = !m.skill_profile || sc.profile === m.skill_profile || !sc.profile;
            const who = `${sc.label || sc.profile || ''}${isActive ? '，当前正在用' : '，切到该模型时才会用'}`;

            if (Array.isArray(sc.missing) && sc.missing.length) {
                console.warn('skill contract missing', sc.profile, sc.dir, sc.source, sc.missing);
                showToast(
                    `⚠️ 技能契约缺失 ${sc.missing.length}/${sc.total} 个文件`
                    + `（${who}）${isActive ? '，生成质量将降级' : ''}。`
                    + `技能包目录：${sc.dir}。`
                    + `请在 server_config.json 的 skillProfiles 里把 "${sc.profile || 'base'}" 指向技能包所在目录（改完不用重启）`,
                    isActive ? 'error' : 'warning', 10000);
            }

            // 文件齐全 ≠ 契约有效。注册表把「SKILL.md 里写的契约」钉到「真正在跑的
            // Python 门禁」上，它缺失或版本对不上，意味着这个包与运行时脱节——照跑
            // 会按错误的契约集合审计，外观上却和一次正常生成一模一样。
            if (sc.registry_status && sc.registry_status !== 'ok') {
                console.warn('skill contract registry', sc.profile, sc.registry_status,
                    sc.contract_version, '->', sc.registry_expected, sc.dir);
                const detail = {
                    missing: `技能包里没有 ${'references/contract-registry.json'}——无法核对契约与门禁是否一致`,
                    unreadable: '契约注册表无法解析（JSON 损坏）',
                    version_mismatch: `契约版本 ${sc.contract_version} 与运行时期望的 ${sc.registry_expected} 不一致`,
                }[sc.registry_status] || `契约注册表状态异常：${sc.registry_status}`;
                showToast(
                    `⚠️ 技能契约注册表异常（${who}）。${detail}。`
                    + `技能包可能与当前代码版本脱节，生成结果仍会产出但审计口径未必正确。`
                    + `技能包目录：${sc.dir}`,
                    isActive ? 'error' : 'warning', 10000);
            }

            // 仓库内置包是与运行时代码同版本的那一份；走到 env/config/default/autodetect
            // 说明正在用一份来源不受版本控制的包，漂移只是时间问题。只提示当前激活的，
            // 免得两个 profile 各弹一条把真正的问题淹掉。
            if (isActive && sc.source && sc.source !== 'vendored') {
                console.warn('skill package is not the vendored copy', sc.profile, sc.source, sc.dir);
                showToast(
                    `ℹ️ 正在使用非仓库内置的技能包（${who}，来源 ${sc.source}）。`
                    + `仓库内置那份才与当前代码同步版本化；外部目录更新不及时会让契约与门禁悄悄分叉。`
                    + `技能包目录：${sc.dir}`,
                    'warning', 8000);
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
    initDebugLimitControls();
    initCoverBurnControl();
    resumeActiveTaskIfExists();
    resumeActiveBackgroundTasksIfExists();
    startGlobalTasksBadgePolling();
    initPacingSkeletonSelector();
    loadIdeationCards();
    initBeatOutlineModal();
    updateDrawerTopOffset();
    window.addEventListener('resize', window._debounce(updateDrawerTopOffset, 150));
    const refreshBtn = document.getElementById('ideate-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            loadIdeationCards(true);
        });
    }
    const countSelect = document.getElementById('ideation-count-select');
    if (countSelect) {
        const savedCount = localStorage.getItem('ideation_card_count');
        if (savedCount) countSelect.value = savedCount;
        countSelect.addEventListener('change', () => {
            localStorage.setItem('ideation_card_count', countSelect.value);
        });
    }
    initLocalServiceLogs();
});

// Function saveSelectionState moved to modular JS file

// Function loadSelectionState moved to modular JS file

// Function updateConfigSummary moved to modular JS file

// 各预设原本还带一个 theme 字段（glacier_cave / water_tower / submarine_cabin /
// hollow_oak），指向已移除的固定基础场景主题选择器里的按钮 id。#theme-selector 不在
// DOM 里之后 applyPreset 里那段选中逻辑必然空转，字段本身也就成了纯死数据，一并删掉。
const PRESETS = {
    nature_wonder: {
        anchors: ['water_glass_floor', 'bioluminescent_moss'],
        complexity: 2,
        budget: 2,
        ratio: 30,
        creativity: 3,
        beats: 5
    },
    industrial_relic: {
        anchors: ['single_slab_counter', 'living_wood_stair'],
        complexity: 3,
        budget: 2,
        ratio: 60,
        creativity: 2,
        beats: 8
    },
    retired_vehicle: {
        anchors: ['carrier_cutout_window', 'single_slab_counter'],
        complexity: 3,
        budget: 3,
        ratio: 75,
        creativity: 3,
        beats: 10
    },
    contrast_novelty: {
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

// 页脚 LLM 芯片组收回折叠态（仅窄屏有意义；桌面端 picker-collapsed 不生效，
// 调了也不会有视觉变化）。config.js 选完模型后回调这里，见 syncIdeationLlmPicker。
function collapseIdeationPickerOnMobile() {
    if (!window.matchMedia || !window.matchMedia('(max-width: 768px)').matches) return;
    const picker = document.getElementById('ideation-model-picker');
    if (picker) picker.classList.add('picker-collapsed');
    const toggle = document.getElementById('ideation-llm-toggle');
    if (toggle) {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.title = '展开模型选择';
    }
}

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
        projects: document.getElementById('panel-projects'),
        gallery: document.getElementById('panel-gallery'),
        ledger: document.getElementById('panel-ledger'),
        replica: document.getElementById('panel-replica'),
    };
    const buttons = {
        config: document.getElementById('main-tab-config'),
        results: document.getElementById('main-tab-results'),
        image: document.getElementById('main-tab-image'),
        projects: document.getElementById('main-tab-projects'),
        gallery: document.getElementById('main-tab-gallery'),
        ledger: document.getElementById('main-tab-ledger'),
        replica: document.getElementById('main-tab-replica'),
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
    // 项目工作台：进入时拉一次并开始轮询，离开时立刻停表。轮询节奏由
    // js/projects.js 按"有没有项目在跑"自己决定（4s / 30s），离开页面还接着
    // 空转就是旧任务抽屉那种恒定 2.5s 全量轮询的老毛病。
    if (typeof projectsTabEntered === 'function') {
        if (tab === 'projects') projectsTabEntered();
        else if (typeof projectsTabLeft === 'function') projectsTabLeft();
    }
    // 爆款复刻：进入时拉一次任务列表。没有轮询——这条线的长耗时阶段全部走 SSE，
    // 停在人工卡点后页面是静止的，轮询只会白烧请求。
    if (tab === 'replica' && typeof replicaTabEntered === 'function') {
        replicaTabEntered();
    }
}

// 结果页的 tab 白名单。「校验与质量审核」页已并入概览页顶部的状态条，但老用户的
// localStorage 里可能还存着 'audit'；不回退的话会出现三个都不高亮、内容区全空的死状态。
const RESULT_TAB_IDS = ['overview', 'prompts'];

function switchTab(tabId) {
    if (!RESULT_TAB_IDS.includes(tabId)) tabId = 'overview';
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

// 实况终端的写入必须按帧合批，不能按 chunk 直写。compose 是流式的，
// text_chunk 事件在一次激发里能来几千到上万条（LLM 逐 token 推），每条都
// createTextNode + insertBefore 的话：① 主线程在整个生成期间被 DOM 写入占满，
// 点按钮/切标签/滚动全部排在后面，就是"交互延迟高"的直接来源；② 文本节点
// 只增不减，跑到后半程终端里挂着几万个节点，之后每次滚动都要重新布局这一大坨。
// 现在：chunk 先进字符串缓冲，一帧最多落一次 DOM；同时把终端内容裁到
// LIVE_TERMINAL_MAX_CHARS，只留最近的一段（往上翻的是完整日志 dock 的活，
// 这里本来就只是"看着它在动"）。
const LIVE_TERMINAL_MAX_CHARS = 20000;
let _terminalBuffer = '';
let _terminalFlushPending = false;
let _terminalText = '';

function flushLiveTerminal() {
    _terminalFlushPending = false;
    const body = document.getElementById('live-terminal-body');
    if (!body) { _terminalBuffer = ''; return; }
    if (_terminalBuffer) {
        _terminalText += _terminalBuffer;
        _terminalBuffer = '';
        if (_terminalText.length > LIVE_TERMINAL_MAX_CHARS) {
            // 从行首截断，别把一行劈成半截
            const cut = _terminalText.length - LIVE_TERMINAL_MAX_CHARS;
            const nl = _terminalText.indexOf('\n', cut);
            _terminalText = _terminalText.slice(nl >= 0 ? nl + 1 : cut);
        }
        const cursor = body.querySelector('.terminal-cursor');
        body.textContent = _terminalText;
        if (cursor) body.appendChild(cursor);
    }
    body.scrollTop = body.scrollHeight;
}

function appendLiveTerminal(chunk) {
    if (!chunk) return;
    _terminalBuffer += chunk;
    if (!_terminalFlushPending) {
        _terminalFlushPending = true;
        requestAnimationFrame(flushLiveTerminal);
    }
}

function resetLiveTerminal() {
    _terminalBuffer = '';
    _terminalText = '';
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
        resetLiveTerminal();
        appendLiveTerminal("[SYSTEM] Initializing creative idea engine...\n[SYSTEM] Loading skill contract...\n");
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
    if (typeof refreshProjects === 'function') refreshProjects({ assets: false });
}

/* ==========================================================================
   创意库写入路径
   --------------------------------------------------------------------------
   历史上这里是 saveLibrary()：「整表覆盖」——客户端持有完整数组、整份 POST 回
   /api/library。代价是每次改动都要上传全库（实测单条创意 164KB），而且服务端
   为了防住这个动作本身挂了三道闸门（空库拒写 / 槽位不自洽 / 未声明缩量 409），
   用户日常撞到的就是那句"保存失败，请刷新页面后重试"。

   2026-07-31（P1/P4）整表写入已彻底移除，全部改走 /api/library/item 与
   /api/library/item/delete：一次只碰一条记录，服务端只写它自己的正文文件 +
   索引行。因此：
     · 不会碰到别的记录，"整库清零 / 未声明缩量"在结构上不可能发生；
     · 删掉库里最后一条也不会被 409（老路径会撞上"空列表覆盖非空库"防护）；
     · 调用方不必再声明"这次缩量是我有意为之"。
   见 docs/project_workbench_refactor_plan.md
   ========================================================================== */

// 单条写入服务端。返回 true/false 表示服务端是否接受。
async function persistIdeaItem(idea) {
    if (!idea || idea.id === undefined || idea.id === null || idea.id === '') {
        console.warn('[library] 缺少 id，无法单条写入', idea);
        return false;
    }
    // localStorage 仍留一份整库镜像作为服务器写失败时的兜底
    try {
        localStorage.setItem('spark_library', JSON.stringify(savedIdeas));
    } catch (e) {
        console.warn('[library] localStorage 镜像写入失败（不影响服务器写入）', e);
    }
    try {
        const res = await fetch('/api/library/item', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ item: idea }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status !== 'success') {
            throw new Error(data.message || `HTTP ${res.status}`);
        }
        return true;
    } catch (e) {
        console.warn('[library] 单条写入失败', e);
        if (typeof showToast === 'function') {
            showToast(`收藏只存到了浏览器本地（服务器写入失败：${e.message}）`, 'error');
        }
        return false;
    }
}

// 单条删除。服务端顺带清掉这条创意生成的图片/视频文件，所以不必再单独打一次
// /api/library/delete_item。
async function deleteIdeaItem(idea) {
    try {
        const res = await fetch('/api/library/item/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: idea.id,
                title: getIdeaSaveTitle(idea),
                covers: idea.covers || [],
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status !== 'ok') {
            throw new Error(data.message || `HTTP ${res.status}`);
        }
        return true;
    } catch (e) {
        console.error('[library] 单条删除失败', e);
        if (typeof showToast === 'function') {
            showToast(`删除失败：${e.message}`, 'error');
        }
        return false;
    }
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
    const beatCountMode = document.getElementById('beat-count-mode');

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
    // 只挂 'change'：拖动松手 / ± 步进（上面补发过 change）才算用户拍板，
    // 读档与「载入灵感卡片」的回填只发 'input'，不会误置这个标记。
    beats.addEventListener('change', () => {
        if (typeof markBeatsUserOverridden === 'function') markBeatsUserOverridden();
    });
    const syncBeatModeLabel = () => {
        const adaptive = !beatCountMode || beatCountMode.value !== 'fixed';
        const label = document.getElementById('beat-count-label');
        // adaptive 下滑块只是额度闸门（骨架由灵感卡片定义，滑块压不动它的下界）；
        // fixed 下它才真的定义拍数。两态文案必须分开，否则用户会把上限读成承诺。
        // 无论哪一态，这个数字都只管施工拍——过门/穿越运镜由后端按拓扑展开成
        // 3~5 个额外的子拍，不占用这里设置的额度（2026-08-06，之前 title 只在
        // HTML 里写死了 adaptive 的说明，切到 fixed 也不会跟着换，文案和实际
        // 交付的总拍数对不上）。
        if (label) {
            label.textContent = adaptive ? '节拍上限 · 额度闸门 (Budget Cap)' : '固定施工节拍数 (Beat Count)';
            label.title = adaptive
                ? '自适应模式下这是额度上限（最多生成多少施工拍），不是承诺的拍数；拍数下界由灵感卡片的工序清单决定。过门/穿越运镜另计，不占用这个额度。'
                : '固定模式下这是施工拍的精确拍数。过门/穿越运镜由过门逻辑按入口/出口拓扑另外展开成 3~5 个子拍，不占用这里设置的施工拍数，所以最终交付的总拍数会比这个数字多。';
        }
        updateConfigSummary();
    };
    if (beatCountMode) {
        beatCountMode.addEventListener('input', syncBeatModeLabel);
        beatCountMode.addEventListener('change', () => {
            syncBeatModeLabel();
            if (typeof markBeatsUserOverridden === 'function') markBeatsUserOverridden();
        });
    }

    // Fire initial displays
    complexity.dispatchEvent(new Event('input'));
    budget.dispatchEvent(new Event('input'));
    ratio.dispatchEvent(new Event('input'));
    creativity.dispatchEvent(new Event('input'));
    beats.dispatchEvent(new Event('input'));
    syncBeatModeLabel();
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

// 「任务列表」「点子库」两个右侧抽屉及其开合函数已于 2026-07-31（P4）删除，
// 内容合并进「📁 项目」主标签页（js/projects.js）。右侧现在只剩日志 dock 一个
// 停靠物，不再需要三者互斥的那套协调逻辑。

// 日志 dock 是 position:fixed 的，`top: 0` 会盖住 header（含它自己的收起按钮）。
// 这里按真实的 header+标签栏高度锚定它，而不是写死像素——那个高度随断点变化。
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
    
    // 配置中心的分区导航 + 改动即存的委托绑定（见 js/config.js）
    if (typeof initSettingsCenter === 'function') initSettingsCenter();

    openSettings.addEventListener('click', () => {
        settingsModal.classList.add('active');
        // 回到上次停留的分区；进「生成号池」时顺带重读池子
        if (typeof switchSettingsSection === 'function') {
            switchSettingsSection(localStorage.getItem('spark_settings_section') || 'backend');
        }
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

    // 项目工作台入口。header 上原先是「任务列表」「点子库」两个抽屉切换按钮，
    // 现在合并成这一个（见 index.html #open-projects-btn）——两个抽屉的内容都
    // 搬进了 #panel-projects 主标签页。点它时若已有任务在跑就直接落到"运行中"
    // 筛选，那正是用户点角标时想看的东西。
    const openProjectsBtn = document.getElementById('open-projects-btn');
    if (openProjectsBtn && typeof openProjectsWorkbench === 'function') {
        openProjectsBtn.addEventListener('click', () => {
            const badge = document.getElementById('active-task-count');
            const hasRunning = badge && badge.style.display !== 'none';
            openProjectsWorkbench(hasRunning ? 'running' : 'all');
        });
    }

    // 抽屉里那两个「清空已完成 / 清空失败」搬到了项目工作台工具栏；筛选与搜索
    // 由 js/projects.js 的 chips 接管，不再需要这里的三个绑定。
    const clearCompletedBtn = document.getElementById('projects-clear-completed-btn');
    const clearFailedBtn = document.getElementById('projects-clear-failed-btn');
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

    // Manual Intervention Banner dismiss（人工已经处理完/暂时不想管了，先手动关掉；
    // 如果脚本仍在等待，检测到清除状态或超时时后端事件还是会再次驱动它）
    const manualInterventionDismissBtn = document.getElementById('manual-intervention-dismiss-btn');
    if (manualInterventionDismissBtn) {
        manualInterventionDismissBtn.addEventListener('click', () => hideManualInterventionBanner());
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

    // 取消按钮只作用于「当前正看着的这个创意」的后台任务——每个创意的任务
    // 各自登记在 ideaTasksById 里，取消动作也必须按创意 id 定位，不能再用全局单例控制器。
    const cancelFramesBtn = document.getElementById('cancel-frames-btn');
    if (cancelFramesBtn) {
        cancelFramesBtn.addEventListener('click', () => {
            // 光 abort() 前端的 SSE 读取只是让浏览器不再看这个任务的进度——后端
            // worker 线程完全不知道、会继续把整条上游重试退避链跑完（卡在限流
            // 重试里可以是分钟级）。必须先把取消请求发到服务端让它真正停下来。
            const rec = currentIdea && getIdeaTaskRecord(currentIdea.id, 'frames');
            if (rec && rec.taskId) {
                fetch('/api/compose-cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: rec.taskId })
                }).catch(e => console.error("Failed to cancel frames task on server:", e));
            }
            if (rec && rec.controller) rec.controller.abort();
            showToast(rec && rec.taskId ? "已发送取消请求，帧序列生成即将停止" : "已取消帧序列生成", "info");
        });
    }

    const cancelVideosBtn = document.getElementById('cancel-videos-btn');
    if (cancelVideosBtn) {
        cancelVideosBtn.addEventListener('click', () => {
            // 见 cancel-frames-btn 的同款说明。
            const rec = currentIdea && getIdeaTaskRecord(currentIdea.id, 'videos');
            if (rec && rec.taskId) {
                fetch('/api/compose-cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: rec.taskId })
                }).catch(e => console.error("Failed to cancel videos task on server:", e));
            }
            if (rec && rec.controller) rec.controller.abort();
            showToast(rec && rec.taskId ? "已发送取消请求，视频序列生成即将停止" : "已取消视频序列生成", "info");
        });
    }

    // 手动上传视频覆盖槽位：各卡片的「上传」按钮点击时先把目标槽位存进
    // input.dataset.slot 再触发这个共用的隐藏 <input type=file>，选完文件后这里
    // 统一读取并调用 uploadVideoToSlot；选完清空 value，允许连续两次选同一个文件。
    const videoUploadInput = document.getElementById('video-upload-input');
    if (videoUploadInput) {
        videoUploadInput.addEventListener('change', (e) => {
            const file = e.target.files && e.target.files[0];
            const slot = parseInt(videoUploadInput.dataset.slot, 10);
            videoUploadInput.value = '';
            if (file && Number.isFinite(slot)) {
                uploadVideoToSlot(slot, file);
            }
        });
    }

    // 手动上传图片覆盖帧槽位：同上的共用隐藏 <input type=file>，由各帧卡片的
    // 「上传」按钮触发。多选时走与"拖多张图进来"同一条路径（uploadFramesFromDrop：
    // 按文件名顺序从目标帧起依次填、超出槽位的丢弃并告知），不另起一套语义。
    const frameUploadInput = document.getElementById('frame-upload-input');
    if (frameUploadInput) {
        frameUploadInput.addEventListener('change', (e) => {
            const files = Array.from((e.target && e.target.files) || []);
            const seq = parseInt(frameUploadInput.dataset.seq, 10);
            frameUploadInput.value = '';
            if (!files.length || !Number.isFinite(seq)) return;
            // accept="image/*" 只是筛选器，部分系统的文件对话框允许绕过它选任意文件
            const images = files.filter(isImageFileLike);
            if (!images.length) {
                showToast(`「${files[0].name}」不是图片文件，帧槽位只接受图片`, 'error');
                return;
            }
            uploadFramesFromDrop(seq, images);
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
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity', 'slider-beats', 'beat-count-mode'].forEach(id => {
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

    // 管线条与 section 弹层：必须排在下面那批按钮监听之前绑定，管线条用的是
    // 捕获阶段拦截，绑定先后本身不影响，但放这里读起来跟 tab 一组更顺。
    initPipelineBar();
    initSectionPops();

    // Idea Interaction Buttons
    document.getElementById('save-idea-btn').addEventListener('click', saveCurrentIdea);
    document.getElementById('export-idea-btn').addEventListener('click', exportIdeaMarkdown);
    document.getElementById('copy-prompt-btn').addEventListener('click', copyPromptToClipboard);
    // 提示词页的手动编辑（✏️ 手动编辑 / ➕ 添加一拍 / 保存 / 取消），见 js/prompt_editor.js
    if (typeof initPromptEditor === 'function') initPromptEditor();
    document.getElementById('copy-prompt-btn-all').addEventListener('click', copyPromptToClipboard);
    document.getElementById('copy-tiktok-meta-btn').addEventListener('click', copyTikTokMetaToClipboard);
    document.getElementById('copy-tiktok-meta-cn-btn').addEventListener('click', copyTikTokMetaCnToClipboard);
    document.getElementById('gen-project-meta-btn')?.addEventListener('click', generateProjectMetaForCurrentIdea);
    // 手机端标题区折叠开关：折叠态只留一行英文主标题（话题串与中文标题行藏起来），
    // 让封面/帧序列能上首屏。class 常驻 DOM，桌面端的 CSS 不理会它，所以按钮本身
    // 也只在 ≤768px 显示。
    const ideaMetaToggle = document.getElementById('idea-meta-toggle');
    if (ideaMetaToggle) {
        ideaMetaToggle.addEventListener('click', () => {
            const header = document.getElementById('idea-content-header');
            if (!header) return;
            const collapsed = header.classList.toggle('meta-collapsed');
            ideaMetaToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            ideaMetaToggle.title = collapsed ? '展开完整标题与话题' : '收起标题与话题';
        });
    }
    // 手机端页脚 LLM 模型选择器的折叠开关：折叠态只留「LLM 模型 · 使用中 xxx」一行，
    // 展开才铺开全部芯片。桌面端 CSS 不理会 picker-collapsed，按钮本身也不显示。
    const llmToggle = document.getElementById('ideation-llm-toggle');
    if (llmToggle) {
        llmToggle.addEventListener('click', () => {
            const picker = document.getElementById('ideation-model-picker');
            if (!picker) return;
            const collapsed = picker.classList.toggle('picker-collapsed');
            llmToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            llmToggle.title = collapsed ? '展开模型选择' : '收起模型选择';
        });
    }
    document.getElementById('make-cover-btn').addEventListener('click', () => generateCover());
    document.getElementById('generate-frames-btn').addEventListener('click', () => generateFrames());
    document.getElementById('run-sequence-review-btn').addEventListener('click', () => runSequenceReview());
    const fullReviewBtn = document.getElementById('run-full-sequence-review-btn');
    if (fullReviewBtn) {
        fullReviewBtn.addEventListener('click', async () => {
            // 全量重审要烧掉整套调用，先说清代价再跑
            const ok = await customConfirm(
                '全量重审会把每一拍与跨帧层整个重跑一遍，不复用任何既有结论——'
                + '十几帧的单子通常是几分钟、几十次模型调用。<br><br>'
                + '日常修完帧之后用「🔍 一致性审查」就够：它只重审帧图变过的那几拍。');
            if (ok) runSequenceReview('full');
        });
    }
    const deletedSlotsBtn = document.getElementById('deleted-slots-btn');
    if (deletedSlotsBtn) deletedSlotsBtn.addEventListener('click', () => openDeletedSlotsPanel());
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
    // 点子库的搜索/排序已由项目工作台的 chips + 搜索框接管（js/projects.js）
    const exportBtn = document.getElementById('export-all-btn');
    if (exportBtn) exportBtn.addEventListener('click', exportAllLibrary);
    
    const importBtn = document.getElementById('import-btn');
    const importFile = document.getElementById('import-file');
    importBtn.addEventListener('click', () => importFile.click());
    importFile.addEventListener('change', importLibrary);

    // 同一排工具条上的「📥 上传提示词集」：导入一份外部提示词集 = 新建一个项目
    // （格式不合槽位契约时自动补全），见 js/prompt_import.js
    if (typeof initPromptImport === 'function') initPromptImport();
}

// --- 任务状态：只剩 header 角标这一路 -------------------------------------
// renderTasks / 抽屉筛选状态 / _lastTasksRenderHtml / taskModelOptions /
// formatTaskDuration 已于 2026-07-31（P4）随「激发任务列表」抽屉一并删除，
// 任务的展示与操作全部由「📁 项目」主标签页承担（js/projects.js）。
// 这里只保留两样别处还在用的东西：
//   · MEDIA_TASK_TYPES / isIdeationTask —— openSparkProject 解析激发任务时要用它
//     排除帧/视频/封面子作业（误配上去会载入一份没有提示词的空壳结果）；
//   · 下面的 header 角标轮询 —— 它跟当前在哪个标签页无关，必须独立跑。
const MEDIA_TASK_TYPES = new Set(['frames', 'staged_render', 'videos', 'cover']);
const isIdeationTask = (t) => !MEDIA_TASK_TYPES.has((t.dimensions && t.dimensions.type) || 'idea');

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
                collage_url: data.collage_url || '',
                project_key: data.project_key || null
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
            // 从任务卡片查看历史成功任务时，优先展示封面、帧与视频等结果概览；
            // 提示词仍可通过相邻标签进入。新任务刚生成完成时的落点保持不变。
            switchTab('overview');
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
            setTimeout(() => { if (typeof refreshProjects === 'function') refreshProjects({ assets: false }); }, 500);
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
            
            if (typeof refreshProjects === 'function') refreshProjects();
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

    switchMainTab('results');
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

async function rerunCompletedTask(taskId, dimensions, event) {
    if (event) event.stopPropagation();
    // 模型选择器现在长在项目工作台的详情栏里（原先是任务抽屉的成功卡，
    // 抽屉已随 P4 删除）。取不到就退回当前全局模型。
    const modelSelect = document.getElementById('projects-rerun-model');
    const selectedModel = (modelSelect && modelSelect.value) || config.model || DEFAULT_CONFIG.model;

    // 这里的模型选择同时成为后续激发的全局模型，保证在线检测、页脚当前模型提示
    // 和本次请求使用同一条网关；新任务不复用旧 id，保留原成功结果供对比。
    config.model = selectedModel;
    localStorage.setItem('spark_config', JSON.stringify(config));
    if (typeof syncIdeationLlmPicker === 'function') syncIdeationLlmPicker();
    switchMainTab('results');
    showToast(`正在使用 ${selectedModel} 重新激发，原结果已保留...`, 'info');

    try {
        await generateIdea({
            dimensions: { ...dimensions },
            config: { ...config }
        });
    } catch (e) {
        console.error('Rerun completed task failed:', e);
        showToast(`重新激发失败: ${e.message}`, 'error');
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
            if (typeof refreshProjects === 'function') refreshProjects();
        } else {
            showToast("清空任务失败", "error");
        }
    } catch (e) {
        console.error("Clear tasks request failed:", e);
        showToast("请求清空失败", "error");
    }
}

// renderTasks / startTasksPolling / stopTasksPolling 已随任务抽屉删除（P4）
window.viewTask = viewTask;
window.loadCompletedTask = loadCompletedTask;
window.cancelTask = cancelTask;
window.deleteTask = deleteTask;
window.retryTask = retryTask;
window.rerunCompletedTask = rerunCompletedTask;
window.generateIdea = generateIdea;
window.clearTasks = clearTasks;

// Compose the full skill-grade prompt set via the server-side skill shell.
// The GUI only collects dimensions; the server runs them through the real
// gemini-veo-restoration-composer contract and relays to the local LLM proxy.
// Compose the full skill-grade prompt set via the server-side skill shell.
// The GUI only collects dimensions; the server runs them through the real
// gemini-veo-restoration-composer contract and relays to the local LLM proxy.
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

    // 基础场景主题选择器已移除：非重试路径的选题只能来自选中过的灵感卡片（联网参考
    // 驱动，点卡片或点卡片上的「🔨 节拍简介」都会载入维度）。没载入过就没有可合成的
    // 主题，在切换视图前先拦下。
    const loadedIdea = (typeof loadedIdeationCover !== 'undefined' && loadedIdeationCover) ? loadedIdeationCover : null;
    if (!retryParams && (!loadedIdea || !loadedIdea.input_str)) {
        showToast('请先点选灵感推荐卡片（或卡片上的「🔨 节拍简介」）选定选题，也可直接在卡片上一键合成', 'error');
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
            topic_dna: loadedIdea.topic_dna || null,
            llm_score: loadedIdea.llm_score ?? null,
            // 台账登记跟随真正的激发请求；仅浏览/载入灵感卡片不会入账。
            ledger_candidate: {
                dna: loadedIdea.topic_dna || null,
                title: loadedIdea.task_label || null,
                score: loadedIdea.llm_score ?? null,
                creative_seed: loadedIdea.creative_seed || null
            },
            // 联网参考案例库使用计次：与卡片「一键合成」路径对齐透传，让选中卡片载入维度
            // 后走主生成按钮的合成同样能在真正借鉴时计次（见 server.py /api/compose）
            trend_ref: loadedIdea.trend_ref || null,
            trend_ref_ids: loadedIdea.trend_ref_ids || [],
            beat_outline: Array.isArray(loadedIdea.beat_outline)
                ? loadedIdea.beat_outline.slice()
                : [],
            pacing_skeleton: loadedIdea.pacing_skeleton || 'linear_milestone',
            anchors: activeAnchors,
            complexity: document.getElementById('val-complexity').textContent,
            budget: document.getElementById('val-budget').textContent,
            ratio: document.getElementById('val-ratio').textContent,
            creativity: document.getElementById('val-creativity').textContent,
            beats_count: parseInt(document.getElementById('slider-beats').value, 10),
            // 载入卡片后走主生成按钮的这条路径也要带上拍数下界，否则它和卡片
            // 「一键合成」会落在两个不同的区间里（同一张卡走两条路拿到不同拍数）。
            // 滑块只压上限；下界由卡片的工序清单决定，见 compute_beats_floor。
            beats_floor: Number.isFinite(+(loadedIdea.beats_floor)) ? +loadedIdea.beats_floor : null,
            beat_count_mode: (document.getElementById('beat-count-mode') || {}).value || 'adaptive'
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
    // text_chunk 是逐 token 来的，但它对进度只贡献"还活着"这一个信息，百分比
    // 在整个流式阶段基本不动。每个 token 都跑一遍 normalizeGenerationProgress
    // （克隆一次 state）+ 三次 DOM 写入纯属浪费，一帧一次足够。
    let composeChunkTick = false;
    const applyComposeChunkProgress = () => {
        if (composeChunkTick) return;
        composeChunkTick = true;
        requestAnimationFrame(() => {
            composeChunkTick = false;
            if (isCurrent()) applyComposeProgress('text_chunk', null);
        });
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
                    applyComposeChunkProgress();
                } else if (type === 'reconnecting') {
                    const stageText = document.getElementById('loading-stage-text');
                    if (stageText) stageText.textContent = `与服务的连接中断，正在自动重连（第 ${data.attempt} 次）...`;
                } else if (type === 'result' || type === 'error') {
                    applyComposeProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;

        if (watch.status === 'disconnected') {
            // 与服务器失联 ≠ 任务失败：合成线程是独立线程，跟客户端有没有人看
            // 毫无关系，会继续跑到底。不清 localStorage 的 active_task_id——
            // 保留它，下次刷新页面 resumeActiveTaskIfExists 才能重新接上这条
            // 任务的事件流，而不是让它在客户端彻底失踪、之后又"凭空"冒出结果。
            stopLoadingTimer();
            if (currentGenerationController === controller) currentGenerationController = null;
            const stageText = document.getElementById('loading-stage-text');
            if (stageText) stageText.textContent = watch.error;
            showToast(watch.error, 'warning');
            return;
        }

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
            social_title_cn: data.social_title_cn || '',
            project_key: data.project_key || null
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
 * 事件重放/重连会对同一槽位重复触发——renderSlotCard 是幂等的整格重画，
 * 且卡片上不绑任何 click（点击走网格级委托，见 js/slot_card.js），
 * 所以重复调用不会像旧实现那样往同一元素上堆叠监听器。
 *
 * 缓存版本已由 applyFrameEventToIdea（数据合并的唯一入口）在本次渲染前递增，
 * renderSlotCard 内部用 bust=false 直接取到新版本号；两处各自 bust 会让同一
 * 张新图被拉两次。
 */
function updateFrameSlotCard(f) {
    if (!f) return;
    const busy = typeof isIdeaTaskActive === 'function' && currentIdea
        ? isIdeaTaskActive(currentIdea.id, 'frames') : false;
    renderSlotById('image', f.sequence, frameSlotState(f, { seq: f.sequence, busy }));
}

/* ── 帧序列实时生成动态 ─────────────────────────────────────────────
   生成过程直接在「连续帧序列生成」模块内滚动直播（逐帧渲染/质检结论/重试原因），
   不必打开任务列表。只增量追加行、贴底才自动跟随，行数上限 300。
   2026-07-15 多创意后台任务改造：行数据先写进该创意在 ideaTasksById 里的
   TaskRecord.feedLines 缓冲区（无论用户是否正看着这个创意），只有正停留在
   这个创意页面时才顺带画进 DOM——这样切到另一个创意不会看到串台的动态行，
   切回来时也能从缓冲区完整回放，而不是"谁最后发起任务谁独占这块面板"。 */
function _framesFeedAppendDom(text, cls, atDate) {
    const lines = document.getElementById('frames-live-feed-lines');
    if (!lines) return;
    const wrap = document.getElementById('frames-live-feed');
    if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
    const nearBottom = lines.scrollHeight - lines.scrollTop - lines.clientHeight < 60;
    const d = atDate || new Date();
    const p = n => String(n).padStart(2, '0');
    const line = document.createElement('div');
    line.className = 'gen-feed-line' + (cls ? ` ${cls}` : '');
    const safeText = escapeHtml(String(text).length > 220 ? String(text).slice(0, 220) + '…' : String(text));
    line.innerHTML = `<span class="gen-feed-time">[${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}]</span> ${safeText}`;
    lines.appendChild(line);
    while (lines.children.length > 300) lines.removeChild(lines.firstChild);
    if (nearBottom) lines.scrollTop = lines.scrollHeight;
}

function framesFeedSetLive(ideaId, isLive) {
    const rec = getIdeaTaskRecord(ideaId, 'frames');
    if (rec) rec.live = !!isLive;
    if (!isViewingIdea(ideaId)) return;
    const dot = document.getElementById('frames-feed-dot');
    if (dot) dot.classList.toggle('active', !!isLive);
}

function framesFeedReset(ideaId, introText) {
    if (isViewingIdea(ideaId)) {
        const wrap = document.getElementById('frames-live-feed');
        const lines = document.getElementById('frames-live-feed-lines');
        if (wrap && lines) {
            lines.innerHTML = '';
            wrap.style.display = 'block';
        }
    }
    framesFeedSetLive(ideaId, true);
    if (introText) framesFeedLine(ideaId, introText);
}

function framesFeedLine(ideaId, text, cls) {
    const rec = getIdeaTaskRecord(ideaId, 'frames');
    const atDate = new Date();
    if (rec) {
        rec.feedLines.push({ text, cls, time: atDate });
        while (rec.feedLines.length > 300) rec.feedLines.shift();
    }
    if (isViewingIdea(ideaId)) _framesFeedAppendDom(text, cls, atDate);
}

/** 把这个创意缓冲区里已经攒下的动态行整批回放进 DOM——切回它正在生成的页面时调用。 */
function framesFeedHydrate(ideaId) {
    const rec = getIdeaTaskRecord(ideaId, 'frames');
    const wrap = document.getElementById('frames-live-feed');
    const lines = document.getElementById('frames-live-feed-lines');
    if (!wrap || !lines) return;
    if (!rec) {
        wrap.style.display = 'none';
        return;
    }
    lines.innerHTML = '';
    wrap.style.display = 'block';
    rec.feedLines.forEach(l => _framesFeedAppendDom(l.text, l.cls, l.time));
    const dot = document.getElementById('frames-feed-dot');
    if (dot) dot.classList.toggle('active', !!rec.live);
}

// 渲染期不做任何视觉判定（2026-08-05 起生成期一致性审查已整体移除），所以新渲的帧
// 一律是 'pending_manual_review'。其余取值只会出现在旧 manifest 或手动一致性审查/
// 人工标记的结果里。isIsolatedRetry 保留在签名上供调用方传参，两条路径现在文案一致。
function framesFeedQualityLine(ideaId, f, isIsolatedRetry) {
    if (!f) return;
    const seq = String(f.sequence || 0).padStart(3, '0');
    const gate = f.quality_gate;
    const reason = typeof f.vlm_qa_reason === 'string' ? f.vlm_qa_reason : '';
    if (gate === 'auto_approved') {
        if (reason.indexOf('WARN') === 0) {
            framesFeedLine(ideaId, `✅ IMG ${seq} 完成（宽松放行留痕：${reason.replace(/^WARN:?\s*/, '')}）`, 'warn');
        } else {
            framesFeedLine(ideaId, `✅ IMG ${seq} 完成（质检通过）`, 'ok');
        }
    } else if (gate === 'auto_approved_degraded') {
        framesFeedLine(ideaId, `⚠️ IMG ${seq} 已放行（判定服务异常 fail-open：帧已渲染但未经核验，可单帧重试复检）`, 'warn');
    } else if (gate === 'vlm_qa_failed' || gate === 'sequence_review_flagged') {
        framesFeedLine(ideaId, `❌ IMG ${seq} 一致性审查未通过（${reason || '原因未知'}），已保留末次渲染结果，可点击「修复此帧问题」定向修复`, 'err');
    } else if (gate === 'manual_flagged') {
        const manual = typeof f.manual_issue === 'string' ? f.manual_issue : '';
        framesFeedLine(ideaId, `📝 IMG ${seq} 已被人工标记问题（${manual || '未填写描述'}），可点击「修复此帧问题」定向修复`, 'warn');
    } else if (gate === 'sequence_reviewed_pass') {
        framesFeedLine(ideaId, `✅ IMG ${seq} 完成（一致性审查通过）`, 'ok');
    } else if (gate === 'pending_manual_review') {
        framesFeedLine(ideaId, `✅ IMG ${seq} 完成（渲染不做审查，如需复核请在帧网格点「一致性审查」）`, 'ok');
    } else {
        framesFeedLine(ideaId, `✅ IMG ${seq} 完成`, 'ok');
    }
}

async function streamFramesProgress(taskId, ownerIdea, targetSequences) {
    ownerIdea = ownerIdea || currentIdea;
    if (!ownerIdea) return;
    const ownerId = ownerIdea.id;
    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    const grid = slotRenderTarget('image');
    if (!btn || !progress || !meta || !grid) return;

    // 同一个创意若已有一条帧序列监听在跑（理论上不该发生，generateFrames 已挡住），
    // 让新的接管旧的，避免两个 watcher 同时往同一份 record 里写。
    const existingRec = getIdeaTaskRecord(ownerId, 'frames');
    if (existingRec && existingRec.controller) {
        try { existingRec.controller.abort(); } catch (_) { /* noop */ }
    }
    const controller = new AbortController();
    const rec = beginIdeaTask(ownerId, 'frames', taskId, controller);
    // 调试模式（仅生成前 N 帧）：标记本次任务的目标槽位范围，renderFramesForIdea
    // 靠这个字段区分"还没轮到（等待中）"和"这次任务压根没请求（正常缺帧）"，
    // 见 retrySingleFrame 里的同款说明与 2026-07-20 的事故复盘。
    if (targetSequences && targetSequences.length) rec.targetSequences = targetSequences;
    const isCurrent = () => isIdeaTaskCurrent(ownerId, 'frames', taskId);
    const isViewing = () => isViewingIdea(ownerId);
    const setMeta = (text) => { rec.meta = text; if (isViewing()) meta.textContent = text; };
    const titleTag = () => isViewing() ? '' : `「${ownerIdea.title || '创意'}」`;

    if (isViewing()) {
        btn.disabled = true;
        progress.style.display = 'flex';
    }
    setMeta('连接帧生成事件流...');

    const applyFramesProgress = (type, data) => {
        if (!window.ProgressModel) return null;
        const info = ProgressModel.normalizeGenerationProgress(type, data, 'frames', rec.progressState);
        rec.progressState = info.state;
        rec.progressInfo = info;
        if (isViewing()) setProgressBar('frames', info);
        return info;
    };
    applyFramesProgress('queue', { message: '连接帧生成事件流...' });
    framesFeedReset(ownerId, '🔌 已连接帧生成事件流，等待后台开始…');

    let disconnectedFrames = false;
    try {
        const watch = await watchTaskUntilTerminal(taskId, {
            label: 'frames',
            signal: controller.signal,
            onEvent: (type, data) => {
                if (!isCurrent()) return;
                if (type === 'start') {
                    applyFramesProgress('start', data);
                    const total = (data && data.total) || 0;
                    rec.total = total;
                    const startMeta = `开始生成共 ${total} 帧序列图...`;
                    setMeta(startMeta);
                    framesFeedLine(ownerId, `🚀 开始生成，共 ${total} 帧（首帧文生图，后续逐帧图生图链式推进）`);

                    // 编排层会在“首帧验收”和“剩余帧分段渲染”各发一次 start。
                    // start 只代表内部阶段开始，不代表之前已经完成的帧作废；保留并
                    // 重画现有 frameRun，确保 IMG 001 一生成就持续留在界面上。
                    ensureFrameRunForStart(ownerIdea);
                    if (currentIdea && currentIdea.id === ownerId) saveCurrentIdeaState();
                    const existingIdx = savedIdeas.findIndex(item => item.id === ownerId);
                    if (existingIdx !== -1) savedIdeas[existingIdx].frameRun = ownerIdea.frameRun;

                    if (isViewing()) {
                        // renderFramesForIdea 按完整 prompt_slots 补齐等待槽位，同时保留
                        // 已完成卡片；不能在这里 grid.innerHTML=''，否则重复 start 会
                        // 把首帧重新画回“等待中”。该函数会改 meta，随后恢复阶段文案。
                        renderFramesForIdea(ownerIdea);
                        setMeta(startMeta);
                    }
                } else if (type === 'frame') {
                    applyFramesProgress('frame', data);
                    const f = data && data.frame;
                    const cur = (data && data.current) || 0;
                    const tot = (data && data.total) || 0;
                    if (cur < tot) {
                        setMeta(`正在生成帧序列: ${cur}/${tot} (正在处理第 ${cur + 1} 帧)...`);
                    } else {
                        setMeta(`正在生成帧序列: ${cur}/${tot} (已生成完毕，正在整理)...`);
                    }
                    framesFeedQualityLine(ownerId, f);
                    // 先合并数据（applyFrameEventToIdea 内会递增该帧 URL 的缓存版本），
                    // 再渲染卡片，卡片即取到新版本号；两处各自 bust 会造成同一新图被拉两次
                    applyFrameEventToIdea(f, ownerIdea);
                    if (isViewing()) updateFrameSlotCard(f);
                } else if (type === 'frame_start' || type === 'frame_retry' || type === 'queue' || type === 'frame_qa') {
                    const info = applyFramesProgress(type, data);
                    if (info && info.label) setMeta(info.label);
                    if (type === 'frame_retry') {
                        const reason = data && data.reason ? `：${data.reason}` : '';
                        framesFeedLine(ownerId, `🔁 ${(info && info.label) || '质检重试'}${reason}`, 'warn');
                    } else if (type === 'frame_start' && info && info.label) {
                        framesFeedLine(ownerId, `🎨 ${info.label}`);
                    } else if (type === 'frame_qa') {
                        const seq = data && (data.sequence || data.slot);
                        framesFeedLine(ownerId, `🧪 IMG ${String(seq || 0).padStart(3, '0')} 质检判定中…`);
                    }
                } else if (type === 'upstream_retry') {
                    // 上游秒报错秒可见：后端每次尝试失败即时推送，不再闷头退避
                    const a = (data && data.attempt) || '?';
                    const m = (data && data.max_attempts) || '?';
                    const tail = data && data.retry_in
                        ? `，${data.retry_in}s 后自动重试（第 ${a}/${m} 次）`
                        : `（第 ${a}/${m} 次，此路终止，任务即将报错结束）`;
                    framesFeedLine(ownerId, `⚠️ 上游报错：${(data && data.error) || '未知错误'}${tail}`, 'warn');
                    setMeta(`上游报错，自动重试中（第 ${a}/${m} 次）...`);
                } else if (type === 'manual_intervention_detected' || type === 'manual_intervention_cleared'
                           || type === 'manual_intervention_timeout') {
                    handleManualInterventionEvent(type, data);
                    if (data && data.reason) {
                        framesFeedLine(ownerId, `🛑 ${type === 'manual_intervention_detected' ? '需要人工处理' : type === 'manual_intervention_cleared' ? '人工处理已完成，继续生成' : '人工处理等待超时'}：${data.reason}`,
                                       type === 'manual_intervention_cleared' ? undefined : 'warn');
                    }
                } else if (type === 'anchor_inertia') {
                    // 换族锚点惯性卡死（本地像素 MAD 判据，不是视觉审查）：动态流留痕
                    if (data && data.message) {
                        framesFeedLine(ownerId, `🧲 ${data.message}`, 'warn');
                    }
                } else if (type === 'account_switch') {
                    // 这一批换了号池账号（IP 全程不动，换 IP 已全局关停）。
                    // 属于正常轮换，不是告警，所以不标 warn。
                    if (data && data.message) {
                        framesFeedLine(ownerId, `🔀 ${data.message}`);
                    }
                } else if (type === 'transport_fallback') {
                    // 图生图端点号池无额度、同模型改走 chat 通道续渲：帧能接着渲。
                    // 只有请求 2K/4K 时才是真降档（该通道固定出 1K 档）——按 degraded
                    // 标警示色，1K 单不吓人。manifest 里也记了 transport/actual_pixels。
                    if (data && data.message) {
                        framesFeedLine(ownerId, `🔀 ${data.message}`, data.degraded ? 'warn' : undefined);
                    }
                } else if (type === 'sequence_review') {
                    setMeta((data && data.message) || '正在对整套序列做一致性审查...');
                    framesFeedLine(ownerId, `🔍 ${(data && data.message) || '正在对整套序列做一致性审查...'}`);
                } else if (type === 'sequence_review_result') {
                    if (data && data.message) {
                        framesFeedLine(ownerId, `${data.passed ? '✅' : '🛠️'} ${data.message}`, data.passed ? 'ok' : 'warn');
                    } else if (data && data.passed) {
                        framesFeedLine(ownerId, '✅ 整套序列一致性审查通过', 'ok');
                    }
                } else if (type === 'reconnecting') {
                    setMeta(`连接中断，正在重连（第 ${data.attempt} 次）...`);
                    framesFeedLine(ownerId, `⚠️ 连接中断，正在重连（第 ${data.attempt} 次）…`, 'warn');
                } else if (type === 'result' || type === 'error') {
                    applyFramesProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;

        if (watch.status === 'cancelled') {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }
        if (watch.status === 'disconnected') {
            // 与服务器失联 ≠ 任务失败：后端渲染线程是独立线程，不知道客户端已经
            // 放弃，会一直跑到底。绝不能在这里当成失败收尾——那样 finally
            // 会 endIdeaTask 清掉任务登记，这条任务从此在客户端没人认领，用户
            // 还可能对同一帧再点一次"生成"，跟后台线程并发写同一张图。
            disconnectedFrames = true;
            setMeta(watch.error);
            framesFeedLine(ownerId, `⚠️ ${watch.error}`, 'warn');
            showToast(`${titleTag()}${watch.error}`, 'warning');
            return;
        }
        if (watch.status === 'failed') {
            throw new Error(watch.error || '未知错误');
        }

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, ownerIdea);
            if (isViewing()) renderFramesForIdea(ownerIdea);
            framesFeedLine(ownerId, `🏁 帧序列全部完成，共 ${(watch.result.frames || []).length} 帧`, 'ok');
            const frameRisks = summarizeRunQuality(watch.result);
            if (frameRisks) {
                framesFeedLine(ownerId, `⚠️ 本单质量风险：${frameRisks.join('；')}——建议先处理（详见帧卡片徽标）再生成视频`, 'warn');
                setMeta(`帧序列完成，但存在质量风险：${frameRisks.join('；')}`);
                showToast(`${titleTag()}帧序列完成，但检测到质量风险，详见帧序列动态流。`, 'warning');
            } else {
                showToast(`${titleTag()}已成功生成 ${(watch.result.frames || []).length} 帧连续帧序列图。`, "success");
            }
        }
    } catch (e) {
        if (!isCurrent()) return;
        console.error("Failed to generate frames:", e);
        if (e.name === 'AbortError') {
            setMeta('帧序列生成已被用户取消。');
            framesFeedLine(ownerId, '⏹ 帧序列生成已被用户取消', 'warn');
            showToast(`${titleTag()}已取消帧序列生成`, 'info');
        } else {
            setMeta(`帧序列生成失败: ${e.message}`);
            framesFeedLine(ownerId, `❌ 帧序列生成失败：${e.message}`, 'err');
            showToast(`${titleTag()}帧序列生成失败: ${e.message}`, "error");
        }

        if (isViewing()) renderFramesForIdea(ownerIdea);
    } finally {
        if (isCurrent()) {
            if (isViewing()) framesFeedSetLive(ownerId, false);
            // 失联分支保留任务登记：下次刷新页面 resumeActiveBackgroundTasksIfExists
            // 才有机会重新接上这条任务的事件流，而不是让它在客户端彻底失踪。
            if (!disconnectedFrames) {
                endIdeaTask(ownerId, 'frames');
                // 忙态画在跨创意共用的网格 DOM 上，按当前正看着的创意现算一遍
                if (typeof refreshSlotGridBusy === 'function') refreshSlotGridBusy('image');
                if (isViewing()) {
                    progress.style.display = 'none';
                    btn.disabled = false;
                    // 必须在 endIdeaTask 之后再重渲一次：renderFramesForIdea 的
                    // isFramePending 读的就是这条任务登记，catch/成功分支里那次重渲
                    // 发生在登记还在的时候，没轮到的槽位会继续画成「等待中」并一直
                    // 转圈——用户点了取消、后台 4 秒后就停了，界面却满屏 spinner，
                    // 看起来像"取消了任务还在跑"（2026-07-28 实机截图）。清掉登记后
                    // 这些槽位落到「未生成/已失效」态，并重新带上生成/上传出口。
                    renderFramesForIdea(ownerIdea);
                }
            }
        }
    }
}

async function streamVideosProgress(taskId, ownerIdea, targetSlots) {
    ownerIdea = ownerIdea || currentIdea;
    if (!ownerIdea) return;
    const ownerId = ownerIdea.id;
    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = slotRenderTarget('video');
    if (!btn || !progress || !meta || !grid) return;

    const existingRec = getIdeaTaskRecord(ownerId, 'videos');
    if (existingRec && existingRec.controller) {
        try { existingRec.controller.abort(); } catch (_) { /* noop */ }
    }
    const controller = new AbortController();
    const rec = beginIdeaTask(ownerId, 'videos', taskId, controller);
    // 调试模式（仅生成前 N 段）：标记本次任务的目标槽位范围，renderVideosForIdea
    // 靠这个字段区分"还没轮到（等待中）"和"这次任务压根没请求（正常缺段）"，
    // 同 streamFramesProgress/retrySingleFrame 的同款契约。
    if (targetSlots && targetSlots.length) rec.targetSlots = targetSlots;
    const isCurrent = () => isIdeaTaskCurrent(ownerId, 'videos', taskId);
    const isViewing = () => isViewingIdea(ownerId);
    const setMeta = (text) => { rec.meta = text; if (isViewing()) meta.textContent = text; };
    const titleTag = () => isViewing() ? '' : `「${ownerIdea.title || '创意'}」`;

    if (isViewing()) {
        btn.disabled = true;
        progress.style.display = 'flex';
    }
    setMeta('连接视频生成事件流...');

    const applyVideoProgress = (eventType, eventData) => {
        if (!window.ProgressModel) return null;
        const progressInfo = ProgressModel.normalizeGenerationProgress(eventType, eventData, 'videos', rec.progressState);
        rec.progressState = progressInfo.state;
        rec.progressInfo = progressInfo;
        if (isViewing()) setProgressBar('videos', progressInfo);
        return progressInfo;
    };
    applyVideoProgress('queue', { message: '连接视频生成事件流...' });

    // 失败/取消时把还挂着转圈的槽位统一改画失败卡（仅在正看着这个创意时才有 DOM 可改）
    const failPendingSlots = (message, labelText) => {
        if (!isViewing()) return;
        grid.querySelectorAll('.placeholder-frame-card').forEach(card => {
            const slotMatch = card.id && card.id.match(/video-slot-(\d+)/);
            if (slotMatch) renderVideoSlotFailed(parseInt(slotMatch[1], 10), message, labelText);
        });
    };

    let disconnectedVideos = false;
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
                    rec.total = total;
                    setMeta(`开始生成共 ${total} 段视频...`);
                    if (isViewing()) {
                        clearSlotGrid(grid, 'video');
                        const slotsToRender = slots.length ? slots : Array.from({ length: total }, (_, i) => i + 1);
                        slotsToRender.forEach(slotIdx => {
                            const placeholderCard = document.createElement('div');
                            placeholderCard.id = `video-slot-${slotIdx}`;
                            // 生成期间新建的占位卡此前漏了这一步，导致整单跑完
                            // 之前这些格子接不住拖拽（上传/换位）——补上
                            enableVideoSlotDnd(placeholderCard, slotIdx);
                            renderSlotCard(placeholderCard, slotPendingState('video', slotIdx, '等待中'));
                            placeSlotCard(placeholderCard, 'video', slotIdx);
                            grid.appendChild(placeholderCard);
                        });
                    }
                } else if (type === 'video_start') {
                    applyVideoProgress('video_start', data);
                    setMeta(`正在生成视频: ${data.current}/${data.total} (正在处理第 ${data.index} 段视频)...`);
                    if (isViewing()) {
                        const slot = document.getElementById(`video-slot-${data.index}`);
                        if (slot && slot.classList.contains('placeholder-frame-card')) {
                            renderVideoSlotPending(data.index, '生成中...');
                        }
                    }
                } else if (type === 'video_done') {
                    applyVideoProgress('video_done', data);
                    setMeta(`正在生成视频: ${data.current}/${data.total}...`);
                    if (isViewing()) renderVideoSlotDone(data.index, data.video);
                } else if (type === 'video_error') {
                    applyVideoProgress('video_error', data);
                    const msg = (data && data.message) || '生成失败';
                    setMeta(`视频 ${data.index} 生成失败: ${msg}`);
                    if (isViewing()) renderVideoSlotFailed(data.index, msg);
                } else if (type === 'video_skipped') {
                    // 硬切占位槽（旧单专属，新单的 [CUT] 槽照常生成）：不生成片段，按已完成计入进度
                    applyVideoProgress('video_done', data);
                    setMeta(`视频 ${data.index} 为声明式硬切槽位，已跳过生成`);
                    if (isViewing() && typeof renderVideoSlotSkippedCut === 'function') {
                        renderVideoSlotSkippedCut(data.index, data && data.message);
                    }
                } else if (type === 'queue') {
                    applyVideoProgress('queue', data);
                    setMeta((data && data.message) || '正在排队等待生成视频...');
                } else if (type === 'merge_skip') {
                    applyVideoProgress('merge_skip', data);
                    setMeta((data && data.message) || '由于存在失败片段，已跳过自动合并。');
                } else if (type === 'merge_start') {
                    applyVideoProgress('merge_start', data);
                    setMeta('正在自动合并并加速视频 (2x Speed)...');
                } else if (type === 'merge_done') {
                    applyVideoProgress('merge_done', data);
                    setMeta('所有视频已成功生成并合并加速！');
                } else if (type === 'merge_error') {
                    applyVideoProgress('merge_error', data);
                    setMeta(`自动合并视频失败: ${(data && data.message) || '未知错误'}`);
                    if (isViewing()) showToast(`自动合并失败: ${(data && data.message) || '未知错误'}`, "warning");
                } else if (type === 'reconnecting') {
                    setMeta(`连接中断，正在重连（第 ${data.attempt} 次）...`);
                } else if (type === 'manual_intervention_detected' || type === 'manual_intervention_cleared'
                           || type === 'manual_intervention_timeout') {
                    handleManualInterventionEvent(type, data);
                } else if (type === 'result' || type === 'error') {
                    applyVideoProgress(type, data);
                }
            }
        });

        if (!isCurrent()) return;

        if (watch.status === 'cancelled') {
            throw Object.assign(new Error('已取消'), { name: 'AbortError' });
        }
        if (watch.status === 'disconnected') {
            // 与服务器失联 ≠ 任务失败，见 streamFramesProgress 同款说明：后端渲染
            // 线程独立于客户端连接，会继续跑到底。不能在这里当失败处理收尾。
            disconnectedVideos = true;
            setMeta(watch.error);
            showToast(`${titleTag()}${watch.error}`, 'warning');
            return;
        }
        if (watch.status === 'failed') {
            throw new Error(watch.error || '未知错误');
        }

        if (watch.result) {
            await syncFrameRunToLibrary(watch.result, ownerIdea);
            if (isViewing()) renderVideosForIdea(ownerIdea);
            const videoRisks = summarizeRunQuality(watch.result);
            if (videoRisks) {
                setMeta(`视频生成完成，但存在质量风险：${videoRisks.join('；')}——建议处理后再合并成片`);
                showToast(`${titleTag()}视频生成完成，但检测到质量风险，建议合并成片前先处理。`, 'warning');
            } else {
                showToast(`${titleTag()}已成功生成 ${(watch.result.videos || []).length} 段连续视频。`, "success");
            }
        }
    } catch (e) {
        if (!isCurrent()) return;
        console.error("Failed to generate videos:", e);

        if (e.name === 'AbortError') {
            setMeta('视频生成已被用户取消。');
            showToast(`${titleTag()}已取消视频生成`, 'info');
            failPendingSlots('已被用户取消', '未生成');
        } else {
            setMeta(`视频生成失败: ${e.message}`);
            showToast(`${titleTag()}视频生成失败: ${e.message}`, "error");
            failPendingSlots(e.message || '生成失败', '生成失败');

            await reloadManifestIntoIdea(ownerIdea);
            if (isViewing() && ownerIdea.frameRun) renderVideosForIdea(ownerIdea);
        }
    } finally {
        if (isCurrent()) {
            // 失联分支保留任务登记，供下次刷新页面重新接上事件流。
            if (!disconnectedVideos) {
                endIdeaTask(ownerId, 'videos');
                if (typeof refreshSlotGridBusy === 'function') refreshSlotGridBusy('video');
                if (isViewing()) {
                    progress.style.display = 'none';
                    btn.disabled = false;
                    // 与 streamFramesProgress 同款收尾：必须在 endIdeaTask 之后再重渲
                    // 一次——上面成功/失败分支那次重渲发生在登记还在的时候，没轮到的
                    // 槽位会继续画成「等待中」转圈、卡片按钮也停在禁用态。
                    renderVideosForIdea(ownerIdea);
                }
            }
        }
    }
}

async function streamCoverProgress(taskId, ownerIdea) {
    ownerIdea = ownerIdea || currentIdea;
    if (!ownerIdea) return;
    const ownerId = ownerIdea.id;
    const loadingEl = document.getElementById('cover-img-loading');
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const makeBtn = document.getElementById('make-cover-btn');
    if (!loadingEl || !placeholderEl || !displayEl || !makeBtn) return;

    const isViewing = () => isViewingIdea(ownerId);
    const titleTag = () => isViewing() ? '' : `「${ownerIdea.title || '创意'}」`;

    if (isViewing()) {
        loadingEl.style.display = 'flex';
        placeholderEl.style.display = 'none';
        displayEl.style.display = 'none';
        makeBtn.disabled = true;
    }

    const controller = new AbortController();
    beginIdeaTask(ownerId, 'cover', taskId, controller);
    const isCurrent = () => isIdeaTaskCurrent(ownerId, 'cover', taskId);

    let disconnectedCover = false;
    try {
        const watch = await watchTaskUntilTerminal(taskId, { label: 'cover', signal: controller.signal });

        if (!isCurrent()) return;

        if (watch.status === 'disconnected') {
            // 与服务器失联 ≠ 任务失败，见 streamFramesProgress 同款说明。
            disconnectedCover = true;
            showToast(`${titleTag()}${watch.error}`, 'warning');
            return;
        }
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

        if (!ownerIdea.covers) {
            ownerIdea.covers = [];
        }
        ownerIdea.covers.push(imageUrl);
        if (englishTitle) {
            ownerIdea.english_title = englishTitle;
        }
        // 旧创意补齐发布用双语标题行（后端只在缺字段时才生成并随结果返回）
        if (data.social_title_en && !ownerIdea.social_title_en) {
            ownerIdea.social_title_en = data.social_title_en;
        }
        if (data.social_title_cn && !ownerIdea.social_title_cn) {
            ownerIdea.social_title_cn = data.social_title_cn;
        }
        if (currentIdea && currentIdea.id === ownerId) {
            saveCurrentIdeaState();
            renderIdeaTitles(ownerIdea);
        }

        const existingIdx = savedIdeas.findIndex(item => item.id === ownerId);
        if (existingIdx !== -1) {
            savedIdeas[existingIdx].covers = ownerIdea.covers;
            if (englishTitle) {
                savedIdeas[existingIdx].english_title = englishTitle;
            }
            if (ownerIdea.social_title_en) {
                savedIdeas[existingIdx].social_title_en = ownerIdea.social_title_en;
            }
            if (ownerIdea.social_title_cn) {
                savedIdeas[existingIdx].social_title_cn = ownerIdea.social_title_cn;
            }
            await persistIdeaItem(savedIdeas[existingIdx]);
        }

        if (isViewing()) renderCoversForIdea(ownerIdea, ownerIdea.covers.length - 1);
        showToast(`${titleTag()}封面图制作成功！`, "success");
    } catch (e) {
        if (!isCurrent()) return;
        console.error("Failed to generate cover:", e);
        showToast(`${titleTag()}封面制作失败: ${e.message}`, "error");

        if (isViewing()) {
            if (ownerIdea.covers && ownerIdea.covers.length > 0) {
                renderCoversForIdea(ownerIdea, ownerIdea.covers.length - 1);
            } else {
                placeholderEl.style.display = 'flex';
            }
        }
    } finally {
        if (isCurrent() && !disconnectedCover) {
            endIdeaTask(ownerId, 'cover');
            if (isViewing()) {
                loadingEl.style.display = 'none';
                makeBtn.disabled = false;
            }
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

// Save currently active idea to the library.
// 走单条写入（/api/library/item）：只写这一条的正文文件 + 索引行，不再把整个
// 创意库上传一遍，也就不会撞上整表覆盖的那三道闸门。
async function saveCurrentIdea() {
    if (!currentIdea) return;

    // Check if already saved
    if (savedIdeas.some(item => item.title === currentIdea.title)) {
        showToast("该创意已存在于点子库中", "error");
        return;
    }

    const idea = { ...currentIdea };
    savedIdeas.unshift(idea);
    updateFavoriteButtonState();
    if (typeof refreshProjects === 'function') refreshProjects({ assets: false });

    const ok = await persistIdeaItem(idea);
    if (ok) showToast("成功保存至点子库！", "success");
}

async function deleteFromLibrary(id) {
    const idea = savedIdeas.find(item => item.id === id);
    if (!idea) return;

    const ok = await deleteIdeaItem(idea);
    if (!ok) return;   // 服务端没删成功就别动本地状态，否则两边会不一致

    savedIdeas = savedIdeas.filter(item => item.id !== id);
    try {
        localStorage.setItem('spark_library', JSON.stringify(savedIdeas));
    } catch (e) {
        console.warn('[library] localStorage 镜像写入失败', e);
    }
    if (typeof refreshProjects === 'function') refreshProjects({ assets: false });
    showToast("已从点子库删除，生成的图片/视频文件已一并清理", "success");
    updateFavoriteButtonState();
    if (typeof refreshProjects === 'function') refreshProjects();
}

function loadSavedIdea(idea, options = {}) {
    currentIdea = idea;
    saveCurrentIdeaState();
    renderIdea(idea);

    document.getElementById('output-placeholder-view').classList.remove('active');
    document.getElementById('output-loading-view').classList.remove('active');

    const errorView = document.getElementById('output-error-view');
    if (errorView) errorView.style.display = 'none';

    document.getElementById('output-content-view').classList.add('active');

    switchTab('overview');
    showToast(options.toast || "已载入收藏的创意", "success");
    updateActiveGenerationBanner();
}

/* ==========================================================================
   台账 / 画廊 / 项目工作台 →「激发项目」直达入口

   2026-07-31（P3）之前这里是三套标题模糊匹配：把一句话选题、场景主题、任务
   task_label、Topic DNA 各归一化一遍互相撞。撞不上就"找不到"，撞错了就打开
   另一条创意——因为那时候根本没有主键：project_key 要等合成跑完才生成。

   现在 project_key 从**任务创建那一刻**就定下（server_common.ensure_task_project_key），
   并且写进任务 dimensions、点子库条目、台账行三处，所以这里是一次直查。
   标题匹配只作为历史数据（没有 project_key 的老记录）的回落分支保留。
   ========================================================================== */

function sparkNormKey(v) {
    return String(v == null ? '' : v).replace(/\s+/g, ' ').trim().toLowerCase();
}

// 画廊传上来的是**目录名**，它是 _safe_project_name(project_key) 的结果：
// '__' 会被折成 '_'，且截断到 60 字符。所以目录名与 project_key 不能直接相等
// 比较，这里做同样的归一化再比。
function sparkProjectDirKey(v) {
    return String(v == null ? '' : v).replace(/_+/g, '_').trim().toLowerCase();
}

function sparkProjectKeyMatches(a, b) {
    if (!a || !b) return false;
    if (String(a) === String(b)) return true;
    const ka = sparkProjectDirKey(a);
    const kb = sparkProjectDirKey(b);
    if (!ka || !kb) return false;
    // 截断：短的那个是长的那个的前缀就算命中
    return ka === kb || ka.startsWith(kb) || kb.startsWith(ka);
}

// 点子库里找这条创意的记录。project_key 优先（硬主键），其余是老记录的回落。
// savedIdeas 按新→旧排列，find 天然取最近一次合成。
function findSavedIdeaForSpark({ ideaId = null, seed = '', title = '', projectKey = '' } = {}) {
    if (!Array.isArray(savedIdeas) || !savedIdeas.length) return null;
    if (ideaId) {
        const byId = savedIdeas.find(i => String(i.id) === String(ideaId));
        if (byId) return byId;
    }
    if (projectKey) {
        const byKey = savedIdeas.find(i => sparkProjectKeyMatches(i.project_key, projectKey));
        if (byKey) return byKey;
    }
    // ── 以下仅供没有 project_key 的历史记录回落 ──
    const seedKey = sparkNormKey(seed);
    const titleKey = sparkNormKey(title);
    return (seedKey && savedIdeas.find(i => sparkNormKey(i.theme) === seedKey))
        || (titleKey && savedIdeas.find(i => sparkNormKey(i.title) === titleKey
                                          || sparkNormKey(i.theme) === titleKey))
        || null;
}

// 任务侧兜底（这条创意还没收藏，或收藏前就想回看）。
async function findCompletedTaskForSpark({ dna = '', seed = '', title = '', projectKey = '' } = {}) {
    let tasks = [];
    try {
        const res = await fetch('/api/tasks');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        tasks = Array.isArray(data) ? data : (data.tasks || []);
    } catch (e) {
        console.warn('Failed to load tasks while resolving spark project', e);
        return null;
    }
    // 只认创意激发任务：帧/分步渲染/视频/封面这些子任务现在也带着同一个
    // project_key（P3），误配上去 loadCompletedTask 会载入一份没有提示词的空壳结果
    const done = tasks.filter(t => t && t.status === 'completed' && t.result && isIdeationTask(t));
    const dims = t => t.dimensions || {};

    if (projectKey) {
        // 主键直查：任务创建时就写进 dimensions 了
        const byKey = done.find(t => sparkProjectKeyMatches(dims(t).project_key, projectKey)
                                  || sparkProjectKeyMatches((t.result || {}).project_key, projectKey));
        if (byKey) return byKey;
        // 老任务没有 project_key：目录名以 run_<task_id>_ 开头，拿它做前缀匹配
        // 比反解目录名更稳（task_id 自身可能带下划线，反解会切错）
        const byPrefix = done.find(t => {
            const safeId = String(t.id || '').replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '');
            return safeId && projectKey.startsWith(`run_${safeId}_`);
        });
        if (byPrefix) return byPrefix;
    }
    // ── 以下仅供没有 project_key 的历史记录回落 ──
    const dnaKey = sparkNormKey(dna);
    const seedKey = sparkNormKey(seed);
    const titleKey = sparkNormKey(title);
    return (dnaKey && done.find(t => sparkNormKey((dims(t).ledger_candidate || {}).dna
                                                  || dims(t).topic_dna) === dnaKey))
        || (seedKey && done.find(t => sparkNormKey(dims(t).theme) === seedKey))
        || (titleKey && done.find(t => sparkNormKey(dims(t).task_label) === titleKey
                                    || sparkNormKey(dims(t).theme) === titleKey))
        || null;
}

// 打开一条创意对应的激发项目，落到「激发结果」工作区。找不到时给出明确提示
// 并返回 false（调用方不需要自己再报错）。
async function openSparkProject({ ideaId = null, dna = '', seed = '', title = '', projectKey = '', label = '' } = {}) {
    const name = label || title || '该创意';
    const idea = findSavedIdeaForSpark({ ideaId, seed, title, projectKey });
    if (idea) {
        switchMainTab('results');
        loadSavedIdea(idea, { toast: `已打开激发项目「${idea.title || name}」` });
        return true;
    }
    const task = await findCompletedTaskForSpark({ dna, seed, title, projectKey });
    if (task) {
        switchMainTab('results');
        await loadCompletedTask(task.id);
        return true;
    }
    showToast(`没找到「${name}」对应的激发项目——它可能还没合成成功，或历史记录已被清理`, 'error');
    return false;
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
    reader.onload = async function(evt) {
        try {
            const imported = JSON.parse(evt.target.result);
            if (!Array.isArray(imported)) throw new Error("Invalid file structure");

            // Merge without duplicates based on title
            const existingTitles = new Set(savedIdeas.map(item => item.title));
            const fresh = [];
            imported.forEach(item => {
                if (item.title && !existingTitles.has(item.title)) {
                    if (!item.id) item.id = Date.now().toString() + Math.random();
                    existingTitles.add(item.title);
                    savedIdeas.push(item);
                    fresh.push(item);
                }
            });
            if (typeof refreshProjects === 'function') refreshProjects({ assets: false });
            // 逐条写入而不是整表回写：导入 50 条时老写法要把「原有全库 + 50 条新
            // 记录」整份上传一遍，而且中途失败就是全有或全无。
            let saved = 0;
            for (const item of fresh) {
                if (await persistIdeaItem(item)) saved++;
            }
            showToast(saved === fresh.length
                ? `成功导入 ${saved} 个新创意点子！`
                : `导入 ${fresh.length} 条，其中 ${saved} 条已存到服务器（其余仅存在浏览器本地）`,
                saved === fresh.length ? "success" : "error");
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

/* ──────────────────────────────────────────────────────────────────────────
   一键生成中英双版主题和 tags（结果页标题行的 ✨）

   正常激发的单子在收尾就带上了这些字段（server.background_worker 调
   generate_social_titles）。手动上传的提示词集走的是另一条路：js/prompt_import.js
   直接建条目，theme 只能填成标题（往往就是个文件名）、两行发布标题留空——工作台
   那一行于是既没主题也没话题。这里补的就是这一步，且刻意以**提示词集正文**为依据
   送给模型（见 prompt_pipeline.generate_project_meta），而不是只递一个标题过去。

   已有值不静默覆盖：这两行是要粘进发布框的文案，用户可能已经手改过。

   项目名跟着新主题一起改，本地目录与目录里的文件也跟着改（见 renameIdeaToTheme）：
   磁盘命名空间取的是 project_key || title（getIdeaSaveTitle），只改条目里的标题、
   不动磁盘，下一次按新名字去找帧/视频/封面只会找到一个空目录——已生成的资产在界面
   上凭空消失。目录搬迁与目录内 json 的路径改写全在服务端一次做完
   （/api/project/rename），这里只负责把回来的改名清单套到创意条目的 URL 上。
   ────────────────────────────────────────────────────────────────────────── */

// 项目目录换名之后，条目里那些指向旧目录的 URL 全都失效。服务端回的
// old_dir_name/new_dir_name/file_map 就是全部改动，照着替换即可。
// 只走媒体字段：提示词/审核报告是正文，正文里碰巧出现同名字符串也不该被改。
const IDEA_MEDIA_URL_FIELDS = ['covers', 'activeCoverUrl', 'coverRoles', 'collage_url',
                               'cover_url', 'frameRun'];

function rewriteIdeaMediaUrls(idea, plan) {
    const pairs = [[`outputs/${plan.old_dir_name}/`, `outputs/${plan.new_dir_name}/`]]
        .concat(Object.entries(plan.file_map || {}));
    const swap = (s) => pairs.reduce((acc, [a, b]) => (a && a !== b ? acc.split(a).join(b) : acc), s);
    const walk = (node) => {
        if (typeof node === 'string') return swap(node);
        if (Array.isArray(node)) return node.map(walk);
        if (node && typeof node === 'object') {
            Object.keys(node).forEach(k => { node[k] = walk(node[k]); });
        }
        return node;
    };
    IDEA_MEDIA_URL_FIELDS.forEach(f => { if (idea[f] != null) idea[f] = walk(idea[f]); });
}

/**
 * 把项目名同步成新主题，并让服务端把本地目录/文件一起改掉。
 *
 * 目录搬不搬 ≠ 名字改不改：这一单还有帧/视频作业在跑时目录不能搬（worker 攥着
 * 旧目录路径），但名字照改——服务端会把旧键回给我们钉进 project_key，磁盘命名
 * 空间从此不再跟着标题走，资产一张都不丢（见 server.py /api/project/rename）。
 *
 * 返回 {renamed, from, to, reason, plan}——reason 是没改名的原因，调用方要如实
 * 报给用户，不能让"名字没变"看起来像什么都没发生。
 */
async function renameIdeaToTheme(idea, newTitle) {
    const from = idea.title || '';
    const to = String(newTitle || '').trim();
    if (!to || to === from) return { renamed: false, from, to, reason: '' };
    if (Array.isArray(savedIdeas) && savedIdeas.some(it => it.id !== idea.id && it.title === to)) {
        return { renamed: false, from, to, reason: `点子库里已有同名创意「${to}」` };
    }

    let plan;
    try {
        // getIdeaSaveTitle 给的就是当前的磁盘命名空间键（没有 project_key 的老创意
        // 用的是标题本身），服务端按它定位要搬的目录
        plan = await slotPostJson('/api/project/rename', {
            project_key: getIdeaSaveTitle(idea),
            new_title: to,
        });
    } catch (e) {
        // 目录没搬成就绝不能改名：改了名字，界面就再也找不到旧目录里的资产了
        return { renamed: false, from, to, reason: `本地目录改名失败：${e.message}` };
    }

    idea.title = to;
    // 无论目录搬没搬，回来的这个键都要钉进条目里当磁盘命名空间：
    //   · 搬了 —— 它是新目录名对应的新键；
    //   · 没搬（还有作业在跑 / 这一单还没生成过任何媒体）—— 它就是旧键，
    //     钉住它，标题才敢改：老条目原本没有 project_key、拿标题当命名空间，
    //     改完名再去找帧/视频/封面就只剩一个空目录（见 getIdeaSaveTitle）。
    idea.project_key = plan.project_key;
    if (plan.moved) rewriteIdeaMediaUrls(idea, plan);
    return { renamed: true, from, to, reason: '', plan };
}

async function generateProjectMetaForCurrentIdea() {
    if (!currentIdea) {
        showToast('先打开一个项目再生成主题和 tags。', 'error');
        return;
    }
    const promptBlock = currentIdea.prompt_block || '';
    if (!promptBlock.trim() && !(currentIdea.title || '').trim()) {
        showToast('这一单既没有提示词集也没有标题，无从推断主题与话题。', 'error');
        return;
    }

    const meta = getIdeaTikTokMeta(currentIdea);
    const hasExisting = !!(currentIdea.social_title_en || currentIdea.social_title_cn);
    if (hasExisting) {
        const ok = await customConfirm(
            '这一单已经有主题和 tags 了，重新生成会<b>覆盖</b>现有的两行发布文案：'
            + `<ul style="margin:8px 0 0 18px; line-height:1.7;">
                 <li>英文：${escapeHtml(meta.english || '（空）')}</li>
                 <li>中文：${escapeHtml(meta.chinese || '（空）')}</li>
                 <li>项目名也会同步改成新主题（当前「${escapeHtml(currentIdea.title || '')}」）</li>
               </ul>`);
        if (!ok) return;
    }

    const btn = document.getElementById('gen-project-meta-btn');
    if (btn) btn.disabled = true;
    // 生成期间换单/关页都可能发生：认准发起时的这条创意，回来只写它
    const ownerId = currentIdea.id;
    const ownerIdea = currentIdea;
    showToast('正在按提示词集推荐主题和 tags…', 'info');
    try {
        const data = await slotPostJson('/api/project_meta', {
            config: config,
            title: ownerIdea.title || '',
            theme: ownerIdea.theme || '',
            creativity: ownerIdea.creativity || '',
            prompt_block: promptBlock,
        });

        // 四个字段各自可能为空（模型漏给），空的不覆盖已有值
        if (data.theme_cn) ownerIdea.theme = data.theme_cn;
        if (data.theme_en) ownerIdea.theme_en = data.theme_en;
        if (data.tiktok) ownerIdea.social_title_en = data.tiktok;
        if (data.cn) ownerIdea.social_title_cn = data.cn;
        const rename = await renameIdeaToTheme(ownerIdea, data.theme_cn);

        if (currentIdea && currentIdea.id === ownerId) {
            saveCurrentIdeaState();
            renderIdeaTitles(ownerIdea);
            const tagThemeEl = document.getElementById('tag-theme');
            if (tagThemeEl) tagThemeEl.textContent = ownerIdea.theme || '';
        }

        const existingIdx = savedIdeas.findIndex(item => item.id === ownerId);
        if (existingIdx !== -1) {
            // 改名会连带重写 covers/collage_url/frameRun 里的 URL（目录换名了），
            // 所以这里整条覆盖，而不是只挑那几个文本字段
            Object.assign(savedIdeas[existingIdx], ownerIdea);
            await persistIdeaItem(savedIdeas[existingIdx]);
        } else if (rename.renamed) {
            // 磁盘那边已经按新名字改完了，条目却不在点子库里：不落库的话，下次打开
            // 看到的还是旧名字（以及可能已经失效的旧 URL）。如实报出来。
            showToast(`项目名已改成「${rename.to}」，但这一单不在点子库里，改名没能存下来——`
                      + '先收藏这一单再点一次 ✨。', 'warning', 8000);
        }
        if (typeof refreshProjects === 'function') refreshProjects({ assets: false });

        const plan = rename.plan || {};
        const movedNote = plan.moved
            ? `，本地目录改成 outputs/${plan.new_dir_name}`
            // 有作业在跑时目录不搬（worker 攥着旧路径），命名空间钉在旧键上。
            // 名字确实改了，但目录名对不上，说清楚免得用户以为改了个寂寞。
            : (plan.reason === 'busy'
                ? `（还有作业在跑，本地目录暂不搬迁，仍是 outputs/${plan.old_dir_name}）` : '');
        showToast(rename.renamed
            ? `主题和 tags 已生成，项目名同步改成「${rename.to}」${movedNote}`
            : `主题和 tags 已生成：${ownerIdea.theme || ''}`, 'success');
        // 改名被挡住时必须说清楚，否则用户只会看到"主题变了、名字没变"
        if (!rename.renamed && rename.reason) {
            showToast(`项目名保持「${rename.from}」未改：${rename.reason}`, 'warning', 7000);
        }
        // 目录搬了但目录里某些 json 没改写成：那些文件里还留着旧路径，如实报出来
        const failures = (rename.plan && rename.plan.rewrite_failures) || [];
        if (failures.length) {
            showToast(`本地目录已改名，但 ${failures.length} 个 json 里的旧路径没能改写：`
                + failures[0], 'warning', 8000);
        }
    } catch (e) {
        console.error('Failed to generate project meta:', e);
        showToast(`生成主题和 tags 失败：${e.message}`, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
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

// ── 调试模式：帧序列/视频序列可选只生成前 N 条 ──────────────────────────
// 用于快速验证一套提示词是否值得往下整单生成，以及方便反复迭代提示词时不用
// 每次都等全量跑完。勾选状态与数量按浏览器持久化（同一创意反复调试不用重设）。
function initDebugLimitControls() {
    ['frames', 'videos'].forEach(kind => {
        const enabledEl = document.getElementById(`${kind}-debug-enabled`);
        const countEl = document.getElementById(`${kind}-debug-count`);
        if (!enabledEl || !countEl) return;

        const storageKey = `spark_${kind}_debug_limit`;
        try {
            const stored = JSON.parse(localStorage.getItem(storageKey) || 'null');
            if (stored) {
                enabledEl.checked = !!stored.enabled;
                if (stored.count) countEl.value = stored.count;
            }
        } catch (e) { /* 存档损坏则忽略，维持控件默认值 */ }
        countEl.disabled = !enabledEl.checked;

        const persist = () => {
            localStorage.setItem(storageKey, JSON.stringify({
                enabled: enabledEl.checked,
                count: Math.max(1, parseInt(countEl.value, 10) || 3)
            }));
        };

        enabledEl.addEventListener('change', () => {
            countEl.disabled = !enabledEl.checked;
            persist();
        });
        countEl.addEventListener('change', () => {
            countEl.value = Math.max(1, parseInt(countEl.value, 10) || 3);
            persist();
        });
    });
}

// 调试模式未勾选时返回 null（等价于原有整单生成语义）；勾选时返回该创意提示词块
// 里前 N 个真实槽位号（帧序列用 image 槽位、视频序列用 video 槽位）——用槽位号
// 本身而非枚举位置，与 resolvePromptSlots 的既有约定一致，避免槽位号不连续时错位。
function computeDebugTargets(kind, idea, slotType) {
    const enabledEl = document.getElementById(`${kind}-debug-enabled`);
    const countEl = document.getElementById(`${kind}-debug-count`);
    if (!enabledEl || !countEl || !enabledEl.checked) return null;

    const n = Math.max(1, parseInt(countEl.value, 10) || 3);
    const slots = resolvePromptSlots(idea)
        .filter(s => s.type === slotType)
        .map(s => s.index)
        .sort((a, b) => a - b);
    if (!slots.length) return null;
    return slots.slice(0, n);
}

// Generate the complete IMAGE prompt chain as ordered still frames.
function withCoverReference(baseConfig, idea) {
    // 帧 1 的参考图走 'frame1' 用途：用户可以把带文案的封面留给项目卡片/成片首帧，
    // 这里单独指一张干净的，避免文案被图生图带进画面（见 media_renderer 的用途分配）。
    const chosen = coverRoleUrl(idea, 'frame1');
    if (!chosen) return baseConfig;
    return Object.assign({}, baseConfig, { coverReferencePath: chosen });
}

async function generateFrames() {
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast("请先激发一个创意点子！", "error");
        return;
    }
    const ownerIdea = currentIdea;
    const selectedCover = coverRoleUrl(ownerIdea, 'frame1');
    if (ownerIdea.degraded === true
        || (ownerIdea.quality_gate && ownerIdea.quality_gate.status !== 'passed')) {
        showToast('提示词处于降级或质量门未通过状态，不能生成帧序列。', 'error');
        return;
    }
    if (!selectedCover) {
        showToast("请先生成或选择封面图；第一帧必须以封面图进行图生图。", "error");
        return;
    }
    if (isIdeaTaskActive(ownerIdea.id, 'frames')) {
        showToast("该创意的帧序列已在生成中，请稍候", "error");
        return;
    }

    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    if (!btn || !progress || !meta) return;

    const targetSequences = computeDebugTargets('frames', ownerIdea, 'image');

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = targetSequences
        ? `准备生成帧序列（调试模式：仅前 ${targetSequences.length} 帧）...`
        : '准备生成帧序列...';

    try {
        const body = {
            config: withCoverReference(config, ownerIdea),
            title: getIdeaSaveTitle(ownerIdea),
            display_title: ownerIdea.title,
            prompt_block: ownerIdea.prompt_block,
            generation_source: ownerIdea.generation_source,
            degraded: ownerIdea.degraded === true,
            quality_gate: ownerIdea.quality_gate || null,
            diagnostic_mode: ownerIdea.diagnostic_mode === true
        };
        if (targetSequences) body.target_sequences = targetSequences;

        const response = await fetch('/api/generate_frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;

        // streamFramesProgress 内部会登记 ideaTasksById 并接管所有 UI/数据合并；
        // 不 await 它——generateFrames 只负责发起任务，让它在后台独立运行，
        // 这样切换到别的创意不会挂起这个 async 调用链。
        streamFramesProgress(taskId, ownerIdea, targetSequences);
    } catch (e) {
        console.error("Failed to generate frames:", e);
        if (isViewingIdea(ownerIdea.id)) {
            meta.textContent = `帧序列生成失败: ${e.message}`;
            renderFramesForIdea(ownerIdea);
            progress.style.display = 'none';
            btn.disabled = false;
        }
        showToast(`帧序列生成失败: ${e.message}`, "error");
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

    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'videos')) {
        showToast("该创意的视频序列已在生成中，请稍候", "error");
        return;
    }

    // Check for frames that failed the post-render sequence consistency review
    const reviewCheck = await confirmSequenceReviewOverride(ownerIdea, null);
    if (!reviewCheck.proceed) return;

    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = slotRenderTarget('video');
    if (!btn || !progress || !meta || !grid) return;

    const targetSlots = computeDebugTargets('videos', ownerIdea, 'video');

    btn.disabled = true;
    progress.style.display = 'flex';
    meta.textContent = targetSlots
        ? `准备生成视频序列（调试模式：仅前 ${targetSlots.length} 段）...`
        : '准备生成视频序列...';

    try {
        const body = {
            config,
            title: getIdeaSaveTitle(ownerIdea),
            display_title: ownerIdea.title,
            prompt_block: ownerIdea.prompt_block,
            override_flagged: reviewCheck.override,
            merge_speed: getMergeSpeed()
        };
        if (targetSlots) body.target_slots = targetSlots;

        const response = await fetch('/api/generate_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        const data = await response.json();
        const taskId = data.task_id;

        // 同 generateFrames：不 await，交给 streamVideosProgress 在后台独立跑完。
        streamVideosProgress(taskId, ownerIdea, targetSlots);
    } catch (e) {
        console.error("Failed to generate videos:", e);
        showToast(`视频生成失败: ${e.message}`, "error");

        if (isViewingIdea(ownerIdea.id)) {
            meta.textContent = `视频生成失败: ${e.message}`;
            grid.querySelectorAll('.placeholder-frame-card').forEach(card => {
                const slotMatch = card.id && card.id.match(/video-slot-(\d+)/);
                if (slotMatch) renderVideoSlotFailed(parseInt(slotMatch[1], 10), e.message);
            });

            progress.style.display = 'none';
            btn.disabled = false;
        }
    }
}

function getMergeSpeed() {
    const select = document.getElementById('merge-speed-select');
    const speed = Number(select && select.value);
    return [1, 1.5, 2].includes(speed) ? speed : 2;
}

function mergeSpeedLabel(speed = getMergeSpeed()) {
    return Number(speed) === 1 ? '无加速' : `${Number(speed)}倍速`;
}

// 成片首帧烧录封面的档位。默认 'frame'（只占一帧：肉眼看不见，平台取缩略图时拿到
// 的却已经是封面）。选择按浏览器持久化，与合并速率同款。
const COVER_BURN_STORAGE_KEY = 'spark_merge_cover_burn';

function getCoverBurn() {
    const select = document.getElementById('merge-cover-burn-select');
    const value = select && select.value;
    return ['frame', '0.5', '1', 'off'].includes(value) ? value : 'frame';
}

function initCoverBurnControl() {
    const select = document.getElementById('merge-cover-burn-select');
    if (!select) return;
    const stored = localStorage.getItem(COVER_BURN_STORAGE_KEY);
    if (stored && Array.from(select.options).some(o => o.value === stored)) select.value = stored;
    select.addEventListener('change', () => {
        localStorage.setItem(COVER_BURN_STORAGE_KEY, select.value);
    });
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
    const speed = getMergeSpeed();
    const speedLabel = mergeSpeedLabel(speed);
    const coverBurn = getCoverBurn();
    mergeBtn.disabled = true;
    // 合并不走 ideaTasksById 登记，管线条的「成片 · 合并中…」只能靠这个标志位
    mergeInFlight = true;
    updatePipelineBar();
    mergeBtn.innerHTML = `
        <div class="cover-spinner" style="width:14px; height:14px; border-width:2px; margin-bottom:0; display:inline-block; vertical-align:middle; margin-right:6px;"></div>
        <span>${force ? '正在跳过缺口合并中...' : '正在合并中...'}</span>
    `;
    videosMeta.textContent = force
        ? `正在跳过缺失/串片片段并以${speedLabel}合并，请稍候...`
        : `正在调用 FFmpeg 以${speedLabel}合并视频，此过程可能需要几秒钟，请稍候...`;

    try {
        const response = await fetch('/api/merge_videos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: getIdeaSaveTitle(currentIdea),
                force: !!force,
                speed,
                // 首帧封面：档位来自合并控件，用哪张来自「成片首帧」用途分配
                cover_burn: coverBurn,
                cover: coverBurn === 'off' ? null : coverRoleUrl(currentIdea, 'video'),
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
                await persistIdeaItem(savedIdeas[existingIdx]);
            }

            renderVideosForIdea(currentIdea);

            const mv = data.merged_video || {};
            const mergedSpeedLabel = mergeSpeedLabel(mv.speed || speed);
            // 没烧成（选了封面却没进成片）要说出来：否则用户只能等平台缩略图出来才发现
            const coverNote = mv.cover_first_frame
                ? `；封面已烧进首帧（${mv.cover_first_frame.seconds ? `${mv.cover_first_frame.seconds}秒` : '1 帧'}）`
                : (coverBurn === 'off' ? '' : '；未烧录封面首帧（没有可用封面）');
            if (mv.partial) {
                const slots = (mv.skipped_slots || []).join(', ');
                showToast(`已生成跳过缺口的合成片（${mergedSpeedLabel}）`, "success");
                videosMeta.innerHTML = `⚠️ 已合成：槽位 <b>${escapeHtml(slots)}</b> 因缺失/串片被跳过（该处为硬切），其余片段正常拼接、${mergedSpeedLabel}${escapeHtml(coverNote)}。建议重试这些片段后重新合并以获得完整成片。`;
            } else {
                showToast(`视频合并成功（${mergedSpeedLabel}）！`, "success");
                videosMeta.textContent = `视频合并已完成（${mergedSpeedLabel}）${coverNote}！`;
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
        // innerHTML 还原后芯片里的 .step-stat 会带着合并前的旧文字回来，
        // 所以必须在还原之后再刷一次管线条
        mergeBtn.innerHTML = originalText;
        mergeInFlight = false;
        updatePipelineBar();
    }
}

// 合成被门禁拦截时，在 videos-meta 区域渲染可操作面板：
//   ① 重试缺失/串片片段并自动重合   ② 按当前所选速率跳过这些片段直接合并
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
                <button type="button" class="action-btn text-btn" id="merge-force-btn">⚡ 跳过缺口合并（${escapeHtml(mergeSpeedLabel())}）</button>
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
        const ok = await customConfirm(`将跳过缺失/串片的片段，把其余可用片段按原顺序直接拼接、以${mergeSpeedLabel()}合成（跳过处为硬切，不再用占位帧填充）。确定继续吗？`);
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
    const ownerIdea = currentIdea;
    if (isIdeaTaskActive(ownerIdea.id, 'cover')) {
        showToast("该创意的封面图已在生成中，请稍候", "error");
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

    try {
        const response = await fetch('/api/generate_cover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config,
                id: ownerIdea.id,
                title: ownerIdea.title,
                // 封面图跟项目打包在一起（outputs/<项目>/cover_*.webp），所以这里
                // 必须把磁盘命名空间一起交上去——与 generateFrames 的 title 字段同源
                project_key: getIdeaSaveTitle(ownerIdea),
                theme: ownerIdea.theme,
                prompt_block: ownerIdea.prompt_block
            })
        });

        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        const taskId = data.task_id;

        // 同 generateFrames：不 await，交给 streamCoverProgress 在后台独立跑完。
        streamCoverProgress(taskId, ownerIdea);
    } catch (e) {
        console.error("Failed to generate cover:", e);
        showToast(`封面制作失败: ${e.message}`, "error");

        if (isViewingIdea(ownerIdea.id)) {
            // Restore state based on whether we already have covers
            if (ownerIdea.covers && ownerIdea.covers.length > 0) {
                renderCoversForIdea(ownerIdea, ownerIdea.covers.length - 1);
            } else {
                placeholderEl.style.display = 'flex';
            }
            loadingEl.style.display = 'none';
            makeBtn.disabled = false;
        }
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

    // 自定义预设不再存 theme：#theme-selector 已移除，原逻辑必然取不到激活按钮、
    // 于是每条新预设都被写死成 'hollow_oak' 这个假值，applyCustomPreset 读它也是空转。
    const activeAnchors = Array.from(document.querySelectorAll('#anchor-selector .anchor-node.active'))
        .map(node => node.dataset.value);

    customPresets[trimmedName] = {
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
    // 全局兜底：拖文件进来时手一抖没落在放置区（帧/视频卡片、素材抽屉、图生图上传区）
    // 上，浏览器默认行为是直接打开这个文件、把整个页面连同进行中的任务一起冲掉。
    // 只拦文件拖拽（types 含 'Files'），纯文本拖拽照常——否则往提示词文本框里拖选中
    // 文字也会失灵。落在放置区里的拖放已被各自 preventDefault，冒泡到这里时
    // defaultPrevented 为真，直接放行不重复处理。
    ['dragover', 'drop'].forEach(eventName => {
        document.addEventListener(eventName, (e) => {
            if (e.defaultPrevented) return;
            const types = (e.dataTransfer && e.dataTransfer.types) || [];
            if (Array.prototype.indexOf.call(types, 'Files') === -1) return;
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'none';
        }, false);
    });

    // 拖放靶区从「点子库」抽屉搬到项目工作台列表（抽屉已于 P4 删除）
    const dropZone = document.getElementById('projects-list');
    if (!dropZone) return;

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, preventDefaults, false);
    });
    
    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }
    
    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => dropZone.classList.add('dragover'), false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => dropZone.classList.remove('dragover'), false);
    });

    dropZone.addEventListener('drop', handleDrop, false);
    
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
    
    // Alt + L / Alt + T：原本各自开合「点子库」与「任务列表」两个抽屉，两者合并
    // 进项目工作台后改为跳到工作台的对应筛选——快捷键的肌肉记忆保住，落点变成
    // 同一个页面的两档 chips。
    if (e.altKey && (e.key.toLowerCase() === 'l' || e.key.toLowerCase() === 't')) {
        e.preventDefault();
        if (typeof openProjectsWorkbench === 'function') {
            openProjectsWorkbench(e.key.toLowerCase() === 'l' ? 'saved' : 'running');
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

        const trendRefsManageModal = document.getElementById('trend-refs-manage-modal');
        if (trendRefsManageModal && trendRefsManageModal.classList.contains('active')) {
            const closeBtn = trendRefsManageModal.querySelector('.close-btn');
            if (closeBtn) closeBtn.click();
        }

        const beatOutlineModal = document.getElementById('beat-outline-modal');
        if (beatOutlineModal && beatOutlineModal.classList.contains('active')) {
            const closeBtn = beatOutlineModal.querySelector('.close-btn');
            if (closeBtn) closeBtn.click();
        }

        // 收起项目工作台的详情栏（两个右侧抽屉与卡片删除确认浮层已随 P4 删除）
        const detailClose = document.querySelector('#projects-detail .projects-detail-close');
        if (detailClose) detailClose.click();
    }
}

// =====================================================================
// 管线条 (Pipeline Bar)
// ---------------------------------------------------------------------
// 「封面 → 帧序列 → 视频 → 成片」四步的状态展示，外加一枚永远指向"当前该
// 做的下一步"的主按钮，取代原来 7 枚等权平铺按钮。
//
// 关键约定：四枚芯片就是原来那四个生成按钮本体（id 未变），所以这里
//   ① 只读状态、只写 class 与状态文字；
//   ② 绝不去写 disabled —— 那个属性归各自的生成流程所有（generateFrames /
//      hydrateFramesPanel / mergeVideos 都在写它），两边都写必然打架。
// 见 docs/spark_result_minimal_layout_plan.md
// =====================================================================

const PIPELINE_STEPS = ['cover', 'frames', 'videos', 'merge'];

const PIPELINE_STEP_NAME = {
    cover: '封面',
    frames: '帧序列',
    videos: '视频序列',
    merge: '成片',
};

const PIPELINE_NEXT_LABEL = {
    cover: '生成封面图',
    frames: '生成帧序列',
    videos: '生成视频序列',
    merge: '合并并加速视频',
};

// 已完成的步骤被再次点击时先确认——芯片摆在一条"看起来像导航"的条上，
// 而这两步动辄十几分钟且会覆盖已有结果。封面会保留历史记录、合并本身可
// 反复执行，都不拦。
const PIPELINE_RERUN_CONFIRM = {
    frames: '这一单的帧序列已全部生成，重新生成会覆盖现有全部帧图。确定重跑吗？',
    videos: '这一单的视频序列已全部生成，重新生成会覆盖现有全部片段。确定重跑吗？',
};

// mergeVideos 不走 ideaTasksById 登记（它是一次同步请求，不是流式任务），
// 合并期间的"进行中"只能靠这个标志位告诉管线条。
let mergeInFlight = false;

function computePipelineState(idea) {
    const blank = () => ({ done: false, busy: false, locked: true, have: 0, stat: '—' });
    if (!idea) {
        return { cover: blank(), frames: blank(), videos: blank(), merge: blank() };
    }

    const slots = (typeof resolvePromptSlots === 'function') ? resolvePromptSlots(idea) : [];
    const imageTotal = slots.filter(s => s.type === 'image').length;
    const videoTotal = slots.filter(s => s.type === 'video').length;

    const run = idea.frameRun || {};
    const framesHave = (run.frames || []).filter(f => f.url || f.file).length;
    const videosHave = (run.videos || []).filter(v => v.url || v.file).length;
    const coversHave = (idea.covers || []).length;
    const hasCover = coversHave > 0 || !!idea.activeCoverUrl;
    const mergedOk = !!(run.merged_video && run.merged_video.status === 'success');

    const busy = (type) => !!(typeof isIdeaTaskActive === 'function' && isIdeaTaskActive(idea.id, type));
    // 槽位数解析不出来时（极旧的库存条目没有 prompt_slots 也没有 prompt_block）
    // 退回"有就算数"，总比画一个 3/0 的假分母强。
    const ratio = (have, total) => `${have}/${total || have || '?'}`;

    return {
        cover: {
            done: hasCover,
            busy: busy('cover'),
            locked: false,
            have: coversHave,
            stat: busy('cover') ? '生成中…' : (coversHave ? `${coversHave} 张` : '未生成'),
        },
        frames: {
            done: imageTotal > 0 ? framesHave >= imageTotal : framesHave > 0,
            busy: busy('frames'),
            locked: !hasCover,
            have: framesHave,
            stat: (busy('frames') || framesHave)
                ? ratio(framesHave, imageTotal)
                : (!hasCover ? '待封面' : '未生成'),
        },
        videos: {
            done: videoTotal > 0 ? videosHave >= videoTotal : videosHave > 0,
            busy: busy('videos'),
            locked: framesHave === 0,
            have: videosHave,
            stat: (busy('videos') || videosHave)
                ? ratio(videosHave, videoTotal)
                : (framesHave === 0 ? '待帧序列' : '未生成'),
        },
        merge: {
            done: mergedOk,
            busy: mergeInFlight,
            locked: videosHave === 0,
            have: mergedOk ? 1 : 0,
            stat: mergeInFlight ? '合并中…'
                : (mergedOk ? '已合成' : (videosHave === 0 ? '待视频' : '未合并')),
        },
    };
}

/** 当前第一个未完成的步骤；全部完成返回 null。 */
function resolveNextPipelineStep(state) {
    return PIPELINE_STEPS.find(step => !state[step].done) || null;
}

function updatePipelineBar() {
    const bar = document.getElementById('pipeline-bar');
    if (!bar) return;

    const state = computePipelineState(typeof currentIdea !== 'undefined' ? currentIdea : null);
    const next = resolveNextPipelineStep(state);

    PIPELINE_STEPS.forEach(step => {
        const chip = bar.querySelector(`.pipeline-step[data-step="${step}"]`);
        if (!chip) return;
        const st = state[step];
        chip.classList.toggle('is-done', st.done && !st.busy);
        chip.classList.toggle('is-busy', st.busy);
        chip.classList.toggle('is-locked', st.locked && !st.done && !st.busy);
        chip.classList.toggle('is-next', step === next && !st.busy);
        chip.title = `${PIPELINE_STEP_NAME[step]} · ${st.stat}`;
        // 合并期间 mergeVideos 会整块换掉按钮的 innerHTML（转圈 + 文案），
        // 那段时间这个 span 不存在；跳过即可，合并结束后会再刷一次。
        const statEl = chip.querySelector('.step-stat');
        if (statEl) statEl.textContent = st.stat;
    });

    const nextBtn = document.getElementById('pipeline-next-btn');
    const nextText = document.getElementById('pipeline-next-text');
    if (!nextBtn || !nextText) return;

    const busyStep = PIPELINE_STEPS.find(step => state[step].busy);
    if (busyStep) {
        nextBtn.dataset.action = '';
        nextBtn.disabled = true;
        nextText.textContent = `${PIPELINE_STEP_NAME[busyStep]}生成中…`;
    } else if (!next) {
        nextBtn.dataset.action = 'download';
        nextBtn.disabled = false;
        nextText.textContent = '下载成品视频';
    } else {
        nextBtn.dataset.action = next;
        nextBtn.disabled = false;
        // 跑了一半（调试限量、中途取消、部分重试）时文案改成"继续"，
        // 免得看着像要从头再来一遍
        const partial = state[next].have > 0 && (next === 'frames' || next === 'videos');
        nextText.textContent = partial ? `继续${PIPELINE_NEXT_LABEL[next]}` : PIPELINE_NEXT_LABEL[next];
    }
}

/** 概览页以外（例如停在提示词页）先切回概览，否则目标 section 在隐藏面板里，滚动无效。 */
function scrollToPipelineSection(sectionId) {
    if (!sectionId) return;
    const overview = document.getElementById('tab-panel-overview');
    if (overview && !overview.classList.contains('active')) switchTab('overview');
    const el = document.getElementById(sectionId);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** 主按钮：把点击转交给"下一步"对应的那枚芯片（也就是原来的生成按钮）。 */
function runPipelineNext() {
    const btn = document.getElementById('pipeline-next-btn');
    const action = btn && btn.dataset.action;
    if (!action) return;
    if (action === 'download') {
        const link = document.getElementById('merged-video-download');
        scrollToPipelineSection('videos-section');
        if (link && link.getAttribute('href') && link.getAttribute('href') !== '#') link.click();
        return;
    }
    const chip = document.querySelector(`.pipeline-step[data-step="${action}"]`);
    if (chip) chip.click();
}

function initPipelineBar() {
    const bar = document.getElementById('pipeline-bar');
    if (!bar) return;

    // 捕获阶段拦一道：芯片本体就是生成按钮，点下去会真的重跑一遍。
    // 在祖先节点的捕获阶段 stopImmediatePropagation 能拦住按钮自己的 click 监听。
    bar.addEventListener('click', (e) => {
        const chip = e.target && e.target.closest ? e.target.closest('.pipeline-step') : null;
        if (!chip) return;
        const step = chip.dataset.step;
        const confirmText = PIPELINE_RERUN_CONFIRM[step];
        if (confirmText && chip.classList.contains('is-done') && !confirm(confirmText)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            return;
        }
        scrollToPipelineSection(chip.dataset.section);
    }, true);

    const nextBtn = document.getElementById('pipeline-next-btn');
    if (nextBtn) nextBtn.addEventListener('click', runPipelineNext);

    const moreBtn = document.getElementById('pipeline-more-btn');
    const moreMenu = document.getElementById('pipeline-more-menu');
    if (moreBtn && moreMenu) {
        const closeMenu = () => {
            moreMenu.hidden = true;
            moreBtn.setAttribute('aria-expanded', 'false');
        };
        moreBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const willOpen = moreMenu.hidden;
            moreMenu.hidden = !willOpen;
            moreBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        });
        // 菜单项的监听绑在按钮自己身上（冒泡先到它们），所以这里收菜单不会吞掉动作
        moreMenu.addEventListener('click', closeMenu);
        document.addEventListener('click', (e) => {
            if (moreMenu.hidden) return;
            if (moreBtn.contains(e.target) || moreMenu.contains(e.target)) return;
            closeMenu();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !moreMenu.hidden) closeMenu();
        });
    }

    // 「分步合成」入口：在 pipeline-more-menu 里点击后启动分步管线
    const steppedBtn = document.getElementById('stepped-pipeline-btn');
    if (steppedBtn) {
        steppedBtn.addEventListener('click', () => {
            if (typeof initSteppedPipeline !== 'function') {
                alert('分步管线模块未加载');
                return;
            }
            // loadedIdea 是全局变量，由 app.js 的 renderIdea/loadIdeaCard 设置
            if (typeof loadedIdea === 'undefined' || !loadedIdea || !loadedIdea.input_str) {
                alert('请先选择一个灵感卡片再使用分步合成');
                return;
            }
            const panel = document.getElementById('stepped-pipeline-panel');
            if (!panel) return;

            // 构建 dimensions，与主 compose 路径对齐
            const dims = {
                theme: loadedIdea.input_str,
                task_label: loadedIdea.task_label || loadedIdea.input_str,
                cover_url: loadedIdea.cover_url || null,
                english_title: loadedIdea.english_title || null,
                topic_dna: loadedIdea.topic_dna || null,
                llm_score: loadedIdea.llm_score ?? null,
                trend_ref: loadedIdea.trend_ref || null,
                trend_ref_ids: loadedIdea.trend_ref_ids || [],
                beat_outline: Array.isArray(loadedIdea.beat_outline) ? loadedIdea.beat_outline.slice() : [],
                pacing_skeleton: loadedIdea.pacing_skeleton || 'linear_milestone',
                beats_count: parseInt(document.getElementById('slider-beats')?.value || '10', 10),
                beats_floor: Number.isFinite(+(loadedIdea.beats_floor)) ? +loadedIdea.beats_floor : null,
            };

            // 切到结果概览页
            if (typeof switchMainTab === 'function') switchMainTab('results');
            if (typeof switchTab === 'function') switchTab('overview');

            initSteppedPipeline(panel, dims);
        });
    }

    updatePipelineBar();
}

/** section 标题栏的 ⚙ / ⓘ 弹层：同一个 section 里同时只展开一个。 */
function initSectionPops() {
    document.querySelectorAll('.section-tool-btn[data-pop]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const pop = document.getElementById(btn.dataset.pop);
            if (!pop) return;
            const willOpen = pop.hidden;
            const section = btn.closest('.idea-section');
            if (section) {
                section.querySelectorAll('.section-pop').forEach(p => { if (p !== pop) p.hidden = true; });
                section.querySelectorAll('.section-tool-btn').forEach(b => { if (b !== btn) b.classList.remove('active'); });
            }
            pop.hidden = !willOpen;
            btn.classList.toggle('active', willOpen);
        });
    });
}

// =====================================================================
// Tab Loading Dot Progress Perception
// =====================================================================
function updateTabStatusDot() {
    // 任务开始/结束都会走到这里（beginIdeaTask / endIdeaTask），顺手把管线条
    // 的三态刷一遍——这是"生成中/已完成"切换最可靠的那个时机。
    updatePipelineBar();

    const dot = document.getElementById('overview-status-dot');
    if (!dot) return;

    // 聚合跨所有创意的后台任务（不再只看当前这一个）——ideaTasksById 才是权威来源。
    const hasCover = anyActiveTaskOfType('cover');
    const hasFrames = anyActiveTaskOfType('frames');
    const hasVideos = anyActiveTaskOfType('videos');

    if (hasCover || hasFrames || hasVideos) {
        dot.style.display = 'inline-block';
        dot.className = 'tab-status-dot active';
        dot.title = `后台生成中: ${hasCover ? '封面图 ' : ''}${hasFrames ? '帧序列 ' : ''}${hasVideos ? '视频序列' : ''}`;
    } else {
        dot.style.display = 'inline-block';
        dot.className = 'tab-status-dot completed';
        dot.title = '后台任务已全部完成';
        setTimeout(() => {
            if (!anyActiveTaskOfType('cover') && !anyActiveTaskOfType('frames') && !anyActiveTaskOfType('videos')) {
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
// 缓存版本随卡片输出契约变化：4 起每条 idea 除 beat_outline
// 外还带 pacing_skeleton，旧缓存不能冒充成「已按骨架参考规划」的新卡。
// v5 作废「只带骨架标签、未经内容验收」时期产出的旧卡：那些卡可能
// 在仅勾 dual_payoff 时仍保存了单线 outline，服务端修好后也不能继续命中它们。
// v8 作废「双空间重置兑现」用自然/原地载体（冰洞、竖井、地窖）产出的旧卡：
// 该骨架现在要求人工运输载体（集装箱/校车/大巴/机身）在第一拍被装备运到现场落位，
// 旧缓存卡的第一拍是「清理」，与新契约对不上。
// v9 把该参考从“只借四幕结构”升级为埋地校车原片的施工顺序一比一复刻；旧卡缺少
// 入口井、逐层地面/墙顶和开门重置等明确阶段，不能继续冒充新骨架产物。
const IDEATION_CACHE_VERSION = '9-nested-space-literal-stage-order';
const DEFAULT_PACING_SKELETON_IDS = ['linear_milestone', 'dual_payoff', 'nested_space_payoff'];

function getSelectedPacingSkeletonIds() {
    const checked = Array.from(document.querySelectorAll('input[name="pacing-skeleton"]:checked'))
        .map(el => String(el.value || '').trim())
        .filter(id => DEFAULT_PACING_SKELETON_IDS.includes(id));
    return checked.length > 0 ? checked : DEFAULT_PACING_SKELETON_IDS.slice();
}

function initPacingSkeletonSelector() {
    const inputs = Array.from(document.querySelectorAll('input[name="pacing-skeleton"]'));
    if (inputs.length === 0) return;

    let stored = [];
    try {
        const parsed = JSON.parse(localStorage.getItem('ideation_pacing_skeleton_ids') || '[]');
        if (Array.isArray(parsed)) {
            stored = parsed.filter(id => DEFAULT_PACING_SKELETON_IDS.includes(id));
        }
    } catch (e) {
        stored = [];
    }
    if (stored.length > 0) {
        inputs.forEach(input => { input.checked = stored.includes(input.value); });
    }

    const updateNote = () => {
        const selected = inputs.filter(input => input.checked);
        const note = document.getElementById('pacing-skeleton-selected-note');
        if (!note) return;
        // 这行是骨架抽屉折叠态的唯一露出处，顺带点名启用了哪几套，
        // 不用展开就知道下一批灵感按什么节奏规划
        const names = (typeof pacingSkeletonLabel === 'function')
            ? selected.map(item => pacingSkeletonLabel(item.value)).join(' / ')
            : '';
        note.textContent = names
            ? `已启用 ${selected.length} 套 · ${names}`
            : `已启用 ${selected.length} 套`;
    };

    inputs.forEach(input => input.addEventListener('change', () => {
        let selected = inputs.filter(item => item.checked);
        if (selected.length === 0) {
            input.checked = true;
            selected = [input];
            showToast('至少保留一套推进节拍骨架', 'info');
        }
        const ids = selected.map(item => item.value);
        localStorage.setItem('ideation_pacing_skeleton_ids', JSON.stringify(ids));
        // 选择变了不立即发起昂贵的 LLM 请求；下次「换一批」/重载时
        // 由缓存绑定键自动判旧批次过期。
        updateNote();
        updateConfigSummary();
    }));
    updateNote();
}

// 一次激发是一串 LLM 调用（失败重试时可能跑上几分钟）。按钮上没有 disabled，
// 用户等不及连点几下就会并发出几路同样昂贵的请求，回来还互相覆盖卡片区。
let ideationInFlight = false;

// 缓存绑定用的「生成数设置」原值（'5' / 'random' …）。用原值而不是解析出来的张数：
// random 每次解析都不同，用解析值会让缓存永远失效；而交付张数可能少于请求张数，
// 用交付数比对又会每次进页面都重新激发一批。
function getIdeationCountKey() {
    const select = document.getElementById('ideation-count-select');
    return String((select && select.value) || localStorage.getItem('ideation_card_count') || '5');
}

function getRequestedIdeationCount() {
    const select = document.getElementById('ideation-count-select');
    let val = select ? select.value : (localStorage.getItem('ideation_card_count') || '5');
    if (val === 'random') {
        return Math.floor(Math.random() * 6) + 3; // 随机 3~8 张
    }
    const parsed = parseInt(val, 10);
    return (Number.isFinite(parsed) && parsed >= 1 && parsed <= 15) ? parsed : 5;
}

async function loadIdeationCards(force = false, options = {}) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    if (ideationInFlight) {
        if (typeof showToast === 'function') showToast('正在激发中，请稍候…', 'info');
        return;
    }
    const remixSeed = options && options.remixSeed ? options.remixSeed : null;
    const requestedCount = getRequestedIdeationCount();

    // 灵感推荐由联网参考案例库驱动（js/trend_refs.js）：勾选了参考就直接从选中
    // 案例取材；没勾选则后端自动联网搜索（结果沉淀回案例库）
    const selIds = remixSeed
        ? []
        : ((typeof getSelectedTrendRefIds === 'function') ? getSelectedTrendRefIds() : []);
    const selKey = selIds.slice().sort().join(',');
    const pacingSkeletonIds = getSelectedPacingSkeletonIds();
    const pacingSkeletonKey = pacingSkeletonIds.slice().sort().join(',');

    const countKey = getIdeationCountKey();

    if (!force && !remixSeed) {
        const cached = localStorage.getItem('ideation_cached_ideas');
        // 缓存与生成时勾选的联网参考集合、骨架、以及「生成数」设置绑定。
        // 生成数此前没进绑定键：改成 10 张之后不点「换一批」就还是那批旧卡，
        // 看起来就像「选了生成数也没用」。
        const cachedSel = localStorage.getItem('ideation_cached_trend_sel');
        const cachedSkeletons = localStorage.getItem('ideation_cached_pacing_skeletons');
        const cachedCount = localStorage.getItem('ideation_cached_count');
        const cachedVersion = localStorage.getItem('ideation_cache_version');
        if (cached && cachedSel === selKey && cachedSkeletons === pacingSkeletonKey
                && cachedCount === countKey
                && cachedVersion === IDEATION_CACHE_VERSION) {
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

    // 激发中不再把整块卡片区清空成一行文案：上一批还能看、还能点（选中态也留着），
    // 进度与秒表由激发轨的 ② 芯片承担（见 js/spark_rail.js）。空库时才铺骨架占位。
    const pendingMsg = remixSeed
        ? `正在以「${remixSeed.one_line || remixSeed.topic_dna || '台账创意'}」为母题激发二创方案`
        : selIds.length > 0
        ? `正在从选中的 ${selIds.length} 条联网参考案例中取材激发灵感 (${requestedCount} 张)`
        : `正在从案例库自动挑选参考激发灵感中 (${requestedCount} 张)`;
    renderIdeationPending(container, pendingMsg, requestedCount);
    ideationInFlight = true;
    if (typeof setSparkIdeating === 'function') setSparkIdeating(true);

    try {
        const response = await fetch('/api/ideate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                config: config,
                count: requestedCount,
                trend_ref_ids: selIds,
                pacing_skeleton_ids: pacingSkeletonIds,
                remix_seed: remixSeed
            })
        });

        const data = await response.json();
        if (data.status === 'ok' && Array.isArray(data.ideas) && data.ideas.length > 0) {
            currentIdeatedIdeas = data.ideas;
            currentIdeationTrendRefs = Array.isArray(data.trend_refs) ? data.trend_refs : [];
            if (!remixSeed) {
                localStorage.setItem('ideation_cached_ideas', JSON.stringify(data.ideas));
                localStorage.setItem('ideation_cached_trend_refs', JSON.stringify(currentIdeationTrendRefs));
                localStorage.setItem('ideation_cached_trend_sel', selKey);
                localStorage.setItem('ideation_cached_pacing_skeletons', pacingSkeletonKey);
                localStorage.setItem('ideation_cached_count', countKey);
                localStorage.setItem('ideation_cache_version', IDEATION_CACHE_VERSION);
            }
            renderIdeationCards(data.ideas);
            // 交付张数少于请求张数时说清原因：后端把没通过节拍骨架验收的候选丢掉了，
            // 不说的话用户只看到「选了 5 张却只回来 2 张」，只能怀疑生成数没生效。
            if (!remixSeed && data.ideas.length < requestedCount && typeof showToast === 'function') {
                showToast(`本批只产出 ${data.ideas.length} 张（请求 ${requestedCount} 张）：`
                    + '其余候选没通过所选节拍骨架的验收已被丢弃。想更容易凑齐可在细调条里'
                    + '同时勾上「单线里程碑推进」。', 'info');
            }
            if (typeof loadTrendRefs === 'function') loadTrendRefs();
        } else {
            renderIdeationFailure(container,
                data.message || (data.status === 'ok' ? '本次没有产出新的灵感卡片' : '未知错误'));
        }
    } catch (e) {
        console.error("Failed to load ideated cards:", e);
        renderIdeationFailure(container, '请检查网络或配置');
    } finally {
        ideationInFlight = false;
        if (typeof setSparkIdeating === 'function') setSparkIdeating(false);
    }
}

// 激发中的卡片区占位：已有卡片就原样留着，空库时按 requestedCount 铺骨架卡。
function renderIdeationPending(container, message, requestedCount = 5) {
    if (!container) return;
    const keepOld = !!container.querySelector('.ideation-card');
    const text = keepOld ? `${message}…（下方是上一批，仍可点选）` : `${message}…`;

    let strip = container.querySelector('.ideation-pending-strip');
    if (!keepOld) {
        container.innerHTML = '';
        strip = null;
        const count = Math.max(1, requestedCount || 5);
        for (let i = 0; i < count; i++) {
            const sk = document.createElement('div');
            sk.className = 'ideation-card-skeleton';
            container.appendChild(sk);
        }
    }
    if (!strip) {
        strip = document.createElement('div');
        strip.className = 'ideation-pending-strip';
        container.insertBefore(strip, container.firstChild);
    }
    strip.textContent = text;
}

/* 激发失败：上一批还在就留着（那是用户仅有的可点内容），只把顶部那条进度提示换成
   失败原因；一张都没有时才把骨架占位替换成错误块。以前这里无条件 innerHTML 覆写，
   等于失败一次就把上一批也一并抹掉。 */
function renderIdeationFailure(container, message) {
    if (!container) return;
    const keepOld = !!container.querySelector('.ideation-card');
    if (!keepOld) {
        container.innerHTML = `<div class="ideation-error">加载失败: ${message}</div>`;
        return;
    }
    let strip = container.querySelector('.ideation-pending-strip');
    if (!strip) {
        strip = document.createElement('div');
        strip.className = 'ideation-pending-strip';
        container.insertBefore(strip, container.firstChild);
    }
    strip.textContent = `激发失败: ${message}（下方仍是上一批，可继续点选）`;
    if (typeof showToast === 'function') showToast(`激发失败: ${message}`, 'error');
}

// Function renderIdeationCards moved to modular JS file

// Function selectIdeationCard moved to modular JS file

// Function composeIdeationCard moved to modular JS file

// Function mapEnglishCarrierToValue moved to modular JS file

/* 暗夜模式 toggle 已抽出到 js/theme_toggle.js(双前端共享,index.html 加载)*/

// Local Service Logs Stream
// Function initLocalServiceLogs moved to modular JS file

// --- LIGHTBOX SYSTEM 已整体抽出到 js/lightbox.js ---
// 全局控制器函数 + 状态(lightboxItems/lightboxActiveIndex)+ 初始化均在该共享模块;
// 本文件其余处仍直接调用全局 openLightbox()。
