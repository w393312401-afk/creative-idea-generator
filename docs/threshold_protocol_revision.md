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
