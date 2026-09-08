# -*- coding: utf-8 -*-
"""
Tests for Object Ledger Auto-Fix (AI & Deterministic In-Flight Repair).
Specification: docs/replica_baseline_and_orthogonal_mutation_spec.md
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompt_pipeline.object_ledger import (
    validate_object_ledger,
    build_object_ledger,
)
from prompt_pipeline.ontology import (
    build_pack,
    infer_role,
)
from prompt_pipeline.mutate import (
    _autofix_variant_beats_deterministic,
    generate_orthogonal_variant,
)


def _broken_user_beats():
    """复现用户遇到的 4 项典型物件账违规：
    1. Beat 1 structure 倒置（subfloor 在 Beat 5 才出现）
    2. Beat 1 opening 倒置（enclosure 在 Beat 7 才出现）
    3. Beat 6 membrane 倒置（enclosure 在 Beat 7 才出现）
    4. Beat 12 phantom ceiling_rib（整条阶梯从未建造过）
    """
    return [
        {
            'index': 1, 'id': 'B01', 'stage': 'demolition', 'role': 'structure',
            'visual_subject': 'dry-stacked limestone portal arches and timber post-and-beam frame',
            'visible_action': 'worker erects the structural frame and frames door opening',
            'visible_result': 'limestone portal arch and door opening established',
            'state_before': 'raw site before build',
            'state_after': 'portal arch and door opening standing',
            'produced_objects': ['structural_frame', 'opening'],
            'inherited_objects': [],
        },
        {
            'index': 2, 'id': 'B02', 'stage': 'demolition', 'role': 'site',
            'visual_subject': 'site clearance',
            'visible_action': 'worker clears loose rocks',
            'visible_result': 'site cleared',
            'state_before': 'site overgrown',
            'state_after': 'site cleared',
            'produced_objects': [],
            'inherited_objects': [],
        },
        {
            'index': 3, 'id': 'B03', 'stage': 'demolition', 'role': 'demolition',
            'visual_subject': 'clearing basin',
            'visible_action': 'worker drains water',
            'visible_result': 'water drained',
            'state_before': 'standing water',
            'state_after': 'basin empty',
            'produced_objects': [],
            'inherited_objects': [],
        },
        {
            'index': 4, 'id': 'B04', 'stage': 'demolition', 'role': 'site',
            'visual_subject': 'prep sub-base',
            'visible_action': 'worker cleans mud',
            'visible_result': 'mud removed',
            'state_before': 'muddy floor',
            'state_after': 'clean rock floor',
            'produced_objects': [],
            'inherited_objects': [],
        },
        {
            'index': 5, 'id': 'B05', 'stage': 'foundation', 'role': 'subfloor',
            'visual_subject': 'aggregate sub-base bed',
            'visible_action': 'worker dumps and rakes crushed gravel sub-base',
            'visible_result': 'interlocking stone aggregate sub-base bed leveled',
            'state_before': 'bare rock floor',
            'state_after': 'compacted aggregate sub-base layer leveled',
            'produced_objects': ['sub_base'],
            'inherited_objects': [],
        },
        {
            'index': 6, 'id': 'B06', 'stage': 'enclosure', 'role': 'membrane',
            'visual_subject': 'vapor barrier membrane',
            'visible_action': 'worker rolls black vapor barrier membrane and tapes seams',
            'visible_result': 'vapor barrier membrane fully sealed',
            'state_before': 'aggregate bed',
            'state_after': 'continuous damp-proof membrane sealed',
            'produced_objects': ['vapour_barrier'],
            'inherited_objects': [],
        },
        {
            'index': 7, 'id': 'B07', 'stage': 'enclosure', 'role': 'enclosure',
            'visual_subject': 'exterior cladding panels',
            'visible_action': 'worker fastens exterior cladding panels to the walls',
            'visible_result': 'exterior panels seal the wall framing bays',
            'state_before': 'open frame',
            'state_after': 'exterior cladding panels installed',
            'produced_objects': ['exterior_cladding'],
            'inherited_objects': [],
        },
        {
            'index': 8, 'id': 'B08', 'stage': 'doors', 'role': 'door',
            'visual_subject': 'double entry doors',
            'visible_action': 'worker hangs timber door leaf onto strap hinges',
            'visible_result': 'double doors hung flush',
            'state_before': 'portal opening',
            'state_after': 'double door leaf mounted and latched',
            'produced_objects': ['door_leaf'],
            'inherited_objects': ['opening'],
        },
        {
            'index': 9, 'id': 'B09', 'stage': 'paving', 'role': 'paving',
            'visual_subject': 'slate flagstone patio',
            'visible_action': 'worker beds flagstone pavers',
            'visible_result': 'flagstone patio completed',
            'state_before': 'gravel path',
            'state_after': 'flagstone paving slabs laid',
            'produced_objects': ['paving'],
            'inherited_objects': [],
        },
        {
            'index': 10, 'id': 'B10', 'stage': 'framing', 'role': 'batten',
            'visual_subject': 'timber floor battens',
            'visible_action': 'worker screws floor battens',
            'visible_result': 'floor batten grid secured',
            'state_before': 'membrane floor',
            'state_after': 'floor batten grid laid',
            'produced_objects': ['floor_batten'],
            'inherited_objects': ['vapour_barrier'],
        },
        {
            'index': 11, 'id': 'B11', 'stage': 'flooring', 'role': 'flooring',
            'visual_subject': 'tongue-and-groove finish floor',
            'visible_action': 'worker lays floorboards',
            'visible_result': 'finished floor smooth and matte',
            'state_before': 'floor battens',
            'state_after': 'finish floor completed',
            'produced_objects': ['finish_floor'],
            'inherited_objects': ['floor_batten'],
        },
        {
            'index': 12, 'id': 'B12', 'stage': 'reward', 'role': 'hero',
            'visual_subject': 'reveal workshop with dog and stove',
            'visible_action': 'stove fire crackles, border collie rests on wool rug',
            'visible_result': 'warm craftsman workshop completed',
            'state_before': 'curved ceiling ribs overhead, finished timber floor below',
            'state_after': 'border collie curled in front of glowing wood stove',
            'produced_objects': [],
            'inherited_objects': ['ceiling_rib', 'finish_floor'],
        },
    ]


def _clean_source_beats():
    """对应的母本真实角色定义（Beat 1 是清理，Beat 5 是基础，Beat 7 是围护）。"""
    return [
        {'index': 1, 'id': 'B01', 'stage': 'demolition', 'operation': 'clear mud and debris'},
        {'index': 2, 'id': 'B02', 'stage': 'demolition', 'operation': 'clear rock surface'},
        {'index': 3, 'id': 'B03', 'stage': 'demolition', 'operation': 'pump standing water'},
        {'index': 4, 'id': 'B04', 'stage': 'demolition', 'operation': 'scrape bedrock'},
        {'index': 5, 'id': 'B05', 'stage': 'foundation', 'operation': 'grade aggregate subfloor'},
        {'index': 6, 'id': 'B06', 'stage': 'enclosure', 'operation': 'erect timber portal frame'},
        {'index': 7, 'id': 'B07', 'stage': 'enclosure', 'operation': 'mount exterior cladding panels'},
        {'index': 8, 'id': 'B08', 'stage': 'doors', 'operation': 'hang double entry doors'},
        {'index': 9, 'id': 'B09', 'stage': 'paving', 'operation': 'pave flagstone patio'},
        {'index': 10, 'id': 'B10', 'stage': 'framing', 'operation': 'install vapor barrier and battens'},
        {'index': 11, 'id': 'B11', 'stage': 'flooring', 'operation': 'lay solid timber flooring'},
        {'index': 12, 'id': 'B12', 'stage': 'reward', 'operation': 'final reveal with dog and fireplace'},
    ]


def test_user_broken_beats_triggers_exact_violations():
    """验证用户场景下的 4 条硬闸确实能够被精确捕获。"""
    beats = _broken_user_beats()
    violations = validate_object_ledger(beats)
    blocking = [v for v in violations if v.get('severity') == 'blocking']
    
    rules_and_objects = {(v['rule'], v['object']) for v in blocking}
    # 1. structure 依赖倒置
    assert ('inversion', 'structure') in rules_and_objects
    # 2. opening 依赖倒置
    assert ('inversion', 'opening') in rules_and_objects
    # 3. membrane 依赖倒置
    assert ('inversion', 'membrane') in rules_and_objects
    # 4. ceiling_rib 凭空出现
    assert ('phantom', 'ceiling_rib') in rules_and_objects
    assert len(blocking) >= 4


def test_deterministic_autofix_resolves_all_four_violations():
    """验证确定性自愈算法能够 100% 自动消除这 4 条违规，闭合物件账。"""
    beats = _broken_user_beats()
    src_beats = _clean_source_beats()
    axes = {
        'environment': '英格兰科茨沃尔德低洼凹槽',
        'material': '干垒石灰岩拱套 + 花旗松骨架',
        'function': '硬派皮具与手作工坊',
        'hero_reveal': '边牧犬舒服蜷卧在羊毛厚垫上',
    }
    pack = build_pack(axes)

    initial_violations = validate_object_ledger(beats)
    assert len([v for v in initial_violations if v.get('severity') == 'blocking']) >= 4

    # 运行自愈
    fixed_beats = _autofix_variant_beats_deterministic(
        source_beats=src_beats,
        variant_beats=beats,
        blocking=initial_violations,
        effective_axes=axes,
        pack=pack,
    )

    # 验证自愈后 0 阻断违规
    after_violations = validate_object_ledger(fixed_beats)
    blocking_after = [v for v in after_violations if v.get('severity') == 'blocking']
    assert blocking_after == []
    assert len(fixed_beats) == len(beats)


def test_generate_orthogonal_variant_with_autofix():
    """验证 generate_orthogonal_variant 在启用 autofix 时自动修复并顺利返回。"""
    baseline_doc = {
        'job_id': 'base_test_001',
        'video_duration_sec': 36.0,
        'beats': _clean_source_beats(),
    }
    target_axes = {
        'environment': '英格兰科茨沃尔德下沉石灰岩槽',
        'material': '手工干垒石灰岩 + 白栎木门板 + 铝箔防潮层',
        'function': '一人一犬硬核手作皮具工坊',
        'hero_reveal': '边牧犬在铸铁木柴炉火前蜷卧',
    }

    # 当 autofix=True 时，即使有初始问题也会自动闭合账本并返回
    variant_doc = generate_orthogonal_variant(
        baseline_doc=baseline_doc,
        mutation_axes=target_axes,
        preset='custom',
        strict=True,
        autofix=True,
    )
    assert variant_doc is not None
    assert 'beats' in variant_doc
    assert len(variant_doc['beats']) == len(baseline_doc['beats'])
    # 确保没有残留阻断问题
    final_blocking = [v for v in variant_doc.get('validation', []) if v.get('severity') == 'blocking']
    assert final_blocking == []
