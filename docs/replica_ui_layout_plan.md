# 爆款复刻模块 UI 布局优化方案

状态：**P0–P3 已落地（2026-08-16）** · 起草 2026-08-16 · 落地记录见 §7
前身：`docs/replica_module_refactor_plan.md`（P0–P2 已落地 2026-08-10）、
`docs/replica_baseline_and_orthogonal_mutation_spec.md` v2.0（2026-08-15 加了双栏工作台）

本方案只谈**布局、流程管理、导航、手机适配**。后端状态机、校验器、反注入结构一律不动。

---

## 0. 结论先行

三个问题，三种性质，不能用同一种力气解决。

**一、这一页最贵的那一层已经画完了，只是没接上。**
`css/app/replica.css:959–1298` 里躺着一整套分段导航系统——吸顶区段条 + scrollspy、
节拍跳轨 chip（按硬伤/警告/过门着色）、折叠全部 / 单拍折叠、悬浮快捷直达、
定焦闪烁、锚点留白。配套的 Playwright 验收用例 `tests/test_full_replica_ui.py` 也在
（未跟踪，六组用例）。而 `js/replica_pipeline.js` **一行都不渲染它**：
CSS 点名的 7 个锚点 id 与 JS 实际发出的 3 个**零交集**。
这不是"要不要做导航"的设计题，是一个半成品卡在工作区里——P0 就是把它接上。

**二、流程管不住，是因为渲染根本不看阶段。**
`replicaRenderJob`（`js/replica_pipeline.js:617-643`）无条件把 7 个区块顺序拼成一根柱子。
于是：成本卡点过了之后，那块再无用处的抽帧区照旧占着节拍区上方一屏多；
节拍还一个没出来的时候，页面上已经摆着「⚡ 一键生成二创变体提示词包」。
用户知道自己停在"核对节拍"，却仍要手动滚过两屏才能摸到节拍。

**三、手机没有适配层。**
`replica.css` 全文 4 条 `max-width` 媒体查询（`:1393 / :1586 / :1757 / :1796`），
**全部只做栅格塌陷**——正文密度、内边距、触控目标、主操作可达性，一条没有。
顶层 6 个标签在 390px 屏上等分成约 55–60px，`panels-tabs.css:1402-1418` 已经把字号
压到 11px 并挂上 `text-overflow: ellipsis` 应急，这本身就是"塞不下"的自白。

---

## 1. 现状体检

### 1.1 已画好但没接上的那一层

| 能力 | 样式落点 | 用例落点 | JS 渲染 |
|---|---|---|---|
| 吸顶区段导航 + scrollspy + 硬伤角标 | `.replica-nav-bar/-scroll/-item/-count/-badge-err`（replica.css:959–1044） | test_full_replica_ui.py TEST 1 / TEST 6 | ❌ |
| 节拍跳轨条（硬伤红 / 警告黄 / 过门左描边） | `.replica-beat-jump-bar/-chip`（:1046–1114） | TEST 4 | ❌ |
| 折叠全部 / 单拍折叠 | `.replica-fold-toggle`、`.replica-beat-fold-btn`、`.replica-beat.is-collapsed`（:1116–1197） | TEST 2 / TEST 3 | ❌ |
| 悬浮快捷直达（44×44，含"回顶部"/"保存"） | `.replica-floating-tools`、`.replica-float-btn`（:1199–1265） | TEST 5 | ❌ |
| 定焦闪烁 + 锚点避让吸顶条 | `.replica-section-flash`、`scroll-margin-top: 68px`（:1267–1298） | — | ❌ |

折叠那一项还差两个结构件：卡片里既没有 `.replica-beat-body` 包层，也没有 `data-beat-id`
（`replicaRenderBeatCard` 只发 `data-beat-index`，`js/replica_pipeline.js:1112`）。

**锚点对不上（零交集）：**

```
CSS 点名（replica.css:1285-1291）:  #replica-sec-uploader  -jobs  -extract  -beats  -scene  -variant  -output
JS  实发                          :  #replica-sec-current-job(:618)  -workbench(:456)  -comparator(:370)
```

### 1.2 流程：渲染不看阶段

