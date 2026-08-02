# Omni Scene Skeleton

Use this six-dimensional skeleton for every Gemini Omni restoration VIDEO prompt. The final prompt must read as natural prose, not a checklist.

## 1. Framing And Motion

Use professional shot-scale terms, but make the capture style feel like casual UGC phone footage by default. The default is edited multi-shot coverage with clean cuts or match cuts, recorded as if different usable moments were captured on a phone or small consumer camera.

Required shot-scale vocabulary:
- establishing long shot
- full shot
- medium shot
- close-up
- extreme close-up
- wide outro shot

Default phone-capture terms:
- shot on a recent smartphone rear camera
- phone camera wide-angle distortion near frame edges
- vertical phone footage or casual horizontal phone footage
- slightly tilted handheld framing
- minor off-center composition
- small hand correction during re-framing
- autofocus breathing or brief focus hunting
- phone auto-exposure pumping
- rolling-shutter wobble during quick movement
- mild motion blur from handheld recording

Optional cinematic terms, only when useful or requested:
- shot on 35mm film / vintage 16mm look
- shallow depth of field / cinematic bokeh
- slow push in
- lateral tracking move
- overhead insert
- low-angle insert
- rack focus / lens breathing

Do not use one-take, oner, one-shot, or single continuous take unless the user explicitly requests that override. This ban has no exemption — the final reward beat is also a
six-shot sequence, with its reveal push carried out as motion **inside** the shots rather
than as a single unbroken take.

### Location DNA vs. Shot Ladder

Two layers, with opposite rules. Getting them backwards is what makes a pack drift.

**Location DNA — copy verbatim across every IMAGE and VIDEO in the pack.** This is the
identity of the world and never varies:
- the location and its spatial character
- the carrier and its material palette
- the dominant light source and its direction
- time of day and weather register
- the capture-device character (phone model class, lens feel, colour rendering)
- the three primary landmarks and their locked relationships

**Shot Ladder — varies by shot, by design.** This is coverage of that world:
- shot scale (long / full / medium / close / extreme close / wide)
- subject distance and what fills the frame
- which landmarks are in view (see `omni-beat-skeleton.md` for the per-scale rule)
- shot-local capture artifacts

IMAGE anchors are always rendered at **full-shot scale** — the same scale shot 6 lands on.
This is what lets a video's wide outro match the next anchor exactly instead of
approximately.

### Anchor Stability Under Handheld Capture

The UGC layer and anchor continuity pull in opposite directions, so the boundary must be
explicit. The rule is: **landmarks are locked, framing is loose.**

Allowed, and wanted:
- a few degrees of horizon tilt
- small off-centre composition and uneven headroom
- a hand correction that re-frames slightly mid-shot
- brief focus hunting that resolves
- exposure pumping tied to a visible bright source

Not allowed, ever:
- a primary landmark leaving frame in shots 1, 2, or 6
- a landmark changing its relationship to another landmark (the column crossing to the
  other side of the window)
- a landmark changing its rough frame share between anchors
- camera height or lens feel changing between anchors of the same shot family
- so much shake, blur, or glare that the operation's causal evidence is unreadable

Test: if a viewer could not overlay IMAGE N and IMAGE N+1 and see the same room from the
same place, the handheld freedom went too far.

Shot-family conditional attitude wording — use the phrasing that matches the space, and
never claim geometry the space cannot have:

- **level exterior**: `the horizon sits roughly level near mid-frame, with the small tilt of handheld phone footage`
- **elevated or downward-angled**: `the camera holds its steep downward angle, vertical lines converging consistently downward, with no horizon in view`
- **enclosed interior, cave, or windowless space**: `the camera holds a level attitude with the room's vanishing axis staying near the centre of frame`

Mentioning a horizon, sky, or drifting clouds inside an enclosed interior is a P0 failure.

## 2. Action And Physics

Write verb-first physical actions. Gemini Omni can infer physical consequences, but prompts must state the causal chain.

Strong action examples:
- scrapes rust flakes from a steel panel with a matte-black rectangular scraper
- drags cracked boards into a rigid crate along a dusty floor path
- rolls primer across bare wall panels while wet roller stipple remains visible
- bolts a bracket through pre-drilled holes, leaving washer rings and drill dust
- pours gravel from a rigid bucket, and the stones settle under gravity into a shallow trench

Do not overexplain every splash or particle if the source, contact, and force are already clear.

## 3. Lighting

Lock the lighting phase across adjacent anchors. Prefer real available light and imperfect phone exposure over polished studio lighting unless the user requests a commercial or cinematic finish. Use specific light sources and natural exposure dynamics:
- overcast daylight
- golden-hour side light with clipped sky highlights
- doorway or window glare with small blown-highlight areas
- harsh overhead bulb with visible color cast
- temporary work light
- practical laptop glow
- neon sign spill
- mixed daylight and warm practical light
- low-light noise in corners
- crushed shadows under work surfaces
- natural lens flare from a single intense light source
- phone auto-exposure adjustment
- realistic shadows and ambient occlusion

No unexplained day/night jumps. If lighting changes, the physical source must appear or activate.

## 4. UGC Capture Artifacts

De-AI starts from the whole image, not from style adjectives. Add two to four capture artifacts to every IMAGE and VIDEO prompt unless the user asks for a clean commercial look.

