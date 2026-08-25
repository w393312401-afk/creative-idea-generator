// 一致性审查结论面板（js/review_report.js）的单测。
//
// 这里守的是"读取端不许再把结构化违规读丢/读重"这条线：
//   · 跨帧层的一条违规会被同时挂到它涉及的每一帧上（pipeline_orchestrator 按
//     beat+1 归属落盘），面板必须去重成一行——逐格看会把一个问题数成好几个，
//     正是改造前 tooltip 的读法；
//   · 复核否决（verified:false）的不许露面；复核没跑成（null）要如实标未复核；
//   · 未审查 ≠ 审过没问题，两者必须分开计。
//
// 跑法：node tests/test_review_report.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const {
    collectReviewIssues, summarizeReviewState, reviewSummaryText,
    reviewIssueSeverity, reviewLayerLabel,
} = require('../js/review_report.js');

const frame = (seq, over) => Object.assign(
    { sequence: seq, url: `/outputs/x/frames/img_${String(seq).padStart(3, '0')}.webp` },
    over);

// ── 层级标签 ────────────────────────────────────────────────────────
assert.strictEqual(reviewLayerLabel('global'), '跨帧');
assert.strictEqual(reviewLayerLabel('manual'), '人工');
assert.strictEqual(reviewLayerLabel('local'), '本拍');
assert.strictEqual(reviewLayerLabel(undefined), '本拍', '未知层退化成本拍，不留空');

// ── 严重度：只认链上守卫真的分过的级，不给手动审查编一个 ──────────────
assert.strictEqual(reviewIssueSeverity({ severity: 'chain' }), 'chain');
assert.strictEqual(reviewIssueSeverity({ severity: 'COSMETIC' }), 'cosmetic');
assert.strictEqual(reviewIssueSeverity({}), '', '没分级就是没分级，不许当成 cosmetic');

// ── 去重：跨帧层同一条违规挂在两帧上，只能出现一行 ────────────────────
const globalIssue = (beat) => ({
    text: '塔吊在第 4 帧凭空消失', layer: 'global', beat, frames: [3, 4], verified: true,
});
let issues = collectReviewIssues({
    frames: [
        frame(3, { quality_gate: 'sequence_review_flagged', review_issues: [globalIssue(2)] }),
        frame(4, { quality_gate: 'sequence_review_flagged', review_issues: [globalIssue(2)] }),
    ],
});
assert.strictEqual(issues.length, 1, '同一条跨帧违规必须去重成一行');
assert.deepStrictEqual(issues[0].frames, [3, 4], '涉及帧合并');
assert.deepStrictEqual(issues[0].owners, [3, 4], '两帧都是「修复此帧问题」的落点');
assert.strictEqual(issues[0].layer, 'global');

// 拍号不同＝不同的问题，不许因为文案一样被合掉
issues = collectReviewIssues({
    frames: [
        frame(3, { review_issues: [globalIssue(2)] }),
        frame(6, { review_issues: [Object.assign(globalIssue(5), { frames: [5, 6] })] }),
    ],
});
assert.strictEqual(issues.length, 2, '不同拍的同款文案是两个问题');

// ── 复核结论：否决的不露面，没跑成的标未复核 ──────────────────────────
issues = collectReviewIssues({
    frames: [frame(3, {
        review_issues: [
            { text: '被推翻的指控', layer: 'local', beat: 2, frames: [2, 3], verified: false },
            { text: '没复核成的判定', layer: 'local', beat: 2, frames: [2, 3], verified: null },
            { text: '确认过的判定', layer: 'local', beat: 2, frames: [2, 3], verified: true },
        ],
    })],
});
assert.deepStrictEqual(issues.map(i => i.text), ['没复核成的判定', '确认过的判定']);
assert.strictEqual(issues[0].verified, false, 'verified=null → 标未复核');
assert.strictEqual(issues[1].verified, true);

// ── 严重度合并：同一条违规在一帧上分了 chain、另一帧没分，按 chain 算 ──
issues = collectReviewIssues({
    frames: [
        frame(3, { review_issues: [Object.assign(globalIssue(2), { severity: 'cosmetic' })] }),
        frame(4, { review_issues: [Object.assign(globalIssue(2), { severity: 'chain' })] }),
    ],
});
assert.strictEqual(issues.length, 1);
assert.strictEqual(issues[0].severity, 'chain', '停链口径：有一处判 chain 就是 chain');

