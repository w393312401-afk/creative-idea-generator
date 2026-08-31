/* =====================================================================
   槽位卡片渲染器 + 网格事件委托
   ---------------------------------------------------------------------
   方案与取舍见 docs/spark_result_slots_plan.md。

   本文件是**全仓唯一**写槽位卡片 innerHTML 的地方。在此之前同一张卡片有
   十几处各写各的模板（media_renderer.js 6 处、api_client.js 4 处 +
   6 处转圈占位、app.js 3 处），两套已经真的分叉：renderVideoSlotDone 画出来的
   卡片没有操作按钮、没有 IMG N ➔ IMG N+1 标签、不认英雄展示、不设 draggable。

   两条设计红线：
   1. **不在卡片上绑 click 监听**。所有点击走网格级委托（initSlotGrids），
      于是 busy 只影响 disabled 属性——浏览器本就不派发 disabled 元素的 click，
      结构上不可能再出现"摘掉 disabled 却没补绑监听器"的死按钮
      （2026-07-21 / 07-22 两次实机事故的根因，见 git 历史与 media_renderer 注释）。
   2. **不写行内样式**。全部靠 .slot-* class，样式收口在 skill-output.css，
      移动端那段 !important 补丁因此可以退役。

   用户文本（问题描述、失败原因、徽标提示）一律用 textContent / title 属性赋值，
   不再手工 .replace(/"/g,'&quot;') 拼进 HTML。
   ===================================================================== */

/** 卡片外壳的 class：既有的 frame-card 系列全部保留（CSS 与移动端补丁依赖它们）。 */
function slotCardClass(state) {
    if (state.kind === 'pending') return 'slot-card frame-card placeholder-frame-card';
    if (state.kind === 'missing' || state.kind === 'failed' || state.kind === 'cut') {
        return 'slot-card frame-card video-failed-card';
    }
    return ('slot-card frame-card ' + (state.cardCls || '')).trim();
}

function slotActionsHtml(state) {
    if (!state.actions || !state.actions.length) return '';
    const wrapCls = state.type === 'video' ? 'video-card-actions' : 'frame-card-actions';
    const btns = state.actions.map(a =>
        `<button type="button" class="action-btn text-btn mini-btn slot-action-btn ${a.cls}"`
        + ` data-act="${a.act}" data-variant="${a.variant}"`
        + (state.type === 'video' ? ` data-slot="${state.seq}"` : ` data-seq="${state.seq}"`)
        + (a.disabled ? ' disabled' : '') + `>${a.label}</button>`
    ).join('');
    return `<div class="slot-actions ${wrapCls}">${btns}</div>`;
}

/**
 * 点开就有明细可看的徽标：这几类背后一定挂着 review_issues 或 manual_issue。
 * 「未审查」「降级」这些没有条目可列，点了会是一个空弹层，不给点。
 */
const ISSUE_DETAIL_BADGES = ['review-failed', 'manual-flagged', 'continuity-failed'];

function slotBadgesHtml(state) {
    if (!state.badges || !state.badges.length) return '';
    const items = state.badges.map(b =>
        `<span class="slot-badge ${b.cls}" data-badge="${b.id}"></span>`
    ).join('');
    return `<div class="slot-badges">${items}</div>`;
}

/**
 * 把一张卡片画成 state 描述的样子。cardEl 的身份（id、拖拽监听）不变，
 * 只换内部结构——拖拽监听绑在卡片元素本身，不会被这里的 innerHTML 冲掉。
 */
