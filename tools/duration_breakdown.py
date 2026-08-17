#!/usr/bin/env python3
"""按秒数拆拍与 2 倍速时序计算与规划工具 (Duration Breakdown CLI Tool).

用法示例:
    # 1. 快速规划 30s、8 拍、2 倍速成片时序
    python tools/duration_breakdown.py --total-seconds 30 --beats 8 --speed 2.0

    # 2. 规划 15s 爆款快剪 (5 拍)
    python tools/duration_breakdown.py --total-seconds 15 --beats 5 --speed 2.0

    # 3. 规划 60s 深度双幕大片 (14 拍)
    python tools/duration_breakdown.py --total-seconds 60 --beats 14 --speed 2.0

    # 4. 从 JSON 导入现有 beats 文件并重新分配秒数
    python tools/duration_breakdown.py --input-json path/to/timelapse_beats.json --total-seconds 30
"""

import argparse
import json
import os
import sys
from pathlib import Path

# 保留原始控制台输出流（防止被服务层日志重定向吞没）
_ORIG_STDOUT = sys.__stdout__ or sys.stdout

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from prompt_pipeline.duration_engine import (
    allocate_beat_durations,
    validate_beat_duration_budget,
    calculate_beat_word_quota,
    convert_time_axes,
    DEFAULT_SPEED_MULTIPLIER,
    DEFAULT_TARGET_SCREEN_SEC,
)


def generate_sample_beats(n_beats: int) -> list:
    """根据拍数生成标准的骨架节拍样例。"""
    stages_order = [
        ('B01', 'demolition', '清理与破土 (Hook/基准确立)', 'large', 1),
        ('B02', 'structural', '地基垫层与防潮铺设', 'large', 2),
        ('B03', 'rough_in', '梁架龙骨与管线预埋', 'large', 2),
        ('B04', 'threshold', '过门穿透/进入室内', 'default', 1),
        ('B05', 'enclosure', '保温填充与内墙封板', 'large', 2),
        ('B06', 'surface', '实木地板铺设与地台搭建', 'large', 2),
        ('B07', 'fixtures', '隐藏灯带与置物架安装', 'small', 1),
        ('B08', 'reveal', '终极完工赏析 (两相沉浸展示)', 'default', 1),
    ]
    if n_beats == 5:
        # 15s 5拍快剪骨架
        stages_order = [
            ('B01', 'demolition', '破损现状定格与破土 (Hook)', 'large', 1),
            ('B02', 'structural', '主梁架设与防水铺设', 'large', 2),
            ('B03', 'enclosure', '实木板材封闭与固定', 'large', 2),
            ('B04', 'fixtures', '氛围灯带点亮与软装点缀', 'small', 1),
            ('B05', 'reveal', '终极完工沉浸全景展示', 'default', 1),
        ]
    elif n_beats > 8:
        # 扩展更多施工细节拍
        extra_count = n_beats - 8
        base_beats = list(stages_order[:-1])
        for k in range(extra_count):
            base_beats.append((f'B{len(base_beats)+1:02d}', 'surface', f'精细化饰面与细节施工 {k+1}', 'large', 2))
        base_beats.append((f'B{n_beats:02d}', 'reveal', '终极完工赏析 (两相沉浸展示)', 'default', 1))
        stages_order = base_beats

    beats = []
    for bid, stage, op, scope, ops_count in stages_order[:n_beats]:
        beats.append({
            'id': bid,
            'stage': stage,
            'operation': op,
            'stage_scope': scope,
            'package_operations': [f'工序 {j+1}' for j in range(ops_count)],
            'changed_grid_cells': ['C1', 'C2'] if ops_count > 1 else ['C1'],
        })
    return beats


