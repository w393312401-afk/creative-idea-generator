---
name: gemini-omni-restoration-composer
description: 专门为 Gemini Omni / Gemini Omni Flash 生成改造延时提示词。接受中文或英文主题、参考图片、参考视频、参考音频、现有改造 brief，也可在无主题时先用「选题发动机」批量产出源源不断、有创新和独特性的延时改造点子。输出 copy-ready 的 IMAGE anchors 和 Gemini Omni 多镜头 VIDEO prompts。强制多镜头组接，远景、全景、中景、近景、特写、结果远景轮换；禁止默认一镜到底。内部严格执行施工顺序依赖、供电链、封闭空间成因、体积守恒、累积状态与因果痕迹契约，对用户屏蔽底层复杂度。P2 去 AI 味从整体画面开始，默认偏 UGC 手机拍摄、真实可用光、轻微过曝、压缩噪点和不稳定构图。对话微调提示词仅在用户明确要求时输出。Trigger this skill when the user asks for Omni提示词, Gemini Omni prompts, Omni Flash video prompts, omni模型, 改造延时提示词, 多镜头改造视频, 反推延时视频提示词, reverse-engineering prompts from an existing restoration time-lapse video, multimodal restoration/renovation timelapse prompts for Gemini Omni, OR asks for topic ideas with 给我点子、帮我想选题、来点延时改造创意、brainstorm topics、选题发动机.
---

# Gemini Omni Restoration Composer

## Core Job

This skill creates copy-ready Gemini Omni / Gemini Omni Flash prompt packs for restoration, renovation, repair, rebuild, construction, and transformation time-lapse videos.

It is a parallel Omni-specific companion to `gemini-veo-restoration-composer`. Do not modify or depend on that skill at runtime. Its production contracts have been absorbed into this skill's own references — adjacent IMAGE anchors, clean static frames, single-operation beats, visible causal traces, construction dependency order, and a Chinese audit report.

The Omni-specific difference is mandatory: every VIDEO prompt uses a multi-shot edit pattern with **stated cut marks**. Do not default to one-take, oner, single continuous take, or one-shot language. The video grammar is a shot ladder sized by clip length — six shots at 10s (establishing long, full, medium, close-up, extreme close-up, wide outro), down to three at 4s (establishing long, medium, wide outro). The ladder table, the never-dropped rungs, and the timeline sentence that pins the cut marks to seconds all live in `references/omni-multishot-language.md`.

The realism difference is also mandatory: de-AI polish starts from the whole image capture style. Unless the user requests polished cinema, default the pack toward UGC-like phone footage, real available light, slight framing instability, overexposed highlight patches, phone auto-exposure shifts, low-light noise, mild compression, and imperfect focus behavior.

## Three Standing Conflict Resolutions

These three pairs of rules pull against each other. They are resolved here once, and the resolution is binding everywhere else.

1. **Six-shot grammar beats every single-take instinct.** There is no exemption for the final reward beat, no exemption for a threshold crossing, and no exemption for a "simple" beat. Reveal pushes and forward moves happen as motion *inside* shots.
2. **Landmarks are locked; framing is loose.** The UGC layer buys handheld tilt, off-centre composition, focus hunting, and exposure pumping. It never buys a primary landmark leaving frame, changing its relationship to another landmark, or changing its frame share between anchors. Full rule in `references/omni-scene-skeleton.md`.
3. **Counts are mandatory; digits are banned.** Write `three roof beams`, never `3 roof beams` and never `70%`. Numerals and percent symbols in a prompt are a leading cause of literal text rendering into the frame. Full rule in `references/omni-output-templates.md`.

## Use This Skill When

- The user asks for Gemini Omni / Gemini Omni Flash prompts for a restoration, renovation, construction, repair, or transformation time-lapse.
- The user says `Omni提示词`, `omni模型`, `Gemini Omni`, `Omni Flash`, `多镜头改造视频`, or asks to adapt an existing restoration prompt set for Omni.
- The user has **no topic yet** and asks for ideas (`给我几个延时改造点子`, `帮我想选题`, `来点创意`, `brainstorm topics`). Run the Topic Ideation Engine first, then offer one-tap composition.
- The user provides multimodal references and expects explicit `<image>`, `<video>`, or `<audio>` usage.
- The user explicitly requests conversational follow-up edit prompts (对话微调提示词) after the base generation prompt.
- The user provides a finished restoration time-lapse video and asks to reverse-engineer (反推) Omni prompts from it.

## Do Not Use This Skill When

- The request is a generic non-restoration story film, product ad, music video, or normal text-to-video prompt.
- The user wants the original restoration prompt format rather than Gemini Omni output.
- The user explicitly requests a single-take / oner style. In that case, ask whether they want to override this skill's default multi-shot contract.

## Required Reference Loading

Load only the reference files needed for the request.

**Always load, every composition run:**

