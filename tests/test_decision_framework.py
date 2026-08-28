# -*- coding: utf-8 -*-
"""
Tests for the Decision Framework & Variant Compatibility Engine.
Specification: 重构判断矩阵（Decision Framework）
"""

import json
import os
import shutil
import tempfile
import pytest

from prompt_pipeline import decision_framework
from prompt_pipeline.decision_framework import (
    evaluate_variant_compatibility,
    DIMENSION_METADATA,
)
from prompt_pipeline import mutate
import replica_pipeline


@pytest.fixture
def baseline_sample():
    return {
        "pipeline_id": "test_pipeline_ground_bunker",
        "title": "荒野水岸水下集装箱改造",
        "video_name": "underground_container.mp4",
        "scene_signature": "A reinforced steel and stone survival bunker embedded into riverbank soil.",
        "beats": [
            {
                "id": "BEAT_01",
                "stage": "clearing",
                "visual_subject": "泥泞河岸与开挖地面",
                "visible_action": "工人用铁锹挖掘泥土与清理碎石",
                "state_before": "杂草落叶覆盖的河岸泥地",
                "state_after": "破土开挖出深基坑",
                "visible_details": ["铁锹铲痕", "泥土碎石"],
                "persistent_traces": ["挖掘铲痕"],
            },
            {
                "id": "BEAT_02",
                "stage": "structural",
                "visual_subject": "集装箱钢构下沉就位",
                "visible_action": "吊装就位并进行螺栓锁固与泥土回填",
                "state_before": "敞开基坑",
                "state_after": "集装箱埋入地下并四周回填压实",
                "visible_details": ["耐候钢构件", "螺栓紧固"],
                "persistent_traces": ["螺栓头", "回填压实痕迹"],
            },
            {
                "id": "BEAT_03",
                "stage": "enclosure",
                "visual_subject": "潜艇式水密舱盖与下行爬梯",
                "visible_action": "工人旋转水密手轮掀开金属舱盖并沿钢爬梯下行",
                "state_before": "地面封闭舱口",
                "state_after": "舱口开启露出垂直下行钢梯",
                "visible_details": ["密封胶圈", "金属爬梯"],
                "persistent_traces": ["铰链磨损痕迹"],
            }
        ]
    }


def test_decision_framework_dimensions_metadata():
    assert len(DIMENSION_METADATA) == 9
    expected_keys = {
        'hook_crisis',
        'character_emotion',
        'god_hand_wonder',
        'contrast_reward',
        'spatial_force',
        'material_phase',
        'scale_envelope',
        'transition_portal',
        'asmr_acoustic'
    }
    assert set(DIMENSION_METADATA.keys()) == expected_keys


def test_miniature_mediterranean_narrative_soul():
    """测试微缩地中海豪华庄园改造：
    母本包含：完全毁坏破烂屋子开局钩子、穷困夫妇看设计图纸、神来之手介入、极致反差。
    若变体保留微缩与夫妇线 -> SAFE;
    若变体被粗暴篡改为普通成年工人工地 -> 触发叙事灵魂丢失与神之手丢失报警！
    """
    miniature_baseline = {
        "title": "微缩地中海豪华庄园极限改造全景缩时",
        "video_name": "miniature_mediterranean_mansion.mp4",
        "scene_signature": "A miniature broken cabin renovated into a luxury mediterranean villa with living figurines.",
        "beats": [
            {
                "id": "BEAT_01",
                "visual_subject": "完全毁坏的破烂微缩屋子与穷困潦倒的夫妻人偶",
                "visible_action": "神来之手降临展示设计图纸，夫妻俩绝望中燃起希望",
                "state_before": "完全毁坏坍塌的微缩破木屋，暴雨中瑟瑟发抖",
                "state_after": "看清设计图纸，夫妻人偶眼神追踪巨手",
            },
            {
                "id": "BEAT_02",
                "visual_subject": "神之手微观精细施工地中海石墙与原木横梁",
                "visible_action": "巨人工匠之手用镊子与胶水精细拼装微缩白墙与罗马柱",
                "state_before": "破木残骸被清理",
                "state_after": "地中海豪华庄园毛坯骨架确立",
            },
            {
                "id": "BEAT_03",
                "visual_subject": "地中海豪华庄园全景与夫妻人偶幸福入住",
                "visible_action": "暖光点亮，夫妻俩喜极而泣搬入奢华卧室并挥手致谢",
                "state_before": "软装入场中",
                "state_after": "极致奢华地中海庄园落成，反差震撼",
            }
        ]
    }

    # 1. 忠实继承微缩夫妇与神之手叙事灵魂的二创变体 -> SAFE
    good_axes = {
        'environment': '微缩极地雪山微观苔原',
        'material': '微缩松木原木 + 迷你壁炉 + 极地羽绒',
        'function': '微缩极地暖炉避难木屋庄园',
        'hero_reveal': '微缩窗外清澈蓝冰与白鲸'
    }
    good_idea = {
        'name': '微缩极地暖炉避难所',
        'hook': '完全毁坏破屋开局，神来之手为穷困夫妇打造温暖极地豪宅',
        'axes': good_axes
    }
    res_good = evaluate_variant_compatibility(miniature_baseline, mutation_axes=good_axes, idea=good_idea)
    assert res_good['compatibility_level'] == 'compatible'
    assert res_good['can_inherit_skeleton'] is True

    # 2. 没血没肉：硬套成普通 1.78m 真人工人工地（丢失夫妇、丢失神之手） -> INCOMPATIBLE
    bad_axes = {
        'environment': '荒野泥地',
        'material': '混凝土与耐候钢',
        'function': '成人地下避难所',
        'hero_reveal': '大鲟鱼'
    }
    res_bad = evaluate_variant_compatibility(
        miniature_baseline,
        mutation_axes=bad_axes,
        brief="1.78m 工人亲自穿工装施工地下掩体，无角色无图纸"
    )
    assert res_bad['compatibility_level'] == 'incompatible'
    assert res_bad['can_inherit_skeleton'] is False
    assert len(res_bad['narrative_alerts']) > 0
    assert any('夫妇' in a or '叙事灵魂' in a or '神来之手' in a for a in res_bad['narrative_alerts'])


