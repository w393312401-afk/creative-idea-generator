# 爆款复刻模块重构方案

状态：**P0 + P1 + P2 已落地（2026-08-10）** · 起草 2026-08-10
前身：`docs/replica_and_variant_pipeline_plan.md`（P0–P3 已落地于 commit 29f2acf）

§6 的三个问题按建议拍板执行：**(b) 字段级复刻**、**banned 命中堵住入库**、**首跑两段式**。
落地情况见文末 §9。

---

## 0. 结论先行

两句抱怨各自对应一个结构问题，都不是打磨能解决的。

**「逻辑不太明白」** —— 人工卡点让你逐拍编辑 7 个字段，其中 5 个下游没有任何消费者。
你花最多时间核对的 `state_before / state_after / persistent_traces`，在
`beats_to_dimensions`（`prompt_pipeline/reverse.py:1529`）那里被丢掉了，
只有一行 `visible_action → visible_result` 传给合成器。看不懂不是你的问题，
是这个模块的产出与它的输入确实对不上。

**「用起来不方便」** —— 成本确认卡点被自动开跑绕过（首跑永远是完整模式，
你从未确认过）、所有反馈都打到页面顶端的上传卡片里、刷新页面即与运行中的任务失联、
终点是一个 `<pre>` 加一个复制按钮。

下面按「先补断点、再改交互、最后收契约」分三期。**后端的校验器与反注入结构不动**——
那是这个模块最扎实的部分，93 个测试压着。

---

## 1. 现状链路与三处断点

```
视频 ─► ingest ─► extract ─► Pass A ─► Pass B ─► ⏸review_beats ─► compose ─► audit ─► 完成
                     │                              │                 │          │
                     └─ 断点 B ────────────┘                 └ 断点 A ┘   └ 断点 C
                        成本卡点被绕过          编辑器 7 字段            合成器不知道
                                                只有 2 个传下去          自己在做复刻
                                                                        输出无去处
```

### 断点 A：编辑器与合成器之间的字段收窄 —— 「逻辑不明白」的根源

`beats_to_dimensions`（`prompt_pipeline/reverse.py:1529`）把每一拍 13 个字段压成：

```python
{'text': '<visible_action> → <visible_result>（工序：…）', 'op': operation}
```

**丢掉的**：`visual_subject`、`visible_details`、`state_before`、`state_after`、
`persistent_traces`、`workers_present`、`evidence_frames`、`confidence`、
`start/end/source_event_ids`。

而 UI 编辑器（`js/replica_pipeline.js:301-309`）恰恰把 `state_before / state_after`
摆成最显眼、带最长说明的字段（"须写具体空间完成范围"），`persistent_traces` 还有一条
专门的校验规则要求至少两条。**用户被引导去精修的，正是不会传下去的那几个。**

更严重的两个死字段（已全库 grep 确认零消费者）：

| 字段 | 写在哪 | 谁读 | 后果 |
|---|---|---|---|
| `dimensions['banned_elements']` | `reverse.py:1562` | **无** | 禁用清单从未进入模型上下文 |
| `dimensions['reverse_engineered']` | `reverse.py:1563` | **无** | 合成器不知道这是复刻 |

`banned_elements` 的自述用途是"this list is what stops the prompt writer from
hallucinating later"（`reverse.py:789`），但提示词写手根本没见过这份清单。
它实际只被 `banned_element_hits`（`reverse.py:1577`）在**成品提示词上做事后 substring 扫描**。
而 `run_audit`（`replica_pipeline.py:587`）扫完记下 hits 之后，
照样 `_publish_to_library` 并 `stage = 'completed'`（`:598-600`）——
原方案 §2.5 承诺的"P0 门禁"落地成了一张事后报告单。

