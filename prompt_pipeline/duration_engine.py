"""按秒数拆拍与双轴时序引擎 (Duration-Based Beat Splitter & Timing Engine).

本模块实现：
1. 双轴时序换算（观感屏幕时间 vs 真实物理动作时间，默认 2.0x 倍速）；
2. 基于拍重（beat_delta_weight）的动态秒数分配算法与闭环整定（sum(t_i) == T_target）；
3. 视听分轨配额（1.0x 舒适语速旁白字数上限 + 20% ASMR 环境声留白）；
4. 拆拍与时长门禁校验（单拍极值保护、相邻拍波动比 <= 1.8、拍重天花板拆拍预警）。
"""

import math
from typing import Any, Dict, List, Optional, Tuple, Union


# ── 全局常量与时序默认值 ──────────────────────────────────────────────────────────

DEFAULT_SPEED_MULTIPLIER = 2.0   # 默认参考片及合成加速倍率 (2x)
DEFAULT_TARGET_SCREEN_SEC = 30.0 # 默认成片目标屏幕时长 (秒)

# 单拍屏幕时间上下界（秒）
MIN_BEAT_SCREEN_SEC = 1.8        # 低于此值肉眼难以识别空间物理变化
MAX_BEAT_SCREEN_SEC = 6.5        # 普通施工拍上限，超过易产生拖沓感
MAX_REWARD_SCREEN_SEC = 8.0      # 终极完工揭示/欣赏镜头上限

# 相邻拍屏幕时间波动比率上限（防止节奏忽快忽慢）
MAX_NEIGHBOR_RATIO = 1.8

# 旁白配音语速默认值（字数/秒）及 ASMR 呼吸留白比例
DEFAULT_ZH_WORDS_PER_SEC = 4.2   # 中文正常舒适语速约为 4.0~4.5 字/秒
DEFAULT_EN_WORDS_PER_SEC = 3.0   # 英文正常语速约为 2.5~3.2 词/秒
DEFAULT_SILENCE_RATIO = 0.20     # 预留 20% 纯环境 ASMR 声空间


def convert_time_axes(
    screen_sec: Optional[float] = None,
    action_sec: Optional[float] = None,
    speed: float = DEFAULT_SPEED_MULTIPLIER,
) -> Tuple[float, float]:
    """在「观感屏幕时间」与「真实物理动作时间」之间进行精确双轴换算。
    
    Args:
        screen_sec: 观感屏幕时长（秒）
        action_sec: 真实物理动作/原生生成时长（秒）
        speed: 变速倍率（默认 2.0x）
        
    Returns:
        (screen_sec, action_sec) 元组
    """
    speed = float(speed) if speed and speed > 0 else DEFAULT_SPEED_MULTIPLIER
    if screen_sec is not None:
        s_sec = max(0.0, float(screen_sec))
        a_sec = s_sec * speed
        return round(s_sec, 2), round(a_sec, 2)
    elif action_sec is not None:
        a_sec = max(0.0, float(action_sec))
        s_sec = a_sec / speed
        return round(s_sec, 2), round(a_sec, 2)
    return 0.0, 0.0


def calculate_beat_word_quota(
    screen_sec: float,
    lang: str = 'zh',
    wps: Optional[float] = None,
    silence_ratio: float = DEFAULT_SILENCE_RATIO,
) -> Dict[str, Union[int, float]]:
    """计算单拍屏幕时长对应的旁白配音字数配额及 ASMR 留白时间。
    
    核心原则：画面 2x 加速，但旁白必须保持 1.0x 正常舒适语速。
    
    Args:
        screen_sec: 该拍的成片屏幕时长（秒）
        lang: 'zh' 或 'en'
        wps: 自定义语速（字/秒 或 词/秒）
        silence_ratio: 预留给纯 ASMR 原声的留白比例 (0.0 ~ 0.5)
        
    Returns:
        {
            'max_words': int,       # 旁白建议最大字数/词数
            'voiceover_sec': float, # 旁白实际占用时长（秒）
            'silence_sec': float,   # ASMR 纯原声留白时长（秒）
        }
    """
    sec = max(0.0, float(screen_sec))
    if wps is None or wps <= 0:
        wps = DEFAULT_ZH_WORDS_PER_SEC if lang.lower().startswith('zh') else DEFAULT_EN_WORDS_PER_SEC
    
    silence_ratio = min(0.5, max(0.0, float(silence_ratio)))
    vo_time = sec * (1.0 - silence_ratio)
    silence_time = sec * silence_ratio
    max_words = max(0, int(math.floor(vo_time * wps)))
    
    return {
        'max_words': max_words,
        'voiceover_sec': round(vo_time, 2),
        'silence_sec': round(silence_time, 2),
    }


