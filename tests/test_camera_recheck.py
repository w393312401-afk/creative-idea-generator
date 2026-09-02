"""机位复核（2026-09-02）：拿 Pass A 的一手帧读数去校 Pass B 转写出来的逐拍机位。

`mixed_camera_angle` 是这批待确认项里唯一一条工艺精修够不着的（不在
`CRAFT_REFINE_CODES` 里），此前只能人工照帧核。它够不着的理由是「原片真换了机位就是
对的，模型没法从多数/少数这个统计事实推出该往哪边改」——那条理由只对「再问一次模型」
成立。真正的出路在产地上：逐拍的 camera_angle 是 **Pass B** 写的，而 Pass B 是文本
模型读摘要；真正看过帧的是 **Pass A**，逐帧多模态读数，早就落盘在 frame_facts.json
里。这一组盯的就是这条对账线，以及它三层里每一层的边界。

盯得最紧的两条：
  · 覆盖一个**已有**读数要两票起。一票只够升级去看图——覆盖会把一把几何锁挪到别处，
    那比留着一个可疑读数更贵。
  · 复核戳存的是**当时那一对读数**，不是布尔。用户手改角度后戳必须自动失效，否则界面
    会顶着「已按帧复核」宣称一件不再为真的事。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import prompt_pipeline as pp
from prompt_pipeline import reverse
from prompt_pipeline.reverse import _num


def _beat(bid, start, end, space='main room', angle='eye_level', bearing='front', **kw):
    beat = {
        'id': bid, 'start': start, 'end': end, 'stage': 'structural', 'space': space,
        'operation': 'board ceiling',
        'visual_subject': 'a ceiling under boarding',
        'camera_angle': angle, 'camera_bearing': bearing,
        'evidence_frames': kw.pop('evidence_frames', []),
    }
    beat.update(kw)
    return beat


def _overview(rows):
    """rows: [(timestamp, 帧名)]。"""
    return {'review_sampling': {'frames': [
        {'frame_path': f'/frames/{name}', 'timestamp': ts} for ts, name in rows]}}


def _facts(rows):
    """rows: [(帧名, 角度, 方位)]。"""
    return [{'frame': f'/frames/{name}', 'camera_angle': angle, 'camera_bearing': bearing}
            for name, angle, bearing in rows]


def _doc(beats):
    return {'beats': beats, 'video_duration_sec': 30.0}


def _codes(violations):
    return {v.get('code') for v in violations}


class CameraRecheckTest(unittest.TestCase):

    # ── 第一层：帧票与梯子一致 ────────────────────────────────────────────

    def test_frames_confirming_a_real_setup_change_retire_the_warn(self):
        """原片确实换了机位时，复核的产出不是「改」，是「不用再问了」。

        这条 warn 问的是「这几拍原片到底换没换机位」。帧答了，它就该从待确认清单里消失
        ——继续摆着等于让用户第二遍回答同一个问题。
        """
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (2.0, 'f02.jpg'), (5.0, 'f03.jpg'),
                              (6.0, 'f04.jpg'), (9.0, 'f05.jpg'), (10.0, 'f06.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'eye_level', 'front'), ('f04.jpg', 'eye_level', 'front'),
                        ('f05.jpg', 'bird_eye', 'front'), ('f06.jpg', 'bird_eye', 'front')])

        self.assertEqual(_codes(reverse._validate_camera_angle_consistency(beats)),
                         {'mixed_camera_angle'})

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertIsNone(report['skipped'])
        self.assertEqual(report['checked'], 3)
        self.assertEqual(report['confirmed'], 3)
        self.assertEqual(report['corrected'], [])
        self.assertEqual(report['escalated'], 0)
        # 读数一个字没动，但这条 warn 已经不再列出来。
        self.assertEqual(beats[2]['camera_angle'], 'bird_eye')
        self.assertEqual(reverse._validate_camera_angle_consistency(beats), [])

    # ── 第二层：帧票与梯子不一致 ──────────────────────────────────────────

    def test_a_pass_b_misread_is_corrected_off_the_frames_with_no_model_call(self):
        """Pass B 把 B03 转写成了鸟瞰，而看过帧的 Pass A 三票都说平视。

        这一层零调用：`config` 根本没传，走到这里就说明它不需要。
        """
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                              (9.0, 'f03.jpg'), (10.0, 'f04.jpg'), (11.0, 'f05.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'eye_level', 'front'), ('f04.jpg', 'eye_level', 'front'),
                        ('f05.jpg', 'eye_level', 'front')])

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertEqual(len(report['corrected']), 1)
        fix = report['corrected'][0]
        self.assertEqual((fix['beat_id'], fix['field'], fix['was'], fix['now']),
                         ('B03', 'camera_angle', 'bird_eye', 'eye_level'))
        self.assertEqual(fix['by'], 'frames')
        self.assertEqual(fix['votes'], '3/3')
        self.assertEqual(beats[2]['camera_angle'], 'eye_level')
        # 订正之后这个空间只剩一个机位——凭空多出来的那把几何锁没有了。
        self.assertEqual(reverse._validate_camera_angle_consistency(beats), [])
        self.assertEqual(report['confirmed'], 3)

    def test_one_dissenting_frame_cannot_overwrite_an_existing_reading(self):
        """一票不够改。覆盖一个已有读数会把一把几何锁挪到别处，这比留着可疑读数贵。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'), (9.0, 'f03.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'eye_level', 'front')])   # B03 窗内只有一张帧

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertEqual(report['corrected'], [])
        self.assertEqual(beats[2]['camera_angle'], 'bird_eye')   # 原样保留
        self.assertEqual([u['beat_id'] for u in report['unresolved']], ['B03'])
        self.assertEqual(report['unresolved'][0]['axes'], ['camera_angle'])
        # 没定案就不盖戳，warn 照旧在列。
        self.assertEqual(_codes(reverse._validate_camera_angle_consistency(beats)),
                         {'mixed_camera_angle'})

    def test_an_empty_reading_is_filled_on_a_single_vote(self):
        """补空栏与覆盖是两回事：空栏本来就没有下游后果，一票就够。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye'),
                 _beat('B04', 12, 16, angle='')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                              (9.0, 'f03.jpg'), (13.0, 'f04.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'bird_eye', 'front'), ('f04.jpg', 'bird_eye', 'front')])

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertEqual(beats[3]['camera_angle'], 'bird_eye')
        self.assertEqual([(c['beat_id'], c['was'], c['now']) for c in report['corrected']],
                         [('B04', '', 'bird_eye')])

    # ── 第三层：帧票自己分裂 ──────────────────────────────────────────────

    def test_a_split_frame_read_never_votes_a_winner_in(self):
        """2/1/1 不是一个读数，是「这一拍里镜头本来就动过」。不给 config 就原样留着。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                              (9.0, 'f03.jpg'), (10.0, 'f04.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'eye_level', 'front'), ('f04.jpg', 'bird_eye', 'front')])

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertEqual(report['corrected'], [])
        self.assertEqual(report['escalated'], 0)
        self.assertEqual(beats[2]['camera_angle'], 'bird_eye')
        self.assertEqual(report['unresolved'][0]['reason'], '帧读数不一致，需要看图复核')

    def test_only_the_split_beats_reach_the_model(self):
        """升级是**逐拍**的，且只升级定不了案的那几拍。前两层已经定案的不该再花一次钱。"""
        tmp = tempfile.mkdtemp()
        try:
            for name in ('f03.jpg', 'f04.jpg'):
                open(os.path.join(tmp, name), 'wb').close()
            beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                     _beat('B03', 8, 12, angle='bird_eye',
                           evidence_frames=['f03.jpg', 'f04.jpg'])]
            doc = _doc(beats)
            overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                                  (9.0, 'f03.jpg'), (10.0, 'f04.jpg')])
            facts = _facts([('f01.jpg', 'eye_level', 'front'),
                            ('f02.jpg', 'eye_level', 'front'),
                            ('f03.jpg', 'eye_level', 'front'),
                            ('f04.jpg', 'bird_eye', 'front')])

            with mock.patch.object(
                    pp, '_multimodal_chat',
                    return_value='{"camera_angle": "high_angle", '
                                 '"camera_bearing": "front", "moved": false}') as chat:
                doc, report = reverse.recheck_camera_setups(
                    doc, overview=overview, facts=facts,
                    config={'model': 'x'}, frames_dir=tmp)

            self.assertEqual(chat.call_count, 1)                 # 只有 B03 花了钱
            self.assertEqual(report['escalated'], 1)
            self.assertEqual(beats[2]['camera_angle'], 'high_angle')
            self.assertEqual([(c['beat_id'], c['by']) for c in report['corrected']],
                             [('B03', 'model')])
            self.assertEqual(report['confirmed'], 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_camera_that_moved_inside_one_beat_is_never_stamped(self):
        """戳的含义是「这一对读数管住整拍」。模型刚说了管不住，就不能盖。"""
        tmp = tempfile.mkdtemp()
        try:
            open(os.path.join(tmp, 'f03.jpg'), 'wb').close()
            beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                     _beat('B03', 8, 12, angle='bird_eye', evidence_frames=['f03.jpg'])]
            doc = _doc(beats)
            overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                                  (9.0, 'f03.jpg'), (10.0, 'f04.jpg')])
            facts = _facts([('f01.jpg', 'eye_level', 'front'),
                            ('f02.jpg', 'eye_level', 'front'),
                            ('f03.jpg', 'eye_level', 'front'),
                            ('f04.jpg', 'bird_eye', 'front')])

            with mock.patch.object(
                    pp, '_multimodal_chat',
                    return_value='{"camera_angle": "eye_level", '
                                 '"camera_bearing": "front", "moved": true}'):
                doc, report = reverse.recheck_camera_setups(
                    doc, overview=overview, facts=facts,
                    config={'model': 'x'}, frames_dir=tmp)

            self.assertFalse(reverse.camera_setup_verified(beats[2]))
            self.assertIn('拆拍', report['unresolved'][0]['reason'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_failed_escalation_keeps_the_earlier_corrections(self):
        """单拍调用失败不拖垮整轮——前两层对账出来的订正不能跟着丢。"""
        tmp = tempfile.mkdtemp()
        try:
            open(os.path.join(tmp, 'f05.jpg'), 'wb').close()
            beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                     _beat('B03', 8, 12, angle='bird_eye'),          # 帧票能定案 → 订正
                     _beat('B04', 12, 16, angle='worm_eye',
                           evidence_frames=['f05.jpg'])]             # 帧票分裂 → 升级失败
            doc = _doc(beats)
            overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'),
                                  (9.0, 'f03.jpg'), (10.0, 'f04.jpg'),
                                  (13.0, 'f05.jpg'), (14.0, 'f06.jpg')])
            facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                            ('f03.jpg', 'eye_level', 'front'), ('f04.jpg', 'eye_level', 'front'),
                            ('f05.jpg', 'eye_level', 'front'), ('f06.jpg', 'bird_eye', 'front')])

            with mock.patch.object(pp, '_multimodal_chat', side_effect=RuntimeError('502')):
                doc, report = reverse.recheck_camera_setups(
                    doc, overview=overview, facts=facts,
                    config={'model': 'x'}, frames_dir=tmp)

            self.assertEqual(beats[2]['camera_angle'], 'eye_level')   # B03 的订正留住了
            self.assertEqual([c['beat_id'] for c in report['corrected']], ['B03'])
            self.assertEqual(beats[3]['camera_angle'], 'worm_eye')    # B04 原样不动
            self.assertIn('502', report['unresolved'][0]['reason'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── 复核戳 ────────────────────────────────────────────────────────────

    def test_the_stamp_dies_the_moment_the_reading_is_edited(self):
        """戳存的是当时那一对读数，不是布尔——用户手改角度，它必须自动失效。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'), (9.0, 'f03.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'bird_eye', 'front')])

        doc, _report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)
        self.assertTrue(reverse.camera_setup_verified(beats[2]))
        self.assertEqual(reverse._validate_camera_angle_consistency(beats), [])

        beats[2]['camera_angle'] = 'worm_eye'           # 用户在卡点上手改
        self.assertFalse(reverse.camera_setup_verified(beats[2]))
        self.assertEqual(_codes(reverse._validate_camera_angle_consistency(beats)),
                         {'mixed_camera_angle'})

    def test_rebalancing_the_ladder_retires_every_stamp_it_copied(self):
        """`autobalance_beats` 拆合都走 `dict(b)`——戳会被原样复制进每一个新拍。

        指纹里带着拍窗，所以复制出来的那几张证明当场失效：它们是在另一个窗上数出来的票。
        这条让「先平衡还是先复核」不再是个要用户记住的顺序，顺序错了只会让 warn 回来。
        """
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8),
                 _beat('B03', 8, 12, angle='bird_eye')]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg'), (9.0, 'f03.jpg')])
        facts = _facts([('f01.jpg', 'eye_level', 'front'), ('f02.jpg', 'eye_level', 'front'),
                        ('f03.jpg', 'bird_eye', 'front')])

        doc, _report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)
        self.assertTrue(all(reverse.camera_setup_verified(b) for b in doc['beats']))
        self.assertEqual(reverse._validate_camera_angle_consistency(doc['beats']), [])

        # 把 B03 拉成一条超长拍，自动平衡会把它拆成两半——两半都继承了同一张证明。
        doc['beats'][2]['end'] = 20.0
        doc, changed = reverse.autobalance_beats(doc, overview=overview)
        self.assertTrue(changed)
        halves = [b for b in doc['beats'] if _num(b['start']) >= 8.0]
        self.assertGreater(len(halves), 1)                          # B03 确实被拆了
        self.assertTrue(all(b.get('camera_setup_verified') for b in halves))   # 戳被复制了
        self.assertFalse(any(reverse.camera_setup_verified(b) for b in halves))
        # 没动过的那两拍窗没变，它们的证明仍然作数——失效面是拍窗，不是"跑过一次平衡"。
        untouched = [b for b in doc['beats'] if _num(b['end']) <= 8.0]
        self.assertTrue(all(reverse.camera_setup_verified(b) for b in untouched))
        self.assertEqual(_codes(reverse._validate_camera_angle_consistency(doc['beats'])),
                         {'mixed_camera_angle'})                    # warn 老实回来了

    def test_a_partly_verified_space_says_how_far_it_got(self):
        """一半核完一半没核时，warn 得说清还剩什么，而不是从头再来一遍。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8, angle='bird_eye'),
                 _beat('B03', 8, 12, angle='worm_eye')]
        beats[0]['camera_setup_verified'] = {
            'reading': reverse.camera_reading_stamp(beats[0]), 'by': 'frames', 'votes': {}}
        violations = reverse._validate_camera_angle_consistency(beats)
        self.assertEqual(_codes(violations), {'mixed_camera_angle'})
        self.assertIn('已有 1 拍按帧复核过', violations[0]['message'])

    # ── 边界 ──────────────────────────────────────────────────────────────

    def test_a_space_without_a_conflict_is_never_touched(self):
        """没冲突的空间读错了也只是全空间少一把锁，不值得为它冒一次改写的险。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8)]
        doc = _doc(beats)
        overview = _overview([(1.0, 'f01.jpg'), (5.0, 'f02.jpg')])
        facts = _facts([('f01.jpg', 'bird_eye', 'back'), ('f02.jpg', 'bird_eye', 'back')])

        doc, report = reverse.recheck_camera_setups(doc, overview=overview, facts=facts)

        self.assertEqual(report['skipped'], '没有空间落下两个以上机位，无需复核')
        self.assertEqual(beats[0]['camera_angle'], 'eye_level')   # 一个字没动

    def test_a_variant_is_skipped(self):
        """变体的 reference_frames 是母本的帧，拿它改变体的机位是用错证据改错文档。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8, angle='bird_eye')]
        doc = dict(_doc(beats), variant_of='replica_abc')
        doc, report = reverse.recheck_camera_setups(doc, overview=_overview([]), facts=[])
        self.assertIn('二创变体', report['skipped'])

    def test_no_pass_a_facts_means_no_reckoning(self):
        """老 job 盘上没有 frame_facts.json——没有一手数据就不对账，也绝不硬改。"""
        beats = [_beat('B01', 0, 4), _beat('B02', 4, 8, angle='bird_eye')]
        doc = _doc(beats)
        doc, report = reverse.recheck_camera_setups(
            doc, overview=_overview([(1.0, 'f01.jpg')]), facts=[])
        self.assertIn('Pass A', report['skipped'])
        self.assertEqual(beats[1]['camera_angle'], 'bird_eye')


if __name__ == '__main__':
    unittest.main()
