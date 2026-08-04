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

// renderParsedPrompts 已移除：提示词页现在只展示原始 Markdown 块（#idea-prompt-block），
// 不再把 prompt_block 解析成槽位卡片。parsePromptBlock 仍保留，供帧序列渲染推算图片槽位数。

// 结构化槽位契约（2026-07-12）：后端 result.prompt_slots 是唯一权威（由后端解析器产出，
// 语义与帧/视频生成完全一致）；仅当字段缺失（旧任务/旧库存条目）时才退回上面的逐行正则
// 解析 prompt_block 兜底。前后端双实现解析的行为差异（同行冒号正文被前端静默丢弃、帧配对
// 按数组下标错位）是两次生产事故的共同前提，此处收口。返回形状与 parsePromptBlock 一致。
function resolvePromptSlots(source) {
    const ps = source && source.prompt_slots;
    if (ps && Array.isArray(ps.images) && Array.isArray(ps.videos)
            && (ps.images.length || ps.videos.length)) {
        const norm = (arr, type) => arr
            .filter(it => it && Number.isFinite(Number(it.index)))
            .map(it => {
                const index = Number(it.index);
                const meta = it.meta || '';
                return {
                    type: type,
                    index: index,
                    meta: meta,
                    label: (type === 'image' ? `图片提示词 ${index}` : `视频提示词 ${index}`) + (meta ? ` [${meta}]` : ''),
                    id: type === 'image' ? `slot-image-${index}` : `slot-video-${index}`,
                    body: it.body || ''
                };
            });
        return norm(ps.images, 'image').concat(norm(ps.videos, 'video'));
    }
    return parsePromptBlock(source ? source.prompt_block : '');
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
async function fetchIdeationCover(idea, idx, coverContainer) {
    if (idea.cover_url) {
        coverContainer.innerHTML = `
            <img src="${idea.cover_url}" alt="${idea.title}" class="ideation-cover-img" />
        `;
        return;
    }

    // 载体家族(4 桶分类)已取消，占位图标不再按家族区分；改成标记这条选题是否
    // 真的借鉴了本批联网参考——那是现在唯一有信息量的来源区分。
    const icon = idea.trend_ref ? '🌐' : '💡';
    coverContainer.innerHTML = `
        <div class="ideation-cover-placeholder">
            <span class="ideation-cover-placeholder-icon">${icon}</span>
        </div>
    `;
}

// 本批联网参考面板(可折叠):展示搜索词/自定义网址各自"搜到了什么"。
// 容器本身是横向滑动轨道,面板作为其上方的兄弟节点插入(宿主 div 幂等复用)。
// 用 DOM + textContent 构建,LLM/网页返回的文本不走 innerHTML,防注入
function renderIdeationTrendPanel(cardsContainer) {
    let host = document.getElementById('ideation-trend-panel-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'ideation-trend-panel-host';
        cardsContainer.parentNode.insertBefore(host, cardsContainer);
    }
    host.innerHTML = '';
    const refs = (typeof currentIdeationTrendRefs !== 'undefined' && Array.isArray(currentIdeationTrendRefs))
        ? currentIdeationTrendRefs.filter(r => r && r.text) : [];
    if (refs.length === 0) return;
    const panel = document.createElement('details');
    panel.className = 'ideation-trend-panel';
    const summary = document.createElement('summary');
    summary.textContent = `🌐 本批联网参考（${refs.map(r => r.source === 'custom_urls' ? '自定义网址' : '联网搜索').join(' + ')}）`;
    panel.appendChild(summary);
    refs.forEach(r => {
        const item = document.createElement('div');
        item.className = 'ideation-trend-ref';
        const label = document.createElement('div');
        label.className = 'ideation-trend-ref-label';
        label.textContent = r.label || r.source || '';
        const body = document.createElement('pre');
        body.className = 'ideation-trend-ref-text';
        body.textContent = r.text;
        item.appendChild(label);
        item.appendChild(body);
        panel.appendChild(item);
    });
    host.appendChild(panel);
}

// LLM 在激发时一次性产出 idea.beat_outline,数组长度约定为 recommended_beats + 1
// (末条是最终 reward 揭示)。它作为软计划随「一键合成」的 dimensions 一并传给后端
// (见 composeIdeationCard),但最终节拍仍由合成阶段的节拍阶梯硬规则裁定,所以对外
// 一律写「预览」而不是承诺。
function ideaBeatOutline(idea) {
    return Array.isArray(idea && idea.beat_outline)
        ? idea.beat_outline.map(s => String(s == null ? '' : s).trim()).filter(Boolean)
        : [];
}

const PACING_SKELETON_LABELS = {
    linear_milestone: '单线里程碑',
    dual_payoff: '内外双重完工',
    nested_space_payoff: '双空间一比一复刻'
};

function pacingSkeletonLabel(id) {
    return PACING_SKELETON_LABELS[String(id || '').trim()] || '单线里程碑';
}

// 节拍简介入口只有一个:卡片底部的「🔨 节拍简介」按钮(见 renderIdeationCards)。
// 卡片正文里原本还有一行计数+首拍的入口,和按钮重复,已删。

// 节拍简介全量视图。卡片轨道装不下十几拍,所以完整清单只在这个弹窗里出现:
// modal-body 自己滚动,节拍一条不落。
function openBeatOutlineModal(index) {
    const idea = currentIdeatedIdeas[index];
    const modal = document.getElementById('beat-outline-modal');
    if (!idea || !modal) return;

    const beats = ideaBeatOutline(idea);
    // 没有节拍简介的卡片(上线前激发的旧缓存卡、或这一条模型没写 beat_outline)不该
    // 弹一个空窗:维度已经在调用方载入好了,这里只给一句能指导下一步的提示。
    if (beats.length === 0) {
        showToast('这张灵感卡没有节拍简介（激发时未产出），维度已载入；点右上「🎲 换一批灵感」可重新激发带节拍简介的卡片', 'info');
        return;
    }

    const title = document.getElementById('beat-outline-modal-title');
    if (title) title.textContent = `🔨 节拍简介 · ${idea.title || '未命名选题'}`;

    const info = document.getElementById('beat-outline-modal-info');
    if (info) {
        const recBeats = Number.isFinite(+idea.recommended_beats) && +idea.recommended_beats > 0
            ? `推荐 ${idea.recommended_beats} 拍${idea.beats_reason ? `（${idea.beats_reason}）` : ''} · `
            : '';
        const floor = Number.isFinite(+idea.beats_floor) && +idea.beats_floor > 0
            ? `其中「不少于 ${idea.beats_floor} 拍」是强制的，合成时压不下去；`
            : '';
        // 改造后拍数区间是强制的（beats_floor 一路传到合成侧的 _beat_count_is_valid），
        // 只有清单内容会被硬规则改写——这里的文案要和那个事实对齐，不能再笼统地说
        // 「最终节拍以合成结果为准」，那会让卡片上的推进密度看起来像个空头承诺。
        info.textContent = `${recBeats}共 ${beats.length} 条（含末条 reward 揭示）。${floor}`
            + '清单本身是工序草案，正式合成时会按真实施工顺序等硬规则改写/合并/增删。';
    }

    const list = document.getElementById('beat-outline-modal-list');
    if (list) {
        list.innerHTML = '';
        beats.forEach((text, i) => {
            const li = document.createElement('li');
            // 末条按约定是 reward/揭示拍,单独标色以便一眼看到成片落点
            if (i === beats.length - 1) li.className = 'reward';
            li.textContent = text;
            list.appendChild(li);
        });
    }

    modal.classList.add('active');
}

function closeBeatOutlineModal() {
    const modal = document.getElementById('beat-outline-modal');
    if (modal) modal.classList.remove('active');
}

function initBeatOutlineModal() {
    const modal = document.getElementById('beat-outline-modal');
    if (!modal) return;
    const closeBtn = document.getElementById('beat-outline-modal-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeBeatOutlineModal);
    // 点遮罩空白处关闭(内容区不冒泡到这里)
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeBeatOutlineModal();
    });
}

