# Drift Lock Packet Assembly Guide

This guide walks through building a complete Drift Lock Packet from a parsed topic. Every prompt set must complete this packet before drafting any IMAGE or VIDEO.

## Assembly Order

### 1. Camera DNA Block

Write one literal camera/composition sentence (~25-30 words) for each shot family.

**Template**:
```
[shot type], [lens feel], [camera height], [angle], [perspective axis]. [Frame coverage]. [Boundary anchors].
```

**Example — Interior abandoned property**:
```
static tripod shot, ultra-wide 14-18mm lens feel, camera height 1.6m, locked eye-level perspective down a double-height loft. Full stairwell void, mezzanine deck, and bottom foreground debris field held in frame. Left mezzanine edge, right stair run, top ceiling void, bottom foreground rubble band anchored.
```

**Example — Elevated mezzanine shot**:
```
static high-corner vertical tripod shot, ultra-wide 14-18mm lens feel, 3.2m elevated mezzanine-level view, steep downward angle, locked diagonal perspective across a ruined double-height loft. Full stairwell void, mezzanine deck, right stair run, rear windows, and bottom foreground debris field held in frame. Left mezzanine edge run, right stair wall, top ceiling structure, bottom floor-level debris band anchored.
```

**Rule**: Copy this block character-for-character across all non-bridge IMAGE anchors in the same shot family.

### 2. Geometry Lock

Every structural fact that cannot change — written as **relative measures against features
visible in the frame**, never as adjectives and never as absolute metres. "Same wall lines"
and "same carrier proportions" are unusable: no image model can draw to them, which is why
this field was ignored downstream for so long and every interior frame re-derived the room's
size on its own.

Fill all seven subfields (SKILL.md Step 6 carries the same table and is the authority):

| Subfield | Unit | Example |
|---|---|---|
| `clear_width` | door widths | `about two and a half door widths wall to wall` |
| `clear_height` | door heights | `about three door heights to the ridge` |
| `depth_bays` | countable facade features | `four rafter pairs deep` |
| `roof_form` | one form, exterior's own pitch | `single shallow gable, same pitch as the outside; no second roof form anywhere` |
| `aperture_ledger` | exhaustive, entry included | `the single plank door in the gable end; two small square vents under the eaves` |
| `aperture_denylist` | explicit absences, by noun | `no skylight, no roof light, no vaulted or domed ceiling, no rear-wall arched window, no second doorway` |
| `wall_material` | same family inside and out | `the same tarred board cladding continues on the inner face` |

Still locked as before, alongside those: door and window count and placement, stair
direction, beam/column placement, full scene boundary, and every threshold opening's
dimensions.

**The denylist is what actually constrains the model.** A generative model fills
unconstrained volume with whatever the genre suggests; listing what exists does not stop it,
naming the absent noun does. Skylights, vaulted ceilings, and rear-wall arched windows are
this pipeline's three recurring inventions — put them on the list by name unless the shell
genuinely has one, in which case register it in the ledger instead.

**Envelope signature**: mark one clause from `clear_width` or `clear_height`, under twelve
words, numeral-free. It gets restated verbatim on every post-crossing interior IMAGE
(SKILL.md Step 7, Shell Envelope Restatement).

### 2b. Material Palette Lock

The Geometry Lock says how big the shell is; this says what it is made of. Same room, same
clear width, and still the stone reads moss-green in one frame and dry ochre in the next —
because each frame's material wording was improvised and every geometric gate compares names,
height ratios and widths, never adjectives.

Register **3-5 materials** that occupy real frame area. Each splits in two (SKILL.md Step 6
carries the same table and is the authority):

| Field | Mutable? | Example |
|---|---|---|
| `substrate` | never — copied character-for-character every frame | `coarse grey-brown fieldstone, irregular courses, dry-laid` |
| `state_track` | one step forward, only in the beat that works this material | `thick wet moss in the joints` → `joints raked clean, stone still dark with damp` → `dry pale grey stone, tight lime pointing` |

Keep lighting out of `substrate`. `warm honey stone` is a lighting phase in a material's
clothes, and it will swing with every phase change in the ladder.

