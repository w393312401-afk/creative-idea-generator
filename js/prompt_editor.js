/* =====================================================================
   手动编辑提示词集（提示词页 · #prompt-box）
   ---------------------------------------------------------------------
   合成出来的提示词不一定条条都对味。在此之前，改一条提示词只有两条路：
   重新激发整单（其余 N-1 条也跟着变），或者走 fix_frame_issue 让模型
   按问题描述改写（改成什么样不由人说了算）。这里补上第三条：直接改，
   以及在末尾添加新的一拍。

   两条红线，都在保存时校验（前端先拦一道、后端 /api/edit_prompts 再拦
   一道，后端那道才是权威）：
     1) 图片槽位号必须从 1 连续编到 N。槽位号是契约不是标签——帧网格、
        配对门禁、合成成片全按它推算，缺一格下游每一处都得学会跳过它。
     2) 手动编辑只能改写与追加，不能删拍。删拍要同时动磁盘文件、manifest
        编号与恢复快照，那是帧卡片上「删除」（/api/delete_slot）的活。

   保存之后：提示词改过、画面还没跟上的帧会被后端标 prompt_dirty，卡片上
   显示「提示词已改」徽标（见 js/slot_model.js）；新追加的拍以空槽位出现
   在帧序列里，等着生成。
   ===================================================================== */

// 「➕ 添加一拍」填进去的占位正文。保存前必须被真正的提示词替换掉——
// 占位符原样存进去等于交付一条空提示词，渲染时会照着这行字画图。
const PROMPT_BEAT_PLACEHOLDER_MARK = '（在此填写';

function promptBeatImagePlaceholder(n) {
    return `（在此填写第 ${n} 张图片的提示词：这一拍画面里发生了什么、`
        + '与上一张的差别在哪，沿用本单既有的镜头/光线/材质约定）';
}

function promptBeatVideoPlaceholder(n) {
    return `（在此填写第 ${n} 段视频的提示词：从 IMG ${padSlot(n)} 运动到 `
        + `IMG ${padSlot(n + 1)} 的这几秒里，镜头与画面如何变化）`;
}

const PROMPT_SLOT_HEADER_RE = /^\s*(图片|视频)\s*(\d+)\s*(?:\[(.*?)\])?\s*:/;
const PROMPT_SECTION_HEADER_RE = /^\s*(图片提示词|视频提示词)\s*$/;

let promptEditorOpen = false;
// 进入编辑态那一刻的基准：哪一单、当时的提示词块长什么样。保存前拿它对一次账——
// 编辑期间别的路径（修复此帧 / 撤销修复 / 删除整拍）也会改写 prompt_block，
// 不对账就会拿一份基于旧文本的整块覆盖，把那些改写静静抹掉。
let promptEditorBaseId = null;
let promptEditorBaseBlock = '';

function promptEditorEls() {
    return {
        box: document.getElementById('prompt-box'),
        pre: document.getElementById('idea-prompt-block'),
        textarea: document.getElementById('idea-prompt-editor'),
        hint: document.getElementById('prompt-edit-hint'),
        editBtn: document.getElementById('edit-prompt-btn'),
        addBtn: document.getElementById('add-beat-btn'),
        saveBtn: document.getElementById('save-prompt-edit-btn'),
        cancelBtn: document.getElementById('cancel-prompt-edit-btn'),
        copyBtn: document.getElementById('copy-prompt-btn'),
    };
}

function setPromptEditorMode(on) {
    const el = promptEditorEls();
    if (!el.textarea || !el.pre) return;
    promptEditorOpen = !!on;
    el.pre.hidden = promptEditorOpen;
    el.textarea.hidden = !promptEditorOpen;
    if (el.hint) el.hint.hidden = !promptEditorOpen;
    if (el.editBtn) el.editBtn.hidden = promptEditorOpen;
    if (el.addBtn) el.addBtn.hidden = !promptEditorOpen;
    if (el.saveBtn) el.saveBtn.hidden = !promptEditorOpen;
    if (el.cancelBtn) el.cancelBtn.hidden = !promptEditorOpen;
    // 编辑态下复制按钮会复制 pre 里的旧文本，与眼前正在改的内容不一致，先收起来
    if (el.copyBtn) el.copyBtn.hidden = promptEditorOpen;
    if (!promptEditorOpen) {
        promptEditorBaseId = null;
        promptEditorBaseBlock = '';
        el.textarea.value = '';
        el.textarea.disabled = false;
    }
}

/**
 * 退出编辑态并丢弃未保存的改动。切换创意/重新渲染结果时必须调用——
 * 编辑器里躺着的是上一个创意的提示词，留在屏幕上一保存就写串了单。
 */
function resetPromptEditor() {
    if (!promptEditorOpen) return;
    setPromptEditorMode(false);
}

