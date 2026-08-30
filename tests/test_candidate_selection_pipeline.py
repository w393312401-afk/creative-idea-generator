import os
import json
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import candidate_selection_pipeline as csp


def _full_subscores(**overrides):
    scores = {key: maximum for _, key, maximum in csp._CANDIDATE_SCORE_FIELDS}
    scores.update(overrides)
    return scores


@pytest.fixture
def temp_project(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    outputs_dir = os.path.join(temp_dir, "outputs")
    project_name = "test_candidate_project"
    project_dir = os.path.join(outputs_dir, project_name)
    frames_dir = os.path.join(project_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    monkeypatch.setattr(csp, "_get_project_dir", lambda name: os.path.join(outputs_dir, name))
    
    yield {
        "temp_dir": temp_dir,
        "outputs_dir": outputs_dir,
        "project_name": project_name,
        "project_dir": project_dir,
        "frames_dir": frames_dir
    }
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_evaluate_and_select_best_candidate_json_parsing(temp_project):
    """Test AI evaluation parser when LLM returns structured JSON."""
    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    
    cand_paths = []
    for i in range(1, 5):
        p = os.path.join(cand_dir, f"candidate_{i}.webp")
        with open(p, "wb") as f:
            f.write(b"fake_image_bytes")
        cand_paths.append(p)

    fake_vlm_response = json.dumps({
        "candidates": [
            {
                "index": 1,
                "score": 75,
                "strengths": "好角度",
                "defects": "光影稍生硬"
            },
            {
                "index": 2,
                "score": 92,
                "strengths": "空间纵深极佳，材质自然无塑料感，严格遵循提示词",
                "defects": "无明显瑕疵"
            },
            {
                "index": 3,
                "score": 80,
                "strengths": "细节丰富",
                "defects": "空间略有膨胀"
            },
            {
                "index": 4,
                "score": 68,
                "strengths": "对比度高",
                "defects": "边缘畸变"
            }
        ],
        "best_index": 2,
        "selection_reason": "候选 #2 在空间三维透视、材质真实感及提示词还原度上表现最为均衡优秀。"
    })

    with patch("candidate_selection_pipeline._multimodal_chat", return_value=fake_vlm_response):
        res = csp.evaluate_and_select_best_candidate(
            config={},
            prompt_text="A cozy underground wooden cabin workshop",
            reference_path=None,
            candidate_paths=cand_paths,
            seq=1
        )

    assert res["best_index"] == 2
    assert len(res["candidates"]) == 4
    assert res["candidates"][1]["score"] == 92
    assert "候选 #2" in res["selection_reason"]


def test_evaluate_and_select_best_candidate_fallback(temp_project):
    """Test fallback logic when VLM chat fails or returns non-JSON."""
    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    
    cand_paths = []
    for i in range(1, 5):
        p = os.path.join(cand_dir, f"candidate_{i}.webp")
        with open(p, "wb") as f:
            f.write(b"fake_image_bytes")
        cand_paths.append(p)

    with patch("candidate_selection_pipeline._multimodal_chat", side_effect=Exception("VLM rate limit")):
        res = csp.evaluate_and_select_best_candidate(
            config={},
            prompt_text="A cozy underground wooden cabin workshop",
            reference_path=None,
            candidate_paths=cand_paths,
            seq=1
        )

    assert res["best_index"] == 1
    assert len(res["candidates"]) == 4
    assert all(candidate["score"] is None for candidate in res["candidates"])
    assert all(candidate["score_source"] == "evaluation_unavailable" for candidate in res["candidates"])
    assert "默认采纳候选 #1" in res["selection_reason"]


def test_structured_scores_are_summed_and_winner_is_chosen_server_side(temp_project):
    cand_paths = []
    for i in range(1, 3):
        path = os.path.join(temp_project["frames_dir"], f"candidate_{i}.webp")
        with open(path, "wb") as f:
            f.write(b"fake")
        cand_paths.append(path)

    response = json.dumps({
        "best_index": 1,
        "selection_reason": "模型偏好候选1",
        "candidates": [
            {
                "index": 1,
                "subscores": _full_subscores(),
                "hard_flags": ["plastic_human"],
                "strengths": "构图完整",
                "defects": "人物像塑料",
            },
            {
                "index": 2,
                "subscores": _full_subscores(core_milestone=8, camera_fidelity=6),
                "hard_flags": [],
                "strengths": "人物真实",
                "defects": "工序略少",
            },
        ],
    })
    with patch.object(csp, "_multimodal_chat", return_value=response):
        result = csp.evaluate_and_select_best_candidate(
            {}, "prompt", None, cand_paths, 1
        )

    first, second = result["candidates"]
    assert first["raw_score"] == 100
    assert first["score"] == 69
    assert first["score_cap"] == 69
    assert second["score"] == 94
    assert result["best_index"] == 2
    assert result["model_best_index"] == 1
    assert result["scoring_version"] == "structured-16-v1"


def test_disqualified_candidate_cannot_win_even_with_higher_score():
    result = csp._normalize_candidate_evaluation({
        "best_index": 1,
        "candidates": [
            {"index": 1, "subscores": _full_subscores(),
             "hard_flags": ["ghost_structure_revival"]},
            {"index": 2, "subscores": _full_subscores(core_milestone=4),
             "hard_flags": []},
        ],
    }, 2)

    assert result["candidates"][0]["disqualified"] is True
    assert result["best_index"] == 2
    assert result["all_candidates_disqualified"] is False


def test_subscores_are_bounded_before_totaling():
    result = csp._normalize_candidate_evaluation({
        "candidates": [{
            "index": 1,
            "subscores": _full_subscores(core_milestone=999, physical_delta=-5),
            "hard_flags": ["unknown_flag"],
        }],
    }, 1)

    candidate = result["candidates"][0]
    assert candidate["subscores"]["core_milestone"] == 12
    assert candidate["subscores"]["physical_delta"] == 0
    assert candidate["score"] == 92
    assert candidate["hard_flags"] == []


def test_run_candidate_selection_frame_sequence(temp_project):
    """Test end-to-end multi-step chain execution with candidate selection."""
    prompt_block = """IMAGE 1:
Prompt: Initial excavation pit in forest ground.
Negative: blur, low quality.

IMAGE 2:
Prompt: Timber framing structure erected inside pit.
Negative: blur, low quality.
"""

    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, **kwargs):
        cand_dir = os.path.join(temp_project["frames_dir"], "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        paths = []
        for i in range(1, candidate_count + 1):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            with open(p, "wb") as f:
                f.write(f"fake_image_bytes_{seq}_{i}".encode('utf-8'))
            paths.append(p)
        return paths

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        # Pick candidate 2 for frame 1, candidate 3 for frame 2
        best_idx = 2 if seq == 1 else 3
        return {
            "best_index": best_idx,
            "selection_reason": f"AI selected candidate #{best_idx}",
            "candidates": [
                {
                    "index": i + 1,
                    "score": 90 if (i + 1) == best_idx else 70,
                    "strengths": f"Strength {i+1}",
                    "defects": "None",
                }
                for i in range(len(candidate_paths))
            ]
        }

    progress_events = []
    def on_progress(stage, data):
        progress_events.append((stage, data))

    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):

        result = csp.run_candidate_selection_frame_sequence(
            config={},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            on_progress=on_progress,
            candidate_count=4
        )

    assert "frames" in result
    assert len(result["frames"]) == 2

    # Check frame 1
    f1 = result["frames"][0]
    assert f1["sequence"] == 1
    assert f1["chosen_candidate_index"] == 2
    assert len(f1["candidates"]) == 4
    assert os.path.exists(os.path.join(temp_project["frames_dir"], "img_001.webp"))

    # Check frame 2
    f2 = result["frames"][1]
    assert f2["sequence"] == 2
    assert f2["chosen_candidate_index"] == 3
    assert len(f2["candidates"]) == 4
    assert os.path.exists(os.path.join(temp_project["frames_dir"], "img_002.webp"))

    # Verify manifest on disk
    manifest_path = os.path.join(temp_project["project_dir"], "manifest.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert len(manifest["frames"]) == 2
    assert manifest["frames"][0]["chosen_candidate_index"] == 2


def test_switch_frame_candidate(temp_project):
    """Test manually switching a frame to another candidate."""
    # Setup initial manifest with candidates
    seq = 1
    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    
    cand_files = []
    for i in range(1, 5):
        p = os.path.join(cand_dir, f"candidate_{i}.webp")
        with open(p, "wb") as f:
            f.write(f"candidate_content_{i}".encode('utf-8'))
        cand_files.append(f"frames/candidates/frame_001/candidate_{i}.webp")

    # Initial frame file copied from candidate 1
    main_frame_path = os.path.join(temp_project["frames_dir"], "img_001.webp")
    shutil.copyfile(os.path.join(temp_project["project_dir"], cand_files[0]), main_frame_path)

    manifest_data = {
        "project": temp_project["project_name"],
        "frames": [
            {
                "sequence": 1,
                "file": "frames/img_001.webp",
                "chosen_candidate_index": 1,
                "candidates": [
                    {
                        "index": i + 1,
                        "file": cand_files[i],
                        "url": f"/outputs/{temp_project['project_name']}/{cand_files[i]}",
                        "is_chosen": (i == 0)
                    }
                    for i in range(4)
                ]
            }
        ]
    }
    
    manifest_path = os.path.join(temp_project["project_dir"], "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    with patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        updated_manifest = csp.switch_frame_candidate(
            title=temp_project["project_name"],
            seq=1,
            new_candidate_index=4
        )

    assert updated_manifest["frames"][0]["chosen_candidate_index"] == 4
    assert updated_manifest["frames"][0]["candidates"][3]["is_chosen"] is True
    assert updated_manifest["frames"][0]["candidates"][0]["is_chosen"] is False

    # Check that main frame file has been updated with candidate 4 content
    with open(main_frame_path, "rb") as f:
        content = f.read()
    assert content == b"candidate_content_4"


def test_api_generate_frames_selection_routing(monkeypatch, temp_project):
    """Test server routing for /api/generate_frames_selection endpoint."""
    import server

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/generate_frames_selection'
    h.headers = {'content-type': 'application/json'}
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {
        'title': temp_project['project_name'],
        'prompt_block': 'IMAGE 1:\nPrompt: test\n',
        'config': {}
    }

    monkeypatch.setattr(server, 'access_ok', lambda self: True)
    monkeypatch.setattr(server, 'rate_ok', lambda ip, k: True)
    monkeypatch.setattr(server, 'prompt_delivery_block_reason', lambda body: None)
    monkeypatch.setattr(server, '_require_fx_admission', lambda self, is_fx: True)
    monkeypatch.setattr(server, 'resolve_cover_reference', lambda c, t, k: True)
    monkeypatch.setattr(server, 'claim_frame_run', lambda p, tid: None)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(server, 'get_or_create_task', lambda tid, meta=None: {'cancel_event': MagicMock()})

    with patch('threading.Thread') as mock_thread:
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        h.do_POST()

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert 'task_id' in body
        assert body['task_id'].startswith('frames_sel_')
        assert mock_thread.called
        assert mock_thread.call_args[1]['target'] == server.generate_frames_selection_worker


def test_api_generate_frames_standard_routing_ignores_old_manifest(monkeypatch, temp_project):
    """Test that /api/generate_frames with standard mode routes to generate_frames_worker
    even if the project previously had candidate_selection in manifest.json."""
    import server

    # Pre-populate manifest with candidate_selection
    manifest_path = os.path.join(temp_project['project_dir'], 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({'generation_mode': 'candidate_selection', 'frames': []}, f)

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/generate_frames'
    h.headers = {'content-type': 'application/json'}
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {
        'title': temp_project['project_name'],
        'prompt_block': 'IMAGE 1:\nPrompt: test\n',
        'generation_mode': 'standard',
        'candidate_selection': False,
        'config': {'candidateSelectionMode': False}
    }

    monkeypatch.setattr(server, 'access_ok', lambda self: True)
    monkeypatch.setattr(server, 'rate_ok', lambda ip, k: True)
    monkeypatch.setattr(server, 'prompt_delivery_block_reason', lambda body: None)
    monkeypatch.setattr(server, '_require_fx_admission', lambda self, is_fx: True)
    monkeypatch.setattr(server, 'resolve_cover_reference', lambda c, t, k: True)
    monkeypatch.setattr(server, 'claim_frame_run', lambda p, tid: None)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(server, 'get_or_create_task', lambda tid, meta=None: {'cancel_event': MagicMock()})

    with patch('threading.Thread') as mock_thread:
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        h.do_POST()

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert mock_thread.called
        assert mock_thread.call_args[1]['target'] == server.generate_frames_worker


def test_api_generate_frames_candidate_selection_routing(monkeypatch, temp_project):
    """Test that /api/generate_frames with candidate_selection mode routes to generate_frames_selection_worker."""
    import server

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/generate_frames'
    h.headers = {'content-type': 'application/json'}
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {
        'title': temp_project['project_name'],
        'prompt_block': 'IMAGE 1:\nPrompt: test\n',
        'generation_mode': 'candidate_selection',
        'candidate_selection': True,
        'config': {}
    }

    monkeypatch.setattr(server, 'access_ok', lambda self: True)
    monkeypatch.setattr(server, 'rate_ok', lambda ip, k: True)
    monkeypatch.setattr(server, 'prompt_delivery_block_reason', lambda body: None)
    monkeypatch.setattr(server, '_require_fx_admission', lambda self, is_fx: True)
    monkeypatch.setattr(server, 'resolve_cover_reference', lambda c, t, k: True)
    monkeypatch.setattr(server, 'claim_frame_run', lambda p, tid: None)
    monkeypatch.setattr(server, 'cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(server, 'get_or_create_task', lambda tid, meta=None: {'cancel_event': MagicMock()})

    with patch('threading.Thread') as mock_thread:
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        h.do_POST()

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert mock_thread.called
        assert mock_thread.call_args[1]['target'] == server.generate_frames_selection_worker


def test_api_switch_candidate_routing(monkeypatch, temp_project):
    """Test server routing for /api/switch_candidate endpoint."""
    import server

    h = object.__new__(server.SparkRequestHandler)
    h.path = '/api/switch_candidate'
    h.headers = {'content-type': 'application/json'}
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))
    h._read_json_body = lambda: {
        'title': temp_project['project_name'],
        'sequence': 1,
        'candidate_index': 3,
    }

    monkeypatch.setattr(server, 'access_ok', lambda self: True)
    
    with patch('candidate_selection_pipeline.switch_frame_candidate', return_value={'frames': [{'sequence': 1, 'chosen_candidate_index': 3}]}) as mock_switch:
        h.do_POST()

        assert len(sent) == 1
        body, status = sent[0]
        assert status == 200
        assert body['status'] == 'ok'
        assert body['manifest']['frames'][0]['chosen_candidate_index'] == 3
        mock_switch.assert_called_once_with(temp_project['project_name'], 1, 3)


def test_generate_frame_candidates_fx_uuid_preservation(temp_project):
    """Test that Google FX batch generation extracts UUIDs, saves raw JPGs, and records metadata."""
    from PIL import Image

    dummy_dir = tempfile.mkdtemp()
    fake_fx_files = []
    fake_uuids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
    ]
    for idx, u in enumerate(fake_uuids):
        fpath = os.path.join(dummy_dir, f"fx_batch_123_{idx}_{u}.jpg")
        img = Image.new("RGB", (64, 64), color="blue")
        img.save(fpath, "JPEG")
        fake_fx_files.append(fpath)

    mock_fx_service = MagicMock()
    mock_fx_service._generate_images_batch_google_fx.return_value = {
        "status": "success",
        "image_urls": fake_fx_files,
        "project_url": "https://labs.google/fx/tools/flow/project/test-proj-123"
    }

    with patch("candidate_selection_pipeline._get_google_fx_image_service", return_value=(mock_fx_service, None)), \
         patch("candidate_selection_pipeline._fx_image_model", return_value="imagen-3.0"):

        cands = csp.generate_frame_candidates(
            config={"imageBackend": "google_fx"},
            title=temp_project["project_name"],
            item={"prompt": "test excavation"},
            reference_path=None,
            seq=1,
            candidate_count=4,
            project_url=None,
            frames_dir=temp_project["frames_dir"]
        )

    assert len(cands) == 4
    for c in cands:
        assert os.path.exists(c)

    # Check candidates_meta.json
    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    meta_json = os.path.join(cand_dir, "candidates_meta.json")
    assert os.path.exists(meta_json)
    with open(meta_json, "r", encoding="utf-8") as f:
        meta = json.load(f)

    assert meta["project_url"] == "https://labs.google/fx/tools/flow/project/test-proj-123"
    assert len(meta["candidates"]) == 4
    for idx, c_meta in enumerate(meta["candidates"]):
        assert c_meta["fx_uuid"] == fake_uuids[idx]
        assert os.path.exists(c_meta["raw_src"])


def test_switch_frame_candidate_updates_fx_src_and_uuid(temp_project):
    """Test that switching a candidate updates fx_src with the new candidate's UUID."""
    from PIL import Image

    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    
    cand_uuid_2 = "22222222-2222-2222-2222-222222222222"
    cand_uuid_3 = "33333333-3333-3333-3333-333333333333"

    cand_2_webp = os.path.join(cand_dir, "candidate_2.webp")
    cand_3_webp = os.path.join(cand_dir, "candidate_3.webp")
    cand_3_raw = os.path.join(cand_dir, f"candidate_3_{cand_uuid_3}.jpg")

    for p in (cand_2_webp, cand_3_webp):
        Image.new("RGB", (64, 64), color="green").save(p, "WEBP")
    Image.new("RGB", (64, 64), color="red").save(cand_3_raw, "JPEG")

    # Initial main frame
    main_frame = os.path.join(temp_project["frames_dir"], "img_001.webp")
    shutil.copyfile(cand_2_webp, main_frame)

    meta_payload = {
        "sequence": 1,
        "project_url": "https://labs.google/fx/tools/flow/project/test-proj-123",
        "candidates": [
            {"index": 2, "path": cand_2_webp, "raw_src": cand_2_webp, "fx_uuid": cand_uuid_2},
            {"index": 3, "path": cand_3_webp, "raw_src": cand_3_raw, "fx_uuid": cand_uuid_3},
        ]
    }
    with open(os.path.join(cand_dir, "candidates_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_payload, f)

    manifest_data = {
        "project": temp_project["project_name"],
        "project_url": "https://labs.google/fx/tools/flow/project/test-proj-123",
        "frames": [
            {
                "sequence": 1,
                "file": "frames/img_001.webp",
                "fx_uuid": cand_uuid_2,
                "chosen_candidate_index": 2,
                "candidates": [
                    {"index": 2, "file": "frames/candidates/frame_001/candidate_2.webp", "fx_uuid": cand_uuid_2, "is_chosen": True},
                    {"index": 3, "file": "frames/candidates/frame_001/candidate_3.webp", "fx_uuid": cand_uuid_3, "is_chosen": False},
                ]
            }
        ]
    }
    manifest_path = os.path.join(temp_project["project_dir"], "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    with patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        updated_manifest = csp.switch_frame_candidate(temp_project["project_name"], seq=1, new_candidate_index=3)

    assert updated_manifest["frames"][0]["chosen_candidate_index"] == 3
    assert updated_manifest["frames"][0]["fx_uuid"] == cand_uuid_3

    # Check fx_src directory
    fx_src_file = csp._fx_find_ref_for(temp_project["frames_dir"], seq=2)
    assert fx_src_file is not None
    assert cand_uuid_3 in fx_src_file


def test_partial_sequence_regeneration_stale_lineage(temp_project):
    """Test regenerating a subset of frames updates stale_lineage correctly."""
    prompt_block = """IMAGE 1:
Prompt: Frame 1
IMAGE 2:
Prompt: Frame 2
IMAGE 3:
Prompt: Frame 3
"""

    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, **kwargs):
        cand_dir = os.path.join(temp_project["frames_dir"], "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        paths = []
        for i in range(1, candidate_count + 1):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            with open(p, "wb") as f:
                f.write(f"fake_image_bytes_{seq}_{i}".encode('utf-8'))
            paths.append(p)
        return paths

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 1,
            "selection_reason": "test selection",
            "candidates": [{"index": i + 1, "score": 85, "strengths": "ok", "defects": ""} for i in range(len(candidate_paths))]
        }

    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):

        # 1. Run all 3 frames
        res1 = csp.run_candidate_selection_frame_sequence(
            config={},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            candidate_count=4
        )
        assert len(res1["frames"]) == 3
        for f in res1["frames"]:
            assert "stale_lineage" not in f

        # 2. Regenerate only Frame 2
        res2 = csp.run_candidate_selection_frame_sequence(
            config={},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            target_sequences=[2],
            candidate_count=4
        )
        # Frame 3 was after frame 2 and not regenerated, so it should be marked stale_lineage
        assert len(res2["frames"]) == 3
        assert res2["frames"][0].get("stale_lineage") is not True  # Frame 1
        assert res2["frames"][1].get("stale_lineage") is not True  # Frame 2 (regenerated)
        assert res2["frames"][2].get("stale_lineage") is True      # Frame 3 (downstream stale)


def test_single_canvas_project_url_propagation(temp_project):
    """Test that project_url established in frame 1 is propagated to frame 2 and manifest."""
    prompt_block = """IMAGE 1:
Prompt: First step on canvas.
IMAGE 2:
Prompt: Second step on same canvas.
"""
    passed_project_urls = []

    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, project_url=None, **kwargs):
        passed_project_urls.append((seq, project_url))
        cand_dir = os.path.join(temp_project["frames_dir"], "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        paths = []
        for i in range(1, candidate_count + 1):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            with open(p, "wb") as f:
                f.write(f"fake_data_{seq}_{i}".encode('utf-8'))
            paths.append(p)
        # Mock writing candidates_meta.json with project_url returned from Flow
        meta_payload = {
            "sequence": seq,
            "project_url": "https://labs.google/fx/tools/flow/project/flow-proj-12345",
            "candidates": [{"index": i, "path": p, "fx_uuid": f"uuid-{seq}-{i}"} for i, p in enumerate(paths, 1)],
        }
        with open(os.path.join(cand_dir, "candidates_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta_payload, f)
        return paths

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 1,
            "selection_reason": "test",
            "candidates": [{"index": i + 1, "score": 90, "strengths": "ok", "defects": ""} for i in range(len(candidate_paths))]
        }

    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):

        result = csp.run_candidate_selection_frame_sequence(
            config={},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            candidate_count=4
        )

    # Frame 1 started with None, Frame 2 received the project_url from Frame 1
    assert passed_project_urls[0] == (1, None)
    assert passed_project_urls[1] == (2, "https://labs.google/fx/tools/flow/project/flow-proj-12345")

    # Manifest contains project_url and google_fx_project_url
    manifest_path = os.path.join(temp_project["project_dir"], "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    assert m.get("project_url") == "https://labs.google/fx/tools/flow/project/flow-proj-12345"
    assert m.get("google_fx_project_url") == "https://labs.google/fx/tools/flow/project/flow-proj-12345"
    assert result["frames"][0]["fx_project_url"] == "https://labs.google/fx/tools/flow/project/flow-proj-12345"
    assert result["frames"][1]["fx_project_url"] == "https://labs.google/fx/tools/flow/project/flow-proj-12345"





def _fx_service_stub(project_url, seen_reqs, status="success"):
    """Stub Google FX service that records every ImageBatchRequest it receives."""
    from PIL import Image

    def _fake_batch(req):
        seen_reqs.append(req)
        out_dir = req.output_path
        os.makedirs(out_dir, exist_ok=True)
        files = []
        for idx in range(4):
            uuid = f"{idx}{idx}{idx}{idx}{idx}{idx}{idx}{idx}-1111-1111-1111-111111111111"
            fpath = os.path.join(out_dir, f"fx_{idx}_{uuid}.jpg")
            Image.new("RGB", (32, 32), color="green").save(fpath, "JPEG")
            files.append(fpath)
        return {"status": status, "image_urls": files, "project_url": project_url}

    svc = MagicMock()
    svc._generate_images_batch_google_fx.side_effect = _fake_batch
    return svc


def _run_fx_candidates(temp_project, seqs, project_url, canvas_state, seen_reqs, status="success"):
    svc = _fx_service_stub(project_url, seen_reqs, status=status)
    with patch("candidate_selection_pipeline._get_google_fx_image_service", return_value=(svc, None)), \
         patch("candidate_selection_pipeline._fx_image_model", return_value="imagen-3.0"):
        for seq in seqs:
            csp.generate_frame_candidates(
                config={"imageBackend": "google_fx"},
                title=temp_project["project_name"],
                item={"prompt": f"beat {seq}"},
                reference_path=None,
                seq=seq,
                candidate_count=4,
                frames_dir=temp_project["frames_dir"],
                canvas_state=canvas_state,
            )


def test_canvas_state_binds_once_and_reuses_project_url(temp_project):
    """第 1 帧新建画布，后续帧只透传 project_url、绝不再要求新画布，且不再换号。"""
    seen = []
    state = {}
    _run_fx_candidates(temp_project, [1, 2, 3], "https://labs.google/fx/tools/flow/project/p-1", state, seen)

    assert [r.require_fresh_canvas for r in seen] == [True, False, False]
    assert [r.project_url for r in seen] == [
        None,
        "https://labs.google/fx/tools/flow/project/p-1",
        "https://labs.google/fx/tools/flow/project/p-1",
    ]
    # 画布已绑定 → 锁号，换号会脱离这块画布
    assert [r.allow_account_switch for r in seen] == [True, False, False]
    assert state["project_url"] == "https://labs.google/fx/tools/flow/project/p-1"
    assert state["opened"] is True


def test_canvas_reused_on_route_less_flow_variant(temp_project):
    """没有 /project/ 路由的 Flow 变体：拿不到 project_url 也不能每帧重开画布。"""
    seen = []
    state = {}
    _run_fx_candidates(temp_project, [1, 2, 3], None, state, seen)

    assert [r.require_fresh_canvas for r in seen] == [True, False, False]
    assert state.get("project_url") is None
    assert state["opened"] is True


def test_failed_canvas_keeps_requiring_fresh_canvas(temp_project):
    """画布没开成的失败不得记账，否则下一帧会跑进上一个任务的画布。"""
    seen = []
    state = {}
    _run_fx_candidates(temp_project, [1, 2], None, state, seen, status="failed")

    assert [r.require_fresh_canvas for r in seen] == [True, True]
    assert not state.get("opened")


def test_subset_rerun_without_binding_still_opens_own_canvas(temp_project):
    """子集重跑（不含第 1 帧）且 manifest 无绑定时，首帧仍必须新建画布。"""
    seen = []
    state = {}
    _run_fx_candidates(temp_project, [5, 6], "https://labs.google/fx/tools/flow/project/p-9", state, seen)

    assert [r.require_fresh_canvas for r in seen] == [True, False]


def test_candidate_selection_skips_existing_frames_and_resumes(temp_project):
    """整单 4选1 模式下自动跳过已存在的帧，从未生成的槽位开始生成，并以前一帧为底图。"""
    prompt_block = """IMAGE 1:
Prompt: First step done.
IMAGE 2:
Prompt: Second step to generate.
IMAGE 3:
Prompt: Third step to generate.
"""
    # 模拟第 1 帧已经生成并落盘
    img1_path = os.path.join(temp_project["frames_dir"], "img_001.webp")
    with open(img1_path, "wb") as f:
        f.write(b"fake_existing_frame_1")

    # 预设 manifest 中第 1 帧记录
    manifest_path = os.path.join(temp_project["project_dir"], "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "title": temp_project["project_name"],
            "frames": [{
                "sequence": 1,
                "slot": 1,
                "file": "frames/img_001.webp",
                "url": "/frames/img_001.webp",
                "prompt": "First step done.",
            }]
        }, f)

    generated_seqs = []
    received_references = {}

    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, **kwargs):
        generated_seqs.append(seq)
        received_references[seq] = reference
        cand_dir = os.path.join(temp_project["frames_dir"], "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        paths = []
        for i in range(1, candidate_count + 1):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            with open(p, "wb") as f:
                f.write(f"cand_{seq}_{i}".encode('utf-8'))
            paths.append(p)
        return paths

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 1,
            "selection_reason": f"good_{seq}",
            "candidates": [{"index": i + 1, "score": 90, "strengths": "ok", "defects": ""} for i in range(len(candidate_paths))]
        }

    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):

        res = csp.run_candidate_selection_frame_sequence(
            config={},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            target_sequences=None,
            candidate_count=4
        )

    # 1. 验证第 1 帧被跳过，只生成了第 2 帧和第 3 帧
    assert generated_seqs == [2, 3]
    # 2. 验证第 2 帧以已存在的第 1 帧作为图生图参考底图
    assert received_references[2] == img1_path
    # 3. 验证最终 manifest 中包含全部 3 帧
    assert len(res["frames"]) == 3
    assert res["frames"][0]["sequence"] == 1
    assert res["frames"][1]["sequence"] == 2
    assert res["frames"][2]["sequence"] == 3


