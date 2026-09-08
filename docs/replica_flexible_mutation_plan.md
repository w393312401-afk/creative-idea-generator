# 灵活复刻改造方案（Flexible Replication）

- **文档版本**：v1.1
- **落地时间**：v1.0 2026-09-03；v1.1 2026-09-07（第一性原理复审后的修订，见文末「v1.1 改了什么」）
- **触发**：一批海蚀洞钢构小屋变体（20 图 + 19 视频）交付后复盘，发现三类硬伤全程无人拦截
- **涉及模块**：`prompt_pipeline/{ontology,object_ledger,anchor_geometry}.py`（新增）、
  `prompt_pipeline/mutate.py`、`prompt_pipeline/reverse.py`、`prompt_pipeline/__init__.py`、
  `prompt_pipeline/composers/base.py`、`replica_pipeline.py`
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

- `CONSTRUCTION_ROLES`：22 个角色（v1.1 补了 `roof`——`shingle conical roof` 从前被判成
  防潮层，词表里根本没有屋面），每条带 `zh` 标签与渲染 beat 名用的 `verb`。声明顺序即
  工序默认秩 `ROLE_RANK`，但**秩只用来给一拍挑工具**（按秩取模），依赖倒置读的是
  `object_ledger.ROLE_DEPENDENCIES` 那张显式前置表。v1.0 的注释在这里写反了。
- `infer_role(beat)`：**一趟一个字段面**，按可信度排 —— 显式 role → `operation` →
  其余标题字段 → `stage` → 铺陈散文；五趟都读不出返回 `UNKNOWN_ROLE`。
  v1.0 把几个标题字段拼成一段话一次过读，而 `re.search` 认的是哪条模式先匹配、不是哪个
  字段先出现，于是「这一拍要干的活」和「画面里还有什么」被放在同一个平面上比：实测那份
  17 拍的已交付母本里错了 7 拍（`hero reveal` 判成装窗、`furnish upper chamber` 判成取暖、
  `plank floor` 判成防潮层）。分趟之后错 1 拍。
  读不出时返回 unknown 而不是默认 'structure'：兜底渲染照着 role 从零写，默认成骨架
  意味着一条 `coat facade` 的涂层拍被写成「耐候钢 portal frame」——静默建错东西，比承认
  读不出来更难在成片前发现。
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

三条规则，两档 severity（v1.1 拆开的，理由见下）：

1. **凭空出现**（blocking）：物件在第 N 拍被当作已存在（起始态 / 锁定锚点）引用，却没有
   任何 M < N 拍把它建造出来
2. **已完成回归**（blocking）：物件在第 M 拍完工后，又在 N > M 拍被动作重新加工
3. **依赖倒置**：物件级硬依赖缺位仍是 blocking；**角色级顺序惯例降为 warning**

### v1.1：闸判的是「变异弄坏了什么」，不是「这条梯子标不标准」

`validate_object_ledger(beats, baseline_beats=...)` 收下母本的阶梯之后，母本自身就有的
缺口一律降级为 warning 并标 `inherited`，只有变体新引入的才拦单。

这不是宽严之争，是判据对不对。母本是原片的反推记录，它的账不平通常意味着**反推漏了
一拍**；变体 1:1 继承骨架，必然把同一个缺口原样带下来。实测：仓库里唯一那份已交付母本
（`outputs/replica_jobs/replica_1ef74020a25b`，17 拍）走生产参数生成变体，v1.0 判死 3 条，
全部是母本自带、且没有一条是复盘里那三类硬伤——闸拦下的是母本的旧账，而真正该拦的东西
淹在里面。v1.1 之后同一条母本：0 blocking、2 warning。

角色依赖表降为 warning 同理：它写的是一套常规工序顺序，不是物理定律。先立架后做排水
垫层的井屋、先挂舱门后开窗洞的舱体都真实存在。

物件级硬依赖里删掉了 `flue_pipe → flue_collar`：一段烟囱管当然穿过某个领圈，但领圈
**被单独描写过**不是物理必然，它多半只是没被反推捞出来。复盘里那条真正的烟囱缺陷
（第 9 拍已存在、第 12 拍才 roughed in）是顺序问题，由规则 1 抓，从不依赖这条。

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

**判死还是记诊断，由申报的来源决定**：

- **声明式**（变体线：模型或兜底渲染填的 `produced_objects` / `inherited_objects`，
  带 `objects_source='declared'`）→ 精确比对 → `blocking`，在 `run_audit` 拦下交付
- **推断式**（母本线：`annotate_beats` 从散文捞词补出来的，标 `objects_source='inferred'`）
  → `warning`

这不是纪律松了。实测（仓库里 6 份已交付提示词包共 86 段视频）推断模式误报率约两成，
全部来自同一构件在图与视频里叫法不同（视频说 "subfloor joist grid"，图说
"engineered floor deck"）。拿那样的判据去拦交付，拦掉的多半是好片子。

