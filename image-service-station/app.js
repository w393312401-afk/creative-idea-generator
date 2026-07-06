/* ==========================================================================
   SPARK Studio - Decoupled Image Service Station Interactive Logic
   ========================================================================== */

// Global State
let currentTab = 't2i'; // 't2i' or 'i2i'
let selectedT2iRatio = '16:9';
let selectedT2iQuality = '2K';
let selectedI2iRatio = 'auto';
let selectedI2iQuality = '1K';
let uploadedFiles = []; // Array of { name, file, base64 }
let imageHistory = []; // Array of { id, type, prompt, model, ratio, quality, timestamp, image }
let taskList = []; // Array of { id, type, prompt, model, ratio, quality, status, error, image, timestamp, controller, extraData }
let serverConfig = {
    baseUrl: '',
    apiKey: '',
    accessCode: ''
};

async function fetchWithTimeout(url, options = {}, timeoutMs = 120000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, {
            ...options,
            signal: controller.signal
        });
    } catch (error) {
        if (error && error.name === 'AbortError') {
            const seconds = Math.round(timeoutMs / 1000);
            throw new Error(`请求超时：图像服务超过 ${seconds} 秒未返回，请稍后重试或换一张更小的参考图。`);
        }
        throw error;
    } finally {
        clearTimeout(timeoutId);
    }
}

// Curated Creative Prompts for Random Generator
const CREATIVE_PROMPTS = [
    {
        zh: "一座隐藏在巨型橡树内部的复古图书馆，温暖的阳光穿过树叶间的缝隙照亮飞舞的微尘，空中漂浮着几本发光的魔法书，精致的木质雕花楼梯，维多利亚风格，极高细节，写实摄影质感",
        en: "A retro library hidden inside a giant hollow oak tree, warm sunlight filtering through leaves illuminating floating dust motes, glowing magic books floating in the air, intricate carved wooden staircases, Victorian style, hyper-detailed, photorealistic photography."
    },
    {
        zh: "蓝冰冰川深处的科幻风格研究基地，半透明的冰墙透出幽蓝色的微光，未来主义的实验仪器，透明的液晶显示屏，一名科学家在操作全息图像，电影级光影，极简科技感，8k分辨率",
        en: "A sci-fi research base deep inside a blue glacier cave, translucent ice walls emitting a mysterious blue glow, futuristic laboratory instruments, transparent LCD screens, a scientist operating a holographic display, cinematic lighting, minimalist high-tech, 8k resolution."
    },
    {
        zh: "退役潜艇改装的深海蒸汽朋克风格酒吧，圆形的潜水窗外可以看到游动的发光水母，铜质管道，闪烁的仪表盘，温暖的琥珀色灯光，木质吧台，复古舒适，胶片质感，超清",
        en: "A cozy steampunk bar converted from a retired submarine cabin, circular portholes showing glowing jellyfish swimming outside, brass pipes, flickering gauges, warm amber lighting, wooden bar counter, vintage aesthetic, film grain texture, ultra-high definition."
    },
    {
        zh: "在河畔斜坡上搭建的一座圆锥形树皮屋，粗糙的木柱作为骨架，外侧铺满深灰色树皮瓦，黄昏时分屋里点亮温暖的马灯，温暖舒适，写实摄影质感",
        en: "A conical bark hut built on a rocky riverside slope, rough wooden poles forming the frame, dark grey bark shingles covering the exterior, a warm lantern glowing inside at dusk, cozy atmosphere, photorealistic photography."
    },
    {
        zh: "废弃水塔顶部改造而成的工业风奢华阁楼，360度环形玻璃窗可以俯瞰雨后的纽约落日，混凝土粗犷质感与高档现代家具完美融合，暖色调软装，落日余晖，极高画质",
        en: "An industrial luxury loft penthouse converted from an abandoned water tower top, 360-degree circular glass windows overlooking a rainy New York sunset, raw concrete textures blended with premium modern furniture, warm interior, golden hour glow, cinematic render."
    },
    {
        zh: "一只穿着航天服的可爱英短猫咪，漂浮在五彩斑斓的太空星云中，爪子里抓着一包太空小鱼干，头盔面罩上倒映着绚丽的超新星爆发，皮克斯风格，3D渲染，可爱，极其精致",
        en: "A cute British Shorthair cat wearing a detailed space suit, floating in a colorful cosmic nebula, holding a pack of space fish snacks in its paws, helmet visor reflecting a brilliant supernova explosion, Pixar style, 3D rendering, adorable, hyper-detailed."
    },
    {
        zh: "阳光明媚的森林深处，一只巨大的神秘生物（半鹿半猫，长着发光的鹿角），一名小女孩正在伸手触摸它，周围环绕着飞舞的金色荧光，吉卜力治愈风，丁达尔光效，梦幻仙境",
        en: "In the depths of a sun-drenched forest, a massive mystical creature (half deer, half cat, with glowing antlers) being touched by a little girl, surrounded by dancing golden fireflies, Studio Ghibli style, Tyndall light effect, dreamlike wonderland."
    },
    {
        zh: "赛步朋克雨夜街道，五颜六色的霓虹灯牌倒映在潮湿的积水中，一辆复古未来的悬浮跑车停在面馆前，蒸汽袅袅升起，高对比度，电影质感，冷暖色调对比",
        en: "A cyberpunk rainy night street, colorful neon signs reflected in wet puddles, a retro-futuristic hovering sports car parked in front of a steaming noodle shop, dramatic reflections, cinematic photography, cold and warm color contrast."
    }
];

