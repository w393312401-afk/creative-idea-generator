/* =====================================================================
   手动上传提示词集（项目工作台工具条 · 📥 上传提示词集）
   ---------------------------------------------------------------------
   在此之前，一份提示词集只有一个来源：本机跑一次激发/合成。手上已经写好、
   或者从别处（另一台机器的输出、Skill 直接跑出来的 .md、聊天记录里粘出来的
   一段）拿到的整份提示词集，进不了这套流程——而帧序列/视频/成片全都挂在
   「创意 + prompt_block」这一对上。这里补上导入口：选个 .md/.txt，或者直接
   把文本粘进来。

   入口挂在项目工作台（与「导入 JSON / 全部导出」同排），因为导入的结果就是那张
   表上多出来的一行：一次导入 = 一个新项目，落库后自动打开它的激发结果页。它从不
   改写已有项目的提示词——改已有一单走结果页的「✏️ 手动编辑」。

   核心是「认出常见写法、保留真实正文」：外面写出来的集子几乎不会正好落在本项目的
   槽位契约上（图片提示词 / 图片 N: / 视频提示词 / 视频 N:）。常见的歪法与
   本模块的处理一一对应，见 normalizePromptSetText：
     · ```代码围栏、Markdown 标题（## 图片 1）、加粗（**图片 1:**）、
       列表符号（- 图片 1:）、全角冒号（图片 1：）、缺冒号（图片 1）；
     · 英文/别名标签（IMAGE 1 / IMG 1 / Frame 1 / VIDEO 1 / VID 1 / Clip 1）；
     · 正文与标签挤在同一行（前端老解析器会把这种正文静默丢掉，是两次事故的
       前提，这里一律把正文挪到下一行）；
     · Markdown 标题与代码块重复写了同一个槽位号 → 合并，不生成空白拍；
     · 图片编号正规化时同步映射视频编号，视频缺段只提示，不造占位正文；
     · 保留导入原文，便于后续核对；不因补全规则丢弃已有视频。

   两条底线，和手动编辑一致：
     1) 槽位号是契约不是标签——图片必须从 1 连续编到 N，视频必须落在 1..N 内。
        本模块只做"补全成合规"，补不出来就报错、不导入。
     2) 导入不是静默落库。改了什么一律列进确认弹窗，人点了「确定」才落地，
        且落地前必过一次 /api/edit_prompts —— 服务端那道槽位契约校验才是权威。
   ===================================================================== */

// 别名表：长的排前面（正则 alternation 按序匹配，'图片' 必须先于 '图'）
const PROMPT_IMPORT_ALIAS_IMAGE =
    '图片|图像|画面|静帧|帧|图|images?|imgs?|frames?|stills?|photos?|pi(?:c|cture)s?';
const PROMPT_IMPORT_ALIAS_VIDEO =
    '视频|片段|镜头|动态|videos?|vids?|clips?|shots?|motions?';

function buildPromptImportHeaderRe(alias) {
    return new RegExp(
        '^(?:' + alias + ')'                                 // 别名
        + '(?:\\s*提示词|\\s*提示|\\s*prompt)?'                // 「图片提示词 3:」
        + '[\\s_-]*(?:第|no\\.?|#)?\\s*(\\d{1,4})(?!\\d)'      // IMG_001 / VIDEO-041
        + '\\s*(?:张|段|帧|号|个)?'                            // 「图片第 3 张:」
        + '((?:\\s*(?:[（\\(].*?[）\\)]|\\[.*?\\]))*)'         // 2 tags（[META] 与 （简介））
        + '\\s*([:：]|[-–—=>|~]+|、|\\.)?'                     // 3 分隔符
        + '\\s*([\\s\\S]*)$',                                 // 4 同行正文
        'i');
}

const PROMPT_IMPORT_IMAGE_HEADER_RE = buildPromptImportHeaderRe(PROMPT_IMPORT_ALIAS_IMAGE);
const PROMPT_IMPORT_VIDEO_HEADER_RE = buildPromptImportHeaderRe(PROMPT_IMPORT_ALIAS_VIDEO);

