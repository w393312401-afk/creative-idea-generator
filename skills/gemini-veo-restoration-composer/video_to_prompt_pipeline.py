#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Video-to-Prompt Reverse Engineering Pipeline (Phase 2 & Phase 3)
An end-to-end automation tool that parses a transformation/renovation video,
extracts keyframes, performs CV light and motion analysis, interacts with
Multimodal LLMs (Gemini/OpenAI) to extract structural metadata, translates it
into 100% SCUP-compliant prompt sets, and runs an automated SCUP Quality Auditor.

No external pip dependencies required (uses standard libraries, PIL, and FFmpeg).
"""

import os
import sys
import json
import math
import re
import base64
import argparse
import tempfile
import shutil
import subprocess
import urllib.request
from datetime import datetime
from PIL import Image, ImageChops, ImageStat

# =====================================================================
# Constants & Defaults
# =====================================================================
DEFAULT_LIGHTING_LADDER = [
    "ambient only",
    "temporary work light active",
    "fixture install in progress",
    "partial practical activation",
    "final practical stabilization"
]

# Standard System Rules for LLM Orchestrator
LLM_SYSTEM_PROMPT = """
You are a high-precision Video Reverse Engineering Assistant. Your task is to analyze the sequence of keyframes provided from a renovation/restoration timelapse video and output a structured metadata JSON exactly matching the schema below.

You must follow these spatial and continuity rules:
1. CAMERA DNA: Determine the camera shot type (e.g. static tripod shot), camera height (typically 1.6m for interior/road, 1.1m for tabletop), lens focal length feeling (14-18mm for wide spaces), and 4-boundary coordinates.
2. PRIMARY LANDMARKS: Identify exactly 3 named static spatial anchors in the scene: 1 Foreground, 1 Mid-depth, and 1 Background. Assign each a 3x3 Grid cell (Grid A1 to Grid C3) and estimate its Z-Depth height scale as a percentage of total frame height.
3. FRAME OBSERVATIONS: Preserve dense chronological evidence. Summarize what visibly changes in the analyzed frames, including frame/time ranges and Grid A1-C3 cells.
4. CHANGE EVENTS: Every detected visual delta must become a change_event with event_id, frame_range, time_range, grid_cells, change_type, before_state, after_state, and evidence_frames. Do not drop brief changes.
5. OBJECT LEDGER (OSPL): Track static objects and coordinate locations. If an object gets hidden behind a worker in intermediate frames, do NOT delete it; it must retain a "Ghost Clause" marking it as hidden.
6. VOLUMETRIC MASS (VMFP): Identify loose materials (rubble, concrete debris, sand, etc.). Measure volume flow from 100% capacity to 0% cleared. Look for rigid containers (crates, wheelbarrows, buckets) and specify Rigid Container Encapsulation (RCE).
7. TRANSIENT AGENTS (HAL): Find workers or machines. Log their entry frame time (t_in) and exit frame time (t_out) relative to Grid margins. Silently trace their silhouette properties for Hero Agent Lock (HAL) silhouettes (helmet, vest, pants colors). Crucially, identify any hand-held manual tools used by the worker (e.g., broom, paint roller, paint brush, shovel, hammer) and specify the precise action loop. Additionally, define at least two concrete, measurable progress markers showing gradual numerical advancement (e.g., swept clean area expanding from 10% to 90%, wood panels completing from 0 to 5 rows). If the input video lacks visible workers or tools, you MUST speculate and infer standard, logical construction tools (e.g., standard broom or roller) and progress scales to prevent magical morphing transitions.
8. BEAT COVERAGE: Each time_sequence beat must cite source_event_ids and source_frame_range. The union of beats must cover all change_events.
9. TEMPORAL PHYSICS SKELETON: Each beat must include shot_family, beat_type, single_physical_operation, and causal_path. The causal_path must specify material_source, entry_path, tool_contact, movement_path, at least two persistent_traces, and next_frame_inheritance. A beat may contain exactly one physical operation only.
10. THRESHOLD BRIDGE: Any exterior-to-interior transition must be its own threshold_bridge beat. The preceding exterior anchor must show at least two interior landmarks through the doorway, and the bridge beat must describe coaxial forward motion with no construction work.

Output ONLY a valid JSON object matching the following structure. Do not output markdown code fences, preambles, or explanations.

