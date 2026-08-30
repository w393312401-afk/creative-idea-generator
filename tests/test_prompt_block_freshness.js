// 下单渲染前的「提示词是不是最新那份」体检（js/api_client.js: ensureFreshPromptBlock）。
//
// 2026-08-30 实测（replica_cf9a445bc52b）：11:27 起的帧任务用的是 09:35 那次合成的提示词，
// 中间三次重新合成都已写进创意库，浏览器里的 idea 对象却还停在旧快照上。渲染下单送的是
// 内存副本，于是重新合成出来的稿子一次都没被送出去过。
//
// 这道体检最容易写错的地方是「无脑用服务端那份覆盖」——提示词编辑器允许手改，覆盖会把
// 手改稿冲掉。所以下面每一条都在钉同一件事：什么时候该问、什么时候必须闭嘴放行。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js/api_client.js'), 'utf8')
    .replace(/\r\n/g, '\n');
const start = src.indexOf('async function ensureFreshPromptBlock');
assert.ok(start > 0, '没在 js/api_client.js 里找到 ensureFreshPromptBlock');
const end = src.indexOf('\n}\n', start) + 3;
const fnSource = src.slice(start, end);

function makeCtx({ remote, remoteStatus = 200, fetchThrows = false, confirmAnswer = true }) {
    const calls = { applied: [], confirms: [], toasts: [], fetches: [] };
    const ctx = {
        console,
        fetch: async (url) => {
            calls.fetches.push(url);
            if (fetchThrows) throw new Error('network down');
            return {
                ok: remoteStatus === 200,
                json: async () => remote,
            };
        },
        customConfirm: async (msg) => {
            calls.confirms.push(msg);
            return confirmAnswer;
        },
        applyPromptBlockToIdea: async (idea, block, slots) => {
            calls.applied.push({ block, slots });
            idea.prompt_block = block;
        },
        showToast: (m, k) => calls.toasts.push([m, k]),
        encodeURIComponent,
        Number, String, Date,
    };
    vm.createContext(ctx);
    vm.runInContext(fnSource, ctx);
    return { ctx, calls };
}

let passed = 0;
function test(name, fn) {
    return fn().then(() => { passed++; console.log('  ✓ ' + name); },
                     (e) => { console.error('  ✗ ' + name + '\n    ' + e.message); process.exitCode = 1; });
}

(async () => {
console.log('--- 提示词新鲜度体检 ---');

await test('服务端更新且内容不同 → 提示用户，同意后换成服务端那份', async () => {
    const idea = { id: 'x', prompt_block: '旧稿', updated_at: 1000 };
    const { ctx, calls } = makeCtx({
        remote: { prompt_block: '新稿', prompt_slots: { images: {} }, updated_at: 2000 },
    });
    const ok = await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
    assert.strictEqual(ok, true);
    assert.strictEqual(calls.confirms.length, 1, '应当问一次');
    assert.strictEqual(idea.prompt_block, '新稿');
    assert.strictEqual(idea.updated_at, 2000, 'updated_at 没跟着换，下一次会重复问');
    assert.strictEqual(calls.applied.length, 1);
});

await test('用户选择仍用页面上这份 → 不动它', async () => {
    const idea = { id: 'x', prompt_block: '旧稿', updated_at: 1000 };
    const { ctx, calls } = makeCtx({
        remote: { prompt_block: '新稿', updated_at: 2000 },
        confirmAnswer: false,
    });
    await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
    assert.strictEqual(idea.prompt_block, '旧稿');
    assert.strictEqual(calls.applied.length, 0);
});

await test('两份一样 → 一声不吭放行', async () => {
    const idea = { id: 'x', prompt_block: '同一份', updated_at: 1000 };
    const { ctx, calls } = makeCtx({ remote: { prompt_block: '同一份', updated_at: 9999 } });
    await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
    assert.strictEqual(calls.confirms.length, 0, '内容相同不该打扰用户');
});

await test('本地领先（手改稿）→ 绝不覆盖、也不问', async () => {
    // 这是这道体检最危险的误伤：把用户刚在编辑器里改的稿子当成"过期"冲掉。
    const idea = { id: 'x', prompt_block: '我手改的稿', updated_at: 5000 };
    const { ctx, calls } = makeCtx({ remote: { prompt_block: '服务端的旧稿', updated_at: 1000 } });
    await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
    assert.strictEqual(idea.prompt_block, '我手改的稿');
    assert.strictEqual(calls.confirms.length, 0);
    assert.strictEqual(calls.applied.length, 0);
});

await test('取数失败 → 放行，不挡渲染', async () => {
    // 这是体检不是门禁：一次网络抖动不该让用户按不动渲染。
    for (const bad of [{ fetchThrows: true }, { remoteStatus: 500, remote: null },
                       { remote: null }, { remote: { prompt_block: '' } }]) {
        const idea = { id: 'x', prompt_block: '旧稿', updated_at: 1000 };
        const { ctx, calls } = makeCtx(Object.assign({ remote: { prompt_block: '新稿', updated_at: 2000 } }, bad));
        const ok = await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
        assert.strictEqual(ok, true);
        assert.strictEqual(calls.confirms.length, 0);
        assert.strictEqual(idea.prompt_block, '旧稿');
    }
});

await test('没有 id / 没有本地提示词 → 直接放行，不发请求', async () => {
    for (const idea of [{ prompt_block: 'x' }, { id: 'x', prompt_block: '' }, null]) {
        const { ctx, calls } = makeCtx({ remote: { prompt_block: '新稿', updated_at: 2000 } });
        const ok = await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
        assert.strictEqual(ok, true);
        assert.strictEqual(calls.fetches.length, 0, '无从比对时不该白打一次请求');
    }
});

await test('缺 updated_at 时按"服务端不更新"处理，不误伤', async () => {
    const idea = { id: 'x', prompt_block: '旧稿' };            // 本地没有 updated_at
    const { ctx, calls } = makeCtx({ remote: { prompt_block: '新稿' } });  // 服务端也没有
    await ctx.ensureFreshPromptBlock(idea, '生成帧序列');
    assert.strictEqual(calls.confirms.length, 0, '两边都没时间戳时判不了新旧，宁可不问');
    assert.strictEqual(idea.prompt_block, '旧稿');
});

console.log(`--- 完成：${passed} 条通过 ---`);
})();
