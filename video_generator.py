import os
import sys
import json
import time
import shutil
import re
import subprocess
import threading

from server_common import (
    SERVER_CONFIG, resolve_gateway, effective_config,
    OUTPUT_ROOT, SKILL_DIR, _get_project_dir, _safe_project_name,
    ACTIVE_TASKS_LOCK, ACTIVE_TASKS, get_or_create_task,
    notify_listeners, save_tasks_to_disk
)


def _get_google_fx_video_service():
    import sys
    adspower_path = SERVER_CONFIG.get('adspowerPath') or 'C:\\Users\\video\\Desktop\\N8N-main\\Adspower\\AI\\core'
    if adspower_path not in sys.path:
        sys.path.append(adspower_path)
    import services.google_fx
    from services import google_fx_video
    import models
    return google_fx_video, models


def generate_video_sequence(config, title, prompt_block, on_progress=None, target_slots=None):
    images, videos = _parse_prompt_slots(prompt_block)
    project_dir = _get_project_dir(title)
    frames_dir = os.path.join(project_dir, 'frames')
    videos_dir = os.path.join(project_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)

    # Load existing manifest to map slots to frame paths and quality gates
    manifest_path = os.path.join(project_dir, 'manifest.json')
    slot_to_path = {}
    slot_to_quality = {}
    manifest_data = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
            for frame in manifest_data.get('frames', []):
                slot_to_path[frame['slot']] = os.path.join(os.path.dirname(os.path.abspath(__file__)), frame['file'].lstrip('/'))
                slot_to_quality[frame['slot']] = frame.get('quality_gate')
        except Exception as e:
            print(f"Warning: could not read manifest.json ({e})")

    # If manifest doesn't exist or is empty, we can try to guess paths
    if not slot_to_path:
        for i in range(1, len(images) + 1):
            guess_path = os.path.join(frames_dir, f'img_{i:03d}.webp')
            if os.path.exists(guess_path):
                slot_to_path[i] = os.path.abspath(guess_path)

    if not slot_to_path:
        raise RuntimeError('未找到已生成的帧图像。请先生成帧序列！')

    google_fx_video, models = _get_google_fx_video_service()

    video_items = sorted(videos.keys())
    if target_slots is not None:
        target_slots = [int(x) for x in target_slots]
        video_items = [idx for idx in video_items if idx in target_slots]
    else:
        # Full regeneration (not a retry): clear all old video files so
        # breakpoint-resume doesn't reuse stale videos from a previous run.
        # This ensures the UI always shows freshly generated videos.
        if os.path.isdir(videos_dir):
            cleared = 0
            for fname in os.listdir(videos_dir):
                fpath = os.path.join(videos_dir, fname)
                if os.path.isfile(fpath) and fname.lower().endswith('.mp4'):
                    try:
                        os.remove(fpath)
                        cleared += 1
                    except Exception as rm_err:
                        print(f"Warning: could not remove old video {fpath}: {rm_err}")
            if cleared:
                print(f"[INFO] Cleared {cleared} old video file(s) for full regeneration.")
        # Also clear old video entries from manifest
        if 'videos' in manifest_data:
            manifest_data['videos'] = []

    if on_progress:
        on_progress('start', {
            'total': len(video_items),
            'slots': video_items
        })

    video_results = []
    pending_items = []
    
    def save_manifest_incremental():
        existing_videos = manifest_data.get('videos', [])
        video_map = {v['slot']: v for v in existing_videos}
        for v in video_results:
            video_map[v['slot']] = v
            
        merged_videos = []
        for slot_idx in sorted(videos.keys()):
            if slot_idx in video_map:
                merged_videos.append(video_map[slot_idx])
                
        manifest_data['videos'] = merged_videos
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Warning: could not write updated manifest.json ({e})")
    
    video_model = config.get('videoModel') or 'Veo 3.1 - Lite [Lower Priority]'

    import tempfile
    import shutil

    for seq, idx in enumerate(video_items, start=1):
        prompt = videos[idx]
        
        # Automatically map IMAGE N -> IMAGE 1 and IMAGE N+1 -> IMAGE 2
        # to match the 2-card UI in Google Labs FX (Veo)
        import re
        prompt = re.sub(rf'\bimage\s+{idx}\b', 'IMAGE 1', prompt, flags=re.IGNORECASE)
        prompt = re.sub(rf'\bimage\s+{idx + 1}\b', 'IMAGE 2', prompt, flags=re.IGNORECASE)
        prompt = re.sub(rf'图片\s*{idx}\b', 'IMAGE 1', prompt)
        prompt = re.sub(rf'图片\s*{idx + 1}\b', 'IMAGE 2', prompt)

        dest_filename = f'vid_{idx:03d}.mp4'
        dest_path = os.path.join(videos_dir, dest_filename)
        
        # 1. Breakpoint Resume: Check if file already exists and is valid
        # If it is an explicit retry, we bypass this check and delete the existing file.
        is_explicit_retry = target_slots is not None and idx in target_slots
        if is_explicit_retry and os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception as e:
                print(f"Warning: could not remove old video file {dest_path}: {e}")

        if not is_explicit_retry and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            rel_path = os.path.relpath(dest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
            video_info = {
                'slot': idx,
                'sequence': seq,
                'file': rel_path,
                'url': '/' + rel_path,
                'prompt': prompt,
                'model': video_model,
                'status': 'success'
            }
            video_results.append(video_info)
            if on_progress:
                on_progress('video_done', {
                    'index': idx,
                    'current': seq,
                    'total': len(video_items),
                    'video': video_info
                })
            continue

        start_frame_path = slot_to_path.get(idx)
        end_frame_path = slot_to_path.get(idx + 1)
        
        err_msg = None
        if not start_frame_path or not os.path.exists(start_frame_path):
            err_msg = f"视频 {idx} 所需的起始帧 IMAGE {idx} 不存在。请重新生成该帧！"
        elif not end_frame_path or not os.path.exists(end_frame_path):
            err_msg = f"视频 {idx} 所需的结束帧 IMAGE {idx+1} 不存在。请重新生成该帧！"
        else:
            start_quality = slot_to_quality.get(idx)
            end_quality = slot_to_quality.get(idx + 1)
            if start_quality == 'i2i_fallback_degraded' or end_quality == 'i2i_fallback_degraded':
                err_msg = (
                    f"视频 {idx} 的起始帧 IMAGE {idx} 或结束帧 IMAGE {idx+1} 属于降级帧（i2i fallback degraded），"
                    f"已拦截该段视频生成以防止画面跳变。请重新生成并修复受损帧。"
                )

        if err_msg:
            video_info = {
                'slot': idx,
                'sequence': seq,
                'file': '',
                'url': '',
                'prompt': prompt,
                'model': video_model,
                'status': 'failed',
                'error': err_msg
            }
            video_results.append(video_info)
            save_manifest_incremental()
            if on_progress:
                on_progress('video_error', {
                    'index': idx,
                    'current': seq,
                    'total': len(video_items),
                    'message': err_msg
                })
            continue

        temp_out_dir = tempfile.mkdtemp()
        req = models.VideoRequest(
            prompt=prompt,
            image=start_frame_path,
            end_image=end_frame_path,
            model=video_model,
            ratio=config.get('imageAspectRatio') or '9:16',  # FIX ratio
            output_path=temp_out_dir
        )
        pending_items.append({
            'idx': idx,
            'seq': seq,
            'req': req,
            'dest_path': dest_path,
            'temp_out_dir': temp_out_dir,
            'prompt': prompt
        })

    if pending_items:
        reqs_list = [item['req'] for item in pending_items]
        
        def batch_progress_cb(batch_idx, stage, details):
            if not on_progress:
                return
            item = pending_items[batch_idx]
            if stage == 'video_start':
                on_progress('video_start', {
                    'index': item['idx'],
                    'current': item['seq'],
                    'total': len(video_items)
                })
            elif stage == 'video_done':
                # Move the generated file to final destination
                generated_path = details.get('video_url')
                if generated_path and os.path.exists(generated_path):
                    shutil.move(generated_path, item['dest_path'])
                    rel_path = os.path.relpath(item['dest_path'], os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
                    video_info = {
                        'slot': item['idx'],
                        'sequence': item['seq'],
                        'file': rel_path,
                        'url': '/' + rel_path,
                        'prompt': item['prompt'],
                        'model': video_model,
                        'status': 'success'
                    }
                    video_results.append(video_info)
                    save_manifest_incremental()
                    on_progress('video_done', {
                        'index': item['idx'],
                        'current': item['seq'],
                        'total': len(video_items),
                        'video': video_info
                    })
                else:
                    on_progress('video_error', {
                        'index': item['idx'],
                        'current': item['seq'],
                        'total': len(video_items),
                        'message': '生成的视频文件不存在'
                    })
            elif stage == 'video_error':
                # Per-segment failure isolation: record failed status and continue
                video_info = {
                    'slot': item['idx'],
                    'sequence': item['seq'],
                    'file': '',
                    'url': '',
                    'prompt': item['prompt'],
                    'model': video_model,
                    'status': 'failed',
                    'error': details.get('message') or '生成失败'
                }
                video_results.append(video_info)
                save_manifest_incremental()
                on_progress('video_error', {
                    'index': item['idx'],
                    'current': item['seq'],
                    'total': len(video_items),
                    'message': details.get('message') or '生成失败'
                })

        def cancel_check_cb():
            if on_progress:
                try:
                    # Trigger a dummy call to check if the connection is dead
                    return on_progress('cancel_check', None)
                except Exception:
                    return True
            return False

        try:
            google_fx_video.generate_videos_batch_google_fx(
                reqs_list,
                on_progress=batch_progress_cb,
                cancel_check=cancel_check_cb
            )
        finally:
            # Clean up all temp directories
            for item in pending_items:
                try:
                    shutil.rmtree(item['temp_out_dir'], ignore_errors=True)
                except:
                    pass

    # Final merge and save of manifest
    save_manifest_incremental()

    manifest_data['manifest'] = '/' + os.path.relpath(manifest_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
    manifest_data['project_dir'] = os.path.abspath(project_dir)
    return manifest_data


def merge_project_videos(project_dir):
    manifest_path = os.path.join(project_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        return None
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest_data = json.load(f)
        
    videos = manifest_data.get('videos', [])
    # Filter and sort by slot index
    video_files = []
    # Make sure we only check files that exist
    for v in sorted(videos, key=lambda x: x.get('slot', 0)):
        if v.get('status') == 'success' and v.get('file'):
            abs_path = os.path.abspath(v['file'].lstrip('/'))
            if not os.path.exists(abs_path):
                abs_path = os.path.abspath(os.path.join(project_dir, 'videos', os.path.basename(v['file'])))
            if os.path.exists(abs_path):
                video_files.append(abs_path)
                
    if not video_files:
        return None
        
    # Write concat list to project directory
    concat_list_path = os.path.join(project_dir, 'concat_list.txt')
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for vf in video_files:
            safe_path = vf.replace('\\', '/')
            f.write(f"file '{safe_path}'\n")
            
    # Determine the Chinese theme name to use for the output filename
    title = manifest_data.get('title', '')
    chinese_name = ""
    
    # 1. Try to find the theme in library.json
    library_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'library.json')
    if os.path.exists(library_path):
        try:
            with open(library_path, 'r', encoding='utf-8') as lf:
                lib_data = json.load(lf)
            if isinstance(lib_data, list):
                for item in lib_data:
                    if item.get('title') == title:
                        theme = item.get('theme', '')
                        theme_chinese = "".join(re.findall(r'[\u4e00-\u9fa5]+', theme))
                        if theme_chinese:
                            chinese_name = theme_chinese
                            break
        except Exception as le:
            print(f"Warning: could not read library.json for theme lookup ({le})")
            
    # 2. Fallback: extract Chinese characters from title
    if not chinese_name and title:
        title_chinese = "".join(re.findall(r'[\u4e00-\u9fa5]+', title))
        if title_chinese:
            chinese_name = title_chinese
            
    # 3. Fallback: use sanitized project folder name if no Chinese characters found
    if not chinese_name:
        chinese_name = _safe_project_name(title)
        
    output_filename = f"{chinese_name}_2x.mp4"
    output_path = os.path.join(project_dir, output_filename)

    # Clean up any old merged files in the project root to prevent duplicate files
    if os.path.exists(project_dir):
        for fname in os.listdir(project_dir):
            if fname.lower().endswith('.mp4') and os.path.isfile(os.path.join(project_dir, fname)):
                try:
                    os.remove(os.path.join(project_dir, fname))
                except Exception as e:
                    print(f"Warning: could not remove old merged file {fname} ({e})")
    
    # Check if the first video has audio
    has_audio = False
    if len(video_files) > 0:
        first_video = video_files[0]
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            first_video
        ]
        try:
            import subprocess
            res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            if "audio" in res.stdout.lower():
                has_audio = True
        except Exception as probe_err:
            print(f"[DEBUG] ffprobe check failed: {probe_err}")
            
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path
    ]
    
    if has_audio:
        cmd.extend([
            "-filter_complex", "[0:v]setpts=0.5*PTS[v];[0:a]atempo=2.0[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:a", "aac"
        ])
    else:
        cmd.extend([
            "-filter_complex", "[0:v]setpts=0.5*PTS[v]",
            "-map", "[v]"
        ])
        
    cmd.extend([
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_path
    ])
    
    print(f"[INFO] Merging {len(video_files)} videos to {output_path} (has_audio={has_audio})...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    try:
        os.remove(concat_list_path)
    except:
        pass
        
    if res.returncode == 0:
        rel_path = os.path.relpath(output_path, os.path.dirname(os.path.abspath(__file__))).replace('\\', '/')
        file_size = os.path.getsize(output_path)
        
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                output_path
            ]
            dur_res = subprocess.run(dur_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            duration = float(dur_res.stdout.strip())
        except Exception as dur_err:
            print(f"[DEBUG] ffprobe duration check failed: {dur_err}")
            
        return {
            'file': rel_path,
            'url': '/' + rel_path,
            'size_bytes': file_size,
            'duration_seconds': round(duration, 2),
            'status': 'success'
        }
    else:
        print(f"[ERROR] ffmpeg merge failed with code {res.returncode}: {res.stderr}")
        raise RuntimeError(f"FFmpeg merge failed: {res.stderr}")


def video_reverse_worker(task_id, temp_video_path, temp_dir_obj, fps, api, prompt_style, client_config, filename):
    t = get_or_create_task(task_id)
    output_root = temp_dir_obj.name
    
    def on_progress(stage, details):
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        with ACTIVE_TASKS_LOCK:
            t["events"].append(('progress', {'stage': stage, 'details': details}))
        notify_listeners(task_id, 'progress', {'stage': stage, 'details': details})

    try:
        # Import video_to_prompt_pipeline from skill root
        if str(SKILL_DIR) not in sys.path:
            sys.path.append(str(SKILL_DIR))
        import video_to_prompt_pipeline
        
        # Step 1: Keyframe Extraction
        on_progress('keyframe_extraction', '正在提取视频关键帧...')
        keyframe_paths = video_to_prompt_pipeline.extract_keyframes(temp_video_path, output_root, fps)
        if not keyframe_paths:
            raise RuntimeError("关键帧提取失败。请确保视频文件有效且 FFmpeg 环境正常。")

        # Step 2: Local CV Motion & Light Heuristics
        on_progress('cv_analysis', '正在使用计算机视觉算法分析运动与光照变化...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        cv_data = video_to_prompt_pipeline.analyze_video_cv(keyframe_paths)

        # Step 3: Fetch semantic metadata from Multimodal LLM
        on_progress('semantic_metadata', '大模型多模态视频分析与时序语义提取中...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        
        old_gemini_key = os.environ.get("GEMINI_API_KEY")
        gemini_key = client_config.get("apiKey") or os.environ.get("GEMINI_API_KEY")
        current_gemini_key = client_config.get("apiKey") or gemini_key
        if current_gemini_key:
            os.environ["GEMINI_API_KEY"] = current_gemini_key

        try:
            client_base_url = client_config.get("baseUrl")
            client_model = client_config.get("model")
            metadata = video_to_prompt_pipeline.fetch_semantic_metadata(
                keyframe_paths, cv_data, force_local=False, fps=fps, base_url=client_base_url, model=client_model
            )
        finally:
            if old_gemini_key is not None:
                os.environ["GEMINI_API_KEY"] = old_gemini_key
            elif "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]

        if not metadata or "time_sequence" not in metadata:
            raise RuntimeError("大模型多模态视频分析失败，请检查 API 密钥、网络连接或稍后重试。")

        # Step 4: Prompt Composition & Audit
        on_progress('prompt_composition', '正在合成 SCUP 提示词并进行物理一致性审计...')
        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        images, videos = video_to_prompt_pipeline.compose_scup_prompts(metadata, clean_mode=(prompt_style == "clean"))

        if t["cancel_event"].is_set():
            raise ConnectionError("Video analysis cancelled by user")
        audit_results = video_to_prompt_pipeline.run_scup_audit(
            images,
            videos,
            fps=fps,
            num_analyzed_frames=metadata.get("num_analyzed_frames"),
            total_frames=len(keyframe_paths),
            change_events=metadata.get("change_events"),
            analysis_frame_indices=metadata.get("analysis_frame_indices"),
            time_sequence=metadata.get("time_sequence"),
            post_render_qc=metadata.get("post_render_qc"),
            video_path=temp_video_path
        )

        # Build Markdown Audit report
        video_name = os.path.splitext(filename)[0]
        failed_gates = [g for g in audit_results["gates"] if g["status"] == "FAIL"]
        
        report_lines = [
            f"# SCUP Quality Audit Report — {video_name}",
            f"**Audit Score**: `{audit_results['score']}/100`",
            f"**Audit Status**: {'PASS' if audit_results['score'] >= 80 else 'REWRITE REQUIRED'}\n",
            "## Detailed Gate Checks\n",
            "| Gate Name | Tier | Status | Details |",
            "|---|---|---|---|"
        ]
        for g in audit_results["gates"]:
            status_emoji = "✅ PASS" if g["status"] == "PASS" else "❌ FAIL"
            details_str = "<br>".join(g["details"])
            report_lines.append(f"| {g['name']} | {g.get('tier', 'P0')} | {status_emoji} | {details_str} |")
            
        report_lines.append("\n## Action Items & Recommendations\n")
        if not failed_gates:
            report_lines.append("🎉 **Congratulations!** Your prompts perfectly adhere to the spatial consistency and time-lapse continuity rules. Ready for production rendering.")
        else:
            for g in failed_gates:
                report_lines.append(f"### ⚠️ Fix {g['name']} ({g['tier']})")
                report_lines.append(f"- **Problem**: {', '.join(g['details'])}")
                report_lines.append(f"- **Solution**: {g['solution']}\n")
                
        audit_md = "\n".join(report_lines)

        # Format prompts lists
        images_list = [{"n": i+1, "text": img} for i, img in enumerate(images)]
        videos_list = [{"n": i+1, "text": vid} for i, vid in enumerate(videos)]

        raw_text = f"===TITLE===\n视频反推提示词 ({video_name})\n\n===THEME===\n从视频分析反推\n\n===PROMPTS===\n图片提示词\n--------------------------------------------------\n"
        for i, img in enumerate(images):
            raw_text += f"图片 {i+1}:\n{img}\n\n"
        raw_text += "--------------------------------------------------\n视频提示词\n--------------------------------------------------\n"
        for i, vid in enumerate(videos):
            raw_text += f"视频 {i+1}:\n{vid}\n\n"
        raw_text += f"--------------------------------------------------\n===AUDIT===\n{audit_md}"

        # Copy collage file to outputs directory if it was generated
        collage_src = os.path.splitext(temp_video_path)[0] + "_collage.jpg"
        collage_url = None
        if os.path.exists(collage_src):
            try:
                os.makedirs(OUTPUT_ROOT, exist_ok=True)
                import time
                dest_filename = f"reverse_{int(time.time())}_{video_name}_collage.jpg"
                dest_path = os.path.join(OUTPUT_ROOT, dest_filename)
                shutil.copy(collage_src, dest_path)
                collage_url = f"/outputs/{dest_filename}"
                print(f"[+] Saved keyframe collage to persistent outputs: {dest_path}")
            except Exception as e:
                print(f"[-] Failed to copy keyframe collage to outputs: {e}")

        # Model label selection
        model_label = "Gemini-1.5-Flash"
        openai_key = os.environ.get("OPENAI_API_KEY")
        if api == "openai" or (api == "auto" and not gemini_key and openai_key):
            model_label = "GPT-4o-Mini"

        result = {
            "images": images_list,
            "videos": videos_list,
            "audit_md": audit_md,
            "prompt_block": raw_text,
            "title": f"视频反推提示词 ({video_name})",
            "model": model_label,
            "collage_url": collage_url,
            "image_count": len(images_list),
            "video_count": len(videos_list),
            "timings": {}
        }

        with ACTIVE_TASKS_LOCK:
            t["status"] = "completed"
            t["result"] = result
            t["events"].append(('result', result))

        notify_listeners(task_id, 'result', result)

    except ConnectionError:
        with ACTIVE_TASKS_LOCK:
            t["status"] = "cancelled"
            t["error"] = "用户取消了视频反推"
            t["events"].append(('error', {'message': "用户取消了视频反推"}))
        notify_listeners(task_id, 'error', {'message': "用户取消了视频反推"})
    except Exception as e:
        if sys.stdout:
            import traceback
            print(f"[DEBUG] Video reverse background task {task_id} failed: {e}")
            traceback.print_exc()
        error_msg = str(e)
        with ACTIVE_TASKS_LOCK:
            t["status"] = "failed"
            t["error"] = error_msg
            t["events"].append(('error', {'message': error_msg}))
        notify_listeners(task_id, 'error', {'message': error_msg})
    finally:
        # Cleanup files
        try:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
            temp_dir_obj.cleanup()
        except Exception as ce:
            print(f"[DEBUG] Cleanup error: {ce}")
        save_tasks_to_disk()


