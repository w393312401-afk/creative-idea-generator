# 过门协议修订方案（TBCP v2 草案）

> 状态：方案评审稿，未动代码。
> 日期：2026-07-14
> 背景：帧序列过门后门框长期残留、室内画面占比过小、室内丢失载体身份（校车案例：入内后变成普通木板房间）。

---

## 0. 目标与非目标

**目标**

1. 过门完成后，门框/门洞边缘必须彻底出画，镜头完全进入室内空间。
2. 支持三种过门策略，按载体几何自动选择：
   - **coaxial**（直推擦除）：开门即见室内主轴 → 现有 TBCP 直推 + Door-Frame Wipe。
   - **pan**（推进后摇镜）：门在侧面、室内主轴与门轴不共线（车类/船舱）→ 过门后原地摇镜转入室内主轴。
   - **hard_cut**（硬切）：过门前完全看不到室内信息（密闭舱/地下）→ 声明式硬切一次。
3. 一切变体以场景一致性为前提；单主题过门次数预算 1 次（二期扩展到 2 次，见 §8）。
4. 除过门桥接拍外，全部镜头保持固定（现状不变）。

**非目标（本次不做）**

- 嵌套第二次过门（车厢→内舱）与"出门"结尾镜头 —— 列为二期（§8）。
- 视频侧 i2v 模型能力问题（桥接视频本身的插值质量）不在本方案范围。

---

## 1. 决策层：`threshold_variant` 在 brief 阶段一次性声明

**现状**：`parsed_brief['mode'] == 'Threshold'` 只声明"有过门"，过门方式无声明，下游各环节各自即兴。

**修订**：brief 生成 LLM 调用（`prompt_pipeline/__init__.py` 中 mode/beat ladder 同层）新增两个字段：

```json
{
  "threshold_variant": "coaxial | pan_left | pan_right | hard_cut",
  "threshold_elevated": true/false
}
```

- `threshold_elevated` 与 pan/coaxial 可组合（校车 = elevated + pan_right：上台阶过门后向后厢摇）。
- `hard_cut` 与 elevated 互斥无意义，忽略组合。

**决策规则（写进 brief prompt，LLM 按载体几何判断）**：

| 条件 | 变体 |
|---|---|
| 门开后沿门轴直视即是室内纵深主轴 | coaxial |
| 门在侧面/端头，室内主轴与门轴垂直或错开（巴士、火车车厢、船舱、飞机） | pan_left / pan_right（方向 = 室内纵深所在方向） |
| 过门前无法看到任何室内信息（密闭罐体、埋入式空间、门后即黑） | hard_cut |
| 门槛高于地面镜头（瞭望塔、筒仓口、缆车） | 叠加 elevated |

**消费方**：beat ladder 结构校验、`_beat_contract`、validators/fixers、`frame_generator`、质检审查、视频配对、前端展示 —— 全部只认这一个声明，禁止任何环节自行推断过门方式。

---

## 2. 镜头族模型扩展

**现状**：`beat_space_family()` / `image_space_family()` 三态 `exterior → sill → interior`，由 `bridge_stage 1/2` 推导。

**修订**：

### 2.1 coaxial（含 elevated）— 结构不变，契约加严

保持 Bridge-1/Bridge-2 两段桥。变化只在提示词契约（§3）和门框清除兜底（§4）。

### 2.2 pan 变体 — 增加第三段桥（`bridge_stage 3`，新族 `vestibule`）

摇镜是第四个系统切换（朝向），塞进 Bridge-2 会突破 TBCP"每 clip ≤1.5 个系统切换"的容忍度。拆成三段：

```
IMAGE T      exterior · 门槛帧（PBISP peek 的锚点必须放在【摇镜落点方向】可见的位置）
VIDEO B-1    推进到 sill（elevated 时为爬升推进）           —— 现状不变
IMAGE T+1    sill 交接帧                                    —— 现状不变
VIDEO B-2    过 sill + 曝光/白平衡完成，落点=门厅位置，
             朝向尚未转（面对入口正对的内壁/端头），门框边缘已出画
IMAGE T+2    vestibule 帧（新族）：已在室内、朝向未转、无门框
VIDEO B-3    原地摇镜（pan_left/right）转入室内纵深主轴；
             摇镜过程中新入画的内容必须全部来自已声明的 interior anchors
IMAGE T+3    interior 定格帧：室内 camera DNA 锁定，之后全部固定
```

- `beat_space_family` 扩展为 `exterior / sill / vestibule / interior` 四态；coaxial 单无 vestibule（三态兼容，旧 manifest 不受影响）。
- vestibule 帧的 camera DNA 单独定义（同高度同镜头、朝向=入口轴、明确声明"door frame fully behind the camera"）。
- **Turn Bridge Camera DNA**（B-3 专用块，仿 Elevated Access Variant 的先例写进 TBCP）：

  ```
  same lens feel, same camera height, camera position fixed at the vestibule point;
  a single smooth horizontal pan to the {left|right}, no dolly, no tilt, no roll;
  the pan ends with the central vanishing axis locked on the interior's long axis.
  ```

