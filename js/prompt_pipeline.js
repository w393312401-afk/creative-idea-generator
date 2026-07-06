// --- prompt_pipeline.js ---

function parsePromptBlock(blockText) {
    const lines = (blockText || '').split('\n');
    const slots = [];
    let currentSlot = null;
    let currentBody = [];
    
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;
        
        // Labels may carry a metadata tag between the number and the colon: "图片 8 [BRIDGE]:"
        const imgMatch = line.match(/^(?:图片)\s*(\d+)(?:\s*\[(.*?)\])?\s*:/i);
        const vidMatch = line.match(/^(?:视频)\s*(\d+)(?:\s*\[(.*?)\])?\s*:/i);

        if (imgMatch || vidMatch) {
            if (currentSlot) {
                currentSlot.body = currentBody.join('\n').trim();
                slots.push(currentSlot);
            }

            const isImage = !!imgMatch;
            const index = parseInt(isImage ? imgMatch[1] : vidMatch[1], 10);
            const meta = (isImage ? imgMatch[2] : vidMatch[2]) || '';

            currentSlot = {
                type: isImage ? 'image' : 'video',
                index: index,
                meta: meta,
                label: (isImage ? `图片提示词 ${index}` : `视频提示词 ${index}`) + (meta ? ` [${meta}]` : ''),
                id: isImage ? `slot-image-${index}` : `slot-video-${index}`,
                body: ''
            };
            currentBody = [];
        } else if (line === '图片提示词' || line === '视频提示词' || line.startsWith('===') || line.startsWith('---') || line.startsWith('***') || line.startsWith('___')) {
            // Header or separators - skip
        } else {
            if (currentSlot) {
                currentBody.push(lines[i]);
            }
        }
    }
    
    if (currentSlot) {
        currentSlot.body = currentBody.join('\n').trim();
        slots.push(currentSlot);
    }
    
    return slots;
}

function renderParsedPrompts(blockText) {
    const slots = parsePromptBlock(blockText);
    const container = document.getElementById('parsed-prompts-container');
    const jumpPills = document.getElementById('jump-pills');
    
    if (!container) return;
    
    container.innerHTML = '';
    if (jumpPills) jumpPills.innerHTML = '';
    
    if (slots.length === 0) {
        container.innerHTML = '<p class="audit-empty">未解析到提示词槽位，请查看下方原始提示词。</p>';
        return;
    }
    
    slots.sort((a, b) => {
        if (a.type !== b.type) {
            return a.type === 'image' ? -1 : 1;
        }
        return a.index - b.index;
    });
    
    slots.forEach(slot => {
        if (jumpPills) {
            const pill = document.createElement('button');
            pill.type = 'button';
            pill.className = `jump-pill ${slot.type === 'image' ? 'image-pill' : 'video-pill'}`;
            pill.textContent = slot.type === 'image' ? `图 ${slot.index}` : `视 ${slot.index}`;
            pill.addEventListener('click', () => {
                const el = document.getElementById(slot.id);
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    el.style.borderColor = slot.type === 'image' ? 'var(--neon-cyan)' : 'var(--neon-purple)';
                    el.style.boxShadow = slot.type === 'image' ? '0 0 20px rgba(0,242,254,0.4)' : '0 0 20px rgba(157,78,221,0.4)';
                    setTimeout(() => {
                        el.style.borderColor = 'var(--border-color)';
                        el.style.boxShadow = 'none';
                    }, 1000);
                }
            });
            jumpPills.appendChild(pill);
        }
        
        const card = document.createElement('div');
        card.className = `prompt-slot-card ${slot.type}-slot`;
        card.id = slot.id;
        
        const header = document.createElement('div');
        header.className = 'prompt-slot-header';
        
        const label = document.createElement('span');
        label.className = 'slot-label';
        label.textContent = slot.type === 'image' ? `🖼️ 图片 ${slot.index}` : `🎬 视频 ${slot.index}`;
        header.appendChild(label);
        
        const actions = document.createElement('div');
        actions.className = 'slot-actions';
        
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-btn text-btn mini-btn';
        copyBtn.innerHTML = '复制';
        copyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyText(slot.body).then(() => {
                showToast(`${slot.type === 'image' ? '图片' : '视频'} ${slot.index} 提示词复制成功！`, 'success');
                copyBtn.innerHTML = '已复制！✓';
                copyBtn.classList.add('copied');
                setTimeout(() => {
                    copyBtn.innerHTML = '复制';
                    copyBtn.classList.remove('copied');
                }, 1000);
            }).catch(err => {
                showToast("复制失败", "error");
            });
        });
        
        const foldBtn = document.createElement('button');
        foldBtn.className = 'action-btn text-btn mini-btn';
        foldBtn.innerHTML = '收起';
        
        actions.appendChild(copyBtn);
        actions.appendChild(foldBtn);
        header.appendChild(actions);
        
        const body = document.createElement('div');
        body.className = 'prompt-slot-body';
        body.textContent = slot.body;
        
        foldBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (body.style.display === 'none') {
                body.style.display = 'block';
                foldBtn.innerHTML = '收起';
            } else {
                body.style.display = 'none';
                foldBtn.innerHTML = '展开';
            }
        });
        
        card.appendChild(header);
        card.appendChild(body);
        container.appendChild(card);
    });

    // Re-apply current slot filter to newly rendered slots
    applySlotFilter();
}

