#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cast_lock_core.py
=================
The Named Cast Lock gates themselves — one implementation, two callers.

Why this file exists separately from check_character_lock.py: cross-segment character
consistency now has to be judged in two places. The CLI (`check_character_lock.py`) judges a
delivered prompt-set markdown before hand-off; the server (`prompt_pipeline.cast_lock`) judges
each beat's prompt as it is composed, because a lock that only fires when someone remembers to
run a CLI is a lock that is off by default. Two copies of eight regex gates would drift within
a month — that is the exact failure mode the contract registries in this package exist to
catch — so the gates live here and both callers import them.

Two hard constraints shape this file:

  * **Standard library only, no imports from this package.** The helper scripts run wherever
    the agent runs, not necessarily where the server's virtualenv lives (see
    requirements-compose.txt). This module must stay importable from a bare interpreter.
  * **No vocabulary literals.** Every anchor word, banned synonym, negative and marker comes
    from references/cast-registry.json. The registry is the single copy of the identity block
    text; a second copy pasted into code is the next drift.

The callers own their own presentation and exit codes; this module only produces findings.
"""
import re

# Fallback only. The real list is per-cast `action_onset_markers` in the registry — this
# package's own delivered sets are construction time-lapses, so that phrase is the historical
# default, but a marker list hard-coded in the checker is what made Gate 2 silently no-op on
# every non-construction topic. Keeping the fallback narrow and the registry authoritative
# means a new topic family fails loudly (see `identity-block-placement-unverifiable`) instead
# of passing with the gate switched off.
DEFAULT_ACTION_ONSET_MARKERS = ("continuous construction time-lapse",)

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"

# A prompt is only judged if it actually contains a person. A worker-free bridge or reward
# clip legitimately has no identity block, and flagging those would train people to ignore
# this checker. Matched on word boundaries, not substrings — bare `he` as a substring hits
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

SLOT_LABELS = {
    "cap": "帽", "jacket": "外套", "shirt": "内搭", "trousers": "裤",
    "boots": "靴", "gloves": "手套", "beard": "胡须", "eyes": "眼睛", "hair": "发色",
}

NEGATION_CUE = re.compile(r"\b(?:no|not|never|without|nor)\s+$", re.IGNORECASE)


class Findings:
    """Ordered findings with a severity split.

    Severity exists for exactly one gate — `identity-block-placement-unverifiable`. That one
    says "this prompt has no registered action-onset marker, so front-loading could not be
    judged", which is an unverifiability signal, not a proven violation. Failing the build on
    it would make every new topic family red on day one and teach people to invent a fake
    marker to silence it; silently skipping it is what this whole change is undoing. A loud
    warning, promotable with --strict, is the honest middle.
    """

    def __init__(self):
        self.rows = []

    def add(self, gate, slot, detail, level=LEVEL_ERROR):
        self.rows.append((gate, slot, detail, level))

    @property
    def errors(self):
        return [r for r in self.rows if r[3] == LEVEL_ERROR]

    @property
    def warnings(self):
        return [r for r in self.rows if r[3] == LEVEL_WARN]

    def messages(self, prefix="Named Cast Lock"):
        """Flat strings, for callers that merge findings into an existing error list."""
        return [f"{prefix} [{gate}] {slot}: {detail}" for gate, slot, detail, _ in self.rows]

    def report(self, strict=False):
        if not self.rows:
            return
        by_slot = {}
        for gate, slot, detail, level in self.rows:
            by_slot.setdefault(slot, []).append((gate, detail, level))
        for slot in sorted(by_slot, key=lambda s: (s != "GLOBAL", s)):
            print(f"\n{slot}")
            for gate, detail, level in by_slot[slot]:
                tag = "FAIL" if (level == LEVEL_ERROR or strict) else "WARN"
                print(f"  {tag} [{gate}] {detail}")


def find_cast(registry, cast_id):
    for entry in (registry or {}).get("cast", []):
        if entry.get("id") == cast_id:
            return entry
    return None


def registry_globals(registry, cast=None):
    """Cross-cast vocabulary, with the per-cast override applied where one exists.

    `action_onset_markers` is the only per-cast key: which phrase marks "the action starts
    here" is a property of the topic family the character appears in, not of the character.
    """
    registry = registry or {}
    cast = cast or {}
    markers = (cast.get("action_onset_markers")
               or registry.get("action_onset_markers")
               or list(DEFAULT_ACTION_ONSET_MARKERS))
    return {
        "always_on_negatives": registry.get("always_on_negatives", []),
        "coreference_bans": registry.get("coreference_bans", []),
        "generic_agent_residue": registry.get("generic_agent_residue", []),
        "hero_agent_lock_markers": registry.get("hero_agent_lock_markers", []),
        "action_onset_markers": [str(m) for m in markers if str(m).strip()],
    }


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
        if block:
            masked = masked.replace(block, " <IDENTITY> ")
    return masked


def negated(text, start):
    """True when the term at `start` sits inside a prohibition rather than a description.

    The negative list bans its items by naming them — `no hardhat, no hi-vis vest` contains
    two banned terms on purpose. Without this guard the vocabulary gate would fire on the very
    clause that enforces the vocabulary, and the only way to get a clean run would be to delete
    the negative list.
    """
    return bool(NEGATION_CUE.search(text[max(0, start - 10):start]))


def contains_person(body):
    low = (body or "").lower()
    if any(marker in low for marker in PERSON_FREE_MARKERS):
        return False
    return bool(PERSON_MARKERS.search(body or ""))


def _first_marker_index(low, markers):
    """Earliest position of any registered action-onset marker, or None if none appear."""
    hits = [low.index(m.lower()) for m in markers if m.lower() in low]
    return min(hits) if hits else None


def check_prompt(label, body, cast, globals_, fnd):
    """Gates 1–7: the per-prompt identity contract for one person-bearing prompt."""
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

    # Gate 2 — front-loading, judged against the cast's registered action-onset markers.
    # When none of them appear the gate cannot run, and that fact is itself reported: the
    # previous hard-coded single phrase meant every non-construction topic passed this gate
    # without it ever executing, with a clean report and exit code 0.
    if present:
        markers = globals_["action_onset_markers"]
        onset = _first_marker_index(low, markers)
        if onset is None:
            fnd.add("identity-block-placement-unverifiable", label,
                    f"未出现任何已登记的动作起点标记 {markers!r}，身份块前置无法判定；"
                    f"给这个角色登记 action_onset_markers，否则这道门等于没有",
                    level=LEVEL_WARN)
        elif body.index(present[0]) > onset:
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
    assistant = cast.get("assistant_lock") or ""
    plural_hit = re.search(r"\b(two|both)\s+(men|figures|people|workers)\b", low)
    if assistant and (plural_hit or "assistant" in low) and assistant.lower() not in low:
        fnd.add("assistant-undifferentiated", label,
                f"双人镜缺少助手区分句；必须写 {assistant!r}")


def check_lock_exclusivity(label, body, globals_, fnd):
    """Gate 8: Hero Agent Lock residue inside a Named Cast Lock project.

    The two locks are mutually exclusive by contract (`named-cast-hal-exclusivity`, P0) and
    the reason is mechanical, not stylistic: Hero Agent Lock dresses the agent in a
    neon-yellow vest and white hardhat and forbids the face, while Named Cast Lock registers a
    specific cap, jacket and beard and forbids the vest. A prompt carrying both hands the
    model two contradictory wardrobe contracts for one person, and it resolves them by
    blending — which is the identity morphing both locks exist to prevent.

    Judged on every prompt, not only the person-bearing ones: a slot that declares itself
    person-free while still carrying a leftover vest clause is precisely the residue this gate
    is for, and `contains_person` would skip it.
    """
    low = (body or "").lower()
    # Longest first, and each matched span is claimed once. The registry deliberately lists
    # overlapping phrasings (`solid bright-neon-yellow safety vest` and the bare
    # `bright-neon-yellow safety vest`) so that either wording is caught, but one vest in the
    # prose is one violation — reporting it twice under two marker names makes the finding
    # count meaningless and reads as two separate problems to fix.
    claimed = []
    for marker in sorted(globals_["hero_agent_lock_markers"], key=len, reverse=True):
        m = re.search(re.escape(marker.lower()), low)
        if not m or negated(low, m.start()):
            continue
        if any(m.start() < end and start < m.end() for start, end in claimed):
            continue
        claimed.append((m.start(), m.end()))
        fnd.add("hero-agent-lock-residue", label,
                f"本项目已选 Named Cast Lock，却出现 Hero Agent Lock 标志写法 "
                f"{marker!r}；两套服装契约互斥，同条并存会让模型把两边特征互串")


def audit_prompt(label, body, cast, globals_, fnd):
    """Every gate for one prompt. Gate 8 runs regardless of whether a person was detected."""
    check_lock_exclusivity(label, body, globals_, fnd)
    if not contains_person(body):
        return False
    check_prompt(label, body, cast, globals_, fnd)
    return True


def judged_kinds_for(cast):
    """Which slot kinds this cast's pipeline mode puts the character into.

    In mode A the character lives only in VIDEO slots and every IMAGE anchor is person-free by
    contract, so judging IMAGE slots would only produce noise — and whether an IMAGE anchor
    wrongly contains a person is already owned by Clean Frame Boundary (`image-clean-frame`,
    which has a real server-side enforcer). In mode B the IMAGE anchors carry the character's
    face and must each restate the identity block, so they are judged too.
    """
    mode = str((cast or {}).get("pipeline_mode") or "A").upper()
    return ("VIDEO",) if mode == "A" else ("VIDEO", "IMAGE")


def audit_prompt_set(prompts, cast, globals_):
    """Audit a whole delivered set. Returns (Findings, stats dict)."""
    kinds = judged_kinds_for(cast)
    fnd = Findings()
    stats = {"total": len(prompts), "judged": 0, "person_free": 0, "out_of_mode": 0}
    for label, body in prompts:
        if not label.startswith(kinds):
            stats["out_of_mode"] += 1
            continue
        if audit_prompt(label, body, cast, globals_, fnd):
            stats["judged"] += 1
        else:
            stats["person_free"] += 1
    return fnd, stats
