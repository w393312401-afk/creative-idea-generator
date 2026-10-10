// 提示词列表展示与一键复制功能（js/prompt_pipeline.js）单元测试
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8').replace(/\r\n/g, '\n');

const makeMockElement = (tag) => {
    const el = {
        tagName: tag.toUpperCase(),
        className: '',
        dataset: {},
        style: {},
        children: [],
        parentElement: null,
        textContent: '',
        title: '',
        type: '',
        hidden: false,
        addEventListener: (evt, fn) => {
            el[`_on_${evt}`] = fn;
        },
        append: (...args) => {
            args.forEach(child => {
                if (typeof child === 'object' && child) {
                    child.parentElement = el;
                    el.children.push(child);
                }
            });
        },
        appendChild: (child) => {
            if (typeof child === 'object' && child) {
                child.parentElement = el;
                el.children.push(child);
            }
            return child;
        },
        querySelector: (sel) => {
            const all = el.querySelectorAll(sel);
            return all.length > 0 ? all[0] : null;
        },
        querySelectorAll: (sel) => {
            const results = [];
            const check = (node) => {
                let match = false;
                if (sel.startsWith('.')) {
                    const c = sel.slice(1);
                    if (node.className && node.className.split(/\s+/).includes(c)) match = true;
                } else if (sel === 'span') {
                    if (node.tagName === 'SPAN') match = true;
                }
                if (match) results.push(node);
                (node.children || []).forEach(check);
            };
            (el.children || []).forEach(check);
            return results;
        },
        closest: (sel) => {
            let curr = el;
            while (curr) {
                if (sel.startsWith('.')) {
                    const c = sel.slice(1);
                    if (curr.className && curr.className.split(/\s+/).includes(c)) return curr;
                }
                curr = curr.parentElement;
            }
            return null;
        },
        classList: {
            add: (cls) => {
                const classes = (el.className || '').split(/\s+/).filter(Boolean);
                if (!classes.includes(cls)) {
                    classes.push(cls);
                    el.className = classes.join(' ');
                }
            },
            remove: (cls) => {
                const classes = (el.className || '').split(/\s+/).filter(c => c && c !== cls);
                el.className = classes.join(' ');
            },
            contains: (cls) => {
                return (el.className || '').split(/\s+/).includes(cls);
            },
            toggle: (cls) => {
                if (el.classList.contains(cls)) {
                    el.classList.remove(cls);
                    return false;
                } else {
                    el.classList.add(cls);
                    return true;
                }
            }
        }
    };

    let _innerHTML = '';
    Object.defineProperty(el, 'innerHTML', {
        get: () => _innerHTML,
        set: (val) => {
            _innerHTML = String(val || '');
            const spanMatches = _innerHTML.match(/<span(?:\s+class="([^"]*)")?[^>]*>(.*?)<\/span>/gi);
            if (spanMatches) {
                spanMatches.forEach(sm => {
                    const classM = sm.match(/class="([^"]*)"/i);
                    const textM = sm.match(/>([^<]*)<\/span>/i);
                    const spanEl = makeMockElement('span');
                    if (classM) spanEl.className = classM[1];
                    if (textM) spanEl.textContent = textM[1];
                    spanEl.parentElement = el;
                    el.children.push(spanEl);
                });
            }
        }
    });

    return el;
};

const domStore = {};

const ctx = {
    document: {
        getElementById: (id) => domStore[id] || null,
        createElement: makeMockElement
    },
    navigator: {
        clipboard: {
            writeText: async (t) => Promise.resolve()
        }
    }
};
vm.createContext(ctx);

// 加载 prompt_pipeline.js
const pipeline = read('js/prompt_pipeline.js');
vm.runInContext(pipeline, ctx);