- `references/omni-scene-skeleton.md`: Six-dimensional Omni scene skeleton, UGC phone-capture realism, Location DNA vs. Shot Ladder, anchor stability boundary, frame density.
- `references/omni-multishot-language.md`: Mandatory far/full/medium/close/extreme-close/wide-outro grammar, pacing declaration, direct worker action from zero seconds, phrasing variation.
- `references/omni-restoration-continuity.md`: Continuity, single-operation beats, construction dependency order and hard vetoes, causal traces, occlusion handling, persistent site plant, worker identity lock.
- `references/omni-beat-skeleton.md`: Internal planning layer — temporal physics skeleton, visible milestone package rule, spatial anchoring without coordinates, object persistence.
- `references/omni-damage-vocabulary.md`: Before-state pathology for IMAGE 1; banned soft-focus words.
- `references/omni-lighting-environment-audio.md`: Lighting phase ladder, passive environmental layer, audio texture defaults.
- `references/omni-output-templates.md`: Load before final output. Formatting, length targets, notation ban, audit table rows.

**Load conditionally:**

- `references/idea-engine.md` + `references/used-topic-ledger.md`: Tier 0 only — the user wants ideas, not prompts.
- `references/omni-threshold-bridge.md`: whenever the story crosses from exterior to interior through a real opening.
- `references/omni-worked-ladders.md`: when the beat ladder's shape is uncertain, or the carrier is a vessel/vehicle/cave/excavated shell.
- `examples/minimal-omni-restoration.md`: when you need a concrete formatting example.

## Input Tiers

### Tier 0 - Idea Request (no topic yet)

```text
给我 8 个有创意的延时改造点子
```

The user wants topics, not prompts. Run the **Topic Ideation Engine** (below) using `references/idea-engine.md`. Return a ranked table of novel, deduplicated, buildable topic seeds, each with a ready-to-paste Tier-1 input string. Do not generate IMAGE/VIDEO prompts yet.

### Tier 1 - Minimal Topic

User gives a short topic such as:

```text
做一个废弃石屋改造成隐藏工作室的 Omni 视频提示词
```

Infer:
- carrier
- environment
- before-state trauma
- final identity
- production beats
- final reward action
- optional multimodal reference policy

Default to three to six construction beats plus one final reveal. If a beat combines more than one physical system, split it. Order the beats by real construction dependencies (see Construction Sequence Logic) before writing any prompts.

### Tier 2 - Multimodal Reference Stack

User provides or refers to uploaded material such as `<image>`, `<video>`, or `<audio>`.

Write prompts as if these are callable source variables:
- Use `<image>` for material, silhouette, color palette, object identity, typography, or spatial style.
- Use `<video>` for subject continuity, motion behavior, gesture timing, environmental motion, or before-state evidence.
- Use `<audio>` for rhythm, impact timing, ambient bed, music-reactive motion, or diegetic sync.

Every referenced asset must be named in the relevant IMAGE, VIDEO, or edit prompt. Do not imply a reference silently.

### Tier 3 - Existing Brief Or Prompt Set

If the user provides a production beat ladder, existing restoration prompt set, or detailed brief:
- Preserve the user's topic, route, and construction order.
- Parse any structured brief fields (`VARIABLES`, `PRODUCTION BEAT LADDER`, `ROUTING`, `TIMELAPSE CONTROL`, `PASSIVE ENVIRONMENTAL LAYER`, `VOICEOVER LAYER`, `HANDOFF`) as **binding inputs**. Do not reinvent topic, reward, route, or construction order that the brief already fixed. Set the beat count from the supplied ladder.
- If the user's beat order violates real construction dependencies (for example wiring after wall panels close, or finish coat before primer), surface the conflict and propose the corrected order before writing prompts.
- Convert the video language to Gemini Omni multi-shot natural prose.
- Split overloaded beats into separate videos; expand under-scoped beats to a full visible milestone.
- Keep adjacent anchor binding and causal trace logic.

### Tier 4 - Reference Video Reverse-Engineering (反推模式)

Use this tier when the user provides an actual finished time-lapse video file and asks to reverse-engineer (反推) the Omni prompt pack from it. Follow the dedicated workflow below before entering the normal composition pipeline.

---

## Topic Ideation Engine (选题发动机)

Runs **before** the composition pipeline whenever the user has no topic and wants ideas (Tier 0). Its job is to supply a continuous stream (源源不断) of novel and unique (创新和独特性) renovation topics that are still fully buildable by this pipeline. Full banks, scoring, and dedup logic live in `references/idea-engine.md`; this is the control flow.

### Genre DNA

> A monumental, improbable RAW SHELL nobody expects to be habitable — its wild/ruined exterior left visibly intact — is opened up and built out beat by beat into a warm, finished, lived-in INTERIOR, carrying exactly ONE signature impossible-but-buildable twist.

The dopamine is contrast held in tension: raw huge untouched outside vs. refined cozy inside, plus one "how is that even possible?" hero frame.

### Algorithm

