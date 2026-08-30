# Antigravity User Preferences & Custom Rules

## Permissions & Automation Guidelines
- 允许 AI 自动调用和使用所有 MCP 工具（包括 `adspower-local-api`、`n8n-mcp`、`notion-mcp-server` 等所有已注册 MCP 服务），项目内默认完全放行，无需人工审批确认。
- 允许 AI 自动运行 `npm install`、`pip install` 及测试脚本。

## Video Production & OpenMontage Guidelines

1. **Beat-to-Frame Strict Mapping (1:1 节拍映射规则)**:
   - 画面数量与节拍清单必须严格 1 对 1 对应（N 拍 = 精确 N 张关键帧图），严禁擅自拆分、合并或重复生成额外帧。

2. **Visual Consistency & Anchor Lock (视觉连续性与基底锁定)**:
   - 以第一帧作为基准锚点帧（Anchor Frame），锁定基准建筑结构、空间开孔与物理地标。
   - 采用增量编辑（Delta Editing），同一机位内保持透视稳定，跨机位严格按节拍清单的观测机位（`camera_setups`）切换，避免前后帧场景结构无序坍塌或扭曲。

3. **Item Lifecycle & Material Rules (道具生命周期与材质锁死)**:
   - 施工工具（三脚架、测量仪、裸露电缆）仅允许在 [clearing] ~ [flooring] 阶段存在；在 [furnishing] 及以后拍中，正文必须以自然语句显式声明这些工具已彻底撤场（严禁出现三脚架、施工工具、裸露电缆）。
   - 地板铺设完成（[flooring]）后，必须维持温润哑光/半哑光实木质感，严禁在最终揭示帧（[reward]）突变为高反光镜面/湿水面效果 (`wet floor, high glossy mirror reflection`)。
   - 灯光必须约束在天然晶隙隐形透光，禁止产生杂乱的网状或 Z 字形荧光灯带。

4. **Audio Mixing (原声 ASMR 保留与混音)**:
   - 在处理或合成本项目的视频时，**默认不进行静音**。
   - 必须保留并混合视频原声中的 **ASMR 音效**（如铲沙、敲石、木工等细节声），默认音量设为 **60% (`videoVolume: 0.6`)**。
   - 完全去掉背景音乐：背景音乐伴奏（BGM）音量设为 0%（即不进行背景音乐混音，关闭背景音乐伴奏），仅保留旁白人声（100% 音量）与视频原声 ASMR（60% 音量）进行混音。

5. **Stepped Pipeline Execution (分步执行链路)**:
   - 当帧数 ≥ 8 时，默认推荐使用分步管线（`/api/stepped/start`）而非一键合成（`/api/auto_run`）。
   - 锚点帧（Frame 1）必须单独渲染并经人工确认后，方可进入剩余帧的提示词组合阶段。
   - 每批渲染完成后必须自动生成多宫格拼图（5 列固定，单帧宽 240px，行数向上取整）供视觉连续性快检。
   - 每批 ≤ 5 帧，避免单批错误传播范围过大（默认 batch_size=4，可通过 `config.steppedBatchSize` 调整）。
   - 支持三种审核动作：`approve`（通过并继续）、`retry`（重渲当前批次）、`skip`（跳过剩余审核直达最终审查）。

6. **Threshold Monotonic Chronological Inheritance (过门时空单调继承与零状态回退规则)**:
   - 进门首帧 (`IMAGE T+1`) 必须 100% 物理继承室外阶段已完成的所有施工产物（如屋顶木梁板、防水卷材、门槛铺设、已清扫泥地等）。
   - 严禁在 `IMAGE T+1` 中描写已被室外工序修复的破损（如天花板漏水、天花板水泥开裂、地面再次堆满落叶枯木等状态倒退词）。
   - 室内工序直接从室外未完成的下一项工序无缝承接（如室外已清地，则进门直接做碎石/防潮，禁止重复扫地）。

7. **Threshold Carrier Spatial DNA & Asymmetry Lock (过门载体空间真实性与非对称地标锁定)**:
   - 进门首帧 (`IMAGE T+1`) 与所有后续室内帧必须严格继承外景确立的真实物理空间特征：
     - **左右非对称边界**：若外景存在非对称材质（如左混凝土、右乱石+粗大树根盘绕），室内必须分别显式声明左右边界，严禁降级为对称混凝土墙。
     - **屋顶几何真型**：严格遵循外景的梁架走向与坡度（如浅坡/平坡平行木梁 `low-profile shallow-slope parallel rafters`），严禁泛化为高耸双坡人字尖顶或教堂桁架（`A-frame / cathedral / high gable`）。
     - **后墙平整性与去立柱**：后墙必须保持真实开阔平整（带水平水渍线），严禁凭空幻觉出中央水泥立柱（`central pier / column`）。
     - **空间体量尺寸锁（Envelope Signature）**：强制声明室内三维净尺寸（宽、高、深），严禁将浅空间拉伸为深长隧道。

