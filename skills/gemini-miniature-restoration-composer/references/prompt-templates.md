# Prompt Templates — 微缩沙盘版 (Miniature Diorama / Giant Hand DIY)

> 本文件是 `gemini-miniature-restoration-composer` 自己的提示词模板范本，**取代**
> base 包的同名文件。
>
> 为什么必须自带一份：`get_cropped_templates` 会按拍型从这份文件里裁出范例，逐拍
> 送进 user message——写手真正照着抄的就是它。base 那一份的每一条范例都是「1.78m
> 工人在 24mm 广角画面里走进真实房间」，抄得越准，微缩契约违得越彻底。
>
> **解析契约**：`get_cropped_templates` 按 `##` / `###` 单行标题切段，用关键词
> 子串匹配定位。下面这些标题的关键词不能改：`IMAGE 1`、`IMAGE 2+`、`Interior IMAGE`、
> `Final IMAGE`、`Anti-Patterns`、`Ordinary Construction VIDEO`、`Threshold Bridge`、
> `Final Reward VIDEO N`、`IMAGE Checklist`、`VIDEO Checklist`。范例正文一律用
> `####` 四级标题（四级不参与切段，会留在正文里）。
>
> **镜头结构**：本文件的每条 VIDEO 范例都是剪辑过的多镜头序列。镜头梯、切点表、节奏声明
> 的完整规范在 `miniature-multishot-language.md`，本文件只负责给出照抄用的成品；两边不一致
> 时以那一份为准。
>
> **所有范例的数字都写成词形或比较物**：NLVTR 门禁禁止数值区间（`10-15cm`、`1:24`），
> 微缩题材满是尺寸，是最容易踩中的一类。照抄范例的写法就不会踩。

---

## IMAGE Templates

### IMAGE 1 — Before/Trauma Anchor (微缩起手帧 · 全锁 + 净帧)

起手帧要一次性立住三件事：**这是个模型**（浅景深 + 沙盘基底）、**它坏在哪**、
**尺度是多少**（人偶在场）。巨人手**不在**这一帧里——它是净帧。

必写要素：微距光学声明 / 载体与破损状态 / 沙盘基底与背景地标 / 两个人偶的起手站位与姿态
（后续每一帧都从这个姿态往下变，见 IMAGE 2+）/
自然光方向。禁止出现任何工具、材料堆、施工痕迹。

#### Exemplar A (林间树桩上的破木屋)

A static macro diorama shot at model eye-level, fifty to eighty-five millimetre macro lens feel, shallow depth of field with creamy woodland bokeh behind. Centred in frame sits a derelict miniature timber shack built on the flat sawn top of a weathered tree stump, its basswood wall slats splintered and silvered, three roof shingles missing so the bare rafters show through, and its tiny door hanging off one hinge. The stump's bark ridges and the compacted leaf-strewn soil around it fill the lower edge of the frame. Two cast-resin painted figurines, each about a thumb tall, stand on the soil at the lower-left corner in a red jacket and a blue dress, looking up at the ruin. Dappled afternoon daylight falls from the upper left, catching the raised grain of the weathered slats. No hands, tools, or materials are in frame.

#### Exemplar B (复古饼干铁盒)

A static macro diorama shot at model eye-level, macro lens feel with shallow depth of field and a softly blurred workshop background. A rusted retro biscuit tin lies on its side at the centre of a pale birch tabletop, its lid prised open and buckled, the printed enamel flaked away to bare speckled metal, and a drift of dried lichen collected inside the open end. The tin's ribbed base and the tabletop's grain hold the lower third of the frame. Two cast-resin figurines about a thumb tall stand at the lower-right edge beside a fallen bottle cap, facing the tin. Cool north-window light rakes across the corrosion from the left. No hands, tools, or materials are in frame.

#### Exemplar C (河畔青苔巨石凹坑)