// Active Particles Canvas Animation
function initParticles() {
    const canvas = document.getElementById('particle-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let particles = [];
    
    function resize() {
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
    }
    window.addEventListener('resize', resize);
    resize();
    
    class Particle {
        constructor() {
            this.x = Math.random() * canvas.width;
            this.y = Math.random() * canvas.height;
            this.size = Math.random() * 1.8 + 0.4;
            this.speedX = Math.random() * 0.4 - 0.2;
            this.speedY = Math.random() * 0.4 - 0.2;
            this.alpha = Math.random() * 0.5 + 0.2;
        }
        update() {
            this.x += this.speedX;
            this.y += this.speedY;
            
            if (this.x < 0 || this.x > canvas.width) this.speedX *= -1;
            if (this.y < 0 || this.y > canvas.height) this.speedY *= -1;
        }
        draw() {
            ctx.save();
            ctx.globalAlpha = this.alpha;
            ctx.fillStyle = '#8a2be2';
            ctx.shadowBlur = 6;
            ctx.shadowColor = '#00f3ff';
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        }
    }
    
    for (let i = 0; i < 45; i++) {
        particles.push(new Particle());
    }
    
    function animate() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        particles.forEach(p => {
            p.update();
            p.draw();
        });
        requestAnimationFrame(animate);
    }
    animate();
}

// Toast Notifications
function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    let icon = 'ℹ️';
    if (type === 'success') icon = '✅';
    if (type === 'warning') icon = '⚠️';
    if (type === 'error') icon = '❌';
    
    toast.innerHTML = `<span>${icon}</span><span style="flex:1">${message}</span>`;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.classList.add('fade-out');
        toast.addEventListener('animationend', () => {
            toast.remove();
        });
    }, duration);
}

// LocalStorage Helper
function loadSettings() {
    const saved = localStorage.getItem('spark_config');
    if (saved) {
        try {
            serverConfig = JSON.parse(saved);
        } catch (e) {}
    }
    
    // Set inputs if modal exists
    document.getElementById('settings-base-url').value = serverConfig.baseUrl || '';
    document.getElementById('settings-api-key').value = serverConfig.apiKey ? '••••••••••••••••••••' : '';
    document.getElementById('settings-access-code').value = serverConfig.accessCode || '';
}

function saveSettings() {
    const baseUrl = document.getElementById('settings-base-url').value.trim();
    const apiKeyRaw = document.getElementById('settings-api-key').value.trim();
    const accessCode = document.getElementById('settings-access-code').value.trim();
    
    // Only update API key if it has changed (isn't the dots placeholder)
    if (apiKeyRaw && apiKeyRaw !== '••••••••••••••••••••') {
        serverConfig.apiKey = apiKeyRaw;
    } else if (!apiKeyRaw) {
        serverConfig.apiKey = '';
    }
    
    serverConfig.baseUrl = baseUrl;
    serverConfig.accessCode = accessCode;
    
    localStorage.setItem('spark_config', JSON.stringify(serverConfig));
    showToast('API 配置参数保存成功', 'success');
    document.getElementById('settings-modal').style.display = 'none';
    
    // Re-check API Status
    checkApiStatus();
}

// API Status Check
async function checkApiStatus() {
    const badge = document.getElementById('api-status-badge');
    const badgeText = badge.querySelector('.status-text');
    
    badge.className = 'status-badge checking';
    badgeText.textContent = '检测 API 连接中...';
    
    try {
        const response = await fetchWithTimeout('/api/ping', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                config: {
                    baseUrl: serverConfig.baseUrl,
                    apiKey: serverConfig.apiKey
                }
            })
        }, 8000);
        const res = await response.json();
        if (res.online) {
            badge.className = 'status-badge online';
            badgeText.textContent = '后端 API 在线';
        } else {
            badge.className = 'status-badge offline';
            badgeText.textContent = '接口代理离线';
        }
    } catch (e) {
        badge.className = 'status-badge offline';
        badgeText.textContent = '连接服务器失败';
    }
}

// Tab Switches (Desktop and Mobile)
function switchTab(tabId) {
    currentTab = tabId;
    document.querySelectorAll('.panel-tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    document.getElementById(`tab-${tabId}`).classList.add('active');
    
    document.querySelectorAll('.input-pane').forEach(pane => {
        pane.classList.remove('active');
    });
    document.getElementById(`pane-${tabId}`).classList.add('active');
}

// Mobile tab layout switch
function switchMobileTab(side) {
    const leftPanel = document.querySelector('.panel-left');
    const rightPanel = document.querySelector('.panel-right');
    const leftBtn = document.getElementById('mobile-tab-left');
    const rightBtn = document.getElementById('mobile-tab-right');
    
    if (side === 'left') {
        leftPanel.classList.add('mobile-active');
        rightPanel.classList.remove('mobile-active');
        leftBtn.classList.add('active');
        rightBtn.classList.remove('active');
    } else {
        rightPanel.classList.add('mobile-active');
        leftPanel.classList.remove('mobile-active');
        rightBtn.classList.add('active');
        leftBtn.classList.remove('active');
    }
}

// Selection Handlers for Custom Grids (Ratio, Quality, Styles)
function initSelectors() {
    // T2I Aspect Ratios
    const t2iRatioCards = document.querySelectorAll('#t2i-ratio-selector .ratio-card');
    t2iRatioCards.forEach(card => {
        card.addEventListener('click', () => {
            t2iRatioCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            selectedT2iRatio = card.getAttribute('data-ratio');
        });
    });

    // I2I Aspect Ratios
    const i2iRatioCards = document.querySelectorAll('#i2i-ratio-selector .ratio-card');
    i2iRatioCards.forEach(card => {
        card.addEventListener('click', () => {
            i2iRatioCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            selectedI2iRatio = card.getAttribute('data-ratio');
        });
    });

    // T2I Quality Cards
    const t2iQualityCards = document.querySelectorAll('#t2i-quality-selector .quality-card');
    t2iQualityCards.forEach(card => {
        card.addEventListener('click', () => {
            t2iQualityCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            selectedT2iQuality = card.getAttribute('data-quality');
        });
    });

    // I2I Quality Cards
    const i2iQualityCards = document.querySelectorAll('#i2i-quality-selector .quality-card');
    i2iQualityCards.forEach(card => {
        card.addEventListener('click', () => {
            i2iQualityCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            selectedI2iQuality = card.getAttribute('data-quality');
        });
    });

    // Style Tags adding to prompt
    const styleTags = document.querySelectorAll('.style-tag');
    const promptTextarea = document.getElementById('t2i-prompt');
    
    styleTags.forEach(tag => {
        tag.addEventListener('click', () => {
            const tagText = tag.getAttribute('data-tag');
            let currentPrompt = promptTextarea.value;
            
            if (tag.classList.contains('active')) {
                // Remove the tag text
                tag.classList.remove('active');
                promptTextarea.value = currentPrompt.replace(tagText, '');
            } else {
                // Add the tag text
                tag.classList.add('active');
                promptTextarea.value = currentPrompt + tagText;
            }
        });
    });

    // Clear buttons
    document.getElementById('t2i-clear-btn').addEventListener('click', () => {
        document.getElementById('t2i-prompt').value = '';
        styleTags.forEach(t => t.classList.remove('active'));
    });
    document.getElementById('i2i-clear-btn').addEventListener('click', () => {
        document.getElementById('i2i-prompt').value = '';
    });

    // Advanced I2I toggle
    const advToggleBtn = document.getElementById('toggle-i2i-advanced');
    const advFields = document.getElementById('i2i-advanced-fields');
    advToggleBtn.addEventListener('click', () => {
        if (advFields.style.display === 'none') {
            advFields.style.display = 'flex';
            advToggleBtn.textContent = '收起 ▴';
        } else {
            advFields.style.display = 'none';
            advToggleBtn.textContent = '展开 ▾';
        }
    });
}

// Random Prompt Generator
function setRandomPrompt() {
    const randomIndex = Math.floor(Math.random() * CREATIVE_PROMPTS.length);
    const prompt = CREATIVE_PROMPTS[randomIndex];
    
    const finalText = Math.random() > 0.4 ? prompt.zh : prompt.en;
    
    const textarea = document.getElementById('t2i-prompt');
    textarea.value = finalText;
    
    document.querySelectorAll('.style-tag').forEach(t => t.classList.remove('active'));
    showToast('灵感提示词已载入，可直接点击生成', 'info');
}

// Drag & Drop / File Uploader Setup
function initFileUploader() {
    const dragArea = document.getElementById('i2i-drag-area');
    const fileInput = document.getElementById('i2i-file-input');
    
    dragArea.addEventListener('click', () => {
        fileInput.click();
    });
    
    fileInput.addEventListener('change', (e) => {
        handleFiles(e.target.files);
    });
    
    ['dragenter', 'dragover'].forEach(eventName => {
        dragArea.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragArea.classList.add('drag-over');
        }, false);
    });
    
    ['dragleave', 'drop'].forEach(eventName => {
        dragArea.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragArea.classList.remove('drag-over');
        }, false);
    });
    
    dragArea.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        handleFiles(files);
    }, false);
}

