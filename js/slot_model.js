/* =====================================================================
   槽位状态模型（纯函数：无 DOM、无全局、无副作用）
   ---------------------------------------------------------------------
   方案与取舍见 docs/spark_result_slots_plan.md。

   这里回答的唯一问题是：「给我一条 frame/video 记录，它是什么状态？」
   ——画成什么壳、挂哪几枚徽标、允许哪些操作、hover 提示写什么。

   在此之前，这些判断散在 media_renderer.js / api_client.js / app.js 的
   十几处 innerHTML 里各算各的，同一语义有多个别名（过期有三种写法、英雄展示
   有两种写法），于是改一处徽标要改十处、也没有任何可单测的口子。

   一条红线：本文件不许 import/引用任何 DOM 与全局状态。所有外部条件
   （这一格是否在当前任务范围内、该创意是否有任务在跑）由调用方通过 ctx 传进来。
   ===================================================================== */

function padSlot(n) {
    return String(Number(n) || 0).padStart(3, '0');
}

// ── 语义别名收口 ────────────────────────────────────────────────────
// 同一件事在历史 manifest 里有多种写法，判定只在这里做一次。

/** 血统过期：stale_lineage 是后端现在唯一会写的字段，另两个是旧 manifest 的写法。 */
function slotIsStale(frame) {
    return !!(frame && (frame.stale_lineage || frame.quality_gate === 'stale' || frame.stale));
}

/** 英雄展示视频（收尾全景）：is_hero 是结构化字段，meta 含 HERO 是旧写法。 */
function slotIsHero(video) {
    if (!video) return false;
    return !!(video.is_hero || (video.meta && String(video.meta).toUpperCase().includes('HERO')));
}

function slotSwappedFrom(entry, key) {
    const v = Number(entry && entry[key]);
    return Number.isFinite(v) ? v : null;
}

// ── 徽标表 ──────────────────────────────────────────────────────────
// 新增一种徽标 = 在表里加一行。tip 是徽标自身的悬浮说明，hover 是它要往
// 整卡 title 里追加的那句话（两者措辞不同是既有行为，原样保留）。

const FRAME_BADGE_DEFS = [
    {
        id: 'degraded', text: '降级', cls: 'degraded-badge',
        test: f => f.quality_gate === 'i2i_fallback_degraded',
        tip: () => '',
        hover: () => ' (降级为文生图)',
    },
    {
        // 人工标记与机器判定可以并存，此时只画"人工标记"这一枚（既有行为）
        id: 'manual-flagged', text: '人工标记', cls: 'manual-flagged-badge',
        test: f => !!frameManualIssue(f),
        tip: f => (frameIsReviewFailed(f)
            ? `${frameManualIssue(f)}（一致性审查另判定：${f.vlm_qa_reason || ''}）`
            : frameManualIssue(f)),
        hover: f => ` (人工标记的问题: ${frameManualIssue(f)})`,
    },
    {
        id: 'review-failed', text: '审查未过', cls: 'vlm-failed-badge',
        test: f => frameIsReviewFailed(f) && !frameManualIssue(f),
        tip: f => f.vlm_qa_reason || '',
        hover: f => ` (一致性审查未通过: ${f.vlm_qa_reason || '跳变或无变化'})`,
    },
    {
        id: 'unverified', text: '未核验', cls: 'degraded-badge',
        test: f => f.quality_gate === 'auto_approved_degraded',
        tip: f => f.vlm_qa_reason || 'VLM 判定服务异常，未经核验',
        hover: () => ' (VLM 判定服务异常，此帧未经核验被放行)',
    },
    {
        id: 'review-skipped', text: '未审查', cls: 'degraded-badge',
        test: f => f.quality_gate === 'sequence_review_skipped',
        tip: f => f.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查',
        hover: f => ` (${f.vlm_qa_reason || '一致性审查服务不可用，此帧未经整套序列审查'})`,
    },
    {
        // 宽松档软性瑕疵放行：quality_gate 仍是 auto_approved，告警留在 vlm_qa_reason
        id: 'warned', text: '留痕', cls: 'degraded-badge',
        test: f => f.quality_gate === 'auto_approved'
            && typeof f.vlm_qa_reason === 'string' && f.vlm_qa_reason.indexOf('WARN') === 0,
        tip: f => f.vlm_qa_reason || '',
        hover: f => ` (宽松档放行: ${f.vlm_qa_reason})`,
    },
    {
        id: 'stale', text: 'Stale', cls: 'stale-badge',
        test: slotIsStale,
        tip: () => '此帧派生自已被替换的旧帧，建议重新生成',
        hover: () => ' (过期：父帧已被重新生成，此帧与父帧血统不一致)',
    },
];

