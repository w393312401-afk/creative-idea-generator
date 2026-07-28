# 拍数与施工推进骨架优化方案

> 状态：**P0-B / P0-A / P1-D 已实施**（2026-07-28）；P1-C、P2-E 仍未做，理由见 §7、§8。
> 日期：2026-07-28
> 分支：基于 `feat/result-slots-refactor`
>
> 落地位置：`outline_skeleton_violations` / `compute_beats_floor` / `_outline_crossing_indices`
> 与灰度开关 `_OUTLINE_GATE_ENFORCING`（`prompt_pipeline/__init__.py`，紧挨
> `pacing_skeleton_outline_violations`）；`_beat_count_is_valid(..., floor=)`；
> `compose_anchor_and_packet` 里的 `beats_floor` 夹逼；前端 `js/prompt_pipeline.js`、
> `app.js`、`js/spark_rail.js`、`index.html`。测试见 `tests/test_outline_skeleton_gate.py`。
>
> 起因：用户提问「拍数滑块最高 15 拍是定死的吗」，顺藤查下去发现真正的问题不是上限，
> 而是**拍数这个量在激发侧和合成侧各说各话**：卡片声称的拍数、滑块显示的拍数、
> ladder 实际产出的拍数，三者之间没有任何强制关系。滑块只是这个问题最显眼的症状。
>
> 本方案的目标不是"能不能调到 20 拍"，而是**让用户在灵感卡片上看到的推进密度，
> 和最终成片的推进密度对得上**。

---

## 0. 给新窗口的前置阅读

动手前请先读这几段代码，本方案全程引用它们：

| 位置 | 是什么 |
|---|---|
| `prompt_pipeline/__init__.py:5104-5130` | `compose_anchor_and_packet` 开头，`beats_count` / `max_total_beats` / `min_total_beats` 的定义处 |
| `prompt_pipeline/__init__.py:85-95` | `_beat_count_is_valid`，adaptive/fixed 的拍数验收 |
| `prompt_pipeline/__init__.py:2098-2160` | `milestone_ladder_violations`，**合成侧**的确定性骨架校验（本方案要复用它的词表） |
| `prompt_pipeline/__init__.py:5452-5545` | beat ladder 生成 + 结构校验 + 三次重试循环 |
| `prompt_pipeline/__init__.py:8038-8130` | `_dual_payoff_crossing_indices` / `pacing_skeleton_outline_violations`，**激发侧**的骨架校验（本方案要泛化它） |
| `prompt_pipeline/__init__.py:8436-8500` | `run_ideate` 的重试循环，激发侧校验的挂载点 |
| `js/prompt_pipeline.js:440-500` | `loadIdeaIntoForm` / `clampRecommendedBeats` / `composeIdeationCard` |
| `index.html:322-341` | 拍数滑块与节拍规划模式的 DOM |

测试用 pytest（`pyproject.toml` 已设 `pythonpath="."`、`testpaths=["tests"]`），跑法：
```
python -m pytest tests/ -q
```

---

## 1. 根因（五条，按严重度排序）

### 1.1 adaptive 的拍数下界是全局写死的 5，与项目重量无关

```python
# prompt_pipeline/__init__.py:78
_MIN_ADAPTIVE_CONSTRUCTION_BEATS = 5
# prompt_pipeline/__init__.py:5127
min_total_beats = min(max_total_beats, _MIN_ADAPTIVE_CONSTRUCTION_BEATS + 1)
```

`count_contract`（:5395-5399）明确要求 ladder LLM 挑"能表达全部必要里程碑的**最小**拍数"，
而 `_beat_count_is_valid` 只要求落在 `[min_total_beats, max_total_beats]`。于是：

- 卡片推荐 13 拍的重型结构改造单，ladder 完全合法地塌回 6 拍
- 卡片推荐 6 拍的轻量单，下界 5 也谈不上约束

**这是"骨架不稳"的第一根因**，比滑块严重得多。用户挑卡时是按推进密度挑的，
成片密度却由一个与卡片无关的常量兜底。

### 1.2 激发侧的结构验收只对 dual_payoff 生效

```python
# prompt_pipeline/__init__.py:8067
if not isinstance(idea, dict) or idea.get('pacing_skeleton') != 'dual_payoff':
    return []
```

`linear_milestone`（默认骨架、占多数）的卡片**零校验**。`run_ideate` 那套完整的
`failures` / `best_batch` / 带错误回喂的三次重试机制（:8462-8487）对它形同虚设。

### 1.3 `recommended_beats` 与 `beat_outline` 是两个独立字段，无一致性校验

ideation schema（:8388-8391）里两条并列声明：

- `recommended_beats`：integer，5 到 15
- `beat_outline`：array of strings，"EXACTLY recommended_beats entries plus ONE final reward entry"

