"""base profile（gemini-veo-restoration-composer）的 Phase 2 composer。

这里是 prompt_pipeline.compose_remaining_beats 的原实现，逐字平移过来的——行为必须
与拆分前完全一致，任何"顺手优化"都属于越界。

平移时只做了一处机械改写：所有对 prompt_pipeline 模块级函数的调用改成 `pp.xxx` 的
属性访问（而不是 `from .. import xxx`）。这不是风格偏好——全套测试都用
`patch.object(pp, 'validate_beat_prompts', ...)` 这类打桩方式，import 绑定会把桩打空。

可被 profile 子类覆写的钩子集中在类的前半部分，且**只覆盖 VIDEO 一侧**：
  · batch_system_prompt / single_beat_system_prompt —— VIDEO 撰写指令（IMAGE 段的
    指令也在同一份 system prompt 里，子类的做法是追加覆写段而不是重写整份）
  · apply_proactive_fixes —— 确定性修复（IMAGE 侧一律委托 base）
  · validate_beat_prompts / split_structural_video_errors / rework_structural_video_beat
    —— 审计与定向回炉：子类把自己的违规项标成结构性硬伤，就自动接进既有的
    「校验 → 回炉一轮 → record_beat_audit 留痕」通路，主流程一行都不用改
  · finalize_fallback_video —— 占位符兜底稿的收尾
"""

import json
import sys

import prompt_pipeline as pp