1. **Recombine** — draw one entry from each of the five orthogonal axes: `CARRIER`, `ENVIRONMENT`, `TRAUMA`, `DESTINY`, `SIGNATURE TWIST`. Rotate the carrier *family* (natural → man-made → vehicle → fantasy-grounded) so no two ideas in a batch share a shell family.
2. **Filter**, in order — Orthogonal-Pairing Rule, mandatory single twist, dedup vs. `references/used-topic-ledger.md`, Cliché Blocklist, Buildability Gate, Realism Gate, **Six-Shot Coverage Gate**, Scroll-Stop Test. Drop any candidate that fails.
3. **Score & rank** — rate each survivor on Novelty, Visual Contrast, Twist Strength, Buildability, Scroll-Stop; output the top N (default 8).
4. **Honor constraints** — if the user pins an axis, lock it and recombine the other four.
5. **Deliver** the Idea-Engine Output Contract table, each row carrying a paste-ready Tier-1 input string, and close with: **「回复任意编号，我直接把它生成完整的 Omni 多镜头提示词包」**.
6. **Ratchet** — when the user selects a topic to build, append its Topic DNA to `references/used-topic-ledger.md` before composing, so the next ideation call is forced into fresh space.

A selected idea's Tier-1 string is a valid composition input. Hand it to step 1 of the Internal Composition Pipeline with zero extra questions.

---

## Reference Video Reverse-Engineering Mode (反推模式)

When the input is a real time-lapse video file, run this mode first. Its output (`timelapse_beats.json`) becomes the beat ladder input for the normal composition pipeline, replacing topic inference.

### Two-Stage Isolation (Anti-Priming)

- **Stage 1 - Objective observation**: look only at pixels. Do NOT read any user brief, topic description, reference prompt set, or this skill's output templates while mapping what is visible. Record only what the frames physically show.
- **Stage 2 - Prompt composition**: only after `timelapse_beats.json` is complete, load the brief and the output templates, then enter the normal Internal Composition Pipeline using the beats as the ladder.

Never let knowledge of "what a renovation video usually contains" turn into a claimed observation. If a tool, material, worker, or operation is not visible in reviewed frames, it does not exist.

### Stage 1 Sampling Rules (Time-Lapse Specific)

Run the bundled analyzer first:

```text
python3 scripts/analyze_timelapse_video.py --video <input.mp4> --output-dir <job-dir>
```

Defaults are tuned for time-lapse and should normally not be lowered:
- Base review sampling 2 fps across the whole video (one second of time-lapse can span hours of real work; 1 fps misses whole work stages).
- Edit-cut detection at `--scene-threshold 0.3` (raised because time-lapse frames naturally differ a lot; a normal threshold over-cuts fixed-camera shots into fragments).
- A second low-threshold pass (`--state-diff-threshold 0.08`) detects intra-shot state jumps — work-stage changes inside one fixed camera shot. Each state jump gets a +/-0.5s window densified to 6 fps (`--dense-fps`).
- Cut points get +/-0.2s frames; every scene gets start/mid/end frames; the first 2s (before state) and last 3s (final reveal) are densified to 4 fps.
- `--max-scenes 0` (unlimited) so no shot is dropped.

The analyzer also produces three artifacts that are **gates, not conveniences**:

1. **Keyframe collage** — a 5-column tiled collage written next to the source video as `<video_name>_collage.jpg`. It is the persistent high-density visual reference for the whole transition. If the script reports `collage: FAILED`, stop and fix it before mapping beats; a missing collage means you are about to define beats without ever having seen the sequence as a whole.
2. **`change_events[]`** — every meaningful intra-shot state jump, clustered with a start, a peak, an end, and evidence frames. Each event must end up bound to exactly one beat (`bound_beat_id`) and be named in that beat's VIDEO prompt. An unbound event means a real visible change was dropped from the reverse-engineering.
3. **`analysis_plan[]`** — the exact frames that must go to semantic review. For clips with ninety extracted frames or fewer, that is every frame. For longer clips, at least forty percent, never fewer than one per second, always including the first and last frame, per-second baselines, and every change event's start, peak, end, and the frames adjacent to each peak. Reviewing fewer frames than the plan lists invalidates the beat ladder.

Review discipline:
- Do not judge from contact sheets alone. View individual full-resolution `review_XXX.png` frames when defining beats.
- For key evidence frames (material labels, tool types, surface condition, completion extent), crop-zoom the relevant region before asserting what it shows.
- Prefer three passes over one: Pass A records frame and event facts; Pass B clusters events into beats; Pass C writes prompts and audits coverage back against `change_events`. Do not let a single high-level visual summary become final prompts without a coverage check.
- Write all analysis artifacts to a job directory outside this skill folder. Never write runtime artifacts into the skill folder. (The collage is the one deliberate exception — it belongs next to the source video.)
- Before a new reverse-engineering job, delete leftover frames and beats JSON from previous jobs so stale frames cannot contaminate observation.

### `timelapse_beats.json` Contract