A static macro diorama shot at model eye-level, macro lens feel, shallow depth of field with a creamy blur of river pebbles behind. A hollowed riverstone sits centred on a bed of dark basalt gravel, its cupped interior choked with dried moss, grit, and a cracked shard of terracotta. The stone's mottled green lichen and the surrounding gravel hold the frame's lower edge. Two cast-resin figurines about a thumb tall stand on the gravel at the lower-left, one in a red jacket and one in a blue dress, looking into the hollow. Overcast daylight from directly above keeps the shadows soft and open. No hands, tools, or materials are in frame.

---

### IMAGE 2+ — Progressive State Anchor (微缩工序交付帧 · 继承 + 净帧)

每一张交付帧都是**手退出画面之后**的静止状态。它要说清三件事：继承了什么、
这一拍完成了什么（完整范围，不是局部）、留下了什么永久痕迹。

必写要素：机位与光学复述（一句）/ 继承地标逐字复述 / 本拍里程碑的**完整**完成态 /
至少两条手工艺痕迹（胶痕、砂浆边、切口毛刺、锯末）/ **人偶的新姿态**。
禁止出现手、工具、材料堆。

人偶锁的是**身份、服装、比例**（同两个小人、同样的红夹克与蓝裙子、同样一个拇指高、
不碰工具、不出画），**不锁姿态**：每一帧它们的朝向、视线、站位都应该跟着这一拍的工序变，
上一帧在看地基、这一帧就该在看墙。写「人偶保持原样」是本包交付过的最典型失败形态
（见 Anti-Patterns）。

#### Exemplar A (椴木骨架搭建完成)

A static macro diorama shot at model eye-level, macro lens feel with shallow depth of field and creamy woodland bokeh. The weathered tree stump, its bark ridges, and the leaf-strewn soil at the lower edge remain exactly as before. On the sawn stump top, a complete basswood post-and-beam frame now stands: eight corner and intermediate posts risen to full height, all four top plates run through and lapped at the corners, and the ridge beam seated along the centre line. Dried glue has squeezed into small amber fillets at every post-to-plate joint, and a fine dusting of pale sawdust has settled in the bark crevices below the cut ends. The two cast-resin figurines have moved along the soil to the stump's foot at lower-left centre, the one in the red jacket now craning up at the ridge beam while the one in the blue dress crouches to sight along the sill line. Dappled afternoon light from the upper left throws the posts' shadows across the stump face.

#### Exemplar B (微缩砖外墙砌筑完成)

A static macro diorama shot at model eye-level, macro lens feel, shallow depth of field with a soft blurred background. The stump base, bark texture, and surrounding soil are unchanged, and the basswood frame from the previous stage stays fully in place. Thumbnail-sized miniature concrete blocks now fill every bay of the frame's lower storey, laid to six even courses on all four walls with the corners properly toothed. Craft mortar has squeezed from the bed joints and been struck flush, leaving a thin raised bead along each course, and pale mortar crumbs have dropped onto the soil at the wall foot. The two figurines have turned from the stump face toward the new block walling, the one in the red jacket a half step ahead with a hand raised toward the toothed corner. Light continues to fall from the upper left.

#### Exemplar C (屋面瓦片铺设完成)

A static macro diorama shot at model eye-level, macro lens feel with shallow depth of field and creamy bokeh behind. Every earlier stage stays intact: stump base, timber frame, and the six courses of miniature block walling. The roof is now fully shingled, with rows of thin terracotta-toned tiles lapped from eave to ridge on both slopes and a capping row bedded along the ridge line. Small beads of clear adhesive have set at the tile laps and catch the light, and a scatter of tile dust lies in the gutter line. The two cast-resin figurines are still at the lower-left on the soil. Dappled daylight rakes across the tile courses from the upper left.

---

### Interior IMAGE — Cutaway Interior Anchor (剖面内装帧 · 微缩题材的"室内")

微缩题材没有室内机位。所谓"室内帧"是**从模型外部透过敞开的剖面**看进去的一张
照片——机位、光学、沙盘基底与外部帧完全一致，变的只是能看见内部了。

必写要素：剖面开口方向（全序列不变）/ 外立面部件的去处 / 内部继承的外部成果
（屋顶内侧、地基）/ 本拍内装里程碑 / 人偶站位。

