"""工序拓扑同构 —— 用它替代「拍数 N 恒定」这条冻结。

## 「拍数恒定」冻错了层

`docs/replica_baseline_and_orthogonal_mutation_spec.md` 3.1 的骨架冻结协议写着
「母本为 11 拍，所有派生变体必须严格为 11 拍，严禁拆拍、合拍」。这条冻的是一个
**整数**，而整数不是骨架 —— 骨架是「谁必须排在谁之前」。

冻整数的代价：不同材质的工序数天然不同。夯土要养护、钢构不用；玄武岩要切割、
帆布不用。强行凑够 N 拍的结果，就是 2026-09-03 那批变体里的
`erect timber portal` 底下站着三道钢拱 —— 拍还在，拍要干的活已经不成立了，于是
只好把名字留给母本、把内容交给模型现编。

## 这里冻什么

  1. **因果拓扑**：母本里角色 a 先于角色 b 出现，变体里两者都在时也必须 a 先于 b。
     顺序是骨架，拍数不是。
  2. **节奏曲线**：每一拍时长占全片的比例。完播率吃的是节奏，不是拍数——同样一条
     曲线，10 拍和 12 拍都能拟合。

于是拍数可以弹性（默认 ±0 保持既有行为，调用方按需放宽），而真正决定成片能不能
看的两样东西第一次被判死了。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .ontology import infer_role


def _beat_index(beat: Dict[str, Any], position: int) -> int:
    try:
        return int(beat.get('index') or beat.get('beat') or position)
    except (TypeError, ValueError):
        return position


def _duration(beat: Dict[str, Any]) -> float:
    for key in ('duration_sec', 'duration'):
        try:
            value = float(beat.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    try:
        return max(0.0, float(beat.get('end', beat.get('end_sec', 0.0)))
                   - float(beat.get('start', beat.get('start_sec', 0.0))))
    except (TypeError, ValueError):
        return 0.0


def build_topology(beats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一条阶梯的拓扑读数。

    ``role_first``：每个角色**第一次**出现的拍号 —— 因果顺序只看首次出现，一个角色
    分几拍做完不影响它和别的角色的先后关系。
    ``chain``：相邻去重后的角色链，即这条梯子的工序骨架。
    ``rhythm``：每拍时长占全片的比例。
    """
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    roles: List[str] = []
    role_first: Dict[str, int] = {}
    durations: List[float] = []

    for position, beat in enumerate(beats, start=1):
        role = infer_role(beat)
        idx = _beat_index(beat, position)
        roles.append(role)
        role_first.setdefault(role, idx)
        durations.append(_duration(beat))

    chain: List[str] = []
    for role in roles:
        if not chain or chain[-1] != role:
            chain.append(role)

    total = sum(durations)
    rhythm = [round(d / total, 4) for d in durations] if total > 0 else []

    return {
        'count': len(beats),
        'roles': roles,
        'chain': chain,
        'role_first': role_first,
        'durations': durations,
        'rhythm': rhythm,
    }


def _ordered_pairs(role_first: Dict[str, int]) -> List[Tuple[str, str]]:
    """母本里所有「a 严格早于 b」的角色对。"""
    items = sorted(role_first.items(), key=lambda kv: kv[1])
    pairs: List[Tuple[str, str]] = []
    for i, (role_a, at_a) in enumerate(items):
        for role_b, at_b in items[i + 1:]:
            if at_a < at_b:
                pairs.append((role_a, role_b))
    return pairs


def validate_isomorphism(source_beats: List[Dict[str, Any]],
                         variant_beats: List[Dict[str, Any]],
                         *,
                         beat_tolerance: int = 0,
                         rhythm_tolerance: float = 0.12) -> List[Dict[str, Any]]:
    """变体是不是母本的拓扑同构像。

    ``beat_tolerance``：允许的拍数偏差。默认 0 —— 既有母本与既有测试都按 1:1 走，
    放宽是调用方的显式决定，不是这里的默认。
    ``rhythm_tolerance``：单拍时长占比允许的漂移（绝对值）。

    返回 ``[{'rule','severity','beat','message'}, ...]``；``severity`` 分
    ``blocking``（拓扑倒置、拍数超容差）与 ``warning``（节奏漂移）。
    """
    src = build_topology(source_beats)
    var = build_topology(variant_beats)
    issues: List[Dict[str, Any]] = []

    delta = var['count'] - src['count']
    if abs(delta) > max(0, int(beat_tolerance)):
        issues.append({
            'rule': 'beat_count', 'severity': 'blocking', 'beat': 0,
            'message': (f'变体 {var["count"]} 拍，母本 {src["count"]} 拍，'
                        f'偏差 {delta:+d} 超出允许的 ±{beat_tolerance}。'),
        })

    # ── 因果倒置 ────────────────────────────────────────────────────────────
    # 只判**两者都在**的角色对：变体里缺席的角色不算倒置（不同材质工序数天然不同，
    # 这正是拍数弹性要留出的空间）。
    for role_a, role_b in _ordered_pairs(src['role_first']):
        at_a = var['role_first'].get(role_a)
        at_b = var['role_first'].get(role_b)
        if at_a is None or at_b is None:
            continue
        if at_a > at_b:
            issues.append({
                'rule': 'topology', 'severity': 'blocking', 'beat': at_b,
                'message': (f'母本里「{role_a}」先于「{role_b}」，变体把顺序倒了过来'
                            f'（{role_a} 在第 {at_a} 拍，{role_b} 在第 {at_b} 拍）。'),
            })

    # ── 节奏曲线 ────────────────────────────────────────────────────────────
    # 拍数一致时逐拍比；不一致时只比整体重心，因为逐拍已经没有对位关系了。
    if src['rhythm'] and var['rhythm']:
        if len(src['rhythm']) == len(var['rhythm']):
            for i, (a, b) in enumerate(zip(src['rhythm'], var['rhythm']), start=1):
                if abs(a - b) > rhythm_tolerance:
                    issues.append({
                        'rule': 'rhythm', 'severity': 'warning', 'beat': i,
                        'message': (f'第 {i} 拍时长占比 {b:.0%}，母本是 {a:.0%}，'
                                    f'漂移超过 {rhythm_tolerance:.0%}。'),
                    })
        else:
            src_centroid = sum((i + 1) * w for i, w in enumerate(src['rhythm']))
            var_centroid = sum((i + 1) * w for i, w in enumerate(var['rhythm']))
            src_norm = src_centroid / max(1, src['count'])
            var_norm = var_centroid / max(1, var['count'])
            if abs(src_norm - var_norm) > rhythm_tolerance:
                issues.append({
                    'rule': 'rhythm', 'severity': 'warning', 'beat': 0,
                    'message': (f'变体的节奏重心落在 {var_norm:.0%} 处，母本在 '
                                f'{src_norm:.0%} 处，整体配速偏离过大。'),
                })

    return issues


def format_issues(issues: List[Dict[str, Any]], limit: int = 10) -> str:
    if not issues:
        return ''
    label = {'beat_count': '拍数越界', 'topology': '因果倒置', 'rhythm': '节奏漂移'}
    lines = []
    for item in issues[:limit]:
        tag = label.get(item.get('rule') or '', item.get('rule') or '')
        lines.append(f'· [{tag}] {item.get("message")}')
    if len(issues) > limit:
        lines.append(f'· …另有 {len(issues) - limit} 条')
    return '\n'.join(lines)
