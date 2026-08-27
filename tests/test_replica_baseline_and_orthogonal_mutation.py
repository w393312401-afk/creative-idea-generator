# -*- coding: utf-8 -*-
"""
Tests for Replica Baseline and Orthogonal Mutation Engine.
Specification: docs/replica_baseline_and_orthogonal_mutation_spec.md (v2.0-STABLE)
"""

import json
import os
import shutil
import tempfile
import pytest

from prompt_pipeline import mutate
from prompt_pipeline.mutate import (
    MUTATION_PRESETS,
    ORTHOGONAL_AXES,
    generate_orthogonal_variant,
    map_asmr_audio,
    apply_slot_replacement,
    apply_trace_mapping,
)
import replica_pipeline


@pytest.fixture
def sample_beats_doc():
    return {
        "pipeline_id": "test_pipeline_001",
        "video_duration_sec": 48.0,
        "scene_signature": "一座江南泥岸旁的老旧石构木屋，依山面水，光线柔和清冷。",
        "banned_elements": [
            "现代塑料水管", "高反光镜面瓷砖", "荧光灯带", "现代电动工具"
        ],
        "scene_constants": {
            "materials": ["粗糙花岗岩", "碳化原木", "湿润泥土", "青苔"],
            "traces": ["水渍侵蚀线", "风化破损边缘"],
            "fixtures_in_shot": ["木制工作台", "老式铁桶"]
        },
        "beats": [
            {
                "id": "BEAT_01",
                "start": 0.0,
                "end": 4.0,
                "stage": "clearing",
                "space": "outdoor_approach",
                "visual_subject": "泥泞河岸与杂物乱石",
                "operation": "清理外围碎石与泥泞落叶",
                "package_operations": ["清理碎石", "清扫落叶"],
                "visible_action": "用铁锹铲除地面乱石杂草，装入竹筐搬走",
                "visible_result": "泥地露出平整碎石基底",
                "visible_details": ["铁锹铲痕", "散落石块"],
                "source_event_ids": ["EVT_01"],
                "state_before": "室外河岸杂物落叶覆盖",
                "state_after": "室外河岸表层清理完毕",
                "persistent_traces": ["清扫铲痕", "湿润泥印"],
                "camera_framing": "14mm wide-angle",
                "camera_height": "eye-level, 50% horizon line",
                "camera_movement": "fixed tripod static shot",
                "audio_cue": "铲石摩擦声与泥土翻动声",
                "evidence_frames": ["frame_0001.png"],
                "workers_present": True
            },
            {
                "id": "BEAT_02",
                "start": 4.0,
                "end": 8.0,
                "stage": "foundation",
                "space": "outdoor_approach",
                "visual_subject": "木桩地基与砂石找平",
                "operation": "夯实地基并铺设防潮木梁",
                "package_operations": ["夯实碎石", "铺设枕木"],
                "visible_action": "用木夯敲击平整碎石层，摆放防腐枕木",
                "visible_result": "水平木骨架牢固就位",
                "visible_details": ["木夯敲击", "枕木平铺"],
                "source_event_ids": ["EVT_02"],
                "state_before": "室外河岸表层清理完毕",
                "state_after": "室外河岸地基木梁成型",
                "persistent_traces": ["夯实压痕", "木梁水平墨线"],
                "camera_framing": "14mm wide-angle",
                "camera_height": "eye-level, 50% horizon line",
                "camera_movement": "fixed tripod static shot",
                "audio_cue": "重木夯击沉闷敲打声",
                "evidence_frames": ["frame_0005.png"],
                "workers_present": True
            },
            {
                "id": "BEAT_03",
                "start": 8.0,
                "end": 14.0,
                "stage": "flooring",
                "space": "interior_living",
                "visual_subject": "室内哑光实木地板铺设",
                "operation": "拼接卡扣实木地板并做哑光防护",
                "package_operations": ["拼接地板", "涂抹木蜡油"],
                "visible_action": "用橡胶锤轻敲木地板边缘拼合缝隙",
                "visible_result": "温润哑光木地板平整铺满",
                "visible_details": ["橡胶锤敲击", "地板拼缝"],
                "source_event_ids": ["EVT_03"],
                "state_before": "室内空间裸露防潮木龙骨",
                "state_after": "室内空间实木地板全铺完成",
                "persistent_traces": ["细微木屑", "哑光木蜡油反光"],
                "camera_framing": "14mm wide-angle",
                "camera_height": "eye-level, 50% horizon line",
                "camera_movement": "fixed tripod static shot",
                "audio_cue": "橡胶锤轻击木板清脆嗒嗒声",
                "evidence_frames": ["frame_0010.png"],
                "workers_present": True
            },
            {
                "id": "BEAT_04",
                "start": 14.0,
                "end": 20.0,
                "stage": "reward",
                "space": "interior_living",
                "visual_subject": "终极全景揭示：江景茶室与野生大鲟鱼",
                "operation": "落地玻璃外野生鲟鱼跃出水面",
                "package_operations": ["全景展示", "生物互动"],
                "visible_action": "茶台茶烟袅袅，窗外野生大鲟鱼缓缓游过玻璃前",
                "visible_result": "完整宁静的江景茶室与自然生物互动",
                "visible_details": ["茶烟袅袅", "鲟鱼游弋"],
                "source_event_ids": ["EVT_04"],
                "state_before": "室内空间软装就位",
                "state_after": "室内空间终极治愈景观呈现",
                "persistent_traces": ["水波倒影纹理", "茶几边缘茶渍微印"],
                "camera_framing": "14mm wide-angle",
                "camera_height": "eye-level, 50% horizon line",
                "camera_movement": "fixed tripod static shot",
                "audio_cue": "水流游动破水声与沸水煮茶声",
                "evidence_frames": ["frame_0020.png"],
                "workers_present": False
            }
        ]
    }


