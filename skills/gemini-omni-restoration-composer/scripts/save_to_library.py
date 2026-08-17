#!/usr/bin/env python3
"""
save_to_library.py
==================
Saves a restoration prompt-composer output into the
creative-idea-generator idea library (library.json via the server API).

Usage (called by the agent after prompt generation):
    python save_to_library.py \
        --title  "做一个废弃阁楼翻新" \
        --prompt_block "图片提示词\n图片 1:\n..." \
        --audit_md  "| 指标 | 状态 | ... |" \
        [--creativity "Tier-1 主题生成"] \
        [--image_count 7] \
        [--video_count 6] \
        [--server http://127.0.0.1:8085]

Returns exit code 0 on success, non-zero on failure.
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

# ── defaults ─────────────────────────────────────────────────────────────────
DEFAULT_SERVER = "http://127.0.0.1:8085"
LIBRARY_API    = "/api/library"


def _count_slots(prompt_block: str, label: str) -> int:
    """Count labelled slots (图片 N: / 视频 N: / 视频 N [BRIDGE]:) inside prompt_block.

    Must stay in sync with prompt_pipeline.py's _parse_prompt_slots, which allows an
    optional bracketed annotation like [BRIDGE] between the slot number and the colon."""
    import re
    return len(re.findall(rf'^{label}\s*\d+((?:\s*(?:[（\(].*?[）\)]|\[.*?\]))*)\s*[:：]', prompt_block, re.MULTILINE))


def fetch_library(server: str) -> list:
    url = server.rstrip("/") + LIBRARY_API
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[save_to_library] ❌ 无法连接到点子库服务 {url}: {e}", file=sys.stderr)
        sys.exit(2)


def post_library(server: str, data: list) -> None:
    url     = server.rstrip("/") + LIBRARY_API
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    req     = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") != "success":
                print(f"[save_to_library] ⚠️  服务器返回非成功状态: {result}", file=sys.stderr)
                sys.exit(3)
    except urllib.error.URLError as e:
        print(f"[save_to_library] ❌ 写入点子库失败 {url}: {e}", file=sys.stderr)
        sys.exit(4)


def build_entry(
    title:        str,
    prompt_block: str,
    audit_md:     str,
    creativity:   str,
    image_count:  int | None,
    video_count:  int | None,
) -> dict:
    ts   = int(time.time() * 1000)
    now  = time.strftime("%m/%d/%Y, %I:%M:%S %p", time.localtime())

    # Auto-count if not provided
    if image_count is None:
        image_count = _count_slots(prompt_block, "图片")
    if video_count is None:
        video_count = _count_slots(prompt_block, "视频")

    return {
        "id":            str(ts),
        "title":         title,
        "theme":         title,
        "creativity":    creativity or "gemini-omni-restoration-composer",
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
    parser.add_argument("--creativity",   default="gemini-omni-restoration-composer",
                        help="来源标签，默认 gemini-omni-restoration-composer")
    parser.add_argument("--image_count",  type=int, default=None,
                        help="图片提示词数量（省略则自动统计）")
    parser.add_argument("--video_count",  type=int, default=None,
                        help="视频提示词数量（省略则自动统计）")
    parser.add_argument("--server",       default=DEFAULT_SERVER,
                        help=f"点子库服务地址，默认 {DEFAULT_SERVER}")
    args = parser.parse_args()

    print(f"[save_to_library] 📡 连接点子库服务: {args.server}", file=sys.stderr)
    library = fetch_library(args.server)

    # Dedup: if the same title already exists, update it; otherwise prepend
    entry   = build_entry(
        title        = args.title,
        prompt_block = args.prompt_block,
        audit_md     = args.audit_md,
        creativity   = args.creativity,
        image_count  = args.image_count,
        video_count  = args.video_count,
    )

    existing_idx = next(
        (i for i, item in enumerate(library) if item.get("title") == args.title),
        None,
    )
    if existing_idx is not None:
        # Preserve existing id / timestamp; only update prompt fields
        old = library[existing_idx]
        entry["id"]        = old["id"]
        entry["timestamp"] = old["timestamp"]
        library[existing_idx] = entry
        action = "更新"
    else:
        library.insert(0, entry)
        action = "新增"

    post_library(args.server, library)
    print(
        f"[save_to_library] ✅ {action}成功！"
        f"  标题={args.title!r}"
        f"  图片={entry['image_count']}  视频={entry['video_count']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