function handleFiles(files) {
    if (!files || files.length === 0) return;
    
    Array.from(files).forEach(file => {
        if (!file.type.startsWith('image/')) {
            showToast(`文件 ${file.name} 不是合法的图片格式`, 'warning');
            return;
        }
        
        if (file.size > 10 * 1024 * 1024) {
            showToast(`文件 ${file.name} 超过了 10MB 的上限`, 'warning');
            return;
        }
        
        const item = {
            name: file.name,
            file: file,
            base64: ''
        };
        uploadedFiles.push(item);

        const reader = new FileReader();
        reader.onload = (e) => {
            item.base64 = e.target.result;
            renderUploadPreviews();
        };
        reader.onerror = () => {
            uploadedFiles = uploadedFiles.filter(uploaded => uploaded !== item);
            renderUploadPreviews();
            showToast(`无法读取图片 ${file.name}`, 'error');
        };
        reader.readAsDataURL(file);
    });

    // Store the real File objects immediately so a quick Generate click cannot
    // race the preview decoder and accidentally omit the references.
    renderUploadPreviews();
}

function renderUploadPreviews() {
    const container = document.getElementById('i2i-upload-previews');
    
    if (uploadedFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    
    container.style.display = 'flex';
    container.innerHTML = '';
    
    uploadedFiles.forEach((item, index) => {
        const card = document.createElement('div');
        card.className = 'upload-preview-card';
        card.innerHTML = `
            ${item.base64 ? `<img src="${item.base64}" alt="${item.name}">` : '<div class="upload-preview-loading">读取中…</div>'}
            <button type="button" class="upload-preview-delete" onclick="removeUploadedFile(${index})">✕</button>
        `;
        container.appendChild(card);
    });
}

window.removeUploadedFile = function(index) {
    uploadedFiles.splice(index, 1);
    renderUploadPreviews();
};

// Action: Generate Image (T2I & I2I Router)
function triggerGeneration() {
    const generateBtn = document.getElementById('generate-btn');
    
    if (window.innerWidth <= 768) {
        switchMobileTab('right');
    }
    
    // Brief shake / disable feedback (200ms) to avoid double submission
    generateBtn.disabled = true;
    setTimeout(() => {
        generateBtn.disabled = false;
    }, 200);

    if (currentTab === 't2i') {
        const prompt = document.getElementById('t2i-prompt').value.trim();
        const model = document.getElementById('t2i-model').value;
        
        if (!prompt) {
            showToast('请输入创意提示词再开始生成', 'warning');
            return;
        }
        
        addTask('t2i', prompt, model, selectedT2iRatio, selectedT2iQuality, null);
        showToast('文生图任务已加入生成队列', 'info');
    } else {
        const prompt = document.getElementById('i2i-prompt').value.trim();
        const model = document.getElementById('i2i-model').value;
        const style = document.getElementById('i2i-style').value;
        
        if (uploadedFiles.length === 0) {
            showToast('请至少上传一张参考图片再开始图生图', 'warning');
            return;
        }
        if (!prompt) {
            showToast('请输入修改或编辑指令提示词', 'warning');
            return;
        }
        
        // Store base64 data for references in order to support page-reload retries
        const filesData = uploadedFiles.map(f => ({
            name: f.name,
            base64: f.base64
        }));
        
        const extraData = {
            style: style,
            files: filesData
        };
        
        const finalRatio = selectedI2iRatio === 'auto' ? 'Auto' : selectedI2iRatio;
        const finalQuality = selectedI2iQuality;
        
        addTask('i2i', prompt, model, finalRatio, finalQuality, extraData);
        showToast('图生图修改任务已加入生成队列', 'info');
    }
}

function base64ToBlob(base64, mimeType) {
    try {
        const parts = base64.split(',');
        const byteString = atob(parts[1]);
        const ab = new ArrayBuffer(byteString.length);
        const ia = new Uint8Array(ab);
        for (let i = 0; i < byteString.length; i++) {
            ia[i] = byteString.charCodeAt(i);
        }
        return new Blob([ab], { type: mimeType });
    } catch (e) {
        console.error("Base64 to Blob conversion failed:", e);
        return null;
    }
}

function loadTaskList() {
    const saved = localStorage.getItem('spark_image_tasks');
    if (saved) {
        try {
            taskList = JSON.parse(saved);
            // Clean up any tasks with broken data url
            taskList = taskList.filter(task => !task.image || !task.image.includes('undefined'));
            // Resume polling for pending tasks with a backendTaskId
            taskList.forEach(task => {
                task.controller = null;
                if (task.status === 'pending') {
                    if (task.backendTaskId) {
                        pollTaskStatus(task);
                    } else {
                        task.status = 'failed';
                        task.error = '页面刷新，任务被中断';
                    }
                }
            });
            saveTaskList(); // Save the cleaned up task list
        } catch (e) {
            taskList = [];
        }
    } else {
        taskList = [];
    }
    renderTaskListUI();
}

function saveTaskList() {
    try {
        // Strip out non-serializable properties (e.g. AbortController)
        const tasksToSave = taskList.map(task => {
            const { controller, ...serializable } = task;
            return serializable;
        });
        localStorage.setItem('spark_image_tasks', JSON.stringify(tasksToSave));
    } catch (e) {
        console.error("Failed to save tasks to localStorage:", e);
    }
}

function renderTaskListUI() {
    const activeCount = taskList.filter(t => t.status === 'pending').length;
    const totalCount = taskList.length;
    
    const activeCountSpan = document.getElementById('tasks-active-count');
    const totalCountSpan = document.getElementById('tasks-total-count');
    const section = document.getElementById('tasks-section');
    const container = document.getElementById('tasks-list');
    
    if (activeCountSpan) activeCountSpan.textContent = activeCount;
    if (totalCountSpan) totalCountSpan.textContent = totalCount;
    if (section) section.style.display = totalCount > 0 ? 'block' : 'none';
    
    if (!container) return;
    container.innerHTML = '';
    
    taskList.forEach(task => {
        const card = document.createElement('div');
        card.className = `task-card ${task.status}`;
        
        let visualHTML = '';
        if (task.status === 'pending') {
            visualHTML = `<div class="task-visual"><div class="task-spinner-ring"></div></div>`;
        } else if (task.status === 'completed' && task.image) {
            visualHTML = `<div class="task-visual"><img src="${task.image}" alt="Thumb" onclick="viewTaskItem('${task.id}')"></div>`;
        } else if (task.status === 'failed') {
            visualHTML = `<div class="task-visual"><span class="task-failed-icon">❌</span></div>`;
        } else if (task.status === 'cancelled') {
            visualHTML = `<div class="task-visual"><span class="task-cancelled-icon">⏹️</span></div>`;
        }
        
        let statusBadgeHTML = '';
        if (task.status === 'pending') {
            statusBadgeHTML = `<span class="task-status-badge pending">渲染中</span>`;
        } else if (task.status === 'completed') {
            statusBadgeHTML = `<span class="task-status-badge completed">已完成</span>`;
        } else if (task.status === 'failed') {
            statusBadgeHTML = `<span class="task-status-badge failed" title="${task.error || ''}">失败</span>`;
        } else if (task.status === 'cancelled') {
            statusBadgeHTML = `<span class="task-status-badge cancelled">已取消</span>`;
        }
        
        let actionHTML = '';
        if (task.status === 'pending') {
            actionHTML = `<button type="button" class="task-btn-icon danger-hover" title="取消任务" onclick="cancelTask('${task.id}')">✕</button>`;
        } else {
            let retryBtn = '';
            if (task.status === 'failed' || task.status === 'cancelled') {
                retryBtn = `<button type="button" class="task-btn-icon" title="重试任务" onclick="retryTask('${task.id}')">🔄</button>`;
            }
            actionHTML = `
                ${retryBtn}
                <button type="button" class="task-btn-icon danger-hover" title="删除记录" onclick="deleteTask('${task.id}')">✕</button>
            `;
        }
        
        const truncatedPrompt = task.prompt.replace(/"/g, '&quot;');
        
        card.innerHTML = `
            ${visualHTML}
            <div class="task-info">
                <div class="task-meta">
                    <span class="task-badge-type">${task.type === 't2i' ? '文生图' : '图生图'}</span>
                    <span class="task-model" translate="no">${task.model.replace('-image', '')}</span>
                    <span>${task.ratio}</span>
                    <span>${task.quality}</span>
                    <span style="margin-left: auto;">${task.timestamp}</span>
                </div>
                <div class="task-prompt" title="${truncatedPrompt}">${task.prompt}</div>
                ${task.status === 'failed' ? `<div class="task-error" title="${task.error || ''}">原因: ${task.error || '未知错误'}</div>` : ''}
            </div>
            <div class="task-control">
                ${statusBadgeHTML}
                ${actionHTML}
            </div>
        `;
        container.appendChild(card);
    });
}

function addTask(type, prompt, model, ratio, quality, extraData) {
    const id = 'task_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
    const task = {
        id: id,
        type: type,
        prompt: prompt,
        model: model,
        ratio: ratio,
        quality: quality,
        status: 'pending',
        error: null,
        image: null,
        timestamp: new Date().toLocaleTimeString(),
        controller: null,
        extraData: extraData
    };
    
    // Add to top of list
    taskList.unshift(task);
    
    // Enforce size limit (max 15 tasks)
    if (taskList.length > 15) {
        const removed = taskList.pop();
        if (removed && removed.status === 'pending' && removed.controller) {
            try { removed.controller.abort(); } catch (e) {}
        }
    }
    
    saveTaskList();
    renderTaskListUI();
    
    // Run asynchronously
    runTaskFetch(task);
    return task;
}

async function pollTaskStatus(task) {
    if (!task.backendTaskId) return;
    
    const intervalId = setInterval(async () => {
        const currentTask = taskList.find(t => t.id === task.id);
        if (!currentTask || currentTask.status === 'cancelled' || currentTask.status === 'completed' || currentTask.status === 'failed') {
            clearInterval(intervalId);
            return;
        }
        
        try {
            const response = await fetch(`/api/image/task/status?task_id=${task.backendTaskId}`);
            if (!response.ok) return;
            
            const data = await response.json();
            if (!data) return;
            
            if (data.status === 'completed') {
                clearInterval(intervalId);
                
                const result = data.result;
                if (result && result.data && result.data.length > 0) {
                    const item = result.data[0];
                    let imgDataUrl = '';
                    if (item.b64_json) {
                        imgDataUrl = `data:image/png;base64,${item.b64_json}`;
                    } else if (item.url) {
                        imgDataUrl = item.url;
                    }
                    
                    if (imgDataUrl) {
                        currentTask.status = 'completed';
                        currentTask.image = imgDataUrl;
                        currentTask.controller = null;
                        
                        saveToHistory(currentTask.type, currentTask.prompt, currentTask.model, currentTask.ratio, currentTask.quality, imgDataUrl);
                        displaySpotlight(imgDataUrl, currentTask.prompt, currentTask.model, currentTask.ratio, currentTask.quality);
                        showToast('图像渲染成功！已存入历史画廊', 'success');
                    } else {
                        currentTask.status = 'failed';
                        currentTask.error = '返回结果中无有效图像数据';
                        currentTask.controller = null;
                        showToast('生图失败: 未在响应中找到图像数据', 'error');
                    }
                } else {
                    currentTask.status = 'failed';
                    currentTask.error = '返回的数据格式不正确';
                    currentTask.controller = null;
                    showToast('生图失败: 数据格式不正确', 'error');
                }
                
                saveTaskList();
                renderTaskListUI();
            } else if (data.status === 'failed') {
                clearInterval(intervalId);
                currentTask.status = 'failed';
                currentTask.error = data.error || '未知后台错误';
                currentTask.controller = null;
                showToast(`生图失败: ${currentTask.error}`, 'error');
                saveTaskList();
                renderTaskListUI();
            }
        } catch (e) {
            console.error("Polling error:", e);
        }
    }, 1500);
}

async function runTaskFetch(task) {
    const controller = new AbortController();
    task.controller = controller;
    
    try {
        let response;
        if (task.type === 't2i') {
            const body = {
                prompt: task.prompt,
                model: task.model,
                size: task.ratio,
                quality: task.quality,
                image_size: task.quality,
                response_format: 'b64_json',
                config: {
                    baseUrl: serverConfig.baseUrl,
                    apiKey: serverConfig.apiKey,
                    accessCode: serverConfig.accessCode
                }
            };
            
            response = await fetch('/api/image/generations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                signal: controller.signal
            });
        } else {
            const formData = new FormData();
            formData.append('prompt', task.prompt);
            formData.append('model', task.model);
            formData.append('response_format', 'b64_json');
            
            if (task.ratio && task.ratio !== 'auto') {
                formData.append('aspect_ratio', task.ratio);
            }
            formData.append('image_size', task.quality);
            
            if (task.extraData && task.extraData.style) {
                formData.append('style', task.extraData.style);
            }
            
            if (task.extraData && task.extraData.files) {
                task.extraData.files.forEach((f, idx) => {
                    const mime = f.base64.split(';')[0].split(':')[1] || 'image/png';
                    const blob = base64ToBlob(f.base64, mime);
                    if (blob) {
                        const file = new File([blob], f.name, { type: mime });
                        const fieldName = idx === 0 ? 'image' : 'image[]';
                        formData.append(fieldName, file, f.name);
                    }
                });
            }
            
            formData.append('config', JSON.stringify({
                baseUrl: serverConfig.baseUrl,
                apiKey: serverConfig.apiKey,
                accessCode: serverConfig.accessCode
            }));
            
            response = await fetch('/api/image/edits', {
                method: 'POST',
                body: formData,
                signal: controller.signal
            });
        }
        
        if (task.status === 'cancelled') return;
        
        const result = await response.json();
        
        if (response.ok && result.task_id) {
            task.backendTaskId = result.task_id;
            task.controller = null;
            saveTaskList();
            pollTaskStatus(task);
        } else {
            throw new Error(result.error || result.message || '任务提交失败');
        }
    } catch (e) {
        if (task.status === 'cancelled') return;
        
        console.error(e);
        task.status = 'failed';
        task.error = e.name === 'AbortError' ? '请求超时或被手动取消' : e.message;
        task.controller = null;
        showToast(`生图失败: ${task.error}`, 'error');
    } finally {
        saveTaskList();
        renderTaskListUI();
    }
}

function cancelTask(taskId) {
    const task = taskList.find(t => t.id === taskId);
    if (task && task.status === 'pending') {
        task.status = 'cancelled';
        if (task.controller) {
            try {
                task.controller.abort();
            } catch (e) {
                console.error(e);
            }
        }
        task.controller = null;
        saveTaskList();
        renderTaskListUI();
        showToast('任务已取消', 'info');
    }
}

function deleteTask(taskId) {
    const index = taskList.findIndex(t => t.id === taskId);
    if (index > -1) {
        const task = taskList[index];
        if (task.status === 'pending') {
            cancelTask(taskId);
        }
        taskList.splice(index, 1);
        saveTaskList();
        renderTaskListUI();
        showToast('任务记录已删除', 'info');
    }
}

function retryTask(taskId) {
    const task = taskList.find(t => t.id === taskId);
    if (task && task.status !== 'pending') {
        task.status = 'pending';
        task.error = null;
        task.image = null;
        task.timestamp = new Date().toLocaleTimeString();
        saveTaskList();
        renderTaskListUI();
        runTaskFetch(task);
        showToast('正在重试生图任务...', 'info');
    }
}

function clearCompletedTasks() {
    const activeTasks = taskList.filter(t => t.status === 'pending');
    const hasCompleted = taskList.length > activeTasks.length;
    if (hasCompleted) {
        taskList = activeTasks;
        saveTaskList();
        renderTaskListUI();
        showToast('已清除已完成和已失效的任务记录', 'info');
    } else {
        showToast('没有可清除的记录', 'info');
    }
}

function clearAllTasks() {
    if (taskList.length === 0) return;
    if (confirm('确认清空所有任务记录？运行中的任务将被取消。')) {
        taskList.forEach(task => {
            if (task.status === 'pending' && task.controller) {
                try { task.controller.abort(); } catch (e) {}
            }
        });
        taskList = [];
        saveTaskList();
        renderTaskListUI();
        showToast('所有任务已成功清空', 'info');
    }
}

// Expose to window for inline onclick attributes
window.cancelTask = cancelTask;
window.deleteTask = deleteTask;
window.retryTask = retryTask;
window.viewTaskItem = function(taskId) {
    const task = taskList.find(t => t.id === taskId);
    if (task && task.status === 'completed' && task.image) {
        displaySpotlight(task.image, task.prompt, task.model, task.ratio, task.quality);
        showToast('已在渲染室展示该图片', 'info');
    }
};

function displaySpotlight(imgDataUrl, prompt, model, ratio, quality) {
    const spotlightPlaceholder = document.getElementById('spotlight-placeholder');
    const spotlightWrapper = document.getElementById('spotlight-image-wrapper');
    const spotlightSkeleton = document.getElementById('spotlight-skeleton');
    
    spotlightSkeleton.style.display = 'none';
    spotlightPlaceholder.style.display = 'none';
    spotlightWrapper.style.display = 'flex';
    
    const imgEl = document.getElementById('spotlight-img');
    imgEl.src = imgDataUrl;
    
    document.getElementById('spotlight-info-model').textContent = model.replace('-image', '');
    document.getElementById('spotlight-info-prompt').textContent = prompt;
    
    setupSpotlightActions(imgDataUrl, prompt, model, ratio, quality);
}

function resetSpotlightUI() {
    const spotlightPlaceholder = document.getElementById('spotlight-placeholder');
    const spotlightWrapper = document.getElementById('spotlight-image-wrapper');
    const spotlightSkeleton = document.getElementById('spotlight-skeleton');
    
    spotlightSkeleton.style.display = 'none';
    spotlightWrapper.style.display = 'none';
    spotlightPlaceholder.style.display = 'flex';
}

function setupSpotlightActions(imgDataUrl, prompt, model, ratio, quality) {
    document.getElementById('spotlight-zoom-btn').onclick = () => {
        const spotlightImg = document.getElementById('spotlight-img');
        const currentSrc = spotlightImg ? spotlightImg.src : imgDataUrl;
        
        let clickedIndex = imageHistory.findIndex(item => item.image === currentSrc);
        if (clickedIndex === -1) {
            const currentItem = {
                type: 'image',
                url: currentSrc,
                caption: `
                    <strong>提示词:</strong> ${prompt}<br>
                    <span style="font-size:0.75rem; color: #94a3b8; display:block; margin-top:0.4rem">
                        模型: ${model} | 比例: ${ratio} | 画质: ${quality}
                    </span>
                `
            };
            const mediaList = [currentItem, ...imageHistory.map(item => ({
                type: 'image',
                url: item.image,
                caption: `
                    <strong>提示词:</strong> ${item.prompt}<br>
                    <span style="font-size:0.75rem; color: #94a3b8; display:block; margin-top:0.4rem">
                        模型: ${item.model} | 比例: ${item.ratio} | 画质: ${item.quality}
                    </span>
                `
            }))];
            openLightbox(mediaList, 0);
        } else {
            const mediaList = imageHistory.map(item => ({
                type: 'image',
                url: item.image,
                caption: `
                    <strong>提示词:</strong> ${item.prompt}<br>
                    <span style="font-size:0.75rem; color: #94a3b8; display:block; margin-top:0.4rem">
                        模型: ${item.model} | 比例: ${item.ratio} | 画质: ${item.quality}
                    </span>
                `
            }));
            openLightbox(mediaList, clickedIndex);
        }
    };
    
    document.getElementById('spotlight-download-btn').onclick = () => {
        downloadImage(imgDataUrl, `spark_${Date.now()}.png`);
    };
    
    document.getElementById('spotlight-copy-btn').onclick = () => {
        copyImageToClipboard(imgDataUrl);
    };
    
    document.getElementById('spotlight-reuse-btn').onclick = () => {
        reusePrompt(prompt, ratio, quality);
    };
    
    document.getElementById('spotlight-to-i2i-btn').onclick = () => {
        sendToImageToImage(imgDataUrl, `ref_spark_${Date.now()}.png`);
    };
}

// --- LIGHTBOX SYSTEM ---
let lightboxItems = [];
let lightboxActiveIndex = -1;

function initLightbox() {
    const modal = document.getElementById('lightbox-modal');
    const closeBtn = document.getElementById('close-lightbox-btn');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');
    
    if (!modal) return;
    
    closeBtn?.addEventListener('click', closeLightbox);
    prevBtn?.addEventListener('click', () => navigateLightbox(-1));
    nextBtn?.addEventListener('click', () => navigateLightbox(1));
    
    // Close on clicking outside the content
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeLightbox();
        }
    });
}