Both fields ride in the `[material realism]` slot the IMAGE templates already carry, so this
lock is word-neutral — it replaces improvised wording rather than adding a sentence.

**Template phrase**:
```
[clear_width clause]; [clear_height clause]; [depth_bays]; [roof_form, stated as never changing]; the only openings are [aperture_ledger]; there is no [aperture_denylist]; [wall_material]
```

### 3. Fixed Landmarks & Normalized Grid Coordinate System (NGCS)

Every landmark and scene boundary must use the standard **9:16 vertical** grid matrix (`Grid A1` to `Grid C3` — this pipeline's rendering default; see the Frame Aspect Lock in SKILL.md Step 6) and specify the **Z-Depth Height Scale** (frame-height percentage) to lock the Z-axis:

```
  NGCS Grid absolute positioning matrix (9:16 vertical)
  +------------+------------+------------+
  | A1 (Top L) | A2 (Top C) | A3 (Top R) |  <-- Background
  +------------+------------+------------+
  | B1 (Mid L) | B2 (Mid C) | B3 (Mid R) |  <-- Mid-Depth
  +------------+------------+------------+
  | C1 (Bot L) | C2 (Bot C) | C3 (Bot R) |  <-- Foreground
  +------------+------------+------------+
```

The frame is taller than it is wide: rows are generous depth bands, columns are narrow slices. Spread the anchors **up the frame**, one per row.

Assign exactly **3 Primary Spatial Anchors** across three depth zones:
- **Foreground Landmark**: 1 named anchor with Grid cell (e.g., `cracked floor seam in Grid C2`)
- **Mid-depth Landmark**: 1 named anchor with Grid cell and Z-depth scale (e.g., `brick column in Grid B2 holds a scale of 55% of total frame height`)
- **Background Landmark**: 1 named anchor with Grid cell and Z-depth scale (e.g., `tall window opening in Grid A2 holds a scale of 30% of total frame height`)

Vertical-frame scale bands (calibrate against these, not horizontal intuition): background ≈ one-fifth to one-third, mid-depth ≈ two-fifths to three-fifths, foreground band ≈ one-fifth to one-quarter.

### 4. Frame Boundary Lock (Grid Bounded)

Explicitly name four boundary anchors tied to specific Grid cells and physical features:

| Boundary | Template / Example |
|---|---|
| `left boundary` | Left Grid cell + named anchor (e.g., `left mezzanine edge in Grid B1`) |
| `right boundary` | Right Grid cell + named anchor (e.g., `right stair wall in Grid B3`) |
| `top boundary` | Top Grid cell + named anchor (e.g., `ceiling void in Grid A2`) |
| `bottom foreground band` | Bottom Grid cell + named anchor (e.g., `debris band in Grid C2`) |

### 5. Object Position-State Ledger (OSPL) & Ghost Clause

For every recurring or support object, specify count, material/color, exact name, current state, Grid coordinate, and Z-Depth Height Scale:

```
[count] [material/color] [exact name], [current state], locked at [Grid coordinate] with [Z-Depth Height Scale]
```

**Examples**:
- `one yellow fiberglass ladder, folded, locked at Grid B1 with a height scale of 30% of total frame height`
- `two black tool cases, closed, parked at Grid C2 with a height scale of 10% of total frame height`

**The Ghost Clause for Occluded Objects**:
When an object is occluded by construction materials or scaffolding, it must NOT be omitted. Maintain its presence to lock latent text encoder parameters:
```text
[Object Name] remains physically locked at [Grid Cell] with [original properties], currently fully hidden behind [occluding object].
```
*Example*: `The original rear window remains physically locked at Grid A2 with broken glass panes, currently fully occluded by the wooden planks of the scaffold; do not relocate or redraw the window.`

No hard object-count limit — provide a comprehensive list of all detail-critical objects, and extend the ledger organically as new persistent traces are introduced beat by beat.

### 6. Worker/Machine Choreography Ledger (Clean Frame & HAL)

**Clean Frame Boundary**:
All static `IMAGE` prompts must contain **zero active workers or active machinery**. They are pure physical environment state snapshots.

**Transient Video Injection**:
Workers and machines exist *only* in `VIDEO` prompts. They enter the frame, perform construction cycles, and exit before the final frame.

**Hero Agent Lock (HAL)**:
If workers/machines must remain in frame during a video, lock their visual properties using high-contrast, low-detail silhouettes:
- *Banned*: Facial details, clothing logos/patterns.
- *Enforced*: `one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants; the worker remains in a static crouched pose in Grid C2, repeating one single arm-troweling motion; do not show the worker's face.`

### 7. Lighting Phase Ladder

Assign one phase per IMAGE anchor:

| IMAGE | Lighting Phase |
|---|---|
| IMAGE 1 | ambient only |
| IMAGE 2-3 | ambient only (hold) |
| IMAGE 4 | temporary work light active (+1) |
| IMAGE 5 | fixture install in progress (+1) |
| IMAGE 6 | partial practical activation (+1) |
| IMAGE 7 | final practical stabilization (+1) |

**Rule**: Adjacent phases may only hold or advance by +1. Enforce strict shadow fall and exposure continuity.

### 8. Passive Environment Direction

Lock one direction for the entire sequence:

**Exterior**:
```
direction: left-to-right
elements: cloud bands, wind-swept grass and tree leaves, downstream river streaks, muddy silt ripples
```

**Interior / Underwater**:
```
direction: [consistent drift direction]
elements: water-caustic reflections, suspended sediment, green river light shimmer
```

### 9. Interest Budget

Plan three tiers:

| Tier | Slot | Example |
|---|---|---|
| Per-clip hook | Every ordinary construction VIDEO | A hidden surface label becomes readable as workers clear debris |
| Sequence-level reveal | At most one, usually mid-sequence | Threshold crossing reveals unexpected interior space |
| Final reward | Last VIDEO only | Hidden hatch opens, warm light spills out |

### 10. New Object Birth Limit

- Each non-reward VIDEO: at most **one** new support-object class
- Must exist in IMAGE N+1 or be visibly carried in
- Final reward is the only slot for surprise objects/elements

---

## Validation Checklist

Before proceeding to IMAGE rendering, verify:

- [ ] Camera DNA Block is complete and reusable, copied character-for-character
- [ ] Geometry Lock covers all seven subfields in relative units (`clear_width`, `clear_height`, `depth_bays`, `roof_form`, `aperture_ledger`, `aperture_denylist`, `wall_material`) — no qualitative-only entries
- [ ] `aperture_denylist` is non-empty and names its absences as nouns, not as "no other openings"
- [ ] Envelope signature chosen: one clause under twelve words, numeral-free, to be restated verbatim on every post-crossing interior IMAGE
- [ ] Material Palette: 3-5 materials registered, each with an immutable `substrate` phrase (no lighting adjectives) and an ordered `state_track` from trauma to restored
- [ ] Exactly 3 primary landmarks across 3 depth zones named with NGCS grid coordinates and Z-depth height scales
- [ ] All 4 frame boundaries named with explicit Grid coordinates
- [ ] Object Position-State Ledger (OSPL) has coordinates and Z-depth scale, and uses the Ghost Clause for any occlusions
- [ ] Clean Frame Boundary: Zero active workers/machines in any static IMAGE anchor
- [ ] Hero Agent Lock (HAL): SILHOUETTES and block-color properties locked for workers in VIDEO prompts
- [ ] Volumetric Mass & Flow Preservation (VMFP): Percentage volume capacity and physical transport vectors defined for bulk materials
- [ ] Reflective Mirror Alignment (RHMA): Mirror reflection alignment clauses included for high-gloss, polished, or wet floors in final staging/reward beats
- [ ] Sealed Entry: the pre-crossing exterior IMAGE keeps its entry shut/opaque with no interior visible; the crossing clip opens it on camera
- [ ] Lighting phase assigned per IMAGE with hold or +1 only, maintaining shadow fall and exposure
- [ ] Passive environment direction locked across the entire sequence
- [ ] Interest budget allocated across three tiers (clip hook, reveal, final reward)
- [ ] Object birth limit set (max 1 new support object per non-reward video)
