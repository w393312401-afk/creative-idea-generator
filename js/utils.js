// --- utils.js ---

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function _setAccessHeader(init, code) {
    const headers = new Headers((init && init.headers) || {});
    headers.set('X-Access-Code', code);
    init.headers = headers;
    return init;
}

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

// 多行文本输入弹窗（customPrompt 的长文本版）：用于人工描述帧问题这类需要写
// 一整句话、还想在"只记录"和"记录并立即修复"之间选的场景。resolve
// {value, action}，action 为 'confirm' | 'extra'；取消/关闭 resolve null。
// 与 customPrompt 的另一处区别：message/defaultValue 一律走 textContent/.value
// 赋值而不是拼进 innerHTML——这里的默认值可能是上一次记录的问题描述（含引号、
// 尖括号的自由文本），拼 HTML 会被它撑破。
function customTextarea({ title = '输入内容', message = '', defaultValue = '', placeholder = '',
                          confirmLabel = '确定', extraLabel = '', rows = 5 } = {}) {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.id = 'custom-textarea-modal';
        modal.style.zIndex = '1100';

        modal.innerHTML = `
            <div class="modal-content glass-panel" style="max-width: 520px; border-color: var(--neon-cyan);">
                <div class="modal-header">
                    <h3 class="ta-title"></h3>
                    <button class="close-btn">&times;</button>
                </div>
                <div class="modal-body" style="padding-top: 10px;">
                    <p class="ta-message" style="margin-bottom: 12px; font-size: 13px; line-height: 1.6; color: var(--text-secondary); white-space: pre-wrap;"></p>
                    <div class="form-group" style="margin-bottom: 0;">
                        <textarea id="custom-textarea-input" rows="${rows}" style="width:100%; border-color: rgba(255,255,255,0.15); resize: vertical;"></textarea>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="action-btn text-btn secondary cancel-btn">取消</button>
                    ${extraLabel ? '<button class="action-btn text-btn secondary extra-btn"></button>' : ''}
                    <button class="action-btn text-btn primary confirm-btn" style="background: var(--neon-cyan); border-color: rgba(0,242,254,0.4); color: #000; font-weight:700;"></button>
                </div>
            </div>
        `;

        modal.querySelector('.ta-title').textContent = title;
        modal.querySelector('.ta-message').textContent = message;
        modal.querySelector('.confirm-btn').textContent = confirmLabel;
        const extraBtn = modal.querySelector('.extra-btn');
        if (extraBtn) extraBtn.textContent = extraLabel;

        document.body.appendChild(modal);

        const input = modal.querySelector('#custom-textarea-input');
        input.value = defaultValue || '';
        input.placeholder = placeholder;
        input.focus();
        input.select();

        const close = () => {
            modal.classList.remove('active');
            setTimeout(() => modal.remove(), 200);
        };
        const finish = (action) => {
            const val = input.value;
            close();
            resolve(action ? { value: val, action } : null);
        };

        modal.querySelector('.close-btn').addEventListener('click', () => finish(null));
        modal.querySelector('.cancel-btn').addEventListener('click', () => finish(null));
        modal.querySelector('.confirm-btn').addEventListener('click', () => finish('confirm'));
        if (extraBtn) extraBtn.addEventListener('click', () => finish('extra'));

        // Enter 在多行输入里是换行，提交要按 Ctrl/Cmd+Enter
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                finish('confirm');
            } else if (e.key === 'Escape') {
                finish(null);
            }
        });
    });
}

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

// duration：可选停留毫秒数，默认 3 秒。少数"配置层面出了问题、需要人真的读完
// 一句话才能修"的提示（如技能契约缺失）3 秒不够看完。
function showToast(message, type = 'success', duration = 3000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = { success: '✓', error: '✗', warning: '⚠', info: 'ℹ' };
    const iconEl = document.createElement('span');
    iconEl.className = 'toast-icon';
    iconEl.textContent = icons[type] || icons.info;
    const msgEl = document.createElement('span');
    msgEl.className = 'toast-message';
    // textContent: messages often embed server/LLM error strings — never inject as HTML
    msgEl.textContent = message;
    toast.append(iconEl, ' ', msgEl);

    container.appendChild(toast);

    // Use CSS class for exit (avoids JS writing style.opacity/transform → forced layout)
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 200);
    }, Number.isFinite(+duration) && +duration > 0 ? +duration : 3000);
}

function showManualInterventionBanner(message) {
    const banner = document.getElementById('manual-intervention-banner');
    if (!banner) return;
    const textEl = document.getElementById('manual-intervention-text');
    if (textEl) textEl.textContent = message;
    banner.style.display = 'flex';
}

function hideManualInterventionBanner() {
    const banner = document.getElementById('manual-intervention-banner');
    if (banner) banner.style.display = 'none';
}

