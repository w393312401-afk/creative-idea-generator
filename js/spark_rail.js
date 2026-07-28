/* =====================================================================
   激发维度页 · 激发轨 + 取材抽屉 + 细调条（纯 UI 状态，零业务逻辑）
   ---------------------------------------------------------------------
   方案与取舍见 docs/spark_dimension_minimal_layout_plan.md。

   这里只做三件事：
     1. 把「① 取材 → ② 选题 → ③ 合成」三段的就绪/进行中状态显示在轨上；
     2. 记住两个取材抽屉与页脚细调条的折叠态（localStorage）；
     3. 按当前进度决定页脚两枚按钮谁是主按钮。

   一条红线：不碰任何请求逻辑，也不改 generate-btn 的 disabled（那是 app.js
   在生成生命周期里管的），主次只用 class 表达。状态全部是"读出来的"——
   参考勾选读 getSelectedTrendRefIds()、选题读 loadedIdeationCover、
   合成读 generationState.status / currentIdea，所以不需要在业务代码里补埋点。
   ===================================================================== */

const SPARK_DRAWER_KEY = 'spark_drawer_open';
const SPARK_TUNE_KEY = 'spark_tune_open';

// 激发中（换一批灵感）由 app.js loadIdeationCards 通过 setSparkIdeating 告知：
// 那是本页唯一一个 DOM 上看不出来的状态。合成中直接读 generationState。
let sparkIdeating = false;
let sparkIdeatingSince = 0;
// 上一次渲染用的状态签名：1 秒心跳只在签名变化时才写 DOM
let sparkRailSignature = '';

function readSparkDrawerState() {
    try {
        const raw = JSON.parse(localStorage.getItem(SPARK_DRAWER_KEY));
        if (raw && typeof raw === 'object') return raw;
    } catch (e) { /* 落回默认 */ }
    return null;
}

function writeSparkDrawerState(state) {
    try {
        localStorage.setItem(SPARK_DRAWER_KEY, JSON.stringify(state));
    } catch (e) { /* 隐私模式写不进去也不影响使用 */ }
}

function setSparkDrawerOpen(name, open) {
    const drawer = document.querySelector(`.spark-drawer[data-drawer="${name}"]`);
    if (!drawer) return;
    drawer.classList.toggle('drawer-open', !!open);
    const toggle = drawer.querySelector('.spark-drawer-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');

    const state = readSparkDrawerState() || {};
    state[name] = !!open;
    writeSparkDrawerState(state);
}

function initSparkDrawers() {
    const stored = readSparkDrawerState();
    // 默认两个抽屉都折叠：「没勾选参考」是完全可用的默认（后端会自动联网搜一条），
    // 为它把 320px 的案例库摊开会把真正要挑的灵感卡整个挤出首屏——实测过。
    // 唯一需要引导的情况是案例库真的空着，那由 maybeGuideEmptyRefLibrary() 在
    // 列表渲染完（拿到条数）时补开一次。
    const state = stored || { refs: false, pacing: false };

    document.querySelectorAll('.spark-drawer').forEach(drawer => {
        const name = drawer.dataset.drawer;
        const open = !!state[name];
        drawer.classList.toggle('drawer-open', open);
        const toggle = drawer.querySelector('.spark-drawer-toggle');
        if (!toggle) return;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.addEventListener('click', () => {
            setSparkDrawerOpen(name, !drawer.classList.contains('drawer-open'));
        });
    });
    // 默认态刻意不落盘：留着 localStorage 为空这个信号，让上面那条空库引导
    // 还有机会判断"用户是否自己动过折叠态"。只有用户点过开关才写。
}

/* 案例库空着（第一次用、或全清过）时，把取材抽屉自动摊开一次当引导——
   这时抽屉里就一句"点右上搜一批新参考"，不占高度，也确实是该做的第一步。
   只在用户还没自己折腾过折叠态时生效（stored 为空），之后一律听用户的。
   由 js/trend_refs.js 的 renderTrendRefs() 在拿到条数后调用。 */
function maybeGuideEmptyRefLibrary(refCount) {
    if (refCount > 0) return;
    if (readSparkDrawerState()) return;
    setSparkDrawerOpen('refs', true);
}

