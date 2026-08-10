# 杰克·米勒 人物一致性方案（Veo 多镜头）

适用对象：硬朗型装修工 杰克·米勒（Jake Miller），38 岁美国白人男性。
适用场景：一个选题拆成 10～15 段视频、每段独立调用生成模型、模型跨段**无记忆**的流水线。

核心前提：模型不会"记住"上一段的人。跨段一致性 100% 来自**每一段都逐字重述同一组锚点词**。因此本方案的重点不是把人物描写写得更漂亮，而是把它压成一个**可复制粘贴、不允许同义替换的固定文本块**。

---

## 一、锚点分级（决定哪些词一个字都不能改）

| 级别 | 内容 | 规则 |
|---|---|---|
| **T1 硬锁** | 深灰色棒球帽、褪色橄榄绿工装夹克、炭灰色 T 恤、棕色帆布工具裤、深棕色皮工作靴、黄色皮手套、修剪整齐的浅棕色短胡须、灰蓝色眼睛、浅棕色短发 | 每一条含人物的提示词**必须逐字出现**，颜色词不得同义替换 |
| **T2 半锁** | 38 岁、白人男性、185cm、宽肩结实、方脸、晒红肤色 | 全身/中景必须出现；特写镜头可省略体格项，但**不得出现矛盾描述** |
| **T3 自由** | 姿势、动作、手持工具、汗渍与灰尘量、光线、机位、情绪 | 按分镜自由变化，不影响身份识别 |

**关键约束：颜色词固定词表**（AI 最主要的漂移来源就是同义词漂色）

| 部位 | 唯一允许写法 | 禁止写法 |
|---|---|---|
| 帽 | `dark-grey baseball cap` | charcoal cap / grey hat / black cap |
| 夹克 | `faded olive-green work jacket` | army green / khaki / military jacket |
| T恤 | `charcoal-grey T-shirt` | dark grey tee / black shirt |
| 裤 | `brown canvas utility trousers` | tan pants / carpenter jeans |
| 靴 | `dark-brown leather work boots` | brown boots / tan boots |
| 手套 | `yellow leather gloves` | orange gloves / work gloves |
| 须 | `neatly trimmed light-brown short beard` | stubble / full beard / goatee |

> 只要有一段写成了 `khaki jacket`，那一段就会生成另一个人。词表比描写更重要。

---

## 二、三档身份文本块（直接复制使用）

### FULL（角色卡 / 首次出场 / 参考图生成，约 75 词）

```text
Jake Miller, a 38-year-old white American male contractor, 185cm tall, broad-shouldered and solidly built, with sun-reddened weathered skin, a square jaw, a neatly trimmed light-brown short beard, grey-blue eyes, and short light-brown hair showing under a dark-grey baseball cap. He wears a faded olive-green work jacket, a charcoal-grey T-shirt, brown canvas utility trousers, dark-brown leather work boots, and yellow leather gloves. His movements are steady and powerful, reading as a real independent American contractor.
```

### MID（每张含人物的图片提示词，约 40 词）

```text
Jake Miller, a 38-year-old broad-shouldered white American contractor with sun-reddened skin, a neatly trimmed light-brown short beard, grey-blue eyes, and a dark-grey baseball cap, wearing a faded olive-green work jacket, charcoal-grey T-shirt, brown canvas utility trousers, dark-brown leather work boots, and yellow leather gloves.
```

### SHORT（每条视频提示词内的出场句，约 22 词）

```text
Jake Miller, the bearded broad-shouldered contractor in a dark-grey baseball cap, faded olive-green work jacket, charcoal-grey T-shirt, brown canvas trousers, dark-brown boots, and yellow leather gloves,
```

**用法铁律**：
1. 视频提示词里**禁止**写 `the same worker` / `he returns` / `the previous contractor` —— 跨段无记忆，指代等于放弃控制，必须整块重述 SHORT。
2. SHORT 块放在**动作动词之前**，紧贴主语位置：`At the first instant, <SHORT> enters from the lower right with a matte-black shovel...`
3. 一条提示词里只出现**一次**完整身份块，后续用 `he` 回指，避免重复描写把注意力权重打散。

---

## 三、两种流水线模式（按项目选一种，不要混用）

### 模式 A：无人静态锚点 + 视频内人物（推荐给现有石化仙人掌类流水线）

- 15 张首尾帧图片**全部不含人物**，人物只活在 14 条视频段里。
- 优点：图片之间零人物漂移风险；首尾帧插值只约束场景，不约束脸。
- 代价：人物完全由文本控制，脸部细节段间会有波动 —— 靠 T1 服装剪影承担识别，而不是靠脸。
- 适配写法：每条视频段固定用 SHORT 块，进场/离场路径照旧。

### 模式 B：人物驱动叙事（人物是主角、要看脸）

- 先出**角色转视图参考图**（见第四节），锁定 seed / 复用同一张参考图作 image-to-image 输入。
- 每张首尾帧图片都含杰克，用 MID 块；视频段用首尾帧插值，脸由图片承载。
- 优点：脸部一致性显著更高。
- 代价：每张静态图都要过一次人物 QC，工作量翻倍；且任一张图漂了，整条链继承错误。

> 判断标准：镜头里杰克的脸占画面高度 **超过 1/6** 就必须走模式 B；否则模式 A 足够。

