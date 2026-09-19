# 「激发结果」页极简布局方案

目标：把结果页从"一堆平铺的功能入口"改成"一条明确的流水线"。不改任何生成逻辑，
**所有现有按钮 id 原样保留**，只换外壳和位置，因此 app.js / media_renderer.js 里
既有的监听、disabled 控制、hydrate 逻辑全部不用动。

---

## 一、现在为什么不好用

对照 `index.html:364-640`：

| 问题 | 具体表现 |
|---|---|
| **不知道下一步点哪个** | `.action-row` 里 7 枚按钮等权重平铺（复制全部提示词 / 生成封面图 / 生成帧序列 / 生成视频序列 / 合并并加速视频 / 收藏点子 / 导出 Markdown）。其中 4 枚是**有先后顺序**的流水线，3 枚是随手工具，视觉上完全一样。 |
| **首屏被标题吃掉** | `.content-header` padding 24/28 + 英文标题行 + 中文标题行 + 按钮行 ≈ 140px。手机上已有 `meta-collapsed` 折叠，桌面端反而没折。 |
| **说明文字常驻** | `frames-desc`、`frames-dnd-hint`、`cover-desc-text` 三段长说明每次都占满宽度显示，看第二遍就是噪音，却挤掉了真正要看的帧网格。 |
| **控件散落** | 生图模型选择器、调试限帧、一致性审查按钮混在 `.frames-controls` 里跟说明文字抢位置。 |
| **三个 tab 有两个是单块内容** | `tab-panel-prompts` 只有一个 `prompt-box`；`tab-panel-audit` 只有一条 repair banner + 一个 `audit-details`。为一块内容付一次 tab 切换成本。 |
| **封面段浪费高度** | 单张 9:16 缩略图独占一整段，宽屏下右侧大片空白。 |

---

## 二、目标布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ 神秘浮空树屋改造 #旧物改造 #爆改            ⌄     ✅审核通过·3建议  │ ← 单行头 48px
├──────────────────────────────────────────────────────────────────────┤
│ ①封面 ✓  ②帧序列 12/16 ⟳  ③视频 —  ④成片 —   [ 继续生成帧序列 → ] ⋯│ ← 管线条 52px
├──────────────────────────────────────────────────────────────────────┤
│  概览 · 提示词                                                        │ ← tab 减到 2
├───────────────┬──────────────────────────────────────────────────────┤
│ 🖼️ 封面        │ 🎞️ 帧序列 · 12/16                          [⚙] [ⓘ]  │
│ ┌───────────┐ │ ┌────┬────┬────┬────┬────┐                          │
│ │           │ │ │ 1  │ 2  │ 3  │ 4  │ 5  │                          │
│ │  9:16     │ │ ├────┼────┼────┼────┼────┤                          │
│ │           │ │ │ 6  │ 7  │ 8  │ 9  │10  │                          │
│ └───────────┘ │ └────┴────┴────┴────┴────┘                          │
│ 历史 ▫▫▫      │                                                      │
│               │ 🎬 视频序列 · 0/15                          [⚙] [ⓘ]  │
│               │ ┌────┬────┬────┬────┐                               │
└───────────────┴──────────────────────────────────────────────────────┘
   260px 固定       自适应（≥1400px 才分栏，窄屏回落为上下堆叠）
