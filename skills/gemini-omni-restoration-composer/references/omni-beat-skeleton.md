# Omni Beat Skeleton — Internal Planning Layer

Everything in this file is **internal**. None of these field names, and none of the
acronyms, may appear in delivered prompt text. This is the ledger you audit prompts
against, not a form you fill into the output.

---

## 1. Temporal Physics Skeleton (per beat)

Before writing a single prompt, convert every beat into these nine fields. A beat that
cannot fill all nine is not a beat yet — it is a wish.

| Field | Meaning |
|---|---|
| `shot_family` | `exterior_cycle` / `threshold_bridge` / `interior_cycle` / `reward_cycle` |
| `space_id` | `site` / `primary` / `secondary`; state monotonicity is audited per space |
| `transition_stage` | topology stage from `omni-threshold-bridge.md`, or `none` |
| `camera_family` | entrance detail / shaft axis / landed partial / oblique / low rail / wall graze / reverse |
| `reveal_scope` | `local` / `partial` / `full`; partial must occlude the far wall |
| `light_source_state` | only physically installed or carried sources visible at this point |
| `beat_type` | `removal` / `excavation` / `surface_prep` / `coating` / `rough_in` / `enclosure` / `fixture_install` / `threshold` / `interior_finish` / `furnishing` / `temporary_works_strike` |
| `single_physical_operation` | the one named terminal milestone this whole video produces |
| `material_source` | where the changed material or object comes from before it enters frame |
| `entry_path` | how it physically enters the working area |
| `tool_contact` | the specific hand / tool / machine contact that causes the change |
| `movement_path` | the visible transport or installation route through the frame |
| `persistent_traces` | at least two physical traces inherited by IMAGE N+1 |
| `next_frame_inheritance` | what must remain visible and unchanged in the next anchor |

Mapping to the shot structure:

- `entry_path` + `material_source` + `movement_path` → the main wide working shot, where the
  worker, tool, and material source are all in frame at full-body scale
- `tool_contact` → the close-up insert
- `persistent_traces` → the extreme close-up insert (or the single close-up insert on 4s and
  6s clips) and carried into IMAGE N+1
- `next_frame_inheritance` → the returning wide shot and the next anchor's inherited-trace list

If a field has no home in the clip's three or four shots, the beat is underspecified and the
model will invent the missing physics.

---

## 2. Visible Milestone Package Rule

The Single-Operation Beat Rule prevents a beat doing **too much**. This rule prevents it
doing **too little**. Both must hold.

Every ordinary beat ends in one named, immediately legible stage product at its **full
declared extent or count**:

- ✅ `all six wall panels installed across the full wall`
- ❌ `the first panel installed in the corner` (token patch)
- ❌ `panel installation underway` (merely begun)

A beat may contain up to **three tightly related actions in the same zone** when all three
are causally necessary for one terminal result:

- ✅ roof panels + ridge closure + drip edge → one weathertight roof
- ✅ floor joists + bay insulation → one insulated deck ready for subfloor
- ❌ demolition + painting + furnishing (cross-phase bundle — split)
- ❌ wiring in one room + flooring in another (different zones — split)
- ❌ rough-in + the panel that conceals it (the panel must be its own beat, or the rough-in is never seen)

Interaction with the Delta Budget: if a full-extent milestone would exceed roughly
one-third of the visible frame area, split the **same operation** into consecutive
quantified beats (`none → left half`, `left half → all`). Each split half is then itself a
full declared extent. Do not resolve a delta-budget conflict by shrinking the milestone to
a token patch.

---

## 3. Spatial Anchoring Without Coordinates

Omni prompts carry no grid coordinate system, and coordinate notation is banned from
output. Anchoring is still required — it is done in prose, in two tiers.

### Tier 1 — Three primary landmarks, one per depth zone

Pick exactly three fixed features and hold them for the entire pack:

- one foreground landmark (a floor seam, a threshold sill, a stacked-material edge)
- one midground landmark (a column, a rib, a door frame, the carrier's dominant mass)
- one background landmark (a window opening, a far corner, a receding passage)

Each is named with its approximate frame share, in words, not numbers:

- ✅ `the brick column stands just left of centre, filling roughly the lower two-thirds of the frame`
- ❌ `brick column in Grid B1 at 60% frame height`

Visibility by shot scale:

- the main wide working shot and the returning wide shot: **all three** landmarks visible in
  their locked relationships. These two are the same camera setup and they are what carries
  anchor continuity.
- the inserts: at least **one** landmark, or a named part of one, stays in frame as the
  spatial handle. An insert with no landmark reference is where layout drift enters.

### Tier 2 — Relative Positioning Lock for drift-prone objects

Do not give secondary objects their own independent position description — that is how
they wander. Tie each one to the nearest primary landmark:

- ✅ `the green toolbox sits a hand's width to the left of the brick column`
- ✅ `the material stack leans against the door frame's right jamb`
- ❌ `a green toolbox on the floor` (unanchored — will move between anchors)

Limit to the two or three most drift-prone items per anchor. Anchoring everything dilutes
the anchoring of anything.

### Occlusion — the hold-in-place clause

When a tracked landmark or ledger object becomes hidden, it must **not** be dropped from
the anchor description. Hold it explicitly:

`the brick column remains in place behind the newly stacked panels, unchanged and fully hidden from this angle`

Silently omitting an occluded object is how it fails to come back. A dropped object is a
Cumulative State failure (P0), whether it was dropped because it was repaired or because
it was merely hidden.

---

## 4. Object Persistence — Three Legal States

Every object in the pack is in exactly one of three states at any time:

1. `inherited in place` — was here, still here, unchanged
2. `human-moved` — moved on camera; the prompt must state the movement and the destination
3. `human-carried-out` — left the scene on camera; the prompt must show the carry-out action

There is no fourth state. An object that changes position without falling into state 2 or
3 has teleported.

### Object birth limit

Each non-reward VIDEO may introduce at most **one** new support-object class (a new tool
type, a new container type, a new material type), and only if it either exists in IMAGE N+1
or is visibly carried in during the video.

Two new object classes in one beat is the strongest single predictor of pop-in. If a beat
seems to need two, one of them belongs to a different beat.

### Reward anchor

The reward anchor contains zero objects that were not installed or carried in during a
prior named beat. Decor, plants, lamps, cups, books, and textiles either earn their own
furnishing beat or do not appear. This is the same rule as Reveal Discipline, stated from
the object ledger's side.

### Negative closure for open-ended beats

Image models over-complete an under-constrained zone rather than under-completing it. Two
confirmed failure patterns:

1. Restating an already-inherited damage descriptor inside a **later** beat's change
   description gets read as a fresh destructive action in that beat, stripping material
   well beyond the declared operation. State inherited damage as inherited
   (`still showing the same bent rib framing from earlier`), never as new.
2. An open-ended furnishing or reward beat invites invented props.

Fix both by writing the bound **negatively and explicitly** in the same anchor:

- `the side wall panels remain in place, not stripped`
- `only these listed objects are present — no additional decor, tools, or furnishings`

Positive description alone does not bound the model.
