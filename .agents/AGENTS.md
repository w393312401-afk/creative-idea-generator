# Antigravity User Preferences & Custom Rules

## Permissions & Automation Guidelines
- 允许 AI 自动调用和使用所有 MCP 工具（包括 `adspower-local-api`、`n8n-mcp`、`notion-mcp-server` 等所有已注册 MCP 服务），项目内默认完全放行，无需人工审批确认。
- 允许 AI 自动运行 `npm install`、`pip install` 及测试脚本。

## Video Production & OpenMontage Guidelines

1. **Beat-to-Frame Strict Mapping (1:1 节拍映射规则)**:
   - 画面数量与节拍清单必须严格 1 对 1 对应（N 拍 = 精确 N 张关键帧图），严禁擅自拆分、合并或重复生成额外帧。

2. **Visual Consistency & Anchor Lock (视觉连续性与基底锁定)**:
   - 以第一帧作为基准锚点帧（Anchor Frame），锁定视角焦段、洞口构图与山体拱门结构。
   - 采用增量编辑（Delta Editing），避免前后帧场景重构或结构扭曲。

3. **Item Lifecycle & Material Rules (道具生命周期与材质锁死)**:
   - 施工工具（三脚架、测量仪、裸露电缆）仅允许在 [clearing] ~ [flooring] 阶段存在，在 [furnishing] 及以后拍中必须在负向提示词添加 `(tripod, construction tools, power cables:1.4)` 强制销毁撤场。
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
   - **镜头焦段与地平线基准统一 (Camera Normalization)**:
     - 全片主镜头统一锁定为 `24mm wide-angle lens feel (natural perspective without extreme fisheye distortion)`，相机高度固定在 `1.3m (human chest level)`，地平线锁定在 `45%~50%` 画面高度。
   - **视频人体比例尺注入 (Video Scale Figure Lock)**:
     - 在视频 I2V 提示词中，必须显式声明工人的公制身高与在空间中的相对比例：`a lone male worker (1.78m tall, occupying ~35% of vertical frame height, realistically proportioned to the 2.2m ceiling)`，确保人物在整个生成过程中不忽大忽小、不滑步失真。
   - **负向防膨胀词库 (Negative Scale Restraints)**:
     - 在图像与视频提示词中强制注入：`(cavernous hall, oversized room, giant space, miniature furniture, dollhouse scale, telephoto distortion:1.4)`。

10. **Two-Shot Decoupled Transition Architecture (全新双镜头解耦过门全局规则体系 · 彻底废除老单镜头直推法)**:
    - **彻底废除老单镜头直推法 (Full Deprecation of Single-Push Pass)**:
      - 严禁使用单镜头从室外一路直推穿过门框进室内的老旧直推法（避免空间拉伸畸变、门框撕裂、曝光骤变与人机施工混杂）。
      - 任何从室外到室内（或主空间到密闭新空间）的转场，**必须全局统一拆解为两个前后紧密咬合的独立节拍（Shot A 室外转场拍 + Shot B 室内承接拍）**。
    - **镜头 A：室外转场拍 · 物理开启与入口特写 (Shot A: Exterior Mechanical Opening & Portal Push-in)**:
      - **构图与运镜**：室外中近景 $\rightarrow$ 特写推进（Push-in forward tracking shot，24mm 广角，机高 1.5m 滑动至 1.2m，微俯俯角 15°~30° 对准入口/阀门/舱盖）。
      - **显式物理开启机制 (Mechanical Action)**：必须明确描写机械解锁与物理开启过程（如旋转潜艇式金属手轮/密封阀门、拉开厚重木门栓、液压支撑杆顶开舱盖、滑动闸门等），露出通向内部的垂直通道/爬梯/台阶。
      - **终帧 (Image T+1 特写)**：特写敞开的门洞/舱口（Portal Framing），清晰呈现门框、密封圈、铰链及通道首段结构（如垂直爬梯顶部、第一级台阶），天光倾泻入内，深处保持原始毛坯状态。
      - **零施工污染 (Zero Work Contamination)**：Shot A 内部全程无工人、无工具、无材料堆积，纯展现通道开启与机械解耦。
      - **音效设计 (ASMR 60%)**：金属阀门齿轮旋转喀哒声、气压密封泄放声、铰链开启摩擦与沉重金属/木质碰撞声。
    - **镜头 B：室内承接拍 · 工人人机入场与首道工序 (Shot B: Interior Entry, Staging & First Physical Work)**:
      - **构图与机位**：室内全景固定三脚架机位（24mm 广角，1.3m 视高，平视，严禁鱼眼畸变），正对室内主景深轴线（如远端观景窗或主后墙），入口/爬梯位于画幅侧方（如 Grid B1/B3）。
      - **起帧继承 (Image T+1 室内视角)**：100% 物理继承 Shot A 的敞开状态（上方或侧方入口有自然天光倾泻），空间呈现原始未动工毛坯状态（Raw shell: 未涂装氧化钢板/粗糙水泥/裸露石壁、地表浮尘）。
      - **工人入画与首道工序交付 (Entry + Tool Action)**：
        - 工人（1.78m 高，身着反光背心与安全帽，占画面高度约 35%）真实入画：顺着金属爬梯下行或迈过门槛步入室内，双脚踏上地面。
        - 工人拉入第一道工序所需工具设备（如高压喷枪与软管、清渣铁铲、龙骨水平尺），立即展开并全域交付室内第一道实质性物理工序（如喷涂防腐底漆、清理地面积尘废渣、铺设基础龙骨）。
      - **终帧 (Image T+2 交付成果)**：全域清晰呈现第一道物理工序交付完成的平整状态，为后续木工/地台/软装工序建立物理基底。
      - **音效设计 (ASMR 60%)**：工人靴底踏击钢梯/踩地闷响、空压机运转轰鸣、高压喷枪脉冲喷射声或铁铲清渣碰撞声。

