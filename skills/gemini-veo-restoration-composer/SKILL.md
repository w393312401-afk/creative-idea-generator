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

1. Never write the full `IMAGE 1-(N+1)` + `VIDEO 1-N` set in your first pass. **Every `IMAGE` that introduces a new shot family stops for its own render gate** — not just `IMAGE 1`. Compose that frame alone, then call `scripts/render_and_gate_anchor.py` **synchronously in the same turn** and wait for the frame (mechanics in Step 6.5).
   - Non-Threshold run: one stop, at `IMAGE 1`.
   - Threshold run: **two** stops — `IMAGE 1` (exterior family) and `IMAGE T+1` (interior family). `IMAGE T+1` is the birth frame of all three interior primary anchors and the ancestor of every later interior frame; TBCP itself calls the crossing "the single most failure-prone beat". Leaving it ungated meant the entire interior section ran with no check at all, and a wrong shell propagated cleanly to the end of the ladder.
   - Declared hard-cut (`[CUT]`) entry: the cut beat's resulting `IMAGE` is the new family's first frame and takes the second stop.
2. Only after the script exits 0 and you have shown the rendered frame to the user may the remaining `IMAGE`/`VIDEO` slots **of that family** be composed and delivered. The server runs **no** automatic judgement on any frame (2026-08-05: all generation-time consistency review was removed) — never tell the user it "passed review". Show it, say plainly that nothing checked it automatically, and let them decide.
3. Exactly two conditions waive staging and permit the legacy one-pass full-set delivery:
   - the user explicitly asked for prompt text only (e.g. `只要提示词`, `不用作图`, `不用渲染`, `纯文本`), or
   - the render server at `http://127.0.0.1:8085` is unreachable (script exit code 2) — then state plainly `⚠️ 渲染服务未运行，本次跳过首帧预览，直接输出全套提示词`, deliver in one pass, and still make the Step 13 interactive offer.
4. Ambiguity is **not** a waiver. If unsure whether the user wants images, stage anyway: one anchor render is cheap; a wrong anchor can waste the entire downstream image and video budget.
5. Emitting the full prompt set without one of the two waivers is a P0 contract violation, equal in severity to a failed kill gate.

### What This Skill Automates

1. **Topic → Scene Decomposition**: Parse natural-language input into carrier, environment, trauma state, destiny, and reward action.
2. **Beat Planning & Visible Milestone Packaging**: Automatically derive the production beat ladder from the scene's construction logic. Consolidate local filler into completed, unmistakable stage products; allow up to three tightly related same-zone actions when they jointly create one terminal milestone.
3. **Drift Lock Packet Assembly**: Build the complete packet (frame aspect, camera DNA, geometry lock, fixed landmarks, frame boundaries, object ledger, worker choreography, lighting phase ladder, passive environment direction, interest budget).
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

- The user only wants a topic brief without prompts → `tiktok-abandoned-rebirth` would own this, but **it is not installed** (see Cross-Skill Integration). Produce the brief here instead of redirecting.
- The user has an existing prompt set and wants to edit specific slots → `restoration-timelapse-engine` would own this, but **it is not installed** (see Cross-Skill Integration). Edit the slots here, holding every contract in Steps 6-9.
- The user wants static interior design concepts without construction process.
- The user wants fantasy/surreal content without build logic.

---

## Where The Contracts Are Actually Enforced (read once, then stop guessing)

This package contains **prose contracts and agent-side helper scripts**. It does **not**
contain the gate runtime. Knowing which is which prevents two opposite mistakes: assuming
nothing checks your output, and assuming everything does.

| Layer | Lives in | What it does |
|---|---|---|
| **Prose contracts** | this `SKILL.md` + `references/*.md` | What you, the composing model, must write. Loaded into your context. Never executed. |
| **Machine gate runtime** | `prompt_pipeline` (a Python package at the **server project root**, *not* shipped inside this skill package) | ~40 `check_*` functions that hard-fail a slot: word limits, NLVTR notation ban, grid leakage, anchor scale lock, shot-family leakage, clean frame, bridge sterility, lighting monotonicity, and more. Runs on the **server-side** composition path (`/api/compose`, `/api/render_staged`). |
| **Contract registry** | `references/contract-registry.json` (ships with this package) | The binding between the two. Each contract names its `enforcer` as `module:function` — resolved against the **server-side** `prompt_pipeline`, not against anything in this directory. `tests/test_skill_contract_registry.py` (server project) imports every one of them, so a renamed or deleted gate turns the test red. Contracts with no executor must carry `enforcer: null` **and** a written `gap` — a registered hole, never a silent one. |
| **Agent-side helper scripts** | `scripts/*.py` (this package) | Thin HTTP clients for the local server: render one anchor, save to the library, trigger the frame sequence. Standard library only. They enforce no contracts. **One exception**: `scripts/check_character_lock.py` is a real gate, not an HTTP client — it checks a delivered prompt set against `references/cast-registry.json` and exits non-zero on a character-lock violation. It is the only Named Cast Lock enforcement that exists anywhere, server side included. |
| **Reverse-engineering pipeline** | `video_to_prompt_pipeline.py` (this package) | Tier-4 video → prompt extraction only. Its `gate_*` functions belong to the reverse path and are deliberately **not** registered in `contract-registry.json`; they are not the composition gates and are not interchangeable with them. |