// ── 测试用例 1: 标准包含图片与视频的提示词集 ──
{
    const sampleBlock = `图片提示词

图片 1:
Generate a photorealistic 9:16 image of a static tripod shot at 1.6m height...

图片 2 [ANCHOR]:
Generate a photorealistic 9:16 image with anchor preserved...

视频提示词

视频 1:
Use the provided first frame and last frame as exact composition anchors...

视频 2 [CUT]:
DECLARED HARD CUT - no video clip is generated for this slot...

视频 3 [HERO]:
Use the provided reference image as the sole starting-frame anchor...`;

    const sections = ctx.parsePromptDisplaySections(sampleBlock);
    assert.ok(sections, '应该成功解析出分节');
    assert.strictEqual(sections.length, 2, '应该有 2 个分节（图片与视频）');

    // 图片分节验证
    const imgSec = sections[0];
    assert.strictEqual(imgSec.type, 'image');
    assert.strictEqual(imgSec.title, '图片提示词');
    assert.strictEqual(imgSec.items.length, 2);

    assert.strictEqual(imgSec.items[0].index, 1);
    assert.strictEqual(imgSec.items[0].meta, '');
    assert.strictEqual(imgSec.items[0].shortLabel, '图片 1');
    assert.strictEqual(imgSec.items[0].label, '图片 1');
    assert.strictEqual(imgSec.items[0].body, 'Generate a photorealistic 9:16 image of a static tripod shot at 1.6m height...');

    assert.strictEqual(imgSec.items[1].index, 2);
    assert.strictEqual(imgSec.items[1].meta, 'ANCHOR');
    assert.strictEqual(imgSec.items[1].shortLabel, '图片 2');
    assert.strictEqual(imgSec.items[1].label, '图片 2 [ANCHOR]');
    assert.strictEqual(imgSec.items[1].body, 'Generate a photorealistic 9:16 image with anchor preserved...');

    // 视频分节验证
    const vidSec = sections[1];
    assert.strictEqual(vidSec.type, 'video');
    assert.strictEqual(vidSec.title, '视频提示词');
    assert.strictEqual(vidSec.items.length, 3);

    assert.strictEqual(vidSec.items[0].index, 1);
    assert.strictEqual(vidSec.items[0].meta, '');

    assert.strictEqual(vidSec.items[1].index, 2);
    assert.strictEqual(vidSec.items[1].meta, 'CUT');

    assert.strictEqual(vidSec.items[2].index, 3);
    assert.strictEqual(vidSec.items[2].meta, 'HERO');
    assert.strictEqual(vidSec.items[2].body, 'Use the provided reference image as the sole starting-frame anchor...');
}

// ── 测试用例 2: 同行正文 (Inline body) ──
{
    const inlineBlock = `图片提示词
图片 1: First image inline prompt
图片 2: Second image inline prompt
视频提示词
视频 1: First video inline prompt`;

    const sections = ctx.parsePromptDisplaySections(inlineBlock);
    assert.strictEqual(sections.length, 2);
    assert.strictEqual(sections[0].items[0].body, 'First image inline prompt');
    assert.strictEqual(sections[0].items[1].body, 'Second image inline prompt');
    assert.strictEqual(sections[1].items[0].body, 'First video inline prompt');
}

// ── 测试用例 3: 自由文本 / 占位符回退 ──
{
    const placeholder = '在左侧选择维度并点击「激发」，这里会输出经 gemini-veo 合成的完整提示词。';
    const sections = ctx.parsePromptDisplaySections(placeholder);
    assert.strictEqual(sections, null, '无槽位的自由文本应返回 null 以供安全降级');

    const empty = ctx.parsePromptDisplaySections('');
    assert.strictEqual(empty, null);

    const whitespace = ctx.parsePromptDisplaySections('   \n  \n  ');
    assert.strictEqual(whitespace, null);
}

// ── 测试用例 4: DOM 渲染测试 ──
{
    const container = ctx.document.createElement('div');
    const testBlock = `图片提示词\n图片 1:\nImage 1 prompt body\n\n视频提示词\n视频 1:\nVideo 1 prompt body`;
    
    ctx.renderPromptDisplay(testBlock, container);
    assert.strictEqual(container.className, 'prompt-display-container');
    assert.strictEqual(container.children.length, 2, '容器应包含 2 个分节');
    assert.strictEqual(container.dataset.rawText, testBlock.trim());

    // 空内容回退测试
    const emptyContainer = ctx.document.createElement('div');
    ctx.renderPromptDisplay('', emptyContainer);
    assert.ok(emptyContainer.className.includes('prompt-empty'));
}

// ── 测试用例 5: Markdown 标题前缀（#### 图片 8:）与全角冒号宽松解析 ──
{
    const mdBlock = `### 图片提示词
#### 图片 8:
Prompt for image 8

#### 图片 9:
Prompt for image 9

### 视频提示词
#### 视频 8：
Prompt for video 8`;

    const sections = ctx.parsePromptDisplaySections(mdBlock);
    assert.ok(sections, '带 #### 的 Markdown 标题应被正常解析');
    assert.strictEqual(sections.length, 2);
    assert.strictEqual(sections[0].items.length, 2);
    assert.strictEqual(sections[0].items[0].index, 8);
    assert.strictEqual(sections[0].items[0].body, 'Prompt for image 8');
    assert.strictEqual(sections[0].items[1].index, 9);
    assert.strictEqual(sections[0].items[1].body, 'Prompt for image 9');
    assert.strictEqual(sections[1].items[0].index, 8);
    assert.strictEqual(sections[1].items[0].body, 'Prompt for video 8');

    const slots = ctx.parsePromptBlock(mdBlock);
    assert.strictEqual(slots.length, 3);
    assert.strictEqual(slots[0].type, 'image');
    assert.strictEqual(slots[0].index, 8);
}
// ── 测试用例 6: 单条槽位正文替换 (replaceSinglePromptSlotBody) ──
{
    const original = `图片提示词

图片 1:
Old image 1

图片 2 [ANCHOR]:
Old image 2

视频提示词

视频 1:
Old video 1`;

    const updated = ctx.replaceSinglePromptSlotBody(original, 'image', 2, 'New image 2 body text');
    assert.ok(updated.includes('图片 2 [ANCHOR]:\nNew image 2 body text'));
    assert.ok(updated.includes('图片 1:\nOld image 1'));
    assert.ok(updated.includes('视频 1:\nOld video 1'));

    const updatedVid = ctx.replaceSinglePromptSlotBody(updated, 'video', 1, 'New video 1 body text');
    assert.ok(updatedVid.includes('视频 1:\nNew video 1 body text'));
}

