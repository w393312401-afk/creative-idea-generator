# Drift Lock Packet Assembly Guide — 微缩沙盘版 (Miniature)

> 本文件是 `gemini-miniature-restoration-composer` 自己的 Drift Lock 包装配指南，
> **取代** base 包的同名文件。
>
> Drift Lock 包（packet）是整条序列的"不许变的东西"清单，在 Phase 1 生成一次，
> 之后每一拍的提示词都拿它做比对基准。base 的那一份把 Camera DNA 定义成
> 「24mm 广角、1.3m 胸高视点」、把施工主体定义成「1.78m 工人 + 反光背心」——
> 包一旦这样装好，后面每一拍都会照着它复述，微缩覆盖段再怎么写也压不过一个
> 逐拍复述的结构化事实。所以这一份必须自带。

字段名与 base 完全一致（下游 `normalize_packet` 与全部门禁按字段名取值），
**变的只是每个字段该填什么**。

---

## 装配顺序

### 1. Camera DNA Block（`camera_dna`）

一句话，锁死整个序列的机位。微缩口径：

```
static macro diorama shot at model eye-level, fifty to eighty-five millimetre macro
lens feel, shallow depth of field with creamy background bokeh, camera fixed on a
tripod just above the tabletop and never moving for the whole sequence
```

- **必须包含**：`macro`、`shallow depth of field`、`model eye-level`（或 `diorama`）。
  门禁 `check_miniature_macro_optics` 做正向校验，缺了会逐拍报错。
- **绝不包含**：`24mm`、`wide-angle`、`chest level`、`eye height`、`1.3m`。
  这些是真实建筑的机位语言，会把模型渲染成真房子。
- 数字写词形（`fifty to eighty-five millimetre`），不写 `50-85mm`——数值区间被
  NLVTR 门禁判违规。

### 2. Geometry Lock（`geometry_lock`）

模型的不变几何。按**厘米或比较物**声明，不按建筑公制：

```
the shell keeps a footprint about the width of a spread hand and stands roughly two
palms tall; the tree-stump base it sits on keeps its exact bark ridges and diameter
throughout
```

禁止 `2.2m ceiling clearance` 这类米制内部尺寸——它会触发真实建筑生成。

### 3. Material Palette Lock（材质基底/状态拆分）

与 base 同构，但词表换成手工艺材质：

- **substrate（基底，永不变）**：`raw basswood`、`cast resin`、`air-dry clay`、
  `printed card stock`、`thin brass rod`、`craft mortar`。逐字复述，不换形容词。
- **state（状态，随工序推进）**：`unsanded` → `sanded smooth` → `primed grey` →
  `painted weathered green` → `sealed with matte varnish`。只允许单调前进。

### 4. Fixed Landmarks & Grid（`primary_landmarks`）

三到五个固定地标，每个带 Grid 单元格与画高占比（占比写词形，如
`about a third of the frame height`）。微缩题材的地标优先取**沙盘基底上的自然物**：

- 背景大树干（Grid A1–A3 之一）
- 基底边缘的大石块或树桩年轮（Grid C1/C3）
- 模型自身的主体轮廓（Grid B2）

**地标必须是不参与施工的东西**。模型上正在被改造的部件不能当地标——它每一拍都在变。

### 5. Frame Boundary Lock

画幅边缘必须始终保留一圈真实沙盘基底（压实泥土、落叶、苔藓、石子）。这圈基底是
缩尺的最终物证，裁掉模型就变成了建筑。在包里写明它占据哪几格（通常 C 行全行）。

### 6. Object Position-State Ledger（`aperture_ledger` / OSPL）

登记模型上每个开口（门洞、窗洞、剖面开口）的位置与当前状态。微缩题材要额外登记
**剖面开口的方向**——它一旦确定，全序列不许换边（见微缩 TBCP 第 4 条）。

### 7. Actor Choreography Ledger（`worker_choreography`）

这一格是 base 与微缩差别最大的地方。**填巨人手，不填工人**：

```
one oversized real human hand enters from the upper frame edge (Grid A2), bare and
unadorned, wielding one named micro-tool per beat; it never appears as a full body,
never stands inside the model, and withdraws from frame before the beat's final moment
```

- **绝不写**：`lone worker`、`male worker`、`safety vest`、`hard hat`、`1.78m`。
  门禁 `check_miniature_actor_violations` 会逐条拦下并触发回炉。
- `worker_scale_percent` 这一格填**手相对模型**的比例，例如
  `the hand spans roughly the height of one storey of the model`——它是本题材的
  核心笑点，必须显式锁住，否则手会被缩小成正常比例、微缩感消失。

### 8. Residents Ledger（微缩专属，写进 `passive_environment`）

两个二十四分之一比例的树脂人偶，固定造型与固定站位：

```
two cast-resin painted figurines, roughly a thumb tall, one in a red jacket and one in
a blue dress, standing on the terrain at the lower-left edge (Grid C1) and watching
the build; they never move between beats unless a beat explicitly restages them
```

人偶是尺度锚，**不是施工主体**——永远不要让它们拿工具干活。

### 9. Lighting Phase Ladder

微缩题材的光相位比实景短：桌面/林间自然光是基调，只在最终揭示拍加入微型 LED 的
暖光。按拍登记每一拍的相位，单调前进不回退。

### 10. New Object Birth Limit

每一拍最多引入一类新材质/新部件。微缩题材尤其容易一拍塞进"墙+屋顶+窗+家具"，
结果每样都只做了一点，观众看不出任何一样完成了。

---

## 装配校验清单

| 字段 | 必须成立 | 常见装配错误 |
| :--- | :--- | :--- |
| `camera_dna` | 含 macro + shallow depth of field + model eye-level | 抄了 base 的 24mm 广角 / 1.3m 视高 |
| `geometry_lock` | 按厘米或比较物声明 | 写成 `2.4m x 6.0m x 2.2m` 建筑公制 |
| `primary_landmarks` | 三到五个，且都不参与施工 | 把正在改造的墙体当地标 |
| `worker_choreography` | 巨人手 + 每拍一件微型工具 | 填成 lone worker / 反光背心 |
| `worker_scale_percent` | 锁手与模型的比例关系 | 留空 → 手被缩成正常比例 |
| `passive_environment` | 两个人偶的造型与站位固定 | 人偶每拍换位置/换衣服 |
| 全字段 | 数字写词形或比较物 | 数值区间触发 NLVTR 门禁 |