function enterPromptEdit() {
    const el = promptEditorEls();
    if (!currentIdea || !currentIdea.prompt_block) {
        showToast('请先激发一个创意点子，或从创意库里打开一单', 'error');
        return;
    }
    if (typeof isIdeaTaskActive === 'function'
            && (isIdeaTaskActive(currentIdea.id, 'frames') || isIdeaTaskActive(currentIdea.id, 'videos'))) {
        showToast('该创意的帧/视频序列正在生成中，等它结束后再改提示词', 'error');
        return;
    }
    promptEditorBaseId = currentIdea.id;
    promptEditorBaseBlock = currentIdea.prompt_block;
    el.textarea.value = currentIdea.prompt_block;
    el.textarea.disabled = false;
    setPromptEditorMode(true);
    el.textarea.focus();
    el.textarea.setSelectionRange(0, 0);
    el.textarea.scrollTop = 0;
}

function cancelPromptEdit() {
    const el = promptEditorEls();
    const dirty = currentIdea && el.textarea
        && el.textarea.value !== (currentIdea.prompt_block || '');
    if (!dirty) {
        setPromptEditorMode(false);
        return;
    }
    customConfirm('放弃本次对提示词集的改动？').then(ok => {
        if (!ok) return;
        setPromptEditorMode(false);
        showToast('已放弃改动。', 'info');
    });
}

/**
 * 保存前的本地校验。与后端 /api/edit_prompts 的规则一一对应——这一道只是
 * 让用户不用等一次网络往返就看到问题，权威判定始终在后端。
 * 返回 {ok, error, imageCount}。
 */
function validatePromptEditorText(text, prevBlock) {
    const slots = parsePromptBlock(text);
    const images = slots.filter(s => s.type === 'image');
    const videos = slots.filter(s => s.type === 'video');
    if (!images.length) {
        return { ok: false, error: '解析不到任何「图片 N:」槽位。每一拍都要以「图片 N:」开头单独成行。' };
    }
    const indices = images.map(s => s.index);
    const imageCount = Math.max.apply(null, indices);
    const seen = new Set(indices);
    if (seen.size !== indices.length) {
        const dup = indices.filter((n, i) => indices.indexOf(n) !== i);
        return { ok: false, error: `图片槽位号重复：${[...new Set(dup)].map(n => `图片 ${n}`).join('、')}` };
    }
    const missing = [];
    for (let n = 1; n <= imageCount; n++) if (!seen.has(n)) missing.push(n);
    if (missing.length) {
        return { ok: false, error: `图片槽位号必须从 1 连续编到 ${imageCount}，缺少：`
            + missing.map(n => `图片 ${n}`).join('、') };
    }
    const stray = videos.map(s => s.index).filter(n => n < 1 || n > imageCount);
    if (stray.length) {
        return { ok: false, error: `视频槽位号必须落在 1–${imageCount} 内，越界的有：`
            + [...new Set(stray)].map(n => `视频 ${n}`).join('、') };
    }
    const placeholder = slots.filter(s => s.body.indexOf(PROMPT_BEAT_PLACEHOLDER_MARK) !== -1);
    if (placeholder.length) {
        return { ok: false, error: `还有 ${placeholder.length} 条占位正文没填（${
            placeholder.map(s => s.label).join('、')}）。占位符会被当成真提示词拿去渲染。` };
    }
    const prevImages = parsePromptBlock(prevBlock || '').filter(s => s.type === 'image');
    const prevCount = prevImages.length
        ? Math.max.apply(null, prevImages.map(s => s.index)) : 0;
    if (prevCount && imageCount < prevCount) {
        return { ok: false, error: `手动编辑不能减少拍数（原 ${prevCount} 拍，现 ${imageCount} 拍）。`
            + '删拍请用帧卡片上的「删除」——那条路径才会同时处理磁盘文件、编号前移与恢复快照。' };
    }
    return { ok: true, imageCount };
}

/**
 * 在文本末尾追加一拍：图片 N+1，以及把它接上来的视频 N。
 *
 * 英雄展示视频（[HERO]，收尾全景）的槽位号恒等于最后一张图，所以追加一拍时
 * 它要整体后挪一位，腾出的 N 号才是新的普通段——与 /api/delete_slot 里
 * `_video_target` 对英雄段的特殊处理是同一条约定，反过来做一遍。
 *
 * 纯文本插入、不重排全文：手动编辑要的就是"我写成什么样就是什么样"，
 * 整块 reformat 会把用户自己加的注释/分节顺手抹掉。
 */
