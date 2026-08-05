---
name: gemini-veo-restoration-composer
description: 一键生成完整的修复/改造延时提示词集合，并可在无主题时先用「选题发动机」批量产出源源不断、有创新和独特性的延时改造点子。接受自然语言主题描述、手动场景规格、上游选题包，或「给我点子/帮我想选题」这类创意请求；自动构建 Drift Lock Packet、Production Beat Ladder、IMAGE anchor 序列和 anchor-first VIDEO 提示词。默认分阶段执行：先只产出 IMAGE 1 并同步渲染过首帧验收门，通过后才允许撰写其余 IMAGE/VIDEO；除非用户明确只要文本、或渲染服务不可用，禁止一次性输出全套提示词。内部严格执行 restoration-timelapse-engine 的全部连续性契约，对用户屏蔽底层复杂度。Trigger this skill when the user says 生成提示词、做一个延时改造、帮我写提示词、prompt composer、compose prompts、generate restoration prompts, OR asks for topic ideas with 给我点子、帮我想选题、来点延时改造创意、brainstorm topics、选题发动机, or provides a short topic description expecting a complete IMAGE/VIDEO prompt set.
---

# Restoration Prompt Composer

## Core Job

This skill is a **staged, anchor-first prompt composition layer** that sits between topic intent and final prompt delivery. It accepts minimal user input — as short as a single sentence describing a transformation topic — and produces a complete, production-ready `IMAGE 1-(N+1)` + `VIDEO 1-N` prompt set that strictly conforms to the `restoration-timelapse-engine` contracts. "One input" never means "one pass": by default the skill composes and renders `IMAGE 1` first, and only composes everything else after the user has seen the real anchor frame (see the Staged Delivery Contract immediately below).

It does **not** replace `restoration-timelapse-engine` or `tiktok-abandoned-rebirth`. Instead it **internalizes their contracts** and produces output that is indistinguishable from a hand-crafted engine invocation, but without requiring the user to manually build topic briefs, drift-lock packets, or production beat ladders.

## Staged Delivery Contract (P0 — read before composing anything)

**Staged (anchor-first) execution is the DEFAULT for every composition run (Tier 1-4). Do not look for explicit render intent before staging — assume rendering will happen.**

1. Never write the full `IMAGE 1-(N+1)` + `VIDEO 1-N` set in your first pass. Compose `IMAGE 1` only, then call `scripts/render_and_gate_anchor.py` **synchronously in the same turn** and wait for the frame (mechanics in Step 6.5).
2. Only after the script exits 0 and you have shown the rendered anchor to the user may the remaining `IMAGE`/`VIDEO` slots be composed and delivered. The server runs **no** automatic judgement on that frame (2026-08-05: all generation-time consistency review was removed) — never tell the user it "passed review". Show it, say plainly that nothing checked it automatically, and let them decide.
3. Exactly two conditions waive staging and permit the legacy one-pass full-set delivery:
   - the user explicitly asked for prompt text only (e.g. `只要提示词`, `不用作图`, `不用渲染`, `纯文本`), or
   - the render server at `http://127.0.0.1:8085` is unreachable (script exit code 2) — then state plainly `⚠️ 渲染服务未运行，本次跳过首帧预览，直接输出全套提示词`, deliver in one pass, and still make the Step 13 interactive offer.
4. Ambiguity is **not** a waiver. If unsure whether the user wants images, stage anyway: one anchor render is cheap; a wrong anchor can waste the entire downstream image and video budget.
5. Emitting the full prompt set without one of the two waivers is a P0 contract violation, equal in severity to a failed kill gate.

### What This Skill Automates

1. **Topic → Scene Decomposition**: Parse natural-language input into carrier, environment, trauma state, destiny, and reward action.
2. **Beat Planning & Visible Milestone Packaging**: Automatically derive the production beat ladder from the scene's construction logic. Consolidate local filler into completed, unmistakable stage products; allow up to three tightly related same-zone actions when they jointly create one terminal milestone.
3. **Drift Lock Packet Assembly**: Build the complete packet (camera DNA, geometry lock, fixed landmarks, frame boundaries, object ledger, worker choreography, lighting phase ladder, passive environment direction, interest budget).
4. **IMAGE Anchor Sequence**: Render `IMAGE 1-(N+1)` with proper Camera DNA Block inheritance, landmark enumeration, bounded one-delta edits, and concrete preserve lists.
5. **VIDEO Motion Chain (Continuous Action Flow)**: Render `VIDEO 1-N` with anchor-first opening, adjacent-frame binding, and adjacent-image first-to-last motion delta under the strict **Continuous Action Flow Contract** (forcing repeated cycles of "verb-first" active labor and at least 2 measurable progress markers while forbidding jump cuts/cross-fades).
6. **Silent Self-Check & Visible Quality Audit**: Run all P0/P1/P2 gates silently, then compose a structured, visible **Quality Audit & Verification Report** (提示词质量审核报告) to be delivered directly alongside the copy-ready prompt set.
7. **Copy-Ready & Audited Delivery**: Return the final copy-ready prompt set inside a fenced code block, and present the structured Quality Audit Report immediately below it in Chinese.

## Use This Skill When

- The user has **no topic yet** and asks for ideas (`给我几个延时改造点子`, `帮我想选题`, `来点创意`, `brainstorm topics`). Run the **Topic Ideation Engine** (below) first, then offer one-tap composition.
- The user provides a short topic description and expects a complete prompt set (e.g., `做一个废弃阁楼翻新` or `abandoned loft restoration timelapse`).
- The user provides a detailed scene specification with camera, landmarks, and construction phases.
- The user provides reference prompts and wants the same pattern applied to a new topic.
- The user provides a `tiktok-abandoned-rebirth` topic brief and wants the full prompt set generated without manual engine invocation.
- The user says any of: 生成提示词, 帮我写提示词, prompt composer, compose prompts, 一键生成, 做一个延时改造.

## Do Not Use This Skill When

- The user only wants a topic brief without prompts → use `tiktok-abandoned-rebirth`.
- The user has an existing prompt set and wants to edit specific slots → use `restoration-timelapse-engine` directly.
- The user wants static interior design concepts without construction process.
- The user wants fantasy/surreal content without build logic.

---

## Input Acceptance Tiers

### Tier 0 — Idea Request (no topic yet)

```text
给我 8 个有创意的延时改造点子
```

The user wants topics, not prompts. The skill must:
- Run the **Topic Ideation Engine** (see dedicated section below) using [Idea Engine](references/idea-engine.md).
- Return a ranked table of novel, deduplicated, buildable topic seeds — each with a ready-to-paste Tier-1 input string.
- Not generate IMAGE/VIDEO prompts yet. Wait for the user to pick a number, then feed that seed into Step 1 of the Internal Composition Pipeline and append its Topic DNA to [`used-topic-ledger.md`](references/used-topic-ledger.md).

### Tier 1 — Minimal Input (one sentence)

```text
做一个废弃双层阁楼翻新
```

The skill must:
- Infer `Space Type`, camera defaults, lighting, and construction macro from [Space Workflows](references/space-workflows.md).
- Auto-derive 3-6 production beats from the space type's standard workflow.
- Build the entire Drift Lock Packet internally.
- Output the full prompt set without asking follow-up questions.

### Tier 2 — Structured Input (topic brief)

The user provides a `tiktok-abandoned-rebirth` topic brief with `VARIABLES`, `PRODUCTION BEAT LADDER`, `ROUTING`, `TIMELAPSE CONTROL`, `PASSIVE ENVIRONMENTAL LAYER`, `ANCHOR EXPANSION GUIDANCE`, `VOICEOVER LAYER`, and `HANDOFF`.

The skill must:
- Parse all brief fields as binding inputs.
- Set `N` from the production beat ladder.
- Respect routing (Standard vs. Threshold).
- Use the handoff constraints for camera family, motion budget, complexity budget, object-ledger seed, risk slots, and simplification notes.

### Tier 3 — Reference-Based Input (reverse engineering)

The user provides example prompts (IMAGE/VIDEO) and says "apply this pattern to [new topic]".

The skill must:
- Extract the structural pattern (camera DNA, landmark strategy, beat structure, object-ledger density, passive environment pattern).
- Apply the extracted pattern to the new topic.
- Preserve the structural quality while adapting all scene-specific content.

### Tier 4 — Video-Based Input (video reverse engineering)

The user provides a video file (e.g., MP4) or an intermediate JSON metadata file and requests video reverse engineering / prompt extraction (反推视频提示词).

#### 0. Two-Stage Isolation (Anti-Priming)

- **Stage 1 — Objective observation**: look only at pixels. Do NOT read any user brief, topic description, reference prompt set, or this skill's output templates while mapping what is visible. Record only what the frames physically show.
- **Stage 2 — Prompt composition**: only after the structural JSON metadata is complete, load the brief and output templates, then enter the normal Internal Composition Pipeline using the beat data as the ladder.

Never let knowledge of "what a renovation video usually contains" turn into a claimed observation. If a tool, material, worker, or operation is not visible in reviewed frames, it does not exist in the prompt set.

#### 1. Visual-to-Semantic Conversion Pipeline (视觉到语义转化管线)
When processing raw video footage, the system operates in a two-stage hybrid pipeline to achieve zero-drift prompt extraction:
1. **First Stage (CV Numerical Extraction Engine)**: Parses raw pixel data into physical constraints (Camera trajectory, object bounding boxes, volume scales, worker pose paths) and exports an intermediate structural JSON metadata file.
2. **Second Stage (LLM Continuity Orchestration)**: This skill acts as the LLM orchestrator, reading the structural JSON metadata and translating it into 100% contract-compliant IMAGE/VIDEO prompts.

#### 2. CV Analysis & Processing Rules
To feed the LLM orchestrator with high-fidelity metadata, the extraction engine must execute these analysis rules:
- **Keyframe Collage Auto-Generation (关键帧多宫格自动拼图 - T0)**: During keyframe extraction, a beautiful 5-column tiled keyframe collage must be automatically generated using FFmpeg's `tile` filter and saved directly next to the input video file (named `<video_name>_collage.jpg`) with scale `scale=240:-1`. This collage serves as a persistent, high-density visual reference for the entire transition sequence.
- **Adaptive Dense Analysis Rate (自适应密集分析 - P0)**: The extracted frame set is the canonical visual evidence. For clips with 90 extracted frames or fewer, send every extracted frame to semantic analysis. For longer clips, send at least 40% of extracted frames, never fewer than one frame per second, and force-include the first frame, last frame, per-second baseline frames, every CV-detected change segment's start / peak / end frames, and each peak's adjacent before/after frames.
- **Change Event Coverage (变化事件覆盖 - P0)**: Local CV must scan all extracted frames, not only sampled frames, and emit `change_events` for every meaningful object, material, lighting, worker, camera, or layout delta. Each `change_event` must be mapped to at least one `time_sequence.source_event_ids` entry and named in the corresponding VIDEO prompt.
- **Three-Pass Semantic Extraction (三轮语义提纯 - P1)**: Prefer Pass A frame/event facts, Pass B beat clustering, and Pass C prompt generation with coverage audit. Do not allow a single high-level visual summary to become final prompts without a coverage check.
- **Camera DNA Estimation**: Use Structure from Motion (SfM) to compute camera parameters. Classify FOV $\ge 90^\circ$ as `ultra-wide 14-18mm` and FOV $\approx 45^\circ$ as `natural 35-50mm`. Calculate camera height and angle from vanishing points.
- **OSPL Tracking & Ghost Clause**: Track all persistent objects using YOLO-World + SAM 2. Auto-map centroid positions to `Grid A1-C3` and Z-depth scales. If an object is 100% occluded in intermediate frames, trigger the **Ghost Clause** instead of omitting it.
- **VMFP Volume Quantization**: Use monocular depth (e.g., Depth Anything V2) to measure bulk material volumes. Represent depletion or accumulation as percentages (100% to 0%). Detect nearby rigid containers and apply **Rigid Container Encapsulation (RCE)**.
- **Worker Pose Tracking**: Trace worker skeletons using PoseTrack. Log entering frame time $t_{in}$ and leaving time $t_{out}$ relative to Grid borders. Auto-extract solid color silhouettes for **Hero Agent Lock (HAL)** and generate bi-directional Out-and-In passage clauses.

