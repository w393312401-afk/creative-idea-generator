# Threshold Bridge Consistency Protocol (TBCP)

This protocol governs the single most failure-prone beat in any restoration timelapse: the **exterior → interior crossing** (the "threshold bridge"). It extends SCUP by treating the crossing as one coordinated event rather than several independent locks. See `spatial-consistency-upgrade-protocol.md` for the static/dynamic/agent locks this builds on.

> **Why this beat is special.** Every other beat changes *state*. The threshold beat is the only beat that simultaneously switches three systems at once:
>
> 1. **Lighting domain** — bright exterior daylight → dim interior (cooler by default; warmer only when a warm interior light source is already burning and visible).
> 2. **Camera family** — exterior Camera DNA → interior Camera DNA.
> 3. **Anchor set** — exterior 3 primary anchors → interior 3 primary anchors.
>
> When they all flip inside one clip without care, the model "explodes" the interior: hallucinated layout, exposure snap, white-balance jump, door-frame rubber-stretch, anchor amnesia. TBCP forces them to flip **gradually and explicitly, inside one deliberately-written clip**, rather than leaving the model to invent the transition.

> **TBCP v4 — single merged beat (2026-07-21).** The crossing is now exactly **ONE beat** (`bridge_stage: 1`), for both the coaxial and pan variants. There is no separate hold/sill/vestibule/turn beat and no discarded internal placeholder clip — earlier revisions (v2's three-stage pan bridge, v3's merged-but-still-three-image crossing) added structure to work around a single edit step being too big a jump for the image-edit model; the door-clearance retry (§5 below) already solves that generation-quality problem without needing a separate beat/IMAGE slot. This beat produces exactly **2 visible images spanning the crossing** (the last ordinary exterior beat's IMAGE, and this beat's own settled-interior IMAGE) connected by exactly **1 visible VIDEO clip**, whose prose narrates the *entire* arc — approach, sill crossing, door-frame wipe, settle, and (pan variant only) the turn onto the interior's long axis — as one continuous, unbroken shot. Also carried over from v3: the settled/turned frame is the **First Interior Reveal — Untouched Trauma State** (§6 below): it must read as untouched pre-renovation decay, not a tidy room, since no interior construction beat has touched it yet. Finally, this crossing beat may never land earlier than Beat 3 in the ladder — at least 2 ordinary exterior beats (establishing the environment + showing exterior cleanup/repair progress) must precede it, so the sequence never reads as "starting indoors."

---

## TBCP Architecture

```mermaid
graph TD
    subgraph "One Beat, Whole Arc"
        A[Single Bridge Beat: 1 IMAGE + 1 VIDEO] --> O
    end
    subgraph "Carry, Don't Switch"
        B[Anchor Inheritance: PBISP peek = interior primaries] --> O[Continuity]
        C[Single-Variable Camera: lock lens+height, translate (+ turn) only] --> O
        D[Cross-Threshold Tether: 1 material or light continues] --> O
    end
    subgraph "Hide & Smooth the Cut"
        E[Exposure & WB Soft Roll: physical attribution, no snap] --> O
        F[Door-Frame Wipe: symmetric edge slide masks the switch] --> O
    end
    O --> Output[TBCP-Compliant Threshold Beat]
```

---

## The Rules

### 1. One Merged Beat (never split, never held on the doorway)

**Principle**: The entire exterior-to-interior crossing is composed as ONE beat, producing ONE IMAGE (the settled interior frame) and ONE VIDEO (the full crossing clip). Splitting it across multiple beats/images/clips — as earlier revisions of this protocol did — only reintroduces held "at the doorway" compositions and extra viewer-facing clips that add nothing the single clip can't narrate itself.

**Canonical layout**:

```
IMAGE T      Exterior · at-threshold anchor (door open, 2 interior anchors already visible inside)
VIDEO T      [THE ONLY VISIBLE CLIP] one continuous shot narrating the full arc: approach,
             crossing the sill (door-frame edges slide out), exposure/WB roll, interior anchors
             scaling to final size, and — pan variant only — ending in one stationary pan onto
             the interior's long axis.
IMAGE T+1    Interior · settled (and, pan variant, already turned) anchor — camera now on
             interior Camera DNA, door frame fully behind it.
```

