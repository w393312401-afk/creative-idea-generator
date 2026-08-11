// 手动上传提示词集（js/prompt_import.js）里正规化器的契约：
//   normalizePromptSetText —— 把任意来源的一份提示词集补全成本项目的槽位契约
//   （图片提示词 / 图片 N: / 视频提示词 / 视频 N:）。它是导入路径上唯一的纯函数，
//   补全对不对全看它；补完的文本必须能过 validatePromptEditorText 那道校验
//   （后端 /api/edit_prompts 是权威，前端这两道与它一一对应）。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8').replace(/\r\n/g, '\n');

const ctx = { document: { getElementById: () => null } };
vm.createContext(ctx);
const pipeline = read('js/prompt_pipeline.js');
vm.runInContext(pipeline.slice(0, pipeline.indexOf('\n}\n', pipeline.indexOf('function parsePromptBlock')) + 3), ctx);
const slotModel = read('js/slot_model.js');
vm.runInContext(slotModel.slice(slotModel.indexOf('function padSlot'),
    slotModel.indexOf('\n}\n', slotModel.indexOf('function padSlot')) + 3), ctx);
vm.runInContext(read('js/prompt_editor.js'), ctx);
vm.runInContext(read('js/prompt_import.js'), ctx);

const norm = (text) => ctx.normalizePromptSetText(text);
const indices = (text, type) => Array.from(ctx.parsePromptBlock(text))
    .filter(s => s.type === type).map(s => s.index).sort((a, b) => a - b);
const bodyOf = (text, type, index) => (Array.from(ctx.parsePromptBlock(text))
    .find(s => s.type === type && s.index === index) || {}).body;

// ── 已经合规的集子：条数不变、正文一字不改、能直接过保存校验 ──────────
{
    const src = [
        '图片提示词', '', '图片 1:', 'image one', '', '图片 2:', 'image two', '',
        '视频提示词', '', '视频 1:', 'video one', '',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.imageCount, 2);
    assert.strictEqual(r.videoCount, 1);
    assert.deepStrictEqual(Array.from(r.fixes), []);
    assert.strictEqual(bodyOf(r.text, 'image', 2), 'image two');
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
}

// ── Markdown 装饰 / 代码围栏 / 全角冒号 / 缺冒号 / 同行正文 ────────────
{
    const src = [
        '# 悬崖小屋：分段提示词',
        '一些说明文字，导入时不属于任何一拍。',
        '```text',
        '## 图片 1：',
        'image one',
        '**图片 2** ',        // 缺冒号、加粗、行尾空格
        'image two',
        '- 图片 3: image three',   // 列表符号 + 正文挤在同一行
        '```',
        '### 视频提示词',
        '视频 1： video one',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.ok, true, r.error);
    assert.deepStrictEqual(indices(r.text, 'image'), [1, 2, 3]);
    assert.deepStrictEqual(indices(r.text, 'video'), [1, 2]);   // 视频 2 由补全填上
    // 同行正文必须被挪到下一行：前端老解析器会把这种正文静默丢掉
    assert.strictEqual(bodyOf(r.text, 'image', 3), 'image three');
    assert.strictEqual(bodyOf(r.text, 'video', 1), 'video one');
    assert.strictEqual(r.suggestedTitle, '悬崖小屋：分段提示词');
    assert.ok(Array.from(r.fixes).some(f => /围栏/.test(f)));
    assert.ok(Array.from(r.fixes).some(f => /同一行/.test(f)));
    assert.ok(Array.from(r.fixes).some(f => /说明文字/.test(f)));
}

// ── 英文别名标签（IMAGE / IMG / VIDEO / VID）──────────────────────────
{
    const src = [
        'IMAGE 1:', 'image one',
        'IMG 2:', 'image two',
        'Frame 3:', 'image three',
        'VIDEO 1:', 'Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the last frame.',
        'VID 2:', 'motion two',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.ok, true, r.error);
    assert.deepStrictEqual(indices(r.text, 'image'), [1, 2, 3]);
    assert.deepStrictEqual(indices(r.text, 'video'), [1, 2]);
    // 视频正文里以 IMAGE N 开头的句子不能被误判成头行、把这一段劈成两半
    assert.ok(/Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the last frame\./
        .test(bodyOf(r.text, 'video', 1)));
    assert.deepStrictEqual(Array.from(r.fixes), []);
}