Useful UGC capture traits:
- casual phone footage with small framing mistakes
- uneven headroom or clipped foreground edges
- slight horizon tilt that does not break anchor continuity
- brief autofocus hunting before settling on the work area
- phone auto-exposure pumping near bright windows, sky, lamps, sparks, or work lights
- small blown highlights on reflective metal, white plaster, wet paint, or bright sky
- low-light sensor noise in shadows
- mild JPEG or video compression in darker corners
- chromatic aberration along high-contrast edges
- mild rolling-shutter wobble during quick pans or tool recoil
- dust or fingerprints on glass, mirrors, or phone-facing reflective surfaces

Do not make the footage look careless enough to break continuity. Imperfect composition is useful; missing anchor landmarks, unreadable action, or random camera chaos is not.

## 5. Visual Style and Tactility

Prefer UGC documentary realism and production-useful tactile detail over empty quality words. To eliminate the artificial "AI look," prompts must describe physical textures, weathering, capture artifacts, and material imperfections.

Useful style directions:
- casual UGC phone footage with tactile renovation detail
- unpolished phone-recorded worksite documentation
- social-media repair clip with real available light
- restrained documentary realism with tactile grit
- tactile material realism focusing on grain and textures
- utilitarian worksite clarity, highlighting dirt and dust
- high-detail physical material behavior

Tactile micro-textures to include:
- visible wood grain, wood splinters, and rough fiber ends
- rust flakes, pitted steel, metal scratches
- porous concrete, brick mortar stipple, rough plaster
- fine dust particles floating in light rays, sawdust, drill shavings
- uneven hand-painted brush overlaps, wet roller stipple, adhesive squeeze-out
- weathering, scuffs, non-uniform dirt

Avoid weak filler:
- 8k, masterpiece, award-winning, ultra photorealistic, high-end render
- flawless, perfect, seamless, pristine, clean CGI style, clean digital style
- perfectly symmetrical, perfectly aligned unless engineering dictates it

### Reflective Surfaces

Sharp mirror reflections are a high-frequency detail the model cannot hold stable across
six cuts — they flicker, double-image, and invent geometry that contradicts the anchors.
Any reflective floor, glass pane, water surface, or polished metal is described as
low-gloss and defocused:

`any reflective floor or glass returns a soft, low-gloss, heavily defocused reflection with muted contrast and realistic falloff toward the edges, never a mirror-sharp double image`

This also serves the UGC layer: real phone footage of a real worksite floor almost never
produces a clean mirror. If the finished space genuinely has a polished floor, keep the
gloss visible in the *material* description but keep the *reflection* soft.

## 6. Location

Specify the environment with enough spatial character for Omni to infer the world:
- a mossy forest clearing around an abandoned stone hut
- a narrow workshop bay with wet concrete and rusted wall ribs
- a foggy riverside lot beside a half-buried cabin
- an underground chamber with compacted clay walls and timber bracing

Maintain the same location identity throughout a prompt pack.

## 7. Text Rendering Policy

Default policy: no rendered text.

Unless the user explicitly asks for typography, every VIDEO prompt should include a natural sentence like:

`No captions, subtitles, floating labels, UI text, or rendered prompt words appear anywhere in the image.`

If the user explicitly asks for text, specify:
- exact words
- surface where the text appears
- font or sign style
- timing
- layout
- whether the text is painted, engraved, projected, typed, or composited

Do not add incidental readable signs or labels.

## 8. Frame Density And Depth Layering

A clean single operation must not produce an empty frame. Build visual density through depth, not through clutter in the work zone. This layer pairs with the Persistent Environmental Dressing Layer rules in `omni-restoration-continuity.md`: the depth and fill elements added here must be frozen, inert, and outside the work zone.

Three-layer depth rule. Every establishing, full, and wide outro shot should read in three depth planes:
- foreground: a partial occluder at the frame edge (a door frame, hanging vine, stacked-material edge, an out-of-focus tool on a surface)
- midground: the carrier and the operation
- background: environmental depth (a receding wall, tree line, far corner, opening to another space)

Large-surface breakup. Walls, floors, ceilings, and sky are low-density flats. Break them with uneven stains, water marks, light patches, cast shadows, material seams, and local reflections instead of a uniform fill.

Vertical and edge fill. Do not leave the upper frame and edges empty. Use beams, hanging cables, lamps, ropes, overhead stockpiles, roof light gaps, and partial edge objects to fill vertical volume.

Atmospheric depth. Volumetric light shafts, floating dust, thin haze, and backlit silhouettes are cheap depth and density. Add at least one atmospheric or volumetric-light element to each wide/establishing shot. Keep the light phase locked (see section 3); multiple coexisting light sources are allowed and add density.

Per-shot density targets. Each shot scale fills the frame differently:
- establishing long shot: world depth, environmental dressing, atmosphere
- full shot: the worker / tool / staged-material relationship in space
- medium shot: debris and material accumulating around the work zone
- close-up: tool contact filling the frame, minimal empty background
- extreme close-up: surface texture fills the entire frame, no large soft empty area
- wide outro shot: the same world depth and dressing as the establishing shot, now carrying the operation's permanent traces

Density hierarchy. Add density toward edges, foreground, and background depth; keep the operation point and its traces high-clarity and low-interference. Never let density bury the causal evidence.