function openLightbox(items, index) {
    lightboxItems = items;
    lightboxActiveIndex = index;
    
    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;
    
    modal.style.display = 'flex';
    updateLightboxContent();
    
    document.addEventListener('keydown', handleLightboxKeydown);
}

function closeLightbox() {
    const modal = document.getElementById('lightbox-modal');
    if (!modal) return;
    modal.style.display = 'none';
    
    const video = document.getElementById('lightbox-video');
    if (video) {
        video.pause();
        video.src = '';
    }
    
    document.removeEventListener('keydown', handleLightboxKeydown);
}

function updateLightboxContent() {
    const img = document.getElementById('lightbox-img');
    const video = document.getElementById('lightbox-video');
    const caption = document.getElementById('lightbox-caption');
    const prevBtn = document.getElementById('prev-lightbox-btn');
    const nextBtn = document.getElementById('next-lightbox-btn');
    
    if (!img || !video || !caption) return;
    
    if (lightboxActiveIndex < 0 || lightboxActiveIndex >= lightboxItems.length) {
        closeLightbox();
        return;
    }
    
    const item = lightboxItems[lightboxActiveIndex];
    
    if (item.type === 'video') {
        img.style.display = 'none';
        video.src = item.url;
        video.style.display = 'block';
        video.play().catch(err => console.log("Auto-play prevented", err));
    } else {
        video.style.display = 'none';
        video.pause();
        video.src = '';
        img.src = item.url;
        img.style.display = 'block';
    }
    
    if (item.caption) {
        caption.innerHTML = item.caption;
        caption.style.display = 'block';
    } else {
        caption.style.display = 'none';
    }
    
    if (lightboxItems.length <= 1) {
        if (prevBtn) prevBtn.style.display = 'none';
        if (nextBtn) nextBtn.style.display = 'none';
    } else {
        if (prevBtn) prevBtn.style.display = 'flex';
        if (nextBtn) nextBtn.style.display = 'flex';
    }
}

