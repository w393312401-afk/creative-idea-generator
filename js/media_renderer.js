// --- media_renderer.js ---

function safeSetImageSrc(imgEl, url) {
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
    if (isSafe) {
        // Add cache buster for local files to avoid caching issues during regeneration
        let finalUrl = url;
        if (!lower.startsWith('http://') && !lower.startsWith('https://') && !lower.startsWith('data:image/')) {
            const separator = url.includes('?') ? '&' : '?';
            finalUrl = url + separator + 't=' + Date.now();
        }
        imgEl.src = finalUrl;
    } else {
        console.warn("Blocked potentially unsafe image URL:", url);
        imgEl.removeAttribute('src');
    }
}

function renderIdea(result) {
    document.getElementById('idea-title').textContent = result.title || '未命名创意';
    document.getElementById('tag-theme').textContent = result.theme || '';
    document.getElementById('tag-creativity').textContent = result.creativity || '';

    renderRepairBanner(result.repair_md);
    document.getElementById('idea-prompt-block').textContent = result.prompt_block || '（本次未返回提示词内容）';
    document.getElementById('idea-audit').innerHTML = renderAuditMarkdown(result.audit_md);
    
    // Parse slots and render them
    renderParsedPrompts(result.prompt_block);
    
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

    // Render covers
    renderCoversForIdea(result);

    // Asynchronously fetch latest manifest (frames & videos) from server if it exists
    fetch(`/api/get_manifest?title=${encodeURIComponent(result.title)}`)
        .then(resp => {
            if (resp.ok) {
                return resp.json();
            }
            throw new Error('Not found');
        })
        .then(manifest => {
            result.frameRun = manifest;
            saveCurrentIdeaState();
            const existingIdx = savedIdeas.findIndex(item => item.id === result.id);
            if (existingIdx !== -1) {
                savedIdeas[existingIdx].frameRun = manifest;
                saveLibrary();
            }
            renderFramesForIdea(result);
            renderVideosForIdea(result);
        })
        .catch(e => {
            // If not found or error, render using whatever is in result
            renderFramesForIdea(result);
            renderVideosForIdea(result);
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

    // Get expected image slots
    const slots = parsePromptBlock(idea ? idea.prompt_block : '');
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
        ? imageSlots.map((slot, idx) => {
            const seq = idx + 1; // 1-based sequence
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
            const isVlmFailed = frame.quality_gate === 'vlm_qa_failed';
            const isStale = frame.quality_gate === 'stale' || frame.stale;
            card.className = 'frame-card' + (isDegraded ? ' degraded-card' : '') + (isVlmFailed ? ' vlm-failed-card' : '') + (isStale ? ' stale-card' : '');
            card.style.cursor = 'pointer';
            
            let hoverTitle = `打开第 ${seq} 帧`;
            if (isDegraded) hoverTitle += ' (降级为文生图)';
            if (isVlmFailed) hoverTitle += ` (VLM 检查未通过: ${frame.vlm_qa_reason || '跳变或无变化'})`;
            if (isStale) hoverTitle += ' (过期：父帧已被重新生成，此帧与父帧血统不一致)';
            card.title = hoverTitle;
            
            card.innerHTML = `
                <img src="" alt="Frame ${seq}" loading="lazy">
                ${isDegraded ? '<div class="degraded-badge">降级</div>' : ''}
                ${isVlmFailed ? '<div class="vlm-failed-badge" title="' + (frame.vlm_qa_reason || '').replace(/"/g, '&quot;') + '">VLM 失败</div>' : ''}
                ${isStale ? `<div class="stale-badge" ${isDegraded || isVlmFailed ? 'style="left: 45px;"' : ''} title="此帧派生自已被替换的旧帧，建议重新生成">Stale</div>` : ''}
                <div class="frame-card-actions" style="position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;">
                    <button class="action-btn text-btn mini-btn retry-frame-btn" data-seq="${seq}" style="background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px; font-size: 10px;">重试</button>
                </div>
                <span>IMG ${String(seq).padStart(3, '0')}</span>
            `;
            
            safeSetImageSrc(card.querySelector('img'), frame.url || frame.file);
            
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
        } else {
            // Missing or failed frame
            card.className = 'frame-card video-failed-card';
            card.style.cursor = 'default';
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

