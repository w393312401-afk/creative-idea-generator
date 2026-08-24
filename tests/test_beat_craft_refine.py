"""工艺精修（2026-08-23）的边界：只改措辞，不改画面。

这条路径存在的理由是「AI 修复硬伤」修不了它：`autofix_beats` 第一件事就是「没有 error
就原样返回」，而单拍体检出的全是 warn。一条 0 硬伤、8 条工艺 warn 的阶梯按下那个按钮，
模型一次都不会被调用，外层却照样清掉合成产物、把 stage 退回卡点、弹一句「已解决全部
硬伤」——本组最后一个用例钉的就是这个。

精修本身的全部风险都在一件事上：模型越界。它拿到的是「把这句话说准」的任务，但手边
摊着一整拍的 JSON，改 stage、改时间窗、改人数的冲动是真实存在的，而这三样任何一样被
改动，1:1 就不再是 1:1 了。所以合并层是白名单制，不是黑名单制。
"""

import copy
import json
import unittest

import prompt_pipeline as pp
from prompt_pipeline import reverse


def _beat(bid='B01', **kw):
    """一拍毛病齐全的原料：状态没写量、细节没位置锚、痕迹没点名、工序写成整句。"""
    beat = {
        'id': bid, 'start': 0.0, 'end': 4.0, 'stage': 'structural',
        'space': 'main room',
        'operation': 'the worker boards the ceiling of the main room',
        'package_operations': ['cut', 'fit', 'fasten'],
        'visual_subject': 'a ceiling under boarding',
        'visible_details': ['plasterboard sheets', 'pine joists', 'an impact driver'],
        'visible_action': 'a worker lifts a sheet and drives screws',
        'visible_result': 'the ceiling is partly boarded',
        'state_before': 'the ceiling is partly boarded',
        'state_after': 'the ceiling is more boarded',
        'persistent_traces': ['fallen leaves', 'green moss'],
        'workers_present': True,
        'source_event_ids': ['E01'],
        'evidence_frames': ['review_002.png'],
    }
    beat.update(kw)
    return beat


def _doc(*beats):
    return {'video_duration_sec': 4.0, 'banned_elements': [], 'beats': list(beats)}


class _Stub:
    """把模型换成一份写死的回复。记下调用次数，顺便断言「只送有毛病的那几拍」。"""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, config, system, user_text, *a, **kw):
        self.calls.append(user_text)
        for bid, reply in self.replies.items():
            if f'beat {bid}' in user_text:
                return json.dumps(reply, ensure_ascii=False)
        return json.dumps({'id': 'B00'})


def _run(test, doc, replies, **kw):
    """跑一轮精修。没有落盘的证据帧，因此走的是纯文本分支。"""
    stub = _Stub(replies)
    test.enterContext(unittest.mock.patch.object(pp, '_chat', stub))
    out, count, uncovered = reverse.refine_beat_craft({}, doc, overview={}, **kw)
    return out, count, uncovered, stub


import unittest.mock  # noqa: E402  （放在 _run 之后只是为了让上面的叙述连贯）


class TestFrozenFields(unittest.TestCase):
    """1:1 契约：画面上发生了什么，精修一个字都不许动。"""

    def test_structural_fields_in_the_reply_are_discarded(self):
        """模型把 stage/space/时间窗/工序包一起改了——白名单外的一律丢弃。"""
        doc = _doc(_beat())
        _run(self, doc, {'B01': {
            'id': 'B01',
            'stage': 'reveal',                 # 施工阶段
            'space': 'somewhere else',         # 空间序列 = 过门次数
            'start': 99.0, 'end': 120.0,       # 时间窗
            'package_operations': ['x'],       # 工序包
            'workers_present': False,          # 清场帧判据
            'source_event_ids': ['E09'],       # 事件认领
            'evidence_frames': ['review_999.png'],
            'state_after': 'three of five bays boarded, flush across the boarded run',
        }})
        beat = doc['beats'][0]
        self.assertEqual(beat['stage'], 'structural')
        self.assertEqual(beat['space'], 'main room')
        self.assertEqual((beat['start'], beat['end']), (0.0, 4.0))
        self.assertEqual(beat['package_operations'], ['cut', 'fit', 'fasten'])
        self.assertIs(beat['workers_present'], True)
        self.assertEqual(beat['source_event_ids'], ['E01'])
        self.assertEqual(beat['evidence_frames'], ['review_002.png'])
        # 白名单内的那一条照常落地，否则这个用例证明不了「丢弃」是白名单起的作用
        self.assertIn('three of five bays', beat['state_after'])

    def test_wording_fields_are_rewritten(self):
        doc = _doc(_beat())
        _run(self, doc, {'B01': {
            'id': 'B01',
            'operation': 'board ceiling',
            'visible_details': ['grey plasterboard sheets stacked against the left wall'],
            'persistent_traces': ['screw dimples along the joist line',
                                  'sawdust smear on the trestle top'],
        }})
        beat = doc['beats'][0]
        self.assertEqual(beat['operation'], 'board ceiling')
        self.assertEqual(beat['visible_details'],
                         ['grey plasterboard sheets stacked against the left wall'])
        self.assertEqual(len(beat['persistent_traces']), 2)


