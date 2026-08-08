#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_and_gate_anchor.py
==========================
Synchronously renders ONE frame (by default IMAGE 1, the anchor) and blocks until it is
on disk. This is what makes SKILL.md's Step 6.5 (Staged Execution Mode) real for a
CONVERSATIONAL invocation of the gemini-veo-restoration-composer skill: call this right
after composing IMAGE 1's prompt and BEFORE composing anything else, so you and the user
can look at the real anchor frame before the rest of the pack is written.

2026-08-05: the server no longer runs any acceptance gate on this frame — every
generation-time consistency review was removed. This script therefore reports "rendered",
never "passed/failed". Show the user the image and let THEM decide whether to continue.

Usage:
    python render_and_gate_anchor.py --title "做一个废弃阁楼翻新" --prompt "Generate an image of..." [--server http://127.0.0.1:8085]
    python render_and_gate_anchor.py --title "..." --prompt_file prompt.txt

Exit codes — shared vocabulary, identical in all three helper scripts (see skill_common.py):
    0 - the frame rendered; safe to continue composing the rest
    1 - other runtime failure
    2 - could not connect to the service (connection refused/DNS). This is the ONLY code
        that triggers the Staged Delivery Contract's "server unreachable" waiver
    3 - service reachable but returned an error (HTTP 4xx/5xx, or status != ok)
    4 - bad input (missing prompt text, unreadable --prompt_file)
    5 - request timed out; the server is still working. Do NOT treat like exit 2 and do NOT
        fall back to unstaged full-set delivery — wait, or ask the user
    6 - not used by this script (title-not-found applies to library lookups only)

IMPORTANT: print the slug this script reports and reuse the SAME --title verbatim in
save_to_library.py and generate_frames.py. The three steps are chained by that title.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import (  # noqa: E402
    DEFAULT_SERVER, EXIT_BAD_INPUT, EXIT_SERVER_ERROR,
    enable_utf8_stdio, http_json, safe_print, title_slug,
)

enable_utf8_stdio()

CONTEXT = "render_and_gate_anchor"


def render_anchor(server, title, prompt, sequence=1, meta=""):
    """POST to /api/render_anchor and return the parsed JSON response.

    Synchronous and blocking by design (unlike /api/compose or /api/generate_frames, which
    return a task_id to poll): the whole point is for the calling agent to have the real
    anchor frame in hand before writing anything else. The generous timeout matches a render
    that can legitimately take minutes.
    """
    return http_json(
        server.rstrip("/") + "/api/render_anchor",
        payload={"title": title, "prompt": prompt, "sequence": sequence, "meta": meta},
        method="POST",
        timeout=900,
        context=CONTEXT,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Render a single anchor frame before composing the rest of the beat ladder."
    )
    parser.add_argument("--title", required=True,
                        help="主题标题（必须与后续 save_to_library / generate_frames 完全一致）")
    parser.add_argument("--prompt", help="要渲染的图片提示词正文（与 --prompt_file 二选一）")
    parser.add_argument("--prompt_file", help="从本地文件读取提示词正文，避免命令行转义问题")
    parser.add_argument("--sequence", type=int, default=1, help="要渲染的图片序号，默认 1（首帧）")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"服务地址，默认 {DEFAULT_SERVER}")
    args = parser.parse_args()

    prompt = args.prompt
    if not prompt and args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read()
        except Exception as e:
            safe_print(f"[{CONTEXT}] ❌ 无法读取 --prompt_file {args.prompt_file}: {e}", is_err=True)
            sys.exit(EXIT_BAD_INPUT)
    if not prompt or not prompt.strip():
        safe_print(f"[{CONTEXT}] ❌ 必须提供非空的 --prompt 或 --prompt_file", is_err=True)
        sys.exit(EXIT_BAD_INPUT)

    safe_print(f"[{CONTEXT}] 📡 正在渲染第 {args.sequence} 帧（可能需要几分钟，请耐心等待）...")
    result = render_anchor(args.server, args.title, prompt, sequence=args.sequence)

    if result.get("status") != "ok":
        safe_print(f"[{CONTEXT}] ❌ 服务返回错误: {result.get('message') or result}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)

    image_url = result.get("image_url")
    safe_print(f"[{CONTEXT}] ✅ 第 {args.sequence} 帧已渲染完成。图片: {image_url}")
    safe_print(f"[{CONTEXT}] 📎 本主题的稳定标识 slug={title_slug(args.title)}；"
               f"后续 save_to_library / generate_frames 请传入完全相同的 --title。")
    safe_print(f"[{CONTEXT}] 服务端不对这一帧做任何自动判定：请把图片给用户看过、"
               f"确认这就是想要的锚帧之后，再继续生成后续内容。")


if __name__ == "__main__":
    main()
