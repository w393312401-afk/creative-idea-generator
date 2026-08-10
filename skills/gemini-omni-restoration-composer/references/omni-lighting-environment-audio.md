# Omni Lighting, Environment, And Audio Layers

Three cross-cutting layers that must stay locked across an entire prompt pack. They are
what makes six separate shots and N separate anchors read as one continuous shoot rather
than N unrelated renders.

---

## 1. Lighting Phase Ladder

One prompt pack runs one lighting logic. Five fixed phases, in order:

```
ambient only
→ temporary work light active
→ fixture install in progress
→ partial practical activation
→ final practical stabilization
```

Rules:

- Each IMAGE anchor sits on exactly one phase.
- Anchor to anchor, the phase may **hold** or advance by **+1**. Never skip a phase, never
  go backwards.
- A phase advance requires the physical cause on screen in the VIDEO between the anchors:
  a work light is carried in and switched on, a fixture is bolted up, a switch is thrown.
- No day/night jumps without a stated cause.
- Key-light direction, shadow fall, white balance, and exposure character are inherited
  across every phase. The phase changes what is emitting light, not where the sun is.

### Interaction with the Power Chain veto

`partial practical activation` and `final practical stabilization` require that an earlier
beat installed the wiring **before** the panels that hide it, and — for off-grid carriers —
that a visible power source (solar panel, battery bank, generator) was installed on camera.
A lamp that lights without that chain fails P0. See `omni-restoration-continuity.md`.

### Interaction with the UGC layer

The phase names the light source; the UGC layer names how the phone reacts to it:

- `ambient only` → phone underexposes shadows, low-light sensor noise in corners
- `temporary work light active` → hot blown highlight around the lamp head, hard shadow edges, exposure pumping when the lamp enters frame
- `partial practical activation` → mixed color temperature between daylight and the new practical, visible color cast
- `final practical stabilization` → phone settles, warm cast dominates, window edges clip

---

## 2. Passive Environmental Timelapse Layer

Ambient motion is what tells the viewer time is passing while a static anchor holds. Lock
its **direction** for the whole pack; vary only the observational detail per VIDEO.

### Exterior default (cloud + wind + water)

```
cloud bands stream [direction] across the overcast sky, roadside grass and tree leaves sweep in the same direction, and the water surface shows fast downstream streaks and shifting silt ripples
```

### Underwater / wet interior default (caustic + sediment + shimmer)

```
water-caustic reflections drift faster across the floor and wall panels, suspended sediment slides past outside the glass, and the green filtered light shimmers
```

### Enclosed dry interior default (dust + light patch + draft)

```
fine dust motes drift through the work-light beam, the daylight patch from the doorway creeps slowly across the floor, and loose sheeting edges tremble in the draft
```

**Hard rule**: never describe sky, clouds, weather, or a horizon inside an enclosed
interior shot. Use only passive motion that is physically visible within the enclosed
frame. This is a P0 failure, not a style note.

### Mandatory variation pattern

Identical environmental sentences copied across clips read as a template loop and trip the
Phrasing Repetition Gate. Keep the direction; escalate the observation:

- VIDEO 1: `cloud bands drift left-to-right across the overcast sky, roadside grass leans steadily in the same wind`
- VIDEO 3: `the same left-to-right cloud bands now carry thinner breaks of pale light, and the grass tips bend further`
- VIDEO 5: `the cloud bands continue left-to-right but are denser and darker, and the grass sways in gusts`

Direction locked, detail escalating. The reverse — changing direction, holding detail — is a
continuity failure.

### Placement inside the shot structure

Environmental motion belongs in the wide working shot and the returning wide shot — the two
framings that can see it. The inserts should not describe sky or weather; they cannot see it.

---

## 3. Audio Texture Defaults

When `<audio>` is supplied, follow the audio-sync rules in `omni-multishot-language.md`.
When no audio reference exists, every VIDEO still closes with diegetic sound written as
ordinary prose. Pick from the operation's row:

| Beat Type | SFX | Ambient bed |
|---|---|---|
| Debris clearing | glass crunch, bag rustle, shelf scrape | hollow room tone, light wind |
| Excavation | shovel scrape, soil thud, root snip | forest wind, muted machinery |
| Demolition / prying | nail shriek, board crack, dust patter | echoing shell, distant birds |
| Structural repair | drill taps, welder crackle, pipe drag | worksite hum, restrained echo |
| Rough-in wiring | cable slap, staple punch, conduit click | enclosed room tone, faint hum |
| Installation / paneling | bolt ratchet, panel thud, wood tap | enclosed room tone, soft wind |
| Coating / finishing | brush scrape, roller hiss, water drip | quiet room tone, ventilation hum |
| Threshold crossing | bootsteps, gear rustle, sole-on-concrete | work-light hum, threshold air tone |
| Final reward | echoing footsteps on [name the floor material] | environment-specific ambient (drip, hum, wind, birdsong) |

Rules:

- Name the material the sound comes from. `footsteps` is filler; `boot soles on new
  pine boards` is production information.
- The SFX must match the operation actually shown. Weld crackle in a painting beat is the
  audio equivalent of borrowing traces from another trade, and fails P1.
- The final reward beat's diegetic footsteps are mandatory — they are what makes the
  finished space read as enterable rather than as a render.