8. **Full-Field Delta Conservation & Multi-Zone Action Rule (全域差量守恒与多工区动作全覆盖准则)**:
   - **空间四域强制扫描 (4-Zone Spatial Scanning)**:
     - 在编写任何相邻关键帧（IMAGE N $\rightarrow$ IMAGE N+1）对应的视频提示词（VIDEO N）前，必须强制扫描四大空间区域的物理变化全集：
       1. **顶域 (Top / Overhead)**：屋顶、天花板、横梁、吊灯、通风口、采光天窗（拆除/新建/修补）。
       2. **中域 (Middle / Facade & Walls)**：墙面、立柱、门窗、开关、管线、挂画（安装/穿线/封闭）。
       3. **底域 (Bottom / Floor & Approach)**：地面、碎石、防潮卷材、地板、地毯、前廊（清运/找平/铺设）。
       4. **边际与物料流向 (Peripherals & Spoil)**：废料堆、工具箱、木料堆、电缆盘（废料堆放/材料消耗）。
   - **100% 动作-工具-音效三位一体映射 (Action-Tool-SFX Triad)**:
     - 只要 IMAGE N+1 相比 IMAGE N 发生了物理差量（$\Delta = \text{IMAGE}_{N+1} - \text{IMAGE}_N$），VIDEO N 中必须**100% 全量分配**对应的工人动作（Action Verb）、具体几何工具（Geometric Tool）以及物理痕迹积累。
     - **零凭空变化（Zero Phantom Changes）**：严禁仅描写单一区域动作（如只写低头铲地）而放任另一区域的结构（如屋顶坍塌物）在无工人干预下凭空消失/融化。
   - **物料与废料守恒 (Material & Spoil Balance)**:
     - 拆除/清理工序必须交代废料去向（如捆扎堆放于左墙、装入黑色硬质箱）；安装工序必须交代原材料堆的递减（如木料堆显著缩减）。
   - **多工区声音同步 (Multi-Zone Sound Synchronization)**:
     - 音效设计（Sound effects）必须覆盖所有活动工区的物理碰撞/撕裂/敲击声（如拆木撬裂声 + 撕膜声 + 铲石摩擦声），严禁声画脱节。

9. **Human-Spatial Metric Conservation & Anti-Cavernous Protocol (人体尺度与空间三维公制守恒准则)**:
   - **公制三维包络硬声明 (Strict Metric Envelope)**:
     - 严禁在提示词中使用模糊或无量纲修饰词（如 `spacious`, `small room`, `waist-deep`）。
     - 必须显式声明公制三维尺寸参数：如地穴/开挖坑 `compact circular excavation pit (diameter 3.2m, depth 1.8m)`，室内空间 `circular subterranean room (diameter 3.0m, strict 2.2m ceiling clearance)`。
   - **道具人机工程学守恒与防空间膨胀 (Ergonomic Prop Allocation)**:
     - 严禁在紧凑型空间（直径/进深 ≤ 3.5m 的地穴、树洞、集装箱）中分配住宅级超大件家具（如 `two-tier bunk bed / double bunk bed` 高低双层床、大转角沙发等），严防 AI 为容纳大件家具而将空间无序拉伸为巨大礼堂（Cavernous hall）。
     - 必须降级为紧凑型人机工程道具：如 `low-profile single timber platform daybed`（单人地台床，高 0.4m）、`recessed wall berth`（内嵌壁龛床）、`compact 80cm-high workbench`。
   - **观测机位与动态视角适配 (Observed Camera Setups & Dynamic Normalization)**:
     - 拍摄角度、机位方位与焦段优先遵循原片反推观测到的多机位矩阵（`camera_setups`，涵盖 bird_eye, high_angle, eye_level, low_angle, worm_eye, dutch_angle 垂直俯仰角与 front, three_quarter, side, rear_three_quarter, back 水平方位，以及 ultra_wide, wide, normal, tele, macro 焦段），按节拍所属机位编号精准呈现。
     - **严禁将俯拍/仰拍/特写强行压平为单一平视**；在无反推机位观测数据的纯原创或兜底场景下，默认采用 `24mm wide-angle lens feel (natural perspective without extreme fisheye distortion)`、胸高视点与 `45%~50%` 地平线基准。
     - 同一机位内部严格锁定透视与地平线基准，跨机位切换时按当前节拍机位句精准执行。
   - **视频人体比例尺注入 (Video Scale Figure Lock)**:
     - 在视频 I2V 提示词中，必须显式声明工人的公制身高与在空间中的相对比例：`a lone male worker (1.78m tall, occupying ~35% of vertical frame height, realistically proportioned to the 2.2m ceiling)`，确保人物在整个生成过程中不忽大忽小、不滑步失真。
   - **负向防膨胀词库 (Negative Scale Restraints)**:
     - 以自然语句约束，严禁出现：洞穴般的大厅（cavernous hall）、超尺寸房间、巨大空旷空间、迷你玩具家具、娃娃屋尺度、长焦压缩畸变。

