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
    const flagged = count(f => f.quality_gate === 'sequence_review_flagged'
        || f.quality_gate === 'vlm_qa_failed'
        || f.quality_gate === 'frame_continuity_failed');
    // 人工主动描述、但还没点「修复此帧问题」的帧：人已经看出这里不对，合成前更
    // 该提醒一次（见 describeFrameIssue）
    const manualFlagged = count(f => typeof f.manual_issue === 'string' && f.manual_issue.trim());
    const skipped = count(f => f.quality_gate === 'sequence_review_skipped');
    // 从没审过的帧（渲染完就没跑过一致性审查，或审查结论因帧被重渲而作废）：
    // 合成前提醒一次，避免把"没查过"读成"查过没问题"
    const neverReviewed = count(f => f.quality_gate === 'pending_manual_review');
    const degraded = count(f => f.quality_gate === 'i2i_fallback_degraded' || f.quality_gate === 'auto_approved_degraded');
    // 降档通道产出：manifest 里一直有 degraded_reason，但此前没有任何一处读它——
    // 合成前必须知道这一单混进过分辨率更低的帧（见 frame_generator.chat_transport_note）
    const downscaled = count(f => !!f.degraded_reason);
    const stale = count(f => f.stale_lineage);
    const inertia = count(f => typeof f.vlm_qa_reason === 'string' && f.vlm_qa_reason.indexOf('anchor_inertia') !== -1);
    if (flagged) risks.push(`${flagged} 帧一致性审查未过`);
    if (manualFlagged) risks.push(`${manualFlagged} 帧被人工标记问题、尚未修复`);
    if (skipped) risks.push(`${skipped} 帧未经整套审查（审查服务不可用）`);
    if (neverReviewed) risks.push(`${neverReviewed} 帧尚未跑过一致性审查`);
    if (degraded) risks.push(`${degraded} 帧降级/未核验`);
    if (downscaled) risks.push(`${downscaled} 帧走降档通道渲出（分辨率低于本单档位，建议补额度后定向重渲）`);
    if (stale) risks.push(`${stale} 帧血统过期`);
    if (inertia) risks.push(`${inertia} 帧疑似换族惯性卡死`);
    const videos = manifest.videos || [];
    const vFailed = videos.filter(v => v.status === 'failed').length;
    const vWarned = videos.filter(v => v.process_warned).length;
    if (vFailed) risks.push(`${vFailed} 段视频失败/被门禁拦截`);
    if (vWarned) risks.push(`${vWarned} 段视频过程检测告警（冻结/空心等，宽松档放行）`);
    // 运行时能力劣化（numpy/ffmpeg/技能契约缺失，见 server_common.stamp_manifest_capabilities）：
    // 这一类最危险的地方在于"看起来什么都没发生"——探针静默跳过，日志上一片安静，
    // 而整套内容级校验（防串片、帧对契约、冻结检测）其实根本没跑。必须原文报出来。
    const stamps = manifest.capability_degraded || {};
    const stageLabel = { frames: '帧阶段', videos: '视频阶段' };
    Object.keys(stamps).forEach(stage => {
        const issues = (stamps[stage] && stamps[stage].issues) || [];
        issues.forEach(text => risks.push(`${stageLabel[stage] || stage}能力劣化：${text}`));
    });
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

    const deliveryWarning = result.degraded === true
        || (result.quality_gate && result.quality_gate.status !== 'passed')
        ? `提示词质量门未通过：${result.failure_code || '结果已降级'}。帧序列渲染已禁用。`
        : '';
    renderRepairBanner(deliveryWarning || result.repair_md);
    // 换单/重渲时必须先退出手动编辑态：编辑器里躺着的是上一单的提示词，
    // 留在屏幕上一保存就写串了单（见 js/prompt_editor.js）。
    if (typeof resetPromptEditor === 'function') resetPromptEditor();
    if (typeof renderPromptDisplay === 'function') {
        renderPromptDisplay(result.prompt_block || '（本次未返回提示词内容）');
    } else {
        const blockEl = document.getElementById('idea-prompt-block');
        if (blockEl) blockEl.textContent = result.prompt_block || '（本次未返回提示词内容）';
    }
    if (result.id && result.prompt_block && typeof recordPromptHistory === 'function') {
        recordPromptHistory(result.id, result.prompt_block, '初始激发生成');
    }
    document.getElementById('idea-audit').innerHTML = renderAuditMarkdown(result.audit_md);

    // 提示词槽位卡片已移除：本页仅展示原始 Markdown 提示词块（#idea-prompt-block，见上）。
    // 注意 parsePromptBlock 仍被帧序列渲染用于推算图片槽位，切勿删除其定义。

    // 质量审核状态条（原独立 tab，现为概览页顶部一行）：默认收起，不占地方。
    // 有修复建议时提示文字切换为"有修复建议"并高亮，但仍保持默认收起状态，用户点击可自由展开。
    const auditDetails = document.getElementById('audit-details');
    const hasRepairs = result.repair_md && result.repair_md.trim() &&
                        !/^PASS/i.test(result.repair_md.trim()) &&
                        !result.repair_md.includes('未发现违规');
    if (auditDetails) {
        auditDetails.open = false;
        if (hasRepairs) {
            auditDetails.classList.add('warning-highlight');
        } else {
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
                if (typeof openCollageViewer === 'function') {
                    openCollageViewer({ collageUrl: result.collage_url, idea: result });
                } else {
                    openLightbox([{
                        type: 'image',
                        url: result.collage_url,
                        caption: '<strong>视频关键帧多宫格拼图 (Keyframe Collage)</strong>'
                    }], 0);
                }
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
            // 两条分支写进库里的都是"服务端此刻的真实状态"（幽灵清理是删光，
            // 刷新是按 manifest 原件覆盖），帧记录变少都属于有意为之。走单条写
            // （persistIdeaItem）之后不再经过整表缩量闸门，无需声明意图。
            if (manifest === null) {
                if (result.frameRun) {
                    delete result.frameRun;
                    saveCurrentIdeaState();
                    const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
                    if (existingIdx !== -1 && savedIdeas[existingIdx].frameRun) {
                        delete savedIdeas[existingIdx].frameRun;
                        persistIdeaItem(savedIdeas[existingIdx]);
                    }
                }
            } else {
                result.frameRun = manifest;
                saveCurrentIdeaState();
                const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
                if (existingIdx !== -1) {
                    savedIdeas[existingIdx].frameRun = manifest;
                    persistIdeaItem(savedIdeas[existingIdx]);
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
    const selBtn = document.getElementById('generate-frames-selection-btn');
    const progress = document.getElementById('frames-progress');
    const meta = document.getElementById('frames-meta');
    renderFramesForIdea(idea);
    if (rec) {
        if (btn) btn.disabled = true;
        if (selBtn) selBtn.disabled = true;
        if (progress) progress.style.display = 'flex';
        if (meta) meta.textContent = rec.meta || '生成中...';
        if (rec.progressInfo && typeof setProgressBar === 'function') setProgressBar('frames', rec.progressInfo);
        if (typeof framesFeedHydrate === 'function') framesFeedHydrate(idea.id);
    } else {
        const deliveryBlocked = idea.degraded === true
            || (idea.quality_gate && idea.quality_gate.status !== 'passed');
        if (btn) {
            btn.disabled = deliveryBlocked;
            btn.title = deliveryBlocked ? '提示词处于降级或质量门未通过状态，不能生成帧序列' : '';
        }
        if (selBtn) {
            selBtn.disabled = deliveryBlocked;
            selBtn.title = deliveryBlocked ? '提示词处于降级或质量门未通过状态，不能生成帧序列' : '';
        }
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
    const grid = slotRenderTarget('video');
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
                    placeholderCard.id = `video-slot-${i}`;
                    enableVideoSlotDnd(placeholderCard, i);
                    renderSlotCard(placeholderCard, slotPendingState('video', i, '等待中'));
                    placeSlotCard(placeholderCard, 'video', i);
                    grid.appendChild(placeholderCard);
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

// 拖拽监听绑在卡片元素本身、只绑一次，之后 renderSlotCard 反复重写 innerHTML
// 都不会冲掉它。「这一格现在有没有内容」不再是绑定时刻的入参（旧实现按当时
// 有没有视频决定 draggable，重渲后就失灵了），改为每次渲染由 renderSlotCard
// 写进 card.draggable / card.dataset.url，拖拽时现读。
function enableVideoSlotDnd(card, slotNum) {
    if (!card || !Number.isFinite(Number(slotNum))) return;
    if (card.dataset.dndBound === '1') return;
    card.dataset.dndBound = '1';
    const slot = Number(slotNum);

    card.addEventListener('dragstart', (e) => {
        if (!e.dataTransfer || !card.dataset.url) return;
        e.dataTransfer.effectAllowed = 'copyMove';
        e.dataTransfer.setData(VIDEO_SLOT_DND_MIME, String(slot));
        // text/plain 兜底：个别浏览器对自定义 MIME 支持不全
        e.dataTransfer.setData('text/plain', `vid-slot:${slot}`);
        card.classList.add('dnd-dragging');
    });
    card.addEventListener('dragend', () => card.classList.remove('dnd-dragging'));

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

function enableFrameSlotDnd(card, seq) {
    if (!card || !Number.isFinite(Number(seq))) return;
    if (card.dataset.dndBound === '1') return;
    card.dataset.dndBound = '1';
    const sequence = Number(seq);

    // 拖出：有图的帧格既能拖到别的格子换位，也能直接拖到 Finder/桌面导出这张图
    // （DownloadURL 是 Chromium 系的扩展，其它浏览器忽略它、换位照常工作）。
    card.addEventListener('dragstart', (e) => {
        const imgUrl = card.dataset.url;
        if (!e.dataTransfer || !imgUrl) return;
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

function renderFramesForIdea(idea) {
    // 容器由 slotRenderTarget 决定：合并视图（一拍一列）下两类卡片共用
    // #beats-grid，拆分视图下各回各的网格。见 js/slot_toolbar.js。
    const grid = slotRenderTarget('image');
    const meta = document.getElementById('frames-meta');
    if (!grid || !meta) return;

    // 该创意的帧序列任务是否正在跑（串行锁，同时只能有一个渲染在飞）——
    // 用来把其余槽位的「生成/重试」按钮画成禁用态，见 markFrameCardMissing。
    const framesBusy = !!(idea && typeof isIdeaTaskActive === 'function' && isIdeaTaskActive(idea.id, 'frames'));

    const frameRun = idea && idea.frameRun;
    const frames = (frameRun && frameRun.frames) || [];
    clearSlotGrid(grid, 'image');

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

    // 该创意的帧序列任务正在跑时，哪些槽位算"还没轮到"：单帧重试的任务记录带
    // targetSequences（如 [3]），此时其余槽位服务端压根没碰，画"等待中"会让人
    // 误以为整套序列正在自动续渲（2026-07-20 实机截图复现：重试 IMG 003 时
    // 004-012 全部显示等待中，但 server.log 证实那次任务子集只有 [3]）。
    // targetSequences 为空/未设置＝整单任务，范围覆盖全部。
    const framesRec = (typeof getIdeaTaskRecord === 'function' && idea)
        ? getIdeaTaskRecord(idea.id, 'frames') : null;
    const isFramePending = (seq) =>
        !!(framesRec && (!framesRec.targetSequences || framesRec.targetSequences.includes(seq)));

    itemsToRender.forEach(item => {
        const seq = item.sequence;
        const card = document.createElement('div');
        card.id = `frame-slot-${seq}`;
        // 任何形态的帧卡片（已出图/等待中/未生成）都能接住拖进来的本地图片与别的
        // 帧格；监听绑在卡片元素本身，renderSlotCard 重写 innerHTML 不会冲掉它。
        enableFrameSlotDnd(card, seq);
        renderSlotCard(card, frameSlotState(item.frame, {
            seq,
            busy: framesBusy,
            pending: isFramePending(seq),
        }));
        placeSlotCard(card, 'image', seq);
        grid.appendChild(card);
    });

    // 整格重渲会换掉所有卡片：让工具条复原选中态、重新套用筛选、刷新计数
    if (typeof syncSlotToolbar === 'function') syncSlotToolbar('image');
    // 审查结论面板与卡片同源、同一次重渲刷新：审查跑完/修完一帧后各处都会
    // reloadManifestIntoIdea + renderFramesForIdea，面板因此不需要自己的刷新时机
    if (typeof renderReviewPanel === 'function') renderReviewPanel(idea);
}

function renderVideosForIdea(idea) {
    const grid = slotRenderTarget('video');
    const meta = document.getElementById('videos-meta');
    if (!grid || !meta) return;

    const frameRun = idea && idea.frameRun;
    const videos = (frameRun && frameRun.videos) || [];
    clearSlotGrid(grid, 'video');

    const mergedContainer = document.getElementById('merged-video-container');
    const mergedPlayer = document.getElementById('merged-video-player');
    const mergedInfo = document.getElementById('merged-video-info');
    const mergedDownload = document.getElementById('merged-video-download');
    const mergedHeading = document.getElementById('merged-video-heading');
    const mergedDescription = document.getElementById('merged-video-description');
    const mergedReveal = document.getElementById('merged-video-reveal');

    if (mergedContainer) {
        if (frameRun && frameRun.merged_video && frameRun.merged_video.status === 'success') {
            const mv = frameRun.merged_video;
            const speed = [1, 1.5, 2].includes(Number(mv.speed)) ? Number(mv.speed) : 2;
            const speedLabel = speed === 1 ? '无加速' : `${speed}倍速`;
            const speedSlug = speed === 1.5 ? '1_5x' : `${speed}x`;
            mergedContainer.style.display = 'block';
            if (mergedHeading) mergedHeading.textContent = `🎬 ${speedLabel}合并成品视频 (Merged Finished Video)`;
            if (mergedDescription) {
                mergedDescription.textContent = speed === 1
                    ? '所有生成的视频片段已按顺序合并，并保留原始播放速度。'
                    : `所有生成的视频片段已按顺序合并，并调整为${speed}倍速度播放。`;
            }
            if (mergedPlayer) {
                // 重新合成会原地覆盖同一个成片文件，同样要带版本号才看得到新的
                mergedPlayer.src = cacheBustedUrl(mv.url);
            }
            if (mergedDownload) {
                mergedDownload.href = mv.url;
                mergedDownload.download = `${idea.title || 'video'}_merged_${speedSlug}.mp4`;
            }
            if (mergedReveal) {
                // 定位用的是磁盘相对路径（mv.file），不是播放地址：合并结果原地
                // 覆盖同名文件时播放地址还挂着 cache-bust 版本号，路径才是真值。
                mergedReveal.dataset.path = mv.file || mv.url || '';
                // 这个按钮是 index.html 里的常驻元素，每次重渲都会走到这里——
                // 监听只绑一次，否则一次点击会重复打开 N 个 Finder 窗口。
                if (!mergedReveal.dataset.bound) {
                    mergedReveal.dataset.bound = '1';
                    mergedReveal.addEventListener('click', () => {
                        revealLocalFile(mergedReveal.dataset.path, '成品视频');
                    });
                }
            }
            if (mergedInfo) {
                const sizeMB = mv.size_bytes ? (mv.size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '未知大小';
                const durationSec = mv.duration_seconds ? mv.duration_seconds + ' 秒' : '未知时长';
                mergedInfo.textContent = `速率: ${speedLabel} | 文件大小: ${sizeMB} | 视频时长: ${durationSec}`;
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

    // 该槽位从未处理过时：若正处在当前后台任务的目标范围内（整单任务，或
    // target_slots 显式包含它），画"等待中"；否则说明这次任务压根没碰它，
    // 画"未生成"+生成/上传出口。同 renderFramesForIdea 的同款判断。
    const videosRec = (typeof getIdeaTaskRecord === 'function' && idea)
        ? getIdeaTaskRecord(idea.id, 'videos') : null;
    const isVideoPending = (slotNum) =>
        !!(videosRec && (!videosRec.targetSlots || videosRec.targetSlots.includes(slotNum)));

    itemsToRender.forEach(item => {
        const slotNum = item.slotNum;
        const card = document.createElement('div');
        card.id = `video-slot-${slotNum}`;
        // 拖拽：卡片本身既是放置区（接文件上传 / 接别的槽位换位过来），有内容时
        // 也是拖拽源。监听绑在卡片元素上，renderSlotCard 重写 innerHTML 不会丢。
        enableVideoSlotDnd(card, slotNum);
        renderSlotCard(card, videoSlotState(item.video, {
            seq: slotNum,
            busy: videosBusy,
            pending: isVideoPending(slotNum),
        }));
        placeSlotCard(card, 'video', slotNum);
        grid.appendChild(card);
    });

    if (typeof syncSlotToolbar === 'function') syncSlotToolbar('video');
}

// ── 封面用途分配 ────────────────────────────────────────────────────────────
// 一个项目会出好几张封面，三个用途未必用同一张：带文案的那张适合当项目封面/成片
// 首帧，却会把文字污染进图生图的第一帧。所以三个用途各自可以指一张，没指的跟随
// 主封面（缩略图里选中的那张）。服务端的真相在 manifest.cover_roles（自动合并、
// 断线恢复只认磁盘上那份），点子库条目里的 coverRoles 是同一份数据的前端副本。
const COVER_ROLE_LABELS = {
    project: '项目封面',
    video: '成片首帧',
    frame1: '帧 1 参考图',
};
const COVER_ROLE_HINTS = {
    project: '项目工作台卡片上显示的那张',
    video: '合并成片时烧进第一帧（平台取缩略图吃的就是它）',
    frame1: '第一帧图生图的参考图；选带文案的封面会把文字带进生成画面',
};

// 某个用途实际用的那张封面（显式指定 none 禁用 → 显式指定某张 → 主封面 → 最后一张）
function coverRoleUrl(idea, role) {
    const covers = (idea && idea.covers) || [];
    const roles = (idea && idea.coverRoles) || {};
    if (roles[role] === 'none') return null;
    return roles[role] || (idea && idea.activeCoverUrl) || covers[covers.length - 1] || null;
}

// 用途分配落盘：点子库条目（前端副本）+ 项目 manifest（服务端唯一真相）。
// manifest 写失败只提示不回滚——用户的选择已经在点子库里，重合并时还能再同步一次。
async function persistCoverRoles(idea) {
    if (!idea) return;
    if (typeof saveCurrentIdeaState === 'function' && typeof currentIdea !== 'undefined'
        && currentIdea && currentIdea.id === idea.id) {
        saveCurrentIdeaState();
    }
    if (typeof savedIdeas !== 'undefined' && Array.isArray(savedIdeas)) {
        const idx = savedIdeas.findIndex(item => item.id === idea.id);
        if (idx !== -1) {
            savedIdeas[idx].coverRoles = idea.coverRoles;
            savedIdeas[idx].activeCoverUrl = idea.activeCoverUrl;
            if (typeof persistIdeaItem === 'function') await persistIdeaItem(savedIdeas[idx]);
        }
    }
    try {
        const title = (typeof getIdeaSaveTitle === 'function') ? getIdeaSaveTitle(idea) : idea.title;
        if (!title) return;
        await fetch('/api/cover_roles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title,
                roles: idea.coverRoles || {},
                active_cover: idea.activeCoverUrl || null,
            }),
        });
    } catch (e) {
        // 项目目录还没建出来（封面尚未落盘）时这里必然 404，不值得打扰用户
        console.warn('[cover] 用途分配同步到项目失败', e);
    }
}

function renderCoverRoleControls(idea, container) {
    const covers = idea.covers || [];
    container.innerHTML = '';
    Object.keys(COVER_ROLE_LABELS).forEach(role => {
        const field = document.createElement('label');
        field.className = 'cover-role-field';
        field.title = COVER_ROLE_HINTS[role];

        const name = document.createElement('span');
        name.className = 'cover-role-name';
        name.textContent = COVER_ROLE_LABELS[role];

        const select = document.createElement('select');
        select.className = 'cover-role-select';
        select.setAttribute('aria-label', `${COVER_ROLE_LABELS[role]}使用的封面`);
        const follow = new Option('跟随主封面', '');
        select.appendChild(follow);
        covers.forEach((url, idx) => select.appendChild(new Option(`封面 ${idx + 1}`, url)));
        if (role === 'frame1') {
            select.appendChild(new Option('🚫 不使用（纯文生图）', 'none'));
        }

        const assigned = (idea.coverRoles || {})[role];
        select.value = (assigned === 'none' || covers.includes(assigned)) ? assigned : '';
        select.addEventListener('change', async () => {
            if (!idea.coverRoles) idea.coverRoles = {};
            if (select.value) idea.coverRoles[role] = select.value;
            else delete idea.coverRoles[role];
            renderCoversForIdea(idea);
            await persistCoverRoles(idea);
        });

        field.appendChild(name);
        field.appendChild(select);
        container.appendChild(field);
    });
}

// activeIndex 传 null/省略时按「已选中的主封面」还原，而不是无脑回到第 0 张——
// 否则任何一次重渲染（切页、任务回调）都会把用户选过的主封面悄悄改掉。
function renderCoversForIdea(idea, activeIndex = null) {
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
    if (!Number.isInteger(activeIndex)) {
        const remembered = covers.indexOf(idea.activeCoverUrl);
        activeIndex = remembered === -1 ? covers.length - 1 : remembered;
    }
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
    
    // 每张缩略图角上标出它当前承担的用途（项/片/帧），一眼能看出哪张在哪里用
    const roleTags = { project: '项', video: '片', frame1: '帧' };
    const usedBy = {};
    Object.keys(COVER_ROLE_LABELS).forEach(role => {
        const url = coverRoleUrl(idea, role);
        if (!usedBy[url]) usedBy[url] = [];
        usedBy[url].push(role);
    });

    covers.forEach((coverUrl, idx) => {
        const thumb = document.createElement('div');
        thumb.className = `cover-thumb ${idx === activeIndex ? 'active' : ''}`;
        const roles = usedBy[coverUrl] || [];
        const badges = roles.length
            ? `<span class="cover-thumb-roles" title="${roles.map(r => COVER_ROLE_LABELS[r]).join(' / ')}">`
              + roles.map(r => roleTags[r]).join('') + '</span>'
            : '';
        thumb.innerHTML = `<img src="" alt="Thumbnail ${idx + 1}" loading="lazy">${badges}`;

        const img = thumb.querySelector('img');
        img.onerror = () => {
            thumb.remove();
            // If all thumbnails are removed/hidden, hide the history container
            if (thumbnailsEl.children.length === 0) {
                historyContainer.style.display = 'none';
            }
        };
        
        safeSetImageSrc(img, coverUrl);
        
        thumb.addEventListener('click', async () => {
            renderCoversForIdea(idea, idx);
            // 主封面的切换要落盘：没单独指定用途的那几项都跟着它走（项目卡片、
            // 成片首帧、帧 1 参考图），只留在内存里等于重开一次就丢。
            await persistCoverRoles(idea);
        });

        thumbnailsEl.appendChild(thumb);
    });

    const roleControls = document.getElementById('cover-role-controls');
    if (roleControls) renderCoverRoleControls(idea, roleControls);
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