11. **Depth-Layered Spatial Protocol & Anti-Distortion Perspective Lock (五层绝对景深协议与防畸变透视锁 · 彻底终结空间与道具漂移)**:
    - **五层绝对景深提示词架构 (DLSP 5-Layer Depth Staging)**:
      - 任何室内或受限空间首帧（IMAGE T+1 / Shot B 室内承接），提示词必须严格按 5 层物理景深编写：
        1. **机位与视线 (Camera)**: 锁定 `24mm wide-angle interior shot at 1.3m eye-level, wide 3/4 diagonal oblique perspective from near corner`（从角落向对角开阔斜拍），严禁在长条空间使用单点对称正灭点（避免拉伸为 15 米保龄球道/火车车厢）。
        2. **近景道具锚点 (Layer 1: Immediate Foreground <1m)**: 入口爬梯、门框或立柱必须显式声明为**镜头前 0.5m 近身物**（如 `Overhead in upper-right ceiling (Grid A3), circular submarine hatch with vertical steel ladder descending through immediate right-foreground (Grid A3-C3) flush to floor`），彻底阻断 AI 将爬梯放置到中景或远端后墙的错误。
        3. **中景开阔通廊 (Layer 2: Midground Staging Floor 1~4m)**: 声明平整开阔的地面主通廊（`expansive, broad floor expanse with weld seams, washed with water caustics, completely open for staging`）。
        4. **侧翼边界拓扑 (Layer 3: Longitudinal Boundaries)**: 必须逐面声明实壁与虚壁（如 `Left wall: two consecutive widescreen rectangular glass windows; Right wall: solid blue corrugated steel wall, zero windows`），严禁泛指导致 AI 脑补成三面玻璃小水箱。
        5. **后景收口与面宽 (Layer 4: Far Background Wall & Metric Envelope >4m)**: 声明真实房间公制三维比例（如 `3.8m wide, 5.5m deep, 2.6m ceiling clearance`），后墙封闭收口。
    - **高危禁忌词与白名单替换 (Banned Perspective Triggers)**:
      - 严禁使用 `corridor`, `tunnel`, `long axis`, `vanishing point`, `one-point perspective`（防止管道拉伸）。
      - 严禁泛指 `panoramic windows`（防止三面水箱盒），必须替换为 `two consecutive widescreen windows exclusively on the left wall`。
    - **负向防畸变词库强注入 (Anti-Distortion Negative Restraints)**:
      - 在图像与视频提示词中强制注入：`(cramped room, square box room, tiny cubicle, 2m small room, elevator shaft, three-sided glass box, glass back wall, endless narrow tunnel, bowling alley effect, train carriage, ladder placed in background, ladder far away:1.6)`。