**v1.1 修的两处**（v1.0 把「精确」写在了纸上，代码里并不成立）：

1. **命名空间打通**。申报值写的是 role 名（`'structure'`），账本比对的是 canonical id
   （`'structural_frame'`），两个空间几乎不相交——申报的东西一个也对不上，比对实际退化
   成纯散文推断，却按 blocking 判。现在 `_declared_objects` 经 `ROLE_OBJECTS` 把 role 名
   展开成它名下的全部 canonical id。
2. **模式判据改看来源，不看字段在不在**。v1.0 只数键在不在，而 v1.1 的 `annotate_beats`
   给每一份阶梯都补了这两个字段——只数键的话，母本线会因为被补过字段就「升级」成申报式，
   那两成误报直接变成拦单理由。
3. **角色层兜底在两种模式下都算**。v1.0 只在推断模式下放宽，于是申报模式一遇到叫法不同
   就直接判死——那正是推断模式量出两成误报的同一件事，只是换了个地方发生。现在它降一档
   而不是放行：角色对得上记 warning，对不上才拦单。

### 3.4 ~~`prompt_pipeline/topology.py`~~ —— v1.1 已删除

v1.0 在这里放了一个「用因果拓扑同构 + 节奏曲线替代拍数恒定」的模块。它在生产路径上
**恒真**：变体由 `deepcopy(母本拍)` 起手，`role` / `duration_sec` / `index` 全部原样继承，
于是 `validate_isomorphism` 比的是同一份数据和它自己的拷贝——role 链、拍数、节奏曲线三项
必然相等。实测那份 17 拍的母本：`chain equal? True / role_first equal? True /
rhythm equal? True / issues: []`，不是「这次刚好没问题」，是构造上不可能有问题。

它宣称要解放的拍数弹性也走不通：三道锁焊死了 1:1——`_LLM_MUTATE_BEATS_SYSTEM` 写着
「输入几拍就输出几拍」、`_llm_mutate_beats` 用 `len(parsed) == len(source_beats)` 丢弃不
等长的回复、`beat_tolerance` 默认 0。留着它，等于留一个假接口和一道假闸。

**真要放开拍数**，要一起做三件事：变体不再逐拍继承 role、LLM 契约允许 N±k 拍、
落盘与合成器接受拍数不等的对位。那时再把这个模块加回来，它才第一次有事可做。
拍数 1:1 目前由 `generate_orthogonal_variant` 逐拍遍历母本这一结构本身保证，
没有它也不会松。

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

v1.1 删掉了俯仰角解析与 `reproject_scale` 的两个物距形参：前者解析出来一行都没被消费过
（要用上俯角得先知道物体是立面还是地面，packet 里没有这个信息），后者唯一的调用点从来
没传过值。同时把「锚点这侧为什么不像 cast 那侧按机位高估物距」写进了 docstring——
`working_distance`（机位高 × 1.6）是给**工人**的估计，锚点是地貌与建筑体，镜头蹲下一米
不会让它近一米；套过去会把一次 20mm→35mm 的换镜算成占比翻近三倍。

### 3.6 反注入：母本散文不进变异上下文

`_llm_mutate_beats` 现在只喂**角色骨架**（role / role_label / target_material /
suggested_title / space / duration），母本的 `visible_action` / `state_before` /
`state_after` 一个字都不进。同时移除了原先直接透传的 `carrier` 与 `scene_signature`。

兜底路径（LLM 失败时）改为 `render_beat_from_role` —— 从角色**从零写**，不改写母本
任何一句话。原先的兜底是「把母本原文正则替换一遍」，那正是泄漏的主干道。

第三道保险 `scrub_carrier_leak`：命中载体名词的字段整体作废、退回角色渲染。返回空串
是故意的 —— 半修半留的句子（"the rusted  beneath the overhang"）比原样泄漏更难发现。

`apply_slot_replacement` / `apply_trace_mapping` 在 v1.1 **已删除**。v1.0 留着它们
「兼容仍在 import 的调用方」，而全仓查下来：生产代码零调用，唯一的 import 是
`prompt_pipeline.__init__` 的再导出和它自己的老测试。留一张不再扩充、也不再有人走的
词典，只会让下一个人以为散文层替换仍是一条可选路径。

`_CARRIER_LEAK` 那张载体名词表也标清楚了它的身份：**事故补丁，不是机制**。表里那几个词
是那一批实际漏出来的，它挡不住下一个母本的载体名词；真正的机制是上游那条「母本散文一个
字都不进变异上下文」。不要指望往这张表里加词能解决泄漏。

---

## 4. 接线点

