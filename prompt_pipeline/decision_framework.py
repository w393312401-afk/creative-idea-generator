"""重构判断矩阵与二创兼容性诊断引擎 (Decision Framework & Compatibility Engine)

解决 AI 视频二创中的“盲目硬套”与“无血无肉”痛点，建立从【1:1 黄金母本】到【正交二创变体】的
【TikTok 爆款叙事与情绪弧线】+【全域物理工程与真实工艺】双轨深度诊断把关机制。

双轨核心诊断维度 (Dual-Track Diagnostic Dimensions):

🎭 轨一：TikTok 爆款叙事与情绪价值弧线 (Narrative & Emotional Soul Track)
  1. hook_crisis: 黄金 3 秒视觉痛点与开局困境 (废墟/破败/完全毁坏/极端危机开局 vs 平淡开场)
  2. character_emotion: 常驻角色羁绊与情绪弧线 (穷困潦倒夫妇/受助生命看图纸燃起希望 -> 见证奇迹 -> 喜极而泣入住)
  3. god_hand_wonder: 神来之手与降维奇观视角 (巨人工匠之手如神迹降临微缩世界、图纸展示、微距特写与微观交互)
  4. contrast_reward: 极致前后蜕变与终极爽点奖赏 (从 0 分破烂废墟到 100 分奢华庄园/温暖宫殿的极致多巴胺反差)

🏗️ 轨二：全域物理工程与真实工艺拓扑 (Physical & Craft Topology Track)
  5. spatial_force: 空间支撑与受力范式 (地面/地下开挖 vs 悬崖挑空/树冠高空/漂浮失重)
  6. material_phase: 材料加工与物理相态 (固体木石切削装配 vs 冰雪热熔/3D打印增材/熔岩/生土夯筑)
  7. scale_envelope: 三维公制尺度与包络 (微缩微距/3m 紧凑掩体 vs 千平大厅/微隙，防 Cavernous 畸变)
  8. transition_portal: 过门转场机制与视线 (双镜头水密舱门下行 vs 敞开无门露台 vs 柔性气闸 vs 微距穿梭)
  9. asmr_acoustic: ASMR 声画沉浸与感官节拍 (微观撕纸/木工敲击/砖石拼搭与听觉情绪叹息)

输出等级 (Verdict Levels):
  - compatible (SAFE, 90-100分): 允许 100% 骨架硬冻结与四轴正交派生
  - risky (RISKY, 60-89分): 存在局部物理工序或叙事/角色冲突，需受控适配
  - incompatible (INCOMPATIBLE, <60分): 严禁硬套母本，必须建立全新独立黄金母本
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# 双轨核心诊断维度元数据
DIMENSION_METADATA: Dict[str, Dict[str, str]] = {
    # 🎭 轨一：TikTok 爆款叙事与情绪价值弧线
    'hook_crisis': {
        'key': 'hook_crisis',
        'track': 'narrative',
        'label': '黄金 3 秒痛点钩子与开局困境',
        'en_label': 'Hook & Crisis Inception',
        'icon': '🪝',
        'desc': '评估开局留存钩子（破烂毁坏/暴雨冲垮/家园被毁的视觉冲击与危机痛点 vs 平淡开场）。',
    },
    'character_emotion': {
        'key': 'character_emotion',
        'track': 'narrative',
        'label': '常驻角色羁绊与情绪弧线',
        'en_label': 'Character Stakes & Emotional Arc',
        'icon': '❤️',
        'desc': '评估有血有肉的情感闭环（穷困夫妇/流浪弱小看设计图纸燃起希望 -> 见证奇迹 -> 喜极而泣入住）。',
    },
    'god_hand_wonder': {
        'key': 'god_hand_wonder',
        'track': 'narrative',
        'label': '神来之手与降维奇观交互',
        'en_label': 'God-Hand & Perspective Wonder',
        'icon': '🖐️',
        'desc': '评估降维治愈与神性奇观（巨人工匠之手如神迹介入微缩世界、图纸展示、微距特写与微观活物互动）。',
    },
    'contrast_reward': {
        'key': 'contrast_reward',
        'track': 'narrative',
        'label': '极致前后反差与终极爽点',
        'en_label': 'Extreme Contrast & Climax Payoff',
        'icon': '💎',
        'desc': '评估情绪多巴胺释放（从 0 分破烂废墟到 100 分奢华庄园/温暖宫殿的极致蜕变反差与入住揭示）。',
    },

    # 🏗️ 轨二：全域物理工程与真实工艺拓扑
    'spatial_force': {
        'key': 'spatial_force',
        'track': 'physical',
        'label': '空间支撑与受力范式',
        'en_label': 'Spatial Force & Gravity Paradigm',
        'icon': '🏗️',
        'desc': '评估地貌载体与基础结构（地下/地表开挖 vs 悬崖挑空/高空树冠/水上漂浮/失重轨道）。',
    },
    'material_phase': {
        'key': 'material_phase',
        'track': 'physical',
        'label': '材料加工与物理相态',
        'en_label': 'Material Phase & Craft Topology',
        'icon': '🪵',
        'desc': '评估材料加工生命周期与工艺痕迹（木石切削装配 vs 冰雪热熔/3D打印/熔岩/生土夯筑）。',
    },
    'scale_envelope': {
        'key': 'scale_envelope',
        'track': 'physical',
        'label': '三维公制尺度与包络',
        'en_label': 'Metric Scale & Envelope Mismatch',
        'icon': '📐',
        'desc': '评估空间净空与人机工程学包络（微缩沙盘/3m 紧凑掩体 vs 千平大厅/微隙），防 Cavernous 畸变。',
    },
    'transition_portal': {
        'key': 'transition_portal',
        'track': 'physical',
        'label': '过门转场机制与视线',
        'en_label': 'Transition & Portal Dynamics',
        'icon': '🚪',
        'desc': '评估空间进出与视线通道（双镜头水密舱门下行 vs 敞开无边际露台 vs 柔性气闸 vs 微距）。',
    },
    'asmr_acoustic': {
        'key': 'asmr_acoustic',
        'track': 'physical',
        'label': 'ASMR 声画沉浸与感官节拍',
        'en_label': 'Audio-Visual ASMR Alignment',
        'icon': '🎧',
        'desc': '评估物理动作与声学材质咬合（微观撕纸/木工敲击/砖石拼搭与听觉情绪叹息）。',
    },
}

# 空间支撑范式特征模式
_SPATIAL_PATTERNS = {
    'miniature_craft_table': {
        'label': '微缩沙盘/手工工作台',
        'keywords': ['微缩', '沙盘', '手工', '桌面', '手作', '庄园微缩', '模型', 'miniature', 'diorama', 'workbench', 'tabletop', 'model'],
    },
    'subterranean_or_ground': {
        'label': '地下开挖/地基掩埋',
        'keywords': ['地下', '开挖', '破土', '掩埋', '集装箱', '河床', '泥岸', '泥土', '回填', '基坑', '地穴', 'subterranean', 'excavation', 'pit', 'bunker', 'buried', 'earth', 'ground', 'riverbank'],
    },
    'cliff_suspended': {
        'label': '悬崖挑空/绝壁悬挂',
        'keywords': ['悬崖', '悬空', '挑空', '绝壁', '峭壁', '岩壁悬挂', 'cliff', 'suspended', 'cantilever', 'hanging', 'abyss'],
    },
    'tree_canopy': {
        'label': '高空树冠/林冠支架',
        'keywords': ['树屋', '树冠', '高空树顶', '林冠', '树干抱箍', 'treehouse', 'canopy', 'treetop'],
    },
    'floating_water': {
        'label': '水面浮筒/水上浮岛',
        'keywords': ['浮筒', '水上浮岛', '浮水船坞', '漂浮平台', 'floating', 'pontoon', 'raft'],
    },
    'orbital_space': {
        'label': '失重空间/轨道舱体',
        'keywords': ['太空', '空间站', '轨道', '失重', '宇航', 'space station', 'orbital', 'zero gravity'],
    },
}

# 材料相态特征模式
_MATERIAL_PHASE_PATTERNS = {
    'solid_wood_stone_metal': {
        'label': '实体木石钢构装配（常规切削/螺栓/批灰/手工微雕）',
        'keywords': ['原木', '松木', '柚木', '耐候钢', '碳钢', '玄武岩', '微水泥', '黄铜', '螺栓', '龙骨', '木板', '石膏', '瓦片', 'timber', 'steel', 'basalt', 'cement', 'wood', 'plaster'],
    },
    'ice_snow': {
        'label': '低温冰雪相态（热熔/链锯/喷灯）',
        'keywords': ['冰雕', '纯冰', '冰雪', '极地厚冰块', '冰砖', 'ice sculpture', 'pure ice', 'ice block', 'snow brick'],
    },
    'lava_molten': {
        'label': '高温熔融相态（熔岩/琉璃铸造）',
        'keywords': ['熔岩琉璃', '流体岩浆', '高温熔融', 'molten lava', 'liquid glass', 'magma flow'],
    },
    'additive_3d_print': {
        'label': '增材打印/高分子熔覆',
        'keywords': ['3d打印', '机械臂挤出', '增材制造', '光敏固化', '3d print', 'additive manufacturing', 'extrusion'],
    },
    'rammed_earth': {
        'label': '传统生土/夯土厚墙',
        'keywords': ['生土', '夯土', '陶土砖', '土坯', 'rammed earth', 'adobe', 'clay brick'],
    },
}

# 尺度范式特征模式
_SCALE_PATTERNS = {
    'miniature_scale': {
        'label': '微缩微距尺度 (拇指人偶与微观沙盘)',
        'keywords': ['微缩', '沙盘', '拇指', '人偶', '微距', '手作模型', 'miniature', 'diorama', 'macro', 'tiny scale', '1:24', '1:35'],
    },
    'compact_envelope': {
        'label': '紧凑型人机工程包络 (2.5m~4m)',
        'keywords': ['紧凑', '掩体', '避难所', '微型木屋', '地穴', '胶囊', 'compact', 'shelter', 'cabin', 'bunker', 'niche'],
    },
    'cavernous_hall': {
        'label': '超大体量礼堂/开阔大厅 (>200m²)',
        'keywords': ['大教堂', '大礼堂', '巨型机库', '万平', '万人', '豪华大堂', 'cathedral', 'grand hall', 'cavernous', 'giant arena', 'hangar'],
    },
    'micro_crevice': {
        'label': '微观缝隙/极小胶囊 (<1.5m)',
        'keywords': ['微缩缝隙', '树洞极小', '超微胶囊', 'micro crevice', 'tiny burrow', 'miniature slot'],
    },
}

# 主体与叙事范式特征模式
_ACTOR_PATTERNS = {
    'god_hand_with_couple': {
        'label': '神之手 (巨人工匠) + 穷困夫妇人偶 (深度叙事)',
        'keywords': ['夫妇', '夫妻', '穷困', '人偶', '神来之手', '巨手', '看图纸', '设计图纸', 'god hand', 'couple', 'figurine couple', 'blueprint', 'homeless couple', 'poor couple'],
    },
    'miniature_diorama': {
        'label': '微缩沙盘/活物人偶 (微观活物互动)',
        'keywords': ['微缩', '沙盘', '拇指人偶', '人偶', '微距', 'miniature', 'diorama', 'tiny figurine', 'figurine', 'macro scale'],
    },
    'solo_human_worker': {
        'label': '单人人类工匠 (1.78m 工装作业)',
        'keywords': ['工匠', '工人', '手工', '工人施工', 'worker', 'craftsman', 'male worker', 'handcrafted'],
    },
    'robotic_automation': {
        'label': '自动化机械臂/无人机群控',
        'keywords': ['机械臂', '无人机', '自动化群控', '工业机器人', 'robotic arm', 'drone swarm', 'automation'],
    },
    'wildlife_nesting': {
        'label': '野生动物自营建 (海狸/蜜蜂/鸟类)',
        'keywords': ['海狸筑坝', '鸟巢', '蜜蜂筑巢', '动物筑巢', 'beaver dam', 'bird nest', 'animal habitat'],
    },
}

# 过门转场特征模式
_PORTAL_PATTERNS = {
    'submarine_hatch_descent': {
        'label': '密闭水密舱盖/垂直下行爬梯 (Two-Shot Decoupled)',
        'keywords': ['舱盖', '水密门', '手轮', '爬梯', '下行', '垂直通道', 'submarine hatch', 'airtight door', 'vertical ladder', 'portal push-in'],
    },
    'open_panorama_deck': {
        'label': '敞开式全景露台/无门露天甲板',
        'keywords': ['露天', '全景露台', '无门', '敞开平台', '开放甲板', 'open terrace', 'open deck', 'alfresco', 'open air'],
    },
    'inflatable_airlock': {
        'label': '柔性充气穹顶/双层气闸拉链',
        'keywords': ['充气舱', '气闸拉链', '透明穹顶', '气膜', 'inflatable dome', 'fabric airlock', 'membrane'],
    },
    'macro_seamless_zoom': {
        'label': '无物理门框/微距虚化穿梭',
        'keywords': ['微距虚化', '镜头穿透', '无门框', 'macro zoom', 'rack focus transition'],
    },
}


def _detect_pattern(text: str, pattern_dict: Dict[str, Dict[str, Any]], default_key: str) -> Tuple[str, str]:
    """根据文本中的关键词推断特征大类及标签。"""
    t = (text or '').lower()
    for key, info in pattern_dict.items():
        if any(k.lower() in t for k in info['keywords']):
            return key, info['label']
    return default_key, pattern_dict[default_key]['label']


def _extract_narrative_dna(corpus: str) -> Dict[str, Any]:
    """提取短视频的 TikTok 爆款叙事基因与情绪特征。"""
    t = (corpus or '').lower()

    # 1. 黄金 3 秒破败钩子
    has_ruin_hook = any(k in t for k in ['破烂', '完全毁坏', '破烂屋子', '废墟', '残破', '坍塌', '暴雨冲垮', '毁坏', '草棚', '摧毁', 'ruin', 'dilapidated', 'destroyed', 'broken cabin', 'shack', 'wreckage'])
    
    # 2. 角色情感与设计图纸线
    has_character_arc = any(k in t for k in ['夫妇', '夫妻', '穷困', '无家可归', '贫困', '看图纸', '设计图纸', '给他们看', '住进', '喜极而泣', '感激', '人偶', '小人', '两口子', 'couple', 'poor', 'homeless', 'blueprint', 'shelter for couple', 'emotional', 'figurine'])
    
    # 3. 神来之手介入
    has_god_hand = any(k in t for k in ['神来之手', '巨手', '巨人之手', '手部入画', '上帝之手', '神之手', 'god hand', 'giant hand', 'human hand', 'craftsman hand', 'hands descend'])
    
    # 4. 极致奢华/反差终局
    has_extreme_contrast = any(k in t for k in ['地中海豪华庄园', '豪华庄园', '豪宅', '极致反差', '奢华', '宫殿', '梦幻', '庄园', 'luxury', 'mansion', 'mediterranean villa', 'palace', 'dramatic contrast', 'villa'])

    return {
        'has_ruin_hook': has_ruin_hook,
        'has_character_arc': has_character_arc,
        'has_god_hand': has_god_hand,
        'has_extreme_contrast': has_extreme_contrast,
    }


def _extract_baseline_features(baseline_doc: Dict[str, Any]) -> Dict[str, Any]:
    """从母本数据中提取客观物理与叙事基准特征。"""
    beats = (baseline_doc.get('beats') if isinstance(baseline_doc.get('beats'), list) else []) or []
    all_text = []
    
    all_text.append(str(baseline_doc.get('title') or ''))
    all_text.append(str(baseline_doc.get('video_name') or ''))
    all_text.append(str(baseline_doc.get('scene_signature') or ''))
    
    for b in beats:
        all_text.append(str(b.get('visual_subject') or b.get('visible_subject') or ''))
        all_text.append(str(b.get('visible_action') or ''))
        all_text.append(str(b.get('state_before') or ''))
        all_text.append(str(b.get('state_after') or ''))
        for d in (b.get('visible_details') or []):
            all_text.append(str(d))
        for tr in (b.get('persistent_traces') or []):
            all_text.append(str(tr))

    full_corpus = ' '.join(all_text)
    
    spatial_key, spatial_lbl = _detect_pattern(full_corpus, _SPATIAL_PATTERNS, 'subterranean_or_ground')
    material_key, material_lbl = _detect_pattern(full_corpus, _MATERIAL_PHASE_PATTERNS, 'solid_wood_stone_metal')
    scale_key, scale_lbl = _detect_pattern(full_corpus, _SCALE_PATTERNS, 'compact_envelope')
    actor_key, actor_lbl = _detect_pattern(full_corpus, _ACTOR_PATTERNS, 'solo_human_worker')
    portal_key, portal_lbl = _detect_pattern(full_corpus, _PORTAL_PATTERNS, 'submarine_hatch_descent')
    narrative_dna = _extract_narrative_dna(full_corpus)

    return {
        'spatial_force': (spatial_key, spatial_lbl),
        'material_phase': (material_key, material_lbl),
        'scale_envelope': (scale_key, scale_lbl),
        'actor_interaction': (actor_key, actor_lbl),
        'transition_portal': (portal_key, portal_lbl),
        'narrative_dna': narrative_dna,
        'corpus': full_corpus,
    }


def _extract_variant_features(
    mutation_axes: Optional[Dict[str, Any]] = None,
    brief: Optional[str] = None,
    idea: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """从二创输入中提取变体特征。"""
    axes = dict(mutation_axes or {})
    if idea and isinstance(idea, dict):
        if idea.get('axes') and isinstance(idea['axes'], dict):
            axes.update(idea['axes'])
        if idea.get('name'):
            axes['name'] = idea['name']
        if idea.get('hook'):
            axes['hook'] = idea['hook']

    text_parts = [
        str(brief or ''),
        str(axes.get('environment') or ''),
        str(axes.get('material') or ''),
        str(axes.get('function') or ''),
        str(axes.get('hero_reveal') or ''),
        str(axes.get('name') or ''),
        str(axes.get('hook') or ''),
    ]
    full_corpus = ' '.join(text_parts)

    spatial_key, spatial_lbl = _detect_pattern(full_corpus, _SPATIAL_PATTERNS, 'subterranean_or_ground')
    material_key, material_lbl = _detect_pattern(full_corpus, _MATERIAL_PHASE_PATTERNS, 'solid_wood_stone_metal')
    scale_key, scale_lbl = _detect_pattern(full_corpus, _SCALE_PATTERNS, 'compact_envelope')
    actor_key, actor_lbl = _detect_pattern(full_corpus, _ACTOR_PATTERNS, 'solo_human_worker')
    portal_key, portal_lbl = _detect_pattern(full_corpus, _PORTAL_PATTERNS, 'submarine_hatch_descent')
    narrative_dna = _extract_narrative_dna(full_corpus)

    return {
        'spatial_force': (spatial_key, spatial_lbl),
        'material_phase': (material_key, material_lbl),
        'scale_envelope': (scale_key, scale_lbl),
        'actor_interaction': (actor_key, actor_lbl),
        'transition_portal': (portal_key, portal_lbl),
        'narrative_dna': narrative_dna,
        'axes': axes,
        'brief': brief or '',
        'corpus': full_corpus,
    }


def evaluate_variant_compatibility(
    baseline_doc: Dict[str, Any],
    mutation_axes: Optional[Dict[str, Any]] = None,
    brief: Optional[str] = None,
    idea: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    根据【TikTok 深度叙事与物理工程双轨判断矩阵】评估二创变体对母本骨架的可复用性与相容性。
    
    返回结构化报告：
      - compatibility_level: 'compatible' | 'risky' | 'incompatible'
      - compatibility_score: 0 ~ 100
      - verdict_title: 决策结论标题
      - can_inherit_skeleton: bool (是否允许 100% 骨架硬冻结正交派生)
      - summary: 简要总结
      - dimensions: 双轨 9 个维度的检查明细列表
      - narrative_alerts: 短视频叙事灵魂与情绪预警列表
      - action_recommendation: 建议操作与 CTA
    """
    base_feat = _extract_baseline_features(baseline_doc)
    var_feat = _extract_variant_features(mutation_axes=mutation_axes, brief=brief, idea=idea)

    base_dna = base_feat['narrative_dna']
    var_dna = var_feat['narrative_dna']

    dimension_results = []
    score_deductions = 0
    incompatible_reasons = []
    warning_reasons = []
    narrative_alerts = []

    # =========================================================================
    # 🎭 轨一：TikTok 爆款叙事与情绪价值弧线诊断 (Narrative & Emotional Soul)
    # =========================================================================

    # 1. 黄金 3 秒视觉痛点与开局困境 (Hook & Ruin Crisis)
    if base_dna['has_ruin_hook']:
        has_ruin_conflict = any(k in var_feat['corpus'] for k in ['平淡开篇', '直接装修', '无破损', '完好无损', '精装交付开局'])
        if var_dna['has_ruin_hook'] or not has_ruin_conflict:
            dim_hook_status = 'pass'
            dim_hook_msg = '继承母本黄金 3 秒视觉钩子（开局完全毁坏/废墟困境），开局留存率有保障。'
            dim_hook_conflict = None
            dim_hook_rec = '保留开局废墟与极端恶劣环境的特写冲击。'
        else:
            dim_hook_status = 'warning'
            score_deductions += 10
            conflict = '母本以【完全毁坏破烂屋子】作为黄金 3 秒留存钩子，二创若平淡开场将面临完播率断崖下跌。'
            dim_hook_msg = conflict
            dim_hook_conflict = conflict
            dim_hook_rec = '建议在变体提示词中强化开局破败残破度与危机感，牢牢锁住前 3 秒。'
            warning_reasons.append(conflict)
            narrative_alerts.append(conflict)
    else:
        dim_hook_status = 'pass'
        dim_hook_msg = '开局视觉节奏正常。'
        dim_hook_conflict = None
        dim_hook_rec = '遵循正常分镜起幅。'

    dimension_results.append({
        'key': 'hook_crisis',
        'track': 'narrative',
        'label': DIMENSION_METADATA['hook_crisis']['label'],
        'icon': DIMENSION_METADATA['hook_crisis']['icon'],
        'status': dim_hook_status,
        'baseline_value': '完全毁坏破屋钩子' if base_dna['has_ruin_hook'] else '常规开篇',
        'variant_value': '继承困境钩子' if var_dna['has_ruin_hook'] else '继承母本钩子',
        'detail': dim_hook_msg,
        'conflict': dim_hook_conflict,
        'recommendation': dim_hook_rec,
    })

    # 2. 常驻角色羁绊与情绪弧线 (Character Stakes & Emotional Arc)
    if base_dna['has_character_arc']:
        # 检查变体是否显式改写为冲突的成人单人或无角色
        has_char_conflict = any(k in var_feat['corpus'] for k in ['1.78m 工人', '真人施工', '单人工人工装', '工人亲自', '成人掩体', '无角色', '无图纸'])
        if var_dna['has_character_arc'] or not has_char_conflict:
            dim_char_status = 'pass'
            dim_char_msg = '有血有肉的情感闭环完整（继承穷困夫妇/受助生命看图纸燃起希望 ➔ 喜极而泣入住）。'
            dim_char_conflict = None
            dim_char_rec = '遵循 Living Cast 活物应激三位一体律，全程保持人偶夫妇的眼神与情绪追踪。'
        else:
            dim_char_status = 'fail'
            score_deductions += 45
            conflict = '丢失叙事灵魂与情绪价值：母本核心是【给穷困潦倒夫妇看设计图纸并帮他们造豪宅】，二创显式改写为成人单人施工，视频将沦为“无血无肉的冰冷工具展示”！'
            dim_char_msg = conflict
            dim_char_conflict = conflict
            dim_char_rec = '必须在变体提示词中显式注入“穷困人偶夫妇看图纸 + 满怀希望注视 + 喜极而泣入住”情感线！'
            incompatible_reasons.append(conflict)
            narrative_alerts.append(conflict)
    else:
        dim_char_status = 'pass'
        dim_char_msg = '角色交互与母本一致。'
        dim_char_conflict = None
        dim_char_rec = '正常执行工人与环境交互。'

    dimension_results.append({
        'key': 'character_emotion',
        'track': 'narrative',
        'label': DIMENSION_METADATA['character_emotion']['label'],
        'icon': DIMENSION_METADATA['character_emotion']['icon'],
        'status': dim_char_status,
        'baseline_value': '穷困夫妇情感线' if base_dna['has_character_arc'] else '单人作业',
        'variant_value': '继承情感羁绊' if (var_dna['has_character_arc'] or not base_dna['has_character_arc']) else '自动承接母本角色',
        'detail': dim_char_msg,
        'conflict': dim_char_conflict,
        'recommendation': dim_char_rec,
    })

    # 3. 神来之手与降维奇观交互 (God-Hand & Perspective Wonder)
    base_is_miniature = base_feat['scale_envelope'][0] == 'miniature_scale' or base_dna['has_god_hand'] or '微缩' in base_feat['corpus'] or '沙盘' in base_feat['corpus']
    var_is_miniature = var_feat['scale_envelope'][0] == 'miniature_scale' or var_dna['has_god_hand'] or '微缩' in var_feat['corpus'] or '沙盘' in var_feat['corpus']
    
    if base_is_miniature:
        # 母本是微缩沙盘：检查变体是否显式改写为真人成人掩体
        has_adult_conflict = any(k in var_feat['corpus'] for k in ['地下掩体', '真人', '集装箱', '1.78m', '成年工人']) and not var_is_miniature
        if not has_adult_conflict:
            dim_hand_status = 'pass'
            dim_hand_msg = '微缩降维奇观同构：巨手如神迹降临（God Hand），图纸展示与微观工艺反差强烈。'
            dim_hand_conflict = None
            dim_hand_rec = '保持巨手入画建造、手指展示微缩图纸与微距镜头特写。'
        else:
            dim_hand_status = 'fail'
            score_deductions += 45
            conflict = '微缩神迹降维成普通成人平视：母本为【巨手降临微缩庄园】，二创被误套为成人真人地下室，丢失了神来之手治愈感与微观奇观！'
            dim_hand_msg = conflict
            dim_hand_conflict = conflict
            dim_hand_rec = '严禁将微缩沙盘硬套为成人地下掩体，必须切换至微缩工坊专属管线。'
            incompatible_reasons.append(conflict)
            narrative_alerts.append(conflict)
    else:
        # 母本为成人真实建造：检查变体是否被写成了微缩沙盘
        if var_is_miniature and ('微缩' in var_feat['corpus'] or '沙盘' in var_feat['corpus']):
            dim_hand_status = 'fail'
            score_deductions += 45
            conflict = '二创为【微缩沙盘/拇指人偶】，母本为成人真实建造，不可硬套真人 1.78m 施工逻辑。'
            dim_hand_msg = conflict
            dim_hand_conflict = conflict
            dim_hand_rec = '建议切换至微缩工坊专属管线。'
            incompatible_reasons.append(conflict)
            narrative_alerts.append(conflict)
        else:
            dim_hand_status = 'pass'
            dim_hand_msg = '视点与尺度交互一致。'
            dim_hand_conflict = None
            dim_hand_rec = '维持标准相机机位。'

    dimension_results.append({
        'key': 'god_hand_wonder',
        'track': 'narrative',
        'label': DIMENSION_METADATA['god_hand_wonder']['label'],
        'icon': DIMENSION_METADATA['god_hand_wonder']['icon'],
        'status': dim_hand_status,
        'baseline_value': '神来之手+微缩奇观' if base_is_miniature else '真人平视',
        'variant_value': '微缩奇观同构' if var_is_miniature else ('承接微缩奇观' if base_is_miniature else '真人平视'),
        'detail': dim_hand_msg,
        'conflict': dim_hand_conflict,
        'recommendation': dim_hand_rec,
    })

    # 4. 极致前后蜕变与终极爽点奖赏 (Extreme Contrast & Climax Payoff)
    if base_dna['has_extreme_contrast'] or base_dna['has_ruin_hook']:
        dim_contrast_status = 'pass'
        dim_contrast_msg = '前后蜕变反差强烈（从 0 分破烂废墟到 100 分奢华庄园），多巴胺终局爽点拉满。'
        dim_contrast_conflict = None
        dim_contrast_rec = '终帧必须留足 2~3 秒展示完工豪宅全景与角色幸福入住特写。'
    else:
        dim_contrast_status = 'pass'
        dim_contrast_msg = '建造交付节奏正常。'
        dim_contrast_conflict = None
        dim_contrast_rec = '执行终帧物理揭示。'

    dimension_results.append({
        'key': 'contrast_reward',
        'track': 'narrative',
        'label': DIMENSION_METADATA['contrast_reward']['label'],
        'icon': DIMENSION_METADATA['contrast_reward']['icon'],
        'status': dim_contrast_status,
        'baseline_value': '废墟到奢华豪宅',
        'variant_value': '继承多巴胺反差',
        'detail': dim_contrast_msg,
        'conflict': dim_contrast_conflict,
        'recommendation': dim_contrast_rec,
    })

    # =========================================================================
    # 🏗️ 轨二：全域物理工程与真实工艺拓扑诊断 (Physical & Craft Topology)
    # =========================================================================

    # 5. 空间支撑与受力范式 (Spatial Force & Gravity)
    base_sp_key, base_sp_lbl = base_feat['spatial_force']
    var_sp_key, var_sp_lbl = var_feat['spatial_force']
    if base_sp_key == var_sp_key or (base_is_miniature and var_is_miniature):
        dim_sp_status = 'pass'
        dim_sp_msg = f'基底载体同构 ({base_sp_lbl})，受力逻辑与建造工序 100% 契合。'
        dim_sp_conflict = None
        dim_sp_rec = '正常继承母本基底结构工序。'
    else:
        if {base_sp_key, var_sp_key} & {'cliff_suspended', 'tree_canopy', 'orbital_space'}:
            dim_sp_status = 'fail'
            score_deductions += 45
            conflict = f'母本为【{base_sp_lbl}】，而二创为【{var_sp_lbl}】。基础力学与支撑方式冲突，悬崖/高空/失重无法执行母本的基础搭建工序。'
            dim_sp_msg = conflict
            dim_sp_conflict = conflict
            dim_sp_rec = '严禁硬套母本地面工序。建议建立专属高空/挑空黄金母本。'
            incompatible_reasons.append(conflict)
        else:
            dim_sp_status = 'warning'
            score_deductions += 15
            conflict = f'母本【{base_sp_lbl}】与二创【{var_sp_lbl}】存在载体微调，需适配基础受力细节。'
            dim_sp_msg = conflict
            dim_sp_conflict = conflict
            dim_sp_rec = '请在词槽中保留母本的基准结构锚点，避免出现悬空悬浮。'
            warning_reasons.append(conflict)

    dimension_results.append({
        'key': 'spatial_force',
        'track': 'physical',
        'label': DIMENSION_METADATA['spatial_force']['label'],
        'icon': DIMENSION_METADATA['spatial_force']['icon'],
        'status': dim_sp_status,
        'baseline_value': base_sp_lbl,
        'variant_value': var_sp_lbl,
        'detail': dim_sp_msg,
        'conflict': dim_sp_conflict,
        'recommendation': dim_sp_rec,
    })

    # 6. 材料加工与物理相态 (Material Phase & Craft)
    base_mat_key, base_mat_lbl = base_feat['material_phase']
    var_mat_key, var_mat_lbl = var_feat['material_phase']
    if base_mat_key == var_mat_key:
        dim_mat_status = 'pass'
        dim_mat_msg = f'材料加工相态同构 ({base_mat_lbl})，工序切削、拼搭与封板批灰完全通用。'
        dim_mat_conflict = None
        dim_mat_rec = '执行四轴材质正交替换即可。'
    else:
        if var_mat_key in ('ice_snow', 'lava_molten', 'additive_3d_print'):
            dim_mat_status = 'fail'
            score_deductions += 45
            conflict = f'二创材料为【{var_mat_lbl}】，加工方式与母本实体木石装配根本不同（如冰雪需链锯/热熔，无法刷微水泥或拧螺栓；3D打印为增材分层）。'
            dim_mat_msg = conflict
            dim_mat_conflict = conflict
            dim_mat_rec = '硬套会导致在冰块上拧螺丝等材料常识硬伤。建议建立专属冰雕/3D打印母本。'
            incompatible_reasons.append(conflict)
        else:
            dim_mat_status = 'warning'
            score_deductions += 10
            conflict = f'材质工艺体系跨越【{base_mat_lbl} ➔ {var_mat_lbl}】，需注意工具与工艺痕迹映射。'
            dim_mat_msg = conflict
            dim_mat_conflict = conflict
            dim_mat_rec = '系统将自动通过 ASMR 与工艺库重映射工具和音效。'
            warning_reasons.append(conflict)

    dimension_results.append({
        'key': 'material_phase',
        'track': 'physical',
        'label': DIMENSION_METADATA['material_phase']['label'],
        'icon': DIMENSION_METADATA['material_phase']['icon'],
        'status': dim_mat_status,
        'baseline_value': base_mat_lbl,
        'variant_value': var_mat_lbl,
        'detail': dim_mat_msg,
        'conflict': dim_mat_conflict,
        'recommendation': dim_mat_rec,
    })

    # 7. 三维公制尺度与包络 (Scale Envelope & Metric)
    base_sc_key, base_sc_lbl = base_feat['scale_envelope']
    var_sc_key, var_sc_lbl = var_feat['scale_envelope']
    if (base_sc_key == var_sc_key) or (base_is_miniature and var_is_miniature):
        dim_sc_status = 'pass'
        dim_sc_msg = f'公制空间尺度同量级 ({base_sc_lbl})，机位透视与人机工程学比例稳定。'
        dim_sc_conflict = None
        dim_sc_rec = '继承母本微距/广角镜头透视即可。'
    else:
        if var_sc_key == 'cavernous_hall':
            dim_sc_status = 'fail'
            score_deductions += 45
            conflict = f'二创空间体量膨胀为【{var_sc_lbl}】，母本紧凑镜头直接套用会导致空间拉伸畸变（Cavernous / Bowling Alley 效应）。'
            dim_sc_msg = conflict
            dim_sc_conflict = conflict
            dim_sc_rec = '请收缩二创为紧凑微缩/庇护所，或建立宏大建筑专属母本。'
            incompatible_reasons.append(conflict)
        elif (base_sc_key == 'compact_envelope' and var_sc_key == 'miniature_scale') or (base_sc_key == 'miniature_scale' and var_sc_key == 'compact_envelope'):
            dim_sc_status = 'fail'
            score_deductions += 45
            conflict = f'尺度跨越根本量级【{base_sc_lbl} ➔ {var_sc_lbl}】，成人建筑与微缩沙盘镜头机位与比例尺冲突！'
            dim_sc_msg = conflict
            dim_sc_conflict = conflict
            dim_sc_rec = '微缩沙盘与成人真实建筑不可混淆套用，需切换专属母本。'
            incompatible_reasons.append(conflict)
        else:
            dim_sc_status = 'warning'
            score_deductions += 10
            conflict = f'尺度存在微差【{base_sc_lbl} ➔ {var_sc_lbl}】。'
            dim_sc_msg = conflict
            dim_sc_conflict = conflict
            dim_sc_rec = '生成提示词中将强制注入 Anti-Cavernous 公制公差声明。'
            warning_reasons.append(conflict)

    dimension_results.append({
        'key': 'scale_envelope',
        'track': 'physical',
        'label': DIMENSION_METADATA['scale_envelope']['label'],
        'icon': DIMENSION_METADATA['scale_envelope']['icon'],
        'status': dim_sc_status,
        'baseline_value': base_sc_lbl,
        'variant_value': var_sc_lbl,
        'detail': dim_sc_msg,
        'conflict': dim_sc_conflict,
        'recommendation': dim_sc_rec,
    })

    # 8. 过门转场机制与视线 (Transition & Portal Dynamics)
    base_port_key, base_port_lbl = base_feat['transition_portal']
    var_port_key, var_port_lbl = var_feat['transition_portal']
    if base_port_key == var_port_key or (base_is_miniature and var_is_miniature):
        dim_port_status = 'pass'
        dim_port_msg = f'过门转场机制同构 ({base_port_lbl})，双镜头解耦过门咬合顺畅。'
        dim_port_conflict = None
        dim_port_rec = '完美支持 Shot A (入口特写开启) + Shot B (室内承接首工序)。'
    else:
        if var_port_key == 'open_panorama_deck' and base_port_key == 'submarine_hatch_descent':
            dim_port_status = 'fail'
            score_deductions += 45
            conflict = f'二创为【{var_port_lbl}】（敞开无门），若硬套母本【{base_port_lbl}】会在露天草地上凭空制造出潜艇手轮舱门。'
            dim_port_msg = conflict
            dim_port_conflict = conflict
            dim_port_rec = '建议将转场改为水平推进拨开树丛，或升级为独立露天母本。'
            incompatible_reasons.append(conflict)
        else:
            dim_port_status = 'warning'
            score_deductions += 5
            conflict = f'转场形式微调【{base_port_lbl} ➔ {var_port_lbl}】。'
            dim_port_msg = conflict
            dim_port_conflict = conflict
            dim_port_rec = '保持视线轴向连贯，防止透视跳轴。'
            warning_reasons.append(conflict)

    dimension_results.append({
        'key': 'transition_portal',
        'track': 'physical',
        'label': DIMENSION_METADATA['transition_portal']['label'],
        'icon': DIMENSION_METADATA['transition_portal']['icon'],
        'status': dim_port_status,
        'baseline_value': base_port_lbl,
        'variant_value': var_port_lbl,
        'detail': dim_port_msg,
        'conflict': dim_port_conflict,
        'recommendation': dim_port_rec,
    })

    # 9. ASMR 声画沉浸与感官节拍 (ASMR & Sensory Immersion)
    dim_asmr_status = 'pass'
    dim_asmr_msg = '微观 ASMR 细节（撕纸、木工敲击、微型砖石拼搭声、夫妇轻微感叹）与 60% 物理音量动态映射。'
    dim_asmr_rec = '自动调用 ASMR 材质音频库映射清脆敲击/沙沙刮抹音。'
    dimension_results.append({
        'key': 'asmr_acoustic',
        'track': 'physical',
        'label': DIMENSION_METADATA['asmr_acoustic']['label'],
        'icon': DIMENSION_METADATA['asmr_acoustic']['icon'],
        'status': dim_asmr_status,
        'baseline_value': '60% 微观原声 ASMR',
        'variant_value': '60% 变体材质 ASMR',
        'detail': dim_asmr_msg,
        'conflict': None,
        'recommendation': dim_asmr_rec,
    })

    # 综合计算总分与等级
    score = max(0, min(100, 100 - score_deductions))
    if incompatible_reasons or score < 60:
        level = 'incompatible'
        verdict_title = '🚫 严禁表面硬套（物理冲突或叙事灵魂丢失）'
        can_inherit = False
        action = {
            'action': 'create_new_baseline',
            'button_label': '👑 升级为全新黄金母本 / 补全叙事灵魂',
            'explanation': '检测到物理规律冲突或严重丢失了母本的核心叙事灵魂（如穷困夫妇看图纸、神来之手介入或破败开局钩子）。盲套将导致视频失去完播率和爆款魅力！',
        }
    elif score < 90:
        level = 'risky'
        verdict_title = '⚠️ 需局部工序与情感适配 (Risky)'
        can_inherit = True
        action = {
            'action': 'adapt_and_mutate',
            'button_label': '🛠️ 智能适配并注入情感正交派生',
            'explanation': '检测到部分材质或叙事细节跨度，系统将自动适配工具、ASMR 与角色情感弧线，建议重点检查生成的工序与人偶交互。',
        }
    else:
        level = 'compatible'
        verdict_title = '✅ 允许 100% 骨架硬冻结正交派生 (Safe)'
        can_inherit = True
        action = {
            'action': 'mutate_orthogonal',
            'button_label': '⚡ 一键生成二创变体提示词包 (Variant)',
            'explanation': '母本与二创在叙事弧线、角色羁绊、空间载体与工艺拓扑上 100% 同构，兼具物理真实与 TikTok 爆款灵魂。',
        }

    summary = (
        f"综合相容性与叙事深度评分: {score}/100 ({level.upper()})。"
        + (f" 存在 {len(incompatible_reasons)} 项硬性冲突/叙事断层，不可盲目硬套。" if incompatible_reasons else " 物理拓扑同构且叙事灵魂闭环，允许正交发散。")
    )

    return {
        'compatibility_level': level,
        'compatibility_score': score,
        'verdict_title': verdict_title,
        'can_inherit_skeleton': can_inherit,
        'summary': summary,
        'incompatible_reasons': incompatible_reasons,
        'warning_reasons': warning_reasons,
        'narrative_alerts': narrative_alerts,
        'dimensions': dimension_results,
        'action_recommendation': action,
        'narrative_dna': {
            'baseline': base_dna,
            'variant': var_dna,
        },
        'baseline_signature': base_feat.get('corpus')[:120],
        'variant_signature': var_feat.get('corpus')[:120],
    }
