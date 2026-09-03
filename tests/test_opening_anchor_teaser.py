# -*- coding: utf-8 -*-
"""开场锚点（IMAGE 1）到底照着原片哪一张帧写。

2026-09-03 复盘（run_replica_9d2e50e291c5 海蚀洞海景木屋）：用户报「帧序列第一帧生成又
开始读爆款视频的首帧（半成品画面）」。排查下来，原片 t=0 那一帧经**四条**通道抵达
IMG 001，而先导闪帧（Teaser Flash）护栏只装在其中一条上，而且那一条排在最前面、成果
会被后面三条盖掉：

  1. 组稿期锚点对齐   reverse.anchor_reference_frame → ground_anchor_on_reference（有护栏）
  2. 组稿收尾对帧订正 observed_grounding.build_observed_digests 的 images[1]（无护栏，且在 1 之后）
  3. 渲染期链路守卫   pp.find_reference_frames_with_roles 的 ref_frames_by_beat[1]（无护栏）
  4. 4选1 打分基准    candidate_selection_pipeline 走的就是 3 那份 ref_dict

这组用例钉住两件事：四条通道共用同一份判据（reverse.select_opening_anchor），以及那份
判据不再依赖 2026-08-21 那单现拧出来的窄词表。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_pipeline as pp
from prompt_pipeline import observed_grounding as og
from prompt_pipeline import reverse


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
        self.assertTrue(reverse.is_teaser_flash_frame(TEASER, [REAL_START, PUMPING]))

    def test_a_normal_opening_is_not_a_teaser(self):
        self.assertFalse(reverse.is_teaser_flash_frame(REAL_START, [PUMPING, PUMPING]))

    def test_completion_score_orders_late_above_early(self):
        self.assertGreater(reverse.completion_score(TEASER), reverse.completion_score(REAL_START))

    def test_a_renovation_before_shot_is_not_flashed_away(self):
        """旧房翻新片真的从一个成品状态开拍，而且那个状态会停留好几秒。

        只看「首帧比后面完工」会把这一拍整个砍掉——闪帧窗（跳完之后落脚的帧仍须在片头
        一瞬之内）就是为这件事存在的。
        """
        rows = [{'name': 'a.png', 'timestamp': 0.0, 'text': TEASER},
                {'name': 'b.png', 'timestamp': 3.0, 'text': REAL_START},
                {'name': 'c.png', 'timestamp': 6.0, 'text': PUMPING}]
        self.assertEqual(reverse.opening_anchor_skip(rows), 0)

    def test_a_real_flash_is_skipped(self):
        rows = [{'name': 'a.png', 'timestamp': 0.0, 'text': TEASER},
                {'name': 'b.png', 'timestamp': 0.2, 'text': REAL_START},
                {'name': 'c.png', 'timestamp': 0.4, 'text': PUMPING}]
        self.assertEqual(reverse.opening_anchor_skip(rows), 1)

    def test_no_readings_means_no_skip(self):
        """判不出是不是闪帧就别跳：宁可读原片首帧，也不能凭空往后挪一帧。"""
        names = ['review_001.png', 'review_002.png']
        self.assertEqual(reverse.select_opening_anchor(names, {}), 'review_001.png')
        self.assertIsNone(reverse.select_opening_anchor([], {}))

    def test_select_reads_timestamps_off_the_facts(self):
        facts = {'review_001.png': _fact('review_001.png', TEASER, 0.0),
                 'review_002.png': _fact('review_002.png', REAL_START, 0.2),
                 'review_003.png': _fact('review_003.png', PUMPING, 0.4)}
        picked = reverse.select_opening_anchor(list(facts), facts)
        self.assertEqual(picked, 'review_002.png')


class TestFourChannelsAgree(unittest.TestCase):
    """四条通道对「开场锚点是哪一张」必须给同一个答案。"""

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

    def _overview(self):
        return {'review_sampling': {'frames': [
            {'frame_path': os.path.join(self.rf, n), 'timestamp': i * 0.2}
            for i, n in enumerate(self.names)]}}

    def test_channel_1_compose_anchor(self):
        picked = reverse.anchor_reference_frame({'beats': self.beats}, self._overview())
        self.assertEqual(os.path.basename(picked), 'review_002.png')

    def test_channel_2_observed_digests(self):
        d = og.build_observed_digests({'beats': self.beats}, self.tmp)
        self.assertTrue(d['image_frames'][1][0].endswith('review_002.png'))
        self.assertIn('review_002.png', d['images'][1])
        # 拍尾那一格不受影响：IMAGE 2 仍是第 1 拍的终点帧。
        self.assertTrue(d['image_frames'][2][0].endswith('review_003.png'))

    def test_channel_3_and_4_benchmark_ref(self):
        refs, roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_002.png')
        self.assertEqual(roles[1], 'benchmark')

    def test_without_frame_facts_every_channel_falls_back_to_the_first_frame(self):
        """读不到读数时四条通道一致退回原片首帧——判据缺席不该让任何一条自己发挥。"""
        os.remove(os.path.join(self.tmp, 'frame_facts.json'))
        self.assertEqual(
            os.path.basename(reverse.anchor_reference_frame({'beats': self.beats}, self._overview())),
            'review_001.png')
        d = og.build_observed_digests({'beats': self.beats}, self.tmp)
        self.assertTrue(d['image_frames'][1][0].endswith('review_001.png'))
        refs, _roles, _ = pp.find_reference_frames_with_roles(self.tmp, total_beats=2)
        self.assertEqual(os.path.basename(refs[1]), 'review_001.png')


if __name__ == '__main__':
    unittest.main()