**`<SKILL_DIR>` in the command examples below** means the directory this `SKILL.md` lives in,
resolved at run time on *your* machine. Substitute the real absolute path before running
anything; never copy a path literally out of this file. (These commands previously hardcoded
one developer's home directory, which is a dead link on every other machine.) The server
resolves the same directory from `server_config.json`'s `skillDir`.

**Consequences you must act on:**

1. When you compose prompts **conversationally in chat**, the `prompt_pipeline` gates do
   **not** run — you are the only thing standing between a bad slot and a wasted render
   budget. Run the Step 9 self-check gates honestly; they are the substitute.
2. `contract-registry.json` version-checks against the server's
   `server_common.SUPPORTED_CONTRACT_VERSION` (currently `1.0`). A major-version mismatch is
   reported to the frontend via `/api/mode` and means this skill package and the runtime have
   drifted apart — say so rather than composing as if nothing were wrong.
3. Two contracts are registered with `enforcer: null` on purpose —
   `anchor-frame-compliance` and `staged-anchor-render-gate` (anchor review is agent-side
   since the server stopped judging frames on 2026-08-05), plus `used-topic-ledger-dedup`
   (dedup is LLM-side only). Those are **your** job, not the runtime's.
4. **Whoever edits this package's own files must run its two health checks before calling
   the edit done** — neither is wired into any CI for this skill package, so nothing else
   catches drift here:
   - `python scripts/lint_skill.py` — mechanical checks over the package's own prose:
     word budgets, `Grid`/percentage/acronym leakage into worked exemplars, dead links,
     retired-protocol wording, and more (0 = clean, 1 = ERROR, 2 = WARN-only).
   - `python scripts/validate_contracts.py` — proves every `enforcer` in
     `references/skill-local-contracts.json` still resolves to real code in
     `video_to_prompt_pipeline.py` / `scripts/render_and_gate_anchor.py` (exit 0 = every
     contract resolves or has a documented gap; exit 1 = drifted).

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
- Never ask the user follow-up questions to fill in missing detail — infer reasonable defaults instead. This is about *not stalling on clarification*, not about output pacing: the Staged Delivery Contract (top of this file) still governs delivery, so compose and render `IMAGE 1` first and hold the rest for after the user has seen it, unless a waiver applies.

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
- **Worker Pose Tracking**: Trace worker skeletons using PoseTrack and auto-extract solid-color silhouettes for **Hero Agent Lock (HAL)**. Construction clips place the worker at the active work face from zero seconds, with immediate effective tool contact and no entrance/exit choreography.

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
        "entry_path": "rigid crates are already staged beside the Grid C2 rubble pile at zero seconds",
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

Derive `N` from the construction macro. **There is exactly one authority for the beat count:
the `Default Beats (N)` column of the routing table in
[references/space-workflows.md](references/space-workflows.md), plus its Beat Derivation
Rules.** This step does not carry its own competing default — look the number up rather than
guessing a generic range, because the ranges legitimately differ by space type (`exterior
facade` 3-4 vs `underground space` 8-12, which must install drainage, waterproofing,
ventilation and a power feed before any finish surface).

Precedence, highest first:

1. **Upstream `PRODUCTION BEAT LADDER`** (Tier 2): one listed physical phase = one `VIDEO`. Binding.
2. **A topic-specific ladder written into this file** (currently only the cliff cable car / gondola ladder in Step 5.5, which is 19 beats). Binding for that topic.
3. **The `space-workflows.md` routing table** for the classified space type. This is the default path.

Then apply the adjustments below. Feasibility rules may push the count *above* the table's
range — a preference for more content may not.
- Merge tiny beats that would produce nearly static clips (unless explicitly listed).
- **Apply the Visible Milestone Package Rule**: Every ordinary beat must end in one named, immediately legible stage product at its full declared region or component count. A beat may contain one operation or up to three tightly related actions in the same zone when all are necessary for that single terminal result (for example roof panels + door + threshold closeout, or joists + bay insulation). Split cross-phase bundles such as demolition + painting + furnishing, rough-in + the panel that conceals it, or unrelated work in different zones. Token patches, one-corner edits, and merely begun/partial states are forbidden — **full declared extent always wins over any grid-cell count below**.
  - **Adjacent-Anchor Delta Budget** (a sanity-check reminder, not a hard cap — a beat that legitimately covers a whole wall or floor is still valid even if it touches more cells than its row below suggests): the number of grid cells an adjacent `IMAGE` pair may plausibly change scales with the beat's `beat_type`, because "redo the whole floor" and "hang one fixture" are not the same size of visual delta.
    | `beat_type` | Typical cell-change budget |
    |---|---|
    | `removal` / `excavation` | up to 4 (may legitimately cover a full floor row) |
    | `coating` / `interior_finish` | up to 6 (may legitimately cover a full wall) |
    | `fixture_install` / `furnishing` | up to 3 |
    | `threshold` | cell counting does not apply (the crossing changes the whole frame by construction), but the beat is **not** unbudgeted: construction progress, lighting phase, exterior state, and camera height each get a delta of **zero** across the sill. See TBCP §6 **Crossing Delta Budget** — the clause this row defers to. |
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

A specialized, longer beat ladder exists for one topic family: cliff cable car / gondola
restoration. When the topic matches, load [references/beat-ladders/cable-car.md](references/beat-ladders/cable-car.md)
and use its 19-beat ladder instead of auto-deriving from the space type's standard workflow —
this is a deliberate, named exception to the "default 3-6 `VIDEO` clips" rule above, not a
contradiction of it. Do not use this ladder, or its length, as a precedent for any other topic.

### Step 6: Drift Lock & SCUP Packet Assembly

Build the complete packet by filling all required fields using the **Spatial Consistency Upgrade Protocol (SCUP)**. The packet's **first** field is `aspect_ratio` (default `9:16` vertical — see the Frame Aspect Lock below); every anchor cell, boundary anchor, and Z-depth scale that follows is written *for that aspect*.

**Camera DNA Block** (~25-30 words):
- Shot type (static tripod / elevated tripod / etc.)
- Lens feel (ultra-wide 14-18mm for spaces / 35-50mm for custom objects)
- Camera height (1.4-1.8m for spaces / 0.9-1.3m for objects)
- Angle and perspective axis
- Frame coverage
- Boundary anchors (left, right, top, bottom foreground band)
- **Sub-Pixel Coordinate Pinning (SPCP — shot-family conditional)**: Pin the camera's angular attitude with wording that matches the shot family. Level exterior shots: `horizon line remains perfectly level at exactly half the frame height`. Elevated or tilted shots: `camera pitch locked at the declared steep downward angle; vertical lines converge consistently toward the same vanishing direction; no horizon reference`. Enclosed interiors, caves, and windowless spaces: `camera pitch locked level; the central vanishing axis stays centered in the frame` — never mention a horizon, sky, or clouds where none can physically exist. Optical-flow radiation wording (`all optical flow lines radiate symmetrically from the optical center`) belongs only in push-in/translation clips, never in static tripod prompts.

**Geometry Lock (P0 — the shell's volume, not a mood board)**:

The four qualitative bullets this field used to carry ("wall/roof lines", "carrier
proportions") were unusable: an image model cannot draw to them, so nothing downstream ever
read the field and every interior frame re-derived the room's size from scratch. Fill all
seven subfields below, and write each one as a **relative measure against a feature that is
visible in the frame** — door widths, door heights, countable facade features. Absolute
metres are worse than useless here (they read as text-overlay bait and the model has no
scale reference for them).

| Subfield | What to write | Example |
|---|---|---|
| `clear_width` | interior width in **door widths** | `about two and a half door widths wall to wall` |
| `clear_height` | floor-to-ridge height in **door heights** | `about three door heights to the ridge` |
| `depth_bays` | depth counted in features visible on the exterior | `four rafter pairs deep`, `three bays deep` |
| `roof_form` | **one** form, at the **same pitch as the exterior silhouette**, written as an immutable fact | `single shallow gable, same pitch as the outside; no second roof form anywhere` |
| `aperture_ledger` | **exhaustive** list of every opening the shell has, entry included | `the single plank door in the gable end; two small square vents under the eaves` |
| `aperture_denylist` | explicit list of what it does **not** have | `no skylight, no roof light, no vaulted or domed ceiling, no rear-wall arched window, no second doorway` |
| `wall_material` | same material family inside and out | `the same tarred board cladding continues on the inner face` |

**The denylist is the subfield that actually does the work.** Stating what exists does not
constrain a generative model — it fills unconstrained volume with whatever the genre
suggests. Naming the absent thing does constrain it. Choose the denylist from what the model
most wants to invent in an enclosed space; the three that show up over and over in this
pipeline's failed interiors are **skylights, vaulted/domed ceilings, and a rear-wall arched
window**. Never leave this subfield empty, and never write it as "no other openings" — the
model needs the noun.

**Envelope signature**: pick **one** clause out of `clear_width` or `clear_height`, under
twelve words, and mark it as the phrase that gets restated verbatim on every interior frame
(Step 7's Shell Envelope Restatement). Keep it free of numerals and percent signs so it
survives NLVTR.

Also still locked, as before: stair direction (if applicable), full scene boundary, and the
exact opening shape/proportions of every door or archway as established in `IMAGE 1`.

When a renderer drives this skill, these subfields land in the Drift Lock Packet as
`geometry_lock` (the prose), `aperture_ledger`, `aperture_denylist`, and
`envelope_signature`, and `prompt_pipeline:check_shell_envelope_consistency` enforces them.
In chat composition nothing enforces them but you.

**Material Palette Lock (P0 — the substrate's identity, separate from its state)**:

The Geometry Lock nails down how big the shell is. This nails down what it is *made of*. They
fail differently and independently: a room can hold its exact clear width across ten frames
and still have its stone walls read moss-green in one and dry ochre in the next, because
every frame's `[material realism]` slot was written fresh and a text-conditioned model
treats a swapped adjective as a swapped object. The fix is not more adjectives — it is the
same adjectives, copied.

Register **3-5 materials** — the ones that occupy real frame area, not every material in the
scene. Each gets two fields, and the split between them is the whole point:

| Field | Mutable? | What to write |
|---|---|---|
| `substrate` | **never** | the material's fixed identity: base hue, grain/texture, finish. `coarse grey-brown fieldstone, irregular courses, dry-laid` |
| `state_track` | **monotonically**, and only on a beat that names this material | the ordered trauma→restored phrases: `thick wet moss in the joints` → `joints raked clean, stone still dark with damp` → `dry pale grey stone, tight lime pointing` |

Rules:
1. **Copy `substrate` character-for-character** into every frame that shows the material.
   Never re-describe it, never reach for a synonym, never let the lighting phase leak into it
   (`warm honey stone` is a lighting statement wearing a material's clothes).
2. **Advance `state_track` only forward, only one step, and only in the beat that does the
   work.** A material not touched by this beat carries its previous state phrase verbatim.
   A state phrase that moves backward is the same class of failure as a repaired surface
   un-repairing itself.
3. **This is word-neutral.** The palette clause goes in the `[material realism]` slot the
   IMAGE template already has — it replaces improvised wording with fixed wording, it does
   not add a new sentence. It does not consume the interior frames' 220-word allowance.
4. Applies to **both** shot families. Exterior drift is the same failure; it is merely less
   obvious because exterior frames have sky and horizon carrying continuity for them.

When a renderer drives this skill, this lands in the Drift Lock Packet as `material_palette`.
Nothing on the server reads it yet (registered as a gap in
[`references/contract-registry.json`](references/contract-registry.json) under
`material-palette-lock`) — in every mode today it is yours to run.

**Frame Aspect Lock (P0 — read before assigning any Grid cell)**:
- This pipeline renders **9:16 vertical** by default (`--aspect_ratio` on `scripts/generate_frames.py`, `config.imageAspectRatio` server-side, and every fallback in `frame_generator.py` all default to `9:16`). The whole product is a vertical short-video pipeline; do **not** assume a horizontal frame.
- Record the aspect in the Drift Lock Packet as an explicit field (`aspect_ratio: 9:16`) and let it govern every downstream spatial decision below. If a run is ever driven at a different aspect, that field changes first and the anchor layout is re-derived from it — never leave the packet silent about which frame it was written for.
- **What a vertical frame changes** (all three are P0, they are the most common source of anchor drift):
  1. **Vertical real estate is cheap, horizontal is scarce.** Separate the three depth anchors mainly **up the frame**, not across it. Anchors placed near the left/right edges of a 9:16 frame sit only a short distance off the optical axis and fall out of frame the moment the camera nudges.
  2. **Boundary anchors must be reachable within a narrow horizontal span.** `left boundary` / `right boundary` must name features that genuinely sit at the edge of a *tall, narrow* view — a near-field wall return, a door jamb, a column face — never a distant lateral landmark that only a horizontal frame would contain. `top boundary` and `bottom foreground band` carry more of the composition than they would in a horizontal frame; give them the strongest, most specific features.
  3. **Z-Depth Height Scales run smaller.** The same real object subtends a *smaller* fraction of total frame height in a tall frame. Calibrate against these vertical-frame defaults rather than horizontal intuition: background anchor roughly one-fifth to one-third, mid-depth anchor roughly two-fifths to three-fifths, foreground band roughly one-fifth to one-quarter. A mid-depth anchor declared at four-fifths of frame height in 9:16 is almost always a mis-estimate.

**Normalized Grid Coordinate System (NGCS) & Fixed Landmarks**:
`Grid A1`-`Grid C3` is an internal bookkeeping coordinate system for this step (Drift Lock Packet assembly) only — it keeps your own reasoning about landmark position and drift consistent across beats. It must **never** appear as literal text in a delivered `IMAGE`/`VIDEO` prompt (Step 7 onward): NLVTR (Step 8 point 5, Step 9's No Banned Notations Gate) fails any final prompt containing a `Grid [A-C][1-3]` token, because it renders as a text-overlay artifact on the generated image/video. Translate every Grid cell to natural language before it reaches output text: depth layer (foreground/mid-depth/background) + position phrase (e.g. `Grid B2` → "the center of the frame", `Grid C1` → "the lower-left of the frame") + the height-ratio phrase already required below. Internal packet notes, the NGCS/OSPL bookkeeping in this step, and the Quality Audit's own gate descriptions may keep using `Grid` notation freely — only the copy-ready `IMAGE`/`VIDEO` prompt bodies themselves must read as pure natural language.

- Divide the **9:16 vertical frame** into a $3 \times 3$ grid (`Grid A1` to `Grid C3` from top-left to bottom-right). Rows are depth bands (`A` background / `B` mid-depth / `C` foreground); in a vertical frame each row is a wide, short band and each column is a narrow slice — so a row change reads clearly on screen and a column change barely does.
- Assign exactly **3 Primary Spatial Anchors** across three depth zones, each tied to an absolute grid cell and visual feature. Prefer a **vertically stacked** spread (one anchor per row) over a lateral spread:
  - Foreground: 1 named anchor with Grid cell (e.g., `cracked floor seam in Grid C2`)
  - Mid-depth: 1 named anchor with Grid cell (e.g., `brick column in Grid B2`)
  - Background: 1 named anchor with Grid cell (e.g., `tall window opening in Grid A2`)
- Specify the **Z-Depth Height Scale** for each anchor (e.g. `brick column holds a scale of 60% of total frame height`) to lock the Z-axis, calibrated to the vertical-frame bands above.
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
- Workers and active machines appear only in `VIDEO` prompts. In ordinary construction clips they are already at the active work face at zero seconds, begin the first effective action immediately, and continue through the final frame.
- **Direct-at-Zero Worker Clause**: Never allocate video time to a worker entering, arriving, exiting, walking out, or leaving the frame. Use the full clip for visible construction action; a separate reward clip may be worker-free.
- **Hero Agent Lock (HAL)**: When a worker must be visible, lock them using high-contrast, low-detail silhouette terms (e.g., `one lone worker in a solid bright-neon-yellow safety vest, a white hardhat, and solid dark blue work pants; do not show the worker's face`). This remains the **default** agent lock for every run.
- **Named Cast Lock (the opposite strategy, opt-in, mutually exclusive with HAL)**: when the topic needs a recurring, recognizable *named* person rather than an anonymous pair of hands, HAL is the wrong tool — its whole method is deleting the detail that makes someone recognizable. Switch to the Named Cast Lock protocol in [references/character-consistency-protocol.md](references/character-consistency-protocol.md), declare `agent_lock_mode: named_cast` in the Drift Lock Packet, and register the character's fixed vocabulary in [references/cast-registry.json](references/cast-registry.json). **One project picks exactly one of the two and never mixes them** — mixing puts two contradictory clothing contracts in one film and the model crosses the features. Selecting Named Cast Lock voids HAL's hardhat/hi-vis requirement *for that project only*; it does not change the default here. Two clauses in this file are then explicitly overridden for that project, both documented in §1 and §4 of the protocol: the **Direct-at-Zero Worker Clause** (named cast may use entry/exit, capped at half a second each end, because a person left standing in the final frame contradicts the next Clean Frame IMAGE anchor) and, in the protocol's mode B only, **Clean Frame Boundary** (which otherwise bans people from IMAGE anchors). Verify a delivered set with `python <SKILL_DIR>/scripts/check_character_lock.py --cast <id> <prompt-set.md>`.
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
3. Render the anchor **synchronously, in this same turn**, before writing anything else, via whatever command-execution tool your environment provides (Bash, PowerShell, `run_command`, etc. — the skill itself is platform-neutral):
   ```
   python "<SKILL_DIR>/scripts/render_and_gate_anchor.py" \
     --title "<the Chinese topic title>" \
     --prompt "<IMAGE 1's prompt text>" \
     --server "http://127.0.0.1:8085"
   ```
   `<SKILL_DIR>` is this skill package's own root directory (the folder containing this `SKILL.md`), resolved by whatever harness/host loaded the skill — never a hardcoded path. On Windows, invoke this via `python "<SKILL_DIR>\scripts\render_and_gate_anchor.py"` through PowerShell with backtick line continuations if your shell needs them; the arguments and semantics are identical.
   This call blocks until the frame is on disk. Wait for it to finish; do not proceed speculatively while it's running.
4. **Exit code 0**: the frame rendered. Show the user the image path the script printed and say plainly that nothing judged it automatically (`⚠️ 服务端不对该帧做自动判定，请确认这就是你要的首帧`). Wait for their read on it before proceeding to step 6 — you are asking a person to do the job no gate does anymore. Never claim the anchor "passed" anything.
5. **Exit code 3/4/5** (server error / missing prompt / timeout): report exactly what the script said and stop. A timeout means the server is still working — do **not** treat it as unreachable and do not fall back to one-pass delivery.
6. **Exit code 2** (render server unreachable): this is the second waiver in the Staged Delivery Contract. Say `⚠️ 渲染服务未运行，本次跳过首帧预览，直接输出全套提示词`, then fall back to one-pass composition (Steps 7-11 as written) and still make the Step 13 interactive offer. Do not retry the script in a loop.
7. Treat the rendered `IMAGE 1` — its prompt text and what actually appeared — as authoritative reality. Reconcile your working model of the Camera DNA / Primary Landmarks / object ledger against it before writing anything else (this is **Packet Reality Reconciliation**): only rewrite what visibly contradicts the render; never invent a landmark or object that isn't visible in it.
8. Only now compose `IMAGE 2-(N+1)` (the rest of Step 7) and `VIDEO 1-N` (Step 8), then continue through Steps 9-11 as usual. The final delivered prompt set includes the exact same `IMAGE 1` prompt that was rendered, verbatim — do not silently rewrite it again at output time, or the delivered set will describe a different frame than the one the user approved.
9. **If you or the user judge the rendered `IMAGE 1` unacceptable** (this is the other branch of step 4's "wait for their read on it" — it was previously undocumented, which let a rejected anchor silently get treated as approved):
   a. Record the rejection reason by category (Camera DNA / Primary Landmark / damage severity / genre tone / text artifact) — this tells you which packet field to fix.
   b. Edit only the packet field(s) that category maps to; leave every other field untouched.
   c. Re-render by calling `render_and_gate_anchor.py` again with `--sequence 1` and the corrected prompt, **and pass `--force_regenerate`** — without it, a server-side disk cache may silently hand back the same rejected frame instead of a fresh render.
   d. Allow at most **3** re-render attempts. If the 3rd is still unacceptable, stop: tell the user plainly and ask for guidance (a genuine `needs_human_review` — do not keep retrying silently).
   e. **Before re-rendering, get rid of the rejected frame on disk** (delete it, or have the render call overwrite it via step c's `--force_regenerate`) — Step 13's `/api/render_staged` reuses whatever is already on disk for a sequence number, so a rejected frame left in place will silently resurface downstream even after you've moved on to a corrected version. This is the step that actually matters: skipping it means everything else in this list was wasted effort, because the polluted frame keeps propagating through the rest of the chain regardless of what you render next.

Step 13's rendering trigger, further down, does not re-render `IMAGE 1`: `/api/render_staged` reuses whatever frames are already on disk and only renders the remainder. That is one more reason Step 7's rule — deliver the rendered `IMAGE 1` prompt verbatim — is load-bearing, and one more reason step 9e above is not optional.

#### Step 6.5b: The second stop — first frame of a new shot family (P0)

Everything above describes the gate at `IMAGE 1`. Under the Staged Delivery Contract's rule
1 the **same** procedure runs again at every frame that introduces a new shot family. In
Threshold mode that is exactly one more frame: `IMAGE T+1`. Nothing about the script changes
— `render_and_gate_anchor.py` already takes `--sequence`, so pass the real sequence number:

```
python "<SKILL_DIR>/scripts/render_and_gate_anchor.py" \
  --title "<the same Chinese topic title, verbatim>" \
  --prompt "<IMAGE T+1's prompt text>" \
  --sequence <T+1> \
  --server "http://127.0.0.1:8085"
```

Compose up to and including `IMAGE T+1` (that is: the exterior frames, `VIDEO T`, and
`IMAGE T+1` itself), stop, render, look. Do not write `VIDEO T+1` or any later interior
frame until the user has read the frame — every one of them chains off it.

**`IMAGE T+1` acceptance checklist** — four items, all four must hold:

1. **Roof form, pitch AND completed ceiling underside match exterior beats (P0).** If exterior beats rebuilt the roof rafters, sheathing, or membrane, the underside of that newly built ceiling must be visible overhead — zero reverted ceiling cracks, holes, or water leaks.
2. **Openings match the aperture ledger, and nothing else.** Count them. Any opening not in
   the ledger — most often a skylight, a rear-wall arched window, or a second doorway — is a
   fail, even a small one, even in shadow.
3. **Clear width and clear height are self-consistent when converted to door widths and door
   heights.** The door is in the frame's recent history and is the only scale reference the
   viewer has; if the room reads as five door widths when the lock says two and a half, the
   shell has already drifted and every later frame inherits it.
4. **The door frame is fully out of frame, and untouched scope applies only to interior surfaces.** The door frame is fully behind the camera. Surfaces untouched by exterior beats (uninsulated bare concrete walls) remain in their raw state; elements already cleared or rebuilt in exterior beats (e.g. swept floor, completed roof framing) must read as finished without state regression.


Any item failing → correct the packet field it points at (roof/aperture failures are
`geometry_lock` and its denylist; scale failures are `clear_width`/`clear_height`; door-frame
failures are `interior_camera_dna`), then re-render **with `--force_regenerate`**, and
**delete the rejected frame from disk first**. Step 6.5 item 9e applies verbatim to this
second gate: `/api/render_staged` reuses whatever is on disk for a sequence number, so a
rejected `IMAGE T+1` left in place resurfaces and keeps propagating down the interior chain
no matter what you render afterwards. Same cap as the first gate — at most 3 attempts, then
stop and ask.

### Step 7: IMAGE Anchor Rendering (HCL & NGCS)

By default (Staged Delivery Contract), compose `IMAGE 1` here first and stop for the Step 6.5 gate before returning to compose `IMAGE 2-(N+1)` — do not write the whole `IMAGE 1-(N+1)` range in one pass. Only under an explicit waiver (user asked for text only, or render server unreachable) compose the full range in one pass as below.

Render `IMAGE 1-(N+1)` executing **Hierarchical Context Layering (HCL)** and **Syntax Pruning** (placing core spatial and grid constraints in the first 40 tokens using weight-sensitive punctuation `:` and `;` to prevent attention dilution):

**Shell Envelope Restatement (P0 — every frame, not just the first)**:
- Any `IMAGE` that is **not in `IMAGE 1`'s shot family** must carry the Geometry Lock's
  envelope signature *verbatim*, plus its `roof_form` clause and, where the frame could
  plausibly show one, the relevant `aperture_denylist` nouns. This is `IMAGE T+1` **and every
  interior frame after it** — writing it once on `IMAGE T+1` and then relying on inheritance
  is exactly how the room silently changes size three frames later. Each frame is generated
  image-to-image off the previous one, so an omitted lock is not "inherited", it is re-derived.
- `Scene inherits all landmarks, geometry, and boundary anchors from IMAGE 1` does **not**
  satisfy this. That sentence points at a frame in a *different* shot family with a different
  camera and a different anchor set; for a post-crossing frame it is a dangling reference,
  not a lock. Restate the measurements.
- Budget: this is why post-crossing interior frames run to 220 words instead of 180 (see the
  Word count budget block below). Do not pay for the envelope clause by dropping the door
  clearance sentence, an anchor's height ratio, or the Ghost Clause.

**Material Palette Restatement (P0 — every frame, both families)**:
- Every `IMAGE` writes the Step 6 `material_palette` `substrate` phrase *verbatim* for each
  registered material visible in that frame, followed by that material's current
  `state_track` phrase. Unlike the envelope signature this is not shot-family conditional —
  exterior frames drift the same way.
- A material this beat did not touch carries its **previous** state phrase unchanged. Only
  the beat that performs the work on a material may advance it, and only by one step.
- This occupies the `[material realism]` slot already present in the IMAGE templates. It is a
  substitution, not an addition — do not budget extra words for it, and do not leave the
  improvised wording in place alongside it.

**Cumulative State And Anchor Delta**:
- Every IMAGE anchor must inherit all permanent changes and traces from all prior beats. A trace may disappear only when a later named operation explicitly covers or removes it, and that covering must be the declared beat where it happens.
- The difference between IMAGE N and IMAGE N+1 must be exactly the declared operation's result of VIDEO N only — no side progress, no bonus cleanup, no improvements in other zones.
- Each progressive IMAGE anchor states completion extent in concrete spatial terms (e.g., `the left two-thirds of the wall panel installed while the right third stays bare framing`), giving adjacent-anchor interpolation an unambiguous start and end.
- **Negative-Constraint Zone Locking**: an image-editing renderer over-completes an under-constrained zone rather than under-completing it. Two recurring failure patterns confirmed by render QA logs: (1) restating an already-inherited damage descriptor from an earlier beat (e.g. `bent rib framing exposed` established back in IMAGE 1-2) inside a later beat's own change description gets misread as a fresh destructive action in that same beat, stripping wall/floor material well beyond the single declared operation; (2) an open-ended furnishing/reward beat invites invented extra props (pillows, cups, books, decor) beyond the declared object list. When a beat's operation is narrowly scoped to one zone, or its object list is closed, say so explicitly and negatively in the same anchor (e.g. `side wall panels remain in place, not stripped`; `only these listed objects are present — no additional decor, tools, or furnishings`) rather than relying on the positive description alone to bound the model.

Every `[...Grid...]` placeholder below is an authoring aid, resolved from your internal NGCS bookkeeping — the text you actually write into the placeholder must already be natural language (position phrase + depth layer + height ratio), never a literal `Grid X#` token. See the Grid-is-internal-only note above Step 6's NGCS section.

**IMAGE 1 (Before/Trauma Anchor - Clean Frame)**:
```
Generate an image of a [Camera DNA Block: static tripod shot, 14mm, height 1.6m, eye-level perspective; SPCP pitch-lock clause matching this shot's shot_family]. Locked anchors: [Primary Anchor A] at [natural-language position, e.g. "the lower-center of the frame", holding a stable visible scale of X of total frame height], [Primary Anchor B] at [natural-language position and height-ratio], left boundary [named anchor toward its natural-language position], right boundary [named anchor toward its natural-language position], top boundary [named anchor toward its natural-language position], and bottom foreground band [named anchor toward its natural-language position]. The scene is the explicit before anchor, completely empty of workers, with [trauma pathology: location + surface-material state + damage type for each major damage zone, described by natural-language position]. [Lighting phase] and [material realism]. [Natural-language guardrail: keep same framing; do not redesign].
```

> **Notation warning (P0).** The bracketed names below — relative positioning lock, causal
> trace rule, mirror consistency clause — are labels for *you*, describing what each clause
> must accomplish. **Never write the acronym or the label into the prompt body.** Writing
> `Relative Positioning Lock (RPL):`, `Global Causal Trace Rule (GCTR):`, or
> `Mirror Consistency Clause (RHMA-Blur):` into a final slot fails `check_nlvtr_violations`
> on the literal substrings `RPL` / `GCTR` / `RHMA`, and image models render such labels as
> on-screen text. Write the clause as ordinary prose instead, exactly as the templates below
> now show.

**IMAGE 2+ (Progressive State Anchors — Clean Frame, relative positioning, causal traces)**:
```
Generate an image of a [Same Camera DNA Block — character-for-character copy within this shot family, including its SPCP pitch-lock clause: subject centred in the middle band]. Locked anchors: [every primary landmark restated verbatim from the packet — exact name, prose bearing, and frame-height ratio, copied character-for-character from IMAGE 1's own anchor sentence. This is a copy, not a reference: `Scene inherits all landmarks... from IMAGE 1` is not a restatement and does not satisfy `primary-landmark-restatement` (P0) or `anchor-scale-lock` (P0)]. [2-3 most drift-prone items, each positioned RELATIVE to a named Primary Landmark — "the green toolbox sits just left of the brick column", never its own absolute cell]. The scene is the [current stage name] anchor, completely empty of workers, with [one dominant change cluster, located by prose bearing] while [inherited evidence: the exact recurring object-ledger phrases + unchanged damage/repair evidence] remain visible and unchanged. [Changed object/surface] shows at least two persistent contact traces — [fastener rows / seam lines / residue / drag marks / tool scars / machine compression / contact dust] — proving how the state changed from IMAGE N. [Lighting phase] and [material realism]. [Guardrail sentence].
```

**Final IMAGE (Reward Tail State — Clean Frame, relative positioning, blurred reflections)**:
```
Generate an image of a [Same Camera DNA Block — character-for-character copy: subject centred in the middle band]. Locked anchors: [every primary landmark restated verbatim from the packet — exact name, prose bearing, and frame-height ratio, copied character-for-character from IMAGE 1's own anchor sentence. This is a copy, not a reference: `Scene inherits all landmarks... from IMAGE 1` is not a restatement and does not satisfy `primary-landmark-restatement` (P0) or `anchor-scale-lock` (P0)]. [2-3 drift-prone items locked relative to named Primary Landmarks]. The scene is the [reward tail state name] anchor, completely empty of workers, with [reward action completed — e.g. the hatch swung open on its hinge, warm light spilling across the centre], [final-state details visible], loose temporary construction clutter carried out, and permanent causal traces still visible where they explain installed or repaired elements. [Final changed elements] retain visible seams, fasteners, contact marks, tool finish texture, or machine pressure traces instead of appearing untouched. [Mirror clause, written as prose:] The highly reflective polished floor across the bottom of the frame displays a heavily blurred, low-gloss, diffused reflection of the background; reflections are muted, dark, and highly out-of-focus, preventing high-frequency contrast or sharp detail, with a realistic Fresnel falloff near the margins. Keep [final lighting phase] and [material realism]. [Guardrail sentence].
```

**Word count budget — SINGLE AUTHORITY (P0)**

These numbers are not a style preference; they are the literal constants the runtime
validator compares against (`prompt_pipeline.validate_beat_prompts` →
`image_word_limit_for(family)` / `BASE_VIDEO_WORD_LIMIT`). Nothing else in this package may
state a different budget — if you find another number in `references/` or `examples/`, this
block wins and the other file is a bug.

| Slot | Target range | Hard ceiling (validator) |
|---|---|---|
| IMAGE — exterior / single-shot-family projects | 140-180 words | **180** — over this fails validation |
| IMAGE — post-crossing interior frames (`IMAGE T+1` onward) | 170-220 words | **220** — over this fails validation |
| VIDEO (base one-take profile) | 260-380 words | **380** — over this fails validation |

- **Why the interior frames get their own profile** (added 2026-08-08 — this is not a
  loophole, it is a budget correction). An interior frame carries a strictly larger set of
  mandatory elements than an exterior one: Camera DNA + 3 primary anchors (each with
  position / depth / frame-height ratio) + boundary anchors + Z-depth + dirt vocabulary +
  lighting phase + preserve list + Ghost Clause — *plus* the door-clearance sentence, *plus*
  the verbatim shell-envelope restatement Step 6's Geometry Lock now requires on every
  interior frame. That set does not fit in 180 words; measured, it lands at 170+ before the
  envelope clause is written at all. A model asked to fit it anyway drops whatever it judges
  least important, which in practice is the clause that was added last — the envelope lock.
  The interior family also loses a batch of required elements it can never use (horizon
  pinning, sky, cloud-flow direction, weather state), so the extra 40 words are largely the
  ones the exterior family spends on the sky.
- **Which frames this applies to**: exactly the frames the runtime tags `family ==
  'interior'` — every frame from the threshold crossing's `IMAGE T+1` onward, and every
  frame after a declared hard-cut entry. A project that is *entirely* indoors with no
  crossing has one shot family, every frame inherits directly from `IMAGE 1`, and it
  therefore does **not** need the envelope restatement and does **not** get the raise — it
  stays at 180.
- The ceiling is a **maximum**, not a goal. Aim at the middle of the target range; the
  ceiling exists so a genuinely dense beat has headroom, not so every beat runs to the wall.
- There is no other mode-based exception: complex reference-image mode and drift-sensitive
  space work are still capped at the same numbers.
- The multi-shot `omni` profile raises the VIDEO ceiling (its contract is 5 shots per clip);
  that ceiling is supplied by the profile at validation time. This skill package ships the
  `base` profile — use 380 unless you are explicitly composing under `omni`.
- **Going over is not free**: an over-budget slot fails validation and forces a regeneration
  retry, and past ~380 words the T5 text encoder measurably dilutes attention on exactly the
  spatial locks (anchors, boundaries, Camera DNA) that this whole protocol exists to protect.
- If a beat feels like it needs more room, trim redundant adjectives, restated boilerplate,
  or secondary description first — never by cutting required structural elements (Camera DNA,
  direct-at-zero worker clause, pacing control phrase, audio clause, Ghost Clause, Mirror Consistency
  Clause).
- **Front-load regardless of length** (HCL): whatever the total, the Camera DNA + the 3
  primary anchors must land in the opening ~40 tokens. The
  `spatial-consistency-upgrade-protocol.md` "first 40 tokens" rule constrains the prompt's
  *opening*, not its total length — the two are not in conflict.

### Step 8: VIDEO Motion Chain Rendering (DKP & VMFP & HAL & PBISP)

Render `VIDEO 1-N` following the Unified VIDEO Skeleton, strictly executing the **Continuous Action Flow Contract** and SCUP rules to prevent spatial jump and identity morphing:

Every VIDEO must follow this exact structure:
1. **Fixed opening**: `Use the provided first frame and last frame as exact composition anchors.`
2. **Adjacent-frame binding**: `Use IMAGE N as the actual first-frame image and IMAGE N+1 as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout.`
3. **Minimal camera/framing lock**: shot family, locked camera position, same frame boundaries, and the natural-language positions of critical fixed landmarks + boundary anchors (reasoned internally via the Grid, translated to prose before it reaches the delivered text — see the NGCS note in Step 6).
4. **First-to-last motion delta**: Describe the visible change from IMAGE N to IMAGE N+1 as a continuous physical action, not a static state transition.
5. **8-second time-lapse action chain (Continuous Action Flow & HAL)**:
   * **Rhythm**: 8 seconds of full-length continuous action progression, with the dominant motion running from the very first frame to the very last frame without static holds, stable starts, or deceleration/settling zones.
   * **Pacing control phrase**: Must include the exact phrase `continuous construction time-lapse, not real-time footage` in the paragraph (except for threshold bridge or final reveal).
   * **Direct-at-Zero Worker Clause**: Workers are already at the active work face at t=0s and make the first effective tool contact immediately, then continue the visible operation through the final frame. Never show or describe worker entrance, arrival, exit, walk-out, or a worker-free tail inside a construction clip.
   * **VIDEO-only Workers & HAL**: Workers and active machines appear only in VIDEO prompts and perform cycles of verb-first active labor. If visible, workers are locked using Hero Agent Lock (HAL) solid-color silhouettes — **unless** the run declared `agent_lock_mode: named_cast`, in which case every person-bearing VIDEO instead restates the registered character's short identity block verbatim, exactly once, ahead of the first action verb, and carries the three always-on negatives. See [references/character-consistency-protocol.md](references/character-consistency-protocol.md); never mix the two locks inside one project.
   * **Volumetric Mass & Flow Preservation (VMFP) with Rigid Encapsulation**: Describe bulk materials encapsulated inside rigid containers (buckets, crates, wheelbarrows, bags) with explicit quantities, and show their fill level changing in natural language, never a percentage (e.g. `sand is encapsulated inside three solid dark-green plastic buckets in the foreground, each holding 15 liters; the buckets fill from empty to heaped as the pile is cleared, then are carried out of frame along the path toward the left edge`). Do not describe loose shovel actions without containers. Scale containers to the material volume: hand buckets and crates only for debris under roughly half a cubic metre; larger removals require mechanical containers (excavator buckets, skips, chutes, tracked carriers) or explicitly repeated trips feeding a spoil pile that visibly grows across anchors. Any cut-out slab, panel, or door-sized solid piece must get its own pry-out and carry-out action — crumbs in buckets never account for a large solid piece.
   * **Measurable Progress Markers**: Mandate at least **two** measurable progress markers (e.g., exposed area increasing from 10% to 90%, wall-covering panel row count completing, spoil pile height growing from 20% to 70% of the frame).
   * **Spatial Completion Extent (SCE)**: Every VIDEO must state the operational start and end extent in concrete spatial terms that match the adjacent IMAGE anchor descriptions (e.g., `the floor panel advances from bare concrete across the left third of the foreground to fully covering the entire foreground band`). Use progressive `-ing` verb forms for mid-clip descriptions; completion language is reserved for the final moment before the clip ends.
   * **Global Causal Trace Rule (GCTR)**: Every added, removed, repaired, cleaned, installed, assembled, or machine-moved element must show its source/entry path, contact method, movement path, attachment/removal evidence, and at least two persistent trace markers that land in `IMAGE N+1`.
   * **Banned Transition Shortcuts**: Strictly forbid any mention of `cross-dissolve`, `fade-in`, `suddenly`, `magically`, `rapid montage`, `jump cut`, or `instant transformation`.
   * **Per-clip hook**: one low-risk visual hook from the current state or the next anchor (e.g., uncovering a fixed edge at frame centre).
   * **Natural-Language Visual-Only Translation Rule (NLVTR - P0)**: The prompt composer must *never* output mathematical percentages (e.g. `%`), colons inside variables, or technical structured labels (such as `TSPA`, `HAL`, `VMFP`, `RCE`, `GCTR`, `RPL`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`, or `Out-and-In Passage:`) in the final prompt text block. This is the pipeline's actual hard-enforced list — every one of these acronyms, including `SCUP` itself, fails validation if it leaks into final prompt text. Any technical or structural constraint parsed internally must be translated into fluid, descriptive, non-technical natural language sentences detailing visual progress, kinetic movements, and persistent traces. For example, instead of writing `Two-Stage Progress Anchor (TSPA): progress marker 1: 10% to 90%`, write `the worker sweeps the floor, causing the dusty grey concrete surface to expand and occupy nearly the entire floor from the margins inward`. The final prompt must read as continuous photorealistic visual prose, completely free of math symbols and technical structured labels to prevent text overlays on the generated video.
6. **Passive environmental timelapse**: locked direction, varied observation per clip.
7. **Physical and continuity realism**: object transport (no teleporting without path), material behavior, lighting, unchanged evidence.
8. **Ambient audio**: `SFX:` + `Ambient noise:` matched to visible action.

**Threshold bridge** (if applicable): Follow the **Threshold Bridge Consistency Protocol (TBCP)** — see [references/threshold-bridge-consistency-protocol.md](references/threshold-bridge-consistency-protocol.md). The exterior→interior crossing is the only beat that flips lighting domain + camera family + anchor set at once, so it gets its own dedicated beat and must never be smuggled into a construction clip.
* **One Merged Beat (TBCP v4 Rule 1 — supersedes the old two-clip split)**: The crossing is exactly **ONE beat** producing **ONE VIDEO** and **ONE new IMAGE**: `IMAGE T (last exterior, door open, 2 interior anchors already visible through it) → VIDEO T (the ONLY visible clip — approach, sill crossing, door-frame wipe, exposure roll, settle, and for the pan variant one closing turn, as a single unbroken take) → IMAGE T+1 (interior settled)`. There is **no** separate Bridge-1/Bridge-2, **no** Sill Handoff IMAGE, and **no** hold/vestibule/turn beat — those belonged to protocol revisions v2/v3 and were retired on 2026-07-21 because they only reintroduced held "at the doorway" compositions. If you find two-clip bridge wording anywhere else in this package, it is stale; this block and `threshold-bridge-consistency-protocol.md` are the authority.
* **The crossing clip carries no work**: it is a pure camera move through an untouched ruin. Nothing is cleaned, cleared, repaired, or installed while the camera travels, and no tool, ladder, tarp, or stacked material appears in it. Do not call it a construction time-lapse — nothing is being built. The cleanout of that mess is the **next** beat.
* **Placement**: the crossing beat may never land earlier than Beat 3 — at least two ordinary exterior beats must precede it, or the sequence reads as "starting indoors".
* **Sealed Entry (TBCP Rule 2, v7 — replaces the PBISP peek)**: `IMAGE T` keeps its entry **CLOSED** — a shut door or hatch, or, on a carrier with no leaf built yet, unlit darkness filling the raw opening — and shows NOTHING of the interior: no peek, no interior landmark visible beyond it, no lit depth. This applies to every crossing variant, `[BRIDGE]` / `[BRIDGE TURN]` / `[CUT]` alike. The crossing clip pushes that entry open on camera and does the reveal itself. Rationale: a half-open doorway hands the video model a low-resolution, largely invented patch of interior that it then has to match while interpolating the crossing; when it cannot, the space the camera lands in reads as a different world, or the camera never fully gets inside. A shut entry gives it nothing to reconcile. **Anchor Inheritance**: the registered interior primary anchors are fixed at brief time and the crossing lands on exactly that set in `IMAGE T+1` — never a fresh set invented at the settle. **Anchor Qualification (mandatory)**: they must be features that plausibly already exist at crossing time — original structure, natural rock/wood formations, pre-existing wreckage, or items installed in an earlier on-camera beat. Future construction products (an uncarved staircase, unplaced furniture, uninstalled fixtures) are banned: the crossing always precedes interior construction, so using them forces objects to exist before the beat that creates them. **Scale declaration**: each anchor's frame-height scale is declared once, at `IMAGE T+1`, where it settles; `IMAGE T` declares no interior scale, because it shows no interior. Inside the clip they still grow continuously along the camera axis from the moment the entry opens.
* **Single-Variable Bridge Camera (TBCP Rule 3)**: Exterior and interior families share identical lens feel and identical camera height, so the bridge changes only forward translation (plus, in the pan variant only, one declared closing pan). Bridge Camera DNA: `same lens feel, same height, coaxial forward push-in; no tilt, no roll; horizon level at mid-frame until the crossing, then pitch locked level`.
* **Exposure & White-Balance Soft Roll (TBCP Rule 4, NLVTR-safe)**: Smooth the lighting change across the whole crossing and attribute it physically to door-shade ahead + doorway backlight behind — `the bright outdoor glare rolls off gently and the frame settles into the cooler dim interior, lit mainly by daylight spilling back through the doorway behind; gradual across the whole clip, never a sudden brightness snap`. No percentages or color-temperature numbers.
* **Door-Frame Wipe & Cross-Threshold Tether (TBCP Rules 5-6)**: At the sill the door-frame edges slide symmetrically outward like a vertical wipe, completing the exposure shift behind them; carry at least one material or light source unbroken across the sill (e.g. the same floor continues inside, or exterior daylight becomes interior backlight).
* **Dynamic Keyframe Projection (DKP) & Optical Flow**: The single crossing clip still projects its frame geometry across the whole arc. Reason internally with the Grid; **write it out as prose with no grid labels, no percentages, and no numeric ranges** (they fail the notation ban and get rendered as on-screen text). Target wording: `At the first frame the open doorway is centred in the middle of the frame. Midway through, the camera reaches the sill and the door-frame edges slide symmetrically outward past the left and right boundaries. By the final frame the door frame has fully exited and the interior room sits centred. One coaxial forward push; optical flow radiates symmetrically from the centre; the horizon line stays perfectly level at exactly half the frame height until the crossing, then the pitch stays locked level; the two pre-visualized landmarks scale up continuously without layout distortion.`

**Final reward**: coaxial handheld push-in reveal, single continuous take.
* **Reflective Mirror Alignment (RHMA-Blur)**: Include Mirror consistency clause (RHMA-Blur) to vertically align highly blurred, low-gloss diffused reflections of the background anchors on the foreground floor beneath them.
* **Dynamic Projection**: Define coaxial path vector, horizon lock, and forward optical flow.
* ASMR diegetic footsteps mandatory (name floor material + footstep texture).

### Step 9: Silent Self-Check & SCUP Quality Audit Report

1. Run the **SCUP P0 Kill Gates** below plus every contract registered in [references/contract-registry.json](references/contract-registry.json) before delivery. **Any P0 gate failure blocks delivery — it is not a score deduction to note and move past.** Do not proceed to Step 10 while a P0 gate is failing. (`references/continuity-contracts.md` exists, but it documents the separate Tier-4 `video_to_prompt_pipeline.py` reverse-engineering gate set, backed by `references/skill-local-contracts.json` — a different pipeline from this conversational composition flow; see "Where The Contracts Are Actually Enforced" above. For this Step 9 self-check, `contract-registry.json` plus the SCUP P0 Kill Gates below are the authority.)

**P0-Blocking Targeted Rewrite Loop**: when a P0 gate fires, do not regenerate the whole set — that destroys already-confirmed slots (most importantly a rendered/approved `IMAGE 1`). Instead:
   1. Identify exactly which slot(s) the failing gate's details point at.
   2. Rewrite only those slots, keeping every other slot byte-for-byte unless the fix legitimately requires a downstream slot to change too (e.g. a corrected Camera DNA Block must propagate to every same-family IMAGE that copies it character-for-character).
   3. Re-run the full P0 gate list (a local fix can accidentally break an adjacent gate).
   4. Allow at most **2** rewrite attempts per slot. If a P0 gate is still failing after 2 targeted rewrites, stop — do not deliver, and tell the user plainly which gate keeps failing and why, so they can adjust the input (this is the one legitimate `needs_human_review` escalation path for the chat-composition flow, mirroring the standalone `video_to_prompt_pipeline.py` CLI's exit-1 behavior for the same condition).

**SCUP P0 Kill Gates** — targeted rewrite (see loop above) if any fires:
- Structure errors (count, slot type, mixed protocol, shot family)
- Camera DNA Block not copied literally across same-family IMAGEs
- Any `IMAGE` contains active workers or machines (violates Clean Frame Boundary).
- Any construction video featuring workers does not begin at t=0s with the worker already at the work face making effective tool contact, or contains worker entrance/exit choreography.
- Any video featuring loose/fluid materials fails to encapsulate them in rigid containers (violates Rigid Container Encapsulation RCE).
- **Volume Conservation Gate (P0)**: container capacity, trip count, or spoil-pile growth must plausibly account for the volume removed or delivered in the beat. Clearing a room-scale debris field or cutting a passable opening into two hand crates fails; any cut-out solid piece that never receives an on-camera carry-out fails. A correctly scaled, visibly growing spoil pile satisfies encapsulation for material that is not transported out of frame.
- Vague landmark or boundary locations that skip natural-language position + depth layer + Z-depth scale (internally tracked via `Grid A1-C3`, but the delivered prompt must state position/depth/scale in natural language — literal `Grid` tokens in the delivered text instead fail the No Banned Notations Gate below), or that fail to lock secondary drift-prone objects relatively using RPL.
- Any occluded landmark or ledger object is omitted instead of being maintained via the `Ghost Clause`.
- Missing coordinate-level dynamic keyframe projection (DKP) for threshold bridge or final reward push-in.
- Volumetric materials lack a described fill-level change (e.g. empty-to-heaped, full-to-empty, in natural language — never a `%` figure, which the No Banned Notations Gate below fails) and a physical transport vector (VMFP).
- Final polished floor lacks a Mirror Consistency Clause (RHMA-Blur) vertically aligning Grid C reflections with Grid A physical objects.
- Active workers in video have complex facial/clothing descriptions instead of Hero Agent Lock (HAL) silhouettes.
- Threshold `IMAGE T` shows its entry open or the interior visible through it — it must be shut/opaque (Sealed Entry), with the reveal happening inside the crossing clip.
- Continuous action failure: Any video slot lacks a verb-first active continuous action flow or uses transition shortcut words (`cross-dissolve`, `fade-in`, `suddenly`, `magically`, `rapid montage`, `jump cut`, `instant transformation`).
- Milestone-package incoherence / insufficient stage delta: Adjacent IMAGE anchors combine unrelated phases or zones in one beat (must be split); one coherent same-zone package of up to three actions is allowed when it produces a single named terminal stage. Full declared coverage/count is REQUIRED — token patches or barely visible local edits fail.
- **Causal Trace Gate (GCTR - P0)**: Any new object, repaired surface, cleaned zone, assembled structure, installed fixture, moved material, or machine-assisted change appears without at least two visible trace markers: contact mark, seam, fastener, residue, drag path, compression mark, dust edge, tool scar, weld bead, cut line, adhesive squeeze-out, tire/track print, cable rub, scaffold footprint, clamp mark, bracket shadow, or alignment guide.
- **Keyframe Collage Auto-Generation Gate (关键帧多宫格拼图自动生成门 - T0)**: During keyframe extraction, a 5-column tiled keyframe collage must be automatically generated using FFmpeg's `tile` filter and saved in the video directory (named `<video_name>_collage.jpg`). If this file is missing or fails to generate, the audit fails immediately with a critical T0 status.
- **Video Analysis Frame Count Gate (视频分析帧数门 - P0)**: The number of keyframes sent to the Multimodal LLM (Gemini/OpenAI) must satisfy Adaptive Dense Analysis Rate: all extracted frames for clips with 90 frames or fewer; for longer clips, at least 40% of extracted frames, never fewer than one frame per second, with mandatory start / peak / end coverage for all CV change segments.
- **Change Event Coverage Gate (变化事件覆盖门 - P0)**: Every CV `change_event` must appear in `time_sequence.source_event_ids`, must be traceable to `source_frame_range`, and must be referenced in the matching VIDEO prompt. Missing event coverage fails the audit even if prompt wording looks polished.
- **Analysis Peak Inclusion Gate (峰值帧送审门 - P0)**: For every detected change event, the event start frame, maximum-delta peak frame, and event end frame must be included in the semantic analysis frame set.
- **No Banned Notations Gate (NLVTR Gate - P0)**: The final image/video prompts must *never* contain mathematical percentage symbols (`%`), raw numerical ranges inside the visual description (e.g. `10% to 90%`, `40cm to 0cm`), colons inside variable descriptions, or dry structured acronyms (`TSPA`, `HAL`, `VMFP`, `GCTR`, `RPL`, `RCE`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`). All visual progress and persistent traces must be fully described in fluid, continuous natural language prose (e.g. "dusty floor area shrinks as clean wood surface grows to cover the floor"). Any presence of math characters, colons in visual slots, technical SCUP acronyms, or grid cell coordinates (e.g. `Grid C1`, `Grid B2`) in final image/video prompts fails this gate.
- **Beat Overload Pop Prevention Gate (P0)**: A beat may combine up to three detected operations when they share one phase family and one zone (this is the Visible Milestone Package Rule's allowance, e.g. ceiling paneling + wall insulation as one envelope-closeout package). It fails when detected operations span more than one phase family in the same beat (e.g. demolition + finish, or a rough-in system run in the same beat as the panel that conceals it — cross-phase bundles, never allowed regardless of packaging) or when more than three operations stack even within one family.
- **Sub-Pixel Coordinate Pinning Gate (SPCP Gate - P0)**: Verify that every prompt (IMAGE and VIDEO) pins the camera attitude with wording matching its shot family: level exteriors pin the horizon line at a stated height; elevated/tilted shots pin the declared pitch angle and vertical convergence (no horizon reference); enclosed interiors pin a level pitch and centered vanishing axis. Mentioning a horizon, sky, or drifting clouds inside an enclosed interior prompt fails this gate; optical-flow radiation phrases inside static tripod prompts also fail.
- **Geometric Tool Lock Gate (MTAL Gate - P0)**: Verify that all non-sterile active videos explicitly define the manual tool (MTAL) with specific color, geometric shape, and material properties (e.g., `matte-black rectangular steel shovel head` or `solid-blue heavy-duty paint roller`), rather than vague terms, to block morphing/flicker.
- **Temporal Physics Skeleton Gate (P0)**: Verify that every `time_sequence` beat declares `shot_family`, `beat_type`, `single_physical_operation`, and a complete `causal_path` with material source, entry path, tool contact, movement path, at least two persistent traces, and next-frame inheritance.
- **Threshold Bridge Continuity Gate (P0 — TBCP v4)**: Any exterior→interior crossing must follow the Threshold Bridge Consistency Protocol. It must be **exactly one beat = one VIDEO clip + one new IMAGE** (`IMAGE T → VIDEO T → IMAGE T+1`); splitting it into Bridge-1/Bridge-2 with a Sill Handoff IMAGE is the retired v2/v3 shape and **fails this gate**. The crossing clip must be one unbroken take carrying no construction work, correctly meta-tagged (`[BRIDGE]` / `[BRIDGE TURN]` / `[CUT]`), and placed no earlier than Beat 3. `IMAGE T` must keep its entry shut and opaque with nothing of the interior visible (Sealed Entry, all variants), and the crossing clip must open it on camera; the crossing must land on exactly the registered interior primary anchors in `IMAGE T+1` (Anchor Inheritance); the bridge camera must lock identical lens + height across exterior and interior families and translate forward only (with at most one declared closing pan in the pan variant, no tilt/roll); the lighting change must be a gradual exposure/white-balance roll attributed to door-shade + doorway backlight with no brightness snap; the door-frame edges must slide symmetrically outward as a wipe; and at least one material or light source must continue unbroken across the sill. Interior anchors must be plausibly pre-existing features at crossing time (original structure, natural formations, or items installed in an earlier on-camera beat — never future construction products such as uncarved stairs or unplaced furniture), and they carry their settled frame-height scales in `IMAGE T+1`. There is no peek and no cross-frame scale ladder to declare in any variant — that requirement was retired in v7.
- **Anchor Review (P0 — staged execution)**: When operating in Staged Execution Mode (Step 6.5), **the first frame of every shot family** must be rendered and shown to the user before any other `IMAGE`/`VIDEO` of that family is composed or rendered — `IMAGE 1` always, plus `IMAGE T+1` in Threshold mode and the cut frame under a declared `[CUT]`. The server judges nothing, so you must check each yourself and say what you see. `IMAGE 1`: Clean Frame Boundary, Camera DNA plausibility, Primary Landmark presence, genuine construction-grade damage, Genre DNA tone match, no text artifacts. `IMAGE T+1`: the four-item checklist in Step 6.5b (roof form/pitch vs exterior, apertures vs ledger, clear width/height in door units, door frame out of frame + dirt). A frame you believe is wrong must be corrected and re-rendered with `--force_regenerate` after deleting the rejected file, never silently accepted. Skipping the second stop is the same P0 violation as skipping the first.
- **Shell Envelope Consistency Gate (P0)**: fails when any interior `IMAGE` (a) omits the Geometry Lock's envelope signature — the verbatim clear-width/clear-height clause in door units — or its `roof_form` clause; (b) contains any element on the `aperture_denylist`, or any opening absent from the `aperture_ledger`; or (c) states a roof form or pitch that contradicts the exterior beats. Restating only on `IMAGE T+1` and relying on inheritance for the rest of the interior chain fails this gate: every frame is generated image-to-image, so an omitted lock is re-derived, not inherited. Enforced server-side by `prompt_pipeline:check_shell_envelope_consistency`; in chat composition it is yours to run.
- **Material Palette Lock Gate (P0)**: fails when any `IMAGE` (a) omits a registered material's `substrate` phrase while that material is visible in frame; (b) re-words a `substrate` phrase instead of copying it verbatim, including swapping in a lighting-derived adjective (`warm honey stone` for `coarse grey-brown fieldstone`); (c) advances a material's `state_track` in a beat that does no work on that material, or by more than one step; or (d) moves a `state_track` phrase backward. Applies to **both** shot families — exterior frames drift identically, they just hide it better. No server-side enforcer exists (registered as a gap under `material-palette-lock`); this gate is yours to run in every mode.
- **Rendered Text Artifact Gate (P0)**: Post-render video QA fails if any extracted frame contains visible numeric overlays, percentage glyphs, caption-like text, or model-rendered prompt notation.
- **Hard Transition Peak Gate (P0)**: Post-render video QA fails when 3fps frame-difference spikes indicate scene replacement or hard cuts that were not declared as threshold bridge motion.
- **Direct Start Gate (P0)**: Post-render video QA fails when a construction clip wastes time on worker arrival/departure or does not begin effective work at zero seconds.
- **Landmark Drift Gate (P0)**: Post-render video QA fails when primary landmarks, horizon line, or vanishing direction drift beyond the locked shot family.
- **Object Birth Without Path Gate (P0)**: Post-render video QA fails when fixtures, solar panels, furniture, walls, railings, lights, or tools appear without a visible source and movement path in the prior frames.
- **State Regression Gate (P0)**: Post-render video QA fails when a completed state reverts to an earlier construction state without a declared removal or rollback beat (a declared temporary-works-strike beat is a legal removal, not a regression). Specifically across the threshold crossing (`IMAGE T → IMAGE T+1`), any structural or envelope element completed in exterior beats (roof framing, ceiling sheathing/membrane, entrance threshold, pre-cleared floor) MUST be 100% inherited in `IMAGE T+1`. Mentioning ceiling cracks/holes/leaks or reverted floor debris after exterior repairs is an immediate P0 failure.
- **Full-Field Delta Conservation Gate (P0)**: Any VIDEO prompt interpolating between IMAGE N and IMAGE N+1 that fails to provide active worker choreography, explicit geometric tools, material/spoil balance, and physical transitions for ALL visible zone deltas ($\Delta = \text{IMAGE}_{N+1} - \text{IMAGE}_N$) across top (roof/ceiling), middle (walls/openings), bottom (floor/approach), and peripherals (spoil/materials) fails. No structural element (e.g. roof debris, wall cracks, floor litter) may appear, disappear, or morph without an active on-camera physical process and corresponding SFX.
- **Construction Sequence Violation Gate (P0)**: Any beat order that violates a hard veto (wiring after enclosure, finish coat before primer, roof before structure, floor finishing before overhead/wet work) fails before prompt rendering.
- **Phrasing Repetition Gate (P0)**: Before finalizing IMAGE N+1 or VIDEO N, compare its sentence structure, opening clauses, and verb choices against the immediately preceding IMAGE/VIDEO of the same type. Reusing the fixed required openers (Camera DNA block, "Use the provided first frame..." anchor sentence) is correct and mandatory — but reusing the *same subsequent sentence template, clause order, or verb set* beat after beat fails this gate. Deliberately vary sentence rhythm, subject phrasing, and verb selection every beat while keeping every required structural element and locked anchor.
- **Word Count Self-Check Gate (P0)**: Before finalizing each IMAGE or VIDEO, count its words against the hard validator limit — **IMAGE 180 words (220 for post-crossing interior frames), VIDEO 380 words** (base profile), the single authority defined in Step 7's Word count budget block. No exception for complex/reference mode or drift-sensitive space work. If over budget, trim redundant adjectives, filler phrases, and restated boilerplate first — never by deleting required structural elements (Camera DNA, direct-at-zero worker clause, pacing control phrase, audio clause, Ghost Clause, Mirror Consistency Clause).

- **Cumulative State Gate (P0)**: Any IMAGE anchor that drops a permanent change or trace from an earlier beat without an explicit covering operation in a named beat fails. Any adjacent anchor pair that differs by more than the declared milestone package's result fails.
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
   - **归一化九宫格锁定与相对位置锁 (NGCS Grid & RPL)**: Landmarks are tracked internally via absolute Grid A1-C3 coordinates but delivered in natural language (position + depth layer + height ratio) — literal `Grid` tokens must never reach the final prompt text; secondary objects are grouped and locked relative to the nearest Primary Landmark using RPL to prevent coordinate dilution and cross-contamination in the T5 encoder.
   - **动态关键帧投影 (DKP)**: Push-in shots contain dynamic keyframe projection coordinates at t=0s, 4s, and 8s with optical flow direction constraints.
   - **隐性状态持久化 (OSPL)**: Occluded landmarks and ledger objects are maintained via the Ghost Clause instead of being omitted.
   - **质量与物流守恒 (VMFP & RCE)**: Bulk construction materials have quantified volume-change descriptions; all loose or fluid materials are encapsulated inside rigid containers during transport to prevent uncontrolled dissolving or flickering.
   - **镜像反射对齐 (RHMA-Blur)**: Reflective floor surfaces default to heavy-matte, high-blur diffused reflection (RHMA-Blur) to prevent high-frequency reflection flickering.
   - **无幽灵首尾过渡 (Clean Frame)**: All static IMAGE anchors contain zero active workers or machines.
   - **零秒直接开工锁**: Every construction VIDEO featuring workers places them at the active work face at t=0s with the first effective action starting immediately, and contains no entry/exit choreography.
   - **单兵轮廓锁定 (HAL)**: Workers in VIDEO prompts are locked as solid-color safety-vest silhouettes (Hero Agent Lock) to prevent identity morphing.
   - **具名人物一致性锁 (Named Cast Lock — only when `agent_lock_mode: named_cast`)**: every person-bearing prompt restates the registered short identity block verbatim exactly once ahead of the first action verb, uses no cross-segment coreference (`the same worker`, `he returns`), carries the three always-on negatives, and differentiates any second person. Report the exit code of `scripts/check_character_lock.py` in this row rather than asserting the lock held — it is a string-equality property, so claiming it by eye is worthless.
   - **全局因果痕迹锁 (GCTR)**: Every addition, removal, repair, cleaning, installation, assembly, transport, or machine-assisted change leaves at least two visible contact traces in IMAGE N+1, proving the change was physically caused.
   - **外壳体量锁 (Shell Envelope Consistency - P0)**: The Geometry Lock is stated as relative measures a model can draw to — clear width in door widths, clear height in door heights, depth in countable bays, one roof form at the exterior's own pitch, an exhaustive aperture ledger, an explicit aperture denylist, and the same wall material inside and out. Every interior IMAGE restates the envelope signature verbatim; no frame grows a skylight, vault, dome, or rear-wall arched window that no beat ever cut.
   - **材质调色板锁 (Material Palette Lock - P0)**: 3-5 registered materials, each split into an immutable `substrate` phrase (base hue, texture, finish) and a monotonic `state_track` (trauma → restored). Every frame copies the `substrate` character-for-character and carries the current state phrase; a material this beat did not touch keeps its previous state phrase. No stone wall reads moss-green in one frame and dry ochre in the next because its adjectives were re-invented.
   - **首帧复核 (Anchor Review — staged execution only)**: When a renderer is driving the skill, the actual rendered first frame of **every shot family** — `IMAGE 1`, and `IMAGE T+1` in Threshold mode — not just its text prompt, is put in front of you and the user before any other beat of that family is composed or rendered. Nothing judges it automatically, so you check it against Clean Frame Boundary, Camera DNA plausibility, Primary Landmark presence, genuine construction-grade damage, Genre DNA tone match, and no text artifacts, and say what you see; the packet is then reconciled against that render (Packet Reality Reconciliation) so downstream beats describe confirmed reality, not the pre-visualized spec.
   - **过门前封门机制 (Sealed Entry — v7, 取代旧的盲区预描 PBISP)**: The static IMAGE preceding a threshold crossing keeps its entry shut and opaque (or, on a carrier with no leaf yet, its raw opening unlit) and shows nothing of the interior; the crossing clip opens it on camera and reveals the interior itself. The registered interior landmarks must still be plausibly pre-existing features at crossing time (original structure, natural formations, or previously installed items — never future construction products), and they carry their settled frame-height scales in the interior frame.
   - **外进内门槛桥协议 (TBCP v4 - P0)**: Any exterior→interior crossing is exactly ONE beat — one meta-tagged VIDEO clip (`[BRIDGE]` / `[BRIDGE TURN]` / `[CUT]`) between `IMAGE T` and `IMAGE T+1`, never split into Bridge-1/Bridge-2 with a sill-handoff frame; the crossing clip is one unbroken take carrying no construction work and lands no earlier than Beat 3; `IMAGE T` keeps its entry shut and opaque with no interior visible and the clip opens it on camera (Sealed Entry, all variants); the crossing lands on the registered interior primary anchors (Anchor Inheritance); the bridge camera must lock identical lens + height and translates forward only (at most one declared closing pan in the pan variant); the lighting change is a gradual exposure/white-balance roll attributed to door-shade and doorway backlight (no snap); the door-frame edges wipe symmetrically outward; at least one material or light source continues unbroken across the sill; and the interior anchors qualify as pre-existing features carrying their settled frame-height scales in `IMAGE T+1`.
   - **关键帧多宫格拼图及分析密度锁定 (Keyframe Collage & Adaptive Dense Analysis Lock - T0/P0)**: When reverse-engineering or analyzing video, a 5-column tiled keyframe collage must be auto-generated via FFmpeg and saved alongside the source file as `<video_name>_collage.jpg` (T0 priority); for clips with 90 or fewer extracted frames, all frames are sent for semantic analysis; for longer clips, at least 40% of extracted frames — never fewer than one per second — with mandatory start, peak, and end frames for every change segment plus adjacent before/after frames around each peak (P0).
   - **变化事件覆盖锁 (Change Event Coverage - P0)**: CV scanning must output `change_events`; every change event must be bound to `time_sequence.source_event_ids` and referenced in the matching VIDEO prompt, ensuring no brief but critical process detail from the source video is missed.
   - **无文字伪影规则锁定 (NLVTR Lock - P0)**: The final IMAGE and VIDEO prompts must contain no mathematical percentage symbols (`%`), numeric ratio ranges, or technical acronyms (`TSPA`, `HAL`, `VMFP`, `GCTR`, `RPL`, `RCE`, `SCUP`, `NGCS`, `OSPL`, `RHMA`, `PBISP`, `HCL`, `NLVTR`, `MTAL`) to prevent text-overlay artifacts from being rendered on the generated video.
   - **单里程碑施工包锁 (Visible Milestone Package Lock - P0)**: Each beat produces one named terminal stage at its FULL declared extent/count, using one operation or at most three tightly related same-zone actions. Both primary-product and secondary-material progress remain continuous; token patches and unrelated cross-phase bundles fail.
   - **亚像素相机视场固定锁 (Sub-Pixel Coordinate Pinning Lock - P0)**: Every prompt pins camera attitude by shot family — level exteriors declare the horizon line height (e.g., `horizon line remains perfectly level at exactly half the frame height`); elevated/tilted shots declare the locked pitch angle and vertical convergence; enclosed interiors declare a level pitch and centered vanishing axis, never a horizon or sky — eliminating sub-pixel camera drift without forcing impossible geometry.
   - **几何工具及单兵动作锁 (Geometric Tool & Active Motion Lock - P0)**: Every hand tool must be described with specific color, material, and geometric head shape (e.g., `matte-black rectangular steel shovel head`) to prevent the model from merging the tool with the background or randomly morphing it.
   - **施工顺序验证锁 (Construction Sequence Validation - P0)**: The beat ladder follows real construction dependencies (demolition → structural repair → rough-in systems → ceiling panels → wall panels → surface coating → floor finishing → fixtures → furniture); no hard vetoes violated (e.g., no wiring after panel closure, no finish coat before primer, no light activation without an earlier wiring beat).
   - **供电链完整性锁 (Power Chain - P0)**: Any practical light activation is preceded by an on-camera wiring/rough-in beat run before panel closure, plus a visible power source (solar panel, battery bank, generator) for off-grid carriers; no lamp ever lights without an installed fixture and a traceable power path.
   - **封闭空间成因锁 (Enclosed-Space Provenance - P0)**: Every interior chamber behind a newly opened shell is either declared pre-existing (natural cavity, original room) at the opening moment, or earns its own excavation and mucking-out beats before any finishing work; interior volume plausibly fits the exterior shell.
   - **体积守恒锁 (Volume Conservation - P0)**: Removed or delivered material volume is plausibly accounted for by container scale, trip count, or a visibly growing spoil pile; cut-out solid pieces receive explicit pry-out and carry-out actions instead of vanishing.
   - **驻场设施锁 (Persistent Site Plant - P1)**: Scaffolding, formwork, shoring, and cribbing persist across anchors between a named erection beat and a named temporary-works-strike beat instead of blinking in and out per clip; fresh unset concrete is always shown supported by its formwork.
   - **累计状态精确性与全域差量守恒锁 (Cumulative State & Full-Field Delta Conservation Lock - P0)**: Each IMAGE inherits all permanent traces from every prior beat; the delta between IMAGE N and IMAGE N+1 equals exactly the result of VIDEO N's declared operations across all 4 spatial zones (Top, Middle, Bottom, Peripherals) with 100% action-tool-SFX mapping — no phantom state disappearance, no unacted structural changes, no side progress.
   - **进度流控制锁 (Progress Flow Control - P0)**: A single VIDEO completes one unmistakable named milestone across its full declared region/count, shows the first instance of every included action plus repeated cycles, carries both primary-product and secondary-material progress lines, and keeps construction state monotonic.
   - **VIDEO 空间完成度描述锁 (Spatial Completion Extent - P1)**: Every VIDEO prompt includes concrete spatial start-and-end extent descriptions matching the adjacent IMAGE anchors (e.g., "floor panel advances from the left third of the foreground to cover the entire foreground band"); progressive `-ing` verb forms in mid-clip; completion language reserved for the final moment of the clip only.
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
- **Bridge tag (required for threshold crossings)**: the single Threshold Bridge Consistency Protocol crossing clip (see Step 8's Threshold bridge section) MUST be labeled with a bracketed meta tag instead of a plain `视频 N:` label. Pick exactly one:

  | Tag | When |
  |---|---|
  | `视频 N [BRIDGE]:` | coaxial crossing — the camera pushes straight through and settles facing the same way |
  | `视频 N [BRIDGE TURN]:` | pan variant — the same push-through, ending in one stationary pan onto the interior's long axis |
  | `视频 N [CUT]:` | declared cut-in — the interior frame is re-established from scratch rather than composed to match the exterior frame. (The shut-entry start frame is not what distinguishes it: since v7 every variant starts from a closed entry.) |

  The renderer's slot parser (`prompt_pipeline._parse_prompt_slots`) and its pairwise continuity judge read this exact bracketed annotation to decide whether an exterior-to-interior camera jump is an intentional threshold crossing or an error. Without it, a legitimate crossing gets auto-rejected as a continuity failure and burns retry budget on a frame that was correct. Tag exactly **one** VIDEO slot per crossing — there is only one crossing clip under TBCP v4. Do not tag ordinary construction VIDEO slots. IMAGE slot labels never take this tag (only VIDEO slots do).
- Never collapse labels onto the same line as prompt body.
- One blank line between each slot.
- No Markdown headings, bullets, tables, or wrapped prose inside the fenced block.

2. Immediately below the fenced `text` block, append the structured **Quality Audit & Verification Report** (提示词质量审核报告) in a Markdown table. Do not put the report inside the fenced `text` block.

### Step 11: Direct Text Delivery

After Step 10, stop. The final answer must contain the copy-ready fenced `text` block, followed immediately by the structured Quality Audit & Verification Report in chat. Keep any surrounding prose minimal.

### Step 12: Auto-Save to Idea Library

After delivering the prompts and audit report to the user, save the result to the creative-idea-generator idea library running at `http://127.0.0.1:8085`. **Default behavior: do this immediately and silently**, without asking for confirmation and without mentioning it unless an error occurs — this stays the default because most users want their generated sets searchable in the library without extra steps. **Opt-out**: if the user has said anything in this conversation to the effect of not wanting auto-save (e.g. `不用保存`, `别自动入库`, `不要存到库里`), skip this step entirely for the rest of the conversation and do not re-offer it unprompted. Saving POSTs the full prompt text and audit report to the local library server at `127.0.0.1` — nothing leaves the machine the skill is running on, but it is still content leaving this conversation's turn, which is why an explicit opt-out must be honored.

**Execution**:

Run the helper script at `<SKILL_DIR>/scripts/save_to_library.py` via whatever command-execution tool your environment provides, with the following arguments, substituting the real values from the just-generated output:

```
python "<SKILL_DIR>/scripts/save_to_library.py" \
  --title       "<the Chinese topic title, e.g. 做一个废弃阁楼翻新>" \
  --prompt_block "<the complete raw text inside the fenced text block, with newlines preserved>" \
  --audit_md    "<the Quality Audit table markdown>" \
  --creativity  "gemini-veo-restoration-composer" \
  --server      "http://127.0.0.1:8085"
```

**Rules**:
- `--title`: Use the exact Chinese topic/theme string parsed in Step 1 (the user's original input or the selected Tier-0 seed). Do NOT invent a new title, and do NOT normalize, re-punctuate, or tidy it between steps — the title is the join key for all three helper scripts. (The scripts now also carry a stable `slug = sha1(canonical(title))[:12]` and match on it first, so a stray space or a full-width colon no longer breaks the chain — but a *genuinely different* title still does.)
- `--prompt_block`: Pass the **raw text content** of the fenced block (not the fences themselves). Preserve all newlines.
- `--audit_md`: Pass the raw Markdown table from the Quality Audit report.
- `--creativity`: Always `"gemini-veo-restoration-composer"` for this skill.
- Exit codes (shared vocabulary across all three scripts — see `scripts/skill_common.py`):

  | Code | Meaning | What to do |
  |---|---|---|
  | 0 | saved | print `✅ 已自动入库：<title>` |
  | 2 | 服务不可达 | tell the user `⚠️ 点子库服务未运行，提示词已生成但未能自动入库。请先启动 creative-idea-generator 服务（run.bat），再手动触发保存。` then stop — no retry loop |
  | 3 | 服务返回错误 | show the stderr line to the user briefly |
  | 4 | 输入有误 | fix the arguments and re-run |
  | 5 | 超时（服务仍在处理） | wait or ask the user; do **not** treat as unreachable |
  | 1 | 其他运行期失败 | show stderr, stop |

- **If this step fails, Step 13 has a hard dependency on it.** `generate_frames.py` reads the
  prompt block *out of the library*, so a failed save makes Step 13 exit 6 ("title not found",
  terminal). Do not just stop: write the prompt block to a local file and pass it to Step 13
  with `--prompt_file`, which is exactly the降级 path that flag exists for.

### Step 13: Auto-Trigger / Prompt Image Generation (连贯画幅帧序列作图)

In the default staged flow, `IMAGE 1` has *already* been rendered and looked at by this point (via `render_and_gate_anchor.py`, called mid-turn before the rest of the prompt set was even composed) — this step now only needs to trigger rendering of the *remainder* (`IMAGE 2-(N+1)` and `VIDEO 1-N`). If staging was waived (user asked for text only, or the render server was unreachable), determine now whether the user wants images generated after all (e.g. explicitly asked for "作图", "生成图片", "渲染", "预览", or the server has come back up).

**Execution Logic**:
1. **Auto-Trigger**: If rendering was already decided (Step 6.5) or the user explicitly requested image generation, immediately and silently trigger the remaining sequence generation.
2. **Interactive Offer**: If neither applies, append the following text to the end of your response to offer it:
   `💡 如需直接生成该套提示词的连贯效果图，您可以回复“开始作图”或“生成预览”。`
   If the user subsequently replies with "开始作图", "生成预览" or any request to generate images, trigger it.

**How to Trigger**:
Run the helper script at `<SKILL_DIR>/scripts/generate_frames.py` via whatever command-execution tool your environment provides:

```
python "<SKILL_DIR>/scripts/generate_frames.py" \
  --title         "<the Chinese topic title, e.g. 做一个废弃阁楼翻新>" \
  --aspect_ratio  "9:16" \
  --quality       "2K" \
  --server        "http://127.0.0.1:8085"
```

This script posts to `/api/render_staged`. Frames already on disk are reused, so in the normal case (per Step 6.5) it renders only the remainder; in the deferred-decision case it renders `IMAGE 1` too. Rendering runs no consistency review of any kind — frames land as `pending_manual_review` and the user can run the frame grid's 「🔍 一致性审查」 afterward if they want one.

**Rules**:
- Use the exact same Chinese topic title used in Step 12 (and, if Step 6.5 ran, the same title passed to `render_and_gate_anchor.py`). The three scripts are chained by that title; matching is now slug-first so incidental whitespace/punctuation differences survive, but a rewritten title still misses.
- `--aspect_ratio` must match the aspect the packet was written for (default `9:16`, per the Frame Aspect Lock in Step 6). Passing a different value renders a frame whose composition the anchors were never designed for.
- Keep the terminal command running until completion so the user can see real-time updates: which frame is generating and any upstream retries.
- On success, present a brief confirmation: `✅ 帧序列已生成完毕，存放在项目目录：outputs/<safe_project_name>/`
- If the stream reports `needs_human_review`, relay that to the user plainly instead of claiming success — this is the one legitimate escalation path in an otherwise fully autonomous flow.

**Exit codes** (shared vocabulary — see `scripts/skill_common.py`). Read this table before
reacting to a non-zero exit; the same number used to mean opposite things in Step 6.5 and here:

| Code | Meaning | What to do |
|---|---|---|
| 0 | 帧序列跑完 | report the project directory |
| 2 | 渲染服务不可达 | say `⚠️ 渲染服务未运行`, stop, no retry loop |
| 3 | 服务返回错误（HTTP 4xx/5xx 或 status≠ok） | relay the stderr message; this is a real server fault, not a connectivity problem |
| 4 | `--prompt_file` 读不了 | fix the path |
| 5 | 请求超时，**服务仍在工作** | wait or ask; do **not** re-submit blindly and do **not** treat as unreachable |
| 6 | **点子库里没有这个标题 —— 终局性错误** | retrying will never help. Either re-run Step 12, or re-invoke with `--prompt_file <本地文件>` |
| 1 | 进度流中断/生成过程报错 | the server task may still be running in the background — check `outputs/` or the frame grid before re-running, or you will pay for the same frames twice |

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

> **Availability (verified 2026-08-08): neither sister skill is installed.** `restoration-timelapse-engine` and `tiktok-abandoned-rebirth` are **not** present in any skill directory on this machine. This whole section describes contract compatibility, not a live handoff — do not attempt to invoke either skill, and never tell the user to "use X directly", because there is no X to use.
>
> What still works without them, and what does not:
> - **Tier 2 is fine.** It parses a `tiktok-abandoned-rebirth` topic brief as *pasted text*. The brief is an input format, not a skill call — a user who has one from elsewhere can still use Tier 2 exactly as documented.
> - **The engine contract list below is fine.** Those contracts are internalized in this file (Steps 6-9) and in [`references/continuity-contracts.md`](references/continuity-contracts.md). Conformance is self-contained.
> - **Delegation is not fine.** The two "→ use X" redirects in the *When NOT to Use This Skill* section above cannot be followed. If a user wants a topic brief only, or wants to edit individual slots of an existing set, do it here rather than pointing them at an absent skill.
> - **The ledger handoff is not automatic.** "After prompt generation, update `references/used-topic-ledger.md`" is an instruction to *you*, not a thing the upstream skill does.
>
> If either skill is installed later, delete this block rather than editing around it.

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
  Threshold Bridge Consistency Protocol (TBCP v4) for exterior→interior crossings: **one merged crossing beat** (one meta-tagged VIDEO between `IMAGE T` and `IMAGE T+1` — never a two-clip split with a sill-handoff frame), anchor inheritance, single-variable bridge camera, exposure/white-balance soft roll, door-frame wipe, cross-threshold tether, untouched-trauma first interior reveal. Load during Steps 6-8 whenever Mode = Threshold.

- [references/prompt-templates.md](references/prompt-templates.md)
  Canonical IMAGE and VIDEO templates with fill-in slots. Load during Steps 7-8.

- [references/beat-ladders/cable-car.md](references/beat-ladders/cable-car.md)
  The 19-beat cliff cable car / gondola restoration ladder. Load during Step 5.5 only when the topic matches this specific family; it's a named exception to the default 3-6 beat count, not a general pattern.

- [references/continuity-contracts.md](references/continuity-contracts.md)
  Rendered gate-by-gate view of [references/skill-local-contracts.json](references/skill-local-contracts.json), the machine-checked registry of every P0/P1/P2 rule the separate Tier-4 `video_to_prompt_pipeline.py` reverse-engineering CLI enforces via its own `run_scup_audit()`. This is **not** the conversational Step 9 self-check's gate list — that authority is `references/contract-registry.json` (see Step 9 above). Load this file only when running the reverse-engineering CLI's own audit path; maintain `skill-local-contracts.json`, not this file, when one of its rules changes.


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