function renderSlotCard(cardEl, state) {
    if (!cardEl || !state) return;

    cardEl.className = slotCardClass(state);
    cardEl.dataset.kind = state.kind;
    cardEl.dataset.type = state.type;
    cardEl.dataset.seq = String(state.seq);
    cardEl.dataset.url = state.url || '';
    // 工具条按这些 data-* 做筛选与计数，不必自己再推一遍状态（见 js/slot_toolbar.js）。
    // fixable＝这一格有待修复的问题（审查未过或人工标记），也就是画着「修复此帧
    // 问题」的那些格子；工具条的「全部修复」按它取帧，与卡片按钮同源。
    cardEl.dataset.badges = String((state.badges || []).length);
    cardEl.dataset.issueBadges = String((state.badges || []).filter(b => b.isIssue !== false).length);
    cardEl.dataset.fixable = (state.flags && state.flags.fixable) ? '1' : '0';
    cardEl.dataset.promptDirty = (state.badges || []).some(b => b.id === 'prompt-dirty') ? '1' : '0';
    // 拖出能力随状态走：只有真的有内容的格子能当换位的源。
    // （旧实现只在首次 enable 时按当时有没有内容设一次，重渲后就失灵了）
    cardEl.draggable = !!state.draggable;
    cardEl.title = '';

    let body;
    if (state.kind === 'pending') {
        body = `<div class="frame-placeholder-spinner">`
            + `<div class="cover-spinner slot-spinner"></div></div>`
            + `<span class="slot-label"></span>`;
    } else if (state.kind === 'cut') {
        body = `<div class="video-failed-placeholder">`
            + `<span class="error-icon">✂️</span>`
            + `<span class="error-text"></span></div>`
            + `<span class="slot-label"></span>`;
    } else if (state.kind === 'missing' || state.kind === 'failed') {
        body = `<div class="video-failed-placeholder">`
            + `<span class="error-icon">⚠️</span>`
            + `<span class="error-text"></span>`
            + slotActionsHtml(state)
            + `</div><span class="slot-label"></span>`;
    } else if (state.type === 'video') {
        body = `<div class="video-preview-wrapper">`
            + `<video loop muted playsinline preload="metadata"></video>`
            + `<div class="video-play-overlay"><span class="play-icon">▶</span></div>`
            + slotBadgesHtml(state)
            + slotActionsHtml(state)
            + `</div><span class="slot-label"></span>`;
    } else {
        body = `<img alt="Frame ${state.seq}" loading="lazy" draggable="false">`
            + slotBadgesHtml(state)
            + slotActionsHtml(state)
            + `<span class="slot-label"></span>`;
    }
    // 多选勾选框：等待中的格子还没有内容可操作，不给选
    if (state.kind !== 'pending') {
        body += `<label class="slot-select" title="选中这一拍，用于批量重试/删除（支持鼠标拖拽框选，按住 Shift 累加选/连选）">`
            + `<input type="checkbox" class="slot-select-box"></label>`;
    }
    cardEl.innerHTML = body;

    // ── 文本与提示一律用属性赋值，不拼进 HTML ──
    const labelEl = cardEl.querySelector('.slot-label');
    if (labelEl) {
        labelEl.textContent = state.label || '';
        labelEl.title = '点击跳转并高亮对应的提示词卡片 📝';
    }

    const errEl = cardEl.querySelector('.error-text');
    if (errEl) {
        errEl.textContent = state.statusText || '';
        errEl.title = state.title || state.statusText || '';
    }

    (state.badges || []).forEach(b => {
        const el = cardEl.querySelector(`.slot-badge[data-badge="${b.id}"]`);
        if (!el) return;
        el.textContent = b.text;
        if (b.tip) el.title = b.tip;
        // 能点开详情的徽标要看得出来能点（光标+下划线由 CSS 给）
        if (state.type !== 'video' && ISSUE_DETAIL_BADGES.includes(b.id)) {
            el.classList.add('is-clickable');
            el.title = (b.tip ? b.tip + '\n' : '') + '点击查看全部违规明细（可复制）';
        }
    });

    (state.actions || []).forEach(a => {
        const el = cardEl.querySelector(`.slot-action-btn[data-act="${a.act}"]`);
        if (!el) return;
        if (a.title) el.title = a.title;
        // 网格级的忙态开关（setSlotGridButtonsBusy）不重新推导状态、只改 disabled
        // 与 title，解禁时要有地方把"不忙时该说什么"读回来——否则跑完一轮之后
        // 每枚按钮的悬浮说明都变成空的。
        el.dataset.idleTitle = a.idleTitle || '';
    });

    if (state.kind === 'ready') {
        cardEl.title = state.title || '';
        if (state.type === 'video') {
            const v = cardEl.querySelector('video');
            if (v) v.src = cacheBustedUrl(state.url);
        } else {
            const img = cardEl.querySelector('img');
            if (img) {
                // 磁盘文件已被删、本地清单还没同步时别让卡片挂着死图/缓存里的旧图，
                // 就地降级成"未生成"占位（同一渲染器，不再另写一套模板）
                img.onerror = () => renderSlotCard(cardEl, frameSlotState(null, {
                    seq: state.seq, busy: slotGridIsBusy('image'),
                }));
                safeSetImageSrc(img, state.url, false);
            }
        }
    }
}

/** 按 id 直接改画某个槽位（生成过程中的实时回调用）。 */
function renderSlotById(type, seq, state) {
    const el = document.getElementById(`${type === 'video' ? 'video' : 'frame'}-slot-${seq}`);
    if (el) renderSlotCard(el, state);
    return el;
}

/** 转圈占位：取代散在六处的同款 innerHTML（重试中/修复中/上传中/等待中……）。 */
function renderSlotPending(type, seq, text) {
    return renderSlotById(type, seq, slotPendingState(type, seq, text));
}

