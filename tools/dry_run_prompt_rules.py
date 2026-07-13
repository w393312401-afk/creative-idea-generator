"""Dev tool: exercise prompt_pipeline's mechanical fix_*/check_* rule chain without an LLM.

apply_proactive_fixes() already no-ops its only LLM step (compress_prompt_to_budget) when
config is falsy, and validate_beat_prompts() already gates its only two LLM checks
(check_monotonic_state_regression / check_visible_delta_between_frames) behind
`if config and not skip_llm_checks`. Passing config=None exercises every other fix_*/check_*
call for real — camera_dna dedup, bridge camera-contradiction stripping, anti-jump-cut text
scrubbing, landmark anchor restatement, word-budget/grid/NLVTR checks, stylistic-repetition
diffing — in milliseconds, deterministically, with no API cost.

Usage:
    python tools/dry_run_prompt_rules.py tests/fixtures/prompt_rules/some_fixture.json
    python tools/dry_run_prompt_rules.py tests/fixtures/prompt_rules/   # run every *.json in a dir
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_pipeline import apply_proactive_fixes, validate_beat_prompts


def _unified_diff(before, after, label):
    return list(difflib.unified_diff(
        before.split(), after.split(), lineterm="", n=2,
        fromfile=f"{label} (before)", tofile=f"{label} (after)",
    ))


def dry_run_beat(fixture):
    """Run the no-LLM fix + validate chain over one beat fixture. Returns a plain dict
    (before/after prompts, per-field diffs, and the mechanical validator errors that
    survive after the proactive fixes have run) — no LLM calls, no network, no cache writes.
    """
    i = fixture["i"]
    image_prompt = fixture["image_prompt"]
    video_prompt = fixture["video_prompt"]
    packet = fixture.get("packet", {})
    mode = fixture.get("mode", "standard")
    is_last = fixture.get("is_last", False)
    is_threshold_or_reveal = fixture.get("is_threshold_or_reveal", False)
    beat = fixture.get("beat")
    prev_image = fixture.get("prev_image")
    prev_video = fixture.get("prev_video")

    fixed_video, fixed_image = apply_proactive_fixes(
        i, video_prompt, image_prompt, packet, mode, is_last, is_threshold_or_reveal,
        beat=beat, config=None,
    )

    errors = validate_beat_prompts(
        i, fixed_video, fixed_image, packet, mode, is_last, is_threshold_or_reveal,
        prev_video=prev_video, prev_image=prev_image, config=None, beat=beat,
        skip_llm_checks=True,
    )

    return {
        "before": {"image_prompt": image_prompt, "video_prompt": video_prompt},
        "after": {"image_prompt": fixed_image, "video_prompt": fixed_video},
        "diff": {
            "image": _unified_diff(image_prompt, fixed_image, "IMAGE"),
            "video": _unified_diff(video_prompt, fixed_video, "VIDEO"),
        },
        "mechanical_errors": errors,
    }


def load_fixture(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_fixture_paths(target):
    target = Path(target)
    if target.is_dir():
        return sorted(target.glob("*.json"))
    return [target]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="fixture JSON file, or a directory of fixture JSON files")
    args = parser.parse_args(argv)

    for fixture_path in iter_fixture_paths(args.path):
        fixture = load_fixture(fixture_path)
        result = dry_run_beat(fixture)
        print(f"=== {fixture_path.name} ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