10. **Two-Shot Decoupled Transition Architecture (全新双镜头解耦过门全局规则体系 · 彻底废除老单镜头直推法)**:
    - **彻底废除老单镜头直推法 (Full Deprecation of Single-Push Pass)**:
      - 严禁使用单镜头从室外一路直推穿过门框进室内的老旧直推法（避免空间拉伸畸变、门框撕裂、曝光骤变与人机施工混杂）。
      - 任何从室外到室内（或主空间到密闭新空间）的转场，**必须全局统一拆解为两个前后紧密咬合的独立节拍（Shot A 室外转场拍 + Shot B 室内承接拍）**。
    - **镜头 A：室外转场拍 · 物理开启与入口特写 (Shot A: Exterior Mechanical Opening & Portal Push-in)**:
      - **构图与运镜**：室外中近景 $\rightarrow$ 特写推进（Push-in forward tracking shot，机高与视角贴合入口，微俯俯角 15°~30° 对准入口/阀门/舱盖）。
      - **显式物理开启机制 (Mechanical Action)**：必须明确描写机械解锁与物理开启过程（如旋转潜艇式金属手轮/密封阀门、拉开厚重木门栓、液压支撑杆顶开舱盖、滑动闸门等），露出通向内部的垂直通道/爬梯/台阶。
      - **终帧 (Image T+1 特写)**：特写敞开的门洞/舱口（Portal Framing），清晰呈现门框、密封圈、铰链及通道首段结构（如垂直爬梯顶部、第一级台阶），天光倾泻入内，深处保持原始毛坯状态。
      - **零施工污染 (Zero Work Contamination)**：Shot A 内部全程无工人、无工具、无材料堆积，纯展现通道开启与机械解耦。
      - **音效设计 (ASMR 60%)**：金属阀门齿轮旋转喀哒声、气压密封泄放声、铰链开启摩擦与沉重金属/木质碰撞声。
    - **镜头 B：室内承接拍 · 工人人机入场与首道工序 (Shot B: Interior Entry, Staging & First Physical Work)**:
      - **构图与机位**：室内固定机位（按室内主 camera_setup 执行，自然透视无鱼眼畸变），正对室内主景深轴线（如远端观景窗或主后墙），入口/爬梯位于画幅侧方（如 Grid B1/B3）。
      - **起帧继承 (Image T+1 室内视角)**：100% 物理继承 Shot A 的敞开状态（上方或侧方入口有自然天光倾泻），空间呈现原始未动工毛坯状态（Raw shell: 未涂装氧化钢板/粗糙水泥/裸露石壁、地表浮尘）。
      - **工人入画与首道工序交付 (Entry + Tool Action)**：
        - 工人（1.78m 高，身着反光背心与安全帽，占画面高度约 35%）真实入画：顺着金属爬梯下行或迈过门槛步入室内，双脚踏上地面。
        - 工人拉入第一道工序所需工具设备（如高压喷枪与软管、清渣铁铲、龙骨水平尺），立即展开并全域交付室内第一道实质性物理工序（如喷涂防腐底漆、清理地面积尘废渣、铺设基础龙骨）。
      - **终帧 (Image T+2 交付成果)**：全域清晰呈现第一道物理工序交付完成的平整状态，为后续木工/地台/软装工序建立物理基底。
      - **音效设计 (ASMR 60%)**：工人靴底踏击钢梯/踩地闷响、空压机运转轰鸣、高压喷枪脉冲喷射声或铁铲清渣碰撞声。