---

## 四、角色参考图（模式 B 前置，4 张）

四张共用同一句机位与光线约束，保证是"同一个人的四个角度"而不是四个人。

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
Close-up of Jake Miller's hands in yellow leather gloves gripping a matte-black tool handle, sun-reddened forearms, faded olive-green jacket cuffs visible, plain neutral mid-grey background, flat even studio lighting. No face, no text, no logos.
```

---

## 五、集成到现有提示词集（必做的替换）

现有 `veo_petrified_saguaro_prompt_set.md` 视频 1 写的是：

```text
one worker in a solid sand-colored shirt, dark trousers, orange gloves, and a white hardhat
```

这与杰克的 T1 锚点直接冲突（沙色衬衫 vs 橄榄绿夹克、橙手套 vs 黄手套、白安全帽 vs 深灰棒球帽）。采用本方案时替换为：

```text
Jake Miller, the bearded broad-shouldered contractor in a dark-grey baseball cap, faded olive-green work jacket, charcoal-grey T-shirt, brown canvas trousers, dark-brown boots, and yellow leather gloves,
```

其余各段的处理规则：

| 原文写法 | 替换为 |
|---|---|
| `one worker enters...` | `<SHORT> enters...` |
| `the worker carries out both crates` | `he carries out both crates`（同段内回指，允许） |
| `two workers enter from the right` | `<SHORT> and one unnamed assistant in plain dark clothing enter from the right` |
| `the worker exits` | `he exits`（同段内）|

**双人镜的额外约束**：助手必须写成 `plain dark clothing, no cap, clean-shaven, visibly shorter`，明确区分度，否则模型会生成两个杰克或把两人特征互串。

---

## 六、负面清单（每条含人物的提示词都要带）

```text
No hardhat, no hi-vis vest, no sunglasses, no visible tattoos, no jewelry, no readable text or logos on clothing, no change of jacket colour, no beard length change, no second person resembling him, no face distortion, no extra fingers.
```

按需精简，但 `no readable text or logos on clothing`、`no change of jacket colour`、`no second person resembling him` 三条建议常驻。

---

## 七、漂移 QC 表（每段成片逐项核对）

| # | 检查项 | 判定标准 |
|---|---|---|
| 1 | 帽子 | 深灰棒球帽，非安全帽/无帽 |
| 2 | 夹克 | 褪色橄榄绿工装夹克，未变卡其/军绿 |
| 3 | 内搭 | 炭灰色 T 恤，领口可见 |
| 4 | 裤 | 棕色帆布工具裤，非牛仔 |
| 5 | 靴 | 深棕皮靴，非黑非浅棕 |
| 6 | 手套 | 黄色皮手套（未戴时须在画面内可见） |
| 7 | 胡须 | 修剪整齐的浅棕短须，非络腮非胡茬 |
| 8 | 发色 | 浅棕短发，帽檐下露出 |
| 9 | 眼睛 | 灰蓝色（仅特写段判定） |
| 10 | 体格 | 宽肩结实，非瘦削 |
| 11 | 肤色 | 晒红风化感，非苍白 |
| 12 | 唯一性 | 画面内不存在第二个相似人物 |
| 13 | 服装文字 | 无 logo、无可读文字 |
| 14 | 进离场 | 有明确进场与离场路径，尾帧无遗留人物 |

判定口径：1～8 项任一不合格 → **该段重生成**（这些是观众用来"认人"的剪影级特征）。9～11 项不合格且非特写 → 可放行。12～14 项不合格 → 重生成。

---

## 八、常见失败模式与对策

| 失败模式 | 成因 | 对策 |
|---|---|---|
| 段间换装 | 各段服装描述用了同义词 | 严格套用第一节固定词表，做一次全文 grep 比对 |
| 胡须变络腮/变光头 | 只写了 `bearded` | 必须写全 `neatly trimmed light-brown short beard` |
| 双人镜出现两个杰克 | 助手无差异化描述 | 助手强制写 `plain dark clothing, no cap, clean-shaven, visibly shorter` |
| 手套时有时无 | 动作段模型自动脱手套 | 写死 `gloves stay on throughout` 或明确 `he removes the gloves and clips them to his belt`（择一，全片统一）|
| 脸在远景糊、近景变人 | 远近景共用同一描述 | 近景切换到 MID/FULL 并补 `grey-blue eyes`、`square jaw` |
| 时间流逝拍摄里人物闪现 | time-lapse 语义与人物连续性冲突 | 保留现有 `continuous construction time-lapse, not real-time footage` 写法，人物只在首尾半秒明确进离场 |
| 越到后段越不像 | 提示词后半段身份词被动作词稀释 | 身份块前置到动作动词之前，全条只出现一次 |

---

## 九、落地检查清单

- [ ] 固定词表已写入项目、全片提示词按表 grep 核对通过
- [ ] 选定模式 A 或 B，全片不混用
- [ ] （模式 B）5 张角色参考图已生成并通过 QC 表
- [ ] 现有 14 条视频提示词的 `one worker` / `two workers` 着装句已全部替换
- [ ] 每条含人物提示词均带负面清单三条常驻项
- [ ] 每段成片过一遍 14 项 QC 表，1～8 项零容忍
