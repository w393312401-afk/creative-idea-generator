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
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor; every shot must preserve the same forest clearing, stone hut identity, overcast lighting, and physical layout while showing only the single operation of roof debris clearing. The sequence opens with an establishing long shot captured like casual smartphone footage, slightly off-center with mild edge distortion and phone auto-exposure settling as it matches IMAGE 1, showing the abandoned stone hut surrounded by damp moss and fallen leaves. A clean cut moves to a full shot with handheld phone sway as one worker enters from the left forest path with a weathered aluminum ladder balanced over one shoulder and a matte-black steel pry bar in hand, leaning forward to brace against the load, sets the ladder against the front wall below the collapsed roof edge, and makes a second trip dragging two rigid dark-green crates into place while the camera makes a small corrective tilt to keep the doorway in frame. A match cut moves into a medium shot where the worker, standing on the ladder, pries the first broken board loose in full view from pry-bite to lift-away, then keeps working board by board, tensing their arms with each physical stroke until about five of the seven boards are down, climbing down between cycles to stack slate pieces beside the mossy boulder and drop splintered boards into the crates as fine dust and splinters settle nearby while the phone briefly hunts focus before locking back onto the roof edge. A close-up isolates the pry bar biting under cracked timber fibers, with minor handheld motion blur and a small blown highlight on wet slate, capturing the raw physics as splinters bend, slate chips slide under gravity, and micro-particles of dust float in the soft air. An extreme close-up lingers on the high-detail porous stone textures and evidence left behind: fresh pry scars along the roof edge and a pale dust boundary around newly exposed stone remain visible in IMAGE 2, with faint compression in the shadowed moss and natural scratches in the grit. A final clean cut, after the last two boards come loose the same way, returns to a wide outro shot, matching the exposure and phone-recorded tone of IMAGE 2, where the worker folds the ladder and carries the crates of splintered boards out along the same forest path, temporary tools leave the frame, and the cleared front-half roof state with the knee-high slate pile beside the boulder matches IMAGE 2; every shot opens at the progress level the previous shot ended with, and nothing appears in the final frame that is absent from IMAGE 2. Keep the scene free of captions, subtitles, floating labels, UI text, and rendered prompt words. SFX include crowbar creaks, slate clacks, crate scrape, and damp forest wind.

对话微调提示词
编辑 1:
Keep the same IMAGE anchors, forest location, six-shot order, and roof debris clearing operation, but make the worker's tool impacts feel heavier with tensed shoulder postures and sync each slate clack to the strongest visible hand motion while preserving the same pry scars and dust boundary.

编辑 2:
Keep the same location, final cleared-roof state, and empty wide outro shot, but make the phone capture feel more like casual UGC footage by increasing the slight horizon tilt, shadow compression, and focus breathing without hiding the roof edge or the physical pry marks.
```

| 审核项 | 状态 | 说明 |
|---|---|---|
| Omni六维场景骨架 | 通过 | 视频段包含镜头、物理动作、光影、风格、地点和无文字渲染策略。 |
| 多镜头轮换 | 通过 | 视频段按 establishing long shot、full shot、medium shot、close-up、extreme close-up、wide outro shot 组织。 |
| 相邻锚点绑定 | 通过 | 视频明确绑定 IMAGE 1 为 first-frame anchor，IMAGE 2 为 last-frame anchor。 |
| 单操作Beat | 通过 | 当前视频只执行 roof debris clearing，没有混入涂装、修墙或家具进场。 |
| 施工顺序与工序依赖 | 通过 | 清渣是改造的第一个 beat，符合 拆除→结构→水电→封板→饰面 的依赖链，无硬性否决项。 |
| 累积状态与锚点差异 | 通过 | 图片 2 与图片 1 的差异仅为前半屋顶清渣结果，进度量化为 front half，地标和石板堆按累积状态保留。 |
| 进度流动控制 | 通过 | 第一块板从撬入到取下完整在镜头内发生，中景推进到七块中的约五块，最后两块以 the same way 压缩在收尾 cut 点明，cut 不携带进度，无状态回退。 |
| 可数清单与Reveal零新增 | 通过 | 木板计数为七块且跨镜头一致，石板堆量化为 knee-high pile，两个板条箱可见运出；本例无最终 reveal beat，不涉及新增物体。 |
| 因果痕迹 | 通过 | 保留 fresh pry scars 和 pale dust boundary，均为撬除作业的特征产物。 |
| 物料与登高合理性 | 通过 | 屋顶高处作业使用铝梯并在收尾镜头撤出；石板堆放在巨石旁延续到图片 2，碎木板装箱可见地运出场景。 |
| UGC去AI画面层 | 通过 | 示例使用手机拍摄感、轻微构图偏移、过曝高光、对焦呼吸、噪点和压缩痕迹，而不是精致电影广告画面。 |
| 画面密度与纵深分层 | 通过 | establishing/wide 含前景（门框、苔藓巨石）、中景（石屋与作业）、背景（林线）三层，湿石板与苔藓打碎了大块石面；石板堆与巨石为跨锚点冻结的惰性陈设，留在作业区外，未遮挡撬除作业的因果证据。 |
| 多模态引用 | 通过 | 本例未提供图片、视频或音频素材标签，因此不强制引用外部素材。 |
| 文本渲染禁用 | 通过 | 明确禁止 captions、subtitles、floating labels、UI text 和 rendered prompt words。 |
| 对话微调可执行性 | 通过 | 编辑提示保留锚点、镜头顺序和物理结果，只改变动作力度或 UGC 采集瑕疵。 |

## Validation Notes

The sample intentionally uses:
- IMAGE anchors with clean frames.
- One ordinary VIDEO with adjacent anchor binding.
- A ladder for above-reach roof work, removed before the wide outro shot.
- Explicit debris logistics: slate persists as a stockpile into IMAGE 2, splintered boards visibly exit in crates.
- A quantified completion extent (front half of the roof cleared).
- Counted elements: seven boards in IMAGE 1, all seven gone in IMAGE 2, two crates tracked throughout.
- First occurrence on camera: the first board pried in full from pry-bite to lift-away.
- Legal compression only: the medium shot reaches about five of seven boards through visible cycles, and the last two are compressed at the final cut with a stated `the same way` phrase.
- The six-shot cycle: establishing long shot, full shot, medium shot, close-up, extreme close-up, wide outro shot.
- UGC phone-capture imperfections before cinematic polish.
- No one-take vocabulary.
- No XML.
- No default rendered text.