Create `timelapse_beats.json` in the job directory before writing any prompt. Root fields: `video_duration_sec`, `beats[]`, and `banned_elements[]` — a list of objects, tools, materials, workers, or operations that a renovation of this type would plausibly involve but that are NOT visible in any reviewed frame. Banned elements must not appear in any IMAGE or VIDEO prompt.

Each beat must include:
- `id`, `start`, `end`
- `visual_subject`, `visible_details`, `visible_action`, `visible_result` — only what frames actually show
- `state_before`, `state_after` — concrete spatial completion extent (for example `left two-thirds of the wall primed, right third bare plaster`)
- `persistent_traces` — traces this beat leaves that the next state must inherit
- `workers_present` — whether workers/machines are visible in this beat's frames; used to pick clean frames as IMAGE anchor candidates
- `source_event_ids` — every `change_events` entry from `video_overview.json` that this beat accounts for
- `evidence_frames` — at least 1 (ideally 3) concrete `review_XXX.png` or `scene_XXX_*.png` filenames whose timestamps fall inside the beat window; required for every beat that claims an action or result

After mapping beats, check that every `change_events` entry appears in exactly one beat's `source_event_ids`, and check the observed order against the Construction Sequence Logic. If the observed order seems to violate a hard veto (for example paint before primer), re-inspect the frames first — the more likely explanation is a misread frame, not an impossible build.

### Stage 2 Mapping To The Prompt Pack

- Beats become the production beat ladder; split any beat containing more than one dominant physical operation, and expand any beat whose result is only a token patch.
- IMAGE anchors come from clean frames (`workers_present: false`) at or near beat boundaries. The anchor description must match the evidence frame's actual state, including its `persistent_traces`.
- Each VIDEO prompt covers exactly one beat, rendered as this clip length's mandatory shot ladder; the close-up and extreme close-up (where the ladder has them) must use the beat's actual visible tool contact and traces.
- `state_before` / `state_after` become the anchor delta; `persistent_traces` feed the cumulative state rules.
- `banned_elements` is enforced during the P0 gate: any banned element appearing in a prompt is a rewrite-before-delivery failure.
- All normal output contract rules and audit gates still apply.

---

## Internal Composition Pipeline

1. Parse the topic into carrier, environment, trauma state, destiny, reward action, and reference assets.
2. Build a production beat ladder. Each ordinary beat contains exactly one dominant physical operation **and** produces one full visible milestone — never a token patch. Validate the ladder against the construction dependency order, the hard vetoes, and the delta budget before writing any prompts. Consult `references/omni-worked-ladders.md` if the shape is uncertain.
3. Convert every beat into a **Temporal Physics Skeleton** (nine fields, `references/omni-beat-skeleton.md`). A beat that cannot fill all nine is underspecified — fix it now, not in prose.
4. Fix the **Location DNA** (copied verbatim everywhere) and the three primary landmarks, one per depth zone, in relative-position prose. Build the shell Geometry Lock before writing prompts: clear width in door widths, clear height in door heights, depth in countable bays, one roof form at the exterior's own pitch, an exhaustive aperture ledger, an explicit aperture denylist, and the same wall-material family inside and out. Choose one numeral-free envelope signature under twelve words.
5. Register a **Material Palette Lock** for the three to five materials that occupy meaningful frame area. Give each an immutable `substrate` phrase and a monotonic `state_track`; copy the substrate wording verbatim whenever that material is visible and advance its state only in the beat that works it. Then draft the internal progress ledger: for each anchor, the cumulative installed items, counts of major countable elements, completion extent, lighting phase, inherited traces, shell envelope, and current material states. Never include the ledger itself in the output.
6. Create IMAGE anchors for the before state, each progressive state, and the final reward state. IMAGE anchors are clean frames at full-shot scale with no active workers or machinery. IMAGE 1 uses the three-part damage pattern from `references/omni-damage-vocabulary.md`. Every post-crossing interior IMAGE restates the envelope signature, roof form, aperture constraints, wall material, and at least one fixed structural carrier landmark; every IMAGE restates the visible materials' immutable substrate phrases.
7. Create VIDEO prompts between adjacent IMAGE anchors. Every VIDEO starts by binding IMAGE N as first frame and IMAGE N+1 as last frame.
8. Render each VIDEO as a multi-shot sequence in natural prose at this clip length's ladder size, carrying the shot timeline sentence right after the anchor-binding sentence. At zero seconds the worker is already at the active work face and begins the first effective action immediately; keep work visible through the wide outro and never allocate a shot to entering or exiting.
9. Apply the lighting phase, passive environment, and audio layers from `references/omni-lighting-environment-audio.md`.
10. Apply the UGC de-AI capture layer to both IMAGE and VIDEO prompts before wording polish.
11. (Optional, only when the user explicitly requests 对话微调提示词) Add two to three conversational edit prompts for Gemini Omni follow-up refinement.
12. Run silent P0/P1/P2 gates, including the phrasing-variation and notation checks.
13. Deliver one fenced `text` block followed by a Chinese audit table.

---

## Non-Negotiable Rules

### Multi-Shot Contract