#### Exemplar A (剖面首次敞开 · 毛坯内部)

A static macro diorama shot at model eye-level, macro lens feel with shallow depth of field. The stump base, timber frame, block walls, and shingled roof are all unchanged. The front facade panel has been lifted clear and now leans against the stump's side at the lower right, exposing the shell's whole interior in one open-front cutaway. Inside, the space is still raw: bare basswood studs on the party wall, an unsanded plank floor grey with settled dust, and no partitions, fittings, or wiring anywhere. The underside of the shingled roof is visible overhead, its rafters and the tile backs sound and dry with no gaps or daylight showing through. The two cast-resin figurines have come right up to the opened front, standing on the soil at the lower-left with both heads tipped back to take in the exposed interior. Daylight from the upper left reaches into the cutaway and falls across the plank floor.

#### Exemplar B (剖面内装推进 · 墙面与隔断)

A static macro diorama shot at model eye-level, macro lens feel, shallow depth of field. Every exterior stage stays as before and the facade panel remains leaning against the stump's side at the lower right; the cutaway still opens from the same front face. Inside, the raw studs are now fully sheathed: thin card panelling covers both interior walls end to end and has been painted a flat chalk white, and a single partition now divides the space into a front room and a rear alcove. Brush strokes are visible where the white paint has pooled slightly at the panel edges, and a fine line of trimmed card offcuts lies along the floor at the wall foot. The plank floor and the roof underside are unchanged. The two figurines have shifted to the cutaway's right edge, the one in the blue dress leaning in past the opening toward the new partition while the other watches from a pace behind.

#### Exemplar C (剖面内装推进 · 家具与灯具，逐件具名)

A static macro diorama shot at model eye-level, macro lens feel with shallow depth of field. All exterior work and the leaning facade panel are unchanged, and the cutaway opens from the same front face. The white-panelled interior is now furnished: a walnut plank bed with a folded felt blanket stands against the rear alcove wall, a two-drawer side cabinet sits beside it, a round table and two spindle chairs occupy the front room, and a woven rag rug covers the plank floor between them. A miniature warm LED lantern is fixed to the ridge above the front room and is lit, throwing a pool of amber light across the table top and up the white panelling. The lantern's wire runs along the ridge and disappears into the wall seam. The two figurines are now sitting together on a pebble at the lower-left, outside the shell, both faces turned up toward the lit lantern inside.

---

### Anti-Patterns — 已交付过的真实失败形态，永远不要复现

- **真人工人混进来**：`one lone worker in a pale shirt places the blocks`。工人是 base
  的世界观；微缩题材的施工主体只有巨人手。门禁会拦，且这条被拦下会烧一轮回炉。
- **建筑广角机位**：`a static twenty-four millimetre wide-angle tripod shot at chest
  level`。这句一写，模型立刻被渲染成真房子。
- **走入式过门**：`the camera pushes through the doorway and settles inside the room`。
  相机不进模型，室内靠剖面。
- **反微缩负向词**：`never reading as a distant miniature`、`(miniature furniture,
  dollhouse scale:1.4)`。这是 base 用来防止真实建筑缩小的措辞，在这里等于压制交付物。
- **数值区间**：`fifty to eighty-five millimetre` 可以，`50-85mm` 不行；`about a thumb
  tall` 可以，`6-8cm tall` 不行。NLVTR 门禁只认后者是违规。
- **局部里程碑**：`a few more blocks have been added to the wall`。巴掌大的东西上
  "又砌了几块砖"根本看不出来。一拍必须完成一个完整范围。
- **手悬在成品帧里**：交付帧是净帧，手必须已经退出画面。
- **人偶拿工具干活**：人偶是尺度锚与住户，不是施工者。
- **人偶整条序列一动不动**（2026-08-23 用户实测反馈）：`The two figurines remain at the
  lower-left` 这一句在旧版模板里出现了十二次，每一帧都写「原样不动」，交付出来的成片里
  两个小人从头到尾是冻住的塑料。它们是画面里唯一的活物。锁身份、服装、比例，**不锁姿态**：
  每一拍给一个跟本拍工序有关的微动作（转身看新墙 / 上前半步 / 蹲下看屋檐底下 /
  一个抬手指另一个仰头 / 坐到石子上）。