#### 3. Intermediate JSON Metadata Spec (中间层数据交互规范)
When provided with an intermediate JSON metadata file, or when generating the prompt set, the LLM must strictly parse and align with this JSON contract:

```json
{
  "camera_dna": {
    "shot_type": "static tripod shot",
    "lens": "14-18mm",
    "height_m": 1.6,
    "perspective": "eye-level perspective looking straight",
    "boundaries": {
      "left": "left mezzanine edge in Grid B1",
      "right": "right stair wall in Grid B3",
      "top": "ceiling void in Grid A2",
      "bottom": "debris band in Grid C2"
    }
  },
  "primary_landmarks": [
    {
      "name": "cracked floor seam",
      "grid": "Grid C2",
      "z_depth_scale": "20%"
    },
    {
      "name": "brick column",
      "grid": "Grid B1",
      "z_depth_scale": "60%"
    },
    {
      "name": "tall window opening",
      "grid": "Grid A2",
      "z_depth_scale": "40%"
    }
  ],
  "frame_observations": [
    {
      "frame": "keyframe_001.jpg",
      "timecode": "0.0s",
      "visible_state": "floor fully covered with heavy concrete debris",
      "changed_grid_cells": ["Grid C2"]
    }
  ],
  "change_events": [
    {
      "event_id": "E01",
      "frame_range": "keyframe_001.jpg-keyframe_006.jpg",
      "time_range": "0.0s-1.7s",
      "grid_cells": ["Grid C2"],
      "change_type": "debris removal begins",
      "before_state": "rubble pile fills the lower foreground",
      "after_state": "raw concrete strip becomes visible",
      "evidence_frames": ["keyframe_001.jpg", "keyframe_004.jpg", "keyframe_006.jpg"]
    }
  ],
  "time_sequence": [
    {
      "beat_index": 1,
      "shot_family": "interior_static",
      "beat_type": "removal",
      "source_event_ids": ["E01"],
      "source_frame_range": "keyframe_001.jpg-keyframe_006.jpg",
      "state_name": "debris clearing",
      "single_physical_operation": "debris clearing only",
      "causal_path": {
        "material_source": "existing rubble pile on the floor",
        "entry_path": "worker enters through Grid C1 edge with crates already visible in hand",
        "tool_contact": "matte-black broom and gloved hands push rubble into crate lips",
        "movement_path": "rubble moves from Grid C2 floor into crates and exits through Grid C1",
        "persistent_traces": ["dust edge around newly exposed slab", "drag scuffs leading toward Grid C1"],
        "next_frame_inheritance": "exposed raw floor strip and dust edges remain visible in IMAGE N+1"
      },
      "image_n_state": "floor fully covered with heavy concrete debris and plaster dust",
      "image_n_plus_1_state": "floor slab completely cleared and swept clean, exposing raw dark grey concrete texture",
      "volumetric_mass": {
        "material": "concrete debris",
        "container": "two heavy-duty black plastic crates",
        "grid": "Grid C2",
        "volume_flow": "100% capacity to 0% cleared"
      },
      "transient_agents": [
        {
          "agent_type": "worker",
          "count": 1,
          "hal_profile": "solid bright-neon-yellow safety vest, white hardhat, dark blue pants",
          "trajectory": {
            "enter_time": "0.0s",
            "enter_grid": "Grid C1",
            "exit_time": "7.5s",
            "exit_grid": "Grid C1"
          },
          "action_loop": "repeatedly bends down to scoop rubble into crates"
        }
      ],
      "lighting_phase": "ambient only",
      "sfx": "glass crunch, bag rustle, shelf scrape",
      "ambient": "hollow room tone, light wind",
      "evidence_frames": ["keyframe_001.jpg", "keyframe_006.jpg"]
    }
  ],
  "post_render_qc": {
    "hard_cut_times": [],
    "text_overlay_hits": [],
    "landmark_drift_score": 0.0,
    "agent_pop_hits": [],
    "object_birth_hits": [],
    "state_regression_hits": []
  },
  "banned_elements": []
}
```

The skill must parse this schema and map fields directly to standard `IMAGE` and `VIDEO` templates.

---

## Topic Ideation Engine (选题发动机)

Runs **before** the composition pipeline whenever the user has no topic and wants ideas (Tier 0). Its job is to supply a *continuous stream* (源源不断) of *novel and unique* (创新和独特性) renovation topics that are still 100% buildable by the pipeline. Full banks, scoring, and dedup logic live in [Idea Engine](references/idea-engine.md); this is the control flow.

### Genre DNA (the one formula)

> **A monumental, improbable RAW SHELL nobody expects to be habitable — its wild/ruined exterior left visibly intact — is opened up and built out beat by beat into a warm, finished, lived-in INTERIOR, carrying exactly ONE signature impossible-but-buildable twist.**

The dopamine is **contrast held in tension**: raw huge untouched outside vs. refined cozy inside, plus one "how is that even possible?" hero frame. (Reference: living redwood trunk → cozy bedroom → glowing root-vein window.)

### Ideation Algorithm

1. **Recombine (volume / 源源不断)**: draw one entry from each of the five orthogonal axes — `CARRIER`, `ENVIRONMENT`, `TRAUMA`, `DESTINY`, `SIGNATURE TWIST`. Rotate the carrier *family* (natural → man-made → vehicle → fantasy-grounded) so no two ideas in a batch share a shell family.
2. **Filter (quality / 创新和独特性)**: apply, in order — Orthogonal-Pairing Rule, **mandatory single twist**, dedup vs. [`used-topic-ledger.md`](references/used-topic-ledger.md), Cliché Blocklist, Buildability Gate (must map to a `space-workflows.md` macro + monotonic order + trace-producible twist), Scroll-Stop Test. Drop any candidate that fails.
3. **Score & rank**: rate each survivor 0-5 on Novelty, Visual Contrast, Twist Strength, Buildability, Scroll-Stop; output the top **N** (default 8).
4. **Honor constraints**: if the user pins an axis ("必须是水下", "得是交通工具", "for a kids channel"), lock it and recombine the other four. Remix mode holds one used carrier constant and forces a new destiny + twist.
5. **Deliver** the Idea-Engine Output Contract table (§5 of the reference), each row carrying a paste-ready Tier-1 input string, and close with: **「回复任意编号，我直接把它生成完整的 IMAGE/VIDEO 提示词集合」**.
6. **Ratchet**: when the user selects a topic to build, append its Topic DNA to [`used-topic-ledger.md`](references/used-topic-ledger.md) before composing, so the next ideation call is forced into fresh space.

### Engine Output → Pipeline Handoff

A selected idea's Tier-1 string is a valid composition input. Hand it to **Step 1: Topic Parsing** of the Internal Composition Pipeline with zero extra questions; the rest of the pipeline runs unchanged.

---

## Internal Composition Pipeline

Follow this order without skipping steps. These steps are internal — do not expose them to the user.

### Step 1: Topic Parsing

Parse the input into scene variables:

| Variable | Source |
|---|---|
| `CARRIER` | The main object being transformed (e.g., double-height loft, school bus, warehouse) |
| `ENV` | The surrounding environment (e.g., wooded hillside, urban lot, riverbank) |
| `TRAUMA` | The before-state pathology (e.g., collapsed ceiling, rust-flaked paint, moss-stained walls) |
| `DESTINY` | The after-state identity (e.g., industrial live-work loft, hidden shelter, steampunk roastery) |
| `REWARD ACTION` | The final reveal motion (e.g., person walks through finished space, hidden hatch opens, lights activate) |

If any variable cannot be inferred, use the space-type default from the workflows reference.

### Step 2: Mode Selection

Choose routing mode:

| Condition | Mode |
|---|---|
| Single continuous space, no threshold crossing | `Standard` |
| Clear threshold structure (doorway, tunnel, garage opening) + story moves exterior → interior | `Threshold` |

### Step 3: Space Type Classification

Map the topic to one of the established space types:

| User Intent | Space Type |
|---|---|
| abandoned warehouse / abandoned property / abandoned loft | `abandoned property` |
| exterior facelift / facade renovation | `exterior facade` |
| road repair / street repair / driveway upgrade | `road / street / driveway` |
| garage upgrade / workshop build-out | `garage / workshop` |
| backyard / landscape / pool renovation | `backyard / landscape / pool` |
| luxury apartment renovation | `luxury apartment` |
| retail refresh / showroom rebuild | `retail / showroom` |
| basement / bunker / sunken space | `underground space` |
| custom object / tabletop / woodwork build | `custom build object` |

### Step 4: Construction Macro Selection

Load the appropriate construction workflow from [Space Workflows](references/space-workflows.md) for the classified space type. The workflow defines the valid physical phase sequence that forms the production beat ladder.

### Step 5: Production Beat Ladder Derivation & Visible Milestone Packaging

Derive `N` from the construction macro:

- Without an upstream ladder: default to `3-6` VIDEO clips based on the space type's standard workflow.
- With an upstream `PRODUCTION BEAT LADDER`: one listed physical phase = one `VIDEO`.
- Merge tiny beats that would produce nearly static clips (unless explicitly listed).
- **Apply the Visible Milestone Package Rule**: Every ordinary beat must end in one named, immediately legible stage product at its full declared region or component count. A beat may contain one operation or up to three tightly related actions in the same zone when all are necessary for that single terminal result (for example roof panels + door + threshold closeout, or joists + bay insulation). Split cross-phase bundles such as demolition + painting + furnishing, rough-in + the panel that conceals it, or unrelated work in different zones. Adjacent IMAGE anchors may change at most 3 grid cells, but token patches, one-corner edits, and merely begun/partial states are forbidden.
- Always add one final `VIDEO` for the explicit reward motion.
- Never merge a threshold bridge with construction or final reward.
- **Construction Sequence Validation**: After deriving the beat ladder, validate its order against real construction dependencies. Default macro order: (1) demolition and debris clearing before new work in that zone; (2) structural repair before rough-in systems; (3) rough-in systems — wiring, plumbing, ducting — before any panel closes over them; (4) ceiling panels before wall panels (board the overhead first so the wall panels can support and hide the ceiling-board edges); (5) primer before finish coat; (6) floor finishing after overhead and wet work; (7) fixtures and lighting only after their wiring exists; (8) furniture and decoration last. Hard vetoes: no wiring or plumbing work after the panels that would hide them are already installed; no finish coat before primer; no new roof before the walls or frame carrying it are repaired; no practical light, lamp, or powered fixture activation in a set that contains no earlier wiring beat (off-grid carriers additionally require a visible power source — solar panel, battery bank, or generator — installed in a prior beat); no interior fit-out inside a space whose creation or pre-existence was never shown or stated on camera. **Enclosed-Space Provenance Rule**: any interior chamber revealed behind a newly opened shell must be physically accounted for — either explicitly described as pre-existing space (a natural cavity, an original room) in the opening beat, or given its own on-camera excavation and mucking-out beats before any interior finishing; the interior volume must plausibly fit inside the exterior shell. If the observed beat order violates a hard veto, re-inspect source material first — a misread frame is more likely than an impossible build sequence.
- **Progress Flow Control**: Compress time-lapse action across the full visible milestone rather than shrinking the result to a local patch. The clip must show the first contact and repeated work cycles, with two continuous progress lines: the primary construction product grows to its full extent/count while material stock/container/spoil or a tightly coupled component changes independently. Construction state remains monotonic. Declared temporary works are the one exemption and may leave only through a named strike beat with removal traces.
- **First Occurrence On Camera Rule**: The first instance of every change type (first board placed, first brush stroke, first fastener driven) must appear in full with its causal chain inside that VIDEO. Time-lapse compression may repeat the action across the clip, but must never skip the first occurrence or introduce a new object or sub-task via a time jump.

### Step 5.5: Temporal Physics Skeleton Assembly

Before rendering any `IMAGE` or `VIDEO` prompt, every beat must be converted into a **Temporal Physics Skeleton**. This is a required internal state-transition layer, not optional wording polish.

Each beat skeleton must declare:
- `shot_family`: one of `exterior_static`, `threshold_bridge`, `interior_static`, or `reward_reveal`.
- `beat_type`: one of `removal`, `excavation`, `surface_prep`, `coating`, `fixture_install`, `threshold`, `interior_finish`, `furnishing`, or `temporary_works_strike`.
- `single_physical_operation`: the single terminal milestone package for the entire 8-second clip; it may contain up to three tightly related same-zone actions serving that one result.
- `material_source`: where the changed material/object comes from before it enters the frame.
- `entry_path`: how the material/object enters the working area.
- `tool_contact`: the specific hand/tool/machine contact that causes the change.
- `movement_path`: the visible transport or installation route through the frame.
- `persistent_traces`: at least two physical traces inherited by `IMAGE N+1`.
- `next_frame_inheritance`: what must remain visible and unchanged in the next anchor.

P0 rule: a beat without one clear terminal milestone fails before prompt rendering. Cross-phase or different-zone bundles fail; coherent closeout packages are allowed only when every action is causally necessary for the same visible end product.

For cliff cable car / gondola restoration sequences, use this 19-beat ladder unless the user explicitly removes either the solar system or the full interior rebuild:
1. Exterior rust scraping.
2. Exterior sanding and leveling.
3. White primer coating.
4. Exterior finish coating.
5. Platform railing installation.
6. Door opening with interior landmark sneak-peek (peek anchors must be original cabin features, per the Anchor Qualification rule).
7. Roof rail and bracket installation.
8. Solar panel placement and cable clipping.
9. Work-light stabilization without structural changes.
10. Threshold bridge into the cabin.
11. Interior debris removal.
12. Interior wall base preparation.
13. Floor joist and subfloor structure installation (creates the level working platform).
14. Interior electrical wiring, fed from the solar cable entry, run before any panel closes over it.
15. Ceiling panel installation with a pre-cut light opening.
16. Yellow wall panel installation.
17. Round light fixture installation and practical activation.
18. Floor surface finishing.
19. Final furniture reward reveal.

### Step 6: Drift Lock & SCUP Packet Assembly

Build the complete packet by filling all required fields using the **Spatial Consistency Upgrade Protocol (SCUP)**:

**Camera DNA Block** (~25-30 words):
- Shot type (static tripod / elevated tripod / etc.)
- Lens feel (ultra-wide 14-18mm for spaces / 35-50mm for custom objects)
- Camera height (1.4-1.8m for spaces / 0.9-1.3m for objects)
- Angle and perspective axis
- Frame coverage
- Boundary anchors (left, right, top, bottom foreground band)
- **Sub-Pixel Coordinate Pinning (SPCP — shot-family conditional)**: Pin the camera's angular attitude with wording that matches the shot family. Level exterior shots: `horizon line remains perfectly level at exactly 50-percent height of the frame`. Elevated or tilted shots: `camera pitch locked at the declared steep downward angle; vertical lines converge consistently toward the same vanishing direction; no horizon reference`. Enclosed interiors, caves, and windowless spaces: `camera pitch locked level; the central vanishing axis stays centered in the frame` — never mention a horizon, sky, or clouds where none can physically exist. Optical-flow radiation wording (`all optical flow lines radiate symmetrically from the optical center`) belongs only in push-in/translation clips, never in static tripod prompts.

**Geometry Lock**:
- Door/window placement and count
- Wall/roof lines
- Stair direction (if applicable)
- Full scene boundary
- Carrier proportions

**Normalized Grid Coordinate System (NGCS) & Fixed Landmarks**:
- Divide the 16:9 frame into a $3 \times 3$ grid (`Grid A1` to `Grid C3` from top-left to bottom-right).
- Assign exactly **3 Primary Spatial Anchors** across three depth zones, each tied to an absolute grid cell and visual feature:
  - Foreground: 1 named anchor with Grid cell (e.g., `cracked floor seam in Grid C2`)
  - Mid-depth: 1 named anchor with Grid cell (e.g., `brick column in Grid B1`)
  - Background: 1 named anchor with Grid cell (e.g., `tall window opening in Grid A2`)
- Specify the **Z-Depth Height Scale** for each anchor (e.g. `brick column holds a scale of 60% of total frame height`) to lock the Z-axis.
- **Relative Positioning Lock (RPL)**: Limit absolute `Grid` coordinates strictly to the 3 Primary Spatial Anchors. Group and lock all secondary/drift-prone objects relatively to the nearest Primary Landmark (e.g., `green toolbox is exactly 10cm to the left of the brick column`) to prevent coordinate dilution and cross-contamination in the T5 text encoder.

**Frame Boundary Lock**:
- `left boundary`: Grid cell and named anchor (e.g. `left mezzanine edge in Grid B1`)
- `right boundary`: Grid cell and named anchor (e.g. `right stair wall in Grid B3`)
- `top boundary`: Grid cell and named anchor (e.g. `ceiling void in Grid A2`)
- `bottom foreground band`: Grid cell and named anchor (e.g. `debris band in Grid C2`)

**Object Position-State Ledger (OSPL)**:
- Each recurring object: count + material/color + exact name + current state + relative position / **Grid cell position** + **Z-Depth Height Scale**
- **Ghost Clause for Occluded Objects**: When an object is occluded, it must not be omitted. Maintain its presence using the Ghost Clause: `[Object Name] remains physically locked at [Grid Cell] with [original properties], currently fully hidden behind [occluding object].`
- Maximum 10 detail-critical objects for consistency

**Worker/Machine Choreography Ledger & HAL**:
- Clean Frame Boundary: Force all static `IMAGE` anchors to contain **zero** active workers or machines.
- Workers and machines are **transient elements** injected *only* in `VIDEO` prompts (entering, acting, and exiting before the final frame).
- **Bi-directional Out-and-In Passage Clause**: Video prompts featuring transient agents must explicitly describe their entry and exit paths. Workers must enter the frame at t=0s and walk out by t=7.5s, leaving the final frame sterile.
- **Hero Agent Lock (HAL)**: When a worker must be visible, lock them using high-contrast, low-detail silhouette terms (e.g., `one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants; do not show the worker's face`).
- **Geometric Manual Tool Anchoring (MTAL)**: To prevent hand-held manual tools from blinking or morphing during continuous action interpolation, every worker's tool must be anchored using precise colors, shapes, and materials (e.g., `the worker sways slightly while performing repeated cycles of sweeping strokes using a solid-black long-handle plastic broom tool`). The tool's description must remain constant and clear.
- **Persistent Site Plant Exception**: long-duration temporary works — scaffolding, formwork, shoring, cribbing, site cranes — are NOT transient agents. They arrive in a named erection beat, persist across later IMAGE anchors as static, unmanned equipment (Clean Frame bans active workers and running machinery, not parked plant), are tracked in the object ledger with Ghost Clauses when occluded, and leave only through a named temporary-works-strike beat that shows removal traces. An anchor showing freshly poured, unset concrete must still show the formwork supporting it.

**Global Causal Trace Rule (GCTR)**:
- Every visible addition, removal, repair, installation, assembly, cleaning, cutting, lifting, dragging, welding, bolting, painting, pouring, sanding, sealing, or machine-assisted action must leave visible physical trace evidence in `IMAGE N+1`. Nothing may appear, disappear, align, attach, open, close, clean, or finish without a visible causal path and contact trace.
- For every changed object or surface, declare: source or entry path, tool/hand/machine contact method, transport or movement path, attachment or removal evidence, residue/deformation/surface mark, and which traces persist into the next `IMAGE` anchor.
- Workers and machines may exit the frame, and temporary tools may be carried out, but their physical consequences must remain visible as causal anchors unless the next beat explicitly removes or covers those traces with a new trace-producing action.
- Permanent construction traces are required evidence, not dirt: keep weld beads, screw heads, bolt rows, bracket shadows, seam lines, adhesive squeeze-out, caulk beads, patched edges, saw-cut lines, drill dust, sanded matte bands, brush overlap, roller stipple, tire or track compression, drag scuffs, clamp pressure marks, crane strap rubs, scaffold foot pads, alignment chalk lines, and machine contact marks when they explain the new state.
- Do not write `all construction evidence resolved` as a blanket cleanup. Only loose temporary mess can be cleaned away; permanent or recently caused contact traces must persist until a later physically justified finishing action changes them.

**Lighting Phase Ladder**:
- Starting phase (usually `ambient only` or `temporary work light active`)
- Phase per IMAGE anchor (hold or +1 progression only)

**Passive Environment Direction**:
- Exterior: cloud-flow direction, wind-swept vegetation direction, river/surface direction
- Interior/Underwater: water-caustic direction, sediment direction, shimmer color family

**Interest Budget**:
- Per-clip hooks (one per ordinary construction VIDEO)
- Optional sequence-level reveal (at most one)
- Final reward (reserved for last VIDEO)

### Step 6.5: Staged Execution Mode (Anchor-First Rendering — DEFAULT)

