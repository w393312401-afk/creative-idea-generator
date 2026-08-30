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
from prompt_pipeline.human_cast import REAL_HUMAN_CAST_RULE


def _clean_str(val, default=''):
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


# 「巨手工匠」那条识别项：微缩线里唯一按真人尺度出现的活物（伸进画面的那只手），
# 比例锁与做旧前缀都不该落在它头上。
_IS_GIANT_HAND_RE = re.compile(
    r'(?i)\b(?:giant|oversized|craftsm[ae]n|god[-\s]?hand)\b|\bhands?\b\s*(?:and|,|$)'
)


def build_fast_composer_system_prompt(banned_elements=None, scene_constants=None, is_miniature=False, cast_identity=None):
    """构建固化了全套工业级规则（AGENTS.md、DLSP 5层景深、ASMR 60%、4-Zone守恒、Rule 13活物应激律）的系统提示词。"""
    banned_list = [str(x).strip() for x in (banned_elements or []) if str(x).strip()]
    banned_str = f"\n- BANNED ELEMENTS (STRICT PROHIBITION): Absolutely DO NOT include or describe: {', '.join(banned_list)}." if banned_list else ""

    if isinstance(scene_constants, dict):
        const_list = [str(x).strip() for v in scene_constants.values() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    else:
        const_list = [str(x).strip() for x in (scene_constants or []) if str(x).strip()]
    const_str = f"\n- SCENE CONSTANTS (MUST PERSIST): {', '.join(const_list)}." if const_list else ""
    # 活物一律真人：两个分支同一份措辞（prompt_pipeline.human_cast.REAL_HUMAN_CAST_RULE），
    # 微缩线也照发——那条线的人只是小，不是塑料的。尺寸归比例锁管。
    human_cast_rule = REAL_HUMAN_CAST_RULE

    if is_miniature:
        cast_desc = "\n".join([f"- {str(c)}" for c in (cast_identity or [])]) if cast_identity else "- Two real human residents at 1:24 scale (a couple: man in a casual shirt and shorts, woman in a colourful wrap dress, each roughly a thumb tall) - living people with real skin and fabric, photographed as humans, never resin or plastic figures."
        return f"""You are an elite master prompt engineer specializing in hyper-realistic MINIATURE CRAFT TIME-LAPSE prompts for state-of-the-art AI video models (Runway Gen-3 Alpha, Kling AI, Luma Dream Machine, Sora, Hailuo) and image models (Midjourney v6, FLUX.1 Pro, Imagen 3).

Your task is to take an approved, locked N-beat construction ladder and synthesize the complete, production-ready prompt block in ONE unbroken pass:
- Exactly N+1 IMAGE prompts (IMAGE 1 to IMAGE N+1)
- Exactly N VIDEO prompts (VIDEO 1 to VIDEO N)

Adhere strictly to the following non-negotiable MINIATURE DIORAMA master rules:

1. [Beat-to-Frame Strict 1:1 Mapping]
- Every beat i represents the physical transformation from IMAGE i to IMAGE i+1 executed in unbroken time-lapse by VIDEO i.
- Exactly N beats -> Exactly N VIDEO prompts and N+1 IMAGE prompts. No missing slots, no extra slots.

2. [9:16 Vertical Macro Composition, Three-Layer Depth & Multi-Camera Matrix (Rule 9, 17 & 18)]
- Aspect ratio: 9:16 vertical (1080x1920).
- Three-Layer Environmental Staging (MANDATORY):
  * Far Horizon & Sky: MUST explicitly construct and preserve the open natural daylight sky with soft drifting clouds and distant rolling hills/mountains on the far horizon, with authentic atmospheric depth haze and distant trees. STRICTLY NEVER use a closed-in blurry bokeh wall, dark curtain, or dead background that cuts off the sky and distant mountain horizon!
  * Midground: The miniature architectural diorama transformation site and evolving craft structure.
  * Foreground & Ground Plane: Rich tactile natural ground texture (dry mineral soil, fine gravel, scattered pebbles, sparse weeds, twigs, surface cracks, or riverbank silt).
- DIVERSE CINEMATOGRAPHY & MULTI-ANGLE MATRIX (STRICTLY FORBID REPETITIVE ANGLES):
  * STRICTLY NEVER freeze or repeat the exact same static camera angle across the sequence!
  * Actively vary and faithfully execute each beat's declared Camera Setup:
    1. High-Angle / Elevated 3/4 Perspective (~45°-60° pitch): for site layout, trenching, foundation mesh, slab pouring, and ground clearing, giving full view of the groundwork workspace while retaining the upper horizon and distant mountains.
    2. Bird's-Eye / Top-Down View (75°-85°): for geometric chalk line surveying, radial grid layout, and foundation trench excavations.
    3. Low-Angle Dramatic Perspective (15°-30° upward looking): for stilt column erection, upper-floor framing, timber roof trusses, and second-story eaves, making the handcrafted structure look soaring and grand against the open sky and clouds.
    4. Tight Macro Close-Up (85-100mm macro lens): for intricate artisan joinery, micro-mortar troweling, bamboo weaving, copper rivet fastening, and precise micro-tool actions.
    5. 3/4 Diagonal Oblique Perspective: for exterior wall assembly, cantilevered terraces, and outdoor staircases showing depth and lateral architecture.
    6. Sweeping Grand Hero Reveal (24-35mm wide lens): for the interior foyer unveiling and final estate hero reveal with lush landscaping, water flow, wildlife, and the joyful residents.

3. [ASMR Audio Mixing (60% Volume, Zero BGM)]
- In EVERY VIDEO prompt, explicitly specify physical ASMR sound effects: micro-tool impacts, delicate wood scraping, gravel pouring, mortar spreading, tweezers snapping, miniature switch toggling at 60% volume (`videoVolume: 0.6`).
- Do NOT add background music (BGM is 0%).

4. [Living Cast Dynamic Reflex, Spatial Migration & Destitute Wear Protocol (Rule 13, 15 & 17 - Mandatory)]
- Permanent Identity & Impoverished Attire Condition: The resident miniature-scale people are the permanent living scale anchor and observers throughout the sequence:
{cast_desc}
- EVERY IMAGE prompt (IMAGE 1 to IMAGE N+1): MUST EXPLICITLY INCLUDE the resident miniature-scale people with their locked attire and appearance:
  * ZERO BEAUTIFICATION HALLUCINATION: In initial trauma/ruined shelter beats (IMAGE 1 / Beat 1), they MUST be described as destitute, impoverished wandering refugees/residents: severely weathered, faded, dust-caked, frayed, grimy clothing with visible dirt smudges (e.g. distressed faded scoop top, patched worn trousers, weathered headwrap/sandals, or bare feet). STRICTLY NEVER describe them wearing clean, crisp, bright, or fashionable modern tourist outfits (NO "clean royal blue shirt", NO "crisp new floral dress")!
  * DYNAMIC SPATIAL MIGRATION & ANTI-POSITION-LOCK PROTOCOL (STRICTLY BAN REPETITIVE LOWER-LEFT STANDING):
    1. They MUST dynamically migrate across distinct physical zones of the diorama as the structure evolves (e.g. Beat 1: crouched beside ruined hut studying blueprint -> Beat 3: walking along chalk survey lines -> Beat 5: touching brass column base -> Beat 6: standing under raised deck looking up -> Beat 10: standing near outer wall panel admiring bamboo weave -> Beat 11: standing by doorway threshold -> Beat 13: standing inside on woven bamboo mat -> Beat 16: walking hand-in-hand along boardwalk -> Beat 17: standing at dock waving).
    2. STRICTLY FORBID repeating the same position (such as "standing at bottom-left", "standing at lower-left", "standing at diorama edge", "standing on the ground watching") across consecutive frames! Every frame must place them at a fresh, contextually relevant milestone interaction spot.
    3. Diverse physical postures & body language: kneeling, leaning in, touching components, pointing upward, walking, stepping across thresholds, resting on finished benches, celebrating. NEVER describe them as static bystanders doing nothing.
  * Baseline demeanor: haggard, sorrowful, helpless distress in initial trauma, progressing to rising wonder and hope during construction, and joyful celebration in the final reveal.
- EVERY VIDEO prompt (VIDEO 1 to VIDEO N): MUST SIMULTANEOUSLY DESCRIBE the giant craftsman's hands AND the residents' dynamic action-reaction causal triad + spatial displacement:
  * Inception reflex: as giant hands/tools enter from frame margins, the residents immediately react with head tilts, gaze shifts, or stepping aside in wonder/awe.
  * Operational tracking: the residents turn heads, shift body weight, lean in, and eye-track the moving micro-tools as work proceeds, expressions shifting from initial disbelief/shock to rising hope and admiration.
  * Settlement stance: as hands withdraw with completed milestone, the residents move/step closer to inspect, touch, nod, cheer, or celebrate at their new position.
- FORBIDDEN in Video: NEVER write 'remain standing', 'stay put', 'unchanged', or leave them as frozen motionless dolls or plastic props. NEVER omit the residents from the video prompt body.

5. [Miniature Scale & Craftsman Giant Hands]
- Construction is executed 100% by OVERSIZED REAL HUMAN HANDS (The craftsman's right hand and forearm entering from upper/side frame margins) wielding precision micro-tools (tweezers, miniature steel trowels, craft knives, glue applicators, mini screeds).
- STRICTLY NEVER describe a full-size human worker inside the structure, neon safety vests, or hard hats!
- NATURAL HAND MECHANICS: the giant hand moves with real human mechanics, not a smooth constant-speed glide — it hesitates and adjusts grip before a precise placement, presses with visibly varying pressure, and withdraws at a different pace than it entered. Never a mechanically identical repeating motion.
- Clean image frame boundary: all IMAGE anchors are pristine still frames with ZERO active craftsman hands and NO floating tools.

6. [Full-Field Delta Conservation & Craft Tools]
- Scan Top, Middle, Bottom, and Peripherals. Any visible delta between IMAGE i and IMAGE i+1 MUST be physically crafted by the craftsman's hand using specific micro-tools in VIDEO i.

7. [Consistent Horizontal Ground Baseline & Anti-Isometric Skewing Protocol (Rule 15)]
- In architectural diorama restoration, the building's front facade and groundwork baseline MUST maintain a single, consistent horizontal alignment facing forward (front bearing, horizontal baseline parallel to frame bottom, central entrance axis aligned with frame center).
- Groundwork, chalk lines, foundation trenches, and slabs MUST NOT be rotated 45 degrees into corner-on isometric diamonds.
- For groundwork beats, use high-angle perspective (~45-60 degrees pitch) retaining upper horizon, mountains, sky, and savanna background. Strictly forbid 90-degree zenith flat orthographic maps.

8. [Negative Restraints & Anti-Distortion Defenses]
- Strictly avoid describing or creating any of the following: clean/brand-new/fashionable modern outfits, tourists, affluent styling, glossy shoes, neat luxury attire; studio backdrops, blurry blank backgrounds, extreme shallow depth of field, or any framing that cuts off the horizon; flat orthographic plan views, vertical aerial map views, overhead blueprint views, or a 90-degree zenith shot with no perspective convergence; isometric diamond grids, tilted/rotated layouts, or a rectangular footprint turned corner-on; full-size human bodies in frame, heavy construction excavators, safety helmets, high-vis vests; cavernous halls, giant spaces, wet glossy mirror floors; and ghost structures — a demolished hut or cleared ruin must never reappear in a later beat.
- A steep bird's-eye (75-85 degrees) is ALLOWED and often correct for groundwork beats, provided it keeps visible perspective convergence and reads as a real camera high above the set. What is banned is the flat, perspective-free map look, not the high angle itself.
- NEVER append negative prompt syntax, bracketed tags, or weighting markers like `(cavernous hall:1.8)` to the prompt body. Prompts must be clean descriptive natural English sentences only.{banned_str}{const_str}

{human_cast_rule}

9. [CRITICAL LANGUAGE SPECIFICATION - 100% ENGLISH PROMPT BODIES]
- ALL IMAGE PROMPTS (IMAGE 1..N+1) AND ALL VIDEO PROMPTS (VIDEO 1..N) MUST BE WRITTEN IN 100% DESCRIPTIVE, PHOTOREALISTIC ENGLISH.
- ABSOLUTELY NEVER write prompt body text in Chinese! Downstream AI generation backends (Midjourney, FLUX, Kling AI, Hailuo, Sora, Runway, Google Imagen) strictly require English prompts.
- The ONLY Chinese allowed in the entire output is the Title in ===TITLE=== and the short slot summary in parentheses on the header line, e.g. "IMAGE 1 (破旧茅草屋初始状态):". All prompt bodies below each header line MUST be pure, production-ready English!

OUTPUT FORMAT REQUIREMENTS:
Output ONLY plain text with strict marker sections. No code fences, no conversational text.

===TITLE===
[Canonical Chinese Title]

===THEME===
[Scene Carrier / Theme Name]

===PROMPTS===
IMAGE 1 (中文说明):
[Comprehensive ENGLISH photoreal image prompt for Image 1, including diorama context and the resident miniature-scale people. Pure English text only, NO Chinese in prompt body]

VIDEO 1 (中文说明):
[Comprehensive ENGLISH I2V video prompt for Video 1, including ASMR 60% sound effects, craftsman hand action, and the residents' dynamic reflex / eye-tracking reaction. Pure English text only, NO Chinese in prompt body]

...
VIDEO N (中文说明):
[Comprehensive ENGLISH I2V video prompt for Video N, including ASMR 60% sound effects, craftsman hand action, and the residents' dynamic reflex / eye-tracking reaction. Pure English text only, NO Chinese in prompt body]

IMAGE N+1 (中文说明):
[Comprehensive ENGLISH photoreal image prompt for final Image N+1 (reward / reveal), including finished diorama and the celebrating residents. Pure English text only, NO Chinese in prompt body]
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
- NATURAL HUMAN BODY MECHANICS (mandatory, not decorative): the worker's own body must move with real human mechanics, not a smooth constant-speed glide between two poses — weight shifts onto the working leg or arm before each effort, the torso leans and counter-rotates with the load, each repeated pass lands at a slightly different angle and pace than the last, and there is a brief natural settle between reaching for a tool and gripping it. A clip whose only instruction is "worker does the task" reads as a robotic loop; describe the weight and timing, not just the task.

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
- Strictly avoid describing or creating forbidden visual artifacts: NO centered symmetrical holes, NO circular portals or tunnel framing, NO telescope vignettes, NO bowling alley perspective, NO cavernous halls, NO oversized rooms, NO dollhouse scale, NO wet glossy mirror reflections.
- NEVER append negative prompt syntax, bracketed tags, or weighting markers like `(circular portal:1.6)` to the prompt body. Prompts must be clean descriptive natural English sentences only.{banned_str}{const_str}

{human_cast_rule}

10. [CRITICAL LANGUAGE SPECIFICATION - 100% ENGLISH PROMPT BODIES]
- ALL IMAGE PROMPTS (IMAGE 1..N+1) AND ALL VIDEO PROMPTS (VIDEO 1..N) MUST BE WRITTEN IN 100% DESCRIPTIVE, PHOTOREALISTIC ENGLISH.
- ABSOLUTELY NEVER write prompt body text in Chinese! Downstream AI generation backends (Midjourney, FLUX, Kling AI, Hailuo, Sora, Runway, Google Imagen) strictly require English prompts.
- The ONLY Chinese allowed in the entire output is the Title in ===TITLE=== and the short slot summary in parentheses on the header line, e.g. "IMAGE 1 (初始未动工状态):". All prompt bodies below each header line MUST be pure, production-ready English!

OUTPUT FORMAT REQUIREMENTS:
Output ONLY plain text with strict marker sections. No code fences, no introductory or concluding conversational text.

===TITLE===
[Canonical Chinese Title: e.g. 废弃地下地堡改造成避世温馨卧室]

===THEME===
[Scene Carrier / Theme Name]

===PROMPTS===
IMAGE 1 (中文说明):
[Comprehensive ENGLISH photoreal image prompt for Image 1. Pure English text only, NO Chinese in prompt body]

VIDEO 1 (中文说明):
[Comprehensive ENGLISH I2V video prompt for Video 1, including ASMR 60% sound effects and worker action. Pure English text only, NO Chinese in prompt body]

IMAGE 2 (中文说明):
[Comprehensive ENGLISH photoreal image prompt for Image 2. Pure English text only, NO Chinese in prompt body]

...
VIDEO N (中文说明):
[Comprehensive ENGLISH I2V video prompt for Video N. Pure English text only, NO Chinese in prompt body]

IMAGE N+1 (中文说明):
[Comprehensive ENGLISH photoreal image prompt for final Image N+1 (reward / reveal). Pure English text only, NO Chinese in prompt body]
"""


def build_fast_composer_user_prompt(title, theme, beats_list, banned_elements=None, scene_constants=None, mutation_axes=None, scene_signature=None, cast_identity=None, observed_block=''):
    """构建用户端结构化节拍提示词，包含 N 拍全部客观事实、交付成果与人物动态行为。

    `observed_block`（见 prompt_pipeline.observed_grounding）是逐帧读数压出来的
    「原片实拍事实卡」全量版。极速通道是单轮直出全部 N 拍，所以整份一起发；深度
    通道逐拍发，走 _build_batch_user_message 那条按拍切好的通路。写手此前从头到尾
    看不到任何一帧原片——拍级字段只给到一句动作描述加机位标签，三区布局、前景植被、
    人偶站位全靠空想（2026-08-30 复盘）。"""
    lines = []
    lines.append(f"Project Title: {title}")
    lines.append(f"Carrier / Theme: {theme}")
    if scene_signature:
        lines.append(f"Scene Signature: {scene_signature}")
    if cast_identity:
        lines.append("Permanent Living Cast Identity:")
        for c in cast_identity:
            lines.append(f"  - {c}")
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
        cast_act = b.get('cast_action') or ''
        cast_str = f" | Cast Action: {cast_act}" if cast_act else ""
        pkg = b.get('package_operations') or []
        pkg_str = f" [Operations: {', '.join(pkg)}]" if pkg else ""
        details = b.get('visible_details') or []
        details_str = f" | Materials/Details: {', '.join(details)}" if details else ""
        traces = b.get('persistent_traces') or []
        traces_str = f" | Persistent Traces: {', '.join(traces)}" if traces else ""
        space = b.get('space') or 'main_space'

        # Camera & Cinematography specifications (Rule 9 & Rule 18)
        zh_dict = b.get('zh') if isinstance(b.get('zh'), dict) else {}
        c_angle = b.get('camera_angle') or zh_dict.get('camera_angle') or ''
        c_bearing = b.get('camera_bearing') or zh_dict.get('camera_bearing') or ''
        s_scale = b.get('shot_scale') or zh_dict.get('shot_scale') or ''
        lens = b.get('lens_feel') or zh_dict.get('lens_feel') or ''
        cam_specs = [x for x in [c_angle, c_bearing, s_scale, lens] if x]
        cam_str = f" | Camera Setup: [{', '.join(cam_specs)}]" if cam_specs else ""
        placement = b.get('subject_placement') or zh_dict.get('subject_placement') or ''
        place_str = f" | Framing: {placement}" if placement else ""

        lines.append(
            f"- Beat {idx} (Space: {space}, Stage: {stage}): {action} -> {result}{cam_str}{place_str}{pkg_str}{details_str}{traces_str}{cast_str}"
        )

    if observed_block:
        lines.append("\n" + observed_block)

    lines.append("\nPlease synthesize the full ===TITLE===, ===THEME===, and ===PROMPTS=== sequence now. CRITICAL: all IMAGE and VIDEO prompt bodies MUST be written in pure descriptive ENGLISH (only slot headers have Chinese summaries). Each beat's prompt MUST strictly reflect its distinct Camera Setup (angle, bearing, shot scale) for cinematic diversity.")
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


def _most_common(values, default=''):
    """出现次数最多的那个非空值。并列时取先出现的——beats 是有序的，靠前的更接近开场口径。"""
    counts = {}
    for v in values:
        v = str(v or '').strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return default
    return max(counts, key=lambda k: (counts[k], -list(counts).index(k)))


# 微缩线的镜头基线。原来这里和真人线共用一份常量，写的是「1.3m 胸高相机、2.2m 层高、
# 3.2×4.5×2.2m 载体」——1:24 沙盘上说这些是范畴错误，等于告诉图像模型这个世界是真人尺度的。
# 2026-08-30 实测（replica_cf9a445bc52b）：九帧人偶占比从「两个小点」漂到「压过整个地基」。
_MINIATURE_CAMERA_DNA = {
    'lens': '85-100mm macro lens feel with shallow depth of field',
    'height': 'camera just above the diorama tabletop, looking down at the observed pitch',
    'attitude': 'perspective locked to the observed scene axis; never a flat orthographic map view',
    'aspect_ratio': '9:16 vertical',
}
_FULLSCALE_CAMERA_DNA = {
    'lens': '24mm wide-angle equivalent',
    'height': '1.3m chest-level',
    'attitude': 'camera pitch locked level, perspective locked to observed scene axis',
    'aspect_ratio': '9:16 vertical',
}


def synthesize_drift_lock_packet(theme, beats_list, carrier="structure",
                                 is_miniature=False, cast_scale=None,
                                 worker_scale_percent=None):
    """为 downstream stepped_pipeline 生成空间锁定包 (Drift Lock Packet)。

    2026-08-30 之前这是一份**完全写死的常量**——三个入参 theme / beats_list / carrier
    一个都没用上，而且内容是真人尺度的（1.3m 胸高、2.2m 层高、3.2×4.5×2.2m 载体、
    以米为单位的景深分层）。微缩单拿到的就是这一份。同时它**没有任何比例键**，于是
    `_worker_scale_clause_from_packet` 一个字都注入不进去、`check_worker_scale_lock`
    在 `expected is None` 处直接 return [] 报绿——锁不存在，校验器还说没问题。
    深度线的 schema 是强制要求 worker_scale_percent 的，这里是快/深两个口径的又一处。

    现在：机位口径从 beats 观测到的机位/焦段真算，尺度按 is_miniature 分叉，比例键由
    调用方按原片量出来的数注入（量不到就留空——宁可没有锁，也不要凭空造一个数骗过
    校验器）。

    `cast_scale` 是**物理比例**（"1:24"）不是画幅占比，这是有意的。微缩线的镜头矩阵明令
    机位要在 85-100mm 微距特写与 24-35mm 广角英雄镜头之间大幅变化，钉画幅占比等于让锁
    和镜头规则互相打架；而人偶相对建筑/砖块的**物理**大小本来就该恒定，这才是 2026-08-30
    那九帧真正漂掉的东西（IMG 006 人偶压过整个地基，IMG 001 又缩成两个点）。
    """
    beats_list = beats_list or []
    obs_angle = _most_common(b.get('camera_angle') for b in beats_list)
    obs_lens = _most_common(b.get('lens_feel') for b in beats_list)
    obs_bearing = _most_common(b.get('camera_bearing') for b in beats_list)

    camera_dna = dict(_MINIATURE_CAMERA_DNA if is_miniature else _FULLSCALE_CAMERA_DNA)
    if obs_lens:
        camera_dna['lens'] = f'{obs_lens} (as observed in the reference film)'
    if obs_angle:
        camera_dna['attitude'] = (
            f'{camera_dna["attitude"]}; dominant observed angle {obs_angle}'
            + (f', bearing {obs_bearing}' if obs_bearing else ''))

    if is_miniature:
        geometry_lock = {
            'build_scale': 'handcrafted miniature diorama built on a tabletop set',
            'floor_plane': 'dry matte authentic substrate, strictly no glossy water reflections',
        }
        frame_boundaries = {
            'layer_1_foreground': 'nearest ground texture at the lower frame edge',
            'layer_2_midground': 'the miniature structure and the active craft work face',
            'layer_3_walls': 'left and right lateral extent of the diorama set',
            'layer_4_background': 'open sky, distant blurred horizon and far trees',
        }
        # 微缩线没有「载体内部净空」这回事——照抄真人线那份 3.2×4.5×2.2m 只会误导。
        carrier_envelope = {'note': 'miniature diorama set; no human-scale interior envelope applies'}
    else:
        geometry_lock = {
            'ceiling_clearance': '~2.2m metric envelope',
            'floor_plane': 'dry matte authentic substrate, strictly no glossy water reflections',
        }
        frame_boundaries = {
            'layer_1_foreground': 'entry threshold / lower frame edge (<1m)',
            'layer_2_midground': 'active work floor and staging zone (1-4m)',
            'layer_3_walls': 'left and right longitudinal boundary surfaces',
            'layer_4_background': 'rear wall closure and room boundary (>4m)',
        }
        carrier_envelope = {'width': '3.2m', 'depth': '4.5m', 'height': '2.2m'}

    packet = {
        "world_lock": {
            "terrain_contour": "fixed terrain and approach path",
            "vegetation": "surrounding persistent natural flora",
            "weather_sky": "consistent diffused overcast lighting",
            "exposure_keylight": "natural 5600K diffused daylight",
            "status": "accepted_image_1_frozen"
        },
        "camera_dna": camera_dna,
        "geometry_lock": geometry_lock,
        "primary_landmarks": [
            {"name": _clean_str(carrier, "Base Carrier Structure") or "Base Carrier Structure",
             "grid": "Grid B2", "z_depth_scale": "50%", "material_color": "weathered authentic"},
            {"name": "Foreground Floor Boundary", "grid": "Grid C2", "z_depth_scale": "15%", "material_color": "ground substrate"},
            {"name": "Rear Depth Wall", "grid": "Grid A2", "z_depth_scale": "85%", "material_color": "background boundary"}
        ],
        "frame_boundaries": frame_boundaries,
        "carrier_envelope": carrier_envelope,
        "entrance_topology": {
            "opening_plane": "vertical_axial",
            "entry_motion": "level_push",
            "turn_degrees": 0,
            "turn_direction": "none"
        },
        "camera_palette": {
            "default": camera_dna['lens'],
            "exterior": camera_dna['lens'],
            "interior": camera_dna['lens'],
        },
    }
    # 比例键：量到了才写。凭空写一个数比没有更糟——校验器会拿它当权威，而它是编的。
    if cast_scale:
        packet['cast_scale'] = cast_scale
    if worker_scale_percent:
        packet['worker_scale_percent'] = worker_scale_percent
    return packet


def _ensure_english_prompt_bodies(config, prompt_block, on_progress=None):
    """确保所有 IMAGE 与 VIDEO 分槽的提示词正文 100% 为英文。
    若模型因主题/标题混入中文而误输出了中文正文，进行确定性英文转译并保留分槽结构与中文说明。
    """
    if not prompt_block or not isinstance(prompt_block, str):
        return prompt_block

    parsed_images, parsed_videos = pp._parse_prompt_slots(prompt_block)
    has_chinese_body = False
    for item in list(parsed_images.values()) + list(parsed_videos.values()):
        body = item.get('body', '') if isinstance(item, dict) else str(item)
        # 提示词正文主体若含有大量中文字符（超过 5 个汉字），判定为中文正文
        if len(re.findall(r'[\u4e00-\u9fa5]', body)) > 5:
            has_chinese_body = True
            break

    if not has_chinese_body:
        return prompt_block

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': '检测到提示词正文包含中文，正在转换为 100% 英文生产提示词…',
        })

    system_prompt = (
        "You are an expert AI prompt engineer specializing in translating and formatting image/video prompts "
        "for AI generation backends (Midjourney v6, FLUX.1 Pro, Kling AI, Hailuo, Sora, Runway, Imagen 3).\n\n"
        "Rules:\n"
        "1. Translate all prompt bodies into descriptive, high-quality, photorealistic ENGLISH.\n"
        "2. Keep the slot header lines e.g. '图片 1（中文说明）:' or '视频 1（中文说明）:' intact with their Chinese summaries.\n"
        "3. Output ONLY the translated prompt block without markdown code fences or conversational text."
    )
    user_prompt = f"Please translate all IMAGE and VIDEO prompt bodies in the following prompt block into pure descriptive ENGLISH:\n\n{prompt_block}"

    try:
        translated = pp._chat(config, system_prompt, user_prompt, temperature=0.3, timeout=90)
        cleaned = pp._strip_code_fences(translated).strip()
        chk_img, chk_vid = pp._parse_prompt_slots(cleaned)
        if len(chk_img) >= len(parsed_images) * 0.8:
            return cleaned
    except Exception as exc:
        if sys.stdout:
            print(f"[FAST_COMPOSER] 英文提示词转译异常: {exc}")
    return prompt_block