function setSparkTuneOpen(open) {
    const tune = document.getElementById('spark-tune');
    const toggle = document.getElementById('spark-tune-toggle');
    if (!tune) return;
    tune.classList.toggle('tune-collapsed', !open);
    if (toggle) {
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.title = open ? '收起节拍与模型细调' : '展开节拍与模型细调';
    }
    try {
        localStorage.setItem(SPARK_TUNE_KEY, open ? '1' : '0');
    } catch (e) { /* 同上 */ }
}

function initSparkTune() {
    const toggle = document.getElementById('spark-tune-toggle');
    if (!toggle) return;
    let open = false;
    try {
        open = localStorage.getItem(SPARK_TUNE_KEY) === '1';
    } catch (e) { /* 默认折叠 */ }
    setSparkTuneOpen(open);
    toggle.addEventListener('click', () => {
        const tune = document.getElementById('spark-tune');
        setSparkTuneOpen(tune ? tune.classList.contains('tune-collapsed') : true);
    });
}

// 轨上三枚芯片都是"指路牌"：点了不发请求，只把对应的那一步摆到眼前。
function initSparkRailNav() {
    const rail = document.getElementById('spark-rail');
    if (!rail) return;
    rail.addEventListener('click', (e) => {
        const stage = e.target.closest('.spark-stage');
        if (!stage) return;
        const body = document.querySelector('.panel-left .panel-body');
        switch (stage.dataset.stage) {
            case 'source':
                setSparkDrawerOpen('refs', true);
                if (body) body.scrollTo({ top: 0, behavior: 'smooth' });
                break;
            case 'topic': {
                // 一张卡都还没有（首次进入、或换参考后缓存失效）：点 ② 直接
                // 借用页脚那枚「换一批灵感」按钮，省掉一次找按钮的来回。
                const hasCards = typeof currentIdeatedIdeas !== 'undefined'
                    && Array.isArray(currentIdeatedIdeas) && currentIdeatedIdeas.length > 0;
                if (!hasCards && !sparkIdeating) {
                    const refresh = document.getElementById('ideate-refresh-btn');
                    if (refresh) refresh.click();
                }
                const group = document.querySelector('.spark-cards-group');
                if (group) group.scrollIntoView({ behavior: 'smooth', block: 'start' });
                break;
            }
            case 'compose': {
                const genBtn = document.getElementById('generate-btn');
                if (genBtn) genBtn.focus({ preventScroll: true });
                break;
            }
        }
    });
}

// app.js loadIdeationCards 在发请求前后各调一次
function setSparkIdeating(on) {
    sparkIdeating = !!on;
    sparkIdeatingSince = on ? Date.now() : 0;
    const container = document.getElementById('ideation-cards-container');
    if (container) container.classList.toggle('is-ideating', !!on);
    updateSparkRail();
}

function sparkStageEl(name) {
    return document.getElementById(`spark-stage-${name}`);
}

function paintSparkStage(name, state, note) {
    const el = sparkStageEl(name);
    if (!el) return;
    el.classList.toggle('stage-ready', state === 'ready');
    el.classList.toggle('stage-active', state === 'active');
    el.classList.toggle('stage-idle', state === 'idle');
    const noteEl = document.getElementById(`spark-stage-${name}-note`);
    if (noteEl && noteEl.textContent !== note) noteEl.textContent = note;
}

/* 三段状态一次算完 + 页脚按钮主次 + 细调条摘要。
   调用点：updateConfigSummary() 末尾（覆盖初始化/滑杆/卡片载入/面板切换）、
   trend_refs 渲染后、renderIdeationCards 末尾、setSparkIdeating，以及 1 秒心跳
   （只为"进行中"的秒表与合成状态，签名不变时不写 DOM）。 */