Staged execution is the **default** per the Staged Delivery Contract (top of this file). Before composing *any* `IMAGE`/`VIDEO` prompt text (Step 7 onward), assume this generation **will** be rendered and follow the staging procedure below. Do not scan the user's wording for render intent ("作图", "渲染", etc.) as a precondition — absence of those words is not a waiver. The only two waivers are: the user explicitly asked for prompt text only (`只要提示词` / `不用作图` / `不用渲染` / `纯文本`), or the render server is unreachable (script exit code 2). Under a waiver, deliver the plain prompt set (Steps 7-11 run exactly as written, in one pass) and make the render offer described in Step 13 afterward.

**Unless a waiver applies, everything below is mandatory — not a nice-to-have that only applies to some other execution layer.** Every downstream frame visually chains from `IMAGE 1` (each later frame is generated image-to-image off the previous one), so a wrong anchor silently wastes the entire downstream image and video generation budget. Since 2026-08-05 the server runs no automatic judgement on any frame, so **the user's eyes on the rendered anchor are the only check that exists** — that is exactly why this step is mandatory rather than optional. This applies exactly the same whether you (the model reading this) are composing the prompts directly in chat, or a separate backend service is driving the composition — do not skip staging just because you are the one writing the prose:

1. Run Steps 1-6 silently as usual (topic parsing through Drift Lock Packet assembly) — nothing user-facing yet.
2. Compose `IMAGE 1`'s prompt only (the relevant slice of Step 7). Do **not** compose `IMAGE 2` onward or any `VIDEO` yet, and do not show `IMAGE 1`'s prompt to the user as a finished deliverable — it is still provisional until the anchor frame below has been rendered and looked at.
3. Render the anchor **synchronously, in this same turn**, before writing anything else:
   ```powershell
   python "C:\Users\video\.codex\skills\gemini-veo-restoration-composer\scripts\render_and_gate_anchor.py" `
     --title "<the Chinese topic title>" `
     --prompt "<IMAGE 1's prompt text>" `
     --server "http://127.0.0.1:8085"
   ```
   This call blocks until the frame is on disk. Wait for it to finish; do not proceed speculatively while it's running.
4. **Exit code 0**: the frame rendered. Show the user the image path the script printed and say plainly that nothing judged it automatically (`⚠️ 服务端不对该帧做自动判定，请确认这就是你要的首帧`). Wait for their read on it before proceeding to step 6 — you are asking a person to do the job no gate does anymore. Never claim the anchor "passed" anything.
5. **Exit code 3/4/5** (server error / missing prompt / timeout): report exactly what the script said and stop. A timeout means the server is still working — do **not** treat it as unreachable and do not fall back to one-pass delivery.
5b. **Exit code 2** (render server unreachable): this is the second waiver in the Staged Delivery Contract. Say `⚠️ 渲染服务未运行，本次跳过首帧预览，直接输出全套提示词`, then fall back to one-pass composition (Steps 7-11 as written) and still make the Step 13 interactive offer. Do not retry the script in a loop.
6. Treat the rendered `IMAGE 1` — its prompt text and what actually appeared — as authoritative reality. Reconcile your working model of the Camera DNA / Primary Landmarks / object ledger against it before writing anything else (this is **Packet Reality Reconciliation**): only rewrite what visibly contradicts the render; never invent a landmark or object that isn't visible in it.
7. Only now compose `IMAGE 2-(N+1)` (the rest of Step 7) and `VIDEO 1-N` (Step 8), then continue through Steps 9-11 as usual. The final delivered prompt set includes the exact same `IMAGE 1` prompt that was rendered, verbatim — do not silently rewrite it again at output time, or the delivered set will describe a different frame than the one the user approved.

Step 13's rendering trigger, further down, does not re-render `IMAGE 1`: `/api/render_staged` reuses whatever frames are already on disk and only renders the remainder. That is one more reason Step 7's rule — deliver the rendered `IMAGE 1` prompt verbatim — is load-bearing.

### Step 7: IMAGE Anchor Rendering (HCL & NGCS)

By default (Staged Delivery Contract), compose `IMAGE 1` here first and stop for the Step 6.5 gate before returning to compose `IMAGE 2-(N+1)` — do not write the whole `IMAGE 1-(N+1)` range in one pass. Only under an explicit waiver (user asked for text only, or render server unreachable) compose the full range in one pass as below.

Render `IMAGE 1-(N+1)` executing **Hierarchical Context Layering (HCL)** and **Syntax Pruning** (placing core spatial and grid constraints in the first 40 tokens using weight-sensitive punctuation `:` and `;` to prevent attention dilution):

**Cumulative State And Anchor Delta**:
- Every IMAGE anchor must inherit all permanent changes and traces from all prior beats. A trace may disappear only when a later named operation explicitly covers or removes it, and that covering must be the declared beat where it happens.
- The difference between IMAGE N and IMAGE N+1 must be exactly the declared operation's result of VIDEO N only — no side progress, no bonus cleanup, no improvements in other zones.
- Each progressive IMAGE anchor states completion extent in concrete spatial terms (e.g., `the left two-thirds of the wall panel installed while the right third stays bare framing`), giving adjacent-anchor interpolation an unambiguous start and end.
- **Negative-Constraint Zone Locking**: an image-editing renderer over-completes an under-constrained zone rather than under-completing it. Two recurring failure patterns confirmed by render QA logs: (1) restating an already-inherited damage descriptor from an earlier beat (e.g. `bent rib framing exposed` established back in IMAGE 1-2) inside a later beat's own change description gets misread as a fresh destructive action in that same beat, stripping wall/floor material well beyond the single declared operation; (2) an open-ended furnishing/reward beat invites invented extra props (pillows, cups, books, decor) beyond the declared object list. When a beat's operation is narrowly scoped to one zone, or its object list is closed, say so explicitly and negatively in the same anchor (e.g. `side wall panels remain in place, not stripped`; `only these listed objects are present — no additional decor, tools, or furnishings`) rather than relying on the positive description alone to bound the model.

**IMAGE 1 (Before/Trauma Anchor - Clean Frame)**:
```
Generate an image of a [Camera DNA Block: static tripod shot, 14mm, height 1.6m, eye-level: subject in Grid B2]. Locked anchors: [Primary Anchor A] at [Grid Cell, scale], [Primary Anchor B] at [Grid Cell, scale], left boundary [Grid LB], right boundary [Grid RB], top boundary [Grid TB], and bottom foreground band [Grid BB]. The scene is the explicit before anchor, completely empty of workers, with [trauma pathology: location + surface-material state + damage type for each major damage zone in its Grid cell]. [Lighting phase] and [material realism]. [Natural-language guardrail: keep same framing; do not redesign].
```

**IMAGE 2+ (Progressive State Anchors - Clean Frame & RPL)**:
```
Generate an image of a [Same Camera DNA Block — character-for-character copy: subject in Grid B2]. Scene inherits all landmarks, geometry, and boundary anchors from IMAGE 1. Relative Positioning Lock (RPL): [2-3 most drift-prone items positioned relatively to Primary Landmarks]. The scene is the [current stage name] anchor, completely empty of workers, with [one dominant change cluster in its Grid cell] while [inherited evidence: exact recurring object-ledger grid phrases + unchanged damage/repair evidence] remain visible and unchanged. Global Causal Trace Rule (GCTR): [changed object/surface] shows at least two persistent contact traces such as [fastener/seam/residue/drag mark/tool scar/machine compression/contact dust], proving how the state changed from IMAGE N. [Lighting phase] and [material realism]. [Guardrail sentence].
```

**Final IMAGE (Reward Tail State - Clean Frame & RPL & RHMA-Blur)**:
```
Generate an image of a [Same Camera DNA Block — character-for-character copy: subject in Grid B2]. Scene inherits all landmarks, geometry, and boundary anchors from IMAGE 1. Relative Positioning Lock (RPL): [2-3 drift-prone items locked relatively to Primary Landmarks]. The scene is the [reward tail state name] anchor, completely empty of workers, with [reward action completed: e.g., hatch opened on hinge, warm light spilling in Grid B2], [final-state details visible], loose temporary construction clutter carried out, and permanent causal traces still visible where they explain installed or repaired elements. Global Causal Trace Rule (GCTR): [final changed elements] retain visible seams, fasteners, contact marks, tool finish texture, or machine pressure traces instead of appearing perfectly untouched. **Mirror Consistency Clause (RHMA-Blur)**: [The highly reflective polished floor surface in Grid C1-C3 displays a heavily blurred, low-gloss, diffused reflection of the background; reflections are muted, dark, and highly out-of-focus, preventing high-frequency contrast or sharp details; realistic Fresnel falloff near the margins]. Keep [final lighting phase] and [material realism]. [Guardrail sentence].
```

Word count targets (hard limits enforced by the pipeline validator — there is no
mode-based exception; any IMAGE over 170 words or VIDEO over 180 words fails
validation and forces a costly regeneration retry regardless of complexity,
reference-image mode, or drift-sensitivity):
- IMAGE: 100-170 words, always (highly pruned for T5 encoder efficiency)
- VIDEO: 120-180 words, always
- If a beat feels like it needs more room (complex reference-image mode, drift-sensitive
  space work), trim redundant adjectives, restated boilerplate, or secondary description
  first — never by cutting required structural elements (Camera DNA, Out-and-In Passage,
  pacing control phrase, audio clause, Ghost Clause, Mirror Consistency Clause).

### Step 8: VIDEO Motion Chain Rendering (DKP & VMFP & HAL & PBISP)

Render `VIDEO 1-N` following the Unified VIDEO Skeleton, strictly executing the **Continuous Action Flow Contract** and SCUP rules to prevent spatial jump and identity morphing:

Every VIDEO must follow this exact structure:
1. **Fixed opening**: `Use the provided first frame and last frame as exact composition anchors.`
2. **Adjacent-frame binding**: `Use IMAGE N as the actual first-frame image and IMAGE N+1 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout.`
3. **Minimal camera/framing lock**: shot family, locked camera position, same frame boundaries, and Grid positions of critical fixed landmarks + boundary anchors.
4. **First-to-last motion delta**: Describe the visible change from IMAGE N to IMAGE N+1 as a continuous physical action, not a static state transition.
5. **8-second time-lapse action chain (Continuous Action Flow & HAL)**:
   * **Rhythm**: 8 seconds of full-length continuous action progression, with the dominant motion running from the very first frame to the very last frame without static holds, stable starts, or deceleration/settling zones.
   * **Pacing control phrase**: Must include the exact phrase `continuous construction time-lapse, not real-time footage` in the paragraph (except for threshold bridge or final reveal).
   * **Bi-directional Out-and-In Passage Clause**: Workers must enter the frame at t=0s and walk out by t=7.5s (e.g., `At t=0s, one lone worker enters the frame from Grid C1 edge; the worker performs work, and by t=7.5s, walks out of the frame through Grid C1 edge, leaving the frame completely empty at t=8s`).
   * **Transient Injection & HAL**: Workers and machines enter the frame, perform cycles of verb-first active labor, and exit before the final frame. If visible, workers are locked using Hero Agent Lock (HAL) solid-color silhouettes.
   * **Volumetric Mass & Flow Preservation (VMFP) with Rigid Encapsulation**: Describe bulk materials encapsulated inside rigid containers (buckets, crates, wheelbarrows, bags) with explicit quantities (e.g. `sand is encapsulated inside three solid dark-green plastic buckets at Grid C2, each holding 15 liters; capacity goes from 100% capacity in C2 to 30% capacity to 0% cleared, and buckets are carried along the path from Grid C2 to B1`). Do not describe loose shovel actions without containers. Scale containers to the material volume: hand buckets and crates only for debris under roughly half a cubic metre; larger removals require mechanical containers (excavator buckets, skips, chutes, tracked carriers) or explicitly repeated trips feeding a spoil pile that visibly grows across anchors. Any cut-out slab, panel, or door-sized solid piece must get its own pry-out and carry-out action — crumbs in buckets never account for a large solid piece.
   * **Measurable Progress Markers**: Mandate at least **two** measurable progress markers (e.g., exposed area increasing from 10% to 90%, wall-covering panel row count completing, spoil pile height growing from 20% to 70% of the frame).
   * **Spatial Completion Extent (SCE)**: Every VIDEO must state the operational start and end extent in concrete spatial terms that match the adjacent IMAGE anchor descriptions (e.g., `the floor panel advances from bare concrete across the left third of Grid C to fully covering Grid C1 through C3`). Use progressive `-ing` verb forms for mid-clip descriptions; completion language is reserved for the final moment before the clip ends.
   * **Global Causal Trace Rule (GCTR)**: Every added, removed, repaired, cleaned, installed, assembled, or machine-moved element must show its source/entry path, contact method, movement path, attachment/removal evidence, and at least two persistent trace markers that land in `IMAGE N+1`.
   * **Banned Transition Shortcuts**: Strictly forbid any mention of `cross-dissolve`, `fade-in`, `suddenly`, `magically`, `rapid montage`, `jump cut`, or `instant transformation`.
   * **Per-clip hook**: one low-risk visual hook from the current state or the next anchor (e.g., uncovering a fixed edge in Grid B2).
   * **Natural-Language Visual-Only Translation Rule (NLVTR - P0)**: The prompt composer must *never* output mathematical percentages (e.g. `%`), colons inside variables, or technical structured labels (such as `TSPA`, `HAL`, `VMFP`, `RCE`, `GCTR`, `RPL`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`, or `Out-and-In Passage:`) in the final prompt text block. This is the pipeline's actual hard-enforced list — every one of these acronyms, including `SCUP` itself, fails validation if it leaks into final prompt text. Any technical or structural constraint parsed internally must be translated into fluid, descriptive, non-technical natural language sentences detailing visual progress, kinetic movements, and persistent traces. For example, instead of writing `Two-Stage Progress Anchor (TSPA): progress marker 1: 10% to 90%`, write `the worker sweeps the floor, causing the dusty grey concrete surface to expand and occupy nearly the entire floor from the margins inward`. The final prompt must read as continuous photorealistic visual prose, completely free of math symbols and technical structured labels to prevent text overlays on the generated video.