Every VIDEO prompt must contain this clip length's full shot-scale ladder, in order, plus the timeline sentence that states where each cut falls:

| Clip length | Shots | Ladder |
|---|---|---|
| 4s | 3 | establishing long, medium, wide outro |
| 6s | 4 | establishing long, medium, close-up, wide outro |
| 8s | 5 | establishing long, full, medium, close-up, wide outro |
| 10s | 6 | establishing long, full, medium, close-up, extreme close-up, wide outro |

The establishing long shot, the medium shot, and the wide outro shot are never dropped at any length. Threshold bridge and reward videos use their own three-station ladders. A dropped rung hands its duties to its neighbour — see `references/omni-multishot-language.md`.

Use clean cuts or match cuts between shots. Do not use cross-dissolve, fade-in, magical transition, instant transformation, montage replacement, or scene teleport language.

### One-Take Ban

Do not write `oner`, `one-shot`, `single continuous take`, `one continuous take`, `one-take`, or equivalent default wording. There is **no exemption** — including the final reward beat. Only use these if the user explicitly overrides the multi-shot contract.

### Pacing Declaration

Every ordinary construction VIDEO states its time base once: `edited construction time-lapse assembled from multiple camera setups, not real-time footage`. Threshold bridge videos and the final reward video are exempt.

### Omni Six-Dimensional Skeleton

Each VIDEO must cover framing and motion, action and physics, lighting, visual style, location, and text rendering policy. If the user did not request visible text, explicitly keep the scene free of captions, signs, subtitles, and rendered prompt text.

### UGC De-AI Capture Layer

Every ordinary prompt pack must include a realistic capture layer before style polish: phone or small consumer camera capture style, imperfect but readable handheld framing, real available light with phone exposure behavior, and at least two plausible image artifacts (blown highlights, sensor noise, compression, chromatic aberration, focus hunting, mild motion blur, edge softness, rolling-shutter wobble). No clean commercial, studio, luxury, or polished cinematic look unless the user explicitly asks.

The image may feel casual, but it must still preserve anchor landmarks and the construction state clearly — see the anchor stability boundary in `references/omni-scene-skeleton.md`.

### Restoration Continuity

Every added, removed, repaired, cleaned, painted, welded, bolted, assembled, dragged, lifted, poured, cut, drilled, wired, installed, opened, or closed element must have a source or entry path, hand/tool/machine contact, a visible movement path, and at least two persistent traces inherited by the next IMAGE anchor.

Before prompt writing, create and carry `world_lock`, `carrier_envelope`, `entrance_topology`,
and `space_graph`. After IMAGE 1 passes visual acceptance, freeze `world_lock` from that actual
render. Every beat also carries `space_id`, `transition_stage`, `camera_family`, `reveal_scope`,
and `light_source_state`. For any physical entry, follow the topology-adaptive additive slot
protocol in `references/omni-threshold-bridge.md`; transition slots never consume construction
milestones. New tasks never use a second-space hard cut or `reset from scratch`.

Nothing may appear, disappear, finish, clean up, align, attach, open, close, or transform without a visible physical cause. Being occluded is not a legal way to leave an anchor — hold hidden objects explicitly.

### Construction Sequence Logic

Order the beat ladder by real construction dependencies. Default macro order:

1. Demolition and debris clearing (structures come down top-first; debris leaves before new work starts in that zone).
2. Structural repair: foundation, framing, load-bearing walls, then roof structure.
3. Rough-in systems: wiring, plumbing, and ducting before any panel closes over them.
4. Enclosure: **ceiling panels, then wall panels** — the wall panels rise into and conceal the ceiling-board edges.
5. Surface finishing: primer before finish coat; wet trades (pouring, plastering) before dry finishes.
6. Floor finishing late, after overhead and wet work that could damage it.
7. Fixture and equipment installation: lighting only after its wiring exists.
8. Furniture and decoration.
9. Final reward reveal.

Hard vetoes:
- No wiring or plumbing work after the panels that would hide it are already installed.
- No finish coat before primer, and no paint over surfaces that still need structural or rough-in work.
- No new roof before the walls or frame that carry it are repaired.
- Removing or cutting a load-bearing element requires visible temporary shoring or bracing inside that VIDEO.
- **Power chain broken** — no practical light activation without an earlier on-camera wiring beat run before enclosure, plus a visible power-source beat for off-grid carriers.
- **Enclosed-space provenance missing** — no interior fit-out inside a chamber whose creation or pre-existence was never shown or stated on camera.
- **Volume not conserved** — container scale, trip count, or spoil-pile growth must account for what was removed or delivered; cut-out solid pieces need their own pry-out and carry-out.

Full statements and rationale in `references/omni-restoration-continuity.md`.

### Cumulative State And Anchor Delta