// 分节标题（图片提示词 / 视频提示词 / IMAGE PROMPTS …）：跳过，不进正文
const PROMPT_IMPORT_SECTION_RE =
    /^(?:图片|视频|image|video)\s*(?:提示词|提示|prompts?)\s*[:：]?$/i;
// 纯分隔线
const PROMPT_IMPORT_RULE_RE = /^(?:-{3,}|={3,}|\*{3,}|_{3,}|—{3,})$/;
// 合成器输出的分节标记（===TITLE=== / ===THEME=== / ===PROMPTS=== / ===AUDIT===）。
// 它们不是分隔线（中间夹着字），也不是 Markdown 小标题，所以两条既有规则都拦不住：
// 直接从 Skill 输出复制过来的集子，末尾那段 `===AUDIT===` + 审核说明会被接到最后
// 一段视频的正文里，然后原样送去渲染。
const PROMPT_IMPORT_MARKER_RE = /^={2,}\s*([^=]{1,40}?)\s*={2,}$/;
// 其中只有「提示词」这一节装的是槽位正文，遇到它是解除断开、不是开始断开
const PROMPT_IMPORT_MARKER_PROMPTS_RE =
    /^(?:image|video|图片|视频)?\s*(?:prompts?|提示词|提示)$/i;
// 代码围栏
const PROMPT_IMPORT_FENCE_RE = /^\s*(?:```|~~~)/;

/**
 * 去掉一行外面裹着的 Markdown 装饰，只为了判断"它是不是槽位头行"。
 * 正文行永远用原文，不受这里影响。
 */
function stripPromptImportDecorations(line) {
    let s = String(line || '').trim();
    s = s.replace(/^#{1,6}\s*/, '');        // ## 图片 1
    s = s.replace(/^>\s*/, '');             // > 图片 1
    s = s.replace(/^[-*+•·]\s+/, '');       // - 图片 1
    s = s.replace(/^\d+[.)、]\s+/, '');      // 1) 图片 1
    // 只去掉包在标题外面的成对标记，不能改掉同行正文中的下划线或 Markdown。
    const wrap = s.match(/^(\*\*|__|`{1,3}|\*)/);
    if (wrap) {
        s = s.slice(wrap[1].length);
        const end = s.indexOf(wrap[1]);
        if (end >= 0) s = s.slice(0, end) + s.slice(end + wrap[1].length);
    }
    return s.trim();
}

/**
 * 宽松解析一行是否槽位头行。返回 {type, index, meta, summary, inline} 或 null。
 *
 * 判定为头行的条件：别名 + 槽位号命中，**且**（有冒号类分隔符 或 该行到此为止）。
 * 少了后半条，视频正文里那句 "IMAGE 1 as the actual first-frame image…" 会被
 * 当成「图片 1」的头行，把一段视频提示词劈成两半。
 */
