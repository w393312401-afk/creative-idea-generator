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
    python scripts/check_character_lock.py --cast jake-miller --list-casts

Standard library only, matching the other helper scripts in this directory. It enforces the
prose contracts in references/character-consistency-protocol.md and reads its vocabulary from
references/cast-registry.json — never from a copy pasted into this file.

Exit code 0: every person-bearing prompt is locked.
Exit code 1: at least one lock violation (a drifted anchor, a missing identity block, a
             cross-segment coreference, an undescribed generic agent, a missing negative).
Exit code 4: bad input (unknown cast id, unreadable target, malformed registry).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill_common import enable_utf8_stdio  # noqa: E402

enable_utf8_stdio()

SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SKILL_DIR / "references" / "cast-registry.json"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_BAD_INPUT = 4

# The identity block must be front-loaded, ahead of the action verbs, or the action words
# dilute the identity words and late segments stop looking like the character. The pacing
# phrase is the stable proxy for "where the action description starts" in this pipeline's
# VIDEO template.
PACING_PHRASE = "continuous construction time-lapse"

# A prompt is only judged if it actually contains a person. A worker-free bridge or reward
# clip legitimately has no identity block, and flagging those would train people to ignore
# this script. Matched on word boundaries, not substrings — bare `he` as a substring hits
# every `the` in the file and would mark all fifteen slots as person-bearing.
PERSON_MARKERS = re.compile(
    r"\b(workers?|contractor|assistant|crew|he|his|him|men|man)\b", re.IGNORECASE)

# Explicit person-free declarations. These prompts are person-free by contract (threshold
# bridges and the reward reveal), so they carry no identity block by design. The list covers
# the negated phrasings the delivered sets actually use — a person-free IMAGE anchor says so
# in prose ("the frame is empty of workers"), and a naive `workers` substring hit would read
# that as a person and demand an identity block in a frame contractually required to have none.
PERSON_FREE_MARKERS = ("no person", "no people", "no worker", "no one appears",
                       "empty of workers", "without workers")

# The negative list is only meaningful on prompts that show the character's clothing. Hands
# and gloves are clothing; a purely off-screen hand-crank is not.
SLOT_LABELS = {
    "cap": "帽", "jacket": "外套", "shirt": "内搭", "trousers": "裤",
    "boots": "靴", "gloves": "手套", "beard": "胡须", "eyes": "眼睛", "hair": "发色",
}


def fail(msg):
    print(f"[-] {msg}", file=sys.stderr)
    return EXIT_BAD_INPUT


class Findings:
    def __init__(self):
        self.rows = []

    def add(self, gate, slot, detail):
        self.rows.append((gate, slot, detail))

    def report(self):
        if not self.rows:
            return
        by_slot = {}
        for gate, slot, detail in self.rows:
            by_slot.setdefault(slot, []).append((gate, detail))
        for slot in sorted(by_slot, key=lambda s: (s != "GLOBAL", s)):
            print(f"\n{slot}")
            for gate, detail in by_slot[slot]:
                print(f"  FAIL [{gate}] {detail}")


def load_registry():
    if not REGISTRY_PATH.exists():
        return None, f"cast registry not found: {REGISTRY_PATH}"
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, f"cast registry is not valid JSON: {e}"


def find_cast(registry, cast_id):
    for entry in registry.get("cast", []):
        if entry.get("id") == cast_id:
            return entry
    return None