def test_candidate_concurrency_config_and_bounds():
    """测试 candidate_concurrency 解析与边界夹取。"""
    assert csp.candidate_concurrency({}) == 4
    assert csp.candidate_concurrency(None) == 4
    assert csp.candidate_concurrency({'candidateConcurrency': 2}) == 2
    assert csp.candidate_concurrency({'candidateConcurrency': 8}) == 8
    assert csp.candidate_concurrency({'candidateConcurrency': 16}) == 8
    assert csp.candidate_concurrency({'candidateConcurrency': 0}) == 4  # 0 or falsy defaults to candidate_count
    assert csp.candidate_concurrency({'candidateConcurrency': 1}) == 1
    assert csp.candidate_concurrency({'candidateConcurrency': 'invalid'}) == 4


def test_generate_frame_candidates_api_concurrent_execution(temp_project):
    """测试 4选1 模式 API 候选图默认并发生成。"""
    events = []
    def on_progress(stage, data):
        events.append((stage, data))

    called_prompts = []
    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        called_prompts.append(p_variant)
        with open(out_path, 'wb') as f:
            f.write(b'fake_api_candidate_bytes')

    with patch.object(csp, '_generate_single_api_candidate', side_effect=fake_make_cand):
        cands = csp.generate_frame_candidates(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            item={"prompt": "A modern underground cabin"},
            reference_path=None,
            seq=1,
            candidate_count=4,
            on_progress=on_progress,
            frames_dir=temp_project["frames_dir"]
        )

    assert len(cands) == 4
    assert len(called_prompts) == 4
    # 验证候选图文件名与内容
    for idx, cp in enumerate(cands, start=1):
        assert os.path.exists(cp)
        assert f"candidate_{idx}.webp" in cp
        with open(cp, 'rb') as f:
            assert f.read() == b'fake_api_candidate_bytes'

    # 验证 variation 提示词后缀
    assert any("[Variation #2]" in p for p in called_prompts)
    assert any("[Variation #3]" in p for p in called_prompts)
    assert any("[Variation #4]" in p for p in called_prompts)

    # 验证 candidates_meta.json 元数据文件落盘
    cand_dir = os.path.join(temp_project["frames_dir"], "candidates", "frame_001")
    meta_path = os.path.join(cand_dir, "candidates_meta.json")
    assert os.path.exists(meta_path)
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta_data = json.load(f)
    assert len(meta_data["candidates"]) == 4
    assert [c["index"] for c in meta_data["candidates"]] == [1, 2, 3, 4]

    # 验证并发进度广播事件
    gen_events = [d for s, d in events if s == 'candidate_generating']
    assert len(gen_events) >= 1
    assert any("并发生成" in (d.get('message') or '') for d in gen_events)


