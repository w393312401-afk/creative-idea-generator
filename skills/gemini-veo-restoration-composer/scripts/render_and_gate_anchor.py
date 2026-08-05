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

Exit codes:
    0 - the frame rendered; safe to continue composing the rest
    2 - could not connect to the service (connection refused/DNS/etc.) -- this is the
        only code that should trigger the Staged Delivery Contract's "server unreachable"
        waiver; a server that responds with an HTTP error or times out is NOT this case
    3 - server returned an error
    4 - missing required prompt text
    5 - request timed out waiting for the render (server was reachable and working, just
        slow); do NOT treat like exit 2 -- retry with patience or ask the user, don't
        silently fall back to unstaged full-set delivery
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

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


def safe_print(msg, is_err=False, end="\n"):
    file = sys.stderr if is_err else sys.stdout
    try:
        file.write(msg + end)
        file.flush()
    except UnicodeEncodeError:
        try:
            file.write(msg.encode('ascii', errors='replace').decode('ascii') + end)
            file.flush()
        except Exception:
            pass


def render_and_gate(server: str, title: str, prompt: str, sequence: int = 1, meta: str = "") -> dict:
    """POST to /api/render_anchor and return the parsed JSON response.

    This is a synchronous, blocking call by design (unlike /api/compose or
    /api/generate_frames, which return a task_id to poll): the whole point is for the
    calling agent to have the real anchor frame in hand before writing anything else."""
    url = server.rstrip("/") + "/api/render_anchor"
    payload = json.dumps({
        "title": title,
        "prompt": prompt,
        "sequence": sequence,
        "meta": meta,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        # Generous timeout: the gate renders + AI-judges up to 3 attempts server-side,
        # each of which can legitimately take tens of seconds to a couple of minutes.
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except TimeoutError as e:
        # A raw socket/read timeout is NOT the same thing as "server unreachable" (that's
        # urllib.error.URLError below, exit code 2). The server was reachable and working;
        # it just didn't finish within the wait window. Exit distinctly (5) so callers do
        # NOT treat this as the "render server unreachable" staging waiver.
        safe_print(f"[render_and_gate_anchor] ⏱️ 服务 {url} 在等待窗口内未返回结果（仍在渲染/判定中，不等同于服务不可用）: {e}", is_err=True)
        sys.exit(5)
    except urllib.error.URLError as e:
        safe_print(f"[render_and_gate_anchor] ❌ 无法连接到服务 {url}: {e}", is_err=True)
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(
        description="Render and AI-gate a single anchor frame before composing the rest of the beat ladder."
    )
    parser.add_argument("--title", required=True, help="主题标题（需与后续 save_to_library / generate_frames 一致）")
    parser.add_argument("--prompt", help="要渲染判定的图片提示词正文（与 --prompt_file 二选一）")
    parser.add_argument("--prompt_file", help="从本地文件读取提示词正文，避免命令行转义问题")
    parser.add_argument("--sequence", type=int, default=1, help="要渲染判定的图片序号，默认 1（首帧）")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"服务地址，默认 {DEFAULT_SERVER}")
    args = parser.parse_args()

    prompt = args.prompt
    if not prompt and args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read()
        except Exception as e:
            safe_print(f"[render_and_gate_anchor] ❌ 无法读取 --prompt_file {args.prompt_file}: {e}", is_err=True)
            sys.exit(4)
    if not prompt:
        safe_print("[render_and_gate_anchor] ❌ 必须提供 --prompt 或 --prompt_file", is_err=True)
        sys.exit(4)

    safe_print(f"[render_and_gate_anchor] 📡 正在渲染第 {args.sequence} 帧（可能需要一些时间，请耐心等待）...")
    result = render_and_gate(args.server, args.title, prompt, sequence=args.sequence)

    if result.get("status") != "ok":
        safe_print(f"[render_and_gate_anchor] ❌ 服务返回错误: {result.get('message') or result}", is_err=True)
        sys.exit(3)

    image_url = result.get("image_url")

    safe_print(f"[render_and_gate_anchor] ✅ 第 {args.sequence} 帧已渲染完成。图片: {image_url}")
    safe_print("[render_and_gate_anchor] 服务端不对这一帧做任何自动判定：请把图片给用户看过、"
               "确认这就是想要的锚帧之后，再继续生成后续内容。")
    sys.exit(0)


if __name__ == "__main__":
    main()