// ── 测试用例 7: 图片/视频提示词折叠与展开功能 (Section & Card Folding + Global Toggle) ──
{
    const container = ctx.document.createElement('div');
    const toggleBtn = ctx.document.createElement('button');
    toggleBtn.innerHTML = '<span>全部折叠</span>';
    domStore['toggle-fold-all-prompts-btn'] = toggleBtn;

    const testBlock = `图片提示词\n图片 1:\nImage 1 prompt\n图片 2:\nImage 2 prompt\n\n视频提示词\n视频 1:\nVideo 1 prompt`;
    ctx.renderPromptDisplay(testBlock, container);

    const sections = container.querySelectorAll('.prompt-section');
    assert.strictEqual(sections.length, 2, '应渲染出 2 个分节');

    const imgSec = sections[0];
    const vidSec = sections[1];
    const imgCards = imgSec.querySelectorAll('.prompt-item-card');
    assert.strictEqual(imgCards.length, 2, '图片分节应有 2 张卡片');

    // 1. 分节折叠测试
    const imgHeader = imgSec.querySelector('.prompt-section-header');
    assert.ok(imgHeader, '分节应具有标题栏');
    assert.strictEqual(imgSec.classList.contains('is-collapsed'), false, '初始状态分节应为展开态');

    // 点击分节标题栏折叠
    imgHeader._on_click();
    assert.strictEqual(imgSec.classList.contains('is-collapsed'), true, '点击后图片分节应为折叠态');
    const foldBadge = imgHeader.querySelector('.prompt-section-fold-badge');
    assert.strictEqual(foldBadge.hidden, false, '折叠态下应显示折叠徽标');

    // 再次点击展开
    imgHeader._on_click();
    assert.strictEqual(imgSec.classList.contains('is-collapsed'), false, '再次点击后图片分节应恢复展开态');
    assert.strictEqual(foldBadge.hidden, true, '展开态下折叠徽标应隐藏');

    // 2. 单条卡片折叠测试
    const firstCard = imgCards[0];
    const cardHeader = firstCard.querySelector('.prompt-item-header');
    assert.ok(cardHeader, '卡片应具有头部');
    assert.strictEqual(firstCard.classList.contains('is-collapsed'), false, '初始状态卡片应为展开态');

    // 点击卡片头部折叠
    cardHeader._on_click();
    assert.strictEqual(firstCard.classList.contains('is-collapsed'), true, '点击后卡片应为折叠态');

    // 点击预览条恢复展开
    const previewEl = firstCard.querySelector('.prompt-item-preview');
    assert.ok(previewEl, '卡片应具有预览条');
    assert.ok(previewEl.textContent.includes('Image 1 prompt'), '预览条应包含提示词摘要');
    previewEl._on_click();
    assert.strictEqual(firstCard.classList.contains('is-collapsed'), false, '点击预览条后应恢复展开态');

    // 3. 分节内批量折叠条目测试
    const foldItemsBtn = imgHeader.querySelector('.prompt-section-fold-items-btn');
    assert.ok(foldItemsBtn, '分节应提供批量折叠条目按钮');
    foldItemsBtn._on_click({ stopPropagation: () => {} });
    assert.strictEqual(imgCards[0].classList.contains('is-collapsed'), true, '批量折叠后卡片1应折叠');
    assert.strictEqual(imgCards[1].classList.contains('is-collapsed'), true, '批量折叠后卡片2应折叠');

    foldItemsBtn._on_click({ stopPropagation: () => {} });
    assert.strictEqual(imgCards[0].classList.contains('is-collapsed'), false, '再次点击后卡片1应展开');
    assert.strictEqual(imgCards[1].classList.contains('is-collapsed'), false, '再次点击后卡片2应展开');

    // 4. 全局一键折叠/展开测试
    ctx.toggleFoldAllPrompts(container);
    assert.strictEqual(imgSec.classList.contains('is-collapsed'), true, '全局折叠后图片分节应折叠');
    assert.strictEqual(vidSec.classList.contains('is-collapsed'), true, '全局折叠后视频分节应折叠');
    assert.strictEqual(imgCards[0].classList.contains('is-collapsed'), true, '全局折叠后卡片应折叠');

    ctx.toggleFoldAllPrompts(container);
    assert.strictEqual(imgSec.classList.contains('is-collapsed'), false, '全局展开后图片分节应展开');
    assert.strictEqual(vidSec.classList.contains('is-collapsed'), false, '全局展开后视频分节应展开');
    assert.strictEqual(imgCards[0].classList.contains('is-collapsed'), false, '全局展开后卡片应展开');
}

console.log('prompt display and copy unit tests passed');
