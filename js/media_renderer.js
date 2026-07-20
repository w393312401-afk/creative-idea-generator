// --- media_renderer.js ---

// Per-URL cache-bust versions. Previously EVERY render appended `?t=Date.now()`, giving
// each render a unique URL that defeated the browser cache — so re-viewing an idea, switching
// tabs, or re-rendering the frame grid re-downloaded every image (server logs showed each
// frame fetched 10-13x, with visible blank-then-reload flicker). We now use a STABLE version
// per URL: passive re-renders reuse the same URL and hit the cache; only an explicit
// regeneration (bust=true, e.g. a retried frame overwriting the same file) bumps the version
// to force exactly one refetch.
const _imgCacheVersions = new Map();

// 服务端刚(重)写过某个文件时调用：递增该 URL 的缓存版本，让下一次渲染
// （无论走哪条渲染路径）强制回源取新图。帧重试/断线恢复等路径先更新数据
// 再整格重渲（bust=false），没有这一步就会拿浏览器缓存里的旧图。
function bustImageCache(url) {
    if (url) _imgCacheVersions.set(url, Date.now());
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
    // Remote (http/https) and inline data: URIs are immutable/opaque — never cache-bust them.
    const isLocalFile = !lower.startsWith('http://') && !lower.startsWith('https://') && !lower.startsWith('data:image/');
    let finalUrl = url;
    if (isLocalFile) {
        if (bust) _imgCacheVersions.set(url, Date.now());
        const version = _imgCacheVersions.get(url);
        if (version) {
            const separator = url.includes('?') ? '&' : '?';
            finalUrl = url + separator + 'v=' + version;
        }
    }
    imgEl.src = finalUrl;
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
    const skipped = count(f => f.quality_gate === 'sequence_review_skipped');
    const degraded = count(f => f.quality_gate === 'i2i_fallback_degraded' || f.quality_gate === 'auto_approved_degraded');
    const stale = count(f => f.stale_lineage);
    const inertia = count(f => typeof f.vlm_qa_reason === 'string' && f.vlm_qa_reason.indexOf('anchor_inertia') !== -1);
    const driftFails = (manifest.chain_drift || []).filter(d => d && d.passed === false).length;
    if (flagged) risks.push(`${flagged} 帧一致性审查未过`);
    if (skipped) risks.push(`${skipped} 帧未经整套审查（审查服务不可用）`);
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
function renderIdeaTitles(idea) {
    const titleEl = document.getElementById('idea-title');
    const titleCnEl = document.getElementById('idea-title-cn');
    const meta = getIdeaTikTokMeta(idea);
    if (titleEl) {
        titleEl.textContent = meta.english;
        titleEl.title = idea.title || meta.english;
    }
    if (titleCnEl) {
        titleCnEl.textContent = meta.chinese;
        titleCnEl.title = meta.chinese;
    }
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

    // Collapsible Audit panel logic: default fold, auto expand & highlight on repair
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
                    grid.appendChild(placeholderCard);
                    if (typeof renderVideoSlotPending === 'function') renderVideoSlotPending(i, '等待中');
                }
            }
        }
    } else {
        if (btn) btn.disabled = false;
        if (progress) progress.style.display = 'none';
    }
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
}

// 把一张帧卡片置为「未生成/已失效」占位态。除了正常的缺帧渲染，也用于
// img 加载失败（onerror）——磁盘文件已被删除但本地清单还没同步时，别让
// 卡片继续挂着一张死图/浏览器缓存里的旧图。
function markFrameCardMissing(card, seq) {
    card.className = 'frame-card video-failed-card';
    card.style.cursor = 'default';
    card.dataset.missing = '1'; // 已绑定的 lightbox 点击回调据此短路
    card.innerHTML = `
        <div class="video-failed-placeholder">
            <span class="error-icon">⚠️</span>
            <span class="error-text" style="font-size: 11px; color: var(--text-secondary);">未生成/已失效</span>
            <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}">生成</button>
        </div>
        <span>IMG ${String(seq).padStart(3, '0')}</span>
    `;
    card.querySelector('.retry-frame-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        retrySingleFrame(seq);
    });
}

