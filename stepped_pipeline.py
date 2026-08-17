import json
import os
import uuid
import subprocess
import math
import glob
from datetime import datetime

from server_common import _get_project_dir, read_manifest, write_manifest, manifest_lock
from prompt_pipeline import (
    compose_anchor_and_packet,
    compose_remaining_beats,
    refine_packet_from_accepted_anchor,
    _parse_prompt_slots,
    _format_prompt_block,
    prompt_block_from_output,
    prompt_slots_list,
)
from pipeline_orchestrator import render_single_frame, _render_videos_with_recovery, persist_outline_delivery_ledger
from frame_generator import generate_frame_sequence

STAGES = [
    'compose_phase1',     # Phase 1: parse brief → Beat Ladder → Drift Lock Packet → IMAGE 1 prompt
    'render_anchor',      # Render Frame 1 (anchor frame)
    'review_anchor',      # ⏸ PAUSE: user reviews anchor frame
    'compose_phase2',     # Phase 2: compose remaining beat prompts
    'render_batch',       # Render current batch of frames (loops)
    'review_batch',       # ⏸ PAUSE: user reviews batch collage (loops)
    'final_review',       # ⏸ PAUSE: full sequence collage review
    'render_videos',      # Generate video clips
    'completed',          # Done
    'cancelled',
]

# Stages where the pipeline pauses for user review
REVIEW_STAGES = {'review_anchor', 'review_batch', 'final_review'}


def _state_path(title):
    return os.path.join(_get_project_dir(title), '.stepped_pipeline.json')


def _load_state(title):
    path = _state_path(title)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _save_state(title, state):
    path = _state_path(title)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _compute_batches(total_beats, batch_size=4):
    """
    Frame 1 is the anchor (rendered separately), so batches start from frame 2.
    """
    sequences = list(range(2, total_beats + 1))  # Frame 1 is anchor
    return [sequences[i:i+batch_size] for i in range(0, len(sequences), batch_size)]


def _generate_batch_collage(title, sequences, batch_index):
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    inputs = []
    input_args = []
    for seq in sequences:
        path = os.path.join(frames_dir, f'img_{seq:03d}.webp')
        if os.path.exists(path):
            input_args.extend(['-i', path])
            inputs.append(path)
            
    if not inputs:
        return None
        
    cols = 5
    rows = math.ceil(len(inputs) / cols)
    outpath = os.path.join(frames_dir, f'batch_{batch_index:03d}_collage.jpg')
    n = len(inputs)
    
    # scale each input to 240px wide, then tile
    filter_parts = [f'[{i}:v]scale=240:-1:force_original_aspect_ratio=decrease,pad=240:ih:(ow-iw)/2:(oh-ih)/2[s{i}]' for i in range(n)]
    filter_str = ';'.join(filter_parts) + ';'
    filter_str += ''.join(f'[s{i}]' for i in range(n))
    filter_str += f'concat=n={n}:v=1:a=0,tile={cols}x{rows}'
    
    try:
        from server_common import get_subprocess_window_flags
        win_flags = get_subprocess_window_flags()
    except Exception:
        win_flags = {}

    subprocess.run(
        ['ffmpeg', '-y'] + input_args + ['-filter_complex', filter_str, outpath],
        capture_output=True, timeout=60, **win_flags
    )
    return outpath if os.path.exists(outpath) else None


def _generate_full_collage(title):
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    inputs = []
    input_args = []
    
    frame_files = sorted(glob.glob(os.path.join(frames_dir, 'img_*.webp')))
    for path in frame_files:
        input_args.extend(['-i', path])
        inputs.append(path)
        
    if not inputs:
        return None
        
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

    subprocess.run(
        ['ffmpeg', '-y'] + input_args + ['-filter_complex', filter_str, outpath],
        capture_output=True, timeout=60, **win_flags
    )
    return outpath if os.path.exists(outpath) else None


def _anchor_config(config):
    """分步任务渲首帧时整块提示词只有 IMAGE 1，封面（要拿首尾两拍拼 before/after）
    还写不出来，所以链头显式允许纯文生图。只用于首帧：批次渲染从第 2 帧起，缺上一帧
    参考仍然是硬错误。"""
    return dict(config or {}, allowTextOnlyAnchor=True)


def _render_batch(config, title, prompt_block, batch_sequences, on_progress=None):
    generate_frame_sequence(config, title, prompt_block, on_progress=on_progress, target_sequences=batch_sequences)
    return True


