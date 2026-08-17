/* ==========================================================================
   Image Studio (图像工坊) — free-form text-to-image / image-to-image generator.
   Folded in from the former standalone /image-service-station/ page: this
   module owns its own tab (t2i/i2i), task queue and history gallery, but
   reuses the host app's shared config, toast, and lightbox systems instead
   of duplicating them.
   ========================================================================== */

let imgStudioCurrentTab = 't2i';
let imgStudioMobileView = 'create'; // 'create' | 'result'
let imgStudioSelectedT2iRatio = '16:9';
let imgStudioSelectedT2iQuality = '2K';
let imgStudioSelectedI2iRatio = 'auto';
let imgStudioSelectedI2iQuality = '1K';
let imgStudioUploadedFiles = []; // Array of { name, file, base64 }
let imgStudioHistory = []; // Array of { id, type, prompt, model, ratio, quality, timestamp, image }
let imgStudioTaskList = []; // Array of { id, type, prompt, model, ratio, quality, status, error, image, timestamp, controller, extraData }

// Mobile Subview Switch (P4: 创作 / 结果 互斥切换)
window.switchImageStudioMobileView = function (view) {
    imgStudioMobileView = view;
    const grid = document.querySelector('#panel-image-studio .imgstudio-grid');
    if (grid) {
        grid.setAttribute('data-mobile-view', view);
        grid.scrollTop = 0;
    }
    const createBtn = document.getElementById('imgstudio-mobtab-create');
    const resultBtn = document.getElementById('imgstudio-mobtab-result');
    if (createBtn) createBtn.classList.toggle('active', view === 'create');
    if (resultBtn) resultBtn.classList.toggle('active', view === 'result');
};

function initImageStudioMobileView() {
    const mql = window.matchMedia('(max-width: 768px)');
    const syncView = (e) => {
        const grid = document.querySelector('#panel-image-studio .imgstudio-grid');
        if (!grid) return;
        if (e.matches) {
            grid.setAttribute('data-mobile-view', imgStudioMobileView);
        } else {
            grid.removeAttribute('data-mobile-view');
        }
    };
    if (mql.matches) {
        switchImageStudioMobileView(imgStudioMobileView);
    }
    if (typeof mql.addEventListener === 'function') {
        mql.addEventListener('change', syncView);
    } else if (typeof mql.addListener === 'function') {
        mql.addListener(syncView);
    }
}

// Curated Creative Prompts for Random Generator
const IMGSTUDIO_CREATIVE_PROMPTS = [
    {
        zh: "一座隐藏在巨型橡树内部的复古图书馆，温暖的阳光穿过树叶间的缝隙照亮飞舞的微尘，空中漂浮着几本发光的魔法书，精致的木质雕花楼梯，维多利亚风格，极高细节，写实摄影质感",
        en: "A retro library hidden inside a giant hollow oak tree, warm sunlight filtering through leaves illuminating floating dust motes, glowing magic books floating in the air, intricate carved wooden staircases, Victorian style, hyper-detailed, photorealistic photography."
    },
    {
        zh: "蓝冰冰川洞穴深处改造的避寒睡眠小屋，半透明的冰墙透出幽蓝天光，厚实的原木床架铺着羊毛毯，黄铜马灯的暖光与冰蓝形成冷暖对比，纪实摄影质感，8k分辨率",
        en: "A snug sleeping refuge built deep inside a blue glacier cave, translucent ice walls glowing with cold daylight, a heavy timber bed frame layered with wool blankets, warm brass lantern light contrasting the icy blue, documentary photorealism, 8k resolution."
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
        zh: "一只可爱的英短猫咪蜷在木屋窗台的粗针织羊毛毯上，窗外是落雪的松林，壁炉火光把猫毛染成暖金色，玻璃上有一圈呵气雾痕，纪实摄影，浅景深，极其精致",
        en: "A cute British Shorthair cat curled up on a chunky wool blanket on a cabin windowsill, snowy pine forest outside, fireplace glow tinting its fur warm gold, a ring of breath fog on the glass, documentary photography, shallow depth of field, hyper-detailed."
    },
    {
        zh: "阳光明媚的森林深处，一只巨大的神秘生物（半鹿半猫，长着发光的鹿角），一名小女孩正在伸手触摸它，周围环绕着飞舞的金色荧光，吉卜力治愈风，丁达尔光效，梦幻仙境",
        en: "In the depths of a sun-drenched forest, a massive mystical creature (half deer, half cat, with glowing antlers) being touched by a little girl, surrounded by dancing golden fireflies, Studio Ghibli style, Tyndall light effect, dreamlike wonderland."
    },
    {
        zh: "雨夜老巷深处的一家面馆，暖黄灯笼的光晕倒映在潮湿的青石板积水中，一辆老式自行车停在门口，蒸汽从档口袅袅升起，纪实街头摄影，胶片质感，冷暖色调对比",
        en: "A tiny noodle shop deep in an old alley on a rainy night, warm paper-lantern glow reflected in wet flagstone puddles, a vintage bicycle parked by the door, steam rising from the counter, documentary street photography, film grain, cold and warm color contrast."
    }
];