- **摇镜锚点规则**：PBISP peek 阶段（IMAGE T）声明的 2 个 interior anchors 中，至少 1 个必须位于摇镜落点方向；B-3 视频提示词以"锚点从画面边缘进入并停稳在其注册 Grid 位"描述新入画内容 —— 没有已声明锚点覆盖的墙面禁止在摇镜中占据主体（避免模型对 90° 外的空间纯幻觉）。
- **Monotonic Scale Lock 修订**：coaxial 的"沿推进轴单调放大"对 pan 段不适用，B-3 段改为"锚点沿摇镜路径横向入画、尺寸恒定"。

### 2.3 hard_cut 变体 — 无桥，声明式切点

```
IMAGE T      exterior · 门槛帧（门可开可闭；不要求 peek —— 定义上就是看不到室内）
VIDEO T      [CUT] 槽位：不送 i2v，正文为切点声明占位（见 §6）
IMAGE T+1    interior 首帧（新族锚、新 i2i 链头，见 §4）
```

- 不生成 sill/vestibule 帧，`bridge_stage` 置空，切点在 VIDEO 槽 meta 上打 `[CUT]` 标（与 `[BRIDGE]` 同款机制）。
- **硬切下的一致性定义**（写进 TBCP 新章节）：室内没有任何已见锚点可继承，一致性锚定在 **Scene DNA 软约束清单** 上，室内首帧 prompt 必须逐条复述：
  1. 载体身份特征（§3.2 的 Carrier Identity Anchors）；
  2. 材质基因（外部已确立的材料、色调、破损/风化程度）;
  3. 光照方向与色温（与外部时刻一致的日光方向，透过载体自有开口进入）;
  4. 施工进度状态（切点时刻室内应处于的阶段 —— 未施工原始状态）。
- **预算**：hard_cut 每主题最多 1 次，且只允许用于过门（不得当作普通转场偷懒用）—— beat ladder 结构校验里加这条。

---

## 3. 提示词契约修订

### 3.1 门框彻底出画（P0，所有变体生效）

`_beat_contract` 的 interior `anchor_rule` 现在只说"不要复述外部锚点/地平线/天空"，**没有明说门框出画**。增加硬性条款：

```
The door frame, door leaf, threshold edges, and the entry opening itself are now
FULLY BEHIND the camera and must NOT appear anywhere in the frame; interior
walls/ceiling/floor fill the frame edge to edge.
```

同步修订跨门光绳（Cross-Threshold Tether）的室内侧写法：禁止"画面中的门口"，改为**不入画的方向性光**：

```
daylight from the entry behind the camera lays a soft bright wedge across the floor
toward the rear wall — the opening itself stays out of frame.
```

### 3.2 载体身份锚点（Carrier Identity Anchors，P1）

校车案例暴露的核心问题：门框是目前唯一把室内画面和载体绑定的元素，去掉门框后室内退化为通用房间。

- brief 的 `interior_primary_landmarks` 生成要求追加：**至少 1 个（建议 2 个）锚点必须是载体固有的、不可移除的身份特征**——校车：侧窗带、肋条弧顶、轮拱、驾驶舱隔断；船：舷窗列、肋骨框架；飞机：舷窗带、行李架弧线。
- interior 契约在每帧复述这些身份锚点（Grid + 尺度），序列审查增加对应检查项（§5）。
- 该条同时天然解决 hard_cut 的一致性锚定（§2.3）。

### 3.3 主光源交接（P1）

门框出画后，"门口光"从画面元素降级为方向光，室内必须有明确的主光源声明，否则模型会自己发明窗户（新的一致性破坏源）：

- 优先级：载体自有开口（车窗带/舷窗，兼任身份锚点）→ 已在先前拍安装的 practical light → 背后入口方向光。
- brief 阶段与 `lighting_phase_ladder` 对齐：入内后的第一批拍若载体无自然开口且 practical lighting 拍还没到，允许"背后入口方向光 + 手提工作灯已亮"的过渡写法。

### 3.4 TBCP 文档修订

`references/threshold-bridge-consistency-protocol.md`（skill 目录）新增两个正式变体章节，写法对齐既有 Elevated Access Variant 的体例（触发条件 → 专用 Camera DNA block → 最大失败模式 → 审计表行）：

- **Pan Access Variant**（§2.2 的三段桥 + Turn Camera DNA + 摇镜锚点规则 + 修订版 Scale Lock）；
- **Hard Cut Variant**（§2.3 的切点声明 + Scene DNA 软约束清单 + 预算限制）；
- 审计表补 4 行：门框出画、vestibule 帧合规、摇镜锚点覆盖、hard_cut 一致性清单齐全。

### 3.5 validators / fixers 白名单（防 validator 打架）

`check_transition_shortcuts`、`fix_out_and_in`、镜头运动词封杀清单等现在会绞杀一切 pan/横移词汇。修订原则：

- 放行严格按声明限定：仅 `threshold_variant` 为 pan 且 `bridge_stage == 3` 的那一拍允许摇镜词汇；hard_cut 的 [CUT] 槽位跳过全部视频词汇校验。
- 其余拍的封杀不放松。每处放行必须以 `_beat_contract` 下发的变体字段为准，禁止用正则从正文反推变体（历史上 validator-vs-validator 冲突的根源）。