def test_generate_frame_candidates_api_serial_fallback(temp_project):
    """测试 candidateConcurrency=1 时走串行兜底通道。"""
    called_prompts = []
    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        called_prompts.append(p_variant)
        with open(out_path, 'wb') as f:
            f.write(b'fake_serial_bytes')

    with patch.object(csp, '_generate_single_api_candidate', side_effect=fake_make_cand):
        cands = csp.generate_frame_candidates(
            config={"imageBackend": "api", "candidateConcurrency": 1},
            title=temp_project["project_name"],
            item={"prompt": "A modern cabin"},
            reference_path=None,
            seq=1,
            candidate_count=4,
            frames_dir=temp_project["frames_dir"]
        )

    assert len(cands) == 4
    assert len(called_prompts) == 4


def test_generate_frame_candidates_api_partial_failure_resilience(temp_project):
    """测试并发生成部分候选失败时，依然保留并采纳成功的候选图。"""
    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        if "Variation #3" in p_variant:
            raise RuntimeError("API timeout on candidate 3")
        with open(out_path, 'wb') as f:
            f.write(b'fake_partial_bytes')

    with patch.object(csp, '_generate_single_api_candidate', side_effect=fake_make_cand):
        cands = csp.generate_frame_candidates(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            item={"prompt": "A modern cabin"},
            reference_path=None,
            seq=1,
            candidate_count=4,
            frames_dir=temp_project["frames_dir"]
        )

    # 候选 1, 2, 4 成功生成，候选 3 失败，总共保留 3 张
    assert len(cands) == 3
    assert not any("candidate_3.webp" in cp for cp in cands)


