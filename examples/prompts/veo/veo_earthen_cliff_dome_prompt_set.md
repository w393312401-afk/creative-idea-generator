# 土崖悬檐改造成地底木构穹顶离网小屋：Veo/Omni 1:1 分段提示词集

## 场景全局规格
- **画幅比例**：竖屏 9:16 (`1080x1920`)
- **镜头焦段与基准**：主镜头锁定 24mm 广角质感（自然透视，严禁鱼眼畸变），相机高度固定在 1.3m（人体胸口视高），地平线锁定在 45%~50% 画面高度。
- **音频混音协议**：保留并混合视频原声中的 ASMR 施工音效（60% 音量 `videoVolume: 0.6`），关闭背景音乐伴奏（BGM 0%），旁白人声 100% 音量。
- **人体比例尺**：`a lone male worker (1.78m tall, occupying ~35% of vertical frame height, realistically proportioned)`。
- **负向防膨胀词库**：`(cavernous hall, oversized room, giant space, miniature furniture, dollhouse scale, telephoto distortion, wet floor, high glossy mirror reflection, tripod, construction tools, power cables in finished room:1.4)`。

---

## 一、 图片提示词（共 18 张关键交付帧）

```text
图片提示词
图片 1:
Generate a photorealistic vertical 9:16 documentary still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, level eye perspective. A natural eroded earthen cliff bluff overhang dominates the upper-middle composition, its cohesive silt-clay underside hanging horizontally above an untouched grassy slope pad. The foreground consists of natural green turf grass with scattered wild patches; the exposed earthen back cut of the bluff is visible in the center, and an open rolling meadow extends toward a distant forest line on the left. The scene shows natural weathering, exposed rootlets along the soil crest, and granular sediment fissures. Natural soft overcast daylight illuminates the terrain evenly, with the horizon sitting steadily at 45% frame height. The site is completely empty of people, machinery, timber, tools, doors, windows, railings, or construction materials. Preserve this exact exterior spatial geometry and rock-earthen boundary as the baseline anchor.

图片 2:
Generate a photorealistic vertical 9:16 documentary still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, level eye perspective. The same earthen cliff overhang, grassy slope foreground, and distant meadow horizon remain rigidly locked in place. Five upright debarked cylindrical softwood log posts (200mm diameter, smooth matte natural pine finish) now stand wedged vertically beneath the earthen soffit under compression bearing. Between the posts, two pre-assembled rectangular softwood window sub-frames with open grid apertures are securely mounted. Visible construction traces include compression soil indentations on the upper clay ledge, pencil layout marks, countersunk wood screws at header joints, and flattened turf blade footprints around post bases. Overcast natural daylight remains unchanged. No workers, ladders, machinery, cladding planks, doors, or loose tools are present.

图片 3:
Generate a photorealistic vertical 9:16 documentary still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, level eye perspective. The earthen bluff overhang, debarked round log posts, window sub-frames, and meadow background hold exact positions. The exterior framing bays are now completely clad in vertical dark brown walnut-stained timber siding planks (20x100mm) with tight tongue-and-groove joints, creating a continuous dark wooden facade wall beneath the overhang. Crisp horizontal rows of countersunk black phosphate wood screws are visible along top and bottom structural headers. The natural round log post profiles remain exposed flanking the bays. Lighting, horizon, and weather conditions remain constant. No workers, ladders, chainsaws, circular openings, door leaves, stone paving, or loose tools are present.

图片 4:
Generate a photorealistic vertical 9:16 documentary still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, level eye perspective. The earthen overhang, dark timber plank facade, log posts, and natural surroundings remain fixed. A perfectly circular hobbit-style doorway opening has been cut through the central facade bay, neatly encased by a satin-finished circular timber trim ring. A hinged circular multi-panel wooden door with curved relief battens and black wrought-iron strap hinges is hung flush in the opening, shown fully closed. Fresh circular chainsaw kerf marks on severed center post stubs, mounting screw holes around the trim ring, and a light dusting of pine sawdust on the grass threshold establish causal construction reality. No workers, active tools, path paving, or lighting fixtures are visible.

图片 5:
Generate a photorealistic vertical 9:16 documentary still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, level eye perspective. All established earthen cliff, dark timber facade, round log columns, and closed circular door geometry remain unchanged. A continuous curved slate flagstone walkway made of 30-50mm thick cleft-finish grey pavers is now laid flush into the excavated ground leading directly to the wooden door threshold. Flanking both edges of the stone path, black polymer solar ground stake lights are inserted into the turf at regular intervals. Tamped trench dirt borders, subtle stone chisel marks, and wheelbarrow tire tracks along the lawn remain visible. Soft daylight and the level horizon remain steady. No workers, shovels, wheelbarrows, interior finishes, or loose materials are present.

图片 6:
Generate a photorealistic vertical 9:16 documentary portal transition still, 24mm wide-angle lens feel, camera height 1.3m, slightly pushed in toward the circular doorway. The circular hobbit timber door is pivoted open outward 90 degrees on its heavy black wrought-iron strap hinges, revealing the threshold opening. Outside natural daylight pours through the doorway, illuminating the cleft slate flagstone threshold that transitions into an unlined, excavated raw earthen floor inside. Through the portal, the rough stratified clay walls and a suspended ceiling fuel lantern of the subterranean main room are clearly visible in raw un-renovated condition. Clean door jamb edges, hinge pivot plates, and threshold seams are sharp. The portal is completely sterile of workers, tools, debris bags, or construction equipment.

图片 7:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, looking forward into the subterranean main room (4.2m width, 2.4m ceiling height). The entrance doorway is behind the camera and out of frame. The room is in its raw excavated state: smooth-troweled cohesive clay domed ceiling with a centrally suspended burning fuel lantern, monolithic compacted earthen rear and side walls with natural horizontal stratigraphy and aggregate inclusions, and a flat bare dirt floor. Daylight from the entrance behind casts a warm grazing quadrilateral across the floor, while lantern light illuminates the vaulted ceiling. The space is completely empty of workers, tools, aggregate, membranes, framing studs, floorboards, or furniture.

图片 8:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the subterranean main room. The clay domed ceiling, lantern, and side earthen walls remain fixed. A uniform 100mm layer of grey angular crushed stone aggregate (15-25mm gravel) is now spread and leveled wall-to-wall across the entire floor. In the center of the rear earth wall, a clean rectangular doorway opening has been excavated through the soil, leading into a dark inner passageway. Horizontal rake tine furrows across the gravel bed, shovel edge scrape lines along the base perimeter, and pickaxe impact marks around the doorway jambs are visible. No workers, wheelbarrows, hoes, pickaxes, membranes, or framing timber are present.

图片 9:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the subterranean main room. The clay ceiling, suspended lantern, side walls, and rear doorway remain fixed. The gravel floor is now completely covered by an unfurled heavy-duty 6-mil black polyethylene vapor barrier membrane, on top of which rests a rigid orthogonal grid of 50x100mm rough-sawn softwood timber sleeper joists. Every rectangular bay of the joist grid is tightly packed with 100mm thick yellow fiberglass glass-wool batt insulation. Taped membrane perimeter seams, countersunk wood screws at joist cross-laps, and scattered yellow insulation fiber tufts establish installation detail. No workers, power tools, floorboards, wall battens, or furniture are visible.

图片 10:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the subterranean main room. The ceiling lantern and rear doorway position remain fixed. The floor is now 100% covered with interlocking dark walnut-stained composite wood floorboards (120mm wide) with a warm satin, non-glossy sheen. Heavy-gauge black polyethylene sheeting is draped and stapled across all clay walls and the vaulted ceiling. Mounted over the black membrane, a complete structural lattice grid of 38x38mm dark-stained timber framing studs lines every wall and arches across the ceiling. Staggered floorboard joints, perimeter expansion gaps, and countersunk black screws at stud junctions remain visible. No workers, ladders, insulation panels, finish cladding, cabinetry, or furniture are present.

图片 11:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the subterranean main room. The dark wood floor and ceiling lantern remain fixed. All wall and ceiling stud cavities have been filled with pink fiberglass insulation, and the entire room envelope is now 100% clad in pale pine tongue-and-groove horizontal and vertical paneling planks (15x90mm) with a natural matte grain. A vertical softwood plank door with horizontal ledgers and black iron hinges is installed in the rear doorway opening, shown closed. Subtle V-groove cladding seams, tiny brad nail heads, and clean corner trim establish completion. No workers, drills, offcuts, cabinetry, sink, table, or chairs have appeared.

图片 12:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the subterranean main room. The pine-paneled walls, vaulted ceiling, dark wood floor, lantern, and closed rear door remain fixed. A complete country-style kitchen run is now installed along the left wall, featuring wood-grain base cabinets, an apron-front white glazed ceramic sink, a gooseneck brass faucet, and open dish shelving above. On the right side, a round solid pine dining table with four square legs and matching wooden chairs is neatly arranged. Faint silicone sealant bead lines along the countertop splash, bracket mounting screws, and subtle shadow definition under furniture legs are visible. The space is clean of tools and construction clutter.

图片 13:
Generate a photorealistic vertical 9:16 documentary portal transition still, 24mm wide-angle lens feel, camera height 1.3m, positioned at the rear doorway of the main room. The rustic pine interior door is pivoted open outward 90 degrees on its black iron hinges. Natural warm interior light spills past the door jamb into the inner subterranean sleeping alcove dome chamber (3.5m diameter, 2.3m dome apex). Inside, the monolithic raw earthen cob dome walls, bare dirt floor, and a circular daylight aperture cut through the far earth wall are visible in unfinished state. Clean door casing edges, black hinge plates, and the threshold floorboard transition line are sharp. The portal is completely sterile of workers, tools, and construction equipment.

图片 14:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, looking into the subterranean sleeping alcove dome chamber. The entrance is behind the camera. The raw sculpted cob dome walls and circular daylight window opening remain fixed. The floor has been excavated, filled with a leveled bed of grey angular crushed stone aggregate, covered with an upturned black polyethylene vapor barrier membrane, and framed with an orthogonal timber sleeper joist grid (50x100mm softwood). Rake marks on the gravel sub-base, staple lines on the upturned membrane skirts, and countersunk wood screws at joist laps establish authentic foundation assembly. No workers, tools, insulation, floorboards, or furnishings are present.

图片 15:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the sleeping alcove dome. The circular daylight opening remains fixed. The timber subfloor joists have been filled with yellow fiberglass insulation and 100% closed with satin-finish oak-grain tongue-and-groove floorboards. Heavy-gauge black polyethylene membrane is draped and stapled across the entire cob dome ceiling and walls, creating a clean black airtight shell above the finished floor. Tight floorboard seams, perimeter staple rows along wall bases, and crinkled black plastic texture on the dome are visible. No workers, ladders, arch ribs, wood cladding, windows, or furniture are present.

图片 16:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the sleeping alcove dome. The finished oak floor, black wall membrane, and circular opening remain fixed. A vaulted framework of radial curved softwood arch ribs (45x90mm) converging at a central ceiling boss disc, reinforced with horizontal blocking battens, is erected across the dome. Every curved ceiling and wall cavity is tightly packed with 100mm unfaced pink fiberglass glass-wool batt insulation. Recessed wood screws at rib intersections, snug insulation seams, and scattered pink fiber wisps on floor edges establish structural execution. No workers, tools, finish slats, glass window unit, bed, or lamps appear.

图片 17:
Generate a photorealistic vertical 9:16 documentary interior still from a locked tripod, 24mm wide-angle lens feel, camera height 1.3m, identical perspective inside the sleeping alcove dome. The oak floor and room geometry remain fixed. The vaulted ceiling dome is now 100% clad in pale pine tongue-and-groove slats (15x90mm) radiating and converging at the central ceiling boss. In the rear opening, a multi-pane circular wood-framed casement window with clear glass and matching rectangular head casing trim is securely installed, framing natural outdoor greenery and daylight. Radial V-groove panel seam lines, countersunk casing screws, and soft diffused light grazing the timber slats establish completion. No workers, tools, bed, lamps, or decor are present.

图片 18:
Generate a photorealistic vertical 9:16 documentary reward still from the completed sleeping alcove dome, 24mm wide-angle lens feel, camera height 1.3m, eye-level perspective. The radial pine-slat vaulted ceiling, circular multi-pane window, and oak floor remain identical. A low solid timber platform bed frame with a white quilted mattress, cream linen duvet, and chunky grey wool knit throw sits centrally in the alcove. Warm-white micro-LED string fairy lights are draped gracefully along the vault arch ribs, glowing warmly. An ivory shearling sheepskin rug rests on the dark oak floor, and a small pine nightstand with a fabric-shade lamp and books stands beside the bed. Soft daylight pours through the circular window, blending with golden ambient fairy lights. Floor reflections are matte and diffused. The room is 100% finished, clean, and free of people, tools, construction debris, or artificial watermarks.
```