def split_prompts(text):
    """Slice a delivered prompt set into (label, body) pairs.

    Splits on the Chinese slot headers the delivered sets actually use (`视频 7:`,
    `视频 5 [BRIDGE]:`, `图片 3:`) rather than on blank lines, because a single prompt is one
    long paragraph and blank-line splitting would merge the whole file into one blob.
    """
    header = re.compile(r"^(视频|图片)\s*(\d+)\s*(\[[A-Z]+\])?\s*[:：]", re.MULTILINE)
    marks = list(header.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip().rstrip("`").strip()
        kind = "VIDEO" if m.group(1) == "视频" else "IMAGE"
        out.append((f"{kind} {m.group(2)}", body))
    return out


def mask_identity_blocks(body, blocks):
    """Remove registered identity blocks before hunting banned synonyms.

    Without this, every banned term that is a substring of its own required term produces a
    false positive: `dark-brown boots` contains `brown boots`, `faded olive-green work jacket`
    contains `green work jacket`, and `neatly trimmed light-brown short beard` contains
    `short beard`. Masking the legitimate carrier first means a banned hit in what remains is
    a real hit — a colour word written loose in the action prose, which is precisely the drift
    the vocabulary lock exists to catch.
    """
    masked = body
    for block in sorted(blocks, key=len, reverse=True):
        masked = masked.replace(block, " <IDENTITY> ")
    return masked


NEGATION_CUE = re.compile(r"\b(?:no|not|never|without|nor)\s+$", re.IGNORECASE)


def negated(text, start):
    """True when the term at `start` sits inside a prohibition rather than a description.

    The negative list bans its items by naming them — `no hardhat, no hi-vis vest` contains
    two banned terms on purpose. Without this guard the vocabulary gate would fire on the very
    clause that enforces the vocabulary, and the only way to get a clean run would be to delete
    the negative list.
    """
    return bool(NEGATION_CUE.search(text[max(0, start - 10):start]))


def contains_person(body):
    low = body.lower()
    if any(marker in low for marker in PERSON_FREE_MARKERS):
        return False
    return bool(PERSON_MARKERS.search(body))


def check_prompt(label, body, cast, globals_, fnd):
    low = body.lower()
    blocks = cast["identity_blocks"]
    short, mid, full = blocks["short"], blocks["mid"], blocks["full"]

    present = [b for b in (full, mid, short) if b in body]
    total_occurrences = sum(body.count(b) for b in (full, mid, short))

    # Gate 1 — identity block present exactly once.
    if not present:
        fnd.add("identity-block-missing", label,
                "含人物但没有逐字出现任何身份块；跨段无记忆，泛指等于放弃控制")
    elif total_occurrences > 1:
        fnd.add("identity-block-repeated", label,
                f"身份块出现 {total_occurrences} 次；重复描写会稀释注意力权重，全条只允许一次，"
                f"后续用 he 回指")

    # Gate 2 — front-loading. Only checkable when the pacing phrase is present, which is the
    # ordinary construction clip; bridges and reward clips omit it by contract.
    if present and PACING_PHRASE in low:
        block = present[0]
        if body.index(block) > low.index(PACING_PHRASE):
            fnd.add("identity-block-placement", label,
                    "身份块出现在动作段之后；必须前置到动作动词之前，紧贴主语位置")

    # Gate 3 — banned synonyms anywhere outside the identity block.
    masked = mask_identity_blocks(body, [full, mid, short]).lower()
    for anchor in cast["tier1_anchors"]:
        for banned in anchor["banned"]:
            hits = [m for m in re.finditer(r"\b" + re.escape(banned.lower()) + r"\b", masked)
                    if not negated(masked, m.start())]
            if hits:
                slot = SLOT_LABELS.get(anchor["slot"], anchor["slot"])
                fnd.add("vocabulary-drift", label,
                        f"{slot}：出现禁止写法 {banned!r}，唯一允许写法是 "
                        f"{anchor['required']!r}")

    # Gate 4 — cross-segment coreference. The model has no memory of the previous segment, so
    # a back-reference resolves to nothing and the segment invents a new person.
    for phrase in globals_["coreference_bans"]:
        if phrase.lower() in masked:
            fnd.add("cross-segment-coreference", label,
                    f"出现跨段指代 {phrase!r}；模型跨段无记忆，必须整块重述身份块")

    # Gate 5 — undescribed generic agents left over from the pre-lock draft.
    for phrase in globals_["generic_agent_residue"]:
        hits = [m for m in re.finditer(r"\b" + re.escape(phrase.lower()) + r"\b", masked)
                if not negated(masked, m.start())]
        if hits:
            fnd.add("generic-agent-residue", label,
                    f"出现泛指人物 {phrase!r}；替换为身份块或同段内 he 回指")

    # Gate 6 — the three always-on negatives.
    for neg in globals_["always_on_negatives"]:
        if neg.lower() not in low:
            fnd.add("negative-list-missing", label, f"缺少常驻负面项 {neg!r}")

    # Gate 7 — two-person shots need a differentiated assistant, or the model renders two
    # copies of the character or crosses their features.
    assistant = cast["assistant_lock"]
    plural_hit = re.search(r"\b(two|both)\s+(men|figures|people|workers)\b", low)
    if (plural_hit or "assistant" in low) and assistant.lower() not in low:
        fnd.add("assistant-undifferentiated", label,
                f"双人镜缺少助手区分句；必须写 {assistant!r}")


def main():
    ap = argparse.ArgumentParser(
        description="Check a prompt set against a registered Named Cast Lock character.")
    ap.add_argument("target", nargs="?", help="prompt-set markdown file to check")
    ap.add_argument("--cast", help="cast id from references/cast-registry.json")
    ap.add_argument("--list-casts", action="store_true", help="list registered cast ids and exit")
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

    cast = find_cast(registry, args.cast)
    if cast is None:
        known = ", ".join(e["id"] for e in registry.get("cast", [])) or "(none)"
        return fail(f"unknown cast id {args.cast!r}; registered: {known}")

    target = Path(args.target)
    if not target.exists():
        return fail(f"target not found: {target}")
    text = target.read_text(encoding="utf-8")

    prompts = split_prompts(text)
    if not prompts:
        return fail(f"no `视频 N:` / `图片 N:` prompt slots found in {target}")

    globals_ = {
        "always_on_negatives": registry.get("always_on_negatives", []),
        "coreference_bans": registry.get("coreference_bans", []),
        "generic_agent_residue": registry.get("generic_agent_residue", []),
    }

    # Mode gating. In mode A the character lives only in VIDEO slots and every IMAGE anchor is
    # person-free by contract, so judging IMAGE slots here would only produce noise — and
    # whether an IMAGE anchor wrongly contains a person is already owned by Clean Frame
    # Boundary (`image-clean-frame`, which has a real server-side enforcer). In mode B the
    # IMAGE anchors carry the character's face and must each restate the identity block, so
    # they are judged too.
    mode = (cast.get("pipeline_mode") or "A").upper()
    judged_kinds = ("VIDEO",) if mode == "A" else ("VIDEO", "IMAGE")

    fnd = Findings()
    judged = skipped = out_of_mode = 0
    for label, body in prompts:
        if not label.startswith(judged_kinds):
            out_of_mode += 1
            continue
        if not contains_person(body):
            skipped += 1
            continue
        judged += 1
        check_prompt(label, body, cast, globals_, fnd)

    print(f"cast: {cast['id']} ({cast.get('display_name','')})  mode {mode}  "
          f"glove policy: {cast.get('glove_policy','—')}")
    print(f"target: {target}")
    print(f"slots: {len(prompts)} total, {judged} judged, {skipped} person-free (skipped)"
          + (f", {out_of_mode} IMAGE (mode A — person-free by contract, owned by Clean Frame "
             f"Boundary)" if out_of_mode else ""))

    fnd.report()
    if fnd.rows:
        print(f"\n{len(fnd.rows)} character-lock violation(s)")
        return EXIT_VIOLATION
    print("\nall person-bearing prompts locked")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
