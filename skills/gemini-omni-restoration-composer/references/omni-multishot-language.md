# Omni Multi-Shot Language

Every VIDEO prompt in this skill must be an edited multi-shot sequence, with UGC-like phone
capture imperfections layered into each shot. What that sequence is, though, is not a tour
of shot scales.

## One Main Working Shot, Cut By One Or Two Close-Up Inserts

**A beat is one sustained working shot, interrupted by one or two close-up inserts, and cut
back to the same camera setup to land.** That is the whole grammar.

| Clip length | Shots | Structure |
|---|---|---|
| 4s | 3 | **wide working shot** → close-up insert → returning wide shot |
| 6s | 3 | **wide working shot** → close-up insert → returning wide shot |
| 8s | 4 | **wide working shot** → close-up insert → extreme close-up insert → returning wide shot |
| 10s | 4 | **wide working shot** → close-up insert → extreme close-up insert → returning wide shot |

Longer clips do not buy more shot scales. They buy a second insert and a longer main shot.

**The first and last shots are the same camera setup** — same position, same framing, same
focal length — differing only in how far the work has got. This is the point of the
structure: the first-frame anchor and the last-frame anchor land in one composition, so
anchor continuity is a property of the camera rather than something the prose has to keep
re-asserting across five changing scales. The returning shot must say so, in words: `the
same camera setup as the opening wide working shot`.