- **沙盘基底被裁掉**：画幅边缘失去真实落叶/石子/木纹，缩尺证据消失。
- **痕迹缺席**：没有胶痕、砂浆边、锯末、切口毛刺的完成帧，读起来像 3D 渲染而不是手工。
- **VIDEO 写成一镜到底**（2026-08-23 前的本包旧稿）：`One unbroken take at a steady
  speed`、`continuous miniature craft time-lapse (not real-time)`。现在整条 VIDEO 是剪辑过的
  多镜头序列，这两句都作废，见 `miniature-multishot-language.md`。
- **把"机位不动"读成"不许剪辑"**：`The camera does not move ... throughout` 仍然要写，
  但它约束的是**机位**。同一台机位上剪进特写、再切回来，与它不矛盾。
- **借 omni 的过门梯/兑现梯**：`wide approach shot`、`threshold shot`、`interior wide shot`、
  `pull-back shot`、`final wide shot`。前三个是走入式穿门的镜头名，后两个会换掉机位——
  本包的揭示拍与兑现拍用的是同名的 `macro working shot / close-up insert / returning macro shot`
  三镜梯，只是逐镜职责不同。

---

### Final IMAGE — Reward Tail State (最终揭示帧 · 完工 + 入住)

最终帧是整条序列唯一允许"漂亮"的一帧：灯亮、人偶入住、环境布景到位。它仍然是
净帧——除非巨人手作为**收尾姿态**入画（见下方 Exemplar A），否则手不出现。

必写要素：全部继承项 / 完工态的完整描述 / 灯光 / 人偶从旁观转为入住 / 环境布景 /
可选的手部收尾姿态。

#### Exemplar A (林间木石别墅 · 手作收尾姿态)

A static macro diorama hero shot at model eye-level, macro lens feel with shallow depth of field and a creamy golden woodland bokeh behind. The finished miniature house stands complete on the weathered tree stump: block-and-timber walls, a fully shingled terracotta roof, white-framed windows glazed clear, and the front facade panel refitted flush so the shell reads whole again. Warm LED light glows from every window and from a lantern over the porch. A moss lawn, a pebble stepping path, and two clipped shrubs now dress the soil around the stump base. The two cast-resin figurines stand together on the porch in their red jacket and blue dress, turned toward the camera. One oversized human hand rests lightly at the roof ridge from the upper frame edge, fingers curled in a protective framing gesture without touching the tiles. Low golden light rakes in from the upper left.

#### Exemplar B (铁盒工坊 · 纯净帧)

A static macro diorama hero shot at model eye-level, macro lens feel, shallow depth of field with a softly blurred workshop background. The restored biscuit tin now reads as a complete micro workshop: the buckled lid straightened and repainted deep green, the open end fitted with a glazed shopfront, and the interior visible through it as a full bench, tool wall, and shelf run in stained walnut. A warm LED strip under the shelf lights the bench top. The birch tabletop around the tin is dressed with a coil of brass wire, a spilled tin of tacks, and a scatter of shavings. The two figurines stand inside at the bench, one holding a tiny plane. No hand is in frame. Warm side light from the left picks out the new enamel.

#### Exemplar C (岩石茶室 · 无灯光工序，故无灯光句)

A static macro diorama hero shot at model eye-level, macro lens feel with shallow depth of field and a creamy blur of river gravel behind. The hollowed riverstone now holds a finished timber tea pavilion: cedar posts, a swept shingle roof, a rolled reed blind tied up at one side, and a plank deck cantilevered over the stone's lip. A pebble path runs from the gravel bed up to the deck edge, and two cushions of live moss have been set at the stone's shoulders. The two cast-resin figurines kneel on the deck facing each other across a thumbnail-sized tea tray. Overcast daylight from above keeps the whole diorama evenly lit with soft open shadows.

---

## VIDEO Templates