11. **Depth-Layered Spatial Protocol & Anti-Distortion Perspective Lock (五层绝对景深协议与防畸变透视锁 · 彻底终结空间与道具漂移)**:
    - **五层绝对景深提示词架构 (DLSP 5-Layer Depth Staging)**:
      - 任何室内或受限空间首帧（IMAGE T+1 / Shot B 室内承接），提示词必须严格按 5 层物理景深编写：
        1. **机位与视线 (Camera)**: 遵循当前空间 `camera_dna` 或所属 `camera_setup` 设定的机位视线（默认推荐从角落向对角开阔斜拍 3/4 diagonal oblique perspective），严禁在长条空间使用单点对称正灭点（避免拉伸为 15 米保龄球道/火车车厢）。
        2. **近景道具锚点 (Layer 1: Immediate Foreground <1m)**: 入口爬梯、门框或立柱必须显式声明为**镜头前 0.5m 近身物**（如 `Overhead in upper-right ceiling (Grid A3), circular submarine hatch with vertical steel ladder descending through immediate right-foreground (Grid A3-C3) flush to floor`），彻底阻断 AI 将爬梯放置到中景或远端后墙的错误。
        3. **中景开阔通廊 (Layer 2: Midground Staging Floor 1~4m)**: 声明平整开阔的地面主通廊（`expansive, broad floor expanse with weld seams, washed with water caustics, completely open for staging`）。
        4. **侧翼边界拓扑 (Layer 3: Longitudinal Boundaries)**: 必须逐面声明实壁与虚壁（如 `Left wall: two consecutive widescreen rectangular glass windows; Right wall: solid blue corrugated steel wall, zero windows`），严禁泛指导致 AI 脑补成三面玻璃小水箱。
        5. **后景收口与面宽 (Layer 4: Far Background Wall & Metric Envelope >4m)**: 声明真实房间公制三维比例（如 `3.8m wide, 5.5m deep, 2.6m ceiling clearance`），后墙封闭收口。
    - **高危禁忌词与白名单替换 (Banned Perspective Triggers)**:
      - 严禁使用 `corridor`, `tunnel`, `long axis`, `vanishing point`, `one-point perspective`（防止管道拉伸）。
      - 严禁泛指 `panoramic windows`（防止三面水箱盒），必须替换为 `two consecutive widescreen windows exclusively on the left wall`。
    - **负向防畸变词库强注入 (Anti-Distortion Negative Restraints)**:
      - 以自然语句约束，严禁出现：逼仄小屋、方盒子房间、小隔间、2m 见方的小房间、电梯井、三面玻璃盒、玻璃后墙、望不到头的窄隧道、保龄球道效应、火车车厢，以及被摆到背景里/很远处的梯子。

12. **Sandwich Bidirectional Context-Bound Iteration & Cascade Blocker (三明治双向上下文绑定修改律与波及阻断机制 · 彻底终结格局断层与全盘重做)**:
    - **彻底禁止孤岛式修改 (Full Prohibition of Isolated Frame Modifications)**:
      - 在成套帧序列全部生成完毕后的审查与修改阶段，**严禁对任何单帧进行脱离上下文的孤立重写或盲抽重绘**。
      - 孤立修改单帧会导致空间透视、边界几何、光影色温及工序交付物与前后帧断裂（格局断层），引发后向雪崩效应（Cascading Desync），导致后续全部帧序列报废重做。
    - **双向三明治约束架构 (Sandwich Bidirectional Context Triad: K-1 $\leftarrow$ [K] $\rightarrow$ K+1)**:
      - 任何单帧（Frame K）的修改必须同时受到**前向物理继承锚点**与**后向交付目标收口**的双向夹具约束：
        1. **前向物理锚定 (Preceding State Anchor - K-1)**:
           - Frame K 必须 100% 物理继承 Frame K-1 的硬装基底、边界拓扑、材质色泽；若与 Frame K-1 属于同一机位，则透视灭点与机位严格锁定，若为机位切换拍则忠实呈现目标机位。
           - 严禁发生“前序已完成产物在 Frame K 突变、消失或材质倒退”。
        2. **后向承接校验与物理通道锁 (Succeeding Target Boundary Lock - K+1)**:
           - Frame K 的修改成果必须是通往 Frame K+1 的自然且唯一的物理前置条件，确保差量 $\Delta(K \rightarrow K+1)$ 的工人动作与物料变化真实可达。
           - 严禁在 Frame K 植入与 Frame K+1 冲突的新结构、大型永久家具或不可逆材质。
    - **三级差量修改层级 (Graded Modification Protocol)**:
      - **Level 1（局部道具/人物/微瑕修正）**：优先采用局部蒙版重绘 (Mask / Inpainting) 或 ControlNet 局部修复，锁定三维空间背景像素 100% 不动。
      - **Level 2（光照/视角微调）**：必须以 Frame K-1 为图生图 (I2I) 核心基底，绑定深度图/线稿控制，严禁脱离底图使用纯文生图 (T2I) 盲抽。
      - **Level 3（工序重构/结构级变更）**：若 Frame K 必须进行结构级调整，必须立即启动“连带连锁预警”，从 Frame K 开始以 Frame K-1 为基准向后链式同步修正提示词，阻止断层向下游蔓延。
    - **三帧联排硬性审查门禁 (3-Frame Triptych Inspection Gate)**:
      - 单帧修改完成后，**严禁单独审查单张图即判定通过**。
      - 必须强制调取 `[Frame K-1] - [Modified Frame K] - [Frame K+1]` 进行三联屏并排动态比对，重点快检：
        1. **空间与透视**：后墙水渍线、梁架走向、窗洞位置及机位透视是否保持物理连续与机位一致。
        2. **材质与状态**：地板/墙面是否发生湿水镜面化、突变反光或破损复活。
        3. **物料守恒**：工具与材料的出现/消耗是否在三帧之间具备连续的物理因果链。
      - 只有在三联屏比对确认无缝咬合后，方可写入管线并固化为最终帧。