function slotGridIsBusy(type) {
    // 忙态标在各自的网格上（setFrameGridButtonsBusy / setVideoGridButtonsBusy），
    // 合并视图下卡片虽然画在 #beats-grid 里，忙态仍然由原网格代表这一类
    const grid = document.getElementById(type === 'video' ? 'videos-grid' : 'frames-grid');
    return !!(grid && grid.classList.contains('is-busy'));
}

// =====================================================================
// 网格事件委托：两个网格各挂一套监听，卡片本身不再绑 click
// =====================================================================

const SLOT_ACTION_HANDLERS = {
    'retry-frame': seq => retrySingleFrame(seq),
    'fix-frame': async seq => {
        const frames = (typeof currentIdea !== 'undefined' && currentIdea
            && currentIdea.frameRun && currentIdea.frameRun.frames) || [];
        const maxSeq = frames.reduce((m, f) => Math.max(m, f.sequence || 0), 0);
        let cascade = false;
        if (seq < maxSeq && typeof customConfirm === 'function') {
            cascade = await customConfirm(
                `修复 IMG ${String(seq).padStart(3, '0')} 属于关键硬装/透视修复。<br><br>`
                + '<b>是否连带向后重渲后续下游帧？</b><br>'
                + '· <b>连带重渲 (推荐)</b>：以修复后的新画面为基底向后链式重渲后续各帧，彻底消除下游血统断层（Stale）。<br>'
                + '· <b>仅修当前帧</b>：仅重渲当前帧，后续各帧保持原样。',
                '连带重渲下游 (推荐)',
                '仅修当前帧'
            );
        }
        return fixFrameIssue(seq, undefined, cascade);
    },
    'undo-fix': seq => undoFrameFix(seq),
    'adopt-fix': seq => adoptRejectedFix(seq),
    'describe-frame': seq => describeFrameIssue(seq, currentFrameManualIssue(seq)),
    'compare-benchmark': seq => {
        if (typeof openBenchmarkCompare === 'function') {
            openBenchmarkCompare({
                idea: typeof currentIdea !== 'undefined' ? currentIdea : null,
                seq,
            });
        } else if (typeof openCollageViewer === 'function') {
            openCollageViewer({
                idea: typeof currentIdea !== 'undefined' ? currentIdea : null,
                initialMode: 'compare',
                compareType: 'benchmark',
                initialFrameSeq: seq,
            });
        }
    },
    'view-candidates': seq => {
        if (typeof openCandidateSelectionModal === 'function') {
            openCandidateSelectionModal(seq);
        } else if (typeof window !== 'undefined' && typeof window.openCandidateSelectionModal === 'function') {
            window.openCandidateSelectionModal(seq);
        }
    },
    'upload-frame': seq => triggerFrameUpload(seq),
    'retry-video': seq => retrySingleVideo(seq),
    'upload-video': seq => triggerVideoUpload(seq),
    'delete-slot': seq => deleteSlotBeat(seq),
    'jump-prompt': (seq, type) => {
        if (typeof jumpFromMediaToPrompt === 'function') {
            jumpFromMediaToPrompt(type || 'image', seq);
        }
    },
};

/** 点「改描述」时现取当前已记录的问题描述（渲染时刻的闭包会过期）。 */
function currentFrameManualIssue(seq) {
    const frames = (typeof currentIdea !== 'undefined' && currentIdea
        && currentIdea.frameRun && currentIdea.frameRun.frames) || [];
    const f = frames.find(x => x.sequence === seq);
    return (f && typeof f.manual_issue === 'string') ? f.manual_issue : '';
}

/**
 * 点开大图/大片：清单在点击时刻现取，因此换位、重试、删除之后打开的一定是
 * 当前内容（旧实现用的是渲染时刻的闭包快照）。
 */
function openSlotLightbox(type, seq) {
    const idea = typeof currentIdea !== 'undefined' ? currentIdea : null;
    const run = (idea && idea.frameRun) || null;
    if (!run) return;
    if (type === 'video') {
        const valid = (run.videos || []).filter(v => v.url || v.file);
        if (!valid.length) return;
        const mediaList = valid.map(v => ({
            type: 'video',
            // 换位/覆盖过的片段路径没变、内容变了，lightbox 也得回源
            url: cacheBustedUrl(v.url || v.file),
            caption: `<strong>${videoSlotLabel(v.slot, slotIsHero(v))}</strong>`,
        }));
        const idx = valid.findIndex(v => Number(v.slot) === Number(seq));
        openLightbox(mediaList, idx >= 0 ? idx : 0);
        return;
    }
    const valid = (run.frames || []).filter(f => f.url || f.file);
    if (!valid.length) return;

    const refFrames = (idea && (idea.ref_frames || (idea.frameRun && idea.frameRun.ref_frames))) || {};
    const refRoles = (idea && (idea.ref_frame_roles || (idea.frameRun && idea.frameRun.ref_frame_roles))) || {};
    const mediaList = [];
    let targetIdx = 0;

    valid.forEach((f, i) => {
        const seqNum = Number(f.sequence);
        const isCurrent = (seqNum === Number(seq));
        if (isCurrent) {
            targetIdx = mediaList.length;
        }
        mediaList.push({
            type: 'image',
            url: f.url || f.file,
            caption: `<strong>🌟 第 ${seqNum} 拍生成关键帧 (IMG ${String(seqNum).padStart(3, '0')})</strong> [${i + 1}/${valid.length}]`,
        });
        const refUrl = refFrames[seqNum] || refFrames[String(seqNum)];
        if (refUrl) {
            mediaList.push({
                type: 'image',
                url: refUrl,
                caption: `<strong>🎯 第 ${seqNum} 拍爆款原片基准抽帧 (REF ${String(seqNum).padStart(3, '0')})</strong> ${refFrameRoleLabel(refRoles, seqNum)}`,
            });
        }
    });

    openLightbox(mediaList, targetIdx);
}

