# 推进节奏均衡方案（全创意类型通用）

> 状态：**Q0-A / Q0-B / Q1-C / Q1-D / Q2-F 与 §7 已实施**（2026-07-31）；
> Q2-E（R3 曲线形状门禁）代码已就位但**灰度关闭**，理由见 §8.3。
> 日期：2026-07-31
> 分支：`feat/result-slots-refactor`
>
> 落地位置：
> - 度量：`beat_delta_weight` / `_layer_family_span` / `ladder_delta_weights` /
>   `_LAYER_FAMILIES` / `_LAYER_FAMILIES_EN`（`prompt_pipeline/__init__.py`，紧挨
>   `_stage_scope_ladder_violations`）
> - 约束：`rhythm_ladder_violations` / `_arc_shape_violations` / `skeleton_rhythm` /
>   `_DEFAULT_RHYTHM` / `PACING_SKELETONS[*]['rhythm']`；开关
>   `_RHYTHM_GATE_ENFORCING`（默认 True）、`_RHYTHM_ARC_ENFORCING`（默认 False）
> - 交付：`beat_clip_seconds` / `beat_clip_speed` / `_RHYTHM_CLIP_TIMING`，
>   `_build_partial_prompt_block` 的 `PACE` meta；合并侧
>   `video_generator._clip_speed_from_meta` / `_atempo_chain` / `_paced_merge_filter`
> - 激发侧：`outline_weight_violations` / `_outline_entry_weight` /
>   `_outline_entry_family_span`，`compute_beats_floor` 的加权密度下界
> - prompt 同改：ladder schema 的 `RHYTHM BAND RULE`、`beat_outline` 的 EVEN WEIGHT 段、
>   每拍规则里的 `REWARD BEAT TWO-PHASE STRUCTURE`
>
> 测试：`tests/test_beat_rhythm_balance.py`（38 条）。
>
> 起因：用户观感反馈——「视频整体推进节奏感觉有点快，节拍量太少，变化量大；
> 但有时候变化又太少，节奏太慢。最后两个视频都是英雄展示镜头。」
>
> 前置阅读：`docs/beat_count_skeleton_plan.md`。那份方案解决的是**拍数**
> （卡片声称的密度 vs 成片实际密度对不上），本方案解决的是**拍重**
> （拍数对了，但每一拍装的东西忽多忽少）。两者正交，本方案假设前者已落地。

---

## 0. 先把问题定性：这不是「快」或「慢」，是方差

用户的两句抱怨看起来互相矛盾，其实是同一个量的两个尾巴：

| 观感 | 实际发生的事 |
|---|---|
| 「节拍量太少，变化量大」 | 某一拍同时打包 3 个工序、跨 3 个格位、跨 2 个材料层族（拍重 5.8） |
| 「变化又太少，节奏太慢」 | 紧挨着的那一拍只有 1 个工序、1 个格位、1 个层族（拍重 1.6） |

**而这两拍的屏幕时间都是恒定的 8 秒**（`prompt_pipeline/__init__.py:69`，
`VIDEO_DURATION = 8.0`）。观众感知到的「节奏」= 信息量 ÷ 屏幕时间，
于是同一条片子里既有跟不上的段落，也有拖沓的段落。

**所以目标不是「整体调快」或「整体调慢」，而是把每拍的变化量收进一条可控的带子里，
并让屏幕时间跟着变化量走。**

---

## 1. 根因（六条）

### 1.1 每拍变化量没有任何量化度量，只有「类型约束」

`milestone_ladder_violations`（`prompt_pipeline/__init__.py:2168-2230`）对一个普通施工拍
只查这些：

| 检查 | 位置 | 形态 |
|---|---|---|
| `package_operations` 数量 1~3 | `:2220` | 计数上下界 |
| `changed_grid_cells` ≤ 3、且 <2 时要求措辞可数 | `:2215`、`:2228` | 计数上下界 |
| `persistent_traces` ≥ 2 | `:2209` | 计数下界 |
| 跨相位打包（`_INCOMPATIBLE_PACKAGE_FAMILIES`，`:2161`） | `:2223` | 集合相交 |
| 弱措辞、里程碑重名 | `:2199`、`:2205` | 词表匹配 |

每一条都是**单拍内的、成员资格式的**判断。于是：

```
拍 A：package_operations=3, changed_grid_cells=3, stage_scope='large'   ← 全部合法
拍 B：package_operations=1, changed_grid_cells=2, stage_scope='default' ← 全部合法
```

A 和 B 的实际视觉增量差着数倍，**门禁里没有任何一条能看见这个差**。
更关键的是：**没有任何一条规则比较拍 i 和拍 i-1**，也没有任何一条看整条序列的形状。

