#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_frames.py
==================
Triggers image sequence generation on the creative-idea-generator backend
and streams live progress via Server-Sent Events (SSE).

Usage:
    python generate_frames.py --title "做一个废弃阁楼翻新" [--server http://127.0.0.1:8085] [--aspect_ratio 9:16] [--quality 4K]

Exit codes — shared vocabulary, identical in all three helper scripts (see skill_common.py):
    0 - the frame sequence completed
    1 - other runtime failure (progress stream broke, generation reported an error)
    2 - could not connect to the service
    3 - service reachable but returned an error
    4 - bad input (unreadable --prompt_file)
    5 - request timed out; the server is still working
    6 - the title does not exist in the idea library. TERMINAL, not transient — retrying will
        never help. Run save_to_library.py first, or pass --prompt_file. (This used to be
        exit 5 here while exit 5 meant "be patient, still rendering" in
        render_and_gate_anchor.py — same flow, opposite instructions.)
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import (  # noqa: E402
    DEFAULT_SERVER, EXIT_BAD_INPUT, EXIT_NOT_FOUND, EXIT_RUNTIME, EXIT_SERVER_ERROR,
    enable_utf8_stdio, find_library_entry, http_json, safe_print, title_slug,
)

enable_utf8_stdio()

CONTEXT = "generate_frames"


def fetch_prompt_from_library(server: str, title: str) -> str:
    """Fetch prompt_block from the library for the given title.

    Matching is tiered (slug → exact title → canonical title) via skill_common. Exact string
    equality alone is too brittle for the one key that chains all three steps: a missed lookup
    here makes /api/render_staged find no existing frames and re-render IMAGE 1, discarding
    the anchor the user just approved.
    """
    library = http_json(server.rstrip("/") + "/api/library", method="GET", timeout=30,
                        context=CONTEXT)
    if not isinstance(library, list):
        safe_print(f"[{CONTEXT}] ❌ /api/library 返回的不是列表：{type(library).__name__}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
    idx, entry = find_library_entry(library, title)
    if entry is None:
        return ""
    matched = entry.get("title")
    if matched != title:
        safe_print(f"[{CONTEXT}] ℹ️ 标题非精确匹配，已按稳定标识 slug={title_slug(title)} "
                   f"命中库中条目 {matched!r}。")
    return entry.get("prompt_block") or ""


def trigger_generation(server, title, prompt_block, aspect_ratio="9:16", quality="2K"):
    """POST to /api/render_staged and return the task_id.

    /api/render_staged renders the already-composed prompt_block, reusing any frames already
    on disk (so the anchor rendered in Step 6.5 is not paid for twice) and then generating
    video. No frame is judged during rendering. See SKILL.md Step 6.5.

    The timeout is 60s, not 15s: this POST can synchronously spin up a task before it returns
    a task_id, and a 15s window turned ordinary server warm-up into a spurious failure.
    """
    result = http_json(
        server.rstrip("/") + "/api/render_staged",
        payload={
            "title": title,
            "prompt_block": prompt_block,
            "config": {"imageAspectRatio": aspect_ratio, "imageQuality": quality},
        },
        method="POST",
        timeout=60,
        context=CONTEXT,
    )
    if result.get("status") != "ok":
        safe_print(f"[{CONTEXT}] ❌ 服务器返回错误: {result.get('error') or result}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
    return result.get("task_id")


def stream_progress(server: str, task_id: str) -> bool:
    """Listen to the SSE progress stream and print updates."""
    url = server.rstrip("/") + f"/api/compose-stream?task_id={task_id}"
    req = urllib.request.Request(url)
    
    safe_print(f"[{CONTEXT}] 📡 已连接到进度流，等待任务执行...")
    
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for line in resp:
                line_str = line.decode("utf-8").strip()
                if not line_str.startswith("data:"):
                    continue
                
                try:
                    wrapper = json.loads(line_str[5:].strip())
                    event_type = wrapper.get("type")
                    event_data = wrapper.get("data")
                    
                    if event_type == "start":
                        total = event_data.get("total", "?")
                        safe_print(f"\n[作图开始] 开始生成连贯画幅帧序列，预计生成 {total} 张图片...\n")
                    
                    elif event_type == "frame_start":
                        seq = event_data.get("sequence", "?")
                        total = event_data.get("total", "?")
                        safe_print(f" -> 正在生成第 {seq}/{total} 帧图片...", end="", flush=False)
                    
                    elif event_type == "frame":
                        seq = event_data.get("sequence", "?")
                        frame_info = event_data.get("frame", {})
                        filepath = frame_info.get("file", "unknown")
                        degraded_note = ""
                        if frame_info.get("quality_gate") == "auto_approved_degraded":
                            degraded_note = "⚠️ 未核验（VLM 判定服务异常被放行）"
                        # Clear line and print completed
                        safe_print(f"\r[作图进度] 第 {seq} 帧已完成！{degraded_note}文件：{filepath}")
                        
                    elif event_type == "frame_retry":
                        seq = event_data.get("sequence", "?")
                        attempt = event_data.get("attempt", 1)
                        reason = event_data.get("reason", "VLM 审计失败")
                        safe_print(f"\n   ⚠️ 第 {seq} 帧未通过 VLM 连续性检查。原因: {reason}。正在进行智能重试 (第 {attempt} 次)...")

                    elif event_type == "anchor_check":
                        seq = event_data.get("sequence", "?")
                        attempt = event_data.get("attempt", 1)
                        passed = event_data.get("passed")
                        reason = event_data.get("reason") or ""
                        if passed and reason.startswith("Skipped ("):
                            # 判定服务异常被 fail-open 放行：不是真实通过，必须如实播报
                            safe_print(f"\n   ⚠️ 第 {seq} 帧为降级放行（第 {attempt} 次）：AI 判定服务异常，帧未经真实核验（{reason}）。请把这一情况如实告知用户，不要当作已通过判定。")
                        elif passed:
                            safe_print(f"\n   ✅ 第 {seq} 帧 AI 自动判定通过（第 {attempt} 次），无需人工审核。")
                        else:
                            safe_print(f"\n   ❌ 第 {seq} 帧 AI 判定未通过（第 {attempt} 次）。原因: {reason or '未说明原因'}")

                    elif event_type == "anchor_retry":
                        seq = event_data.get("sequence", "?")
                        attempt = event_data.get("attempt", 1)
                        reason = event_data.get("reason", "未说明原因")
                        safe_print(f"   🔁 正在依据判定反馈重写第 {seq} 帧提示词并重新渲染（第 {attempt} 次重试）...")

                    elif event_type == "packet_refine_start":
                        safe_print(f"\n   🧭 {event_data.get('message', '正在依据首帧修正空间一致性数据...')}")

                    elif event_type == "packet_refined":
                        safe_print(f"   ✅ {event_data.get('message', '空间一致性数据已修正。')}")

                    elif event_type == "needs_human_review":
                        seq = event_data.get("sequence", "?")
                        reason = event_data.get("reason", "未说明原因")
                        safe_print(f"\n   🛑 第 {seq} 帧多次重试后仍未通过 AI 判定，已停止自动流程，需要人工介入。原因: {reason}")

                    elif event_type == "video_retry_autonomous":
                        slots = event_data.get("slots", [])
                        safe_print(f"\n   🔁 检测到视频片段 {slots} 生成失败/被拦截，正在自动重试...")

                    elif event_type == "result":
                        project_dir = event_data.get("project_dir", "outputs")
                        if event_data.get("status") == "needs_human_review":
                            safe_print(f"\n[作图暂停] 🛑 首帧多次重试后仍未通过 AI 判定，流程已停止，等待人工处理。项目目录: {project_dir}")
                        else:
                            safe_print(f"\n[作图完成] ✅ 帧序列已生成完毕！项目目录: {project_dir}")
                        return True

                    elif event_type == "error":
                        msg = event_data.get("message", "未知错误")
                        safe_print(f"\n[作图失败] ❌ 生成中断: {msg}", is_err=True)
                        return False
                        
                except Exception as ex:
                    pass
    except Exception as e:
        safe_print(f"\n[{CONTEXT}] ⚠️ 进度流连接中断或请求超时: {e}\n"
                   f"          注意：服务端任务可能仍在后台继续跑。这只说明本脚本失去了进度流，"
                   f"不代表生成失败——先去 outputs/ 目录或前端帧网格确认实际进度，再决定是否重跑。",
                   is_err=True)
        return False
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Trigger and monitor image frame sequence generation."
    )
    parser.add_argument("--title", required=True, help="主题标题 (与 library.json 中的 title 一致)")
    parser.add_argument("--prompt_file", help="可选，从本地 file 读取提示词块（若不提供则从点子库获取）")
    parser.add_argument("--aspect_ratio", default="9:16", help="图片宽高比，如 9:16, 16:9, 1:1, 默认 9:16")
    parser.add_argument("--quality", default="2K", help="图片清晰度，如 1K, 2K, 4K, 默认 2K")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"点子库服务地址，默认 {DEFAULT_SERVER}")
    args = parser.parse_args()

    prompt_block = ""
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt_block = f.read()
            safe_print(f"[{CONTEXT}] 📂 已从本地文件 {args.prompt_file} 读取提示词")
        except Exception as e:
            safe_print(f"[{CONTEXT}] ❌ 无法读取本地提示词文件 {args.prompt_file}: {e}", is_err=True)
            sys.exit(EXIT_BAD_INPUT)

    if not prompt_block:
        prompt_block = fetch_prompt_from_library(args.server, args.title)

    if not prompt_block:
        safe_print(
            f"[{CONTEXT}] ❌ 点子库中找不到标题为 {args.title!r}（slug={title_slug(args.title)}）的提示词。\n"
            f"          这是终局性错误，重试不会好转。二选一：\n"
            f"            1) 先跑 save_to_library.py 把这套提示词入库，再重跑本脚本；\n"
            f"            2) 用 --prompt_file <文件> 直接把提示词正文传进来（Step 12 入库失败时的降级路径）。",
            is_err=True)
        sys.exit(EXIT_NOT_FOUND)

    safe_print(f"[{CONTEXT}] 📡 正在向服务 {args.server} 提交生成任务（画幅 {args.aspect_ratio}，清晰度 {args.quality}）...")
    task_id = trigger_generation(args.server, args.title, prompt_block, args.aspect_ratio, args.quality)
    safe_print(f"[{CONTEXT}] 🚀 任务创建成功，Task ID: {task_id}")

    success = stream_progress(args.server, task_id)
    if not success:
        sys.exit(EXIT_RUNTIME)


if __name__ == "__main__":
    main()
