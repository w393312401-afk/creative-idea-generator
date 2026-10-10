# Minimal Input — Warehouse Restoration

## Demonstrates

Tier 1 input → full prompt set. The user provides one sentence. The composer auto-infers all
parameters, builds the drift-lock packet, derives 3 production beats, and renders
`IMAGE 1-4` + `VIDEO 1-3`.

## User Input

```text
做一个废弃仓库翻新
```

## Internal Inference (not shown to user)

```
aspect_ratio: 9:16 (vertical — the pipeline default; anchors spread UP the frame, not across)
CARRIER: abandoned industrial warehouse bay
ENV: industrial zone, interior bay
TRAUMA: collapsed insulation, rust-streaked shelving, broken glass, damp concrete, blown plaster
DESTINY: restrained functional industrial shell
REWARD ACTION: final carry-out + practical light stabilization (subtle convergence)
Space Type: abandoned property          → space-workflows.md routing table: 3-6 beats
Mode: Standard (camera never crosses a threshold; the bay is one continuous interior)
N: 3 VIDEO clips → 4 IMAGE anchors
   1. Hazard clearing        (debris, broken glass, blown plaster carried out)
   2. Shell repair           (ceiling bay patched, insulation re-hung, vapour barrier closed)
   3. Final carry-out + practical light stabilization  ← reward beat
Camera: static tripod, ultra-wide 16mm, 1.6m, locked eye-level, enclosed interior
        → attitude lock is "camera pitch locked level; central vanishing axis centred"
          NOT a horizon line (this is an enclosed interior; see SPCP)

Primary anchors (one per depth row, vertically stacked — 9:16):
  foreground  cracked concrete floor seam        Grid C2   ~ one-quarter frame height
  mid-depth   rust-streaked steel shelving bay   Grid B2   ~ half frame height
  background  high clerestory window band        Grid A2   ~ one-fifth frame height

Boundaries (must be reachable inside a NARROW horizontal span):
  left   corrugated wall return        Grid B1
  right  roller-door jamb              Grid B3
  top    exposed roof truss            Grid A2
  bottom foreground debris band        Grid C2

Lighting ladder — one phase per IMAGE anchor, so it has exactly N+1 = 4 entries:
  IMAGE 1  ambient only
  IMAGE 2  ambient only                    (hold)
  IMAGE 3  temporary work light active     (+1)
  IMAGE 4  final practical stabilization
```

> **Ladder length is a real check, not bookkeeping.** The lighting ladder has one entry per
> IMAGE anchor, i.e. `N+1`. A 5-entry ladder against `N=3` (as an earlier revision of this
> file carried) means either a phase is being skipped or a beat is missing — the mismatch is
> the signal.

## Worked Slot Pair (contract-clean specimen)

The full 4+3 set follows the same shape; these two show the target *form*. Note what is
**absent**: no `Grid` labels, no `%`, no numeric ranges, no acronyms, no telegraphic
`Label: value` sentences. Grid and percentages are internal reasoning only — the delivered
body states positions as prose bearings and scales as fractions.

```text
图片 1:
Generate an image of a static tripod shot inside an abandoned warehouse bay, ultra-wide 16mm lens feel, camera height 1.6m, locked eye-level; camera pitch locked level, central vanishing axis centred on the rear shelving. Locked anchors: a cracked concrete floor seam across the bottom at about a quarter of the frame height, a rust-streaked steel shelving bay at the centre rising to about half the frame height, and a high clerestory band across the top at about a fifth. Boundaries: a corrugated wall return left, a roller-door jamb right, an exposed roof truss overhead, a debris band below. This is the before anchor, empty of workers: the ceiling bay shows a collapsed section trailing torn vapour barrier and hanging insulation, the steel uprights carry rust streaking and flaked coating loss, and the slab holds broken glass, blown plaster, and damp debris. Flat overcast daylight from the clerestory reads as soft shadowless ambient light; raw concrete, corroded steel, and torn membrane textures stay physically believable. Keep the same framing and landmark positions; do not redesign or move the camera.

视频 1:
Use the provided first frame and last frame as exact composition anchors. Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. The camera remains locked in the same static tripod shot with the same frame boundaries, holding the floor seam, shelving bay, and clerestory band exactly where they sit. The clip shows the continuous construction time-lapse, not real-time footage, of clearing the hazard debris field from the bay floor. The entire eight seconds sustain continuous action without static holds. At the very beginning one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants enters from the lower-left edge, wheeling a solid dark-green steel debris cart. The worker crouches in the foreground and performs repeated cycles of raking broken glass and blown plaster toward the cart with a matte-black long-handle steel rake, then lifting the loaded scoop over the cart lip. Loose material stays encapsulated in the cart, never loose on a shovel. Two continuous progress lines run together: the swept concrete grows from a narrow strip at the seam outward until it covers the whole foreground slab, while the cart fills from empty to heaped and is wheeled out and back once. By seven and a half seconds the worker exits through the lower-left edge with the cart, leaving the frame completely empty and sterile at the final frame. A brief hook lands as the raked slab reveals a clean pale aggregate stripe. Fine dust motes drift through the clerestory light and loose membrane edges tremble in the draft. Persistent traces left behind include drag scuff paths toward the exit, a dust edge around the newly exposed slab, and glass grit collected along the wall return. SFX: glass crunch, rake scrape on concrete, cart wheel rumble. Ambient noise: hollow room tone with light wind through the clerestory.
```

## Validation Points

- [ ] Aspect declared as `9:16`; anchors spread one per depth row, not laterally
- [ ] A single-sentence input produces a complete, contract-compliant prompt set with no follow-up questions
- [ ] Camera DNA Block copied character-for-character across `IMAGE 2-4`
- [ ] `IMAGE 1` carries the full landmark and boundary enumeration; later anchors inherit it
- [ ] Enclosed interior → pitch/vanishing-axis attitude lock, never a horizon or sky
- [ ] Every VIDEO opens with the fixed anchor sentence + adjacent-frame binding
- [ ] Object-ledger phrases (debris cart, steel rake) restated literally across slots
- [ ] Passive environmental cues present and varied per clip
- [ ] Audio matches the visible action per phase
- [ ] The final VIDEO is carry-out convergence, not a full reward reveal
- [ ] Delivered bodies contain no `Grid` labels, no `%`, no numeric ranges, no acronyms
- [ ] IMAGE slots ≤180 words, VIDEO slots ≤380 words

## See Also

For a full-length worked set (18 IMAGE + 17 VIDEO) including a threshold crossing, see
[threshold-mode-hollow-oak-tree.md](threshold-mode-hollow-oak-tree.md).
