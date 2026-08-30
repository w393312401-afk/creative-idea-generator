/* =====================================================================
   前置提示词静态合规审查（Pre-flight Prompt Linter Engine & Modal）
   ---------------------------------------------------------------------
   在「生成帧」或「保存提示词」前运行轻量级规则审查，拦截：
   1. 裸百分比符号（%）：容易引起画面文字污染
   2. 工业草写与缩写词（w/, img, vid, diff 等）
   3. 时空单调继承与零状态回退（过门/室内帧重复描述已清理破损）
   4. 道具生命周期与施工工具残留（软装/收尾阶段残留三脚架等）
   5. 材质反光与光效违规（高反光镜面湿地面、荧光灯带）
   6. 结构完整性（未填占位符、槽位缺失等）
   ===================================================================== */

const PROMPT_LINTER_RULES = [
    {
        id: 'RULE_PERCENT_SYMBOL',
        name: '百分比符号违规',
        severity: 'warning',
        autoFixable: true,
        test: (slot) => {
            // 匹配正文中的裸 % 符号
            const text = slot.body || '';
            const matches = text.match(/\b\d+\s*%|%/g);
            if (matches && matches.length) {
                return {
                    message: `正文包含 ${matches.length} 处裸百分比符号（%）。部分出图模型会将 '%' 字符直接渲染到画面中造成文字污染，建议转写为自然语言（如 '50 percent' 或 'about half'）。`,
                    matched: matches.slice(0, 3).join('、') + (matches.length > 3 ? ' 等' : ''),
                    fixSuggestion: '自动将所有 "%" 替换为 " percent"'
                };
            }
            return null;
        },
        fix: (text) => {
            return (text || '').replace(/(\d+)\s*%/g, '$1 percent').replace(/%/g, ' percent');
        }
    },
    {
        id: 'RULE_ABBREVIATION',
        name: '非规范草写/缩写词',
        severity: 'warning',
        autoFixable: true,
        test: (slot) => {
            const text = slot.body || '';
            // 正文中排除提示词标头 (如 IMAGE 1 / VID 1) 之后的草写缩写
            const abbrRegex = /(?:\bw\/|\bb\/c\b|\bapprox\.|\bmin\.|\bmax\.|\btemp\.|\bpic\b|\bpics\b|\bref\b|\brefs\b|\bbg\b|\bfg\b|\bprops\b)/gi;
            const matches = text.match(abbrRegex);
            if (matches && matches.length) {
                const unique = Array.from(new Set(matches.map(m => m.toLowerCase())));
                return {
                    message: `正文包含非规范草写词：${unique.join('、')}。工业草写易导致视觉模型语义理解漂移，建议展开为完整自然语言英文。`,
                    matched: unique.join('、'),
                    fixSuggestion: '展开为标准单词（如 with, because, approximately, background, foreground）'
                };
            }
            return null;
        },
        fix: (text) => {
            if (!text) return text;
            return text
                .replace(/\bw\/(?=\s|[.,;!]|$)/gi, 'with')
                .replace(/\bb\/c\b/gi, 'because')
                .replace(/\bapprox\./gi, 'approximately')
                .replace(/\bbg\b/gi, 'background')
                .replace(/\bfg\b/gi, 'foreground')
                .replace(/\bpic\b/gi, 'picture')
                .replace(/\bpics\b/gi, 'pictures');
        }
    },
    {
        id: 'RULE_CHRONO_REGRESSION',
        name: '时空单调继承与状态倒退',
        severity: 'warning',
        autoFixable: false,
        test: (slot, ctx) => {
            const text = (slot.body || '').toLowerCase();
            const meta = (slot.meta || '').toLowerCase();
            const isCutOrBridge = /cut|bridge/i.test(meta);
            const isInterior = isCutOrBridge || /interior|indoor|chamber|hull|inside/i.test(text) || (ctx.isIndoorSequence && slot.index > (ctx.firstIndoorIndex || 1));
            
            if (isInterior && slot.index > 2) {
                const regressionWords = [
                    'leaking ceiling', 'cracked concrete floor', 'accumulated dead leaves',
                    'decaying debris piles', 'collapsed roof', 'water pooling on raw floor',
                    'heavy crystalline salt scaling and pale mineral brine',
                    'slumped clay banks', 'wash-out gullies', 'broken mud blocks',
                    'wind-scattered mineral-encrusted shale'
                ];
                const found = regressionWords.filter(w => text.includes(w));
                if (found.length) {
                    return {
                        message: `检测到在室内或中后期工序中重复描写室外已修复/清理的破损词（${found.join('、')}）。违反过门单调继承与零状态回退规则，可能导致已修缮画面在后续帧中倒退变回破损状态。`,
                        matched: found.join('、'),
                        fixSuggestion: '移除已被前期工序修复的破损描述，直接承接当前阶段的施工成果'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_TOOL_LIFECYCLE',
        name: '施工工具撤场残留',
        severity: 'warning',
        autoFixable: false,
        test: (slot, ctx) => {
            const text = (slot.body || '').toLowerCase();
            const meta = (slot.meta || '').toLowerCase();
            const isLateStage = /furnishing|reward|hero|staging|decorat|final|reveal/i.test(meta)
                || /tatami bed|soft furnishings|tea corner|tea table|linen mattresses|finished interior/i.test(text)
                || (ctx.totalSlots && slot.index >= ctx.totalSlots - 1);
            
            if (isLateStage) {
                const tools = [
                    'portable halogen work tripod', 'work tripods', 'work tripod',
                    'measuring laser', 'power cable', 'power cables', 'scaffolding',
                    'wheelbarrow', 'pneumatic nailer', 'power drill', 'construction equipment',
                    'construction tools', 'equipment rack'
                ];

                const toolMatches = tools.filter(t => {
                    const idx = text.indexOf(t);
                    if (idx === -1) return false;
                    const snippet = text.slice(Math.max(0, idx - 30), Math.min(text.length, idx + t.length + 30));
                    if (/no\s+|without\s+|cleared\s+|removed\s+|exit\s+|out\s+of\s+frame|strictly\s+forbidden/i.test(snippet)) return false;
                    return true;
                });

                // 检查孤立的 tripod 作为场景道具（排除 "static tripod shot", "tripod shot", "tripod perspective" 等镜头描述）
                const matches = text.match(/\b(?:halogen\s+|work\s+|portable\s+|unlit\s+|twin\s+)?tripods?\b/gi) || [];
                for (const m of matches) {
                    const idx = text.indexOf(m);
                    const snippet = text.slice(Math.max(0, idx - 20), Math.min(text.length, idx + m.length + 20));
                    if (/tripod\s+(?:shot|perspective|view|framing|angle|camera)/i.test(snippet) ||
                        /(?:static|locked|wide|18mm|14mm|lens)\s+tripod/i.test(snippet)) {
                        continue;
                    }
                    if (!toolMatches.includes(m)) {
                        toolMatches.push(m);
                    }
                }

                if (toolMatches.length) {
                    return {
                        message: `在软装陈设/收尾揭示帧（第 ${slot.index} 拍）的正向描述中检测到施工工具（${toolMatches.join('、')}）。根据道具生命周期契约，施工工具仅允许在前期阶段存在，完工帧必须销毁撤场。`,
                        matched: toolMatches.join('、'),
                        fixSuggestion: '在收尾帧正向提示词中移除施工工具，并确保负向提示词包含 (tripod, construction tools, power cables:1.4)'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_MATERIAL_VIOLATION',
        name: '材质与反光违规',
        severity: 'warning',
        autoFixable: false,
        test: (slot, ctx) => {
            const text = (slot.body || '').toLowerCase();
            const meta = (slot.meta || '').toLowerCase();
            const isFinishedOrLate = /flooring|furnishing|reward|hero/i.test(meta)
                || /teak wood|wood plank flooring|finished/i.test(text)
                || (ctx.totalSlots && slot.index >= ctx.totalSlots - 2);

            if (isFinishedOrLate) {
                const badVisuals = [
                    'wet floor', 'high glossy mirror reflection', 'mirror-like puddle',
                    'glossy water surface', 'zigzag neon', 'fluorescent mesh grid'
                ];
                const found = badVisuals.filter(v => text.includes(v));
                if (found.length) {
                    return {
                        message: `检测到违规材质或光效描述（${found.join('、')}）。地板铺设后必须保持温润哑光/半哑光实木质感，严禁突变为高反光镜面水面，灯光禁止出现杂乱荧光灯带。`,
                        matched: found.join('、'),
                        fixSuggestion: '修改为 matte/satin-finished natural timber floor 与 warm natural ambient lighting'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_STRUCTURE_SANITY',
        name: '未填占位符与槽位残缺',
        severity: 'error',
        autoFixable: false,
        test: (slot) => {
            const text = slot.body || '';
            if (text.includes('（在此填写')) {
                return {
                    message: `槽位中包含未填写的占位正文（"（在此填写..."）。占位符会被扩散模型当做正向提示词拿去直接渲染，产生严重坏帧。`,
                    matched: '（在此填写...）',
                    fixSuggestion: '请在提示词编辑器中将占位符替换为具体画面描述'
                };
            }
            if (!text.trim()) {
                return {
                    message: `槽位正文为空，缺少画面内容描述。`,
                    matched: '（空）',
                    fixSuggestion: '补齐该拍的画面与变化提示词'
                };
            }
            return null;
        }
    },
    {
        id: 'RULE_METRIC_SPACE_ENVELOPE',
        name: '公制空间尺寸与防透视畸变',
        severity: 'warning',
        autoFixable: false,
        test: (slot) => {
            const text = (slot.body || '').toLowerCase();
            const bannedPerspective = [
                'vanishing point', 'one-point perspective', 'long corridor',
                'endless narrow tunnel', 'bowling alley effect', 'train carriage perspective'
            ];
            const foundPerspective = bannedPerspective.filter(p => text.includes(p));
            if (foundPerspective.length) {
                return {
                    message: `检测到易引发空间拉伸管道化的高危透视词（${foundPerspective.join('、')}）。建议使用 3/4 对角斜透视（3/4 diagonal oblique perspective）与显式公制尺寸，避免 AI 将紧凑空间拉伸为 15 米保龄球道。`,
                    matched: foundPerspective.join('、'),
                    fixSuggestion: '替换为 3/4 diagonal oblique perspective，并声明公制三维包络（如 3.8m wide, 5.5m deep, 2.2m clearance）'
                };
            }

            const cavernousWords = ['cavernous hall', 'oversized room', 'giant space', 'cathedral-scale', 'two-tier bunk bed', 'double bunk bed'];
            const isCompactOrDiorama = /diorama|miniature|subterranean|excavation|bunker|container|pit|cellar|niche/i.test(text);
            if (isCompactOrDiorama) {
                const foundCavernous = cavernousWords.filter(c => text.includes(c));
                if (foundCavernous.length) {
                    return {
                        message: `在紧凑/微缩/地穴空间中检测到可能引发空间无序膨胀的词汇或超大人机道具（${foundCavernous.join('、')}）。AI 会为了容纳大件家具而将空间拉伸为巨大礼堂。`,
                        matched: foundCavernous.join('、'),
                        fixSuggestion: '降级为紧凑型人机工程道具（如 low-profile single timber platform daybed 或 recessed wall berth），并在负向词中加入 (cavernous hall, oversized room:1.4)'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_SINGLE_GROUND_BASELINE',
        name: '单向基准轴线与防菱形旋转',
        severity: 'warning',
        autoFixable: false,
        test: (slot) => {
            const text = (slot.body || '').toLowerCase();
            const skewWords = [
                'isometric diamond grid', 'tilted rotated layout', '45 degree oblique angle',
                'corner-on skewed perspective', 'rotated 45 degrees'
            ];
            const foundSkew = skewWords.filter(s => text.includes(s));
            if (foundSkew.length) {
                return {
                    message: `检测到易引发地基斜偏与整楼旋转的菱形视角描述（${foundSkew.join('、')}）。建筑主体与室外工序必须严格保持与画幅底边平行的水平横向基准线。`,
                    matched: foundSkew.join('、'),
                    fixSuggestion: '改为 horizontal baseline parallel to frame bottom，并注入负向词 (isometric diamond grid, tilted rotated layout:1.8)'
                };
            }

            const zenithWords = ['90 degree bird eye', 'vertical aerial map', 'flat orthographic plan', 'overhead blueprint view'];
            const foundZenith = zenithWords.filter(z => text.includes(z));
            if (foundZenith.length) {
                return {
                    message: `检测到 90° 纯垂直平面顶视描述（${foundZenith.join('、')}）。纯垂直正交视学会导致地平线与立体纵深丢失，室外工序应采用 45°~60° 高角度俯拍并保留天际地平线。`,
                    matched: foundZenith.join('、'),
                    fixSuggestion: '改为 elevated high-angle 45-60 degree perspective preserving distant horizon'
                };
            }
            return null;
        }
    },
    {
        id: 'RULE_LIVING_CAST_STATIC',
        name: '活物动态应激与防静态假人',
        severity: 'warning',
        autoFixable: false,
        test: (slot) => {
            const text = (slot.body || '').toLowerCase();
            const isDioramaOrWorkshop = /diorama|miniature|figurine|craftsman|workshop|sandtable/i.test(text);
            if (isDioramaOrWorkshop) {
                const staticPhrases = [
                    'remain standing', 'stay put', 'static in place', 'unchanged in place',
                    'standing at bottom-left observing', 'standing at bottom-right observing',
                    'static posture'
                ];
                const foundStatic = staticPhrases.filter(p => text.includes(p));
                if (foundStatic.length) {
                    return {
                        message: `检测到人偶/活物静态描述（${foundStatic.join('、')}）。在微缩沙盘与工坊中，活物是场景唯一生命体，严禁全序列钉死不动，必须随着施工动作产生入场应激、作业追踪或定格注视。`,
                        matched: foundStatic.join('、'),
                        fixSuggestion: '赋予人偶具体因果动态（如 eye tracking, leaning in to observe, shifting weight, raising hand in awe）'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_DESTITUTE_CAST_CLEANLINESS',
        name: '流浪落魄人偶与天地大景深',
        severity: 'warning',
        autoFixable: false,
        test: (slot) => {
            const text = (slot.body || '').toLowerCase();
            const isInitialOrDiorama = /diorama|miniature|refugee|vagrant|destitute|abandoned|ruin|excavation|earth/i.test(text) && slot.index <= 4;
            if (isInitialOrDiorama) {
                const cleanPhrases = ['clean royal blue shirt', 'crisp modern clothing', 'brand new floral dress', 'glossy shoes', 'neat luxury attire'];
                const foundClean = cleanPhrases.filter(c => text.includes(c));
                if (foundClean.length) {
                    return {
                        message: `在初期破旧/受助阶段检测到衣着光鲜描述（${foundClean.join('、')}）。流浪受助人偶初始应呈现做旧、尘土磨损的粗糙麻布/工装与沧桑憔悴神态。`,
                        matched: foundClean.join('、'),
                        fixSuggestion: '改为 distressed, dust-caked, grimy worn-out coarse clothing with weary realistic gaze'
                    };
                }
            }

            if (/diorama|miniature/i.test(text)) {
                const denseBokeh = ['creamy dense bokeh wall cutting off sky', 'dense studio bokeh wall', 'cutting off distant horizon'];
                const foundBokeh = denseBokeh.filter(b => text.includes(b));
                if (foundBokeh.length) {
                    return {
                        message: `检测到切断天际线的致密虚化墙描述（${foundBokeh.join('、')}）。微缩沙盘必须保持天地山水三层真实大景深（天际云层 + 中景主体 + 近景地表）。`,
                        matched: foundBokeh.join('、'),
                        fixSuggestion: '保留 3-Layer Environmental Depth: open daylight sky with drifting clouds, distant rolling hills, midground build, foreground mineral textures'
                    };
                }
            }
            return null;
        }
    },
    {
        id: 'RULE_CINEMATOGRAPHY_HEADER',
        name: '机位景别显式声明',
        severity: 'warning',
        autoFixable: false,
        test: (slot) => {
            if (slot.type !== 'image') return null;
            const text = (slot.body || '').trim();
            if (!text) return null;
            
            const firstSentence = text.split(/[.\n]/)[0].toLowerCase();
            const validCameraKeywords = [
                'shot', 'view', 'angle', 'perspective', 'camera', 'macro',
                'close-up', 'bird\'s-eye', 'bird-eye', 'high-angle', 'low-angle',
                'wide-angle', 'wide', 'oblique', 'panoramic', 'hero perspective',
                'tripod', 'eye level', 'eye-level', 'over-the-shoulder'
            ];
            const hasCameraHeader = validCameraKeywords.some(kw => firstSentence.includes(kw));
            if (!hasCameraHeader) {
                return {
                    message: `当前 IMAGE 提示词开篇第一句未显式声明具体的摄影机位与景别。根据电影级多机位调度铁律，每一拍开头应明确摄影角度（如 A high-angle 3/4 oblique shot, A low-angle dramatic upward-looking shot, A tight macro close-up 等），避免出图模型视角模糊漂移。`,
                    matched: firstSentence.slice(0, 45) + (firstSentence.length > 45 ? '...' : ''),
                    fixSuggestion: '在句首补充机位景别声明（如 "A dynamic high-angle 3/4 oblique shot..."）'
                };
            }
            return null;
        }
    }
];

/**
 * 对输入的提示词块执行全套静态合规审查 (Pre-flight Prompt Linter)
 * @param {string} promptBlockText 提示词全文
 * @param {Object} [options] 额外参数
 * @returns {Object} 审查结果报告
 */
function lintPromptBlock(promptBlockText, options = {}) {
    if (!promptBlockText || typeof promptBlockText !== 'string' || !promptBlockText.trim()) {
        return {
            passed: false,
            totalIssues: 1,
            errorCount: 1,
            warningCount: 0,
            issues: [{
                ruleId: 'RULE_EMPTY',
                ruleName: '内容为空',
                severity: 'error',
                slotType: 'global',
                slotIndex: 0,
                slotLabel: '全局',
                message: '提示词内容为空，请先激发创意或导入提示词。',
                matchedText: '',
                fixSuggestion: '生成或填入提示词'
            }]
        };
    }

    let slots = [];
    if (typeof parsePromptBlock === 'function') {
        slots = parsePromptBlock(promptBlockText);
    } else {
        // 简易兜底解析
        const lines = promptBlockText.split('\n');
        let cur = null, body = [];
        for (const line of lines) {
            const m = line.match(/^(?:图片|视频|IMAGE|VIDEO)\s*(\d+)/i);
            if (m) {
                if (cur) { cur.body = body.join('\n').trim(); slots.push(cur); }
                const isImg = /图片|image/i.test(line);
                cur = { type: isImg ? 'image' : 'video', index: parseInt(m[1], 10), label: `${isImg ? 'IMG' : 'VID'} ${m[1]}`, body: '' };
                body = [];
            } else if (cur) {
                body.push(line);
            }
        }
        if (cur) { cur.body = body.join('\n').trim(); slots.push(cur); }
    }

    const imageSlots = slots.filter(s => s.type === 'image');
    const totalImages = imageSlots.length;
    let isIndoorSequence = false;
    let firstIndoorIndex = 0;

    // 扫描过门切分点
    for (const s of imageSlots) {
        if (/cut|bridge/i.test(s.meta || '') || /interior|tanker hull|indoor/i.test(s.body || '')) {
            isIndoorSequence = true;
            firstIndoorIndex = s.index;
            break;
        }
    }

    const ctx = {
        totalSlots: totalImages,
        isIndoorSequence,
        firstIndoorIndex,
        ...options
    };

    const issues = [];
    for (const slot of slots) {
        const slotLabel = slot.type === 'image'
            ? `IMG ${String(slot.index).padStart(3, '0')}`
            : `VID ${String(slot.index).padStart(3, '0')}`;

        for (const rule of PROMPT_LINTER_RULES) {
            const res = rule.test(slot, ctx);
            if (res) {
                issues.push({
                    ruleId: rule.id,
                    ruleName: rule.name,
                    severity: rule.severity,
                    autoFixable: !!rule.autoFixable,
                    slotType: slot.type,
                    slotIndex: slot.index,
                    slotLabel: slotLabel,
                    message: res.message,
                    matchedText: res.matched || '',
                    fixSuggestion: res.fixSuggestion || ''
                });
            }
        }
    }

    const errorCount = issues.filter(i => i.severity === 'error').length;
    const warningCount = issues.filter(i => i.severity === 'warning').length;
    const passed = errorCount === 0 && warningCount === 0;

    return {
        passed,
        totalIssues: issues.length,
        errorCount,
        warningCount,
        hasAutoFixable: issues.some(i => i.autoFixable),
        issues
    };
}

/**
 * 自动修复提示词中所有可自动处理的违规项（如百分比符号 % 替换为 percent，规范缩写词等）
 */
function autoFixLintIssues(promptBlockText) {
    if (!promptBlockText || typeof promptBlockText !== 'string') return promptBlockText;
    let fixed = promptBlockText;
    for (const rule of PROMPT_LINTER_RULES) {
        if (rule.autoFixable && typeof rule.fix === 'function') {
            fixed = rule.fix(fixed);
        }
    }
    return fixed;
}

/**
 * 弹出前置提示词合规审查黄色预警弹窗
 * @param {Object} opts
 * @param {Object} opts.report 审查报告对象 (from lintPromptBlock)
 * @param {string} opts.actionTitle 当前操作（如 '保存提示词' 或 '生成帧序列'）
 * @param {Function} opts.onProceed 用户选择「忽略警告并继续」时的回调
 * @param {Function} [opts.onAutoFix] 用户选择「一键自动修复」时的回调
 * @param {Function} [opts.onEdit] 用户选择「去修改」时的回调
 */
function showPromptLinterModal(opts) {
    const { report, actionTitle = '操作', onProceed, onAutoFix, onEdit } = opts;
    const existing = document.getElementById('prompt-linter-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.className = 'modal active prompt-linter-modal';
    modal.id = 'prompt-linter-modal';
    modal.style.zIndex = '1150';

    const hasErrors = report.errorCount > 0;
    const badgeColor = hasErrors ? 'var(--color-danger, #ef4444)' : 'var(--color-warning, #f59e0b)';

    const escapeStr = (s) => (typeof escapeHtml === 'function' ? escapeHtml(s) : String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'));

    const issuesHtml = report.issues.map((issue) => {
        const isErr = issue.severity === 'error';
        const tagCls = isErr ? 'linter-tag-error' : 'linter-tag-warning';
        return `
            <div class="linter-issue-card ${isErr ? 'is-error' : 'is-warning'}">
                <div class="linter-issue-header">
                    <span class="linter-slot-badge">${escapeStr(issue.slotLabel)}</span>
                    <span class="linter-rule-badge ${tagCls}">${escapeStr(issue.ruleName)}</span>
                    ${issue.autoFixable ? '<span class="linter-autofix-badge">可自动修复</span>' : ''}
                </div>
                <div class="linter-issue-body">
                    <p class="linter-issue-msg">${escapeStr(issue.message)}</p>
                    ${issue.matchedText ? `<div class="linter-issue-match">违规片段：<code>${escapeStr(issue.matchedText)}</code></div>` : ''}
                    ${issue.fixSuggestion ? `<div class="linter-issue-suggestion">💡 建议：${escapeStr(issue.fixSuggestion)}</div>` : ''}
                </div>
            </div>
        `;
    }).join('');

    modal.innerHTML = `
        <div class="modal-content glass-panel linter-modal-content" style="max-width: 640px; border-color: ${badgeColor};">
            <div class="modal-header linter-modal-header">
                <div class="linter-title-group">
                    <span class="linter-warning-icon">${hasErrors ? '🚫' : '⚠️'}</span>
                    <div>
                        <h3 style="margin:0; font-size:16px; font-weight:700; color:#fff;">前置提示词合规审查（Pre-flight Linter）</h3>
                        <span style="font-size:12px; color:var(--text-muted);">在执行「${escapeStr(actionTitle)}」前拦截潜在不符合项</span>
                    </div>
                </div>
                <button type="button" class="close-btn linter-close-btn">&times;</button>
            </div>
            <div class="linter-status-bar">
                <span>共发现 <b>${report.totalIssues}</b> 项潜在合规风险（${report.errorCount} 处严重错误，${report.warningCount} 处规则预警）</span>
            </div>
            <div class="modal-body linter-modal-body">
                <div class="linter-issues-list">
                    ${issuesHtml}
                </div>
            </div>
            <div class="modal-footer linter-modal-footer">
                <button type="button" class="action-btn text-btn secondary linter-cancel-btn">✏️ 返回修改</button>
                ${(report.hasAutoFixable && onAutoFix) ? '<button type="button" class="action-btn text-btn linter-autofix-btn">✨ 一键自动修复格式</button>' : ''}
                ${!hasErrors ? `<button type="button" class="action-btn text-btn linter-proceed-btn">⚠️ 忽略并继续${escapeStr(actionTitle)}</button>` : `<button type="button" class="action-btn text-btn secondary" disabled title="存在严重错误（如占位符未填），必须修改后方可继续">存在严重错误，请先修复</button>`}
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    const close = () => {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 200);
    };

    modal.querySelector('.linter-close-btn').addEventListener('click', close);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            close();
            if (onEdit) onEdit();
        }
    });
    modal.querySelector('.linter-cancel-btn').addEventListener('click', () => {
        close();
        if (onEdit) onEdit();
    });

    const autoFixBtn = modal.querySelector('.linter-autofix-btn');
    if (autoFixBtn) {
        autoFixBtn.addEventListener('click', () => {
            close();
            if (onAutoFix) onAutoFix();
        });
    }

    const proceedBtn = modal.querySelector('.linter-proceed-btn');
    if (proceedBtn) {
        proceedBtn.addEventListener('click', () => {
            close();
            if (onProceed) onProceed();
        });
    }
}

/**
 * 统一执行前置门禁审查包装器
 * @param {Object} opts
 * @param {string} opts.promptBlock 提示词文本
 * @param {string} opts.actionName 操作名（如 '生成帧' / '保存提示词'）
 * @param {Function} opts.onProceed 审查通过或用户确认继续时的回调
 * @param {Function} [opts.onAutoFixApplied] 自动修复应用后的回调 (入参为 fixedText)
 * @param {Function} [opts.onCancelOrEdit] 用户选择返回修改时的回调
 */
function runPromptPreflightLinter(opts) {
    const { promptBlock, actionName = '执行', onProceed, onAutoFixApplied, onCancelOrEdit } = opts;
    const report = lintPromptBlock(promptBlock);

    // 完全通过无违规项，直接放行
    if (report.passed) {
        if (onProceed) onProceed();
        return;
    }

    showPromptLinterModal({
        report,
        actionTitle: actionName,
        onProceed: () => {
            if (onProceed) onProceed();
        },
        onAutoFix: () => {
            const fixed = autoFixLintIssues(promptBlock);
            if (onAutoFixApplied) {
                onAutoFixApplied(fixed);
            } else if (onProceed) {
                onProceed(fixed);
            }
        },
        onEdit: () => {
            if (onCancelOrEdit) onCancelOrEdit();
        }
    });
}

// Node 单测导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        PROMPT_LINTER_RULES,
        lintPromptBlock,
        autoFixLintIssues,
        runPromptPreflightLinter,
    };
}