// Tab Switch (T2I / I2I panes) — named distinctly from the host app's own switchTab()
function switchImageStudioTab(tabId) {
    imgStudioCurrentTab = tabId;
    document.querySelectorAll('#imgstudio-controls .panel-tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    document.getElementById(`imgstudio-tab-${tabId}`).classList.add('active');

    document.querySelectorAll('#imgstudio-controls .input-pane').forEach(pane => {
        pane.classList.remove('active');
    });
    document.getElementById(`imgstudio-pane-${tabId}`).classList.add('active');
}

// Selection Handlers for Custom Grids (Ratio, Quality, Styles)
function initImageStudioSelectors() {
    const t2iRatioCards = document.querySelectorAll('#t2i-ratio-selector .ratio-card');
    t2iRatioCards.forEach(card => {
        card.addEventListener('click', () => {
            t2iRatioCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            imgStudioSelectedT2iRatio = card.getAttribute('data-ratio');
        });
    });

    const i2iRatioCards = document.querySelectorAll('#i2i-ratio-selector .ratio-card');
    i2iRatioCards.forEach(card => {
        card.addEventListener('click', () => {
            i2iRatioCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            imgStudioSelectedI2iRatio = card.getAttribute('data-ratio');
        });
    });

    const t2iQualityCards = document.querySelectorAll('#t2i-quality-selector .quality-card');
    t2iQualityCards.forEach(card => {
        card.addEventListener('click', () => {
            t2iQualityCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            imgStudioSelectedT2iQuality = card.getAttribute('data-quality');
        });
    });

    const i2iQualityCards = document.querySelectorAll('#i2i-quality-selector .quality-card');
    i2iQualityCards.forEach(card => {
        card.addEventListener('click', () => {
            i2iQualityCards.forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            imgStudioSelectedI2iQuality = card.getAttribute('data-quality');
        });
    });

    // Style Tags adding to prompt
    const styleTags = document.querySelectorAll('#imgstudio-controls .style-tag');
    const promptTextarea = document.getElementById('t2i-prompt');

    styleTags.forEach(tag => {
        tag.addEventListener('click', () => {
            const tagText = tag.getAttribute('data-tag');
            const currentPrompt = promptTextarea.value;

            if (tag.classList.contains('active')) {
                tag.classList.remove('active');
                promptTextarea.value = currentPrompt.replace(tagText, '');
            } else {
                tag.classList.add('active');
                promptTextarea.value = currentPrompt + tagText;
            }
        });
    });

    document.getElementById('t2i-clear-btn').addEventListener('click', () => {
        document.getElementById('t2i-prompt').value = '';
        styleTags.forEach(t => t.classList.remove('active'));
    });
    document.getElementById('i2i-clear-btn').addEventListener('click', () => {
        document.getElementById('i2i-prompt').value = '';
    });

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

function imgStudioSetRandomPrompt(options = {}) {
    const { silent = false, onlyIfEmpty = false } = options;
    const textarea = document.getElementById('t2i-prompt');
    if (!textarea) return;
    // 页面加载时的静默预填不能覆盖用户已有输入，也不该在别的工作区弹 toast
    if (onlyIfEmpty && textarea.value.trim()) return;

    const randomIndex = Math.floor(Math.random() * IMGSTUDIO_CREATIVE_PROMPTS.length);
    const prompt = IMGSTUDIO_CREATIVE_PROMPTS[randomIndex];
    textarea.value = Math.random() > 0.4 ? prompt.zh : prompt.en;

    document.querySelectorAll('#imgstudio-controls .style-tag').forEach(t => t.classList.remove('active'));
    if (!silent) showToast('灵感提示词已载入，可直接点击生成', 'success');
}

// Drag & Drop / File Uploader Setup
function initImageStudioFileUploader() {
    const dragArea = document.getElementById('i2i-drag-area');
    const fileInput = document.getElementById('i2i-file-input');

    dragArea.addEventListener('click', () => {
        fileInput.click();
    });

    fileInput.addEventListener('change', (e) => {
        imgStudioHandleFiles(e.target.files);
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
        imgStudioHandleFiles(e.dataTransfer.files);
    }, false);
}

function imgStudioHandleFiles(files) {
    if (!files || files.length === 0) return;

    Array.from(files).forEach(file => {
        if (!file.type.startsWith('image/')) {
            showToast(`文件 ${file.name} 不是合法的图片格式`, 'error');
            return;
        }

        if (file.size > 10 * 1024 * 1024) {
            showToast(`文件 ${file.name} 超过了 10MB 的上限`, 'error');
            return;
        }

        const item = { name: file.name, file: file, base64: '' };
        imgStudioUploadedFiles.push(item);

        const reader = new FileReader();
        reader.onload = (e) => {
            item.base64 = e.target.result;
            imgStudioRenderUploadPreviews();
        };
        reader.onerror = () => {
            imgStudioUploadedFiles = imgStudioUploadedFiles.filter(uploaded => uploaded !== item);
            imgStudioRenderUploadPreviews();
            showToast(`无法读取图片 ${file.name}`, 'error');
        };
        reader.readAsDataURL(file);
    });

    imgStudioRenderUploadPreviews();
}

function imgStudioRenderUploadPreviews() {
    const container = document.getElementById('i2i-upload-previews');

    if (imgStudioUploadedFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    container.style.display = 'flex';
    container.innerHTML = '';

    imgStudioUploadedFiles.forEach((item, index) => {
        const card = document.createElement('div');
        card.className = 'upload-preview-card';
        card.innerHTML = `
            ${item.base64 ? `<img src="${item.base64}" alt="${item.name}">` : '<div class="upload-preview-loading">读取中…</div>'}
            <button type="button" class="upload-preview-delete" onclick="removeImageStudioFile(${index})">✕</button>
        `;
        container.appendChild(card);
    });
}

window.removeImageStudioFile = function (index) {
    imgStudioUploadedFiles.splice(index, 1);
    imgStudioRenderUploadPreviews();
};

// Action: Generate Image (T2I & I2I Router)
function imgStudioTriggerGeneration() {
    const generateBtn = document.getElementById('imgstudio-generate-btn');

    generateBtn.disabled = true;
    setTimeout(() => { generateBtn.disabled = false; }, 200);

    if (imgStudioCurrentTab === 't2i') {
        const prompt = document.getElementById('t2i-prompt').value.trim();
        const model = document.getElementById('t2i-model').value;

        if (!prompt) {
            showToast('请输入创意提示词再开始生成', 'error');
            return;
        }

        imgStudioAddTask('t2i', prompt, model, imgStudioSelectedT2iRatio, imgStudioSelectedT2iQuality, null);
        switchImageStudioMobileView('result');
        showToast('文生图任务已加入生成队列', 'success');
    } else {
        const prompt = document.getElementById('i2i-prompt').value.trim();
        const model = document.getElementById('i2i-model').value;
        const style = document.getElementById('i2i-style').value;

        if (imgStudioUploadedFiles.length === 0) {
            showToast('请至少上传一张参考图片再开始图生图', 'error');
            return;
        }
        if (!prompt) {
            showToast('请输入修改或编辑指令提示词', 'error');
            return;
        }

        const filesData = imgStudioUploadedFiles.map(f => ({ name: f.name, base64: f.base64 }));
        const extraData = { style: style, files: filesData };
        const finalRatio = imgStudioSelectedI2iRatio === 'auto' ? 'Auto' : imgStudioSelectedI2iRatio;

        imgStudioAddTask('i2i', prompt, model, finalRatio, imgStudioSelectedI2iQuality, extraData);
        switchImageStudioMobileView('result');
        showToast('图生图修改任务已加入生成队列', 'success');
    }
}

function imgStudioBase64ToBlob(base64, mimeType) {
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

function imgStudioLoadTaskList() {
    const saved = localStorage.getItem('spark_image_tasks');
    if (saved) {
        try {
            imgStudioTaskList = JSON.parse(saved);
            imgStudioTaskList = imgStudioTaskList.filter(task => !task.image || !task.image.includes('undefined'));
            imgStudioTaskList.forEach(task => {
                task.controller = null;
                // 旧版本存档没有动态流字段：补默认值，别让渲染层到处判空
                if (!task.createdAt) task.createdAt = Date.now();
                if (!Array.isArray(task.stages)) task.stages = [];
                if (task.status === 'pending') {
                    if (task.backendTaskId) {
                        imgStudioPushStage(task, '页面刷新，恢复轮询中…');
                        imgStudioPollTaskStatus(task);
                    } else {
                        task.status = 'failed';
                        task.error = '页面刷新，任务被中断';
                        task.finishedAt = task.finishedAt || Date.now();
                        imgStudioPushStage(task, '❌ 页面刷新，任务被中断（尚未提交到后端）');
                    }
                }
            });
            imgStudioSaveTaskList();
        } catch (e) {
            imgStudioTaskList = [];
        }
    } else {
        imgStudioTaskList = [];
    }
    imgStudioRenderTaskListUI();
}

function imgStudioSaveTaskList() {
    // 序列化时剥离参考图 base64：几张参考图就能把 5MB 的 localStorage 配额打爆，
    // 之前配额溢出是静默的，整个任务队列的持久化会就此失效
    const serialize = () => imgStudioTaskList.map(task => {
        const { controller, extraData, ...serializable } = task;
        if (extraData) {
            const { files, ...restExtra } = extraData;
            serializable.extraData = restExtra;
            if (files && files.length) {
                serializable.extraData.fileNames = files.map(f => f.name);
                serializable.filesStripped = true;
            }
        }
        return serializable;
    });
    try {
        localStorage.setItem('spark_image_tasks', JSON.stringify(serialize()));
    } catch (e) {
        // 配额不足：丢弃已完成任务里内联的 data: 图片后再试一次
        console.warn('任务列表保存失败（疑似超出 localStorage 配额），压缩后重试:', e);
        try {
            const slim = serialize().map(t =>
                (t.image && typeof t.image === 'string' && t.image.startsWith('data:')) ? { ...t, image: null } : t
            );
            localStorage.setItem('spark_image_tasks', JSON.stringify(slim));
        } catch (e2) {
            console.error('任务队列持久化失败:', e2);
            showToast('任务队列本地持久化失败（浏览器存储空间不足）', 'warning');
        }
    }
}

/* ── 实时生成动态（Live Feed）────────────────────────────────────────
   任务不再以"队列卡片"整表重建展示：每个任务是一条动态条目，阶段行只增量
   追加、节点只建一次（整表重建会把用户的滚动位置顶回去——任务抽屉踩过的坑）。
   数组顺序仍是新任务在前（unshift），渲染时倒序遍历 = 旧在上、新在下，
   容器贴底时自动跟随最新动态。 */

let imgStudioFeedTicker = null;

function imgStudioPushStage(task, text) {
    if (!Array.isArray(task.stages)) task.stages = [];
    const last = task.stages[task.stages.length - 1];
    if (last && last.text === text) return;
    task.stages.push({ t: Date.now(), text });
    if (task.stages.length > 40) {
        task.stages.splice(0, task.stages.length - 40);
        task._stagesTrimmed = true;
    }
}

function imgStudioFmtClock(ms) {
    const d = new Date(ms);
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function imgStudioTaskElapsedText(task) {
    const base = task.createdAt || Date.now();
    const end = task.finishedAt || Date.now();
    const s = Math.max(0, Math.round((end - base) / 1000));
    return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

function imgStudioLatestPendingTask() {
    return imgStudioTaskList.find(t => t.status === 'pending') || null; // 数组新在前
}

function imgStudioFeedActionsHTML(task) {
    if (task.status === 'pending') {
        return `<button type="button" class="task-btn-icon danger-hover" title="取消任务" onclick="cancelImageStudioTask('${task.id}')">✕</button>`;
    }
    const retryBtn = (task.status === 'failed' || task.status === 'cancelled')
        ? `<button type="button" class="task-btn-icon" title="重试任务" onclick="retryImageStudioTask('${task.id}')">🔄</button>` : '';
    return `${retryBtn}<button type="button" class="task-btn-icon danger-hover" title="删除记录" onclick="deleteImageStudioTask('${task.id}')">✕</button>`;
}

const IMGSTUDIO_STATUS_TEXT = { pending: '渲染中', completed: '已完成', failed: '失败', cancelled: '已取消' };

function imgStudioBuildFeedEntry(task) {
    const node = document.createElement('div');
    node.id = `feed-entry-${task.id}`;
    node.className = 'feed-entry';
    node.innerHTML = `
        <div class="feed-entry-head">
            <span class="task-badge-type">${task.type === 't2i' ? '文生图' : '图生图'}</span>
            <span class="feed-model" translate="no">${escapeHtml((task.model || '').replace('-image', ''))}</span>
            <span class="feed-meta">${escapeHtml(task.ratio || '')} · ${escapeHtml(task.quality || '')}</span>
            <span class="feed-head-right">
                <span class="feed-status-chip"></span>
                <span class="feed-actions"></span>
            </span>
        </div>
        <div class="feed-entry-prompt" title="${escapeHtml(task.prompt)}">${escapeHtml(task.prompt)}</div>
        <div class="feed-stage-lines"></div>
        <div class="feed-entry-result" style="display:none;"></div>
    `;
    return node;
}

function imgStudioUpdateFeedEntry(node, task) {
    // 状态胶囊 + 操作按钮：仅在状态变化时重建
    if (node.dataset.status !== task.status) {
        node.dataset.status = task.status;
        node.className = `feed-entry st-${task.status}`;
        const chip = node.querySelector('.feed-status-chip');
        if (chip) {
            chip.className = `feed-status-chip task-status-badge ${task.status}`;
            chip.innerHTML = task.status === 'pending'
                ? `${IMGSTUDIO_STATUS_TEXT.pending} <span class="feed-elapsed">(${imgStudioTaskElapsedText(task)})</span>`
                : `${IMGSTUDIO_STATUS_TEXT[task.status] || task.status} · ${imgStudioTaskElapsedText(task)}`;
            if (task.status === 'failed') chip.title = task.error || '';
        }
        const actions = node.querySelector('.feed-actions');
        if (actions) actions.innerHTML = imgStudioFeedActionsHTML(task);
    }

    // 阶段行：只追加新行；被截断或重试重置过的整块重建
    const linesBox = node.querySelector('.feed-stage-lines');
    const stages = task.stages || [];
    let rendered = parseInt(node.dataset.stageCount || '0', 10);
    if (task._stagesTrimmed || rendered > stages.length) {
        linesBox.innerHTML = '';
        rendered = 0;
        task._stagesTrimmed = false;
    }
    for (let i = rendered; i < stages.length; i++) {
        const line = document.createElement('div');
        line.className = 'feed-stage-line';
        line.innerHTML = `<span class="feed-stage-time">[${imgStudioFmtClock(stages[i].t)}]</span> ${escapeHtml(stages[i].text)}`;
        linesBox.appendChild(line);
    }
    node.dataset.stageCount = String(stages.length);
    if (stages.length !== rendered) linesBox.scrollTop = linesBox.scrollHeight;

    // 完成缩略图（只设一次）
    if (task.status === 'completed' && task.image && !node.dataset.thumbSet) {
        node.dataset.thumbSet = '1';
        const box = node.querySelector('.feed-entry-result');
        if (box) {
            box.style.display = 'flex';
            box.innerHTML = `<img src="${task.image}" alt="生成结果" onclick="viewImageStudioTaskItem('${task.id}')" title="点击在渲染室大屏查看">`;
        }
    }
}

function imgStudioSyncFeedTicker(activeCount) {
    // 秒表只在有渲染中任务时运转，空闲即停——不留常驻定时器
    if (activeCount > 0 && !imgStudioFeedTicker) {
        imgStudioFeedTicker = setInterval(() => {
            let anyPending = false;
            imgStudioTaskList.forEach(task => {
                if (task.status !== 'pending') return;
                anyPending = true;
                const el = document.querySelector(`#feed-entry-${task.id} .feed-elapsed`);
                if (el) el.textContent = `(${imgStudioTaskElapsedText(task)})`;
            });
            const sk = document.getElementById('spotlight-skeleton');
            if (sk && sk.style.display !== 'none') {
                const t = imgStudioLatestPendingTask();
                const st = sk.querySelector('.loader-status-text');
                if (t && st) {
                    const stageText = (t.stages && t.stages.length) ? t.stages[t.stages.length - 1].text : '渲染中';
                    st.textContent = `${stageText} · ${imgStudioTaskElapsedText(t)}`;
                }
            }
            if (!anyPending && imgStudioFeedTicker) {
                clearInterval(imgStudioFeedTicker);
                imgStudioFeedTicker = null;
            }
        }, 1000);
    } else if (activeCount === 0 && imgStudioFeedTicker) {
        clearInterval(imgStudioFeedTicker);
        imgStudioFeedTicker = null;
    }
}

function imgStudioSyncSpotlightSkeleton(activeCount) {
    // 渲染室大屏联动：有任务渲染中且没有成品在展示时，亮起骨架屏当"生成进程"主视觉
    const skeleton = document.getElementById('spotlight-skeleton');
    const placeholder = document.getElementById('spotlight-placeholder');
    const imageWrapper = document.getElementById('spotlight-image-wrapper');
    if (!skeleton || !placeholder || !imageWrapper) return;
    const imageShowing = imageWrapper.style.display !== 'none' && imageWrapper.style.display !== '';
    if (activeCount > 0 && !imageShowing) {
        placeholder.style.display = 'none';
        skeleton.style.display = 'flex';
    } else if (activeCount === 0 && skeleton.style.display === 'flex') {
        skeleton.style.display = 'none';
        if (!imageShowing) placeholder.style.display = 'flex';
    }
}

function imgStudioRenderTaskListUI() {
    const activeCount = imgStudioTaskList.filter(t => t.status === 'pending').length;
    const totalCount = imgStudioTaskList.length;

    const activeCountSpan = document.getElementById('tasks-active-count');
    const totalCountSpan = document.getElementById('tasks-total-count');
    const section = document.getElementById('tasks-section');
    const container = document.getElementById('imgstudio-tasks-list');

    if (activeCountSpan) activeCountSpan.textContent = activeCount;
    if (totalCountSpan) totalCountSpan.textContent = totalCount;
    if (section) section.style.display = totalCount > 0 ? 'block' : 'none';
    const liveDot = document.getElementById('feed-live-dot');
    if (liveDot) liveDot.classList.toggle('active', activeCount > 0);

    const mobileBadge = document.getElementById('imgstudio-mobile-active-badge');
    if (mobileBadge) {
        mobileBadge.textContent = activeCount;
        mobileBadge.style.display = activeCount > 0 ? 'inline-block' : 'none';
    }

    if (!container) return;

    // 先记滚动位置：只有本就贴底才自动跟随，不打断用户回看历史动态
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;

    const alive = new Set(imgStudioTaskList.map(t => `feed-entry-${t.id}`));
    Array.from(container.children).forEach(child => {
        if (!alive.has(child.id)) child.remove();
    });

    for (let i = imgStudioTaskList.length - 1; i >= 0; i--) {
        const task = imgStudioTaskList[i];
        let node = document.getElementById(`feed-entry-${task.id}`);
        if (!node) {
            node = imgStudioBuildFeedEntry(task);
            container.appendChild(node);
        }
        imgStudioUpdateFeedEntry(node, task);
    }

    if (nearBottom || container.dataset.forceScroll === '1') {
        container.scrollTop = container.scrollHeight;
        delete container.dataset.forceScroll;
    }

    imgStudioSyncFeedTicker(activeCount);
    imgStudioSyncSpotlightSkeleton(activeCount);
}

function imgStudioAddTask(type, prompt, model, ratio, quality, extraData) {
    const id = 'task_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
    const task = {
        id, type, prompt, model, ratio, quality,
        status: 'pending',
        error: null,
        image: null,
        timestamp: new Date().toLocaleTimeString(),
        createdAt: Date.now(),
        finishedAt: null,
        stages: [],
        lastStage: null,
        controller: null,
        extraData: extraData
    };
    imgStudioPushStage(task, `任务已提交（${type === 't2i' ? '文生图' : '图生图'} · ${model} · ${ratio} · ${quality}）`);

    imgStudioTaskList.unshift(task);

    if (imgStudioTaskList.length > 15) {
        const removed = imgStudioTaskList.pop();
        if (removed && removed.status === 'pending' && removed.controller) {
            try { removed.controller.abort(); } catch (e) {}
        }
    }

    // 新任务永远滚入视野（即便用户此前把动态流滚到了别处）
    const container = document.getElementById('imgstudio-tasks-list');
    if (container) container.dataset.forceScroll = '1';

    imgStudioSaveTaskList();
    imgStudioRenderTaskListUI();
    imgStudioRunTaskFetch(task);
    return task;
}

async function imgStudioPollTaskStatus(task) {
    if (!task.backendTaskId) return;

    // 长轮询推送（毫秒级同步）：请求带 wait+since 指纹在服务端挂起，任务的
    // 状态/阶段一变化立即返回——上游秒报错，动态流秒可见；无变化则 ~20s 一轮空转。
    // 连续失败上限：服务挂掉后不再让任务永远转圈「渲染中」（失败间隔 1.5s，约 60s 放弃）。
    let failStreak = 0;
    const MAX_FAIL_STREAK = 40;
    let fingerprint = '';

    const liveTask = () => {
        const cur = imgStudioTaskList.find(t => t.id === task.id);
        return (cur && cur.status === 'pending') ? cur : null;
    };

    while (true) {
        const currentTask = liveTask();
        if (!currentTask) return;

        const registerFailure = async () => {
            failStreak += 1;
            if (failStreak >= MAX_FAIL_STREAK) {
                currentTask.status = 'failed';
                currentTask.finishedAt = Date.now();
                currentTask.error = '与本地服务失联，已停止轮询（服务恢复后可点重试）';
                currentTask.controller = null;
                imgStudioPushStage(currentTask, '❌ 与本地服务失联，已停止轮询');
                imgStudioSaveTaskList();
                imgStudioRenderTaskListUI();
                return true;
            }
            await new Promise(r => setTimeout(r, 1500));
            return false;
        };

        try {
            const qs = `task_id=${encodeURIComponent(task.backendTaskId)}&wait=20&since=${encodeURIComponent(fingerprint)}`;
            const response = await fetch(`/api/image/task/status?${qs}`);
            if (!response.ok) { if (await registerFailure()) return; continue; }
            failStreak = 0;

            const data = await response.json();
            if (!data) continue;
            fingerprint = data.fingerprint || '';

            if (data.status === 'pending') {
                // 后端 worker 汇报的真实阶段（排队/上游渲染/上游报错重试/落盘）→ 追加进动态流
                if (data.stage && data.stage !== currentTask.lastStage) {
                    currentTask.lastStage = data.stage;
                    imgStudioPushStage(currentTask, data.stage);
                    imgStudioSaveTaskList();
                    imgStudioRenderTaskListUI();
                }
            } else if (data.status === 'completed') {
                currentTask.finishedAt = Date.now();

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
                        imgStudioPushStage(currentTask, `✅ 渲染完成，已存入历史画廊（总用时 ${imgStudioTaskElapsedText(currentTask)}）`);

                        imgStudioSaveToHistory(currentTask.type, currentTask.prompt, currentTask.model, currentTask.ratio, currentTask.quality, imgDataUrl);
                        imgStudioDisplaySpotlight(imgDataUrl, currentTask.prompt, currentTask.model, currentTask.ratio, currentTask.quality);
                        showToast('图像渲染成功！已存入历史画廊', 'success');
                    } else {
                        currentTask.status = 'failed';
                        currentTask.error = '返回结果中无有效图像数据';
                        currentTask.controller = null;
                        imgStudioPushStage(currentTask, '❌ 失败：返回结果中无有效图像数据');
                        showToast('生图失败: 未在响应中找到图像数据', 'error');
                    }
                } else {
                    currentTask.status = 'failed';
                    currentTask.error = '返回的数据格式不正确';
                    currentTask.controller = null;
                    imgStudioPushStage(currentTask, '❌ 失败：返回的数据格式不正确');
                    showToast('生图失败: 数据格式不正确', 'error');
                }

                imgStudioSaveTaskList();
                imgStudioRenderTaskListUI();
                return; // 终态，长轮询循环结束
            } else if (data.status === 'failed' || data.status === 'not_found') {
                currentTask.status = 'failed';
                currentTask.finishedAt = Date.now();
                currentTask.error = data.status === 'not_found' ? '后台任务未找到（可能服务已重启）' : (data.error || '未知后台错误');
                currentTask.controller = null;
                imgStudioPushStage(currentTask, `❌ 失败：${currentTask.error}`);
                showToast(`生图失败: ${currentTask.error}`, 'error');
                imgStudioSaveTaskList();
                imgStudioRenderTaskListUI();
                return; // 终态，长轮询循环结束
            }
        } catch (e) {
            console.error("Polling error:", e);
            if (await registerFailure()) return;
        }
    }
}

async function imgStudioRunTaskFetch(task) {
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
                config: config
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
                    const blob = imgStudioBase64ToBlob(f.base64, mime);
                    if (blob) {
                        const file = new File([blob], f.name, { type: mime });
                        const fieldName = idx === 0 ? 'image' : 'image[]';
                        formData.append(fieldName, file, f.name);
                    }
                });
            }

            formData.append('config', JSON.stringify(config));

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
            imgStudioPushStage(task, `后端已受理（任务 ${result.task_id.slice(-8)}），等待上游返回…`);
            imgStudioSaveTaskList();
            imgStudioPollTaskStatus(task);
        } else {
            throw new Error(result.error || result.message || '任务提交失败');
        }
    } catch (e) {
        if (task.status === 'cancelled') return;

        console.error(e);
        task.status = 'failed';
        task.finishedAt = Date.now();
        task.error = e.name === 'AbortError' ? '请求超时或被手动取消' : e.message;
        task.controller = null;
        imgStudioPushStage(task, `❌ 提交失败：${task.error}`);
        showToast(`生图失败: ${task.error}`, 'error');
    } finally {
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
    }
}