> 这就是方差问题的直接机制。所有现存门禁都在管「这一拍合不合法」，
> 没有人管「这一拍和上一拍差多少」。

### 1.2 `stage_scope` 在真实数据里是个常量，不携带任何变化量信息

> **2026-07-31 落地时的实测纠正。** 本节初稿推断「run 规则把变化量和工序切换绑死了」，
> 动手时核对发现前提不成立，实际情况比推断的更简单也更糟：

**`_stage_scope_ladder_violations`（`:2233-2279`）定义了，但生产路径从未调用它。**
全仓引用只有 `tests/test_threshold_variants.py`；ladder 校验点
（`:6122`）只调了 `milestone_ladder_violations` 一个。

而 ladder schema 的第 7 条（`:5936`）明文写着：

> `"stage_scope"`: Set **"large" on every ordinary construction beat** for backward
> compatibility. Omit or set null on threshold/reward/bridge/hard-cut beats.

所以真实数据里**每一个普通施工拍的 `stage_scope` 都是 `large`**，
`normalize_beat_ladder`（`:2134-2138`）只是在模型漏给时补个 `default` 兜底。

**结论：`stage_scope` 是个常量，它既不制造「每拍都跳」，也不制造「连着两拍没变化」——
它根本不参与区分。** 那么变化量的差异实际来自哪里？只剩三个量：

| 量 | 门禁 | 有没有下界 |
|---|---|---|
| `package_operations` 条数 | 1~3（`:2220`） | 有，但下界是 1 |
| `changed_grid_cells` 跨度 | ≤3（`:2228`）、<2 时要求措辞可数（`:2215`） | 半个 |
| 材料层族跨度 | **无任何门禁** | 无 |

**三者都只防「一拍塞太多」，不防「一拍装太少」，更不防这两种拍紧挨着。**
于是「装一盏吊灯」（1 工序 / 1 格位 / 1 族）和「封板+批腻子+刷漆」
（3 工序 / 3 格位 / 2 族）都是完全合法的相邻拍——这就是那两句抱怨的实际来源。

> 顺带：`_stage_scope_ladder_violations` 是否该接回生产路径是另一个问题，
> 本方案不处理（见 §8.2）。但**它没被调用这件事必须先写在这里**，
> 否则下一个人会像本文档初稿一样，基于一条不生效的规则去推断行为。

### 1.3 屏幕时间恒定，与拍重无关

`VIDEO_DURATION = 8.0`（`:69`）是全局常量，每一段 i2v 都是同样长度；
合并时 `_merge_filter`（`video_generator.py:1094`）只施加一个**全局** `setpts` 倍速，
对每一段一视同仁。

一拍装 3 个工序是 8 秒，一拍装一盏灯也是 8 秒。**这是「节奏感」最直接的物理来源，
目前完全没有被调度。** 而这一条恰恰是全套方案里唯一不需要动 LLM、零重排成本的杠杆（见 §5）。

### 1.4 激发侧只管条数与顺序，不管每条的重量

`outline_skeleton_violations`（`:9495`）查的是：长度下界、末拍是 reward、过门唯一性与位置、
过门后清理、弱词前缀、条目重复。`compute_beats_floor`（`:9580`）的两个来源
（结构必备拍数、`_OUTLINE_SHRINK_TOLERANCE` 收缩容忍）**都只由条数派生**。

于是在门禁眼里：

```
"封板批腻子并刷完整个室内"     ← 1 条
"装一盏吊灯"                   ← 1 条
```

完全等价。**卡片阶段就已经埋好了方差**，合成侧再怎么补都是下游治标。

### 1.5 各创意类型的节奏差异只写在散文里，没有可执行参数

`PACING_SKELETONS`（`:9076-9120`）每个骨架只有两个键：`label_zh` 和一段英文 `summary`。
真正可执行的节奏约束散落在各处的专属常量里：

| 骨架 | 可执行约束 | 位置 |
|---|---|---|
| `dual_payoff` | `_DUAL_MIN_OUTLINE_ENTRIES` 等 5 个常量 + `_DUAL_EXTERIOR_FAMILIES` 词表 | `:9255`、`:9436` |
| `nested_space_payoff` | `_NESTED_MIN_*` 6 个常量 | `:9266-9275` |
| `linear_milestone` | **无** | — |

**新增一个创意类型时，没有地方可以声明它的节奏形状**——只能再手写一批
`_XXX_MIN_*` 常量散落进这个一万行的文件。用户要的是「针对后续所有创意类型」的方案，
这一条是可扩展性的根子，必须先解决。