| 位置 | 改动 |
|---|---|
| `mutate.generate_orthogonal_variant` | 新增 `strict`（默认 True）；账本硬闸在返回前判、**对着母本的基线判**，命中即抛。变体每拍标 `objects_source='declared'` |
| `reverse.normalize_beat_keys` | v1.1：调用 `annotate_beats`，把 `role` 与物件申报就地登记进阶梯。这是母本线唯一一处「每份阶梯、每次读写都会经过」的地方 |
| `replica_pipeline.run_audit` | 视频差量守恒，blocking 命中即 `audit_failed`，与禁用元素门禁同层 |
| `replica_pipeline._write_beats` | 落盘前登记 role 与物件申报，并挂一份账本诊断，母本变体一视同仁。v1.1 把 `setdefault` 改成直接赋值——诊断是**这一版**阶梯的读数，setdefault 会把第一版的清单永久冻在文件里 |
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
- 35mm 重投影后占比 > 50%；同焦段保持原值；读不出焦段不动
- `70mm deep aggregate` 不是焦段

v1.1 新增/改写：

- 母本自己就有的缺口不拦变体（`baseline_beats` 差集），变体新引入的照拦
- 角色顺序惯例只记 warning
- `operation` 压过其余标题字段（`hero reveal` 不再被判成装窗）
- `shingle conical roof` 判成 roof；读不出角色返回 unknown 并记诊断
- 登记补出来的申报标 `inferred`，不把母本线升级成可拦单
- 申报写 role 名也能对上 canonical id
- 申报模式下同角色的另一种叫法记 warning，角色也对不上才 blocking

---

## 6. 已知边界

1. **推断式账本仍是 warning**。v1.1 让 Pass B 落盘时就登记 `role` 与
   `produced_objects` / `inherited_objects`（`annotate_beats`），但那是**正则从散文里捞
   出来的**，标着 `inferred`，不能拿去拦交付——换个标签不会让判据变准。要让母本线真的
   升级成 blocking，得让 Pass B 的模型自己申报这两个字段（此时它正看着帧），那是下一步。
2. **物件词表是受控的**，覆盖不到的构件不进账。扩表的门槛：它是不是「一旦装上就
   必须在后续每一帧里继续存在」的东西。
3. **俯仰角不参与占比计算**。它需要知道物体是立面还是地面，packet 里没有这个信息，
   硬猜的收益低于误差。
4. **物距按机位高度估**（`working_distance`）。这是估计不是测量 —— 它只需要好到能
   把 20mm 和 35mm 分开，而那一步绰绰有余。
5. **拍数仍是 1:1**，由 `generate_orthogonal_variant` 逐拍遍历母本这一结构本身保证。
   要放开，见 3.4 里那三件必须一起做的事。
6. **角色识别仍是正则**。分趟读之后实测 17 拍错 1 拍，但它依然是整条链的承重假设——
   role 决定变体这一拍建什么。真正的解法是 Pass B 直接申报 role（此时它正看着帧），
   与边界 1 是同一件事。写进阶梯之后至少它可见、可改、只推一次。

---

## v1.1 改了什么

一次第一性原理复审。四个模块留下三个，判据从「这条梯子合不合规」改成「变异弄坏了什么」。

| # | 改动 | 为什么 |
|---|---|---|
| 1 | 账本闸对着母本基线判差集；角色顺序惯例降 warning | 真实母本走生产参数被判死 3 条，全是母本自带、且没有一条是复盘里的硬伤 |
| 2 | `role` / 物件申报在 Pass B 落盘时登记进阶梯 | role 是母本与变体之间唯一被继承的东西，却从来没被写下来，每个调用点各推一次 |
| 3 | `infer_role` 分趟读 + unknown + roof 角色 | 实测 17 拍错 7 拍；默认成 'structure' 会静默把一拍改写成钢门架 |
| 4 | 删除 `topology.py` | 在生产路径上恒真（比的是同一份数据的拷贝），且它要解放的拍数弹性被三道锁焊死 |
| 5 | 申报命名空间打通 + 模式看来源 + 角色兜底两种模式都算 | 「申报式=精确可拦单」在代码里不成立：申报的 role 名与比对的 canonical id 从不相交 |
| 6 | 删除 `apply_slot_replacement` / `apply_trace_mapping` / 俯角解析 / 物距形参 / 包内 asmr / `'en'` 标签 | 全是零消费者的东西；留着它们等于留下几条并不存在的可选路径 |
| 7 | 材质词表单一归属（`detect_asmr_bucket`）；角色动词并进 `CONSTRUCTION_ROLES` | 同一段轴文本在两张词表里可能落到不相干的桶；三张按 role 并排的表，新增角色要改三处 |
| 8 | `_write_beats` 的 `setdefault` 改赋值 | 诊断会永久冻在第一版，用户改完再存看到的还是旧清单 |