`reverse_engineered` 没人读的直接代价，是合成器照旧套用为**原创选题**写的产线规则去审一份
**照实转录**的阶梯。`_translate_compose_failure`（`replica_pipeline.py:489`）那 25 行道歉文案
就是这个断点的疤痕——它在向用户解释"照实复刻"和"产线规则"为什么打架。正确的修法不是把
报错翻译得更好听，是让合成器知道自己在做什么。

### 断点 B：成本确认卡点不存在

原方案 §2.1 / §4 要求"extract 完成后把预估摆给用户确认再开跑"。实际：

- `replicaUpload` 上传成功后直接 `await replicaStart()`（`js/replica_pipeline.js:457`）
- `start_replica_job`（`replica_pipeline.py:617`）一口气跑完 extract → Pass A → Pass B
- `cost_estimate` 在 `run_extract` 里算好，经 SSE 推给前端**当作一行进度文案显示**，
  然后立刻开始烧钱

那个「完整 / 降级」单选框只在 `canStart = !hasBeats`（`js:175`）时渲染 ——
**首跑时它没有任何机会被看见**。所以：首跑必然是完整模式，降级模式只有重试时才用得上，
而"先确认再烧钱"这道卡点在设计文档里存在、在代码里不存在。

### 断点 C：终点是一个复制按钮

`prompt_block` 生成后 UI 只给「复制全部」。而 `/api/stepped/start`（`server.py:4732`）
只需要 `dimensions` + `project_key` —— 复刻侧 `beats_to_dimensions` 已经算出了 dimensions。
一步之遥，没接。

`_publish_to_library` 会把它写进创意库，但复刻页上一个字都没提，
用户不知道东西去了哪、也不知道下一步该去哪个页面。

---

## 2. 其余具体不便（按性价比排序）

| # | 现象 | 位置 | 修法 |
|---|---|---|---|
| 1 | 所有提示都打到上传卡片。你在页面底部点保存，反馈出现在视口外的顶端 | `js:420` `replicaToast` | 固定浮层，或每个 section 自带 status 行 |
| 2 | 每次操作全量 `innerHTML` 重建，滚动位置/焦点/展开状态全丢。几十拍 × 7 个 textarea | `js:100` `replicaRender` | 框架渲染一次，拍卡片按 id 增量 diff |
| 3 | 刷新即失联：无轮询、无 SSE 重连。跑了 15 分钟的 Pass A 刷新后没有任何"在跑"的指示，还会给出「开始反推」按钮诱导重复点击 | `js:655` `replicaTabEntered` | 复用 app.js 已有的 `/api/tasks` 轮询模式 |
| 4 | 运行中的任务不能取消。后端 `cancel_event` 和 `cancel_replica_job` 都写好了，UI 没按钮、路由没接 | `replica_pipeline.py:690` | 接一个按钮和一条路由 |
| 5 | 二创派生新 job 却长在当前 job 的卡片里；跑完靠 `newest.variant_of === 当前 job` 的启发式偷偷切过去，并发时会切错 | `js:627` | worker 直接回传 `variant_job_id` |
| 6 | 拆拍/合拍只改本地不落盘，且 id 不重排——拆完 UI 上出现两个 `B03` | `js:564-596` | 立即落盘并取回重排后的 id |
| 7 | UI 提示"用、分隔"，解析用 `/[、,\n]/`——打全角「，」整串塌成一个元素 | `js:538` | 统一分隔符解析（含全角） |
| 8 | `temporary_object_lingering` 标成 warn，但 UI 自己都在说"其中一项会让合成直接失败" | `js:233` / `reverse.py` | 见 §3.2 P0-1：修好合成器后这条不该存在 |
| 9 | schema 读不到就静默跳过字段校验，只 print；UI 于是显示"已通过全部机械校验" | `reverse.py:_load_schema` | 降级本身进 validation 列表 |
| 10 | 前端用 `/^scene_/` 正则猜后端目录布局 | `js:77` `replicaFrameUrl` | 后端直接下发相对 URL |
| 11 | UI 的 ①②③④⑤ 与后端 9 个 stage 不对应；chip 显示"聚类节拍"，页面上找不到对应区块 | 见 §3.3 P1-1 | 统一阶段模型，常量下发 |

补一句 #11 的背景：stage → 中文标签的映射现在已经抄了**两份**
（`js/replica_pipeline.js:14` 的 `REPLICA_STAGE_LABELS` 和 `js/projects.js:68` 的
`PROJECT_STAGE_LABELS`）。本仓库在 contract registry 上已经吃过一次"两份长得一样但互不相关"
的亏，别加第三份。

---

## 3. 重构方案

### 3.1 先拍一个决定：「复刻」到底交付什么

这是所有断点的上游。当前实现在两个语义之间摇摆：

- **(a) 骨架复刻** —— 只复用节拍数、时长、每拍推进量，内容重新生成。
  这是目前**实际在做**的（只传一行文本给合成器）。
- **(b) 字段级复刻** —— `state_before/after`、痕迹继承链、构图逐拍绑定进提示词。
  这是 UI 编辑器、13 字段 schema 和证据帧校验**承诺**的。

**建议选 (b)。** 理由：若选 (a)，那么人工卡点、13 字段 schema、证据帧、痕迹链校验、
Pass A 的逐帧精读全是白做的工程量——那条路直接给选题发动机加一个"拍数 = N"参数就够了，
不需要这个模块。何况原方案 §8.2 自己就说 1:1 成片有侵权风险、应把默认路径引导到二创，
而二创要的恰恰是 (b) 的痕迹继承链。

选定 (b) 之后，断点 A 的修法就明确了：**不是让 UI 少显示几个字段，而是给合成器开一条
replica 绑定通路。**

### 3.2 P0 — 补断点（不动 UI 也能见效）

**P0-1 让合成器认识复刻简报**
- `beats_to_dimensions` 增加 `replica_binding`：逐拍带上完整字段，而不是压成一行文本
- 合成器侧读它，把这些字段作为**绑定约束**注入每一拍，而不是让模型从一句话重新想
- `reverse_engineered=True` 时替换掉那条为原创选题写的场景状态预检
- 然后 `_translate_compose_failure` 那 25 行道歉可以删掉，`temporary_object_lingering`
  这条 warn 也随之消失（§2 #8 一并解决）

**P0-2 `banned_elements` 变成真门禁**
- 传进合成器的 system prompt 作负面清单——让写手先看见，而不是写完再抓
- `run_audit` 命中时不进 `completed`，进新的 `audit_failed`，UI 给「重写这些表述」
- 命中时不入库（现在是照样入库）

**P0-3 成本卡点真的卡住**
- 拆 `start_replica_job` 为两段：extract 单独一个 action，跑完停在新卡点 `confirm_cost`
- `replicaUpload` 不再自动串 `replicaStart`
- 完整/降级单选框在 `confirm_cost` 卡点渲染，且**任何时候都能换模式重跑 Pass A**
  （现在只有 `hasBeats === false` 时才显示）

**P0-4 接上下游**
- 节拍区与输出区加「送去分步管线渲染」→ `/api/stepped/start`，
  dimensions 直接用 `beats_to_dimensions` 的产物
- 输出区显式写明「已存入创意库：<标题>」并给跳转链接

### 3.3 P1 — UI 重构

**P1-1 阶段模型统一**
把 UI 的 ①②③④⑤ 和后端 9 个 stage 收敛成 4 个用户可见阶段，每阶段一屏：

```
素材 ─► 反推 ─► 核对节拍 ⏸ ─► 交付
                    └─► 二创 ⏸（派生新任务，显式跳过去）
```

映射写一处，由后端 `/api/replica/stages` 下发，前端不再抄。

**P1-2 节拍编辑器改增量渲染**
- 顶层框架渲染一次，拍卡片按 id diff
- textarea 用 `input` 事件写回内存 model，不再靠全量扫 DOM 的 `replicaCollectBeats`
- 拆拍/合拍立即落盘并取回重排后的 id

