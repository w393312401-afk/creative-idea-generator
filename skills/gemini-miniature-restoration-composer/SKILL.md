---
name: gemini-miniature-restoration-composer
description: 专门为微缩模型建造、迷你房屋翻新、巨人手手工DIY（Miniature Diorama / Giant Hand DIY / Dollhouse Construction）生成延时提示词。接受中文或英文主题、参考图片、参考视频。输出 copy-ready 的 IMAGE 关键帧锚点和**多镜头组接**的微距 VIDEO 提示词：单段是一条贯穿全段的微距主工作镜，被一到两个特写插入切开，再切回同一台锁死机位收尾，切点用时间线句钉在秒上；禁止一镜到底，也禁止景别轮换。强制微缩摄影语法：超大真人手（Giant Human Hands）从画幅边缘伸入进行微距精密装配，微缩树脂人偶（Tiny Figurines）在微缩场景中观察/生活；室内工序强制采用敞开式娃娃屋剖面（Cutaway / Open-front Dollhouse View）而非走入式全景；锁定微距镜头、浅景深虚化与微缩手工艺材质。Trigger this skill when the user asks for 微缩模型提示词, 迷你修建, 巨人手微缩, 娃娃屋改造, miniature diorama, dollhouse build, giant hand miniature, 微缩多镜头, 多镜头微缩改造, 或反推微缩建造延时视频.
---

# Gemini Miniature Restoration Composer

## Core Job

This skill creates copy-ready prompt packs for **Miniature Diorama Construction, Giant Hand DIY, Miniature House Transformation, and Dollhouse Restoration** time-lapse videos.

It operates under a dedicated miniature & macro photography worldview that is strictly decoupled from full-scale civil construction:

1. **The Actor**: The primary construction agent is **Oversized Real Human Hands (Giant Hands / Macro Fingers)** entering from frame margins to place, glue, screw, trowel, or wire miniature components.
2. **The Residents/Observers**: **Two tiny resin figurines / miniature residents (1:12 or 1:24 dollhouse scale)** inhabit or watch the miniature construction progress.
3. **The Optics**: **Macro diorama photography (50mm-85mm macro lens feel)**, locked at model eye-level (10-15cm above tabletop/ground) with creamy background bokeh (shallow depth of field).
4. **The Architecture**: All interior spaces use **Open-front Cutaway Dollhouse Framing (娃娃屋敞开立面/剖面)**, keeping the camera outside the model so giant hands can physically reach into rooms from above/side.
5. **The Edit**: Every VIDEO prompt is an **edited multi-shot sequence with stated cut marks** — one sustained `macro working shot`, cut open by one or two close-up inserts, then cut back to the *same locked camera setup* to land. Three shots at four and six seconds, four at eight and ten. This is not a shot-scale rotation, and it is never a one-take. Full grammar in `references/miniature-multishot-language.md`.

## Four Standing Conflict Resolutions

1. **Giant Hands beat Lone Full-Scale Workers.** Never describe a 1.78m human worker walking inside a miniature room. Construction is delivered 100% via human hands entering from frame edges wielding precision micro-tools.
2. **Open-front Cutaways beat Walk-in Thresholds.** Never push the camera through a door into a full-scale enclosed room. Interior fit-out is filmed through an open facade or cutaway roof with tabletop macro depth.
3. **Miniature whitelist beats anti-cavernous negative constraints.** The terms `miniature`, `diorama`, `dollhouse`, `tiny figurines`, and `oversized hands` are authoritative positive features, not negative bans.
4. **Cut coverage beats every one-take instinct; the locked setup beats every camera-move instinct.** The camera never pans, tracks, pushes, or pulls back — but the clip is still cut. "机位不动" constrains the *camera*, not the *edit*: the inserts cut closer on the same model and cut back at the same completion level. There is no exemption for the cutaway reveal beat or for the final reward beat; both keep their own three-shot ladder under the same shot names. The retired one-take wording (`continuous miniature craft time-lapse`, `one unbroken take`) is banned outright.

## Required Reference Loading

Loaded on every composition run (the composer reads them into the system prompt; there is no fallback to the base package):

- `references/miniature-scene-skeleton.md`: 微距光学参数、沙盘基底锁死。
- `references/miniature-multishot-language.md`: 镜头梯、切点表、节奏声明、镜内连续性、巨人手在各镜里的状态。
- `references/miniature-macro-language.md`: 巨人手动作词表与人偶设定。
- `references/miniature-materials-and-tools.md`: 微缩材质与微型工具。
- `references/miniature-cutaway-architecture.md`: 娃娃屋剖面两大构图模式。
- `references/miniature-output-templates.md`: IMAGE/VIDEO 输出模板、负向词库、记号禁用。

`references/prompt-templates.md` 由逐拍裁剪送入（`get_cropped_templates`），
`references/spatial-consistency-upgrade-protocol.md` 与
`references/threshold-bridge-consistency-protocol.md` 走各自的通路内插。

## Input Tiers

### Tier 0 - Idea Request (微缩创意发动机)
User asks for miniature topic ideas (`来几个微缩修建点子`, `迷你改造创意`). Run the Miniature Idea Engine in `references/idea-engine.md`.

### Tier 1 - Minimal Topic
User provides a short topic (e.g. `林间树桩微缩二层别墅建造`, `苔藓岩石迷你避难所`). Synthesize full miniature scene skeleton and beat ladder.

### Tier 2 - Multimodal Stack
User provides reference images, audio, or video clips for miniature style transfer.

### Tier 3 - Existing Beat Ladder
User provides an existing production beat ladder. Adapt to miniature cutaway grammar.

### Tier 4 - Video Reverse-Engineering (爆款微缩复刻)
User provides an existing viral miniature construction time-lapse video. Extract per-frame micro facts (block courses, micro trowels, giant hand actions, figurine locations) and compose 1:1 matching prompts.
