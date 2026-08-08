# Antigravity User Preferences & Custom Rules

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

