import os
import json
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import candidate_selection_pipeline as csp
import frame_generator as fg


@pytest.fixture
def temp_project():
    tmp = tempfile.mkdtemp()
    proj_name = "test_accum_proj"
    proj_dir = os.path.join(tmp, "outputs", proj_name)
    frames_dir = os.path.join(proj_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    with patch("candidate_selection_pipeline._get_project_dir", return_value=proj_dir), \
         patch("frame_generator._get_project_dir", return_value=proj_dir):
        yield {
            "root": tmp,
            "project_name": proj_name,
            "project_dir": proj_dir,
            "frames_dir": frames_dir,
        }
    shutil.rmtree(tmp, ignore_errors=True)


def test_candidate_generation_accumulates_across_rounds_without_overwriting(temp_project):
    """测试多轮生成同一槽位候选图时，候选图文件递增编号累积，不覆盖也不删除已有文件。"""
    frames_dir = temp_project["frames_dir"]
    seq = 1

    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        with open(out_path, "wb") as f:
            f.write(f"fake_image_bytes_for_{os.path.basename(out_path)}".encode("utf-8"))

    with patch.object(csp, "_generate_single_api_candidate", side_effect=fake_make_cand):
        # 第 1 轮：生成 4 张候选图 (1..4)
        cands_round1 = csp.generate_frame_candidates(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            item={"prompt": "Ancient ruins excavation"},
            reference_path=None,
            seq=seq,
            candidate_count=4,
            frames_dir=frames_dir
        )
        assert len(cands_round1) == 4
        assert [os.path.basename(p) for p in cands_round1] == [
            "candidate_1.webp", "candidate_2.webp", "candidate_3.webp", "candidate_4.webp"
        ]

        # 检查 candidates_meta.json
        meta_file = os.path.join(frames_dir, "candidates", f"frame_{seq:03d}", "candidates_meta.json")
        assert os.path.exists(meta_file)
        with open(meta_file, "r", encoding="utf-8") as f:
            meta1 = json.load(f)
        assert len(meta1["candidates"]) == 4
        assert [c["index"] for c in meta1["candidates"]] == [1, 2, 3, 4]

        # 第 2 轮（例如重试/再次生成）：再生成 4 张候选图 (应顺延为 5..8，且 1..4 完好保留)
        cands_round2 = csp.generate_frame_candidates(
            config={"imageBackend": "api"},
            title=temp_project["project_name"],
            item={"prompt": "Ancient ruins excavation improved"},
            reference_path=None,
            seq=seq,
            candidate_count=4,
            frames_dir=frames_dir
        )
        assert len(cands_round2) == 4
        assert [os.path.basename(p) for p in cands_round2] == [
            "candidate_5.webp", "candidate_6.webp", "candidate_7.webp", "candidate_8.webp"
        ]

        # 验证 1..8 全量文件均存在于池中，未被删除或覆盖
        cand_dir = os.path.join(frames_dir, "candidates", f"frame_{seq:03d}")
        for i in range(1, 9):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            assert os.path.exists(p)
            with open(p, "rb") as f:
                content = f.read().decode("utf-8")
            assert f"candidate_{i}.webp" in content

        # 检查 candidates_meta.json 包含全量 8 张候选元数据
        with open(meta_file, "r", encoding="utf-8") as f:
            meta2 = json.load(f)
        assert len(meta2["candidates"]) == 8
        assert [c["index"] for c in meta2["candidates"]] == list(range(1, 9))


def test_manifest_accumulates_candidates_and_allows_switching_across_batches(temp_project):
    """测试 manifest.json 全量累积候选池，并在不同轮次的候选之间自由切换。"""
    prompt_block = "IMAGE 1:\nPrompt: Excavation entry\n"
    frames_dir = temp_project["frames_dir"]

    def fake_gen_candidates(config, title, item, reference, seq, candidate_count=4, **kwargs):
        cand_dir = os.path.join(frames_dir, "candidates", f"frame_{seq:03d}")
        os.makedirs(cand_dir, exist_ok=True)
        meta_file = os.path.join(cand_dir, "candidates_meta.json")
        existing_meta = []
        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as f:
                existing_meta = json.load(f).get("candidates", [])
        start_idx = len(existing_meta) + 1
        new_paths = []
        new_meta = []
        for i in range(start_idx, start_idx + candidate_count):
            p = os.path.join(cand_dir, f"candidate_{i}.webp")
            with open(p, "wb") as f:
                f.write(f"candidate_content_{i}".encode("utf-8"))
            new_paths.append(p)
            new_meta.append({"index": i, "path": p, "raw_src": p, "fx_uuid": f"uuid-{i}"})
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump({"sequence": seq, "candidates": existing_meta + new_meta}, f)
        return new_paths

    def fake_eval_round1(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 2,  # Batch relative best (index 2)
            "selection_reason": "Round 1 candidate 2 is best",
            "candidates": [{"index": i + 1, "score": 80 + i, "strengths": f"s{i+1}", "defects": ""} for i in range(len(candidate_paths))]
        }

    def fake_eval_round2(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 3,  # Batch relative best (index 3 of second batch = candidate 7)
            "selection_reason": "Round 2 candidate 7 is best",
            "candidates": [{"index": i + 1, "score": 88 + i, "strengths": f"s_round2_{i+1}", "defects": ""} for i in range(len(candidate_paths))]
        }

    with patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        # 1. 运行第 1 轮 4选1 生成
        with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
             patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval_round1):
            res1 = csp.run_candidate_selection_frame_sequence(
                config={},
                title=temp_project["project_name"],
                prompt_block=prompt_block,
                candidate_count=4
            )

        f1 = res1["frames"][0]
        assert f1["chosen_candidate_index"] == 2
        assert len(f1["candidates"]) == 4
        assert [c["index"] for c in f1["candidates"]] == [1, 2, 3, 4]
        assert f1["candidates"][1]["is_chosen"] is True
        assert f1["candidates"][0]["is_chosen"] is False

        # 2. 针对第 1 帧再次触发重选/重渲 (模拟定向修复或重跑)
        with patch.object(csp, "generate_frame_candidates", side_effect=fake_gen_candidates), \
             patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval_round2):
            res2 = csp.run_candidate_selection_frame_sequence(
                config={},
                title=temp_project["project_name"],
                prompt_block=prompt_block,
                target_sequences=[1],
                candidate_count=4
            )

        f2 = res2["frames"][0]
        # 第 2 轮优选选中的是本批第 3 张 (即全局候选 #7)
        assert f2["chosen_candidate_index"] == 7
        assert len(f2["candidates"]) == 8
        assert [c["index"] for c in f2["candidates"]] == list(range(1, 9))
        assert f2["candidates"][6]["is_chosen"] is True   # candidate 7
        assert f2["candidates"][1]["is_chosen"] is False  # candidate 2 is now False

        # 验证当前主帧内容是 candidate_7
        img1_path = os.path.join(frames_dir, "img_001.webp")
        with open(img1_path, "rb") as f:
            assert f.read() == b"candidate_content_7"

        # 3. 手动切换回第 1 轮的候选 #2
        switched = csp.switch_frame_candidate(temp_project["project_name"], seq=1, new_candidate_index=2)
        sw_f = switched["frames"][0]
        assert sw_f["chosen_candidate_index"] == 2
        assert sw_f["candidates"][1]["is_chosen"] is True
        assert sw_f["candidates"][6]["is_chosen"] is False
        assert len(sw_f["candidates"]) == 8  # 候选池全量 8 张依然保留

        with open(img1_path, "rb") as f:
            assert f.read() == b"candidate_content_2"


