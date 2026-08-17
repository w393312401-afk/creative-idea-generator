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


def _win_subprocess_flags() -> dict:
    flags: dict = {}
    if sys.platform.startswith("win"):
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags["creationflags"] = subprocess.CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        flags["startupinfo"] = si
    return flags


def build_keyframe_collage(
    frame_paths: list[Path | str],
    output_path: Path | str,
    columns: int = 5,
) -> Path | None:
    """Render a tiled keyframe collage using ffmpeg."""
    if not frame_paths:
        return None

    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        return None

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in frame_paths]
    rows = math.ceil(len(paths) / columns)
    padded = list(paths)
    while len(padded) < rows * columns:
        padded.append(padded[-1])

    concat_list_path = out.parent / f".tmp_concat_{out.stem}.txt"
    try:
        lines = [f"file '{str(p.resolve()).replace('\'', '\'\\\'\'')}'\n" for p in padded]
        concat_list_path.write_text("".join(lines), encoding="utf-8")

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-vf",
            f"scale=240:-1:force_divisible_by=2,setsar=1,tile={columns}x{rows}:padding=4:margin=4:color=black",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(output_path),
        ]
        subprocess.run(command, capture_output=True, check=True, **_win_subprocess_flags())
    except (subprocess.CalledProcessError, OSError):
        return None
    finally:
        if concat_list_path.exists():
            try:
                concat_list_path.unlink()
            except OSError:
                pass
    return output_path if output_path.exists() and output_path.stat().st_size > 0 else None