---

## 4. 生成机制（frame_generator / i2i 链）

### 4.1 门框清除兜底重试（P0，核心）

门框残留的机理是 i2i 编辑模型保守：参考帧（sill 帧）门框占满画面，文字说"前进"它只做保守裁切。提示词修订治标，必须加生成侧兜底：

- interior 首帧（coaxial 的 T+2 / pan 的 vestibule T+2）渲出后，VLM 单项判定："门框/门洞边缘是否仍在画面内？室内结构是否占满全幅？"
- 判定不过 → 用 `IMG2IMG_BRIDGE_CONTROL_PROMPT`（推进版控制指令，`server_common.py:221`）以刚渲出的帧为参考**再推一步**，等于把"过门"拆成两次连续推进；进入现有重试轮计数。
- 该检查只跑换族后的第一帧（成本最低、收益最大的位置）；后续 interior 帧靠序列审查兜底（§5）。

### 4.2 pan 变体的摇镜控制指令

新增 `IMG2IMG_TURN_CONTROL_PROMPT`（与推进版并列的第三种控制指令）：视点原地旋转、按视差横移已知锚点、新入画边缘按已声明锚点补全、禁止 dolly/tilt。仅 B-3 产出的 interior 定格帧使用。

### 4.3 hard_cut 的新链头

- 室内首帧**不拿上一帧当参考**：走 t2i（`_generate_text_image` 路径），prompt 注入 §2.3 的 Scene DNA 清单 + 载体身份锚点。
- 该帧成为后续 i2i 链的新链头；之后恢复正常 `IMG2IMG_CONTROL_PROMPT` 锁定构图链。
- 注意 i2i 契约（memory：/images/edits 专用），t2i 走 chat 路径不受影响。

---

## 5. 检测与豁免

**现状**：逐帧质检门已停用；现役为整套序列一致性审查（`_sequence_consistency_review`）+ 检查点现实同步（`_checkpoint_reality_sync`）+ 链尾回望（`_chain_drift_lookback`，检测型不拦截）。

### 5.1 新增检查项（进序列审查 checklist + 检查点现实同步）

1. **门框出画**：换族完成后的所有 interior 帧不得出现门框/门洞/门槛边缘（sill、vestibule 帧豁免）。
2. **室内占比**：interior 帧的室内结构（墙/顶/地）必须充满画面（措辞用主观判定，不给数字阈值以免文字入画）。
3. **载体身份**：interior 帧至少一个 Carrier Identity Anchor 可见且与注册一致。
4. **hard_cut 一致性**：切点后首帧对照 Scene DNA 清单逐条核对（材质/破损/光向/进度）。

### 5.2 豁免通道（防误杀）

- `[CUT]` 标签与 `[BRIDGE]` 同权：`family_anchor_seq` / `image_space_family` / `resolve_family_anchor` 把 [CUT] 视频槽当换族点，切点后以室内首帧为新族锚 —— 与既有 `config['_reanchors']` 重锚定机制同一思路，复用其带内通道。
- pairwise 连续性判定（含 elevated 变体已有的"别把 T+1 判成硬切"规则）：对声明了 hard_cut 的切点对**跳过**pairwise 判定；对 pan 的 B-3 前后帧，判定基准从"构图一致"改为"同一空间不同朝向"（提示词里明说这是声明过的摇镜）。
- 切点写入 manifest（`manifest['declared_cuts'] = [seq]`）留痕，供恢复轮/回望/前端消费。

---

## 6. 视频配对与合成

hard_cut 切点两侧的帧**不得组成首尾帧对送 i2v**（否则视频模型在两张无关构图间硬插值出扭曲变形）。

- **方案（推荐）**：VIDEO T 槽位保留（槽位编号连续性不破坏现有前后端 `prompt_slots` 契约），meta 打 `[CUT]`，正文为固定占位声明（"declared hard cut, no video clip"）。
- 消费侧：帧配对逻辑（后端权威 `result.prompt_slots` + 前端 `resolvePromptSlots`）遇 `[CUT]` meta 跳过该对的 i2v 提交；合成阶段该处直接 concat 硬拼（复用部分合并的 concat 基建）；缺片门禁（PartialMergeBlocked）把 [CUT] 槽登记为"预期缺失"不计入缺片。
- 段内空心检测、门禁档位对 [CUT] 槽自动跳过。

---

## 7. 监修与留痕

- **监修暂停点扩展**：现有监修在族锚帧（首帧/桥接交接帧）单独成段暂停。新增两个最高风险帧进暂停清单：pan 的 B-3 落点帧（interior 定格帧）、hard_cut 的室内首帧（新链头）。这两帧一旦歪掉污染整条后链，正是人工把关性价比最高的位置。
- **manifest 留痕**：`threshold_variant`、`threshold_elevated`、`declared_cuts`、门框清除兜底的触发与结果。

---

## 8. 过门次数预算与二期边界