const VIDEO_BADGE_DEFS = [
    {
        id: 'manual-upload', text: '手动', cls: 'degraded-badge',
        test: v => v.source === 'manual_upload',
        tip: () => '此片段由用户手动上传覆盖，非本地 UI 自动化产出',
        hover: () => ' (人工上传的本地视频)',
    },
    {
        id: 'swapped', text: '换位', cls: 'degraded-badge',
        test: v => slotSwappedFrom(v, 'swapped_from_slot') !== null,
        tip: v => `此片段由人工从 VID ${padSlot(v.swapped_from_slot)} 拖过来，`
            + '首尾帧未按本槽位锚点重新校验',
        hover: v => ` (人工从 VID ${padSlot(v.swapped_from_slot)} 拖过来)`,
    },
];

/** 'vlm_qa_failed' 是已停用的逐帧质检门终态，仅为兼容旧 manifest 保留。 */
function frameIsReviewFailed(frame) {
    return frame.quality_gate === 'vlm_qa_failed' || frame.quality_gate === 'sequence_review_flagged';
}

function frameManualIssue(frame) {
    return (typeof frame.manual_issue === 'string' && frame.manual_issue.trim())
        ? frame.manual_issue : '';
}

function collectBadges(defs, entry) {
    return defs.filter(d => d.test(entry)).map(d => ({
        id: d.id,
        text: d.text,
        cls: d.cls,
        tip: d.tip(entry) || '',
    }));
}

// ── 操作按钮 ────────────────────────────────────────────────────────
// variant 决定配色（danger = 红底），disabled/title 由 ctx.busy 统一决定。

const BUSY_TIP_FRAMES = '该创意的帧序列正在生成/重试中，请稍候';
const BUSY_TIP_VIDEOS = '该创意的视频序列正在生成/重试中，请稍候';
const DELETE_TIP = '删除这一整拍：先保存恢复快照，再将其后图片/视频整体前移一位';

function slotAction(act, label, opts) {
    const o = opts || {};
    return {
        act,
        label,
        cls: o.cls || '',
        variant: o.variant || 'default',
        disabled: !!o.disabled,
        title: o.title || '',
    };
}

function frameActions(state, ctx) {
    const busy = !!ctx.busy;
    const tip = busy ? BUSY_TIP_FRAMES : '';
    const list = [];
    if (state.kind === 'ready') {
        if (state.flags.fixable) {
            list.push(slotAction('fix-frame', '修复此帧问题', {
                cls: 'fix-frame-btn', variant: 'danger', disabled: busy,
                title: busy ? tip : '依据问题描述优化提示词后图生图重渲此帧',
            }));
        }
        list.push(slotAction('describe-frame', state.flags.manualFlagged ? '改描述' : '描述问题', {
            cls: 'describe-frame-btn', disabled: busy,
            title: busy ? tip : '人工描述这一帧哪里不对，作为定向修复的依据',
        }));
        list.push(slotAction('retry-frame', '重试', { cls: 'retry-frame-btn', disabled: busy, title: tip }));
    } else if (state.kind === 'missing') {
        list.push(slotAction('retry-frame', '生成', { cls: 'retry-frame-btn', disabled: busy, title: tip }));
    }
    if (state.kind === 'ready' || state.kind === 'missing') {
        // 出图卡是深色底、红底删除键用来跟其余按钮区分；占位卡是浅色底，
        // 沿用 secondary 的浅色描边（两种底色下的既有外观）
        list.push(slotAction('delete-slot', '删除', {
            cls: state.kind === 'ready' ? 'delete-slot-btn' : 'delete-slot-btn secondary',
            variant: state.kind === 'ready' ? 'danger' : 'default',
            disabled: busy,
            title: busy ? tip : DELETE_TIP,
        }));
    }
    return list;
}