function navigateLightbox(direction) {
    if (lightboxItems.length <= 1) return;
    
    lightboxActiveIndex += direction;
    if (lightboxActiveIndex < 0) {
        lightboxActiveIndex = lightboxItems.length - 1;
    } else if (lightboxActiveIndex >= lightboxItems.length) {
        lightboxActiveIndex = 0;
    }
    
    updateLightboxContent();
}

function handleLightboxKeydown(e) {
    if (e.key === 'ArrowLeft') {
        navigateLightbox(-1);
    } else if (e.key === 'ArrowRight') {
        navigateLightbox(1);
    } else if (e.key === 'Escape') {
        closeLightbox();
    }
}

function downloadImage(dataUrl, filename) {
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast('图片下载已启动', 'success');
}

async function copyImageToClipboard(dataUrl) {
    try {
        const response = await fetch(dataUrl);
        const blob = await response.blob();
        
        await navigator.clipboard.write([
            new ClipboardItem({
                [blob.type]: blob
            })
        ]);
        showToast('图片二进制已成功复制到剪贴板', 'success');
    } catch (err) {
        console.error('Copy image failed', err);
        try {
            await navigator.clipboard.writeText(dataUrl);
            showToast('直接复制二进制失败，已复制 Base64 文本数据', 'warning');
        } catch (e) {
            showToast('复制图片失败，浏览器安全权限受限', 'error');
        }
    }
}