13. **Living Cast Dynamic Reflex & Action-Reaction Triad (活物即时应激与动作-反应三位一体律 · 彻底终结静止假人与动作脱节)**:
    - **活物非静态布景准则 (Living Subjects as Reactive Narrative Actors)**:
      - 在微缩沙盘（Miniature Diorama）、手工工坊或任何含有常驻微缩人偶/动物/常驻住户的画面中，活物是画面唯一的生命体，**严禁作为毫无知觉的静态道具存在**。
      - 严禁在整条序列或单拍内部将人偶写为“保持原样不动”、“站位不变”（`remain`, `stay put`, `static in place`, `unchanged`）。
    - **三段式因果时序咬合链 (Action-Reaction Causal Triad)**:
      - 每一拍内部，活物的生理与视线反应必须与施工主体的物理动作形成**强因果时序咬合**：
        1. **入场与接触应激 (Inception Reflex)**：当工匠手/工具从画幅边缘进入画面或接触工件时，活物必须产生即时的感知应激（如抬头仰望天空、身体惊起、视线迅速转向入画点）。
        2. **作业过程追踪 (Operational Tracking)**：在工具切削、铲土、抹灰、搬运等作业过程中，活物必须呈现微观的视线追踪（Eye tracking）、重心微移（Shift of weight）、探头观察（Leaning in）或抬手指引动作。
        3. **交付成果定格 (Settlement Stance)**：当手/工具完成操作并撤出画面时，活物身体转向并定格在最终成果前进行驻足注视（Settle facing the finished work）。
    - **身份尺度锁定与姿态自由解耦 (Identity Lock vs. Dynamic Posture Decoupling)**:
      - 严格锁定活物的身份、服装色彩与身材比例（如深肤色黑人夫妇、红夹克与蓝裙子、拇指高），**彻底放开姿态与朝向的动态演进**。
    - **负向假人词库强注入 (Anti-Static Negative Restraints)**:
      - 在生成与优化中严禁出现孤立静态人偶，确保视频大模型将活物渲染为具备灵性互动感的场景参与者。

14. **Priority Browser Instances & Quota Reset Date Scheduling Rules (号池优先级浏览器实例与重置日期选号调度规则)**:
    - **优先级浏览器实例（Priority Browser Instances）**:
      - 支持在号池中多选标记优先级浏览器实例（`googleFxPriorityUserIds`，单号点击「⭐/☆」星标，或勾选后批量设置）。
      - 在发起生图与视频序列生成时，系统默认优先仅在勾选的优先级实例集合中调度执行；当且仅当优先级账号全部额度耗尽或进入冷却时，自动平滑降级至号池其余可用账号，保证任务不中断。
    - **多维选号调度策略（Multi-dimensional Scheduling Strategy）**:
      - **🏆 积分最多优先 (`credit_desc`)**（默认）：优先选择当前积分余额最高的账号，保证长序列稳定跑完。
      - **⏳ 重置日期最早优先 (`expiration_asc`)**：优先挑选额度重置日期最近的账号，优先消耗即将重置的当月额度（无重置日期的账号排在之后）。
      - **🔄 均衡轮换 (`rotation`)**：按设置的换号节拍（每 N 个请求）在优先账号集合中依次轮替。
    - **额度重置日期管理规范（Quota Reset Date Management）**:
      - 账号额度重置日期统一使用「**重置日期 / 重置日**」（`expires_at`，格式 `YYYY-MM-DD`），支持单个与批量维护。
      - 严禁在 UI 界面、交互提示、文档或配置中回退使用「到期日/到期时间」等模糊词汇，确保与按月循环重置的额度模型严格一致。