- **一期（本方案）**：每主题 1 次过门，三变体任选其一；其余全部拍固定镜头（现状机制不变）。
- **二期（明确不在本次）**：
  - 第二次过门（嵌套空间：车厢→内舱）——需要把四态族模型泛化为多级 family 栈，每级完整走一遍锚点交接；
  - "出门"结尾镜头（成品外观展示）——按反向 bridge 处理，同样占用过门预算；需要放开现有 VIEWPOINT CONTINUITY RULE 的"过门后永远室内"约束。
- 一期在 beat ladder 结构校验中硬性限制：`bridge_stage` 组最多一组，`[CUT]` 最多一个，两者互斥。

---

## 9. 实施顺序

| 阶段 | 内容 | 触及文件 | 验证 |
|---|---|---|---|
| **P0** 门框清除 | §3.1 interior 契约硬条款 + §4.1 兜底重试 + §5.1 检查项 1/2 | `prompt_pipeline/__init__.py`（`_beat_contract`/审查 checklist）、`frame_generator.py`、TBCP md | 单测 + 校车创意重跑，验收=interior 帧无门框 |
| **P1** 身份与光源 | §3.2 载体身份锚点 + §3.3 主光源交接 + §5.1 检查项 3 | brief prompt、`_beat_contract`、TBCP md | 校车重跑，验收=车窗带/弧顶入画 |
| **P2** pan 变体 | §1 字段 + §2.2 三段桥/vestibule 族 + §3.4/3.5 + §4.2 | `beat_space_family`/`image_space_family`/`select_camera_dna`/validators、`server_common.py`（新控制指令）、TBCP md | 单测（族计算/白名单）+ 校车（elevated+pan）实测 |
| **P3** hard_cut 变体 | §2.3 + §4.3 t2i 新链头 + §5.2 豁免 + §6 配对跳过 | 同上 + `pipeline_orchestrator.py`、`js/media_renderer.js`/`js/prompt_pipeline.js`（配对）、合成门禁 | 单测（[CUT] 豁免/配对跳过/门禁预期缺失）+ 密闭载体实测 |
| **P4** 监修扩展 | §7 暂停点 | `pipeline_orchestrator.py` | 监修开启实测 |

每阶段独立可发布：P0/P1 对现有 coaxial 单变体立即生效，不依赖 P2/P3。

---

## 10. 风险与开放问题

1. **B-3 摇镜的 i2v 质量**是本方案最大的模型能力赌注：i2v 对 90° pan 的表现未知，需 P2 前先用现有素材做一次小样验证；若不可用，pan 变体退化为"vestibule 帧直接作为 interior 定格帧 + 声明式 [CUT] 转向"（复用 P3 基建）。
2. **t2i 新链头与外部帧的色调断差**（hard_cut）：Scene DNA 软约束能否稳住色温/胶片感，需实测；不稳则考虑以 IMAGE 1 做弱参考的 i2i（指令="同一世界的另一空间"）作为备选。
3. 序列审查 checklist 增项会拉长审查轮次耗时，注意与现有修复轮上限的配额平衡。

---

## 11. TBCP v3 — 合并过门视频 + 首现脏乱差（2026-07-20）

**触发**：用户实测反馈——①「进门镜头不再保留中间带门框的帧，直接完全进入到室内」；②「室内也是脏乱差的现象，而不是干净的现象」。根因定位：coaxial/pan 变体的过门本是两段（sill 停驻+door 可见 → cross 完全入内）各自独立成片，拼接处即"停在门口"的观感来源；室内首现的 anchor_rule 分支从未带衰败措辞（仅 hard_cut 变体有）。

**方案（不改 bridge_stage 基数与结构校验，只改视频侧生成/装配）**：

- **HOLD/SPAN 双拍语义不变，视频装配改单可见片段**：`bridge_stage=1`（HOLD）拍的 IMAGE 仍按原两跳 i2i 正常生成（保留门槛内部定格帧，作为 i2i 接续锚点，不丢弃这层空间落地保护），但其 VIDEO 槽确定性覆盖成丢弃占位声明（`BRIDGE_HOLD_VIDEO_PLACEHOLDER`），不生成不送 i2v。`bridge_stage=2`（SPAN，coaxial 里=interior 定格拍，pan 里=vestibule 拍）成为过门唯一可见片段，首帧锚点从"IMAGE i"（HOLD 产出的门槛帧）重定向到"IMAGE i-1"（HOLD 之前的室外锚点帧），正文要求把"接近门口→穿越→门框滑出画外→完全入内"一镜到底叙述完，不得在门口停顿。pan 的 Bridge-3 转向拍不受影响。
- **meta 标签扩展**：`_build_partial_prompt_block` 的视频侧 meta 由统一 `"BRIDGE"` 拆成 `"BRIDGE HOLD"`（bridge_stage=1）/`"BRIDGE SPAN"`（bridge_stage=2）/`"BRIDGE TURN"`（bridge_stage=3，不变）；IMAGE 侧 meta 不变（仍统一 `"BRIDGE"`，因为 IMAGE 序列本身没有任何改动）。所有既有 `'BRIDGE' in meta` 消费点（`image_space_family`/`family_anchor_seq`/链回望/frame_generator 的 is_bridge 判定）天然兼容，无需改动。
- **`video_generator.plan_video_slots` 新增 `skip_bridge_hold` 动作**：与既有 `skip_cut` 同款"预期缺失"语义，不生成、不算失败、合并门禁不计缺口。SPAN 槽位新增 `start_anchor_slot = slot - 1` 字段，贯穿 `_video_info`（写入 manifest）→ `merge_project_videos` 的锚点一致性核对（否则会把正确生成的合并片段误判成串片）→ `_merge_with_placeholders` 的强制合并占位帧解析。
- **首现脏乱差（issue 2）**：`_beat_contract` 新增 `is_first_interior_reveal`（`family=='interior' and bridge_stage in (2,3)`，即 coaxial 的 SPAN 拍或 pan 的 TURN 拍——只命中过门后第一次揭示，不命中后续任何普通室内拍）；命中时在 anchor_rule 追加"UNTOUCHED TRAUMA STATE"条款，措辞对齐既有 IMAGE 1 的 GENUINE DAMAGE 审计（结构损坏/表面腐蚀/植被侵入/碎屑堆积，至少两类)。用户确认 pan 变体的 vestibule 落点帧（现为 SPAN 拍的定格尾帧）也追加一条更轻量的脏乱要求（至少一处可见腐朽迹象），避免转向前那一瞬间显得过于干净。hard_cut 变体本就有独立的衰败措辞（anchor_rule 的 is_cut 分支第4项），不重复处理。

