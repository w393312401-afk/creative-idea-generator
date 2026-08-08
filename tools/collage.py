"""Shared ffmpeg keyframe-collage builder.

Extracted from the duplicated implementations in
``skills/gemini-omni-restoration-composer/scripts/analyze_timelapse_video.py`` and
``skills/gemini-veo-restoration-composer/video_to_prompt_pipeline.py`` so the main
render pipeline (frame_generator.generate_frame_sequence) can produce a
post-render QA collage without depending on the skills' offline analysis scripts.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

# Extra directories to probe when ffmpeg is not already on PATH. Covers a Homebrew
# Intel/Apple-silicon install, MacPorts, and the WinGet layout on Windows.
FALLBACK_BINARY_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    r"C:\Users\video\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin",
)


def resolve_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sys.platform.startswith("win") else ""
    for directory in FALLBACK_BINARY_DIRS:
        candidate = Path(directory) / f"{name}{suffix}"
        if candidate.exists():
            return str(candidate)
    return None


def build_keyframe_collage(frame_paths: list[Path], output_path: Path, columns: int = 5) -> Path | None:
    """5-column tiled collage saved next to the source frames.

    This is the persistent, high-density visual reference for a whole render — it
    makes it possible to spot a missed work stage or continuity break at a glance.
    Returns None (rather than raising) when ffmpeg is unavailable or the tile
    fails, so callers can treat the collage as best-effort QA, not a hard gate.
    """
    if not frame_paths:
        return None
    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = math.ceil(len(frame_paths) / columns)
    padded = list(frame_paths)
    # ffmpeg's tile filter needs a full grid; repeat the last frame to fill the tail.
    while len(padded) < rows * columns:
        padded.append(padded[-1])

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
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
        subprocess.run(command, capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return None
    return output_path if output_path.exists() and output_path.stat().st_size > 0 else None