> 佐证：`layer_families` 这份七族词表在 `:9691` 和 `:9784` 各内联了一份。
> 仓库自己在 `_WEAK_MILESTONE_PREFIXES_ZH` 的注释（`:2153-2155`）里写过
> 「两份必然漂移」的教训，这里已经在漂了。

### 1.6 收尾是两条连着的完工欣赏镜头

见 §7，已实施。

---

## 2. 总体设计：拍重 + 节奏曲线 + 时间分配

三层，各自可以独立上线、独立回滚：

```
┌ 第 1 层 · 度量 ──────────────────────────────────────────┐
│ beat_delta_weight(beat) → 一个标量「拍重」                │
│ 全部由 ladder 已声明的字段派生，不新增 LLM 输出字段        │
└──────────────────────────────────────────────────────────┘
                          ↓
┌ 第 2 层 · 约束 ──────────────────────────────────────────┐
│ PACING_SKELETONS[*]['rhythm'] → 每个创意类型的节奏曲线    │
│ rhythm_ladder_violations() 确定性校验，接进现有重排循环    │
└──────────────────────────────────────────────────────────┘
                          ↓
┌ 第 3 层 · 交付 ──────────────────────────────────────────┐
│ 合并阶段按拍重分配每段的屏幕时间（per-clip setpts）       │
│ 不动 LLM、不动 ladder，纯确定性后处理                     │
└──────────────────────────────────────────────────────────┘
```

**推荐上线顺序：第 3 层 → 第 1 层 → 第 2 层。**
理由：第 3 层零重排成本、当天可见效果、随时可关；用它先把观感问题压下去，
再慢慢做需要重排预算的第 1、2 层。这和 `beat_count_skeleton_plan.md` 里
「先做 P0-B 因为它产出 P0-A 的依据」是相反的顺序逻辑——那份方案的依赖是数据依赖，
这里的依赖是**风险依赖**。

---

## 3. 第 1 层：拍重（beat delta weight）

### 3.1 关键设计决策：派生，不申报

新增函数，建议放在 `_stage_scope_ladder_violations`（`:2233`）之后：

```python
# 一个「普通工序的一次完工跳变」= 1.0。这个基准刻意选在 large 单工序单格位上，
# 因为它是整条序列里出现频率最高的形态，读数直觉最好。
_SCOPE_WEIGHT = {'large': 1.6, 'small': 0.8, 'default': 0.6}
_GRID_SPAN_WEIGHT = 0.30      # 每多跨一个 Grid 格位
_FAMILY_SPAN_WEIGHT = 0.40    # 每多跨一个材料层族

def beat_delta_weight(beat):
    """一拍的视觉变化量标量。

    **全部由 ladder 已声明的字段派生，绝不要求模型额外申报一个 weight 字段。**
    两条理由：
      1) 模型评估自己的「变化量」必然乐观——它刚写完这一拍，主观上就是「一件事」；
      2) 多一个并列字段就多一处漂移。这正是 beat_count_skeleton_plan.md §1.3 记录的
         recommended_beats / beat_outline 双字段教训，不要在同一个文件里犯第二次。

    运镜拍（threshold / reward / bridge / hard_cut）返回 None：它们有专属契约，
    不承载施工增量，参与加权只会污染统计。
    """
    if beat.get('operation') in ('threshold', 'reward') \
            or beat.get('bridge_stage') or beat.get('hard_cut'):
        return None
    ops = len(beat.get('package_operations') or []) or 1
    w = ops * _SCOPE_WEIGHT.get(beat.get('stage_scope'), 0.6)
    w += _GRID_SPAN_WEIGHT * max(0, len(set(beat.get('changed_grid_cells') or [])) - 1)
    w += _FAMILY_SPAN_WEIGHT * max(0, _layer_family_span(beat) - 1)
    return round(w, 2)
```

`_layer_family_span(beat)` 数这一拍的 `package_operations` + `milestone_name` 跨了几个材料层族，
**复用 §1.5 提到的那份七族词表**——同时把 `:9691` / `:9784` 的两份内联副本收编成一个
模块级常量 `_LAYER_FAMILIES`，顺手还掉那笔技术债。

### 3.2 拍重的实际落点（用来校准常量）

| 拍 | 派生 | 拍重 |
|---|---|---|
| 装一盏吊灯（1 op，1 格，1 族，default） | 1×0.6 | **0.60** |
| 装一盏吊灯（同上但 `large`，即真实数据的常态形态） | 1×1.6 | **1.60** |
| 铺完整面墙的龙骨（1 op，2 格，1 族，large） | 1×1.6 + 0.3 | **1.90** |
| 封板 + 批腻子（2 op，2 格，跨 2 族，large） | 2×1.6 + 0.3 + 0.4 | **3.90** |
| 封板 + 批腻子 + 刷漆（3 op，3 格，跨 2 族，large） | 3×1.6 + 0.6 + 0.4 | **5.80** |

