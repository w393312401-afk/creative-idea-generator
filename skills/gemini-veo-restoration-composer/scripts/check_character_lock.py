#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_character_lock.py
=======================
Machine-checks a delivered prompt set against a registered Named Cast Lock character, so
"每一段都逐字重述同一组锚点词" stops being a promise and becomes an exit code.

Why this script exists at all: cross-segment character consistency in a memoryless pipeline
comes 100% from repeating the SAME anchor words verbatim in every segment. That is a
string-equality property, which means a human re-reading fourteen prompts is the wrong tool —
one `khaki jacket` where the registry says `faded olive-green work jacket` silently generates
a different person in that segment, and eyeballing prose is exactly how that slips through.
The protocol calls for a full-text comparison against the fixed vocabulary; this is it.

    python scripts/check_character_lock.py --cast jake-miller <prompt-set.md>
    python scripts/check_character_lock.py --cast jake-miller --strict <prompt-set.md>
    python scripts/check_character_lock.py --list-casts

This file is the CLI shell only. The gates themselves live in scripts/cast_lock_core.py,
because the server (prompt_pipeline.cast_lock) runs the same gates during composition and two
copies of eight regexes would drift apart within a month. Vocabulary comes from
references/cast-registry.json — never from a copy pasted into either file.

Standard library only, matching the other helper scripts in this directory.

Exit code 0: every person-bearing prompt is locked (warnings may still be printed).
Exit code 1: at least one lock violation (a drifted anchor, a missing identity block, a
             cross-segment coreference, an undescribed generic agent, a missing negative,
             Hero Agent Lock residue) — or, under --strict, at least one warning.
Exit code 4: bad input (unknown cast id, unreadable target, malformed registry).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import enable_utf8_stdio  # noqa: E402
import cast_lock_core as core  # noqa: E402

enable_utf8_stdio()

SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SKILL_DIR / "references" / "cast-registry.json"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_BAD_INPUT = 4


def fail(msg):
    print(f"[-] {msg}", file=sys.stderr)
    return EXIT_BAD_INPUT


def load_registry():
    if not REGISTRY_PATH.exists():
        return None, f"cast registry not found: {REGISTRY_PATH}"
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, f"cast registry is not valid JSON: {e}"


def main():
    ap = argparse.ArgumentParser(
        description="Check a prompt set against a registered Named Cast Lock character.")
    ap.add_argument("target", nargs="?", help="prompt-set markdown file to check")
    ap.add_argument("--cast", help="cast id from references/cast-registry.json")
    ap.add_argument("--list-casts", action="store_true", help="list registered cast ids and exit")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings (e.g. unverifiable identity-block placement) as failures")
    args = ap.parse_args()

    registry, err = load_registry()
    if err:
        return fail(err)

    if args.list_casts:
        for entry in registry.get("cast", []):
            print(f"{entry['id']:<16} {entry.get('display_name','')} "
                  f"(mode {entry.get('pipeline_mode','?')}) — {entry.get('role','')}")
        return EXIT_OK

    if not args.cast or not args.target:
        return fail("both --cast and a target file are required (or use --list-casts)")

    cast = core.find_cast(registry, args.cast)
    if cast is None:
        known = ", ".join(e["id"] for e in registry.get("cast", [])) or "(none)"
        return fail(f"unknown cast id {args.cast!r}; registered: {known}")

    target = Path(args.target)
    if not target.exists():
        return fail(f"target not found: {target}")
    text = target.read_text(encoding="utf-8")

    prompts = core.split_prompts(text)
    if not prompts:
        return fail(f"no `视频 N:` / `图片 N:` prompt slots found in {target}")

    globals_ = core.registry_globals(registry, cast)
    fnd, stats = core.audit_prompt_set(prompts, cast, globals_)

    mode = str(cast.get("pipeline_mode") or "A").upper()
    print(f"cast: {cast['id']} ({cast.get('display_name','')})  mode {mode}  "
          f"glove policy: {cast.get('glove_policy','—')}")
    print(f"target: {target}")
    print(f"action-onset markers: {globals_['action_onset_markers']}")
    print(f"slots: {stats['total']} total, {stats['judged']} judged, "
          f"{stats['person_free']} person-free (skipped)"
          + (f", {stats['out_of_mode']} IMAGE (mode A — person-free by contract, owned by "
             f"Clean Frame Boundary)" if stats["out_of_mode"] else ""))

    fnd.report(strict=args.strict)
    errors, warnings = fnd.errors, fnd.warnings
    if errors or (warnings and args.strict):
        counted = len(errors) + (len(warnings) if args.strict else 0)
        print(f"\n{counted} character-lock violation(s)"
              + (f", {len(warnings)} promoted from warnings by --strict"
                 if args.strict and warnings else ""))
        return EXIT_VIOLATION
    if warnings:
        print(f"\n{len(warnings)} warning(s); all person-bearing prompts otherwise locked "
              f"(re-run with --strict to fail on these)")
        return EXIT_OK
    print("\nall person-bearing prompts locked")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
