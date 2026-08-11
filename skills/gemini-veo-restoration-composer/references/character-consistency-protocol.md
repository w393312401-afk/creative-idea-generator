# Named Cast Lock — 跨段人物一致性协议

**适用条件**：一个选题拆成多段视频、每段独立调用生成模型、模型跨段**无记忆**的流水线。

**核心前提**：模型不会"记住"上一段的人。跨段一致性 100% 来自**每一段都逐字重述同一组锚点词**。
本协议的重点不是把人物描写写得更漂亮，而是把它压成一个**可复制粘贴、不允许同义替换的固定文本块**。

**这个协议故意没有缩写。** 本包里每个缩写（`HAL`、`SCUP`、`VMFP` …）都得同步进 NLVTR 禁用词表，
否则它会漏进提示词正文、被渲染成画面上的文字。少一个缩写就少一个泄漏面，本协议全程写全名
`Named Cast Lock`。

---

## 0. 先决策：Named Cast Lock 还是 Hero Agent Lock？

本包已有一个人物一致性方案 —— **Hero Agent Lock**（SKILL.md Step 6 `Worker/Machine
Choreography Ledger`）。两者解决同一个问题（identity morphing），但方向**相反**，
且**互斥**：

| | Hero Agent Lock（既有默认） | Named Cast Lock（本协议） |
|---|---|---|
| 策略 | 把人压成**低细节高对比剪影**，让模型没有可漂移的细节 | 把人写成**高细节命名角色**，靠逐字重述压住漂移 |
| 典型写法 | `one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants; do not show the worker's face` | 见第二节 SHORT 块 |
| 脸 | 明确不给（`do not show the worker's face`） | 给，且远近景分档 |
| 安全帽 / 反光背心 | **要求** | **禁止**（进负面清单） |
| 适用 | 人物是施工工具，观众不认人 | 人物是叙事主体，观众要认人 |

**选择规则**：

1. 观众不需要认出这个人 → 用 Hero Agent Lock。这仍是本包的**默认**。
2. 选题以某个具名人物为主角、跨段要认人 → 用 Named Cast Lock，并在 Drift Lock Packet 里
   写死 `agent_lock_mode: named_cast`。
3. **一个项目只能选一种，全片不得混用。** 混用等于在同一条片子里放两个互相矛盾的服装契约，
   模型会把两套特征互串。
4. 一旦选了 Named Cast Lock，该项目内 Hero Agent Lock 的安全帽/反光背心要求**作废**，
   由本协议第五节负面清单替代。这是**项目级豁免**，不是全局改契约。

---

## 1. 已登记的 Direct-at-Zero 冲突（选用前必读）

SKILL.md Step 6 与 Step 8 的 **Direct-at-Zero Worker Clause**（P0）规定：

> Never allocate video time to a worker entering, arriving, exiting, walking out, or leaving the frame.

本协议第三节的身份块放置规则、以及第六节 QC 表第 14 项，都要求**明确的进场与离场路径**
（`人物只在首尾半秒明确进离场`）。两条规则直接矛盾，且矛盾是真实的、不是措辞问题：

- Direct-at-Zero 的动机是**不浪费 8 秒里的时间预算**在走路上。
- 进离场的动机是**尾帧不能留人** —— 静态 IMAGE 锚点受 Clean Frame Boundary 约束必须无人，
  若视频尾帧还站着人，尾帧与下一张 IMAGE 锚点直接冲突。

**当前裁决（明确登记，不静默选边）**：

| 模式 | 进离场 | 依据 |
|---|---|---|
| Hero Agent Lock（默认） | **禁止**，零秒即在工作面 | Direct-at-Zero 原文 |
| Named Cast Lock | **允许且必需**，但压缩到首尾各半秒以内 | 本协议 QC 第 14 项 |

Named Cast Lock 段落里进离场的合法写法上限是 **首尾各半秒**；把进场写成"走过来、放下工具、
戴上手套"这种多拍动作，仍然违反 Direct-at-Zero 的原始动机，按 P0 处理。

> 这条冲突尚未在服务端 `prompt_pipeline` 里表达。它在
> [`contract-registry.json`](contract-registry.json) 的 `named-cast-direct-at-zero-exemption`
> 条目下注册为 `gap`，由你在撰写时人工执行。

---

## 2. 锚点分级（决定哪些词一个字都不能改）

| 级别 | 内容 | 规则 |
|---|---|---|
| **T1 硬锁** | 帽、外套、内搭、裤、靴、手套、胡须、眼睛、发色 | 每一条含人物的提示词**必须逐字出现**，颜色词不得同义替换 |
| **T2 半锁** | 年龄、族裔、身高、体格、脸型、肤色 | 全身/中景必须出现；特写镜头可省略体格项，但**不得出现矛盾描述** |
| **T3 自由** | 姿势、动作、手持工具、汗渍与灰尘量、光线、机位、情绪 | 按分镜自由变化，不影响身份识别 |

**T1 的执行方式是固定词表，不是描写。** 模型最主要的漂移来源是同义词漂色：
只要有一段写成了 `khaki jacket`，那一段就会生成另一个人。每个 T1 部位必须登记
**唯一允许写法** + **禁止写法清单**，写进 [`cast-registry.json`](cast-registry.json)，
由 `scripts/check_character_lock.py` 做全文机器比对。词表比描写重要。

---

## 3. 三档身份文本块

每个登记角色必须备三档身份块，长度递减，用途互斥：

| 档 | 长度量级 | 用在哪 |
|---|---|---|
| `full` | 七十余词 | 角色卡、首次出场、参考图生成 |
| `mid` | 四十词上下 | 每张**含人物**的 IMAGE 提示词 |
| `short` | 二十余词 | 每条 VIDEO 提示词内的出场句 |

**用法铁律**：

1. 视频提示词里**禁止**写 `the same worker` / `he returns` / `the previous contractor`
   —— 跨段无记忆，指代等于放弃控制，必须整块重述 `short`。
2. `short` 块放在**动作动词之前**，紧贴主语位置：
   `At the first instant, <SHORT> enters from the lower right with a matte-black shovel...`
   机器校验用的代理判据是：身份块必须出现在 `continuous construction time-lapse` 这句之前。
3. 一条提示词里只出现**一次**完整身份块，后续用 `he` 回指 —— 重复描写会把注意力权重打散，
   这是"越到后段越不像"的直接成因。
4. **远近景分档**：人物脸部占画面高度超过六分之一的镜头，`short` 不够，必须升到 `mid`
   并补齐眼睛颜色与脸型。

---

## 4. 两种流水线模式（按项目选一种，不要混用）

### 模式 A：无人静态锚点 + 视频内人物

- 全部 IMAGE 首尾帧**不含人物**，人物只活在 VIDEO 段里。
- 优点：图片之间零人物漂移风险；首尾帧插值只约束场景，不约束脸。
- 代价：人物完全由文本控制，脸部细节段间会有波动 —— 靠 T1 服装剪影承担识别，而不是靠脸。
- 写法：每条视频段固定用 `short` 块，进离场路径照第一节裁决。
- **与本包既有契约天然兼容**：Clean Frame Boundary 本来就要求 IMAGE 锚点零活动人物，
  模式 A 不需要任何豁免。

### 模式 B：人物驱动叙事（人物是主角、要看脸）

- 先出**角色转视图参考图**（见第七节），锁定 seed / 复用同一张参考图作 image-to-image 输入。
- 每张首尾帧 IMAGE 都含人物，用 `mid` 块；视频段用首尾帧插值，脸由图片承载。
- 优点：脸部一致性显著更高。
- 代价：每张静态图都要过一次人物 QC，工作量翻倍；且任一张图漂了，整条链继承错误。
- **需要一条额外豁免**：模式 B 与 Clean Frame Boundary 正面冲突（后者要求 IMAGE 零人物）。
  选模式 B 必须在 Drift Lock Packet 里显式写 `clean_frame_exemption: named_cast_mode_b`，
  并接受该项目内 Clean Frame Boundary 只约束**机械与活动施工机具**、不再约束主角。

> **判断标准**：镜头里主角的脸占画面高度**超过六分之一**就必须走模式 B；否则模式 A 足够。

---

## 5. 负面清单（每条含人物的提示词都要带）

```text
No hardhat, no hi-vis vest, no sunglasses, no visible tattoos, no jewelry, no readable text or logos on clothing, no change of jacket colour, no beard length change, no second person resembling him, no face distortion, no extra fingers.
```

按需精简，但下面三条**常驻**，`check_character_lock.py` 按这三条判定：

- `no readable text or logos on clothing`
- `no change of jacket colour`
- `no second person resembling him`

**双人镜的额外约束**：助手必须写成
`plain dark clothing, no cap, clean-shaven, visibly shorter`，
明确区分度，否则模型会生成两个主角或把两人特征互串。

---

## 6. 漂移 QC 表（每段成片逐项核对）

| # | 检查项 | 判定标准 | 不合格处置 |
|---|---|---|---|
| 1 | 帽 | 与 T1 登记写法一致，非安全帽/无帽 | 重生成 |
| 2 | 外套 | 与 T1 登记颜色一致，未漂同义色 | 重生成 |
| 3 | 内搭 | 与 T1 登记颜色一致，领口可见 | 重生成 |
| 4 | 裤 | 与 T1 登记材质颜色一致 | 重生成 |
| 5 | 靴 | 与 T1 登记颜色一致 | 重生成 |
| 6 | 手套 | 与 T1 登记颜色一致（未戴时须在画面内可见） | 重生成 |
| 7 | 胡须 | 与 T1 登记形态一致，非络腮非胡茬 | 重生成 |
| 8 | 发色 | 与 T1 登记一致，帽檐下露出 | 重生成 |
| 9 | 眼睛 | 与 T1 登记一致（仅特写段判定） | 非特写可放行 |
| 10 | 体格 | 与 T2 登记一致 | 非特写可放行 |
| 11 | 肤色 | 与 T2 登记一致 | 非特写可放行 |
| 12 | 唯一性 | 画面内不存在第二个相似人物 | 重生成 |
| 13 | 服装文字 | 无 logo、无可读文字 | 重生成 |
| 14 | 进离场 | 有明确进场与离场路径，**尾帧无遗留人物** | 重生成 |

判定口径：第 1～8 项是观众用来"认人"的剪影级特征，**零容忍**。
第 9～11 项在非特写段可放行。第 12～14 项重生成。

第 14 项的尾帧要求不是风格偏好：尾帧留人会与下一张受 Clean Frame Boundary 约束的
IMAGE 锚点直接冲突。

---

## 7. 角色参考图（模式 B 前置）

四张共用同一句机位与光线约束，保证是"同一个人的四个角度"而不是四个人。
`<FULL>` 处逐字粘贴该角色的 `full` 块。

```text
角色图 1（正面全身）:
Generate a photorealistic vertical 9:16 character reference full-body front view against a plain neutral mid-grey seamless backdrop, locked tripod, 50mm lens feel, camera height 1.6m, flat even three-point studio lighting with no dramatic shadow. <FULL>. He stands relaxed and symmetrical, arms at his sides, gloves worn, facing the camera directly. No props, no tools, no text, no logos, no background objects, no other people.

角色图 2（侧面全身）: 同上，改为 exact 90-degree left profile full-body view，其余逐字不变。
角色图 3（背面全身）: 同上，改为 exact rear full-body view，其余逐字不变。
角色图 4（半身特写）: 同上，改为 waist-up three-quarter view, cap and beard clearly legible，其余逐字不变。
```

补充一张**手部/工具特写**（避免手套颜色在近景漂移）：

```text
角色图 5:
Close-up of the contractor's hands in yellow leather gloves gripping a matte-black tool handle, sun-reddened forearms, faded olive-green jacket cuffs visible, plain neutral mid-grey background, flat even studio lighting. No face, no text, no logos.
```

参考图与正片共用 `9:16` 竖幅，与本包 Frame Aspect Lock 一致 —— 换幅面会让服装剪影的
构图占比改变，参考图就不再是同一个人的可比样本。

---

## 8. 常见失败模式与对策

| 失败模式 | 成因 | 对策 |
|---|---|---|
| 段间换装 | 各段服装描述用了同义词 | 严格套用固定词表，跑 `check_character_lock.py` 做全文比对 |
| 胡须变络腮/变光头 | 只写了 `bearded` | 必须写全登记的完整胡须短语 |
| 双人镜出现两个主角 | 助手无差异化描述 | 助手强制写 `plain dark clothing, no cap, clean-shaven, visibly shorter` |
| 手套时有时无 | 动作段模型自动脱手套 | 写死 `gloves stay on throughout`，或明确 `he removes the gloves and clips them to his belt`；**择一，全片统一**，登记在 `glove_policy` |
| 脸在远景糊、近景变人 | 远近景共用同一描述 | 近景切换到 `mid` 并补眼睛颜色与脸型 |
| 时间流逝拍摄里人物闪现 | time-lapse 语义与人物连续性冲突 | 保留 `continuous construction time-lapse, not real-time footage` 写法，人物只在首尾半秒明确进离场 |
| 越到后段越不像 | 提示词后半段身份词被动作词稀释 | 身份块前置到动作动词之前，全条只出现一次 |

---

## 9. 落地检查清单

- [ ] 角色已登记进 [`cast-registry.json`](cast-registry.json)，T1 每个部位都有唯一写法 + 禁止写法
- [ ] 已选定 Hero Agent Lock 或 Named Cast Lock，写进 Drift Lock Packet 的 `agent_lock_mode`
- [ ] 已选定模式 A 或 B，全片不混用；模式 B 已显式登记 Clean Frame 豁免
- [ ] （模式 B）5 张角色参考图已生成并通过 QC 表
- [ ] 全片 `one worker` / `two workers` 类泛指着装句已全部替换为身份块
- [ ] 每条含人物提示词均带负面清单三条常驻项
- [ ] `python scripts/check_character_lock.py --cast <id> <prompt-set.md>` 退出码为 0
- [ ] 每段成片过一遍 14 项 QC 表，第 1～8 项零容忍

---

## 10. 已登记角色

角色的权威数据在 [`cast-registry.json`](cast-registry.json)，本节只做索引。
**不要**在本文件里复制身份块正文 —— 两处各写一份就是下一次漂移的源头。

| id | 显示名 | 角色 | 默认模式 | 首次使用 |
|---|---|---|---|---|
| `jake-miller` | Jake Miller | 38 岁美国白人男性硬朗型装修工 | A | `veo_petrified_saguaro_prompt_set.md` |