**在 §1.2 说明的真实数据形态下（`stage_scope` 恒为 `large`），实际跨度是
1.60 ~ 5.80，约 3.6 倍**；把 `default` 拍也算进来的理论跨度是 9.7 倍。
两者屏幕时间完全相同——这个比值就是用户观感问题的量化表达，也是本方案要收敛的目标量。

### 3.3 先只观测，不打回

上线第一步**只在 ladder 接受处打一行 DEBUG 日志**，把整条序列的拍重数列打出来：

```python
print(f"[RHYTHM] weights={[beat_delta_weight(b) for b in beat_ladder]} "
      f"skeleton={pacing_skeleton_id}")
```

跑一批真实单，看真实分布再定 §4 的带宽。**不要凭这份文档里的估算值直接开门禁**——
`beat_count_skeleton_plan.md` §4.3 记录过误判的代价：150s 重试白烧 + 掉进静态兜底。

---

## 4. 第 2 层：节奏曲线（rhythm profile）

### 4.1 把节奏参数收进 `PACING_SKELETONS`

`PACING_SKELETONS`（`:9076`）每个条目加一个 `rhythm` 字典。**这是本方案对
「后续所有创意类型」的正面回答**：新增骨架时，节奏形状是一处声明式配置，
而不是再散落一批 `_XXX_MIN_*` 常量。

```python
'linear_milestone': {
    'label_zh': '单线里程碑推进',
    'summary': '...',
    'rhythm': {
        'weight_band': (0.9, 2.6),   # 普通施工拍的软区间
        'hard_ceiling': 3.4,         # 超过必须拆拍，无条件打回
        'neighbor_ratio': 2.0,       # 相邻两个施工拍的拍重比值上限
        'arc': 'front_load_plateau', # 曲线形状，见 4.3
        'tail_accel_from': 0.75,     # 从序列 75% 处进入加速段
    },
},
```

三个骨架的建议初值：

| 骨架 | `weight_band` | `hard_ceiling` | `neighbor_ratio` | `arc` |
|---|---|---|---|---|
| `linear_milestone` | (0.9, 2.6) | 3.4 | 2.0 | `front_load_plateau` |
| `dual_payoff` | (0.9, 2.4) | 3.2 | 1.8 | `two_arcs` |
| `nested_space_payoff` | (0.8, 2.4) | 3.2 | 1.8 | `two_arcs_reset` |

`dual` / `nested` 的带子更窄、比值更严，因为它们本来就要在有限拍数里塞两幕，
**最容易发生的失效模式恰恰是把一幕压成一两拍**（`beat_count_skeleton_plan.md` §8.5
记录的就是这个失败路径，且它会自我强化）。

同时给 `rhythm` 一份模块级默认值 `_DEFAULT_RHYTHM`，
`PACING_SKELETONS` 里缺 `rhythm` 键的骨架自动回落——**新增创意类型时不写这个键也不会炸**，
只是拿不到定制曲线。这条向后兼容性对「后续所有创意类型」是必需的。

### 4.2 校验器：`rhythm_ladder_violations(beat_ladder, skeleton_id)`

三条规则，按误判风险从低到高排：

**规则 R1 · 硬天花板（误判风险：低）**
```python
if w > rhythm['hard_ceiling']:
    -> f'Beat {idx} packs a delta weight of {w} (ceiling {ceiling}) — split it into two beats.'
```
只打最极端的那一小撮。一个拍重 5.8 的拍就是「封板+腻子+刷漆一次做完」，
**它本来就该被 `_INCOMPATIBLE_PACKAGE_FAMILIES` 拦住却漏了**（那三组家族对
`drywall`+`priming`+`painting` 这种同相位串联无能为力，`:2161-2165`）。
R1 是那道门禁的定量补充，不是新增限制。

**规则 R2 · 相邻比值（误判风险：低，收益最高）**
```python
for a, b in 相邻的两个普通施工拍:
    if max(w_a, w_b) / min(w_a, w_b) > rhythm['neighbor_ratio']:
        -> violation
```
**这是整套方案里性价比最高的一条。** 它完全不管绝对值，只管「不要突变」——
而用户的抱怨本质就是突变。误判风险低：只有差到 2 倍以上才响，正常序列碰不到。