class BaseComposer:
    """Phase 2 的默认实现。子类只覆写 VIDEO 相关钩子，主流程不复制。"""

    profile = 'base'

    def __init__(self):
        # 本次运行的上下文，由 begin_run 填。system prompt 的风格分支要读 brief
        # （例如"用户明确要院线感"），而钩子签名里没有它。
        self.config = None
        self.state = None

    # ── 可覆写钩子（默认全部原样委托给 prompt_pipeline 的模块级实现）──────────

    def begin_run(self, config, state):
        """每次 Phase 2 开跑时调用一次，让子类拿到本单的 config/state。base 只记下来。"""
        self.config = config
        self.state = state

    def banned_elements_block(self):
        """反推复刻线的负面清单段落。非复刻单返回空串。

        `banned_elements` 是 Pass B 从帧事实里反推出来的「这类改造通常会有、但原片里
        一帧都没出现」的东西。2026-08-10 之前它只在成品提示词上做事后 substring 扫描：
        写手从未见过这份清单，扫出命中也只是记一笔就照常交付——一道声称存在的门禁，
        实际是张事后报告单。现在它进 system prompt，命中率的问题在写之前就解决掉，
        `banned_element_hits` 退回它本来该是的角色：交付前的兜底复核。
        """
        brief = (self.state or {}).get('parsed_brief') or {}
        banned = [str(x).strip() for x in (brief.get('banned_elements') or []) if str(x).strip()]
        if not banned:
            return ""
        return (
            "\n==================== BANNED ELEMENTS (HARD, THIS JOB ONLY) ====================\n"
            "This job reproduces the beat ladder of a real reference film. The following things "
            "are exactly the ones a renovation of this type would plausibly involve but that "
            "appear in NO frame of that film. They are absent on purpose — writing them in would "
            "invent work the reference never showed:\n"
            + "\n".join(f"- {x}" for x in banned)
            + "\nNever name any of these in a VIDEO or IMAGE prompt, in any wording, including as "
              "something absent, removed, or not present. If a beat seems to need one, the beat "
              "is describing work the reference film did not contain — write only what the beat's "
              "own declared fields state.\n")

    def scene_constants_block(self):
        """反推复刻线的场景恒常特征段落。非复刻单返回空串。

        与 `banned_elements_block` 严格对称：那一段说「原片里永远没有的东西」，这一段说
        「原片里一直都在的东西」——墙上的绿霉污渍、屋檐的青苔、常驻画面的那盏工作灯。

        它们为什么需要单独一段：整条反推链路是围绕**变化**建的，节拍只承载每一拍的
        delta，而恒常的东西不产生 delta。实测（2026-08-13）一条片子里 57% 的帧都记到了
        墙上的污渍，而它在整条节拍阶梯里一个字都没有。不把它们送进来，写手写出的就是
        同一道工序的通用想象——干净的混凝土、没有落叶青苔——工序全对，就是不像那条片子。
        """
        brief = (self.state or {}).get('parsed_brief') or {}
        from prompt_pipeline import reverse
        lines = reverse.scene_constants_lines(brief.get('scene_constants'),
                                              brief.get('scene_signature'))
        if not lines:
            return ""
        return (
            "\n==================== SCENE CONSTANTS (THIS JOB ONLY) ====================\n"
            "These are present in the reference film from the first frame to the last. They are "
            "not work anyone performs — they are what the place is made of and what it looks "
            "like. Carry them through EVERY prompt as standing description of the environment:\n"
            + "\n".join(f"- {x}" for x in lines)
            + "\nNever describe a surface these cover as clean, new, or unmarked unless a beat "
              "explicitly says that beat's work made it so. They are the reason the reference "
              "film looks like itself.\n")

    def batch_system_prompt(self, config, packet, scup_ref, tbcp_ref):
        """批量直出调用的共享 system prompt（每拍都相同的那部分）。"""
        return (pp._batch_shared_system_prompt(packet, scup_ref, tbcp_ref)
                + self.banned_elements_block() + self.scene_constants_block())

    def single_beat_system_prompt(self, config, i, contract, packet, compiled_images,
                                  compiled_videos, scup_ref, tbcp_ref_i):
        """单拍兜底生成的 system prompt。"""
        beat = contract['beat']

        prior_prompts_block = ""
        if i > 1:
            prior_prompts_block = f"""
==================== PREVIOUS BEAT GENERATED PROMPTS (DO NOT DUPLICATE PHRASING) ====================
To prevent formulaic repetition, the vocabulary, sentence structures, and opening patterns of VIDEO {i} and IMAGE {i+1} must NOT duplicate or mirror those in the previous beat prompts:
Previous VIDEO {i-1}:
{compiled_videos[i-1]}

Previous IMAGE {i}:
{compiled_images[i]}
"""

        return f"""You are a professional prompt composer operating under the `gemini-veo-restoration-composer` skill.
Your job is to generate exactly two prompts for Beat {i}:
1. VIDEO {i}: The construction timelapse video.
2. IMAGE {i+1}: The clean environment state snapshot after the video.

==================== LIGHTING PHASE CONTRACT FOR THIS BEAT ====================
- IMAGE {i} (State before this beat) uses lighting phase: {contract['img_i_lighting']}
- IMAGE {i+1} (The state you are generating now) MUST use lighting phase: {contract['img_ip1_lighting']}
- VIDEO {i} (The transition video prompt) MUST describe the transition matching this lighting phase progression: from '{contract['img_i_lighting']}' to '{contract['img_ip1_lighting']}'.

==================== SHOT FAMILY CONTRACT FOR THIS BEAT ====================
{contract['family_contract']}

==================== SKILL CONTRACTS ====================
{scup_ref}
{tbcp_ref_i}
{contract['templates_cropped']}

==================== DRIFT LOCK PACKET ====================
{json.dumps(packet, indent=2, ensure_ascii=False)}

==================== PRIOR PROMPTS (for continuity) ====================
IMAGE 1 (Trauma State):
{compiled_images[1]}

IMAGE {i} (State before this beat):
{compiled_images[i]}
{prior_prompts_block}

Instructions:
- VIDEO {i} must start with: "Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout."
- VIDEO {i} must use progressive (-ing) verbs for ongoing actions, name worker silhouettes (HAL) and tools (MTAL) if workers are present, encapsulate bulk materials in rigid containers (VMFP/RCE), and include pacing control "continuous construction time-lapse, not real-time footage" (unless threshold or reward).
- EVEN RATE (unless threshold or reward): the clip must also state that the transformation advances continuously and at an even rate across the entire clip duration — at every moment something is visibly progressing, no interval of the clip is static or paused, and no part of the change is deferred and then delivered as a single sudden step. Distribute the beat's work evenly over the whole clip; never describe the scene as holding, settling, or waiting mid-clip, and never save a visible portion of the milestone for the final moment.
- VIDEO {i} CONCRETENESS (no abstractions): describe the SAME single lone worker every beat, reusing the exact costume from the packet worker_choreography (e.g. "one lone worker in a solid pale shirt, dark pants, and dark cap"); name the ONE specific manual tool used; describe the concrete repeated work cycle in -ing verbs (e.g. scooping, lifting, pressing, fastening). NEVER write vague filler like "transformation progresses" or "the scene transforms" — show observable physical actions only.
- FULL-FIELD DELTA CONSERVATION & MULTI-ZONE ACTION COVERAGE (P0): In VIDEO {i}, all physical differences between IMAGE {i} and IMAGE {i+1} across 4 spatial zones (Top/Roof/Ceiling, Middle/Walls/Openings, Bottom/Floor/Approach, Peripherals/Spoil/Materials) MUST have 100% assigned worker actions, explicit geometric tools, and corresponding audio SFX. If IMAGE {i+1} shows structural demolition (e.g. rotted roof boards/membrane stripped away) or debris cleared alongside ground work, VIDEO {i} MUST explicitly describe the worker dismantling and tossing/stacking those roof elements into designated spoil piles as well as the ground clearing. Zero phantom changes: never leave any changing zone unacted. Material balance: demolition debris must visibly stack/bundle, and installed materials must deplete.
- VIDEO {i} must end with a PERSISTENT-TRACES clause naming the marks this beat leaves behind (e.g. scrape grooves, end-grain circles, screw heads, nail rows, sawdust trails, trimmed edges, compression tracks), followed by a natural-language description of both the near-field diegetic sound effects (2-4 specific sounds of tools, materials, or footsteps) and the steady room/environment ambient noise. Use varied phrasing for these audio descriptions rather than a single formulaic structure.
- IMAGE {i+1} must be a clean frame with ZERO workers/machinery. Do NOT use the words 'worker', 'builder', 'carpenter', 'laborer', 'person', 'man', 'woman', or 'people' under any circumstances, even to state that they are absent or not present. Describe only static objects, surfaces, and traces. {contract['anchor_rule']} Then describe this beat's state delta following its own STAGE SCOPE TIER (see the STAGE SCOPE FOR THIS BEAT instruction below). Also include a FEW (2-3, not exhaustive) PERSISTENT physical traces that prove the work happened (scrape marks, fastener heads, sawdust, membrane wrinkles, displaced soil, etc.).
- {pp._milestone_beat_directive(beat, img_before=f"IMAGE {i}", img_after=f"IMAGE {i+1}") or (pp.outline_delivery_directive(beat) + 'This is a threshold/bridge/reward beat — follow its dedicated camera/reward rules instead of the ordinary milestone package contract.')}
  Prior MAJOR installed/finished features (panels, walls, floors, fixtures, primary landmarks) stay present and unchanged (monotonic state) — but you do NOT need to re-list every minor trace from every earlier beat; it is fine and expected for small cosmetic details to fade from the description as new ones accumulate.
- REWARD BEAT TWO-PHASE STRUCTURE (only applies if this beat's operation is "reward" — the final beat): this clip is now the sequence's ONLY closing shot, so it must carry both jobs. Its first two thirds deliver the declared reward as ACTUAL PHYSICAL MOTION — the mechanism moving through its travel, the lights coming up, the occupant walking in and using the space — never a static hold on a finished room. Its final third settles into a held, slightly tightened framing on the signature anchor with no further action, and that settle IS the closing appreciation; no separate showcase clip follows it. Keep the whole space finished, styled, and free of workers, tools, materials, and construction activity throughout.
- For threshold bridge beats (if beat is a threshold bridge), follow the TBCP rules: the ENTIRE exterior-to-interior crossing is ONE single beat (bridge_stage 1) — there is no separate hold/sill/vestibule/turn beat. Its VIDEO is the ONLY visible clip for the crossing, bound normally from the previous beat's IMAGE to this beat's own IMAGE, and must depict the full exterior-to-settle arc (plus, in the PAN variant, ending in a stationary pan locking onto the interior's long axis) in one continuous shot, with the door-frame wipe, exposure/white-balance roll, and anchor scale-up all completing within it. EVERY crossing — bridge or DECLARED CUT-IN alike — starts from a CLOSED entry: the previous beat's IMAGE keeps its door/hatch shut (or, on a carrier with no leaf yet, its raw opening unlit and opaque) and shows nothing of the interior, and the crossing clip itself pushes that entry open on camera before advancing through it. There is no interior peek and no anchor scale-up before the clip. A DECLARED CUT-IN beat works the same way on the video side — its VIDEO is a real generated crossing clip, written as an ordinary video prompt bound from the previous beat's IMAGE to this beat's own IMAGE — while its IMAGE re-establishes the interior from scratch per its anchor rule. The crossing clip enters an untouched ruin and stays that way for its whole length — nothing is cleaned, cleared, tidied, repaired, or installed while the camera moves, and no tool, ladder, scaffolding, tarp, work light, or stacked material appears in it; write it as one unbroken take at a steady speed (no cut, fade, dissolve, speed ramp, or freeze), and never call it a construction time-lapse.
- THRESHOLD MONOTONIC INHERITANCE (P0): When crossing from exterior to interior (IMAGE T+1), the interior first reveal MUST 100% physically inherit all envelope/structural work completed in prior exterior beats:
  1. Roof/Ceiling: If exterior beats built or weatherproofed the roof, the ceiling underside in IMAGE T+1 MUST show the newly installed roof timbers/sheathing/membrane. NEVER describe ceiling cracks, missing roof sections, or water leaks.
  2. Ground/Floor: If an earlier exterior beat already cleared the earthen floor, the floor in IMAGE T+1 is already clean bare earth. DO NOT describe fallen timber or leaf piles again.
  3. Untouched Scope: ONLY untreated interior wall surfaces, lack of interior framing/insulation, and lack of internal utilities remain in their raw state.
- NLVTR visual-only rule: No '%' symbols, no numeric ranges, no acronyms (HAL, SCUP, NGCS, VMFP, RCE, GCTR, RPL, OSPL, RHMA, PBISP, HCL, NLVTR, MTAL, TSPA) in the prompts.
- REALISM rule (mandatory): strictly documentary photorealism. Every material, fixture, tool, and technique must be real-world and present-day (wood, stone, brass, wool, glass, leather, standard trade tools). NO sci-fi, futuristic, cyberpunk, holographic, glowing-tech-panel, LED-neon, or spacecraft-style elements anywhere in the scene.
- SINGLE CONTINUOUS PHOTOGRAPH rule (mandatory): each IMAGE is one real photograph of one moment — never a grid of multiple panels, a collage, a storyboard, a comparison/before-after split, or a multi-view composite. The "Grid A1-C3" notation used elsewhere in this contract is an internal composition-registration convention for you the writer — never describe or render literal grid lines, panel borders, or divided frames in the image itself.
- FULL-ENCLOSURE COVERAGE: When the beat involves framing, insulating, paneling, or painting walls, the IMAGE prompt MUST explicitly include the ceiling/roof/top surface as well. For example, if walls in Grid B1, B3, C1, C3 are paneled, the ceiling curve in Grid A1, A2, A3 must ALSO be described as paneled. Never treat wall coverage as complete without ceiling coverage in any enclosed space (cabin, room, fuselage, container, vault, etc.).
{pp.ENVELOPE_CROSS_VIEW_RULE}
- CAMERA VIEWPOINT CONTINUITY: If the previous IMAGE was shot from an interior viewpoint (camera inside the space, entry behind camera), the next IMAGE MUST maintain the same interior viewpoint UNLESS an explicit camera-pullback VIDEO is inserted between them. You CANNOT jump from interior to exterior viewpoint without a transition. If the beat requires switching back to an exterior view, generate the VIDEO as a reverse dolly pulling back through the doorway, and describe the exposure transition accordingly.
- EXTERIOR WORK VISIBILITY: If the beat involves work on the EXTERIOR surface of the structure (e.g., exterior insulation, exterior membrane), and the camera is positioned INSIDE looking out, the VIDEO must show the worker operating at the boundary edges visible from inside (e.g., working at seam lines visible in Grid B1/B3 from the interior). Do not describe exterior work that would be invisible from the current camera position.
- ZONE-APPROPRIATE PROTECTIVE LAYERS: only describe waterproofing membrane, tar/bitumen coating, or vapor barrier material on a surface with real moisture/weather exposure (below-grade wall/floor, roof, exterior envelope, bathroom/kitchen/pool). Never describe these on an ordinary dry interior wall, floor, or ceiling — use plain primer/paint finish there instead.
- CONSTRUCTION ORDER CONSTRAINTS: Floor finish (hardwood, tile) MUST be installed BEFORE heavy anchored objects (fireplace, stove) are placed on it. If this beat installs a fireplace or heavy object, the IMAGE must show it sitting on the FINISHED floor, not on bare metal/subfloor. If the floor is not yet finished, the fireplace cannot be installed in this beat.
- HUMAN-SPATIAL METRIC CONSERVATION & ERGONOMIC SCALE (P0):
  1. Strict Metric Dimensions: Every space MUST declare explicit 3D metric dimensions (e.g. excavation pit: 3.2m diameter, 1.8m deep; interior room: 3.0m diameter, strict 2.2m ceiling clearance). Never use vague relative terms like 'spacious' or 'waist-deep'.
  2. Ergonomic Prop Scale: In compact structures (diameter <= 3.5m), FORBID oversized residential furniture (e.g. two-tier bunk beds, large sectional sofas) that causes AI to hallucinate cavernous halls. Use compact ergonomic furniture (low-profile single platform daybed with under-bed storage, recessed berth, compact 80cm workbench).
  3. Camera Normalization: Default to 24mm wide-angle lens feel at 1.3m chest height, horizon/vanishing axis at 45%-50% frame height.
  4. Video Worker Scale Figure: VIDEO {i} must declare the worker's metric scale (e.g. 'one lone male worker, 1.78m tall, occupying ~35% of frame height, realistically proportioned to the 2.2m ceiling').
- Output the prompts in the following format:
===VIDEO===
<video prompt body>
===IMAGE===
<image prompt body>
===TRACES===
[
  {{
"name": "precise name of new permanent feature/material/trace (e.g. steel screw heads, green insulation foam)",
"material_color": "color/texture (e.g. metallic silver)",
"initial_state": "state when introduced (e.g. freshly installed)",
"grid": "approximate grid coordinate if mentioned (e.g. Grid B2, default to Grid B2)",
"z_depth_scale": "depth scale if mentioned (e.g. 50%, default to 50%)"
  }}
]
{self.banned_elements_block()}{self.scene_constants_block()}"""

    def apply_proactive_fixes(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, beat=None, config=None, family=None):
        """确定性修复（VIDEO + IMAGE）。"""
        return pp.apply_proactive_fixes(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            beat=beat, config=config, family=family)

    def validate_beat_prompts(self, i, video_prompt, image_prompt, packet, mode, is_last,
                              is_threshold_or_reveal, prev_video=None, prev_image=None,
                              beat=None, family=None, is_pre_bridge=False,
                              is_post_reveal_cleanup=False, video_word_limit=None):
        """单拍校验。video_word_limit 缺省 = base 的一镜到底档硬顶（见 pp 侧说明）。"""
        return pp.validate_beat_prompts(
            i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
            prev_video, prev_image, beat=beat, family=family, is_pre_bridge=is_pre_bridge,
            is_post_reveal_cleanup=is_post_reveal_cleanup, video_word_limit=video_word_limit)

    def split_structural_video_errors(self, errs):
        """把校验结果分成（结构性硬伤, 其余瑕疵）——前者触发定向回炉。"""
        return pp.split_structural_video_errors(errs)

    def rework_structural_video_beat(self, config, i, video_prompt, structural_errs, packet, beat=None):
        """结构性硬伤的定向回炉（只重写 VIDEO）。"""
        return pp.rework_structural_video_beat(config, i, video_prompt, structural_errs, packet, beat=beat)

    def finalize_fallback_video(self, video_prompt, contract):
        """占位符兜底稿的收尾（base 不做任何额外处理）。"""
        return video_prompt

    # ── Phase 2 主流程 ──────────────────────────────────────────────────────

    def compose_remaining_beats(self, config, state, on_progress=None):
        """Phase 2 of the composer: beats 2..N+1 text generation and assembly. Consumes
        `state` from compose_anchor_and_packet(); if the caller refined state['packet']
        against an accepted rendered IMAGE 1, beats 2+ are written against that confirmed
        packet instead of the pre-visualized one.

        skill 直出模式：文本阶段不做任何拦截式审查。批量直出的每拍结果经确定性修复
        （apply_proactive_fixes）后直接采纳；validate_beat_prompts 只以日志形式留痕，
        不触发重写。整套序列的施工顺序/SCUP 一致性审查移到帧渲染完成后，对着真实画面跑
        （见 pipeline_orchestrator._sequence_consistency_review /
        prompt_pipeline.check_full_sequence_consistency），因为凭空文本判断"这套提示词
        会不会渲出违反工序逻辑的画面"既慢又不准。

        断点续传:每完成一拍(beat)就把进度存盘(见 _save_checkpoint),按
        state['brief_fingerprint'] 存取——同一份 dimensions 中断/失败后重试时，已经成功生成
        的拍会被跳过，只重新生成尚未成功的那些拍，不必推倒重来整单重跑。落到占位符兜底的拍
        不算成功，仍会在续传时重新尝试真实生成。"""
        self.begin_run(config, state)
        theme = state['theme']
        total_beats = state['total_beats']
        parsed_brief = state['parsed_brief']
        title = state['title']
        beat_ladder = state['beat_ladder']
        packet = state['packet']
        compiled_images = state['compiled_images']
        compiled_videos = state['compiled_videos']
        brief_fingerprint = state['brief_fingerprint']

        mode = parsed_brief.get('mode', 'Standard')
        _profile = self.profile
        scup_ref = pp.load_reference_file('spatial-consistency-upgrade-protocol.md', _profile)
        templates_raw = pp.load_reference_file('prompt-templates.md', _profile)

        _checkpoint = pp.load_compose_checkpoint(brief_fingerprint) or {}
        pass_beats_done = set(int(x) for x in (_checkpoint.get('pass_beats_done') or []))
        fallback_count = int(_checkpoint.get('fallback_count') or 0)
        slot_states = dict(_checkpoint.get('slot_states') or {})
        for i in range(1, total_beats + 1):
            slot_states[str(i)] = 'validated' if i in pass_beats_done else 'pending'
        diagnostic_mode = bool(config.get('diagnostic_mode') or config.get('diagnosticMode'))
        strict_v2 = config.get('strictPromptPipelineV2', True) is not False
        allow_placeholders = (
            diagnostic_mode and bool(config.get('allowPlaceholderPrompts', False))) or not strict_v2
        if isinstance(config, dict):
            config['_compose_slot_states'] = slot_states

        # 自愈:若存档里的 fallback_count 已超过质量门禁上限,这份 checkpoint 是一次「合成失败」的终态
        # (而非可续的中断)——继续按它续传只会把那几拍当"已完成"跳过、fallback_count 一进门禁就再挂,
        # 使每次重试都变成"零工作量瞬间再失败"(用户侧就是"出错任务重试不了")。此时丢弃拍级续传状态,
        # 从头全量重生成所有拍(Phase 1 的 packet/beat_ladder/IMAGE 1 仍从 state 复用)。
        if pp._checkpoint_is_failed_terminal(_checkpoint, total_beats):
            if sys.stdout:
                print(f"[RESUME] Checkpoint fallback_count={fallback_count} 已超门禁上限 {max(2, total_beats // 3)}，"
                      f"判定为失败终态存档而非可续中断；丢弃拍级续传状态，全量重生成所有拍。")
            pass_beats_done = set()
            fallback_count = 0

        def _save_checkpoint():
            pp.save_compose_checkpoint(brief_fingerprint, {
                'theme': theme,
                'total_beats': total_beats,
                'parsed_brief': parsed_brief,
                'title': title,
                'beat_ladder': beat_ladder,
                'packet': packet,
                'image_1_prompt': compiled_images.get(1, ''),
                'compiled_images': pp._checkpoint_encode_slots(compiled_images),
                'compiled_videos': pp._checkpoint_encode_slots(compiled_videos),
                'pass_beats_done': sorted(pass_beats_done),
                'fallback_count': fallback_count,
                'slot_states': dict(slot_states),
            })

        # 落盘一次起点(Phase 1 的产出，或已被上游 gate/refine 过的版本):即便第一拍就崩，
        # 这些也不会跟着丢。
        _save_checkpoint()

        beats_to_generate = [b for b in range(1, total_beats + 1) if b not in pass_beats_done]
        if pass_beats_done and sys.stdout:
            print(f"[RESUME] Skipping beats already completed before the last interruption/failure: {sorted(pass_beats_done)}")

        def _generate_single_beat_with_retries(i, contract):
            """skill 直出模式的单拍兜底：仅当批量直出没给出这一拍的 VIDEO/IMAGE 段时才走到
            这里。单独生成一次即采纳（确定性修复照常、结构校验只记录），重试只针对
            传输/代理故障或响应缺段，不再做「校验不过→带反馈重写」的自愈循环。
            Returns (vid_prompt, img_prompt, new_ledger_items, beat_succeeded)."""
            nonlocal fallback_count
            beat = contract['beat']
            is_last = contract['is_last']
            is_threshold_or_reveal = contract['is_threshold_or_reveal']
            is_pre_bridge = contract['is_pre_bridge']
            family = contract['family']
            # 第二空间的帧要用换过锚点视图的包（见 packet_for_space）；其余拍拿到的就是原包。
            beat_packet = contract.get('packet') or packet
            tbcp_ref_i = tbcp_ref if (contract['is_bridge'] or contract['is_cut']) else ''

            beat_system = self.single_beat_system_prompt(
                config, i, contract, packet, compiled_images, compiled_videos,
                scup_ref, tbcp_ref_i)
            beat_user = f"Generate prompts for Beat {i}: {beat.get('operation', '')} - {beat.get('description', '')}."

            vid_prompt = ""
            img_prompt = ""
            new_ledger_items = None

            for attempt in range(max(0, int(config.get('composeBatchRetryCount', 1))) + 1):
                request_started = pp.time.time()
                try:
                    pp._raise_if_cancelled(on_progress)
                    resp = pp._chat(config, beat_system, beat_user, temperature=0.8, timeout=90)
                    config.setdefault('_compose_request_timings', []).append({
                        'kind': 'single', 'beat': i, 'attempt': attempt + 1,
                        'started_at': request_started, 'ended_at': pp.time.time(),
                        'failure_reason': None,
                    })
                    secs = pp._extract_marked(resp, ['===VIDEO===', '===IMAGE===', '===TRACES==='])
                    v_p = secs.get('===VIDEO===', '').strip()
                    i_p = secs.get('===IMAGE===', '').strip()
                    if not (v_p and i_p):
                        if sys.stdout:
                            print(f"[DEBUG] Beat {i} attempt {attempt+1}: response missing VIDEO/IMAGE sections, retrying.")
                        continue

                    # Apply proactive fixes
                    v_p, i_p = self.apply_proactive_fixes(i, v_p, i_p, beat_packet, mode, is_last, is_threshold_or_reveal, beat=beat, config=config, family=family)
                    # 2026-07-30：声明式切入拍的 VIDEO 不再被占位声明覆盖——它和单一过门拍
                    # 一样是真实可见的跨越片段，正文一律走 LLM 稿 + 确定性修复 + 校验 + 回炉
                    # 的普通通路（占位覆盖正是「过门镜头不生成」的根因）。

                    # skill 直出模式：风格瑕疵只记录不拦截——确定性修复已经兜住会直接
                    # 破坏渲染的硬伤，剩余瑕疵交给帧渲染后的真实画面审查
                    # (prompt_pipeline.check_full_sequence_consistency)。
                    # 例外：结构性硬伤（VIDEO 无动作正文/桥接无运镜/幽灵施工）意味着
                    # i2v 将无画面可拍（静止/冻结闪切/自造空间），对该拍定向回炉一轮。
                    prev_v = compiled_videos.get(i - 1) if i > 1 else None
                    prev_i = compiled_images.get(i) if i > 1 else None
                    errs = self.validate_beat_prompts(i, v_p, i_p, beat_packet, mode, is_last, is_threshold_or_reveal, prev_v, prev_i, beat=beat, family=family, is_pre_bridge=is_pre_bridge,
                                                      is_post_reveal_cleanup=contract['is_post_reveal_cleanup'])
                    structural, style_errs = self.split_structural_video_errors(errs)
                    reworked = None
                    if structural:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 结构性硬伤，定向回炉一轮: {structural}")
                        v_p, reworked = self.rework_structural_video_beat(config, i, v_p, structural, packet, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 回炉{'成功，已采用重写稿' if reworked else '未通过，保留原稿（仅留痕）'}")
                    # TRACES 解析提前到检查链最前面：check_image_realizes_traces 需要用这拍
                    # 自己声明的 new_ledger_items 反查 IMAGE 正文，必须在那道检查跑之前拿到。
                    parsed_traces = None
                    traces_str = secs.get('===TRACES===', '').strip()
                    if traces_str:
                        try:
                            traces_clean = pp._strip_code_fences(traces_str).strip()
                            parsed = json.loads(traces_clean)
                            if isinstance(parsed, list):
                                parsed_traces = [
                                    {
                                        "name": str(item.get("name")),
                                        "material_color": str(item.get("material_color", "unknown")),
                                        "initial_state": str(item.get("initial_state", "installed")),
                                        "grid": str(item.get("grid", "Grid B2")),
                                        "z_depth_scale": str(item.get("z_depth_scale", "50%"))
                                    }
                                    for item in parsed if isinstance(item, dict) and "name" in item
                                ]
                        except Exception as e:
                            if sys.stdout:
                                print(f"[DEBUG] Failed to parse prompt-embedded TRACES JSON: {e}")
                    image_similar, style_errs = pp.split_image_similarity_errors(style_errs)
                    image_reworked = None
                    if image_similar:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} IMAGE 相似度瑕疵，定向回炉一轮: {image_similar}")
                        i_p, image_reworked = pp.rework_similar_image_beat(config, i, i_p, image_similar, packet, prev_image=prev_i, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} IMAGE 回炉{'成功，已采用重写稿' if image_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + image_similar
                    milestone_video_errs = pp.check_milestone_video_prompt(v_p, beat)
                    milestone_image_errs = pp.check_milestone_image_prompt(i_p, beat)
                    if milestone_video_errs or milestone_image_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 显著里程碑骨架缺失，成对回炉一轮: "
                                  f"VIDEO={milestone_video_errs}; IMAGE={milestone_image_errs}")
                        v_p, i_p, milestone_reworked = pp.rework_milestone_prompt_pair(
                            config, i, v_p, i_p, beat, milestone_video_errs, milestone_image_errs)
                        structural = structural + milestone_video_errs
                        style_errs = style_errs + milestone_image_errs
                        reworked = milestone_reworked if reworked is None else (reworked or milestone_reworked)
                        image_reworked = milestone_reworked if image_reworked is None else (image_reworked or milestone_reworked)
                    content_errs = pp.check_image_realizes_traces(i_p, parsed_traces)
                    if content_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 图文内容脱节，定向回炉一轮: {content_errs}")
                        i_p, content_reworked = pp.rework_missing_content_image_beat(config, i, i_p, parsed_traces, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 图文内容回炉{'成功，已采用重写稿' if content_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + content_errs
                        image_reworked = content_reworked if image_reworked is None else (image_reworked or content_reworked)
                    # 卡片工序（节拍简介）在成片里的收口：这拍认领的工序必须真的写进 IMAGE
                    # 回炉**之前**的缺失编号要先留一份，否则下面记总账时区分不出
                    # "一次过"和"回炉后通过"（见 pp.record_outline_delivery）
                    outline_missing_before = pp.outline_missing_indices(i_p, beat)
                    outline_errs = pp.check_outline_delivery_realized(i_p, beat)
                    if outline_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 卡片工序未交付，定向回炉一轮: {outline_errs}")
                        i_p, outline_reworked = pp.rework_missing_outline_delivery_beat(config, i, i_p, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 卡片工序回炉{'成功，已采用重写稿' if outline_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + outline_errs
                        image_reworked = outline_reworked if image_reworked is None else (image_reworked or outline_reworked)
                    wording_errs = pp.check_stage_scope_wording(i_p, contract['stage_scope'])
                    if wording_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} STAGE SCOPE 措辞瑕疵，定向回炉一轮: {wording_errs}")
                        i_p, wording_reworked = pp.rework_stage_scope_wording_beat(
                            config, i, i_p, wording_errs, contract['stage_scope'], beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} STAGE SCOPE 回炉{'成功，已采用重写稿' if wording_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + wording_errs
                        # 相似度回炉和 stage_scope 措辞回炉都改写同一个 IMAGE 正文，合并成
                        # 一个 image_reworked 信号喂给 record_beat_audit——否则这里两个独立
                        # 局部变量互相覆盖，措辞回炉真实成功了审核报告也会显示"仅留痕"。
                        image_reworked = wording_reworked if image_reworked is None else (image_reworked or wording_reworked)
                    anchor_errs = pp.check_signature_anchor_realized(i_p, beat)
                    if anchor_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 招牌反差点缺失，定向回炉一轮: {anchor_errs}")
                        i_p, anchor_reworked = pp.rework_missing_anchor_beat(config, i, i_p, anchor_errs, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 招牌反差点回炉{'成功，已采用重写稿' if anchor_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + anchor_errs
                        image_reworked = anchor_reworked if image_reworked is None else (image_reworked or anchor_reworked)
                    placeholder_errs = pp.check_image_decay_placeholder(i_p)
                    if placeholder_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} sterile占位句，定向回炉一轮: {placeholder_errs}")
                        i_p, placeholder_reworked = pp.rework_decay_placeholder_beat(config, i, i_p, placeholder_errs, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} sterile占位句回炉{'成功，已采用重写稿' if placeholder_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + placeholder_errs
                        image_reworked = placeholder_reworked if image_reworked is None else (image_reworked or placeholder_reworked)
                    decay_errs = pp.check_first_interior_reveal_decay(i_p, contract['is_first_interior_reveal'])
                    if decay_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 首现衰败措辞缺失，定向回炉一轮: {decay_errs}")
                        i_p, decay_reworked = pp.rework_first_interior_reveal_decay_beat(config, i, i_p, decay_errs, beat=beat)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 首现衰败措辞回炉{'成功，已采用重写稿' if decay_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + decay_errs
                        image_reworked = decay_reworked if image_reworked is None else (image_reworked or decay_reworked)
                    envelope_errs = pp.check_envelope_seal_regression(i_p, i, beat_ladder, family=family)
                    if envelope_errs:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 包络体状态倒退（已封构件又写成敞开），定向回炉一轮: {envelope_errs}")
                        i_p, envelope_reworked = pp.rework_envelope_seal_regression_beat(
                            config, i, i_p, envelope_errs, beat=beat, beat_ladder=beat_ladder)
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 包络体状态倒退回炉{'成功，已采用重写稿' if envelope_reworked else '未通过，保留原稿（仅留痕）'}")
                        style_errs = style_errs + envelope_errs
                        image_reworked = envelope_reworked if image_reworked is None else (image_reworked or envelope_reworked)
                    # 回读校验：修没修以最终文本为准，不以每道回炉自报的布尔为准
                    residual = pp.reverify_beat_repairs(
                        i, v_p, i_p, beat, parsed_traces=parsed_traces,
                        stage_scope=contract['stage_scope'],
                        is_first_interior_reveal=contract['is_first_interior_reveal'],
                        beat_ladder=beat_ladder, family=family)
                    remaining_milestone_errors = (
                        pp.check_milestone_video_prompt(v_p, beat) + pp.check_milestone_image_prompt(i_p, beat))
                    # 终帧倒退是整条序列最贵的失败，和里程碑硬门同级：重试整拍而不是留痕
                    payoff_blocking = pp.payoff_blocking_residual(residual, is_last)
                    # 硬门只有两类：里程碑骨架残缺 + 终帧倒退。其余回读残留一律留痕放行
                    # （下面的 record_beat_audit 会记成 rework_failed/needs_attention）。
                    # 2026-08-08：这里曾把整份 residual 也算进硬门，等于「回炉没修干净就
                    # 整拍重来」——每拍白烧一次整拍 LLM 调用，10 拍的单子撞死 720s 硬时限
                    # （COMPOSE_TIMEOUT），日志里还会打出「重试整拍: []」这种空原因。
                    hard_gate_errors = list(dict.fromkeys(
                        remaining_milestone_errors + payoff_blocking))
                    if hard_gate_errors:
                        if sys.stdout:
                            print(f"[DIRECT] Beat {i} 硬门仍未通过，重试整拍: {hard_gate_errors}")
                        continue
                    if style_errs and sys.stdout:
                        print(f"[DIRECT] Beat {i} 校验有瑕疵（直出模式仅记录，不重写）: {style_errs}")
                    if residual and sys.stdout:
                        print(f"[DIRECT] Beat {i} 回读校验仍有残留（回炉未真正生效）: {residual}")
                    pp.record_beat_audit(config, i, structural, style_errs, reworked, image_reworked,
                                         milestone_name=beat.get('milestone_name'),
                                         residual=residual)
                    # 同一批结论按**卡片工序**再记一份（_beat_audit 是按拍组织的，
                    # 回答不了"卡片上第 3 条最后成没成"）——见 pp.record_outline_delivery
                    pp.record_outline_delivery(config, i, i_p, beat,
                                               missing_before=outline_missing_before)

                    vid_prompt = v_p
                    img_prompt = i_p
                    new_ledger_items = parsed_traces
                    break
                except pp.GenerationCancelled:
                    raise
                except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Beat {i} hit a code-level error ({type(e).__name__}: {e}); aborting to avoid "
                        f"shipping placeholder output. Fix the bug rather than retrying."
                    ) from e
                except Exception as e:
                    config.setdefault('_compose_request_timings', []).append({
                        'kind': 'single', 'beat': i, 'attempt': attempt + 1,
                        'started_at': request_started, 'ended_at': pp.time.time(),
                        'failure_reason': str(e),
                    })
                    if sys.stdout:
                        print(f"[DEBUG] Beat {i} attempt {attempt+1} error: {e}")

            beat_succeeded = bool(vid_prompt and img_prompt)
            if not beat_succeeded:
                slot_states[str(i)] = 'failed'
                _save_checkpoint()
                if not allow_placeholders:
                    raise pp.ComposeFailure(
                        f"Beat {i} failed prompt generation; validated checkpoint retained. "
                        "Production mode forbids placeholder IMAGE/VIDEO prompts.",
                        'BEAT_GENERATION_FAILED')
                fallback_count += 1
                desc = beat.get('description', 'performing restoration work').strip().rstrip('.')

                if contract['is_cut']:
                    # 声明式切入拍的兜底稿：与 bridge 兜底同款的真实跨越镜头，只多一步
                    # 「封闭的门在片段里被推开」——绝不再回落成占位声明。
                    vid_prompt = (
                        f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                        f"The closed entry seen in the first frame is pushed open on camera, revealing the dark interior beyond, and the camera pushes forward in one continuous coaxial move straight through the opening, the door frame sliding fully out of frame, settling fully inside by the last frame with the threshold completely behind the camera. Exposure and white balance roll from exterior daylight to the interior's dimmer tone across the clip. "
                        f"The interior it enters is an untouched ruin at every moment of the clip — debris lying where it fell, dirt drifts, stained and corroded surfaces — and nothing is cleaned, cleared, or repaired during the crossing; the frame stays sterile of workers and no tools, ladders, or staged materials appear at any point. One unbroken take at a steady speed: no cut, no fade, no dissolve, no speed ramp."
                    )
                elif contract['is_bridge']:
                    _turn_dir = str(beat.get('turn_direction') or '').strip().lower()
                    _turn_txt = (
                        f", then turns with one smooth pan to the {_turn_dir} to align with the interior's long axis"
                        if _turn_dir in ('left', 'right') else ""
                    )
                    vid_prompt = (
                    f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                    f"The camera pushes forward in one continuous coaxial move through the open threshold, the door frame sliding fully out of frame{_turn_txt}, and settles fully inside by the last frame. "
                    f"The interior it enters is an untouched ruin at every moment of the clip — debris lying where it fell, dirt drifts, stained and corroded surfaces — and nothing is cleaned, cleared, or repaired during the crossing; no tools, ladders, or staged materials appear at any point. One unbroken take at a steady speed: no cut, no fade, no dissolve, no speed ramp."
                )
                elif not is_threshold_or_reveal:
                    _package = ', '.join(beat.get('package_operations') or [beat.get('operation', 'construction')])
                    _traces = ', '.join(beat.get('persistent_traces') or ['contact marks', 'material dust'])
                    vid_prompt = (
                        f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                        f"This is a continuous construction time-lapse, not real-time footage, creating the {beat.get('milestone_name')} milestone through the cohesive {_package} package. At t=0s the visible state is {beat.get('before_state')}; the same lone worker is already positioned at the active work face, makes the first effective tool contact immediately, and repeatedly performs the work cycle along a visible movement path. The primary progression shows {beat.get('primary_progress')}; simultaneously the secondary progression shows {beat.get('secondary_progress')}. By the final moment {beat.get('after_state')} across {beat.get('completion_extent')}, while {_traces} remain and the worker continues the visible operation through the final frame."
                    )
                else:
                    vid_prompt = (
                    f"Use the provided first frame and last frame as exact composition anchors. Use IMAGE {i} as the actual first-frame image and IMAGE {i+1} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                    f"The video captures the physical process of: {desc}. A worker is visible performing the manual installation and assembly steps, slowly building and placing elements. The background and camera position remain locked."
                )
                if not is_threshold_or_reveal:
                    vid_prompt += " continuous construction time-lapse, not real-time footage."
                vid_prompt = self.finalize_fallback_video(vid_prompt, contract)

                _attitude = ("horizon line remains level" if family == 'exterior'
                             else "camera pitch locked level; the central vanishing axis stays centered")
                if not is_threshold_or_reveal:
                    _traces = ', '.join(beat.get('persistent_traces') or ['contact marks', 'material dust'])
                    img_prompt = (
                        f"A static ultra-wide 14mm tripod shot at 1.6m height; {_attitude}. The scene is the "
                        f"{beat.get('milestone_name')} anchor, with {beat.get('after_state')} across "
                        f"{beat.get('completion_extent')}. {_traces} remain visibly embedded in the completed "
                        f"work. {beat.get('preserve_state')}. The frame contains only static surfaces, materials, "
                        f"and causal traces."
                    )
                else:
                    img_prompt = (
                        f"A static ultra-wide 14mm tripod shot at 1.6m height: clean completed state after the step of {desc} of {theme}; "
                        f"{_attitude}; no workers are present in this clean frame. The newly completed features are visible and integrated into the scene."
                    )
                if is_last:
                    img_prompt += " Polished floor displays blurred diffused reflections."

            return vid_prompt, img_prompt, new_ledger_items, beat_succeeded

        # Batched first pass: precompute every pending beat's deterministic contract, then
        # generate ALL of them in ONE _chat call (shared reference docs/rules/packet sent
        # once instead of once-per-beat — see _batch_shared_system_prompt). This is the
        # dominant cost saver versus the old one-call-per-beat loop; only whichever beats
        # this batched pass doesn't produce validly for fall back to the (unchanged)
        # single-beat retry loop above, which is the uncommon case.
        contracts = {i: pp._beat_contract(i, total_beats, beat_ladder, mode, packet, templates_raw,
                                          parsed_brief=parsed_brief) for i in beats_to_generate}
        batch_secs = {}
        tbcp_ref = ''
        if beats_to_generate:
            # Keep model context bounded. Beats omitted from this first rolling window follow
            # the existing per-beat path below and are checkpointed independently.
            batch_size = max(1, int(config.get('composeBatchSize', 3)))
            batch_beats = beats_to_generate[:batch_size]
            if any(contracts[i]['is_bridge'] or contracts[i]['is_cut'] for i in batch_beats):
                tbcp_ref = pp.load_reference_file('threshold-bridge-consistency-protocol.md',
                                                  _profile)
            batch_system = self.batch_system_prompt(config, packet, scup_ref, tbcp_ref)
            first_anchor_image = compiled_images.get(batch_beats[0], '')
            batch_user = pp._build_batch_user_message(batch_beats, contracts, first_anchor_image)
            markers = []
            for i in batch_beats:
                markers += [f'===BEAT {i} VIDEO===', f'===BEAT {i} IMAGE===', f'===BEAT {i} TRACES===']

            if on_progress:
                on_progress('batch_generating', {
                    'batch_index': 1, 'batch_total': max(1, (len(beats_to_generate) + batch_size - 1) // batch_size),
                    'beat_start': batch_beats[0], 'beat_end': batch_beats[-1],
                    'attempt': 1, 'attempt_total': 2,
                    'elapsed_seconds': 0,
                    'deadline_remaining_seconds': int(config.get('composeRequestTimeoutSeconds', 120)),
                })
            if sys.stdout:
                print(f"[DEBUG] Step 5: Batch-composing {len(beats_to_generate)} beat(s) of {total_beats} in one call: {beats_to_generate}...")
            pp._raise_if_cancelled(on_progress)
            def _request_batch():
                retry_count = max(0, int(config.get('composeBatchRetryCount', 1)))
                timeout_seconds = int(config.get('composeRequestTimeoutSeconds', 120))
                last_error = None
                for attempt in range(retry_count + 1):
                    started = pp.time.time()
                    try:
                        pp._raise_if_cancelled(on_progress)
                        response = pp._chat(
                            config, batch_system, batch_user, temperature=0.8,
                            timeout=timeout_seconds)
                        config.setdefault('_compose_request_timings', []).append({
                            'kind': 'batch', 'beats': list(batch_beats), 'attempt': attempt + 1,
                            'started_at': started, 'ended_at': pp.time.time(),
                            'failure_reason': None,
                        })
                        if on_progress:
                            on_progress('batch_generated', {
                                'batch_index': 1,
                                'batch_total': max(1, (len(beats_to_generate) + batch_size - 1) // batch_size),
                                'beat_start': batch_beats[0], 'beat_end': batch_beats[-1],
                                'attempt': attempt + 1, 'attempt_total': retry_count + 1,
                                'elapsed_seconds': round(pp.time.time() - started, 2),
                                'deadline_remaining_seconds': max(
                                    0, round(timeout_seconds - (pp.time.time() - started), 2)),
                            })
                        return response
                    except pp.GenerationCancelled:
                        raise
                    except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError):
                        raise
                    except Exception as exc:
                        last_error = exc
                        config.setdefault('_compose_request_timings', []).append({
                            'kind': 'batch', 'beats': list(batch_beats), 'attempt': attempt + 1,
                            'started_at': started, 'ended_at': pp.time.time(),
                            'failure_reason': str(exc),
                        })
                        if on_progress:
                            on_progress('batch_retry' if attempt < retry_count else 'batch_failed', {
                                'batch_index': 1,
                                'batch_total': max(1, (len(beats_to_generate) + batch_size - 1) // batch_size),
                                'beat_start': batch_beats[0], 'beat_end': batch_beats[-1],
                                'attempt': attempt + 1, 'attempt_total': retry_count + 1,
                                'elapsed_seconds': round(pp.time.time() - started, 2),
                                'deadline_remaining_seconds': 0,
                                'failure_reason': str(exc),
                            })
                raise last_error
            # Same fail-fast-on-code-bugs philosophy as the single-beat retry loop: a
            # NameError/AttributeError/etc from this call (including inside _chat itself)
            # means real code is broken, not that the LLM/proxy hiccuped — abort rather than
            # mask it behind "fall back to individual retries for everyone", which would
            # just re-trigger the same bug per beat. Everything else (timeouts, connection
            # errors, malformed API responses) IS treated as a transient/flaky-proxy issue
            # and falls back to per-beat retry below, exactly what that path exists for.
            try:
                resp = _request_batch()
                batch_secs = pp._extract_marked(resp, markers)
            except pp.GenerationCancelled:
                raise
            except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError) as e:
                raise RuntimeError(
                    f"Batched beat generation hit a code-level error ({type(e).__name__}: {e}); aborting to "
                    f"avoid shipping placeholder output. Fix the bug rather than retrying."
                ) from e
            except Exception as e:
                if sys.stdout:
                    print(f"[DEBUG] Batch beat generation call failed ({e}); falling back to individual retries for all {len(beats_to_generate)} beat(s).")

        # One beat at a time: try to resolve it from the batch response first, otherwise
        # fall back to an individual retry — and commit (compiled_images/videos, beat_ready,
        # object ledger, checkpoint) immediately either way. Committing per-beat as each one
        # resolves (rather than only after the whole batch has been parsed) means a
        # code-level bug hit while processing a LATER beat in the same batch still leaves
        # every EARLIER beat's already-valid result safely checkpointed, matching the
        # granularity the resume mechanism has always guaranteed.
        for i in beats_to_generate:
            if on_progress:
                on_progress('batch', {'current': i, 'total': total_beats})

            contract = contracts[i]
            vid_prompt = img_prompt = ''
            new_ledger_items = None
            beat_succeeded = False

            v_p = batch_secs.get(f'===BEAT {i} VIDEO===', '').strip()
            i_p = batch_secs.get(f'===BEAT {i} IMAGE===', '').strip()
            if v_p and i_p:
                try:
                    # 第二空间的帧用换过锚点视图的包（见 packet_for_space）
                    _pkt = contract.get('packet') or packet
                    v_p, i_p = self.apply_proactive_fixes(
                        i, v_p, i_p, _pkt, mode, contract['is_last'], contract['is_threshold_or_reveal'],
                        beat=contract['beat'], config=config, family=contract['family'])
                    # 2026-07-30：声明式切入拍的 VIDEO 不再被占位声明覆盖（同单拍通路的注释）。
                    prev_v = compiled_videos.get(i - 1) if i > 1 else None
                    prev_i = compiled_images.get(i) if i > 1 else None
                    errs = self.validate_beat_prompts(
                        i, v_p, i_p, _pkt, mode, contract['is_last'], contract['is_threshold_or_reveal'],
                        prev_v, prev_i, beat=contract['beat'], family=contract['family'],
                        is_pre_bridge=contract['is_pre_bridge'],
                        is_post_reveal_cleanup=contract['is_post_reveal_cleanup'])
                except (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Beat {i} hit a code-level error ({type(e).__name__}: {e}) while processing the "
                        f"batched generation result; aborting to avoid shipping placeholder output. Fix "
                        f"the bug rather than retrying."
                    ) from e
                # TRACES 解析提前到检查链最前面：check_image_realizes_traces 需要用这拍自己
                # 声明的 new_ledger_items 反查 IMAGE 正文，必须在那道检查跑之前就拿到。
                parsed_traces = None
                traces_str = batch_secs.get(f'===BEAT {i} TRACES===', '').strip()
                if traces_str:
                    try:
                        traces_clean = pp._strip_code_fences(traces_str).strip()
                        parsed = json.loads(traces_clean)
                        if isinstance(parsed, list):
                            parsed_traces = [
                                {
                                    "name": str(item.get("name")),
                                    "material_color": str(item.get("material_color", "unknown")),
                                    "initial_state": str(item.get("initial_state", "installed")),
                                    "grid": str(item.get("grid", "Grid B2")),
                                    "z_depth_scale": str(item.get("z_depth_scale", "50%")),
                                }
                                for item in parsed if isinstance(item, dict) and "name" in item
                            ]
                    except Exception as e:
                        if sys.stdout:
                            print(f"[DEBUG] Failed to parse prompt-embedded TRACES JSON for beat {i}: {e}")
                # skill 直出模式：批量直出的结果只要有 VIDEO/IMAGE 两段就直接采纳——风格
                # 瑕疵只记录不打回（确定性修复已兜住渲染硬伤，剩余瑕疵交给帧渲染后对真实
                # 画面的审查）。例外：结构性硬伤（VIDEO 无动作正文/桥接无运镜/幽灵施工）
                # 会让 i2v 无画面可拍，对命中的拍定向回炉一轮（只重写 VIDEO，失败保留原稿）。
                structural, style_errs = self.split_structural_video_errors(errs)
                reworked = None
                if structural:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 结构性硬伤，定向回炉一轮: {structural}")
                    v_p, reworked = self.rework_structural_video_beat(config, i, v_p, structural, packet, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 回炉{'成功，已采用重写稿' if reworked else '未通过，保留原稿（仅留痕）'}")
                image_similar, style_errs = pp.split_image_similarity_errors(style_errs)
                image_reworked = None
                if image_similar:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} IMAGE 相似度瑕疵，定向回炉一轮: {image_similar}")
                    i_p, image_reworked = pp.rework_similar_image_beat(config, i, i_p, image_similar, packet, prev_image=prev_i, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} IMAGE 回炉{'成功，已采用重写稿' if image_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + image_similar
                milestone_video_errs = pp.check_milestone_video_prompt(v_p, contract['beat'])
                milestone_image_errs = pp.check_milestone_image_prompt(i_p, contract['beat'])
                if milestone_video_errs or milestone_image_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 显著里程碑骨架缺失，成对回炉一轮: "
                              f"VIDEO={milestone_video_errs}; IMAGE={milestone_image_errs}")
                    v_p, i_p, milestone_reworked = pp.rework_milestone_prompt_pair(
                        config, i, v_p, i_p, contract['beat'], milestone_video_errs, milestone_image_errs)
                    structural = structural + milestone_video_errs
                    style_errs = style_errs + milestone_image_errs
                    reworked = milestone_reworked if reworked is None else (reworked or milestone_reworked)
                    image_reworked = milestone_reworked if image_reworked is None else (image_reworked or milestone_reworked)
                content_errs = pp.check_image_realizes_traces(i_p, parsed_traces)
                if content_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 图文内容脱节，定向回炉一轮: {content_errs}")
                    i_p, content_reworked = pp.rework_missing_content_image_beat(config, i, i_p, parsed_traces, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 图文内容回炉{'成功，已采用重写稿' if content_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + content_errs
                    image_reworked = content_reworked if image_reworked is None else (image_reworked or content_reworked)
                # 卡片工序（节拍简介）在成片里的收口：这拍认领的工序必须真的写进 IMAGE
                # 回炉前的缺失编号先留一份（见上面单拍路径的同款说明）
                outline_missing_before = pp.outline_missing_indices(i_p, contract['beat'])
                outline_errs = pp.check_outline_delivery_realized(i_p, contract['beat'])
                if outline_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 卡片工序未交付，定向回炉一轮: {outline_errs}")
                    i_p, outline_reworked = pp.rework_missing_outline_delivery_beat(config, i, i_p, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 卡片工序回炉{'成功，已采用重写稿' if outline_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + outline_errs
                    image_reworked = outline_reworked if image_reworked is None else (image_reworked or outline_reworked)
                wording_errs = pp.check_stage_scope_wording(i_p, contract['stage_scope'])
                if wording_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} STAGE SCOPE 措辞瑕疵，定向回炉一轮: {wording_errs}")
                    i_p, wording_reworked = pp.rework_stage_scope_wording_beat(
                        config, i, i_p, wording_errs, contract['stage_scope'], beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} STAGE SCOPE 回炉{'成功，已采用重写稿' if wording_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + wording_errs
                    # 相似度回炉和 stage_scope 措辞回炉都改写同一个 IMAGE 正文，合并成一个
                    # image_reworked 信号喂给 record_beat_audit——否则措辞回炉真实成功了
                    # 审核报告也会显示"仅留痕"。
                    image_reworked = wording_reworked if image_reworked is None else (image_reworked or wording_reworked)
                anchor_errs = pp.check_signature_anchor_realized(i_p, contract['beat'])
                if anchor_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 招牌反差点缺失，定向回炉一轮: {anchor_errs}")
                    i_p, anchor_reworked = pp.rework_missing_anchor_beat(config, i, i_p, anchor_errs, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 招牌反差点回炉{'成功，已采用重写稿' if anchor_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + anchor_errs
                    image_reworked = anchor_reworked if image_reworked is None else (image_reworked or anchor_reworked)
                placeholder_errs = pp.check_image_decay_placeholder(i_p)
                if placeholder_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} sterile占位句，定向回炉一轮: {placeholder_errs}")
                    i_p, placeholder_reworked = pp.rework_decay_placeholder_beat(config, i, i_p, placeholder_errs, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} sterile占位句回炉{'成功，已采用重写稿' if placeholder_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + placeholder_errs
                    image_reworked = placeholder_reworked if image_reworked is None else (image_reworked or placeholder_reworked)
                decay_errs = pp.check_first_interior_reveal_decay(i_p, contract['is_first_interior_reveal'])
                if decay_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 首现衰败措辞缺失，定向回炉一轮: {decay_errs}")
                    i_p, decay_reworked = pp.rework_first_interior_reveal_decay_beat(config, i, i_p, decay_errs, beat=contract['beat'])
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 首现衰败措辞回炉{'成功，已采用重写稿' if decay_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + decay_errs
                    image_reworked = decay_reworked if image_reworked is None else (image_reworked or decay_reworked)
                envelope_errs = pp.check_envelope_seal_regression(i_p, i, beat_ladder, family=contract['family'])
                if envelope_errs:
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 包络体状态倒退（已封构件又写成敞开），定向回炉一轮: {envelope_errs}")
                    i_p, envelope_reworked = pp.rework_envelope_seal_regression_beat(
                        config, i, i_p, envelope_errs, beat=contract['beat'], beat_ladder=beat_ladder)
                    if sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 包络体状态倒退回炉{'成功，已采用重写稿' if envelope_reworked else '未通过，保留原稿（仅留痕）'}")
                    style_errs = style_errs + envelope_errs
                    image_reworked = envelope_reworked if image_reworked is None else (image_reworked or envelope_reworked)
                # 回读校验：修没修以最终文本为准（见 pp.reverify_beat_repairs）
                residual = pp.reverify_beat_repairs(
                    i, v_p, i_p, contract['beat'], parsed_traces=parsed_traces,
                    stage_scope=contract['stage_scope'],
                    is_first_interior_reveal=contract['is_first_interior_reveal'],
                    beat_ladder=beat_ladder, family=contract['family'])
                remaining_milestone_errors = (
                    pp.check_milestone_video_prompt(v_p, contract['beat'])
                    + pp.check_milestone_image_prompt(i_p, contract['beat']))
                payoff_blocking = pp.payoff_blocking_residual(residual, contract['is_last'])
                if style_errs and sys.stdout:
                    print(f"[DIRECT] Batch beat {i} 校验有瑕疵（直出模式仅记录，不重写）: {style_errs}")
                # 同上：批量稿的硬门也只有里程碑骨架 + 终帧倒退两类。把整份 residual
                # 算进来会让批量结果几乎全军覆没、每拍都退回单拍通路重做（实测 10/10 拍
                # 都走了「Individually composing」），是撞硬时限的主要成本来源。
                hard_gate_errors = list(dict.fromkeys(
                    remaining_milestone_errors + payoff_blocking))
                if not hard_gate_errors:
                    if residual and sys.stdout:
                        print(f"[DIRECT] Batch beat {i} 回读校验仍有残留（回炉未真正生效）: {residual}")
                    pp.record_beat_audit(config, i, structural, style_errs, reworked, image_reworked,
                                         milestone_name=contract['beat'].get('milestone_name'),
                                         residual=residual)
                    pp.record_outline_delivery(config, i, i_p, contract['beat'],
                                               missing_before=outline_missing_before)
                    vid_prompt, img_prompt, beat_succeeded = v_p, i_p, True
                    new_ledger_items = parsed_traces
                elif sys.stdout:
                    print(f"[DIRECT] Batch beat {i} 硬门未通过，转入单拍重试: {hard_gate_errors}")

            if not beat_succeeded:
                if sys.stdout:
                    print(f"[DEBUG] Step 5: Individually composing Beat {i} of {total_beats} (batch response missing this beat's sections)...")
                vid_prompt, img_prompt, new_ledger_items, beat_succeeded = _generate_single_beat_with_retries(i, contract)

            compiled_images[i + 1] = img_prompt
            compiled_videos[i] = vid_prompt

            if on_progress:
                _, _, partial_block = pp._build_partial_prompt_block(
                    compiled_images, compiled_videos, beat_ladder,
                    parsed_brief.get('pacing_skeleton'),
                    parsed_brief=parsed_brief)
                on_progress('beat_ready', {
                    'index': i,
                    'total': total_beats,
                    'prompt_block': partial_block,
                    'is_revision': False,
                })

            # Dynamically update the object ledger with new persistent traces/features.
            # skill 直出模式：只消费生成响应里自带的 ===TRACES=== 段；缺失时不再额外调
            # extract_persistent_traces_to_ledger 的 LLM 兜底（台账更新是 best-effort）。
            if vid_prompt and img_prompt:
                if new_ledger_items:
                    if 'object_ledger' not in packet or not isinstance(packet['object_ledger'], list):
                        packet['object_ledger'] = []
                    existing_names = {x['name'].lower() for x in packet['object_ledger'] if isinstance(x, dict) and 'name' in x}
                    added_count = 0
                    for item in new_ledger_items:
                        if item['name'].lower() not in existing_names:
                            packet['object_ledger'].append(item)
                            existing_names.add(item['name'].lower())
                            added_count += 1
                    if sys.stdout:
                        print(f"[DEBUG] Dynamic Ledger: Added {added_count} new items (deduplicated). Total objects: {len(packet['object_ledger'])}")

            # 断点续传:只把真正成功生成(非占位符兜底)的拍标记为已完成——兜底拍在续传时
            # 仍需重新真实生成，否则一次 LLM 抖动就会把某一拍永久锁死成占位符文本。
            if beat_succeeded:
                slot_states[str(i)] = 'validated'
                pass_beats_done.add(i)
            else:
                slot_states[str(i)] = 'degraded'
            _save_checkpoint()

        # Quality gate
        fallback_limit = 0 if strict_v2 and not diagnostic_mode else total_beats
        if fallback_count > fallback_limit:
            raise pp.ComposeFailure(
                f"{fallback_count} of {total_beats} beats fell back to placeholder prompts "
                f"(limit {fallback_limit}); diagnostic output cannot be shipped.",
                'PLACEHOLDER_PROMPTS_PRESENT',
            )
        if isinstance(config, dict):
            config['_compose_degraded'] = bool(fallback_count)
            config['_compose_placeholder_count'] = fallback_count
            config['_compose_diagnostic'] = diagnostic_mode

        # 收尾步骤：追加一条"英雄展示视频"提示词（视频 total_beats+1 [HERO]），
        # 唯一来源锚点是帧序列最后一张整体完工图。锦上添花，不设硬门禁——失败/跳过
        # 都不影响整单合成结果，只是没有这一条额外视频。
        # 2026-07-31 起 _HERO_SHOWCASE_ENABLED 默认为 False（收尾不再放两条完工镜头，
        # 见 docs/pacing_rhythm_balance_plan.md §7），整段连同进度提示一起跳过。
        hero_video_text = ''
        if pp._HERO_SHOWCASE_ENABLED:
            if on_progress:
                on_progress('outline', '正在生成英雄展示视频提示词（完工全景 · 手持推镜/摇镜）...')
            try:
                pp._raise_if_cancelled(on_progress)
                hero_video_text = pp._compose_hero_showcase_video(config, state, on_progress=on_progress)
            except pp.GenerationCancelled:
                raise
            except Exception as e:
                if sys.stdout:
                    print(f"[HERO] 英雄展示视频提示词生成异常，跳过该附加步骤: {e}")
                hero_video_text = ''
        if hero_video_text:
            compiled_videos[total_beats + 1] = hero_video_text

        # Convert compiled_images and compiled_videos to dicts with meta before formatting.
        # Shared with the per-beat progressive-reveal on_progress('beat_ready', ...) events
        # via _build_partial_prompt_block, so live per-beat snapshots and the final assembly
        # never diverge in BRIDGE-tagging.
        formatted_images, formatted_videos, reassembled_prompts_block = pp._build_partial_prompt_block(
            compiled_images, compiled_videos, beat_ladder, parsed_brief.get('pacing_skeleton'), parsed_brief
        )
        if (total_beats + 1) in formatted_videos:
            # _build_partial_prompt_block only tags BRIDGE/CUT beats from beat_ladder — the
            # hero slot sits one past the last real beat, so it always falls through that
            # loop untagged. Stamp it here instead of teaching the shared helper about a
            # slot that only exists in this one caller.
            formatted_videos[total_beats + 1]['meta'] = 'HERO'
            reassembled_prompts_block = pp._format_prompt_block(formatted_images, formatted_videos)

        # 卡片工序交付总账：规划期的认领结果（_outline_contract）与合成期的逐条交付
        # 结果（_outline_prompt_audit）在这里按**工序**汇成一张表，run 结束由 server.py
        # 汇入 result。纯留痕重排索引，不参与任何判定（见 pp.build_outline_delivery_ledger）。
        pp.stash_outline_delivery_ledger(config, beat_ladder,
                                         skeleton=parsed_brief.get('pacing_skeleton'))

        skipped = config.get('_skipped_checks', 0) if isinstance(config, dict) else 0
        skipped_str = f"\n\n[WARNING] 本次跳过了 {skipped} 项校验。" if skipped > 0 else ""

        # Safety net: earlier free-form LLM generation steps can silently truncate or drop
        # slots. compiled_images/compiled_videos are the verified-complete source of truth
        # (every beat unconditionally writes both an image and video entry), so re-check the
        # final block against them and rebuild from source if anything went missing rather
        # than shipping a partial prompt set.
        check_images, check_videos = pp._parse_prompt_slots(reassembled_prompts_block)
        missing_images, missing_videos = pp._missing_prompt_slots(
            check_images, check_videos, (1, total_beats + 1), (1, total_beats)
        )
        if missing_images or missing_videos:
            if sys.stdout:
                print(f"[WARNING] Final prompt block was missing slots (images={missing_images}, videos={missing_videos}); "
                      f"rebuilding from the verified-complete compiled beat data.")
            reassembled_prompts_block = pp._format_prompt_block(formatted_images, formatted_videos)

        final_output = f"""===TITLE===
{title}
===THEME===
{parsed_brief.get('theme', theme)}
===PROMPTS===
{reassembled_prompts_block}
===AUDIT===
skill 直出模式：文本阶段无审查、无重写，批量直出+确定性修复一次成型；一致性审查在帧渲染完成后对着真实画面进行。{skipped_str}"""

        # 整单成功交付，断点续传存档功成身退——否则下次同一份 dimensions 的全新一键合成
        # 会被误当成续传，平白复用一份已经用过的旧输出。
        pp.clear_compose_checkpoint(brief_fingerprint)

        return final_output