def start_stepped_pipeline(config, dimensions, on_progress=None, precomposed=None):
    """Starts the stepped pipeline: compose Phase 1 + render anchor, then pause for review.

    Unlike run_autonomous_pipeline which runs to completion, this pauses at review_anchor
    so the user can inspect Frame 1 before committing to the rest of the pipeline.
    Title is derived from compose_anchor_and_packet (same as run_autonomous_pipeline).

    `precomposed`：一份已经跑完的 Phase 1 产物（形状同 compose_anchor_and_packet 的返回值）。
    给了就跳过重合成。目前唯一的来源是爆款复刻线的交接（replica_pipeline.handoff_to_render）：
    那边已经合成过一次并让产物过了 banned 门禁，这里再合成一遍等于既重复付钱、又把渲染
    建在一份没审过的提示词上。其余调用方一律不传，行为与之前逐字相同。
    """
    state = {
        'pipeline_id': f'stepped_{uuid.uuid4().hex[:12]}',
        'title': None,  # Set after compose_anchor_and_packet
        'stage': 'compose_phase1',
        'batch_size': config.get('steppedBatchSize', 4),
        'current_batch_index': 0,
        'batches': [],
        'total_beats': 0,
        'anchor_image_path': None,
        'image_1_prompt': None,
        'prompt_block': None,
        'packet': None,
        'parsed_brief': None,
        'beat_ladder': None,
        'full_collage': None,
        'video_result': None,
        'banned_hits': [],
        'error': None,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
    }

    try:
        if on_progress:
            on_progress('stepped_stage', {
                'stage': 'compose_phase1',
                'message': ('阶段 1/7: 沿用复刻线已合成并通过门禁的提示词，跳过重新合成...'
                            if precomposed else
                            '阶段 1/7: 正在解析创意简报并生成首帧 Prompt...')})

        compose_state = precomposed or compose_anchor_and_packet(
            config, dimensions, on_progress=on_progress)
        # Title comes from compose_anchor_and_packet, same as run_autonomous_pipeline
        title = compose_state['title']
        state['title'] = title
        state['image_1_prompt'] = compose_state.get('image_1_prompt')
        state['packet'] = compose_state.get('packet')
        state['parsed_brief'] = compose_state.get('parsed_brief')
        state['beat_ladder'] = compose_state.get('beat_ladder')
        state['total_beats'] = len(state['beat_ladder']) if state['beat_ladder'] else 0
        
        state['stage'] = 'render_anchor'
        _save_state(title, state)
        
        if on_progress:
            on_progress('stepped_stage', {'stage': 'render_anchor', 'message': '阶段 2/7: 正在渲染首帧 (Anchor Frame)...'})
            
        rendered = render_single_frame(_anchor_config(config), title, 1, state['image_1_prompt'],
                                       on_progress=on_progress)
        state['anchor_image_path'] = rendered.get('image_path')
        project_dir = rendered.get('project_dir') or _get_project_dir(title)
        
        # Refine packet and write spatial_contract to manifest
        state['packet'] = refine_packet_from_accepted_anchor(
            config, state['anchor_image_path'], state['packet'], state.get('parsed_brief'))
            
        with manifest_lock(project_dir):
            _manifest = read_manifest(project_dir) or {'title': title, 'frames': []}
            _manifest['spatial_contract'] = {
                key: state['packet'].get(key) or state.get('parsed_brief', {}).get(key)
                for key in ('world_lock', 'carrier_envelope', 'entrance_topology', 'space_graph',
                            'camera_palette')
            }
            _manifest['spatial_beats'] = [
                # 'space' 必须在册，理由同 pipeline_orchestrator 的同款投影。
                {key: beat.get(key) for key in (
                    'index', 'space', 'space_id', 'transition_stage', 'camera_family', 'reveal_scope',
                    'light_source_state', 'operation', 'package_operations', 'milestone_name',
                    'before_state', 'after_state', 'preserve_state', 'changed_grid_cells',
                    'persistent_traces', 'hard_cut', 'bridge_stage', 'turn_direction')}
                for beat in state.get('beat_ladder', []) if isinstance(beat, dict)
            ]
            write_manifest(project_dir, _manifest)
            
        # Compute batches for remaining frames
        if state['total_beats'] > 1:
            batch_lists = _compute_batches(state['total_beats'], state['batch_size'])
            state['batches'] = [{'sequences': b, 'status': 'queued', 'collage': None} for b in batch_lists]
            
        state['stage'] = 'review_anchor'
        state['updated_at'] = datetime.now().isoformat()
        _save_state(title, state)
        
        if on_progress:
            on_progress('stepped_stage', {'stage': 'review_anchor', 'message': '首帧渲染完成，请审查并确认（通过后继续，打回则重绘）。'})
            
        return state
        
    except Exception as e:
        state['error'] = str(e)
        state['updated_at'] = datetime.now().isoformat()
        _save_state(title, state)
        raise