实现注意：运镜拍（`beat_delta_weight` 返回 `None`）在配对时**跳过而不是断开**——
过门拍是节奏上的呼吸点，它两侧的施工拍仍然应该衔接得上。

**规则 R3 · 曲线形状（误判风险：中，建议灰度）**
见 4.3。**首次上线建议只记录不打回**，用一个和 `_OUTLINE_GATE_ENFORCING`
（`beat_count_skeleton_plan.md` §4.4 引入的那个开关）同款的模块级常量
`_RHYTHM_ARC_ENFORCING = False` 控制。

### 4.3 三种曲线形状

**`front_load_plateau`（linear_milestone）**
结构/外壳段偏重 → 中段平台 → 收尾软装加速（拍重递减）。
判据（宽松版）：最后 25% 的拍的平均拍重 **不高于** 前 75% 的平均拍重。
理由：软装阶段本来就该越走越快，观众此时已经知道结局，拖沓是最伤的。

**`two_arcs`（dual_payoff）**
外部幕、内部幕各自一条完整的「起—平—收」。过门拍是幕间呼吸点。
判据：两幕各自的平均拍重之差不超过 30%——**防的是「一幕丰满一幕干瘪」**，
这正是 §8.5 记录的那个失效模式的定量版本。

**`two_arcs_reset`（nested_space_payoff）**
同 `two_arcs`，但**第二幕的平均拍重应压到第一幕的 0.75~0.95 倍**。
理由是叙事性的，不是工程性的：观众在第一幕已经学完了整套材料梯
（清理→膜→龙骨→填充→封板→饰面），第二遍走同一条梯子必须更快，
否则就是重复。这一条是 `nested_space_payoff` 的 `summary` 里那句
"shorten beats as furnishing begins" 的可执行化。

### 4.4 挂载点

`compose_anchor_and_packet` 的 ladder 结构校验处（`:5452-5545` 那段三次重试循环，
`milestone_ladder_violations` 与 `_stage_scope_ladder_violations` 的调用点旁边）：

```python
errs = (milestone_ladder_violations(beat_ladder, mode)
        + _stage_scope_ladder_violations(beat_ladder)
        + rhythm_ladder_violations(beat_ladder, pacing_skeleton_id))   # 新增
```

**同时必须改 prompt。** `beat_count_skeleton_plan.md` §8.5 写得很明白：
「只收紧门禁而不告诉模型新规则 = 整批返工 150s」。
ladder 的 schema 说明（`:5931-5963`）要补一条：

> - RHYTHM BAND: consecutive ordinary construction beats must carry comparable visible
>   deltas. Do not let one beat complete three chained operations across the whole space
>   while its neighbour installs a single fixture. If one beat's package is much heavier
>   than its neighbours', split it; if much lighter, merge it into the adjacent beat.

用**自然语言描述规则**，不要把拍重公式喂给模型——它会开始反向工程分数，
写出迎合公式但内容空洞的拍。

### 4.5 激发侧的对应改动（治 §1.4）

新增 `outline_weight_violations(idea)`，只拦一种情况：**单条 outline 里塞了 3 个及以上材料层族**。

```python
# 复用 §3.1 收编出来的 _LAYER_FAMILIES 中文词表
if _outline_entry_family_span(entry) >= 3:
    -> f'beat_outline entry "{entry}" bundles three or more material layers; split it.'
```

只拦 ≥3 族（不是 ≥2），因为「封板+批腻子」这类同相位串联在卡片粒度上是合理的表述。
**宁可漏判，不可误判**——这是这个文件里反复付过学费的原则。

并把 `compute_beats_floor`（`:9580`）的第二个来源从「条数 × 0.7」
改成「**按族跨度加权后的条数** × 0.7」：一条塞两族的条目按 1.5 条计。
这样一份「条数少但每条很重」的清单也能算出足够高的 `beats_floor`，
从源头压掉 §1.2 的「每个 run 长度为 1」形态。

> ⚠️ 这会改变 `compute_beats_floor` 的输出 → `dimensions` 内容变化 →
> `get_brief_fingerprint`（`:2304`）指纹换代 → 存量断点全部失效。
> 和 `beat_count_skeleton_plan.md` §5.4 记录的完全同构，**是期望行为，别当 bug 修**。
> `tests/test_compose_checkpoint_resume.py` 需要一起更新。

---

## 5. 第 3 层：屏幕时间随拍重分配（建议最先做）

### 5.1 为什么这一层最该先上

- **零 LLM 成本**：不改 prompt、不触发任何重排，不存在 150s 白烧的风险
- **纯后处理**：只动合并阶段，改错了重新合并一次就行，不用重跑整单
- **当天见效**：不需要等一批新单跑出来才能评估
- **可独立回滚**：一个开关关掉就回到现状

