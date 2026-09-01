"""复刻线的接线契约。

各层自己的单测都过、中间那一跳却对不上，是这条链路最贵的失败方式——它只会在
用户真的跑一单时才暴露，而那一单已经花掉了几十次视觉调用。这里盯四处接缝：

  1. 负面清单真的进了 composer 的 system prompt（不是只写进 dimensions 就没人读）；
  2. 交接给分步管线时能跳过重合成（否则渲的不是过了门禁的那一份）；
  3. 三条新路由真的注册了；
  4. 富字段绑定在 reverse → prompt_pipeline 的交界处没被丢掉。
"""

import inspect

import prompt_pipeline as pp
from _source_reader import top_level_function_source
from prompt_pipeline import reverse
from prompt_pipeline.composers.base import BaseComposer


class _Composer(BaseComposer):
    """只为拿到 banned_elements_block —— begin_run 之外的东西都不需要。"""


def test_banned_elements_reach_the_composer_system_prompt():
    """2026-08-10 之前 dimensions['banned_elements'] 零消费者：提示词写手从没见过这份
    清单，而 banned_element_hits 却在成品上扫它——一道声称存在、实际只发报告的门禁。"""
    c = _Composer()
    c.state = {'parsed_brief': {'banned_elements': ['excavator', 'tower crane']}}
    block = c.banned_elements_block()
    assert 'BANNED ELEMENTS' in block
    assert 'excavator' in block and 'tower crane' in block
    # 「连"不存在"都不许写」——否则模型会写成 "no excavator is present"，
    # 而画面里照样会长出一台。
    assert 'absent' in block


def test_a_normal_job_gets_no_banned_block():
    """非复刻单不该凭空多一段负面清单。"""
    c = _Composer()
    c.state = {'parsed_brief': {}}
    assert c.banned_elements_block() == ''
    c.state = None
    assert c.banned_elements_block() == ''


def test_compose_anchor_and_packet_carries_banned_elements_onto_the_brief():
    """dimensions → parsed_brief 这一跳断了，上面那个 block 就永远是空的。"""
    src = top_level_function_source(pp.compose_anchor_and_packet)
    assert "parsed_brief['banned_elements'] = _banned_elements" in src
    # 规划器也要看见：梯子里排进一道原片没有的工序，写手就只能违规或写空话。
    assert '_banned_plan_block' in src


def test_stepped_pipeline_can_skip_recomposition():
    """交接只递 dimensions 的话，分步管线会自己再合成一遍——既重复付钱，
    又让渲染建在一份没过 banned 门禁的提示词上。"""
    from stepped_pipeline import start_stepped_pipeline
    assert 'precomposed' in inspect.signature(start_stepped_pipeline).parameters
    import server
    assert 'precomposed' in inspect.signature(server.stepped_pipeline_start_worker).parameters


def test_the_new_routes_are_registered():
    import server
    src = inspect.getsource(server)
    for route in ('/api/replica/extract', '/api/replica/cancel', '/api/replica/handoff',
                  '/api/replica/to_project'):
        assert f"path == '{route}'" in src, f'{route} 没有注册'


def test_replica_extract_tasks_fold_into_the_same_project_row():
    """同一条 job 的 start/extract/advance 是一个项目。漏掉一种任务类型，
    工作台上就会多出一行同名记录。"""
    from server_common import REPLICA_TASK_TYPES
    assert {'replica', 'replica_extract', 'replica_advance'} <= set(REPLICA_TASK_TYPES)


def test_reverse_model_choices_survive_the_managed_mode_config_whitelist():
    """反推段的模型选择走请求体里的 config，托管模式下只有白名单里的键活得下来。

    漏掉它就是「页面上选了、后端从没收到」的静默失效——skillProfile / qaGateLevel /
    imageEditTransport 都在这个口子上栽过，而这里的代价更贵：用户以为自己已经把
    逐帧识别换成了强模型，实际整单还是 flash 读出来的。
    """
    import unittest.mock as mock
    import server_common

    client = {'frameFactsModel': 'gemini-3.1-pro-high', 'peakVerifyModel': 'off'}
    with mock.patch.object(server_common, 'SERVER_MANAGED', True):
        merged = server_common.effective_config(client)
    assert merged.get('frameFactsModel') == 'gemini-3.1-pro-high'
    assert merged.get('peakVerifyModel') == 'off'
    # 送到 Pass A 的调用里也要还在（反注入的剥壳不该把模型名一起剥掉）。
    assert reverse._pass_a_model(merged) == 'gemini-3.1-pro-high'
    assert reverse._peak_verify_model(merged) is None


def test_sampling_and_scope_reach_the_pipeline_from_the_request_body():
    """抽帧密度与送审档位是两个独立旋钮，各自要有一条从 HTTP 到流水线的完整通路。"""
    import server
    src = inspect.getsource(server)
    assert "body.get('base_fps')" in src, '抽帧密度没有从请求体接进 extract'
    assert "body.get('scope')" in src, '送审档位没有从请求体接进 start'

    import replica_pipeline
    assert 'base_fps' in inspect.signature(replica_pipeline.extract_replica_job).parameters
    assert 'scope' in inspect.signature(replica_pipeline.start_replica_job).parameters


def test_rich_binding_survives_the_reverse_to_composer_boundary():
    beat = {
        'id': 'B01', 'start': 0.0, 'end': 5.0, 'stage': 'surface',
        'operation': 'plastering', 'package_operations': ['mix', 'trowel'],
        'visual_subject': 'a wall', 'visible_details': ['bare plaster'],
        'visible_action': 'a worker trowels', 'visible_result': 'left half coated',
        'state_before': 'left two-thirds bare', 'state_after': 'left two-thirds coated',
        'persistent_traces': ['trowel ridges', 'splatter flecks'],
    }
    outline = reverse.beats_to_dimensions({'beats': [beat]})['beat_outline']
    _plan, block = pp.build_outline_plan_block(outline, len(outline))
    assert 'STATE BEFORE: left two-thirds bare' in block
    assert 'STATE AFTER: left two-thirds coated' in block
    assert 'STATE PAIR' in block          # 绑定规则，不只是把值印出来
    assert 'trowel ridges; splatter flecks' in block


def test_observed_operations_reach_the_composer_as_a_structured_field():
    """2026-08-11 整单失败的根因：工序只写进了条目正文。

    正文里那段「（工序：…）」是给规划模型读的；合成器的**确定性**通路
    （compile_outline_fallback_ladder / backfill_package_operations）读不了散文，
    只能读结构化字段。规划四轮全灭退回兜底梯子时，末拍因此只剩一个单元素占位，
    随即被合成器自己的 frame_state 硬闸判死。
    """
    beat = {
        'id': 'B09', 'start': 40.0, 'end': 48.0, 'stage': 'reveal',
        'operation': 'show', 'package_operations': ['show', 'display', 'overlook'],
        'visual_subject': 'the finished cabin', 'visible_details': ['deck railing'],
        'visible_action': 'the camera looks out through the doorway',
        'visible_result': 'the completed cabin and deck are revealed',
        'state_before': 'interior complete, deck unseen',
        'state_after': 'interior and deck both fully revealed',
        'persistent_traces': ['log railing', 'stone steps'],
    }
    outline = reverse.beats_to_dimensions({'beats': [beat]})['beat_outline']
    assert outline[0]['package_operations'] == ['show', 'display', 'overlook']
    # 归一化（送进规划器与兜底通路的那一份）必须原样保留它。
    plan, _block = pp.build_outline_plan_block(outline, len(outline))
    assert plan[0]['package_operations'] == ['show', 'display', 'overlook']


def test_fallback_ladder_from_a_reverse_outline_passes_the_frame_state_gate():
    """兜底梯子自己必须过得了下游硬闸——它的存在意义就是「模型全灭时也能交付」。

    复现 2026-08-11：9 条反推清单，末拍的 op 是 `show`（覆盖掉基础梯子的 reward，
    于是不再算运镜拍），继承来的 package_operations 只有一个元素。
    """
    from prompt_pipeline.frame_state import (
        build_frame_state_contract, validate_frame_state_contract)

    ops = ['mark', 'chop', 'carve']
    brief = {
        'mode': 'Standard',
        'beat_outline': (
            [{'op': 'clearing', 'text': f'清理第 {i} 处', 'package_operations': ops}
             for i in range(1, 9)]
            + [{'op': 'show', 'text': '推门看向完工的观景平台',
                'package_operations': ['show', 'display', 'overlook']}]
        ),
    }
    ladder = pp.compile_outline_fallback_ladder(brief, 9)
    assert ladder[-1]['package_operations'] == ['show', 'display', 'overlook']
    errors = validate_frame_state_contract(build_frame_state_contract(ladder))
    assert not [e for e in errors if 'tightly coupled operations' in e]