def test_generate_frame_candidates_api_all_fail_raises(temp_project):
    """测试所有候选并发生成均失败时向上抛出 RuntimeError。"""
    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        raise RuntimeError("Gateway 502 Bad Gateway")

    with patch.object(csp, '_generate_single_api_candidate', side_effect=fake_make_cand):
        with pytest.raises(RuntimeError) as exc_info:
            csp.generate_frame_candidates(
                config={"imageBackend": "api"},
                title=temp_project["project_name"],
                item={"prompt": "A modern cabin"},
                reference_path=None,
                seq=1,
                candidate_count=4,
                frames_dir=temp_project["frames_dir"]
            )
        assert "失败" in str(exc_info.value)


def test_generate_frame_candidates_api_cancel_check(temp_project):
    """测试候选图并发生成过程中响应取消事件。"""
    def progress_cb(stage, data):
        if stage == 'cancel_check':
            return True
        return False

    with pytest.raises(ConnectionError) as exc_info:
        csp.generate_frame_candidates(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            item={"prompt": "A modern cabin"},
            reference_path=None,
            seq=1,
            candidate_count=4,
            on_progress=progress_cb,
            frames_dir=temp_project["frames_dir"]
        )
    assert "取消" in str(exc_info.value)


def _fake_fx_batch(uuids):
    """造一批带 UUID 文件名的假 FX 产出。"""
    from PIL import Image
    d = tempfile.mkdtemp()
    files = []
    for idx, u in enumerate(uuids):
        p = os.path.join(d, f"fx_batch_9_{idx}_{u}.jpg")
        Image.new("RGB", (64, 64), color="green").save(p, "JPEG")
        files.append(p)
    return files