def test_standard_generation_archives_to_candidate_pool(temp_project):
    """测试标准生成模式下，生成的帧同样自动归档进候选池并记录在 manifest candidates 中。"""
    prompt_block = "IMAGE 1:\nPrompt: Standard mountain cabin\n"
    frames_dir = temp_project["frames_dir"]

    def fake_make_img(config, prompt, target_path, control_prompt=None):
        with open(target_path, "wb") as f:
            f.write(b"standard_frame_1_content")
        return "api"

    with patch.object(fg, "_generate_text_image", side_effect=fake_make_img), \
         patch("tools.collage.build_keyframe_collage", return_value="frames/collage.jpg"):
        res = fg.generate_frame_sequence(
            config={"imageBackend": "api", "allowTextOnlyAnchor": True},
            title=temp_project["project_name"],
            prompt_block=prompt_block
        )

    assert "frames" in res
    f1 = res["frames"][0]
    assert "candidates" in f1
    assert len(f1["candidates"]) >= 1
    assert f1["candidates"][0]["index"] == 1
    assert f1["candidates"][0]["is_chosen"] is True

    # 验证候选池物理文件存在
    cand_path = os.path.join(frames_dir, "candidates", "frame_001", "candidate_1.webp")
    assert os.path.exists(cand_path)
    with open(cand_path, "rb") as f:
        assert f.read() == b"standard_frame_1_content"