function renderIdeationCards(ideas) {
    const container = document.getElementById('ideation-cards-container');
    if (!container) return;
    
    if (ideas.length === 0) {
        container.innerHTML = '<div class="ideation-loading">暂无灵感推荐</div>';
        return;
    }
    
    container.innerHTML = '';
    renderIdeationTrendPanel(container);

    ideas.forEach((idea, idx) => {
        const card = document.createElement('div');
        card.className = 'ideation-card';
        card.dataset.index = idx;
        
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
                <span class="ideation-card-tag">反差强度: ${idea.score >= 23 ? '极高' : '高'}</span>
                ${Number.isFinite(+idea.recommended_beats) && +idea.recommended_beats > 0
                    ? `<span class="ideation-card-tag beats" title="${idea.beats_reason || ''}">⏱ ${idea.recommended_beats} 拍${
                        Number.isFinite(+idea.beats_floor) && +idea.beats_floor > 0
                            ? `（不少于 ${idea.beats_floor}）` : ''}</span>`
                    : ''}
                <span class="ideation-card-tag pacing-skeleton" title="这张卡的节拍简介所采用的推进骨架">🦴 ${pacingSkeletonLabel(idea.pacing_skeleton)}</span>
            </div>
            <div class="ideation-card-body">
                <div>载体: ${idea.carrier} (${idea.env})</div>
                <div>现状: ${idea.trauma}</div>
                <div class="ideation-card-twist">招牌反差: ${idea.twist_zh || idea.twist}</div>
                ${idea.trend_ref ? `<div class="ideation-card-trend">🌐 趋势借鉴: ${idea.trend_ref}</div>` : ''}
            </div>
            <div class="ideation-card-actions">
                ${ideaBeatOutline(idea).length > 0
                    ? `<button type="button" class="ideation-card-btn select-action-btn" title="查看该选题的全部节拍简介（共 ${ideaBeatOutline(idea).length} 拍），并把它载入下方维度">🔨 节拍简介</button>`
                    // 这一条没产出节拍简介时退回按钮的原职责，别给一个点开是空的入口
                    : `<button type="button" class="ideation-card-btn select-action-btn" title="这张卡激发时未产出节拍简介，点击仅载入维度">载入维度</button>`}
                <button type="button" class="ideation-card-btn copy-action-btn">复制选题</button>
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
        
        // 「🔨 节拍简介」按钮（原「载入维度」）：打开全量节拍清单，同时保留原按钮的
        // 载入维度行为——挑卡时看完工序就能直接走下方主生成按钮，不用再点一次卡片。
        const outlineBtn = card.querySelector('.select-action-btn');
        outlineBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            selectIdeationCard(idx);
            openBeatOutlineModal(idx);
        });

        // Clicking "一键合成" directly starts compose
        const composeBtn = card.querySelector('.compose-action-btn');
        composeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            composeIdeationCard(idx);
        });

        // Clicking "复制选题" copies the ready-to-paste Tier-1 input string so it can be
        // pasted directly into a real restoration-prompt-composer skill chat session.
        const copyBtn = card.querySelector('.copy-action-btn');
        copyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyText(idea.input_str).then(() => {
                showToast(`已复制选题："${idea.title}"，可直接粘贴到 restoration-prompt-composer 技能会话中使用`, 'success');
            }).catch(() => {
                showToast('复制失败', 'error');
            });
        });

        container.appendChild(card);
    });

    // 新一批卡片就位：激发轨的 ② 芯片改口播"N 张待选"
    if (typeof updateSparkRail === 'function') updateSparkRail();
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
    
    // Set loaded cover information — 基础场景主题选择器已移除：主生成按钮的选题
    // （dimensions.theme）直接用这里存下的一键输入串，任务名用卡片选题名
    loadedIdeationCover = {
        input_str: idea.input_str || null,
        cover_url: idea.cover_url || null,
        english_title: idea.english_title || null,
        topic_dna: idea.dna || null,
        llm_score: Number.isFinite(+idea.score) ? +idea.score : null,
        creative_seed: {
            input_str: idea.input_str || null,
            carrier: idea.carrier || null,
            env: idea.env || null,
            trauma: idea.trauma || null,
            destiny: idea.destiny || null,
            twist: idea.twist || null,
            twist_zh: idea.twist_zh || null
        },
        task_label: idea.title || null,
        // 载入卡片后走页脚主「激发」按钮时也要带上同一份节拍计划和骨架；
        // 此前只有卡片上的「一键合成」路径会透传 beat_outline。
        beat_outline: ideaBeatOutline(idea),
        pacing_skeleton: String(idea.pacing_skeleton || 'linear_milestone'),
        // 施工拍下界：后端在激发时按这张卡的工序清单算好（compute_beats_floor），
        // 前端只原样带走。合成时它替掉与项目重量无关的全局常量下界，
        // 让「载入 → 手动生成」和卡片「一键合成」落在同一个拍数区间里。
        beats_floor: Number.isFinite(+idea.beats_floor) ? +idea.beats_floor : null,
        // 随卡片一并载入，供「载入维度」后走主生成按钮时也能在合成时计次
        // 联网参考案例库使用次数（见 app.js generateIdea 与 server.py /api/compose）
        trend_ref: idea.trend_ref || null,
        trend_ref_ids: idea.trend_ref_ids || []
    };

    // 只同步这张卡真正申报过的维度。复杂度/预算/反差/尺度以前会被重置成硬编码的
    // 3/2/50/3，和「载入这张卡」的语义矛盾（卡片并没有申报这四项），用户手调过的值
    // 会被静静抹掉；现在一律保留当前值。
    // 节拍滑块在改造后只是**额度上限**：卡片给的推荐拍数就是这一单的上限，
    // 下界由 idea.beats_floor 带给后端，滑块压不动它。
    // 用户自己设过拍数就不再覆盖：卡片推荐只是**缺省**，不能反过来把用户刚拨的
    // 滑块静静改掉（那正是「节拍数设置无效」的一半成因）。
    const recBeats = clampRecommendedBeats(idea.recommended_beats);
    const beatsKeptByUser = beatsUserOverridden && recBeats !== null
        && String(recBeats) !== String(document.getElementById('slider-beats').value);
    if (recBeats !== null && !beatsUserOverridden) {
        document.getElementById('slider-beats').value = recBeats;
    }

    // Trigger input events to update labels
    ['slider-complexity', 'slider-budget', 'slider-ratio', 'slider-creativity', 'slider-beats'].forEach(id => {
        document.getElementById(id).dispatchEvent(new Event('input'));
    });
    
    // 保留了用户自己的拍数就说一声,否则卡片上写着「⏱ 13 拍」、滑块停在 8,
    // 用户不知道该信哪个
    showToast(beatsKeptByUser
        ? `已载入灵感: ${idea.title}。已保留你手动设定的 ${document.getElementById('slider-beats').value} 拍（卡片推荐 ${recBeats} 拍）`
        : `已载入灵感: ${idea.title}。可以在下方微调维度并点击生成！`, "success");
    saveSelectionState();
}