"array 长度 = recommended_beats + 1"只是 prompt 里的口头约定。
`_normalize_beat_outlines`（:8200-8221）只做类型归一（把字符串拆成 list、过滤空项），
**从不比对长度**。模型报 12、清单给 8 条，卡片照样显示「⏱ 推荐 12 拍」
（`js/prompt_pipeline.js:330`），而合成时传下去的是那个 12。

### 1.4 一键合成与手动合成走两条不同的拍数来源

| 路径 | 拍数来源 |
|---|---|
| 卡片「一键合成」 | `clampRecommendedBeats(idea.recommended_beats) \|\| 15`（`js/prompt_pipeline.js:489`），**完全不读滑块** |
| 载入卡片 → 手动生成 | 滑块值（`app.js:1974`），滑块由 `loadIdeaIntoForm` 预填（`js/prompt_pipeline.js:455-458`） |

同一张卡走两条路可能拿到不同拍数。而且 `loadIdeaIntoForm` 顺手把复杂度/预算/反差/尺度
重置成硬编码的 3/2/50/3（:451-454），和"载入这张卡"的语义不符。

### 1.5 滑块默认值 15 = 上限，在 adaptive 下等于没有约束

`index.html:329` 写死 `value="15"`，而 15 就是 `max`。adaptive 语义下 `beats_count` 是**上限**，
所以默认状态的滑块是个 no-op，却长得像个承诺。卡片写"推荐 12 拍"、滑块显示"15 拍"、
成片 8 拍——三个数字都对用户可见，互相矛盾。

---

## 2. 总体判断：滑块保留，但改语义

**不删。** 滑块真正承担的是**额度闸门**：15 拍 ≈ 16 张 IMAGE + 15 段 VIDEO 的 Google FX 额度，
这是真实成本，用户需要一个硬上限。`fixed` 模式（精确时长/精确额度）也是真实需求。

**要删的是它"定义施工骨架"这个职责**——那应该整体归卡片。

改造后的职责划分：

```
灵感卡片  →  施工骨架（拍数区间 [floor, max]、工序清单、必备拍结构）
滑块      →  额度上限（只能在卡片给的 max 之下再压，不能定义骨架）
合成 ladder → 在 [floor, max] 内挑最小可行拍数，硬规则优先级最高
```

---

## 3. 实施分级

| 级别 | 内容 | 依赖 | 预估 | 状态 |
|---|---|---|---|---|
| **P0-A** | 拍数区间化：新增 `beats_floor`，替掉写死的下界 | 无 | 小 | ✅ 已实施 |
| **P0-B** | 激发侧全骨架结构验收（泛化现有 dual_payoff 门禁） | 无 | 中 | ✅ 已实施 |
| **P1-C** | `beat_outline` 结构化（带 `operation` 字段），拍数由清单派生 | 改 schema | 中 | ⏸ 待观察（见 §7）<br>其中「拍数由清单派生」已随 §4.5 落地 |
| **P1-D** | 滑块降级 + 卡片区间展示 + `loadIdeaIntoForm` 修正 | P0-A、P0-B | 小 | ✅ 已实施 |
| **P2-E** | 拍数上限从 15 放开（可选，见 §8） | P0-A | 小 | ⏸ 未做（两个软天花板未处理） |

**建议顺序：P0-B → P0-A → P1-D → P1-C。**
P0-B 先做是因为它产出的"必备拍结构"正是 P0-A 计算 `beats_floor` 的依据。

---

## 4. P0-B：激发侧全骨架结构验收

### 4.1 关键约束：激发阶段不知道 `space_type` / `threshold_variant`

这两个字段是 **compose Step 1 的 brief 解析**（`prompt_pipeline/__init__.py:5183`、`:5198`）
才产出的，那时候卡片早就选完了。所以 P0-B **不能**依赖它们，必须从 outline 中文文本自行推断。

好消息是现成范本就在旁边：`_dual_payoff_crossing_indices`（:8038-8056）已经在干这件事，
且它的注释记录了一个重要教训——

> 旧实现把"搬入家具进入室内布置"也算成过门，于是整批因为"过门拍不止一处"被否掉，
> server.log 里那一长串 `must contain exactly one visible doorway-crossing entry`
> 绝大多数是这么来的，不是模型真写错了。

**新校验必须继承这个教训：宁可漏判，不可误判。**每一条规则都要问一句"正常的合格卡片
会不会被这条误伤"，误伤的代价是 150s 的重试白烧 + 最后掉进静态兜底列表。

### 4.2 新增函数

在 `prompt_pipeline/__init__.py` 的 `_dual_payoff_crossing_indices` 附近（约 :8057 之后）新增：

```python
def _outline_crossing_indices(outline):
    """把 _dual_payoff_crossing_indices 的判定泛化给所有骨架用。

    实现上就是把原函数改名 + 原地保留一个 _dual_payoff_crossing_indices 别名
    （或让原函数直接调用本函数），不要复制两份正则——两份必然漂移。
    """
```