15. **Single Horizontal Ground Baseline & Anti-Isometric Skewing Protocol (单向基准轴线与防菱形旋转协议 · 彻底终结地基斜偏与整楼旋转)**:
    - **正面对齐水平基准线 (Strict Front Horizontal Baseline)**:
      - 建筑主体与室外工序（放线、开挖、地台、主体砌筑、竣工揭示）必须严格保持与画幅底边平行的**水平横向基准线（Horizontal baseline parallel to frame bottom）**，中央门廊垂直对齐画幅中轴线。
      - 严禁将矩形地基、放线灰线或开挖沟槽渲染为 45° 菱形/斜向对角线（Corner-on isometric diamond），严禁在序列中途无故旋转建筑朝向。
    - **俯角透视与正交平面图废除 (High-Angle Perspective vs. Orthographic Map Deprecation)**:
      - 地面工序（放线、挖槽、碎石）默认采用 45°~60° 高角度俯拍，在画面上部保留荒原灌木地平线与立体微缩投影。
      - **要废除的是「正交平面图」这一步，不是俯角本身**：禁止 90° 无透视、无纵深、地平线彻底消失、画面退化成一张施工平面图的正交投影（Orthographic map view）。
      - **允许并鼓励陡俯（75°~85°）**用于轴网放样与沟槽分布——见规则 18 第 2 条。陡俯的合规条件是**仍保留可见的透视收敛**（近大远小、地台边缘不平行），而不是保留地平线；地平线在陡俯下本就不可见，不能拿它当判据。
    - **人物地平线与活动视域基准 (Figurine Ground-Plane Zone & Dynamic Reflex)**:
      - 人偶严格遵循 **Rule 13 (Living Cast Dynamic Reflex & Action-Reaction Triad)** 保持姿态演进与情绪因果咬合。在室外施工全过程，人偶在建筑前景或近侧地面视域内活动，严禁漂移到无支撑的空中或不可见的建筑后方远景。
      - **严禁全序列静态钉死**：严禁在提示词中全序列复读“统一站立左下角注视”（`standing at bottom-left observing`）或无动作的静态词（`static in place, remain standing`）。每一拍必须随着当拍施工动作产生显式的情绪神态演变（忧虑 $\rightarrow$ 惊叹 $\rightarrow$ 欣喜）与具体的身体动作（如低头端详蓝图、弯腰查看放线、抬头仰望屋架与二层阳台、携手漫步花园小径挥手等）。
    - **负向防旋转约束 (Anti-Skewing Negative Restraints)**:
      - 以**自然语句**约束，严禁出现：正交平面图（flat orthographic plan）、垂直航拍地图（vertical aerial map）、俯视蓝图（overhead blueprint view）、无透视收敛的 90° 顶视；以及等距菱形网格（isometric diamond grid）、整体倾斜旋转的布局、矩形地基被转成 45° 斜角对角（corner-on skewed perspective）。
      - ⚠️ **禁止使用权重标注语法**（`(xxx:1.8)`、方括号标签、负向提示词块）写进提示词正文——正文只能是干净的描述性自然语句。这条与 `fast_composer.py` 两个 profile 的规则 8 / 规则 9 是同一条口径；正文里混进权重标签会被一路带到交付提示词与 VLM 鉴别输入里。
      - 本禁令是**全文档统一口径**：本文件其余各条负向约束（规则 1 工具撤场、规则 6 防空旷、规则 9 防逼仄等）一律已改写为自然语句，不得再回退成 `(a, b, c:1.4)` 形态。

