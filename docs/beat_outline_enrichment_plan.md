# 节拍简介「信息增稠」方案

> **落地状态（2026-08-08）：P1–P4 全部上线，`_OUTLINE_RICH_GATE_ENFORCING` 已打开。**
> 实施时对本方案做了三处有意偏离，原因见下，代码注释里也各留了一份：
>
> 1. **富字段拆成两个独立降级包**，而不是一个原子包：事实源 `en`/`mat` 与拍属性
>    `zone`/`scope`/`trace` 各自验收、各自剥离。绑成一包实测会出现"模型写对了
>    `en`/`mat`、漏了 `zone`，整条被剥成 `{op,text}`"——连合格的跨语言锚点一起扔掉，
>    比不加字段更糟。见 `_outline_rich_entry_violations` / `_outline_beat_property_violations`。
> 2. **痕迹链只查到下一拍**，不按 §5.2 的"第 k 条的 trace 必须出现在第 k+1..N 拍"。
>    `persistent_traces` 每拍只有两三条，第 12 拍不可能同时背着前 11 条的残留，那样
>    的契约必然每单必违、把重排循环烧穿。相邻两拍连得上链条自然是通的；"后面还在
>    不在"交给帧审查在真图上查（§5.7 已落地）。见 `_outline_rich_binding_violations`。
> 3. **`changed_grid_cells` 与 `zone` 的绑定改成"同区必须共格"**，而不是"必须包含
>    该条 zone"：`changed_grid_cells` 是 A1-C3 的闭集坐标，`zone` 是卡片自己的分区名，
>    两者没有先验对应关系。同区共格是确定性可判、且真正治"空间横跳"的那一条。
>
> 未做（数据补写，不影响流程）：`prompt_pipeline` 里三份静态兜底选题仍是 `{op,text}`
> 形态，激发全败退时交付的卡片没有富字段，行为等同上线前。
>
> 日期：2026-08-08
> 前置：`节拍简介升级为硬规则`（2026-08-05）与 `beat_outline_delivery_audit_plan.md`（交付审查）。
> 那两套解决的是**"卡片上那条工序有没有被交付"**；本方案解决的是**"那条工序说清楚了没有"**——
> 约束链已经很硬，但被约束的**内容本身信息量太低**，硬约束等于把一句模糊的话钉死了。

---

## 1. 问题：链条是硬的，源头是稀的

