# Spatial Consistency Upgrade Protocol — 微缩沙盘版 (Miniature SCUP v1)

> 本文件是 `gemini-miniature-restoration-composer` 自己的空间一致性协议，**取代**
> base 包（`gemini-veo-restoration-composer`）的同名文件。
>
> 为什么必须自带一份：base 版本锁的是真人施工、建筑广角与人眼胸高视点的实景世界观，
> 并在负向词库里压制微缩与娃娃屋相关词汇——那正是本技能包的交付物本身。两份协议
> 不是详略之别，是互为反面；靠回退借用 base 的那一份，等于在每一份提示词里同时
> 下达两条相反的指令。
>
> **本文件刻意不复述 base 的具体锁值与负向词串**：这份文档整篇都会被内插进 system
> prompt，写进来的每一个字面量都会进入模型上下文。要对照原文时去读 base 包，不要
> 把它抄到这里——同理由见 base 净帧规则"连声明其不存在都不许写"那一条。

---

## 1. 微距光学锁 (Macro Optics Lock)

这一节取代 base 的 Camera DNA（24mm 广角 / 1.3m 胸高视点 / 水平线居中）。

- **焦段与质感**：`macro lens feel`，五十到八十五毫米等效定焦。正文写词形数字
  （`fifty to eighty-five millimetre macro lens feel`），不写 `50-85mm`——数值区间
  会被 NLVTR 门禁判违规，见第 6 节。
- **景深**：`shallow depth of field with creamy background bokeh`。焦外必须是可读的
  柔光斑（林间树冠、桌面木纹、远处苔藓），不是平涂的糊。**浅景深是微缩题材的
  第一识别特征**，缺了它模型会被读成真实建筑。
- **视高**：`model eye-level`——镜头高度以**模型自身**为基准（约桌面上方十到十五
  厘米），或三十到四十五度斜俯的四分之三视角。绝不写 `chest level` / `eye height`
  这类以真人身高为基准的措辞。
- **画幅占比**：模型主体占画幅中央宽度的一半到七成，边缘保留真实沙盘基底（压实
  泥地、青苔、碎树叶、石子）。这圈基底是尺度证据，裁掉它模型就变成了建筑。

---

## 2. 三重尺度锚 (Triple-Scale Anchor Lock)

实景题材只靠一个尺度锚（画面里那个真人）。微缩题材有三个，**必须同时成立**，
它们互相印证才让观众读出「这是模型」：

| 尺度锚 | 物理量 | 在画面里的作用 |
| :--- | :--- | :--- |
| **巨人手** (Giant Hand) | 真人手，掌宽约模型一层楼高 | 唯一的施工主体；手一进画，模型的微缩身份立刻成立 |
| **微缩人偶** (Figurine) | 二十四分之一比例，六到八厘米高 | 常驻居民；它与模型的比例锁死模型的"真实"内部尺度 |
| **沙盘基底** (Terrain) | 真实落叶、石子、苔藓 | 不可缩放的自然物；它是判断整体缩尺的最终物证 |

**恒常性要求**：整个序列中，背景大树干、地面大石块、基底植被的比例必须绝对恒定。
它们不参与施工，也永远不能被"修好"——它们是尺度基准，变了就等于换了个世界。

**巨人手的入画规范**：从画幅边缘伸入（上缘或左右缘，对应 Grid A1 / A3 / B1 / B3），
永远不完整出现全身，永远不站在模型内部。手在画内的部分只有手掌与前臂。

---

## 3. 沙盘包络与防"变成真房子" (Tabletop Envelope & Anti-Full-Scale Protocol)

这一节是 base「防空间膨胀成礼堂」(Anti-Cavernous) 的镜像。base 怕紧凑空间被拉成
大厅；这里怕微缩模型被渲染成真实建筑。

- **包络声明**：模型整体尺寸按**厘米或比较物**声明，不按米（`a palm-sized shell
  roughly thirty centimetres across`）。正文一旦出现以米为单位的层高、净宽、开挖
  深度这类建筑公制，模型就会被当成真房子生成——这类度量一个都不要写。
- **固定结构锚**：必须锁定至少一个不可移动的沙盘结构特征（树桩顶面、岩台边缘、
  木底座切边）。可移动的微型道具（工具、家具、人偶）不算锚点。