16. **Automated Multi-Account Failover & Daily Limit Circuit Breaker Rules (自动切号与单日上限熔断漫游规则 · 彻底终结额度耗尽任务中断)**:
    - **实时异常特征全量捕获 (Comprehensive Failure & Quota Fingerprinting)**:
      - **单日上限与配额耗尽**：必须全量覆盖识别 `reached the daily limit`, `daily limit for`, `daily generation limit`, `try using a different model`, `you have not been charged for this generation`, `quota_exhausted`, `insufficient credits`, `0 credits`, `单日上限`, `单日配额已用完`, `今日生成次数已达上限`, `今日额度已耗尽` 等报错特征。
      - **登录状态失效**：必须实时识别 `google login page`, `sign in to google`, `account_login_required` 等认证过期信号。
      - **出口与风控异常**：识别 `unusual activity`, `help center`, `使用人数过多` 等风控拦截信号。
    - **即时优雅熔断与冷却隔离 (Graceful Circuit Break & Cooldown Isolation)**:
      - **释放进程与锁**：账号一旦命中单日上限、积分耗尽或登录失效，立即调用 AdsPower API 优雅关闭该环境浏览器实例（`close-browser`），释放内存、端口与 profile 文件锁。
      - **持久化冷却标记**：自动将故障账号写入 `runtime/account_pool.json` 记录 24 小时冷却（`cooldown_until`，理由 `quota_exhausted / daily_limit_reached`），彻底阻断后续任务与轮转环再次误选该账号。
    - **无感平滑漫游切号 (Seamless Account Roaming & Priority Fallback)**:
      - **优先级顺位选号**：触发切号时，首选在 `googleFxPriorityUserIds` 优先级账号集合中按选号策略（`credit_desc` / `expiration_asc` / `rotation`）顺位切换至下一个健康账号。
      - **全池平滑降级**：若优先级集合全部耗尽或处于冷却，自动平滑降级至号池其余可用账号，保证自动化任务全程不中断。
      - **干净画布初始化**：换号后自动开辟独立干净画布（Fresh canvas），重置项目 URL 绑定，彻底杜绝跨账号/跨任务画布污染。
    - **用户主动切号/换环境指令执行规约 (User Manual Switch Directive Workflow)**:
      - 当用户发出「换环境」、「换号」、「切号」、「账号限额了」等指令或贴出额度耗尽报错时，AI 必须严格执行以下四步标准化流程：
        1. **关闭故障实例**：调用 AdsPower 本地 API 关闭当前耗尽环境；
        2. **标记冷却持久化**：在号池状态中对故障账号打上冷却标记；
        3. **推选健康新号**：从号池挑选积分充足的健康账号并写入 `server_config.json` 的 `googleFxPriorityUserIds`；
        4. **服务端热加载推送**：调用 `/api/google-fx/config` 接口将新配置推送到运行中的服务端，确保无需重启服务即可立即生效。

17. **Three-Layer Deep Environmental Staging & Destitute Cast Identity Protocol (天地山云三层大景深环境与流浪落魄人偶硬性准则 · 彻底终结虚化断层与光鲜出戏)**:
    - **宏观天地山水真实纵深 (Three-Layer Deep Environmental Staging)**:
      - 微缩沙盘提示词必须显式构建并保留三层宏观环境景深（3-Layer Environmental Depth）：
        1. **远景天际 (Far Background Sky & Horizon)**：真实开阔的自然天空，漂浮细腻自然云层（`open daylight sky with soft drifting clouds`）、远山/丘陵连绵起伏轮廓（`distant rolling hills/mountains on the far natural horizon`）、原生稀疏林木与大气透视（`distant acacia trees/foliage, natural atmospheric depth haze`）；
        2. **中景主体 (Midground Architecture & Craft Site)**：微缩建筑主体结构演进、工序交付成果、微缩施工地台；
        3. **近景地表 (Foreground & Tactile Ground Plane)**：丰富真实的地表矿物纹理（干燥沙土、细碎岩石颗粒、零星杂草丛、地表裂纹、落叶碎片、红树林气生根或河滩湿润泥质水渍）。
      - **严禁虚化断层 (Prohibit Closed Bokeh Wall)**：严禁使用死黑背景、单一平面近身绿幕虚化（`creamy dense bokeh wall cutting off sky`）切断天地纵深与远山云层，必须维持微缩沙盘与宏观真实天地一体化的宏大视效。
    - **流浪难民/落魄灾民破旧做旧服饰与真实受难神态 (Destitute & Impoverished Cast Identity)**:
      - **彻底禁止衣着光鲜 (Zero Beautification Hallucination & Clean Clothes Ban)**:
        - 严禁将初始破旧阶段的受助人偶描述或渲染为“衣着光鲜”、“整洁衬衫/崭新现代服饰”或“度假游客”（`clean royal blue shirt`, `crisp modern clothing`, `brand new floral dress`, `tourists`, `affluent`）；
      - **流浪灾民做旧做脏标准 (Destitute Vagrant / Weathered Refugee Aesthetics)**:
        - **服饰做旧与破损**：必须显式声明为严重磨损、沾满尘土污垢、毛边撕裂、开线打补丁的粗糙粗织麻布或做旧破旧工装（`distressed, dust-caked, grimy, faded, patched, frayed-hem worn-out coarse-weave clothing with realistic dirt smudges`）；
        - **神态与体貌**：身形消瘦、面容憔悴、风尘仆仆、初期眼神流露深切绝望、无助与忧伤（`haggard, weary, impoverished, distressed and helpless sorrowful gaze`）；在全片推进中随着工序展开产生动态希望应激，直至最终揭示帧才展现由衷的喜悦与感恩；
        - **鞋履**：赤脚沾泥或缠裹磨损开胶的破旧草鞋/旧皮凉鞋（`weathered mud-stained worn sandals or bare feet`）；
    - **负向约束 (Negative Restraints)**:
      - 以**自然语句**约束，严禁出现：干净整洁/全新/时髦的现代服饰、游客感、富裕感造型、锃亮皮鞋、精致奢华着装；影棚背景、糊成一片的空白背景、极浅景深，以及任何把地平线切掉的取景。
      - ⚠️ 同规则 15：**不得使用 `(xxx:1.6)` 这类权重标注语法**写进提示词正文。

