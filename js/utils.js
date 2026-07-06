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

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    // Add icon
    const icon = type === 'success' ? '✓' : '✗';
    toast.innerHTML = `<span class="toast-icon">${icon}</span> <span class="toast-message">${message}</span>`;
    
    container.appendChild(toast);
    
    // Use CSS class for exit (avoids JS writing style.opacity/transform → forced layout)
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 200);
    }, 3000);
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