def advance_stepped_pipeline(title, action='approve', on_progress=None, config=None):
    if config is None:
        config = {}
    
    state = _load_state(title)
    if not state:
        raise ValueError(f"No stepped pipeline state found for {title}")
        
    stage = state['stage']
    
    try:
        if stage == 'review_anchor':
            if action == 'approve':
                state['stage'] = 'compose_phase2'
                _save_state(title, state)
                
                if on_progress:
                    on_progress('stepped_stage', {'stage': 'compose_phase2', 'message': '阶段 4/7: 正在基于确认的首帧，生成后续所有分镜提示词...'})
                    
                compose_state = {
                    'title': state['title'],
                    'image_1_prompt': state['image_1_prompt'],
                    'packet': state['packet'],
                    'parsed_brief': state.get('parsed_brief'),
                    'beat_ladder': state.get('beat_ladder'),
                }
                
                # 合成器返回的是整份带标记文档（TITLE/THEME/PROMPTS/AUDIT），不是提示词
                # 正文；不剥头尾，末尾那句审计说明会跟着最后一段视频送去渲染。
                prompt_block = prompt_block_from_output(
                    compose_remaining_beats(config, compose_state, on_progress=on_progress))
                state['prompt_block'] = prompt_block
                
                # 事后门禁复核：若简报带有 banned_elements，对 Phase 2 重写后的成稿做二次扫描
                banned = (state.get('parsed_brief') or {}).get('banned_elements') or []
                if banned:
                    from prompt_pipeline.reverse import banned_element_hits
                    hits = banned_element_hits(prompt_block, banned)
                    state['banned_hits'] = hits
                    if hits and on_progress:
                        on_progress('stepped_stage', {
                            'stage': 'compose_phase2',
                            'message': f'警告：重写提示词命中了 {len(hits)} 个禁用元素（{"、".join(hits[:5])}），请在审阅时重点核对。',
                            'banned_hits': hits,
                        })
                else:
                    state['banned_hits'] = []

                project_dir = _get_project_dir(title)
                persist_outline_delivery_ledger(
                    project_dir, config.get('_outline_delivery_ledger'), title=title)
                
                state['stage'] = 'render_batch'
                state['current_batch_index'] = 0
                _save_state(title, state)
                
                _execute_render_batch(state, config, on_progress)
                
            elif action == 'retry':
                state['stage'] = 'render_anchor'
                _save_state(title, state)
                
                if on_progress:
                    on_progress('stepped_stage', {'stage': 'render_anchor', 'message': '正在重新渲染首帧...'})
                    
                rendered = render_single_frame(_anchor_config(config), title, 1, state['image_1_prompt'],
                                               on_progress=on_progress)
                state['anchor_image_path'] = rendered.get('image_path')

                state['stage'] = 'review_anchor'
                state['updated_at'] = datetime.now().isoformat()
                _save_state(title, state)
                if on_progress:
                    on_progress('stepped_stage', {'stage': 'review_anchor', 'message': '首帧重绘完成，请再次审查。'})
                    
        elif stage == 'review_batch':
            if action == 'approve':
                state['batches'][state['current_batch_index']]['status'] = 'approved'
                state['current_batch_index'] += 1
                
                if state['current_batch_index'] < len(state['batches']):
                    state['stage'] = 'render_batch'
                    _save_state(title, state)
                    _execute_render_batch(state, config, on_progress)
                else:
                    _finish_rendering_and_full_collage(state, on_progress)
            
            elif action == 'retry':
                state['stage'] = 'render_batch'
                _save_state(title, state)
                _execute_render_batch(state, config, on_progress)
                
            elif action == 'skip':
                state['batches'][state['current_batch_index']]['status'] = 'approved'
                state['current_batch_index'] += 1
                while state['current_batch_index'] < len(state['batches']):
                    _execute_render_batch_sync(state, config, on_progress)
                    state['batches'][state['current_batch_index']]['status'] = 'approved'
                    state['current_batch_index'] += 1
                _finish_rendering_and_full_collage(state, on_progress)
                
        elif stage == 'final_review':
            if action in ('approve', 'skip'):
                state['stage'] = 'render_videos'
                _save_state(title, state)
                
                if on_progress:
                    on_progress('stepped_stage', {'stage': 'render_videos', 'message': '阶段 7/7: 开始生成并处理所有视频段落...'})
                    
                video_result = _render_videos_with_recovery(config, title, state['prompt_block'], on_progress=on_progress,
                                                            project_dir=_get_project_dir(title))
                state['video_result'] = video_result
                
                state['stage'] = 'completed'
                state['updated_at'] = datetime.now().isoformat()
                _save_state(title, state)
                
                if on_progress:
                    on_progress('stepped_stage', {'stage': 'completed', 'message': '流水线全部完成！'})
                    
            elif action == 'retry':
                # User stays at final_review, makes adjustments manually and re-triggers approve
                pass
                
        return state
        
    except Exception as e:
        state['error'] = str(e)
        state['updated_at'] = datetime.now().isoformat()
        _save_state(title, state)
        raise