function updateTasksBadge(tasksList) {
    const badge = document.getElementById('active-task-count');
    if (!badge) return;
    
    const runningTasksCount = tasksList.filter(t => t.status === 'running').length;
    if (runningTasksCount > 0) {
        badge.textContent = runningTasksCount;
        badge.style.display = 'flex';
    } else {
        badge.style.display = 'none';
    }
}

function renderRepairBanner(repairMd) {
    const el = document.getElementById('idea-repair');
    if (!el) return;
    const rm = (repairMd || '').trim();
    if (!rm) {
        el.style.display = 'none';
        el.textContent = '';
        return;
    }
    const passed = /^PASS/i.test(rm) || rm.includes('未发现违规');
    el.style.display = 'block';
    el.className = 'repair-banner ' + (passed ? 'ok' : 'fixed');
    el.textContent = (passed ? '✅ 工序与场景一致性校验：' : '🔧 工序与场景一致性校验·已修复：\n') + rm;
}

function splitTableRow(line) {
    const cells = line.split('|').map(c => c.trim());
    if (cells.length && cells[0] === '') cells.shift();
    if (cells.length && cells[cells.length - 1] === '') cells.pop();
    return cells;
}

function renderAuditMarkdown(md) {
    if (!md) return '<p class="audit-empty">（本次未返回审核报告）</p>';
    const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Escape HTML, then promote **bold** and `code` Markdown to inline tags.
    const inline = (s) => esc(s)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
    const lines = md.split('\n');
    let html = '';
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        const isTableHeader = line.includes('|') && i + 1 < lines.length &&
            /^[\s|:\-]+$/.test(lines[i + 1]) && lines[i + 1].includes('-');
        if (isTableHeader) {
            const header = splitTableRow(line);
            const rows = [];
            i += 2;
            while (i < lines.length && lines[i].includes('|')) {
                rows.push(splitTableRow(lines[i]));
                i++;
            }
            html += '<table class="audit-table"><thead><tr>' +
                header.map(h => `<th>${inline(h)}</th>`).join('') +
                '</tr></thead><tbody>' +
                rows.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
                '</tbody></table>';
            continue;
        }
        const t = line.trim();
        if (t) html += `<p>${inline(t.replace(/^#+\s*/, ''))}</p>`;
        i++;
    }
    return html || '<p class="audit-empty">（本次未返回审核报告）</p>';
}

let loadedIdeationCover = null;

// NOTE: There is no server-side "/api/generate_ideation_cover" endpoint — it was
// never implemented (grep the whole backend, zero matches for "ideation"). Calling
// it used to fire a failing POST for every idea card on every page load and every
// "换一批灵感" refresh, each ending in a console error and a "生成失败" flash. Ideation
// cards get a fast, free, deterministic placeholder instead; the covers actually
// worth spending an image-generation call on are the ones made after a project is
// selected and composed (see /api/generate_cover, which does work — task-based,
// polled for the result).
const IDEATION_FAMILY_ICON = { 'natural': '🌲', 'man-made': '🏚️', 'vehicle': '🚢', 'fantasy': '🔮' };

async function fetchIdeationCover(idea, idx, coverContainer) {
    if (idea.cover_url) {
        coverContainer.innerHTML = `
            <img src="${idea.cover_url}" alt="${idea.title}" class="ideation-cover-img" />
        `;
        return;
    }

    const icon = IDEATION_FAMILY_ICON[idea.carrier_family] || '💡';
    coverContainer.innerHTML = `
        <div class="ideation-cover-placeholder">
            <span class="ideation-cover-placeholder-icon">${icon}</span>
        </div>
    `;
}

