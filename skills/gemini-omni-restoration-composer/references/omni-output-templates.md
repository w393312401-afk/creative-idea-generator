# Omni Output Templates

Use these patterns as compact guidance, not paragraphs to copy wholesale. Prompt bodies are
English natural prose; Chinese slot labels and audit evidence stay outside those bodies.

## Length budgets and actual counting

| Slot / actual structure | Working target | Hard ceiling |
|---|---|---|
| IMAGE — exterior or single camera family | 140–180 words | 180 |
| IMAGE — post-crossing interior, including partial interior anchors | 170–220 words | 220 |
| VIDEO — three shots, including reward, full crossing and atomic entry stages | about 340 words | 400 |
| VIDEO — four construction shots | about 395 words | 455 |
| VIDEO — explicit user one-take override | concise complete action | 400 |
| Conversational edit | 30–60 words | 90 |

These ceilings include the **entire slot body**: anchor binding, camera/action prose,
identity/geometry locks, pacing, no-text and audio clauses. Counts are whitespace-separated
words, matching the local evaluation. Lower targets are drafting guidance, not failure gates.
A ten-second reward or entry stage still has three shots and a 400-word ceiling; do not
apply the 455-word construction allowance just because its duration is ten seconds.

Before delivery, save the final fenced pack to a temporary job file and run:

```text
python3 scripts/lint_prompt_pack.py <pack.txt> --clip-seconds 10 --required-edits 2 --expected-videos 5 --post-crossing-images 4,5,6 --three-shot-videos 1,2,5
```

The path is relative to this skill folder; use its absolute path if working elsewhere.
Declare flags from the user's request and the planned roles, never from an output's own audit.
`--post-crossing-images` lists the anchors entitled to 220 words; other images get 180.
`--three-shot-videos` lists dedicated reward/entry clips in a long-duration pack; 4s/6s
construction already defaults to three shots. Add `--single-take` only for an explicit user
override, which uses 400 words. `--required-edits 0` is the default when none were requested.

Read the returned `counts` and `errors`. Fix missing slots/edits, compress overlong bodies
and rerun on the **final text**. A count is not a physical or semantic review. Do not claim
the whole pack passed because `measurable_pass` is true. When tool execution is unavailable,
provide reproducible counts if you can obtain them; otherwise mark word counts `待核验`.

Compress in this order: duplicate qualifiers/capture artifacts, repeated general boilerplate,
then secondary description outside the operation. Keep short immutable material/geometry
clauses. Do not trim a requested action, causal path, source/destination, first/last binding,
applicable shot/cut/return structure or trace inheritance to fit. If a complete action really
will not fit, split its work into adjacent quantified beats rather than omit a deliverable.

## Notation and the registered IMAGE Grid exception

Write counts, dimensions and frame-height shares as English words (`three roof beams`,
`twenty-four millimetres`, `forty-five percent of frame height`). Do not put percent symbols,
count digits, metric digits, timecodes, numeric weights, internal acronyms or value fields
into prompt prose. `IMAGE 1` anchor references and numbered Chinese slot labels are allowed.
Use positional prose for chat-created anchors.

One registered runtime compromise remains: when an application supplies a locked IMAGE
landmark packet containing `Grid B2`, retain that **existing cell** verbatim for its spatial
drift parser. The exception is IMAGE-only; it is not permission to invent Grid coordinates,
insert them into VIDEO, write numerical scales or add weighted negative prompts. See
`omni-grid-notation-ban` in `contract-registry.json`. An audit using this exception names it
instead of claiming the prompt contains no coordinates.

Temporal order is natural prose with explicit clean cuts. There is no timecode exemption
and no required sentence pinning cuts to seconds.

## Fenced block format

Deliver one fenced `text` block, then the Chinese audit table. Slot labels stand alone;
separate slots with a blank line. No XML, bullets, tables or hidden field labels inside the pack.

```text
图片提示词
图片 1:
Generate a static anchor ...

图片 2:
Generate the same camera-family anchor ...

视频提示词
视频 1:
Use IMAGE 1 as the first-frame anchor and IMAGE 2 as the last-frame anchor. ...

对话微调提示词
编辑 1:
In VIDEO one, preserve ... and change only ...
```

Include the edit section **only when requested**, with the exact requested number. An explicit
edit request without a number defaults to two. Each edit names its target, permitted change
and protected anchors/result, and can be pasted independently into Gemini Omni. Do not add
new materials, electrical lighting or props through an edit that is meant to preserve them.

