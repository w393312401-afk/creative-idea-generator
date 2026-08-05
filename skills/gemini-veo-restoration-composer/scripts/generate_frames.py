#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_frames.py
==================
Triggers image sequence generation on the creative-idea-generator backend
and streams live progress via Server-Sent Events (SSE).

Usage:
    python generate_frames.py --title "做一个废弃阁楼翻新" [--server http://127.0.0.1:8085] [--aspect_ratio 9:16] [--quality 4K]
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# Reconfigure stdout/stderr to UTF-8 on Windows to prevent UnicodeEncodeError when printing emojis
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

DEFAULT_SERVER = "http://127.0.0.1:8085"


def safe_print(msg, is_err=False, end="\n", flush=True):
    """Safely print message catching encoding errors if reconfigure wasn't enough."""
    file = sys.stderr if is_err else sys.stdout
    try:
        file.write(msg + end)
        if flush:
            file.flush()
    except UnicodeEncodeError:
        # Fallback to printing ascii-safe characters
        try:
            safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
            file.write(safe_msg + end)
            if flush:
                file.flush()
        except Exception:
            pass


def fetch_prompt_from_library(server: str, title: str) -> str:
    """Fetch prompt_block from the library for the given title."""
    url = server.rstrip("/") + "/api/library"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            library = json.loads(resp.read().decode("utf-8"))
            for entry in library:
                if entry.get("title") == title or entry.get("theme") == title:
                    return entry.get("prompt_block") or ""
    except Exception as e:
        safe_print(f"[generate_frames] ⚠️  无法从点子库获取提示词 ({e})", is_err=True)
    return ""


def trigger_generation(server: str, title: str, prompt_block: str, aspect_ratio: str = "9:16", quality: str = "2K") -> str:
    """POST to /api/render_staged and return the task_id.

    /api/render_staged renders the already-composed prompt_block, reusing any frames
    already on disk (so the anchor rendered in Step 6.5 is not paid for twice) and then
    generating video. No frame is judged during rendering. See SKILL.md Step 6.5."""
    url = server.rstrip("/") + "/api/render_staged"
    payload = json.dumps({
        "title": title,
        "prompt_block": prompt_block,
        "config": {
            "imageAspectRatio": aspect_ratio,
            "imageQuality": quality
        }
    }, ensure_ascii=False).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "ok":
                return result.get("task_id")
            else:
                safe_print(f"[generate_frames] ❌ 服务器返回错误: {result.get('error') or result}", is_err=True)
                sys.exit(3)
    except urllib.error.URLError as e:
        safe_print(f"[generate_frames] ❌ 无法连接到服务 {url}: {e}", is_err=True)
        sys.exit(2)


def stream_progress(server: str, task_id: str) -> bool:
    """Listen to the SSE progress stream and print updates."""
    url = server.rstrip("/") + f"/api/compose-stream?task_id={task_id}"
    req = urllib.request.Request(url)
    
    safe_print(f"[generate_frames] 📡 已连接到进度流，等待任务执行...")
    
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
        safe_print(f"\n[generate_frames] ⚠️ 连接中断或请求超时: {e}", is_err=True)
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
            safe_print(f"[generate_frames] 📂 已从本地文件 {args.prompt_file} 读取提示词")
        except Exception as e:
            safe_print(f"[generate_frames] ❌ 无法读取本地提示词文件 {args.prompt_file}: {e}", is_err=True)
            sys.exit(4)
            
    if not prompt_block:
        prompt_block = fetch_prompt_from_library(args.server, args.title)
        
    if not prompt_block:
        safe_print(f"[generate_frames] ❌ 找不到标题为 '{args.title}' 的提示词模板。请确认该点子已被保存到库中，或者使用 --prompt_file 传入提示词文本。", is_err=True)
        sys.exit(5)

    safe_print(f"[generate_frames] 📡 正在向服务 {args.server} 提交生成任务...")
    task_id = trigger_generation(args.server, args.title, prompt_block, args.aspect_ratio, args.quality)
    safe_print(f"[generate_frames] 🚀 任务创建成功，Task ID: {task_id}")
    
    success = stream_progress(args.server, task_id)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
