# 灵活复刻改造方案（Flexible Replication）

- **文档版本**：v1.0
- **落地时间**：2026-09-03
- **触发**：一批海蚀洞钢构小屋变体（20 图 + 19 视频）交付后复盘，发现三类硬伤全程无人拦截
- **涉及模块**：`prompt_pipeline/{ontology,object_ledger,topology,anchor_geometry}.py`（新增）、
  `prompt_pipeline/mutate.py`、`prompt_pipeline/__init__.py`、`prompt_pipeline/composers/base.py`、
  `replica_pipeline.py`
- **与 `replica_baseline_and_orthogonal_mutation_spec.md` 的关系**：本文修订该规范 3.1
  「骨架硬冻结协议」的**冻结对象**，不改变两阶段架构本身。

---

## 1. 复盘：那一批交付错在哪

按性质分三类。全部走到了成片，`chain_guard` 一条都没拦住。

| 类别 | 实例 | 为什么没被拦住 |
|---|---|---|
| 凭空出现 | 窗户在 IMAGE 9 第一次作为 locked anchor 出现，被 10–20 共 12 张图继承，**没有任何一拍建造过它**；外墙包覆那一拍的范围明写着只有左墙和前舱壁，右墙自始至终不存在。同类还有门框企口、水槽、厨房岛台 | `chain_guard` 只比对相邻两帧像素，看不见跨十几拍的账目缺口 |
| 已完成回归 | VIDEO 9 让工人「用铝耙找平碎石」，而碎石层在 BEAT 3 已是压实找平的终态 | "no regression of any previously completed feature" 只是写给模型看的一句话，代码里没有判据 |
| 依赖倒置 | 烟囱穿顶领圈在 IMAGE 9 已存在，IMAGE 12 才 roughed in，IMAGE 18 才密封 | 同上 |
| 词汇泄漏 | `railcar` / `carriage` / `scrapyard` / `camper` / `blast doors` / `mountain skyline` | `apply_slot_replacement` 的词典是母本专用硬编码，词典外的名词原样留下 |
| 名实脱节 | `erect timber portal` 底下站着三道 Corten 钢拱；`lay cobble subfloor` 铺的是碎石；`lay stone infill` 装的是钢板 | beat 名从母本原样继承，body 被 LLM 换成新材质，两者各说各话 |
| 几何不自洽 | 同一锚点在 20mm/2.6m 俯 30° 与 35mm/1.6m 平视下都声称占画幅高度三分之一；人物占比 `~35%` 逐字抄进 19 段视频 | 占比是被复制的字符串，不是被计算的量 |

### 根因（三条，都在代码里）

1. **变异发生在散文层**。`mutate.apply_slot_replacement` 是在已渲染好的句子上跑正则，
   词典硬编码母本词汇（集装箱 / 河岸 / 鲟鱼 / 木棚）。散文层没有「这是同一个构件」
   这个概念，所以做不到全链一致，也拦不住词典外的名词。
2. **母本散文直接进了变异的上下文**。`_llm_mutate_beats` 把每一拍的
   `visible_action` / `state_before` / `state_after` 原文 JSON 塞给模型。反推那侧
   Pass A 的反注入纪律很严（`_scrub_config_for_pass_a` 连形参都不留），变异这侧
   没有对称的反继承。
3. **复刻线完全绕过状态账闸门**。`frame_state.build_space_state_ledger` /
   `validate_frame_state_contract` 是现成的，母本线在 `__init__.py:12062` 有硬闸，
   但 `replica_pipeline.py` 一次都没调用过，`mutate.py` 全文件零处 validate。
   `scene_state.py:22` 的注释自己写着「反推复刻线就是这么降级的，两条规则在那里
   都只记诊断不拦单」。变体是**裸奔**到下游的。

---

## 2. 设计原则：把冻结从表层挪到深层

现在的问题不是锁得不够，是**锁错了层**——冻死了句子和整数（表层），放任了因果和
账目（深层）。

### 松开（原先冻得过死）

| 规范 3.1 原条款 | 问题 | 改成 |
|---|---|---|
| 拍数 N 恒定，严禁拆拍合拍 | 不同材质工序数天然不同（夯土要养护、钢构不用） | 冻**因果拓扑同构** + **节奏曲线对齐**，拍数弹性由调用方给容差 |
| 镜头机位完全继承 | 新主体尺寸不同，绝对机位照抄必然构图失衡 | 冻景别序列与视线关系；占比按新机位重算 |
| 四轴全量置换 | 强制全换导致语义崩坏 | 允许部分轴保持，但轴内必须自洽 |