## Compact IMAGE pattern

```text
Generate a static [camera-family] anchor of [carrier/location]. Preserve [three depth landmarks and relationships], [light direction] and [capture character]. [Short envelope signature, roof form, complete opening constraints and fixed carrier feature if interior.] [Current named milestone, full completion extent and count.] [Visible registered substrates and their current states.] [Permanent earlier results and traces, including explicit retention of occluded items.] [Located retained stock/resting tools and captured waste.] [Two plausible capture artifacts and tactile surface detail.] Keep captions and rendered text out of the image.
```

Describe static states without active people or moving tools. Use the three-part concrete
damage vocabulary for the first raw anchor. Between anchors, change only the current beat's
result; inherited props cannot be pre-positioned to bypass a requested carry-in operation.
Every partial and settled interior remains within the actual carrier, not a generic room.

## Compact construction VIDEO pattern

```text
Use IMAGE N as the first-frame anchor and IMAGE N+1 as the last-frame anchor; show only [operation and full declared extent]. The wide working shot matches the opening anchor and [fixed layout/light]. Immediately afterwards [worker identity] reaches from [credible adjacent access] into [first contact with specific tool], showing the first [action] through [result]. Repeated [work cycles] consume [located source] and retain [accounted waste]. A clean cut moves into a close-up insert on [already-worked contact/resistance]. A second clean cut shows an extreme close-up insert of [two traces formed earlier], with no completion advance. A final clean cut returns to a returning wide shot from the same camera setup as the opening wide working shot. Resume the same progress, then finish the remaining [repetitions] the same way; [last contact gesture] withdraws the visible worker to match IMAGE N+1 exactly, retaining [results, traces, stock and equipment]. Edited construction time-lapse assembled from multiple camera setups, not real-time footage. Inserts hold completion fixed; cuts compress only demonstrated repetitions. [Capture artifacts.] [Material-specific SFX] over [ambient sound]; no music, captions or rendered text.
```

This is the four-shot pattern. At 4s/6s omit the extreme insert and carry both traces in the
single close-up. For reward, full crossing, expanded stages or the user's one take, use their
role-specific patterns instead. All final IMAGE deltas must have their generating action in
the corresponding VIDEO; completed trace language cannot precede its causal contact.

## Evidence-based Chinese audit

Review the actual final pack against the original request and applicable references. Use
`通过` only with checkable evidence; use `需修订` for an observed failure, `待核验` for an
unperformed check, and `不适用` (with the reason) for irrelevant checks. A user's camera or
style override is `用户覆盖／不适用` for the replaced default, not a fabricated pass.

A concise table is sufficient; combine related physical gates without silently skipping them:

```markdown
| 审核项 | 状态 | 证据 |
|---|---|---|
| 请求动作与交付清单 | 待核验 | 原请求动作逐项对应VIDEO及IMAGE差量；图片/视频/编辑实际数量与要求比较。 |
| 镜头与剪辑 | 待核验 | 逐条列角色、镜数、实际cut；施工return同位置/构图/焦段；单镜用户覆盖单列。 |
| 因果、物料与累积状态 | 待核验 | 首次接触→结果→痕迹顺序，来源/路径/数量/废料去向，相邻delta、保留/遮挡对象及驻场设施生命周期。 |
| 工序、光线与空间 | 待核验 | 工序依赖、供电/封板顺序、wet/dry端点、外壳/屋顶/开口/材质/固定地标；无相关操作写不适用。 |
| 门槛及相机族 | 待核验 | 无跨门槛写不适用；否则列具体拓扑阶段与槽号、三镜、系绳及零施工增量；不写固定“两条桥”。 |
| 正文计数与记号 | 待核验 | 每个IMAGE/VIDEO/EDIT的实测词数/上限，缺槽及记号结果；runtime Grid例外明确披露。 |
| 风格、引用与声音 | 待核验 | 用户风格覆盖、真实引用素材及使用槽、工序/脚步声音、无新增文字/物体。 |
| 对话微调 | 待核验 | 用户要求N条、实际N条及目标槽；未要求写不适用。 |
```

Do not paste these placeholder rows as if reviewed. For example, the count row should say
`IMAGE one 162/180; IMAGE two 156/180; VIDEO one 368/455; edits required zero, found zero`
using actual final measurements. Missing requested edits require repair even when the
construction footage itself is valid. Manual text checks and rendered-media inspection are
separate: a text review cannot certify an ungenerated image or video.