一条卡片工序今天只有两个字段（[__init__.py:14372](../prompt_pipeline/__init__.py#L14372)）：

```json
{"op": "flooring", "text": "铺好防潮垫层与松木地板"}   // text ≤ 16 字，动词开头
```

而下游一拍的 ladder schema 要填的是：`milestone_name` / `before_state` / `after_state` /
`completion_extent` / `stage_scope` / `changed_grid_cells` / `package_operations` /
`persistent_traces` / `preserve_state` / `outline_delivery`。

**十个字段里，卡片只喂了两个的信息量。其余八个全是规划器凭空发挥。**
一比一契约（[:13250](../prompt_pipeline/__init__.py#L13250)）锁住的是"第 k 拍对应第 k 条"这个**编号骨架**，
锁不住"第 k 拍到底长什么样"。用户在弹窗里读到的 11 条，和最终 11 张图之间，
**数量对上了，内容仍是下游自由创作**。

### 1.1 最大的洞：跨语言桥是下游自己搭的

现在唯一的中英桥 `outline_delivery` 由**规划器**书写（[:13308](../prompt_pipeline/__init__.py#L13308)），
`check_outline_delivery_realized`（[:7201](../prompt_pipeline/__init__.py#L7201)）拿它的英文名词去 IMAGE 正文里匹配。

于是这条校验实际在做的是：**规划器自己说的话，合成侧有没有照抄**。

```
卡片(中文，事实源)  ──译──▶  规划器复述(英文)  ──匹配──▶  IMAGE 正文
       ↑                          ↑
   用户读的是这个            但校验只锚在这个
```

规划器把"防潮垫层"复述成 `moisture barrier membrane`、合成侧照抄，校验满分——
可卡片承诺的是**毛毡垫层 + 松木地板**两层，交付的是一张膜。编号对、复述对、图错。
**桥的一端没有钉在事实源上。**

### 1.2 三个被迫"猜"的字段，每个都是漂移面

| 下游字段 | 现在从哪来 | 猜错的后果 |
|---|---|---|
| `stage_scope` | 规划器自定 | 直接决定 IMAGE 用不用 "the entire / all" 措辞（[:10472](../prompt_pipeline/__init__.py#L10472)），猜成 small 则整面墙的活被写成局部进度 |
| `changed_grid_cells` | 规划器自定 | 空间无锚，相邻拍可以在完全不同的区域反复横跳 |
| `persistent_traces` | 规划器自定 | 第 k 拍的成果不进第 k+1 拍的痕迹表，就等于**做完又消失** |

这三个恰恰是画面连续性的三根柱子，卡片一根都没提供。

### 1.3 材料层判定跑在 16 个中文字上

`_outline_entry_family_span` / `outline_weight_violations`（[:13688](../prompt_pipeline/__init__.py#L13688)）
靠中文关键词猜条目跨了几个材料层。词表覆盖不到的写法（"做好基层处理"）就静默漏判——
**拍重均衡这道闸门的输入精度，等于一个中文关键词表的召回率。**

---

## 2. 原则

1. **加字段，不加字数。** `text` 保持 ≤16 字、动词开头——它是用户在弹窗里读的那一行，
   变长就毁了卡片。新增信息全部走**旁路结构化字段**，用户界面默认不显示。
2. **桥钉在事实源。** 英文复述由**卡片**产出，规划器只能沿用/扩写，不能另起炉灶。
3. **把下游在猜的，改成上游在报。** 只补那些下游**必然要填、且卡片有资格决定**的字段。
4. **全部可选，缺失即退回今天的行为。** 老缓存卡、老断点、手动主题一行代码不受影响。

---

## 3. 新条目 schema

```json
{
  "op": "flooring",
  "text": "铺好防潮垫层与松木地板",
  "en": "grey wool felt underlay laid edge to edge, oiled pine plank floor nailed over it",
  "mat": ["wool felt underlay", "oiled pine planks"],
  "zone": "floor",
  "scope": "large",
  "trace": "visible pine plank seams running lengthwise"
}
```

| 字段 | 类型 | 约束 | 喂给谁 |
|---|---|---|---|
| `op` | str | 现有 13 值枚举（[:12763](../prompt_pipeline/__init__.py#L12763)） | 已有：`outline_binding_violations` 第 2 道 |
| `text` | str | 现有：≤16 字、动词开头、单一里程碑 | 已有：用户界面 + 契约原文 |
| `en` | str | **英文**，命名物理动作与终态产物，8–20 词，禁占位 | **取代规划器自写的 `outline_delivery` 成为校验锚** |
| `mat` | str[] | 1–3 个具体材料/物件英文名词，须为 `en` 的子串 | `check_outline_delivery_realized` 的匹配词源；材料层判定 |
| `zone` | str | 取自卡片级 `zone_map` 的闭集 | `changed_grid_cells` 的上游约束；空间连续性 |
| `scope` | enum | `large` / `default` / `small` | 直接下发 `stage_scope`，锁死覆盖度措辞 |
| `trace` | str | 英文，这拍留下、此后每拍都得在的可见残留 | 并进后续拍的 `persistent_traces` |

卡片级新增一个字段（一次，不是每拍）：

```json
"zone_map": ["exterior shell", "entry threshold", "floor", "walls & ceiling", "sleeping nook", "service corner"]
```

3–6 个该载体自己的空间分区。它让 `zone` 成为**闭集取值**——闭集才能做确定性校验，
自由文本只能做字符串比对。

---

## 4. 激发侧闸门（新增，全部确定性、零模型成本）

放进 `outline_skeleton_violations` 同一层，加独立灰度开关 `_OUTLINE_RICH_GATE_ENFORCING`
（照 [:12833](../prompt_pipeline/__init__.py#L12833) 的先例，先只打日志跑一批真实激发）。

1. **EN 可用性**：`en` 必须过 `_OUTLINE_DELIVERY_PLACEHOLDER`（[:13450](../prompt_pipeline/__init__.py#L13450)）
   与 `_milestone_keywords` 两关——**直接复用合成侧现成的两个判据，口径天然一致**。
2. **材料锚定**：`mat` 每一项必须出现在本条 `en` 里（防止两个字段各说各话）。
3. **材料新颖度**：非 `clearing`/`threshold`/`reward` 的条目，其 `mat` 不得与前一条完全相同——
   相同材料连续两拍 = 画面上看不出进度。
4. **分区闭集 + 抖动上界**：`zone ∈ zone_map`；`zone` 切换次数 ≤ ⌈N/3⌉，
   且 `op="threshold"` 那条必须是外→内的那一次切换（与现有过门唯一性规则合流）。
5. **覆盖度分布**：不得全 `large`（"每拍都是整屋完工"＝没有进度感），也不得全 `small`；
   末条 `reward` 恒为 `large`。
6. **痕迹链**：`trace` 两两不得重复；`clearing` 之后的每条都必须有非空 `trace`。
7. **材料层判定改走 `mat`**：`_outline_entry_family_span` 优先按 `mat` 的英文名词判层，
   `mat` 缺失才回落到今天的中文关键词表——**闸门精度从词表召回率升级为模型自报**。

失败处理**不整批打回**：沿用 `_salvage_pacing_failures` 的思路，富字段不合格的条目
**剥掉富字段降级成 `{op, text}`**（就是今天的形态，诚实、不骗人），卡片照常交付，
只在 `idea.outline_enriched=False` 上留标。绝不因为多要了几个字段而把一批灵感烧掉。

---

## 5. 下游接线（改动点逐一）

### 5.1 `_outline_normalized_entries`（[:12793](../prompt_pipeline/__init__.py#L12793)）
透传新字段，保持"与 `_outline_entry_texts` 同顺序同过滤"这条铁律——
**下标错位会让整套契约的编号全歪**，新字段一律用 `.get()` 补 `None`，不改过滤条件。

### 5.2 `build_outline_plan_block`（[:13250](../prompt_pipeline/__init__.py#L13250)）
清单行从 `3. 文本 [op]` 扩成：

```
  3. 铺好防潮垫层与松木地板 [flooring] | zone: floor | scope: large
     EN: grey wool felt underlay laid edge to edge, oiled pine plank floor nailed over it
     MATERIALS: wool felt underlay, oiled pine planks | LEAVES: visible pine plank seams running lengthwise
```

同时把 **DELIVERY RESTATEMENT 段整段改写**——这是本方案的核心收益：

> 旧："你自己用英文复述一遍"
> 新："**逐字沿用**该条的 `EN`。只允许追加你这拍自己的具体名词，不得替换或省略
> `MATERIALS` 里的任何一个。这串会被拿去和最终 IMAGE 正文比对。"

并新增三条硬绑定：`stage_scope` 必须等于该条 `scope`；`changed_grid_cells` 必须包含
该条 `zone`；第 k 条的 `trace` 必须出现在第 k+1..N 拍的 `persistent_traces` 里。

### 5.3 `outline_binding_violations`（[:13456](../prompt_pipeline/__init__.py#L13456)）
在现有三道（认领序 / 工序类型 / 英文复述）后加第四道 **FIDELITY**：
规划器的 `outline_delivery[k]` 必须包含该条 `mat` 的全部名词。
今天这里只查"复述是不是英文、有没有实词"，查不出**换了词**——正是 §1.1 那个洞。

### 5.4 `bind_outline_to_ladder`（[:13552](../prompt_pipeline/__init__.py#L13552)）
`beat['outline_items']` 每项从 `{index, text, delivery}` 扩成
`{index, text, delivery, card_en, mat, zone, scope, trace}`。
**钉在 beat 上而不是另开参数**这条现有理由继续成立（一路流到批量/单拍/断点/回炉）。

### 5.5 `outline_delivery_directive`（[:10406](../prompt_pipeline/__init__.py#L10406)）
IMAGE 合成时给的那几行，从"中文原文 — 英文复述"扩成带 `MATERIALS`/`LEAVES` 的三行。
**合成侧终于拿到了真名词可抄**，而不是自己从一句中文里再翻一遍。

### 5.6 `check_outline_delivery_realized`（[:7201](../prompt_pipeline/__init__.py#L7201)）
匹配词源改为 `mat ∪ _trace_name_keywords(card_en)`，**规划器复述降级为补充**。
桥的两端这才都钉在卡片上：

```
卡片 en/mat（事实源） ──匹配──▶ IMAGE 正文
        └──约束──▶ 规划器复述（§5.3 查它有没有换词）
```

### 5.7 帧审查（`outline_frame_review_block`，[:11028](../prompt_pipeline/__init__.py#L11028)）
VLM 逐帧问句从"这拍的工序做完了吗"升级为**指名点姓**：
"Is `oiled pine plank floor` visible in the `floor` zone, with `visible pine plank seams`?"
——这是富字段唯一一处**不改任何闸门逻辑就白拿**的收益。

### 5.8 前端
`ideaBeatOutline`（[js/prompt_pipeline.js:220](../js/prompt_pipeline.js#L220)）透传新字段；
卡片轨道**一个字都不加**；弹窗（[:253](../js/prompt_pipeline.js#L253)）每条下面加一行浅色副文本
`floor · 松木地板 · 整体`。用户看到具体材料会更敢下单，但这是副产物，不是目标。

---

## 6. 分期（按"收益/风险"排序，每期可独立上线）

| 期 | 内容 | 收益 | 风险 |
|---|---|---|---|
| **P1** | `en` + `mat`；§4 的 1–3、§5.1/5.2/5.3/5.4/5.5/5.6 | **补上 §1.1 那个洞**——桥钉回事实源。单期就能把"编号对、内容错"这一类堵死 | 激发 JSON 增约 25 token/拍 |
| **P2** | `zone` + `zone_map` + `scope`；§4 的 4–5、`stage_scope`/`changed_grid_cells` 绑定 | 空间不再横跳；覆盖度措辞不再靠猜 | `zone_map` 是新的卡片级字段，静态兜底选题要补写 |
| **P3** | `trace`；§4 的 6、痕迹链约束、§5.7 帧审查指名 | 做完的东西不再消失；帧层校验从泛问变实指 | 痕迹链是跨拍约束，重排循环可能多跑一轮 |
| **P4** | §4 的 7：材料层判定改走 `mat` | 拍重闸门精度脱离中文词表 | 与既有中文判据并存期要对拍一批历史卡验证不回退 |

**先做 P1。** 它是唯一一个"不改任何空间/时序语义、只把已有校验的锚点挪对位置"的改动，
风险最低、修的却是最贵的那个 bug 形态。

---

## 7. 成本与风险

- **激发提示词膨胀**：`beat_outline` 那条 schema 说明本就是全文最长的一行。新字段说明
  必须**同样压成一行**、且**给一个完整样例条目**——样例比规则管用，这是 `op` 字段上线时验证过的。
- **模型合规率下降 → 150s 重烧**：由 §4 的**降级而非打回**兜住。降级卡 = 今天的卡，
  不存在"比现状更差"的结局。上线后看 `outline_enriched` 的比率决定要不要收紧。
- **token 成本**：6 张卡 × 12 拍 × 约 25 token ≈ 1.8k 输出 token/次激发，可接受。
- **与一比一契约的关系**：本方案**不碰**条目数与拍数的绑定，`_outline_strict` 分支
  一行不改。富字段只影响每一拍**内容**的确定度。

---

## 8. 验收（新增测试）

放进 `tests/test_ideation_trend_sources.py`（激发侧）与
`tests/test_anchor_lifecycle_and_contracts.py`（契约侧），沿用两处现有的组织方式：

1. 富字段条目 + 规划器换掉 `mat` 名词 → §5.3 的 FIDELITY 必须报错（**P1 的核心回归**）。
2. 富字段条目 → `outline_items` 带全 `card_en/mat/zone/scope/trace`，断点续传后仍在。
3. IMAGE 正文只提规划器复述的词、不提 `mat` 的词 → `check_outline_delivery_realized` 必须判缺失。
4. 三种形态混装（纯字符串 / `{op,text}` / 富字段）→ 下标、覆盖率、一比一判定与今天逐位相等。
5. `en` 写成中文 / 占位串 / `mat` 不在 `en` 里 → 该条被剥成 `{op,text}`，卡片仍交付，
   `outline_enriched=False`。
6. `scope` 与规划器 `stage_scope` 冲突 → 报错且错误文案指名两个值（喂回重排要能自愈）。