def test_standard_then_4in1_accumulates_and_preserves_initial_frame(temp_project):
    """测试先走单张标准生成，再走 4选1 模式时，原标准生成图安全转为 candidate_1，且后续 4 张候选顺延为 2..5。"""
    frames_dir = temp_project["frames_dir"]
    proj_name = temp_project["project_name"]

    # 1. 模拟单张标准生成
    def fake_make_img(config, prompt, target_path, control_prompt=None):
        with open(target_path, "wb") as f:
            f.write(b"initial_standard_frame_1_content")
        return "api"

    with patch.object(fg, "_generate_text_image", side_effect=fake_make_img), \
         patch("tools.collage.build_keyframe_collage", return_value="frames/collage.jpg"):
        res1 = fg.generate_frame_sequence(
            config={"imageBackend": "api", "allowTextOnlyAnchor": True},
            title=proj_name,
            prompt_block="IMAGE 1:\nPrompt: Initial standard anchor\n"
        )

    assert len(res1["frames"]) == 1
    f1 = res1["frames"][0]
    assert len(f1["candidates"]) == 1
    assert f1["candidates"][0]["index"] == 1
    assert f1["chosen_candidate_index"] == 1

    # 2. 模拟随后对第 1 帧发起 4选1 智能生成
    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        c_num = os.path.basename(out_path).replace(".webp", "")
        with open(out_path, "wb") as f:
            f.write(f"4in1_generated_bytes_{c_num}".encode("utf-8"))

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 3,  # 本批第 3 张 (即全局 candidate_4)
            "selection_reason": "4选1 中的第 3 张最佳",
            "candidates": [{"index": i + 1, "score": 80 + i, "strengths": f"s{i+1}", "defects": ""} for i in range(len(candidate_paths))]
        }

    with patch.object(csp, "_generate_single_api_candidate", side_effect=fake_make_cand), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        res2 = csp.run_candidate_selection_frame_sequence(
            config={"imageBackend": "api"},
            title=proj_name,
            prompt_block="IMAGE 1:\nPrompt: 4in1 upgraded anchor\n",
            target_sequences=[1],
            candidate_count=4
        )

    f2 = res2["frames"][0]
    # 候选池应当包含全量 5 张 (1: 原标准生成帧, 2..5: 4选1 新生成的 4 张候选)
    assert len(f2["candidates"]) == 5
    assert [c["index"] for c in f2["candidates"]] == [1, 2, 3, 4, 5]
    # 选中的是全局 candidate_4
    assert f2["chosen_candidate_index"] == 4
    assert f2["candidates"][3]["is_chosen"] is True   # index 4
    assert f2["candidates"][0]["is_chosen"] is False  # index 1

    # 验证物理文件 1..5 均存在
    cand_dir = os.path.join(frames_dir, "candidates", "frame_001")
    for i in range(1, 6):
        p = os.path.join(cand_dir, f"candidate_{i}.webp")
        assert os.path.exists(p)

    # 验证 candidate_1 依然是原初的 initial_standard_frame_1_content
    with open(os.path.join(cand_dir, "candidate_1.webp"), "rb") as f:
        assert f.read() == b"initial_standard_frame_1_content"