def _get_beat_weight(beat: Dict[str, Any]) -> float:
    """提取或计算单个节拍的拍重（Delta Weight）。"""
    if not isinstance(beat, dict):
        return 1.0
    
    stage = str(beat.get('stage') or '').lower()
    op = str(beat.get('operation') or '').lower()
    
    # 运镜/转折拍（过门、揭示）
    if stage in ('threshold', 'reward', 'reveal') or op in ('threshold', 'reward', 'reveal') or beat.get('bridge_stage') or beat.get('hard_cut'):
        # 揭示镜头赋予较高权重以确保成片有充足展示时间 (5.0s~8.0s)
        if stage in ('reward', 'reveal') or op in ('reward', 'reveal'):
            return 2.5
        # 过门拍作为转折呼吸点
        return 1.3
    
    # 若已有 beat_delta_weight 计算好的 weight / delta_weight 字段
    for k in ('weight', 'delta_weight'):
        if k in beat and beat[k] is not None:
            try:
                return float(beat[k])
            except (ValueError, TypeError):
                pass
    
    # 尝试从 prompt_pipeline 调用 beat_delta_weight
    try:
        import prompt_pipeline as pp
        if hasattr(pp, 'beat_delta_weight'):
            w = pp.beat_delta_weight(beat)
            if w is not None:
                return float(w)
    except Exception:
        pass
        
    # 基于工序条数与格位的兜底启发式估算
    ops = len(beat.get('package_operations') or []) or 1
    scope_w = {'large': 1.6, 'small': 0.8, 'default': 1.0}.get(beat.get('stage_scope'), 1.0)
    grid_span = max(0, len(set(beat.get('changed_grid_cells') or [])) - 1)
    return round(scope_w + (ops - 1) * 0.5 + grid_span * 0.3, 2)