function renderIdeationCards(ideas) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    
    if (ideas.length === 0) {
        container.innerHTML = '<div class="ideation-loading">暂无灵感推荐</div>';
        return;
    }
    
    container.innerHTML = '';
    ideas.forEach((idea, idx) => {
        const card = document.createElement('div');
        card.className = 'ideation-card';
        card.dataset.index = idx;
        
        const familyClass = idea.carrier_family || 'natural';
        const familyText = {
            'natural': '🌲 自然',
            'man-made': '🏚️ 遗迹',
            'vehicle': '🚢 载具',
            'fantasy': '🔮 幻想'
        }[familyClass] || '自然';
        
        card.innerHTML = `
            <div class="ideation-card-cover" id="ideation-cover-${idx}">
                <div class="cover-spinner">
                    <span class="spinner-icon">⏳</span>
                    <span class="spinner-text">正在制作灵感封面...</span>
                </div>
            </div>
            <div class="ideation-card-header">
                <div class="ideation-card-title">${idea.title}</div>
                <div class="ideation-card-score">${idea.score}分</div>
            </div>
            <div class="ideation-card-metadata">
                <span class="ideation-card-tag ${familyClass}">${familyText}</span>
                <span class="ideation-card-tag">反差强度: ${idea.score >= 23 ? '极高' : '高'}</span>
            </div>
            <div class="ideation-card-body">
                <div>载体: ${idea.carrier} (${idea.env})</div>
                <div>现状: ${idea.trauma}</div>
                <div class="ideation-card-twist">招牌反差: ${idea.twist_zh || idea.twist}</div>
            </div>
            <div class="ideation-card-actions">
                <button type="button" class="ideation-card-btn select-action-btn">载入维度</button>
                <button type="button" class="ideation-card-btn primary compose-action-btn">一键合成</button>
            </div>
        `;
        
        // Asynchronously fetch cover
        const coverContainer = card.querySelector('.ideation-card-cover');
        fetchIdeationCover(idea, idx, coverContainer);
        
        // Clicking the cover opens the lightbox
        coverContainer.addEventListener('click', (e) => {
            e.stopPropagation();
            if (idea.cover_url && typeof openLightbox === 'function') {
                openLightbox([{
                    type: 'image',
                    url: idea.cover_url,
                    caption: `<strong>${idea.title}</strong><br>载体: ${idea.carrier} | 现状: ${idea.trauma} | 招牌反差: ${idea.twist_zh || idea.twist}`
                }], 0);
            } else {
                selectIdeationCard(idx);
            }
        });
        
        // Clicking the card itself loads the dimensions
        card.addEventListener('click', (e) => {
            if (e.target.classList.contains('compose-action-btn')) return;
            selectIdeationCard(idx);
        });
        
        // Clicking "一键合成" directly starts compose
        const composeBtn = card.querySelector('.compose-action-btn');
        composeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            composeIdeationCard(idx);
        });
        
        container.appendChild(card);
    });
}

function selectIdeationCard(index) {
    const idea = currentIdeatedIdeas[index];
    if (!idea) return;
    
    // Highlight selected card
    document.querySelectorAll('.ideation-card').forEach((card, idx) => {
        if (idx === index) {
            card.classList.add('selected');
        } else {
            card.classList.remove('selected');
        }
    });
    
    // 1. Select matching carrier theme in GUI
    const themeVal = mapEnglishCarrierToValue(idea.carrier);
    
    // Set loaded cover information
    loadedIdeationCover = {
        themeValue: themeVal,
        cover_url: idea.cover_url || null,
        english_title: idea.english_title || null
    };
    
    const themeBtn = document.querySelector(`#theme-selector .theme-btn[data-value="${themeVal}"]`);
    if (themeBtn) {
        document.querySelectorAll('#theme-selector .theme-btn').forEach(btn => btn.classList.remove('active'));
        themeBtn.classList.add('active');
    }
    
    // 2. Select matching anchors
    const mappedAnchorVal = mapTwistToAnchorValue(idea.dna);
    document.querySelectorAll('#anchor-selector .anchor-node').forEach(node => {
        if (node.dataset.value === mappedAnchorVal) {
            node.classList.add('active');
        } else {
            node.classList.remove('active');
        }
    });
    
    // 3. Populate slider values
    document.getElementById('slider-complexity').value = 3;
    document.getElementById('slider-budget').value = 2;
    document.getElementById('slider-ratio').value = 50;
    document.getElementById('slider-creativity').value = 3;
    
    // Trigger input events to update labels
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity'].forEach(id => {
        document.getElementById(id).dispatchEvent(new Event('input'));
    });
    
    showToast(`已载入灵感: ${idea.title}。可以在下方微调维度并点击生成！`, "success");
    saveSelectionState();
}

function composeIdeationCard(index) {
    const idea = currentIdeatedIdeas[index];
    if (!idea) return;
    
    const dimensions = {
        theme: idea.input_str,
        anchors: [idea.twist_zh || idea.twist],
        complexity: "硬核重工",
        budget: "轻奢设计师级",
        ratio: "50% (外壳粗野 ↔ 内里精致)",
        creativity: "脑洞大开",
        beats_count: 15,
        cover_url: idea.cover_url || null,
        english_title: idea.english_title || null
    };
    
    showToast(`🚀 开始一键合成灵感: ${idea.title}...`, "success");
    
    generateIdea({
        dimensions: dimensions,
        config: { ...config }
    });
}