def test_short_fx_batch_is_not_topped_up_with_api_candidates(temp_project):
    """FX 少回几张时不拿 API 候选补齐。

    API 候选没有画布 tile（fx_uuid=None）。补进来一旦被 4选1 选中，这一帧就脱离
    Flow 血统，下一帧的链式参考只能靠重新上传接链——为多一个候选赔掉整条下游链的
    画布连续性。2026-08-23 IMG 017 就是这么坏的。
    """
    uuids = ["aaaaaaaa-1111-1111-1111-111111111111",
             "bbbbbbbb-2222-2222-2222-222222222222",
             "cccccccc-3333-3333-3333-333333333333"]
    svc = MagicMock()
    svc._generate_images_batch_google_fx.return_value = {
        "status": "partial", "image_urls": _fake_fx_batch(uuids),
        "project_url": "https://labs.google/fx/tools/flow/project/p1",
    }

    with patch("candidate_selection_pipeline._get_google_fx_image_service", return_value=(svc, None)), \
         patch("candidate_selection_pipeline._fx_image_model", return_value="imagen-3.0"), \
         patch("candidate_selection_pipeline._generate_single_api_candidate") as api_gen:
        cands = csp.generate_frame_candidates(
            config={"imageBackend": "google_fx"},
            title=temp_project["project_name"],
            item={"prompt": "p"}, reference_path=None, seq=1, candidate_count=4,
            project_url=None, frames_dir=temp_project["frames_dir"],
        )

    assert api_gen.call_count == 0, "FX 已经给出候选时不该再叫 API 补齐"
    assert len(cands) == 3

    meta_json = os.path.join(temp_project["frames_dir"], "candidates", "frame_001",
                             "candidates_meta.json")
    with open(meta_json, encoding="utf-8") as f:
        meta = json.load(f)
    # 留下来的每一张都带画布 UUID —— 无论选中哪张，下一帧都能直接挂 tile
    assert [c["fx_uuid"] for c in meta["candidates"]] == uuids