def test_4in1_then_standard_retry_accumulates_candidates(temp_project):
    """测试先走 4选1 模式生成 4 张候选，再对该帧执行单张标准重试时，新生成图归档为 candidate_5 且保留 1..4。"""
    frames_dir = temp_project["frames_dir"]
    proj_name = temp_project["project_name"]

    def fake_make_cand(config, p_variant, reference_path, out_path, is_text_only, ctrl_prompt):
        c_num = os.path.basename(out_path).replace(".webp", "")
        with open(out_path, "wb") as f:
            f.write(f"cand_bytes_{c_num}".encode("utf-8"))

    def fake_eval(config, prompt_text, reference_path, candidate_paths, seq, on_progress=None):
        return {
            "best_index": 2,
            "selection_reason": "候选 2 最好",
            "candidates": [{"index": i + 1, "score": 85, "strengths": "", "defects": ""} for i in range(len(candidate_paths))]
        }

    # 1. 运行 4选1 生成
    with patch.object(csp, "_generate_single_api_candidate", side_effect=fake_make_cand), \
         patch.object(csp, "evaluate_and_select_best_candidate", side_effect=fake_eval), \
         patch.object(csp, "_generate_full_collage_from_frames", return_value="frames/collage.jpg"):
        res1 = csp.run_candidate_selection_frame_sequence(
            config={"imageBackend": "api"},
            title=proj_name,
            prompt_block="IMAGE 1:\nPrompt: 4in1 anchor\n",
            candidate_count=4
        )

    assert len(res1["frames"][0]["candidates"]) == 4

    # 2. 单张重试生成第 1 帧
    def fake_retry_img(config, prompt, target_path, control_prompt=None):
        with open(target_path, "wb") as f:
            f.write(b"single_retry_new_content")
        return "api"

    with patch.object(fg, "_generate_text_image", side_effect=fake_retry_img), \
         patch("tools.collage.build_keyframe_collage", return_value="frames/collage.jpg"):
        res2 = fg.generate_frame_sequence(
            config={"imageBackend": "api", "allowTextOnlyAnchor": True},
            title=proj_name,
            prompt_block="IMAGE 1:\nPrompt: Single retry anchor\n",
            target_sequences=[1]
        )

    f2 = res2["frames"][0]
    # 候选池必须全量保留 1..5，新重试图为 candidate_5 且设为当前采用
    assert len(f2["candidates"]) == 5
    assert [c["index"] for c in f2["candidates"]] == [1, 2, 3, 4, 5]
    assert f2["chosen_candidate_index"] == 5
    assert f2["candidates"][4]["is_chosen"] is True
    assert f2["candidates"][1]["is_chosen"] is False

    # 验证磁盘物理文件
    cand5_path = os.path.join(frames_dir, "candidates", "frame_001", "candidate_5.webp")
    assert os.path.exists(cand5_path)
    with open(cand5_path, "rb") as f:
        assert f.read() == b"single_retry_new_content"


def test_multiple_standard_retries_accumulate(temp_project):
    """测试连续多次单张标准生成/重试时，每次均按 1..N 递增全量入池。"""
    proj_name = temp_project["project_name"]

    for round_idx in range(1, 4):
        def make_round_img(config, prompt, target_path, control_prompt=None, r=round_idx):
            with open(target_path, "wb") as f:
                f.write(f"standard_round_{r}".encode("utf-8"))
            return "api"

        with patch.object(fg, "_generate_text_image", side_effect=make_round_img), \
             patch("tools.collage.build_keyframe_collage", return_value="frames/collage.jpg"):
            res = fg.generate_frame_sequence(
                config={"imageBackend": "api", "allowTextOnlyAnchor": True},
                title=proj_name,
                prompt_block="IMAGE 1:\nPrompt: Standard retry test\n",
                target_sequences=[1]
            )

        f = res["frames"][0]
        assert len(f["candidates"]) == round_idx
        assert f["chosen_candidate_index"] == round_idx
        assert f["candidates"][round_idx - 1]["is_chosen"] is True


