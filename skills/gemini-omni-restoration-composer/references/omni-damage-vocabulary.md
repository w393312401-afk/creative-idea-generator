# Omni Damage Vocabulary — Before-State Pathology

The before-state anchor (IMAGE 1) decides whether the whole pack reads as a real
restoration or as a stock "old room" render. Soft-focus damage words produce soft-focus
damage. This file defines how to write trauma so Gemini Omni renders construction-grade
pathology instead of a mild vintage filter.

## Banned Soft-Focus Words

Never use these as the primary damage descriptor:

- `worn`
- `aged`
- `dirty`
- `messy`
- `in disrepair`
- `run down`
- `neglected`
- `weathered` (allowed only as a secondary texture note, never as the damage itself)

They describe a mood, not a physical failure mode. A model cannot render a mood.

## Required Pattern

Every damage clause is three parts:

`location + surface-material state + damage type`

- location: which part of the frame or carrier (`the lower wall span`, `the upper ceiling bay`, `the foreground slab`)
- surface-material state: what the material is actually doing (`plaster spalling`, `coating flake loss`, `moisture runoff staining`)
- damage type: the named failure mode (`lath exposure`, `rust streaking`, `pothole breakout`)

Write two to four such clauses in IMAGE 1, covering different depth zones, so the frame
has damage in foreground, midground, and background rather than one hero crack.

## Reusable Pathology Templates

| Space Type | Pathology Template |
|---|---|
| `road / driveway` | `foreground full-lane asphalt surface shows severe pothole breakouts with exposed aggregate and branching asphalt cracking; the mid-lane wheel path carries oil-darkened staining and loose gravel scatter; the curbside concrete edge shows spalled corners and broken joint lines` |
| `interior room` | `the lower wall span shows full-width plaster spalling with lath exposure; ceiling-to-floor moisture columns stain the mid-left drywall with dark runoff streaking; the foreground subfloor carries scattered demolition rubble and broken finish fragments` |
| `abandoned property` | `the upper ceiling bay shows a collapsed section with hanging insulation and torn vapor barrier; the full-height metal surface carries heavy rust streaking and flaked coating loss; the foreground concrete slab holds broken glass fragments and damp debris accumulation` |
| `timber / bark shell` | `the lower trunk wall shows dry-rot fibre separation with soft punky pockets; the mid-height bark skin carries beetle-galleried channels and loose flaking plates; the floor cavity holds packed leaf litter, animal droppings, and shed bark fragments` |
| `steel vessel / vehicle` | `the hull plating shows scaling rust in lifted brown flakes with pitted metal beneath; the interior bulkhead carries salt-bloom crusting and blistered paint loss; the deck pan holds standing rust-stained water and collapsed fitting debris` |
| `cave / rock chamber` | `the chamber wall shows spalled rock faces with fresh pale fracture scars against dark weathered stone; the ceiling seam carries active seep staining and mineral drip columns; the floor holds angular rockfall blocks and compacted silt drifts` |
| `underground / bunker` | `the concrete wall shows carbonation cracking with rust-jacked rebar breaking through the cover; the ceiling slab carries efflorescence bloom and dark seep patches; the floor holds standing silt-laden water and collapsed conduit runs` |

Adapt the wording — do not paste a template verbatim into a scene it does not fit.

## UGC Interaction

The de-AI capture layer and the damage layer reinforce each other, but they are different
jobs and must both be present:

- Damage layer answers **what is physically wrong with the carrier**.
- UGC layer answers **how a phone recorded it** (blown highlights, sensor noise, handheld tilt).

A prompt that only has UGC artifacts renders a clean room shot badly; a prompt that only has
damage renders a polished CGI ruin. IMAGE 1 needs both.

## Progressive And Reward Anchors

- Progressive anchors keep every unrepaired trauma clause from IMAGE 1 verbatim in
  meaning, dropping a clause only in the beat whose named operation repairs it. This is
  the trauma-side application of Cumulative State Inheritance.
- The reward anchor may have all declared trauma repaired, but the carrier's untouched
  exterior character (bark, rust, rock, hull) stays intact — the genre's whole contrast
  depends on the shell not being restored into newness.

## Audit Hooks

- P0: IMAGE 1 uses a banned soft-focus word as its primary damage descriptor.
- P0: an unrepaired trauma clause disappears from a later anchor without a named repair beat.
- P1: IMAGE 1 has fewer than two damage clauses, or all damage sits in one depth zone.