// ── 人工标记：与机器判定并列，但转写后不重复列 ────────────────────────
issues = collectReviewIssues({
    frames: [frame(5, { quality_gate: 'manual_flagged', manual_issue: '  透视歪了  ' })],
});
assert.strictEqual(issues.length, 1);
assert.strictEqual(issues[0].layer, 'manual');
assert.strictEqual(issues[0].text, '透视歪了', '首尾空白要去掉');
assert.deepStrictEqual(issues[0].owners, [5]);

issues = collectReviewIssues({
    frames: [frame(5, {
        manual_issue: '透视歪了',
        review_issues: [{ text: '透视歪了', layer: 'manual', beat: 4, frames: [5], verified: true }],
    })],
});
assert.strictEqual(issues.length, 1, '修复后复审转写成 layer=manual 的记录后不再重复列一遍');

// 空白的 manual_issue 不算问题
assert.strictEqual(collectReviewIssues({ frames: [frame(5, { manual_issue: '   ' })] }).length, 0);

// ── 只有 vlm_qa_reason、没有结构化记录的帧要兜住 ──────────────────────
// 老 manifest（结构化留痕是后来才加的）与生成期连续性失败（走 continuity_check，
// 那条路不产 review_issues）：汇总把它们数成"有问题"，清单就必须列得出来
issues = collectReviewIssues({
    frames: [frame(4, { quality_gate: 'vlm_qa_failed', vlm_qa_reason: '画面与上一帧几乎无变化' })],
});
assert.strictEqual(issues.length, 1, '老 manifest 的 vlm_qa_reason 要兜住');
assert.strictEqual(issues[0].text, '画面与上一帧几乎无变化');
assert.strictEqual(issues[0].verified, false, '不是复核过的结构化判定，标未复核');
assert.deepStrictEqual(issues[0].owners, [4]);

issues = collectReviewIssues({
    frames: [frame(4, {
        quality_gate: 'frame_continuity_failed',
        continuity_check: { reason: '镜头族跳变' },
        vlm_qa_reason: '兜底文案',
    })],
});
assert.strictEqual(issues[0].text, '镜头族跳变', 'continuity_check.reason 优先于 vlm_qa_reason');

// 已经有结构化记录的帧不许再多兜一条出来（否则同一个问题会列两遍）
issues = collectReviewIssues({
    frames: [frame(4, {
        quality_gate: 'sequence_review_flagged',
        vlm_qa_reason: '塔吊在第 4 帧凭空消失',
        review_issues: [globalIssue(3)],
    })],
});
assert.strictEqual(issues.length, 1, '有结构化记录就不再走兜底');

// 通过的帧即使留着上一轮的 vlm_qa_reason（宽松档 WARN 留痕）也不算问题
assert.strictEqual(collectReviewIssues({
    frames: [frame(4, { quality_gate: 'auto_approved', vlm_qa_reason: 'WARN 轻微色温漂移' })],
}).length, 0);

// 汇总数出来的"有问题帧数"与清单列得出来的行必须能对上
const run = {
    frames: [
        frame(3, { quality_gate: 'sequence_review_flagged', review_issues: [globalIssue(2)] }),
        frame(4, { quality_gate: 'vlm_qa_failed', vlm_qa_reason: '几乎无变化' }),
    ],
};
assert.strictEqual(summarizeReviewState(run).flagged, 2);
assert.strictEqual(collectReviewIssues(run).length, 2, '有几帧有问题就得列得出几条，不许空清单');

// ── 排序：按涉及帧号，再按 本拍 → 跨帧 → 人工 ────────────────────────
issues = collectReviewIssues({
    frames: [
        frame(2, { review_issues: [{ text: 'B', layer: 'global', beat: 1, frames: [2], verified: true }] }),
        frame(2, { review_issues: [{ text: 'A', layer: 'local', beat: 1, frames: [2], verified: true }] }),
        frame(9, { manual_issue: 'C' }),
    ],
});
assert.deepStrictEqual(issues.map(i => i.text), ['A', 'B', 'C']);