| # | 现象 | 落点 | 后果 |
|---|---|---|---|
| L1 | 7 个区块无条件顺序拼接 | `:617-643` | 11 拍的 job ≈ 6–8 屏、约 100 个 textarea 在同一根滚动柱里 |
| L2 | 四阶段阶梯是纯指示灯 | `replicaRenderPhases :313-324` | `<li>` 不可点、没有任何 `data-` 钩子；阶段与页面区块的对应关系只活在注释里 |
| L3 | 双栏工作台只挡掉 3 个 stage | `:434-437`（挡 ingest/extract/confirm_cost） | `review_frames` / `cluster_beats` 期间就渲染「拍数强约束 —」和「一键生成二创变体」 |
| L4 | 两个二创入口规则互相矛盾 | `:599-608`（四轴全填） vs `:1151-1170`（勾选 + "最多同时变两轴"） | 在 `review_beats` / `completed` 同屏并存，两套规则相反 |
| L5 | 3 张 metric 卡里 2 张是协议常量 | `:499-508`（14mm/50%、ASMR 60%） | 摆在真实值「拍数 11 拍」旁边，读起来像本条 job 测出来的 |
| L6 | 双轨对比器左右两轨是同一张图 | `:381-392`（右轨 = 同一 URL + `filter: saturate(1.2)`） | 号称"肉眼 3 秒判定漂移"的功能永远判不出漂移 |
| L7 | 抽帧区过了成本卡点仍全量展开 | `:669-760` | 三档单选 + 两个模型选择器 + 5 段 hint，永久占据节拍区上方 |

### 1.3 一个真 bug：每次保存都弹回顶部

`replicaRefreshBeats` 用 `window.scrollY` 存位、`window.scrollTo` 还位
（`js/replica_pipeline.js:1455`、`:1459`）。但**真正的滚动容器是 `.replica-shell`**
（`replica.css:14-21` 的 `overflow-y: auto`）——全站在任何宽度下都是"单面板 + 面板内滚动"
（`base.css:92-106`、`panels-tabs.css:1244-1272`），`window` 从来不滚动。

于是那两行是空操作：**保存并重校验 / AI 修复硬伤 / 拆拍 / 上并 之后一律回到节拍区顶部**。
`replica_module_refactor_plan.md` 的 P1-2 承诺过"编辑几十拍不丢滚动位置"，这条实际没生效。

修法是两行：`const box = replicaRoot().closest('.replica-shell'); const top = box.scrollTop; … box.scrollTop = top;`

### 1.4 手机

| # | 现象 | 落点 |
|---|---|---|
| M1 | 4 条媒体查询全是栅格塌陷，无正文密度层 | replica.css:1393 / 1586 / 1757 / 1796 |
| M2 | `.replica-shell` 手机内边距是 `4px 2px 24px`；全站手机规则给的 `padding:16px` 只覆盖 `.panel-body, .content-scroll-area`，replica-shell 不在其中，两头落空 | replica.css:14-21 vs panels-tabs.css:1266-1272 |
| M3 | `.replica-beat-head` 一行塞 6–8 个 chip + 一个 select + 两个按钮，`.replica-beat-tools` 靠 `margin-left:auto` 吊右；窄屏换行后"拆拍/上并"会漂到 chip 中间 | replica.css:479-491 |
| M4 | `.replica-mini-btn` = `padding:2px 10px` + 11px 字 → 触控高度约 20px（拆拍/上并/删除全是它） | replica.css:493-500 |
| M5 | `.replica-metrics-grid` 固定 `repeat(3,1fr)`，无塌陷 | replica.css:1492-1496 |
| M6 | `.replica-win-row` 固定三列 `92px 96px 1fr` | replica.css:392-396 |
| M7 | `.replica-diverge-bar` 单行 flex（输入框 + 按钮），窄屏按钮被挤扁 | replica.css:1543-1547 |
| M8 | 主操作沉在各自区块末尾——「合成提示词」在整片节拍编辑器之后 | `:937-946` |

### 1.5 导航

| # | 现象 |
|---|---|
| N1 | 6 个顶层标签 `flex:1` 等分，390px 上每个约 55–60px，装不下"图标 + 四个汉字"；已靠 11px 字 + 省略号硬撑（panels-tabs.css:1402-1418） |
| N2 | 复刻页与项目/画廊/台账并列，但它是一条**有阶段的流水线**——顶层标签栏表达不了"这条 job 走到哪了"，切走再回来只能从任务列表重找 |
| N3 | 当前 job 的切换入口是页面正文里的「已有任务」卡片（`:291-311`），手机上要先滚过它才够得着正文 |
| N4 | `nav_customize.js` 的排序/隐藏是好逃生口，但它是 localStorage 偏好，救不了"六个都要用"的人 |