**范围**：coaxial 与 pan 两变体统一处理（两者的 HOLD 拍问题同构）；hard_cut 不受影响（从无门槛停驻帧，衰败措辞已存在）。

**测试**：`tests/test_threshold_variants.py` 新增 `TestBeatContractBridgeFlags`（HOLD/SPAN/is_first_interior_reveal 标记 + 脏乱差条款存在性）、`TestVideoOpeningFirstFrameIndex`（`fix_video_opening`/`check_video_opening` 的 `first_frame_index` 覆盖 + HOLD 拍跳过视频侧全部校验）、`TestPlanVideoSlotsBridgeHold`（HOLD 跳过 + SPAN 锚点重定向 + 缺帧兜底）、`TestMergeGateBridgeHold`（`skipped_bridge_hold` 预期缺失语义）；`tests/test_prompt_fixes.py`/`tests/test_threshold_variants.py` 原有 meta 断言同步改为 `BRIDGE HOLD`/`BRIDGE SPAN`。全量 489 pytest 全绿。

**Rollout 注意**：`packet_cache.json` 无需清理（未新增/改动 packet 字段）；`compose_checkpoints.json` 里若有 Threshold 项目的桥接拍已落在 `pass_beats_done`（部署时正在断点续传中），其存量正文仍是旧两段式叙述、首帧锚点句仍写着旧的 IMAGE 编号——这类存量断点条目建议清理，逼一次整单重新合成，避免装配阶段用新 meta 规则去配对旧正文产生锚点不匹配。已完成/已渲染项目的 manifest/prompt 永远是纯字面 `[BRIDGE]`，不会被新逻辑误触发，不受影响。改动全部在 `refactor/structure` 分支，未提交；需重启 :8085 生效。

---

## 12. TBCP v4 — 单拍收编过门 + 首拍不得过早 + 禁止多宫格图（2026-07-21）

**触发**：用户实测反馈三点——①帧序列一开始就在室内（beat_ladder 把过门拍排到了 Beat 1/2，`_img1_is_pre_bridge` 兜底分支被真实命中，IMAGE 1 直接变成门口帧）；②过门过程过度工程化，只需要「完全室外→完全室内」两张可见图 + 一段视频；③实际生成图出现拼贴/多宫格画面。

**方案**：

- **过门收编成单一 `bridge_stage=1` 拍**：coaxial 与 pan 变体统一为一拍——不再有 HOLD（bridge_stage=1 旧义）/SPAN（2）/TURN（3）三段。该拍自己的 IMAGE 就是定格（pan 变体已完成转向）的室内首现帧；VIDEO 是过门唯一可见片段，正文一镜到底叙述"推进→穿越门槛→（pan 变体）原地摇镜转入纵深轴→定格"，首尾帧锚点就是普通的 IMAGE i → IMAGE i+1，不再需要 TBCP v3 的 `start_anchor_slot` 重定向（因为已经没有 HOLD 帧需要跳过）。`video_generator.plan_video_slots` 的 `skip_bridge_hold` 分支、`prompt_pipeline` 的 `is_bridge_hold`/`is_bridge_span`/'sill'/'vestibule' 族分支、`server_common.IMG2IMG_TURN_CONTROL_PROMPT`（旋转专用）全部删除；新增 `IMG2IMG_BRIDGE_TURN_CONTROL_PROMPT`（推进+转向合并版控制指令）供 pan 变体单拍使用。旧 `skipped_bridge_hold`/`start_anchor_slot` 字段仅保留读取以兼容存量 manifest。
- **强制最少铺垫拍数**：beat_ladder 结构校验新增硬性下限——过门拍下标必须 ≥3（即至少 2 个普通室外拍在其前面，覆盖"建立大环境印象"+"展示室外清理/维修进度"），LLM 指令与确定性兜底节拍梯生成器同步落这条下限；`_img1_is_pre_bridge` 兜底分支保留但理论上不再会被真实命中。`image_1_system` 新增硬性规则："WIDE ESTABLISHING SHOT"——IMAGE 1 必须是整个场景/载体的大全景，不得是细节特写，覆盖 Standard 模式/纯室内载体也没有真正室外可言的场景。
- **禁止多宫格图**：`image_1_system`、`_batch_shared_system_prompt`、`compose_remaining_beats` 内层单拍兜底 system prompt 三处统一追加"SINGLE CONTINUOUS PHOTOGRAPH"硬性条款——每张 IMAGE 必须是单一真实照片，严禁拼贴/多宫格/分屏对比/故事板；并明确点破"Grid A1-C3"记号只是给写作者用的内部构图坐标约定，不得被理解成要画出真实网格线。根因假设：现有提示词大量出现"Locked anchors: X at Grid A2..., Y at Grid B2..."这类分区坐标语言，尤其在建立性/多锚点拍里密集出现，是触发生图模型误解成"多面板参考图/故事板"的最可能诱因。