function reusePrompt(prompt, ratio, quality) {
    if (currentTab === 't2i') {
        document.getElementById('t2i-prompt').value = prompt;
        const ratioCard = document.querySelector(`#t2i-ratio-selector .ratio-card[data-ratio="${ratio}"]`);
        if (ratioCard) ratioCard.click();
        
        const qualityCard = document.querySelector(`#t2i-quality-selector .quality-card[data-quality="${quality}"]`);
        if (qualityCard) qualityCard.click();
    } else {
        document.getElementById('i2i-prompt').value = prompt;
    }
    
    if (window.innerWidth <= 768) {
        switchMobileTab('left');
    }
    
    showToast('创意提示词已填回配置面板', 'info');
}

function sendToImageToImage(dataUrl, filename) {
    switchTab('i2i');
    
    fetch(dataUrl)
        .then(res => res.blob())
        .then(blob => {
            const file = new File([blob], filename, { type: "image/png" });
            uploadedFiles = [{
                name: filename,
                file: file,
                base64: dataUrl
            }];
            
            renderUploadPreviews();
            
            if (window.innerWidth <= 768) {
                switchMobileTab('left');
            }
            showToast('图片已送往图生图作为参考画布！', 'success');
        })
        .catch(e => {
            console.error(e);
            showToast('转换图片失败', 'error');
        });
}

