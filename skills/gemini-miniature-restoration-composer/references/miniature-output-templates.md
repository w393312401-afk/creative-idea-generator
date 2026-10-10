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

## 2. VIDEO 提示词标准模板（纯自然语言多镜头组接）

> 单段 VIDEO 是**纯自然语言叙述的微距多镜头序列**，绝不使用数字切点表或时间戳。完整语法见 `miniature-multishot-language.md`。

```text
Use the provided image as the exact starting composition and environment anchor. Use IMAGE {i} as the actual first-frame image; begin from this initial state and naturally progress the work through the multi-shot sequence without inventing extraneous layouts. A locked macro diorama setup at model eye-level, fifty to eighty-five millimetre macro lens feel with shallow depth of field and creamy background bokeh. The scene opens with a macro working shot holding the locked diorama framing: one oversized human hand is already reaching in from the upper frame edge with a [specific micro-tool, e.g. miniature stainless pointing trowel] and is [concrete repeating micro-action, e.g. drawing craft mortar along the course and setting thumbnail-sized blocks into it one after another], while two tiny resin figurines watch from the foreground corner. The camera then cuts into a tight close-up insert on the tool contact point, where [material physics: mortar squeezing out, glue wetting the grain, clay grit lifting]. Shifting to an extreme close-up insert, the macro view highlights [two persistent craft traces: struck bead, fallen crumbs, glue fillet, sawdust]. Finally, the camera cuts back to a returning macro shot from the exact same locked macro setup as the opening shot, where the remaining repetitions happen smoothly and the hands withdraw clear of the frame before the last moment, leaving [milestone product] clean and stable as this beat's finished state. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of [crisp miniature tapping, stone clinking, adhesive syringe clicks] at sixty percent volume with zero background music.
```

### 记号禁用 (Notation Ban)

正文一律不出现阿拉伯数字、百分号、时间戳与内部缩写：计数写成英文单词（`three roof tiles`, `two figurines`），尺寸写成词形或比较物（`about a thumb tall`、`fifty to eighty-five millimetre`）。严禁任何形式的机器时间码或切点表。

## 3. 负向词库 (Miniature Negative Restraints)

在微缩模式下强制使用的负向安全词库：
`(full-size human room, life-sized building, oversized worker walking inside model, deformed hands, extra fingers, floating detached limbs, blurry hand motion, cartoon CGI render, digital plastic look:1.5)`
