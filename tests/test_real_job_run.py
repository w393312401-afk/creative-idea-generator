# -*- coding: utf-8 -*-
"""复刻任务的端到端生命周期：锁定母本 → 四大预置正交变体 → 派生 job → 血统树。

原版（2026-08-19 前）跑在 outputs/ 里一个真实存在的 job 上，有两个硬伤：

  1. 它指名的 replica_7fc89a0bd5d5 早就不在了，退化成 list_replica_jobs()[0]，
     抓到哪个 job 全看目录里剩什么——那个 job 停在 confirm_cost、beats 为空，
     测试便长期挂在 `assert len(beats) > 0` 上。
  2. 更要命的是它会**写用户的真实数据**：lock_baseline_job 给真 job 加锁，
     mutate_orthogonal 还会在 outputs/ 里凭空生出一个变体 job 目录。

现在整条链路跑在 tmp_path 下自造的母本上（OUTPUT_ROOT 打桩，见 baseline_job），
既不依赖任何现存产物，也一个字节都不碰 outputs/。
"""

import json
import os

import pytest

import replica_pipeline
from prompt_pipeline.mutate import MUTATION_PRESETS, generate_orthogonal_variant


# 四拍：清理 → 地基 → 铺装 → 终极揭示。拍数/时间窗与下面 overview 的
# change_events 一一对应，validate_beats 才不会报硬伤（否则锁不上母本）。
_BEAT_ROWS = [
    # (id, start, end, stage, space, visual_subject, operation, audio_cue, workers)
    ("BEAT_01", 0.0, 4.0, "clearing", "outdoor_approach",
     "泥泞河岸与杂物乱石", "清理外围碎石与泥泞落叶",
     "铲石摩擦声与泥土翻动声", True),
    ("BEAT_02", 4.0, 8.0, "foundation", "outdoor_approach",
     "木桩地基与砂石找平", "夯实地基并铺设防潮木梁",
     "重木夯击沉闷敲打声", True),
    ("BEAT_03", 8.0, 14.0, "flooring", "interior_living",
     "室内哑光实木地板铺设", "拼接卡扣实木地板并做哑光防护",
     "橡胶锤轻击木板清脆嗒嗒声", True),
    ("BEAT_04", 14.0, 20.0, "reward", "interior_living",
     "终极全景揭示：江景茶室与野生大鲟鱼", "落地玻璃外野生鲟鱼跃出水面",
     "水流游动破水声与沸水煮茶声", False),
]

# 镜头三件套全程锁死——正交变异的核心契约就是只换内容不换机位，
# 所有拍共用同一组值，变体里必须原样保留。
_CAMERA = {
    "camera_framing": "14mm wide-angle",
    "camera_height": "eye-level, 50% horizon line",
    "camera_movement": "fixed tripod static shot",
}


@pytest.fixture
def beats_doc():
    beats = []
    for i, (bid, start, end, stage, space, subject, op, audio, workers) in enumerate(_BEAT_ROWS, 1):
        beats.append({
            "id": bid,
            "start": start,
            "end": end,
            "stage": stage,
            "space": space,
            "visual_subject": subject,
            "operation": op,
            "package_operations": [op[:4], op[-4:]],
            "visible_action": f"{op}（可见动作）",
            "visible_result": f"{subject}完成态",
            "visible_details": [f"{stage}细节A", f"{stage}细节B"],
            "source_event_ids": [f"EVT_{i:02d}"],
            "state_before": f"{space} 第{i}步施工前",
            "state_after": f"{space} 第{i}步施工后",
            "persistent_traces": [f"{stage}留痕A", f"{stage}留痕B"],
            "audio_cue": audio,
            "evidence_frames": [f"frame_{i * 5:04d}.png"],
            "workers_present": workers,
            **_CAMERA,
        })
    return {
        "pipeline_id": "test_pipeline_real_job_run",
        "video_duration_sec": 20.0,
        "scene_signature": "一座江南泥岸旁的老旧石构木屋，依山面水，光线柔和清冷。",
        "banned_elements": ["现代塑料水管", "高反光镜面瓷砖", "荧光灯带", "现代电动工具"],
        "scene_constants": {
            "materials": ["粗糙花岗岩", "碳化原木", "湿润泥土", "青苔"],
            "traces": ["水渍侵蚀线", "风化破损边缘"],
            "fixtures_in_shot": ["木制工作台", "老式铁桶"],
        },
        "beats": beats,
    }