class TestFillOnlyFields(unittest.TestCase):
    """制作字段只补空。用户在卡点上手填过的，模型不得覆盖——他看着帧填的。"""

    def test_an_existing_value_is_never_overwritten(self):
        doc = _doc(_beat(tool='rubber mallet', shot_scale='close'))
        _run(self, doc, {'B01': {'id': 'B01', 'tool': 'cordless driver', 'shot_scale': 'wide'}})
        self.assertEqual(doc['beats'][0]['tool'], 'rubber mallet')
        self.assertEqual(doc['beats'][0]['shot_scale'], 'close')

    def test_an_empty_field_is_filled(self):
        doc = _doc(_beat())
        _run(self, doc, {'B01': {'id': 'B01', 'tool': 'cordless impact driver',
                                 'camera_move': 'slow push in', 'light_state': 'overcast midday'}})
        beat = doc['beats'][0]
        self.assertEqual(beat['tool'], 'cordless impact driver')
        self.assertEqual(beat['camera_move'], 'push_in')   # 归一化照常跑
        self.assertEqual(beat['light_state'], 'overcast midday')

    def test_worker_count_that_contradicts_the_boolean_is_refused(self):
        """人数会被归一化反写进 workers_present，而 workers_present=false 的帧才是
        IMAGE 锚点候选。模型把有人的拍写成 0，就是凭空把这一拍变成了清场帧。"""
        doc = _doc(_beat(workers_present=True))
        _run(self, doc, {'B01': {'id': 'B01', 'worker_count': 0}})
        self.assertNotIn('worker_count', doc['beats'][0])
        self.assertIs(doc['beats'][0]['workers_present'], True)

    def test_a_consistent_worker_count_is_accepted(self):
        doc = _doc(_beat(workers_present=True))
        _run(self, doc, {'B01': {'id': 'B01', 'worker_count': 2}})
        self.assertEqual(doc['beats'][0]['worker_count'], 2)


