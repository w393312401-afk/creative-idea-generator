import os
import json
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import candidate_selection_pipeline as csp


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
    assert "默认采纳候选 #1" in res["selection_reason"]


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

