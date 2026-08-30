// 测试前置提示词静态合规审查（js/prompt_linter.js）
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8').replace(/\r\n/g, '\n');

const ctx = {
    document: { getElementById: () => null },
    escapeHtml: (s) => String(s || ''),
};
vm.createContext(ctx);

// 加载 parsePromptBlock & prompt_linter.js
const pipeline = read('js/prompt_pipeline.js');
vm.runInContext(pipeline.slice(0, pipeline.indexOf('\n}\n', pipeline.indexOf('function parsePromptBlock')) + 3), ctx);
vm.runInContext(read('js/prompt_linter.js'), ctx);

console.log('--- 开始 Pre-flight Prompt Linter 单元测试 ---');

// 1. 测试合规提示词（0 违规）
{
    const cleanBlock = `图片提示词
图片 1:
Static tripod shot, wide 18mm lens feel at eye level. Raw earth site with level horizon at fifty percent frame height.

图片 2:
Static tripod shot at 1.5m eye level. Water tanker seated in excavated trench. Ambient daylight.

图片 3 [FURNISHING]:
Static tripod shot inside renovated room. Tatami mats fitted on platform, soft linen bedding arranged, warm ambient sconces active.
`;
    const res = ctx.lintPromptBlock(cleanBlock);
    assert.strictEqual(res.passed, true, '合规提示词应完全通过审查');
    assert.strictEqual(res.totalIssues, 0);
}

// 2. 测试百分比符号违规与自动修复 (RULE_PERCENT_SYMBOL)
{
    const percentBlock = `图片提示词
图片 1:
The horizon line sits at 50% frame height, with 100% of the soil excavated.
`;
    const res = ctx.lintPromptBlock(percentBlock);
    assert.strictEqual(res.passed, false, '包含裸 % 应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_PERCENT_SYMBOL');
    assert.ok(issue, '应触发 RULE_PERCENT_SYMBOL');
    assert.strictEqual(issue.autoFixable, true);

    const fixed = ctx.autoFixLintIssues(percentBlock);
    assert.ok(fixed.includes('50 percent'), '自动修复应将 50% 替换为 50 percent');
    assert.ok(fixed.includes('100 percent'), '自动修复应将 100% 替换为 100 percent');
    assert.ok(!fixed.includes('%'), '修复后不应留有裸 % 字符');
}

// 3. 测试非规范缩写词 (RULE_ABBREVIATION)
{
    const abbrBlock = `图片提示词
图片 1:
Shot w/ 18mm lens, worker digging trench b/c soil is soft. Foreground props placed on left bg.
`;
    const res = ctx.lintPromptBlock(abbrBlock);
    assert.strictEqual(res.passed, false, '包含草写缩写词应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_ABBREVIATION');
    assert.ok(issue, '应触发 RULE_ABBREVIATION');

    const fixed = ctx.autoFixLintIssues(abbrBlock);
    assert.ok(fixed.includes('with'), '应自动展开 w/ 为 with');
    assert.ok(fixed.includes('because'), '应自动展开 b/c 为 because');
    assert.ok(fixed.includes('background'), '应自动展开 bg 为 background');
}

// 4. 测试时空单调继承与状态回退 (RULE_CHRONO_REGRESSION)
{
    const regressionBlock = `图片提示词
图片 1:
Exterior excavation site with loose earth.

图片 2 [CUT]:
Entering interior room.

图片 4:
Inside the finished room, but there is still a leaking ceiling and cracked concrete floor with accumulated dead leaves.
`;
    const res = ctx.lintPromptBlock(regressionBlock);
    assert.strictEqual(res.passed, false, '室内帧重复描述已清理破损应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_CHRONO_REGRESSION');
    assert.ok(issue, '应触发 RULE_CHRONO_REGRESSION');
    assert.ok(issue.matchedText.includes('leaking ceiling'));
}

// 5. 测试施工工具撤场残留 (RULE_TOOL_LIFECYCLE)
{
    const toolLeakBlock = `图片提示词
图片 1:
Excavation phase.

图片 5 [FURNISHING]:
Soft furnishings and tatami setup complete, with a portable halogen work tripod standing unlit near the bed.
`;
    const res = ctx.lintPromptBlock(toolLeakBlock);
    assert.strictEqual(res.passed, false, '软装完工阶段残留三脚架应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_TOOL_LIFECYCLE');
    assert.ok(issue, '应触发 RULE_TOOL_LIFECYCLE');
    assert.ok(issue.matchedText.includes('tripod'));
}

// 6. 测试材质与反光违规 (RULE_MATERIAL_VIOLATION)
{
    const materialBlock = `图片提示词
图片 1:
Excavation.

图片 4 [FLOORING]:
Teak wood plank flooring complete, with wet floor and high glossy mirror reflection.
`;
    const res = ctx.lintPromptBlock(materialBlock);
    assert.strictEqual(res.passed, false, '地板铺设出现湿地面反光应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_MATERIAL_VIOLATION');
    assert.ok(issue, '应触发 RULE_MATERIAL_VIOLATION');
}