def test_compatible_variant_safe(baseline_sample):
    """测试拓扑同构的变体（如极地木屋/火山温泉庇护所）能够判定为 SAFE (compatible)。"""
    axes = {
        'environment': '极地厚积雪地貌与剔透深蓝峡湾',
        'material': '粗犷芬兰松木原木 + 气凝胶保温层 + 侘寂微水泥 + 黑色耐候钢',
        'function': '防风雪极地避险庇护所 + 粗石壁炉与防寒羽绒保暖卧榻',
        'hero_reveal': '野生北极白鲸群在窗外深蓝峡湾中缓缓掠过'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes)
    
    assert result['compatibility_level'] == 'compatible'
    assert result['compatibility_score'] >= 90
    assert result['can_inherit_skeleton'] is True
    assert len(result['incompatible_reasons']) == 0
    assert result['action_recommendation']['action'] == 'mutate_orthogonal'



def test_incompatible_spatial_force_cliff(baseline_sample):
    """测试悬崖挑空二创硬套地面开挖母本时，必须判定为 INCOMPATIBLE。"""
    axes = {
        'environment': '千仞悬崖峭壁绝壁挑空',
        'material': '悬空挑空玻璃与钢索',
        'function': '悬崖挑空玻璃观景吊舱',
        'hero_reveal': '绝壁上空金雕盘旋'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes, brief="悬崖绝壁挑空玻璃屋")
    
    assert result['compatibility_level'] == 'incompatible'
    assert result['compatibility_score'] < 60
    assert result['can_inherit_skeleton'] is False
    assert len(result['incompatible_reasons']) > 0
    assert any('悬崖' in r or '支撑' in r or '力学' in r for r in result['incompatible_reasons'])
    assert result['action_recommendation']['action'] == 'create_new_baseline'


def test_incompatible_material_phase_ice(baseline_sample):
    """测试纯冰雕二创硬套实体木石装配母本时，必须判定为 INCOMPATIBLE。"""
    axes = {
        'environment': '极地纯冰冰川内部',
        'material': '晶莹冰雕纯冰冰砖与热熔琉璃',
        'function': '极地冰雕旅馆与冰雪套房',
        'hero_reveal': '冰层深处远古猛犸象阴影'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes, brief="冰雕旅馆")
    
    assert result['compatibility_level'] == 'incompatible'
    assert result['compatibility_score'] < 60
    assert result['can_inherit_skeleton'] is False
    assert any('冰' in r or '相态' in r or '材料' in r for r in result['incompatible_reasons'])


def test_incompatible_scale_cavernous(baseline_sample):
    """测试千平大教堂/大礼堂二创硬套紧凑掩体母本时，必须判定为 INCOMPATIBLE。"""
    axes = {
        'environment': '荒野平原开阔地貌',
        'material': '大理石立柱与高耸拱顶',
        'function': '宏大万人大教堂大礼堂中央大厅',
        'hero_reveal': '穹顶阳光倾泻'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes, brief="宏大大礼堂")
    
    assert result['compatibility_level'] == 'incompatible'
    assert result['compatibility_score'] < 60
    assert result['can_inherit_skeleton'] is False
    assert any('体量' in r or '膨胀' in r or '尺度' in r for r in result['incompatible_reasons'])


def test_incompatible_actor_miniature_diorama(baseline_sample):
    """测试微缩沙盘小人族二创硬套成人施工母本时，必须判定为 INCOMPATIBLE。"""
    axes = {
        'environment': '微缩桌面沙盘微观森林',
        'material': '微缩黏土与迷你树枝',
        'function': '拇指人偶微缩工坊王国',
        'hero_reveal': '拇指小人欢呼'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes, brief="微缩沙盘拇指人偶王国")
    
    assert result['compatibility_level'] == 'incompatible'
    assert result['compatibility_score'] < 60
    assert result['can_inherit_skeleton'] is False
    assert any('微缩' in r or '主体' in r or '人偶' in r for r in result['incompatible_reasons'])


