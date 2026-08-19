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
    max_frames: int = 25,
    tile_width: int = 360,
) -> Path | None:
    """Render a tiled keyframe collage using ffmpeg.

    If `len(frame_paths) > max_frames` and `max_frames > 0`, it intelligently samples
    milestone frames (always preserving first and last frame anchors) to prevent runaway
    collage tile explosion and extreme vertical aspect ratio squishing.

    Pass `max_frames=0` to disable sampling entirely. Callers whose collage carries an
    index contract — "tile k is exactly item k of some parallel list" — MUST do so:
    dropping a frame shifts every later tile by one, which is worse than having no
    collage at all. See `prompt_pipeline.reverse._build_pass_b_sheets`.
    """
    if not frame_paths:
        return None

    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        return None

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in frame_paths]

    # max_frames <= 0 表示"不降采样，全都要"（带下标契约的拼图必须走这条，见
    # prompt_pipeline/reverse.py 的调用点）。max_frames == 1 单独挡掉：下面的
    # step 要除以 max_frames-1，取 1 会直接 ZeroDivisionError；而"只留一张"本就
    # 不是拼图，退回首帧即可。
    if max_frames is not None and max_frames > 0 and len(paths) > max_frames:
        if max_frames == 1:
            paths = paths[:1]
        else:
            n = len(paths)
            indices = [0]
            step = (n - 1) / float(max_frames - 1)
            for i in range(1, max_frames - 1):
                idx = int(round(i * step))
                if idx not in indices and idx < n - 1:
                    indices.append(idx)
            indices.append(n - 1)
            indices = sorted(list(dict.fromkeys(indices)))
            paths = [paths[i] for i in indices]

    rows = math.ceil(len(paths) / columns)
    padded = list(paths)
    while len(padded) < rows * columns:
        padded.append(padded[-1])

    concat_list_path = out.parent / f".tmp_concat_{out.stem}.txt"
    try:
        lines = [f"file '{str(p.resolve()).replace('\'', '\'\\\'\'')}'\n" for p in padded]
        concat_list_path.write_text("".join(lines), encoding="utf-8")

        scale_w = max(120, tile_width)
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
            f"scale={scale_w}:-1:force_divisible_by=2,setsar=1,tile={columns}x{rows}:padding=4:margin=4:color=black",
            "-frames:v",
            "1",
            "-q:v",
            "2",
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