> 本节的每一条范例都是**剪辑过的多镜头序列**，不是一镜到底。镜头结构、切点表、节奏声明
> 的完整规范在 `miniature-multishot-language.md`，这里只放照抄用的成品。
> 片长按本链路默认的八秒写：普通工序拍四镜，揭示拍与兑现拍三镜。

### Ordinary Construction VIDEO (普通微缩工序拍 · 四镜)

骨架：**锚定开场句 → 切点表 → 主镜（手入画、具名工具、重复循环、推进到四分之三）→
特写插入（工具接触点与材料物理，零推进）→ 第二处特写插入（持久痕迹，零推进）→
切回同一机位（same-way 压缩 + 手退出画幅 + 落到结果帧）→ 节奏声明 → 音效句**。

必写：切点表逐字 / 每镜首句用英文单词复述自己的入点 / 巨人手从哪条边缘伸入 /
**一件**具名微型工具 / 可重复的手部动作循环 / 切回镜写明 the same locked macro setup /
微缩节奏声明 / 痕迹（在插入镜里）/ **人偶的即时应激与动作-反应时序咬合** / 微观音效（无脚步声）。

人偶必须写出**因果时序反应链**（手入画时抬头/应激，工具作业时视线/重心跟随，收工撤出时定格），
绝不能只在结尾贴一句静态站位。它们不碰工具、不出画、比例不变——但一动不动的人偶会把整条片子读成静物幻灯片。

#### Exemplar A (砌微缩砖墙 · 抹刀与镊子)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds. The clip opens on a macro working shot that holds the anchored framing and the stump terrain exactly: one oversized human hand is already reaching in from the upper frame edge with a miniature stainless pointing trowel and is drawing a thin ribbon of craft mortar along the top course, while a second hand steadies the shell from the left; thumbnail-sized blocks are being set into the bed one after another, pressed down and slid against their neighbours, and the walling is climbing across all four sides as the block pile on the soil beside the stump empties. A clean cut at the two-and-a-half-second mark drops into a close-up insert on the trowel tip, where mortar is squeezing out from under a block edge in a fat grey bead and a fingertip is wiping it flush, grains of sand dragging in the wet skin. A second clean cut near the four-second mark holds an extreme close-up insert on a struck joint: the raised bead already stiffening along the course, pale mortar crumbs fallen into the bark crevice below, and nothing advancing while the camera lingers. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot, where the remaining courses come up the same way, the final block is tapped level, and both hands withdraw clear of the frame before the last moment; down on the soil the two cast-resin figurines turn from the stump face toward the finished courses, the one in the red jacket stepping half a pace closer as the wall tops out. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of the trowel scraping mortar, blocks clicking as they seat, and a fingertip tapping one level, over the quiet room tone of the workshop.

#### Exemplar B (铺屋面瓦 · 镊子)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds. In the opening macro working shot the anchored framing and the creamy background blur hold exactly as before: one oversized human hand is already gripping a pair of fine-tip tweezers at the upper right edge, lifting thin terracotta-toned tiles from a small stack on the stump and laying them along the eave line, the other hand cradling the shell steady from below; row after row is climbing the slope toward the ridge as the stack visibly shrinks. A clean cut just past the two-and-a-half-second mark drops into a close-up insert on the tweezer jaws releasing a single tile, the fingertip behind pressing the lap flat until the clay seats with a shiver. Another clean cut around the four-second mark holds an extreme close-up insert on the finished laps, where set beads of clear adhesive glint at the tile edges and a dusting of fired-clay grit has collected in the gutter line. The final cut is a returning macro shot from the same locked macro setup as the opening macro working shot: the second pitch is shingled the same way, a capping row is bedded along the ridge, and the hands lift out of frame before the last moment while the two figurines below tip their heads further and further back to follow the tiles up the slope. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of tweezers clicking on fired clay, tiles ticking as they seat, and the faint rasp of a fingertip smoothing a lap, over a steady quiet room tone.

