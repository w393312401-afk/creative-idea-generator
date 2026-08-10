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
<<<<<<< Updated upstream
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
=======
    2 - could not connect to the service (connection refused/DNS/etc., i.e. urllib never got
        a response at all) -- this is the ONLY code that should trigger the Staged Delivery
        Contract's "server unreachable" waiver. Before 2026-08-07 this code was also
        (incorrectly) raised whenever the server responded with an HTTP error status, because
        urllib.error.HTTPError is a subclass of URLError and was being caught by the same
        broad `except URLError` -- a plain HTTP 500 looked identical to "service not running"
        and silently triggered the one-pass full-set-delivery waiver. HTTPError is now caught
        first and mapped to exit 3 instead.
    3 - server reached and responded, but with an error: non-2xx HTTP status, a malformed
        (non-JSON) response body, a JSON body missing/empty "image_url", or (when the URL
        looks like a local filesystem path rather than an http(s) URL) a path that doesn't
        exist on disk despite the server claiming success
    4 - missing required prompt text
    5 - request timed out waiting for the render (server was reachable and working, just
        slow) -- this covers both a read timeout after the request was sent (raised by
        urllib as TimeoutError) and a connect-phase timeout (raised by urllib as a URLError
        whose .reason is a socket.timeout/TimeoutError -- previously misclassified as exit 2
        "unreachable" here too). Do NOT treat exit 5 like exit 2 -- retry with patience or
        ask the user, don't silently fall back to unstaged full-set delivery.