function renderFramesForIdea(idea) {
    const grid = document.getElementById('frames-grid');
    const meta = document.getElementById('frames-meta');
    if (!grid || !meta) return;

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
        
        const hasImage = frame && (frame.url || frame.file);
        
        if (hasImage) {
            const isDegraded = frame.quality_gate === 'i2i_fallback_degraded';
            // 'vlm_qa_failed' 是旧逐帧质检门的终态，逐帧质检门已停用，仅为兼容展示旧 manifest 保留；
            // 'sequence_review_flagged' 是新的整套序列一致性审查修复轮次耗尽仍有问题的终态
            const isVlmFailed = frame.quality_gate === 'vlm_qa_failed' || frame.quality_gate === 'sequence_review_flagged';
            const isUnverified = frame.quality_gate === 'auto_approved_degraded';
            // 整套序列一致性审查两次（常规+降级）都没跑成时后端如实标 skipped，
            // 不再盖 sequence_reviewed_pass 假章——这里对应亮黄色"未审查"徽标
            const isReviewSkipped = frame.quality_gate === 'sequence_review_skipped';
            const isStale = frame.quality_gate === 'stale' || frame.stale;
            // 宽松档软性瑕疵放行：quality_gate 仍是 auto_approved，告警留在 vlm_qa_reason
            const isWarned = frame.quality_gate === 'auto_approved' && typeof frame.vlm_qa_reason === 'string' && frame.vlm_qa_reason.indexOf('WARN') === 0;
            card.className = 'frame-card' + (isDegraded ? ' degraded-card' : '') + (isVlmFailed ? ' vlm-failed-card' : '') + ((isUnverified || isReviewSkipped) ? ' degraded-card' : '') + (isStale ? ' stale-card' : '');
            card.style.cursor = 'pointer';

            let hoverTitle = `打开第 ${seq} 帧`;
            if (isDegraded) hoverTitle += ' (降级为文生图)';
            if (isVlmFailed) hoverTitle += ` (一致性审查未通过: ${frame.vlm_qa_reason || '跳变或无变化'})`;
            if (isUnverified) hoverTitle += ' (VLM 判定服务异常，此帧未经核验被放行)';
            if (isReviewSkipped) hoverTitle += ` (${frame.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查'})`;
            if (isWarned) hoverTitle += ` (宽松档放行: ${frame.vlm_qa_reason})`;
            if (isStale) hoverTitle += ' (过期：父帧已被重新生成，此帧与父帧血统不一致)';
            card.title = hoverTitle;

            card.innerHTML = `
                <img src="" alt="Frame ${seq}" loading="lazy">
                ${isDegraded ? '<div class="degraded-badge">降级</div>' : ''}
                ${isVlmFailed ? '<div class="vlm-failed-badge" title="' + (frame.vlm_qa_reason || '').replace(/"/g, '&quot;') + '">审查未过</div>' : ''}
                ${isUnverified ? '<div class="degraded-badge" title="' + (frame.vlm_qa_reason || 'VLM 判定服务异常，未经核验').replace(/"/g, '&quot;') + '">未核验</div>' : ''}
                ${isReviewSkipped ? '<div class="degraded-badge" title="' + (frame.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查').replace(/"/g, '&quot;') + '">未审查</div>' : ''}
                ${isWarned ? '<div class="degraded-badge" title="' + frame.vlm_qa_reason.replace(/"/g, '&quot;') + '">留痕</div>' : ''}
                ${isStale ? `<div class="stale-badge" ${isDegraded || isVlmFailed || isUnverified || isWarned ? 'style="left: 45px;"' : ''} title="此帧派生自已被替换的旧帧，建议重新生成">Stale</div>` : ''}
                <div class="frame-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
                    <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;">重试</button>
                </div>
                <span>IMG ${String(seq).padStart(3, '0')}</span>
            `;
            
            const frameImgEl = card.querySelector('img');
            frameImgEl.onerror = () => markFrameCardMissing(card, seq);
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
            
            // Click on the card opens lightbox (excluding the retry button)
            card.addEventListener('click', (e) => {
                if (e.target.classList.contains('retry-frame-btn')) return;
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
            markFrameCardMissing(card, seq);
        }

        grid.appendChild(card);
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
                mergedPlayer.src = mv.url;
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

    if (!videos.length) {
        meta.textContent = '尚未生成任何视频序列。';
        return;
    }

    const manifestText = frameRun.manifest ? ` 清单: ${frameRun.manifest}` : '';
    meta.textContent = `已生成 ${videos.length} 段连续视频，保存在 ${frameRun.project_dir || 'outputs'}.${manifestText}`;

    videos.forEach(video => {
        const card = document.createElement('div');
        card.id = `video-slot-${video.slot}`;
        
        const isFailed = video.status === 'failed' || (!video.url && !video.file);
        
        const startImg = String(video.slot).padStart(3, '0');
        const endImg = String(video.slot + 1).padStart(3, '0');
        const labelText = `VID ${String(video.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})`;
        
        if (isFailed) {
            card.className = 'frame-card video-failed-card';
            card.style.cursor = 'default';
            card.innerHTML = `
                <div class="video-failed-placeholder">
                    <span class="error-icon">⚠️</span>
                    <span class="error-text" title="${video.error || '生成失败'}">生成失败</span>
                    <button class="action-btn text-btn mini-btn retry-video-btn" data-slot="${video.slot}">重试</button>
                </div>
                <span>${labelText}</span>
            `;
            card.querySelector('.retry-video-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                retrySingleVideo(video.slot);
            });
        } else {
            card.className = 'frame-card';
            card.style.cursor = 'pointer';
            card.innerHTML = `
                <div class="video-preview-wrapper" style="position: relative; width: 100%; aspect-ratio: 9/16; border-radius: 5px; overflow: hidden; background: #03050c;">
                    <video src="${video.url}" loop muted playsinline style="width:100%; height:100%; object-fit: cover; display: block;"></video>
                    <div class="video-play-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.25); transition: all 0.2s ease;">
                        <span class="play-icon" style="font-size: 2rem; color: #fff; opacity: 0.85; transition: all 0.2s ease;">▶</span>
                    </div>
                </div>
                <span>${labelText}</span>
            `;
            
            const videoEl = card.querySelector('video');
            const playOverlay = card.querySelector('.video-play-overlay');
            const playIcon = card.querySelector('.play-icon');
            
            card.addEventListener('mouseenter', () => {
                videoEl.play().catch(() => {});
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0)';
                if (playIcon) playIcon.style.opacity = '0';
            });
            card.addEventListener('mouseleave', () => {
                videoEl.pause();
                if (playOverlay) playOverlay.style.background = 'rgba(0,0,0,0.25)';
                if (playIcon) playIcon.style.opacity = '0.85';
            });
            
            card.addEventListener('click', () => {
                const validVideos = videos.filter(v => v.url || v.file);
                const mediaList = validVideos.map((v, idx) => {
                    const startImg = String(v.slot).padStart(3, '0');
                    const endImg = String(v.slot + 1).padStart(3, '0');
                    return {
                        type: 'video',
                        url: v.url || v.file,
                        caption: `<strong>VID ${String(v.slot).padStart(3, '0')} (IMG ${startImg} ➔ IMG ${endImg})</strong>`
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