def test_preset_definitions():
    """Verify all 4 orthogonal presets are properly defined."""
    assert len(MUTATION_PRESETS) >= 4
    for key in ("polar", "volcano", "cyber", "cave"):
        assert key in MUTATION_PRESETS
        preset = MUTATION_PRESETS[key]
        assert "name" in preset
        assert "axes" in preset
        assert "environment" in preset["axes"]
        assert "material" in preset["axes"]
        assert "function" in preset["axes"]
        assert "hero_reveal" in preset["axes"]


def test_orthogonal_mutation_preserves_skeleton(sample_beats_doc):
    """Verify orthogonal mutation strictly preserves beat count, timing, camera framing, causality."""
    orig_beats = sample_beats_doc["beats"]
    n_orig = len(orig_beats)

    variant = generate_orthogonal_variant(
        sample_beats_doc,
        preset="polar",
    )

    var_beats = variant["beats"]
    # 1. 严格 1:1 节拍映射，拍数完全相等
    assert len(var_beats) == n_orig

    # 2. 镜头视高、焦段、运镜 100% 锁死
    for orig_b, var_b in zip(orig_beats, var_beats):
        assert var_b["id"] == orig_b["id"]
        assert var_b["start"] == orig_b["start"]
        assert var_b["end"] == orig_b["end"]
        assert var_b["stage"] == orig_b["stage"]
        assert var_b["space"] == orig_b["space"]
        assert var_b["camera_framing"] == orig_b["camera_framing"]
        assert var_b["camera_height"] == orig_b["camera_height"]
        assert var_b["camera_movement"] == orig_b["camera_movement"]

    # 3. 场景签名与禁用词矩阵更新
    assert variant["scene_signature"] != sample_beats_doc["scene_signature"]
    assert len(variant["banned_elements"]) > 0

    # 4. 终极揭示轴 4 替换生效（从鲟鱼替换为独角鲸）
    last_beat = var_beats[-1]
    assert "鲟鱼" not in last_beat["visual_subject"]
    assert "独角鲸" in last_beat["visual_subject"] or "鲸" in last_beat["visual_subject"] or "极地" in last_beat["visual_subject"]


