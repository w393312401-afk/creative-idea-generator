# -*- coding: utf-8 -*-
"""Opening-frame selection for local reference review and candidate scoring."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_pipeline as pp
from prompt_pipeline import reference_context as reference


# 完工闪帧：一句话里好几处独立的完工证据，一处早期证据都没有。
TEASER = 'Fully finished cabin interior with installed cladding, cabinetry and furniture in place.'
# 真正的起点：海蚀洞抽水清淤。注意它一个「放线/清表」词都没有——正是旧词表判不出来的那种。
REAL_START = 'Natural cavern basin intact with the pool undisturbed and standing water over bare rock.'
PUMPING = 'Pump unit set down onto the pool floor; suction hose coupled and resting in the turbid water.'


def _fact(frame, extent, timestamp=0.0):
    return {'frame': frame, 'timestamp': timestamp, 'subject': f'subject of {frame}',
            'completion_extent': extent}


class TestTeaserJudgement(unittest.TestCase):
    """判据本身：不再靠题材词表，也不会把「改造前」全景当成闪帧砍掉。"""

    def test_the_old_keyword_table_alone_would_still_miss_this_one(self):
        """这条不是在测新代码，是在钉住「为什么必须换判据」。

        海蚀洞那一单的后续帧读数是 pump / suction hose / turbid water，旧判据要求后续帧
        命中 spray paint / clearing grass / bare ground 那张放线词表才算数，一个都不沾。
        """
        layout = r'\b(marking can|spray paint|clearing grass|cutting sod|bare ground)\b'
        import re
        self.assertFalse(any(re.search(layout, t, re.I) for t in (REAL_START, PUMPING)))

    def test_stage_gap_catches_a_vocabulary_free_teaser(self):
        self.assertTrue(reference.is_teaser_flash_frame(TEASER, [REAL_START, PUMPING]))

    def test_a_normal_opening_is_not_a_teaser(self):
        self.assertFalse(reference.is_teaser_flash_frame(REAL_START, [PUMPING, PUMPING]))

    def test_completion_score_orders_late_above_early(self):
        self.assertGreater(reference.completion_score(TEASER), reference.completion_score(REAL_START))

    def test_a_renovation_before_shot_is_not_flashed_away(self):
        """旧房翻新片真的从一个成品状态开拍，而且那个状态会停留好几秒。

        只看「首帧比后面完工」会把这一拍整个砍掉——闪帧窗（跳完之后落脚的帧仍须在片头
        一瞬之内）就是为这件事存在的。
        """
        rows = [{'name': 'a.png', 'timestamp': 0.0, 'text': TEASER},
                {'name': 'b.png', 'timestamp': 3.0, 'text': REAL_START},
                {'name': 'c.png', 'timestamp': 6.0, 'text': PUMPING}]
        self.assertEqual(reference.opening_anchor_skip(rows), 0)

    def test_a_real_flash_is_skipped(self):
        rows = [{'name': 'a.png', 'timestamp': 0.0, 'text': TEASER},
                {'name': 'b.png', 'timestamp': 0.2, 'text': REAL_START},
                {'name': 'c.png', 'timestamp': 0.4, 'text': PUMPING}]
        self.assertEqual(reference.opening_anchor_skip(rows), 1)

    def test_no_readings_means_no_skip(self):
        """判不出是不是闪帧就别跳：宁可读原片首帧，也不能凭空往后挪一帧。"""
        names = ['review_001.png', 'review_002.png']
        self.assertEqual(reference.select_opening_anchor(names, {}), 'review_001.png')
        self.assertIsNone(reference.select_opening_anchor([], {}))

    def test_select_reads_timestamps_off_the_facts(self):
        facts = {'review_001.png': _fact('review_001.png', TEASER, 0.0),
                 'review_002.png': _fact('review_002.png', REAL_START, 0.2),
                 'review_003.png': _fact('review_003.png', PUMPING, 0.4)}
        picked = reference.select_opening_anchor(list(facts), facts)
        self.assertEqual(picked, 'review_002.png')


class TestLocalReferenceSelection(unittest.TestCase):
    """Local review and candidate scoring share the same opening anchor."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rf = os.path.join(self.tmp, 'review_frames')
        os.makedirs(self.rf)
        self.names = ['review_001.png', 'review_002.png', 'review_003.png', 'review_004.png']
        for n in self.names:
            with open(os.path.join(self.rf, n), 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n')

        extents = [TEASER, REAL_START, PUMPING, PUMPING]
        facts = [_fact(n, e, i * 0.2) for i, (n, e) in enumerate(zip(self.names, extents))]
        with open(os.path.join(self.tmp, 'frame_facts.json'), 'w', encoding='utf-8') as f:
            json.dump({'facts': facts}, f)

        self.beats = [
            {'index': 1, 'space': 'cave', 'start': 0.0, 'end': 0.6,
             'coverage_frames': [{'frame': n, 'timestamp': i * 0.2}
                                 for i, n in enumerate(self.names[:3])],
             'evidence_frames': self.names[:3]},
            {'index': 2, 'space': 'cave', 'start': 0.6, 'end': 1.0,
             'coverage_frames': [{'frame': 'review_004.png', 'timestamp': 0.6}],
             'evidence_frames': ['review_004.png']},
        ]
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': self.beats}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)




    def test_local_review_and_candidate_benchmark_ref(self):
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_002.png')
        self.assertEqual(roles[1], 'benchmark')

    def test_curated_evidence_frames_are_prioritized_over_raw_coverage(self):
        """当第一拍已经经过 Pass B 或人工核对选定了真实的起步证据帧（如 review_004.png），
        本地审查与候选评分必须以该帧为准，不能被包含 0s 闪帧的 coverage_frames 覆盖回 review_001.png。"""
        # 第一拍 evidence_frames 明确指向真实的起步帧 review_002.png，
        # 而 coverage_frames 首张包含了 0s 闪帧 review_001.png 且采样步长拉大到 0.65s
        self.beats[0]['evidence_frames'] = ['review_002.png', 'review_003.png']
        self.beats[0]['coverage_frames'] = [
            {'frame': 'review_001.png', 'timestamp': 0.0},
            {'frame': 'review_004.png', 'timestamp': 0.65}
        ]
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': self.beats}, f)

        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_002.png')
        self.assertEqual(roles[1], 'benchmark')

    def test_sparse_coverage_alone_also_skips_teaser_across_0_6s_step(self):
        """即使没有 evidence_frames，coverage_frames 均分采样跨度略大于 0.6s（如 0.628s）时，
        先导闪帧护栏也必须正常识别并顺延，绝不能卡在 0.6s 死线退回成品首帧。"""
        self.beats[0].pop('evidence_frames', None)
        self.beats[0]['coverage_frames'] = [
            {'frame': 'review_001.png', 'timestamp': 0.0},
            {'frame': 'review_002.png', 'timestamp': 0.65},
            {'frame': 'review_003.png', 'timestamp': 1.3}
        ]
        with open(os.path.join(self.tmp, 'timelapse_beats.json'), 'w', encoding='utf-8') as f:
            json.dump({'beats': self.beats}, f)

        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_002.png')
        self.assertEqual(roles[1], 'benchmark')


    def test_without_frame_facts_selection_falls_back_to_first_frame(self):
        """读不到读数时退回参考首帧。"""
        os.remove(os.path.join(self.tmp, 'frame_facts.json'))
        refs, _roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_001.png')


if __name__ == '__main__':
    unittest.main()

