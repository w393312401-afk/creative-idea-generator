// 提示词版本历史与 Visual Diff 引擎单元测试
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8').replace(/\r\n/g, '\n');

// 模拟浏览器环境与 localStorage
const storage = {};
const ctx = {
    localStorage: {
        getItem: (k) => (k in storage ? storage[k] : null),
        setItem: (k, v) => { storage[k] = String(v); },
        removeItem: (k) => { delete storage[k]; }
    },
    Date: {
        now: () => 1700000000000
    },
    console: console,
    parsePromptBlock: (text) => {
        const lines = (text || '').split('\n');
        const slots = [];
        lines.forEach(l => {
            if (/^图片\s*\d+/.test(l)) slots.push({ type: 'image' });
            if (/^视频\s*\d+/.test(l)) slots.push({ type: 'video' });
        });
        return slots;
    }
};
vm.createContext(ctx);

// 加载 prompt_history.js
const historyCode = read('js/prompt_history.js');
vm.runInContext(historyCode, ctx);

// ── 测试用例 1: computeLineDiff 行级差异算法 ──
{
    const oldText = `图片 1:\nOld prompt 1\n图片 2:\nOld prompt 2`;
    const newText = `图片 1:\nOld prompt 1\n图片 2:\nModified prompt 2\n图片 3:\nNew prompt 3`;

    const diffs = ctx.computeLineDiff(oldText, newText);
    assert.ok(diffs && diffs.length > 0);

    const types = diffs.map(d => d.type);
    assert.ok(types.includes('context'), '应该有未改动的公共行');
    assert.ok(types.includes('added'), '应该有新增的行');
    assert.ok(types.includes('deleted'), '应该有删除的旧行');

    // 检查新增与删除的具体内容
    const addedLines = diffs.filter(d => d.type === 'added').map(d => d.line);
    const deletedLines = diffs.filter(d => d.type === 'deleted').map(d => d.line);

    assert.ok(addedLines.includes('Modified prompt 2'));
    assert.ok(addedLines.includes('图片 3:'));
    assert.ok(deletedLines.includes('Old prompt 2'));
}

// ── 测试用例 2: 快照存储与上限淘汰 ──
{
    const ideaId = 'test_idea_123';
    ctx.clearPromptHistory(ideaId);

    // 记录初始版本
    ctx.recordPromptHistory(ideaId, '图片 1:\nPrompt A\n视频 1:\nVid A', '初始生成');
    let history = ctx.getPromptHistory(ideaId);
    assert.strictEqual(history.length, 1);
    assert.strictEqual(history[0].summary, '初始生成');
    assert.strictEqual(history[0].imageCount, 1);
    assert.strictEqual(history[0].videoCount, 1);

    // 相同内容不重复记录
    ctx.recordPromptHistory(ideaId, '图片 1:\nPrompt A\n视频 1:\nVid A', '重复保存');
    history = ctx.getPromptHistory(ideaId);
    assert.strictEqual(history.length, 1, '内容相同时不应产生重复快照');

    // 记录新版本
    ctx.recordPromptHistory(ideaId, '图片 1:\nPrompt B\n视频 1:\nVid B', '手动编辑');
    history = ctx.getPromptHistory(ideaId);
    assert.strictEqual(history.length, 2);
    assert.strictEqual(history[0].summary, '手动编辑');

    // 批量记录以验证 30 条上限淘汰
    for (let i = 1; i <= 35; i++) {
        ctx.recordPromptHistory(ideaId, `图片 1:\nPrompt ${i}`, `版本 ${i}`);
    }
    history = ctx.getPromptHistory(ideaId);
    assert.strictEqual(history.length, 30, '最多保留 30 条快照记录');
}

// ── 测试用例 3: 格式化时间 ──
{
    assert.strictEqual(ctx.formatHistoryTime(1700000000000), '刚刚');
}

console.log('prompt history unit tests passed');
