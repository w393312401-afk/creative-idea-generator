// --- media_renderer.js ---

// Per-URL cache-bust versions. Previously EVERY render appended `?t=Date.now()`, giving
// each render a unique URL that defeated the browser cache — so re-viewing an idea, switching
// tabs, or re-rendering the frame grid re-downloaded every image (server logs showed each
// frame fetched 10-13x, with visible blank-then-reload flicker). We now use a STABLE version
// per URL: passive re-renders reuse the same URL and hit the cache; only an explicit
// regeneration (bust=true, e.g. a retried frame overwriting the same file) bumps the version
// to force exactly one refetch.
const _imgCacheVersions = new Map();

// manifest 里同一个文件有两种写法（url='/outputs/…'、file='outputs/…'），不同渲染
// 路径用的不一样。按去掉前导斜杠与查询串的路径做键，两种写法共用同一个版本号——
// 否则拿 file 渲染的地方永远看不到用 url 记的那次作废。
function _mediaCacheKey(url) {
    return String(url).split('?')[0].replace(/^\/+/, '');
}

// 服务端刚(重)写过某个文件时调用：递增该 URL 的缓存版本，让下一次渲染
// （无论走哪条渲染路径）强制回源取新图。帧重试/断线恢复等路径先更新数据
// 再整格重渲（bust=false），没有这一步就会拿浏览器缓存里的旧图。
// 拖拽换位/手动上传同理：路径没变、内容变了，版本号是唯一能让浏览器回源的东西。
function bustImageCache(url) {
    if (url) _imgCacheVersions.set(_mediaCacheKey(url), Date.now());
}

// 本地媒体（图片/视频）拼上当前缓存版本；远程与 data: URI 原样返回。
function cacheBustedUrl(url) {
    if (!url) return url;
    const lower = String(url).trim().toLowerCase();
    if (lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('data:')) {
        return url;
    }
    const version = _imgCacheVersions.get(_mediaCacheKey(url));
    if (!version) return url;
    return url + (url.includes('?') ? '&' : '?') + 'v=' + version;
}

function safeSetImageSrc(imgEl, url, bust = false) {
    if (!imgEl) return;
    if (!url) {
        imgEl.removeAttribute('src');
        return;
    }
    const lower = url.trim().toLowerCase();
    const isSafe = lower.startsWith('http://') ||
                   lower.startsWith('https://') ||
                   lower.startsWith('data:image/') ||
                   lower.startsWith('/') ||
                   lower.startsWith('outputs/');
    if (!isSafe) {
        console.warn("Blocked potentially unsafe image URL:", url);
        imgEl.removeAttribute('src');
        return;
    }
    if (bust) bustImageCache(url);
    imgEl.src = cacheBustedUrl(url);
}

// 任务终态质量风险汇总（2026-07-15 事故复盘：各门禁告警散落在日志/单帧徽标里，
// 用户在合并成片前看不到"这单有 2 处空间断裂"的总量信号）。输入 manifest
// （frameRun），返回风险描述数组；无风险返回 null。
function summarizeRunQuality(manifest) {
    if (!manifest) return null;
    const risks = [];
    const frames = manifest.frames || [];
    const count = (pred) => frames.filter(pred).length;
    const flagged = count(f => f.quality_gate === 'sequence_review_flagged' || f.quality_gate === 'vlm_qa_failed');
    // 人工主动描述、但还没点「修复此帧问题」的帧：人已经看出这里不对，合成前更
    // 该提醒一次（见 describeFrameIssue）
    const manualFlagged = count(f => typeof f.manual_issue === 'string' && f.manual_issue.trim());
    const skipped = count(f => f.quality_gate === 'sequence_review_skipped');
    // 从没审过的帧（渲染完就没跑过一致性审查，或审查结论因帧被重渲而作废）：
    // 合成前提醒一次，避免把"没查过"读成"查过没问题"
    const neverReviewed = count(f => f.quality_gate === 'pending_manual_review');
    const degraded = count(f => f.quality_gate === 'i2i_fallback_degraded' || f.quality_gate === 'auto_approved_degraded');
    const stale = count(f => f.stale_lineage);
    const inertia = count(f => typeof f.vlm_qa_reason === 'string' && f.vlm_qa_reason.indexOf('anchor_inertia') !== -1);
    const driftFails = (manifest.chain_drift || []).filter(d => d && d.passed === false).length;
    if (flagged) risks.push(`${flagged} 帧一致性审查未过`);
    if (manualFlagged) risks.push(`${manualFlagged} 帧被人工标记问题、尚未修复`);
    if (skipped) risks.push(`${skipped} 帧未经整套审查（审查服务不可用）`);
    if (neverReviewed) risks.push(`${neverReviewed} 帧尚未跑过一致性审查`);
    if (degraded) risks.push(`${degraded} 帧降级/未核验`);
    if (stale) risks.push(`${stale} 帧血统过期`);
    if (inertia) risks.push(`${inertia} 帧疑似换族惯性卡死`);
    if (driftFails) risks.push(`${driftFails} 个镜头族存在空间断裂（链回望 FAIL）`);
    const videos = manifest.videos || [];
    const vFailed = videos.filter(v => v.status === 'failed').length;
    const vWarned = videos.filter(v => v.process_warned).length;
    if (vFailed) risks.push(`${vFailed} 段视频失败/被门禁拦截`);
    if (vWarned) risks.push(`${vWarned} 段视频过程检测告警（冻结/空心等，宽松档放行）`);
    return risks.length ? risks : null;
}

// 标题双行（TikTok 英文整行 / 国内社媒中文整行）单独可刷新：
// 封面任务后台补齐 social_title_* 字段后需要在不整页重渲的情况下更新这两行
//
// 每行拆成 .social-title-name + .social-title-tags 两个 span（见 splitSocialTitle）：
// 复制按钮取的仍是完整整行（走 getIdeaTikTokMeta，不受这里的拆分影响），拆开只为
// 让手机折叠态能单独把话题串藏掉。用 textContent 逐段写入，标题里的 <> 不会被当标签。
function fillSocialTitleLine(el, text) {
    if (!el) return;
    const { name, tags } = splitSocialTitle(text);
    el.textContent = '';
    const nameEl = document.createElement('span');
    nameEl.className = 'social-title-name';
    nameEl.textContent = name;
    el.appendChild(nameEl);
    if (tags) {
        const tagsEl = document.createElement('span');
        tagsEl.className = 'social-title-tags';
        tagsEl.textContent = ' ' + tags;
        el.appendChild(tagsEl);
    }
    el.title = text || '';
}

function renderIdeaTitles(idea) {
    const titleEl = document.getElementById('idea-title');
    const titleCnEl = document.getElementById('idea-title-cn');
    const meta = getIdeaTikTokMeta(idea);
    if (titleEl) {
        fillSocialTitleLine(titleEl, meta.english);
        titleEl.title = idea.title || meta.english;
    }
    fillSocialTitleLine(titleCnEl, meta.chinese);
}

