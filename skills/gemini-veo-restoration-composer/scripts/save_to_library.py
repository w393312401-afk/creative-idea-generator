#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
save_to_library.py
==================
Saves a restoration prompt-composer output into the creative-idea-generator idea library
(library.json via the server API).

Usage (called by the agent after prompt generation):
    python save_to_library.py \
        --title  "做一个废弃阁楼翻新" \
        --prompt_block "图片提示词\n图片 1:\n..." \
        --audit_md  "| 指标 | 状态 | ... |" \
        [--creativity "gemini-veo-restoration-composer"] \
        [--image_count 7] \
        [--video_count 6] \
        [--server http://127.0.0.1:8085]

Exit codes — shared vocabulary, identical in all three helper scripts (see skill_common.py):
    0 - saved
    1 - other runtime failure
    2 - could not connect to the service
    3 - service reachable but returned an error
    4 - bad input
    5 - request timed out
    6 - not used by this script

This step writes a stable `slug` onto the entry (see skill_common.title_slug). generate_frames
prefers that slug over the raw title, so a later step still finds this entry even if the title
picks up a stray space or a full-width character on the way.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import (  # noqa: E402
    DEFAULT_SERVER, EXIT_SERVER_ERROR,
    enable_utf8_stdio, find_library_entry, http_json, safe_print, title_slug,
)

enable_utf8_stdio()

CONTEXT = "save_to_library"
LIBRARY_API = "/api/library"


def _count_slots(prompt_block: str, label: str) -> int:
    """Count labelled slots (图片 N: / 视频 N: / 视频 N [BRIDGE]:) inside prompt_block.

    Must stay in sync with prompt_pipeline._parse_prompt_slots, which allows an optional
    bracketed annotation ([BRIDGE], [BRIDGE TURN], [CUT], [HERO]) between the slot number
    and the colon.
    """
    import re
    return len(re.findall(rf'^{label}\s*\d+((?:\s*(?:[（\(].*?[）\)]|\[.*?\]))*)\s*[:：]', prompt_block, re.MULTILINE))


def fetch_library(server: str) -> list:
    data = http_json(server.rstrip("/") + LIBRARY_API, method="GET", timeout=30, context=CONTEXT)
    if not isinstance(data, list):
        safe_print(f"[{CONTEXT}] ❌ /api/library 返回的不是列表：{type(data).__name__}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
    return data


def post_library(server: str, data: list) -> None:
    result = http_json(server.rstrip("/") + LIBRARY_API, payload=data, method="POST",
                       timeout=60, context=CONTEXT)
    if result.get("status") != "success":
        safe_print(f"[{CONTEXT}] ⚠️  服务器返回非成功状态: {result}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)


def build_entry(title, prompt_block, audit_md, creativity, image_count, video_count) -> dict:
    ts = int(time.time() * 1000)
    now = time.strftime("%m/%d/%Y, %I:%M:%S %p", time.localtime())

    if image_count is None:
        image_count = _count_slots(prompt_block, "图片")
    if video_count is None:
        video_count = _count_slots(prompt_block, "视频")

    return {
        "id":            str(ts),
        "title":         title,
        "theme":         title,
        # 稳定连接键：三个脚本各自从标题算出同一个 slug，标题被顺手规范化也不会失联。
        "slug":          title_slug(title),
        "creativity":    creativity or "gemini-veo-restoration-composer",
        "prompt_block":  prompt_block,
        "audit_md":      audit_md,
        "repair_md":     "",
        "timestamp":     now,
        "timings":       {},
        "image_count":   image_count,
        "video_count":   video_count,
        "covers":        [],
        "frameRun":      None,
        "english_title": "",
        "collage_url":   "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Save a generated prompt set to the creative-idea-generator library."
    )
    parser.add_argument("--title",        required=True,  help="主题标题 (中文)")
    parser.add_argument("--prompt_block", required=True,  help="完整提示词文本块")
    parser.add_argument("--audit_md",     default="",     help="质量审核报告 Markdown 表格")
    parser.add_argument("--creativity",   default="gemini-veo-restoration-composer",
                        help="来源标签，默认 gemini-veo-restoration-composer")
    parser.add_argument("--image_count",  type=int, default=None, help="图片提示词数量（省略则自动统计）")
    parser.add_argument("--video_count",  type=int, default=None, help="视频提示词数量（省略则自动统计）")
    parser.add_argument("--server",       default=DEFAULT_SERVER,
                        help=f"点子库服务地址，默认 {DEFAULT_SERVER}")
    args = parser.parse_args()

    safe_print(f"[{CONTEXT}] 📡 连接点子库服务: {args.server}", is_err=True)
    library = fetch_library(args.server)

    entry = build_entry(
        title        = args.title,
        prompt_block = args.prompt_block,
        audit_md     = args.audit_md,
        creativity   = args.creativity,
        image_count  = args.image_count,
        video_count  = args.video_count,
    )

    # Dedup on the same tiered key generate_frames will use to read it back, so "saved" and
    # "found later" can never disagree about which entry is this topic's.
    existing_idx, old = find_library_entry(library, args.title)
    if existing_idx is not None:
        entry["id"]        = old.get("id", entry["id"])
        entry["timestamp"] = old.get("timestamp", entry["timestamp"])
        library[existing_idx] = entry
        action = "更新"
    else:
        library.insert(0, entry)
        action = "新增"

    post_library(args.server, library)
    safe_print(
        f"[{CONTEXT}] ✅ {action}成功！"
        f"  标题={args.title!r}  slug={entry['slug']}"
        f"  图片={entry['image_count']}  视频={entry['video_count']}",
        is_err=True,
    )


if __name__ == "__main__":
    main()
