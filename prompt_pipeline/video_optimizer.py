"""Video Prompt Visual Delta Optimization Gate (视频生成前画面差量提示词优化门).

根据真实渲染落盘的帧序列画面（IMAGE i 与 IMAGE i+1），通过多模态 VLM 进行全域四区差量扫描
（顶域、中域、底域、物料与废料流向），结合动作-工具-音效三位一体与公制尺度守恒，优化并重写视频提示词
（VIDEO i），彻底消除 I2V 生成过程中的画面跳变、突变、凭空变化与空心定格。
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime

import server_common


def _find_frame_file(project_dir, sequence):
    """查找指定序号的帧图片文件路径（优先 .webp，其次 .png / .jpg / .jpeg）。"""
    frames_dir = os.path.join(project_dir, 'frames')
    if not os.path.isdir(frames_dir):
        return None
    for ext in ('.webp', '.png', '.jpg', '.jpeg'):
        p = os.path.join(frames_dir, f'img_{sequence:03d}{ext}')
        if os.path.exists(p):
            return p
    return None


def _get_file_fingerprint(file_path):
    """计算单个帧文件的快速内容指纹（MD5 + 文件大小）。"""
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        stat = os.stat(file_path)
        h = hashlib.md5()
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return f"{stat.st_size}:{h.hexdigest()}"
    except Exception:
        try:
            stat = os.stat(file_path)
            return f"{stat.st_size}:{stat.st_mtime_ns}"
        except Exception:
            return None


def _get_slot_delta_fingerprint(start_path, end_path, video_meta=None, spatial_contract=None):
    """计算槽位差量优化的环境指纹。当起止帧内容或元数据变化时，指纹改变。"""
    fp_start = _get_file_fingerprint(start_path)
    fp_end = _get_file_fingerprint(end_path)
    if not fp_start or not fp_end:
        return None
    meta_str = str(video_meta or '').upper().strip()
    contract_str = ""
    if isinstance(spatial_contract, dict) and spatial_contract:
        dim = spatial_contract.get('carrier_envelope', {})
        if dim:
            contract_str = json.dumps(dim, sort_keys=True)
    return f"s:{fp_start}|e:{fp_end}|m:{meta_str}|c:{contract_str}"


def _persist_optimization_results(project_dir, title, prompt_block, optimizations):
    """原子持久化优化结果至 manifest.json 和 .stepped_pipeline.json。"""
    try:
        from prompt_pipeline import prompt_slots_list
        with server_common.manifest_lock(project_dir):
            cur_manifest = server_common.read_manifest(project_dir) or {'title': title, 'frames': []}
            cur_manifest['prompt_block'] = prompt_block
            cur_manifest['prompt_slots'] = prompt_slots_list(prompt_block)
            cur_manifest['video_prompt_optimizations'] = optimizations
            server_common.write_manifest(project_dir, cur_manifest)
    except Exception as e:
        if sys.stdout:
            print(f"[VIDEO OPTIMIZER] Warning: failed to persist updated prompt_block to manifest: {e}")

    try:
        stepped_state_path = os.path.join(project_dir, '.stepped_pipeline.json')
        if os.path.exists(stepped_state_path):
            with open(stepped_state_path, 'r', encoding='utf-8') as f:
                st_state = json.load(f)
            if isinstance(st_state, dict):
                st_state['prompt_block'] = prompt_block
                st_state['video_prompt_optimizations'] = optimizations
                st_state['updated_at'] = datetime.now().isoformat()
                with open(stepped_state_path, 'w', encoding='utf-8') as f:
                    json.dump(st_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        if sys.stdout:
            print(f"[VIDEO OPTIMIZER] Warning: failed to sync stepped state: {e}")


def optimize_single_video_prompt(config, start_frame_path, end_frame_path, original_video_prompt,
                                 slot_index=None, start_seq=None, end_seq=None,
                                 video_meta='', spatial_contract=None):
    """针对单段视频提示词（VIDEO i），基于真实的起始帧与结束帧图像进行多模态视觉差量分析与优化。

    - start_frame_path: 起始锚点帧图像路径 (IMAGE i)
    - end_frame_path: 结束锚点帧图像路径 (IMAGE i+1)
    - original_video_prompt: 原始视频提示词正文
    - slot_index: 视频槽位号 (i)
    - start_seq / end_seq: 起始帧号与结束帧号（默认 start_seq=slot_index, end_seq=slot_index+1）
    - video_meta: 槽位元数据标签 (如 [BRIDGE], [CUT], [HERO] 等)
    - spatial_contract: 空间契约/Drift Lock 数据（可选）

    返回优化后的视频提示词字符串。若 VLM 调用失败或发生异常，安全降级返回原始提示词。
    """
    if not original_video_prompt or not original_video_prompt.strip():
        return original_video_prompt

    if not start_frame_path or not os.path.exists(start_frame_path):
        return original_video_prompt

    # 若为单帧槽位（如无结束帧的独立片段），且无 end_frame_path，则无法做两帧差量对比，直接返回
    if not end_frame_path or not os.path.exists(end_frame_path):
        return original_video_prompt

    s_idx = start_seq if start_seq is not None else (slot_index if slot_index is not None else 1)
    e_idx = end_seq if end_seq is not None else (s_idx + 1)
    meta_str = str(video_meta or '').upper()
    is_crossing = 'BRIDGE' in meta_str or 'CUT' in meta_str

    contract_info = ""
    if isinstance(spatial_contract, dict) and spatial_contract:
        dim = spatial_contract.get('carrier_envelope', {})
        if dim:
            contract_info = f"\nSpatial Envelope Reference: {json.dumps(dim, ensure_ascii=False)}"

    crossing_rules = (
        "\nCRITICAL TWO-SHOT TRANSITION RULE FOR CROSSING SLOTS:\n"
        "- This slot is marked as a TRANSITION / BRIDGE slot.\n"
        "- If Shot A (Exterior Opening): The camera performs a 24mm forward push toward the entrance. "
        "Show explicit mechanical unlocking (turning hatch wheel, sliding bolt, unlatching). "
        "Strictly ZERO workers, ZERO tools, and ZERO construction work inside the frame.\n"
        "- If Shot B (Interior Entry): Camera is locked inside facing main depth axis; worker enters "
        "with tools to stage and deliver the first physical work."
        if is_crossing else ""
    )

    system_prompt = (
        "You are an expert visual supervisor and video prompt optimization engine for continuous "
        "time-lapse construction and renovation videos (interpolating between two keyframes for "
        "Google Veo / Omni video generation).\n\n"
        "You are provided with two ACTUAL rendered images in chronological order:\n"
        f"1. Image 1: The START anchor frame (IMAGE {s_idx})\n"
        f"2. Image 2: The END anchor frame (IMAGE {e_idx})\n\n"
        "Your task is to visually compare Image 1 and Image 2, identify all physical deltas between them, "
        "and REWRITE / OPTIMIZE the provided VIDEO prompt so that it 100% accurately, smoothly, and "
        "physically interpolates between these two exact images, completely preventing visual jumps, "
        "pops, teleportation, or ungrounded discrepancies.\n\n"
        "RULES & PROTOCOLS (MUST COMPLY STRICTLY):\n"
        "1. FULL-FIELD 4-ZONE DELTA SCANNING:\n"
        "   - Top / Overhead: Roof, rafters, skylights, ceiling panels, light fixtures, insulation.\n"
        "   - Middle / Facade & Walls: Wall finishes, masonry, cracks, partitions, window openings, conduit.\n"
        "   - Bottom / Floor: Bare ground, crushed gravel, waterproof membrane, floor joists, floorboards, rugs.\n"
        "   - Peripherals & Spoil: Waste stacks/crates accumulating, raw timber/materials visibly diminishing.\n"
        "2. ZERO PHANTOM CHANGES & ACTION-TOOL-SFX TRIAD:\n"
        "   - Every visible change between Image 1 and Image 2 MUST be physically delivered by worker action.\n"
        "   - Assign concrete action verbs, specific geometric hand/power tools (e.g. cordless drill, aluminum rake, "
        "utility knife, rubber mallet), and visible trace buildup (dust lines, screw heads, shavings).\n"
        "   - No ghost work: structures must NEVER appear or melt away without an active worker.\n"
        "3. HUMAN SCALE & METRIC CONSERVATION:\n"
        "   - Describe the worker with exact scale: 'a lone male worker (1.78m tall, occupying ~35% of vertical "
        "frame height, realistically proportioned to the ceiling clearance) in a solid work jacket, dark trousers, "
        "boots, face never visible'.\n"
        "   - Camera perspective: Faithful to the camera angle, lens feel, and framing established in Image 1 and Image 2 (locked to the active camera setup without inventing new camera moves or altering the viewpoint angle).\n"
        "4. ASMR SOUND EFFECTS (60% VOLUME, 0% BGM):\n"
        "   - Include specific physical ASMR sound effects: gritty scraping, sawing buzzing, cordless drill chatter, "
        "hammer taps, rubber membrane crackle, footsteps on gravel/wood. Strictly NO background music (BGM).\n"
        "5. LIVING CAST ACTION-REACTION INTERLOCK (WHEN FIGURES/ANIMALS ARE PRESENT):\n"
        "   - When miniature figurines, resident characters, or animals are visible, NEVER omit them or freeze them into static dolls.\n"
        "   - Interweave their bodily responses directly with the worker/craftsman actions: immediate reflex/head-tilt when hands or tools enter, active gaze/body tracking during tool operations, and settled posture facing the completed work upon completion.\n"
        "6. MANDATORY SYNTAX & CLAUSES:\n"
        f"   - Opening sentence MUST be: \"Use the provided first frame and last frame as exact composition anchors. "
        f"Use IMAGE {s_idx} as the actual first-frame image and IMAGE {e_idx} as the actual last-frame image; every "
        "visible action must interpolate between those two frame images without inventing a third layout.\"\n"
        "   - Pacing clause MUST be: \"Continuous construction time-lapse, not real-time footage.\"\n\n"
        + crossing_rules
        + contract_info
        + "\n\nOUTPUT FORMAT:\n"
        "Return STRICT JSON only with NO markdown fences, matching exactly this structure:\n"
        '{"optimized_prompt": "...", "visual_delta_summary": "..."}'
    )

    user_text = (
        f"START Frame (IMAGE {s_idx}): Attached as first image.\n"
        f"END Frame (IMAGE {e_idx}): Attached as second image.\n\n"
        f"Current VIDEO {slot_index or s_idx} prompt draft:\n"
        f"{original_video_prompt.strip()}\n\n"
        "Visually inspect both images and rewrite the VIDEO prompt to match their exact real physical delta."
    )

    from prompt_pipeline import _multimodal_chat, clean_prompt_text, fix_camera_contradictions, _strip_code_fences, _reraise_if_cancelled

    try:
        response = _multimodal_chat(
            config,
            system_prompt,
            user_text,
            [start_frame_path, end_frame_path],
            max_tokens=2048,
            timeout=90
        )
        response_clean = _strip_code_fences(response).strip()
        data = json.loads(response_clean)
        optimized = str(data.get('optimized_prompt') or '').strip()
        if not optimized:
            return original_video_prompt

        # 确定性后处理与清理
        optimized = clean_prompt_text(optimized)
        optimized = fix_camera_contradictions(optimized, is_moving=is_crossing)

        # 确保标准开篇句完整
        expected_anchor = (
            f"Use the provided first frame and last frame as exact composition anchors. "
            f"Use IMAGE {s_idx} as the actual first-frame image and IMAGE {e_idx} as the actual last-frame image; "
            f"every visible action must interpolate between those two frame images without inventing a third layout."
        )
        if not optimized.lower().startswith("use the provided first frame and last frame as exact composition anchors"):
            optimized = f"{expected_anchor} {optimized}"

        return optimized
    except Exception as e:
        _reraise_if_cancelled(e)
        if sys.stdout:
            print(f"[VIDEO OPTIMIZER] Slot {slot_index or s_idx} optimization warning (kept original prompt): {e}")
        return original_video_prompt


def optimize_video_prompts_for_sequence(config, title, prompt_block, on_progress=None, target_slots=None, force=False):
    """视频生成前优化门：扫描全套已渲染帧图像，对比每拍真实差量并优化整套提示词块中的视频提示词。

    - config: 全局配置字典
    - title: 项目标题 / project_key
    - prompt_block: 当前的完整提示词文本（包含图片与视频提示词）
    - on_progress: 进度回调 (stage, details)
    - target_slots: 限制优化的视频槽位列表（None 表示全量）
    - force: 是否强制重新执行 VLM 优化（False 时优先复用已持久化的槽位差量优化记录）

    返回优化后的新 prompt_block 字符串，并自动原子持久化至 manifest.json。
    """
    project_dir = server_common._get_project_dir(title)
    if not prompt_block or not str(prompt_block).strip():
        if os.path.isdir(project_dir):
            mdata = server_common.read_manifest(project_dir) or {}
            prompt_block = mdata.get('prompt_block', '')
        if not prompt_block or not str(prompt_block).strip():
            return prompt_block

    # 配置项检查：若显式禁用优化门，则直接放行
    if not server_common.gate_setting('optimizeVideoPromptsBeforeGen', config):
        return prompt_block

    from prompt_pipeline import (
        _parse_prompt_slots, _format_prompt_block, prompt_slots_list,
        _map_parallel, _reraise_if_cancelled
    )

    images, videos = _parse_prompt_slots(prompt_block)
    if not videos:
        return prompt_block

    if not os.path.isdir(project_dir):
        return prompt_block

    # 读取空间契约与 manifest 中已持久化的差量优化记录
    manifest_data = server_common.read_manifest(project_dir) or {}
    spatial_contract = manifest_data.get('spatial_contract')
    existing_optimizations = manifest_data.get('video_prompt_optimizations') or {}
    if not isinstance(existing_optimizations, dict):
        existing_optimizations = {}

    # 确定待优化的槽位集合与指纹匹配情况
    wanted_slots = set(target_slots) if target_slots else set(videos.keys())
    slots_to_vlm = []
    slot_fingerprints = {}
    cached_results = {}

    for slot_idx in sorted(videos.keys()):
        if slot_idx not in wanted_slots:
            continue
        start_seq = slot_idx
        end_seq = slot_idx + 1
        start_path = _find_frame_file(project_dir, start_seq)
        end_path = _find_frame_file(project_dir, end_seq)
        
        # 必须起始帧与结束帧均已渲染落盘
        if not start_path or not end_path:
            continue

        video_item = videos[slot_idx]
        meta = video_item.get('meta', '') if isinstance(video_item, dict) else ''
        fp = _get_slot_delta_fingerprint(start_path, end_path, meta, spatial_contract)
        if not fp:
            continue
        slot_fingerprints[slot_idx] = fp

        # 检查持久化记录是否存在且环境指纹完全匹配
        opt_rec = existing_optimizations.get(str(slot_idx)) or existing_optimizations.get(slot_idx)
        if (not force) and isinstance(opt_rec, dict) and opt_rec.get('fingerprint') == fp and opt_rec.get('optimized_prompt'):
            cached_results[slot_idx] = opt_rec['optimized_prompt']
        else:
            slots_to_vlm.append(slot_idx)

    # 回填已命中的持久化优化结果
    updated_videos = dict(videos)
    changed_count = 0
    for slot_idx, cached_body in cached_results.items():
        if not cached_body or not cached_body.strip():
            continue
        orig_item = updated_videos.get(slot_idx)
        orig_body = orig_item['body'] if isinstance(orig_item, dict) else str(orig_item)
        if orig_body.strip() != cached_body.strip():
            if isinstance(orig_item, dict):
                orig_item['body'] = cached_body
            else:
                updated_videos[slot_idx] = cached_body
            changed_count += 1

    # 若所有槽位均已命中持久化缓存，无需任何 VLM 调用
    if not slots_to_vlm:
        if changed_count > 0:
            new_prompt_block = _format_prompt_block(images, updated_videos)
            _persist_optimization_results(project_dir, title, new_prompt_block, existing_optimizations)
            if on_progress:
                on_progress('prompt_block_updated', {
                    'prompt_block': new_prompt_block,
                    'prompt_slots': prompt_slots_list(new_prompt_block),
                    'optimized_slots_count': len(cached_results),
                    'changed_count': changed_count,
                    'cached_count': len(cached_results),
                    'newly_optimized_count': 0,
                    'message': f'视频提示词优化门：已复用 {len(cached_results)} 段已持久化的画面差量优化提示词',
                })
            return new_prompt_block
        else:
            return prompt_block

    total_count = len(slots_to_vlm)
    if on_progress:
        msg = (f'正在执行视频提示词视觉优化门（依据真实画面差量防跳变，待优化 {total_count} 段，已复用缓存 {len(cached_results)} 段）...'
               if cached_results else f'正在执行视频提示词视觉优化门（依据真实画面差量防跳变，共 {total_count} 段）...')
        on_progress('video_optimization_start', {
            'total': total_count,
            'slots': slots_to_vlm,
            'cached_count': len(cached_results),
            'message': msg,
        })

    items = []
    for slot_idx in slots_to_vlm:
        video_item = videos[slot_idx]
        body = video_item['body'] if isinstance(video_item, dict) else str(video_item)
        meta = video_item.get('meta', '') if isinstance(video_item, dict) else ''
        start_path = _find_frame_file(project_dir, slot_idx)
        end_path = _find_frame_file(project_dir, slot_idx + 1)
        items.append((slot_idx, {
            'config': config,
            'start_frame_path': start_path,
            'end_frame_path': end_path,
            'original_video_prompt': body,
            'slot_index': slot_idx,
            'start_seq': slot_idx,
            'end_seq': slot_idx + 1,
            'video_meta': meta,
            'spatial_contract': spatial_contract,
        }))

    completed_count = 0

    def _worker(arg):
        return optimize_single_video_prompt(**arg)

    def _on_slot_done(slot_idx, result):
        nonlocal completed_count
        completed_count += 1
        if result and str(result).strip():
            start_p = _find_frame_file(project_dir, slot_idx)
            end_p = _find_frame_file(project_dir, slot_idx + 1)
            existing_optimizations[str(slot_idx)] = {
                'slot': slot_idx,
                'fingerprint': slot_fingerprints.get(slot_idx),
                'optimized_prompt': result,
                'optimized_at': datetime.now().isoformat(),
                'start_frame': os.path.basename(start_p) if start_p else None,
                'end_frame': os.path.basename(end_p) if end_p else None,
            }
        if on_progress:
            on_progress('video_optimization_slot', {
                'slot': slot_idx,
                'current': completed_count,
                'total': total_count,
                'prompt': result,
                'message': f'视频 {slot_idx} 提示词已依据真实画面差量完成优化 ({completed_count}/{total_count})',
            })

    # 并发执行各槽位差量优化
    max_workers = min(4, len(items)) if len(items) > 1 else 1
    try:
        optimized_results = _map_parallel(_worker, items, max_workers=max_workers, on_done=_on_slot_done)
    except Exception as e:
        _reraise_if_cancelled(e)
        if sys.stdout:
            print(f"[VIDEO OPTIMIZER] Batch optimization error: {e}")
        optimized_results = {}

    # 将优化结果回填进 videos 结构并持久化记录
    for slot_idx, new_body in optimized_results.items():
        if not new_body or not new_body.strip():
            continue
        original_item = updated_videos.get(slot_idx)
        orig_body = original_item['body'] if isinstance(original_item, dict) else str(original_item)
        if orig_body.strip() != new_body.strip():
            if isinstance(original_item, dict):
                original_item['body'] = new_body
            else:
                updated_videos[slot_idx] = new_body
            changed_count += 1

        start_path = _find_frame_file(project_dir, slot_idx)
        end_path = _find_frame_file(project_dir, slot_idx + 1)
        existing_optimizations[str(slot_idx)] = {
            'slot': slot_idx,
            'fingerprint': slot_fingerprints.get(slot_idx),
            'optimized_prompt': new_body,
            'optimized_at': datetime.now().isoformat(),
            'start_frame': os.path.basename(start_path) if start_path else None,
            'end_frame': os.path.basename(end_path) if end_path else None,
        }

    # 组装新 prompt_block
    new_prompt_block = _format_prompt_block(images, updated_videos)

    # 原子写回 manifest.json 与 stepped_state
    _persist_optimization_results(project_dir, title, new_prompt_block, existing_optimizations)

    if on_progress:
        on_progress('prompt_block_updated', {
            'prompt_block': new_prompt_block,
            'prompt_slots': prompt_slots_list(new_prompt_block),
            'optimized_slots_count': len(optimized_results) + len(cached_results),
            'changed_count': changed_count,
            'newly_optimized_count': len(optimized_results),
            'cached_count': len(cached_results),
            'message': f'视频提示词优化门完成：已依据真实画面差量优化 {len(optimized_results)} 段，复用缓存 {len(cached_results)} 段',
        })

    return new_prompt_block