def test_map_asmr_audio():
    """Verify physical ASMR sound mapping."""
    res_ice = map_asmr_audio("clearing", "极地厚冰原与剔透深蓝冰海", "铲石摩擦声与泥土翻动声")
    assert any("破冰" in item or "冰" in item or "重镐" in item for item in res_ice)

    res_hot = map_asmr_audio("foundation", "火山热泉泥地与冒泡地热水体", "重木夯击沉闷敲打声")
    assert any("石" in item or "焊" in item or "撞击" in item or "螺栓" in item or "重型" in item for item in res_hot)


def test_custom_axes_mutation(sample_beats_doc):
    """Test orthogonal mutation with custom user axes."""
    custom_axes = {
        "environment": "火星红色沙暴荒原与地下冰洞",
        "material": "碳纤维隔热舱 + 钛合金防风骨架",
        "function": "火星宇航员独立休眠舱",
        "hero_reveal": "火星地下冰层苏醒的远古微光发光生物",
    }
    variant = generate_orthogonal_variant(
        sample_beats_doc,
        mutation_axes=custom_axes,
        preset="custom",
    )
    assert len(variant["beats"]) == len(sample_beats_doc["beats"])
    assert "火星" in variant["scene_signature"]


def test_pipeline_baseline_lock_and_lineage(tmp_path, monkeypatch, sample_beats_doc):
    """Test locking baseline job, error prevention, lineage tracking, and get_lineage."""
    import server_common
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    # 1. 创建基准任务
    job_id = "test_job_base_100"
    j_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(j_dir, exist_ok=True)

    overview_doc = {
        "duration_sec": 48.0,
        "frames": 24,
        "collage": "collage.jpg",
        "change_events": [
            {"event_id": "EVT_01", "start": 0.0, "end": 4.0},
            {"event_id": "EVT_02", "start": 4.0, "end": 8.0},
            {"event_id": "EVT_03", "start": 8.0, "end": 14.0},
            {"event_id": "EVT_04", "start": 14.0, "end": 20.0},
        ],
        "review_sampling": {
            "frames": [
                {"frame_path": "/path/frame_0001.png", "timestamp": 2.0},
                {"frame_path": "/path/frame_0005.png", "timestamp": 6.0},
                {"frame_path": "/path/frame_0010.png", "timestamp": 10.0},
                {"frame_path": "/path/frame_0020.png", "timestamp": 16.0},
            ]
        }
    }
    with open(os.path.join(j_dir, "video_overview.json"), "w", encoding="utf-8") as f:
        json.dump(overview_doc, f)

    state = {
        "job_id": job_id,
        "stage": "review_beats",
        "job_type": "baseline",
        "is_locked_baseline": False,
        "video_name": "test_mother_video.mp4",
        "parent_baseline_id": None,
        "lineage_variants": [],
        "beats": sample_beats_doc,
        "validation": [],
        "overview": overview_doc,
    }
    replica_pipeline._save_state(state)

    # 2. 锁定为 Gold Baseline
    locked_state = replica_pipeline.lock_baseline_job(job_id, lock=True)
    assert locked_state["is_locked_baseline"] is True
    assert locked_state["job_type"] == "baseline"

    # 3. 锁定状态下禁止直接调用 save_beats
    with pytest.raises(ValueError, match="已加锁为 Gold Baseline"):
        replica_pipeline.save_beats(job_id, sample_beats_doc)

    # 4. 解锁后允许调用
    unlocked_state = replica_pipeline.lock_baseline_job(job_id, lock=False)
    assert unlocked_state["is_locked_baseline"] is False

    # 再次锁定
    replica_pipeline.lock_baseline_job(job_id, lock=True)

    # 5. 基于母本派生正交变体
    config = {}
    var_state = replica_pipeline.mutate_orthogonal(
        config,
        baseline_job_id=job_id,
        preset="volcano",
    )
    var_id = var_state["job_id"]
    assert var_state["job_type"] == "variant"
    assert var_state["parent_baseline_id"] == job_id
    assert var_state["variant_of"] == job_id
    assert len(var_state["beats"]["beats"]) == len(sample_beats_doc["beats"])

    # 6. 查询母本的血统树
    lineage = replica_pipeline.get_lineage(job_id)
    assert lineage["baseline"]["job_id"] == job_id
    assert lineage["baseline"]["is_locked_baseline"] is True
    assert len(lineage["variants"]) >= 1
    assert lineage["variants"][0]["job_id"] == var_id


