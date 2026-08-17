import os
import sys
import json
import contextlib
import time
import shutil
import re
import math
import subprocess
import glob

from server_common import (
    SERVER_CONFIG, resolve_gateway, effective_config,
    OUTPUT_ROOT, _get_project_dir, _safe_project_name,
    IMG2IMG_CONTROL_PROMPT, IMG2IMG_CROSSING_REVEAL_CONTROL_PROMPT,
    resolve_cover_reference, project_cover_path,
    apply_google_fx_runtime_overrides, fx_cancel_context, fx_request_deadline,
    read_manifest, write_manifest, GenerationCancelled, log,
    gate_setting, _get_account_pool_service, _select_pool_account,
)
from frame_continuity import (
    analyze_frame, changed_grid_cells, continuity_max_retries, continuity_mode,
    family_map, is_transition_frame,
)
from frame_generator import (
    _image_edit_model, _image_generation_model, _generate_text_image,
    _generate_image_edit, _match_color_lab, detect_anchor_inertia,
    _continuity_family_maps, _continuity_beat, _image_quality_to_label,
    _get_google_fx_image_service, _fx_image_model,
    _fx_extract_uuid, _fx_store_frame, _fx_find_ref_for, _fx_src_dir,
    _fx_clear_frame_reference, _fx_cover_ref_jpg, _fx_local_frame_ref_jpg, update_manifest_stale_status,
)
from prompt_pipeline import (
    _parse_prompt_slots, _multimodal_chat, ground_threshold_reveal_prompt,
    threshold_reveal_continuity_clause,
)

_AI_DISCRIMINATION_SYSTEM_PROMPT = """You are an elite Hollywood Visual Director and Automated Quality Assurance Evaluator for AI-generated continuous video frame sequences.

You will be given:
1. THE PROMPT describing the current frame's physical scene, construction milestone delta, tools/actions, materials, and lighting.
2. THE REFERENCE IMAGE (if provided: the previous authoritative frame in the sequence).
3. 4 CANDIDATE IMAGES (labeled CANDIDATE 1, CANDIDATE 2, CANDIDATE 3, CANDIDATE 4) generated for this current frame.

Your mission:
Critically inspect all 4 candidate images and determine the SINGLE BEST candidate image to be selected as the official frame for this step. The chosen frame will become the authoritative reference image for the subsequent steps.

Evaluate each candidate strictly across 4 Core Pillars:
1. PROMPT & CONSTRUCTION DELTA ACCURACY (0-30 pts):
   - Does it accurately portray the new physical milestone requested in the prompt (e.g. framing installed, floor laid, concrete poured)?
   - Are the declared items, materials, and actions present without missing steps or premature completion?

2. SPATIAL ANCHOR & PERSPECTIVE CONTINUITY (0-30 pts):
   - Camera Alignment: Horizon height, field of view (24mm wide angle, human eye/chest level 1.3m), camera angle, and perspective must match the REFERENCE IMAGE.
   - Landmark Lock: Ceilings, background walls, boundary structures, door frames, and fixed landmarks must remain physically consistent with the reference frame.
   - Zero Cavernous Hall Expansion: Compact rooms/pits must NOT stretch into giant auditoriums or cave-like halls.
   - Zero Phantom Changes: No unexplained alterations to already finished areas.

3. MATERIAL REALISM & LIGHTING INTEGRITY (0-25 pts):
   - Photorealistic texture (matte/satin wood grains, authentic rough concrete/stone, natural light bounce).
   - Forbidden: NO artificial mirror reflections or wet floor gloss in finished reveal scenes, NO random neon or zig-zag fluorescent light bars.

4. ARTIFACT & PROHIBITED OBJECTS FREEDOM (0-15 pts):
   - No extra/mutated human limbs or duplicated workers.
   - Construction tool lifecycle: Tripods, loose cables, and heavy demolition tools must be removed once rooms are finished/furnished.
   - No blurry distortion, floating objects, or AI rendering glitches.

Output strictly valid JSON with this exact schema:
{
  "candidates": [
    {
      "index": 1,
      "score": 85,
      "strengths": "Detailed strengths of candidate 1",
      "defects": "Defects or continuity drift of candidate 1"
    },
    {
      "index": 2,
      "score": 92,
      "strengths": "Detailed strengths of candidate 2",
      "defects": "Defects or continuity drift of candidate 2"
    },
    {
      "index": 3,
      "score": 78,
      "strengths": "Detailed strengths of candidate 3",
      "defects": "Defects or continuity drift of candidate 3"
    },
    {
      "index": 4,
      "score": 88,
      "strengths": "Detailed strengths of candidate 4",
      "defects": "Defects or continuity drift of candidate 4"
    }
  ],
  "best_index": 2,
  "selection_reason": "Clear explanation of why candidate 2 was chosen as the best frame"
}
"""