**范围**：coaxial 与 pan 两变体统一收编；hard_cut 变体结构不变（本就是两拍+声明式切点，天然满足"两张图"形态）。

**测试**：`tests/test_threshold_variants.py`/`test_prompt_fixes.py`/`test_spatial_negative_example.py`/`test_structural_beat_rework.py` 里所有 bridge_stage 2/3、'sill'/'vestibule' 族、`is_bridge_hold`/`is_bridge_span`、`skip_bridge_hold`/`start_anchor_slot` 重定向、`BRIDGE HOLD`/`BRIDGE SPAN` meta 相关用例改写为单拍模型；外部 skill 参考文档 `threshold-bridge-consistency-protocol.md`（改写为 TBCP v4）与 `prompt-templates.md`（"Threshold Bridge"章节改写、Interior IMAGE exemplar 措辞同步）一并更新，因为它们会被 `load_reference_file` 逐字注入生成 LLM 的 system prompt。

**Rollout 注意**：改动全部在 `refactor/structure` 分支，未提交；需重启 :8085 生效。未做实机生成验证（本次仅代码+单测层面）。

---

## 13. TBCP v5 — 过门帧回归"原始"+ 过门后强制清理工序 + 过门视频提示词收紧（2026-07-26）

**触发**：用户实测反馈——①「过门帧有人工痕迹，不够原始」（渲出来的室内首现帧地面像被扫过、杂物码得整齐、表面看着刚修过，读起来像被布景过的房间，而不是刚被推开门的废墟）；②「过门帧后也要加一个清理的工序」（首现帧满地瓦砾，下一拍却直接贴板/刷漆，现实里必然存在的清运整拍消失）；③视频提示词同时优化。

根因定位：①契约侧首现帧只要求 2 类衰败（IMAGE 1 自己的 GENUINE DAMAGE 审计要 3 类），且**从未有过"零人工痕迹"条款**（IMAGE 1 有 ZERO INTERVENTION EVIDENCE，首现帧没有）；渲染侧只有门框清除一道像素门禁，门框出画之后没有任何东西看过这一帧"脏不脏"。②节拍梯对过门后第一拍只要求"是室内施工工序"，没有指定清理。③过门片段的正文契约只写了运镜几何（推进/门框擦出/曝光滚动/锚点放大），没有一条约束镜头进去之后看到的**状态**，i2v 完全可以把室内插值成干净房间。

**方案**：

