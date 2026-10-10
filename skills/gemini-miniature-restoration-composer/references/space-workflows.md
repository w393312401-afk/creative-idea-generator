# Space Workflows — 微缩沙盘工序路由表 (Miniature Composer Quick Reference)

> 本文件是 `gemini-miniature-restoration-composer` 自己的工序路由表，**取代** base 包
> 的同名文件。base 那一份路由的是真实建筑工地的工序（脚手架、市政开挖、水电粗装），
> 微缩沙盘的工序是**手工艺流程**（选料、切割、点胶、打磨、上色、布景），两套词汇
> 几乎不重叠。
>
> 解析契约：`prompt_pipeline.parse_space_workflows` 只读下面那张表——按 `|` 切列，
> 第一列去掉反引号作为 space type，第三列按 `→` 切成 phases。改表时保持四列结构。

## Workflow Routing Table

| Space Type | Default Beats (N) | Standard Workflow Phases | Threshold? |
|---|---|---|---|
| `abandoned property` | 4-7 | 基底清整 → 骨架搭建 → 外墙砌筑 → 屋面铺设 → 剖面内装 → 灯光布线 → 环境布景 | Standard |
| `miniature dwelling` | 4-7 | site prep on the base → basswood frame assembly → miniature masonry walls → roof shingling → cutaway interior fit-out → micro LED wiring → terrain landscaping | Standard |
| `natural carrier shell` | 4-6 | hollowing and cleaning the shell → internal frame fitting → floor and stair build → window and door joinery → interior furnishing → moss and pebble dressing | Standard |
| `tin or vessel shell` | 3-6 | de-rusting and cutting the opening → internal partition build → surface priming and painting → fixture and furniture install → lighting and dressing | Standard |
| `treehouse / elevated build` | 4-7 | trunk base preparation → platform and stilt assembly → cabin shell build → roof and balcony → rope ladder and railing → interior fit-out → foliage dressing | Standard |
| `garden / terrain diorama` | 3-5 | terrain sculpting → hardscape paving → water feature build → planting and moss dressing → micro furniture staging | Standard |
| `workshop / interior room box` | 3-5 | room box shell build → wall and floor finishing → workbench and shelving install → tool and prop dressing → task lighting | Standard |
| `custom build object` | 3-4 | raw stock prep → carving and assembly → finishing and painting → detail dressing | Standard |

> **`Threshold?` 一列全部为 `Standard`**：微缩题材不存在走入式过门，室内一律靠剖面
> 揭示。若上游仍规划出过门拍，按 `threshold-bridge-consistency-protocol.md`（微缩版）
> 把它整体改写为剖面揭示拍，而不是照 base 的双镜头穿门法执行。
>
> 注：该列当前仅作文档声明——运行时 `parse_space_workflows` 会解析它，但节拍规划层
> 读的是 brief 里的 `threshold_variant`，不读这一列。改这一列不会改变是否规划过门拍。

## Beat Derivation Rules

**这张表是 `N` 的唯一权威。** 读取对应 space type 的 `Default Beats (N)`，再按下面
调整：

- **载体越小，拍数越少**：一个巴掌大的铁盒改造给不出七道有辨识度的工序，硬凑会
  让相邻拍的差别小到看不出来。铁盒/葫芦/椰壳这类取下限，树桩/岩台/树屋取上限。
- **每一拍必须有一个肉眼可辨的里程碑**：微缩题材的"可辨"标准比实景更严——观众
  是在看一个巴掌大的东西，"补了三块砖"根本看不出来。一拍要么完成一整面墙、
  一整片屋面、一整套楼梯，要么就并进相邻拍。
- **剖面内装单独成拍**：外壳完成到内装之间必须有一拍专门做剖面揭示（见微缩 TBCP），
  不要把"揭开 + 装修"压进同一拍。
- **最终揭示拍固定占一拍**：完工全景 + 人偶入住 + 灯光点亮，不与最后一道工序合并。

## Construction Phase → Visual Signature Map

微缩工序的可见特征词表。写 IMAGE 里程碑时从这里取"完成态"的说法，写 VIDEO 时
取"进行态"的动作。

| 工序阶段 | 完成态可见特征 (IMAGE) | 进行态动作 (VIDEO) |
| :--- | :--- | :--- |
| 基底清整 | 压实的泥土表面平整，碎叶石子被扫到边缘堆成一圈 | 指尖用微型刮板把浮土推平，镊子夹走碎叶 |
| 骨架搭建 | 椴木立柱等距立起，顶部横梁贯通，接头处有干透的胶痕 | 手指扶正立柱，另一只手用点胶针在榫口挤出一滴胶 |
| 外墙砌筑 | 微缩砖逐层砌到指定高度，砖缝里有挤出的砂浆边 | 微型抹刀刮一层砂浆，指尖逐块按压砖块并抹掉溢浆 |
| 屋面铺设 | 瓦片/木片自下而上叠压铺满，屋脊压顶完成 | 镊子夹起瓦片逐排叠放，指腹压实每一排的搭接 |
| 剖面内装 | 内墙面平整上色，地板铺满，隔断到位 | 手伸进剖面开口安放隔断板，刷子在内墙面刷底色 |
| 灯光布线 | 微型 LED 嵌入天花/墙面，导线藏进墙体，灯亮 | 镊子把 LED 塞进预留孔，指尖把导线压进墙缝 |
| 环境布景 | 苔藓、碎石步道、微型植栽沿基底铺开 | 指尖撒下苔藓碎屑，镊子逐颗摆放碎石 |
| 家具陈设 | 微型家具各就各位，人偶入座 | 镊子夹起家具逐件放入剖面，手指微调角度 |
