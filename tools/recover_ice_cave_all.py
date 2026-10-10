#!/usr/bin/env python3
"""Rebuild the ice-cave run after the second accidental slot deletion."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = "run_1785155679655_蓝冰冰川洞穴改造成隐居雪境卧室"
RUN_DIR = ROOT / "outputs" / RUN_NAME
MANIFEST = RUN_DIR / "manifest.json"
IDEA_ID = "1785155679655"
LIBRARY_FILES = (ROOT / "library.json", ROOT / "library.json.bak")
CANONICAL = ROOT / ".recovery" / "ice_cave_slot3_before_restore_20260727_221105"
CURRENT_SNAPSHOT = ROOT / ".recovery" / "ice_cave_before_restore_all_20260727_230921"
RECOVERED_VIDEO_9 = Path("/tmp/video9.mp4")
RECOVERED_UUID_3 = "7d7d299b-a065-44f2-946e-67df0a7c54f4"
RECOVERED_UUID_9 = "98434079-b099-49de-be78-b357cc9bffe7"
RECOVERED_VIDEO_UUID_9 = "953c1017-606c-487b-8ed1-432ae2c8eb9e"

BRIDGE_PROMPT = (
    "Use IMAGE 3 as the exact first-frame anchor and IMAGE 4 as the exact "
    "last-frame anchor. Create one continuous visible doorway transition: "
    "begin outside the completed timber-and-glass ice-cave vestibule, move "
    "steadily forward along the stone path, pass physically through the open "
    "glass doorway and timber portal, then settle inside the untouched blue-ice "
    "chamber. Preserve the entrance, glacier geometry, horizon, scale, and all "
    "landmarks from both anchors; the exterior must remain visibly connected "
    "to the interior throughout the move. No teleport, dissolve, hard cut, "
    "black frame, new layout, construction work, or object morphing. Natural "
    "cold daylight transitions continuously from bright exterior snow to dim "
    "blue cave ambience. Smooth stabilized forward dolly, 14mm lens feel."
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reset_media() -> None:
    frames = RUN_DIR / "frames"
    videos = RUN_DIR / "videos"
    shutil.rmtree(frames)
    shutil.rmtree(videos)
    frames.mkdir(parents=True)
    (frames / "fx_src").mkdir()
    videos.mkdir()

    old_frames = CANONICAL / "frames"
    current_frames = CURRENT_SNAPSHOT / "task" / "frames"
    frame_sources = {
        1: old_frames / "img_001.webp",
        2: old_frames / "img_002.webp",
        3: current_frames / "img_003.webp",
        **{slot: old_frames / f"img_{slot - 1:03d}.webp" for slot in range(4, 12)},
    }
    for slot, source in frame_sources.items():
        shutil.copy2(source, frames / f"img_{slot:03d}.webp")

    old_fx = old_frames / "fx_src"
    current_fx = current_frames / "fx_src"
    fx_sources: dict[int, Path] = {}
    for source in old_fx.iterdir():
        match = re.match(r"img_(\d{3})_(.+)$", source.name)
        if match:
            old_slot = int(match.group(1))
            fx_sources[old_slot if old_slot < 3 else old_slot + 1] = source
    fx_sources[3] = next(current_fx.glob(f"img_003_{RECOVERED_UUID_3}.*"))
    for slot, source in fx_sources.items():
        suffix = source.name.split("_", 2)[2]
        shutil.copy2(source, frames / "fx_src" / f"img_{slot:03d}_{suffix}")

    old_videos = CANONICAL / "videos"
    current_videos = CURRENT_SNAPSHOT / "task" / "videos"
    video_sources = {
        1: old_videos / "vid_001.mp4",
        2: old_videos / "vid_002.mp4",
        3: current_videos / "vid_003.mp4",
        **{slot: old_videos / f"vid_{slot - 1:03d}.mp4" for slot in range(4, 9)},
        9: RECOVERED_VIDEO_9,
        10: old_videos / "vid_009.mp4",
        11: old_videos / "vid_010.mp4",
    }
    for slot, source in video_sources.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, videos / f"vid_{slot:03d}.mp4")


def rebuild_manifest() -> dict:
    old = load(CANONICAL / "manifest.json")
    current = load(CURRENT_SNAPSHOT / "task" / "manifest.json")
    frames = []
    for record in old["frames"]:
        item = copy.deepcopy(record)
        old_slot = int(item["slot"])
        slot = old_slot if old_slot < 3 else old_slot + 1
        item["slot"] = item["sequence"] = slot
        frame_path = RUN_DIR / "frames" / f"img_{slot:03d}.webp"
        item["file"] = rel(frame_path)
        item["url"] = "/" + rel(frame_path)
        if item.get("fx_src"):
            src_name = Path(item["fx_src"]).name
            src_name = re.sub(r"^img_\d{3}_", f"img_{slot:03d}_", src_name)
            item["fx_src"] = rel(RUN_DIR / "frames" / "fx_src" / src_name)
        item.pop("stale_lineage", None)
        frames.append(item)

    restored3 = copy.deepcopy(next(item for item in current["frames"] if item.get("slot") == 3))
    restored3["slot"] = restored3["sequence"] = 3
    restored3["file"] = rel(RUN_DIR / "frames" / "img_003.webp")
    restored3["url"] = "/" + restored3["file"]
    restored3["fx_uuid"] = RECOVERED_UUID_3
    restored3["fx_src"] = rel(next((RUN_DIR / "frames" / "fx_src").glob(f"img_003_{RECOVERED_UUID_3}.*")))
    restored3["restored_after_accidental_delete"] = True
    frames.append(restored3)
    frames.sort(key=lambda item: item["slot"])

    videos = []
    for record in old["videos"]:
        item = copy.deepcopy(record)
        old_slot = int(item["slot"])
        slot = old_slot if old_slot < 3 else old_slot + 1
        item["slot"] = item["sequence"] = slot
        item["start_anchor_slot"] = slot
        item["end_anchor_slot"] = slot + 1 if slot < 11 else None
        video_path = RUN_DIR / "videos" / f"vid_{slot:03d}.mp4"
        item["file"] = rel(video_path)
        item["url"] = "/" + rel(video_path)
        if slot == 9:
            item["fx_uuid"] = RECOVERED_VIDEO_UUID_9
            item["restored_after_accidental_delete"] = True
            item["restored_source_sha256"] = sha256(video_path)
        videos.append(item)

    manual3 = copy.deepcopy(next(item for item in current["videos"] if item.get("slot") == 3))
    manual3.update({
        "slot": 3,
        "sequence": 3,
        "file": rel(RUN_DIR / "videos" / "vid_003.mp4"),
        "url": "/" + rel(RUN_DIR / "videos" / "vid_003.mp4"),
        "prompt": BRIDGE_PROMPT,
        "meta": "BRIDGE",
        "is_hero": False,
        "start_anchor_slot": 3,
        "end_anchor_slot": 4,
        "anchor_check": "restored visible exterior-to-interior doorway bridge",
    })
    videos.append(manual3)
    videos.sort(key=lambda item: item["slot"])

    old["frames"] = frames
    old["videos"] = videos
    old.pop("merged_video", None)
    old["slot_restore"] = {
        "restored_slots": [3, 9, 10, 11],
        "restored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "image_3_fx_uuid": RECOVERED_UUID_3,
        "image_9_fx_uuid": RECOVERED_UUID_9,
        "video_9_fx_uuid": RECOVERED_VIDEO_UUID_9,
        "source": "verified recovery snapshots plus Google FX originals",
    }
    write(MANIFEST, old)
    return old


def prompt_block(images: list[dict], videos: list[dict]) -> str:
    lines = ["图片提示词"]
    for item in images:
        meta = f" [{item['meta']}]" if item.get("meta") else ""
        lines.extend((f"图片 {item['index']}{meta}:", item.get("body", ""), ""))
    lines.append("视频提示词")
    for item in videos:
        meta = f" [{item['meta']}]" if item.get("meta") else ""
        lines.extend((f"视频 {item['index']}{meta}:", item.get("body", ""), ""))
    return "\n".join(lines).strip()


def rebuild_library(manifest: dict) -> None:
    library = load(CANONICAL / "library.json")
    idea = next(item for item in library if str(item.get("id")) == IDEA_ID)
    old_images = copy.deepcopy(idea["prompt_slots"]["images"])
    old_videos = copy.deepcopy(idea["prompt_slots"]["videos"])

    images = []
    for item in old_images:
        old_index = int(item["index"])
        item["index"] = old_index if old_index < 3 else old_index + 1
        if item["index"] == 4 and item.get("meta") == "CUT":
            item["meta"] = ""
        images.append(item)
    frame3 = next(item for item in manifest["frames"] if item["slot"] == 3)
    images.append({"index": 3, "body": frame3["prompt"], "meta": ""})
    images.sort(key=lambda item: item["index"])

    videos = []
    for item in old_videos:
        old_index = int(item["index"])
        item["index"] = old_index if old_index < 3 else old_index + 1
        videos.append(item)
    videos.append({"index": 3, "body": BRIDGE_PROMPT, "meta": "BRIDGE"})
    videos.sort(key=lambda item: item["index"])

    idea["prompt_slots"] = {"images": images, "videos": videos}
    idea["prompt_block"] = prompt_block(images, videos)
    idea["image_count"] = idea["video_count"] = 11
    idea["frameRun"] = copy.deepcopy(manifest)
    for path in LIBRARY_FILES:
        write(path, library)


def merge_videos(manifest: dict) -> None:
    videos = [RUN_DIR / "videos" / f"vid_{slot:03d}.mp4" for slot in range(1, 12)]
    concat = RUN_DIR / ".recovery_concat.txt"
    concat.write_text("".join(f"file '{path.as_posix()}'\n" for path in videos), encoding="utf-8")
    merged = RUN_DIR / "做一个蓝冰冰川洞穴改造成隐居雪境卧室_1x.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(concat), "-c", "copy", str(merged),
    ], check=True)
    concat.unlink()
    manifest["merged_video"] = {
        "status": "success",
        "file": rel(merged),
        "url": "/" + rel(merged),
        "sequence_count": 11,
        "rebuilt_after_restore": True,
    }
    write(MANIFEST, manifest)
    for path in LIBRARY_FILES:
        library = load(path)
        idea = next(item for item in library if str(item.get("id")) == IDEA_ID)
        idea["frameRun"] = copy.deepcopy(manifest)
        write(path, library)


def verify() -> None:
    manifest = load(MANIFEST)
    assert [item["slot"] for item in manifest["frames"]] == list(range(1, 12))
    assert [item["slot"] for item in manifest["videos"]] == list(range(1, 12))
    for slot in range(1, 12):
        assert (RUN_DIR / "frames" / f"img_{slot:03d}.webp").is_file()
        assert (RUN_DIR / "videos" / f"vid_{slot:03d}.mp4").is_file()
    assert next(item for item in manifest["frames"] if item["slot"] == 9)["fx_uuid"] == RECOVERED_UUID_9
    assert next(item for item in manifest["videos"] if item["slot"] == 9)["fx_uuid"] == RECOVERED_VIDEO_UUID_9
    for path in LIBRARY_FILES:
        idea = next(item for item in load(path) if str(item.get("id")) == IDEA_ID)
        assert [item["index"] for item in idea["prompt_slots"]["images"]] == list(range(1, 12))
        assert [item["index"] for item in idea["prompt_slots"]["videos"]] == list(range(1, 12))


def main() -> None:
    reset_media()
    manifest = rebuild_manifest()
    rebuild_library(manifest)
    merge_videos(manifest)
    verify()
    print("Restored the complete 11-image / 11-video ice-cave run.")


if __name__ == "__main__":
    main()
