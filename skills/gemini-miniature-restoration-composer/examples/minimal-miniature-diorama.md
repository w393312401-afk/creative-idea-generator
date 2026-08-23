# Minimal Miniature Diorama Construction Example

## User Input

```text
制作一个老树桩微缩林间双层小屋建造的延时提示词，巨人手手工制作
```

## Expected Output Shape

```text
图片提示词
图片 1:
Generate an image of a locked miniature diorama restoration anchor showing a weathered hollow oak tree stump on a tabletop moss bed, captured in macro diorama photography. The camera is locked at model eye-level ten centimetres above the tabletop with fifty to eighty-five millimetres macro lens feel, shallow depth of field, and creamy background bokeh. Two cast-resin miniature figurines in one to twenty-four dollhouse scale sit near the front edge of the moss. The before state shows a hollowed cavity inside the weathered stump with rough bark edges, dry lichen, and bare earth floor. No full-scale human workers, machines, or floating text appear. Lighting is soft warm tabletop workshop lighting with gentle directional sunlight filtering across the moss fibers. Keep this image usable as a stable first frame for adjacent video generation.

图片 2:
Generate an image of the same locked miniature diorama anchor, preserving the weathered tree stump, tabletop moss bed, and tiny resident figurines in identical positions with the same macro shallow depth of field and soft background blur. The progressive state shows the ground floor framed with precision miniature basswood foundation beams, miniature terracotta floor tiles laid in neat mortar courses, and microscopic glue fillets securing the timber edges. No active hands or tools appear. Warm workshop illumination and macro tabletop realism remain completely consistent.

视频提示词
视频 1:
Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds. The opening macro working shot holds the anchored macro diorama framing, the shallow depth of field, and the mossy tabletop exactly as they are: one oversized real human hand is already reaching in from the upper frame margin with fine-tip tweezers, placing miniature basswood floor joists into the stump cavity one after another, while the other hand steadies the stump from the left. A clean cut at the two-and-a-half-second mark drops into a close-up insert on the syringe applicator, where a bead of clear craft glue is swelling at the needle tip and wetting the joint line, the timber grain drinking it dark. Another clean cut near the four-second mark holds an extreme close-up insert on the finished joints: set glue fillets glinting amber at the joist ends and a scatter of pale wood shavings caught in the bark below, with nothing advancing. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot, where miniature terracotta tiles are pressed into a level grid across the rest of the floor the same way, and both hands withdraw clear of the frame before the last moment; on the moss at the lower left the two resin figurines rise from where they were sitting and step in toward the stump, both faces turned up at the new floor. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of delicate tweezer taps, faint tile clicks, the squeeze of the glue syringe, and the quiet steady hum of an indoor workshop.

对话微调提示词
编辑 1:
Keep the same macro tabletop framing and tree stump geometry, but make the giant hand's placement of miniature joists more delicate with steady macro finger control while preserving the shallow depth of field and resin figurines.
```

| 审核项 | 状态 | 说明 |
|---|---|---|
| 施工主体契约 | 通过 | 100% 由画外伸入的超大真人手（Oversized Human Hand / Giant Fingers）与精密微型工具执行，无 1.78m 工人。 |
| 空间尺度与人偶 | 通过 | 明确呈现 1:24 微缩树脂人偶（Tiny Figurines），建立人机微观尺度趣味。 |
| 微距光学与景深 | 通过 | 声明 50-85mm 微距镜头感、浅景深（Shallow DOF）与模型眼平机位（10cm 机高），奶油焦外虚化。 |
| 敞开式剖面立面 | 通过 | 树桩内部工序通过敞开式立面从外部微距拍摄，巨人手直接伸入，无走入式穿门。 |
| 镜头组接 | 通过 | 四镜组接（主镜 → 特写插入 → 二次特写插入 → 切回），切点表钉在秒上，首末两镜同一台锁死微距机位，无一镜到底措辞。 |
| 人偶是活的 | 通过 | 人偶从坐姿起身上前、视线转向新地板；身份、服装与拇指高比例不变，不碰工具、不出画。 |
| 镜头级进度锁 | 通过 | 推进量只在主镜与切回镜；两个插入镜零推进，分别承担材料物理与持久痕迹。 |
| 自然语言记号合规 | 通过 | 除切点表（时间码豁免）外正文数字全量词形化，无百分号，无未掩蔽的数值区间。 |
