# Omni Restoration Continuity

This file defines the anti-pop and physical continuity rules for Gemini Omni restoration prompt packs.

## Adjacent Anchor Binding

Every VIDEO prompt must explicitly state:

`Use IMAGE N as the first-frame anchor and IMAGE N+1 as the last-frame anchor.`

All shot changes must interpolate between those two anchors. Multi-shot editing is allowed; layout replacement is not.

## Single-Operation Beat Rule

One ordinary VIDEO equals one dominant physical operation.

Separate these operations into different beats:
- debris clearing
- scraping
- sanding
- primer coating
- finish coating
- excavation
- framing
- wall panel installation
- ceiling panel installation
- lighting installation
- wiring
- floor installation
- furniture placement
- final reward reveal

If a user-provided beat combines operations, split it before writing prompts.

## Construction Sequence Dependencies

The beat ladder must follow real construction order. Default macro sequence:

1. Demolition and debris clearing — structures come down top-first; debris leaves a zone before new work starts there.
2. Structural repair — foundation, framing, load-bearing walls, then roof structure.
3. Rough-in systems — wiring, plumbing, ducting before any panel closes over them.
4. Enclosure — ceiling panels, then wall panels. Board the overhead first so the wall
   panels can rise into and conceal the ceiling-board edges; boarding walls first leaves
   the ceiling perimeter with no legal way to close out.
5. Surface finishing — primer before finish coat; wet trades before dry finishes.
6. Floor finishing — late, after overhead and wet work that could damage it.
7. Fixtures and equipment — lighting only after its wiring exists.
8. Furniture and decoration.
9. Final reward reveal.

Hard vetoes (rewrite the ladder if any occurs):
- Wiring or plumbing after the panels that hide it are installed.
- Finish coat before primer.
- New roof before the walls or frame that carry it are repaired.
- Removing or cutting a load-bearing element without visible temporary shoring in that VIDEO.
- **Power chain broken.** Any pack containing a practical light, lamp, or powered fixture
  switching on must contain an earlier on-camera wiring/rough-in beat, run before the
  panels that conceal it. Off-grid carriers additionally require a visible power source
  installed in a prior beat — solar panel, battery bank, or generator. A missing wiring
  beat fails even when the beats that do exist are in legal relative order. See the
  lighting phase ladder in `omni-lighting-environment-audio.md`.
- **Enclosed-space provenance missing.** Any interior chamber revealed behind a newly
  opened shell must be physically accounted for: either explicitly described as
  pre-existing space (a natural cavity, an original room) at the moment of opening, or
  given its own on-camera excavation and mucking-out beats before any interior finishing.
  The interior volume must plausibly fit inside the exterior shell.
- **Volume not conserved.** Container scale, trip count, or spoil-pile growth must
  plausibly account for the volume removed or delivered in the beat. Clearing a room-scale
  debris field into two hand crates fails. Any cut-out slab, panel, or door-sized solid
  piece must get its own pry-out and carry-out action — crumbs in a bucket never account
  for a large solid piece. Scale the container to the load: hand buckets and crates only
  for debris under roughly half a cubic metre; larger removals need mechanical containers
  (excavator bucket, skip, chute, tracked carrier) or explicitly repeated trips feeding a
  visibly growing spoil pile.

If an observed or user-supplied beat order appears to violate a hard veto, re-inspect the
source material first. A misread frame is far more likely than an impossible build order.

## Cumulative State Inheritance

Persistent traces are not just inherited by the next anchor — they accumulate. IMAGE anchor N must contain all permanent changes and traces from beats 1 through N-1.

A trace may disappear only when a later named operation visibly covers or removes it, and only in the beat where that operation happens. Examples:
- finish coat covers primer brush overlap
- floor installation covers a chalk alignment mark on the subfloor
- panel installation hides cable runs (which is exactly why wiring must come first)

### Occlusion Is Not Removal

Being hidden is not one of the legal ways for a trace or object to leave an anchor. When a
tracked landmark, trace, or ledger object becomes occluded, hold it explicitly in the
anchor description instead of dropping it:

`the brick column remains in place behind the newly stacked panels, unchanged and fully hidden from this angle`

`the pry scars along the joist stay where they were, now covered by the subfloor sheets`

A silently omitted object does not reliably come back, and its reappearance later reads as
pop-in. Omitting an occluded item is a Cumulative State failure exactly like omitting a
visible one.

## Persistent Environmental Dressing Layer

Frame density is a first-class goal, but it is added as a frozen layer, never as new content. The dressing layer is the set of inert background and depth elements that fill the world without participating in any operation: wall hooks, old stains, water marks, corner clutter, dust on sills, weathered signage shapes, distant foliage, fixed furniture that is not being worked on.

