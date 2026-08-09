# Omni Multi-Shot Language

Every VIDEO prompt in this skill must be an edited multi-shot sequence, with UGC-like phone
capture imperfections layered into each shot. How many shots depends on how long the clip is.

## Shot Ladder By Clip Length

Gemini Omni's Flow panel offers 4, 6, 8, and 10 second clips. A shot needs at least about
1.3 seconds to read as a shot rather than a flash, and the work shot needs at least 1.9
seconds to show a first full occurrence followed by repeated cycles. That fixes the ladder:

| Clip length | Shots | Ladder |
|---|---|---|
| 4s | 3 | establishing long → **medium** → wide outro |
| 6s | 4 | establishing long → **medium** → close-up → wide outro |
| 8s | 5 | establishing long → full → **medium** → close-up → wide outro |
| 10s | 6 | establishing long → full → **medium** → close-up → extreme close-up → wide outro |

**Never dropped, at any length**: the establishing long shot (it *is* the first-frame
anchor), the medium shot (the only shot that carries this beat's progress), and the wide
outro shot (it *is* the last-frame anchor).

**Refill order as the clip gets longer**: close-up, then full shot, then extreme close-up.
The close-up outranks the full shot because full-body context can fold into the medium shot's
first clause, while causal traces have nowhere
else to live.

**A dropped rung's duties move to its neighbour — they are never dropped with it.** Without
a full shot, the worker is already at the work face and begins effective action at zero seconds.
Without an extreme close-up, the two persistent traces are shown inside the close-up.
Without either, both land in the medium shot.

**Never add a rung this ladder does not have.** Writing the familiar six scales into a four
second clip is the default failure mode at short lengths, and it is worse than dropping
them: six shots in four seconds pushes every shot under a second, which reads as flicker
rather than coverage. Fold the duty in; do not open another shot.

**Threshold bridge videos and the final reward video use their own three-station ladders**,
at every clip length: `wide approach shot → threshold shot → interior wide shot` for a
crossing, and `detail shot → pull-back shot → final wide shot` for the reward. A crossing
has three natural stations; cutting it finer only chops one movement into pieces. Both are
exempt from the pacing declaration below — they traverse or reveal rather than compress
work — but neither is exempt from the One-Take Ban.

## Shot Timeline

Naming the shot scales is not enough: without stated cut marks the model picks its own, and
the six named scales collapse into two long ones. Every VIDEO body therefore carries one
timeline sentence, placed immediately after the anchor-binding opening sentence:

```text
Cut this ten-second clip on these marks and hold no other cuts — an establishing long shot from 0.0 to 1.6, a full shot from 1.6 to 3.1, a medium shot from 3.1 to 5.3, a close-up from 5.3 to 6.9, an extreme close-up from 6.9 to 8.3, and a wide outro shot from 8.3 to 10.0 seconds.
```

Rules:

- Time is split by shot weight, not evenly. The medium shot always gets the largest slice.
- Marks are monotonic and gapless: each shot starts exactly where the previous one ended,
  the first starts at `0.0`, and the last ends exactly on the clip length.
- Each shot's own sentence restates its entry mark **in English words** (`A clean cut at the
  one-and-a-half-second mark moves to a full shot ...`), so prose and timeline bind twice.
- **This sentence is the only place in the prompt body where arabic digits may appear** —
  see the Timecode exemption in `omni-output-templates.md`. Every other count stays an
  English word.

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
clip. Frame motion and beat progress are two different things: the establishing and full
shots carry zero progress by contract, the close-up and extreme close-up add none, and the
wide outro is precisely where a stated same-way compression lands. A clip cannot be
continuously progressing and obey its own shot-level progress locks at the same time.

## Worker Action From Zero Seconds

The pack's IMAGE anchors remain worker-free render references, but the VIDEO does not spend
time reconciling that boundary. At the first video instant the worker is already at the work
face and makes effective tool contact. No shot is allocated to arrival or departure:

| Shot | Worker state |
|---|---|
| establishing long | The worker is already positioned at the work zone and makes the first effective tool contact at zero seconds. |
| full | The same active operation continues with full-body scale, tool, material source, and physical weight visible. |
| medium | The worker performs repeated work cycles. |
| close-up | The worker's hands / tool contact only. |
| extreme close-up | Traces only; the worker may be entirely out of frame. |
| wide outro | The worker continues the visible operation through the end of the shot. No exit or empty tail is staged; the scene reaches the state represented by IMAGE N+1. |

Do not name or stage worker entry and exit paths. The worker is already at the active work
face at the first frame and remains engaged through the last frame. Material and debris paths
must still be physically plausible and may cross frame boundaries when the operation requires it.

On ladders with no full shot, full-body context moves into the medium shot; direct work still
begins in the establishing shot at zero seconds.

Machines follow the same lifecycle. Erected plant does not — see the Persistent Site Plant
Exception in `omni-restoration-continuity.md`.

## Shot Reference

The full six-rung vocabulary, in ladder order. Shorter clips use the subset named in the
Shot Ladder table above; a rung that is not in this clip's ladder hands its duties to its
neighbour rather than disappearing.

### Establishing Long Shot — every ladder

Purpose: orient the viewer.

Progress lock: exactly IMAGE N — zero progress on this beat's change.

Include:
- full environment
- restoration carrier
- weather or ambient motion
- first-frame anchor match
- no major state jump yet
- phone-capture parameters such as a recent smartphone rear camera, slight off-center framing, mild wide-angle edge distortion, and phone auto-exposure settling

Natural prose pattern:

`The sequence opens with an establishing long shot captured like casual smartphone footage, slightly off-center with mild wide-angle edge distortion and phone auto-exposure settling, matching IMAGE N as the first frame to show the full [location] and the [carrier] in its [current state] under [lighting].`

### Full Shot — 8s and 10s ladders

Purpose: show the human, tool, and material already engaged at the active work face.

Progress lock: the first effective contact happens at zero seconds and work begins advancing.

Include:
- worker or machine already working at zero seconds
- full-body scale or full object scale
- tool already visible
- material source visible
- first effective tool contact
- physical weight
- ladder, scaffold, or standing surface if the task is above arm reach
- small human re-framing correction that preserves anchor landmarks

Natural prose pattern:

`The opening establishing long shot begins at zero seconds with [worker/machine] already at [work zone], making the first effective contact with [tool/material]; a clean cut moves closer to a full shot while the same operation continues under real physical weight and the locked layout remains stable.`

### Medium Shot — every ladder

Purpose: show the main operation.

Progress lock: visible work advances this beat's change from zero to roughly three quarters, beginning with the first occurrence shown in full (first board, first stroke, first fastener) and continuing through repeated cycles. Use progressive partial-state wording (`-ing`, `partially`, `growing`), never finished-state wording.

Include:
- one dominant physical action
- repeated work cycle
- object or surface changing progressively
- no second structural system
- physical resistance and rising dust/debris
- brief focus breathing before the phone locks onto the work area again

Natural prose pattern:

`A match cut moves into a medium shot where [actor] repeatedly [verb] [object/surface] with [specific tool], tensing their muscles with each physical stroke, while the phone briefly breathes focus before locking again and the changed area grows steadily as fine dust or debris settles nearby.`

### Close-Up — 6s, 8s and 10s ladders

Purpose: show material physics.

Progress lock: no measurable progress jump — the close-up examines ongoing contact, not a new state.

Include:
- tool contact
- material deformation
- debris, fluid, fiber, fastener, dust, paint, weld, adhesive, or friction behavior
- audio sync opportunity
- phone-camera imperfection such as minor motion blur, imperfect focus falloff, small blown highlights, sensor noise, or compression

Natural prose pattern:

`A close-up isolates [tool/contact point] with minor handheld motion blur and imperfect focus falloff, capturing the raw material physics as [force] bends timber fibers, showers rust flakes, or sprays fine dust, leaving [visible trace].`

### Extreme Close-Up — 10s ladder only

Purpose: prove causality.

Progress lock: still no jump — every trace shown belongs to work already performed on screen.

Include at least two persistent traces and tactile micro-textures. The traces must be characteristic products of the current operation (roller work leaves stipple, bolting leaves washer rings, prying leaves pry scars — not traces borrowed from another trade):
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

`An extreme close-up lingers on the high-detail textures and evidence left behind: [trace one] and [trace two] are clearly engraved or embedded into the porous [surface] texture, with low-light noise, mild compression, natural scratches, and dust edges that prove the physical causality.`

### Wide Outro Shot — every ladder

Purpose: return to anchor continuity.

Progress lock: the remaining repetitions finish through a stated same-way compression at the cut (`after the remaining boards come loose the same way`), landing exactly on IMAGE N+1 — no overshoot, no missing elements. Finished-state wording is allowed only here.

Include:
- worker or machine continues the visible operation through the shot end
- temporary tools, ladders, and scaffolds remain only when supported by the resulting state or the next beat
- final layout matches IMAGE N+1
- permanent traces remain, including all changes inherited from earlier beats
- no staged exit or worker-free tail
- phone-recorded exposure and tone matching the next anchor

Natural prose pattern:

`A final clean cut returns to a wide outro shot, matching the phone-recorded exposure and tone of IMAGE N+1, where [worker/machine] continues the same visible operation through the last instant as the realistically weathered scene reaches IMAGE N+1 and [persistent traces] remain visible.`

## Progress Across Cuts

Cuts are where models teleport. Enforce these rules at every cut:

- Every shot opens at the completion level the previous shot ended with. A cut never silently advances the work.
- A cut may compress repetitions of an action already shown once in full, and the prose must state it: `after several more panels go up the same way`.
- A cut may never skip the first occurrence of a change type, introduce a new object, or finish a different sub-task.
- Counts of major elements stay identical across a cut unless the change happened on screen or was stated as same-way repetition.

## Edit Rhythm

Use clean cuts and match cuts. The sequence may feel dynamic, but it must not feel like random montage replacement.

Allowed:
- clean cut
- match cut
- hard cut only if it does not imply a state jump
- rack focus or focus breathing inside a close-up
- insert shot
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

The shot ladder is a fixed skeleton, which makes template-loop prose the default failure
mode of this skill: every beat opening the same way, using the same clause order, and
reaching for the same verbs.

Before finalising each VIDEO, compare it against the immediately preceding VIDEO:

- **Required and correct to repeat**: the anchor-binding opening sentence, the shot timeline
  sentence, the shot-scale names, the worker silhouette phrase, the pacing declaration, the
  in-shot continuity sentence, the no-text sentence. These are structural and must stay
  verbatim — the timeline in particular is identical in every beat of a pack, because clip
  length and ladder are constant across the pack.
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