def _resolve_job_dir(state):
    """这条 job 在磁盘上的目录。state 里没有 job_dir 这个键（39 个字段里就没有），
    所以只能从 job_id 折出来；折不出时退回 compose_state 的所在目录。"""
    direct = (state or {}).get('job_dir')
    if direct and os.path.isdir(direct):
        return direct
    job_id = (state or {}).get('job_id')
    if job_id:
        try:
            from replica_pipeline import job_dir as _job_dir
            cand = _job_dir(job_id)
            if os.path.isdir(cand):
                return cand
        except Exception:
            pass
    cs_path = (state or {}).get('compose_state_path')
    if cs_path:
        cand = os.path.dirname(cs_path)
        if os.path.isdir(cand):
            return cand
    return None


def _load_job_overview(state):
    """读这条 job 的 video_overview.json（送审帧名册就在这里面）。

    `state['overview']` 是给前端看的摘要，**不含 review_sampling**，拿它去找参考帧
    永远是空手而归。深度链路用的是 replica_pipeline._load_overview，这里对齐同一口径。
    """
    jd = _resolve_job_dir(state)
    if jd:
        ov_path = os.path.join(jd, 'video_overview.json')
        try:
            with open(ov_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and loaded:
                return loaded
        except (OSError, ValueError):
            pass
    # 读盘失败时才退回 state 里的副本：调用方（含测试替身）可能直接把完整 overview
    # 塞进 state，那份是能用的；摘要那份则会在下游自然判空。
    for key in ('video_overview', 'overview'):
        cand = (state or {}).get(key)
        if isinstance(cand, dict) and ((cand.get('review_sampling') or {}).get('frames')):
            return cand
    return {}


# 极速直通通道产不出来的东西。这条清单必须**当场说出口**：设置面板上「提示词链路」
# 选了 Omni 多镜头，而这条通道只有两份系统提示词（微缩 / 真实尺度），两份写的都是
# 单镜头正文——不说的话，用户看到的是"我明明选了多镜头"，而交付的每一条 VIDEO 都是
# 一镜到底，且没有任何地方能看出为什么。多镜头语法只有深度通道按
# active_skill_profile 分派 OmniComposer 才有。
#
# 只报不改：极速通道要不要补 omni 分支是另一件事（多镜头切点表、分镜字数上限、
# 镜头语法校验都要跟着来），不该在一个"告知"里顺手半做。
def _profile_is_explicit(config):
    """用户是不是在设置里显式钉了链路（而不是 auto 跟随视频模型）。"""
    import server_common
    raw = (os.environ.get('SKILL_PROFILE')
           or (config or {}).get('skillProfile')
           or server_common.SERVER_CONFIG.get('skillProfile') or 'auto')
    return str(raw).strip().lower() not in ('', 'auto')


def announce_fast_lane_limits(config, is_miniature, on_progress=None):
    """把极速直通通道**做不到**的那部分设置当场讲清楚。返回给出的提示列表。"""
    try:
        profile = pp.active_skill_profile(config)
    except Exception:
        return []

    notes = []
    if profile == 'omni':
        notes.append(
            '⚠️ 本单走的是极速直通合成，只产单镜头视频正文；'
            '「提示词链路」当前是 Omni 多镜头组接，这条通道上不生效——'
            '要多镜头切点语法，请把合成模式切到深度合成（composeMode=deep）后重新合成')
    # 显式钉了链路、题材证据却把本单判成微缩（或反过来），也要说一句：题材由原片
    # 证据定（reverse.detect_miniature_scale），链路只管视频语法，两者不是一回事。
    if _profile_is_explicit(config):
        if is_miniature and profile != 'miniature':
            notes.append(
                f'ℹ️ 本单按微缩沙盘口径写（题材由原片证据判定），与「提示词链路」当前的 '
                f'{profile} 不冲突：链路管视频语法，题材管画面里是什么')

    for note in notes:
        if sys.stdout:
            print(f'[FAST_COMPOSER] {note}')
        if on_progress:
            on_progress('replica_stage', {'stage': 'compose', 'message': note})
    return notes

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
    # 活物一律真人：这一栏是系统提示词里 cast_desc 的正文，写着 figurine 的话
    # 写手会照抄进每一条 IMAGE。老任务（本次改动之前反推的）落盘时没过归一，
    # 在这里补一道。见 prompt_pipeline.human_cast。
    cast_identity = pp.humanize_cast_list(list(beats_doc.get('cast_identity') or []))
    scene_signature = beats_doc.get('scene_signature') or state.get('scene_signature') or ''
    mutation_axes = beats_doc.get('mutation_axes') or (state.get('mutation_config') or {}).get('axes') or {}

    # 走不走微缩沙盘那套系统提示词，判据只有一处：reverse.detect_miniature_scale。
    #
    # 旧判据把 `'craftsman' in cast_identity` 当微缩证据（"巨手工匠"），而 craftsman
    # 就是「手艺人/工匠」——真人施工片里最常见的自称之一。2026-08-30 实测
    # replica_af8db0d7a95f：一单真人实拍的半挂车改造，识别项写着 "Caucasian male
    # builder/craftsman in his 30s…"，一个微缩字样都没有，整单却被路由进微缩通道，
    # 交付正文 39 处 miniature、33 处 giant hands、8 处 diorama，施工者成了拇指高的
    # 模型人。判定还全程静默，出图之前没人看得出走错了道。
    is_miniature, miniature_reason = reverse.detect_miniature_scale(
        beats_doc, title=state.get('title'), config=config)
    # 判定必须说出口。上面那次误判之所以烧掉一整单，是因为它从头到尾没有一行声音。
    if sys.stdout:
        print(f"[FAST_COMPOSER] 合成通道：{'微缩沙盘' if is_miniature else '真实尺度'}"
              f"（依据：{miniature_reason}）")
    if on_progress:
        on_progress('replica_stage', {
            'stage': 'compose',
            'message': (f"合成通道：{'微缩沙盘（巨手 + 拇指高住户）' if is_miniature else '真实尺度（真人施工）'}"
                        f"——依据：{miniature_reason}"),
        })
    announce_fast_lane_limits(config, is_miniature, on_progress=on_progress)

    # Rule 17: 规范化常驻人偶描述，剔除"衣着光鲜"偏见，强制为落魄流浪灾民做旧体态
    if is_miniature and cast_identity:
        # 尺度记号补齐。系统提示词里 cast_desc 的**兜底**文案写的是「1:24 scale …
        # roughly a thumb tall」，而真实抽出来的 cast_identity 往往只有 "slim build"
        # 加一身衣服、一个尺寸字都没有——于是抽到了真数据反而把唯一那句尺度声明顶掉，
        # 「知道得越多锁得越松」。2026-08-30 实测（replica_cf9a445bc52b）三条识别项
        # 全部无尺度记号，交付的九帧人偶占比随机漂。
        #
        # 补的比例来自原片逐帧读数投票（measure_cast_scale），量不到就不补——凭空写一个
        # 比例比没有更糟。
        scale_hint = ''
        try:
            from prompt_pipeline.observed_grounding import measure_cast_scale
            measured = measure_cast_scale(beats_doc, _resolve_job_dir(state),
                                          cast_identity=cast_identity)
            if measured:
                scale_hint = measured.split('—')[0].strip()   # 只要 "1:24 scale" 这半句
        except Exception:
            scale_hint = ''

        normalized_cast = []
        for c in cast_identity:
            c_str = str(c)
            c_str = re.sub(r'\bclean\s+', 'distressed faded ', c_str, flags=re.IGNORECASE)
            c_str = re.sub(r'\bbrand[-\s]?new\b', 'worn-out', c_str, flags=re.IGNORECASE)
            c_str = re.sub(r'\bexquisite\b', 'weathered', c_str, flags=re.IGNORECASE)
            # 「这一条说的是住在场景里那几个拇指高的人」——判据不能再写成
            # `'figurine' in c_str`：活物一律真人（human_cast）之后这一栏落地就是
            # "real living human — …male person"，一个 figurine 字都没有，于是做旧
            # 前缀与比例补齐双双静默失效（改了产地没改验收口径）。微缩线里除了「巨手
            # 工匠」那条真人尺度的手，其余活物都是住户。
            is_resident = not _IS_GIANT_HAND_RE.search(c_str)
            if is_resident and not any(w in c_str.lower() for w in ['impoverished', 'destitute', 'weathered', 'ragged', 'faded']):
                c_str = f"impoverished destitute refugee resident: {c_str}"
            # 只给住户补，不给「巨手工匠」那条补——那是真人尺度的手，补个 1:24 就反了。
            if (scale_hint and is_resident
                    and not re.search(r'\b1\s*[:：]\s*\d', c_str)):
                c_str = f"{scale_hint} {c_str}"
            normalized_cast.append(c_str)
        cast_identity = normalized_cast

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
    # 前置：把逐帧读数压成事实卡随输入一起发。零额外模型调用——依据是反推阶段已经落盘
    # 的 frame_facts.json。算不出来（老任务没有 frame_facts / 变体单）就发空串，行为回到
    # 改动之前。
    # run_compose 已经算好一份挂在 config 上（两条链路共用）；独立调用时自己算。
    observed_digests = (config or {}).get('_observed_digests') or state.get('_observed_digests')
    if observed_digests is None:
        try:
            from prompt_pipeline.observed_grounding import build_observed_digests
            # 微观痕迹只发给微缩通道（见 pp.micro_traces_channel_enabled）。主通路上
            # 这份事实卡是 replica_pipeline 算好挂在 config 上的、已经过同一道门控；
            # 这里是独立调用时的兜底，判据必须一致，否则同一条快线两个入口两种口径。
            observed_digests = build_observed_digests(
                beats_doc, _resolve_job_dir(state),
                include_micro_traces=pp.micro_traces_channel_enabled(config))
        except Exception as exc:
            observed_digests = None
            if sys.stdout:
                print(f'[FAST_COMPOSER] 原片实拍事实卡构建软退: {exc}')
    observed_block = ''
    if observed_digests:
        try:
            from prompt_pipeline.observed_grounding import observed_digest_block
            observed_block = observed_digest_block(observed_digests, total_beats)
        except Exception:
            observed_block = ''

    user_prompt = build_fast_composer_user_prompt(
        title=title,
        theme=theme,
        beats_list=beats_list,
        banned_elements=banned_elements,
        scene_constants=scene_constants,
        mutation_axes=mutation_axes,
        scene_signature=scene_signature,
        cast_identity=cast_identity,
        observed_block=observed_block,
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
    prompt_block = _ensure_english_prompt_bodies(config, prompt_block, on_progress=on_progress)

    # 解析 slots
    parsed_images, parsed_videos = pp._parse_prompt_slots(prompt_block)
    compiled_images = {k: (v.get('body') if isinstance(v, dict) else str(v)) for k, v in parsed_images.items()}
    compiled_videos = {k: (v.get('body') if isinstance(v, dict) else str(v)) for k, v in parsed_videos.items()}

    # 微缩模式主动后处理：确保人偶动态应激与巨人手施工口径一致
    if is_miniature:
        try:
            from prompt_pipeline.composers.miniature import MiniatureComposer
            mini_comp = MiniatureComposer()
            for k, v in compiled_videos.items():
                beat_k = beats_list[k - 1] if (beats_list and k - 1 < len(beats_list)) else None
                fixed_v = mini_comp.fix_miniature_video(v)
                fixed_v = mini_comp.ensure_living_cast_reaction(fixed_v, beat=beat_k)
                compiled_videos[k] = fixed_v
        except Exception as exc:
            if sys.stdout:
                print(f"[FAST_COMPOSER] 微缩提示词人偶应激后处理提示: {exc}")

    # 自然力学兜底：极速直出没有走 apply_proactive_fixes/fix_pacing_control 那条确定性
    # 链路（只有微缩线上面那几行有后处理），非微缩线的工人动作全靠系统提示词一次成型、
    # 没有任何保底。这里补一道跟深度线同款的确定性兜底（pp.fix_natural_body_mechanics，
    # 与 EVEN_RATE 同一模式），出现人体动作时缺了自然力学正向要求就补一句，有则不动。
    compiled_videos = {k: pp.fix_natural_body_mechanics(v) for k, v in compiled_videos.items()}

    # 自动进行跨帧人物位置重复度清洗与动态迁移，杜绝连续左下角站桩 (Rule 13 & Rule 15)
    if compiled_images:
        compiled_images = pp.fix_repetitive_cast_positioning(compiled_images, beats_list)

    # 锚点槽位不一定是 1（定向补合成的子集可能从别的号起头）。对齐结果必须写回**这个**
    # 号，不能一律写进 1 号——那样等于给一个不存在的槽位挂了一份对齐稿，而真正要出图的
    # 那一号仍然是没对齐的原稿。
    anchor_key = 1 if 1 in compiled_images else (sorted(parsed_images.keys())[0] if parsed_images else None)
    image_1_prompt = compiled_images.get(anchor_key, '') if anchor_key is not None else ''

    # 锚点图如果能找到原片真实首帧，自动走 ground_anchor_on_reference 进行像素级空间与材质校正
    reference = None
    if not reverse.is_variant_doc(beats_doc):
        # 送审帧名册只在磁盘上那份 video_overview.json 里。state['overview'] 是一份
        # 摘要（path/collage/duration/sampling…），**没有 review_sampling**——2026-08-30
        # 复盘：这里原先写 `state.get('overview') or {}`，摘要非空于是读盘兜底永远进不
        # 去，`anchor_reference_frame` 拿着空名册返回 None，整段对齐静默空转。深度链路
        # 走的是 `_load_overview`（按 job_id 读盘），两条线因此一条对齐、一条不对齐，而
        # prompt_block 上看不出区别。统一到同一个取数口径。
        overview = _load_job_overview(state)
        reference = reverse.anchor_reference_frame(beats_doc, overview)
        if not reference:
            for sdir_name in ('review_frames', 'storyboard'):
                sdir = os.path.join(_resolve_job_dir(state) or '', sdir_name)
                if sdir and os.path.isdir(sdir):
                    cands = sorted([os.path.join(sdir, fn) for fn in os.listdir(sdir) if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                    if cands:
                        reference = cands[0]
                        break

    if reference and image_1_prompt:
        try:
            image_1_prompt = reverse.ground_anchor_on_reference(config, image_1_prompt, reference, on_progress=on_progress)
            compiled_images[anchor_key] = image_1_prompt
        except Exception as exc:
            if sys.stdout:
                print(f"[FAST_COMPOSER] 首帧参考图空间对齐软退: {exc}")
    elif image_1_prompt and not reverse.is_variant_doc(beats_doc):
        # 找不到参考帧不是异常，走不到上面那个 except——不出声就等于悄悄交付一份纯靠
        # 文字空想的锚点图，而锚点图的材质与光线会被后面每一拍继承。必须说出来。
        if sys.stdout:
            print('[FAST_COMPOSER] ⚠️ 未找到原片送审帧，本轮锚点图（IMAGE 1）未做像素对齐，'
                  '构图/景别/尺度可能与原片首帧对不上')
        if on_progress:
            on_progress('replica_stage', {
                'stage': 'compose',
                'message': '⚠️ 未找到原片送审帧，锚点图未照原片首帧对齐（构图可能与对标帧不符）',
            })

    # 上面几道后处理（人偶应激/自然力学兜底、跨帧站位去重、锚点像素对齐）都只改了
    # compiled_images / compiled_videos 这两份字典——必须统一写回 parsed_images /
    # parsed_videos 并重建 prompt_block，否则修复止步于内存里的一份影子副本，从不出现
    # 在真正交付的文本里。2026-08-30 复盘：这条回写此前只在锚点对齐分支里做一次，锚点
    # 对齐一旦没找到参考帧（常见情形，见上面的告警分支）——连同一批的人偶应激修复、
    # 站位去重修复也一起被静默吞掉，prompt_block 上一个字都没变。
    # 活物一律真人（见 prompt_pipeline.human_cast）。极速通道不经过深度线的
    # apply_proactive_fixes 收口，这里是它自己的交付边界：写手把人写成 figurine 时
    # 就地掰回真人措辞。放在回写之前，两份字典与最终 prompt_block 才是同一份文本。
    compiled_images = {k: pp.humanize_prompt_text(v) for k, v in compiled_images.items()}
    compiled_videos = {k: pp.humanize_prompt_text(v) for k, v in compiled_videos.items()}

    for k, v in compiled_images.items():
        if k in parsed_images:
            if isinstance(parsed_images[k], dict):
                parsed_images[k]['body'] = v
            else:
                parsed_images[k] = v
    for k, v in compiled_videos.items():
        if k in parsed_videos:
            if isinstance(parsed_videos[k], dict):
                parsed_videos[k]['body'] = v
            else:
                parsed_videos[k] = v
    prompt_block_raw = pp._format_prompt_block(parsed_images, parsed_videos)
    prompt_block = _ensure_prompt_block_summaries(prompt_block_raw, beats_doc)

    beat_ladder = beats_to_ladder_payload(beats_list)
    # 人偶物理比例：从原片逐帧读数里量（见 observed_grounding.measure_cast_scale）。
    # 量不到就留空——packet 里没有这个键，下游的锁自然不注入，绝不凭空造一个比例。
    cast_scale = None
    try:
        from prompt_pipeline.observed_grounding import measure_cast_scale
        cast_scale = measure_cast_scale(beats_doc, _resolve_job_dir(state),
                                        cast_identity=cast_identity)
    except Exception as exc:
        if sys.stdout:
            print(f'[FAST_COMPOSER] 人偶比例测量软退: {exc}')
    packet = synthesize_drift_lock_packet(
        resolved_theme, beats_list, carrier=carrier, is_miniature=is_miniature,
        cast_scale=cast_scale)

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
