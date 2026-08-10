# Minimal Omni Restoration Example

## User Input

```text
做一个废弃石屋改造成隐藏工作室的 Omni 视频提示词
```

## Expected Output Shape

```text
图片提示词
图片 1:
Generate an image of a locked Gemini Omni restoration anchor showing an abandoned stone hut in a mossy forest clearing, captured like an unpolished smartphone still from casual UGC worksite footage. The framing is slightly off-center with mild phone wide-angle distortion near the tree-line edges, faint sensor noise in the darker doorway, and a small blown-highlight patch where overcast sky catches on wet slate. The camera frames the hut in a wide phone-recorded architectural view with the cracked stone doorway, collapsed roof edge, and moss-covered foreground boulder held as stable landmarks for the whole sequence. The before state shows seven broken roof boards sagging over the front half of the roof, scattered slate fragments, damp leaf piles, and a dark unused doorway, highlighting weathered wood, mold stains, and porous, moss-grown stone textures. No active workers, machines, captions, subtitles, floating labels, or rendered prompt words appear. The lighting features soft diffuse overcast shadows, slight green-gray color cast from damp foliage, and realistic ambient occlusion. Keep this image usable as a stable first frame for adjacent video generation.

图片 2:
Generate an image of the same locked Gemini Omni restoration anchor, preserving the stone hut, forest clearing, doorway, collapsed roof edge, and mossy foreground boulder in the same positions, still captured like a casual smartphone still with the same slight off-center framing, mild edge distortion, faint compression in the darker moss, and overcast sky highlights clipping subtly on wet slate. The progressive state shows the roof debris cleared from the front half of the hut with all seven broken boards gone, slate fragments stacked in a single knee-high pile beside the boulder, and exposed stone edges carrying scrape marks, fine dust boundaries, and fresh pry scars. No active workers, machines, captions, subtitles, floating labels, or rendered prompt words appear. The overcast daylight, phone auto-exposure behavior, green-gray foliage cast, and tactile UGC documentary restoration style remain consistent.

视频提示词
视频 1:
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor; every shot must preserve the same forest clearing, stone hut identity, overcast lighting, and physical layout while showing only the single operation of roof debris clearing. Cut this ten-second clip on these marks and hold no other cuts — a wide working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, an extreme close-up insert from 5.3 to 7.2, and a returning wide shot from 7.2 to 10.0 seconds. The clip opens on a wide working shot captured like casual smartphone footage, slightly off-center with mild edge distortion and phone auto-exposure settling as it matches IMAGE 1, showing the abandoned stone hut surrounded by damp moss and fallen leaves with one lone worker already up the weathered aluminum ladder at the collapsed roof edge, a matte-black steel pry bar biting under the first broken board at zero seconds; that board comes loose in full view from pry-bite to lift-away, and the worker keeps going board by board, arms tensing against each stroke until about five of the seven boards are down, climbing between cycles to stack slate beside the mossy boulder and drop splintered boards into two rigid dark-green crates as dust and splinters settle and the phone briefly hunts focus before locking back onto the roof edge. A clean cut at the three-second mark drops into a close-up insert on the pry bar biting under cracked timber fibers, with minor handheld motion blur and a small blown highlight on wet slate, capturing the raw physics as splinters bend, slate chips slide under gravity, and micro-particles of dust float in the soft air. A second insert at the five-second mark pushes to an extreme close-up insert on the evidence left behind: fresh pry scars along the roof edge and a pale dust boundary around newly exposed stone, with faint compression in the shadowed moss and natural scratches in the grit. A final clean cut at the seven-second mark returns to the same camera setup as the opening wide working shot, matching the exposure and phone-recorded tone of IMAGE 2, where — after the last two boards come loose the same way — the worker keeps prying at the roof edge through the final instant while the cleared front-half roof state, the knee-high slate pile beside the boulder, and the two loaded crates match IMAGE 2. No shot shows or describes entrance, arrival, exit, or a worker-free tail; every shot opens at the progress level the previous shot ended with, and nothing appears in the final frame that is absent from IMAGE 2. Keep the scene free of captions, subtitles, floating labels, UI text, and rendered prompt words. SFX include crowbar creaks, slate clacks, crate scrape, and damp forest wind.

对话微调提示词
编辑 1:
Keep the same IMAGE anchors, forest location, shot order, and roof debris clearing operation, but make the worker's tool impacts feel heavier with tensed shoulder postures and sync each slate clack to the strongest visible hand motion while preserving the same pry scars and dust boundary.

编辑 2:
Keep the same location, final cleared-roof state, and returning wide shot, but make the phone capture feel more like casual UGC footage by increasing the slight horizon tilt, shadow compression, and focus breathing without hiding the roof edge or the physical pry marks.
```