def test_incompatible_portal_open_terrace(baseline_sample):
    """测试敞开露天露台二创硬套水密下行舱门母本时，判定转场冲突。"""
    axes = {
        'environment': '海边无门全景露台',
        'material': '老柚木防腐木地板',
        'function': '开放全景露天甲板休闲平台',
        'hero_reveal': '远方鲸鱼喷水'
    }
    result = evaluate_variant_compatibility(baseline_sample, mutation_axes=axes, brief="全景露天无门露台")
    
    assert result['compatibility_level'] == 'incompatible'
    assert result['can_inherit_skeleton'] is False
    assert any('转场' in r or '舱门' in r or '露天' in r for r in result['incompatible_reasons'])


def test_replica_pipeline_evaluate_compatibility(tmp_path, monkeypatch, baseline_sample):
    """测试 replica_pipeline 模块上的 evaluate_variant_compatibility 接口。"""
    import server_common
    monkeypatch.setattr(server_common, "OUTPUT_ROOT", str(tmp_path))

    job_id = "test_job_decision_framework"
    job_dir = replica_pipeline.job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)

    state = {
        "job_id": job_id,
        "stage": "review_beats",
        "title": "水下集装箱黄金母本",
        "video_name": "container.mp4",
        "beats": baseline_sample,
        "is_locked_baseline": True,
    }

    replica_pipeline._save_state(state)

    # 1. 评估相容方案


    safe_axes = {
        'environment': '火山热泉与黑色玄武岩',
        'material': '耐候钢与原木',
        'function': '地热自持庇护所',
        'hero_reveal': '高山红点鲑'
    }
    rep_safe = replica_pipeline.evaluate_variant_compatibility({}, job_id, mutation_axes=safe_axes)
    assert rep_safe['compatibility_level'] == 'compatible'

    # 2. 评估不相容方案
    bad_axes = {
        'environment': '悬崖绝壁高空树冠',
        'material': '纯冰雕',
        'function': '高空树屋',
        'hero_reveal': '金雕'
    }
    rep_bad = replica_pipeline.evaluate_variant_compatibility({}, job_id, mutation_axes=bad_axes)
    assert rep_bad['compatibility_level'] == 'incompatible'
    assert rep_bad['can_inherit_skeleton'] is False


def test_ai_diverge_orthogonal_ideas_with_compatibility(baseline_sample):
    """测试 ai_diverge_orthogonal_ideas 生成的创意方案自带 compatibility 打标且全部判定通过 (SAFE)。"""
    ideas = mutate.ai_diverge_orthogonal_ideas(
        config={},
        baseline_doc=baseline_sample,
        brief="极地与火山",
        count=4
    )
    assert len(ideas) >= 1
    for idea in ideas:
        assert 'compatibility' in idea
        assert 'compatibility_level' in idea['compatibility']
        assert 'compatibility_score' in idea['compatibility']
        # 确保发散出的创意方案相容性通过，绝非一条过不去
        assert idea['compatibility']['compatibility_level'] in ('compatible', 'risky')
        assert idea['compatibility']['can_inherit_skeleton'] is True


def test_ai_diverge_miniature_diorama_ideas_pass_with_compatibility():
    """测试微缩地中海改造母本发散出的创意方案全部 100% 具备微缩与人偶叙事，兼容性全部满分通过！"""
    miniature_baseline = {
        "title": "微缩地中海豪华庄园极限改造全景缩时",
        "video_name": "miniature_mediterranean_mansion.mp4",
        "scene_signature": "A miniature broken cabin renovated into a luxury mediterranean villa with living figurines.",
        "beats": [
            {
                "id": "BEAT_01",
                "visual_subject": "完全毁坏的破烂微缩屋子与穷困潦倒的夫妻人偶",
                "visible_action": "神来之手降临展示设计图纸，夫妻俩绝望中燃起希望",
                "state_before": "完全毁坏坍塌的微缩破木屋，暴雨中瑟瑟发抖",
                "state_after": "看清设计图纸，夫妻人偶眼神追踪巨手",
            },
            {
                "id": "BEAT_02",
                "visual_subject": "神之手微观精细施工地中海石墙与原木横梁",
                "visible_action": "巨人工匠之手用镊子与胶水精细拼装微缩白墙与罗马柱",
                "state_before": "破木残骸被清理",
                "state_after": "地中海豪华庄园毛坯骨架确立",
            }
        ]
    }
    ideas = mutate.ai_diverge_orthogonal_ideas(
        config={},
        baseline_doc=miniature_baseline,
        brief="想要极地与火山风格的微缩庄园",
        count=4
    )
    assert len(ideas) == 4
    for idea in ideas:
        assert idea['compatibility']['compatibility_level'] == 'compatible'
        assert idea['compatibility']['compatibility_score'] >= 90
        assert idea['compatibility']['can_inherit_skeleton'] is True


def test_evaluate_compatibility_route_registered():
    """测试 /api/replica/evaluate_compatibility 路由已在 server.py 注册。"""
    import inspect
    import server
    src = inspect.getsource(server)
    assert "/api/replica/evaluate_compatibility" in src
    assert "evaluate_variant_compatibility" in src


