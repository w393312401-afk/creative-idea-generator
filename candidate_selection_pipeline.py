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
    gate_setting, chain_guard_mode, _get_account_pool_service, _select_pool_account,
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
    _fx_clear_frame_reference, _fx_cover_ref_jpg, _fx_local_frame_ref_jpg, _fx_heal_frame_uuid,
    update_manifest_stale_status,
    current_thread_sinks, set_upstream_event_sink, set_cancel_check_sink,
)
from prompt_pipeline import (
    _parse_prompt_slots, _multimodal_chat, ground_threshold_reveal_prompt,
    threshold_reveal_continuity_clause,
    accounting_is_active, start_accounting, stop_and_get_accounting, merge_accounting,
)

_AI_DISCRIMINATION_SYSTEM_PROMPT = """你是一名好莱坞级电影视觉总监与 AI 视频连续帧序列智能质检鉴别专家。

你将收到：
1. 【本帧施工阶段与物理提示词】描述当前帧的具体施工节点、物理变化差量、工装工具/动作、材质与光影。
2. 【前序基准参考图】（若提供：即前一拍已确认的官方基底画面）。
3. 【后续目标参考图】（若提供：即后一拍已生成的官方交付画面，用于三明治前后双向闭环，严防单帧修改导致整体格局断层）。
4. 【4 张当前帧生成候选图】（标记为 CANDIDATE 1, CANDIDATE 2, CANDIDATE 3, CANDIDATE 4）。

你的核心任务：
对全部 4 张候选图进行严格对比评审与多维度打分，选出最优秀、最连贯、最写实的唯一 1 张图作为本帧的官方采用图。被选中的帧将作为后续所有帧的基准参考图。

严格围绕以下 16 个细分项逐项评分。每项只能给 0 至该项满分的数值；不得直接臆造总分，
总分由服务端对细分项求和并应用硬伤规则：
1. 提示词与工序差量准确性 (0-30 分)：
   - core_milestone 核心施工节点完成度 (0-12)
   - physical_delta 本帧新增物理差量准确性 (0-8)
   - state_progression 无漏做、提前施工或状态倒退 (0-6)
   - actions_materials 人物动作、工具与材料匹配 (0-4)

2. 空间锚点与前后双向连续性 (0-30 分)：
   - camera_fidelity 机位、焦段、俯仰与构图符合提示词 (0-8)
   - anchor_stability 门窗、梁柱、洞口等空间锚点稳定 (0-8)
   - scale_depth 景深、空间尺度与人物比例合理 (0-6)
   - previous_continuity 与前一帧状态连续 (0-5)
   - next_reachability 与后一帧目标物理可衔接 (0-3)
   - 镜头与机位一致性：严格按照提示词声明的机位视角（俯仰角、方位、焦段及构图比例，如俯拍、仰拍、平视、特写或三七开等）进行评估；同一机位下的连续帧必须保持透视与地平线高度稳定，跨机位切换时必须忠实呈现提示词要求的全新视角，严禁因非平视广角而误判扣分。
   - DLSP 五层绝对景深协议：严格检查近景锚点（门框/爬梯 <1m）、中景开阔通廊（1-4m）、侧翼实虚墙体边界与后景收口（>4m），严禁保龄球道式管道拉伸（Bowling alley effect）。
   - 双向咬合（若提供后续目标参考图）：所选候选图必须能够作为连接【前序基准图】与【后续目标图】的自然桥梁，严禁破坏通往后一帧的物理可达性，绝不允许环境格局断层（如门窗突变、后墙消失、透视颠覆等引起后续帧整体报废的硬伤）。
   - 地标锁定与非对称特征：天花板梁架走向、后背景墙（无凭空立柱）、门框/洞口边界、左右非对称地标结构维持物理一致。
   - 严防空间膨胀：紧凑空间/坑穴（如直径 3.0m、净高 2.2m 空间）严禁异常拉伸为巨大礼堂或洞穴大厅（Cavernous expansion），工人人体比例尺（1.78m 占画面高度约 35%）自然协调。
   - 零凭空变化与严防已拆除结构死而复生：已完成的区域绝无莫名变动或无故复原破损；在拆除/清场工序完成之后（如破旧泥屋被拆除、旧墙被推倒），后续帧（放线、开挖、地梁、砌砖、二层、精装等）严禁在背景或周边再次出现已被拆除销毁的旧结构残体（Ghost structure）。若候选图出现旧破烂结构复活，属于 P0 级严重状态倒退穿帮，直接大幅扣分（分数应 < 70 分）并一票否决！

3. 材质真实感与光影一致性 (0-25 分)：
   - material_realism 主体材质真实 (0-8)
   - lighting_consistency 光照、阴影与色温一致 (0-6)
   - human_realism 真人皮肤、服装与动作真实 (0-6)
   - physical_reference_fidelity 施工物理合理，且在提供原片时忠实对标 (0-5)
   - 真实物理质感（温润哑光/半哑光实木纹理、粗糙混凝土/天然石材、自然漫反射光）。
   - 严禁违规：成品展示帧严禁出现假反光镜面或湿水面效果；灯光必须自然，严禁产生杂乱网状荧光灯带。

4. 瑕疵与违规物过滤 (0-15 分；分数越高表示瑕疵越少)：
   - anatomy_integrity 无肢体、面部、人物融合或重影问题 (0-5)
   - image_integrity 清晰且无融化、重复纹理等 AI 伪影 (0-5)
   - lifecycle_composition_integrity 工具生命周期正确，且无黑边、底座等构图穿帮 (0-5)
   - 严禁多肢/融化/肢体畸变或工人克隆多体重影。
   - 工具生命周期：已完工/软装展示阶段必须彻底撤除三脚架、裸露电缆与破坏性重型工具。
   - 画面清晰，无模糊失真或 AI 伪影。

【爆款原片对标参考图 (IMAGE REF) 权威基准规则】：
- 若提供了【爆款原片对标参考图 (IMAGE REF)】，它是本拍客观真实画面的最高权威基准（Ground Truth）。
- 取景与构图保真：必须以原片参考图的真实取景为准。例如原片若为自然露天微缩场景，绝不允许候选图凭空出现黑色管道圆环、望远镜黑边、圆形隧道框景等画框伪影；带有此类多余画框伪影的候选图属于严重幻觉，必须大幅扣分！
- 道具与主体比例：人物形象、服装、场景结构与原片对标保持高度吻合者优先胜出。
- 活物一律真人（硬性）：画面里的人必须是真人——真实皮肤质感、真实毛发、真实布料垂坠与自然发力体态。凡是渲成蜡像、树脂/塑料人偶、玩偶、假人模特者，一律判为严重瑕疵大幅扣分，不得以「与原片人偶风格一致」为由给高分；微缩场景同样适用——那里的人只是**小**，不是塑料的，尺寸对不对由比例判，材质像不像真人单独判。

【评分标尺校准原则（Score Calibration & Tiering）- 严禁无端给出压抑性低分】：
- 基础基准分（Baseline 80分）：画面完整、主体清晰、大致符合施工提示词要求的合格候选，基准分应在 80-85 分区间，切忌因微小 AI 瑕疵盲目扣至 60 几分。
- 90 - 98 分（卓越 / 极力推荐）：完全契合核心机位与原片构图对标、人物比例真实自然且渲染为有血有肉的真人、材质与光影极佳、无破绽穿帮。
- 82 - 89 分（良好 / 合格次选）：主体和工序正确，但存在轻微构图偏差（如局部阴影生硬、轻微边缘暗化）或轻微背景小瑕疵，整体依然可用。
- 70 - 81 分（中等 / 明显瑕疵）：存在较明显的构图穿帮（如凭空出现多余黑边画框/管道圈、漏出微缩模型木质底座台面、视角偏差过大）或局部质感失真。
- < 70 分（不合格 / 严重违规）：存在严重肢体畸变、空间坍塌膨胀、提示词核心工序完全缺失等硬伤。

【硬伤代码（hard_flags）】：
- ghost_structure_revival：已拆除结构死而复生；直接淘汰。
- wrong_scene_or_carrier：主体场景或载体身份错误；直接淘汰。
- core_process_missing：核心工序完全缺失；总分最高 69。
- severe_spatial_collapse：严重空间坍塌或异常膨胀；总分最高 65。
- severe_anatomy：严重人物肢体/面部畸变；总分最高 59。
- plastic_human：真人被渲染成塑料、树脂、蜡像或玩偶；总分最高 69。
没有硬伤必须输出空数组，不要发明近义代码。未提供前帧、后帧或原片参考时，对应细分项按本帧
自身可验证内容正常评分，不得仅因参考缺失而扣分。

【输出语言强制要求】：
必须全量严格使用【简体中文】输出所有评分、优势（strengths）、缺陷（defects）与最终优选理由（selection_reason）。语言简练、专业、切中要点。

请严格输出符合以下结构的合法 JSON 格式。必须为每张候选图输出完整的 16 项 subscores：
{
  "candidates": [
    {
      "index": 1,
      "subscores": {
        "core_milestone": 10,
        "physical_delta": 7,
        "state_progression": 5,
        "actions_materials": 4,
        "camera_fidelity": 7,
        "anchor_stability": 7,
        "scale_depth": 5,
        "previous_continuity": 4,
        "next_reachability": 3,
        "material_realism": 7,
        "lighting_consistency": 5,
        "human_realism": 5,
        "physical_reference_fidelity": 4,
        "anatomy_integrity": 5,
        "image_integrity": 4,
        "lifecycle_composition_integrity": 4
      },
      "hard_flags": [],
      "strengths": "简述候选1的核心优势（中文，不超过35字）",
      "defects": "简述候选1的不足或偏差（中文，不超过35字）"
    }
  ],
  "best_index": 1,
  "selection_reason": "综合评估结论：清晰阐述为何推荐该候选图（中文，不超过60字）"
}
上例只展示候选1的字段形状；实际必须按相同形状返回收到的全部候选图。best_index 仅为模型建议，
服务端会自行求和、应用淘汰/封顶规则并确定最终采用图。
"""