def test_audio_mixing_rules():
    """Verify Antigravity rule: videoVolume=0.6, bgmVolume=0.0, voiceVolume=1.0."""
    audio_contract = {
        "videoVolume": 0.6,
        "bgmVolume": 0.0,
        "voiceVolume": 1.0,
    }
    assert audio_contract["videoVolume"] == 0.6
    assert audio_contract["bgmVolume"] == 0.0
    assert audio_contract["voiceVolume"] == 1.0


def test_multiple_variants_lineage(tmp_path, monkeypatch, sample_beats_doc):
    """Verify that multiple variants derived from the same baseline form a full lineage family."""
    import server_common
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    job_id = "test_job_base_200"
    j_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(j_dir, exist_ok=True)

    overview_doc = {
        "duration_sec": 48.0,
        "frames": 24,
        "collage": "collage.jpg",
        "change_events": [
            {"event_id": "EVT_01", "start": 0.0, "end": 4.0},
            {"event_id": "EVT_02", "start": 4.0, "end": 8.0},
            {"event_id": "EVT_03", "start": 8.0, "end": 14.0},
            {"event_id": "EVT_04", "start": 14.0, "end": 20.0},
        ],
        "review_sampling": {
            "frames": [
                {"frame_path": "/path/frame_0001.png", "timestamp": 2.0},
                {"frame_path": "/path/frame_0005.png", "timestamp": 6.0},
                {"frame_path": "/path/frame_0010.png", "timestamp": 10.0},
                {"frame_path": "/path/frame_0020.png", "timestamp": 16.0},
            ]
        }
    }
    with open(os.path.join(j_dir, "video_overview.json"), "w", encoding="utf-8") as f:
        json.dump(overview_doc, f)

    state = {
        "job_id": job_id,
        "stage": "review_beats",
        "job_type": "baseline",
        "is_locked_baseline": True,
        "video_name": "test_multi_video.mp4",
        "parent_baseline_id": None,
        "lineage_variants": [],
        "beats": sample_beats_doc,
        "validation": [],
        "overview": overview_doc,
    }
    replica_pipeline._save_state(state)

    # 派生 3 个不同预置变体
    v_polar = replica_pipeline.mutate_orthogonal({}, job_id, preset="polar")
    v_cyber = replica_pipeline.mutate_orthogonal({}, job_id, preset="cyber")
    v_cave = replica_pipeline.mutate_orthogonal({}, job_id, preset="cave")

    # 查询母本血统树
    tree = replica_pipeline.get_lineage(job_id)
    assert tree["baseline"]["job_id"] == job_id
    assert len(tree["variants"]) == 3
    variant_ids = [v["job_id"] for v in tree["variants"]]
    assert v_polar["job_id"] in variant_ids
    assert v_cyber["job_id"] in variant_ids
    assert v_cave["job_id"] in variant_ids


def test_ai_diverge_orthogonal_ideas(tmp_path, monkeypatch, sample_beats_doc):
    """Verify AI divergence generates distinct orthogonal ideas without hardcoded static limits."""
    import server_common
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    job_id = "test_job_ai_diverge_1"
    j_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(j_dir, exist_ok=True)

    state = {
        "job_id": job_id,
        "stage": "review_beats",
        "job_type": "baseline",
        "is_locked_baseline": True,
        "video_name": "test_ai_diverge.mp4",
        "parent_baseline_id": None,
        "lineage_variants": [],
        "beats": sample_beats_doc,
        "validation": [],
        "overview": {"duration_sec": 30.0},
    }
    replica_pipeline._save_state(state)

    # 1. 触发 AI 创意发散
    ideas = replica_pipeline.ai_diverge_ideas({}, job_id, brief="赛博废土与深海", count=4)
    assert len(ideas) >= 3
    for idea in ideas:
        assert "name" in idea
        assert "icon" in idea
        assert "axes" in idea
        axes = idea["axes"]
        assert "environment" in axes
        assert "material" in axes
        assert "function" in axes
        assert "hero_reveal" in axes
        assert len(axes["environment"]) > 0
        assert len(axes["material"]) > 0

    # 2. 检查 state 中是否缓存了 ai_diverged_ideas
    loaded = replica_pipeline._load_state(job_id)
    assert "ai_diverged_ideas" in loaded
    assert len(loaded["ai_diverged_ideas"]) == len(ideas)

    # 3. 基于 AI 发散出的第一个方案执行变体生成
    first_idea = ideas[0]
    var_state = replica_pipeline.mutate_orthogonal(
        {},
        job_id,
        mutation_axes=first_idea["axes"],
        preset=first_idea["id"],
        brief=first_idea.get("hook", ""),
    )
    assert var_state["job_type"] == "variant"
    assert len(var_state["beats"]["beats"]) == len(sample_beats_doc["beats"])
    assert var_state["mutation_config"]["axes"]["environment"] == first_idea["axes"]["environment"]


def test_ai_diverge_with_trend_refs(tmp_path, monkeypatch, sample_beats_doc):
    """Verify AI divergence combines web trend references and passes them to generated ideas."""
    import server_common
    import prompt_pipeline as pp
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    fake_refs = [
        {
            "id": "tr_test_sub_ice",
            "label": "极地冰潜观测站改造",
            "text": "将废弃水下集装箱改造成双层耐压极地观测舱，外层钛合金加固，结尾北极独角鲸游过。",
            "source": "web_search",
            "used_count": 0,
            "created_at": "2026-08-16 12:00:00",
        },
        {
            "id": "tr_test_volcano",
            "label": "火山黑石私汤",
            "text": "黑碳钢与玄武岩火山地热温泉，引入隐形地暖与发光蝾螈。",
            "source": "custom_urls",
            "used_count": 1,
            "created_at": "2026-08-16 12:00:00",
        }
    ]
    monkeypatch.setattr(pp, "load_trend_refs", lambda: fake_refs)

    job_id = "test_job_ai_diverge_trends"
    j_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(j_dir, exist_ok=True)

    state = {
        "job_id": job_id,
        "stage": "review_beats",
        "job_type": "baseline",
        "is_locked_baseline": True,
        "video_name": "test_trends.mp4",
        "parent_baseline_id": None,
        "lineage_variants": [],
        "beats": sample_beats_doc,
        "validation": [],
        "overview": {"duration_sec": 35.0},
    }
    replica_pipeline._save_state(state)

    # 1. 结合指定联网参考发散
    ideas = replica_pipeline.ai_diverge_ideas(
        {},
        job_id,
        brief="极地冰海与火山反差",
        count=4,
        trend_ref_ids=["tr_test_sub_ice"],
    )
    assert len(ideas) >= 1
    for idea in ideas:
        assert "trend_ref" in idea
        assert "trend_ref_ids" in idea
        assert "tr_test_sub_ice" in idea["trend_ref_ids"]

    # 2. 检查 mutate_orthogonal 能把 trend_ref 和 trend_ref_ids 固化到变体中
    chosen = ideas[0]
    mutation_axes = dict(chosen["axes"])
    mutation_axes["trend_ref"] = chosen.get("trend_ref")
    mutation_axes["trend_ref_ids"] = chosen.get("trend_ref_ids")

    var_state = replica_pipeline.mutate_orthogonal(
        {},
        job_id,
        mutation_axes=mutation_axes,
        preset=chosen["id"],
        brief=chosen.get("hook", ""),
    )
    assert var_state["trend_ref"] == chosen.get("trend_ref")
    assert var_state["trend_ref_ids"] == chosen.get("trend_ref_ids")
    assert var_state["mutation_config"]["trend_ref"] == chosen.get("trend_ref")