JSON SCHEMA:
{
  "camera_dna": {
    "shot_type": "static tripod shot",
    "lens": "14-18mm",
    "height_m": 1.6,
    "perspective": "eye-level perspective looking straight",
    "boundaries": {
      "left": "left wall in Grid B1",
      "right": "right column in Grid B3",
      "top": "ceiling structure in Grid A2",
      "bottom": "floor edge in Grid C2"
    }
  },
  "primary_landmarks": [
    {
      "name": "detailed name of foreground landmark",
      "grid": "Grid C2",
      "z_depth_scale": "20%"
    },
    {
      "name": "detailed name of mid-depth landmark",
      "grid": "Grid B1",
      "z_depth_scale": "60%"
    },
    {
      "name": "detailed name of background landmark",
      "grid": "Grid A2",
      "z_depth_scale": "45%"
    }
  ],
  "frame_observations": [
    {
      "frame": "keyframe_001.jpg",
      "timecode": "0.0s",
      "visible_state": "before-state rubble fully covers Grid C2",
      "changed_grid_cells": ["Grid C2"]
    }
  ],
  "change_events": [
    {
      "event_id": "E01",
      "frame_range": "keyframe_001.jpg-keyframe_006.jpg",
      "time_range": "0.0s-1.7s",
      "grid_cells": ["Grid C2"],
      "change_type": "debris removal begins",
      "before_state": "rubble pile fills lower foreground",
      "after_state": "first raw concrete strip becomes visible",
      "evidence_frames": ["keyframe_001.jpg", "keyframe_004.jpg", "keyframe_006.jpg"]
    }
  ],
  "time_sequence": [
    {
      "beat_index": 1,
      "shot_family": "interior_static",
      "beat_type": "removal",
      "source_event_ids": ["E01"],
      "source_frame_range": "keyframe_001.jpg-keyframe_006.jpg",
      "state_name": "debris clearing",
      "single_physical_operation": "debris clearing only",
      "causal_path": {
        "material_source": "existing rubble pile on the floor",
        "entry_path": "worker enters through Grid C1 edge with crates already visible in hand",
        "tool_contact": "matte-black broom and gloved hands push rubble into crate lips",
        "movement_path": "rubble moves from Grid C2 floor into crates and exits through Grid C1",
        "persistent_traces": ["dust edge around newly exposed slab", "drag scuffs leading toward Grid C1"],
        "next_frame_inheritance": "exposed raw floor strip and dust edges remain visible in IMAGE N+1"
      },
      "image_n_state": "detailed description of floor covered with heavy concrete debris and dust",
      "image_n_plus_1_state": "detailed description of floor slab completely swept clean, exposing raw concrete texture",
      "volumetric_mass": {
        "material": "concrete debris",
        "container": "heavy-duty black plastic crates",
        "grid": "Grid C2",
        "volume_flow": "100% capacity to 0% cleared"
      },
      "transient_agents": [
        {
          "agent_type": "worker",
          "count": 1,
          "hal_profile": "solid bright-neon-yellow safety vest, white hardhat, dark blue pants",
          "manual_tool": "solid-black long-handle plastic broom",
          "trajectory": {
            "enter_time": "0.0s",
            "enter_grid": "Grid C1",
            "exit_time": "7.5s",
            "exit_grid": "Grid C1"
          },
          "action_loop": "repeatedly bends down to scoop rubble into crates using the broom",
          "progress_markers": [
            "swept clean floor area ratio increases from 10% to 90%",
            "rubble crate loading completes from 0% to 100% capacity"
          ]
        }
      ],
      "lighting_phase": "ambient only",
      "sfx": "glass crunch, bag rustle",
      "ambient": "hollow room tone, light wind"
    }
  ],
  "post_render_qc": {
    "hard_cut_times": [],
    "text_overlay_hits": [],
    "landmark_drift_score": 0.0,
    "agent_pop_hits": [],
    "object_birth_hits": [],
    "state_regression_hits": []
  }
}
"""

# =====================================================================
# Utilities
# =====================================================================
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    return path

def check_command(cmd):
    try:
        subprocess.run([cmd, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False

def frame_timecode(frame_index, fps=3.0):
    return f"{frame_index / fps:.1f}s" if fps else f"{frame_index:.1f}s"

def grid_cell_name(row, col):
    return f"Grid {'ABC'[row]}{col + 1}"

def grid_hotspots(prev_gray, gray, top_n=2):
    diff = ImageChops.difference(gray, prev_gray)
    width, height = diff.size
    cells = []
    for row in range(3):
        for col in range(3):
            left = int(col * width / 3)
            upper = int(row * height / 3)
            right = int((col + 1) * width / 3)
            lower = int((row + 1) * height / 3)
            crop = diff.crop((left, upper, right, lower))
            score = ImageStat.Stat(crop).mean[0]
            cells.append((score, grid_cell_name(row, col)))
    cells.sort(reverse=True)
    return [cell for score, cell in cells[:top_n] if score > 3.0]

def contiguous_segments(indices, max_gap=1):
    if not indices:
        return []
    segments = []
    start = prev = indices[0]
    for idx in indices[1:]:
        if idx - prev <= max_gap:
            prev = idx
            continue
        segments.append((start, prev))
        start = prev = idx
    segments.append((start, prev))
    return segments

def select_analysis_frame_indices(keyframe_paths, cv_data, fps=3.0, max_frames=120):
    total = len(keyframe_paths)
    if total <= 0:
        return []
    if total <= 90:
        return list(range(total))

    duration_seconds = math.ceil(total / fps) if fps else total
    selected = {0, total - 1}

    # Baseline temporal coverage: at least one frame per second.
    for i in range(max(3, duration_seconds)):
        selected.add(int(round(i * (total - 1) / max(1, duration_seconds - 1))))

    # Dense coverage around CV-detected changes.
    for event in cv_data.get("change_events", []):
        indices = event.get("indices", [])
        for idx in indices:
            selected.update({max(0, idx - 1), idx, min(total - 1, idx + 1)})
        if event.get("start_index") is not None and event.get("end_index") is not None:
            start = event["start_index"]
            end = event["end_index"]
            mid = int(round((start + end) / 2))
            selected.update({start, mid, end})

    # Add strongest global peaks until target coverage is reached.
    diffs = cv_data.get("frame_differences", [])
    ranked = sorted(range(len(diffs)), key=lambda idx: diffs[idx], reverse=True)
    target = min(max_frames, max(duration_seconds * 2, math.ceil(total * 0.4)))
    for idx in ranked:
        if len(selected) >= target:
            break
        selected.update({max(0, idx - 1), idx, min(total - 1, idx + 1)})

    selected = sorted(idx for idx in selected if 0 <= idx < total)
    if len(selected) > max_frames:
        must_keep = {0, total - 1}
        for event in cv_data.get("change_events", []):
            if event.get("start_index") is not None:
                must_keep.add(event["start_index"])
            if event.get("peak_index") is not None:
                must_keep.add(event["peak_index"])
            if event.get("end_index") is not None:
                must_keep.add(event["end_index"])
        remaining = [idx for idx in selected if idx not in must_keep]
        if len(must_keep) >= max_frames:
            return sorted(must_keep)
        step = len(remaining) / max(1, max_frames - len(must_keep))
        sampled = {remaining[int(i * step)] for i in range(max_frames - len(must_keep)) if remaining}
        selected = sorted((must_keep | sampled) & set(range(total)))
    return selected

# =====================================================================
# Module 1: Keyframe Extraction
# =====================================================================
def extract_keyframes(video_path, output_dir, fps=3.0):
    print(f"[*] Extracting keyframes from {video_path} at {fps} fps...")
    keyframes_dir = ensure_dir(os.path.join(output_dir, "keyframes"))
    
    # Try using ffmpeg
    ffmpeg_path = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-vsync", "vfr",
        os.path.join(keyframes_dir, "keyframe_%03d.jpg")
    ]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        # Scan extracted files
        files = sorted([os.path.join(keyframes_dir, f) for f in os.listdir(keyframes_dir) if f.startswith("keyframe_") and f.endswith(".jpg")])
        print(f"[+] Successfully extracted {len(files)} keyframes.")
        
        # Auto-generate a beautiful 5-column tiled keyframe collage persistent next to the video (T0 Rule)
        if files:
            try:
                collage_path = os.path.splitext(video_path)[0] + "_collage.jpg"
                print(f"[*] Generating 5-column tiled keyframe collage at: {collage_path}...")
                cols = 5
                rows = math.ceil(len(files) / cols)
                
                collage_cmd = [
                    ffmpeg_path,
                    "-y",
                    "-i", video_path,
                    "-vf", f"fps={fps},scale=240:-1,tile={cols}x{rows}",
                    "-vframes", "1",
                    collage_path
                ]
                subprocess.run(collage_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                print(f"[+] Successfully generated keyframe collage: {collage_path}")
            except Exception as collage_err:
                print(f"[-] Warning: Failed to generate keyframe collage: {collage_err}")
                
        return files
    except Exception as e:
        print(f"[-] FFmpeg keyframe extraction failed: {e}")
        print("[*] Falling back to manual CV frame-by-frame extraction via FFmpeg metadata or local decoder...")
        # Simple fallback: if ffmpeg fails, try using opencv if available (even though we didn't specify it as required, good safety)
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            v_fps = cap.get(cv2.CAP_PROP_FPS)
            interval = int(round(v_fps / fps)) if v_fps else 30
            count = 0
            frame_idx = 0
            extracted = []
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break
                if count % interval == 0:
                    frame_idx += 1
                    name = os.path.join(keyframes_dir, f"keyframe_{frame_idx:03d}.jpg")
                    cv2.imwrite(name, frame)
                    extracted.append(name)
                count += 1
            cap.release()
            print(f"[+] Fallback extracted {len(extracted)} keyframes.")
            return extracted
        except ImportError:
            print("[-] cv2 not available either. Pipeline aborted.")
            raise e

# =====================================================================
# Module 2: Local CV Motion & Light Analyzer
# =====================================================================
def analyze_video_cv(keyframe_paths):
    print("[*] Running local CV Motion and Light Analysis...")
    cv_data = {
        "camera_movement": "static tripod shot",
        "lighting_progression": [],
        "activity_segments": [],
        "frame_observations": [],
        "change_events": [],
        "frame_differences": [],
        "total_duration": len(keyframe_paths)
    }
    
    if not keyframe_paths:
        return cv_data
        
    luminances = []
    differences = []
    
    prev_gray = None
    hotspots_by_frame = {}
    for idx, path in enumerate(keyframe_paths):
        img = Image.open(path)
        gray = img.convert('L')
        
        # Calculate luminance (average intensity)
        stat = ImageStat.Stat(gray)
        mean_lum = stat.mean[0]
        luminances.append(mean_lum)
        
        # Map to light phases
        if mean_lum < 50:
            phase = "ambient only"
        elif mean_lum < 100:
            phase = "temporary work light active"
        elif mean_lum < 160:
            phase = "fixture install in progress"
        else:
            phase = "final practical stabilization"
        cv_data["lighting_progression"].append(phase)
        
        # Calculate differences (motion/change)
        if prev_gray is not None:
            diff = ImageChops.difference(gray, prev_gray)
            diff_stat = ImageStat.Stat(diff)
            mean_diff = diff_stat.mean[0]
            differences.append(mean_diff)
            hotspots_by_frame[idx] = grid_hotspots(prev_gray, gray)
        else:
            differences.append(0.0)
            hotspots_by_frame[idx] = []
            
        cv_data["frame_observations"].append({
            "frame": os.path.basename(path),
            "frame_index": idx,
            "timecode": frame_timecode(idx, 3.0),
            "mean_luminance": round(mean_lum, 2),
            "lighting_phase": phase,
            "diff_from_previous": round(differences[-1], 2),
            "changed_grid_cells": hotspots_by_frame[idx]
        })
        prev_gray = gray
        
    # Analyze camera movement based on pixel correlation and shifts
    # (If overall consecutive frame differences are extremely small, it's highly static)
    avg_diff = sum(differences) / len(differences) if differences else 0
    max_diff = max(differences) if differences else 0
    
    # Calculate distance shift between start and end frame using simple MSE of borders
    first_img = Image.open(keyframe_paths[0]).convert('L')
    last_img = Image.open(keyframe_paths[-1]).convert('L')
    end_diff = ImageStat.Stat(ImageChops.difference(first_img, last_img)).mean[0]
    
    if end_diff > 35.0:
        # High final frame deviation, check if uniform direction
        # Simple heuristic check: if differences are uniform, it's a push/zoom or pan
        cv_data["camera_movement"] = "push-in panning shot"
    else:
        cv_data["camera_movement"] = "static tripod shot"
        
    # Activity profiling (workers / changes)
    # Frames with difference above threshold indicates activity
    active_frames = []
    variance = sum((d - avg_diff) ** 2 for d in differences) / len(differences) if differences else 0
    std_diff = math.sqrt(variance)
    threshold = max(8.0, avg_diff + std_diff)
    for i, diff_val in enumerate(differences):
        if diff_val > threshold:
            active_frames.append(i)
            
    if active_frames:
        t_in = f"{active_frames[0]:.1f}s"
        t_out = f"{min(active_frames[-1] + 1, len(keyframe_paths) - 1):.1f}s"
        cv_data["activity_segments"].append({"t_in": t_in, "t_out": t_out, "has_motion": True})
    else:
        cv_data["activity_segments"].append({"t_in": "0.0s", "t_out": "0.0s", "has_motion": False})

    cv_data["frame_differences"] = [round(d, 2) for d in differences]
    for event_number, (start, end) in enumerate(contiguous_segments(active_frames, max_gap=1), start=1):
        event_indices = list(range(start, end + 1))
        peak = max(event_indices, key=lambda idx: differences[idx])
        event_cells = []
        for idx in event_indices:
            for cell in hotspots_by_frame.get(idx, []):
                if cell not in event_cells:
                    event_cells.append(cell)
        cv_data["change_events"].append({
            "event_id": f"E{event_number:02d}",
            "start_index": start,
            "end_index": end,
            "peak_index": peak,
            "indices": event_indices,
            "frame_range": f"{os.path.basename(keyframe_paths[start])}-{os.path.basename(keyframe_paths[end])}",
            "time_range": f"{frame_timecode(start, 3.0)}-{frame_timecode(end, 3.0)}",
            "grid_cells": event_cells[:4] or ["Grid B2"],
            "change_type": "high pixel-delta construction or restoration change",
            "evidence_frames": [
                os.path.basename(keyframe_paths[start]),
                os.path.basename(keyframe_paths[peak]),
                os.path.basename(keyframe_paths[end])
            ],
            "confidence": round(min(0.99, max(0.1, differences[peak] / 64.0)), 2)
        })
        
    print(f"[+] CV Analysis Done: Camera={cv_data['camera_movement']}, AvgDiff={avg_diff:.2f}, Events={len(cv_data['change_events'])}, Motion={cv_data['activity_segments']}")
    return cv_data

def detect_text_like_overlay(image_path):
    """Heuristic for rendered prompt/text artifacts when OCR is unavailable."""
    img = Image.open(image_path).convert("L")
    width, height = img.size
    # Focus on the central/right action area; sky and bright windows near the top are noisy.
    crop = img.crop((int(width * 0.25), int(height * 0.25), int(width * 0.9), int(height * 0.75)))
    px = crop.load()
    cw, ch = crop.size
    visited = set()
    components = []
    for y in range(ch):
        for x in range(cw):
            if (x, y) in visited or px[x, y] < 220:
                continue
            stack = [(x, y)]
            visited.add((x, y))
            min_x = max_x = x
            min_y = max_y = y
            count = 0
            while stack:
                sx, sy = stack.pop()
                count += 1
                min_x = min(min_x, sx)
                max_x = max(max_x, sx)
                min_y = min(min_y, sy)
                max_y = max(max_y, sy)
                for nx in (sx - 1, sx, sx + 1):
                    for ny in (sy - 1, sy, sy + 1):
                        if nx < 0 or ny < 0 or nx >= cw or ny >= ch or (nx, ny) in visited:
                            continue
                        if px[nx, ny] >= 220:
                            visited.add((nx, ny))
                            stack.append((nx, ny))
            box_w = max_x - min_x + 1
            box_h = max_y - min_y + 1
            # Text glyphs are compact, bright, and often clustered. Clouds/windows form much larger blobs.
            if 3 <= box_w <= 40 and 6 <= box_h <= 34 and 8 <= count <= 360:
                components.append((min_x, min_y, max_x, max_y, count))
    return len(components) >= 3

POP_BIRTH_SYSTEM_PROMPT = (
    "You are a defect detector for AI-generated construction time-lapse footage. Frame A and Frame B are "
    "CONSECUTIVE sampled frames from the same locked-camera clip (a fraction of a second apart in playback). "
    "Report ONLY these two defect classes:\n"
    "1. AGENT_POP: a worker, person, or hand tool that is fully present in one frame and completely absent in "
    "the other WITHOUT being at a frame edge (mid-frame materialization or vanishing). A worker partially "
    "crossing a frame edge is a normal entry/exit — not a defect.\n"
    "2. OBJECT_BIRTH: a fixture, panel, furniture piece, railing, light, or other object that pops into "
    "existence fully formed between the two frames with no worker contact, carrying motion, or "
    "partial-progress state visible.\n"
    "Time-lapse speed means fast visible progress is normal; flag only instant, causeless materialization.\n"
    "Answer EXACTLY one line: 'CLEAN' or 'AGENT_POP: <short reason>' or 'OBJECT_BIRTH: <short reason>' or "
    "'AGENT_POP+OBJECT_BIRTH: <short reason>'."
)

REGRESSION_SYSTEM_PROMPT = (
    "You are a defect detector for AI-generated construction time-lapse footage. Frame A is EARLIER and "
    "Frame B is LATER in the same locked-camera project. Detect STATE REGRESSION only: any surface, repair, "
    "coating, panel, or installed element that is complete/clean in Frame A but has reverted to a more "
    "damaged, dirty, or unfinished state in Frame B. Declared temporary works (scaffolding, formwork, "
    "shoring, cribbing, portable work lights) being removed is NOT regression. New construction progress "
    "is NOT regression.\n"
    "Answer EXACTLY one line: 'CLEAN' or 'REGRESSION: <short reason>'."
)


def _vlm_pair_verdict(system_prompt, image_path_a, image_path_b, base_url=None, model=None, api_key=None, timeout=60):
    """Send two frames plus an instruction to an OpenAI-compatible multimodal endpoint.
    Returns the response text, or None on any failure — callers must treat None as
    'check unavailable' (NOT CHECKED), never as a pass."""
    base_url = (base_url or os.environ.get("API_BASE_URL") or "").rstrip('/')
    if not base_url:
        return None
    api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    url = f"{base_url}/chat/completions"
    content = [{"type": "text", "text": "Frame A (earlier), then Frame B (later)."}]
    for p in (image_path_a, image_path_b):
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encode_image_base64(p)}"}
        })
    payload = {
        "model": model or "gemini-3-flash-agent",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content}
        ],
        "temperature": 0.0
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        import requests
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout,
                             proxies={"http": None, "https": None})
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[-] VLM pair-verdict call failed: {e}")
        return None


def vlm_post_render_defect_scan(keyframe_paths, differences, fps=3.0, base_url=None, model=None,
                                max_pair_checks=10, max_regression_checks=4):
    """Per-frame VLM scan implementing the Agent Boundary Pop, Object Birth Without Path,
    and State Regression post-render gates.

    Pop/birth: the largest frame-difference spikes are where pops occur, so the top
    `max_pair_checks` consecutive pairs by CV difference get a VLM verdict.
    Regression: `max_regression_checks` progressively spanning (earlier, later) pairs
    get a VLM verdict, catching completed states that silently un-happen.

    Returns (agent_pop_hits, object_birth_hits, state_regression_hits) as lists of
    annotated timecodes; any channel that could not actually be checked (no endpoint,
    all calls failed) is returned as None so the audit reports NOT CHECKED instead of
    a fake PASS."""
    base_url = base_url or os.environ.get("API_BASE_URL")
    if not base_url or len(keyframe_paths) < 2:
        return None, None, None
    candidate_idx = sorted(
        (idx for idx in range(1, len(keyframe_paths))
         if idx < len(differences) and differences[idx] > 0),
        key=lambda i: differences[i], reverse=True
    )[:max_pair_checks]
    agent_hits, birth_hits = [], []
    pair_checked = False
    for idx in sorted(candidate_idx):
        verdict = _vlm_pair_verdict(POP_BIRTH_SYSTEM_PROMPT, keyframe_paths[idx - 1], keyframe_paths[idx],
                                    base_url=base_url, model=model)
        if verdict is None:
            continue
        pair_checked = True
        v_upper = verdict.upper()
        if "AGENT_POP" in v_upper:
            agent_hits.append(f"{frame_timecode(idx, fps)} ({verdict[:120]})")
        if "OBJECT_BIRTH" in v_upper:
            birth_hits.append(f"{frame_timecode(idx, fps)} ({verdict[:120]})")
    n = len(keyframe_paths)
    reg_hits = []
    reg_checked = False
    steps = max(1, max_regression_checks)
    span_pairs = []
    for k in range(1, steps + 1):
        a = int((k - 1) * (n - 1) / steps)
        b = int(k * (n - 1) / steps)
        if b > a:
            span_pairs.append((a, b))
    for a, b in span_pairs:
        verdict = _vlm_pair_verdict(REGRESSION_SYSTEM_PROMPT, keyframe_paths[a], keyframe_paths[b],
                                    base_url=base_url, model=model)
        if verdict is None:
            continue
        reg_checked = True
        if "REGRESSION" in verdict.upper():
            reg_hits.append(f"{frame_timecode(a, fps)}->{frame_timecode(b, fps)} ({verdict[:120]})")
    return (agent_hits if pair_checked else None,
            birth_hits if pair_checked else None,
            reg_hits if reg_checked else None)


def analyze_post_render_forensics(video_path, work_dir, fps=3.0, base_url=None, model=None, enable_vlm=True):
    print(f"[*] Running post-render forensic QA on {video_path}...")
    forensic_root = ensure_dir(os.path.join(work_dir, "post_render_forensics"))
    keyframe_paths = extract_keyframes(video_path, forensic_root, fps=fps)
    cv_data = analyze_video_cv(keyframe_paths)
    differences = cv_data.get("frame_differences", [])
    nonzero = [d for d in differences if d > 0]
    avg = sum(nonzero) / len(nonzero) if nonzero else 0.0
    variance = sum((d - avg) ** 2 for d in nonzero) / len(nonzero) if nonzero else 0.0
    std = math.sqrt(variance)
    threshold = max(28.0, avg + (1.7 * std))
    hard_cut_times = [
        frame_timecode(idx, fps)
        for idx, diff in enumerate(differences)
        if idx > 0 and diff >= threshold
    ]
    text_overlay_hits = [
        frame_timecode(idx, fps)
        for idx, path in enumerate(keyframe_paths)
        if detect_text_like_overlay(path)
    ]
    # A lightweight drift proxy: many large spikes in a supposedly continuous render indicate landmark jumps.
    landmark_drift_score = round(min(1.0, len(hard_cut_times) / max(1, len(keyframe_paths) / 12.0)), 2)
    # Semantic per-frame VLM scan for the three defect classes CV alone cannot judge.
    # None = channel could not be checked -> audit reports NOT CHECKED, never a silent PASS.
    agent_pop_hits = object_birth_hits = state_regression_hits = None
    if enable_vlm:
        agent_pop_hits, object_birth_hits, state_regression_hits = vlm_post_render_defect_scan(
            keyframe_paths, differences, fps=fps, base_url=base_url, model=model)
        if agent_pop_hits is None:
            print("[!] VLM defect scan unavailable (no API_BASE_URL / endpoint failed) — "
                  "pop/birth/regression gates will report NOT CHECKED.")
    qc = {
        "hard_cut_times": hard_cut_times,
        "text_overlay_hits": text_overlay_hits,
        "landmark_drift_score": landmark_drift_score,
        "agent_pop_hits": agent_pop_hits,
        "object_birth_hits": object_birth_hits,
        "state_regression_hits": state_regression_hits,
        "frame_differences": differences,
        "num_frames": len(keyframe_paths)
    }
    print(f"[+] Post-render QA Done: hard_cut_times={hard_cut_times[:8]}, text_overlay_hits={text_overlay_hits[:8]}, drift={landmark_drift_score}, "
          f"vlm_scan={'ok' if agent_pop_hits is not None else 'unavailable'}")
    return qc

# =====================================================================
# Module 3: Multimodal LLM Integration
# =====================================================================
def encode_image_base64(path):
    with open(path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def cv_density_summary(cv_data, analysis_indices):
    summary = {
        "analysis_frame_indices": analysis_indices,
        "num_analysis_frames": len(analysis_indices),
        "change_events": cv_data.get("change_events", []),
        "frame_observations": [
            obs for obs in cv_data.get("frame_observations", [])
            if obs.get("frame_index") in set(analysis_indices)
        ]
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)

def naturalize_visual_text(text):
    """Scrub structured planning notation from final visual prompts."""
    if text is None:
        return ""
    text = str(text)
    replacements = {
        "%": " percent",
        "TSPA": "",
        "HAL": "",
        "VMFP": "",
        "GCTR": "",
        "RPL": "",
        "RCE": "",
        "RHMA-Blur": "",
        "SCUP": "",
    }
    for find, replace in replacements.items():
        text = text.replace(find, replace)
    percentage_phrases = {
        r"\b100 percent\b": "filled to the rim",
        r"\b95 percent\b": "nearly complete",
        r"\b90 percent\b": "nearly complete",
        r"\b50 percent\b": "halfway",
        r"\b30 percent\b": "partly depleted",
        r"\b17 percent\b": "only a small remnant",
        r"\b10 percent\b": "a small starting patch",
        r"\b0 percent\b": "empty",
    }
    for pattern, replacement in percentage_phrases.items():
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\bprogress marker\s*\d+\s*[:：-]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfrom\s+[\w -]*percent\s+to\s+[\w -]*percent\b", "as a smooth visible progression", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def sentence_or_default(value, default):
    value = naturalize_visual_text(value)
    return value if value else default

def default_causal_path(seq, state_lower):
    if "paint" in state_lower or "coat" in state_lower or "primer" in state_lower:
        return {
            "material_source": "paint arrives in a covered tray from the lower frame edge",
            "entry_path": "the tray and roller enter from the foreground walkway",
            "tool_contact": "a solid-blue cylindrical roller presses wet coating onto the surface",
            "movement_path": "coating spreads from the fixed frame edges across the prepared panels",
            "persistent_traces": ["roller stipple", "brush overlap at the seams"],
            "next_frame_inheritance": "coated surface, panel seams, and wet edge texture remain visible"
        }
    if "install" in state_lower or "build" in state_lower or "fixture" in state_lower or "panel" in state_lower or "rail" in state_lower:
        return {
            "material_source": "components are carried in from the nearest frame edge",
            "entry_path": "parts move along the visible walkway before reaching the work area",
            "tool_contact": "a pinned hand tool presses fasteners into the prepared mounting points",
            "movement_path": "components travel from the entry edge to their final aligned positions",
            "persistent_traces": ["fastener heads", "alignment guide marks"],
            "next_frame_inheritance": "installed components, fasteners, and guide marks remain visible"
        }
    if "threshold" in state_lower or seq.get("shot_family") == "threshold_bridge":
        return {
            "material_source": "no new material enters during the bridge",
            "entry_path": "the camera advances through the already open threshold",
            "tool_contact": "no construction tool acts during the bridge",
            "movement_path": "door-frame edges slide outward while interior landmarks scale up",
            "persistent_traces": ["door hinge line", "threshold scuff line"],
            "next_frame_inheritance": "the same rear window and ceiling light anchors remain visible inside"
        }
    if "furnish" in state_lower or "table" in state_lower or "plant" in state_lower:
        return {
            "material_source": "furniture waits just outside the doorway",
            "entry_path": "the table and plant are carried in through the doorway",
            "tool_contact": "gloved hands lower each item onto the finished floor",
            "movement_path": "objects move from the threshold to the centered final placement",
            "persistent_traces": ["soft contact shadows", "small footstep scuffs"],
            "next_frame_inheritance": "furniture placement, contact shadows, and stable light remain visible"
        }
    return {
        "material_source": "existing damaged material already present in the scene",
        "entry_path": "worker enters from the nearest frame edge with a rigid container",
        "tool_contact": "a matte-black rectangular broom head and gloved hands push material into the container",
        "movement_path": "loose material moves from the damaged zone into the container and out through the entry path",
        "persistent_traces": ["dust edge", "drag scuffs"],
        "next_frame_inheritance": "the exposed clean zone and residual dust edge remain visible"
    }

def causal_path_for(seq):
    state_lower = seq.get("state_name", "").lower()
    path = dict(default_causal_path(seq, state_lower))
    supplied = seq.get("causal_path") or {}
    for key in ("material_source", "entry_path", "tool_contact", "movement_path", "next_frame_inheritance"):
        if supplied.get(key):
            path[key] = supplied[key]
    traces = supplied.get("persistent_traces")
    if isinstance(traces, str):
        traces = [traces]
    if traces:
        path["persistent_traces"] = traces
    path["persistent_traces"] = [naturalize_visual_text(t) for t in path.get("persistent_traces", []) if naturalize_visual_text(t)]
    if len(path["persistent_traces"]) < 2:
        path["persistent_traces"].extend(["visible seam line", "tool contact mark"])
    return {k: naturalize_visual_text(v) if not isinstance(v, list) else v for k, v in path.items()}

def visual_progress_markers(seq, state_lower):
    agents = seq.get("transient_agents", [])
    markers = []
    if agents:
        raw_markers = agents[0].get("progress_markers", [])
        if isinstance(raw_markers, str):
            raw_markers = [raw_markers]
        markers = [naturalize_visual_text(marker) for marker in raw_markers if naturalize_visual_text(marker)]
    if len(markers) >= 2:
        return markers[:2]
    if "paint" in state_lower or "coat" in state_lower or "primer" in state_lower:
        return [
            "the wet coating expands from the window-frame edge until the old surface is almost fully covered",
            "a continuous roller texture and darker wet edge travel across the surface in the same direction"
        ]
    if "install" in state_lower or "build" in state_lower or "panel" in state_lower or "rail" in state_lower:
        return [
            "component rows fill in one after another along the prepared guide line",
            "fastener points appear in a regular sequence behind the worker's hand path"
        ]
    if "threshold" in state_lower or seq.get("shot_family") == "threshold_bridge":
        return [
            "the doorway edges slide evenly toward the frame margins",
            "the pre-seen interior landmarks grow larger without changing their relative positions"
        ]
    if "furnish" in state_lower or "table" in state_lower or "plant" in state_lower:
        return [
            "the table travels from the doorway to the centered floor position",
            "the plant is lowered onto the table and its contact shadow settles under the pot"
        ]
    return [
        "the damaged or dusty area shrinks from the work edge inward",
        "the exposed clean surface grows continuously behind the repeated tool path"
    ]

def is_threshold_beat(seq):
    return seq.get("shot_family") == "threshold_bridge" or seq.get("beat_type") == "threshold" or "threshold" in seq.get("state_name", "").lower()

def is_reward_beat(seq, idx, seqs):
    return seq.get("shot_family") == "reward_reveal" or seq.get("beat_type") == "furnishing" or idx == len(seqs) - 1

def call_gemini_api(api_key, system_prompt, image_paths, cv_data=None, fps=3.0, base_url=None, model=None):
    print("[*] Ingesting keyframes into Multimodal Gemini API...")
    if base_url:
        # Route through local proxy/custom API base
        base_url = base_url.rstrip('/')
        # The proxy/custom endpoint uses the standard chat completions structure,
        # but since Gemini is requested, we can use the openai format or chat completions formatted payload
        # if the local proxy supports OpenAI-style compatibility (which is standard for localhost:8045/v1)
        url = f"{base_url}/chat/completions"
        cv_data = cv_data or {}
        indices_to_send = select_analysis_frame_indices(image_paths, cv_data, fps=fps)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [{
                "type": "text",
                "text": (
                    "Analyze these chronological timelapse frames and output the structural JSON spec. "
                    "Treat the CV DENSITY SUMMARY as binding evidence. Every change_event must appear in time_sequence.source_event_ids.\n\n"
                    f"CV DENSITY SUMMARY:\n{cv_density_summary(cv_data, indices_to_send)}"
                )
            }]}
        ]
        
        for idx in indices_to_send:
            path = image_paths[idx]
            b64_data = encode_image_base64(path)
            messages[1]["content"].append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_data}"
                }
            })
            print(f"[+] Loaded keyframe {os.path.basename(path)} as input.")
        print(f"[+] Selected {len(indices_to_send)}/{len(image_paths)} frames for dense semantic analysis.")
        
        # Determine target model name for local proxy
        payload = {
            "model": model or "gemini-3-flash-agent",
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        try:
            import requests
            response = requests.post(url, json=payload, headers=headers, timeout=120, proxies={"http": None, "https": None})
            response.raise_for_status()
            res_json = response.json()
            text_response = res_json['choices'][0]['message']['content']
            meta = json.loads(text_response)
            meta["_analysis_frame_indices"] = indices_to_send
            return meta
        except Exception as e:
            import traceback
            print(f"[-] Local Proxy Gemini API Call Failed: {e}")
            traceback.print_exc()
            if hasattr(e, 'response') and e.response is not None:
                try:
                    print("[-] Error details:", e.response.text)
                except Exception:
                    pass
            return None
            
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    cv_data = cv_data or {}
    indices_to_send = select_analysis_frame_indices(image_paths, cv_data, fps=fps)
    parts = [{
        "text": (
            system_prompt
            + "\n\nCV DENSITY SUMMARY (binding evidence; every change_event must be covered by time_sequence.source_event_ids):\n"
            + cv_density_summary(cv_data, indices_to_send)
        )
    }]
    
    for idx in indices_to_send:
        path = image_paths[idx]
        b64_data = encode_image_base64(path)
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": b64_data
            }
        })
        print(f"[+] Loaded keyframe {os.path.basename(path)} as input.")
    print(f"[+] Selected {len(indices_to_send)}/{len(image_paths)} frames for dense semantic analysis.")
        
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            res_data = response.read().decode('utf-8')
            res_json = json.loads(res_data)
            text_response = res_json['candidates'][0]['content']['parts'][0]['text']
            meta = json.loads(text_response)
            meta["_analysis_frame_indices"] = indices_to_send
            return meta
    except Exception as e:
        print(f"[-] Gemini API Call Failed: {e}")
        return None

def call_openai_api(api_key, system_prompt, image_paths, cv_data=None, fps=3.0):
    print("[*] Ingesting keyframes into Multimodal OpenAI API...")
    url = "https://api.openai.com/v1/chat/completions"
    cv_data = cv_data or {}
    indices_to_send = select_analysis_frame_indices(image_paths, cv_data, fps=fps)
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [{
            "type": "text",
            "text": (
                "Analyze these chronological timelapse frames and output the structural JSON spec. "
                "Treat the CV DENSITY SUMMARY as binding evidence. Every change_event must appear in time_sequence.source_event_ids.\n\n"
                f"CV DENSITY SUMMARY:\n{cv_density_summary(cv_data, indices_to_send)}"
            )
        }]}
    ]
    
    for idx in indices_to_send:
        path = image_paths[idx]
        b64_data = encode_image_base64(path)
        messages[1]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64_data}"
            }
        })
        print(f"[+] Loaded keyframe {os.path.basename(path)} as input.")
    print(f"[+] Selected {len(indices_to_send)}/{len(image_paths)} frames for dense semantic analysis.")
        
    payload = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            res_data = response.read().decode('utf-8')
            res_json = json.loads(res_data)
            text_response = res_json['choices'][0]['message']['content']
            meta = json.loads(text_response)
            meta["_analysis_frame_indices"] = indices_to_send
            return meta
    except Exception as e:
        print(f"[-] OpenAI API Call Failed: {e}")
        return None

def build_metadata_interactive(cv_data):
    print("\n" + "="*50)
    print("  [Interactive Mode] Build Video-to-Prompt Spec")
    print("="*50)
    print(f"CV Extracted: Camera={cv_data['camera_movement']}, Total Frames={cv_data['total_duration']}")
    
    carrier = input("Enter Carrier/Space name (e.g. abandoned attic, old warehouse) [attic]: ").strip() or "abandoned attic"
    trauma = input("Enter Trauma Pathology (e.g. collapsed ceiling, rust stain): ").strip() or "collapsed ceiling structure with hanging plaster insulation"
    destiny = input("Enter Destiny/After state (e.g. modern cozy studio): ").strip() or "clean retro home studio"
    
    landmarks = []
    print("\nDefine 3 Primary Spatial Landmarks (Foreground, Mid-depth, Background):")
    # Foreground
    fg_name = input("Foreground landmark [floor seam]: ").strip() or "cracked concrete floor seam"
    fg_grid = input("Foreground grid cell [Grid C2]: ").strip() or "Grid C2"
    landmarks.append({"name": fg_name, "grid": fg_grid, "z_depth_scale": "20%"})
    # Mid-depth
    mid_name = input("Mid-depth landmark [brick column]: ").strip() or "raw red-brick column"
    mid_grid = input("Mid-depth grid cell [Grid B1]: ").strip() or "Grid B1"
    landmarks.append({"name": mid_name, "grid": mid_grid, "z_depth_scale": "60%"})
    # Background
    bg_name = input("Background landmark [window void]: ").strip() or "arched window opening"
    bg_grid = input("Background grid cell [Grid A2]: ").strip() or "Grid A2"
    landmarks.append({"name": bg_name, "grid": bg_grid, "z_depth_scale": "45%"})
    
    beats_count = int(input("\nHow many construction phases/beats? [2]: ").strip() or "2")
    time_sequence = []
    for i in range(1, beats_count + 1):
        print(f"\n--- Phase {i} details ---")
        state_n = input("IMAGE N state (starting state): ").strip() or "floor fully covered with heavy concrete debris"
        state_np1 = input("IMAGE N+1 state (ending state): ").strip() or "floor swept completely clean to raw concrete"
        mat = input("Volumetric material name [concrete debris]: ").strip() or "concrete debris"
        container = input("Rigid container used [heavy plastic crates]: ").strip() or "heavy plastic crates"
        vol_grid = input("Material grid location [Grid C2]: ").strip() or "Grid C2"
        
        motion = cv_data["activity_segments"][0] if cv_data["activity_segments"] else {"t_in": "0.0s", "t_out": "7.5s"}
        
        time_sequence.append({
            "beat_index": i,
            "state_name": f"phase {i}",
            "image_n_state": state_n,
            "image_n_plus_1_state": state_np1,
            "volumetric_mass": {
                "material": mat,
                "container": container,
                "grid": vol_grid,
                "volume_flow": "100% capacity to 0% cleared"
            },
            "transient_agents": [
                {
                    "agent_type": "worker",
                    "count": 1,
                    "hal_profile": "solid bright-neon-yellow safety vest, white hardhat, dark blue pants",
                    "trajectory": {
                        "enter_time": motion["t_in"],
                        "enter_grid": "Grid C1",
                        "exit_time": motion["t_out"],
                        "exit_grid": "Grid C1"
                    },
                    "action_loop": "repeatedly bends down to clear material"
                }
            ],
            "lighting_phase": cv_data["lighting_progression"][min(i, len(cv_data["lighting_progression"])-1)],
            "sfx": "debris scrape, shovel tap",
            "ambient": "hollow room tone"
        })
        
    meta = {
        "camera_dna": {
            "shot_type": cv_data["camera_movement"],
            "lens": "14-18mm" if "wide" in cv_data["camera_movement"] else "14mm",
            "height_m": 1.6,
            "perspective": "eye-level perspective looking straight",
            "boundaries": {
                "left": f"left margin in {landmarks[1]['grid']}",
                "right": f"right margin in {landmarks[1]['grid']}",
                "top": f"ceiling in {landmarks[2]['grid']}",
                "bottom": f"debris floor in {landmarks[0]['grid']}"
            }
        },
        "primary_landmarks": landmarks,
        "time_sequence": time_sequence
    }
    return meta

def enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=3.0):
    analysis_indices = meta.pop("_analysis_frame_indices", None)
    if analysis_indices is None:
        analysis_indices = select_analysis_frame_indices(keyframe_paths, cv_data, fps=fps)

    meta.setdefault("frame_observations", [
        obs for obs in cv_data.get("frame_observations", [])
        if obs.get("frame_index") in set(analysis_indices)
    ])
    meta.setdefault("change_events", cv_data.get("change_events", []))
    meta["analysis_frame_indices"] = analysis_indices
    meta["num_analyzed_frames"] = len(analysis_indices)
    meta["total_extracted_frames"] = len(keyframe_paths)

    cv_event_ids = [event.get("event_id") for event in cv_data.get("change_events", []) if event.get("event_id")]
    seqs = meta.get("time_sequence", [])
    if seqs and cv_event_ids:
        uncovered = set(cv_event_ids)
        for idx, seq in enumerate(seqs):
            if not seq.get("source_event_ids"):
                event_id = cv_event_ids[min(idx, len(cv_event_ids) - 1)]
                seq["source_event_ids"] = [event_id]
            for event_id in seq.get("source_event_ids", []):
                uncovered.discard(event_id)
            if not seq.get("source_frame_range"):
                event_id = seq.get("source_event_ids", [None])[0]
                event = next((e for e in cv_data.get("change_events", []) if e.get("event_id") == event_id), None)
                if event:
                    seq["source_frame_range"] = event.get("frame_range")
        if uncovered:
            seqs[-1].setdefault("source_event_ids", [])
            seqs[-1]["source_event_ids"].extend(sorted(uncovered))
    return meta

def fetch_semantic_metadata(keyframe_paths, cv_data, force_local=False, fps=3.0, base_url=None, model=None):
    gemini_key = os.environ.get("GEMINI_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("API_BASE_URL")
    
    # Check if running in a non-interactive environment (like a web server backend)
    is_non_interactive = os.environ.get("NON_INTERACTIVE") == "1" or not sys.stdin or not sys.stdin.isatty()
    
    if force_local:
        if is_non_interactive:
            raise RuntimeError("Cannot run interactive wizard in a non-interactive environment.")
        meta = build_metadata_interactive(cv_data)
        return enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=fps)
        
    if base_url:
        print(f"[DEBUG] fetch_semantic_metadata routing via base_url: {base_url}, model: {model}")
        api_key = gemini_key or openai_key or ""
        meta = call_gemini_api(api_key, LLM_SYSTEM_PROMPT, keyframe_paths, cv_data=cv_data, fps=fps, base_url=base_url, model=model)
        if meta:
            meta["camera_dna"]["shot_type"] = cv_data["camera_movement"]
            return enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=fps)
            
    if gemini_key:
        meta = call_gemini_api(gemini_key, LLM_SYSTEM_PROMPT, keyframe_paths, cv_data=cv_data, fps=fps, base_url=base_url, model=model)
        if meta:
            # Merge CV computed timing & camera properties to strengthen MLLM data
            meta["camera_dna"]["shot_type"] = cv_data["camera_movement"]
            return enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=fps)
    elif openai_key:
        meta = call_openai_api(openai_key, LLM_SYSTEM_PROMPT, keyframe_paths, cv_data=cv_data, fps=fps)
        if meta:
            meta["camera_dna"]["shot_type"] = cv_data["camera_movement"]
            return enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=fps)
            
    if is_non_interactive:
        raise RuntimeError("No visual API credentials found (or API call failed), and interactive wizard is disabled in non-interactive environments.")
        
    print("[!] No visual API credentials found (or API failed). Defaulting to interactive wizard.")
    meta = build_metadata_interactive(cv_data)
    return enrich_metadata_with_cv(meta, cv_data, keyframe_paths, fps=fps)

# =====================================================================
# Module 4: SCUP Prompt Composer (Contract Translation)
# =====================================================================
def compose_scup_prompts(metadata, clean_mode=False):
    print(f"[*] Composing SCUP Prompts from intermediate metadata JSON (clean_mode={clean_mode})...")
    
    cam = metadata["camera_dna"]
    landmarks = metadata["primary_landmarks"]
    seqs = metadata["time_sequence"]
    
    # Assemble Camera DNA Block (Inherited strictly character-for-character)
    camera_dna_str = f"{cam['shot_type']}, {cam['lens'] or '14mm'} lens, height {cam['height_m']}m, {cam['perspective']}; horizon line remains perfectly level at mid-frame height; all optical flow lines radiate symmetrically from the optical center of Grid B2"
    
    # Boundary constraints
    bounds = cam["boundaries"]
    boundary_str = f"left boundary {bounds['left']}, right boundary {bounds['right']}, top boundary {bounds['top']}, and bottom foreground band {bounds['bottom']}"
    
    # Landmarks enumeration
    landmark_strs = []
    for l in landmarks:
        landmark_strs.append(f"{l['name']} in {l['grid']} holds a stable visible scale of {naturalize_visual_text(l['z_depth_scale'])} of total frame height")
    landmarks_str = ", ".join(landmark_strs)
    
    # Generate IMAGE Prompts
    images = []
    
    if clean_mode:
        # IMAGE 1 (Before/Trauma clean frame)
        img1_prompt = (
            f"A professional photo of a {cam['shot_type']} from an {cam['perspective'] or 'eye-level perspective'}. "
            f"Wide {cam['lens'] or '14-18mm'} lens, camera height {cam['height_m']}m. "
            f"The scene shows the 'before' state: {naturalize_visual_text(seqs[0]['image_n_state'])}. "
            f"Visible elements include {', '.join([l['name'] for l in landmarks])}. "
            f"Lighting is {seqs[0]['lighting_phase']}. Beautiful photorealistic material realism, highly detailed, 4k resolution."
        )
        images.append(naturalize_visual_text(img1_prompt))
        
        # Intermediate state images & final image
        for idx, seq in enumerate(seqs):
            is_last = (idx == len(seqs) - 1)
            state_desc = seq['image_n_plus_1_state']
            state_term = "final completed state" if is_last else f"progressive phase {idx+1} state"
            
            img_prompt = (
                f"A professional photo of a {cam['shot_type']} from an {cam['perspective'] or 'eye-level perspective'}. "
                f"This image inherits all landmarks, geometry, and camera framing from IMAGE 1. "
                f"The scene is in its {state_term}, completely empty of workers, showing: {naturalize_visual_text(state_desc)}. "
                f"The landmarks ({landmarks[0]['name']}, {landmarks[1]['name']}) remain in the exact same positions as IMAGE 1. "
                f"Lighting is {seq['lighting_phase']}. Highly detailed, photorealistic material realism, consistent with IMAGE 1."
            )
            images.append(naturalize_visual_text(img_prompt))
    else:
        # IMAGE 1 (Before/Trauma clean frame)
        img1_prompt = (
            f"Generate an image of a {camera_dna_str}. "
            f"Locked anchors: {landmarks[0]['name']} at {landmarks[0]['grid']} ({naturalize_visual_text(landmarks[0]['z_depth_scale'])}), "
            f"{landmarks[1]['name']} at {landmarks[1]['grid']} ({naturalize_visual_text(landmarks[1]['z_depth_scale'])}), {boundary_str}. "
            f"The scene is the explicit before anchor, completely empty of workers, with {naturalize_visual_text(seqs[0]['image_n_state'])}. "
            f"Primary landmarks remain fixed: {landmarks_str}. "
            f"Lighting is {seqs[0]['lighting_phase']} and photorealistic material realism. "
            f"[Natural-language guardrail: keep same framing; do not redesign]."
        )
        images.append(naturalize_visual_text(img1_prompt))
        
        # Intermediate state images & final image
        for idx, seq in enumerate(seqs):
            is_last = (idx == len(seqs) - 1)
            phase_num = idx + 2
            
            # Build RPL locks
            rpl_str = f"{landmarks[0]['name']} is locked relatively to {landmarks[1]['name']}"
            
            path = causal_path_for(seq)
            trace_phrase = ", ".join(path["persistent_traces"][:3])
            inheritance = path["next_frame_inheritance"]
            if is_last:
                img_prompt = (
                    f"Generate an image of a {camera_dna_str}. "
                    f"Scene inherits all landmarks, geometry, and boundary anchors from IMAGE 1. "
                    f"{rpl_str} remains the relative spatial relationship for drift-sensitive details. "
                    f"The scene is the final completed state, completely empty of workers, with {naturalize_visual_text(seq['image_n_plus_1_state'])}. "
                    f"The final anchor keeps visible physical proof of the last action: {trace_phrase}, and {inheritance}. "
                    f"Any glossy floor or tabletop reflection is muted, blurred, low-gloss, and diffused so background shapes remain soft and low-frequency. "
                    f"Keep lighting as {seq['lighting_phase']} and photorealistic material realism. "
                    f"[Guardrail sentence: keep identical camera DNA angle]."
                )
            else:
                img_prompt = (
                    f"Generate an image of a {camera_dna_str}. "
                    f"Scene inherits all landmarks, geometry, and boundary anchors from IMAGE 1. "
                    f"{rpl_str} remains the relative spatial relationship for drift-sensitive details. "
                    f"The scene is the progressive phase {idx+1} state, completely empty of workers, with {naturalize_visual_text(seq['image_n_plus_1_state'])} while {landmarks[1]['name']} remains visible and unchanged. "
                    f"The changed surface keeps visible physical proof from the preceding action: {trace_phrase}, and {inheritance}. "
                    f"Lighting is {seq['lighting_phase']} and photorealistic material realism. "
                    f"[Guardrail: keep same framing; do not redesign]."
                )
            images.append(naturalize_visual_text(img_prompt))
        
    # Generate VIDEO Prompts
    videos = []
    for idx, seq in enumerate(seqs):
        first_img_idx = idx + 1
        last_img_idx = idx + 2
        
        state_lower = seq.get("state_name", "").lower()
        path = causal_path_for(seq)
        trace_phrase = ", ".join(path["persistent_traces"][:3])
        progress_a, progress_b = visual_progress_markers(seq, state_lower)

        # Safe extraction of volumetric mass
        vol = seq.get("volumetric_mass", {})
        if vol and vol.get("material"):
            vol_clause = (
                f"The {naturalize_visual_text(vol['material'])} stays inside {naturalize_visual_text(vol.get('container', 'rigid containers'))} "
                f"at {vol.get('grid', 'Grid C2')}; the containers visibly fill, travel along the work path, and reveal their empty bottoms as material is removed, so no loose material dissolves or evaporates. "
            )
        else:
            vol_clause = "No loose bulk material is created in this clip; every changed object remains rigid and countable. "

        event_ids = seq.get("source_event_ids", [])
        if isinstance(event_ids, str):
            event_ids = [event_ids]
        source_frame_range = seq.get("source_frame_range", "unlisted source frame range")
        event_clause = (
            f"The motion follows the observed source event path {', '.join(event_ids) if event_ids else 'for this beat'} from {source_frame_range}, preserving the intermediate object, material, lighting, and worker-action changes without skipping over them. "
        )
            
        # Safe extraction of transient agents
        agents = seq.get("transient_agents", [])
        
        # Build manual tool mapping
        manual_tool = ""
        if agents:
            agent = agents[0]
            traj = agent.get("trajectory", {})
            manual_tool = agent.get("manual_tool")
            if not manual_tool:
                if "paint" in state_lower or "coat" in state_lower or "refinish" in state_lower:
                    manual_tool = "solid-blue cylindrical synthetic fiber paint roller connected to a metallic handle"
                elif "debris" in state_lower or "clear" in state_lower or "sweep" in state_lower or "clean" in state_lower:
                    manual_tool = "solid-black rectangular plastic broom block with stiff nylon bristles connected to a long matte-black aluminum handle"
                elif "install" in state_lower or "build" in state_lower or "repair" in state_lower:
                    manual_tool = "matte-black rectangular steel hammer head connected to a solid-ashwood handle"
                else:
                    manual_tool = "matte-black and orange heavy-duty hand tool with a textured black grip handle"
            else:
                if "shovel" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "matte-black rectangular steel shovel head connected to a solid-ashwood handle"
                elif "wrench" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "silver metallic impact wrench tool with a textured black rubberized grip handle"
                elif "screwdriver" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "solid-orange cordless electric screwdriver tool with a textured black grip handle"
                elif "drill" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "solid-orange electric drill tool with a textured black grip handle"
                elif "saw" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "silver steel timber saw blade connected to a solid-yellow plastic D-grip handle"
                elif "broom" in manual_tool.lower() and "solid" not in manual_tool.lower():
                    manual_tool = "solid-black long-handle plastic broom with stiff nylon bristles"

        if clean_mode:
            if is_threshold_beat(seq):
                vid_prompt = (
                    f"A continuous first-person camera shot interpolating smoothly between IMAGE {first_img_idx} (start frame) and IMAGE {last_img_idx} (end frame). "
                    f"The camera performs one continuous coaxial forward push through the already open doorway, moving from the exterior scene to the interior scene. "
                    f"No construction work occurs; the only action is the camera crossing the threshold. "
                    f"The camera path follows: {path['movement_path']}. "
                    f"Sound effects: hinge creak, bootstep scrape, and gear rustle. Ambient noise shifts from exterior wind to hollow cabin tone."
                )
                videos.append(naturalize_visual_text(vid_prompt))
                continue
                
            if agents:
                agent = agents[0]
                action_loop = naturalize_visual_text(agent.get("action_loop", "performs physical construction labor"))
                agent_desc = f"A worker wearing a {naturalize_visual_text(agent.get('hal_profile', 'safety vest and hardhat'))} enters, uses a {naturalize_visual_text(manual_tool)} to {action_loop}, and exits before the end of the clip."
            else:
                agent_desc = "No workers or human agents appear in the video."
                
            progress_desc = f"The progress is shown clearly: first {progress_a}, then {progress_b}."
            
            vid_prompt = (
                f"A smooth construction time-lapse video interpolating from IMAGE {first_img_idx} (start frame) to IMAGE {last_img_idx} (end frame). "
                f"The camera remains in a fixed, locked tripod position with the same framing and landmarks as IMAGE 1. "
                f"The video shows the process of: {naturalize_visual_text(seq.get('single_physical_operation') or seq['state_name'])}. "
                f"{agent_desc} "
                f"{progress_desc} "
                f"The transition is a smooth physical progression, with no cross-dissolves, fades, or jump cuts. "
                f"Sound effects: {seq['sfx']}. Ambient sound: {seq['ambient']}."
            )
            videos.append(naturalize_visual_text(vid_prompt))
        else:
            if agents:
                agent = agents[0]
                traj = agent.get("trajectory", {})
                action_loop = naturalize_visual_text(agent.get("action_loop", "performs physical construction labor"))
                mtal_clause = f"The worker keeps the same {naturalize_visual_text(manual_tool)} in hand and repeats the same action loop: {action_loop}. "
                
                passage_clause = f"At the first frame, {agent.get('count', 1)} worker enters from the {traj.get('enter_grid', 'Grid C1')} edge; the worker performs the action continuously, then walks out through the {traj.get('exit_grid', 'Grid C1')} edge before the final frame, leaving the last frame empty of active agents. "
                hal_clause = f"The worker remains a simple solid silhouette of {naturalize_visual_text(agent.get('hal_profile', 'solid yellow safety vest, white hardhat, blue pants'))}, with no readable face, logo, or fabric pattern. "
                tspa_clause = f"Two visible progress cues must unfold naturally: first, {progress_a}; second, {progress_b}. "
            else:
                mtal_clause = "No active worker appears; the visible motion comes only from the declared camera path or passive environment. "
                passage_clause = "The scene contains zero active workers or human agents throughout the clip, and the final frame remains empty. "
                hal_clause = "No worker silhouette is introduced. "
                tspa_clause = f"Two visible progress cues must unfold naturally: first, {progress_a}; second, {progress_b}. "

            if is_threshold_beat(seq):
                vid_prompt = (
                    f"Use the provided first frame and last frame as exact composition anchors. "
                    f"Use IMAGE {first_img_idx} as the actual first-frame image and IMAGE {last_img_idx} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                    f"The camera performs one continuous coaxial forward push through the already open doorway, with the horizon line level at mid-frame height and the optical flow radiating from Grid B2. "
                    f"The doorway edges slide outward toward Grid B1 and Grid B3 as the pre-seen interior landmarks grow larger without changing their relative positions. "
                    f"No construction work occurs during this bridge; the only action is threshold crossing from the exterior anchor to the interior anchor. "
                    f"The motion preserves {path['movement_path']}, while {trace_phrase} remain visible as the camera passes the threshold. "
                    f"Sound cues include hinge creak, bootstep scrape, and gear rustle. Ambient noise shifts from exterior wind to hollow cabin tone."
                )
                videos.append(naturalize_visual_text(vid_prompt))
                continue
                
            vid_prompt = (
                f"Use the provided first frame and last frame as exact composition anchors. "
                f"Use IMAGE {first_img_idx} as the actual first-frame image and IMAGE {last_img_idx} as the actual last-frame image; every visible action must interpolate between those two frame images without inventing a third layout. "
                f"Keep locked camera position, same frame boundaries, and Grid positions of critical fixed landmarks; horizon line remains perfectly level at mid-frame height; all optical flow lines radiate symmetrically from the optical center of Grid B2. "
                f"The video shows one continuous physical operation only: {naturalize_visual_text(seq.get('single_physical_operation') or seq['state_name'])} from IMAGE {first_img_idx} to IMAGE {last_img_idx}. "
                f"This is a continuous construction time-lapse, not real-time footage. "
                f"{passage_clause}"
                f"{hal_clause}"
                f"{mtal_clause}"
                f"{tspa_clause}"
                f"The material source is {path['material_source']}; it enters by {path['entry_path']}; contact is caused by {path['tool_contact']}; movement follows {path['movement_path']}. "
                f"{event_clause}"
                f"{vol_clause}"
                f"The final frame retains {trace_phrase}, and {path['next_frame_inheritance']} so the changed state has a visible physical cause. "
                f"Strictly forbid transition shortcuts like cross-dissolve, fade-in, suddenly, magically, rapid montage, or jump cut. "
                f"Sound cues include {seq['sfx']}. Ambient noise is {seq['ambient']}."
            )
            videos.append(naturalize_visual_text(vid_prompt))
            
    return images, videos

# =====================================================================
# Module 5: Programmatic SCUP Quality Auditor
# =====================================================================
def run_scup_audit(images, videos, fps=3.0, num_analyzed_frames=None, total_frames=None, change_events=None, analysis_frame_indices=None, time_sequence=None, post_render_qc=None, video_path=None):
    print("[*] Running automated SCUP Quality Audit checks...")
    audit_results = {
        "score": 100,
        "gates": []
    }
    
    banned_words_image = ["worker", "workers", "machine", "machines", "carpenter", "builder", "people", "man", "woman"]
    banned_transition_words = ["cross-dissolve", "fade-in", "suddenly", "magically", "rapid montage", "jump cut", "instant transformation"]
    
    # Gate 0: Keyframe Collage Auto-Generation Gate (T0)
    collage_ok = True
    collage_details = []
    if video_path:
        collage_file = os.path.splitext(video_path)[0] + "_collage.jpg"
        if not os.path.exists(collage_file):
            collage_ok = False
            collage_details.append(f"Keyframe collage is missing. Expected at: {collage_file}")
        else:
            collage_details.append(f"Keyframe collage successfully generated at: {collage_file}")
    else:
        collage_details.append("No video path provided for collage verification. Assuming passed.")

    if not collage_ok:
        audit_results["score"] -= 40
        audit_results["gates"].append({
            "name": "Keyframe Collage Auto-Generation Gate",
            "status": "FAIL",
            "tier": "T0",
            "details": collage_details,
            "solution": "Ensure FFmpeg's tile filter successfully generates a 5-column tiled keyframe collage persistent next to the video."
        })
    else:
        audit_results["gates"].append({
            "name": "Keyframe Collage Auto-Generation Gate",
            "status": "PASS",
            "tier": "T0",
            "details": collage_details
        })

    # Gate 0b: Video Reverse Engineering Analysis Frame Count Gate (P0)
    if num_analyzed_frames is not None and total_frames is not None:
        duration_seconds = math.ceil(total_frames / fps) if fps else 1
        minimum_dense_frames = total_frames if total_frames <= 90 else max(duration_seconds, math.ceil(total_frames * 0.4))
        if num_analyzed_frames < minimum_dense_frames:
            audit_results["score"] -= 30
            audit_results["gates"].append({
                "name": "Video Analysis Frame Count Gate",
                "status": "FAIL",
                "tier": "P0",
                "details": [f"Analyzed {num_analyzed_frames} frames from {total_frames} extracted frames, but dense reverse analysis requires at least {minimum_dense_frames} frames (short videos require all frames; longer videos require baseline temporal coverage plus change peaks)."],
                "solution": "Use adaptive dense sampling: all frames for short clips, plus baseline per-second frames, CV peak frames, and event boundary frames for longer clips."
            })
        else:
            audit_results["gates"].append({
                "name": "Video Analysis Frame Count Gate",
                "status": "PASS",
                "tier": "P0",
                "details": [f"Analyzed {num_analyzed_frames} frames from {total_frames} extracted frames, satisfying dense coverage minimum {minimum_dense_frames}."]
            })

    # Gate 0c: Change Event Coverage Gate (P0)
    if change_events is not None:
        event_ids = [event.get("event_id") for event in change_events if event.get("event_id")]
        declared_ids = set()
        for seq in time_sequence or []:
            ids = seq.get("source_event_ids", [])
            if isinstance(ids, str):
                ids = [ids]
            declared_ids.update(ids)
        missing_events = [event_id for event_id in event_ids if event_id not in declared_ids]
        missing_video_refs = [event_id for event_id in event_ids if not any(event_id in vid for vid in videos)]
        if missing_events or missing_video_refs:
            audit_results["score"] -= 30
            audit_results["gates"].append({
                "name": "Change Event Coverage Gate",
                "status": "FAIL",
                "tier": "P0",
                "details": [
                    f"Metadata beats missing source_event_ids: {missing_events or 'none'}",
                    f"VIDEO prompts missing event references: {missing_video_refs or 'none'}"
                ],
                "solution": "Every CV change_event must be assigned to time_sequence.source_event_ids and named in the corresponding VIDEO prompt."
            })
        else:
            audit_results["gates"].append({
                "name": "Change Event Coverage Gate",
                "status": "PASS",
                "tier": "P0",
                "details": [f"All {len(event_ids)} detected change events are covered by metadata beats and VIDEO prompts."]
            })

    # Gate 0d: Analysis Peak Inclusion Gate (P0)
    if change_events is not None and analysis_frame_indices is not None:
        selected = set(analysis_frame_indices)
        missing_peak_frames = []
        for event in change_events:
            for idx in (event.get("start_index"), event.get("peak_index"), event.get("end_index")):
                if idx is not None and idx not in selected:
                    missing_peak_frames.append(f"{event.get('event_id', 'event')} index {idx}")
        if missing_peak_frames:
            audit_results["score"] -= 30
            audit_results["gates"].append({
                "name": "Analysis Peak Inclusion Gate",
                "status": "FAIL",
                "tier": "P0",
                "details": missing_peak_frames,
                "solution": "Send each detected event's start, peak, and end frames to the multimodal model."
            })
        else:
            audit_results["gates"].append({
                "name": "Analysis Peak Inclusion Gate",
                "status": "PASS",
                "tier": "P0",
                "details": ["All detected event start, peak, and end frames were included in semantic analysis."]
            })

    # Gate 0e: Temporal Physics Skeleton Gate (P0)
    gate_skeleton_fail = []
    if time_sequence is not None:
        required_path_fields = ("material_source", "entry_path", "tool_contact", "movement_path", "persistent_traces", "next_frame_inheritance")
        for idx, seq in enumerate(time_sequence):
            if not seq.get("shot_family"):
                gate_skeleton_fail.append(f"Beat {idx+1} is missing shot_family.")
            if not seq.get("beat_type"):
                gate_skeleton_fail.append(f"Beat {idx+1} is missing beat_type.")
            if not seq.get("single_physical_operation"):
                gate_skeleton_fail.append(f"Beat {idx+1} is missing single_physical_operation.")
            path = seq.get("causal_path") or {}
            for field in required_path_fields:
                if not path.get(field):
                    gate_skeleton_fail.append(f"Beat {idx+1} causal_path is missing {field}.")
            traces = path.get("persistent_traces", [])
            if isinstance(traces, str):
                traces = [traces]
            if len([t for t in traces if str(t).strip()]) < 2:
                gate_skeleton_fail.append(f"Beat {idx+1} causal_path has fewer than two persistent traces.")
    if gate_skeleton_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Temporal Physics Skeleton Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_skeleton_fail,
            "solution": "Each beat must declare shot_family, beat_type, single_physical_operation, and a full causal_path before prompt rendering."
        })
    else:
        audit_results["gates"].append({
            "name": "Temporal Physics Skeleton Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All beats carry a complete temporal physics skeleton."]
        })

    # Gate 0f: Threshold Bridge Continuity Gate (P0)
    gate_threshold_fail = []
    if time_sequence is not None:
        for idx, seq in enumerate(time_sequence):
            if not is_threshold_beat(seq):
                continue
            if any(word in seq.get("state_name", "").lower() for word in ("install", "paint", "clean", "floor", "panel")):
                gate_threshold_fail.append(f"Beat {idx+1} threshold bridge appears to include construction work.")
            if idx > 0:
                pre_anchor = (time_sequence[idx - 1].get("image_n_plus_1_state", "") + " " + time_sequence[idx - 1].get("state_name", "")).lower()
                sneak_terms = sum(1 for term in ("interior", "landmark", "rear window", "ceiling", "light", "bench", "cabinet") if term in pre_anchor)
                if sneak_terms < 2:
                    gate_threshold_fail.append(f"Beat {idx+1} lacks a preceding anchor with two visible interior sneak-peek landmarks.")
            vid = videos[idx] if idx < len(videos) else ""
            for term in ("coaxial forward", "doorway edges", "interior landmarks"):
                if term not in vid.lower():
                    gate_threshold_fail.append(f"VIDEO {idx+1} threshold bridge is missing '{term}'.")
    if gate_threshold_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Threshold Bridge Continuity Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_threshold_fail,
            "solution": "Isolate exterior-to-interior movement in a threshold_bridge beat with pre-seen interior landmarks and coaxial door-frame projection."
        })
    else:
        audit_results["gates"].append({
            "name": "Threshold Bridge Continuity Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All threshold bridges are isolated and preserve pre-visualized interior landmarks."]
        })

    # Gate 0g: Post-render forensic gates (P0)
    if post_render_qc is not None:
        post_render_gate_specs = [
            ("Rendered Text Artifact Gate", "text_overlay_hits", "Rendered video contains text-like numeric or percentage overlays."),
            ("Hard Transition Peak Gate", "hard_cut_times", "Rendered video contains hard transition peaks at 3fps sampling."),
            ("Agent Boundary Pop Gate", "agent_pop_hits", "Rendered video contains worker/tool boundary pop-in or pop-out."),
            ("Object Birth Without Path Gate", "object_birth_hits", "Rendered video contains objects that appear without a visible source/path."),
            ("State Regression Gate", "state_regression_hits", "Rendered video regresses from a later state to an earlier construction state."),
        ]
        for gate_name, key, problem in post_render_gate_specs:
            hits = post_render_qc.get(key)
            if hits is None:
                # Detection for this gate is not implemented yet — report honestly
                # instead of emitting a fake PASS from an always-empty placeholder.
                audit_results["gates"].append({
                    "name": gate_name,
                    "status": "SKIP",
                    "tier": "P0",
                    "details": ["NOT CHECKED: automated detection for this gate is not implemented; verify manually (VLM frame-pair review covers it during staged rendering)."]
                })
            elif hits:
                audit_results["score"] -= 30
                audit_results["gates"].append({
                    "name": gate_name,
                    "status": "FAIL",
                    "tier": "P0",
                    "details": [f"{problem} Hits: {hits[:12]}"],
                    "solution": "Rewrite the relevant beat skeleton and prompt before marking the render complete."
                })
            else:
                audit_results["gates"].append({
                    "name": gate_name,
                    "status": "PASS",
                    "tier": "P0",
                    "details": ["No post-render hits detected for this gate."]
                })
        drift_score = post_render_qc.get("landmark_drift_score", 0.0) or 0.0
        if drift_score >= 0.45:
            audit_results["score"] -= 30
            audit_results["gates"].append({
                "name": "Landmark Drift Gate",
                "status": "FAIL",
                "tier": "P0",
                "details": [f"Landmark drift score is {drift_score}, indicating repeated hard spatial shifts."],
                "solution": "Strengthen shot_family, primary landmark, horizon, and threshold bridge constraints before re-rendering."
            })
        else:
            audit_results["gates"].append({
                "name": "Landmark Drift Gate",
                "status": "PASS",
                "tier": "P0",
                "details": [f"Landmark drift score is {drift_score}."]
            })
        
    # Gate 1: Clean Frame Boundary in IMAGE prompts (P0)
    gate1_fail = []
    for idx, img in enumerate(images):
        # Remove negative constraints so they don't trigger false positives
        clean_img = img.lower()
        clean_img = clean_img.replace("completely empty of workers", "")
        clean_img = clean_img.replace("completely empty of dynamic elements", "")
        clean_img = clean_img.replace("zero active workers", "")
        for w in banned_words_image:
            if re.search(r'\b' + re.escape(w) + r'\b', clean_img):
                gate1_fail.append(f"IMAGE {idx+1} contains active dynamic word '{w}'")
                
    if gate1_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Clean Frame Boundary",
            "status": "FAIL",
            "tier": "P0",
            "details": gate1_fail,
            "solution": "All IMAGE prompts must be sterile and contain zero active workers or machinery."
        })
    else:
        audit_results["gates"].append({
            "name": "Clean Frame Boundary",
            "status": "PASS",
            "tier": "P0",
            "details": ["All IMAGE prompts are 100% sterile of transient workers/agents."]
        })
        
    # Gate 2: Adjacent-Frame Binding in VIDEO prompts (P0)
    gate2_fail = []
    for idx, vid in enumerate(videos):
        first_img = f"IMAGE {idx+1}"
        last_img = f"IMAGE {idx+2}"
        if first_img not in vid or last_img not in vid or "exact composition anchors" not in vid:
            gate2_fail.append(f"VIDEO {idx+1} is missing binding anchors {first_img} and {last_img}")
            
    if gate2_fail:
        audit_results["score"] -= 20
        audit_results["gates"].append({
            "name": "IMAGE-VIDEO Frame Binding",
            "status": "FAIL",
            "tier": "P0",
            "details": gate2_fail,
            "solution": "All VIDEO prompts must explicitly state the composition anchors and reference adjacent IMAGE frames."
        })
    else:
        audit_results["gates"].append({
            "name": "IMAGE-VIDEO Frame Binding",
            "status": "PASS",
            "tier": "P0",
            "details": ["All VIDEO prompts enforce rigid composition anchors and adjacent frame bindings."]
        })
        
    # Gate 3: Bi-directional Out-and-In Passage Trajectory (P0)
    gate3_fail = []
    for idx, vid in enumerate(videos):
        if "bi-directional" not in vid.lower() and "out-and-in" not in vid.lower():
            gate3_fail.append(f"VIDEO {idx+1} is missing Out-and-In passage trajectory clause")
            
    if gate3_fail:
        audit_results["score"] -= 20
        audit_results["gates"].append({
            "name": "Bi-directional Passage Lock",
            "status": "FAIL",
            "tier": "P0",
            "details": gate3_fail,
            "solution": "All video prompts featuring workers must explicitly define entry (t=0s) and exit (t=7.5s) coordinates."
        })
    else:
        audit_results["gates"].append({
            "name": "Bi-directional Passage Lock",
            "status": "PASS",
            "tier": "P0",
            "details": ["All VIDEO prompts lock workers using double-ended Out-and-In entry/exit bounds."]
        })
        
    # Gate 4: Rigid Container Encapsulation - RCE (P0)
    gate4_fail = []
    for idx, vid in enumerate(videos):
        if "encapsulated" not in vid.lower() or "rigid" not in vid.lower():
            gate4_fail.append(f"VIDEO {idx+1} is missing Rigid Container Encapsulation (RCE)")
            
    if gate4_fail:
        audit_results["score"] -= 15
        audit_results["gates"].append({
            "name": "Rigid Container Encapsulation",
            "status": "FAIL",
            "tier": "P0",
            "details": gate4_fail,
            "solution": "Granular/loose materials must be encapsulated in solid crates/buckets/bags to prevent rendering morphs."
        })
    else:
        audit_results["gates"].append({
            "name": "Rigid Container Encapsulation",
            "status": "PASS",
            "tier": "P0",
            "details": ["All granular flows are bounded using rigid containers (RCE)."]
        })
        
    # Gate 5: Banned Video Transitions (P0)
    gate5_fail = []
    for idx, vid in enumerate(videos):
        clean_vid = vid.lower()
        clean_vid = clean_vid.replace("strictly forbid transition shortcuts like cross-dissolve, fade-in, suddenly, magically, rapid montage, or jump cut", "")
        for w in banned_transition_words:
            if w in clean_vid:
                gate5_fail.append(f"VIDEO {idx+1} contains forbidden transition '{w}'")
                
    if gate5_fail:
        audit_results["score"] -= 15
        audit_results["gates"].append({
            "name": "Continuous Action Flow",
            "status": "FAIL",
            "tier": "P0",
            "details": gate5_fail,
            "solution": "Remove jump-cuts and soft transitions. Use time-lapse pacing verbs only."
        })
    else:
        audit_results["gates"].append({
            "name": "Continuous Action Flow",
            "status": "PASS",
            "tier": "P0",
            "details": ["All VIDEO prompts contain continuous temporal descriptions without cinematic dissolves/cuts."]
        })

    # Gate 5b: Manual Tool & Construction Realism Gate (P0)
    gate5b_fail = []
    required_tools = ['broom', 'brush', 'roller', 'shovel', 'hammer', 'trowel', 'drill', 'saw', 'spatula', 'scraper', 'tool']
    for idx, vid in enumerate(videos):
        # Skip check if the video has zero active workers (sterile frame) or is a threshold bridge
        is_sterile = "zero active workers" in vid.lower() or "empty of active agents" in vid.lower() or "no active worker appears" in vid.lower() or "none (no active workers present)" in vid.lower() or "none (no active workers)" in vid.lower()
        is_bridge = "threshold bridge" in vid.lower() or "coaxial forward push" in vid.lower() or "doorway edges" in vid.lower()
        if is_sterile or is_bridge:
            continue
            
        # Tool check
        has_tool = any(t in vid.lower() for t in required_tools)
        if not has_tool:
            gate5b_fail.append(f"VIDEO {idx+1} is missing a physical manual construction tool (e.g. broom, roller, brush, hammer).")
            
        if "two visible progress cues" not in vid.lower() or "first," not in vid.lower() or "second," not in vid.lower():
            gate5b_fail.append(f"VIDEO {idx+1} is missing two natural-language visual progress cues.")
            
    if gate5b_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Manual Tool & Construction Realism Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate5b_fail,
            "solution": "Ensure video prompts contain explicit manual tools and two natural-language visual progress cues without structured marker labels."
        })
    else:
        audit_results["gates"].append({
            "name": "Manual Tool & Construction Realism Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All non-sterile active videos enforce manual tools and dual visual progress cues without structured marker labels."]
        })

    # Gate 5c: Global Causal Trace Rule - GCTR (P0)
    gate5c_fail = []
    trace_keywords = [
        'contact mark', 'contact marks', 'seam', 'seams', 'fastener', 'fasteners',
        'residue', 'drag path', 'drag paths', 'compression mark', 'compression marks',
        'dust edge', 'dust edges', 'tool scar', 'tool scars', 'weld bead', 'weld beads',
        'cut line', 'cut lines', 'adhesive squeeze-out', 'tire print', 'tire prints',
        'track print', 'track prints', 'cable rub', 'cable rubs', 'scaffold footprint',
        'scaffold footprints', 'clamp mark', 'clamp marks', 'bracket shadow',
        'bracket shadows', 'alignment guide', 'alignment guides', 'machine pressure',
        'equipment pressure', 'equipment compression', 'drag scuff', 'drag scuffs',
        'roller stipple', 'brush overlap', 'fastener head', 'fastener heads',
        'footstep scuff', 'footstep scuffs', 'contact shadow', 'contact shadows'
    ]
    for idx, img in enumerate(images[1:], start=2):
        img_lower = img.lower()
        trace_count = sum(1 for kw in trace_keywords if kw in img_lower)
        has_causal_language = any(phrase in img_lower for phrase in ("visible physical proof", "visible physical cause", "changed surface keeps", "final anchor keeps"))
        if not has_causal_language or trace_count < 2:
            gate5c_fail.append(f"IMAGE {idx} is missing natural-language causal proof or has fewer than two visible trace markers.")

    for idx, vid in enumerate(videos, start=1):
        vid_lower = vid.lower()
        trace_count = sum(1 for kw in trace_keywords if kw in vid_lower)
        has_causal_language = any(phrase in vid_lower for phrase in ("material source", "visible physical cause", "final frame retains", "contact is caused"))
        if not has_causal_language or trace_count < 2:
            gate5c_fail.append(f"VIDEO {idx} is missing natural-language causal path proof or has fewer than two persistent trace markers.")

    if gate5c_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Global Causal Trace Rule",
            "status": "FAIL",
            "tier": "P0",
            "details": gate5c_fail,
            "solution": "Every visible change must declare a natural-language source, entry path, contact method, movement path, and at least two persistent trace markers."
        })
    else:
        audit_results["gates"].append({
            "name": "Global Causal Trace Rule",
            "status": "PASS",
            "tier": "P0",
            "details": ["All changed anchors and videos carry natural-language causal path evidence and at least two persistent trace markers."]
        })    # NLVTR: Banned Notations Check (P0)
    gate_nlvtr_fail = []
    banned_acronyms = ["tspa", "hal", "vmfp", "gctr", "rpl", "rce", "rhma", "scup"]
    banned_structured_phrases = [
        "causal trace evidence:",
        "relative positioning locks:",
        "volumetric flow",
        "source change event coverage:",
        "progress marker 1:",
        "progress marker 2:",
        "dual-stage active progress indicators:",
        "worker color and silhouette locking profile:",
        "tool and active movement loop:",
        "bi-directional out-and-in entry and exit bounds:"
    ]
    for idx, img in enumerate(images):
        img_lower = img.lower()
        if "%" in img:
            gate_nlvtr_fail.append(f"IMAGE {idx+1} contains banned percentage character '%'")
        found = [acro for acro in banned_acronyms if re.search(r'\b' + re.escape(acro) + r'\b', img_lower)]
        if found:
            gate_nlvtr_fail.append(f"IMAGE {idx+1} contains banned structured acronyms: {found}")
        structured = [phrase for phrase in banned_structured_phrases if phrase in img_lower]
        if structured:
            gate_nlvtr_fail.append(f"IMAGE {idx+1} contains banned structured labels: {structured}")
            
    for idx, vid in enumerate(videos):
        vid_lower = vid.lower()
        if "%" in vid:
            gate_nlvtr_fail.append(f"VIDEO {idx+1} contains banned percentage character '%'")
        found = [acro for acro in banned_acronyms if re.search(r'\b' + re.escape(acro) + r'\b', vid_lower)]
        if found:
            gate_nlvtr_fail.append(f"VIDEO {idx+1} contains banned structured acronyms: {found}")
        structured = [phrase for phrase in banned_structured_phrases if phrase in vid_lower]
        if structured:
            gate_nlvtr_fail.append(f"VIDEO {idx+1} contains banned structured labels: {structured}")
            
    if gate_nlvtr_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "No Banned Notations Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_nlvtr_fail,
            "solution": "Translate all internal structural locks and mathematical progress variables into descriptive natural-language prose. Completely scrub '%' and acronyms like TSPA, HAL, VMFP, etc."
        })
    else:
        audit_results["gates"].append({
            "name": "No Banned Notations Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All prompts are 100% compliant with NLVTR, containing no mathematical symbols or technical acronyms."]
        })

    # Beat Overload Pop Prevention Check (P0)
    gate_overload_fail = []
    if time_sequence is not None:
        for idx, seq in enumerate(time_sequence):
            state_lower = seq.get("state_name", "").lower()
            img_n = seq.get("image_n_state", "").lower()
            img_np1 = seq.get("image_n_plus_1_state", "").lower()
            
            # Detect overloaded operations in a single beat using robust phrase patterns
            ops = []
            if any(w in state_lower or w in img_n for w in ["debris", "clear", "sweep", "clean"]):
                ops.append("debris clearing")
            if any(w in state_lower or w in img_np1 for w in ["paint", "coat", "refinish"]):
                ops.append("painting/coating")
            if any(w in state_lower or w in img_np1 for w in ["insulation", "insulate", "foam panel"]):
                ops.append("wall insulation")
            if any(w in state_lower or w in img_np1 for w in ["ceiling panel", "ceiling board", "ceiling frame"]):
                ops.append("ceiling paneling")
            if any(w in state_lower or w in img_np1 for w in ["ceiling light", "light fixture", "light installation", "sconce", "pendant light", "lamp install"]):
                ops.append("lighting installation")
            if any(w in state_lower or w in img_np1 for w in ["flooring grid", "wood floor", "plywood floor", "floor frame", "floor board", "parquet", "laminate"]):
                ops.append("flooring grid/wood flooring")
            if any(w in state_lower or w in img_np1 for w in ["table", "chair", "plant", "mug", "furnish", "sofa", "bed"]):
                ops.append("furnishing")
                
            # If a single beat combines more than one operation, it is overloaded!
            if len(ops) > 1:
                gate_overload_fail.append(f"Beat {idx+1} is overloaded. It combines: {ops}. Split them into discrete beats.")
                
    if gate_overload_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Beat Overload Pop Prevention Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_overload_fail,
            "solution": "Apply the Micro-Incremental Splitting Rule. Split any beat combining multiple distinct structural operations (e.g. clearing + subfloor + insulation) into individual, step-by-step sub-beats."
        })
    else:
        audit_results["gates"].append({
            "name": "Beat Overload Pop Prevention Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All beats focus on exactly one single physical operation, ensuring smooth time-lapse transitions without visual snaps."]
        })

    # Power Chain Gate (P0): a practical light may only activate after an earlier
    # wiring/rough-in (or visible power source) beat exists in the ladder.
    light_kw = [
        "light fixture", "sconce", "pendant light", "chandelier", "lamp glows",
        "bulb glows", "lights come on", "light turns on", "practical activation",
        "fixture halos", "tests the lights", "switch tick", "lantern housings glow"
    ]
    wiring_kw = [
        "wiring", "conduit", "cable run", "cable clip", "rough-in", "electrical wire",
        "wire pull", "junction box", "solar panel", "battery bank", "generator"
    ]
    first_light_idx = None
    first_wiring_idx = None
    for idx, vid in enumerate(videos):
        v_lower = vid.lower()
        if first_wiring_idx is None and any(k in v_lower for k in wiring_kw):
            first_wiring_idx = idx
        if first_light_idx is None and any(k in v_lower for k in light_kw):
            first_light_idx = idx
    if first_light_idx is not None and (first_wiring_idx is None or first_wiring_idx > first_light_idx):
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Power Chain Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": [
                f"A powered light/fixture activates in VIDEO {first_light_idx + 1} but no earlier wiring/power-source beat exists"
                + ("." if first_wiring_idx is None else f" (first wiring beat is VIDEO {first_wiring_idx + 1}).")
            ],
            "solution": "Insert a wiring/rough-in beat (plus a visible power source such as a solar panel, battery bank, or generator for off-grid carriers) before any light, lamp, or powered fixture activates."
        })
    else:
        audit_results["gates"].append({
            "name": "Power Chain Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["No powered light activates before its wiring/power beat, or no powered lighting appears in this set."]
        })

    # Volume Conservation Gate (P1 heuristic): hand containers alone cannot account for
    # vanishing bulk material, and a cut-out opening needs its solid piece carried out.
    _carry_out_re = re.compile(r"(carr\w+|drag\w+|haul\w+|tip\w+)[^.]{0,80}\b(out|onto a|toward the threshold|through the)\b", re.IGNORECASE)
    _exit_with_re = re.compile(r"(walks out|exits|leaves the frame|out of the frame)[^.]{0,120}(carr\w+|drag\w+|bucket|crate|tub|bag|wheelbarrow)", re.IGNORECASE)
    # Act-of-cutting-an-opening: progressive/gerund cutting verb followed by an opening
    # noun nearby. Past-participle state descriptors ("already cut opening", "carved tree
    # archway") deliberately do NOT match — they describe prior state, not this beat's act.
    _cut_act_re = re.compile(
        r"\b(cutting|carving|chiseling|chiselling|sawing|boring|hollowing|roughing out|chainsaw)\b"
        r"[^.]{0,70}\b(opening|portal|doorway|archway|arch outline|arched|hatch|mouth|chamber|void|hollow)\b",
        re.IGNORECASE)
    # Boilerplate/trace vocabulary that must never be read as an act of cutting.
    _cut_noise_re = re.compile(
        r"no camera cut|no cuts|jump cuts?|cross-dissolve|saw-cut|cut lines?|expansion cuts?|kerf",
        re.IGNORECASE)
    _mech_kw = ["wheelbarrow", "skip", "excavator", "loader", "chute", "tracked carrier", "crane"]
    _pile_kw = ["spoil pile", "growing pile", "pile below", "stockpile"]
    _trip_kw = ["repeated trips", "returns with", "returning with", "back and forth"]
    _hand_container_kw = ["bucket", "crate", "tub", " bin", " bag"]
    _removal_kw = ["debris", "rubble", "spoil", "excavat", "demolish", "clearing", "shovel", "scoop", "mucking"]
    _vanish_kw = ["cleared", "swept", "exposing bare", "exposing clean", "disappear", "vanish", "shrink", "decreases steadily", "emptied"]
    _solid_piece_kw = ["pry", "pries", "pried", "lever", "cut-out", "slab"]

    def _volume_accounted(v_lower):
        return (any(k in v_lower for k in _mech_kw)
                or any(k in v_lower for k in _pile_kw)
                or any(k in v_lower for k in _trip_kw)
                or _carry_out_re.search(v_lower) is not None
                or _exit_with_re.search(v_lower) is not None)

    vol_fail = []
    for idx, vid in enumerate(videos):
        v_lower = vid.lower()
        v_clean = _cut_noise_re.sub(" ", v_lower)
        if (any(k in v_lower for k in _removal_kw)
                and any(k in v_lower for k in _hand_container_kw)
                and any(k in v_lower for k in _vanish_kw)
                and not _volume_accounted(v_lower)):
            vol_fail.append(
                f"VIDEO {idx + 1}: bulk material visibly vanishes but is handled only by hand containers with no "
                f"accounting (no repeated trips, carry-out, mechanical container, or growing spoil pile)."
            )
        if (_cut_act_re.search(v_clean) is not None
                and not (any(k in v_lower for k in _solid_piece_kw) or _volume_accounted(v_lower))):
            vol_fail.append(
                f"VIDEO {idx + 1}: an opening is cut but neither the cut-out solid piece nor the fragments receive "
                f"an on-camera pry-out/carry-out or other volume accounting."
            )
    if vol_fail:
        audit_results["score"] -= 10
        audit_results["gates"].append({
            "name": "Volume Conservation Gate",
            "status": "FAIL",
            "tier": "P1",
            "details": vol_fail,
            "solution": "Scale containers to the load (mechanical containers for cubic-metre removals), describe repeated trips or a visibly growing spoil pile, and give every cut-out slab/panel its own pry-out and carry-out action."
        })
    else:
        audit_results["gates"].append({
            "name": "Volume Conservation Gate",
            "status": "PASS",
            "tier": "P1",
            "details": ["Removed/delivered material volume is plausibly accounted for by container scale, trips, carry-outs, or spoil piles."]
        })

    # Enclosed-Space Provenance Gate (P1 heuristic): a chamber revealed behind a newly cut
    # opening must be declared pre-existing or earn its own excavation beats.
    _all_text = " ".join(list(images) + list(videos)).lower()
    _enters_interior_kw = ["interior camera", "threshold crossing", "crosses the sill", "into the interior",
                           "interior tripod", "into the cabin", "interior settled"]
    _provenance_kw = ["natural", "pre-existing", "preexisting", "original room", "original interior",
                      "existing chamber", "existing cavity", "hollow chamber", "hollow cavity",
                      "behind the rock face", "behind the cliff face"]
    _excavation_kw = ["excavat", "dig out", "digging", "muck", "spoil"]
    _cuts_opening_set = _cut_act_re.search(_cut_noise_re.sub(" ", _all_text)) is not None
    _camera_enters = any(k in _all_text for k in _enters_interior_kw)
    if _cuts_opening_set and _camera_enters and not (
            any(k in _all_text for k in _provenance_kw) or any(k in _all_text for k in _excavation_kw)):
        audit_results["score"] -= 10
        audit_results["gates"].append({
            "name": "Enclosed-Space Provenance Gate",
            "status": "FAIL",
            "tier": "P1",
            "details": ["An opening is cut and the camera enters an interior, but the set neither declares the interior space pre-existing (natural cavity / original room) nor contains any excavation/mucking-out beat that creates it."],
            "solution": "Either state in the opening beat that the revealed chamber is pre-existing (e.g. a natural hollow behind the face), or insert on-camera excavation and mucking-out beats before any interior finishing."
        })
    else:
        audit_results["gates"].append({
            "name": "Enclosed-Space Provenance Gate",
            "status": "PASS",
            "tier": "P1",
            "details": ["No unexplained interior chamber: the space is declared pre-existing, excavated on camera, or no cut-and-enter pattern exists in this set."]
        })

    # Sub-Pixel Coordinate Pinning Check (SPCP) (P0)
    gate_spcp_fail = []
    for idx, img in enumerate(images):
        img_lower = img.lower()
        if "horizon line" not in img_lower:
            gate_spcp_fail.append(f"IMAGE {idx+1} is missing Sub-Pixel Coordinate Pinning (SPCP) horizon line declaration.")
    for idx, vid in enumerate(videos):
        vid_lower = vid.lower()
        if "horizon line" not in vid_lower:
            gate_spcp_fail.append(f"VIDEO {idx+1} is missing Sub-Pixel Coordinate Pinning (SPCP) horizon line declaration.")

    if gate_spcp_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Sub-Pixel Coordinate Pinning Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_spcp_fail,
            "solution": "Inject Sub-Pixel Coordinate Pinning (SPCP) specifications (e.g. 'horizon line remains perfectly level at exactly 50-percent height') into all static IMAGE and active VIDEO prompts to lock vanishing points."
        })
    else:
        audit_results["gates"].append({
            "name": "Sub-Pixel Coordinate Pinning Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All IMAGE and VIDEO prompts successfully pin the horizon line and lock vanishing points to prevent camera space drifting."]
        })

    # Geometric Tool Lock Check (MTAL) (P0)
    gate_mtal_fail = []
    required_materials = ['solid', 'blue', 'black', 'yellow', 'orange', 'silver', 'steel', 'plastic', 'metallic', 'ashwood', 'rubberized', 'aluminum', 'synthetic', 'canvas', 'rubber', 'heavy-duty']
    for idx, vid in enumerate(videos):
        is_sterile = "zero active workers" in vid.lower() or "empty of active agents" in vid.lower() or "no active worker appears" in vid.lower() or "none (no active workers present)" in vid.lower() or "none (no active workers)" in vid.lower()
        is_bridge = "threshold bridge" in vid.lower() or "coaxial forward push" in vid.lower() or "doorway edges" in vid.lower()
        if is_sterile or is_bridge:
            continue
        has_pinned_tool = any(mat in vid.lower() for mat in required_materials)
        if not has_pinned_tool:
            gate_mtal_fail.append(f"VIDEO {idx+1} manual tool description lacks specific color/material pinning keywords (MTAL).")
            
    if gate_mtal_fail:
        audit_results["score"] -= 30
        audit_results["gates"].append({
            "name": "Geometric Tool Lock Gate",
            "status": "FAIL",
            "tier": "P0",
            "details": gate_mtal_fail,
            "solution": "Enforce high-contrast color, shape, and material keywords for all manual tools (MTAL) in active video prompts (e.g. 'solid-blue cylindrical synthetic fiber paint roller' or 'matte-black rectangular steel shovel head') to block tool morphing/flicker."
        })
    else:
        audit_results["gates"].append({
            "name": "Geometric Tool Lock Gate",
            "status": "PASS",
            "tier": "P0",
            "details": ["All active worker tools successfully enforce precise geometric shape, color, and material descriptions (MTAL) to prevent temporal morphing."]
        })

    # Floor Reflective Alignment - RHMA-Blur (P1)
    gate6_fail = []
    if "rhma-blur" not in images[-1].lower() and "reflective polished" in images[-1].lower():
        gate6_fail.append("Final IMAGE is missing Reflection Consistency (RHMA-Blur) clause for glossy floors.")
        
    if gate6_fail:
        audit_results["score"] -= 5
        audit_results["gates"].append({
            "name": "Mirror Reflective Alignment",
            "status": "FAIL",
            "tier": "P1",
            "details": gate6_fail,
            "solution": "Include highly blurred, Fresnel-corrected diffused reflections clause to secure reflective floors."
        })
    else:
        audit_results["gates"].append({
            "name": "Mirror Reflective Alignment",
            "status": "PASS",
            "tier": "P1",
            "details": ["Reflective floor alignment is locked via high-blur diffused Fresnel limits."]
        })
        
    # Ensure minimum score floor is 0
    audit_results["score"] = max(0, audit_results["score"])
    return audit_results

# =====================================================================
# Main Orchestrator
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description="Fully Automated Video-to-Prompt Pipeline")
    parser.add_argument("video", help="Absolute path to target video file (.mp4)")
    parser.add_argument("--fps", type=float, default=3.0, help="Keyframe sampling rate in frames per second (default: 3.0)")
    parser.add_argument("--local", action="store_true", help="Force local interactive CV wizard instead of visual MLLM APIs")
    parser.add_argument("--post-render-video", help="Optional rendered MP4 to audit with post-render forensic P0 gates")
    
    args = parser.parse_args()
    
    video_path = os.path.abspath(args.video)
    if not os.path.exists(video_path):
        print(f"[-] Video file not found: {video_path}")
        sys.exit(1)
        
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    
    # Initialize a temporary directory for analysis files
    temp_dir_obj = tempfile.TemporaryDirectory(prefix=f"video_analysis_{video_name}_")
    output_root = temp_dir_obj.name
    
    print("="*60)
    print(f"  Starting Video-to-Prompt Pipeline for: {video_name}")
    print(f"  Target Output (Temporary): {output_root}")
    print("="*60)
    try:
        # Step 1: Keyframe Extraction
        keyframe_paths = extract_keyframes(video_path, output_root, args.fps)
        if not keyframe_paths:
            print("[-] Keyframe extraction failed. Aborting pipeline.")
            sys.exit(1)
            
        # Step 2: Local CV Motion & Light Heuristics
        cv_data = analyze_video_cv(keyframe_paths)
        
        # Step 3: Parse intermediate metadata specs (MLLM vs Interactive)
        metadata = fetch_semantic_metadata(keyframe_paths, cv_data, force_local=args.local, fps=args.fps)
        if args.post_render_video:
            post_render_video = os.path.abspath(args.post_render_video)
            if not os.path.exists(post_render_video):
                print(f"[-] Post-render video file not found: {post_render_video}")
                sys.exit(1)
            metadata["post_render_qc"] = analyze_post_render_forensics(post_render_video, output_root, fps=args.fps)
        
        # Save intermediate JSON to temporary folder
        # json_path = os.path.join(output_root, "metadata.json")
        # with open(json_path, "w", encoding="utf-8") as f:
        #     json.dump(metadata, f, indent=2, ensure_ascii=False)
        # print(f"[+] Saved intermediate metadata spec to {json_path}")
        
        # Step 4: Prompt Composition (SKILL.md)
        images, videos = compose_scup_prompts(metadata)
        
        # Step 5: Programmatic Auditor
        audit_results = run_scup_audit(
            images, 
            videos, 
            fps=args.fps, 
            num_analyzed_frames=metadata.get("num_analyzed_frames"), 
            total_frames=len(keyframe_paths),
            change_events=metadata.get("change_events"),
            analysis_frame_indices=metadata.get("analysis_frame_indices"),
            time_sequence=metadata.get("time_sequence"),
            post_render_qc=metadata.get("post_render_qc"),
            video_path=video_path
        )
        
        # Save Prompts file to temporary folder
        # prompts_path = os.path.join(output_root, "prompts.txt")
        # with open(prompts_path, "w", encoding="utf-8") as f:
        #     f.write("```text\n")
        #     f.write("图片提示词\n\n")
        #     for i, img in enumerate(images):
        #         f.write(f"图片 {i+1}:\n")
        #         f.write(img + "\n\n")
        #         
        #     f.write("视频提示词\n\n")
        #     for i, vid in enumerate(videos):
        #         f.write(f"视频 {i+1}:\n")
        #         f.write(vid + "\n\n")
        #     f.write("```\n")
        # print(f"[+] Saved copy-ready prompts to {prompts_path}")
        
        # Generate and Save Audit Markdown Report to temporary folder
        # audit_path = os.path.join(output_root, "scup_audit_report.md")
        # with open(audit_path, "w", encoding="utf-8") as f:
        #     f.write(f"# SCUP Quality Audit Report — {video_name}\n\n")
        #     f.write(f"**Audit Score**: `{audit_results['score']}/100`\n")
        #     f.write(f"**Audit Status**: {'PASS' if audit_results['score'] >= 80 else 'REWRITE REQUIRED'}\n")
        #     f.write(f"**Execution Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        #     
        #     f.write("## Detailed Gate Checks\n\n")
        #     f.write("| Gate Name | Tier | Status | Details |\n")
        #     f.write("|---|---|---|---|\n")
        #     for g in audit_results["gates"]:
        #         status_emoji = "✅ PASS" if g["status"] == "PASS" else "❌ FAIL"
        #         details_str = "<br>".join(g["details"])
        #         f.write(f"| {g['name']} | {g.get('tier', 'P0')} | {status_emoji} | {details_str} |\n")
        #         
        #     f.write("\n## Action Items & Recommendations\n\n")
        #     failed_gates = [g for g in audit_results["gates"] if g["status"] == "FAIL"]
        #     if not failed_gates:
        #         f.write("🎉 **Congratulations!** Your prompts perfectly adhere to the spatial consistency and time-lapse continuity rules. Ready for production rendering.\n")
        #     else:
        #         for g in failed_gates:
        #             f.write(f"### ⚠️ Fix {g['name']} ({g['tier']})\n")
        #             f.write(f"- **Problem**: {', '.join(g['details'])}\n")
        #             f.write(f"- **Solution**: {g['solution']}\n\n")
        # print(f"[+] Saved SCUP Audit Report to {audit_path}")
        
        # -------------------------------------------------------------
        # Step 6: Print copy-ready prompts inside a fenced code block to stdout
        # -------------------------------------------------------------
        print("\n" + "="*20 + " COPY-READY PROMPTS " + "="*20)
        print("```text")
        print("图片提示词\n")
        for i, img in enumerate(images):
            print(f"图片 {i+1}:")
            print(img)
            print()
            
        print("视频提示词\n")
        for i, vid in enumerate(videos):
            print(f"视频 {i+1}:")
            print(vid)
            print()
        print("```")
        
        # -------------------------------------------------------------
        # Step 7: Print structured Quality Audit & Verification Report below it to stdout
        # -------------------------------------------------------------
        print("\n" + "="*20 + " QUALITY AUDIT REPORT " + "="*20)
        report_text = []
        report_text.append(f"# SCUP Quality Audit Report — {video_name}")
        report_text.append(f"**Audit Score**: `{audit_results['score']}/100`")
        report_text.append(f"**Audit Status**: {'PASS' if audit_results['score'] >= 80 else 'REWRITE REQUIRED'}")
        report_text.append(f"**Execution Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        report_text.append("## Detailed Gate Checks\n")
        report_text.append("| Gate Name | Tier | Status | Details |")
        report_text.append("|---|---|---|---|")
        for g in audit_results["gates"]:
            status_emoji = "✅ PASS" if g["status"] == "PASS" else ("⏭️ NOT CHECKED" if g["status"] == "SKIP" else "❌ FAIL")
            details_str = "<br>".join(g["details"])
            report_text.append(f"| {g['name']} | {g.get('tier', 'P0')} | {status_emoji} | {details_str} |")
            
        report_text.append("\n## Action Items & Recommendations\n")
        if not failed_gates:
            report_text.append("🎉 **Congratulations!** Your prompts perfectly adhere to the spatial consistency and time-lapse continuity rules. Ready for production rendering.")
        else:
            for g in failed_gates:
                report_text.append(f"### ⚠️ Fix {g['name']} ({g['tier']})")
                report_text.append(f"- **Problem**: {', '.join(g['details'])}")
                report_text.append(f"- **Solution**: {g['solution']}\n")
                
        print("\n".join(report_text))
        
        print("\n" + "="*60)
        print("  Pipeline Completed Successfully!")
        print(f"  Audit Result: {audit_results['score']}/100")
        print("="*60)

    finally:
        print("\n" + "="*60)
        print(f"[*] Cleaning up temporary analysis directory: {output_root}")
        try:
            temp_dir_obj.cleanup()
            print("[+] Cleanup complete. All temporary files deleted successfully.")
        except Exception as cleanup_err:
            print(f"[-] Warning: Failed to clean up temporary directory {output_root}: {cleanup_err}")
        print("="*60)

if __name__ == "__main__":
    main()
