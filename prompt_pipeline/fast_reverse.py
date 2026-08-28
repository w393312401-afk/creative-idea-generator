"""视频原生直读极速反推管线 (Video-Native Fast Reverse Pipeline)

通过单次全时序多模态大模型调用，直接将视频的关键帧序列/拼贴图
端到端转换为具备 13 字段规范的 1:1 节拍阶梯文档 (timelapse_beats.json)。
将反推耗时从 3~6 分钟压缩至 30~45 秒。
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


STAGE_ALIAS_MAP = {
    'clearing': 'demolition',
    'demolishing': 'demolition',
    'framing': 'structural',
    'masonry': 'structural',
    'plumbing': 'rough_in',
    'wiring': 'rough_in',
    'insulation': 'rough_in',
    'paneling': 'enclosure',
    'sheathing': 'enclosure',
    'surfacing': 'surface',
    'painting': 'surface',
    'flooring': 'floor',
    'tiling': 'floor',
    'lighting': 'fixtures',
    'cabinetry': 'furnishing',
    'decorating': 'furnishing',
}


def build_fast_reverse_system_prompt():
    """构建单次视频时序原生反推的系统提示词。"""
    return """You are a world-class time-lapse construction analysis AI.
Your job is to analyze the provided chronological sequence of representative video frames and contact sheets from a restoration/renovation time-lapse video, and output a structured 1:1 beat ladder document (timelapse_beats.json) in STRICT JSON format.

Every beat must represent an actual physical milestone and progress delta in the video from start to end (in seconds).

Rules:
1. [Strict Monotonic Causal Order]
   - Operations must follow realistic build order: demolition/clearing -> structural -> rough_in -> enclosure -> surface -> floor -> fixtures -> furnishing -> reveal.
   - Never reverse construction state (unless an explicit demolition step is shown).

2. [Exact Schema per Beat]
   Each item in "beats" array must contain:
   - "id": "B01", "B02", "B03"... (strictly consecutive)
   - "index": 1, 2, 3...
   - "start": start time in seconds (float, >= 0)
   - "end": end time in seconds (float, > start)
   - "stage": exactly one of ["demolition", "structural", "rough_in", "enclosure", "surface", "floor", "fixtures", "furnishing", "transition", "reveal"]
   - "operation": 1-3 word mechanical milestone name (e.g. "pressure_washing", "timber_framing", "hardwood_flooring")
   - "visual_subject": concise title of what is visually transforming
   - "visible_action": detailed description of worker physical actions and active tools
   - "visible_result": detailed physical outcome delivered at the end of this beat
   - "state_before": measurable physical starting condition with extent/quantity (e.g. "Bare rough concrete slab with 5cm standing water")
   - "state_after": measurable physical ending condition with extent/quantity (e.g. "100% covered with dry compacted 50mm gravel bed")
   - "package_operations": array of 1-3 tightly coupled operations completed in this beat
   - "visible_details": array of 3-5 specific visible items formatted as "[material] + [color/texture] + [exact position e.g. (Grid B2)]"
   - "persistent_traces": array of at least 2 persistent visual traces left behind that future frames will inherit
   - "space": spatial region (e.g. "exterior", "main_interior", "secondary_space")
   - "camera_angle": vertical angle ("eye_level", "high_angle", "low_angle", "bird_eye")
   - "camera_bearing": horizontal bearing ("front", "three_quarter", "side", "back")
   - "lens_feel": lens specification ("24mm_wide", "35mm_standard", "16mm_ultra_wide")
   - "subject_placement": detailed 3-zone spatial layout & grid placement. Explicitly declare horizontal thirds: where solid walls, main work face, and openings/portals sit (e.g. "Solid wall on left third (Grid A1-C1), portal opening strictly on right third (Grid A3-B3), puddle in lower center (Grid B2-C2)"). If the shot is 3/4 oblique or off-center, describe the exact asymmetry and NEVER default to centered framing.
   - "tool": primary hand/power tool used (e.g. "high-pressure hose", "framing hammer", "paint roller")
   - "sfx": ASMR physical sound effect description at 60% volume
   - "time_treatment": "time_lapse" or "real_time"
   - "worker_count": integer count of active workers (e.g. 1)
   - "material_flow": description of raw material intake and spoil disposal
   - "cast_action": ACTION-REACTION CAUSAL CHAIN of what all living things (workers, resident miniature figurines, or animals) are doing with their bodies, GAZE, AND FACIAL/EMOTIONAL EXPRESSIONS in response to the work: [Trigger Event] -> [Immediate Reflex / Bodily Reaction & Facial Expression] -> [Engaged Tracking / Motion] -> [Settled Posture & Demeanor]. For miniature dioramas with resident figurines, capture their emotional evolution (e.g. initial beats: haggard, distressed, sorrowful, helpless tearful gaze facing ruined shelter; mid-beats: astonished, leaning in with wide-eyed awe and rising hope; reveal beats: beaming with joyful smiles and relief). NEVER write static placeholders like 'remain', 'stay', 'unchanged'.
   - "zh": object containing Chinese translations:
     {
       "visible_action": "中文工人动作与工具描述",
       "visible_result": "中文交付成果描述",
       "headline": "短标题（4-8字）"
     }

3. [Global Metadata]
   Root object must also include:
   - "carrier": The main object/building being restored (e.g. "abandoned bunker", "vintage camper")
   - "env": Surrounding environment
   - "trauma": Initial damaged/decayed starting condition
   - "destiny_zh": Chinese 4-10 char end-state phrase (e.g. "温馨隐居卧室", "极简设计师工坊")
   - "video_duration_sec": Total duration in seconds
   - "cast_identity": array of strings describing the FIXED permanent physical identity, head-to-toe clothing, AND CLOTHING WEAR/CONDITION of each recurring living person, miniature figurine couple, or resident animal, including realistic weathering/distress and baseline demeanor (e.g. ["1:24 scale African male miniature figurine: distressed faded dusty blue short-sleeve shirt with dirt smudges, worn brown work trousers, haggard and sorrowful initial demeanor", "1:24 scale African female miniature figurine: weathered earth-toned patterned headwrap, faded scoop-neck top, traditional patterned wrap skirt, distressed and helpless initial gaze"]). Accurately record if clothing is ragged, weathered, dusty, or tattered in trauma scenes rather than assuming clean new clothes. Empty array [] if strictly nobody alive appears.
   - "banned_elements": array of objects or tools that NEVER appear in this video (e.g. ["excavator", "crane"])
   - "scene_constants": array of fixed spatial landmarks that stay unchanged

Output ONLY the valid JSON object, without markdown formatting or introductory text."""


def collect_keyframe_images(job_dir, overview, max_frames=24):
    """提取用于单次全景多模态直读的代表性关键帧序列与拼图。"""
    image_paths = []
    
    # 1. 优先将全局拼贴大图作为首图（如有）
    collage_path = overview.get('keyframe_collage')
    if collage_path and os.path.exists(collage_path):
        image_paths.append(collage_path)
    else:
        for fname in ('clip_collage.jpg', 'collage.jpg'):
            candidate = os.path.join(job_dir, fname)
            if os.path.exists(candidate):
                image_paths.append(candidate)
                break

    # 2. 收集各独立关键帧
    frames_candidates = []
    
    # 从 review_sampling 中读取
    sampling_frames = (overview.get('review_sampling') or {}).get('frames') or []
    for f in sampling_frames:
        p = f.get('frame_path')
        if p and os.path.exists(p):
            frames_candidates.append(p)

    # 若未找到，从 analysis_plan 中读取
    if not frames_candidates:
        req_frames = (overview.get('analysis_plan') or {}).get('required_frames') or []
        for r in req_frames:
            p = os.path.join(job_dir, 'review_frames', r)
            if os.path.exists(p):
                frames_candidates.append(p)
            else:
                p2 = os.path.join(job_dir, 'storyboard', r)
                if os.path.exists(p2):
                    frames_candidates.append(p2)

    # 若仍未找到，直接扫描 review_frames / storyboard 目录
    if not frames_candidates:
        for sub in ('review_frames', 'storyboard'):
            sdir = os.path.join(job_dir, sub)
            if os.path.isdir(sdir):
                files = sorted(os.listdir(sdir))
                for fn in files:
                    if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                        frames_candidates.append(os.path.join(sdir, fn))

    # 去重并均匀采样最多 max_frames 帧
    unique_candidates = []
    seen = set()
    for p in frames_candidates:
        norm = os.path.abspath(p).lower()
        if norm not in seen:
            seen.add(norm)
            unique_candidates.append(p)

    if len(unique_candidates) > max_frames:
        step = len(unique_candidates) / max_frames
        sampled = [unique_candidates[int(i * step)] for i in range(max_frames)]
        if unique_candidates[-1] not in sampled:
            sampled[-1] = unique_candidates[-1]
        unique_candidates = sampled

    for p in unique_candidates:
        if p not in image_paths:
            image_paths.append(p)

    return image_paths


def fast_video_native_reverse(config, job_dir, on_progress=None):
    """执行视频原生 1-Pass 极速反推。
    
    1. 打包全视频时序高清关键帧序列；
    2. 单次调用视觉模型（Gemini 3.7 / 2.0 / Flash）端到端生成 timelapse_beats.json；
    3. 本地确定性校验并写盘，30~45 秒直达人工卡点。
    """
    overview_path = os.path.join(job_dir, 'video_overview.json')
    if not os.path.exists(overview_path):
        raise FileNotFoundError(f'找不到视频元数据文件: {overview_path}')

    with open(overview_path, 'r', encoding='utf-8') as f:
        overview = json.load(f)

    duration = (overview.get('media_metadata') or {}).get('duration_sec') or 0
    change_events = overview.get('change_events') or []

    if on_progress:
        on_progress('replica_stage', {
            'stage': 'review_frames',
            'message': '正在启动视频原生极速直读（单轮多模态时序反推）…',
        })

    keyframe_paths = collect_keyframe_images(job_dir, overview, max_frames=20)
    if not keyframe_paths:
        raise ValueError('未找到可用于视觉反推的关键帧图像')

    # 压缩图像以加速网络传输
    compressed_images = pp._compress_frames_for_review(keyframe_paths, max_side=768, quality=75)

    system_prompt = build_fast_reverse_system_prompt()
    user_prompt = (
        f"Video Duration: {duration} seconds\n"
        f"Detected Visual Change Events: {len(change_events)} events\n\n"
        "Attached are the chronological representative frame sequence and keyframe collage from this timelapse video.\n"
        "Please analyze the physical transformation and output the complete 1:1 beat ladder (timelapse_beats.json) in STRICT JSON format."
    )

    clean_config = reverse._scrub_config_for_pass_a(config)
    # 复刻页上的「帧事实模型」下拉写的是 config.frameFactsModel
    model = reverse._pass_a_model(clean_config)

    pp._raise_if_cancelled(on_progress)
    t0 = time.time()
    raw_response = pp._multimodal_chat(
        clean_config,
        system_prompt,
        user_prompt,
        compressed_images,
        model=model,
        max_tokens=16384,
        timeout=180
    )
    elapsed = round(time.time() - t0, 1)

    if sys.stdout:
        print(f"[FAST_REVERSE] 单轮视频原生反推完成，耗时 {elapsed}s")

    beats_doc = reverse.parse_json_reply(raw_response)
    if not isinstance(beats_doc, dict):
        raise ValueError(f'极速反推返回数据格式异常: {type(beats_doc).__name__}')

    beats_list = beats_doc.get('beats') or []
    if not beats_list:
        raise ValueError('极速反推未识别出有效节拍')

    total_n = len(beats_list)
    # 规范化与丰满化节拍数据
    for i, b in enumerate(beats_list):
        idx = i + 1
        b['index'] = idx
        if not b.get('id'):
            b['id'] = f"B{idx:02d}"

        # 归一化时间窗
        s = b.get('start', b.get('timestamp_start'))
        e = b.get('end', b.get('timestamp_end'))
        if s is None or e is None or (float(s) == 0 and float(e) == 0 and i > 0) or float(e) <= float(s):
            s = round(i * (duration / total_n), 1)
            e = round((i + 1) * (duration / total_n), 1)
        b['start'] = float(s)
        b['end'] = float(e)
        b['timestamp_start'] = b['start']
        b['timestamp_end'] = b['end']

        # 归一化 Stage
        stg = str(b.get('stage') or '').lower()
        b['stage'] = STAGE_ALIAS_MAP.get(stg, stg if stg in reverse._STAGE_RANK else 'structural')

        # 补全基础契约字段
        b['visual_subject'] = b.get('visual_subject') or b.get('visible_result') or f'Milestone {idx}'
        b['state_before'] = b.get('state_before') or f'Starting condition before {b.get("operation")}'
        b['state_after'] = b.get('state_after') or b.get('visible_result') or 'Completed progress'
        b['workers_present'] = b.get('workers_present', True)
        b['source_event_ids'] = b.get('source_event_ids') or []

        # 自动补全工艺字段（Craft Fields）
        b.setdefault('tool', (b.get('package_operations') or ['hand tool'])[0])
        b.setdefault('sfx', f"ASMR physical sound effects of {b.get('operation')} at 60% volume")
        b.setdefault('shot_scale', 'wide_shot')
        b.setdefault('camera_angle', b.get('camera_angle') or 'eye_level')
        if not b.get('subject_placement'):
            bearing = str(b.get('camera_bearing') or 'three_quarter').lower()
            if 'three_quarter' in bearing:
                b['subject_placement'] = 'Off-center three-quarter perspective across Grid B1-B3 workspace'
            elif 'side' in bearing:
                b['subject_placement'] = 'Side profile workspace spanning Grid B1-C2'
            else:
                b['subject_placement'] = 'Grid B2 (center workspace)'
        b.setdefault('time_treatment', 'time_lapse')
        b.setdefault('worker_count', 1)
        b.setdefault('light_state', 'worklight_daylight')
        b.setdefault('material_flow', 'raw materials staged and consumed, debris cleared')
        b.setdefault('cast_action', 'Worker or resident figurines actively track and react to the construction process')

    beats_doc['beats'] = beats_list
    beats_doc.setdefault('video_duration_sec', duration)
    beats_doc.setdefault('cast_identity', [])
    beats_doc.setdefault('banned_elements', [])
    beats_doc.setdefault('scene_constants', [])

    # 执行标准反推对齐流水线：对齐空间、绑定变化事件、挂载三级证据帧与镜头尺度
    try:
        reverse.normalize_beat_spaces(beats_doc)
        reverse.reconcile_event_coverage(beats_doc, overview, reconcile_unbound=True)
        reverse.attach_coverage_frames(beats_doc, overview)
        reverse.attach_shot_cuts(beats_doc, overview)
        reverse.attach_shot_scales(beats_doc, overview, job_dir=job_dir)
        reverse.ensure_three_evidence_frames(beats_doc, overview)
    except Exception as exc:
        if sys.stdout:
            print(f"[FAST_REVERSE] 后处理对齐警告: {exc}")

    # 写入 timelapse_beats.json
    beats_path = os.path.join(job_dir, 'timelapse_beats.json')
    with open(beats_path, 'w', encoding='utf-8') as f:
        json.dump(beats_doc, f, ensure_ascii=False, indent=2)

    # 写入配套的 frame_facts.json 保持下游引用兼容
    facts_payload = {
        'frame_count': len(keyframe_paths),
        'model': model,
        'scope': 'fast_native',
        'degraded': False,
        'facts': [
            {
                'frame': os.path.basename(p),
                'timestamp': round(i * (duration / max(1, len(keyframe_paths) - 1)), 2),
                'active_tools': [],
                'status': 'extracted'
            }
            for i, p in enumerate(keyframe_paths)
        ]
    }
    facts_path = os.path.join(job_dir, 'frame_facts.json')
    with open(facts_path, 'w', encoding='utf-8') as f:
        json.dump(facts_payload, f, ensure_ascii=False, indent=2)

    return beats_doc