function videoActions(state, ctx) {
    const busy = !!ctx.busy;
    const tip = busy ? BUSY_TIP_VIDEOS : '';
    if (state.kind === 'pending' || state.kind === 'cut') return [];
    const isReady = state.kind === 'ready';
    // 占位卡是浅色底，上传/删除沿用 secondary 的浅色描边；出图卡是深色底，
    // 删除用红底跟其余按钮区分（两种底色下的既有外观）
    const sub = isReady ? '' : ' secondary';
    return [
        slotAction('retry-video', state.kind === 'missing' ? '生成' : '重试', {
            cls: 'retry-video-btn', disabled: busy,
            title: busy ? tip : (isReady ? '重新生成此槽位视频（覆盖当前片段）' : ''),
        }),
        slotAction('upload-video', '上传', {
            cls: 'upload-video-btn' + sub, disabled: busy,
            title: busy ? tip : '手动上传本地视频文件覆盖此槽位',
        }),
        slotAction('delete-slot', '删除', {
            cls: 'delete-slot-btn' + sub,
            variant: isReady ? 'danger' : 'default',
            disabled: busy,
            title: busy ? tip : DELETE_TIP,
        }),
    ];
}

// ── 状态构造 ────────────────────────────────────────────────────────
// ctx: { seq, busy, pending }
//   busy    该创意的同类任务正在跑 → 所有按钮画成禁用态
//   pending 这一格在当前任务的目标范围内、只是还没轮到 → 画等待中而非"未生成"
//           （单帧重试时其余槽位服务端压根没碰，画等待中会误导，见
//            2026-07-20 实机事故记录）

/** 等待/进行中占位（生成中、重试中、修复中、上传中……），文案由调用方给。 */
function slotPendingState(type, seq, text) {
    const label = type === 'video'
        ? `第 ${padSlot(seq)} 段视频 (${text})`
        : `第 ${padSlot(seq)} 帧 (${text})`;
    return {
        type, seq, kind: 'pending', label,
        statusText: '', title: '', url: '',
        badges: [], actions: [], flags: {}, draggable: false,
    };
}

function frameSlotState(frame, ctx) {
    const seq = Number(ctx.seq);
    const url = frame && (frame.url || frame.file);
    const label = `IMG ${padSlot(seq)}`;

    if (!url) {
        if (ctx.pending) return slotPendingState('image', seq, '等待中');
        const st = {
            type: 'image', seq, kind: 'missing', label, url: '',
            statusText: '未生成/已失效', title: '', badges: [], flags: {}, draggable: false,
        };
        st.actions = frameActions(st, ctx);
        return st;
    }

    const manualIssue = frameManualIssue(frame);
    const flags = {
        degraded: frame.quality_gate === 'i2i_fallback_degraded',
        reviewFailed: frameIsReviewFailed(frame),
        manualFlagged: !!manualIssue,
        unverified: frame.quality_gate === 'auto_approved_degraded',
        reviewSkipped: frame.quality_gate === 'sequence_review_skipped',
        stale: slotIsStale(frame),
        manualUpload: frame.source === 'manual_upload',
        swappedFrom: slotSwappedFrom(frame, 'swapped_from_sequence'),
        manualIssue,
    };
    // 「修复此帧问题」的出口条件：机器判定未过、或人工标记了问题，两者任一成立
    flags.fixable = flags.reviewFailed || flags.manualFlagged;

    const st = {
        type: 'image', seq, kind: 'ready', label, url,
        statusText: '', flags,
        badges: collectBadges(FRAME_BADGE_DEFS, frame),
        title: frameHoverTitle(frame, seq, flags),
        draggable: true,
        // 卡片描边：未核验/未审查沿用降级的琥珀色（既有行为）
        cardCls: [
            flags.degraded ? 'degraded-card' : '',
            flags.reviewFailed ? 'vlm-failed-card' : '',
            flags.manualFlagged ? 'manual-flagged-card' : '',
            (flags.unverified || flags.reviewSkipped) ? 'degraded-card' : '',
            flags.stale ? 'stale-card' : '',
        ].filter(Boolean).join(' '),
    };
    st.actions = frameActions(st, ctx);
    return st;
}

