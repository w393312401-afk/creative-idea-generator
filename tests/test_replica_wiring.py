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
    src = inspect.getsource(pp.compose_anchor_and_packet)
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
    for route in ('/api/replica/extract', '/api/replica/cancel', '/api/replica/handoff'):
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