function renderIdea(result) {
    renderIdeaTitles(result);
    const tagThemeEl = document.getElementById('tag-theme');
    const tagCreativityEl = document.getElementById('tag-creativity');
    if (tagThemeEl) tagThemeEl.textContent = result.theme || '';
    if (tagCreativityEl) tagCreativityEl.textContent = result.creativity || '';

    renderRepairBanner(result.repair_md);
    document.getElementById('idea-prompt-block').textContent = result.prompt_block || '（本次未返回提示词内容）';
    document.getElementById('idea-audit').innerHTML = renderAuditMarkdown(result.audit_md);

    // 提示词槽位卡片已移除：本页仅展示原始 Markdown 提示词块（#idea-prompt-block，见上）。
    // 注意 parsePromptBlock 仍被帧序列渲染用于推算图片槽位，切勿删除其定义。

    // 质量审核状态条（原独立 tab，现为概览页顶部一行）：默认收起，只有真的有
    // 修复建议时才自动展开并高亮——平时它就该是一行"通过"，不占地方。
    const auditDetails = document.getElementById('audit-details');
    const hasRepairs = result.repair_md && result.repair_md.trim() &&
                        !/^PASS/i.test(result.repair_md.trim()) &&
                        !result.repair_md.includes('未发现违规');
    if (auditDetails) {
        if (hasRepairs) {
            auditDetails.open = true;
            auditDetails.classList.add('warning-highlight');
        } else {
            auditDetails.open = false;
            auditDetails.classList.remove('warning-highlight');
        }
    }
    const auditStatusText = document.getElementById('audit-status-text');
    const auditStatusIcon = document.getElementById('audit-status-icon');
    if (auditStatusText) auditStatusText.textContent = hasRepairs ? '有修复建议' : '通过';
    if (auditStatusIcon) auditStatusIcon.textContent = hasRepairs ? '⚠️' : '✅';

    // Sync favorite state
    updateFavoriteButtonState();

    // Render collage preview if available
    const collageWrapper = document.getElementById('collage-preview-wrapper');
    const collageImg = document.getElementById('collage-preview-img');
    const collageDownload = document.getElementById('collage-download-link');
    if (collageWrapper && collageImg) {
        if (result.collage_url) {
            collageImg.src = result.collage_url;
            if (collageDownload) collageDownload.href = result.collage_url;
            collageWrapper.style.display = 'block';
            
            collageImg.onclick = () => {
                openLightbox([{
                    type: 'image',
                    url: result.collage_url,
                    caption: '<strong>视频关键帧多宫格拼图 (Keyframe Collage)</strong>'
                }], 0);
            };
        } else {
            collageWrapper.style.display = 'none';
            collageImg.src = '';
            collageImg.onclick = null;
        }
    }

    // Render covers（若该创意有封面任务在后台跑，hydrateCoverPanel 会立刻改画成加载态）
    renderCoversForIdea(result);
    if (typeof hydrateCoverPanel === 'function') hydrateCoverPanel(result);

    // 切换到（或初次载入）这个创意后，立刻按它是否是某个后台生成任务（帧序列/
    // 视频/封面）的归属对象，重新裁决直播面板的可见性——否则一路后台任务生成
    // 期间反复切换创意时，进度条/直播日志会停留在离开时的状态，显示着跟眼前
    // 这个创意毫无关系的内容（即"实时生成动态日志都共用"）。见 app.js 里
    // syncFramesPanelToCurrentIdea / syncVideosPanelToCurrentIdea /
    // syncCoverPanelToCurrentIdea 的说明。
    if (typeof syncFramesPanelToCurrentIdea === 'function') syncFramesPanelToCurrentIdea();
    if (typeof syncVideosPanelToCurrentIdea === 'function') syncVideosPanelToCurrentIdea();
    if (typeof syncCoverPanelToCurrentIdea === 'function') syncCoverPanelToCurrentIdea();

    // Asynchronously fetch latest manifest (frames & videos) from server if it exists
    fetch(`/api/get_manifest?title=${encodeURIComponent(getIdeaSaveTitle(result))}`)
        .then(resp => {
            if (resp.ok) {
                return resp.json();
            }
            // 404 = 服务端确认该项目在磁盘上已不存在（画廊"删除本组"会 rmtree 整个
            // 目录）。此时本地残留的 frameRun 是幽灵数据——继续渲染会把已删除的帧
            // 从浏览器缓存里"复活"。区别于网络错误（走 catch，保留本地数据兜底）。
            if (resp.status === 404) return null;
            throw new Error(`manifest fetch failed: HTTP ${resp.status}`);
        })
        .then(manifest => {
            if (manifest === null) {
                if (result.frameRun) {
                    delete result.frameRun;
                    saveCurrentIdeaState();
                    const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
                    if (existingIdx !== -1 && savedIdeas[existingIdx].frameRun) {
                        delete savedIdeas[existingIdx].frameRun;
                        saveLibrary();
                    }
                }
            } else {
                result.frameRun = manifest;
                saveCurrentIdeaState();
                const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
                if (existingIdx !== -1) {
                    savedIdeas[existingIdx].frameRun = manifest;
                    saveLibrary();
                }
            }
            // 这是个异步回调：等待期间用户可能已经切到别的创意，此时不该把这份
            // （已经不是当前查看对象的）数据画进 DOM，只需要数据已经同步好即可。
            if (typeof isViewingIdea !== 'function' || isViewingIdea(result.id)) {
                hydrateFramesPanel(result);
                hydrateVideosPanel(result);
            }
        })
        .catch(e => {
            // If not found or error, render using whatever is in result
            if (typeof isViewingIdea !== 'function' || isViewingIdea(result.id)) {
                hydrateFramesPanel(result);
                hydrateVideosPanel(result);
            }
        });
}

/**
 * 帧序列/视频/封面面板的「按创意 hydrate」——切到某个创意页面时调用，决定
 * 面板该显示静态结果还是接管这个创意在后台仍在跑的任务（进度条/网格占位/
 * 帧序列还有滚动动态流回放），取代旧版"面板永远只反映最后一次发起的任务"。
 * 依赖 js/api_client.js 的 getIdeaTaskRecord/isViewingIdea（先加载，运行期调用不受脚本顺序影响）
 * 与 app.js 的 setProgressBar/framesFeedHydrate（同理，事件触发时早已就绪）。
 */