6. **Passive environmental timelapse**: locked direction, varied observation per clip.
7. **Physical and continuity realism**: object transport (no teleporting without path), material behavior, lighting, unchanged evidence.
8. **Ambient audio**: `SFX:` + `Ambient noise:` matched to visible action.

**Threshold bridge** (if applicable): Follow the **Threshold Bridge Consistency Protocol (TBCP)** — see [references/threshold-bridge-consistency-protocol.md](references/threshold-bridge-consistency-protocol.md). The exterior→interior crossing is the only beat that flips lighting domain + camera family + anchor set at once, so it must never be done in a single clip.
* **Bridge Split (TBCP Rule 1)**: Render the crossing as two clips joined by a shared **Sill Handoff IMAGE** — `IMAGE T → VIDEO Bridge-1 (approach to sill) → IMAGE T+1 (sill handoff) → VIDEO Bridge-2 (cross and settle) → IMAGE T+2 (interior settled)`. The sill handoff IMAGE is literally Bridge-1's last frame and Bridge-2's first frame (frame hand-off lock). Each bridge clip switches at most 1.5 systems.
* **Anchor Inheritance (TBCP Rule 2, PBISP upgrade)**: The two interior landmarks peeked through the door in `IMAGE T` must be the **exact same objects** promoted to the interior shot family's mid-depth and background primary anchors in `IMAGE T+2`. They scale up continuously across both bridge clips, never repositioning or re-rendering. Introducing any interior primary anchor not pre-visualized through the doorway is banned. **Anchor Qualification (mandatory)**: peeked anchors must be features that plausibly already exist at crossing time — original structure, natural rock/wood formations, pre-existing wreckage, or items installed in an earlier on-camera beat. Future construction products (an uncarved staircase, unplaced furniture, uninstalled fixtures) are banned as peek anchors: the bridge always precedes interior construction, so using them forces objects to exist before the beat that creates them. **Monotonic Scale Lock**: each peeked anchor's declared frame-height scale must strictly increase across `IMAGE T → IMAGE T+1 → IMAGE T+2` (e.g. one-fifth → two-fifths → three-fifths), matching the forward push-in; locking one constant scale across the crossing contradicts the required continuous scale-up and reads as a fake digital zoom.
* **Single-Variable Bridge Camera (TBCP Rule 3)**: Exterior and interior families share identical lens feel and identical camera height, so the bridge changes only forward translation. Bridge Camera DNA: `same lens feel, same height, coaxial forward push-in only; no pan, no tilt, no roll; horizon level at mid-frame`.
* **Exposure & White-Balance Soft Roll (TBCP Rule 4, NLVTR-safe)**: Smooth the lighting change across the whole crossing and attribute it physically to door-shade ahead + doorway backlight behind — `the bright outdoor glare rolls off gently and the frame settles into the cooler dim interior, lit mainly by daylight spilling back through the doorway behind; gradual across the whole clip, never a sudden brightness snap`. No percentages or color-temperature numbers.
* **Door-Frame Wipe & Cross-Threshold Tether (TBCP Rules 5-6)**: At the sill the door-frame edges slide symmetrically outward like a vertical wipe, completing the exposure shift behind them; carry at least one material or light source unbroken across the sill (e.g. the same floor continues inside, or exterior daylight becomes interior backlight).
* **Dynamic Keyframe Projection (DKP) & Optical Flow**: Each bridge clip still projects frame coordinates dynamically: `At first frame, the door frame opening occupies Grid B2 (0.35 to 0.65 x-axis). At 4 seconds, the camera enters the opening; door frame edges slide symmetrically outward crossing Grid B1 and B3 boundaries. At final frame, the door frame has fully exited, revealing the interior room centered at Grid B2. Coaxial forward push-in vector; all optical flow lines radiate symmetrically from Grid B2; horizon line remains perfectly level at exactly 50% height; pre-visualized landmarks scale up continuously without layout distortion.`

**Final reward**: coaxial handheld push-in reveal, single continuous take.
* **Reflective Mirror Alignment (RHMA-Blur)**: Include Mirror consistency clause (RHMA-Blur) to vertically align highly blurred, low-gloss diffused reflections of Grid A1-A3 on the floor Grid C1-C3.
* **Dynamic Projection**: Define coaxial path vector, horizon lock, and forward optical flow.
* ASMR diegetic footsteps mandatory (name floor material + footstep texture).

### Step 9: Silent Self-Check & SCUP Quality Audit Report

1. Run all gates from [References/Continuity Contracts](references/continuity-contracts.md) and the **SCUP P0 Kill Gates** before delivery:

**SCUP P0 Kill Gates** — rewrite entire clip if any fires:
- Structure errors (count, slot type, mixed protocol, shot family)
- Camera DNA Block not copied literally across same-family IMAGEs
- Any `IMAGE` contains active workers or machines (violates Clean Frame Boundary).
- Any video featuring workers lacks an explicit Out-and-In Passage Clause trajectory at t=0s and t=7.5s.
- Any video featuring loose/fluid materials fails to encapsulate them in rigid containers (violates Rigid Container Encapsulation RCE).
- **Volume Conservation Gate (P0)**: container capacity, trip count, or spoil-pile growth must plausibly account for the volume removed or delivered in the beat. Clearing a room-scale debris field or cutting a passable opening into two hand crates fails; any cut-out solid piece that never receives an on-camera carry-out fails. A correctly scaled, visibly growing spoil pile satisfies encapsulation for material that is not transported out of frame.
- Vague landmark or boundary locations without explicit `Grid A1-C3` cells and Z-depth scale, or fails to lock secondary drift-prone objects relatively using RPL.
- Any occluded landmark or ledger object is omitted instead of being maintained via the `Ghost Clause`.
- Missing coordinate-level dynamic keyframe projection (DKP) for threshold bridge or final reward push-in.
- Volumetric materials lack a percentage scale (e.g. % capacity) and a physical transport vector (VMFP).
- Final polished floor lacks a Mirror Consistency Clause (RHMA-Blur) vertically aligning Grid C reflections with Grid A physical objects.
- Active workers in video have complex facial/clothing descriptions instead of Hero Agent Lock (HAL) silhouettes.
- Threshold `IMAGE T` lacks a Sneak-Peek (PBISP) revealing at least 2 interior landmarks before the bridge video.
- Continuous action failure: Any video slot lacks a verb-first active continuous action flow or uses transition shortcut words (`cross-dissolve`, `fade-in`, `suddenly`, `magically`, `rapid montage`, `jump cut`, `instant transformation`).
- Milestone-package incoherence / insufficient stage delta: Adjacent IMAGE anchors combine unrelated phases or zones in one beat (must be split); one coherent same-zone package of up to three actions is allowed when it produces a single named terminal stage. Full declared coverage/count is REQUIRED — token patches or barely visible local edits fail.
- **Causal Trace Gate (GCTR - P0)**: Any new object, repaired surface, cleaned zone, assembled structure, installed fixture, moved material, or machine-assisted change appears without at least two visible trace markers: contact mark, seam, fastener, residue, drag path, compression mark, dust edge, tool scar, weld bead, cut line, adhesive squeeze-out, tire/track print, cable rub, scaffold footprint, clamp mark, bracket shadow, or alignment guide.
- **Keyframe Collage Auto-Generation Gate (关键帧多宫格拼图自动生成门 - T0)**: During keyframe extraction, a 5-column tiled keyframe collage must be automatically generated using FFmpeg's `tile` filter and saved in the video directory (named `<video_name>_collage.jpg`). If this file is missing or fails to generate, the audit fails immediately with a critical T0 status.
- **Video Analysis Frame Count Gate (视频分析帧数门 - P0)**: The number of keyframes sent to the Multimodal LLM (Gemini/OpenAI) must satisfy Adaptive Dense Analysis Rate: all extracted frames for clips with 90 frames or fewer; for longer clips, at least 40% of extracted frames, never fewer than one frame per second, with mandatory start / peak / end coverage for all CV change segments.
- **Change Event Coverage Gate (变化事件覆盖门 - P0)**: Every CV `change_event` must appear in `time_sequence.source_event_ids`, must be traceable to `source_frame_range`, and must be referenced in the matching VIDEO prompt. Missing event coverage fails the audit even if prompt wording looks polished.
- **Analysis Peak Inclusion Gate (峰值帧送审门 - P0)**: For every detected change event, the event start frame, maximum-delta peak frame, and event end frame must be included in the semantic analysis frame set.
- **No Banned Notations Gate (NLVTR Gate - P0)**: The final image/video prompts must *never* contain mathematical percentage symbols (`%`), raw numerical ranges inside the visual description (e.g. `10% to 90%`, `40cm to 0cm`), colons inside variable descriptions, or dry structured acronyms (`TSPA`, `HAL`, `VMFP`, `GCTR`, `RPL`, `RCE`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`). All visual progress and persistent traces must be fully described in fluid, continuous natural language prose (e.g. "dusty floor area shrinks as clean wood surface grows to cover the floor"). Any presence of math characters, colons in visual slots, technical SCUP acronyms, or grid cell coordinates (e.g. `Grid C1`, `Grid B2`) in final image/video prompts fails this gate.
- **Beat Overload Pop Prevention Gate (P0)**: Verify that no single beat combines more than one distinct physical operation or structural system change (e.g. debris clearing, subflooring, insulation paneling, ceiling panels, painting, and lighting are distinct operations and must be split into separate beats). Any beat combining multiple operations will fail this gate.
- **Sub-Pixel Coordinate Pinning Gate (SPCP Gate - P0)**: Verify that every prompt (IMAGE and VIDEO) pins the camera attitude with wording matching its shot family: level exteriors pin the horizon line at a stated height; elevated/tilted shots pin the declared pitch angle and vertical convergence (no horizon reference); enclosed interiors pin a level pitch and centered vanishing axis. Mentioning a horizon, sky, or drifting clouds inside an enclosed interior prompt fails this gate; optical-flow radiation phrases inside static tripod prompts also fail.
- **Geometric Tool Lock Gate (MTAL Gate - P0)**: Verify that all non-sterile active videos explicitly define the manual tool (MTAL) with specific color, geometric shape, and material properties (e.g., `matte-black rectangular steel shovel head` or `solid-blue heavy-duty paint roller`), rather than vague terms, to block morphing/flicker.
- **Temporal Physics Skeleton Gate (P0)**: Verify that every `time_sequence` beat declares `shot_family`, `beat_type`, `single_physical_operation`, and a complete `causal_path` with material source, entry path, tool contact, movement path, at least two persistent traces, and next-frame inheritance.
- **Threshold Bridge Continuity Gate (P0 — TBCP)**: Any exterior→interior crossing must follow the Threshold Bridge Consistency Protocol. It must be split into two bridge clips joined by a shared Sill Handoff IMAGE (no single clip performs the whole crossing); the two interior landmarks peeked through the opening in `IMAGE T` must be the exact same objects inherited as the interior primary anchors in `IMAGE T+2` (Anchor Inheritance); the bridge camera must lock identical lens + height across exterior and interior families and translate forward only (no pan/tilt/roll); the lighting change must be a gradual exposure/white-balance roll attributed to door-shade + doorway backlight with no brightness snap; the door-frame edges must slide symmetrically outward as a wipe; at least one material or light source must continue unbroken across the sill; and Bridge-1 tail = Bridge-2 head = the Sill Handoff IMAGE must be declared. Any threshold_bridge beat must also stay isolated from construction work. Peeked anchors must be plausibly pre-existing features at crossing time (original structure, natural formations, or items installed in an earlier on-camera beat — never future construction products such as uncarved stairs or unplaced furniture), and each peeked anchor's declared frame-height scale must increase monotonically across `IMAGE T` → `IMAGE T+1` → `IMAGE T+2`.
- **Anchor Review (P0 — staged execution)**: When operating in Staged Execution Mode (Step 6.5), `IMAGE 1` must be rendered and shown to the user before any other `IMAGE`/`VIDEO` is composed or rendered. The server judges nothing, so you must check it yourself against Clean Frame Boundary, Camera DNA plausibility, Primary Landmark presence, genuine construction-grade damage, Genre DNA tone match, and no text artifacts — and say what you see. An anchor you believe is wrong must be corrected and re-rendered, never silently accepted.
- **Rendered Text Artifact Gate (P0)**: Post-render video QA fails if any extracted frame contains visible numeric overlays, percentage glyphs, caption-like text, or model-rendered prompt notation.
- **Hard Transition Peak Gate (P0)**: Post-render video QA fails when 3fps frame-difference spikes indicate scene replacement or hard cuts that were not declared as threshold bridge motion.
- **Agent Boundary Pop Gate (P0)**: Post-render video QA fails when a worker or tool appears/disappears at segment boundaries without an entry/exit path.
- **Landmark Drift Gate (P0)**: Post-render video QA fails when primary landmarks, horizon line, or vanishing direction drift beyond the locked shot family.
- **Object Birth Without Path Gate (P0)**: Post-render video QA fails when fixtures, solar panels, furniture, walls, railings, lights, or tools appear without a visible source and movement path in the prior frames.
- **State Regression Gate (P0)**: Post-render video QA fails when a completed state reverts to an earlier construction state without a declared removal or rollback beat (a declared temporary-works-strike beat is a legal removal, not a regression).
- **Phrasing Repetition Gate (P0)**: Before finalizing IMAGE N+1 or VIDEO N, compare its sentence structure, opening clauses, and verb choices against the immediately preceding IMAGE/VIDEO of the same type. Reusing the fixed required openers (Camera DNA block, "Use the provided first frame..." anchor sentence) is correct and mandatory — but reusing the *same subsequent sentence template, clause order, or verb set* beat after beat fails this gate. Deliberately vary sentence rhythm, subject phrasing, and verb selection every beat while keeping every required structural element and locked anchor.
- **Word Count Self-Check Gate (P0)**: Before finalizing each IMAGE or VIDEO, count its words against the hard validator limit (IMAGE 170 words, VIDEO 180 words — no exception for complex/reference mode or drift-sensitive space work). If over budget, trim redundant adjectives, filler phrases, and restated boilerplate first — never by deleting required structural elements (Camera DNA, Out-and-In Passage, pacing control phrase, audio clause, Ghost Clause, Mirror Consistency Clause).

- **Cumulative State Gate (P0)**: Any IMAGE anchor that drops a permanent change or trace from an earlier beat without an explicit covering operation in a named beat fails. Any adjacent anchor pair that differs by more than the declared milestone package's result fails.
- **Construction Sequence Violation Gate (P0)**: Any beat order that violates a hard veto (wiring after enclosure, finish coat before primer, roof before structure, floor finishing before overhead/wet work) fails before prompt rendering.
- **Power Chain Gate (P0)**: any set containing a practical light, lamp, or powered fixture activation must contain an earlier wiring/rough-in beat, run before the panels that conceal it, plus a visible power-source installation beat (solar panel, battery bank, generator) for off-grid carriers. Absence of the wiring beat fails validation even when the relative order of the beats that do exist is legal.
- **Enclosed-Space Provenance Gate (P0)**: any interior chamber appearing behind a newly opened shell without either an explicit pre-existing-space statement in the opening beat or its own excavation/mucking-out beats fails before prompt rendering.
- **(Reverse-Engineering) Banned Element Contamination Gate (P0)**: Any IMAGE or VIDEO prompt that mentions an element listed in `banned_elements` fails. Any beat-derived claim that lacks at least one `evidence_frames` filename fails.

**P1 Rewrite Gates** — keep shot family, rewrite motion segments:
- Weak planning fields
- Vague camera/framing lock
- Shortened object-ledger names
- Second system leakage
- Vague motion wording
- Thin choreography
- Low action density (reads as one subject doing one verb with no repeated cycles or continuous progression)
- Missing progress markers (any ordinary video lacks at least two measurable progress markers)
- Missing pacing control phrase (`continuous construction time-lapse, not real-time footage`)
- **Scene Text Isolation Gate (P1)**: If any IMAGE or VIDEO prompt describes a scene that could reasonably contain diegetic text (storefronts, signage, workshop labels, street markings), verify the prompt includes an explicit instruction to keep that text invisible or out of focus. Add `no readable text, signs, labels, or visible typography in the scene` to any affected prompt slot.

**P2 Polish Gates** — rewrite only tail rhythm:
- Dead tail
- Tail micro-noise
- Vague wording
- Undercooked hold polish
- **AI-Filler Scrub**: Actively filter AI-style buzzwords: `perfect`, `flawless`, `seamless`, `pristine`, `clean CGI style`, `high-end render`. Scrub instant-transformation wording from mid-clip positions: `transforms`, `becomes`, `now features`, `is now complete`, `suddenly`. Mid-clip motion must use progressive forms (`-ing`, `partially`, `growing`, `half-covered`); finished-state descriptions belong only in the reward VIDEO and IMAGE anchors.

2. Compile a structured **Quality Audit & Verification Report** (提示词质量审核报告) in Chinese detailing the status of the generated prompts. The report must contain explicit checks for the SCUP Quality Audit metrics:
   - **归一化九宫格锁定与相对位置锁 (NGCS Grid & RPL)**: Landmarks use absolute Grid A1-C3 coordinates; secondary objects are grouped and locked relative to the nearest Primary Landmark using RPL to prevent coordinate dilution and cross-contamination in the T5 encoder.
   - **动态关键帧投影 (DKP)**: Push-in shots contain dynamic keyframe projection coordinates at t=0s, 4s, and 8s with optical flow direction constraints.
   - **隐性状态持久化 (OSPL)**: Occluded landmarks and ledger objects are maintained via the Ghost Clause instead of being omitted.
   - **质量与物流守恒 (VMFP & RCE)**: Bulk construction materials have quantified volume-change descriptions; all loose or fluid materials are encapsulated inside rigid containers during transport to prevent uncontrolled dissolving or flickering.
   - **镜像反射对齐 (RHMA-Blur)**: Reflective floor surfaces default to heavy-matte, high-blur diffused reflection (RHMA-Blur) to prevent high-frequency reflection flickering.
   - **无幽灵首尾过渡 (Clean Frame)**: All static IMAGE anchors contain zero active workers or machines.
   - **双向进出通道锁 (Out-and-In Passage)**: Every VIDEO featuring workers explicitly describes the entry path at t=0s and the exit path at t=7.5s.
   - **单兵轮廓锁定 (HAL)**: Workers in VIDEO prompts are locked as solid-color safety-vest silhouettes (Hero Agent Lock) to prevent identity morphing.
   - **全局因果痕迹锁 (GCTR)**: Every addition, removal, repair, cleaning, installation, assembly, transport, or machine-assisted change leaves at least two visible contact traces in IMAGE N+1, proving the change was physically caused.
   - **首帧复核 (Anchor Review — staged execution only)**: When a renderer is driving the skill, the actual rendered `IMAGE 1` — not just its text prompt — is put in front of you and the user before any other beat is composed or rendered. Nothing judges it automatically, so you check it against Clean Frame Boundary, Camera DNA plausibility, Primary Landmark presence, genuine construction-grade damage, Genre DNA tone match, and no text artifacts, and say what you see; the packet is then reconciled against that render (Packet Reality Reconciliation) so downstream beats describe confirmed reality, not the pre-visualized spec.
   - **盲区预描机制 (PBISP)**: The static IMAGE preceding a threshold bridge pre-visualizes at least two high-contrast interior landmarks through the door opening; those landmarks are plausibly pre-existing features at crossing time (original structure, natural formations, or previously installed items — never future construction products), and their frame-height scales rise monotonically across the bridge IMAGEs.
   - **外进内门槛桥协议 (TBCP - P0)**: Any exterior→interior crossing is split into two bridge clips joined by a shared Sill Handoff IMAGE; the two doorway-peeked landmarks are inherited as the interior primary anchors (Anchor Inheritance); the bridge camera locks identical lens + height and translates forward only; the lighting change is a gradual exposure/white-balance roll attributed to door-shade and doorway backlight (no snap); the door-frame edges wipe symmetrically outward; at least one material or light source continues unbroken across the sill; the frame hand-off (Bridge-1 tail = Bridge-2 head = sill IMAGE) is declared; the peeked anchors qualify as pre-existing features; and their declared scales increase monotonically across the three bridge IMAGEs.
   - **关键帧多宫格拼图及分析密度锁定 (Keyframe Collage & Adaptive Dense Analysis Lock - T0/P0)**: When reverse-engineering or analyzing video, a 5-column tiled keyframe collage must be auto-generated via FFmpeg and saved alongside the source file as `<video_name>_collage.jpg` (T0 priority); for clips with 90 or fewer extracted frames, all frames are sent for semantic analysis; for longer clips, at least 40% of extracted frames — never fewer than one per second — with mandatory start, peak, and end frames for every change segment plus adjacent before/after frames around each peak (P0).
   - **变化事件覆盖锁 (Change Event Coverage - P0)**: CV scanning must output `change_events`; every change event must be bound to `time_sequence.source_event_ids` and referenced in the matching VIDEO prompt, ensuring no brief but critical process detail from the source video is missed.
   - **无文字伪影规则锁定 (NLVTR Lock - P0)**: The final IMAGE and VIDEO prompts must contain no mathematical percentage symbols (`%`), numeric ratio ranges, or technical acronyms (`TSPA`, `HAL`, `VMFP`, `GCTR`, `RPL`, `RCE`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`) to prevent text-overlay artifacts from being rendered on the generated video.
   - **单里程碑施工包锁 (Visible Milestone Package Lock - P0)**: Each beat produces one named terminal stage at its FULL declared extent/count, using one operation or at most three tightly related same-zone actions. Both primary-product and secondary-material progress remain continuous; token patches and unrelated cross-phase bundles fail.
   - **亚像素相机视场固定锁 (Sub-Pixel Coordinate Pinning Lock - P0)**: Every prompt pins camera attitude by shot family — level exteriors declare the horizon line height (e.g., `horizon line remains perfectly level at exactly 50-percent height of the frame`); elevated/tilted shots declare the locked pitch angle and vertical convergence; enclosed interiors declare a level pitch and centered vanishing axis, never a horizon or sky — eliminating sub-pixel camera drift without forcing impossible geometry.
   - **几何工具及单兵动作锁 (Geometric Tool & Active Motion Lock - P0)**: Every hand tool must be described with specific color, material, and geometric head shape (e.g., `matte-black rectangular steel shovel head`) to prevent the model from merging the tool with the background or randomly morphing it.
   - **施工顺序验证锁 (Construction Sequence Validation - P0)**: The beat ladder follows real construction dependencies (demolition → structural repair → rough-in systems → ceiling panels → wall panels → surface coating → floor finishing → fixtures → furniture); no hard vetoes violated (e.g., no wiring after panel closure, no finish coat before primer, no light activation without an earlier wiring beat).
   - **供电链完整性锁 (Power Chain - P0)**: Any practical light activation is preceded by an on-camera wiring/rough-in beat run before panel closure, plus a visible power source (solar panel, battery bank, generator) for off-grid carriers; no lamp ever lights without an installed fixture and a traceable power path.
   - **封闭空间成因锁 (Enclosed-Space Provenance - P0)**: Every interior chamber behind a newly opened shell is either declared pre-existing (natural cavity, original room) at the opening moment, or earns its own excavation and mucking-out beats before any finishing work; interior volume plausibly fits the exterior shell.
   - **体积守恒锁 (Volume Conservation - P0)**: Removed or delivered material volume is plausibly accounted for by container scale, trip count, or a visibly growing spoil pile; cut-out solid pieces receive explicit pry-out and carry-out actions instead of vanishing.
   - **驻场设施锁 (Persistent Site Plant - P1)**: Scaffolding, formwork, shoring, and cribbing persist across anchors between a named erection beat and a named temporary-works-strike beat instead of blinking in and out per clip; fresh unset concrete is always shown supported by its formwork.
   - **累计状态精确性锁 (Cumulative State & Anchor Delta - P0)**: Each IMAGE inherits all permanent traces from every prior beat; the delta between IMAGE N and IMAGE N+1 equals exactly the result of VIDEO N's single declared operation — no side progress, no bonus cleanup, no unrelated zone improvements.
   - **进度流控制锁 (Progress Flow Control - P0)**: A single VIDEO completes one unmistakable named milestone across its full declared region/count, shows the first instance of every included action plus repeated cycles, carries both primary-product and secondary-material progress lines, and keeps construction state monotonic.
   - **VIDEO 空间完成度描述锁 (Spatial Completion Extent - P1)**: Every VIDEO prompt includes concrete spatial start-and-end extent descriptions matching the adjacent IMAGE anchors (e.g., "floor panel advances from the left third of Grid C to cover the entire C row"); progressive `-ing` verb forms in mid-clip; completion language reserved for the final moment of the clip only.
   - **物料可信度锁 (Material, Access & Crew Plausibility - P1)**: Bulk materials are staged in a visible stockpile or visibly delivered into the scene in a prior anchor before use; wet materials at a VIDEO's end show a cured or dried state in the next IMAGE; overhead work has a visible ladder or scaffold; loads beyond one person's capacity require a second worker or machine.
   - **可数物体清单纪律锁 (Countable Inventory & Reveal Discipline - P1)**: Major countable elements (beams, crates, shelf boards) have explicit counts; count changes follow on-camera declared actions; the final Reward IMAGE contains no object that was not installed or carried in during a prior named beat.
   - **P0/P1 Gates status** (self-check gate trigger status)

