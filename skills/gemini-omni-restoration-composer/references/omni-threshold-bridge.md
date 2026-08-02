# Omni Threshold Bridge

Load this file whenever the story moves from an exterior shell into an interior space
through a real opening (a doorway, hatch, tunnel mouth, garage opening, carved portal).

The exterior→interior crossing is the only moment in a pack that flips lighting domain,
camera family, and the entire landmark set at once. Attempting it in one video is the
single most reliable way to make Gemini Omni replace the layout instead of traversing it.

---

## Rule 1 — Split the crossing into two videos

Never render the crossing as one video. The structure is always:

```
IMAGE T      (exterior, opening visible, interior peeked)
  ↓ VIDEO Bridge-1  — approach to the sill
IMAGE T+1    (sill handoff — standing at the threshold)
  ↓ VIDEO Bridge-2  — cross and settle
IMAGE T+2    (interior, settled)
```

`IMAGE T+1` is literally Bridge-1's last frame **and** Bridge-2's first frame. Declare that
handoff explicitly in both videos. Each bridge video changes at most one and a half
systems, not three.

---

## Rule 2 — Anchor inheritance and anchor qualification

The two interior landmarks glimpsed through the opening in `IMAGE T` must be the **exact
same objects** that become the interior midground and background landmarks in `IMAGE T+2`.
They scale up continuously across both bridge videos. They never reposition, never
re-render as something else, and no interior landmark may appear in `IMAGE T+2` that was
not pre-visualised through the opening.

**Anchor qualification (mandatory).** A peeked landmark must plausibly already exist at
crossing time:

- ✅ original structure — a rib, a beam, a rock shelf, a stair already cut
- ✅ natural formations — rock faces, root masses, ice, mineral seams
- ✅ pre-existing wreckage — collapsed fittings, old furniture, debris piles
- ✅ items visibly installed in an earlier on-camera beat
- ❌ future construction products — an uncarved staircase, unplaced furniture, uninstalled
  fixtures, a wall that has not been built

The bridge always precedes interior construction. Peeking at something the pack has not
built yet forces an object to exist before the beat that creates it, and every downstream
anchor then inherits an impossible state.

**Monotonic scale-up.** Each peeked landmark's frame share must strictly increase across
the three bridge anchors, described in words:

`about a fifth of the opening` → `roughly two fifths of the frame` → `filling three fifths of the frame`

A constant scale across the crossing contradicts the forward move and reads as a fake
digital zoom.

---

## Rule 3 — Single-variable bridge camera

The exterior and interior shot families must share **identical lens feel and identical
camera height**, so the only variable the bridge changes is forward translation.

Bridge camera wording:

`the same phone camera at the same height moves straight forward along its own axis; no pan, no tilt, no roll, no change of lens feel`

This still sits inside the UGC layer — handheld sway, small framing corrections, and
footstep bounce are expected and welcome. What is banned is a *deliberate* camera-family
change mid-crossing.

---

## Rule 4 — Exposure and white-balance soft roll

Attribute the lighting change to physics, not to a grade:

`the bright outdoor glare rolls off gently as the door shade takes over, and the frame settles into the cooler dim interior, still lit mainly by daylight spilling back through the opening behind; the shift is gradual across the whole video, never a sudden brightness snap`

The phone's auto-exposure hunting through the crossing is a UGC asset — describe it as the
camera catching up to the darker interior, with shadow noise rising as it does. Do not use
percentages or colour-temperature numbers.

---

## Rule 5 — Door-frame wipe and cross-threshold tether

At the sill, the opening's edges slide symmetrically outward past the frame margins like a
vertical wipe, completing the exposure shift behind them.

At least one material or light source must continue **unbroken** across the sill:

- the same floor material continues inside
- the exterior daylight becomes the interior's backlight
- the same rock face continues from outside wall to inside wall
- a cable, rail, or pipe runs through the opening

The tether is what proves the interior is behind the exterior rather than being a different
location.

---

## Rule 6 — Isolation from construction

A threshold bridge beat carries **no construction work**. No debris clearing, no
installation, no coating happens during either bridge video. The bridge's entire job is
traversal. Construction resumes in the beat after `IMAGE T+2`.

Correspondingly, neither bridge video needs a worker. If one appears, they walk the path
and carry nothing that gets installed.

---

## Six-Shot Adaptation

Both bridge videos still obey the mandatory six-shot cycle. The scales are the same; the
*content* of each shot changes to serve traversal.

### Bridge-1 — approach to the sill

1. **Establishing long shot** — exterior, matching `IMAGE T`, all three exterior landmarks, the opening reading as a dark rectangle in the shell.
2. **Full shot** — the walk begins; full-body scale moving toward the opening; the exterior landmarks start to slide outward at frame edges.
3. **Medium shot** — the opening grows; the two peeked interior landmarks resolve from dark shapes into identifiable objects; exposure begins its roll.
4. **Close-up** — the sill itself: material, threshold edge, the tether material crossing it.
5. **Extreme close-up** — sill texture and the exact point where the tether material continues inward.
6. **Wide outro shot** — standing at the sill, matching `IMAGE T+1` exactly; peeked landmarks now at their middle declared scale.

### Bridge-2 — cross and settle

1. **Establishing long shot** — from the sill, matching `IMAGE T+1`; the door-frame edges already at the frame margins.
2. **Full shot** — the frame edges wipe outward past the margins; the interior volume opens.
3. **Medium shot** — the two inherited landmarks scale to their final declared share and settle into their `IMAGE T+2` positions.
4. **Close-up** — an interior surface, showing the interior's true material state (which is the *unrestored* interior — construction has not started here).
5. **Extreme close-up** — the tether material on its interior side, proving continuity across the sill.
6. **Wide outro shot** — matching `IMAGE T+2` exactly; the opening now behind the camera as a bright rectangle of backlight.

---

## Audit Hooks

- **P0** — a crossing rendered in one video instead of two.
- **P0** — an interior landmark in `IMAGE T+2` that was not peeked through the opening in `IMAGE T`.
- **P0** — a peeked landmark that is a future construction product.
- **P0** — peeked landmark scale that holds constant or decreases across the three bridge anchors.
- **P0** — a bridge video that also performs construction work.
- **P1** — a brightness snap instead of a gradual exposure roll.
- **P1** — no material or light source continuing across the sill.
- **P1** — the sill handoff not declared as Bridge-1's last frame and Bridge-2's first frame.
