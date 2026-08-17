#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_cast_drift.py
===================
把人物一致性 QC 表第 1～8 项（帽/外套/内搭/裤/靴/手套/胡须/发色）从人眼变成退出码。

那八项是"观众用来认人"的剪影级特征，协议判它们**零容忍** —— 而它们在像素上就是
固定的几块颜色。段数一多，人眼逐段核对必然放水，机器不会。

这个 CLI 住在 tools/ 而不是 skill 包的 scripts/：它要 numpy 和 Pillow，而 skill 脚本
是 stdlib-only、要能在裸解释器上跑（见 requirements-compose.txt）。文本层的
check_character_lock.py 留在 skill 包里，像素层住在服务端这边 —— 这条分界是依赖决定的，
不是随手放的。

用法：

    # 1) 先标定：从一张已认可的帧上取每个部位的参考色（相对坐标 0..1）
    python tools/check_cast_drift.py calibrate --cast jake-miller \\
        --frame outputs/<项目>/frames/img_003.webp --slot jacket --at 0.48,0.42

    # 2) 再判片：对整个项目逐段抽帧比对
    python tools/check_cast_drift.py check --cast jake-miller --project outputs/<项目>

    # 也可以直接给视频文件
    python tools/check_cast_drift.py check --cast jake-miller --video a.mp4 --video b.mp4

退出码：
    0  有可判部位，且它们在所有段里都稳定 —— 这才是"通过"
    1  检出剪影级漂移
    2  判不了：角色没标定，或所有部位都不可用（参考色标错 / 该部位没入过镜 /
       参考色掺了大块场景）。**这不是通过** —— 单独一个码，就是为了让它没法被
       当成 0 混过交付门
    4  输入有问题（角色不存在、项目目录读不了、参考色格式错）
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

# 先抓住真正的 stdout/stderr，再 import 任何本项目模块。
# server_common 在 **import 期**（不是调用期）就把 sys.stdout tee 进 server.log，且在
# 非 TTY 下不再往控制台写一份（见 server_common._console_stream）。本 CLI 会间接拉起它
# （cast_drift._ffmpeg_binary 里的延迟导入），于是不做这一步的话，`python tools/... > x.txt`
# 拿到的是空文件，报告安静地躺在 server.log 里。对服务进程那是对的行为，对 CLI 不是。
_REAL_STDOUT, _REAL_STDERR = sys.stdout, sys.stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cast_drift  # noqa: E402


def out(msg=""):
    print(msg, file=_REAL_STDOUT)


def err(msg):
    print(msg, file=_REAL_STDERR)

EXIT_OK, EXIT_DRIFT, EXIT_INCONCLUSIVE, EXIT_BAD_INPUT = 0, 1, 2, 4

_DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "gemini-veo-restoration-composer", "references", "cast-registry.json")


def _registry_path(explicit=None):
    if explicit:
        return explicit
    try:
        from prompt_pipeline.cast_lock import _registry_path as resolve
        return resolve() or _DEFAULT_REGISTRY
    except Exception:
        return _DEFAULT_REGISTRY


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_cast(registry, cast_id):
    for entry in registry.get("cast", []):
        if entry.get("id") == cast_id:
            return entry
    return None


def cmd_calibrate(args):
    path = _registry_path(args.registry)
    registry = _load(path)
    if _find_cast(registry, args.cast) is None:
        err(f"[-] 注册表里没有角色 {args.cast!r}")
        return EXIT_BAD_INPUT
    try:
        x, y = (float(v) for v in args.at.split(","))
    except ValueError:
        err(f"[-] --at 要写成 x,y 的相对坐标（0..1），收到 {args.at!r}")
        return EXIT_BAD_INPUT
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        err(f"[-] --at 的坐标必须在 0..1 之间（相对坐标，与分辨率无关）")
        return EXIT_BAD_INPUT
    if not os.path.exists(args.frame):
        err(f"[-] 帧文件不存在：{args.frame}")
        return EXIT_BAD_INPUT

    hex_value = cast_drift.sample_reference(args.frame, x, y, patch=args.patch)
    if args.dry_run:
        out(f"{args.cast} / {args.slot}  →  {hex_value}（--dry-run，未写回）")
        return EXIT_OK
    try:
        cast_drift.write_reference(path, args.cast, args.slot, hex_value)
    except KeyError as e:
        err(f"[-] {e}")
        return EXIT_BAD_INPUT
    out(f"已写回 {path}：{args.cast} / {args.slot} = {hex_value}")
    out("提示：取色点应落在该部位最大、最不受高光影响的一块上；标完用 check 看一眼"
          "基线覆盖率，太低说明取偏了。")
    return EXIT_OK