def test_upload_frame_archives_to_candidate_pool(temp_project):
    """测试手动上传图片接口 (/api/upload_frame) 能够将上传的图自动归档为候选图。"""
    import io
    from http.client import HTTPMessage
    from PIL import Image
    import server

    proj_name = temp_project["project_name"]
    proj_dir = temp_project["project_dir"]
    frames_dir = temp_project["frames_dir"]

    # 先初始化一个已有 candidate_1 的 manifest
    manifest_data = {
        "title": proj_name,
        "aspect_ratio": "9:16",
        "frames": [
            {
                "slot": 1,
                "sequence": 1,
                "file": "frames/img_001.webp",
                "url": "/frames/img_001.webp",
                "model": "gemini",
                "chosen_candidate_index": 1,
                "candidates": [
                    {"index": 1, "file": "frames/candidates/frame_001/candidate_1.webp", "url": "/frames/candidates/frame_001/candidate_1.webp", "is_chosen": True}
                ]
            }
        ]
    }
    with open(os.path.join(proj_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    cand_dir = os.path.join(frames_dir, "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    with open(os.path.join(cand_dir, "candidate_1.webp"), "wb") as f:
        f.write(b"existing_candidate_1")
    with open(os.path.join(frames_dir, "img_001.webp"), "wb") as f:
        f.write(b"existing_candidate_1")

    # 构造上传请求
    img_byte_arr = io.BytesIO()
    Image.new("RGB", (90, 160), color=(100, 150, 200)).save(img_byte_arr, format="WEBP")
    img_bytes = img_byte_arr.getvalue()

    boundary = "----WebKitFormBoundaryTest123456"
    body_parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n{proj_name}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"sequence\"\r\n\r\n1\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"upload.webp\"\r\nContent-Type: image/webp\r\n\r\n",
    ]
    body_data = body_parts[0].encode("utf-8") + body_parts[1].encode("utf-8") + body_parts[2].encode("utf-8") + img_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    h = object.__new__(server.SparkRequestHandler)
    h.path = "/api/upload_frame"
    headers = HTTPMessage()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["Content-Length"] = str(len(body_data))
    h.headers = headers
    h.rfile = io.BytesIO(body_data)
    h._body_bytes = body_data
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))

    with patch("server.access_ok", return_value=True), \
         patch("server._get_project_dir", return_value=proj_dir):
        server.SparkRequestHandler.do_POST(h)

    assert len(sent) == 1
    resp, status = sent[0]
    assert status == 200
    assert resp["status"] == "ok"
    frame_res = resp["frame"]
    assert "candidates" in frame_res
    assert len(frame_res["candidates"]) == 2  # candidate 1 + uploaded candidate 2
    assert frame_res["chosen_candidate_index"] == 2
    assert frame_res["candidates"][1]["model"] == "manual_upload"
    assert frame_res["candidates"][1]["is_chosen"] is True


def test_get_frame_candidates_endpoint(temp_project):
    """测试 /api/get_frame_candidates 接口可以准确返回指定帧的全量候选池数据。"""
    from http.client import HTTPMessage
    import server

    proj_name = temp_project["project_name"]
    proj_dir = temp_project["project_dir"]
    frames_dir = temp_project["frames_dir"]

    cand_dir = os.path.join(frames_dir, "candidates", "frame_001")
    os.makedirs(cand_dir, exist_ok=True)
    for i in range(1, 4):
        with open(os.path.join(cand_dir, f"candidate_{i}.webp"), "wb") as f:
            f.write(f"content_{i}".encode("utf-8"))

    with open(os.path.join(frames_dir, "img_001.webp"), "wb") as f:
        f.write(b"content_2")

    manifest_data = {
        "title": proj_name,
        "frames": [
            {"sequence": 1, "chosen_candidate_index": 2, "candidates": []}
        ]
    }
    with open(os.path.join(proj_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    h = object.__new__(server.SparkRequestHandler)
    h.path = f"/api/get_frame_candidates?title={proj_name}&sequence=1"
    headers = HTTPMessage()
    h.headers = headers
    sent = []
    h._send_json = lambda obj, status=200: sent.append((obj, status))

    with patch.object(h, "_gate", return_value=True), \
         patch("server._get_project_dir", return_value=proj_dir):
        server.SparkRequestHandler.do_GET(h)

    assert len(sent) == 1
    resp, status = sent[0]
    assert status == 200
    assert resp["status"] == "ok"
    assert resp["sequence"] == 1
    assert len(resp["candidates"]) == 3
    assert resp["chosen_candidate_index"] == 2