**The clip carries no work**: the crossing clip is a pure camera move through an untouched ruin. Nothing is cleaned, cleared, tidied, repaired, or installed while the camera travels, and no tool, ladder, scaffolding, tarp, work light, or stacked material appears at any point in it; the interior reads as filthy from the first moment it becomes visible, not only at the settle. Write it as ONE unbroken take at a steady speed — no cut, fade, dissolve, wipe transition, speed ramp, or freeze (the only edit-like motion is the door frame leaving the frame) — and never label it a construction time-lapse, because nothing is being built. The cleanout of that mess is the NEXT beat (§9).

**Generation note (not a separate beat)**: IMAGE T+1 is generated as a single i2i edit from IMAGE T. If the door-clearance check (§5) finds the door frame still lingering, the generator pushes one more i2i step using the just-rendered frame as reference (§5) — this is an internal quality retry on the SAME image slot, never a new numbered beat/IMAGE.

---

### 2. Anchor Inheritance (PBISP — closes the biggest gap)

**Principle**: SCUP-PBISP requires the pre-bridge exterior IMAGE to pre-visualize 2 interior landmarks through the doorway. TBCP makes those **the same two objects that become the interior shot family's mid-depth and background primary anchors.** Anchors cross the threshold; they are never swapped for a fresh set. This gives the model an unbroken spatial spine through the cut.

**Two-stage hand-off syntax**:

* **IMAGE T (still exterior)** — peek the future primaries, already sharp but still small:
  ```
  Through the open doorway in Grid B2, two interior anchors are already visible and in sharp focus: a brushed-steel tool cabinet at interior mid-right and a red fire extinguisher on the interior rear wall, each reading small at about one-fifth of the frame height.
  ```
* **VIDEO T** — scale, never re-render:
  ```
  the same brushed-steel tool cabinet and red fire extinguisher scale up continuously along the camera axis, never repositioning, never re-rendering, their apparent size growing naturally as the camera approaches (and, pan variant, sliding to their registered position as the camera turns).
  ```
* **IMAGE T+1 (interior settled)** — promote the identical objects to primary anchors:
  ```
  Interior primary anchors: the brushed-steel tool cabinet locked at Grid B3 holding a scale of about half the frame height; the red fire extinguisher locked at Grid A3.
  ```

**Banned**: introducing any interior primary anchor in IMAGE T+1 that was not visible through the doorway in IMAGE T.

**Anchor Qualification (mandatory)**: peeked anchors must be features that plausibly already exist at crossing time — original structure, natural rock/wood formations, pre-existing wreckage, or items installed in an earlier on-camera beat. Future construction products (an uncarved staircase, unplaced furniture, uninstalled fixtures) are banned as peek anchors. The bridge always precedes interior construction in the timeline; peeking a future product forces that object to exist before the beat that creates it — a hard causality inversion the audience reads instantly as an AI error. For sealed or never-entered shells, choose natural interior features (a heartwood ridge, a rock rib, an original bulkhead) as the two peek anchors.

**Monotonic Scale Lock (mandatory)**: the same anchor's declared frame-height scale must strictly increase from `IMAGE T` to `IMAGE T+1` (e.g. one-fifth → three-fifths). The camera closes several metres of distance during the crossing; a constant scale across both IMAGEs contradicts the required continuous scale-up, and the generated bridge reads as a fake digital zoom or a mid-clip shrink-back.

---

### 3. Single-Variable Camera (+ one declared turn for the pan variant)

**Principle**: The fewer physical variables change across the cut, the less drift. Force the exterior and interior shot families to share **identical lens feel and identical camera height**, so the only quantities that change during the crossing are forward translation along the optical axis and — pan variant only — one clean rotation at the very end. This is the **default (Ground-Level) case** — the crossing door sits at the same height as the established camera.