然后新增通用门禁：

```python
def outline_skeleton_violations(idea):
    """所有 pacing_skeleton 共用的确定性骨架验收。

    返回中文/英文错误串列表（沿用 pacing_skeleton_outline_violations 的英文风格，
    因为这些串会被回喂给 LLM 当返工说明，见 run_ideate:8484）。

    与 pacing_skeleton_outline_violations 的分工：
    - 本函数：所有骨架都必须满足的通用结构（必备拍、末拍、弱词、重复）
    - 原函数：dual_payoff 独有的双完工叙事结构（保持不动）
    """
```

### 4.3 校验条目（逐条给出判据与误判风险）

**规则 1 · 长度下界**
```
len(outline) >= 4
```
再少就凑不出"起手 + 推进 + 收尾 + reward"。
*误判风险：低。*

**规则 2 · 末拍必须是 reward 揭示**
末条命中 `点亮|亮起|入住|完工|落成|成品|揭晓|收尾|交付` 之一。
schema 已经明确要求（:8391 "The LAST entry is the reward reveal"），这里只是把口头约定变成校验。
*误判风险：中。* 词表要给足；建议先在现有卡片样例（:8531 起的 few-shot 例子）上试跑，
确认全部通过再启用。**这条如果误判率高，降级成"只记录不打回"。**

**规则 3 · 过门拍唯一性（仅当检出过门时才生效）**
```python
crossing = _outline_crossing_indices(outline)
if len(crossing) > 1:  # 注意：== 0 不报错
    -> violation
```
`== 0` 不报错，因为 Standard 模式（无内外过门的载体，如纯外立面/庭院改造）本来就没有过门拍。
**只有"多于一处"才是真错误。** 这是相对现有 dual_payoff 门禁（要求恰好 1 处）
的关键放宽——现有那条对 dual_payoff 成立，因为该骨架定义上必有过门。
*误判风险：低（继承已调优的正则）。*

**规则 4 · 过门前留够室外拍**
```python
if crossing and crossing[0] < 2:
    -> violation  # 对齐 compose 侧 _MIN_PRE_THRESHOLD_BEATS = 2 (:5308)
```
*误判风险：低。这是 compose 侧的硬规则，提前到激发侧拦是纯收益。*

**规则 5 · 过门后第一拍必须是清理**
```python
if crossing:
    nxt = outline[crossing[0] + 1]  # 注意越界
    if not re.search(r'清[空运理除]|清出|搬空|搬出|扫|铲除|拆除|清理', nxt):
        -> violation
```
对齐 compose 侧的 `_post_crossing_cleanup_rule`（:5312-5321）与它的确定性校验（:5519-5529）。
*误判风险：中。词表要覆盖"清运碎冰"/"铲除锈渣"/"搬空杂物"等写法。*

**规则 6 · 弱里程碑措辞**
outline 是中文，而 `_WEAK_MILESTONE_PHRASES`（:2086-2090）是英文（`begins to`、`one corner`…），
**不能直接复用**。需要新建一份中文对照词表：

```python
_WEAK_MILESTONE_PHRASES_ZH = (
    '开始', '继续', '逐步', '局部', '部分', '一角', '一小块', '初步', '尝试', '推进中',
)
```
放在 `_WEAK_MILESTONE_PHRASES` 旁边，并加注释说明两者是"同一条规则的中英两侧"，
改一边要想到另一边。schema 里其实已经点名禁止"开始施工"/"继续完善"（:8391 末句），
这条只是把它变成可执行的校验。
*误判风险：中高。「开始」可能出现在合法语境里。建议只查**开头两字**或整条 ≤16 字里的独立出现。*

**规则 7 · 里程碑重复**
相邻或全局出现重复的 outline 条目（去空格后完全相同，或前 6 字相同）。
对齐 `milestone_ladder_violations` 的 `seen_names` 逻辑（:2128-2131）。
*误判风险：低。*

**规则 8 · `recommended_beats` 与 outline 长度一致（修根因 1.3）**
```python
rec = idea.get('recommended_beats')
if isinstance(rec, int) and rec > 0 and len(outline) != rec + 1:
    -> violation
```
**但更好的做法是不校验、直接改写**——见 §4.5。

**规则 9 · 工序顺序单调性 —— 本期不做，见 §9.1。**

### 4.4 挂载点

`run_ideate` 的验收循环，`prompt_pipeline/__init__.py:8462-8466`：

```python
failures = {}
for idea_idx, idea in enumerate(novel_ideas):
    errs = pacing_skeleton_outline_violations(idea)   # 现状
    if errs:
        failures[idea_idx] = errs
```

改成：

```python
failures = {}
for idea_idx, idea in enumerate(novel_ideas):
    errs = outline_skeleton_violations(idea) + pacing_skeleton_outline_violations(idea)
    if errs:
        failures[idea_idx] = errs
```