---

## 二、 视频提示词（共 17 段 1:1 分步工序）

```text
视频提示词
视频 1:
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor; all visible actions must interpolate between these two states. Maintain the vertical locked tripod view, earthen cliff overhang, and meadow horizon. At the opening instant, a lone male worker (1.78m tall, ~35% frame height) in dark workwear and safety gloves enters carrying round debarked timber posts. In continuous construction time-lapse (not real-time), he measures, hoists, and wedges five 200mm round pine posts vertically under compression beneath the earthen bluff soffit, then secures two prefabricated window grid frames between them with an 18V cordless impact driver. The empty overhang steadily becomes structurally braced while the staged timber stack shrinks and sawdust gathers at post bases. By the final half-second, he carries the driver out of frame, leaving the braced structural frame sterile.
SFX (ASMR 60%): heavy timber thuds, impact driver pulse chatter, grass crunch.
Ambient: outdoor meadow breeze and distant birds.

视频 2:
Use IMAGE 2 as the first-frame anchor and IMAGE 3 as the last-frame anchor; all visible actions must interpolate between these two states. Hold the exact camera position, earthen overhang, round log posts, and meadow background. At the first instant, the worker enters from the lower right with bundles of dark-stained vertical timber cladding planks. Continuous construction time-lapse shows him aligning tongue-and-groove boards against the exterior sub-framing and driving countersunk black phosphate wood screws through top and bottom headers with a cordless driver. The open framing bays disappear row by row beneath an expanding continuous dark wood wall; board stacks diminish as sawdust drifts along the grass footing. By the final half-second, the worker removes all offcuts and tools, leaving the enclosed dark timber facade empty.
SFX (ASMR 60%): timber plank slide clicks, high-torque screw driver whirr, drill bit ratchets.
Ambient: steady coastal meadow breeze.

视频 3:
Use IMAGE 3 as the first-frame anchor and IMAGE 4 as the last-frame anchor; all visible actions must interpolate between these two states. Maintain the locked exterior framing, dark timber wall, log posts, and horizon. At the opening instant, the worker enters wearing eye and ear protection, carrying a gasoline-powered chainsaw, a circular trim ring, and a round multi-panel door leaf. In continuous construction time-lapse, he plunges the chainsaw through the center post and wall boards along a circular guide mark (airborne sawdust spraying outward), clears the cutout plug, pushes the circular wooden trim ring flush into the opening, and hangs the round timber door on heavy black strap hinges. The solid wall transforms into a functioning hobbit portal door. The worker removes the chainsaw and debris before the final frame.
SFX (ASMR 60%): chainsaw two-stroke engine roar, wood fiber tear, ring mallet taps, hinge latch click.
Ambient: outdoor wind rustling grass.

视频 4:
Use IMAGE 4 as the first-frame anchor and IMAGE 5 as the last-frame anchor; all visible actions must interpolate between these two states. Lock the exterior camera, earthen cliff, round hobbit door, and dark facade. At the opening instant, the worker enters with a steel spade, wheelbarrows of grey slate flagstones, and solar stake lights. Continuous construction time-lapse shows him excavating a shallow curved trench through turf in front of the door, dry-laying 30-50mm cleft flagstones into a leveled walkway, and pressing black solar stake lights into the lawn margins. Uneven grass ground becomes an elegant paved stone path while the stone pile empties and tamped earth borders form. The worker carts away the shovel and wheelbarrow before the final frame.
SFX (ASMR 60%): steel spade slicing root-turf, heavy stone clank and gravel scrape, soil tamp thuds.
Ambient: meadow wind and gentle outdoor atmosphere.

视频 5 [过门转场拍 1]:
Use IMAGE 5 as the first-frame anchor and IMAGE 6 as the last-frame anchor; all visible actions must interpolate between these two states. In a smooth, continuous forward push-in tracking shot (24mm wide-angle, camera height gliding from 1.3m to 1.2m), the camera approaches the circular hobbit door along the center axis. The round timber door unlatches and pivots smoothly outward 90 degrees on its black iron hinges. Natural exterior daylight floods inward across the slate threshold, revealing the unlined earthen floor, raw stratified clay walls, and suspended ceiling lantern of the subterranean main room. The camera glides directly to the portal opening. Zero workers, tools, or construction mess are present throughout the shot.
SFX (ASMR 60%): iron door latch clink, heavy timber door hinge creak, gravel footstep crunch.
Ambient: exterior wind fading into deep subterranean room acoustics.

视频 6:
Use IMAGE 7 as the first-frame anchor and IMAGE 8 as the last-frame anchor; all visible actions must interpolate between these two states. Inside the subterranean main room (24mm wide-angle, 1.3m height, locked tripod), the clay ceiling and suspended lantern remain fixed. At the opening instant, the worker enters from the entrance behind the camera with a steel wheelbarrow of crushed stone aggregate and a pickaxe. In continuous construction time-lapse, he dumps and rakes aggregate evenly across the bare dirt floor, then uses the pickaxe to break through the rear clay wall, carving out a clean rectangular doorway into the inner dome chamber. Crushed stone expands wall-to-wall while a doorway opening breaches the rear wall and soil spoil is hauled out. All tools exit before the final frame.
SFX (ASMR 60%): aggregate gravel tumbling from wheelbarrow, rake scraping stone, rhythmic pickaxe striking clay.
Ambient: hollow subterranean echo and burning fuel lantern hum.

视频 7:
Use IMAGE 8 as the first-frame anchor and IMAGE 9 as the last-frame anchor; all visible actions must interpolate between these two states. Maintain the static interior camera, clay ceiling dome, lantern, and rear doorway. At the first instant, the worker enters from behind the camera carrying a roll of black 6-mil vapor barrier, precut 50x100mm timber joists, and bagged yellow fiberglass insulation. Continuous construction time-lapse shows him unrolling and taping the black membrane over gravel, assembling an orthogonal timber joist grid with an impact driver, and friction-fitting 100mm yellow insulation batts into every floor bay. Bare gravel disappears beneath an insulated subfloor framework; insulation bags empty and joist stacks shrink. The worker carries out empty wrappers and tools before the final frame.
SFX (ASMR 60%): heavy plastic membrane crackle, timber joist drops, screw driver bursts, insulation fiber compression.
Ambient: enclosed subterranean room acoustics.

视频 8:
Use IMAGE 9 as the first-frame anchor and IMAGE 10 as the last-frame anchor; all visible actions must interpolate between these two states. Hold the interior camera, clay ceiling, lantern, and rear doorway fixed. At the opening instant, the worker enters carrying dark walnut-stained composite floorboards, black membrane rolls, 38x38mm timber studs, and a step stool. In continuous construction time-lapse, he installs interlocking dark floorboards over the insulated subfloor, drapes black polyethylene membrane across the clay walls and ceiling dome, and fastens a grid lattice of dark timber framing studs over the membrane with long screws. Insulated joists and raw clay vanish beneath finished dark floor and stud-framed black walls. The stool, saw, and driver exit before the final frame.
SFX (ASMR 60%): floorboard tapping block mallet taps, plastic sheeting rustle, high-torque screw driver whirr into studs.
Ambient: muffled underground room tone.

视频 9:
Use IMAGE 10 as the first-frame anchor and IMAGE 11 as the last-frame anchor; all visible actions must interpolate between these two states. Lock the static interior camera, dark wood floor, and ceiling lantern. At the first instant, the worker enters from behind the camera with pink fiberglass insulation batts, bundles of pale pine tongue-and-groove cladding planks (15x90mm), and a rustic softwood door. In continuous construction time-lapse, he presses pink insulation into all wall/ceiling stud bays, nails pine cladding planks horizontally and vertically across walls and ceiling, and hangs the wooden door in the rear opening. Black framed walls transform into a bright, 100% pine-clad room envelope. The worker removes all offcuts, brad nailer, and ladder before the final frame.
SFX (ASMR 60%): fiberglass tearing, pneumatic brad nailer sharp pops, pine tongue-and-groove snapping together, hinge screws.
Ambient: warm, insulated pine room acoustics.

视频 10:
Use IMAGE 11 as the first-frame anchor and IMAGE 12 as the last-frame anchor; all visible actions must interpolate between these two states. Preserve the interior camera, pine-clad walls, dark wood floor, and closed rear door. At the opening instant, two workers enter carrying wood-grain kitchen base cabinets, a white ceramic apron sink, brass faucet, wall shelves, and a round pine dining table set. Continuous construction time-lapse shows them aligning and leveling base cabinets along the left wall, sealing the sink rim, screwing upper dish shelves to wall studs, and assembling the round dining table and four chairs on the right. Empty floor space becomes a fully equipped kitchen and dining living zone. Both workers remove all packaging, spirit level, and drills before the final frame.
SFX (ASMR 60%): cabinet sliding into place, screwdriver ratchet, brass plumbing fittings, chair legs set on timber.
Ambient: cozy domestic room tone and soft lantern flicker.

视频 11 [过门转场拍 2]:
Use IMAGE 12 as the first-frame anchor and IMAGE 13 as the last-frame anchor; all visible actions must interpolate between these two states. In a continuous forward tracking push-in shot (24mm wide-angle, camera height gliding from 1.3m to 1.2m), the camera approaches the rear wooden door of the main room. The door pivots open inward 90 degrees on its black iron hinges. Warm light from the main room pours across the door threshold into the raw earthen cob dome chamber (3.5m diameter, 2.3m apex), revealing the rough monolithic dome ceiling, bare dirt floor, and circular daylight opening. The camera glides directly to the doorway threshold. Zero workers, tools, or construction mess are present throughout the shot.
SFX (ASMR 60%): brass latch click, rustic wood door swing on iron pivots, timber threshold boot step.
Ambient: transition from insulated pine room to hollow earthen dome reverberation.

视频 12:
Use IMAGE 13 as the first-frame anchor and IMAGE 14 as the last-frame anchor; all visible actions must interpolate between these two states. Inside the sleeping alcove dome chamber (24mm wide-angle, 1.3m height, locked tripod), the cob dome and circular window remain fixed. At the first instant, the worker enters from behind the camera carrying buckets of crushed stone aggregate, a garden rake, black membrane, and 50x100mm timber sleepers. In continuous construction time-lapse, he dumps and rakes aggregate into a level bed, lays black vapor barrier with upturned skirts along the walls, and screws together an orthogonal timber sleeper joist grid. Bare dirt floor becomes an insulated-ready sleeper subfloor. All tools and buckets exit before the final frame.
SFX (ASMR 60%): gravel pouring from bucket, metal rake tines scraping aggregate, cordless impact driver fastening joist laps.
Ambient: subterranean dome echo and subtle outdoor breeze through circular opening.

视频 13:
Use IMAGE 14 as the first-frame anchor and IMAGE 15 as the last-frame anchor; all visible actions must interpolate between these two states. Maintain the static interior camera, cob dome walls, and circular window. At the opening instant, the worker enters carrying yellow fiberglass insulation batts, oak tongue-and-groove floorboards, heavy-duty black membrane, and a steel manual staple gun. In continuous construction time-lapse, he packs yellow insulation between floor sleepers, lays interlocking oak floorboards tight, and staples black polyethylene membrane across the entire cob ceiling dome and walls. Open subfloor and raw cob transform into a finished oak deck and an airtight black dome lining. All tools and scrap exit before the final frame.
SFX (ASMR 60%): insulation compression, floorboard mallet taps, heavy staple gun trigger snaps against dome surface.
Ambient: enclosed room tone.

视频 14:
Use IMAGE 15 as the first-frame anchor and IMAGE 16 as the last-frame anchor; all visible actions must interpolate between these two states. Hold the oak floor, black wall membrane, and circular opening fixed. At the first instant, the worker enters carrying radial curved softwood arch ribs (45x90mm), horizontal blocking battens, and pink fiberglass insulation. In continuous construction time-lapse, he fastens curved arch ribs converging at the central ceiling boss, installs horizontal blocking between ribs, and friction-fits 100mm pink insulation batts into every curved dome cavity. Smooth black dome becomes a robust vaulted timber rib skeleton packed wall-to-apex with pink insulation. The ladder, driver, and scraps leave before the final frame.
SFX (ASMR 60%): curved timber rib positioning, impact driver whirring, fiberglass insulation tearing and pressing into bays.
Ambient: muffled dome room acoustics.

视频 15:
Use IMAGE 16 as the first-frame anchor and IMAGE 17 as the last-frame anchor; all visible actions must interpolate between these two states. Lock the static interior camera, oak floor, and circular opening position. At the opening instant, the worker enters with bundles of pale pine tongue-and-groove slats (15x90mm), a multi-pane circular casement window unit, and rectangular casing trim. In continuous construction time-lapse, he fastens pine slats radiating from the perimeter to the central ceiling boss, installs the circular glass window into the rear opening, and screws surrounding casing trim flush. Exposed insulation transforms into an elegant vaulted radial pine ceiling and functioning glass window. The worker cleans all dust and exits before the final frame.
SFX (ASMR 60%): pine slat interlocking clicks, brad nailer pops, window sash hinge screws, glass pane setting.
Ambient: natural outdoor birdsong filtering through glass and quiet timber room tone.

视频 16:
Use IMAGE 17 as the first-frame anchor and IMAGE 18 as the last-frame anchor; all visible actions must interpolate between these two states. Preserve the radial pine dome ceiling, circular window, and oak floor. At the first instant, two workers enter carrying a solid timber platform bed frame, quilted mattress, linen duvet, chunky knit throw, sheepskin rug, bedside table, lamp, and micro-LED fairy light strings. In continuous construction time-lapse, they assemble the platform bed, spread linen bedding and throw, lay the ivory sheepskin rug, drape warm-white LED string lights along ceiling arch ribs, and switch on bedside lighting. Empty wooden dome becomes a fully dressed, illuminated, and cozy bedroom retreat. Both workers remove all packaging and exit before the final frame.
SFX (ASMR 60%): bed frame timber joints clicking, linen and duvet rustle, fairy light wire clips, lamp switch click.
Ambient: serene, warm, cozy ambient room acoustics.

视频 17:
Use IMAGE 18 as the first-frame anchor and IMAGE 18 as the last-frame anchor (static hero showcase). A tranquil, beautifully graded vertical 9:16 locked tripod shot captures the completed subterranean timber dome bedroom pod. Natural soft daylight filters through the circular multi-pane window, illuminating the layered linen duvet and knit throw, while warm-white micro-LED fairy lights trace the radial ceiling ribs with a golden glow. The dark oak floorboards maintain a gentle matte diffuse reflection under the ivory sheepskin rug. No workers, tools, or construction traces exist. The camera holds a perfectly calm, cinematic showcase moment.
SFX (ASMR 60%): soft ambient wind breathing outside window, gentle warm room resonance, delicate firefly-like fairy light hum.
Ambient: peaceful off-grid sanctuary soundscape.
```