def test_package_operations_backfill_uses_declared_work_before_inventing_any():
    """补齐工序时的取材顺序：本拍申报 → 卡片条目申报 → 同族伴随工序。"""
    brief = {'beat_outline': [
        {'op': 'show', 'text': '推门看向观景平台（工序：show、display、overlook）'},
        {'op': 'framing', 'text': '搭起地梁栅格'},
    ]}
    ladder = [
        {'index': 1, 'operation': 'show', 'package_operations': ['show'], 'outline_refs': [1]},
        {'index': 2, 'operation': 'framing', 'package_operations': [], 'outline_refs': [2]},
        {'index': 3, 'operation': 'reward', 'package_operations': ['reward']},
    ]
    repaired = pp.backfill_package_operations(ladder, brief)

    # 1) 条目正文里申报过的真实工序优先（存量断点里的清单只有这一种形态）。
    assert ladder[0]['package_operations'] == ['show', 'display', 'overlook']
    # 2) 谁都没给第二道时才兜到同族伴随工序。
    assert ladder[1]['package_operations'] == ['framing', 'insulation']
    # 3) 运镜拍不承载施工增量，一律不碰。
    assert ladder[2]['package_operations'] == ['reward']
    assert [r[0] for r in repaired] == [1, 2]


def test_the_stage_taxonomy_agrees_across_validator_prompt_and_ui():
    """第五处接缝：施工阶段这套九档分类，判据、产出它的提示词、改它的 UI 必须一致。

    2026-08-13 的整单卡死就出在这条缝上：Pass B 的提示词只列了九个**名字**、一条释义
    没有，模型把「立墙龙骨 + 塞保温」判成 fixtures（灯具设备）；校验器照着这个标签
    推断「装了通电设备却全程没布线」，报出一条与真实病灶无关的硬伤；而前端把 stage
    渲成只读 chip，用户在唯一的人工卡点上根本改不动它。三层各自都"对"，缝上却对不齐。
    """
    import os
    import re

    stages = set(reverse._STAGE_RANK)

    # 1) 提示词必须逐档给出释义，而不只是列举名字——没有释义，分类就是按词面猜。
    for stage in stages:
        assert f'- {stage}:' in reverse._PASS_B_SYSTEM, f'Pass B 提示词缺少 {stage} 的释义'

    # 2) UI 必须给得出全部九档，否则用户改不到某一档（改不动的字段最容易错）。
    js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'js', 'replica_pipeline.js')
    with open(js_path, encoding='utf-8') as f:
        js = f.read()
    block = re.search(r'const REPLICA_BEAT_STAGE_LABELS = \{(.*?)\};', js, re.S)
    assert block, '前端的施工阶段标签表不见了'
    assert set(re.findall(r'(\w+):', block.group(1))) == stages

    # 3) 那张表必须真的接到可编辑控件上。断言渲染函数存在且写的是 stage 这个键——
    #    只有标签表、没有 select，就退回 2026-08-13 之前那个改不动的只读 chip。
    assert 'function replicaStageSelect(' in js
    assert 'data-key="stage"' in js