function cancelImageStudioTask(taskId) {
    const task = imgStudioTaskList.find(t => t.id === taskId);
    if (task && task.status === 'pending') {
        task.status = 'cancelled';
        task.finishedAt = Date.now();
        imgStudioPushStage(task, '⏹ 已被用户取消');
        if (task.controller) {
            try { task.controller.abort(); } catch (e) { console.error(e); }
        }
        task.controller = null;
        // 通知后端放弃该任务：之前只掐前端 fetch，后台 worker 会继续烧上游配额
        if (task.backendTaskId) {
            fetch('/api/image/task/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: task.backendTaskId })
            }).catch(() => { /* 服务不可达时取消请求本身失败可忽略 */ });
        }
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
        showToast('任务已取消', 'success');
    }
}

function deleteImageStudioTask(taskId) {
    const index = imgStudioTaskList.findIndex(t => t.id === taskId);
    if (index > -1) {
        const task = imgStudioTaskList[index];
        if (task.status === 'pending') {
            cancelImageStudioTask(taskId);
        }
        imgStudioTaskList.splice(index, 1);
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
        showToast('任务记录已删除', 'success');
    }
}

function retryImageStudioTask(taskId) {
    const task = imgStudioTaskList.find(t => t.id === taskId);
    if (!task || task.status === 'pending') return;

    // 页面刷新后参考图 base64 不再持久化（防爆 localStorage 配额）：
    // 没有参考图的 i2i 重试会静默变成"无参考图生图"，必须拦下
    const hasFiles = task.extraData && task.extraData.files && task.extraData.files.length > 0;
    if (task.type === 'i2i' && !hasFiles) {
        task.status = 'failed';
        task.finishedAt = Date.now();
        task.error = '参考图已失效（页面刷新后不保留），请重新上传后再生成';
        imgStudioPushStage(task, '❌ 参考图已失效（页面刷新后不保留），无法重试');
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
        showToast('参考图已失效，请重新上传后再生成', 'warning');
        return;
    }

    task.status = 'pending';
    task.error = null;
    task.image = null;
    task.backendTaskId = null;
    task.timestamp = new Date().toLocaleTimeString();
    // 动态流从头计时、从头记录；旧条目节点删掉由渲染层重建（清缩略图/旧阶段行）
    task.createdAt = Date.now();
    task.finishedAt = null;
    task.stages = [];
    task.lastStage = null;
    imgStudioPushStage(task, '🔄 重试：任务重新提交');
    const oldNode = document.getElementById(`feed-entry-${task.id}`);
    if (oldNode) oldNode.remove();
    imgStudioSaveTaskList();
    imgStudioRenderTaskListUI();
    imgStudioRunTaskFetch(task);
    showToast('正在重试生图任务...', 'success');
}

