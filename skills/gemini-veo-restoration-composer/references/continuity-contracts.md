# Continuity Contracts

Rendered from [`references/skill-local-contracts.json`](skill-local-contracts.json) — **edit the registry, not this file.** Regenerate with `python scripts/render_continuity_contracts.py` after any registry change, then run `python scripts/validate_contracts.py` to confirm every `enforcer` still resolves to real code.

This is the skill-local counterpart to the project root's `references/contract-registry.json` (which governs the server-side rendering pipeline in `prompt_pipeline/__init__.py`). This file governs only what ships inside this skill package: `video_to_prompt_pipeline.py` (the standalone Tier-4 video reverse-engineering CLI) and `scripts/render_and_gate_anchor.py`.

## P0 — Kill Gates (block delivery)

### `GCTR`

每个可见的新增/移除/修复/清洁/安装/组装/搬运/机械动作，必须在下一帧留下至少两条可辨识的因果痕迹证据。

- **Source**: SKILL.md Step 6 Global Causal Trace Rule
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Global Causal Trace Rule`

### `MTAL`

非无人、非过门的 VIDEO 里，每一次工具名词出现都必须在其前 8 个词的窗口内命中颜色/材质锁定词，而不是整条 clip 里任意位置出现一次颜色词就算通过。

- **Source**: SKILL.md Step 6 MTAL definition, Step 9 Geometric Tool Lock Gate
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Geometric Tool Lock Gate`

### `NLVTR`

正文禁止出现 '%' 符号、内部缩写全集（TSPA/HAL/VMFP/GCTR/RPL/RCE/SCUP/NGCS/OSPL/RHMA/PBISP/HCL/NLVTR/MTAL）、以及 Grid A1-C3 坐标 token。

- **Source**: SKILL.md Step 8 point 5, Step 9 No Banned Notations Gate line ~613
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:No Banned Notations Gate`

### `SPCP`

按 shot_family 锁定相机姿态措辞：室外/桥接/奖励用 horizon line；室内/封闭空间用 camera pitch locked + 居中灭点轴，且禁止出现 horizon/sky/clouds；elevated 用 pitch angle + convergence direction。optical flow 措辞只允许出现在 push-in 类镜头。

- **Source**: SKILL.md Step 6 (Camera DNA Block SPCP), Step 9 P0 Kill Gates
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Sub-Pixel Coordinate Pinning Gate`

### `banned-transition-shortcuts`

禁止 cross-dissolve/fade-in/suddenly/magically/rapid montage/jump cut/instant transformation 等跳过施工过程的转场措辞。

- **Source**: SKILL.md Step 8 Banned Transition Shortcuts
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Continuous Action Flow`

### `beat-overload`

一个 beat 最多包含三个同 zone、同 phase family、共同服务于一个命名终态产物的动作；跨 phase family 的动作组合（如拆除+粉刷、rough-in 与遮盖它的面板同 beat）在任意数量下都 FAIL。

- **Source**: SKILL.md Step 5 Visible Milestone Package Rule, Step 9 Beat Overload Pop Prevention Gate
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Beat Overload Pop Prevention Gate`

### `direct-at-zero-worker-lock`

出现工人的施工 VIDEO 必须从 t=0s 起已经在作业面立即做有效动作，并持续到片段结束，不安排工人进场或退场。

- **Source**: SKILL.md Step 6 Direct-at-Zero Worker Clause
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Direct-at-Zero Worker Lock`

### `clean-frame-boundary`

所有 IMAGE 锚点必须完全不含在场工人/机械动作词。

- **Source**: SKILL.md Step 6 Clean Frame Boundary
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Clean Frame Boundary`

### `grid-internal-only`

Grid A1-C3 坐标只用于内部记账（NGCS/OSPL 装配阶段），最终交付的 IMAGE/VIDEO 正文一律不得出现字面 Grid 坐标 token；必须转译为方位+深度层+高度比例的自然语言。

- **Source**: SKILL.md Step 6 (Grid-is-internal-only note), Step 9 No Banned Notations Gate
- **Enforced by**: `video_to_prompt_pipeline.py::function:grid_to_natural_language`

### `image-video-frame-binding`

每条 VIDEO 必须显式声明与前后 IMAGE 锚点的绑定（exact composition anchors）。