### 5.2 现状与可行性

Google FX 的 i2v 不暴露时长参数（`integrations/google_fx/services/google_fx_video.py`
全文无 duration 相关调用），**每段固定 8 秒，这一层改不了**。

但合并阶段可以：`merge_project_videos`（`video_generator.py:1103`）用 concat demuxer
拼接（`:1233-1238` 写 `concat_list.txt`），再施加一个全局 `setpts`
（`_merge_filter`，`:1094`）。**把「全局一个倍速」换成「每段自己的倍速」是完全可行的。**

### 5.3 做法

拍重 → 屏幕时间：

```python
_CLIP_SECONDS_MIN, _CLIP_SECONDS_MAX = 4.0, 11.0

def beat_clip_seconds(weight, rhythm):
    """把拍重换算成该段的目标屏幕时间。

    开平方是刻意的：信息量翻倍时屏幕时间只给约 1.4 倍。纪录片式推进本来就该
    「越重的拍相对越紧」——线性分配会让重拍拖沓，而重拍恰恰是观众注意力最高的时刻。
    """
    ref = sum(rhythm['weight_band']) / 2          # 带子中点作参考拍重
    target = VIDEO_DURATION * (weight / ref) ** 0.5
    return min(_CLIP_SECONDS_MAX, max(_CLIP_SECONDS_MIN, round(target, 1)))
```

实测落点（默认带子，参考拍重 1.75）：0.6 → 4.7s；0.9 → 5.7s；1.6 → 7.6s；
1.75 → 8.0s；1.9 → 8.3s；2.6 → 9.8s；3.4 → 11.0s（封顶）。

实现上有两条路，**推荐第二条**：

| 路 | 做法 | 评价 |
|---|---|---|
| A. concat demuxer + `outpoint` | 在 `concat_list.txt` 每行后加 `outpoint N`，只能**截短**不能拉长 | 简单但只有一半能力，且截短会砍掉工人退场的收尾（违反 Out-and-In 契约的观感前提） |
| B. `filter_complex` 多输入 + 每段 `setpts` | 每段单独 `-i`，各自 `setpts=k_i*PTS`，再 `concat` filter | 能拉能缩，且变速比截断更符合「时间流速」的语义 |

路 B 的形状：

```
-i clip1.mp4 -i clip2.mp4 ... -i clipN.mp4
-filter_complex "[0:v]setpts=0.75*PTS[v0];[1:v]setpts=1.21*PTS[v1];...
                 [v0][v1]...[vN-1]concat=n=N:v=1:a=0[v]"
```

**注意与现有全局倍速的关系**：`merge_project_videos(speed=2.0)` 的全局倍速要
**乘**进每段的系数，不能并列——否则两套时间缩放会互相打架。
`_normalize_merge_speed`（`:1080`）限定 1/1.5/2 三档的约束保持不变。

**音频**：`_merge_filter` 在 `has_audio` 时用 `atempo`。每段不同倍速时要逐段 `atempo`
再 `concat=a=1`；`atempo` 单次只接受 0.5~2.0，本方案的系数范围（约 0.55~1.4）在界内，
但要在实现时显式断言，不要静默出一段音画不同步的成片。

**运镜拍不参与**：过门拍、reward 拍拍重为 `None`，保持 `1.0` 系数原样通过。
它们的时长是叙事设计的一部分，不该被施工密度调制。

### 5.4 开关与留痕

```python
_RHYTHM_CLIP_TIMING = True   # False = 全部走 1.0，退回改造前的等长拼接
```

并在 manifest 里给每段记一个 `clip_speed` 字段——**没有留痕的时间缩放是不可复现的**，
出了观感问题无法定位是哪一段的系数不对。

---

## 6. 分级与预估

| 级别 | 内容 | 状态 |
|---|---|---|
| **Q0-A** | `beat_delta_weight` + `_LAYER_FAMILIES` 收编 + `[RHYTHM]` 日志 | ✅ 已实施 |
| **Q0-B** | 第 3 层：合并阶段按拍重分配屏幕时间 | ✅ 已实施（`_RHYTHM_CLIP_TIMING`） |
| **Q1-C** | 规则 R1 + R2 接进 ladder 重排循环 + prompt 同步 | ✅ 已实施（`_RHYTHM_GATE_ENFORCING`） |
| **Q1-D** | `PACING_SKELETONS[*]['rhythm']` + `_DEFAULT_RHYTHM` 回落 | ✅ 已实施 |
| **Q2-E** | 规则 R3 曲线形状 | ⏸ 代码就位，灰度关闭（见 §8.3） |
| **Q2-F** | 激发侧 `outline_weight_violations` + 加权 `compute_beats_floor` | ✅ 已实施 |

