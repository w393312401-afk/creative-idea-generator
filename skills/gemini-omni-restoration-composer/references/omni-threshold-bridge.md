# Omni Adaptive Threshold and Secondary-Space Protocol

This file is the sole Omni rule source for physical space entry. A transition consumes
additional IMAGE/VIDEO slots; it never steals, merges, or compresses construction milestones.
Every VIDEO still uses the Omni multi-shot grammar, but each transition slot performs only its
declared stage.

## Shot structure within each stage

Every stage uses three shots with two explicit clean cuts, at every clip length (400-word
VIDEO ceiling). Bind its own adjacent anchors; a stage does not inherit the whole crossing's
camera path or construction work cycle.

| Stage role | Three-shot coverage | Camera / progress rule |
|---|---|---|
| `door_hardware_open`, `hatch_hardware_open` | wide working shot → close-up insert → returning wide shot | Opening and return share the same position, framing and focal length. Show only opening the original hardware and its unavoidable local dust/debris. The insert inspects existing contact/hinge/latch evidence without additional opening progress; finish opening in the return. No room cleanup, repair or fit-out. |
| `threshold_partial`, `shaft_descent`, `landing_turn`, `orientation_turn`, `partial_first_look`, `interior_establish` and secondary travel/establish stages | transition working shot → detail insert → landing shot | First and last match that stage's distinct anchors. The working and landing shots perform its actual movement/reveal. The insert retains the same spatial position and reveals no further depth; it cannot skip a rung, sill crossing, landing or turn. No same-camera-return demand for this traversal. |
| Complete, unexpanded crossing | wide approach shot → threshold shot → interior wide shot | Traverse the actual entrance using shared sill/floor/light evidence; opening and arrival viewpoints legitimately differ. |

For `divider_open`, apply the hardware-opening structure in the finished primary space;
the following secondary traversal stages use the travel structure. The user's explicit
one-take request overrides these cut structures directly while keeping the actual topology,
reveal budget and state continuity. Mark the cut checks `用户覆盖／不适用`.

## Frozen ledgers before entry

Carry four records through every prompt and manifest:

- `world_lock`: exact terrain contour, foreground/mid/background landmarks, sky, water,
  vegetation, weather, exposure and key-light direction. After IMAGE 1 acceptance, rendered
  reality replaces the previsualized description.
- `carrier_envelope`: real external proportions and orientation, maximum clear interior and
  the volume no room may exceed.
- `entrance_topology`: opening plane, main/auxiliary role, leaf/cover, hinges, latch/lock,
  gasket, first rung/tread, shaft or steps, landing, depth, turn, gravity, drainage and venting.
- `space_graph`: named site/primary/secondary nodes and visible connecting edges. Never invent
  an unregistered room.

## Sealed-until-crossed rule (applies to every topology below)

The hardware-open slot opens the entry on camera — that is its milestone — but what lies beyond it
must stay **unreadable** in that slot's IMAGE: flat unlit darkness in the opening, with no interior
wall, floor, depth, or registered interior anchor visible through it. State the darkness explicitly.

This is deliberate, and it costs nothing to obey: an opening that shows a small, low-resolution
patch of interior hands the video model a half-invented fact it then has to match while
interpolating the crossing, and when it cannot, the space the camera lands in reads as a different
world — or the camera never fully gets inside. Darkness gives it nothing to reconcile, so the
interior is generated where it is actually being generated: inside the crossing clip.

## Topology-adaptive primary entry

### Vertical axial door — three slots

1. `door_hardware_open`: open the registered leaf and show hinges, latch, gasket and sill; the opening itself reads as unlit darkness.
2. `threshold_partial`: cross the sill locally; retain frame and shared floor line.
3. `interior_establish`: deliver the raw interior from a three-quarter oblique axis.

### Vertical side door — four slots

1. `door_hardware_open`.
2. `threshold_partial`.
3. `orientation_turn`: visibly turn after crossing toward the real long axis.
4. `interior_establish`.

### Horizontal top hatch — five slots