function updateSparkRail() {
    if (!document.getElementById('spark-rail')) return;

    const refCount = typeof getSelectedTrendRefIds === 'function'
        ? getSelectedTrendRefIds().length : 0;
    const skeletonCount = document.querySelectorAll('input[name="pacing-skeleton"]:checked').length;
    const loaded = (typeof loadedIdeationCover !== 'undefined' && loadedIdeationCover)
        ? loadedIdeationCover : null;
    const cards = (typeof currentIdeatedIdeas !== 'undefined' && Array.isArray(currentIdeatedIdeas))
        ? currentIdeatedIdeas.length : 0;
    const composing = typeof generationState !== 'undefined'
        && generationState && generationState.status === 'composing';
    const composed = typeof currentIdea !== 'undefined' && !!currentIdea;
    const beats = (document.getElementById('slider-beats') || {}).value || '';
    const beatModeFixed = ((document.getElementById('beat-count-mode') || {}).value || 'adaptive') === 'fixed';
    const model = (typeof config !== 'undefined' && config && config.model) ? config.model : '';

    const ideatingSec = sparkIdeating
        ? Math.max(0, Math.round((Date.now() - sparkIdeatingSince) / 1000)) : 0;
    const composingSec = (composing && generationState.startTime)
        ? Math.max(0, Math.round((Date.now() - generationState.startTime) / 1000)) : 0;

    const signature = [refCount, skeletonCount, loaded ? loaded.task_label : '', cards,
        composing ? 1 : 0, composed ? 1 : 0, beats, beatModeFixed ? 1 : 0, model,
        sparkIdeating ? 1 : 0, ideatingSec, composingSec].join('|');
    if (signature === sparkRailSignature) return;
    sparkRailSignature = signature;

    // ① 取材：骨架是激发 prompt 的必填输入，一套都没勾就是没就绪；
    //    参考没勾不算缺，后端会自动联网搜一条（见 /api/ideate）。
    paintSparkStage('source',
        skeletonCount > 0 ? 'ready' : 'idle',
        skeletonCount === 0
            ? '未启用骨架'
            : `${refCount > 0 ? `${refCount} 条参考` : '自动搜参考'} · ${skeletonCount} 套骨架`);

    // ② 选题
    if (sparkIdeating) {
        paintSparkStage('topic', 'active', `激发中 ${ideatingSec}s`);
    } else if (loaded && loaded.task_label) {
        paintSparkStage('topic', 'ready', loaded.task_label);
    } else {
        paintSparkStage('topic', 'idle', cards > 0 ? `${cards} 张待选` : '未激发');
    }

    // ③ 合成
    if (composing) {
        paintSparkStage('compose', 'active', `合成中 ${composingSec}s`);
    } else if (composed) {
        paintSparkStage('compose', 'ready', `已产出 · ${beats}拍`);
    } else {
        paintSparkStage('compose', 'idle',
            beats ? `${beats}拍 · ${beatModeFixed ? '固定' : '自适应'}` : '待激发');
    }

    // 页脚两枚按钮的主次：没选定选题 → 先换一批/挑一张；选定了 → 该点激发了。
    // 不动 disabled（那是 app.js 的生成生命周期在管），只用 class 表达权重。
    const refresh = document.getElementById('ideate-refresh-btn');
    const genBtn = document.getElementById('generate-btn');
    const topicReady = !!(loaded && loaded.task_label);
    if (refresh) {
        refresh.classList.toggle('is-next', !topicReady && !sparkIdeating);
        refresh.classList.toggle('is-muted', topicReady);
    }
    if (genBtn) {
        genBtn.classList.toggle('is-next', topicReady);
        genBtn.classList.toggle('is-muted', !topicReady);
    }

    const tuneSummary = document.getElementById('spark-tune-summary-text');
    if (tuneSummary) {
        // 自适应下滑块是上限而非承诺，载入过卡片时把卡片给的下界一并写出来，
        // 免得芯片上那个数字被当成成片拍数（见 docs/beat_count_skeleton_plan.md §1.5）。
        const floor = (!beatModeFixed && loaded && Number.isFinite(+loaded.beats_floor)
            && +loaded.beats_floor > 0) ? `≥${loaded.beats_floor} ` : '';
        const text = `${floor}${beats} 拍 · ${beatModeFixed ? '固定' : '自适应上限'}${model ? ` · ${model}` : ''}`;
        if (tuneSummary.textContent !== text) tuneSummary.textContent = text;
    }

    const cardsNote = document.getElementById('spark-cards-note');
    if (cardsNote) {
        const text = sparkIdeating
            ? '正在激发新的一批…'
            : (cards > 0
                ? (topicReady ? `本批 ${cards} 张 · 已选定` : `本批 ${cards} 张 · 待选定`)
                : '');
        if (cardsNote.textContent !== text) cardsNote.textContent = text;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('spark-rail')) return;
    initSparkDrawers();
    initSparkTune();
    initSparkRailNav();
    updateSparkRail();
    // 心跳只服务两件事：进行中的秒表、以及合成状态的开始/结束（那是 app.js 内部
    // 的 generationState 变更，不想为了显示去改它的每一处赋值）。签名不变不写 DOM。
    setInterval(updateSparkRail, 1000);
});