**重要：`_salvage_pacing_failures`（:8395-8415）不能直接沿用。**
它的降级逻辑是"把 dual_payoff 标签降成 linear_milestone，因为内容本来就是单线清单，
改标签之后标签不再骗人"。但通用结构违规（比如末拍不是 reward）**没有任何标签能让它变诚实**，
降级救不了。

处理方式：把 failures 分成两类
- **软失败**（只有 dual_payoff 专属违规）→ 走现有 `_salvage_pacing_failures` 降级
- **硬失败**（命中 `outline_skeleton_violations`）→ 该卡直接丢弃，不降级

实现上给 failures 的 value 加个标记，或者干脆维护两个 dict（`hard_failures` / `soft_failures`）。
`_deliver_best_batch` 的 `passed_count` 排序逻辑要相应更新。

**注意重试成本**：现有逻辑 `last_chance = attempt >= 2 or (passed > 0 and attempt >= 1)`（:8482）
——已经有卡过关时最多再补一次。新增校验会让 `passed` 变小、重试变多，每次 150s。
**建议先只打日志跑一轮观察通过率**，再决定要不要真的打回。可以用一个模块级开关：
```python
_OUTLINE_GATE_ENFORCING = True   # False = 只记录不打回，用于灰度观察
```

> 实施结果：按上面这行落地，默认 `True`（强制）。词表已对着仓库自带的全部 6 条样例
> 清单（静态兜底三条 + dual 兜底三条）跑通，见 `tests/test_outline_skeleton_gate.py::
> TestOutlineSkeletonGateAcceptsGoodCards::test_shipped_fallback_outlines_all_pass`。
> 灰度期若在 server.log 里看到通过率异常低（大量 `beat_outline` 相关返工说明），
> 把这个常量改成 `False` 即可退回「只打日志不打回」，无需回滚其余改动。

### 4.5 顺手修 1.3：拍数由 outline 派生，不再独立申报

在 `_normalize_beat_outlines`（:8200-8221）里，归一完 outline 之后直接改写：

```python
idea['beat_outline'] = items
if len(items) >= 2:
    with_outline += 1
    # recommended_beats 一律由清单长度派生，不再信任模型独立申报的那个数。
    # 两个字段并列存在时它们必然漂移（见 docs/beat_count_skeleton_plan.md §1.3），
    # 而清单是用户在卡片上真正看到的东西，它才是事实来源。
    idea['recommended_beats'] = len(items) - 1
```

这比"校验不一致就打回"更好：**零重试成本，且 100% 消除不一致。**
schema 里 `recommended_beats` 的描述（:8388）相应改成"仅作规划参考，最终以 beat_outline 长度为准"，
或者干脆从 schema 删掉这个字段（让模型专心写清单）。

> ⚠️ 删字段前确认前端：`js/prompt_pipeline.js:259`、`:329`、`:455`、`:489` 都读它。
> 派生赋值的做法保持字段存在，前端零改动，**推荐走派生这条**。

---

## 5. P0-A：拍数区间化

### 5.1 后端：新增 `beats_floor`

`prompt_pipeline/__init__.py:5116-5128` 现状：

```python
beats_count = max(1, int(dimensions.get('beats_count', 15)))
...
max_total_beats = beats_count + 1
min_total_beats = min(max_total_beats, _MIN_ADAPTIVE_CONSTRUCTION_BEATS + 1)
```

改为：

```python
beats_count = max(1, int(dimensions.get('beats_count', 15)))
...
max_total_beats = beats_count + 1
# beats_floor：本项目的施工拍下界，由灵感卡片的骨架推出（见 compute_beats_floor）。
# 缺省回落到全局常量 = 完全保持旧行为，因此老任务/老断点/手动输入主题的路径不受影响。
beats_floor = dimensions.get('beats_floor')
try:
    beats_floor = int(beats_floor)
except (TypeError, ValueError):
    beats_floor = _MIN_ADAPTIVE_CONSTRUCTION_BEATS
beats_floor = max(_MIN_ADAPTIVE_CONSTRUCTION_BEATS, min(beats_floor, beats_count))
min_total_beats = min(max_total_beats, beats_floor + 1)
```

三处夹逼的意义：
- `max(_MIN_ADAPTIVE_CONSTRUCTION_BEATS, ...)`：下界只能抬高不能降低，卡片给个 2 也不会破坏既有保底
- `min(..., beats_count)`：下界不能超过上限，否则 `_beat_count_is_valid` 变成永假、每次都掉进兜底 ladder（:5551-5570）
- `min(max_total_beats, beats_floor + 1)`：保持原式的形状

`_beat_count_is_valid`（:85-95）本身**不用改**——它已经接受 `min/max` 两端。
但它内部还硬编码了一次 `_MIN_ADAPTIVE_CONSTRUCTION_BEATS`：