- **Source**: SKILL.md Step 8 point 1-2
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:IMAGE-VIDEO Frame Binding`

### `manual-tool-construction-realism`

非无人、非过门的 VIDEO 必须包含具体手持施工工具名词，并声明两条自然语言进度线索。

- **Source**: SKILL.md Step 8 point 5 Measurable Progress Markers
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Manual Tool & Construction Realism Gate`

### `p0-blocking-rewrite-loop`

任何 P0 门失败必须阻断交付，且只能针对性重写失败的槽位（最多 2 次），不得整套重生成；重写 2 次仍失败必须停止并升级为人工介入，不得静默交付。

- **Source**: SKILL.md Step 9 P0-Blocking Targeted Rewrite Loop
- **Enforced by**: `video_to_prompt_pipeline.py::function:audit_blocking_failures`
- **Note**: audit_blocking_failures() 只覆盖独立 CLI（video_to_prompt_pipeline.py::main()，P0 失败时 exit(1) 且不再打印 successfully 横幅）。对话式 SKILL.md 流程里真正执行'针对性重写、最多 2 次、然后升级'这套循环的是 agent 本身，无法被这个 CLI 脚本校验——那部分的强制力完全来自 SKILL.md 这段 prose 有没有被遵守。

### `power-chain`

任何通电灯具/装置的激活必须有更早的布线/电源 beat（离网载体还需一个可见电源安装 beat）。

- **Source**: SKILL.md Step 5 Construction Sequence Validation hard vetoes
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Power Chain Gate`

### `render-exit-code-taxonomy`

render_and_gate_anchor.py 的退出码必须准确区分：2=连接失败（唯一触发'服务不可用'豁免的退出码）、3=服务已连接但返回错误或响应体不合法、4=缺少必需的 prompt 文本、5=超时（读超时或连接期超时，均不等同于不可用）。HTTP 错误状态码不得被误判为退出码 2。

- **Source**: scripts/render_and_gate_anchor.py module docstring, SKILL.md Step 6.5 points 4-5b
- **Enforced by**: `scripts/render_and_gate_anchor.py::exit_code:3`
- **Note**: 退出码 2/4/5 同样由这次修复引入/修正，均可用 exit_code:<n> 形式单独登记；这里只挑最容易和'不可用(2)'混淆的退出码 3 作为代表性校验目标，避免注册表条目膨胀成一码一条。

### `rigid-container-encapsulation`

散装材料必须封装在刚性容器内描述，不得写裸露的铲运动作。

- **Source**: SKILL.md Step 8 VMFP with Rigid Encapsulation
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Rigid Container Encapsulation`

### `staged-anchor-render-gate`

分阶段交付：先只出 IMAGE 1 并同步渲染，人工确认后才允许撰写其余槽位；首帧被否决后必须走定向修复+重渲循环（最多 3 次）并清除磁盘上的旧帧，不得被 Step 13 静默复用。

- **Source**: SKILL.md Staged Delivery Contract, Step 6.5 points 4 and 8
- **Enforced by**: *(no programmatic enforcer — see gap note)*
- **Gap**: 这条约束的是 agent 在对话里的交付节奏，本仓库没有让这个独立 CLI 脚本参与分阶段渲染（它是一次性反推整套 beat 的批处理工具，不经过 render_and_gate_anchor.py）。render_and_gate_anchor.py 提供了 --force_regenerate 参数作为'不复用磁盘旧帧'的机制入口，但是否在被否决后真的调用它、真的清理磁盘，完全取决于 agent 是否遵守 SKILL.md Step 6.5 第 8 条——无程序化执行者，登记于此以免被误当作已执行。

### `temporal-physics-skeleton`

每个 beat 必须声明 shot_family、beat_type、single_physical_operation，以及完整的 causal_path（含至少两条 persistent_traces）。

- **Source**: SKILL.md Step 5.5 Temporal Physics Skeleton Assembly
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Temporal Physics Skeleton Gate`

### `threshold-bridge-continuity`

过门桥接 beat 必须与施工内容隔离，且前一个锚点必须预先展示至少两个室内地标。

- **Source**: SKILL.md Step 8 Threshold bridge section, TBCP
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Threshold Bridge Continuity Gate`

### `tier4-evidence-discipline`

Tier 4 视频反推：画面中没有看到工人/工具时，必须如实标注为 unobserved，不得编造标准施工工具或动作循环来'防止变形'。