function appendBeatToPromptText(text) {
    const lines = (text || '').split('\n');
    const slots = parsePromptBlock(text);
    const images = slots.filter(s => s.type === 'image');
    const videos = slots.filter(s => s.type === 'video');
    if (!images.length) return null;

    const imageCount = Math.max.apply(null, images.map(s => s.index));
    const newImageIndex = imageCount + 1;
    const heroSlot = videos.find(v => /HERO/i.test(v.meta || ''));
    // 英雄段占着 N 号时，新普通段仍是 N（英雄段让位后挪到 N+1）
    const newVideoIndex = imageCount;

    /** 某个槽位的头行在 lines 里的下标（按类型+槽位号找，正文里的同形文字不会误命中）。 */
    const headerLineOf = (type, index) => lines.findIndex(l => {
        const m = l.match(PROMPT_SLOT_HEADER_RE);
        return !!m && m[1] === (type === 'image' ? '图片' : '视频') && Number(m[2]) === index;
    });

    /** 从头行往后扫到这一段正文的末尾（下一个头行/分节标题之前）。 */
    const bodyEndAfter = (headerLine) => {
        let j = headerLine + 1;
        while (j < lines.length
               && !PROMPT_SLOT_HEADER_RE.test(lines[j])
               && !PROMPT_SECTION_HEADER_RE.test(lines[j])) j++;
        return j;
    };

    /** 在 at 处插入一段，并保证它与前一段之间隔一个空行。 */
    const insertBlock = (at, block) => {
        const needsGap = at > 0 && lines[at - 1].trim() !== '';
        lines.splice(at, 0, ...(needsGap ? [''] : []), ...block);
    };

    // ── 视频先插：图片插入会改变行号，反过来做就得重算一次 ──
    if (heroSlot) {
        const heroLine = headerLineOf('video', heroSlot.index);
        if (heroLine !== -1) {
            const meta = heroSlot.meta ? ` [${heroSlot.meta}]` : '';
            lines[heroLine] = lines[heroLine].replace(
                PROMPT_SLOT_HEADER_RE, `视频 ${heroSlot.index + 1}${meta}:`);
            insertBlock(heroLine, [`视频 ${newVideoIndex}:`, promptBeatVideoPlaceholder(newVideoIndex), '']);
        }
    } else if (imageCount >= 1) {
        const lastVideo = videos.length
            ? videos.reduce((a, b) => (a.index > b.index ? a : b)) : null;
        const anchor = lastVideo ? headerLineOf('video', lastVideo.index) : -1;
        const at = anchor !== -1 ? bodyEndAfter(anchor) : lines.length;
        insertBlock(at, [`视频 ${newVideoIndex}:`, promptBeatVideoPlaceholder(newVideoIndex), '']);
    }

    const lastImageLine = headerLineOf('image', imageCount);
    const imageAt = lastImageLine !== -1 ? bodyEndAfter(lastImageLine) : lines.length;
    insertBlock(imageAt, [`图片 ${newImageIndex}:`, promptBeatImagePlaceholder(newImageIndex), '']);

    return { text: lines.join('\n'), imageIndex: newImageIndex, videoIndex: newVideoIndex };
}

function addBeatInPromptEditor() {
    const el = promptEditorEls();
    if (!el.textarea || el.textarea.hidden) return;
    const result = appendBeatToPromptText(el.textarea.value);
    if (!result) {
        showToast('当前文本里解析不到任何「图片 N:」槽位，无法在末尾追加一拍', 'error');
        return;
    }
    el.textarea.value = result.text;
    // 选中新插入的图片占位正文：追加之后马上要填的就是它，直接打字就替换掉
    const marker = `图片 ${result.imageIndex}:`;
    const at = el.textarea.value.indexOf(marker);
    el.textarea.focus();
    if (at !== -1) {
        const bodyStart = at + marker.length + 1;
        const bodyEnd = el.textarea.value.indexOf('\n', bodyStart);
        el.textarea.setSelectionRange(bodyStart, bodyEnd === -1 ? el.textarea.value.length : bodyEnd);
        // 追加点通常在整块文本的末尾，不主动滚过去就看不见刚加的那一拍
        el.textarea.scrollTop = el.textarea.scrollHeight;
    }
    showToast(`已追加第 ${result.imageIndex} 拍（图片 ${result.imageIndex} + 视频 ${result.videoIndex}），`
        + '请把占位正文替换成真正的提示词再保存', 'info');
}