// ── 槽位号乱了：从 0 起 / 跳号 / 补零 → 按出现顺序重编成 1..N ─────────
{
    const src = [
        '图片 0:', 'zero', '图片 01:', 'one', '图片 5:', 'five',
        '视频 3:', 'v-a', '视频 9:', 'v-b',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.imageCount, 3);
    assert.deepStrictEqual(indices(r.text, 'image'), [1, 2, 3]);
    assert.strictEqual(bodyOf(r.text, 'image', 1), 'zero');
    assert.strictEqual(bodyOf(r.text, 'image', 3), 'five');
    // 视频 3/9 → 1/2（视频 k 接的是 IMG k → IMG k+1）
    assert.deepStrictEqual(indices(r.text, 'video'), [1, 2]);
    assert.strictEqual(bodyOf(r.text, 'video', 1), 'v-a');
    assert.ok(Array.from(r.fixes).some(f => /图片槽位号重编/.test(f)));
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
}

// ── 视频缺段：有视频提示词但不齐 → 补骨架，且占位正文必须挡住保存 ─────
{
    const src = ['图片 1:', 'a', '图片 2:', 'b', '图片 3:', 'c', '视频 1:', 'v1'].join('\n');
    const r = norm(src);
    assert.deepStrictEqual(Array.from(r.filledVideos), [2]);
    assert.deepStrictEqual(indices(r.text, 'video'), [1, 2]);
    assert.strictEqual(r.hasPlaceholder, true);
    const check = ctx.validatePromptEditorText(r.text, '');
    assert.strictEqual(check.ok, false);
    assert.ok(/占位/.test(check.error));
}

// ── 一个视频都没有：不凭空造视频，只标记出来让界面提示 ────────────────
{
    const r = norm(['图片 1:', 'a', '图片 2:', 'b'].join('\n'));
    assert.strictEqual(r.videoCount, 0);
    assert.strictEqual(r.noVideos, true);
    assert.strictEqual(r.hasPlaceholder, false);
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
}

// ── 英雄展示段（[HERO]）：槽位号恒等于最后一张图，与 delete_slot 同源约定 ──
{
    const src = [
        '图片 1:', 'a', '图片 2:', 'b', '图片 3:', 'c',
        '视频 1:', 'v1', '视频 2:', 'v2', '视频 3 [HERO]:', 'hero',
    ].join('\n');
    const r = norm(src);
    assert.deepStrictEqual(indices(r.text, 'video'), [1, 2, 3]);
    const hero = Array.from(ctx.parsePromptBlock(r.text)).find(s => s.type === 'video' && s.index === 3);
    assert.strictEqual(hero.meta, 'HERO');
    assert.strictEqual(hero.body, 'hero');
    assert.deepStrictEqual(Array.from(r.fixes), []);
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
}
// 全角括号写的 （HERO） 也认
{
    const r = norm(['图片 1:', 'a', '图片 2:', 'b', '视频 2（HERO）:', 'hero'].join('\n'));
    const hero = Array.from(ctx.parsePromptBlock(r.text)).find(s => s.type === 'video' && s.meta === 'HERO');
    assert.ok(hero, '全角括号里的 HERO 标记没被认出来');
    assert.strictEqual(hero.index, 2);
}

// ── 视频比"图片数-1"还多：多出来的段没有首尾锚点图，丢掉并如实报告 ────
{
    const src = ['图片 1:', 'a', '图片 2:', 'b',
                 '视频 1:', 'v1', '视频 2:', 'v2', '视频 3:', 'v3'].join('\n');
    const r = norm(src);
    assert.deepStrictEqual(indices(r.text, 'video'), [1]);
    assert.strictEqual(r.droppedVideos.length, 2);
    assert.ok(Array.from(r.fixes).some(f => /多余的视频/.test(f)));
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
}

// ── 空正文：空槽位存进去等于交付一条空提示词，补占位符挡住保存 ─────────
{
    const r = norm(['图片 1:', '', '图片 2:', 'b'].join('\n'));
    assert.deepStrictEqual(Array.from(r.emptyBodies), ['图片 1']);
    assert.strictEqual(r.hasPlaceholder, true);
    assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, false);
}

