# 🔨 峡谷悬崖大理石天然石龛改造成崖边独居隐居睡眠窝 - 修复版节拍提示词 (10 拍 1:1 精确映射)

## 📌 项目优化规范
1. **严格 1:1 节拍映射**：共 10 拍，精确输出 10 张图，禁止多增/拆分。
2. **基底锁定与增量编辑**：固定第一帧视角、大理石拱门主结构与背景峡谷，后续帧仅做动作增量。
3. **道具生命周期管制**：施工工具仅在 [clearing] ~ [flooring] 存在，在 [furnishing] 强制销毁撤场。
4. **材质防突变**：[flooring] 铺设哑光胡桃木地板后，[reward] 最终揭示帧保持温润木质感，严禁变为湿水高反光镜面。

---

## 🌐 全局基础 Base Prompt
```text
A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic.
```

## 🚫 全局 Negative Prompt
```text
distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality
```

---

## 🎬 10 拍精细提示词清单

### 1. [repair] 清理石龛口悬崖落石与枯藤
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, exterior perspective looking at the entrance of the natural marble cave niche, workers clearing fallen rocks and dried vines from the cliff entrance, clean entrance opening, natural daylight, construction in progress.`
- **Negative**: `distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 2. [framing] 锚固崖边钢结构支撑平台框架
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, exterior perspective, precision heavy-duty black steel framework platform anchored into the cliff edge floor at the cave entrance, clean industrial frame structure, architectural foundation work, sharp focus.`
- **Negative**: `distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 3. [threshold] 推镜进入原始毛坯石龛内部
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective moving inside the spacious raw marble cave, long shot looking towards the end wall, raw textured grey marble arch walls, cleared flat bedrock floor without big boulders, natural ambient light entering from the entrance.`
- **Negative**: `boulders, fallen rocks, distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 4. [clearing] 清运洞内积砂与风化碎石
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, workers clearing residual sand and fine weathered gravel from the cave bedrock floor, industrial dust vacuum equipment, clean base rock surface revealed, dusty air particle effect, warm work light.`
- **Negative**: `boulders, fallen rocks, distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 5. [rough-in] 铺设找平防潮底膜与隐蔽线路
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, flat dark grey moisture-proof insulation membrane seamlessly laid on the cave floor, neatly arranged concealed electrical conduits along the wall edges, precise installation, clean setup.`
- **Negative**: `distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 6. [flooring] 铺设温润胡桃木实木地板
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, installation of premium warm walnut hardwood flooring, matte finish, rich natural wood grain, neatly aligned wooden planks covering the entire cave floor, cozy architecture interior.`
- **Negative**: `wet floor, glossy mirror reflection, distortion, architecture deform, neon light, artificial led strips, extra floating objects, bad geometry, low quality`

### 7. [repair] 打磨大理石晶体缝隙透光墙
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, craftsman polishing the back marble wall, subtle golden warm light glowing from within the natural veins and translucent crystalline fissures of the marble wall, organic glowing cracks, soft aura, craftsmanship.`
- **Negative**: `wet floor, glossy mirror reflection, neon tubes, z-shaped LED, distortion, architecture deform, extra floating objects, bad geometry, low quality`

### 8. [lighting] 预埋晶隙隐形暖色LED柔光灯
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, invisible warm 2700K LED soft illumination embedded inside the natural crystalline fissures of the marble walls and arch ceiling, golden light bleeding naturally through organic rock cracks, elegant hidden lighting design, high-end sanctuary ambience.`
- **Negative**: `neon tubes, z-shaped LED, harsh light strips, wet floor, glossy mirror reflection, distortion, architecture deform, extra floating objects, bad geometry, low quality`

### 9. [furnishing] 布置悬空木质卧榻与羊毛软装
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, cozy custom floating walnut wooden daybed built against the back glowing marble wall, layered with premium thick white wool bedding, plush cushions and knitted throws, clean environment, no construction tools, ultra cozy aesthetic.`
- **Negative**: `tripod, construction equipment, power tools, trash, wet floor, glossy mirror reflection, neon tubes, distortion, architecture deform, extra floating objects, bad geometry, low quality`

### 10. [reward] 工人退出点亮大理石暖光总揭示
- **Prompt**: `A vertical shot of a cliffside natural marble arch cave niche in a canyon, rugged canyon background, arched marble architectural opening, realistic architecture photography, raw luxury aesthetic, 8k resolution, photorealistic, interior perspective, grand reveal master shot, empty room without people, all golden warm ambient lights glowing through marble natural fissures, glowing ceiling veins, warm walnut wooden floor with subtle soft satin sheen, cozy luxury cliffside sleeping sanctuary, dramatic serene atmosphere, masterpiece, cinematic photography.`
- **Negative**: `wet floor, high glossy mirror reflection, water surface, construction tools, human, tripod, power cables, neon tubes, distortion, architecture deform, bad geometry, low quality`