- **负向防真实化词库 (Negative Full-Scale Restraints)**：
  `(full-size human room, life-sized building, real architecture, walk-in interior,
  construction worker inside the structure, deformed hands, extra fingers,
  floating detached limbs, cartoon CGI render, digital plastic look:1.5)`

> **与 base 的关键差异**：base 的防膨胀负向词串里含有压制微缩家具与娃娃屋比例的
> 条目——那两类词在本技能包里是**正向词，必须放行**。任何时候都不要把 base 的那串
> 负向词整体搬进微缩提示词；要用负向词，就用上面这一串。

---

## 4. Grid 坐标登记 (Grid Registration)

Grid A1–C3 的九宫格登记制度**保留**，与 base 完全一致——它是漂移门禁的解析锚点
（地标复述、锚点比例锁都按 Grid 单元格比对），换掉会让那几道门失去可校验输入。

- Grid 只是**写手与门禁之间的内部登记约定**，绝不描述成画面里真实存在的网格线、
  分格边框或拼图分屏。每一张 IMAGE 都是一张完整的真实照片。
- 微缩题材的常用登记：模型主体居中（B2），巨人手从 A1/A3 伸入，人偶常驻前景下缘
  （C1/C3），背景树干/岩壁占 A 行。

---

## 5. 剖面室内的空间连续性 (Cutaway Interior Continuity)

微缩题材没有"室内视点"——所有室内工序都是从**模型外部**透过敞开的剖面拍摄的。
因此 base 的「相机视点连续性」（室内视点不能跳回室外视点，除非插入拉出镜头）
在这里不适用：机位从头到尾都在模型外部，视点从未改变。

详细规则见同目录 `threshold-bridge-consistency-protocol.md`（微缩版）与
`miniature-cutaway-architecture.md`。

---

## 6. 自然语言视觉化 (NLVTR) 在微缩题材下的写法

门禁 `check_nlvtr_violations` 禁止：`%` 符号、数值区间（`10-15cm`、`2 to 6`）、
内部缩写（HAL / SCUP / VMFP / NLVTR …）。微缩题材天然满是尺寸，最容易踩中第二条。

| 不要写 | 改写成 |
| :--- | :--- |
| `50mm-85mm macro lens` | `fifty to eighty-five millimetre macro lens feel` |
| `10-15cm above the tabletop` | `about a hand's width above the tabletop` |
| `1:24 scale figurines` | `one-to-twenty-four scale resin figurines` |
| `6-8cm tall` | `roughly the height of a thumb` |
| `15x30mm CMU blocks` | `thumbnail-sized miniature concrete blocks` |

优先用**可视的比较物**（拇指、指甲盖、一枚硬币、手掌宽）而不是数字——这既绕开
NLVTR，也比数字更能让生成模型建立尺度。

---

## 微缩 SCUP 审计检查表

| 审计指标 | 检查项目 | 消除的失真风险 |
| :--- | :--- | :--- |
| **微距光学声明** | 正文是否声明 macro lens feel + shallow depth of field？ | 缺了浅景深，模型被渲染成真实建筑 |
| **视高基准** | 是否用 model eye-level 而非 chest level / eye height？ | 以真人身高为基准会把机位抬到建筑尺度 |
| **三重尺度锚** | 巨人手、微缩人偶、沙盘基底是否至少两个同时在画？ | 单一尺度锚不足以让观众读出缩尺 |
| **基底恒常性** | 背景树干/石块/植被比例是否全序列恒定？ | 基底一变，尺度基准失效 |
| **厘米包络** | 模型尺寸是否按厘米/比较物声明，而非建筑公制？ | 米制声明会触发真实建筑生成 |
| **负向词方向** | 是否误用了 base 的反微缩负向词串？ | 压制 dollhouse scale 等于压制交付物本身 |
| **Grid 不可见** | Grid 是否只作内部登记，未描述成画面里的网格线？ | 渲出真实分格线 / 拼图分屏 |
| **NLVTR 合规** | 尺寸是否写成词形或比较物，没有数值区间？ | 数值区间被门禁判违规并触发回炉 |