/**
 * 给一个卡片容器挂上委托。类型一律从 card.dataset.type 现取，不走闭包——
 * 合并视图下 #beats-grid 里同时放着两类卡片，闭包里的 type 会张冠李戴。
 */
function bindSlotGrid(gridId) {
    const grid = document.getElementById(gridId);
    if (!grid || grid.dataset.slotBound === '1') return;
    grid.dataset.slotBound = '1';

    grid.addEventListener('click', (e) => {
        const card = e.target.closest('.slot-card');
        if (!card || !grid.contains(card)) return;
        const seq = Number(card.dataset.seq);
        if (!Number.isFinite(seq)) return;

        // 点击左下角槽位标签直接跳转对应提示词卡片
        const labelEl = e.target.closest('.slot-label');
        if (labelEl) {
            e.stopPropagation();
            if (typeof jumpFromMediaToPrompt === 'function') {
                jumpFromMediaToPrompt(card.dataset.type || 'image', seq);
            }
            return;
        }

        // 点击"有问题"类徽标展开该帧的违规详情弹层：原生 title= 只留一句摘要，
        // 明细在弹层里可选中、可复制、按条排版（见 js/review_report.js）
        const issueBadge = e.target.closest(
            ISSUE_DETAIL_BADGES.map(id => `.slot-badge[data-badge="${id}"]`).join(','));
        if (issueBadge && card.dataset.type !== 'video') {
            e.stopPropagation();
            if (typeof openFrameIssuePop === 'function') openFrameIssuePop(card, seq);
            return;
        }

        // 点击 4选1 徽标直接打开候选对比弹窗
        const candidateBadge = e.target.closest('.slot-badge[data-badge="candidate-selection"]');
        if (candidateBadge) {
            e.stopPropagation();
            if (typeof openCandidateSelectionModal === 'function') {
                openCandidateSelectionModal(seq);
            } else if (typeof window !== 'undefined' && typeof window.openCandidateSelectionModal === 'function') {
                window.openCandidateSelectionModal(seq);
            }
            return;
        }

        const btn = e.target.closest('.slot-action-btn');
        if (btn) {
            // disabled 的按钮浏览器不会派发 click，走到这里就一定是可点的
            const handler = SLOT_ACTION_HANDLERS[btn.dataset.act];
            if (handler) handler(seq, card.dataset.type);
            return;
        }
        if (card.dataset.kind !== 'ready') return;
        openSlotLightbox(card.dataset.type, seq);
    });

    // 视频卡片悬浮自动播放。用会冒泡的 mouseover/mouseout + relatedTarget 判断，
    // 这样在卡片内部子元素之间移动不会反复 play/pause。
    const insideSameCard = (e, card) => {
        const to = e.relatedTarget;
        return to && card.contains(to);
    };
    grid.addEventListener('mouseover', (e) => {
        const card = e.target.closest('.slot-card');
        if (!card || card.dataset.type !== 'video'
            || card.dataset.kind !== 'ready' || insideSameCard(e, card)) return;
        const v = card.querySelector('video');
        if (v) v.play().catch(() => {});
        card.classList.add('is-hovered');
    });
    grid.addEventListener('mouseout', (e) => {
        const card = e.target.closest('.slot-card');
        if (!card || card.dataset.type !== 'video' || insideSameCard(e, card)) return;
        const v = card.querySelector('video');
        if (v) v.pause();
        card.classList.remove('is-hovered');
    });
}

function initSlotGrids() {
    // 三个容器：拆分视图的两个网格，以及合并视图（一拍一列）的 #beats-grid
    ['frames-grid', 'videos-grid', 'beats-grid'].forEach(bindSlotGrid);
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSlotGrids);
    } else {
        initSlotGrids();
    }
}