def cmd_check(args):
    path = _registry_path(args.registry)
    registry = _load(path)
    cast = _find_cast(registry, args.cast)
    if cast is None:
        known = ", ".join(e["id"] for e in registry.get("cast", [])) or "(无)"
        err(f"[-] 注册表里没有角色 {args.cast!r}；已登记：{known}")
        return EXIT_BAD_INPUT

    work = tempfile.mkdtemp(prefix="cast_drift_cli_")
    try:
        if args.project:
            if not os.path.isdir(args.project):
                err(f"[-] 项目目录不存在：{args.project}")
                return EXIT_BAD_INPUT
            segments = cast_drift.segments_from_project(args.project, count=args.frames,
                                                        work_dir=work)
        else:
            segments = []
            for i, video in enumerate(args.video, start=1):
                if not os.path.exists(video):
                    err(f"[-] 视频不存在：{video}")
                    return EXIT_BAD_INPUT
                sub = os.path.join(work, f"seg_{i:03d}")
                os.makedirs(sub, exist_ok=True)
                segments.append((f"VIDEO {i}",
                                 cast_drift.extract_frames(video, count=args.frames, out_dir=sub)))
        if not segments:
            err("[-] 没有可分析的视频段")
            return EXIT_BAD_INPUT

        report = cast_drift.analyze_project(segments, cast, delta_e=args.delta_e)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if args.json:
        out(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        out(f"cast: {cast['id']} ({cast.get('display_name','')})  段数: {len(segments)}")
        out(cast_drift.format_report(report))
    return {"drift_detected": EXIT_DRIFT,
            "inconclusive": EXIT_INCONCLUSIVE,
            "uncalibrated": EXIT_INCONCLUSIVE}.get(report["status"], EXIT_OK)


def main():
    ap = argparse.ArgumentParser(description="剪影级人物漂移探针（QC 表第 1～8 项）")
    ap.add_argument("--registry", help="cast-registry.json 路径（默认走 skill 目录解析）")
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("calibrate", help="从一张已认可的帧上取某个 T1 部位的参考色")
    c.add_argument("--cast", required=True)
    c.add_argument("--slot", required=True, help="T1 部位 id，如 jacket / gloves / beard")
    c.add_argument("--frame", required=True, help="一张已认可的帧")
    c.add_argument("--at", required=True, help="取色点相对坐标 x,y（0..1）")
    c.add_argument("--patch", type=int, default=5, help="取色方块半径（像素），默认 5")
    c.add_argument("--dry-run", action="store_true", help="只打印取到的颜色，不写回注册表")
    c.set_defaults(func=cmd_calibrate)

    k = sub.add_parser("check", help="逐段比对成片")
    k.add_argument("--cast", required=True)
    k.add_argument("--project", help="outputs/<项目> 目录（读 manifest.json 的 videos）")
    k.add_argument("--video", action="append", default=[], help="直接指定视频，可重复")
    k.add_argument("--frames", type=int, default=cast_drift.FRAMES_PER_SEGMENT,
                   help=f"每段抽几帧，默认 {cast_drift.FRAMES_PER_SEGMENT}")
    k.add_argument("--delta-e", type=float, default=cast_drift.DEFAULT_DELTA_E,
                   help=f"颜色容差 ΔE76，默认 {cast_drift.DEFAULT_DELTA_E}")
    k.add_argument("--json", action="store_true", help="输出机器可读的 JSON 报告")
    k.set_defaults(func=cmd_check)

    args = ap.parse_args()
    if args.command == "check" and not args.project and not args.video:
        ap.error("check 需要 --project 或至少一个 --video")
    try:
        return args.func(args)
    except RuntimeError as e:
        err(f"[-] {e}")
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