def test_empty_fx_batch_still_falls_back_to_api(temp_project):
    """FX 彻底空手是"有帧/没帧"的区别，仍然要退回 API 把这一帧渲出来。"""
    from PIL import Image
    svc = MagicMock()
    svc._generate_images_batch_google_fx.return_value = {
        "status": "failed", "image_urls": [], "project_url": None,
    }

    def _make(config, prompt_text, reference_path, out_path, is_text_only, ctrl_prompt):
        Image.new("RGB", (64, 64), color="red").save(out_path, "WEBP")

    with patch("candidate_selection_pipeline._get_google_fx_image_service", return_value=(svc, None)), \
         patch("candidate_selection_pipeline._fx_image_model", return_value="imagen-3.0"), \
         patch("candidate_selection_pipeline._generate_single_api_candidate", side_effect=_make) as api_gen:
        cands = csp.generate_frame_candidates(
            config={"imageBackend": "google_fx"},
            title=temp_project["project_name"],
            item={"prompt": "p"}, reference_path=None, seq=1, candidate_count=4,
            project_url=None, frames_dir=temp_project["frames_dir"],
        )

    assert api_gen.call_count == 4
    assert len(cands) == 4


def test_frame_1_never_uses_history_as_reference_in_candidate_selection(temp_project):
    """验证当 img_001.webp 已存在且重渲第1帧时，绝不能把历史 img_001.webp 误作为参考图传入。"""
    from PIL import Image
    frames_dir = temp_project["frames_dir"]
    img_1_path = os.path.join(frames_dir, "img_001.webp")
    Image.new("RGB", (64, 64), color="blue").save(img_1_path, "WEBP")
    assert os.path.exists(img_1_path) and os.path.getsize(img_1_path) > 0

    prompt_block = "IMAGE 1:\nPrompt: Fresh frame 1 prompt.\nNegative: blur."

    passed_references = []
    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, **kwargs):
        passed_references.append((seq, reference))
        cand_dir = os.path.join(frames_dir, "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        paths = []
        for i in range(1, candidate_count + 1):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            Image.new("RGB", (64, 64), color="green").save(p, "WEBP")
            paths.append(p)
        return paths

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 1,
            "selection_reason": "AI picked candidate 1",
            "candidates": [{"index": i + 1, "score": 90, "strengths": "", "defects": ""} for i in range(len(candidate_paths))]
        }

    # 1. 测试 API 后端重渲第 1 帧 (target_sequences=[1])
    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        csp.run_candidate_selection_frame_sequence(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            target_sequences=[1]
        )

    assert len(passed_references) == 1
    assert passed_references[0][0] == 1
    assert passed_references[0][1] is None, f"第 1 帧重渲时 reference 必须为 None，实际为: {passed_references[0][1]}"

    # 2. 测试 Google FX 后端重渲第 1 帧
    passed_references.clear()
    with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        csp.run_candidate_selection_frame_sequence(
            config={"imageBackend": "google_fx"},
            title=temp_project["project_name"],
            prompt_block=prompt_block,
            target_sequences=[1]
        )

    assert len(passed_references) == 1
    assert passed_references[0][0] == 1
    assert passed_references[0][1] is None, f"Google FX 第 1 帧重渲时 reference 必须为 None，实际为: {passed_references[0][1]}"