def _execute_render_batch(state, config, on_progress):
    _execute_render_batch_sync(state, config, on_progress)
    
    idx = state['current_batch_index']
    batch = state['batches'][idx]
    
    if on_progress:
        on_progress('stepped_stage', {
            'stage': 'render_batch', 
            'message': '正在生成批次拼图...',
            'batch_index': idx,
            'total_batches': len(state['batches'])
        })
        
    collage_path = _generate_batch_collage(state['title'], batch['sequences'], idx)
    batch['collage'] = collage_path
    batch['status'] = 'pending_review'
    
    state['stage'] = 'review_batch'
    state['updated_at'] = datetime.now().isoformat()
    _save_state(title=state['title'], state=state)
    
    if on_progress:
        on_progress('stepped_stage', {
            'stage': 'review_batch', 
            'message': f'批次 {idx+1}/{len(state["batches"])} 渲染完成，请审查拼图（通过后继续下一批，打回则重绘本批次）。',
            'batch_index': idx,
            'total_batches': len(state['batches']),
            'collage_path': collage_path
        })


def _execute_render_batch_sync(state, config, on_progress):
    idx = state['current_batch_index']
    batch = state['batches'][idx]
    batch['status'] = 'rendering'
    _save_state(title=state['title'], state=state)
    
    if on_progress:
        on_progress('stepped_stage', {
            'stage': 'render_batch', 
            'message': f'阶段 5/7: 正在渲染分镜批次 {idx+1}/{len(state["batches"])} (包含帧 {batch["sequences"]})',
            'batch_index': idx,
            'total_batches': len(state['batches'])
        })
        
    _render_batch(config, state['title'], state['prompt_block'], batch['sequences'], on_progress)


def _finish_rendering_and_full_collage(state, on_progress):
    if on_progress:
        on_progress('stepped_stage', {'stage': 'final_review', 'message': '所有分镜渲染完毕，正在生成全序列长拼图...'})
    
    full_collage = _generate_full_collage(state['title'])
    state['full_collage'] = full_collage
    state['stage'] = 'final_review'
    state['updated_at'] = datetime.now().isoformat()
    _save_state(title=state['title'], state=state)
    
    if on_progress:
        on_progress('stepped_stage', {
            'stage': 'final_review', 
            'message': '全序列渲染完成！请审查所有成图并执行最终手动调整（如需重绘单个画面请使用网格面板），确认无误后批准进入视频生成阶段。',
            'collage_path': full_collage
        })


def get_stepped_status(title):
    state = _load_state(title)
    if not state:
        return None
    
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    rendered_count = 0
    if os.path.exists(frames_dir):
        rendered_count = len(glob.glob(os.path.join(frames_dir, 'img_*.webp')))
    
    state['total_frames_rendered'] = rendered_count
    return state


def cancel_stepped_pipeline(title):
    state = _load_state(title)
    if state:
        state['stage'] = 'cancelled'
        state['updated_at'] = datetime.now().isoformat()
        _save_state(title, state)
        return state
    return None