async function savePromptEdit() {
    const el = promptEditorEls();
    const ownerIdea = currentIdea;
    if (!ownerIdea || !el.textarea) return false;

    // 编辑期间换了单：眼前这段文字属于另一条创意，存下去就是张冠李戴
    if (promptEditorBaseId && ownerIdea.id !== promptEditorBaseId) {
        showToast('编辑期间切换过创意，本次改动无法安全保存，已放弃。', 'error');
        setPromptEditorMode(false);
        return false;
    }
    // 编辑期间提示词块被别的路径改写过（修复此帧 / 撤销修复 / 删除整拍）
    if (promptEditorBaseBlock && (ownerIdea.prompt_block || '') !== promptEditorBaseBlock) {
        const overwrite = await customConfirm(
            '开始编辑之后，这一单的提示词已被别的操作改写过（如「修复此帧问题」或「删除整拍」）。'
            + '<br><br>继续保存会用你手上这份文本<b>整块覆盖</b>，那些改写将丢失。仍要保存吗？');
        if (!overwrite) {
            showToast('已取消保存。可先取消编辑、看过最新提示词后再改。', 'info');
            return false;
        }
    }

    const text = el.textarea.value;
    if (text.trim() === (ownerIdea.prompt_block || '').trim()) {
        setPromptEditorMode(false);
        showToast('提示词没有改动。', 'info');
        return false;
    }
    const check = validatePromptEditorText(text, ownerIdea.prompt_block);
    if (!check.ok) {
        showToast(check.error, 'error');
        return false;
    }

    const prevSlots = parsePromptBlock(ownerIdea.prompt_block || '');
    const prevCount = prevSlots.filter(s => s.type === 'image').length;
    const added = Math.max(0, check.imageCount - prevCount);
    const proceed = await customConfirm(
        '<b>保存手动改写的提示词集</b>：'
        + '<ul style="margin:8px 0 0 18px; line-height:1.7;">'
        + `<li>本单共 ${check.imageCount} 拍`
        + (added ? `（其中 <b>新增 ${added} 拍</b>，以空槽位出现在帧序列里等待生成）` : '')
        + '</li>'
        + '<li>提示词改过、画面还没跟上的帧会标上「提示词已改」徽标，按新提示词重渲后自动消失</li>'
        + '<li>已合并的成片会作废（它是按旧提示词渲的素材拼的）</li>'
        + '</ul>'
    );
    if (!proceed) {
        showToast('已取消保存。', 'info');
        return false;
    }

    el.textarea.disabled = true;
    const ok = await mutateSlot({
        what: '保存提示词改动',
        ownerIdea, scope: 'both', requirePrompt: true,
        request: () => slotPostJson('/api/edit_prompts', {
            title: getIdeaSaveTitle(ownerIdea),
            prompt_block: text,
            // 改动的判定基准：服务端拿它逐槽位比对，只标真正改过的那几帧
            prev_prompt_block: ownerIdea.prompt_block || '',
        }),
        // 提示词块是槽位总数的源头，必须先落到创意对象上再重渲两张网格。
        // deferSave：紧随其后的 reloadManifestIntoIdea 会带声明把库写掉。
        beforeApply: async (d) => {
            await applyPromptBlockToIdea(ownerIdea, d.prompt_block, d.prompt_slots, true);
        },
        patch: (d) => ({ frames: d.frames, videos: d.videos, dropMerged: true }),
        success: (d) => `提示词已保存（共 ${d.image_count} 拍`
            + ((d.added_beats || []).length ? `，新增第 ${d.added_beats.join('、')} 拍` : '')
            + '）。',
        extraToasts: (d) => {
            const out = [];
            if ((d.dirty_frames || []).length) {
                out.push([`IMG ${d.dirty_frames.map(padSlot).join(' / ')} 的提示词改了、画面还是旧的，`
                    + '已标为待重渲。', 'info']);
            }
            if ((d.dirty_videos || []).length) {
                out.push([`VID ${d.dirty_videos.map(padSlot).join(' / ')} 的提示词改了，建议重跑这几段。`,
                    'info']);
            }
            if ((d.added_beats || []).length) {
                out.push([`新增的第 ${d.added_beats.join('、')} 拍已作为空槽位排进帧序列，`
                    + '在帧序列卡片上生成它们。', 'info']);
            }
            return out;
        },
        failure: (e) => `保存提示词失败: ${e.message}`,
    });

    el.textarea.disabled = false;
    if (ok) setPromptEditorMode(false);
    return ok;
}

function initPromptEditor() {
    const el = promptEditorEls();
    if (!el.textarea) return;
    if (el.editBtn) el.editBtn.addEventListener('click', enterPromptEdit);
    if (el.addBtn) el.addBtn.addEventListener('click', addBeatInPromptEditor);
    if (el.saveBtn) el.saveBtn.addEventListener('click', savePromptEdit);
    if (el.cancelBtn) el.cancelBtn.addEventListener('click', cancelPromptEdit);
    // Ctrl/⌘+S 保存、Esc 放弃：整块提示词动辄几千字，手不离键盘才改得动
    el.textarea.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            savePromptEdit();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelPromptEdit();
        }
    });
}