```python
# prompt_pipeline/__init__.py:94
minimum = min(max_total, _MIN_ADAPTIVE_CONSTRUCTION_BEATS + 1)
```

**这里必须一起改**，加一个 `floor=None` 形参，否则 :5467 的校验用的还是旧下界，
`min_total_beats` 只在 prompt 文案（`count_contract`，:5396）和兜底路径（:5553）里生效，
**LLM 返回一个 6 拍 ladder 照样被判合格**——这是本方案最容易漏掉的一处，务必确认。

```python
def _beat_count_is_valid(candidate_total, max_total, mode='adaptive', floor=None):
    ...
    base = _MIN_ADAPTIVE_CONSTRUCTION_BEATS if floor is None else floor
    minimum = min(max_total, base + 1)
```
调用点 :5467 相应传 `floor=beats_floor`。

### 5.2 `beats_floor` 怎么算

在 `prompt_pipeline/__init__.py` 里，紧挨 `outline_skeleton_violations` 新增：

```python
_OUTLINE_SHRINK_TOLERANCE = 0.7   # ladder 相对卡片清单最多收缩 30%

def compute_beats_floor(idea):
    """由灵感卡片的骨架推出该项目的施工拍下界（不含 reward 拍）。

    两个来源取大：
      1) 结构必备拍 —— 有过门时 4（室外 x2 + 过门 + 过门后清理），无过门时 2
      2) 清单收缩容忍 —— ceil((len(outline) - 1) * _OUTLINE_SHRINK_TOLERANCE)

    取大而非取小：结构必备是物理下限，清单容忍是密度下限，两者都要满足。
    """
```

对一张 13 条 outline（12 施工拍 + reward）的重型 Threshold 单：
`max(4, ceil(12 * 0.7)) = max(4, 9) = 9`。ladder 从此不能塌到 9 拍以下。
对一张 6 条 outline 的轻量单：`max(2, ceil(5*0.7)) = max(2, 4) = 4`，
再被 §5.1 的 `max(_MIN_ADAPTIVE_CONSTRUCTION_BEATS, ...)` 抬到 5——旧行为，无回退风险。

`_OUTLINE_SHRINK_TOLERANCE` 建议做成模块级常量并写明调参含义：
调高 → 更贴卡片、ladder 自由度更小、结构校验失败重排概率上升；调低 → 更宽松、更接近现状。
**首次上线建议 0.7，观察一批真实单之后再定。**

### 5.3 前端接线

`js/prompt_pipeline.js:480-500` 的 `composeIdeationCard`，dimensions 里加一条：

```javascript
beats_count: clampRecommendedBeats(idea.recommended_beats) || 15,
beats_floor: idea.beats_floor,     // 新增，由后端在 idea 上算好带出
beat_count_mode: 'adaptive',
```

`beats_floor` 由后端在 `run_ideate` 里算好挂在每条 idea 上（和 `trend_ref_ids` 的做法一致，见 :8238），
**前端不做计算**——计算逻辑必须和后端校验同源，放前端必然漂移。

`loadIdeaIntoForm` 那条手动路径也要带上：把 `idea.beats_floor` 暂存到某个 state 里，
`app.js:1974` 组装 dimensions 时一起发。

> ⚠️ `server.py` 无需改动：`dimensions` 从 `body.get('dimensions', {})`（server.py:3282）
> 到 `call_llm(config, dimensions, ...)`（:92）是整体透传的 dict，新字段自动流通。

### 5.4 断点续传的影响（必读）

`get_brief_fingerprint`（:2234-2241）把**整个 dimensions** 连同 `MILESTONE_POLICY_VERSION`
一起哈希。新增 `beats_floor` 字段会让所有指纹换代 → 存量断点全部失效、下次重排。

**这是期望行为**（旧断点是按旧下界规划的，续传会绕过新约束产出旧形态整单——
和 `MILESTONE_POLICY_VERSION` 注释 :75-77 记录的情况完全同构），
但实施时要主动确认，别当成 bug 去"修"。

`tests/test_compose_checkpoint_resume.py` 里的断点用例会受影响，需要一起更新。

---

## 6. P1-D：滑块降级与 UI

### 6.1 `index.html:322-341`

- `<span id="beat-count-label">最多施工节拍数 (Milestone Cap)</span>`
  → `节拍上限 · 额度闸门 (Budget Cap)`
- `app.js:679` 那句动态改 label 的逻辑同步改（adaptive/fixed 两态文案）
- 整个 `.control-group.spark-beats` 折进高级区（参考 `js/spark_rail.js:93` 已有的
  「收起节拍与模型细调」折叠态，直接放进那个容器）
- **`value="15"` 保留**——这是没载卡片、纯手动输入主题时的缺省，不改

