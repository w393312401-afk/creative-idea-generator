/* 质量门禁已永久退役。保留旧接口供历史项目与设置导航调用，
   不再从 localStorage、服务端旧值或表单恢复任何审查规则。 */
window.GATE_SETTINGS_SPEC = null;

// 这些仅用于清理旧浏览器配置，不是可修改的规则默认值。
const RETIRED_GATE_KEYS = Object.freeze([
    'qaGateLevel', 'videoProcessVlmReview', 'videoAnchorVerify',
    'optimizeVideoPromptsBeforeGen', 'anchorInertiaAutoRetry', 'chainGuardMode',
    'frameContinuityMode', 'frameContinuityMaxRetries', 'strictFrameStateContract',
    'autoSplitHighRiskBeats', 'strictGates', 'strictPromptPipelineV2', 'frameContinuityLocalEdit',
]);

function normalizeRetiredGateConfig(target) {
    if (!target || typeof target !== 'object') return false;
    let changed = target.reviewsDisabled !== true;
    target.reviewsDisabled = true;
    const specs = Array.isArray(window.GATE_SETTINGS_SPEC) ? window.GATE_SETTINGS_SPEC : [];
    const keys = new Set([...RETIRED_GATE_KEYS, ...specs.map(spec => spec.key)]);
    keys.delete('reviewsDisabled');
    for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(target, key)) {
            delete target[key];
            changed = true;
        }
    }
    return changed;
}

function gateSettingServerValue(spec) {
    if (spec.key === 'reviewsDisabled') return true;
    if (spec.type === 'bool') return false;
    if (spec.type === 'int') return 0;
    return 'off';
}

function gateSettingCurrentValue(spec) {
    return gateSettingServerValue(spec);
}

function gateReviewsDisabled() {
    return true;
}

function renderGateReviewsMaster() {
    const input = document.getElementById('gate-setting-reviewsDisabled');
    if (input) {
        input.checked = true;
        input.disabled = true;
    }
    const status = document.getElementById('gate-reviews-status');
    if (status) status.textContent = '所有审查规则已永久退役';
}

// 兼容旧页面控件调用；即使旧事件被派发，也只清理配置，绝不写回启用值。
function applyGateSettingFromControl() {
    if (typeof config === 'object' && config) normalizeRetiredGateConfig(config);
}

function renderGateSettingsPanel() {
    if (typeof config === 'object' && config) normalizeRetiredGateConfig(config);
    renderGateReviewsMaster();
    for (const id of ['run-sequence-review-btn', 'run-full-sequence-review-btn']) {
        const button = document.getElementById(id);
        if (button) {
            button.hidden = true;
            button.disabled = true;
        }
    }
    const host = document.getElementById('gate-settings-list');
    if (!host) return;
    host.textContent = '';
    host.dataset.status = 'retired';
    const specs = window.GATE_SETTINGS_SPEC;
    if (!Array.isArray(specs) || !specs.length) return;
    const list = document.createElement('ul');
    list.className = 'settings-hint';
    for (const spec of specs) {
        if (spec.key === 'reviewsDisabled') continue;
        const item = document.createElement('li');
        item.dataset.gateKey = spec.key;
        item.textContent = `${spec.label || spec.key}：已退役`;
        list.appendChild(item);
    }
    host.appendChild(list);
}

function openSettingsToGate() {
    if (typeof openSettingsModal === 'function') {
        openSettingsModal();
    } else {
        const modal = document.getElementById('settings-modal');
        if (modal) modal.style.display = 'flex';
    }
    const navBtn = document.querySelector('#settings-nav [data-section="gates"]');
    if (navBtn) navBtn.click();
    renderGateSettingsPanel();
}

function resetGateSettings() {
    if (typeof config === 'object' && config) normalizeRetiredGateConfig(config);
    renderGateSettingsPanel();
}

if (typeof window !== 'undefined') {
    window.openSettingsToGate = openSettingsToGate;
}
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        gateSettingCurrentValue, gateSettingServerValue, normalizeRetiredGateConfig,
        applyGateSettingFromControl, renderGateSettingsPanel, resetGateSettings,
        openSettingsToGate, gateReviewsDisabled,
    };
}