#### Exemplar C (剖面内墙上色 · 平头刷)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 2.6, a close-up insert from 2.6 to 4.3, an extreme close-up insert from 4.3 to 5.8, and a returning macro shot from 5.8 to 8.0 seconds. The opening macro working shot keeps the camera outside the model in its anchored framing, looking straight into the open-front cutaway: one oversized human hand is already inside the opening with a flat-tipped craft brush loaded with chalk-white paint, drawing long even strokes down the card panelling and working wall by wall from the rear alcove forward, while the other hand turns the shell a few degrees between passes so the far corners can be reached; the grey card is disappearing behind flat white as the paint pot on the tabletop empties. A clean cut at the two-and-a-half-second mark drops into a close-up insert on the bristle edge, where wet white is pooling slightly into the panel seam and a bead of paint is being drawn back out along the join. Another clean cut near the four-second mark holds an extreme close-up insert on the floor at the wall foot: a line of trimmed card offcuts, one dried spatter of white on the plank grain, and no work advancing. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot, where the final wall is covered the same way, the brush is lifted, and the hand withdraws clear of the frame while the interior stands still; the two figurines edge along the soil to the cutaway's mouth, the one in the blue dress leaning in past the opening to see the last white wall. Edited miniature craft time-lapse assembled from multiple macro camera setups, not real-time footage, with oversized human hands entering and withdrawing between passes. Near-field sound of soft bristles dragging on card, the brush tapping the pot rim, and a faint wooden knock as the shell is turned, over the quiet room tone.

---

### Threshold Bridge — 剖面揭示拍 (Cutaway Reveal · 三镜，取代走入式过门)

上游仍可能规划出一个"过门拍"。这一拍**不作废，但整体改写**为剖面揭示：机位不动，
巨人手把外立面板或屋顶整体揭开，露出未动工的内部。它同样是**剪辑过的三镜**，
免除节奏声明。

必写：切点表逐字 / 机位不变声明 / 手取下面板的完整动作 / **面板的去处**（插入镜里交代）/
内部毛坯状态 / 内部继承外部成果（屋顶内侧完好）/ **本拍零施工** / 揭示音效。
禁止：任何 time-lapse 措辞、任何清理/安装/上色、任何 walk-in / step inside 措辞、
任何一镜到底措辞。

#### Exemplar A (取下正立面板)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds. The opening macro working shot keeps the camera outside the model in its anchored framing and never moves: one oversized human hand enters from the upper frame edge, fingertips closing on the top corners of the front facade panel, and lifts it steadily up and out of its slots in one unbroken movement at an even speed. A clean cut at the three-second mark drops into a close-up insert on the panel edge clearing the last slot, then follows the panel down to where it comes to rest, leaning against the stump's side at the lower right where it stays. The final cut is a returning macro shot from the same locked macro setup as the opening macro working shot, with the shell's whole interior now open to view in a single open-front cutaway: bare basswood studs, an unsanded plank floor grey with settled dust, and no partitions, fittings, wiring, tools, or materials anywhere inside. Overhead the underside of the shingled roof reads sound and dry, its rafters and tile backs continuous with no gaps or daylight coming through. Nothing inside is cleaned, cleared, or repaired at any point in the clip, and the hand is out of frame by the last moment. On the soil below, the two figurines take a step back as the panel swings clear of its slots, then turn to face the opened shell. Near-field sound of the panel sliding free of its slots, a soft wooden knock as it settles against the bark, and the quiet room tone of the workshop.

#### Exemplar B (整体提起屋顶)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds. The opening macro working shot holds the locked macro diorama framing outside the model: two oversized human hands enter from the left and right frame edges, fingers spreading beneath the roof's eaves, and raise the entire shingled roof section straight up off the wall plates in one continuous even lift. A clean cut at the three-second mark drops into a close-up insert on a wall plate as the roof leaves it — the dry rasp of release, the plate and the block course beneath it square and untouched — and follows the roof out of frame to the upper left. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot, the interior now open from above: bare stud walls, a dusty plank floor, and an empty unlit space with no partitions, fittings, tools, or stacked materials in it. Nothing inside is touched, cleared, or worked on at any point, and both hands are clear of the frame by the last moment. The two figurines on the gravel tip their heads back to follow the roof up and out of frame, then step in toward the open walls. Near-field sound of shingles ticking against each other as the roof lifts, a dry rasp where the plates release, and the steady quiet room tone.