class TestScopeAndFailure(unittest.TestCase):

    def test_only_beats_with_issues_are_sent(self):
        """干净的拍不该被送去精修：一次多模态调用就是一次真金白银。"""
        clean = _beat('B02', start=4.0, end=8.0,
                      operation='board ceiling',
                      visible_details=[
                          'grey plasterboard sheets stacked against the left wall',
                          'raw sawn pine joists overhead in the middle bay',
                          'a black impact driver on the trestle at frame right'],
                      visible_result='the sheet snaps flat and the driver clutch stops',
                      state_before='two of five bays boarded, three open to the joists',
                      state_after='three of five bays boarded, flush across the boarded run',
                      persistent_traces=['screw dimples along the joist line',
                                         'sawdust smear on the trestle top'],
                      tool='cordless impact driver', sfx=['clutch chatter'],
                      shot_scale='medium', camera_move='static',
                      camera_angle='eye_level', camera_bearing='front',
                      lens_feel='wide', time_treatment='timelapse',
                      subject_placement='the open bay sits centred, filling about half the frame height',
                      worker_count=1,
                      light_state='overcast midday', material_flow='sheets from the left stack',
                      cast_action='the worker crouches under the open bay',
                      source_event_ids=['E02'], evidence_frames=['review_006.png'])
        doc = _doc(_beat(), clean)
        _, _, _, stub = _run(self, doc, {'B01': {'id': 'B01', 'operation': 'board ceiling'}})
        self.assertEqual(len(stub.calls), 1)
        self.assertIn('beat B01', stub.calls[0])

    def test_one_beat_failing_does_not_lose_the_others(self):
        """19 拍里第 7 拍网络抖一下，前 6 拍的成果不能跟着丢。"""
        doc = _doc(_beat('B01'), _beat('B02', start=4.0, end=8.0, source_event_ids=['E02']))
        calls = []

        def flaky(config, system, user_text, *a, **kw):
            calls.append(user_text)
            if 'beat B01' in user_text:
                raise RuntimeError('boom')
            return json.dumps({'id': 'B02', 'operation': 'board ceiling'})

        with unittest.mock.patch.object(pp, '_chat', flaky):
            _, count, _ = reverse.refine_beat_craft({}, doc, overview={})
        self.assertEqual(len(calls), 2)
        self.assertEqual(count, 1)
        self.assertEqual(doc['beats'][1]['operation'], 'board ceiling')

    def test_no_issues_means_no_model_call_at_all(self):
        doc = _doc()
        with unittest.mock.patch.object(pp, '_chat', _Stub({})) as stub:
            out, count, uncovered = reverse.refine_beat_craft({}, doc, overview={})
        self.assertEqual(count, 0)

    def test_new_hard_errors_roll_the_whole_thing_back(self):
        """措辞层的改写不该产出新的结构性违规。产出了就是模型越界，留半份比全退回更糟。"""
        doc = _doc(_beat())
        before = copy.deepcopy(doc)

        def bad(config, system, user_text, *a, **kw):
            # persistent_traces 掉到 2 条以下是合成器的硬闸
            return json.dumps({'id': 'B01', 'persistent_traces': ['one lonely mark']})

        with unittest.mock.patch.object(pp, '_chat', bad):
            with self.assertRaises(reverse.CraftRefineRolledBack):
                reverse.refine_beat_craft({}, doc, overview={})
        self.assertEqual(doc['beats'][0]['persistent_traces'], before['beats'][0]['persistent_traces'])


class TestJudgementSharedWithValidation(unittest.TestCase):
    """判据只有一份：界面上报的毛病，与精修实际去修的毛病，必须是同一批拍号。"""

    def test_scan_and_validate_agree_on_which_beats_are_flagged(self):
        doc = _doc(_beat('B01'), _beat('B02', start=4.0, end=8.0, source_event_ids=['E02']))
        buckets, missing = reverse._scan_beat_craft(doc['beats'])
        flagged = {bid for ids in buckets.values() for bid in ids}
        flagged |= {bid for ids in missing.values() for bid in ids}
        self.assertEqual(flagged, {'B01', 'B02'})

        messages = ' '.join(v['message'] for v in reverse._validate_beat_craft(doc['beats']))
        for bid in flagged:
            self.assertIn(bid, messages)

    def test_refine_covers_exactly_the_symptoms_the_scanner_reports(self):
        """精修声称覆盖的症状码，必须逐一对上体检器真的会报的码。

        判等而不是判包含：以后谁往体检器里加一种新症状，这条会当场红，逼他明确回答
        「这一种精修修不修」。默认漏掉的结果是——界面上多出一条 warn，按下精修永远
        不动它，而用户看不出为什么。
        """
        buckets, _missing = reverse._scan_beat_craft([])
        known = set(buckets) | {'missing_craft_fields'}
        self.assertEqual(set(reverse.CRAFT_REFINE_CODES), known,
                         '体检器与工艺精修的症状码对不上，'
                         f'差集：{known ^ set(reverse.CRAFT_REFINE_CODES)}')

    def test_every_covered_symptom_has_an_instruction_for_the_model(self):
        """码在覆盖清单里、却没有对应的祈使句，等于这一拍被送去精修但没告诉它修什么。"""
        buckets, _missing = reverse._scan_beat_craft([])
        for code in buckets:
            self.assertIn(code, reverse._CRAFT_ISSUE_BRIEFS, code)
        for field, _label in reverse._CRAFT_FIELD_LABELS:
            self.assertIn(field, reverse._CRAFT_FILL_BRIEFS, field)


if __name__ == '__main__':
    unittest.main()
