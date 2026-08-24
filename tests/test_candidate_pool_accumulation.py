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
