#!/usr/bin/env python3
"""Restore the accidentally deleted slot 3 of the 2026-07-27 ice-cave run.

The delete endpoint compacted slots 4..11 into 3..10 and permanently removed
IMG 003.  The original Google FX JPEG was recovered by its logged media UUID.
This script snapshots the current state, expands the compacted files back to
their original slots, and repairs manifest/library metadata.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = "run_1785155679655_蓝冰冰川洞穴改造成隐居雪境卧室"
RUN_DIR = ROOT / "outputs" / RUN_NAME
MANIFEST = RUN_DIR / "manifest.json"
LIBRARY_FILES = (ROOT / "library.json", ROOT / "library.json.bak")
IDEA_ID = "1785155679655"
RECOVERED_UUID = "7d7d299b-a065-44f2-946e-67df0a7c54f4"

RESTORED_PROMPT = (
    "Generate an image of a static tripod shot, ultra-wide 14mm lens feel, "
    "camera height 1.6m, locked eye-level perspective facing the ice cave "
    "entrance portal; left cliff wall, right ice ridge, top sky, and bottom "
    "snow slope anchored; horizon line remains perfectly level at exactly "
    "50-percent height of the frame; all optical flow lines radiate "
    "symmetrically from the optical center of Grid B2. Scene inherits all "
    "landmarks, geometry, and boundary anchors from IMAGE 2. Depict the fully "
    "enclosed exterior wooden vestibule completed around the cave entrance, "
    "with an airtight glass door panel centered in the timber arch, a full "
    "solar panel roof array above, and a completed granite stone walkway "
    "leading through Grid C2. The cracked blue ice boulder remains at Grid C2, "
    "the glacier arch portal remains at Grid B2, and the jagged alpine peak "
    "remains at Grid A2. Persistent traces include clean grey silicone sealant "
    "lines along the porch-ice interface and fresh wet mortar joints between "
    "granite path pavers. Keep material realism, lighting phase: ambient only, "
    "and physical constraints believable; no active workers. Keep the same "
    "camera framing and landmark positions; do not redesign the cavern portal "
    "or move the camera."
)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def snapshot() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = ROOT / ".recovery" / f"ice_cave_slot3_before_restore_{stamp}"
    target.mkdir(parents=True)
    shutil.copy2(MANIFEST, target / "manifest.json")
    shutil.copy2(LIBRARY_FILES[0], target / "library.json")
    shutil.copy2(LIBRARY_FILES[1], target / "library.json.bak")
    shutil.copytree(RUN_DIR / "frames", target / "frames")
    shutil.copytree(RUN_DIR / "videos", target / "videos")
    return target


def move_numbered_files() -> None:
    frames = RUN_DIR / "frames"
    videos = RUN_DIR / "videos"
    fx_src = frames / "fx_src"

    for slot in range(10, 2, -1):
        src = frames / f"img_{slot:03d}.webp"
        dst = frames / f"img_{slot + 1:03d}.webp"
        if not src.exists() or dst.exists():
            raise RuntimeError(f"Unsafe frame move: {src} -> {dst}")
        src.replace(dst)

    fx_files = []
    for path in fx_src.iterdir():
        match = re.match(r"img_(\d{3})_(.+)$", path.name)
        if match and 3 <= int(match.group(1)) <= 10:
            fx_files.append((int(match.group(1)), match.group(2), path))
    for slot, suffix, src in sorted(fx_files, reverse=True):
        dst = fx_src / f"img_{slot + 1:03d}_{suffix}"
        if dst.exists():
            raise RuntimeError(f"Unsafe FX source move: {src} -> {dst}")
        src.replace(dst)

    # Slot 3 was a hard-cut boundary. The original run intentionally had no
    # VID 003; existing VID 003..010 are the compacted original VID 004..011.
    for slot in range(10, 2, -1):
        src = videos / f"vid_{slot:03d}.mp4"
        dst = videos / f"vid_{slot + 1:03d}.mp4"
        if not src.exists() or dst.exists():
            raise RuntimeError(f"Unsafe video move: {src} -> {dst}")
        src.replace(dst)


def install_recovered_image(jpeg: Path) -> None:
    fx_dst = RUN_DIR / "frames" / "fx_src" / f"img_003_{RECOVERED_UUID}.jpg"
    webp_dst = RUN_DIR / "frames" / "img_003.webp"
    shutil.copy2(jpeg, fx_dst)
    with Image.open(jpeg) as image:
        image.convert("RGB").save(webp_dst, format="WEBP", quality=80)


def repair_manifest() -> dict:
    data = load_json(MANIFEST)
    frames = data.get("frames") or []
    videos = data.get("videos") or []
    if len(frames) != 10 or [f.get("sequence") for f in frames] != list(range(1, 11)):
        raise RuntimeError("Manifest is not in the expected post-delete 10-frame state")

    for frame in frames:
        old = int(frame["sequence"])
        if old < 3:
            continue
        new = old + 1
        frame["slot"] = new
        frame["sequence"] = new
        frame_path = RUN_DIR / "frames" / f"img_{new:03d}.webp"
        frame["file"] = rel(frame_path)
        frame["url"] = "/" + rel(frame_path)
        frame.pop("stale_lineage", None)

    frame2_path = RUN_DIR / "frames" / "img_002.webp"
    restored_webp = RUN_DIR / "frames" / "img_003.webp"
    restored_fx = RUN_DIR / "frames" / "fx_src" / f"img_003_{RECOVERED_UUID}.jpg"
    restored = {
        "slot": 3,
        "sequence": 3,
        "file": rel(restored_webp),
        "url": "/" + rel(restored_webp),
        "prompt": RESTORED_PROMPT,
        "meta": "",
        "reference": rel(frame2_path),
        "model": "Nano Banana 2",
        "backend": "google_fx",
        "fx_uuid": RECOVERED_UUID,
        "fx_src": rel(restored_fx),
        "aspect_ratio": data.get("aspect_ratio", "9:16"),
        "image_size": data.get("image_size", "1K"),
        "retry_count": 0,
        "quality_gate": "pending_manual_review",
        "vlm_qa_reason": None,
        "parent_hash": sha256(frame2_path),
        "restored_after_accidental_delete": True,
        "restored_source_sha256": sha256(restored_fx),
    }
    frames.append(restored)
    frames.sort(key=lambda item: item["sequence"])
    data["frames"] = frames

    for video in videos:
        old = int(video["slot"])
        if old >= 3:
            new = old + 1
            video["slot"] = new
            video["start_anchor_slot"] = new
            video_path = RUN_DIR / "videos" / f"vid_{new:03d}.mp4"
            video["file"] = rel(video_path)
            video["url"] = "/" + rel(video_path)
        if old == 2:
            video["anchor_check"] = "restored original IMG 003; anchor mapping recovered"
    videos.sort(key=lambda item: item["slot"])
    for sequence, video in enumerate(videos, 1):
        video["sequence"] = sequence
    data["videos"] = videos
    data.pop("merged_video", None)
    data["slot_restore"] = {
        "restored_slot": 3,
        "source": "google_fx_media_uuid",
        "fx_uuid": RECOVERED_UUID,
        "restored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(MANIFEST, data)
    return data


def rebuild_prompt_block(images: list[dict], videos: list[dict]) -> str:
    lines = ["图片提示词"]
    for item in sorted(images, key=lambda value: value["index"]):
        meta = f" [{item['meta']}]" if item.get("meta") else ""
        lines.extend([f"图片 {item['index']}{meta}:", item.get("body", ""), ""])
    lines.append("视频提示词")
    for item in sorted(videos, key=lambda value: value["index"]):
        meta = f" [{item['meta']}]" if item.get("meta") else ""
        lines.extend([f"视频 {item['index']}{meta}:", item.get("body", ""), ""])
    return "\n".join(lines).strip()


def repair_library(manifest: dict) -> None:
    restored_payload = None
    for path in LIBRARY_FILES:
        library = load_json(path)
        idea = next((item for item in library if str(item.get("id")) == IDEA_ID), None)
        if idea is None:
            raise RuntimeError(f"Idea {IDEA_ID} missing from {path}")

        if restored_payload is None:
            slots = copy.deepcopy(idea.get("prompt_slots") or {})
            images = slots.get("images") or []
            videos = slots.get("videos") or []
            if len(images) != 10:
                raise RuntimeError(f"Unexpected image prompt count in {path}: {len(images)}")
            for item in images:
                if int(item["index"]) >= 3:
                    item["index"] = int(item["index"]) + 1
            images.append({"index": 3, "body": RESTORED_PROMPT, "meta": ""})
            images.sort(key=lambda item: item["index"])
            for item in videos:
                if int(item["index"]) >= 3:
                    item["index"] = int(item["index"]) + 1
            videos.sort(key=lambda item: item["index"])
            restored_payload = {
                "prompt_slots": {"images": images, "videos": videos},
                "prompt_block": rebuild_prompt_block(images, videos),
            }

        idea["prompt_slots"] = copy.deepcopy(restored_payload["prompt_slots"])
        idea["prompt_block"] = restored_payload["prompt_block"]
        idea["image_count"] = 11
        idea["video_count"] = 11
        idea["frameRun"] = copy.deepcopy(manifest)
        write_json(path, library)


def verify() -> None:
    data = load_json(MANIFEST)
    frame_slots = [item["slot"] for item in data["frames"]]
    video_slots = [item["slot"] for item in data["videos"]]
    if frame_slots != list(range(1, 12)):
        raise RuntimeError(f"Frame verification failed: {frame_slots}")
    if video_slots != [1, 2, *range(4, 12)]:
        raise RuntimeError(f"Video verification failed: {video_slots}")
    for item in data["frames"] + data["videos"]:
        if not (ROOT / item["file"]).is_file():
            raise RuntimeError(f"Missing restored media: {item['file']}")
    idea = next(item for item in load_json(LIBRARY_FILES[0]) if str(item.get("id")) == IDEA_ID)
    if len(idea["prompt_slots"]["images"]) != 11:
        raise RuntimeError("Library image slots were not restored")
    if [item["index"] for item in idea["prompt_slots"]["videos"]] != [1, 2, *range(4, 12)]:
        raise RuntimeError("Library video slots were not restored")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jpeg", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        raise SystemExit("Refusing to mutate without --apply")
    if not args.jpeg.is_file():
        raise SystemExit(f"Recovered JPEG not found: {args.jpeg}")

    backup = snapshot()
    move_numbered_files()
    install_recovered_image(args.jpeg)
    manifest = repair_manifest()
    repair_library(manifest)
    verify()
    print(f"Restored slot 3 successfully. Rollback snapshot: {backup}")


if __name__ == "__main__":
    main()