1. `hatch_hardware_open`: hand/pry-bar close detail; cover, hinges, latch, gasket and falling
   dust are readable; show the first rung.
2. `shaft_descent`: descend past fixed rungs with entry daylight above and landing below.
3. `landing_turn`: land, keep gravity vertical, then make the registered ninety-degree turn.
4. `partial_first_look`: reveal only one rust wall, a short rail/floor segment or one old device;
   keep the far wall occluded.
5. `interior_establish`: only now reveal the complete raw room.

The opening size never directly implies room size. A small hatch may connect to a large carrier
interior only through the registered shaft, landing and turn.

## Entry hardware is a P0 fact

An enterable opening is never a bare square/round hole or a decorative timber frame. It must
show the relevant leaf/cover, hinges, latch/lock, gasket, first rung/tread, landing and necessary
drain/vent hardware. Missing hardware fails the anchor and triggers targeted regeneration.

## Light continuity

First entry allows only doorway/hatch daylight and a carried portable work light. A neglected
space may not have glowing fixed ceiling lights. Fixed practicals turn on only after an earlier
beat visibly installs power source, wiring and fixtures. Entry/back light keeps the same direction
and colour across the threshold.

## Reveal budget & DLSP 5-layer depth structure

Partial stages retain orientation evidence and hide the far wall. The full room overview is a
later establish stage. In 9:16 vertical long-axis spaces, **default to a three-quarter oblique establish (wide 3/4 diagonal corner perspective)**; strictly forbid centered one-point perspective corridor overviews that trigger extreme tunnel/bowling-alley stretching.

Every interior establish prompt (`interior_establish` / its settled IMAGE) carries a compact
**Depth-Layered Spatial Protocol (DLSP)** in natural prose:

1. **Camera geometry**: eye-level wide-angle, three-quarter diagonal oblique perspective
   from the near corner, with a lens/height appropriate to the actual carrier. Spell any
   measurements in English words; do not force one numerical room template on every shell.
2. **Immediate foreground**: the named entrance hardware stays near the entry margin while
   the crossing camera can see it. At the settled interior viewpoint, it may remain near a
   frame edge or lie physically behind the camera; state which. Never relocate it to another
   physical wall. A later visibly reached reverse viewpoint may see the same original entry
   in its background; name its original boundary and distinguish it from the opposite solid
   wall. Keep an original near floor seam/rung as an orientation tether when hardware leaves view.
3. **Midground**: the real unobstructed raw floor/staging area, with inherited seams and
   material state. A shallow carrier stays shallow; do not invent several metres of floor.
4. **Lateral boundaries**: describe every wall and its complete aperture inventory, for
   example two original windows only on the left and solid material on the right and rear.
5. **Background and envelope**: close the real far wall at the declared door-width,
   door-height and countable-bay proportions. Repeat the short envelope/roof lock.
6. **Distortion restraint**: one concise sentence forbids stretched depth, invented rear
   glass/openings and entrance hardware moved to another physical boundary. No numeric weights,
   parenthesized negative lists or invented Grid cells; the output reference owns the
   runtime-supplied IMAGE Grid exception.

## Scale and camera families

Use entrance detail, shaft axis, landed partial, oblique establish, floor/rail low angle, wall
graze and far-wall reverse. Within a construction anchor family the camera stays locked. A family
change requires a visible no-work reframe. The same centered family may cover at most three
consecutive construction milestones. At least once per new space, a transition VIDEO briefly
shows an anonymous worker silhouette together with a standard door, rung/tread or known-size
device; IMAGE anchors remain worker-free.

The `camera_reframe` / `operation: reframe` slot is a no-work change within the same space,
not a new entrance or construction milestone. Use transition working shot → detail insert →
landing shot with two explicit cuts and the 400-word ceiling. Visibly perform the camera move
between its distinct start and end anchors; the insert holds the current position and work
state. Do not replay entry, require a return to the opening camera, add labor cycles or advance
the room's construction state.