_CANDIDATE_SCORE_FIELDS = (
    ('process', 'core_milestone', 12),
    ('process', 'physical_delta', 8),
    ('process', 'state_progression', 6),
    ('process', 'actions_materials', 4),
    ('continuity', 'camera_fidelity', 8),
    ('continuity', 'anchor_stability', 8),
    ('continuity', 'scale_depth', 6),
    ('continuity', 'previous_continuity', 5),
    ('continuity', 'next_reachability', 3),
    ('realism', 'material_realism', 8),
    ('realism', 'lighting_consistency', 6),
    ('realism', 'human_realism', 6),
    ('realism', 'physical_reference_fidelity', 5),
    ('defects', 'anatomy_integrity', 5),
    ('defects', 'image_integrity', 5),
    ('defects', 'lifecycle_composition_integrity', 5),
)

_CANDIDATE_HARD_FLAG_RULES = {
    'ghost_structure_revival': {'disqualify': True},
    'wrong_scene_or_carrier': {'disqualify': True},
    'core_process_missing': {'cap': 69},
    'severe_spatial_collapse': {'cap': 65},
    'severe_anatomy': {'cap': 59},
    'plastic_human': {'cap': 69},
}


def _bounded_candidate_score(value, maximum):
    """Turn an untrusted VLM subscore into a bounded, stable numeric value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    number = max(0.0, min(float(maximum), number))
    return int(number) if number.is_integer() else round(number, 1)


def _normalize_candidate_evaluation(result_data, candidate_count):
    """Validate VLM output, calculate totals server-side, and select the winner."""
    raw_candidates = result_data.get('candidates') if isinstance(result_data, dict) else []
    raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []
    by_index = {}
    for position, candidate in enumerate(raw_candidates, 1):
        if not isinstance(candidate, dict):
            continue
        try:
            index = int(candidate.get('index', position))
        except (TypeError, ValueError):
            index = position
        if 1 <= index <= candidate_count and index not in by_index:
            by_index[index] = candidate

    normalized = []
    for index in range(1, candidate_count + 1):
        candidate = by_index.get(index, {})
        raw_subscores = candidate.get('subscores')
        has_structured_scores = isinstance(raw_subscores, dict)
        subscores = {}
        group_scores = {'process': 0, 'continuity': 0, 'realism': 0, 'defects': 0}
        if has_structured_scores:
            for group, key, maximum in _CANDIDATE_SCORE_FIELDS:
                value = _bounded_candidate_score(raw_subscores.get(key), maximum)
                subscores[key] = value
                group_scores[group] += value
            raw_score = round(sum(group_scores.values()), 1)
            raw_score = int(raw_score) if float(raw_score).is_integer() else raw_score
            score_source = 'server_subscore_sum'
        else:
            raw_score = _bounded_candidate_score(candidate.get('score'), 100)
            score_source = 'legacy_model_total'

        raw_flags = candidate.get('hard_flags')
        raw_flags = raw_flags if isinstance(raw_flags, list) else []
        hard_flags = []
        for flag in raw_flags:
            flag = str(flag or '').strip()
            if flag in _CANDIDATE_HARD_FLAG_RULES and flag not in hard_flags:
                hard_flags.append(flag)
        disqualified = any(_CANDIDATE_HARD_FLAG_RULES[f].get('disqualify') for f in hard_flags)
        caps = [_CANDIDATE_HARD_FLAG_RULES[f].get('cap') for f in hard_flags]
        caps = [cap for cap in caps if cap is not None]
        score_cap = min(caps) if caps else 100
        score = min(raw_score, score_cap)

        normalized.append({
            'index': index,
            'score': score,
            'raw_score': raw_score,
            'score_cap': score_cap,
            'score_source': score_source,
            'subscores': subscores,
            'group_scores': group_scores if has_structured_scores else {},
            'hard_flags': hard_flags,
            'disqualified': disqualified,
            'strengths': str(candidate.get('strengths') or ''),
            'defects': str(candidate.get('defects') or ''),
        })

    eligible = [candidate for candidate in normalized if not candidate['disqualified']]
    ranking_pool = eligible or normalized
    winner = max(
        ranking_pool,
        key=lambda candidate: (
            candidate['score'],
            candidate['group_scores'].get('continuity', 0),
            candidate['group_scores'].get('process', 0),
            candidate['group_scores'].get('realism', 0),
            candidate['group_scores'].get('defects', 0),
            -candidate['index'],
        ),
    ) if ranking_pool else {'index': 1}
    try:
        model_best_index = int(result_data.get('best_index', 1))
    except (TypeError, ValueError, AttributeError):
        model_best_index = 1
    if not 1 <= model_best_index <= candidate_count:
        model_best_index = 1
    return {
        'candidates': normalized,
        'best_index': winner['index'],
        'model_best_index': model_best_index,
        'all_candidates_disqualified': bool(normalized and not eligible),
    }


_DEFAULT_CANDIDATE_CONCURRENCY = 4

# autofix 档下同一拍最多就地自动修几次。修一次≈一次定向提示词重写 + 4 张候选重渲，
# 不设上限就会在"改写解决不了的问题"上原地烧额度（典型是提示词本身与上游画面矛盾，
# 改多少遍都渲不对）。连修这么多次仍不过 = 这拍不是自动改写能解决的，退化成停链等人。
_CHAIN_GUARD_AUTOFIX_ATTEMPTS = 2


def candidate_concurrency(config, candidate_count=4):
    """4选1模式 API 候选图生成的并发度。config['candidateConcurrency'] 可覆盖；默认 candidate_count (4)，范围 1~8。"""
    try:
        n = int((config or {}).get('candidateConcurrency') or candidate_count or _DEFAULT_CANDIDATE_CONCURRENCY)
    except Exception:
        n = candidate_count or _DEFAULT_CANDIDATE_CONCURRENCY
    return max(1, min(8, n))


def _generate_single_api_candidate(config, prompt_text, reference_path, out_path, is_text_only, ctrl_prompt):
    """Generate a single candidate image via API."""
    if is_text_only or not reference_path:
        _generate_text_image(config, prompt_text, out_path)
    else:
        _generate_image_edit(config, prompt_text, reference_path, out_path, control_prompt=ctrl_prompt)


# 像素级近重复校验拿最近几张槽位图当参照。取"最近"而不是"全部"：比对成本随帧数
# 线性增长，而画布重排捞回来的历史 tile 几乎总是最近生成的那几张。
_NEAR_DUP_REFERENCE_FRAMES = 6


def fx_media_exclusions(project_dir, frames_dir):
    """本任务已经消费过的 Flow 媒体 UUID + 已落盘的槽位图。

    候选线一直没往 ImageBatchRequest 里塞过这两样，于是 FX 侧提交前的黑名单只剩
    「画布面板上此刻看得见的 UUID」——而 Flow 画布是虚拟化的，滚出视口的历史 tile
    一个都不在里面。结果就是本任务自己刚生成的图被下一拍当成新结果抓回来
    （2026-08-22：IMG 011 的三张候选原样变成 IMG 012 的候选）。

    UUID 取三处，取全为止：manifest 里每帧的 fx_uuid、每张候选图的 fx_uuid，以及
    frames/fx_src 与 frames/candidates 下的文件名（partial 批次可能还没写进 manifest，
    但图已经落了盘、UUID 已经被消费掉了）。
    图片路径只取最近 _NEAR_DUP_REFERENCE_FRAMES 张正式槽位图 img_XXX.webp：它们是
    下载后像素级近重复校验的参照物。这份名单要克制——每张候选都要跟名单上每张图做
    一次解码+缩放+差分，整表比对会给每一拍凭空加上好几秒。真正的广撒网是上面那份
    UUID 名单（近乎免费），像素比对只是兜住"UUID 都认不出来"的残余情形，而画布重排
    捞回来的恰恰是刚生成不久的那几张。
    """
    uuids = set()
    paths = []

    manifest_path = os.path.join(project_dir or '', 'manifest.json')
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception:
        manifest = {}
    for frame in (manifest.get('frames') or []) if isinstance(manifest, dict) else []:
        if not isinstance(frame, dict):
            continue
        if frame.get('fx_uuid'):
            uuids.add(str(frame['fx_uuid']).lower())
        for cand in (frame.get('candidates') or []):
            if isinstance(cand, dict) and cand.get('fx_uuid'):
                uuids.add(str(cand['fx_uuid']).lower())

    frames_dir = frames_dir or os.path.join(project_dir or '', 'frames')
    if os.path.isdir(frames_dir):
        for name in sorted(os.listdir(frames_dir)):
            if not re.match(r'^img_\d{3}\.webp$', name):
                continue
            full = os.path.join(frames_dir, name)
            try:
                if os.path.getsize(full) > 0:
                    paths.append(full)
            except OSError:
                pass
        paths = paths[-_NEAR_DUP_REFERENCE_FRAMES:]
    for sub in ('fx_src', 'candidates'):
        root = os.path.join(frames_dir, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                found = _fx_extract_uuid(name)
                if found:
                    uuids.add(found.lower())

    return sorted(uuids), paths


def sync_frame_candidates_pool(frames_dir, seq, current_frame=None, target_path=None, auto_archive_current=True, project_url=None):
    """
    Synchronizes, recovers, and normalizes the full candidate pool for a given sequence `seq`.
    - Scans `candidates/frame_{seq:03d}/` on disk for existing webp/jpg files and `candidates_meta.json`.
    - If `img_{seq:03d}.webp` exists on disk and is not yet in candidate pool, safely archives it as a candidate.
    - Resolves and maintains contiguous, collision-free candidate indices (1..N).
    - Preserves model, model_display, fx_uuid, raw_src, scores, and chosen flags.
    - Writes updated `candidates_meta.json`.
    - Returns `(candidates_for_manifest, chosen_candidate_index, meta_payload)`.
    """
    workspace_root = os.path.dirname(os.path.abspath(__file__))
    candidates_dir = os.path.join(frames_dir, 'candidates', f'frame_{seq:03d}')
    os.makedirs(candidates_dir, exist_ok=True)
    meta_file = os.path.join(candidates_dir, 'candidates_meta.json')

    # 1. Load existing candidates_meta.json if present
    meta_dict = {}
    stored_project_url = project_url
    if os.path.exists(meta_file):
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                payload = json.load(f)
                if isinstance(payload, dict):
                    if payload.get('project_url') and not stored_project_url:
                        stored_project_url = payload['project_url']
                    raw_cands = payload.get('candidates')
                    if isinstance(raw_cands, list):
                        for c in raw_cands:
                            if isinstance(c, dict):
                                try:
                                    c_idx = int(c.get('index') or 0)
                                    if c_idx > 0:
                                        meta_dict[c_idx] = dict(c)
                                except (TypeError, ValueError):
                                    pass
        except Exception:
            pass

    # 2. Discover physical candidate files on disk
    if os.path.isdir(candidates_dir):
        for fname in os.listdir(candidates_dir):
            m = re.match(r'^candidate_(\d+)(?:_([a-zA-Z0-9_-]+))?\.webp$', fname)
            if m:
                try:
                    c_idx = int(m.group(1))
                    fpath = os.path.join(candidates_dir, fname)
                    if c_idx not in meta_dict:
                        raw_src = fpath
                        cand_uuid = _fx_extract_uuid(fname)
                        prefix = f'candidate_{c_idx}_'
                        for jname in os.listdir(candidates_dir):
                            if jname.startswith(prefix) and jname.lower().endswith('.jpg'):
                                raw_src = os.path.join(candidates_dir, jname)
                                cand_uuid = _fx_extract_uuid(jname) or cand_uuid
                                break
                        meta_dict[c_idx] = {
                            'index': c_idx,
                            'path': fpath,
                            'raw_src': raw_src,
                            'fx_uuid': cand_uuid,
                        }
                    else:
                        if not meta_dict[c_idx].get('path') or not os.path.exists(meta_dict[c_idx]['path']):
                            meta_dict[c_idx]['path'] = fpath
                except ValueError:
                    pass

    # 3. Check and archive existing authoritative frame if not yet in pool
    authoritative_frame_path = target_path or os.path.join(frames_dir, f'img_{seq:03d}.webp')
    pool_is_empty = (len(meta_dict) == 0)
    should_archive = (auto_archive_current or pool_is_empty)
    if should_archive and os.path.exists(authoritative_frame_path) and os.path.getsize(authoritative_frame_path) > 0:
        already_archived = False
        auth_size = os.path.getsize(authoritative_frame_path)
        try:
            with open(authoritative_frame_path, 'rb') as f:
                auth_bytes = f.read()
        except Exception:
            auth_bytes = None

        if auth_bytes:
            for c_idx, c_item in meta_dict.items():
                c_p = c_item.get('path')
                if c_p and os.path.exists(c_p) and os.path.getsize(c_p) == auth_size:
                    try:
                        with open(c_p, 'rb') as cf:
                            if cf.read() == auth_bytes:
                                already_archived = True
                                break
                    except Exception:
                        pass

        if not already_archived:
            new_idx = (max(meta_dict.keys()) if meta_dict else 0) + 1
            dest_cand_path = os.path.join(candidates_dir, f'candidate_{new_idx}.webp')
            try:
                if not os.path.exists(dest_cand_path):
                    shutil.copy2(authoritative_frame_path, dest_cand_path)
            except Exception:
                pass

            c_m = (current_frame.get('model') if current_frame else None) or 'gemini-3.1-flash-image'
            c_m_disp = (current_frame.get('model_display') if current_frame else None) or ('手动上传' if c_m == 'manual_upload' else ('GPT-2' if 'gpt' in str(c_m).lower() else ('Google FX' if ('google_fx' in str(c_m).lower() or (current_frame and current_frame.get('fx_uuid'))) else 'Gemini')))
            meta_dict[new_idx] = {
                'index': new_idx,
                'path': dest_cand_path,
                'raw_src': dest_cand_path,
                'fx_uuid': current_frame.get('fx_uuid') if current_frame else None,
                'model': c_m,
                'model_display': c_m_disp,
            }

    # 4. Resolve chosen index
    chosen_idx = None
    if current_frame and current_frame.get('chosen_candidate_index'):
        try:
            chosen_idx = int(current_frame['chosen_candidate_index'])
        except (TypeError, ValueError):
            pass

    if chosen_idx not in meta_dict and os.path.exists(authoritative_frame_path):
        auth_size = os.path.getsize(authoritative_frame_path)
        try:
            with open(authoritative_frame_path, 'rb') as f:
                auth_bytes = f.read()
            for c_idx, c_item in meta_dict.items():
                c_p = c_item.get('path')
                if c_p and os.path.exists(c_p) and os.path.getsize(c_p) == auth_size:
                    with open(c_p, 'rb') as cf:
                        if cf.read() == auth_bytes:
                            chosen_idx = c_idx
                            break
        except Exception:
            pass

    if chosen_idx not in meta_dict:
        chosen_idx = min(meta_dict.keys()) if meta_dict else 1

    # 5. Build clean, sorted candidate list
    sorted_meta = []
    manifest_cands = []
    
    existing_eval_map = {}
    if current_frame and isinstance(current_frame.get('candidates'), list):
        for ec in current_frame['candidates']:
            if isinstance(ec, dict) and ec.get('index'):
                existing_eval_map[ec['index']] = ec

    for c_idx in sorted(meta_dict.keys()):
        cm = meta_dict[c_idx]
        c_path = cm.get('path') or os.path.join(candidates_dir, f'candidate_{c_idx}.webp')
        rel_path = os.path.relpath(c_path, workspace_root).replace('\\', '/')
        url = '/' + rel_path if not rel_path.startswith('/') else rel_path
        
        c_model = cm.get('model') or (existing_eval_map.get(c_idx, {}).get('model')) or (current_frame.get('model') if current_frame else None) or 'gemini-3.1-flash-image'
        c_model_display = cm.get('model_display') or (existing_eval_map.get(c_idx, {}).get('model_display')) or ('手动上传' if c_model == 'manual_upload' else ('GPT-2' if 'gpt' in str(c_model).lower() else ('Google FX' if ('google_fx' in str(c_model).lower() or cm.get('fx_uuid')) else 'Gemini')))
        
        # 评分/评语只认真的评过的那一份，**绝不编**。两条规矩：
        #
        # 1. 调用方传进来的 current_frame 优先于落盘的 candidates_meta.json。前者是本次
        #    刚跑出来的 AI 鉴别结论，后者是上一次的快照；反过来取会让新评分永远盖不进去
        #    ——一旦某个 index 在 meta 里有过分，后面再评多少次都以第一次为准。
        # 2. 没评过就留 None / ''，不要塞 85 分和"基础主帧"。前端 (app.js) 对 score==null
        #    本来就渲染成"-- 分"，占位分只会让一张从没被鉴别过的图，在弹层里跟真评过的
        #    候选并排显示同一种分数徽标；更糟的是它会被下面几行写回 meta 落盘，从此变成
        #    一个永久的假分，再也分不出哪些是真评过的。
        eval_info = existing_eval_map.get(c_idx, {})
        score = eval_info.get('score')
        if score is None:
            score = cm.get('score')
        strengths = eval_info.get('strengths') or cm.get('strengths') or ''
        defects = eval_info.get('defects') or cm.get('defects') or ''
        scoring_details = {}
        for detail_key in ('raw_score', 'score_cap', 'score_source', 'subscores',
                           'group_scores', 'hard_flags', 'disqualified'):
            if detail_key in eval_info:
                scoring_details[detail_key] = eval_info[detail_key]
            elif detail_key in cm:
                scoring_details[detail_key] = cm[detail_key]

        is_chosen = (c_idx == chosen_idx)

        cm['model'] = c_model
        cm['model_display'] = c_model_display
        # 只把真有值的写回落盘，别把"没评过"固化成一条假记录
        if score is not None:
            cm['score'] = score
        if strengths:
            cm['strengths'] = strengths
        if defects:
            cm['defects'] = defects
        cm.update(scoring_details)
        sorted_meta.append(cm)

        manifest_cands.append({
            'index': c_idx,
            'file': rel_path,
            'url': url,
            'fx_uuid': cm.get('fx_uuid'),
            'model': c_model,
            'model_display': c_model_display,
            'is_chosen': is_chosen,
            'score': score,
            'strengths': strengths,
            'defects': defects,
            **scoring_details,
        })

    meta_payload = {
        'sequence': seq,
        'project_url': stored_project_url,
        'candidates': sorted_meta,
    }

    try:
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta_payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return manifest_cands, chosen_idx, meta_payload


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

    # 检查并同步已有候选池与落盘图片，确保历史生成帧（含先前单张生成或老批次）安全归档，不被覆盖
    sync_frame_candidates_pool(frames_dir, seq, auto_archive_current=True, project_url=project_url)

    # 检查池中已有候选图与元数据，实现全量累积追加而非覆盖
    existing_meta = []
    meta_file = os.path.join(candidates_dir, 'candidates_meta.json')
    if os.path.exists(meta_file):
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                payload = json.load(f)
                if isinstance(payload, dict) and isinstance(payload.get('candidates'), list):
                    existing_meta = [c for c in payload['candidates'] if isinstance(c, dict)]
        except Exception:
            existing_meta = []

    max_existing_idx = 0
    for cm in existing_meta:
        try:
            c_idx = int(cm.get('index') or 0)
            if c_idx > max_existing_idx:
                max_existing_idx = c_idx
        except (TypeError, ValueError):
            pass
    if not existing_meta and os.path.isdir(candidates_dir):
        for name in os.listdir(candidates_dir):
            m = re.match(r'^candidate_(\d+)(?:_.*)?\.webp$', name)
            if m:
                try:
                    max_existing_idx = max(max_existing_idx, int(m.group(1)))
                except ValueError:
                    pass

    start_idx = max_existing_idx + 1
    end_idx = max_existing_idx + candidate_count

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
            
            # 提交前把「本任务已经消费过的媒体」交给 FX 侧：没有它，抓图那边只剩
            # 实时 DOM 基线，而画布虚拟化会让历史 tile 在基线里彻底隐身。
            fx_excluded_uuids, fx_excluded_paths = fx_media_exclusions(project_dir, frames_dir)
            req = ImageBatchRequest(
                prompts=[prompt_text],
                images=req_images,
                excluded_media_uuids=fx_excluded_uuids,
                excluded_image_paths=fx_excluded_paths,
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
                # 参考图只能靠上传挂载的那次，上传完它就在画布上并有了自己的 UUID。
                # 立刻把上一帧的 fx_src 留档改名成带 UUID 的形式，下一次引用（下一帧
                # 的链式参考、以及每一次「修复此帧问题」）就能直接挂画布 tile。
                # 不做这一步，同一张图会被无限次重传：留档名一直是 nouuid，
                # _fx_find_ref_for 永远认不出它。
                uploaded_uuids = fx_res.get('uploaded_reference_uuids') or {}
                if uploaded_uuids and seq > 1 and frames_dir:
                    healed = _fx_heal_frame_uuid(
                        frames_dir, seq - 1,
                        uploaded_uuids.get(eff_ref) or next(iter(uploaded_uuids.values()), None))
                    if healed:
                        log('INFO', 'CANDIDATE_GEN',
                            f"IMG {seq - 1:03d} 的参考图留档已补上画布 UUID，"
                            f"后续引用与修复不再重复上传")
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
                        cand_idx = start_idx + idx
                        cand_dest = os.path.join(candidates_dir, f'candidate_{cand_idx}.webp')
                        cand_uuid = _fx_extract_uuid(src_p)
                        cand_raw_jpg = None
                        if cand_uuid and os.path.exists(src_p) and src_p.lower().endswith('.jpg'):
                            cand_raw_jpg = os.path.join(candidates_dir, f'candidate_{cand_idx}_{cand_uuid}.jpg')
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
                            'index': cand_idx,
                            'path': cand_dest,
                            'raw_src': cand_raw_jpg or cand_dest,
                            'fx_uuid': cand_uuid,
                            'model': fx_model if backend == 'google_fx' else (config.get('imageModel') or 'gemini-3.1-flash-image'),
                            'model_display': 'Google FX' if backend == 'google_fx' else 'Gemini',
                        })
        except Exception as e:
            # 这里静默退回 API 会同时丢掉画布与 UUID 续链（后续帧再也挂不上参考图
            # tile），而日志只有一行 WARN。按 ERROR 记，并把异常类型带出来。
            log('ERROR', 'CANDIDATE_GEN',
                f"IMG {seq:03d} Google FX 批量候选生成失败（{type(e).__name__}: {e}），"
                f"退回 API 多候选生成；本帧不参与 Flow 单画布串联")

    # FX 少返回几张时，**不要**用 API 候选去补齐缺口（除非 FX 一张都没给出来）。
    #
    # API 候选没有画布 tile，_make_cand 里的 'fx_uuid': None 就是这个事实。它一旦被
    # 4选1 选中，这一帧就脱离了 Flow 血统：留档只能落成 img_NNN_nouuid.jpg，
    # _fx_find_ref_for 认不出，于是**下一帧的链式参考只能靠重新上传接链**——为了多
    # 一个候选，赔掉的是下游整条链的画布连续性。2026-08-23 实测：IMG 017 的 FX 批次
    # 只回了 3 张，第 4 张由 API 补齐并被选中，此后每次渲染/修复第 18 帧都要重传一次
    # 同一张图。
    #
    # 3 张画布候选足够挑，比 4 张里混一张"选中就断链"的划算。FX 彻底空手（0 张）时
    # 仍然退回 API——那是"有帧"与"没帧"的区别，另当别论；那种情况下下一帧会付一次
    # 上传，并由 _fx_heal_frame_uuid 就地自愈，不会反复付。
    if backend == 'google_fx' and candidate_paths and len(candidate_paths) < candidate_count:
        log('INFO', 'CANDIDATE_GEN',
            f"IMG {seq:03d} Flow 只回了 {len(candidate_paths)}/{candidate_count} 张候选，"
            f"按画布血统优先不补 API 候选（补进来的候选一旦选中，下一帧就得靠上传接链）")

    # If Google FX produced fewer candidates or backend is API, use API generation
    elif len(candidate_paths) < candidate_count:
        missing_count = candidate_count - len(candidate_paths)
        cur_start_idx = start_idx + len(candidate_paths)
        missing_indices = list(range(cur_start_idx, cur_start_idx + missing_count))
        max_workers = candidate_concurrency(config, missing_count)
        log('INFO', 'CANDIDATE_GEN', f"IMG {seq:03d} 并发生成 {missing_count} 张候选图 (从 index {cur_start_idx} 开始，并发度 {max_workers})...")

        if on_progress and on_progress('cancel_check', None):
            raise ConnectionError('用户取消了候选帧生成')

        if on_progress:
            on_progress('candidate_generating', {
                'sequence': seq,
                'candidate_index': None,
                'total_candidates': candidate_count,
                'message': f"IMG {seq:03d} 正在并发生成 {missing_count} 张候选图 (#{cur_start_idx}~#{cur_start_idx + missing_count - 1})...",
            })

        def _make_cand(c_idx):
            cand_path = os.path.join(candidates_dir, f'candidate_{c_idx}.webp')
            var_num = (c_idx - start_idx + 1)
            p_variant = prompt_text
            if var_num > 1:
                p_variant = f"{prompt_text}\n\n[Variation #{var_num}]"
            _generate_single_api_candidate(config, p_variant, reference_path, cand_path, is_text_only, ctrl_prompt)
            c_model = config.get('imageModel') or 'gemini-3.1-flash-image'
            c_model_disp = 'GPT-2' if 'gpt' in str(c_model).lower() else ('Google FX' if 'google_fx' in str(c_model).lower() else 'Gemini')
            if os.path.exists(cand_path) and os.path.getsize(cand_path) > 0:
                return {
                    'index': c_idx,
                    'path': cand_path,
                    'raw_src': cand_path,
                    'fx_uuid': None,
                    'model': c_model,
                    'model_display': c_model_disp,
                }
            raise RuntimeError(f"Candidate #{c_idx} image file is missing or empty")

        if max_workers <= 1 or len(missing_indices) <= 1:
            for c_idx in missing_indices:
                if on_progress and on_progress('cancel_check', None):
                    raise ConnectionError('用户取消了候选帧生成')
                if on_progress:
                    on_progress('candidate_generating', {
                        'sequence': seq,
                        'candidate_index': c_idx,
                        'total_candidates': candidate_count,
                        'message': f"IMG {seq:03d} 正在生成候选图 #{c_idx} (本批第 {c_idx - start_idx + 1}/{candidate_count} 张)...",
                    })
                try:
                    meta = _make_cand(c_idx)
                    candidate_paths.append(meta['path'])
                    candidates_meta.append(meta)
                except Exception as gen_err:
                    log('ERROR', 'CANDIDATE_GEN', f"生成候选图 #{c_idx} 失败: {gen_err}")
                    if candidate_paths:
                        break
                    raise
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            parent_upstream, parent_cancel = current_thread_sinks()
            parent_accounting = accounting_is_active()

            def _wrapped(c_idx):
                set_upstream_event_sink(parent_upstream)
                set_cancel_check_sink(parent_cancel)
                if parent_accounting:
                    start_accounting()
                try:
                    res = _make_cand(c_idx)
                    usage = stop_and_get_accounting() if parent_accounting else None
                    return res, usage
                finally:
                    set_upstream_event_sink(None)
                    set_cancel_check_sink(None)

            generated_results = {}
            errors = {}
            completed_so_far = len(candidate_paths)

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_wrapped, c_idx): c_idx for c_idx in missing_indices}
                try:
                    for fut in as_completed(futures):
                        c_idx = futures[fut]
                        if on_progress and on_progress('cancel_check', None):
                            for f in futures:
                                f.cancel()
                            raise ConnectionError('用户取消了候选帧生成')
                        try:
                            cand_meta, usage = fut.result()
                            if parent_accounting and usage:
                                merge_accounting(usage)
                            generated_results[c_idx] = cand_meta
                            completed_so_far += 1
                            if on_progress:
                                on_progress('candidate_generating', {
                                    'sequence': seq,
                                    'candidate_index': c_idx,
                                    'completed_count': completed_so_far,
                                    'total_candidates': candidate_count,
                                    'message': f"IMG {seq:03d} 候选图 #{c_idx} 生成完成 ({completed_so_far}/{candidate_count})...",
                                })
                        except GenerationCancelled:
                            for f in futures:
                                f.cancel()
                            raise
                        except ConnectionError:
                            for f in futures:
                                f.cancel()
                            raise
                        except Exception as gen_err:
                            log('ERROR', 'CANDIDATE_GEN', f"IMG {seq:03d} 生成候选图 #{c_idx} 失败: {gen_err}")
                            errors[c_idx] = gen_err
                except GenerationCancelled:
                    raise
                except ConnectionError:
                    raise
                finally:
                    for fut in futures:
                        fut.cancel()

            # 按候选序号顺序组装结果
            for c_idx in missing_indices:
                if c_idx in generated_results:
                    meta = generated_results[c_idx]
                    candidate_paths.append(meta['path'])
                    candidates_meta.append(meta)

            if not candidate_paths:
                if errors:
                    first_err = next(iter(errors.values()))
                    raise RuntimeError(f"IMG {seq:03d} 所有候选图并发生成均失败: {first_err}")
                raise RuntimeError(f"IMG {seq:03d} 生成候选图失败，未获得任何有效图片")

    if not candidate_paths:
        raise RuntimeError(f"IMG {seq:03d} 生成候选图失败，未获得任何有效图片")

    # Save candidates metadata for easy recovery/switch (accumulate with existing_meta)
    try:
        combined_meta_map = {c['index']: c for c in existing_meta if isinstance(c, dict) and c.get('index')}
        for cm in candidates_meta:
            if isinstance(cm, dict) and cm.get('index'):
                combined_meta_map[cm['index']] = cm
        all_candidates_meta = [combined_meta_map[k] for k in sorted(combined_meta_map.keys())]
        meta_payload = {
            'sequence': seq,
            'project_url': returned_project_url,
            'candidates': all_candidates_meta,
        }
        with open(os.path.join(candidates_dir, 'candidates_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta_payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return candidate_paths


def evaluate_and_select_best_candidate(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None, succeeding_path=None, succeeding_prompt=None, ref_frame_path=None):
    """
    Calls multimodal VLM (Gemini 3.7 / 3.5 / Flash) to evaluate all candidates and choose the best one.
    Supports succeeding_path and succeeding_prompt for bidirectional sandwich context binding during frame repair.
    Supports ref_frame_path for ground-truth viral benchmark keyframe alignment.
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
    if not candidate_paths:
        return {
            "candidates": [{"index": 1, "score": 90, "strengths": "无生成候选图", "defects": ""}],
            "best_index": 1,
            "selection_reason": "无候选"
        }

    if on_progress:
        on_progress('candidate_evaluating', {
            'sequence': seq,
            'candidate_count': len(candidate_paths),
            'message': f"IMG {seq:03d} {len(candidate_paths)}张候选图已就绪，AI 模型正在多模态鉴别与打分...",
        })

    # 只清理**送进鉴别模型的这份副本**：老单落盘的提示词正文里可能还留着
    # `(cavernous hall, giant space:1.6)` 这类权重标注（合成器与 AGENTS 规则现已一律
    # 改用自然语句，见 fast_composer 规则 8/9），带着它去问 VLM"这张图符不符合提示词"
    # 会让它把负向词当成正向要求去找。
    #
    # ⚠️ 这里**不是**提示词正文的收口点。交付正文的唯一出口仍是
    # apply_proactive_fixes 末尾那一处——别把清洗逻辑往这边搬，两个出口就意味着
    # 两套口径。本函数改的是 clean_prompt 这个局部变量，绝不回写 item/manifest。
    clean_prompt = re.sub(r'\s*\([a-zA-Z0-9_,\s\-:]+:[0-9\.]+\)\s*$', '', prompt_text).strip()

    # Prepare multimodal message
    user_text_parts = [
        f"--- 当前帧施工与画面要求 (IMG {seq:03d}) ---",
        f"【本帧施工节点与提示词要求】:",
        clean_prompt,
        "",
    ]
    
    image_paths_to_send = []
    if reference_path and os.path.exists(reference_path) and os.path.getsize(reference_path) > 0:
        user_text_parts.append("附带的第 1 张图片为【前序基准参考图】（来自前一拍已确认的官方基底画面）。")
        image_paths_to_send.append(reference_path)
    else:
        user_text_parts.append("本帧为【第 1 帧 / 锚点基准帧】，无前序参考图，请重点评估提示词还原度、透视构图与画面质感。")

    if ref_frame_path and os.path.exists(ref_frame_path) and os.path.getsize(ref_frame_path) > 0:
        ref_img_idx = len(image_paths_to_send) + 1
        user_text_parts.append(f"附带的第 {ref_img_idx} 张图片为【爆款原片对标参考图 (IMAGE REF)】（来自爆款视频同阶段真实关键帧，用于对标机位视角、构图留白、道具尺度与交付量级）。【绝对基准注意】：所有候选图必须以原片参考图的真实视角与场景形态为准。若原片无圆形管道/隧道黑色画框，候选图绝不可凭空出现黑圈暗角/管道伪影，违者直接大幅扣分！")
        image_paths_to_send.append(ref_frame_path)

    if succeeding_path and os.path.exists(succeeding_path) and os.path.getsize(succeeding_path) > 0:
        succ_img_idx = len(image_paths_to_send) + 1
        user_text_parts.append(f"附带的第 {succ_img_idx} 张图片为【后续目标参考图】（来自后一拍已生成的官方画面，用于三明治前后双向闭环校验，确保当前帧与后续帧不脱节断层）。")
        if succeeding_prompt:
            user_text_parts.append(f"【后续帧目标要求】: {succeeding_prompt.strip()}")
        image_paths_to_send.append(succeeding_path)

    start_cand_idx = len(image_paths_to_send) + 1
    user_text_parts.append(f"随后的图片依次为【候选图 1】至【候选图 {len(candidate_paths)}】（对应附件中第 {start_cand_idx} 至 {start_cand_idx + len(candidate_paths) - 1} 张图片）。")

    for idx, cp in enumerate(candidate_paths):
        user_text_parts.append(f"- 候选图 {idx+1}: {os.path.basename(cp)}")
        image_paths_to_send.append(cp)

    user_text_parts.append("\n【重要】：请全量严格使用【简体中文】输出 JSON 评审分析，对各候选图的优势（strengths）、缺陷（defects）与最终优选结论（selection_reason）进行中文评价。")
    user_text = "\n".join(user_text_parts)


    try:
        response_text = _multimodal_chat(
            config,
            system=_AI_DISCRIMINATION_SYSTEM_PROMPT,
            user_text=user_text,
            image_paths=image_paths_to_send,
            # 4 candidates × 16 subscores plus Chinese findings no longer fits the old
            # 2k ceiling reliably; truncating this JSON would incorrectly trigger fallback.
            max_tokens=4000,
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
        normalized = _normalize_candidate_evaluation(result_data, len(candidate_paths))
        best_idx = normalized['best_index']
        model_best_idx = normalized['model_best_index']
        cands_meta = normalized['candidates']
        model_reason = result_data.get('selection_reason') or ''
        if normalized['all_candidates_disqualified']:
            selection_reason = (
                f"全部候选均命中淘汰级硬伤；暂按后端得分采用候选 #{best_idx}，必须人工复核。"
            )
        elif best_idx != model_best_idx:
            selection_reason = (
                f"后端按细分项汇总与硬伤规则选中候选 #{best_idx}"
                + (f"；模型原建议 #{model_best_idx}。{model_reason}" if model_reason else
                   f"；模型原建议 #{model_best_idx}。")
            )
        else:
            selection_reason = model_reason or f"后端汇总评分选出最佳候选 #{best_idx}"

        return {
            "candidates": cands_meta,
            "best_index": best_idx,
            "model_best_index": model_best_idx,
            "scoring_version": "structured-16-v1",
            "all_candidates_disqualified": normalized['all_candidates_disqualified'],
            "selection_reason": selection_reason,
            "raw_response": response_text[:500],
        }
    except Exception as e:
        log('WARN', 'CANDIDATE_AI', f"IMG {seq:03d} AI 多模态鉴别调用异常 ({e})，默认采纳第 1 张候选")
        # Fallback to candidate 1
        return {
            "candidates": [{
                "index": i + 1,
                "score": None,
                "score_source": "evaluation_unavailable",
                "subscores": {},
                "group_scores": {},
                "hard_flags": [],
                "disqualified": False,
                "strengths": "未评分",
                "defects": "鉴别服务不可用",
            } for i in range(len(candidate_paths))],
            "best_index": 1,
            "model_best_index": 1,
            "scoring_version": "unavailable",
            "selection_reason": f"AI鉴别服务暂不可用 ({e})，已默认采纳候选 #1",
        }


def _generate_full_collage_from_frames(frames_dir, project_dir=None, manifest=None):
    """Generate 5-col collage image using ffmpeg for visual consistency checking."""
    frame_files = sorted(glob.glob(os.path.join(frames_dir, 'img_*.webp')))
    if not frame_files:
        return None
    outpath = os.path.join(frames_dir, 'full_collage.jpg')
    try:
        from tools.collage import build_keyframe_collage
        from pathlib import Path
        build_keyframe_collage(frame_files, outpath, columns=5, tile_width=240, max_frames=0)
        if project_dir:
            c_name = f"{os.path.basename(os.path.normpath(project_dir))}_collage.jpg"
            c_path = Path(project_dir) / c_name
            build_keyframe_collage(frame_files, c_path, columns=5, tile_width=240, max_frames=0)
            if manifest is not None and c_path.exists():
                manifest['collage_url'] = '/' + os.path.relpath(str(c_path), os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        return outpath if os.path.exists(outpath) else None
    except Exception:
        pass

    cols = 5
    rows = math.ceil(len(frame_files) / cols)
    n = len(frame_files)
    input_args = []
    for path in frame_files:
        input_args.extend(['-i', path])

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


def run_candidate_selection_frame_sequence(config, title, prompt_block, on_progress=None, target_sequences=None, candidate_count=4, chain_guard_review=True):
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
                    if isinstance(existing_manifest['frames'], list):
                        manifest['frames'] = [f for f in existing_manifest['frames'] if isinstance(f, dict)]
                    manifest['created_at'] = existing_manifest.get('created_at', manifest['created_at'])
                    for _k, _v in existing_manifest.items():
                        if _k not in manifest or manifest[_k] is None:
                            manifest[_k] = _v
        except Exception:
            pass

    # 上一趟停链留下的印记不能顺着上面那圈"未知键原样继承"带进这一趟：带进来的话，
    # 这一趟哪怕从断点顺利渲到底，返回的 manifest 里仍挂着 halted_at_beat，前端会
    # 一直把这单报成「已暂停」。这一趟停没停，只由这一趟自己写。
    manifest.pop('halted_at_beat', None)
    manifest.pop('halted_at_sequence', None)

    manifest_frames_by_seq = {
        int(f.get('sequence') or f.get('slot')): f
        for f in manifest.get('frames', [])
        if isinstance(f, dict) and (f.get('sequence') or f.get('slot'))
    }
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
    # autofix 就地改写过的提示词正文：改过就要还给调用方（manifest['prompt_block'] →
    # syncFrameRunToLibrary 会写回创意），否则下次重渲又会拿着旧提示词跑一遍。
    autofixed_prompt_block = None
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
                _fx_clear_frame_reference(frames_dir, 1)
                cover_ref = resolve_cover_reference(config, title)
                if cover_ref:
                    reference = _fx_cover_ref_jpg(cover_ref, frames_dir)
                else:
                    reference = None
            else:
                reference = _fx_find_ref_for(frames_dir, seq) or previous_path
        else:
            cover_ref = (resolve_cover_reference(config, title) if seq == 1 else None)
            cover_anchor = bool(cover_ref)
            reference = cover_ref if cover_anchor else (previous_path if seq > 1 else None)
            if not reference and seq > 1:
                durable_parent = os.path.join(frames_dir, f'img_{seq - 1:03d}.webp')
                if os.path.exists(durable_parent) and os.path.getsize(durable_parent) > 0:
                    reference = durable_parent

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
                'message': f"IMG {seq:03d} {len(candidate_paths)}张候选图生成完毕，准备 AI 鉴别...",
            })

        # ── Step 2: AI Multi-modal Evaluation & Selection (with Sandwich Bidirectional Check if succ frame exists) ──
        succeeding_frame_path = None
        succeeding_prompt_text = None
        if frames_dir:
            potential_succ = os.path.join(frames_dir, f'img_{seq + 1:03d}.webp')
            if os.path.exists(potential_succ) and os.path.getsize(potential_succ) > 0:
                succeeding_frame_path = potential_succ
                succeeding_prompt_text = prompts_by_seq.get(seq + 1, {}).get('prompt')

        # 爆款原片对标参考关键帧
        ref_frame_path = None
        try:
            from prompt_pipeline import find_reference_frames_for_project
            ref_dict, _ = find_reference_frames_for_project(project_dir, len(prompts_by_seq) - 1)
            ref_frame_path = ref_dict.get(seq)
        except Exception:
            ref_frame_path = None

        eval_kw = {'on_progress': on_progress}
        if ref_frame_path and os.path.exists(ref_frame_path):
            eval_kw['ref_frame_path'] = ref_frame_path
        if succeeding_frame_path:
            eval_kw['succeeding_path'] = succeeding_frame_path
        if succeeding_prompt_text:
            eval_kw['succeeding_prompt'] = succeeding_prompt_text

        try:
            eval_result = evaluate_and_select_best_candidate(
                config, item.get('prompt', ''), reference, candidate_paths, seq, **eval_kw
            )
        except TypeError as te:
            if 'succeeding_path' in str(te) or 'unexpected keyword' in str(te) or 'ref_frame_path' in str(te):
                eval_result = evaluate_and_select_best_candidate(
                    config, item.get('prompt', ''), reference, candidate_paths, seq, on_progress=on_progress
                )
            else:
                raise



        cand_path_indices = []
        for cp in candidate_paths:
            m = re.search(r'candidate_(\d+)(?:_.*)?\.webp$', os.path.basename(cp))
            cand_path_indices.append(int(m.group(1)) if m else len(cand_path_indices) + 1)

        eval_best_pos = eval_result.get('best_index', 1)
        if not isinstance(eval_best_pos, int) or eval_best_pos < 1 or eval_best_pos > len(candidate_paths):
            eval_best_pos = 1
        best_actual_idx = cand_path_indices[eval_best_pos - 1]
        best_candidate_path = candidate_paths[eval_best_pos - 1]
        selection_reason = eval_result.get('selection_reason', '')

        # ── Step 3: Copy Best Candidate to Target Frame Path & fx_src ──
        shutil.copy2(best_candidate_path, target_path)

        best_cand_meta = cand_meta_map.get(best_actual_idx, {})
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

        # ── Step 4: Record into Manifest (全量保留候选池所有批次候选) ──
        existing_frame = manifest_frames_by_seq.get(seq, {})
        
        # Build evaluation mapping for the newly evaluated candidates
        eval_cands_list = eval_result.get('candidates') or []
        temp_manifest_cands = []
        for i, cp in enumerate(candidate_paths):
            c_idx = cand_path_indices[i]
            c_meta = cand_meta_map.get(c_idx, {})
            eval_c = eval_cands_list[i] if i < len(eval_cands_list) else {}
            c_model = c_meta.get('model') or (backend if backend == 'google_fx' else (config.get('imageModel') or 'gemini-3.1-flash-image'))
            c_model_display = 'Google FX' if ('google_fx' in str(c_model).lower() or backend == 'google_fx') else ('GPT-2' if 'gpt' in str(c_model).lower() else 'Gemini')
            temp_manifest_cands.append({
                'index': c_idx,
                'fx_uuid': c_meta.get('fx_uuid'),
                'model': c_model,
                'model_display': c_model_display,
                'score': eval_c.get('score'),
                'strengths': eval_c.get('strengths', ''),
                'defects': eval_c.get('defects', ''),
                'raw_score': eval_c.get('raw_score'),
                'score_cap': eval_c.get('score_cap'),
                'score_source': eval_c.get('score_source'),
                'subscores': eval_c.get('subscores') or {},
                'group_scores': eval_c.get('group_scores') or {},
                'hard_flags': eval_c.get('hard_flags') or [],
                'disqualified': bool(eval_c.get('disqualified')),
            })

        # Merge existing candidates with newly evaluated candidates
        merged_cands_feed = (existing_frame.get('candidates', []) if isinstance(existing_frame.get('candidates'), list) else []) + temp_manifest_cands

        full_candidates, _, _ = sync_frame_candidates_pool(
            frames_dir, seq,
            current_frame={
                'chosen_candidate_index': best_actual_idx,
                'candidates': merged_cands_feed,
                'model': existing_frame.get('model'),
                'fx_uuid': best_uuid,
            },
            target_path=target_path,
            auto_archive_current=False,
            project_url=project_url
        )
        rel_target_path = os.path.relpath(target_path, workspace_root).replace('\\', '/')

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
            'vlm_qa_reason': f"AI 4选1 鉴别优选 (候选 #{best_actual_idx}): {selection_reason}",
            'selection_mode': 'candidate_selection',
            'chosen_candidate_index': best_actual_idx,
            'ai_evaluation': eval_result,
            'candidates': full_candidates,
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
                'best_index': best_actual_idx,
                'selection_reason': selection_reason,
                'scores': [c.get('score') for c in full_candidates],
                'message': f"IMG {seq:03d} AI 鉴别选中候选 #{best_actual_idx}：{selection_reason}",
            })
            # guard_pending：这一帧落盘后还有一道链上守卫要审（见下面的守卫分支）。
            # frame_data 里的 quality_gate 恒为 'auto_approved'（4选1 优选的结论），
            # 前端拿它打的"完成（质检通过）"跟一致性审查毫无关系——不声明还有后续
            # 审查，用户就会先看到一行绿的、过一会儿卡片突然变红。
            _guard_pending = (
                (chain_guard_mode(config) if chain_guard_review else 'off') != 'off'
                and (seq == 1 or os.path.exists(os.path.join(frames_dir, f'img_{seq - 1:03d}.webp')))
            )
            on_progress('frame', {
                'sequence': seq,
                'slot': seq,
                'current': len([f for f in manifest['frames'] if f.get('file')]),
                'total': total_to_generate,
                'frame': frame_data,
                'guard_pending': bool(_guard_pending),
            })

        # 链上逐拍守卫审查
        #
        # chain_guard_review=False：这一趟整个不审。只有守卫自己的 autofix 分支会传
        # （fix_frame_issue(suppress_chain_guard=True) 转下来）——那条路在本函数返回后
        # 立刻对同一拍、同一对图再跑一次 guard_beat 拿停链结论，这里再审一遍是纯重复
        # 的一整套 check + 逐条复核 + 分级，而且两次判定还可能互相打架。
        guard_mode = chain_guard_mode(config) if chain_guard_review else 'off'
        if guard_mode != 'off':
            if seq == 1:
                try:
                    from chain_guard import run_anchor_guard

                    def _resync_from_disk():
                        updated_manifest = read_manifest(project_dir)
                        if updated_manifest and 'frames' in updated_manifest:
                            manifest['frames'] = updated_manifest['frames']
                            for f_entry in manifest['frames']:
                                if f_entry.get('sequence') == seq:
                                    manifest_frames_by_seq[seq] = f_entry
                                    break

                    forward_build = (target_sequences is None)
                    anchor_res = run_anchor_guard(
                        config, title, prompt_block, project_dir,
                        guard_mode=guard_mode, forward_build=forward_build,
                        on_progress=on_progress,
                        on_manifest_dirty=_resync_from_disk,
                        autofix_attempts=_CHAIN_GUARD_AUTOFIX_ATTEMPTS,
                    )
                    new_block = anchor_res.get('prompt_block')
                    if new_block and new_block != prompt_block:
                        prompt_block = new_block
                        autofixed_prompt_block = new_block
                    # 首帧判废就停在这里：它是整条 i2i 链的地基，接着往下渲等于让
                    # 一张已知歪掉的图当所有下游帧的底图（与逐拍守卫同一口径）。
                    if anchor_res.get('halt'):
                        manifest['halted_at_beat'] = 0
                        manifest['halted_at_sequence'] = 1
                        break
                except GenerationCancelled:
                    # 自动修复途中用户点了取消：必须原样抛出去，被下面的兜底吞掉
                    # 就成了"点了取消还在接着渲"。
                    raise
                except Exception as e:
                    log('WARN', 'CHAIN_GUARD', f"IMG 001 锚点审查异常: {e}")
            elif seq >= 2:
                beat = seq - 1
                prev_seq = seq - 1
                prev_file = os.path.join(frames_dir, f'img_{prev_seq:03d}.webp')
                if os.path.exists(prev_file):
                    try:
                        from chain_guard import (
                            guard_beat, guard_autofix_enabled, guard_halt_enabled)

                        def _resync_from_disk():
                            """守卫与自动修复都是隔着磁盘改 manifest 的，改完必须把这一份
                            内存副本追上去——否则本函数收尾那次整份写盘会把它们全盖掉。"""
                            updated_manifest = read_manifest(project_dir)
                            if updated_manifest and 'frames' in updated_manifest:
                                manifest['frames'] = updated_manifest['frames']
                                for f_entry in manifest['frames']:
                                    if f_entry.get('sequence') == seq:
                                        manifest_frames_by_seq[seq] = f_entry
                                        break

                        # 定向重渲（单帧重试 / fix_frame_issue 的连带重渲）只审不停也不自动修：
                        # 那一趟的下游帧本来就要被重新盖掉，中途停链会留下一条修了一半的链
                        # （上游新图 + 下游旧血统），正是 cascade_downstream 要消灭的
                        # stale_lineage；而自动修复本身就是靠调 fix_frame_issue 实现的，
                        # 在它内部再触发一次就是无限递归。停与修都只在向前建链时才成立。
                        forward_build = (target_sequences is None)
                        guard_res = guard_beat(
                            config, title, prompt_block, beat, project_dir,
                            on_progress=on_progress,
                            allow_halt=forward_build,
                        )
                        _resync_from_disk()

                        # autofix：就地走一遍「修复此帧问题」，复审过了就接着往下渲。
                        if guard_res.get('halt') and guard_autofix_enabled(guard_mode) and forward_build:
                            from pipeline_orchestrator import fix_frame_issue
                            for attempt in range(1, _CHAIN_GUARD_AUTOFIX_ATTEMPTS + 1):
                                texts = '；'.join(
                                    i.get('text') or '' for i in (guard_res.get('issues') or [])
                                    if i.get('severity') == 'chain') or '结构级链式问题'
                                if on_progress:
                                    on_progress('chain_guard_autofix', {
                                        'beat': beat, 'sequence': seq,
                                        'attempt': attempt, 'max_attempts': _CHAIN_GUARD_AUTOFIX_ATTEMPTS,
                                        'issues': guard_res.get('issues', []),
                                        'message': f"🔧 第 {beat} 拍检出结构级问题，正在就地自动修复 "
                                                   f"IMG {seq:03d}（第 {attempt}/{_CHAIN_GUARD_AUTOFIX_ATTEMPTS} 次）：{texts}",
                                    })
                                # 下游此刻还不存在，没有血统要清 → 不必连带重渲。
                                # suppress_chain_guard：重渲内部不必再审这一拍，下面几行
                                # 立刻就会拿 allow_halt=True 亲自再判一次。
                                try:
                                    fix_res = fix_frame_issue(
                                        config, title, prompt_block, seq,
                                        on_progress=on_progress, cascade_downstream=False,
                                        suppress_chain_guard=True,
                                    )
                                except GenerationCancelled:
                                    raise
                                except Exception as fix_err:
                                    # 修复本身没跑成（网关异常/没有可修的记录）≠ 这帧没问题。
                                    # 此刻 guard_res 仍是 halt，跳出去让下面的停链分支接管——
                                    # 绝不能吞掉异常继续往下渲，那等于拿一张已知有结构问题的
                                    # 图当所有下游帧的 i2i 基底。
                                    log('WARN', 'CHAIN_GUARD',
                                        f"IMG {seq:03d} 自动修复第 {attempt} 次未跑成，转为停链: {fix_err}")
                                    break
                                # 三联屏门禁判这次修复把画面改坏了 → 已经自动退回上一版，
                                # 帧图与提示词都回到了修复前。再修一次只会拿同样的输入跑出
                                # 同样的结果，每一轮还要白烧一整套守卫复审。当场转停链，让
                                # 人来看：修后那一版留档在 .frame_fixes/<seq>_rejected，
                                # 门禁误判的话可以在帧网格「采用修后版」。
                                if (fix_res or {}).get('rolled_back'):
                                    log('WARN', 'CHAIN_GUARD',
                                        f"IMG {seq:03d} 自动修复第 {attempt} 次被三联屏门禁退回"
                                        f"（画面已还原），转为停链等人处理")
                                    if on_progress:
                                        on_progress('chain_guard_autofix_rolled_back', {
                                            'beat': beat, 'sequence': seq, 'attempt': attempt,
                                            'triptych': (fix_res or {}).get('triptych'),
                                            'rejected_fix': (fix_res or {}).get('rejected_fix'),
                                            'message': f"↩️ IMG {seq:03d} 第 {attempt} 次自动修复被三联屏门禁退回，"
                                                       f"画面已还原为修复前，转为停链等人处理",
                                        })
                                    _resync_from_disk()
                                    break

                                new_block = (fix_res or {}).get('prompt_block')
                                if new_block:
                                    # 这一拍的 IMAGE/VIDEO 正文被改写过了。后续帧的提示词
                                    # 没变（prompts 不用重解析），但之后每一拍的守卫都要
                                    # 拿着改写后的全文当上下文，否则它对着旧文本判新画面。
                                    prompt_block = new_block
                                    autofixed_prompt_block = new_block
                                _resync_from_disk()

                                guard_res = guard_beat(
                                    config, title, prompt_block, beat, project_dir,
                                    on_progress=on_progress, allow_halt=True,
                                )
                                _resync_from_disk()
                                if not guard_res.get('halt'):
                                    if on_progress:
                                        on_progress('chain_guard_autofix_done', {
                                            'beat': beat, 'sequence': seq, 'attempt': attempt,
                                            'message': f"✅ IMG {seq:03d} 自动修复后复审通过（第 {attempt} 次），继续往下生成",
                                        })
                                    break

                        # halt 档一检出就停；autofix 档修满次数仍不过才停——继续往下渲
                        # 等于让一张已知有结构问题的图当所有下游帧的 i2i 基底。
                        # autofix_soft 是明确接受这笔代价的那一档：只记账，不停链。
                        still_flagged = bool(guard_res.get('halt'))
                        if still_flagged and guard_halt_enabled(guard_mode):
                            manifest['halted_at_beat'] = beat
                            manifest['halted_at_sequence'] = seq
                            if on_progress:
                                tail = ('' if guard_mode == 'halt'
                                        else f"（已自动修复 {_CHAIN_GUARD_AUTOFIX_ATTEMPTS} 次仍未通过）")
                                on_progress('chain_guard_halt', {
                                    'beat': beat,
                                    'sequence': seq,
                                    'issues': guard_res.get('issues', []),
                                    'autofix_exhausted': guard_autofix_enabled(guard_mode),
                                    'message': f"第 {beat} 拍（IMG {prev_seq:03d}→{seq:03d}）检出结构级链式问题{tail}，生成已自动暂停，请检查并修复此帧问题。",
                                })
                            break
                        if still_flagged and guard_mode == 'autofix_soft' and on_progress:
                            on_progress('chain_guard_soft_continue', {
                                'beat': beat,
                                'sequence': seq,
                                'issues': guard_res.get('issues', []),
                                'autofix_exhausted': True,
                                'message': f"⚠️ 第 {beat} 拍（IMG {prev_seq:03d}→{seq:03d}）仍有结构级问题"
                                           f"（已自动修复 {_CHAIN_GUARD_AUTOFIX_ATTEMPTS} 次），软档不停链——"
                                           f"已记入待复核清单，继续往下渲。",
                            })
                    except Exception as guard_err:
                        log('WARN', 'CHAIN_GUARD', f"拍 {beat} 链上守卫执行异常: {guard_err}")

    # Finalize stale status & capability stamping
    try:
        update_manifest_stale_status(
            manifest, project_dir,
            regenerated_sequences=target_sequences,
            finalize=True,
        )
    except Exception as stale_err:
        log('WARN', 'CANDIDATE_GEN', f"最终收尾 manifest stale 状态异常: {stale_err}")

    # Generate full collage and update manifest collage_url
    _generate_full_collage_from_frames(frames_dir, project_dir=project_dir, manifest=manifest)

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    manifest['project_dir'] = project_dir
    manifest['manifest'] = manifest_path
    # 只放进返回值、**不落盘**（写盘在上面已经做完了）：manifest 的未知键会被下一趟
    # 原样继承（见本函数开头读盘合并那段），落盘的话 syncFrameRunToLibrary 之后每趟
    # 都会拿这份陈旧正文回写创意，跟 halted_at_beat 是同一个坑。
    if autofixed_prompt_block:
        manifest['prompt_block'] = autofixed_prompt_block
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
                full_cands, _, _ = sync_frame_candidates_pool(
                    frames_dir, seq,
                    current_frame=frame,
                    target_path=target_frame_path,
                    auto_archive_current=False
                )
                frame['candidates'] = full_cands
                for cand in full_cands:
                    if cand.get('index') == new_candidate_index and cand.get('model'):
                        frame['model'] = cand['model']
                break

        try:
            update_manifest_stale_status(
                manifest, project_dir,
                regenerated_sequences=[seq],
                finalize=True,
            )
        except Exception as stale_err:
            log('WARN', 'CANDIDATE_GEN', f"更新 manifest stale 状态异常: {stale_err}")

        # Regenerate collage and update manifest
        _generate_full_collage_from_frames(frames_dir, project_dir=project_dir, manifest=manifest)
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest
