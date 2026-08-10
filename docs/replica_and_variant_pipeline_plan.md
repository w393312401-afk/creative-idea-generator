# 爆款延时视频「1:1 复刻 + 二创」实施方案

状态：P0–P3 已落地（2026-08-09）· 起草 2026-08-09

落地清单见 §6，与本文档一致。已实现：`prompt_pipeline/reverse.py`、`replica_pipeline.py`、
`/api/replica/*` 七条路由、顶级「爆款复刻」页、`timelapse-beats.schema.json`，
以及 `tests/test_reverse_beats.py`（34）+ `tests/test_replica_pipeline.py`（22）。
未做：§4 的 Pass A 帧事实缓存已实现，但**断点续跑仅覆盖抽帧与帧事实两层**——
Pass B 之后中断仍需重跑聚类（成本很低，暂不投入）。

---

## 0. 先说结论：这个能力已经有一半了

`skills/gemini-omni-restoration-composer/SKILL.md` 的 **Tier 4 反推模式**（第 112 行起）
已经把 1:1 复刻的**方法论**写死了，`scripts/analyze_timelapse_video.py` 也已经把
**抽帧那一层**做完了（2fps 基线采样、state-jump 二次密采、cut ±0.2s、首尾密采、
`change_events[]`、`analysis_plan[]`、拼贴图门禁）。

**缺的不是算法，是产品化。** 现在这套东西只能由 Claude 在对话里手工跑：
人肉调脚本 → 人肉逐帧看图 → 人肉写 `timelapse_beats.json` → 人肉喂回合成器。
没有服务端路由、没有 UI、没有断点续跑、没有任务台账，更没有「二创」这一层。

所以本方案 = **把 Tier 4 提升为 app 内的一条正式流水线**（对标现有
`stepped_pipeline.py`），并在它产出的中间物之上加一层变体生成器。

不要重写分析脚本，不要重写合成器。

---

## 1. 架构：一个中间物，两个消费者

整个方案围绕一份可持久化、可编辑、可复用的中间产物：

```
                       ┌──────────────────┐
   成品视频 ─────────► │  replica_pack/   │
                       │  ├ video_overview.json   (脚本产出，客观)
                       │  ├ review_frames/*.png   (证据帧)
                       │  ├ *_collage.jpg         (整体参考)
                       │  ├ frame_facts.json      (逐帧客观事实, 新)
                       │  └ timelapse_beats.json  (节拍阶梯, 新)
                       └────────┬─────────┘
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
          【1:1 复刻】                     【二创】
       beats 原样 → 提示词包        beats + 变体轴 → 变异 beats → 提示词包
```

关键判断：**爆款视频真正可复用的资产是节拍骨架**（几拍、每拍推进多少、
镜头阶梯怎么排、状态增量多大、因果痕迹怎么继承），而不是画面内容本身。
`timelapse_beats.json` 就是这份骨架的载体。1:1 复刻是它的恒等消费，
二创是它的受控变异消费。两个功能共用一条管线，不是两条。

---

## 2. 流水线阶段（对标 `stepped_pipeline.py` 的 STAGES 设计）

新文件 `replica_pipeline.py`，状态机写在项目目录的 `.replica_pipeline.json`：

| 阶段 | 做什么 | 暂停? |
|---|---|---|
| `ingest` | 收视频、ffprobe 探测、算 sha256 去重、建 job 目录 | |
| `extract` | 调 `analyze_timelapse_video.py`，产出 overview / frames / collage | |
| `review_frames` | **Pass A** 逐帧客观事实提取（多模态，批量） | |
| `cluster_beats` | **Pass B** 事实 + change_events → `timelapse_beats.json` | |
| `review_beats` | ⏸ 用户对着证据帧核对 / 拆合节拍 / 改 banned_elements | ⏸ |
| `compose_replica` | **Pass C** beats 作为 Tier 3 绑定简报喂进现有合成器 | |
| `audit` | banned_elements 门禁 + 现有 P0/P1/P2 | |
| `completed` | | |

二创复用同一状态机，只是从 `review_beats` 后分叉：
`mutate_beats` → `compose_variant` → `audit`。

### 2.1 `extract`：直接复用脚本，只做三件事

1. 用 `subprocess` 调 `scripts/analyze_timelapse_video.py --video X --output-dir <job>`。
   脚本已自带 ffmpeg 路径回退（`FALLBACK_BINARY_DIRS`），不用重造。
2. **拼贴图 FAILED 必须硬失败并暴露到 UI**——SKILL.md 明确说这是门禁不是便利。
   现在手工跑时人会看到，产品化后没人看 stdout，必须转成任务错误。
3. 把 `analysis_plan[]` 的帧数、预估 token、预估耗时回写任务状态，**在进入
   Pass A 之前展示给用户确认**。见 §4 成本。

### 2.2 `review_frames`（Pass A）：本方案最大的新增工程量

复用 `prompt_pipeline._multimodal_chat`（`prompt_pipeline/__init__.py:1011`）——
它已经做好 base64 内联和 gateway 路由。但**不能整批一次塞进去**：

- `analysis_plan` 在 60s 视频上轻松上百帧，全尺寸 webp base64 十几 MB
  （代码注释在 `_compress_frames_for_review` 里已经踩过这个坑）。
- 做法：**先过 `_compress_frames_for_review(paths, max_side=768)`**（`:11028`，
  现成的），再按 **8–12 帧一批**并发调用，并发度沿用 `review_concurrency(config)`。
- 每批的输出是结构化 JSON：`{frame, ts, subject, materials, tools, workers_present,
  completion_extent, traces}`，聚合成 `frame_facts.json`。

**反注入（anti-priming）必须结构性保证，不能靠约定。**
SKILL.md 的 Stage 1 要求「只看像素，不读简报」。产品化后这条极易破功——
只要 Pass A 的 system prompt 里混进了项目标题或用户主题，模型就会开始
「补全」它以为该有的施工步骤。落地做法：
- Pass A 的 system prompt 单独写在 `prompt_pipeline/reverse.py` 里，
  **函数签名不接受 `dimensions` / `brief` / `title`**，从类型上堵死。
- 只传 `image_paths` + 帧时间戳。
- 输出里加一个 `confidence` 字段，低置信度的观察在 Pass B 里降权。

### 2.3 `cluster_beats`（Pass B）：纯文本，可校验

输入 `frame_facts.json` + `video_overview.json` 的 `change_events[]`，
输出 `timelapse_beats.json`。这一步**必须机械校验，不能信 LLM 自述**：

- 每个 `change_events` 条目恰好出现在一个 beat 的 `source_event_ids` 里
  （漏 = 真实变化被丢掉；重 = 节拍重叠）。
- 每个 beat 至少 1 张 `evidence_frames`，且时间戳落在 beat 窗口内。
- beat 顺序过一遍施工依赖硬否决（底漆先于面漆、布线先于封板等）。
  冲突时按 SKILL.md 的指示**优先怀疑读帧错误**，回炉重看那几帧，而不是改顺序。
- 校验失败 → 定向回炉一轮（沿用 composer 里 `rework_structural_video_beat`
  那套「校验 → 回炉 → 留痕」的既有通路形态）。

### 2.4 `review_beats`：唯一必须有的人工卡点

UI 展示：节拍阶梯表格 + 每拍的证据帧缩略图 + 未绑定事件告警 + `banned_elements` 列表。
用户可拆拍、合拍、改 `state_before/after`、增删 banned。
这个卡点不能省——它是整条链路唯一能拦住「模型脑补了一个不存在的工序」的地方。

### 2.5 `compose_replica`：零改动接入

`timelapse_beats.json` 转成 SKILL.md 已定义的 **Tier 3 绑定简报**格式，
走现有 `compose_anchor_and_packet` / `compose_remaining_beats`。
Tier 3 的语义就是「用户给了节拍阶梯，不许重新发明主题和顺序」，正好对上。

唯一新增：`banned_elements` 要接进 P0 门禁，任一 banned 元素出现在提示词里 =
交付前必须重写。

---

## 3. 二创（变体生成）

### 3.1 变异轴

二创不是「随机改一改」，是沿着**受控的轴**做替换，骨架不动：

| 轴 | 变什么 | 不变什么 |
|---|---|---|
| 载体替换 | 石屋 → 废弃巴士 / 船舱 / 地窖 | 节拍数、每拍推进量、镜头阶梯 |
| 地域环境 | 江南 → 北欧 / 沙漠 | 施工依赖顺序、状态增量 |
| 材质风格 | 木作 → 清水混凝土 | 因果痕迹的继承链 |
| 节奏 | 6 拍 8s → 4 拍 10s | 起点创伤态、终点奖励态的语义 |
| 结局奖励 | 工作室 → 茶室 / 民宿 | 前面所有施工拍 |

**默认只允许动一到两轴**。三轴以上同时变，产出的就不是「参考爆款」，
而是一个全新选题——那条路用现有的选题发动机走更合适。

### 3.2 实现

`mutate_beats(beats, axis_spec, config)`：
- 逐 beat 做**同构映射**：保留 `id/start/end/source_event_ids` 的时间骨架，
  重写 `visual_subject / visible_details / visible_action / visible_result /
  state_before / state_after / persistent_traces`。
- 映射后**重跑 §2.3 的同一套校验**（施工依赖、痕迹继承链、增量预算）。
  变体最容易坏在这里：换了载体但抄了旧载体的施工顺序，就会出现
  「给巴士砌砖墙前先浇地基」这种荒谬。
- `evidence_frames` 变成 `reference_frames`（原视频的帧，仅供构图参考，
  不再是事实断言），`banned_elements` 按新载体重算而不是继承。
- 再走 `compose_replica` 同一条合成路径。

### 3.3 抽帧图怎么「完全参考」

用户明确要求二创要参考抽帧图。落地为两处，而不是把图硬塞进生成：
1. **构图参考**：把每拍的 `reference_frames` 转成提示词里的构图/景别描述
   （机位高度、主体在画面中的占比、前后景关系），而不是让模型抄画面内容。