```

---

## 三、五处改动

### A. 管线条（Pipeline Bar）—— 核心改动

用一条 `.pipeline-bar` 取代 `.action-row.header-sticky-actions` 的 7 枚平铺按钮：

* **左侧 4 枚步骤芯片**：① 封面 → ② 帧序列 → ③ 视频序列 → ④ 合并成片。
  每枚芯片就是原按钮换的外壳，**沿用原 id**（`make-cover-btn` /
  `generate-frames-btn` / `generate-videos-btn` / `merge-videos-btn`），
  所以 `hydrateCoverPanel` 等函数里的 `btn.disabled = true` 继续生效，
  只是视觉上表现为"该步骤进行中"。
  芯片带三态：`未开始`（灰）/ `进行中`（转圈 + `12/16`）/ `已完成`（✓ + 数量）。
  点击 = 触发生成 + 滚动定位到对应 section。
* **中间一枚主按钮**：`继续生成帧序列 →`，文案与动作由 `updatePipelineBar()`
  指向"当前第一个未完成步骤"。这是"更方便"的落点——不用再判断该点哪个。
  全部完成时变成 `↓ 下载成品视频`。
* **右侧 `⋯` 溢出菜单**：收纳 `copy-prompt-btn-all`、`save-idea-btn`、
  `export-idea-btn` 三枚随手工具（依旧原 id，只是移进菜单容器）。

新增 `updatePipelineBar()`，读取 `currentIdea` 的封面 / frames / videos manifest 与
`getIdeaTaskRecord(idea.id, kind)`，在这几个时机调用：`renderIdea()` 末尾、
三个 `hydrate*Panel()` 末尾、`setProgressBar()` 里、任务完成/失败回调里。

### B. 标题压成单行

`.content-header` 桌面端也套用 `meta-collapsed`：默认只显示一行主标题 + 折叠箭头
`idea-meta-toggle`，展开才露话题串与中文标题行。header 高度 140px → 48px。

> **落地时的调整**：原计划让中文标题当折叠态的主行。实现时保持了英文那行当主行
> ——它是可以直接粘去 TikTok 的成品文案，而中文行是它的附属；把两行对调要动
> `panels-tabs.css` 里已经跑熟的移动端折叠规则（`#idea-title` 的 line-clamp、
> `.tiktok-title-line-cn` 的隐藏），收益抵不上风险。桌面端的折叠规则直接写在
> `result-minimal.css` 的 `@media (min-width: 769px)` 里，与移动端那份并列，
> 互不覆盖。

### C. 说明文字与次级控件收纳

每个 section 头压成一行：`🎞️ 帧序列 · 12/16   [⚙] [ⓘ]`

* `[ⓘ]` 气泡收纳：`frames-desc`、`frames-dnd-hint`、`cover-desc-text`
  （元素不删，只是移进气泡容器并默认 `hidden`，避免其它地方引用失效）。
* `[⚙]` 弹层收纳：`frames-model-picker`（生图模型）、`debug-limit-picker`（调试限帧/限段）、
  `run-sequence-review-btn`（一致性审查）。
* `frames-meta` / `videos-meta` **留在正文**，只压成一行。原计划把它们也收进气泡，
  实现时发现 `renderMergeBlocked()` 会往 `videos-meta` 里渲染「重试缺口 / 跳过合并」
  两个可操作按钮——收进气泡等于把合并失败后的唯一出口藏起来。计数改由管线条承担，
  section 标题行不再重复显示。

### D. tab 从 3 减到 2

删掉 `tab-btn-audit`，把 `idea-repair` + `audit-details` 整块移到概览页最顶部，
折叠成一枚状态条：`✅ 质量审核 通过 · 3 条建议`，点击展开（`<details>` 已有，改成默认 `close`）。
审核结果本来就是"扫一眼确认没红字"的东西，不值得独立一页。

**注意**：`switchTab()` 会从 `localStorage['spark_active_tab']` 恢复上次的 tab，
老用户那里可能存着 `'audit'`。需要在 `switchTab` 里加一句白名单回退（已实现为
`RESULT_TAB_IDS`），否则会出现"三个 tab 都不高亮、内容区空白"。

另外，`renderRepairBanner()` 在校验通过时也会铺一整行 "✅ 工序与场景一致性校验：
PASS 未发现违规"，跟状态条上的「✅ 通过」完全重复。用一条 CSS
（`.audit-strip .repair-banner.ok { display: none !important }`）把通过态收掉，
只有真做了修复（`.fixed`）才单独占行——不动 JS，避免影响那个函数的其它调用方。

### E. 宽屏封面侧栏