---

## 2. 目标形态

### 2.1 桌面

```
┌─────────────────────────────────────────────────────────────────────┐
│ 🎬 爆款复刻   [任务 ▾ forest_bunker.mp4]           [＋ 新建]         │ 页头（常驻）
├─────────────────────────────────────────────────────────────────────┤
│ ①素材 ──── ②反推 ──── ③核对节拍 ──── ④交付        🧬 变体 ×2       │ 阶段轨（可点）
├─────────────────────────────────────────────────────────────────────┤
│ [素材] [抽帧] [节拍 11 ⚠2] [场景恒常] [二创] [交付]                  │ 吸顶区段条 ← CSS 已有
├─────────────────────────────────────────────────────────────────────┤
│ ▸ 素材 · 12.5s / 96 帧 / 2fps / 计划档 38 帧            （折叠摘要）  │
│ ▾ 节拍阶梯（11 拍）                                                  │
│   [全部折叠] B01 B02 ●B03 B04 ⚠B05 …                （跳轨条 ← 已有）│
│   ┌ B03 · 12–18s · 面层饰面 · 过门→阁楼      [⌃折叠][拆拍][上并] ┐   │
│   └ …字段区（折叠后只留一行摘要 ← CSS 已有）                    ┘   │
├─────────────────────────────────────────────────────────────────────┤
│ ⚠ 2 项硬伤   [保存并重校验]  [⋯更多]            [合成提示词]         │ 吸底动作条
└─────────────────────────────────────────────────────────────────────┘
                                                        ┌──┐ 悬浮直达
                                                        │⌃ │ ← CSS 已有
                                                        │💾│
                                                        └──┘
```

### 2.2 手机（≤768px）

```
┌───────────────────────┐
│ ⚙️ ☄️ 📁 🖼️ 📒 [🎬复刻] │ 顶层标签：只有 active 显示文字
├───────────────────────┤
│ ①②③④  ③核对节拍      │ 阶段轨压成点 + 当前阶段名
├───────────────────────┤
│ [素材][抽帧][节拍11⚠2] │ 区段条横滑（CSS 已支持 overflow-x）
├───────────────────────┤
│ ▸ B01 · 0–4s · 拆除   │ 节拍卡**默认全折叠**
│ ▸ B02 · 4–9s · 结构   │ 首屏从 100 个输入框
│ ●▸ B03 · 9–14s ⚠     │ 变成 11 行
│ ▸ B04 …               │
├───────────────────────┤
│ [保存]      [合成提示词]│ 吸底
└───────────────────────┘
```

---

## 3. 分期

### P0 — 接上已经画好的那一层（纯 JS，CSS 一行不改）

投入最小、收益最大。**验收标准现成**：`tests/test_full_replica_ui.py` 六组用例全绿。

- **P0-1 补锚点**。把 CSS 点名的 7 个 id 发到对应容器上：
  `#replica-sec-uploader`（:273）、`-jobs`（:307）、`-extract`（:670）、
  `-beats`（:922）、`-scene`（`replicaRenderSceneConstants`）、
  `-variant`（:1159）、`-output`（:1180）。现有 3 个旧 id 保留不动（`replicaToggleComparator :1767` 在用）。
- **P0-2 渲染 `.replica-nav-bar`**。区段条按"该区段这次渲不渲染"动态生成，
  带 `.replica-nav-count`（拍数）与 `.replica-nav-badge-err`（硬伤数）；
  点击 → `scrollIntoView` + 加 `.replica-section-flash`；
  scrollspy 用 `IntersectionObserver` 监听 `.replica-shell` 内的锚点，回写 `.active`。
- **P0-3 节拍卡加折叠**。卡片补 `data-beat-id`、把字段区包进 `.replica-beat-body`、
  头部加 `.replica-beat-fold-btn`；节拍区顶部加 `.replica-beat-jump-bar`
  （`#replica-toggle-fold-all` + 每拍一个 `.replica-beat-jump-chip`，
  按 validation 结果打 `.is-error`/`.is-warn`，过门拍打 `.is-crossed`）。
  硬伤横幅现有的 `[data-jump-beat]` 通路（:1375-1387）直接复用，跳过去顺带展开。