---

### Final Reward VIDEO N (最终揭示拍 · 三镜)

最后一拍交付的是"住进去了"这件事：灯亮、人偶就位、环境布景铺开。它不是工序拍，
免除节奏声明，但**机位仍然锁死、仍然是剪辑过的三镜**。

必写：切点表逐字 / 机位不变 / 最后一件收尾动作（装回面板 / 点亮灯 / 摆好人偶）/
签名细部插入镜 / 完工全貌 / 人偶从旁观转为入住 / 暖光建立 / 收尾音效。

#### Exemplar A (装回面板并点灯)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds. The opening macro working shot holds the locked macro framing outside the model: one oversized human hand enters from the upper right carrying the same front facade panel that has been leaning against the stump, and slides it back down into its slots until it sits flush and the shell reads whole again, then reaches to the stump base as the warm interior lights come up together. A clean cut at the three-second mark drops into a close-up insert on one glazed window, where the filament of a miniature warm LED swells behind the glass and light spills across the sill onto the porch boards. The final cut is a returning macro shot from the same locked macro setup as the opening macro working shot: a second hand sets the two cast-resin figurines onto the porch deck side by side, turned toward the camera, and both hands withdraw clear of the frame, leaving the finished house complete on the stump with its moss lawn, pebble path, and clipped shrubs, lit warm from within against the cooling woodland light behind. Near-field sound of the panel seating into its slots, a faint click as the light comes on, and the soft settle of the figurines on the deck, over an evening room tone.

#### Exemplar B (点亮工坊灯带)

Use the provided first frame and last frame as exact composition anchors; every visible action must interpolate between those two frame images without inventing a third layout. Cut this eight-second clip on these marks and hold no other cuts — a macro working shot from 0.0 to 3.2, a close-up insert from 3.2 to 5.3, and a returning macro shot from 5.3 to 8.0 seconds. The opening macro working shot keeps the anchored framing and the birch tabletop exactly as before: one oversized human hand enters from the left, thumb and forefinger easing the last shopfront glazing bar into place on the restored tin, then reaching beneath the bench until the warm strip under the shelf comes on and washes the walnut bench top in amber. A clean cut at the three-second mark drops into a close-up insert on the tool wall behind the bench, where the new light picks out a row of thumbnail-sized planes and chisels hung in order and a coil of brass wire catches a highlight. The last cut is a returning macro shot from the same locked macro setup as the opening macro working shot: the other hand sets the two figurines at the bench, one holding a tiny plane, and both hands lift away out of frame, leaving the finished micro workshop reading complete through the glazed front with a scatter of shavings dressing the tabletop outside. Near-field sound of the glazing bar clicking home, the faint tick of the light strip, and a soft tap as the figurines are set down, over the quiet workshop tone.

---

## Fill-In Checklist

### IMAGE Checklist (每一张交付帧交付前逐条过)

- [ ] 机位与光学复述：含 `macro` + `shallow depth of field` + `model eye-level`
      （或 `diorama`）中至少一项，且**不含** `wide-angle` / `chest level` / `24mm`
- [ ] 净帧：画面里没有手、没有工具、没有材料堆（最终揭示帧的收尾手势除外）
- [ ] 无人称词：不出现 worker / builder / person / man / woman / people，
      连"没有工人在场"这种否定句也不写
- [ ] 继承：上一帧已完成的每一项都逐字复述，没有任何一项回退
- [ ] 里程碑完整：本拍的工序完成的是一个**完整范围**（一整面墙 / 一整片屋面），
      不是"又做了一些"
- [ ] 痕迹：至少两条手工艺痕迹（胶痕 / 砂浆边 / 锯末 / 切口毛刺 / 笔触）
- [ ] 尺度锚：巨人手（本帧不在）之外，人偶与沙盘基底至少一个在画且比例未变
- [ ] 人偶是活的：本帧的人偶姿态/朝向/站位与上一帧**不同**，且这个变化跟本拍工序有关；
      身份、服装、比例不变。不出现「人偶保持原样/站位不变」这类写法
