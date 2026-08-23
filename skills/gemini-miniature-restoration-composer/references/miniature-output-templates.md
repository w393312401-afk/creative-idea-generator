# Miniature Output Templates & Prompt Grammar — 提示词输出模板与语法

> 规范微缩模型专属的 IMAGE 关键帧与 VIDEO 提示词输出结构。

## 1. IMAGE 提示词标准模板

### 起手锚点帧 (IMAGE 1: Before State)
```text
A static macro diorama eye-level shot (50mm macro lens feel, shallow depth of field with creamy woodland background bokeh) centered on a dilapidated miniature elevated [carrier description, e.g. weathered wooden shack on tree stump base]. The miniature structure shows [specific trauma: splintered basswood slats, broken roof, peeled paint]. Two cast-resin miniature figurines (1:24 dollhouse scale, detailed painted couple) sit on the leaf-strewn compacted dirt ground at foreground left, looking toward the old structure. Dappled natural daylight illuminates the textured tree bark and micro terrain details.
```

### 工序交付帧 (IMAGE N: Milestone Completion)
```text
A static macro diorama eye-level vertical 9:16 tripod shot, centered on the miniature [carrier/structure name]. Macro lens with creamy soft background blur. Inherited landmarks remain locked: [site terrain, tree trunk, background foliage]. Visible milestone completed: [concrete completed craft work, e.g. miniature CMU perimeter walls built to six courses with timber lintels]. Persistent craft traces visible: [micro glue fillets, tiny cement beads, clean cut lines].
```

### 最终揭示帧 (IMAGE Final: Hero Reveal)
```text
A static macro diorama eye-level hero shot, centered on the completed luxury miniature [destiny name, e.g. two-story timber and stone woodland villa diorama]. The miniature home features warm interior LED lights glowing through clear windows, finished terracotta tiled roof, landscaped moss garden with pebble stepping stones. The two tiny resident figurines stand happily on the front porch waving at the camera, while an oversized human hand gently frames the upper roof apex in a warm protective gesture. Soft golden woodland lighting with creamy bokeh.
```

## 2. VIDEO 提示词标准模板（多镜头组接）

> 单段 VIDEO 是**剪辑过的多镜头序列**，不是一镜到底。完整语法（镜头梯、切点表、节奏声明、
> 镜内连续性）在 `miniature-multishot-language.md`；这里是照抄用的骨架。八秒四镜：

```text
Use IMAGE {i} as the first-frame anchor and IMAGE {i+1} as the last-frame anchor; all visible actions must interpolate between these two states. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds. The opening macro working shot holds the locked macro framing, the shallow depth of field, and the diorama terrain exactly as anchored: one oversized human hand is already reaching in from the upper frame edge with a [specific micro-tool, e.g. miniature stainless pointing trowel] and is [concrete repeating micro-action, e.g. drawing craft mortar along the course and setting thumbnail-sized blocks into it one after another], while the two tiny figurines watch from the foreground corner. A clean cut at the two-and-a-half-second mark drops into a close-up insert on the tool contact point, where [material physics: mortar squeezing out, glue wetting the grain, clay grit lifting]. Another clean cut near the four-second mark holds an extreme close-up insert on [two persistent craft traces: struck bead, fallen crumbs, glue fillet, sawdust], with nothing advancing. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot, where the remaining repetitions happen the same way and the hands withdraw clear of the frame before the last moment, leaving [milestone product] clean and stable. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of [crisp miniature tapping, stone clinking, adhesive syringe clicks] over a quiet workshop room tone.
```

揭示拍与兑现拍用三镜版切点表（`... a macro working shot from 0.0 to 3.2, a close-up insert
from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds.`），并且**免除**上面那句
节奏声明。

### 记号禁用与时间码豁免 (Notation Ban)

正文一律不出现阿拉伯数字、百分号与内部缩写：计数写成英文单词（`three roof tiles`），
尺寸写成词形或比较物（`about a thumb tall`、`fifty to eighty-five millimetre`）。

**唯一的例外是切点表那一句**——`Cut this eight-second clip on these marks ...` 里的秒数
必须是阿拉伯数字，否则切点钉不住。除它之外，正文里任何一个裸数字都算违规。

## 3. 负向词库 (Miniature Negative Restraints)

在微缩模式下强制使用的负向安全词库：
`(full-size human room, life-sized building, oversized worker walking inside model, deformed hands, extra fingers, floating detached limbs, blurry hand motion, cartoon CGI render, digital plastic look:1.5)`
