"""「从来没被判过」的帧必须一路可见，不能静默读作"没问题"。

事故来源（2026-07-30 那一单，library.json 的 frameRun）：12 帧里 4 帧停在初始的
pending_manual_review、vlm_qa_reason 全空——它们从未被任何一次审查看过。一致性审查自
2026-07-24 起改成手动触发，所以"渲染继续往后跑、审查停在第 8 帧"是正常路径，这个状态
很常见。问题在于它此前在两个地方都是隐形的：

  1. _manifest_review_summary 只把 sequence_review_skipped 算未审，pending_manual_review
     既不进 flagged 也不进 unreviewed —— 汇总于是报"审查通过"。
  2. plan_video_slots 对已知坏帧有拦截和警告，对未经判定的帧一句话都没有 —— 未审的锚点
     帧照常烧视频额度出片。

这两个洞就是"没有一次是顺利全部通过 VLM"里最难自查的那部分：三分之一的帧压根没进过考场。
"""
import os
import shutil
import tempfile
import unittest

import pipeline_orchestrator as po
import server_common
from video_generator import plan_video_slots


class TestSummaryCountsNeverJudgedFramesAsUnreviewed(unittest.TestCase):
    TITLE = 'unreviewed_coverage_test_project'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = server_common.OUTPUT_ROOT
        server_common.OUTPUT_ROOT = self.tmp
        self.project_dir = server_common._get_project_dir(self.TITLE)
        os.makedirs(os.path.join(self.project_dir, 'frames'), exist_ok=True)

    def tearDown(self):
        server_common.OUTPUT_ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, gates):
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': s, 'quality_gate': g} for s, g in gates.items()
        ]})

    def test_the_shipped_shape_is_no_longer_reported_as_clean(self):
        """事故原形：前 8 帧审过（6 通过 / 2 有问题），后 4 帧从未被审。"""
        gates = {s: 'sequence_reviewed_pass' for s in (1, 2, 3, 6, 7, 8)}
        gates.update({4: 'sequence_review_flagged', 5: 'sequence_review_flagged'})
        gates.update({s: 'pending_manual_review' for s in (9, 10, 11, 12)})
        self._write(gates)

        state = po._manifest_review_summary(self.project_dir, range(1, 13))
        self.assertEqual(sorted(state['flagged']), [4, 5])
        self.assertEqual(state['unreviewed'], [9, 10, 11, 12],
                         '从未审过的 4 帧此前既不进 flagged 也不进 unreviewed，被静默读作没问题')

    def test_missing_manifest_row_counts_as_unreviewed(self):
        """漏记不等于通过。"""
        self._write({1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass'})
        state = po._manifest_review_summary(self.project_dir, [1, 2, 3])
        self.assertEqual(state['unreviewed'], [3])

    def test_skipped_and_unknown_gates_also_count_as_unreviewed(self):
        self._write({1: 'sequence_reviewed_pass', 2: 'sequence_review_skipped',
                     3: 'i2i_fallback_degraded', 4: None})
        state = po._manifest_review_summary(self.project_dir, [1, 2, 3, 4])
        self.assertEqual(state['unreviewed'], [2, 3, 4])

    def test_a_real_verdict_under_a_manual_flag_still_counts_as_reviewed(self):
        """人工标记压着机器判定时，真实结论在 manual_flag_prev_gate 里。"""
        server_common.write_manifest(self.project_dir, {'frames': [
            {'sequence': 1, 'quality_gate': 'manual_flagged',
             'manual_flag_prev_gate': 'sequence_reviewed_pass'},
        ]})
        state = po._manifest_review_summary(self.project_dir, [1])
        self.assertEqual(state['unreviewed'], [])
        self.assertEqual(state['flagged'], {})


class TestVideoStageSaysSoBeforeSpendingCredit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.videos_dir = os.path.join(self.tmp, 'videos')
        self.frames_dir = os.path.join(self.tmp, 'frames')
        os.makedirs(self.videos_dir)
        os.makedirs(self.frames_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _frames(self, n=2):
        out = {}
        for s in range(1, n + 1):
            p = os.path.join(self.frames_dir, f'img_{s:03d}.webp')
            with open(p, 'wb') as f:
                f.write(bytes([s]) * 64)  # 各帧内容不同，避免触发"首尾近乎相同"
            out[s] = p
        return out

    VIDEOS = {1: {'body': 'v1 from IMAGE 1 to IMAGE 2', 'meta': ''}}

    def test_never_judged_anchor_frame_produces_a_warning(self):
        plans = plan_video_slots(
            self.VIDEOS, self._frames(),
            {1: 'sequence_reviewed_pass', 2: 'pending_manual_review'},
            self.videos_dir)
        plan = plans[0]
        self.assertEqual(plan['action'], 'generate', '未审不等于有问题，不该拦截')
        self.assertIn('warning', plan)
        self.assertIn('IMAGE 2', plan['warning'])
        self.assertIn('从未经过一致性审查', plan['warning'])

    def test_skipped_review_also_warns(self):
        plans = plan_video_slots(
            self.VIDEOS, self._frames(),
            {1: 'sequence_review_skipped', 2: 'sequence_reviewed_pass'},
            self.videos_dir)
        self.assertIn('IMAGE 1', plans[0]['warning'])

    def test_fully_reviewed_pair_warns_about_nothing(self):
        plans = plan_video_slots(
            self.VIDEOS, self._frames(),
            {1: 'sequence_reviewed_pass', 2: 'sequence_reviewed_pass'},
            self.videos_dir)
        self.assertNotIn('未经', plans[0].get('warning', ''))

    def test_flagged_frames_are_still_blocked_not_merely_warned(self):
        """未审是警告，已知坏帧仍然是硬拦——两条线不能混。"""
        plans = plan_video_slots(
            self.VIDEOS, self._frames(),
            {1: 'sequence_reviewed_pass', 2: 'sequence_review_flagged'},
            self.videos_dir)
        self.assertEqual(plans[0]['action'], 'blocked')


if __name__ == '__main__':
    unittest.main()