### 锁死（原先根本没锁）

- 物件本体一致性：同一 role 全链解析同一材料
- 账本闭合：introduced → completed → 不回归
- 依赖图无环且无倒置
- 锚点几何自洽：机位变了就重算
- 视频实体 ⊆ 相邻帧差量

保留原样：进出场时间戳（t=0 进 / t=7.5s 撤）——与完播率强相关且与材质无关。

---

## 3. 落地内容

### 3.1 `prompt_pipeline/ontology.py`（新增）— 角色本体与材质包

构件先归到**角色**（role）：它在建造逻辑里承担的职能，与具体材料无关。角色是母本
与变体之间唯一被继承的东西；具体材料在渲染时才由 `MaterialPack` 解析。

- `CONSTRUCTION_ROLES`：21 个角色，**声明顺序即工序默认秩**（`topology.ROLE_RANK`
  直接读它，新增角色必须插到正确位置，不能追加到末尾）
- `infer_role(beat)`：三趟读 —— 显式 role → 标题字段 → `stage` 结构化字段 → 铺陈散文。
  `stage` 排在散文之前，因为一条 `stage='demolition'` 的拆除拍，描写里一句
  "lifts away the shack from soil ground" 里的 `ground` 会让它被判成场地平整。
- `MaterialPack.resolve(role)`：纯函数式查表，同一 role 问一百次得到同一答案 ——
  这是「同句既 slate 又 basalt」不可能再发生的原因
- 材质包按**类目**（metal / stone / timber / earth / textile / composite）组织，
  不按预置场景：换一个母本不需要改任何词典
- `render_beat_title(role, pack)`：beat 名与 body 从同一次解析渲染

### 3.2 `prompt_pipeline/object_ledger.py`（新增）— spec 层硬闸

**为什么不复用 `frame_state.validate_frame_state_contract`**：那道闸吃的是母本线的
schema（`before_state` / `package_operations` / `changed_grid_cells`）；复刻线的 beats
是另一套（`state_before` / `visible_action`），且没有 `package_operations`，直接接上去
每一拍都会挨一条 "declares 0 operations"，一百条假报错淹掉真问题。

三条硬闸（全部 blocking）：

1. **凭空出现**：物件在第 N 拍被当作已存在（起始态 / 锁定锚点）引用，却没有任何
   M < N 拍把它建造出来
2. **已完成回归**：物件在第 M 拍完工后，又在 N > M 拍被动作重新加工
3. **依赖倒置**：角色 / 物件排在它的物理前置之前

判据的克制：
- 物件识别走**受控词表**，只收建造产物；工具、耗材、人、天候单列白名单
  （工具本来就该反复出现，算进账里每把锤子都成一次「凭空出现」）
- 角色依赖只判**两者都在**的对子，缺席不算倒置
- 物件级硬依赖是 **any-of** 而不是 all-of：门洞可以由显式 opening 交付，也可以由
  主体骨架带出来

### 3.3 视频差量守恒（`object_ledger` + `replica_pipeline.run_audit`）

规范 2.3 的 Zero Phantom Changes 此前只是 `composers/base.py` 里写给模型看的一句话。
现在是集合运算：

```
allowed = 目标帧新增的物件 ∪ 此刻已建成的物件
视频里的建造产物 ⊆ allowed
```

**判死还是记诊断，由账本是不是声明式决定**：

- **声明式**（变体线：每拍带 `produced_objects` / `inherited_objects`）→ 精确比对 →
  `blocking`，在 `run_audit` 拦下交付
- **推断式**（母本线：靠散文捞词）→ 放宽到角色层 → `warning`

这不是纪律松了。实测（仓库里 6 份已交付提示词包共 86 段视频）推断模式误报率约两成，
全部来自同一构件在图与视频里叫法不同（视频说 "subfloor joist grid"，图说
"engineered floor deck"）。拿那样的判据去拦交付，拦掉的多半是好片子。

### 3.4 `prompt_pipeline/topology.py`（新增）— 拓扑同构替代拍数恒定

存下来的骨架是**因果顺序**，不是整数：

- `build_topology(beats)` → `role_first` / `chain` / `rhythm`
- `validate_isomorphism(src, var, beat_tolerance=0, rhythm_tolerance=0.12)`
  - 因果倒置 → blocking
  - 拍数超容差 → blocking（默认容差 0，维持既有 1:1 行为；放宽是调用方的显式决定）
  - 节奏曲线漂移 → warning

### 3.5 `prompt_pipeline/anchor_geometry.py`（新增）— 占比重算