- **首现帧契约加严（`_beat_contract` 的 `is_first_interior_reveal` 分支 + family_contract 条目）**：衰败类别 2→3 类（与 IMAGE 1 的审计门槛对齐）；新增 ZERO INTERVENTION EVIDENCE（工具/工具箱/梯子/脚手架/漆桶/防水布/工作灯/锥筒/码放的新材料，以及任何看着已修补/已清洁/已粉刷的局部）；新增 UNARRANGED（杂物必须散落在重力和时间让它落的位置，不得扫拢成堆、不得对齐摆放），并明写"地面不得读作已清空——那是下一拍的活"。
- **首现帧事后文本校验加严（`check_first_interior_reveal_decay`）**：门槛同步提到 3 类；新增人工痕迹词表检测，**带否定窗口**（`_mentions_without_negation`，命中词前 60 字符内有 no/without/free of/never 等否定线索即放行）——因为契约本身就鼓励写 "no ladders, no tools anywhere in frame" 这类澄清句，`fix_image_prompt_with_vlm_feedback` 更是主动追加，硬匹配会把最合规的稿子判死。回炉指令（`rework_...`）同步要求 3 类 + 显式否定式删除人工痕迹。
- **首现帧像素门禁（新，`check_first_interior_reveal_raw_state` + frame_generator）**：紧跟门框清除之后跑，对真实像素判 4 项（人工痕迹 / 已被收拾过 / 已被修复过的表面 / 衰败类别少于 2 类）。未通过则以该帧自身为参考做一次定向状态修正（`IMG2IMG_RAW_STATE_CONTROL_PROMPT` + 把 VLM 报回来的具体原因写进指令，镜头锁死只改内容），最多 `_RAW_STATE_MAX_FIXES=1` 次——这一步不改构图，改不动通常是模型不肯加脏，多刷一轮边际收益很低。修完仍不过只写进 `vlm_qa_reason` 留痕（与门框清除的留痕合并，不互相覆盖），绝不拦渲染。进度事件 `raw_state` 与 `door_clearance` 同款，前端动态流已接。
- **过门后强制清理工序（节拍梯层）**：`_post_crossing_cleanup_rule` 同时写进 coaxial/pan 与 hard_cut 两套 `threshold_split_rules`；结构校验新增硬性要求——过门拍的下一拍 `operation` 必须是 `clearing`，否则打回重生成并把违规喂回重试提示；确定性兜底节拍梯生成器同步在 `t_idx+1` 落 `clearing`。契约层新增 `is_post_reveal_cleanup` 标记（Threshold 模式 + 上一拍是过门拍/硬切拍 + 本拍是普通室内拍），带自己的 family_contract 条目：搬走一切可搬运的杂物到裸地面，但**结构性衰败（锈迹/裂缝/水渍/剥落/腐朽）原封不动留给后面的修复拍**；VIDEO 要求工人拿一件具名工具反复往外搬、清出的面积随片渐进扩大 + 渣土容器渐满（本拍的两条可观测进度线）。新增确定性校验 `check_post_reveal_cleanup_prompts`（IMAGE 没写清空结果 / IMAGE 把衰败一并擦干净 / VIDEO 没有搬运动作三项），挂在 `validate_beat_prompts` 上，两条生成链（批量直出 + 单拍兜底）自动共享，按风格瑕疵留痕不拦截。
- **过门视频提示词优化**：过门拍的 family_contract 新增两条——(1) 全程原始废墟（镜头进去看到的第一眼就必须是脏的，穿越途中不得清理/修缮/安装，不得出现工具梯子脚手架防水布工作灯堆料），(2) 一镜到底（不得切/淡入淡出/叠化/变速/定格，唯一的"剪辑感"只能是门框擦出画面；且不得把这一拍写成施工延时）。兜底过门视频正文同步补这两句。新增确定性校验：`check_video_process_content` 的 bridge 分支检测穿越途中的施工/清理动词（`_BRIDGE_WORK_ACTION_PATTERN`，只匹配 -ing/-s/被动态与"动词+杂物宾语/杂物宾语+动词"，避免误伤 peeling paint / stacked wreckage / 门框 clears the frame edge / 光楔 sweeping across the floor 这类合法措辞），命中计入结构性硬伤，对该拍定向回炉一轮。

**范围**：coaxial/pan/hard_cut 三变体统一（清理工序对三者都强制；首现帧原始度对 bridge 变体的 i2i 帧生效，hard_cut 的 t2i 新链头帧走它自己的 anchor_rule 衰败条款，像素门禁按 `is_bridge` 判定不覆盖它）。

**测试**：`tests/test_threshold_variants.py` 新增 `TestPostRevealCleanupContract`（清理拍标记/契约文本、过门拍与后续普通室内拍不误命中、Standard 模式不命中、首现帧 3 类+零人工痕迹、过门片段两条新契约）、`TestBridgeClipWorkContentCheck`（穿越途中施工/清理命中 + 纯运镜措辞不误报）、`TestPostRevealCleanupPromptCheck`、`TestPostCrossingCleanupLadderGate`（节拍梯结构校验打回 + 违规回写重试提示 + 合规一次通过）；`tests/test_structural_beat_rework.py` 新增 2 类被判不合格、3 类通过、人工痕迹命中/否定式放行/非首现拍跳过；`tests/test_frame_generator.py` 新增 `TestFirstInteriorRevealRawState`（定向修正带上 VLM 原因、修完仍不过保留帧并留痕、通过则不动帧）。全量 pytest 除 2 项与本次无关的既有失败（`test_ideation_trend_sources` 两例，命中的是外部 skill 的 `idea-engine.md` 文案与 run_ideate 兜底，改动前后一致）外全绿。

**Rollout 注意**：外部 skill 参考文档 `threshold-bridge-consistency-protocol.md`（Rule 1 补"穿越途中不干活/一镜到底"、Rule 6 改写为三条强制项+像素门禁说明、新增 §9 Post-Crossing Cleanout、审计表改一行加两行）与 `prompt-templates.md`（首现帧说明改写、Interior IMAGE exemplar 补零人工痕迹措辞、三条过门 VIDEO exemplar 补全程原始+一镜到底、总检查清单补三项）一并更新，因为它们会被 `load_reference_file` 逐字注入生成 LLM 的 system prompt。`packet_cache.json` 无需清理（未改 packet 字段）；`MILESTONE_POLICY_VERSION` 已从 `visible-milestones-v1` 换代为 `visible-milestones-v2-post-crossing-cleanout`：它进 `get_brief_fingerprint`，所以全部存量 `compose_checkpoints.json` 断点与 `packet_cache.json` 条目自动失效、整单重排（存量断点的节拍梯没有清理拍，续传会绕过新的结构校验产出旧形态，只能靠指纹换代逼它重来），无需手工清文件。改动全部在 `refactor/structure` 分支，未提交；需重启 :8085 生效。未做实机生成验证（本次仅代码+单测层面）。

