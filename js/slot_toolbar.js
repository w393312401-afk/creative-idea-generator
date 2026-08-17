/* =====================================================================
   槽位网格工具条：计数 / 筛选 / 多选批量操作 / 尺寸档
   ---------------------------------------------------------------------
   方案见 docs/spark_result_slots_plan.md（§F）。

   在此之前，管理粒度只有"一格一格 hover 出小按钮"：12 拍以上的单子只能靠
   肉眼扫两个网格找问题，也没有任何批量出口（retryMissingVideos 早就存在，
   但只有"合并被拦截"的横幅能触到它）。

   两条设计约束：
   1. **不重新推导状态**。筛选与计数只读卡片上的 data-kind / data-badges，
      那是 renderSlotCard 按 slot_model 的判定写下的——工具条再算一遍就会有
      第二套口径，迟早对不上。
   2. **筛选是纯视觉的**。只给不匹配的卡片加 class，不动 DOM 结构、不动选中集，
      于是"筛完再选、清筛选后选中仍在"是自然行为。
   ===================================================================== */

const SLOT_SIZE_KEY = 'slot_grid_size';
const SLOT_MERGED_KEY = 'slot_merged_view';
const SLOT_SIZES = { S: 88, M: 120, L: 168 };

// 每个网格各自的筛选与选中集。选中集按槽位号存，重渲后由 syncSlotToolbar 复原。
const slotToolbarState = {
    image: { filter: 'all', selected: new Set() },
    video: { filter: 'all', selected: new Set() },
};

// ── 合并视图（一拍一列）──────────────────────────────────────────
// 契约是 VID N ≡ IMG N → IMG N+1，但两个网格隔着一屏，判断"这一段接不接得上"
// 要跨屏对号。合并视图把它们并进同一个 CSS 网格：第 N 列＝第 N 拍，
// 上行 IMG N、下行 VID N，空位一眼可见。
//
// 实现上不搬 DOM、不改渲染器输出：两个渲染器照旧各画各的卡片，只是改往
// #beats-grid 里 append，并按槽位号写 grid-column/grid-row。所以卡片 id、
// 拖拽监听、事件委托（委托绑在容器上，见下）全都原样成立。
let slotMergedView = false;

function isSlotMergedView() {
    return slotMergedView;
}

/** 渲染器该往哪个容器里 append。合并视图下两类卡片共用 #beats-grid。 */
function slotRenderTarget(type) {
    if (slotMergedView) {
        const merged = document.getElementById('beats-grid');
        if (merged) return merged;
    }
    return document.getElementById(type === 'video' ? 'videos-grid' : 'frames-grid');
}

/** 只清掉这一类的卡片：合并视图下两个渲染器共用一个容器，整体 innerHTML=''
    会把对方刚画好的那一行也抹掉。 */
function clearSlotGrid(grid, type) {
    if (!grid) return;
    if (grid.id !== 'beats-grid') { grid.innerHTML = ''; return; }
    grid.querySelectorAll(`.slot-card[data-type="${type}"]`).forEach(c => c.remove());
}

/** 合并视图下把卡片放到第 seq 列；拆分视图下清掉这两个属性回到自然流。 */
function placeSlotCard(card, type, seq) {
    if (!slotMergedView || !card) {
        if (card) { card.style.gridColumn = ''; card.style.gridRow = ''; }
        return;
    }
    card.style.gridColumn = String(seq);
    card.style.gridRow = type === 'video' ? '2' : '1';
}

function slotToolbarEls(type) {
    const bar = document.querySelector(`.slot-toolbar[data-slot-type="${type}"]`);
    return { bar, grid: slotRenderTarget(type) };
}

function readSlotSize() {
    try {
        const v = localStorage.getItem(SLOT_SIZE_KEY);
        if (v && SLOT_SIZES[v]) return v;
    } catch (e) { /* 隐私模式读不到就用默认 */ }
    return 'S';
}

function applySlotSize(size) {
    const key = SLOT_SIZES[size] ? size : 'S';
    document.querySelectorAll('.frames-grid').forEach(g => {
        g.style.setProperty('--slot-min', SLOT_SIZES[key] + 'px');
    });
    document.querySelectorAll('.slot-size-btn').forEach(b => {
        b.classList.toggle('is-active', b.dataset.size === key);
    });
    try {
        localStorage.setItem(SLOT_SIZE_KEY, key);
    } catch (e) { /* 写不进去不影响使用 */ }
}

/** 这张卡片在当前筛选下是否该显示。只读渲染时写下的 data-*，不另算一套。 */
function slotMatchesFilter(card, filter) {
    if (filter === 'all') return true;
    if (filter === 'flagged') return Number(card.dataset.badges || 0) > 0;
    if (filter === 'dirty') return card.dataset.promptDirty === '1';
    if (filter === 'missing') return ['missing', 'failed'].includes(card.dataset.kind);
    return true;
}

/**
 * 每次整格重渲之后调用：复原选中态、重新套用筛选、刷新计数。
 * 由 renderFramesForIdea / renderVideosForIdea 在收尾处调用。
 */
function syncSlotToolbar(type) {
    const { bar, grid } = slotToolbarEls(type);
    if (!bar || !grid) return;
    const st = slotToolbarState[type];
    const cards = Array.from(grid.querySelectorAll(
        slotMergedView ? `.slot-card[data-type="${type}"]` : '.slot-card'));

    // 删除/恢复会改变槽位总数：选中集里已经不存在的槽位要丢掉，否则批量操作
    // 会打到一个别的拍上
    const present = new Set(cards.map(c => Number(c.dataset.seq)));
    Array.from(st.selected).forEach(seq => {
        if (!present.has(seq)) st.selected.delete(seq);
    });

    let shown = 0, ready = 0, flagged = 0, missing = 0, fixable = 0, dirty = 0;
    cards.forEach(card => {
        const seq = Number(card.dataset.seq);
        const match = slotMatchesFilter(card, st.filter);
        card.classList.toggle('slot-filtered-out', !match);
        if (match) shown += 1;
        if (card.dataset.kind === 'ready') ready += 1;
        if (Number(card.dataset.badges || 0) > 0) flagged += 1;
        if (card.dataset.fixable === '1') fixable += 1;
        if (card.dataset.promptDirty === '1') dirty += 1;
        if (['missing', 'failed'].includes(card.dataset.kind)) missing += 1;

        const picked = st.selected.has(seq);
        card.classList.toggle('is-selected', picked);
        const box = card.querySelector('.slot-select-box');
        if (box) box.checked = picked;
    });

    grid.classList.toggle('has-selection', st.selected.size > 0);

    const label = type === 'video' ? 'VID' : 'IMG';
    const countEl = bar.querySelector('.slot-count');
    if (countEl) {
        const parts = [`${label} ${ready}/${cards.length}`];
        if (flagged) parts.push(`⚠ ${flagged}`);
        if (dirty) parts.push(`⚡改动 ${dirty}`);
        if (missing) parts.push(`缺 ${missing}`);
        countEl.textContent = parts.join(' · ');
    }

    bar.querySelectorAll('.slot-filter-btn').forEach(b => {
        b.classList.toggle('is-active', b.dataset.filter === st.filter);
    });
    const emptyEl = bar.querySelector('.slot-filter-empty');
    if (emptyEl) {
        emptyEl.hidden = !(st.filter !== 'all' && shown === 0 && cards.length > 0);
    }

    const actions = bar.querySelector('.slot-bulk');
    if (actions) {
        actions.hidden = st.selected.size === 0;
        const n = st.selected.size;
        const retryBtn = actions.querySelector('[data-bulk="retry"]');
        const delBtn = actions.querySelector('[data-bulk="delete"]');
        if (retryBtn) retryBtn.textContent = `重试所选 (${n})`;
        if (delBtn) delBtn.textContent = `删除所选 (${n})`;
    }
    const jumpBtn = bar.querySelector('.slot-jump-btn');
    if (jumpBtn) jumpBtn.hidden = flagged === 0;

    // 「⚡ 重渲已改动帧」：存在 prompt_dirty 标脏槽位时自动显示
    const retryDirtyBtn = bar.querySelector('.slot-retry-dirty-btn');
    if (retryDirtyBtn) {
        retryDirtyBtn.hidden = dirty === 0;
        retryDirtyBtn.textContent = `⚡ 重渲已改动帧 (${dirty})`;
    }

    // 「🖼️ 多宫格检查器」快捷入口按钮
    const cvBtn = bar.querySelector('.slot-collage-btn');
    if (cvBtn) {
        const hasCollage = typeof currentIdea !== 'undefined' && currentIdea && (currentIdea.collage_url || (currentIdea.frameRun && (currentIdea.frameRun.collage_url || (currentIdea.frameRun.frames && currentIdea.frameRun.frames.length))));
        cvBtn.hidden = !hasCollage;
    }

    // 「全部修复」只在真有待修帧时露面，并把条数写在按钮上——⚠ 计数里还混着
    // 降级/过期这类修不了的徽标，光看它判断不出"有几帧可以一键修"
    const fixAllBtn = bar.querySelector('.slot-fix-all-btn');
    if (fixAllBtn) {
        fixAllBtn.hidden = fixable === 0;
        fixAllBtn.textContent = `🛠 全部修复 (${fixable})`;
    }
}

function setSlotFilter(type, filter) {
    slotToolbarState[type].filter = filter;
    syncSlotToolbar(type);
}

function clearSlotSelection(type) {
    slotToolbarState[type].selected.clear();
    syncSlotToolbar(type);
}

/** 滚到第一枚带徽标的卡片，并短暂高亮——20 拍以上的单子靠肉眼扫太慢。 */
function jumpToFirstFlagged(type) {
    const { grid } = slotToolbarEls(type);
    if (!grid) return;
    const target = Array.from(grid.querySelectorAll('.slot-card'))
        .find(c => Number(c.dataset.badges || 0) > 0
            && !c.classList.contains('slot-filtered-out'));
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.classList.add('slot-flash');
    setTimeout(() => target.classList.remove('slot-flash'), 1200);
}

// ── 批量操作 ────────────────────────────────────────────────────────

/**
 * 批量重试：串行，一格一格来。并行提交没有意义——服务端本来就是串行锁，
 * 同时发只会让后来的几个直接吃到"已在生成中"的错误。
 */
async function bulkRetrySlots(type) {
    const st = slotToolbarState[type];
    const seqs = Array.from(st.selected).sort((a, b) => a - b);
    if (!seqs.length) return;
    const label = type === 'video' ? 'VID' : 'IMG';
    const proceed = await customConfirm(
        `将依次重新生成 ${seqs.length} 个槽位：${seqs.map(padSlot).map(s => label + ' ' + s).join('、')}`);
    if (!proceed) return;

    clearSlotSelection(type);
    for (const seq of seqs) {
        if (type === 'video') await retrySingleVideo(seq);
        else await retrySingleFrame(seq);
    }
    showToast(`已依次重试 ${seqs.length} 个槽位。`, 'success');
}

/**
 * 批量删除整拍。**必须从大到小删**：删除会把其后所有槽位整体前移一位，
 * 按升序删的话，删完第 3 拍之后原来的第 5 拍已经变成第 4 拍，接着去删"第 5 拍"
 * 打到的是另一拍。降序删则每一次的槽位号在删的那一刻都还没被前移影响过。
 */
async function bulkDeleteSlots(type) {
    const st = slotToolbarState[type];
    const seqs = Array.from(st.selected).sort((a, b) => b - a);
    if (!seqs.length) return;
    const asc = Array.from(seqs).reverse();
    const proceed = await customConfirm(
        `将删除 ${seqs.length} 整拍：${asc.map(s => '第 ' + s + ' 拍').join('、')}<br><br>`
        + '每一拍的图片与视频提示词、文件一并删除，其后整体前移一位。'
        + '删除按<b>从后往前</b>的顺序进行，因此上面列出的拍号都是当前编号。<br>'
        + '每一拍都会各自保存恢复快照，可在 ⚙「已删除的拍」里逐个撤销。');
    if (!proceed) return;

    clearSlotSelection(type);
    let done = 0;
    for (const seq of seqs) {
        // deleteSlotBeat 自带二次确认，批量时跳过，否则每删一拍弹一次
        const ok = await deleteSlotBeat(seq, { skipConfirm: true });
        if (ok === false) break;
        done += 1;
    }
    showToast(`已删除 ${done} 整拍。`, done === seqs.length ? 'success' : 'warning');
}

/** 当前网格里"有待修问题"的槽位号（升序）。只读卡片上的 data-fixable——
    那是 renderSlotCard 按 slot_model.frameIsFixable 写下的，与卡片上「修复此帧
    问题」按钮的出现条件同源。 */
function fixableSlotSequences(type) {
    const { grid } = slotToolbarEls(type);
    if (!grid) return [];
    return Array.from(grid.querySelectorAll(
        `.slot-card[data-type="${type}"][data-fixable="1"]`))
        .map(c => Number(c.dataset.seq))
        .filter(n => Number.isFinite(n))
        .sort((a, b) => a - b);
}

function slotCardIsFixable(type, seq) {
    const card = document.getElementById(`${type === 'video' ? 'video' : 'frame'}-slot-${seq}`);
    return !!card && card.dataset.fixable === '1';
}

// 一轮批量修复是否正在进行：帧与帧之间有一段没有任务登记的空隙（回读 manifest、
// 弹确认框），此时 isIdeaTaskActive 是假的，再点一次按钮就会有两轮批量交错着修。
let fixAllRunning = false;
let retryDirtyRunning = false;

/** 当前网格里"提示词已改"的槽位号（升序）。只读卡片上的 data-prompt-dirty */
function dirtySlotSequences(type) {
    const { grid } = slotToolbarEls(type);
    if (!grid) return [];
    return Array.from(grid.querySelectorAll(
        `.slot-card[data-type="${type}"][data-prompt-dirty="1"]`))
        .map(c => Number(c.dataset.seq))
        .filter(n => Number.isFinite(n))
        .sort((a, b) => a - b);
}

/**
 * 「一键重渲已改动帧」：把所有被标脏（prompt_dirty）的槽位按编号升序自动依次重新生成。
 */
async function bulkRetryDirtySlots(type = 'image') {
    const idea = (typeof currentIdea !== 'undefined' && currentIdea) || null;
    if (!idea) {
        showToast('请先激发一个创意点子！', 'error');
        return;
    }
    if (retryDirtyRunning || isIdeaTaskActive(idea.id, type === 'video' ? 'videos' : 'frames')) {
        showToast(`该创意的${type === 'video' ? '视频' : '帧'}序列正在生成/重试中，请稍候`, 'error');
        return;
    }
    const seqs = dirtySlotSequences(type);
    if (!seqs.length) {
        showToast('当前没有需要重渲的已改动帧。', 'info');
        return;
    }

    retryDirtyRunning = true;
    try {
        const label = type === 'video' ? 'VID' : 'IMG';
        const proceed = await customConfirm(
            `将按编号依次重新生成 <b>${seqs.length}</b> 个提示词已改动的槽位：<br><br>`
            + `<b>${seqs.map(s => label + ' ' + padSlot(s)).join('、')}</b><br><br>`
            + '生成完成后将自动刷新画面并清除「提示词已改」徽标。');
        if (!proceed) return;

        const feed = (text, cls) => {
            if (typeof framesFeedLine === 'function') framesFeedLine(idea.id, text, cls);
        };
        feed(`⚡ 一键重渲已改动槽位：共 ${seqs.length} 帧待重跑，按编号依次处理…`);

        let count = 0;
        for (const seq of seqs) {
            if (type === 'video') {
                await retrySingleVideo(seq);
            } else {
                await retrySingleFrame(seq);
            }
            count += 1;
            if (typeof reloadManifestIntoIdea === 'function') await reloadManifestIntoIdea(idea);
            if (typeof isViewingIdea === 'function' && isViewingIdea(idea.id)) {
                if (type === 'video' && typeof renderVideosForIdea === 'function') renderVideosForIdea(idea);
                else if (typeof renderFramesForIdea === 'function') renderFramesForIdea(idea);
            }
        }

        feed(`⚡ 一键重渲结束：共完成 ${count} 个槽位的重新渲染。`, 'ok');
        showToast(`⚡ 已成功重渲 ${count} 个槽位！`, 'success');
    } catch (e) {
        showToast(`重渲中断: ${e.message}`, 'error');
    } finally {
        retryDirtyRunning = false;
    }
}

/**
 * 「一键全部修复」：把所有带待修问题的帧（一致性审查未过 + 人工标记）按帧号
 * 从小到大依次走一遍定向修复（fixFrameIssue，与卡片上那枚按钮同一条路径）。
 * 此前审查一次标出七八帧问题，只能一格一格点、点完还得盯着每一次复核结论。
 *
 * 为什么串行且升序：服务端是项目级串行锁，并发提交只会让后来的直接吃到"已在
 * 生成中"；而非首帧走图生图链式编辑，修完 IMG 003 再修 IMG 005 时后者读到的
 * 已经是新的 IMG 004——升序才让每一次修复都建立在前面已修好的画面上。
 *
 * 每修完一帧回读一次 manifest：修复后的复核结论（问题到底解决没有）只写在
 * 服务端 manifest 上，事件流里没有对应的 frame 事件；不回读的话下一轮"这帧还
 * 要不要修"与网格徽标读到的都还是修复前的旧判定。轮到某帧时它已经没有待修
 * 问题就跳过——对着一张没有记录问题的帧，后端 fix_frame_issue 会直接报错。
 */
async function bulkFixFlaggedFrames() {
    const idea = (typeof currentIdea !== 'undefined' && currentIdea) || null;
    if (!idea) {
        showToast('请先激发一个创意点子！', 'error');
        return;
    }
    if (fixAllRunning || isIdeaTaskActive(idea.id, 'frames')) {
        showToast('该创意的帧序列正在生成/修复中，请稍候', 'error');
        return;
    }
    const seqs = fixableSlotSequences('image');
    if (!seqs.length) {
        showToast('当前没有待修复的帧。', 'info');
        return;
    }

    fixAllRunning = true;
    try {
        const proceed = await customConfirm(
            `将按帧号依次修复 ${seqs.length} 帧：${seqs.map(s => 'IMG ' + padSlot(s)).join('、')}<br><br>`
            + '每一帧都会先按已记录的问题（一致性审查判定 + 人工描述）定向重写提示词，'
            + '再图生图重渲，并对着新画面复核问题是否已解决。<br>'
            + '一帧一帧来，中途可点进度条旁的「取消」停下，已经修好的帧保留。');
        if (!proceed) return;

        const feed = (text, cls) => {
            if (typeof framesFeedLine === 'function') framesFeedLine(idea.id, text, cls);
        };
        feed(`🛠 一键全部修复：共 ${seqs.length} 帧待修，按帧号依次处理…`);

        let fixed = 0, unresolved = 0, skipped = 0;
        let stopped = '', stoppedKind = '';
        for (const seq of seqs) {
            if (!slotCardIsFixable('image', seq)) {
                // 前面几帧的修复把这一帧的问题一并带掉了（或人工描述已被撤销）
                skipped += 1;
                feed(`⏭️ IMG ${padSlot(seq)} 轮到时已无待修问题，跳过`, 'ok');
                continue;
            }
            const r = (await fixFrameIssue(seq)) || {};
            if (r.status === 'ok') {
                if ((r.remaining || []).length) unresolved += 1; else fixed += 1;
            } else if (r.status === 'cancelled') {
                stopped = '已取消'; stoppedKind = 'cancelled'; break;
            } else if (r.status === 'disconnected') {
                stopped = '与服务的连接中断，后台可能仍在继续'; stoppedKind = 'failed'; break;
            } else {
                stopped = r.error || '修复失败'; stoppedKind = 'failed'; break;
            }
            if (typeof reloadManifestIntoIdea === 'function') await reloadManifestIntoIdea(idea);
            if (typeof renderFramesForIdea === 'function'
                && typeof isViewingIdea === 'function' && isViewingIdea(idea.id)) {
                renderFramesForIdea(idea);
            }
        }

        const parts = [];
        if (fixed) parts.push(`${fixed} 帧已修复`);
        if (unresolved) parts.push(`${unresolved} 帧重渲后复核仍有问题`);
        if (skipped) parts.push(`${skipped} 帧无需修复`);
        const summary = parts.join('，') || '没有帧被改动';
        const tail = stopped ? `，随后中止（${stopped}）` : '';
        feed(`🛠 一键全部修复结束：${summary}${tail}`,
             stoppedKind === 'failed' ? 'err' : ((unresolved || stopped) ? 'warn' : 'ok'));
        showToast(`全部修复：${summary}${tail}。`,
                  stoppedKind === 'failed' ? 'error'
                      : ((unresolved || stopped) ? 'warning' : 'success'));
    } finally {
        fixAllRunning = false;
    }
}

// ── 绑定 ────────────────────────────────────────────────────────────

function bindSlotToolbar(type) {
    const { bar, grid } = slotToolbarEls(type);
    if (!bar || bar.dataset.bound === '1') return;
    bar.dataset.bound = '1';

    bar.addEventListener('click', (e) => {
        const filterBtn = e.target.closest('.slot-filter-btn');
        if (filterBtn) { setSlotFilter(type, filterBtn.dataset.filter); return; }
        const sizeBtn = e.target.closest('.slot-size-btn');
        if (sizeBtn) { applySlotSize(sizeBtn.dataset.size); return; }
        if (e.target.closest('.slot-jump-btn')) { jumpToFirstFlagged(type); return; }
        if (e.target.closest('.slot-retry-dirty-btn')) { bulkRetryDirtySlots(type); return; }
        if (e.target.closest('.slot-collage-btn')) {
            if (typeof openCollageViewer === 'function') openCollageViewer();
            return;
        }
        // 「全部修复」只有图片工具条有这枚按钮（视频槽位没有"待修问题"这一说）
        if (e.target.closest('.slot-fix-all-btn')) { bulkFixFlaggedFrames(); return; }
        if (e.target.closest('.slot-merge-btn')) { setSlotMergedView(!slotMergedView); return; }
        const bulk = e.target.closest('[data-bulk]');
        if (!bulk) return;
        if (bulk.dataset.bulk === 'retry') bulkRetrySlots(type);
        else if (bulk.dataset.bulk === 'delete') bulkDeleteSlots(type);
        else clearSlotSelection(type);
    });

}

/**
 * 勾选走容器级委托。类型从 card.dataset.type 现取而不是闭包——合并视图下
 * #beats-grid 里同时放着两类卡片（同 slot_card.js 的 bindSlotGrid）。
 */
function bindSlotSelection(gridId) {
    const grid = document.getElementById(gridId);
    if (!grid || grid.dataset.selectBound === '1') return;
    grid.dataset.selectBound = '1';
    grid.addEventListener('change', (e) => {
        const box = e.target.closest('.slot-select-box');
        if (!box) return;
        const card = box.closest('.slot-card');
        if (!card) return;
        const cardType = card.dataset.type === 'video' ? 'video' : 'image';
        const seq = Number(card.dataset.seq);
        const st = slotToolbarState[cardType];
        if (box.checked) st.selected.add(seq); else st.selected.delete(seq);
        syncSlotToolbar(cardType);
    });
    // 勾选框在卡片内，别让这一下点击顺带开了 lightbox
    grid.addEventListener('click', (e) => {
        if (e.target.closest('.slot-select')) e.stopPropagation();
    }, true);
}

/**
 * 切换合并视图。切换本身只改 class 与容器归属，随后整格重渲两个网格——
 * 卡片由同一个 renderSlotCard 画出，两种视图下内容完全一致。
 */
function setSlotMergedView(on, persist = true) {
    slotMergedView = !!on;
    document.body.classList.toggle('slot-merged-view', slotMergedView);
    document.querySelectorAll('.slot-merge-btn').forEach(b => {
        b.classList.toggle('is-active', slotMergedView);
        b.setAttribute('aria-pressed', slotMergedView ? 'true' : 'false');
    });
    if (persist) {
        try {
            localStorage.setItem(SLOT_MERGED_KEY, slotMergedView ? '1' : '0');
        } catch (e) { /* 写不进去不影响使用 */ }
    }
    // 切换会把卡片挪到另一个容器：两边都要重画，否则旧容器里会留下上一次的卡片
    const merged = document.getElementById('beats-grid');
    if (merged) merged.innerHTML = '';
    const framesGrid = document.getElementById('frames-grid');
    const videosGrid = document.getElementById('videos-grid');
    if (framesGrid) framesGrid.innerHTML = '';
    if (videosGrid) videosGrid.innerHTML = '';
    if (typeof currentIdea !== 'undefined' && currentIdea) {
        if (typeof renderFramesForIdea === 'function') renderFramesForIdea(currentIdea);
        if (typeof renderVideosForIdea === 'function') renderVideosForIdea(currentIdea);
    }
}

function readSlotMergedView() {
    try {
        return localStorage.getItem(SLOT_MERGED_KEY) === '1';
    } catch (e) {
        return false;
    }
}

function initSlotToolbars() {
    bindSlotToolbar('image');
    bindSlotToolbar('video');
    ['frames-grid', 'videos-grid', 'beats-grid'].forEach(bindSlotSelection);
    applySlotSize(readSlotSize());
    setSlotMergedView(readSlotMergedView(), false);
    syncSlotToolbar('image');
    syncSlotToolbar('video');
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSlotToolbars);
    } else {
        initSlotToolbars();
    }
}
