/* =====================================================================
   一致性审查结论的可读化：结论面板（A）+ 单帧违规详情弹层（B）
   ---------------------------------------------------------------------
   背景：审查早就把违规存成了结构化记录（prompt_pipeline 落
   {text, layer, beat, frames, verified}，chain_guard 另加 severity），
   落进 manifest 的 frame.review_issues。但在此之前读取端只有两条路：
     · slot_model.frameHoverTitle 把它们逐条换行拼进原生 title= —— 不能选中
       复制、不能筛选、断行不可控、鼠标一动就没；
     · 日志流里一条把「N 帧有问题 + 每帧明细 + 未审 + 增量说明」拼死的长句。
   于是"这一单到底有几个问题、分别在哪几帧"要靠人肉逐格悬停去拼。

   这里补上两个读取口，都只消费已有字段，不改后端、不改审查逻辑：
   1. collectReviewIssues：按 (layer, beat, text) 去重——跨帧层的一条违规
      本来就会被同时挂到它涉及的**每一帧**上（pipeline_orchestrator 按
      beat+1 归属落盘），逐格看会把同一个问题数成好几个。
   2. renderReviewPanel：帧网格上方的一览，一行一个问题，点行高亮对应格子。
   3. openFrameIssuePop：点徽标展开可复制的详情弹层，title= 那边只留摘要。
   ===================================================================== */

const REVIEW_LAYER_LABELS = { global: '跨帧', local: '本拍', manual: '人工' };

/** 判定为"这一格有待处理问题"的 quality_gate（与 slot_model.frameIsFixable 同源）。 */
const ISSUE_GATES = ['sequence_review_flagged', 'vlm_qa_failed', 'frame_continuity_failed'];

/** 严重度只有链上守卫那条路径产出（chain_guard.classify_chain_impact）；
 *  手动整套审查的违规没有这个字段，返回 '' 表示"未分级"，不许假装是 cosmetic。 */
function reviewIssueSeverity(issue) {
    const sev = issue && typeof issue.severity === 'string' ? issue.severity.toLowerCase() : '';
    return (sev === 'chain' || sev === 'cosmetic') ? sev : '';
}

function reviewLayerLabel(layer) {
    return REVIEW_LAYER_LABELS[layer] || '本拍';
}

/**
 * 把整单的 review_issues 收成一张去重后的问题清单。
 *
 * 返回 [{ key, layer, text, severity, verified, frames, owners }]：
 *   frames＝这条违规涉及的帧号（跨帧层往往不止一帧）
 *   owners＝它被落在哪几帧的 manifest 条目上，也就是「修复此帧问题」的落点
 * 按涉及帧号升序，同帧内 本拍 → 跨帧 → 人工。
 */
