#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lint_skill.py
=============
One-shot health check for this skill package. Standard library only.

    python scripts/lint_skill.py                 # lint the package this script lives in
    python scripts/lint_skill.py --path <dir>    # lint a specific package directory
    python scripts/lint_skill.py --json          # machine-readable output

Exit codes: 0 = clean, 1 = at least one ERROR, 2 = only WARNINGs.

WHY THIS EXISTS
---------------
Everything checked here is a class of defect this package actually shipped, where the prose
contract and the artefact disagreed and nothing noticed. The contracts are enforced by
`prompt_pipeline` **on the server** (see SKILL.md "Where The Contracts Are Actually
Enforced") — but only for server-driven composition. Nothing at all checked the *package's
own* files: its templates, its examples, its cross-references. That gap is what this covers.

Deliberately NOT checked: anything requiring judgement (is this beat one operation? is this
anchor plausible?). Those stay with the Step 9 self-check gates. Everything here is
mechanical, so a failure is a fact rather than an opinion.
"""

import argparse
import json
import os
import re
import sys

# Findings quote Chinese slot labels; a Windows console on the default codepage turns those
# into mojibake and buries the actual message.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

# ── the single authority for these numbers is SKILL.md Step 7, which mirrors the runtime
# constants prompt_pipeline.IMAGE_WORD_LIMIT / BASE_VIDEO_WORD_LIMIT ─────────────────────
IMAGE_WORD_LIMIT = 180
VIDEO_WORD_LIMIT = 380

# prompt_pipeline.check_nlvtr_violations' literal list
BANNED_ACRONYMS = ("HAL", "TSPA", "VMFP", "GCTR", "RPL", "RCE", "SCUP", "NGCS",
                   "OSPL", "RHMA", "PBISP", "HCL", "NLVTR", "MTAL")

GRID_RE = re.compile(r"\bGrid\s+[A-Ca-c]\s*[1-3]\b")
PERCENT_NUM_RE = re.compile(r"\b\d+\s*(?:-\s*)?(?:percent|%)")
NUMERIC_RANGE_RE = re.compile(r"\b\d+\s*%?\s*(?:to|-)\s*\d+\s*%?\s*(?:cm|m|kg|s|h|l|ml)?\b")
# MULTILINE matters: this pattern is used both line-by-line and with finditer over whole
# files. Without it the anchors bind to the whole string and every finditer scan returns
# nothing — which reads as "no bridge tag anywhere" rather than "the check never ran".
SLOT_RE = re.compile(r"^(图片|视频)[ \t]*(\d+)[ \t]*(\[[A-Z][A-Z ]*\])?[ \t]*:[ \t]*$", re.MULTILINE)
BRIDGE_TAGS = ("[BRIDGE]", "[BRIDGE TURN]", "[CUT]")

# references/prompt-templates.md labels its worked exemplars with headings instead of the
# 图片/视频 slot labels examples/ use — a separate pattern is needed to find their bodies.
EXEMPLAR_RE = re.compile(r"^#{4,5}\s+Exemplar\s+[A-Z]", re.MULTILINE)

# Absolute paths belonging to somebody else's machine. A model that follows one finds nothing
# and improvises, which is worse than having no pointer at all.
FOREIGN_PATH_RE = re.compile(
    r"(?:/Users/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\)"
    r"(?![Uu]sers?>|<)"
)

# Structures retired by TBCP v4 (2026-07-21). Their presence anywhere but an explicit
# "this is retired" sentence means a stale copy of the protocol survived a revision.
RETIRED_TBCP = ("Bridge-1", "Bridge-2", "Sill Handoff", "sill handoff", "IMAGE T+2")
RETIRED_TBCP_ALLOWED_CONTEXT = ("retired", "废弃", "never split", "fails this gate",
                                "supersedes", "v2/v3", "no longer")


class Findings:
    def __init__(self):
        self.items = []

    def add(self, level, rule, path, detail, line=None):
        self.items.append({"level": level, "rule": rule, "file": path,
                           "line": line, "detail": detail})

    error = lambda self, *a, **k: self.add("ERROR", *a, **k)      # noqa: E731
    warn = lambda self, *a, **k: self.add("WARN", *a, **k)        # noqa: E731


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rel(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def walk(root, exts):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in sorted(files):
            if os.path.splitext(name)[1] in exts:
                yield os.path.join(base, name)


# ── checks ───────────────────────────────────────────────────────────────────────────────

def check_prompt_slots(root, fnd):
    """Word budget + notation ban on every labelled prompt slot in every example.

    Examples are what a composing model imitates. An example that violates the contract it
    illustrates teaches the violation more effectively than the contract teaches the rule —
    all 18 IMAGE slots of this package's only worked example once exceeded the limit.
    """
    for path in walk(os.path.join(root, "examples"), {".md"}):
        r = rel(root, path)
        lines = read(path).split("\n")
        cur = None
        for i, line in enumerate(lines, 1):
            m = SLOT_RE.match(line.strip())
            if m:
                cur = (m.group(1), int(m.group(2)), m.group(3), i)
                continue
            if not cur or not line.strip():
                continue
            kind, num, tag, label_line = cur
            cur = None
            body = line.strip()
            is_img = kind == "图片"
            slot = f"{'IMAGE' if is_img else 'VIDEO'} {num}"
            limit = IMAGE_WORD_LIMIT if is_img else VIDEO_WORD_LIMIT
            words = len(body.split())
            if words > limit:
                fnd.error("word-budget", r,
                          f"{slot} is {words} words, over the {limit}-word validator limit "
                          f"(over by {words - limit})", label_line)
            if GRID_RE.search(body):
                hits = sorted(set(GRID_RE.findall(body)))
                fnd.error("grid-leak", r,
                          f"{slot} carries grid notation {hits} in the delivered body; "
                          f"image models render these as literal letters", i)
            if "%" in body:
                fnd.error("notation-percent", r, f"{slot} contains a '%' symbol", i)
            elif PERCENT_NUM_RE.search(body):
                fnd.error("notation-percent", r,
                          f"{slot} contains a numeric percentage "
                          f"({PERCENT_NUM_RE.search(body).group(0)!r}); write it as a fraction", i)
            if NUMERIC_RANGE_RE.search(body):
                fnd.error("notation-range", r,
                          f"{slot} contains a numeric range "
                          f"({NUMERIC_RANGE_RE.search(body).group(0)!r})", i)
            for ac in BANNED_ACRONYMS:
                if re.search(rf"\b{ac}\b", body):
                    fnd.error("notation-acronym", r,
                              f"{slot} leaks the internal acronym {ac!r}", i)
            if tag and is_img:
                fnd.error("bridge-tag", r,
                          f"{slot} carries the meta tag {tag} — only VIDEO slots take one",
                          label_line)
            if tag and not is_img and tag not in BRIDGE_TAGS:
                fnd.error("bridge-tag", r,
                          f"{slot} has unknown meta tag {tag}; expected one of {BRIDGE_TAGS}",
                          label_line)


def check_reference_templates(root, fnd):
    """Same word budget + notation ban as check_prompt_slots, but for the canonical
    exemplars in references/prompt-templates.md.

    This file is loaded during Steps 7-8 and is what a composing model imitates most
    directly for slot shape, so a violation here teaches the violation before the model
    ever reaches examples/. check_prompt_slots never covered it because these exemplars are
    headed `#### Exemplar A (...)`, not the `图片 N:` / `视频 N:` labels examples/ uses.
    """
    p = os.path.join(root, "references", "prompt-templates.md")
    if not os.path.exists(p):
        return
    r = rel(root, p)
    section = None
    cur = None
    for i, line in enumerate(read(p).split("\n"), 1):
        stripped = line.strip()
        if stripped == "## IMAGE Templates":
            section = "IMAGE"
        elif stripped == "## VIDEO Templates":
            section = "VIDEO"
        if EXEMPLAR_RE.match(stripped):
            cur = (i, stripped[:60])
            continue
        if not cur:
            continue
        if not stripped or stripped.startswith("**"):
            continue
        label_line, label = cur
        cur = None
        slot = f"{section} exemplar ({label})"
        limit = IMAGE_WORD_LIMIT if section == "IMAGE" else VIDEO_WORD_LIMIT
        words = len(stripped.split())
        if words > limit:
            fnd.error("word-budget", r,
                      f"{slot} is {words} words, over the {limit}-word validator limit "
                      f"(over by {words - limit})", label_line)
        if GRID_RE.search(line):
            hits = sorted(set(GRID_RE.findall(line)))
            fnd.error("grid-leak", r,
                      f"{slot} carries grid notation {hits} in the delivered body; "
                      f"image models render these as literal letters", i)
        if "%" in line:
            fnd.error("notation-percent", r, f"{slot} contains a '%' symbol", i)
        elif PERCENT_NUM_RE.search(line):
            fnd.error("notation-percent", r,
                      f"{slot} contains a numeric percentage "
                      f"({PERCENT_NUM_RE.search(line).group(0)!r}); write it as a fraction", i)
        if NUMERIC_RANGE_RE.search(line):
            fnd.error("notation-range", r,
                      f"{slot} contains a numeric range "
                      f"({NUMERIC_RANGE_RE.search(line).group(0)!r})", i)
        for ac in BANNED_ACRONYMS:
            if re.search(rf"\b{ac}\b", line):
                fnd.error("notation-acronym", r,
                          f"{slot} leaks the internal acronym {ac!r}", i)


def check_bridge_tagging(root, fnd):
    """A threshold crossing must be exactly one clip, and it must be meta-tagged.

    An untagged crossing is read by the renderer's continuity judge as an exterior→interior
    error and burns retry budget on a frame that was correct.
    """
    for path in walk(os.path.join(root, "examples"), {".md"}):
        r = rel(root, path)
        text = read(path)
        # A file can be *named* threshold-mode and still route Standard — the buried-vehicle
        # example exists precisely to show a topic that never crosses a threshold on camera.
        # Only a declared Threshold route needs a crossing clip.
        if not re.search(r"^\s*Mode:\s*Threshold", text, re.MULTILINE):
            continue
        if not SLOT_RE.search(text):
            continue  # a routing/ladder specimen with no prompt bodies has nothing to tag
        tagged = [m for m in SLOT_RE.finditer(text) if m.group(3)]
        tagged_videos = [m for m in tagged if m.group(1) == "视频"]
        if not tagged_videos:
            fnd.error("bridge-tag-missing", r,
                      "threshold example contains no meta-tagged crossing clip; expected "
                      "exactly one 视频 N [BRIDGE] / [BRIDGE TURN] / [CUT] label")
        elif len(tagged_videos) > 1:
            nums = [m.group(2) for m in tagged_videos]
            fnd.error("bridge-tag-count", r,
                      f"{len(tagged_videos)} tagged crossing clips (slots {nums}); TBCP v4 "
                      f"allows exactly one per crossing")


def check_retired_tbcp(root, fnd):
    for path in list(walk(root, {".md"})):
        r = rel(root, path)
        if r.endswith("threshold-bridge-consistency-protocol.md"):
            continue
        for i, line in enumerate(read(path).split("\n"), 1):
            for token in RETIRED_TBCP:
                if token in line and not any(c in line for c in RETIRED_TBCP_ALLOWED_CONTEXT):
                    fnd.error("tbcp-stale", r,
                              f"references the retired two-clip bridge structure "
                              f"({token!r}); TBCP v4 merged the crossing into one beat", i)
                    break


def check_aspect_consistency(root, fnd):
    """The grid, the anchors, and the renderer must agree on which frame shape they describe."""
    for path in walk(root, {".md"}):
        r = rel(root, path)
        for i, line in enumerate(read(path).split("\n"), 1):
            if "16:9" in line and "9:16" not in line:
                fnd.error("aspect-mismatch", r,
                          "declares a 16:9 frame; this pipeline renders 9:16 vertical by "
                          "default, so every grid cell and Z-depth scale written for 16:9 "
                          "is wrong", i)


def check_word_budget_claims(root, fnd):
    """Only one place may state the budget. Competing numbers are how this drifted before."""
    pat = re.compile(r"(\d{2,3})\s*[-–]\s*(\d{2,3})\s*words")
    for path in walk(root, {".md"}):
        r = rel(root, path)
        for i, line in enumerate(read(path).split("\n"), 1):
            low = line.lower()
            if "word" not in low:
                continue
            for m in pat.finditer(line):
                lo, hi = int(m.group(1)), int(m.group(2))
                if hi not in (IMAGE_WORD_LIMIT, VIDEO_WORD_LIMIT) and lo >= 90:
                    fnd.warn("word-budget-claim", r,
                             f"states a word range {lo}-{hi} whose ceiling is neither "
                             f"{IMAGE_WORD_LIMIT} (IMAGE) nor {VIDEO_WORD_LIMIT} (VIDEO)", i)


def check_foreign_paths(root, fnd):
    for path in walk(root, {".md", ".py", ".json", ".yaml", ".yml", ".txt"}):
        r = rel(root, path)
        if r.endswith("scripts/lint_skill.py"):
            continue
        for i, line in enumerate(read(path).split("\n"), 1):
            m = FOREIGN_PATH_RE.search(line)
            if m:
                fnd.error("foreign-path", r,
                          f"hardcodes an absolute per-machine path ({m.group(0)}...); it is a "
                          f"dead link everywhere else — use <SKILL_DIR> or a relative path", i)


def check_dead_links(root, fnd):
    link = re.compile(r"\[[^\]]+\]\((?!https?:|#)([^)#]+)")
    for path in walk(root, {".md"}):
        r = rel(root, path)
        base = os.path.dirname(path)
        for i, line in enumerate(read(path).split("\n"), 1):
            for m in link.finditer(line):
                target = m.group(1).strip()
                if not os.path.exists(os.path.join(base, target)):
                    fnd.error("dead-link", r, f"links to missing file {target!r}", i)


def check_contract_registry(root, fnd):
    p = os.path.join(root, "references", "contract-registry.json")
    if not os.path.exists(p):
        fnd.error("registry-missing", "references/contract-registry.json",
                  "the machine-readable contract registry is absent")
        return
    r = "references/contract-registry.json"
    try:
        data = json.loads(read(p))
    except json.JSONDecodeError as e:
        fnd.error("registry-invalid", r, f"is not valid JSON: {e}")
        return
    seen = set()
    for c in data.get("contracts", []):
        cid = c.get("id")
        if not cid:
            fnd.error("registry-entry", r, "a contract has no id")
            continue
        if cid in seen:
            fnd.error("registry-entry", r, f"duplicate contract id {cid!r}")
        seen.add(cid)
        # A null enforcer is legitimate — but only when the hole is written down.
        if c.get("enforcer") is None and not c.get("gap"):
            fnd.error("registry-gap", r,
                      f"contract {cid!r} has no enforcer and no 'gap' explanation; an "
                      f"unenforced contract must be registered as such, never silent")
        src = c.get("source", "")
        if src.startswith("references/"):
            if not os.path.exists(os.path.join(root, src)):
                fnd.error("registry-source", r,
                          f"contract {cid!r} cites a missing source file {src!r}")


def check_ledger_dna(root, fnd):
    """Dedup compares fingerprints, so a malformed fingerprint silently matches nothing."""
    p = os.path.join(root, "references", "used-topic-ledger.md")
    if not os.path.exists(p):
        return
    r = "references/used-topic-ledger.md"
    families = {"natural", "man-made", "vehicle", "fantasy", "living-tree", "treehouse"}
    roots, bad_carrier, placeholder = {}, 0, 0
    rows = 0
    for i, line in enumerate(read(p).split("\n"), 1):
        if not line.startswith("| 20"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3:
            continue
        parts = [x.strip() for x in cells[2].split("/")]
        if len(parts) != 3:
            continue
        rows += 1
        carrier, _, twist = parts
        if carrier not in families:
            bad_carrier += 1
        if twist in ("custom-twist", "unspecified-twist"):
            placeholder += 1
            continue
        root_key = "-".join(twist.split("-")[:2])
        roots.setdefault(root_key, []).append(i)
    for root_key, hits in sorted(roots.items(), key=lambda kv: -len(kv[1])):
        if len(hits) >= 3:
            fnd.warn("ledger-twist-cluster", r,
                     f"twist root {root_key!r} is used {len(hits)} times "
                     f"({len(hits) * 100 // max(rows, 1)}% of rows) — suffix variants are one "
                     f"idea in costumes, not distinct topics")
    if bad_carrier:
        fnd.warn("ledger-carrier-field", r,
                 f"{bad_carrier} of {rows} rows put a specific carrier in the carrier-FAMILY "
                 f"field; a unique string collides with nothing and is invisible to dedup")
    if placeholder:
        fnd.warn("ledger-placeholder-twist", r,
                 f"{placeholder} rows fingerprint the twist as custom/unspecified — those "
                 f"match nothing and block nothing")
    if rows > 80:
        fnd.warn("ledger-size", r,
                 f"{rows} DNA rows; this file is loaded whole into the ideation prompt. "
                 f"Compact per the ledger's archive rhythm (keep 60, collapse the rest)")


def check_scripts(root, fnd):
    """The three helper scripts are chained by one key and share one exit-code vocabulary."""
    sd = os.path.join(root, "scripts")
    if not os.path.isdir(sd):
        return
    chain = ("render_and_gate_anchor.py", "save_to_library.py", "generate_frames.py")
    for name in chain:
        p = os.path.join(sd, name)
        r = f"scripts/{name}"
        if not os.path.exists(p):
            fnd.error("script-missing", r, "helper script referenced by SKILL.md is absent")
            continue
        src = read(p)
        if "skill_common" not in src:
            fnd.warn("script-shared", r,
                     "does not use scripts/skill_common.py; the join key and the exit-code "
                     "vocabulary must be shared or the three steps drift apart again")
        # HTTPError subclasses URLError: catching URLError first reports an HTTP 500 as
        # "cannot connect", which then reads as the unreachable-server staging waiver.
        iu, ih = src.find("except urllib.error.URLError"), src.find("except urllib.error.HTTPError")
        if iu != -1 and (ih == -1 or ih > iu):
            fnd.error("http-error-swallowed", r,
                      "catches URLError before HTTPError (or not at all); HTTPError is a "
                      "SUBCLASS of URLError, so HTTP 4xx/5xx is misreported as 'service "
                      "unreachable' and wrongly triggers the staging waiver")


def check_skill_md_refs(root, fnd):
    p = os.path.join(root, "SKILL.md")
    if not os.path.exists(p):
        fnd.error("skill-md-missing", "SKILL.md", "the package has no SKILL.md")
        return
    text = read(p)
    for name in ("render_and_gate_anchor.py", "save_to_library.py", "generate_frames.py"):
        if name in text and not os.path.exists(os.path.join(root, "scripts", name)):
            fnd.error("skill-md-script-ref", "SKILL.md",
                      f"references scripts/{name}, which does not exist")
    if "9:16" not in text:
        fnd.warn("aspect-undeclared", "SKILL.md",
                 "never states the render aspect ratio; every grid cell and Z-depth scale "
                 "below it is then written for an unknown frame shape")


CHECKS = (check_prompt_slots, check_reference_templates, check_bridge_tagging, check_retired_tbcp,
          check_aspect_consistency, check_word_budget_claims, check_foreign_paths,
          check_dead_links, check_contract_registry, check_ledger_dna, check_scripts,
          check_skill_md_refs)


def main():
    ap = argparse.ArgumentParser(description="Health check for the restoration composer skill package.")
    ap.add_argument("--path", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="package directory to lint (default: the one this script lives in)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    root = os.path.abspath(args.path)
    fnd = Findings()
    for check in CHECKS:
        try:
            check(root, fnd)
        except Exception as e:  # a broken check must not hide the other ten
            fnd.error("lint-internal", "scripts/lint_skill.py",
                      f"check {check.__name__} raised {type(e).__name__}: {e}")

    errors = [f for f in fnd.items if f["level"] == "ERROR"]
    warns = [f for f in fnd.items if f["level"] == "WARN"]

    if args.json:
        print(json.dumps({"root": root, "errors": len(errors), "warnings": len(warns),
                          "findings": fnd.items}, ensure_ascii=False, indent=2))
    else:
        for f in errors + warns:
            loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            print(f"{f['level']:5s} [{f['rule']}] {loc}\n      {f['detail']}")
        print(f"\n{len(errors)} error(s), {len(warns)} warning(s) in {root}")
        if not errors and not warns:
            print("clean")

    sys.exit(1 if errors else (2 if warns else 0))


if __name__ == "__main__":
    main()