function frameHoverTitle(frame, seq, flags) {
    let t = `打开第 ${seq} 帧`;
    FRAME_BADGE_DEFS.forEach(d => {
        if (d.test(frame)) t += d.hover(frame);
    });
    // 结构化违规：哪一层检出的、涉及哪几帧都留了下来，按条列出比一根 '；' 好读
    if (Array.isArray(frame.review_issues) && frame.review_issues.length) {
        t += '\n' + frame.review_issues.map(i =>
            `· [${i.layer === 'global' ? '跨帧' : (i.layer === 'manual' ? '人工' : '本拍')}] `
            + `${i.text}（涉及 IMG ${(i.frames || []).map(padSlot).join('/')}）`
        ).join('\n');
    }
    if (flags.swappedFrom !== null) t += ` (人工从 IMG ${padSlot(flags.swappedFrom)} 拖过来)`;
    if (flags.manualUpload) t += ' (人工上传的本地图片)';
    return t;
}

/**
 * 视频槽位标签：VID N 恒等于 IMG N → IMG N+1（槽位契约）。
 * 英雄展示视频只上传首帧完工图、没有"下一张"可指向，用专属文案。
 */
function videoSlotLabel(seq, isHero) {
    return isHero
        ? `VID ${padSlot(seq)} (英雄展示 · 完工全景)`
        : `VID ${padSlot(seq)} (IMG ${padSlot(seq)} ➔ IMG ${padSlot(seq + 1)})`;
}

function videoSlotState(video, ctx) {
    const seq = Number(ctx.seq);

    if (!video) {
        if (ctx.pending) return slotPendingState('video', seq, '等待中');
        const st = {
            type: 'video', seq, kind: 'missing', label: `VID ${padSlot(seq)}`, url: '',
            statusText: '未生成', title: '', badges: [], flags: {}, draggable: false,
        };
        st.actions = videoActions(st, ctx);
        return st;
    }

    const isHero = slotIsHero(video);
    const label = videoSlotLabel(Number(video.slot) || seq, isHero);
    const url = video.url || video.file;

    // 声明式硬切（[CUT]，TBCP v2 hard_cut）：该槽不生成视频，成片在此处直接硬切。
    // 画中性卡片而不是失败卡——重试它没有意义。合并门禁把 skipped_cut /
    // skipped_bridge_hold 都当"预期缺失"（server.py:1395），这里口径必须一致：
    // 此前重渲清单时它们没有 url，会被当成 status==='failed' 画成带重试按钮的
    // 失败卡，与服务端判定相反。
    if (video.status === 'skipped_cut' || video.status === 'skipped_bridge_hold') {
        return {
            type: 'video', seq, kind: 'cut', label, url: '',
            statusText: '声明式硬切（无片段）',
            title: video.message || '声明式硬切槽位：不生成视频片段，成片在此处直接硬切。',
            badges: [], actions: [], flags: { hero: isHero }, draggable: false,
        };
    }

    if (video.status === 'failed' || !url) {
        const st = {
            type: 'video', seq, kind: 'failed', label, url: '',
            statusText: '生成失败', title: video.error || '生成失败',
            badges: [], flags: { hero: isHero }, draggable: false,
        };
        st.actions = videoActions(st, ctx);
        return st;
    }

    const flags = {
        hero: isHero,
        manualUpload: video.source === 'manual_upload',
        swappedFrom: slotSwappedFrom(video, 'swapped_from_slot'),
    };
    const st = {
        type: 'video', seq, kind: 'ready', label, url,
        statusText: '', flags, cardCls: '',
        badges: collectBadges(VIDEO_BADGE_DEFS, video),
        title: `播放 ${label}`,
        draggable: true,
    };
    st.actions = videoActions(st, ctx);
    return st;
}

/**
 * 一组槽位状态的汇总（供工具条计数、"只看有问题的"筛选与合成前风险提示复用）。
 * 与 summarizeRunQuality 的口径从此同源。
 */
function summarizeSlotStates(states) {
    const list = states || [];
    return {
        total: list.length,
        ready: list.filter(s => s.kind === 'ready').length,
        pending: list.filter(s => s.kind === 'pending').length,
        missing: list.filter(s => s.kind === 'missing' || s.kind === 'failed').length,
        flagged: list.filter(s => (s.badges || []).length > 0).length,
    };
}

// Node 下单测用；浏览器里各函数已是全局，这段不执行。
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        padSlot, slotIsStale, slotIsHero,
        frameSlotState, videoSlotState, slotPendingState,
        videoSlotLabel, summarizeSlotStates,
        FRAME_BADGE_DEFS, VIDEO_BADGE_DEFS,
    };
}