def allocate_beat_durations(
    beats: List[Dict[str, Any]],
    target_total_screen_sec: float = DEFAULT_TARGET_SCREEN_SEC,
    speed_multiplier: float = DEFAULT_SPEED_MULTIPLIER,
    rhythm: Optional[Dict[str, Any]] = None,
    gamma: float = 0.70,
    lang: str = 'zh',
) -> List[Dict[str, Any]]:
    """核心算法：基于拍重与 2x 倍速模型，为所有节拍动态分配最佳屏幕秒数与物理动作秒数。
    
    算法流程：
    1. 计算每拍拍重 w_i 与初始非线性时长基准 t_i ~ w_i^gamma；
    2. 对 Reward/Reveal 拍赋予足够的展示权重；
    3. 施加单拍上下界约束 [MIN_BEAT_SCREEN_SEC, MAX_BEAT_SCREEN_SEC / MAX_REWARD_SCREEN_SEC]；
    4. 执行两轮平滑与按比例整定，确保 sum(t_screen_i) == target_total_screen_sec 绝对闭环；
    5. 根据 speed_multiplier 计算原生物理生成时长 t_action_i = t_screen_i * speed_multiplier；
    6. 计算旁白字数配额及 FFmpeg 变速参数（setpts / atempo）。
    
    Args:
        beats: 节拍列表（每个字典可包含 id, stage, operation, package_operations 等）
        target_total_screen_sec: 目标成片总时长（秒，默认 30.0s）
        speed_multiplier: 视频生成与合成加速倍率（默认 2.0x）
        rhythm: 骨架节奏配置（可选）
        gamma: 次线性弹性系数（默认 0.70）
        lang: 语言代码 ('zh' | 'en')
        
    Returns:
        更新后的节拍列表副本，每个节拍注入：
        - screen_duration_sec: 该拍成片屏幕时长（秒）
        - action_duration_sec: 该拍 I2V 原生生成物理时长（秒）
        - speed_factor: 该拍合成时的加速倍率 (等于 speed_multiplier)
        - setpts_expr: 该拍对应的 FFmpeg setpts 表达式
        - atempo_chain: 该拍对应的 FFmpeg atempo 音频滤镜串
        - voiceover_quota: 旁白字数与留白配额字典
    """
    if not beats:
        return []
    
    total_target = max(len(beats) * MIN_BEAT_SCREEN_SEC, float(target_total_screen_sec))
    speed = float(speed_multiplier) if speed_multiplier and speed_multiplier > 0 else DEFAULT_SPEED_MULTIPLIER
    
    n_beats = len(beats)
    weights = [_get_beat_weight(b) for b in beats]
    avg_weight = sum(weights) / float(n_beats) if n_beats > 0 else 1.0
    if avg_weight <= 0:
        avg_weight = 1.0
        
    # 1. 初始非线性时间分配
    raw_times = []
    for i, b in enumerate(beats):
        w = weights[i]
        is_reward = (b.get('operation') in ('reward', 'reveal') or 
                     b.get('stage') in ('reward', 'reveal') or 
                     i == n_beats - 1)
        
        # 次线性弹性映射
        ratio = (w / avg_weight) ** gamma
        base_t = (total_target / float(n_beats)) * ratio
        
        # 单拍极值上下界初步截断
        max_t = MAX_REWARD_SCREEN_SEC if is_reward else MAX_BEAT_SCREEN_SEC
        clamped_t = min(max_t, max(MIN_BEAT_SCREEN_SEC, base_t))
        raw_times.append(clamped_t)
        
    # 2. 闭环整定与相邻平滑（保证 sum(raw_times) == total_target 且相邻不过陡）
    current_sum = sum(raw_times)
    if current_sum > 0:
        scale = total_target / current_sum
        allocated_times = []
        for i, t in enumerate(raw_times):
            b = beats[i]
            is_reward = (b.get('operation') in ('reward', 'reveal') or 
                         b.get('stage') in ('reward', 'reveal') or 
                         i == n_beats - 1)
            max_t = MAX_REWARD_SCREEN_SEC if is_reward else MAX_BEAT_SCREEN_SEC
            t_scaled = round(t * scale, 1)
            t_scaled = min(max_t, max(MIN_BEAT_SCREEN_SEC, t_scaled))
            allocated_times.append(t_scaled)

        # 相邻施工拍平滑（保证相邻施工拍比值不超过 1.75）
        for _ in range(2):
            for i in range(1, n_beats):
                b_curr = beats[i]
                b_prev = beats[i - 1]
                is_trans = (b_curr.get('stage') in ('threshold', 'reward', 'reveal') or
                            b_prev.get('stage') in ('threshold', 'reward', 'reveal') or
                            b_curr.get('operation') in ('threshold', 'reward', 'reveal') or
                            b_prev.get('operation') in ('threshold', 'reward', 'reveal'))
                if not is_trans:
                    t1, t2 = allocated_times[i - 1], allocated_times[i]
                    if t2 > t1 * 1.75:
                        allocated_times[i] = round(t1 * 1.75, 1)
                    elif t1 > t2 * 1.75:
                        allocated_times[i - 1] = round(t2 * 1.75, 1)

        # 微调尾差，保证完全对齐总秒数
        diff = round(total_target - sum(allocated_times), 1)
        if abs(diff) >= 0.05:
            # 优先将微小尾差补在最后一拍（通常为揭示拍）或中间施工拍
            adjust_idx = n_beats - 1
            allocated_times[adjust_idx] = round(max(MIN_BEAT_SCREEN_SEC, allocated_times[adjust_idx] + diff), 1)
    else:
        allocated_times = [round(total_target / float(n_beats), 1)] * n_beats
        
    # 3. 构造注入数据
    result_beats = []
    for i, b in enumerate(beats):
        beat_copy = dict(b)
        s_dur = allocated_times[i]
        a_dur = round(s_dur * speed, 1)
        
        # setpts 系数: 播放速度为 speed 时，PTS 缩放为 1.0 / speed
        setpts_val = round(1.0 / speed, 4)
        
        # 音频 atempo 链生成
        atempo_chain = _build_atempo_filter(speed)
        
        # 旁白字数配额
        quota = calculate_beat_word_quota(s_dur, lang=lang)
        
        beat_copy['screen_duration_sec'] = s_dur
        beat_copy['action_duration_sec'] = a_dur
        beat_copy['speed_factor'] = speed
        beat_copy['setpts_factor'] = setpts_val
        beat_copy['setpts_expr'] = f'setpts={setpts_val:g}*PTS'
        beat_copy['atempo_chain'] = atempo_chain
        beat_copy['voiceover_quota'] = quota
        beat_copy['delta_weight'] = round(weights[i], 2)
        
        result_beats.append(beat_copy)
        
    return result_beats