function saveHistoryToStorage() {
    try {
        localStorage.setItem('spark_image_history', JSON.stringify(imageHistory));
    } catch (e) {
        if (e.name === 'QuotaExceededError' || e.code === 22 || e.number === 0x8007000E) {
            console.warn("Storage quota exceeded, trying to prune older items...");
            let prunedHistory = [...imageHistory];
            let success = false;
            
            while (prunedHistory.length > 1) {
                prunedHistory.pop();
                try {
                    localStorage.setItem('spark_image_history', JSON.stringify(prunedHistory));
                    success = true;
                    break;
                } catch (innerErr) {
                    // continue pruning
                }
            }
            
            if (!success) {
                console.warn("Even a single item exceeds localStorage quota. Omit saving this batch to localStorage.");
                showToast('由于高清大图尺寸过大，无法存入浏览器本地持久缓存。图片已在当前会话中生成，刷新页面后将消失，请及时下载保存！', 'warning', 6000);
            } else {
                showToast('本地浏览器缓存已满，已自动清理较旧的生图记录以释放空间。', 'info');
            }
        } else {
            console.error("Failed to save history to localStorage:", e);
        }
    }
}

function saveToHistory(type, prompt, model, ratio, quality, image) {
    const newItem = {
        id: 'img_' + Date.now(),
        type: type,
        prompt: prompt,
        model: model,
        ratio: ratio,
        quality: quality,
        timestamp: new Date().toLocaleTimeString(),
        image: image
    };
    
    imageHistory.unshift(newItem);
    
    if (imageHistory.length > 30) {
        imageHistory = imageHistory.slice(0, 30);
    }
    
    saveHistoryToStorage();
    renderHistoryGrid();
}

async function checkImageExists(url) {
    if (!url || url.includes('undefined')) return false;
    if (url.startsWith('data:')) return true;
    try {
        const response = await fetch(url, { method: 'HEAD' });
        return response.ok;
    } catch (e) {
        return false;
    }
}

async function loadHistory() {
    const saved = localStorage.getItem('spark_image_history');
    if (saved) {
        try {
            imageHistory = JSON.parse(saved);
        } catch (e) {}
    }
    
    // Try to load any restored history items from the server (e.g. 4K image generated before the fix)
    try {
        const response = await fetch('restored_history.json');
        if (response.ok) {
            const restored = await response.json();
            if (Array.isArray(restored)) {
                for (const item of restored) {
                    if (!imageHistory.some(x => x.id === item.id)) {
                        imageHistory.unshift(item);
                    }
                }
            }
        }
    } catch (e) {
        console.log("No restored history found or failed to fetch:", e);
    }
    
    // Filter out history items whose images do not exist on the server (only for non-data URLs)
    const filteredHistory = [];
    let needCleanSave = false;
    for (const item of imageHistory) {
        if (await checkImageExists(item.image)) {
            filteredHistory.push(item);
        } else {
            needCleanSave = true;
        }
    }
    imageHistory = filteredHistory;
    if (needCleanSave) {
        saveHistoryToStorage();
    }
    
    renderHistoryGrid();
}

