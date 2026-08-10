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

const GATE_SECTION_LABELS = {
    prompt: '提示词编排阶段',
    frame: '帧渲染阶段',
    video: '视频阶段',
    env: '环境与兜底',
};

function gateSettingControl(spec, current) {
    const wrap = document.createElement('div');
    wrap.className = 'form-group settings-field gate-setting-field';
    wrap.dataset.gateKey = spec.key;

    const label = document.createElement('label');
    label.setAttribute('for', `gate-setting-${spec.key}`);
    label.textContent = spec.label || spec.key;
    wrap.appendChild(label);

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
    wrap.appendChild(input);

    if (spec.hint) {
        const hint = document.createElement('small');
        hint.className = 'settings-hint';
        // LLM/服务端文案一律 textContent，不走 innerHTML
        hint.textContent = spec.hint;
        wrap.appendChild(hint);
    }

    // 服务端 server_config.json 里钉死了这一项：浏览器改的值仍然会随请求带过去
    // 并覆盖（effective_config 里请求 config 优先），但要让用户知道服务端有个
    // 不一样的基线值——否则"我没改过为什么行为不一样"永远查不出来。
    if (spec.server_pinned) {
        const pinned = document.createElement('small');
        pinned.className = 'settings-hint gate-setting-pinned';
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

function renderGateSettingsPanel() {
    const host = document.getElementById('gate-settings-list');
    if (!host) return;
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
    // 按生成链路的时间顺序分组显示：编排 → 渲帧 → 出片 → 环境
    for (const section of ['prompt', 'frame', 'video', 'env']) {
        const items = bySection.get(section);
        if (!items || !items.length) continue;
        const title = document.createElement('h5');
        title.className = 'gate-settings-group-title';
        title.textContent = GATE_SECTION_LABELS[section] || section;
        host.appendChild(title);
        for (const spec of items) {
            host.appendChild(gateSettingControl(spec, gateSettingCurrentValue(spec)));
        }
    }
}

/* 恢复默认：把本地存的门禁项整个删掉，让它们退回服务端下发的生效值——
   而不是写入一份前端猜的默认值（那又是一份会漂移的真相源）。 */
function resetGateSettings() {
    const specs = window.GATE_SETTINGS_SPEC;
    if (!Array.isArray(specs) || typeof config !== 'object' || !config) return;
    for (const spec of specs) delete config[spec.key];
    renderGateSettingsPanel();
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        gateSettingCurrentValue, gateSettingServerValue,
        applyGateSettingFromControl, renderGateSettingsPanel, resetGateSettings,
    };
}