def _generate_single_api_candidate(config, prompt_text, reference_path, out_path, is_text_only, ctrl_prompt):
    """Generate a single candidate image via API."""
    if is_text_only or not reference_path:
        _generate_text_image(config, prompt_text, out_path)
    else:
        _generate_image_edit(config, prompt_text, reference_path, out_path, control_prompt=ctrl_prompt)


def generate_frame_candidates(config, title, item, reference_path, seq, candidate_count=4, on_progress=None, is_bridge=False, is_cut_head=False, is_turn=False, project_url=None, frames_dir=None, canvas_state=None):
    """
    Generate `candidate_count` candidate images for a given sequence step.
    Saves candidates to `outputs/<title>/frames/candidates/frame_{seq:03d}/candidate_{1..N}.webp`.
    Extracts & preserves Google FX UUIDs and supports single-canvas project_url reuse.
    Returns list of absolute candidate file paths.

    ``canvas_state``：本次整条序列共用的画布账本（调用方持有并跨帧传同一个 dict）。
    - ``project_url``：已绑定的 Flow 项目；有它就一路透传，禁止新建。
    - ``opened``：本任务是否已经开出过自己的画布。**没有 project_url 不等于没开过
      画布**——部分 Flow 变体的工作台没有 /project/ 路由，画布只能靠这个旗记账
      （与 frame_generator 的 canvas_session['opened_accounts'] 同一套账）。用
      ``seq == 1`` 代替它有两个方向都错：子集重跑（target_sequences 不含 1）永远
      不要求新画布，会直接跑进上一个任务的画布；而路由缺失的账号上每帧都以为自己
      是"还没开画布"，i2i 续链被打断成一次次重新上传。
    - ``account_id``：画布是账号私有的，整条序列必须钉在同一个 AdsPower 账号上。
    """
    project_dir = _get_project_dir(title)
    if not frames_dir:
        frames_dir = os.path.join(project_dir, 'frames')
    candidates_dir = os.path.join(frames_dir, 'candidates', f'frame_{seq:03d}')
    os.makedirs(candidates_dir, exist_ok=True)

    candidate_paths = []
    candidates_meta = []
    is_text_only = (seq == 1 and not reference_path)
    
    # Determine control prompt
    if is_text_only:
        ctrl_prompt = ''
    elif seq == 1 and reference_path:
        ctrl_prompt = ''
    elif is_turn or is_bridge or is_cut_head:
        ctrl_prompt = IMG2IMG_CROSSING_REVEAL_CONTROL_PROMPT
    else:
        ctrl_prompt = IMG2IMG_CONTROL_PROMPT
        _region_cells = changed_grid_cells(item.get('prompt', ''))
        if _region_cells:
            ctrl_prompt += f" REGION LOCK: this beat's declared change is confined to grid cell(s) {', '.join(_region_cells)}."

    prompt_text = item.get('prompt', '')

    backend = (config.get('imageBackend') or 'api').strip().lower()
    canvas_state = canvas_state if isinstance(canvas_state, dict) else {}
    project_url = project_url or canvas_state.get('project_url')
    canvas_opened = bool(canvas_state.get('opened') or project_url)
    returned_project_url = project_url

    if backend == 'google_fx':
        log('INFO', 'CANDIDATE_GEN', f"IMG {seq:03d} 走 Google FX UI 自动化生成 {candidate_count} 张候选图...")
        # Check if Google FX service can generate multiple candidates
        try:
            google_fx, fx_models = _get_google_fx_image_service()
            from integrations.google_fx.models import ImageBatchRequest
            fx_model = _fx_image_model(config)
            
            # If Google FX, prefer fx_src reference with UUID if available
            eff_ref = reference_path
            if seq > 1 and frames_dir:
                fx_ref = _fx_find_ref_for(frames_dir, seq)
                if fx_ref and os.path.exists(fx_ref) and os.path.getsize(fx_ref) > 0:
                    eff_ref = fx_ref
                elif eff_ref and os.path.exists(eff_ref) and eff_ref.lower().endswith('.webp'):
                    eff_ref = _fx_local_frame_ref_jpg(eff_ref, frames_dir, seq - 1)

            req_images = [eff_ref] if eff_ref and os.path.exists(eff_ref) else []
            
            req = ImageBatchRequest(
                prompts=[prompt_text],
                images=req_images,
                ratio=config.get('imageAspectRatio') or '9:16',
                model=fx_model,
                output_path=candidates_dir,
                project_url=project_url,
                # 只有"本任务还没有自己的画布"时才新建；已开出来的画布一律复用。
                require_fresh_canvas=(not canvas_opened),
                # 画布是账号私有的：换号必然脱离已绑定的画布，所以一旦画布建立
                # 就锁号（与 frame_generator 迭代已绑定画布时的口径一致）。
                allow_account_switch=(not canvas_opened),
                generation_count=f"{candidate_count}x",
                is_candidate_mode=True,
            )
            # 取消/预算上下文必须显式建：没有它，FX 运行时里那一整排 _check_cancelled()
            # 与 deadline_exceeded() 全是空转（理由见 server_common.fx_cancel_context）。
            _cancel_fn = (lambda: bool(on_progress('cancel_check', None))) if on_progress else None
            with contextlib.ExitStack() as _fx_stack:
                _fx_account = canvas_state.get('account_id')
                if _fx_account:
                    # 画布是账号私有的：每帧都要在同一个账号上下文里连浏览器，
                    # 否则解析到的是进程默认账号，绑定的 project_url 根本打不开。
                    from integrations.google_fx.utils import account_binding
                    _fx_stack.enter_context(account_binding.bound_task_account(_fx_account))
                _fx_stack.enter_context(fx_cancel_context(_cancel_fn, deadline=fx_request_deadline()))
                fx_res = google_fx._generate_images_batch_google_fx(req)
            if fx_res:
                if fx_res.get('project_url'):
                    returned_project_url = fx_res.get('project_url')
                    if project_url and returned_project_url != project_url:
                        # 换画布 = 上一帧的结果 tile 不在了，i2i 只能靠重新上传接链。
                        # 静默换绑会让"每帧都在上传做好的图"看起来像无缘无故发生。
                        log('WARN', 'CANDIDATE_GEN',
                            f"IMG {seq:03d} Flow 画布已换绑："
                            f"{str(project_url).rsplit('/', 1)[-1][:8]} → "
                            f"{str(returned_project_url).rsplit('/', 1)[-1][:8]}；"
                            f"本帧起参考图改用上传方式挂载")
                    canvas_state['project_url'] = returned_project_url
                # 画布真的为本任务开出来了才记账：画布都没开成的失败
                # （FLOW_CANVAS_UNAVAILABLE）必须让下一帧继续要求新画布，
                # 否则重试就退回沿用上一个任务画布的老路。
                if fx_res.get('status') in ('success', 'partial', 'ok'):
                    canvas_state['opened'] = True
                if fx_res.get('image_urls'):
                    fx_urls = fx_res['image_urls']
                    for idx, src_p in enumerate(fx_urls[:candidate_count]):
                        cand_dest = os.path.join(candidates_dir, f'candidate_{idx+1}.webp')
                        cand_uuid = _fx_extract_uuid(src_p)
                        cand_raw_jpg = None
                        if cand_uuid and os.path.exists(src_p) and src_p.lower().endswith('.jpg'):
                            cand_raw_jpg = os.path.join(candidates_dir, f'candidate_{idx+1}_{cand_uuid}.jpg')
                            if src_p != cand_raw_jpg:
                                shutil.copy2(src_p, cand_raw_jpg)
                        elif os.path.exists(src_p):
                            cand_raw_jpg = src_p

                        if src_p != cand_dest:
                            try:
                                from PIL import Image
                                with Image.open(src_p) as im:
                                    im.save(cand_dest, 'WEBP', quality=95)
                            except Exception:
                                shutil.copy2(src_p, cand_dest)
                        
                        candidate_paths.append(cand_dest)
                        candidates_meta.append({
                            'index': idx + 1,
                            'path': cand_dest,
                            'raw_src': cand_raw_jpg or cand_dest,
                            'fx_uuid': cand_uuid,
                        })
        except Exception as e:
            # 这里静默退回 API 会同时丢掉画布与 UUID 续链（后续帧再也挂不上参考图
            # tile），而日志只有一行 WARN。按 ERROR 记，并把异常类型带出来。
            log('ERROR', 'CANDIDATE_GEN',
                f"IMG {seq:03d} Google FX 批量候选生成失败（{type(e).__name__}: {e}），"
                f"退回 API 多候选生成；本帧不参与 Flow 单画布串联")

    # If Google FX produced fewer candidates or backend is API, use API generation
    if len(candidate_paths) < candidate_count:
        missing_count = candidate_count - len(candidate_paths)
        start_idx = len(candidate_paths) + 1
        log('INFO', 'CANDIDATE_GEN', f"IMG {seq:03d} 生成 {missing_count} 张候选图 (从 index {start_idx} 开始)...")
        
        def _make_cand(c_idx):
            cand_path = os.path.join(candidates_dir, f'candidate_{c_idx}.webp')
            p_variant = prompt_text
            if c_idx > 1:
                p_variant = f"{prompt_text}\n\n[Variation #{c_idx}]"
            _generate_single_api_candidate(config, p_variant, reference_path, cand_path, is_text_only, ctrl_prompt)
            return cand_path

        for c_idx in range(start_idx, candidate_count + 1):
            if on_progress and on_progress('cancel_check', None):
                raise ConnectionError('用户取消了候选帧生成')
            if on_progress:
                on_progress('candidate_generating', {
                    'sequence': seq,
                    'candidate_index': c_idx,
                    'total_candidates': candidate_count,
                    'message': f"IMG {seq:03d} 正在生成候选图 #{c_idx}/{candidate_count}...",
                })
            try:
                p = _make_cand(c_idx)
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    candidate_paths.append(p)
                    candidates_meta.append({
                        'index': c_idx,
                        'path': p,
                        'raw_src': p,
                        'fx_uuid': None,
                    })
            except Exception as gen_err:
                log('ERROR', 'CANDIDATE_GEN', f"生成候选图 #{c_idx} 失败: {gen_err}")
                if candidate_paths:
                    break
                raise

    if not candidate_paths:
        raise RuntimeError(f"IMG {seq:03d} 生成候选图失败，未获得任何有效图片")

    # Save candidates metadata for easy recovery/switch
    try:
        meta_payload = {
            'sequence': seq,
            'project_url': returned_project_url,
            'candidates': candidates_meta,
        }
        with open(os.path.join(candidates_dir, 'candidates_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta_payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return candidate_paths


def evaluate_and_select_best_candidate(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
    """
    Calls multimodal VLM (Gemini 3.7 / 3.5 / Flash) to evaluate all candidates and choose the best one.
    Returns:
    {
        "candidates": [
            {"index": 1, "score": int, "strengths": str, "defects": str},
            ...
        ],
        "best_index": int (1-based),
        "selection_reason": str
    }
    """
    if len(candidate_paths) == 1:
        return {
            "candidates": [{"index": 1, "score": 90, "strengths": "唯一生成候选图", "defects": "无对比候选"}],
            "best_index": 1,
            "selection_reason": "单候选直选"
        }

    if on_progress:
        on_progress('candidate_evaluating', {
            'sequence': seq,
            'candidate_count': len(candidate_paths),
            'message': f"IMG {seq:03d} 4张候选图已就绪，AI 模型正在多模态鉴别与打分...",
        })

    # Prepare multimodal message
    user_text_parts = [
        f"--- CURRENT FRAME REQUIREMENTS (IMAGE {seq:03d}) ---",
        f"Prompt / Construction Milestone:",
        prompt_text,
        "",
    ]
    
    image_paths_to_send = []
    if reference_path and os.path.exists(reference_path) and os.path.getsize(reference_path) > 0:
        user_text_parts.append("The first image attached is the REFERENCE IMAGE (authoritative ground-truth from previous frame).")
        user_text_parts.append(f"The following images are CANDIDATE 1 through CANDIDATE {len(candidate_paths)}.")
        image_paths_to_send.append(reference_path)
    else:
        user_text_parts.append("This is Frame 1 (Anchor). There is no prior reference image. Evaluate based on prompt fidelity and photorealism.")
        user_text_parts.append(f"The following images are CANDIDATE 1 through CANDIDATE {len(candidate_paths)}.")

    for idx, cp in enumerate(candidate_paths):
        user_text_parts.append(f"- CANDIDATE {idx+1}: {os.path.basename(cp)}")
        image_paths_to_send.append(cp)

    user_text_parts.append("\nPlease output strictly the requested JSON analysis evaluating all candidates and selecting the best one.")
    user_text = "\n".join(user_text_parts)

    try:
        response_text = _multimodal_chat(
            config,
            system=_AI_DISCRIMINATION_SYSTEM_PROMPT,
            user_text=user_text,
            image_paths=image_paths_to_send,
            max_tokens=2000,
            timeout=120,
        )
        
        # Parse JSON
        clean_json = response_text.strip()
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean_json, re.DOTALL)
        if m:
            clean_json = m.group(1)
        else:
            m2 = re.search(r'(\{.*\})', clean_json, re.DOTALL)
            if m2:
                clean_json = m2.group(1)

        result_data = json.loads(clean_json)
        best_idx = int(result_data.get('best_index', 1))
        if best_idx < 1 or best_idx > len(candidate_paths):
            best_idx = 1
        
        cands_meta = result_data.get('candidates') or []
        selection_reason = result_data.get('selection_reason') or f"AI 鉴别选出最佳候选 #{best_idx}"

        return {
            "candidates": cands_meta,
            "best_index": best_idx,
            "selection_reason": selection_reason,
            "raw_response": response_text[:500],
        }
    except Exception as e:
        log('WARN', 'CANDIDATE_AI', f"IMG {seq:03d} AI 多模态鉴别调用异常 ({e})，默认采纳第 1 张候选")
        # Fallback to candidate 1
        return {
            "candidates": [{"index": i + 1, "score": 80, "strengths": "默认采纳", "defects": ""} for i in range(len(candidate_paths))],
            "best_index": 1,
            "selection_reason": f"AI鉴别服务暂不可用 ({e})，已默认采纳候选 #1",
        }


def _generate_full_collage_from_frames(frames_dir):
    """Generate 5-col collage image using ffmpeg for visual consistency checking."""
    frame_files = sorted(glob.glob(os.path.join(frames_dir, 'img_*.webp')))
    if not frame_files:
        return None
    input_args = []
    inputs = []
    for path in frame_files:
        input_args.extend(['-i', path])
        inputs.append(path)

    cols = 5
    rows = math.ceil(len(inputs) / cols)
    outpath = os.path.join(frames_dir, 'full_collage.jpg')
    n = len(inputs)

    filter_parts = [f'[{i}:v]scale=240:-1:force_original_aspect_ratio=decrease,pad=240:ih:(ow-iw)/2:(oh-ih)/2[s{i}]' for i in range(n)]
    filter_str = ';'.join(filter_parts) + ';'
    filter_str += ''.join(f'[s{i}]' for i in range(n))
    filter_str += f'concat=n={n}:v=1:a=0,tile={cols}x{rows}'

    try:
        from server_common import get_subprocess_window_flags
        win_flags = get_subprocess_window_flags()
    except Exception:
        win_flags = {}

    try:
        subprocess.run(
            ['ffmpeg', '-y'] + input_args + ['-filter_complex', filter_str, outpath],
            capture_output=True, timeout=60, **win_flags
        )
        return outpath if os.path.exists(outpath) else None
    except Exception:
        return None


def run_candidate_selection_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4):
    """
    Main entry point for 4-candidate AI selection frame sequence generation.
    For each frame in the sequence:
    1. Generates `candidate_count` (4) candidate images via UI automation / API.
    2. Runs AI multimodal model to evaluate & rank all 4 images, picking the best one.
    3. Saves winning candidate to `frames/img_{seq:03d}.webp`.
    4. Records candidate metadata & AI review in `manifest.json`.
    5. Feeds winning candidate as the reference image to the next frame.
    """
    images, videos = _parse_prompt_slots(prompt_block)
    prompts = []
    for idx in sorted(images):
        item = images[idx]
        body = item['body'] if isinstance(item, dict) else item
        meta = item.get('meta', '') if isinstance(item, dict) else ''
        prompts.append({'index': idx, 'prompt': body, 'meta': meta})

    if not prompts:
        raise RuntimeError('未在 prompt_block 中找到任何 图片 N: 提示词')

    prompts_by_seq = {int(item['index']): item for item in prompts}
    all_seqs = sorted(prompts_by_seq.keys())
    continuity_families = _continuity_family_maps(prompts_by_seq, videos)

    def _check_cancel():
        if on_progress and on_progress('cancel_check', None):
            raise ConnectionError('用户取消了帧序列生成')

    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest = {
        'title': title,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'method': 'A_single_chain_4_candidates',
        'generation_mode': 'candidate_selection',
        'candidate_count': candidate_count,
        'aspect_ratio': config.get('imageAspectRatio') or '9:16',
        'image_size': _image_quality_to_label(config.get('imageQuality')),
        'control_prompt': IMG2IMG_CONTROL_PROMPT,
        'project_url': None,
        'frames': [],
    }

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                existing_manifest = json.load(f)
                if isinstance(existing_manifest, dict) and 'frames' in existing_manifest:
                    manifest['frames'] = existing_manifest['frames']
                    manifest['created_at'] = existing_manifest.get('created_at', manifest['created_at'])
                    for _k, _v in existing_manifest.items():
                        if _k not in manifest or manifest[_k] is None:
                            manifest[_k] = _v
        except Exception:
            pass

    manifest_frames_by_seq = {f['sequence']: f for f in manifest.get('frames', [])}
    total_to_generate = len(target_sequences) if target_sequences is not None else len(prompts)
    project_url = manifest.get('project_url') or manifest.get('google_fx_project_url')
    backend = (config.get('imageBackend') or 'api').strip().lower()

    # 整条序列共用一块画布 = 整条序列必须钉在同一个 AdsPower 账号上，并且 FX 运行时
    # 得先拿到本次配置。此前这里两件事都没做：账号由进程默认环境变量随缘解析（上一个
    # 任务留下的值），额度耗尽时又会在帧与帧之间被换号救场——换号后原 project_url
    # 打不开，兜底就新建画布，表现成"每帧一块新画布"。
    fx_account_id = None
    if backend == 'google_fx':
        apply_google_fx_runtime_overrides(config)
        try:
            account_pool = _get_account_pool_service()
        except Exception as pool_err:
            log('WARN', 'CANDIDATE_GEN', f"号池服务不可用，帧序列沿用当前账号 ({pool_err})")
            account_pool = None
        pool_account_id = _select_pool_account(config, account_pool) if account_pool else None
        if pool_account_id:
            apply_google_fx_runtime_overrides(config)
        fx_account_id = pool_account_id or (str(config.get('googleFxUserId') or '').strip() or None)
        if fx_account_id:
            log('INFO', 'CANDIDATE_GEN', f"本次帧序列钉定 Flow 账号 {fx_account_id}（单画布复用要求账号不变）")

    # 单画布账本：跨帧透传（详见 generate_frame_candidates 的 canvas_state 说明）。
    canvas_state = {
        'project_url': project_url,
        'opened': bool(project_url),
        'account_id': fx_account_id,
    }

    if on_progress:
        on_progress('start', {'total': total_to_generate, 'mode': 'candidate_selection', 'candidate_count': candidate_count})

    previous_path = None
    workspace_root = os.path.dirname(os.path.abspath(__file__))

    for item in prompts:
        seq = int(item['index'])
        _check_cancel()

        target_path = os.path.join(frames_dir, f'img_{seq:03d}.webp')
        should_generate = True
        if target_sequences is not None:
            should_generate = seq in target_sequences

        if not should_generate:
            if os.path.exists(target_path):
                previous_path = target_path
            continue

        already_exists = os.path.exists(target_path) and os.path.getsize(target_path) > 0
        skip_generation = already_exists and (target_sequences is None)

        if skip_generation:
            previous_path = target_path
            existing_frame = manifest_frames_by_seq.get(seq)
            if not existing_frame:
                rel_target_path = os.path.relpath(target_path, workspace_root).replace('\\', '/')
                existing_frame = {
                    'sequence': seq,
                    'slot': seq,
                    'file': rel_target_path,
                    'url': '/' + rel_target_path if not rel_target_path.startswith('/') else rel_target_path,
                    'prompt': item.get('prompt', ''),
                    'meta': item.get('meta', ''),
                    'quality_gate': 'auto_approved',
                    'selection_mode': 'candidate_selection',
                    'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }
                manifest_frames_by_seq[seq] = existing_frame
                manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
            if on_progress:
                on_progress('frame', {
                    'sequence': seq,
                    'slot': seq,
                    'current': len([f for f in manifest.get('frames', []) if f.get('file')]),
                    'total': total_to_generate,
                    'frame': existing_frame,
                    'skipped': True,
                    'message': f"IMG {seq:03d} 已存在，跳过重新生成",
                })
            continue

        incoming_video = videos.get(seq - 1)
        incoming_meta = (incoming_video.get('meta', '') if isinstance(incoming_video, dict) else '').upper()
        is_bridge = ('BRIDGE' in item.get('meta', '').upper() or 'BRIDGE' in incoming_meta)
        is_turn = 'TURN' in incoming_meta
        is_cut_head = ('CUT' in incoming_meta) and ('BRIDGE' not in incoming_meta)
        is_continuity_transition = is_transition_frame(
            seq, item.get('meta', ''), incoming_meta, _continuity_beat(manifest, seq)
        )

        if backend == 'google_fx':
            if seq == 1:
                cover_ref = resolve_cover_reference(config, title)
                if cover_ref:
                    reference = _fx_cover_ref_jpg(cover_ref, frames_dir)
                elif target_sequences is not None and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                    reference = _fx_local_frame_ref_jpg(target_path, frames_dir, 1)
                else:
                    reference = None
            else:
                reference = _fx_find_ref_for(frames_dir, seq) or previous_path
        else:
            cover_ref = (resolve_cover_reference(config, title) if seq == 1 else None)
            cover_anchor = bool(cover_ref)
            reference = cover_ref if cover_anchor else previous_path
            if not reference:
                if seq > 1:
                    durable_parent = os.path.join(frames_dir, f'img_{seq - 1:03d}.webp')
                    if os.path.exists(durable_parent) and os.path.getsize(durable_parent) > 0:
                        reference = durable_parent
                elif seq == 1 and target_sequences is not None and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                    reference = target_path

        if on_progress:
            on_progress('frame_start', {
                'slot': seq,
                'sequence': seq,
                'total': total_to_generate,
                'message': f"IMG {seq:03d} 开始生成（4选1 智能模式）...",
            })

        # ── Step 1: Generate 4 Candidate Images ──
        candidate_paths = generate_frame_candidates(
            config, title, item, reference, seq,
            candidate_count=candidate_count,
            on_progress=on_progress,
            is_bridge=is_bridge,
            is_cut_head=is_cut_head,
            is_turn=is_turn,
            project_url=project_url,
            frames_dir=frames_dir,
            canvas_state=canvas_state,
        )

        # 画布绑定以 canvas_state 为准（内存直传），candidates_meta.json 只是留档：
        # 元数据落盘在 try/except: pass 里，写失败时绑定不能跟着一起丢。
        if canvas_state.get('project_url'):
            project_url = canvas_state['project_url']
            manifest['project_url'] = project_url
            manifest['google_fx_project_url'] = project_url

        # Retrieve candidate metadata if saved
        cand_meta_map = {}
        cands_dir = os.path.join(frames_dir, 'candidates', f'frame_{seq:03d}')
        meta_file = os.path.join(cands_dir, 'candidates_meta.json')
        if os.path.exists(meta_file):
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta_payload = json.load(f)
                    if meta_payload.get('project_url'):
                        project_url = meta_payload.get('project_url')
                        manifest['project_url'] = project_url
                        manifest['google_fx_project_url'] = project_url
                    for c in meta_payload.get('candidates', []):
                        cand_meta_map[c.get('index')] = c
            except Exception:
                pass

        # Rel paths for candidates
        cand_rel_urls = [
            os.path.relpath(cp, workspace_root).replace('\\', '/')
            for cp in candidate_paths
        ]

        if on_progress:
            on_progress('candidate_batch_ready', {
                'sequence': seq,
                'candidate_urls': cand_rel_urls,
                'candidate_count': len(candidate_paths),
                'message': f"IMG {seq:03d} 4张候选图生成完毕，准备 AI 鉴别...",
            })

        # ── Step 2: AI Multi-modal Evaluation & Selection ──
        eval_result = evaluate_and_select_best_candidate(
            config, item.get('prompt', ''), reference, candidate_paths, seq, on_progress=on_progress
        )

        best_idx = eval_result.get('best_index', 1)
        best_candidate_path = candidate_paths[best_idx - 1]
        selection_reason = eval_result.get('selection_reason', '')

        # ── Step 3: Copy Best Candidate to Target Frame Path & fx_src ──
        shutil.copy2(best_candidate_path, target_path)

        best_cand_meta = cand_meta_map.get(best_idx, {})
        best_uuid = best_cand_meta.get('fx_uuid')
        best_raw_src = best_cand_meta.get('raw_src')

        if best_raw_src and os.path.exists(best_raw_src):
            try:
                _, fx_src_path, stored_uuid = _fx_store_frame(best_raw_src, frames_dir, seq)
                best_uuid = stored_uuid or best_uuid
            except Exception as e:
                log('WARN', 'CANDIDATE_GEN', f"IMG {seq:03d} 留档 fx_src 异常: {e}")
        elif os.path.exists(best_candidate_path):
            try:
                _, fx_src_path, stored_uuid = _fx_store_frame(best_candidate_path, frames_dir, seq)
                best_uuid = stored_uuid or best_uuid
            except Exception:
                pass

        # Color matching if needed
        if seq > 1 and os.path.exists(target_path):
            first_frame_path = os.path.join(frames_dir, 'img_001.webp')
            if os.path.exists(first_frame_path) and not is_continuity_transition:
                _match_color_lab(target_path, first_frame_path, target_path)

        # Set as previous_path for the next frame
        previous_path = target_path

        # ── Step 4: Record into Manifest ──
        rel_target_path = os.path.relpath(target_path, workspace_root).replace('\\', '/')
        rel_candidates = [
            {
                'index': i + 1,
                'file': os.path.relpath(cp, workspace_root).replace('\\', '/'),
                'url': os.path.relpath(cp, workspace_root).replace('\\', '/'),
                'fx_uuid': cand_meta_map.get(i + 1, {}).get('fx_uuid'),
                'is_chosen': (i + 1 == best_idx),
                'score': (eval_result.get('candidates') or [{}])[i].get('score', 80) if i < len(eval_result.get('candidates') or []) else 80,
                'strengths': (eval_result.get('candidates') or [{}])[i].get('strengths', '') if i < len(eval_result.get('candidates') or []) else '',
                'defects': (eval_result.get('candidates') or [{}])[i].get('defects', '') if i < len(eval_result.get('candidates') or []) else '',
            }
            for i, cp in enumerate(candidate_paths)
        ]

        frame_data = {
            'sequence': seq,
            'slot': seq,
            'file': rel_target_path,
            'url': rel_target_path,
            'fx_uuid': best_uuid,
            'fx_project_url': project_url,
            'prompt': item.get('prompt', ''),
            'reference': os.path.relpath(reference, workspace_root).replace('\\', '/') if reference else None,
            'quality_gate': 'auto_approved',
            'vlm_qa_reason': f"AI 4选1 鉴别优选 (候选 #{best_idx}): {selection_reason}",
            'selection_mode': 'candidate_selection',
            'chosen_candidate_index': best_idx,
            'ai_evaluation': eval_result,
            'candidates': rel_candidates,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }

        # Update manifest frames
        manifest_frames_by_seq[seq] = frame_data
        manifest['frames'] = [manifest_frames_by_seq[s] for s in sorted(manifest_frames_by_seq.keys())]
        if project_url:
            manifest['project_url'] = project_url
            manifest['google_fx_project_url'] = project_url

        try:
            update_manifest_stale_status(
                manifest, project_dir,
                regenerated_sequences=target_sequences,
                finalize=False,
            )
        except Exception as stale_err:
            log('WARN', 'CANDIDATE_GEN', f"更新 manifest stale 状态异常: {stale_err}")

        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        if on_progress:
            on_progress('candidate_ai_evaluation', {
                'sequence': seq,
                'best_index': best_idx,
                'selection_reason': selection_reason,
                'scores': [c.get('score') for c in rel_candidates],
                'message': f"IMG {seq:03d} AI 鉴别选中候选 #{best_idx}：{selection_reason}",
            })
            on_progress('frame', {
                'sequence': seq,
                'slot': seq,
                'current': len([f for f in manifest['frames'] if f.get('file')]),
                'total': total_to_generate,
                'frame': frame_data,
            })

    # Finalize stale status & capability stamping
    try:
        update_manifest_stale_status(
            manifest, project_dir,
            regenerated_sequences=target_sequences,
            finalize=True,
        )
    except Exception as stale_err:
        log('WARN', 'CANDIDATE_GEN', f"最终收尾 manifest stale 状态异常: {stale_err}")

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Generate full collage
    _generate_full_collage_from_frames(frames_dir)

    manifest['project_dir'] = project_dir
    manifest['manifest'] = manifest_path
    return manifest


def switch_frame_candidate(title, seq, new_candidate_index):
    """
    Manually switch the authoritative frame for sequence `seq` to a different candidate.
    Updates `img_{seq:03d}.webp`, `fx_src`, and `manifest.json`.
    """
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    candidates_dir = os.path.join(frames_dir, 'candidates', f'frame_{seq:03d}')
    target_cand_path = os.path.join(candidates_dir, f'candidate_{new_candidate_index}.webp')

    if not os.path.exists(target_cand_path):
        raise FileNotFoundError(f"未找到候选图片: {target_cand_path}")

    target_frame_path = os.path.join(frames_dir, f'img_{seq:03d}.webp')
    shutil.copy2(target_cand_path, target_frame_path)

    # Find raw candidate jpg or uuid
    new_uuid = None
    raw_src = None
    meta_path = os.path.join(candidates_dir, 'candidates_meta.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                m_data = json.load(f)
            for c in m_data.get('candidates', []):
                if c.get('index') == new_candidate_index:
                    new_uuid = c.get('fx_uuid')
                    raw_src = c.get('raw_src')
                    break
        except Exception:
            pass

    if not raw_src or not os.path.exists(raw_src):
        if os.path.exists(candidates_dir):
            prefix = f'candidate_{new_candidate_index}_'
            for fname in os.listdir(candidates_dir):
                if fname.startswith(prefix) and fname.lower().endswith('.jpg'):
                    raw_src = os.path.join(candidates_dir, fname)
                    new_uuid = _fx_extract_uuid(fname)
                    break

    if raw_src and os.path.exists(raw_src):
        try:
            _, _, stored_uuid = _fx_store_frame(raw_src, frames_dir, seq)
            new_uuid = stored_uuid or new_uuid
        except Exception:
            pass
    elif os.path.exists(target_cand_path):
        try:
            _, _, stored_uuid = _fx_store_frame(target_cand_path, frames_dir, seq)
            new_uuid = stored_uuid or new_uuid
        except Exception:
            pass

    # Update manifest
    manifest_path = os.path.join(project_dir, 'manifest.json')
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)

        for frame in manifest.get('frames', []):
            if frame.get('sequence') == seq:
                frame['chosen_candidate_index'] = new_candidate_index
                frame['vlm_qa_reason'] = f"人工切换为候选 #{new_candidate_index}"
                if new_uuid:
                    frame['fx_uuid'] = new_uuid
                for cand in frame.get('candidates', []):
                    cand['is_chosen'] = (cand.get('index') == new_candidate_index)
                break

        try:
            update_manifest_stale_status(
                manifest, project_dir,
                regenerated_sequences=[seq],
                finalize=True,
            )
        except Exception as stale_err:
            log('WARN', 'CANDIDATE_GEN', f"更新 manifest stale 状态异常: {stale_err}")

        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Regenerate collage
    _generate_full_collage_from_frames(frames_dir)
    return manifest