"""

import argparse
import json
import os
import socket
>>>>>>> Stashed changes
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import (  # noqa: E402
    DEFAULT_SERVER, EXIT_BAD_INPUT, EXIT_SERVER_ERROR,
    enable_utf8_stdio, http_json, safe_print, title_slug,
)

enable_utf8_stdio()

CONTEXT = "render_and_gate_anchor"


<<<<<<< Updated upstream
def render_anchor(server, title, prompt, sequence=1, meta="", force_regenerate=False):
    """POST to /api/render_anchor and return the parsed JSON response.

    Synchronous and blocking by design (unlike /api/compose or /api/generate_frames, which
    return a task_id to poll): the whole point is for the calling agent to have the real
    anchor frame in hand before writing anything else. The generous timeout matches a render
    that can legitimately take minutes.
    """
    payload = {
=======
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


def render_and_gate(server: str, title: str, prompt: str, sequence: int = 1, meta: str = "",
                    force_regenerate: bool = False) -> dict:
    """POST to /api/render_anchor and return the parsed JSON response.

    This is a synchronous, blocking call by design (unlike /api/compose or
    /api/generate_frames, which return a task_id to poll): the whole point is for the
    calling agent to have the real anchor frame in hand before writing anything else."""
    url = server.rstrip("/") + "/api/render_anchor"
    body = {
>>>>>>> Stashed changes
        "title": title,
        "prompt": prompt,
        "sequence": sequence,
        "meta": meta,
    }
    if force_regenerate:
<<<<<<< Updated upstream
        payload["force_regenerate"] = True
    return http_json(
        server.rstrip("/") + "/api/render_anchor",
        payload=payload,
=======
        # Forwarded for the server to act on if it supports it (e.g. skip any disk-cache
        # reuse for this sequence number). If the server doesn't recognize the field it's
        # simply ignored -- harmless either way. See Step 6.5 point 8(c) in SKILL.md: a
        # rejected anchor must not be silently reused by a later render call.
        body["force_regenerate"] = True
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
>>>>>>> Stashed changes
        method="POST",
        timeout=900,
        context=CONTEXT,
    )
<<<<<<< Updated upstream
=======
    try:
        # Generous timeout: the gate renders + AI-judges up to 3 attempts server-side,
        # each of which can legitimately take tens of seconds to a couple of minutes.
        with urllib.request.urlopen(req, timeout=900) as resp:
            raw = resp.read().decode("utf-8")
    except TimeoutError as e:
        # A raw socket/read timeout after the request was sent is NOT the same thing as
        # "server unreachable" (that's urllib.error.URLError below, exit code 2). The server
        # was reachable and working; it just didn't finish within the wait window. Exit
        # distinctly (5) so callers do NOT treat this as the "render server unreachable"
        # staging waiver.
        safe_print(f"[render_and_gate_anchor] ⏱️ 服务 {url} 在等待窗口内未返回结果（仍在渲染/判定中，不等同于服务不可用）: {e}", is_err=True)
        sys.exit(5)
    except urllib.error.HTTPError as e:
        # HTTPError IS a URLError subclass -- it must be caught BEFORE the plain URLError
        # handler below, or a 500/503 from a server that is very much running looks
        # identical to "connection refused" and wrongly triggers the unreachable waiver.
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        safe_print(f"[render_and_gate_anchor] ❌ 服务返回 HTTP {e.code} {e.reason}: {url}"
                  + (f"\n{detail}" if detail else ""), is_err=True)
        sys.exit(3)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            # Timed out during connection setup rather than after connecting -- still "the
            # server is slow", not "the server doesn't exist". Same exit code as the
            # post-connect TimeoutError case above, for the same reason.
            safe_print(f"[render_and_gate_anchor] ⏱️ 连接服务 {url} 超时（不等同于服务不可用）: {e}", is_err=True)
            sys.exit(5)
        safe_print(f"[render_and_gate_anchor] ❌ 无法连接到服务 {url}: {e}", is_err=True)
        sys.exit(2)
>>>>>>> Stashed changes

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        safe_print(f"[render_and_gate_anchor] ❌ 服务 {url} 返回体不是合法 JSON: {e}\n"
                  f"原始响应（截断）: {raw[:500]}", is_err=True)
        sys.exit(3)


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
    parser.add_argument("--force_regenerate", action="store_true",
                        help="首帧被用户否决、需要重渲同一序号时使用：告知服务端不要复用磁盘上已有的旧帧"
                             "（SKILL.md Step 6.5 第 8 条）。服务端若不支持此字段会被安全忽略。")
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

<<<<<<< Updated upstream
    safe_print(f"[{CONTEXT}] 📡 正在渲染第 {args.sequence} 帧（可能需要几分钟，请耐心等待）...")
    result = render_anchor(args.server, args.title, prompt, sequence=args.sequence,
                          force_regenerate=args.force_regenerate)
=======
    safe_print(f"[render_and_gate_anchor] 📡 正在渲染第 {args.sequence} 帧（可能需要一些时间，请耐心等待）...")
    result = render_and_gate(args.server, args.title, prompt, sequence=args.sequence,
                             force_regenerate=args.force_regenerate)
>>>>>>> Stashed changes

    if result.get("status") != "ok":
        safe_print(f"[{CONTEXT}] ❌ 服务返回错误: {result.get('message') or result}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)

    image_url = result.get("image_url")
    if not image_url:
<<<<<<< Updated upstream
        safe_print(f"[{CONTEXT}] ❌ 服务声称成功（status=ok）但没有返回 image_url: {result}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
=======
        safe_print(f"[render_and_gate_anchor] ❌ 服务声称成功（status=ok）但没有返回 image_url: {result}", is_err=True)
        sys.exit(3)
    # If the server handed back a local filesystem path rather than an http(s) URL, verify
    # the docstring's promise ("blocks until it is on disk") actually held -- previously this
    # was taken on faith and printed as success even if the path didn't exist.
    if not image_url.startswith(("http://", "https://")) and not os.path.exists(image_url):
        safe_print(f"[render_and_gate_anchor] ❌ 服务返回的本地路径不存在: {image_url}", is_err=True)
        sys.exit(3)
>>>>>>> Stashed changes

    if not image_url.startswith(("http://", "https://")) and not os.path.exists(image_url):
        safe_print(f"[{CONTEXT}] ❌ 服务返回的本地路径不存在: {image_url}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)

    safe_print(f"[{CONTEXT}] ✅ 第 {args.sequence} 帧已渲染完成。图片: {image_url}")
    safe_print(f"[{CONTEXT}] 📎 本主题的稳定标识 slug={title_slug(args.title)}；"
               f"后续 save_to_library / generate_frames 请传入完全相同的 --title。")
    safe_print(f"[{CONTEXT}] 服务端不对这一帧做任何自动判定：请把图片给用户看过、"
               f"确认这就是想要的锚帧之后，再继续生成后续内容。")
    sys.exit(0)


if __name__ == "__main__":
    main()