18. **Dynamic Multi-Angle Cinematography & Perspective Diversity Protocol (多元化拍摄角度与多机位动态调度铁律 · 彻底终结单调平视与角度固化)**:
    - **打破单一平视与视角单调性 (Ban Repetitive Camera Framing)**:
      - 严禁在整条 10~17 拍序列中机械复读同一种平视或正向机位。必须按施工工序物理特征与戏剧高潮，动态调度专业电影级与微缩微距多机位矩阵：
        1. **俯拍/高俯角 (High-Angle / Elevated 3/4 Perspective, 45°~60°)**：用于场地测绘、放线打桩、地基沉箱、底板浇筑与地面整平，清晰展现二维/三维地貌全局作业面，同时在画面上方保留天际山峦地平线。
        2. **鸟瞰俯视 (Bird's-Eye, 75°~85°)**：用于几何草木灰轴网放样、同心圆规划与开挖沟槽分布，呈现工整秩序感与宏观规划感。**必须仍是一台架在高处的真实摄影机**：保留可见的透视收敛与微缩投影，严禁滑到 90° 正交平面图（见规则 15）。此处不要求保留地平线——陡俯下地平线本就不可见。
        3. **低角度仰拍 (Low-Angle Dramatic Hero Perspective, 15°~30° upward looking)**：用于粗壮高脚立柱架设、二层楼面抬升、挑空人字木屋架搭建与封顶，凸显建筑体量的高耸挺拔与抗洪气势，背景映衬开阔天空与漂浮云层。
        4. **微距/特写景别 (Tight Macro Focus / Extreme Close-up, 85-100mm macro feel)**：用于工匠巨手进行精密微雕操作（如黄铜铆钉紧固、榫卯结构咬合、防腐胶打胶、编织竹板细节），直接聚焦微观工艺质感。
        5. **三面/侧翼对角斜透视 (3/4 Diagonal Oblique Perspective)**：用于墙板封闭、外廊悬挑挑台与室外楼梯，展现立体三维进深与侧翼非对称结构。
        6. **全景与史诗大远景 (Wide Hero & Sweeping Horizon, 24-35mm wide feel)**：用于过门后的门厅初见（Beat 13）与终极庄园全貌揭示（Beat 17），宏伟展现天际白云、连绵远山、清澈河道、微缩水豚/鱼群生态与欢庆人偶。
    - **提示词开篇机位声明强制绑定 (Mandatory Cinematography Declaration in Prompt Header)**:
      - 每一张 IMAGE 提示词开篇必须显式声明当拍的具体摄影机位与景别（如 `A dynamic high-angle 3/4 oblique macro shot...`、`A low-angle upward-looking perspective from the riverbank mud...`、`A steep bird's-eye macro survey shot looking down at the site...`（注意：**不要**写 `top-down` / `flat overhead plan`，那是规则 15 负向词库明令禁止的措辞）、`An intimate macro close-up focus on the craftsman's fingers...`），严禁全篇清一色 `A vertical 9:16 macro diorama photograph...`。