def test_scene_constants_reach_the_composer_system_prompt():
    """第六处接缝：场景恒常特征必须真的被写手读到。

    与 banned_elements 的历史教训同型（2026-08-10：dimensions 里写了、零消费者）。
    这条链路有四跳，断在任何一跳，产物看起来都正常，只是复刻出来不像原片——
    beats_doc → beats_to_dimensions → parsed_brief → composer system prompt。
    """
    doc = {
        'video_duration_sec': 10.0, 'banned_elements': [],
        'scene_signature': 'A mossy concrete bunker in autumn woodland, lit by one work light.',
        'scene_constants': {
            'materials': ['greenish stained concrete'],
            'fixtures_in_shot': ['tripod-mounted work light'],
        },
        'beats': [{'id': 'B01', 'start': 0, 'end': 10, 'stage': 'demolition',
                   'operation': 'clearing', 'package_operations': ['rake', 'sweep'],
                   'visual_subject': 'a floor', 'visible_details': ['leaf litter'],
                   'visible_action': 'a worker rakes', 'visible_result': 'floor is clear',
                   'state_before': 'covered', 'state_after': 'clear',
                   'persistent_traces': ['rake lines', 'damp patch'],
                   'workers_present': True, 'source_event_ids': [],
                   'evidence_frames': ['review_002.png']}],
    }

    # 1) beats → dimensions
    dims = reverse.beats_to_dimensions(doc)
    assert dims['scene_constants']['materials'] == ['greenish stained concrete']
    assert dims['scene_signature'].startswith('A mossy concrete bunker')

    # 2) dimensions → parsed_brief（compose_anchor_and_packet 内部那一跳；这里直接验
    #    它读的是哪个键，避免为了一次接线断言去跑整个 Phase 1）
    brief = {}
    if dims.get('scene_constants'):
        brief['scene_constants'] = dims['scene_constants']
    if dims.get('scene_signature'):
        brief['scene_signature'] = dims['scene_signature']

    # 3) parsed_brief → system prompt
    c = _Composer()
    c.state = {'parsed_brief': brief}
    block = c.scene_constants_block()
    assert 'SCENE CONSTANTS' in block
    assert 'greenish stained concrete' in block
    assert 'tripod-mounted work light' in block
    assert 'mossy concrete bunker' in block

    # 4) 非复刻单一个字都不该多出来
    plain = _Composer()
    plain.state = {'parsed_brief': {}}
    assert plain.scene_constants_block() == ""


def test_the_composer_prompt_actually_includes_the_scene_block():
    """段落本身写对了，但没被拼进 system prompt 的话，等于没写。"""
    import inspect

    src = inspect.getsource(BaseComposer)
    assert src.count('self.scene_constants_block()') >= 2, \
        '批量直出与单拍兜底两条路径都要带上这一段'


def test_gc_route_registered_in_server():
    import inspect
    import server
    src = inspect.getsource(server)
    assert "path == '/api/replica/gc'" in src


def test_upload_1gb_limit_and_reuse_contract():
    import inspect
    import server
    src = inspect.getsource(server)
    assert "content_length > 1024 * 1024 * 1024" in src
    assert "'existing_job': state if state.get('reused') else None" in src


def test_advance_uses_replica_pipeline_valid_actions():
    import inspect
    import server
    src = inspect.getsource(server)
    assert "from replica_pipeline import VALID_ACTIONS" in src
    assert "if action not in VALID_ACTIONS:" in src


def test_replica_library_item_contains_asmr_mixing_defaults():
    """复刻生成的项目记录必须默认携带 ASMR 混音偏好（video_volume: 0.6, bgm_volume: 0.0）。"""
    import replica_pipeline
    state = {
        'job_id': 'replica_test123',
        'stage': 'completed',
        'title': '测试复刻',
        'prompt_block': 'IMAGE 1: A photo\n\nVIDEO 1: Camera moves',
        'video_name': 'test.mp4',
    }
    item = replica_pipeline._library_item(state)
    assert item['video_volume'] == 0.6
    assert item['bgm_volume'] == 0.0
    assert item['mute_original'] is False


def test_space_monotonicity_validation_catches_regression():
    """跨空间过门后，若描述了前序工序已修好的倒退特征，必须报出 space_state_regression 警告。"""
    beats = [
        {'id': 'B01', 'space': 'outdoor', 'stage': 'demolition', 'visible_action': 'clearing fallen leaves', 'state_after': 'clean ground'},
        {'id': 'B02', 'space': 'indoor', 'stage': 'rough_in', 'state_before': 'ground full of dead leaves, decayed rafter leaking', 'visible_details': ['broken rubble']},
    ]
    warns = reverse._validate_space_monotonicity(beats)
    assert len(warns) == 1
    assert warns[0]['code'] == 'space_state_regression'
    assert warns[0]['beat_id'] == 'B02'