- **P0-4 悬浮直达**。`#replica-floating-tools` 挂在 `#replica-root` 外层，
  监听 `.replica-shell` 的 scroll，`scrollTop > 150` 加 `.is-visible`；
  两个按钮：`data-float-action="top"`、`="save"`。
- **P0-5 修 §1.3 的滚动 bug**（两行）。

**参考实现就在隔壁**：`js/prompt_pipeline.js:330-345 / 482-533 / 620-661` 有一套完整的
区段折叠 + 卡片折叠 + 跳转展开逻辑，同一套 `.is-collapsed` 语义。照它写，不要另发明一套。

### P1 — 让渲染跟着阶段走

- **P1-1 阶段轨可点**。`replicaRenderPhases` 的 `<li>` 加 `data-phase`；
  点击 = 滚到该阶段的主区段。已完成阶段可回看，未到的阶段禁用并在 `title` 里写清缺什么。
- **P1-2 过期区段自动折叠**。抽帧区（`:669-760`）在 `stage !== 'confirm_cost'` 时
  折成一行摘要：`抽帧 96 张 · 2fps · 计划档 38 帧 / 12 次调用 · gemini-3.7-flash-high ⌄`，
  展开才出现三档单选与模型选择器——**能力一个不删**，只是不再默认占屏。
- **P1-3 双栏工作台的门槛从 stage 改成"有没有节拍"**。`beatsCount > 0` 才渲染右栏
  mutator；反推跑着的时候只留左栏母本视窗（拼图 + 加锁卡）。
- **P1-4 二创入口收成一个**（见 §6 拍板项 1）。
- **P1-5 metric 卡只放本 job 的实测值**。拍数、时长、过门次数、硬伤数；
  协议常量（14mm/50%、ASMR 60%、进出场时间戳）挪进「🛡️ 骨架硬冻结保障」框——
  那个框本来就是讲协议的，放这里名实相符。
- **P1-6 主操作提到吸底动作条**。每个阶段一个主 CTA（确认并开始反推 / 合成提示词 /
  存入项目并打开激发结果），次要动作收进「⋯更多」。同时解决 M8。

### P2 — 手机适配层

新增一段 `@media (max-width: 768px)`，集中放在 replica.css 末尾：

| 目标 | 规则 |
|---|---|
| 正文密度 | `.replica-shell{padding:12px 12px 96px}`（底部留吸底条位）、`.replica-card{padding:12px}`、`.replica-pane-baseline/.replica-pane-mutator{padding:12px}` |
| 栅格塌陷补齐 | `.replica-metrics-grid{grid-template-columns:1fr 1fr}`、`.replica-win-row{grid-template-columns:1fr}`、`.replica-diverge-bar{flex-direction:column;align-items:stretch}` |
| 触控目标 | `.replica-mini-btn{min-height:32px;padding:6px 12px}`；`.action-btn{min-height:44px}`；`.replica-beat-tools{margin-left:0;width:100%;justify-content:flex-end}`（M3+M4） |
| 首屏高度 | `.replica-collage{max-height:200px}`；**节拍卡在手机上默认全折叠**（P0-3 的能力，手机改默认值即可）——首屏从 ~100 个输入框变成 11 行 |
| 吸底/悬浮避让 | 吸底动作条 `position:sticky;bottom:0`；`.replica-floating-tools{right:12px;bottom:88px}` 避开它 |

### P3 — 导航

- **P3-1 顶层标签：只有 active 显示文字**（≤480px）。
  `.mobile-nav-btn:not(.active) .btn-text{display:none}` → 每个标签退成 44px 图标，
  active 的那个撑开显示文字。一行放得下 6 个，不再省略号。
- **P3-2 任务切换提到页头**。「已有任务」卡片（`:291-311`）改成页头的 `[任务 ▾]` 下拉，
  显示当前 job + 阶段 chip；正文里不再占一屏。手机上尤其重要（N3）。
- **P3-3 阶段带进标签**。复刻 tab 上挂一个小角标（跑着 = 转圈，停在人工卡点 = ⏸，
  硬伤 = 红点），切走也知道那条 job 在等你（N2）。

---

## 4. 验收