function clearCompletedImageStudioTasks() {
    const activeTasks = imgStudioTaskList.filter(t => t.status === 'pending');
    const hasCompleted = imgStudioTaskList.length > activeTasks.length;
    if (hasCompleted) {
        imgStudioTaskList = activeTasks;
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
        showToast('已清除已完成和已失效的任务记录', 'success');
    } else {
        showToast('没有可清除的记录', 'success');
    }
}

function clearAllImageStudioTasks() {
    if (imgStudioTaskList.length === 0) return;
    if (confirm('确认清空所有任务记录？运行中的任务将被取消。')) {
        imgStudioTaskList.forEach(task => {
            if (task.status === 'pending' && task.controller) {
                try { task.controller.abort(); } catch (e) {}
            }
        });
        imgStudioTaskList = [];
        imgStudioSaveTaskList();
        imgStudioRenderTaskListUI();
        showToast('所有任务已成功清空', 'success');
    }
}

window.cancelImageStudioTask = cancelImageStudioTask;
window.deleteImageStudioTask = deleteImageStudioTask;
window.retryImageStudioTask = retryImageStudioTask;
window.viewImageStudioTaskItem = function (taskId) {
    const task = imgStudioTaskList.find(t => t.id === taskId);
    if (task && task.status === 'completed' && task.image) {
        imgStudioDisplaySpotlight(task.image, task.prompt, task.model, task.ratio, task.quality);
        showToast('已在渲染室展示该图片', 'success');
    }
};