- [ ] 沙盘基底：画幅边缘保留真实落叶 / 石子 / 木纹
- [ ] 剖面帧另加：开口方向与前一张一致，外立面部件的去处已交代
- [ ] NLVTR：无 `%`、无数值区间（写词形或比较物）、无内部缩写
- [ ] 无反微缩措辞：不出现 `never reading as a distant miniature`、
      `miniature furniture, dollhouse scale` 这类 base 负向词

### VIDEO Checklist (每一条 VIDEO 交付前逐条过)

- [ ] 锚定开场句：逐字带上"以首末帧为构图锚、动作在两帧之间插值"那一句
- [ ] 切点表：紧跟锚定开场句，逐字带上 `Cut this eight-second clip on these marks ...`
      那一句；秒数与镜头名与本拍的梯一致（工序拍四镜、揭示拍与兑现拍三镜）
- [ ] 镜头名齐全且顺序正确：`macro working shot` → `close-up insert`
      （→ `extreme close-up insert`）→ `returning macro shot`，一个不缺、不多写别的景别
- [ ] 每镜入点复述：每个镜头首句用**英文单词**复述自己的入点
      （A clean cut at the three-second mark ...）
- [ ] 同机位收尾：切回镜写明 `the same locked macro setup as the opening macro working shot`
- [ ] 相机不动：不出现 pan / track / dolly / push in / pull back / crane，
      也不出现相机进入模型的任何写法
- [ ] 无一镜到底措辞：不出现 oner / one-shot / one-take / single continuous take /
      unbroken take，也不再写 `continuous miniature craft time-lapse`
- [ ] 施工主体：明确写出 `oversized human hand` / `giant hand` / `macro fingers`，
      且**不含** `lone worker` / `male worker` / `safety vest` / `hard hat` / `1.78m`
- [ ] 手的进出：主镜零秒就已在作业面（不为入画单开一镜）、切回镜末帧前已退出画幅
- [ ] 工具具名：本拍**一件**具名微型工具（镊子 / 抹刀 / 点胶针 / 刻刀 / 平头刷）
- [ ] 动作循环：用 -ing 动词写出可重复的手部循环，不写"改造在推进"这类空话
- [ ] 镜头级进度锁：推进量只在主镜与切回镜里发生，插入镜零推进；剪辑点上的 same-way
      压缩要在正文里说明
- [ ] 材料收支：材料堆变少、废料变多，至少写一头
- [ ] 节奏声明：普通工序拍带上多镜头版微缩节奏句（`edited miniature craft time-lapse
      assembled from multiple macro camera setups ...`）；揭示拍与最终拍免除
- [ ] 痕迹：这一拍留下的永久痕迹写在**插入镜**里，不再堆到结尾一句
- [ ] 人偶是活的：正文里有一句人偶的微动作，且写的是**从什么姿态动到什么姿态**；
      不出现「人偶保持原样/站位不变」，也不出现人偶碰工具、参与施工、走出画幅。
      沙盘里的动物（树脂小狗/小鸡/小鸟）与人偶同档，同样要动
- [ ] 环境在动：上游给了「常驻运动」（溪水/炉烟/风吹树冠/飘雪）时，正文里它们**仍在动**。
      一条只有手在动、背景全是静止贴图的微距片子，观感是照片上贴了一只会动的手
- [ ] 音效：微观工艺音（刀刃刮擦 / 镊子轻叩 / 石片轻碰 / 点胶针挤压 / 细砂纸摩擦）
      \+ 稳定室内底噪；**绝不写脚步声**；切点本身不配音效
- [ ] 剖面揭示拍另加：面板去处已交代、本拍零施工、无 time-lapse 措辞
- [ ] 无走入式措辞：不出现 walk across the threshold / step inside the room /
      push through the doorway / camera enters into
- [ ] NLVTR：切点表之外无阿拉伯数字，无 `%`、无数值区间、无内部缩写