- Every IMAGE anchor N must inherit all permanent changes and traces from beats 1 through N-1. A trace may disappear only when a later named operation visibly covers or removes it, and that covering operation must be the beat where it happens.
- The difference between IMAGE N and IMAGE N+1 must be exactly the declared operation's result of VIDEO N. No side progress, no extra cleanup, no bonus improvements in other zones.
- Each progressive IMAGE anchor states its completion extent in concrete spatial terms, so adjacent-anchor interpolation has an unambiguous start and end.
- Where a beat's scope is narrow or its object list is closed, say so **negatively and explicitly** (`the side wall panels remain in place, not stripped`; `only these listed objects are present`). Positive description alone does not bound an image model.

### Progress Flow Control

Timelapse must compress time, but only through legal channels. If the prompt offers no legal compression path, the model invents illegal ones — instant completion and pop-in.

- Delta budget: one VIDEO may only carry a plausible amount of change. If an operation alters more than roughly one-third of the visible frame area, or could not believably progress that far within one short video, split the same operation into consecutive quantified beats. Split by extent — never by shrinking the milestone to a token patch.
- Shot-level progress lock: shot 1 shows exactly IMAGE N with zero new progress; shot 2 is staging only; shot 3 advances the beat's change from zero to roughly three quarters through repeated visible work cycles; shots 4 and 5 examine ongoing contact and existing traces without advancing the state; shot 6 lands exactly on IMAGE N+1.
- Cuts carry no progress: every shot opens at the completion level the previous shot ended with.
- First occurrence on camera: the first instance of every change type must appear in full with its causal chain. A cut may compress only repetitions of an action already shown once, and the prose must say so.
- Monotonicity: construction state never regresses, across shots or across anchors.

### Countable Inventory And Reveal Discipline

- State explicit counts for major countable elements in IMAGE anchors and VIDEO prompts, written as English words. Counts may change only through on-camera action or a stated same-way repetition.
- The final reveal anchor may not contain any object that was not installed or carried in during a prior beat.

### Object Persistence

Every object is `inherited in place`, `human-moved` (state the movement and destination), or `human-carried-out` (show the carry-out). Each non-reward VIDEO may introduce at most one new support-object class.

### Material, Access, And Crew Plausibility

- Bulk materials must be staged in a visible stockpile in a prior anchor or visibly carried/delivered into the scene inside the VIDEO before use.
- Removed material must either visibly exit the scene boundary or persist as a stockpile until it does; stockpile volume must roughly match what was removed.
- If a VIDEO ends with wet material, the next IMAGE anchor shows the cured or dried state.
- Work above comfortable arm reach requires a visible ladder, scaffold, or standing surface. Erected scaffolding follows the Persistent Site Plant Exception — it persists across anchors between a named erection beat and a named strike beat.
- Loads beyond one person's plausible capacity require a second worker or a machine.

### Clean Frame Boundary

IMAGE prompts must contain zero active workers and zero active machines. Workers, tools, vehicles, and temporary machines appear only inside VIDEO prompts. In construction VIDEO prompts the worker is already at the work face at zero seconds, acts immediately, and continues through the final shot; no entry or exit shot is used. Parked plant is not an active machine and may remain.

### Worker Identity Lock

Workers are locked as high-contrast, low-detail silhouettes with solid nameable colours and no facial description, repeated identically across every shot and every VIDEO in the pack.

---

## Optional Render Loop

This skill composes text. If — and only if — the local creative-idea-generator service is running at `http://127.0.0.1:8085`, three bundled scripts can also gate, archive, and render the pack. **None of this is part of the default text-only flow.** When no service is configured, skip this section entirely and deliver the prompt pack as text; do not mention the scripts, and do not probe for the server on every run.

Use them only when the user explicitly asks to render, preview, gate, or archive (`作图`, `生成图片`, `渲染`, `预览`, `入库`), or when they have told you the service is running.

| Script | Purpose |
|---|---|
| `scripts/render_and_gate_anchor.py` | Renders the first IMAGE of a shot family and blocks until it is on disk, so it can be inspected before dependent frames are composed. |
| `scripts/save_to_library.py` | Archives the delivered prompt block and audit table to the idea library. |
| `scripts/generate_frames.py` | Triggers rendering of the remaining frames. |

```bash
python3 scripts/render_and_gate_anchor.py \
  --title "<the Chinese topic title>" \
  --prompt_file <path to IMAGE 1 prompt text> \
  --server "http://127.0.0.1:8085"
```

Exit codes: `0` rendered; `2` server unreachable, fall back to plain text delivery in one pass; `3` server error; `4` missing prompt text; `5` timed out while the server was still working — do not treat as unreachable.

The server runs **no** automatic judgement on the frame (2026-08-05: all generation-time consistency review was removed). Show the user the rendered anchor, say plainly that nothing checked it automatically, and let them decide before you compose the rest. The rendered prompt is then authoritative: deliver it verbatim and reconcile dependent prompts against what actually rendered rather than against the pre-visualised plan.