function matchPromptImportHeader(line) {
    const s = stripPromptImportDecorations(line);
    if (!s || PROMPT_IMPORT_SECTION_RE.test(s)) return null;
    for (const [type, re] of [['image', PROMPT_IMPORT_IMAGE_HEADER_RE],
                              ['video', PROMPT_IMPORT_VIDEO_HEADER_RE]]) {
        const m = s.match(re);
        if (!m) continue;
        const tags = m[2] || '';
        const summaryMatch = tags.match(/[（\(](.*?)[）\)]/);
        let summary = summaryMatch ? summaryMatch[1].trim() : '';
        const metaMatch = tags.match(/\[(.*?)\]/);
        let meta = metaMatch ? metaMatch[1].trim() : '';
        // 括号里写的是标记词而不是简介时收进 meta。判据与 js/prompt_pipeline.js 的
        // 两处解析共用一份 SLOT_META_TAG_RE——三处必须认出同一批标记，否则同一份
        // 文本"看到的"和"存下来的"槽位语义会分叉。这里尤其要紧：认不出 HERO，
        // 下面的 heroVideos 就筛不到它，收尾英雄段会被当成普通段重编号。
        if (!meta && typeof SLOT_META_TAG_RE !== 'undefined' && SLOT_META_TAG_RE.test(summary)) {
            meta = summary.toUpperCase().replace(/\s+/g, ' ');
            summary = '';
        }
        const sep = m[3] || '';
        let inline = (m[4] || '').trim();
        const isColon = /^[:：]$/.test(sep);
        // 标题里的首尾帧/时长是说明，不是新提示词，也不能把正文引用当成头行。
        const rangeLabel = /^(?:[|—–-]\s*)?(?:(?:IMAGE|IMG|图片|图像)[\s_-]*\d+\s*(?:→|->|=>|–|—|至|到|to)\s*(?:(?:IMAGE|IMG|图片|图像)[\s_-]*)?\d+)(?:\s*(?:[|·,，;；—–-]|\(|（|\[).*)?$/i;
        const markdownHeading = /^\s*#{1,6}\s+/.test(line);
        if (inline && !isColon) {
            if (rangeLabel.test(inline) || (markdownHeading && sep)) {
                summary = [summary, inline].filter(Boolean).join(' · ');
                inline = '';
            } else continue;
        }
        return {
            type,
            index: parseInt(m[1], 10),
            meta,
            summary,
            inline,
        };
    }
    return null;
}

function promptImportHeaderLine(type, index, meta, summary) {
    const label = type === 'image' ? '图片' : '视频';
    // 描述里常含“IMAGE 1 → IMAGE 2 (6s)”；内层括号不能提前闭合标准标题标签。
    const safeSummary = String(summary || '').replace(/[（(\[]/g, '〔').replace(/[）)\]]/g, '〕');
    const sumStr = safeSummary ? `（${safeSummary}）` : '';
    const metaStr = meta ? ` [${meta}]` : '';
    return `${label} ${index}${sumStr}${metaStr}:`;
}

/**
 * 把任意来源的一份提示词集正规化成本项目的槽位契约文本。
 *
 * 返回：
 *   { ok, error, text, imageCount, videoCount, fixes[], filledVideos[],
 *     emptyBodies[], droppedVideos[], suggestedTitle, hasPlaceholder }
 * fixes 是给人看的改动清单——导入前必须让人看见自动补了什么。
 */
function normalizePromptSetText(raw) {
    const fixes = [];
    let text = String(raw || '')
        .replace(/^\uFEFF/, '')
        .replace(/\r\n?/g, '\n')
        // 零宽字符：从聊天窗口/网页复制来的文本里常夹着，肉眼看不见，
        // 但足以让「图片 1:」的头行正则整行匹配不上
        .replace(/[\u200B-\u200D\u2060\uFEFF]/g, '');

    const lines = text.split('\n');
    const slots = [];       // 按出现顺序
    let current = null;
    let suggestedTitle = '';
    let preambleLines = 0;
    let fenceLines = 0;
    let droppedHeadings = 0;
    let droppedMarkers = 0;
    let droppedSectionLines = 0;
    let inlineMoved = 0;
    let repeatedHeaders = 0;
    // 正文中间遇到 Markdown 小标题后置位：它开了文档的新一节（「## 提示词质量审核报告」
    // 这类），后面的内容不再属于上一拍。不这么断，那份审核报告表格会被整段接到
    // 最后一段视频的提示词末尾，然后原样送去渲染。下一个槽位头行才解除。
    let skippingSection = false;

    const flush = () => {
        if (!current) return;
        current.body = current.bodyLines.join('\n').replace(/\s+$/, '').replace(/^\s*\n/, '');
        delete current.bodyLines;
        slots.push(current);
        current = null;
    };

    for (const rawLine of lines) {
        const trimmed = rawLine.trim();

        if (PROMPT_IMPORT_FENCE_RE.test(trimmed)) { fenceLines++; continue; }
        if (PROMPT_IMPORT_RULE_RE.test(trimmed)) continue;
        const stripped = stripPromptImportDecorations(trimmed);
        if (PROMPT_IMPORT_SECTION_RE.test(stripped)) continue;

        // ===AUDIT=== / ===THEME=== 这类分节标记：标记行本身丢掉，其下整节内容
        // 一并丢到下一个槽位头行为止（===PROMPTS=== 反过来，是解除断开）
        const marker = stripped.match(PROMPT_IMPORT_MARKER_RE);
        if (marker) {
            if (PROMPT_IMPORT_MARKER_PROMPTS_RE.test(marker[1])) {
                skippingSection = false;
            } else if (current) {
                droppedMarkers++;
                skippingSection = true;
            }
            continue;
        }

        const header = matchPromptImportHeader(rawLine);
        if (header) {
            // 常见粘贴格式：### VIDEO 29 → ```text → VIDEO 29: 正文。
            // 第二个标签属于同一段；旧逻辑会新增一条空段，随后把全部视频编号推后。
            if (current && current.type === header.type && current.srcIndex === header.index
                    && !current.bodyLines.some(l => l.trim())) {
                current.meta = header.meta || current.meta;
                current.summary = current.summary || header.summary;
                if (header.inline) {
                    current.bodyLines.push(header.inline);
                    inlineMoved++;
                }
                repeatedHeaders++;
                skippingSection = false;
                continue;
            }
            flush();
            skippingSection = false;
            current = {
                type: header.type,
                srcIndex: header.index,
                meta: header.meta,
                summary: header.summary,
                bodyLines: [],
            };
            if (header.inline) {
                current.bodyLines.push(header.inline);
                inlineMoved++;
            }
            continue;
        }

        if (!current) {
            // 首个槽位之前的说明文字：只留 Markdown 一级标题当创意标题候选
            if (!trimmed) continue;
            if (!suggestedTitle) {
                const h = trimmed.match(/^#{1,3}\s*(.+)$/);
                if (h) suggestedTitle = h[1].replace(/[*_`]/g, '').trim();
            }
            preambleLines++;
            continue;
        }

        // 正文中间夹着的 Markdown 小标题（## 提示词质量审核报告 / ## 第二阶段）不属于
        // 任何一拍的提示词——它连同其下的整节内容一起丢到下一个槽位头行为止
        if (/^#{1,6}\s+\S/.test(trimmed)) {
            droppedHeadings++;
            skippingSection = true;
            continue;
        }
        if (skippingSection) {
            if (trimmed) droppedSectionLines++;
            continue;
        }

        current.bodyLines.push(rawLine.replace(/\s+$/, ''));
    }
    flush();

    const images = slots.filter(s => s.type === 'image');
    const videos = slots.filter(s => s.type === 'video');

    if (!images.length) {
        return {
            ok: false,
            error: '这份文本里解析不到任何图片提示词。每一拍要以「图片 N:」'
                 + '（IMAGE N: / IMG N: 也认）单独起一行，正文写在它下面。',
        };
    }

    if (fenceLines) fixes.push(`去掉了 ${fenceLines} 行 \`\`\` 代码围栏`);
    if (preambleLines) fixes.push(`丢弃了提示词集之前的 ${preambleLines} 行说明文字`);
    if (droppedHeadings || droppedMarkers) {
        const what = [];
        if (droppedHeadings) what.push(`${droppedHeadings} 处 Markdown 小标题`);
        if (droppedMarkers) what.push(`${droppedMarkers} 处 ===XXX=== 分节标记`);
        fixes.push(`丢弃了 ${what.join(' 和 ')}`
            + (droppedSectionLines ? `及其下的 ${droppedSectionLines} 行内容` : '')
            + '（不属于任何一拍，如附在末尾的质量审核报告）');
    }
    if (inlineMoved) fixes.push(`把 ${inlineMoved} 处与标签挤在同一行的正文挪到了下一行`);
    if (repeatedHeaders) fixes.push(`合并了 ${repeatedHeaders} 处标题与正文重复的槽位标签`);

    // 同一编号若对应不同正文，不能再按出现次序重编号：那会把视频绑到错误的图。
    // 完全相同的重复块可安全去重；其余情况保留原文并报告冲突。
    const deduplicate = (items, label) => {
        const seen = new Map();
        for (const slot of items) {
            const previous = seen.get(slot.srcIndex);
            if (!previous) { seen.set(slot.srcIndex, slot); continue; }
            if (previous.body === slot.body && previous.meta === slot.meta) {
                fixes.push(`去掉了重复出现的${label} ${slot.srcIndex}（正文相同）`);
                continue;
            }
            return { error: `${label} ${slot.srcIndex} 出现了不同的正文，无法确定对应关系。请保留需要的那一份后再导入。` };
        }
        return { items: Array.from(seen.values()) };
    };
    const uniqueImages = deduplicate(images, '图片');
    const uniqueVideos = deduplicate(videos, '视频');
    if (uniqueImages.error || uniqueVideos.error) {
        return { ok: false, error: uniqueImages.error || uniqueVideos.error };
    }
    images.splice(0, images.length, ...uniqueImages.items);
    videos.splice(0, videos.length, ...uniqueVideos.items);

    // 连续编号即使在文件中乱序也按编号排列。其他编号建立显式映射，视频沿用同一映射。
    const srcImageIndices = images.map(s => s.srcIndex);
    const sortedIndices = [...srcImageIndices].sort((a, b) => a - b);
    const canonical = sortedIndices.every((n, i) => n === i + 1);
    if (canonical) images.sort((a, b) => a.srcIndex - b.srcIndex);
    const imageIndexMap = new Map();
    images.forEach((s, i) => {
        s.index = i + 1;
        imageIndexMap.set(s.srcIndex, s.index);
    });
    if (!canonical) {
        fixes.push(`图片槽位号重编成 1–${images.length}`
            + `（原文是 ${srcImageIndices.join('/')}，对应视频同步映射）`);
    }
    const imageCount = images.length;
    const droppedVideos = [];
    const filledVideos = []; // 保留旧调用方字段；导入不再生成占位正文。
    const outVideos = [];
    for (const video of videos) {
        const index = /HERO/i.test(video.meta || '')
            ? imageCount : imageIndexMap.get(video.srcIndex);
        if (index === undefined) {
            return { ok: false, error: `视频 ${video.srcIndex} 没有对应的图片编号，无法确定首帧。原有视频未被丢弃，请核对编号后再导入。` };
        }
        if (outVideos.some(v => v.index === index)) {
            return { ok: false, error: `多段视频对应到图片 ${index}，请核对视频编号或 HERO 标记后再导入。` };
        }
        video.index = index;
        outVideos.push(video);
    }
    outVideos.sort((a, b) => a.index - b.index);
    if (outVideos.some(v => v.index !== v.srcIndex)) {
        fixes.push('视频槽位号已按对应图片同步映射，保留原有首帧关系');
    }
    const presentVideos = new Set(outVideos.map(v => v.index));
    const missingVideos = [];
    if (videos.length) {
        for (let k = 1; k < imageCount; k++) {
            if (!presentVideos.has(k)) missingVideos.push(k);
        }
    }

    // 没有正文就是没有可生成的内容，不能自动写一句“在此填写”冒充成功导入。
    const emptyBodies = images.concat(outVideos)
        .filter(s => !s.body || !s.body.trim())
        .map(s => `${s.type === 'image' ? '图片' : '视频'} ${s.index}`);
    if (emptyBodies.length) {
        return {
            ok: false,
            error: `${emptyBodies.join('、')} 没有提示词正文。请补上实际内容后再导入；不会自动生成占位提示词。`,
            emptyBodies,
        };
    }

    // ── 按契约布局重新输出 ──
    const out = ['图片提示词', ''];
    images.forEach(s => {
        out.push(promptImportHeaderLine('image', s.index, s.meta, s.summary), s.body, '');
    });
    if (outVideos.length) {
        out.push('视频提示词', '');
        outVideos.forEach(v => {
            out.push(promptImportHeaderLine('video', v.index, v.meta, v.summary), v.body, '');
        });
    }

    const normalized = out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
    const hasPlaceholder = typeof PROMPT_BEAT_PLACEHOLDER_MARK === 'string'
        && normalized.indexOf(PROMPT_BEAT_PLACEHOLDER_MARK) !== -1;

    return {
        ok: true,
        text: normalized,
        imageCount,
        videoCount: outVideos.length,
        fixes,
        filledVideos,
        missingVideos,
        emptyBodies,
        droppedVideos,
        suggestedTitle,
        hasPlaceholder,
        noVideos: !videos.length,
    };
}

/* ──────────────────────────────────────────────────────────────────────
   UI：弹窗（选文件 / 粘贴文本）→ 报告确认 → 落地
   ────────────────────────────────────────────────────────────────────── */

function promptImportReportHtml(report, sourceLabel) {
    const rows = [];
    rows.push(`<li>来源：<b>${escapeHtml(sourceLabel || '粘贴的文本')}</b></li>`);
    rows.push(`<li>解析结果：<b>${report.imageCount}</b> 张图片提示词`
        + ` / <b>${report.videoCount}</b> 段视频提示词</li>`);
    if (report.noVideos) {
        rows.push('<li>⚠️ 没有视频提示词，导入后只能生成帧序列</li>');
    }
    if (report.missingVideos && report.missingVideos.length) {
        rows.push(`<li>缺少视频 ${report.missingVideos.join('、')}，仅导入已有正文，不生成占位提示词</li>`);
    }
    if (report.fixes.length) {
        rows.push('<li>自动整理：<ul style="margin:4px 0 0 16px;">'
            + report.fixes.map(f => `<li>${escapeHtml(f)}</li>`).join('') + '</ul></li>');
    } else {
        rows.push('<li>已识别全部提示词，正文未作改动</li>');
    }
    if (report.hasPlaceholder) {
        rows.push('<li>⚠️ 含占位正文，<b>保存前必须替换成真提示词</b>（占位符会被当真提示词拿去渲染）</li>');
    }
    return '<b>导入这份提示词集？</b>'
        + `<ul style="margin:8px 0 0 18px; line-height:1.7;">${rows.join('')}</ul>`;
}

/**
 * 拿这份提示词集建一条新创意（= 项目工作台里的新一行）。
 *
 * 导入永远是"新建"，从不改写已经打开的那一单。入口在项目工作台上，那里一行就是
 * 一个项目；同一个按钮有时新建、有时悄悄覆盖当前结果页，是猜不出来的行为。要改
 * 已有一单的提示词，走结果页的「✏️ 手动编辑」（它有基准对账与标脏，见
 * js/prompt_editor.js）。
 *
 * project_key 自带 run_ 前缀（与 make_idea_project_key 同款）：媒体目录按它分家，
 * 导入两次同名集子不会共用同一份 manifest/帧文件。落库前先过一次
 * /api/edit_prompts —— 那是槽位契约的权威校验，也顺手回一份后端解析的 prompt_slots
 * （结构化槽位以后端为唯一权威，见 resolvePromptSlots）。
 */
async function importPromptSetAsNewIdea(report, sourceLabel) {
    const suggested = report.suggestedTitle
        || String(sourceLabel || '').replace(/\.(md|markdown|txt|text)$/i, '')
        || '导入的提示词集';
    let title = await customPrompt('给这份导入的提示词集起个标题（它同时是服务端项目目录名）：',
                                   escapeHtml(suggested));
    if (title === null) {
        showToast('已取消导入。', 'info');
        return false;
    }
    title = String(title).trim();
    if (!title) {
        showToast('标题不能为空，导入已取消。', 'error');
        return false;
    }
    const library = typeof libraryEntries === 'function' ? libraryEntries() : savedIdeas;
    if (Array.isArray(library) && library.some(it => it.title === title)) {
        showToast('点子库里已有同名创意，请换一个标题。', 'error');
        return false;
    }

    const stamp = Date.now();
    const projectKey = `run_import_${stamp}__${title}`;
    let promptSlots = null;
    try {
        const data = await slotPostJson('/api/edit_prompts', {
            title: projectKey,
            prompt_block: report.text,
            prev_prompt_block: '',
        });
        promptSlots = data && data.prompt_slots;
    } catch (e) {
        showToast(`提示词集没通过服务端校验，未导入：${e.message}`, 'error', 6000);
        return false;
    }

    const idea = {
        id: `import_${stamp}`,
        title,
        project_key: projectKey,
        theme: title,
        creativity: '手动导入',
        prompt_block: report.text,
        timestamp: new Date(stamp).toLocaleString(),
        timings: {},
        image_count: report.imageCount,
        video_count: report.videoCount,
        collage_url: '',
        covers: [],
        frameRun: null,
        english_title: '',
        social_title_en: '',
        social_title_cn: '',
        // 导入的集子没跑过质量门/二次校验，审核区如实写明，别让它看起来像"通过了"
        audit_md: '手动导入的提示词集，未经本机质量门与工序一致性二次校验。',
        repair_md: '',
        imported_at: new Date(stamp).toISOString(),
        imported_source_text: report.sourceText || report.text,
        imported_source_label: sourceLabel || '粘贴的文本',
    };
    if (promptSlots) idea.prompt_slots = promptSlots;

    if (Array.isArray(savedIdeas)) savedIdeas.unshift(idea);
    await persistIdeaItem(idea);
    if (typeof refreshProjects === 'function') refreshProjects({ assets: false });
    loadSavedIdea(idea, { toast: `已导入 ${report.imageCount} 拍提示词集，并存入点子库` });
    if (typeof switchMainTab === 'function') switchMainTab('results');
    if (typeof switchTab === 'function') switchTab('prompts');
    if (report.hasPlaceholder && typeof enterPromptEdit === 'function') {
        enterPromptEdit();
        showToast('这份集子里有占位正文，已直接打开手动编辑：填完再按「💾 保存」。', 'warning', 6000);
    } else {
        // 导入的单子没跑过激发收尾（那里才生成发布用的双语标题行），主题也只是照抄
        // 标题——工作台上这一行会既没主题也没话题。指一下结果页标题行那枚 ✨，
        // 它按提示词集正文一键补齐（见 app.js generateProjectMetaForCurrentIdea）。
        showToast('这一单还没有主题和 tags：点标题行的 ✨ 按提示词集一键生成中英双版。',
                  'info', 7000);
    }
    return true;
}

/** 正规化 → 把改动列给人看 → 建成新项目。 */
async function runPromptSetImport(raw, sourceLabel) {
    if (!String(raw || '').trim()) {
        showToast('没有可导入的内容。', 'error');
        return false;
    }
    const report = normalizePromptSetText(raw);
    if (!report.ok) {
        showToast(report.error, 'error', 7000);
        return false;
    }
    const ok = await customConfirm(promptImportReportHtml(report, sourceLabel)
        + '<ul style="margin:6px 0 0 18px; line-height:1.7;">'
        + '<li>落地方式：<b>新建一个项目</b>（存入点子库，随后自动打开它的激发结果页）'
        + '——导入不会改动任何已有项目</li></ul>');
    if (!ok) {
        showToast('已取消导入。', 'info');
        return false;
    }
    report.sourceText = String(raw);
    return importPromptSetAsNewIdea(report, sourceLabel);
}

async function readPromptSetFile(file) {
    const text = await file.text();
    return runPromptSetImport(text, file.name);
}

/** 上传弹窗：选文件 / 拖进来 / 直接粘贴，三条入口一个窗口。 */
function openPromptImportDialog() {
    const modal = document.createElement('div');
    modal.className = 'modal active';
    modal.id = 'prompt-import-modal';
    modal.style.zIndex = '1100';
    modal.innerHTML = `
        <div class="modal-content glass-panel" style="max-width: 620px; border-color: var(--neon-cyan);">
            <div class="modal-header">
                <h3>📥 上传提示词集</h3>
                <button class="close-btn">&times;</button>
            </div>
            <div class="modal-body" style="padding-top: 10px;">
                <p style="margin-bottom: 10px; font-size: 13px; line-height: 1.7; color: var(--text-secondary);">
                    选一个 <b>.md / .txt</b> 文件，或把整份提示词集粘到下面的框里。
                    自动识别 <code>图片 N:</code> / <code>视频 N:</code>、IMAGE/IMG/VIDEO/VID、
                    Markdown 标题、代码块和重复标签，并保留原始文件内容。
                    缺少的视频会列出提示，不再填入占位文字，也不会把后面的段落挤到错误编号。
                </p>
                <div class="prompt-import-drop" id="prompt-import-drop" style="border:1px dashed rgba(0,242,254,0.45); border-radius:8px; padding:14px; text-align:center; margin-bottom:12px; cursor:pointer;">
                    <span style="font-size:13px; color: var(--text-secondary);">点击选择文件，或把 .md / .txt 拖到这里</span>
                    <input type="file" id="prompt-import-file" accept=".md,.markdown,.txt,.text,text/plain,text/markdown" style="display:none;">
                </div>
                <div class="form-group" style="margin-bottom: 0;">
                    <textarea id="prompt-import-text" rows="10" spellcheck="false"
                        placeholder="图片提示词&#10;图片 1:&#10;……&#10;&#10;视频提示词&#10;视频 1:&#10;……"
                        style="width:100%; border-color: rgba(255,255,255,0.15); resize: vertical; font-family: var(--font-mono, monospace); font-size:12px;"></textarea>
                </div>
            </div>
            <div class="modal-footer">
                <button class="action-btn text-btn secondary cancel-btn">取消</button>
                <button class="action-btn text-btn primary confirm-btn" style="background: var(--neon-cyan); border-color: rgba(0,242,254,0.4); color:#000; font-weight:700;">解析并导入</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const close = () => {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 200);
    };
    const drop = modal.querySelector('#prompt-import-drop');
    const fileInput = modal.querySelector('#prompt-import-file');
    const textarea = modal.querySelector('#prompt-import-text');

    drop.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
        const file = fileInput.files && fileInput.files[0];
        fileInput.value = '';
        if (!file) return;
        close();
        await readPromptSetFile(file);
    });
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, (e) => {
        e.preventDefault();
        drop.style.background = 'rgba(0,242,254,0.08)';
    }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, () => {
        drop.style.background = '';
    }));
    drop.addEventListener('drop', async (e) => {
        e.preventDefault();
        const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (!file) return;
        close();
        await readPromptSetFile(file);
    });

    modal.querySelector('.close-btn').addEventListener('click', close);
    modal.querySelector('.cancel-btn').addEventListener('click', close);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });
    modal.querySelector('.confirm-btn').addEventListener('click', async () => {
        const raw = textarea.value;
        if (!raw.trim()) {
            showToast('先选个文件或把提示词集粘进来。', 'error');
            return;
        }
        close();
        await runPromptSetImport(raw, '粘贴的文本');
    });
    textarea.focus();
}

function initPromptImport() {
    // 入口在项目工作台的工具条上，与「导入 JSON / 全部导出」同排：那一页一行就是
    // 一个项目，导入一份提示词集就是多出一行，语义正好落在这里。
    const btn = document.getElementById('projects-import-prompt-btn');
    if (btn) btn.addEventListener('click', openPromptImportDialog);
}