### 6.2 载入卡片时同步区间

`js/prompt_pipeline.js:455-458` 现状只写 `slider-beats` 的 value。补上：

```javascript
const recBeats = clampRecommendedBeats(idea.recommended_beats);
if (recBeats !== null) {
    document.getElementById('slider-beats').value = recBeats;   // 上限跟卡片走，不再停在 15
}
```
（这行其实已经是对的，只是被 `app.js:692` 的初始 `dispatchEvent` 和 `value="15"` 掩盖了效果；
确认一遍即可。）

顺手修 §1.4 的另一半——`js/prompt_pipeline.js:451-454` 把四个滑块重置成硬编码 3/2/50/3：

```javascript
document.getElementById('slider-complexity').value = 3;
document.getElementById('slider-budget').value = 2;
document.getElementById('slider-ratio').value = 50;
document.getElementById('slider-creativity').value = 3;
```

这和"载入这张卡"的语义矛盾。要么让 idea 带出这四个维度、要么保留用户当前值，
**不要重置成常量**。建议保留用户当前值（改动最小、最不意外）。

### 6.3 卡片标签显示区间

`js/prompt_pipeline.js:329-331`：

```javascript
`<span class="ideation-card-tag beats" title="${idea.beats_reason || ''}">⏱ 推荐 ${idea.recommended_beats} 拍</span>`
```
→
```javascript
`⏱ ${idea.recommended_beats} 拍（不少于 ${idea.beats_floor}）`
```

节拍简介弹窗的说明文案（:259-264）也相应更新——现在那段写的是
"这是激发阶段的工序草案，合成阶段的硬规则优先级更高，可能改写/合并/增删"。
改造后可以更硬气一点：**拍数区间是强制的，只有清单内容会被改写。**

---

## 7. P1-C：`beat_outline` 结构化（可选增强）

P0-B 的所有校验都是中文正则启发式。把 schema 从 `array of string` 升级成
`array of {op, text}` 之后，规则 2/5/9 全部从"猜中文"变成"读字段"，精度大幅提升。

```
- "beat_outline": (array of objects) ... Each entry is
  {"op": <one of the 12 operations>, "text": <中文一行，≤16 字，动词开头>}
```
`op` 枚举直接复用合成侧 ladder 的那 12 个值（`prompt_pipeline/__init__.py:5406`）：
`clearing / repair / rough-in / flooring / framing / drywall / priming / painting /
wiring / lighting / furnishing / threshold / reward`。

收益：
1. 规则 2/5 变成 `outline[-1]['op'] == 'reward'`、`outline[cross+1]['op'] == 'clearing'`，零误判
2. §9.1 那条被推迟的工序单调性校验变得可做
3. 传给 compose 的软计划（`_outline_plan_block`，:5355-5371）带上 `op`，
   ladder LLM 能逐条对照硬规则，而不是靠中文语义猜

代价：
- 改 schema 会影响 few-shot 样例（:8525-8560 那两条要一起改成新形态）
- 前端三处读 `beat_outline` 的地方要适配对象形态（`ideaBeatOutline` :221、弹窗渲染、
  `composeIdeationCard` :493 的透传）
- `_normalize_beat_outlines` 要同时兼容旧字符串形态（存量任务的 dimensions 里存着旧形态）

**建议：P0 跑一轮看效果，确认 P0-B 的误判率可接受之后再决定要不要做 C。**
如果 P0-B 的中文正则误判率 < 5%，C 的边际收益不大。

---

## 8. P2-E：上限 15 放开（可选，独立于以上）

15 只在四处，后端无上界：

| 位置 | 内容 |
|---|---|
| `index.html:329` | `max="15"` ← 真正的硬上限 |
| `index.html:331` | datalist 刻度 5–15 |
| `js/prompt_pipeline.js:473` | `Math.min(15, Math.max(5, n))` |
| `js/config.js:681` | 随机预设 `setRandomVal('slider-beats', 5, 15)` |

后端 `beats_count = max(1, int(...))`（:5116）无上界，server.py 也不校验。

**但只改这四处会退化，有两个软天花板必须一起处理：**

1. **批量生成的 token 预算**。所有剩余拍在一次调用里出（:6778），用的是
   `_chat` 默认 `max_tokens=16384`。那个默认值旁边的注释（:795）写着
   "16 IMAGE + 15 VIDEO ≈ 31 条提示词，需要很大的输出预算"——**16384 就是照着 15 拍配的**。
   超了会撞 `TruncatedResponse`（:1001），虽有单拍回退兜底，但整批白烧一次。
   修法：给 :6778 显式传随拍数缩放的 `max_tokens`，如 `min(32768, 1100 * len(beats_to_generate))`。

2. **Phase 1 ladder 是单次 JSON**。:5463 的 `timeout=150`，注释记录了 12 拍在 90s 下
   已经连续假阳性超时才加宽到 150s。拍数继续涨要按拍数放宽。