- **Source**: SKILL.md line ~107 (Two-Stage Isolation, Anti-Priming)
- **Enforced by**: *(no programmatic enforcer — see gap note)*
- **Gap**: 这条约束的是 LLM_SYSTEM_PROMPT 里喂给分析模型的指令文本（一段长自然语言指令，2026-08-07 已从'必须编造标准工具'改写为'必须如实标注 unobserved，禁止编造'），本质上是提示词工程而非可静态断言的代码逻辑。是否真的没编造，只能靠人工抽查分析结果里 tool_evidence 字段与画面是否一致。登记于此以免被误当作已有程序化执行者。

### `word-count`

IMAGE 100-200 词、VIDEO 120-240 词的硬性区间（2026-08-07 从 100-170/120-180 上调，原区间对本管线自身必填结构元素而言不可达）。

- **Source**: SKILL.md Step 7 word count targets, Step 9 Word Count Self-Check Gate
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Word Count Self-Check Gate`

## P1 — Rewrite Gates (score deduction, fix before delivery)

### `enclosed-space-provenance`

新开洞口后镜头进入的室内空间必须在开洞 beat 里声明是预先存在的，或者有独立的挖掘/清运 beat 交代其成因。

- **Source**: SKILL.md Step 5 Enclosed-Space Provenance Rule
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Enclosed-Space Provenance Gate`

### `mirror-reflective-alignment`

结尾 IMAGE 如果描述了高反光地面，必须带 RHMA-Blur 式的高模糊低光泽反射描述子句（子句本身在最终正文里以自然语言呈现，不留 RHMA-Blur 缩写）。

- **Source**: SKILL.md Step 7 Final IMAGE template, Mirror Consistency Clause
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Mirror Reflective Alignment`

### `naturalize-sanitizer-idempotent`

naturalize_visual_text() 必须幂等（二次调用不再改变输出），且不得残留空括号或悬空标点。

- **Source**: internal implementation guarantee, not directly SKILL.md-visible
- **Enforced by**: `video_to_prompt_pipeline.py::function:naturalize_visual_text`

### `prompt-slot-structure`

门禁读取 shot_family / beat_type / enclosed / is_bridge / sterile 时必须从 time_sequence 派生的结构化字段读取，不得靠对渲染出的正文做关键词猜测（历史上 MTAL/SPCP 的假通过全部源于关键词猜测）。

- **Source**: internal implementation guarantee
- **Enforced by**: `video_to_prompt_pipeline.py::class:PromptSlot`

### `volume-conservation`

移除/运入的材料体量必须靠容器规模、往返趟数或可见增长的堆料来交代；切出的实心大块必须有独立的撬出/搬出动作。

- **Source**: SKILL.md Material, Access, And Crew Plausibility section
- **Enforced by**: `video_to_prompt_pipeline.py::gate_name:Volume Conservation Gate`

## P2 — Polish / Advisory

### `adjacent-anchor-delta-budget`

相邻 IMAGE 锚点允许改变的格数按 beat_type 分级（removal/excavation<=4，coating/interior_finish<=6，fixture_install/furnishing<=3），且格数只是提醒工具，declared full extent 优先于格数上限。

- **Source**: SKILL.md Step 5 Adjacent-Anchor Delta Budget table
- **Enforced by**: *(no programmatic enforcer — see gap note)*
- **Gap**: 这是一条软提醒（P2，非阻断），当前只有 Cumulative State Gate 的正文校验（inherited traces / declared operation result）在代码里执行；格数本身没有专门的程序化门，因为 SKILL.md 明确写了格数不是硬门。登记于此以说明这是刻意的设计，不是遗漏。

### `auto-save-opt-out`

Step 12 的自动入库默认执行，但用户在对话中表达过不想自动入库时必须跳过且不再重新提起。

- **Source**: SKILL.md Step 12 Auto-Save to Idea Library
- **Enforced by**: *(no programmatic enforcer — see gap note)*
- **Gap**: 这是一条对话记忆型约束（'用户是否在本次对话里说过不要自动入库'），依赖 agent 对上下文的记忆与遵守，无法被独立 CLI 脚本静态校验。save_to_library.py 本身没有、也不需要感知这个选择——跳过与否发生在是否调用它之前。