function renderHistoryGrid() {
    const grid = document.getElementById('history-grid');
    const countSpan = document.getElementById('history-count');
    
    countSpan.textContent = imageHistory.length;
    
    if (imageHistory.length === 0) {
        grid.innerHTML = '<div class="history-empty-text">画廊目前空空如也。生成的图片都会收录在此处。</div>';
        return;
    }
    
    grid.innerHTML = '';
    
    imageHistory.forEach(item => {
        const card = document.createElement('div');
        card.className = 'history-card';
        card.innerHTML = `
            <img src="${item.image}" alt="History Item" loading="lazy">
            <div class="history-card-overlay">
                <button class="history-card-btn" title="查看" onclick="viewHistoryItem('${item.id}', 'zoom')">🔍</button>
                <button class="history-card-btn" title="下载" onclick="viewHistoryItem('${item.id}', 'download')">📥</button>
                <button class="history-card-btn" title="填回提示词" onclick="viewHistoryItem('${item.id}', 'reuse')">🔄</button>
                <button class="history-card-btn" title="送往图生图" onclick="viewHistoryItem('${item.id}', 'to-i2i')">🎨</button>
                <button class="history-card-btn" style="background:rgba(239, 68, 68, 0.4)" title="删除" onclick="deleteHistoryItem('${item.id}')">✕</button>
            </div>
        `;
        grid.appendChild(card);
    });
}

window.viewHistoryItem = function(id, action) {
    const item = imageHistory.find(x => x.id === id);
    if (!item) return;
    
    displaySpotlight(item.image, item.prompt, item.model, item.ratio, item.quality);
    
    if (action === 'zoom') {
        const clickedIndex = imageHistory.findIndex(x => x.id === id);
        const mediaList = imageHistory.map(item => ({
            type: 'image',
            url: item.image,
            caption: `
                <strong>提示词:</strong> ${item.prompt}<br>
                <span style="font-size:0.75rem; color: #94a3b8; display:block; margin-top:0.4rem">
                    模型: ${item.model} | 比例: ${item.ratio} | 画质: ${item.quality}
                </span>
            `
        }));
        openLightbox(mediaList, clickedIndex !== -1 ? clickedIndex : 0);
    } else if (action === 'download') {
        downloadImage(item.image, `spark_${item.id}.png`);
    } else if (action === 'reuse') {
        reusePrompt(item.prompt, item.ratio, item.quality);
    } else if (action === 'to-i2i') {
        sendToImageToImage(item.image, `ref_${item.id}.png`);
    }
};

window.deleteHistoryItem = function(id) {
    const index = imageHistory.findIndex(x => x.id === id);
    if (index > -1) {
        imageHistory.splice(index, 1);
        saveHistoryToStorage();
        renderHistoryGrid();
        showToast('记录已删除', 'info');
    }
};

function clearHistory() {
    if (confirm('确认清空所有生图历史记录？此操作不可撤销。')) {
        imageHistory = [];
        localStorage.removeItem('spark_image_history');
        renderHistoryGrid();
        resetSpotlightUI();
        showToast('创作历史画廊已成功清空', 'info');
    }
}

// ==========================================================================
// Initializations
// ==========================================================================
document.addEventListener('DOMContentLoaded', () => {
    initParticles();
    loadSettings();
    loadHistory();
    loadTaskList();
    checkApiStatus();
    initSelectors();
    initFileUploader();
    
    const clearCompletedTasksBtn = document.getElementById('clear-completed-tasks-btn');
    if (clearCompletedTasksBtn) clearCompletedTasksBtn.addEventListener('click', clearCompletedTasks);
    
    const clearAllTasksBtn = document.getElementById('clear-all-tasks-btn');
    if (clearAllTasksBtn) clearAllTasksBtn.addEventListener('click', clearAllTasks);
    
    document.getElementById('t2i-random-prompt-btn').addEventListener('click', setRandomPrompt);
    document.getElementById('generate-btn').addEventListener('click', triggerGeneration);
    document.getElementById('clear-history-btn').addEventListener('click', clearHistory);

    // Keyboard Hotkey (Ctrl + Enter) to trigger generation
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            const genBtn = document.getElementById('generate-btn');
            if (genBtn && !genBtn.disabled) {
                genBtn.click();
            }
        }
    });
    
    const settingsModal = document.getElementById('settings-modal');
    document.getElementById('open-settings-btn').addEventListener('click', () => {
        settingsModal.style.display = 'flex';
    });
    document.getElementById('close-settings-btn').addEventListener('click', () => {
        settingsModal.style.display = 'none';
    });
    document.getElementById('save-settings-btn').addEventListener('click', saveSettings);
    
    initLightbox();
    
    settingsModal.addEventListener('click', (e) => {
        if (e.target === settingsModal) settingsModal.style.display = 'none';
    });
    
    setRandomPrompt();
});

/* ============================================================
   General Settings (通用设置) panel + 暗夜模式 toggle
   Theme persists in localStorage('spark_theme'), shared with
   the main SPARK app (same origin).
   ============================================================ */
(function initThemeToggle() {
    const btn = document.getElementById('theme-toggle-btn');
    const icon = document.getElementById('theme-toggle-icon');
    if (!btn) return;
    const isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';
    const sync = () => {
        if (icon) icon.textContent = isDark() ? '☀️' : '🌙';
        btn.title = isDark() ? '切换到明亮模式' : '切换到暗夜模式';
    };
    sync();
    btn.addEventListener('click', () => {
        const next = !isDark();
        document.documentElement.setAttribute('data-theme', next ? 'dark' : 'light');
        try { localStorage.setItem('spark_theme', next ? 'dark' : 'light'); } catch (e) {}
        sync();
    });
})();