| 期 | 可验证结论 |
|---|---|
| P0 | `python tests/test_full_replica_ui.py` 六组全绿；编辑第 9 拍后点保存，**停在第 9 拍**不弹顶 |
| P1 | 打开一条 `review_beats` 的 job，首屏就是节拍区；反推跑着的时候页面上没有「一键生成二创变体」 |
| P2 | 390px 宽下：首屏能看到全部 11 拍的折叠行；任意按钮触控高度 ≥32px；页面无横向滚动 |
| P3 | 390px 下 6 个标签一行显示且无省略号；不进复刻页也能看出那条 job 在等人 |

P0 与 P1 各自独立可发。**建议 P0 单独发一次**——它不改任何布局决策，只是把已经画好的
接上，风险面只有"渲不渲染"。

---

## 5. 明确不动的东西

测试钉死的 id 与属性，重构期间**一个字都不改名**：

| 钉子 | 谁钉的 |
|---|---|
| `#replica-autofix-btn`、`#replica-banner-autofix-btn` | tests/test_replica_controls.js:99-100 |
| `replicaStageSelect()` 函数名、`data-key="stage"` | tests/test_replica_wiring.py:226-227 |
| `REPLICA_BEAT_STAGE_LABELS` 九档表的字面形态（正则扫源码） | tests/test_replica_wiring.py:221-222 |
| `data-lightbox-beat` / `data-lightbox-at` / `data-key="package_operations"` / 中文对照 | tests/test_replica_progress.js:113-117 |
| `#replica-progress-stage/-percent/-log` 与 `#replica-progress` 的 display 语义 | tests/test_replica_progress.js:72-103 |
| `replicaSpaceChip` 的过门 chip 与 `.replica-chip-cross` | tests/test_replica_space_chip.js |
| 通用回写通路 `[data-beat][data-key]` + `replicaCollectBeats` 的 DOM 扫描契约 | `js/replica_pipeline.js:1399-1438`、`:1882` |

以及：后端状态机、`STAGE_LABELS` / `PHASES` 单一真源、校验器、`beats_to_dimensions` 的字段绑定。
本方案不碰其中任何一处。

---

## 6. 需要拍板

1. **两个二创入口留哪个？**
   建议留双栏工作台的四轴 mutator（`:547-587`），删掉底部的勾选式 `replicaRenderVariantForm`
   （`:1151-1170`），并把它那条**"最多同时变两轴"**的纪律并进四轴——
   填了内容的轴即视为启用，留空 = 继承母本，填满三轴以上时给一条警告。
   理由：四轴那套有 AI 发散、有正交保障框、有血统导航，是新的那条；勾选表单是旧的。

2. **双轨对比器的右轨怎么处理？**（`:388-392` 现在是同一张图加 CSS 滤镜）
   建议：没有变体 job 时**直接不渲染这个入口**，有变体时从 `lineage_variants` 取真拼图。
   拿滤镜冒充对比，比没有对比更糟——它会让人以为已经查过漂移了。

3. **桌面上节拍卡默认展开还是折叠？**
   手机默认折叠已定（P2）。桌面建议：**有硬伤的展开，其余折叠**——
   把"唯一的人工卡点"直接指到需要动手的那几拍上。

4. **`tests/test_full_replica_ui.py` 是否收进版本库？**
   它现在未跟踪，且硬编码了 Chrome 路径与 `127.0.0.1:8085`。
   建议收进来作为 P0 的验收夹具，同时把这两处改成环境变量。

---

## 7. 落地记录（2026-08-16）

P0–P3 全部落地。§6 的四个拍板项：1 选四轴 mutator（底部勾选表单已删）、
2 选「不渲染假对比」、3 选「有硬伤的展开」、4 收进库并改成环境变量。

### 与方案的两处偏离

**双轨对比器没有按 §6-2 的「有变体时取真拼图」做。**
动手时才发现变体**没有自己的拼图**：`replicaFrameBase` 把变体的帧目录指回源 job
（变体自己不抽帧，它派生的是提示词而不是素材），所以「取变体拼图」取到的就是母本那一张——
这正是原来那段代码要用 `filter: saturate(1.2)` 伪造的原因。
变体要走完分步渲染才有自己的画面，那时它已经在项目工作台里了。
改法：右轨给空态与去处，不再造假图；整个对比器入口也随二创栏一起受 `beatsCount > 0` 约束。