function collectReviewIssues(frameRun) {
    const frames = (frameRun && frameRun.frames) || [];
    const byKey = new Map();

    frames.forEach(f => {
        const seq = Number(f.sequence);
        (Array.isArray(f.review_issues) ? f.review_issues : []).forEach(i => {
            if (!i || typeof i.text !== 'string' || !i.text.trim()) return;
            // verified===false 的违规后端本来就不落盘（复核否决）；万一老 manifest
            // 里留着，这里也不展示——展示已被推翻的指控比不展示更糟。
            if (i.verified === false) return;
            const layer = (i.layer === 'global' || i.layer === 'manual') ? i.layer : 'local';
            const key = `${layer}|${i.beat}|${i.text}`;
            let entry = byKey.get(key);
            if (!entry) {
                entry = {
                    key, layer, text: i.text.trim(),
                    severity: reviewIssueSeverity(i),
                    // 复核跑过并确认＝true；跑不成时是 null/undefined，如实标"未复核"
                    verified: i.verified === true,
                    frames: [], owners: [],
                };
                byKey.set(key, entry);
            }
            (Array.isArray(i.frames) && i.frames.length ? i.frames : [seq]).forEach(n => {
                const v = Number(n);
                if (Number.isFinite(v) && !entry.frames.includes(v)) entry.frames.push(v);
            });
            if (Number.isFinite(seq) && !entry.owners.includes(seq)) entry.owners.push(seq);
            // 同一条违规在不同帧上分级可能缺一半：有 chain 就按 chain 算（停链口径）
            if (reviewIssueSeverity(i) === 'chain') entry.severity = 'chain';
            else if (!entry.severity) entry.severity = reviewIssueSeverity(i);
        });
    });

    // 只有 vlm_qa_reason、没有结构化记录的帧：老 manifest（结构化留痕是后来才加的）
    // 与生成期连续性失败（frame_continuity_failed 走的是 continuity_check，那条路
    // 压根不产 review_issues）。不兜这一层的话，这些帧会被汇总数成"有问题"、清单里
    // 却一行都列不出来，徽标点开也是空的——比不显示更让人不知所措。
    frames.forEach(f => {
        const seq = Number(f.sequence);
        if (!ISSUE_GATES.includes(f.quality_gate)) return;
        const has = Array.from(byKey.values()).some(e => e.owners.includes(seq) && e.layer !== 'manual');
        if (has) return;
        const text = String((f.continuity_check && f.continuity_check.reason)
            || f.vlm_qa_reason || '').trim();
        if (!text) return;
        const key = `local|${seq - 1}|${text}`;
        if (byKey.has(key)) return;
        byKey.set(key, {
            key, layer: 'local', text, severity: '',
            // 这条不是复核过的结构化判定，如实标未复核，别冒充确认过的
            verified: false,
            frames: [seq], owners: [seq],
        });
    });

    // 人工标记的问题：它存在 manual_issue 字段里，不在 review_issues 里，但对
    // "这一单还有什么没修"来说和机器判定是同一类东西（也正是「全部修复」的取帧
    // 口径，见 slot_model.frameIsFixable）。修复后复审会把它转写成 layer=manual
    // 的记录，那时按文本+归属帧认出来，不重复列一遍。
    frames.forEach(f => {
        const seq = Number(f.sequence);
        const text = typeof f.manual_issue === 'string' ? f.manual_issue.trim() : '';
        if (!text) return;
        const dup = Array.from(byKey.values()).some(
            e => e.layer === 'manual' && e.text === text && e.owners.includes(seq));
        if (dup) return;
        const key = `manual|${seq}|${text}`;
        if (byKey.has(key)) return;
        byKey.set(key, {
            key, layer: 'manual', text, severity: '', verified: true,
            frames: [seq], owners: [seq],
        });
    });

    const layerOrder = { local: 0, global: 1, manual: 2 };
    return Array.from(byKey.values())
        .map(e => Object.assign({}, e, {
            frames: e.frames.slice().sort((a, b) => a - b),
            owners: e.owners.slice().sort((a, b) => a - b),
        }))
        .sort((a, b) => (a.frames[0] || 0) - (b.frames[0] || 0)
            || (layerOrder[a.layer] - layerOrder[b.layer])
            || a.text.localeCompare(b.text));
}

/**
 * 这一单此刻的审查覆盖面。注意区分三种"没有问题"：审过且通过、从没审过、
 * 审查服务不可用被跳过——只看绿色徽标会把后两种读成第一种。
 */
function summarizeReviewState(frameRun) {
    const frames = ((frameRun && frameRun.frames) || []).filter(f => f.url || f.file);
    const gate = (f) => f.quality_gate;
    const passed = frames.filter(f => gate(f) === 'sequence_reviewed_pass').length;
    const flagged = frames.filter(f => ISSUE_GATES.includes(gate(f))).length;
    const skipped = frames.filter(f => gate(f) === 'sequence_review_skipped')
        .map(f => Number(f.sequence)).filter(Number.isFinite);
    const never = frames.filter(f => gate(f) === 'pending_manual_review')
        .map(f => Number(f.sequence)).filter(Number.isFinite);
    const manual = frames.filter(
        f => typeof f.manual_issue === 'string' && f.manual_issue.trim()).length;
    const stamps = frames.map(f => f.reviewed_at).filter(Boolean).sort();
    return {
        rendered: frames.length,
        passed, flagged, manual,
        skippedSeqs: skipped.sort((a, b) => a - b),
        neverSeqs: never.sort((a, b) => a - b),
        lastReviewedAt: stamps.length ? stamps[stamps.length - 1] : '',
    };
}