2. **可选的多模态引用**：SKILL.md Tier 2 已支持 `<image>` 变量。
   二创时把关键参考帧作为 `<image>` 显式命名进 VIDEO 提示词，
   限定其用途为「运镜与构图参考，不引用其中的物体与材质」。

---

## 4. 成本与性能（必须先看这个再决定要不要做）

单条 60 秒延时视频，`analysis_plan` 按 SKILL.md 的下限（≥40%、≥1 帧/秒）估：

- 抽帧：约 120–200 张 review 帧，ffmpeg 本地，几十秒。
- Pass A：压到 768px JPEG 后单帧约 100–200KB，10 帧/批 → 12–20 次多模态调用。
  这是**整个方案的成本大头**，也是唯一会明显拖慢的一步。
- Pass B/C：纯文本，与现有合成一个量级。

工程要求：
- Pass A 结果按 `frame_path + prompt_version` 做**磁盘缓存**，改了 beats 重跑
  不该重付一次视觉钱（`_review_frame_cache` 已有类似形态可参考）。
- `extract` 完成后把预估帧数/调用数摆到 UI 上让用户确认再开跑。
- 支持「降级模式」：只跑 `change_events` 的 start/peak/end 帧（约 30–40%），
  明确标注此模式下的 beats 精度更低。

---

## 5. 契约的唯一真源（重要）

本仓库已经踩过「两套长得一样但互不相关的 contract registry」的坑
（根 `contract-registry.json` vs skill 内 `skill-local-contracts.json`）。
**不要再造第三份。**

`timelapse_beats.json` 的字段契约现在只以散文形式活在 SKILL.md 里。
做法：抽出 `skills/gemini-omni-restoration-composer/references/timelapse-beats.schema.json`，
SKILL.md 改为引用它，`replica_pipeline.py` 直接 import 校验。
一份 schema，两个消费者（对话里的 Claude、app 的流水线）。

---

## 6. 落地清单

**新增**
- `replica_pipeline.py` — 状态机（形态对标 `stepped_pipeline.py`）
- `prompt_pipeline/reverse.py` — Pass A 帧事实提取、Pass B 节拍聚类、校验器、变异器
- `js/replica_pipeline.js` + `css/app/replica.css` — UI 控制器（形态对标 `js/stepped_pipeline.js`）
- `skills/.../references/timelapse-beats.schema.json` — 契约唯一真源
- `tests/test_reverse_beats.py` — 覆盖校验器（事件绑定、依赖顺序、变异同构）

**改动**
- `server.py` — 新增路由，沿用现有 `elif path == ...` 分发风格：
  - `POST /api/replica/upload`（multipart，抄 `/api/upload_video` 的解析，`:4727`）
  - `POST /api/replica/start` / `POST /api/replica/advance`
  - `GET  /api/replica/status`（对标 `/api/stepped/status`，`:3054`）
  - `PATCH`（走 POST）`/api/replica/beats` — 保存用户编辑后的节拍
  - `POST /api/replica/variant` — 二创入口
- `index.html` — 主面板加「复刻」tab（`:655` 那组 `tab-btn` 旁）
- `skills/.../SKILL.md` — Tier 4 章节改为引用 schema 文件

**不动**
- `analyze_timelapse_video.py`（除非发现真 bug）
- `prompt_pipeline/composers/*`
- 现有合成主流程

---

## 7. 分期

| 期 | 交付 | 可验证结论 |
|---|---|---|
| P0 | ingest + extract + 任务状态 + UI 上传 | 传一个视频，能在页面上看到拼贴图和 analysis_plan |
| P1 | Pass A + Pass B + 校验 + `review_beats` UI | 能拿到一份人工核对过的 `timelapse_beats.json` |
| P2 | `compose_replica` 接入 + banned 门禁 | 端到端跑出 1:1 复刻提示词包 |
| P3 | 变异轴 + 二创 UI | 同一份 beats 产出多个变体 |
| P4 | 缓存、降级模式、断点续跑 | 成本可控、失败可恢复 |

P0–P2 是一条完整的价值链，P3 之前就应该拿真视频验一次；
如果 P1 产出的 beats 需要大量人工修，说明 Pass A 的采样或提示词有问题，
先修那里，不要往后堆。

---

## 8. 两个需要你拍板的问题

1. **Pass A 用哪个模型？** 默认 `gemini-3.6-flash-high` 便宜，但逐帧读
   「材料标签 / 工具类型 / 完成范围」这类细节，flash 容易糊。
   建议：Pass A 用 flash 打底 + 对 `change_events` 的 peak 帧用强模型复核。
2. **1:1 复刻的用途边界。** 反推提示词用于学习节奏和骨架没问题；
   但如果目标是产出与原片几乎不可区分的成片再去分发，那是实打实的侵权风险。
   建议在 UI 上把默认路径引导到「复刻骨架 → 二创」，而不是把 1:1 成片当终点。
