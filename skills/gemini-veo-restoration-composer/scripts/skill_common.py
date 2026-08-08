#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill_common.py
===============
Shared plumbing for the three agent-side helper scripts
(`render_and_gate_anchor.py` → `save_to_library.py` → `generate_frames.py`).

Standard library only, by design: these scripts run wherever the agent runs and must not
require the server project's dependencies. See `requirements-compose.txt`.

Two things live here, both of which used to be duplicated-or-missing and caused real
failures:

1. **A stable join key** (`title_slug`). The three scripts are chained purely by the Chinese
   topic title, and the library lookup used to demand exact string equality. One extra space,
   a full-width colon instead of a half-width one, or a model helpfully "tidying" the title
   between steps and the lookup misses. The failure is not benign: a missed lookup in
   `generate_frames.py` means `/api/render_staged` finds no existing frames and re-renders
   IMAGE 1 — throwing away the anchor frame the user just approved and rebuilding the whole
   downstream chain off a first frame nobody ever looked at, which is precisely what staged
   delivery exists to prevent.

2. **One exit-code vocabulary** (`EXIT_*`). The scripts previously disagreed: `5` meant
   "request timed out, the server is still working, be patient" in one and "title not found,
   this will never succeed" in the other — and they are called back-to-back in the same flow.

Exit codes (identical in all three scripts):

    0  success
    1  other runtime failure (progress stream broke, unexpected exception)
    2  service unreachable — connection refused/DNS. The ONLY code that triggers the
       Staged Delivery Contract's "render server unreachable" waiver.
    3  service reachable but returned an error (HTTP 4xx/5xx, or status != ok)
    4  bad input (missing prompt text, unreadable --prompt_file)
    5  request timed out; the server is still working. NOT a waiver — do not fall back to
       one-pass delivery, wait or ask the user.
    6  the requested title does not exist in the idea library. Terminal, not transient:
       retrying will never help. Re-run the save step, or pass --prompt_file.
"""

import hashlib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_UNREACHABLE = 2
EXIT_SERVER_ERROR = 3
EXIT_BAD_INPUT = 4
EXIT_TIMEOUT = 5
EXIT_NOT_FOUND = 6

DEFAULT_SERVER = "http://127.0.0.1:8085"


def enable_utf8_stdio():
    """Windows consoles default to a codepage that cannot print the emoji these scripts use."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def safe_print(msg, is_err=False, end="\n", flush=True):
    stream = sys.stderr if is_err else sys.stdout
    try:
        stream.write(msg + end)
        if flush:
            stream.flush()
    except UnicodeEncodeError:
        try:
            stream.write(msg.encode("ascii", errors="replace").decode("ascii") + end)
            if flush:
                stream.flush()
        except Exception:
            pass


def canonical_title(title):
    """Normalize a title for *comparison* only — never for display or for storage.

    NFKC folds full-width punctuation and spaces onto their half-width forms (so `阁楼：翻新`
    and `阁楼:翻新` compare equal), whitespace runs collapse, and the result is casefolded.
    Mirrors the normalization `server_common._safe_project_name` applies before deriving a
    project directory, so agent-side matching and server-side directory naming agree about
    which titles are "the same".
    """
    raw = unicodedata.normalize("NFKC", str(title or "")).strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw.casefold()


def title_slug(title):
    """Stable 12-hex-char key derived from the canonical title.

    Deterministic across processes and machines, so all three scripts compute the same slug
    from the same topic without any coordination. Stored on the library entry as `slug` by
    `save_to_library.py` and preferred by `generate_frames.py`, with title matching kept as a
    fallback for entries written before this field existed.
    """
    return hashlib.sha1(canonical_title(title).encode("utf-8")).hexdigest()[:12]


def find_library_entry(library, title):
    """Locate a library entry by, in order: slug, exact title/theme, canonical title/theme.

    Returns (index, entry) or (None, None). The tiered fallback is deliberate — exact match
    keeps old behaviour for untouched entries; the canonical tier is what rescues a title that
    picked up a stray space or a full-width character somewhere along the chain.
    """
    if not isinstance(library, list):
        return None, None
    want_slug = title_slug(title)
    want_canon = canonical_title(title)

    for tier in ("slug", "exact", "canonical"):
        for i, entry in enumerate(library):
            if not isinstance(entry, dict):
                continue
            if tier == "slug":
                if entry.get("slug") and entry["slug"] == want_slug:
                    return i, entry
            elif tier == "exact":
                if entry.get("title") == title or entry.get("theme") == title:
                    return i, entry
            else:
                if want_canon and want_canon in (
                    canonical_title(entry.get("title")),
                    canonical_title(entry.get("theme")),
                ):
                    return i, entry
    return None, None


def http_json(url, payload=None, method=None, timeout=30, context=""):
    """One HTTP+JSON call with the shared error→exit-code mapping.

    `HTTPError` is a *subclass* of `URLError`, so catching `URLError` first reports an HTTP
    500 as "cannot connect to the service" — which then reads as the unreachable-server
    waiver and silently downgrades staged delivery. It is caught first here, on purpose.
    """
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers=headers)
    label = f"[{context}] " if context else ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        safe_print(f"{label}❌ 服务返回 HTTP {e.code} ({url})：{detail or e.reason}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
    except TimeoutError as e:
        safe_print(f"{label}⏱️ 服务 {url} 在等待窗口内未返回（仍在处理中，不等同于服务不可用）：{e}",
                   is_err=True)
        sys.exit(EXIT_TIMEOUT)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            safe_print(f"{label}⏱️ 服务 {url} 在等待窗口内未返回（仍在处理中）：{e}", is_err=True)
            sys.exit(EXIT_TIMEOUT)
        safe_print(f"{label}❌ 无法连接到服务 {url}：{e}", is_err=True)
        sys.exit(EXIT_UNREACHABLE)
    except json.JSONDecodeError as e:
        safe_print(f"{label}❌ 服务 {url} 返回的不是合法 JSON：{e}", is_err=True)
        sys.exit(EXIT_SERVER_ERROR)
