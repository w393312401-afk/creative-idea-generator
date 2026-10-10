# Space Workflows — Composer Quick Reference

> This is a condensed routing table for the prompt composer, and it is **self-contained** —
> everything the composer needs to route a topic is on this page. The fuller construction
> macros live in the peer `restoration-timelapse-engine` skill package, wherever that is
> installed on the current machine; this file previously pointed into one developer's home
> directory, which is a dead link on every other machine and left the model guessing. Do not
> go looking for that file: if a detail is not here, derive it from the
> Construction Phase → Visual Signature map below plus SKILL.md's Construction Sequence
> Validation rules.

## Workflow Routing Table

| Space Type | Default Beats (N) | Standard Workflow Phases | Threshold? |
|---|---|---|---|
| `abandoned property` | 3-6 | hazard clearing → shell repair → service rough-in → surface finish → practical lighting → final carry-out | Standard |
| `exterior facade` | 3-4 | scaffold/prep → masonry/cladding repair → paint/finish → cleanup | Standard |
| `road / street / driveway` | 3-5 | demolition/mill → base repair → paving → striping/furniture | Standard |
| `garage / workshop` | 4-6 | clearing → shell repair → service rough-in → floor/wall finish → bench/storage install | Standard or Threshold |
| `backyard / landscape / pool` | 3-5 | demo/clearing → hardscape → softscape/water → lighting/furniture | Standard |
| `luxury apartment` | 4-6 | demo → rough-in → surface finish → fixture install → staging | Standard |
| `retail / showroom` | 3-5 | gutting → shell repair → service rough-in → fixture install → display staging | Standard |
| `underground space` | 8-12 | site/excavation → structural shell + end walls → drainage + waterproofing → ventilation + power rough-in → floor/wall/ceiling build-up → fixtures → concealment → reward | Threshold |
| `custom build object` | 3-4 | raw material prep → assembly → finishing → detail | Standard |

## Beat Derivation Rules

> **This table is the single authority for `N`.** SKILL.md Step 5 delegates here; it does not
> carry a competing default. Read the space type's `Default Beats (N)` cell above, then apply
> the adjustments below. The only case that overrides the table is an explicit upstream
> `PRODUCTION BEAT LADDER` (Tier 2), where one listed physical phase = one VIDEO.

1. **Range comes from the table**, not from a global default. It varies by space type by
   design: `exterior facade` genuinely finishes in 3-4 beats, `underground space` genuinely
   needs 8-12 because it must install drainage, waterproofing, ventilation and a power feed
   before any finish surface (rule 11 below). Never substitute a generic "3-6".
2. **Hard floor / hard ceiling**: never fewer than 3 VIDEO clips; never more than 19. Above
   the table's range, justify the excess by naming which single-operation splits forced it —
   feasibility rules (Beat Overload, Power Chain, Enclosed-Space Provenance) may push a count
   up, a preference for "more content" may not.
3. **Threshold adds**: exactly ONE TBCP v4 crossing beat (one clip + one settled-interior
   IMAGE — never a two-clip Bridge-1/Bridge-2 split, which was retired in v4) plus typically
   2-4 additional interior beats. See `threshold-bridge-consistency-protocol.md`.