存下来的 `(占比, 机位)` 这一对**隐含了物体的真实尺寸**，重新投影即可：

```
H = 2·d·tan(FOV_v/2) = d·sensor_h / f      画幅在物距 d 处覆盖的实际高度
r₁ = r₀ · (d₀·f₁) / (d₁·f₀)
物距未记录时（锁定机位意味着站位基本不动）→ r₁ = r₀ · f₁ / f₀
```

实测效果（海蚀洞那批的真实机位串，参考机位 20mm）：

| 帧 | 机位 | 原占比 | 重算 |
|---|---|---|---|
| IMAGE 2 | 20mm / 2.6m / 俯30° | 33% | 33% |
| IMAGE 3 | 22mm / 2.5m / 俯35° | 33% | 36% |
| IMAGE 7 | **35mm** / 1.6m / 平视 | 33% | **58%** |

人物占比同理：原先 `~35%` 抄进 19 段视频，现在按每拍机位算出 24%–68%。

两个机位任一读不出焦段时**保留原值** —— 猜出来的焦段比不改更糟。
焦段识别要求镜头语境（`lens` / `focal` / `wide` 前缀），否则
"a 70mm deep layer of basalt aggregate" 会被读成 70mm 镜头。

### 3.6 反注入：母本散文不进变异上下文

`_llm_mutate_beats` 现在只喂**角色骨架**（role / role_label / target_material /
suggested_title / space / duration），母本的 `visible_action` / `state_before` /
`state_after` 一个字都不进。同时移除了原先直接透传的 `carrier` 与 `scene_signature`。

兜底路径（LLM 失败时）改为 `render_beat_from_role` —— 从角色**从零写**，不改写母本
任何一句话。原先的兜底是「把母本原文正则替换一遍」，那正是泄漏的主干道。

第三道保险 `scrub_carrier_leak`：命中载体名词的字段整体作废、退回角色渲染。返回空串
是故意的 —— 半修半留的句子（"the rusted  beneath the overhang"）比原样泄漏更难发现。

`apply_slot_replacement` 保留但**已退役**，只为兼容仍在 import 它的调用方；那张词典
不再扩充。

---

## 4. 接线点

| 位置 | 改动 |
|---|---|
| `mutate.generate_orthogonal_variant` | 新增 `strict`（默认 True）与 `beat_tolerance`（默认 0）；账本 + 拓扑硬闸在返回前判，命中即抛 |
| `replica_pipeline.run_audit` | 视频差量守恒，blocking 命中即 `audit_failed`，与禁用元素门禁同层 |
| `replica_pipeline._write_beats` | 每次落盘挂账本诊断，母本变体一视同仁（母本账不平通常意味着**反推漏了一拍**，下游所有变体都会继承这个缺口） |
| `__init__._canonical_anchor_clause` | 新增 `ref_camera_text` / `cur_camera_text`，占比重投影 |
| `composers/base.single_beat_system_prompt` | 人物比例指令按本拍机位算 |

---

## 5. 回归测试

`tests/test_flexible_replica.py` —— 每条用例钉在复盘里的一个具体缺陷上：

- 窗户凭空出现、烟囱倒置、碎石回归各一条
- 账目闭合的梯子必须零报错（防误伤）
- VIDEO 1 幽灵木地板；推断式账本只记 warning
- `screed` 作动词不是物件
- `erect timber portal` + 钢材质包 → 标题不含 timber
- 同一 role 解析 50 次结果唯一
- `stage` 优先于铺陈散文
- 拓扑倒置 blocking；容差放开后拍数弹性生效；角色缺席不算倒置
- 35mm 重投影后占比 > 50%；同焦段保持原值；读不出焦段不动
- `thirty-five` 不被 `thirty` 截断；`70mm deep aggregate` 不是焦段

---

## 6. 已知边界

1. **推断式账本仍是 warning**。母本线要升级成 blocking，需要反推 Pass B 直接产出
   `produced_objects` / `inherited_objects`。那是下一步，不在本次范围。
2. **物件词表是受控的**，覆盖不到的构件不进账。扩表的门槛：它是不是「一旦装上就
   必须在后续每一帧里继续存在」的东西。
3. **俯仰角不参与占比计算**。它需要知道物体是立面还是地面，packet 里没有这个信息，
   硬猜的收益低于误差。
4. **物距按机位高度估**（`working_distance`）。这是估计不是测量 —— 它只需要好到能
   把 20mm 和 35mm 分开，而那一步绰绰有余。
5. **拍数弹性默认关闭**（`beat_tolerance=0`）。既有母本与既有测试都按 1:1 走，放宽
   需要调用方显式传参。
