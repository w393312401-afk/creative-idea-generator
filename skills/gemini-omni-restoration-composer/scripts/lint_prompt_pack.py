#!/usr/bin/env python3
"""Offline slot/word-count checks. No model calls and no application imports."""

import argparse
import json
import re
from pathlib import Path


_FENCE = re.compile(r"```(?:text)?[ \t]*\n(.*?)\n```", re.S)
_BOUNDARY = re.compile(
    r"^(?:(图片|视频|编辑)[ \t]*(\d+)[ \t]*[:：][ \t]*|"
    r"(图片提示词|视频提示词|对话微调提示词)[ \t]*)$", re.M
)


def lint_prompt_pack(text, *, clip_seconds=10, required_edits=0,
                     post_crossing_images=(), three_shot_videos=(),
                     expected_videos=None, single_take=False):
    """Return measurable checks only; callers must review scope and physical semantics.

    Word counts use whitespace-separated words, including the complete body of each slot,
    to match the evaluation convention. Chinese labels and audit tables are excluded.
    Role/override arguments describe the user's request and the planned beat roles; they
    must not be inferred from an output's own claim that it passed.
    """
    if clip_seconds not in (4, 6, 8, 10):
        raise ValueError("clip_seconds must be one of 4, 6, 8, 10")
    if required_edits < 0 or (expected_videos is not None and expected_videos < 0):
        raise ValueError("expected counts cannot be negative")
    post_crossing_images = set(post_crossing_images)
    three_shot_videos = set(three_shot_videos)
    errors = []
    blocks = [b for b in _FENCE.findall(text)
              if re.search(r"^(?:图片|视频|编辑)[ \t]*\d+[ \t]*[:：]", b, re.M)]
    if len(blocks) > 1:
        errors.append("multiple_prompt_blocks: deliver one fenced prompt pack")
    body = blocks[0] if blocks else text
    if "```" in text and not blocks:
        errors.append("prompt_block_not_found: no numbered slots in a text fence")
    matches = list(_BOUNDARY.finditer(body))
    slots = {kind: {} for kind in ("IMAGE", "VIDEO", "EDIT")}
    kinds = {"图片": "IMAGE", "视频": "VIDEO", "编辑": "EDIT"}
    seen_sections = set()
    for index, match in enumerate(matches):
        section = match.group(3)
        if section:
            if section in seen_sections:
                errors.append(f"duplicate_section: {section}")
            seen_sections.add(section)
            continue
        kind = kinds[match.group(1)]
        number = int(match.group(2))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        prompt = body[match.end():end].strip()
        if number in slots[kind]:
            errors.append(f"duplicate_slot: {kind} {number}")
            continue
        if not prompt:
            errors.append(f"empty_slot: {kind} {number}")
        slots[kind][number] = prompt
    for match in re.finditer(r"^(图片|视频|编辑)[ \t]*\d+[ \t]*[:：].+$", body, re.M):
        errors.append(f"slot_label_not_own_line: {match.group(0).strip()}")
    counts = []
    for kind, numbered in slots.items():
        numbers = sorted(numbered)
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            errors.append(f"nonsequential_slots: {kind} {numbers}")
        for number, prompt in sorted(numbered.items()):
            if kind == "IMAGE":
                ceiling = 220 if number in post_crossing_images else 180
            elif kind == "VIDEO":
                ceiling = (400 if single_take or clip_seconds in (4, 6)
                           or number in three_shot_videos else 455)
            else:
                ceiling = 90
            words = len(prompt.split())
            counts.append({"kind": kind, "number": number, "words": words,
                           "ceiling": ceiling, "within_ceiling": words <= ceiling})
            if words > ceiling:
                errors.append(f"word_ceiling: {kind} {number} has {words}, limit {ceiling}")
    images, videos, edits = (len(slots[k]) for k in ("IMAGE", "VIDEO", "EDIT"))
    if not images or not videos:
        errors.append("missing_base_pack: IMAGE and VIDEO slots are required")
    if images != videos + 1:
        errors.append(f"anchor_chain_count: {images} IMAGE, {videos} VIDEO; need VIDEO + 1 IMAGE")
    if expected_videos is not None and videos != expected_videos:
        errors.append(f"video_count: found {videos}, required {expected_videos}")
    if edits != required_edits:
        errors.append(f"edit_count: found {edits}, required {required_edits}")
    if post_crossing_images - slots["IMAGE"].keys():
        errors.append("unknown_post_crossing_image: role argument names a missing IMAGE")
    if three_shot_videos - slots["VIDEO"].keys():
        errors.append("unknown_three_shot_video: role argument names a missing VIDEO")
    return {
        "counts": counts,
        "errors": errors,
        "checks": {"measurable_pass": not errors,
                   "slot_counts": {"IMAGE": images, "VIDEO": videos, "EDIT": edits},
                   "word_count_method": "complete slot body split on whitespace",
                   "coverage_override": "user_single_take" if single_take else "default_multishot"},
        "limits": ["Does not verify shot semantics, action coverage, reference accuracy, "
                   "physical continuity, geometry, adjacent deltas or rendered media quality.",
                   "A measurable pass does not mean the whole prompt pack passed review."],
    }


def _numbers(value):
    try:
        numbers = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated positive slot numbers") from exc
    if any(number < 1 for number in numbers):
        raise argparse.ArgumentTypeError("slot numbers must be positive")
    return numbers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="saved prompt pack or complete response")
    parser.add_argument("--clip-seconds", type=int, choices=(4, 6, 8, 10), default=10)
    parser.add_argument("--required-edits", type=int, default=0)
    parser.add_argument("--post-crossing-images", type=_numbers, default=())
    parser.add_argument("--three-shot-videos", type=_numbers, default=(),
                        help="reward/threshold/atomic stage slots at any clip length")
    parser.add_argument("--expected-videos", type=int)
    parser.add_argument("--single-take", action="store_true", help="explicit user override")
    args = parser.parse_args()
    try:
        result = lint_prompt_pack(
            args.path.read_text(encoding="utf-8"), clip_seconds=args.clip_seconds,
            required_edits=args.required_edits, post_crossing_images=args.post_crossing_images,
            three_shot_videos=args.three_shot_videos, expected_videos=args.expected_videos,
            single_take=args.single_take,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