// ── 覆盖面汇总：三种"没有问题"必须分开 ────────────────────────────────
const state = summarizeReviewState({
    frames: [
        frame(1, { quality_gate: 'sequence_reviewed_pass', reviewed_at: '2026-08-24T10:00:00' }),
        frame(2, { quality_gate: 'sequence_review_flagged', reviewed_at: '2026-08-25T09:30:00' }),
        frame(3, { quality_gate: 'pending_manual_review' }),
        frame(4, { quality_gate: 'sequence_review_skipped' }),
        frame(5, { quality_gate: 'auto_approved', manual_issue: '歪' }),
        { sequence: 6 },  // 还没渲出来的槽位不进分母
    ],
});
assert.strictEqual(state.rendered, 5, '未渲染的槽位不算进覆盖面');
assert.strictEqual(state.passed, 1);
assert.strictEqual(state.flagged, 1);
assert.strictEqual(state.manual, 1);
assert.deepStrictEqual(state.neverSeqs, [3], '从没审过');
assert.deepStrictEqual(state.skippedSeqs, [4], '审查服务不可用被跳过');
assert.strictEqual(state.lastReviewedAt, '2026-08-25T09:30:00', '取最近一次');

// ── 抬头那一句 ──────────────────────────────────────────────────────
assert.strictEqual(reviewSummaryText(state, 3), '3 处待处理问题 · 已审 2/5 帧 · 2 帧未审查');
const clean = summarizeReviewState({
    frames: [frame(1, { quality_gate: 'sequence_reviewed_pass' })],
});
assert.strictEqual(reviewSummaryText(clean, 0), '未发现问题 · 已审 1/1 帧');

// 空单子不炸
assert.deepStrictEqual(collectReviewIssues(null), []);
assert.strictEqual(summarizeReviewState(undefined).rendered, 0);

// ── 接线：DOM 骨架与调用点必须都在 ───────────────────────────────────
const root = path.join(__dirname, '..');
const indexHtml = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const mediaJs = fs.readFileSync(path.join(root, 'js', 'media_renderer.js'), 'utf8');
const slotCardJs = fs.readFileSync(path.join(root, 'js', 'slot_card.js'), 'utf8');
const slotModelJs = fs.readFileSync(path.join(root, 'js', 'slot_model.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'css', 'app', 'skill-output.css'), 'utf8');

assert.ok(indexHtml.includes('id="frames-review-panel"'), '面板容器要在帧网格那一段里');
['review-panel-toggle', 'review-panel-summary', 'review-panel-stamp',
 'review-issue-list', 'review-panel-empty', 'review-panel-gap', 'review-gap-frames',
].forEach(cls => assert.ok(indexHtml.includes(cls), `缺少 .${cls}`));
assert.ok(indexHtml.includes('js/review_report.js'), '脚本没挂上');
// 面板要在网格之前出现，否则"先看结论再看图"这个顺序不成立
assert.ok(indexHtml.indexOf('id="frames-review-panel"')
    < indexHtml.indexOf('class="slot-toolbar" data-slot-type="image"'),
    '面板必须排在图片槽位工具条与网格之前');

assert.ok(mediaJs.includes('renderReviewPanel(idea)'),
    'renderFramesForIdea 收尾要刷新面板——否则审查跑完/修完一帧后面板还是旧的');
assert.ok(slotCardJs.includes('ISSUE_DETAIL_BADGES'), '徽标点击要走详情弹层');
assert.ok(slotCardJs.includes('openFrameIssuePop'), '徽标点击要走详情弹层');
assert.ok(slotModelJs.includes('点徽标查看明细'), 'hover 只留摘要');
// .slot-badges 整块 pointer-events:none，可点的徽标必须把事件收回来，
// 否则委托里的处理器一次也收不到（4选1 徽标此前就是这样成了死按钮）
assert.ok(css.includes('.slot-badges > .slot-badge.is-clickable'), '可点徽标缺少 pointer-events 复位');
assert.ok(css.includes('.slot-badges > .slot-badge.candidate-selection-badge'), '4选1 徽标同款复位');

// ── 结构化播报的消费端（api_client 的 sequence_review 观察者）─────────
const apiJs = fs.readFileSync(path.join(root, 'js', 'api_client.js'), 'utf8');
assert.ok(apiJs.includes('Array.isArray(evData.lines)'),
    'sequence_review_result 要优先按结构化 lines 逐行画');
assert.ok(apiJs.includes("feedLine(`　${l.text}`, l.cls || '')"),
    'lines 的每一行各占一行、按语义着色');
assert.ok(apiJs.includes('evData.message'),
    'message 兜底必须留着——老服务端不会给 lines');
assert.ok(apiJs.includes("'review-beat'"),
    '审干净的拍要收进一条就地刷新的计数行，不能一拍灌一行');
// 复用说明只该播一次：lines 里已经有了就不再另外补一句
assert.ok(apiJs.includes('hadLines'), '有 lines 时不重复播报复用/完成');

console.log('review report tests passed');