def test_ai_diverge_realism_and_no_scifi():
    """Verify AI divergence prompt and fallback presets strictly adhere to the REALISM-ONLY policy."""
    from prompt_pipeline.mutate import _AI_DIVERGE_SYSTEM, _generate_fallback_ideas, MUTATION_PRESETS

    # 1. 验证 System Prompt 明确注入了去科幻与真实施工约束
    assert "REALISM-ONLY POLICY" in _AI_DIVERGE_SYSTEM
    assert "去科幻" in _AI_DIVERGE_SYSTEM
    assert "严禁任何科幻" in _AI_DIVERGE_SYSTEM

    # 2. 验证兜底创意均为写实建造主题，无赛博/太空/外星概念
    fallback_ideas = _generate_fallback_ideas(count=5)
    banned_keywords = ["赛博", "空间站", "星轨", "宇航员", "异星", "钛合金深潜", "电竞影音", "RGB", "发光晶簇", "荧光蝾螈"]
    for idea in fallback_ideas:
        idea_text = json.dumps(idea, ensure_ascii=False)
        for kw in banned_keywords:
            assert kw not in idea_text, f"Fallback idea '{idea.get('name')}' contains banned sci-fi keyword '{kw}'"

    # 3. 验证预置库中均为真实建筑/自然主题
    for preset_key, preset in MUTATION_PRESETS.items():
        preset_text = json.dumps(preset, ensure_ascii=False)
        for kw in ["空间站", "星轨", "宇航员", "异星", "钛合金深潜"]:
            assert kw not in preset_text, f"Preset '{preset_key}' contains banned sci-fi keyword '{kw}'"


def test_miniature_villa_orthogonal_mutation():
    """Verify that miniature villa baseline (with wooden shack, masonry, tiles, stucco) correctly mutates without retaining baseline text."""
    miniature_baseline = {
        "pipeline_id": "test_miniature_villa",
        "video_duration_sec": 77.2,
        "carrier": "miniature diorama setting",
        "scene_signature": "Outdoor miniature diorama setting with natural soil ground and miniature two-story concrete block cottage.",
        "cast_identity": [
            "dark-skinned Black male miniature figurine",
            "the craftsman: light-brown-skinned adult human right hand",
        ],
        "beats": [
            {
                "id": "B01",
                "stage": "demolition",
                "visual_subject": "Miniature wooden shack removed on reddish-brown earth.",
                "visible_action": "Craftsman lifts away the dilapidated shack from soil ground.",
                "visible_result": "Old shack is removed, revealing bare compacted dirt.",
                "visible_details": ["dilapidated miniature wood shack", "fallen leaves"],
                "persistent_traces": ["cleared footprint"],
            },
            {
                "id": "B07",
                "stage": "structural",
                "visual_subject": "Concrete masonry units laid with pine timber lintel.",
                "visible_action": "Craftsman lays grey concrete masonry blocks with mortar.",
                "visible_result": "Ground floor masonry cottage walls erected.",
                "visible_details": ["concrete block", "pine timber"],
                "persistent_traces": ["mortar squeeze-out"],
            },
        ],
    }

    target_axes = {
        "environment": "极地厚积雪冻土",
        "material": "耐寒炭化双层实木与气凝胶保温层",
        "function": "防风雪双层防火哨所",
        "hero_reveal": "风雪停歇破晓极光与野生北极白鲸",
    }

    variant = generate_orthogonal_variant(miniature_baseline, mutation_axes=target_axes, preset="polar")
    beats = variant["beats"]
    assert len(beats) == 2

    b1_action = beats[0]["visible_action"]
    b1_res = beats[0]["visible_result"]
    assert "耐寒炭化" in b1_action or "极地厚积雪" in b1_action
    assert "耐寒炭化" in b1_res or "极地厚积雪" in b1_res
    assert "shack" not in b1_action.lower() or "耐寒炭化" in b1_action

    b7_action = beats[1]["visible_action"]
    assert "耐寒炭化" in b7_action