| 审核项 | 状态 | 说明 |
|---|---|---|
| Omni六维场景骨架 | 通过 | 视频段包含镜头、物理动作、光影、风格、地点和无文字渲染策略。 |
| 主镜加特写插入 | 通过 | 视频段按 wide working shot、close-up insert、extreme close-up insert、returning wide shot 组织，末镜写明与主镜同机位，无旧梯景别名。 |
| 相邻锚点绑定 | 通过 | 视频明确绑定 IMAGE 1 为 first-frame anchor，IMAGE 2 为 last-frame anchor。 |
| 单操作Beat | 通过 | 当前视频只执行 roof debris clearing，没有混入涂装、修墙或家具进场。 |
| 施工顺序与工序依赖 | 通过 | 清渣是改造的第一个 beat，符合 拆除→结构→水电→封板→饰面 的依赖链，无硬性否决项。 |
| 累积状态与锚点差异 | 通过 | 图片 2 与图片 1 的差异仅为前半屋顶清渣结果，进度量化为 front half，地标和石板堆按累积状态保留。 |
| 进度流动控制 | 通过 | 第一块板从撬入到取下完整在镜头内发生，主镜推进到七块中的约五块，两个插入镜零推进，最后两块以 the same way 压缩在切回主镜的 cut 上点明，cut 不携带进度，无状态回退。 |
| 可数清单与Reveal零新增 | 通过 | 木板计数为七块且跨镜头一致，石板堆量化为 knee-high pile，两个板条箱可见运出；本例无最终 reveal beat，不涉及新增物体。 |
| 因果痕迹 | 通过 | 保留 fresh pry scars 和 pale dust boundary，均为撬除作业的特征产物。 |
| 物料与登高合理性 | 通过 | 屋顶高处作业全程使用铝梯，梯子按下一拍需要留在场内；石板堆放在巨石旁延续到图片 2，碎木板装箱可见地运出场景。 |
| UGC去AI画面层 | 通过 | 示例使用手机拍摄感、轻微构图偏移、过曝高光、对焦呼吸、噪点和压缩痕迹，而不是精致电影广告画面。 |
| 画面密度与纵深分层 | 通过 | 主镜与切回镜含前景（门框、苔藓巨石）、中景（石屋与作业）、背景（林线）三层，湿石板与苔藓打碎了大块石面；石板堆与巨石为跨锚点冻结的惰性陈设，留在作业区外，未遮挡撬除作业的因果证据。 |
| 多模态引用 | 通过 | 本例未提供图片、视频或音频素材标签，因此不强制引用外部素材。 |
| 文本渲染禁用 | 通过 | 明确禁止 captions、subtitles、floating labels、UI text 和 rendered prompt words。 |
| 对话微调可执行性 | 通过 | 编辑提示保留锚点、镜头顺序和物理结果，只改变动作力度或 UGC 采集瑕疵。 |

## Validation Notes

The sample intentionally uses:
- IMAGE anchors with clean frames.
- One ordinary VIDEO with adjacent anchor binding.
- A ladder for above-reach roof work, held in place through the returning wide shot because the work continues on it.
- Explicit debris logistics: slate persists as a stockpile into IMAGE 2, splintered boards visibly exit in crates.
- A quantified completion extent (front half of the roof cleared).
- Counted elements: seven boards in IMAGE 1, all seven gone in IMAGE 2, two crates tracked throughout.
- First occurrence on camera: the first board pried in full from pry-bite to lift-away.
- Legal compression only: the wide working shot reaches about five of seven boards through visible cycles, and the last two are compressed at the cut back with a stated `the same way` phrase.
- The shot structure: one wide working shot, a close-up insert, an extreme close-up insert, and a returning wide shot from the identical camera setup.
- UGC phone-capture imperfections before cinematic polish.
- No one-take vocabulary.
- No XML.
- No default rendered text.