// 推荐拍数合法化:非数字/越界返回 null(调用方保持原值),合法值收进滑块的 5-15 区间
function clampRecommendedBeats(v) {
    const n = Math.round(+v);
    if (!Number.isFinite(n) || n <= 0) return null;
    return Math.min(15, Math.max(5, n));
}

// 「一键合成」不经过「载入维度」这一步，所以它必须自己去读激发维度页上的节拍设置。
// 此前这里整份 dimensions 都是硬编码的，节拍数与规划模式压根没读页面——用户在页面上
// 怎么调都没用。规则：用户手动设过（beatsUserOverridden）就以页面为准，没设过才回落到
// 这张卡的推荐拍数；规划模式恒以页面为准（缺省本来就是 adaptive）。
function resolveCardBeatSettings(idea) {
    const beatsEl = document.getElementById('slider-beats');
    const modeEl = document.getElementById('beat-count-mode');
    const sliderBeats = beatsEl ? clampRecommendedBeats(beatsEl.value) : null;
    const cardBeats = clampRecommendedBeats(idea.recommended_beats) || 15;
    return {
        beats_count: (beatsUserOverridden && sliderBeats !== null) ? sliderBeats : cardBeats,
        beat_count_mode: (modeEl && modeEl.value === 'fixed') ? 'fixed' : 'adaptive'
    };
}