**P1-3 反馈就地显示**
- toast 改固定浮层；长耗时操作在**触发它的那个按钮**上转圈，不再全页 disable

**P1-4 断线可恢复**
- `replicaTabEntered` 查 `/api/tasks`，发现本 job 有 running 任务就自动重连 SSE
- job 状态里存 `active_task_id`
- 运行中不渲染「开始反推」，渲染「取消」

**P1-5 取消** —— 接 `cancel_replica_job` 到按钮和路由

### 3.4 P2 — 收拾契约

- `_load_schema` 失败进 validation 列表（至少 warn），不再静默降级
- 帧 URL 由后端下发，前端不猜目录布局
- 分隔符解析统一（含全角逗号）
- stage 标签常量去重（消掉 `PROJECT_STAGE_LABELS` 里那份抄写）

---

## 4. 分期与验收

| 期 | 交付 | 可验证结论 |
|---|---|---|
| P0 | §3.2 四项 | 跑一条真视频：开跑前看得到预估并能选降级；编辑的字段真的影响提示词；banned 命中会挡住交付；产出能一键送去渲染 |
| P1 | §3.3 五项 | 编辑几十拍不丢滚动位置；刷新页面能接回运行中的任务；跑错了能取消 |
| P2 | §3.4 四项 | schema 缺失时 UI 会说"字段校验未执行"，而不是"已通过全部校验" |

P0 是"这个模块能不能用"的分水岭，P1 是"用起来累不累"。
**建议 P0 做完就拿真视频验一次再决定 P1 的范围**——如果 P0-1 之后合成产出仍然与
编辑内容对不上，说明 (b) 这条路的绑定粒度还得再谈，那时候改 UI 就白改了。

---

## 5. 明确不动的东西

- **`reverse.py` 的 Pass A / Pass B 提示词与反注入结构**。
  `_scrub_config_for_pass_a` 那套"签名里装不下主题"的保护是对的，别为了任何理由松动它。
- **93 个后端测试压着的校验器**（`_validate_event_coverage`、`_validate_construction_order`、
  `_validate_composer_frame_contract`、`_validate_temporary_objects`）。
  这是模块里质量最高的部分，重构期间它们应当**一个都不改地继续通过**。
- `analyze_timelapse_video.py`、磁盘缓存与 job 目录布局、`timelapse-beats.schema.json`。

---

## 6. 需要你拍板（已按建议执行，可回退）

1. **§3.1 的 (a) 还是 (b)？** → 选了 **(b) 字段级复刻**。
2. **banned 命中要不要真的堵住入库？** → **堵**。命中进 `audit_failed`，不入库、不算完成。
3. **首跑改成两段式能否接受？** → **改了**。抽帧与 Pass A 拆成两个任务，中间停在 `confirm_cost`。

---

## 9. 落地记录（2026-08-10）

### 与原方案的一处偏离

**P0-4 没有按 §3.2 写的「把 dimensions 递给 `/api/stepped/start`」做。**
动手时发现 `start_stepped_pipeline` 自己会调一遍 `compose_anchor_and_packet` ——
只递 dimensions 会**重新合成一份**提示词。那样既白付一次 Phase 1 的钱，更要命的是
渲染用的不是刚过 banned 门禁的那一份：审的是 A、渲的是 B，P0-2 那道门禁当场失效。

改法：`run_compose` 把 Phase 1 产物落到 `compose_state.json`，
`start_stepped_pipeline` 新增可选参数 `precomposed` 跳过重合成，
新路由 `/api/replica/handoff` 负责交接。其余调用方行为逐字不变。

