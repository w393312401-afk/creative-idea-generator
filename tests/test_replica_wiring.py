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