function imgStudioDisplaySpotlight(imgDataUrl, prompt, model, ratio, quality) {
    document.getElementById('spotlight-skeleton').style.display = 'none';
    document.getElementById('spotlight-placeholder').style.display = 'none';
    document.getElementById('spotlight-image-wrapper').style.display = 'flex';

    document.getElementById('spotlight-img').src = imgDataUrl;
    document.getElementById('spotlight-info-model').textContent = model.replace('-image', '');
    document.getElementById('spotlight-info-prompt').textContent = prompt;

    imgStudioSetupSpotlightActions(imgDataUrl, prompt, model, ratio, quality);
}

function imgStudioResetSpotlightUI() {
    document.getElementById('spotlight-skeleton').style.display = 'none';
    document.getElementById('spotlight-image-wrapper').style.display = 'none';
    document.getElementById('spotlight-placeholder').style.display = 'flex';
}

function imgStudioCaptionFor(item) {
    return `
        <strong>提示词:</strong> ${item.prompt}<br>
        <span style="font-size:0.75rem; color: #94a3b8; display:block; margin-top:0.4rem">
            模型: ${item.model} | 比例: ${item.ratio} | 画质: ${item.quality}
        </span>
    `;
}

function imgStudioSetupSpotlightActions(imgDataUrl, prompt, model, ratio, quality) {
    document.getElementById('spotlight-zoom-btn').onclick = () => {
        const spotlightImg = document.getElementById('spotlight-img');
        const currentSrc = spotlightImg ? spotlightImg.src : imgDataUrl;

        const clickedIndex = imgStudioHistory.findIndex(item => item.image === currentSrc);
        if (clickedIndex === -1) {
            const currentItem = { type: 'image', url: currentSrc, caption: imgStudioCaptionFor({ prompt, model, ratio, quality }) };
            const mediaList = [currentItem, ...imgStudioHistory.map(item => ({ type: 'image', url: item.image, caption: imgStudioCaptionFor(item) }))];
            openLightbox(mediaList, 0);
        } else {
            const mediaList = imgStudioHistory.map(item => ({ type: 'image', url: item.image, caption: imgStudioCaptionFor(item) }));
            openLightbox(mediaList, clickedIndex);
        }
    };

    document.getElementById('spotlight-download-btn').onclick = () => {
        const ext = /\.webp(\?|$)/i.test(imgDataUrl) ? 'webp' : 'png';
        imgStudioDownloadImage(imgDataUrl, `spark_${Date.now()}.${ext}`);
    };

    document.getElementById('spotlight-copy-btn').onclick = () => {
        imgStudioCopyImageToClipboard(imgDataUrl);
    };

    document.getElementById('spotlight-reuse-btn').onclick = () => {
        imgStudioReusePrompt(prompt, ratio, quality, model);
    };

    document.getElementById('spotlight-to-i2i-btn').onclick = () => {
        imgStudioSendToImageToImage(imgDataUrl, `ref_spark_${Date.now()}.png`);
    };
}

