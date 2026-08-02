#!/usr/bin/env python3
"""
Create an objective structural summary of a restoration time-lapse video
for reverse-engineering Gemini Omni prompt packs.

Time-lapse adaptation compared to a normal finished-cut analyzer:
- Base review sampling is dense across the whole video (default 2 fps),
  because one second of time-lapse can span hours of real construction.
- A second, low-threshold scene pass detects intra-shot "state jumps"
  (demolition finishing, paint appearing, panels closing) that are not
  edit cuts; each state jump gets a +/-0.5s window densified to a higher
  fps so the operation boundary is captured.
- Cut detection threshold is raised (default 0.3) because time-lapse
  frames naturally differ a lot, which makes a normal threshold over-cut
  a single fixed-camera shot into fragments.
- The first and last seconds are densified too: the before-state and the
  final reveal are the anchor endpoints of the prompt pack.

Outputs:
- video_overview.json
- storyboard/scene_001_start|mid|end.png, contact_sheet.png
- review_frames/review_001.png ... review_N.png, review_contact_sheet_pNN.png
- <video_name>_collage.jpg written next to the SOURCE VIDEO (5-column tile collage)
- change_events[] in video_overview.json, plus an analysis_plan[] naming the exact
  frames that must be sent to semantic review
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


NUL_DEVICE = "NUL" if sys.platform.startswith("win") else "/dev/null"

# Extra directories to probe when ffmpeg/ffprobe are not already on PATH. Covers a
# Homebrew Intel/Apple-silicon install, MacPorts, and the WinGet layout on Windows.
FALLBACK_BINARY_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    r"C:\Users\video\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin",
)


def resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sys.platform.startswith("win") else ""
    for directory in FALLBACK_BINARY_DIRS:
        candidate = Path(directory) / f"{name}{suffix}"
        if candidate.exists():
            return str(candidate)
    raise SystemExit(
        f"Missing required binary: {name}. Install ffmpeg (macOS: brew install ffmpeg) "
        "or put it on PATH."
    )


def run(cmd: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    if cmd and cmd[0] in ("ffmpeg", "ffprobe"):
        cmd[0] = resolve_binary(cmd[0])
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture_output,
        check=True,
    )


def ffprobe_json(video_path: Path) -> dict[str, Any]:
    completed = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
    )
    return json.loads(completed.stdout)


def parse_fps(raw_value: str | None) -> float:
    if not raw_value:
        return 0.0
    if "/" in raw_value:
        numerator, denominator = raw_value.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return 0.0
            return round(float(numerator) / denominator_value, 3)
        except ValueError:
            return 0.0
    try:
        return round(float(raw_value), 3)
    except ValueError:
        return 0.0


def media_metadata(video_path: Path) -> dict[str, Any]:
    payload = ffprobe_json(video_path)
    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    fps = parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    return {
        "duration_sec": round(float(format_info.get("duration", 0.0) or 0.0), 3),
        "width": int(video_stream.get("width", 0) or 0),
        "height": int(video_stream.get("height", 0) or 0),
        "fps": fps,
        "has_audio": audio_stream is not None,
    }


def safe_frame_timestamp(timestamp: float, duration: float, fps: float) -> float:
    if duration <= 0:
        return 0.0

    frame_guard = 0.05
    if fps > 0:
        frame_guard = max(frame_guard, round(1.0 / fps, 3))

    safe_end = max(0.0, duration - frame_guard)
    return round(min(max(timestamp, 0.0), safe_end), 3)


def detect_scored_change_points(video_path: Path, threshold: float) -> list[tuple[float, float]]:
    """Return (timestamp, scene_score) pairs above the threshold.

    The score is what lets change events name a peak frame instead of guessing one, which
    is what the Analysis Peak Inclusion rule needs.
    """
    completed = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-filter:v",
            f"select='gt(scene,{threshold})',metadata=print:file=-",
            "-an",
            "-f",
            "null",
            NUL_DEVICE,
        ],
        capture_output=True,
    )
    combined = "\n".join([completed.stdout or "", completed.stderr or ""])

    scored: list[tuple[float, float]] = []
    pending_time: float | None = None
    for line in combined.splitlines():
        time_match = re.search(r"pts_time:(\d+(?:\.\d+)?)", line)
        if time_match:
            pending_time = round(float(time_match.group(1)), 3)
            continue
        score_match = re.search(r"lavfi\.scene_score=(\d+(?:\.\d+)?)", line)
        if score_match and pending_time is not None:
            scored.append((pending_time, round(float(score_match.group(1)), 6)))
            pending_time = None

    if not scored:
        # metadata=print unavailable or filtered out; fall back to timestamps only.
        for match in re.findall(r"pts_time:(\d+(?:\.\d+)?)", combined):
            scored.append((round(float(match), 3), 0.0))

    deduped: list[tuple[float, float]] = []
    for timestamp, score in scored:
        if deduped and abs(deduped[-1][0] - timestamp) < 0.15:
            if score > deduped[-1][1]:
                deduped[-1] = (deduped[-1][0], score)
            continue
        deduped.append((timestamp, score))
    return deduped


def detect_scene_change_points(video_path: Path, threshold: float) -> list[float]:
    return [timestamp for timestamp, _score in detect_scored_change_points(video_path, threshold)]


def scored_state_change_points(
    video_path: Path,
    state_diff_threshold: float,
    cut_points: list[float],
) -> list[tuple[float, float]]:
    """Low-threshold pass: frame jumps that are state changes, not edit cuts."""
    low_points = detect_scored_change_points(video_path, state_diff_threshold)
    state_points: list[tuple[float, float]] = []
    for point, score in low_points:
        if any(abs(point - cut) < 0.3 for cut in cut_points):
            continue
        if state_points and abs(state_points[-1][0] - point) < 0.3:
            if score > state_points[-1][1]:
                state_points[-1] = (state_points[-1][0], score)
            continue
        state_points.append((point, score))
    return state_points


def build_change_events(
    scored_points: list[tuple[float, float]],
    duration: float,
    event_gap: float = 1.5,
) -> list[dict[str, Any]]:
    """Cluster state jumps into change events with a start, a peak, and an end.

    Every event produced here must end up bound to a beat and named in that beat's VIDEO
    prompt. An event with no beat means the reverse-engineering pass dropped a real visible
    change from the source video.
    """
    if not scored_points:
        return []

    clusters: list[list[tuple[float, float]]] = [[scored_points[0]]]
    for timestamp, score in scored_points[1:]:
        if timestamp - clusters[-1][-1][0] <= event_gap:
            clusters[-1].append((timestamp, score))
        else:
            clusters.append([(timestamp, score)])

    events: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters, start=1):
        times = [point[0] for point in cluster]
        peak_time, peak_score = max(cluster, key=lambda point: point[1])
        start = round(max(0.0, min(times) - 0.25), 3)
        end = round(min(duration, max(times) + 0.25), 3)
        events.append(
            {
                "event_id": f"E{index:02d}",
                "start": start,
                "peak": round(peak_time, 3),
                "end": end,
                "peak_score": peak_score,
                "jump_count": len(cluster),
                "jump_times": [round(value, 3) for value in times],
                # Filled in by the caller once review frame timestamps are known.
                "evidence_frames": [],
                # To be filled by the analyst, not the script:
                "change_type": None,
                "before_state": None,
                "after_state": None,
                "bound_beat_id": None,
            }
        )
    return events


def nearest_frames(
    review_entries: list[dict[str, Any]],
    targets: list[float],
    neighbours: int = 1,
) -> list[str]:
    """Frame filenames closest to each target timestamp, plus adjacent neighbours."""
    if not review_entries:
        return []
    chosen: set[int] = set()
    for target in targets:
        best_index = min(
            range(len(review_entries)),
            key=lambda i: abs(review_entries[i]["timestamp"] - target),
        )
        for offset in range(-neighbours, neighbours + 1):
            candidate = best_index + offset
            if 0 <= candidate < len(review_entries):
                chosen.add(candidate)
    return [Path(review_entries[i]["frame_path"]).name for i in sorted(chosen)]


def build_analysis_plan(
    review_entries: list[dict[str, Any]],
    change_events: list[dict[str, Any]],
    duration: float,
) -> dict[str, Any]:
    """Adaptive dense analysis rate.

    Small clips go to semantic review in full. Longer clips send at least forty percent of
    extracted frames, never fewer than one per second, and always include the first frame,
    the last frame, per-second baselines, and every change event's start / peak / end plus
    the frames either side of each peak.
    """
    total = len(review_entries)
    if total == 0:
        return {"mode": "empty", "required_frames": [], "required_count": 0, "total_frames": 0}

    if total <= 90:
        return {
            "mode": "full",
            "rule": "clip has ninety extracted frames or fewer; send every extracted frame",
            "total_frames": total,
            "required_count": total,
            "required_frames": [Path(entry["frame_path"]).name for entry in review_entries],
        }

    required: set[int] = {0, total - 1}

    # Per-second baseline.
    second = 0.0
    while second <= duration:
        best_index = min(
            range(total),
            key=lambda i: abs(review_entries[i]["timestamp"] - second),
        )
        required.add(best_index)
        second += 1.0

    # Every event's start / peak / end, with the frames either side of the peak.
    name_to_index = {Path(entry["frame_path"]).name: i for i, entry in enumerate(review_entries)}
    for event in change_events:
        for name in nearest_frames(review_entries, [event["start"], event["end"]], neighbours=0):
            required.add(name_to_index[name])
        for name in nearest_frames(review_entries, [event["peak"]], neighbours=1):
            required.add(name_to_index[name])

    # Top up to the forty-percent floor with an even spread over frames not yet chosen.
    floor_count = math.ceil(total * 0.4)
    if len(required) < floor_count:
        remaining = [i for i in range(total) if i not in required]
        needed = floor_count - len(required)
        if remaining and needed > 0:
            step = max(1, len(remaining) // needed)
            for i in remaining[::step][:needed]:
                required.add(i)

    ordered = sorted(required)
    return {
        "mode": "adaptive",
        "rule": (
            "clip exceeds ninety extracted frames; send at least forty percent, never fewer "
            "than one per second, with forced first/last, per-second baselines, and every "
            "change event's start, peak, end and the frames adjacent to each peak"
        ),
        "total_frames": total,
        "required_count": len(ordered),
        "required_fraction": round(len(ordered) / total, 3),
        "required_frames": [Path(review_entries[i]["frame_path"]).name for i in ordered],
    }


def build_keyframe_collage(frame_paths: list[Path], output_path: Path, columns: int = 5) -> Path | None:
    """5-column tiled collage saved next to the source video.

    This is the persistent, high-density visual reference for the whole transition — it is
    what makes it possible to spot a missed work stage at a glance before committing to a
    beat ladder.
    """
    if not frame_paths:
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = math.ceil(len(frame_paths) / columns)
    padded = list(frame_paths)
    # ffmpeg's tile filter needs a full grid; repeat the last frame to fill the tail.
    while len(padded) < rows * columns:
        padded.append(padded[-1])

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for frame_path in padded:
        command.extend(["-i", str(frame_path)])

    # tile takes a SINGLE stream of successive frames, so the inputs are scaled to a
    # uniform size and concatenated into one stream before tiling.
    filter_parts = [
        f"[{index}:v]scale=240:-1:force_divisible_by=2,setsar=1[t{index}]"
        for index in range(len(padded))
    ]
    filter_parts.append(
        "".join(f"[t{index}]" for index in range(len(padded)))
        + f"concat=n={len(padded)}:v=1:a=0[cat]"
    )
    filter_parts.append(
        f"[cat]tile={columns}x{rows}:padding=4:margin=4:color=black[out]"
    )

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            "-update",
            "1",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(output_path),
        ]
    )
    try:
        run(command)
    except subprocess.CalledProcessError:
        return None
    return output_path if output_path.exists() and output_path.stat().st_size > 0 else None


def build_segments(points: list[float], total_duration: float, min_scene_duration: float) -> list[tuple[float, float]]:
    duration = max(0.05, total_duration)
    boundaries = [0.0]
    for point in sorted(points):
        if point <= 0.05 or point >= duration - 0.05:
            continue
        if point - boundaries[-1] < 0.15:
            continue
        boundaries.append(point)
    boundaries.append(duration)

    segments: list[list[float]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start <= 0.05:
            continue
        if not segments:
            segments.append([start, end])
            continue
        if (end - start) < min_scene_duration:
            segments[-1][1] = end
            continue
        if (segments[-1][1] - segments[-1][0]) < min_scene_duration:
            segments[-1][1] = end
            continue
        segments.append([start, end])

    if not segments:
        segments = [[0.0, duration]]

    return [(round(start, 3), round(end, 3)) for start, end in segments]


def evenly_sample_segments(segments: list[tuple[float, float]], max_scenes: int) -> list[tuple[float, float]]:
    if max_scenes <= 0 or len(segments) <= max_scenes:
        return segments
    if max_scenes == 1:
        return [segments[0]]
    selected_indexes = {
        min(len(segments) - 1, round(index * (len(segments) - 1) / (max_scenes - 1)))
        for index in range(max_scenes)
    }
    return [segments[index] for index in sorted(selected_indexes)]


def extract_frame(video_path: Path, output_path: Path, timestamp: float, duration: float, fps: float) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_timestamp = safe_frame_timestamp(timestamp, duration, fps)
    for attempt in range(10):
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{safe_timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
        ]
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            command.extend(["-q:v", "2"])
        command.append(str(output_path))
        try:
            run(command)
        except subprocess.CalledProcessError:
            pass
        if output_path.exists() and output_path.stat().st_size > 0:
            return
        safe_timestamp = max(0.0, safe_timestamp - 0.1)
    # Fallback to start of video
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.000",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
    ]
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        command.extend(["-q:v", "2"])
    command.append(str(output_path))
    run(command)


def build_contact_sheet(frame_paths: list[Path], output_path: Path, columns: int = 3) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not frame_paths:
        raise SystemExit("No frames available for contact sheet.")
    if len(frame_paths) == 1:
        shutil.copy2(frame_paths[0], output_path)
        return

    columns = min(columns, len(frame_paths))
    tile_width = 320
    tile_height = 568

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for frame_path in frame_paths:
        command.extend(["-i", str(frame_path)])

    filter_parts: list[str] = []
    layout_parts: list[str] = []
    for index in range(len(frame_paths)):
        filter_parts.append(
            f"[{index}:v]scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:color=black[v{index}]"
        )
        column = index % columns
        row = index // columns
        layout_parts.append(f"{column * tile_width}_{row * tile_height}")

    filter_parts.append(
        "".join(f"[v{index}]" for index in range(len(frame_paths)))
        + f"xstack=inputs={len(frame_paths)}:layout={'|'.join(layout_parts)}:fill=black[out]"
    )

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            "-update",
            "1",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    run(command)


def build_paged_contact_sheets(frame_paths: list[Path], output_dir: Path, stem: str, columns: int = 6, page_size: int = 48) -> list[Path]:
    pages: list[Path] = []
    for page_index in range(0, len(frame_paths), page_size):
        chunk = frame_paths[page_index : page_index + page_size]
        page_path = output_dir / f"{stem}_p{(page_index // page_size) + 1:02d}.png"
        build_contact_sheet(chunk, page_path, columns=columns)
        pages.append(page_path)
    return pages


def review_timestamps(
    duration: float,
    fps: float,
    base_fps: float,
    dense_fps: float,
    segments: list[tuple[float, float]] | None = None,
    cut_points: list[float] | None = None,
    state_points: list[float] | None = None,
) -> list[float]:
    if duration <= 0:
        return [0.0]

    ts_set = set()

    # 1. Base time-lapse sampling across the full duration (default 2 fps).
    base_step = 1.0 / max(0.5, base_fps)
    steps = int(duration / base_step)
    for i in range(steps + 1):
        ts_set.add(round(i * base_step, 3))

    # 2. Before-state densification: first 2 seconds at 4 fps.
    for i in range(9):
        t = i * 0.25
        if t <= duration:
            ts_set.add(t)

    # 3. Final-reveal densification: last 3 seconds at 4 fps.
    reveal_start = max(0.0, duration - 3.0)
    reveal_steps = int((duration - reveal_start) / 0.25)
    for i in range(reveal_steps + 1):
        t = reveal_start + i * 0.25
        if t <= duration:
            ts_set.add(round(t, 3))

    # 4. Scene start, middle, end timestamps.
    if segments:
        for start, end in segments:
            ts_set.add(start)
            ts_set.add(round(start + ((end - start) / 2), 3))
            ts_set.add(end)

    # 5. Edit cut points +/-0.2s.
    if cut_points:
        for pt in cut_points:
            if 0.0 <= pt - 0.2 <= duration:
                ts_set.add(round(pt - 0.2, 3))
            if 0.0 <= pt + 0.2 <= duration:
                ts_set.add(round(pt + 0.2, 3))

    # 6. Intra-shot state jumps: +/-0.5s window densified at dense_fps.
    if state_points:
        dense_step = 1.0 / max(1.0, dense_fps)
        for pt in state_points:
            window_start = max(0.0, pt - 0.5)
            window_end = min(duration, pt + 0.5)
            t = window_start
            while t <= window_end + 1e-9:
                ts_set.add(round(t, 3))
                t += dense_step

    ts_set.add(duration)

    normalized: list[float] = []
    for timestamp in sorted(list(ts_set)):
        value = safe_frame_timestamp(timestamp, duration, fps)
        if normalized and abs(normalized[-1] - value) < 0.08:
            continue
        normalized.append(value)
    return normalized or [0.0]


def pace_metrics(points: list[float], segments: list[tuple[float, float]], duration: float) -> dict[str, Any]:
    return {
        "scene_count": len(segments),
        "avg_shot_duration": round(duration / max(1, len(segments)), 3),
        "cut_count": len(points),
    }


def analyze_video(
    video_path: Path,
    output_dir: Path,
    scene_threshold: float,
    state_diff_threshold: float,
    min_scene_duration: float,
    max_scenes: int,
    base_fps: float,
    dense_fps: float,
) -> Path:
    metadata = media_metadata(video_path)
    duration = metadata["duration_sec"]
    if duration <= 0:
        raise SystemExit("Could not determine video duration.")

    cut_points = detect_scene_change_points(video_path, scene_threshold)
    scored_state_points = scored_state_change_points(video_path, state_diff_threshold, cut_points)
    state_points = [timestamp for timestamp, _score in scored_state_points]
    change_events = build_change_events(scored_state_points, duration)
    all_segments = build_segments(cut_points, duration, min_scene_duration)
    kept_segments = evenly_sample_segments(all_segments, max_scenes)

    storyboard_dir = output_dir / "storyboard"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    review_dir = output_dir / "review_frames"
    review_dir.mkdir(parents=True, exist_ok=True)

    scene_entries: list[dict[str, Any]] = []
    frame_paths: list[Path] = []
    for index, (start, end) in enumerate(kept_segments, start=1):
        midpoint = round(start + ((end - start) / 2), 3)
        frame_path_start = storyboard_dir / f"scene_{index:03d}_start.png"
        frame_path_mid = storyboard_dir / f"scene_{index:03d}_mid.png"
        frame_path_end = storyboard_dir / f"scene_{index:03d}_end.png"

        extract_frame(video_path, frame_path_start, start, duration, metadata["fps"])
        extract_frame(video_path, frame_path_mid, midpoint, duration, metadata["fps"])
        extract_frame(video_path, frame_path_end, end, duration, metadata["fps"])

        # Keep midpoint as main frame for compatibility
        frame_path = storyboard_dir / f"scene_{index:03d}.png"
        shutil.copy2(frame_path_mid, frame_path)

        frame_paths.append(frame_path)
        scene_entries.append(
            {
                "index": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "midpoint": midpoint,
                "state_jumps_inside": [pt for pt in state_points if start < pt <= end],
                "frame_path": str(frame_path.resolve()),
                "frame_path_start": str(frame_path_start.resolve()),
                "frame_path_mid": str(frame_path_mid.resolve()),
                "frame_path_end": str(frame_path_end.resolve()),
            }
        )

    contact_sheet_path = storyboard_dir / "contact_sheet.png"
    build_contact_sheet(frame_paths, contact_sheet_path)

    review_entries: list[dict[str, Any]] = []
    timestamps = review_timestamps(
        duration,
        metadata["fps"],
        base_fps,
        dense_fps,
        all_segments,
        cut_points,
        state_points,
    )
    for index, timestamp in enumerate(timestamps, start=1):
        frame_path = review_dir / f"review_{index:03d}.png"
        extract_frame(video_path, frame_path, timestamp, duration, metadata["fps"])
        review_entries.append(
            {
                "index": index,
                "timestamp": round(timestamp, 3),
                "frame_path": str(frame_path.resolve()),
            }
        )

    sheet_pages = build_paged_contact_sheets(
        [Path(entry["frame_path"]) for entry in review_entries],
        review_dir,
        "review_contact_sheet",
        columns=6,
    )

    # Attach concrete evidence frames to every change event.
    for event in change_events:
        event["evidence_frames"] = nearest_frames(
            review_entries,
            [event["start"], event["peak"], event["end"]],
            neighbours=0,
        )

    analysis_plan = build_analysis_plan(review_entries, change_events, duration)

    # 5-column keyframe collage, written next to the SOURCE VIDEO, not into the job dir.
    collage_path = build_keyframe_collage(
        [Path(entry["frame_path"]) for entry in review_entries],
        video_path.with_name(f"{video_path.stem}_collage.jpg"),
    )

    payload = {
        "source_video": str(video_path.resolve()),
        "media_metadata": metadata,
        "pace_metrics": pace_metrics(cut_points, all_segments, duration),
        "cut_points": cut_points,
        "state_change_points": state_points,
        "keyframe_collage": str(collage_path.resolve()) if collage_path else None,
        "change_events": change_events,
        "change_event_count": len(change_events),
        "analysis_plan": analysis_plan,
        "review_sampling": {
            "strategy": (
                f"timelapse: base {base_fps}fps, state-jump windows {dense_fps}fps, "
                "cut points +/-0.2s, scene start/mid/end, before-state and final-reveal 4fps"
            ),
            "frame_count": len(review_entries),
            "contact_sheets": [str(page.resolve()) for page in sheet_pages],
            "frames": review_entries,
        },
        "scenes": scene_entries,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    overview_path = output_dir / "video_overview.json"
    overview_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return overview_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize a restoration time-lapse into state-change points, pacing metrics, and dense storyboard frames."
    )
    parser.add_argument("--video", required=True, help="Path to the time-lapse video file.")
    parser.add_argument("--output-dir", required=True, help="Directory for JSON summary and storyboard frames.")
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=0.3,
        help="FFmpeg scene threshold for edit cuts. Raised for time-lapse because frames naturally differ a lot.",
    )
    parser.add_argument(
        "--state-diff-threshold",
        type=float,
        default=0.08,
        help="Lower scene threshold for intra-shot state jumps (work-stage changes inside a fixed camera shot).",
    )
    parser.add_argument("--min-scene-duration", type=float, default=0.2, help="Minimum retained scene duration in seconds.")
    parser.add_argument("--max-scenes", type=int, default=0, help="Maximum storyboard scenes to keep. 0 means unlimited (default).")
    parser.add_argument("--base-fps", type=float, default=2.0, help="Base review sampling density across the full video.")
    parser.add_argument("--dense-fps", type=float, default=6.0, help="Densified sampling inside +/-0.5s windows around state jumps.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    video_path = Path(args.video)
    output_dir = Path(args.output_dir)

    if not video_path.exists():
        raise SystemExit(f"Video not found: {video_path}")
    if args.max_scenes < 0:
        raise SystemExit("--max-scenes must be at least 0 (0 means unlimited).")
    if args.scene_threshold <= 0:
        raise SystemExit("--scene-threshold must be positive.")
    if args.state_diff_threshold <= 0:
        raise SystemExit("--state-diff-threshold must be positive.")
    if args.state_diff_threshold >= args.scene_threshold:
        raise SystemExit("--state-diff-threshold must be lower than --scene-threshold.")
    if args.min_scene_duration <= 0:
        raise SystemExit("--min-scene-duration must be positive.")

    overview_path = analyze_video(
        video_path=video_path,
        output_dir=output_dir,
        scene_threshold=args.scene_threshold,
        state_diff_threshold=args.state_diff_threshold,
        min_scene_duration=args.min_scene_duration,
        max_scenes=args.max_scenes,
        base_fps=args.base_fps,
        dense_fps=args.dense_fps,
    )
    print(overview_path)

    summary = json.loads(overview_path.read_text(encoding="utf-8"))
    collage = summary.get("keyframe_collage")
    plan = summary.get("analysis_plan", {})
    if collage:
        print(f"collage: {collage}")
    else:
        print("collage: FAILED — keyframe collage was not generated; do not proceed to beat mapping")
    print(f"change_events: {summary.get('change_event_count', 0)}")
    print(
        "analysis_plan: "
        f"{plan.get('mode')} — send {plan.get('required_count', 0)} of "
        f"{plan.get('total_frames', 0)} frames to semantic review"
    )


if __name__ == "__main__":
    main()
