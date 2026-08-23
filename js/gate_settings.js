/* ── 质量门禁开关面板 ─────────────────────────────────────────────────
   配置中心「质量门禁」分区的控件全部由这里按服务端下发的表动态渲染。

   为什么不在 HTML 里写死控件、在 state.js 里写死默认值：
   门禁项此前散在四处手工同步（消费点的 config.get / effective_config 白名单 /
   前端默认值 / server_config.example.json 注释），任何一处漏掉就是一次「配了但
   从未生效」的静默失效——qaGateLevel、imageEditTransport、skillProfile 各栽过
   一次，videoProcessVlmReview 和 strictGates 一直漏到 2026-08-10。现在唯一真源
   是 server_common.GATE_SETTINGS，经 /api/mode 的 gate_settings 字段下发，前端
   只渲染、不复制。新增门禁项前端一行都不用改。

   落盘口径与配置中心其余控件一致：改动即存（autoSaveConfig），值写进
   localStorage 的 config，随每个生成请求带到服务端。
   ──────────────────────────────────────────────────────────────────── */

// /api/mode 下发的门禁表（app.js initServerMode 填）。拿不到时保持 null——
// 面板显示"读取失败"而不是拿一份猜出来的默认值渲染。
window.GATE_SETTINGS_SPEC = null;

function gateSettingServerValue(spec) {
    return Object.prototype.hasOwnProperty.call(spec, 'server_value')
        ? spec.server_value : spec.default;
}

/* 当前生效值：localStorage 里存过就用存的，否则用服务端下发的生效值。
   注意 false 与"没存过"必须分开判断——用 || 兜底会让"显式关掉某项"每次
   打开面板都被服务端默认值顶回开启，正是这套配置踩过的同一类坑。 */
function gateSettingCurrentValue(spec) {
    if (typeof config === 'object' && config
            && Object.prototype.hasOwnProperty.call(config, spec.key)
            && config[spec.key] !== null && config[spec.key] !== undefined) {
        return config[spec.key];
    }
    return gateSettingServerValue(spec);
}

let gateSettingsSearchQuery = '';
let gateSettingsActiveFilter = 'all';
let gateSettingsToolbarBound = false;

const GATE_SECTION_META = {
    prompt: {
        title: '阶段 1 · 提示词生成与状态契约门禁',
        desc: '母案提示词交付生图前的单拍变化硬校验（before/delta/after）及高风险拍拆分',
        icon: '📝',
        badge: '提示词阶段',
    },
    frame: {
        title: '阶段 2 · 关键帧图像渲染与连续性门禁',
        desc: '关键帧生图阶段的视角防漂移检测、桥接帧惯性防固化与自动重试策略',
        icon: '🖼️',
        badge: '帧生图阶段',
    },
    video: {
        title: '阶段 3 · 视频生成、画面差量优化与质检门禁',
        desc: 'I2V 生成前真实画面差量智能重写（核心防跳变）、首尾锚点校验与 VLM 过程复审',
        icon: '🎬',
        badge: '视频生成阶段',
    },
    env: {
        title: '全局 · 运行环境与容错降级策略',
        desc: '底层 AI 网关、模型或本地工具异常时的全局故障安全与降级放行策略',
        icon: '🛡️',
        badge: '全局兜底',
    },
};

const GATE_KEY_META = {
    optimizeVideoPromptsBeforeGen: { icon: '✨', badge: '🔥 核心防跳变' },
    videoAnchorVerify: { icon: '🔒', badge: '⚡ 零成本防串片' },
    videoProcessVlmReview: { icon: '🤖', badge: 'VLM 过程复审' },
    qaGateLevel: { icon: '🎯', badge: '质检总闸' },
    anchorInertiaAutoRetry: { icon: '🪄', badge: '桥接帧增强' },
    frameContinuityMode: { icon: '📐', badge: '机位锁死' },
    frameContinuityMaxRetries: { icon: '🔁', badge: '重试上限' },
    strictFrameStateContract: { icon: '📋', badge: '状态契约' },
    autoSplitHighRiskBeats: { icon: '✂️', badge: '高风险拆拍' },
    strictGates: { icon: '🚨', badge: 'Fail-Closed' },
};

function gateSettingControl(spec, current) {
    const wrap = document.createElement('div');
    wrap.className = 'gate-setting-card';
    wrap.dataset.gateKey = spec.key;
    wrap.dataset.section = spec.section || 'env';

    const headerRow = document.createElement('div');
    headerRow.className = 'gate-setting-header-row';

    const labelWrap = document.createElement('div');
    labelWrap.className = 'gate-setting-label-wrap';

    const meta = GATE_KEY_META[spec.key] || {};
    const iconSpan = document.createElement('span');
    iconSpan.textContent = meta.icon || '⚙️';
    labelWrap.appendChild(iconSpan);

    const label = document.createElement('label');
    label.className = 'gate-setting-label';
    label.setAttribute('for', `gate-setting-${spec.key}`);
    label.textContent = spec.label || spec.key;
    labelWrap.appendChild(label);

    if (meta.badge) {
        const pill = document.createElement('span');
        pill.className = 'gate-setting-pill';
        pill.textContent = meta.badge;
        labelWrap.appendChild(pill);
    }
    headerRow.appendChild(labelWrap);

    const controlWrap = document.createElement('div');
    controlWrap.className = 'gate-setting-control-wrap';

    let input;
    if (spec.type === 'bool') {
        input = document.createElement('select');
        for (const [value, text] of [['true', '开启'], ['false', '关闭']]) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = text;
            input.appendChild(opt);
        }
        input.value = current ? 'true' : 'false';
    } else if (spec.type === 'enum') {
        input = document.createElement('select');
        for (const value of (spec.options || [])) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = (spec.option_labels && spec.option_labels[value]) || value;
            input.appendChild(opt);
        }
        input.value = String(current);
    } else {
        input = document.createElement('input');
        input.type = 'number';
        if (spec.min !== undefined) input.min = String(spec.min);
        if (spec.max !== undefined) input.max = String(spec.max);
        input.value = String(current);
    }
    input.id = `gate-setting-${spec.key}`;
    controlWrap.appendChild(input);
    headerRow.appendChild(controlWrap);
    wrap.appendChild(headerRow);

    if (spec.hint) {
        const hint = document.createElement('small');
        hint.className = 'gate-setting-hint';
        hint.textContent = spec.hint;
        wrap.appendChild(hint);
    }

    if (spec.server_pinned) {
        const pinned = document.createElement('small');
        pinned.className = 'gate-setting-hint gate-setting-pinned';
        pinned.textContent = `⚙️ 服务端 server_config.json 已钉死本项为 `
            + `${String(gateSettingServerValue(spec))}；此处的值只对本浏览器发起的生成生效。`;
        wrap.appendChild(pinned);
    }

    input.addEventListener('change', () => {
        applyGateSettingFromControl(spec, input);
        if (typeof autoSaveConfig === 'function') autoSaveConfig();
    });
    return wrap;
}

function applyGateSettingFromControl(spec, input) {
    if (typeof config !== 'object' || !config) return;
    if (spec.type === 'bool') {
        config[spec.key] = input.value === 'true';
    } else if (spec.type === 'int') {
        const parsed = parseInt(input.value, 10);
        if (Number.isNaN(parsed)) {
            config[spec.key] = gateSettingServerValue(spec);
        } else {
            const lo = spec.min !== undefined ? spec.min : parsed;
            const hi = spec.max !== undefined ? spec.max : parsed;
            config[spec.key] = Math.max(lo, Math.min(hi, parsed));
        }
        input.value = String(config[spec.key]);
    } else {
        config[spec.key] = input.value;
    }
}

function _initGateSettingsToolbar() {
    if (gateSettingsToolbarBound) return;
    const searchInput = document.getElementById('gate-settings-search');
    const clearBtn = document.getElementById('gate-settings-search-clear');
    const chipsContainer = document.getElementById('gate-filter-chips');

    if (searchInput) {
        searchInput.addEventListener('input', () => {
            gateSettingsSearchQuery = searchInput.value.trim().toLowerCase();
            if (clearBtn) clearBtn.style.display = gateSettingsSearchQuery ? 'inline-block' : 'none';
            renderGateSettingsPanel();
        });
    }

    if (clearBtn && searchInput) {
        clearBtn.addEventListener('click', () => {
            searchInput.value = '';
            gateSettingsSearchQuery = '';
            clearBtn.style.display = 'none';
            renderGateSettingsPanel();
            searchInput.focus();
        });
    }

    if (chipsContainer) {
        chipsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.gate-filter-chip');
            if (!btn) return;
            const filter = btn.dataset.filter || 'all';
            gateSettingsActiveFilter = filter;
            chipsContainer.querySelectorAll('.gate-filter-chip').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            renderGateSettingsPanel();
        });
    }

    gateSettingsToolbarBound = true;
}