This layer is invisible to the Anchor Delta Rule on purpose. Because the same dressing appears identically in IMAGE N and IMAGE N+1, it cancels out of the delta and can never inflate the difference between adjacent anchors. A richer constant background also gives adjacent-anchor interpolation more fixed landmarks, so it strengthens continuity rather than threatening it.

Three hard guardrails keep the dressing layer from breaking the contract:

1. Frozen and inherited. Every dressing element is pixel-stable across all anchors and is inherited and accumulated exactly like a persistent trace (see Cumulative State Inheritance). It may disappear only when a later named operation visibly covers or removes it, and only in that beat. A dressing element that drifts, appears, or vanishes between anchors is a pop-in / monotonicity failure.
2. Inert, never operational. Dressing is decorative and is never used, installed, carried, or transformed. The moment an object will later be acted on, it stops being dressing and must follow full staging and causal-trace rules. Background filler may be uncountable (piles, scatter, foliage masses) and is exempt from countable-inventory counts, but its overall volume must stay monotonic across anchors just like a stockpile.
3. Out of the work zone. Dressing stays outside the active operation area so it can never be mistaken for, or interfere with, the single declared operation. Density is added at the frame edges, foreground, and background depth; the work zone and its causal traces stay clean and readable. Never let dressing, clutter, blur, or atmosphere hide the physical causality of the operation.

## Anchor Delta Rule

The difference between IMAGE N and IMAGE N+1 must be exactly the result of VIDEO N's single declared operation. No side progress in other zones, no bonus cleanup, no extra improvements. If a desired change is not the current operation's product, move it to its own beat.

Each progressive IMAGE anchor states completion extent in concrete spatial terms, such as `the left two-thirds of the wall is primed while the right third stays bare plaster`, so interpolation between anchors has an unambiguous start and end.

## Delta Budget

One VIDEO carries only a plausible amount of change. If an operation alters more than roughly one-third of the visible frame area, or could not believably progress that far within one short video, split the same operation into consecutive quantified beats:

- `wall panels from none to the left half` → `wall panels from the left half to all`
- `roof boards from intact-but-broken to front half cleared` → `front half cleared to fully cleared`

Splitting by operation type alone is not enough; an oversized single-operation beat still forces the model to teleport.

## Legal Time Compression

Timelapse must compress time. Allowed channels, and only these:

1. Repeated action cycles inside one shot (prying the first, second, third board on screen).
2. A cut may skip repetitions of an action already shown once in full, and the prose must state it: `after several more boards come loose the same way`.
3. The time jump between anchors absorbs drying, curing, and settling.

First occurrence on camera: the first instance of every change type — first board, first brush stroke, first fastener — must appear in full with its causal chain. A cut may never skip a first occurrence, introduce a new object, or finish a different sub-task.

Cuts carry no progress: every shot opens at the completion level the previous shot ended with. Progress advances only during visible work.

## State Monotonicity

Construction state never regresses, across shots or across anchors:
- cleaned areas stay clean
- installed parts stay put
- dried coats stay dry
- stockpiles only grow by visible additions and shrink by visible removals

## Countable Inventory

State explicit counts for major countable elements in anchors and videos: three roof beams, two crates, six stacked panels. Counts change only through on-camera action or a stated same-way repetition. Numeric anchoring is the cheapest, strongest defense against drift between shots.

## Reveal Discipline

The final reveal anchor contains only objects installed or carried in during prior beats. Decor, plants, lamps, and props either get their own furnishing beat or do not appear. The reward action interacts only with already-existing items.

## Material And Debris Logistics

- Bulk materials must be staged as a visible stockpile in a prior anchor or visibly carried/delivered into the scene inside the VIDEO before use.
- Removed material either visibly exits the scene boundary or persists as a stockpile in later anchors until it visibly exits.
- Stockpile volume should roughly match what was removed or what will be installed.

## Wet-To-Dry State Mapping

If a VIDEO ends with wet material, the next IMAGE anchor shows its cured or dried state, using the time jump between anchors as the drying interval:
- wet glossy paint with roller stipple → matte dry finish with the same stipple
- fresh concrete pour with float marks → set pale-gray surface with the same float marks
- adhesive squeeze-out → hardened bead with the same squeeze line

## Clean IMAGE Frames

IMAGE anchors are static state references:
- no active workers
- no active machinery
- no tools in mid-motion
- no floating labels or captions

Temporary construction objects may remain only when they are physically present in the anchor state and useful for continuity.

### Persistent Site Plant Exception

Long-duration temporary works are **not** transient tools and must not blink in and out
between anchors: scaffolding, formwork, shoring, cribbing, propping, and site cranes.