// 7. 测试未填占位符 (RULE_STRUCTURE_SANITY)
{
    const placeholderBlock = `图片提示词
图片 1:
（在此填写第 1 张图片的提示词：这一拍画面里发生了什么）
`;
    const res = ctx.lintPromptBlock(placeholderBlock);
    assert.strictEqual(res.passed, false, '未填占位符应作为严重错误拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_STRUCTURE_SANITY');
    assert.ok(issue, '应触发 RULE_STRUCTURE_SANITY');
    assert.strictEqual(issue.severity, 'error');
}

// 8. 测试公制空间尺寸与防透视畸变 (RULE_METRIC_SPACE_ENVELOPE)
{
    const perspectiveBlock = `图片提示词
图片 1:
Static shot looking down an endless narrow tunnel with one-point perspective and vanishing point at center.
`;
    const res = ctx.lintPromptBlock(perspectiveBlock);
    assert.strictEqual(res.passed, false, '单点透视/狭长隧道高危词应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_METRIC_SPACE_ENVELOPE');
    assert.ok(issue, '应触发 RULE_METRIC_SPACE_ENVELOPE');
    assert.ok(issue.matchedText.includes('vanishing point'));

    const cavernousBlock = `图片提示词
图片 1:
A miniature diorama excavation pit with a cavernous hall and two-tier bunk bed inside.
`;
    const resCavern = ctx.lintPromptBlock(cavernousBlock);
    assert.strictEqual(resCavern.passed, false, '紧凑地穴沙盘塞入大礼堂/双层床应拦截');
    const issueCav = resCavern.issues.find(i => i.ruleId === 'RULE_METRIC_SPACE_ENVELOPE');
    assert.ok(issueCav, '应触发 RULE_METRIC_SPACE_ENVELOPE');
}

// 9. 测试单向基准轴线与防菱形旋转 (RULE_SINGLE_GROUND_BASELINE)
{
    const skewBlock = `图片提示词
图片 1:
High-angle perspective showing foundation trench on an isometric diamond grid with 45 degree oblique angle.
`;
    const res = ctx.lintPromptBlock(skewBlock);
    assert.strictEqual(res.passed, false, '菱形轴线与斜向网格应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_SINGLE_GROUND_BASELINE');
    assert.ok(issue, '应触发 RULE_SINGLE_GROUND_BASELINE');

    const zenithBlock = `图片提示词
图片 1:
A vertical aerial map top-down view of the ground excavation layout.
`;
    const resZen = ctx.lintPromptBlock(zenithBlock);
    assert.strictEqual(resZen.passed, false, '90° 纯正交顶视应拦截');
    const issueZen = resZen.issues.find(i => i.ruleId === 'RULE_SINGLE_GROUND_BASELINE');
    assert.ok(issueZen, '应触发 RULE_SINGLE_GROUND_BASELINE');
}

// 10. 测试活物动态应激与防静态假人 (RULE_LIVING_CAST_STATIC)
{
    const staticCastBlock = `图片提示词
图片 1:
A miniature diorama craft site, two small figurines remain standing unchanged in place at bottom-left observing.
`;
    const res = ctx.lintPromptBlock(staticCastBlock);
    assert.strictEqual(res.passed, false, '人偶静态钉死不动应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_LIVING_CAST_STATIC');
    assert.ok(issue, '应触发 RULE_LIVING_CAST_STATIC');
}

// 11. 测试流浪落魄人偶做旧与天地大景深 (RULE_DESTITUTE_CAST_CLEANLINESS)
{
    const cleanCastBlock = `图片提示词
图片 1:
A miniature diorama earth shelter with destitute refugees wearing clean royal blue shirt and crisp modern clothing.
`;
    const res = ctx.lintPromptBlock(cleanCastBlock);
    assert.strictEqual(res.passed, false, '初期受助灾民衣着崭新光鲜应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_DESTITUTE_CAST_CLEANLINESS');
    assert.ok(issue, '应触发 RULE_DESTITUTE_CAST_CLEANLINESS');
}

// 12. 测试机位景别开篇显式声明 (RULE_CINEMATOGRAPHY_HEADER)
{
    const noCameraBlock = `图片提示词
图片 1:
The wooden cabin sits on a dry hillside with morning sun shining from the left.
`;
    const res = ctx.lintPromptBlock(noCameraBlock);
    assert.strictEqual(res.passed, false, '开篇第一句缺失摄影机位景别声明应拦截');
    const issue = res.issues.find(i => i.ruleId === 'RULE_CINEMATOGRAPHY_HEADER');
    assert.ok(issue, '应触发 RULE_CINEMATOGRAPHY_HEADER');
}

console.log('✔ 所有 Pre-flight Prompt Linter 单元测试通过！');

