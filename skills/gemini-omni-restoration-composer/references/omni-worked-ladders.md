# Worked Beat Ladders

Reference ladders for carriers that are hard to sequence correctly. These are **examples to
reason from, not templates to paste**. Every one of them still has to pass the
Construction Sequence Dependencies vetoes, the Delta Budget, and the Visible Milestone
Package Rule against the actual topic in front of you.

Use them to check your own ladder's shape: how many beats a full exterior-plus-interior
rebuild really needs, where the rough-in sits, and where the threshold crossing goes.

---

## Ladder A — Exterior shell + threshold + full interior rebuild

The most complete shape in the genre: refurbish the outside, cross in, rebuild the inside.
Written here for a cliff-side cable-car cabin, but the skeleton transfers to any vessel,
vehicle, or small structure with a real door.

| # | Beat | Type | Notes |
|---|---|---|---|
| 1 | Exterior rust scraping | `removal` | Full declared face, not a patch. Spoil = rust flakes, must land somewhere. |
| 2 | Exterior sanding and levelling | `surface_prep` | Leaves sanded matte bands as traces. |
| 3 | Primer coating | `coating` | Next anchor shows it **cured**, matte. |
| 4 | Exterior finish coating | `coating` | Primer must be cured first — hard veto otherwise. |
| 5 | Platform railing installation | `fixture_install` | Bolted; washer rings + drill dust as traces. |
| 6 | Door opening, interior still unreadable | `threshold` prep | The opening reads as unlit darkness; the registered interior landmarks must be **pre-existing** cabin features, revealed only by the crossing. |
| 7 | Roof rail and bracket installation | `fixture_install` | Prepares the solar mount. |
| 8 | Solar panel placement and cable clipping | `fixture_install` | This is the power source the Power Chain veto requires. |
| 9 | Work-light stabilisation | `fixture_install` | Lighting phase → `temporary work light active`. |
| 10+ | **Adaptive threshold slots** | `threshold_bridge` | Insert the 3/4/5-stage axial/side/top-hatch protocol; no construction work is displaced. |
| 12 | Interior debris removal | `removal` | Volume must be conserved — room-scale debris needs real containers or trips. |
| 13 | Interior wall base preparation | `surface_prep` | |
| 14 | Floor joist and subfloor installation | `enclosure` | Creates the level working platform everything later stands on. |
| 15 | Interior electrical wiring | `rough_in` | Fed from beat 8's cable entry. **Before** any panel closes over it. |
| 16 | Ceiling panel installation, light opening pre-cut | `enclosure` | Ceiling before walls. |
| 17 | Wall panel installation | `enclosure` | Wall panels rise into and conceal the ceiling-board edges. |
| 18 | Light fixture installation and activation | `fixture_install` | Legal only because beats 8 and 15 exist. Lighting phase → `partial practical activation`. |
| 19 | Floor surface finishing | `interior_finish` | After all overhead and wet work. |
| 20 | Furniture reward reveal | `furnishing` / `reward_cycle` | Contains nothing not installed or carried in earlier. |

Why this shape is long: the crossing costs topology-dependent additive beats, the power chain costs two
(source + wiring), and enclosure costs two (ceiling, then walls). Cutting any of those pairs
to one beat is what produces the classic failures — a lamp with no wiring, a wall with no
ceiling edge, a teleport through a doorway.

If the user wants it shorter, cut **scope** (drop the solar system and run the pack on
ambient light only; or refurbish the exterior only), never cut the dependency pairs.

---

## Ladder B — Interior-only room rebuild (no threshold)

Single continuous space, camera never crosses a boundary. Six beats plus reveal.

1. Debris clearing and strip-out — `removal`
2. Structural repair of the damaged wall or ceiling bay — `surface_prep`
3. Electrical rough-in — `rough_in`
4. Ceiling panels, then wall panels — `enclosure` (split into two beats if the delta budget says so)
5. Primer, then finish coat — `coating` (two beats; primer must cure between them)
6. Floor finishing — `interior_finish`
7. Furnishing and reward reveal — `reward_cycle`

---

## Ladder C — Excavated / carved carrier (cave, rock, trunk, earth)

The provenance-sensitive shape. The interior does not exist yet, so it must be created on
camera.

1. Exterior clearing around the opening — `removal`
2. Portal cutting or carving — `excavation` (this is where the shell is opened)
3. **Adaptive threshold sequence** — `threshold_bridge` ×3/4/5 by opening topology
4. Interior excavation — `excavation` (**mandatory**: the chamber must be dug, or beat 2 must have explicitly declared a pre-existing natural cavity)
5. Mucking-out — `removal` (the spoil from beat 4 has to physically leave; volume conservation applies hard here)
6. Floor levelling and platform — `enclosure`
7. Rough-in, if the destiny has any powered element — `rough_in`
8. Lining or panelling — `enclosure`
9. Finishing — `interior_finish`
10. Furnishing and reward — `reward_cycle`

Beats 4 and 5 are the ones packs skip, and skipping them is exactly the Enclosed-Space
Provenance veto: a finished interior appears inside a shell that was never hollowed.

---

## Sizing Guidance

- Fewer than three construction beats plus a reveal: the transformation will not read.
- More than about twelve: the pack becomes unwieldy to deliver in one message; consider
  scoping to exterior-only or interior-only.
- A threshold crossing costs three axial-door, four side-door, or five top-hatch beats. Never compress it into one, and never take those slots from construction.
- A powered destiny always costs at least one rough-in beat, plus a source beat if off-grid.
- Enclosure of a room costs at least two beats (ceiling, walls) unless only one surface is
  being closed.
