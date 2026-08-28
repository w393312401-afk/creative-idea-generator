"""爆款复刻 1-Pass 极速直通合成器 (One-Pass Fast Composer)

跳过通用灵感生成管线中的 8~15 次串行 API 握手与 4 轮阶梯重排，
直接以用户已核验锁定的 `timelapse_beats.json` 为唯一真源，
通过单次端到端大模型推理生成符合工业级规范的全量提示词包（IMAGE 1..N+1 与 VIDEO 1..N），
将合成耗时从 3~5 分钟压缩至 15~30 秒。
"""

import json
import os
import re
import sys
import time

import prompt_pipeline as pp
from prompt_pipeline import reverse


def _clean_str(val, default=''):
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def build_fast_composer_system_prompt(banned_elements=None, scene_constants=None, is_miniature=False, cast_identity=None):
    """构建固化了全套工业级规则（AGENTS.md、DLSP 5层景深、ASMR 60%、4-Zone守恒等）的系统提示词。"""
    banned_list = [str(x).strip() for x in (banned_elements or []) if str(x).strip()]
    banned_str = f"\n- BANNED ELEMENTS (STRICT PROHIBITION): Absolutely DO NOT include or describe: {', '.join(banned_list)}." if banned_list else ""

    if isinstance(scene_constants, dict):
        const_list = [str(x).strip() for v in scene_constants.values() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    else:
        const_list = [str(x).strip() for x in (scene_constants or []) if str(x).strip()]
    const_str = f"\n- SCENE CONSTANTS (MUST PERSIST): {', '.join(const_list)}." if const_list else ""

    if is_miniature:
        cast_desc = "\n".join([f"- {str(c)}" for c in (cast_identity or [])]) if cast_identity else "- Miniature Figurines living dynamic cast."
        return f"""You are an elite master prompt engineer specializing in hyper-realistic MINIATURE CRAFT TIME-LAPSE prompts for state-of-the-art AI video models (Runway Gen-3 Alpha, Kling AI, Luma Dream Machine, Sora, Hailuo) and image models (Midjourney v6, FLUX.1 Pro, Imagen 3).

Your task is to take an approved, locked N-beat construction ladder and synthesize the complete, production-ready prompt block in ONE unbroken pass:
- Exactly N+1 IMAGE prompts (IMAGE 1 to IMAGE N+1)
- Exactly N VIDEO prompts (VIDEO 1 to VIDEO N)

Adhere strictly to the following non-negotiable MINIATURE DIORAMA master rules:

1. [Beat-to-Frame Strict 1:1 Mapping]
- Every beat i represents the physical transformation from IMAGE i to IMAGE i+1 executed in unbroken time-lapse by VIDEO i.
- Exactly N beats -> Exactly N VIDEO prompts and N+1 IMAGE prompts. No missing slots, no extra slots.

2. [9:16 Vertical Macro Composition & Optics]
- Aspect ratio: 9:16 vertical (1080x1920).
- Lens: Macro perspective (50-85mm macro lens feel, shallow depth of field with creamy natural background blur, authentic scale ratios).

3. [ASMR Audio Mixing (60% Volume, Zero BGM)]
- In EVERY VIDEO prompt, explicitly specify physical ASMR sound effects: micro-tool impacts, delicate wood scraping, gravel pouring, mortar spreading, tweezers snapping, miniature switch toggling at 60% volume (`videoVolume: 0.6`).
- Do NOT add background music (BGM is 0%).

4. [Miniature Scale & Craftsman Giant Hands]
- Construction is executed 100% by OVERSIZED REAL HUMAN HANDS (The craftsman's right hand and forearm entering from upper/side frame margins) wielding precision micro-tools (tweezers, miniature steel trowels, craft knives, glue applicators, mini screeds).
- STRICTLY NEVER describe a full-size human worker inside the structure, neon safety vests, or hard hats!
- Living Cast / Figurines: Maintain the miniature figurines (1:24 dollhouse scale) as the living scale anchor and resident observers throughout the sequence.
{cast_desc}
- Clean image frame boundary: all IMAGE anchors are pristine still frames with ZERO active craftsman hands and NO floating tools.

5. [Full-Field Delta Conservation & Craft Tools]
- Scan Top, Middle, Bottom, and Peripherals. Any visible delta between IMAGE i and IMAGE i+1 MUST be physically crafted by the craftsman's hand using specific micro-tools in VIDEO i.

6. [Negative Restraints & Anti-Cavernous Hall]
- All prompts must strictly enforce: (full-size human body in frame, heavy construction excavator, safety helmet, high-vis vest, cavernous hall, giant space, wet glossy mirror floor:1.4).{banned_str}{const_str}

OUTPUT FORMAT REQUIREMENTS:
Output ONLY plain text with strict marker sections. No code fences, no conversational text.

===TITLE===
[Canonical Chinese Title]

===THEME===
[Scene Carrier / Theme Name]

===PROMPTS===
IMAGE 1 (中文说明):
[Comprehensive English photoreal image prompt for Image 1]

VIDEO 1 (中文说明):
[Comprehensive English I2V video prompt for Video 1, including ASMR 60% sound effects and craftsman hand action]

...
VIDEO N (中文说明):
[Comprehensive English I2V video prompt for Video N]

IMAGE N+1 (中文说明):
[Comprehensive English photoreal image prompt for final Image N+1 (reward / reveal)]
"""

    return f"""You are an elite master prompt engineer specializing in hyper-realistic restoration time-lapse prompts for state-of-the-art AI video models (Runway Gen-3 Alpha, Kling AI, Luma Dream Machine, Sora, Hailuo) and image models (Midjourney v6, FLUX.1 Pro, Imagen 3).

Your task is to take an approved, locked N-beat construction ladder and synthesize the complete, production-ready prompt block in ONE unbroken pass:
- Exactly N+1 IMAGE prompts (IMAGE 1 to IMAGE N+1)
- Exactly N VIDEO prompts (VIDEO 1 to VIDEO N)

Adhere strictly to the following non-negotiable master rules:

1. [Beat-to-Frame Strict 1:1 Mapping]
- Every beat i represents the physical transformation from IMAGE i to IMAGE i+1 executed in unbroken time-lapse by VIDEO i.
- Exactly N beats -> Exactly N VIDEO prompts and N+1 IMAGE prompts. No missing slots, no extra slots.

2. [9:16 Vertical Composition & Asymmetric 3-Zone Spatial Registration]
- Aspect ratio: 9:16 vertical (1080x1920).
- Lens: 24mm wide-angle equivalent feel (natural perspective without extreme fisheye distortion), 1.3m chest-level eye height, horizon line at 45%-50%.
- Respect observed 3-zone spatial layout (Grid A1-C1 Left Zone vs Grid A2-C2 Center Zone vs Grid A3-C3 Right Zone). If the reference scene is a 3/4 oblique or off-center perspective (e.g., solid wall on left, portal opening on right), preserve this exact asymmetric perspective and NEVER collapse it into a centered symmetrical tunnel.

3. [ASMR Audio Mixing (60% Volume, Zero BGM)]
- In EVERY VIDEO prompt, explicitly specify physical ASMR sound effects: tool impacts, sawing, scraping, power drills, welding sparks, high-pressure water sprays, or timber placement at 60% volume (`videoVolume: 0.6`).
- Do NOT add background music (BGM is 0%).

4. [Human Scale, Clean Frame Anchor & Structural Balance]
- In VIDEO prompts with workers, describe a lone male worker (1.78m tall, occupying ~35% of vertical frame height, realistically proportioned to the ~2.2m ceiling clearance).
- Worker gear: Describe neutral, authentic utility attire (e.g., durable work shirt and rugged pants). STRICTLY AVOID describing any banned items (e.g., hard hats, safety helmets, or vests if prohibited).
- Direct-at-zero action: workers are already positioned at the active work zone at t=0s.
- Clean image frame boundary: all IMAGE anchors are pristine still frames with ZERO active workers and NO handheld tools in mid-air.
- Structural Balance Compensation in IMAGE 1: When removing active workers from IMAGE 1, explicitly describe the stationary structural massing, rock ledges, ground platforms, or pillars in that zone to preserve authentic visual weight and prevent image models from sliding portals to the center.

5. [Full-Field Delta Conservation (4-Zone Spatial Scanning)]
- Scan all 4 physical zones: Top (Overhead/Ceiling), Middle (Walls/Facade), Bottom (Floor/Approach), Peripherals (Debris containers/Spoil piles).
- 100% Action-Tool Triad: Any visible delta between IMAGE i and IMAGE i+1 MUST be physically performed by the worker using specific tools in VIDEO i. Zero phantom transformations.

6. [DLSP 5-Layer Depth Staging & Asymmetry Lock]
- Layer 1 Foreground (<1m): Entry rim, threshold, or nearest foreground structural landmark.
- Layer 2 Midground (1-4m): Main expansive staging floor, active work face.
- Layer 3 Longitudinal Walls: Distinct left and right wall boundaries (e.g., solid left rock wall vs right portal opening). Forbid forced one-point center vanishing axes in asymmetric environments.
- Layer 4 Far Background (>4m): Room rear wall closure, strict metric envelope (~2.2m ceiling clearance).

7. [Matte Surface Texture & Zero Wet Glossy Reflections]
- Finished flooring (wood, stone, tile, polished concrete) must maintain a warm, matte or semi-matte authentic dry texture.
- NEVER describe wet glossy floors, high mirror-like reflections, or floating neon light strips.

8. [Two-Shot Decoupled Crossing Architecture (For Exterior -> Interior transitions)]
- If the ladder contains a portal/threshold transition:
  * Shot A (Exterior mechanical opening): Push-in to opening hatch/door with mechanical unlocking action; zero work contamination.
  * Shot B (Interior arrival & staging): Worker enters down ladder/steps, stages tools, and delivers the first interior physical milestone.

9. [Negative Restraints & Anti-Centering Defenses]
- All prompts must strictly enforce: (centered composition, symmetrical framing, central hole, circular portal, telescope vignette, tunnel vision, bowling alley effect, one-point perspective, cavernous hall, oversized room, giant space, miniature furniture, dollhouse scale, wet floor, high glossy mirror reflection, telephoto distortion:1.6).{banned_str}{const_str}

OUTPUT FORMAT REQUIREMENTS:
Output ONLY plain text with strict marker sections. No code fences, no introductory or concluding conversational text.

===TITLE===
[Canonical Chinese Title: e.g. 废弃地下地堡改造成避世温馨卧室]

===THEME===
[Scene Carrier / Theme Name]

===PROMPTS===
IMAGE 1 (中文说明):
[Comprehensive English photoreal image prompt for Image 1]

VIDEO 1 (中文说明):
[Comprehensive English I2V video prompt for Video 1, including ASMR 60% sound effects and worker action]

IMAGE 2 (中文说明):
[Comprehensive English photoreal image prompt for Image 2]

...
VIDEO N (中文说明):
[Comprehensive English I2V video prompt for Video N]

IMAGE N+1 (中文说明):
[Comprehensive English photoreal image prompt for final Image N+1 (reward / reveal)]
"""


def build_fast_composer_user_prompt(title, theme, beats_list, banned_elements=None, scene_constants=None, mutation_axes=None, scene_signature=None):
    """构建用户端结构化节拍提示词，包含 N 拍全部客观事实与交付成果。"""
    lines = []
    lines.append(f"Project Title: {title}")
    lines.append(f"Carrier / Theme: {theme}")
    if scene_signature:
        lines.append(f"Scene Signature: {scene_signature}")
    if isinstance(mutation_axes, dict) and mutation_axes:
        lines.append("Target 4-Axis Theme Settings:")
        for k, v in mutation_axes.items():
            if v:
                lines.append(f"  - {k}: {v}")
    lines.append(f"Total Beats: {len(beats_list)}\n")
    lines.append("Approved Beat Ladder:")

    for i, b in enumerate(beats_list):
        idx = i + 1
        op = b.get('operation') or b.get('stage') or 'construction'
        stage = b.get('stage') or op
        action = b.get('visible_action') or ''
        result = b.get('visible_result') or ''
        pkg = b.get('package_operations') or []
        pkg_str = f" [Operations: {', '.join(pkg)}]" if pkg else ""
        details = b.get('visible_details') or []
        details_str = f" | Materials/Details: {', '.join(details)}" if details else ""
        traces = b.get('persistent_traces') or []
        traces_str = f" | Persistent Traces: {', '.join(traces)}" if traces else ""
        space = b.get('space') or 'main_space'

        lines.append(
            f"- Beat {idx} (Space: {space}, Stage: {stage}): {action} -> {result}{pkg_str}{details_str}{traces_str}"
        )

    lines.append("\nPlease synthesize the full ===TITLE===, ===THEME===, and ===PROMPTS=== sequence now.")
    return "\n".join(lines)


def beats_to_ladder_payload(beats_list):
    """把 beats 数组转换为与 downstream 渲染兼容的 beat_ladder 结构体。"""
    ladder = []
    for i, b in enumerate(beats_list):
        idx = i + 1
        op = str(b.get('operation') or b.get('stage') or 'construction').strip()
        stage = str(b.get('stage') or op).strip()
        action = _clean_str(b.get('visible_action'))
        result = _clean_str(b.get('visible_result'))
        milestone = _clean_str(b.get('milestone_name'), result or f'Stage {idx}')
        before = _clean_str(b.get('state_before') or b.get('before_state'))
        after = _clean_str(b.get('state_after') or b.get('after_state'), result)
        pkg = b.get('package_operations') or ([op] if op else ['construction'])
        traces = b.get('persistent_traces') or ['contact marks', 'material dust']
        space = _clean_str(b.get('space'), 'exterior')
        camera = _clean_str(b.get('camera_setup') or b.get('observed_camera'), 'wide')
        is_bridge = (stage in ('transition', 'threshold') or op in ('threshold', 'reframe', 'bridge'))
        is_cut = bool(b.get('hard_cut'))
        turn_dir = _clean_str(b.get('turn_direction'), 'none')

        ladder.append({
            'index': idx,
            'operation': op,
            'stage': stage,
            'milestone_name': milestone,
            'visible_action': action,
            'visible_result': result,
            'before_state': before,
            'after_state': after,
            'package_operations': pkg,
            'persistent_traces': traces,
            'space': space,
            'camera_family': camera,
            'bridge_stage': 1 if is_bridge else None,
            'hard_cut': is_cut,
            'turn_direction': turn_dir,
        })
    return ladder


def synthesize_drift_lock_packet(theme, beats_list, carrier="structure"):
    """为 downstream stepped_pipeline 生成空间锁定包 (Drift Lock Packet)。"""
    return {
        "world_lock": {
            "terrain_contour": "fixed terrain and approach path",
            "vegetation": "surrounding persistent natural flora",
            "weather_sky": "consistent diffused overcast lighting",
            "exposure_keylight": "natural 5600K diffused daylight",
            "status": "accepted_image_1_frozen"
        },
        "camera_dna": {
            "lens": "24mm wide-angle equivalent",
            "height": "1.3m chest-level",
            "attitude": "camera pitch locked level, perspective locked to observed scene axis",
            "aspect_ratio": "9:16 vertical"
        },
        "geometry_lock": {
            "ceiling_clearance": "~2.2m metric envelope",
            "floor_plane": "dry matte authentic substrate, strictly no glossy water reflections"
        },
        "primary_landmarks": [
            {"name": "Base Carrier Structure", "grid": "Grid B2", "z_depth_scale": "50%", "material_color": "weathered authentic"},
            {"name": "Foreground Floor Boundary", "grid": "Grid C2", "z_depth_scale": "15%", "material_color": "ground substrate"},
            {"name": "Rear Depth Wall", "grid": "Grid A2", "z_depth_scale": "85%", "material_color": "background boundary"}
        ],
        "frame_boundaries": {
            "layer_1_foreground": "entry threshold / lower frame edge (<1m)",
            "layer_2_midground": "active work floor and staging zone (1-4m)",
            "layer_3_walls": "left and right longitudinal boundary surfaces",
            "layer_4_background": "rear wall closure and room boundary (>4m)"
        },
        "carrier_envelope": {
            "width": "3.2m",
            "depth": "4.5m",
            "height": "2.2m"
        },
        "entrance_topology": {
            "opening_plane": "vertical_axial",
            "entry_motion": "level_push",
            "turn_degrees": 0,
            "turn_direction": "none"
        },
        "camera_palette": {
            "default": "24mm wide 1.3m chest-level",
            "exterior": "24mm wide level horizon",
            "interior": "24mm wide level pitch centered axis"
        }
    }


def compose_replica_one_pass(config, state, on_progress=None):
    """执行 1-Pass 极速直通合成。
    
    1. 组装固化了全量规则的系统提示词与节拍事实；
    2. 单次大模型调用直接生成全量提示词；
    3. 本地解析分槽并构建兼容 stepped_pipeline 的 compose_state。
    """
    beats_doc = state.get('beats') or {}
    beats_list = beats_doc.get('beats') or []
    if not beats_list:
        raise ValueError('还没有节拍阶梯，无法进行极速合成')

    total_beats = len(beats_list)
    banned_elements = beats_doc.get('banned_elements') or []
    scene_constants = beats_doc.get('scene_constants') or []
    carrier = _clean_str(beats_doc.get('carrier') or state.get('video_name'), '空间载体')
    destiny_zh = _clean_str(beats_doc.get('destiny_zh'), '精品空间')
    title = state.get('title') or pp._canonical_title(carrier, destiny_zh) or f"{carrier}改造成{destiny_zh}"
    theme = carrier
    cast_identity = beats_doc.get('cast_identity') or []
    scene_signature = beats_doc.get('scene_signature') or state.get('scene_signature') or ''
    mutation_axes = beats_doc.get('mutation_axes') or (state.get('mutation_config') or {}).get('axes') or {}

    is_miniature = (
        str((config or {}).get('skillProfile') or '').lower() == 'miniature'
        or 'miniature' in str(state.get('title') or '').lower()
        or 'miniature' in str(carrier).lower()
        or 'miniature' in str(scene_signature).lower()
        or any('miniature' in str(c).lower() or 'craftsman' in str(c).lower() for c in cast_identity)
    )

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': f'正在启动极速直通合成（单轮直出 {total_beats} 拍提示词）…',
        })

    system_prompt = build_fast_composer_system_prompt(
        banned_elements=banned_elements,
        scene_constants=scene_constants,
        is_miniature=is_miniature,
        cast_identity=cast_identity,
    )
    user_prompt = build_fast_composer_user_prompt(
        title=title,
        theme=theme,
        beats_list=beats_list,
        banned_elements=banned_elements,
        scene_constants=scene_constants,
        mutation_axes=mutation_axes,
        scene_signature=scene_signature,
    )

    pp._raise_if_cancelled(on_progress)
    t0 = time.time()
    raw_response = pp._chat(config, system_prompt, user_prompt, temperature=0.7, timeout=120)
    elapsed = round(time.time() - t0, 1)

    if sys.stdout:
        print(f"[FAST_COMPOSER] 单轮极速直出完成，耗时 {elapsed}s")

    cleaned_response = pp._strip_code_fences(raw_response).strip()
    parsed_sections = pp.parse_sections(cleaned_response)

    resolved_title = parsed_sections.get('title') or title
    resolved_theme = parsed_sections.get('theme') or theme
    prompt_block_raw = parsed_sections.get('prompt_block') or cleaned_response

    # 规范化 prompt_block，确保槽位中文说明与 beats_doc 对应
    from replica_pipeline import _ensure_prompt_block_summaries
    prompt_block = _ensure_prompt_block_summaries(prompt_block_raw, beats_doc)

    # 解析 slots
    parsed_images, parsed_videos = pp._parse_prompt_slots(prompt_block)
    compiled_images = {k: (v.get('body') if isinstance(v, dict) else str(v)) for k, v in parsed_images.items()}
    compiled_videos = {k: (v.get('body') if isinstance(v, dict) else str(v)) for k, v in parsed_videos.items()}

    image_1_prompt = compiled_images.get(1, '')
    if not image_1_prompt and parsed_images:
        first_k = sorted(parsed_images.keys())[0]
        image_1_prompt = compiled_images.get(first_k, '')

    # 锚点图如果能找到原片真实首帧，自动走 ground_anchor_on_reference 进行像素级空间与材质校正
    reference = None
    if not reverse.is_variant_doc(beats_doc):
        overview = state.get('overview') or state.get('video_overview') or {}
        if not overview and state.get('job_dir'):
            ov_path = os.path.join(state['job_dir'], 'video_overview.json')
            if os.path.exists(ov_path):
                try:
                    with open(ov_path, 'r', encoding='utf-8') as f:
                        overview = json.load(f)
                except Exception:
                    pass
        reference = reverse.anchor_reference_frame(beats_doc, overview)
        if not reference and state.get('job_dir'):
            for sdir_name in ('review_frames', 'storyboard'):
                sdir = os.path.join(state['job_dir'], sdir_name)
                if os.path.isdir(sdir):
                    cands = sorted([os.path.join(sdir, fn) for fn in os.listdir(sdir) if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                    if cands:
                        reference = cands[0]
                        break

    if reference and image_1_prompt:
        try:
            image_1_prompt = reverse.ground_anchor_on_reference(config, image_1_prompt, reference, on_progress=on_progress)
            compiled_images[1] = image_1_prompt
            if 1 in parsed_images:
                if isinstance(parsed_images[1], dict):
                    parsed_images[1]['body'] = image_1_prompt
                else:
                    parsed_images[1] = image_1_prompt
                prompt_block_raw = pp._format_prompt_block(parsed_images, parsed_videos)
                prompt_block = _ensure_prompt_block_summaries(prompt_block_raw, beats_doc)
        except Exception as exc:
            if sys.stdout:
                print(f"[FAST_COMPOSER] 首帧参考图空间对齐软退: {exc}")

    beat_ladder = beats_to_ladder_payload(beats_list)
    packet = synthesize_drift_lock_packet(resolved_theme, beats_list, carrier=carrier)

    dims = reverse.beats_to_dimensions(beats_doc)

    # 着装口径必须来自原片（beats_doc.worker_attire，或 beats_to_dimensions 从 cast 折出来的
    # 那一份）。这里曾经兜底成 "…with reflective vest"——反光背心/安全帽恰恰是这条线上最
    # 常见的 banned_elements，而 parsed_brief.worker_attire 是下游合成器"照抄不误"的字段：
    # 兜底一句就等于给每一单凭空穿上原片没有的背心，再被自己的门禁拦下来。
    # 见 prompt_pipeline/__init__.py 的 THEME-ADAPTIVE ATTIRE RULE。
    default_attire = 'one lone craftsman in a solid neutral work t-shirt, dark cargo trousers, and boots'
    parsed_brief = {
        'carrier': carrier,
        'env': 'surrounding environment',
        'trauma': 'ruined state',
        'destiny': destiny_zh,
        'destiny_zh': destiny_zh,
        'reward': 'lights activate and reveal finished space',
        'mode': 'Threshold' if any(b.get('stage') == 'transition' or b.get('hard_cut') for b in beats_list) else 'Standard',
        'space_type': 'abandoned property',
        'threshold_variant': 'hard_cut' if any(b.get('hard_cut') for b in beats_list) else 'coaxial',
        'worker_attire': _clean_str(beats_doc.get('worker_attire') or dims.get('worker_attire'),
                                    default_attire),
        'banned_elements': banned_elements,
        'scene_constants': scene_constants,
    }

    brief_fingerprint = pp.get_brief_fingerprint(dims, pp.active_skill_profile(config))

    compose_state = {
        'theme': resolved_theme,
        'total_beats': total_beats,
        'parsed_brief': parsed_brief,
        'title': resolved_title,
        'beat_ladder': beat_ladder,
        'packet': packet,
        'brief_fingerprint': brief_fingerprint,
        'image_1_prompt': image_1_prompt,
        'compiled_images': compiled_images,
        'compiled_videos': compiled_videos,
    }

    return prompt_block, compose_state