**交接后的一处刻意保留**：分步管线在首帧通过审核之后仍会重跑 Phase 2
（`compose_remaining_beats`），因为它要拿**真实渲染出来的首帧**去 refine packet 再写后续拍
——那是分步管线的质量机制，不该为复刻绕过。所以最终渲染用的逐拍提示词是重写的，
不是复刻页上那一份。这不影响 P0-2 的门禁：`banned_elements` 挂在 `parsed_brief` 上，
而 `parsed_brief` 会带进重建的 compose_state，负面清单在重写时照样生效；
`beat_ladder` 与 P0-1 绑定的起止状态也原样保留。
真正没覆盖到的是重写后那一份没有再过一次 `banned_element_hits` 的事后扫描（见「仍未做」）。

### 改动清单

| 项 | 落点 |
|---|---|
| P0-1 富字段绑定 | `reverse.beats_to_dimensions` 发 `mat/trace/state_before/state_after`；`_outline_normalized_entries` 透传；`build_outline_plan_block` 新增 STATE PAIR 绑定规则 |
| P0-1 产线预检豁免 | `validate_scene_states(allow_lingering_temporaries=)`，由 `dimensions.reverse_engineered` 触发；删掉 `_translate_compose_failure` 的道歉分支 |
| P0-2 负面清单 | `composers/base.banned_elements_block()`（两个 profile 都走 `super()`）+ 规划器 `_banned_plan_block` |
| P0-2 门禁 | `run_audit` 命中进 `audit_failed`，不 `_publish_to_library` |
| P0-3 成本卡点 | 新 stage `confirm_cost`；`extract_replica_job` + `/api/replica/extract`；上传不再自动串 Pass A |
| P0-4 交接 | `handoff_to_render` + `/api/replica/handoff` + `precomposed` |
| P1-1 阶段模型 | `replica_pipeline.STAGE_LABELS` / `PHASES` 单一真源，随 job 行下发 `stage_label`；projects.js 删掉抄写 |
| P1-2 编辑器 | `input` 写回 model；`replicaRefreshBeats` 局部刷新保滚动位置；拆合拍立即落盘取回重排 id |
| P1-3 反馈 | 固定浮层 toast；spinner 只加在触发的按钮上 |
| P1-4 断线 | job 行带 `active_task_id`，`replicaReattach` 重连 SSE |
| P1-5 取消 | `/api/replica/cancel` + 「中断这一轮」 |
| P2 | schema 读不到进 validation（`schema_unavailable`）；`frame_urls` 后端按磁盘解析；分隔符含全角逗号 |

### 测试

`tests/test_replica_wiring.py` 新增（7 条，盯四处接缝）。
`test_replica_pipeline.py` / `test_reverse_beats.py` / `test_scene_state_pipeline.py` 各有增改。
其中三条旧测试按新行为改写，都是**故意的行为变更**，原断言写在各自 docstring 里：
`banned 命中不阻断交付` → 阻断；`extract 落在 review_frames` → 落在 `confirm_cost`。

### 仍未做

- **合成器对 STATE PAIR 的遵从度没有实测。** 绑定规则已经进提示词，但"模型是否真的照抄
  空间完成范围"只有跑一条真视频才知道。这是 §4 说的 P0 验收点，建议先跑一单再决定
  要不要把 state 也纳入交付前的机械校验。
- **交接后重写的提示词没有再过一次 banned 事后扫描。** 负面清单在写的时候生效（进
  system prompt），但分步管线 Phase 2 重写完不会再跑 `banned_element_hits`。
  写之前的约束在、写之后的复核不在。要补的话，接在
  `stepped_pipeline` 拿到 `prompt_block` 之后、`parsed_brief.banned_elements` 非空时扫一遍。
- 二创派生新 job 后仍靠 `replicaJobs[0].variant_of` 的启发式跳转（§2 #5）——
  并发跑多单时会跳错。worker 侧回传 `variant_job_id` 才是正解，未做。
- 节拍卡片仍是整段重建，只是范围从整页收窄到节拍区；没有做逐卡片 id diff。
  几十拍规模下够用，上百拍会再次变卡。