**协议常量没有按 §P1-5 全部挪走。** 三张 metric 卡换成了实测值（拍数 / 时长 / 空间过门次数），
14mm/50%、ASMR 60%、进出场时间戳挪进了「🛡️ 骨架硬冻结保障」框——那个框本来就是讲协议的。

### 修掉的四个 bug（都是「用户会看到错的东西」）

| # | 病灶 | 落点 |
|---|---|---|
| A1 | 标签角标整段写在 `@media (max-width:768px)` 里，桌面上是个无样式空 span；`.mobile-nav-btn` 没有 `position:relative`，红点锚到整条标签栏右上角 | panels-tabs.css |
| A2 | `replicaRender()` 从不调一次 `replicaHandleScroll` —— 重渲染后导航药丸跳回第一项、悬浮工具消失，要手动滚一下才回来 | replica_pipeline.js `replicaRender` |
| A3 | `replicaBeatFoldState` 按 `beat.id` 存且从不清空 —— 折叠态跨任务串味，拆拍/合拍重排 id 后留陈旧键 | `replicaLoadJob` / `replicaSplitBeat` / `replicaMergeBeat` |
| A4 | 吸底栏 `margin:-16px` 是照「外面是 16px 内边距的卡片」写的，实际外面是 `.replica-shell` —— 桌面左右各溢出 14px、底部悬空 | replica.css |

A4 顺手把 `.replica-shell` 的内边距抽成了 `--replica-shell-pad-x/-b` 两个变量，
吸底栏用 `calc(-1 * var(...))` 精确抵掉它——桌面与手机不再各写一套魔数。

### 其余改动

- **收口**：保存 / 合成只留在吸底栏（节拍区那一排只剩重跑聚类、重译中文、AI 修复硬伤）；
  吸底栏的第三个 AI 修复删掉，硬伤计数改成可点、按一下跳到校验横幅；
  「已有任务」卡片改成默认收起的 `<details>`。
- **二创门槛**：从 stage 改成 `beatsCount > 0`。骨架都还没有的时候不该摆出「一键生成二创变体」。
- **`replicaRefreshBeats` 连带刷新栏外**：硬伤数变了，吸顶导航的红角标、吸底栏的计数与
  「合成提示词」的禁用态都得跟着变。新增 `replicaRefreshChrome()`。
- **手机层补齐**：`.replica-mini-btn` 触控高度 32px、`.replica-beat-tools` 不再 `margin-left:auto`、
  时间线单列、发散栏纵向堆叠、卡片与双栏内边距收一档、拼图限高 200px。
- **卫生**：找回被删的 72 行注释（14 段，记的都是「为什么」）；页头工具条 8 处内联样式搬进 CSS，
  另清掉阶段轨 / 区段标题 / 硬伤横幅三处；`replicaShell()` 收掉 4 份重复的滚动容器查找；
  scrollspy 排序从 `offsetTop` 改成 `relTop`（前者依赖「所有锚点共用同一个 offsetParent」这个
  随时会被一行 `position:relative` 打破的前提）。

### 测试

新增 `tests/test_replica_layout.js`（7 组）。它盯的是三种**静默**失败：
导航项指向不存在的锚点（点了毫无反应）、主操作在一屏内出现两次、没有节拍就摆出二创入口。
第一条在这次改动过程中真的犯过一次——B3 给二创栏加了 `beatsCount` 门槛，
导航项的条件忘了跟着改，`#replica-sec-variant` 当场变成死药丸。

既有用例全绿：`test_replica_controls.js` / `test_replica_progress.js` /
`test_replica_space_chip.js` / `test_nav_customize.js`，以及 233 条 pytest。

### 仍未做

- **`tests/test_full_replica_ui.py` 没有实跑过。** 它要一个跑着的服务端和一条真任务；
  硬编码已经改成 `CHROME_PATH` / `REPLICA_TEST_URL`，但这次的验收走的是
  `test_replica_layout.js` 那条不需要浏览器的路。跑一次真的会更稳。
- **折叠态没有按视口变化重算。** 手机默认全折叠是渲染时读 `window.innerWidth` 定的，
  没有 resize 监听——转屏不会重算（每拍的折叠态本来就是粘的，影响很小）。
- **节拍卡仍是整段重建**，没有做逐卡片 id diff。这条从 `replica_module_refactor_plan.md`
  一路留到现在，几十拍规模下够用。
