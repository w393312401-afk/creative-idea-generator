// --- prompt_pipeline.js ---

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
            const summary = summaryMatch ? summaryMatch[1].trim() : '';
            const metaMatch = tags.match(/\[(.*?)\]/);
            const meta = metaMatch ? metaMatch[1].trim() : '';
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
            const summary = summaryMatch ? summaryMatch[1].trim() : '';
            const metaMatch = tags.match(/\[(.*?)\]/);
            const meta = metaMatch ? metaMatch[1].trim() : '';
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
// (末条是最终 reward 揭示)。富字段存在时一并透传；旧形态（纯字符串）仍自动兼容。
function ideaBeatOutline(idea) {
    if (!Array.isArray(idea && idea.beat_outline)) return [];
    return idea.beat_outline
        .map(s => {
            if (typeof s === 'object' && s !== null) {
                const text = String(s.text == null ? '' : s.text).trim();
                const mat = Array.isArray(s.mat)
                    ? s.mat.map(x => String(x == null ? '' : x).trim()).filter(Boolean)
                    : [];
                return text ? {
                    op: s.op || null,
                    text,
                    en: String(s.en == null ? '' : s.en).trim() || null,
                    mat,
                    zone: String(s.zone == null ? '' : s.zone).trim() || null,
                    scope: String(s.scope == null ? '' : s.scope).trim() || null,
                    trace: String(s.trace == null ? '' : s.trace).trim() || null
                } : null;
            }
            const text = String(s == null ? '' : s).trim();
            return text ? { op: null, text } : null;
        })
        .filter(Boolean);
}
// 纯文本辅助（供显示用，如卡片标签计数）
function ideaBeatOutlineTexts(idea) {
    return ideaBeatOutline(idea).map(e => e.text);
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
        // 2026-08-07：清单一比一还原（默认行为，不是可选模式）——最终拍数恒等于这里
        // 列出的条目数，逐条一一对应，不再有"下界强制、正式合成时按硬规则改写/合并/
        // 增删"的浮动空间（旧文案对应的正是用户反馈的"提示词不按节拍来"：11 条清单
        // 合成出 12 张图，其中只有 5 张真正对应清单内容）。
        info.textContent = `${recBeats}共 ${beats.length} 拍（含末条 reward 揭示），与下方清单逐条一一对应：`
            + '正式合成时每一拍对应且仅对应一条，不增、不减、不合并、不拆分。';
    }

    const list = document.getElementById('beat-outline-modal-list');
    if (list) {
        list.innerHTML = '';
        beats.forEach((entry, i) => {
            const li = document.createElement('li');
            // 末条按约定是 reward/揭示拍,单独标色以便一眼看到成片落点
            if (i === beats.length - 1) li.className = 'reward';
            const text = typeof entry === 'object' ? entry.text : String(entry);
            const op = typeof entry === 'object' && entry.op ? entry.op : null;
            const main = document.createElement('div');
            main.textContent = op ? `[${op}] ${text}` : text;
            li.appendChild(main);
            const scopeLabels = { large: '整体', default: '常规', small: '局部' };
            const metaParts = [];
            if (entry.zone) metaParts.push(entry.zone);
            if (Array.isArray(entry.mat) && entry.mat.length) metaParts.push(entry.mat.join(', '));
            if (entry.scope) metaParts.push(scopeLabels[entry.scope] || entry.scope);
            if (metaParts.length) {
                const meta = document.createElement('div');
                meta.className = 'beat-outline-entry-meta';
                meta.textContent = metaParts.join(' · ');
                li.appendChild(meta);
            }
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
                ${idea.degraded ? '<span class="ideation-card-tag degraded" title="模型生成失败后的静态兜底卡，不计入正常随机结果">降级兜底</span>' : ''}
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
                ${(idea.salvage_zh || idea.salvage_en)
                    ? `<div class="ideation-card-salvage" title="从这个壳子上拆下来、再回到室内变成别的东西的那一件原生旧构件——改造类的 DIY 内核">♻️ 旧物再生: ${idea.salvage_zh || idea.salvage_en}</div>`
                    : ''}
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
        if (idea.degraded) {
            card.classList.add('degraded-card');
        }
        
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
        // pasted directly into a real gemini-veo-restoration-composer skill chat session.
        const copyBtn = card.querySelector('.copy-action-btn');
        copyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyText(idea.input_str).then(() => {
                showToast(`已复制选题："${idea.title}"，可直接粘贴到 gemini-veo-restoration-composer 技能会话中使用`, 'success');
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
            twist_zh: idea.twist_zh || null,
            // 旧物再生申报随种子一起入账：它是这条选题「为什么算改造而不是雕刻」的
            // 证据，合成侧和后续复盘都要能查到（见 ideation_salvage_violations）
            salvage: idea.salvage_en || null,
            salvage_zh: idea.salvage_zh || null
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
    // 用户不知道该信哪个。2026-08-07：有节拍简介的卡片改为一比一还原——合成时的
    // 实际拍数恒等于清单条目数，节拍滑杆/规划模式对这类卡片不再生效，只在没有
    // 清单的手动路径上才真正起作用，这里如实说明，别让滑杆看着像还有用。
    const _hasOutline = ideaBeatOutline(idea).length > 0;
    showToast(_hasOutline
        ? `已载入灵感: ${idea.title}。这张卡有节拍简介，合成时按清单一比一还原（共 ${ideaBeatOutline(idea).length} 拍），节拍滑杆/规划模式不生效`
        : beatsKeptByUser
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
        // P1-C: 保留结构化 {op, text} 对象原样透传给后端
        beat_outline: ideaBeatOutline(idea),
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
                twist_zh: idea.twist_zh || null,
                salvage: idea.salvage_en || null,
                salvage_zh: idea.salvage_zh || null
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