**Bridge Camera DNA** (the VIDEO's own camera-move description — **ground-level, coaxial case only**, see the Elevated Access Variant below for the other case):
```
same 14-18mm lens feel, same 1.6m camera height, coaxial forward push-in only; no pan, no tilt, no roll; horizon stays perfectly level at mid-frame throughout.
```

**Pan variant addendum** (appended to the same clip's prose, not a separate clip): after the push completes and the door frame is fully out of frame, the SAME clip continues with one smooth horizontal pan — no dolly, no tilt, no roll during the pan itself — ending with the central vanishing axis locked on the interior's long axis:
```
...then, without stopping, the camera pans smoothly to the {left|right} until the central vanishing axis locks onto the interior's long axis, with the registered interior anchors sliding in from the frame edge at constant scale to fill the newly revealed side.
```

**Rule (ground-level case)**: If the interior must use a different height/lens (e.g. a low interior), do the height/lens change in a *separate* interior beat **after** IMAGE T+1 — never inside the bridge clip.

**Elevated Access Variant — supersedes the two rules above whenever the crossing door is not at camera height**: Many carriers put the threshold above the ground — a lookout-tower cabin reached by a ladder, a silo hatch, a cable-car cabin door, a wind-turbine nacelle hatch. A pure horizontal "coaxial forward push-in" cannot physically reach an elevated door, so this variant is the **one deliberate exception** to "identical camera height" and "never change height inside the bridge clip": for an elevated threshold, closing the height gap *is* the bridge's job, because no other beat in the ladder is positioned to do it. Use this camera-move description instead:

```
same 14-18mm lens feel, camera rising smoothly along the access ladder's line while pushing forward at the same steady rate, closing the distance to the elevated doorway in one unbroken climbing approach; no pan, no tilt, no roll during the climb — only the combined forward-and-upward vector changes (pan variant: the turn still happens only after the climb settles, as its own final step).
```

Describe the climb as one continuous ascending push, never a flat translation followed by an unexplained jump in height.

**Where the interior settles**: once the crossing completes at IMAGE T+1, the interior shot family simply keeps whatever height/lens the crossing arrived at (or the normal interior default from the Camera Defaults Quick Reference, whichever the composer intends for that interior) — there is no requirement to return to the exterior's ground-level height, since the elevated variant already performed the one-time transition. Treat IMAGE T+1's Camera DNA as a fresh interior-family declaration, same as the ground-level case.

---

### 4. Exposure & White-Balance Soft Roll (NLVTR-safe)

**Principle**: The lighting-domain switch is the most visible snap. Smooth it across the whole crossing clip and **attribute it to a physical light source** so the model treats it as natural falloff, not a hard cut. Per NLVTR, use visual prose only — no percentages, no color-temperature numbers (they get rendered as on-screen text).

**Direction consistency (mandatory)**: pick ONE colour-temperature direction for the whole clip. Default (no interior light source burning): dimmer **and cooler**. If a warm practical or work light is already burning inside and visible through the doorway, write dimmer **and warmer** and attribute it to that light. The clip must never reverse direction mid-arc (e.g. warmer at the start, cooler by the end).

* **Clip prose (default cooler case)**:
  ```
  bright overcast daylight holds on the exterior at the start; as the camera crosses the sill the glare rolls off gently and the frame settles into a cooler, dimmer interior tone, lit mainly by daylight spilling back through the doorway behind — the change is gradual across the whole clip, never a sudden brightness snap.
  ```
* **Warm-source variant**: replace `cooler`/`cooler, dimmer` with `warmer`/`warmer, dimmer` and add `lit by the warm work light glowing ahead inside` as the attribution.

---

### 5. Door-Frame Wipe (hide the cut) + Settle-Frame Door Clearance

**Principle**: Let the door-frame edges slide symmetrically out of frame at the moment of crossing. This creates a natural vertical wipe and lets the exposure/WB transition complete *behind* the moving frame edges — the eye reads it as a portal, not a cut. This also satisfies DKP's symmetric optical-flow requirement.

```
at the sill the door-frame edges slide symmetrically outward past the left and right boundaries, briefly framing the shot like a vertical wipe; the exposure shift completes while the frame edges pass.
```

**Settle-Frame Door Clearance (mandatory)**: the wipe must actually FINISH. In IMAGE T+1 (the interior-settled frame) and in EVERY later interior IMAGE, the door frame, door leaf, threshold edges, and the entry opening itself are FULLY BEHIND the camera and must not appear anywhere in the frame — interior walls, ceiling, and floor fill the frame edge to edge. An interior still seen THROUGH the doorway (opening edges visible near the frame borders, interior occupying only an inner rectangle) is the single most common shipped failure of this protocol: the image-edit model, given a reference frame whose door frame fills the composition, tends to make only a timid crop. Countermeasures, all mandatory:

1. Every post-crossing IMAGE prompt states the door-clearance clause explicitly (never assume it).
2. The cross-threshold light tether (Rule 6) is REWORDED after the settle: entry daylight becomes **directional light from behind the camera** ("daylight from the entry behind the camera lays a soft bright wedge across the floor toward the rear wall") — never a visible doorway, door frame, or bright opening in frame.
3. The renderer runs a door-clearance check on IMAGE T+1 and, on failure, pushes one more step forward using the same bridge (camera-advance) edit instruction, reusing the just-rendered frame as reference — this is an internal retry on the same image slot, not a new beat.

**Carrier Identity Hand-off (mandatory)**: the door frame is usually the only element binding the interior to the carrier. Once it leaves the frame, the carrier's OWN fixed interior identity features must take over as anchors — a bus's side window band, ribbed roof curve, or wheel arches; a boat's rib frames or portholes; an aircraft's window row. At least one registered interior primary anchor must be such a feature, restated in every interior IMAGE, or the interior degrades into a generic room. Equally: once fully inside, the interior's main light source must be NAMED (the carrier's own openings, an installed practical light, or entry daylight from behind the camera) — an unlit interior invites the model to invent windows the carrier does not have, which is its own consistency break.

---

### 6. First Interior Reveal — Untouched Trauma State

**Principle**: IMAGE T+1 is the FIRST time the interior is seen, and no interior construction beat has touched it yet — it must read as the SAME untouched, pre-renovation trauma state already established outside, never a tidy or staged room. Three requirements, all mandatory:

1. **Decay severity**: the same material palette and weathering established in the exterior beats, plus AT LEAST THREE of these decay categories clearly visible: structural damage (cracks, sagging, holes, missing sections), surface decay (rust, water stains, peeling paint, mold, corrosion), biological/vegetation intrusion (moss, vines, roots, weeds), or debris/clutter accumulation (rubble, fallen materials, scattered trash, collapsed fixtures). This matches the bar IMAGE 1 is already audited against — the interior is not allowed to be the mild one.
2. **Zero intervention evidence**: no tools, toolboxes, ladders, scaffolding, paint cans, buckets, tarps, drop cloths, work lights, safety cones, or fresh/stacked construction materials anywhere in frame, and no patch that reads as already repaired, re-clad, cleaned, or painted. Nobody has entered this space yet.
3. **Unarranged**: every piece of wreckage lies exactly where gravity and time dropped it — debris scattered unevenly, dirt drifted into corners, growth following the damp cracks. Never swept, never gathered into neat piles, never aligned or set-dressed. The floor must NOT read as cleared: clearing it is the very next beat's job (§9).

This applies ONLY to IMAGE T+1 — every later interior beat instead follows its own STAGE SCOPE progressive-completion rule.

**Renderer backstop**: after the door-clearance check (§5) passes, the renderer runs a raw-state check on the rendered IMAGE T+1 pixels (intervention evidence / already-tidied space / already-restored surfaces / fewer than two decay categories). On failure it re-edits the same image slot once with a state-correction instruction — camera locked, only the state of what is in frame changes — then keeps the frame and records the reason for the sequence review. Like §5 this is an internal retry on the SAME image slot, never a new beat.

---

### 7. Cross-Threshold Tether

**Principle**: Carry **at least one material or one light source unbroken across the sill** to give the interior a physical tie to the exterior. Without a tether the interior reads as a disconnected new world.

* **Material tether**:
  ```
  the same grey concrete floor runs continuously from the exterior threshold into the interior, unbroken at the sill.
  ```
* **Light tether** (the daylight that lit the exterior becomes the interior backlight):
  ```
  the exterior daylight persists inside as a bright rectangle of backlight from the doorway behind the camera.
  ```

---

### 8. Frame Hand-off Lock

**Principle**: Enforce strict temporal continuity by declaring IMAGE T as the VIDEO's first-frame anchor and IMAGE T+1 as its last-frame anchor, exactly like any ordinary beat's video — no redirection to any other IMAGE number.

```
Use IMAGE T as the actual first-frame image and IMAGE T+1 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout.
```

---

### 9. Post-Crossing Cleanout (the mandatory beat after the crossing)

**Principle**: §6 forces IMAGE T+1 to be a genuinely filthy, nobody-has-been-here-yet ruin. The beat immediately after the crossing (Beat T+1) must therefore be a `clearing` operation that actually deals with that mess — otherwise the sequence jumps from knee-deep wreckage straight to panelling or paint, and the one step every real restoration starts with silently disappears.

**Contract for Beat T+1**:

* **Operation**: `clearing`. Never framing, rough-in, surfacing, painting, or furnishing — those all come after it.
* **Its IMAGE (T+2)**: everything loose from IMAGE T+1 is gone — rubble, fallen material, scattered wreckage, dead vegetation, and drifted dirt hauled out; the floor is back to its bare original surface across its FULL extent.
* **What must NOT change**: nothing is repaired, patched, coated, or installed here. The structural damage, rust, water stains, peeling paint, and rot established in the reveal all stay exactly as they are — now simply readable on a cleared floor. Later beats repair them.
* **Its VIDEO (T+1)**: the lone worker carrying the debris out in repeated trips with ONE named manual tool (shovel, rake, wheelbarrow, debris sack); the cleared area sweeping progressively across the floor while the spoil container/pile fills — the beat's two independently observable progress lines.

---

## Minimum Run-Up (never start indoors)

**Principle**: The crossing beat (`bridge_stage: 1`) must never be placed at Beat 1 or Beat 2. At least 2 ordinary exterior beats must precede it — one establishing a WIDE view of the whole environment/carrier, and at least one showing exterior cleanup/repair progress — so the sequence reads as "outside first, then a series of exterior progress, then inside," never as starting indoors.

---

## Hard Cut Variant (declared cut, no bridge) — unchanged

**Trigger**: NOTHING of the interior is visible before crossing (sealed shell, pitch-black behind the hatch, no openable view in). PBISP peek is impossible by definition, so a walked crossing would force the model to hallucinate the interior mid-clip. Declared once at brief time as `threshold_variant: hard_cut`; budget: at most ONE per project, and ONLY for the threshold crossing — never as a generic transition shortcut. This variant already matches the "2 images, connected by nothing visible" shape (its crossing has no video at all, just a declared cut), so it is unaffected by the single-beat merge above — it was always exactly one beat.

**Structure — no bridge clips, one declared cut**:

```
IMAGE T      Exterior · at-threshold anchor (door may stay closed; no peek required).
VIDEO T      [CUT] slot: a fixed placeholder declaration — NO video clip is generated for this
             slot, it is never sent to the image-to-video model (interpolating two unrelated
             compositions produces a smeared morph), and the merge treats it as an EXPECTED gap:
             the final film hard-cuts here.
IMAGE T+1    Interior FIRST frame — a NEW CHAIN HEAD: rendered text-to-image, WITHOUT the
             previous frame as reference. Every frame after chains from it as usual.
```

**Consistency across the cut (Scene DNA restatement — the interior has no inherited visual anchor, so the prompt must re-establish the world from scratch, all four mandatory)**:

1. **Carrier identity**: the registered interior anchors, led by the carrier's fixed identity features, with Grid cells and scales.
2. **Material genes**: the same material palette, weathering and decay severity established in the exterior beats.
3. **Light**: the same daylight direction and colour temperature, entering through the carrier's own openings — never invented ones.
4. **Progress state**: the interior at its untouched pre-construction trauma state (the cut happens before any interior beat).

**Judging exemption**: the viewpoint jump across a [CUT] slot is sanctioned — continuity judges must never flag it or "repair" it with a transition; drift baselines re-anchor on the interior first frame (it is the new family anchor).

---

## TBCP Audit Table

A single `FAIL` triggers a rewrite of the threshold beat before delivery.

| TBCP Metric | Audit Checklist Item | Drift Risk Eliminated |
|---|---|---|
| **One Merged Beat** | Is the exterior→interior crossing exactly ONE beat, producing exactly 2 visible images (last exterior + settled interior) and exactly 1 visible VIDEO clip narrating the full arc? | Extra held "at the doorway" clips / images the viewer never needed to see |
| **Minimum Run-Up** | Is the crossing beat at index ≥3, with at least 2 ordinary exterior beats before it? | Sequence reads as "starting indoors" |
| **Anchor Inheritance** | Are the 2 interior anchors peeked through the doorway in IMAGE T the exact same objects promoted to interior primary anchors in IMAGE T+1? | Anchor amnesia / interior layout reshuffle |
| **Anchor Qualification** | Are both peeked anchors plausibly pre-existing at crossing time (original structure, natural formations, or previously installed on camera) — never future construction products (uncarved stairs, unplaced furniture, uninstalled fixtures)? | Causality inversion: objects existing before the beat that creates them |
| **Monotonic Scale** | Does each peeked anchor's declared frame-height scale strictly increase from IMAGE T to IMAGE T+1? | Fake digital-zoom feel / mid-clip shrink-back on the push-in |
| **Single-Variable Camera (ground-level case)** | If the crossing door sits at camera height, does the clip use coaxial forward translation only (+ one declared pan at the very end for the pan variant), with no other pan/tilt/roll? Not applicable when the Elevated Access Variant fires. | Perspective stretch, camera-family discontinuity |
| **Elevated Access Variant (supersedes the row above when applicable)** | If the crossing door sits above camera height, does the clip describe one continuous forward-and-upward climbing push — never a flat push followed by an unexplained height jump? | Impossible camera physics (flat push reaching an elevated door) |
| **Exposure & WB Soft Roll** | Is the lighting change gradual across the whole clip, attributed to door-shade + doorway backlight, with no percentages/color-temp numbers, and with a single consistent colour-temperature direction (no mid-clip reversal)? | Brightness snap, white-balance jump/pumping, text artifacts |
| **Door-Frame Wipe** | Do the door-frame edges slide symmetrically out at the sill to mask the transition? | Visible hard cut at the crossing |
| **Settle-Frame Door Clearance** | Does IMAGE T+1 and every later interior IMAGE state the door-clearance clause (door frame/leaf/threshold/entry opening fully behind the camera, interior filling the frame edge to edge), with entry daylight written as directional light from behind the camera? | Interior seen through the doorway; interior occupying only a small inner rectangle |
| **Carrier Identity Hand-off** | Is at least one registered interior anchor a fixed identity feature of THIS carrier (window band, ribbed roof, wheel arches, rib frames, portholes...), restated in every interior IMAGE, plus a named main light source? | Interior degrades into a generic room; model invents windows the carrier does not have |
| **Cross-Threshold Tether** | Does ≥1 material or light source continue unbroken across the sill? | Interior reads as a disconnected world |
| **Frame Hand-off Lock** | Does the VIDEO bind IMAGE T (first frame) to IMAGE T+1 (last frame) with no redirection? | Anchor/first-frame mismatch |
| **First Interior Reveal — Untouched Trauma State** | Does IMAGE T+1 show the SAME untouched pre-renovation decay established outside — ≥3 of structural damage / surface decay / vegetation intrusion / debris-clutter — with ZERO intervention evidence (no tools, ladders, scaffolding, tarps, work lights, stacked materials, no already-repaired or already-cleaned patch) and nothing swept, piled, or arranged? Applies ONLY to IMAGE T+1; later interior beats follow their own progressive-completion rule instead. | Interior reads as already clean/renovated/set-dressed before any construction beat has touched it |
| **Crossing Clip Carries No Work** | Is the crossing clip a pure camera move — nothing cleaned, cleared, repaired or installed during it, no tools/ladders/staged materials appearing, one unbroken take with no cut/fade/speed ramp, and not labelled a construction time-lapse? | Work happening mid-crossing; the interior quietly tidying itself before the cleanout beat exists |
| **Post-Crossing Cleanout** | Is the beat right after the crossing a `clearing` operation that hauls out every loose piece of the reveal's mess to a bare floor, while leaving all structural damage/rust/stains for the later repair beats? | Sequence jumps from knee-deep wreckage straight to finishes; the first real restoration step disappears |
| **Pan Variant Turn (pan crossings only)** | Does the same single clip end in one stationary pan locking onto the interior's long axis, described in the same clip's prose (not a separate beat), with every surface the pan newly reveals covered by registered anchors at constant scale? | Turn hallucinates unseen space; turn split into its own beat re-introduces a held clip |
| **Hard Cut Declaration (hard-cut crossings only)** | Exactly one [CUT] slot (placeholder body, no clip generated, expected-gap at merge), interior first frame re-establishing all four Scene DNA items, and no judge flagging/"repairing" the sanctioned jump? | Smeared i2v morph across the cut; disconnected generic interior; false continuity violations |
| **Single Continuous Photograph** | Does every IMAGE (including both sides of the crossing) read as one real photograph — never a grid of panels, a collage, a storyboard, or a before/after split? | Grid-coordinate ("Grid A2" etc.) prose misread by the image model as an instruction to render an actual grid/collage |

---

## Relationship to Existing SCUP Locks

TBCP does not replace SCUP — it sequences and tightens the SCUP locks that all fire at the threshold:

- **DKP (SCUP #3)** still supplies the `t=0s / 4s / 8s` door-opening coordinates and symmetric optical flow for the bridge clip.
- **PBISP (SCUP #8)** is upgraded by TBCP Rule 2 (Anchor Inheritance) — the peek is no longer decorative; it defines the interior primaries.
- **Clean Frame (SCUP #7)** still applies: the Sill/settled IMAGE and the bridge clip stay sterile of active workers.
- **Beat Overload Lock (SCUP #10)** is satisfied because the crossing stays a single beat rather than fragmenting into several.