另注：`"mode": "Threshold" if beats_count >= 12 else "Standard"`（:5221）
是 brief 解析全部失败时的兜底字典，不是主路径，影响有限，但放开上限后这个阈值的含义会变淡，
可以顺手改成不依赖 `beats_count`。

---

## 8.5 P0-C：dual_payoff 的两幕结构成本（2026-07-28 追加，已实施）

> 起因：用户提问「内外双重完工的节拍数是不是太少了，一个节拍包含多重变化，
> 也没看到有装太阳能板的」。三条观察是同一个根因的三个表征。

### 根因：门禁只给内部幕计数，外部幕不计数；长度下界照单线骨架配

`PACING_SKELETONS['dual_payoff']['summary']` 逐项点名 **17 个必须发生的状态变化**
（外部 6 + 过门 1 + 内部 10），三条 dual 兜底清单也一致落在 13~15 条。而旧门禁：

| 项 | 旧值 | 后果 |
|---|---|---|
| `len(outline)` | ≥ 7 | 17 个状态压进 6 个施工拍 ≈ 每拍 2.8 个变化 |
| 过门位置 | `2 ≤ idx ≤ len-4` | 7 条时外部只剩 2~3 条、内部只剩 1~2 条，两幕都摊不开 |
| 外部幕内容 | 只查过门前**最后一条**的措辞 | 「外部设备/平台」是骨架里唯一没人计数的必需项，拍数一紧第一个被砍 |
| `compute_beats_floor` 的 `structural` | 4（单线的账：室外 x2 + 过门 + 清理） | 11 条的双完工卡算出 floor=7，ladder 可以合法地把一幕压没 |

两道门禁在最小长度下**互相矛盾**：7 条时内部只剩 1~2 条要独自补齐「≥4 个层族」，
即一拍塞 防水+龙骨+封板——正是合成侧 `_INCOMPATIBLE_PACKAGE_FAMILIES` 明文否决的
组合。于是 ladder 结构校验三次重排 → 掉进兜底 ladder（:5611，用 `min_total_beats`）
→ 拍数被进一步压缩。**这个失败路径自我强化。**

画面不稳的机制是确定的：每拍的 IMAGE 从上一帧续写（Step 4 "continue directly from
the IMAGE you just wrote"），一拍同时改多处时模型会重排整幅而不是叠加 delta；VIDEO
侧一段固定时长要走完 3 个工序，尾帧与下一拍 IMAGE 声明的起始状态对不上。

### 落地

| 改动 | 位置 |
|---|---|
| `_DUAL_MIN_OUTLINE_ENTRIES=11` / `_DUAL_MIN_EXTERIOR_ENTRIES=4` / `_DUAL_MIN_POST_CROSSING_ENTRIES=6` / `_DUAL_MIN_EXTERIOR_FAMILIES=3` / `_DUAL_STRUCTURAL_FLOOR=9` | `prompt_pipeline/__init__.py`，紧挨 `_DUAL_THRESHOLD_CUE` |
| 外部族词表 `_DUAL_EXTERIOR_FAMILIES` + `_DUAL_EXTERIOR_UTILITY`（与内部 `layer_families` 对称） | 同上 |
| 过门位置改成两端各自计数；新增外部族计数 + 外部设备/平台必需 | `pacing_skeleton_outline_violations` |
| dual 走 `_DUAL_STRUCTURAL_FLOOR`，其余骨架行为不变 | `compute_beats_floor` |
| 激发 prompt 的 dual 编号规则 5 条 → 6 条，外部幕写进枚举 | `pacing_block` |
| 合成 prompt 补一条**正向**约束（旧的 "Do not move all exterior utility work after the cut" 在清单没有外部设备时恒真） | `_pacing_plan_block` |

**门禁与 prompt 必须同改**：只收紧门禁而不告诉模型新规则 = 整批返工 150s。

### 防误判

`_DUAL_EXTERIOR_FAMILIES` 的门扇一族不能写裸 `门`（`点亮门廊灯`、`完成入口门面`
都会算进来，这一族就废了），改成固定词 + 「动词 + …门」。词表按真实样例放宽过一轮：
`安装双开谷仓门`、`铺设门前碎石车道` 起初被漏判，已补 `车道|步道|台基|散水` 与动词式门规则。

回归锁：`tests/test_outline_skeleton_gate.py::TestOutlineSkeletonGateAcceptsGoodCards::
test_shipped_dual_fallback_outlines_pass_the_dual_gate`（三条 dual 兜底清单必须全过）
与 `test_minimum_legal_dual_outline_passes_both_gates`（恰好卡在下界上的卡必须放行，
否则下界等于事实上禁用了这个骨架）。

### 未做