function renderGateSettingsPanel() {
    const host = document.getElementById('gate-settings-list');
    if (!host) return;
    _initGateSettingsToolbar();

    host.textContent = '';
    const specs = window.GATE_SETTINGS_SPEC;
    if (!Array.isArray(specs) || !specs.length) {
        const err = document.createElement('p');
        err.className = 'settings-hint';
        err.textContent = '读取服务端门禁配置失败（/api/mode 无 gate_settings 字段）。'
            + '这通常是服务端还在跑旧代码——重启后端服务后重新打开本页。'
            + '在此期间各门禁按服务端默认值运行，不受影响。';
        host.appendChild(err);
        return;
    }

    const bySection = new Map();
    for (const spec of specs) {
        const section = spec.section || 'env';
        if (!bySection.has(section)) bySection.set(section, []);
        bySection.get(section).push(spec);
    }

    const query = gateSettingsSearchQuery;
    const filter = gateSettingsActiveFilter;
    let totalRendered = 0;

    // 按生成链路的时间顺序分组显示：编排 → 渲帧 → 出片 → 环境
    for (const section of ['prompt', 'frame', 'video', 'env']) {
        if (filter !== 'all' && filter !== section) continue;

        const items = bySection.get(section);
        if (!items || !items.length) continue;

        const secMeta = GATE_SECTION_META[section] || { title: section, desc: '', icon: '🛡️', badge: section };
        
        // 过滤匹配的项
        const matchedItems = items.filter(spec => {
            if (!query) return true;
            const text = `${spec.key} ${spec.label || ''} ${spec.hint || ''} ${secMeta.title} ${secMeta.desc}`.toLowerCase();
            return text.includes(query);
        });

        if (!matchedItems.length) continue;

        totalRendered += matchedItems.length;

        const group = document.createElement('div');
        group.className = 'gate-settings-group';

        const header = document.createElement('div');
        header.className = 'gate-settings-group-header';

        const titleRow = document.createElement('div');
        titleRow.className = 'gate-settings-group-title-row';

        const titleEl = document.createElement('h5');
        titleEl.className = 'gate-settings-group-title';
        titleEl.textContent = secMeta.title;
        titleRow.appendChild(titleEl);

        const badgeEl = document.createElement('span');
        badgeEl.className = 'gate-settings-group-badge';
        badgeEl.textContent = `${matchedItems.length} 项开关`;
        titleRow.appendChild(badgeEl);

        header.appendChild(titleRow);

        if (secMeta.desc) {
            const descEl = document.createElement('p');
            descEl.className = 'gate-settings-group-desc';
            descEl.textContent = secMeta.desc;
            header.appendChild(descEl);
        }

        group.appendChild(header);

        for (const spec of matchedItems) {
            group.appendChild(gateSettingControl(spec, gateSettingCurrentValue(spec)));
        }

        host.appendChild(group);
    }

    if (totalRendered === 0) {
        const empty = document.createElement('div');
        empty.className = 'gate-empty-search';
        empty.innerHTML = `<p>🔍 未找到与 "<b>${query}</b>" 匹配的门禁开关</p>`
            + `<button type="button" class="text-btn" id="gate-reset-search-btn">清空搜索条件</button>`;
        host.appendChild(empty);

        const resetBtn = document.getElementById('gate-reset-search-btn');
        if (resetBtn) {
            resetBtn.addEventListener('click', () => {
                const searchInput = document.getElementById('gate-settings-search');
                if (searchInput) searchInput.value = '';
                gateSettingsSearchQuery = '';
                gateSettingsActiveFilter = 'all';
                const chipsContainer = document.getElementById('gate-filter-chips');
                if (chipsContainer) {
                    chipsContainer.querySelectorAll('.gate-filter-chip').forEach(c => {
                        c.classList.toggle('active', c.dataset.filter === 'all');
                    });
                }
                const clearBtn = document.getElementById('gate-settings-search-clear');
                if (clearBtn) clearBtn.style.display = 'none';
                renderGateSettingsPanel();
            });
        }
    }
}

/* 快速定位并高亮指定门禁项（如从控制台或快捷按钮调用） */
function openSettingsToGate(gateKey) {
    if (typeof openSettingsModal === 'function') {
        openSettingsModal();
    } else {
        const modal = document.getElementById('settings-modal');
        if (modal) modal.style.display = 'flex';
    }

    // 切换到「质量门禁」标签页
    const navBtn = document.querySelector('#settings-nav [data-section="gates"]');
    if (navBtn) navBtn.click();

    // 清空搜索与过滤
    gateSettingsSearchQuery = '';
    gateSettingsActiveFilter = 'all';
    const searchInput = document.getElementById('gate-settings-search');
    if (searchInput) searchInput.value = '';
    const chips = document.getElementById('gate-filter-chips');
    if (chips) {
        chips.querySelectorAll('.gate-filter-chip').forEach(c => {
            c.classList.toggle('active', c.dataset.filter === 'all');
        });
    }

    renderGateSettingsPanel();

    setTimeout(() => {
        const target = document.querySelector(`.gate-setting-card[data-gate-key="${gateKey}"]`);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.classList.remove('gate-setting-highlight');
            // 触发重绘以重启动画
            void target.offsetWidth;
            target.classList.add('gate-setting-highlight');
        }
    }, 120);
}

/* 恢复默认：把本地存的门禁项整个删掉，让它们退回服务端下发的生效值——
   而不是写入一份前端猜的默认值（那又是一份会漂移的真相源）。 */
function resetGateSettings() {
    const specs = window.GATE_SETTINGS_SPEC;
    if (!Array.isArray(specs) || typeof config !== 'object' || !config) return;
    for (const spec of specs) delete config[spec.key];
    renderGateSettingsPanel();
}

if (typeof window !== 'undefined') {
    window.openSettingsToGate = openSettingsToGate;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        gateSettingCurrentValue, gateSettingServerValue,
        applyGateSettingFromControl, renderGateSettingsPanel, resetGateSettings,
        openSettingsToGate,
    };
}