function imgStudioDownloadImage(dataUrl, filename) {
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast('图片下载已启动', 'success');
}

/** 任意图片 Blob → PNG Blob（Chromium 剪贴板只收 image/png，后端现在返回 WebP URL）。 */
function imgStudioBlobToPngBlob(blob) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
            try {
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth;
                canvas.height = img.naturalHeight;
                canvas.getContext('2d').drawImage(img, 0, 0);
                canvas.toBlob(b => (b ? resolve(b) : reject(new Error('PNG 转码失败'))), 'image/png');
            } catch (e) {
                reject(e);
            } finally {
                URL.revokeObjectURL(url);
            }
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片加载失败')); };
        img.src = url;
    });
}

async function imgStudioCopyImageToClipboard(dataUrl) {
    try {
        // 把 Promise<Blob> 直接交给 ClipboardItem：保住用户手势上下文（Safari 要求），
        // 同时统一转码成 PNG——直接写 image/webp 会被 Chromium 拒绝
        const pngPromise = (async () => {
            const response = await fetch(dataUrl);
            let blob = await response.blob();
            if (blob.type !== 'image/png') blob = await imgStudioBlobToPngBlob(blob);
            return blob;
        })();
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': pngPromise })]);
        showToast('图片已复制到剪贴板', 'success');
    } catch (err) {
        console.error('Copy image failed', err);
        showToast('复制图片失败（浏览器权限受限或格式不支持），可改用下载按钮保存', 'error');
    }
}

