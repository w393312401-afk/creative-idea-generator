# Threshold Mode — Buried Vehicle

## Demonstrates

Threshold routing with exterior work → same-axis forward bridge → interior fit-out → concealment → reward. Shows how the composer handles mode selection, bridge VIDEO insertion, and the split between exterior and interior shot families. Also demonstrates **multi-system beat splitting** (the naive "waterproofing + window repair + hatch install" beat is split into single-operation beats), **persistent site plant** (cribbing timbers erected once, struck in a named strike beat), a **ventilation beat** for a habitable sealed shell, and the **power chain** for an off-grid warm-light reward (battery bank + wiring installed on camera before any light glows).

## User Input

```text
做一个埋在山坡里的旧校车改造成隐藏休息舱，有一个隐藏入口，最后奖励镜头是 hatch 打开透出暖光
```

## Internal Inference

```
CARRIER: buried yellow school bus on wooded hillside
ENV: wooded hillside with leaf litter, cedar stump, muddy bank
TRAUMA: half-submerged in soil, windows mud-packed, roof dented under roots, yellow paint rust-flaked, drainage ruts
DESTINY: concealed underground rest shelter with hidden hatch
REWARD ACTION: hidden hatch opens on hinge, warm light spills from finished interior
Space Type: underground space
Mode: Standard (no threshold bridge needed — entire sequence is exterior-facing)

Note: Despite being "underground space", this specific topic keeps the camera
on the exterior hillside throughout. The interior is only revealed through the
side work access and the final hatch opening. No threshold crossing is needed
because the camera never enters the bus.

N: 12 production beats (single-operation splitting applied — the naive 6-beat
   version packed three systems into one beat and buried repaired glass windows):
  1. Excavation reveal (clear roots, expose bus side)
  2. Shoring + bracing (cribbing timbers erected — persistent plant, stays until beat 10)
  3. Drainage bed (gravel bed + perforated pipe along the slope cut)
  4. Waterproofing membrane (roof and exposed side)
  5. Window sealing (steel blanking plates bolted over the window band — glass is
     never repaired only to be buried under soil pressure)
  6. Hatch plate install (flush in roof line)
  7. Ventilation snorkel install (low intake + exhaust risers through the soil line)
  8. Service rough-in (battery bank + interior wiring, visible through side work access)
  9. Interior fit-out (bench, bedding, warm light fixture mounted onto the roughed-in
     wiring — visible through side work access; no light activates yet)
  10. Temporary works strike (cribbing timbers carried out, foot-pad compression marks remain)
  11. Concealment (soil grading, camouflage, snorkel caps disguised as cut sapling stumps)
  12. Reward (hidden hatch opens, warm light spills from the wired fixture)

Camera DNA Block:
  "static tripod pulled back to hold the full slope cut and bus length,
   single full-frame composition, ultra-wide 14-18mm lens feel,
   camera height 1.6m, and locked eye-level perspective along the bus side."

Geometry Lock:
  - bus length and proportions
  - hillside slope angle
  - window band position
  - roof edge line
  - rear wheel arch position
  - hatch plate location (introduced IMAGE 7, concealed IMAGE 12, opened IMAGE 13)

Fixed Landmarks:
  - Foreground: leaf-litter path
  - Mid-depth: collapsed bus roof edge, cedar stump (left), muddy bank (right)
  - Background: rear wheel arch, tree line

Object Ledger:
  - one cedar stump, weathered, left of path
  - one rear wheel arch, rust-corroded, right mid-depth
  - one rough hatch plate, metal, flush in roof line (appears IMAGE 7)
  - cribbing timbers (erected on camera in VIDEO 2, appear IMAGE 3, persist as static
    plant through IMAGE 10, struck on camera in VIDEO 10 leaving foot-pad marks)
  - two grey ventilation risers (appear IMAGE 8, disguised as sapling stumps from IMAGE 12)
  - one dark battery bank box (glimpsed through side access from IMAGE 9)
```

## Threshold vs Standard Decision Logic

This topic illustrates an important routing decision: even though the space type is "underground," the camera never physically crosses a threshold. The entire sequence is observed from the exterior hillside. The interior is only glimpsed through the side work access opening during fit-out (IMAGE 5 / VIDEO 4).

**Threshold mode would be used if**: The camera needed to travel from the hillside, through the hatch or side opening, and into the bus interior for post-bridge shots.

**Standard mode is correct here because**: The camera stays on the hillside. The interior reveal happens through the reward hatch opening, not through a camera crossing.

## Key Composition Notes

1. **Hatch as controlled reveal**: The hatch plate appears in IMAGE 7 (installed flush), is concealed in IMAGE 12 (soil and moss cover it), and opens in IMAGE 13 (reward). It must not glow, open, or show interior light before IMAGE 13.
2. **Camouflage continuity**: After IMAGE 12, the bus must be visually hidden. The only cues are the hatch outline, the two disguised snorkel stumps, and bus hints (wheel arch trace, disguised roof edge).
3. **Interior glimpse through work access**: IMAGE 9-10 / VIDEO 8-9 show the battery bank, wiring, and fit-out through the side work access, but the camera remains on the hillside.
4. **Final reward is hatch-only**: No camera entry, no interior hero shot. Just the hatch opening and warm light spilling upward — physically explained by the fixture and battery bank installed on camera in beats 8-9.
5. **Habitability logic**: a sealed, buried sleeping shell needs air — the ventilation beat (7) exists so the concealed state does not read as an airtight coffin; the snorkel caps stay findable in the concealment anchor as disguised stumps.

## Validation Points

- [ ] Mode correctly set to Standard (not Threshold)
- [ ] No threshold bridge VIDEO present
- [ ] Every beat carries exactly one physical operation (no waterproofing+window+hatch composites)
- [ ] Hatch plate first visible in IMAGE 7 — not before
- [ ] Windows sealed with steel blanking plates, never re-glazed before burial
- [ ] Ventilation risers installed (IMAGE 8) and still findable, disguised, in the concealment anchor
- [ ] Battery bank + wiring visible through side access (IMAGE 9) before any light glows
- [ ] No warm light or interior glow before IMAGE 13
- [ ] Cribbing timbers persist as static plant IMAGE 3-10 and are struck on camera in VIDEO 10 (no blink-out)
- [ ] Cedar stump locked in left position across all 13 IMAGEs
- [ ] Rear wheel arch evolves: corroded → reinforced → trace → trace
- [ ] Concealment (IMAGE 12) shows completely hidden bus, no light leak
- [ ] Final VIDEO uses static tripod reward (not push-in, since camera stays exterior)
- [ ] ASMR audio: latch click, hinge creak, seal break
- [ ] 12 VIDEOs + 13 IMAGEs

## Reference

This file is a **routing and beat-ladder specimen**, not a prompt-text specimen: its value is
the Standard-vs-Threshold decision and the 6→12 beat split, both fully written out above. It
used to close with a pointer into one developer's home directory, which is a dead link on
every other machine — a model that follows it finds nothing and improvises, which is worse
than having no pointer at all.

For actual prompt-text form, use the two examples that carry real bodies:
- [minimal-input-warehouse.md](minimal-input-warehouse.md) — a contract-clean worked slot pair
- [threshold-mode-hollow-oak-tree.md](threshold-mode-hollow-oak-tree.md) — a full-length set including a TBCP v4 crossing