### 6.1 上线后第一件要做的事：看 `[RHYTHM]` 日志

§3.2 的拍重落点是**估算**，`weight_band` / `neighbor_ratio` 的初值也是。
ladder 每次被接受时会打一行：

```
[RHYTHM] skeleton=linear_milestone weights=[1.9, 2.3, None, 1.6, ...] avg=1.94 max/min=2.10
```

跑一批真实单之后：

- `max/min` 若普遍远低于 `neighbor_ratio`，说明门禁形同虚设，可以收紧
- server.log 里若出现大量 `Beats X and Y sit next to each other` 的重排说明，
  说明 `neighbor_ratio` 定得太严，把 `_RHYTHM_GATE_ENFORCING` 改成 `False`
  即可退回「只打日志不打回」，无需回滚其余改动
- `avg` 是 `weight_band` 中点（默认 1.75）的校准依据——它同时决定屏幕时间的参考点，
  偏得多的话每一段的时长都会系统性偏移

### 6.2 落地时发现的两处与初稿不符

1. **`_stage_scope_ladder_violations` 从未被生产路径调用**，且 schema 要求每个普通拍
   都设 `large` → `stage_scope` 在真实数据里是常量。§1.2 已按实测重写。
2. **未申报里程碑字段的拍不计权**（返回 `None` 而不是最轻）。兜底 ladder（三次生成
   全失败时那条应急梯）只有 `operation`/`bridge_stage`，把它们当最轻拍会把每一段都
   压到 4 秒下限——在整单质量已经最差的那条路径上再叠一层节奏破坏。

---

## 7. 收尾双英雄镜头：去掉偏静止的那条（已实施）

### 7.1 两条镜头的实际差别

序列末尾确实连着两条在完工场景上运镜的片段：

| | 视频 N（reward 拍） | 视频 N+1（`[HERO]`） |
|---|---|---|
| 锚点 | IMAGE N → IMAGE N+1，双帧插值 | 只有 IMAGE N+1 一张，无结束锚点（`:7186-7187`） |
| 内容 | reward 动作（灯亮起 / 人物入住 / 机构展开） | **明令零内容运动**：`ZERO CONSTRUCTION CONTENT ... no motion of any kind except the camera itself`（`:7200`） |
| 运镜 | 允许摇镜（`allow_camera_sweep`，`:3303`、`:5524`） | 三选一的手持推/摇/拉（`:7199`） |
| 硬规则 | `MANDATORY CLIMAX VIDEO`：必须有机构的真实物理运动，**不能是静态保持**（`:8023`） | 无 |
| 结构地位 | **不可删**——它是唯一落到最终揭示图上的片段，删了序列就断在 IMAGE N | 可选附加，代码注释原文「锦上添花的附加步骤，不是硬门禁」（`:7162`） |

**两个判据指向同一个答案：`[HERO]` 就是偏静止的那条，也是唯一可以干净移除的那条。**

### 7.2 已实施的改动

在 `prompt_pipeline/__init__.py` 新增模块级开关，默认关闭 HERO 提示词的生成：

```python
_HERO_SHOWCASE_ENABLED = False
```

**选择关开关而不是删代码**，三条理由：

1. HERO 的下游管线牵涉 `video_generator.plan_video_slots` 的单帧分支、
   `merge_project_videos` 的可选附加逻辑、`server.py` 的 `is_hero` 字段透传、
   `js/slot_model.js:29-32` 的前端识别——**存量项目的 manifest 里还有 HERO 槽位**，
   删掉这些代码会让老项目重新合并时失败。
2. 用户手动上传一段收尾片到 HERO 槽位的路径（`source == 'manual_upload'`，
   `video_generator.py:1193`）仍然应该工作。
3. 观感类改动应当可逆。真觉得少了一条收尾镜头，改一个布尔值就回来了。

### 7.3 同时要做的事：让 reward 拍把欣赏的职责收回去

用户说「最后两个都是英雄展示镜头」——这说明 **reward 拍的视频当前也在往
「慢慢欣赏完工场景」的方向写**，而它的契约本来要求的是兑现动作（`:8023` 的
`MANDATORY CLIMAX VIDEO`）。删掉 HERO 之后，收尾的全部重量落在这一条上，
它必须同时承担「兑现」和「收束」。

建议在 reward 拍的专属指令里明确两段式结构（**未实施，属于 Q1 范围**）：

