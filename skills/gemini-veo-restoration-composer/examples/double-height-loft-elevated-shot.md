# Double-Height Loft — Elevated Mezzanine Shot

## Demonstrates

Complex interior with elevated mezzanine camera position (3.2m), multi-level landmarks across stairwell void, mezzanine deck, and lower floor, and a 10-beat production ladder. This is the pattern reverse-engineered from the user's original loft transformation prompts. Also demonstrates **beat-count correction**: the user asked for 8 clips, but the Micro-Incremental Splitting Rule and the Power Chain Gate force the composer to split lighting from window installation and to insert a wiring rough-in beat — feasibility rules override a literal clip count.

## User Input

```text
做一个废弃双层阁楼改造成工业风 loft 的延时修复。有楼梯、夹层平台、后面有两个大窗。机位在夹层高度往下拍。8段视频。
```

## Internal Inference

```
CARRIER: abandoned double-height loft with central stair void and mezzanine deck
ENV: urban industrial building, interior two-level space
TRAUMA: rotten concrete walls, collapsed ceiling strips, moss stains, broken window frames, trash on both floors
DESTINY: polished industrial live-work loft
REWARD ACTION: person descends restored stairs, walks across polished lower floor (ASMR footsteps)
Space Type: abandoned property (variant: multi-level interior)
Mode: Standard
N: 10 production beats (user asked for 8 clips; single-operation and power-chain
   rules force the split — the composer corrects infeasible counts instead of
   merging systems):
  1. Debris clearing (lower floor)
  2. Debris clearing (mezzanine level)
  3. Structural stabilization (ceiling, stair frame, mezzanine edge)
  4. Window installation (two tall rear openings glazed — envelope closed before any finishes)
  5. Electrical rough-in (surface conduit runs to sconce and pendant points, before skim coating covers the walls)
  6. Wall treatment (pressure washing, skim coating)
  7. Stair restoration (treads, railing)
  8. Floor system (lower floor leveling, polishing — last wet/heavy work, after all overhead trades)
  9. Lighting fixture installation and activation (onto the roughed-in wiring)
  10. Final reward walk-through

Camera DNA Block:
  "static high-corner vertical tripod shot, ultra-wide 14-18mm lens feel,
   3.2m elevated mezzanine-level view, steep downward angle, locked diagonal
   perspective across a ruined double-height loft. Full stairwell void,
   mezzanine deck, right stair run, rear windows, and bottom foreground
   debris field held in frame. Left mezzanine edge, right stair wall,
   top ceiling structure, bottom floor-level debris band anchored."

Geometry Lock:
  - rear wall has two tall window openings
  - stair rises along right wall to upper deck
  - left mezzanine edge runs horizontally across mid-frame
  - lower floor opens below with central stair void
  - same wall placement, same window count (2), same stair direction (right-side rise)

Fixed Landmarks:
  - Foreground: mezzanine deck surface, debris band
  - Mid-depth: stairwell void opening, right stair run, left mezzanine edge
  - Background: two tall rear window openings, upper ceiling structure

Frame Boundary Lock:
  - left boundary: left mezzanine edge run
  - right boundary: right stair wall
  - top boundary: ceiling structure / exposed joists
  - bottom foreground band: floor-level debris band (IMAGE 1) → polished floor (final IMAGE)

Lighting Ladder:
  IMAGE 1-3: ambient only
  IMAGE 4-7: temporary work light active
  IMAGE 8-9: fixture install in progress
  IMAGE 10: partial practical activation
  IMAGE 11: final practical stabilization
```

## Key Composition Notes

1. **Elevated camera** breaks the standard 1.6m default — justified by the multi-level space requiring a high-corner view to capture both floors.
2. **10 beats** is at the high end — justified by the complexity of two-level work (lower floor + mezzanine + stair + windows + wiring + walls + floor + lighting + reward). Real-trade order is enforced: envelope (windows) closes before finishes, wiring runs before the skim coat covers it, the floor is polished only after all overhead and heavy work, and fixtures light up only after their wiring exists.
3. **Object ledger** must track items across levels: debris bags on lower floor vs. mezzanine, scaffolding position, tool cases.
4. **Stair as landmark**: The stair run is both a fixed landmark and a construction target — its geometry lock prevents it from being "redesigned" while its surface state advances through the ladder.
5. **Final reward**: Person descends stairs, walks on polished lower floor. ASMR footsteps on polished concrete mandatory. Coaxial push-in from mezzanine level following the descent path.

## Validation Points

- [ ] Camera DNA Block at 3.2m, not 1.6m — attitude pinned as locked steep downward pitch with consistent vertical convergence (no horizon reference: a steep downward shot cannot hold a mid-frame horizon)
- [ ] All 11 IMAGE anchors use identical Camera DNA Block
- [ ] Two rear windows locked in geometry — never becomes 1 or 3
- [ ] Window installation (beat 4) precedes all finish work — no polished surfaces next to open window holes
- [ ] Wiring rough-in (beat 5) precedes skim coating (beat 6); no fixture lights before beat 9
- [ ] Stair direction locked — always right-side rise
- [ ] Left mezzanine edge never changes horizontal position
- [ ] Floor polishing (beat 8) comes after every overhead and heavy trade — no worker walks tools over the finished floor except the fixture beat, which leaves floor-protection traces
- [ ] Lower floor debris → polished concrete is a gradual progression
- [ ] Final VIDEO uses coaxial push-in with named floor material
- [ ] 9 construction VIDEOs + reward VIDEO + correct IMAGE count (11 IMAGEs)