// ── 末尾附着的文档小节（质量审核报告表格）不能被接到最后一拍的正文上 ────
// 这类集子的常见收尾：… 视频 N 的正文，然后 `## 提示词质量审核报告` + 一张表。
// 小标题之下的整节内容都不属于任何一拍，接上去就会原样送去渲染。
{
    const src = [
        '图片 1:', 'a', '图片 2:', 'b',
        '视频 1:', 'v1 body',
        '',
        '## 提示词质量审核报告',
        '',
        '| 检查项 | 结果 |',
        '|---|---|',
        '| 推进节拍 | 通过 |',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.imageCount, 2);
    assert.strictEqual(r.videoCount, 1);
    assert.strictEqual(bodyOf(r.text, 'video', 1), 'v1 body');
    assert.ok(!/检查项|推进节拍/.test(r.text), '审核报告被接到了提示词正文里');
    assert.ok(Array.from(r.fixes).some(f => /小标题/.test(f)));
    // 小标题之后又出现槽位头行时，断开状态必须解除
    const resumed = norm([src, '', '图片 3:', 'c', '视频 2:', 'v2 body'].join('\n'));
    assert.strictEqual(bodyOf(resumed.text, 'image', 3), 'c');
    assert.strictEqual(bodyOf(resumed.text, 'video', 2), 'v2 body');
}

// ── 直接从合成器输出复制来的集子：===AUDIT=== 那一节不能接到最后一拍上 ──
// `===XXX===` 既不是分隔线（中间夹着字）也不是 Markdown 小标题，两条既有规则
// 都拦不住它；漏掉的话末尾那句「skill 直出模式：…」会跟着视频 N 送去渲染。
{
    const src = [
        '===TITLE===', '悬崖小屋',
        '===THEME===', '一句主题',
        '===PROMPTS===',
        '图片 1:', 'a', '图片 2:', 'b',
        '视频 1:', 'v1 body',
        '',
        '===AUDIT===',
        'skill 直出模式：文本阶段无审查、无重写，批量直出+确定性修复一次成型。',
    ].join('\n');
    const r = norm(src);
    assert.strictEqual(r.ok, true, r.error);
    assert.strictEqual(r.imageCount, 2);
    assert.strictEqual(r.videoCount, 1);
    assert.strictEqual(bodyOf(r.text, 'video', 1), 'v1 body');
    assert.ok(!/AUDIT|直出模式|===/.test(r.text), '尾部审查小节被接进了提示词正文');
    assert.ok(Array.from(r.fixes).some(f => /分节标记/.test(f)));
    // ===PROMPTS=== 是解除断开，不是开始断开：它下面的第一拍不能被吞掉
    assert.strictEqual(bodyOf(r.text, 'image', 1), 'a');
    // 标记之后又出现槽位头行时同样解除断开
    const resumed = norm([src, '图片 3:', 'c', '视频 2:', 'v2 body'].join('\n'));
    assert.strictEqual(bodyOf(resumed.text, 'image', 3), 'c');
    assert.strictEqual(bodyOf(resumed.text, 'video', 2), 'v2 body');
    assert.ok(!/直出模式/.test(resumed.text));
}

// ── 解析不到任何图片提示词：报错、不导入 ───────────────────────────────
{
    const r = norm('随便一段没有槽位标签的文字\n再来一行');
    assert.strictEqual(r.ok, false);
    assert.ok(/图片 N/.test(r.error));
    assert.strictEqual(r.text, undefined);
}

// ── 真实样例：仓库里手写的 Veo 分段提示词集（17 图 + 16 视频）──────────
{
    const sample = path.join(__dirname, '..', 'veo_cliff_house_prompt_set.md');
    if (fs.existsSync(sample)) {
        const r = norm(fs.readFileSync(sample, 'utf8'));
        assert.strictEqual(r.ok, true, r.error);
        assert.strictEqual(r.imageCount, 17);
        assert.strictEqual(r.videoCount, 16);
        assert.strictEqual(r.hasPlaceholder, false);
        assert.strictEqual(ctx.validatePromptEditorText(r.text, '').ok, true);
        // 围栏与文件标题之外，正文不该被改动
        assert.ok(/Preserve this exact geometry and framing throughout the exterior sequence\./
            .test(bodyOf(r.text, 'image', 1)));
    }
}

console.log('prompt import tests passed');