### Step 10: Output Formatting

1. Emit the complete prompt set as one Markdown fenced `text` block per the Output Contract:

````text
```text
图片提示词
图片 1:
Generate an image of ...

图片 2:
Generate an image of ...

视频提示词
视频 1:
Use the provided first frame and last frame as exact composition anchors. Use IMAGE 1 as the actual first-frame image and IMAGE 2 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. ...

视频 2:
Use the provided first frame and last frame as exact composition anchors. Use IMAGE 2 as the actual first-frame image and IMAGE 3 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. ...
```
````

Copy-safety rules:
- `图片提示词`, every `图片 N:`, `视频提示词`, and every `视频 N:` must each be on its own line.
- Only use exact Chinese labels: `图片提示词`, `图片 N:`, `视频提示词`, `视频 N:` — with exactly one exception below.
- **Bridge tag (required for threshold crossings)**: any `VIDEO` slot that is one of the two Threshold Bridge Consistency Protocol clips (Bridge-1 approach or Bridge-2 cross-and-settle — see Step 8's Threshold bridge section) MUST be labeled `视频 N [BRIDGE]:` instead of plain `视频 N:`. The renderer's pairwise continuity judge (`prompt_pipeline.py:_parse_prompt_slots` / `run_vlm_qa_check`) reads this exact bracketed annotation to decide whether an exterior-to-interior camera jump is an intentional threshold crossing or an error; without it, a legitimate bridge transition gets auto-rejected as a continuity failure and wastes retry budget. Do not tag ordinary (non-bridge) VIDEO slots — only the two bridge clips. IMAGE slot labels never take this tag (only VIDEO slots do).
- Never collapse labels onto the same line as prompt body.
- One blank line between each slot.
- No Markdown headings, bullets, tables, or wrapped prose inside the fenced block.

2. Immediately below the fenced `text` block, append the structured **Quality Audit & Verification Report** (提示词质量审核报告) in a Markdown table. Do not put the report inside the fenced `text` block.

### Step 11: Direct Text Delivery

After Step 10, stop. The final answer must contain the copy-ready fenced `text` block, followed immediately by the structured Quality Audit & Verification Report in chat. Keep any surrounding prose minimal.

### Step 12: Auto-Save to Idea Library

After delivering the prompts and audit report to the user, **immediately and silently** save the result to the creative-idea-generator idea library running at `http://127.0.0.1:8085`. This step is **mandatory and non-negotiable** — do not skip it, do not ask the user for confirmation, do not mention it unless an error occurs.

**Execution**:

Run the helper script at `C:\Users\video\.codex\skills\gemini-veo-restoration-composer\scripts\save_to_library.py` via the `run_command` tool with the following arguments, substituting the real values from the just-generated output:

```powershell
python "C:\Users\video\.codex\skills\gemini-veo-restoration-composer\scripts\save_to_library.py" `
  --title       "<the Chinese topic title, e.g. 做一个废弃阁楼翻新>" `
  --prompt_block "<the complete raw text inside the fenced text block, with newlines preserved>" `
  --audit_md    "<the Quality Audit table markdown>" `
  --creativity  "gemini-veo-restoration-composer" `
  --server      "http://127.0.0.1:8085"
```

**Rules**:
- `--title`: Use the exact Chinese topic/theme string parsed in Step 1 (the user's original input or the selected Tier-0 seed). Do NOT invent a new title.
- `--prompt_block`: Pass the **raw text content** of the fenced block (not the fences themselves). Preserve all newlines.
- `--audit_md`: Pass the raw Markdown table from the Quality Audit report.
- `--creativity`: Always `"gemini-veo-restoration-composer"` for this skill.
- If the server is unreachable (exit code 2), notify the user: "⚠️ 点子库服务未运行，提示词已生成但未能自动入库。请先启动 creative-idea-generator 服务（run.bat），再手动触发保存。" Then stop — do not retry in a loop.
- On any other error (exit codes 3–4), show the stderr output to the user in a brief note.
- On success (exit code 0), output a single line: `✅ 已自动入库：<title>`

### Step 13: Auto-Trigger / Prompt Image Generation (连贯画幅帧序列作图)

In the default staged flow, `IMAGE 1` has *already* been rendered and looked at by this point (via `render_and_gate_anchor.py`, called mid-turn before the rest of the prompt set was even composed) — this step now only needs to trigger rendering of the *remainder* (`IMAGE 2-(N+1)` and `VIDEO 1-N`). If staging was waived (user asked for text only, or the render server was unreachable), determine now whether the user wants images generated after all (e.g. explicitly asked for "作图", "生成图片", "渲染", "预览", or the server has come back up).

**Execution Logic**:
1. **Auto-Trigger**: If rendering was already decided (Step 6.5) or the user explicitly requested image generation, immediately and silently trigger the remaining sequence generation.
2. **Interactive Offer**: If neither applies, append the following text to the end of your response to offer it:
   `💡 如需直接生成该套提示词的连贯效果图，您可以回复“开始作图”或“生成预览”。`
   If the user subsequently replies with "开始作图", "生成预览" or any request to generate images, trigger it.

**How to Trigger**:
Run the helper script at `C:\Users\video\.codex\skills\gemini-veo-restoration-composer\scripts\generate_frames.py` via the `run_command` tool:

```powershell
python "C:\Users\video\.codex\skills\gemini-veo-restoration-composer\scripts\generate_frames.py" `
  --title "<the Chinese topic title, e.g. 做一个废弃阁楼翻新>" `
  --server "http://127.0.0.1:8085"
```

This script posts to `/api/render_staged`. Frames already on disk are reused, so in the normal case (per Step 6.5) it renders only the remainder; in the deferred-decision case it renders `IMAGE 1` too. Rendering runs no consistency review of any kind — frames land as `pending_manual_review` and the user can run the frame grid's 「🔍 一致性审查」 afterward if they want one.

**Rules**:
- Use the exact same Chinese topic title used in Step 12 (and, if Step 6.5 ran, the same title passed to `render_and_gate_anchor.py`).
- Keep the terminal command running until completion so the user can see real-time updates: which frame is generating and any upstream retries.
- On success, present a brief confirmation: `✅ 帧序列已生成完毕，存放在项目目录：outputs/<safe_project_name>/`
- If the stream reports `needs_human_review`, relay that to the user plainly instead of claiming success — this is the one legitimate escalation path in an otherwise fully autonomous flow.

---

## Dirty-State Baseline Vocabulary

When composing IMAGE 1 trauma pathology, use specific damage descriptors, not soft-focus words.

**Banned soft-focus words**: `worn`, `aged`, `dirty`, `messy`, `in disrepair`

**Required pattern**: location + surface-material state + damage type

Reusable pathology templates:

| Space Type | Pathology Template |
|---|---|
| `road` | `foreground full-lane asphalt surface shows severe pothole breakouts with exposed aggregate and branching asphalt cracking; mid-lane wheel path carries oil-darkened staining and loose gravel scatter; curbside concrete edge shows spalled corners and broken joint lines` |
| `interior` | `lower wall span shows full-width plaster spalling with lath exposure; ceiling-to-floor moisture columns stain the mid-left drywall with dark runoff streaking; foreground subfloor carries scattered demolition rubble and broken finish fragments` |
| `abandoned property` | `upper ceiling bay shows collapsed section with hanging insulation and torn vapor barrier; full-height metal surface carries heavy rust streaking and flaked coating loss; foreground concrete slab holds broken glass fragments and damp debris accumulation` |

---

## Camera Defaults Quick Reference

| Space Type | Lens | Camera Height | Perspective |
|---|---|---|---|
| Interior / Exterior / Road / Pool / Backyard | ultra-wide 14-18mm | 1.4-1.8m (prefer 1.6m) | 1-point or locked 2-point |
| Custom build object | natural 35-50mm | 0.9-1.3m (prefer 1.1m) | Subject-centered |
| Elevated mezzanine shot | ultra-wide 14-18mm | 2.5-3.5m (prefer 3.2m) | Steep downward diagonal |

**Do not use** for space scenes: `24mm`, `28mm`, `35mm`, `natural lens`.

---

## Lighting Phase Ladder

Five fixed phases, hold or +1 only:

```
ambient only
→ temporary work light active
→ fixture install in progress
→ partial practical activation
→ final practical stabilization
```

Rules:
- One prompt set keeps one lighting logic.
- No skipping phases.
- No day/night jumps without cause.
- Inherit key-light direction, shadow fall, white-balance, and exposure across phases.

---

## Passive Environmental Timelapse Layer

Lock direction across all clips; vary only observational detail per clip.

**Exterior defaults** (cloud-flow + wind + river):
```
cloud bands stream [direction] across the overcast sky, riverside grass and tree leaves sweep in [same direction], and the river surface shows fast downstream streaks and changing muddy silt ripples
```

**Interior/Underwater defaults** (caustic + sediment + shimmer):
```
water-caustic reflections drift faster across floor and wall panels, suspended sediment slides outside the glass, and the green river light shimmers
```

**Plain interior defaults** (enclosed dry interiors — rooms, caves, cabins, shells):
```
fine dust motes drift through the work-light beam, the daylight patch from the doorway creeps slowly across the floor, and loose sheeting edges tremble in the draft
```
Never describe sky, clouds, or weather inside an enclosed interior clip; use only passive motion that is physically visible within the enclosed frame.

**Variation pattern** (mandatory — no identical copies across clips):
- VIDEO 1: `cloud bands drift left-to-right across the overcast sky, riverside grass leans steadily in the same wind`
- VIDEO 3: `the same left-to-right cloud bands now carry thinner breaks of pale light, and grass tips bend further`
- VIDEO 5: `cloud bands continue left-to-right but are denser and darker, grass sways in gusts`

---

## Object Persistence Rules

Objects may only exist in three states:
- `inherited in place`
- `human-moved` (must state movement and destination)
- `human-carried-out` (must show carry-out action)

Birth limit: each non-reward VIDEO may introduce at most **one** new support-object class, and only if it exists in IMAGE N+1 or is visibly carried in.

**Countable Inventory And Reveal Discipline**:
- State explicit counts for major countable elements in IMAGE anchors and VIDEO prompts (e.g., three roof beams, two crates, six stacked panels). Counts may change only through declared on-camera action.
- The final Reward IMAGE may not contain any object that was not installed or carried in during a prior named beat. Decor, plants, lamps, and props either get their own furnishing beat or do not appear in the reward anchor.

---

## Material, Access, And Crew Plausibility

- Bulk materials (panels, boards, flooring, fixtures) must be staged in a visible stockpile in a prior anchor or visibly carried/delivered into the scene inside the VIDEO before they are used.
- Removed material must either visibly exit the scene boundary or persist as a stockpile in later anchors until it visibly exits. Stockpile volume should roughly match what was removed — this match is a hard requirement enforced by the Volume Conservation Gate (P0), not a style suggestion. Containers must be scaled to the load: hand buckets and crates for small debris only; cubic-metre-scale removals need mechanical containers or repeated trips with a visibly growing spoil pile.
- If a VIDEO ends with wet material (paint, concrete, adhesive, plaster), the next IMAGE anchor must show the cured or dried state of that material (matte sheen, set surface, cured bead), using the time jump between anchors as the drying interval.
- Work above comfortable arm reach requires a visible ladder, scaffold, or standing surface described in the VIDEO. Hand-carried ladders and step platforms may enter and exit within one clip like any other transient tool; erected scaffolding follows the Persistent Site Plant Exception — it stays in place across anchors until its declared temporary-works-strike beat.
- Loads beyond one person's plausible capacity require a second worker or a machine; describe the shared or mechanical lift explicitly.

---

## Audio Texture Defaults

| Phase Type | SFX Examples | Ambient Examples |
|---|---|---|
| Debris clearing | glass crunch, bag rustle, shelf scrape | hollow room tone, light wind |
| Excavation | shovel scrape, soil thud, root snip | forest wind, muted machinery |
| Structural repair | drill taps, welder crackle, pipe drag | worksite hum, restrained echo |
| Installation | bolt ratchet, panel thud, wood tap | enclosed room tone, soft wind |
| Finishing | brush scrape, water drip, switch tick | quiet room tone, ventilation hum |
| Threshold crossing | bootsteps, gear rustle, sole-on-concrete | work-light hum, threshold air tone |
| Final reward | echoing footsteps on [floor material] | environment-specific ambient (drip, hum, wind) |

---

## Cross-Skill Integration

### With `tiktok-abandoned-rebirth` (upstream)

When a validated topic brief exists:
- Parse `VARIABLES`, `PRODUCTION BEAT LADDER`, `ROUTING`, `TIMELAPSE CONTROL`, `PASSIVE ENVIRONMENTAL LAYER`, `ANCHOR EXPANSION GUIDANCE`, `VOICEOVER LAYER`, and `HANDOFF` as binding inputs.
- Do not reinvent topic, reward, route, or construction order.
- After prompt generation, update `references/used-topic-ledger.md` with Topic DNA row + avoid entries.

### With `restoration-timelapse-engine` (peer)

This skill produces output that conforms to all engine contracts:
- Drift Lock Contract
- Anchor-First Opening Contract
- Output Order Contract
- IMAGE-VIDEO Frame Binding
- Object Persistence Contract
- Lighting Phase Contract
- Motion Delta And Time-Lapse Contract
- Physical Realism Contract
- Ambient Audio Contract
- Silent Self-Check Gates

The output is interchangeable with direct engine invocation.

---

## Reference Loading Guide

Load these files for every composition task:

- [references/idea-engine.md](references/idea-engine.md)
  Five-axis morphological banks, novelty filters, scoring rubric, and continuous-supply ratchet. Load for Tier 0 idea requests (Topic Ideation Engine).

- [references/used-topic-ledger.md](references/used-topic-ledger.md)
  Dedup memory of already-used Topic DNA. Read during ideation to reject repeats; append a row when a topic is selected for composition.

- [references/space-workflows.md](references/space-workflows.md)
  Construction macro per space type. Load after Step 3.

- [references/drift-lock-assembly-guide.md](references/drift-lock-assembly-guide.md)
  Step-by-step packet assembly with field-level examples. Load during Step 6.

- [references/spatial-consistency-upgrade-protocol.md](references/spatial-consistency-upgrade-protocol.md)
  Spatial Consistency Upgrade Protocol (SCUP) specifications. Load during Step 6, 7, 8.

- [references/threshold-bridge-consistency-protocol.md](references/threshold-bridge-consistency-protocol.md)
  Threshold Bridge Consistency Protocol (TBCP) for exterior→interior crossings: bridge split into two clips + shared sill handoff frame, anchor inheritance, single-variable bridge camera, exposure/white-balance soft roll, door-frame wipe, cross-threshold tether, frame hand-off lock. Load during Steps 6-8 whenever Mode = Threshold.

- [references/prompt-templates.md](references/prompt-templates.md)
  Canonical IMAGE and VIDEO templates with fill-in slots. Load during Steps 7-8.


## Examples

- [examples/minimal-input-warehouse.md](examples/minimal-input-warehouse.md)
  Tier 1 input → full prompt set. Shows auto-inference of all parameters.

- [examples/double-height-loft-elevated-shot.md](examples/double-height-loft-elevated-shot.md)
  Complex interior with elevated mezzanine camera, multi-level landmarks, and 8-beat ladder.

- [examples/threshold-mode-buried-vehicle.md](examples/threshold-mode-buried-vehicle.md)
  Threshold routing with exterior → bridge → interior transition.

- [examples/threshold-mode-hollow-oak-tree.md](examples/threshold-mode-hollow-oak-tree.md)
  Threshold mode with exterior → bridge → interior transition inside a hollow tree trunk. Demonstrates closed-trunk start logic (zero man-made traces in IMAGE 1), sequential portal carving, and material carriage logistics.

---

## Delivery Style

- Deliver copy-ready prompts directly in the fenced `text` block.
- Do not show planning process, drift-lock packet, or internal field labels.
- Speak to the user in their language (default zh-CN); keep all model-facing IMAGE/VIDEO prompt bodies in English.
- Ask follow-up questions only when the missing detail would change routing or reference handling.
- For ambiguous short inputs, infer reasonable defaults and proceed.
- After delivery, the chat response is the final deliverable.