They follow their own lifecycle:

1. They arrive in a **named erection beat** — the erection is itself the beat's milestone.
2. They **persist across every later anchor** as static, unmanned equipment. Clean Frame
   bans active workers and running machinery, not parked plant.
3. While they persist they are tracked in the object ledger and held explicitly when
   occluded (see Occlusion Is Not Removal).
4. They leave only through a **named temporary-works-strike beat** that shows the removal
   and leaves traces — scaffold foot pads, prop dents, formwork tie holes, board impressions.

Hard consequence: an anchor showing freshly poured, unset concrete must still show the
formwork supporting it. Concrete that is wet and unsupported has no legal way to hold its
shape.

Hand-carried ladders and step platforms are ordinary transient tools — they enter, are
used, and exit within one video. The exception applies only to erected plant.

### Worker Identity Lock

Workers are the fastest-morphing element across a six-shot cut sequence: a person
described in ordinary detail will change face, build, and clothing between shots. Lock
them as a high-contrast, low-detail silhouette and repeat the same description in every
shot that shows them:

`one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants; the worker's face is never shown`

Rules:

- Use solid, saturated, nameable colours. Patterns, logos, and fabric detail invite drift.
- Never describe facial features, hair, age, or build. There is nothing to keep consistent
  if there is nothing specified.
- Repeat the same silhouette phrase in shots 2, 3, and 4 rather than shortening it to
  `the worker` after first mention — the shortened form is where the model re-invents them.
- A second worker (required for loads beyond one person's capacity) gets a **different**
  solid colour and is described just as consistently.
- The silhouette must stay identical across every VIDEO in the pack, not just within one.

## Causal Trace Requirement

Every changed element must include:
- source or entry path
- contact method
- movement or installation path
- at least two persistent traces inherited by the next IMAGE anchor and accumulated by all later anchors (see Cumulative State Inheritance)

The two traces must be characteristic products of the current operation: roller work leaves roller stipple, bolting leaves washer rings and drill dust, prying leaves pry scars. Do not borrow traces from a different trade.

Trace examples:
- seam shadow
- screw heads
- washer rings
- drill dust
- adhesive squeeze-out
- caulk bead
- brush overlap
- roller stipple
- drag scuff
- scrape mark
- weld bead
- cut line
- compression mark
- chalk alignment mark
- cable rub
- bracket shadow
- dust boundary

These traces are not dirt by default. They are visual proof of physical causality.

## Worker And Tool Handling

Workers and machines appear only inside VIDEO prompts. To ensure physics simulation matches reality and avoids weightless actions, describe physical resistance, mass, and inertia.

Each worker sequence needs:
- entry path
- visible tool
- single dominant task
- exit path
- empty final wide shot

Access and crew plausibility:
- Work above comfortable arm reach requires a visible ladder, scaffold, or standing surface. Treat it like any other temporary tool: it enters, it is used, it exits before the wide outro shot unless it logically stays for the next beat.
- Loads beyond one person's plausible capacity (full ceiling panels, beams, large appliances) require a second worker or a machine; describe the shared or mechanical lift.

Physical weight and resistance guidelines:
- Describe body posture adapting to load, such as leaning forward to brace against weight.
- Describe tool physics, such as recoil, friction resistance, or physical inertia.
- Describe micro-physics debris, such as drywall dust, sawdust, sparks, rust flakes, or water splashes.

Describe hand tools with color, geometry, and material:
- matte-black rectangular steel shovel head
- solid-blue heavy-duty paint roller
- bright-orange pneumatic staple gun
- matte-gray masonry hammer
- black rubber-handled pry bar

Avoid vague tools such as `a tool`, `equipment`, or `machine` when the action depends on tool contact.

## UGC Capture Continuity

Phone-camera artifacts must be stable enough to support anchor continuity:
- preserve the same landmarks and object identity across adjacent anchors
- keep imperfect framing readable, not chaotic
- keep exposure changes tied to visible light sources
- keep focus hunting brief and resolved before important trace evidence appears
- do not hide physical causality behind blur, shake, glare, or compression

## Multimodal Reference Continuity

When references are present:
- `<image>` can define material finish, object identity, shape, color, typography, or spatial style.
- `<video>` can define motion behavior, subject continuity, source transformation evidence, or environmental motion.
- `<audio>` can define tempo, impact rhythm, ambience, or reactive motion timing.

If a prompt uses reference material, name the reference label directly in the prompt. Do not silently rely on it.

## Text Artifact Prevention

Default all prompt packs to no visible text:
- no captions
- no subtitles
- no floating labels
- no UI
- no prompt words
- no accidental signage

Only render text when the user explicitly requests it.