`≥1400px` 时把 `#cover-section` 变成 260px 固定左列，`#frames-section` + `#videos-section`
占右列（`#tab-panel-overview` 用 grid）。窄屏保持现在的上下堆叠。
封面本来就是一张小图 + 几枚历史缩略图，独占整行纯属浪费。

---

## 四、落地清单（已实施）

| 文件 | 改动 |
|---|---|
| `index.html` | 头部只留标题行；新增 `.pipeline-bar`；三个 section 头加 `⚙`/`ⓘ` 与 `.section-pop`；audit 块移入 overview 顶部并删掉 `tab-btn-audit` / `tab-panel-audit` |
| `css/app/result-minimal.css`（新建） | 管线条、芯片三态、桌面端折叠头、`⚙ⓘ` 弹层、审核状态条、≥1400px 分栏、移动端两行管线条 |
| `app.js` | `computePipelineState()` / `resolveNextPipelineStep()` / `updatePipelineBar()` / `initPipelineBar()` / `initSectionPops()`；`switchTab` 加 `RESULT_TAB_IDS` 白名单；`mergeVideos` 加 `mergeInFlight` 标志与收尾刷新 |
| `js/media_renderer.js` | 三个 `hydrate*Panel()` 末尾各加一次刷新；`renderIdea` 里补审核状态条的文案/图标 |
| `js/utils.js` | `setProgressBar()` 末尾刷新管线条（帧/视频每推进一格计数跟着走） |
| `css/app/efficiency.css`、`css/app/panels-tabs.css` | 删掉已失效的 `.header-sticky-actions` 规则 |

生成逻辑（`generateCover` / `generateFrames` / `generateVideos` / `mergeVideos` 的主体、
帧视频渲染、任务登记表）**未改动**。

### 管线条与既有按钮的分工（改这块前必读）

四枚芯片就是那四个生成按钮本体，`disabled` 属性归各自的生成流程所有
（`generateFrames`、`hydrateFramesPanel`、`mergeVideos` 都在写它）。
`updatePipelineBar()` 只读状态、只写 class 与状态文字，**绝不碰 `disabled`**——
两边都写必然打架。

芯片"已完成时再点会重跑"这件事由 `#pipeline-bar` 上的一个**捕获阶段**监听兜底：
帧序列/视频这两步先 `confirm` 一次，取消就 `stopImmediatePropagation()`，
按钮自己的 click 监听不会被触发（祖先节点在捕获阶段停止传播，事件到不了目标）。

`mergeVideos` 合并期间会整块替换按钮的 `innerHTML`（转圈 + 文案），
那段时间芯片里的 `.step-stat` 节点不存在，所以 `updatePipelineBar()` 对它做了空值兜底，
并在 `finally` 里 `innerHTML` 还原之后再刷一次。

## 五、收益与验收

* 首屏非内容高度：约 237px → 约 140px（1600×1000 实测），帧网格多露出一整行。
* 决策成本：7 个等权入口 → 1 个主按钮（+ 4 枚可跳过的芯片）。
* 点击路径：查看审核结果从"切 tab → 找折叠块"变成"概览页顶端一眼可见"；
  校验通过时它只是一行字。

实测过的状态（Playwright，1600×1000 与 390×844，浅色/暗色各一遍）：

| 场景 | 芯片 | 主按钮 |
|---|---|---|
| 全新 | 封面 未生成(next) · 帧序列 未生成 · 视频 待帧序列(dim) · 成片 待视频(dim) | 生成封面图 |
| 跑到一半 | 封面 ✓1张 · 帧序列 3/6(next) · 视频 未生成 · 成片 待视频(dim) | 继续生成帧序列 |
| 全部完成 | 封面 ✓ · 帧序列 ✓6/6 · 视频 ✓5/5 · 成片 未合并(next) | 合并并加速视频 |

另外验过：`⋯` 菜单开合与点外面收起、`⚙`/`ⓘ` 在同一 section 内互斥、
已完成步骤点击弹确认且取消后不触发生成、停在提示词页点芯片会自动切回概览、
`localStorage` 里存着旧的 `'audit'` 时回退到 `overview`。