def _build_atempo_filter(tempo: float) -> str:
    """生成合法的 FFmpeg atempo 滤镜链（单次只接受 0.5~2.0）。"""
    parts = []
    remaining = float(tempo)
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    parts.append(remaining)
    return ','.join(f'atempo={p:.6g}' for p in parts)


def validate_beat_duration_budget(
    beats: List[Dict[str, Any]],
    target_total_screen_sec: Optional[float] = None,
    speed_multiplier: float = DEFAULT_SPEED_MULTIPLIER,
    neighbor_ratio_max: float = MAX_NEIGHBOR_RATIO,
) -> List[Dict[str, Any]]:
    """校验拆拍与时长预算，返回结构化告警或违规列表。
    
    检查项：
    1. 单拍屏幕时长是否低于 1.8s（过细）或高于上限（拖沓）；
    2. 相邻两个普通施工拍的屏幕时长突变比是否超过 neighbor_ratio_max (默认 1.8)；
    3. 拍重是否超过硬天花板（需要拆拍）；
    4. 总时长是否与 target_total_screen_sec 存在严重偏差。
    """
    violations = []
    if not beats:
        return violations
        
    durations = [b.get('screen_duration_sec') for b in beats]
    total_screen = sum(d for d in durations if d is not None)
    
    # 1. 检查总时长偏差
    if target_total_screen_sec is not None:
        target = float(target_total_screen_sec)
        if abs(total_screen - target) > 0.5:
            violations.append({
                'type': 'total_duration_mismatch',
                'severity': 'warning',
                'message': f'所有节拍屏幕时长之和 ({total_screen:.1f}s) 与目标总时长 ({target:.1f}s) 存在偏差。',
            })
            
    # 2. 逐拍与相邻拍检查
    prev_dur = None
    prev_id = None
    prev_is_trans = False
    for i, b in enumerate(beats):
        bid = b.get('id', f'Beat_{i+1}')
        s_dur = b.get('screen_duration_sec')
        is_reward = (b.get('operation') in ('reward', 'reveal') or 
                     b.get('stage') in ('reward', 'reveal'))
        
        if s_dur is not None:
            if s_dur < MIN_BEAT_SCREEN_SEC:
                violations.append({
                    'type': 'beat_too_short',
                    'severity': 'warning',
                    'beat_id': bid,
                    'message': f'{bid} 屏幕时长仅 {s_dur:.1f}s（低于建议下限 {MIN_BEAT_SCREEN_SEC}s），建议与相邻同工序合并。',
                })
            max_limit = MAX_REWARD_SCREEN_SEC if is_reward else MAX_BEAT_SCREEN_SEC
            if s_dur > max_limit:
                violations.append({
                    'type': 'beat_too_long',
                    'severity': 'warning',
                    'beat_id': bid,
                    'message': f'{bid} 屏幕时长达 {s_dur:.1f}s（超过上限 {max_limit}s），建议拆分为具体施工里程碑。',
                })
                
            # 相邻拍突变比检查（跳过运镜/过门拍与收尾拍）
            is_trans_curr = (b.get('stage') in ('threshold', 'bridge', 'reward', 'reveal') or
                             b.get('operation') in ('threshold', 'bridge', 'reward', 'reveal'))
            if prev_dur is not None and not is_trans_curr and not prev_is_trans:
                ratio = max(s_dur, prev_dur) / max(0.1, min(s_dur, prev_dur))
                if ratio > neighbor_ratio_max:
                    violations.append({
                        'type': 'duration_jump_too_steep',
                        'severity': 'warning',
                        'beat_id': bid,
                        'message': f'{bid} 与上一拍 {prev_id} 屏幕时长突变比达 {ratio:.2f}x（超出门禁上限 {neighbor_ratio_max}x）。',
                    })
            prev_dur = s_dur
            prev_id = bid
            prev_is_trans = is_trans_curr
            
        # 拍重硬天花板检查 (w > 3.4 必须拆拍)
        w = b.get('delta_weight')
        if w is None:
            w = _get_beat_weight(b)
        if w is not None and w > 3.4 and not is_reward:
            violations.append({
                'type': 'beat_weight_exceeds_ceiling',
                'severity': 'error',
                'beat_id': bid,
                'message': f'{bid} 物理拍重达 {w:.2f}（超出门禁上限 3.4），塞入了过多交叉工序，必须强制拆分为 2 拍。',
            })
            
    return violations