function hydrateFramesPanel(idea) {
    if (!idea) return;
    const rec = (typeof getIdeaTaskRecord === 'function') ? getIdeaTaskRecord(idea.id, 'frames') : null;
    const btn = document.getElementById('generate-frames-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    renderFramesForIdea(idea);
    if (rec) {
        if (btn) btn.disabled = true;
        if (progress) progress.style.display = 'flex';
        if (meta) meta.textContent = rec.meta || '生成中...';
        if (rec.progressInfo && typeof setProgressBar === 'function') setProgressBar('frames', rec.progressInfo);
        if (typeof framesFeedHydrate === 'function') framesFeedHydrate(idea.id);
    } else {
        if (btn) btn.disabled = false;
        if (progress) progress.style.display = 'none';
        const wrap = document.getElementById('frames-live-feed');
        const lines = document.getElementById('frames-live-feed-lines');
        if (wrap) wrap.style.display = 'none';
        if (lines) lines.innerHTML = ''; // 别让上一个查看过的创意的动态行残留在隐藏 DOM 里
    }
    if (typeof updatePipelineBar === 'function') updatePipelineBar();
}

function hydrateVideosPanel(idea) {
    if (!idea) return;
    const rec = (typeof getIdeaTaskRecord === 'function') ? getIdeaTaskRecord(idea.id, 'videos') : null;
    const btn = document.getElementById('generate-videos-btn');
    const progress = document.getElementById('videos-progress');
    const meta = document.getElementById('videos-meta');
    const grid = document.getElementById('videos-grid');
    renderVideosForIdea(idea);
    if (rec) {
        if (btn) btn.disabled = true;
        if (progress) progress.style.display = 'flex';
        if (meta) meta.textContent = rec.meta || '生成中...';
        if (rec.progressInfo && typeof setProgressBar === 'function') setProgressBar('videos', rec.progressInfo);
        // renderVideosForIdea 只画 manifest 里已有结果的槽位；补上还没轮到的槽位占位卡
        if (grid && rec.total) {
            for (let i = 1; i <= rec.total; i++) {
                if (!document.getElementById(`video-slot-${i}`)) {
                    const placeholderCard = document.createElement('div');
                    placeholderCard.className = 'frame-card placeholder-frame-card';
                    placeholderCard.id = `video-slot-${i}`;
                    enableVideoSlotDnd(placeholderCard, i, false);
                    grid.appendChild(placeholderCard);
                    if (typeof renderVideoSlotPending === 'function') renderVideoSlotPending(i, '等待中');
                }
            }
        }
    } else {
        if (btn) btn.disabled = false;
        if (progress) progress.style.display = 'none';
    }
    if (typeof updatePipelineBar === 'function') updatePipelineBar();
}

function hydrateCoverPanel(idea) {
    if (!idea) return;
    const rec = (typeof getIdeaTaskRecord === 'function') ? getIdeaTaskRecord(idea.id, 'cover') : null;
    const loadingEl = document.getElementById('cover-img-loading');
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const makeBtn = document.getElementById('make-cover-btn');
    if (!loadingEl || !placeholderEl || !displayEl || !makeBtn) return;
    if (rec) {
        loadingEl.style.display = 'flex';
        placeholderEl.style.display = 'none';
        displayEl.style.display = 'none';
        makeBtn.disabled = true;
    } else {
        makeBtn.disabled = false;
        loadingEl.style.display = 'none';
    }
    if (typeof updatePipelineBar === 'function') updatePipelineBar();
}

// =====================================================================
// 帧/视频网格的拖拽操作
//   · 从桌面拖一个视频文件到视频卡片   → 上传覆盖该槽位（等同「上传」按钮）
//   · 从桌面拖一张图片到帧卡片         → 上传覆盖该帧
//   · 把一张视频卡片拖到另一张视频卡片 → 两个槽位的视频互换（目标空着＝搬运过去）
//     按住 Alt/⌥ 再放手 = 复制一份到目标槽位，源槽位保留
// 槽位编号与网格位置在任何情况下都固定不动，动的只是"哪段内容落在哪个槽位"。
// =====================================================================
const VIDEO_SLOT_DND_MIME = 'application/x-video-slot';
const FRAME_SLOT_DND_MIME = 'application/x-frame-slot';

// dragover 阶段拿不到 dataTransfer 的内容（只有 types 可读），因此拖的是什么
// 一律靠 types 判断：文件拖拽带 'Files'，卡片间换位带自定义 MIME。
function dndHasFiles(e) {
    const types = (e.dataTransfer && e.dataTransfer.types) || [];
    return Array.prototype.indexOf.call(types, 'Files') !== -1;
}

function dndHasVideoSlot(e) {
    const types = (e.dataTransfer && e.dataTransfer.types) || [];
    return Array.prototype.indexOf.call(types, VIDEO_SLOT_DND_MIME) !== -1;
}

function dndHasFrameSlot(e) {
    const types = (e.dataTransfer && e.dataTransfer.types) || [];
    return Array.prototype.indexOf.call(types, FRAME_SLOT_DND_MIME) !== -1;
}

// dragenter/dragleave 会随指针掠过卡片内部子元素反复触发，用进出计数配对，
// 否则拖到卡片里的按钮/视频上时高亮会不停闪断。
function bindDropZone(card, { accepts, hint, onDrop }) {
    if (!card) return;
    let depth = 0;
    const clear = () => {
        depth = 0;
        card.classList.remove('dnd-drop-target');
        delete card.dataset.dropHint;
    };
    card.addEventListener('dragenter', (e) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.stopPropagation();
        depth += 1;
        card.classList.add('dnd-drop-target');
        card.dataset.dropHint = hint(e);
    });
    card.addEventListener('dragover', (e) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer) {
            e.dataTransfer.dropEffect = (dndHasFiles(e) || e.altKey) ? 'copy' : 'move';
        }
        // Alt 键是在拖拽途中按下的，提示文案要跟着变
        card.dataset.dropHint = hint(e);
    });
    card.addEventListener('dragleave', (e) => {
        e.stopPropagation();
        depth -= 1;
        if (depth <= 0) clear();
    });
    card.addEventListener('drop', (e) => {
        if (!accepts(e)) { clear(); return; }
        e.preventDefault();
        e.stopPropagation();
        clear();
        onDrop(e);
    });
}

function enableVideoSlotDnd(card, slotNum, hasVideo) {
    if (!card || !Number.isFinite(Number(slotNum))) return;
    const slot = Number(slotNum);

    // 拖出：只有真的有视频的槽位才能当换位的源
    if (hasVideo) {
        card.draggable = true;
        card.addEventListener('dragstart', (e) => {
            if (!e.dataTransfer) return;
            e.dataTransfer.effectAllowed = 'copyMove';
            e.dataTransfer.setData(VIDEO_SLOT_DND_MIME, String(slot));
            // text/plain 兜底：个别浏览器对自定义 MIME 支持不全
            e.dataTransfer.setData('text/plain', `vid-slot:${slot}`);
            card.classList.add('dnd-dragging');
        });
        card.addEventListener('dragend', () => card.classList.remove('dnd-dragging'));
    }

    bindDropZone(card, {
        accepts: (e) => dndHasFiles(e) || dndHasVideoSlot(e),
        hint: (e) => {
            if (dndHasFiles(e)) return `上传到 VID ${String(slot).padStart(3, '0')}`;
            return e.altKey ? `复制到 VID ${String(slot).padStart(3, '0')}`
                            : `与 VID ${String(slot).padStart(3, '0')} 换位`;
        },
        onDrop: (e) => {
            const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (file) {
                if (!isVideoFileLike(file)) {
                    showToast(`「${file.name}」不是视频文件，视频槽位只接受视频`, 'error');
                    return;
                }
                uploadVideoToSlot(slot, file);
                return;
            }
            const raw = e.dataTransfer.getData(VIDEO_SLOT_DND_MIME)
                     || String(e.dataTransfer.getData('text/plain') || '').replace('vid-slot:', '');
            const from = parseInt(raw, 10);
            if (!Number.isFinite(from) || from === slot) return;
            swapVideoSlots(from, slot, e.altKey ? 'copy' : 'swap');
        }
    });
}

function enableFrameSlotDnd(card, seq, frame) {
    if (!card || !Number.isFinite(Number(seq))) return;
    const sequence = Number(seq);
    const imgUrl = frame && (frame.url || frame.file);

    // 拖出：有图的帧格既能拖到别的格子换位，也能直接拖到 Finder/桌面导出这张图
    // （DownloadURL 是 Chromium 系的扩展，其它浏览器忽略它、换位照常工作）。
    if (imgUrl) {
        card.draggable = true;
        card.addEventListener('dragstart', (e) => {
            if (!e.dataTransfer) return;
            e.dataTransfer.effectAllowed = 'copyMove';
            e.dataTransfer.setData(FRAME_SLOT_DND_MIME, String(sequence));
            e.dataTransfer.setData('text/plain', `img-slot:${sequence}`);
            try {
                const abs = new URL(imgUrl, window.location.href).href;
                e.dataTransfer.setData('DownloadURL',
                    `image/webp:img_${String(sequence).padStart(3, '0')}.webp:${abs}`);
            } catch (err) { /* 导出是附赠能力，拼不出绝对地址就算了 */ }
            card.classList.add('dnd-dragging');
        });
        card.addEventListener('dragend', () => card.classList.remove('dnd-dragging'));
    }

    bindDropZone(card, {
        accepts: (e) => dndHasFiles(e) || dndHasFrameSlot(e),
        hint: (e) => {
            const label = `IMG ${String(sequence).padStart(3, '0')}`;
            if (dndHasFiles(e)) return `上传到 ${label}（多张＝从这里往后依次填）`;
            return e.altKey ? `复制到 ${label}` : `与 ${label} 换位`;
        },
        onDrop: (e) => {
            const files = (e.dataTransfer && e.dataTransfer.files) || [];
            if (files.length) {
                const images = Array.prototype.filter.call(files, isImageFileLike);
                if (!images.length) {
                    showToast(`「${files[0].name}」不是图片文件，帧槽位只接受图片`, 'error');
                    return;
                }
                uploadFramesFromDrop(sequence, images);
                return;
            }
            const raw = e.dataTransfer.getData(FRAME_SLOT_DND_MIME)
                     || String(e.dataTransfer.getData('text/plain') || '').replace('img-slot:', '');
            const from = parseInt(raw, 10);
            if (!Number.isFinite(from) || from === sequence) return;
            swapFrameSlots(from, sequence, e.altKey ? 'copy' : 'swap');
        }
    });
}

// 文件类型判断都带扩展名兜底：从某些来源（压缩包解出、部分网盘客户端）拖出来的
// 文件 type 是空串，只按 MIME 判断会把合法文件挡在门外。
function isVideoFileLike(file) {
    if (!file) return false;
    if (file.type && file.type.startsWith('video/')) return true;
    return /\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(file.name || '');
}

function isImageFileLike(file) {
    if (!file) return false;
    if (file.type && file.type.startsWith('image/')) return true;
    return /\.(png|jpe?g|webp|bmp|gif)$/i.test(file.name || '');
}

// 把一张帧卡片置为「未生成/已失效」占位态。除了正常的缺帧渲染，也用于
// img 加载失败（onerror）——磁盘文件已被删除但本地清单还没同步时，别让
// 卡片继续挂着一张死图/浏览器缓存里的旧图。
// busy=true 时该创意的帧序列任务正在跑（同一浏览器会话有串行锁，同时只能
// 有一个渲染在飞）——按钮画成禁用态，而不是让用户点了才弹"已在生成中"的
// 错误提示（2026-07-21 用户实机复现：依次点生成按钮，除第一个外全部报错，
// 根因是这些按钮从未随任务状态被禁用过）。
function markFrameCardMissing(card, seq, busy) {
    card.className = 'frame-card video-failed-card';
    card.style.cursor = 'default';
    card.dataset.missing = '1'; // 已绑定的 lightbox 点击回调据此短路
    card.innerHTML = `
        <div class="video-failed-placeholder">
            <span class="error-icon">⚠️</span>
            <span class="error-text" style="font-size: 11px; color: var(--text-secondary);">未生成/已失效</span>
            <div style="display:flex; gap:4px;">
                <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}"${busy ? ' disabled' : ''}>生成</button>
                <button class="action-btn text-btn mini-btn secondary delete-slot-btn" data-seq="${seq}"${busy ? ' disabled' : ''}>删除</button>
            </div>
        </div>
        <span>IMG ${String(seq).padStart(3, '0')}</span>
    `;
    const delBtn = card.querySelector('.delete-slot-btn');
    delBtn.title = busy ? '该创意的帧序列正在生成/重试中，请稍候'
                        : '删除这一整拍：图片与视频提示词、文件一并删除，其后整体前移一位';
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteSlotBeat(seq);
    });
    const btn = card.querySelector('.retry-frame-btn');
    // 监听器必须无条件绑定——disabled 属性本身已经在 busy 时挡掉点击，
    // 若把绑定也塞进 `if (!busy)`，之后 setFrameGridButtonsBusy(false) 只是
    // 摘掉 disabled 属性，从未补绑监听器，按钮会变成"看着能点、点了没反应"的死按钮
    // （2026-07-22 用户实机复现：连续手动生成帧序列，第 2 帧起点击无响应）。
    btn.title = busy ? '该创意的帧序列正在生成/重试中，请稍候' : '';
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        retrySingleFrame(seq);
    });
}

function renderFramesForIdea(idea) {
    const grid = document.getElementById('frames-grid');
    const meta = document.getElementById('frames-meta');
    if (!grid || !meta) return;

    // 该创意的帧序列任务是否正在跑（串行锁，同时只能有一个渲染在飞）——
    // 用来把其余槽位的「生成/重试」按钮画成禁用态，见 markFrameCardMissing。
    const framesBusy = !!(idea && typeof isIdeaTaskActive === 'function' && isIdeaTaskActive(idea.id, 'frames'));

    const frameRun = idea && idea.frameRun;
    const frames = (frameRun && frameRun.frames) || [];
    grid.innerHTML = '';

    // If there are no frames, and no prompt_block, show empty
    if (!frames.length && (!idea || !idea.prompt_block)) {
        meta.textContent = '尚未生成任何帧序列。';
        return;
    }

    // Get expected image slots：优先消费后端结构化 prompt_slots，正则解析仅兜底
    const slots = resolvePromptSlots(idea);
    const imageSlots = slots.filter(s => s.type === 'image').sort((a, b) => a.index - b.index);

    if (imageSlots.length === 0) {
        if (!frames.length) {
            meta.textContent = '尚未生成任何帧序列。';
            return;
        }
    }

    const totalFramesCount = imageSlots.length || frames.length;
    const manifestText = (frameRun && frameRun.manifest) ? ` 清单: ${frameRun.manifest}` : '';
    const dirText = (frameRun && frameRun.project_dir) ? `，保存在 ${frameRun.project_dir || 'outputs'}.${manifestText}` : '';
    
    const generatedCount = frames.filter(f => f.url || f.file).length;
    meta.textContent = `已生成 ${generatedCount}/${totalFramesCount} 帧连续帧序列图${dirText}`;

    // Loop through the slots (or frames if slots is empty)
    const itemsToRender = imageSlots.length > 0
        ? imageSlots.map((slot) => {
            // 用槽位号本身做 sequence（后端保证 1..N 连续），不再用"数组下标+1"——
            // 一旦解析漏掉一个槽位，下标制会让后续所有帧整体错位配对（历史事故前提）。
            const seq = slot.index;
            const frame = frames.find(f => f.sequence === seq || f.slot === slot.index);
            return {
                sequence: seq,
                slot: slot.index,
                frame: frame
            };
          })
        : frames.map((f, idx) => ({
            sequence: f.sequence || (idx + 1),
            slot: f.slot || (idx + 1),
            frame: f
          }));

    itemsToRender.forEach(item => {
        const seq = item.sequence;
        const frame = item.frame;
        
        const card = document.createElement('div');
        card.id = `frame-slot-${seq}`;
        // 任何形态的帧卡片（已出图/等待中/未生成）都能接住拖进来的本地图片与别的帧格；
        // 监听绑在卡片元素本身，后续各分支只改写 innerHTML，不会把它冲掉。
        enableFrameSlotDnd(card, seq, frame);

        const hasImage = frame && (frame.url || frame.file);
        
        if (hasImage) {
            const isDegraded = frame.quality_gate === 'i2i_fallback_degraded';
            // 'vlm_qa_failed' 是旧逐帧质检门的终态，逐帧质检门已停用，仅为兼容展示旧 manifest 保留；
            // 'sequence_review_flagged' 是新的整套序列一致性审查修复轮次耗尽仍有问题的终态
            const isVlmFailed = frame.quality_gate === 'vlm_qa_failed' || frame.quality_gate === 'sequence_review_flagged';
            // 人工主动描述的问题（见 describeFrameIssue）：与机器判定分开存放在
            // manual_issue，可以在没跑过一致性审查的帧上单独成立，也可以和机器判定
            // 并存。两者任一成立都要给出「修复此帧问题」入口。
            const manualIssue = typeof frame.manual_issue === 'string' ? frame.manual_issue : '';
            const isManualFlagged = !!manualIssue;
            const isFixable = isVlmFailed || isManualFlagged;
            const isUnverified = frame.quality_gate === 'auto_approved_degraded';
            // 整套序列一致性审查两次（常规+降级）都没跑成时后端如实标 skipped，
            // 不再盖 sequence_reviewed_pass 假章——这里对应亮黄色"未审查"徽标
            const isReviewSkipped = frame.quality_gate === 'sequence_review_skipped';
            // stale_lineage 是后端唯一真正会写的血统过期标记（部分重生、手动上传帧
            // 都写它，见 update_manifest_stale_status / /api/upload_frame）；
            // quality_gate==='stale' / frame.stale 是更早的写法，留着兼容旧 manifest。
            const isStale = frame.stale_lineage || frame.quality_gate === 'stale' || frame.stale;
            // 宽松档软性瑕疵放行：quality_gate 仍是 auto_approved，告警留在 vlm_qa_reason
            const isWarned = frame.quality_gate === 'auto_approved' && typeof frame.vlm_qa_reason === 'string' && frame.vlm_qa_reason.indexOf('WARN') === 0;
            card.className = 'frame-card' + (isDegraded ? ' degraded-card' : '') + (isVlmFailed ? ' vlm-failed-card' : '') + (isManualFlagged ? ' manual-flagged-card' : '') + ((isUnverified || isReviewSkipped) ? ' degraded-card' : '') + (isStale ? ' stale-card' : '');
            card.style.cursor = 'pointer';

            let hoverTitle = `打开第 ${seq} 帧`;
            if (isDegraded) hoverTitle += ' (降级为文生图)';
            if (isVlmFailed) hoverTitle += ` (一致性审查未通过: ${frame.vlm_qa_reason || '跳变或无变化'})`;
            // 结构化违规（2026-07-25）：哪一层检出的、涉及哪几帧都留了下来，
            // 悬浮提示按条列出，比一根 '；' 拼接的字符串好读
            if (Array.isArray(frame.review_issues) && frame.review_issues.length) {
                hoverTitle += '\n' + frame.review_issues.map(i =>
                    `· [${i.layer === 'global' ? '跨帧' : (i.layer === 'manual' ? '人工' : '本拍')}] `
                    + `${i.text}（涉及 IMG ${(i.frames || []).map(s => String(s).padStart(3, '0')).join('/')}）`
                ).join('\n');
            }
            if (isManualFlagged) hoverTitle += ` (人工标记的问题: ${manualIssue})`;
            if (isUnverified) hoverTitle += ' (VLM 判定服务异常，此帧未经核验被放行)';
            if (isReviewSkipped) hoverTitle += ` (${frame.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查'})`;
            if (isWarned) hoverTitle += ` (宽松档放行: ${frame.vlm_qa_reason})`;
            if (isStale) hoverTitle += ' (过期：父帧已被重新生成，此帧与父帧血统不一致)';
            if (Number.isFinite(Number(frame.swapped_from_sequence))) {
                hoverTitle += ` (人工从 IMG ${String(frame.swapped_from_sequence).padStart(3, '0')} 拖过来)`;
            }
            if (frame.source === 'manual_upload') hoverTitle += ' (人工上传的本地图片)';
            card.title = hoverTitle;

            card.innerHTML = `
                <img src="" alt="Frame ${seq}" loading="lazy">
                ${isDegraded ? '<div class="degraded-badge">降级</div>' : ''}
                ${isManualFlagged
                    ? '<div class="manual-flagged-badge" title="' + (isVlmFailed ? manualIssue + '（一致性审查另判定：' + (frame.vlm_qa_reason || '') + '）' : manualIssue).replace(/"/g, '&quot;') + '">人工标记</div>'
                    : (isVlmFailed ? '<div class="vlm-failed-badge" title="' + (frame.vlm_qa_reason || '').replace(/"/g, '&quot;') + '">审查未过</div>' : '')}
                ${isUnverified ? '<div class="degraded-badge" title="' + (frame.vlm_qa_reason || 'VLM 判定服务异常，未经核验').replace(/"/g, '&quot;') + '">未核验</div>' : ''}
                ${isReviewSkipped ? '<div class="degraded-badge" title="' + (frame.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查').replace(/"/g, '&quot;') + '">未审查</div>' : ''}
                ${isWarned ? '<div class="degraded-badge" title="' + frame.vlm_qa_reason.replace(/"/g, '&quot;') + '">留痕</div>' : ''}
                ${isStale ? `<div class="stale-badge" ${isDegraded || isFixable || isUnverified || isWarned ? 'style="left: 45px;"' : ''} title="此帧派生自已被替换的旧帧，建议重新生成">Stale</div>` : ''}
                <div class="frame-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
                    ${isFixable ? `<button class="action-btn text-btn mini-btn fix-frame-btn" data-seq="${seq}" style="background: rgba(180,40,40,0.75); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${framesBusy ? ' disabled title="该创意的帧序列正在生成/重试中，请稍候"' : ' title="依据问题描述优化提示词后图生图重渲此帧"'}>修复此帧问题</button>` : ''}
                    <button class="action-btn text-btn mini-btn describe-frame-btn" data-seq="${seq}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${framesBusy ? ' disabled title="该创意的帧序列正在生成/重试中，请稍候"' : ' title="人工描述这一帧哪里不对，作为定向修复的依据"'}>${isManualFlagged ? '改描述' : '描述问题'}</button>
                    <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${framesBusy ? ' disabled title="该创意的帧序列正在生成/重试中，请稍候"' : ''}>重试</button>
                    <button class="action-btn text-btn mini-btn delete-slot-btn" data-seq="${seq}" style="background: rgba(150,30,30,0.75); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${framesBusy ? ' disabled title="该创意的帧序列正在生成/重试中，请稍候"' : ' title="删除这一整拍：图片与视频提示词、文件一并删除，其后整体前移一位"'}>删除</button>
                </div>
                <span>IMG ${String(seq).padStart(3, '0')}</span>
            `;

            const frameImgEl = card.querySelector('img');
            // 图自身可拖会抢走卡片的拖拽源身份（浏览器默认拖的是这张图，带不上换位
            // 需要的槽位号）——关掉它，改由卡片统一发起拖拽（导出到 Finder 的能力
            // 由 dragstart 里的 DownloadURL 补上）。
            frameImgEl.draggable = false;
            frameImgEl.onerror = () => markFrameCardMissing(card, seq, framesBusy);
            safeSetImageSrc(frameImgEl, frame.url || frame.file);
            
            // Hover effect to show action buttons
            card.addEventListener('mouseenter', () => {
                const actions = card.querySelector('.frame-card-actions');
                if (actions) actions.style.opacity = '1';
            });
            card.addEventListener('mouseleave', () => {
                const actions = card.querySelector('.frame-card-actions');
                if (actions) actions.style.opacity = '0';
            });
            
            // Click on the card opens lightbox (excluding the retry/fix buttons)
            card.addEventListener('click', (e) => {
                if (e.target.classList.contains('retry-frame-btn')) return;
                if (e.target.classList.contains('fix-frame-btn')) return;
                if (e.target.classList.contains('describe-frame-btn')) return;
                if (e.target.classList.contains('delete-slot-btn')) return;
                if (card.dataset.missing) return; // 图已失效被降级成占位卡，别开死图 lightbox
                
                // Get all valid frames for the lightbox
                const validFrames = itemsToRender
                    .filter(i => i.frame && (i.frame.url || i.frame.file))
                    .map(i => i.frame);
                
                const mediaList = validFrames.map((f) => ({
                    type: 'image',
                    url: f.url || f.file,
                    caption: `<strong>第 ${f.sequence} 帧 / 共 ${validFrames.length} 帧</strong>`
                }));
                
                const clickedIndex = validFrames.findIndex(f => f.sequence === seq);
                openLightbox(mediaList, clickedIndex >= 0 ? clickedIndex : 0);
            });
            
            card.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleFrame(seq);
            });
            const fixBtn = card.querySelector('.fix-frame-btn');
            if (fixBtn) {
                fixBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    fixFrameIssue(seq);
                });
            }
            card.querySelector('.describe-frame-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                describeFrameIssue(seq, manualIssue);
            });
            card.querySelector('.delete-slot-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                deleteSlotBeat(seq);
            });
        } else if ((() => {
            // 只有这个槽位真的在当前后台任务的目标范围内，才画"等待中"——
            // 单帧重试的任务记录会带 targetSequences（如 [3]），此时其余槽位
            // 服务端压根没碰，画"等待中"会让人误以为整套序列正在自动续渲
            // （2026-07-20 用户实机截图复现：重试 IMG 003 时 004-012 全部显示
            // 等待中，但 server.log 证实那次任务子集只有 [3]，其余槽位从未
            // 被请求过）。targetSequences 为空/未设置＝整单任务，范围覆盖全部。
            const rec = (typeof getIdeaTaskRecord === 'function' && idea) ? getIdeaTaskRecord(idea.id, 'frames') : null;
            return rec && (!rec.targetSequences || rec.targetSequences.includes(seq));
        })()) {
            // 该创意的帧序列任务正在后台跑，这个槽位只是还没轮到——画等待中占位而不是"已失效"重试卡
            card.className = 'frame-card placeholder-frame-card';
            card.innerHTML = `
                <div class="frame-placeholder-spinner">
                    <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                </div>
                <span>第 ${String(seq).padStart(3, '0')} 帧 (等待中)</span>
            `;
        } else {
            // Missing or failed frame
            markFrameCardMissing(card, seq, framesBusy);
        }

        grid.appendChild(card);
    });
}

// 视频卡片上的「删除」按钮：删的是这一整拍（图片 N + 视频 N 的提示词与文件），
// 不是只删这一段视频——槽位号是契约，视频 N 恒等于 IMG N → IMG N+1，单删视频会让
// 提示词条数与格子数对不上（见 api_client.deleteSlotBeat / server /api/delete_slot）。
// 同各处按钮的教训：监听器无条件绑定，busy 只影响 disabled/title。
function bindDeleteSlotButton(card, slotNum, busy) {
    const btn = card.querySelector('.delete-slot-btn');
    if (!btn) return;
    btn.title = busy ? '该创意的视频序列正在生成/重试中，请稍候'
                     : '删除这一整拍：图片与视频提示词、文件一并删除，其后整体前移一位';
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteSlotBeat(slotNum);
    });
}

// 视频槽位从未生成过（不同于"生成失败"——没有 error 原因可展示）：给「生成」
// 「上传」两个出口，同 markFrameCardMissing 一样监听器无条件绑定，busy 只影响
// disabled/title，否则 setVideoGridButtonsBusy(false) 之后按钮会变成看着能点、
// 点了没反应的死按钮。
function markVideoCardMissing(card, slotNum, busy) {
    card.className = 'frame-card video-failed-card';
    card.style.cursor = 'default';
    card.innerHTML = `
        <div class="video-failed-placeholder">
            <span class="error-icon">⚠️</span>
            <span class="error-text" style="font-size: 11px; color: var(--text-secondary);">未生成</span>
            <div style="display:flex; gap:4px; flex-wrap:wrap; justify-content:center;">
                <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${slotNum}"${busy ? ' disabled' : ''}>生成</button>
                <button class="action-btn text-btn mini-btn secondary upload-video-btn" data-slot="${slotNum}"${busy ? ' disabled' : ''}>上传</button>
                <button class="action-btn text-btn mini-btn secondary delete-slot-btn" data-slot="${slotNum}"${busy ? ' disabled' : ''}>删除</button>
            </div>
        </div>
        <span>VID ${String(slotNum).padStart(3, '0')}</span>
    `;
    bindDeleteSlotButton(card, slotNum, busy);
    const genBtn = card.querySelector('.retry-video-btn');
    const uploadBtn = card.querySelector('.upload-video-btn');
    genBtn.title = busy ? '该创意的视频序列正在生成/重试中，请稍候' : '';
    genBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        retrySingleVideo(slotNum);
    });
    uploadBtn.title = busy ? '该创意的视频序列正在生成/重试中，请稍候' : '手动上传本地视频文件覆盖此槽位';
    uploadBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        triggerVideoUpload(slotNum);
    });
}

function renderVideosForIdea(idea) {
    const grid = document.getElementById('videos-grid');
    const meta = document.getElementById('videos-meta');
    if (!grid || !meta) return;

    const frameRun = idea && idea.frameRun;
    const videos = (frameRun && frameRun.videos) || [];
    grid.innerHTML = '';

    const mergedContainer = document.getElementById('merged-video-container');
    const mergedPlayer = document.getElementById('merged-video-player');
    const mergedInfo = document.getElementById('merged-video-info');
    const mergedDownload = document.getElementById('merged-video-download');

    if (mergedContainer) {
        if (frameRun && frameRun.merged_video && frameRun.merged_video.status === 'success') {
            const mv = frameRun.merged_video;
            mergedContainer.style.display = 'block';
            if (mergedPlayer) {
                // 重新合成会原地覆盖同一个成片文件，同样要带版本号才看得到新的
                mergedPlayer.src = cacheBustedUrl(mv.url);
            }
            if (mergedDownload) {
                mergedDownload.href = mv.url;
                mergedDownload.download = `${idea.title || 'video'}_merged_2x.mp4`;
            }
            if (mergedInfo) {
                const sizeMB = mv.size_bytes ? (mv.size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '未知大小';
                const durationSec = mv.duration_seconds ? mv.duration_seconds + ' 秒' : '未知时长';
                mergedInfo.textContent = `文件大小: ${sizeMB} | 视频时长: ${durationSec}`;
            }
        } else {
            mergedContainer.style.display = 'none';
            if (mergedPlayer) {
                mergedPlayer.removeAttribute('src');
                mergedPlayer.load();
            }
        }
    }

    // If there are no videos, and no prompt_block, show empty
    if (!videos.length && (!idea || !idea.prompt_block)) {
        meta.textContent = '尚未生成任何视频序列。';
        return;
    }

    // 预期视频槽位数以提示词里「视频 N:」的条数为准（resolvePromptSlots，与
    // renderFramesForIdea 同一套契约），不是"目前已经跑过/成功了几段"——否则调试
    // 限量生成/中途取消/批量重试遗漏的槽位不会画出来，卡片数和视频提示词条数对
    // 不上（用户实际反馈：提示词有几条，视频片段槽位就该有多少个）。
    const slots = resolvePromptSlots(idea);
    const videoSlots = slots.filter(s => s.type === 'video').sort((a, b) => a.index - b.index);

    if (videoSlots.length === 0) {
        if (!videos.length) {
            meta.textContent = '尚未生成任何视频序列。';
            return;
        }
    }

    const totalVideosCount = videoSlots.length || videos.length;
    const generatedCount = videos.filter(v => v.url || v.file).length;
    const manifestText = (frameRun && frameRun.manifest) ? ` 清单: ${frameRun.manifest}` : '';
    const dirText = (frameRun && frameRun.project_dir) ? `，保存在 ${frameRun.project_dir || 'outputs'}.${manifestText}` : '';
    meta.textContent = `已生成 ${generatedCount}/${totalVideosCount} 段连续视频${dirText}`;

    // 该创意的视频序列任务是否正在跑——用来把其余槽位的「重试」按钮画成禁用态。
    const videosBusy = !!(idea && typeof isIdeaTaskActive === 'function' && isIdeaTaskActive(idea.id, 'videos'));

    // Loop through the slots (or videos if slots is empty，兼容没有 prompt_slots 的旧数据)
    const itemsToRender = videoSlots.length > 0
        ? videoSlots.map((slot) => ({
            slotNum: slot.index,
            video: videos.find(v => v.slot === slot.index)
          }))
        : videos.map((v) => ({ slotNum: v.slot, video: v }));

    itemsToRender.forEach(item => {
        const slotNum = item.slotNum;
        const video = item.video;
        const card = document.createElement('div');
        card.id = `video-slot-${slotNum}`;
        // 拖拽：卡片本身既是放置区（接文件上传 / 接别的槽位换位过来），
        // 有视频时也是拖拽源。同帧卡片，监听绑在卡片元素上，不随 innerHTML 重写丢失。
        enableVideoSlotDnd(card, slotNum, !!(video && (video.url || video.file)));

        if (!video) {
            // 该槽位从未处理过：若正处在当前后台任务的目标范围内（整单任务，或
            // target_slots 显式包含它），画"等待中"；否则说明这次任务压根没碰它，
            // 画"未生成"+生成/上传出口。同 renderFramesForIdea 的同款判断。
            const rec = (typeof getIdeaTaskRecord === 'function' && idea) ? getIdeaTaskRecord(idea.id, 'videos') : null;
            const isPending = rec && (!rec.targetSlots || rec.targetSlots.includes(slotNum));
            if (isPending) {
                card.className = 'frame-card placeholder-frame-card';
                card.innerHTML = `
                    <div class="frame-placeholder-spinner">
                        <div class="cover-spinner" style="width:20px; height:20px; margin-bottom:0;"></div>
                    </div>
                    <span>第 ${String(slotNum).padStart(3, '0')} 段视频 (等待中)</span>
                `;
            } else {
                markVideoCardMissing(card, slotNum, videosBusy);
            }
            grid.appendChild(card);
            return;
        }

        const isFailed = video.status === 'failed' || (!video.url && !video.file);
        // 英雄展示视频（默认收尾步骤）：只上传首帧完工图，没有"下一张"图可以指向——
        // 标签用专属文案，不套用其余槽位的 IMG N ➔ IMG N+1 箭头写法。
        const isHero = !!(video.is_hero || (video.meta && String(video.meta).toUpperCase().includes('HERO')));

        const startImg = String(video.slot).padStart(3, '0');
        const endImg = String(video.slot + 1).padStart(3, '0');
        const labelText = isHero
            ? `VID ${String(video.slot).padStart(3, '0')} (英雄展示 · 完工全景)`
            : `VID ${String(video.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})`;

        if (isFailed) {
            card.className = 'frame-card video-failed-card';
            card.style.cursor = 'default';
            card.innerHTML = `
                <div class="video-failed-placeholder">
                    <span class="error-icon">⚠️</span>
                    <span class="error-text" title="${video.error || '生成失败'}">生成失败</span>
                    <div style="display:flex; gap:4px; flex-wrap:wrap; justify-content:center;">
                        <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${video.slot}"${videosBusy ? ' disabled' : ''}>重试</button>
                        <button class="action-btn text-btn mini-btn secondary upload-video-btn" data-slot="${video.slot}"${videosBusy ? ' disabled' : ''}>上传</button>
                        <button class="action-btn text-btn mini-btn secondary delete-slot-btn" data-slot="${video.slot}"${videosBusy ? ' disabled' : ''}>删除</button>
                    </div>
                </div>
                <span>${labelText}</span>
            `;
            bindDeleteSlotButton(card, video.slot, videosBusy);
            const retryBtn = card.querySelector('.retry-video-btn');
            const uploadBtn = card.querySelector('.upload-video-btn');
            // 监听器无条件绑定，同 markFrameCardMissing 的教训——busy 只应影响
            // disabled 属性/title，不能连带跳过 addEventListener。
            retryBtn.title = videosBusy ? '该创意的视频序列正在生成/重试中，请稍候' : '';
            retryBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleVideo(video.slot);
            });
            uploadBtn.title = videosBusy ? '该创意的视频序列正在生成/重试中，请稍候' : '手动上传本地视频文件覆盖此槽位';
            uploadBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                triggerVideoUpload(video.slot);
            });
        } else {
            const isManualUpload = video.source === 'manual_upload';
            // 手动换位/复制过来的片段：首尾帧未重新校验，徽标上标出它原本在哪个槽位，
            // 免得事后看着一格格视频想不起来自己调过顺序。
            const swappedFrom = Number.isFinite(Number(video.swapped_from_slot))
                ? Number(video.swapped_from_slot) : null;
            card.className = 'frame-card';
            card.style.cursor = 'pointer';
            card.innerHTML = `
                <div class="video-preview-wrapper" style="position: relative; width: 100%; aspect-ratio: 9/16; border-radius: 5px; overflow: hidden; background: #03050c;">
                    <video src="${cacheBustedUrl(video.url)}" loop muted playsinline style="width:100%; height:100%; object-fit: cover; display: block;"></video>
                    <div class="video-play-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.25); transition: all 0.2s ease;">
                        <span class="play-icon" style="font-size: 2rem; color: #fff; opacity: 0.85; transition: all 0.2s ease;">▶</span>
                    </div>
                    ${isManualUpload ? '<div class="degraded-badge" title="此片段由用户手动上传覆盖，非本地 UI 自动化产出">手动</div>' : ''}
                    ${swappedFrom !== null ? `<div class="degraded-badge" title="此片段由人工从 VID ${String(swappedFrom).padStart(3, '0')} 拖过来，首尾帧未按本槽位锚点重新校验">换位</div>` : ''}
                    <div class="video-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
                        <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${video.slot}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${videosBusy ? ' disabled' : ''}>重试</button>
                        <button class="action-btn text-btn mini-btn upload-video-btn" data-slot="${video.slot}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${videosBusy ? ' disabled' : ''}>上传</button>
                        <button class="action-btn text-btn mini-btn delete-slot-btn" data-slot="${video.slot}" style="background: rgba(150,30,30,0.75); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;"${videosBusy ? ' disabled' : ''}>删除</button>
                    </div>
                </div>
                <span>${labelText}</span>
            `;
            
            const videoEl = card.querySelector('video');
            const playOverlay = card.querySelector('.video-play-overlay');
            const playIcon = card.querySelector('.play-icon');
            const cardActions = card.querySelector('.video-card-actions');

            card.addEventListener('mouseenter', () => {
                videoEl.play().catch(() => {});
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0)';
                if (playIcon) playIcon.style.opacity = '0';
                if (cardActions) cardActions.style.opacity = '1';
            });
            card.addEventListener('mouseleave', () => {
                videoEl.pause();
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0.25)';
                if (playIcon) playIcon.style.opacity = '0.85';
                if (cardActions) cardActions.style.opacity = '0';
            });

            const successRetryBtn = card.querySelector('.retry-video-btn');
            successRetryBtn.title = videosBusy ? '该创意的视频序列正在生成/重试中，请稍候' : '重新生成此槽位视频（覆盖当前片段）';
            successRetryBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleVideo(video.slot);
            });
            const successUploadBtn = card.querySelector('.upload-video-btn');
            successUploadBtn.title = videosBusy ? '该创意的视频序列正在生成/重试中，请稍候' : '手动上传本地视频文件覆盖此槽位';
            successUploadBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                triggerVideoUpload(video.slot);
            });
            bindDeleteSlotButton(card, video.slot, videosBusy);

            card.addEventListener('click', (e) => {
                if (e.target.classList.contains('retry-video-btn')
                    || e.target.classList.contains('upload-video-btn')
                    || e.target.classList.contains('delete-slot-btn')) return;
                const validVideos = videos.filter(v => v.url || v.file);
                const mediaList = validVideos.map((v, idx) => {
                    const vIsHero = !!(v.is_hero || (v.meta && String(v.meta).toUpperCase().includes('HERO')));
                    const startImg = String(v.slot).padStart(3, '0');
                    const endImg = String(v.slot + 1).padStart(3, '0');
                    const cap = vIsHero
                        ? `VID ${String(v.slot).padStart(3, '0')} (英雄展示 · 完工全景)`
                        : `VID ${String(v.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})`;
                    return {
                        type: 'video',
                        // 换位/覆盖过的片段路径没变、内容变了，lightbox 也得回源，
                        // 否则大图里放的还是缓存里的旧片
                        url: cacheBustedUrl(v.url || v.file),
                        caption: `<strong>${cap}</strong>`
                    };
                });
                const clickedIndex = validVideos.indexOf(video);
                openLightbox(mediaList, clickedIndex);
            });
        }
        grid.appendChild(card);
    });
}

function renderCoversForIdea(idea, activeIndex = 0) {
    const placeholderEl = document.getElementById('cover-image-placeholder');
    const displayEl = document.getElementById('cover-img-display');
    const historyContainer = document.getElementById('cover-history-container');
    const thumbnailsEl = document.getElementById('cover-history-thumbnails');
    const hookDisplay = document.getElementById('cover-hook-display');
    const hookVal = document.getElementById('cover-hook-val');
    
    // Render English hook text if it exists
    if (hookDisplay && hookVal) {
        if (idea.english_title) {
            hookDisplay.style.display = 'flex';
            hookVal.textContent = idea.english_title;
        } else {
            hookDisplay.style.display = 'none';
            hookVal.textContent = '';
        }
    }
    
    const covers = idea.covers || [];
    
    if (covers.length === 0) {
        placeholderEl.style.display = 'flex';
        displayEl.style.display = 'none';
        historyContainer.style.display = 'none';
        return;
    }
    
    // Bound activeIndex
    if (activeIndex < 0 || activeIndex >= covers.length) {
        activeIndex = covers.length - 1;
    }
    
    // Set up displayEl load/error handlers before setting src
    displayEl.onload = () => {
        displayEl.style.display = 'block';
        placeholderEl.style.display = 'none';
    };
    displayEl.onerror = () => {
        displayEl.style.display = 'none';
        placeholderEl.style.display = 'flex';
    };

    // 图片 1 图生图时只把当前封面作为视觉参考；文本提示词仍取“图片 1”。
    idea.activeCoverUrl = covers[activeIndex];
    
    // Update main image display
    safeSetImageSrc(displayEl, covers[activeIndex]);
    
    // Set up click on main image to open in lightbox on the current page
    displayEl.onclick = () => {
        const mediaList = covers.map((c, idx) => ({
            type: 'image',
            url: c,
            caption: `<strong>${idea.title} - 封面 ${idx + 1}/${covers.length}</strong>`
        }));
        openLightbox(mediaList, activeIndex);
    };
    
    // Update history thumbnails
    historyContainer.style.display = 'flex';
    thumbnailsEl.innerHTML = '';
    
    covers.forEach((coverUrl, idx) => {
        const thumb = document.createElement('div');
        thumb.className = `cover-thumb ${idx === activeIndex ? 'active' : ''}`;
        thumb.innerHTML = `<img src="" alt="Thumbnail ${idx + 1}" loading="lazy">`;
        
        const img = thumb.querySelector('img');
        img.onerror = () => {
            thumb.remove();
            // If all thumbnails are removed/hidden, hide the history container
            if (thumbnailsEl.children.length === 0) {
                historyContainer.style.display = 'none';
            }
        };
        
        safeSetImageSrc(img, coverUrl);
        
        thumb.addEventListener('click', () => {
            renderCoversForIdea(idea, idx);
        });
        
        thumbnailsEl.appendChild(thumb);
    });
}

function extractImageUrl(content) {
    if (!content) return null;
    
    // 1. Check if it's a markdown image: ![alt](url)
    const markdownRegex = /!\[.*?\]\((.*?)\)/;
    const match = content.match(markdownRegex);
    if (match && match[1]) {
        return match[1].trim();
    }
    
    // 2. Check if it's a raw URL starting with http, https, data:, or a local path starting with / or outputs/
    const urlRegex = /(https?:\/\/[^\s\)]+|data:image\/[^\s\)]+|\/outputs\/[^\s\)]+|outputs\/[^\s\)]+)/;
    const urlMatch = content.match(urlRegex);
    if (urlMatch && urlMatch[1]) {
        return urlMatch[1].trim();
    }
    
    // 3. Fallback: if it's just the content itself, trim and return if it starts with valid protocols or paths
    const trimmed = content.trim();
    if (trimmed.startsWith('http://') || 
        trimmed.startsWith('https://') || 
        trimmed.startsWith('data:image/') ||
        trimmed.startsWith('/') ||
        trimmed.startsWith('outputs/')) {
        return trimmed;
    }
    
    return null;
}

// --- LIGHTBOX 通用控制器已抽出到 js/lightbox.js(双前端共享的全局函数)---
// 本文件其余处的 openLightbox(mediaList, idx) 调用点保持不变,直接调用该共享模块的全局函数。