## 14. TBCP v6 — 硬切槽位改为真实生成的跨越片段（2026-07-30）

**触发**：用户实测反馈——「过门硬切镜头不生成」。根因就是 §2.3/§6 当年的设计选择：`hard_cut` 变体的 VIDEO T 槽位不送 i2v，正文被确定性覆盖成固定占位声明（`HARD_CUT_VIDEO_PLACEHOLDER`），成片在过门处直接 concat 硬拼。于是这一单的过门只存在于文字里，用户在结果面板看到的就是一个不生成、也无法重试的空槽。

**修订**：`[CUT]` 槽位照常生成视频，正文就是一段普通的过门跨越镜头提示词（"就当正常描述视频提示词"）。

- **视频侧一律按跨越镜头处理**：新增单一判据 `beat_is_crossing_clip(beat)`（`bridge_stage == 1` 或 `hard_cut`）。`apply_proactive_fixes`（`fix_camera_contradictions` 的 is_moving 口径）、`validate_beat_prompts`（`check_camera_contradictions` / `check_bridge_sterile` / `check_video_process_content`）、`rework_structural_video_beat`（回炉时要求补运镜正文而不是施工正文）全部改走这个判据——此前它们按 `bridge_stage` 单独判定，会把切入拍当静止施工拍，运镜句直接被删。
- **契约文案**：`_beat_contract` 的 `is_cut` 分支从"占位声明、本拍真实内容是 IMAGE"改成与 bridge 同款的完整跨越镜头契约（绑定 IMAGE i → i+1；门在片段里被推开→推进→门框擦出画→曝光/白平衡滚动→完全入内），外加本变体独有的一条"片段开始前不得预览室内"（起帧的门是封闭的，揭示发生在片段内）。批量/单拍两条生成链的规则文本、`threshold_split_rules` 的 hard_cut 分支、brief 阶段的变体定义同步改写。
- **占位覆盖与豁免全部撤掉**：三处 `v_p = HARD_CUT_VIDEO_PLACEHOLDER`（批量、单拍、兜底）删除，兜底稿换成与 bridge 兜底同款的真实跨越镜头；结构性回炉不再跳过切入拍；视频侧校验不再整段跳过（§5.2 的"[CUT] 槽位跳过全部视频词汇校验"作废）。
- **审查规则**：`_local_beat_review_system_prompt` 的 `DECLARED CUT-IN SLOT` 条目保留"起帧门封闭是既定状态、没有 peek/无 scale-up、构图不必与外部帧对齐"这部分豁免，撤掉"不是片段、不按片段规则judge"——现在它按跨越片段judge（纯运镜、无工人、室内全程未被动工）。定向重写（`fix_beat_from_sequence_review`）对 `[CUT]` 正文解冻，并按运动镜头跑收尾的确定性修复（此前 `fix_camera_contradictions` 默认静止口径会把跨越片段唯一的动作句删掉，BRIDGE 槽也受此影响，一并修）。
- **旧单兼容按正文识别，不按标签识别**：`[CUT]` 标签在新单里仍然有用（帧渲染据它把室内首帧当新批链头、`family_anchor_seq`/`image_space_family` 据它换族、审查据它豁免 peek），所以不能拿标签判断"要不要生成"。改由 `plan_video_slots` 检查正文是否以 `DECLARED HARD CUT` 开头（`HARD_CUT_PLACEHOLDER_PREFIX` / `is_legacy_hard_cut_placeholder`）：只有切换前落盘的旧单继续 `skip_cut`，`skipped_cut` 相关的合成门禁/恢复轮/前端中性卡片全部原样保留，仅退化为旧单专属路径。
- **帧侧不变**：切点后的室内首帧仍是新批链头 + bridge 版 i2i 控制指令（§4.3 不受影响）；`frame_pair_contract` 的"两端差异过大"只是警告，不拦截，切点这一对帧照常提交。

**范围**：只动 hard_cut 变体的视频侧；coaxial/pan 行为不变（除定向重写的运动镜头口径修正，对它们是修 bug）。

**测试**：`tests/test_threshold_variants.py` — `TestCrossingClipPredicate`、`TestLegacyHardCutPlaceholder`、`test_hard_cut_beat_is_validated_as_a_crossing_clip`、`test_hard_cut_beat_without_any_camera_move_is_flagged`（占位式正文现在会被校验报出来，不再静默放行）、`test_cut_beat_contract_demands_a_real_generated_crossing_clip`（契约不得再出现 placeholder/no video clip 口径）、`TestPlanVideoSlotsCut`（新单生成 / 旧单占位仍跳过）；`tests/test_sequence_consistency_review.py` — 审查规则口径、[CUT] 正文解冻、跨越片段运镜句不被删。全量 pytest 1355 passed。