// 视频/帧序列生成的 google_fx 自动化脚本检测到登录失效/验证码/安全拦截时会
// 暂停轮询等人工在 AdsPower 浏览器窗口处理，而不是直接判整批失败——这里把
// 该状态转成常驻横幅（不像 toast 那样几秒自动消失），避免人工没注意到就
// 让脚本干等到超时。三个 phase 对应 wait_out_manual_intervention 的
// detected/cleared/timeout。
const MANUAL_INTERVENTION_CODE_LABELS = {
    login_required: '登录失效',
    captcha_required: '需要验证码',
    verification_required: '需要二次验证',
    security_check: '触发安全拦截',
};

function handleManualInterventionEvent(type, data) {
    const code = (data && data.code) || '';
    const reason = (data && data.reason) || '';
    const codeLabel = MANUAL_INTERVENTION_CODE_LABELS[code] || code || '需要人工处理';

    if (type === 'manual_intervention_detected') {
        const maxWaitMin = Math.max(1, Math.round(((data && data.max_wait_secs) || 1200) / 60));
        showManualInterventionBanner(
            `🛑 Google Flow ${codeLabel}${reason ? `（${reason}）` : ''}——请打开 AdsPower 浏览器窗口手动处理，` +
            `处理完成后脚本会自动继续（最长等待 ${maxWaitMin} 分钟）`
        );
        showToast(`需要人工处理：${codeLabel}，脚本已暂停等待`, 'warning');
    } else if (type === 'manual_intervention_cleared') {
        hideManualInterventionBanner();
        showToast('人工处理已完成，自动继续生成', 'success');
    } else if (type === 'manual_intervention_timeout') {
        hideManualInterventionBanner();
        showToast(`等待人工处理超时（${codeLabel}），相关任务已标记失败`, 'error');
    }
}

/**
 * Drives one of the page's progress-bar triplets:
 *   #<prefix>-progress-label / #<prefix>-progress-percent / #<prefix>-progress-fill
 * (prefix: 'generation' | 'frames' | 'videos').
 * Null-safe by design: a missing DOM node or malformed info must never be able
 * to crash a stream-consumer loop.
 */
function setProgressBar(prefix, info) {
    if (!prefix) return;
    const label = document.getElementById(`${prefix}-progress-label`);
    const percentEl = document.getElementById(`${prefix}-progress-percent`);
    const fill = document.getElementById(`${prefix}-progress-fill`);
    const pct = Math.max(0, Math.min(100, Number(info && info.percent) || 0));
    if (label && info && info.label) label.textContent = info.label;
    if (percentEl) percentEl.textContent = `${Math.round(pct)}%`;
    if (fill) fill.style.width = `${pct}%`;

    // 帧/视频每推进一格，管线条上的「3/16」也跟着走一格（app.js 提供；
    // 生成流程里这个函数调用得很频繁，updatePipelineBar 本身是纯读 + 改几个
    // class/文本，代价与这里已有的 DOM 写入同量级）。
    if (typeof updatePipelineBar === 'function') updatePipelineBar();
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

/**
 * Returns the publish-ready social caption lines for an idea.
 * english: TikTok 整行（英文标题+英文tags，可原样粘贴）；旧数据退回封面 hook / 中文标题
 * chinese: 国内社媒整行（中文标题+中文话题）；旧数据退回中文标题
 */
function getIdeaTikTokMeta(idea) {
    if (!idea) return { english: '', chinese: '' };
    return {
        english: idea.social_title_en || idea.english_title || idea.title || '',
        chinese: idea.social_title_cn || idea.title || '',
    };
}

/**
 * 把一整行社媒标题拆成「主标题」与「话题串」两段：
 *   "Secret Treehouse Build #TinyHome #DIY" → { name: "Secret Treehouse Build", tags: "#TinyHome #DIY" }
 * 结果头部渲染时分成两个 span，手机折叠态只留主标题、把话题串整段藏起来
 * （话题串常常比标题本身还长，在窄屏里能吃掉三四行首屏）。
 * 找不到话题时 tags 为空串，主标题原样返回。
 */
function splitSocialTitle(text) {
    const s = (text || '').trim();
    // 第一个「#紧跟非空白」处即话题串起点；标题正文里的孤立 # 不算
    const idx = s.search(/#\S/);
    if (idx < 0) return { name: s, tags: '' };
    return { name: s.slice(0, idx).trim(), tags: s.slice(idx).trim() };
}

/**
 * Returns the canonical save-title used as the server-side project directory key.
 * New ideas use a per-compose project_key so two model runs of the same creative
 * never share frames/videos/manifest. Old saved ideas fall back to their title.
 */
function getIdeaSaveTitle(idea) {
    if (!idea) return '';
    return idea.project_key || idea.title || '';
}