When rendering, stop at the first IMAGE of **every new shot family**, not only IMAGE 1. A single-family run stops once at IMAGE 1. A threshold run stops again at the first settled interior IMAGE; a declared cut to a new family stops at that cut's resulting IMAGE. Pass its real slot number through `--sequence`. Before continuing, inspect roof form and pitch, aperture ledger/denylist, clear width and height in door units, fixed carrier landmark, material substrates, untouched trauma state, and full door-frame clearance. On failure, delete the rejected frame and re-render the same slot with `--force_regenerate`; allow at most three attempts. A retry never creates a new beat or IMAGE number.

`--prompt_file` is preferred over `--prompt`; prompt bodies contain characters that are painful to escape on a command line.

---

## Output Contract

Final output must be:

1. One fenced `text` block containing copy-ready prompts.
2. A Chinese Markdown audit table immediately below the fenced block.
3. Minimal surrounding prose.

Inside the fenced block, use only these Chinese section labels:

```text
图片提示词
图片 1:

视频提示词
视频 1:
```

If the user explicitly requests conversational edit prompts, append an additional section:

```text
对话微调提示词
编辑 1:
```

Rules:
- Each label must be on its own line, never collapsed onto the same line as a prompt body.
- One blank line between each slot.
- Prompt bodies must be English natural-language paragraphs.
- Do not use XML.
- Do not use Markdown bullets, headings, or tables inside the fenced block.
- Do not expose internal acronyms, field names, or structured labels in prompt bodies.
- No percent symbols, arabic digits for counts, coordinate notation, or colons introducing values inside descriptive sentences.
- Do not default to visible text rendering.
- Length targets: exterior and single-family IMAGE 140–180 words with a hard ceiling of 180; post-crossing interior IMAGE 170–220 words with a hard ceiling of 220; VIDEO follows the clip-length table in `references/omni-output-templates.md`. Trim adjectives and boilerplate before ever trimming a required structural element.

---

## Audit Gates

### P0 - Rewrite Before Delivery

- Any VIDEO lacks this clip length's full shot ladder, or lacks the shot timeline sentence.
- Any VIDEO names a shot scale that is not in this clip length's ladder (six scales crammed into a four second clip pushes every shot under a second and reads as flicker).
- Any VIDEO defaults to one-take / oner / one-shot language.
- Any ordinary beat combines more than one dominant physical operation.
- Any ordinary beat delivers only a token patch, a one-corner edit, or a merely-begun state instead of one full named milestone.
- Any object or completed state appears without a source path, contact action, movement path, and persistent traces.
- Any beat order violates the construction dependency order or a hard veto (wiring after enclosure, finish coat before primer, roof before structure, load-bearing removal without shoring, broken power chain, missing enclosed-space provenance, unconserved volume).
- Any IMAGE anchor drops a permanent change or trace from an earlier beat without an explicit covering operation in a named beat — including dropping an object merely because it became occluded.
- Any adjacent anchor pair differs by more than the declared operation's result.
- Any single VIDEO's declared change exceeds the delta budget.
- Any first occurrence of a change type happens off camera or is skipped by a cut.
- The final reveal contains any object never installed or carried in during a prior beat.
- Any VIDEO lacks adjacent first-frame / last-frame binding.
- Any IMAGE includes active workers or machinery.
- Any construction VIDEO with a worker does not place that worker at the active work face at zero seconds with immediate effective tool contact, or spends any shot on worker entrance, arrival, exit, or walk-out.
- A worker's silhouette description changes between shots or between videos.
- Any referenced `<image>`, `<video>`, or `<audio>` is not explicitly used where needed.
- Any prompt defaults to captions, subtitles, prompt text, labels, or rendered typography without user request.
- Any transition language implies cross-dissolve, fade-in, magic, instant transformation, or teleportation.
- Any prompt body contains a percent symbol, an arabic numeral used as a count, coordinate notation, or an internal acronym.
- IMAGE 1 uses a banned soft-focus word (`worn`, `aged`, `dirty`, `messy`, `in disrepair`) as its primary damage descriptor.
- An enclosed interior prompt mentions a horizon, sky, clouds, or weather.
- A primary landmark leaves frame in shot 1, 2, or 6, or changes its relationship to another landmark between anchors.
- The lighting phase skips a step, regresses, or advances without an on-camera physical cause.
- Any interior IMAGE omits the verbatim envelope signature or single roof-form clause, contains an opening absent from the aperture ledger, contains an item on the aperture denylist, or contradicts the exterior roof pitch.
- Any visible registered material omits or rewords its immutable substrate phrase, advances state in a beat that does not work it, skips a state, or moves backward.
- (Rendered flow) The first frame of a new shot family is not shown and inspected before dependent frames are composed; the first settled interior frame fails roof, aperture, scale, fixed-feature, material, untouched-trauma, or door-clearance inspection.
- (Threshold) The topology-adaptive stage order is incomplete; entrance hardware, shaft/landing/turn, shared light/landmark tether or reveal budget fails; a transition slot performs construction work; or a second space arrives by hard cut / `reset from scratch` instead of through the visible divider.
- (Reverse-engineering mode) Any prompt mentions an element listed in `banned_elements`; any beat-derived claim lacks `evidence_frames`; any `change_events` entry is unbound to a beat; the keyframe collage failed to generate; or fewer frames were reviewed than `analysis_plan` requires.