function composeIdeationCard(index) {
    const idea = currentIdeatedIdeas[index];
    if (!idea) return;

    const beatSettings = resolveCardBeatSettings(idea);

    const dimensions = {
        theme: idea.input_str,
        // 任务名称直接用卡片选题名（任务抽屉/激发 loading 优先展示 task_label）
        task_label: idea.title || null,
        anchors: [idea.twist_zh || idea.twist],
        complexity: "硬核重工",
        budget: "轻奢设计师级",
        ratio: "50% (外壳粗野 ↔ 内里精致)",
        creativity: "脑洞大开",
        beats_count: beatSettings.beats_count,
        // 拍数下界（后端按这张卡的工序清单算好，见 compute_beats_floor）。
        // beats_count 是上限、beats_floor 是下界，合成侧的 ladder 只能在这个区间里挑。
        // 下界高过用户设的上限时由后端夹回（min(beats_floor, beats_count)），
        // 所以往下压拍数永远不会把验收弄成恒假。
        beats_floor: Number.isFinite(+idea.beats_floor) ? +idea.beats_floor : null,
        beat_count_mode: beatSettings.beat_count_mode,
        // 卡片上展示的工序预览作为软计划传给后端节拍规划(硬规则优先,冲突时会被改写),
        // 让用户挑卡时看到的工序和最终成片大体对得上
        beat_outline: Array.isArray(idea.beat_outline)
            ? idea.beat_outline.map(s => String(s == null ? '' : s).trim()).filter(Boolean)
            : [],
        pacing_skeleton: String(idea.pacing_skeleton || 'linear_milestone'),
        cover_url: idea.cover_url || null,
        english_title: idea.english_title || null,
        topic_dna: idea.dna || null,
        llm_score: Number.isFinite(+idea.score) ? +idea.score : null,
        // 只有真正点击一键合成/激发时才随请求送到后端入账；生成灵感卡片本身不入账。
        ledger_candidate: {
            dna: idea.dna || null,
            title: idea.title || null,
            score: Number.isFinite(+idea.score) ? +idea.score : null,
            creative_seed: {
                input_str: idea.input_str || null,
                carrier: idea.carrier || null,
                env: idea.env || null,
                trauma: idea.trauma || null,
                destiny: idea.destiny || null,
                twist: idea.twist || null,
                twist_zh: idea.twist_zh || null
            }
        },
        // 联网参考案例库使用计次：后端只在这条 idea 确实借鉴过参考（trend_ref
        // 非空）时才对 trend_ref_ids 计次一次，浏览/激发阶段不算数
        trend_ref: idea.trend_ref || null,
        trend_ref_ids: idea.trend_ref_ids || []
    };

    // 拍数是这条路径上唯一会被用户改动的维度，直接报出来（含模式），
    // 免得用户再一次怀疑页面上的设置到底进没进这一单
    showToast(`🚀 开始一键合成灵感: ${idea.title}（${beatSettings.beats_count} 拍 · ${
        beatSettings.beat_count_mode === 'fixed' ? '固定' : '自适应上限'}）...`, "success");
    
    generateIdea({
        dimensions: dimensions,
        config: { ...config }
    });
}