@pytest.fixture
def baseline_job(tmp_path, monkeypatch, beats_doc):
    """在 tmp_path 下造一个已生成节拍、可被锁定的母本 job，返回 job_id。

    OUTPUT_ROOT 必须用 setattr 打桩而不是 chdir：jobs_root() 每次都重读
    server_common.OUTPUT_ROOT（模块里专门为此写了注释），chdir 穿不透它。
    """
    import server_common
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    job_id = "test_job_real_run_001"
    j_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(j_dir, exist_ok=True)

    overview_doc = {
        "duration_sec": 20.0,
        "frames": 24,
        "collage": "collage.jpg",
        "change_events": [
            {"event_id": f"EVT_{i:02d}", "start": row[1], "end": row[2]}
            for i, row in enumerate(_BEAT_ROWS, 1)
        ],
        "review_sampling": {
            "frames": [
                {"frame_path": f"/path/frame_{i * 5:04d}.png",
                 "timestamp": (row[1] + row[2]) / 2}
                for i, row in enumerate(_BEAT_ROWS, 1)
            ]
        },
    }
    with open(os.path.join(j_dir, "video_overview.json"), "w", encoding="utf-8") as f:
        json.dump(overview_doc, f)

    replica_pipeline._save_state({
        "job_id": job_id,
        "stage": "review_beats",
        "job_type": "baseline",
        "is_locked_baseline": False,
        "video_name": "test_mother_video.mp4",
        "parent_baseline_id": None,
        "lineage_variants": [],
        "beats": beats_doc,
        "validation": [],
        "overview": overview_doc,
    })
    return job_id


def test_fixture_is_isolated_from_real_outputs(baseline_job, tmp_path):
    """回归保险：这个文件曾经往真实 outputs/ 里写变体 job，绝不能重演。"""
    assert os.path.abspath(replica_pipeline.jobs_root()).startswith(os.path.abspath(str(tmp_path)))


def test_real_job_e2e_lifecycle(baseline_job, beats_doc):
    job_id = baseline_job
    n_beats = len(beats_doc["beats"])

    # [1] 状态能原样读回
    state = replica_pipeline._load_state(job_id)
    assert state is not None
    assert state["video_name"] == "test_mother_video.mp4"
    assert len((state.get("beats") or {}).get("beats") or []) == n_beats

    # [2] 锁定为 Gold Baseline——原版这里是 try/except 吞掉 ValueError 后继续，
    #     锁失败也算过；现在硬要求锁成功，锁不上就是校验真出了问题。
    locked = replica_pipeline.lock_baseline_job(job_id, lock=True)
    assert locked["is_locked_baseline"] is True
    assert locked["job_type"] == "baseline"

    # [3] 四大预置逐个派生，拍数与镜头三件套必须 1:1 保留
    for preset_key in ("polar", "volcano", "cyber", "cave"):
        var_doc = generate_orthogonal_variant(
            beats_doc,
            preset=preset_key,
            mutation_axes=MUTATION_PRESETS[preset_key]["axes"],
        )
        var_beats = var_doc.get("beats") or []
        assert len(var_beats) == n_beats, f"预置 {preset_key} 改变了拍数"
        assert var_doc.get("scene_signature") != beats_doc["scene_signature"], \
            f"预置 {preset_key} 没有换场景签名"
        assert var_doc.get("banned_elements"), f"预置 {preset_key} 没有生成禁用词"
        for orig, var in zip(beats_doc["beats"], var_beats):
            assert var["id"] == orig["id"]
            assert var["start"] == orig["start"]
            assert var["end"] == orig["end"]
            for key in _CAMERA:
                assert var[key] == orig[key], f"预置 {preset_key} 动了 {key}"

    # [4] 真正派生出一个变体 job
    var_state = replica_pipeline.mutate_orthogonal({}, baseline_job_id=job_id, preset="volcano")
    var_job_id = var_state["job_id"]
    assert var_state["job_type"] == "variant"
    assert var_state["parent_baseline_id"] == job_id
    assert var_state["variant_of"] == job_id
    assert len(var_state["beats"]["beats"]) == n_beats

    # [5] 血统树能查到这个变体
    lineage = replica_pipeline.get_lineage(job_id)
    assert lineage["baseline"]["job_id"] == job_id
    assert lineage["baseline"]["is_locked_baseline"] is True
    assert any(v["job_id"] == var_job_id for v in lineage["variants"])

    # [6] list_replica_jobs 能同时看到母本与变体（本文件是它唯一的测试调用方）
    listed = {j["job_id"] for j in replica_pipeline.list_replica_jobs()}
    assert {job_id, var_job_id} <= listed