function imgStudioReusePrompt(prompt, ratio, quality, model) {
    if (imgStudioCurrentTab === 't2i') {
        document.getElementById('t2i-prompt').value = prompt;
        if (model) {
            const modelSelect = document.getElementById('t2i-model');
            if (modelSelect && Array.from(modelSelect.options).some(o => o.value === model)) {
                modelSelect.value = model;
            }
        }
        const ratioCard = document.querySelector(`#t2i-ratio-selector .ratio-card[data-ratio="${ratio}"]`);
        if (ratioCard) ratioCard.click();

        const qualityCard = document.querySelector(`#t2i-quality-selector .quality-card[data-quality="${quality}"]`);
        if (qualityCard) qualityCard.click();
    } else {
        document.getElementById('i2i-prompt').value = prompt;
        if (model) {
            const modelSelect = document.getElementById('i2i-model');
            if (modelSelect && Array.from(modelSelect.options).some(o => o.value === model)) {
                modelSelect.value = model;
            }
        }
    }

    showToast('创意提示词已填回配置面板', 'success');
    switchImageStudioMobileView('create');
}

function imgStudioSendToImageToImage(dataUrl, filename) {
    switchImageStudioTab('i2i');
    switchImageStudioMobileView('create');

    fetch(dataUrl)
        .then(res => res.blob())
        .then(blob => {
            const file = new File([blob], filename, { type: "image/png" });
            imgStudioUploadedFiles = [{ name: filename, file: file, base64: dataUrl }];
            imgStudioRenderUploadPreviews();
            showToast('图片已送往图生图作为参考画布！', 'success');
        })
        .catch(e => {
            console.error(e);
            showToast('转换图片失败', 'error');
        });
}

function imgStudioSaveHistoryToStorage() {
    try {
        localStorage.setItem('spark_image_history', JSON.stringify(imgStudioHistory));
    } catch (e) {
        if (e.name === 'QuotaExceededError' || e.code === 22 || e.number === 0x8007000E) {
            console.warn("Storage quota exceeded, trying to prune older items...");
            // Prune in a few large steps instead of one item at a time. The old loop popped a
            // single item and re-JSON.stringify'd the ENTIRE multi-MB array on every iteration
            // (~25x for 30 base64 images, each ~1-3MB) plus re-attempted a failing setItem each
            // time — a multi-second main-thread FREEZE on the delete-history click and at each
            // generation-complete. Trying a handful of "keep newest N" cut points bounds this to
            // at most 4 stringifies of small slices.
            let success = false;
            for (const keep of [10, 5, 3, 1]) {
                if (imgStudioHistory.length <= keep) continue;
                try {
                    localStorage.setItem('spark_image_history', JSON.stringify(imgStudioHistory.slice(0, keep)));
                    success = true;
                    break;
                } catch (innerErr) {
                    // still too big — try keeping fewer
                }
            }

            if (!success) {
                showToast('由于高清大图尺寸过大，无法存入浏览器本地持久缓存。图片已在当前会话中生成，刷新页面后将消失，请及时下载保存！', 'error');
            } else {
                showToast('本地浏览器缓存已满，已自动清理较旧的生图记录以释放空间。', 'success');
            }
        } else {
            console.error("Failed to save history to localStorage:", e);
        }
    }
}