A declared far-wall reverse looks back toward the same original entry. Its apparent distance
or screen side may change with the actual camera movement, but its physical wall, hinge side,
aperture inventory and relationship to the original floor seams cannot change. State the new
orientation explicitly; a port wall can appear on the opposite screen side without becoming
the starboard wall. Do not mistake that justified perspective change for a relocated opening.

The first settled IMAGE of every new family is a render stop when rendering is active. Inspect it
before composing dependent frames. For an interior family, confirm the exterior roof form and
pitch remain credible, every opening matches the ledger, the denylist is clean, clear width and
height read correctly in door units, at least one primary landmark is a fixed carrier feature,
the entrance remains on its original registered boundary (near the entry viewpoint's frame
edge, behind it, or visible from a declared reverse viewpoint), and the interior remains in
untouched trauma state.
Nothing checks these pixels automatically. Delete a rejected frame and re-render the same slot
with `--force_regenerate`; a retry is not a new transition or construction beat.

The untouched-trauma half of that check applies only when the family lands in a space **no earlier
beat has worked in**. Where the space sequence returns to an earlier space, the check inverts:
reject the frame if any milestone that space already earned reads as raw, missing, cluttered or
under construction again. Reading a re-entry frame against the first-entry bar rejects a correct
frame and re-renders a restored room back into a ruin. A structural element visible from two
spaces — roof, ceiling, floor slab, glazing — is one object: once an exterior beat delivers it,
it stays delivered across the sill, first entry included.

## Crossing delta budget

Grid-area counting does not apply to a threshold because the viewpoint and enclosure legitimately
replace the whole frame. The crossing is still tightly budgeted:

| Quantity | Allowed delta across the crossing |
|---|---|
| viewpoint / enclosure | the whole beat; this is the only transformation the crossing buys |
| construction progress | zero; no cleaning, clearing, repair, cladding, paint, staging or installation |
| lighting phase, weather, season | zero; exposure adaptation is not a phase advance |
| exterior state | zero wherever it remains visible later |
| camera height and lens family | zero except a declared top-hatch descent that requires the move |

The first settled interior IMAGE must therefore preserve at least three established trauma
categories and contain no intervention evidence. If the story needs both entry and interior
progress, allocate the entry first and the clean-out as the next construction beat.

## Secondary-space traversal — never reset from scratch

The primary act continuously shows its concrete divider. If a door already exists, keep it shut
until entry. If no door exists, an earlier construction beat cuts and frames a passable opening
and fits a door/panel. Then allocate four additive slots:

1. `divider_open`: open the named divider in the finished primary space.
2. `secondary_threshold`: cross it while retaining divider edge, primary-space return light and
   a shared floor rail/pipe/cable run.
3. `secondary_partial_first_look`: local raw view; far wall hidden.
4. `secondary_establish`: establish the registered secondary node from a distinct oblique axis.

The first secondary image must retain the divider edge or primary-space return light. Only after
secondary establishment may the entrance leave frame. Construction state is monotonic per
`space_id`: raw secondary material is not a regression of the finished primary space, which stays
finished through the visible connection.

## Hard failures

- exterior terrain, sky, water, vegetation, exposure or light direction drifts before entry;
- carrier appears without access route, ruts/prints and proportional spoil evidence;
- bare opening lacks required hardware;
- interior exceeds the carrier envelope or an unregistered room appears;
- hatch/shaft/landing/turn/gravity chain is incomplete;
- shared sill/rail/utility or motivated light disappears across the boundary;
- partial-first-look gives away the full room;
- second space arrives by hard cut, teleport or `reset from scratch`;
- fixed lights glow before power/wiring/fixture installation;
- one centered camera family repeats for more than three construction milestones.
- a crossing also advances construction, lighting phase, weather, exterior state, or undeclared camera height/lens;
- the first settled interior family is composed onward without its render-stop inspection when rendering is active;
- both interior primary anchors are movable, or an interior IMAGE omits the envelope signature, roof form, or aperture constraints.

Any failure rewrites only the implicated beat and redraws only its target frame. Allow the initial
render plus two targeted repair attempts. If still failing, mark `needs_human_review` and stop all
dependent downstream generation.
