"""Shared scene context and banned-element compositor contracts."""
import inspect
import prompt_pipeline as pp
from _source_reader import top_level_function_source
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


def test_the_composer_prompt_actually_includes_the_scene_block():
    """段落本身写对了，但没被拼进 system prompt 的话，等于没写。"""
    import inspect

    src = inspect.getsource(BaseComposer)
    assert src.count('self.scene_constants_block()') >= 2, \
        '批量直出与单拍兜底两条路径都要带上这一段'