**Never write a shot-scale rotation.** The retired grammar's names — `establishing long
shot`, `full shot`, `medium shot`, `wide outro shot` — are banned outright, and writing one
is a P0 rewrite, not a stylistic wobble. Two things go wrong at once when they appear: every
shot drops under a second at short lengths and reads as flicker, and the camera moves in a
clip whose whole continuity argument rests on it not moving.

**With only one insert (4s and 6s), the second insert's duty folds into it** — the close-up
carries the tool contact *and* at least two persistent traces. A duty is never dropped with
the shot that would have carried it.

**Threshold bridge videos and the final reward video keep their own three-station ladders**,
at every clip length: `wide approach shot → threshold shot → interior wide shot` for a
crossing, and `detail shot → pull-back shot → final wide shot` for the reward. A crossing is
a traverse, not a work beat: it has three natural stations and no work face to insert into.
Both are exempt from the pacing declaration below — they traverse or reveal rather than
compress work — but neither is exempt from the One-Take Ban.

## Cinematic Multi-Shot Narrative Flow (纯自然语言多镜头因果流)

**We do NOT use rigid numeric timestamp tables or robotic cut mark sentences (e.g. `Cut this ten-second clip on these marks...` is forbidden).** Video diffusion models (Veo, Kling, Sora) respond best to **fluent cinematic narrative transitions** that naturally guide camera shifts, macro close-ups, and worker actions in pure English prose:

```text
The sequence opens with a wide working shot of the restoration area in its initial state, where the worker is already positioned and begins [primary action] with [tool]. The camera then cuts in closer to a tight close-up insert on the tool contact point, showing [material physics: mortar extrusion, wood shavings curling, adhesive spreading]. Next, an extreme close-up insert reveals [two persistent craft traces and micro-textures]. Finally, the camera cuts back to a returning wide shot from the exact same camera setup as the opening shot, where the worker continues the visible operation smoothly through to the finished state.
```

Rules:
- Express temporal progression and cut sequences using natural cinematic connectors (`The sequence opens with...`, `Cutting in closer to a close-up insert on...`, `An extreme close-up insert captures...`, `Cutting back to a returning wide shot from the same camera setup...`).
- Never output robotic timecode tables, bracketed seconds (`0.0 to 3.2s:`), or decimal cut marks.
- Keep the entire prompt in 100% natural prose. All counts and dimensions are written in English words.

## Pacing Declaration

Every ordinary construction VIDEO must state its time base once, in prose, so the model
does not render real-time labour or invent instant completion:

`edited construction time-lapse assembled from multiple camera setups, not real-time footage`

Do **not** use the word `continuous` in this phrase. In a cut pack it reads as an
instruction to shoot a oner and collides with the One-Take Ban.

The threshold bridge videos and the final reward video are exempt from the pacing phrase —
they traverse or reveal rather than compress work.

## In-Shot Continuity

Inside each shot, the dominant motion runs from the shot's first moment to its last: no
static holds, no stable starts, no deceleration or settling zones. Compression happens at
the cuts, never by freezing inside a shot. State it once, in prose:

```text
Inside every shot the frame keeps moving from its first to its last moment — handheld drift, ambient motion, and the subject's own action never freeze — while this beat's change advances only during the work shots. The only compressions in the clip fall exactly on the listed cut marks; no shot contains a hold, a stall, or a deferred step that is then delivered all at once.
```

**Do not** instead demand that the change itself advance at an even rate across the whole
clip. Frame motion and beat progress are two different things: the inserts add no progress
by contract, and the returning shot is precisely where a stated same-way compression lands.
A clip cannot be continuously progressing and obey its own shot-level progress locks at the
same time.

## Worker Action From Zero Seconds

The pack's IMAGE anchors remain worker-free render references, but the VIDEO does not spend
time reconciling that boundary. At the first video instant the worker is already at the work
face and makes effective tool contact. No shot is allocated to arrival or departure:

| Shot | Worker state |
|---|---|
| wide working shot | The worker is already positioned at the work zone and makes the first effective tool contact at zero seconds, then works through repeated cycles at full-body scale with tool, material source, and physical weight all visible. |
| close-up insert | The worker's hands / tool contact only. |
| extreme close-up insert | Traces only; the worker may be entirely out of frame. |
| returning wide shot | The worker continues the visible operation through the end of the shot. No exit or empty tail is staged; the scene reaches the state represented by IMAGE N+1. |

Do not name or stage worker entry and exit paths. The worker is already at the active work
face at the first frame and remains engaged through the last frame. Material and debris paths
must still be physically plausible and may cross frame boundaries when the operation requires it.

Machines follow the same lifecycle. Erected plant does not — see the Persistent Site Plant
Exception in `omni-restoration-continuity.md`.

## Shot Reference

Four shots exist in the construction grammar. Every clip uses the main working shot and the
returning wide shot; the extreme close-up insert appears only at eight and ten seconds.

### Wide Working Shot — every clip

Purpose: orient the viewer *and* carry the entire visible advance of this beat. It is the
first-frame anchor and the only shot that moves the work forward.

Progress lock: opens exactly on IMAGE N with zero progress, then advances this beat's change
from zero to roughly three quarters — the first occurrence (first board, first stroke, first
fastener) shown in full from contact to placement, then repeated work cycles. Use progressive
partial-state wording (`-ing`, `partially`, `growing`) throughout; finished-state wording is
forbidden here.

Include:
- full environment, restoration carrier, and weather or ambient motion
- first-frame anchor match, with no state jump at the opening instant
- an empty opening frame, then the worker entering from off-frame and making effective tool contact without pausing, and stepping fully out of frame before the closing moment
- full-body scale, tool visible, material source visible, real physical weight
- one dominant physical action, repeated in visible cycles
- a ladder, scaffold, or standing surface if the task is above arm reach
- physical resistance and rising dust or debris
- phone-capture parameters such as a recent smartphone rear camera, slight off-centre
  framing, mild wide-angle edge distortion, phone auto-exposure settling, and brief focus
  breathing before the phone locks back onto the work area
- a small human re-framing correction that preserves anchor landmarks

Natural prose pattern:

`The clip opens on a wide working shot captured like casual smartphone footage, slightly off-center with mild wide-angle edge distortion and phone auto-exposure settling, matching IMAGE N to show the full [location] and the [carrier] in its [current state] under [lighting], with [worker] already at [work zone] making the first effective contact with [tool] at zero seconds and then repeatedly [verb] [object/surface] as the changed area grows steadily and fine dust settles nearby.`

### Close-Up Insert — every clip

Purpose: show material physics at the contact point.

Progress lock: no measurable progress jump — the insert examines ongoing contact, not a new
state, and the cut back returns at exactly the completion level the cut away left.

Include:
- tool contact
- material deformation
- debris, fluid, fiber, fastener, dust, paint, weld, adhesive, or friction behavior
- audio sync opportunity
- phone-camera imperfection such as minor motion blur, imperfect focus falloff, small blown
  highlights, sensor noise, or compression
- on 4s and 6s clips, where this is the only insert: at least two persistent traces as well

Natural prose pattern:

`A clean cut at the [entry mark in words] drops into a close-up insert on [tool/contact point] with minor handheld motion blur and imperfect focus falloff, capturing the raw material physics as [force] bends timber fibers, showers rust flakes, or sprays fine dust, leaving [visible trace].`

### Extreme Close-Up Insert — 8s and 10s clips

Purpose: prove causality.

Progress lock: still no jump — every trace shown belongs to work already performed on screen.

Include at least two persistent traces and tactile micro-textures. The traces must be
characteristic products of the current operation (roller work leaves stipple, bolting leaves
washer rings, prying leaves pry scars — not traces borrowed from another trade):
- screw heads
- washer rings
- weld beads
- adhesive squeeze-out
- seam shadow
- dust edge
- drag scuff
- clamp mark
- brush overlap
- roller stipple
- drill dust
- cable rub
- pressure imprint
- broken fiber ends
- wood grain, steel texture, or brick porosity

Natural prose pattern:

`A second insert at the [entry mark in words] pushes to an extreme close-up insert on the evidence left behind: [trace one] and [trace two] are clearly engraved or embedded into the porous [surface] texture, with low-light noise, mild compression, natural scratches, and dust edges that prove the physical causality.`

### Returning Wide Shot — every clip

Purpose: return to the opening camera setup and land the anchor.

Progress lock: the remaining repetitions finish through a stated same-way compression at the
cut (`after the remaining boards come loose the same way`), landing exactly on IMAGE N+1 — no
overshoot, no missing elements. Finished-state wording is allowed only here.

Include:
- an explicit statement that this is the same camera setup as the opening wide working shot
- the worker continuing the visible operation through the shot end
- temporary tools, ladders, and scaffolds remaining only when supported by the resulting
  state or the next beat
- a final layout matching IMAGE N+1
- permanent traces, including all changes inherited from earlier beats
- no staged exit and no worker-free tail
- phone-recorded exposure and tone matching the next anchor

Natural prose pattern:

`A final clean cut at the [entry mark in words] returns to the same camera setup as the opening wide working shot, matching the phone-recorded exposure and tone of IMAGE N+1, where — after the remaining [repetitions] are completed the same way — [worker] continues the same visible operation through the last instant as the realistically weathered scene reaches IMAGE N+1 and [persistent traces] remain visible.`

## Progress Across Cuts

Cuts are where models teleport. Enforce these rules at every cut:

- Every shot opens at the completion level the previous shot ended with. A cut never silently advances the work.
- A cut may compress repetitions of an action already shown once in full, and the prose must state it: `after several more panels go up the same way`.
- A cut may never skip the first occurrence of a change type, introduce a new object, or finish a different sub-task.
- Counts of major elements stay identical across a cut unless the change happened on screen or was stated as same-way repetition.
- Cutting away to an insert and back must not move the work: the returning wide shot picks up at the completion level the main working shot cut away on, and only then applies its stated same-way compression.

## Edit Rhythm

Use clean cuts and match cuts. The sequence may feel dynamic, but it must not feel like random montage replacement.

Allowed:
- clean cut
- match cut
- hard cut only if it does not imply a state jump
- rack focus or focus breathing inside an insert
- cutting into an insert and back to the same camera setup
- small phone re-framing correction

Forbidden by default:
- cross-dissolve
- fade-in
- fade-out
- magical transition
- instant transformation
- sudden replacement
- teleport
- rapid montage that skips the physical path

## Phrasing Variation

The shot structure is a fixed skeleton — and a smaller one than it used to be — which makes
template-loop prose the default failure mode of this skill: every beat opening the same way,
using the same clause order, and reaching for the same verbs. With only two shot names in
play, the burden falls entirely on the work description.

Before finalising each VIDEO, compare it against the immediately preceding VIDEO:

- **Required and correct to repeat**: the anchor-binding opening sentence, the shot timeline
  sentence, the four shot names, the same-camera-setup clause, the worker silhouette phrase,
  the pacing declaration, the in-shot continuity sentence, the no-text sentence. These are
  structural and must stay verbatim — the timeline in particular is identical in every beat
  of a pack, because clip length and structure are constant across the pack.
- **Failure if repeated**: the same subsequent sentence template, the same clause order
  inside a shot, the same verb set, the same transition wording between shots, the same
  adjective pairs.

Deliberately vary sentence rhythm, subject phrasing, and verb selection every beat while
keeping every required structural element intact. The same audit applies to adjacent IMAGE
anchors.

If varying the prose would cost a required element, keep the element — trim adjectives
elsewhere instead.

## Audio Sync

If `<audio>` is supplied, align the shot rhythm and diegetic impacts to it.

Examples:
- crowbar pries hit the downbeat
- hammer taps match the percussion
- shovel impacts fall on low-frequency pulses
- brush strokes follow the musical tempo
- ambient wind or water motion follows the audio bed

If no `<audio>` is supplied, provide SFX and ambient noise as ordinary natural prose.