function imgStudioSaveToHistory(type, prompt, model, ratio, quality, image) {
    const newItem = {
        id: 'img_' + Date.now(),
        type, prompt, model, ratio, quality,
        timestamp: new Date().toLocaleTimeString(),
        image
    };

    imgStudioHistory.unshift(newItem);
    if (imgStudioHistory.length > 30) {
        imgStudioHistory = imgStudioHistory.slice(0, 30);
    }

    imgStudioSaveHistoryToStorage();
    imgStudioRenderHistoryGrid();
}

async function imgStudioCheckImageExists(url) {
    if (!url || url.includes('undefined')) return false;
    if (url.startsWith('data:')) return true;
    try {
        const response = await fetch(url, { method: 'HEAD' });
        return response.ok;
    } catch (e) {
        return false;
    }
}

async function imgStudioLoadHistory() {
    const saved = localStorage.getItem('spark_image_history');
    if (saved) {
        try {
            imgStudioHistory = JSON.parse(saved);
        } catch (e) {}
    }

    // Filter out history items whose images no longer exist on the server (non-data URLs only)
    const filteredHistory = [];
    let needCleanSave = false;
    for (const item of imgStudioHistory) {
        if (await imgStudioCheckImageExists(item.image)) {
            filteredHistory.push(item);
        } else {
            needCleanSave = true;
        }
    }
    imgStudioHistory = filteredHistory;
    if (needCleanSave) {
        imgStudioSaveHistoryToStorage();
    }

    imgStudioRenderHistoryGrid();
}

function imgStudioRenderHistoryGrid() {
    const grid = document.getElementById('history-grid');
    const countSpan = document.getElementById('history-count');

    countSpan.textContent = imgStudioHistory.length;

    if (imgStudioHistory.length === 0) {
        grid.innerHTML = '<div class="history-empty-text">画廊目前空空如也。生成的图片都会收录在此处。</div>';
        return;
    }

    grid.innerHTML = '';

    imgStudioHistory.forEach(item => {
        const card = document.createElement('div');
        card.className = 'history-card';
        card.onclick = (e) => {
            if (e.target.closest('button')) return;
            imgStudioDisplaySpotlight(item.image, item.prompt, item.model, item.ratio, item.quality);
        };
        card.innerHTML = `
            <img src="${item.image}" alt="History Item" loading="lazy">
            <div class="history-card-overlay">
                <button class="history-card-btn" title="查看" onclick="viewImageStudioHistoryItem('${item.id}', 'zoom')">🔍</button>
                <button class="history-card-btn" title="下载" onclick="viewImageStudioHistoryItem('${item.id}', 'download')">📥</button>
                <button class="history-card-btn" title="填回提示词" onclick="viewImageStudioHistoryItem('${item.id}', 'reuse')">🔄</button>
                <button class="history-card-btn" title="送往图生图" onclick="viewImageStudioHistoryItem('${item.id}', 'to-i2i')">🎨</button>
                <button class="history-card-btn" style="background:rgba(239, 68, 68, 0.4)" title="删除" onclick="deleteImageStudioHistoryItem('${item.id}')">✕</button>
            </div>
        `;
        grid.appendChild(card);
    });
}

window.viewImageStudioHistoryItem = function (id, action) {
    const item = imgStudioHistory.find(x => x.id === id);
    if (!item) return;

    imgStudioDisplaySpotlight(item.image, item.prompt, item.model, item.ratio, item.quality);

    if (action === 'zoom') {
        const clickedIndex = imgStudioHistory.findIndex(x => x.id === id);
        const mediaList = imgStudioHistory.map(h => ({ type: 'image', url: h.image, caption: imgStudioCaptionFor(h) }));
        openLightbox(mediaList, clickedIndex !== -1 ? clickedIndex : 0);
    } else if (action === 'download') {
        imgStudioDownloadImage(item.image, `spark_${item.id}.png`);
    } else if (action === 'reuse') {
        imgStudioReusePrompt(item.prompt, item.ratio, item.quality, item.model);
    } else if (action === 'to-i2i') {
        imgStudioSendToImageToImage(item.image, `ref_${item.id}.png`);
    }
};

window.deleteImageStudioHistoryItem = function (id) {
    const index = imgStudioHistory.findIndex(x => x.id === id);
    if (index > -1) {
        imgStudioHistory.splice(index, 1);
        imgStudioSaveHistoryToStorage();
        imgStudioRenderHistoryGrid();
        showToast('记录已删除', 'success');
    }
};

function imgStudioClearHistory() {
    if (confirm('确认清空所有生图历史记录？此操作不可撤销。')) {
        imgStudioHistory = [];
        localStorage.removeItem('spark_image_history');
        imgStudioRenderHistoryGrid();
        imgStudioResetSpotlightUI();
        showToast('创作历史画廊已成功清空', 'success');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    imgStudioLoadHistory();
    imgStudioLoadTaskList();
    initImageStudioSelectors();
    initImageStudioFileUploader();
    initImageStudioMobileView();

    const clearCompletedTasksBtn = document.getElementById('clear-completed-tasks-btn');
    if (clearCompletedTasksBtn) clearCompletedTasksBtn.addEventListener('click', clearCompletedImageStudioTasks);

    const clearAllTasksBtn = document.getElementById('clear-all-tasks-btn');
    if (clearAllTasksBtn) clearAllTasksBtn.addEventListener('click', clearAllImageStudioTasks);

    document.getElementById('t2i-random-prompt-btn').addEventListener('click', () => imgStudioSetRandomPrompt());
    document.getElementById('imgstudio-generate-btn').addEventListener('click', imgStudioTriggerGeneration);
    document.getElementById('clear-history-btn').addEventListener('click', imgStudioClearHistory);

    // Ctrl/Cmd+Enter triggers generation only while the Image Studio tab is the active view
    document.addEventListener('keydown', (e) => {
        const panel = document.getElementById('panel-image-studio');
        if (!panel || !panel.classList.contains('mobile-active')) return;
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            const genBtn = document.getElementById('imgstudio-generate-btn');
            if (genBtn && !genBtn.disabled) {
                genBtn.click();
            }
        }
    });

    imgStudioSetRandomPrompt({ silent: true, onlyIfEmpty: true });
});