4. **Reward always**: The final VIDEO is always reserved for the reward motion.
5. **Never merge**: Excavation with installation, threshold bridge with construction, construction with reward.
6. **Split overloaded**: If one beat combines 2+ construction systems (e.g., floor finish + lighting + furniture), split into separate beats.
7. **Full enclosure**: For any enclosed space (room, cabin, fuselage, container, vault), wall paneling/painting and ceiling paneling/painting must BOTH be present. Never omit the ceiling/roof treatment when walls are covered. Board the ceiling before the walls (macro order rule 4) so wall panels support and hide the ceiling-board edges.
8. **Fixture completeness (bidirectional)**: If wiring/electrical rough-in is run, light fixture installation must appear as a separate beat before the reward. Conversely, if any practical light, lamp, or powered fixture activates anywhere in the set, an earlier wiring/rough-in beat is mandatory — run before the panels that conceal it — plus a visible power source (solar panel, battery bank, generator) for off-grid carriers; its absence fails validation. If a door frame is built, door leaf/panel installation must appear in a subsequent beat.
9. **Persistent plant**: Scaffolding, formwork, shoring, and cribbing erected in one beat persist across later anchors as static equipment and are removed only in a named temporary-works-strike beat with visible strike traces — they never blink in and out per clip.
10. **Narrative origin**: Every project declares exactly one physical origin: existing restoration, delivered-shell build, or ground-up build. A ground-up build starts from ground and earns excavation, structural shell/arch assembly, end walls and portal before fit-out; a restoration starts with the named existing asset already present. Never switch premises because a title happens to say “BUILD”.
11. **Underground engineering minimum**: Underground rooms must visibly install drainage/collection, waterproofing, ventilation, a traceable electrical feed/source, sealed entrance hardware and a usable access path before finish surfaces. Extension leads and portable work lights do not satisfy permanent services.
12. **Surface-state ledger**: Track floor, walls, ceiling, entrance and utilities separately per registered room. Finished material never reverts to substrate or changes material without a dedicated visible removal/replacement beat. Entering a second room resets only that room's queue; the first room stays visibly complete through the connecting interface.
13. **Temporary equipment**: A portable work light, cable reel, ladder or loose tool may support a real operation but can never be its own milestone. Its entry, inherited position and carry-out remain accounted for.

## Construction Phase → Visual Signature Map

| Phase | Visible Signature |
|---|---|
| Hazard clearing / demo | Workers bagging debris, dragging scrap, carrying braces, dust settling |
| Excavation | Bucket cycles, growing spoil pile, exposed edges, root cuts |
| Structural repair | Welding sparks, patching, cribbing, bracing, membrane rolls |
| Waterproofing / sealing | Membrane application, sealant lines, drainage pipe placement |
| Service rough-in | Conduit runs, pipe installation, junction boxes, wire pulls |
| Floor / wall finish | Trowel application, panel installation, paint rolling, grout lines |
| Fixture install | Light housing mounting, switch wiring, furniture anchoring |
| Ceiling / roof finish | Panel lifting, overhead screwing, curved board bending, overhead paint rolling |
| Practical lighting | Relay clicks, light spill widening, room-tone change |
| Staging / fit-out | Furniture carry-in, shelf mounting, display arrangement |
| Temporary works strike | Workers unbolting scaffold frames, carrying out cribbing, patched tie holes, foot-pad compression marks |
| Concealment | Soil grading, camouflage patching, access cover installation |
| Threshold crossing | Same-axis forward push-in through opening |
| Final reward | Coaxial push-in reveal with ASMR footsteps |

## Camera Default Lookup

Test the **elevated-shot flag first**. It is a property of the *shot*, not of the space type,
so it must be checked before the space-type branches — otherwise every interior/exterior scene
is claimed by the first branch and the elevated case becomes unreachable. (It was written last
in an earlier revision, which made the 3.2m mezzanine camera dead code even though
`examples/double-height-loft-elevated-shot.md` exists precisely to demonstrate it.)

```
if elevated_shot:                       # multi-level space, mezzanine/high-corner view,
    lens = "ultra-wide 14-18mm lens feel"   # or the user asked to shoot down from above
    height = "camera height 3.2m"
    perspective = "steep downward diagonal perspective"
    attitude = "camera pitch locked at the declared steep downward angle; vertical lines
                converge consistently toward the same vanishing direction; no horizon reference"
elif space_type == "custom build object":
    lens = "natural 35-50mm lens feel"
    height = "camera height 1.1m"
    perspective = "subject-centered perspective"
else:                                   # interior, exterior, road, pool, backyard, and
    lens = "ultra-wide 14-18mm lens feel"   # every other space type
    height = "camera height 1.6m"
    perspective = "locked eye-level perspective"
```

**Set `elevated_shot` when** the space has two occupied levels the shot must hold at once (a
mezzanine plus the floor below), when the user asks for a high/downward/overhead angle, or
when a 1.6m eye-level view physically cannot contain the declared landmarks. An elevated shot
never pins a horizon — a steep downward view has none; use the pitch/convergence attitude
lock above (SKILL.md's Sub-Pixel Coordinate Pinning, elevated branch).

## Lighting Default

- **Default**: `soft overcast daytime continuity`
- **Allowed**: `bright natural daylight`, `warm golden hour`, `controlled practical night`
- **Rule**: One set = one lighting logic. No day/night flips.