def print_breakdown_table(allocated_beats: list, total_screen_sec: float, speed: float, lang: str = 'zh'):
    """在终端打印漂亮的格式化时序矩阵。"""
    print("\n" + "=" * 96)
    print(f" 🎬 视频按秒数拆拍与双轴时序规划表 (Total Screen: {total_screen_sec:.1f}s | Speed: {speed:.1f}x)")
    print("=" * 96)
    
    header = (
        f"{'拍序':<6} | {'阶段/工序':<24} | {'拍重':<6} | "
        f"{'成片时长':<10} | {'I2V物理时长':<12} | {'旁白配额':<10} | {'FFmpeg 滤镜'}"
    )
    print(header)
    print("-" * 96)

    total_screen = 0.0
    total_action = 0.0
    total_words = 0

    for b in allocated_beats:
        bid = b.get('id', '')
        op = (b.get('operation') or b.get('stage') or '')[:22]
        w = b.get('delta_weight', 1.0)
        s_dur = b.get('screen_duration_sec', 0.0)
        a_dur = b.get('action_duration_sec', 0.0)
        quota = b.get('voiceover_quota') or {}
        words = quota.get('max_words', 0)
        setpts = b.get('setpts_expr', f'setpts={1.0/speed:.3g}*PTS')

        total_screen += s_dur
        total_action += a_dur
        total_words += words

        print(
            f"{bid:<6} | {op:<24} | {w:<6.2f} | "
            f"{s_dur:<8.1f}s | {a_dur:<10.1f}s | {words:<3} 字 (留白 {quota.get('silence_sec',0):.1f}s) | {setpts}"
        )

    print("-" * 96)
    unit_label = "字" if lang == 'zh' else "words"
    print(
        f"📊 合计统计: 成片屏幕总长 = {total_screen:.1f}s | "
        f"I2V原生素材总长 = {total_action:.1f}s | "
        f"旁白建议总字数 = ≤ {total_words} {unit_label}"
    )
    atempo = allocated_beats[0].get('atempo_chain', f'atempo={speed}') if allocated_beats else f'atempo={speed}'
    print(f"🔊 音频 ASMR 滤镜: [0:a]{atempo}[a] (ASMR 音量推荐 60% 混音, 旁白人声 100% 混音)")
    print("=" * 96 + "\n")


def main():
    sys.stdout = _ORIG_STDOUT
    sys.stderr = sys.__stderr__ or sys.stderr
    parser = argparse.ArgumentParser(description="按秒数拆拍与 2 倍速时序计算器")
    parser.add_argument("--total-seconds", "-t", type=float, default=DEFAULT_TARGET_SCREEN_SEC,
                        help="目标成片屏幕总时长（秒，默认 30.0）")
    parser.add_argument("--beats", "-n", type=int, default=8,
                        help="拆解节拍数（默认 8 拍）")
    parser.add_argument("--speed", "-s", type=float, default=DEFAULT_SPEED_MULTIPLIER,
                        help="加速倍率（默认 2.0x）")
    parser.add_argument("--input-json", "-i", type=str, default=None,
                        help="输入的 beats JSON 文件路径（可选）")
    parser.add_argument("--output-json", "-o", type=str, default=None,
                        help="输出更新后的 beats JSON 路径（可选）")
    parser.add_argument("--lang", type=str, default="zh", choices=["zh", "en"],
                        help="旁白语言 ('zh' | 'en')")
    
    args = parser.parse_args()

    if args.input_json and os.path.exists(args.input_json):
        with open(args.input_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
            beats = data.get('beats', data) if isinstance(data, dict) else data
    else:
        beats = generate_sample_beats(args.beats)

    allocated = allocate_beat_durations(
        beats,
        target_total_screen_sec=args.total_seconds,
        speed_multiplier=args.speed,
        lang=args.lang,
    )

    violations = validate_beat_duration_budget(
        allocated,
        target_total_screen_sec=args.total_seconds,
        speed_multiplier=args.speed,
    )

    print_breakdown_table(allocated, args.total_seconds, args.speed, lang=args.lang)

    if violations:
        print("⚠️ 预算与门禁校验发现以下提示/警告:")
        for v in violations:
            icon = "❌" if v.get('severity') == 'error' else "⚠️"
            print(f"  {icon} [{v.get('type')}] {v.get('message')}")
        print()

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({'beats': allocated}, f, ensure_ascii=False, indent=2)
        print(f"✅ 规划结果已成功写入: {out_path}")


if __name__ == "__main__":
    main()