/** 面板抬头那一句：说清"审过多少、几处问题、还有多少没审"。 */
function reviewSummaryText(state, issueCount) {
    const parts = [];
    parts.push(issueCount ? `${issueCount} 处待处理问题` : '未发现问题');
    parts.push(`已审 ${state.passed + state.flagged}/${state.rendered} 帧`);
    const unreviewed = state.skippedSeqs.length + state.neverSeqs.length;
    if (unreviewed) parts.push(`${unreviewed} 帧未审查`);
    return parts.join(' · ');
}

// =====================================================================
// 以下是 DOM 层。Node 单测只 require 上面的纯函数，这些不会被执行。
// =====================================================================

function padReviewSeq(n) {
    return String(n).padStart(3, '0');
}

/** 滚到某一帧的卡片并短暂高亮（复用工具条那套 .slot-flash）。 */
function flashFrameCards(seqs) {
    let first = null;
    (seqs || []).forEach(seq => {
        const card = document.getElementById(`frame-slot-${seq}`);
        if (!card) return;
        if (!first) first = card;
        card.classList.add('slot-flash');
        setTimeout(() => card.classList.remove('slot-flash'), 1200);
    });
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function reviewIssueRow(entry) {
    const li = document.createElement('li');
    li.className = 'review-issue';
    li.dataset.seqs = entry.frames.join(',');
    li.dataset.layer = entry.layer;
    if (entry.severity) li.dataset.severity = entry.severity;

    const layerEl = document.createElement('span');
    layerEl.className = 'review-issue-layer';
    layerEl.textContent = reviewLayerLabel(entry.layer);
    layerEl.title = entry.layer === 'global'
        ? '跨帧层检出：拿整段序列互相比出来的问题（施工顺序、空间拓扑）'
        : (entry.layer === 'manual' ? '人工标记：你自己写下的问题描述，尚未修复'
                                    : '本拍检出：相邻两帧之间比出来的问题');
    li.appendChild(layerEl);

    const textEl = document.createElement('span');
    textEl.className = 'review-issue-text';
    textEl.textContent = entry.text;
    li.appendChild(textEl);

    const metaEl = document.createElement('span');
    metaEl.className = 'review-issue-meta';
    const framesEl = document.createElement('button');
    framesEl.type = 'button';
    framesEl.className = 'review-issue-frames';
    framesEl.textContent = entry.frames.map(s => `IMG ${padReviewSeq(s)}`).join(' / ');
    framesEl.title = '滚到这几帧并高亮';
    metaEl.appendChild(framesEl);

    // 严重度只有链上守卫分过级；没分级的不编一个出来
    if (entry.severity) {
        const sevEl = document.createElement('span');
        sevEl.className = `review-issue-sev sev-${entry.severity}`;
        sevEl.textContent = entry.severity === 'chain' ? '会传染下游' : '仅观感';
        sevEl.title = entry.severity === 'chain'
            ? '结构/顺序级：图生图会把它一路带进后面每一帧，优先修'
            : '观感级：同一物理状态的光影/色温/材质差异，可以放着';
        metaEl.appendChild(sevEl);
    }
    if (!entry.verified) {
        const unv = document.createElement('span');
        unv.className = 'review-issue-unverified';
        unv.textContent = '未复核';
        unv.title = '复核那一步没跑成，这条判定还没被二次确认';
        metaEl.appendChild(unv);
    }

    entry.owners.forEach(seq => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'review-issue-fix';
        btn.dataset.seq = String(seq);
        btn.textContent = `修复 IMG ${padReviewSeq(seq)}`;
        btn.title = '等同于该帧卡片上的「修复此帧问题」';
        metaEl.appendChild(btn);
    });
    li.appendChild(metaEl);
    return li;
}

/**
 * 帧网格上方的审查结论面板。数据全部从当前 manifest 现算，因此重渲一次
 * （审查跑完、修完一帧、拉过 manifest）就自动是最新的。
 */
function renderReviewPanel(idea) {
    const panel = document.getElementById('frames-review-panel');
    if (!panel) return;
    const frameRun = idea && idea.frameRun;
    const state = summarizeReviewState(frameRun);
    const issues = collectReviewIssues(frameRun);

    // 一帧都没渲的单子不占地方
    if (!state.rendered) { panel.hidden = true; return; }
    panel.hidden = false;
    panel.dataset.clean =
        (!issues.length && !state.skippedSeqs.length && !state.neverSeqs.length) ? '1' : '0';

    const summaryEl = panel.querySelector('.review-panel-summary');
    if (summaryEl) summaryEl.textContent = reviewSummaryText(state, issues.length);
    const stampEl = panel.querySelector('.review-panel-stamp');
    if (stampEl) {
        stampEl.textContent = state.lastReviewedAt
            ? `最近审查 ${String(state.lastReviewedAt).replace('T', ' ')}` : '';
    }

    const list = panel.querySelector('.review-issue-list');
    if (list) {
        list.textContent = '';
        issues.forEach(e => list.appendChild(reviewIssueRow(e)));
    }

    // 没审过的帧单独一行：它们既不是"通过"也不是"有问题"，混进上面的清单会被
    // 当成没事（2026-07-15 fail-open 事故的同款误读）
    const gapEl = panel.querySelector('.review-panel-gap');
    if (gapEl) {
        const unreviewed = state.neverSeqs.concat(state.skippedSeqs).sort((a, b) => a - b);
        gapEl.hidden = !unreviewed.length;
        gapEl.dataset.seqs = unreviewed.join(',');
        const listEl = gapEl.querySelector('.review-gap-frames');
        if (listEl) listEl.textContent = unreviewed.map(s => `IMG ${padReviewSeq(s)}`).join('、');
    }

    const emptyEl = panel.querySelector('.review-panel-empty');
    if (emptyEl) emptyEl.hidden = issues.length > 0;
}

/** 面板事件：整块委托一次，重渲换掉行不影响。 */
function initReviewPanel() {
    const panel = document.getElementById('frames-review-panel');
    if (!panel || panel.dataset.bound === '1') return;
    panel.dataset.bound = '1';

    const head = panel.querySelector('.review-panel-toggle');
    if (head) {
        // 折叠态记在本地：审过一轮、问题都看过之后不该每次进来都占半屏
        let collapsed = false;
        try { collapsed = localStorage.getItem('frames_review_panel_collapsed') === '1'; } catch (e) {}
        panel.classList.toggle('is-collapsed', collapsed);
        head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        head.addEventListener('click', () => {
            const next = !panel.classList.contains('is-collapsed');
            panel.classList.toggle('is-collapsed', next);
            head.setAttribute('aria-expanded', next ? 'false' : 'true');
            try { localStorage.setItem('frames_review_panel_collapsed', next ? '1' : '0'); } catch (e) {}
        });
    }

    panel.addEventListener('click', (e) => {
        const fixBtn = e.target.closest('.review-issue-fix');
        if (fixBtn) {
            e.stopPropagation();
            // 直接点该帧卡片上的那枚按钮：确认弹窗、连带重渲、忙态禁用全部沿用
            // 同一条路径，这里不复制一份逻辑（也就不会与它分叉）
            const seq = Number(fixBtn.dataset.seq);
            const cardBtn = document.querySelector(
                `#frame-slot-${seq} .slot-action-btn[data-act="fix-frame"]`);
            if (cardBtn && !cardBtn.disabled) cardBtn.click();
            else if (typeof showToast === 'function') {
                showToast(`IMG ${padReviewSeq(seq)} 当前不可修复（正在生成中，或这一格没有待修问题）`,
                          'warning');
            }
            return;
        }
        const row = e.target.closest('.review-issue, .review-panel-gap');
        if (row && row.dataset.seqs) {
            flashFrameCards(row.dataset.seqs.split(',').map(Number).filter(Boolean));
        }
    });
}

// =====================================================================
// B：单帧违规详情弹层（取代塞满 title= 的那一长串）
// =====================================================================

let _issuePopEl = null;

function closeFrameIssuePop() {
    if (_issuePopEl && _issuePopEl.parentNode) _issuePopEl.parentNode.removeChild(_issuePopEl);
    _issuePopEl = null;
}

/** 某一帧自己的待处理问题（含人工标记），口径与面板一致。 */
function frameIssueEntries(seq) {
    const run = (typeof currentIdea !== 'undefined' && currentIdea && currentIdea.frameRun) || null;
    return collectReviewIssues(run).filter(
        e => e.owners.includes(seq) || e.frames.includes(seq));
}

/**
 * 在卡片旁边展开详情。相对 title= 的三点差别：文字可选中复制、按条排版不被
 * 系统 tooltip 的断行规则揉成一坨、不会因为鼠标挪一下就消失。
 */
function openFrameIssuePop(cardEl, seq) {
    closeFrameIssuePop();
    if (!cardEl) return;
    const entries = frameIssueEntries(seq);
    if (!entries.length) {
        // 徽标画了、却一条明细都留不下（极老的 manifest：只有 quality_gate、
        // 连 vlm_qa_reason 都没有）。宁可说清楚，也不要一枚点了没反应的徽标。
        if (typeof showToast === 'function') {
            showToast(`IMG ${padReviewSeq(seq)} 只留下了状态标记，没有可展开的违规原文——重跑一次「🔍 一致性审查」即可补齐`,
                      'warning');
        }
        return;
    }

    const pop = document.createElement('div');
    pop.className = 'frame-issue-pop';
    pop.setAttribute('role', 'dialog');

    const head = document.createElement('div');
    head.className = 'frame-issue-pop-head';
    const title = document.createElement('span');
    title.className = 'frame-issue-pop-title';
    title.textContent = `IMG ${padReviewSeq(seq)} · ${entries.length} 处问题`;
    head.appendChild(title);
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'frame-issue-pop-copy';
    copyBtn.textContent = '复制';
    copyBtn.title = '复制全部违规原文（贴进「描述问题」或对话里追问用）';
    copyBtn.addEventListener('click', () => {
        const text = entries.map(e =>
            `[${reviewLayerLabel(e.layer)}] ${e.text}（涉及 IMG ${e.frames.map(padReviewSeq).join('/')}）`
        ).join('\n');
        if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
        if (typeof showToast === 'function') showToast('已复制审查违规原文', 'success');
    });
    head.appendChild(copyBtn);
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'frame-issue-pop-close';
    closeBtn.textContent = '×';
    closeBtn.title = '关闭（Esc）';
    closeBtn.addEventListener('click', closeFrameIssuePop);
    head.appendChild(closeBtn);
    pop.appendChild(head);

    const ul = document.createElement('ul');
    ul.className = 'frame-issue-pop-list';
    entries.forEach(e => {
        const li = document.createElement('li');
        li.dataset.layer = e.layer;
        if (e.severity) li.dataset.severity = e.severity;
        const tag = document.createElement('span');
        tag.className = 'review-issue-layer';
        tag.textContent = reviewLayerLabel(e.layer);
        li.appendChild(tag);
        const txt = document.createElement('span');
        txt.className = 'review-issue-text';
        txt.textContent = e.text;
        li.appendChild(txt);
        const involved = document.createElement('span');
        involved.className = 'review-issue-frames-static';
        involved.textContent = `涉及 IMG ${e.frames.map(padReviewSeq).join(' / ')}`;
        li.appendChild(involved);
        ul.appendChild(li);
    });
    pop.appendChild(ul);

    document.body.appendChild(pop);
    _issuePopEl = pop;

    // 定位：贴着卡片左下，越界就翻到左/上。用 fixed + 视口坐标，网格自身的
    // 横向滚动容器不会把它裁掉。
    const r = cardEl.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = r.left;
    let top = r.bottom + 8;
    if (left + w > window.innerWidth - 8) left = Math.max(8, window.innerWidth - w - 8);
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
}

if (typeof document !== 'undefined') {
    document.addEventListener('click', (e) => {
        if (_issuePopEl && !e.target.closest('.frame-issue-pop')
            && !e.target.closest('.slot-badge')) closeFrameIssuePop();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFrameIssuePop(); });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initReviewPanel);
    } else {
        initReviewPanel();
    }
}

// Node 下单测用；浏览器里各函数已是全局，这段不执行。
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        collectReviewIssues, summarizeReviewState, reviewSummaryText,
        reviewIssueSeverity, reviewLayerLabel,
    };
}
