// --- prompt_pipeline.js ---

// 结构标记词（meta）的规范写法是方括号 [HERO] / [BRIDGE] / [BRIDGE TURN] / [CUT]，
// 与后端 prompt_pipeline/reverse.py 写出的 meta 一一对应。但手写或从别处粘来的集子
// 常把它写成括号——中文输入法下 shift+9 出的就是全角（），[ 反而要切一次输入法。
// 括号在本格式里另有含义（正文简介），所以不能一律当 meta；只认这几个标记词本身。
// 认不出来的后果不是报错而是静默降级：meta 留空 → normalizePromptSetText 里的
// heroVideos 筛不到它 → 收尾英雄段丢掉「槽位号恒等于最后一张图」的约定。
// 位置刻意排在 parsePromptBlock 之前：tests/test_prompt_import.js 只 vm 装载到该函数
// 结尾为止的切片，放在后面会让测试里的调用撞上 TDZ。
const SLOT_META_TAG_RE = /^(?:HERO|BRIDGE(?:\s+TURN)?|CUT)$/i;

function parsePromptBlock(blockText) {
    const raw = String(blockText || '')
        .replace(/^\uFEFF/, '')
        .replace(/\r\n?/g, '\n')
        .replace(/[\u200B-\u200D\u2060\uFEFF]/g, '');
    const lines = raw.split('\n');
    const slots = [];
    let currentSlot = null;
    let currentBody = [];
    
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;
        
        // 剥离 Markdown 标题符号（#### ）、列表符号、加粗符号以供宽松匹配
        const stripped = line.replace(/^[#\-*+>`\s]+/, '').replace(/[*_`]/g, '').trim();

        // 匹配各类槽位头行: "图片 8:", "#### 图片 8:", "**图片 8:**", "图片 8（简介）[BRIDGE]:", "IMAGE 8:", "图片 8："
        const imgMatch = stripped.match(/^(?:图片|图像|画面|IMAGE|IMG|Frame)\s*(?:第|#)?\s*(\d+)((?:\s*(?:[（\(].*?[）\)]|\[.*?\]))*)\s*[:：]\s*(.*)$/i);
        const vidMatch = stripped.match(/^(?:视频|镜头|VIDEO|VID|Clip)\s*(?:第|#)?\s*(\d+)((?:\s*(?:[（\(].*?[）\)]|\[.*?\]))*)\s*[:：]\s*(.*)$/i);

        if (imgMatch || vidMatch) {
            if (currentSlot) {
                currentSlot.body = currentBody.join('\n').trim();
                slots.push(currentSlot);
            }

            const isImage = !!imgMatch;
            const match = isImage ? imgMatch : vidMatch;
            const index = parseInt(match[1], 10);
            const tags = match[2] || '';
            const summaryMatch = tags.match(/[（\(](.*?)[）\)]/);
            let summary = summaryMatch ? summaryMatch[1].trim() : '';
            const metaMatch = tags.match(/\[(.*?)\]/);
            let meta = metaMatch ? metaMatch[1].trim() : '';
            // 括号里写的是标记词而不是简介时收进 meta（见 SLOT_META_TAG_RE）。
            // 方括号写法优先：两者都写了就以方括号为准，不动括号里的内容。
            if (!meta && SLOT_META_TAG_RE.test(summary)) {
                meta = summary.toUpperCase().replace(/\s+/g, ' ');
                summary = '';
            }
            const inlineRest = (match[3] || '').trim();

            currentSlot = {
                type: isImage ? 'image' : 'video',
                index: index,
                summary: summary,
                meta: meta,
                label: (isImage ? `图片提示词 ${index}` : `视频提示词 ${index}`) + (summary ? `（${summary}）` : '') + (meta ? ` [${meta}]` : ''),
                id: isImage ? `slot-image-${index}` : `slot-video-${index}`,
                body: ''
            };
            currentBody = inlineRest ? [inlineRest] : [];
        } else if (stripped === '图片提示词' || stripped === '视频提示词'
                   || /^image\s*prompts?$/i.test(stripped) || /^video\s*prompts?$/i.test(stripped)
                   || stripped.startsWith('===') || stripped.startsWith('---') || stripped.startsWith('***') || stripped.startsWith('___')) {
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

/**
 * 将 prompt_block 文本解析为带分节与槽位的结构化数据，用于提示词页的卡片化展示。
 * 每个槽位具备专属的序号、标签与一键复制功能。
 * 若文本未匹配到任何槽位（如初始提示或自由文本），返回 null 以便无损降级为纯文本渲染。
 */
function parsePromptDisplaySections(text) {
    if (!text || typeof text !== 'string' || !text.trim()) return null;

    const raw = text
        .replace(/^\uFEFF/, '')
        .replace(/\r\n?/g, '\n')
        .replace(/[\u200B-\u200D\u2060\uFEFF]/g, '');
    const lines = raw.split('\n');
    const sections = [];
    let currentSection = null;
    let currentItem = null;
    let preamble = [];

    const ensureSection = (type, title) => {
        if (!currentSection || currentSection.type !== type) {
            if (currentItem) {
                currentItem.body = currentItem.bodyLines.join('\n').trim();
                currentSection.items.push(currentItem);
                currentItem = null;
            }
            if (currentSection && (currentSection.items.length > 0 || currentSection.notes.length > 0)) {
                sections.push(currentSection);
            }
            currentSection = {
                type: type, // 'image' | 'video'
                title: title || (type === 'image' ? '图片提示词' : '视频提示词'),
                notes: [],
                items: []
            };
        }
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();
        const stripped = trimmed.replace(/^[#\-*+>`\s]+/, '').replace(/[*_`]/g, '').trim();

        // 1. 分节标题判定，例如 "图片提示词" / "视频提示词" / "### 图片提示词" / "=== 视频提示词 ===" / "IMAGE PROMPTS"
        const secMatch = stripped.match(/^(?:图片提示词|视频提示词|image\s*prompts?|video\s*prompts?)/i);
        if (secMatch) {
            const secName = secMatch[0];
            const isImg = /图片|image/i.test(secName);
            ensureSection(isImg ? 'image' : 'video', isImg ? '图片提示词' : '视频提示词');
            continue;
        }

        // 2. 槽位起始判定，例如 "图片 1:", "#### 图片 8:", "图片 8（简介）[BRIDGE]:", "视频 12 [HERO]:", "视频 3（简介）[CUT]:"
        const slotMatch = stripped.match(/^(图片|视频|图像|画面|IMAGE|IMG|Frame|VIDEO|VID|Clip)\s*(?:第|#)?\s*(\d+)((?:\s*(?:[（\(].*?[）\)]|\[.*?\]))*)\s*[:：]\s*(.*)$/i);
        if (slotMatch) {
            if (currentItem) {
                currentItem.body = currentItem.bodyLines.join('\n').trim();
                if (!currentSection) ensureSection(currentItem.type, currentItem.type === 'image' ? '图片提示词' : '视频提示词');
                currentSection.items.push(currentItem);
            }
            const typeStr = slotMatch[1];
            const isImg = /^(?:图片|图像|画面|IMAGE|IMG|Frame)$/i.test(typeStr);
            const idx = parseInt(slotMatch[2], 10);
            const tags = slotMatch[3] || '';
            const summaryMatch = tags.match(/[（\(](.*?)[）\)]/);
            let summary = summaryMatch ? summaryMatch[1].trim() : '';
            const metaMatch = tags.match(/\[(.*?)\]/);
            let meta = metaMatch ? metaMatch[1].trim() : '';
            // 与 parsePromptBlock 同一套判定：卡片化展示与导入正规化必须认出同一批标记，
            // 否则同一份文本在"看"和"存"两条路上会得出不同的槽位语义。
            if (!meta && SLOT_META_TAG_RE.test(summary)) {
                meta = summary.toUpperCase().replace(/\s+/g, ' ');
                summary = '';
            }
            const inlineBody = slotMatch[4] || '';
            const stdLabel = isImg ? '图片' : '视频';

            if (!currentSection) {
                ensureSection(isImg ? 'image' : 'video', isImg ? '图片提示词' : '视频提示词');
            } else if (currentSection.type !== (isImg ? 'image' : 'video')) {
                ensureSection(isImg ? 'image' : 'video', isImg ? '图片提示词' : '视频提示词');
            }

            currentItem = {
                type: isImg ? 'image' : 'video',
                index: idx,
                summary: summary,
                meta: meta,
                label: `${stdLabel} ${idx}` + (summary ? `（${summary}）` : '') + (meta ? ` [${meta}]` : ''),
                shortLabel: `${stdLabel} ${idx}`,
                bodyLines: inlineBody.trim() ? [inlineBody.trim()] : []
            };
            continue;
        }

        // 3. 正文或备注累加
        if (currentItem) {
            currentItem.bodyLines.push(line);
        } else if (currentSection) {
            if (trimmed && !trimmed.startsWith('===') && !trimmed.startsWith('---') && !trimmed.startsWith('***')) {
                currentSection.notes.push(line);
            }
        } else {
            if (trimmed) {
                preamble.push(line);
            }
        }
    }

    if (currentItem) {
        currentItem.body = currentItem.bodyLines.join('\n').trim();
        if (!currentSection) ensureSection(currentItem.type, currentItem.type === 'image' ? '图片提示词' : '视频提示词');
        currentSection.items.push(currentItem);
    }
    if (currentSection && (currentSection.items.length > 0 || currentSection.notes.length > 0)) {
        sections.push(currentSection);
    }

    const totalItems = sections.reduce((sum, s) => sum + s.items.length, 0);
    if (totalItems === 0) return null;

    if (preamble.length && sections.length) {
        sections[0].notes = preamble.concat(sections[0].notes);
    }

    return sections;
}

/**
 * 渲染提示词展示区（#idea-prompt-block）：
 * 将提示词集以结构化卡片呈现，并为每条提示词配备独立的一键复制按钮。
 */
function renderPromptDisplay(promptText, targetEl) {
    const el = targetEl || document.getElementById('idea-prompt-block');
    if (!el) return;

    const raw = (promptText || '').trim();
    el.dataset.rawText = raw;

    if (!raw || raw === '（本次未返回提示词内容）') {
        el.className = 'prompt-pre prompt-empty';
        el.textContent = raw || '在左侧选择维度并点击「激发」，这里会输出经 gemini-veo / omni-restoration-composer skill 合成的完整图片 / 视频提示词集。';
        return;
    }

    const sections = parsePromptDisplaySections(raw);
    if (!sections || !sections.length) {
        // 无标准槽位时无损降级为普通代码块展示
        el.className = 'prompt-pre';
        el.textContent = raw;
        return;
    }

    el.className = 'prompt-display-container';
    el.innerHTML = '';

    sections.forEach(sec => {
        const secEl = document.createElement('div');
        secEl.className = `prompt-section prompt-section-${sec.type}`;

        // 分节标题栏
        const headerEl = document.createElement('div');
        headerEl.className = 'prompt-section-header';
        headerEl.title = `点击折叠/展开「${sec.title}」`;

        const titleWrap = document.createElement('div');
        titleWrap.className = 'prompt-section-title';
        const icon = sec.type === 'image' ? '🖼️' : (sec.type === 'video' ? '🎬' : '📋');
        const unit = sec.type === 'image' ? '拍' : '段';

        const foldIcon = document.createElement('span');
        foldIcon.className = 'prompt-section-fold-icon';
        foldIcon.title = '折叠/展开本节';
        foldIcon.innerHTML = `
            <svg class="prompt-fold-chevron" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
        `;

        const iconSpan = document.createElement('span');
        iconSpan.className = 'prompt-section-icon';
        iconSpan.textContent = icon;

        const textSpan = document.createElement('span');
        textSpan.className = 'prompt-section-text';
        textSpan.textContent = sec.title;

        const countSpan = document.createElement('span');
        countSpan.className = 'prompt-section-count';
        countSpan.textContent = `共 ${sec.items.length} ${unit}`;

        const foldBadge = document.createElement('span');
        foldBadge.className = 'prompt-section-fold-badge';
        foldBadge.textContent = '已折叠';
        foldBadge.hidden = true;

        titleWrap.append(foldIcon, iconSpan, textSpan, countSpan, foldBadge);

        const secActionsWrap = document.createElement('div');
        secActionsWrap.className = 'prompt-section-actions';

        // 折叠/展开本节所有条目
        const secFoldItemsBtn = document.createElement('button');
        secFoldItemsBtn.type = 'button';
        secFoldItemsBtn.className = 'action-btn text-btn mini-btn prompt-section-fold-items-btn';
        secFoldItemsBtn.title = `折叠/展开本节所有${sec.type === 'image' ? '图片' : '视频'}条目`;
        secFoldItemsBtn.innerHTML = `
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="4 14 10 14 10 20"></polyline>
                <polyline points="20 10 14 10 14 4"></polyline>
                <line x1="14" y1="10" x2="21" y2="3"></line>
                <line x1="3" y1="21" x2="10" y2="14"></line>
            </svg>
            <span>折叠条目</span>
        `;

        const secCopyBtn = document.createElement('button');
        secCopyBtn.type = 'button';
        secCopyBtn.className = 'action-btn text-btn mini-btn prompt-section-copy-btn';
        secCopyBtn.title = `复制全部${sec.title}`;
        secCopyBtn.innerHTML = `
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            <span>复制全部${sec.type === 'image' ? '图片' : '视频'}</span>
        `;

        secCopyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const secText = sec.items.map(it => `${it.label}:\n${it.body}`).join('\n\n');
            const doCopy = typeof copyText === 'function' ? copyText(secText) : navigator.clipboard.writeText(secText);
            Promise.resolve(doCopy).then(() => {
                const span = secCopyBtn.querySelector('span');
                const orig = span ? span.textContent : '复制全部';
                if (span) span.textContent = '✓ 已复制全部';
                secCopyBtn.classList.add('copied');
                if (typeof showToast === 'function') {
                    showToast(`已复制全部「${sec.title}」到剪贴板`, 'success');
                }
                setTimeout(() => {
                    if (span) span.textContent = orig;
                    secCopyBtn.classList.remove('copied');
                }, 1500);
            }).catch(err => {
                if (typeof showToast === 'function') {
                    showToast('复制失败，请手动复制', 'error');
                }
            });
        });

        secActionsWrap.append(secFoldItemsBtn, secCopyBtn);
        headerEl.append(titleWrap, secActionsWrap);
        secEl.appendChild(headerEl);

        if (sec.notes && sec.notes.length) {
            const notesEl = document.createElement('div');
            notesEl.className = 'prompt-section-notes';
            notesEl.textContent = sec.notes.join('\n');
            secEl.appendChild(notesEl);
        }

        // 提示词列表
        const listEl = document.createElement('div');
        listEl.className = 'prompt-items-list';

        // 绑定分节折叠/展开
        headerEl.addEventListener('click', () => {
            const isCollapsed = secEl.classList.toggle('is-collapsed');
            const badge = headerEl.querySelector('.prompt-section-fold-badge');
            if (badge) badge.hidden = !isCollapsed;
            headerEl.title = isCollapsed ? `点击展开「${sec.title}」` : `点击折叠/展开「${sec.title}」`;
        });

        secFoldItemsBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const cards = listEl.querySelectorAll('.prompt-item-card');
            const hasExpanded = Array.from(cards).some(c => !c.classList.contains('is-collapsed'));
            cards.forEach(c => {
                if (hasExpanded) {
                    c.classList.add('is-collapsed');
                } else {
                    c.classList.remove('is-collapsed');
                }
            });
            const span = secFoldItemsBtn.querySelector('span');
            if (span) span.textContent = hasExpanded ? '展开条目' : '折叠条目';
        });

        sec.items.forEach(item => {
            const cardEl = document.createElement('div');
            cardEl.className = `prompt-item-card prompt-item-${item.type}`;
            cardEl.dataset.type = item.type;
            cardEl.dataset.index = String(item.index);

            // 卡片头部
            const itemHeader = document.createElement('div');
            itemHeader.className = 'prompt-item-header';
            itemHeader.title = '点击折叠/展开提示词正文';

            const metaWrap = document.createElement('div');
            metaWrap.className = 'prompt-item-meta';

            const foldIcon = document.createElement('span');
            foldIcon.className = 'prompt-item-fold-icon';
            foldIcon.title = '折叠/展开';
            foldIcon.innerHTML = `
                <svg class="fold-chevron-mini" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
            `;
            metaWrap.appendChild(foldIcon);

            const badge = document.createElement('span');
            badge.className = `prompt-item-badge badge-${item.type}`;
            badge.textContent = item.shortLabel;
            metaWrap.appendChild(badge);

            if (item.summary) {
                const summaryEl = document.createElement('span');
                summaryEl.className = 'prompt-item-summary';
                summaryEl.textContent = `（${item.summary}）`;
                metaWrap.appendChild(summaryEl);
            }

            if (item.meta) {
                const tag = document.createElement('span');
                const mLower = item.meta.toLowerCase();
                const tagCls = mLower === 'cut' ? 'tag-cut' : (mLower === 'hero' ? 'tag-hero' : 'tag-meta');
                tag.className = `prompt-item-tag ${tagCls}`;
                tag.textContent = item.meta;
                metaWrap.appendChild(tag);
            }

            // 卡片头部操作按钮组
            const actionsWrap = document.createElement('div');
            actionsWrap.className = 'prompt-item-actions';

            // 1. 定位到对应画面帧/视频卡片按钮
            const jumpBtn = document.createElement('button');
            jumpBtn.type = 'button';
            jumpBtn.className = 'action-btn text-btn mini-btn prompt-item-jump-btn';
            jumpBtn.title = `定位到对应的${item.type === 'image' ? '画面帧' : '视频'}卡片 ↗`;
            jumpBtn.innerHTML = `
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polygon points="12 8 8 12 12 16 12 8"></polygon>
                    <line x1="16" y1="12" x2="12" y2="12"></line>
                </svg>
                <span>定位</span>
            `;
            jumpBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                jumpFromPromptToMedia(item.type, item.index);
            });

            // 2. 单条就地编辑按钮
            const editBtn = document.createElement('button');
            editBtn.type = 'button';
            editBtn.className = 'action-btn text-btn mini-btn prompt-item-edit-btn';
            editBtn.title = `就地编辑「${item.label}」提示词正文`;
            editBtn.innerHTML = `
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 20h9"></path>
                    <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
                </svg>
                <span>编辑</span>
            `;
            editBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                enterInlinePromptEdit(cardEl, item);
            });

            // 3. 单条提示词一键复制按钮
            const copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'action-btn text-btn mini-btn prompt-item-copy-btn';
            copyBtn.title = `一键复制「${item.label}」提示词正文`;
            copyBtn.innerHTML = `
                <svg class="copy-icon" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                </svg>
                <span class="copy-btn-label">复制</span>
            `;

            copyBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                const doCopy = typeof copyText === 'function' ? copyText(item.body) : navigator.clipboard.writeText(item.body);
                Promise.resolve(doCopy).then(() => {
                    const labelSpan = copyBtn.querySelector('.copy-btn-label');
                    const origText = labelSpan ? labelSpan.textContent : '复制';
                    if (labelSpan) labelSpan.textContent = '✓ 已复制';
                    copyBtn.classList.add('copied');
                    if (typeof showToast === 'function') {
                        showToast(`已复制「${item.label}」提示词`, 'success');
                    }
                    setTimeout(() => {
                        if (labelSpan) labelSpan.textContent = origText;
                        copyBtn.classList.remove('copied');
                    }, 1500);
                }).catch(err => {
                    if (typeof showToast === 'function') {
                        showToast('复制失败，请手动复制', 'error');
                    }
                });
            });

            actionsWrap.append(jumpBtn, editBtn, copyBtn);
            itemHeader.append(metaWrap, actionsWrap);

            // 卡片折叠态预览条
            const previewEl = document.createElement('div');
            previewEl.className = 'prompt-item-preview';
            const cleanSnippet = (item.body || '').replace(/\s+/g, ' ').trim();
            previewEl.textContent = cleanSnippet ? (cleanSnippet.length > 130 ? cleanSnippet.slice(0, 130) + '...' : cleanSnippet) : '（空提示词）';
            previewEl.title = '点击展开提示词正文';

            // 点击卡片头部或预览条折叠/展开
            itemHeader.addEventListener('click', () => {
                cardEl.classList.toggle('is-collapsed');
            });
            previewEl.addEventListener('click', () => {
                cardEl.classList.remove('is-collapsed');
            });

            // 卡片正文
            const bodyEl = document.createElement('div');
            bodyEl.className = 'prompt-item-body';
            bodyEl.textContent = item.body;

            cardEl.append(itemHeader, previewEl, bodyEl);
            listEl.appendChild(cardEl);
        });

        secEl.appendChild(listEl);
        el.appendChild(secEl);
    });
}

/**
 * 全局一键全部折叠或全部展开提示词
 */
function toggleFoldAllPrompts(targetContainer) {
    const container = targetContainer || document.getElementById('idea-prompt-block');
    if (!container) return;
    const sections = container.querySelectorAll ? container.querySelectorAll('.prompt-section') : [];
    const cards = container.querySelectorAll ? container.querySelectorAll('.prompt-item-card') : [];
    if (!sections.length && !cards.length) return;

    // 若有任意分节或卡片处于展开态，则全部折叠；若已全部折叠，则全部展开
    const hasExpandedSec = Array.from(sections).some(s => !s.classList.contains('is-collapsed'));
    const hasExpandedCard = Array.from(cards).some(c => !c.classList.contains('is-collapsed'));
    const shouldCollapse = hasExpandedSec || hasExpandedCard;

    sections.forEach(s => {
        if (shouldCollapse) {
            s.classList.add('is-collapsed');
            const badge = s.querySelector ? s.querySelector('.prompt-section-fold-badge') : null;
            if (badge) badge.hidden = false;
        } else {
            s.classList.remove('is-collapsed');
            const badge = s.querySelector ? s.querySelector('.prompt-section-fold-badge') : null;
            if (badge) badge.hidden = true;
        }
    });

    cards.forEach(c => {
        if (shouldCollapse) {
            c.classList.add('is-collapsed');
        } else {
            c.classList.remove('is-collapsed');
        }
    });

    const btn = document.getElementById('toggle-fold-all-prompts-btn');
    if (btn) {
        const span = btn.querySelector ? btn.querySelector('span') : null;
        if (span) span.textContent = shouldCollapse ? '全部展开' : '全部折叠';
        btn.title = shouldCollapse ? '一键展开全部提示词' : '一键全部折叠或展开图片与视频提示词';
    }

    if (typeof showToast === 'function') {
        showToast(shouldCollapse ? '已折叠全部图片与视频提示词' : '已展开全部提示词', 'info', 1500);
    }
}

/**
 * 替换提示词块中指定单个槽位的正文内容，保持其它槽位与排版结构不变
 */
function replaceSinglePromptSlotBody(fullBlock, type, index, newBody) {
    const raw = String(fullBlock || '');
    const sections = parsePromptDisplaySections(raw);
    if (!sections || !sections.length) {
        return raw;
    }

    let found = false;
    sections.forEach(sec => {
        sec.items.forEach(it => {
            if (it.type === type && Number(it.index) === Number(index)) {
                it.body = String(newBody || '').trim();
                found = true;
            }
        });
    });

    if (!found) return raw;

    const out = [];
    sections.forEach(sec => {
        out.push(sec.title, '');
        if (sec.notes && sec.notes.length) {
            out.push(sec.notes.join('\n'), '');
        }
        sec.items.forEach(it => {
            out.push(`${it.label}:`, it.body, '');
        });
    });

    return out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

/**
 * 从提示词卡片平滑跳转并高亮对应的画面帧 / 视频卡片
 */
function jumpFromPromptToMedia(type, index) {
    if (typeof switchMainTab === 'function') switchMainTab('results');
    if (typeof switchTab === 'function') {
        switchTab('overview');
    }

    const cardId = type === 'image' ? `frame-slot-${index}` : `video-slot-${index}`;
    setTimeout(() => {
        const target = document.getElementById(cardId);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.classList.add('highlight-slot-pulse');
            setTimeout(() => target.classList.remove('highlight-slot-pulse'), 2200);
            if (typeof showToast === 'function') {
                showToast(`已定位到 ${type === 'image' ? '画面帧' : '视频'} ${index}`, 'info', 2000);
            }
        } else {
            if (typeof showToast === 'function') {
                showToast(`未找到 ${type === 'image' ? '画面帧' : '视频'} ${index} 的卡片`, 'warning');
            }
        }
    }, 100);
}

/**
 * 从画面帧 / 视频卡片跳转并高亮对应的提示词卡片
 */
function jumpFromMediaToPrompt(type, seq) {
    if (typeof switchMainTab === 'function') switchMainTab('results');
    if (typeof switchTab === 'function') {
        switchTab('prompts');
    }

    setTimeout(() => {
        const sel = `.prompt-item-card[data-type="${type}"][data-index="${seq}"]`;
        const target = document.querySelector(sel);
        if (target) {
            // 若所在分节或卡片处于折叠态，自动展开以供查看
            const parentSection = target.closest ? target.closest('.prompt-section') : null;
            if (parentSection && parentSection.classList.contains('is-collapsed')) {
                parentSection.classList.remove('is-collapsed');
                const badge = parentSection.querySelector ? parentSection.querySelector('.prompt-section-fold-badge') : null;
                if (badge) badge.hidden = true;
            }
            if (target.classList.contains('is-collapsed')) {
                target.classList.remove('is-collapsed');
            }
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.classList.add('highlight-prompt-pulse');
            setTimeout(() => target.classList.remove('highlight-prompt-pulse'), 2500);
            if (typeof showToast === 'function') {
                showToast(`已定位到「${type === 'image' ? '图片' : '视频'} ${seq}」提示词卡片`, 'info', 2000);
            }
        }
    }, 100);
}

/**
 * 进入提示词单条就地编辑模式
 */
function enterInlinePromptEdit(cardEl, item) {
    if (!cardEl || !item) return;
    if (cardEl.classList.contains('is-inline-editing')) return;

    if (typeof currentIdea !== 'undefined' && currentIdea && typeof isIdeaTaskActive === 'function'
            && (isIdeaTaskActive(currentIdea.id, 'frames') || isIdeaTaskActive(currentIdea.id, 'videos'))) {
        if (typeof showToast === 'function') {
            showToast('该创意的帧/视频序列正在生成中，等它结束后再修改提示词', 'error');
        }
        return;
    }

    // 若当前卡片处于折叠态，展开以供编辑
    cardEl.classList.remove('is-collapsed');
    cardEl.classList.add('is-inline-editing');
    const origBody = item.body;

    const bodyEl = cardEl.querySelector('.prompt-item-body');
    if (!bodyEl) return;

    const formEl = document.createElement('div');
    formEl.className = 'prompt-inline-edit-form';
    formEl.innerHTML = `
        <textarea class="prompt-inline-textarea" spellcheck="false" placeholder="在此编辑提示词正文..."></textarea>
        <div class="prompt-inline-actions">
            <span class="prompt-inline-hint">快捷键：⌘/Ctrl+Enter 保存，Esc 取消</span>
            <div class="prompt-inline-buttons">
                <button type="button" class="action-btn text-btn mini-btn prompt-inline-cancel-btn">✖ 取消</button>
                <button type="button" class="action-btn text-btn mini-btn primary prompt-inline-save-btn">💾 保存</button>
            </div>
        </div>
    `;

    const textarea = formEl.querySelector('.prompt-inline-textarea');
    textarea.value = origBody;

    const autoResize = () => {
        textarea.style.height = 'auto';
        textarea.style.height = Math.max(76, textarea.scrollHeight + 4) + 'px';
    };
    textarea.addEventListener('input', autoResize);

    const cancelEdit = () => {
        cardEl.classList.remove('is-inline-editing');
        formEl.remove();
        bodyEl.style.display = '';
    };

    const saveEdit = async () => {
        const newBody = textarea.value.trim();
        if (newBody === origBody.trim()) {
            cancelEdit();
            if (typeof showToast === 'function') showToast('提示词未改动', 'info');
            return;
        }
        if (!newBody) {
            if (typeof showToast === 'function') showToast('提示词正文不能为空', 'error');
            return;
        }

        const ownerIdea = (typeof currentIdea !== 'undefined') ? currentIdea : null;
        if (!ownerIdea || !ownerIdea.prompt_block) return;

        const updatedBlock = replaceSinglePromptSlotBody(ownerIdea.prompt_block, item.type, item.index, newBody);

        textarea.disabled = true;
        const saveBtn = formEl.querySelector('.prompt-inline-save-btn');
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '保存中...';
        }

        const ok = await mutateSlot({
            what: `修改${item.shortLabel}提示词`,
            ownerIdea, scope: 'both', requirePrompt: true,
            request: () => slotPostJson('/api/edit_prompts', {
                title: getIdeaSaveTitle(ownerIdea),
                prompt_block: updatedBlock,
                prev_prompt_block: ownerIdea.prompt_block || '',
            }),
            beforeApply: async (d) => {
                await applyPromptBlockToIdea(ownerIdea, d.prompt_block, d.prompt_slots, true);
            },
            patch: (d) => ({ frames: d.frames, videos: d.videos, dropMerged: true }),
            success: (d) => `已保存「${item.shortLabel}」提示词修改。`,
            failure: (e) => `保存提示词失败: ${e.message}`,
        });

        if (ok) {
            if (typeof recordPromptHistory === 'function') {
                recordPromptHistory(ownerIdea.id, updatedBlock, `单条就地编辑 ${item.shortLabel}`);
            }
            renderPromptDisplay(updatedBlock);
        } else {
            textarea.disabled = false;
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 保存';
            }
        }
    };

    formEl.querySelector('.prompt-inline-cancel-btn').addEventListener('click', cancelEdit);
    formEl.querySelector('.prompt-inline-save-btn').addEventListener('click', saveEdit);

    textarea.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            saveEdit();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelEdit();
        }
    });

    bodyEl.style.display = 'none';
    cardEl.appendChild(formEl);
    autoResize();
    textarea.focus();
}

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

// ── 灵感卡片链路已整体移除（2026-08-19）────────────────────────────────
// 原先这里是「激发维度」页的灵感卡：fetchIdeationCover / renderIdeationTrendPanel /
// ideaBeatOutline / 节拍简介弹窗 / renderIdeationCards / selectIdeationCard /
// composeIdeationCard 等一整套，入口是那一页的卡片网格与「激发」按钮。
// 那一页下线后整条链路没有任何调用方（app.js 的 loadIdeationCards 一并移除），
// 留着只会让人误以为还有这条路。
// 后端 /api/ideate 与 prompt_pipeline.run_ideate（含 remix_seed）仍然完好，
// 将来要重建卡片区，从那里接回来即可。
