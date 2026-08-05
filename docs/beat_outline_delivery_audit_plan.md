# 节拍简介「交付审查」方案

> 状态：**P1 + P2 已落地**（2026-08-05，见文末 §8 落地记录）；P3 待灰度数据。
> 日期：2026-08-05
> 前置：同日已落地的「节拍简介升级为硬规则」（`build_outline_plan_block` 改写、
> `outline_binding_violations` 三道新闸门、`bind_outline_to_ladder` 把工序钉上梯子、
> `check_outline_delivery_realized` 收口）。本方案不改那套**约束**，只补它的**审查面**。

---

## 1. 现在查到哪一步

节拍简介从卡片走到成片要过四道关，前三道已经有闸门：

| 阶段 | 闸门 | 判什么 | 强制性 |
|---|---|---|---|
| 激发 | `outline_skeleton_violations` / `outline_weight_violations` / `pacing_skeleton_outline_violations` | 清单自身的形状（长度、末拍 reward、过门唯一性、拍重均衡） | 打回重烧 |
| 规划 | `outline_contract_violations` + `outline_binding_violations`（[:12072](../prompt_pipeline/__init__.py#L12072)） | 覆盖率 / 合并宽度 / 认领序 / 工序类型 / 英文复述 | 阻塞（用满 4 轮重排） |
| 合成 | `check_outline_delivery_realized`（[:6562](../prompt_pipeline/__init__.py#L6562)） | 认领的工序有没有写进 IMAGE **正文** | 定向回炉一轮 |
| **渲帧** | **无** | — | — |

留痕链路：`config['_outline_contract']` → `render_outline_contract_md`（[:11830](../prompt_pipeline/__init__.py#L11830)）
→ `result['outline_contract']` + 审核面板 markdown（[server.py:177](../server.py#L177) 一带）。

---

## 2. 三个缺口

### 2.1 缺口 A（最大）：闸门全在**文字**上，没有一道看**画面**

四道关里前三道判的都是提示词文本。帧渲染之后确实有一整套 VLM 审查
（`check_full_sequence_consistency`，[:10324](../prompt_pipeline/__init__.py#L10324)，三层：逐拍
`check_beat_consistency` / 跨帧 `check_global_sequence_consistency` / 二次复核
`_verify_review_violation`），但它的 rubric 是施工顺序、SCUP、地标、载体身份那一套，
**完全不知道卡片工序的存在**。

于是「IMAGE 正文写了躺椅、渲出来的图里没有躺椅」这一类，文本层判通过、画面层压根没在查
——正是 `docs/beat_content_realization_check_plan.md` 那次喀斯特事故的**下一层**形态：
上次修的是「VIDEO 说装了、IMAGE 没写」，这次剩下的是「IMAGE 写了、图里没有」。

而且这不是"忘了加规则"，是**结构性够不着**：

```
_sequence_consistency_review(config, title, prompt_block, project_dir, ...)   # 无 beat_ladder
  └── check_full_sequence_consistency(config, prompt_block, frame_image_paths, ...)  # 无 beat_ladder
        └── check_beat_consistency(config, prompt_block, beat_index, total_beats, 两张帧图)
```

`beat['outline_items']`（`bind_outline_to_ladder` 钉上去的工序原文+英文复述）到不了这一层。
`beat_ladder` 也没有落盘：manifest 只写了 `spatial_beats` 这个投影
（[pipeline_orchestrator.py:1066](../pipeline_orchestrator.py#L1066)），字段白名单里没有 `outline_items`。

### 2.2 缺口 B：没有「逐条工序的总账」

数据散在三处，谁也回答不了「卡片上第 3 条工序，最后落在第几帧、成没成」：

- `_outline_contract.diff` —— 只到「哪一拍认领了它」（规划期）；
- `_beat_audit` —— 按**拍**组织，条目是回炉记录，不按工序索引；
- manifest 的 `quality_gate` —— 按**帧**组织，理由是自由文本。

用户是照着那份清单挑的选题，最该看见的一张表恰恰拼不出来。

### 2.3 缺口 C：硬规则上线后没有回归观测

今天无从判断「节拍简介变硬规则」到底把交付率抬高了多少，也无从发现某个骨架/某类工序
系统性掉链子。缺一个可累积的口径。

---

## 3. 方案

### 3.1 一份账：`outline_delivery_ledger`

每条卡片工序一行，贯穿三个阶段：

```python
{
  'index': 3,                        # 卡片上的第几条（1-based）
  'text': '铺设隐蔽水管与地暖',        # 中文原文（用户在弹窗里看到的那一行）
  'delivery': 'run the hidden pipe circuits and underfloor heating loops',
  'claimed_beats': [4],              # 哪几拍认领（来自 outline_milestone_contract）
  'frame_seqs': [5],                 # 对应的到达帧（beat + 1）
  'plan_verdict': 'claimed',         # claimed | merged | split | dropped
  'prompt_verdict': 'delivered',     # delivered | reworked | missing | skipped
  'frame_verdict': 'unreviewed',     # visible | missing | unreviewed | not_applicable
  'note': '',
}
```

三个 verdict 各有明确来源，**不新增判定逻辑，只是把已有结论按工序重新索引**：

- `plan_verdict` ← `outline_milestone_contract(...)['diff']` / `['uncovered']`
- `prompt_verdict` ← 合成期 `check_outline_delivery_realized` 的命中与回炉结果
  （目前只进 `style_errs`/`_beat_audit`，需要额外按 outline index 记一份）
- `frame_verdict` ← 3.3 新增的帧级判定

存放位置沿用现有约定：`config['_outline_delivery_ledger']`，run 结束由 server.py 汇入
`result`，与 `_beat_audit` / `_frame_state_contract` / `_outline_contract` 同一套写法。

### 3.2 让工序原文抵达帧审查层

manifest 是天然载体，而且**已有先例**：`spatial_beats` 就是在同一个 `with manifest_lock`
块里从 `state['beat_ladder']` 投影出去的（[pipeline_orchestrator.py:1059-1072](../pipeline_orchestrator.py#L1059)）。
在同一处加一行：

```python
_manifest['outline_items'] = {
    str(beat.get('index')): pp.beat_outline_items(beat)
    for beat in state.get('beat_ladder', []) if isinstance(beat, dict)
}
```

然后 `_sequence_consistency_review` 从 `project_dir` 读 manifest 拿到它，
沿 `check_full_sequence_consistency` → `check_beat_consistency` 透传下去。

选 manifest 而不是新开一个文件：审查是**用户手动触发**的独立入口
（`run_sequence_consistency_review`，[:599](../pipeline_orchestrator.py#L599)），跟合成那次运行不在同一个进程生命周期里，
必须落盘；而 manifest 本来就是这条边界上的存量载体，两个渲染后端都刻意保留未知键。

### 3.3 逐拍审查里加一条「卡片工序交付」

`check_beat_consistency` 已经有现成的结构可挂——`FOCUS RECORD FOR THIS BEAT
(authoritative scope)`（[:10084](../prompt_pipeline/__init__.py#L10084)），当前把该拍的 VIDEO 正文与到达 IMAGE 正文塞进去当权威范围。
卡片工序作为**第二段**追加：

```
CARD WORK ITEM(S) THIS BEAT MUST DELIVER (the user chose this creative by reading these):
  · 铺设隐蔽水管与地暖 — run the hidden pipe circuits and underfloor heating loops
Judge, in the ARRIVAL frame only: is the finished result of each item plainly visible?
If an item's result cannot be seen at all, report: CARD WORK NOT DELIVERED: <item>
```

**只加在逐拍局部层，不加跨帧层。** 跨帧层刻意只保留 6 条真正需要跨帧比较的规则
（场景/材质/地标/载体身份），2026-07-23 那次三层改版的结论就是「规则和图片一起被稀释
→ 要么找不到问题、要么硬塞问题」；工序交付是**单帧可判**的，塞进跨帧层只会稀释它。

**灰度先行：`_OUTLINE_FRAME_GATE_ENFORCING = False`。**
沿用 `_OUTLINE_GATE_ENFORCING`（[:11720](../prompt_pipeline/__init__.py#L11720)）/ `_RHYTHM_ARC_ENFORCING`（[:2351](../prompt_pipeline/__init__.py#L2351)）的开关惯例。
理由：VLM 判「这条施工工序算不算完成」的尺度是未知量，比「这个地标在不在画面里」模糊得多
（一条"铺设隐蔽水管"在封板后**本来就该看不见**——见下面的风险一节）。灰度期只写 ledger
和日志，**不进 `failures`、不影响 `quality_gate`**；摸清误判率再决定是否并入。

### 3.4 面板：从「映射 diff」升级成「逐条工序体检表」

`render_outline_contract_md` 现在渲染的是合并/拆分/新增/未交付 + 未满足项。
在它上方补一张按 ledger 生成的总表——用户第一眼要看的是这张：

```markdown
### 卡片工序交付体检（14 条）

| # | 卡片工序 | 落点 | 提示词 | 画面 |
|---|---|---|---|---|
| 1 | 清空洞内碎冰与积雪 | 第 1 拍 → 帧 2 | ✅ | ✅ |
| 2 | 修补岩壁裂缝并除锈 | 第 2 拍 → 帧 3 | ♻️ 回炉后通过 | ✅ |
| 3 | 铺设隐蔽水管与地暖 | 第 4 拍 → 帧 5 | ✅ | ➖ 隐蔽工序，封板后不可见 |
| 7 | 切割舱门装配入口梯 | ⚠️ 无人认领 | — | — |
```

### 3.5 灰度观测点

一行可累积的日志，口径与现有 `[RHYTHM]` / `[OUTLINE]` 一致：

```
[OUTLINE-AUDIT] skeleton=nested_space_payoff entries=14 plan=14/14 prompt=13/14 frame=11/14
```

`plan/prompt/frame` 三个比值就是缺口 C 要的回归口径：硬规则上线前 `plan` 分母侧的丢失
是主要项，上线后应当收敛到 `frame` 这一侧。

---

## 4. 风险与刻意不做的事

**隐蔽工序天然不可见。** 「铺设隐蔽水管与地暖」「填充保温棉」这类，按施工顺序**必然**被
后续封板盖住；如果它被认领的那一拍的到达帧恰好已经封板，判「看不见」是对的观察、错的结论。
处理：ledger 的 `frame_verdict` 增加 `not_applicable`，由该拍的 `operation` 落在
`rough-in` / `framing`（隐蔽层族）且**下一拍是 drywall/封板**时自动置位；灰度期先只观察
这类的实际占比，不急着做规则。

**不做自动重渲。** 2026-07-23 已经确立「检出问题立即停手，不先斩后奏改构图」
（见 `_sequence_consistency_review` 的行为变更说明）。工序未交付一律只留痕，
修复走用户在帧网格点「修复此帧问题」那条既有通路。

**不改 `check_outline_delivery_realized` 的宽松度。** 它刻意只抓「一个实义词都没命中」
的 100% 缺失，这是 `_missing_trace_items` 沿用下来的口径；收紧它会在合成阶段制造假阳性，
而真正该收紧的是画面层——那正是本方案要补的。

---

## 5. 分期

| 期 | 内容 | 触碰生成路径 | 风险 |
|---|---|---|---|
| **P1** | 3.1 ledger + 3.4 面板表 + 3.5 日志 | 否（纯留痕重排索引） | 无 |
| **P2** | 3.2 manifest 透传 + 3.3 逐拍规则（灰度，不 flag） | 只加审查入参 | 低 |
| **P3** | 依灰度数据决定 `frame_verdict` 是否并入 `failures` | 是 | 中，需数据支撑 |

P1 单独就能解决缺口 B 和 C —— 用户立刻看得见「我挑的 14 条工序落实了几条」，
而这一期完全不碰生成路径。建议先只做 P1。

---

## 6. 测试

`tests/test_anchor_lifecycle_and_contracts.py`（工序契约的既有归属地）新增：

1. `test_ledger_indexes_every_card_entry_once` —— 14 条工序 → 14 行，合并/拆分不重复不丢行。
2. `test_dropped_entry_shows_as_unclaimed_with_no_downstream_verdict` —— `plan_verdict='dropped'`
   的行，`prompt/frame` 必须是 `—` 而不是 `missing`（没落点就无从谈交付，报成 missing 是误导）。
3. `test_reworked_prompt_is_distinguishable_from_first_pass` —— 回炉后通过 ≠ 一次过。
4. `test_hidden_layer_entry_is_marked_not_applicable` —— rough-in 拍且下一拍封板 → `not_applicable`。
5. `test_ledger_survives_a_ladder_without_outline_items` —— 老断点/手填维度直出：返回空账，不炸。
6. `test_audit_table_renders_all_four_verdict_symbols` —— 面板表四种状态都渲染得出。

P2 追加：

7. `test_manifest_carries_outline_items_for_the_review_stage` —— 投影写入与读回往返。
8. `test_beat_review_prompt_carries_the_card_work_items` —— `check_beat_consistency` 的 user
   turn 里出现工序原文与英文复述（mock `_multimodal_chat`，只断言入参）。
9. `test_frame_verdict_is_observed_but_never_flags_while_gray` —— `_OUTLINE_FRAME_GATE_ENFORCING=False`
   时，判定进 ledger 但 `failures` / `quality_gate` 一个字都不变。

再跑一次全量 `python -m pytest tests/ -q`。

---

## 7. 端到端复验

1. 重启 `:8085`（`python server.py`；不要用 run.bat/pythonw，会在工具会话里被静默杀掉）。
2. 走一遍真实 `POST /api/compose`（不要绕过 API 直调 `compose_anchor_and_packet`，否则不写任务记录），
   选一张工序条数多、且含明显隐蔽工序（水电/保温）的卡。
3. 读 `tasks/<task_id>.json` 的 `outline_delivery_ledger`：逐条核对 `claimed_beats`
   与「🔨 节拍简介」弹窗里那份清单对得上，`prompt_verdict` 与 `beat_audit` 里的回炉记录自洽。
4. P2 起：渲完帧后在帧网格手动触发一致性审查，确认 `frame_verdict` 落位，
   且 manifest 的 `quality_gate` 与灰度前**逐字节相同**（灰度期不许影响既有判定）。

---

## 8. 落地记录（2026-08-05）

P1 与 P2 一起落地，P3 按原议留给灰度数据。落点：

| 环节 | 位置 |
|---|---|
| 按工序记合成期结论 | `record_outline_delivery` / `outline_missing_indices`（`prompt_pipeline/__init__.py`），两条合成路径各在 `record_beat_audit` 旁调一次 |
| 总账 | `build_outline_delivery_ledger` + `stash_outline_delivery_ledger`，在 `composers/base.py` 合成收尾处生成 |
| 面板表 | `render_outline_delivery_md`，`server.py` 里排在 `render_outline_contract_md` 之上 |
| 观测日志 | `outline_delivery_log_line` → `[OUTLINE-AUDIT] ... plan=/prompt=/frame= na=` |
| 落盘 | `persist_outline_delivery_ledger`（`pipeline_orchestrator.py`），compose 收尾与自治管线各一次 |
| 帧审查透传 | `outline_items_by_beat` → `check_full_sequence_consistency(outline_items=)` → `check_beat_consistency(outline_items=, outline_out=)` |
| 灰度开关 | `_OUTLINE_FRAME_GATE_ENFORCING = False`；判定经 `split_outline_frame_verdicts` / `outline_frame_verdicts` 只回写总账 |

与方案的四处出入，都是实现时才看清的约束：

1. **manifest 上放的是整份总账，不是 `outline_items` 投影。** §3.2 原打算投影
   `{beat: items}`，但那样帧审查回来的判定就没地方回写（总账在 compose 那次运行的
   `config` 里，进程已经结束）。现在 manifest 存 `outline_delivery_ledger`，
   `outline_items_by_beat` 从它派生 —— 一份数据源，审查判定原地回写。
2. **总账在 `compose_remaining_beats` 收尾处生成，不在 server.py。** `beat_ladder`
   只在合成期在场；server.py 只负责汇入 `result` 与落盘。
3. **落盘时机与建目录。** 手动路径（`/api/compose` → 帧网格）在合成结束时项目目录
   往往还没建（封面/首帧才建），`persist_outline_delivery_ledger` 因此按需
   `makedirs`；自治管线则在既有的 manifest 写入块附近顺手落一次。
4. **「逐字节相同」只保证代码侧。** 灰度期我们的代码绝不把工序判定写进 `failures` /
   `quality_gate`（有回归测试钉住），但逐拍 user turn 确实多了一段审查要求，模型对
   *其他* 规则的判断仍可能有微小漂移 —— 这是"要观察就得先问"的固有代价，
   `_OUTLINE_FRAME_GATE_ENFORCING` 只能控制判定的去向，控制不了注意力。

已知留白：断点续传恢复的拍这次不跑合成，也就不会记 `prompt_verdict`，那几行在面板上
显示 `—`（未知）而不是 `✅` —— 与「没记录 ≠ 没交付」的口径一致，但确实不如全量跑那么满。

测试：`tests/test_anchor_lifecycle_and_contracts.py` 新增
`TestOutlineDeliveryLedger` / `TestOutlineReachesTheFrameReview`（方案 §6 的 1–9 条全覆盖，
另补了三条：认不出归属的上报宁可丢弃、开关翻 True 后照常上报、增量审查合并取坏消息），
`tests/test_sequence_consistency_review.py` 新增 `TestOutlineFrameAuditIsGrayOnly`
（manifest 往返 + quality_gate 不受影响 + 目录按需创建）。全量 `pytest tests/` 2144 passed；
另有 2 项 `test_omni_composer` 失败与本方案无关 —— 本机 `server_config.json` 的
`videoDuration=8`（5 镜梯）与该测试预期的出厂默认 10 秒（6 镜梯）对不上。
