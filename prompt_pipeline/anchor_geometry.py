"""锚点几何 —— 让画幅占比随机位重算，而不是被复制。

## 问题

`packet` 给每个地标存一个 `z_depth_scale`（画幅高度占比），`_canonical_anchor_clause`
把它渲染成 "rising to about a third of the frame height"。这个数字**在整条链上原样
复制**，而机位一直在变。2026-09-03 那批海蚀洞变体里，同一个「fractured cavern rock
headland」在下面这些机位下都被声称占画幅高度的三分之一：

    IMAGE 2   20mm  机位高 2.6m  俯 30°
    IMAGE 3   22mm  机位高 2.5m  俯 35°
    IMAGE 4   20mm  机位高 1.6m  平视
    IMAGE 7   35mm  机位高 1.6m  平视      ← 35mm 比 20mm 放大 1.75 倍

一个物体不可能在 20mm 和 35mm 下占同样的画幅比例。人物占比同理：
"~35 percent of vertical frame height" 被逐字抄进 19 段视频，不管镜头是 20mm 俯拍
还是 22mm 侧身平视。

## 解法

存下来的 (占比, 机位) 这一对**隐含了物体的真实尺寸**。把那个尺寸重新投影到新机位上，
就得到新的占比：

    画幅在物距 d 处覆盖的实际高度  H = 2·d·tan(FOV_v/2) = d·sensor_h / f
    占比 r = h_obj / H
    ⟹  h_obj = r₀ · d₀ · sensor_h / f₀
    ⟹  r₁ = r₀ · (d₀ / f₀) · (f₁ / d₁) = r₀ · (d₀·f₁) / (d₁·f₀)

物距通常没有记录。锁定机位的约定本身就意味着站位基本不动，所以 d₁ ≈ d₀，一阶项就是
焦段比 **r₁ = r₀ · f₁ / f₀** —— 而焦段恰恰是上面那张表里唯一明确写着、且明确被忽略了
的量。物距已知时按完整公式算。

俯仰角对占比也有影响（俯拍会压缩地面物体的投影高度），作为二阶修正给出，默认关闭：
它需要知道物体是立面还是地面，而 packet 里没有这个信息，硬猜的收益低于误差。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional

# 全画幅竖幅（9:16）的画幅高度。这条线交付的是竖版短视频，"frame height" 说的是长边。
DEFAULT_SENSOR_H_MM = 36.0

# 光是 `\d+mm` 不够：建造散文里满地都是毫米尺寸（"a 70mm deep layer of basalt
# aggregate"、"12mm birch plywood"），全都会被当成焦段读走。必须要有镜头语境。
_FOCAL_RE = re.compile(r'(\d{1,3}(?:\.\d)?)\s*mm', re.I)
_LENS_CONTEXT_RE = re.compile(r'\b(?:lens|focal|equivalent|prime|zoom)\b', re.I)
_LENS_PREFIX_RE = re.compile(r'\b(?:ultra-?wide|wide|normal|standard|tele(?:photo)?)\s*$', re.I)
_LENS_CONTEXT_WINDOW = 30
_HEIGHT_RE = re.compile(r'(?:camera\s+height|height\s+of|at)\s*(\d(?:\.\d+)?)\s*m\b', re.I)
_PITCH_NUM_RE = re.compile(r'(?:looking\s+down|tilted\s+down|pitch(?:ed)?\s+down)\s*(\d{1,2})\s*(?:°|degrees?)', re.I)
# 复合词必须排在它的前缀**之前**：'thirty' 排在 'thirty-five' 前面的话，正则交替会先
# 匹配到 'thirty'，'looking down thirty-five degrees' 被读成 30°。
_PITCH_WORD_RE = re.compile(
    r'looking\s+down\s+(twenty-five|thirty-five|forty-five|fifteen|twenty|thirty|forty|fifty|ten)\s*(?:degrees?)?',
    re.I)
_WORD_DEG = {'ten': 10, 'fifteen': 15, 'twenty': 20, 'twenty-five': 25, 'thirty': 30,
             'thirty-five': 35, 'forty': 40, 'forty-five': 45, 'fifty': 50}


def parse_camera(text: Any) -> Dict[str, Optional[float]]:
    """从一句机位散文里读出焦段 / 机位高 / 俯角。

    读不出的项返回 None —— 缺项让调用方决定怎么办，不在这里编一个默认值：编出来的
    焦段会让重算比不重算更错。
    """
    blob = str(text or '')
    focal = None
    for match in _FOCAL_RE.finditer(blob):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if not (8.0 <= value <= 200.0):
            continue
        after = blob[match.end():match.end() + _LENS_CONTEXT_WINDOW]
        before = blob[max(0, match.start() - _LENS_CONTEXT_WINDOW):match.start()]
        if _LENS_CONTEXT_RE.search(after) or _LENS_PREFIX_RE.search(before):
            focal = value
            break

    height = None
    match = _HEIGHT_RE.search(blob)
    if match:
        try:
            value = float(match.group(1))
            height = value if 0.1 <= value <= 12.0 else None
        except ValueError:
            height = None

    pitch = None
    match = _PITCH_NUM_RE.search(blob)
    if match:
        pitch = float(match.group(1))
    else:
        match = _PITCH_WORD_RE.search(blob)
        if match:
            pitch = float(_WORD_DEG.get(match.group(1).lower(), 0) or 0) or None

    return {'focal_mm': focal, 'height_m': height, 'pitch_deg': pitch}


def frame_height_at(distance_m: float, focal_mm: float,
                    sensor_h_mm: float = DEFAULT_SENSOR_H_MM) -> float:
    """物距 ``distance_m`` 处，画幅覆盖的真实高度（米）。"""
    if focal_mm <= 0:
        return 0.0
    return float(distance_m) * float(sensor_h_mm) / float(focal_mm)


def screen_height_ratio(subject_h_m: float, distance_m: float, focal_mm: float,
                        sensor_h_mm: float = DEFAULT_SENSOR_H_MM) -> Optional[float]:
    """一个 ``subject_h_m`` 米高的东西，在这个机位下占画幅高度的比例（0~1）。"""
    span = frame_height_at(distance_m, focal_mm, sensor_h_mm)
    if span <= 0:
        return None
    return max(0.0, min(1.0, float(subject_h_m) / span))


def reproject_scale(scale_percent: Any,
                    ref_camera: Dict[str, Optional[float]],
                    cur_camera: Dict[str, Optional[float]],
                    *,
                    ref_distance_m: Optional[float] = None,
                    cur_distance_m: Optional[float] = None) -> Optional[int]:
    """把参考机位下测得的占比，重投影到当前机位。

    两个机位的焦段任一读不出时返回 ``None`` —— 调用方据此保留原值。宁可不改，
    也不拿一个猜出来的焦段去改一个本来就对的数字。
    """
    base = _percent(scale_percent)
    if base is None:
        return None
    ref_f = (ref_camera or {}).get('focal_mm')
    cur_f = (cur_camera or {}).get('focal_mm')
    if not ref_f or not cur_f:
        return None

    ratio = float(cur_f) / float(ref_f)
    if ref_distance_m and cur_distance_m and cur_distance_m > 0:
        ratio *= float(ref_distance_m) / float(cur_distance_m)

    projected = base * ratio
    # 夹在 2~100：超过画幅的锚点在提示词里没有意义，2 以下渲染不出任何措辞。
    return int(round(max(2.0, min(100.0, projected))))


def cast_screen_percent(subject_h_m: float, distance_m: float, focal_mm: float,
                        sensor_h_mm: float = DEFAULT_SENSOR_H_MM) -> Optional[int]:
    """人物在这个机位下的画幅占比（整数百分比）。"""
    ratio = screen_height_ratio(subject_h_m, distance_m, focal_mm, sensor_h_mm)
    if ratio is None:
        return None
    return int(round(max(2.0, min(100.0, ratio * 100.0))))


def working_distance(camera: Dict[str, Optional[float]], default_m: float = 3.0) -> float:
    """工人干活时与镜头的典型距离。

    机位高度是唯一有记录的空间量，工作距离与它强相关（1.3m 侧身近景 vs 2.6m 俯拍
    全景），所以按它推。这是个估计，不是测量 —— 它只需要好到能把 20mm 和 35mm
    分开，而那一步它绰绰有余。
    """
    height = (camera or {}).get('height_m')
    if not height:
        return default_m
    return max(1.2, float(height) * 1.6)


def cast_scale_hint(camera_text: Any, subject_h_m: float = 1.78) -> str:
    """给合成器的一句人物比例指令，按这一拍的真实机位算出来。

    读不出焦段时返回空串，调用方退回原来的写法 —— 一句算不出来的比例指令，
    不如没有。
    """
    camera = parse_camera(camera_text)
    focal = camera.get('focal_mm')
    if not focal:
        return ''
    percent = cast_screen_percent(subject_h_m, working_distance(camera), focal)
    if not percent:
        return ''
    return (f'one lone worker, {subject_h_m:.2f}m tall, occupying about {percent} percent '
            f'of frame height at this beat\'s {int(focal)}mm framing')


def _percent(value: Any) -> Optional[float]:
    """'65%' / '65' / 65 → 65.0；读不出返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r'(\d{1,3}(?:\.\d+)?)', str(value))
        if not match:
            return None
        number = float(match.group(1))
    return number if 0 < number <= 100 else None