> REWARD BEAT TWO-PHASE STRUCTURE: the first two-thirds of the clip delivers the
> declared reward action as actual physical motion (the mechanism moving, the lights
> coming up, the occupant entering); the final third settles into a held, slightly
> tightened framing on the signature anchor with no further action — that settle IS
> the closing appreciation, so no separate showcase clip follows.

这句话同时解决两件事：兑现动作不会被写成静态保持（原有契约），
以及收尾有一个明确的呼吸点（HERO 原本提供的价值）。

---

## 8. 明确不做的事

### 8.1 不让模型申报拍重

理由见 §3.1。模型自评的变化量必然乐观，且并列字段必然漂移。
拍重必须是**从既有字段确定性派生**的量。

### 8.2 不废除 `_stage_scope_ladder_violations`

§1.2 指出的是它的副作用，不是它错了。它 2026-07-14 替掉的那个全局配额
（注释在 `:2241-2245`）造成的问题更严重——「大多数拍读起来是模糊的、局部的、
几乎注意不到的进展」。**新规则是在它之上加一个正交维度，不是替换它。**

### 8.3 不在第一版就开曲线形状门禁

R3（§4.3）是三条规则里唯一依赖「整条序列形状」的，也是最容易在合法的
特殊结构上误判的。默认 `_RHYTHM_ARC_ENFORCING = False`，先积累一批真实分布。

### 8.4 不动 i2v 的时长参数

Google FX 不暴露它（§5.2）。所有时长调度都在合并阶段做。
如果将来换了支持变长的 i2v 后端，§5.3 的 `beat_clip_seconds` 可以直接改成
生成侧参数，公式不用变。

---

## 9. 测试清单

新建 `tests/test_beat_rhythm_balance.py`：

**`beat_delta_weight`**
- 运镜拍（threshold / reward / bridge_stage=1 / hard_cut=True）四种各返回 `None`
- §3.2 表里四个落点逐一对上（这张表就是常量的回归锁）
- 缺字段的畸形 beat 不抛异常，落到最小权重

**`rhythm_ladder_violations`**
- 均匀序列 → 空列表
- 一拍拍重超 `hard_ceiling` → 命中 R1
- 相邻两拍 0.6 / 2.4（比值 4.0）→ 命中 R2
- **相邻两拍中间隔着一个过门拍 → 仍然按相邻处理**（§4.2 的跳过而非断开）
- `_RHYTHM_ARC_ENFORCING=False` 时 R3 不产出违规（灰度开关锁）
- 缺 `rhythm` 键的骨架 → 走 `_DEFAULT_RHYTHM`，不抛 KeyError（新增创意类型的兼容锁）

**`beat_clip_seconds`**
- 拍重 = 带子中点 → 恰好 `VIDEO_DURATION`
- 极重/极轻拍被夹在 `[_CLIP_SECONDS_MIN, _CLIP_SECONDS_MAX]` 内
- 全局倍速 2.0 与每段系数是**相乘**关系（§5.3 的易错点）
- 所有 `atempo` 系数落在 0.5~2.0 内

**HERO（已实施部分）**
- `tests/test_hero_showcase_video.py` 已更新：新增开关关闭时不生成 HERO 的用例，
  原有三层用例改为在开关打开时运行，**证明管线本身没被拆掉**

**回归**
- `tests/test_structural_beat_rework.py`、`tests/test_outline_skeleton_gate.py`
- `tests/test_compose_checkpoint_resume.py`（Q2-F 的指纹换代）
- 全量 `python -m pytest tests/ -q`

---

## 10. 验收标准

1. **同一条序列内，相邻两个普通施工拍的拍重比值 ≤ `neighbor_ratio`**（Q1-C 上线后）
2. **每段的屏幕时间与其拍重单调相关**，最重与最轻拍的时长比接近拍重比的平方根（Q0-B）
3. 全片拍重的**变异系数**（标准差 ÷ 均值）从改造前的基线下降 ——
   这是最直接的观测指标，Q0-A 的 DEBUG 日志就是为它准备的
4. `dual_payoff` / `nested_space_payoff` 的两幕平均拍重之差 ≤ 30%
5. 序列末尾**只有一条**完工镜头，且它包含真实的兑现动作而非静态保持
6. 新增一个创意类型时，**不写 `rhythm` 键也能正常工作**（回落到 `_DEFAULT_RHYTHM`）

最直接的人工观测：打一批真实单，把每拍的拍重和实际屏幕时间并排列出来，
看有没有「重拍一闪而过」或「轻拍杵在那儿」的条目。
这两类条目的数量就是用户那两句抱怨的量化对应物。
