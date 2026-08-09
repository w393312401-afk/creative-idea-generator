// 手动编辑提示词集（js/prompt_editor.js）里两个纯函数的契约：
//   appendBeatToPromptText —— 「➕ 添加一拍」的文本改写。它是纯文本插入，
//     不重排全文（用户自己写的备注/分节必须原样留着），并且要按英雄展示视频
//     的约定给新的普通段腾出槽位号。
//   validatePromptEditorText —— 保存前的本地校验，规则与后端 /api/edit_prompts
//     一一对应（后端那道才是权威，这一道只是省一次网络往返）。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8').replace(/\r\n/g, '\n');

const ctx = {
    // 这两个函数只依赖 parsePromptBlock / padSlot，其余全局量不进 vm
    document: { getElementById: () => null },
};
vm.createContext(ctx);
// parsePromptBlock（js/prompt_pipeline.js 开头那一段）与 padSlot（js/slot_model.js）
const pipeline = read('js/prompt_pipeline.js');
vm.runInContext(pipeline.slice(0, pipeline.indexOf('\n}\n', pipeline.indexOf('function parsePromptBlock')) + 3), ctx);
const slotModel = read('js/slot_model.js');
vm.runInContext(slotModel.slice(slotModel.indexOf('function padSlot'),
    slotModel.indexOf('\n}\n', slotModel.indexOf('function padSlot')) + 3), ctx);
vm.runInContext(read('js/prompt_editor.js'), ctx);

function block({ images = 4, videos = 3, heroAt = null } = {}) {
    const lines = ['图片提示词'];
    for (let i = 1; i <= images; i++) lines.push(`图片 ${i}:`, `image prompt ${i}`, '');
    lines.push('视频提示词');
    for (let i = 1; i <= videos; i++) {
        lines.push(`视频 ${i}${heroAt === i ? ' [HERO]' : ''}:`, `video prompt ${i}`, '');
    }
    return lines.join('\n').trim();
}

// Array.from：vm 里造出来的数组是另一个 realm 的，deepStrictEqual 会因原型不同而失败
const indices = (text, type) => Array.from(ctx.parsePromptBlock(text))
    .filter(s => s.type === type).map(s => s.index).sort((a, b) => a - b);
const findSlot = (text, pred) => Array.from(ctx.parsePromptBlock(text)).find(pred);

// ── 追加一拍：普通序列（视频数 = 图片数 - 1）──────────────────────────
{
    const out = ctx.appendBeatToPromptText(block());
    assert.strictEqual(out.imageIndex, 5);
    assert.strictEqual(out.videoIndex, 4);
    assert.deepStrictEqual(indices(out.text, 'image'), [1, 2, 3, 4, 5]);
    assert.deepStrictEqual(indices(out.text, 'video'), [1, 2, 3, 4]);
    // 既有正文一字未动
    assert.ok(out.text.includes('image prompt 4'));
    assert.ok(out.text.includes('video prompt 3'));
}

// ── 追加一拍：末段是英雄展示（[HERO] 槽位号恒等于最后一张图）─────────
// 英雄段是全片收尾镜头，追加一拍后它要整体后挪一位，腾出的号才是新的普通段。
{
    const out = ctx.appendBeatToPromptText(block({ images: 4, videos: 4, heroAt: 4 }));
    assert.strictEqual(out.imageIndex, 5);
    assert.strictEqual(out.videoIndex, 4);
    assert.deepStrictEqual(indices(out.text, 'image'), [1, 2, 3, 4, 5]);
    assert.deepStrictEqual(indices(out.text, 'video'), [1, 2, 3, 4, 5]);
    const hero = findSlot(out.text, s => /HERO/i.test(s.meta || ''));
    assert.strictEqual(hero.index, 5, '英雄段挪到新的最后一张图上');
    assert.strictEqual(hero.body, 'video prompt 4', '挪的是槽位号，正文原样保留');
}

// ── 追加一拍不重排全文：用户自己加的备注/分节必须原样留着 ─────────────
{
    const raw = '# 我自己加的备注\n\n' + block() + '\n\n<!-- 收尾备注 -->';
    const out = ctx.appendBeatToPromptText(raw);
    assert.ok(out.text.startsWith('# 我自己加的备注'));
    assert.ok(out.text.includes('<!-- 收尾备注 -->'));
}

// ── 解析不到槽位时不瞎猜 ─────────────────────────────────────────────
assert.strictEqual(ctx.appendBeatToPromptText('随便写点什么'), null);

// ── 保存前校验 ───────────────────────────────────────────────────────
{
    const ok = ctx.validatePromptEditorText(block(), block());
    assert.strictEqual(ok.ok, true);
    assert.strictEqual(ok.imageCount, 4);

    // 槽位号留空洞
    const hole = ctx.validatePromptEditorText(block().replace('图片 3:', '图片 5:'), block());
    assert.strictEqual(hole.ok, false);
    assert.ok(/连续/.test(hole.error));

    // 视频槽位号越界
    const stray = ctx.validatePromptEditorText(block() + '\n\n视频 9:\nstray\n', block());
    assert.strictEqual(stray.ok, false);
    assert.ok(/视频 9/.test(stray.error));

    // 拍数变少 = 想用手动编辑删拍，必须被拦下（删拍要连文件和恢复快照一起处理）
    const shrunk = ctx.validatePromptEditorText(block({ images: 3, videos: 2 }), block());
    assert.strictEqual(shrunk.ok, false);
    assert.ok(/删除/.test(shrunk.error));

    // 占位正文没填就保存 = 交付一条空提示词，渲染时会照着这行字画图
    const withPlaceholder = ctx.appendBeatToPromptText(block()).text;
    const unfilled = ctx.validatePromptEditorText(withPlaceholder, block());
    assert.strictEqual(unfilled.ok, false);
    assert.ok(/占位/.test(unfilled.error));

    // 填好之后就能过
    const filled = withPlaceholder
        .replace(ctx.promptBeatImagePlaceholder(5), 'image prompt 5')
        .replace(ctx.promptBeatVideoPlaceholder(4), 'video prompt 4');
    const good = ctx.validatePromptEditorText(filled, block());
    assert.strictEqual(good.ok, true, good.error);
    assert.strictEqual(good.imageCount, 5);
}

console.log('prompt editor tests passed');