### P1 - Strengthen Before Delivery

- Shot scales repeat instead of rotating far/full/medium/close/extreme-close/wide.
- The close-up lacks tool contact or material deformation.
- The extreme close-up lacks lasting trace evidence.
- The extreme close-up traces are not characteristic products of the current operation (roller work must leave roller stipple, not weld beads).
- A hand tool is described vaguely instead of with specific colour, geometry, and material.
- Bulk materials appear without staging or visible delivery, or removed debris disappears without visible removal.
- Wet material at the end of a VIDEO is not shown cured or dried in the next IMAGE anchor.
- A progressive IMAGE anchor lacks a concrete completion extent.
- Above-reach work lacks a ladder or scaffold, or a single worker handles a load beyond one-person capacity.
- Erected scaffolding, formwork, or shoring appears or vanishes between anchors without a named erection or strike beat; or unset concrete is shown without its formwork.
- A shot opens with more progress than the previous shot ended with (a cut carries progress).
- Result-state wording appears before the wide outro shot.
- Major countable elements lack counts, or counts drift between shots or anchors without on-camera cause.
- State regresses anywhere.
- Lighting, environment, or style does not persist across adjacent anchors.
- The passive environmental layer changes direction between clips, or repeats verbatim instead of escalating its observational detail.
- Audio is vague, or the SFX belongs to a different trade than the operation shown; the final reward lacks named-material footsteps.
- Any wide or establishing shot lacks a foreground / background depth layer, a large flat surface is left as a uniform low-density fill, an anchor reads as an empty frame, or a persistent environmental dressing element drifts, appears, or vanishes between anchors.
- The UGC capture layer is vague, generic, or limited to the word `realistic` without concrete phone-camera artifacts.
- A reflective surface is described as sharp or mirror-like instead of low-gloss and defocused.
- Sentence templates, clause order, or verb sets repeat beat after beat beyond the required fixed structural sentences.
- A scene that could plausibly contain diegetic text (storefronts, signage, workshop labels, road markings) lacks an explicit instruction keeping that text unreadable or out of frame.
- Any slot exceeds its hard length ceiling.
- Audio sync is vague when `<audio>` is supplied.
- (Only when included) Conversational edit prompts cannot be executed independently by Gemini Omni.

### P2 - Polish

- Remove weak image-model filler such as `8k`, `masterpiece`, generic `photorealistic`, or token-stuffing adjectives.
- Actively filter and scrub "AI-style" buzzwords like "perfect", "flawless", "seamless", "pristine", "clean CGI style", "high-end render", or "perfectly aligned".
- Scrub instant-transformation wording from mid-video positions: "transforms", "becomes", "now features", "is now complete", "suddenly", "reveals". Shots 2 through 5 use progressive partial-state phrasing (`-ing` verbs, "partially", "half-covered", "growing"); finished-state descriptions belong only in shot 6 and the IMAGE anchors.
- De-AI from the whole image first: default the capture style toward UGC-like phone footage or casual documentary phone stills unless the user explicitly asks for polished cinema, luxury commercial, or studio production.
- Specify imperfect capture artifacts: handheld phone framing, slight horizon tilt, edge softness, rolling-shutter wobble, autofocus breathing, minor motion blur, sensor noise, mild compression, chromatic aberration, mixed color temperature, crushed shadows, and small blown-highlight patches.
- Prefer real available light over ideal lighting: window glare, harsh overhead bulbs, temporary work lights, doorway backlight, visible color cast, exposure pumping, and uneven shadow falloff are better than clean studio or flat HDR illumination.
- Keep the multi-shot grammar, but make each shot feel recorded by a person on a phone or small consumer camera: imperfect re-framing, small hand corrections, off-center composition, brief focus hunting, and practical occlusions may appear as long as anchor continuity remains stable.
- Use cinematic terms only when they help physical clarity; do not let 35mm film, shallow depth of field, bokeh, or polished color grading override the UGC realism layer.
- Inject tactile micro-textures and natural surface imperfections (weathering, scratches, non-uniform dirt/dust, visible wood grain, concrete pores) to eliminate flat, plastic surfaces.
- Simulate physical resistance, weight, and inertia in all actions (worker shifting body weight under load, tool recoil, friction sparks, flying splinters, natural dust clouds).
- Apply natural lighting and exposure dynamics instead of flat, over-saturated HDR illumination.
- Tighten long sentences.
- Replace vague camera words with professional shot-scale and motion language.

## Delivery Style

Speak to the user in zh-CN unless they ask otherwise. Keep model-facing prompt bodies in English. Deliver the prompt pack directly; do not show the internal plan, the beat skeleton, or the progress ledger. Ask follow-up questions only when the missing detail would change routing or reference handling; for ambiguous short inputs, infer reasonable defaults and proceed.