内部层族仍是 ≥4，没跟着抬到 5。过门后已强制 ≥6 条，更多条目摊同样的族数**正是想要的
结果**（每拍变化更少），再抬只增加误判风险。

---

## 9. 明确不做的事

### 9.1 工序顺序单调性校验 —— 本期不做

原本想让 outline 的工序序列必须是 `parse_space_workflows()[space_type]['phases']` 的子序列
（拦掉"刷漆在铺地板前"）。**不做，两个原因：**

1. **激发阶段拿不到 `space_type`**（§4.1），只能猜，猜错就误伤
2. **`parse_space_workflows()` 依赖外部技能包**。它读 `load_reference_file('space-workflows.md')`
   （:1877），而技能目录由 `server_config.json` 的 `skillDir` 指向仓库之外
   （见 `load_reference_file` 注释 :1772-1774）。文件缺失时函数返回**只有一条兜底记录**的字典
   （:1880-1882），此时校验会对所有非 `abandoned property` 的载体全面失效或误判。

**如果将来要做，前提是 P1-C 落地（有了 `op` 字段）+ 卡片申报 `space_type`
+ 校验在 `workflows` 只剩兜底记录时自动降级为 no-op。**

### 9.2 不动 `pacing_skeleton_outline_violations` 的现有逻辑

它的正则是踩着 server.log 的假阳性调出来的（见 :8042-8047 注释），
本次只在它**之外**加通用门禁，不改它一个字符。

### 9.3 不动 fixed 模式

`fixed` 语义是"严格按所选拍数"，`beats_floor` 对它无意义
（`_beat_count_is_valid` 的 fixed 分支要求 `candidate_total == max_total`，:92-93）。
新增字段在 fixed 下自然不生效，无需特判。

---

## 10. 测试清单

新建 `tests/test_outline_skeleton_gate.py`（unittest 风格，对齐 `tests/test_threshold_variants.py`）：

**`outline_skeleton_violations`**
- 合格的 linear_milestone 清单 → 空列表
- 合格的 dual_payoff 清单 → 空列表（**且新旧两个门禁都要过**，防止新门禁和老门禁互相打架）
- 末拍不是 reward → 命中规则 2
- 两处过门 → 命中规则 3；**零处过门 → 不报错**（Standard 载体回归用例）
- 过门在第 1 条 → 命中规则 4
- 过门后接刷漆 → 命中规则 5
- 含"开始施工"/"继续完善" → 命中规则 6（schema :8391 明文禁止的那两个）
- 重复条目 → 命中规则 7
- **把 schema few-shot 里的两条样例（:8531 起）直接拿来跑，必须全过**——
  这是防误判最有效的一条用例

**`compute_beats_floor`**
- 13 条重型 Threshold 清单 → 9
- 6 条轻量清单 → 4（再经 §5.1 夹逼抬到 5）
- 空/畸形 outline → 回落 `_MIN_ADAPTIVE_CONSTRUCTION_BEATS`

**`_beat_count_is_valid`（扩展 `floor` 形参后）**
- `floor=9, max=13, candidate=8, adaptive` → False（**这条是 §5.1 那个易漏点的回归锁**）
- `floor=9, max=13, candidate=10, adaptive` → True
- `floor=None` → 行为与改造前完全一致（向后兼容锁）
- fixed 模式下 `floor` 不影响判定

**`_normalize_beat_outlines`（派生 `recommended_beats` 后）**
- 模型报 12、清单 8 条 → `recommended_beats` 被改写成 7
- 清单为空 → 不改写（保留原值，避免把没 outline 的卡片写成 0）

**回归**
- `tests/test_compose_checkpoint_resume.py`：指纹换代导致的用例更新
- `tests/test_ideation_trend_sources.py`：`run_ideate` 验收循环改动的连带影响
- 全量 `python -m pytest tests/ -q`

---

## 11. 验收标准

改造完成后，同一张灵感卡片：

1. 「一键合成」与「载入 → 手动生成」产出的拍数**落在同一区间**
2. 卡片显示的 `⏱ N 拍（不少于 M）` 中的 M **在成片里被真正兑现**
   （ladder 长度 ≥ M+1，或明确落进兜底路径并留痕）
3. `recommended_beats` 与 `beat_outline` 长度**永远一致**（派生而非申报）
4. linear_milestone 的卡片和 dual_payoff 一样**经过结构验收**
5. 滑块调到最低不再能把重型单压成 5 拍——它只能在卡片给的 `[floor, max]` 里压 max

最直接的观测指标：**打一批真实单，统计「卡片声称拍数」与「ladder 实际拍数」的差值分布。**
改造前这个差值无上界（13 → 6 完全合法），改造后应收敛到 `≤ 30%`（由
`_OUTLINE_SHRINK_TOLERANCE` 定义）。建议在 `run_ideate` 和 ladder 接受处各加一行 DEBUG 日志
把这两个数打出来，方便灰度期观察。
